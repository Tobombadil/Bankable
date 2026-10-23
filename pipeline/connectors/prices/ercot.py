"""us.iso.ercot.prices — ERCOT settlement point prices at the seven trading hubs and eight load zones.

Terms (docs/29 §2.1): Website User Agreement clause 5, raw data "may be used, reproduced, and
redistributed in compilations, charts, and analyses"; no delay; raw-ok. Credit "Source: ERCOT" as
policy. Rate: 0.4 requests a second (`rate_limit_rps`), inside the API's 30 a minute.

Routes, all public and keyless, chosen for the fewest requests:

* **backfill** — the annual hub and load-zone workbooks on the public MIS document list:
  NP6-785-ER (report type 13061, real-time 15-minute) and NP4-180-ER (13060, day-ahead hourly),
  one zip per year (2025: 13,969,454 and 2,050,483 bytes). A trailing year needs the previous
  year's final file and the current year's year-to-date file, which ERCOT republishes weekly: two
  list requests and four downloads. "You may not download the same report ... more than three
  times within a 12-month period" (Terms of Use for ERCOT API, docs/13 §6), so every workbook is
  stored once in the snapshot store and re-parsed from there (a stored DocID is never downloaded
  again), and a year whose report the store already shows downloaded three times in the trailing
  twelve months is not requested a fourth time (`report_downloads`). Each download is on the
  runner's download ledger before it is sent, and the runner refuses a fourth itself, so a run
  that dies mid-parse still counts (`runner.py`, review 2026-10-23). Only the sheets (one per
  month) and rows inside the backfill window are parsed, one sheet at a time, so a year of
  fifteen-minute prices is never held whole. The days after the year-to-date file's last interval
  are filled from the dated display pages below.
* **daily** — the public display pages ERCOT keeps per operating day,
  `content/cdr/html/<yyyymmdd>_real_time_spp.html` (every 15-minute interval of one day for the
  fifteen locations, about 77 KB) and `<yyyymmdd>_dam_spp.html` (24 hours, about 23 KB, published
  the day before). One request per day per market, against 96 per day for the per-interval MIS
  files; robots.txt allows `/content/cdr/` (it disallows `/misapp` and `/misdownload`, which this
  connector, like the ERCOT queue connector, treats as the documented data-product API).
* **latest** — `content/cdr/html/real_time_spp.html`, today's intervals so far (about 55 KB),
  asked with the page's own validators (`ETag`, `Last-Modified`), so an unchanged page costs a
  304. The newest interval is the latest-price readout.
* **ERCOT API** (api.ercot.com), only when `ERCOT_API_SUBSCRIPTION_KEY`, `ERCOT_API_USERNAME` and
  `ERCOT_API_PASSWORD` are all set (a person registers the account: "Accounts registered by
  'bots' ... are not permitted"). Used by backfill for a year whose annual workbook may not be
  downloaded again, through `ercot_api.py`; never otherwise.

Parse: the annual workbooks through gridstatus's own parser (vendored, `ercot_annual_spp`); the
display pages here (gridstatus has no parser for them), with the same Central-time rules:
an interval ending `0100*` is the second, standard-time pass of the repeated hour on the day
daylight saving ends, and the interval ending `0200` on the day it starts ends at the jump.
Load zones are the `LZ` settlement point type; the energy-weighted `LZEW` prices in the
real-time workbook (`LZ_<name>_EW` after parsing) are not one of the fifteen locations and are
left out, as are DC ties.
"""

from __future__ import annotations

import calendar
import datetime as dt
import html
import json
import re
from collections.abc import Callable, Iterator
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from pipeline.connectors.base import ParseError, RawSnapshot
from pipeline.connectors.prices import ercot_api
from pipeline.connectors.prices.base import (
    PRICE_COLUMNS,
    Coverage,
    FetchRequest,
    PriceConnector,
    as_json,
    downloads_within,
)
from pipeline.connectors.prices.locations import ERCOT_SOURCE, location_id
from pipeline.vendor.gridstatus.prices import (
    DAY_AHEAD_HOURLY,
    ERCOT_TIMEZONE,
    REAL_TIME_15_MIN,
    ErcotDocument,
    PriceLayoutError,
    ercot_annual_spp_sheets,
    ercot_documents,
)

