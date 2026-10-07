"""Procrastinate app: periodic per-bucket ticks that read `data/sources.yaml` and enqueue a
`fetch` job per due source, plus the `run_connector` job itself (ADR 0004).

Entrypoints (see infra/compose/docker-compose.yml):

    python -m infra.scheduler.app     # the `scheduler` service: runs only the periodic ticks
                                       # (docs/20 §4.2 "singleton, leader lock in Postgres" — see
                                       # the module docstring note on how that's achieved here)
    python -m infra.scheduler.worker  # the `worker`/`browser-worker` services: consume jobs

Deliberately thin (docs/adr/0004 "a thin runner is fine"): this module never imports
`pipeline.connectors` directly. `run_connector` shells out to the same CLI a human runs by hand
(`pipeline/README.md`), so the worker and the operator's terminal exercise exactly one code path,
and a change to the connector framework's Python API cannot silently desync from what the
scheduler runs.

The loop (docs/20 §3, closed 2026-09-18 — audit §3.1 "the always-on loop is not a loop"):

    tick_<bucket>  ->  run_connector(source_id)      fetch/snapshot/diff via the CLI; writes the
                                                     `source_run` row and `source.health`
                       -> load_source(source_id, ts)  only after a run that produced a new
                                                     normalised snapshot (`status == "ok"`)
                          -> resolve_tick             organisations + proposal clusters, store-wide
                             -> enrich_tick           geocode backfill and later enrichment stages
    tick_resolve (daily)  ->  resolve_tick            safety net for rows loaded outside the chain
    admin release         ->  release_held_run        a DQ-held run an operator released (2026-09-27):
                              -> load_source ...      promote `held/` -> `normalized/`, then the chain

Overlap guards: `run_connector` and `load_source` share the per-source Procrastinate `lock`
(`cadence.execution_lock_for`), so one source is never fetched and loaded at the same moment;
`resolve_tick`/`enrich_tick` carry a `queueing_lock` (one queued at a time) and a shared `lock`
(never two store-wide passes at once). Failures: `run_connector` retries only
`TransientConnectorFailure` (network, 5xx, crash, timeout) with exponential backoff and
dead-letters on the fifth attempt; a block, a corrupt payload or a gate refusal is recorded once
and left for the next tick (`FETCH_RETRY` below; `infra/scheduler/jobs.py`).

On the "singleton" requirement: Procrastinate's periodic deferrer dedupes at the database level —
"the database will keep us from deferring the same task for the same scheduled time multiple
times" (procrastinate.periodic.PeriodicDeferrer.defer_jobs) — so running the tick task in more
than one process is actually safe, not just tolerated. The `scheduler` service is still kept to
one replica and given an empty `queues` filter (below) so that in the normal case exactly one
process is doing the ticking, matching docs/20 §4.2's process table; the DB-level dedup is the
belt-and-braces that makes a brief overlap during a rolling restart harmless rather than a
double-fetch.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import procrastinate
import yaml

from infra.scheduler import jobs
from infra.scheduler.cadence import (
    CRON_BY_BUCKET,
    execution_lock_for,
    is_due,
    queue_for_source,
    queueing_lock_for,
    safe_id,
)
from infra.scheduler.jobs import (
    ConnectorRunFailed,
    TransientConnectorFailure,
    alert_tick_job,
    freshness_tick_job,
    post_draft_tick_job,
    visibility_audit_tick_job,
)

logger = logging.getLogger("infra.scheduler")

ROOT = Path(__file__).resolve().parents[2]
SOURCES_YAML = ROOT / "data" / "sources.yaml"

# A queue name no `run_connector` job is ever enqueued to, so a worker started with
# `--queues __scheduler_only__` (the `scheduler` compose service) runs the periodic deferrer
# side-thread and never actually dequeues a fetch job itself.
SCHEDULER_ONLY_QUEUE = "__scheduler_only__"


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is required (see infra/compose/.env.example); "
            "the scheduler has nothing to enqueue jobs into without it."
        )
    # SQLAlchemy's `+psycopg` dialect suffix is not a libpq conninfo string; strip it.
    return url.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+psycopg2://", "postgresql://"
    )


def _build_connector() -> procrastinate.PsycopgConnector:
    return procrastinate.PsycopgConnector(conninfo=_database_url())


app = procrastinate.App(connector=_build_connector())


def _load_sources() -> list[dict[str, Any]]:
    doc = yaml.safe_load(SOURCES_YAML.read_text())
    sources: list[dict[str, Any]] = doc.get("sources", [])
    # `category: social_channel` entries have no connector and no cadence (docs/20 §4.3's egress
    # table is about fetch sources, not distribution channels); they are never scheduled here.
    return [s for s in sources if "cadence" in s]


#: docs/20 §4.2 "retries with exponential backoff; five failures -> dead-letter". Before
#: 2026-09-18 this was `retry=5`, which Procrastinate reads as `RetryStrategy(max_attempts=5)`
#: with `wait=0`: back-to-back retries, no backoff (audit §3.1). Procrastinate's formula is
#: `wait = exponential_wait ** (attempts + 1)` with `attempts` the attempts already made (0 on the
#: first run), and it retries while `attempts < max_attempts`. Measured on procrastinate 3.9.0,
#: `max_attempts=5` ran the job six times, one more failure than docs/20 §4.2 allows; since
#: 2026-09-27 it is 4: the job runs five times (attempts 0..4), with waits of 5 s, 25 s, 125 s and
#: 625 s (≈ 13 min in total), and the fifth failure dead-letters (docs/21 D-13). All inside a
#: daily cadence. Procrastinate has no jitter parameter; the per-host token bucket in
#: `pipeline/connectors/http.py` already spreads the requests themselves. Only
#: `TransientConnectorFailure` is retried: a block, a corrupt payload or a gate refusal is
#: recorded once (`source_run` + `source.health`) and left for the next tick.
FETCH_RETRY = procrastinate.RetryStrategy(
    max_attempts=4, exponential_wait=5, retry_exceptions=[TransientConnectorFailure]
)
LOAD_TIMEOUT_S = 1800  # a full NYISO/EIA frame loads in well under this on the reference laptop
RESOLVE_TIMEOUT_S = 3600
ENRICH_TIMEOUT_S = 1800


@app.task(queue="fetch", retry=FETCH_RETRY, pass_context=True)
def run_connector(
    context: procrastinate.JobContext,
    source_id: str,
    trigger: str = jobs.SCHEDULED_TRIGGER,
    run_id: str | None = None,
) -> None:
    """Run one connector via the same CLI a human uses (`pipeline/README.md`), record the run
    (`source_run` row, `source.health`) and, when it produced a new normalised snapshot, enqueue
    `load_source` for it under the same per-source lock.

    `trigger` is `schedule` for the bucket ticks (they defer with `source_id` only) and whatever
    the admin "run now" route passes (`manual`/`backfill`). It goes to the CLI as `--trigger`, so
    the run record itself says it — the loader copies the record's value into the row it writes —
    and to `fetch_outcome`, whose explicit value wins over the record's (2026-09-27: before this,
    the CLI's default `manual` won and every scheduled run was recorded as manual).

    `run_id` is the `source_run` row the admin "run now" route created before deferring this job
    (2026-09-27). It goes to the CLI as `--run-id`, so the runner's record and the row completed
    from it are that row, not a second one; a crash, timeout or refusal is recorded against it too,
    so it never stays `running` (`fetch_outcome`). Bucket ticks pass none; the runner makes one.

    Retries (2026-09-27): Procrastinate re-runs this job with the same arguments after a
    `TransientConnectorFailure`, and `context.job.attempts` is the number of attempts already made
    (0 the first time; `procrastinate.RetryStrategy.get_retry_decision`). A retry is its own run —
    its own row, `trigger = retry`, `attempt = attempts + 1` — so it does not reuse `run_id`, which
    the first attempt already completed. The attempt `FETCH_RETRY` will not retry again is the
    dead letter: a transient failure there is recorded `dead_lettered`.

    A single `fetch` job may not run more than 10 minutes for a plain source, 5 for a browser one
    (docs/20 §4.2); the timeout is enforced here rather than trusted to the connector itself, so a
    hung request cannot wedge a worker slot forever. `context.job.queue` reflects the queue this
    particular job was actually deferred to (`fetch` or `fetch_browser`, overridden per-source at
    defer time in `_register_bucket_tick` below), not the task's `fetch` decorator default.
    """
    timeout = 300 if context.job.queue == "fetch_browser" else 600
    attempts = int(getattr(context.job, "attempts", 0) or 0)
    if attempts > 0:
        # If the first attempt died unrecorded, its row gets no outcome now: close it.
        _close_pending(run_id, "the first attempt ended without an outcome")
        trigger, run_id = jobs.RETRY_TRIGGER, None
    else:
        trigger = jobs.normalise_trigger(trigger)
    final_attempt = FETCH_RETRY.max_attempts is not None and attempts >= FETCH_RETRY.max_attempts
    cmd = [sys.executable, "-m", "pipeline.connectors", "run", source_id]
    if run_id is not None:
        cmd += ["--run-id", run_id]
    cmd += ["--trigger", trigger]
    logger.info("fetch job starting", extra={"source_id": source_id, "cmd": cmd, "timeout_s": timeout})
    started_at = jobs._utcnow()
    outcome_kw: dict[str, Any] = {
        "trigger": trigger,
        "started_at": started_at,
        "run_id": run_id,
        "attempt": attempts + 1,
        "final_attempt": final_attempt,
    }
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv built from a trusted registry id, no shell
            cmd, cwd=ROOT, timeout=timeout, capture_output=True, text=True, check=False
        )
    except subprocess.TimeoutExpired as exc:
        jobs.fetch_outcome(
            source_id,
            returncode=None,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            timeout_s=timeout,
            **outcome_kw,
        )
        raise TransientConnectorFailure(f"connector run for {source_id} timed out after {timeout}s") from exc
    except Exception as exc:
        # The CLI never ran (no interpreter, no fork): nothing will report on the named run.
        _close_pending(run_id, f"fetch job failed: {exc!r}")
        raise
    try:
        outcome = jobs.fetch_outcome(
            source_id,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            **outcome_kw,
        )
    except Exception as exc:
        # Recording the outcome failed (the database, most likely): try to close the named row so
        # the admin guard does not see it `running`; the job fails and the next tick fetches again.
        _close_pending(run_id, f"recording failed: {exc!r}")
        raise
    status = outcome["status"]
    if status == "ok" and outcome["ts"]:
        _defer_load(source_id, outcome["ts"])
    if status in ("ok", "unchanged", "partial", "refused"):
        logger.info("fetch job finished", extra={"source_id": source_id, "status": status})
        return
    logger.error(
        "fetch job failed",
        extra={
            "source_id": source_id,
            "status": status,
            "returncode": result.returncode,
            "stderr": result.stderr[-4000:],
        },
    )
    if outcome["transient"]:
        raise TransientConnectorFailure(
            f"connector run failed for {source_id} ({status}); will retry with backoff"
        )
    raise ConnectorRunFailed(f"connector run for {source_id} ended {status}; not retried")


def _close_pending(run_id: str | None, error: str) -> None:
    """`jobs.close_pending_run` for the row an admin run-now created, when there is one; never
    raises, so it cannot mask the failure being handled."""
    if run_id is None:
        return
    try:
        jobs.close_pending_run(jobs.build_session_factory(), run_id, error=error)
    except Exception:
        logger.exception("could not close the pending run row", extra={"run_id": run_id})


def _defer_load(source_id: str, ts: str) -> None:
    """Enqueue `load_source` for one run's snapshot token under the per-source execution lock.

    Only for a source whose connector the generic loader loads (`proposal`/`opportunity`,
    `jobs.load_kind_refusal`): a `document` source is fetched and diffed, and no load is queued
    for it, because the generic loader would only refuse its frame (2026-09-30, lane FX2)."""
    refusal = jobs.load_kind_refusal(source_id)
    if refusal:
        logger.info(
            "skipped: no generic load for this source", extra={"source_id": source_id, "reason": refusal}
        )
        return
    try:
        load_source.configure(
            lock=execution_lock_for(source_id), queueing_lock=f"load:{safe_id(source_id)}"
        ).defer(source_id=source_id, ts=ts)
    except procrastinate.exceptions.AlreadyEnqueued:
        logger.info("skipped: previous load still queued or running", extra={"source_id": source_id})


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def due_sources(bucket: str, timestamp: int) -> list[dict[str, Any]]:
    """The sources one tick of `bucket` fetches: `cadence.is_due` at the tick's scheduled time
    (Procrastinate passes it as a Unix timestamp), so the monthly annual tick picks only the
    sources whose run month it is. A malformed `release_month` is logged and that source falls
    back to the default January run rather than stopping the whole tick."""
    month = dt.datetime.fromtimestamp(timestamp, tz=dt.UTC).month
    due = []
    for source in _load_sources():
        try:
            if is_due(source, bucket, month):
                due.append(source)
        except ValueError as exc:
            logger.warning("bad release_month; using the default run month", extra={"error": str(exc)})
            if is_due({**source, "release_month": None}, bucket, month):
                due.append(source)
    return due


def _register_bucket_tick(bucket: str, cron: str) -> None:
    @app.periodic(cron=cron, periodic_id=f"tick:{bucket}")
    @app.task(name=f"tick_{bucket}", queue=SCHEDULER_ONLY_QUEUE)
    def _tick(timestamp: int, _bucket: str = bucket) -> None:
        due = due_sources(_bucket, timestamp)
        logger.info("bucket tick", extra={"bucket": _bucket, "timestamp": timestamp, "due_count": len(due)})
        for source in due:
            source_id = source["id"]
            queue = queue_for_source(source)
            try:
                run_connector.configure(
                    queue=queue,
                    lock=execution_lock_for(source_id),
                    queueing_lock=queueing_lock_for(source_id),
                ).defer(source_id=source_id)
            except procrastinate.exceptions.AlreadyEnqueued:
                logger.info(
                    "skipped: previous fetch still queued or running",
                    extra={"source_id": source_id, "queue": queue},
                )


for _bucket_name, _cron in CRON_BY_BUCKET.items():
    _register_bucket_tick(_bucket_name, _cron)


# Alert cycle and social draft generation (docs/00-PLAN.md Sprint 3 item 4). Unlike the bucket
# ticks above, each of these is a single unit of work per firing (no per-source fan-out), so the
# `SCHEDULER_ONLY_QUEUE` tick's body defers exactly one job rather than N — but the split is the
# same one: the tick (this process, `scheduler`) only ever decides *whether* it's time to run; the
# deferred job (`worker`, whose queues line already includes `alert` and `post_draft`) does the
# work. Both real tasks carry `retry=0` because the next periodic tick is the retry (15 minutes for
# alerts, an hour for post drafts) — a Procrastinate-managed retry would just race it.

ALERT_TICK_TIMEOUT_S = 600  # docs/20 §4.2's 10-minute fetch-job ceiling, reused for consistency
POST_DRAFT_TICK_TIMEOUT_S = 600
# The audit scans every public surface of the store plus a sample of live requests: 5.5 s on the
# 2026-09-26 dev store (10.4k proposals, 17.9k assets, SQLite); 30 minutes leaves two orders of
# magnitude for Postgres round trips and a store that has grown.
VISIBILITY_AUDIT_TIMEOUT_S = 1800


def _run_with_timeout(fn: Callable[[], dict[str, Any]], *, timeout_s: int) -> dict[str, Any]:
    """Enforce a hard wall-clock timeout on an in-process call, the same discipline
    `run_connector` gets for free from `subprocess.run(timeout=...)` above. These two jobs call an
    in-process Python function rather than shelling out, so there is no subprocess for the OS to
    kill on timeout; running the call in its own thread and bounding `Future.result()` is the
    closest equivalent for a plain callable (a `signal.alarm` only works on a process's main
    thread, which a Procrastinate worker does not guarantee). Known limitation, same as any
    thread-based Python timeout: a call that ignores the deadline is not forcibly interrupted, only
    abandoned — the job still fails/raises promptly so the next periodic tick can retry, which is
    the property that matters here (docs/20 §4.2's timeout exists to stop a hung job from wedging a
    worker slot forever; a queueing_lock, not this timeout, is what stops a *second* tick from
    piling on)."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn).result(timeout=timeout_s)
    finally:
        pool.shutdown(wait=False)


