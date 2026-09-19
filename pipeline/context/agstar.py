"""EPA AgSTAR Livestock Anaerobic Digester Database -> `rng_project` asset rows (`us.epa.agstar`,
data/sources.yaml section K; ADR 0008; owner decision 2026-09-19).

Source: `agstar-livestock-ad-database.xlsx`, linked from the AgSTAR database page (the link path
carries an upload month, so `fetch_workbook` reads the page and follows the link it finds). Two
sheets, both with the header on the first row: "Operational and Construction" and "Shutdown".
The Shutdown sheet has the same fields in a slightly different order plus `Year Shutdown` and
`Reason for Closure`; both are read by column name, never by position.

Status: Operational -> `operating`, Shut down -> `retired` (asset rows); Construction rows are
proposals (ADR 0008 §1), returned separately by `build_rows` and written next to the asset
parquet as `<source_id>.proposals.parquet`.

Identity: the table has no id, so `source_asset_id` is a content key over (Project Name, State)
(docs/20 §3.1); the 2024-06 build has no duplicate pair inside or across the two sheets.

Objective feature set: digester type (`technology_raw`; `technology = farm_digester`), animal or
farm type as the feedstock (`feedstock`), biogas end use(s), project type (farm scale,
centralised, multiple farm, research), biogas generation estimate in cu-ft/day
(`capacity_value`, unit `cu-ft/day`), electricity generated in kWh/yr, head counts by animal,
co-digestion feedstock, LCFS pathway flag, receiving utility, USDA funding flag, total emission
reductions, year operational (`commissioned_year`), year and reason of shutdown, and the
designer/developer string (`developer_raw`; the farm itself is the operator and is named in the
project name, so `operator_name` is left null rather than guessed).

Placement: the table gives city and county, no coordinate, so every row is unplaced for the
loader and carries its county FIPS and county centroid on the `centroid_*` columns
(`placement_precision = county_centroid`), as the manifest note prescribes ("geocode through
the Census gazetteer like proposals").

Licence: EPA-authored database, 17 U.S.C. §105, under EPA's own copyright hedge (docs/13 §2.12).
The workbook has no cover sheet; the page (read 2026-09-19) carries a data-accuracy caveat and
a no-endorsement line, and no copyright or reuse notice.
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

SOURCE_ID = "us.epa.agstar"
PAGE_URL = "https://www.epa.gov/agstar/livestock-anaerobic-digester-database"
DEFAULT_OUT = fuels.CONTEXT_DIR / f"{SOURCE_ID}.parquet"
DEFAULT_PROPOSALS_OUT = fuels.CONTEXT_DIR / f"{SOURCE_ID}.proposals.parquet"
SHEETS = ("Operational and Construction", "Shutdown")

_WORKBOOK_RE = re.compile(
    r'href="(https://www\.epa\.gov/[^"]*?/agstar-livestock-ad-database\.xlsx)"', re.IGNORECASE
)

ASSET_STATUS = {"Operational": "operating", "Shut down": "retired"}
PROPOSAL_STATUSES = ("Construction",)
REQUIRED = ("Project Name", "State", "Status", "Digester Type")

C_NAME = "Project Name"
C_CLUSTER = "Cluster Name"
C_PROJECT_TYPE = "Project Type"
C_CITY = "City"
C_COUNTY = "County"
C_STATE = "State"
C_DIGESTER = "Digester Type"
C_STATUS = "Status"
C_YEAR_OP = "Year Operational"
C_YEAR_SHUT = "Year Shutdown"
C_REASON = "Reason for Closure"
C_ANIMALS = "Animal/Farm Type(s)"
C_CATTLE = "Cattle"
C_DAIRY = "Dairy"
C_POULTRY = "Poultry"
C_SWINE = "Swine"
C_CODIGESTION = "Co-Digestion"
C_BIOGAS = "Biogas Generation Estimate (cu-ft/day)"
C_KWH = "Electricity Generated (kWh/yr)"
C_END_USE = "Biogas End Use(s)"
C_LCFS = "LCFS Pathway"
C_DEVELOPERS = "System Designer(s)/Developer(s) and Affiliates"
C_UTILITY = "Receiving Utility"
C_REDUCTIONS = "Total Emission Reductions (MTCO2e/yr)"
C_USDA = "Awarded USDA Funding?"


def find_workbook_url(page_html: bytes) -> str:
    m = _WORKBOOK_RE.search(page_html.decode("utf-8", errors="replace"))
    if not m:
        raise ParseError(f"no agstar-livestock-ad-database.xlsx link on {PAGE_URL}")
    return m.group(1)


def fetch_workbook(session: PoliteSession) -> tuple[bytes, str]:
    page = fuels.fetch(session, PAGE_URL)
    url = find_workbook_url(page)
    content = fuels.fetch(session, url)
    if not content.startswith(b"PK"):
        raise ParseError(f"{url} did not return an xlsx (first bytes {content[:8]!r})")
    return content, url


def parse_workbook(content: bytes) -> pd.DataFrame:
    """Both sheets stacked, read by column name, with a `sheet` column saying which one."""
    book = pd.read_excel(
        io.BytesIO(content), sheet_name=list(SHEETS), header=0, dtype=object, engine="openpyxl"
    )
    frames = []
    for sheet in SHEETS:
        df = book[sheet]
        df.columns = [str(c).strip() for c in df.columns]
        missing = [c for c in REQUIRED if c not in df.columns]
        if missing:
            raise ParseError(f"AgSTAR sheet {sheet!r} layout changed: missing {missing}")
        df = df[df[C_NAME].map(fuels.clean_str).notna()].copy()
        df["sheet"] = sheet
        frames.append(df)
    out = pd.concat(frames, ignore_index=True, sort=False)
    if out.empty:
        raise ParseError("AgSTAR workbook parsed to zero project rows")
    return out


def _row(
    rec: dict[str, Any],
    *,
    status: str,
    status_raw: str,
    retrieved_at: str,
    source_url: str,
    licence_id: str,
    place: fuels.Placement,
) -> dict[str, Any]:
    name = str(fuels.clean_str(rec.get(C_NAME)))
    state = fuels.clean_str(rec.get(C_STATE))
    county = fuels.clean_str(rec.get(C_COUNTY))
    year_op = fuels.to_year(rec.get(C_YEAR_OP))
    return fuels.asset_row(
        source_id=SOURCE_ID,
        source_url=source_url,
        retrieved_at=retrieved_at,
        licence_id=licence_id,
        source_asset_id=fuels.content_key(name, state),
        name=name,
        operator_name=None,
        developer_raw=fuels.clean_str(rec.get(C_DEVELOPERS)),
        status=status,
        status_raw=status_raw,
        technology="farm_digester",
        technology_raw=fuels.clean_str(rec.get(C_DIGESTER)),
        feedstock=fuels.clean_str(rec.get(C_ANIMALS)),
        capacity_value=fuels.to_float(rec.get(C_BIOGAS)),
        capacity_unit="cu-ft/day",
        commissioned_year=year_op,
        state_code=fuels.state_code(state),
        county_name=county,
        attributes=fuels.numeric_attributes(
            {
                "biogas_generation_estimate_cuft_day": rec.get(C_BIOGAS),
                "electricity_generated_kwh_yr": rec.get(C_KWH),
                "cattle": rec.get(C_CATTLE),
                "dairy": rec.get(C_DAIRY),
                "poultry": rec.get(C_POULTRY),
                "swine": rec.get(C_SWINE),
                "total_emission_reductions_mtco2e_yr": rec.get(C_REDUCTIONS),
                "year_operational": year_op,
                "year_shutdown": fuels.to_year(rec.get(C_YEAR_SHUT)),
                "lcfs_pathway": (fuels.clean_str(rec.get(C_LCFS)) or "").lower() in {"yes", "y"},
                "awarded_usda_funding": (fuels.clean_str(rec.get(C_USDA)) or "").lower() in {"yes", "y"},
            }
        ),
        attributes_text=fuels.text_attributes(
            {
                "project_type": rec.get(C_PROJECT_TYPE),
                "cluster_name": rec.get(C_CLUSTER),
                "digester_type": rec.get(C_DIGESTER),
                "animal_farm_types": rec.get(C_ANIMALS),
                "co_digestion": rec.get(C_CODIGESTION),
                "biogas_end_uses": rec.get(C_END_USE),
                "receiving_utility": rec.get(C_UTILITY),
                "reason_for_closure": rec.get(C_REASON),
                "city": rec.get(C_CITY),
                "sheet": rec.get("sheet"),
            }
        ),
        raw={k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in rec.items()},
        **place.resolve(state, county),
    )


def build_rows(
    df: pd.DataFrame,
    *,
    retrieved_at: str,
    source_url: str,
    licence_id: str,
    placement: fuels.Placement | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """`(assets, proposals, dropped_by_status)`."""
    place = placement or fuels.Placement()
    assets: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    for rec in df.to_dict("records"):
        status_raw = fuels.clean_str(rec.get(C_STATUS)) or "Unknown"
        if status_raw in ASSET_STATUS:
            target, status = assets, ASSET_STATUS[status_raw]
        elif status_raw in PROPOSAL_STATUSES:
            target, status = proposals, "unknown"
        else:
            dropped[status_raw] = dropped.get(status_raw, 0) + 1
            continue
        target.append(
            _row(
                rec,
                status=status,
                status_raw=status_raw,
                retrieved_at=retrieved_at,
                source_url=source_url,
                licence_id=licence_id,
                place=place,
            )
        )
    return fuels.assemble(assets), fuels.assemble(proposals), dropped


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--fetch", action="store_true", help="Fetch the AgSTAR workbook and snapshot it")
    group.add_argument("--workbook", type=pathlib.Path, help="Parse this .xlsx instead of a snapshot")
    group.add_argument(
        "--latest-snapshot", action="store_true", help="Newest data/snapshots snapshot (default)"
    )
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--manifest", type=pathlib.Path, help="Alternate data/sources.yaml (default: the repo's)"
    )
    parser.add_argument("--proposals-out", type=pathlib.Path, default=DEFAULT_PROPOSALS_OUT)
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
            meta={"page_url": PAGE_URL},
        )
        source_url = url
    else:
        path = args.workbook or fuels.latest_snapshot(SOURCE_ID, "xlsx")
        retrieved_at, source_url = fuels.snapshot_metadata(path, SOURCE_ID, PAGE_URL)

    df = parse_workbook(path.read_bytes())
    assets, proposals, dropped = build_rows(
        df, retrieved_at=retrieved_at, source_url=source_url, licence_id=entry.licence_id
    )
    fuels.write_context_parquet(assets, args.out)
    fuels.write_context_parquet(proposals, args.proposals_out)
    summary = fuels.summarise(
        assets,
        source_rows=len(df),
        proposals=len(proposals),
        dropped=dropped,
        county_placed=int((assets["placement_precision"] == "county_centroid").sum()) if len(assets) else 0,
        snapshot=str(path),
        out=str(args.out),
        elapsed_s=round(time.monotonic() - t0, 2),
    )
    print(json.dumps(summary))  # noqa: T201 — CLI summary line, same convention as eia_plants.py


if __name__ == "__main__":
    main()
