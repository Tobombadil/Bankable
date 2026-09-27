"""One source run: fetch -> snapshot -> parse -> normalise -> DQ gates -> diff -> store
(docs/20 §3.1–3.4, docs/21 §4.2 `source_run`, docs/04 DA-6).

Stage rules: every stage writes its output before the next starts; a failure fails closed and is
recorded on the run; a DQ hold keeps the raw snapshot (evidence) and writes nothing publishable;
a gated source (reuse restricted/unknown) is refused unless `allow_restricted=True` and is then
routed to the quarantine store, which cannot write to a publishable path.

Object-store failures fail closed (docs/20 §12): a snapshot or parquet write that raises
`StoreError` ends the run `failed`, after the run's own normalised/events/held objects are deleted
again, and the run record — the commit marker every downstream reader starts from — is written
last. If that record cannot be written either, the exception propagates and the CLI exits 1 with
no result line, which the scheduler records as a crash and retries.

`trigger` is what the run record says started the run, one of `RUN_TRIGGERS` (docs/21 §4.2):
the CLI defaults to `manual` and the scheduler passes `schedule` explicitly, so a scheduled run is
never recorded as a manual one. An off-vocabulary value is refused before any I/O.

`release_held` is the other way a run's output reaches `normalized/`: an operator accepted a
data-quality hold (`POST /admin/v1/source-runs/{run_id}/release`), so the held frame is diffed
against the previous normalised snapshot and written exactly as step 6 of `run` would have, the
run record is rewritten `ok` with a `release` block, and the caller enqueues the load. It refuses
a run that is not held, one a later run has superseded, and any gated source (the same gate the
runner and the loader apply; a release never makes quarantined output publishable).
"""

from __future__ import annotations

import datetime as dt
import logging
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from pipeline.connectors.base import (
    BlockedError,
    ConnectorError,
    GateViolation,
    RawSnapshot,
    utcnow,
)
from pipeline.connectors.dedupe import align_previous_keys
from pipeline.connectors.dq import DQResult, run_gates
from pipeline.connectors.http import HttpBlocked, HttpFailed, PoliteSession
from pipeline.connectors.objectstore import StoreError
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import QuarantineStore, Store, open_store, ts_token
from pipeline.diff import EVENT_TYPES, diff_snapshots

log = logging.getLogger("pipeline.connectors")

#: docs/21 §4.2 `source_run.trigger`. Mirrors `services.db.models.SOURCE_RUN_TRIGGERS` (the
#: pipeline does not import the ORM); `infra/scheduler/test_trigger.py` pins the two equal.
RUN_TRIGGERS = ("schedule", "manual", "backfill", "retry")

# Opportunity columns that play the roles pipeline/diff.py keys on (docs/21 §7.2 vs §7.1).
_DIFF_ALIASES = {"lifecycle_state": "status", "capacity_mw": "capacity_sought_mw", "proposed_cod": "due_at"}


@dataclass
class RunResult:
    run: dict[str, Any]
    records: pd.DataFrame | None = None
    events: pd.DataFrame | None = None
    dq: DQResult | None = None
    paths: dict[str, pathlib.Path] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return str(self.run["status"])


def _diff_view(df: pd.DataFrame) -> pd.DataFrame:
    view = df.copy()
    for target, src in _DIFF_ALIASES.items():
        if target not in view.columns and src in view.columns:
            view[target] = view[src]
    return view


def _discard(st: Store, paths: list[pathlib.Path], source_id: str, run_id: str) -> None:
    """Best-effort removal of the outputs a failed run had already written, so no downstream stage
    (or a consumer globbing the tree) sees a half-written result. A failure here is logged, not
    raised: without a run record those objects are unreachable through the store anyway."""
    for path in paths:
        try:
            st.delete(path)
        except Exception:
            log.warning(
                "could not remove partial output",
                extra={"source_id": source_id, "run_id": run_id, "path": st.locate(path)},
                exc_info=True,
            )


