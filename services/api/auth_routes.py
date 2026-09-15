"""`/v1/auth/*`: the password login/registration surface on top of `services/api/auth.py`'s
session machinery (Sprint 3 "login and registration surface", first wave). Fills the gap that
module's docstring names — "no public HTTP registration/login endpoint is added this sprint" was
true for Sprint 2 only; this sprint adds the endpoint that calls those tested library functions.

Envelopes and error handling follow `services/api/pro.py`'s conventions exactly: plain
`dict`/`list` bodies built by `services/api/serialize.py`, `ProblemError` for every non-2xx
response, RFC 9457 problem bodies via the app-wide exception handler. Mounted onto
`services.api.app.app` with one `include_router` call, the same pattern `pro.router` uses
(`services/api/app.py`, out of this task's writable scope — the coordinator does the mount).

**Decisions**
1. Request bodies are plain `dict[str, Any]`, matching `services/api/pro.py`'s
   `create_saved_search` et al. — no parallel Pydantic model tree, per `serialize.py`'s own
   rationale (the committed OpenAPI file is the shape authority, `tests/test_api_contract.py`
   checks responses against it directly).
2. The seat check (US-602 AC2) counts *actual* non-revoked, unexpired `UserSession` rows for the
   account's users at login time, not the `Account.seats_used` mirror column — that column has no
   writer yet anywhere in the codebase (grep confirms) and `docs/21` §3.13 describes it as part of
   the CRM/ERP entitlement mirror, which Sprint 3's task brief does not touch. Counting live
   sessions directly is simpler and cannot drift from what "seats" is meant to police: concurrent
   logins.
3. Session-row expiry is compared in Python after loading candidate rows, mirroring
   `resolve_session`'s own naive/aware handling (`services/api/common.py`'s `ensure_aware`
   docstring: SQLite round-trips `DateTime(timezone=True)` as naive) rather than filtering
   `expires_at` in SQL, which would behave differently across the SQLite test target and a real
   Postgres deployment.
4. `dev_verification_url` is included in `register`/`resend-verification` responses only when the
   process-wide `EmailPort` is a dry-run `ResendEmailAdapter` (no `RESEND_API_KEY` configured) —
   documented on the field in `api/fragments/auth.yaml`, never present against a real provider.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.auth import (
    AuthContext,
    EmailPort,
    ResendEmailAdapter,
    authenticate_user,
    create_session,
    get_auth_context,
    iter_client_ip_prefix,
    make_verification_token,
    read_verification_token,
    register_user,
    resolve_session_row,
    revoke_session,
    send_verification_email,
)
from services.api.common import WEB_HOST, utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError
from services.api.ratelimit import default_limiter
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_meta,
    serialize_account,
    serialize_user,
)
from services.db.models import Account, User, UserSession

router = APIRouter()

SESSION_COOKIE_NAME = "session"
SESSION_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 3600  # matches services/api/auth.py _SESSION_IDLE_DAYS

_REGISTER_LIMIT = 10
_LOGIN_LIMIT = 20
_RESEND_LIMIT = 5
_MIN_PASSWORD_LENGTH = 12
_MAX_EMAIL_LENGTH = 254


# ------------------------------------------------------------------------------------ email port
#: One process-wide `EmailPort`, mirroring `services/api/deps.py`'s single `_sessionmaker` and
#: `services/api/ratelimit.py`'s single `default_limiter` — a real deployment sends through one
#: Resend account regardless of which worker handles the request. Dry-run whenever
#: `RESEND_API_KEY` is unset (`ResendEmailAdapter`'s own docstring); tests override this
#: dependency with `app.dependency_overrides[get_email_port]` to capture `.sent` without it.
_email_port: EmailPort | None = None


def get_email_port() -> EmailPort:
    global _email_port
    if _email_port is None:
        _email_port = ResendEmailAdapter()
    return _email_port


# ------------------------------------------------------------------------------------- cookies
def set_session_cookie(response: Response, value: str, request: Request) -> None:
    """`Secure` when the request itself arrived over HTTPS, or when `SESSION_COOKIE_SECURE` is
    set truthy for a deployment that terminates TLS upstream of this process (the request then
    reaches us as plain HTTP even though the browser used HTTPS)."""
    secure = request.url.scheme == "https" or os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        value,
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
        secure=secure,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


# ------------------------------------------------------------------------------------- validation
def _email_error(email: str) -> str | None:
    """Simple shape check (task brief: "one @, a dot after it, ≤254 chars") — not RFC 5322, which
    is the deliverable a verification email link already proves in practice."""
    if not email or len(email) > _MAX_EMAIL_LENGTH:
        return f"must be 1-{_MAX_EMAIL_LENGTH} characters"
    if email.count("@") != 1:
        return "must contain exactly one @"
    local, domain = email.split("@", 1)
    if not local:
        return "must have a name before the @"
    dot = domain.find(".")
    if dot <= 0 or dot == len(domain) - 1:
        return "must have a domain with a dot after the @"
    return None


def _password_error(password: str) -> str | None:
    if len(password) < _MIN_PASSWORD_LENGTH:
        return f"must be at least {_MIN_PASSWORD_LENGTH} characters"
    return None


def _rate_limit(key: str, *, limit: int) -> None:
    result = default_limiter.check(key, limit=limit)
    if not result.allowed:
        raise ProblemError(
            "rate_limited",
            "Rate limit exceeded",
            detail=f"More than {result.limit} requests in the current window.",
            headers={"Retry-After": str(result.reset_seconds)},
        )


def _client_ip_key(request: Request) -> str:
    return iter_client_ip_prefix(request) or "unknown"


def _verification_url(token: str) -> str:
    """The exact URL `send_verification_email` puts in the message body (`services/api/auth.py`),
    reconstructed here so `dev_verification_url` is provably "the same URL that went into the
    email" rather than a second, possibly-diverging template."""
    return f"{WEB_HOST}/verify?token={token}"


