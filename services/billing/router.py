"""Billing HTTP routes: checkout, the customer portal, the inbound Stripe webhook, and the admin
subscription list. `router = APIRouter()` is mounted by the coordinator (`services/api/app.py`);
this module never imports or mutates `app.py` itself (CLAUDE.md "one agent per file area").
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.auth import AuthContext, require_admin, require_session_only
from services.api.common import WEB_HOST, iso
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, validation_error
from services.api.pagination import DEFAULT_LIMIT, clamp_limit, paginate
from services.api.params import check_allowed, csv_param
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
)
from services.billing.entitlement import apply_entitlement_change
from services.db.models import Account, Subscription
from services.posture import platform_posture
from services.sor.ports import (
    PLAN_TIERS,
    BillingPort,
    CheckoutRequest,
    CrmPort,
    SorError,
    SorRejected,
    SorUnavailable,
    WebhookRejected,
)
from services.sor.wiring import get_billing_port, get_crm_port

logger = logging.getLogger(__name__)

router = APIRouter()

#: Whether a *new* paid-tier purchase may be started. Computed once at import from
#: `PLATFORM_POSTURE`, the same moment `services/api/visibility.py`'s `PUBLISHABLE_REUSE_CLASSES`
#: and `services/api/app.py`'s `PLATFORM_POSTURE` constant are (docs/26-platform-posture.md §3
#: precondition (i); owner, 2026-09-26 decisions log, verbatim: "Mark inactive, then flip — keep
#: the pricing page visible with a 'paid tiers not currently offered' notice; disable checkout;
#: flip the posture once ready."). `False` only under `noncommercial`; unaffected — always
#: `True` — under `commercial`, the default, so a deployment that never sets `PLATFORM_POSTURE`
#: behaves exactly as it did before this constant existed. `open_portal` below does not read this:
#: managing a subscription already sold is not selling a new one (see its docstring).
PAID_TIERS_ACTIVE = platform_posture() != "noncommercial"


# ------------------------------------------------------------------------------------ serialization
def _serialize_subscription(row: Subscription, account: Account) -> dict[str, Any]:
    """`api/openapi.yaml` `Subscription` (docs/21 §3.14). Shared by the admin list below and
    `subscriptions_for_account`, which the coordinator wires into `GET /v1/account`'s
    `subscriptions` field (`services/api/pro.py:get_account`, currently a hardcoded `[]`)."""
    return {
        "subscription_id": row.public_id,
        "account_id": account.public_id,
        "sor_kind": row.sor_kind,
        "sor_ref": row.sor_ref,
        "plan_code": row.plan_code,
        "plan_tier": row.plan_tier,
        "status": row.status,
        "seats": row.seats,
        "current_period_start": iso(row.current_period_start),
        "current_period_end": iso(row.current_period_end),
        "cancel_at": iso(row.cancel_at),
        "mrr_amount": float(row.mrr_amount) if row.mrr_amount is not None else None,
        "currency": row.currency,
        "mirrored_at": iso(row.mirrored_at),
        "drift_flag": row.drift_flag,
    }


def subscriptions_for_account(db: Session, account: Account) -> list[dict[str, Any]]:
    """For `GET /v1/account`'s `subscriptions` field — the coordinator's wiring point, not called
    from this module's own routes."""
    rows = list(db.scalars(select(Subscription).where(Subscription.account_id == account.id)).all())
    return [_serialize_subscription(row, account) for row in rows]


