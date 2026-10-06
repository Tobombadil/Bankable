"""Auth: argon2 password hashing, signed session cookies, API-key issuance/verification, an
`EmailPort` with a dry-run Resend adapter, and the FastAPI dependencies that turn a request into
an `AuthContext` (docs/04-standards.md §6 S-2/S-3; docs/23-api-spec-outline.md §5).

**Decision** (services/README.md "Pro tier and alerts"): `docs/20-architecture.md` §7 specifies
passwordless magic-link + Google sign-in. This task's brief explicitly asks for "signed cookie,
argon2 password hashing, email verification stubbed through an EmailPort" — password auth. Both
can coexist (`User.auth_provider` now has `password` alongside `magic_link`/`google`,
`services/db/models.py`); this module implements the password path plus the session/cookie
machinery either provider needs. Registering the `magic_link`/`google` flows themselves is out of
scope this sprint (no endpoint for either exists in `api/openapi.yaml`) and is left as an open
decision in `services/README.md`.

No public HTTP registration/login endpoint is added this sprint: `api/openapi.yaml` (normative,
`docs/04` API-1) has no `/v1/auth/*` path, and adding one without a corresponding `docs/23` entry
would itself put the contract out of sync. `register_user`/`authenticate_user`/`create_session`
below are the tested library functions a web front end (or a future spec'd endpoint) calls; this
sprint's tests call them directly, the same way `services/api/conftest.py`'s fixtures build
domain rows directly rather than through an API none of this sprint's tests need.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Protocol

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Depends, Header, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.client_ip import client_ip_prefix, is_internal_request, rate_limit_address
from services.api.deps import get_db
from services.api.errors import ProblemError
from services.db.models import Account, ApiKey, User, UserSession
from services.environment import DEV_ENVIRONMENTS
from services.ids import public_id

# ------------------------------------------------------------------------------------- passwords
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:  # invalid hash format, corrupted row, etc. — never a 500 on login
        return False


# --------------------------------------------------------------------------------------- EmailPort
@dataclass(frozen=True)
class SentEmail:
    to: str
    subject: str
    body: str
    provider_message_id: str
    dry_run: bool


class EmailPort(Protocol):
    """The interface `services/alerts` sends through. Every implementation returns a provider
    message id (US-502 AC4's `provider_message_id`) whether or not a real send happened."""

    def send(self, *, to: str, subject: str, body: str) -> SentEmail: ...


class ResendEmailAdapter:
    """`docs/20-architecture.md` §15's pick. **Dry-run whenever `RESEND_API_KEY` is unset**
    (CLAUDE.md: secrets from environment only; no credential in the repo to make a real send
    possible in CI or this sandbox) — the send is recorded in `self.sent` and a synthetic
    `provider_message_id` is returned, so callers (and tests) never need a live network call
    (docs/04 E-6 "fixtures over live calls")."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("RESEND_API_KEY")
        self.sent: list[SentEmail] = []

    @property
    def dry_run(self) -> bool:
        return not self.api_key

    def send(self, *, to: str, subject: str, body: str) -> SentEmail:
        if self.dry_run:
            record = SentEmail(
                to=to,
                subject=subject,
                body=body,
                provider_message_id=f"dryrun_{secrets.token_hex(8)}",
                dry_run=True,
            )
        else:  # pragma: no cover — no live Resend account in this environment (docs/04 E-6)
            import httpx

            resp = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"from": "alerts@infraque.com", "to": [to], "subject": subject, "text": body},
                timeout=10.0,
            )
            resp.raise_for_status()
            record = SentEmail(
                to=to,
                subject=subject,
                body=body,
                provider_message_id=resp.json().get("id", "unknown"),
                dry_run=False,
            )
        self.sent.append(record)
        return record


# ------------------------------------------------------------------------------------ sessions
_SESSION_COOKIE_NAME = "session"
_SESSION_IDLE_DAYS = 30  # docs/20 §7: 30-day idle expiry


_DEV_SESSION_SECRET = "dev-only-insecure-session-secret-do-not-deploy"  # noqa: S105 -- dev/test only, see below
_SESSION_SECRET_MIN_LENGTH = 32
# `ENVIRONMENT` values that may run on the committed dev secret (`services/environment.py`). Compose
# sets `dev` by default and deploy.sh writes `staging` or `production` into the env file; anything
# not listed there (production, staging, preview, a typo) must carry a real SESSION_SECRET
# (docs/04 E-19).
_DEV_ENVIRONMENTS = DEV_ENVIRONMENTS


def session_secret() -> str:
    """The signing secret for sessions and verification tokens (CLAUDE.md: secrets from environment
    only). Outside the dev environments above a missing or short `SESSION_SECRET` raises at
    import time, so a misconfigured deploy fails at startup rather than signing every cookie with
    the constant checked into this file."""
    secret = os.environ.get("SESSION_SECRET", "")
    environment = os.environ.get("ENVIRONMENT", "").strip().lower()
    if environment in _DEV_ENVIRONMENTS:
        return secret or _DEV_SESSION_SECRET
    if len(secret) < _SESSION_SECRET_MIN_LENGTH:
        raise RuntimeError(
            f"SESSION_SECRET must be set to at least {_SESSION_SECRET_MIN_LENGTH} characters when "
            f"ENVIRONMENT={environment!r} (it is {'unset' if not secret else f'{len(secret)} chars'}); "
            "see infra/compose/.env.example and docs/60-deployment.md §5"
        )
    return secret


session_secret()  # fail fast at startup, not on the first request


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(session_secret(), salt="platform-session-cookie")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User, *, ip_prefix: str | None = None) -> tuple[UserSession, str]:
    """Creates the server-side row and returns `(row, signed_cookie_value)`. The cookie carries
    only the session id, signed; the token stored server-side is its SHA-256 (mirrors
    `ApiKey.key_hash` — a stolen DB row alone cannot forge a valid cookie, docs/04 S-2)."""
    now = dt.datetime.now(dt.UTC)
    row = UserSession(
        user_id=user.id,
        token_hash="",
        expires_at=now + dt.timedelta(days=_SESSION_IDLE_DAYS),
        ip_prefix=ip_prefix,
    )
    db.add(row)
    db.flush()
    token = secrets.token_urlsafe(32)
    row.token_hash = _hash_token(f"{row.id}:{token}")
    db.flush()
    cookie_value = _serializer().dumps({"sid": str(row.id), "tok": token})
    return row, cookie_value


def revoke_session(db: Session, session_row: UserSession) -> None:
    session_row.revoked_at = dt.datetime.now(dt.UTC)
    db.flush()


def resolve_session(db: Session, cookie_value: str) -> User | None:
    """Verifies the cookie signature, then the server-side row (not expired, not revoked) and the
    per-session token hash. Any failure returns `None` rather than raising — an absent/invalid
    session cookie falls back to the public tier (docs/23 §5), it is never itself an error."""
    try:
        payload = _serializer().loads(cookie_value, max_age=_SESSION_IDLE_DAYS * 86400)
        sid, tok = payload["sid"], payload["tok"]
    except (BadSignature, KeyError, TypeError, ValueError):
        return None
    row = db.get(UserSession, sid)
    if row is None or row.revoked_at is not None:
        return None
    now = dt.datetime.now(dt.UTC)
    expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=dt.UTC)
    if expires_at <= now:
        return None
    if not hmac.compare_digest(row.token_hash, _hash_token(f"{row.id}:{tok}")):
        return None
    return db.get(User, row.user_id)


def register_user(
    db: Session,
    *,
    email: str,
    password: str,
    name: str | None = None,
    account: Account | None = None,
    role: str = "member",
) -> User:
    """Creates a `personal` account (unless `account` is given, e.g. inviting a member into an
    existing organisation account) and its first user. `email_verified_at` stays null — email
    verification is stubbed through `EmailPort` (`send_verification_email` below), never assumed
    true at registration."""
    if account is None:
        account = Account(public_id="", name=email, kind="personal")
        db.add(account)
        db.flush()
        account.public_id = public_id("acc", account.id)
        db.flush()
    user = User(
        public_id="",
        account_id=account.id,
        email=email.lower(),
        password_hash=hash_password(password),
        name=name,
        role=role,
        auth_provider="password",
    )
    db.add(user)
    db.flush()
    user.public_id = public_id("usr", user.id)
    db.flush()
    return user


def authenticate_user(db: Session, *, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.lower(), User.status == "active"))
    if user is None or user.password_hash is None:
        return None
    if not verify_password(user.password_hash, password):
        return None
    user.last_login_at = dt.datetime.now(dt.UTC)
    db.flush()
    return user


def send_verification_email(email_port: EmailPort, user: User, *, token: str) -> SentEmail:
    return email_port.send(
        to=user.email or "",
        subject="Verify your email",
        body=f"Confirm your account: https://infraque.com/verify?token={token}",
    )


# -------------------------------------------------------------------------------------- API keys
_KEY_ALPHABET = string.ascii_letters + string.digits


def generate_api_key(prefix: str = "bk_live") -> tuple[str, str, str]:
    """Returns `(full_secret, key_hash, last4)`. The secret matches
    `api/openapi.yaml`'s `^bk_(live|test)_[0-9A-Za-z]{43}$` pattern; only the SHA-256 hash and the
    last 4 characters are ever persisted (docs/23 §5, US-701 AC2)."""
    body = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(43))
    secret = f"{prefix}_{body}"
    key_hash = hashlib.sha256(secret.encode()).hexdigest()
    return secret, key_hash, body[-4:]


def resolve_api_key(db: Session, bearer_token: str) -> ApiKey | None:
    key_hash = hashlib.sha256(bearer_token.encode()).hexdigest()
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
    if key is None or key.revoked_at is not None:
        return None
    if key.expires_at is not None:
        expires_at = key.expires_at if key.expires_at.tzinfo else key.expires_at.replace(tzinfo=dt.UTC)
        if expires_at <= dt.datetime.now(dt.UTC):
            return None
    key.last_used_at = dt.datetime.now(dt.UTC)
    db.flush()
    return key


# ---------------------------------------------------------------------------------- AuthContext
@dataclass(frozen=True)
class AuthContext:
    """What every Pro/API route needs, resolved once per request. `entitlement` already accounts
    for a key's `scopes ∩ plan_tier` (docs/23 §5): a `read:public`-only key on a `pro` account
    resolves to `public`, never claims more than the credential grants."""

    user: User | None
    account: Account | None
    api_key: ApiKey | None
    entitlement: str
    scopes: frozenset[str]

    @property
    def is_authenticated(self) -> bool:
        return self.user is not None or self.api_key is not None


PUBLIC_CONTEXT = AuthContext(user=None, account=None, api_key=None, entitlement="public", scopes=frozenset())


def _entitlement_for_key(key: ApiKey, account: Account) -> str:
    """`scopes ∩ plan_tier` (docs/23 §5): a key can never see more than its own scopes grant, nor
    more than the account it belongs to is entitled to."""
    if "read:bulk" in key.scopes or "write:webhooks" in key.scopes:
        resolved = "api"
    elif "read:live" in key.scopes:
        resolved = "pro"
    else:
        resolved = "public"
    order = {"public": 0, "pro": 1, "api": 2, "admin": 3}
    return resolved if order[resolved] <= order.get(account.entitlement, 0) else account.entitlement


def build_auth_context(db: Session, *, session_cookie: str | None, authorization: str | None) -> AuthContext:
    """Session takes precedence when both are present (an unusual case); an invalid or absent
    credential of either kind falls back to `PUBLIC_CONTEXT` rather than raising — Pro/API-only
    routes enforce the 401 themselves (docs/23 §5 "a public read is always available")."""
    if session_cookie:
        user = resolve_session(db, session_cookie)
        if user is not None:
            account = db.get(Account, user.account_id)
            if account is not None:
                return AuthContext(
                    user=user,
                    account=account,
                    api_key=None,
                    entitlement=account.entitlement,
                    scopes=frozenset(),
                )
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        key = resolve_api_key(db, token)
        if key is not None:
            account = db.get(Account, key.account_id)
            if account is not None:
                return AuthContext(
                    user=None,
                    account=account,
                    api_key=key,
                    entitlement=_entitlement_for_key(key, account),
                    scopes=frozenset(key.scopes),
                )
    return PUBLIC_CONTEXT


def _charge_unresolved_credential(request: Request) -> None:
    """Meter a credential that resolved to nothing on the caller's anonymous per-address bucket,
    once per request. The middleware in `services/api/app.py` skips that bucket for any request
    carrying a credential, on the assumption that a Pro/API bucket meters it further down; a junk
    credential would otherwise be served on the public tier unmetered (API audit 2026-09-18
    finding S3; backend audit 2026-09-30 F8)."""
    if getattr(request.state, "unresolved_credential_charged", False):
        return
    request.state.unresolved_credential_charged = True
    from services.api.ratelimit import TIER_LIMITS, default_limiter

    result = default_limiter.check(f"public:{rate_limit_address(request)}", limit=TIER_LIMITS["public"])
    if not result.allowed:
        raise ProblemError(
            "rate_limited",
            "Rate limit exceeded",
            detail=(
                "Public-tier request limit reached for this address; "
                "the credential presented did not resolve."
            ),
            headers={"Retry-After": str(result.reset_seconds)},
        )


def resolve_request_auth(
    request: Request, db: Session, *, session_cookie: str | None, authorization: str | None
) -> AuthContext:
    """The request's `AuthContext`, resolved once and kept on `request.state` (same request, same
    DB session, so the ORM rows stay attached). A credential that resolves to nothing is charged to
    the public bucket unless the request is the site's own (`X-Internal-Token`): the site forwards
    its visitors' cookies, and it is already exempt from the anonymous bucket (`services/api/app.py`)."""
    cached: AuthContext | None = getattr(request.state, "auth_context", None)
    if cached is not None:
        return cached
    ctx = build_auth_context(db, session_cookie=session_cookie, authorization=authorization)
    if ctx is PUBLIC_CONTEXT and (session_cookie or authorization) and not is_internal_request(request):
        _charge_unresolved_credential(request)
    request.state.auth_context = ctx
    return ctx


