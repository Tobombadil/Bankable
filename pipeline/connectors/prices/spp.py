"""us.iso.spp.prices — SPP Integrated Marketplace hub prices (SPP North, SPP South, RTO West).

Terms (docs/29 §2.4; owner decision 2026-10-22): SPP's grant permits copying and distribution
"with appropriate citation" except within a commercial publication, so the rows are
`noncommercial`: published only while `PLATFORM_POSTURE=noncommercial`, which the live site runs,
and withdrawn at a posture flip (docs/26 §5). The gate (`base.gate`) refuses the fetch under the
commercial posture, so nothing is even stored then. Credit "Source: Southwest Power Pool".

SPP's External Systems Acceptable Use Policy (v3.0) asks for approval before "polling of systems
at a frequency higher than necessary for basic reconciliation of data", so this connector asks
little:

* **daily** — the day-ahead hub prices from SPP's own price contour map,
  `pricecontourmap.spp.org/.../PCM/DA_Features/MapServer/2/query` (the three hubs, 24 hours of the
  posted operating day, about 24 KB). A day the map has rolled past is read from the portal's
  daily day-ahead settlement-location file instead (below), within a week.
* **latest** — the real-time balancing market's newest five-minute hub prices from the same map
  (`PCM/RTBM_Features`, about 2.5 KB), once an hour: a readout, not a series. Real-time statistics
  are not built: they need SPP's daily real-time files, a heavier pull than basic reconciliation.
* **backfill** — the portal's daily day-ahead settlement-location files
  (`portal.spp.org/file-browser-api/download/da-lmp-by-settlement-location`, 4,262,072 bytes for
  2026-10-21, every settlement location), one per day for `backfill_days` days (30 by default,
  one request every five seconds). The hub rows are kept; the snapshot keeps the file as fetched.

Parse: the map features here (gridstatus reads only their attributes); the portal files through
gridstatus's own SPP finaliser (vendored, `spp_finalize_lmp`). `GMTINTERVALEND` is the interval
end in UTC; the map's `INTERVAL` field is local time written as if it were UTC and is not used.
Each hub's point is SPP's display position (`locations.py`); a run whose feed draws a hub more
than a kilometre from the recorded point flags it (`placement_drift`, runner DQ).
"""

from __future__ import annotations

import datetime as dt
import io
import json
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import ParseError, RawSnapshot
from pipeline.connectors.prices import dq as price_dq
from pipeline.connectors.prices.base import Coverage, FetchRequest, PriceConnector, as_json
from pipeline.connectors.prices.locations import SPP_SOURCE
from pipeline.vendor.gridstatus.prices import DAY_AHEAD_HOURLY, PriceLayoutError, spp_finalize_lmp

PCM = "https://pricecontourmap.spp.org/arcgis/rest/services/PCM"
PORTAL_DA = (
    "https://portal.spp.org/file-browser-api/download/da-lmp-by-settlement-location"
    "?path=/{y}/{m}/By_Day/DA-LMP-SL-{ymd}0100.csv"
)
HUB_QUERY = {"where": "PNODETYPE='H'", "outFields": "*", "outSR": "4326", "f": "json"}
DAILY_CATCHUP_DAYS = 7


