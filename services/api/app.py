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
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.gzip import GZipMiddleware

from services.api.auth import AuthContext, get_auth_context
from services.api.build_info import build_info, data_as_of
from services.api.common import API_HOST, WEB_HOST, new_request_id, utcnow
from services.api.coverage import coverage, source_vintages
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, problem_exception_handler, validation_error
from services.api.feeds import render_json_feed, render_rss
from services.api.lifecycle import vocabulary as lifecycle_vocabulary
from services.api.pagination import clamp_limit, paginate
from services.api.params import LIST_COMMON, check_allowed, csv_param, int_param, sort_spec
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    event_licence_row,
    serialize_event,
    serialize_licence,
    serialize_opportunity,
    serialize_organization,
    serialize_proposal,
    serialize_source,
)
from services.api.slippage import SLIP_BUCKET_MAX_DAYS, SLIP_GRACE_DAYS
from services.api.visibility import (
    event_public_filter,
    event_visibility_filter,
    opportunity_public_filter,
    opportunity_visibility_filter,
    proposal_public_filter,
    proposal_visibility_filter,
)
from services.db.models import (
    REUSE_CLASSES,
    Event,
    Licence,
    Opportunity,
    Organization,
    Proposal,
    Source,
)
from services.ids import public_id
from services.ingest.lag import RECORD_LAG_DAYS
from services.posture import platform_posture, posture_statement
from services.sor.ports import BillingPort
from services.sor.wiring import get_billing_port

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
from services.api.admin_intake import router as admin_intake_router  # noqa: E402

app.include_router(admin_intake_router)
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
from services.api.orgtree import org_scope, scope_from_request  # noqa: E402
from services.api.regions import router as regions_router  # noqa: E402

app.include_router(assets_router)
app.include_router(regions_router)

# Public record surface (docs/42-backend-review-2026-09-26.md, lane L5): `v1/proposals` and
# `v1/opportunities` (list, geo, detail, events, sources) plus the filter stack, moved out of this
# file into their own router. `records.py` never imports this module (it would cycle with the
# `include_router` call below), so the handful of names this file's own routes still call
# (the feeds and the organisation-scoped proposal/opportunity lists) are imported back from there.
from services.api.records import (  # noqa: E402
    OPPORTUNITY_FILTERS,
    OPPORTUNITY_SORT_ALLOWLIST,
    PROPOSAL_FILTERS,
    PROPOSAL_SORT_ALLOWLIST,
    _opportunity_licence_rows,
    _opportunity_query_with_filters,
    _opportunity_technologies_filter,
    _proposal_licence_rows,
    _proposal_query_with_filters,
)
from services.api.records import router as records_router  # noqa: E402

app.include_router(records_router)


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


# ------------------------------------------------------------------------------------ organizations
@app.get("/v1/organizations")
def list_organizations(request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, LIST_COMMON | {"type", "country", "is_curated_issuer", "slug"})
    limit = clamp_limit(int_param(request, "limit"))
    field, ascending = sort_spec(request, {"name_canonical"}, "name_canonical")
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


def _counts_for_org(
    db: Session, org: Organization, scope_org_ids: list[Any] | None = None
) -> tuple[int, int]:
    ids = scope_org_ids if scope_org_ids is not None else [org.id]
    p = db.scalar(
        select(func.count())
        .select_from(Proposal)
        .where(Proposal.sponsor_org_id.in_(ids), *proposal_public_filter())
    )
    o = db.scalar(
        select(func.count())
        .select_from(Opportunity)
        .where(Opportunity.issuer_org_id.in_(ids), *opportunity_public_filter())
    )
    return p or 0, o or 0


