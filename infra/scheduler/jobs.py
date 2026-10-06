"""Job bodies for the `alert_tick`, `post_draft_tick` and `visibility_audit_tick` periodic jobs
(docs/00-PLAN.md Sprint 3 item 4; docs/04 R-4; ADR 0004).

Importable without Postgres and without `services.alerts`/`services.social` existing yet: the two
worker modules are lazily imported inside each `*_job` function, and `build_session_factory` only
touches `services.db.session` (which itself falls back to an in-memory SQLite engine when
`DATABASE_URL` is unset — see its docstring) when actually called, never at import time. This lets
`infra/scheduler/app.py` import this module unconditionally, the same way it already imports
`infra/scheduler/cadence.py`.

Each `*_job` function is the exact body registered on the corresponding Procrastinate task in
`infra/scheduler/app.py`; kept in this module, separate from `app.py`, only so the module stays
importable before a live `DATABASE_URL`/`procrastinate.App` exists (this file imports neither).
"""

from __future__ import annotations

import datetime as dt
import functools
import importlib
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("infra.scheduler.jobs")

ROOT = Path(__file__).resolve().parents[2]


def _load_fn(module_path: str, attr: str) -> Callable[..., Any]:
    """Import `module_path` and return its `attr`, both fully dynamic to mypy (a plain `from X
    import Y` makes mypy resolve `X` at check time, which fails today for `services.social.worker`
    — a concurrent agent is still writing it — and would then flip to an "unused ignore" error
    under `--strict` the moment it lands if suppressed with a `type: ignore` instead). This way the
    module and function are only ever looked up at call time, which is also the actual "lazy
    import" requirement: neither worker module is imported until a tick runs.
    """
    module = importlib.import_module(module_path)
    return getattr(module, attr)  # type: ignore[no-any-return]


@functools.lru_cache(maxsize=1)
def build_session_factory() -> Any:
    """Build (and cache) a SQLAlchemy sessionmaker bound to `DATABASE_URL`.

    `services.db.session.get_engine` already implements the "`DATABASE_URL` from the environment,
    fall back to in-memory SQLite" convention (CLAUDE.md: config from environment only) — this
    function just reuses it rather than re-deriving it, and caches the result so every tick shares
    one engine/connection pool instead of opening a new one per job.
    """
    from services.db.session import get_engine, get_sessionmaker

    engine = get_engine(os.environ.get("DATABASE_URL"))
    return get_sessionmaker(engine)


def _report_to_dict(report: Any) -> dict[str, Any]:
    """Normalise a worker report (a frozen dataclass per the contract, a plain dict, or a test
    double shaped like one) into a plain, JSON-safe dict for Procrastinate's job result column."""
    if isinstance(report, dict):
        data = dict(report)
    elif is_dataclass(report) and not isinstance(report, type):
        data = asdict(report)
    else:
        data = dict(vars(report))
    return {key: (list(value) if isinstance(value, tuple) else value) for key, value in data.items()}


def _log_report(job_name: str, data: dict[str, Any]) -> None:
    """Log one structured line: every count field, plus `error_count` — never the `errors` tuple's
    text, which can embed a customer's saved-search query or a post's draft copy (CLAUDE.md "store
    the minimum personal data")."""
    errors = data.get("errors") or []
    fields = {key: value for key, value in data.items() if key != "errors"}
    fields["error_count"] = len(errors)
    line = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
    logger.info("%s %s", job_name, line, extra={"job": job_name, **fields})


