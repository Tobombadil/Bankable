"""Asset endpoints (docs/23 §3.1, ADR 0008): `GET /v1/assets`, `/{public_id}`, `/geo`,
`/{public_id}/nearby-proposals`, `GET /v1/organizations/{public_id}/assets` and
`GET /v1/organizations/{public_id}/nearby-proposals`.

Assets carry no lag and no tier gating (ADR 0008: every source in scope this sprint is public
domain or CC BY) -- every caller, credentialed or not, sees the same response, same as
`GET /v1/context/plants/geo` before it (`services/api/context_routes.py`'s module docstring).
They do carry the licence gate (2026-09-18 audit, docs/50 §3.1 "the asset endpoint had no licence
gate"): every read path here -- list, detail, geo indexes (and their cache key), nearby, organisation
assets and the plants alias -- filters through `services/api/visibility.py::
asset_visibility_filter`, so an asset whose licence is `restricted`/`unknown` or whose source is
not on the public surface is absent, not greyed out (docs/21 §8 checklist items 1 and 4). Stored
coordinates additionally pass `asset_geometry_permitted` (2026-09-19): a licence that allows
derived data but not raw coordinates keeps the asset on the list and its page, but off the map,
with `geometry: null` and a `redactions[]` row -- generic, not triggered by any source today.

Two process-local indexes back `GET /v1/assets/geo`, both following
`services/api/context_routes.py`'s pan/zoom-performance design: a cache of the whole visible asset
table as lightweight tuples, invalidated by one `(count, max(last_changed))` aggregate query per
request plus the source/licence `updated_at` maxima (`_asset_index_cache_key`), filtered in Python
so a re-filter never touches the database.

* Points (`AssetIndexRow`, generalising that module's `PlantIndexRow`): every visible asset with a
  representative `geom` and no `geom_line`; clustered by grid cell above `SPLIT_THRESHOLD`, full
  `Asset` rows fetched by id for the <= 500 that render as individual markers.
* Lines (`LineIndexRow`, 2026-09-19, owner option (a): pipelines as a GeoJSON line layer first,
  simplified by zoom, vector tiles later): every visible asset with a `geom_line`, held at full
  resolution with its bbox and length, simplified per integer zoom on first use and memoised on
  the cache (`_LineIndexCache.simplified`), so a warm request at a given zoom is a bbox test and a
  serialisation. Lines are never clustered; a viewport test is bbox overlap and then an exact
  segment-vs-viewport check, never centroid containment. Above `LINE_FEATURE_CAP` lines in view
  the longest are kept (what a tile pipeline's low-zoom drop rule does), and the totals say so.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Annotated, Any, NamedTuple

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only, selectinload

from pipeline.normalize import TECH_RULES
from services.api import geo_cache
from services.api.common import WEB_HOST, iso
from services.api.deps import get_db
from services.api.errors import invalid_cursor, not_found, validation_error
from services.api.geo import SPLIT_THRESHOLD, _in_bbox
from services.api.interconnection_points import point_url
from services.api.lines import (
    Bbox,
    Parts,
    bbox_overlaps,
    km_to_miles,
    map_parts_for_zoom,
    parse_line_parts,
    parts_bbox,
    parts_intersect_bbox,
    parts_length_km,
    parts_to_geojson,
    point_to_parts_km,
    tolerance_for_zoom,
)
from services.api.orgtree import OwnershipEdge, org_ancestors, org_scope, scope_from_request, scope_ids
from services.api.pagination import decode_cursor, encode_cursor, paginate
from services.api.params import check_allowed, csv_param, int_param, page_limit
from services.api.serialize import (
    asset_geometry,
    asset_geometry_redactions,
    asset_length_miles,
    asset_line_parts,
    asset_source_row,
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    event_licence_row,
    licence_summary_from_source_aggregates,
    licence_summary_row,
    personal_share,
    provenance_quartet,
    serialize_asset,
    serialize_asset_summary,
    serialize_organization_summary,
    serialize_proposal,
)
from services.api.visibility import (
    PUBLISHABLE_REUSE_CLASSES,
    asset_event_visibility_filter,
    asset_geometry_permitted,
    asset_geometry_visible,
    asset_visibility_filter,
    interconnection_point_visibility_filter,
    location_exact_permitted,
    organization_visibility_filter,
    organization_visible,
    proposal_public_filter,
    visible_organization_or_404,
    visible_source_links,
)
from services.api.withheld_names import NONE as NO_WITHHELD_NAMES
from services.api.withheld_names import WithheldNames, withheld_names
from services.db.models import (
    ASSET_EVENT_TYPES,
    ASSET_OWNER_ROLES,
    ASSET_STATUSES,
    ASSET_TYPES,
    Asset,
    AssetOwner,
    Event,
    InterconnectionPoint,
    Licence,
    Location,
    Organization,
    Proposal,
    Source,
)

router = APIRouter()

#: Same classification vocabulary the connectors write, imported rather than re-listed so this
#: never drifts from `services/api/context_routes.py`'s identical constant.
TECHNOLOGY_VOCAB: frozenset[str] = frozenset({tech for _, tech, _ in TECH_RULES} | {"unknown", "other"})

# `LINE_ASSET_TYPES` (services/db/models.py) are the types whose rows are expected to carry
# `geom_line` (ADR 0008 §1). Drawing is decided per row by the presence of `geom_line`, not by type:
# a pipeline row loaded with only a representative point is drawn as a point (honest: "roughly
# here"), never as a fabricated line.

#: Most `asset_line` features one response carries. With `SPLIT_THRESHOLD` (500) individual point
#: markers the total stays within `AssetGeoFeatureCollection.features.maxItems` (2,000; docs/04
#: D-13). When more lines intersect the viewport the longest are kept -- at national zoom the
#: dropped ones are sub-pixel laterals -- and `totals.line_count` / `totals.lines_shown` differ.
LINE_FEATURE_CAP = 1500

#: `attributes` keys copied onto an `asset_line` feature (a subset, so a national payload stays
#: small; the detail response carries the whole objective set). The first group is what
#: `pipeline/context/eia_atlas.py` records for a `gas_pipeline` row (one row per operator x
#: pipeline type, dissolved: `pipeline_type`, `miles`, `states_crossed`, `segment_count`,
#: `part_count`); the rest are the names the brief anticipated and the transmission equivalents,
#: kept so a later source needs no API change. An absent key is simply absent.
LINE_FEATURE_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "pipeline_type",
    "miles",
    "states_crossed",
    "segment_count",
    "part_count",
    "operator",
    "interstate",
    "intrastate",
    "diameter",
    "diameter_in",
    "diameter_mix",
    "length_miles",
    "states",
    "voltage_kv",
)


def stated_length_miles(attributes: dict[str, Any] | None) -> float | None:
    """The registry's stated length: `attributes.miles` (EIA Atlas, as the data lane names it)
    or `attributes.length_miles` (the name the brief anticipated), whichever is present."""
    for key in ("length_miles", "miles"):
        v = (attributes or {}).get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return round(float(v), 1)
    return None


#: Most assets `GET /v1/organizations/{id}/nearby-proposals` measures from, in `asset_owner.id`
#: order; `totals.assets_considered` reports the number actually used.
ORG_NEARBY_ASSET_CAP = 200


ASSET_SORT_ALLOWLIST = {"last_changed", "first_seen", "name", "capacity_mw", "retirement_year"}

#: `retirement_year[gte]` / `[lte]` bounds a caller may ask for (lane R1); outside this is a 400,
#: not an empty page, because no EIA date is that far out (the latest planned year is 2072).
RETIREMENT_YEAR_RANGE = (1900, 2100)

_ASSET_DETAIL_COLUMNS = (
    Asset.id,
    Asset.public_id,
    Asset.slug,
    Asset.asset_type,
    Asset.name,
    Asset.operator_name,
    Asset.status,
    Asset.technology,
    Asset.technology_raw,
    Asset.technologies,
    Asset.capacity_mw,
    Asset.capacity_value,
    Asset.capacity_unit,
    Asset.commissioned_year,
    Asset.unit_count,
    Asset.retirement_year,
    Asset.geom,
    Asset.attributes,
    Asset.state_code,
    Asset.county_name,
    Asset.county_fips,
    Asset.country,
    Asset.source_id,
    Asset.source_url,
    Asset.retrieved_at,
    Asset.licence_id,
)


def _is_postgres(db: Session) -> bool:
    return db.bind is not None and db.bind.dialect.name == "postgresql"


def _line_geojson_column() -> Any:
    """`geom_line` as GeoJSON text on Postgres (`ST_AsGeoJSON` over geography); the stored WKT
    text on SQLite. `parse_line_parts` reads both."""
    return sa.func.ST_AsGeoJSON(Asset.geom_line)


# ------------------------------------------------------------------------- geo: index + clusters
@dataclass(frozen=True, slots=True)
class AssetIndexRow:
    id: str
    lon: float
    lat: float
    asset_type: str
    technology: str | None
    capacity_mw: float | None
    country: str
    #: `asset.status`, so `GET /v1/assets/geo?status=` filters the cached index without SQL (R1).
    status: str = "operating"


@dataclass(frozen=True, slots=True)
class LineIndexRow:
    """One line asset at full stored resolution plus what its map feature needs, so a warm request
    builds `asset_line` features without touching `asset` again (provenance comes from the
    `source`/`licence` rows fetched by id per request, a handful at most)."""

    id: str
    public_id: str
    slug: str
    name: str
    operator_name: str | None
    asset_type: str
    status: str
    state_code: str | None
    country: str
    capacity_value: float | None
    capacity_unit: str | None
    source_id: str
    source_url: str
    retrieved_at: dt.datetime
    licence_id: str
    attributes: dict[str, Any]
    parts: Parts
    bbox: Bbox
    length_km: float


_CacheKey = tuple[int, int, str | None, str | None, str | None]


class _MapLine(NamedTuple):
    """One line as the map layer draws it at one zoom: parts for the viewport test, and the
    GeoJSON geometry built once, not per request (PERF-5)."""

    parts: Parts
    geometry: dict[str, Any]


@dataclass(frozen=True)
class _AssetIndexCache:
    key: _CacheKey
    rows: tuple[AssetIndexRow, ...]


@dataclass(frozen=True)
class _LineIndexCache:
    key: _CacheKey
    rows: tuple[LineIndexRow, ...]
    #: zoom -> asset id -> the line as the map draws it at that zoom (`lines.map_parts_for_zoom`)
    #: with its GeoJSON geometry, or `None` when it is too small to draw there; filled lazily under
    #: `_index_cache_lock` by `_simplified_for_zoom`.
    simplified: dict[int, dict[str, _MapLine | None]] = field(default_factory=dict)


_index_cache: _AssetIndexCache | None = None
_line_cache: _LineIndexCache | None = None
_index_cache_lock = threading.Lock()


def _reset_asset_index_cache() -> None:
    """Test-only hook (mirrors `services.api.context_routes._reset_plant_index_cache`)."""
    global _index_cache, _line_cache
    with _index_cache_lock:
        _index_cache = None
        _line_cache = None
    _ASSET_LICENCE_ROWS.clear()


def _asset_index_cache_key(db: Session) -> _CacheKey:
    """`(bind, visible asset count, max last_changed over the visible set, max source.updated_at,
    max licence.updated_at)`: the last two are what make a licence reclassification or a source
    `publish_state` flip invalidate the cache -- neither touches `asset.last_changed`, so a key
    over the asset table alone would keep serving a gated asset until an unrelated asset moved.
    One key serves both the point and the line index (the count is over every visible asset, a
    superset of either index's rows, so any change to either invalidates both -- rebuilds only
    happen when data actually changed, so the shared key costs nothing on the warm path)."""
    bind_id = id(db.get_bind())
    count, max_last_changed = db.execute(
        select(sa.func.count(), sa.func.max(Asset.last_changed)).where(*asset_visibility_filter())
    ).one()
    max_source_updated = db.scalar(select(sa.func.max(Source.updated_at)))
    max_licence_updated = db.scalar(select(sa.func.max(Licence.updated_at)))
    return (
        bind_id,
        count,
        max_last_changed.isoformat() if max_last_changed is not None else None,
        max_source_updated.isoformat() if max_source_updated is not None else None,
        max_licence_updated.isoformat() if max_licence_updated is not None else None,
    )


def _point_index_where() -> list[Any]:
    return [
        Asset.geom.is_not(None),
        Asset.geom_line.is_(None),
        asset_geometry_permitted(),
        *asset_visibility_filter(),
    ]


def _rebuild_asset_index(db: Session) -> tuple[AssetIndexRow, ...]:
    """Every visible point asset as `(id, lon, lat, ...)`, in `asset.id` order on both dialects.
    The order is the order of the individual features and clusters in `/v1/assets/geo`, and of the
    floating-point sums behind each cluster's centroid; without it the two backends returned the
    same features in different orders (audit A2, 2026-09-30)."""
    rows: list[AssetIndexRow] = []
    if _is_postgres(db):
        # `ST_X`/`ST_Y` exist for `geometry` only; `asset.geom` is `geography(Point,4326)`, so
        # the point is cast first (same SRID, no reprojection). Without the cast Postgres raised
        # `function st_x(geography) does not exist` and both map layers answered 500 (audit A1,
        # 2026-09-30; pinned by tests/test_postgres.py).
        from geoalchemy2 import Geometry

        point = sa.cast(Asset.geom, Geometry(geometry_type="POINT", srid=4326))
        id_col = sa.cast(Asset.id, sa.Text)
        lon_col = sa.func.ST_X(point)
        lat_col = sa.func.ST_Y(point)
        stmt = (
            select(
                id_col,
                lon_col,
                lat_col,
                Asset.asset_type,
                Asset.technology,
                Asset.capacity_mw,
                Asset.country,
                Asset.status,
            )
            .where(*_point_index_where())
            .order_by(Asset.id)
        )
        for asset_id, lon, lat, asset_type, technology, capacity_mw, country, status in db.execute(
            stmt
        ).all():
            rows.append(
                AssetIndexRow(
                    id=asset_id,
                    lon=float(lon),
                    lat=float(lat),
                    asset_type=asset_type,
                    technology=technology,
                    capacity_mw=float(capacity_mw) if capacity_mw is not None else None,
                    country=country,
                    status=status,
                )
            )
        return tuple(rows)

    id_col = sa.cast(Asset.id, sa.Text)
    geom_col = sa.cast(Asset.geom, sa.Text)
    stmt = (
        select(
            id_col,
            geom_col,
            Asset.asset_type,
            Asset.technology,
            Asset.capacity_mw,
            Asset.country,
            Asset.status,
        )
        .where(*_point_index_where())
        .order_by(Asset.id)
    )
    for asset_id, geom_text, asset_type, technology, capacity_mw, country, status in db.execute(stmt).all():
        if geom_text is None:  # pragma: no cover - excluded by the WHERE clause; defensive only
            continue
        geom = json.loads(geom_text)
        rows.append(
            AssetIndexRow(
                id=asset_id,
                lon=geom["lon"],
                lat=geom["lat"],
                asset_type=asset_type,
                technology=technology,
                capacity_mw=float(capacity_mw) if capacity_mw is not None else None,
                country=country,
                status=status,
            )
        )
    return tuple(rows)


def _get_asset_index(db: Session, key: _CacheKey | None = None) -> tuple[AssetIndexRow, ...]:
    """`key` is the request's own `_asset_index_cache_key`, when the caller already has it: the
    key's aggregates ran twice per map request before (backend audit 2026-10-07, PERF-5)."""
    global _index_cache
    if key is None:
        key = _asset_index_cache_key(db)
    with _index_cache_lock:
        cached = _index_cache
        if cached is not None and cached.key == key:
            return cached.rows
    rows = _rebuild_asset_index(db)
    with _index_cache_lock:
        _index_cache = _AssetIndexCache(key=key, rows=rows)
    return rows


