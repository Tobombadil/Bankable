"""API keys: creation, scopes, rotation, and the audit log (task brief item 1; docs/23-api-spec-
outline.md §5, §8; docs/04-standards.md S-4)."""

from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from services.api.app import app
from services.api.pro import API_LICENCE_VERSION, MAX_API_KEYS_PER_USER
from services.db.models import ApiKey, Event
from tests.conftest import login, make_account, make_api_key, make_user

UTC = dt.UTC


def _bearer_only_client() -> TestClient:
    """A `client` fixture logs in with a session cookie for the write calls a key-only endpoint
    (`createKey`, `revokeKey`) requires; `TestClient` persists cookies across requests, and a
    session takes precedence over a bearer token when both are present
    (`services/api/auth.py::build_auth_context`) — a real integration a stray session cookie
    should never mask. A second `TestClient` sharing the same app (and so the same
    `get_db` dependency override / in-memory database) but no cookie jar isolates the
    bearer-token-only assertions below."""
    return TestClient(app)


def test_create_key_requires_a_session_not_a_key(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live"])
    db.commit()
    resp = client.post(
        "/v1/keys",
        json={"name": "n", "licence_accepted_version": API_LICENCE_VERSION},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert resp.status_code == 401, "a key cannot mint keys (US-701, api/openapi.yaml createKey)"


def test_create_key_shows_the_secret_once_and_stores_only_its_hash(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)

    resp = client.post(
        "/v1/keys", json={"name": "production-etl", "licence_accepted_version": API_LICENCE_VERSION}
    )
    assert resp.status_code == 201
    body = resp.json()["data"]
    assert body["secret"].startswith("bk_live_")
    assert body["prefix"] == "bk_live"
    assert body["last4"] == body["secret"][-4:]

    stored = db.query(ApiKey).filter_by(public_id=body["key_id"]).one()
    assert stored.key_hash != body["secret"]

    listing = client.get("/v1/keys").json()["data"]
    assert all("secret" not in row for row in listing), "the secret is never returned again"


def test_create_key_rejects_wrong_licence_version(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    resp = client.post("/v1/keys", json={"name": "n", "licence_accepted_version": "api-licence-0.1"})
    assert resp.status_code == 400


def test_create_key_rejects_admin_scope(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    resp = client.post(
        "/v1/keys",
        json={
            "name": "n",
            "licence_accepted_version": API_LICENCE_VERSION,
            "scopes": ["admin:*"],
        },
    )
    assert resp.status_code == 400


def test_create_key_enforces_the_per_account_limit(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    for i in range(MAX_API_KEYS_PER_USER):
        resp = client.post(
            "/v1/keys", json={"name": f"key-{i}", "licence_accepted_version": API_LICENCE_VERSION}
        )
        assert resp.status_code == 201
    over_limit = client.post(
        "/v1/keys", json={"name": "one-too-many", "licence_accepted_version": API_LICENCE_VERSION}
    )
    assert over_limit.status_code == 403
    assert over_limit.json()["code"] == "forbidden_tier"


def test_revoke_key_is_immediate_and_audited(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)

    created = client.post(
        "/v1/keys",
        json={
            "name": "to-revoke",
            "licence_accepted_version": API_LICENCE_VERSION,
            "scopes": ["read:live"],
        },
    ).json()["data"]
    secret = created["secret"]
    key_id = created["key_id"]

    bearer = _bearer_only_client()
    # Works before revocation.
    assert bearer.get("/v1/saved-searches", headers={"Authorization": f"Bearer {secret}"}).status_code == 200

    revoke = client.delete(f"/v1/keys/{key_id}")
    assert revoke.status_code == 204

    assert bearer.get("/v1/saved-searches", headers={"Authorization": f"Bearer {secret}"}).status_code == 401

    events = db.query(Event).filter_by(subject_type="api_key", event_type="key_revoked").all()
    assert len(events) == 1
    assert events[0].actor_type == "user"
    assert events[0].reason


def test_key_issued_writes_an_audit_event(client, db):
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    client.post("/v1/keys", json={"name": "audited-key", "licence_accepted_version": API_LICENCE_VERSION})

    events = db.query(Event).filter_by(subject_type="api_key", event_type="key_issued").all()
    assert len(events) == 1
    assert events[0].actor_user_id == user.id
    assert events[0].after["scopes"] == ["read:public"]


def test_rotation_is_revoke_then_create_and_only_the_new_secret_works(client, db):
    """No dedicated `/rotate` endpoint exists in `api/openapi.yaml` (only `createKey` and
    `revokeKey`, task brief note in services/README.md); rotation is this two-call workflow,
    exercised end to end here."""
    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)

    old = client.post(
        "/v1/keys",
        json={
            "name": "rotate-me",
            "licence_accepted_version": API_LICENCE_VERSION,
            "scopes": ["read:live"],
        },
    ).json()["data"]
    old_secret, old_id = old["secret"], old["key_id"]

    new = client.post(
        "/v1/keys",
        json={
            "name": "rotate-me",
            "licence_accepted_version": API_LICENCE_VERSION,
            "scopes": ["read:live"],
        },
    ).json()["data"]
    new_secret = new["secret"]
    assert new_secret != old_secret

    revoke_resp = client.delete(f"/v1/keys/{old_id}")
    assert revoke_resp.status_code == 204

    bearer = _bearer_only_client()
    assert (
        bearer.get("/v1/saved-searches", headers={"Authorization": f"Bearer {old_secret}"}).status_code == 401
    )
    assert (
        bearer.get("/v1/saved-searches", headers={"Authorization": f"Bearer {new_secret}"}).status_code == 200
    )


def test_keys_are_scoped_to_their_own_account(client, db):
    account_a = make_account(db, entitlement="api", name="Account A")
    user_a = make_user(db, account_a, email="a@example.com")
    account_b = make_account(db, entitlement="api", name="Account B")
    user_b = make_user(db, account_b, email="b@example.com")
    db.commit()

    login(client, db, user_a)
    key = client.post(
        "/v1/keys", json={"name": "a-key", "licence_accepted_version": API_LICENCE_VERSION}
    ).json()["data"]

    client.cookies.clear()
    login(client, db, user_b)
    resp = client.delete(f"/v1/keys/{key['key_id']}")
    assert resp.status_code == 404, "account B must not be able to revoke account A's key"
