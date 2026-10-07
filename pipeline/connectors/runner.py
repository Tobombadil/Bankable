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

`run_id` lets the caller name the run (2026-09-27): the admin "run now" route creates the
`source_run` row the operator sees before the job starts, and the scheduler passes that row's id
down through the CLI (`--run-id`), so the record, and the row the scheduler completes from it, is
that same run rather than a second one. Without it a fresh id is generated, as before. A value
that is not a UUID is refused before any I/O.

A status-map correction is not a real-world change (2026-09-30, docs/22 §8.1). Before the diff, the
previous normalised snapshot is restated under the current status map from each row's own `raw`
payload (`Connector.restate_status`), so the diff sees only what the source changed. Rows the new
map places differently are counted on the run record (`reclassified`) and as an `info` DQ check
(`status_reclassified`, persisted on `source_run.dq`), and emit no `status_change` event; the
loader then writes the corrected state onto the stored record as an ordinary field update.
An opportunity's `open` is re-evaluated against its `due_at` at every run (2026-10-06, audit
2026-09-30 data engineer F4 / market M-7): after an incremental source's earlier rows are carried
forward, every `open` row whose deadline is before this run's `retrieved_at` becomes `closed`
(`pipeline.connectors.opportunity.close_past_deadline`, docs/21 §7.2). That is a real lifecycle
change, so the diff publishes it as a `status_change` (loaded as a `closed` event), and the run
records an `info` check (`deadline_closed`).

A capacity-rule correction is handled like a status-map correction (2026-10-06, NESO stages): a connector
that implements `Connector.restate_capacity` has the previous frame's `capacity_mw` recomputed from
`raw` before the diff, the moved rows are counted as `reclassified.capacity_rows` and an `info`
check (`capacity_restated`), and no `capacity_change` event is emitted for them.

Incremental windows are anchored (2026-10-07, audit 2026-09-30 F3). Before `fetch()` the runner
sets `connector.watermark` to the `retrieved_at` of the last *promoted* run (`Store.last_promoted`:
status `ok` with a normalised output, a released hold included), and an incremental connector asks
upstream for everything since the start of that day minus its overlap (`Connector.fetch_window`).
A run after an outage therefore catches up, a held run leaves the watermark where it was, and a
run right after the last one asks only for what is new. A fetch that stops at its page cap marks
the snapshot `truncated` and is held. The window is recorded on the snapshot's `meta` and as an
`info` DQ check (`fetch_window`; `warn` when a gap longer than `max_catchup_days` was clamped).

A parser change is a restatement, not news (2026-10-07, audit F10). The run record names the
connector's effective parser version (`Connector.effective_parser_version`: the declared version
plus a digest of the parser's code and status map). When it differs from the previous promoted
run's, that run's output is first re-derived under the current code: a full-register source's
stored snapshot is parsed and normalised again, an incremental source's stored rows are normalised
again from their own `raw` payloads, so the diff sees only what the source changed. The run records
`parser_restated` (the versions and how many events the restatement kept out of the feed) and an
`info` check of the same name. The unchanged short-circuit compares the SHA-256 *and* the parser
version, so new code reaches an unchanged source on its next tick; `reparse=True` (CLI
`run --reparse`) does the same from the stored snapshot without fetching at all.

`release_held` is the other way a run's output reaches `normalized/`: an operator accepted a
data-quality hold (`POST /admin/v1/source-runs/{run_id}/release`), so the held frame is diffed
against the previous normalised snapshot and written exactly as step 6 of `run` would have, the
run record is rewritten `ok` with a `release` block, and the caller enqueues the load. It refuses
a run that is not held, one a later run has superseded, and any gated source (the same gate the
runner and the loader apply; a release never makes quarantined output publishable).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from pipeline.connectors.base import (
    BlockedError,
    Connector,
    ConnectorError,
    GateViolation,
    RawSnapshot,
    utcnow,
)
from pipeline.connectors.dedupe import align_previous_keys
from pipeline.connectors.dq import Check, DQResult, daily_profile, run_gates
from pipeline.connectors.http import HttpBlocked, HttpFailed, PoliteSession
from pipeline.connectors.objectstore import StoreError
from pipeline.connectors.opportunity import close_past_deadline
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import QuarantineStore, Store, open_store, ts_token
from pipeline.diff import CAP_ABS_MW, CAP_REL, EVENT_TYPES, diff_snapshots

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