def _active_session_count(db: Session, account: Account) -> int:
    """US-602 AC2: non-revoked, unexpired `UserSession` rows across every user of the account —
    see this module's docstring decision 2 and decision 3."""
    now = utcnow()
    rows = db.scalars(
        select(UserSession)
        .join(User, UserSession.user_id == User.id)
        .where(User.account_id == account.id, UserSession.revoked_at.is_(None))
    ).all()
    count = 0
    for row in rows:
        expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=dt.UTC)
        if expires_at > now:
            count += 1
    return count


# --------------------------------------------------------------------------------------- register
@router.post("/v1/auth/register", status_code=201)
def register(
    body: dict[str, Any],
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    email_port: Annotated[EmailPort, Depends(get_email_port)],
) -> Any:
    email = str(body.get("email") or "")
    password = str(body.get("password") or "")
    name = body.get("name")
    name = str(name) if name else None

    errors: list[dict[str, str]] = []
    email_err = _email_error(email)
    if email_err:
        errors.append({"field": "email", "message": email_err})
    password_err = _password_error(password)
    if password_err:
        errors.append({"field": "password", "message": password_err})
    if errors:
        raise ProblemError("validation_error", "Invalid request", errors=errors, instance=request.url.path)

    _rate_limit(f"auth-register:{_client_ip_key(request)}", limit=_REGISTER_LIMIT)

    email_normalised = email.lower()
    existing = db.scalar(select(User).where(User.email == email_normalised, User.status == "active"))
    if existing is not None:
        raise ProblemError("conflict", "Email already registered", instance=request.url.path)

    user = register_user(db, email=email, password=password, name=name)
    db.flush()
    account = db.get(Account, user.account_id)
    if account is None:  # pragma: no cover - register_user always creates or attaches one
        raise ProblemError("validation_error", "Invalid request", instance=request.url.path)

    token = make_verification_token(user)
    send_verification_email(email_port, user, token=token)

    ip_prefix = iter_client_ip_prefix(request)
    _row, cookie_value = create_session(db, user, ip_prefix=ip_prefix)
    set_session_cookie(response, cookie_value, request)

    data: dict[str, Any] = {
        "user": serialize_user(user, account_public_id=account.public_id),
        "account": serialize_account(account),
        "email_verified": False,
        "verification_sent": True,
    }
    if getattr(email_port, "dry_run", False):
        data["dev_verification_url"] = _verification_url(token)
    return build_envelope(
        data,
        meta=build_meta(lag_days=0, tier=account.entitlement),
        licence_summary=build_licence_summary([]),
    )