CDR = "https://www.ercot.com/content/cdr/html"
DOC_LIST = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={rtid}"
#: The annual hub and load-zone workbooks: market -> (report type id, constructed-name stem).
ANNUAL_REPORTS: dict[str, tuple[int, str]] = {
    "rt": (13061, "RTMLZHBSPP"),
    "da": (13060, "DAMLZHBSPP"),
}
#: "no more than three times within a 12-month period" (docs/13 §6, `us.iso.ercot.prices`).
MAX_REPORT_DOWNLOADS = 3
#: How far back a daily run fills from the display pages (a longer gap is a backfill's).
DAILY_CATCHUP_DAYS = 14
API_ENV = ("ERCOT_API_SUBSCRIPTION_KEY", "ERCOT_API_USERNAME", "ERCOT_API_PASSWORD")

_ROW_DATE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_TAG = re.compile(r"<[^>]+>")
_CELL = re.compile(r"<t([hd])\b[^>]*>(.*?)</t[hd]>", re.S | re.I)
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_UPDATED = re.compile(
    r"Last (?:Updated|Date and Time):(?:&nbsp;|\s)*([A-Z][a-z]{2} \d{1,2}, \d{4} \d{1,2}:\d{2})"
)


def api_credentials(env: Any) -> dict[str, str] | None:
    """The three ERCOT API settings when all are set and non-empty, else None."""
    values = {name: str(env.get(name) or "").strip() for name in API_ENV}
    return values if all(values.values()) else None


_SHEET_MONTHS = {name: i for i, name in enumerate(calendar.month_abbr) if name}


def report_downloads(downloads: dict[str, list[str]], stem: str, now: dt.datetime) -> int:
    """How many times the store shows report `stem` (`RTMLZHBSPP_2026`) downloaded in the trailing
    twelve months; `downloads` maps a stem to the time of each download (the runner builds it
    from the download ledger and the run records, `runner.coverage`)."""
    return downloads_within(downloads, stem, now)


def sheet_filter(year: int, floor: dt.date) -> Callable[[str], bool]:
    """Which monthly sheets of year `year`'s workbook reach `floor` or later. A sheet whose name is
    not a month abbreviation is read (a layout change must not silently drop data)."""

    def keep(name: str) -> bool:
        month = _SHEET_MONTHS.get(name.strip()[:3].title())
        if month is None:
            return True
        return dt.date(year, month, calendar.monthrange(year, month)[1]) >= floor

    return keep


def parse_display_page(content: bytes) -> tuple[list[str], list[list[str]], str | None]:
    """An ERCOT cdr price display page as (header, rows, last-updated text). The header is the
    `<th>` row (`Oper Day`, `Interval Ending` or `Hour Ending`, then one column per location);
    each row is one interval's cells as text."""
    text = content.decode("utf-8", "replace")
    text = re.sub(r"<script\b.*?</script>", "", text, flags=re.S | re.I)
    header: list[str] = []
    rows: list[list[str]] = []
    for row in _ROW.findall(text):
        cells = [
            (kind.lower(), html.unescape(_TAG.sub("", body)).strip()) for kind, body in _CELL.findall(row)
        ]
        if not cells:
            continue
        values = [value for _, value in cells]
        if all(kind == "h" for kind, _ in cells) and values and values[0] == "Oper Day":
            header = values
        elif values and _ROW_DATE.match(values[0]):
            rows.append(values)
    match = _UPDATED.search(text)
    return header, rows, (html.unescape(match.group(1)) if match else None)