def _line_feature_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """The subset the line layer prints. The cached index keeps the stored strings; a withheld
    organisation's name is removed per request in `_line_feature`, so a takedown or a republish
    takes effect without invalidating the index."""
    src = attributes or {}
    return {k: src[k] for k in LINE_FEATURE_ATTRIBUTE_KEYS if k in src}


def _rebuild_line_index(db: Session) -> tuple[LineIndexRow, ...]:
    line_col = _line_geojson_column() if _is_postgres(db) else sa.cast(Asset.geom_line, sa.Text)
    stmt = select(
        sa.cast(Asset.id, sa.Text),
        Asset.public_id,
        Asset.name,
        Asset.operator_name,
        Asset.asset_type,
        Asset.status,
        Asset.state_code,
        Asset.country,
        Asset.capacity_value,
        Asset.capacity_unit,
        Asset.source_id,
        Asset.source_url,
        Asset.retrieved_at,
        Asset.licence_id,
        Asset.attributes,
        line_col,
        Asset.slug,
    ).where(Asset.geom_line.is_not(None), asset_geometry_permitted(), *asset_visibility_filter())
    rows: list[LineIndexRow] = []
    for row in db.execute(stmt).all():
        try:
            parts = parse_line_parts(row[15])
        except ValueError:
            parts = None  # an unparseable geometry is a data defect, not a map outage
        if parts is None:
            continue
        rows.append(
            LineIndexRow(
                id=row[0],
                public_id=row[1],
                slug=row[16],
                name=row[2],
                operator_name=row[3],
                asset_type=row[4],
                status=row[5],
                state_code=row[6],
                country=row[7],
                capacity_value=float(row[8]) if row[8] is not None else None,
                capacity_unit=row[9],
                source_id=row[10],
                source_url=row[11],
                retrieved_at=row[12],
                licence_id=row[13],
                attributes=_line_feature_attributes(row[14]),
                parts=parts,
                bbox=parts_bbox(parts),
                length_km=parts_length_km(parts),
            )
        )
    return tuple(rows)


def _get_line_index(db: Session, key: _CacheKey) -> _LineIndexCache:
    global _line_cache
    with _index_cache_lock:
        cached = _line_cache
        if cached is not None and cached.key == key:
            return cached
    rows = _rebuild_line_index(db)
    with _index_cache_lock:
        if _line_cache is None or _line_cache.key != key:
            _line_cache = _LineIndexCache(key=key, rows=rows)
        return _line_cache


def _simplified_for_zoom(cache: _LineIndexCache, zoom: int) -> dict[str, _MapLine | None]:
    zoom = max(0, min(zoom, 22))
    with _index_cache_lock:
        ready = cache.simplified.get(zoom)
    if ready is not None:
        return ready
    built: dict[str, _MapLine | None] = {}
    for row in cache.rows:
        parts = map_parts_for_zoom(row.parts, zoom)
        built[row.id] = None if parts is None else _MapLine(parts, parts_to_geojson(parts))
    with _index_cache_lock:
        cache.simplified.setdefault(zoom, built)
        return cache.simplified[zoom]


def _filter_index(
    index: tuple[AssetIndexRow, ...],
    asset_types: list[str] | None,
    technologies: list[str] | None,
    countries: list[str] | None,
    statuses: list[str] | None = None,
) -> list[AssetIndexRow]:
    rows: list[AssetIndexRow] = list(index)
    if statuses is not None:
        status_set = set(statuses)
        rows = [r for r in rows if r.status in status_set]
    if asset_types is not None:
        type_set = set(asset_types)
        rows = [r for r in rows if r.asset_type in type_set]
    if technologies is not None:
        tech_set = set(technologies)
        rows = [r for r in rows if r.technology in tech_set]
    if countries is not None:
        country_set = set(countries)
        rows = [r for r in rows if r.country in country_set]
    return rows


def _filter_line_index(
    index: tuple[LineIndexRow, ...],
    asset_types: list[str] | None,
    technologies: list[str] | None,
    countries: list[str] | None,
    statuses: list[str] | None = None,
) -> list[LineIndexRow]:
    """Lines carry no `technology` (pipelines and transmission have none in the vocabulary), so a
    `technology` filter excludes every line -- the same result `Asset.technology IN (...)` gives
    in SQL for a NULL column."""
    if technologies is not None:
        return []
    rows: list[LineIndexRow] = list(index)
    if asset_types is not None:
        type_set = set(asset_types)
        rows = [r for r in rows if r.asset_type in type_set]
    if countries is not None:
        country_set = set(countries)
        rows = [r for r in rows if r.country in country_set]
    if statuses is not None:
        status_set = set(statuses)
        rows = [r for r in rows if r.status in status_set]
    return rows


