"""FastAPI app: public-tier operations of `api/openapi.yaml` (Sprint 2 backend brief).

Implemented (all `x-tier: public`): proposals (list, geo, detail, events, sources), opportunities
(the same five), organizations (list, detail, proposals, opportunities), the global event feed and
get-by-id, sources and licences registers, vocabularies, health, and the RSS/JSON Feed twins.

Deliberately not implemented this sprint (see services/README.md "Open decisions"):
  - `/v1/proposals/{id}/matches`, `/v1/opportunities/{id}/matches`, `/v1/matches*` — no
    match-producing pipeline exists yet (docs/21 `match` table has no writer before Sprint 3).
  - `/v1/documents/{id}` — no document ingestion this sprint (out of the task's entity list).
  - `/v1/intake/*`, `/v1/reports` — `x-sprint: 3` in api/openapi.yaml.
  - Pro, API-key and admin surfaces — explicitly out of scope for this sprint.
  - Sitemaps (`/sitemap.xml`) — a web/ concern (docs/23 §9.2 open decision 11), not this service.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from collections import defaultdict
from typing import Any

import sqlalchemy as sa
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import (
    InstrumentedAttribute,
    Session,
    contains_eager,
    load_only,
    noload,
    selectinload,
)

from services.api.auth import AuthContext, get_auth_context
from services.api.common import WEB_HOST, ensure_aware, new_request_id, utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, problem_exception_handler, validation_error
from services.api.feeds import render_json_feed, render_rss
from services.api.geo import build_geo_feature_collection
from services.api.pagination import clamp_limit, paginate
from services.api.params import check_allowed, csv_param
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    event_licence_row,
    licence_summary_from_source_aggregates,
    licence_summary_row,
    serialize_event,
    serialize_licence,
    serialize_opportunity,
    serialize_organization,
    serialize_proposal,
    serialize_source,
)
from services.api.visibility import (
    event_public_filter,
    event_visibility_filter,
    opportunity_public_filter,
    opportunity_visibility_filter,
    proposal_public_filter,
    proposal_visibility_filter,
)
from services.db.models import (
    Event,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    Source,
)
from services.ids import public_id
from services.ingest.lag import LAG_DAYS_BY_KIND

app = FastAPI(
    title="Platform API",
    version="1.0.0-draft",
    description="Public tier only (Sprint 2 backend brief). See api/openapi.yaml for the full contract.",
)
app.add_exception_handler(ProblemError, problem_exception_handler)

# Pro/API tier and alerts (Sprint 2 backend brief "Pro tier and alerts"): session and API-key
# auth, saved searches, alerts, keys, webhooks, the private saved-search feed, and the interim
# admin entitlement-grant endpoint. Kept in its own module (services/api/pro.py) per that task's
# "extend, don't rewrite" instruction for this file — one import and one include_router call.
from services.api.pro import router as pro_router  # noqa: E402 - after `app` exists, by design

app.include_router(pro_router)


@app.middleware("http")
async def standard_headers(request: Request, call_next: Any) -> Response:
    """`X-Request-Id` and static rate-limit headers on every response (docs/23 §1, §6; this
    sprint's brief: "rate-limit headers (static values for now)")."""
    response: Response = await call_next(request)
    response.headers["X-Request-Id"] = new_request_id()
    response.headers["RateLimit-Limit"] = "60"
    response.headers["RateLimit-Remaining"] = "59"
    response.headers["RateLimit-Reset"] = "3600"
    response.headers["RateLimit-Policy"] = '60;w=3600;policy="public-read"'
    if request.url.path.startswith("/v1/") and request.method == "GET":
        response.headers["Cache-Control"] = "public, max-age=300"
    return response


LIST_COMMON = {"limit", "cursor", "include", "sort", "q"}


# ------------------------------------------------------------------------------------- proposals
PROPOSAL_FILTERS = {
    "kind",
    "technology",
    "lifecycle_state",
    "jurisdiction",
    "iso",
    "state",
    "source_id",
    "capacity_mw[gte]",
    "capacity_mw[lte]",
    "slug",
}
PROPOSAL_SORT_ALLOWLIST = {"last_changed", "first_seen", "capacity_mw", "name_canonical"}


def _apply_proposal_filters(stmt: sa.Select[Any], request: Request) -> sa.Select[Any]:
    qp = request.query_params
    if v := qp.get("kind"):
        stmt = stmt.where(Proposal.kind.in_(csv_param(v)))
    if v := qp.get("technology"):
        stmt = stmt.where(Proposal.technology.in_(csv_param(v)))
    if v := qp.get("lifecycle_state"):
        stmt = stmt.where(Proposal.lifecycle_state.in_(csv_param(v)))
    if v := qp.get("jurisdiction"):
        stmt = stmt.where(Proposal.jurisdiction.in_(csv_param(v)))
    if v := qp.get("iso"):
        stmt = stmt.where(Proposal.iso.in_(csv_param(v)))
    if v := qp.get("source_id"):
        stmt = stmt.join(ProposalSource, ProposalSource.proposal_id == Proposal.id).where(
            ProposalSource.source_id.in_(csv_param(v)), ProposalSource.active.is_(True)
        )
    if v := qp.get("capacity_mw[gte]"):
        stmt = stmt.where(Proposal.capacity_mw >= float(v))
    if v := qp.get("capacity_mw[lte]"):
        stmt = stmt.where(Proposal.capacity_mw <= float(v))
    if v := qp.get("slug"):
        stmt = stmt.where(Proposal.slug == v)
    if v := qp.get("q"):
        like = f"%{v.lower()}%"
        stmt = stmt.where(func.lower(Proposal.name_canonical).like(like))
    return stmt


def _proposal_query_with_filters(
    request: Request, entitlement: str = "public"
) -> sa.Select[tuple[Proposal]]:
    # `Proposal.sources` is a `viewonly` relationship with no eager default (unlike `sponsor`/
    # `location`, both `lazy="joined"`), so a caller that reads `.sources` over a whole result set
    # (`_proposal_licence_rows`) would otherwise issue one query per proposal -- fine at this list
    # endpoint's page size (<=200), unlike the geo endpoint's unpaginated full-viewport set, which
    # uses `_proposal_geo_plottable_query` below instead (services/README.md "Sprint 2 fixes").
    #
    # `entitlement` (Pro tier and alerts, task item 2): "public" reads `public_at` as before;
    # "pro"/"api" read `published_at` instead (services/api/visibility.py
    # `proposal_visibility_filter`) — the same list/detail code path serves every tier, only the
    # predicate changes, per docs/21 §5.4's "one predicate" design.
    stmt = (
        select(Proposal)
        .where(*proposal_visibility_filter(entitlement))
        .options(selectinload(Proposal.sources))
    )
    return _apply_proposal_filters(stmt, request)


#: Exactly the `Proposal`/`Location` columns `services/api/geo.py` reads for a placed feature
#: (individual marker or cluster member) plus its identity/join keys. Kept as an explicit list
#: (rather than loading every column) because unused-column hydration -- not the join itself --
#: turned out to be most of the residual cost at the real ~8,200-row placed set: even a bare,
#: no-relationship `select(Proposal)` over every column took ~0.44s of pure ORM row construction;
#: narrowing to these columns cuts that to ~0.12-0.25s (services/README.md "Sprint 2 fixes" has the
#: full before/after). Any column `services/api/geo.py` starts reading later needs adding here too
#: -- SQLAlchemy's default `load_only` behaviour for a column left out is a silent per-row
#: deferred-load query on first access, not an error, so a gap here would quietly reintroduce an
#: N+1 rather than fail loudly; nothing but the endpoint's own timing budget would catch it.
_GEO_PROPOSAL_COLUMNS = (
    Proposal.id,
    Proposal.public_id,
    Proposal.slug,
    Proposal.name_canonical,
    Proposal.kind,
    Proposal.technology,
    Proposal.lifecycle_state,
    Proposal.capacity_mw,
    Proposal.last_changed,
    Proposal.location_id,
)
_GEO_LOCATION_COLUMNS = (
    Location.id,
    Location.geom,
    Location.precision,
    Location.county_name,
    Location.state_code,
    Location.licence_id,
)


def _proposal_geo_plottable_query(
    request: Request, entitlement: str = "public"
) -> sa.Select[tuple[Proposal]]:
    """Same filters as `_proposal_query_with_filters`, restricted to proposals that can actually
    be placed on the map (`location` present with a resolved `geom`) and loaded for `GET
    /v1/proposals/geo`'s access pattern: every visible *placeable* proposal (not one page of ~50),
    over only the columns `services/api/geo.py` reads (`_GEO_PROPOSAL_COLUMNS`/
    `_GEO_LOCATION_COLUMNS` above).

    Restricting to placeable rows in SQL (an inner join, `Location.geom.is_not(None)`) rather than
    loading every visible proposal and filtering the ~2,200 unplaced ones out in Python matters at
    the real ~10,400-row proposal set — `_proposal_geo_totals` below still counts every visible
    proposal (placeable or not) for `records`/`lifecycle_state_counts`/`unplaced_count`, just via a
    separate, much cheaper column-only query instead of full ORM hydration.

    `sponsor` (the list endpoint's default, `lazy="joined"` on the model, never read here) is
    dropped entirely; `location`'s own `lazy="joined"` `source`/`licence` (needed only for the
    below-`SPLIT_THRESHOLD` individual-feature path's restricted-precision check,
    `services/api/geo.py::_precision_reason`) fall back to a per-record lazy load instead of an
    eager join on every row; `.sources` is not loaded here at all -- `_source_licence_aggregate`
    below computes the licence summary with one SQL aggregate instead, and the individual-feature
    path lazy-loads `.sources` per record (bounded by `SPLIT_THRESHOLD`, so at most a few hundred
    small queries). Together with the visibility indexes (services/db/models.py), this is the
    services/README.md "Sprint 2 fixes" geo timing.
    """
    loc_load = contains_eager(Proposal.location)
    stmt = (
        select(Proposal)
        .join(Location, Location.id == Proposal.location_id)
        .where(*proposal_visibility_filter(entitlement), Location.geom.is_not(None))
        .options(
            load_only(*_GEO_PROPOSAL_COLUMNS),
            noload(Proposal.sponsor),
            loc_load.load_only(*_GEO_LOCATION_COLUMNS),
            loc_load.lazyload(Location.source),
            loc_load.lazyload(Location.licence),
        )
    )
    return _apply_proposal_filters(stmt, request)


def _proposal_geo_totals(
    db: Session, request: Request, entitlement: str = "public"
) -> tuple[int, dict[str, int], dict[str, int], int]:
    """`(records_total, lifecycle_state_counts, technology_counts, unplaced_count)` over every
    visible proposal matching `request`'s filters (not just the placeable subset
    `_proposal_geo_plottable_query` loads) — one column-only query (three plain columns, not full
    `Proposal`/`Location` ORM entities) rather than four separate `COUNT`/`GROUP BY` round trips
    each re-evaluating the visibility predicate, or hydrating every row as an ORM object just to
    tally two of its columns in Python (services/README.md "Sprint 2 fixes").
    """
    stmt = _apply_proposal_filters(
        select(Proposal.lifecycle_state, Proposal.technology, Location.geom)
        .select_from(Proposal)
        .outerjoin(Location, Location.id == Proposal.location_id)
        .where(*proposal_visibility_filter(entitlement)),
        request,
    )
    rows = db.execute(stmt).all()
    lifecycle_counts: dict[str, int] = defaultdict(int)
    technology_counts: dict[str, int] = defaultdict(int)
    unplaced = 0
    for lifecycle_state, technology, geom in rows:
        lifecycle_counts[lifecycle_state] += 1
        if technology:
            technology_counts[technology] += 1
        if geom is None:
            unplaced += 1
    return len(rows), dict(lifecycle_counts), dict(technology_counts), unplaced


def _sort_spec(request: Request, allowlist: set[str], default: str) -> tuple[str, bool]:
    raw = request.query_params.get("sort", default)
    token = raw.split(",")[0]
    ascending = not token.startswith("-")
    field = token[1:] if not ascending else token
    if field not in allowlist:
        raise validation_error(
            "sort", f"sort field {field!r} is not allowlisted for this resource", request.url.path
        )
    return field, ascending


def _proposal_licence_rows(proposals: list[Proposal]) -> list[dict[str, Any]]:
    rows = []
    for p in proposals:
        for s in p.sources:
            if s.active:
                rows.append(licence_summary_row(s.source, s.source.licence, s.retrieved_at))
    return rows


@app.get("/v1/proposals")
def list_proposals(
    request: Request, db: Session = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)
) -> Any:
    check_allowed(request, LIST_COMMON | PROPOSAL_FILTERS)
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, PROPOSAL_SORT_ALLOWLIST, "-last_changed")
    stmt = _proposal_query_with_filters(request, ctx.entitlement)
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Proposal, field),
        id_column=Proposal.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_proposal(p) for p in rows]
    meta = build_meta("proposal", tier=ctx.entitlement)
    if "count" in (request.query_params.get("include") or "").split(","):
        total = db.scalar(
            select(func.count()).select_from(_proposal_query_with_filters(request, ctx.entitlement).subquery())
        )
        meta["total"] = total
        meta["total_is_estimate"] = total is not None and total > 10000
    env = build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_proposal_licence_rows(rows)),
        page=build_page(next_cursor, None, has_more),
    )
    return env


