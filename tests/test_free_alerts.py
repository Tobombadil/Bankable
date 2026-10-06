"""Free capped email alerts under the noncommercial posture (owner decision 2026-09-30;
`services/api/alert_plan.py`; docs/26 §7).

The posture is switched the way `services/billing/test_router.py` switches checkout: by setting
`services.billing.router.PAID_TIERS_ACTIVE`, the one constant both read. Under `commercial` (the
default) every assertion here is the behaviour `tests/test_paid_shapes_are_gated.py` pins.
"""

from __future__ import annotations

import datetime as dt

import pytest

from services.api import alert_plan
from services.api.alert_plan import FREE_ALERT_CAP_DEFAULT, parse_free_alert_cap
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.billing import router as billing_router
from services.db.models import SavedSearch, UiEvent
from tests.conftest import login, make_account, make_api_key, make_user

UTC = dt.UTC


@pytest.fixture()
def noncommercial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(billing_router, "PAID_TIERS_ACTIVE", False)


def _free_user(client, db, *, verified: bool = True, email: str = "reader@example.com"):
    account = make_account(db, entitlement="public", name=email)
    user = make_user(db, account, email=email)
    if verified:
        user.email_verified_at = dt.datetime.now(UTC)
    db.commit()
    login(client, db, user)
    return account, user


def _create(client, name: str = "TX storage", **extra):
    body = {"name": name, "entity": "proposal", "query": {"kind": "storage", "jurisdiction": "US-TX"}}
    body.update(extra)
    return client.post("/v1/saved-searches", json=body)


# ------------------------------------------------------------------------------ commercial: as before
def test_under_commercial_a_free_account_still_has_no_alerts(client, db) -> None:
    _free_user(client, db)
    assert alert_plan.free_alerts_active() is False
    resp = _create(client)
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden_tier"
    assert resp.json()["detail"] == "This operation needs at least the 'pro' entitlement."
    me = client.get("/v1/me").json()["data"]
    assert me["alert_plan"] is None
    assert me["saved_search_quota"]["limit"] == alert_plan.SAVED_SEARCH_QUOTA
    assert client.get("/v1/health").json()["free_alerts"]["active"] is False


def test_under_commercial_a_pro_account_holds_the_paid_plan(client, db) -> None:
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    plan = client.get("/v1/me").json()["data"]["alert_plan"]
    assert plan == {
        "basis": "paid",
        "quota": 25,
        "delivery_modes": ["immediate", "daily", "weekly"],
        "channels": ["email", "rss"],
        "requires_verified_email": False,
    }
    assert _create(client, delivery_mode="immediate").status_code == 201


# --------------------------------------------------------------------------- noncommercial: free plan
def test_a_verified_free_account_creates_lists_pauses_resumes_and_deletes(client, db, noncommercial) -> None:
    _free_user(client, db)
    created = _create(client)
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["delivery_mode"] == "daily" and data["channels"] == ["email"]
    sid = data["saved_search_id"]

    listing = client.get("/v1/saved-searches").json()["data"]
    assert [s["saved_search_id"] for s in listing] == [sid]

    paused = client.patch(f"/v1/saved-searches/{sid}", json={"status": "paused"})
    assert paused.status_code == 200 and paused.json()["data"]["status"] == "paused"
    resumed = client.patch(f"/v1/saved-searches/{sid}", json={"status": "active"})
    assert resumed.json()["data"]["status"] == "active"
    weekly = client.patch(f"/v1/saved-searches/{sid}", json={"delivery_mode": "weekly"})
    assert weekly.status_code == 200 and weekly.json()["data"]["delivery_mode"] == "weekly"

    assert client.delete(f"/v1/saved-searches/{sid}").status_code == 204
    assert client.get("/v1/saved-searches").json()["data"] == []
    assert client.get("/v1/alerts").status_code == 200


def test_me_and_health_report_the_free_plan(client, db, noncommercial) -> None:
    _free_user(client, db)
    me = client.get("/v1/me").json()["data"]
    assert me["alert_plan"] == {
        "basis": "free",
        "quota": FREE_ALERT_CAP_DEFAULT,
        "delivery_modes": ["daily", "weekly"],
        "channels": ["email"],
        "requires_verified_email": True,
    }
    assert me["saved_search_quota"] == {"limit": FREE_ALERT_CAP_DEFAULT, "used": 0}
    assert client.get("/v1/health").json()["free_alerts"] == {
        "active": True,
        "cap": FREE_ALERT_CAP_DEFAULT,
        "delivery_modes": ["daily", "weekly"],
    }


def test_an_unverified_free_account_cannot_create_but_can_read(client, db, noncommercial) -> None:
    _free_user(client, db, verified=False)
    resp = _create(client)
    assert resp.status_code == 403
    assert resp.json()["title"] == "Verify your email address first"
    assert client.get("/v1/saved-searches").status_code == 200


def test_the_cap_is_enforced_server_side_with_a_clear_error(client, db, noncommercial, monkeypatch) -> None:
    monkeypatch.setattr(alert_plan, "FREE_ALERT_CAP", 3)
    _free_user(client, db)
    for i in range(3):
        assert _create(client, name=f"search {i}").status_code == 201
    sid = client.get("/v1/saved-searches").json()["data"][0]["saved_search_id"]
    client.patch(f"/v1/saved-searches/{sid}", json={"status": "paused"})
    over = _create(client, name="one too many")
    assert over.status_code == 403
    body = over.json()
    assert body["code"] == "forbidden_tier"
    assert body["title"] == "Free alert limit reached"
    assert "up to 3 alerts" in body["detail"] and "paused alert still counts" in body["detail"]
    assert client.get("/v1/me").json()["data"]["saved_search_quota"] == {"limit": 3, "used": 3}


