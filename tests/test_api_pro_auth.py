"""Auth: argon2 password hashing, signed session cookies, API-key issuance/verification, the
`EmailPort` dry-run adapter, and the `require_*` FastAPI dependencies (docs/04-standards.md §6
S-2/S-3; task brief item 1)."""

from __future__ import annotations

import datetime as dt

from services.api.auth import (
    PUBLIC_CONTEXT,
    ResendEmailAdapter,
    authenticate_user,
    build_auth_context,
    create_session,
    generate_api_key,
    hash_password,
    register_user,
    resolve_api_key,
    resolve_session,
    revoke_session,
    verify_password,
)
from tests.conftest import make_account, make_api_key, make_user

UTC = dt.UTC


# --------------------------------------------------------------------------------------- passwords
def test_password_hash_is_argon2_and_verifies():
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2")
    assert verify_password(hashed, "correct horse battery staple") is True
    assert verify_password(hashed, "wrong password") is False


def test_two_hashes_of_the_same_password_differ():
    """argon2 salts every hash; two calls must not produce the same digest."""
    assert hash_password("same-password") != hash_password("same-password")


def test_register_and_authenticate_user(db):
    user = register_user(db, email="Ana@Example.com", password="hunter2-hunter2", name="Ana")
    db.commit()
    assert user.email == "ana@example.com"  # lower-cased
    assert user.password_hash != "hunter2-hunter2"

    found = authenticate_user(db, email="ANA@EXAMPLE.COM", password="hunter2-hunter2")
    assert found is not None
    assert found.id == user.id
    assert found.last_login_at is not None

    assert authenticate_user(db, email="ana@example.com", password="wrong") is None
    assert authenticate_user(db, email="nobody@example.com", password="x") is None