def get_auth_context(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    session: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    return resolve_request_auth(request, db, session_cookie=session, authorization=authorization)


#: Paths that meter a credential on a bucket of their own instead of the tier's read bucket
#: (`services/api/bulk.py`: "the key's own `bulk` bucket and nothing from its `read` bucket").
_OWN_BUCKET_PREFIXES = ("/v1/bulk/",)


def credential_tier(ctx: AuthContext) -> str:
    """The docs/23 §6 row a resolved credential is metered under. A signed-in account with no paid
    entitlement is a `free_account` (300 an hour), not the anonymous 60; so is a key that resolves to
    the public tier, since only an account can hold one."""
    from services.api.ratelimit import TIER_LIMITS

    if ctx.entitlement in TIER_LIMITS and ctx.entitlement != "public":
        return ctx.entitlement
    return "free_account" if ctx.is_authenticated else "public"


def charge_credential(request: Request, ctx: AuthContext) -> dict[str, str]:
    """Charge a resolved credential one request on its tier's read bucket, once per request, and
    return the `RateLimit-*` headers (cached on `request.state`, so a route that asks again, through
    `services/api/pro.py::_rate_limit_headers`, gets the same numbers without a second charge).
    Keyed by API key, else by user. Raises `429 rate_limited` with `Retry-After` when spent."""
    cached: dict[str, str] | None = getattr(request.state, "credential_rate_limit", None)
    if cached is not None:
        return cached
    from services.api.ratelimit import TIER_LIMITS, default_limiter, policy_header

    tier = credential_tier(ctx)
    key = ctx.api_key.public_id if ctx.api_key else (str(ctx.user.id) if ctx.user else "anon")
    result = default_limiter.check(f"{tier}:{key}", limit=TIER_LIMITS[tier])
    headers = {
        "RateLimit-Limit": str(result.limit),
        "RateLimit-Remaining": str(result.remaining),
        "RateLimit-Reset": str(result.reset_seconds),
        "RateLimit-Policy": policy_header(tier, result),
    }
    if not result.allowed:
        raise ProblemError(
            "rate_limited",
            "Rate limit exceeded",
            detail=f"More than {result.limit} requests in the current window.",
            headers={"Retry-After": str(result.reset_seconds), **headers},
        )
    request.state.credential_rate_limit = headers
    return headers


def meter_credentialed_request(request: Request, db: Annotated[Session, Depends(get_db)]) -> None:
    """App-wide dependency (`services/api/app.py` `FastAPI(dependencies=...)`): every route, not
    only the ones that ask for `get_auth_context`, resolves a presented credential and meters it.

    - A credential that resolves to nothing is charged to the caller's anonymous bucket, so a junk
      `Authorization` or `session` header exempts nothing. Before, 13 public GET routes never
      resolved auth and served such callers unmetered (backend audit 2026-09-30 F8).
    - A valid credential is charged to its own tier's bucket (`charge_credential`; docs/23 §6,
      US-702). Before, the main read routes did not meter it at all, and a free account pulled
      150 pages of 200 rows with no `RateLimit-*` headers (QA audit 2026-09-30, QA-3).
    - The site's own server-side calls (`X-Internal-Token`) stay exempt, as they are from the
      anonymous bucket; so do the bulk streams, which meter their own bucket.

    It reads the credential from the raw request rather than via `Cookie()`/`Header()` parameters
    so it adds nothing to every operation in the OpenAPI document, and shares the per-request
    caches with `get_auth_context` and `_rate_limit_headers`, so nothing is resolved or charged
    twice. Anonymous requests return at once; the middleware meters those."""
    session_cookie = request.cookies.get(_SESSION_COOKIE_NAME)
    authorization = request.headers.get("authorization")
    if not (session_cookie or authorization):
        return
    ctx = resolve_request_auth(request, db, session_cookie=session_cookie, authorization=authorization)
    if ctx is PUBLIC_CONTEXT or is_internal_request(request):
        return
    if request.url.path.startswith(_OWN_BUCKET_PREFIXES):
        return
    charge_credential(request, ctx)


_ENTITLEMENT_ORDER = {"public": 0, "pro": 1, "api": 2, "admin": 3}


def require_authenticated() -> Callable[[AuthContext], AuthContext]:
    """Any valid session or key, regardless of entitlement — `GET /v1/me`'s security set
    (`Session: []` / `ApiKey: [read:public]`) is "signed in", not "Pro"."""

    def _dep(ctx: Annotated[AuthContext, Depends(get_auth_context)]) -> AuthContext:
        if not ctx.is_authenticated:
            raise ProblemError("unauthenticated", "Authentication required")
        return ctx

    return _dep


def require_entitlement(minimum: str) -> Callable[[AuthContext], AuthContext]:
    """FastAPI dependency factory: `401 unauthenticated` with no credential at all, `403
    forbidden_tier` with a valid-but-insufficient one (docs/23 §5, §8)."""

    def _dep(ctx: Annotated[AuthContext, Depends(get_auth_context)]) -> AuthContext:
        if not ctx.is_authenticated:
            raise ProblemError("unauthenticated", "Authentication required")
        if _ENTITLEMENT_ORDER[ctx.entitlement] < _ENTITLEMENT_ORDER[minimum]:
            raise ProblemError(
                "forbidden_tier",
                "Insufficient entitlement",
                detail=f"This operation needs at least the {minimum!r} entitlement.",
            )
        return ctx

    return _dep


def require_scope(scope: str) -> Callable[[AuthContext], AuthContext]:
    """For API-key-only operations (`write:webhooks`, `read:bulk`) — a session with sufficient
    entitlement does not automatically carry a key scope it never requested."""

    def _dep(ctx: Annotated[AuthContext, Depends(get_auth_context)]) -> AuthContext:
        if not ctx.is_authenticated:
            raise ProblemError("unauthenticated", "Authentication required")
        if ctx.api_key is None or scope not in ctx.scopes:
            raise ProblemError(
                "forbidden_tier",
                "Missing scope",
                detail=f"An API key with the {scope!r} scope is required.",
            )
        return ctx

    return _dep


def require_session_only(minimum: str) -> Callable[[AuthContext], AuthContext]:
    """For operations `api/openapi.yaml` marks `Session: [...]` only (creating a key: "a key
    cannot mint keys", US-701)."""

    def _dep(ctx: Annotated[AuthContext, Depends(get_auth_context)]) -> AuthContext:
        if ctx.user is None:
            raise ProblemError("unauthenticated", "A signed-in session is required")
        if _ENTITLEMENT_ORDER[ctx.entitlement] < _ENTITLEMENT_ORDER[minimum]:
            raise ProblemError("forbidden_tier", "Insufficient entitlement")
        return ctx

    return _dep


def require_admin(*, roles: tuple[str, ...] = ("operator", "owner")) -> Callable[[AuthContext], AuthContext]:
    """Admin surface (docs/20 §7, docs/23 §4): a session whose `user.role` is one of `roles`.
    This sprint has no separate `admin.` host/second-factor enforcement (out of scope; the
    reverse proxy/hosting layer owns that per `infra/`), so it is a role check only, documented
    as an open decision in services/README.md."""

    def _dep(ctx: Annotated[AuthContext, Depends(get_auth_context)]) -> AuthContext:
        if ctx.user is None:
            raise ProblemError("unauthenticated", "An operator session is required")
        if ctx.user.role not in roles:
            raise ProblemError("forbidden_tier", "Operator role required", detail=f"Needs one of {roles}.")
        return ctx

    return _dep


def iter_client_ip_prefix(request: Request) -> str | None:
    """The caller's coarse address (docs/21 §3.17 `api_key.last_used_ip`): IPv4 /24, IPv6 /64.
    The address is the visitor's, not the proxy's (`services/api/client_ip.py`)."""
    return client_ip_prefix(request)


# ------------------------------------------------------------------------- Sprint 3: /v1/auth/*
# The functions below are new, append-only additions for `services/api/auth_routes.py` (Sprint 3
# "login and registration surface" task). They do not change any function above; `resolve_session`
# in particular keeps its exact behaviour and signature — `resolve_session_row` below duplicates
# its verification steps rather than factoring them out of it, since this module is append-only
# for this task.


def resolve_session_row(db: Session, cookie_value: str) -> UserSession | None:
    """Same verification `resolve_session` performs (signature check, then the server-side row:
    not revoked, not expired, per-session token hash), returning the `UserSession` row itself
    rather than its `User` — `POST /v1/auth/logout` needs the row to revoke exactly the session
    the cookie names, not the user it belongs to."""
    try:
        payload = _serializer().loads(cookie_value, max_age=_SESSION_IDLE_DAYS * 86400)
        sid, tok = payload["sid"], payload["tok"]
    except (BadSignature, KeyError, TypeError, ValueError):
        return None
    row = db.get(UserSession, sid)
    if row is None or row.revoked_at is not None:
        return None
    now = dt.datetime.now(dt.UTC)
    expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=dt.UTC)
    if expires_at <= now:
        return None
    if not hmac.compare_digest(row.token_hash, _hash_token(f"{row.id}:{tok}")):
        return None
    return row