# ------------------------------------------------------------------------------------- webhook
@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    port: Annotated[BillingPort, Depends(get_billing_port)],
    crm: Annotated[CrmPort, Depends(get_crm_port)],
) -> dict[str, int]:
    """Security `[]` (`api/fragments/billing.yaml`): Stripe authenticates itself via the
    `Stripe-Signature` header, verified inside `port.handle_webhook`, not via session/API-key
    auth. `security: []` on the OpenAPI operation is Stripe-facing, not "unauthenticated" in the
    platform's usual sense."""
    body = await request.body()
    try:
        changes = port.handle_webhook(body=body, headers=request.headers)
    except WebhookRejected as exc:
        raise ProblemError("unauthenticated", "Webhook signature invalid", detail=str(exc)) from exc
    except SorError as exc:
        # A downstream GET (e.g. `checkout.session.completed` fetching the subscription) failed:
        # never 5xx the webhook receiver over it (Stripe would retry indefinitely) — log and treat
        # as zero changes for this delivery; the next event for the same subscription (or Stripe's
        # own webhook retry) carries the same information again.
        logger.warning("billing webhook: adapter failed while parsing event: %s", exc, exc_info=True)
        changes = []

    applied = 0
    for change in changes:
        try:
            account = apply_entitlement_change(db, change, crm=crm)
        except Exception:  # never fail the whole webhook delivery over one bad change
            logger.exception(
                "billing webhook: failed to apply entitlement change for event %s", change.event_ref
            )
            continue
        if account is not None:
            applied += 1
    return {"received": len(changes), "applied": applied}


# ------------------------------------------------------------------------------------- checkout
@router.post("/v1/billing/checkout", status_code=201)
def create_checkout(
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_session_only("public"))],
    port: Annotated[BillingPort, Depends(get_billing_port)],
) -> Any:
    """Any signed-in user may start a checkout (`require_session_only("public")`) — the account
    does not need an existing entitlement to buy one.

    Refuses with `403 paid_tiers_inactive` while `PAID_TIERS_ACTIVE` is `False` (`noncommercial`
    posture): a new paid subscription is exactly what the owner's "mark inactive" decision stops
    this route from selling, before checking anything else about the request (a bad plan or a
    missing email is beside the point when nothing can be bought at all right now).
    `POST /v1/billing/portal` is untouched — see its docstring."""
    if not PAID_TIERS_ACTIVE:
        raise ProblemError(
            "paid_tiers_inactive",
            "Paid tiers are not currently offered",
            detail=(
                "The platform is operating under a noncommercial posture "
                "(docs/26-platform-posture.md); no new paid subscription can be started until "
                "the posture changes. An existing subscription can still be managed at "
                "POST /v1/billing/portal."
            ),
        )
    plan = body.get("plan")
    seats = body.get("seats", 1)
    if plan not in PLAN_TIERS:
        raise validation_error("plan", f"plan must be one of {PLAN_TIERS}", request.url.path)
    if not isinstance(seats, int) or isinstance(seats, bool) or seats < 1:
        raise validation_error("seats", "seats must be an integer >= 1", request.url.path)

    account = ctx.account
    user = ctx.user
    if account is None or user is None:  # pragma: no cover - require_session_only guarantees both
        raise not_found(request.url.path)

    if user.email is None:
        # Never fabricate an email for a third-party system: Stripe sends receipts and dunning
        # to the Customer's address, so a made-up one means lost payment notices, and it plants a
        # fake identity in Stripe's records (services/billing/README.md decision #7). No
        # email-verification story exists yet this sprint (services/README.md); until one does,
        # a user without an email on file simply cannot check out.
        raise ProblemError("conflict", "An email address is required before checkout")

    try:
        session = port.create_checkout(
            CheckoutRequest(
                account_public_id=account.public_id,
                customer_email=user.email,
                plan=plan,
                seats=seats,
                success_url=f"{WEB_HOST}/account?checkout=success",
                cancel_url=f"{WEB_HOST}/account?checkout=cancelled",
                billing_ref=account.billing_ref,
            )
        )
    except SorUnavailable as exc:
        logger.warning("billing checkout: provider unavailable for account %s: %s", account.public_id, exc)
        raise ProblemError(
            "sor_unavailable",
            "Billing provider unavailable",
            detail="The billing provider could not be reached; try again shortly.",
        ) from exc
    except SorRejected as exc:
        logger.warning(
            "billing checkout: provider rejected request for account %s: %s", account.public_id, exc
        )
        raise ProblemError(
            "conflict",
            "Billing provider rejected the checkout",
            detail="The billing provider rejected the request.",
        ) from exc

    if account.billing_ref is None:
        account.billing_ref = session.billing_ref
        db.flush()

    return build_envelope(
        {"url": session.url, "session_ref": session.session_ref},
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
    )


