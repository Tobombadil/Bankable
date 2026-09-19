"""Job bodies for the `alert_tick` and `post_draft_tick` periodic jobs (docs/00-PLAN.md Sprint 3
item 4; ADR 0004).

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


def record_source_run(
    session_factory: Any, source_id: str, record: Mapping[str, Any], *, trigger: str = "scheduled"
) -> bool:
    """Write one `source_run` row from a runner record (docs/21 §4.2) and update the source's
    health, failure counter and last-success/last-error fields (docs/21 §4.1). Idempotent per
    run id. Returns False when the source has no row and cannot get one (a gated source is
    refused by `upsert_licence_and_source`, so there is nothing to attach a run to)."""
    from services.db.models import SOURCE_RUN_STATUSES, Source, SourceRun
    from services.db.session import session_scope

    with session_scope(session_factory) as session:
        source = session.get(Source, source_id)
        if source is None:
            source = _upsert_source(session, source_id)
            if source is None:
                return False
        run_id = _run_uuid(record.get("id"))
        if run_id is not None and session.get(SourceRun, run_id) is not None:
            return True  # already recorded (a retried outcome write)
        status = str(record.get("status") or "failed")
        if status not in SOURCE_RUN_STATUSES:
            status = "failed"
        started_at = _to_datetime(record.get("started_at")) or _utcnow()
        finished_at = _to_datetime(record.get("finished_at")) or _utcnow()
        run = SourceRun(
            source_id=source_id,
            trigger=str(record.get("trigger") or trigger),
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            http_status=record.get("http_status"),
            bytes=record.get("bytes"),
            egress_class=str(record.get("egress_class") or source.egress or "plain"),
            rows_seen=int(record.get("rows_seen") or 0),
            rows_new=int(record.get("rows_new") or 0),
            rows_changed=int(record.get("rows_changed") or 0),
            rows_gone=int(record.get("rows_gone") or 0),
            events_emitted=int(record.get("events_emitted") or 0),
            worker_seconds=float(record.get("worker_seconds") or 0.0),
            dq_status=record.get("dq_status"),
            dq=record.get("dq"),
            error=(str(record["error"])[:2000] if record.get("error") else None),
            error_class=record.get("error_class"),
            attempt=int(record.get("attempt") or 1),
            dead_lettered=bool(record.get("dead_lettered", False)),
        )
        if run_id is not None:
            run.id = run_id
        session.add(run)
        _update_health(source, status, run.error, finished_at)
        session.flush()
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
    trigger: str = "scheduled",
    started_at: dt.datetime | None = None,
    session_factory: Any = None,
    timeout_s: int | None = None,
) -> dict[str, Any]:
    """Turn one `python -m pipeline.connectors run <id>` invocation into a `source_run` row plus
    a health update, and say what the caller should do next: `status` (the runner's, or
    `refused` for a gate/registration refusal, or `failed` for a crash/timeout), `ts` (the run's
    snapshot token, for `load_source`) and `transient` (whether a retry is worth it)."""
    result = parse_result_line(stdout)
    record: dict[str, Any] | None = None
    if result is not None and result.get("run_path"):
        path = Path(str(result["run_path"]))
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = None
    if record is None and result is not None:
        record = {"status": result.get("status"), "error": result.get("error"), "id": result.get("run_id")}
    if record is None and returncode == 2:
        # The CLI's own refusals (gate, unregistered): logged there, nothing to run or retry.
        logger.warning("fetch refused", extra={"source_id": source_id, "stderr": stderr[-2000:]})
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
    factory = session_factory if session_factory is not None else build_session_factory()
    recorded = record_source_run(factory, source_id, record, trigger=trigger)
    ts = Path(str(result["run_path"])).stem if result is not None and result.get("run_path") else None
    status = str(record.get("status") or "failed")
    logger.info(
        "fetch outcome",
        extra={"source_id": source_id, "status": status, "recorded": recorded, "ts": ts},
    )
    return {"status": status, "ts": ts, "transient": is_transient(record), "record": record}


def _result_to_dict(result: Any) -> dict[str, Any]:
    data = _report_to_dict(result)
    return {key: (str(value) if isinstance(value, uuid.UUID) else value) for key, value in data.items()}


def load_source_job(
    source_id: str, ts: str, *, _load: Callable[..., Any] | None = None, _data_root: Path | None = None
) -> dict[str, Any]:
    """Body of `load_source`: `services.ingest.loader.load_from_files` for one run's parquet
    (the same call `web/data_loading.py` makes), committed as one transaction. A gate refusal
    is logged and returned, never raised — there is nothing to retry."""
    load = _load if _load is not None else _load_fn("services.ingest.loader", "load_from_files")
    from services.db.session import session_scope

    data_root = _data_root if _data_root is not None else ROOT / "data"
    with session_scope(build_session_factory()) as session:
        try:
            result = load(session, source_id, ts, data_root=data_root)
        except Exception as exc:
            if type(exc).__name__ == "GateRefused":
                logger.warning("load refused", extra={"source_id": source_id, "error": str(exc)})
                return {"source_id": source_id, "ts": ts, "skipped": "gate refused"}
            raise
    data = _result_to_dict(result)
    data.update(source_id=source_id, ts=ts)
    _log_report("load_source", data)
    return data


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
    import pandas as pd

    from services.db.session import session_scope

    resolve_organizations = _load_fn("services.resolve.merge", "resolve_organizations")
    norm_org = _load_fn("pipeline.normalize", "norm_org")
    report: dict[str, Any] = {
        "organizations_merged": 0,
        "proposal_clusters": 0,
        "proposals_merged": 0,
        "decisions_proposed": 0,
    }
    with session_scope(session_factory) as session:
        org_report = resolve_organizations(session, norm_org)
        report["organizations_merged"] = int(getattr(org_report, "merged", 0) or 0)
        frames = _latest_proposal_frames(data_root)
        if not frames:
            return report
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
        report["proposal_clusters"] = len(multi)
        report["proposals_merged"] = sum(a.members_merged for a in applications if a.action == "merged")
        report["decisions_proposed"] = sum(len(a.decisions) for a in applications if a.action == "proposed")
    return report


def _latest_proposal_frames(data_root: Path | None) -> list[Any]:
    """The latest normalised frame of every implemented, non-gated `proposal` connector."""
    from pipeline.connectors.registry import Registry
    from pipeline.connectors.store import Store

    registry = Registry()
    store = Store(data_root) if data_root is not None else Store()
    frames = []
    for row in registry.status():
        if row.get("state") != "implemented":
            continue
        source_id = str(row["id"])
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
