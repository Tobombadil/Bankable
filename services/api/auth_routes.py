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
4. **The seat limit applies to paid accounts only** (owner decision 2026-09-30; audit PM-4). A
   seat is a unit of a paid subscription (US-602 is a Pro-subscriber story; `account.seats` is the
   subscription mirror's count). A free account has no seat to buy under either posture, so
   counting its sessions against `seats = 1` only ever locked a registered reader to one device
   for up to 30 days, with "add a seat" as the remedy. `SEAT_LIMITED_ENTITLEMENTS` is the paid
   set; `admin` is a manual operator grant, not a subscription, and is not seat-limited either.
   Keyed on the entitlement rather than on the posture: a posture rule would still lock free
   readers out under `commercial` and would release paying accounts from their seat count
   under `noncommercial`, and neither is what a seat means.
5. **Lockout recovery** (QA-5, 2026-09-30). `POST /v1/auth/login` takes `sign_out_other_sessions:
   true`: after the password is verified, the user's own other sessions are revoked before the seat
   check, so a paid user whose old browser still holds the seat can get back in (other users'
   sessions on the account are never touched). `POST /v1/auth/sessions/revoke-others` does the same
   from a signed-in session. `POST /v1/auth/password-reset/request` always answers `202`, whether or
   not the address has an account (no enumeration), and emails a signed one-hour link bound to the
   current password hash (`services/api/auth.py::make_password_reset_token`), so it works once;
   `POST /v1/auth/password-reset` sets the new password and revokes every session of that user.
6. `dev_verification_url` is included in `register`/`resend-verification` responses only when the
   process-wide `EmailPort` is a dry-run `ResendEmailAdapter` (no `RESEND_API_KEY` configured) —
   documented on the field in `api/fragments/auth.yaml`, never present against a real provider.
7. **A member deletes their own account** (2026-10-09; docs/40 §4 row 6): `DELETE /v1/me` with
   `{"password": ...}`. Sign-in here is by password, so re-entering it is the re-authentication: a
   stolen cookie alone cannot erase an account, and a wrong password is `400 validation_error`
   naming `password` (not `401`: the session is still valid). Attempts are rate-limited per user.
   A session is required; an API key cannot delete the account that issued it, as it cannot mint
   keys. The body takes `password` only, and the route acts on the session's own user, so there is
   no way to name someone else. Staff roles (`operator`, `legal`, `owner`) are refused with `409`:
   an owner deleting themselves could lock every operator out of `/admin`, so a staff member is
   demoted by another owner first. The erasure is `services/api/account_erasure.py::erase_user`,
   the same procedure an operator's completed deletion task runs; its docstring lists what each
   table loses and keeps. A `deletion_request` task is written as the record of the request (the
   rights-request log of docs/13-legal-outreach-and-social.md §8.3), `done` unless the billing side
   still needs a human, in which case it stays `open` for the operator. Every session of the user
   is deleted and the response clears the cookie.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.account_erasure import erase_user
from services.api.auth import (
    AuthContext,
    EmailPort,
    ResendEmailAdapter,
    authenticate_user,
    create_session,
    get_auth_context,
    hash_password,
    iter_client_ip_prefix,
    make_password_reset_token,
    make_verification_token,
    password_fingerprint,
    read_password_reset_token,
    read_verification_token,
    register_user,
    resolve_session_row,
    revoke_session,
    revoke_user_sessions,
    send_password_reset_email,
    send_verification_email,
    verify_password,
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
from services.db.models import PRIVACY_REQUEST_RESPONSE_DAYS, Account, Task, User, UserSession
from services.ids import public_id
from services.sor.ports import BillingPort, CrmPort, SorUnavailable
from services.sor.wiring import get_billing_port, get_crm_port

router = APIRouter()

SESSION_COOKIE_NAME = "session"
SESSION_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 3600  # matches services/api/auth.py _SESSION_IDLE_DAYS

_REGISTER_LIMIT = 10
_LOGIN_LIMIT = 20
_RESEND_LIMIT = 5
_RESET_LIMIT = 5
#: Password attempts on `DELETE /v1/me` per user per window (decision 7): enough for typos, too few
#: to guess a password with a stolen cookie.
_DELETE_ACCOUNT_LIMIT = 5
#: Roles that cannot delete themselves in-app (decision 7); another owner demotes them first.
STAFF_ROLES = frozenset({"operator", "legal", "owner"})
#: Reason recorded on the audit event and in the CRM task for an in-app deletion. A constant, so
#: nothing the member types reaches the append-only log.
SELF_DELETION_REASON = "Account holder deleted their own account in-app"
_MIN_PASSWORD_LENGTH = 12
_MAX_EMAIL_LENGTH = 254
#: Entitlements whose logins count against `account.seats` (module docstring, decision 4).
SEAT_LIMITED_ENTITLEMENTS = frozenset({"pro", "api"})


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

    if body.get("sign_out_other_sessions") is True:
        revoke_user_sessions(db, user)
    if account.entitlement in SEAT_LIMITED_ENTITLEMENTS:
        active_sessions = _active_session_count(db, account)
        if active_sessions >= account.seats:
            raise ProblemError(
                "seat_limit",
                "Seat limit reached",
                detail=(
                    f"This account has {account.seats} seat"
                    f"{'s' if account.seats != 1 else ''} and {active_sessions} active "
                    f"session{'s' if active_sessions != 1 else ''}. Sign out another session, sign in "
                    "again with sign_out_other_sessions to end your own other sessions, or add a seat."
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


# ---------------------------------------------------------------- lockout recovery (decision 5)
@router.post("/v1/auth/sessions/revoke-others")
def revoke_other_sessions(
    db: Annotated[Session, Depends(get_db)],
    session: Annotated[str | None, Cookie()] = None,
) -> Any:
    row = resolve_session_row(db, session) if session else None
    if row is None:
        raise ProblemError("unauthenticated", "A signed-in session is required")
    user = db.get(User, row.user_id)
    if user is None:  # pragma: no cover - a session row always names its user
        raise ProblemError("unauthenticated", "A signed-in session is required")
    return {"revoked": revoke_user_sessions(db, user, keep=row)}


def _reset_url(token: str) -> str:
    return f"{WEB_HOST}/reset-password?token={token}"


@router.post("/v1/auth/password-reset/request", status_code=202)
def request_password_reset(
    body: dict[str, Any],
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    email_port: Annotated[EmailPort, Depends(get_email_port)],
) -> Any:
    email = str(body.get("email") or "")
    if _email_error(email):
        raise ProblemError(
            "validation_error",
            "Invalid request",
            errors=[{"field": "email", "message": _email_error(email) or ""}],
            instance=request.url.path,
        )
    _rate_limit(f"auth-reset:{_client_ip_key(request)}", limit=_RESET_LIMIT)
    data: dict[str, Any] = {"reset_requested": True}
    user = db.scalar(select(User).where(User.email == email.lower(), User.status == "active"))
    if user is not None and user.password_hash:
        url = _reset_url(make_password_reset_token(user))
        send_password_reset_email(email_port, user, url=url)
        if getattr(email_port, "dry_run", False):
            data["dev_reset_url"] = url
    return data


@router.post("/v1/auth/password-reset")
def reset_password(
    body: dict[str, Any],
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Any:
    _rate_limit(f"auth-reset:{_client_ip_key(request)}", limit=_RESET_LIMIT)
    invalid = ProblemError(
        "validation_error",
        "Invalid request",
        detail="This reset link is invalid, expired or already used. Ask for a new one.",
        errors=[{"field": "token", "message": "invalid, expired or already used"}],
        instance=request.url.path,
    )
    token = str(body.get("token") or "")
    result = read_password_reset_token(token) if token else None
    if result is None:
        raise invalid
    uid, fingerprint = result
    user = db.scalar(select(User).where(User.public_id == uid, User.status == "active"))
    if user is None or password_fingerprint(user) != fingerprint:
        raise invalid
    password = str(body.get("password") or "")
    password_err = _password_error(password)
    if password_err:
        raise ProblemError(
            "validation_error",
            "Invalid request",
            errors=[{"field": "password", "message": password_err}],
            instance=request.url.path,
        )
    user.password_hash = hash_password(password)
    revoked = revoke_user_sessions(db, user)
    db.flush()
    return {"password_reset": True, "sessions_revoked": revoked}


# ------------------------------------------------------------- delete one's own account (decision 7)
def _invalid_body(request: Request, field: str, message: str, detail: str | None = None) -> ProblemError:
    return ProblemError(
        "validation_error",
        "Invalid request",
        detail=detail,
        errors=[{"field": field, "message": message}],
        instance=request.url.path,
    )


@router.delete("/v1/me")
def delete_me(
    body: dict[str, Any],
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    crm: Annotated[CrmPort, Depends(get_crm_port)],
    billing: Annotated[BillingPort, Depends(get_billing_port)],
) -> Any:
    user = ctx.user
    if user is None:  # no session (an API key alone resolves no `ctx.user`)
        raise ProblemError("unauthenticated", "A signed-in session is required", instance=request.url.path)
    unknown = sorted(set(body) - {"password"})
    if unknown:  # e.g. a `user_id`: this operation only ever acts on the session's own user
        raise _invalid_body(request, unknown[0], "not accepted by this operation")
    password = body.get("password")
    if not isinstance(password, str) or not password:
        raise _invalid_body(request, "password", "is required")
    if user.role in STAFF_ROLES:
        raise ProblemError(
            "conflict",
            "Staff accounts cannot be deleted in-app",
            detail=(
                f"This account has the {user.role!r} role. Ask an owner to change your role to member "
                "first, or to delete the account from the admin panel."
            ),
            instance=request.url.path,
        )
    _rate_limit(f"auth-delete:{user.id}", limit=_DELETE_ACCOUNT_LIMIT)
    if user.password_hash is None or not verify_password(user.password_hash, password):
        raise _invalid_body(
            request,
            "password",
            "does not match this account",
            detail="The password does not match this account. Nothing was deleted.",
        )

    try:
        result = erase_user(
            db,
            user,
            actor=user,
            reason=SELF_DELETION_REASON,
            initiated_by="account_holder",
            crm=crm,
            billing=billing,
        )
    except SorUnavailable as exc:
        raise ProblemError(
            "sor_unavailable",
            "Account deletion is unavailable",
            detail=(
                "A service the deletion depends on could not be reached. Nothing was deleted; "
                "try again shortly."
            ),
            instance=request.url.path,
        ) from exc

    now = utcnow()
    pending = result.billing_pending
    task = Task(
        public_id="",
        type="deletion_request",
        subject_type="user",
        subject_id=user.id,
        status="open" if pending else "done",
        due_at=now + dt.timedelta(days=PRIVACY_REQUEST_RESPONSE_DAYS),
        created_by_user_id=user.id,
        completed_at=None if pending else now,
        notes=(
            "Deleted in-app by the account holder. Remaining: cancel subscription(s) "
            f"{', '.join(str(r) for r in result.billing.get('subscription_refs', []))} in the billing "
            "provider, then mark this task done."
            if pending
            else "Deleted in-app by the account holder; nothing remains."
        ),
        audit_event_ids=[public_id("evt", result.event.id)],
    )
    db.add(task)
    db.flush()
    task.public_id = public_id("task", task.id)
    db.flush()

    clear_session_cookie(response)
    data: dict[str, Any] = {
        "deleted": True,
        "user_id": user.public_id,
        "deletion_task_id": task.public_id,
        "sessions_deleted": result.sessions_deleted,
        "api_keys_revoked": result.api_keys_revoked,
        "saved_searches_paused": result.saved_searches_paused,
        "alerts_redacted": result.alerts_redacted,
        "webhooks_disabled": result.webhooks_disabled,
        "account_closed": result.account_closed,
        "billing": str(result.billing.get("billing")),
    }
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier="public"), licence_summary=build_licence_summary([])
    )
