"""Asset endpoints (docs/23 §3.1, ADR 0008): `GET /v1/assets`, `/{public_id}`, `/geo`,
`/{public_id}/nearby-proposals`, and `GET /v1/organizations/{public_id}/assets`.

Assets carry no lag and no tier gating (ADR 0008: every source in scope this sprint is public
domain or CC BY) -- every caller, credentialed or not, sees the same response, same as
`GET /v1/context/plants/geo` before it (`services/api/context_routes.py`'s module docstring).
They do carry the licence gate (2026-09-18 audit, docs/50 §3.1 "the asset endpoint had no licence
gate"): every read path here -- list, detail, geo index (and its cache key), nearby, organisation
assets and the plants alias -- filters through `services/api/visibility.py::
asset_visibility_filter`, so an asset whose licence is `restricted`/`unknown` or whose source is
not on the public surface is absent, not greyed out (docs/21 §8 checklist items 1 and 4).

`GET /v1/assets/geo`'s clustering follows `services/api/context_routes.py`'s pan/zoom-performance
design exactly (`AssetIndexRow` generalises that module's `PlantIndexRow`): a process-local cache
of the *whole* asset table as lightweight tuples, invalidated by one `(count, max(last_changed))`
aggregate query per request (`last_changed`, not `updated_at` -- `asset` has no `TimestampMixin`,
docs/21 §3.22), with `asset_type`/`technology`/`country` filtering done in Python over the cached
index so a re-filter never touches the database. A full `Asset` row (name, technologies split,
owners, source block) is fetched by id, bounded to the `<= SPLIT_THRESHOLD` rows that actually
render as individual markers.
"""

from __future__ import annotations

import json
import math
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only, selectinload

from pipeline.normalize import TECH_RULES
from services.api.deps import get_db
from services.api.errors import not_found, validation_error
from services.api.geo import SPLIT_THRESHOLD, _in_bbox, effective_placement
from services.api.pagination import clamp_limit, paginate
from services.api.params import check_allowed, csv_param
from services.api.serialize import (
    asset_source_row,
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    licence_summary_from_source_aggregates,
    licence_summary_row,
    serialize_asset,
    serialize_proposal,
)
from services.api.visibility import (
    PUBLISHABLE_REUSE_CLASSES,
    asset_visibility_filter,
    location_exact_permitted,
    proposal_public_filter,
)
from services.db.models import (
    ASSET_TYPES,
    Asset,
    AssetOwner,
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


def _int_param(request: Request, name: str) -> int | None:
    v = request.query_params.get(name)
    return int(v) if v is not None else None


ASSET_SORT_ALLOWLIST = {"last_changed", "first_seen", "name", "capacity_mw"}

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


@dataclass(frozen=True)
class _AssetIndexCache:
    key: tuple[int, int, str | None, str | None, str | None]
    rows: tuple[AssetIndexRow, ...]


_index_cache: _AssetIndexCache | None = None
_index_cache_lock = threading.Lock()


def _reset_asset_index_cache() -> None:
    """Test-only hook (mirrors `services.api.context_routes._reset_plant_index_cache`)."""
    global _index_cache
    with _index_cache_lock:
        _index_cache = None


def _asset_index_cache_key(db: Session) -> tuple[int, int, str | None, str | None, str | None]:
    """`(bind, visible placed count, max last_changed over the visible set, max source.updated_at,
    max licence.updated_at)`: the last two are what make a licence reclassification or a source
    `publish_state` flip invalidate the cache -- neither touches `asset.last_changed`, so a key
    over the asset table alone would keep serving a gated asset until an unrelated asset moved."""
    bind_id = id(db.get_bind())
    count, max_last_changed = db.execute(
        select(sa.func.count(), sa.func.max(Asset.last_changed)).where(
            Asset.geom.is_not(None), *asset_visibility_filter()
        )
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


def _rebuild_asset_index(db: Session) -> tuple[AssetIndexRow, ...]:
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    rows: list[AssetIndexRow] = []
    if dialect == "postgresql":
        id_col = sa.cast(Asset.id, sa.Text)
        lon_col = sa.func.ST_X(Asset.geom)
        lat_col = sa.func.ST_Y(Asset.geom)
        stmt = select(
            id_col, lon_col, lat_col, Asset.asset_type, Asset.technology, Asset.capacity_mw, Asset.country
        ).where(Asset.geom.is_not(None), *asset_visibility_filter())
        for asset_id, lon, lat, asset_type, technology, capacity_mw, country in db.execute(stmt).all():
            rows.append(
                AssetIndexRow(
                    id=asset_id,
                    lon=float(lon),
                    lat=float(lat),
                    asset_type=asset_type,
                    technology=technology,
                    capacity_mw=float(capacity_mw) if capacity_mw is not None else None,
                    country=country,
                )
            )
        return tuple(rows)

    id_col = sa.cast(Asset.id, sa.Text)
    geom_col = sa.cast(Asset.geom, sa.Text)
    stmt = select(
        id_col, geom_col, Asset.asset_type, Asset.technology, Asset.capacity_mw, Asset.country
    ).where(Asset.geom.is_not(None), *asset_visibility_filter())
    for asset_id, geom_text, asset_type, technology, capacity_mw, country in db.execute(stmt).all():
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
            )
        )
    return tuple(rows)