# --------------------------------------------------------------------------------------- portal
@router.post("/v1/billing/portal")
def open_portal(
    request: Request,
    ctx: Annotated[AuthContext, Depends(require_session_only("public"))],
    port: Annotated[BillingPort, Depends(get_billing_port)],
) -> Any:
    """Stays available under every posture, including `noncommercial` — unlike `create_checkout`
    above, this route does not read `PAID_TIERS_ACTIVE` and never refuses on it. Managing a
    subscription an account already holds is not selling a new one, and the owner's "mark
    inactive, then flip" decision (docs/26-platform-posture.md §3 precondition (i);
    `docs/00-PLAN.md` 2026-09-26) was to stop *offering* paid tiers, not to strand anyone already
    on one without a way to change their card or cancel."""
    account = ctx.account
    if account is None or account.billing_ref is None:
        raise ProblemError(
            "conflict", "No billing customer on file", detail="Start a checkout before opening the portal."
        )
    try:
        session = port.open_portal(billing_ref=account.billing_ref, return_url=f"{WEB_HOST}/account")
    except SorUnavailable as exc:
        logger.warning("billing portal: provider unavailable for account %s: %s", account.public_id, exc)
        raise ProblemError(
            "sor_unavailable",
            "Billing provider unavailable",
            detail="The billing provider could not be reached; try again shortly.",
        ) from exc
    except SorRejected as exc:
        logger.warning("billing portal: provider rejected request for account %s: %s", account.public_id, exc)
        raise ProblemError(
            "conflict",
            "Billing provider rejected the request",
            detail="The billing provider rejected the request.",
        ) from exc
    return build_envelope(
        {"url": session.url},
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
    )


# ---------------------------------------------------------------------- admin: subscription list
@router.get("/admin/v1/subscriptions")
def admin_list_subscriptions(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    """`POST /admin/v1/subscriptions` (create-through-adapter, `api/openapi.yaml` lines 3729-3757)
    stays unimplemented this wave — it needs the admin-console write path the admin wave owns —
    and is left `x-status: planned` in `api/fragments/billing.yaml` (services/billing/README.md
    "Deferred")."""
    check_allowed(request, {"limit", "cursor", "account_id", "plan_tier", "status"})
    stmt = select(Subscription)

    if account_public_id := request.query_params.get("account_id"):
        account = db.scalar(select(Account).where(Account.public_id == account_public_id))
        if account is None:
            return build_list_envelope(
                [],
                meta=build_meta(lag_days=0, tier="admin"),
                licence_summary=build_licence_summary([]),
                page=build_page(None, None, False),
            )
        stmt = stmt.where(Subscription.account_id == account.id)
    if plan_tier := request.query_params.get("plan_tier"):
        stmt = stmt.where(Subscription.plan_tier.in_(csv_param(plan_tier)))
    if status_filter := request.query_params.get("status"):
        stmt = stmt.where(Subscription.status.in_(csv_param(status_filter)))

    limit_raw = request.query_params.get("limit")
    limit = clamp_limit(int(limit_raw)) if limit_raw is not None else DEFAULT_LIMIT
    cursor = request.query_params.get("cursor")

    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=Subscription.created_at,
        id_column=Subscription.public_id,
        ascending=False,
        cursor=cursor,
        limit=limit,
        instance=request.url.path,
    )
    accounts_by_id = {}
    if rows:
        account_ids = {row.account_id for row in rows}
        accounts_by_id = {
            a.id: a for a in db.scalars(select(Account).where(Account.id.in_(account_ids))).all()
        }
    data = [_serialize_subscription(row, accounts_by_id[row.account_id]) for row in rows]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


__all__ = ["router", "subscriptions_for_account"]
