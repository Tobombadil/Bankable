"""us.iso.nyiso.prices — NYISO zonal LBMP for the eleven New York zones (A to K), day-ahead hourly
and real-time hourly (the integrated real-time LBMP, `rtlbmp`).

Terms (docs/29 §2.5): NYISO Legal Notice; register class attribution-restricted, rule
**derived-only**, credit "Source: NYISO". Statistics publish; no raw interval price and no daily
series leave the building (no latest readout).

Route: mis.nyiso.com's public csv tree. A trailing year is the monthly zip archives, one per month
per market (`<yyyymm01>damlbmp_zone_csv.zip`, about 110 KB; the current month's zip is rewritten
each morning with the days so far, 23 files on 2026-10-22): 26 requests. The daily csv files
(`<yyyymmdd>damlbmp_zone.csv`, about 17 KB) are kept for about ten days only (2026-10-22: the
2026-10-12 file present, 2026-10-11 gone), so a missing day older than `DAILY_FILE_DAYS` is read
from its month's zip and a newer one from its daily file. Day-ahead for tomorrow is posted the
day before. Some archives are short at the source: the July 2026 real-time zip holds 12 days
(07-19, 07-21 to 07-31; checked 2026-10-22), and those days cannot be filled from the public tree;
coverage and the statistics' sample counts show the gap. No published rate; 0.5 requests a second
(`rate_limit_rps`). mis.nyiso.com has no robots.txt (404). The external proxy zones in the files
(H Q, NPX, O H, PJM) are not New York zones and are dropped (`locations.py`).

Parse: gridstatus's own NYISO time handling and LMP finaliser (vendored `nyiso_handle_time`,
`nyiso_lmp`); time stamps are interval starts in US/Eastern, the repeated hour inferred per zone.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import ParseError, RawSnapshot
from pipeline.connectors.prices.base import Coverage, FetchRequest, PriceConnector
from pipeline.connectors.prices.locations import NYISO_SOURCE
from pipeline.vendor.gridstatus.prices import (
    DAY_AHEAD_HOURLY,
    NYISO_DAM_LMP_DATASET,
    NYISO_REAL_TIME_HOURLY_LMP_DATASET,
    REAL_TIME_HOURLY,
    PriceLayoutError,
    nyiso_handle_time,
    nyiso_lmp,
)

BASE = "https://mis.nyiso.com/public/csv"
#: market -> (dataset, vendored market)
DATASETS: dict[str, tuple[str, str]] = {
    "da": (NYISO_DAM_LMP_DATASET, DAY_AHEAD_HOURLY),
    "rt": (NYISO_REAL_TIME_HOURLY_LMP_DATASET, REAL_TIME_HOURLY),
}
DAILY_CATCHUP_DAYS = 14
#: Days back that a daily csv file is still on the server (about ten observed; one day's margin).
DAILY_FILE_DAYS = 9


class Connector(PriceConnector):
    source_id: ClassVar[str] = NYISO_SOURCE
    market_code: ClassVar[str] = "NYISO"
    honour_robots: ClassVar[bool] = True
    modes: ClassVar[tuple[str, ...]] = ("daily", "backfill")
    interval_minutes: ClassVar[dict[str, int]] = {"da": 60, "rt": 60}

    def plan(self, mode: str, today: dt.date, coverage: Coverage) -> list[FetchRequest]:
        last_day = {"da": today + dt.timedelta(days=1), "rt": today - dt.timedelta(days=1)}
        out: list[FetchRequest] = []
        if mode == "backfill":
            floor = today - dt.timedelta(days=self.backfill_days)
        elif mode == "daily":
            floor = today - dt.timedelta(days=DAILY_CATCHUP_DAYS)
        else:
            raise ValueError(f"mode {mode!r} not supported by {self.source_id}")
        for market in ("da", "rt"):
            out.extend(
                self._requests_for(market, coverage.missing_days(market, floor, last_day[market]), today)
            )
        return out

    def _requests_for(self, market: str, missing: list[dt.date], today: dt.date) -> list[FetchRequest]:
        """A month's zip for every month with a missing day the daily files no longer hold (the
        zip then stands for that whole month); a daily file for every other missing day."""
        horizon = today - dt.timedelta(days=DAILY_FILE_DAYS)
        months = sorted({d.replace(day=1) for d in missing if d < horizon})
        out = [self._month(market, month) for month in months]
        out.extend(
            self._day(market, d, today) for d in missing if d >= horizon and d.replace(day=1) not in months
        )
        return out

    def _day(self, market: str, day: dt.date, today: dt.date) -> FetchRequest:
        dataset, _ = DATASETS[market]
        return FetchRequest(
            key=f"{market}-{day:%Y%m%d}",
            url=f"{BASE}/{dataset}/{day:%Y%m%d}{dataset}_zone.csv",
            ext="csv",
            meta={"kind": "day_csv", "market": market, "day": day.isoformat(), "optional": day >= today},
        )

    def _month(self, market: str, month: dt.date) -> FetchRequest:
        dataset, _ = DATASETS[market]
        return FetchRequest(
            key=f"{market}-month-{month:%Y%m}",
            url=f"{BASE}/{dataset}/{month:%Y%m01}{dataset}_zone_csv.zip",
            ext="zip",
            meta={"kind": "month_zip", "market": market, "month": month.isoformat()},
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        if raw.http_status == 404 or not raw.content:
            return []
        market = str(raw.meta.get("market"))
        dataset, vendored = DATASETS[market]
        frames: list[pd.DataFrame] = []
        try:
            if raw.meta.get("kind") == "month_zip":
                with zipfile.ZipFile(io.BytesIO(raw.content)) as z:
                    for name in sorted(z.namelist()):
                        if name.endswith(".csv"):
                            frames.append(pd.read_csv(z.open(name)))
            else:
                frames.append(pd.read_csv(io.BytesIO(raw.content)))
        except (zipfile.BadZipFile, ValueError, pd.errors.ParserError) as exc:
            raise ParseError(f"NYISO file unreadable: {exc}") from exc
        rows: list[dict[str, Any]] = []
        for frame in frames:
            frame = (frame[frame["Name"].isin(self.location_names)] if "Name" in frame else frame).copy()
            try:
                df = nyiso_lmp(nyiso_handle_time(frame, dataset), vendored, "zone")
            except PriceLayoutError as exc:
                raise ParseError(str(exc)) from exc
            starts = pd.to_datetime(df["Interval Start"]).dt.tz_convert("UTC")
            rows.extend(
                {
                    "Name": str(loc),
                    "LBMP ($/MWHr)": float(lmp),
                    "Marginal Cost Losses ($/MWHr)": float(loss),
                    "Marginal Cost Congestion ($/MWHr)": float(-cong),  # as published (gridstatus flips it)
                    "_interval_start": start.isoformat(),
                    "_interval_minutes": 60,
                    "_market": market,
                }
                for loc, lmp, loss, cong, start in zip(
                    df["Location"], df["LMP"], df["Loss"], df["Congestion"], starts, strict=True
                )
            )
        return rows

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        records = [
            {
                "location": row["Name"],
                "market": row["_market"],
                "interval_start": row["_interval_start"],
                "interval_minutes": row["_interval_minutes"],
                "price": row["LBMP ($/MWHr)"],
            }
            for row in rows
        ]
        return self.frame(records, rows, raw)