def _asset_feature(asset: Asset, lon: float, lat: float, withheld: WithheldNames) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": asset.public_id,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "feature_kind": "asset",
            "public_id": asset.public_id,
            "slug": asset.slug,
            "url": f"{WEB_HOST}/assets/{asset.slug}",
            "name": asset.name,
            "operator_name": withheld.operator_name(asset.id, asset.operator_name),
            "asset_type": asset.asset_type,
            "status": asset.status,
            "technology": asset.technology,
            "technology_raw": asset.technology_raw,
            "technologies": dict(asset.technologies or {}),
            "capacity_mw": float(asset.capacity_mw) if asset.capacity_mw is not None else None,
            "unit_count": asset.unit_count,
            "commissioned_year": asset.commissioned_year,
            "retirement_year": asset.retirement_year,
            "state_code": asset.state_code,
            "county_name": asset.county_name,
            "source": asset_source_row(asset),
        },
    }


def _line_feature(
    row: LineIndexRow,
    parts: Parts,
    sources: dict[str, Source],
    licences: dict[str, Licence],
    withheld: WithheldNames,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stated = stated_length_miles(row.attributes)
    length_miles = stated if stated is not None else round(km_to_miles(row.length_km), 1)
    return {
        "type": "Feature",
        "id": row.public_id,
        "geometry": geometry if geometry is not None else parts_to_geojson(parts),
        "properties": {
            "feature_kind": "asset_line",
            "public_id": row.public_id,
            "slug": row.slug,
            "url": f"{WEB_HOST}/assets/{row.slug}",
            "name": row.name,
            "operator_name": withheld.operator_name(row.id, row.operator_name),
            "asset_type": row.asset_type,
            "status": row.status,
            "state_code": row.state_code,
            "capacity_value": row.capacity_value,
            "capacity_unit": row.capacity_unit,
            "length_miles": length_miles,
            "attributes": withheld.attributes(row.id, row.operator_name, row.attributes),
            "source": provenance_quartet(
                sources[row.source_id],
                licences[row.licence_id],
                source_url=row.source_url,
                retrieved_at=row.retrieved_at,
            ),
        },
    }


def _asset_cluster_feature(members: list[AssetIndexRow]) -> dict[str, Any]:
    lons = [m.lon for m in members]
    lats = [m.lat for m in members]
    asset_type_counts: dict[str, int] = defaultdict(int)
    technology_counts: dict[str, int] = defaultdict(int)
    capacity_sum = 0.0
    for m in members:
        asset_type_counts[m.asset_type] += 1
        if m.technology:
            technology_counts[m.technology] += 1
        if m.capacity_mw:
            capacity_sum += m.capacity_mw

    centroid_lon = sum(lons) / len(lons)
    centroid_lat = sum(lats) / len(lats)
    dominant_asset_type = max(asset_type_counts, key=lambda k: asset_type_counts[k])
    dominant_technology = (
        max(technology_counts, key=lambda k: technology_counts[k]) if technology_counts else None
    )
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [centroid_lon, centroid_lat]},
        "properties": {
            "feature_kind": "asset_cluster",
            "count": len(members),
            "asset_type_counts": dict(asset_type_counts),
            "technology_counts": dict(technology_counts),
            "capacity_mw_sum": capacity_sum,
            "dominant_asset_type": dominant_asset_type,
            "dominant_technology": dominant_technology,
            "bbox": [min(lons), min(lats), max(lons), max(lats)],
            "expands_to_zoom": 10,
        },
    }