def _source_licence_aggregate(
    db: Session,
    link_model: type[ProposalSource] | type[OpportunitySource],
    fk_column: InstrumentedAttribute[Any],
    id_subquery: sa.Select[Any],
) -> dict[str, Any]:
    """`licence_summary` for a whole (unpaginated) result set via one `GROUP BY source_id`
    aggregate query, instead of materialising every visible `proposal_source`/`opportunity_source`
    ORM row just to fold them in Python (`_proposal_licence_rows` — fine at a list page's size, the
    dominant remaining cost at the geo endpoints' full-viewport scale after the visibility-index
    fix, services/README.md "Sprint 2 fixes")."""
    agg_stmt = (
        select(
            Source.id,
            Source.name,
            Source.operator,
            Licence.id,
            Licence.name,
            Licence.url,
            Licence.reuse_class,
            func.coalesce(Source.attribution_text, Licence.attribution_text),
            Licence.requires_link_back,
            func.max(link_model.retrieved_at),
            func.count(),
        )
        .select_from(link_model)
        .join(Source, Source.id == link_model.source_id)
        .join(Licence, Licence.id == Source.licence_id)
        .where(fk_column.in_(id_subquery), link_model.active.is_(True))
        .group_by(Source.id, Licence.id)
    )
    rows = [tuple(row) for row in db.execute(agg_stmt).all()]
    return licence_summary_from_source_aggregates(rows)  # type: ignore[arg-type]