class Connector(PriceConnector):
    source_id: ClassVar[str] = SPP_SOURCE
    market_code: ClassVar[str] = "SPP"
    honour_robots: ClassVar[bool] = True
    modes: ClassVar[tuple[str, ...]] = ("latest", "daily", "backfill")
    interval_minutes: ClassVar[dict[str, int]] = {"da": 60, "rt": 5}
    backfill_days: ClassVar[int] = 30

    def placement_check(self, df: pd.DataFrame) -> dict[str, Any]:
        """`placement_drift` (runner DQ): the map's hub points against the recorded ones."""
        points: dict[str, tuple[float, float]] = {}
        for raw in df["raw"]:
            row = json.loads(raw)
            if row.get("_x") is not None and row.get("_y") is not None:
                points[str(row["SETTLEMENT_LOCATION"])] = (float(row["_y"]), float(row["_x"]))
        recorded = {
            loc.name: (float(loc.placement.lat), float(loc.placement.lon))
            for loc in self.locations.values()
            if loc.placement.lat is not None and loc.placement.lon is not None
        }
        return price_dq.placement_drift(points, recorded)

    def plan(self, mode: str, today: dt.date, coverage: Coverage) -> list[FetchRequest]:
        if mode == "latest":
            return [
                FetchRequest(
                    key="rt-latest",
                    url=f"{PCM}/RTBM_Features/MapServer/2/query",
                    ext="json",
                    params=HUB_QUERY,
                    meta={"kind": "features", "market": "rt", "minutes": 5},
                )
            ]
        if mode == "daily":
            requests = [
                FetchRequest(
                    key="da-map",
                    url=f"{PCM}/DA_Features/MapServer/2/query",
                    ext="json",
                    params=HUB_QUERY,
                    meta={"kind": "features", "market": "da", "minutes": 60},
                )
            ]
            # Days the map no longer shows: the portal file, at most a week back.
            floor = today - dt.timedelta(days=DAILY_CATCHUP_DAYS)
            for day in coverage.missing_days("da", floor, today - dt.timedelta(days=1)):
                requests.append(self._portal(day))
            return requests
        if mode == "backfill":
            start = today - dt.timedelta(days=self.backfill_days)
            return [
                self._portal(day) for day in coverage.missing_days("da", start, today - dt.timedelta(days=1))
            ]
        raise ValueError(f"mode {mode!r} not supported by {self.source_id}")

    def _portal(self, day: dt.date) -> FetchRequest:
        return FetchRequest(
            key=f"da-portal-{day:%Y%m%d}",
            url=PORTAL_DA.format(y=f"{day:%Y}", m=f"{day:%m}", ymd=f"{day:%Y%m%d}"),
            ext="csv",
            meta={"kind": "portal_da", "market": "da", "day": day.isoformat(), "optional": True},
        )

    def fetch(self, request: FetchRequest) -> RawSnapshot:
        # The portal's file-browser API is a data download behind a JavaScript app (its robots.txt
        # path answers the app's HTML page, not a robots file); the price map is ArcGIS REST.
        return self.get(request, timeout=300, honour_robots=False)

    # ------------------------------------------------------------------------------------ parse
    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        kind = raw.meta.get("kind")
        if raw.http_status == 404 or not raw.content:
            return []
        if kind == "features":
            return self._parse_features(raw)
        if kind == "portal_da":
            return self._parse_portal(raw)
        raise ParseError(f"no parser for snapshot kind {kind!r}")

    def _parse_features(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        body = as_json(raw.content)
        if "error" in body:
            raise ParseError(f"SPP price map error: {body['error']}")
        features = body.get("features")
        if not isinstance(features, list):
            raise ParseError("SPP price map answer without features")
        minutes = int(raw.meta.get("minutes") or 60)
        out: list[dict[str, Any]] = []
        for feature in features:
            attrs = dict(feature.get("attributes") or {})
            end_ms = attrs.get("GMTINTERVALEND")
            if end_ms is None or attrs.get("SETTLEMENT_LOCATION") is None:
                raise ParseError(f"SPP feature without GMTINTERVALEND or SETTLEMENT_LOCATION: {attrs}")
            end = pd.Timestamp(int(end_ms), unit="ms", tz="UTC")
            geometry = feature.get("geometry") or {}
            out.append(
                {
                    **attrs,
                    "_x": geometry.get("x"),
                    "_y": geometry.get("y"),
                    "_interval_start": (end - pd.Timedelta(minutes=minutes)).isoformat(),
                    "_interval_minutes": minutes,
                    "_market": str(raw.meta.get("market")),
                }
            )
        return out

    def _parse_portal(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        try:
            df = pd.read_csv(io.BytesIO(raw.content))
        except (ValueError, pd.errors.ParserError) as exc:
            raise ParseError(f"SPP day-ahead file unreadable: {exc}") from exc
        df.columns = df.columns.str.strip()
        df = df[df["Settlement Location"].isin(self.location_names)] if "Settlement Location" in df else df
        try:
            df = spp_finalize_lmp(df, DAY_AHEAD_HOURLY, include_baa=True)
        except PriceLayoutError as exc:
            raise ParseError(str(exc)) from exc
        starts = pd.to_datetime(df["Interval Start"]).dt.tz_convert("UTC")
        return [
            {
                "SETTLEMENT_LOCATION": str(loc),
                "PNODE": pnode,
                "LMP": float(lmp),
                "MEC": float(energy),
                "MCC": float(cong),
                "MLC": float(loss),
                "_interval_start": start.isoformat(),
                "_interval_minutes": 60,
                "_market": "da",
            }
            for loc, pnode, lmp, energy, cong, loss, start in zip(
                df["Location"],
                df["PNode"],
                df["LMP"],
                df["Energy"],
                df["Congestion"],
                df["Loss"],
                starts,
                strict=True,
            )
        ]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        records = [
            {
                "location": row["SETTLEMENT_LOCATION"],
                "market": row["_market"],
                "interval_start": row["_interval_start"],
                "interval_minutes": row["_interval_minutes"],
                "price": row.get("LMP"),
            }
            for row in rows
        ]
        return self.frame(records, rows, raw)