def _source_columns(rows: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for r in rows:
        for k in r:
            seen.setdefault(str(k), None)
    return list(seen)


def run(
    source_id: str,
    *,
    registry: Registry | None = None,
    store: Store | None = None,
    allow_restricted: bool = False,
    trigger: str = "manual",
    http: PoliteSession | None = None,
    raw: RawSnapshot | None = None,
    now: dt.datetime | None = None,
) -> RunResult:
    """Run one connector end to end and persist everything the run produced.

    `raw` injects a recorded snapshot (tests, replays) so `fetch()` is skipped.
    Raises `GateViolation` before any I/O when the source is gated and the flag is absent.
    """
    if trigger not in RUN_TRIGGERS:
        raise ValueError(f"trigger must be one of {RUN_TRIGGERS}, got {trigger!r}")
    registry = registry or Registry()
    source = registry.get(source_id)
    connector = registry.instantiate(source_id, allow_restricted=allow_restricted, http=http)
    base_store = store if store is not None else open_store()
    if source.gated:
        # allow_restricted=True got us here: outputs are quarantined, never publishable.
        st: Store = QuarantineStore(base_store.base_root, backend=base_store.backend)
    else:
        st = base_store

    started = now or utcnow()
    t0 = time.monotonic()
    run_id = str(uuid.uuid4())
    record: dict[str, Any] = {
        "id": run_id,
        "source_id": source_id,
        "trigger": trigger,
        "started_at": started.isoformat(),
        "finished_at": None,
        "status": "running",
        "egress_class": connector.egress,
        "reuse_class": source.reuse,
        "licence_id": source.licence_id,
        "publishable": st.publishable,
        "parser_version": f"{source_id}@{connector.parser_version}",
        "attempt": 1,
        "dead_lettered": False,
        "http_status": None,
        "bytes": None,
        "snapshot": None,
        "rows_seen": 0,
        "rows_fetched": 0,
        "rows_new": 0,
        "rows_changed": 0,
        "rows_gone": 0,
        "events_emitted": 0,
        "model_calls": 0,
        "cost_usd": 0.0,
        "worker_seconds": 0.0,
        "dq_status": None,
        "dq": None,
        "stats": None,
        "error": None,
        "error_class": None,
        "outputs": {},
    }
    result = RunResult(run=record)

    def finish(status: str, error: Exception | None = None) -> RunResult:
        record["status"] = status
        record["finished_at"] = utcnow().isoformat()
        record["worker_seconds"] = round(time.monotonic() - t0, 2)
        if error is not None:
            record["error"] = repr(error)[:500]
            record["error_class"] = type(error).__name__
        ts = record.get("_ts") or ts_token(started)
        record.pop("_ts", None)
        result.paths["run"] = st.write_run(source_id, ts, record)
        log.info(
            "run finished",
            extra={
                "source_id": source_id,
                "run_id": run_id,
                "status": status,
                "rows": record["rows_seen"],
                "dq": record["dq_status"],
            },
        )
        return result

    # 1. fetch ------------------------------------------------------------
    try:
        snap = raw or connector.fetch()
    except (HttpBlocked, BlockedError) as e:
        return finish("blocked", e)
    except (ConnectorError, HttpFailed) as e:
        return finish("failed", e)
    except GateViolation:
        raise  # never caught-and-continued (docs/04 E-17)
    except Exception as e:
        log.exception("fetch crashed", extra={"source_id": source_id, "run_id": run_id})
        return finish("failed", e)
    ts = ts_token(snap.retrieved_at)
    record["_ts"] = ts
    record["http_status"] = snap.http_status
    content = connector.redact(snap.content)
    snap.redacted = content != snap.content
    snap.content = content
    record["bytes"] = len(content)
    record["snapshot"] = {
        "object_key": None,
        "sha256": snap.sha256,
        "byte_size": len(content),
        "content_type": snap.content_type,
        "fetched_url": snap.url,
        "http_status": snap.http_status,
        "retrieved_at": snap.retrieved_at_iso,
        "licence_id": source.licence_id,
        "parser_version": record["parser_version"],
        "record_count": None,
        "requests_made": snap.requests_made,
        "redacted": snap.redacted,
        "meta": snap.meta,
    }

    # 2. snapshot (unchanged short-circuit, docs/20 §3.2) ------------------
    if st.last_snapshot_sha(source_id) == snap.sha256:
        return finish("unchanged")
    try:
        path = st.write_snapshot(source_id, ts, snap.ext, content)
    except StoreError as e:
        log.error("snapshot write failed", extra={"source_id": source_id, "run_id": run_id, "error": repr(e)})
        return finish("failed", e)
    record["snapshot"]["object_key"] = st.locate(path)
    result.paths["snapshot"] = path

    # 3. parse + normalise ------------------------------------------------
    # Fail closed on *anything* the parser raises (docs/04 E-17): a truncated xlsx surfaces as
    # `zipfile.BadZipFile`, a corrupt CSV as a pandas parser error, a layout change as
    # `KeyError` — none of them may escape this function, because the run record is the only
    # evidence the batch has that this source is broken, and the next source must still run
    # (audit 2026-09-18 §3.1 item 3). The raw snapshot above is kept as evidence.
    try:
        rows = connector.parse(snap)
        df = connector.normalize(rows, snap)
    except GateViolation:
        raise
    except Exception as e:
        log.exception("parse/normalise failed", extra={"source_id": source_id, "run_id": run_id})
        return finish("failed", e)
    record["rows_fetched"] = len(df)
    record["snapshot"]["record_count"] = len(df)
    source_columns = _source_columns(rows)

    prev_df, prev_run_id = st.previous_normalized(source_id)
    if prev_df is not None:
        # Legacy positional `#N` keys, and unique <-> duplicated transitions, are matched to the
        # current content keys before anything is compared (pipeline/connectors/dedupe.py).
        prev_df = align_previous_keys(prev_df, df)
    if connector.snapshot_mode == "incremental" and prev_df is not None:
        keep = prev_df[~prev_df["record_id"].isin(df["record_id"])]
        df = pd.concat([keep[df.columns.intersection(keep.columns)], df], ignore_index=True)
    record["rows_seen"] = len(df)
    record["snapshot"]["previous_run_id"] = prev_run_id

    # 4. DQ gates ---------------------------------------------------------
    dq = run_gates(
        df,
        connector.kind,
        source_columns,
        st.dq_history(source_id),
        required=connector.dq_required_fields,
        key_source_columns=connector.key_source_columns,
        duplicates_resolved=int(df.attrs.get("duplicates_resolved", 0)),
        rows_fetched=record["rows_fetched"],
    )
    result.dq = dq
    record["dq_status"] = dq.dq_status
    record["dq"] = dq.to_dict()
    record["stats"] = dq.stats
    result.records = df
    if dq.held:
        record["hold_reasons"] = dq.hold_reasons()
        held_path = st.held_path(source_id, ts)
        try:
            result.paths["held"] = st.write_parquet(held_path, df)
        except StoreError as e:
            _discard(st, [held_path], source_id, run_id)
            return finish("failed", e)
        record["outputs"]["held"] = st.locate(held_path)
        return finish("partial")

    # 5-6. diff against the previous normalised snapshot, then store ----------
    try:
        _diff_and_store(st, source_id, ts, df, prev_df, snap.retrieved_at_iso, record, result)
    except StoreError as e:
        return finish("failed", e)
    return finish("ok")


def _diff_and_store(
    st: Store,
    source_id: str,
    ts: str,
    df: pd.DataFrame,
    prev_df: pd.DataFrame | None,
    observed_at: str,
    record: dict[str, Any],
    result: RunResult,
) -> None:
    """Steps 5 and 6 of a run, shared with `release_held`: diff `df` against `prev_df`, set the
    record's diff counts, and write the normalised frame (+ events) all-or-nothing. A failed write
    deletes whatever this step already wrote and re-raises `StoreError` for the caller to record."""
    if prev_df is not None:
        events = diff_snapshots(_diff_view(prev_df), _diff_view(df), observed_at=observed_at)
    else:
        events = pd.DataFrame(
            columns=["event_type", "record_id", "source_id", "field", "before", "after", "observed_at"]
        )
        events["event_type"] = pd.Categorical(events["event_type"], categories=EVENT_TYPES)
    counts = events["event_type"].value_counts()
    record["rows_new"] = int(counts.get("new", 0)) if prev_df is not None else len(df)
    record["rows_gone"] = int(counts.get("removed", 0))
    record["rows_changed"] = int(
        events.loc[~events["event_type"].isin(["new", "removed"]), "record_id"].nunique()
    )
    record["events_emitted"] = len(events)
    result.events = events

    # Publishable outputs only from a publishable store. All-or-nothing: a failed write deletes
    # whatever this step already wrote and fails the run.
    targets = [st.normalized_path(source_id, ts)] + ([st.events_path(source_id, ts)] if len(events) else [])
    try:
        result.paths["normalized"] = st.write_parquet(targets[0], df)
        if len(events):
            ev = events.copy()
            ev["event_type"] = ev["event_type"].astype(str)
            result.paths["events"] = st.write_parquet(targets[1], ev)
    except StoreError as e:
        log.error(
            "output write failed",
            extra={"source_id": source_id, "run_id": record.get("id"), "error": repr(e)},
        )
        _discard(st, targets, source_id, str(record.get("id")))
        result.paths.pop("normalized", None)
        result.paths.pop("events", None)
        raise
    record["outputs"]["normalized"] = st.locate(result.paths["normalized"])
    if "events" in result.paths:
        record["outputs"]["events"] = st.locate(result.paths["events"])


# ------------------------------------------------------------------------ DQ hold release
class ReleaseRefused(Exception):
    """`release_held` will not promote this run. `code` is one of `not_found` (no run record with
    that id), `not_held` (the run did not end in a DQ hold), `superseded` (a later run produced
    output of its own; release that one instead, or let the next run stand) or `gated` (the
    source's reuse class is gated: nothing it fetches may reach a publishable path)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class ReleaseResult:
    run: dict[str, Any]
    ts: str
    #: True when the run record already carried a release: nothing was rewritten this time.
    already_released: bool
    paths: dict[str, pathlib.Path] = field(default_factory=dict)


def release_held(
    source_id: str,
    run_id: str,
    *,
    released_by: str,
    registry: Registry | None = None,
    store: Store | None = None,
    now: dt.datetime | None = None,
) -> ReleaseResult:
    """Promote the held output of run `run_id` to `normalized/` (+ `events/`), as though its DQ
    gates had passed, and rewrite its run record `ok` with a `release` block (`released_by`,
    `released_at`). Idempotent: a record that already carries a release is returned untouched.

    The record is found by id among the source's run records, not by timestamp, because the id is
    what the `source_run` row and the admin route carry. Raises `ReleaseRefused` (see its codes)
    and lets `StoreError` propagate after deleting any partial output, leaving the record held.
    """
    registry = registry or Registry()
    source = registry.get(source_id)
    if source.gated:
        raise ReleaseRefused(
            "gated", f"{source_id} is gated (reuse {source.reuse!r}); nothing is loaded from it"
        )
    st = store if store is not None else open_store()

    entries = st._run_entries(source_id)
    index = next((i for i, (_, r) in enumerate(entries) if str(r.get("id")) == run_id), None)
    if index is None:
        raise ReleaseRefused("not_found", f"no run record {run_id} for {source_id}")
    ts, record = entries[index]
    record = dict(record)
    if record.get("release") and record.get("status") == "ok":
        return ReleaseResult(run=record, ts=ts, already_released=True)
    if record.get("status") != "partial" or not (record.get("outputs") or {}).get("held"):
        raise ReleaseRefused("not_held", f"run {run_id} ended {record.get('status')!r}, not in a DQ hold")
    later = [r.get("id") for _, r in entries[index + 1 :] if r.get("status") in ("ok", "partial")]
    if later:
        raise ReleaseRefused(
            "superseded",
            f"run {run_id} is superseded by later run(s) with output: {', '.join(map(str, later))}",
        )

    held_path = st.held_path(source_id, ts)
    if not st.exists(held_path):
        raise ReleaseRefused(
            "not_held", f"the held output of run {run_id} is missing ({st.locate(held_path)})"
        )
    df = st.read_parquet(held_path)
    prev_df, prev_run_id = st.previous_normalized(source_id)
    if prev_df is not None:
        prev_df = align_previous_keys(prev_df, df)
    observed_at = str((record.get("snapshot") or {}).get("retrieved_at") or record.get("started_at"))
    record.setdefault("outputs", {})
    result = RunResult(run=record, records=df)
    _diff_and_store(st, source_id, ts, df, prev_df, observed_at, record, result)
    if record.get("snapshot") is not None:
        record["snapshot"]["previous_run_id"] = prev_run_id
    released_at = (now or utcnow()).isoformat()
    record["status"] = "ok"
    record["release"] = {"released_by": released_by, "released_at": released_at, "held_status": "partial"}
    # The run record is the commit marker (module docstring): written last, after the outputs.
    result.paths["run"] = st.write_run(source_id, ts, record)
    log.info(
        "held run released",
        extra={"source_id": source_id, "run_id": run_id, "rows_new": record["rows_new"], "ts": ts},
    )
    return ReleaseResult(run=record, ts=ts, already_released=False, paths=result.paths)


__all__ = [
    "RUN_TRIGGERS",
    "GateViolation",
    "ReleaseRefused",
    "ReleaseResult",
    "RunResult",
    "release_held",
    "run",
]
