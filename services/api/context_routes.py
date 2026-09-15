"""Built-infrastructure context layer (docs/00-PLAN.md decision 2026-09-14; docs/21 §3.20):
existing operating plants drawn *beneath* the proposals map. `GET /v1/context/plants/geo` is
public-domain data (this sprint's only source, EIA-860M, is US federal work) and carries no
publish delay and no tier gating — every caller, credentialed or not, sees the same response;
the standard per-IP rate limit in `services/api/app.py`'s `standard_headers` middleware still
applies to anonymous callers exactly as it does to every other route.

Mounted onto `services.api.app.app` with one `include_router` call, per this task's "extend,
don't rewrite" instruction for that file.

Pan/zoom performance (coordinator follow-up, 2026-09-15): measured against the real 14,659-row
load, the original version (one `select(BuiltPlant)` with its `Source`/`Licence` eagerly joined,
for every visible plant, on every request) cost 860-960 ms per pan/zoom — ORM hydration and the
join, not clustering. This version keeps a process-local cache of the *whole* placed-plant table
as lightweight `PlantIndexRow` tuples (`_get_plant_index`), invalidated by one cheap
`(count, max(updated_at))` aggregate query per request; `technology`/`country` filtering happens
in Python over that cached index, never in SQL, so a re-filter never touches the database. A full
`BuiltPlant` (name, technologies split, source block, …) is fetched by id — bounded to at most
`SPLIT_THRESHOLD` rows — only for the plants that actually render as individual markers. See
`services/README.md` "Context layer endpoints — pan/zoom performance" for the measured before/after.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from pipeline.normalize import TECH_RULES
from services.api.context_geo import PlantIndexRow, build_plant_feature_collection
from services.api.deps import get_db
from services.api.errors import validation_error
from services.api.geo import SPLIT_THRESHOLD, _in_bbox
from services.api.params import check_allowed, csv_param
from services.api.serialize import (
    build_envelope,
    build_meta,
    licence_summary_from_source_aggregates,
)
from services.db.models import BuiltPlant, Licence, Source

router = APIRouter()

#: Same classification vocabulary the connectors write (`pipeline.normalize.classify_tech`),
#: imported rather than re-listed so the two never drift (`unknown`/`other` are `classify_tech`'s
#: own fallback values, added here since `TECH_RULES` itself only enumerates the matched cases).
TECHNOLOGY_VOCAB: frozenset[str] = frozenset({tech for _, tech, _ in TECH_RULES} | {"unknown", "other"})

#: Exactly the `BuiltPlant` columns `services/api/context_geo.py::_plant_feature`/`_plant_source`
#: read, mirroring `services/api/app.py`'s `_GEO_PROPOSAL_COLUMNS` pattern — a column this
#: endpoint's feature builder starts reading later must be added here too, or SQLAlchemy silently
#: reintroduces a per-row deferred-load query rather than failing loudly. Used only for the
#: bounded (`<= SPLIT_THRESHOLD`) plant-details fetch, never for the table-wide index below.
_CONTEXT_PLANT_COLUMNS = (
    BuiltPlant.id,
    BuiltPlant.name,
    BuiltPlant.operator_name,
    BuiltPlant.technology,
    BuiltPlant.technology_raw,
    BuiltPlant.technologies,
    BuiltPlant.capacity_mw,
    BuiltPlant.generator_count,
    BuiltPlant.earliest_operating_year,
    BuiltPlant.geom,
    BuiltPlant.state_code,
    BuiltPlant.county_name,
    BuiltPlant.country,
    BuiltPlant.source_id,
    BuiltPlant.source_url,
    BuiltPlant.retrieved_at,
    BuiltPlant.licence_id,
)


# ------------------------------------------------------------------- process-local index cache
@dataclass(frozen=True)
class _PlantIndexCache:
    #: `(bind_id, row_count, max_updated_at_iso)`. `bind_id` namespaces the cache per `Engine`
    #: (`id(db.get_bind())`) so two callers on different databases in the same process (every test
    #: in this file, each with its own in-memory SQLite engine) can never read each other's cached
    #: rows — in production there is exactly one engine for the process's lifetime, so this never
    #: changes the cache's hit rate there, only its safety under test.
    key: tuple[int, int, str | None]
    rows: tuple[PlantIndexRow, ...]


_index_cache: _PlantIndexCache | None = None
_index_cache_lock = threading.Lock()


def _reset_plant_index_cache() -> None:
    """Test-only hook (mirrors `services.api.ratelimit.default_limiter.reset()`) — never called
    from request-handling code, only from this module's own tests and `test_context_routes.py`'s
    autouse fixture, so one test's seeded plants can never leak into another's cached index."""
    global _index_cache
    with _index_cache_lock:
        _index_cache = None


def _plant_index_cache_key(db: Session) -> tuple[int, int, str | None]:
    bind_id = id(db.get_bind())
    count, max_updated_at = db.execute(
        select(sa.func.count(), sa.func.max(BuiltPlant.updated_at)).where(BuiltPlant.geom.is_not(None))
    ).one()
    return (bind_id, count, max_updated_at.isoformat() if max_updated_at is not None else None)


def _rebuild_plant_index(db: Session) -> tuple[PlantIndexRow, ...]:
    """One full-table scan of `built_plant`, no `Source`/`Licence` join and no ORM hydration — the
    fix for the measured 860-960 ms cost (module docstring). Dialect-aware exactly the way
    `services/db/types.py` branches: on SQLite `geom` is JSON text (`GeographyPoint`'s SQLite
    fallback), decoded here in Python; on Postgres `geography` has no such Python-side decode, so
    `ST_X`/`ST_Y` do the split in SQL instead. The Postgres branch is written to spec but not
    exercised in this sandbox (no Postgres installed here), the same caveat every other
    dialect-specific path in this service carries (e.g.
    `services/api/app.py::_opportunity_technologies_filter`).

    `id` and (on SQLite) `geom` are selected `CAST(... AS TEXT)` rather than as their mapped
    `GUID`/`GeographyPoint` columns, deliberately bypassing those `TypeDecorator`s'
    `process_result_value` — profiled against the real ~14,700-row load
    (`services/README.md` "Context layer endpoints — pan/zoom performance"), that per-value
    decorator dispatch was roughly half this query's cost even *without* touching `Source`/
    `Licence`; casting to plain text and parsing `id`/`geom` directly (`id` is already the
    decorator's own `str(uuid)` format so no parsing is needed at all; `geom`'s JSON is parsed with
    one `json.loads`) cut the measured cold-cache rebuild from ~140 ms to ~50 ms.
    """
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    rows: list[PlantIndexRow] = []
    if dialect == "postgresql":
        id_col = sa.cast(BuiltPlant.id, sa.Text)
        lon_col = sa.func.ST_X(BuiltPlant.geom)
        lat_col = sa.func.ST_Y(BuiltPlant.geom)
        stmt = select(
            id_col, lon_col, lat_col, BuiltPlant.technology, BuiltPlant.capacity_mw, BuiltPlant.country
        ).where(BuiltPlant.geom.is_not(None))
        for plant_id, lon, lat, technology, capacity_mw, country in db.execute(stmt).all():
            rows.append(
                PlantIndexRow(
                    id=plant_id,
                    lon=float(lon),
                    lat=float(lat),
                    technology=technology,
                    capacity_mw=float(capacity_mw) if capacity_mw is not None else None,
                    country=country,
                )
            )
        return tuple(rows)

    id_col = sa.cast(BuiltPlant.id, sa.Text)
    geom_col = sa.cast(BuiltPlant.geom, sa.Text)
    stmt = select(id_col, geom_col, BuiltPlant.technology, BuiltPlant.capacity_mw, BuiltPlant.country).where(
        BuiltPlant.geom.is_not(None)
    )
    for plant_id, geom_text, technology, capacity_mw, country in db.execute(stmt).all():
        if geom_text is None:  # pragma: no cover - excluded by the WHERE clause; defensive only
            continue
        geom = json.loads(geom_text)
        rows.append(
            PlantIndexRow(
                id=plant_id,
                lon=geom["lon"],
                lat=geom["lat"],
                technology=technology,
                capacity_mw=float(capacity_mw) if capacity_mw is not None else None,
                country=country,
            )
        )
    return tuple(rows)


def _get_plant_index(db: Session) -> tuple[PlantIndexRow, ...]:
    """Cached `built_plant` index, rebuilt only when `(row_count, max(updated_at))` over the
    placed rows changes — any insert, update or delete on the table bumps one of those two
    (`updated_at` has `onupdate=utcnow`, `services/db/models.py` `TimestampMixin`), so a stale
    cache is never served. One aggregate query is still paid on every request to check the key;
    that query alone (no join, no per-row hydration) is the cheap part measured in
    `services/README.md`."""
    global _index_cache
    key = _plant_index_cache_key(db)
    with _index_cache_lock:
        cached = _index_cache
        if cached is not None and cached.key == key:
            return cached.rows
    rows = _rebuild_plant_index(db)
    with _index_cache_lock:
        _index_cache = _PlantIndexCache(key=key, rows=rows)
    return rows


# ------------------------------------------------------------------------------- filter parsing
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


def _filter_index(
    index: tuple[PlantIndexRow, ...], technologies: list[str] | None, countries: list[str] | None
) -> list[PlantIndexRow]:
    """Filters the cached table-wide index in Python — never re-queries the database per filter
    (coordinator follow-up point 3: "keep totals correct under the technology/country filters, but
    don't cache per filter"), so `records_total`/`technology_counts` computed from the result are
    always correct for the request's own filters even though the cache itself is unfiltered."""
    rows: list[PlantIndexRow] = list(index)
    if technologies is not None:
        tech_set = set(technologies)
        rows = [r for r in rows if r.technology in tech_set]
    if countries is not None:
        country_set = set(countries)
        rows = [r for r in rows if r.country in country_set]
    return rows


def _apply_sql_filters(
    stmt: sa.Select[Any], technologies: list[str] | None, countries: list[str] | None
) -> sa.Select[Any]:
    """Same two filters as `_filter_index`, in SQL — used only for the licence-summary aggregate
    query below, which is cheap (a `GROUP BY source_id, licence_id` over a handful of distinct
    sources) and stays a live query rather than joining the cache's concerns."""
    if technologies is not None:
        stmt = stmt.where(BuiltPlant.technology.in_(technologies))
    if countries is not None:
        stmt = stmt.where(BuiltPlant.country.in_(countries))
    return stmt


def _fetch_plant_details(db: Session, ids: list[str]) -> dict[str, BuiltPlant]:
    """Full `BuiltPlant` rows (with `Source`/`Licence` eager-joined, the model's own default) for
    exactly `ids` — called only for the `<= SPLIT_THRESHOLD` in-view rows that render as individual
    markers, never for the whole table (that was the 860-960 ms cost this task fixes)."""
    if not ids:
        return {}
    stmt = select(BuiltPlant).where(BuiltPlant.id.in_(ids)).options(load_only(*_CONTEXT_PLANT_COLUMNS))
    return {str(p.id): p for p in db.scalars(stmt).all()}


def _parse_bbox(value: str, instance: str) -> tuple[float, float, float, float]:
    """Duplicated from `services/api/app.py::_parse_bbox` (four lines) rather than imported, to
    avoid a circular import: `app.py` mounts this router with `include_router`, so this module
    must not import back from `app.py`."""
    parts = value.split(",")
    if len(parts) != 4:
        raise validation_error("bbox", "bbox must be min_lon,min_lat,max_lon,max_lat", instance)
    a, b, c, d = (float(p) for p in parts)
    return (a, b, c, d)


@router.get("/v1/context/plants/geo")
def get_context_plants_geo(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Any:
    """Same envelope/clustering contract as `GET /v1/proposals/geo` (`services/api/geo.py`), over
    `built_plant` instead of `proposal`. No `AuthContext`/tier dependency: this is context data,
    not a proposal, so it carries no lag and no per-tier visibility predicate (docs/00-PLAN.md
    2026-09-14: "US first, ... ships with an identifier-free measurement" — the layer itself is
    free on every tier)."""
    check_allowed(request, {"bbox", "zoom", "technology", "country"})
    bbox_param = request.query_params.get("bbox")
    zoom_param = request.query_params.get("zoom")
    if not bbox_param or zoom_param is None:
        raise validation_error("bbox", "bbox and zoom are required", request.url.path)
    try:
        bbox = _parse_bbox(bbox_param, request.url.path)
        zoom = int(zoom_param)
    except ValueError as exc:
        raise validation_error("bbox", "bbox/zoom malformed", request.url.path) from exc

    technologies = _technology_filter_values(request)
    countries = _country_filter_values(request)

    index = _get_plant_index(db)
    filtered = _filter_index(index, technologies, countries)

    technology_counts: dict[str, int] = defaultdict(int)
    for row in filtered:
        if row.technology:
            technology_counts[row.technology] += 1
    records_total = len(filtered)

    # Decided here, not inside `build_plant_feature_collection`, so the plant-details query only
    # ever runs when it is actually needed — but using the exact same `_in_bbox`/`SPLIT_THRESHOLD`
    # logic that function uses internally, so the two decisions can never disagree.
    in_view = [row for row in filtered if _in_bbox(row.lon, row.lat, bbox)]
    plant_details = (
        _fetch_plant_details(db, [row.id for row in in_view]) if len(in_view) <= SPLIT_THRESHOLD else None
    )

    fc = build_plant_feature_collection(
        filtered,
        bbox=bbox,
        zoom=zoom,
        records_total=records_total,
        technology_counts=dict(technology_counts),
        plant_details=plant_details,
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
            sa.func.max(BuiltPlant.retrieved_at),
            sa.func.count(),
        )
        .select_from(BuiltPlant)
        .join(Source, Source.id == BuiltPlant.source_id)
        .join(Licence, Licence.id == BuiltPlant.licence_id)
        .where(BuiltPlant.geom.is_not(None))
        .group_by(Source.id, Licence.id),
        technologies,
        countries,
    )
    licence_rows = [tuple(row) for row in db.execute(agg_stmt).all()]
    licence_summary = licence_summary_from_source_aggregates(licence_rows)  # type: ignore[arg-type]

    meta = build_meta(lag_days=0, tier="public")
    return build_envelope(fc, meta=meta, licence_summary=licence_summary)