@app.get("/v1/proposals/geo")
def get_proposals_geo(
    request: Request, db: Session = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)
) -> Any:
    check_allowed(request, {"bbox", "zoom"} | PROPOSAL_FILTERS | {"q"})
    bbox_param = request.query_params.get("bbox")
    zoom_param = request.query_params.get("zoom")
    if not bbox_param or zoom_param is None:
        raise validation_error("bbox", "bbox and zoom are required", request.url.path)
    try:
        bbox = _parse_bbox(bbox_param, request.url.path)
        zoom = int(zoom_param)
    except ValueError as exc:
        raise validation_error("bbox", "bbox/zoom malformed", request.url.path) from exc
    stmt = _proposal_geo_plottable_query(request, ctx.entitlement)
    plottable = list(db.scalars(stmt).all())
    records_total, lifecycle_counts, technology_counts, unplaced_count = _proposal_geo_totals(
        db, request, ctx.entitlement
    )
    fc = build_geo_feature_collection(
        plottable,
        bbox=bbox,
        zoom=zoom,
        records_total=records_total,
        lifecycle_state_counts=lifecycle_counts,
        technology_counts=technology_counts,
    )
    meta = build_meta("proposal", tier=ctx.entitlement, extra={"unplaced_count": unplaced_count})
    id_subquery = _apply_proposal_filters(
        select(Proposal.id).where(*proposal_visibility_filter(ctx.entitlement)), request
    )
    licence_summary = _source_licence_aggregate(db, ProposalSource, ProposalSource.proposal_id, id_subquery)
    return build_envelope(fc, meta=meta, licence_summary=licence_summary)


@app.get("/v1/proposals/{public_id}")
def get_proposal(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    prop = db.scalar(
        select(Proposal).where(Proposal.public_id == public_id, *proposal_visibility_filter(ctx.entitlement))
    )
    if prop is None:
        raise not_found(request.url.path)
    data = serialize_proposal(prop)
    meta = build_meta("proposal", tier=ctx.entitlement)
    return build_envelope(
        data, meta=meta, licence_summary=build_licence_summary(_proposal_licence_rows([prop]))
    )


@app.get("/v1/proposals/{public_id}/events")
def list_proposal_events(public_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, {"limit", "cursor", "event_type", "sort"})
    prop = db.scalar(select(Proposal).where(Proposal.public_id == public_id, *proposal_public_filter()))
    if prop is None:
        raise not_found(request.url.path)
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, {"seq", "observed_at"}, "-seq")
    stmt = select(Event).where(
        Event.subject_type == "proposal", Event.subject_id == prop.id, *event_public_filter()
    )
    if v := request.query_params.get("event_type"):
        stmt = stmt.where(Event.event_type.in_(csv_param(v)))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Event, field),
        id_column=Event.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [
        serialize_event(
            e,
            subject_public_id=prop.public_id,
            subject_name=prop.name_canonical,
            subject_url=f"{WEB_HOST}/proposals/{prop.slug}",
        )
        for e in rows
    ]
    meta = build_meta("proposal")
    licence_rows = [r for e in rows if (r := event_licence_row(e)) is not None]
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


