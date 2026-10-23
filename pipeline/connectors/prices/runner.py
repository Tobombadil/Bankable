"""One price run: gate -> plan -> fetch (or re-use a stored snapshot) -> parse -> normalise -> DQ ->
merge into the interval store -> run record (docs/29 §7, §9 lane P1).

Stage rules follow the record runner's (`pipeline/connectors/runner.py`): every snapshot is
written before it is parsed, a DQ hold keeps the snapshots and writes no interval, and the run
record is written last as the commit marker the loader starts from.

* **Gate.** `base.gate`: a source whose rows could never be published under the platform posture
  is not fetched at all (CLAUDE.md, "never fetch from sources marked `reuse: restricted` for
  publication paths"); nor is a `status: pending` or `excluded` row.
* **Re-use.** A request with a `reuse_key` that an earlier run already stored (ERCOT's annual
  workbooks, by DocID) is read from the snapshot store, its SHA-256 checked, and not downloaded.
* **Download caps (review 2026-10-23).** A request whose `meta` carries `report` and
  `max_downloads` (ERCOT's "no more than three times within a 12-month period") is counted in the
  download ledger (`store.py`) before it is sent and refused (`DownloadRefused`, recorded under
  `skipped`, nothing sent) once the ledger and the run records show the cap reached in the trailing
  twelve months. The check sits here, at the one place bytes are fetched, so every path that could
  fetch a capped report again (a planned download, a stored copy that fails its SHA-256 or has
  gone) passes it. A capped request is sent once, without `PoliteSession`'s in-call retries.
* **Bounded memory (review 2026-10-23).** Each request's rows are parsed in pieces
  (`PriceConnector.frames`) and spilled, piece by piece, to a staging directory of parquet files
  partitioned by UTC month; the DQ checks read only the light key columns, and the merge reads one
  month at a time. A year of ERCOT's fifteen-minute prices with their `raw` source rows is never
  in memory at once. Freed memory does not go back to the operating system (pandas, openpyxl and
  Arrow leave the heap fragmented: measured, a backfill done in one process still climbed to
  560-640 MB), so a snapshot of `isolate_parse_bytes` or more is parsed, and a month of
  `ISOLATE_MERGE_ROWS` or more staged rows merged, in a forked child process that returns only its
  counts and light columns: its memory is the operating system's again when it exits, and an
  out-of-memory kill takes the child, not the run (the run then records the failure). Measured
  peaks in docs/29 §9.
* **Conditional requests.** A request marked `conditional` carries the validators the last
  snapshot of the same key recorded; a 304 stores nothing and adds no rows.
* **Corrections, not events.** A stored interval whose price a run finds changed is replaced (both
  ERCOT and CAISO publish corrections, docs/29 §7.2) and the before/after pair is written to the
  corrections log `prices/<source>/corrections/<ts>.parquet`. Prices are context data with no
  lifecycle (docs/29 §7.1), so nothing here becomes a public `event`.
* **Status** uses `source_run`'s vocabulary: `ok`, `unchanged` (no new or changed row), `partial`
  (a DQ hold), `failed`, `blocked` (robots.txt or a challenge). One failed request among several
  (a day's page missing) is recorded as a `request_failures` warning, and the next run asks for
  that day again, because coverage, not the plan, decides what a run asks for.
"""

from __future__ import annotations

import datetime as dt
import functools
import gc
import hashlib
import logging
import multiprocessing
import pathlib
import re
import sys
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped]  # pandas's parquet engine; no stubs installed
import pyarrow.compute as pc  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from pipeline.connectors.base import ConnectorError, RawSnapshot, to_parquet_safe, utcnow
from pipeline.connectors.http import HttpBlocked, HttpFailed, PoliteSession
from pipeline.connectors.objectstore import StoreError
from pipeline.connectors.prices import connector_class_for
from pipeline.connectors.prices import dq as price_dq
from pipeline.connectors.prices import store as pstore
from pipeline.connectors.prices.base import (
    MODES,
    Coverage,
    DownloadRefused,
    FetchRequest,
    PriceConnector,
    downloads_within,
    gate,
)
from pipeline.connectors.registry import PUBLISHABLE_REUSE, Registry
from pipeline.connectors.store import Store, open_store, ts_token

log = logging.getLogger("pipeline.connectors.prices")

TRIGGERS = ("schedule", "manual", "backfill", "retry")
#: What the DQ checks and the coverage of a staged run read: no `raw`, no provenance strings.
LIGHT_COLUMNS = [*pstore.KEY_COLUMNS, "price"]
_HTTP_STATUS = re.compile(r"HTTP (\d{3})\b")
#: A staged month with this many rows or more is merged in a child process (module docstring):
#: a backfill month of ERCOT is about 56,000 rows, a daily run 1,800 to 3,600.
ISOLATE_MERGE_ROWS = 5_000

T = TypeVar("T")