def _restate_previous(connector: Connector, prev_df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """The previous snapshot as the current status map and capacity rule read it
    (`Connector.restate_status`, `Connector.restate_capacity`), and the reclassification summary
    for the run record: how many stored rows change state, by `before->after` transition, and how
    many change `capacity_mw` (`capacity_rows`). Diffing against the restated frame is what keeps a
    mapping correction from being published as a real-world `status_change` or `capacity_change`
    (module docstring)."""
    summary: dict[str, Any] = {"rows": 0, "transitions": {}}
    out, capacity_rows = _restate_capacity(connector, prev_df)
    if capacity_rows:
        summary["capacity_rows"] = capacity_rows
    if "lifecycle_state" not in out.columns:
        return out, summary
    restated = connector.restate_status(out)
    if restated is None:
        return out, summary
    before = out["lifecycle_state"].astype("string").fillna("").to_numpy()
    after = restated["lifecycle_state"].astype("string").fillna("").to_numpy()
    changed = before != after
    if not changed.any():
        return out, summary
    out = out.copy()
    out["lifecycle_state"] = out["lifecycle_state"].astype("object")
    out.loc[changed, "lifecycle_state"] = restated["lifecycle_state"].to_numpy()[changed]
    if "status_rule" in out.columns and "status_rule" in restated.columns:
        out["status_rule"] = out["status_rule"].astype("object")
        out.loc[changed, "status_rule"] = restated["status_rule"].to_numpy()[changed]
    pairs = pd.Series([f"{b}->{a}" for b, a in zip(before[changed], after[changed], strict=True)])
    transitions = {str(k): int(v) for k, v in pairs.value_counts().items()}
    summary["rows"] = int(changed.sum())
    summary["transitions"] = transitions
    log.info(
        "previous snapshot restated under the current status map",
        extra={"source_id": connector.source_id, "rows": summary["rows"]},
    )
    return out, summary


def _restate_capacity(connector: Connector, prev_df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """`prev_df` with `capacity_mw` recomputed by the connector's current capacity rule, and how
    many rows the diff would otherwise have published as a `capacity_change`. A row the
    restatement cannot read (null) keeps its stored value."""
    if "capacity_mw" not in prev_df.columns or "raw" not in prev_df.columns:
        return prev_df, 0
    restated = connector.restate_capacity(prev_df)
    if restated is None:
        return prev_df, 0
    before = pd.to_numeric(prev_df["capacity_mw"], errors="coerce").astype(float).to_numpy()
    after = pd.to_numeric(restated, errors="coerce").astype(float).to_numpy()
    after = np.where(np.isnan(after), before, after)
    with np.errstate(invalid="ignore", divide="ignore"):
        delta = np.abs(after - before)
        rel = delta / np.maximum(np.abs(before), np.abs(after))
    moved = ((delta > CAP_ABS_MW) & (rel > CAP_REL)) | (np.isnan(before) ^ np.isnan(after))
    if not moved.any():
        return prev_df, 0
    out = prev_df.copy()
    out["capacity_mw"] = pd.array(after, dtype="Float64")
    n = int(moved.sum())
    log.info(
        "previous snapshot restated under the current capacity rule",
        extra={"source_id": connector.source_id, "rows": n},
    )
    return out, n


def _reclassified_check(summary: dict[str, Any]) -> Check:
    return Check(
        "status_reclassified",
        "info",
        f"{summary['rows']} stored rows restated under the current status map; no status_change "
        "events emitted for them",
        {"rows": summary["rows"], "transitions": summary["transitions"]},
    )


def _capacity_restated_check(rows: int) -> Check:
    return Check(
        "capacity_restated",
        "info",
        f"{rows} stored rows restated under the current capacity rule; no capacity_change events "
        "emitted for them",
        {"rows": rows},
    )


def _deadline_closed_check(rows: int) -> Check:
    return Check(
        "deadline_closed",
        "info",
        f"{rows} open opportunities past their due_at at this run's retrieval time set to closed",
        {"rows": rows},
    )


def _parse_ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _promoted_watermark(record: dict[str, Any] | None) -> dt.datetime | None:
    """The `retrieved_at` of a promoted run's snapshot: the instant up to which its output is known
    complete, and what an incremental connector anchors its next window on."""
    if not record:
        return None
    snap = record.get("snapshot") or {}
    return _parse_ts(snap.get("retrieved_at")) or _parse_ts(record.get("started_at"))


def _snapshot_from_record(record: dict[str, Any], content: bytes) -> RawSnapshot:
    snap = record.get("snapshot") or {}
    retrieved = _promoted_watermark(record) or utcnow()
    ext = str(snap.get("object_key") or "").rsplit("/", 1)[-1].rpartition(".")[2] or "bin"
    meta = snap.get("meta")
    return RawSnapshot(
        content=content,
        content_type=str(snap.get("content_type") or "application/octet-stream"),
        url=str(snap.get("fetched_url") or ""),
        retrieved_at=retrieved,
        http_status=int(snap.get("http_status") or 200),
        ext=ext,
        meta=dict(meta) if isinstance(meta, dict) else {},
    )


def _reparse_previous(
    connector: Connector, st: Store, source_id: str, ts: str, record: dict[str, Any], prev_df: pd.DataFrame
) -> pd.DataFrame | None:
    """The previous promoted output re-derived under the current parser (module docstring): a
    full-register source from its stored snapshot, an incremental source from each stored row's own
    `raw` payload, grouped by the fetch it came from. None when it cannot be re-derived (the
    snapshot is gone, or the current code cannot read the old bytes)."""
    try:
        if connector.snapshot_mode == "full":
            body = st.snapshot_bytes(source_id, ts, record)
            if body is None:
                return None
            snap = _snapshot_from_record(record, body)
            out = connector.normalize(connector.parse(snap), snap)
        else:
            frames = []
            for retrieved_at, group in prev_df.groupby("retrieved_at", sort=True, dropna=False):
                rows = [json.loads(str(v)) for v in group["raw"]]
                stub = RawSnapshot(
                    content=b"",
                    content_type="application/json",
                    url=str(group["source_url"].iloc[0]),
                    retrieved_at=_parse_ts(retrieved_at) or utcnow(),
                    http_status=200,
                    ext=connector.ext,
                )
                frames.append(connector.normalize(rows, stub))
            out = pd.concat(frames, ignore_index=True) if frames else prev_df.iloc[0:0]
        if connector.kind == "opportunity":
            out, _ = close_past_deadline(out, _promoted_watermark(record) or utcnow())
        return out
    except GateViolation:
        raise
    except Exception:
        log.warning(
            "previous output could not be restated under the current parser",
            extra={"source_id": source_id, "ts": ts},
            exc_info=True,
        )
        return None


def _previous_for_diff(
    connector: Connector,
    st: Store,
    source_id: str,
    df: pd.DataFrame,
    parser_version: str,
    record: dict[str, Any],
) -> tuple[pd.DataFrame | None, str | None]:
    """The frame this run diffs against: the last promoted output, restated under the current
    parser when the parser changed, keys aligned, and restated under the current status map and
    capacity rule. Writes the restatement summaries onto `record`."""
    promoted = st.last_promoted(source_id)
    if promoted is None:
        return None, None
    prev_ts, prev_record = promoted
    prev_df = st.read_parquet(st.normalized_path(source_id, prev_ts))
    prev_run_id = str(prev_record.get("id"))
    prev_version = str(prev_record.get("parser_version") or "")
    if prev_version != parser_version and "raw" in prev_df.columns:
        restated = _reparse_previous(connector, st, source_id, prev_ts, prev_record, prev_df)
        summary: dict[str, Any] = {"from": prev_version, "to": parser_version}
        if restated is None:
            summary["restated"] = False
        else:
            suppressed = diff_snapshots(
                _diff_view(prev_df), _diff_view(align_previous_keys(restated, prev_df))
            )
            summary["restated"] = True
            summary["events_suppressed"] = len(suppressed)
            summary["by_type"] = {
                str(k): int(v) for k, v in suppressed["event_type"].value_counts().items() if v
            }
            prev_df = restated
        record["parser_restated"] = summary
    # Legacy positional `#N` keys, and unique <-> duplicated transitions, are matched to the
    # current content keys before anything is compared (pipeline/connectors/dedupe.py).
    prev_df = align_previous_keys(prev_df, df)
    if (record.get("parser_restated") or {}).get("restated"):
        # Re-derived by the current code end to end, status map and capacity rule included.
        return prev_df, prev_run_id
    prev_df, reclassified = _restate_previous(connector, prev_df)
    record["rows_reclassified"] = reclassified["rows"]
    record["reclassified"] = reclassified
    return prev_df, prev_run_id


def _parser_restated_check(summary: dict[str, Any]) -> Check:
    if not summary.get("restated"):
        return Check(
            "parser_restated",
            "warn",
            f"parser changed ({summary['from']} -> {summary['to']}) but the previous output could not be "
            "re-derived; the diff compares against it as stored",
            summary,
        )
    return Check(
        "parser_restated",
        "info",
        f"previous output restated under parser {summary['to']}; {summary['events_suppressed']} "
        "change events from the parser change were not emitted",
        summary,
    )


def _window_check(meta: dict[str, Any]) -> Check:
    anchor = str(meta.get("window_anchor"))
    return Check(
        "fetch_window",
        "warn" if anchor == "clamped" else "info",
        f"{meta.get('window_start')} .. {meta.get('window_end')} ({anchor}"
        + (f", watermark {meta.get('watermark')}" if meta.get("watermark") else "")
        + (
            "; the gap was longer than the catch-up limit and its start was not fetched"
            if anchor == "clamped"
            else ""
        )
        + ")",
        {k: meta.get(k) for k in ("window_start", "window_end", "window_anchor", "watermark")},
    )


def _daily_counts(
    connector: Connector, df: pd.DataFrame, rows: list[dict[str, Any]], snap: RawSnapshot
) -> dict[str, int] | None:
    """Rows per complete UTC day of this run's fetch window (dq `daily_profile`), or None when the
    connector declares no publication date."""
    dates = connector.row_dates(df, rows)
    if dates is None:
        return None
    start = _parse_ts(snap.meta.get("window_start"))
    end = _parse_ts(snap.meta.get("window_end")) or snap.retrieved_at
    if start is None:
        start = snap.retrieved_at - dt.timedelta(days=connector.window_days or 1)
    return daily_profile(dates, start, end)


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
    run_id: str | None = None,
    http: PoliteSession | None = None,
    raw: RawSnapshot | None = None,
    now: dt.datetime | None = None,
    reparse: bool = False,
) -> RunResult:
    """Run one connector end to end and persist everything the run produced.

    `raw` injects a recorded snapshot (tests, replays) so `fetch()` is skipped. `now` fixes the
    run's clock (the connector's `now()` too). `reparse` skips the fetch and runs the latest stored
    snapshot through the current parser (module docstring); it fails when there is none.
    Raises `GateViolation` before any I/O when the source is gated and the flag is absent.
    """
    if trigger not in RUN_TRIGGERS:
        raise ValueError(f"trigger must be one of {RUN_TRIGGERS}, got {trigger!r}")
    if run_id is not None:
        try:
            run_id = str(uuid.UUID(run_id))
        except ValueError as e:
            raise ValueError(f"run_id must be a UUID, got {run_id!r}") from e
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
    run_id = run_id or str(uuid.uuid4())
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
        "parser_version": f"{source_id}@{connector.effective_parser_version()}",
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
        "rows_reclassified": 0,
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

    if now is not None:
        connector.clock = lambda: now
    # 1. fetch ------------------------------------------------------------
    reused_key: str | None = None
    try:
        promoted = st.last_promoted(source_id)
    except StoreError as e:
        log.warning("promoted run unavailable", extra={"source_id": source_id, "error": repr(e)})
        promoted = None
    connector.watermark = _promoted_watermark(promoted[1] if promoted else None)
    if raw is None:
        # The snapshot step 2 compares against, handed over first so a connector that can ask
        # upstream "changed since?" returns these same bytes on a 304 (docs/20 §3.2).
        try:
            connector.previous = st.last_snapshot(source_id)
        except StoreError as e:
            log.warning("previous snapshot unavailable", extra={"source_id": source_id, "error": repr(e)})
    if reparse and raw is None:
        entry = st._last_snapshot_entry(source_id)
        body = st.snapshot_bytes(source_id, entry[0], entry[1]) if entry else None
        if entry is None or body is None:
            return finish("failed", ConnectorError("reparse: no stored snapshot to read"))
        raw = _snapshot_from_record(entry[1], body)
        reused_key = (entry[1].get("snapshot") or {}).get("object_key")
        record["_ts"] = ts_token(started)
        record["reparse"] = {"of_run": entry[1].get("id"), "of_ts": entry[0]}
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
    ts = record.get("_ts") or ts_token(snap.retrieved_at)
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
    # Same bytes *and* the same parser: nothing new can come out of this run. Same bytes under a
    # changed parser go on, so a parser fix reaches an unchanged source (module docstring).
    last = st._last_snapshot_entry(source_id)
    last_version = (
        str((last[1].get("snapshot") or {}).get("parser_version") or last[1].get("parser_version") or "")
        if last
        else ""
    )
    if (
        not reparse
        and last is not None
        and str(last[1]["snapshot"]["sha256"]) == snap.sha256
        and last_version == record["parser_version"]
    ):
        return finish("unchanged")
    if reused_key:
        record["snapshot"]["object_key"] = reused_key
    else:
        try:
            path = st.write_snapshot(source_id, ts, snap.ext, content)
        except StoreError as e:
            log.error(
                "snapshot write failed", extra={"source_id": source_id, "run_id": run_id, "error": repr(e)}
            )
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
    header = snap.meta.get("source_header")
    source_columns = (
        [str(c) for c in header] if isinstance(header, list) and header else _source_columns(rows)
    )
    daily_counts = (
        _daily_counts(connector, df, rows, snap) if connector.snapshot_mode == "incremental" else None
    )
    fetched_df = df

    prev_df, prev_run_id = _previous_for_diff(connector, st, source_id, df, record["parser_version"], record)
    if connector.snapshot_mode == "incremental" and prev_df is not None:
        keep = prev_df[~prev_df["record_id"].isin(df["record_id"])]
        df = pd.concat([keep[df.columns.intersection(keep.columns)], df], ignore_index=True)
    deadline_closed = 0
    if connector.kind == "opportunity":
        # Carried-forward rows were last evaluated when they were fetched (module docstring).
        df, deadline_closed = close_past_deadline(df, snap.retrieved_at)
    record["rows_seen"] = len(df)
    record["snapshot"]["previous_run_id"] = prev_run_id

    # 4. DQ gates ---------------------------------------------------------
    window_start = _parse_ts(snap.meta.get("window_start"))
    thresholds = source.raw.get("dq_thresholds")
    dq = run_gates(
        df,
        connector.kind,
        source_columns,
        st.dq_history(source_id),
        thresholds=thresholds if isinstance(thresholds, dict) else None,
        required=connector.dq_required_fields,
        key_source_columns=connector.key_source_columns,
        duplicates_resolved=int(df.attrs.get("duplicates_resolved", 0)),
        rows_fetched=record["rows_fetched"],
        daily_counts=daily_counts,
        incremental=connector.snapshot_mode == "incremental",
        may_be_empty=connector.may_be_empty,
        window_days=((snap.retrieved_at - window_start).total_seconds() / 86400.0 if window_start else None),
        previous=prev_df,
        fetched=fetched_df,
        truncated=bool(snap.meta.get("truncated")),
    )
    if snap.meta.get("window_anchor"):
        dq.checks.append(_window_check(snap.meta))
    if record.get("parser_restated"):
        dq.checks.append(_parser_restated_check(record["parser_restated"]))
        if not record["parser_restated"].get("restated") and dq.status == "pass":
            dq.status = "warn"
    if snap.meta.get("window_anchor") == "clamped" and dq.status == "pass":
        dq.status = "warn"
    if record["rows_reclassified"]:
        dq.checks.append(_reclassified_check(record["reclassified"]))
    if deadline_closed:
        dq.checks.append(_deadline_closed_check(deadline_closed))
    if record.get("reclassified", {}).get("capacity_rows"):
        dq.checks.append(_capacity_restated_check(record["reclassified"]["capacity_rows"]))
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
    connector = registry.instantiate(source_id)
    prev_df, prev_run_id = _previous_for_diff(
        connector, st, source_id, df, str(record.get("parser_version") or ""), record
    )
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