@app.get("/v1/organizations/{public_id}")
def get_organization(public_id: str, request: Request, db: Session = Depends(get_db)) -> Any:
    org = db.scalar(select(Organization).where(Organization.public_id == public_id))
    if org is None:
        raise not_found(request.url.path)
    group = org_scope(db, org, "all")
    p_count, o_count = _counts_for_org(db, org)
    data = serialize_organization(org, proposal_count=p_count, opportunity_count=o_count)
    # Company page (ADR 0008 §2; 2026-09-19 line layer): what the organisation owns or operates by
    # type and role, and where it sits in the ownership tree -- parent with the provenance of that
    # claim, the ancestor chain for breadcrumbs, the direct subsidiaries and the full descendant
    # count (`organization_hierarchy`).
    data["asset_counts"] = organization_asset_totals(db, org)
    data.update(organization_hierarchy(db, org))
    # `group_asset_counts` was the *direct* subsidiaries' assets until 2026-09-20 and is now the
    # whole descent (`scope=all`). The field name and shape are unchanged, and so is the number
    # wherever the tree is one level deep -- 130 of the 137 rooted trees on the 2026-09-20 load.
    # It changes for the 9 organisations that have a grandchild, where the old number was simply
    # wrong: THE SOUTHERN COMPANY reported its 2 direct subsidiaries' assets and not the 18 its
    # group holds.
    data["group_asset_counts"] = (
        organization_asset_totals(db, org, scope="all") if group.organizations > 1 else data["asset_counts"]
    )
    group_p, group_o = _counts_for_org(db, org, group.ids) if group.organizations > 1 else (p_count, o_count)
    data["group_proposal_count"] = group_p
    data["group_opportunity_count"] = group_o
    data["group_scope"] = group.as_meta()
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
        request,
        {
            "limit",
            "cursor",
            "sort",
            "kind",
            "technology",
            "lifecycle_state",
            "jurisdiction",
            "scope",
            "include_subsidiaries",
        },
    )
    org = db.scalar(select(Organization).where(Organization.public_id == public_id))
    if org is None:
        raise not_found(request.url.path)
    limit = clamp_limit(int_param(request, "limit"))
    field, ascending = sort_spec(request, PROPOSAL_SORT_ALLOWLIST, "-last_changed")
    # `scope` (2026-09-20) gives this list the same ownership-tree treatment the asset and
    # nearby-proposal lists have: a holding company sponsors nothing itself, its operating
    # companies do, so "Tallgrass's proposals" at `scope=self` is an empty page that is
    # technically correct and useless. Default stays `self` -- widening an existing list
    # endpoint's default would change what every current caller's counts mean.
    scope = scope_from_request(request)
    scope_result = org_scope(db, org, scope)
    stmt = (
        select(Proposal)
        .where(
            Proposal.sponsor_org_id.in_(scope_result.ids),
            *proposal_visibility_filter(ctx.entitlement),
        )
        .options(selectinload(Proposal.sources))
    )
    qp = request.query_params
    if v := qp.get("kind"):
        stmt = stmt.where(Proposal.kind.in_(csv_param(v)))
    if v := qp.get("lifecycle_state"):
        stmt = stmt.where(Proposal.lifecycle_state.in_(csv_param(v)))
    # `technology` and `jurisdiction` were accepted by `check_allowed` from the day this endpoint
    # landed but never applied, so `?technology=solar` returned the unfiltered list and no error
    # (found 2026-09-20). Applied here the same way `kind` and `lifecycle_state` are.
    if v := qp.get("technology"):
        stmt = stmt.where(Proposal.technology.in_(csv_param(v)))
    if v := qp.get("jurisdiction"):
        stmt = stmt.where(Proposal.jurisdiction.in_(csv_param(v)))
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
    env = build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(_proposal_licence_rows(rows)),
        page=build_page(next_cursor, None, has_more),
    )
    env["scope"] = scope_result.as_meta()
    return env


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
    limit = clamp_limit(int_param(request, "limit"))
    field, ascending = sort_spec(request, OPPORTUNITY_SORT_ALLOWLIST, "due_at")
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
    limit = clamp_limit(int_param(request, "limit"))
    field, ascending = sort_spec(request, {"seq", "observed_at"}, "-seq")
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
    # land on this page. Since 2026-09-21 it is `0` on every tier including the public one: the
    # ISO change-event delay is gone (owner; `services/ingest/lag.py`), so there is no withheld
    # event from yesterday and the events feed is complete as of now for every reader.
    meta = build_meta(lag_days=0, tier=ctx.entitlement)
    licence_rows = [r for e in rows if (r := event_licence_row(e)) is not None]
    return build_list_envelope(
        data,
        meta=meta,
        licence_summary=build_licence_summary(licence_rows),
        page=build_page(next_cursor, None, has_more),
    )


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
    # `0` on every tier: an event is public when it is published (owner, 2026-09-21).
    meta = build_meta(lag_days=0, tier=ctx.entitlement)
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
    limit = clamp_limit(int_param(request, "limit"))
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