# ----------------------------------------------------------------------------- child processes
class ChildFailed(ConnectorError):
    """A child process ended without a result: killed (out of memory, a signal) or crashed."""


def _rebuild(name: str, message: str) -> BaseException:
    """The parent's exception for one a child raised, as a class the runner records."""
    from pipeline.connectors.base import ParseError

    known: dict[str, type[BaseException]] = {
        "ParseError": ParseError,
        "ValueError": ValueError,
        "KeyError": KeyError,
        "OSError": OSError,
        "FileNotFoundError": OSError,
    }
    cls = known.get(name)
    return cls(message) if cls is not None else ConnectorError(f"{name}: {message}")


def can_fork() -> bool:
    return "fork" in multiprocessing.get_all_start_methods()


def in_child(work: Callable[[], T]) -> T:
    """`work()` in a forked child process, its result (pickled) returned here. The child's memory
    is returned to the operating system when it exits; an exception it raises is raised here as a
    class the runner records (`_rebuild`); a child that dies without a result raises `ChildFailed`.
    Without `fork` (not Linux), `work` runs in this process."""
    if not can_fork():
        return work()
    ctx = multiprocessing.get_context("fork")
    reader, writer = ctx.Pipe(duplex=False)

    def target() -> None:
        try:
            writer.send(("ok", work()))
        except BaseException as exc:  # every failure, a KeyboardInterrupt included, goes back to the parent
            writer.send(("error", (type(exc).__name__, str(exc)[:2000])))
        finally:
            writer.close()

    for stream in (sys.stdout, sys.stderr):
        stream.flush()  # nothing buffered here may be written twice by the child
    proc = ctx.Process(target=target, daemon=True)
    # The parent's objects as they are at the fork go to the permanent generation, so a collection
    # in the child never writes to their headers (which would copy the parent's heap page by page).
    gc.collect()
    gc.freeze()
    try:
        proc.start()
    finally:
        gc.unfreeze()
    writer.close()
    try:
        kind, payload = reader.recv()
    except EOFError:
        kind, payload = "died", None
    finally:
        reader.close()
        proc.join()
    if kind == "ok":
        result: T = payload
        return result
    if kind == "error":
        raise _rebuild(*payload)
    raise ChildFailed(
        f"the child process ended with exit code {proc.exitcode} and no result (out of memory?)"
    )


@dataclass
class PriceRunResult:
    record: dict[str, Any]
    #: The run's distinct new intervals, light columns only (`LIGHT_COLUMNS`); the full rows are
    #: in the interval store. EIA's retail table comes back whole (it is small).
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    run_path: Any = None

    @property
    def status(self) -> str:
        return str(self.record["status"])


# ------------------------------------------------------------------------------------- coverage
def _local_day_hours(tz: str, day: dt.date) -> float:
    start = pd.Timestamp(day).tz_localize(tz)
    end = pd.Timestamp(day + dt.timedelta(days=1)).tz_localize(tz)
    return float((end - start).total_seconds() / 3600.0)


def light_dtypes(connector: PriceConnector) -> dict[str, pd.CategoricalDtype]:
    """One categorical dtype per light key column for a connector, so the light frames of every
    piece (and the stored months' keys) concatenate without falling back to Python objects."""
    return {
        "location": pd.CategoricalDtype(sorted(connector.location_names)),
        "market": pd.CategoricalDtype(sorted(connector.interval_minutes) or ["da", "rt"]),
    }


def complete_days(df: pd.DataFrame, connector: PriceConnector) -> dict[str, set[dt.date]]:
    """Per market, the market-local days on which every served location has every interval. The
    days are grouped as local-midnight timestamps (no Python `date` per row: a year of fifteen-
    minute prices is 660,000 rows)."""
    out: dict[str, set[dt.date]] = {}
    if df.empty:
        return out
    locations = list(connector.locations.values())
    for market, minutes in connector.interval_minutes.items():
        served = {loc.name for loc in locations if market in loc.markets}
        sub = df.loc[(df["market"] == market) & (df["location"].isin(served)), ["location", "interval_start"]]
        if sub.empty or not served:
            continue
        tz = locations[0].timezone
        local = pd.to_datetime(sub["interval_start"], utc=True).dt.tz_convert(tz).dt.tz_localize(None)
        counts = (
            pd.DataFrame({"_day": local.dt.normalize().to_numpy(), "location": sub["location"].to_numpy()})
            .groupby(["_day", "location"])
            .size()
        )
        days: set[dt.date] = set()
        for stamp, per_loc in counts.groupby(level=0):
            day = pd.Timestamp(stamp).date()
            expected = round(_local_day_hours(tz, day) * 60 / minutes)
            by_loc = per_loc.droplevel(0)
            if all(int(by_loc.get(name, 0)) >= expected for name in served):
                days.add(day)
        out[market] = days
    return out


