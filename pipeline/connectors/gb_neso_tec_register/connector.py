"""gb.neso.tec_register — NESO Transmission Entry Capacity (TEC) Register.

Fetch: CKAN `package_show` names the current CSV resource (renamed each publication, e.g.
`tec-register-11-september-2026.csv`), which redirects to object storage.
Parse: CSV with a UTF-8 BOM; 15 columns including "Project ID", "Project Number", "Stage".
source_record_id: `<Project ID>/<Stage>` where the project is split into MW stages (144 projects
have 2–5 stage rows with different effective dates), else `<Project ID>/<hash>` over the capacity,
effective date and status of the row — one project (a0l4L0000005im7QAA, VPI Immingham) files a
built row and an unstaged future increase under the same id, so the project id alone is not a key.
capacity_mw: the row's own MW, `MW Connected` + `MW Increase / Decrease` (`stage_capacity_mw`). The
register's `Cumulative Total Capacity (MW)` is the project's running total up to that row, so using
it on every stage counted earlier stages again: on the 2026-09-13 register the 267 rows of the 123
multi-row projects summed to 136,499 MW against 85,292 MW real, and all of NESO to 731,514 MW
against 680,307 MW (audit 2026-09-30, data scientist F3). Per row, connected + increase equals the
cumulative figure on all 1,931 single-row projects, and summed over a project's rows it equals the
project's largest cumulative figure on all 123 multi-row projects to within 0.01 MW (three differ by
exactly 0.01 MW, the source's own rounding). The
cumulative figure is kept as its own attribute in `raw` (`Cumulative Total Capacity (MW)`), and
`restate_capacity` restates a stored frame under this rule so the correction is not published as
capacity changes.
Reuse: NESO Open Data Licence v1.0 — attribution "Supported by National Energy SO Open Data".
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot, content_hash
from pipeline.connectors.canonical import (
    classify_tech,
    harmonise_status,
    norm_name,
    norm_org,
    to_date,
    to_float,
)

PACKAGE_URL = "https://api.neso.energy/api/3/action/package_show?id=transmission-entry-capacity-tec-register"
DATASET_PAGE = "https://www.neso.energy/data-portal/transmission-entry-capacity-tec-register"


class Connector(BaseConnector):
    source_id: ClassVar[str] = "gb.neso.tec_register"
    kind: ClassVar[Kind] = "proposal"
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
    #: Every column a diffed field, the record id or the placement reads (audit 2026-09-30 F5:
    #: a renamed `Connection Site` nulled the 1,204 GB grid points, a renamed `Stage` re-keyed rows).
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Project ID",
        "Project Name",
        "Project Status",
        "Plant Type",
        "MW Connected",
        "MW Increase / Decrease",
        "Cumulative Total Capacity (MW)",
        "MW Effective From",
        "Stage",
        "Connection Site",
        "Customer Name",
        "Project Number",
    )

    def restate_capacity(self, df: pd.DataFrame) -> pd.Series | None:
        """`capacity_mw` of a stored frame recomputed from each row's own `raw` (base contract)."""
        return pd.Series([stage_capacity_mw(_raw_row(v)) for v in df["raw"]], index=df.index, dtype="Float64")

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

        def g(key: str) -> list[Any]:
            return [r.get(key) for r in rows]

        tech = [classify_tech(v) for v in g("Plant Type")]
        harmonised = [
            harmonise_status(self.status_key, {"status_raw": s}, self.status_map) for s in g("Project Status")
        ]
        stage = [str(s or "").strip() for s in g("Stage")]
        srid = [
            f"{pid}/{st}" if st else f"{pid}/{_row_hash(r)}"
            for pid, st, r in zip(g("Project ID"), stage, rows, strict=True)
        ]
        cap = [stage_capacity_mw(r) for r in rows]
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


def stage_capacity_mw(row: dict[str, Any]) -> float | None:
    """The MW this row itself adds: `MW Connected` + `MW Increase / Decrease` (module docstring).

    Not `Cumulative Total Capacity (MW)`, which repeats every earlier stage of the project, and not
    the increase alone, which is 0 on a built stage (Arecleoch stage 1: 114 MW connected, +0).
    Falls back to the cumulative figure only when the row states neither component. A row whose
    net is negative (a stage that only reduces TEC) has no capacity of its own: None."""
    connected = to_float(row.get("MW Connected"))
    increase = to_float(row.get("MW Increase / Decrease"))
    if connected is None and increase is None:
        return to_float(row.get("Cumulative Total Capacity (MW)"))
    mw = (connected or 0.0) + (increase or 0.0)
    return round(mw, 3) if mw >= 0 else None


def _raw_row(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_hash(row: dict[str, Any]) -> str:
    """Identity for an unstaged row: its capacity, effective date and status."""
    return content_hash(
        row.get("MW Effective From"),
        row.get("Project Status"),
        row.get("MW Connected"),
        row.get("MW Increase / Decrease"),
    )