@app.get("/v1/coverage")
def get_coverage(request: Request, db: Session = Depends(get_db)) -> Any:
    """What the register does and does not contain, measured at request time.

    Every number is a query or a read of `data/sources.yaml`, so this cannot go stale the way a
    written coverage paragraph does; only the `notes` are prose, and each carries the date it was
    written (`services/api/coverage.py`). Public on every tier and unaffected by the visibility
    predicate: which technologies have no rows and which sources are withheld are facts about our
    coverage, not rows, and answering them differently per tier would be its own dishonesty."""
    check_allowed(request, set())
    return build_envelope(
        coverage(db),
        meta=build_meta(lag_days=0),
        licence_summary=build_licence_summary([]),
    )


@app.get("/v1/lifecycle-states")
def get_lifecycle_states(request: Request, db: Session = Depends(get_db)) -> Any:
    """The published definitions of the status vocabulary, with the raw source values that map
    into each one derived from `pipeline/status_map.yaml` and the per-connector status maps at
    request time (`services/api/lifecycle.py`). The prose half carries the date it was written;
    the mapping half cannot drift from the pipeline because it is read out of the same files."""
    check_allowed(request, set())
    return build_envelope(
        lifecycle_vocabulary(),
        meta=build_meta(lag_days=0),
        licence_summary=build_licence_summary([]),
    )


@app.get("/v1/licences")
def list_licences(request: Request, db: Session = Depends(get_db)) -> Any:
    check_allowed(request, {"limit", "cursor", "reuse_class"})
    limit = clamp_limit(int_param(request, "limit"))
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
REUSE_CLASS_VALUES = list(REUSE_CLASSES)
PUBLISH_STATE_VALUES = ["pending_review", "ingest_only", "api_only", "public", "unpublished"]


def _vocab(values: list[str]) -> list[dict[str, Any]]:
    return [
        {"value": v, "label": v.replace("_", " ").title(), "sort_order": i, "active": True}
        for i, v in enumerate(values)
    ]


def _slip_bucket_vocab() -> list[dict[str, Any]]:
    labels = {
        "under_1y": "Overdue under 1 year",
        "1_to_3y": "Overdue 1-3 years",
        "over_3y": "Overdue over 3 years",
    }
    out: list[dict[str, Any]] = []
    lower = SLIP_GRACE_DAYS
    for i, (name, maximum) in enumerate(SLIP_BUCKET_MAX_DAYS):
        out.append(
            {
                "value": name,
                "label": labels[name],
                "sort_order": i,
                "active": True,
                "min_days_late": lower + 1,
                "max_days_late": maximum,
                "grace_days": SLIP_GRACE_DAYS,
            }
        )
        lower = maximum if maximum is not None else lower
    return out


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
        # Slip buckets carry the day boundaries and the grace period, because a caller that draws
        # its own "overdue" line from `proposed_online_date` and ours must be able to see where
        # ours is rather than guess it (services/api/slippage.py).
        "slip_bucket": _slip_bucket_vocab(),
    }
    meta = build_meta(lag_days=0)
    return build_envelope(data, meta=meta, licence_summary=build_licence_summary([]))


