"""Queue, once, the jobs a store that starts empty would otherwise wait days for.

The bucket ticks (`infra/scheduler/app.py`) fetch a source only when its bucket next fires: the 1st
of the month for EIA-860M, Monday 04:13 UTC for the weekly queues. A load is queued only by a fetch
that wrote a new snapshot. A new server therefore serves an empty store for up to a month, and a
data root fetched by hand (`python -m pipeline.connectors run --all`) is never loaded at all,
because a hand run records no `source_run` row and queues nothing.

    python -m infra.scheduler.bootstrap fetch             # every implemented, ungated source, now
    python -m infra.scheduler.bootstrap first-fetch       # only those that never succeeded (each deploy)
    python -m infra.scheduler.bootstrap load              # each source's latest stored promoted run
    python -m infra.scheduler.bootstrap context           # the context layers on disk, now
    python -m infra.scheduler.bootstrap context --build   # rebuild them first (`context_build`)
    python -m infra.scheduler.bootstrap sources [--json]  # read-only: each source's last attempt
    python -m infra.scheduler.bootstrap preflight [--json] # read-only GO / NO-GO before inviting anyone

`first-fetch` (2026-10-24, docs/28 lane F5) is what every deploy runs (`infra/ci/beta.sh deploy`). A
source added to a running server waited for its bucket's next tick, up to a week or a month, and
CAISO and NYISO still read "never" a day after they shipped. It queues the `fetch` job for each
implemented, ungated, scheduled source whose `source` row has no `last_success_at` (or that has no
row), unless an operator paused it, its last attempt was `blocked` (a refusal is retried on the
schedule only, docs/20 §12), or a `run_connector` job for it is queued or running. Same job, queue
and locks as `fetch`, so it is safe to repeat: a second deploy reports `already_queued` or `running`.

`sources` reads and prints, per implemented source, the last attempt (time, outcome, whether it
succeeded), the last success, any queued job and a short error: one line, credentials, keys and
query strings removed (`short_error`). Then the implemented sources that never succeeded, and the
site finder's state, with the step that would compute it. A failed fetch records `source.last_error`,
which until now only the admin pages showed; deploy-beta's `status` action prints this.

Both queue the jobs the ticks queue (`run_connector`, `load_source`) under the same per-source
`lock` and `queueing_lock`, so they are safe while the scheduler and workers run and safe to
repeat: a source whose job is still queued is reported `already_queued`. `fetch` is the path for a
new server. Each run records its `source_run` row and, when it writes a new snapshot, queues its own
load, which chains into the store-wide resolve and enrich passes. `load` is the path for a data root
that already holds runs. The loader starts from the latest promoted run when the source has never
been loaded, and replays the promoted runs after its last loaded one otherwise
(`services.ingest.loader.runs_to_load`). `context` queues the monthly tick's `context_load` (or,
with `--build`, its `context_build`, which chains into the load), for a data root shipped with its
`normalized/context/` files or a refresh that should not wait for the 3rd of the month.

The selection and the deferral are separate functions so the tests need no database; `main` wires
them to the Procrastinate app, which needs `DATABASE_URL` (`infra/scheduler/app.py`).

`preflight` queues nothing: it is the check the operator runs after the seed and before inviting
testers (docs/64 §3), and lives in `infra/scheduler/preflight.py`, which documents each check. It
needs no Procrastinate app, so it is dispatched before one is imported; so is `sources`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("infra.scheduler.bootstrap")

#: `source_run` outcomes that count as a success (`jobs._update_health`): they set `last_success_at`.
SUCCESS_STATUSES = frozenset({"ok", "unchanged"})
#: The longest error `sources` prints for one source: room for a refusal's edge headers
#: (`pipeline/connectors/iso_queue.py::describe_response`) after the URL.
SHORT_ERROR_CHARS = 360


def fetchable_sources(
    scheduled: Iterable[Mapping[str, Any]], registry_status: Iterable[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """The scheduled sources (`data/sources.yaml` entries with a `cadence`) that a fetch would run:
    registry state `implemented`, the same rule as `pipeline.connectors run --all`. Gated,
    excluded and unimplemented sources are left out, where a tick queues them and the runner
    refuses them before any I/O."""
    implemented = {str(row["id"]) for row in registry_status if row.get("state") == "implemented"}
    return [source for source in scheduled if str(source.get("id")) in implemented]


def latest_promoted_ts(runs: Iterable[Mapping[str, Any]]) -> str | None:
    """The snapshot token of the newest run that promoted a normalised output (`status == "ok"`):
    the basename of `outputs.normalized`, as `services.ingest.loader._run_ts` reads it. `None` when
    no run did, so there is nothing to load."""
    latest: str | None = None
    for record in runs:
        if record.get("status") != "ok":
            continue
        location = (record.get("outputs") or {}).get("normalized")
        if not location:
            continue
        name = pathlib.PurePosixPath(str(location)).name
        if not name.endswith(".parquet"):
            continue
        ts = name[: -len(".parquet")]
        if latest is None or ts > latest:
            latest = ts
    return latest


def queue_fetches(
    sources: Iterable[Mapping[str, Any]], defer_fetch: Callable[[Mapping[str, Any]], bool]
) -> list[dict[str, str]]:
    """Queue one fetch per source. `defer_fetch` returns False when the source's fetch is already
    queued or running."""
    report = []
    for source in sources:
        queued = defer_fetch(source)
        report.append(
            {
                "source_id": str(source["id"]),
                "action": "fetch",
                "result": "queued" if queued else "already_queued",
            }
        )
    return report


def queue_loads(
    source_ids: Iterable[str],
    runs_for: Callable[[str], Iterable[Mapping[str, Any]]],
    load_refusal: Callable[[str], str | None],
    defer_load: Callable[[str, str], bool],
) -> list[dict[str, str]]:
    """Queue one load per source that has a promoted run, at that run's token. A source the generic
    loader does not load (`jobs.load_kind_refusal`: a `document` or an asset-only source, which the
    fetch job never queues a load for either) is reported `no_generic_load`. `defer_load` returns
    False when the source's load is already queued or running."""
    report = []
    for source_id in source_ids:
        row = {"source_id": source_id, "action": "load"}
        ts = latest_promoted_ts(runs_for(source_id))
        if ts is None:
            report.append({**row, "result": "no_promoted_run"})
            continue
        refusal = load_refusal(source_id)
        if refusal:
            report.append({**row, "result": "no_generic_load", "reason": refusal})
            continue
        queued = defer_load(source_id, ts)
        report.append({**row, "result": "queued" if queued else "already_queued", "ts": ts})
    return report


