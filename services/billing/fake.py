"""`InMemoryBilling`: the dry-run `BillingPort` used when no Stripe secret key is configured
(`services.billing.stripe.build_billing_port`'s fallback) and by every test in this package and
`services/billing/test_router.py` that needs a billing port without live network access
(CLAUDE.md "no live network in tests").

`handle_webhook` deliberately verifies the *same* `t=…,v1=…` HMAC scheme
(`services.billing.signing`) that the real Stripe adapter does, over a reduced JSON body shaped
like a real Stripe event envelope (`{"id", "type", "data": {"object": {...}}}`) but with a flat,
already-platform-shaped `object` (no nested `items.data[0].price...`, no metadata indirection) —
this is a test double's contract, not Stripe's real payload; a router or entitlement test that
sends one through `POST /webhooks/stripe` still exercises the real signature-verification code
path (`services.billing.signing.verify_stripe_signature`), just not Stripe's real object shape.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.billing.signing import sign_payload, verify_stripe_signature
from services.sor.ports import (
    CheckoutRequest,
    CheckoutSession,
    EntitlementChange,
    Invoice,
    PortalSession,
    SubscriptionState,
    entitlement_for,
)

_DEFAULT_WEBHOOK_SECRET = "test-webhook-secret"  # noqa: S105 - low-entropy fixture value, not a credential


@dataclass(frozen=True)
class RecordedCheckout:
    """One `create_checkout` call, kept for tests to assert on (`request` is the exact
    `CheckoutRequest` passed in)."""

    request: CheckoutRequest
    session_ref: str
    billing_ref: str


@dataclass(frozen=True)
class RecordedPortal:
    billing_ref: str
    return_url: str
    url: str


def _parse_dt(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=dt.UTC)
    text = str(value).replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


class InMemoryBilling:
    """Public attributes (`checkouts`, `portals`, `subscriptions`, `invoices`) are there for other
    suites to assert on directly, the same way `services.api.auth.ResendEmailAdapter.sent`
    works."""

    sor_kind = "stripe"

    def __init__(self, *, webhook_secret: str = _DEFAULT_WEBHOOK_SECRET) -> None:
        self.webhook_secret = webhook_secret
        self._counter = itertools.count(1)
        self.checkouts: list[RecordedCheckout] = []
        self.portals: list[RecordedPortal] = []
        self.subscriptions: dict[str, SubscriptionState] = {}
        self.invoices: dict[str, list[Invoice]] = {}

    # ------------------------------------------------------------------------------------ BillingPort
    def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        n = next(self._counter)
        billing_ref = request.billing_ref or f"cus_fake_{n}"
        session_ref = f"cs_fake_{n}"
        url = f"https://checkout.stripe.invalid/c/{n}"
        self.checkouts.append(
            RecordedCheckout(request=request, session_ref=session_ref, billing_ref=billing_ref)
        )
        return CheckoutSession(url=url, session_ref=session_ref, billing_ref=billing_ref)

    def open_portal(self, *, billing_ref: str, return_url: str) -> PortalSession:
        n = next(self._counter)
        url = f"https://portal.stripe.invalid/p/{n}"
        self.portals.append(RecordedPortal(billing_ref=billing_ref, return_url=return_url, url=url))
        return PortalSession(url=url)

    def get_subscription(self, ref: str) -> SubscriptionState | None:
        return self.subscriptions.get(ref)

    def list_invoices(self, *, billing_ref: str) -> list[Invoice]:
        return list(self.invoices.get(billing_ref, []))

    def handle_webhook(self, *, body: bytes, headers: Mapping[str, str]) -> list[EntitlementChange]:
        verify_stripe_signature(
            body=body, headers=headers, secret=self.webhook_secret, now=dt.datetime.now(dt.UTC)
        )
        payload = json.loads(body.decode("utf-8"))
        event_id = str(payload.get("id", f"evt_fake_{next(self._counter)}"))
        obj: dict[str, Any] = payload.get("data", {}).get("object", {})

        state = SubscriptionState(
            sor_kind="stripe",
            sor_ref=str(obj["id"]),
            billing_ref=str(obj.get("customer", "")),
            plan_code=str(obj.get("plan_code") or obj.get("plan_tier", "")),
            plan_tier=str(obj.get("plan_tier", "pro")),
            status=str(obj.get("status", "active")),
            seats=int(obj.get("seats", 1)),
            current_period_start=_parse_dt(obj.get("current_period_start")) or dt.datetime.now(dt.UTC),
            current_period_end=_parse_dt(obj.get("current_period_end")) or dt.datetime.now(dt.UTC),
            currency=str(obj.get("currency", "USD")).upper(),
            cancel_at=_parse_dt(obj.get("cancel_at")),
            mrr_amount=obj.get("mrr_amount"),
        )
        self.subscriptions[state.sor_ref] = state
        entitlement = entitlement_for(state.plan_tier, state.status)
        return [
            EntitlementChange(
                event_ref=event_id,
                occurred_at=dt.datetime.now(dt.UTC),
                billing_ref=state.billing_ref,
                subscription=state,
                entitlement=entitlement,
                account_public_id=obj.get("account_public_id"),
            )
        ]

    # ------------------------------------------------------------------------------------ test helper
    def sign(self, body: bytes, *, t: int | None = None) -> str:
        ts = t if t is not None else int(dt.datetime.now(dt.UTC).timestamp())
        return sign_payload(body, secret=self.webhook_secret, t=ts)


__all__ = ["InMemoryBilling", "RecordedCheckout", "RecordedPortal"]