def _grid_cell(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    from services.api.geo import BASE_CELL_DEG, MIN_CELL_DEG

    effective_zoom = max(1, min(zoom, 20))
    cell_deg = max(BASE_CELL_DEG / (2 ** (effective_zoom - 1)), MIN_CELL_DEG)
    return (math.floor(lon / cell_deg), math.floor(lat / cell_deg))


def build_asset_feature_collection(
    index: list[AssetIndexRow],
    *,
    bbox: tuple[float, float, float, float],
    zoom: int,
    records_total: int,
    asset_type_counts: dict[str, int],
    technology_counts: dict[str, int],
    asset_details: dict[str, Asset] | None = None,
    line_features: list[dict[str, Any]] | None = None,
    line_count: int = 0,
    withheld: WithheldNames = NO_WITHHELD_NAMES,
) -> dict[str, Any]:
    in_view = [row for row in index if _in_bbox(row.lon, row.lat, bbox)]

    features: list[dict[str, Any]] = []
    if len(in_view) <= SPLIT_THRESHOLD:
        if asset_details is None:
            raise ValueError(
                "asset_details is required when the in-view asset count is at or below SPLIT_THRESHOLD"
            )
        for row in in_view:
            features.append(_asset_feature(asset_details[row.id], row.lon, row.lat, withheld))
    else:
        groups: dict[tuple[int, int], list[AssetIndexRow]] = defaultdict(list)
        for row in in_view:
            groups[_grid_cell(row.lon, row.lat, zoom)].append(row)
        for members in groups.values():
            features.append(_asset_cluster_feature(members))

    lines = line_features or []
    features.extend(lines)

    # `records_total` is the caller's aggregate over the whole filter match, deliberately not
    # narrowed to `bbox` -- the same contract `services/api/geo.py` documents for proposals, so a
    # record the viewport excludes (or one with no geometry at all) stays counted instead of
    # dropping out the moment a caller pans. A client wanting an "in view" number counts the
    # features it received, adding each cluster's `count`; reading `totals.records` for that gave
    # "1,536 existing assets in view" beside eight rows (found on the map 2026-09-19).
    return {
        "type": "FeatureCollection",
        "bbox": list(bbox),
        "features": features,
        "totals": {
            "records": records_total,
            "clustered": len(in_view) > SPLIT_THRESHOLD,
            "asset_type_counts": asset_type_counts,
            "technology_counts": technology_counts,
            "line_count": line_count,
            "lines_shown": len(lines),
            "line_tolerance_deg": tolerance_for_zoom(zoom),
        },
    }


def _fetch_asset_details(db: Session, ids: list[str]) -> dict[str, Asset]:
    if not ids:
        return {}
    stmt = select(Asset).where(Asset.id.in_(ids)).options(load_only(*_ASSET_DETAIL_COLUMNS))
    return {str(a.id): a for a in db.scalars(stmt).all()}


def _line_features_in_view(
    db: Session,
    cache: _LineIndexCache,
    rows: list[LineIndexRow],
    bbox: Bbox,
    zoom: int,
    withheld: WithheldNames,
) -> tuple[list[dict[str, Any]], int]:
    """`asset_line` features for the lines that touch `bbox`, as drawn at `zoom`
    (`lines.map_parts_for_zoom`), longest first and capped at `LINE_FEATURE_CAP`. Returns
    `(features, lines in view)`: a line in view that is too small to draw at this zoom is counted
    like one past the cap, so `line_count` and `lines_shown` differ when either rule binds."""
    simplified = _simplified_for_zoom(cache, zoom)
    in_view: list[LineIndexRow] = []
    drawable: list[LineIndexRow] = []
    for row in rows:
        if not bbox_overlaps(row.bbox, bbox):
            continue
        drawn = simplified[row.id]
        if drawn is None:
            # Under `MIN_LINE_PX` at this zoom, so its box is under four pixels: overlapping the
            # viewport is being in it, to the pixel.
            in_view.append(row)
        elif parts_intersect_bbox(drawn.parts, bbox):
            in_view.append(row)
            drawable.append(row)
    if not in_view:
        return [], 0
    shown = sorted(drawable, key=lambda r: r.length_km, reverse=True)[:LINE_FEATURE_CAP]
    source_ids = {r.source_id for r in shown}
    licence_ids = {r.licence_id for r in shown}
    sources = {s.id: s for s in db.scalars(select(Source).where(Source.id.in_(source_ids))).all()}
    licences = {lic.id: lic for lic in db.scalars(select(Licence).where(Licence.id.in_(licence_ids))).all()}
    features = []
    for r in shown:
        if (drawn := simplified[r.id]) is not None:  # always, for a drawable row
            features.append(_line_feature(r, drawn.parts, sources, licences, withheld, drawn.geometry))
    return features, len(in_view)


def asset_surface_shapes(
    db: Session, assets: list[Asset], withheld: WithheldNames
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Every shape a non-admin surface prints each of `assets` in, keyed by public id, built by the
    same functions the routes call: `detail` (`GET /v1/assets/{id}`, `serialize_asset` with owners),
    `list` (`GET /v1/assets`, search, `/v1/organizations/{id}/assets`), `map_point` (the point
    feature of `/v1/assets/geo` and `/v1/context/plants/geo`) and, for a line asset in the line
    index, `map_line`. For the M-11 audit's withheld-name check (`services/visibility_audit/run.py`,
    lane E15), so it scans what the surfaces build rather than a restatement of it. Coordinates are
    not the point here, so the point feature is built at (0, 0)."""
    lines: dict[str, LineIndexRow] = {}
    sources: dict[str, Source] = {}
    licences: dict[str, Licence] = {}
    if any(a.geom_line is not None for a in assets):
        wanted = {str(a.id) for a in assets}
        lines = {r.id: r for r in _get_line_index(db, _asset_index_cache_key(db)).rows if r.id in wanted}
        source_ids = {r.source_id for r in lines.values()}
        licence_ids = {r.licence_id for r in lines.values()}
        sources = {s.id: s for s in db.scalars(select(Source).where(Source.id.in_(source_ids))).all()}
        licences = {
            lic.id: lic for lic in db.scalars(select(Licence).where(Licence.id.in_(licence_ids))).all()
        }
    out: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for asset in assets:
        # The detail route's own owner rule: an edge under a gated licence is not passed in.
        owners = [o for o in asset.owners if o.licence.reuse_class in PUBLISHABLE_REUSE_CLASSES]
        shapes = [
            ("detail", serialize_asset(asset, withheld=withheld, owners=owners)),
            ("list", serialize_asset(asset, withheld=withheld, include_owners=False, include_geometry=False)),
            ("map_point", _asset_feature(asset, 0.0, 0.0, withheld)),
        ]
        if (row := lines.get(str(asset.id))) is not None:
            shapes.append(("map_line", _line_feature(row, row.parts, sources, licences, withheld)))
        out[asset.public_id] = shapes
    return out


def _parse_bbox(value: str, instance: str) -> tuple[float, float, float, float]:
    parts = value.split(",")
    if len(parts) != 4:
        raise validation_error("bbox", "bbox must be min_lon,min_lat,max_lon,max_lat", instance)
    a, b, c, d = (float(p) for p in parts)
    return (a, b, c, d)


def _asset_type_filter_values(request: Request) -> list[str] | None:
    v = request.query_params.get("asset_type")
    if not v:
        return None
    asset_types = csv_param(v) or []
    unknown = [t for t in asset_types if t not in ASSET_TYPES]
    if unknown:
        raise validation_error(
            "asset_type", f"unknown asset_type value(s): {', '.join(unknown)}", request.url.path
        )
    return asset_types


def _technology_filter_values(request: Request) -> list[str] | None:
    v = request.query_params.get("technology")
    if not v:
        return None
    technologies = csv_param(v) or []
    unknown = [t for t in technologies if t not in TECHNOLOGY_VOCAB]
    if unknown:
        raise validation_error(
            "technology", f"unknown technology value(s): {', '.join(unknown)}", request.url.path
        )
    return technologies


def _country_filter_values(request: Request) -> list[str] | None:
    v = request.query_params.get("country")
    if not v:
        return None
    return [c.upper() for c in (csv_param(v) or [])]


def _status_filter_values(request: Request) -> list[str] | None:
    """`status=retired,retiring` (lane R1): csv of `ASSET_STATUSES`; an unknown value is a 400."""
    v = request.query_params.get("status")
    if not v:
        return None
    statuses = csv_param(v) or []
    unknown = [s for s in statuses if s not in ASSET_STATUSES]
    if unknown:
        raise validation_error("status", f"unknown status value(s): {', '.join(unknown)}", request.url.path)
    return statuses


def _retirement_year_bound(request: Request, name: str) -> int | None:
    value = int_param(request, name)
    if value is not None and not (RETIREMENT_YEAR_RANGE[0] <= value <= RETIREMENT_YEAR_RANGE[1]):
        raise validation_error(
            name,
            f"{name} must be between {RETIREMENT_YEAR_RANGE[0]} and {RETIREMENT_YEAR_RANGE[1]}",
            request.url.path,
        )
    return value


def _role_filter_values(request: Request) -> list[str] | None:
    v = request.query_params.get("role")
    if not v:
        return None
    roles = csv_param(v) or []
    unknown = [r for r in roles if r not in ASSET_OWNER_ROLES]
    if unknown:
        raise validation_error("role", f"unknown role value(s): {', '.join(unknown)}", request.url.path)
    return roles


def _apply_sql_filters(
    stmt: sa.Select[Any],
    asset_types: list[str] | None,
    technologies: list[str] | None,
    countries: list[str] | None,
    statuses: list[str] | None = None,
) -> sa.Select[Any]:
    if statuses is not None:
        stmt = stmt.where(Asset.status.in_(statuses))
    if asset_types is not None:
        stmt = stmt.where(Asset.asset_type.in_(asset_types))
    if technologies is not None:
        stmt = stmt.where(Asset.technology.in_(technologies))
    if countries is not None:
        stmt = stmt.where(Asset.country.in_(countries))
    return stmt


#: Viewport and zoom assumed by `GET /v1/assets/geo?organization=...` when the caller passes
#: neither (the company page map wants "everything this organisation has" in one request): the
#: whole world, simplified for a state-to-region page map (zoom 5, tolerance 0.022 deg ~ 2.4 km).
ORGANIZATION_GEO_DEFAULT_BBOX: Bbox = (-180.0, -90.0, 180.0, 90.0)
ORGANIZATION_GEO_DEFAULT_ZOOM = 5


def _organization_asset_ids(db: Session, org_public_ids: list[str], scope: str) -> set[str]:
    """Ids of the assets any of `org_public_ids` owns or operates (`asset_owner`, either role),
    widened down the ownership tree by `scope` (`services/api/orgtree.py`). An unknown
    organisation simply matches nothing, as `GET /v1/assets?organization=` does.

    Each named organisation is scoped separately and the id sets unioned, rather than one walk
    from a synthetic multi-root: the walks share a `MAX_SCOPE_ORGS` budget only per organisation,
    which is the right shape here because `organization=` is already a bounded CSV of names the
    caller typed."""
    holders: set[Any] = set()
    # A taken-down organisation matches nothing, exactly as an unknown one does (the organisation
    # arm, services/api/visibility.py): `?organization=<its id>` must not confirm it holds anything.
    for org in db.scalars(
        select(Organization).where(
            Organization.public_id.in_(org_public_ids), *organization_visibility_filter()
        )
    ).all():
        holders.update(scope_ids(db, org, scope))
    if not holders:
        return set()
    stmt = select(sa.cast(AssetOwner.asset_id, sa.Text)).where(AssetOwner.organization_id.in_(list(holders)))
    return set(db.scalars(stmt).all())


#: `(index key, filters, organisation asset ids)` -> the map's licence aggregate rows.
_ASSET_LICENCE_ROWS = geo_cache.register(geo_cache.TtlLru(max_entries=32))


def _asset_geo_impl(request: Request, db: Session, *, forced_asset_type: str | None) -> Any:
    org_filter = csv_param(request.query_params.get("organization") or None)
    bbox_param = request.query_params.get("bbox")
    zoom_param = request.query_params.get("zoom")
    if org_filter is None and (not bbox_param or zoom_param is None):
        raise validation_error(
            "bbox", "bbox and zoom are required unless organization is set", request.url.path
        )
    try:
        bbox = _parse_bbox(bbox_param, request.url.path) if bbox_param else ORGANIZATION_GEO_DEFAULT_BBOX
        zoom = int(zoom_param) if zoom_param is not None else ORGANIZATION_GEO_DEFAULT_ZOOM
    except ValueError as exc:
        raise validation_error("bbox", "bbox/zoom malformed", request.url.path) from exc

    asset_types = [forced_asset_type] if forced_asset_type else _asset_type_filter_values(request)
    technologies = _technology_filter_values(request)
    countries = _country_filter_values(request)
    statuses = _status_filter_values(request)
    org_asset_ids = (
        _organization_asset_ids(db, org_filter, scope_from_request(request))
        if org_filter is not None
        else None
    )

    key = _asset_index_cache_key(db)
    index = _get_asset_index(db, key)
    filtered = _filter_index(index, asset_types, technologies, countries, statuses)
    line_cache = _get_line_index(db, key)
    filtered_lines = _filter_line_index(line_cache.rows, asset_types, technologies, countries, statuses)
    if org_asset_ids is not None:
        filtered = [row for row in filtered if row.id in org_asset_ids]
        filtered_lines = [row for row in filtered_lines if row.id in org_asset_ids]

    asset_type_counts: dict[str, int] = defaultdict(int)
    technology_counts: dict[str, int] = defaultdict(int)
    for row in filtered:
        asset_type_counts[row.asset_type] += 1
        if row.technology:
            technology_counts[row.technology] += 1
    for line in filtered_lines:
        asset_type_counts[line.asset_type] += 1
    records_total = len(filtered) + len(filtered_lines)

    in_view = [row for row in filtered if _in_bbox(row.lon, row.lat, bbox)]
    asset_details = (
        _fetch_asset_details(db, [row.id for row in in_view]) if len(in_view) <= SPLIT_THRESHOLD else None
    )
    withheld = withheld_names(db)
    line_features, line_count = _line_features_in_view(db, line_cache, filtered_lines, bbox, zoom, withheld)

    fc = build_asset_feature_collection(
        filtered,
        bbox=bbox,
        zoom=zoom,
        records_total=records_total,
        asset_type_counts=dict(asset_type_counts),
        technology_counts=dict(technology_counts),
        asset_details=asset_details,
        line_features=line_features,
        line_count=line_count,
        withheld=withheld,
    )

    # The licence summary covers the whole filter match, so it is the same until the index key or
    # the filters change; cached with the index's key (PERF-5), with `geo_cache`'s TTL as a backstop.
    agg_key = (
        geo_cache.engine_token(db),
        key,
        tuple(asset_types or ()),
        tuple(technologies or ()),
        tuple(countries or ()),
        tuple(statuses or ()),
        None if org_asset_ids is None else frozenset(org_asset_ids),
    )
    licence_rows = _ASSET_LICENCE_ROWS.get(agg_key)
    if licence_rows is None:
        agg_stmt = _apply_sql_filters(
            select(
                Source.id,
                Source.name,
                Source.operator,
                Licence.id,
                Licence.name,
                Licence.url,
                Licence.reuse_class,
                sa.func.coalesce(Source.attribution_text, Licence.attribution_text),
                Licence.requires_link_back,
                sa.func.max(Asset.retrieved_at),
                sa.func.count(),
            )
            .select_from(Asset)
            .join(Source, Source.id == Asset.source_id)
            .join(Licence, Licence.id == Asset.licence_id)
            .where(
                sa.or_(Asset.geom.is_not(None), Asset.geom_line.is_not(None)),
                asset_geometry_permitted(),
                *asset_visibility_filter(),
            ),
            asset_types,
            technologies,
            countries,
            statuses,
        ).group_by(Source.id, Licence.id)
        # Explicit, because a `GROUP BY` promises no order: SQLite returned the groups by key and
        # Postgres by hash, so `licence_summary.sources` differed between the backends (audit A2).
        agg_stmt = agg_stmt.order_by(Source.id, Licence.id)
        if org_asset_ids is not None:
            agg_stmt = agg_stmt.where(sa.cast(Asset.id, sa.Text).in_(sorted(org_asset_ids)))
        licence_rows = [tuple(row) for row in db.execute(agg_stmt).all()]
        _ASSET_LICENCE_ROWS.put(agg_key, licence_rows)
    licence_summary = licence_summary_from_source_aggregates(licence_rows)

    meta = build_meta(lag_days=0, tier="public")
    return build_envelope(fc, meta=meta, licence_summary=licence_summary)


@router.get("/v1/assets/geo")
def get_assets_geo(request: Request, db: Annotated[Session, Depends(get_db)]) -> Any:
    check_allowed(
        request,
        {
            "bbox",
            "zoom",
            "asset_type",
            "technology",
            "country",
            "organization",
            "scope",
            "include_subsidiaries",
            "status",
        },
    )
    return _asset_geo_impl(request, db, forced_asset_type=None)


@router.get("/v1/context/plants/geo")
def get_context_plants_geo_alias(request: Request, db: Annotated[Session, Depends(get_db)]) -> Any:
    """Alias kept for the shipped map (ADR 0008 consequence): forwards to the asset geo builder
    with `asset_type` forced to `power_plant`, same bbox/zoom/technology/country parameters as
    before ADR 0008 (`asset_type` itself is not an accepted parameter here -- it is not a choice,
    it is what this path *means*)."""
    check_allowed(request, {"bbox", "zoom", "technology", "country"})
    return _asset_geo_impl(request, db, forced_asset_type="power_plant")


# ------------------------------------------------------------------------------------- list/detail
def _asset_query_with_filters(
    request: Request, withheld: WithheldNames = NO_WITHHELD_NAMES
) -> sa.Select[tuple[Asset]]:
    stmt = (
        select(Asset)
        .where(*asset_visibility_filter())
        .options(selectinload(Asset.owners).selectinload(AssetOwner.organization))
    )
    qp = request.query_params
    if v := qp.get("slug"):
        stmt = stmt.where(Asset.slug == v)
    if v := qp.get("asset_type"):
        stmt = stmt.where(Asset.asset_type.in_(csv_param(v)))
    if v := qp.get("technology"):
        stmt = stmt.where(Asset.technology.in_(csv_param(v)))
    if v := qp.get("state"):
        stmt = stmt.where(Asset.state_code.in_(csv_param(v)))
    if (statuses := _status_filter_values(request)) is not None:
        stmt = stmt.where(Asset.status.in_(statuses))
    # A plant with no retirement date matches no bound (lane R1): the column is NULL for it.
    if (year := _retirement_year_bound(request, "retirement_year[gte]")) is not None:
        stmt = stmt.where(Asset.retirement_year >= year)
    if (year := _retirement_year_bound(request, "retirement_year[lte]")) is not None:
        stmt = stmt.where(Asset.retirement_year <= year)
    if v := qp.get("organization"):
        org_ids = csv_param(v)
        owner_asset_ids = (
            select(AssetOwner.asset_id)
            .join(Organization, Organization.id == AssetOwner.organization_id)
            .where(Organization.public_id.in_(org_ids), *organization_visibility_filter())
        )
        stmt = stmt.where(Asset.id.in_(owner_asset_ids))
    if v := qp.get("q"):
        like = f"%{v.lower()}%"
        owner_name_hits = (
            select(AssetOwner.asset_id)
            .join(Organization, Organization.id == AssetOwner.organization_id)
            .where(sa.func.lower(Organization.name_canonical).like(like), *organization_visibility_filter())
        )
        stmt = stmt.where(
            sa.or_(
                sa.func.lower(Asset.name).like(like),
                # A withheld operator name is not searchable: finding the asset by it would name the
                # organisation as surely as printing it (`services/api/withheld_names.py`).
                sa.and_(sa.func.lower(Asset.operator_name).like(like), withheld.operator_name_searchable()),
                Asset.id.in_(owner_name_hits),
            )
        )
    return stmt


def _asset_sort_spec(request: Request) -> tuple[str, bool]:
    raw = request.query_params.get("sort", "-last_changed")
    token = raw.split(",")[0]
    ascending = not token.startswith("-")
    field = token[1:] if not ascending else token
    if field not in ASSET_SORT_ALLOWLIST:
        raise validation_error(
            "sort", f"sort field {field!r} is not allowlisted for this resource", request.url.path
        )
    return field, ascending


@router.get("/v1/assets")
def list_assets(request: Request, db: Annotated[Session, Depends(get_db)]) -> Any:
    check_allowed(
        request,
        {
            "limit",
            "cursor",
            "sort",
            "q",
            "asset_type",
            "technology",
            "state",
            "organization",
            "slug",
            "status",
            "retirement_year[gte]",
            "retirement_year[lte]",
        },
    )
    limit = page_limit(request)
    field, ascending = _asset_sort_spec(request)
    withheld = withheld_names(db)
    stmt = _asset_query_with_filters(request, withheld)
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Asset, field),
        id_column=Asset.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_asset(a, withheld=withheld, include_owners=False, include_geometry=False) for a in rows]
    meta = build_meta(lag_days=0, tier="public")
    licence_rows = [licence_summary_row(a.source, a.licence, a.retrieved_at) for a in rows]
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


def _line_parts_for(db: Session, asset: Asset) -> Parts | None:
    """The asset's full-resolution line as parts, on either dialect: parsed from the ORM value on
    SQLite, fetched as GeoJSON in one scalar query on Postgres (a `WKBElement` is not parsed in
    Python -- no `shapely`)."""
    if asset.geom_line is None:
        return None
    if _is_postgres(db):
        text = db.scalar(select(_line_geojson_column()).where(Asset.id == asset.id))
        return parse_line_parts(text)
    return asset_line_parts(asset)


def _line_parts_by_id(db: Session, assets: list[Asset]) -> dict[str, Parts]:
    """`_line_parts_for` over many assets in one query (the organisation nearby endpoint)."""
    line_assets = [a for a in assets if a.geom_line is not None]
    if not line_assets:
        return {}
    out: dict[str, Parts] = {}
    if _is_postgres(db):
        stmt = select(sa.cast(Asset.id, sa.Text), _line_geojson_column()).where(
            Asset.id.in_([a.id for a in line_assets])
        )
        for asset_id, text in db.execute(stmt).all():
            parts = parse_line_parts(text)
            if parts is not None:
                out[asset_id] = parts
        return out
    for a in line_assets:
        parts = asset_line_parts(a)
        if parts is not None:
            out[str(a.id)] = parts
    return out


@router.get("/v1/assets/{public_id}")
def get_asset(public_id: str, request: Request, db: Annotated[Session, Depends(get_db)]) -> Any:
    asset = db.scalar(
        select(Asset)
        .where(Asset.public_id == public_id, *asset_visibility_filter())
        .options(selectinload(Asset.owners).selectinload(AssetOwner.organization))
    )
    if asset is None:
        raise not_found(request.url.path)
    # An ownership edge carries its own provenance quartet (docs/21 §3.23); one recorded under a
    # gated licence is omitted from the visible asset's `owners[]`, same rule as a proposal's
    # restricted `proposal_source` row (docs/21 §8 item 3).
    owners = [o for o in asset.owners if o.licence.reuse_class in PUBLISHABLE_REUSE_CLASSES]
    parts = _line_parts_for(db, asset)
    data = serialize_asset(asset, withheld=withheld_names(db), owners=owners)
    data["geometry"] = asset_geometry(asset, parts=parts)
    data["length_miles"] = asset_length_miles(asset, parts=parts)
    changes = asset_retirement_changes(db, asset)
    data["retirement_changes"] = [row for row, _event in changes]
    meta = build_meta(lag_days=0, tier="public")
    licence_rows = [licence_summary_row(asset.source, asset.licence, asset.retrieved_at)]
    licence_rows += [row for _row, event in changes if (row := event_licence_row(event)) is not None]
    licence_summary = build_licence_summary(licence_rows)
    return build_envelope(
        data, meta=meta, licence_summary=licence_summary, redactions=asset_geometry_redactions(asset)
    )


# ------------------------------------------------------------------------- retirement news (lane R1)
#: Newest retirement events an asset page carries.
RETIREMENT_CHANGES_LIMIT = 20


def asset_retirement_changes(
    db: Session, asset: Asset, now: dt.datetime | None = None
) -> list[tuple[dict[str, Any], Event]]:
    """`(row, event)` for the asset's own retirement events (`services/ingest/retirements.py`),
    newest observation first. The caller has already resolved the asset through
    `asset_visibility_filter`; the event clauses are `asset_event_visibility_filter`'s. Empty for
    every asset type but `power_plant`, which is the only one that has any."""
    if asset.asset_type != "power_plant":
        return []
    stmt = (
        select(Event)
        .where(
            Event.subject_id == asset.id,
            Event.event_type.in_(ASSET_EVENT_TYPES),
            *asset_event_visibility_filter("public", now),
        )
        .order_by(Event.observed_at.desc(), Event.seq.desc())
        .limit(RETIREMENT_CHANGES_LIMIT)
    )
    out: list[tuple[dict[str, Any], Event]] = []
    for event in db.scalars(stmt).all():
        after = event.after or {}
        out.append(
            (
                {
                    "event_type": event.event_type,
                    "observed_at": iso(event.observed_at),
                    "summary": event.reason,
                    "units": list(after.get("units") or []),
                    "capacity_mw": after.get("capacity_mw"),
                    "plant_status": after.get("plant_status"),
                    "provenance": provenance_quartet(
                        event.source,
                        event.licence,
                        source_url=event.source_url or "",
                        retrieved_at=event.retrieved_at,
                    )
                    if event.source is not None
                    and event.licence is not None
                    and event.retrieved_at is not None
                    else None,
                },
                event,
            )
        )
    return out


# --------------------------------------------------------------------------------- nearby proposals
EARTH_RADIUS_KM = 6371.0088


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _radius_km_param(request: Request) -> float:
    radius_param = request.query_params.get("radius_km")
    radius_km = 25.0
    if radius_param is not None:
        try:
            radius_km = float(radius_param)
        except ValueError as exc:
            raise validation_error("radius_km", "radius_km must be a number", request.url.path) from exc
    if not (0 < radius_km <= 100):
        raise validation_error("radius_km", "radius_km must be > 0 and <= 100", request.url.path)
    return radius_km


def _asset_measure_parts(asset: Asset, line: Parts | None) -> Parts | None:
    """What distance is measured to: the full-resolution line for a line asset (never its
    representative point -- "near this pipeline" means near the pipe), else the point as a
    one-vertex part, else `None` for an asset with no geometry."""
    if line is not None:
        return line
    if asset.geom is not None and isinstance(asset.geom, (list, tuple)):
        lon, lat = asset.geom
        return (((float(lon), float(lat)),),)
    return None


class _Candidate(NamedTuple):
    """An exact-grade, publicly visible, raw-permitted proposal as the distance pass reads it: its
    served point and the two keys the ranking and the `technology` filter use. The full record is
    loaded only for the page that is returned (`_proposals_by_id`)."""

    lon: float
    lat: float
    id: Any
    public_id: str
    technology: str | None
    #: Read by `nearby-grid`, which groups nearby proposals by the point they connect at.
    interconnection_point_id: Any


def _location_in_bbox_clause(db: Session, bbox: Bbox) -> Any:
    """SQL half of the candidate cut: the location's stored point inside `bbox`. On SQLite
    `location.geom` is the JSON text `{"lon": ..., "lat": ...}` (`services/db/types.py::
    GeographyPoint`), so `json_extract` reads it; on Postgres it is a geography, cast to geometry
    and tested with `&&` against the planar envelope. Both are the same lon/lat box, inclusive at
    the edges, and an over-approximation of the radius that `_within_radius` then measures exactly,
    so neither changes a result -- only how many rows are loaded (measured 2026-09-19 on a
    10,000-proposal fixture: 2.1 s to load every exact row versus ~100 ms for the bbox cut plus the
    distance loop).

    Until 2026-09-30 the Postgres branch was `ST_Intersects(geom, envelope::geography)`. A
    geography envelope's edges are great-circle arcs, which bow poleward, so it is not the lon/lat
    box and not an over-approximation: on the audit store it dropped 43 exact points along the
    southern edge of a -105..-80 x 29..42 box, and one organisation's nearby-proposals counted
    1,356 where SQLite counted 1,358. The planar test measured 2.6-3.5 ms over 11,094 locations
    without an index, against 13-31 ms for the geography one. `tests/test_postgres.py` pins it."""
    min_lon, min_lat, max_lon, max_lat = bbox
    if _is_postgres(db):
        from geoalchemy2 import Geometry

        envelope = sa.func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
        return sa.cast(Location.geom, Geometry(geometry_type="POINT", srid=4326)).op("&&")(envelope)
    lon = sa.cast(sa.func.json_extract(Location.geom, "$.lon"), sa.Float)
    lat = sa.cast(sa.func.json_extract(Location.geom, "$.lat"), sa.Float)
    return sa.and_(lon >= min_lon, lon <= max_lon, lat >= min_lat, lat <= max_lat)


def _padded_bbox(parts: Parts, radius_km: float) -> Bbox:
    """The geometry's bbox grown by a generous degree pad (1 degree of latitude is ~111 km, so
    `radius_km / 100` degrees over-approximates every radius the endpoints allow)."""
    deg_pad = max(radius_km / 100.0, 0.05)
    min_lon, min_lat, max_lon, max_lat = parts_bbox(parts)
    return (min_lon - deg_pad, min_lat - deg_pad, max_lon + deg_pad, max_lat + deg_pad)


def _exact_proposal_candidates(db: Session, bbox: Bbox) -> list[_Candidate]:
    """Exact-precision proposals only (ADR 0008: "region-grade and none-grade proposals are never
    returned here") *as served*: an exact row whose licence forbids raw publication is region
    grade on this surface (`location_exact_permitted`, the restricted-precision rule) and is
    excluded, since "within N km of this asset" would otherwise disclose the withheld point.
    Public-tier visibility rules applied, and only rows inside `bbox` loaded
    (`_location_in_bbox_clause`); the exact radius cut is `_within_radius`, one Python code path
    on both dialects.

    Read as columns, not entities (backend audit 2026-10-07, PERF-4): a national owner's box holds
    about 2,900 candidates, and hydrating each as a `Proposal` with its links was most of a 1.3 s
    call. A permitted exact row is served at its stored point (`effective_placement` downgrades
    only what `location_exact_permitted` already excluded), so the point is the stored `geom`."""
    stmt = (
        select(
            Proposal.id,
            Proposal.public_id,
            Proposal.technology,
            Proposal.interconnection_point_id,
            Location.geom,
        )
        .join(Location, Location.id == Proposal.location_id)
        .where(
            *proposal_public_filter(),
            Location.precision == "exact",
            location_exact_permitted(),
            Location.geom.is_not(None),
            _location_in_bbox_clause(db, bbox),
        )
    )
    out: list[_Candidate] = []
    for pid, public_id, technology, point_id, geom in db.execute(stmt):
        if geom is None:  # pragma: no cover - excluded in SQL
            continue
        lon, lat = geom
        out.append(_Candidate(float(lon), float(lat), pid, public_id, technology, point_id))
    return out


#: Grid-cell size, in degrees, of `_CandidateGrid`.
_GRID_DEG = 0.5


class _CandidateGrid:
    """Candidates bucketed by a 0.5-degree lon/lat cell, so each asset tests only the candidates in
    the cells its padded box covers, not the whole union box (PERF-4: 121 assets x 2,900 candidates
    were tested pairwise). The box test in `_within_radius` still runs on what a cell returns, so
    the result is the same set."""

    def __init__(self, candidates: list[_Candidate]) -> None:
        self._cells: dict[tuple[int, int], list[_Candidate]] = defaultdict(list)
        for c in candidates:
            self._cells[(math.floor(c.lon / _GRID_DEG), math.floor(c.lat / _GRID_DEG))].append(c)

    def in_box(self, bbox: Bbox) -> list[_Candidate]:
        min_lon, min_lat, max_lon, max_lat = bbox
        x0, x1 = math.floor(min_lon / _GRID_DEG), math.floor(max_lon / _GRID_DEG)
        y0, y1 = math.floor(min_lat / _GRID_DEG), math.floor(max_lat / _GRID_DEG)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > len(self._cells):
            keys = [k for k in self._cells if x0 <= k[0] <= x1 and y0 <= k[1] <= y1]
        else:
            keys = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1) if (x, y) in self._cells]
        return [c for k in keys for c in self._cells[k]]


def _proposals_by_id(db: Session, ids: list[Any]) -> dict[Any, Proposal]:
    """The full records of one returned page, with their links, in one query."""
    if not ids:
        return {}
    stmt = select(Proposal).where(Proposal.id.in_(ids)).options(selectinload(Proposal.sources))
    return {p.id: p for p in db.scalars(stmt)}


def _nearby_cursor(request: Request) -> tuple[float, str] | None:
    """`(distance_km, public_id)` of the last row of the previous page: the two keys every nearby
    list is ordered by, so a page resumes exactly after it (`api/openapi.yaml` `Cursor`)."""
    raw = request.query_params.get("cursor")
    if not raw:
        return None
    decoded = decode_cursor(raw, request.url.path)
    value = decoded.sort_value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid_cursor(request.url.path)
    return float(value), decoded.tiebreaker_id


def _page_after(
    ranked: list[Any], cursor: tuple[float, str] | None, limit: int
) -> tuple[list[Any], str | None, bool]:
    """`ranked` is ordered by `(distance_km, public_id)` (`row[0]`, `row[1].public_id`)."""
    if cursor is not None:
        ranked = [row for row in ranked if (row[0], row[1].public_id) > cursor]
    page = ranked[:limit]
    has_more = len(ranked) > limit
    next_cursor = encode_cursor(page[-1][0], page[-1][1].public_id) if has_more and page else None
    return page, next_cursor, has_more


def _within_radius(
    parts: Parts, candidates: list[_Candidate], radius_km: float
) -> list[tuple[float, _Candidate]]:
    """`(distance_km, proposal)` for every candidate within `radius_km` of `parts`, measured to the
    nearest point of the geometry (`services/api/lines.py::point_to_parts_km`: haversine for a
    point asset, point-to-segment for a line). The padded bounding box first (cheap; the same
    box the SQL cut used, re-applied here because an organisation's candidate set spans every
    asset's box), the exact cut after."""
    bbox = _padded_bbox(parts, radius_km)
    within: list[tuple[float, _Candidate]] = []
    for c in candidates:
        if not (bbox[0] <= c.lon <= bbox[2] and bbox[1] <= c.lat <= bbox[3]):
            continue
        distance_km = point_to_parts_km(c.lon, c.lat, parts)
        if distance_km <= radius_km:
            within.append((distance_km, c))
    return within


def _nearby_licence_rows(proposals: list[Proposal]) -> list[dict[str, Any]]:
    """Only the links the public tier may see (docs/21 §8 items 3-4)."""
    rows = []
    for p in proposals:
        for s in visible_source_links(p.sources):
            rows.append(licence_summary_row(s.source, s.source.licence, s.retrieved_at))
    return rows


@router.get("/v1/assets/{public_id}/nearby-proposals")
def list_nearby_proposals(public_id: str, request: Request, db: Annotated[Session, Depends(get_db)]) -> Any:
    check_allowed(request, {"radius_km", "limit", "cursor"})
    asset = db.scalar(select(Asset).where(Asset.public_id == public_id, *asset_visibility_filter()))
    if asset is None:
        raise not_found(request.url.path)
    radius_km = _radius_km_param(request)
    limit = page_limit(request)
    cursor = _nearby_cursor(request)
    meta = build_meta("proposal", tier="public")

    parts = _asset_measure_parts(asset, _line_parts_for(db, asset)) if asset_geometry_visible(asset) else None
    if parts is None:
        # No geometry, or geometry withheld under the licence: a distance list would disclose
        # what `geometry: null` withholds, so the list is empty and the redaction says why.
        return build_list_envelope(
            [],
            meta=meta,
            licence_summary=build_licence_summary([]),
            page=build_page(None, None, False),
            redactions=asset_geometry_redactions(asset),
        )

    within = _within_radius(parts, _exact_proposal_candidates(db, _padded_bbox(parts, radius_km)), radius_km)
    # Nearest first; equal distances by public id, so the order is total and a cursor resumes it.
    within.sort(key=lambda pair: (pair[0], pair[1].public_id))
    page, next_cursor, has_more = _page_after(within, cursor, limit)
    records = _proposals_by_id(db, [c.id for _, c in page])
    shown = [(d, records[c.id]) for d, c in page if c.id in records]

    data = []
    for distance_km, p in shown:
        row = serialize_proposal(p)
        row["distance_km"] = round(distance_km, 3)
        data.append(row)

    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_nearby_licence_rows([p for _, p in shown])),
        page=build_page(next_cursor, None, has_more),
    )


