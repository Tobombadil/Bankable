"""us.eia.retail_sales — average retail electricity price by state and sector, monthly (EIA-861M,
the data behind Electric Power Monthly Tables 5.6.A and 5.6.B).

Terms (docs/29 §2.12): public domain (17 U.S.C. §105); EIA asks for "an acknowledgment, which
includes the publication date", which the API renders from the file's own release date. Raw-ok.

Route: one public file, no key: `www.eia.gov/electricity/data/eia861m/xls/sales_revenue.xlsx`
(2,269,132 bytes on 2026-10-22, last modified 2026-09-24, July 2026 data), sheet `Monthly-States`:
revenue (thousand dollars), sales (MWh), customers and average price (cents per kWh) by state and
month for the residential, commercial, industrial, transportation and total sectors, back to 2010.
EIA's API v2 route (`electricity/retail-sales`) needs a key and is not used. Asked monthly with
`If-Modified-Since`, so an unchanged month costs a 304. robots.txt allows the path.

The figure is an average revenue per kWh, not a tariff and not what a new large load would pay
(docs/29 §2.12); the page says so. Months EIA marks `Preliminary` keep that status.
"""

from __future__ import annotations

import datetime as dt
import io
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import ParseError, RawSnapshot, raw_json
from pipeline.connectors.prices.base import Coverage, FetchRequest, PriceConnector

URL = "https://www.eia.gov/electricity/data/eia861m/xls/sales_revenue.xlsx"
SHEET = "Monthly-States"
SECTORS = ("residential", "commercial", "industrial", "transportation", "total")
_FIELDS = ("revenue_thousand_usd", "sales_mwh", "customers", "price_cents_kwh")
#: The 50 states and DC. `US` is accepted should the sheet carry a national row; it does not (2026-10-22).
STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY US".split()
)

RETAIL_COLUMNS: list[str] = [
    "record_id",
    "source_id",
    "source_record_id",
    "source_url",
    "retrieved_at",
    "licence_id",
    "state",
    "sector",
    "period",
    "price_cents_kwh",
    "revenue_thousand_usd",
    "sales_mwh",
    "customers",
    "data_status",
    "raw",
]


class Connector(PriceConnector):
    source_id: ClassVar[str] = "us.eia.retail_sales"
    market_code: ClassVar[str] = "EIA"
    honour_robots: ClassVar[bool] = True
    modes: ClassVar[tuple[str, ...]] = ("daily", "backfill")
    #: Not interval prices: the runner stores this frame as the retail table, not the interval store.
    frame_kind: ClassVar[str] = "retail"

    def plan(self, mode: str, today: dt.date, coverage: Coverage) -> list[FetchRequest]:
        if mode not in self.modes:
            raise ValueError(f"mode {mode!r} not supported by {self.source_id}")
        return [
            FetchRequest(
                key="sales-revenue", url=URL, ext="xlsx", meta={"kind": "workbook"}, conditional=True
            )
        ]

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        if raw.http_status == 304 or not raw.content:
            return []
        try:
            grid = pd.read_excel(io.BytesIO(raw.content), sheet_name=SHEET, header=None, dtype=object)
        except (ValueError, KeyError) as exc:
            raise ParseError(f"EIA workbook has no {SHEET!r} sheet: {exc}") from exc
        if grid.shape[1] < 24:
            raise ParseError(f"{SHEET} has {grid.shape[1]} columns, expected 24")
        sectors = [str(v).strip().lower() for v in grid.iloc[0, 4:24].ffill()]
        measures = [str(v).strip().lower() for v in grid.iloc[1, 4:24]]
        expected = [s for s in SECTORS for _ in range(4)]
        if sectors != expected or measures != ["revenue", "sales", "customers", "price"] * 5:
            raise ParseError(f"{SHEET} header changed: {sectors[:8]} / {measures[:4]}")
        out: list[dict[str, Any]] = []
        for _, row in grid.iloc[3:].iterrows():
            year, month, state, status = row.iloc[0], row.iloc[1], row.iloc[2], row.iloc[3]
            if not (isinstance(year, int | float) and isinstance(month, int | float)) or pd.isna(year):
                continue  # the notes block under the table
            state = str(state).strip().upper()
            if state not in STATES:
                continue
            values = list(row.iloc[4:24])
            for i, sector in enumerate(SECTORS):
                rev, sales, customers, price = values[i * 4 : i * 4 + 4]
                out.append(
                    {
                        "Year": int(year),
                        "Month": int(month),
                        "State": state,
                        "Data Status": None if pd.isna(status) else str(status),
                        "Sector": sector,
                        "Revenue (Thousand Dollars)": _num(rev),
                        "Sales (Megawatthours)": _num(sales),
                        "Customers (Count)": _num(customers),
                        "Price (Cents/kWh)": _num(price),
                    }
                )
        if not out:
            raise ParseError(f"{SHEET} held no state rows")
        return out

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        records = []
        for row in rows:
            period = f"{row['Year']:04d}-{row['Month']:02d}"
            key = f"{row['State']}|{row['Sector']}|{period}"
            records.append(
                {
                    "record_id": f"{self.source_id}:{key}",
                    "source_id": self.source_id,
                    "source_record_id": key,
                    "source_url": raw.url,
                    "retrieved_at": raw.retrieved_at_iso,
                    "licence_id": self.source.licence_id,
                    "state": row["State"],
                    "sector": row["Sector"],
                    "period": period,
                    "price_cents_kwh": row["Price (Cents/kWh)"],
                    "revenue_thousand_usd": row["Revenue (Thousand Dollars)"],
                    "sales_mwh": row["Sales (Megawatthours)"],
                    "customers": row["Customers (Count)"],
                    "data_status": row["Data Status"],
                    "raw": raw_json(row),
                }
            )
        df = pd.DataFrame(records, columns=RETAIL_COLUMNS)
        df.attrs["dropped"] = 0
        df.attrs["unknown_locations"] = []
        return df


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def release_label(raw_last_modified: str | None) -> str | None:
    """`Thu, 24 Sep 2026 13:48:22 GMT` -> `2026-09-24`: the publication date EIA's acknowledgment
    names (the file's `Last-Modified`)."""
    if not raw_last_modified:
        return None
    try:
        stamp = dt.datetime.strptime(raw_last_modified.strip(), "%a, %d %b %Y %H:%M:%S GMT").replace(
            tzinfo=dt.UTC
        )
        return stamp.date().isoformat()
    except ValueError:
        return None