def _get_asset_index(db: Session) -> tuple[AssetIndexRow, ...]:
    global _index_cache
    key = _asset_index_cache_key(db)
    with _index_cache_lock:
        cached = _index_cache
        if cached is not None and cached.key == key:
            return cached.rows
    rows = _rebuild_asset_index(db)
    with _index_cache_lock:
        _index_cache = _AssetIndexCache(key=key, rows=rows)
    return rows


def _filter_index(
    index: tuple[AssetIndexRow, ...],
    asset_types: list[str] | None,
    technologies: list[str] | None,
    countries: list[str] | None,
) -> list[AssetIndexRow]:
    rows: list[AssetIndexRow] = list(index)
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


def _asset_feature(asset: Asset, lon: float, lat: float) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": asset.public_id,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "feature_kind": "asset",
            "public_id": asset.public_id,
            "name": asset.name,
            "operator_name": asset.operator_name,
            "asset_type": asset.asset_type,
            "status": asset.status,
            "technology": asset.technology,
            "technology_raw": asset.technology_raw,
            "technologies": dict(asset.technologies or {}),
            "capacity_mw": float(asset.capacity_mw) if asset.capacity_mw is not None else None,
            "unit_count": asset.unit_count,
            "commissioned_year": asset.commissioned_year,
            "state_code": asset.state_code,
            "county_name": asset.county_name,
            "source": asset_source_row(asset),
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
) -> dict[str, Any]:
    in_view = [row for row in index if _in_bbox(row.lon, row.lat, bbox)]

    features: list[dict[str, Any]] = []
    if len(in_view) <= SPLIT_THRESHOLD:
        if asset_details is None:
            raise ValueError(
                "asset_details is required when the in-view asset count is at or below SPLIT_THRESHOLD"
            )
        for row in in_view:
            features.append(_asset_feature(asset_details[row.id], row.lon, row.lat))
    else:
        groups: dict[tuple[int, int], list[AssetIndexRow]] = defaultdict(list)
        for row in in_view:
            groups[_grid_cell(row.lon, row.lat, zoom)].append(row)
        for members in groups.values():
            features.append(_asset_cluster_feature(members))

    return {
        "type": "FeatureCollection",
        "bbox": list(bbox),
        "features": features,
        "totals": {
            "records": records_total,
            "clustered": len(in_view) > SPLIT_THRESHOLD,
            "asset_type_counts": asset_type_counts,
            "technology_counts": technology_counts,
        },
    }


def _fetch_asset_details(db: Session, ids: list[str]) -> dict[str, Asset]:
    if not ids:
        return {}
    stmt = select(Asset).where(Asset.id.in_(ids)).options(load_only(*_ASSET_DETAIL_COLUMNS))
    return {str(a.id): a for a in db.scalars(stmt).all()}


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


def _apply_sql_filters(
    stmt: sa.Select[Any],
    asset_types: list[str] | None,
    technologies: list[str] | None,
    countries: list[str] | None,
) -> sa.Select[Any]:
    if asset_types is not None:
        stmt = stmt.where(Asset.asset_type.in_(asset_types))
    if technologies is not None:
        stmt = stmt.where(Asset.technology.in_(technologies))
    if countries is not None:
        stmt = stmt.where(Asset.country.in_(countries))
    return stmt