@app.get("/v1/proposals/{public_id}/sources")
def list_proposal_sources(public_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, {"include"})
    prop = db.scalar(select(Proposal).where(Proposal.public_id == public_id, *proposal_public_filter()))
    if prop is None:
        raise not_found(request.url.path)
    from services.api.serialize import provenance_row

    links = [s for s in prop.sources if s.active]
    data = [provenance_row(s, s.source) for s in links]
    meta = build_meta("proposal")
    rows = [licence_summary_row(s.source, s.source.licence, s.retrieved_at) for s in links]
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary(rows))


# ----------------------------------------------------------------------------------- opportunities
OPPORTUNITY_FILTERS = {
    "kind",
    "status",
    "technologies",
    "jurisdiction",
    "source_id",
    "due_at[from]",
    "due_at[to]",
    "slug",
}
OPPORTUNITY_SORT_ALLOWLIST = {"due_at", "open_at", "last_changed", "budget_amount"}


def _opportunity_technologies_filter(db: Session, values: list[str]) -> ColumnElement[bool]:
    """Any-of over `Opportunity.technologies[]`; an all-source opportunity (empty array) matches
    every value (api/openapi.yaml `Technologies` parameter). `technologies` is a real Postgres
    `ARRAY(Text)` in the canonical migration but a JSON-encoded `TEXT` column on SQLite
    (`services/db/types.py TextArray`, this sprint's test target — services/README.md), so the
    membership test is dialect-specific: Postgres uses the native `&&` overlap operator; SQLite
    matches the JSON-quoted token as a substring, which is exact for the closed technology
    vocabulary (docs/21 §7 — no value contains a quote or backslash). The Postgres branch is
    written to spec but not exercised here, same caveat as every other dialect-specific path in
    this service.
    """
    empty = Opportunity.technologies == []
    dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
    overlap: ColumnElement[bool]
    if dialect == "postgresql":
        overlap = Opportunity.technologies.op("&&")(list(values))
    else:
        # `.like()` on a `TextArray` column would otherwise bind the pattern *through the
        # column's own type* (`services/db/types.py` JSON-encodes it, turning `%"wind"%` into a
        # JSON array of one character per list element) rather than as a plain string -- casting
        # to `Text` first makes the right-hand literal an ordinary string bind again.
        technologies_text = sa.cast(Opportunity.technologies, sa.Text)
        overlap = sa.or_(*(technologies_text.like(f'%"{t}"%') for t in values))
    return sa.or_(empty, overlap)


def _opportunity_query_with_filters(request: Request, db: Session) -> sa.Select[tuple[Opportunity]]:
    # Same N+1 fix as `_proposal_query_with_filters` above, for `Opportunity.sources`.
    stmt = select(Opportunity).where(*opportunity_public_filter()).options(selectinload(Opportunity.sources))
    qp = request.query_params
    status = csv_param(qp.get("status")) or ["open"]
    stmt = stmt.where(Opportunity.status.in_(status))
    if v := qp.get("kind"):
        stmt = stmt.where(Opportunity.kind.in_(csv_param(v)))
    if v := qp.get("technologies"):
        stmt = stmt.where(_opportunity_technologies_filter(db, csv_param(v)))
    if v := qp.get("jurisdiction"):
        stmt = stmt.where(Opportunity.jurisdiction.in_(csv_param(v)))
    if v := qp.get("source_id"):
        stmt = stmt.join(OpportunitySource, OpportunitySource.opportunity_id == Opportunity.id).where(
            OpportunitySource.source_id.in_(csv_param(v)), OpportunitySource.active.is_(True)
        )
    if v := qp.get("due_at[from]"):
        stmt = stmt.where(Opportunity.due_at >= dt.datetime.fromisoformat(v.replace("Z", "+00:00")))
    if v := qp.get("due_at[to]"):
        stmt = stmt.where(Opportunity.due_at <= dt.datetime.fromisoformat(v.replace("Z", "+00:00")))
    if v := qp.get("slug"):
        stmt = stmt.where(Opportunity.slug == v)
    if v := qp.get("q"):
        like = f"%{v.lower()}%"
        stmt = stmt.where(func.lower(Opportunity.title).like(like))
    return stmt


def _opportunity_licence_rows(items: list[Opportunity]) -> list[dict[str, Any]]:
    rows = []
    for o in items:
        for s in o.sources:
            if s.active:
                rows.append(licence_summary_row(s.source, s.source.licence, s.retrieved_at))
    return rows


@app.get("/v1/opportunities")
def list_opportunities(request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, LIST_COMMON | OPPORTUNITY_FILTERS)
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, OPPORTUNITY_SORT_ALLOWLIST, "due_at")
    stmt = _opportunity_query_with_filters(request, db)
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Opportunity, field),
        id_column=Opportunity.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_opportunity(o) for o in rows]
    meta = build_meta("opportunity")
    if "count" in (request.query_params.get("include") or "").split(","):
        total = db.scalar(
            select(func.count()).select_from(_opportunity_query_with_filters(request, db).subquery())
        )
        meta["total"] = total
        meta["total_is_estimate"] = total is not None and total > 10000
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_opportunity_licence_rows(rows)),
        page=build_page(next_cursor, None, has_more),
    )


@app.get("/v1/opportunities/geo")
def get_opportunities_geo(request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, {"bbox", "zoom"} | OPPORTUNITY_FILTERS | {"q"})
    bbox_param = request.query_params.get("bbox")
    zoom_param = request.query_params.get("zoom")
    if not bbox_param or zoom_param is None:
        raise validation_error("bbox", "bbox and zoom are required", request.url.path)
    bbox = _parse_bbox(bbox_param, request.url.path)
    zoom = int(zoom_param)
    stmt = _opportunity_query_with_filters(request, db)
    items = list(db.scalars(stmt).all())
    # `services/ingest/loader.py` never geocodes opportunities (no state/county columns in
    # `pipeline.connectors.base.OPPORTUNITY_COLUMNS` to geocode from -- unlike proposals) so
    # `location` is always null here; every visible opportunity is unplaced by construction, never
    # dropped (docs/04 D-8). This endpoint predates that gap being understood and previously
    # crashed on first access to `Proposal`-only attributes (`lifecycle_state`, `technology`) that
    # `Opportunity` doesn't have, the moment any opportunity matched the filters -- untested and
    # unnoticed because nothing had populated `Opportunity.location_id` yet. Fixed here to the
    # extent this sprint's scope covers (proposals' geo endpoint, task priority): a correct,
    # honestly-empty response rather than a 500. Full parity with the proposal side (a real
    # `status_counts`/`kind_counts` aggregate, `technologies[]` clustering) is a follow-up once
    # opportunities have geometry to place at all.
    technology_counts: dict[str, int] = defaultdict(int)
    for o in items:
        for t in o.technologies or []:
            technology_counts[t] += 1
    fc = build_geo_feature_collection(
        [],
        bbox=bbox,
        zoom=zoom,
        records_total=len(items),
        lifecycle_state_counts={},
        technology_counts=dict(technology_counts),
    )
    unplaced_count = len(items)
    meta = build_meta("opportunity", extra={"unplaced_count": unplaced_count})
    return build_envelope(
        fc, meta=meta, licence_summary=build_licence_summary(_opportunity_licence_rows(items))
    )


@app.get("/v1/opportunities/{public_id}")
def get_opportunity(public_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    opp = db.scalar(
        select(Opportunity).where(Opportunity.public_id == public_id, *opportunity_public_filter())
    )
    if opp is None:
        raise not_found(request.url.path)
    data = serialize_opportunity(opp)
    meta = build_meta("opportunity")
    return build_envelope(
        data, meta=meta, licence_summary=build_licence_summary(_opportunity_licence_rows([opp]))
    )


@app.get("/v1/opportunities/{public_id}/events")
def list_opportunity_events(public_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, {"limit", "cursor", "event_type", "sort"})
    opp = db.scalar(
        select(Opportunity).where(Opportunity.public_id == public_id, *opportunity_public_filter())
    )
    if opp is None:
        raise not_found(request.url.path)
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, {"seq", "observed_at"}, "-seq")
    stmt = select(Event).where(
        Event.subject_type == "opportunity", Event.subject_id == opp.id, *event_public_filter()
    )
    if v := request.query_params.get("event_type"):
        stmt = stmt.where(Event.event_type.in_(csv_param(v)))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Event, field),
        id_column=Event.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [
        serialize_event(
            e,
            subject_public_id=opp.public_id,
            subject_name=opp.title,
            subject_url=f"{WEB_HOST}/opportunities/{opp.slug}",
        )
        for e in rows
    ]
    meta = build_meta("opportunity")
    licence_rows = [r for e in rows if (r := event_licence_row(e)) is not None]
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


@app.get("/v1/opportunities/{public_id}/sources")
def list_opportunity_sources(public_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, {"include"})
    opp = db.scalar(
        select(Opportunity).where(Opportunity.public_id == public_id, *opportunity_public_filter())
    )
    if opp is None:
        raise not_found(request.url.path)
    from services.api.serialize import provenance_row

    links = [s for s in opp.sources if s.active]
    data = [provenance_row(s, s.source) for s in links]
    meta = build_meta("opportunity")
    rows = [licence_summary_row(s.source, s.source.licence, s.retrieved_at) for s in links]
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary(rows))


# ------------------------------------------------------------------------------------ organizations
@app.get("/v1/organizations")
def list_organizations(request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, LIST_COMMON | {"type", "country", "is_curated_issuer"})
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, {"name_canonical"}, "name_canonical")
    stmt = select(Organization).where(Organization.merged_into_id.is_(None))
    qp = request.query_params
    if v := qp.get("type"):
        stmt = stmt.where(Organization.type.in_(csv_param(v)))
    if v := qp.get("country"):
        stmt = stmt.where(Organization.country.in_(csv_param(v)))
    if v := qp.get("is_curated_issuer"):
        stmt = stmt.where(Organization.is_curated_issuer.is_(v.lower() == "true"))
    if v := qp.get("q"):
        like = f"%{v.lower()}%"
        stmt = stmt.where(func.lower(Organization.name_canonical).like(like))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Organization, field),
        id_column=Organization.id,
        ascending=ascending,
        cursor=qp.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_organization(o) for o in rows]
    meta = build_meta(lag_days=0)
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


def _counts_for_org(db: Session, org: Organization) -> tuple[int, int]:
    p = db.scalar(
        select(func.count())
        .select_from(Proposal)
        .where(Proposal.sponsor_org_id == org.id, *proposal_public_filter())
    )
    o = db.scalar(
        select(func.count())
        .select_from(Opportunity)
        .where(Opportunity.issuer_org_id == org.id, *opportunity_public_filter())
    )
    return p or 0, o or 0


@app.get("/v1/organizations/{public_id}")
def get_organization(public_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    org = db.scalar(select(Organization).where(Organization.public_id == public_id))
    if org is None:
        raise not_found(request.url.path)
    p_count, o_count = _counts_for_org(db, org)
    data = serialize_organization(org, proposal_count=p_count, opportunity_count=o_count)
    meta = build_meta(lag_days=0)
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary([]))


@app.get("/v1/organizations/{public_id}/proposals")
def list_organization_proposals(public_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(
        request, {"limit", "cursor", "sort", "kind", "technology", "lifecycle_state", "jurisdiction"}
    )
    org = db.scalar(select(Organization).where(Organization.public_id == public_id))
    if org is None:
        raise not_found(request.url.path)
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, PROPOSAL_SORT_ALLOWLIST, "-last_changed")
    stmt = (
        select(Proposal)
        .where(Proposal.sponsor_org_id == org.id, *proposal_public_filter())
        .options(selectinload(Proposal.sources))
    )
    qp = request.query_params
    if v := qp.get("kind"):
        stmt = stmt.where(Proposal.kind.in_(csv_param(v)))
    if v := qp.get("lifecycle_state"):
        stmt = stmt.where(Proposal.lifecycle_state.in_(csv_param(v)))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Proposal, field),
        id_column=Proposal.id,
        ascending=ascending,
        cursor=qp.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_proposal(p) for p in rows]
    meta = build_meta("proposal")
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_proposal_licence_rows(rows)),
        page=build_page(next_cursor, None, has_more),
    )


