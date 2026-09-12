"""gb.neso.tec_register — NESO Transmission Entry Capacity (TEC) Register.

Fetch: CKAN `package_show` names the current CSV resource (renamed each publication, e.g.
`tec-register-11-september-2026.csv`), which redirects to object storage.
Parse: CSV with a UTF-8 BOM; 15 columns including "Project ID", "Project Number", "Stage".
source_record_id: `<Project ID>/<Stage>` where the project is split into MW stages (144 projects
have 2–5 stage rows with different effective dates), else `<Project ID>/<hash>` over the capacity,
effective date and status of the row — one project (a0l4L0000005im7QAA, VPI Immingham) files a
built row and an unstaged future increase under the same id, so the project id alone is not a key.
Reuse: NESO Open Data Licence v1.0 — attribution "Supported by National Energy SO Open Data".
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
from pipeline.connectors.base import ConnectorError, ParseError, RawSnapshot, content_hash
from pipeline.normalize import classify_tech, harmonise_status, norm_name, norm_org, to_date, to_float

PACKAGE_URL = "https://api.neso.energy/api/3/action/package_show?id=transmission-entry-capacity-tec-register"
DATASET_PAGE = "https://www.neso.energy/data-portal/transmission-entry-capacity-tec-register"


class Connector(BaseConnector):
    source_id: ClassVar[str] = "gb.neso.tec_register"
    kind: ClassVar[str] = "proposal"
    ext: ClassVar[str] = "csv"
    honour_robots: ClassVar[bool] = False  # CKAN API + signed object-storage URL
    status_key: ClassVar[str] = "neso_tec"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    dq_required_fields: ClassVar[tuple[str, ...]] = (
        "name_canonical",
        "sponsor_name",
        "capacity_mw",
        "technology_raw",
    )
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Project ID",
        "Project Name",
        "Project Status",
        "Plant Type",
        "Cumulative Total Capacity (MW)",
        "MW Effective From",
    )

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        pkg = self.http.get(PACKAGE_URL, honour_robots=False).json()
        resources = [
            r for r in pkg.get("result", {}).get("resources", []) if str(r.get("format", "")).upper() == "CSV"
        ]
        if not resources:
            raise ConnectorError("TEC register package lists no CSV resource")
        res = resources[0]
        r = self.http.get(res["url"], honour_robots=False, timeout=180)
        if r.status_code != 200:
            raise ConnectorError(f"GET {res['url']} -> HTTP {r.status_code}")
        return RawSnapshot(
            content=r.content,
            content_type=r.headers.get("Content-Type", ""),
            url=res["url"],
            retrieved_at=dt.datetime.now(dt.UTC),
            http_status=r.status_code,
            ext="csv",
            headers=dict(r.headers),
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=2,
            meta={
                "resource_id": res.get("id"),
                "last_modified": res.get("last_modified"),
                "licence": pkg.get("result", {}).get("license_title"),
                "dataset_page": DATASET_PAGE,
            },
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        text = raw.content.decode("utf-8-sig", errors="replace")
        if text.lstrip().lower().startswith("<html"):
            raise ParseError("TEC register download answered HTML (redirect not followed?)")
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows or "Project ID" not in rows[0]:
            raise ParseError(f"TEC register layout changed: {list(rows[0]) if rows else 'empty'}")
        return rows

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        n = len(rows)
        g = lambda k: [r.get(k) for r in rows]  # noqa: E731
        tech = [classify_tech(v) for v in g("Plant Type")]
        harmonised = [
            harmonise_status(self.status_key, {"status_raw": s}, self.status_map) for s in g("Project Status")
        ]
        stage = [str(s or "").strip() for s in g("Stage")]
        srid = [
            f"{pid}/{st}" if st else f"{pid}/{_row_hash(r)}"
            for pid, st, r in zip(g("Project ID"), stage, rows, strict=True)
        ]
        cap = [
            to_float(c) or to_float(i)
            for c, i in zip(g("Cumulative Total Capacity (MW)"), g("MW Increase / Decrease"), strict=True)
        ]
        df = pd.DataFrame(
            {
                "source_record_id": srid,
                "source_url": DATASET_PAGE,
                "kind": [k for _, k in tech],
                "name_canonical": g("Project Name"),
                "name_norm": [norm_name(v) for v in g("Project Name")],
                "sponsor_name": g("Customer Name"),
                "sponsor_norm": [norm_org(v) for v in g("Customer Name")],
                "technology": [t for t, _ in tech],
                "technology_raw": g("Plant Type"),
                "capacity_mw": pd.array(cap, dtype="Float64"),
                "storage_mwh": pd.array([None] * n, dtype="Float64"),
                "iso": "NESO",
                "state": None,
                "county": g("Connection Site"),
                "county_norm": None,
                "lifecycle_state": [s for s, _ in harmonised],
                "status_raw": g("Project Status"),
                "status_rule": [r for _, r in harmonised],
                "status_conflict": False,
                "queue_date": pd.NaT,
                "proposed_cod": [to_date(v) for v in g("MW Effective From")],
                "queue_id": g("Project Number"),
                "eia_plant_id": None,
                "eia_generator_id": None,
                "cross_refs": "",
            }
        )
        return self.finalize(df, rows, raw)


def _row_hash(row: dict[str, Any]) -> str:
    """Identity for an unstaged row: its capacity, effective date and status."""
    return content_hash(
        row.get("MW Effective From"),
        row.get("Project Status"),
        row.get("MW Connected"),
        row.get("MW Increase / Decrease"),
    )
