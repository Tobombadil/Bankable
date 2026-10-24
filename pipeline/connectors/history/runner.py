"""One history run: gate -> plan -> for each item: fetch (or read stored bytes) -> snapshot -> summarise
-> DQ -> the month's document -> run record (`docs/24-trends.md` §9.3).

* **Gate.** `base.gate`: a source whose rows could never be published under the platform posture
  is not fetched at all (CLAUDE.md), nor a `status: pending` or `excluded` row.
* **Idempotent.** A month whose document already holds the same bytes under the same parser is
  `unchanged` and not even parsed again; a file the store already holds (the shared snapshot of
  another connector, this source's own last fetch answered 304) is read, never downloaded twice.
  Running a backfill twice writes nothing the second time.
* **Resumable.** Every month is committed on its own: its bytes stored, then its document written
  atomically. A run killed half way keeps the months it finished, and the next run plans only
  what is still missing. `auto` mode runs `backfill` until a backfill has ended `ok` or
  `unchanged` with the same `backfill_key` (EIA's mirror record, when one is switched on), then
  `monthly`.
* **A failed fetch keeps the last good data.** Nothing here deletes a document. A month is replaced
  only by a successful parse of a copy at least as authoritative (`base.ROUTE_RANK`: the
  publisher's own server, then a person's download, then a mirror), or by a re-release or a
  correction from the same route, or by a restatement under a new parser; a copy of the same file
  retrieved earlier than the one held never replaces it (`kept_copy`). A reading its DQ checks
  hold never replaces a served month either: it is kept beside it (`MonthlyStore.write_pending`)
  until an operator releases it, and the same file is then neither fetched nor parsed again. A
  file whose bytes are already another month's served document is held (`duplicate_file`). An
  item that failed in `MAX_ITEM_FAILURES` consecutive runs is skipped (`skipped_repeated_failure`)
  until an operator passes `--retry-failed`, so a broken file is not downloaded again every week;
  the publisher's index is never skipped, since its file changes every month.
* **Bounded memory.** A workbook of `ISOLATE_BYTES` or more is summarised in a forked child process
  that returns only the tallies: openpyxl leaves the heap fragmented, and an out-of-memory kill then
  takes the child, not the run (the month is recorded failed).
* **Status** uses `source_run`'s vocabulary: `ok` (a month written), `unchanged`, `partial` (some
  item failed, a route could not be planned, or a month was held by its DQ checks), `failed`,
  `blocked` (robots.txt or a challenge on every item). The run record is written last, as the
  record runner's is, and the CLI prints the `result` line `infra/scheduler/jobs.py::fetch_outcome`
  reads.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import multiprocessing
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pipeline.connectors.base import ConnectorError, ParseError, utcnow
from pipeline.connectors.history import connector_class_for
from pipeline.connectors.history import dq as history_dq
from pipeline.connectors.history.base import (
    MODES,
    ROUTE_RANK,
    HeldPeriod,
    HistoryConnector,
    MonthFile,
    gate,
    parse_time,
)
from pipeline.connectors.history.documents import (
    DOCUMENT_KIND,
    SCHEMA_VERSION,
    MonthlyStore,
    is_held,
    shift_period,
)
from pipeline.connectors.http import HttpBlocked, HttpFailed, PoliteSession
from pipeline.connectors.objectstore import StoreError
from pipeline.connectors.registry import PUBLISHABLE_REUSE, Registry
from pipeline.connectors.store import Store, ts_token

log = logging.getLogger("pipeline.connectors.history")

TRIGGERS = ("schedule", "manual", "backfill", "retry")
RUN_MODES = (*MODES, "auto")
MAX_ITEM_FAILURES = 3
#: A workbook this large or larger is summarised in a child process (module docstring): every
#: real EIA-860M month (2.7 to 14 MB) and no recorded fixture.
ISOLATE_BYTES = 2_000_000
FAILED = ("failed", "blocked")


@dataclass
class RunResult:
    record: dict[str, Any]
    run_path: Path


def held_periods(monthly: MonthlyStore, source_id: str) -> dict[str, HeldPeriod]:
    """What the store holds for `source_id`, by month: the month's document, and a held reading
    kept beside it (`pending_*`)."""
    out: dict[str, HeldPeriod] = {}
    for doc in monthly.documents(source_id):
        came = doc.get("retrieved_from") or {}
        raw = doc.get("raw") or {}
        out[str(doc["period"])] = HeldPeriod(
            period=str(doc["period"]),
            route=str(came.get("route") or "origin"),
            sha256=str(raw.get("sha256") or ""),
            retrieved_at=str(doc.get("retrieved_at") or ""),
            parser_version=str(doc.get("parser_version") or ""),
            key=str(came.get("item") or ""),
            snapshot_key=raw.get("snapshot"),
            held=is_held(doc),
            source_url=str(doc.get("source_url") or ""),
            file_id=str(doc["file_id"]) if doc.get("file_id") else None,
        )
    for doc in monthly.pending(source_id):
        current = out.get(str(doc["period"]))
        if current is None:
            continue  # a held reading beside nothing: the next reading of the month decides
        out[current.period] = replace(
            current,
            pending_sha256=str((doc.get("raw") or {}).get("sha256") or "") or None,
            pending_parser_version=str(doc.get("parser_version") or "") or None,
            pending_file_id=str(doc["file_id"]) if doc.get("file_id") else None,
        )
    return out


def effective_mode(
    mode: str, previous_runs: Sequence[Mapping[str, Any]], backfill_key: str | None = None
) -> str:
    """`auto` is `backfill` until a backfill has ended `ok` or `unchanged` with the same
    `backfill_key` (what it read beyond the standing routes: EIA's mirror record), then `monthly`.
    So a mirror switched on, or moved to a newer record, is backfilled from without an operator."""
    if mode != "auto":
        return mode
    done = any(
        r.get("mode") == "backfill"
        and r.get("status") in ("ok", "unchanged")
        and r.get("backfill_key") == backfill_key
        for r in previous_runs
    )
    return "monthly" if done else "backfill"


def kept_copy(current: HeldPeriod | None, month: MonthFile, route: str, file_id: str | None) -> str | None:
    """Why the month's held document stays when `month` (arriving by `route`) is another copy of it,
    or None when `month` may replace it.

    A copy from a less authoritative route never replaces one from a more authoritative route
    (`ROUTE_RANK`). A copy of the same file from an equally authoritative route that was retrieved
    earlier never replaces one retrieved later: an older stored snapshot of a month the publisher
    has since re-issued (the register connector's copy of EIA's first September file, after this
    source fetched the corrected one) would otherwise put the old figures back every week. A
    restatement re-reads the held copy itself, and another file of the publisher's (ERCOT's
    corrected re-issue, under a new DocID) is the publisher's own choice of the month's document;
    neither is held back."""
    if current is None or month.route == "restate":
        return None
    held_rank, rank = ROUTE_RANK.get(current.route, 0), ROUTE_RANK.get(route, 0)
    if held_rank > rank:
        return current.route
    if held_rank == rank and current.file_id == file_id:
        try:
            held_at = parse_time(current.retrieved_at)
        except ValueError:
            return None
        if month.retrieved_at < held_at:
            return f"{current.route}, retrieved later"
    return None


def consecutive_failures(previous_runs: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Per item key, how many of the latest runs that tried it failed it in a row."""
    out: dict[str, int] = {}
    settled: set[str] = set()
    for record in reversed(previous_runs):
        for entry in record.get("items") or []:
            key = str(entry.get("key") or "")
            if not key or key in settled:
                continue
            if entry.get("status") in FAILED:
                out[key] = out.get(key, 0) + 1
            elif entry.get("status") != "skipped_repeated_failure":
                settled.add(key)
    return out


