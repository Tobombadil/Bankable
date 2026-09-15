"""us.permits_dashboard — Federal Permitting Dashboard (FAST-41) full milestone dataset, Socrata.

Fetch: GET the bulk CSV export. `data/sources.yaml`'s original resource id (`q7va-8c67`) was
verified reachable on 2026-09-12 (HTTP 200, 24,008 bytes) but that "content" is 24,008 newline
bytes — the resource is retired: its Socrata view metadata (`/api/views/q7va-8c67.json`) reports
zero columns, and `/resource/q7va-8c67.json` returns empty `{}` rows. `/api/views.json?id=q7va-8c67`
resolves to a *different* asset (`cdh7-hgu4`, a map visualization) whose `modifyingViewUid` names
the real source table, `mcm3-xbid` ("Permitting Dashboard Full Dataset", 74 columns, ~24,000 rows,
matches the "milestone-level rows" description in `data/sources.yaml`'s notes exactly). This
connector fetches `mcm3-xbid`; `data/sources.yaml` is updated to match (verified 2026-09-12).
Parse: CSV, one row per (project, action, milestone) triple. Rows are grouped by `Project ID`
into one record per **project**, carrying every milestone as a nested list (the `raw` payload) —
this is the "documents table shape... per-project rows with milestone lists" the task asked for,
built the same way `pipeline/resolve.py`'s EIA plant rollup aggregates generator rows into one
plant record: group, take the project-level fields from the first row, fold the rest into a list.
Filter: kept only when `Project Sector` is one of the six sectors that are unambiguously energy or
transmission infrastructure: `Renewable Energy Production`, `Electricity Transmission`,
`Pipelines`, `Energy Storage`, `Conventional Energy Production`, `Carbon Capture and
Sequestration` (measured 2026-09-12: 104 of 458 projects). Excluded: `Surface Transportation`,
`Aviation`, `Mining`, `Water Resources`, `Broadband`, `Ports and Waterways`, `Manufacturing`,
`Data Storage and Data Management`, `High-performance computing...`, `Other` — mining and
data-centre/compute sectors are deliberately left out of *this* connector's scope even though the
platform tracks large loads elsewhere (`docs/00`), because this dataset's "Project Sector Type"
column for those two sectors names ore/metals and IT infrastructure, not power plants or grid
assets. `Project Sector Type` (the finer column, e.g. "Wind: Federal Offshore", "Interstate
Natural Gas Pipelines") feeds `technology`/`kind` via the shared `classify_tech` keyword rules.
lifecycle_state: derived from `Project Status` via `status_map.yaml` (`field: Status` is the only
literal `pipeline.normalize.harmonise_status` recognises, so the raw value travels through
`ctx["status_raw"]` under that name — same convention as every other connector's status map).
`Complete` maps to `permitted`, not `built`: this dataset tracks the *permitting* timeline
(NEPA reviews, agency consultations), not construction or commercial operation, so "all tracked
permitting actions complete" is exactly the `docs/21` §7.1 `permitted` state, no further evidence.
location: `state` from `Project Location State` (already USPS two-letter in this dataset).
capacity_mw: this dataset carries no MW column; the field stays null (`dq_required_fields`
excludes it so a permanently-null field cannot trigger a spurious null-spike gate).
source_record_id: `Project ID`. source_url: `Project Url` (a public permits.performance.gov page
per project) when the row carries one, else the dataset's landing page.
Reuse: US federal government work, public domain (`data/sources.yaml`); no gating needed.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import pathlib
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot
from pipeline.connectors.canonical import (
    classify_tech,
    harmonise_status,
    norm_county,
    norm_name,
    norm_org,
    norm_state,
    to_date,
)

CSV_URL = "https://data.permits.performance.gov/api/views/mcm3-xbid/rows.csv?accessType=DOWNLOAD"
DATASET_PAGE = "https://www.permits.performance.gov/"

# Measured 2026-09-12 against the live dataset (see module docstring for the excluded sectors).
ENERGY_SECTORS = frozenset(
    {
        "Renewable Energy Production",
        "Electricity Transmission",
        "Pipelines",
        "Energy Storage",
        "Conventional Energy Production",
        "Carbon Capture and Sequestration",
    }
)

MILESTONE_FIELDS = (
    "Milestone ID",
    "Milestone Type",
    "Milestone Short Description",
    "Milestone Target Completion Date",
    "Milestone Actual Completion Date",
    "Milestone Complete",
    "Action ID",
    "Action Type",
    "Action Status",
    "Action Responsible Agency",
    "Action Final Milestone",
)
PROJECT_FIELDS = (
    "Project ID",
    "Project",
    "Project Sector",
    "Project Sector Type",
    "Project Status",
    "Project Location State",
    "Project Location County",
    "Project Sponsor",
    "Project Lead Agency",
    "Total Estimated Project Cost",
    "Project Original Completion Date",
    "Project Url",
    "Project Reference Id",
)


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.permits_dashboard"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "csv"
    honour_robots: ClassVar[bool] = False  # Socrata bulk export API, not a browsed page
    status_key: ClassVar[str] = "permits_dashboard"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    dq_required_fields: ClassVar[tuple[str, ...]] = ("name_canonical", "technology_raw", "state")
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Project ID",
        "Project",
        "Project Sector",
        "Project Sector Type",
        "Project Status",
        "Project Location State",
    )

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        r = self.http.get(CSV_URL, honour_robots=False, timeout=180)
        if r.status_code != 200:
            raise ConnectorError(f"GET {CSV_URL} -> HTTP {r.status_code}")
        return RawSnapshot(
            content=r.content,
            content_type=r.headers.get("Content-Type", ""),
            url=CSV_URL,
            retrieved_at=dt.datetime.now(dt.UTC),
            http_status=r.status_code,
            ext="csv",
            headers=dict(r.headers),
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=1,
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        text = raw.content.decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows or "Project ID" not in rows[0]:
            raise ParseError(f"Permitting Dashboard layout changed: {list(rows[0]) if rows else 'empty'}")

        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row.get("Project Sector") not in ENERGY_SECTORS:
                continue
            pid = str(row.get("Project ID") or "").strip()
            if not pid:
                continue
            groups.setdefault(pid, []).append(row)

        projects: list[dict[str, Any]] = []
        for group in groups.values():
            first = group[0]
            project = {k: first.get(k) for k in PROJECT_FIELDS}
            project["milestones"] = [{k: r.get(k) for k in MILESTONE_FIELDS} for r in group]
            projects.append(project)
        return projects

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        def g(key: str) -> list[Any]:
            return [r.get(key) for r in rows]

        tech = [classify_tech(v) for v in g("Project Sector Type")]
        harmonised = [
            harmonise_status(self.status_key, {"status_raw": s}, self.status_map) for s in g("Project Status")
        ]
        df = pd.DataFrame(
            {
                "source_record_id": g("Project ID"),
                "source_url": [u or DATASET_PAGE for u in g("Project Url")],
                "kind": [k for _, k in tech],
                "name_canonical": g("Project"),
                "name_norm": [norm_name(v) for v in g("Project")],
                "sponsor_name": g("Project Sponsor"),
                "sponsor_norm": [norm_org(v) for v in g("Project Sponsor")],
                "technology": [t for t, _ in tech],
                "technology_raw": g("Project Sector Type"),
                "capacity_mw": pd.array([None] * len(rows), dtype="Float64"),
                "storage_mwh": pd.array([None] * len(rows), dtype="Float64"),
                "iso": None,
                "state": [norm_state(v) for v in g("Project Location State")],
                "county": g("Project Location County"),
                "county_norm": [norm_county(v) for v in g("Project Location County")],
                "lifecycle_state": [s for s, _ in harmonised],
                "status_raw": g("Project Status"),
                "status_rule": [r for _, r in harmonised],
                "status_conflict": False,
                "queue_date": pd.NaT,
                "proposed_cod": [to_date(v) for v in g("Project Original Completion Date")],
                "queue_id": None,
                "eia_plant_id": None,
                "eia_generator_id": None,
                "cross_refs": "",
            }
        )
        return self.finalize(df, rows, raw)