#: One read, at import, alongside the visibility predicate's own (`services/api/visibility.py`).
PLATFORM_POSTURE = platform_posture()


def _health_vintage(db: Session) -> dict[str, Any]:
    """The release bound, small enough for a health probe: one indexed read of `source`."""
    summary = source_vintages(db)
    oldest = summary["oldest"]
    return {
        "oldest": oldest["vintage"] if oldest else None,
        "oldest_label": oldest["vintage_label"] if oldest else None,
        "oldest_source_id": oldest["source_id"] if oldest else None,
        "sources_stating_a_release": summary["sources_stating_a_release"],
        "sources_stating_none": summary["sources_stating_none"],
        "sources_undetermined": summary["sources_undetermined"],
    }


@app.get("/v1/health")
def get_health(
    db: Session = Depends(get_db),
    billing_port: BillingPort = Depends(get_billing_port),
) -> Any:
    try:
        db.execute(select(1))
        database_ok = True
    except Exception:
        database_ok = False
    now = utcnow()
    data = {
        "status": "ok" if database_ok else "degraded",
        "api_version": "v1",
        # Nothing is delayed on any tier (owner, 2026-09-19 for records, 2026-09-21 for change
        # events), so the public view is current: `data_as_of == live_as_of`. `lag_days_default`
        # keeps its shape -- the public site reads it for the footer and `web/` would break on a
        # missing key -- and it is now zeros all the way down. The `iso_change_events` key went
        # with the delay it named.
        "data_as_of": (now - dt.timedelta(days=RECORD_LAG_DAYS)).isoformat().replace("+00:00", "Z"),
        "live_as_of": now.isoformat().replace("+00:00", "Z"),
        "lag_days_default": {
            "supply": RECORD_LAG_DAYS,
            "opportunities": RECORD_LAG_DAYS,
        },
        "checks": {
            "database": database_ok,
            "queue": True,
            "rate_limiter_active": True,
            "edge_cache": True,
            "backup_age_hours": None,
            "queue_age_seconds": None,
            # Whether a real payment processor is wired, so a surface that asks for money can say
            # payments are off *before* the visitor presses the button rather than after. The web
            # host is a separate deployable and cannot see the processor secret, which lives only
            # here; this is the boolean it reads instead. Configuration, never a secret.
            "billing_configured": billing_port.live,
        },
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        # Which build is answering, and how fresh the rows it is serving are (services/api/
        # build_info.py): two different questions that both get asked as "am I seeing the latest
        # version?". `data_as_of` is the newest fetch from a source, not the publish lag above.
        "build": build_info(),
        "source_data_as_of": data_as_of(db),
        # `source_data_as_of` is *our* newest fetch. `source_vintage` is what the sources
        # themselves say they released, which on the load this was added against was two months
        # older for EIA-860M and nine years older for one Energy Atlas layer. A probe that reads
        # only the fetch date and calls the data current is the mistake the pair exists to stop;
        # `oldest` is the bound, and `sources_stating_none` says how many sources cannot be
        # bounded that way at all rather than letting the fetch date pretend to (migration 0018).
        "source_vintage": _health_vintage(db),
        # The platform posture (owner, 2026-09-25; docs/26; `services/posture.py`), read-only:
        # which reuse classes the gates are admitting, and the one sentence the public pages
        # print about it. Read once at import (`PLATFORM_POSTURE` below), the same moment
        # `services/api/visibility.py` read it, so this reports the posture the predicate is
        # actually applying rather than whatever the environment says now. Configuration, never
        # a secret.
        "posture": PLATFORM_POSTURE,
        "posture_statement": posture_statement(PLATFORM_POSTURE),
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
                    "data_as_of": build_meta(lag_days=0)["data_as_of"],
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