_VERIFICATION_SALT = "email-verification"
_VERIFICATION_MAX_AGE_SECONDS = 24 * 3600  # docs/23 email-verification link lifetime


def _verification_serializer() -> URLSafeTimedSerializer:
    # Same secret source as `_serializer()` (`session_secret()`, CLAUDE.md: secrets from
    # environment only), a different salt so a verification token can never be replayed as a
    # session cookie or vice versa (itsdangerous salts namespace the signature, not the payload).
    return URLSafeTimedSerializer(session_secret(), salt=_VERIFICATION_SALT)


def make_verification_token(user: User) -> str:
    """Signs `{uid, email}` (the public id, so the token cannot be replayed for a different user
    even if two users somehow shared an email at signing time) for the `GET /v1/auth/verify` link
    `send_verification_email` puts in the message body."""
    return _verification_serializer().dumps({"uid": user.public_id, "email": user.email})


def read_verification_token(token: str) -> tuple[str, str] | None:
    """Returns `(uid, email)` for a valid, unexpired token, or `None` for anything else (bad
    signature, tampered payload, expired past `_VERIFICATION_MAX_AGE_SECONDS`) — the route turns
    `None` into `400 validation_error` rather than this module raising."""
    try:
        payload = _verification_serializer().loads(token, max_age=_VERIFICATION_MAX_AGE_SECONDS)
        return payload["uid"], payload["email"]
    except (BadSignature, KeyError, TypeError, ValueError):
        return None