def _ending_to_interval(day: str, ending: str, minutes: int) -> pd.Timestamp:
    """The interval start (UTC) of a display-page row: `day` (`10/21/2026`) and its ending label,
    `0015`..`2400` for 15-minute rows or `1`..`24` for hourly rows; a trailing `*` marks the
    standard-time pass of the repeated hour."""
    repeated = ending.endswith("*")
    label = ending.rstrip("*").strip()
    base = dt.datetime.strptime(day, "%m/%d/%Y")  # noqa: DTZ007 - a Central wall-clock date, localised below
    if minutes == 60:
        if not label.isdigit():
            raise ParseError(f"hour ending {ending!r} is not a number")
        start_local = base + dt.timedelta(hours=int(label) - 1)
        stamp = pd.Timestamp(start_local).tz_localize(
            ERCOT_TIMEZONE, ambiguous=not repeated, nonexistent="shift_forward"
        )
        return stamp.tz_convert("UTC")
    if not (label.isdigit() and len(label) == 4):
        raise ParseError(f"interval ending {ending!r} is not HHMM")
    end_local = base + dt.timedelta(hours=int(label[:2]), minutes=int(label[2:]))
    end = pd.Timestamp(end_local).tz_localize(
        ERCOT_TIMEZONE, ambiguous=not repeated, nonexistent="shift_forward"
    )
    return (end - pd.Timedelta(minutes=minutes)).tz_convert("UTC")


