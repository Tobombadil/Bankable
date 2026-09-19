"""Stripe webhook signature verification (`Stripe-Signature: t=<ts>,v1=<hex>[,v1=<hex>...]`).

Shared by `services.billing.stripe.StripeBillingAdapter` and `services.billing.fake.InMemoryBilling`
so the two implementations verify inbound webhooks the same way — `services/billing/README.md`
"Webhook verification" — rather than duplicating a hand-rolled HMAC parser twice. Vendor facts
(header shape, signed-payload construction, default 5-minute tolerance) are from the saved Stripe
webhooks documentation (`services/billing/README.md` cites the exact source).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from collections.abc import Mapping

from services.sor.ports import WebhookRejected

DEFAULT_TOLERANCE_SECONDS = 300


def verify_stripe_signature(
    *,
    body: bytes,
    headers: Mapping[str, str],
    secret: str | None,
    now: dt.datetime,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> None:
    """Raises `WebhookRejected` on any failure; returns `None` on success. Never returns a
    partial result — routers answer 401 and never process the body (`services.sor.ports`
    `WebhookRejected` docstring)."""
    if not secret:
        raise WebhookRejected("no webhook secret configured")
    sig_header = headers.get("Stripe-Signature") or headers.get("stripe-signature")
    if not sig_header:
        raise WebhookRejected("missing Stripe-Signature header")

    timestamps: list[str] = []
    signatures: list[str] = []
    for chunk in sig_header.split(","):
        key, _, value = chunk.strip().partition("=")
        if key == "t":
            timestamps.append(value)
        elif key == "v1":
            signatures.append(value)

    if not timestamps or not signatures:
        raise WebhookRejected("malformed Stripe-Signature header")

    try:
        t_int = int(timestamps[0])
    except ValueError as exc:
        raise WebhookRejected("malformed Stripe-Signature timestamp") from exc

    signed_payload = f"{timestamps[0]}.{body.decode('utf-8')}"
    expected = hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise WebhookRejected("Stripe-Signature mismatch")

    if abs(now.timestamp() - t_int) > tolerance_seconds:
        raise WebhookRejected("Stripe-Signature timestamp outside tolerance")


def sign_payload(body: bytes, *, secret: str, t: int) -> str:
    """The inverse of `verify_stripe_signature` — builds a valid header. Used by
    `InMemoryBilling.sign` (tests) and nowhere in production code (the platform never signs a
    Stripe webhook; it only verifies one)."""
    signed_payload = f"{t}.{body.decode('utf-8')}"
    sig = hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"
