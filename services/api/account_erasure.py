"""Erasing an account holder: the one procedure behind both ways an account is deleted.

- **Operator**: completing a `deletion_request` task, `PATCH /admin/v1/tasks/{id}` with `status:
  done` (`services/api/admin_people.py::_complete_deletion_task`; US-910).
- **Self-service**: `DELETE /v1/me` (`services/api/auth_routes.py::delete_me`), after the member
  re-enters their password.

Both call `erase_user`, so an in-app deletion and an operator deletion cannot drift apart. The
procedure is docs/21 §6.6 as far as the code reaches today, with the 2026-09-19 erasure fixes
(docs/50-audit-2026-09-18.md §3.1, `services/api/admin_people.md` "Erasure"). Per table:

- `user`: kept as the anchor of the audit chain. `email` becomes `erased-<hash16>@erased.invalid`;
  `name`, `password_hash`, `last_login_at` and `sor_ref` (docs/21 §3.12's personal-data inventory)
  become null; `marketing_consent` false; `status` `anonymised` with `anonymised_at`. Kept:
  `public_id`, `role`, `created_at`, `email_verified_at`, consent and terms versions.
- `session`: every row of the user is deleted (each held a coarse IP prefix).
- `api_key`, the user's own (admin_people decision 2): revoked, `last_used_ip` cleared; the rows stay
  listed on the account as revoked.
- `saved_search`: paused, `email` dropped from `channels`, `rss_token` cleared (the private feed stops
  answering). Name and query are kept because the alert log references them.
- `alert`: `recipient` cleared (docs/21 §3.16 "redacted on deletion"); the send log itself (window,
  event seqs, status, `sent_at`, the metric M-5 source) is kept.
- `webhook_endpoint` created by the user: `disabled` (an account resource, treated like a key).
- `account`, `personal` kind with no other live member: `name` (registration sets it to the address)
  becomes `Deleted account` and `status` `closed`; `billing_ref` and `sor_ref` stay as pointers.
- `subscription`: the mirror is untouched (only the billing provider's webhook writes it). A
  `personal` account's live subscriptions are cancelled through the billing port when it offers
  `cancel_subscription`, else recorded `cancellation_pending` for a human.
- `suppression`: a `(hash(email), erasure)` row is added, so the address is never written to again,
  unless a fresh sign-up later proves the address by its verification link, which lifts that row
  (`services/alerts/suppression.py::lift_on_verified_sign_up`; owner decision 2026-10-10).
- `event`: a `personal_data_redacted` event is added; `before` holds peppered hashes of the email and
  name, never the values.
- `export`, `match_dismissal`: unchanged; they hold no personal data and point at the now-anonymous
  user id.

The CRM goes first (`CrmPort.request_personal_data_deletion`, which opens a task for a human in the
CRM; it never deletes, docs/00-PLAN.md 2026-09-13) because it needs the real address and can fail:
`SorUnavailable` propagates before anything local has changed, and each caller turns it into a
`503 sor_unavailable` with nothing written (the request's one DB session rolls back). Dry-run
adapters stay dry-run: this module only ever sees the ports.

Not done here, recorded as open items in `services/api/admin_people.md`: historical event payloads
are not rewritten (docs/21 §6.6 asks for it; no event this platform writes about a user carries an
address or name, but earlier free-text `reason`s could), and a restore from backup brings erased
values back until the restore drill re-applies erasures (docs/13 §5.5.5 item 5).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from services.alerts.suppression import suppress
from services.api.audit import hash_identifier, record_audit_event
from services.api.common import utcnow
from services.db.models import (
    Account,
    Alert,
    ApiKey,
    Event,
    SavedSearch,
    Subscription,
    User,
    UserSession,
    WebhookEndpoint,
)
from services.sor.ports import BillingPort, CrmPort

#: The reserved-TLD tombstone an erased user's `email` becomes (RFC 2606 `.invalid`): a
#: non-deliverable, non-personal value built from the *peppered* hash, so an operator can still
#: answer "was this address one of ours?" by hashing a candidate, without the log or the row
#: holding the address (docs/50-audit-2026-09-18.md §3.1).
ERASED_EMAIL_DOMAIN = "erased.invalid"
#: What a closed personal account is called once its holder is erased (registration names a
#: personal account after the address, `services/api/auth.py::register_user`).
ERASED_ACCOUNT_NAME = "Deleted account"
#: Who asked: an operator completing a task, or the account holder in-app. Recorded on the event.
Initiator = Literal["operator", "account_holder"]


def tombstone_email(email: str | None) -> str | None:
    digest = hash_identifier(email)
    return f"erased-{digest[:16]}@{ERASED_EMAIL_DOMAIN}" if digest else None


def cancel_billing_for_erased_user(db: Session, user: User, billing: BillingPort) -> dict[str, Any]:
    """Cancels the erased user's subscriptions through the billing port — but only for a
    `personal` account, whose one member is the person being erased. An `organization`
    account's subscription belongs to the organisation and outlives any one member (docs/21
    §3.13/§3.14). `services.sor.ports.BillingPort` has no cancel operation yet (ADR 0006's
    port surface: checkout, portal, create, get, invoices, webhook), so the call is made
    through `cancel_subscription(ref=...)` when the adapter provides it and recorded as pending
    otherwise — never silently skipped. The local `subscription` mirror is *not* edited here:
    it is written only from the provider's webhook (`services/billing/entitlement.py`)."""
    account = db.get(Account, user.account_id)
    if account is None or account.kind != "personal":
        return {"billing": "not_applicable"}
    active = [
        s
        for s in db.scalars(select(Subscription).where(Subscription.account_id == account.id)).all()
        if s.status in ("trialing", "active", "past_due", "paused")
    ]
    if not active:
        return {"billing": "no_active_subscription"}
    cancel = getattr(billing, "cancel_subscription", None)
    if cancel is None:
        return {
            "billing": "cancellation_pending",
            "subscription_refs": [s.sor_ref for s in active],
            "detail": "billing port has no cancel_subscription operation",
        }
    cancelled = [str(cancel(ref=s.sor_ref)) for s in active]
    return {"billing": "cancelled", "subscription_refs": cancelled}


