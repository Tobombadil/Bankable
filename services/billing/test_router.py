"""Router tests for `services/billing/router.py`: checkout, portal, the inbound webhook, and the
admin subscription list — through the standalone test app `services/billing/conftest.py` builds
(billing routes plus a read-only-imported `services.api.pro.router`, just for the one test that
checks `/v1/me` after a webhook).
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.billing.fake import InMemoryBilling
from services.sor.ports import SorRejected, SorUnavailable
from tests.conftest import login, make_account, make_user
from tests.test_api_contract import assert_valid

UTC = dt.UTC


def _signed_up_user(db: Session, client: TestClient, *, email: str = "buyer@example.com"):
    account = make_account(db, entitlement="public", name="Buyer Co")
    user = make_user(db, account, email=email)
    db.commit()
    login(client, db, user)
    return account, user


# ------------------------------------------------------------------------------------- checkout
def test_checkout_requires_session(client: TestClient) -> None:
    resp = client.post("/v1/billing/checkout", json={"plan": "pro", "seats": 1})
    assert resp.status_code == 401


def test_checkout_rejects_bad_plan(client: TestClient, db: Session) -> None:
    _signed_up_user(db, client)
    resp = client.post("/v1/billing/checkout", json={"plan": "not-a-real-plan", "seats": 1})
    assert resp.status_code == 400


def test_checkout_rejects_bad_seats(client: TestClient, db: Session) -> None:
    _signed_up_user(db, client)
    resp = client.post("/v1/billing/checkout", json={"plan": "pro", "seats": 0})
    assert resp.status_code == 400


def test_checkout_conflict_when_user_has_no_email(
    client: TestClient, db: Session, billing_port: InMemoryBilling
) -> None:
    """Never fabricate an email for a third-party system (Stripe emails receipts/dunning to it):
    a user with no email on file cannot check out at all."""
    account = make_account(db, entitlement="public", name="No Email Co")
    user = make_user(db, account, email="placeholder@example.com")
    user.email = None  # tests.conftest.make_user's `email` param is typed `str`; set it directly
    db.commit()
    login(client, db, user)

    resp = client.post("/v1/billing/checkout", json={"plan": "pro", "seats": 1})
    assert resp.status_code == 409
    assert resp.json()["title"] == "An email address is required before checkout"
    assert billing_port.checkouts == []


def test_checkout_201_with_url_and_persists_billing_ref(
    client: TestClient, db: Session, billing_port: InMemoryBilling
) -> None:
    account, _user = _signed_up_user(db, client)
    resp = client.post("/v1/billing/checkout", json={"plan": "pro", "seats": 2})
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert body["url"].startswith("https://checkout.stripe.invalid/")
    assert body["session_ref"]

    assert len(billing_port.checkouts) == 1
    recorded = billing_port.checkouts[0]
    assert recorded.request.account_public_id == account.public_id
    assert recorded.request.plan == "pro"
    assert recorded.request.seats == 2

    db.expire_all()
    refreshed = db.get(type(account), account.id)
    assert refreshed is not None
    assert refreshed.billing_ref == recorded.billing_ref


# --------------------------------------------------------------------------------------- portal
def test_portal_conflict_before_checkout(client: TestClient, db: Session) -> None:
    _signed_up_user(db, client)
    resp = client.post("/v1/billing/portal")
    assert resp.status_code == 409


def test_portal_200_after_checkout(client: TestClient, db: Session) -> None:
    account, _user = _signed_up_user(db, client)
    account.billing_ref = "cus_already_a_customer"
    db.commit()
    resp = client.post("/v1/billing/portal")
    assert resp.status_code == 200
    assert resp.json()["data"]["url"].startswith("https://portal.stripe.invalid/")


# -------------------------------------------------------------------------------------- webhook
def test_webhook_bad_signature_is_401(client: TestClient, billing_port: InMemoryBilling) -> None:
    body = b'{"id": "evt_bad", "type": "customer.subscription.updated", "data": {"object": {}}}'
    resp = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=deadbeef"})
    assert resp.status_code == 401


def test_webhook_applies_and_flips_entitlement_then_me_shows_new_tier(
    client: TestClient, db: Session, billing_port: InMemoryBilling
) -> None:
    account, _user = _signed_up_user(db, client, email="upgrader@example.com")
    body_obj = {
        "id": "evt_router_test_1",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_router_test_1",
                "customer": "cus_router_test_1",
                "status": "active",
                "plan_tier": "pro",
                "seats": 4,
                "currency": "USD",
                "current_period_start": "2026-09-01T00:00:00Z",
                "current_period_end": "2026-10-01T00:00:00Z",
                "account_public_id": account.public_id,
            }
        },
    }
    body = json.dumps(body_obj).encode()
    header = billing_port.sign(body)
    resp = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": header})
    assert resp.status_code == 200
    assert resp.json() == {"received": 1, "applied": 1}

    me = client.get("/v1/me")
    assert me.status_code == 200
    assert me.json()["data"]["tier"] == "pro"
    assert me.json()["data"]["account"]["entitlement"] == "pro"
    assert me.json()["data"]["account"]["seats"] == 4


def test_webhook_never_5xx_when_change_application_fails(
    client: TestClient, billing_port: InMemoryBilling, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-change failure is logged and counted, never a 500 on the whole delivery."""
    import services.billing.router as router_module

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(router_module, "apply_entitlement_change", _boom)

    body_obj = {
        "id": "evt_router_test_2",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_router_test_2",
                "customer": "cus_router_test_2",
                "status": "active",
                "plan_tier": "pro",
                "seats": 1,
                "current_period_start": "2026-09-01T00:00:00Z",
                "current_period_end": "2026-10-01T00:00:00Z",
            }
        },
    }
    body = json.dumps(body_obj).encode()
    header = billing_port.sign(body)
    resp = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": header})
    assert resp.status_code == 200
    assert resp.json() == {"received": 1, "applied": 0}