def coverage(
    store: Store,
    connector: PriceConnector,
    months: list[str],
    *,
    runs: list[dict[str, Any]],
    ledger: list[dict[str, Any]] | None = None,
) -> tuple[Coverage, pd.DataFrame]:
    """What the store holds for the months a run plans over (key columns only), the stored
    snapshots a request may re-use, and each capped report's downloads: from the download ledger
    first, then from run records (a store from before the ledger), each download counted once."""
    existing = pstore.read_months(store, connector.source_id, months, columns=pstore.KEY_COLUMNS)
    for col, dtype in light_dtypes(connector).items():
        existing[col] = existing[col].astype(dtype)
    cov = Coverage()
    if not existing.empty:
        for market, sub in existing.groupby("market"):
            cov.first_interval[str(market)] = pd.Timestamp(sub["interval_start"].min()).to_pydatetime()
            cov.last_interval[str(market)] = pd.Timestamp(sub["interval_start"].max()).to_pydatetime()
        cov.complete_days = complete_days(existing, connector)
    counted: set[str] = set()
    for entry in pstore.read_ledger(store, connector.source_id) if ledger is None else ledger:
        if entry.get("state") == "stored" and entry.get("reuse_key") and entry.get("object_key"):
            if entry.get("sha256"):
                cov.snapshots[str(entry["reuse_key"])] = entry
        report = entry.get("report")
        if report and pstore.counts_as_download(entry):
            when = entry.get("retrieved_at") or entry.get("started_at")
            cov.downloads.setdefault(str(report), []).append(str(when))
            counted.add(str(entry.get("object_key") or f"{entry.get('run_ts')}.{entry.get('key')}"))
    for record in runs:
        for entry in record.get("requests") or []:
            if entry.get("reuse_key") and entry.get("object_key") and entry.get("sha256"):
                cov.snapshots[str(entry["reuse_key"])] = entry
            report = (entry.get("meta") or {}).get("report")
            if report and entry.get("downloaded") and str(entry.get("object_key")) not in counted:
                cov.downloads.setdefault(str(report), []).append(str(entry.get("retrieved_at")))
    return cov, existing


