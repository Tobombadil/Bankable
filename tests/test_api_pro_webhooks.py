"""Webhook endpoint HTTP surface: registration, listing, test/replay/deliveries (task brief item
4; docs/23-api-spec-outline.md §9.1, `/v1/webhooks*`)."""

from __future__ import annotations

import datetime as dt

from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from tests.conftest import login, make_account, make_api_key, make_user

UTC = dt.UTC


def test_create_webhook_requires_api_entitlement(client, db):
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    resp = client.post("/v1/webhooks", json={"url": "https://example.com/hook", "types": ["event.published"]})
    assert resp.status_code == 403


def test_create_webhook_via_session_shows_secret_once(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)

    resp = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hook", "types": ["event.published"], "entity": "proposal"},
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["secret"]
    assert data["url"] == "https://example.com/hook"

    listing = client.get("/v1/webhooks").json()["data"]
    assert all("secret" not in row for row in listing)


def test_create_webhook_via_api_key_with_write_scope(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["write:webhooks"])
    db.commit()

    resp = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hook", "types": ["event.published"]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert resp.status_code == 201


def test_create_webhook_rejects_non_https_url(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    resp = client.post(
        "/v1/webhooks", json={"url": "http://insecure.example.com/hook", "types": ["event.published"]}
    )
    assert resp.status_code == 400


def test_delete_and_get_webhook(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    created = client.post(
        "/v1/webhooks", json={"url": "https://example.com/hook", "types": ["event.published"]}
    ).json()["data"]
    whid = created["webhook_id"]

    assert client.get(f"/v1/webhooks/{whid}").status_code == 200
    assert client.delete(f"/v1/webhooks/{whid}").status_code == 204
    assert client.get(f"/v1/webhooks/{whid}").status_code == 404


def test_test_delivery_is_queued(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    created = client.post(
        "/v1/webhooks", json={"url": "https://example.com/hook", "types": ["event.published"]}
    ).json()["data"]
    whid = created["webhook_id"]

    resp = client.post(f"/v1/webhooks/{whid}/test")
    assert resp.status_code == 202
    body = resp.json()["data"]
    assert body["type"] == "webhook.test"
    assert body["status"] == "pending"

    deliveries = client.get(f"/v1/webhooks/{whid}/deliveries").json()["data"]
    assert len(deliveries) == 1
    assert deliveries[0]["delivery_id"] == body["delivery_id"]


def test_replay_queues_matching_events_since_a_seq(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)

    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    old_event = make_event(db, prop, src)
    db.flush()
    new_event = make_event(db, prop, src, event_type="status_change")
    db.commit()

    created = client.post(
        "/v1/webhooks",
        json={"url": "https://example.com/hook", "types": ["event.published"], "entity": "event"},
    ).json()["data"]
    whid = created["webhook_id"]

    resp = client.post(f"/v1/webhooks/{whid}/replay", params={"since": old_event.seq})
    assert resp.status_code == 202
    body = resp.json()["data"]
    assert body["queued_count"] == 1

    deliveries = client.get(f"/v1/webhooks/{whid}/deliveries").json()["data"]
    assert len(deliveries) == 1
    assert deliveries[0]["event_seq"] == new_event.seq


def test_replay_requires_since_parameter(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    created = client.post(
        "/v1/webhooks", json={"url": "https://example.com/hook", "types": ["event.published"]}
    ).json()["data"]
    resp = client.post(f"/v1/webhooks/{created['webhook_id']}/replay")
    assert resp.status_code == 400


def test_webhooks_are_scoped_to_their_own_account(client, db):
    account_a = make_account(db, entitlement="api", name="A")
    user_a = make_user(db, account_a, email="a@example.com")
    account_b = make_account(db, entitlement="api", name="B")
    user_b = make_user(db, account_b, email="b@example.com")
    db.commit()

    login(client, db, user_a)
    created = client.post(
        "/v1/webhooks", json={"url": "https://example.com/hook", "types": ["event.published"]}
    ).json()["data"]

    client.cookies.clear()
    login(client, db, user_b)
    assert client.get(f"/v1/webhooks/{created['webhook_id']}").status_code == 404
    assert client.get("/v1/webhooks").json()["data"] == []


def test_webhook_limit_per_account(client, db):
    from services.api.pro import MAX_WEBHOOKS_PER_ACCOUNT

    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    for i in range(MAX_WEBHOOKS_PER_ACCOUNT):
        resp = client.post(
            "/v1/webhooks", json={"url": f"https://example.com/hook{i}", "types": ["event.published"]}
        )
        assert resp.status_code == 201
    over = client.post(
        "/v1/webhooks", json={"url": "https://example.com/one-too-many", "types": ["event.published"]}
    )
    assert over.status_code == 403
