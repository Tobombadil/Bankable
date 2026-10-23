"""gb.elexon.bmrs_prices — the GB system (imbalance) price per half-hour settlement period.

Terms (docs/29 §2.8): the BMRS open data licence, "worldwide, royalty-free, perpetual,
non-exclusive", including commercial use, on one condition that ends the licence automatically if
broken: "you must always use the following attribution statement ...: Contains BMRS data © Elexon
Limited copyright and database right [year]", with a link to the licence where possible. The
[year] is the year of the data shown, so the API renders the credit with the years of the rows it
serves (`services/api/prices.py`). Raw-ok; a latest readout is allowed with no delay. Market Index
Data is a separate, held-back row (`gb.elexon.market_index_data`) and is never read here.

Route: `data.elexon.co.uk/bmrs/api/v1/balancing/settlement/system-prices/{settlementDate}`, the
only form the API offers for this dataset (one settlement date per request; no key; "All our APIs
are public and no API key is required"). A trailing year is 366 requests at 0.5 a second, about
twelve minutes; a daily run asks for the days not yet held; the latest readout asks for today
once an hour, inside the BMRS API terms' bar on "invoking the API more than once in a half hour
period for data that is only updated on a half hourly basis". The API has no robots.txt (404).

The price is `systemSellPrice`: GB has had a single imbalance price since November 2015, and the
buy price is kept in `raw` beside it. `startTime` is the period start in UTC.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import ParseError, RawSnapshot
from pipeline.connectors.prices.base import Coverage, FetchRequest, PriceConnector, as_json
from pipeline.connectors.prices.locations import ELEXON_SOURCE

API = "https://data.elexon.co.uk/bmrs/api/v1/balancing/settlement/system-prices/{day}"
LOCATION = "SYSTEM"
DAILY_CATCHUP_DAYS = 14


class Connector(PriceConnector):
    source_id: ClassVar[str] = ELEXON_SOURCE
    market_code: ClassVar[str] = "GB"
    honour_robots: ClassVar[bool] = True
    modes: ClassVar[tuple[str, ...]] = ("latest", "daily", "backfill")
    interval_minutes: ClassVar[dict[str, int]] = {"rt": 30}

    def plan(self, mode: str, today: dt.date, coverage: Coverage) -> list[FetchRequest]:
        if mode == "latest":
            return [self._day(today, key="rt-today")]
        if mode == "daily":
            floor = today - dt.timedelta(days=DAILY_CATCHUP_DAYS)
        elif mode == "backfill":
            floor = today - dt.timedelta(days=self.backfill_days)
        else:
            raise ValueError(f"mode {mode!r} not supported by {self.source_id}")
        return [self._day(d) for d in coverage.missing_days("rt", floor, today - dt.timedelta(days=1))]

    def _day(self, day: dt.date, key: str | None = None) -> FetchRequest:
        return FetchRequest(
            key=key or f"rt-{day:%Y%m%d}",
            url=API.format(day=day.isoformat()),
            ext="json",
            params={"format": "json"},
            meta={"kind": "system_prices", "market": "rt", "day": day.isoformat()},
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        body = as_json(raw.content)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise ParseError("Elexon system prices answer without `data`")
        out: list[dict[str, Any]] = []
        for row in data:
            if "startTime" not in row or "systemSellPrice" not in row:
                raise ParseError(f"Elexon row without startTime/systemSellPrice: {sorted(row)[:8]}")
            start = pd.Timestamp(row["startTime"])
            if start.tzinfo is None:
                start = start.tz_localize("UTC")
            out.append(
                {
                    **row,
                    "_interval_start": start.tz_convert("UTC").isoformat(),
                    "_interval_minutes": 30,
                    "_market": "rt",
                }
            )
        return out

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        records = [
            {
                "location": LOCATION,
                "market": row["_market"],
                "interval_start": row["_interval_start"],
                "interval_minutes": row["_interval_minutes"],
                "price": row["systemSellPrice"],
            }
            for row in rows
        ]
        return self.frame(records, rows, raw)