class Connector(PriceConnector):
    source_id: ClassVar[str] = ERCOT_SOURCE
    market_code: ClassVar[str] = "ERCOT"
    honour_robots: ClassVar[bool] = True
    modes: ClassVar[tuple[str, ...]] = ("latest", "daily", "backfill")
    interval_minutes: ClassVar[dict[str, int]] = {"rt": 15, "da": 60}
    #: The annual workbooks (1.6-14 MB zipped) are parsed in a child process; the daily display
    #: pages (23-77 KB) in this one.
    isolate_parse_bytes: ClassVar[int | None] = 1_000_000

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The API host shares the source's politeness: 0.4 a second is inside "30 requests per minute".
        self.http.rate_limits.setdefault(ercot_api.HOST, self.source.max_rps)
        self.skipped: list[dict[str, Any]] = []
        self._token: str | None = None

    def fetch(self, request: FetchRequest) -> RawSnapshot:
        if request.meta.get("kind") != "api":
            if "/misapp/" in request.url or "/misdownload/" in request.url:
                # The documented MIS data-product route, as the ERCOT queue connector reads it
                # (robots.txt names /misapp and /misdownload; module docstring).
                return self._get_unrobotted(request)
            return self.get(request)
        creds = api_credentials(self.env)
        if creds is None:
            raise ParseError("an ERCOT API request without the three API settings")
        if self._token is None:
            self._token = ercot_api.get_token(self.http, creds)
        return self.get(request, headers=ercot_api.auth_headers(self._token, creds), honour_robots=False)

    def _get_unrobotted(self, request: FetchRequest) -> RawSnapshot:
        return self.get(request, timeout=300, honour_robots=False)

    # ------------------------------------------------------------------------------------- plan
    def plan(self, mode: str, today: dt.date, coverage: Coverage) -> list[FetchRequest]:
        if mode == "latest":
            return [
                FetchRequest(
                    key="rt-today",
                    url=f"{CDR}/real_time_spp.html",
                    ext="html",
                    meta={"kind": "rt_page", "market": "rt"},
                    conditional=True,
                )
            ]
        if mode == "daily":
            return self._daily(today, coverage, DAILY_CATCHUP_DAYS)
        if mode == "backfill":
            return self._backfill_lists()
        raise ValueError(f"mode {mode!r} not supported by {self.source_id}")

    def _daily(self, today: dt.date, coverage: Coverage, catchup: int) -> list[FetchRequest]:
        """The display pages for every operating day of the last `catchup` days that the store
        does not hold complete, up to yesterday (real time) and tomorrow (day ahead, published
        the day before; tomorrow's page may not be out yet, so its absence is not an error)."""
        requests: list[FetchRequest] = []
        floor = today - dt.timedelta(days=catchup)
        for day in coverage.missing_days("rt", floor, today - dt.timedelta(days=1)):
            stamp = day.strftime("%Y%m%d")
            requests.append(
                FetchRequest(
                    key=f"rt-{stamp}",
                    url=f"{CDR}/{stamp}_real_time_spp.html",
                    ext="html",
                    meta={"kind": "rt_page", "market": "rt", "day": day.isoformat()},
                )
            )
        for day in coverage.missing_days("da", floor, today + dt.timedelta(days=1)):
            stamp = day.strftime("%Y%m%d")
            requests.append(
                FetchRequest(
                    key=f"da-{stamp}",
                    url=f"{CDR}/{stamp}_dam_spp.html",
                    ext="html",
                    meta={"kind": "da_page", "market": "da", "day": day.isoformat(), "optional": day > today},
                )
            )
        return requests

    def _backfill_lists(self) -> list[FetchRequest]:
        return [
            FetchRequest(
                key=f"list-{rtid}",
                url=DOC_LIST.format(rtid=rtid),
                ext="json",
                meta={"kind": "doc_list", "market": market, "report_type": rtid},
            )
            for market, (rtid, _stem) in ANNUAL_REPORTS.items()
        ]

    def follow_up(
        self, request: FetchRequest, raw: RawSnapshot, today: dt.date, coverage: Coverage
    ) -> list[FetchRequest]:
        """What a fetched request leads to (the runner asks after every fetch): after a document
        list, the annual workbooks the trailing year needs; after an API page, the next page."""
        kind = request.meta.get("kind")
        if kind == "api":
            page = int(request.meta.get("page") or 1)
            if page < ercot_api.total_pages(as_json(raw.content)):
                return [
                    ercot_api.request_for(
                        str(request.meta["market"]),
                        dt.date.fromisoformat(str(request.meta["start"])),
                        dt.date.fromisoformat(str(request.meta["end"])),
                        str(request.meta["point"]),
                        page + 1,
                    )
                ]
            return []
        if kind != "doc_list":
            return []
        market = str(request.meta["market"])
        _rtid, stem = ANNUAL_REPORTS[market]
        try:
            docs = ercot_documents(as_json(raw.content), constructed_name_contains=stem, extension="")
        except PriceLayoutError as exc:
            raise ParseError(str(exc)) from exc
        window_start = today - dt.timedelta(days=self.backfill_days)
        out: list[FetchRequest] = []
        for year in range(window_start.year, today.year + 1):
            report = f"{stem}_{year}"
            candidates = [d for d in docs if d.constructed_name.endswith(f"{report}.zip")]
            if not candidates:
                continue
            doc = max(candidates, key=lambda d: d.publish_date)
            annual = self._annual_request(doc, market, report, year, window_start)
            if annual.reuse_key in coverage.snapshots:
                out.append(annual)  # stored: the runner reads the snapshot, nothing is downloaded
            elif report_downloads(coverage.downloads, report, self.now()) < MAX_REPORT_DOWNLOADS:
                out.append(annual)
            else:
                out.extend(self._api_year(market, year, today, report))
        return out

    def _api_year(self, market: str, year: int, today: dt.date, report: str) -> list[FetchRequest]:
        """A year whose workbook may not be downloaded again: the API when its settings are present,
        else nothing (the run records the skip and the display pages fill what they can)."""
        if api_credentials(self.env) is None:
            self.skipped.append(
                {
                    "report": report,
                    "reason": f"downloaded {MAX_REPORT_DOWNLOADS} times in 12 months; no API account",
                }
            )
            return []
        start = max(dt.date(year, 1, 1), today - dt.timedelta(days=self.backfill_days))
        end = min(dt.date(year, 12, 31), today)
        return [ercot_api.request_for(market, start, end, name) for name in self.location_names]

    def _annual_request(
        self, doc: ErcotDocument, market: str, report: str, year: int, window_start: dt.date
    ) -> FetchRequest:
        return FetchRequest(
            key=f"annual-{report}-{doc.doc_id}",
            url=doc.url,
            ext="zip",
            meta={
                "kind": "annual",
                "market": market,
                "report": report,
                "year": year,
                "doc_id": doc.doc_id,
                "publish_date": doc.publish_date.isoformat(),
                "content_size": doc.content_size,
                "max_downloads": MAX_REPORT_DOWNLOADS,
                # The first market-local day the backfill needs: sheets and rows before it are
                # not parsed (the workbook stays stored whole, so a later run can read more).
                "window_start": window_start.isoformat(),
            },
            reuse_key=f"annual-{report}-{doc.doc_id}",
        )

    def gap_fill(self, today: dt.date, coverage: Coverage) -> list[FetchRequest]:
        """After a backfill: the display pages for the days the newest workbook does not reach."""
        return self._daily(today, coverage, catchup=self.backfill_days)

    # ------------------------------------------------------------------------------------ parse
    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        kind = raw.meta.get("kind")
        if kind in ("rt_page", "da_page"):
            return self._parse_page(raw)
        if kind == "annual":
            return self._parse_annual(raw)
        if kind == "api":
            market = str(raw.meta.get("market"))
            return self._rows_from_frame(
                ercot_api.parse_response(as_json(raw.content), market), market, "api"
            )
        if kind == "doc_list":
            return []
        raise ParseError(f"no parser for snapshot kind {kind!r}")

    def _parse_page(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        header, rows, updated = parse_display_page(raw.content)
        if not rows:
            if raw.meta.get("optional"):
                return []
            # A dated page ERCOT keeps but no longer fills (2025-11-02's day-ahead page is empty).
            return []
        if len(header) < 3 or header[0] != "Oper Day" or header[1] not in ("Interval Ending", "Hour Ending"):
            raise ParseError(f"unexpected ERCOT display header {header[:3]!r}")
        minutes = 15 if header[1] == "Interval Ending" else 60
        market = "rt" if minutes == 15 else "da"
        out: list[dict[str, Any]] = []
        for row in rows:
            if len(row) != len(header):
                raise ParseError(f"row of {len(row)} cells under a header of {len(header)}")
            start = _ending_to_interval(row[0], row[1], minutes)
            for name, value in zip(header[2:], row[2:], strict=True):
                out.append(
                    {
                        "Oper Day": row[0],
                        header[1]: row[1],
                        "Settlement Point": name,
                        "Settlement Point Price": value,
                        "_interval_start": start.isoformat(),
                        "_interval_minutes": minutes,
                        "_market": market,
                        "_last_updated": updated,
                    }
                )
        return out

    def frames(self, raw: RawSnapshot) -> Iterator[pd.DataFrame]:
        """An annual workbook one monthly sheet at a time (each piece normalised and handed to the
        runner's staging before the next sheet is read); any other snapshot in one piece."""
        if raw.meta.get("kind") != "annual":
            yield from super().frames(raw)
            return
        market = str(raw.meta.get("market"))
        origin = str(raw.meta.get("report") or "annual")
        for df in self._annual_frames(raw):
            piece = self.annual_frame(df, market, origin, raw)
            if len(piece):
                yield piece

    def annual_frame(self, df: pd.DataFrame, market: str, origin: str, raw: RawSnapshot) -> pd.DataFrame:
        """`normalize(_rows_from_frame(df))` column by column: the same `PRICE_COLUMNS` frame, `raw`
        included, without building two dicts per price (a real-time sheet is 45,000 prices; the
        dicts were most of a backfill's memory). `test_parsers.py` holds the two equal."""
        df = df[df["Location"].isin(self.location_names)]
        price = pd.to_numeric(df["SPP"], errors="coerce").astype(float)
        finite = np.isfinite(price.to_numpy())
        df, price = df[finite], price[finite]
        minutes = 15 if market == "rt" else 60
        names = df["Location"].astype(str)
        start = pd.to_datetime(df["Interval Start"]).dt.tz_convert("UTC")
        stamp = start.dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        quoted = {name: json.dumps(name, ensure_ascii=False) for name in names.unique()}
        types = df["Location Type"].astype(str)
        type_quoted = {t: json.dumps(t, ensure_ascii=False) for t in types.unique()}
        # `raw_json(row)` of `_rows_from_frame`'s row: keys sorted, `float.__repr__` for the price,
        # `Timestamp.isoformat()` for a whole-second UTC start.
        raw_rows = (
            '{"Location Type": '
            + types.map(type_quoted)
            + ', "Settlement Point": '
            + names.map(quoted)
            + ', "Settlement Point Price": '
            + pd.Series([repr(float(v)) for v in price.to_numpy()], index=df.index, dtype=object)
            + f', "_interval_minutes": {minutes}, "_interval_start": "'
            + start.dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            + f'", "_market": {json.dumps(market)}, "_origin": {json.dumps(origin, ensure_ascii=False)}}}'
        )
        source_record_id = names + f"|{market}|" + stamp
        locs = {name: self.locations[name] for name in names.unique()}
        out = pd.DataFrame(
            {
                "record_id": f"{self.source_id}:" + source_record_id,
                "source_id": self.source_id,
                "source_record_id": source_record_id,
                "source_url": raw.url,
                "retrieved_at": raw.retrieved_at_iso,
                "licence_id": self.source.licence_id,
                "location_id": names.map({n: location_id(loc.market, loc.name) for n, loc in locs.items()}),
                "location": names,
                "market": market,
                "interval_start": start,
                "interval_end": start + pd.Timedelta(minutes=minutes),
                "interval_minutes": minutes,
                "price": price,
                "currency": names.map({n: loc.currency for n, loc in locs.items()}),
                "raw": raw_rows,
            },
            columns=PRICE_COLUMNS,
        ).reset_index(drop=True)
        out["interval_minutes"] = out["interval_minutes"].astype(int)
        out.attrs["dropped"] = int((~finite).sum())
        out.attrs["unknown_locations"] = []
        return out

    def _annual_frames(self, raw: RawSnapshot) -> Iterator[pd.DataFrame]:
        """The vendored parser's frame per sheet of an annual workbook, inside the request's
        window."""
        market = str(raw.meta.get("market"))
        vendored_market = REAL_TIME_15_MIN if market == "rt" else DAY_AHEAD_HOURLY
        window = raw.meta.get("window_start")
        floor: pd.Timestamp | None = None
        keep: Callable[[str], bool] | None = None
        if window:
            first_day = dt.date.fromisoformat(str(window))
            # Local midnight of the window's first day, as UTC; the sheet before is skipped only
            # when it ends before that day.
            floor = pd.Timestamp(first_day).tz_localize(ERCOT_TIMEZONE).tz_convert("UTC")
            year = raw.meta.get("year")
            if year:
                keep = sheet_filter(int(year), first_day)
        try:
            for _name, df in ercot_annual_spp_sheets(raw.content, vendored_market, keep):
                yield df[df["Interval Start"] >= floor] if floor is not None else df
        except PriceLayoutError as exc:
            raise ParseError(str(exc)) from exc

    def _parse_annual(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        """Source-shaped rows of an annual workbook (`parse`; the runner reads `frames`)."""
        market = str(raw.meta.get("market"))
        origin = str(raw.meta.get("report") or "annual")
        return [row for df in self._annual_frames(raw) for row in self._rows_from_frame(df, market, origin)]

    def _rows_from_frame(self, df: pd.DataFrame, market: str, origin: str) -> list[dict[str, Any]]:
        """The vendored parser's frame as source-shaped rows for the fifteen locations."""
        if df.empty:
            return []
        df = df[df["Location"].isin(self.location_names)]
        minutes = 15 if market == "rt" else 60
        starts = pd.to_datetime(df["Interval Start"]).dt.tz_convert("UTC")
        return [
            {
                "Settlement Point": str(loc),
                "Settlement Point Price": float(price),
                "Location Type": str(ltype),
                "_interval_start": start.isoformat(),
                "_interval_minutes": minutes,
                "_market": market,
                "_origin": origin,
            }
            for loc, price, ltype, start in zip(
                df["Location"], df["SPP"], df["Location Type"], starts, strict=True
            )
        ]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        records = [
            {
                "location": row["Settlement Point"],
                "market": row["_market"],
                "interval_start": row["_interval_start"],
                "interval_minutes": row["_interval_minutes"],
                "price": row["Settlement Point Price"],
            }
            for row in rows
        ]
        return self.frame(records, rows, raw)