# ------------------------------------------------------------------------- nearby grid (lane R1)
#: Most transmission lines and interconnection points `GET /v1/assets/{id}/nearby-grid` returns.
NEARBY_GRID_LIMIT = 10


@router.get("/v1/assets/{public_id}/nearby-grid")
def get_nearby_grid(public_id: str, request: Request, db: Annotated[Session, Depends(get_db)]) -> Any:
    """The grid around a point asset, for the powered-land question "what is this site connected
    to?" (lane R1). Two lists, both reusing the nearby machinery above, nearest first:

    * `transmission_lines`: line assets of type `transmission_line` (the LBNL subset, docs/25 §2)
      within `radius_km` of the asset's point, measured to the nearest point of the line
      (`point_to_parts_km`), from the same cached line index the map draws.
    * `interconnection_points`: the points named by the exact-grade, publicly visible proposals
      within `radius_km` (the `nearby-proposals` candidate set), each with how many of those
      proposals name it and the nearest one's distance. A point has no coordinates of its own
      (docs/25 §1.1), so this is "queue requests near here connect at", never a located substation.

    Empty, with the geometry redaction, when the asset's geometry is withheld or absent."""
    check_allowed(request, {"radius_km"})
    asset = db.scalar(select(Asset).where(Asset.public_id == public_id, *asset_visibility_filter()))
    if asset is None:
        raise not_found(request.url.path)
    radius_km = _radius_km_param(request)
    meta = build_meta(lag_days=0, tier="public")
    data: dict[str, Any] = {"radius_km": radius_km, "transmission_lines": [], "interconnection_points": []}
    point = asset.geom if asset_geometry_visible(asset) and asset.geom_line is None else None
    if point is None or not isinstance(point, (list, tuple)):
        return build_envelope(
            data,
            meta=meta,
            licence_summary=build_licence_summary([]),
            redactions=asset_geometry_redactions(asset),
        )
    lon, lat = float(point[0]), float(point[1])
    parts: Parts = (((lon, lat),),)
    bbox = _padded_bbox(parts, radius_km)
    licence_rows: list[dict[str, Any]] = []

    cache = _get_line_index(db, _asset_index_cache_key(db))
    near_lines: list[tuple[float, LineIndexRow]] = []
    for row in cache.rows:
        if row.asset_type != "transmission_line" or not bbox_overlaps(row.bbox, bbox):
            continue
        distance = point_to_parts_km(lon, lat, row.parts)
        if distance <= radius_km:
            near_lines.append((distance, row))
    near_lines.sort(key=lambda pair: pair[0])
    shown_lines = near_lines[:NEARBY_GRID_LIMIT]
    line_sources = {
        s.id: s
        for s in db.scalars(select(Source).where(Source.id.in_({r.source_id for _, r in shown_lines}))).all()
    }
    line_licences = {
        lic.id: lic
        for lic in db.scalars(
            select(Licence).where(Licence.id.in_({r.licence_id for _, r in shown_lines}))
        ).all()
    }
    for distance, row in shown_lines:
        kv = row.attributes.get("voltage_kv")
        data["transmission_lines"].append(
            {
                "public_id": row.public_id,
                "slug": row.slug,
                "url": f"{WEB_HOST}/assets/{row.slug}",
                "name": row.name,
                "voltage_kv": float(kv)
                if isinstance(kv, (int, float)) and not isinstance(kv, bool)
                else None,
                "distance_km": round(distance, 3),
            }
        )
        if row.source_id in line_sources and row.licence_id in line_licences:
            licence_rows.append(
                licence_summary_row(
                    line_sources[row.source_id], line_licences[row.licence_id], row.retrieved_at
                )
            )

    within = _within_radius(parts, _exact_proposal_candidates(db, bbox), radius_km)
    by_point: dict[Any, list[float]] = defaultdict(list)
    for distance, proposal in within:
        if proposal.interconnection_point_id is not None:
            by_point[proposal.interconnection_point_id].append(distance)
    if by_point:
        points = db.scalars(
            select(InterconnectionPoint).where(
                InterconnectionPoint.id.in_(list(by_point)),
                *interconnection_point_visibility_filter("public"),
            )
        ).all()
        ranked = sorted(points, key=lambda p: min(by_point[p.id]))[:NEARBY_GRID_LIMIT]
        for p in ranked:
            data["interconnection_points"].append(
                {
                    "public_id": p.public_id,
                    "url": point_url(p),
                    "name": p.name_display,
                    "iso": p.operator,
                    "kind": p.kind,
                    "voltage_kv": float(p.voltage_kv) if p.voltage_kv is not None else None,
                    "proposals_nearby": len(by_point[p.id]),
                    "nearest_proposal_km": round(min(by_point[p.id]), 3),
                }
            )
            licence_rows.append(licence_summary_row(p.source, p.licence, p.retrieved_at))
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary(licence_rows))