@app.task(name="alert_tick", queue="alert", retry=0, queueing_lock="alert_tick")
def alert_tick() -> dict[str, Any]:
    """US-502 AC1: alerts fire within 15 minutes of the triggering change. Body in
    `infra/scheduler/jobs.py` so this module stays a thin Procrastinate registration layer
    (docs/adr/0004 "a thin runner is fine")."""
    return _run_with_timeout(alert_tick_job, timeout_s=ALERT_TICK_TIMEOUT_S)


@app.periodic(cron="*/15 * * * *", periodic_id="tick:alert")
@app.task(name="tick_alert", queue=SCHEDULER_ONLY_QUEUE)
def _tick_alert(timestamp: int) -> None:
    try:
        alert_tick.defer()
    except procrastinate.exceptions.AlreadyEnqueued:
        logger.info("skipped: previous alert_tick still queued or running")


@app.task(name="post_draft_tick", queue="post_draft", retry=0, queueing_lock="post_draft_tick")
def post_draft_tick() -> dict[str, Any]:
    """Social draft generation from the event log. Hourly cadence is plenty for drafts a human
    reviews before anything posts (`docs/32` review queue); body in `infra/scheduler/jobs.py`."""
    return _run_with_timeout(post_draft_tick_job, timeout_s=POST_DRAFT_TICK_TIMEOUT_S)


