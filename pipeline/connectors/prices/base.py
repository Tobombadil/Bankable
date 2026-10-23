"""The price connector contract (docs/29 §7, §9 "Why not subclass `Connector`").

A price connector is the record connector's three pure steps on the same parts
(`pipeline/connectors/base.py`: `RawSnapshot`, `PoliteSession`, the registry's `SourceEntry`),
with a plan in front because a price run fetches several files:

    plan(mode, today, coverage) -> list[FetchRequest]   which files this run needs, none it has
    fetch(request)              -> RawSnapshot          network, bytes in, nothing parsed
    parse(raw)                  -> list[dict]           source-shaped rows; never the network
    normalize(rows, raw)        -> DataFrame            `PRICE_COLUMNS`, provenance on every row
    frames(raw)                 -> Iterator[DataFrame]  parse + normalize in pieces (what the runner
                                                        calls; a large file yields one piece per
                                                        sheet so a run never holds it whole)

It is not a `pipeline.connectors.base.Connector`: that class types `kind` as proposal,
opportunity or document, its runner diffs records into lifecycle events, and editing it would
re-version every connector's parser (docs/29 §9). Prices are context data with no lifecycle
(docs/29 §7.1); a changed price for an interval already stored is a correction, which the price
runner records as one row of its corrections log (`runner.py`), never a public event.

Every normalised row carries `source_id, source_url, retrieved_at, licence_id, raw` (CLAUDE.md),
plus `record_id = <source_id>:<location>|<market>|<interval_start UTC>`, stable across runs.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import ConnectorError, ParseError, RawSnapshot, raw_json, utcnow
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.prices.locations import PriceLocation, by_name, for_source, location_id
from pipeline.connectors.registry import SourceEntry

#: Run modes. `latest`: the newest published interval(s), for the readout; `daily`: the last
#: complete operating day(s) and the next day's day-ahead where published; `backfill`: history for
#: the trailing statistics, with the fewest requests the source's terms allow.
MODES = ("latest", "daily", "backfill")

PRICE_COLUMNS: list[str] = [
    "record_id",
    "source_id",
    "source_record_id",
    "source_url",
    "retrieved_at",
    "licence_id",
    "location_id",
    "location",
    "market",
    "interval_start",
    "interval_end",
    "interval_minutes",
    "price",
    "currency",
    "raw",
]


@dataclass(frozen=True)
class FetchRequest:
    """One file a run needs. `key` names it in the snapshot store (`<ts>.<key>.<ext>`) and in the
    run record; `reuse_key` says that a stored snapshot under that key may stand in for a new
    download (ERCOT's annual workbooks: "store each downloaded report once", docs/13 §6)."""

    key: str
    url: str
    ext: str
    params: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)
    reuse_key: str | None = None
    conditional: bool = False


@dataclass
class Coverage:
    """What the store already holds for a source (`runner.coverage`): per market, the first and
    last interval start (UTC) and the market-local days whose every interval is stored for every
    location; the stored snapshots by request key (for re-use); and, per report, when each
    download happened (ERCOT's three-downloads rule)."""

    last_interval: dict[str, dt.datetime] = field(default_factory=dict)
    first_interval: dict[str, dt.datetime] = field(default_factory=dict)
    complete_days: dict[str, set[dt.date]] = field(default_factory=dict)
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    downloads: dict[str, list[str]] = field(default_factory=dict)

    def missing_days(self, market: str, start: dt.date, end: dt.date) -> list[dt.date]:
        """The days from `start` to `end` (both included) not complete for `market`."""
        done = self.complete_days.get(market, set())
        return [d for d in day_range(start, end) if d not in done]


class PriceGateRefused(ConnectorError):
    """The source may not be fetched for a publication path (gated class, `publication: none`,
    private aggregator, or a row still `status: pending` / `excluded`)."""


class DownloadRefused(ConnectorError):
    """A capped report (`meta.max_downloads`) already downloaded that many times in the trailing
    twelve months; the runner records the skip and sends nothing (ERCOT's API terms, docs/13 §6)."""


#: The trailing period a download cap counts over ("within a 12-month period").
CAP_WINDOW = dt.timedelta(days=365)


def downloads_within(downloads: Mapping[str, list[str]], report: str, now: dt.datetime) -> int:
    """How many times `downloads` (a report -> the time of each download, ISO) shows `report`
    downloaded in the trailing twelve months before `now`. An unreadable time counts (the safe
    reading of a cap)."""
    floor = now - CAP_WINDOW
    count = 0
    for stamp in downloads.get(report, []):
        try:
            when = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            count += 1
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        if when >= floor:
            count += 1
    return count