# ---------------------------------------------------------------------- organization -> assets
# The walk down the ownership tree moved to `services/api/orgtree.py` on 2026-09-20 and became
# recursive (`scope=self|children|all`, visited set, depth cap). What was `_subsidiary_ids` /
# `_org_scope_ids` / `_include_subsidiaries_param` here is `org_scope` / `scope_from_request`
# there, so `services/api/app.py` can scope the sponsored-proposals list through the same code.


#: Portfolio rows (`totals.by_organization`) one response carries. A fund page lists the companies
#: it holds rather than their assets, so this is the cap on that list; beyond it the response says
#: `organization_count` and the page links to the subsidiaries index instead of pretending to be
#: complete.
PORTFOLIO_CAP = 100


def organization_asset_totals(db: Session, org: Organization, *, scope: str = "self") -> dict[str, Any]:
    """Counts of the organisation's visible assets through `asset_owner`, for the company page's
    "operates 3 pipelines, owns 2 plants" line: `assets` (distinct), `by_role`, `by_type` (both
    distinct assets per key), `by_role_and_type`, and `by_organization` -- the portfolio breakdown,
    one row per organisation in scope that actually holds an edge. An asset the organisation both
    owns and operates counts once in `assets` and `by_type`, and once under each role.

    `scope` widens the set down the ownership tree (`services/api/orgtree.py`). `by_organization`
    is what makes a fund-level page renderable: with a scope spanning dozens of portfolio
    companies, "1,400 assets" is not a page, and "Tallgrass Energy 10, Rockies Express 49, ..." is.
    It costs nothing extra -- the same single query already reads one row per (edge, asset) and
    only the group-by in Python changes.
    """
    org_scope_result = org_scope(db, org, scope)
    stmt = (
        select(
            AssetOwner.role,
            Asset.asset_type,
            sa.cast(Asset.id, sa.Text),
            sa.cast(AssetOwner.organization_id, sa.Text),
        )
        .join(Asset, Asset.id == AssetOwner.asset_id)
        .where(
            AssetOwner.organization_id.in_(org_scope_result.ids),
            *asset_visibility_filter(),
        )
        .distinct()
    )
    by_role: dict[str, set[str]] = defaultdict(set)
    by_type: dict[str, set[str]] = defaultdict(set)
    by_role_and_type: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_org: dict[str, set[str]] = defaultdict(set)
    per_org_types: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    all_ids: set[str] = set()
    for role, asset_type, asset_id, holder_id in db.execute(stmt).all():
        by_role[role].add(asset_id)
        by_type[asset_type].add(asset_id)
        by_role_and_type[role][asset_type] += 1
        per_org[holder_id].add(asset_id)
        per_org_types[holder_id][asset_type].add(asset_id)
        all_ids.add(asset_id)
    return {
        "assets": len(all_ids),
        "by_role": {role: len(ids) for role, ids in sorted(by_role.items())},
        "by_type": {t: len(ids) for t, ids in sorted(by_type.items())},
        "by_role_and_type": {
            role: dict(sorted(types.items())) for role, types in sorted(by_role_and_type.items())
        },
        **_portfolio_rows(db, per_org, per_org_types),
        "scope": org_scope_result.as_meta(),
    }