def queue_context(task_name: str, defer: Callable[[], Any]) -> dict[str, str]:
    """Queue `context_load` or `context_build`. Both carry a `queueing_lock`: a second call while
    one is still waiting is reported `already_queued`. One that has started no longer holds it, so
    a second call then queues a run that waits on the job's `lock` and follows it."""
    import procrastinate

    row = {"action": task_name}
    try:
        defer()
    except procrastinate.exceptions.AlreadyEnqueued:
        return {**row, "result": "already_queued"}
    return {**row, "result": "queued"}


# ----------------------------------------------------------------------------- source state
@dataclass(frozen=True)
class SourceState:
    """What the store records about one source's fetches: its `source` row and newest `source_run`."""

    source_id: str
    has_row: bool = False
    paused: bool = False
    health: str | None = None
    last_success_at: dt.datetime | None = None
    last_error: str | None = None
    last_error_at: dt.datetime | None = None
    consecutive_failures: int = 0
    #: The newest `source_run` row: status, started_at, finished_at, error, error_class, dq, ...
    last_run: Mapping[str, Any] | None = None

    @property
    def succeeded_ever(self) -> bool:
        return self.last_success_at is not None


def read_source_states(session: Any, source_ids: Iterable[str]) -> dict[str, SourceState]:
    """One `SourceState` per id, from the `source` row and its newest `source_run` (by start)."""
    from sqlalchemy import select

    from services.db.models import Source, SourceRun

    out: dict[str, SourceState] = {}
    for source_id in source_ids:
        source = session.get(Source, source_id)
        run = session.scalars(
            select(SourceRun)
            .where(SourceRun.source_id == source_id)
            .order_by(SourceRun.started_at.desc())
            .limit(1)
        ).first()
        last_run = None
        if run is not None:
            last_run = {
                "status": run.status,
                "trigger": run.trigger,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "http_status": run.http_status,
                "error": run.error,
                "error_class": run.error_class,
                "dq_status": run.dq_status,
                "dq": run.dq,
                "attempt": run.attempt,
                "dead_lettered": run.dead_lettered,
            }
        if source is None:
            out[source_id] = SourceState(source_id, last_run=last_run)
            continue
        out[source_id] = SourceState(
            source_id,
            has_row=True,
            paused=bool(source.paused) or source.health == "paused",
            health=source.health,
            last_success_at=source.last_success_at,
            last_error=source.last_error,
            last_error_at=source.last_error_at,
            consecutive_failures=int(source.consecutive_failures or 0),
            last_run=last_run,
        )
    return out