@dataclass(frozen=True)
class ErasureResult:
    """What `erase_user` did, as counts and outcomes only (no personal values)."""

    event: Event
    sessions_deleted: int
    api_keys_revoked: int
    saved_searches_paused: int
    alerts_redacted: int
    webhooks_disabled: int
    account_closed: bool
    billing: dict[str, Any]

    @property
    def billing_pending(self) -> bool:
        return self.billing.get("billing") == "cancellation_pending"


def _close_personal_account(db: Session, user: User) -> bool:
    """A `personal` account whose only live member is being erased is closed and loses its name
    (registration sets the name to the address). An organisation account, or a personal account
    that somehow has another live member, is left alone."""
    account = db.get(Account, user.account_id)
    if account is None or account.kind != "personal":
        return False
    others = db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.account_id == account.id, User.id != user.id, User.status != "anonymised")
    )
    if others:
        return False
    account.name = ERASED_ACCOUNT_NAME
    account.status = "closed"
    return True


def erase_user(
    db: Session,
    user: User,
    *,
    actor: User,
    reason: str,
    initiated_by: Initiator,
    crm: CrmPort,
    billing: BillingPort,
) -> ErasureResult:
    """Erase `user` (module docstring has the per-table table). Raises
    `services.sor.ports.SorUnavailable` from the CRM call before writing anything; the caller
    turns that into `503 sor_unavailable`. `reason` lands on the audit event and in the CRM task,
    so it must not itself carry personal data."""
    original_email = user.email
    crm.request_personal_data_deletion(email=original_email or "", reason=reason)

    before = {
        "email_hash": hash_identifier(original_email),
        "name_hash": hash_identifier(user.name),
        "status": user.status,
    }
    now: dt.datetime = utcnow()
    billing_outcome = cancel_billing_for_erased_user(db, user, billing)
    suppress(db, original_email, "erasure")

    user.email = tombstone_email(original_email)
    user.name = None
    user.password_hash = None
    user.last_login_at = None
    user.sor_ref = None
    user.status = "anonymised"
    user.anonymised_at = now
    user.marketing_consent = False

    sessions_deleted = (
        db.scalar(select(func.count()).select_from(UserSession).where(UserSession.user_id == user.id)) or 0
    )
    db.execute(delete(UserSession).where(UserSession.user_id == user.id))

    api_keys_revoked = 0
    for key in db.scalars(select(ApiKey).where(ApiKey.created_by_user_id == user.id)).all():
        if key.revoked_at is None:
            key.revoked_at = now
            api_keys_revoked += 1
        key.last_used_ip = None

    saved_searches_paused = 0
    for search in db.scalars(select(SavedSearch).where(SavedSearch.user_id == user.id)).all():
        search.status = "paused"
        search.channels = [c for c in search.channels if c != "email"]
        search.rss_token = None
        saved_searches_paused += 1

    alerts_redacted = 0
    for alert in db.scalars(
        select(Alert).where(Alert.user_id == user.id, Alert.recipient.is_not(None))
    ).all():
        alert.recipient = None
        alerts_redacted += 1

    webhooks_disabled = 0
    for endpoint in db.scalars(
        select(WebhookEndpoint).where(WebhookEndpoint.created_by_user_id == user.id)
    ).all():
        if endpoint.status != "disabled":
            endpoint.status = "disabled"
            webhooks_disabled += 1

    account_closed = _close_personal_account(db, user)
    db.flush()

    event = record_audit_event(
        db,
        subject_type="user",
        subject_id=user.id,
        event_type="personal_data_redacted",
        actor=actor,
        reason=reason,
        before=before,
        after={
            "email": "tombstone",
            "name": None,
            "status": "anonymised",
            "initiated_by": initiated_by,
            "suppressed": True,
            "sessions_deleted": sessions_deleted,
            "api_keys_revoked": api_keys_revoked,
            "saved_searches": "paused",
            "alerts_redacted": alerts_redacted,
            "webhooks_disabled": webhooks_disabled,
            "account": "closed" if account_closed else "unchanged",
            **billing_outcome,
        },
    )
    return ErasureResult(
        event=event,
        sessions_deleted=sessions_deleted,
        api_keys_revoked=api_keys_revoked,
        saved_searches_paused=saved_searches_paused,
        alerts_redacted=alerts_redacted,
        webhooks_disabled=webhooks_disabled,
        account_closed=account_closed,
        billing=billing_outcome,
    )


__all__ = [
    "ERASED_ACCOUNT_NAME",
    "ERASED_EMAIL_DOMAIN",
    "ErasureResult",
    "cancel_billing_for_erased_user",
    "erase_user",
    "tombstone_email",
]