# --------------------------------------------------------------------------- admin subscriptions
def test_admin_subscriptions_requires_operator(client: TestClient, db: Session) -> None:
    account = make_account(db, entitlement="public", name="Non admin")
    user = make_user(db, account, email="viewer@example.com", role="viewer")
    db.commit()
    login(client, db, user)
    resp = client.get("/admin/v1/subscriptions")
    assert resp.status_code == 403


def test_admin_subscriptions_lists_mirror_rows_matching_the_spec(
    client: TestClient, db: Session, billing_port: InMemoryBilling, spec: dict
) -> None:
    account, _user = _signed_up_user(db, client, email="customer@example.com")

    body_obj = {
        "id": "evt_router_test_admin",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_router_test_admin",
                "customer": "cus_router_test_admin",
                "status": "active",
                "plan_tier": "pro",
                "seats": 2,
                "currency": "USD",
                "current_period_start": "2026-09-01T00:00:00Z",
                "current_period_end": "2026-10-01T00:00:00Z",
                "account_public_id": account.public_id,
            }
        },
    }
    body = json.dumps(body_obj).encode()
    header = billing_port.sign(body)
    webhook_resp = client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": header})
    assert webhook_resp.status_code == 200

    operator_account = make_account(db, entitlement="admin", name="Ops")
    operator = make_user(db, operator_account, email="ops@example.com", role="operator")
    db.commit()
    login(client, db, operator)

    resp = client.get("/admin/v1/subscriptions")
    assert resp.status_code == 200
    body_json = resp.json()
    assert_valid(spec, "SubscriptionListResponse", body_json)
    rows = body_json["data"]
    assert any(row["account_id"] == account.public_id and row["plan_tier"] == "pro" for row in rows)

    filtered = client.get("/admin/v1/subscriptions", params={"account_id": account.public_id})
    assert filtered.status_code == 200
    assert len(filtered.json()["data"]) == 1

    filtered_status = client.get("/admin/v1/subscriptions", params={"status": "canceled"})
    assert filtered_status.status_code == 200
    assert filtered_status.json()["data"] == []

    filtered_plan = client.get("/admin/v1/subscriptions", params={"plan_tier": "pro"})
    assert filtered_plan.status_code == 200
    assert len(filtered_plan.json()["data"]) == 1

    filtered_unknown_account = client.get(
        "/admin/v1/subscriptions", params={"account_id": "acc_doesnotexist0000000000"}
    )
    assert filtered_unknown_account.status_code == 200
    assert filtered_unknown_account.json()["data"] == []


# --------------------------------------------------------------------- adapter failure surfacing
class _UnavailablePort:
    sor_kind = "stripe"

    def create_checkout(self, request: object) -> None:
        raise SorUnavailable("stripe is down")

    def open_portal(self, *, billing_ref: str, return_url: str) -> None:
        raise SorUnavailable("stripe is down")


