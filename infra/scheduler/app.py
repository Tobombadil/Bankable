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

from infra.scheduler.cadence import CRON_BY_BUCKET, bucket_for_cadence, queue_for_source, queueing_lock_for
from infra.scheduler.jobs import alert_tick_job, post_draft_tick_job

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


@app.task(
    queue="fetch", retry=5, pass_context=True
)  # docs/20 §4.2 "retries with exponential backoff... five failures -> dead-letter"
def run_connector(context: procrastinate.JobContext, source_id: str) -> None:
    """Run one connector via the same CLI a human uses (`pipeline/README.md`).

    A single `fetch` job may not run more than 10 minutes for a plain source, 5 for a browser one
    (docs/20 §4.2); the timeout is enforced here rather than trusted to the connector itself, so a
    hung request cannot wedge a worker slot forever. `context.job.queue` reflects the queue this
    particular job was actually deferred to (`fetch` or `fetch_browser`, overridden per-source at
    defer time in `_register_bucket_tick` below), not the task's `fetch` decorator default.
    """
    timeout = 300 if context.job.queue == "fetch_browser" else 600
    cmd = [sys.executable, "-m", "pipeline.connectors", "run", source_id]
    logger.info("fetch job starting", extra={"source_id": source_id, "cmd": cmd, "timeout_s": timeout})
    result = subprocess.run(  # noqa: S603 -- fixed argv built from a trusted registry id, no shell
        cmd, cwd=ROOT, timeout=timeout, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        logger.error(
            "fetch job failed",
            extra={"source_id": source_id, "returncode": result.returncode, "stderr": result.stderr[-4000:]},
        )
        raise RuntimeError(f"connector run failed for {source_id} (exit {result.returncode})")
    logger.info("fetch job finished", extra={"source_id": source_id})


def _register_bucket_tick(bucket: str, cron: str) -> None:
    @app.periodic(cron=cron, periodic_id=f"tick:{bucket}")
    @app.task(name=f"tick_{bucket}", queue=SCHEDULER_ONLY_QUEUE)
    def _tick(timestamp: int, _bucket: str = bucket) -> None:
        due = [s for s in _load_sources() if bucket_for_cadence(str(s["cadence"])).bucket == _bucket]
        logger.info("bucket tick", extra={"bucket": _bucket, "timestamp": timestamp, "due_count": len(due)})
        for source in due:
            source_id = source["id"]
            queue = queue_for_source(source)
            try:
                run_connector.configure(
                    queue=queue,
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