def _asset_geo_impl(request: Request, db: Session, *, forced_asset_type: str | None) -> Any:
    bbox_param = request.query_params.get("bbox")
    zoom_param = request.query_params.get("zoom")
    if not bbox_param or zoom_param is None:
        raise validation_error("bbox", "bbox and zoom are required", request.url.path)
    try:
        bbox = _parse_bbox(bbox_param, request.url.path)
        zoom = int(zoom_param)
    except ValueError as exc:
        raise validation_error("bbox", "bbox/zoom malformed", request.url.path) from exc

    asset_types = [forced_asset_type] if forced_asset_type else _asset_type_filter_values(request)
    technologies = _technology_filter_values(request)
    countries = _country_filter_values(request)

    index = _get_asset_index(db)
    filtered = _filter_index(index, asset_types, technologies, countries)

    asset_type_counts: dict[str, int] = defaultdict(int)
    technology_counts: dict[str, int] = defaultdict(int)
    for row in filtered:
        asset_type_counts[row.asset_type] += 1
        if row.technology:
            technology_counts[row.technology] += 1
    records_total = len(filtered)

    in_view = [row for row in filtered if _in_bbox(row.lon, row.lat, bbox)]
    asset_details = (
        _fetch_asset_details(db, [row.id for row in in_view]) if len(in_view) <= SPLIT_THRESHOLD else None
    )

    fc = build_asset_feature_collection(
        filtered,
        bbox=bbox,
        zoom=zoom,
        records_total=records_total,
        asset_type_counts=dict(asset_type_counts),
        technology_counts=dict(technology_counts),
        asset_details=asset_details,
    )

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
        .where(Asset.geom.is_not(None), *asset_visibility_filter()),
        asset_types,
        technologies,
        countries,
    ).group_by(Source.id, Licence.id)
    licence_rows = [tuple(row) for row in db.execute(agg_stmt).all()]
    licence_summary = licence_summary_from_source_aggregates(licence_rows)

    meta = build_meta(lag_days=0, tier="public")
    return build_envelope(fc, meta=meta, licence_summary=licence_summary)


