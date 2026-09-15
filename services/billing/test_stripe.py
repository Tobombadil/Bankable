"""`StripeBillingAdapter` tests: webhook signature verification, event-to-`EntitlementChange`
mapping, outbound request shapes (form encoding, `Idempotency-Key`), and the HTTP retry/error
rules (`services/billing/README.md` "Decisions"). No live network anywhere — every HTTP call goes
through `httpx.MockTransport` (CLAUDE.md "no live network in tests").

Fixture event/object JSON files under `tests/fixtures/stripe/` are hand-written in Stripe's
documented shape (the saved `docs.stripe.com` pages) — not recorded from a live Stripe account, as
`tests/fixtures/stripe/README.md` states.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import urllib.parse
from collections.abc import Callable

import httpx
import pytest

from services.billing.signing import sign_payload
from services.billing.stripe import StripeBillingAdapter
from services.sor.ports import (
    CheckoutRequest,
    SorRejected,
    SorUnavailable,
    SubscriptionCreate,
    WebhookRejected,
)

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "stripe"
SECRET_KEY = "test-secret-key"  # noqa: S105 - low-entropy fixture value, not a credential
WEBHOOK_SECRET = "test-webhook-secret"  # noqa: S105 - low-entropy fixture value, not a credential
PRICE_IDS = {
    "pro": "price_test_pro",
    "team": "price_test_team",
    "api": "price_test_api",
    "enterprise": "price_test_enterprise",
}


def load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def load_fixture_json(name: str) -> dict:
    return json.loads(load_fixture(name))


def make_adapter(
    *, transport: httpx.MockTransport | None = None, sleep: Callable[[float], None] | None = None
) -> StripeBillingAdapter:
    return StripeBillingAdapter(
        SECRET_KEY,
        webhook_secret=WEBHOOK_SECRET,
        price_ids=PRICE_IDS,
        transport=transport,
        sleep=sleep or (lambda _seconds: None),
    )


def sign(body: bytes, *, t: int | None = None, secret: str = WEBHOOK_SECRET) -> str:
    ts = t if t is not None else int(dt.datetime.now(dt.UTC).timestamp())
    return sign_payload(body, secret=secret, t=ts)


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(request.content.decode()))


def _transport_for(routes: dict[tuple[str, str], httpx.Response]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key not in routes:
            raise AssertionError(f"unexpected request {key}")
        return routes[key]

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------------- signature
def test_webhook_valid_signature_maps_event() -> None:
    body = load_fixture("subscription_created.json")
    adapter = make_adapter()
    changes = adapter.handle_webhook(body=body, headers={"Stripe-Signature": sign(body)})
    assert len(changes) == 1
    change = changes[0]
    assert change.entitlement == "pro"
    assert change.subscription.status == "active"
    assert change.subscription.seats == 3
    assert change.subscription.mrr_amount == pytest.approx(147.0)
    assert change.account_public_id == "acc_00000000TESTACCT1"


def test_webhook_wrong_secret_rejected() -> None:
    body = load_fixture("subscription_created.json")
    adapter = make_adapter()
    header = sign(body, secret="a-completely-different-secret")  # noqa: S106 - fixture value, not a credential
    with pytest.raises(WebhookRejected):
        adapter.handle_webhook(body=body, headers={"Stripe-Signature": header})


def test_webhook_tampered_body_rejected() -> None:
    body = load_fixture("subscription_created.json")
    header = sign(body)
    tampered = body.replace(b'"active"', b'"paused"')
    adapter = make_adapter()
    with pytest.raises(WebhookRejected):
        adapter.handle_webhook(body=tampered, headers={"Stripe-Signature": header})


def test_webhook_stale_timestamp_rejected() -> None:
    body = load_fixture("subscription_created.json")
    old_t = int(dt.datetime.now(dt.UTC).timestamp()) - 10_000
    header = sign(body, t=old_t)
    adapter = make_adapter()
    with pytest.raises(WebhookRejected):
        adapter.handle_webhook(body=body, headers={"Stripe-Signature": header})


def test_webhook_missing_header_rejected() -> None:
    body = load_fixture("subscription_created.json")
    adapter = make_adapter()
    with pytest.raises(WebhookRejected):
        adapter.handle_webhook(body=body, headers={})


def test_webhook_no_secret_configured_rejected() -> None:
    body = load_fixture("subscription_created.json")
    adapter = StripeBillingAdapter(SECRET_KEY, webhook_secret=None, price_ids=PRICE_IDS)
    with pytest.raises(WebhookRejected):
        adapter.handle_webhook(body=body, headers={"Stripe-Signature": sign(body)})


# ---------------------------------------------------------------------------- event mapping
def test_subscription_updated_past_due_keeps_entitlement() -> None:
    body = load_fixture("subscription_updated.json")
    changes = make_adapter().handle_webhook(body=body, headers={"Stripe-Signature": sign(body)})
    assert changes[0].subscription.status == "past_due"
    assert changes[0].entitlement == "pro"


def test_subscription_deleted_forces_canceled_status() -> None:
    body = load_fixture("subscription_deleted.json")
    changes = make_adapter().handle_webhook(body=body, headers={"Stripe-Signature": sign(body)})
    assert changes[0].subscription.status == "canceled"
    assert changes[0].entitlement == "public"


def test_subscription_paused_drops_entitlement() -> None:
    body = load_fixture("subscription_paused.json")
    changes = make_adapter().handle_webhook(body=body, headers={"Stripe-Signature": sign(body)})
    assert changes[0].subscription.status == "paused"
    assert changes[0].entitlement == "public"


def test_subscription_resumed_restores_entitlement() -> None:
    body = load_fixture("subscription_resumed.json")
    changes = make_adapter().handle_webhook(body=body, headers={"Stripe-Signature": sign(body)})
    assert changes[0].subscription.status == "active"
    assert changes[0].entitlement == "pro"


def test_subscription_incomplete_is_no_change() -> None:
    body = load_fixture("subscription_incomplete.json")
    changes = make_adapter().handle_webhook(body=body, headers={"Stripe-Signature": sign(body)})
    assert changes == []


def test_unknown_event_type_is_no_change() -> None:
    body = load_fixture("unknown_event.json")
    changes = make_adapter().handle_webhook(body=body, headers={"Stripe-Signature": sign(body)})
    assert changes == []


def test_checkout_session_completed_payment_mode_is_no_change() -> None:
    body = load_fixture("checkout_session_completed_payment_mode.json")
    changes = make_adapter().handle_webhook(body=body, headers={"Stripe-Signature": sign(body)})
    assert changes == []


def test_checkout_session_completed_subscription_mode_fetches_and_maps() -> None:
    body = load_fixture("checkout_session_completed.json")
    sub_obj = load_fixture_json("subscription_object.json")
    transport = _transport_for(
        {("GET", "/v1/subscriptions/sub_1NG8Du2eZvKYlo2Cq5f8Vsxu"): httpx.Response(200, json=sub_obj)}
    )
    changes = make_adapter(transport=transport).handle_webhook(
        body=body, headers={"Stripe-Signature": sign(body)}
    )
    assert len(changes) == 1
    assert changes[0].account_public_id == "acc_00000000TESTACCT1"
    assert changes[0].entitlement == "pro"


def test_invoice_paid_fetches_and_maps_subscription() -> None:
    body = load_fixture("invoice_paid.json")
    sub_obj = load_fixture_json("subscription_object.json")
    transport = _transport_for(
        {("GET", "/v1/subscriptions/sub_1NG8Du2eZvKYlo2Cq5f8Vsxu"): httpx.Response(200, json=sub_obj)}
    )
    changes = make_adapter(transport=transport).handle_webhook(
        body=body, headers={"Stripe-Signature": sign(body)}
    )
    assert len(changes) == 1
    assert changes[0].entitlement == "pro"


def test_invoice_payment_failed_fetches_and_maps_subscription() -> None:
    body = load_fixture("invoice_payment_failed.json")
    sub_obj = load_fixture_json("subscription_object.json")
    transport = _transport_for(
        {("GET", "/v1/subscriptions/sub_1NG8Du2eZvKYlo2Cq5f8Vsxu"): httpx.Response(200, json=sub_obj)}
    )
    changes = make_adapter(transport=transport).handle_webhook(
        body=body, headers={"Stripe-Signature": sign(body)}
    )
    assert len(changes) == 1


# --------------------------------------------------------------------------------- request shapes
def test_create_checkout_new_customer_request_shape() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/customers":
            return httpx.Response(200, json={"id": "cus_fake_new"})
        return httpx.Response(200, json={"id": "cs_fake_1", "url": "https://checkout.stripe.com/c/1"})

    result = make_adapter(transport=httpx.MockTransport(handler)).create_checkout(
        CheckoutRequest(
            account_public_id="acc_00000000TESTACCT1",
            customer_email="person@example.com",
            plan="pro",
            seats=2,
            success_url="https://example.com/s",
            cancel_url="https://example.com/c",
        )
    )
    assert result.billing_ref == "cus_fake_new"
    assert result.session_ref == "cs_fake_1"
    assert result.url == "https://checkout.stripe.com/c/1"
    assert len(calls) == 2

    customer_req, checkout_req = calls
    assert customer_req.method == "POST"
    assert customer_req.url.path == "/v1/customers"
    assert customer_req.headers["Authorization"] == "Bearer test-secret-key"
    assert "Idempotency-Key" in customer_req.headers
    customer_form = _form(customer_req)
    assert customer_form["email"] == "person@example.com"
    assert customer_form["metadata[account_public_id]"] == "acc_00000000TESTACCT1"

    assert checkout_req.method == "POST"
    assert checkout_req.url.path == "/v1/checkout/sessions"
    assert "Idempotency-Key" in checkout_req.headers
    checkout_form = _form(checkout_req)
    assert checkout_form["mode"] == "subscription"
    assert checkout_form["customer"] == "cus_fake_new"
    assert checkout_form["client_reference_id"] == "acc_00000000TESTACCT1"
    assert checkout_form["line_items[0][price]"] == "price_test_pro"
    assert checkout_form["line_items[0][quantity]"] == "2"
    assert checkout_form["subscription_data[metadata][account_public_id]"] == "acc_00000000TESTACCT1"
    assert checkout_form["subscription_data[metadata][plan]"] == "pro"


def test_create_checkout_existing_customer_skips_customer_create() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "cs_fake_2", "url": "https://checkout.stripe.com/c/2"})

    result = make_adapter(transport=httpx.MockTransport(handler)).create_checkout(
        CheckoutRequest(
            account_public_id="acc_1",
            customer_email="x@example.com",
            plan="team",
            seats=1,
            success_url="https://example.com/s",
            cancel_url="https://example.com/c",
            billing_ref="cus_existing",
        )
    )
    assert len(calls) == 1
    assert result.billing_ref == "cus_existing"


def test_create_checkout_unknown_plan_rejected_before_any_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an unknown plan")

    adapter = make_adapter(transport=httpx.MockTransport(handler))
    with pytest.raises(SorRejected):
        adapter.create_checkout(
            CheckoutRequest(
                account_public_id="acc_1",
                customer_email="x@example.com",
                plan="doesnotexist",
                seats=1,
                success_url="https://example.com/s",
                cancel_url="https://example.com/c",
            )
        )


def test_open_portal_request_shape() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "bps_fake_1", "url": "https://billing.stripe.com/p/1"})

    session = make_adapter(transport=httpx.MockTransport(handler)).open_portal(
        billing_ref="cus_existing", return_url="https://example.com/account"
    )
    assert session.url == "https://billing.stripe.com/p/1"
    assert len(calls) == 1
    req = calls[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/billing_portal/sessions"
    assert "Idempotency-Key" in req.headers
    form = _form(req)
    assert form["customer"] == "cus_existing"
    assert form["return_url"] == "https://example.com/account"


def test_create_subscription_request_shape_and_mapping() -> None:
    sub_obj = load_fixture_json("subscription_object.json")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=sub_obj)

    state = make_adapter(transport=httpx.MockTransport(handler)).create_subscription(
        SubscriptionCreate(
            billing_ref="cus_QXtest0000000001",
            plan="pro",
            seats=3,
            trial_days=14,
            account_public_id="acc_00000000TESTACCT1",
            reason="pre-sold pilot",
        )
    )
    # The mirror is refreshed from the read-back, never from the request (US-902 AC2): the
    # returned state reflects `subscription_object.json`'s fixture content, not the seats/plan
    # passed in above (which happen to match here only because the fixture was written to match).
    assert state.sor_ref == sub_obj["id"]
    assert state.plan_tier == "pro"

    assert len(calls) == 1
    req = calls[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/subscriptions"
    assert "Idempotency-Key" in req.headers
    today = dt.datetime.now(dt.UTC).date().isoformat()
    assert req.headers["Idempotency-Key"] == f"subscription:cus_QXtest0000000001:pro:{today}"
    form = _form(req)
    assert form["customer"] == "cus_QXtest0000000001"
    assert form["items[0][price]"] == "price_test_pro"
    assert form["items[0][quantity]"] == "3"
    assert form["collection_method"] == "send_invoice"
    assert form["days_until_due"] == "30"
    assert form["trial_period_days"] == "14"
    assert form["metadata[account_public_id]"] == "acc_00000000TESTACCT1"
    assert form["metadata[plan]"] == "pro"


def test_create_subscription_without_trial_omits_trial_period_days() -> None:
    sub_obj = load_fixture_json("subscription_object.json")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=sub_obj)

    make_adapter(transport=httpx.MockTransport(handler)).create_subscription(
        SubscriptionCreate(billing_ref="cus_existing", plan="team", seats=1)
    )
    form = _form(calls[0])
    assert "trial_period_days" not in form
    assert "metadata[account_public_id]" not in form
    assert form["metadata[plan]"] == "team"


def test_create_subscription_unknown_plan_rejected_before_any_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for an unknown plan")

    adapter = make_adapter(transport=httpx.MockTransport(handler))
    with pytest.raises(SorRejected):
        adapter.create_subscription(SubscriptionCreate(billing_ref="cus_1", plan="doesnotexist", seats=1))


def test_create_subscription_4xx_raises_sor_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "Your card was declined."}})

    adapter = make_adapter(transport=httpx.MockTransport(handler))
    with pytest.raises(SorRejected):
        adapter.create_subscription(SubscriptionCreate(billing_ref="cus_1", plan="pro", seats=1))


def test_get_subscription_request_shape_and_mapping() -> None:
    sub_obj = load_fixture_json("subscription_object.json")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=sub_obj)

    state = make_adapter(transport=httpx.MockTransport(handler)).get_subscription(
        "sub_1NG8Du2eZvKYlo2Cq5f8Vsxu"
    )
    assert state is not None
    assert state.plan_tier == "pro"
    assert state.seats == 3
    assert state.mrr_amount == pytest.approx(147.0)
    assert state.currency == "USD"
    assert calls[0].method == "GET"
    assert calls[0].url.path == "/v1/subscriptions/sub_1NG8Du2eZvKYlo2Cq5f8Vsxu"


def test_get_subscription_not_found_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "No such subscription"}})

    adapter = make_adapter(transport=httpx.MockTransport(handler))
    assert adapter.get_subscription("sub_missing") is None


def test_get_subscription_yearly_price_divides_by_twelve() -> None:
    sub_obj = load_fixture_json("subscription_object.json")
    sub_obj["items"]["data"][0]["price"]["recurring"]["interval"] = "year"
    sub_obj["items"]["data"][0]["price"]["unit_amount"] = 120000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sub_obj)

    state = make_adapter(transport=httpx.MockTransport(handler)).get_subscription(sub_obj["id"])
    assert state is not None
    assert state.mrr_amount == pytest.approx(300.0)  # $1200 * 3 seats / 12 months


def test_get_subscription_plan_tier_from_price_id_when_no_metadata() -> None:
    sub_obj = load_fixture_json("subscription_object.json")
    del sub_obj["metadata"]["plan"]
    sub_obj["items"]["data"][0]["price"]["id"] = "price_test_team"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sub_obj)

    state = make_adapter(transport=httpx.MockTransport(handler)).get_subscription(sub_obj["id"])
    assert state is not None
    assert state.plan_tier == "team"


def test_get_subscription_plan_tier_defaults_to_pro_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    sub_obj = load_fixture_json("subscription_object.json")
    del sub_obj["metadata"]["plan"]
    sub_obj["items"]["data"][0]["price"]["id"] = "price_totally_unknown"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sub_obj)

    with caplog.at_level("WARNING"):
        state = make_adapter(transport=httpx.MockTransport(handler)).get_subscription(sub_obj["id"])
    assert state is not None
    assert state.plan_tier == "pro"
    assert any("no resolvable plan tier" in rec.message for rec in caplog.records)


def test_get_subscription_unmapped_status_fails_closed_to_canceled(caplog: pytest.LogCaptureFixture) -> None:
    sub_obj = load_fixture_json("subscription_object.json")
    sub_obj["status"] = "incomplete"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=sub_obj)

    with caplog.at_level("WARNING"):
        state = make_adapter(transport=httpx.MockTransport(handler)).get_subscription(sub_obj["id"])
    assert state is not None
    assert state.status == "canceled"


def test_list_invoices_request_shape_and_mapping() -> None:
    invoices_payload = load_fixture_json("invoice_list.json")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=invoices_payload)

    invoices = make_adapter(transport=httpx.MockTransport(handler)).list_invoices(
        billing_ref="cus_QXtest0000000001"
    )
    assert len(invoices) == 2
    assert invoices[0].ref == "in_test0000000001"
    assert invoices[0].amount_due == pytest.approx(49.0)
    assert invoices[0].currency == "USD"
    req = calls[0]
    assert req.method == "GET"
    assert req.url.path == "/v1/invoices"
    assert req.url.params["customer"] == "cus_QXtest0000000001"
    assert req.url.params["limit"] == "24"


# ----------------------------------------------------------------------------- retries and errors
def test_429_retries_then_succeeds() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) <= 2:
            return httpx.Response(
                429, headers={"Retry-After": "1"}, json={"error": {"message": "rate limited"}}
            )
        return httpx.Response(200, json={"id": "bps_fake", "url": "https://billing.stripe.com/p/ok"})

    sleeps: list[float] = []
    session = make_adapter(transport=httpx.MockTransport(handler), sleep=sleeps.append).open_portal(
        billing_ref="cus_1", return_url="https://x"
    )
    assert session.url == "https://billing.stripe.com/p/ok"
    assert len(attempts) == 3
    assert sleeps == [1.0, 1.0]


def test_429_exhausts_retries_then_sor_unavailable() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"message": "rate limited"}})

    sleeps: list[float] = []
    adapter = make_adapter(transport=httpx.MockTransport(handler), sleep=sleeps.append)
    with pytest.raises(SorUnavailable):
        adapter.open_portal(billing_ref="cus_1", return_url="https://x")
    assert len(attempts) == 4  # three retries (services/billing/README.md) plus the final attempt
    assert sleeps == [1.0, 1.0, 1.0]


def test_5xx_retries_once_then_succeeds() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(500, json={"error": {"message": "server error"}})
        return httpx.Response(200, json={"id": "bps_fake", "url": "https://billing.stripe.com/p/ok"})

    adapter = make_adapter(transport=httpx.MockTransport(handler))
    session = adapter.open_portal(billing_ref="cus_1", return_url="https://x")
    assert session.url == "https://billing.stripe.com/p/ok"
    assert len(attempts) == 2


def test_5xx_retries_once_then_sor_unavailable() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(500, json={"error": {"message": "server error"}})

    sleeps: list[float] = []
    adapter = make_adapter(transport=httpx.MockTransport(handler), sleep=sleeps.append)
    with pytest.raises(SorUnavailable):
        adapter.open_portal(billing_ref="cus_1", return_url="https://x")
    assert len(attempts) == 2
    assert sleeps == [1.0]


def test_4xx_other_than_429_raises_sor_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "Your card was declined."}})

    adapter = make_adapter(transport=httpx.MockTransport(handler))
    with pytest.raises(SorRejected):
        adapter.open_portal(billing_ref="cus_1", return_url="https://x")


def test_transport_error_retries_once_then_sor_unavailable() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    adapter = make_adapter(transport=httpx.MockTransport(handler))
    with pytest.raises(SorUnavailable):
        adapter.open_portal(billing_ref="cus_1", return_url="https://x")
    assert calls["n"] == 2
