"""us.iso.caiso.oasis_prices — CAISO trading hubs (SP15, NP15, ZP26) and default load aggregation
points (PG&E, SCE, SDG&E, Valley Electric), day-ahead hourly and fifteen-minute real time.

Terms (docs/29 §2.2): CAISO Terms of Use and API Terms of Use; register class
attribution-restricted, rule **derived-only** with the credit "Source: California ISO". The
statistics are derived and publish; the raw interval price does not (no latest readout, no daily
series) until CAISO authorises it in writing. The rows are fetched and stored to compute the
statistics, and the API's visibility rule keeps them off every public surface.

Route: OASIS `SingleZip` with the seven nodes named (the interface allows up to ten), the fewest
requests the interface allows: "Data can be requested for period of 31 days only" (error 1004),
so one request covers up to 31 days of one market. Day-ahead is `PRC_LMP` (`market_run_id=DAM`,
version 12), real time the fifteen-minute `PRC_RTPD_LMP` (`RTPD`, version 3), the interval
docs/29 §7.4 names as the fallback that is a third of the five-minute volume. A trailing year is
24 requests; a daily run is two. No published request rate; one request every five seconds
(gridstatus's default, docs/29 A-1). OASIS has no robots.txt (404).

Parse: gridstatus's own OASIS reader and LMP pivot (vendored `caiso_oasis_frame`, `caiso_lmp`).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import ParseError, RawSnapshot
from pipeline.connectors.prices.base import Coverage, FetchRequest, PriceConnector
from pipeline.connectors.prices.locations import CAISO_SOURCE
from pipeline.vendor.gridstatus.prices import (
    CAISO_TIMEZONE,
    DAY_AHEAD_HOURLY,
    REAL_TIME_15_MIN,
    PriceLayoutError,
    caiso_lmp,
    caiso_oasis_frame,
)

OASIS = "https://oasis.caiso.com/oasisapi/SingleZip"
#: market -> (queryname, version, market_run_id, vendored market, interval minutes)
QUERIES: dict[str, tuple[str, int, str, str, int]] = {
    "da": ("PRC_LMP", 12, "DAM", DAY_AHEAD_HOURLY, 60),
    "rt": ("PRC_RTPD_LMP", 3, "RTPD", REAL_TIME_15_MIN, 15),
}
MAX_WINDOW_DAYS = 31
DAILY_CATCHUP_DAYS = 14


def oasis_time(day: dt.date) -> str:
    """Pacific midnight of `day` as OASIS's `YYYYMMDDTHH:MM-0000` (UTC)."""
    local = pd.Timestamp(day).tz_localize(CAISO_TIMEZONE)
    return str(local.tz_convert("UTC").strftime("%Y%m%dT%H:%M-0000"))


def windows(days: list[dt.date], size: int = MAX_WINDOW_DAYS) -> list[tuple[dt.date, dt.date]]:
    """Runs of consecutive `days` cut into spans of at most `size` days, as (first, last)."""
    out: list[tuple[dt.date, dt.date]] = []
    for day in sorted(days):
        if out and (day - out[-1][1]).days == 1 and (day - out[-1][0]).days < size:
            out[-1] = (out[-1][0], day)
        else:
            out.append((day, day))
    return out


class Connector(PriceConnector):
    source_id: ClassVar[str] = CAISO_SOURCE
    market_code: ClassVar[str] = "CAISO"
    honour_robots: ClassVar[bool] = True
    modes: ClassVar[tuple[str, ...]] = ("daily", "backfill")
    interval_minutes: ClassVar[dict[str, int]] = {"da": 60, "rt": 15}

    def plan(self, mode: str, today: dt.date, coverage: Coverage) -> list[FetchRequest]:
        if mode == "daily":
            floor = today - dt.timedelta(days=DAILY_CATCHUP_DAYS)
        elif mode == "backfill":
            floor = today - dt.timedelta(days=self.backfill_days)
        else:
            raise ValueError(f"mode {mode!r} not supported by {self.source_id}")
        out: list[FetchRequest] = []
        last_day = {"da": today + dt.timedelta(days=1), "rt": today - dt.timedelta(days=1)}
        for market in ("da", "rt"):
            missing = coverage.missing_days(market, floor, last_day[market])
            for first, last in windows(missing):
                out.append(self._request(market, first, last, optional=last > today))
        return out

    def _request(self, market: str, first: dt.date, last: dt.date, *, optional: bool) -> FetchRequest:
        query, version, run_id, _vendored, _minutes = QUERIES[market]
        nodes = ",".join(self.location_names)
        return FetchRequest(
            key=f"{market}-{first:%Y%m%d}-{last:%Y%m%d}",
            url=OASIS,
            ext="zip",
            params={
                "resultformat": 6,
                "queryname": query,
                "version": version,
                "market_run_id": run_id,
                "node": nodes,
                "startdatetime": oasis_time(first),
                "enddatetime": oasis_time(last + dt.timedelta(days=1)),
            },
            meta={
                "kind": "oasis",
                "market": market,
                "first": first.isoformat(),
                "last": last.isoformat(),
                "optional": optional,
            },
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        market = str(raw.meta.get("market"))
        if raw.http_status == 404 or not raw.content:
            return []
        _query, _version, _run, vendored, minutes = QUERIES[market]
        frame = caiso_oasis_frame(raw.content)
        if frame is None:
            if raw.meta.get("optional"):
                return []  # tomorrow's day-ahead not yet published
            raise ParseError("OASIS answered without data (an error document)")
        try:
            df = caiso_lmp(frame, vendored)
        except PriceLayoutError as exc:
            raise ParseError(str(exc)) from exc
        starts = pd.to_datetime(df["Interval Start"]).dt.tz_convert("UTC")
        return [
            {
                "NODE": str(loc),
                "LMP": float(lmp),
                "MCE": float(energy),
                "MCC": float(cong),
                "MCL": float(loss),
                "MGHG": None if pd.isna(ghg) else float(ghg),
                "_interval_start": start.isoformat(),
                "_interval_minutes": minutes,
                "_market": market,
            }
            for loc, lmp, energy, cong, loss, ghg, start in zip(
                df["Location"],
                df["LMP"],
                df["Energy"],
                df["Congestion"],
                df["Loss"],
                df["GHG"],
                starts,
                strict=True,
            )
        ]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        records = [
            {
                "location": row["NODE"],
                "market": row["_market"],
                "interval_start": row["_interval_start"],
                "interval_minutes": row["_interval_minutes"],
                "price": row["LMP"],
            }
            for row in rows
        ]
        return self.frame(records, rows, raw)
