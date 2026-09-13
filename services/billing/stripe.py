"""`StripeBillingAdapter`: the real `BillingPort` implementation, over plain `httpx` form-encoded
requests (CLAUDE.md "no new dependencies" — the `stripe` PyPI package is never installed).

Vendor facts below (endpoints, form shapes, webhook signature scheme, rate limits) come from the
Stripe documentation pages saved to the coordinator's scratchpad and are cited with numbers in
`services/billing/README.md`, not re-derived here. `build_billing_port()` is what
`services.sor.wiring.get_billing_port` imports: it builds a real adapter from environment
variables when `STRIPE_SECRET_KEY` is set, and otherwise falls back to a process-wide
`InMemoryBilling` singleton (dry run), logging that fallback once.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from services.billing.fake import InMemoryBilling
from services.billing.signing import verify_stripe_signature
from services.sor.ports import (
    BillingPort,
    CheckoutRequest,
    CheckoutSession,
    EntitlementChange,
    Invoice,
    PortalSession,
    SorRejected,
    SorUnavailable,
    SubscriptionCreate,
    SubscriptionState,
    entitlement_for,
)

logger = logging.getLogger(__name__)

#: Stripe's own subscription `status` values (https://docs.stripe.com/api/subscriptions/object)
#: mapped onto the platform's docs/21 §3.14 vocabulary. `incomplete` is deliberately absent: a
#: subscription whose first invoice has not yet been paid is not a real decision either way, and
#: mapping it to `past_due` (which keeps entitlement, docs/21 §3.14) would grant access nobody
#: has paid for; mapping it to `canceled` would revoke access from a subscription that might
#: still succeed seconds later. Decision (services/billing/README.md #4): treated as "no change"
#: at the webhook layer (the caller skips emitting an `EntitlementChange` for it) rather than
#: forced into either wrong bucket. `get_subscription` cannot return "no answer" the same way (a
#: caller asked for a specific subscription and needs a `SubscriptionState`), so it falls back to
#: `canceled` (fail-closed: no entitlement) with a warning log when it hits `incomplete` or any
#: status Stripe adds that this table does not yet know — a different rule for a different
#: contract, both documented in the README rather than silently diverging.
_RAW_TO_PLATFORM_STATUS: Mapping[str, str] = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "paused": "paused",
    "canceled": "canceled",
    "unpaid": "canceled",
    "incomplete_expired": "canceled",
}

_SUBSCRIPTION_LIFECYCLE_EVENTS = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.paused",
        "customer.subscription.resumed",
    }
)

_MAX_429_RETRIES = 3


def _map_status(raw: str) -> str | None:
    return _RAW_TO_PLATFORM_STATUS.get(raw)


def _from_unix(value: Any) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(value), tz=dt.UTC)


def _flatten_form(params: Mapping[str, Any], *, prefix: str = "") -> dict[str, str]:
    """Stripe's bracket notation for nested form fields, e.g. `line_items[0][price]=price_123`
    (form-encoded body; the saved `stripe-checkout-create.html` example page uses this shape)."""
    out: dict[str, str] = {}
    for key, value in params.items():
        composed = f"{prefix}[{key}]" if prefix else str(key)
        _flatten_value(composed, value, out)
    return out


def _flatten_value(key: str, value: Any, out: dict[str, str]) -> None:
    if value is None:
        return
    if isinstance(value, Mapping):
        for sub_key, sub_value in value.items():
            _flatten_value(f"{key}[{sub_key}]", sub_value, out)
    elif isinstance(value, (list, tuple)):
        for i, sub_value in enumerate(value):
            _flatten_value(f"{key}[{i}]", sub_value, out)
    elif isinstance(value, bool):
        out[key] = "true" if value else "false"
    else:
        out[key] = str(value)


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        message = body.get("error", {}).get("message")
        if message:
            return str(message)
    except ValueError:
        pass
    return response.text or f"HTTP {response.status_code}"


class StripeBillingAdapter:
    """`BillingPort` over the Stripe HTTP API. Every mutating call carries an `Idempotency-Key`
    derived from the caller's own ids (never a bare random value when a stable one is available)
    so a retried checkout or portal call cannot double-create a Stripe object
    (services/billing/README.md #2). `Stripe-Version` is deliberately not pinned — the saved
    vendor pages only show a `$$API_VERSION_REPLACE_ME$$` placeholder, never a literal current
    version string, so pinning one here would be a guess; the account's configured default
    applies instead (services/billing/README.md "API version decision")."""

    sor_kind = "stripe"

    def __init__(
        self,
        secret_key: str,
        *,
        webhook_secret: str | None,
        price_ids: Mapping[str, str],
        base_url: str = "https://api.stripe.com",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], dt.datetime] | None = None,
        tolerance_seconds: int = 300,
    ) -> None:
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret
        self.price_ids: dict[str, str] = dict(price_ids)
        self.tolerance_seconds = tolerance_seconds
        self._sleep = sleep
        self._clock_fn = clock or (lambda: dt.datetime.now(dt.UTC))
        self._client = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=30.0,
            headers={"Authorization": f"Bearer {secret_key}"},
        )

    def _clock(self) -> dt.datetime:
        return self._clock_fn()

    def _today(self) -> str:
        return self._clock().date().isoformat()

    # ---------------------------------------------------------------------------------- HTTP core
    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        not_found_returns_none: bool = False,
    ) -> dict[str, Any] | None:
        attempts_429 = 0
        retried_5xx = False
        while True:
            try:
                response = self._client.request(method, path, data=data, params=params, headers=headers)
            except httpx.TransportError as exc:
                if retried_5xx:
                    raise SorUnavailable(f"stripe transport error: {exc}") from exc
                retried_5xx = True
                self._sleep(1.0)
                continue

            if response.status_code == 404 and not_found_returns_none:
                return None
            if response.status_code == 429:
                attempts_429 += 1
                if attempts_429 > _MAX_429_RETRIES:
                    raise SorUnavailable("stripe rate limit exceeded after retries")
                retry_after = float(response.headers.get("Retry-After", "1"))
                self._sleep(retry_after)
                continue
            if 500 <= response.status_code < 600:
                if retried_5xx:
                    raise SorUnavailable(f"stripe server error {response.status_code}")
                retried_5xx = True
                self._sleep(1.0)
                continue
            if 400 <= response.status_code < 500:
                message = _error_message(response)
                logger.warning("stripe rejected %s %s: %s", method, path, message)
                raise SorRejected(message)
            return dict(response.json())

    def _post(self, path: str, params: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        result = self._request(
            "POST", path, data=_flatten_form(params), headers={"Idempotency-Key": idempotency_key}
        )
        if result is None:  # pragma: no cover - `not_found_returns_none` is never set for POST
            raise SorUnavailable("stripe returned an empty response body")
        return result

    def _get(
        self, path: str, *, params: dict[str, str] | None = None, not_found_returns_none: bool = False
    ) -> dict[str, Any] | None:
        return self._request("GET", path, params=params, not_found_returns_none=not_found_returns_none)

    # ------------------------------------------------------------------------------------ BillingPort
    def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        if request.plan not in self.price_ids:
            raise SorRejected(f"unknown plan {request.plan!r}: no configured Stripe price id")

        billing_ref = request.billing_ref
        if billing_ref is None:
            customer = self._post(
                "/v1/customers",
                {
                    "email": request.customer_email,
                    "metadata": {"account_public_id": request.account_public_id},
                },
                idempotency_key=f"customer:{request.account_public_id}",
            )
            billing_ref = str(customer["id"])

        session = self._post(
            "/v1/checkout/sessions",
            {
                "mode": "subscription",
                "customer": billing_ref,
                "client_reference_id": request.account_public_id,
                "line_items": [{"price": self.price_ids[request.plan], "quantity": request.seats}],
                "success_url": request.success_url,
                "cancel_url": request.cancel_url,
                "subscription_data": {
                    "metadata": {"account_public_id": request.account_public_id, "plan": request.plan}
                },
            },
            idempotency_key=f"checkout:{request.account_public_id}:{request.plan}:{self._today()}",
        )
        return CheckoutSession(
            url=str(session["url"]), session_ref=str(session["id"]), billing_ref=billing_ref
        )

    def open_portal(self, *, billing_ref: str, return_url: str) -> PortalSession:
        session = self._post(
            "/v1/billing_portal/sessions",
            {"customer": billing_ref, "return_url": return_url},
            idempotency_key=f"portal:{billing_ref}:{self._today()}",
        )
        return PortalSession(url=str(session["url"]))

    def create_subscription(self, request: SubscriptionCreate) -> SubscriptionState:
        """`POST /v1/subscriptions` (US-902: an operator creates a subscription through the admin
        console rather than the customer self-serving through Checkout). Invoice-based collection
        (`collection_method=send_invoice`, `days_until_due=30`) rather than
        `charge_automatically`: an operator-created subscription has no payment method on file yet
        (services/billing/README.md "collection_method / days_until_due" — `collection_method` is
        confirmed as a real Stripe subscription field from the saved vendor pages' own changelog
        reference; `days_until_due`'s exact behavior is not independently verified there and is
        kept, marked unverified, as Stripe's documented invoice-collection field). The mirror row
        is refreshed from this call's read-back (`_subscription_state_from_object`), never from
        `request` itself (US-902 AC2)."""
        if request.plan not in self.price_ids:
            raise SorRejected(f"unknown plan {request.plan!r}: no configured Stripe price id")

        params: dict[str, Any] = {
            "customer": request.billing_ref,
            "items": [{"price": self.price_ids[request.plan], "quantity": request.seats}],
            "collection_method": "send_invoice",
            "days_until_due": 30,
        }
        if request.trial_days is not None:
            params["trial_period_days"] = request.trial_days
        metadata: dict[str, str] = {}
        if request.account_public_id:
            metadata["account_public_id"] = request.account_public_id
        metadata["plan"] = request.plan
        params["metadata"] = metadata

        obj = self._post(
            "/v1/subscriptions",
            params,
            idempotency_key=f"subscription:{request.billing_ref}:{request.plan}:{self._today()}",
        )
        return self._subscription_state_from_object(obj)

    def get_subscription(self, ref: str) -> SubscriptionState | None:
        obj = self._get(f"/v1/subscriptions/{ref}", not_found_returns_none=True)
        if obj is None:
            return None
        return self._subscription_state_from_object(obj)

    def list_invoices(self, *, billing_ref: str) -> list[Invoice]:
        result = self._get("/v1/invoices", params={"customer": billing_ref, "limit": "24"})
        rows = (result or {}).get("data", [])
        invoices: list[Invoice] = []
        for row in rows:
            invoices.append(
                Invoice(
                    ref=str(row["id"]),
                    status=str(row.get("status", "")),
                    amount_due=float(row.get("amount_due", 0)) / 100,
                    currency=str(row.get("currency", "usd")).upper(),
                    created_at=_from_unix(row["created"]),
                    hosted_url=row.get("hosted_invoice_url"),
                )
            )
        return invoices

    def handle_webhook(self, *, body: bytes, headers: Mapping[str, str]) -> list[EntitlementChange]:
        verify_stripe_signature(
            body=body,
            headers=headers,
            secret=self.webhook_secret,
            now=self._clock(),
            tolerance_seconds=self.tolerance_seconds,
        )
        payload = json.loads(body.decode("utf-8"))
        event_id = str(payload["id"])
        event_type = str(payload.get("type", ""))
        obj: dict[str, Any] = payload.get("data", {}).get("object", {})
        occurred_at = _from_unix(payload["created"]) if "created" in payload else self._clock()

        if event_type in _SUBSCRIPTION_LIFECYCLE_EVENTS:
            if obj.get("status") == "incomplete":
                logger.info("stripe event %s: subscription incomplete, no entitlement change", event_id)
                return []
            state = self._subscription_state_from_object(obj)
            return self._changes_from_state(
                state,
                event_ref=event_id,
                occurred_at=occurred_at,
                account_public_id=self._account_public_id(obj),
            )

        if event_type == "customer.subscription.deleted":
            state = self._subscription_state_from_object(obj, force_status="canceled")
            return self._changes_from_state(
                state,
                event_ref=event_id,
                occurred_at=occurred_at,
                account_public_id=self._account_public_id(obj),
            )

        if event_type == "checkout.session.completed":
            if obj.get("mode") != "subscription":
                return []
            sub_ref = obj.get("subscription")
            if not sub_ref:
                return []
            fetched_state = self.get_subscription(sub_ref if isinstance(sub_ref, str) else sub_ref.get("id"))
            if fetched_state is None:
                return []
            account_public_id = self._account_public_id(
                obj, client_reference_id=obj.get("client_reference_id")
            )
            return self._changes_from_state(
                fetched_state,
                event_ref=event_id,
                occurred_at=occurred_at,
                account_public_id=account_public_id,
            )

        if event_type in ("invoice.paid", "invoice.payment_failed"):
            sub_ref = obj.get("subscription")
            if not sub_ref:
                return []
            fetched_state = self.get_subscription(sub_ref if isinstance(sub_ref, str) else sub_ref.get("id"))
            if fetched_state is None:
                return []
            return self._changes_from_state(
                fetched_state,
                event_ref=event_id,
                occurred_at=occurred_at,
                account_public_id=self._account_public_id(obj),
            )

        return []

    # ---------------------------------------------------------------------------------- internals
    def _subscription_state_from_object(
        self, obj: Mapping[str, Any], *, force_status: str | None = None
    ) -> SubscriptionState:
        sub_id = str(obj["id"])
        customer = obj["customer"]
        billing_ref = customer if isinstance(customer, str) else str(customer["id"])

        items = ((obj.get("items") or {}).get("data")) or []
        first_item: dict[str, Any] = items[0] if items else {}
        price: dict[str, Any] = first_item.get("price") or {}
        price_id = price.get("id")

        plan_tier = (obj.get("metadata") or {}).get("plan")
        if not plan_tier and price_id:
            plan_tier = next((k for k, v in self.price_ids.items() if v == price_id), None)
        if not plan_tier:
            logger.warning(
                "stripe subscription %s has no resolvable plan tier (no metadata.plan, price %r not in "
                "configured price_ids); defaulting to 'pro'",
                sub_id,
                price_id,
            )
            plan_tier = "pro"

        raw_status = str(obj.get("status", ""))
        status = force_status or _map_status(raw_status)
        if status is None:
            logger.warning(
                "stripe subscription %s has unmapped status %r; treating as canceled (fail-closed)",
                sub_id,
                raw_status,
            )
            status = "canceled"

        quantity = int(first_item.get("quantity", 1))
        unit_amount = price.get("unit_amount")
        interval = (price.get("recurring") or {}).get("interval")
        mrr_amount: float | None = None
        if unit_amount is not None:
            gross = (float(unit_amount) * quantity) / 100
            mrr_amount = gross / 12 if interval == "year" else gross
        currency = str(price.get("currency") or obj.get("currency") or "usd").upper()

        cps = obj.get("current_period_start") or first_item.get("current_period_start")
        cpe = obj.get("current_period_end") or first_item.get("current_period_end")
        if cps is None or cpe is None:
            # No known Stripe API shape omits these on a real subscription; fail-safe rather than
            # raise, so a webhook with an unexpected shape still produces a (stale) mirror row
            # instead of crashing the receiver (services/billing/README.md #6).
            logger.warning("stripe subscription %s missing current_period_start/end; using now()", sub_id)
        period_start = _from_unix(cps) if cps is not None else self._clock()
        period_end = _from_unix(cpe) if cpe is not None else self._clock()
        cancel_at = obj.get("cancel_at")

        return SubscriptionState(
            sor_kind="stripe",
            sor_ref=sub_id,
            billing_ref=billing_ref,
            plan_code=str(price_id or plan_tier),
            plan_tier=str(plan_tier),
            status=status,
            seats=quantity,
            current_period_start=period_start,
            current_period_end=period_end,
            currency=currency,
            cancel_at=_from_unix(cancel_at) if cancel_at is not None else None,
            mrr_amount=mrr_amount,
        )

    @staticmethod
    def _account_public_id(obj: Mapping[str, Any], *, client_reference_id: str | None = None) -> str | None:
        metadata = obj.get("metadata") or {}
        found = metadata.get("account_public_id")
        return found if found else client_reference_id

    @staticmethod
    def _changes_from_state(
        state: SubscriptionState, *, event_ref: str, occurred_at: dt.datetime, account_public_id: str | None
    ) -> list[EntitlementChange]:
        entitlement = entitlement_for(state.plan_tier, state.status)
        return [
            EntitlementChange(
                event_ref=event_ref,
                occurred_at=occurred_at,
                billing_ref=state.billing_ref,
                subscription=state,
                entitlement=entitlement,
                account_public_id=account_public_id,
            )
        ]


_fallback_lock = threading.Lock()
_fallback_instance: InMemoryBilling | None = None
_fallback_logged = False


def build_billing_port() -> BillingPort:
    """`services.sor.wiring.get_billing_port` imports this. Real adapter from `STRIPE_SECRET_KEY`
    plus the four `STRIPE_PRICE_*` env vars (CLAUDE.md: secrets/config from environment only, no
    credential in the repo); otherwise a process-wide `InMemoryBilling` dry run, logged once
    (services/billing/README.md #1)."""
    secret_key = os.environ.get("STRIPE_SECRET_KEY")
    if secret_key:
        price_ids = {
            "pro": os.environ.get("STRIPE_PRICE_PRO", ""),
            "team": os.environ.get("STRIPE_PRICE_TEAM", ""),
            "api": os.environ.get("STRIPE_PRICE_API", ""),
            "enterprise": os.environ.get("STRIPE_PRICE_ENTERPRISE", ""),
        }
        return StripeBillingAdapter(
            secret_key,
            webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET"),
            price_ids={k: v for k, v in price_ids.items() if v},
        )
    global _fallback_instance, _fallback_logged
    with _fallback_lock:
        if _fallback_instance is None:
            _fallback_instance = InMemoryBilling()
        if not _fallback_logged:
            logger.warning("STRIPE_SECRET_KEY not set; billing port running InMemoryBilling (dry run)")
            _fallback_logged = True
        return _fallback_instance


__all__ = ["StripeBillingAdapter", "build_billing_port"]
