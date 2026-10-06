"""Turns a vendor-neutral `EntitlementChange` (from `BillingPort.handle_webhook`) into the
database write it means: an upserted `Subscription` mirror row, an `account.entitlement` update,
and an append-only `Event` row for the audit trail — one function, called from
`services/billing/router.py`'s webhook handler.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.common import utcnow
from services.db.models import SUBSCRIPTION_PLAN_TIERS, Account, Event, Subscription
from services.ids import public_id
from services.sor.ports import CrmPort, EntitlementChange, SorError, SubscriptionMirror

logger = logging.getLogger(__name__)

#: `Subscription.plan_tier`'s CHECK is docs/21's `free | pro | team | api` (`services/db/models.py`
#: `Subscription` docstring, `services/billing/README.md` decision #3) — narrower than
#: `services.sor.ports.PLAN_TIERS` (`pro | team | api | enterprise`), which is what a Stripe
#: subscription's `plan_tier` actually carries when the purchase was `enterprise`. `enterprise`
#: grants the same entitlement as `api` (`services.sor.ports.PLAN_ENTITLEMENT`), so it is stored
#: as `api` here; `plan_code` keeps the vendor's exact code, so nothing about the purchase is lost.
_PLAN_TIER_FOR_STORAGE = {"enterprise": "api"}


def _plan_tier_for_storage(plan_tier: str) -> str:
    mapped = _PLAN_TIER_FOR_STORAGE.get(plan_tier, plan_tier)
    if mapped not in SUBSCRIPTION_PLAN_TIERS:
        logger.warning("unrecognised plan_tier %r from billing adapter; storing as 'api'", plan_tier)
        return "api"
    return mapped


def _find_account(db: Session, change: EntitlementChange) -> Account | None:
    account: Account | None = None
    if change.billing_ref:
        account = db.scalar(select(Account).where(Account.billing_ref == change.billing_ref))
    if account is None and change.account_public_id:
        account = db.scalar(select(Account).where(Account.public_id == change.account_public_id))
        if account is not None and change.billing_ref and account.billing_ref != change.billing_ref:
            account.billing_ref = change.billing_ref
    return account


def _aware(value: dt.datetime) -> dt.datetime:
    # SQLite returns a stored instant naive; every instant this module writes is UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _mirror_row(db: Session, change: EntitlementChange) -> Subscription | None:
    state = change.subscription
    return db.scalar(
        select(Subscription).where(
            Subscription.sor_kind == state.sor_kind, Subscription.sor_ref == state.sor_ref
        )
    )


def is_older_than_applied(row: Subscription | None, change: EntitlementChange) -> bool:
    """Whether `change` was created before the event last applied to its subscription (backend
    audit 2026-09-30 F4). Stripe does not deliver events in order, and each carries the subscription
    as it was when the event was created, so applying an older one would restore an older state: a
    late `active` after a newer `canceled` re-granted Pro. Equal times are applied (Stripe's
    `created` has one-second resolution, so their order is unknown); the last one delivered wins."""
    if row is None or row.last_event_at is None:
        return False
    return _aware(change.occurred_at) < _aware(row.last_event_at)


def _upsert_subscription_mirror(
    db: Session, account: Account, change: EntitlementChange, *, now: dt.datetime, row: Subscription | None
) -> Subscription:
    state = change.subscription
    if row is None:
        row = Subscription(
            public_id="",
            account_id=account.id,
            sor_kind=state.sor_kind,
            sor_ref=state.sor_ref,
            plan_code=state.plan_code,
            plan_tier=_plan_tier_for_storage(state.plan_tier),
            status=state.status,
            seats=state.seats,
            current_period_start=state.current_period_start,
            current_period_end=state.current_period_end,
            cancel_at=state.cancel_at,
            mrr_amount=state.mrr_amount,
            currency=state.currency,
            mirrored_at=now,
            drift_flag=False,
            last_event_at=change.occurred_at,
        )
        db.add(row)
        db.flush()
        # `subn` per `api/openapi.yaml`'s `SubscriptionPublicId` pattern (`sub` is not the
        # spec's chosen prefix for this entity — verified against the pattern directly rather
        # than assumed, services/billing/README.md decision #8).
        row.public_id = public_id("subn", row.id)
    else:
        row.account_id = account.id
        row.plan_code = state.plan_code
        row.plan_tier = _plan_tier_for_storage(state.plan_tier)
        row.status = state.status
        row.seats = state.seats
        row.current_period_start = state.current_period_start
        row.current_period_end = state.current_period_end
        row.cancel_at = state.cancel_at
        row.mrr_amount = state.mrr_amount
        row.currency = state.currency
        row.mirrored_at = now
        row.drift_flag = False
        row.last_event_at = change.occurred_at
    db.flush()
    return row


def apply_entitlement_change(
    db: Session, change: EntitlementChange, *, crm: CrmPort | None = None, now: dt.datetime | None = None
) -> Account | None:
    """Idempotent on `change.event_ref` (an `Event` row with `idempotency_key =
    f"billing:{change.event_ref}"` already existing means this webhook was already applied — a
    replayed Stripe delivery is a no-op, not a double-apply). Returns `None` — logging, never
    raising — when nothing is applied and a retry would not change that, so the webhook route can
    still answer Stripe with a 200 (docs/23 §8: Stripe retries on anything but 2xx): no account
    matches, or the change is older than the event last applied to its subscription
    (`is_older_than_applied`). A database failure raises, and the route answers non-2xx so Stripe
    delivers the event again."""
    resolved_now = now if now is not None else utcnow()
    account = _find_account(db, change)
    if account is None:
        logger.warning(
            "billing entitlement change %s: no account for billing_ref=%r account_public_id=%r",
            change.event_ref,
            change.billing_ref,
            change.account_public_id,
        )
        return None

    idempotency_key = f"billing:{change.event_ref}"
    already_applied = db.scalar(select(Event).where(Event.idempotency_key == idempotency_key))
    if already_applied is not None:
        return account

    row = _mirror_row(db, change)
    if is_older_than_applied(row, change):
        logger.info(
            "billing entitlement change %s ignored: created %s, before the last applied event (%s)",
            change.event_ref,
            change.occurred_at.isoformat(),
            row.last_event_at.isoformat() if row is not None and row.last_event_at is not None else None,
        )
        return None

    _upsert_subscription_mirror(db, account, change, now=resolved_now, row=row)

    before_entitlement = account.entitlement
    if account.entitlement == "admin":
        # docs/21 §3.13: `admin` is never derived from billing. Operator accounts are a manual
        # grant; a Stripe webhook (e.g. an operator's personal test subscription) must never
        # demote or otherwise touch one.
        logger.info(
            "billing entitlement change %s: account %s is 'admin'; entitlement left untouched",
            change.event_ref,
            account.public_id,
        )
    else:
        account.entitlement = change.entitlement
        account.entitlement_source = "sor"
        account.entitlement_checked_at = resolved_now
        account.entitlement_stale = False
        account.seats = change.subscription.seats
        db.flush()

    event = Event(
        subject_type="account",
        subject_id=account.id,
        # `event.event_type` carries no CHECK constraint (`services/db/models.py` `Event.
        # __table_args__` only constrains `actor_type`), so no existing vocab tuple needs the
        # "extend only if a CHECK blocks you" escape hatch here — `entitlement_changed` is a new,
        # freely-chosen value (services/billing/README.md decision #5).
        event_type="entitlement_changed",
        observed_at=change.occurred_at,
        recorded_at=resolved_now,
        # Mirrors `services.api.audit.record_audit_event`'s convention for a non-pipeline event
        # (published_at = public_at = now): this is a system action, not raw source ingest, so it
        # has no lag to hide. It is still never surfaced on the public proposal/opportunity
        # timeline — every public-feed endpoint filters by subject_type in {proposal, opportunity}
        # — but leaving the honest timestamp here rather than nulling it matches the existing
        # admin_edit convention (services/billing/README.md decision #5).
        published_at=resolved_now,
        public_at=resolved_now,
        before={"entitlement": before_entitlement},
        after={"entitlement": account.entitlement},
        changed_keys=["entitlement"],
        actor_type="system",
        reason=f"stripe webhook {change.event_ref}",
        idempotency_key=idempotency_key,
    )
    db.add(event)
    db.flush()

    if crm is not None:
        state = change.subscription
        mirror = SubscriptionMirror(
            stripe_subscription_id=state.sor_ref,
            stripe_customer_id=state.billing_ref,
            platform_account_id=account.public_id,
            plan=state.plan_tier,
            seats=state.seats,
            status=state.status,
            current_period_end=state.current_period_end,
            mrr=state.mrr_amount,
        )
        try:
            crm.upsert_subscription(mirror)
        except SorError:
            # docs/34 §2.5: the CRM's Subscription object is a read-only mirror for humans; the
            # platform state (`account.entitlement`, the `subscription` row above) is already
            # correct and authoritative regardless of whether Attio is reachable right now.
            logger.warning(
                "billing entitlement change %s: CRM subscription mirror failed for account %s",
                change.event_ref,
                account.public_id,
                exc_info=True,
            )

    db.flush()
    return account


__all__ = ["apply_entitlement_change", "is_older_than_applied"]