def test_immediate_delivery_and_the_rss_channel_stay_paid(client, db, noncommercial) -> None:
    _free_user(client, db)
    immediate = _create(client, delivery_mode="immediate")
    assert immediate.status_code == 403
    assert immediate.json()["errors"][0]["field"] == "delivery_mode"
    rss = _create(client, name="rss", channels=["email", "rss"])
    assert rss.status_code == 403
    assert rss.json()["errors"][0]["field"] == "channels"

    sid = _create(client, name="ok").json()["data"]["saved_search_id"]
    patched = client.patch(f"/v1/saved-searches/{sid}", json={"delivery_mode": "immediate"})
    assert patched.status_code == 403
    assert client.patch(f"/v1/saved-searches/{sid}", json={"channels": ["rss"]}).status_code == 403


def test_a_pro_account_keeps_the_paid_plan_under_noncommercial(client, db, noncommercial) -> None:
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    assert client.get("/v1/me").json()["data"]["alert_plan"]["basis"] == "paid"
    assert _create(client, delivery_mode="immediate", channels=["email", "rss"]).status_code == 201


def test_a_public_api_key_gets_no_free_plan(client, db, noncommercial) -> None:
    """The free plan is a signed-in reader's, not the API tier's: a `read:public` key is refused."""
    account = make_account(db, entitlement="public")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:public"])
    db.commit()
    resp = client.get("/v1/saved-searches", headers={"Authorization": f"Bearer {secret}"})
    assert resp.status_code == 403


def test_anonymous_is_still_401(client, noncommercial) -> None:
    assert client.get("/v1/saved-searches").status_code == 401
    assert client.post("/v1/saved-searches", json={}).status_code == 401


def test_vocabulary_errors_and_duplicate_names(client, db, noncommercial) -> None:
    _free_user(client, db)
    bad = _create(client, delivery_mode="hourly")
    assert bad.status_code == 400 and bad.json()["code"] == "validation_error"
    assert _create(client, name="same").status_code == 201
    dup = _create(client, name="same")
    assert dup.status_code == 409 and dup.json()["code"] == "conflict"
    sid = client.get("/v1/saved-searches").json()["data"][0]["saved_search_id"]
    assert client.patch(f"/v1/saved-searches/{sid}", json={"status": "gone"}).status_code == 400


def test_a_new_alert_starts_at_the_head_of_the_event_log(client, db, noncommercial) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src)
    event = make_event(db, prop, src)
    db.commit()
    _free_user(client, db)
    data = _create(client).json()["data"]
    assert data["watermark_seq"] == event.seq


def test_creating_an_alert_is_counted_without_identifiers(client, db, noncommercial) -> None:
    _free_user(client, db)
    _create(client)
    rows = db.query(UiEvent).filter(UiEvent.name == "alert.created").all()
    assert len(rows) == 1 and rows[0].props == {}
    assert db.query(SavedSearch).count() == 1


def test_parse_free_alert_cap() -> None:
    assert parse_free_alert_cap(None) == FREE_ALERT_CAP_DEFAULT
    assert parse_free_alert_cap("") == FREE_ALERT_CAP_DEFAULT
    assert parse_free_alert_cap(" 25 ") == 25
    assert parse_free_alert_cap("0") == 0
    assert parse_free_alert_cap("-1") == FREE_ALERT_CAP_DEFAULT
    assert parse_free_alert_cap("ten") == FREE_ALERT_CAP_DEFAULT


# --------------------------------------------------------------------------------------- seat rule
def _login_via_api(client, email: str):
    return client.post("/v1/auth/login", json={"email": email, "password": "correct horse battery staple"})


@pytest.mark.parametrize("posture_paid_active", [True, False])
def test_a_free_account_can_sign_in_on_a_second_device(client, db, monkeypatch, posture_paid_active) -> None:
    monkeypatch.setattr(billing_router, "PAID_TIERS_ACTIVE", posture_paid_active)
    account = make_account(db, entitlement="public")
    make_user(db, account, email="two.devices@example.com")
    db.commit()
    assert _login_via_api(client, "two.devices@example.com").status_code == 200
    client.cookies.clear()
    second = _login_via_api(client, "two.devices@example.com")
    assert second.status_code == 200, second.text


@pytest.mark.parametrize("posture_paid_active", [True, False])
def test_a_paid_account_is_still_held_to_its_seats(client, db, monkeypatch, posture_paid_active) -> None:
    monkeypatch.setattr(billing_router, "PAID_TIERS_ACTIVE", posture_paid_active)
    account = make_account(db, entitlement="pro")
    make_user(db, account, email="one.seat@example.com")
    db.commit()
    assert _login_via_api(client, "one.seat@example.com").status_code == 200
    client.cookies.clear()
    second = _login_via_api(client, "one.seat@example.com")
    assert second.status_code == 403 and second.json()["code"] == "seat_limit"


# ------------------------------------------------------------------------------------- feed titles
def test_feed_title_points_to_free_alerts_only_under_noncommercial(monkeypatch) -> None:
    from services.api.common import WEB_HOST
    from services.api.feeds import feed_title

    assert feed_title("Proposals", "proposal").endswith("alerts and API in Pro")
    monkeypatch.setattr(billing_router, "PAID_TIERS_ACTIVE", False)
    title = feed_title("Proposals", "proposal")
    assert title == f"Proposals — Public feed, live — free email alerts at {WEB_HOST}/alerts"
    assert "API" not in title and "in Pro" not in title