@router.get("/v1/assets/geo")
def get_assets_geo(request: Request, db: Annotated[Session, Depends(get_db)]) -> Any:
    check_allowed(request, {"bbox", "zoom", "asset_type", "technology", "country"})
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
def _asset_query_with_filters(request: Request) -> sa.Select[tuple[Asset]]:
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
    if v := qp.get("organization"):
        org_ids = csv_param(v)
        owner_asset_ids = (
            select(AssetOwner.asset_id)
            .join(Organization, Organization.id == AssetOwner.organization_id)
            .where(Organization.public_id.in_(org_ids))
        )
        stmt = stmt.where(Asset.id.in_(owner_asset_ids))
    if v := qp.get("q"):
        like = f"%{v.lower()}%"
        owner_name_hits = (
            select(AssetOwner.asset_id)
            .join(Organization, Organization.id == AssetOwner.organization_id)
            .where(sa.func.lower(Organization.name_canonical).like(like))
        )
        stmt = stmt.where(
            sa.or_(
                sa.func.lower(Asset.name).like(like),
                sa.func.lower(Asset.operator_name).like(like),
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
        {"limit", "cursor", "sort", "q", "asset_type", "technology", "state", "organization", "slug"},
    )
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _asset_sort_spec(request)
    stmt = _asset_query_with_filters(request)
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
    data = [serialize_asset(a, include_owners=False) for a in rows]
    meta = build_meta(lag_days=0, tier="public")
    licence_rows = [licence_summary_row(a.source, a.licence, a.retrieved_at) for a in rows]
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


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
    data = serialize_asset(asset, owners=owners)
    meta = build_meta(lag_days=0, tier="public")
    licence_row = licence_summary_row(asset.source, asset.licence, asset.retrieved_at)
    licence_summary = build_licence_summary([licence_row])
    return build_envelope(data, meta=meta, licence_summary=licence_summary)


# --------------------------------------------------------------------------------- nearby proposals
EARTH_RADIUS_KM = 6371.0088


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@router.get("/v1/assets/{public_id}/nearby-proposals")
def list_nearby_proposals(public_id: str, request: Request, db: Annotated[Session, Depends(get_db)]) -> Any:
    check_allowed(request, {"radius_km", "limit", "slug"})
    asset = db.scalar(select(Asset).where(Asset.public_id == public_id, *asset_visibility_filter()))
    if asset is None:
        raise not_found(request.url.path)
    if asset.geom is None:
        data: list[dict[str, Any]] = []
        meta = build_meta("proposal", tier="public")
        return build_list_envelope(
            data, meta=meta, licence_summary=build_licence_summary([]), page=build_page(None, None, False)
        )

    radius_param = request.query_params.get("radius_km")
    radius_km = 25.0
    if radius_param is not None:
        try:
            radius_km = float(radius_param)
        except ValueError as exc:
            raise validation_error("radius_km", "radius_km must be a number", request.url.path) from exc
    if not (0 < radius_km <= 100):
        raise validation_error("radius_km", "radius_km must be > 0 and <= 100", request.url.path)

    limit = clamp_limit(_int_param(request, "limit"))
    asset_lon, asset_lat = asset.geom

    # Exact-precision proposals only (ADR 0008: "region-grade and none-grade proposals are never
    # returned here") *as served*: an exact row whose licence forbids raw publication is region
    # grade on this surface (`location_exact_permitted`, the restricted-precision rule) and is
    # excluded, since "within N km of this asset" would otherwise disclose the withheld point.
    # Public-tier visibility rules -- a bounding box in SQL first (cheap,
    # generous: 1 degree of latitude is ~111 km, so `radius_km / 100` degrees is always an
    # over-approximation at `radius_km <= 100`), the exact haversine cut in Python after.
    deg_pad = max(radius_km / 100.0, 0.05)
    bbox = (
        asset_lon - deg_pad,
        asset_lat - deg_pad,
        asset_lon + deg_pad,
        asset_lat + deg_pad,
    )
    stmt = (
        select(Proposal)
        .join(Location, Location.id == Proposal.location_id)
        .where(
            *proposal_public_filter(),
            Location.precision == "exact",
            location_exact_permitted(),
            Location.geom.is_not(None),
        )
        .options(selectinload(Proposal.sources))
    )
    candidates = list(db.scalars(stmt).all())
    within: list[tuple[float, Proposal]] = []
    for p in candidates:
        if p.location is None:
            continue
        placement = effective_placement(p.location)
        if placement.downgraded or placement.geom is None:  # pragma: no cover - excluded in SQL
            continue
        lon, lat = placement.geom
        if not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        distance_km = _haversine_km(asset_lon, asset_lat, lon, lat)
        if distance_km <= radius_km:
            within.append((distance_km, p))
    within.sort(key=lambda pair: pair[0])
    page = within[:limit]

    data = []
    licence_rows = []
    for distance_km, p in page:
        row = serialize_proposal(p)
        row["distance_km"] = round(distance_km, 3)
        data.append(row)
        for s in p.sources:
            if s.active:
                licence_rows.append(licence_summary_row(s.source, s.source.licence, s.retrieved_at))

    meta = build_meta("proposal", tier="public")
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(None, None, len(within) > limit),
    )


# ---------------------------------------------------------------------- organization -> assets
@router.get("/v1/organizations/{public_id}/assets")
def list_organization_assets(
    public_id: str, request: Request, db: Annotated[Session, Depends(get_db)]
) -> Any:
    check_allowed(request, {"limit", "cursor"})
    org = db.scalar(select(Organization).where(Organization.public_id == public_id))
    if org is None:
        raise not_found(request.url.path)

    limit = clamp_limit(_int_param(request, "limit"))
    stmt = (
        select(AssetOwner)
        .join(Asset, Asset.id == AssetOwner.asset_id)
        .where(AssetOwner.organization_id == org.id, *asset_visibility_filter())
        .options(selectinload(AssetOwner.asset))
    )
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
    data = []
    licence_rows = []
    for edge in rows:
        asset_row = serialize_asset(edge.asset, include_owners=False)
        asset_row["role"] = edge.role
        asset_row["share_pct"] = float(edge.share_pct) if edge.share_pct is not None else None
        data.append(asset_row)
        licence_rows.append(
            licence_summary_row(edge.asset.source, edge.asset.licence, edge.asset.retrieved_at)
        )

    meta = build_meta(lag_days=0, tier="public")
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )
