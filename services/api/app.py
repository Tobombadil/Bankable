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
import hmac
import os
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
from starlette.middleware.gzip import GZipMiddleware

from services.api.auth import AuthContext, get_auth_context
from services.api.build_info import build_info, data_as_of
from services.api.common import API_HOST, WEB_HOST, ensure_aware, new_request_id, utcnow
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
    location_redactions,
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
    location_exact_permitted,
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
from services.ingest.lag import ISO_CHANGE_EVENT_LAG_DAYS, RECORD_LAG_DAYS

app = FastAPI(
    title="Platform API",
    version="1.0.0-draft",
    description="Public tier only (Sprint 2 backend brief). See api/openapi.yaml for the full contract.",
)
app.add_exception_handler(ProblemError, problem_exception_handler)
# Response compression (2026-09-19, line layer): a national `GET /v1/assets/geo` over the 3,000-line
# synthetic pipeline fixture is ~1.3 MB of JSON and ~200 KB gzipped, because a GeoJSON feature list
# repeats keys and provenance quartets that compress ~6:1. Every response over 1 KB is compressed
# when the client accepts gzip; smaller responses and clients that do not are untouched.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Pro/API tier and alerts (Sprint 2 backend brief "Pro tier and alerts"): session and API-key
# auth, saved searches, alerts, keys, webhooks, the private saved-search feed, and the interim
# admin entitlement-grant endpoint. Kept in its own module (services/api/pro.py) per that task's
# "extend, don't rewrite" instruction for this file — one import and one include_router call.
from services.api.privacy_routes import router as privacy_router  # noqa: E402
from services.api.pro import router as pro_router  # noqa: E402 - after `app` exists, by design
from services.api.unsubscribe_routes import router as unsubscribe_router  # noqa: E402

# Mounted before pro_router on purpose: Starlette matches routes in registration order and
# pro.py's `GET /v1/alerts/{alert_id}` would otherwise swallow `/v1/alerts/unsubscribe`
# (US-502 AC3, US-908; found while building the unsubscribe route).
app.include_router(unsubscribe_router)
# Same ordering reason: `POST /v1/privacy/requests` must not fall into a Pro path (US-910).
app.include_router(privacy_router)
app.include_router(pro_router)

# Sprint 3, first wave (docs/00-PLAN.md "Sprint 3 kickoff" item 1 and 2): the Attio CRM adapter's
# routes (inbound webhook, US-403 lead hand-off), the Stripe billing routes (checkout, portal,
# inbound webhook, admin subscription mirror) and the password login/registration surface. Each
# lives in its own module behind the ports in services/sor; this file only mounts them.
from services.api.auth_routes import router as auth_router  # noqa: E402
from services.billing.router import router as billing_router  # noqa: E402
from services.crm.router import router as crm_router  # noqa: E402

app.include_router(auth_router)
app.include_router(crm_router)
app.include_router(billing_router)

# Sprint 3 item 3, the admin panel (docs/20 §8; docs/10 US-901 to US-910), one module per nav
# group so each was built and verified on its own: sources/gate/costs/audit first.
from services.api.admin_sources import router as admin_sources_router  # noqa: E402

app.include_router(admin_sources_router)
from services.api.admin_posts import router as admin_posts_router  # noqa: E402

app.include_router(admin_posts_router)
from services.api.admin_people import router as admin_people_router  # noqa: E402

app.include_router(admin_people_router)
from services.api.admin_records import router as admin_records_router  # noqa: E402

app.include_router(admin_records_router)

# Its identifier-free interaction measurement (docs/00-PLAN.md decision 2026-09-14; docs/21 §3.21).
from services.api.ui_events import router as ui_events_router  # noqa: E402

app.include_router(ui_events_router)

# Assets (ADR 0008, 2026-09-18): registry-sourced infrastructure with identity, generalising the
# 2026-09-14 built-infrastructure context layer. `GET /v1/context/plants/geo` (the old context
# layer's route) is mounted from here too, as an alias forcing `asset_type=power_plant`
# (services/api/assets.py's own module docstring) -- services/api/context_routes.py is kept only
# as a `TECHNOLOGY_VOCAB` re-export for web/test_map_layers.py, never mounted.
from services.api.assets import organization_asset_totals, organization_hierarchy  # noqa: E402
from services.api.assets import router as assets_router  # noqa: E402
from services.api.regions import router as regions_router  # noqa: E402

app.include_router(assets_router)
app.include_router(regions_router)


@app.middleware("http")
async def standard_headers(request: Request, call_next: Any) -> Response:
    """`X-Request-Id` on every response, plus real per-tier rate-limit headers (docs/23 §1, §6).

    Sprint 2 shipped static placeholder values here ("rate-limit headers (static values for
    now)"); this sprint (Pro tier and alerts, task item 2: "real per-tier token buckets") replaces
    them for anonymous traffic — the public tier per docs/23 §6's "60/hour, per IP" row — with a
    real count through `services.api.ratelimit.default_limiter`. A request carrying a session
    cookie or `Authorization` header is left to the route itself: `services/api/pro.py`'s routes
    already compute and set real per-caller headers via `_rate_limit_headers` (keyed by API key or
    session, tier from `AuthContext.entitlement`), which this middleware never overwrites
    (`setdefault` only). **Known gap**, not silently dropped: a Pro/API-entitled credential calling
    one of `services/api/app.py`'s original Sprint 2 public-tier routes directly (rather than a
    `services/api/pro.py` route) is not yet metered by this middleware, since crediting it to the
    anonymous per-IP bucket would double-count against unrelated anonymous traffic sharing that
    IP, and this sprint does not thread `AuthContext` resolution into the middleware layer itself.
    Recorded in services/README.md as a follow-up: unify both under one rate-limiting dependency.
    """
    from services.api.ratelimit import TIER_LIMITS, default_limiter, policy_header

    is_credentialed = bool(request.headers.get("authorization")) or bool(request.cookies.get("session"))
    # The public site calls this API server-side for every visitor from one address, so without a
    # service identity the whole site would share one anonymous bucket (live probe 2026-09-18: a
    # sitemap render plus a few map pans returned 429). A matching `X-Internal-Token` marks the
    # request as the site's own; the site is then responsible for per-visitor limits.
    internal_token = os.environ.get("API_INTERNAL_TOKEN")
    is_internal = bool(internal_token) and hmac.compare_digest(
        request.headers.get("x-internal-token", ""), internal_token or ""
    )
    if not is_credentialed and not is_internal:
        client_ip = request.client.host if request.client else "unknown"
        result = default_limiter.check(f"public:{client_ip}", limit=TIER_LIMITS["public"])
        if not result.allowed:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=429,
                media_type="application/problem+json",
                headers={
                    "Retry-After": str(result.reset_seconds),
                    "RateLimit-Limit": str(result.limit),
                    "RateLimit-Remaining": "0",
                    "RateLimit-Reset": str(result.reset_seconds),
                    "RateLimit-Policy": policy_header("public", result),
                    "X-Request-Id": new_request_id(),
                },
                content={
                    "type": f"{API_HOST}/errors/rate_limited",
                    "title": "Rate limit exceeded",
                    "status": 429,
                    "code": "rate_limited",
                    "detail": f"More than {result.limit} requests in the current window.",
                    "request_id": new_request_id(),
                    "instance": request.url.path,
                },
            )
        response: Response = await call_next(request)
        response.headers.setdefault("RateLimit-Limit", str(result.limit))
        response.headers.setdefault("RateLimit-Remaining", str(result.remaining))
        response.headers.setdefault("RateLimit-Reset", str(result.reset_seconds))
        response.headers.setdefault("RateLimit-Policy", policy_header("public", result))
    else:
        response = await call_next(request)
    response.headers["X-Request-Id"] = new_request_id()
    if request.url.path.startswith("/v1/") and request.method == "GET":
        # A response produced for a credential (valid or not) is never shareable: `/v1/me`, live
        # Pro rows and saved searches must not land in a CDN or proxy cache (API audit 2026-09-18,
        # finding S1; docs/23 §1 "Pro/API responses are private, no-store").
        credentialed = bool(request.headers.get("authorization")) or bool(request.cookies.get("session"))
        response.headers["Cache-Control"] = "private, no-store" if credentialed else "public, max-age=300"
        response.headers["Vary"] = "Authorization, Cookie"
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
    "county_fips",
    "placement",
}
PROPOSAL_SORT_ALLOWLIST = {"last_changed", "first_seen", "capacity_mw", "name_canonical"}

#: ADR 0008, docs/21 §3.7: the three region-grade precisions a `placement=region` filter expands
#: to, and the `none` grade's own precision value. Mirrors `services/api/geo.py::REGION_PRECISIONS`
#: (not imported from there to avoid this module depending on `geo.py` for a plain tuple it only
#: needs for one `IN` clause; both are asserted equal in `services/api/test_placement.py`).
PLACEMENT_REGION_PRECISIONS = ("county_centroid", "state_centroid", "country_centroid")


def _apply_placement_filter(
    stmt: sa.Select[Any], request: Request, *, default: list[str] | None
) -> sa.Select[Any]:
    """Applied separately from `_apply_proposal_filters` (docstring there) because it must *not*
    run inside `_proposal_geo_totals`: `placement` controls which grades are drawn as map
    features, never `meta.unplaced_count`/`totals.records`/the lifecycle and technology counts,
    which always cover every visible record matching every other filter (docs/23 §3.1, ADR 0008).
    `default` is `None` on `GET /v1/proposals` (every grade returned unless the caller asks
    otherwise) and `["exact", "region"]` on `GET /v1/proposals/geo` (docs/23 §3.1's stated
    default for that endpoint)."""
    v = request.query_params.get("placement")
    grades = csv_param(v) if v else default
    if grades is None:
        return stmt
    unknown = [g for g in grades if g not in ("exact", "region", "none")]
    if unknown:
        raise validation_error(
            "placement", f"unknown placement value(s): {', '.join(unknown)}", request.url.path
        )
    # Placement is judged on the grade a row is *served* at, not the one it is stored at
    # (restricted-precision rule, docs/04 D-9): an `exact` row whose licence forbids raw
    # publication is a `region` row on every non-admin surface (`services/api/geo.py::
    # effective_placement`), so `placement=exact` must not select it and `placement=region`
    # must -- `location_exact_permitted` is the SQL twin of that rule (services/api/visibility.py).
    clauses: list[sa.ColumnElement[bool]] = []
    if "exact" in grades:
        clauses.append(sa.and_(Location.precision == "exact", location_exact_permitted()))
    if "region" in grades:
        clauses.append(Location.precision.in_(PLACEMENT_REGION_PRECISIONS))
        clauses.append(sa.and_(Location.precision == "exact", sa.not_(location_exact_permitted())))
    if "none" in grades:
        clauses.append(Location.precision == "unknown")
    loc_subquery = select(Location.id).where(sa.or_(*clauses))
    return stmt.where(Proposal.location_id.in_(loc_subquery))


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
    if v := qp.get("county_fips"):
        # A subquery on `Proposal.location_id`, not a `.join(Location, ...)`, because this
        # function runs both before and after `Location` is already joined at some call sites
        # (`_proposal_geo_plottable_query` inner-joins it, `_proposal_geo_totals` outer-joins it)
        # -- a second join to the same table there would be invalid SQL, and a subquery is correct
        # regardless of what the caller already joined.
        loc_subquery = select(Location.id).where(Location.county_fips.in_(csv_param(v)))
        stmt = stmt.where(Proposal.location_id.in_(loc_subquery))
    if v := qp.get("q"):
        # Substring match over the three things a user actually types (web/templates/base.html
        # promises "name, sponsor, queue ID"): the canonical name, the sponsor organisation's
        # canonical name, and any active source record id (queue position, docket, plant-generator
        # id). Subqueries rather than joins so a proposal with several sources is not repeated.
        like = f"%{v.lower()}%"
        sponsor_ids = select(Organization.id).where(func.lower(Organization.name_canonical).like(like))
        record_hits = select(ProposalSource.proposal_id).where(
            ProposalSource.active.is_(True), func.lower(ProposalSource.source_record_id).like(like)
        )
        stmt = stmt.where(
            sa.or_(
                func.lower(Proposal.name_canonical).like(like),
                Proposal.sponsor_org_id.in_(sponsor_ids),
                Proposal.id.in_(record_hits),
            )
        )
    return stmt


