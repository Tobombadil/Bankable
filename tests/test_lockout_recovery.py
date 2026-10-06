"""Lockout recovery (QA audit 2026-09-30, finding QA-5; `services/api/auth_routes.py` decision 5):
sign in while ending one's own other sessions, sign out other sessions, and reset a password by
email. Every email here goes through a dry-run `ResendEmailAdapter`."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from services.api.app import app
from services.api.auth import ResendEmailAdapter, create_session
from services.api.auth_routes import get_email_port
from tests.conftest import login, make_account, make_user

PASSWORD = "correct horse battery staple"


@pytest.fixture()
def email_port():
    port = ResendEmailAdapter()
    app.dependency_overrides[get_email_port] = lambda: port
    yield port
    app.dependency_overrides.pop(get_email_port, None)


def _login(client, email: str, password: str = PASSWORD, **extra):
    return client.post("/v1/auth/login", json={"email": email, "password": password, **extra})


def _me_with(client, cookie: str) -> int:
    client.cookies.clear()
    client.cookies.set("session", cookie)
    return client.get("/v1/me").status_code


# ---------------------------------------------------------------------------- seat-limited login
def test_a_paid_user_locked_out_by_a_lost_browser_can_get_back_in(client, db) -> None:
    account = make_account(db, entitlement="pro")  # one seat
    user = make_user(db, account, email="lost@example.com")
    db.commit()
    _row, old_cookie = create_session(db, user)
    db.commit()

    assert _login(client, "lost@example.com").json()["code"] == "seat_limit"
    assert "sign_out_other_sessions" in _login(client, "lost@example.com").json()["detail"]
    resp = _login(client, "lost@example.com", sign_out_other_sessions=True)
    assert resp.status_code == 200, resp.text
    new_cookie = resp.cookies.get("session")
    assert _me_with(client, old_cookie) == 401
    assert _me_with(client, new_cookie) == 200


def test_the_option_needs_the_right_password(client, db) -> None:
    account = make_account(db, entitlement="pro")
    user = make_user(db, account, email="guard@example.com")
    db.commit()
    _row, cookie = create_session(db, user)
    db.commit()
    assert (
        _login(client, "guard@example.com", "wrong password here", sign_out_other_sessions=True).status_code
        == 401
    )
    assert _me_with(client, cookie) == 200, "a wrong password revokes nothing"


def test_other_users_seats_are_never_taken(client, db) -> None:
    account = make_account(db, entitlement="pro")  # one seat, held by a colleague
    colleague = make_user(db, account, email="colleague@example.com")
    make_user(db, account, email="me@example.com")
    db.commit()
    _row, colleague_cookie = create_session(db, colleague)
    db.commit()
    assert _login(client, "me@example.com", sign_out_other_sessions=True).json()["code"] == "seat_limit"
    assert _me_with(client, colleague_cookie) == 200


# ------------------------------------------------------------------------ sign out other sessions
def test_sign_out_other_sessions_keeps_this_one(client, db) -> None:
    account = make_account(db, entitlement="public")
    user = make_user(db, account, email="two@example.com")
    db.commit()
    _row, laptop = create_session(db, user)
    db.commit()
    login(client, db, user)  # the phone
    phone = client.cookies.get("session")
    resp = client.post("/v1/auth/sessions/revoke-others")
    assert resp.status_code == 200 and resp.json() == {"revoked": 1}
    assert _me_with(client, laptop) == 401
    assert _me_with(client, phone) == 200


def test_sign_out_other_sessions_needs_a_session(client) -> None:
    assert client.post("/v1/auth/sessions/revoke-others").status_code == 401


# --------------------------------------------------------------------------------- password reset
def test_a_reset_request_answers_the_same_for_unknown_addresses(client, db, email_port) -> None:
    resp = client.post("/v1/auth/password-reset/request", json={"email": "nobody@example.com"})
    assert resp.status_code == 202 and resp.json() == {"reset_requested": True}
    assert email_port.sent == []
    bad = client.post("/v1/auth/password-reset/request", json={"email": "not-an-address"})
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "email"


def test_password_reset_end_to_end(client, db, email_port) -> None:
    account = make_account(db, entitlement="pro")
    user = make_user(db, account, email="forgot@example.com")
    db.commit()
    _row, stale_cookie = create_session(db, user)
    db.commit()

    resp = client.post("/v1/auth/password-reset/request", json={"email": "Forgot@Example.com"})
    assert resp.status_code == 202
    url = resp.json()["dev_reset_url"]
    assert len(email_port.sent) == 1 and url in email_port.sent[0].body
    token = parse_qs(urlsplit(url).query)["token"][0]

    short = client.post("/v1/auth/password-reset", json={"token": token, "password": "short"})
    assert short.status_code == 400 and short.json()["errors"][0]["field"] == "password"

    done = client.post("/v1/auth/password-reset", json={"token": token, "password": "a brand new passphrase"})
    assert done.status_code == 200 and done.json() == {"password_reset": True, "sessions_revoked": 1}
    assert _me_with(client, stale_cookie) == 401
    client.cookies.clear()
    assert _login(client, "forgot@example.com").status_code == 401
    assert _login(client, "forgot@example.com", "a brand new passphrase").status_code == 200

    reused = client.post(
        "/v1/auth/password-reset", json={"token": token, "password": "yet another passphrase"}
    )
    assert reused.status_code == 400 and reused.json()["errors"][0]["field"] == "token"


def test_a_forged_token_is_refused(client, db) -> None:
    resp = client.post(
        "/v1/auth/password-reset", json={"token": "not-a-token", "password": "a brand new passphrase"}
    )
    assert resp.status_code == 400 and resp.json()["errors"][0]["field"] == "token"
    assert (
        client.post("/v1/auth/password-reset", json={"password": "a brand new passphrase"}).status_code == 400
    )