def alert_tick_job(_run: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Body of the `alert_tick` Procrastinate task: run `services.alerts.worker.run_alert_tick`
    against the shared session factory, log its report, and return the report as a dict (the
    Procrastinate job result).

    `_run` is the test seam: pass a fake to exercise this function without importing
    `services.alerts.worker` (which a concurrent agent may not have written yet) or needing a
    database — `build_session_factory()` still runs, but only ever builds a cheap in-memory SQLite
    sessionmaker unless `DATABASE_URL` is set, and a fake `_run` need not use it at all.
    """
    run = _run if _run is not None else _load_fn("services.alerts.worker", "run_alert_tick")
    report = run(build_session_factory())
    data = _report_to_dict(report)
    _log_report("alert_tick", data)
    return data


def post_draft_tick_job(_run: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Body of the `post_draft_tick` Procrastinate task: the `services.social.worker` analogue of
    `alert_tick_job` above — same injection seam, same report handling."""
    run = _run if _run is not None else _load_fn("services.social.worker", "draft_posts_tick")
    report = run(build_session_factory())
    data = _report_to_dict(report)
    _log_report("post_draft_tick", data)
    return data


class VisibilityAuditBreach(RuntimeError):
    """The nightly audit found M-11 > 0: gated or restricted rows reachable on a non-admin
    surface — an S1 incident (docs/04 S-9). Raised *after* the result is persisted and logged, so
    the job shows as failed in the queue (the operator-visible signal) without losing the evidence
    (`GET /admin/v1/visibility-audits/latest`). The message carries the count only, never ids."""


#: The result keys the one log line carries; the row lists (`breaches`, `served.checks`,
#: `gated_sources`) stay in the persisted event and the job result, not in the log.
_VISIBILITY_AUDIT_LOG_KEYS = ("run_id", "posture", "m11", "breach_total", "breaches_truncated")


def visibility_audit_tick_job(_run: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Body of the `visibility_audit_tick` Procrastinate task (docs/04 R-4 "M-11 = 0 in the nightly
    audit"; docs/40 §4 row 11): `services.visibility_audit.run.run_audit` against the shared
    session factory, one summary log line, the full result as the job result, and
    `VisibilityAuditBreach` when `m11 > 0`. Same `_run` seam as `alert_tick_job`."""
    run = _run if _run is not None else _load_fn("services.visibility_audit.run", "run_audit")
    data = _report_to_dict(run(build_session_factory()))
    summary: dict[str, Any] = {key: data.get(key) for key in _VISIBILITY_AUDIT_LOG_KEYS}
    for surface, counts in (data.get("counts") or {}).items():
        if isinstance(counts, Mapping):
            summary[f"{surface}_shown"] = counts.get("shown")
            summary[f"{surface}_breaches"] = counts.get("breaches")
    served = data.get("served") or {}
    if isinstance(served, Mapping):
        summary["served_checked"] = served.get("checked")
        summary["served_leaks"] = served.get("leaks")
        summary["served_inconclusive"] = served.get("inconclusive")
    _log_report("visibility_audit_tick", summary)
    m11 = int(data.get("m11") or 0)
    if m11 > 0:
        raise VisibilityAuditBreach(
            f"M-11 = {m11}: gated or restricted rows reachable on a non-admin surface (S1)"
        )
    return data


# ============================================================================ the closed loop
# Audit 2026-09-18 §3.1 "the always-on loop is not a loop" / §4 item 2: until this landed the
# scheduled path ended at `python -m pipeline.connectors run <id>` — a JSON file under
# `data/runs/`, no `source_run` row, no `source.health`, nothing loaded, resolved or enriched.
# Everything below is the body of one Procrastinate task in `infra/scheduler/app.py` (which stays
# a thin registration layer, ADR 0004) and is importable without Postgres: every `services.*` and
# `pipeline.*` import is lazy (`_load`) or happens inside the function that needs it.

#: Consecutive failed runs at which `source.health` flips from `degraded` to `failing`
#: (api/openapi.yaml `adminListSources`, US-904 AC3: "consecutive_failures >= 3 is failing").
FAILING_AFTER = 3

#: `error_class` values (the runner's `type(error).__name__`) that a later attempt can fix: the
#: host was unreachable, slow or answered 5xx. Anything else — a corrupt payload
#: (`BadZipFile`, `ParserError`), a layout change (`KeyError`), a challenge/robots block
#: (`HttpBlocked`), a gate refusal — is not fixed by fetching again and is not retried.
TRANSIENT_ERROR_CLASSES = frozenset(
    {
        "HttpFailed",
        "ConnectorError",
        "ConnectionError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "ChunkedEncodingError",
        "RemoteDisconnected",
        "ProcessCrashed",
        "TimeoutExpired",
        "StoreWriteError",  # object storage unreachable mid-run (docs/20 §12); the run failed closed
    }
)


class ConnectorRunFailed(RuntimeError):
    """The fetch failed for a reason a retry will not fix (blocked, corrupt payload, refused)."""


class TransientConnectorFailure(RuntimeError):
    """The fetch failed for a reason worth retrying with backoff (network, 5xx, crash, timeout)."""


def parse_result_line(stdout: str) -> dict[str, Any] | None:
    """The CLI's structured `result` log line (`pipeline/connectors/__main__.py`), if any."""
    result: dict[str, Any] | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == "result":
            result = payload
    return result


def is_transient(record: Mapping[str, Any]) -> bool:
    if record.get("status") != "failed":
        return False
    return str(record.get("error_class") or "") in TRANSIENT_ERROR_CLASSES


def _to_datetime(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


#: What a run the scheduler started is recorded as (docs/21 §4.2 `schedule | manual | backfill |
#: retry`). Until 2026-09-27 this module wrote `scheduled` — off the vocabulary — and even that lost
#: to the runner's own default `manual` (below), so every scheduled row read `manual`.
SCHEDULED_TRIGGER = "schedule"
#: Values written before the vocabulary was enforced (migration 0025 rewrites them in the table).
_LEGACY_TRIGGERS = {"scheduled": "schedule"}


def normalise_trigger(value: Any, *, fallback: str = SCHEDULED_TRIGGER) -> str:
    """A `source_run.trigger` value inside `SOURCE_RUN_TRIGGERS`: the legacy `scheduled` becomes
    `schedule`; anything else off the vocabulary is logged and replaced by `fallback` rather than
    failing the row insert on the CHECK (the run happened; losing its row would hide it)."""
    from services.db.models import SOURCE_RUN_TRIGGERS

    text = _LEGACY_TRIGGERS.get(str(value), str(value)) if value else ""
    if text in SOURCE_RUN_TRIGGERS:
        return text
    if value:
        logger.warning("off-vocabulary trigger %r recorded as %r", value, fallback)
    return fallback


#: What a Procrastinate retry of a fetch job is recorded as (docs/21 §4.2). Until 2026-09-27 a retry
#: re-ran the job with its original arguments and was recorded as whatever started the first
#: attempt (`schedule`, or `manual` for a run-now), with `attempt = 1` every time.
RETRY_TRIGGER = "retry"

#: `error_class` of a `running` row closed because no outcome was ever recorded for it: the admin
#: "run now" guard found it older than its cutoff (`services/api/admin_sources.py`
#: `STALE_RUNNING_AFTER`), or the job failed before the connector could report. A row closed this
#: way is still completed by the run's real outcome if one arrives later (`record_source_run`).
ABANDONED_ERROR_CLASS = "RunAbandoned"


def record_source_run(
    session_factory: Any,
    source_id: str,
    record: Mapping[str, Any],
    *,
    trigger: str | None = None,
    attempt: int | None = None,
    dead_lettered: bool | None = None,
) -> bool:
    """Write one `source_run` row from a runner record (docs/21 §4.2) and update the source's
    health, failure counter and last-success/last-error fields (docs/21 §4.1). Returns False when
    the source has no row and cannot get one (a gated source is refused by
    `upsert_licence_and_source`, so there is nothing to attach a run to).

    One row per run, keyed by the record's id (2026-09-27). A row with that id that is still
    `running` — the one the admin "run now" route created before deferring the job, whose id the
    job passed down as `--run-id` — or that was closed as abandoned is completed in place; any other
    existing row means the outcome was already recorded, and nothing changes (idempotent).

    `trigger`, when the caller gives one, wins over the record's: the caller is the one that
    knows who started the run (the scheduler says `schedule`, or `retry` on a Procrastinate retry;
    the admin "run now" path says `manual`/`backfill`). Without one the record's value is used,
    then `schedule`. `attempt` and `dead_lettered` likewise come from the job when it knows them
    (Procrastinate's attempt counter and retry strategy), else from the record."""
    from services.db.models import SOURCE_RUN_STATUSES, Source, SourceRun
    from services.db.session import session_scope

    with session_scope(session_factory) as session:
        source = session.get(Source, source_id)
        if source is None:
            source = _upsert_source(session, source_id)
            if source is None:
                return False
        run_id = _run_uuid(record.get("id"))
        run = session.get(SourceRun, run_id) if run_id is not None else None
        if run is not None and not _is_open(run):
            return True  # already recorded (a retried outcome write)
        status = str(record.get("status") or "failed")
        if status not in SOURCE_RUN_STATUSES or status == "running":
            status = "failed"
        started_at = _to_datetime(record.get("started_at")) or _utcnow()
        finished_at = _to_datetime(record.get("finished_at")) or _utcnow()
        values: dict[str, Any] = {
            "source_id": source_id,
            "trigger": normalise_trigger(trigger or record.get("trigger")),
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "http_status": record.get("http_status"),
            "bytes": record.get("bytes"),
            "egress_class": str(record.get("egress_class") or source.egress or "plain"),
            "rows_seen": int(record.get("rows_seen") or 0),
            "rows_new": int(record.get("rows_new") or 0),
            "rows_changed": int(record.get("rows_changed") or 0),
            "rows_gone": int(record.get("rows_gone") or 0),
            "events_emitted": int(record.get("events_emitted") or 0),
            "worker_seconds": float(record.get("worker_seconds") or 0.0),
            "dq_status": record.get("dq_status"),
            "dq": record.get("dq"),
            "error": (str(record["error"])[:2000] if record.get("error") else None),
            "error_class": record.get("error_class"),
            "attempt": int(attempt if attempt is not None else record.get("attempt") or 1),
            "dead_lettered": bool(
                dead_lettered if dead_lettered is not None else record.get("dead_lettered", False)
            ),
        }
        if run is None:
            run = SourceRun(**values)
            if run_id is not None:
                run.id = run_id
            session.add(run)
        else:
            # The pre-created row keeps the time the operator asked for the run: the admin panel
            # listed it from then, and the queue wait is part of what they waited for.
            values["started_at"] = min(_aware(run.started_at), started_at)
            for key, value in values.items():
                setattr(run, key, value)
        _update_health(source, status, values["error"], finished_at)
        session.flush()
    return True


def _is_open(run: Any) -> bool:
    """A row whose outcome has not been recorded: still `running`, or closed as abandoned."""
    return bool(
        run.status == "running" or (run.status == "failed" and run.error_class == ABANDONED_ERROR_CLASS)
    )


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def close_pending_run(session_factory: Any, run_id: str | None, *, error: str) -> bool:
    """Close a `running` row that will get no outcome from the connector (the job failed before the
    CLI reported, or the CLI refused the run): `failed`, `error_class = RunAbandoned`, `error`. The
    source's health is left alone, since no fetch happened to judge it by. False, and nothing
    written, when there is no such row or it is no longer `running`. Never raises: it runs on
    failure paths, and must not mask the failure that brought it here."""
    from services.db.models import SourceRun
    from services.db.session import session_scope

    key = _run_uuid(run_id)
    if key is None:
        return False
    try:
        with session_scope(session_factory) as session:
            run = session.get(SourceRun, key)
            if run is None or run.status != "running":
                return False
            run.status = "failed"
            run.finished_at = _utcnow()
            run.error = error[:2000]
            run.error_class = ABANDONED_ERROR_CLASS
            session.flush()
    except Exception:
        logger.exception("could not close the pending run row", extra={"run_id": run_id})
        return False
    logger.warning("pending run row closed as abandoned", extra={"run_id": run_id, "error": error[:500]})
    return True


def _run_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _upsert_source(session: Any, source_id: str) -> Any | None:
    """Mirror the registry entry into `source` the same way the loader does; None when the
    source is gated or unknown (nothing to write a run against, logged, never raised)."""
    from pipeline.connectors.registry import RegistrationError, Registry
    from services.ingest.loader import GateRefused, upsert_licence_and_source

    registry = Registry()
    try:
        entry = registry.get(source_id)
        return upsert_licence_and_source(session, entry, registry.version)
    except (GateRefused, RegistrationError, KeyError) as exc:
        logger.info("no source row for %s: %s", source_id, exc, extra={"source_id": source_id})
        return None


def _update_health(source: Any, status: str, error: str | None, finished_at: dt.datetime) -> None:
    """docs/21 §4.1 vocabulary: ok | degraded | failing | blocked | paused. `ok`/`unchanged` are
    successes; `partial` (a DQ hold) fetched fine but published nothing, so it degrades health
    without counting as a failure; `failed`/`budget` count; `blocked` is its own state. A paused
    source keeps `paused` whatever its runs do."""
    if status in ("ok", "unchanged"):
        source.consecutive_failures = 0
        source.last_success_at = finished_at
        source.last_error = None
        source.last_error_at = None
        if source.health != "paused":
            source.health = "ok"
        return
    if status == "partial":
        if source.health not in ("paused", "failing", "blocked"):
            source.health = "degraded"
        return
    source.consecutive_failures = int(source.consecutive_failures or 0) + 1
    source.last_error = error
    source.last_error_at = finished_at
    if source.health == "paused":
        return
    if status == "blocked":
        source.health = "blocked"
    else:
        source.health = "failing" if source.consecutive_failures >= FAILING_AFTER else "degraded"


def fetch_outcome(
    source_id: str,
    *,
    returncode: int | None,
    stdout: str,
    stderr: str,
    trigger: str = SCHEDULED_TRIGGER,
    started_at: dt.datetime | None = None,
    session_factory: Any = None,
    timeout_s: int | None = None,
    run_id: str | None = None,
    attempt: int = 1,
    final_attempt: bool = False,
) -> dict[str, Any]:
    """Turn one `python -m pipeline.connectors run <id>` invocation into a `source_run` row plus
    a health update, and say what the caller should do next: `status` (the runner's, or
    `refused` for a gate/registration refusal, or `failed` for a crash/timeout), `ts` (the run's
    snapshot token, for `load_source`) and `transient` (whether a retry is worth it).

    `run_id` is the id the job passed as `--run-id` (the row an admin run-now created): a crash or
    timeout with no record from the CLI is recorded against it, and a refusal closes it, so that
    row never stays `running`. `attempt` is the job's 1-based attempt number; `final_attempt`
    says the retry strategy will not run it again, so a transient failure now is the dead letter
    (docs/20 §4.2) and its row says `dead_lettered`."""
    result = parse_result_line(stdout)
    record: dict[str, Any] | None = None
    if result is not None and result.get("run_path"):
        record = _read_run_record(result)
    if record is None and result is not None:
        record = {"status": result.get("status"), "error": result.get("error"), "id": result.get("run_id")}
    if record is None and returncode == 2:
        # The CLI's own refusals (gate, unregistered): logged there, nothing to run or retry.
        logger.warning("fetch refused", extra={"source_id": source_id, "stderr": stderr[-2000:]})
        if run_id is not None:
            close_pending_run(
                session_factory if session_factory is not None else build_session_factory(),
                run_id,
                error=f"the connector CLI refused the run: {stderr[-1500:]}",
            )
        return {"status": "refused", "ts": None, "transient": False, "record": None}
    if record is None:
        detail = "timeout" if returncode is None else f"exit {returncode}"
        tail = (stderr or stdout)[-2000:]
        record = {
            "status": "failed",
            "started_at": (started_at or _utcnow()).isoformat(),
            "finished_at": _utcnow().isoformat(),
            "error": f"connector process {detail}"
            + (f" after {timeout_s}s" if returncode is None else "")
            + f": {tail}",
            "error_class": "TimeoutExpired" if returncode is None else "ProcessCrashed",
        }
    if run_id is not None and not record.get("id"):
        record["id"] = run_id  # the CLI never reported an id: the outcome belongs to the named run
    transient = is_transient(record)
    factory = session_factory if session_factory is not None else build_session_factory()
    recorded = record_source_run(
        factory,
        source_id,
        record,
        trigger=trigger,
        attempt=attempt,
        dead_lettered=transient and final_attempt,
    )
    ts = Path(str(result["run_path"])).stem if result is not None and result.get("run_path") else None
    status = str(record.get("status") or "failed")
    logger.info(
        "fetch outcome",
        extra={"source_id": source_id, "status": status, "recorded": recorded, "ts": ts},
    )
    return {"status": status, "ts": ts, "transient": transient, "record": record}


def _read_run_record(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """The run record the CLI's result line points at: from the object store when the CLI wrote
    to one (`store: s3` plus `run_key`), otherwise from the local file at `run_path`."""
    try:
        if result.get("store") == "s3" and result.get("run_key"):
            from pipeline.connectors.store import open_store

            data = json.loads(open_store().backend.get(str(result["run_key"])).decode("utf-8"))
        else:
            data = json.loads(Path(str(result["run_path"])).read_text(encoding="utf-8"))
    except Exception as exc:  # unreadable record -> the thin record from the result line
        logger.warning("run record unreadable", extra={"error": repr(exc)[:500]})
        return None
    return data if isinstance(data, dict) else None


def _result_to_dict(result: Any) -> dict[str, Any]:
    data = _report_to_dict(result)
    return {key: (str(value) if isinstance(value, uuid.UUID) else value) for key, value in data.items()}


def connector_data_root() -> Path:
    """Where `python -m pipeline.connectors run` wrote the run `load_source` reads: the same
    `INFRAQUE_DATA_DIR` rule as `pipeline/connectors/store.py`'s `DATA_DIR` (default `data/` in the
    checkout). Read here rather than imported, so the loader keeps working whichever storage
    backend that module grows (lane E10b); in a container it is the `connector_data` volume
    (infra/compose/docker-compose.yml)."""
    return Path(os.environ.get("INFRAQUE_DATA_DIR") or ROOT / "data")


def load_source_job(
    source_id: str,
    ts: str,
    *,
    _load: Callable[..., Any] | None = None,
    _data_root: Path | None = None,
    _plan: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Body of `load_source`: `services.ingest.loader.load_from_files` for one run's parquet
    (the same call `web/data_loading.py` makes). A gate refusal or a kind refusal (a `document`
    source: `services.ingest.loader.KindRefused`) is logged and returned, never raised — there is
    nothing to retry.

    Replay (architect audit 2026-09-30 A6): the runner diffs each run against the previous promoted
    snapshot whether or not it was loaded, so a load that failed used to lose its run's change events
    for good. `services.ingest.loader.runs_to_load` lists the promoted runs after the source's
    `last_loaded_ts`, oldest first, ending with `ts`; each is loaded and committed in its own
    transaction, so a replay that fails part-way keeps the runs it finished and the next load resumes
    after them. A run older than the last loaded one is reported `superseded` and not loaded. The
    report is the last run's, with `replayed` naming the earlier runs loaded first."""
    load = _load if _load is not None else _load_fn("services.ingest.loader", "load_from_files")
    plan = _plan if _plan is not None else _load_fn("services.ingest.loader", "runs_to_load")
    from pipeline.connectors import store as store_module
    from services.db.session import session_scope

    # The same root and backend the fetch wrote through: `INFRAQUE_DATA_DIR` read at call time
    # (the container's `connector_data` volume) and `SNAPSHOT_STORE` (docs/60 §5), so with the S3
    # backend the load reads the bucket and need not run on the host that fetched.
    data_root = _data_root if _data_root is not None else connector_data_root()
    store = store_module.open_store(data_root)
    factory = build_session_factory()
    with session_scope(factory) as session:
        pending = [str(run_ts) for run_ts in plan(session, store, source_id, ts)]
    if not pending:
        logger.info("load superseded by a later loaded run", extra={"source_id": source_id, "ts": ts})
        return {"source_id": source_id, "ts": ts, "skipped": "superseded"}
    if len(pending) > 1:
        logger.warning(
            "replaying runs whose load did not commit", extra={"source_id": source_id, "runs": pending[:-1]}
        )
    result: Any = None
    for run_ts in pending:
        with session_scope(factory) as session:
            try:
                result = load(session, source_id, run_ts, data_root=data_root, store=store)
            except Exception as exc:
                if type(exc).__name__ == "GateRefused":
                    logger.warning("load refused", extra={"source_id": source_id, "error": str(exc)})
                    return {"source_id": source_id, "ts": ts, "skipped": "gate refused"}
                if type(exc).__name__ == "KindRefused":
                    logger.warning("load refused", extra={"source_id": source_id, "error": str(exc)})
                    return {"source_id": source_id, "ts": ts, "skipped": "kind refused"}
                raise
    data = _result_to_dict(result)
    data.update(source_id=source_id, ts=ts)
    if len(pending) > 1:
        data["replayed"] = pending[:-1]
    _log_report("load_source", data)
    return data


def load_kind_refusal(source_id: str, *, registry: Any = None) -> str | None:
    """Why the generic `load_source` would refuse `source_id` by its connector's kind, or None
    when it loads it (`services.ingest.loader.kind_refusal`, the loader's own rule). The scheduler
    asks before it queues a load, so a `document` source (FERC eLibrary every 15 minutes, EIA-860
    and GHGRP once a year) is fetched, snapshotted and diffed but never queued for a load that
    would only be refused (2026-09-30, lane FX2)."""
    from pipeline.connectors.registry import Registry

    kind_refusal = _load_fn("services.ingest.loader", "kind_refusal")
    refusal = kind_refusal(registry if registry is not None else Registry(), source_id)
    return str(refusal) if refusal else None


class HoldReleaseRefused(RuntimeError):
    """`release_held_run` found the run not releasable in the store (not held, superseded by a
    later run, gated, or its record missing). Raised so the job shows as failed in the queue; the
    `source_run` row is left as it was (`status = partial`, the release request recorded)."""


def release_held_job(
    source_id: str,
    run_id: str,
    released_by: str,
    *,
    _release: Callable[..., Any] | None = None,
    _session_factory: Any = None,
    _data_root: Path | None = None,
) -> dict[str, Any]:
    """Body of `release_held_run` (docs/21 §4.2, `POST /admin/v1/source-runs/{run_id}/release`):
    promote the held frame of one run through `pipeline.connectors.runner.release_held` — the same
    diff and all-or-nothing write a passing run makes — then mark its `source_run` row `ok` with
    the diff counts the promotion produced. The caller (`infra/scheduler/app.py`) enqueues
    `load_source` for the returned `ts`, exactly as after a fetch that passed its gates.

    Idempotent: a run already promoted comes back `already_released` and its row is re-marked
    with the same values. Reads and writes through the same store the fetch used (`open_store`
    over `INFRAQUE_DATA_DIR`), as `load_source_job` does."""
    release = _release if _release is not None else _load_fn("pipeline.connectors.runner", "release_held")
    from pipeline.connectors import store as store_module

    data_root = _data_root if _data_root is not None else connector_data_root()
    try:
        result = release(source_id, run_id, released_by=released_by, store=store_module.open_store(data_root))
    except Exception as exc:
        if type(exc).__name__ == "ReleaseRefused":
            logger.warning(
                "hold release refused",
                extra={"source_id": source_id, "run_id": run_id, "code": getattr(exc, "code", None)},
            )
            raise HoldReleaseRefused(str(exc)) from exc
        raise
    record: Mapping[str, Any] = result.run
    factory = _session_factory if _session_factory is not None else build_session_factory()
    marked = _mark_run_released(factory, run_id, record)
    data: dict[str, Any] = {
        "source_id": source_id,
        "run_id": run_id,
        "ts": result.ts,
        "already_released": bool(result.already_released),
        "row_marked": marked,
        "rows_new": int(record.get("rows_new") or 0),
        "rows_changed": int(record.get("rows_changed") or 0),
        "rows_gone": int(record.get("rows_gone") or 0),
        "events_emitted": int(record.get("events_emitted") or 0),
    }
    _log_report("release_held_run", data)
    return data


def _mark_run_released(session_factory: Any, run_id: str, record: Mapping[str, Any]) -> bool:
    """Set the released run's row to what the promotion produced. False (logged) when no row
    carries that id — the run was never recorded — which does not undo the promotion."""
    from services.db.models import SourceRun
    from services.db.session import session_scope

    key = _run_uuid(run_id)
    with session_scope(session_factory) as session:
        run = session.get(SourceRun, key) if key is not None else None
        if run is None:
            logger.warning("released run has no source_run row", extra={"run_id": run_id})
            return False
        run.status = "ok"
        run.rows_new = int(record.get("rows_new") or 0)
        run.rows_changed = int(record.get("rows_changed") or 0)
        run.rows_gone = int(record.get("rows_gone") or 0)
        run.events_emitted = int(record.get("events_emitted") or 0)
        session.flush()
    return True


def resolve_tick_job(
    _run: Callable[..., Any] | None = None, *, _session_factory: Any = None, _data_root: Path | None = None
) -> dict[str, Any]:
    """Body of `resolve_tick`: organisations through `services.resolve.merge.resolve_organizations`
    (store-based) and proposal clusters through the same chain `services/resolve/report.py` runs
    — `pipeline.resolve.run` over the latest normalised frame of every loadable proposal source,
    then `build_clusters` / `apply_all_clusters` under the confidence gate."""
    factory = _session_factory if _session_factory is not None else build_session_factory()
    run = _run if _run is not None else functools.partial(default_resolve, data_root=_data_root)
    data = _report_to_dict(run(factory))
    _log_report("resolve_tick", data)
    return data


def default_resolve(session_factory: Any, *, data_root: Path | None = None) -> dict[str, Any]:
    from services.db.session import session_scope

    resolve_organizations = _load_fn("services.resolve.merge", "resolve_organizations")
    norm_org = _load_fn("pipeline.normalize", "norm_org")
    report: dict[str, Any] = {
        "organizations_merged": 0,
        "proposal_clusters": 0,
        "proposals_merged": 0,
        "decisions_proposed": 0,
        "proposals_suppressed": 0,
    }
    with session_scope(session_factory) as session:
        org_report = resolve_organizations(session, norm_org)
        report["organizations_merged"] = int(getattr(org_report, "merged", 0) or 0)
        frames = _latest_proposal_frames(data_root)
        if frames:
            report.update(_resolve_proposal_clusters(session, frames))
    # Reviewed suppressions (services/resolve/suppress.py) act on the merged store, so they run last.
    with session_scope(session_factory) as session:
        suppressed = _load_fn("services.resolve.suppress", "apply_suppressions")(session)
        report["proposals_suppressed"] = len(suppressed.unpublished)
    return report


def _resolve_proposal_clusters(session: Any, frames: list[Any]) -> dict[str, int]:
    """Proposal clusters over the latest frames, through the confidence gate (`default_resolve`)."""
    import pandas as pd

    resolve_run = _load_fn("pipeline.resolve", "run")
    report_mod = importlib.import_module("services.resolve.report")
    merge_mod = importlib.import_module("services.resolve.merge")
    df = pd.concat(frames, ignore_index=True)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "normalized.parquet"
        df.to_parquet(path, index=False)
        matches, clusters = resolve_run(merge_mod.MERGE_SCORE_THRESHOLD, path)
    loaded_sources = {str(s): str(s) for s in df["source_id"].unique()}
    link_index = report_mod.build_link_index(session)
    members, edges = report_mod.build_clusters(df, matches, clusters, loaded_sources, link_index)
    multi = {k: v for k, v in members.items() if len(v) >= 2}
    applications = report_mod.apply_all_clusters(session, multi, edges)
    return {
        "proposal_clusters": len(multi),
        "proposals_merged": sum(a.members_merged for a in applications if a.action == "merged"),
        "decisions_proposed": sum(len(a.decisions) for a in applications if a.action == "proposed"),
    }


def _latest_proposal_frames(data_root: Path | None, *, registry: Any = None) -> list[Any]:
    """The latest normalised frame of every implemented `proposal` connector whose rows the loader
    loads. The loader's own rule decides (`services.ingest.loader.load_refusal`: reuse class,
    `publication`, platform posture), so a source it refuses never reaches `pipeline.resolve.run`,
    where it would still change blocking groups and fuzzy candidate sets (docs/25 §3.9)."""
    from pipeline.connectors.registry import Registry
    from pipeline.connectors.store import open_store

    load_refusal = _load_fn("services.ingest.loader", "load_refusal")
    registry = registry if registry is not None else Registry()
    store = open_store(data_root)
    frames = []
    for row in registry.status():
        if row.get("state") != "implemented":
            continue
        source_id = str(row["id"])
        refusal = load_refusal(registry.get(source_id))
        if refusal:
            logger.info("resolve: skipping %s (%s)", source_id, refusal, extra={"source_id": source_id})
            continue
        try:
            connector_cls = registry.connector_class(source_id)
        except Exception as exc:
            logger.info("resolve: skipping %s (%s)", source_id, exc, extra={"source_id": source_id})
            continue
        if getattr(connector_cls, "kind", None) != "proposal":
            continue
        frame, _ = store.previous_normalized(source_id)
        if frame is not None and len(frame):
            frames.append(frame)
    return frames


def enrich_tick_job(
    _run: Callable[..., Any] | None = None, *, _session_factory: Any = None
) -> dict[str, Any]:
    """Body of `enrich_tick`: `services.ingest.geocode.backfill_county_fips` over the store (the
    loader geocodes at load time; this fills what a later gazetteer or a source-level change
    left behind). Further enrichment stages (docs/20 §3.6) plug in here."""
    factory = _session_factory if _session_factory is not None else build_session_factory()
    run = _run if _run is not None else default_enrich
    data = _report_to_dict(run(factory))
    _log_report("enrich_tick", data)
    return data


def default_enrich(session_factory: Any) -> dict[str, Any]:
    backfill = _load_fn("services.ingest.geocode", "backfill_county_fips")
    with session_factory() as session:
        result: dict[str, Any] = dict(backfill(session))
    return result
