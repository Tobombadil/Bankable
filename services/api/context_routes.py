"""Built-infrastructure context layer (docs/00-PLAN.md decision 2026-09-14; docs/21 §3.20):
existing operating plants drawn *beneath* the proposals map. `GET /v1/context/plants/geo` is
public-domain data (this sprint's only source, EIA-860M, is US federal work) and carries no
publish delay and no tier gating — every caller, credentialed or not, sees the same response;
the standard per-IP rate limit in `services/api/app.py`'s `standard_headers` middleware still
applies to anonymous callers exactly as it does to every other route.

Mounted onto `services.api.app.app` with one `include_router` call, per this task's "extend,
don't rewrite" instruction for that file.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from pipeline.normalize import TECH_RULES
from services.api.context_geo import build_plant_feature_collection
from services.api.deps import get_db
from services.api.errors import validation_error
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

#: Exactly the `BuiltPlant` columns `services/api/context_geo.py` reads, mirroring
#: `services/api/app.py`'s `_GEO_PROPOSAL_COLUMNS` pattern — a column this endpoint's feature
#: builder starts reading later must be added here too, or SQLAlchemy silently reintroduces a
#: per-row deferred-load query rather than failing loudly.
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


def _parse_bbox(value: str, instance: str) -> tuple[float, float, float, float]:
    """Duplicated from `services/api/app.py::_parse_bbox` (four lines) rather than imported, to
    avoid a circular import: `app.py` mounts this router with `include_router`, so this module
    must not import back from `app.py`."""
    parts = value.split(",")
    if len(parts) != 4:
        raise validation_error("bbox", "bbox must be min_lon,min_lat,max_lon,max_lat", instance)
    a, b, c, d = (float(p) for p in parts)
    return (a, b, c, d)


def _apply_context_plant_filters(stmt: sa.Select[Any], request: Request) -> sa.Select[Any]:
    qp = request.query_params
    if v := qp.get("technology"):
        technologies = csv_param(v) or []
        unknown = [t for t in technologies if t not in TECHNOLOGY_VOCAB]
        if unknown:
            raise validation_error(
                "technology", f"unknown technology value(s): {', '.join(unknown)}", request.url.path
            )
        stmt = stmt.where(BuiltPlant.technology.in_(technologies))
    if v := qp.get("country"):
        stmt = stmt.where(BuiltPlant.country.in_([c.upper() for c in csv_param(v) or []]))
    return stmt


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

    plottable_stmt = _apply_context_plant_filters(
        select(BuiltPlant).where(BuiltPlant.geom.is_not(None)).options(load_only(*_CONTEXT_PLANT_COLUMNS)),
        request,
    )
    plants = list(db.scalars(plottable_stmt).all())

    totals_stmt = _apply_context_plant_filters(
        select(BuiltPlant.technology).where(BuiltPlant.geom.is_not(None)), request
    )
    technology_rows = db.execute(totals_stmt).all()
    technology_counts: dict[str, int] = defaultdict(int)
    for (technology,) in technology_rows:
        if technology:
            technology_counts[technology] += 1
    records_total = len(technology_rows)

    fc = build_plant_feature_collection(
        plants,
        bbox=bbox,
        zoom=zoom,
        records_total=records_total,
        technology_counts=dict(technology_counts),
    )

    agg_stmt = _apply_context_plant_filters(
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
        request,
    )
    licence_rows = [tuple(row) for row in db.execute(agg_stmt).all()]
    licence_summary = licence_summary_from_source_aggregates(licence_rows)  # type: ignore[arg-type]

    meta = build_meta(lag_days=0, tier="public")
    return build_envelope(fc, meta=meta, licence_summary=licence_summary)