def fetch_jobs_by_source(list_jobs: Callable[..., Iterable[Any]]) -> dict[str, str]:
    """`source_id -> "doing" | "todo"` for every `run_connector` job queued or running, from
    Procrastinate's own job listing (`app.job_manager.list_jobs`); `doing` wins over `todo`."""
    found: dict[str, str] = {}
    for status in ("todo", "doing"):
        for job in list_jobs(task="run_connector", status=status):
            source_id = str((getattr(job, "task_kwargs", None) or {}).get("source_id") or "")
            if source_id:
                found[source_id] = status
    return found


def first_fetch_plan(
    sources: Iterable[Mapping[str, Any]], states: Mapping[str, SourceState], jobs: Mapping[str, str]
) -> list[dict[str, str]]:
    """What `first-fetch` does for each source that never succeeded: `queue`, or why not
    (`paused`, `blocked`, `already_queued`, `running`). A source that has succeeded is left out."""
    plan = []
    for source in sources:
        source_id = str(source["id"])
        state = states.get(source_id) or SourceState(source_id)
        if state.succeeded_ever:
            continue
        row = {"source_id": source_id, "action": "first_fetch"}
        last = (state.last_run or {}).get("status")
        if state.paused:
            plan.append({**row, "result": "paused"})
        elif jobs.get(source_id) == "doing":
            plan.append({**row, "result": "running"})
        elif jobs.get(source_id) == "todo":
            plan.append({**row, "result": "already_queued"})
        elif last == "blocked":
            plan.append({**row, "result": "blocked", "reason": "a refusal is retried on the schedule only"})
        else:
            plan.append({**row, "result": "queue"})
    return plan


def queue_first_fetches(
    plan: Iterable[Mapping[str, str]],
    sources: Iterable[Mapping[str, Any]],
    defer_fetch: Callable[[Mapping[str, Any]], bool],
) -> list[dict[str, str]]:
    """Defer the plan's `queue` rows with `defer_fetch` (as `queue_fetches` does); the rest as planned."""
    by_id = {str(s["id"]): s for s in sources}
    report = []
    for row in plan:
        if row["result"] != "queue":
            report.append(dict(row))
            continue
        queued = defer_fetch(by_id[row["source_id"]])
        report.append({**row, "result": "queued" if queued else "already_queued"})
    return report


# --------------------------------------------------------------------------------- redaction
#: Environment variables whose values never appear in what `sources` prints.
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|DSN|HASH|CREDENTIAL|DATABASE_URL|AUTH|COOKIE")
#: `name=value`, `name: value` and `"name": "value"` where the name is, or ends in, a secret word:
#: `api_key`, `client_secret`, `access_token`, `X-Api-Key`, `password`, `sig`, `Authorization`.
_SECRET_FIELD = re.compile(
    r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_-]*(?:api[_-]?key|access[_-]?key|subscription[_-]?key|secret|"
    r"token|password|passwd|signature|sig|authorization))([\"']?\s*[=:]\s*)([\"']?)[^\s&\"',;)}]+"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_USERINFO = re.compile(r"://[^/@\s'\"]+@")
_WS = re.compile(r"\s+")


def short_error(
    text: str | None, env: Mapping[str, str] | None = None, limit: int = SHORT_ERROR_CHARS
) -> str | None:
    """One line of `text` that is safe to print: no query string, no credentials in a URL, no
    `key=value` that names a secret, no bearer token, and none of this process's secret settings
    (`_SECRET_ENV` names, values of 6 characters or more); whitespace collapsed, cut to `limit`."""
    if not text:
        return None
    from pipeline.connectors.http import strip_queries

    env = os.environ if env is None else env
    out = str(text).replace("\\'", "'").replace('\\"', '"').replace("\\n", " ")
    out = strip_queries(out)
    out = _USERINFO.sub("://***@", out)
    out = _BEARER.sub(lambda m: f"{m.group(1)} ***", out)  # before the field rule, which would take "Bearer"
    out = _SECRET_FIELD.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}***", out)
    for name, value in env.items():
        value = (value or "").strip()
        if len(value) >= 6 and _SECRET_ENV.search(name.upper()) and value in out:
            out = out.replace(value, "***")
    out = _WS.sub(" ", out).strip()
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _hold_reasons(dq: Any) -> list[str]:
    checks = (dq or {}).get("checks") if isinstance(dq, Mapping) else None
    return [str(c.get("detail") or c.get("check")) for c in checks or [] if c.get("level") == "hold"]