# ------------------------------------------------------------------------------------------ login
@router.post("/v1/auth/login")
def login(
    body: dict[str, Any],
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> Any:
    email = str(body.get("email") or "")
    password = str(body.get("password") or "")

    _rate_limit(f"auth-login:{_client_ip_key(request)}", limit=_LOGIN_LIMIT)

    user = authenticate_user(db, email=email, password=password)
    if user is None:
        raise ProblemError("unauthenticated", "Invalid email or password", instance=request.url.path)
    account = db.get(Account, user.account_id)
    if account is None:  # pragma: no cover - every active user has an account row
        raise ProblemError("unauthenticated", "Invalid email or password", instance=request.url.path)

    active_sessions = _active_session_count(db, account)
    if active_sessions >= account.seats:
        raise ProblemError(
            "seat_limit",
            "Seat limit reached",
            detail=(
                f"This account has {account.seats} seat"
                f"{'s' if account.seats != 1 else ''} and {active_sessions} active "
                f"session{'s' if active_sessions != 1 else ''}. Sign out another session or add a "
                "seat."
            ),
            instance=request.url.path,
        )

    ip_prefix = iter_client_ip_prefix(request)
    _row, cookie_value = create_session(db, user, ip_prefix=ip_prefix)
    set_session_cookie(response, cookie_value, request)

    data: dict[str, Any] = {
        "user": serialize_user(user, account_public_id=account.public_id),
        "account": serialize_account(account),
        "email_verified": user.email_verified_at is not None,
    }
    return build_envelope(
        data,
        meta=build_meta(lag_days=0, tier=account.entitlement),
        licence_summary=build_licence_summary([]),
    )


# ----------------------------------------------------------------------------------------- logout
@router.post("/v1/auth/logout", status_code=204)
def logout(
    db: Annotated[Session, Depends(get_db)],
    session: Annotated[str | None, Cookie()] = None,
) -> Response:
    if not session:
        raise ProblemError("unauthenticated", "Authentication required")
    row = resolve_session_row(db, session)
    if row is None:
        raise ProblemError("unauthenticated", "Authentication required")
    revoke_session(db, row)
    response = Response(status_code=204)
    clear_session_cookie(response)
    return response


# ----------------------------------------------------------------------------------------- verify
@router.get("/v1/auth/verify")
def verify_email(
    token: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Any:
    result = read_verification_token(token)
    if result is None:
        raise ProblemError(
            "validation_error",
            "Invalid request",
            detail="This verification link is invalid or has expired.",
            errors=[{"field": "token", "message": "invalid or expired"}],
            instance=request.url.path,
        )
    uid, _email = result
    user = db.scalar(select(User).where(User.public_id == uid))
    if user is None:
        raise ProblemError(
            "validation_error",
            "Invalid request",
            detail="This verification link is invalid or has expired.",
            errors=[{"field": "token", "message": "invalid or expired"}],
            instance=request.url.path,
        )
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
        db.flush()
    return {"verified": True}


# ------------------------------------------------------------------------------ resend-verification
def _require_session_user(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> User:
    if ctx.user is None:
        raise ProblemError("unauthenticated", "A signed-in session is required")
    return ctx.user


@router.post("/v1/auth/resend-verification", status_code=202)
def resend_verification(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(_require_session_user)],
    email_port: Annotated[EmailPort, Depends(get_email_port)],
) -> Any:
    _rate_limit(f"auth-resend:{_client_ip_key(request)}", limit=_RESEND_LIMIT)

    if user.email_verified_at is not None:
        response.status_code = 200
        return {"verified": True}

    token = make_verification_token(user)
    send_verification_email(email_port, user, token=token)
    data: dict[str, Any] = {"verification_sent": True}
    if getattr(email_port, "dry_run", False):
        data["dev_verification_url"] = _verification_url(token)
    return data