def _portfolio_rows(
    db: Session,
    per_org: dict[str, set[str]],
    per_org_types: dict[str, dict[str, set[str]]],
) -> dict[str, Any]:
    """`by_organization` (largest holding first, `PORTFOLIO_CAP` rows) and `organization_count`
    (every holder, capped or not), resolved to organisation summaries in one query."""
    ranked = sorted(per_org.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:PORTFOLIO_CAP]
    if not ranked:
        return {"by_organization": [], "organization_count": 0}
    wanted = [key for key, _ in ranked]
    orgs = {
        str(o.id): o
        for o in db.scalars(select(Organization).where(sa.cast(Organization.id, sa.Text).in_(wanted))).all()
    }
    rows = []
    for key, asset_ids in ranked:
        holder = orgs.get(key)
        if holder is None:  # pragma: no cover - the edge's FK guarantees the row
            continue
        rows.append(
            {
                "organization": serialize_organization_summary(holder),
                "assets": len(asset_ids),
                "by_type": {t: len(ids) for t, ids in sorted(per_org_types[key].items())},
            }
        )
    return {"by_organization": rows, "organization_count": len(per_org)}


SUBSIDIARY_CAP = 50


def serialize_ownership_edge(edge: OwnershipEdge) -> dict[str, Any]:
    """One `child -> parent` claim as the API states it: who, from what source, as of when, for
    what stake -- with the nulls kept.

    `as_of: null` is not omitted and not softened. A company page that prints "parent: X" with no
    date is asserting a fact about today from a file that may state any year, which is exactly the
    failure this field exists to make visible, so the null travels to the renderer and the
    renderer says "no date recorded" (2026-09-20 brief). Same for `share_pct`: no loaded source
    states a percentage, so every value is null today and nothing here fills one in.
    """
    return {
        "organization": serialize_organization_summary(edge.parent),
        "source_id": edge.source_id,
        "as_of": edge.as_of.isoformat() if edge.as_of is not None else None,
        "share_pct": edge.share_pct,
    }