class _RejectingPort:
    sor_kind = "stripe"

    def create_checkout(self, request: object) -> None:
        raise SorRejected("stripe said no")

    def open_portal(self, *, billing_ref: str, return_url: str) -> None:
        raise SorRejected("stripe said no")


def test_checkout_503_when_billing_provider_unavailable(client: TestClient, db: Session) -> None:
    from services.sor.wiring import get_billing_port

    app = client.app  # type: ignore[attr-defined]
    _signed_up_user(db, client)
    app.dependency_overrides[get_billing_port] = lambda: _UnavailablePort()
    try:
        resp = client.post("/v1/billing/checkout", json={"plan": "pro", "seats": 1})
    finally:
        del app.dependency_overrides[get_billing_port]
    assert resp.status_code == 503
    assert "stripe is down" not in resp.text
    assert resp.json()["detail"] == "The billing provider could not be reached; try again shortly."


def test_checkout_409_when_billing_provider_rejects(client: TestClient, db: Session) -> None:
    from services.sor.wiring import get_billing_port

    app = client.app  # type: ignore[attr-defined]
    _signed_up_user(db, client)
    app.dependency_overrides[get_billing_port] = lambda: _RejectingPort()
    try:
        resp = client.post("/v1/billing/checkout", json={"plan": "pro", "seats": 1})
    finally:
        del app.dependency_overrides[get_billing_port]
    assert resp.status_code == 409
    assert "stripe said no" not in resp.text
    assert resp.json()["detail"] == "The billing provider rejected the request."


def test_portal_503_when_billing_provider_unavailable(client: TestClient, db: Session) -> None:
    from services.sor.wiring import get_billing_port

    app = client.app  # type: ignore[attr-defined]
    account, _user = _signed_up_user(db, client)
    account.billing_ref = "cus_existing"
    db.commit()
    app.dependency_overrides[get_billing_port] = lambda: _UnavailablePort()
    try:
        resp = client.post("/v1/billing/portal")
    finally:
        del app.dependency_overrides[get_billing_port]
    assert resp.status_code == 503
    assert "stripe is down" not in resp.text
    assert resp.json()["detail"] == "The billing provider could not be reached; try again shortly."


def test_portal_409_when_billing_provider_rejects(client: TestClient, db: Session) -> None:
    from services.sor.wiring import get_billing_port

    app = client.app  # type: ignore[attr-defined]
    account, _user = _signed_up_user(db, client)
    account.billing_ref = "cus_existing"
    db.commit()
    app.dependency_overrides[get_billing_port] = lambda: _RejectingPort()
    try:
        resp = client.post("/v1/billing/portal")
    finally:
        del app.dependency_overrides[get_billing_port]
    assert resp.status_code == 409
    assert "stripe said no" not in resp.text
    assert resp.json()["detail"] == "The billing provider rejected the request."


def test_subscriptions_for_account_helper(db: Session, billing_port: InMemoryBilling) -> None:
    """Unit test for the coordinator's `GET /v1/account` wiring point, independent of any route."""
    from services.billing.entitlement import apply_entitlement_change
    from services.billing.router import subscriptions_for_account
    from services.sor.ports import EntitlementChange, SubscriptionState, entitlement_for

    account = make_account(db, entitlement="public", name="Direct Co")
    account.billing_ref = "cus_direct_1"
    db.commit()

    now = dt.datetime.now(UTC)
    state = SubscriptionState(
        sor_kind="stripe",
        sor_ref="sub_direct_1",
        billing_ref="cus_direct_1",
        plan_code="pro_monthly",
        plan_tier="pro",
        status="active",
        seats=1,
        current_period_start=now,
        current_period_end=now + dt.timedelta(days=30),
        currency="USD",
    )
    change = EntitlementChange(
        event_ref="evt_direct_1",
        occurred_at=now,
        billing_ref=state.billing_ref,
        subscription=state,
        entitlement=entitlement_for(state.plan_tier, state.status),
    )
    apply_entitlement_change(db, change)

    rows = subscriptions_for_account(db, account)
    assert len(rows) == 1
    assert rows[0]["sor_ref"] == "sub_direct_1"
    assert rows[0]["account_id"] == account.public_id