def _validators(runs: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """The newest validators each request key recorded (a 304 keeps the ones it was asked with)."""
    out: dict[str, dict[str, str]] = {}
    for record in runs:
        for entry in record.get("requests") or []:
            headers = {k.lower(): v for k, v in (entry.get("headers") or {}).items()}
            seen = {}
            if headers.get("etag"):
                seen["etag"] = headers["etag"]
            if headers.get("last-modified"):
                seen["last_modified"] = headers["last-modified"]
            if seen and entry.get("http_status") == 200:
                out[str(entry["key"])] = seen
    return out


def _plan_months(mode: str, today: dt.date, connector: PriceConnector) -> list[str]:
    if mode == "latest":
        first = today - dt.timedelta(days=2)
    elif mode == "daily":
        first = today - dt.timedelta(days=16)
    else:
        first = today - dt.timedelta(days=connector.backfill_days + 2)
    return pstore.months_between(first, today + dt.timedelta(days=2))


# ---------------------------------------------------------------------------------------- staging
class Staging:
    """A run's normalised rows, spilled to local parquet as they are produced: one file per piece
    and UTC month (`<month>.<seq>.parquet`), with only the light columns kept in memory for the DQ
    checks. The merge reads one month back at a time, in the order the pieces arrived, so "the
    last row for a key wins" holds as it did for one in-memory frame."""

    def __init__(
        self, root: pathlib.Path, prefix: str = "", dtypes: Mapping[str, pd.CategoricalDtype] | None = None
    ) -> None:
        self.root = root
        self.prefix = prefix
        self.dtypes = dict(dtypes or {})
        self.files: dict[str, list[pathlib.Path]] = {}
        self.month_rows: dict[str, int] = {}
        self._light: list[pd.DataFrame] = []
        self.rows = 0

    def add(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        self.rows += len(df)
        light = df[LIGHT_COLUMNS].copy()
        for col in ("location", "market"):
            light[col] = light[col].astype(self.dtypes.get(col, "category"))
        self._light.append(light)
        months = pstore.month_labels(df["interval_start"])
        for month, part in df.groupby(months, sort=False):
            bucket = self.files.setdefault(str(month), [])
            path = self.root / f"{month}.{self.prefix}{len(bucket):05d}.parquet"
            # As `Store.write_parquet` writes a month file, so a staged table and a stored one
            # carry the same column types.
            to_parquet_safe(part.reset_index(drop=True)).to_parquet(path, index=False)
            bucket.append(path)
            self.month_rows[str(month)] = self.month_rows.get(str(month), 0) + len(part)

    def absorb(self, other: _Parsed) -> None:
        """Take a parse's staged files (written by this process or a child) in arrival order."""
        for month, paths in other.files.items():
            self.files.setdefault(month, []).extend(paths)
        for month, rows in other.month_rows.items():
            self.month_rows[month] = self.month_rows.get(month, 0) + rows
        if other.light is not None and len(other.light):
            self._light.append(other.light)
        self.rows += other.staged_rows

    def light(self) -> pd.DataFrame:
        if not self._light:
            return pd.DataFrame(columns=LIGHT_COLUMNS)
        return pd.concat(self._light, ignore_index=True)

    def months(self) -> list[str]:
        return sorted(self.files)

    def month_table(self, month: str) -> pa.Table:
        """The month's staged rows as one Arrow table, the last row for a `record_id` kept."""
        table = pa.concat_tables(
            [pq.read_table(p, use_threads=False) for p in self.files[month]], promote_options="default"
        )
        ids = table.column("record_id").to_pandas()
        return table.filter(pa.array((~ids.duplicated(keep="last")).to_numpy()))


@dataclass
class _Parsed:
    """What parsing one snapshot produced: counts, staged files and light columns (an interval
    source), or the frames themselves (the small retail table). Picklable, so a child returns it."""

    rows: int = 0
    dropped: int = 0
    unknown: set[str] = field(default_factory=set)
    drift: list[dict[str, Any]] = field(default_factory=list)
    frames: list[pd.DataFrame] = field(default_factory=list)
    files: dict[str, list[pathlib.Path]] = field(default_factory=dict)
    month_rows: dict[str, int] = field(default_factory=dict)
    light: pd.DataFrame | None = None
    staged_rows: int = 0


def _parse_snapshot(
    connector: PriceConnector, raw: RawSnapshot, stage_root: pathlib.Path, tag: str, retail: bool
) -> _Parsed:
    out = _Parsed()
    staging = Staging(stage_root, prefix=f"{tag}.", dtypes=light_dtypes(connector))
    drift = getattr(connector, "placement_check", None)
    for df in connector.frames(raw):
        out.rows += len(df)
        out.dropped += int(df.attrs.get("dropped", 0))
        out.unknown.update(df.attrs.get("unknown_locations", []))
        if not len(df):
            continue
        if retail:
            out.frames.append(df)
            continue
        if callable(drift):
            out.drift.append(drift(df))
        staging.add(df)
    out.files, out.month_rows, out.staged_rows = staging.files, staging.month_rows, staging.rows
    out.light = staging.light()
    return out


def parse_snapshot(
    connector: PriceConnector, raw: RawSnapshot, stage_root: pathlib.Path, tag: str, retail: bool
) -> _Parsed:
    """`_parse_snapshot`, in a child process for a snapshot of the connector's
    `isolate_parse_bytes` or more (module docstring)."""
    limit = getattr(connector, "isolate_parse_bytes", None)
    if not retail and limit is not None and len(raw.content) >= int(limit):
        return in_child(lambda: _parse_snapshot(connector, raw, stage_root, tag, retail))
    return _parse_snapshot(connector, raw, stage_root, tag, retail)


# -------------------------------------------------------------------------------------------- run
def run(
    source_id: str,
    mode: str,
    *,
    registry: Registry | None = None,
    store: Store | None = None,
    now: dt.datetime | None = None,
    http: PoliteSession | None = None,
    env: Mapping[str, str] | None = None,
    trigger: str = "manual",
    run_id: str | None = None,
    publishable: frozenset[str] = PUBLISHABLE_REUSE,
    clock: Callable[[], dt.datetime] | None = None,
) -> PriceRunResult:
    """Run `source_id` in `mode`. Raises `PriceGateRefused` (nothing fetched) and `KeyError`
    for a source this family does not serve; every other failure is recorded on the run."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {TRIGGERS}, got {trigger!r}")
    registry = registry or Registry()
    store = store or open_store()
    entry = registry.get(source_id)
    gate(entry, publishable)
    cls = connector_class_for(source_id)
    if mode not in cls.modes:
        raise ValueError(f"{source_id} does not support mode {mode!r} (supports {cls.modes})")
    started = now or utcnow()
    tick = clock or (lambda: now or utcnow())
    connector = cls(entry, http=http, env=env, clock=tick)
    ts = _unique_ts(store, source_id, started)
    record: dict[str, Any] = {
        "id": run_id or str(uuid.uuid4()),
        "source_id": source_id,
        "kind": "price",
        "mode": mode,
        "trigger": trigger,
        "parser_version": cls.parser_version,
        "started_at": started.isoformat(),
        "status": "running",
        "requests": [],
        "egress_class": entry.egress or "plain",
    }
    t0 = time.monotonic()
    frame = pd.DataFrame()
    try:
        if mode == "backfill":
            # Before anything is fetched: a backfill killed before its run record still shows as
            # attempted, so the scheduler does not start it again on the next tick.
            pstore.write_attempt(
                store,
                source_id,
                ts,
                {"mode": mode, "trigger": trigger, "run_id": record["id"], "started_at": started.isoformat()},
            )
        with tempfile.TemporaryDirectory(prefix="price-stage-") as stage:
            frame = _execute(connector, mode, store, ts, record, started.date(), pathlib.Path(stage))
    except HttpBlocked as exc:
        record.update(status="blocked", error=str(exc)[:2000], error_class="HttpBlocked")
    except (HttpFailed, ConnectorError, StoreError, OSError, ValueError, KeyError) as exc:
        record.update(status="failed", error=str(exc)[:2000], error_class=type(exc).__name__)
        log.warning("price run failed", extra={"source_id": source_id, "mode": mode, "error": str(exc)[:500]})
    elapsed = time.monotonic() - t0
    finished = utcnow() if now is None else now + dt.timedelta(seconds=elapsed)
    record["finished_at"] = finished.isoformat()
    record["worker_seconds"] = round(elapsed, 2)
    record["requests_made"] = connector.http.requests_made
    run_path = store.write_run(source_id, ts, record)
    record["run_path"] = str(run_path)
    return PriceRunResult(record=record, frame=frame, run_path=run_path)


def _unique_ts(store: Store, source_id: str, when: dt.datetime) -> str:
    ts = ts_token(when)
    bump = 0
    while store.exists(store.run_path(source_id, ts)) or store.exists(
        pstore.attempt_path(store, source_id, ts)
    ):
        bump += 1
        ts = ts_token(when + dt.timedelta(seconds=bump))
    return ts


def _cap(request: FetchRequest) -> tuple[str, int] | None:
    report, limit = request.meta.get("report"), request.meta.get("max_downloads")
    if report and limit:
        return str(report), int(limit)
    return None


def _stored_bytes(store: Store, stored: Mapping[str, Any]) -> bytes | None:
    """A stored snapshot's bytes, or None when the object has gone or cannot be read."""
    try:
        return store.backend.get(str(stored["object_key"]))
    except (StoreError, OSError, KeyError):
        return None


def _http_status(exc: BaseException) -> int | None:
    match = _HTTP_STATUS.search(str(exc))
    return int(match.group(1)) if match else None


def _obtain(
    connector: PriceConnector, request: FetchRequest, store: Store, ts: str, cov: Coverage
) -> tuple[RawSnapshot, dict[str, Any]]:
    """The request's bytes: the stored snapshot its `reuse_key` names, else a fetch (stored). A
    capped request passes the download cap first and is written to the ledger before it is sent."""
    entry: dict[str, Any] = {
        "key": request.key,
        "url": request.url,
        "reuse_key": request.reuse_key,
        "meta": {
            k: v for k, v in request.meta.items() if isinstance(v, str | int | float | bool) or v is None
        },
    }
    stored = cov.snapshots.get(request.reuse_key or "") if request.reuse_key else None
    if stored is not None:
        body = _stored_bytes(store, stored)
        if body is not None and hashlib.sha256(body).hexdigest() == stored["sha256"]:
            raw = RawSnapshot(
                content=body,
                content_type=str(stored.get("content_type") or ""),
                url=str(stored.get("source_url") or request.url),
                retrieved_at=dt.datetime.fromisoformat(str(stored["retrieved_at"]).replace("Z", "+00:00")),
                http_status=200,
                ext=request.ext,
                meta={"key": request.key, **dict(request.meta), "reused": True},
                requests_made=0,
            )
            entry.update(
                reused=True,
                downloaded=False,
                object_key=stored["object_key"],
                sha256=stored["sha256"],
                bytes=len(body),
                http_status=200,
                retrieved_at=stored["retrieved_at"],
                source_url=raw.url,
            )
            return raw, entry
        log.warning(
            "stored snapshot missing or failing its SHA-256; fetching again within its cap",
            extra={"key": request.key},
        )
        entry["stored_snapshot_unusable"] = True
    cap = _cap(request)
    ledger: dict[str, Any] = {}
    if cap is not None:
        report, limit = cap
        done = downloads_within(cov.downloads, report, connector.now())
        if done >= limit:
            raise DownloadRefused(
                f"{report}: downloaded {done} times in the trailing 12 months (cap {limit}); not asked again"
            )
        started = connector.now().astimezone(dt.UTC).isoformat(timespec="seconds")
        ledger = {
            "state": "started",
            "report": report,
            "max_downloads": limit,
            "url": request.url,
            "reuse_key": request.reuse_key,
            "doc_id": request.meta.get("doc_id"),
            "started_at": started,
        }
        pstore.write_ledger(store, connector.source_id, ts, request.key, ledger)
        cov.downloads.setdefault(report, []).append(started)
    try:
        raw = connector.fetch(request)
    except HttpBlocked as exc:
        if cap is not None:
            ledger.update(state="failed", blocked=True, error=str(exc)[:500])
            pstore.write_ledger(store, connector.source_id, ts, request.key, ledger)
        raise
    except (HttpFailed, ConnectorError) as exc:
        if cap is not None:
            ledger.update(state="failed", http_status=_http_status(exc), error=str(exc)[:500])
            pstore.write_ledger(store, connector.source_id, ts, request.key, ledger)
        raise
    entry.update(
        reused=False,
        downloaded=raw.http_status == 200 and bool(raw.content),
        http_status=raw.http_status,
        bytes=len(raw.content),
        elapsed_s=raw.elapsed_s,
        retrieved_at=raw.retrieved_at_iso,
        headers=dict(raw.headers),
        source_url=raw.url,
        content_type=raw.content_type,
    )
    if raw.http_status == 200 and raw.content:
        path = store.write_snapshot(connector.source_id, f"{ts}.{request.key}", request.ext, raw.content)
        entry.update(object_key=store.key(path), sha256=raw.sha256)
    if cap is not None:
        ledger.update(
            state="stored" if entry.get("object_key") else "failed",
            http_status=raw.http_status,
            bytes=len(raw.content),
            retrieved_at=raw.retrieved_at_iso,
            source_url=raw.url,
            content_type=raw.content_type,
            object_key=entry.get("object_key"),
            sha256=entry.get("sha256"),
        )
        pstore.write_ledger(store, connector.source_id, ts, request.key, ledger)
    return raw, entry


def _drift(checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The placement checks of a run's pieces as one (the moved hubs of every piece)."""
    if not checks:
        return None
    moved: dict[str, Any] = {}
    for check in checks:
        moved.update((check.get("data") or {}).get("moved_km") or {})
    return price_dq.placement_drift_result(moved)


def _execute(
    connector: PriceConnector,
    mode: str,
    store: Store,
    ts: str,
    record: dict[str, Any],
    today: dt.date,
    stage_root: pathlib.Path,
) -> pd.DataFrame:
    runs = store.runs(connector.source_id)
    retail = getattr(connector, "frame_kind", "interval") == "retail"
    months = [] if retail else _plan_months(mode, today, connector)
    cov, existing = coverage(store, connector, months, runs=runs)
    connector.validators = _validators(runs)
    staging = Staging(stage_root, dtypes=light_dtypes(connector))
    retail_frames: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    drift_checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped = 0
    unknown: set[str] = set()

    def process(queue: deque[FetchRequest], coverage_now: Coverage) -> None:
        nonlocal dropped
        while queue:
            request = queue.popleft()
            if request.key in seen:
                continue
            seen.add(request.key)
            try:
                raw, entry = _obtain(connector, request, store, ts, coverage_now)
            except DownloadRefused as exc:
                refused.append({"key": request.key, "report": request.meta.get("report"), "reason": str(exc)})
                continue
            except (HttpFailed, ConnectorError) as exc:
                failures.append({"key": request.key, "url": request.url, "error": str(exc)[:500]})
                record["requests"].append({"key": request.key, "url": request.url, "error": str(exc)[:500]})
                continue
            # Recorded before the bytes are parsed: a parse that fails still leaves the download
            # on the run record (and, for a capped report, it is on the ledger already).
            record["requests"].append(entry)
            rows = 0
            if raw.http_status == 200:
                parsed = parse_snapshot(connector, raw, stage_root, f"{len(seen):04d}", retail)
                rows = parsed.rows
                dropped += parsed.dropped
                unknown.update(parsed.unknown)
                retail_frames.extend(parsed.frames)
                drift_checks.extend(parsed.drift)
                staging.absorb(parsed)
            entry["rows"] = rows
            follow = getattr(connector, "follow_up", None)
            if callable(follow) and raw.http_status == 200:
                queue.extend(follow(request, raw, today, coverage_now))

    process(deque(connector.plan(mode, today, cov)), cov)
    gap_fill = getattr(connector, "gap_fill", None)
    if mode == "backfill" and callable(gap_fill) and not retail:
        staged = staging.light()[pstore.KEY_COLUMNS]
        held = pd.concat([existing, staged], ignore_index=True) if len(existing) else staged
        cov_after = Coverage(
            complete_days=complete_days(held, connector), snapshots=cov.snapshots, downloads=cov.downloads
        )
        process(deque(gap_fill(today, cov_after)), cov_after)
    skipped = [*list(getattr(connector, "skipped", None) or []), *refused]
    if skipped:
        record["skipped"] = skipped

    attempted = [e for e in record["requests"] if "error" not in e]
    if failures and not attempted:
        raise ConnectorError(f"every request failed: {failures[0]['error']}")
    if retail:
        new = pd.concat(retail_frames, ignore_index=True) if retail_frames else pd.DataFrame()
        rows_fetched = len(new)
    else:
        new = staging.light()
        rows_fetched = staging.rows
    parsed = sum(1 for e in record["requests"] if e.get("http_status") == 200 and e.get("bytes"))
    checks = _checks(connector, mode, new, dropped, unknown, runs, parsed=parsed, drift=_drift(drift_checks))
    if failures:
        checks.append(
            {
                "check": "request_failures",
                "level": "warn",
                "detail": f"{len(failures)} request(s) failed",
                "data": {"failures": failures[:20]},
            }
        )
    record["dq"] = {"status": price_dq.status(checks), "checks": checks}
    record["dq_status"] = record["dq"]["status"]
    record["bytes"] = sum(int(e.get("bytes") or 0) for e in record["requests"] if not e.get("reused"))
    last = [e for e in record["requests"] if e.get("http_status")]
    record["http_status"] = last[-1]["http_status"] if last else None
    record["rows_fetched"] = rows_fetched
    if record["dq_status"] == "hold":
        record.update(
            status="partial",
            hold_reasons=[c["check"] for c in checks if c["level"] == "hold"],
            rows_seen=rows_fetched,
            rows_new=0,
            rows_changed=0,
            rows_gone=0,
            events_emitted=0,
        )
        return pd.DataFrame()
    if retail:
        deduped = new.drop_duplicates("record_id", keep="last") if len(new) else new
        counts = _merge_retail(connector, store, ts, deduped)
        record["rows_seen"] = len(deduped)
        record["stats"] = _stats(deduped)
    else:
        # Distinct rows and their summary come from the merge, month by month: the parent never
        # de-duplicates a backfill's 660,000 light rows itself.
        counts = _merge_intervals(connector, store, ts, staging)
        record["rows_seen"] = counts.pop("rows_distinct")
        record["stats"] = counts.pop("stats")
        deduped = new
    record.update(counts)
    record["rows_gone"] = 0
    record["events_emitted"] = 0
    record["status"] = "ok" if (counts["rows_new"] or counts["rows_changed"]) else "unchanged"
    return deduped.reset_index(drop=True)


def _checks(
    connector: PriceConnector,
    mode: str,
    df: pd.DataFrame,
    dropped: int,
    unknown: set[str],
    runs: list[dict[str, Any]],
    *,
    parsed: int,
    drift: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """`parsed`: requests that returned content this run. With none (every answer a 304, an absent
    optional page, a re-used snapshot already merged), there is nothing new to count. `df` is the
    run's rows before de-duplication: the light columns for an interval source, the whole frame
    for the retail table."""
    previous = next(
        (
            int(r.get("rows_fetched") or 0)
            for r in reversed(runs)
            if r.get("mode") == mode and r.get("status") == "ok" and r.get("rows_fetched")
        ),
        None,
    )
    checks = [price_dq.null_spike(len(df), dropped)]
    if getattr(connector, "frame_kind", "interval") == "retail":
        if parsed:
            checks.append(price_dq.row_count(len(df), previous))
        return checks
    # A daily or latest run asks for what is missing, so its size varies; only a backfill's
    # (and the retail table's) row count is compared with the previous run of the same mode.
    if mode == "backfill" and parsed:
        checks.append(price_dq.row_count(len(df), previous))
    checks.append(price_dq.no_rows(len(df), parsed))
    checks.append(price_dq.duplicate_keys(df))
    checks.append(
        price_dq.location_vocabulary(df, connector.location_names, unknown, connector.interval_minutes)
    )
    checks.append(price_dq.price_range(df))
    if drift is not None:
        checks.append(drift)
    return checks


_CORRECTION_COLUMNS = ["record_id", "location", "market", "interval_start", "source_url", "retrieved_at"]


def _merge_month(source_id: str, store: Store, staging: Staging, month: str) -> dict[str, Any]:
    """One UTC month's staged rows into its stored file, as Arrow tables: only the keys and prices
    (and a correction's few columns) ever become pandas objects. Returns the counts, whether the
    file was written and the corrections (a small frame, or None)."""
    out: dict[str, Any] = {"rows_new": 0, "rows_changed": 0, "rows_unchanged": 0, "written": False}
    out["corrections"] = None
    part = staging.month_table(month)
    starts = part.column("interval_start")
    out["distinct"] = part.num_rows
    out["by_market"] = {str(k): int(v) for k, v in part.column("market").to_pandas().value_counts().items()}
    out["first"] = pd.Timestamp(pc.min(starts).as_py()) if part.num_rows else None
    out["last"] = pd.Timestamp(pc.max(starts).as_py()) if part.num_rows else None
    existing = pstore.read_month_table(store, source_id, month)
    if existing is None or existing.num_rows == 0:
        merged = part
        out["rows_new"] = part.num_rows
    else:
        joined = (
            part.select(["record_id", "price"])
            .to_pandas()
            .merge(
                existing.select(["record_id", "price"]).to_pandas(),
                on="record_id",
                how="left",
                suffixes=("", "_before"),
            )
        )
        fresh = joined["price_before"].isna()
        changed = (~fresh) & ((joined["price"] - joined["price_before"]).abs() > 1e-9)
        out["rows_new"] = int(fresh.sum())
        out["rows_changed"] = int(changed.sum())
        out["rows_unchanged"] = int((~fresh & ~changed).sum())
        if changed.any():
            ids = pa.array(joined.loc[changed, "record_id"].tolist(), type=pa.string())
            rows = part.filter(pc.is_in(part["record_id"], value_set=ids)).select(_CORRECTION_COLUMNS)
            corrected = joined[changed].merge(rows.to_pandas(), on="record_id")
            corrected["interval_start"] = pd.to_datetime(corrected["interval_start"], utc=True)
            out["corrections"] = corrected
        if not (fresh.any() or changed.any()):
            return out
        kept = existing.filter(pc.invert(pc.is_in(existing["record_id"], value_set=part["record_id"])))
        merged = pa.concat_tables([kept, part], promote_options="default")
    pstore.write_month_table(store, source_id, month, merged)
    out["written"] = True
    return out


def _merge_intervals(connector: PriceConnector, store: Store, ts: str, staging: Staging) -> dict[str, Any]:
    """Merge the staged rows into the interval store one UTC month at a time; a large month in a
    child process (`ISOLATE_MERGE_ROWS`, module docstring)."""
    out: dict[str, Any] = {"rows_new": 0, "rows_changed": 0, "rows_unchanged": 0, "outputs": {"months": []}}
    corrections: list[pd.DataFrame] = []
    source_id = connector.source_id
    distinct = 0
    by_market: dict[str, int] = {}
    first: pd.Timestamp | None = None
    last: pd.Timestamp | None = None
    for month in staging.months():
        merge = functools.partial(_merge_month, source_id, store, staging, month)
        result = in_child(merge) if staging.month_rows.get(month, 0) >= ISOLATE_MERGE_ROWS else merge()
        for key in ("rows_new", "rows_changed", "rows_unchanged"):
            out[key] += int(result[key])
        if result["corrections"] is not None:
            corrections.append(result["corrections"])
        if result["written"]:
            out["outputs"]["months"].append(month)
        distinct += int(result["distinct"])
        for market, n in result["by_market"].items():
            by_market[market] = by_market.get(market, 0) + n
        if result["first"] is not None:
            first = result["first"] if first is None else min(first, result["first"])
            last = result["last"] if last is None else max(last, result["last"])
    out["rows_distinct"] = distinct
    out["stats"] = (
        {
            "rows": distinct,
            "by_market": dict(sorted(by_market.items())),
            "first_interval": pd.Timestamp(first).tz_convert("UTC").isoformat(),
            "last_interval": pd.Timestamp(last).tz_convert("UTC").isoformat(),
        }
        if distinct and first is not None and last is not None
        else {"rows": 0}
    )
    if corrections:
        log_df = pd.concat(corrections, ignore_index=True).rename(columns={"price": "price_after"})
        path = store.write_parquet(pstore.corrections_path(store, connector.source_id, ts), log_df)
        out["outputs"]["corrections"] = store.key(path)
    return out


def _merge_retail(connector: PriceConnector, store: Store, ts: str, new: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {"rows_new": 0, "rows_changed": 0, "rows_unchanged": 0, "outputs": {}}
    if new.empty:
        return out
    path = pstore.retail_path(store, connector.source_id)
    if store.exists(path):
        existing = store.read_parquet(path)
        joined = new[["record_id", "price_cents_kwh", "data_status"]].merge(
            existing[["record_id", "price_cents_kwh", "data_status"]],
            on="record_id",
            how="left",
            suffixes=("", "_before"),
        )
        fresh = joined["price_cents_kwh_before"].isna() & joined["data_status_before"].isna()
        changed = ~fresh & (
            (joined["price_cents_kwh"].fillna(-1) != joined["price_cents_kwh_before"].fillna(-1))
            | (joined["data_status"].fillna("") != joined["data_status_before"].fillna(""))
        )
        out["rows_new"] = int(fresh.sum())
        out["rows_changed"] = int(changed.sum())
        out["rows_unchanged"] = int((~fresh & ~changed).sum())
        if changed.any():
            corr = store.write_parquet(
                pstore.corrections_path(store, connector.source_id, ts), joined[changed]
            )
            out["outputs"]["corrections"] = store.key(corr)
        if not (fresh.any() or changed.any()):
            return out
        merged = pd.concat([existing[~existing["record_id"].isin(new["record_id"])], new], ignore_index=True)
    else:
        merged = new
        out["rows_new"] = len(new)
    store.write_parquet(path, merged.sort_values(["state", "sector", "period"]).reset_index(drop=True))
    out["outputs"]["retail"] = store.key(path)
    return out


def _stats(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"rows": 0}
    if "market" in df.columns:
        return {
            "rows": len(df),
            "by_market": {str(k): int(v) for k, v in df.groupby("market", observed=True).size().items()},
            "first_interval": pd.Timestamp(df["interval_start"].min()).isoformat(),
            "last_interval": pd.Timestamp(df["interval_start"].max()).isoformat(),
        }
    return {"rows": len(df), "periods": sorted(df["period"].unique())[-1:]}