@app.get("/v1/organizations/{public_id}/opportunities")
def list_organization_opportunities(public_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, {"limit", "cursor", "sort", "kind", "status", "technologies"})
    org = db.scalar(select(Organization).where(Organization.public_id == public_id))
    if org is None:
        raise not_found(request.url.path)
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, OPPORTUNITY_SORT_ALLOWLIST, "due_at")
    qp = request.query_params
    status = csv_param(qp.get("status")) or ["open"]
    stmt = (
        select(Opportunity)
        .where(
            Opportunity.issuer_org_id == org.id,
            Opportunity.status.in_(status),
            *opportunity_public_filter(),
        )
        .options(selectinload(Opportunity.sources))
    )
    if v := qp.get("kind"):
        stmt = stmt.where(Opportunity.kind.in_(csv_param(v)))
    if v := qp.get("technologies"):
        stmt = stmt.where(_opportunity_technologies_filter(db, csv_param(v)))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Opportunity, field),
        id_column=Opportunity.id,
        ascending=ascending,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_opportunity(o) for o in rows]
    meta = build_meta("opportunity")
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_opportunity_licence_rows(rows)),
        page=build_page(next_cursor, None, has_more),
    )


# ------------------------------------------------------------------------------------------ events
@app.get("/v1/events")
def list_events(request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, LIST_COMMON | {"subject_type", "subject_id", "event_type", "source_id", "since"})
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, {"seq", "observed_at"}, "-seq")
    stmt = select(Event).where(*event_public_filter())
    qp = request.query_params
    if v := qp.get("subject_type"):
        stmt = stmt.where(Event.subject_type.in_(csv_param(v)))
    if v := qp.get("event_type"):
        stmt = stmt.where(Event.event_type.in_(csv_param(v)))
    if v := qp.get("source_id"):
        stmt = stmt.where(Event.source_id.in_(csv_param(v)))
    if v := qp.get("subject_id"):
        subj = _resolve_subject(db, v)
        if subj is None:
            stmt = stmt.where(Event.subject_id == _uuid.UUID(int=0))
        else:
            stmt = stmt.where(Event.subject_id == subj.id)
    if v := qp.get("since"):
        if v.isdigit():
            stmt = stmt.where(Event.seq > int(v))
        else:
            stmt = stmt.where(Event.observed_at > dt.datetime.fromisoformat(v.replace("Z", "+00:00")))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=getattr(Event, field),
        id_column=Event.id,
        ascending=ascending,
        cursor=qp.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_event(e, **_subject_info(db, e)) for e in rows]
    meta = build_meta(lag_days=max((_lag_days_for_subject(e.subject_type) for e in rows), default=14))
    licence_rows = [r for e in rows if (r := event_licence_row(e)) is not None]
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


def _lag_days_for_subject(subject_type: str) -> int:
    """`LAG_DAYS_BY_KIND` is keyed by the `Kind` literal; `event.subject_type` is a plain `str`
    column, so this narrows the lookup instead of an untypeable `dict.get(str, int)` call."""
    if subject_type == "proposal":
        return LAG_DAYS_BY_KIND["proposal"]
    if subject_type == "opportunity":
        return LAG_DAYS_BY_KIND["opportunity"]
    return 14


def _resolve_subject(db: Session, public_id_value: str) -> Proposal | Opportunity | None:
    if public_id_value.startswith("prop_"):
        return db.scalar(select(Proposal).where(Proposal.public_id == public_id_value))
    if public_id_value.startswith("opp_"):
        return db.scalar(select(Opportunity).where(Opportunity.public_id == public_id_value))
    return None


def _subject_info(db: Session, event: Event) -> dict[str, str]:
    if event.subject_type == "proposal":
        p = db.get(Proposal, event.subject_id)
        if p:
            return {
                "subject_public_id": p.public_id,
                "subject_name": p.name_canonical,
                "subject_url": f"{WEB_HOST}/proposals/{p.slug}",
            }
    if event.subject_type == "opportunity":
        o = db.get(Opportunity, event.subject_id)
        if o:
            return {
                "subject_public_id": o.public_id,
                "subject_name": o.title,
                "subject_url": f"{WEB_HOST}/opportunities/{o.slug}",
            }
    return {"subject_public_id": str(event.subject_id), "subject_name": "Unknown", "subject_url": WEB_HOST}


@app.get("/v1/events/{event_id}")
def get_event(event_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    ev = _find_event_by_public_id(db, event_id)
    if ev is None or ev.public_at is None or ensure_aware(ev.public_at) > utcnow():
        raise not_found(request.url.path)
    if ev.licence and ev.licence.reuse_class not in ("open", "attribution"):
        raise not_found(request.url.path)
    data = serialize_event(ev, **_subject_info(db, ev))
    meta = build_meta(lag_days=_lag_days_for_subject(ev.subject_type))
    row = event_licence_row(ev)
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary([row] if row else []))


def _find_event_by_public_id(db: Session, event_id: str) -> Event | None:
    """`evt_<crockford>` ids are synthesised from `event.id` at serialization time (docs/21 §3.10
    has no separate `public_id` column for events); reverse it by scanning candidates is not
    viable at scale, so this walks the crockford alphabet back to a uuid instead."""
    from services.ids import _CROCKFORD

    if not event_id.startswith("evt_"):
        return None
    digits = event_id[4:]
    try:
        n = 0
        for ch in digits:
            n = n * 32 + _CROCKFORD.index(ch)
        candidate = _uuid.UUID(int=n)
    except (ValueError, OverflowError):
        return None
    return db.get(Event, candidate)