def attempt_outcome(state: SourceState, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The last attempt in words: `result` (the run's status, `never_attempted` without one), `what`
    (a short reading of it) and `error` (`short_error` of what the run or the `source` row says)."""
    run = state.last_run or {}
    status = str(run.get("status") or "")
    error_text = run.get("error")  # never an older run's error (`source.last_error`) for this one
    if not status:
        if state.last_error:
            return {"result": "failed", "what": "failed", "error": short_error(state.last_error, env)}
        return {"result": "never_attempted", "what": "never attempted on this server", "error": None}
    if status in SUCCESS_STATUSES:
        return {"result": status, "what": "succeeded", "error": None}
    if status == "running":
        return {"result": status, "what": "running now", "error": None}
    if status == "partial":
        reasons = _hold_reasons(run.get("dq"))
        detail = "; ".join(reasons) or "a data-quality gate"
        return {"result": status, "what": "held by data quality", "error": short_error(detail, env)}
    error_class = str(run.get("error_class") or "")
    text = str(error_text or "")
    if status == "blocked":
        what = "refused by the host"
    elif (
        error_class == "HttpTlsUnverified" or "CERTIFICATE_VERIFY_FAILED" in text or "did not verify" in text
    ):
        what = "TLS certificate did not verify"
        # OpenSSL's reason first: an older run's error is a long nested repr cut at 500 characters.
        from pipeline.connectors.http import tls_verify_failure

        reason = tls_verify_failure(Exception(text))
        if reason and "did not verify" not in text:
            text = f"{reason}; {text}"
    elif error_class in ("TimeoutExpired", "ReadTimeout", "ConnectTimeout", "Timeout") or "timed out" in text:
        what = "timed out"
    elif error_class == "ParseError":
        what = "the file's layout changed"
    elif error_class == "RunAbandoned":
        what = "abandoned (the job ended before the run reported)"
    else:
        what = "failed"
    return {"result": status, "what": what, "error": short_error(text or error_class or None, env)}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
        return aware.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%MZ")
    return str(value)


def source_status_rows(
    implemented: Iterable[Mapping[str, Any]],
    scheduled_ids: set[str],
    states: Mapping[str, SourceState],
    jobs: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One row per implemented source for `sources`: never-succeeded first, then by id."""
    rows = []
    for entry in implemented:
        source_id = str(entry["id"])
        state = states.get(source_id) or SourceState(source_id)
        run = state.last_run or {}
        outcome = attempt_outcome(state, env)
        rows.append(
            {
                "source_id": source_id,
                "scheduled": source_id in scheduled_ids,
                "last_attempt_at": _iso(
                    run.get("finished_at") or run.get("started_at") or state.last_error_at
                ),
                "trigger": run.get("trigger"),
                **outcome,
                "succeeded": outcome["result"] in SUCCESS_STATUSES,
                "last_success_at": _iso(state.last_success_at),
                "health": state.health,
                "consecutive_failures": state.consecutive_failures,
                "job": jobs.get(source_id),
                "paused": state.paused,
            }
        )
    rows.sort(key=lambda r: (r["last_success_at"] is not None, r["source_id"]))
    return rows


# ------------------------------------------------------------------------------- site finder
GRID_LAYER_PATH = "grid/osm-power/latest.json"


def read_site_finder(session: Any, data_root: pathlib.Path, env: Mapping[str, str]) -> dict[str, Any]:
    """Whether the site finder has a run to serve, why not, and the step that would compute it.

    It is computed only by `site_finder_refresh`, which `context_load` chains, which `context_build`
    chains: the monthly tick (the 4th, 02:43 UTC) or deploy-beta's `seed`. Its one line source,
    `us.osm.power`, reaches the server only through `context_build`'s pull of the published grid
    layer (`https://tiles.<DOMAIN>/grid/osm-power/latest.json`, built by grid-layers.yml)."""
    from sqlalchemy import func, select

    from services.db.models import Asset, SiteFinderRun

    domain = (env.get("DOMAIN") or env.get("BANKABLE_DOMAIN") or "<DOMAIN>").strip()
    pointer = f"https://tiles.{domain}/{GRID_LAYER_PATH}"
    latest = session.scalars(select(SiteFinderRun).order_by(SiteFinderRun.started_at.desc()).limit(1)).first()
    served = session.scalars(
        select(SiteFinderRun)
        .where(SiteFinderRun.status == "ok")
        .order_by(SiteFinderRun.finished_at.desc(), SiteFinderRun.started_at.desc())
        .limit(1)
    ).first()
    lines = int(
        session.scalar(
            select(func.count(Asset.id)).where(
                Asset.asset_type == "transmission_line", Asset.source_id == "us.osm.power"
            )
        )
        or 0
    )
    manifest = data_root / "normalized" / "context" / "us.osm.power.manifest.json"
    out: dict[str, Any] = {
        "computed": served is not None and int(served.segments or 0) > 0,
        "segments": int(served.segments or 0) if served is not None else 0,
        "served_run_finished_at": _iso(served.finished_at) if served is not None else None,
        "latest_run": None
        if latest is None
        else {
            "status": latest.status,
            "started_at": _iso(latest.started_at),
            "finished_at": _iso(latest.finished_at),
            "segments": int(latest.segments or 0),
            "error": short_error(latest.error, env),
        },
        "osm_lines_loaded": lines,
        "grid_layer_on_disk": manifest.is_file(),
        "grid_layer_pointer": pointer,
    }
    if out["computed"]:
        out["next"] = None
    elif not out["grid_layer_on_disk"] and lines == 0:
        out["next"] = (
            f"no OpenStreetMap grid layer on this server: context_build pulls it from {pointer}, which "
            "grid-layers.yml publishes. Owner: run grid-layers.yml (docs/66, Site finder go-live, step "
            "(b)), then deploy-beta with action seed (step (d))"
        )
    elif latest is not None and latest.status == "running":
        out["next"] = f"a run is in progress since {_iso(latest.started_at)}"
    elif latest is not None and latest.status == "failed":
        out["next"] = "the last run failed (error above): read the worker log for site_finder_refresh"
    elif latest is not None and latest.status == "ok":
        out["next"] = "the last run found no 69 kV-or-higher line in GRID_LAYERS_REGIONS"
    else:
        out["next"] = (
            f"{lines:,} OpenStreetMap lines are loaded but site_finder_refresh has not run since: "
            "deploy-beta with action seed queues context_build, then context_load, then the finder"
        )
    return out


# ---------------------------------------------------------------------------- `sources` action
@contextmanager
def read_only(factory: Any) -> Iterator[Any]:
    """A session whose transaction is rolled back; `READ ONLY` on Postgres."""
    from sqlalchemy import text

    session = factory()
    try:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        yield session
    finally:
        session.rollback()
        session.close()


@dataclass
class StatusInputs:
    """What `sources_report` reads; every field can be stood in by a test."""

    registry_status: Sequence[Mapping[str, Any]]
    scheduled: Sequence[Mapping[str, Any]]
    session_factory: Any
    list_jobs: Callable[..., Iterable[Any]] | None = None
    data_root: pathlib.Path = field(default_factory=lambda: pathlib.Path("data"))
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    now: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


def sources_report(inputs: StatusInputs) -> dict[str, Any]:
    implemented = [r for r in inputs.registry_status if r.get("state") == "implemented"]
    scheduled_ids = {str(s.get("id")) for s in inputs.scheduled}
    jobs: dict[str, str] = {}
    jobs_error = None
    if inputs.list_jobs is not None:
        try:
            jobs = fetch_jobs_by_source(inputs.list_jobs)
        except Exception as exc:  # the queue unreadable: say so, keep the rest
            jobs_error = short_error(f"{type(exc).__name__}: {exc}", inputs.env)
    with read_only(inputs.session_factory) as session:
        states = read_source_states(session, [str(r["id"]) for r in implemented])
        try:
            with session.begin_nested():
                finder: dict[str, Any] | None = read_site_finder(session, inputs.data_root, inputs.env)
        except Exception as exc:  # a store behind migration 0039
            finder = {"computed": False, "error": short_error(f"{type(exc).__name__}: {exc}", inputs.env)}
    rows = source_status_rows(implemented, scheduled_ids, states, jobs, inputs.env)
    never = [r["source_id"] for r in rows if r["last_success_at"] is None]
    return {
        "generated_at": _iso(inputs.now),
        "implemented": len(rows),
        "succeeded_once": len(rows) - len(never),
        "never_succeeded": never,
        "queue_unreadable": jobs_error,
        "sources": rows,
        "site_finder": finder,
    }


def render_sources(report: Mapping[str, Any]) -> str:
    rows = report["sources"]
    width = max((len(r["source_id"]) for r in rows), default=10)
    lines = [
        f"Sources at {report['generated_at']}: {report['implemented']} implemented, "
        f"{report['succeeded_once']} succeeded at least once, {len(report['never_succeeded'])} never"
    ]
    if report.get("queue_unreadable"):
        lines.append(f"(the job queue could not be read: {report['queue_unreadable']})")
    head = (
        f"{'source':<{width}}  {'last attempt':<17}  {'result':<15}  {'last success':<17}  {'job':<5}  what"
    )
    lines += [head, "-" * len(head)]
    for r in rows:
        what = r["what"] + (f": {r['error']}" if r.get("error") else "")
        if not r["scheduled"]:
            what += " (no cadence: never scheduled)"
        if r["paused"]:
            what += " (paused)"
        lines.append(
            f"{r['source_id']:<{width}}  {r['last_attempt_at'] or '-':<17}  {r['result']:<15}  "
            f"{r['last_success_at'] or 'never':<17}  {r['job'] or '-':<5}  {what}"
        )
    never = report["never_succeeded"]
    lines.append(f"Implemented sources that never succeeded ({len(never)}): {', '.join(never) or 'none'}")
    finder = report.get("site_finder") or {}
    if finder.get("computed"):
        when = finder["served_run_finished_at"]
        lines.append(f"Site finder: computed, {finder['segments']:,} segments (run of {when})")
    elif finder.get("error"):
        lines.append(f"Site finder: not readable here ({finder['error']})")
    else:
        latest = finder.get("latest_run")
        last = (
            f"last run {latest['status']} {latest['finished_at'] or latest['started_at']}"
            + (f": {latest['error']}" if latest.get("error") else "")
            if latest
            else "no run yet"
        )
        lines.append(
            f"Site finder: not computed ({last}; {finder.get('osm_lines_loaded', 0):,} OpenStreetMap "
            f"lines loaded; grid layer on disk: {'yes' if finder.get('grid_layer_on_disk') else 'no'}). "
            f"Next: {finder.get('next')}"
        )
    return "\n".join(lines) + "\n"


def _sources_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m infra.scheduler.bootstrap sources",
        description="Read-only: each implemented source's last attempt, and the site finder's state.",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    from infra.scheduler import jobs
    from pipeline.connectors.registry import Registry

    factory = jobs.build_session_factory()
    report = sources_report(
        StatusInputs(
            registry_status=Registry().status(),
            scheduled=scheduled_sources(),
            session_factory=factory,
            list_jobs=sql_job_lister(factory),
            data_root=jobs.connector_data_root(),
        )
    )
    sys.stdout.write(
        json.dumps(report, indent=1, default=str) + "\n" if args.json else render_sources(report)
    )
    return 0


SOURCES_YAML = pathlib.Path(__file__).resolve().parents[2] / "data" / "sources.yaml"


def scheduled_sources(path: pathlib.Path = SOURCES_YAML) -> list[dict[str, Any]]:
    """The `data/sources.yaml` entries with a `cadence`, as `app._load_sources` reads them, without
    importing the Procrastinate app (`sources` needs none)."""
    import yaml

    doc = yaml.safe_load(path.read_text())
    return [s for s in doc.get("sources", []) if "cadence" in s]


def sql_job_lister(factory: Any) -> Callable[..., list[Any]]:
    """`list_jobs(task=..., status=...)` over `procrastinate_jobs` in a read-only session, shaped
    like Procrastinate's own listing (`.task_kwargs`). Empty where there is no queue schema."""
    from types import SimpleNamespace

    from sqlalchemy import text

    def list_jobs(*, task: str, status: str) -> list[Any]:
        with read_only(factory) as session:
            if session.get_bind().dialect.name != "postgresql":
                return []
            rows = session.execute(
                text("SELECT args FROM procrastinate_jobs WHERE task_name = :task AND status = :status"),
                {"task": task, "status": status},
            )
            return [SimpleNamespace(task_kwargs=dict(row[0] or {})) for row in rows]

    return list_jobs


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["preflight"]:
        from infra.scheduler import preflight

        return preflight.main(args_list[1:])
    if args_list[:1] == ["sources"]:
        return _sources_main(args_list[1:])
    parser = argparse.ArgumentParser(
        prog="python -m infra.scheduler.bootstrap", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "action",
        choices=("fetch", "first-fetch", "load", "context", "sources", "preflight"),
        help="sources and preflight take their own flags: `sources --help`, `preflight --help`",
    )
    parser.add_argument("--build", action="store_true", help="context: rebuild the files before loading them")
    args = parser.parse_args(args_list)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Deferred: importing the app opens no connection but needs DATABASE_URL.
    import procrastinate

    from infra.scheduler import app as scheduler
    from infra.scheduler import jobs
    from infra.scheduler.cadence import (
        execution_lock_for,
        queue_for_source,
        queueing_lock_for,
        safe_id,
    )
    from pipeline.connectors import store as store_module
    from pipeline.connectors.registry import Registry

    sources = fetchable_sources(scheduler._load_sources(), Registry().status())

    def defer_fetch(source: Mapping[str, Any]) -> bool:
        source_id = str(source["id"])
        try:
            scheduler.run_connector.configure(
                queue=queue_for_source(dict(source)),
                lock=execution_lock_for(source_id),
                queueing_lock=queueing_lock_for(source_id),
            ).defer(source_id=source_id)
        except procrastinate.exceptions.AlreadyEnqueued:
            return False
        return True

    def defer_load(source_id: str, ts: str) -> bool:
        # The lock pair `run_connector` queues its own load under (`app._defer_load`).
        try:
            scheduler.load_source.configure(
                lock=execution_lock_for(source_id), queueing_lock=f"load:{safe_id(source_id)}"
            ).defer(source_id=source_id, ts=ts)
        except procrastinate.exceptions.AlreadyEnqueued:
            return False
        return True

    extra: dict[str, int] = {}
    with scheduler.app.open():
        if args.action == "context":
            task = scheduler.context_build if args.build else scheduler.context_load
            report = [queue_context(task.name, task.defer)]
        elif args.action == "fetch":
            report = queue_fetches(sources, defer_fetch)
        elif args.action == "first-fetch":
            ids = [str(s["id"]) for s in sources]
            with read_only(jobs.build_session_factory()) as session:
                states = read_source_states(session, ids)
            plan = first_fetch_plan(
                sources, states, fetch_jobs_by_source(scheduler.app.job_manager.list_jobs)
            )
            report = queue_first_fetches(plan, sources, defer_fetch)
            extra["succeeded_before"] = sum(1 for sid in ids if states[sid].succeeded_ever)
        else:
            # The root and backend the fetch wrote through (`INFRAQUE_DATA_DIR`, `SNAPSHOT_STORE`).
            store = store_module.open_store(jobs.connector_data_root())
            ids = [str(s["id"]) for s in sources]
            report = queue_loads(ids, store.runs, jobs.load_kind_refusal, defer_load)
    counts: dict[str, int] = {}
    for row in report:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
        sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.write(json.dumps({"action": args.action, "sources": len(report), **counts, **extra}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
