"""EIA "U.S. Fuel Ethanol Plant Production Capacity" annual table -> `ethanol_plant` asset rows
(`us.eia.ethanol_capacity`, data/sources.yaml section K; ADR 0008; owner decision 2026-09-19).

The workbook (`ethanolcapacity.xlsx`, one sheet "Ethanol Plant Listing <year>") is a report
layout, not a table: a title row carrying the as-of date ("... as of January 1, 2025"), a header
row (`State, Respondent, City, MMgal/yr, Mb/d`), then PADD subtotal rows, one row per plant with
the state name only on the first plant of each state, a "U.S. Total" row and footnotes. The
parser keys plant rows on a non-empty Respondent, forward-fills the state, and treats EIA's
`(s)` symbol ("less than 0.5 MMgal/yr") as a suppressed value, not a zero.

Objective feature set (docs/00-PLAN.md 2026-09-18: objective, no valuation): nameplate capacity in
million gallons per year (`capacity_value`, unit `MMgal/yr`; also `attributes.
nameplate_capacity_mmgal_yr`), the as-of year, the respondent string (the EIA-819 respondent,
i.e. the operating company; `operator_name` and `owner_raw`), city and PADD. The table carries no
feedstock and no coordinate: `feedstock` is null and the row is unplaced (state centroid on the
`centroid_*` columns, `placement_precision = state_centroid`), because a city name is not a
coordinate. Every listed plant is an operating plant by construction of the table (capacity in
operation on January 1), so `status = operating`.

Identity: no id in the source, so `source_asset_id` is `<STATE>-<respondent slug>-<city slug>`
(docs/20 §3.1 content key); a second row with the same three fields gets a `-2` suffix.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import re
import time
from typing import Any

import pandas as pd

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import PoliteSession
from pipeline.context import fuels
from pipeline.normalize import US_STATES
from services.ids import slugify

SOURCE_ID = "us.eia.ethanol_capacity"
PAGE_URL = "https://www.eia.gov/petroleum/ethanolcapacity/"
WORKBOOK_URL = PAGE_URL + "ethanolcapacity.xlsx"
DEFAULT_OUT = fuels.CONTEXT_DIR / f"{SOURCE_ID}.parquet"
SUPPRESSED = "(s)"
SUPPRESSED_NOTE = "less than 0.5 MMgal/yr (EIA suppression symbol)"

PARSED_COLUMNS = ["state_name", "padd", "respondent", "city", "capacity_mmgal_yr", "capacity_suppressed"]


def fetch_workbook(session: PoliteSession) -> tuple[bytes, str]:
    content = fuels.fetch(session, WORKBOOK_URL)
    if not content.startswith(b"PK"):
        raise ParseError(f"{WORKBOOK_URL} did not return an xlsx (first bytes {content[:8]!r})")
    return content, WORKBOOK_URL


def parse_capacity_table(content: bytes) -> tuple[pd.DataFrame, int | None]:
    """Plant rows only, with the state forward-filled and the PADD each row sits under.

    Returns `(frame[PARSED_COLUMNS], as_of_year)`; `as_of_year` is read from the title cell and
    is `None` if EIA changes that wording (the sheet still parses).
    """
    raw = pd.read_excel(io.BytesIO(content), header=None, dtype=object, engine="openpyxl")
    if raw.empty:
        raise ParseError("ethanol capacity workbook is empty")
    title = fuels.clean_str(raw.iat[0, 0]) or ""
    m = re.search(r"January 1,\s*(\d{4})", title)
    as_of_year = int(m.group(1)) if m else None

    header_idx = None
    for i in range(min(len(raw), 10)):
        if fuels.clean_str(raw.iat[i, 0]) == "State" and fuels.clean_str(raw.iat[i, 1]) == "Respondent":
            header_idx = i
            break
    if header_idx is None:
        raise ParseError(f"ethanol capacity layout changed: no 'State | Respondent' header in {title!r}")
    header = [fuels.clean_str(c) for c in raw.iloc[header_idx]]
    try:
        cap_idx = header.index("MMgal/yr")
        city_idx = header.index("City")
    except ValueError as e:
        raise ParseError(f"ethanol capacity layout changed: header {header}") from e

    rows: list[dict[str, Any]] = []
    state_name: str | None = None
    padd: str | None = None
    for i in range(header_idx + 1, len(raw)):
        first = fuels.clean_str(raw.iat[i, 0])
        respondent = fuels.clean_str(raw.iat[i, 1])
        if first and first.upper().startswith("PADD"):
            padd = first
            continue
        if respondent is None:
            continue  # U.S. Total, footnotes, blank spacer rows
        if first:
            state_name = first
        cap_text = fuels.clean_str(raw.iat[i, cap_idx])
        suppressed = cap_text == SUPPRESSED
        rows.append(
            {
                "state_name": state_name,
                "padd": padd,
                "respondent": respondent,
                "city": fuels.clean_str(raw.iat[i, city_idx]),
                "capacity_mmgal_yr": None if suppressed else fuels.to_float(cap_text),
                "capacity_suppressed": suppressed,
            }
        )
    if not rows:
        raise ParseError("ethanol capacity workbook parsed to zero plant rows")
    return pd.DataFrame(rows, columns=PARSED_COLUMNS), as_of_year


def _state_code(state_name: str | None) -> str | None:
    if not state_name:
        return None
    code = US_STATES.get(state_name.strip().lower())
    return str(code) if code else None


def build_assets(
    df: pd.DataFrame,
    *,
    retrieved_at: str,
    source_url: str,
    as_of_year: int | None,
    licence_id: str,
    placement: fuels.Placement | None = None,
) -> pd.DataFrame:
    place = placement or fuels.Placement()
    seen: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for rec in df.to_dict("records"):
        code = _state_code(rec.get("state_name"))
        respondent = str(rec["respondent"])
        city = fuels.clean_str(rec.get("city"))
        base = (
            f"{code or 'XX'}-{slugify(respondent, max_length=40)}-{slugify(city or 'unknown', max_length=30)}"
        )
        n = seen.get(base, 0) + 1
        seen[base] = n
        key = base if n == 1 else f"{base}-{n}"
        placed = place.resolve(code, None)
        rows.append(
            fuels.asset_row(
                source_id=SOURCE_ID,
                source_url=source_url,
                retrieved_at=retrieved_at,
                licence_id=licence_id,
                source_asset_id=key,
                name=f"{respondent} ({city}, {code})" if city and code else respondent,
                operator_name=respondent,
                owner_raw=respondent,
                status="operating",
                status_raw="listed with production capacity in operation",
                technology="ethanol",
                technology_raw=None,
                capacity_value=rec.get("capacity_mmgal_yr"),
                capacity_unit="MMgal/yr",
                state_code=fuels.state_code(code),
                attributes=fuels.numeric_attributes(
                    {"nameplate_capacity_mmgal_yr": rec.get("capacity_mmgal_yr"), "as_of_year": as_of_year}
                ),
                attributes_text=fuels.text_attributes(
                    {
                        "city": city,
                        "padd": rec.get("padd"),
                        "state_name": rec.get("state_name"),
                        "capacity_note": SUPPRESSED_NOTE if rec.get("capacity_suppressed") else None,
                    }
                ),
                raw={k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in rec.items()},
                **placed,
            )
        )
    return fuels.assemble(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--fetch", action="store_true", help="Fetch ethanolcapacity.xlsx and snapshot it")
    group.add_argument("--workbook", type=pathlib.Path, help="Parse this .xlsx instead of a snapshot")
    group.add_argument(
        "--latest-snapshot", action="store_true", help="Newest data/snapshots snapshot (default)"
    )
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--manifest", type=pathlib.Path, help="Alternate data/sources.yaml (default: the repo's)"
    )
    args = parser.parse_args(argv)

    t0 = time.monotonic()
    entry = fuels.entry_for(SOURCE_ID, args.manifest)
    if args.fetch:
        session = fuels.polite_session()
        content, url = fetch_workbook(session)
        path, retrieved_at = fuels.record_snapshot(
            SOURCE_ID,
            content,
            ext="xlsx",
            fetched_url=url,
            retrieved_at=fuels.utc_now(),
            requests_made=session.requests_made,
        )
        source_url = url
    else:
        path = args.workbook or fuels.latest_snapshot(SOURCE_ID, "xlsx")
        retrieved_at, source_url = fuels.snapshot_metadata(path, SOURCE_ID, WORKBOOK_URL)

    table, as_of_year = parse_capacity_table(path.read_bytes())
    assets = build_assets(
        table,
        retrieved_at=retrieved_at,
        source_url=source_url,
        as_of_year=as_of_year,
        licence_id=entry.licence_id,
    )
    fuels.write_context_parquet(assets, args.out)
    summary = fuels.summarise(
        assets,
        as_of_year=as_of_year,
        suppressed_capacity=int(table["capacity_suppressed"].sum()),
        total_capacity_mmgal_yr=round(float(table["capacity_mmgal_yr"].sum()), 3),
        snapshot=str(path),
        out=str(args.out),
        elapsed_s=round(time.monotonic() - t0, 2),
    )
    print(json.dumps(summary))  # noqa: T201 — CLI summary line, same convention as eia_plants.py


if __name__ == "__main__":
    main()
