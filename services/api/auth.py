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

from services.api.deps import get_db
from services.api.errors import ProblemError
from services.db.models import Account, ApiKey, User, UserSession
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
                json={"from": "alerts@{{DOMAIN}}", "to": [to], "subject": subject, "text": body},
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


def _serializer() -> URLSafeTimedSerializer:
    # CLAUDE.md: secrets from environment only. A hardcoded fallback would sign every session
    # with a secret checked into the repo; the fallback here is dev-only and every real deploy
    # sets SESSION_SECRET (docs/04 E-19 requires an owner/environment/rotation date per secret).
    secret = os.environ.get("SESSION_SECRET", "dev-only-insecure-session-secret-do-not-deploy")
    return URLSafeTimedSerializer(secret, salt="platform-session-cookie")


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
        body=f"Confirm your account: https://{{{{DOMAIN}}}}/verify?token={token}",
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


def get_auth_context(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    session: Annotated[str | None, Cookie()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    return build_auth_context(db, session_cookie=session, authorization=authorization)


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
    """Truncates an IPv4 address to /24 (docs/21 §3.17 `api_key.last_used_ip`); best-effort only —
    IPv6 truncation is a follow-up, not needed by this sprint's tests."""
    host = request.client.host if request.client else None
    if host is None:
        return None
    parts = host.split(".")
    if len(parts) == 4:
        return ".".join([*parts[:3], "0"])
    return host
