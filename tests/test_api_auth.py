"""`/v1/auth/*` (Sprint 3 "login and registration surface", first wave):
`services/api/auth_routes.py` mounted onto the shared `services.api.app.app` singleton the same
way `services/api/app.py` mounts `services/api/pro.py` — this module does the `include_router`
call itself (once, at import time) since editing `services/api/app.py` is out of this task's
writable scope; the coordinator's real mount is the identical one-line call.

Reuses `tests/conftest.py`'s DB/TestClient/rate-limiter fixtures and `make_account`/`make_user`/
`login` factories (the same fixtures `tests/test_api_pro_auth.py` uses) rather than
`services/api/conftest.py`'s, since those are scoped to `services/api/` by pytest's conftest
discovery rules and this file lives under `tests/`.
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qs, urlsplit

import pytest

from services.api.app import app
from services.api.auth import ResendEmailAdapter, make_verification_token
from services.api.auth_routes import get_email_port
from services.api.auth_routes import router as auth_router
from tests.conftest import login, make_account, make_user

UTC = dt.UTC

# Guarded rather than unconditional: `services.api.app.app` is one process-wide singleton and
# `web/test_auth.py` mounts this same router onto it too (both are needed since this file's
# `client` fixture and that file's in-process `ApiClient` both talk to the one `app` object) --
# whichever of the two test modules pytest imports first performs the real registration.
if not any(getattr(r, "path", None) == "/v1/auth/register" for r in app.routes):
    app.include_router(auth_router)


@pytest.fixture()
def email_port():
    """Overrides the process-wide `EmailPort` dependency with a fresh dry-run adapter per test, so
    `.sent` reflects only what this test triggered (mirrors `services/api/auth.py`'s own
    `ResendEmailAdapter` dry-run tests, `tests/test_api_pro_auth.py`)."""
    port = ResendEmailAdapter()
    app.dependency_overrides[get_email_port] = lambda: port
    yield port
    app.dependency_overrides.pop(get_email_port, None)


def _token_from_dev_url(url: str) -> str:
    return parse_qs(urlsplit(url).query)["token"][0]


# -------------------------------------------------------------------------------------- register
def test_register_creates_session_cookie_and_v1_me_succeeds(client, db, email_port):
    resp = client.post(
        "/v1/auth/register", json={"email": "ana@example.com", "password": "correct horse battery"}
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["email_verified"] is False
    assert data["verification_sent"] is True
    assert data["dev_verification_url"]  # dry-run: no RESEND_API_KEY in this environment
    assert data["user"]["email"] == "ana@example.com"

    cookie = resp.cookies.get("session")
    assert cookie
    me = client.get("/v1/me", cookies={"session": cookie})
    assert me.status_code == 200
    assert me.json()["data"]["user"]["user_id"] == data["user"]["user_id"]

    assert len(email_port.sent) == 1
    assert email_port.sent[0].to == "ana@example.com"


def test_register_duplicate_active_email_is_409(client, db, email_port):
    first = client.post(
        "/v1/auth/register", json={"email": "dup@example.com", "password": "correct horse battery"}
    )
    assert first.status_code == 201

    dup = client.post(
        "/v1/auth/register", json={"email": "DUP@example.com", "password": "another-long-password"}
    )
    assert dup.status_code == 409
    assert dup.json()["code"] == "conflict"


def test_register_weak_password_is_400_with_field_error(client, db):
    resp = client.post("/v1/auth/register", json={"email": "weak@example.com", "password": "short"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "validation_error"
    assert {e["field"] for e in body["errors"]} == {"password"}


def test_register_invalid_email_is_400_with_field_error(client, db):
    resp = client.post(
        "/v1/auth/register", json={"email": "not-an-email", "password": "correct horse battery"}
    )
    assert resp.status_code == 400
    fields = {e["field"] for e in resp.json()["errors"]}
    assert "email" in fields


def test_dev_verification_url_token_verifies(client, db, email_port):
    resp = client.post(
        "/v1/auth/register", json={"email": "ivan@example.com", "password": "correct horse battery"}
    )
    token = _token_from_dev_url(resp.json()["data"]["dev_verification_url"])
    verify_resp = client.get(f"/v1/auth/verify?token={token}")
    assert verify_resp.status_code == 200
    assert verify_resp.json() == {"verified": True}


def test_register_rate_limited_after_ten_per_ip(client, db, email_port):
    for i in range(10):
        resp = client.post(
            "/v1/auth/register", json={"email": f"user{i}@example.com", "password": "correct horse battery"}
        )
        assert resp.status_code == 201
    eleventh = client.post(
        "/v1/auth/register", json={"email": "user10@example.com", "password": "correct horse battery"}
    )
    assert eleventh.status_code == 429
    assert eleventh.json()["code"] == "rate_limited"


# ----------------------------------------------------------------------------------------- login
def test_login_wrong_password_is_401(client, db):
    account = make_account(db, entitlement="public")
    make_user(db, account, email="bob@example.com", password="correct horse battery staple")
    db.commit()
    resp = client.post("/v1/auth/login", json={"email": "bob@example.com", "password": "totally-wrong"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthenticated"


def test_login_ok_sets_cookie_and_returns_identity(client, db):
    account = make_account(db, entitlement="public")
    make_user(db, account, email="carol@example.com", password="correct horse battery staple")
    db.commit()
    resp = client.post(
        "/v1/auth/login", json={"email": "carol@example.com", "password": "correct horse battery staple"}
    )
    assert resp.status_code == 200
    assert resp.cookies.get("session")
    data = resp.json()["data"]
    assert data["user"]["email"] == "carol@example.com"
    assert data["email_verified"] is False


def test_login_above_seat_count_is_403_seat_limit(client, db):
    account = make_account(db, entitlement="pro")  # seats defaults to 1
    user = make_user(db, account, email="dora@example.com")
    db.commit()
    login(client, db, user)  # one already-active session; seats == 1

    resp = client.post(
        "/v1/auth/login", json={"email": "dora@example.com", "password": "correct horse battery staple"}
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "seat_limit"


# ---------------------------------------------------------------------------------------- logout
def test_logout_revokes_session_then_v1_me_is_401(client, db):
    account = make_account(db, entitlement="public")
    user = make_user(db, account, email="erin@example.com")
    db.commit()
    login(client, db, user)

    resp = client.post("/v1/auth/logout")
    assert resp.status_code == 204

    me = client.get("/v1/me")
    assert me.status_code == 401


def test_logout_without_a_cookie_is_401(client, db):
    resp = client.post("/v1/auth/logout")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------------------- verify
def test_verify_flips_email_verified_at_and_is_idempotent(client, db):
    account = make_account(db, entitlement="public")
    user = make_user(db, account, email="frank@example.com")
    db.commit()
    token = make_verification_token(user)

    resp = client.get(f"/v1/auth/verify?token={token}")
    assert resp.status_code == 200
    assert resp.json() == {"verified": True}
    db.refresh(user)
    assert user.email_verified_at is not None
    first_verified_at = user.email_verified_at

    again = client.get(f"/v1/auth/verify?token={token}")
    assert again.status_code == 200
    db.refresh(user)
    assert user.email_verified_at == first_verified_at  # idempotent, not bumped a second time


def test_verify_expired_token_is_400(client, db, monkeypatch):
    account = make_account(db, entitlement="public")
    user = make_user(db, account, email="gina@example.com")
    db.commit()

    import services.api.auth as auth_module

    token = auth_module.make_verification_token(user)
    monkeypatch.setattr(auth_module, "_VERIFICATION_MAX_AGE_SECONDS", -1)

    resp = client.get(f"/v1/auth/verify?token={token}")
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_verify_garbage_token_is_400(client, db):
    resp = client.get("/v1/auth/verify?token=not-a-real-token")
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


# ------------------------------------------------------------------------------- resend-verification
def test_resend_verification_sends_and_is_captured(client, db, email_port):
    account = make_account(db, entitlement="public")
    user = make_user(db, account, email="hank@example.com")
    db.commit()
    login(client, db, user)

    resp = client.post("/v1/auth/resend-verification")
    assert resp.status_code == 202
    body = resp.json()
    assert body["verification_sent"] is True
    assert body["dev_verification_url"]
    assert len(email_port.sent) == 1
    assert email_port.sent[0].to == "hank@example.com"


def test_resend_verification_already_verified_is_200(client, db, email_port):
    account = make_account(db, entitlement="public")
    user = make_user(db, account, email="iris@example.com")
    user.email_verified_at = dt.datetime.now(UTC)
    db.commit()
    login(client, db, user)

    resp = client.post("/v1/auth/resend-verification")
    assert resp.status_code == 200
    assert resp.json() == {"verified": True}
    assert email_port.sent == []


def test_resend_verification_requires_a_session(client, db):
    resp = client.post("/v1/auth/resend-verification")
    assert resp.status_code == 401


def test_resend_verification_rate_limited_after_five(client, db, email_port):
    account = make_account(db, entitlement="public")
    user = make_user(db, account, email="jan@example.com")
    db.commit()
    login(client, db, user)

    for _ in range(5):
        resp = client.post("/v1/auth/resend-verification")
        assert resp.status_code == 202
    sixth = client.post("/v1/auth/resend-verification")
    assert sixth.status_code == 429
    assert sixth.json()["code"] == "rate_limited"