# ----------------------------------------------------------------------------------- sources/licences
@app.get("/v1/sources")
def list_sources(request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, {"limit", "cursor", "category", "jurisdiction", "reuse_class", "publish_state"})
    limit = clamp_limit(_int_param(request, "limit"))
    stmt = select(Source)
    qp = request.query_params
    if v := qp.get("category"):
        stmt = stmt.where(Source.category.in_(csv_param(v)))
    if v := qp.get("jurisdiction"):
        stmt = stmt.where(Source.jurisdiction.in_(csv_param(v)))
    if v := qp.get("publish_state"):
        stmt = stmt.where(Source.publish_state.in_(csv_param(v)))
    if v := qp.get("reuse_class"):
        stmt = stmt.join(Licence, Licence.id == Source.licence_id).where(
            Licence.reuse_class.in_(csv_param(v))
        )
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=Source.id,
        id_column=Source.id,
        ascending=True,
        cursor=qp.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [serialize_source(s) for s in rows]
    meta = build_meta(lag_days=0)
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@app.get("/v1/sources/{source_id}")
def get_source(source_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    src = db.get(Source, source_id)
    if src is None:
        raise not_found(request.url.path)
    meta = build_meta(lag_days=0)
    return build_envelope(serialize_source(src), meta=meta, licence_summary=build_licence_summary([]))


@app.get("/v1/licences")
def list_licences(request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, {"limit", "cursor", "reuse_class"})
    limit = clamp_limit(_int_param(request, "limit"))
    stmt = select(Licence)
    if v := request.query_params.get("reuse_class"):
        stmt = stmt.where(Licence.reuse_class.in_(csv_param(v)))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=Licence.id,
        id_column=Licence.id,
        ascending=True,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [
        serialize_licence(
            lic,
            source_count=db.scalar(
                select(func.count()).select_from(Source).where(Source.licence_id == lic.id)
            )
            or 0,
        )
        for lic in rows
    ]
    meta = build_meta(lag_days=0)
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@app.get("/v1/licences/{licence_id}")
def get_licence(licence_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    lic = db.get(Licence, licence_id)
    if lic is None:
        raise not_found(request.url.path)
    count = db.scalar(select(func.count()).select_from(Source).where(Source.licence_id == lic.id)) or 0
    meta = build_meta(lag_days=0)
    return build_envelope(
        serialize_licence(lic, source_count=count), meta=meta, licence_summary=build_licence_summary([])
    )


# ---------------------------------------------------------------------------------- meta / health
PROPOSAL_KIND_VALUES = [
    "generation",
    "storage",
    "load",
    "transmission",
    "pipeline",
    "lng",
    "nuclear",
    "ccs",
    "hydrogen",
    "other",
]
LIFECYCLE_STATE_VALUES = [
    "unknown",
    "announced",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "under_construction",
    "built",
    "withdrawn",
    "cancelled",
]
OPPORTUNITY_KIND_VALUES = ["rfp", "foa", "tender", "auction", "loan_program", "procurement_notice", "program"]
OPPORTUNITY_STATUS_VALUES = [
    "unknown",
    "announced",
    "open",
    "frozen",
    "reinstated",
    "closed",
    "cancelled",
    "awarded",
]
EVENT_TYPE_VALUES = [
    "created",
    "source_linked",
    "source_unlinked",
    "merged",
    "unmerged",
    "alias_added",
    "status_change",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "under_construction",
    "built",
    "withdrawn",
    "cancelled",
    "announced",
    "opened",
    "closed",
    "frozen",
    "reinstated",
    "awarded",
    "due_date_changed",
    "field_changed",
    "capacity_changed",
    "sponsor_changed",
    "location_changed",
    "extraction_accepted",
    "match_added",
    "match_removed",
    "lead_created",
    "published",
    "unpublished",
    "gate_cleared",
    "licence_reclassified",
    "source_health_changed",
    "admin_edit",
    "personal_data_redacted",
    "key_issued",
    "key_revoked",
]
ORGANIZATION_TYPE_VALUES = [
    "developer",
    "ipp",
    "utility",
    "coop",
    "cca",
    "agency",
    "lender",
    "investor",
    "epc",
    "oem",
    "offtaker",
    "other",
]
REUSE_CLASS_VALUES = ["open", "attribution", "restricted", "unknown"]
PUBLISH_STATE_VALUES = ["pending_review", "ingest_only", "api_only", "public", "unpublished"]


def _vocab(values: list[str]) -> list[dict[str, Any]]:
    return [
        {"value": v, "label": v.replace("_", " ").title(), "sort_order": i, "active": True}
        for i, v in enumerate(values)
    ]


@app.get("/v1/meta/vocabularies")
def get_vocabularies(db: Session = Depends(get_db)) -> Any:
    isos = [
        row[0] for row in db.execute(select(Proposal.iso).where(Proposal.iso.is_not(None)).distinct()).all()
    ]
    data = {
        "proposal_kind": _vocab(PROPOSAL_KIND_VALUES),
        "technology": _vocab(
            sorted(
                {
                    row[0]
                    for row in db.execute(
                        select(Proposal.technology).where(Proposal.technology.is_not(None)).distinct()
                    ).all()
                }
            )
        ),
        "lifecycle_state": _vocab(LIFECYCLE_STATE_VALUES),
        "opportunity_kind": _vocab(OPPORTUNITY_KIND_VALUES),
        "opportunity_status": _vocab(OPPORTUNITY_STATUS_VALUES),
        "event_type": _vocab(EVENT_TYPE_VALUES),
        "organization_type": _vocab(ORGANIZATION_TYPE_VALUES),
        "reuse_class": _vocab(REUSE_CLASS_VALUES),
        "publish_state": _vocab(PUBLISH_STATE_VALUES),
        "iso": _vocab(sorted(isos)),
    }
    meta = build_meta(lag_days=0)
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary([]))


@app.get("/v1/health")
def get_health(db: Session = Depends(get_db)) -> Any:
    try:
        db.execute(select(1))
        database_ok = True
    except Exception:
        database_ok = False
    now = utcnow()
    data = {
        "status": "ok" if database_ok else "degraded",
        "api_version": "v1",
        "data_as_of": (now - dt.timedelta(days=LAG_DAYS_BY_KIND["proposal"]))
        .isoformat()
        .replace("+00:00", "Z"),
        "live_as_of": now.isoformat().replace("+00:00", "Z"),
        "lag_days_default": {
            "supply": LAG_DAYS_BY_KIND["proposal"],
            "opportunities": LAG_DAYS_BY_KIND["opportunity"],
        },
        "checks": {
            "database": database_ok,
            "queue": True,
            "rate_limiter_active": True,
            "edge_cache": True,
            "backup_age_hours": None,
            "queue_age_seconds": None,
        },
        "generated_at": now.isoformat().replace("+00:00", "Z"),
    }
    return data


# -------------------------------------------------------------------------------------------- feeds
@app.get("/feeds/proposals.{format}")
def feed_proposals(format: str, request: Request, db: Session = Depends(get_db)) -> Response:
    check_allowed(request, PROPOSAL_FILTERS | {"q"})
    stmt = _proposal_query_with_filters(request)
    proposals = list(db.scalars(stmt.order_by(Proposal.last_changed.desc()).limit(50)).all())
    items = []
    for p in proposals:
        source_row = next((s for s in p.sources if s.active), None)
        items.append(
            {
                "title": f"{p.name_canonical} — {p.lifecycle_state}",
                "url": f"{WEB_HOST}/proposals/{p.slug}",
                "guid": p.public_id,
                "pub_date": p.public_at,
                "creator": (source_row.source.attribution_text or source_row.source.name)
                if source_row
                else "the platform",
                "categories": [p.lifecycle_state, p.kind],
                "description": f"{p.name_canonical}: {p.lifecycle_state} ({p.jurisdiction}).",
                "platform_ext": {
                    "event_type": "status_change",
                    "subject": {
                        "public_id": p.public_id,
                        "name": p.name_canonical,
                        "url": f"{WEB_HOST}/proposals/{p.slug}",
                    },
                    "provenance": [],
                    "licence_summary": build_licence_summary(_proposal_licence_rows([p])),
                    "data_as_of": build_meta("proposal")["data_as_of"],
                },
            }
        )
    return _render_feed(
        format, resource="Proposals", kind="proposal", self_path=str(request.url), items=items
    )


@app.get("/feeds/opportunities.{format}")
def feed_opportunities(format: str, request: Request, db: Session = Depends(get_db)) -> Response:
    check_allowed(request, OPPORTUNITY_FILTERS | {"q"})
    stmt = _opportunity_query_with_filters(request, db)
    items_rows = list(db.scalars(stmt.order_by(Opportunity.last_changed.desc()).limit(50)).all())
    items = []
    for o in items_rows:
        source_row = next((s for s in o.sources if s.active), None)
        items.append(
            {
                "title": f"{o.title} — {o.status}",
                "url": f"{WEB_HOST}/opportunities/{o.slug}",
                "guid": o.public_id,
                "pub_date": o.public_at,
                "creator": (source_row.source.attribution_text or source_row.source.name)
                if source_row
                else "the platform",
                "categories": [o.status, o.kind],
                "description": f"{o.title}: {o.status} ({o.jurisdiction}).",
                "platform_ext": {
                    "event_type": "status_change",
                    "subject": {
                        "public_id": o.public_id,
                        "name": o.title,
                        "url": f"{WEB_HOST}/opportunities/{o.slug}",
                    },
                    "provenance": [],
                    "licence_summary": build_licence_summary(_opportunity_licence_rows([o])),
                    "data_as_of": build_meta("opportunity")["data_as_of"],
                },
            }
        )
    return _render_feed(
        format, resource="Opportunities", kind="opportunity", self_path=str(request.url), items=items
    )


@app.get("/feeds/events.{format}")
def feed_events(format: str, request: Request, db: Session = Depends(get_db)) -> Response:
    check_allowed(request, {"subject_type", "event_type", "source_id"})
    stmt = select(Event).where(*event_public_filter())
    qp = request.query_params
    if v := qp.get("subject_type"):
        stmt = stmt.where(Event.subject_type.in_(csv_param(v)))
    if v := qp.get("event_type"):
        stmt = stmt.where(Event.event_type.in_(csv_param(v)))
    rows = list(db.scalars(stmt.order_by(Event.seq.desc()).limit(50)).all())
    items = []
    for e in rows:
        info = _subject_info(db, e)
        items.append(
            {
                "title": f"{info['subject_name']}: {e.event_type}",
                "url": info["subject_url"],
                "guid": _event_public_id_for(e),
                "pub_date": e.public_at,
                "creator": e.source.attribution_text or e.source.name if e.source else "the platform",
                "categories": [e.event_type, e.subject_type],
                "description": f"{info['subject_name']}: {e.event_type}",
                "platform_ext": {
                    "event_type": e.event_type,
                    "subject": {
                        "public_id": info["subject_public_id"],
                        "name": info["subject_name"],
                        "url": info["subject_url"],
                    },
                    "provenance": [],
                    "licence_summary": build_licence_summary(
                        [r] if (r := event_licence_row(e)) is not None else []
                    ),
                    "data_as_of": build_meta(lag_days=_lag_days_for_subject(e.subject_type))["data_as_of"],
                },
            }
        )
    return _render_feed(format, resource="Events", kind="proposal", self_path=str(request.url), items=items)


def _event_public_id_for(event: Event) -> str:
    return public_id("evt", event.id)


def _render_feed(
    format: str, *, resource: str, kind: Any, self_path: str, items: list[dict[str, Any]]
) -> Response:
    if format not in ("rss", "json"):
        raise validation_error("format", "format must be rss or json", self_path)
    if format == "rss":
        xml = render_rss(resource=resource, kind=kind, self_url=self_path, items=items)
        return Response(content=xml, media_type="application/rss+xml")
    body = render_json_feed(resource=resource, kind=kind, self_url=self_path, items=items)
    return JSONResponse(content=body, media_type="application/feed+json")


def _int_param(request: Request, name: str) -> int | None:
    v = request.query_params.get(name)
    return int(v) if v is not None else None


def _parse_bbox(value: str, instance: str) -> tuple[float, float, float, float]:
    parts = value.split(",")
    if len(parts) != 4:
        raise validation_error("bbox", "bbox must be min_lon,min_lat,max_lon,max_lat", instance)
    a, b, c, d = (float(p) for p in parts)
    return (a, b, c, d)