# ------------------------------------------------------- lockout recovery (QA-5, lane A1, 2026-09-30)
# A lost cookie or a forgotten password had no way back: no reset, no "sign out other sessions"
# (QA audit 2026-09-30, finding QA-5). Both reuse the machinery above -- the same signing secret
# with its own salt, and the same `EmailPort` -- rather than adding a token table.

_PASSWORD_RESET_SALT = "password-reset"  # noqa: S105 -- an itsdangerous salt (a namespace), not a secret
PASSWORD_RESET_MAX_AGE_SECONDS = 3600


def password_fingerprint(user: User) -> str:
    """A short digest of the user's current password hash. Carried in the reset token, so the token
    stops working the moment the password changes: single use, with no server-side token state."""
    return hashlib.sha256((user.password_hash or "").encode()).hexdigest()[:16]


def _password_reset_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(session_secret(), salt=_PASSWORD_RESET_SALT)


def make_password_reset_token(user: User) -> str:
    return _password_reset_serializer().dumps({"uid": user.public_id, "fp": password_fingerprint(user)})


def read_password_reset_token(token: str) -> tuple[str, str] | None:
    """`(uid, fingerprint)` for a valid token younger than `PASSWORD_RESET_MAX_AGE_SECONDS`, else `None`."""
    try:
        payload = _password_reset_serializer().loads(token, max_age=PASSWORD_RESET_MAX_AGE_SECONDS)
        return str(payload["uid"]), str(payload["fp"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None


def send_password_reset_email(email_port: EmailPort, user: User, *, url: str) -> SentEmail:
    """Sent only because the account holder asked for it on the sign-in page; dry-run (recorded,
    never sent) whenever `RESEND_API_KEY` is unset."""
    return email_port.send(
        to=user.email or "",
        subject="Reset your password",
        body=(
            f"Someone asked to reset the password for this address. To choose a new one, open:\n{url}\n\n"
            "The link works once and for one hour. If you did not ask, ignore this email; your password "
            "is unchanged."
        ),
    )


def revoke_user_sessions(db: Session, user: User, *, keep: UserSession | None = None) -> int:
    """Revokes every live session of `user` except `keep`; returns how many were revoked."""
    now = dt.datetime.now(dt.UTC)
    rows = db.scalars(
        select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    ).all()
    count = 0
    for row in rows:
        if keep is not None and row.id == keep.id:
            continue
        row.revoked_at = now
        count += 1
    db.flush()
    return count