# ----------------------------------------------------------------------------------------- EmailPort
def test_resend_adapter_is_dry_run_without_an_api_key(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    port = ResendEmailAdapter()
    assert port.dry_run is True
    sent = port.send(to="ana@example.com", subject="Hi", body="Body text")
    assert sent.dry_run is True
    assert sent.provider_message_id.startswith("dryrun_")
    assert port.sent == [sent]


def test_resend_adapter_reads_key_from_environment(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_live_abc123")
    port = ResendEmailAdapter()
    assert port.dry_run is False


# ------------------------------------------------------------------------------------------ sessions
def test_session_round_trips_and_is_revocable(db):
    account = make_account(db)
    user = make_user(db, account)
    db.flush()

    row, cookie = create_session(db, user)
    db.commit()

    resolved = resolve_session(db, cookie)
    assert resolved is not None
    assert resolved.id == user.id

    revoke_session(db, row)
    db.commit()
    assert resolve_session(db, cookie) is None


def test_session_cookie_is_rejected_after_tampering(db):
    account = make_account(db)
    user = make_user(db, account)
    _row, cookie = create_session(db, user)
    db.commit()

    # Flip a character in the payload segment (before the first ".") rather than the very last
    # character of the whole cookie: the tail few base64 characters of an itsdangerous token can
    # have spare bits that some substitutions decode identically, making tampering there flaky.
    payload, _, rest = cookie.partition(".")
    flipped_char = "A" if payload[-1] != "A" else "B"
    tampered = payload[:-1] + flipped_char + "." + rest
    assert resolve_session(db, tampered) is None


def test_unknown_or_garbage_cookie_resolves_to_no_user(db):
    assert resolve_session(db, "not-a-real-cookie") is None
    assert resolve_session(db, "") is None


# ----------------------------------------------------------------------------------------- API keys
def test_generate_api_key_shape_and_storage():
    secret, key_hash, last4 = generate_api_key("bk_live")
    assert secret.startswith("bk_live_")
    assert len(secret) == len("bk_live_") + 43
    assert last4 == secret[-4:]
    assert key_hash != secret  # only the hash is meant to be persisted


def test_resolve_api_key_round_trips_and_rejects_revoked(db):
    account = make_account(db)
    user = make_user(db, account)
    key, secret = make_api_key(db, account, user, scopes=["read:live"])
    db.commit()

    resolved = resolve_api_key(db, secret)
    assert resolved is not None
    assert resolved.id == key.id
    assert resolved.last_used_at is not None

    key.revoked_at = dt.datetime.now(UTC)
    db.commit()
    assert resolve_api_key(db, secret) is None


def test_resolve_api_key_rejects_expired(db):
    account = make_account(db)
    user = make_user(db, account)
    key, secret = make_api_key(db, account, user)
    key.expires_at = dt.datetime.now(UTC) - dt.timedelta(days=1)
    db.commit()
    assert resolve_api_key(db, secret) is None


def test_resolve_api_key_rejects_wrong_secret(db):
    account = make_account(db)
    user = make_user(db, account)
    make_api_key(db, account, user)
    db.commit()
    assert resolve_api_key(db, "bk_live_" + "z" * 43) is None


# ------------------------------------------------------------------------------------- AuthContext
def test_build_auth_context_falls_back_to_public_on_no_credential(db):
    ctx = build_auth_context(db, session_cookie=None, authorization=None)
    assert ctx == PUBLIC_CONTEXT
    assert ctx.entitlement == "public"
    assert ctx.is_authenticated is False


def test_build_auth_context_from_session_cookie(db):
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    _row, cookie = create_session(db, user)
    db.commit()

    ctx = build_auth_context(db, session_cookie=cookie, authorization=None)
    assert ctx.is_authenticated is True
    assert ctx.user is not None and ctx.user.id == user.id
    assert ctx.entitlement == "pro"


def test_build_auth_context_key_entitlement_is_min_of_scope_and_account(db):
    """A `read:live` key on an `api`-entitled account resolves to `pro` (scope ceiling), and a
    `read:bulk` key on a `pro`-entitled account never claims `api` (account ceiling) — the
    `scopes ∩ plan_tier` rule of docs/23 §5."""
    api_account = make_account(db, entitlement="api")
    user = make_user(db, api_account, email="a@example.com")
    _key, secret = make_api_key(db, api_account, user, scopes=["read:live"])
    db.commit()
    ctx = build_auth_context(db, session_cookie=None, authorization=f"Bearer {secret}")
    assert ctx.entitlement == "pro"

    pro_account = make_account(db, entitlement="pro", name="Pro Account")
    user2 = make_user(db, pro_account, email="b@example.com")
    _key2, secret2 = make_api_key(db, pro_account, user2, scopes=["read:bulk", "write:webhooks"])
    db.commit()
    ctx2 = build_auth_context(db, session_cookie=None, authorization=f"Bearer {secret2}")
    assert ctx2.entitlement == "pro"  # capped by the account, not the key's own scope tier


def test_build_auth_context_ignores_malformed_authorization_header(db):
    ctx = build_auth_context(db, session_cookie=None, authorization="NotBearer abc")
    assert ctx == PUBLIC_CONTEXT


# ---------------------------------------------------------------------------- HTTP-level dependencies
def test_me_requires_authentication(client):
    resp = client.get("/v1/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthenticated"


def test_me_returns_identity_and_entitlement_for_a_session(client, db):
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    from tests.conftest import login

    login(client, db, user)
    resp = client.get("/v1/me")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["user"]["user_id"] == user.public_id
    assert body["account"]["account_id"] == account.public_id
    assert body["tier"] == "pro"
    assert body["api_licence"]["current_version"]


def test_account_requires_pro_entitlement(client, db):
    account = make_account(db, entitlement="public")
    user = make_user(db, account)
    db.commit()
    from tests.conftest import login

    login(client, db, user)
    resp = client.get("/v1/account")
    assert resp.status_code == 403
    assert resp.json()["code"] == "forbidden_tier"


def test_api_key_bearer_grants_access_to_pro_route(client, db):
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live"])
    db.commit()
    resp = client.get("/v1/saved-searches", headers={"Authorization": f"Bearer {secret}"})
    assert resp.status_code == 200


def test_me_with_a_key_only_credential_renders_as_the_keys_creator(client, db):
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live"])
    db.commit()
    resp = client.get("/v1/me", headers={"Authorization": f"Bearer {secret}"})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["user"]["user_id"] == user.public_id
    assert body["scopes"] == ["read:live"]


def test_revoked_key_is_rejected(client, db):
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    key, secret = make_api_key(db, account, user, scopes=["read:live"])
    key.revoked_at = dt.datetime.now(UTC)
    db.commit()
    resp = client.get("/v1/saved-searches", headers={"Authorization": f"Bearer {secret}"})
    assert resp.status_code == 401


def test_user_roles_vocabulary_includes_legal():
    from services.db.models import USER_ROLES

    assert "legal" in USER_ROLES
    assert set(USER_ROLES) == {"viewer", "member", "operator", "legal", "owner"}


def test_apikey_model_scopes_vocabulary_excludes_nothing_customers_need():
    from services.db.models import API_KEY_SCOPES

    assert API_KEY_SCOPES == ("read:public", "read:live", "read:bulk", "write:webhooks", "admin:*")