# ----------------------------------------------------------------------------- child processes
def _child(conn: Any, connector: HistoryConnector, month: MonthFile) -> None:
    try:
        conn.send(("ok", connector.summarise(month)))
    except BaseException as exc:  # everything goes back to the parent, which records it
        conn.send(("err", (type(exc).__name__, str(exc)[:2000])))
    finally:
        conn.close()


def summarise(connector: HistoryConnector, month: MonthFile, *, isolate_bytes: int | None) -> dict[str, Any]:
    """`connector.summarise(month)`, in a forked child when the file is large (module docstring)."""
    if (
        isolate_bytes is None
        or len(month.content) < isolate_bytes
        or "fork" not in (multiprocessing.get_all_start_methods())
    ):
        return connector.summarise(month)
    ctx = multiprocessing.get_context("fork")
    receive, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(send, connector, month))
    proc.start()
    send.close()
    try:
        payload = receive.recv()
    except EOFError:
        payload = None
    finally:
        receive.close()
        proc.join()
    if payload is None:
        raise ConnectorError(f"the summarising process ended without a result (exit {proc.exitcode})")
    kind, value = payload
    if kind == "ok":
        result: dict[str, Any] = value
        return result
    name, message = value
    if name == "ParseError":
        raise ParseError(message)
    raise ConnectorError(f"{name}: {message}")


