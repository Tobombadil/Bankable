"""us.va.deq.data_center_air_sites — Virginia DEQ air-permitted sites that DEQ flags as data centres.

What the source is (measured 2026-09-28, docs/25 §3): Virginia DEQ's "Air Sites (Daily)" layer, layer 294
of the public `EDMA` map service on `gisdata.deq.virginia.gov` (the host the DEQ Environmental Data Hub
lists for its "Active Air Sites" feature service). One point per facility registered with DEQ's air
programme — 4,043 rows across operating, temporarily shut down, seasonal, planned and under-construction
sites — refreshed daily from DEQ's permitting system. DEQ carries its own data-centre flag on every row,
`PLA_DATA_CENTER_YN` ("Data Center Y/N"): 198 rows read `Y` on the measurement date (153 Operating,
43 Planned, 1 Under Construction, 1 Temporarily Shutdown). A data centre registers here because its
backup generators need a minor-NSR air permit, which is filed before the building goes up; the Planned
rows are therefore data centres before they are built, with DEQ's own facility point.

Selection (`select_basis`): a row is kept when DEQ's flag is `Y` (`deq_flag`) or, with the flag blank or
`N`, when DEQ's own `PLA_PRINCIPAL_PRODUCT` names a data centre (`principal_product`, 7 extra rows on the
measurement date, e.g. "Amazon Data Services Inc - DCA-062", principal product "Data Center", flag `N`).
The same rule is sent to the server as the `where` clause and re-applied in `parse`, so a fixture proves
it. Nothing else (NAICS 518210, name keywords) selects a row: those caught office buildings.

Record model (docs/21 §3.1; the decision and its reasoning are in docs/25 §3): one `proposal` per DEQ
facility, `kind = load`, `technology = load` — a data centre is a real-world project with a sponsor, a
site and a lifecycle, which is the proposal definition; it is not an `opportunity` (a solicitation with an
issuer and a due date, docs/21 §3.3). The ERCOT large-load connector set the same precedent.

Fields:
- source_record_id: DEQ's air registration number `PLA_REG_NUM` (unique across the layer; stable — it
  is the number DEQ's permits and the public notices cite). source_url: the ArcGIS query that returns
  exactly that registration as an HTML page (the nearest addressable page for one row).
- name_canonical: `PLA_NAME` verbatim. sponsor_name: left null — DEQ has no owner/operator column and
  the facility name mixes company, campus and building codes ("Amazon Data Services Inc IAD-124/125").
- lifecycle_state: `AIR_OP_STATUS` through `status_map.yaml` (Planned -> filed, Under Construction ->
  under_construction, Operating/Seasonal/Temporarily Shutdown -> built).
- capacity_mw: null. DEQ publishes no generator or load capacity on this layer; `PLA_DESC` is free text
  that sometimes counts engines ("280 diesel engines") and is kept in `raw`, never parsed into MW.
- location: DEQ's point geometry, requested in WGS84 (`outSR=4326`) and rounded to 6 decimals (the
  server reprojects from Web Mercator and adds float noise beyond that). It is written to `raw` as
  `Latitude`/`Longitude`, which `services/ingest/loader.py` promotes to an `exact` location for a
  `raw_ok` source. `county` is derived from that point by point-in-polygon against the vendored Census
  county boundaries (`data/vendored/regions/us_counties.geojson`), since the layer has city and ZIP but
  no county; Virginia's independent cities come out as "<Name> city", counties as "<Name> County".
- cross_refs: `PLA_ICIS_ID`, DEQ's federal ICIS-Air id (the join key to EPA ECHO), as `icis_air:<id>`.

Personal data (docs/13 §5.4): layer 294 carries no person fields. Its sibling "Planned Air Sites" layer
(298) carries DEQ staff user names (`CHANGED_BY`, `INSERTED_BY`, `VERIFIEDBY`); this connector never
queries it, and `redact` strips any such staff column (`STAFF_COLUMN`) should the Air Sites layer
ever gain one, before the snapshot is stored. `Data_Disclaimer` (one identical paragraph on every
row) is dropped in `parse`; the text is recorded in `data/sources.yaml`.

Terms: DEQ Data and Web GIS Tools Terms of Use (the map service's own description links them): "GIS
information is in the public domain and may be copied without permission; citation of the source is
suggested." Credit "Virginia Department of Environmental Quality" renders on every record.
robots.txt: `gisdata.deq.virginia.gov/robots.txt` answers 404 (no rule); honoured anyway. The sibling
host `apps.deq.virginia.gov` answers `Disallow: /` and is never fetched.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import time
from functools import lru_cache
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot
from pipeline.connectors.canonical import harmonise_status, norm_county, norm_name
from pipeline.context.geo import StateIndex

LAYER_URL = "https://gisdata.deq.virginia.gov/arcgis/rest/services/public/EDMA/MapServer/294"
QUERY_URL = f"{LAYER_URL}/query"
#: Server-side twin of `select_basis` (kept identical; `parse` re-applies the rule).
WHERE = (
    "PLA_DATA_CENTER_YN = 'Y' OR UPPER(PLA_PRINCIPAL_PRODUCT) LIKE '%DATA CENT%' "
    "OR UPPER(PLA_PRINCIPAL_PRODUCT) LIKE '%DATACENT%'"
)
PAGE_SIZE = 500  # the layer's maxRecordCount is 1000
MAX_PAGES = 20
COORD_DECIMALS = 6
DROP_COLUMNS = frozenset({"Data_Disclaimer"})
#: DEQ staff user-name columns (layer 298 carries CHANGED_BY, INSERTED_BY, VERIFIEDBY).
STAFF_COLUMN = re.compile(r"^(CHANGED|INSERTED|VERIFIED|UPDATED|CREATED|EDITED|MODIFIED)_?BY$", re.I)
_PRODUCT_RE = re.compile(r"data\s*cent", re.I)
COUNTIES_GEOJSON = (
    pathlib.Path(__file__).resolve().parents[3] / "data" / "vendored" / "regions" / "us_counties.geojson"
)
REQUIRED_ATTRIBUTES = ("PLA_REG_NUM", "PLA_NAME", "AIR_OP_STATUS", "PLA_DATA_CENTER_YN")


def select_basis(attrs: dict[str, Any]) -> str | None:
    """Why a row is a data centre: DEQ's own flag, else DEQ's principal-product text; None = not one."""
    if str(attrs.get("PLA_DATA_CENTER_YN") or "").strip().upper() == "Y":
        return "deq_flag"
    if _PRODUCT_RE.search(str(attrs.get("PLA_PRINCIPAL_PRODUCT") or "")):
        return "principal_product"
    return None


def query_params(offset: int) -> dict[str, str]:
    return {
        "where": WHERE,
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "orderByFields": "PLA_REG_NUM",
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE_SIZE),
        "f": "json",
    }


def record_url(reg_num: str) -> str:
    """ArcGIS HTML view of exactly one registration (the row's nearest addressable page)."""
    return f"{QUERY_URL}?" + urlencode(
        {"where": f"PLA_REG_NUM={reg_num}", "outFields": "*", "f": "html"}, quote_via=quote
    )


def _reg_num(value: Any) -> str:
    """`PLA_REG_NUM` arrives as a double (71804.0); the id is its integer text."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    return str(int(f)) if f.is_integer() else str(value).strip()


class _PreciseIndex(StateIndex):
    """`StateIndex` with its lookup cache at ~1 cm instead of ~1 km: a campus next to a county line
    must not inherit the county of an earlier point in the same kilometre cell."""

    GRID = 10_000_000.0


@lru_cache(maxsize=1)
def _va_counties() -> tuple[_PreciseIndex, dict[str, str]]:
    doc = json.loads(COUNTIES_GEOJSON.read_text(encoding="utf-8"))
    feats = [f for f in doc.get("features", []) if (f.get("properties") or {}).get("state_code") == "US-VA"]
    names: dict[str, str] = {}
    for f in feats:
        p = f["properties"]
        geoid = str(p["region_id"])
        # Census county FIPS 510+ in Virginia are the independent cities (county equivalents).
        suffix = "city" if int(geoid[-3:]) >= 510 else "County"
        names[geoid] = f"{p['name']} {suffix}"
    return _PreciseIndex(feats), names


def county_for_point(lon: float | None, lat: float | None) -> tuple[str | None, str | None]:
    """(county GEOID, county name) for a WGS84 point in Virginia, or (None, None)."""
    if lon is None or lat is None:
        return None, None
    index, names = _va_counties()
    geoid = index.lookup(lon, lat)
    return (geoid, names.get(geoid)) if geoid else (None, None)


def _coord(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else round(f, COORD_DECIMALS)


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.va.deq.data_center_air_sites"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = True  # robots.txt is 404 on this host (permissive); checked anyway
    status_key: ClassVar[str] = "va_deq_air_sites"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    dq_required_fields: ClassVar[tuple[str, ...]] = ("name_canonical", "state", "county")
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "PLA_REG_NUM",
        "PLA_NAME",
        "AIR_OP_STATUS",
        "PLA_DATA_CENTER_YN",
        "PLA_PRINCIPAL_PRODUCT",
    )

    # ------------------------------------------------------------------ fetch
    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        pages: list[dict[str, Any]] = []
        requests_before = self.http.requests_made
        offset = 0
        headers: dict[str, str] = {}
        status = 200
        for _ in range(MAX_PAGES):
            r = self.http.get(
                QUERY_URL, params=query_params(offset), honour_robots=self.honour_robots, timeout=120
            )
            status = r.status_code
            if r.status_code != 200:
                raise ConnectorError(f"GET {QUERY_URL} -> HTTP {r.status_code}")
            try:
                page = r.json()
            except ValueError as e:
                raise ParseError(f"DEQ Air Sites query did not return JSON: {r.content[:200]!r}") from e
            if "error" in page:
                # ArcGIS answers HTTP 200 with an error body (docs/02 §7).
                raise ConnectorError(f"DEQ Air Sites query error: {str(page['error'])[:300]}")
            headers = dict(r.headers)
            pages.append(page)
            n = len(page.get("features") or [])
            if not page.get("exceededTransferLimit") or n == 0:
                break
            offset += n
        else:
            raise ConnectorError(f"DEQ Air Sites query did not finish within {MAX_PAGES} pages")
        doc = {"layer": LAYER_URL, "where": WHERE, "pages": pages}
        return RawSnapshot(
            content=json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            content_type="application/json",
            url=LAYER_URL,
            retrieved_at=dt.datetime.now(dt.UTC),
            http_status=status,
            ext="json",
            headers=headers,
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=self.http.requests_made - requests_before,
            meta={"pages": len(pages)},
        )

    def redact(self, content: bytes) -> bytes:
        """Strip DEQ staff user-name columns (`CHANGED_BY`, `INSERTED_BY`, `VERIFIEDBY`) if any page
        carries them; bytes are returned untouched when there is nothing to strip."""
        try:
            doc = json.loads(content)
        except json.JSONDecodeError:
            return content
        stripped = False
        for page in doc.get("pages") or []:
            fields = page.get("fields") or []
            keep = [f for f in fields if not STAFF_COLUMN.search(str(f.get("name", "")))]
            if len(keep) != len(fields):
                page["fields"] = keep
                stripped = True
            for feat in page.get("features") or []:
                attrs = feat.get("attributes") or {}
                for k in [k for k in attrs if STAFF_COLUMN.search(k)]:
                    attrs.pop(k)
                    stripped = True
        if not stripped:
            return content
        return json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")

    # ------------------------------------------------------------------ parse
    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        try:
            doc = json.loads(raw.content)
        except json.JSONDecodeError as e:
            raise ParseError(f"DEQ Air Sites snapshot is not JSON: {e}") from e
        pages = doc.get("pages") if isinstance(doc, dict) else None
        if not isinstance(pages, list) or not pages:
            raise ParseError("DEQ Air Sites snapshot has no pages")
        rows: list[dict[str, Any]] = []
        for page in pages:
            if "error" in page:
                raise ParseError(f"DEQ Air Sites page carries an error: {str(page['error'])[:300]}")
            feats = page.get("features")
            if not isinstance(feats, list):
                raise ParseError("DEQ Air Sites page without features (layout changed?)")
            for feat in feats:
                attrs = dict(feat.get("attributes") or {})
                missing = [c for c in REQUIRED_ATTRIBUTES if c not in attrs]
                if missing:
                    raise ParseError(f"DEQ Air Sites row lacks {missing}: {sorted(attrs)[:20]}")
                basis = select_basis(attrs)
                if basis is None:
                    continue
                row = {k: v for k, v in attrs.items() if k not in DROP_COLUMNS and not STAFF_COLUMN.search(k)}
                geom = feat.get("geometry") or {}
                row["Latitude"] = _coord(geom.get("y"))
                row["Longitude"] = _coord(geom.get("x"))
                row["select_basis"] = basis
                rows.append(row)
        raw.meta["rows_selected"] = len(rows)
        return rows

    # ------------------------------------------------------------------ normalize
    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        harmonised = [
            harmonise_status(self.status_key, {"status_raw": r.get("AIR_OP_STATUS")}, self.status_map)
            for r in rows
        ]
        counties = [county_for_point(r.get("Longitude"), r.get("Latitude")) for r in rows]
        reg = [_reg_num(r.get("PLA_REG_NUM")) for r in rows]
        df = pd.DataFrame(
            {
                "source_record_id": reg,
                "source_url": [record_url(x) if x else None for x in reg],
                "kind": "load",
                "name_canonical": [r.get("PLA_NAME") for r in rows],
                "name_norm": [norm_name(r.get("PLA_NAME")) for r in rows],
                "sponsor_name": None,
                "sponsor_norm": None,
                "technology": "load",
                "technology_raw": "Data Center",
                "capacity_mw": pd.array([None] * len(rows), dtype="Float64"),
                "storage_mwh": pd.array([None] * len(rows), dtype="Float64"),
                "iso": None,
                "state": [str(r.get("FAC_L_STATE") or "VA").strip().upper() or "VA" for r in rows],
                "county": [name for _, name in counties],
                "county_norm": [norm_county(name) for _, name in counties],
                "lifecycle_state": [s for s, _ in harmonised],
                "status_raw": [r.get("AIR_OP_STATUS") for r in rows],
                "status_rule": [rule for _, rule in harmonised],
                "status_conflict": False,
                "queue_date": pd.NaT,
                "proposed_cod": pd.NaT,
                "queue_id": None,
                "eia_plant_id": None,
                "eia_generator_id": None,
                "cross_refs": [f"icis_air:{r['PLA_ICIS_ID']}" if r.get("PLA_ICIS_ID") else "" for r in rows],
            },
            index=range(len(rows)),
        )
        return self.finalize(df, rows, raw)