@app.periodic(cron="7 * * * *", periodic_id="tick:post_draft")  # off the hour: never coincides
@app.task(name="tick_post_draft", queue=SCHEDULER_ONLY_QUEUE)  # with a fetch bucket's top-of-hour
def _tick_post_draft(timestamp: int) -> None:  # jitter offset (cadence.py's CRON_BY_BUCKET)
    try:
        post_draft_tick.defer()
    except procrastinate.exceptions.AlreadyEnqueued:
        logger.info("skipped: previous post_draft_tick still queued or running")


@app.task(name="visibility_audit_tick", queue="audit", retry=0, queueing_lock="visibility_audit_tick")
def visibility_audit_tick() -> dict[str, Any]:
    """The nightly M-11 visibility audit (docs/10 §5 M-11; docs/04 R-4 and S-9; docs/40 §4 row 11):
    recompute every public surface from the store, check each shown row against the publication
    invariants, probe a sample through the real app anonymously, persist one audit event. Body in
    `infra/scheduler/jobs.py`; `retry=0` because tomorrow's tick is the retry and a breach must not
    be retried into silence — the job fails loudly (`VisibilityAuditBreach`) after persisting."""
    return _run_with_timeout(visibility_audit_tick_job, timeout_s=VISIBILITY_AUDIT_TIMEOUT_S)