# ----------------------------------------------------------------------------------------- run
def run(
    source_id: str,
    mode: str,
    *,
    store: Store,
    trigger: str = "manual",
    run_id: str | None = None,
    http: PoliteSession | None = None,
    registry: Registry | None = None,
    now: Callable[[], dt.datetime] | None = None,
    retry_failed: bool = False,
    isolate_bytes: int | None = ISOLATE_BYTES,
) -> RunResult:
    if mode not in RUN_MODES:
        raise ValueError(f"mode {mode!r}: expected one of {', '.join(RUN_MODES)}")
    entry = (registry or Registry()).get(source_id)
    gate(entry, PUBLISHABLE_REUSE)
    cls = connector_class_for(source_id)
    clock = now or utcnow
    started = clock()
    t0 = time.monotonic()
    ts = ts_token(started)
    previous_runs = store.runs(source_id)
    connector = cls(entry, http, store=store, clock=clock, previous_runs=previous_runs)
    backfill_key = connector.backfill_key()
    actual = effective_mode(mode, previous_runs, backfill_key)
    monthly = MonthlyStore(store.base_root)
    held = held_periods(monthly, source_id)
    record: dict[str, Any] = {
        "id": run_id or str(uuid.uuid4()),
        "source_id": source_id,
        "runner": "history",
        "mode": actual,
        "requested_mode": mode,
        "backfill_key": backfill_key,
        "trigger": trigger,
        "started_at": started.isoformat(),
        "parser_version": cls.version(),
        "licence_id": entry.licence_id,
        "egress_class": "plain",
        "items": [],
        "documents": {"written": [], "replaced": [], "unchanged": [], "skipped": [], "held": []},
        #: Months a held reading waits on an operator for (`release`), whatever this run did.
        "awaiting_release": [],
        "plan_errors": [],
        "bytes": 0,
        "rows_seen": 0,
        "rows_new": 0,
        "rows_changed": 0,
        "rows_gone": 0,
        "events_emitted": 0,
        "dq_status": None,
    }

    def finish(status: str, error: BaseException | None = None) -> RunResult:
        record["status"] = status
        record["finished_at"] = clock().isoformat()
        record["worker_seconds"] = round(time.monotonic() - t0, 2)
        record["requests"] = connector.http.requests_made
        if error is not None:
            record["error"] = repr(error)[:500]
            record["error_class"] = type(error).__name__
        path = store.write_run(source_id, ts, record)
        log.info(
            "history run finished",
            extra={"source_id": source_id, "run_id": record["id"], "status": status, "mode": actual},
        )
        return RunResult(record=record, run_path=path)

    try:
        items = connector.plan(actual, held)
    except HttpBlocked as exc:
        return finish("blocked", exc)
    except (ConnectorError, HttpFailed, StoreError, OSError) as exc:
        return finish("failed", exc)
    record["plan_errors"] = list(connector.plan_errors)
    failures = consecutive_failures(previous_runs)
    dq_levels: set[str] = set()
    counter = 0

    def remember_origin(month: MonthFile, snapshot_key: str | None, sha: str) -> None:
        """The publisher's validators for its current file, on every run that read it (written,
        replaced or unchanged), so the next run's request is conditional on the newest ones."""
        if month.route == "origin" and month.validators and snapshot_key:
            record["origin"] = {
                "url": month.source_url,
                "snapshot_key": snapshot_key,
                "sha256": sha,
                **month.validators,
            }

    def same_reading(h: HeldPeriod, sha: str, version: str, file_id: str | None) -> str | None:
        """`document` when `h`'s served document is these bytes under this parser and file,
        `held` when the reading held beside it is; None otherwise."""
        if h.sha256 == sha and h.parser_version == version and h.file_id == file_id:
            return "document"
        if h.pending_sha256 == sha and h.pending_parser_version == version and h.pending_file_id == file_id:
            return "held"
        return None

    def unchanged(
        h: HeldPeriod, which: str, month: MonthFile, snapshot_key: str | None, sha: str, log: dict[str, Any]
    ) -> None:
        entry: dict[str, Any] = {"period": h.period, "outcome": "unchanged"}
        if which == "held":
            entry["held"] = True  # the same file its checks held: it still waits on an operator
        log["months"].append(entry)
        record["documents"]["unchanged"].append(h.period)
        remember_origin(month, snapshot_key, sha)

    def process(month: MonthFile, entry_log: dict[str, Any]) -> None:
        nonlocal counter
        sha = hashlib.sha256(month.content).hexdigest()
        version = cls.version()
        record["bytes"] += len(month.content) if month.snapshot_key is None else 0
        if month.route != "restate":
            # The same bytes under the same parser make the same document: not even parsed again.
            # Where the route names the month (a report's name, a file's), only that month's
            # document is compared, since ERCOT's month comes from the name, not the bytes.
            candidates = (
                ([held[month.period_hint]] if month.period_hint in held else [])
                if month.period_hint
                else list(held.values())
            )
            file_id = str(month.via["doc_id"]) if month.via.get("doc_id") else None
            for h in candidates:
                which = same_reading(h, sha, version, file_id)
                if which:
                    unchanged(h, which, month, month.snapshot_key or h.snapshot_key, sha, entry_log)
                    return
            hint = month.period_hint
            if hint:
                # A copy that would lose to the one held is neither stored nor parsed.
                kept = kept_copy(held.get(hint), month, month.record_route or month.route, file_id)
                if kept:
                    entry_log["months"].append({"period": hint, "outcome": "skipped", "kept": kept})
                    record["documents"]["skipped"].append(hint)
                    return
        snapshot_key = month.snapshot_key
        if snapshot_key is None:
            counter += 1
            label = f"{ts}.{counter:03d}" + (f"-{month.period_hint}" if month.period_hint else "")
            path = store.write_snapshot(source_id, label, "xlsx", month.content)
            snapshot_key = store.key(path)
        summary = summarise(connector, month, isolate_bytes=isolate_bytes)
        period = str(summary["period"])
        route = month.record_route or month.route
        record["rows_seen"] += 1
        current = held.get(period)
        file_id = str(summary["file_id"]) if summary.get("file_id") else None
        which = same_reading(current, sha, version, file_id) if current is not None else None
        if current is not None and which:
            unchanged(current, which, month, snapshot_key, sha, entry_log)
            return
        kept = kept_copy(current, month, route, file_id)
        if kept:
            entry_log["months"].append({"period": period, "outcome": "skipped", "kept": kept})
            record["documents"]["skipped"].append(period)
            return
        # The same bytes as another month's served document: a file posted under the wrong
        # month's name (ERCOT's names are typed by hand), never a second month of figures.
        twins = sorted(p for p, h in held.items() if p != period and not h.held and h.sha256 == sha)
        if twins:
            summary = {**summary, "dq_facts": {**(summary.get("dq_facts") or {}), "duplicate_file": twins}}
        previous = None
        before = shift_period(period, -1)
        if before in held and not held[before].held:
            try:
                previous = monthly.load(source_id, before)
            except (OSError, ValueError):
                previous = None
        dq = history_dq.check_month(summary, previous)
        via = dict(month.via)
        restating = month.route == "restate" and current is not None
        earlier_doc: Mapping[str, Any] = {}
        if restating:
            # A restatement re-reads the same bytes: the document keeps how they first arrived (the
            # mirror's DOI and file, the publisher's DocID and publication date, the HTTP status)
            # beside the snapshot read.
            try:
                earlier_doc = monthly.load(source_id, period)
            except (OSError, ValueError):
                earlier_doc = {}
            earlier = earlier_doc.get("retrieved_from") or {}
            via = {k: v for k, v in earlier.items() if k not in ("route", "item", "restated_from")} | via
        document = {
            "schema_version": SCHEMA_VERSION,
            "kind": DOCUMENT_KIND,
            "source_id": source_id,
            "period": period,
            "frequency": "monthly",
            "source_url": summary["source_url"],
            "archive_url": summary.get("archive_url"),
            "file_id": summary.get("file_id"),
            "published": summary.get("published") or earlier_doc.get("published"),
            "retrieved_at": month.retrieved_at_iso,
            "licence_id": entry.licence_id,
            "credit": entry.credit_text,
            "retrieved_from": {"route": route, "item": month.item_key, **via},
            "parser_version": version,
            "run_id": record["id"],
            "layout": summary.get("layout") or {},
            "blocks": summary["blocks"],
            "technology_labels": summary.get("technology_labels") or {},
            "dq": dq,
            "raw": {"snapshot": snapshot_key, "sha256": sha, "bytes": len(month.content)},
        }
        dq_levels.add(dq["status"])
        if dq["status"] == "held" and current is not None and not current.held:
            # Held, while the month has a served document: kept beside it, never over it, until
            # an operator releases it (`documents.MonthlyStore.write_pending`).
            monthly.write_pending(document)
            record["documents"]["held"].append(period)
            entry_log["months"].append({"period": period, "outcome": "held", "beside": True, "dq": "held"})
            held[period] = replace(
                current, pending_sha256=sha, pending_parser_version=version, pending_file_id=file_id
            )
            remember_origin(month, snapshot_key, sha)
            return
        monthly.write(document)
        keeps_pending = restating and current is not None and current.pending_sha256 is not None
        if current is not None and current.pending_sha256 is not None and not restating:
            # A newer file of the month passed its checks: the held reading it supersedes goes.
            monthly.discard_pending(source_id, period)
        outcome = "replaced" if current is not None else "written"
        record["documents"][outcome].append(period)
        if dq["status"] == "held":
            record["documents"]["held"].append(period)
        record["rows_new" if current is None else "rows_changed"] += 1
        entry_log["months"].append({"period": period, "outcome": outcome, "dq": dq["status"]})
        held[period] = HeldPeriod(
            period=period,
            route=route,
            sha256=sha,
            retrieved_at=month.retrieved_at_iso,
            parser_version=version,
            key=month.item_key,
            snapshot_key=snapshot_key,
            held=dq["status"] == "held",
            source_url=str(summary["source_url"]),
            file_id=file_id,
            pending_sha256=current.pending_sha256 if keeps_pending and current else None,
            pending_parser_version=current.pending_parser_version if keeps_pending and current else None,
            pending_file_id=current.pending_file_id if keeps_pending and current else None,
        )
        remember_origin(month, snapshot_key, sha)

    for item in items:
        entry_log: dict[str, Any] = {
            "key": item.key,
            "route": item.route,
            "url": item.url if item.route in ("origin", "mirror") else None,
            "periods": list(item.periods),
            "meta": {k: v for k, v in item.meta.items() if k in ("sha256", "name", "doc_id", "file")},
            "months": [],
        }
        record["items"].append(entry_log)
        # The publisher's index (an origin item naming no month, EIA's index page) is a standing
        # route whose file changes every month: it is never parked, and a run it fails says so.
        standing = item.route == "origin" and not item.periods
        if not retry_failed and not standing and failures.get(item.key, 0) >= MAX_ITEM_FAILURES:
            entry_log["status"] = "skipped_repeated_failure"
            continue
        try:
            month_failures = 0
            for month in connector.fetch(item):
                try:
                    process(month, entry_log)
                except (ConnectorError, StoreError, OSError, ValueError, KeyError) as exc:
                    # One unreadable month of a yearly zip does not cost the others; the item is
                    # still recorded failed, so a file that never parses stops being fetched.
                    month_failures += 1
                    entry_log["months"].append(
                        {"period": month.period_hint, "outcome": "failed", "error": repr(exc)[:300]}
                    )
            if month_failures:
                entry_log.update(status="failed", error=f"{month_failures} month(s) could not be read")
            else:
                written = [m for m in entry_log["months"] if m["outcome"] in ("written", "replaced", "held")]
                entry_log["status"] = "ok" if written else "unchanged"
        except HttpBlocked as exc:
            entry_log.update(status="blocked", error=repr(exc)[:500])
        except (ConnectorError, HttpFailed, StoreError, OSError, ValueError, KeyError) as exc:
            log.warning(
                "history item failed", extra={"source_id": source_id, "item": item.key, "error": repr(exc)}
            )
            entry_log.update(status="failed", error=repr(exc)[:500], error_class=type(exc).__name__)

    tried = [e for e in record["items"] if e.get("status") != "skipped_repeated_failure"]
    broken = [e for e in tried if e.get("status") in FAILED]
    record["dq_status"] = (
        "held" if "held" in dq_levels else "warn" if "warn" in dq_levels else ("pass" if dq_levels else None)
    )
    record["awaiting_release"] = sorted(p for p, h in held.items() if h.held or h.pending_sha256)
    wrote = record["documents"]["written"] or record["documents"]["replaced"]
    if tried and len(broken) == len(tried) and not wrote:
        status = "blocked" if all(e["status"] == "blocked" for e in broken) else "failed"
        return finish(status, ConnectorError(str(broken[0].get("error"))))
    if broken or record["plan_errors"] or record["documents"]["held"]:
        return finish("partial")
    return finish("ok" if wrote else "unchanged")


__all__ = [
    "ISOLATE_BYTES",
    "MAX_ITEM_FAILURES",
    "RUN_MODES",
    "TRIGGERS",
    "RunResult",
    "consecutive_failures",
    "effective_mode",
    "held_periods",
    "kept_copy",
    "run",
    "summarise",
]