def _proposal_query_with_filters(request: Request, entitlement: str = "public") -> sa.Select[tuple[Proposal]]:
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
    stmt = _apply_proposal_filters(stmt, request)
    # No default (ADR 0008): every placement grade is returned unless the caller filters
    # explicitly — a `none`-grade proposal (unknown location) belongs in list/search by design
    # (docs/21 §3.7), unlike the map, which defaults to `exact,region`.
    return _apply_placement_filter(stmt, request, default=None)


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
    Location.county_fips,
    Location.state_code,
    Location.country,
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
    dropped entirely; `location`'s own `lazy="joined"` `source` falls back to a per-record lazy
    load instead of an eager join on every row, while `location.licence` *is* joined (two columns
    of a table with one row per source) because `services/api/geo.py::effective_placement` reads
    `allows_raw_publication` for every plotted row to decide whether an `exact` point may be
    served at all (restricted-precision rule; a lazy load there would be an N+1 over the whole
    placed set); `.sources` is not loaded here at all -- `_source_licence_aggregate`
    below computes the licence summary with one SQL aggregate instead, and the individual-feature
    path lazy-loads `.sources` per record (bounded by `SPLIT_THRESHOLD`, so at most a few hundred
    small queries). Together with the visibility indexes (services/db/models.py), this is the
    services/README.md "Sprint 2 fixes" geo timing.
    """
    loc_load = contains_eager(Proposal.location)
    stmt = (
        select(Proposal)
        .join(Location, Location.id == Proposal.location_id)
        .join(Licence, Licence.id == Location.licence_id)
        .where(*proposal_visibility_filter(entitlement), Location.geom.is_not(None))
        .options(
            load_only(*_GEO_PROPOSAL_COLUMNS),
            noload(Proposal.sponsor),
            loc_load.load_only(*_GEO_LOCATION_COLUMNS),
            loc_load.lazyload(Location.source),
            loc_load.contains_eager(Location.licence).load_only(Licence.id, Licence.allows_raw_publication),
        )
    )
    stmt = _apply_proposal_filters(stmt, request)
    # Default `exact,region` (docs/23 §3.1): a caller who never passes `placement` sees exactly
    # the pre-ADR-0008 shape (points/clusters) plus the new region features, never bare `none`
    # rows -- those have no `geom` anyway and are excluded by this query's own join already.
    return _apply_placement_filter(stmt, request, default=["exact", "region"])


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
            select(func.count()).select_from(
                _proposal_query_with_filters(request, ctx.entitlement).subquery()
            )
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
    # Same default as the plottable query, so the licence summary credits exactly the sources
    # behind what is actually drawn.
    id_subquery = _apply_placement_filter(id_subquery, request, default=["exact", "region"])
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
        data,
        meta=meta,
        licence_summary=build_licence_summary(_proposal_licence_rows([prop])),
        redactions=location_redactions(prop.public_id, prop.location),
    )


@app.get("/v1/proposals/{public_id}/events")
def list_proposal_events(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"limit", "cursor", "event_type", "sort"})
    prop = db.scalar(
        select(Proposal).where(Proposal.public_id == public_id, *proposal_visibility_filter(ctx.entitlement))
    )
    if prop is None:
        raise not_found(request.url.path)
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, {"seq", "observed_at"}, "-seq")
    stmt = select(Event).where(
        Event.subject_type == "proposal",
        Event.subject_id == prop.id,
        *event_visibility_filter(ctx.entitlement),
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
    meta = build_meta("proposal", tier=ctx.entitlement)
    licence_rows = [r for e in rows if (r := event_licence_row(e)) is not None]
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


@app.get("/v1/proposals/{public_id}/sources")
def list_proposal_sources(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"include"})
    prop = db.scalar(
        select(Proposal).where(Proposal.public_id == public_id, *proposal_visibility_filter(ctx.entitlement))
    )
    if prop is None:
        raise not_found(request.url.path)
    from services.api.serialize import provenance_row

    links = [s for s in prop.sources if s.active]
    data = [provenance_row(s, s.source) for s in links]
    meta = build_meta("proposal", tier=ctx.entitlement)
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


def _opportunity_query_with_filters(
    request: Request, db: Session, entitlement: str = "public"
) -> sa.Select[tuple[Opportunity]]:
    # Same N+1 fix as `_proposal_query_with_filters` above, for `Opportunity.sources`; same
    # entitlement wiring too (services/api/visibility.py `opportunity_visibility_filter`).
    stmt = (
        select(Opportunity)
        .where(*opportunity_visibility_filter(entitlement))
        .options(selectinload(Opportunity.sources))
    )
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
        # Same contract as proposals: title, issuer organisation name, or an active source record id.
        like = f"%{v.lower()}%"
        issuer_ids = select(Organization.id).where(func.lower(Organization.name_canonical).like(like))
        record_hits = select(OpportunitySource.opportunity_id).where(
            OpportunitySource.active.is_(True), func.lower(OpportunitySource.source_record_id).like(like)
        )
        stmt = stmt.where(
            sa.or_(
                func.lower(Opportunity.title).like(like),
                Opportunity.issuer_org_id.in_(issuer_ids),
                Opportunity.id.in_(record_hits),
            )
        )
    return stmt


def _opportunity_licence_rows(items: list[Opportunity]) -> list[dict[str, Any]]:
    rows = []
    for o in items:
        for s in o.sources:
            if s.active:
                rows.append(licence_summary_row(s.source, s.source.licence, s.retrieved_at))
    return rows


@app.get("/v1/opportunities")
def list_opportunities(
    request: Request, db: Session = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)
) -> Any:
    check_allowed(request, LIST_COMMON | OPPORTUNITY_FILTERS)
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, OPPORTUNITY_SORT_ALLOWLIST, "due_at")
    stmt = _opportunity_query_with_filters(request, db, ctx.entitlement)
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
    meta = build_meta("opportunity", tier=ctx.entitlement)
    if "count" in (request.query_params.get("include") or "").split(","):
        total = db.scalar(
            select(func.count()).select_from(
                _opportunity_query_with_filters(request, db, ctx.entitlement).subquery()
            )
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
def get_opportunities_geo(
    request: Request, db: Session = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)
) -> Any:
    check_allowed(request, {"bbox", "zoom"} | OPPORTUNITY_FILTERS | {"q"})
    bbox_param = request.query_params.get("bbox")
    zoom_param = request.query_params.get("zoom")
    if not bbox_param or zoom_param is None:
        raise validation_error("bbox", "bbox and zoom are required", request.url.path)
    bbox = _parse_bbox(bbox_param, request.url.path)
    zoom = int(zoom_param)
    stmt = _opportunity_query_with_filters(request, db, ctx.entitlement)
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
    meta = build_meta("opportunity", tier=ctx.entitlement, extra={"unplaced_count": unplaced_count})
    return build_envelope(
        fc, meta=meta, licence_summary=build_licence_summary(_opportunity_licence_rows(items))
    )


@app.get("/v1/opportunities/{public_id}")
def get_opportunity(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    opp = db.scalar(
        select(Opportunity).where(
            Opportunity.public_id == public_id, *opportunity_visibility_filter(ctx.entitlement)
        )
    )
    if opp is None:
        raise not_found(request.url.path)
    data = serialize_opportunity(opp)
    meta = build_meta("opportunity", tier=ctx.entitlement)
    return build_envelope(
        data, meta=meta, licence_summary=build_licence_summary(_opportunity_licence_rows([opp]))
    )


@app.get("/v1/opportunities/{public_id}/events")
def list_opportunity_events(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"limit", "cursor", "event_type", "sort"})
    opp = db.scalar(
        select(Opportunity).where(
            Opportunity.public_id == public_id, *opportunity_visibility_filter(ctx.entitlement)
        )
    )
    if opp is None:
        raise not_found(request.url.path)
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, {"seq", "observed_at"}, "-seq")
    stmt = select(Event).where(
        Event.subject_type == "opportunity",
        Event.subject_id == opp.id,
        *event_visibility_filter(ctx.entitlement),
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
    meta = build_meta("opportunity", tier=ctx.entitlement)
    licence_rows = [r for e in rows if (r := event_licence_row(e)) is not None]
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


@app.get("/v1/opportunities/{public_id}/sources")
def list_opportunity_sources(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    check_allowed(request, {"include"})
    opp = db.scalar(
        select(Opportunity).where(
            Opportunity.public_id == public_id, *opportunity_visibility_filter(ctx.entitlement)
        )
    )
    if opp is None:
        raise not_found(request.url.path)
    from services.api.serialize import provenance_row

    links = [s for s in opp.sources if s.active]
    data = [provenance_row(s, s.source) for s in links]
    meta = build_meta("opportunity", tier=ctx.entitlement)
    rows = [licence_summary_row(s.source, s.source.licence, s.retrieved_at) for s in links]
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary(rows))


# ------------------------------------------------------------------------------------ organizations
@app.get("/v1/organizations")
def list_organizations(request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, LIST_COMMON | {"type", "country", "is_curated_issuer", "slug"})
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, {"name_canonical"}, "name_canonical")
    stmt = select(Organization).where(Organization.merged_into_id.is_(None))
    qp = request.query_params
    if v := qp.get("slug"):
        stmt = stmt.where(Organization.slug == v)
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
    # Company page (ADR 0008 §2; 2026-09-19 line layer): what the organisation owns or operates by
    # type and role, and its GLEIF parent/subsidiaries where the data lane has set `parent_org_id`.
    data["asset_counts"] = organization_asset_totals(db, org)
    data.update(organization_hierarchy(db, org))
    data["group_asset_counts"] = (
        organization_asset_totals(db, org, include_subsidiaries=True)
        if data["subsidiary_count"]
        else data["asset_counts"]
    )
    meta = build_meta(lag_days=0)
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary([]))


@app.get("/v1/organizations/{public_id}/proposals")
def list_organization_proposals(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
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
        .where(Proposal.sponsor_org_id == org.id, *proposal_visibility_filter(ctx.entitlement))
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
    meta = build_meta("proposal", tier=ctx.entitlement)
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_proposal_licence_rows(rows)),
        page=build_page(next_cursor, None, has_more),
    )


@app.get("/v1/organizations/{public_id}/opportunities")
def list_organization_opportunities(
    public_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
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
            *opportunity_visibility_filter(ctx.entitlement),
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
    meta = build_meta("opportunity", tier=ctx.entitlement)
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_opportunity_licence_rows(rows)),
        page=build_page(next_cursor, None, has_more),
    )


# ------------------------------------------------------------------------------------------ events
@app.get("/v1/events")
def list_events(
    request: Request, db: Session = Depends(get_db), ctx: AuthContext = Depends(get_auth_context)
) -> Any:
    check_allowed(request, LIST_COMMON | {"subject_type", "subject_id", "event_type", "source_id", "since"})
    limit = clamp_limit(_int_param(request, "limit"))
    field, ascending = _sort_spec(request, {"seq", "observed_at"}, "-seq")
    stmt = select(Event).where(*event_visibility_filter(ctx.entitlement))
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
    # `meta.lag_days` is a completeness claim about the feed, not about the rows that happened to
    # land on this page: on the public tier the events feed is only complete as of
    # `ISO_CHANGE_EVENT_LAG_DAYS` ago, because an ISO queue change event from yesterday exists and
    # is withheld. Reporting the maximum over `rows` would answer `0` for a page with no ISO event
    # on it and tell the caller the feed was current when it is not.
    lag_days = 0 if ctx.entitlement != "public" else ISO_CHANGE_EVENT_LAG_DAYS
    meta = build_meta(lag_days=lag_days, tier=ctx.entitlement)
    licence_rows = [r for e in rows if (r := event_licence_row(e)) is not None]
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


def _event_lag_days(event: Event) -> int:
    """The delay this one event actually carries, read back from its own stored columns.

    Since the paywall became a matter of shape rather than time (owner, 2026-09-19) the delay is a
    per-source property -- the eight ISO queue registers withhold their change events, everything
    else publishes live -- so there is no per-`subject_type` constant left to look up. Both
    columns were written together by `services/ingest/lag.py::change_event_public_at`, so their
    difference *is* the lag that was applied, including any per-event-type override an operator
    set; nothing has to re-derive it from the source row. An event missing either column is not
    visible on the public tier at all (`services/api/visibility.py` requires `public_at`), so the
    fallback is the conservative one rather than `0`."""
    if event.public_at is None or event.published_at is None:
        return ISO_CHANGE_EVENT_LAG_DAYS
    return max(0, (ensure_aware(event.public_at) - ensure_aware(event.published_at)).days)


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
def get_event(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> Any:
    event_uuid = _event_uuid_from_public_id(event_id)
    if event_uuid is None:
        raise not_found(request.url.path)
    # The same `event_visibility_filter` the list and feed use, as a `WHERE` on the decoded id
    # (2026-09-18 audit): a hand-rolled re-statement here used to check timing and the event's own
    # licence but not the subject's visibility, so a hidden proposal's event answered 200 by id.
    ev = db.scalar(select(Event).where(Event.id == event_uuid, *event_visibility_filter(ctx.entitlement)))
    if ev is None:
        raise not_found(request.url.path)
    data = serialize_event(ev, **_subject_info(db, ev))
    lag_days = 0 if ctx.entitlement != "public" else _event_lag_days(ev)
    meta = build_meta(lag_days=lag_days, tier=ctx.entitlement)
    row = event_licence_row(ev)
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary([row] if row else []))


def _event_uuid_from_public_id(event_id: str) -> _uuid.UUID | None:
    """`evt_<crockford>` ids are synthesised from `event.id` at serialization time (docs/21 §3.10
    has no separate `public_id` column for events); reversing it by scanning candidates is not
    viable at scale, so this walks the crockford alphabet back to a uuid instead."""
    from services.ids import _CROCKFORD

    if not event_id.startswith("evt_"):
        return None
    digits = event_id[4:]
    try:
        n = 0
        for ch in digits:
            n = n * 32 + _CROCKFORD.index(ch)
        return _uuid.UUID(int=n)
    except (ValueError, OverflowError):
        return None


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
        # Records carry no delay on any tier (owner, 2026-09-19), so the public materialised view
        # is current: `data_as_of == live_as_of`. `lag_days_default` keeps its shape -- the public
        # site reads it for the footer and `web/` would break on a missing key -- and gains
        # `iso_change_events`, the one delay that survives.
        "data_as_of": (now - dt.timedelta(days=RECORD_LAG_DAYS)).isoformat().replace("+00:00", "Z"),
        "live_as_of": now.isoformat().replace("+00:00", "Z"),
        "lag_days_default": {
            "supply": RECORD_LAG_DAYS,
            "opportunities": RECORD_LAG_DAYS,
            "iso_change_events": ISO_CHANGE_EVENT_LAG_DAYS,
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
        # Which build is answering, and how fresh the rows it is serving are (services/api/
        # build_info.py): two different questions that both get asked as "am I seeing the latest
        # version?". `data_as_of` is the newest fetch from a source, not the publish lag above.
        "build": build_info(),
        "source_data_as_of": data_as_of(db),
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
                    "data_as_of": build_meta(lag_days=_event_lag_days(e))["data_as_of"],
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