def organization_hierarchy(db: Session, org: Organization) -> dict[str, Any]:
    """Where this organisation sits in the ownership tree, in both directions.

    * `parent` -- the direct parent as an organisation summary, or `null` (unchanged).
    * `parent_edge` -- that same link with its provenance: `source_id`, `as_of`, `share_pct`
      (2026-09-20). `parent` alone was an undated ownership claim.
    * `ancestors` -- the chain above, **root first**, one `parent_edge`-shaped entry per link, so
      a page renders breadcrumbs (Blackstone / Tallgrass / Trailblazer) without walking the API
      one request per level. Bounded and cycle-safe (`services/api/orgtree.py`).
    * `subsidiaries` / `subsidiary_count` -- the direct children, name order, `SUBSIDIARY_CAP`
      rows (unchanged).
    * `descendant_count` -- every organisation below this one at any depth, which is what a
      holding company's page has to say instead of the direct count.
    """
    parent = db.get(Organization, org.parent_org_id) if org.parent_org_id is not None else None
    if parent is not None and not organization_visible(parent):
        # The parent link is dropped, not rendered with the name withheld; `org_ancestors` stops
        # at the same row, so `parent_edge` and `ancestors` agree with this (organisation arm,
        # services/api/visibility.py).
        parent = None
    ancestors = org_ancestors(db, org)
    subs_where = (
        Organization.parent_org_id == org.id,
        Organization.merged_into_id.is_(None),
        *organization_visibility_filter(),
    )
    subs_stmt = select(Organization).where(*subs_where).order_by(Organization.name_canonical, Organization.id)
    subs = list(db.scalars(subs_stmt.limit(SUBSIDIARY_CAP + 1)).all())
    count = len(subs)
    if count > SUBSIDIARY_CAP:
        count = db.scalar(select(sa.func.count()).select_from(Organization).where(*subs_where)) or count
    return {
        "parent": serialize_organization_summary(parent) if parent is not None else None,
        "parent_edge": serialize_ownership_edge(ancestors[0]) if ancestors else None,
        "ancestors": [serialize_ownership_edge(e) for e in reversed(ancestors)],
        "subsidiaries": [serialize_organization_summary(s) for s in subs[:SUBSIDIARY_CAP]],
        "subsidiary_count": count,
        "descendant_count": org_scope(db, org, "all").organizations - 1,
    }


@router.get("/v1/organizations/{public_id}/assets")
def list_organization_assets(
    public_id: str, request: Request, db: Annotated[Session, Depends(get_db)]
) -> Any:
    """The organisation's assets through `asset_owner`, one row per edge.

    `scope` (2026-09-20) widens the set down the ownership tree: `self`, `children` (the direct
    subsidiaries — what the deprecated `include_subsidiaries=true` has always meant and still
    means) or `all` (the full descent). `totals` counts the whole scope regardless of paging or
    the `role`/`asset_type` filters and carries the portfolio breakdown; the `scope` block says
    how many organisations were spanned and whether any bound stopped the walk.
    """
    check_allowed(request, {"limit", "cursor", "role", "asset_type", "scope", "include_subsidiaries"})
    org = visible_organization_or_404(db, public_id, request.url.path)

    limit = page_limit(request)
    roles = _role_filter_values(request)
    asset_types = _asset_type_filter_values(request)
    scope = scope_from_request(request)
    org_scope_result = org_scope(db, org, scope)
    stmt = (
        select(AssetOwner)
        .join(Asset, Asset.id == AssetOwner.asset_id)
        .where(AssetOwner.organization_id.in_(org_scope_result.ids), *asset_visibility_filter())
        .options(selectinload(AssetOwner.asset), selectinload(AssetOwner.organization))
    )
    if roles is not None:
        stmt = stmt.where(AssetOwner.role.in_(roles))
    if asset_types is not None:
        stmt = stmt.where(Asset.asset_type.in_(asset_types))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=AssetOwner.id,
        id_column=AssetOwner.id,
        ascending=True,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    withheld = withheld_names(db)
    data = []
    licence_rows = []
    for edge in rows:
        asset_row = serialize_asset(
            edge.asset, withheld=withheld, include_owners=False, include_geometry=False
        )
        asset_row["role"] = edge.role
        asset_row["share_pct"] = personal_share(edge.organization, edge.share_pct)
        asset_row["as_of"] = edge.as_of.isoformat() if edge.as_of is not None else None
        asset_row["held_by"] = serialize_organization_summary(edge.organization)
        data.append(asset_row)
        licence_rows.append(
            licence_summary_row(edge.asset.source, edge.asset.licence, edge.asset.retrieved_at)
        )

    meta = build_meta(lag_days=0, tier="public")
    env = build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )
    env["totals"] = organization_asset_totals(db, org, scope=scope)
    env["scope"] = org_scope_result.as_meta()
    return env


@router.get("/v1/organizations/{public_id}/nearby-proposals")
def list_organization_nearby_proposals(
    public_id: str, request: Request, db: Annotated[Session, Depends(get_db)]
) -> Any:
    """Exact-grade proposals within `radius_km` of *any* of the organisation's assets (owned or
    operated), each once, at its distance to the nearest of those assets, which is named in
    `nearest_asset`. Distance is to the geometry: to the pipe for a line asset, to the point for
    a plant. Assets are taken in `asset_owner.id` order up to `ORG_NEARBY_ASSET_CAP`; the
    candidate proposal set is loaded once and cut per asset by bounding box, so the cost is one
    proposal query plus bbox tests, with segment distance only for candidates near a line.

    `technology` (CSV, the same vocabulary as `GET /v1/proposals`) narrows the result. It is
    applied *after* the distance pass rather than in the candidate SQL, on purpose: `totals` then
    reports both the filtered count and the count the filter was taken from, so a caller can say
    "N of M" honestly (the company page does exactly that, 2026-09-20). The cost of the wider
    candidate load is bounded by the bounding box either way, so the SQL cut would only save
    distance arithmetic on rows already in memory.

    `scope` (2026-09-20) widens the asset set down the ownership tree. The cost of a large scope
    is bounded where it was already bounded: `ORG_NEARBY_ASSET_CAP` assets are measured from
    whatever the scope spans, and `totals.assets_in_scope` now says how many the cap was taken
    from, so a fund whose assets were cut is visible instead of silently short. The candidate
    proposal load stays one query over the union of the measured assets' padded boxes -- widening
    the scope widens that box, it does not multiply the queries."""
    check_allowed(
        request,
        {"radius_km", "limit", "cursor", "role", "asset_type", "scope", "include_subsidiaries", "technology"},
    )
    org = visible_organization_or_404(db, public_id, request.url.path)
    radius_km = _radius_km_param(request)
    limit = page_limit(request)
    roles = _role_filter_values(request)
    asset_types = _asset_type_filter_values(request)
    technologies = _technology_filter_values(request)
    cursor = _nearby_cursor(request)
    org_scope_result = org_scope(db, org, scope_from_request(request))

    # Each asset once, in the order of its first qualifying `asset_owner` edge. `DISTINCT` with
    # `ORDER BY asset_owner.id` said the same thing, and SQLite accepted it, but Postgres refuses
    # an ORDER BY term outside a DISTINCT select list, so the route answered 500 there (audit A1,
    # 2026-09-30). `min(asset_owner.id)` is not an option either: Postgres 16 has no `min(uuid)`.
    # A window numbers each asset's edges instead, and only the first one is kept. Both dialects
    # order a uuid the same way (bytes on Postgres, the lower-case hex text on SQLite).
    edges = (
        select(
            AssetOwner.asset_id.label("asset_id"),
            AssetOwner.id.label("edge_id"),
            sa.func.row_number()
            .over(partition_by=AssetOwner.asset_id, order_by=AssetOwner.id)
            .label("edge_rank"),
        )
        .join(Asset, AssetOwner.asset_id == Asset.id)
        .where(
            AssetOwner.organization_id.in_(org_scope_result.ids),
            sa.or_(Asset.geom.is_not(None), Asset.geom_line.is_not(None)),
            asset_geometry_permitted(),
            *asset_visibility_filter(),
        )
    )
    if roles is not None:
        edges = edges.where(AssetOwner.role.in_(roles))
    if asset_types is not None:
        edges = edges.where(Asset.asset_type.in_(asset_types))
    first_edges = edges.subquery("first_edges")
    # How many assets the cap is taken from (`totals.assets_in_scope`): counted before the limit,
    # which it could never exceed when it was `len()` of the capped list (PERF-4).
    in_scope = db.scalar(select(sa.func.count()).select_from(first_edges).where(first_edges.c.edge_rank == 1))
    stmt = (
        select(Asset)
        .join(first_edges, first_edges.c.asset_id == Asset.id)
        .where(first_edges.c.edge_rank == 1)
        .order_by(first_edges.c.edge_id)
        .limit(ORG_NEARBY_ASSET_CAP)
    )
    assets = list(db.scalars(stmt).all())
    line_parts = _line_parts_by_id(db, assets)

    measured: list[tuple[Asset, Parts]] = []
    for asset in assets:
        parts = _asset_measure_parts(asset, line_parts.get(str(asset.id)))
        if parts is not None:
            measured.append((asset, parts))
    considered = len(measured)

    best: dict[str, tuple[float, _Candidate, Asset]] = {}
    if measured:
        # One candidate load over the union of every asset's padded box, bucketed once; each asset
        # then tests only the cells its own box covers.
        boxes = [_padded_bbox(parts, radius_km) for _, parts in measured]
        union = (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
        grid = _CandidateGrid(_exact_proposal_candidates(db, union))
        for (asset, parts), box in zip(measured, boxes, strict=True):
            for distance_km, c in _within_radius(parts, grid.in_box(box), radius_km):
                current = best.get(c.public_id)
                if current is None or distance_km < current[0]:
                    best[c.public_id] = (distance_km, c, asset)

    ranked = sorted(best.values(), key=lambda t: (t[0], t[1].public_id))
    within_radius_unfiltered = len(ranked)
    if technologies is not None:
        wanted = frozenset(technologies)
        ranked = [row for row in ranked if row[1].technology in wanted]
    page, next_cursor, has_more = _page_after(ranked, cursor, limit)
    records = _proposals_by_id(db, [c.id for _, c, _ in page])
    shown = [(d, records[c.id], asset) for d, c, asset in page if c.id in records]
    data = []
    for distance_km, p, asset in shown:
        row = serialize_proposal(p)
        row["distance_km"] = round(distance_km, 3)
        row["nearest_asset"] = serialize_asset_summary(asset)
        data.append(row)

    meta = build_meta("proposal", tier="public")
    env = build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_nearby_licence_rows([p for _, p, _ in shown])),
        page=build_page(next_cursor, None, has_more),
    )
    env["totals"] = {
        "assets_considered": considered,
        "assets_in_scope": in_scope or 0,
        "proposals_within_radius": len(ranked),
        "proposals_within_radius_unfiltered": within_radius_unfiltered,
    }
    env["scope"] = org_scope_result.as_meta()
    return env