@app.periodic(cron="52 4 * * *", periodic_id="tick:visibility_audit")  # the hour after the daily
@app.task(name="tick_visibility_audit", queue=SCHEDULER_ONLY_QUEUE)  # fetch bucket (03:07) and its
def _tick_visibility_audit(timestamp: int) -> None:  # load chain; after the 04:37 resolve tick
    try:
        visibility_audit_tick.defer()
    except procrastinate.exceptions.AlreadyEnqueued:
        logger.info("skipped: previous visibility_audit_tick still queued or running")


@app.task(name="freshness_tick", queue="audit", retry=0, queueing_lock="freshness_tick")
def freshness_tick() -> dict[str, Any]:
    """Source freshness (audit 2026-09-30 F2): every implemented source's last success against
    twice its poll allowance (`infra/scheduler/freshness.py`). Fails loudly (`SourcesStale`) while
    any scheduled source is stale or has never run, so a source the scheduler silently stopped
    running is visible within the hour instead of staying `health = ok`. Body in `jobs.py`."""
    return _run_with_timeout(freshness_tick_job, timeout_s=ALERT_TICK_TIMEOUT_S)


@app.periodic(cron="41 * * * *", periodic_id="tick:freshness")  # off every fetch bucket's minute
@app.task(name="tick_freshness", queue=SCHEDULER_ONLY_QUEUE)
def _tick_freshness(timestamp: int) -> None:
    try:
        freshness_tick.defer()
    except procrastinate.exceptions.AlreadyEnqueued:
        logger.info("skipped: previous freshness_tick still queued or running")


