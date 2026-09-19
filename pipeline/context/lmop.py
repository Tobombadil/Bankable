"""EPA LMOP Landfill and Project Database -> `rng_project` asset rows (`us.epa.lmop`,
data/sources.yaml section K; ADR 0008; owner decision 2026-09-19).

Source: the composite workbook linked from the LMOP database page (`lmopcompositedata.xlsx`,
sheets "Summary", "LMOP Database", "Field Descriptions"). The page's link path carries the build
month (`/system/files/documents/<YYYY-MM>/lmopcompositedata.xlsx`), so `fetch_workbook` reads
the page first and follows whatever link it finds rather than hard-coding a month.

Row model. Every row of "LMOP Database" pairs one landfill with one "project" slot; the
`Current Project Status` vocabulary (quoted on the Summary sheet) mixes real projects
(Operational, Construction, Planned, Shutdown) with landfill-potential classes (Candidate, Future
Potential, Low Potential, Unknown). Only the first group describes an LFG energy project:

- Operational -> `status = operating`; Shutdown -> `retired`: these are the asset rows.
- Construction and Planned are proposals (ADR 0008 §1) and are returned separately by
  `build_rows` (written next to the asset parquet as `<source_id>.proposals.parquet` so the
  proposals lane can pick them up); they never enter the asset frame.
- The potential classes are neither and are dropped with a count.

Identity is `Project ID` ("parent project and expansion number", per the Field Descriptions
sheet). A project fed by several landfills repeats its Project ID once per landfill (30 such ids
in the 2024-09 build); the parser collapses those to one asset, keeps the first landfill's
coordinate and county, lists every landfill in `attributes_text` and counts them in
`attributes.landfill_count`.

Objective feature set: project type category and LFG energy project type (`technology` =
`lfg_electricity` | `rng` | `lfg_direct_use`, `technology_raw` = the specific type), RNG delivery
method and LFG use details, rated and actual MW (`capacity_mw` = rated), LFG flow to project in
mmscfd (`capacity_value`, unit `mmscfd`), start/shutdown years, end users, owner and developer
strings (`owner_raw`, `developer_raw`; `operator_name` = owner, else developer), plus the host
landfill's name, status, owner/operator, waste in place and LFG collected. Coordinates come from
the landfill's own Latitude/Longitude (exact); a project row without one is left unplaced with
its county FIPS and centroid on the `centroid_*` columns.

Licence: EPA-authored database, 17 U.S.C. §105, under EPA's own copyright hedge (docs/13 §2.12).
That section asks for a cover-sheet check per workbook: `parse_workbook` returns the Summary
sheet's text so the run can record what notice, if any, the file carries (the 2024-09 build
carries a data-quality caveat and the status vocabulary, and no copyright or reuse notice).
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

SOURCE_ID = "us.epa.lmop"
PAGE_URL = "https://www.epa.gov/lmop/lmop-landfill-and-project-database"
DEFAULT_OUT = fuels.CONTEXT_DIR / f"{SOURCE_ID}.parquet"
DEFAULT_PROPOSALS_OUT = fuels.CONTEXT_DIR / f"{SOURCE_ID}.proposals.parquet"
SHEET = "LMOP Database"
SUMMARY_SHEET = "Summary"

_COMPOSITE_RE = re.compile(
    r'href="(https://www\.epa\.gov/system/files/[^"]*?/lmopcompositedata\.xlsx)"', re.IGNORECASE
)

ASSET_STATUS = {"Operational": "operating", "Shutdown": "retired"}
PROPOSAL_STATUSES = ("Construction", "Planned")
CATEGORY_TECH = {"Electricity": "lfg_electricity", "Renewable Natural Gas": "rng", "Direct": "lfg_direct_use"}
REQUIRED = ("Landfill ID", "Landfill Name", "Project ID", "Current Project Status", "State")

# Source column names, spelled once.
C_LF_ID = "Landfill ID"
C_LF_NAME = "Landfill Name"
C_STATE = "State"
C_CITY = "City"
C_COUNTY = "County"
C_LAT = "Latitude"
C_LON = "Longitude"
C_LF_OWNER_TYPE = "Ownership Type"
C_LF_OWNER = "Landfill Owner Organization(s)"
C_LF_OPERATOR = "Landfill Operator Organization"
C_LF_OPENED = "Year Landfill Opened"
C_LF_CLOSURE = "Landfill Closure Year"
C_LF_STATUS = "Current Landfill Status"
C_LF_DESIGN_TONS = "Landfill Design Capacity (tons)"
C_LF_WIP = "Waste in Place (tons)"
C_LF_WIP_YEAR = "Waste in Place Year"
C_LF_LFG_COLLECTED = "LFG Collected (mmscfd)"
C_LF_LFG_GENERATED = "LFG Generated (mmscfd)"
C_GHGRP = "GHGRP ID"
C_PROJECT_ID = "Project ID"
C_STATUS = "Current Project Status"
C_PROJECT_NAME = "Project Name"
C_START = "Project Start Date"
C_SHUTDOWN = "Project Shutdown Date"
C_CATEGORY = "Project Type Category"
C_TYPE = "LFG Energy Project Type"
C_RNG_DELIVERY = "RNG Delivery Method"
C_USE_DETAILS = "LFG Use Details"
C_ACTUAL_MW = "Actual MW Generation"
C_RATED_MW = "Rated MW Capacity"
C_LFG_FLOW = "LFG Flow to Project (mmscfd)"
C_POTENTIAL_END_USERS = "Potential End Users"
C_END_USERS = "End User(s)"
C_DEVELOPERS = "Project Developer(s)"
C_OWNERS = "Project Owner(s)"
C_SUPPLIERS = "Product Supplier(s)"
C_SELF_DEVELOPED = "Self-Developed"
C_RED_DIRECT = "Current Year Emission Reductions (MMTCO2e/yr) - Direct"
C_RED_AVOIDED = "Current Year Emission Reductions (MMTCO2e/yr) - Avoided"


def find_composite_url(page_html: bytes) -> str:
    m = _COMPOSITE_RE.search(page_html.decode("utf-8", errors="replace"))
    if not m:
        raise ParseError(f"no lmopcompositedata.xlsx link on {PAGE_URL}")
    return m.group(1)


def fetch_workbook(session: PoliteSession) -> tuple[bytes, str]:
    page = fuels.fetch(session, PAGE_URL)
    url = find_composite_url(page)
    content = fuels.fetch(session, url)
    if not content.startswith(b"PK"):
        raise ParseError(f"{url} did not return an xlsx (first bytes {content[:8]!r})")
    return content, url


def parse_workbook(content: bytes) -> tuple[pd.DataFrame, str]:
    """`(database_frame, summary_text)`. Values are read as objects (never coerced) so ids such
    as `1016-0` and the `Project Start Date` datetimes survive; the Summary sheet's text is the
    docs/13 §2.12 cover-sheet evidence."""
    book = pd.read_excel(
        io.BytesIO(content), sheet_name=[SHEET, SUMMARY_SHEET], header=None, dtype=object, engine="openpyxl"
    )
    raw = book[SHEET]
    if raw.empty:
        raise ParseError(f"sheet {SHEET!r} is empty")
    header = [fuels.clean_str(c) for c in raw.iloc[0]]
    df = raw.iloc[1:].reset_index(drop=True)
    df.columns = [h if h is not None else f"col_{i}" for i, h in enumerate(header)]
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ParseError(f"LMOP layout changed: missing {missing}; header {header[:12]}")
    summary_text = "\n".join(
        s for v in book[SUMMARY_SHEET].to_numpy().ravel().tolist() if (s := fuels.clean_str(v)) is not None
    )
    return df, summary_text


def _project_rows(
    project_id: str,
    g: pd.DataFrame,
    *,
    status: str,
    status_raw: str,
    retrieved_at: str,
    source_url: str,
    licence_id: str,
    place: fuels.Placement,
) -> dict[str, Any]:
    first = g.iloc[0]
    landfill_names = [n for n in (fuels.clean_str(v) for v in g[C_LF_NAME]) if n]
    landfill_ids = [n for n in (fuels.clean_str(v) for v in g[C_LF_ID]) if n]
    category = fuels.clean_str(first.get(C_CATEGORY))
    project_name = fuels.clean_str(first.get(C_PROJECT_NAME))
    landfill_name = (
        landfill_names[0] if landfill_names else f"Landfill {landfill_ids[0] if landfill_ids else '?'}"
    )
    name = (
        f"{project_name} - {landfill_name}"
        if project_name
        else f"{landfill_name} {category or 'LFG'} project"
    )
    owner = fuels.clean_str(first.get(C_OWNERS))
    developer = fuels.clean_str(first.get(C_DEVELOPERS))
    state = fuels.clean_str(first.get(C_STATE))
    county = fuels.clean_str(first.get(C_COUNTY))
    point = fuels.valid_point(first.get(C_LAT), first.get(C_LON))
    lon, lat = point if point is not None else (None, None)
    placed = place.resolve(state, county)
    if point is not None:
        placed.update({"placement_precision": "exact", "centroid_lon": None, "centroid_lat": None})
    rated_mw = fuels.to_float(first.get(C_RATED_MW))
    raw = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in first.to_dict().items()}
    return fuels.asset_row(
        source_id=SOURCE_ID,
        source_url=source_url,
        retrieved_at=retrieved_at,
        licence_id=licence_id,
        source_asset_id=project_id,
        name=name,
        operator_name=owner or developer,
        owner_raw=owner,
        developer_raw=developer,
        status=status,
        status_raw=status_raw,
        technology=CATEGORY_TECH.get(category or "", "unknown"),
        technology_raw=fuels.clean_str(first.get(C_TYPE)),
        capacity_mw=rated_mw,
        capacity_value=fuels.to_float(first.get(C_LFG_FLOW)),
        capacity_unit="mmscfd",
        commissioned_year=fuels.to_year(first.get(C_START)),
        lon=lon,
        lat=lat,
        state_code=fuels.state_code(state),
        county_name=county,
        attributes=fuels.numeric_attributes(
            {
                "rated_mw": rated_mw,
                "actual_mw_generation": first.get(C_ACTUAL_MW),
                "lfg_flow_to_project_mmscfd": first.get(C_LFG_FLOW),
                "landfill_count": len(g),
                "project_start_year": fuels.to_year(first.get(C_START)),
                "project_shutdown_year": fuels.to_year(first.get(C_SHUTDOWN)),
                "landfill_year_opened": first.get(C_LF_OPENED),
                "landfill_closure_year": first.get(C_LF_CLOSURE),
                "landfill_design_capacity_tons": first.get(C_LF_DESIGN_TONS),
                "landfill_waste_in_place_tons": first.get(C_LF_WIP),
                "landfill_waste_in_place_year": first.get(C_LF_WIP_YEAR),
                "landfill_lfg_generated_mmscfd": first.get(C_LF_LFG_GENERATED),
                "landfill_lfg_collected_mmscfd": first.get(C_LF_LFG_COLLECTED),
                "emission_reductions_direct_mmtco2e_yr": first.get(C_RED_DIRECT),
                "emission_reductions_avoided_mmtco2e_yr": first.get(C_RED_AVOIDED),
            }
        ),
        attributes_text=fuels.text_attributes(
            {
                "project_type_category": category,
                "lfg_energy_project_type": first.get(C_TYPE),
                "rng_delivery_method": first.get(C_RNG_DELIVERY),
                "lfg_use_details": first.get(C_USE_DETAILS),
                "end_users": first.get(C_END_USERS),
                "potential_end_users": first.get(C_POTENTIAL_END_USERS),
                "product_suppliers": first.get(C_SUPPLIERS),
                "self_developed": first.get(C_SELF_DEVELOPED),
                "project_name": project_name,
                "landfill_name": landfill_name,
                "landfill_names": "; ".join(landfill_names) if len(landfill_names) > 1 else None,
                "landfill_id": landfill_ids[0] if landfill_ids else None,
                "landfill_ids": "; ".join(landfill_ids) if len(landfill_ids) > 1 else None,
                "landfill_status": first.get(C_LF_STATUS),
                "landfill_ownership_type": first.get(C_LF_OWNER_TYPE),
                "landfill_owner": first.get(C_LF_OWNER),
                "landfill_operator": first.get(C_LF_OPERATOR),
                "ghgrp_id": first.get(C_GHGRP),
                "city": first.get(C_CITY),
            }
        ),
        raw=raw,
        **placed,
    )


def build_rows(
    df: pd.DataFrame,
    *,
    retrieved_at: str,
    source_url: str,
    licence_id: str,
    placement: fuels.Placement | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    """`(assets, proposals, dropped_by_status)`: one row per Project ID for the asset statuses
    and, separately, for the proposal statuses; landfill-potential rows counted and dropped."""
    place = placement or fuels.Placement()
    assets: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    work = df[df[C_PROJECT_ID].map(fuels.clean_str).notna()]
    for project_id, g in work.groupby(work[C_PROJECT_ID].map(lambda v: str(fuels.clean_str(v))), sort=False):
        status_raw = fuels.clean_str(g.iloc[0].get(C_STATUS)) or "Unknown"
        if status_raw in ASSET_STATUS:
            target, status = assets, ASSET_STATUS[status_raw]
        elif status_raw in PROPOSAL_STATUSES:
            target, status = proposals, "unknown"
        else:
            dropped[status_raw] = dropped.get(status_raw, 0) + 1
            continue
        target.append(
            _project_rows(
                str(project_id),
                g,
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
    group.add_argument("--fetch", action="store_true", help="Fetch the composite workbook and snapshot it")
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

    df, summary_text = parse_workbook(path.read_bytes())
    assets, proposals, dropped = build_rows(
        df, retrieved_at=retrieved_at, source_url=source_url, licence_id=entry.licence_id
    )
    fuels.write_context_parquet(assets, args.out)
    fuels.write_context_parquet(proposals, args.proposals_out)
    notice = [
        line for line in summary_text.splitlines() if re.search(r"copyright|licen[cs]e|reuse|©", line, re.I)
    ]
    summary = fuels.summarise(
        assets,
        source_rows=len(df),
        proposals=len(proposals),
        dropped_landfill_potential_rows=dropped,
        cover_sheet_notice=notice or "none found on the Summary sheet",
        snapshot=str(path),
        out=str(args.out),
        elapsed_s=round(time.monotonic() - t0, 2),
    )
    print(json.dumps(summary))  # noqa: T201 — CLI summary line, same convention as eia_plants.py


if __name__ == "__main__":
    main()
