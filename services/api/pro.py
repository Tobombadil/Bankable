"""Pro and API tier operations (Sprint 2 backend brief "Pro tier and alerts"): `GET /v1/me` and
`/v1/account`, saved searches, alerts, API keys, webhooks, the private saved-search feed, and the
interim admin entitlement-grant endpoint. Every operation here is `x-status: live` in
`api/openapi.yaml`; see `services/README.md` "Pro tier and alerts" for the full endpoint list and
the open decisions (no billing/CRM integration yet — Sprint 3 — so entitlements are admin-set).

Mounted onto `services.api.app.app` with one `include_router` call (`services/api/app.py`); kept
in its own module rather than appended to `app.py`'s ~1,300 lines per the task's "extend, don't
rewrite" instruction for that file.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.alerts.feed import generate_rss_token, matching_items_for_feed
from services.alerts.webhooks import create_test_delivery, replay_from_seq
from services.api.audit import record_audit_event
from services.api.auth import (
    AuthContext,
    generate_api_key,
    get_auth_context,
    require_admin,
    require_authenticated,
    require_entitlement,
    require_session_only,
)
from services.api.common import utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, unknown_parameter, validation_error
from services.api.feeds import render_json_feed, render_rss
from services.api.params import check_allowed, csv_param
from services.api.ratelimit import plan_quota
from services.api.records import (
    OPPORTUNITY_FILTERS,
    PROPOSAL_FILTERS,
    _opportunity_query_with_filters,
    _proposal_query_with_filters,
)
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    serialize_account,
    serialize_alert,
    serialize_api_key,
    serialize_saved_search,
    serialize_user,
    serialize_webhook_delivery,
    serialize_webhook_endpoint,
)
from services.billing.router import subscriptions_for_account
from services.db.models import (
    ACCOUNT_ENTITLEMENTS,
    Account,
    Alert,
    ApiKey,
    Organization,
    SavedSearch,
    UiEvent,
    User,
    WebhookDelivery,
    WebhookEndpoint,
)
from services.ids import public_id

router = APIRouter()

#: `GET /v1/me` → `api_licence.current_version` (US-704 AC2); `POST /v1/keys` must be called with
#: exactly this value. One constant, not read from `api/openapi.yaml`'s `info.license`, to avoid a
#: YAML parse at import time for a value that only changes with a deliberate licence revision.
API_LICENCE_VERSION = "api-licence-1.0"
API_LICENCE_URL = "https://infraque.com/legal/api-licence"
SAVED_SEARCH_QUOTA = 25
MAX_API_KEYS_PER_USER = 5
MAX_WEBHOOKS_PER_ACCOUNT = 10


def _rate_limit_headers(request: Request, ctx: AuthContext) -> dict[str, str]:
    from services.api.ratelimit import TIER_LIMITS, default_limiter, policy_header

    tier = ctx.entitlement if ctx.entitlement in TIER_LIMITS else "public"
    key = ctx.api_key.public_id if ctx.api_key else (str(ctx.user.id) if ctx.user else "anon")
    result = default_limiter.check(f"{tier}:{key}", limit=TIER_LIMITS[tier])
    if not result.allowed:
        raise ProblemError(
            "rate_limited",
            "Rate limit exceeded",
            detail=f"More than {result.limit} requests in the current window.",
            headers={"Retry-After": str(result.reset_seconds)},
        )
    return {
        "RateLimit-Limit": str(result.limit),
        "RateLimit-Remaining": str(result.remaining),
        "RateLimit-Reset": str(result.reset_seconds),
        "RateLimit-Policy": policy_header(tier, result),
    }


# --------------------------------------------------------------------------------------- /v1/me
@router.get("/v1/me")
def get_me(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_authenticated())],
) -> Any:
    rl = _rate_limit_headers(request, ctx)
    for k, v in rl.items():
        response.headers[k] = v
    if ctx.account is None:
        raise not_found(request.url.path, "No account context for this credential.")
    # A key-only caller has no `ctx.user` of its own; every key still names the user who created
    # it (`ApiKey.created_by_user_id`, docs/21 §3.17), so `/v1/me` renders as that user rather
    # than 404ing on a perfectly valid credential.
    me_user = (
        ctx.user
        if ctx.user is not None
        else db.get(User, ctx.api_key.created_by_user_id)
        if ctx.api_key
        else None
    )
    if me_user is None:
        raise not_found(request.url.path, "No user context for this credential.")
    account = ctx.account
    # The export and bulk figures are the ones `services/api/exports.py` and `bulk.py` enforce
    # (`ratelimit.PLAN_QUOTAS`, lane E6b); before those routes existed they were `None` here. Bulk
    # is a key scope, so a session reads `None` for it whatever the plan.
    quota = plan_quota(ctx.entitlement)
    data: dict[str, Any] = {
        "user": serialize_user(me_user, account_public_id=account.public_id),
        "account": serialize_account(account),
        "tier": ctx.entitlement,
        "limits": {
            "read_per_hour": int(rl["RateLimit-Limit"]),
            "search_per_hour": int(rl["RateLimit-Limit"]),
            "writes_per_hour": int(rl["RateLimit-Limit"]),
            "bulk_requests_per_hour": quota.bulk_requests_per_hour if ctx.api_key is not None else None,
            "exports_per_day": quota.exports_per_day,
            "export_rows_max": quota.export_rows_max,
            "daily_cap": None,
        },
        "saved_search_quota": {
            "limit": SAVED_SEARCH_QUOTA,
            "used": db.scalar(
                select(func.count()).select_from(SavedSearch).where(SavedSearch.user_id == me_user.id)
            )
            or 0,
        },
        "api_licence": {"current_version": API_LICENCE_VERSION, "url": API_LICENCE_URL},
    }
    if ctx.api_key is not None:
        data["scopes"] = sorted(ctx.scopes)
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier=ctx.entitlement), licence_summary=build_licence_summary([])
    )


@router.get("/v1/account")
def get_account(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    for k, v in _rate_limit_headers(request, ctx).items():
        response.headers[k] = v
    if ctx.account is None:
        raise not_found(request.url.path)
    org = db.get(Organization, ctx.account.organization_id) if ctx.account.organization_id else None
    # Sprint 3: the Stripe mirror rows for this account (services/billing/router.py) — an empty
    # list until the first webhook lands, never a vendor call on the request path (ADR 0006).
    data = {
        "account": serialize_account(ctx.account, organization=org),
        "subscriptions": subscriptions_for_account(db, ctx.account),
    }
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier=ctx.entitlement), licence_summary=build_licence_summary([])
    )


# -------------------------------------------------------------------------------- saved searches
SAVED_SEARCH_ENTITIES = ("proposal", "opportunity", "event", "match")
#: Page and ordering parameters: they shape a response, not the set a saved search or webhook
#: watches, so a stored `query` may not name them (the same rule an export's `query` follows).
_PAGE_KEYS = frozenset({"limit", "cursor", "include", "sort"})


def saved_search_query_keys(entity: str) -> frozenset[str]:
    """The keys a saved search's or webhook's `query` may name for `entity`: exactly the filters
    that entity's list endpoint applies (docs/23 §9.1 "a webhook is a saved search with a URL"),
    which `services/alerts/matching.py` implements in full (`tests/test_saved_search_parity.py`).
    `q` is one on proposals and opportunities; `GET /v1/events` allowlists `q` but applies no text
    filter, so an event query may not name it -- storing it would be the silently ignored filter
    this check exists to refuse."""
    from services.api.resource_queries import EVENT_FILTERS

    if entity == "proposal":
        return frozenset(PROPOSAL_FILTERS | {"q"})
    if entity == "opportunity":
        return frozenset(OPPORTUNITY_FILTERS | {"q"})
    if entity == "event":
        return frozenset(EVENT_FILTERS)
    # Deferred: `services.api.matches` imports this module (`_rate_limit_headers`).
    from services.api.matches import LIST_MATCHES_PARAMS

    return frozenset(LIST_MATCHES_PARAMS - _PAGE_KEYS)


def validate_saved_search_query(db: Session, entity: str, query: dict[str, Any], instance: str) -> None:
    """A stored `query` (saved search create/update, webhook create) is held to the list endpoint's
    rules: a key the entity's list does not filter on is a `400 unknown_parameter` naming it, and a
    value the list would refuse (an unknown `placement`, a non-numeric capacity bound, a malformed
    `due_at[from]`, ...) is the list's own `400 validation_error`, found by building -- not running
    -- the list's statement over the stored query. Before 2026-09-27 any dict was stored and the
    matcher ignored what it did not implement, so a search for "storage over 500 MW in US-TX"
    alerted on every storage proposal. Rows already stored are not re-validated on read."""
    from services.api.resource_queries import check_query_values, event_query_with_filters, synthetic_request

    check_query_values(query, instance)
    allowed = saved_search_query_keys(entity)
    for key in query:
        if key not in allowed:
            raise unknown_parameter(key, instance)
    request = synthetic_request(query, path=instance)
    if entity == "proposal":
        _proposal_query_with_filters(request)
    elif entity == "opportunity":
        _opportunity_query_with_filters(request, db)
    elif entity == "event":
        event_query_with_filters(request, db, "public")


def _query_hash(query: dict[str, Any]) -> str:
    import json

    return hashlib.sha256(json.dumps(query, sort_keys=True, default=str).encode()).hexdigest()


@router.get("/v1/saved-searches")
def list_saved_searches(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    check_allowed(request, {"limit", "cursor"})
    for k, v in _rate_limit_headers(request, ctx).items():
        response.headers[k] = v
    if ctx.account is None:
        raise not_found(request.url.path)
    stmt = select(SavedSearch).where(SavedSearch.account_id == ctx.account.id)
    rows = list(db.scalars(stmt.order_by(SavedSearch.created_at.desc())).all())
    data = [serialize_saved_search(s) for s in rows]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
        page=build_page(None, None, False),
    )


@router.post("/v1/saved-searches", status_code=201)
def create_saved_search(
    request: Request,
    response: Response,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    for k, v in _rate_limit_headers(request, ctx).items():
        response.headers[k] = v
    if ctx.user is None or ctx.account is None:
        raise not_found(request.url.path)
    name = body.get("name")
    entity = body.get("entity")
    query = body.get("query")
    if not name or entity not in SAVED_SEARCH_ENTITIES or not isinstance(query, dict):
        raise validation_error("name", "name, entity and query are required", request.url.path)
    validate_saved_search_query(db, entity, query, request.url.path)
    existing = db.scalar(
        select(func.count()).select_from(SavedSearch).where(SavedSearch.user_id == ctx.user.id)
    )
    if (existing or 0) >= SAVED_SEARCH_QUOTA:
        raise ProblemError(
            "forbidden_tier", "Saved search quota exceeded", detail=f"Limit is {SAVED_SEARCH_QUOTA}."
        )
    channels = body.get("channels") or ["email"]
    search = SavedSearch(
        public_id="",
        user_id=ctx.user.id,
        account_id=ctx.account.id,
        name=name,
        entity=entity,
        query=query,
        query_hash=_query_hash(query),
        delivery_mode=body.get("delivery_mode", "daily"),
        channels=channels,
        rss_token=generate_rss_token() if "rss" in channels else None,
    )
    db.add(search)
    db.flush()
    search.public_id = public_id("ss", search.id)
    db.flush()
    # docs/00-PLAN.md 2026-09-14 "ships with a measurement": a saved search is the product's
    # "alert" (services/db/models.py UI_EVENT_NAMES `alert.created`), identifier-free by design.
    db.add(UiEvent(name="alert.created", props={}))
    return build_envelope(
        serialize_saved_search(search),
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
    )


def _get_owned_saved_search(db: Session, ctx: AuthContext, saved_search_id: str, path: str) -> SavedSearch:
    search = db.scalar(select(SavedSearch).where(SavedSearch.public_id == saved_search_id))
    if search is None or ctx.account is None or search.account_id != ctx.account.id:
        raise not_found(path)
    return search


@router.get("/v1/saved-searches/{saved_search_id}")
def get_saved_search(
    saved_search_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    search = _get_owned_saved_search(db, ctx, saved_search_id, request.url.path)
    return build_envelope(
        serialize_saved_search(search),
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
    )


@router.patch("/v1/saved-searches/{saved_search_id}")
def update_saved_search(
    saved_search_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    search = _get_owned_saved_search(db, ctx, saved_search_id, request.url.path)
    if "name" in body:
        search.name = body["name"]
    if "query" in body:
        if not isinstance(body["query"], dict):
            raise validation_error("query", "query must be an object", request.url.path)
        validate_saved_search_query(db, search.entity, body["query"], request.url.path)
        search.query = body["query"]
        search.query_hash = _query_hash(body["query"])
    if "delivery_mode" in body:
        search.delivery_mode = body["delivery_mode"]
    if "channels" in body:
        search.channels = body["channels"]
        if "rss" in search.channels and not search.rss_token:
            search.rss_token = generate_rss_token()
        if "rss" not in search.channels:
            search.rss_token = None
    if "status" in body:
        search.status = body["status"]
    db.flush()
    return build_envelope(
        serialize_saved_search(search),
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
    )


@router.delete("/v1/saved-searches/{saved_search_id}", status_code=204)
def delete_saved_search(
    saved_search_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Response:
    search = _get_owned_saved_search(db, ctx, saved_search_id, request.url.path)
    db.delete(search)
    db.flush()
    return Response(status_code=204)


@router.post("/v1/saved-searches/{saved_search_id}/preview")
def preview_saved_search(
    saved_search_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    search = _get_owned_saved_search(db, ctx, saved_search_id, request.url.path)
    if ctx.account is None:
        raise not_found(request.url.path)
    items = matching_items_for_feed(db, search, ctx.account, limit=50)
    return build_list_envelope(
        items,
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
        page=build_page(None, None, False),
    )


# ------------------------------------------------------------------------------------- alerts
@router.get("/v1/alerts")
def list_alerts(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    check_allowed(request, {"limit", "cursor", "saved_search_id", "status", "sent_at[from]", "sent_at[to]"})
    if ctx.user is None:
        raise not_found(request.url.path)
    stmt = select(Alert).where(Alert.user_id == ctx.user.id)
    qp = request.query_params
    if v := qp.get("status"):
        stmt = stmt.where(Alert.status.in_(csv_param(v)))
    rows = list(db.scalars(stmt.order_by(Alert.created_at.desc())).all())
    data = [serialize_alert(a) for a in rows]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
        page=build_page(None, None, False),
    )


@router.get("/v1/alerts/{alert_id}")
def get_alert(
    alert_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("pro"))],
) -> Any:
    alert = db.scalar(select(Alert).where(Alert.public_id == alert_id))
    if alert is None or ctx.user is None or alert.user_id != ctx.user.id:
        raise not_found(request.url.path)
    return build_envelope(
        serialize_alert(alert, event_ids=[]),
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
    )


# --------------------------------------------------------------------------------------- keys
@router.get("/v1/keys")
def list_keys(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_entitlement("api"))],
) -> Any:
    if ctx.account is None:
        raise not_found(request.url.path)
    rows = list(db.scalars(select(ApiKey).where(ApiKey.account_id == ctx.account.id)).all())
    data = [
        serialize_api_key(
            k, account_public_id=ctx.account.public_id, created_by_public_id=_created_by_public_id(db, k)
        )
        for k in rows
    ]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
        page=build_page(None, None, False),
    )


def _created_by_public_id(db: Session, key: ApiKey) -> str:
    user = db.get(User, key.created_by_user_id)
    return user.public_id if user else ""


@router.post("/v1/keys", status_code=201)
def create_key(
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_session_only("api"))],
) -> Any:
    if ctx.user is None or ctx.account is None:
        raise not_found(request.url.path)
    name = body.get("name")
    if not name:
        raise validation_error("name", "name is required", request.url.path)
    if body.get("licence_accepted_version") != API_LICENCE_VERSION:
        raise validation_error(
            "licence_accepted_version",
            f"Must equal the current API licence version ({API_LICENCE_VERSION!r}).",
            request.url.path,
        )
    existing = db.scalar(
        select(func.count())
        .select_from(ApiKey)
        .where(ApiKey.account_id == ctx.account.id, ApiKey.revoked_at.is_(None))
    )
    if (existing or 0) >= MAX_API_KEYS_PER_USER:
        raise ProblemError(
            "forbidden_tier", "API key limit reached", detail=f"Limit is {MAX_API_KEYS_PER_USER}."
        )
    requested_scopes = body.get("scopes") or ["read:public"]
    if "admin:*" in requested_scopes:
        raise validation_error("scopes", "admin:* may never be requested by a customer key", request.url.path)
    prefix = body.get("prefix", "bk_live")
    secret, key_hash, last4 = generate_api_key(prefix)
    tier = (
        "api"
        if ("read:bulk" in requested_scopes or "write:webhooks" in requested_scopes)
        else ("pro" if "read:live" in requested_scopes else "public")
    )
    key = ApiKey(
        public_id="",
        account_id=ctx.account.id,
        created_by_user_id=ctx.user.id,
        name=name,
        prefix=prefix,
        last4=last4,
        key_hash=key_hash,
        scopes=requested_scopes,
        tier=tier,
        licence_accepted_version=API_LICENCE_VERSION,
        licence_accepted_at=utcnow(),
    )
    db.add(key)
    db.flush()
    key.public_id = public_id("key", key.id)
    db.flush()
    record_audit_event(
        db,
        subject_type="api_key",
        subject_id=key.id,
        event_type="key_issued",
        actor=ctx.user,
        reason=f"API key {key.name!r} created",
        after={"scopes": requested_scopes, "tier": tier},
    )
    data = serialize_api_key(
        key, account_public_id=ctx.account.public_id, created_by_public_id=ctx.user.public_id
    )
    data["secret"] = secret
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier=ctx.entitlement), licence_summary=build_licence_summary([])
    )


@router.delete("/v1/keys/{key_id}", status_code=204)
def revoke_key(
    key_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_session_only("api"))],
) -> Response:
    key = db.scalar(select(ApiKey).where(ApiKey.public_id == key_id))
    if key is None or ctx.account is None or key.account_id != ctx.account.id:
        raise not_found(request.url.path)
    key.revoked_at = utcnow()
    db.flush()
    if ctx.user is not None:
        record_audit_event(
            db,
            subject_type="api_key",
            subject_id=key.id,
            event_type="key_revoked",
            actor=ctx.user,
            reason=f"API key {key.name!r} revoked",
            before={"revoked_at": None},
            after={"revoked_at": key.revoked_at.isoformat()},
        )
    return Response(status_code=204)


# ------------------------------------------------------------------------------------ webhooks
def _require_webhook_access(ctx: AuthContext) -> None:
    if ctx.user is not None:
        if ctx.entitlement != "api":
            raise ProblemError("forbidden_tier", "API entitlement required")
        return
    if ctx.api_key is not None and "write:webhooks" in ctx.scopes:
        return
    raise ProblemError("unauthenticated", "Authentication required")


@router.get("/v1/webhooks")
def list_webhooks(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    _require_webhook_access(ctx)
    if ctx.account is None:
        raise not_found(request.url.path)
    rows = list(db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.account_id == ctx.account.id)).all())
    data = [serialize_webhook_endpoint(e) for e in rows]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
        page=build_page(None, None, False),
    )


@router.post("/v1/webhooks", status_code=201)
def create_webhook(
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    _require_webhook_access(ctx)
    # A key-only caller can manage webhooks too (`write:webhooks`); attribute creation to the
    # key's own creator for the audit trail rather than requiring a user session.
    creator = ctx.user
    if creator is None and ctx.api_key is not None:
        creator = db.get(User, ctx.api_key.created_by_user_id)
    account = ctx.account
    if account is None or creator is None:
        raise not_found(request.url.path)
    url = body.get("url", "")
    types = body.get("types") or []
    if not url.startswith("https://"):
        raise validation_error("url", "url must be https://", request.url.path)
    if not types:
        raise validation_error("types", "at least one type is required", request.url.path)
    entity = body.get("entity", "event")
    if entity not in SAVED_SEARCH_ENTITIES:
        raise validation_error(
            "entity", f"entity must be one of {', '.join(SAVED_SEARCH_ENTITIES)}", request.url.path
        )
    query = body.get("query") or {}
    if not isinstance(query, dict):
        raise validation_error("query", "query must be an object", request.url.path)
    validate_saved_search_query(db, entity, query, request.url.path)
    existing = db.scalar(
        select(func.count()).select_from(WebhookEndpoint).where(WebhookEndpoint.account_id == account.id)
    )
    if (existing or 0) >= MAX_WEBHOOKS_PER_ACCOUNT:
        raise ProblemError(
            "forbidden_tier", "Webhook limit reached", detail=f"Limit is {MAX_WEBHOOKS_PER_ACCOUNT}."
        )
    # `generate_api_key` is reused only for its random-secret generation (43 base62 characters);
    # its hash return value is discarded — `WebhookEndpoint.secret` stores the secret itself, per
    # that field's docstring.
    secret, _discarded_hash, _ = generate_api_key("bk_live")
    endpoint = WebhookEndpoint(
        public_id="",
        account_id=account.id,
        created_by_user_id=creator.id,
        url=url,
        description=body.get("description"),
        types=types,
        entity=entity,
        query=query,
        secret=secret,
    )
    db.add(endpoint)
    db.flush()
    endpoint.public_id = public_id("whe", endpoint.id)
    db.flush()
    data = serialize_webhook_endpoint(endpoint)
    data["secret"] = secret
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier=ctx.entitlement), licence_summary=build_licence_summary([])
    )


def _get_owned_webhook(db: Session, ctx: AuthContext, webhook_id: str, path: str) -> WebhookEndpoint:
    endpoint = db.scalar(select(WebhookEndpoint).where(WebhookEndpoint.public_id == webhook_id))
    if endpoint is None or ctx.account is None or endpoint.account_id != ctx.account.id:
        raise not_found(path)
    return endpoint


@router.get("/v1/webhooks/{webhook_id}")
def get_webhook(
    webhook_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    _require_webhook_access(ctx)
    endpoint = _get_owned_webhook(db, ctx, webhook_id, request.url.path)
    return build_envelope(
        serialize_webhook_endpoint(endpoint),
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
    )


@router.delete("/v1/webhooks/{webhook_id}", status_code=204)
def delete_webhook(
    webhook_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Response:
    _require_webhook_access(ctx)
    endpoint = _get_owned_webhook(db, ctx, webhook_id, request.url.path)
    db.delete(endpoint)
    db.flush()
    return Response(status_code=204)


@router.post("/v1/webhooks/{webhook_id}/test", status_code=202)
def test_webhook(
    webhook_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    _require_webhook_access(ctx)
    endpoint = _get_owned_webhook(db, ctx, webhook_id, request.url.path)
    delivery = create_test_delivery(db, endpoint)
    return build_envelope(
        serialize_webhook_delivery(delivery, event_public_id=None),
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
    )


@router.post("/v1/webhooks/{webhook_id}/replay", status_code=202)
def replay_webhook(
    webhook_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    _require_webhook_access(ctx)
    endpoint = _get_owned_webhook(db, ctx, webhook_id, request.url.path)
    since_raw = request.query_params.get("since")
    if since_raw is None or not since_raw.lstrip("-").isdigit():
        raise validation_error("since", "since (event seq) is required", request.url.path)
    deliveries = replay_from_seq(db, endpoint, since_seq=int(since_raw))
    data = {"webhook_id": endpoint.public_id, "since": int(since_raw), "queued_count": len(deliveries)}
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier=ctx.entitlement), licence_summary=build_licence_summary([])
    )


@router.get("/v1/webhooks/{webhook_id}/deliveries")
def list_webhook_deliveries(
    webhook_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    _require_webhook_access(ctx)
    endpoint = _get_owned_webhook(db, ctx, webhook_id, request.url.path)
    check_allowed(request, {"limit", "cursor", "status"})
    stmt = select(WebhookDelivery).where(WebhookDelivery.webhook_endpoint_id == endpoint.id)
    if v := request.query_params.get("status"):
        stmt = stmt.where(WebhookDelivery.status.in_(csv_param(v)))
    rows = list(db.scalars(stmt.order_by(WebhookDelivery.created_at.desc())).all())
    data = [serialize_webhook_delivery(d, event_public_id=_event_public_id(db, d.event_id)) for d in rows]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
        page=build_page(None, None, False),
    )


def _event_public_id(db: Session, event_id: Any) -> str | None:
    if event_id is None:
        return None
    from services.ids import public_id as _pid

    return _pid("evt", event_id)


# -------------------------------------------------------------------------- saved-search feed
@router.get("/feeds/saved/{rss_token}")
def feed_saved_search(rss_token: str, request: Request, db: Annotated[Session, Depends(get_db)]) -> Response:
    fmt = request.query_params.get("format", "rss")
    if fmt not in ("rss", "json"):
        raise validation_error("format", "format must be rss or json", request.url.path)
    search = db.scalar(select(SavedSearch).where(SavedSearch.rss_token == rss_token))
    if search is None or search.status != "active":
        raise not_found(request.url.path)
    account = db.get(Account, search.account_id)
    if account is None:
        raise not_found(request.url.path)
    items = matching_items_for_feed(db, search, account, limit=50)
    # `render_rss`/`render_json_feed`'s `kind` only feeds the (unreached when `live=True`) public
    # lag-days lookup (`services/ingest/lag.py` `Kind` has no `event`/`match` member); any
    # placeholder kind is safe here since the live-feed title never consults it.
    feed_kind = search.entity if search.entity in ("proposal", "opportunity") else "proposal"
    if fmt == "rss":
        xml = render_rss(
            resource=f"Saved search: {search.name}",
            kind=feed_kind,  # type: ignore[arg-type]
            self_url=str(request.url),
            items=items,
            live=True,
        )
        return Response(content=xml, media_type="application/rss+xml")
    body = render_json_feed(
        resource=f"Saved search: {search.name}",
        kind=feed_kind,  # type: ignore[arg-type]
        self_url=str(request.url),
        items=items,
        live=True,
    )
    return JSONResponse(content=body, media_type="application/feed+json")


# --------------------------------------------------------------- admin: interim entitlement grant
@router.put("/admin/v1/accounts/{account_id}/entitlement")
def admin_set_account_entitlement(
    account_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    """Interim manual-grant path (task brief: "billing is Sprint 3 so entitlements are set by an
    admin-only endpoint for now"). Not a CRM/ERP adapter write like `/admin/v1/customers` (that
    path needs the system-of-record integration this sprint does not build) — this sets
    `account.entitlement` directly with `entitlement_source = "manual_grant"` and an audited
    reason, exactly the escape hatch docs/21 §3.13 already models (`sor` / `manual_grant` /
    `trial`). Superseded once billing wires the adapter (services/README.md)."""
    account = db.scalar(select(Account).where(Account.public_id == account_id))
    if account is None:
        raise not_found(request.url.path)
    entitlement = body.get("entitlement")
    reason = body.get("reason")
    if entitlement not in ACCOUNT_ENTITLEMENTS:
        raise validation_error("entitlement", f"must be one of {ACCOUNT_ENTITLEMENTS}", request.url.path)
    if not reason:
        raise validation_error(
            "reason", "reason is required for an admin entitlement change", request.url.path
        )
    before = {"entitlement": account.entitlement}
    account.entitlement = entitlement
    account.entitlement_source = "manual_grant"
    account.entitlement_checked_at = utcnow()
    account.entitlement_stale = False
    db.flush()
    if ctx.user is not None:
        record_audit_event(
            db,
            subject_type="account",
            subject_id=account.id,
            event_type="admin_edit",
            actor=ctx.user,
            reason=reason,
            before=before,
            after={"entitlement": entitlement},
        )
    return build_envelope(
        {"account": serialize_account(account)},
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


__all__ = ["router"]