# The rest of the loop (module docstring). Queues are ones the compose `worker` service already
# consumes (`normalise`, `resolve`; infra/scheduler/worker.py's docstring), so no compose change.


@app.task(name="load_source", queue="normalise", retry=0)
def load_source(source_id: str, ts: str) -> dict[str, Any]:
    """Load one run's normalised parquet + events into the store (`services.ingest.loader`),
    then ask for a store-wide resolve pass. Deferred by `run_connector` under the same per-source
    `lock`, so it never reads a parquet the next fetch is rewriting. `retry=0`: the next
    successful fetch of the source re-enqueues it, and the loader is idempotent."""
    report = _run_with_timeout(lambda: jobs.load_source_job(source_id, ts), timeout_s=LOAD_TIMEOUT_S)
    try:
        resolve_tick.defer()
    except procrastinate.exceptions.AlreadyEnqueued:
        logger.info("skipped: resolve_tick already queued", extra={"source_id": source_id})
    return report


@app.task(name="release_held_run", queue="normalise", retry=0)
def release_held_run(source_id: str, run_id: str, released_by: str) -> dict[str, Any]:
    """An operator released a data-quality hold (`POST /admin/v1/source-runs/{run_id}/release`,
    which records who, when and why before deferring this): promote the held frame to
    `normalized/`, mark the run `ok`, then enqueue the `load_source` the hold withheld. Deferred
    under the per-source execution lock, so it never runs beside a fetch or load of the same
    source. `retry=0`: a refusal (`HoldReleaseRefused`) is not fixed by retrying, and a crash is
    recoverable by releasing again (every step is idempotent)."""
    report = _run_with_timeout(
        lambda: jobs.release_held_job(source_id, run_id, released_by), timeout_s=LOAD_TIMEOUT_S
    )
    if report.get("ts"):
        _defer_load(source_id, str(report["ts"]))
    return report


