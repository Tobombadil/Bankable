"""EIA US Energy Atlas natural gas layers -> `asset` context rows (ADR 0008; docs/21 §3.22;
docs/00-PLAN.md 2026-09-18 "Midstream and fuels context layers", owner's 2026-09-19 option (a):
pipelines, gas processing, storage and LNG now, objective features only, no valuation).

Four layers, one connector (``LAYERS``), same three steps as `pipeline/context/eia_plants.py`:

    fetch(layer)   -> FetchResult      network; snapshot + run record under data/snapshots|runs
    parse(bytes)   -> list[Feature]    GeoJSON FeatureCollection *or* zipped shapefile, no network
    normalise_*()  -> DataFrame        one row per asset, columns `services/ingest/assets.py` reads

**Where the bytes come from (measured 2026-09-19).** The manifest entries name the Atlas ArcGIS
feature services (``services7.arcgis.com/FGr1D95XCGALKXqM/.../FeatureServer/0``). On 2026-09-19
every one of the four gas layers on that org answered ``{"error": {"code": 499, "message":
"Token Required"}}`` (HTTP 200) to an anonymous ``/query``, and EIA's Hub catalogue lists them
only as *Image* items now; the org's 79 public feature services carry no gas layer. EIA's own
downloadable copies of the same layers are still public on ``www.eia.gov/maps/map_data/*.zip``
(shapefile zips; ``/maps/`` is allowed by eia.gov's robots.txt, checked live). So ``fetch`` tries
the feature service first, records its exact query URL and answer in the run record, and falls
back to the zip; ``parse`` accepts either. The zips are older than the service was (pipelines
``202001``, processing plants ``2017_v2``, storage ``202012``, LNG ``202004`` -- the vintage is
kept on every row as ``attributes.source_vintage``), which is the honest state of the free
source: when the feature service is public again the same code path picks it up.

**Pipelines: one row per (operator, pipeline type), not per segment.** The layer has no pipeline
name -- its only attributes are ``TYPEPIPE`` (Interstate/Intrastate/Gathering), ``Operator``,
``Status`` and a degrees-length -- and 32,961 segments for 253 operators. Dissolving by
``(Operator, TYPEPIPE)`` gives 259 rows, each a ``MULTILINESTRING`` of every segment, with
``segment_count``, geodesic ``miles`` (`pipeline/context/geo.py`; the source's ``Shape_Leng`` is
in degrees), ``states_crossed`` (vertex point-in-polygon against the vendored Census state
polygons, ~1 km grid cache, so a vertex within ~1 km of a border may be assigned to the
neighbour) and the representative point on the longest part. ``name`` is the operator string
because the source has nothing else to call the line. No diameter, capacity or vintage fields
exist in the layer, so none are invented (ADR 0008 §4).

**Objective attributes only.** Every ``attributes`` key is a field the registry states or a
length/count derived from its geometry. Owner and operator strings are kept as the source spells
them (``operator_name`` / ``owner_name``) for `services/ingest/midstream.py` to turn into
`asset_owner` edges; nothing is resolved here.

CLI (docs/40 convention, ``python -m pipeline.context.eia_atlas --layer gas_pipelines``): fetches
(or reads ``--snapshot``), normalises, writes ``data/normalized/context/<source_id>.parquet`` and
prints one JSON summary line. The dev runner then loads with
``services.ingest.assets.load_assets_parquet(session, path, asset_type)`` followed by
``services.ingest.midstream.load_operator_edges_parquet(session, path, asset_type)``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import HttpBlocked, HttpFailed, PoliteSession
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store, ts_token
from pipeline.context import shapefile
from pipeline.context.geo import (
    StateIndex,
    geodesic_length_miles,
    merge_touching_lines,
    representative_point,
    simplify,
    wkt_multilinestring,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
NORMALIZED_DIR = ROOT / "data" / "normalized" / "context"
STATES_GEOJSON = ROOT / "data" / "vendored" / "regions" / "us_states.geojson"

ARCGIS_ORG = "https://services7.arcgis.com/FGr1D95XCGALKXqM/arcgis/rest/services"
EIA_MAP_DATA = "https://www.eia.gov/maps/map_data"
ARCGIS_PAGE_SIZE = 2000


@dataclass(frozen=True)
class Layer:
    key: str
    source_id: str
    asset_type: str
    feature_layer_url: str
    download_url: str
    geometry: str  # "line" | "point"


LAYERS: dict[str, Layer] = {
    "gas_pipelines": Layer(
        key="gas_pipelines",
        source_id="us.eia.atlas.gas_pipelines",
        asset_type="gas_pipeline",
        feature_layer_url=f"{ARCGIS_ORG}/NaturalGas_InterIntrastate_Pipelines_US_EIA/FeatureServer/0",
        download_url=f"{EIA_MAP_DATA}/NaturalGas_InterIntrastate_Pipelines_US_EIA.zip",
        geometry="line",
    ),
    "gas_processing_plants": Layer(
        key="gas_processing_plants",
        source_id="us.eia.atlas.gas_processing_plants",
        asset_type="gas_processing_plant",
        feature_layer_url=f"{ARCGIS_ORG}/NaturalGas_ProcessingPlants_US_EIA/FeatureServer/0",
        download_url=f"{EIA_MAP_DATA}/NaturalGas_ProcessingPlants_US_EIA.zip",
        geometry="point",
    ),
    "gas_storage": Layer(
        key="gas_storage",
        source_id="us.eia.atlas.gas_storage",
        asset_type="gas_storage",
        feature_layer_url=f"{ARCGIS_ORG}/Natural_Gas_Underground_Storage/FeatureServer/0",
        download_url=f"{EIA_MAP_DATA}/NaturalGas_UndergroundStorage_US_EIA.zip",
        geometry="point",
    ),
    "lng_terminals": Layer(
        key="lng_terminals",
        source_id="us.eia.atlas.lng_terminals",
        asset_type="lng_terminal",
        feature_layer_url=f"{ARCGIS_ORG}/Lng_ImportExportTerminals_US_EIA/FeatureServer/0",
        download_url=f"{EIA_MAP_DATA}/Lng_ImportExportTerminals_US_EIA.zip",
        geometry="point",
    ),
}

#: `asset_type` -> layer key, for callers that start from the asset type.
LAYER_BY_ASSET_TYPE: dict[str, str] = {layer.asset_type: key for key, layer in LAYERS.items()}

Feature = dict[str, Any]


# --------------------------------------------------------------------------------------- fetch
class ArcGISError(Exception):
    """The feature service answered with an ArcGIS error body (e.g. ``Token Required``)."""


@dataclass
class FetchResult:
    layer: str
    source_id: str
    fetched_url: str
    retrieved_at: str
    content: bytes
    format: str  # "geojson" | "shapefile_zip"
    sha256: str
    arcgis_query_url: str
    arcgis_result: str  # "ok" | error text
    snapshot_path: pathlib.Path | None = None
    run_path: pathlib.Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def arcgis_query_url(layer: Layer, offset: int = 0, page_size: int = ARCGIS_PAGE_SIZE) -> str:
    return (
        f"{layer.feature_layer_url}/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"
        f"&resultOffset={offset}&resultRecordCount={page_size}"
    )


def fetch_arcgis_geojson(layer: Layer, session: PoliteSession) -> tuple[bytes, dict[str, Any]]:
    """Page through ``/query?f=geojson`` and return one FeatureCollection. Raises ``ArcGISError``
    on an error body (the ``HTTP 200 with an error body`` case, docs/02 §7)."""
    features: list[Feature] = []
    offset = 0
    pages = 0
    while True:
        url = arcgis_query_url(layer, offset)
        resp = session.get(url, honour_robots=False)
        try:
            payload = resp.json()
        except ValueError as e:
            raise ArcGISError(f"non-JSON answer from {url}: {resp.text[:120]!r}") from e
        if isinstance(payload, dict) and "error" in payload:
            err = payload["error"]
            raise ArcGISError(f"code {err.get('code')}: {err.get('message')}")
        page = payload.get("features") or []
        features.extend(page)
        pages += 1
        exceeded = bool(
            payload.get("exceededTransferLimit")
            or (payload.get("properties") or {}).get("exceededTransferLimit")
        )
        if len(page) < ARCGIS_PAGE_SIZE and not exceeded:
            break
        offset += len(page)
        if not page:
            break
    body = json.dumps({"type": "FeatureCollection", "features": features}).encode("utf-8")
    return body, {"pages": pages, "features": len(features)}


def fetch(
    layer: Layer,
    *,
    session: PoliteSession | None = None,
    store: Store | None = None,
    write: bool = True,
) -> FetchResult:
    """Feature service first, EIA's zip second; snapshot + run record written unless
    ``write=False``. The run record carries both URLs and what each answered so the choice is
    auditable per run (``data/runs/<source_id>/<ts>.json``)."""
    session = session or PoliteSession(rate_limits={"services7.arcgis.com": 1.0, "www.eia.gov": 1.0})
    store = store or Store()
    query_url = arcgis_query_url(layer)
    arcgis_result: str
    meta: dict[str, Any] = {}
    content: bytes
    fmt: str
    fetched_url: str
    try:
        content, meta = fetch_arcgis_geojson(layer, session)
        arcgis_result = "ok"
        fmt = "geojson"
        fetched_url = query_url
    except (ArcGISError, HttpBlocked, HttpFailed) as e:
        arcgis_result = f"{type(e).__name__}: {e}"
        resp = session.get(layer.download_url, honour_robots=True)
        if resp.status_code != 200 or not shapefile.is_zip(resp.content):
            raise HttpFailed(
                f"{layer.download_url} answered HTTP {resp.status_code} "
                f"({resp.headers.get('Content-Type')}), not a zip"
            ) from None
        content = resp.content
        fmt = "shapefile_zip"
        fetched_url = layer.download_url
        meta = {"last_modified": resp.headers.get("Last-Modified")}

    retrieved_at = _now_iso()
    result = FetchResult(
        layer=layer.key,
        source_id=layer.source_id,
        fetched_url=fetched_url,
        retrieved_at=retrieved_at,
        content=content,
        format=fmt,
        sha256=hashlib.sha256(content).hexdigest(),
        arcgis_query_url=query_url,
        arcgis_result=arcgis_result,
        meta=meta,
    )
    if write:
        token = ts_token(dt.datetime.now(dt.UTC))
        ext = "geojson" if fmt == "geojson" else "zip"
        result.snapshot_path = store.write_snapshot(layer.source_id, token, ext, content)
        result.run_path = store.write_run(
            layer.source_id,
            token,
            {
                "source_id": layer.source_id,
                "layer": layer.key,
                "retrieved_at": retrieved_at,
                "snapshot": {
                    "fetched_url": fetched_url,
                    "format": fmt,
                    "bytes": len(content),
                    "sha256": result.sha256,
                    "path": str(result.snapshot_path.relative_to(store.root)),
                    **meta,
                },
                "arcgis": {"query_url": query_url, "result": arcgis_result},
                "requests_made": session.requests_made,
            },
        )
    return result


# --------------------------------------------------------------------------------------- parse
def parse(content: bytes) -> list[Feature]:
    """GeoJSON FeatureCollection or zipped shapefile -> GeoJSON-shaped features. Never touches
    the network; runs on the recorded fixtures under ``pipeline/context/fixtures/``."""
    if shapefile.is_zip(content):
        try:
            return shapefile.read_zip(content)
        except shapefile.ShapefileError as e:
            raise ParseError(str(e)) from e
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ParseError(f"neither a zip nor JSON: {e}") from e
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ParseError(f"not a FeatureCollection: {str(payload)[:120]!r}")
    features = payload.get("features")
    if not isinstance(features, list):
        raise ParseError("FeatureCollection without a features list")
    return [f for f in features if isinstance(f, dict)]


def source_vintage(fetch_result_or_content: FetchResult | bytes) -> str | None:
    """The vintage token in EIA's shapefile member names (``..._US_202001.shp`` -> ``202001``,
    ``..._US_2017_v2.shp`` -> ``2017_v2``); ``None`` for GeoJSON, which carries no such token."""
    content = (
        fetch_result_or_content.content
        if isinstance(fetch_result_or_content, FetchResult)
        else fetch_result_or_content
    )
    if not shapefile.is_zip(content):
        return None
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            m = re.search(r"_(\d{6}|\d{4}(?:_v\d+)?)\.shp$", name, re.I)
            if m:
                return m.group(1)
    return None


# ----------------------------------------------------------------------------------- normalise
def _prop(props: dict[str, Any], *names: str) -> Any:
    """Case-insensitive property lookup (ArcGIS may re-case shapefile field names)."""
    lowered = {str(k).lower(): v for k, v in props.items()}
    for n in names:
        if n.lower() in lowered:
            return lowered[n.lower()]
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    s = str(value).strip()
    if s in ("", "-", "NA", "N/A", "null", "None"):
        return None
    return s


def _num(value: Any) -> float | None:
    s = _text(value)
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    f = _num(value)
    return None if f is None else int(f)


def _key(*parts: str | None) -> str:
    joined = " ".join(p for p in parts if p)
    slug = re.sub(r"[^a-z0-9]+", "-", joined.lower()).strip("-")
    return slug[:80] or "record"


def _content_key(*parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]  # noqa: S324 - identity key, not security


def _point(feature: Feature) -> tuple[float, float] | None:
    geom = feature.get("geometry") or {}
    if geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates") or []
    if len(coords) < 2:
        return None
    lon, lat = float(coords[0]), float(coords[1])
    if (lon == 0.0 and lat == 0.0) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return (lon, lat)


def _lines(feature: Feature) -> list[list[list[float]]]:
    geom = feature.get("geometry") or {}
    if geom.get("type") == "LineString":
        return [[[float(c[0]), float(c[1])] for c in geom.get("coordinates") or []]]
    if geom.get("type") == "MultiLineString":
        return [[[float(c[0]), float(c[1])] for c in ln] for ln in geom.get("coordinates") or []]
    return []


US_STATE_CODES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}  # fmt: skip


def _state_code(value: Any) -> str | None:
    s = _text(value)
    if s is None:
        return None
    if len(s) == 2 and s.isalpha():
        return f"US-{s.upper()}"
    code = US_STATE_CODES.get(s.lower())
    return f"US-{code}" if code else None


@dataclass
class Provenance:
    source_id: str
    source_url: str
    retrieved_at: str
    licence: str
    vintage: str | None = None

    def columns(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "licence": self.licence,
        }


ASSET_COLUMNS = [
    "source_id",
    "source_asset_id",
    "name",
    "operator_name",
    "owner_name",
    "status",
    "technology",
    "technology_raw",
    "capacity_value",
    "capacity_unit",
    "unit_count",
    "lon",
    "lat",
    "geom_line_wkt",
    "state_code",
    "county_name",
    "country",
    "attributes",
    "source_url",
    "retrieved_at",
    "licence",
]


def _frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=ASSET_COLUMNS)
    return df


def normalise_pipelines(
    features: list[Feature],
    prov: Provenance,
    *,
    simplify_deg: float = 0.0,
    states: StateIndex | None = None,
) -> pd.DataFrame:
    """Dissolve segments by ``(Operator, TYPEPIPE)`` (module docstring)."""
    groups: dict[tuple[str, str], list[Feature]] = defaultdict(list)
    dropped_no_geometry = 0
    for f in features:
        props = f.get("properties") or {}
        operator = _text(_prop(props, "Operator", "OPERATOR")) or "Unknown operator"
        pipe_type = _text(_prop(props, "TYPEPIPE", "Type", "PIPE_TYPE")) or "Unknown"
        if not _lines(f):
            dropped_no_geometry += 1
            continue
        groups[(operator, pipe_type)].append(f)

    rows: list[dict[str, Any]] = []
    parts_in = 0
    parts_out = 0
    for (operator, pipe_type), segs in groups.items():
        lines: list[list[list[float]]] = []
        statuses: set[str] = set()
        for f in segs:
            for ln in _lines(f):
                if len(ln) >= 2:
                    lines.append(simplify(ln, simplify_deg))
            s = _text(_prop(f.get("properties") or {}, "Status", "STATUS"))
            if s:
                statuses.add(s)
        if not lines:
            continue
        # Chain segments that touch before anything is measured or written: the source is one
        # shapefile network, so a segment's end coordinate is usually exactly the next one's
        # start. Douglas-Peucker keeps both endpoints of every part, so unchained parts cost two
        # vertices each at every zoom and the national view is set by part count, not detail.
        # Length is preserved (only duplicated joints go), so `miles` is unchanged; the
        # representative point may move onto the midpoint of a continuous run instead of an
        # arbitrary segment, which is the intended improvement (docs/21-data-model.md §3.22).
        parts_in += len(lines)
        lines = merge_touching_lines(lines)
        parts_out += len(lines)
        miles = round(geodesic_length_miles(lines), 1)
        rep = representative_point(lines)
        lon, lat = (rep[0], rep[1]) if rep else (None, None)
        state_code = states.lookup(lon, lat) if (states and lon is not None and lat is not None) else None
        crossed = states.states_for(lines) if states else []
        status_raw = sorted(statuses)
        status = (
            "operating" if status_raw and all(s.lower() == "operating" for s in status_raw) else "unknown"
        )
        attributes: dict[str, Any] = {
            "pipeline_type": pipe_type.lower(),
            "segment_count": len(segs),
            "part_count": len(lines),
            "miles": miles,
            "states_crossed": crossed,
            "status_raw": status_raw,
            "source_vintage": prov.vintage,
        }
        rows.append(
            {
                "source_asset_id": _key(operator, pipe_type),
                "name": operator,
                "operator_name": operator if operator != "Unknown operator" else None,
                "owner_name": None,
                "status": status,
                "technology": pipe_type.lower(),
                "technology_raw": pipe_type,
                "capacity_value": None,
                "capacity_unit": None,
                "unit_count": len(segs),
                "lon": lon,
                "lat": lat,
                "geom_line_wkt": wkt_multilinestring(lines),
                "state_code": state_code,
                "county_name": None,
                "country": "US",
                "attributes": attributes,
                **prov.columns(),
            }
        )
    df = _frame(rows)
    df.attrs["dropped_no_geometry"] = dropped_no_geometry
    df.attrs["parts_before_merge"] = parts_in
    df.attrs["parts_after_merge"] = parts_out
    return df


def normalise_processing_plants(features: list[Feature], prov: Provenance) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for f in features:
        props = f.get("properties") or {}
        name = _text(_prop(props, "Plant_Name", "PLANT_NAME", "Name"))
        if name is None:
            continue
        pt = _point(f)
        lon, lat = pt if pt else (None, None)
        state = _text(_prop(props, "State"))
        county = _text(_prop(props, "County"))
        owner = _text(_prop(props, "Owner"))
        operator = _text(_prop(props, "Operator"))
        capacity = _num(_prop(props, "Cap_MMcfd", "CAP_MMCFD"))
        attributes = {
            "capacity_mmcfd": capacity,
            "plant_flow_mmcfd": _num(_prop(props, "Plant_Flow", "PLANT_FLOW")),
            "btu_content": _num(_prop(props, "BTU_Conten", "BTU_CONTENT", "BTU_Content")),
            "dry_stor": _num(_prop(props, "Dry_Stor", "DRY_STOR")),
            "ngl_stor": _num(_prop(props, "NGL_Stor", "NGL_STOR")),
            "period": _int(_prop(props, "Period")),
            "city": _text(_prop(props, "City")),
            "zip_code": _text(_prop(props, "ZipCode", "ZIP")),
            "owner_raw": owner,
            "operator_raw": operator,
            "source_vintage": prov.vintage,
        }
        rows.append(
            {
                "source_asset_id": _content_key(name, state, county, lon, lat),
                "name": name,
                "operator_name": operator,
                "owner_name": owner,
                "status": "operating",
                "technology": None,
                "technology_raw": None,
                "capacity_value": capacity,
                "capacity_unit": "MMcf/d" if capacity is not None else None,
                "unit_count": None,
                "lon": lon,
                "lat": lat,
                "geom_line_wkt": None,
                "state_code": _state_code(state),
                "county_name": county,
                "country": "US",
                "attributes": attributes,
                **prov.columns(),
            }
        )
    return _frame(rows)


_STORAGE_STATUS = {"active": "operating", "inactive": "standby"}


def normalise_storage(features: list[Feature], prov: Provenance) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for f in features:
        props = f.get("properties") or {}
        field_name = _text(_prop(props, "Field"))
        source_id = _text(_prop(props, "ID"))
        if field_name is None or source_id is None:
            continue
        pt = _point(f)
        lon, lat = pt if pt else (None, None)
        field_type = _text(_prop(props, "Field_Type", "FIELD_TYPE"))
        status_raw = _text(_prop(props, "Status"))
        work_cap = _num(_prop(props, "work_cap", "WORK_CAP"))
        attributes = {
            "field_type": field_type,
            "reservoir": _text(_prop(props, "Reservoir")),
            "region": _text(_prop(props, "Region")),
            "base_gas_mcf": _num(_prop(props, "base_gas", "BASE_GAS")),
            "working_gas_capacity_mcf": work_cap,
            "total_field_capacity_mcf": _num(_prop(props, "fld_cap", "FLD_CAP")),
            "max_deliverability_mcfd": _num(_prop(props, "maxdeliv", "MAXDELIV")),
            "status_raw": status_raw,
            "period": _int(_prop(props, "Period")),
            "field_code": _text(_prop(props, "fld_code", "FLD_CODE")),
            "reservoir_code": _text(_prop(props, "res_code", "RES_CODE")),
            "operator_raw": _text(_prop(props, "Company")),
            "source_vintage": prov.vintage,
        }
        rows.append(
            {
                "source_asset_id": source_id,
                "name": field_name.title() if field_name.isupper() else field_name,
                "operator_name": _text(_prop(props, "Company")),
                "owner_name": None,
                "status": _STORAGE_STATUS.get((status_raw or "").lower(), "unknown"),
                "technology": _key(field_type).replace("-", "_") if field_type else None,
                "technology_raw": field_type,
                "capacity_value": work_cap,
                "capacity_unit": "Mcf" if work_cap is not None else None,
                "unit_count": None,
                "lon": lon,
                "lat": lat,
                "geom_line_wkt": None,
                "state_code": _state_code(_prop(props, "State")),
                "county_name": _text(_prop(props, "County")),
                "country": "US",
                "attributes": attributes,
                **prov.columns(),
            }
        )
    return _frame(rows)


def normalise_lng_terminals(features: list[Feature], prov: Provenance) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for f in features:
        props = f.get("properties") or {}
        name = _text(_prop(props, "Facility", "Name"))
        if name is None:
            continue
        pt = _point(f)
        lon, lat = pt if pt else (None, None)
        functions = _text(_prop(props, "Functions", "Function"))
        regas = _num(_prop(props, "Regas_Bcfd", "REGAS_BCFD"))
        liq = _num(_prop(props, "Liq_Bcfd", "LIQ_BCFD"))
        storage = _num(_prop(props, "Stora_Bcf", "STORA_BCF", "Storage_Bcf"))
        exports = "export" in (functions or "").lower()
        capacity = liq if (exports and liq is not None) else regas
        state = _text(_prop(props, "State"))
        county = _text(_prop(props, "County"))
        attributes = {
            "functions": functions,
            "regasification_bcfd": regas,
            "liquefaction_bcfd": liq,
            "storage_bcf": storage,
            "period": _text(_prop(props, "Period")),
            "city": _text(_prop(props, "City")),
            "owner_raw": _text(_prop(props, "Owner")),
            "operator_raw": _text(_prop(props, "Operator")),
            "source_vintage": prov.vintage,
        }
        rows.append(
            {
                "source_asset_id": _content_key(name, state, county, lon, lat),
                "name": name,
                "operator_name": _text(_prop(props, "Operator")),
                "owner_name": _text(_prop(props, "Owner")),
                "status": "operating",
                "technology": _key(functions).replace("-", "_") if functions else None,
                "technology_raw": functions,
                "capacity_value": capacity,
                "capacity_unit": "Bcf/d" if capacity is not None else None,
                "unit_count": None,
                "lon": lon,
                "lat": lat,
                "geom_line_wkt": None,
                "state_code": _state_code(state),
                "county_name": county,
                "country": "US",
                "attributes": attributes,
                **prov.columns(),
            }
        )
    return _frame(rows)


def normalise(
    layer: Layer,
    features: list[Feature],
    prov: Provenance,
    *,
    simplify_deg: float = 0.0,
    states: StateIndex | None = None,
) -> pd.DataFrame:
    if layer.key == "gas_pipelines":
        return normalise_pipelines(features, prov, simplify_deg=simplify_deg, states=states)
    if layer.key == "gas_processing_plants":
        return normalise_processing_plants(features, prov)
    if layer.key == "gas_storage":
        return normalise_storage(features, prov)
    if layer.key == "lng_terminals":
        return normalise_lng_terminals(features, prov)
    raise ValueError(f"no normaliser for layer {layer.key!r}")


# --------------------------------------------------------------------------------------- CLI
def default_output(layer: Layer) -> pathlib.Path:
    return NORMALIZED_DIR / f"{layer.source_id}.parquet"


def _latest_snapshot(store: Store, layer: Layer) -> pathlib.Path:
    d = store.root / "snapshots" / layer.source_id
    candidates = sorted([*d.glob("*.zip"), *d.glob("*.geojson")]) if d.exists() else []
    if not candidates:
        raise FileNotFoundError(f"no snapshot under {d}")
    return candidates[-1]


def _snapshot_provenance(store: Store, layer: Layer, path: pathlib.Path, licence: str) -> Provenance:
    token = path.stem
    run_path = store.run_path(layer.source_id, token)
    fetched_url = layer.download_url if path.suffix == ".zip" else arcgis_query_url(layer)
    retrieved_at: str | None = None
    if run_path.exists():
        try:
            record = json.loads(run_path.read_text(encoding="utf-8"))
            fetched_url = (record.get("snapshot") or {}).get("fetched_url") or fetched_url
            retrieved_at = record.get("retrieved_at")
        except (json.JSONDecodeError, OSError):
            pass
    if retrieved_at is None:
        try:
            retrieved_at = (
                dt.datetime.strptime(token, "%Y%m%dT%H%M%SZ")
                .replace(tzinfo=dt.UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
            )
        except ValueError:
            retrieved_at = _now_iso()
    return Provenance(layer.source_id, fetched_url, retrieved_at, licence)


def run_layer(
    layer: Layer,
    *,
    snapshot: pathlib.Path | None = None,
    latest_snapshot: bool = False,
    out: pathlib.Path | None = None,
    simplify_deg: float = 0.0,
    with_states: bool = True,
    store: Store | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    store = store or Store()
    registry = Registry()
    entry = registry.get(layer.source_id)
    fetch_summary: dict[str, Any] = {}
    if snapshot is None and latest_snapshot:
        snapshot = _latest_snapshot(store, layer)
    if snapshot is not None:
        content = snapshot.read_bytes()
        prov = _snapshot_provenance(store, layer, snapshot, entry.license)
        fetch_summary = {"snapshot": str(snapshot)}
    else:
        result = fetch(layer, store=store)
        content = result.content
        prov = Provenance(layer.source_id, result.fetched_url, result.retrieved_at, entry.license)
        fetch_summary = {
            "fetched_url": result.fetched_url,
            "format": result.format,
            "bytes": len(content),
            "sha256": result.sha256,
            "arcgis_query_url": result.arcgis_query_url,
            "arcgis_result": result.arcgis_result,
            "snapshot": str(result.snapshot_path) if result.snapshot_path else None,
            "run": str(result.run_path) if result.run_path else None,
        }
    prov.vintage = source_vintage(content)

    features = parse(content)
    states = None
    if layer.geometry == "line" and with_states and STATES_GEOJSON.exists():
        states = StateIndex.from_file(STATES_GEOJSON)
    df = normalise(layer, features, prov, simplify_deg=simplify_deg, states=states)

    out = out or default_output(layer)
    _write_parquet(out, df)

    summary: dict[str, Any] = {
        "layer": layer.key,
        "source_id": layer.source_id,
        "features": len(features),
        "rows": len(df),
        "with_coordinates": int(df["lon"].notna().sum()) if len(df) else 0,
        "with_line": int(df["geom_line_wkt"].notna().sum()) if len(df) else 0,
        "source_vintage": prov.vintage,
        "retrieved_at": prov.retrieved_at,
        "out": str(out),
        "parquet_bytes": out.stat().st_size,
        "elapsed_s": round(time.monotonic() - t0, 2),
        **fetch_summary,
    }
    if layer.key == "gas_pipelines":
        summary["dropped_no_geometry"] = df.attrs.get("dropped_no_geometry", 0)
        summary["parts_before_merge"] = df.attrs.get("parts_before_merge", 0)
        summary["parts_after_merge"] = df.attrs.get("parts_after_merge", 0)
        summary["total_miles"] = (
            round(float(df["attributes"].map(lambda a: a["miles"]).sum()), 1) if len(df) else 0
        )
    return summary


def _write_parquet(path: pathlib.Path, df: pd.DataFrame) -> None:
    from pipeline.connectors.base import to_parquet_safe

    path.parent.mkdir(parents=True, exist_ok=True)
    to_parquet_safe(df).to_parquet(path, index=False)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--layer", required=True, choices=[*LAYERS, "all"])
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--snapshot", type=pathlib.Path, help="Parse this recorded zip/geojson instead of fetching"
    )
    group.add_argument(
        "--latest-snapshot", action="store_true", help="Parse the newest data/snapshots/<source_id>/*"
    )
    parser.add_argument("--out", type=pathlib.Path, help="Output parquet (single layer only)")
    parser.add_argument("--simplify-deg", type=float, default=0.0, help="Douglas-Peucker tolerance, degrees")
    parser.add_argument("--no-states", action="store_true", help="Skip states_crossed / state_code for lines")
    args = parser.parse_args(argv)

    keys = list(LAYERS) if args.layer == "all" else [args.layer]
    if args.out and len(keys) > 1:
        parser.error("--out needs a single --layer")
    for key in keys:
        summary = run_layer(
            LAYERS[key],
            snapshot=args.snapshot,
            latest_snapshot=args.latest_snapshot,
            out=args.out,
            simplify_deg=args.simplify_deg,
            with_states=not args.no_states,
        )
        print(json.dumps(summary))  # noqa: T201 — CLI summary line, same convention as eia_plants.py


if __name__ == "__main__":
    main()
