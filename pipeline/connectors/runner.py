"""One source run: fetch -> snapshot -> parse -> normalise -> DQ gates -> diff -> store
(docs/20 §3.1–3.4, docs/21 §4.2 `source_run`, docs/04 DA-6).

Stage rules: every stage writes its output before the next starts; a failure fails closed and is
recorded on the run; a DQ hold keeps the raw snapshot (evidence) and writes nothing publishable;
a gated source (reuse restricted/unknown) is refused unless `allow_restricted=True` and is then
routed to the quarantine store, which cannot write to a publishable path.
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
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import QuarantineStore, Store, ts_token
from pipeline.diff import EVENT_TYPES, diff_snapshots

log = logging.getLogger("pipeline.connectors")

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
    registry = registry or Registry()
    source = registry.get(source_id)
    connector = registry.instantiate(source_id, allow_restricted=allow_restricted, http=http)
    base_store = store or Store()
    if source.gated:
        # allow_restricted=True got us here: outputs are quarantined, never publishable.
        st: Store = QuarantineStore(base_store.root)
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
    path = st.write_snapshot(source_id, ts, snap.ext, content)
    record["snapshot"]["object_key"] = str(path)
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
        result.paths["held"] = st.write_parquet(st.held_path(source_id, ts), df)
        record["outputs"]["held"] = str(result.paths["held"])
        return finish("partial")

    # 5. diff against the previous normalised snapshot ---------------------
    if prev_df is not None:
        events = diff_snapshots(_diff_view(prev_df), _diff_view(df), observed_at=snap.retrieved_at_iso)
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

    # 6. store (publishable outputs only from a publishable store) ---------
    result.paths["normalized"] = st.write_parquet(st.normalized_path(source_id, ts), df)
    record["outputs"]["normalized"] = str(result.paths["normalized"])
    if len(events):
        ev = events.copy()
        ev["event_type"] = ev["event_type"].astype(str)
        result.paths["events"] = st.write_parquet(st.events_path(source_id, ts), ev)
        record["outputs"]["events"] = str(result.paths["events"])
    return finish("ok")


__all__ = ["GateViolation", "RunResult", "run"]