def release_queueing_lock_for(run_id: str) -> str:
    """One queued release per run: a double-submitted release form cannot enqueue it twice."""
    return f"release:{safe_id(run_id)}"


@app.task(name="resolve_tick", queue="resolve", retry=0, queueing_lock="resolve_tick", lock="resolve")
def resolve_tick() -> dict[str, Any]:
    """Store-wide resolution (docs/20 §3.5) through the entry points `services/resolve/report.py`
    already uses; body in `infra/scheduler/jobs.py`. Chains to `enrich_tick`."""
    report = _run_with_timeout(jobs.resolve_tick_job, timeout_s=RESOLVE_TIMEOUT_S)
    try:
        enrich_tick.defer()
    except procrastinate.exceptions.AlreadyEnqueued:
        logger.info("skipped: enrich_tick already queued")
    return report


@app.task(name="enrich_tick", queue="resolve", retry=0, queueing_lock="enrich_tick", lock="resolve")
def enrich_tick() -> dict[str, Any]:
    """Enrichment (docs/20 §3.6): the geocode backfill today; body in `infra/scheduler/jobs.py`."""
    return _run_with_timeout(jobs.enrich_tick_job, timeout_s=ENRICH_TIMEOUT_S)


@app.periodic(cron="37 4 * * *", periodic_id="tick:resolve")  # after the daily fetch bucket (03:07)
@app.task(name="tick_resolve", queue=SCHEDULER_ONLY_QUEUE)
def _tick_resolve(timestamp: int) -> None:
    try:
        resolve_tick.defer()
    except procrastinate.exceptions.AlreadyEnqueued:
        logger.info("skipped: previous resolve_tick still queued or running")


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(message)s")
    logger.info(
        "scheduler starting",
        extra={"buckets": list(CRON_BY_BUCKET), "source_count": len(_load_sources())},
    )
    # Only the periodic side-thread does anything useful here; SCHEDULER_ONLY_QUEUE has no
    # `run_connector` job ever enqueued to it, so this process defers fetch jobs and never runs one.
    app.run_worker(queues=[SCHEDULER_ONLY_QUEUE], concurrency=1, wait=True)


if __name__ == "__main__":
    main()
