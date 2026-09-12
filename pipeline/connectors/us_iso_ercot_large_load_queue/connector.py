"""us.iso.ercot.large_load_queue — ERCOT Large Load Interconnection Status Report (discovery mode).

What was confirmed on 2026-09-12 (data-engineer):
- The registry's product id `NP3-990-CD` does not exist: the EMIL details page answers
  "Product Not Found" (https://www.ercot.com/mp/data-products/data-product-details?id=NP3-990-CD).
- The full public EMIL catalogue (`/api/1/services/read/common/all-emil-items-search.json`,
  5,837 products) has no product whose name or description mentions a large-load interconnection
  status report; the only "Large Load" hits are Secure-classified RPG project files (PG3-xxxx-M).
- The public MIS report list (`IceRepListJsonWS`, 184 report types) has no such report; the GIS
  Report doc list (reportTypeId 15933) carries only `GIS_Report_*` and the co-located battery report.
- NPRR1267 (§3.2.7, PUCT-approved 2025-07-31) requires a monthly aggregated report "to the ERCOT
  website". What is published today is a monthly PDF slide deck to the Large Load Working Group /
  TAC ("Large Load Interconnection Status Update", charts, no tables) and the ERCOT Monthly
  Operational Overview PDF; ERCOT's April 2026 board deck says per-project status reports go to
  TSPs only, ahead of a "Large Load Portal" in development. There is no machine-readable queue.

So this connector watches the EMIL catalogue: `fetch` pulls the compact catalogue
(`filter-emil-items-search.json`, ~0.3 MB), `parse` keeps public products whose name or
description reads "Large Load ... Interconnection" (or "Load Interconnection Status"), and
`normalize` emits one `kind = load` record per product. Today that is zero rows and the run is
`ok`; the day ERCOT publishes the product the diff emits a `new` event and this docstring plus
`data/sources.yaml` get the confirmed id. The per-project parser is written then, against the real file.
source_record_id: the EMIL id (e.g. `pg7-200-er`).
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ParseError, RawSnapshot
from pipeline.normalize import harmonise_status, norm_name

CATALOGUE_URL = "https://www.ercot.com/api/1/services/read/common/filter-emil-items-search.json?keyword=large%20load"
PRODUCT_PAGE = "https://www.ercot.com/mp/data-products/data-product-details?id={emil}"
PATTERN = re.compile(r"large[\s-]*load.*interconnection|load[\s-]*interconnection[\s-]*status", re.I | re.S)


def is_large_load_product(item: dict[str, Any]) -> bool:
    """Public catalogue item that describes the Protocol 3.2.7 report (not an RPG project file)."""
    text = f"{item.get('productName_s', '')} | {item.get('productDescription_s', '')}"
    public = str(item.get("securityClassification_s", "")).lower() == "public"
    rpg = "regional planning group" in str(item.get("productDescription_s", "")).lower()
    return public and not rpg and bool(PATTERN.search(text))


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.iso.ercot.large_load_queue"
    kind: ClassVar[str] = "proposal"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = False  # JSON API behind the data-products page
    status_key: ClassVar[str] = "ercot_large_load"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    dq_required_fields: ClassVar[tuple[str, ...]] = ("name_canonical",)
    key_source_columns: ClassVar[tuple[str, ...]] = ("emilId_s", "productName_s")

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        r = self.http.get(CATALOGUE_URL, honour_robots=False, timeout=120)
        return RawSnapshot(content=r.content, content_type=r.headers.get("Content-Type", ""), url=CATALOGUE_URL,
                           retrieved_at=dt.datetime.now(dt.UTC), http_status=r.status_code, ext="json",
                           headers=dict(r.headers), elapsed_s=round(time.monotonic() - t0, 2),
                           meta={"registry_product_id_checked": "NP3-990-CD", "registry_product_found": False})

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        try:
            items = json.loads(raw.content)
        except json.JSONDecodeError as e:
            raise ParseError(f"EMIL catalogue is not JSON: {e}") from e
        if not isinstance(items, list):
            raise ParseError("EMIL catalogue shape changed (expected a list)")
        raw.meta["catalogue_items"] = len(items)
        keep = ("emilId_s", "reportTypeId_i", "productName_s", "productDescription_s", "securityClassification_s",
                "status_s", "lastUpdatedDate_dt", "firstRunDate_dt", "generationFrequency_o", "fileType_o",
                "dataPortalUrl_s")
        return [{k: it.get(k) for k in keep} for it in items if is_large_load_product(it)]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        harmonised = [harmonise_status(self.status_key, {"status_raw": r.get("status_s")}, self.status_map)
                      for r in rows]
        df = pd.DataFrame({
            "source_record_id": [str(r.get("emilId_s") or "").lower() for r in rows],
            "source_url": [PRODUCT_PAGE.format(emil=str(r.get("emilId_s") or "").upper()) for r in rows],
            "kind": "load",
            "name_canonical": [r.get("productName_s") for r in rows],
            "name_norm": [norm_name(r.get("productName_s")) for r in rows],
            "sponsor_name": "ERCOT",
            "sponsor_norm": "ERCOT",
            "technology": "load",
            "technology_raw": "Large Load",
            "capacity_mw": pd.array([None] * len(rows), dtype="Float64"),
            "storage_mwh": pd.array([None] * len(rows), dtype="Float64"),
            "iso": "ERCOT",
            "state": "TX",
            "county": None, "county_norm": None,
            "lifecycle_state": [s for s, _ in harmonised],
            "status_raw": [r.get("status_s") for r in rows],
            "status_rule": [rule for _, rule in harmonised],
            "status_conflict": False,
            "queue_date": [pd.to_datetime(r.get("firstRunDate_dt"), errors="coerce", utc=True) for r in rows],
            "proposed_cod": pd.NaT,
            "queue_id": [r.get("reportTypeId_i") for r in rows],
            "eia_plant_id": None, "eia_generator_id": None, "cross_refs": "",
        }, index=range(len(rows)))
        return self.finalize(df, rows, raw)