def gate(entry: SourceEntry, publishable: frozenset[str]) -> None:
    """Refuse a source whose rows could never be published: CLAUDE.md "never fetch from sources
    marked `reuse: restricted` for publication paths". `publishable` is the posture's set
    (`pipeline.connectors.registry.PUBLISHABLE_REUSE`), so SPP (`noncommercial`) runs only under
    `PLATFORM_POSTURE=noncommercial`."""
    status = str(entry.raw.get("status") or "")
    if entry.never_ingest:
        raise PriceGateRefused(f"{entry.id} is a private aggregator; it has no connector by design")
    if status in ("pending", "excluded"):
        raise PriceGateRefused(f"{entry.id} is `status: {status}` in data/sources.yaml; nothing is fetched")
    if entry.reuse not in publishable:
        raise PriceGateRefused(
            f"{entry.id} has reuse={entry.reuse!r}, not publishable under this posture; not fetched"
        )
    if entry.publication == "none":
        raise PriceGateRefused(f"{entry.id}: publication=none publishes nothing; not fetched")


class PriceConnector:
    """Base class. Subclasses set the class attributes and implement plan/fetch/parse/normalize."""

    source_id: ClassVar[str]
    #: The market code the locations carry (`ERCOT`, `SPP`, ...).
    market_code: ClassVar[str]
    honour_robots: ClassVar[bool] = True
    #: Bump on a deliberate change of what `parse`/`normalize` produce.
    parser_version: ClassVar[str] = "1.0.0"
    #: The modes this source supports (a derived-only source has no `latest`).
    modes: ClassVar[tuple[str, ...]] = ("daily", "backfill")
    #: Days of history `backfill` asks for (the trailing-statistics window plus margin).
    backfill_days: ClassVar[int] = 366
    #: Interval length per market, in minutes: what a complete market-local day holds.
    interval_minutes: ClassVar[dict[str, int]] = {}
    #: A snapshot this large or larger is parsed in a child process (`runner.parse_snapshot`), so
    #: the memory its parse leaves fragmented goes back to the operating system; None: never.
    isolate_parse_bytes: ClassVar[int | None] = None

    def __init__(
        self,
        source: SourceEntry,
        http: PoliteSession | None = None,
        *,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], dt.datetime] = utcnow,
    ) -> None:
        if source.id != self.source_id:
            raise ValueError(f"{type(self).__name__} is bound to {self.source_id}, got {source.id}")
        self.source = source
        self.http = http or PoliteSession(rate_limits={source.host: source.max_rps})
        self.env: Mapping[str, str] = env if env is not None else os.environ
        self.clock = clock
        self.locations: dict[str, PriceLocation] = by_name(self.source_id)
        #: The previous snapshot's HTTP validators per request key, set by the runner.
        self.validators: dict[str, dict[str, str]] = {}

    # ---------------------------------------------------------------------------------- contract
    def plan(self, mode: str, today: dt.date, coverage: Coverage) -> list[FetchRequest]:
        raise NotImplementedError

    def fetch(self, request: FetchRequest) -> RawSnapshot:
        return self.get(request)

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        raise NotImplementedError

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        raise NotImplementedError

    def frames(self, raw: RawSnapshot) -> Iterator[pd.DataFrame]:
        """The normalised rows of `raw`, in pieces: what the runner reads. One piece by default; a
        connector whose files are large (ERCOT's annual workbooks) yields one per sheet, so the
        run spills each to its staging area before the next is parsed."""
        rows = self.parse(raw)
        if rows:
            yield self.normalize(rows, raw)

    # ----------------------------------------------------------------------------------- helpers
    def now(self) -> dt.datetime:
        return self.clock()

    def get(
        self,
        request: FetchRequest,
        *,
        timeout: float = 120.0,
        honour_robots: bool | None = None,
        **kwargs: Any,
    ) -> RawSnapshot:
        """One polite GET as a `RawSnapshot`; a conditional request answered 304 comes back with
        `http_status=304` and no content, which the runner reads as unchanged. A 404 on a request
        whose `meta.optional` is set (tomorrow's day-ahead page before it is published) comes back
        as an empty 404 snapshot rather than an error.

        A capped request (`meta.max_downloads`) is sent **once**: `PoliteSession` otherwise retries
        a reset or a 5xx inside the one call, and each retry of a report that was partly sent is
        another download the runner's ledger would not see (it records one per call)."""
        headers: dict[str, str] = dict(kwargs.pop("headers", {}) or {})
        if request.conditional:
            seen = self.validators.get(request.key) or {}
            if seen.get("etag"):
                headers["If-None-Match"] = seen["etag"]
            if seen.get("last_modified"):
                headers["If-Modified-Since"] = seen["last_modified"]
        t0 = time.monotonic()
        before = self.http.requests_made
        attempts = self.http.max_attempts
        if request.meta.get("max_downloads"):
            self.http.max_attempts = 1
        try:
            r = self.http.get(
                request.url,
                honour_robots=self.honour_robots if honour_robots is None else honour_robots,
                params=dict(request.params) or None,
                headers=headers or None,
                timeout=timeout,
                **kwargs,
            )
        finally:
            self.http.max_attempts = attempts
        ok = (200, 304, 404) if request.meta.get("optional") else (200, 304)
        if r.status_code not in ok:
            raise ConnectorError(f"GET {request.url} -> HTTP {r.status_code}")
        return RawSnapshot(
            content=bytes(r.content) if r.status_code == 200 else b"",
            content_type=r.headers.get("Content-Type", ""),
            url=str(getattr(r, "url", "") or request.url),
            retrieved_at=self.now(),
            http_status=r.status_code,
            ext=request.ext,
            headers={k: v for k, v in r.headers.items() if k.lower() in ("etag", "last-modified")},
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=max(self.http.requests_made - before, 1),
            meta={"key": request.key, **dict(request.meta)},
        )

    def frame(
        self, records: list[dict[str, Any]], rows: list[dict[str, Any]], raw: RawSnapshot
    ) -> pd.DataFrame:
        """`records` (one per price: `location`, `market`, `interval_start`, `interval_minutes`,
        `price`, optional `source_url`) aligned with `rows` (the source-shaped row each came from)
        as `PRICE_COLUMNS`: only locations this connector serves, provenance stamped, `raw` the
        source row. Prices that are not numbers are dropped and counted on `attrs["dropped"]`."""
        if len(records) != len(rows):
            raise ParseError(f"{len(records)} price records for {len(rows)} source rows")
        out: list[dict[str, Any]] = []
        dropped = 0
        unknown: set[str] = set()
        for rec, row in zip(records, rows, strict=True):
            loc = self.locations.get(str(rec["location"]))
            if loc is None:
                unknown.add(str(rec["location"]))
                continue
            price = rec.get("price")
            try:
                value = math.nan if price is None else float(price)
            except (TypeError, ValueError):
                value = math.nan
            if not math.isfinite(value):
                dropped += 1
                continue
            start = pd.Timestamp(rec["interval_start"])
            if start.tzinfo is None:
                raise ParseError(f"interval_start without a time zone: {rec['interval_start']!r}")
            start = start.tz_convert("UTC")
            minutes = int(rec["interval_minutes"])
            market = str(rec["market"])
            source_record_id = f"{loc.name}|{market}|{start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            out.append(
                {
                    "record_id": f"{self.source_id}:{source_record_id}",
                    "source_id": self.source_id,
                    "source_record_id": source_record_id,
                    "source_url": rec.get("source_url") or raw.url,
                    "retrieved_at": raw.retrieved_at_iso,
                    "licence_id": self.source.licence_id,
                    "location_id": location_id(loc.market, loc.name),
                    "location": loc.name,
                    "market": market,
                    "interval_start": start,
                    "interval_end": start + pd.Timedelta(minutes=minutes),
                    "interval_minutes": minutes,
                    "price": value,
                    "currency": loc.currency,
                    "raw": raw_json(row),
                }
            )
        df = pd.DataFrame(out, columns=PRICE_COLUMNS)
        if not df.empty:
            df["interval_start"] = pd.to_datetime(df["interval_start"], utc=True)
            df["interval_end"] = pd.to_datetime(df["interval_end"], utc=True)
            df["price"] = df["price"].astype(float)
            df["interval_minutes"] = df["interval_minutes"].astype(int)
        df.attrs["dropped"] = dropped
        df.attrs["unknown_locations"] = sorted(unknown)
        return df

    @property
    def location_names(self) -> tuple[str, ...]:
        return tuple(loc.name for loc in for_source(self.source_id))


def as_json(content: bytes) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f"not JSON: {exc}") from exc


def day_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """Every date from `start` to `end`, both included."""
    days = (end - start).days
    return [start + dt.timedelta(days=i) for i in range(max(days + 1, 0))]
