"""Login and registration surface (Sprint 3 "login and registration surface", first wave) on top
of `services/api`'s `/v1/auth/*` endpoints (`services/api/auth_routes.py`) and `GET /v1/me`
(`services/api/pro.py`). Plain HTML `<form>` POSTs -- no client-side JavaScript, no htmx -- since a
credential form is exactly the case docs/31's "no JS-only interaction" default is for.

**Two-host cookie relay.** In production the web host and the API host are separate deployables
(`docs/20-architecture.md`); a browser only ever talks to the web host. Every route below forwards
the browser's own `session` cookie to the API on requests that need it, and relays every
`Set-Cookie` the API sends back onto its own response unchanged (same cookie name, same
attributes) so the *browser* ends up holding one cookie, set by whichever host answered last, and
either host resolves it identically (`services/api/auth.py`'s cookie is a signed, opaque value --
the web host never needs to parse or mint it, only carry it). Locally (`web/dev_up.py`'s
in-process mode) this relay is a no-op in effect but the code path is identical, so there is only
one cookie-handling design to test (`web/test_auth.py`), not two.

**CSRF.** Every state-changing route here requires the request's `Origin` header (or `Referer`
when `Origin` is absent -- browsers omit `Origin` on some plain-navigation form posts) to name this
same origin, refusing with a `403` text response otherwise. `SameSite=Lax` on the session cookie
already blocks the cookie from riding along on a genuinely cross-site POST in a modern browser;
this header check is the second, origin-based layer for the residual cases `SameSite=Lax` does not
cover (a followed link/redirect chain, an older or misconfigured browser) — the standard
belt-and-braces pairing, not a full anti-CSRF token scheme (no server-side token state is
justified for a same-origin form given the cookie is already `SameSite=Lax` and `HttpOnly`).

**Deleting one's own account** (2026-10-09): `/account` links to `/account/delete`, a confirmation
step that says what is deleted and kept and asks for the password (sign-in here is by password, so
that is the re-authentication). Its form posts, under the same same-origin check, to
`DELETE /v1/me`; on success the browser's cookie is cleared and it is redirected to
`/account/deleted`. The API decides everything: the password, the staff-role refusal, the erasure.

**Why this module keeps its own `Jinja2Templates`/`get_api`/`is_preview_active`/`footer_lag_days`
instead of importing them from `web/app.py`:** `web/app.py` mounts this router
(`app.include_router(web.auth.router)`), so `web.auth` importing back from `web.app` at module
level would be a circular import. The duplicated helpers are small, read only `request.app.state`
(shared by every router mounted on the same `FastAPI` app regardless of which module defined the
route), and are the exact bodies `web/app.py` already has -- there is no second source of truth
for *behaviour*, only for these few lines of glue.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from web import labels
from web.api_client import ApiClient, build_client
from web.assets import ASSET_VERSION
from web.viewmodels import footer_build as vm_footer_build
from web.viewmodels import source_freshness as vm_source_freshness
from web.viewmodels import web_relative_url

router = APIRouter()

_WEB_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_WEB_ROOT / "templates"))
labels.install(templates.env)

SESSION_COOKIE_NAME = "session"


# ------------------------------------------------------------------- duplicated web/app.py glue
def get_api(request: Request) -> ApiClient:
    client: ApiClient | None = getattr(request.app.state, "api_client", None)
    if client is None:
        client = build_client()
        request.app.state.api_client = client
    return client


def is_preview_active(request: Request) -> bool:
    override = getattr(request.app.state, "preview_active", None)
    if override is not None:
        return bool(override)
    return os.environ.get("WEB_DEV_PREVIEW", "").strip().lower() in ("1", "true", "yes", "on")


def get_lag_days(request: Request) -> dict[str, int]:
    cached: dict[str, int] | None = getattr(request.app.state, "lag_days_default", None)
    if cached is None:
        health = get_api(request).get("/v1/health")
        cached = dict(health["lag_days_default"])
        request.app.state.lag_days_default = cached
    return cached


templates.env.globals["is_preview_active"] = is_preview_active
templates.env.globals["footer_lag_days"] = get_lag_days
templates.env.globals["footer_build"] = lambda request: vm_footer_build(request, get_api(request))
# The header, tier notice and footer state how current the sources are (web/viewmodels.py).
templates.env.globals["source_freshness"] = lambda request: vm_source_freshness(request, get_api(request))
templates.env.globals["asset_version"] = ASSET_VERSION


# ---------------------------------------------------------------------------------------- CSRF
def _is_same_origin(request: Request) -> bool:
    """A same-origin `Origin` header, or (when absent) a same-origin `Referer`. Neither present,
    or present but pointing elsewhere, fails closed."""
    candidate = request.headers.get("origin") or request.headers.get("referer")
    if not candidate:
        return False
    parsed = urlsplit(candidate)
    if not parsed.scheme or not parsed.netloc:
        return False
    return (parsed.scheme, parsed.netloc) == (request.url.scheme, request.url.netloc)


def _csrf_rejection() -> PlainTextResponse:
    return PlainTextResponse("Forbidden: this request did not come from the same site.", status_code=403)


# -------------------------------------------------------------------------------------- helpers
def _safe_next(value: str | None, *, default: str = "/account") -> str:
    """Only ever redirects within this site — an absolute URL or a protocol-relative `//host/...`
    is rejected in favour of `default` (open-redirect prevention)."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return default
    # Browsers normalise a backslash to a slash when following a Location header, so `/\evil.com`
    # resolves as `//evil.com` (web audit 2026-09-18); reject it, control characters, and anything
    # that parses to a host.
    if "\\" in value or "\r" in value or "\n" in value or urlsplit(value).netloc:
        return default
    return value


def _field_errors(body: dict[str, Any]) -> dict[str, str]:
    return {e["field"]: e["message"] for e in body.get("errors", []) if "field" in e and "message" in e}


def _relay_cookies(response: Response, set_cookie: list[str]) -> None:
    for value in set_cookie:
        response.headers.append("set-cookie", value)


def _post_registered_event(api: ApiClient, next_path: str) -> None:
    """Map task item 5: a sign-up that started from the map page (`?next=/...&layers=plants`,
    written by `map.js`'s `writeFilters` onto the header "Sign in" link so it survives the
    login/register round trip) is measurement for the layer toggle's engagement effect, not the
    account flow itself -- so this never blocks or shows on the redirect regardless of outcome.
    `next_path` is already `_safe_next`-validated (same-origin path + query only).

    `props.layers` is a single comma-joined string, not a list: `services/api/ui_events.py`'s
    `_ALLOWED_PROPS["auth.registered"]` types it `(str, 64)`, matching `docs/21` §3.21's
    `UI_EVENT_NAMES` table -- a JSON list would fail that endpoint's type check and the whole
    event would be silently dropped by the `except Exception` below."""
    raw_layers = parse_qs(urlsplit(next_path).query).get("layers", [])
    layers = [item for raw in raw_layers for item in raw.split(",") if item]
    try:
        api.post("/v1/ui-events", json={"name": "auth.registered", "props": {"layers": ",".join(layers)}})
    except Exception:  # noqa: S110 -- measurement must never block or surface an error here
        pass


# ---------------------------------------------------------------------------------------- login
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    next_path = _safe_next(request.query_params.get("next"))
    return templates.TemplateResponse(request, "auth/login.html", {"next": next_path})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/account",
    sign_out_other_sessions: Annotated[str, Form()] = "",
) -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    next_path = _safe_next(next)
    api = get_api(request)
    body: dict[str, Any] = {"email": email, "password": password}
    if sign_out_other_sessions:
        # QA-5 lockout recovery: end this user's own other sessions before the seat check.
        body["sign_out_other_sessions"] = True
    result = api.post("/v1/auth/login", json=body)
    if result.status_code == 200:
        redirect = RedirectResponse(url=next_path, status_code=303)
        _relay_cookies(redirect, result.set_cookie)
        return redirect
    context = {
        "next": next_path,
        "email": email,
        "sign_out_other_sessions": bool(sign_out_other_sessions) or result.body.get("code") == "seat_limit",
        "error_title": result.body.get("title", "Sign in failed"),
        "error_detail": result.body.get("detail"),
        "field_errors": _field_errors(result.body),
    }
    return templates.TemplateResponse(
        request, "auth/login.html", context, status_code=result.status_code or 400
    )


# ----------------------------------------------------------------------------------- register
@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request) -> HTMLResponse:
    next_path = _safe_next(request.query_params.get("next"))
    return templates.TemplateResponse(request, "auth/register.html", {"next": next_path})


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/account",
) -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    next_path = _safe_next(next)
    api = get_api(request)
    body: dict[str, Any] = {"email": email, "password": password}
    if name.strip():
        body["name"] = name.strip()
    result = api.post("/v1/auth/register", json=body)
    if result.status_code == 201:
        redirect = RedirectResponse(url=next_path, status_code=303)
        _relay_cookies(redirect, result.set_cookie)
        _post_registered_event(api, next_path)
        return redirect
    context = {
        "next": next_path,
        "email": email,
        "name": name,
        "error_title": result.body.get("title", "Registration failed"),
        "error_detail": result.body.get("detail"),
        "field_errors": _field_errors(result.body),
    }
    return templates.TemplateResponse(
        request, "auth/register.html", context, status_code=result.status_code or 400
    )


# ------------------------------------------------------------------------------------- logout
@router.post("/logout")
def logout(request: Request) -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    redirect = RedirectResponse(url="/", status_code=303)
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        api = get_api(request)
        result = api.post("/v1/auth/logout", cookies={SESSION_COOKIE_NAME: cookie})
        _relay_cookies(redirect, result.set_cookie)
    # Belt-and-braces: clear the cookie on our own response too, in case the API had nothing to
    # revoke (an already-expired or already-revoked cookie) so the browser is never left holding
    # a session cookie this site itself just told the user to sign out of.
    redirect.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return redirect


# ------------------------------------------------------------------------------------- verify
@router.get("/verify", response_class=HTMLResponse)
def verify(request: Request) -> HTMLResponse:
    token = request.query_params.get("token", "")
    api = get_api(request)
    result = api.get_result("/v1/auth/verify", params={"token": token})
    context = {
        "verified": result.status_code == 200 and bool(result.body.get("verified")),
        "problem": result.body if result.status_code >= 400 else None,
    }
    status_code = 200 if context["verified"] else (result.status_code if result.status_code >= 400 else 400)
    return templates.TemplateResponse(request, "auth/verify.html", context, status_code=status_code)


# ------------------------------------------------------------------------------------- account
@router.get("/account", response_class=HTMLResponse)
def account(request: Request) -> Response:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return RedirectResponse(url="/login?next=/account", status_code=303)
    api = get_api(request)
    result = api.get_result("/v1/me", cookies={SESSION_COOKIE_NAME: cookie})
    if result.status_code != 200:
        return RedirectResponse(url="/login?next=/account", status_code=303)
    return templates.TemplateResponse(request, "auth/account.html", {"me": result.body.get("data", {})})


@router.post("/account/resend", response_class=HTMLResponse)
def account_resend(request: Request) -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return RedirectResponse(url="/login?next=/account", status_code=303)
    api = get_api(request)
    me_result = api.get_result("/v1/me", cookies={SESSION_COOKIE_NAME: cookie})
    if me_result.status_code != 200:
        return RedirectResponse(url="/login?next=/account", status_code=303)
    resend_result = api.post("/v1/auth/resend-verification", cookies={SESSION_COOKIE_NAME: cookie})
    resend_body = dict(resend_result.body)
    dev_url = resend_body.get("dev_verification_url")
    if dev_url:
        # The API's `dev_verification_url` is built from `services.api.common.WEB_HOST`, still the
        # literal `infraque.com` placeholder (docs/00-PLAN.md) -- not a navigable link on whatever
        # host this site is actually served from. `web_relative_url` is the same fix
        # `web/viewmodels.py` already applies to every API-supplied `url` field the map renders.
        resend_body["dev_verification_url"] = web_relative_url(dev_url)
    context = {
        "me": me_result.body.get("data", {}),
        "resend_status": resend_result.status_code,
        "resend_body": resend_body,
    }
    return templates.TemplateResponse(request, "auth/account.html", context)


# ------------------------------------------------- lockout recovery (QA-5, lane A1, 2026-09-30)
@router.post("/account/sign-out-others", response_class=HTMLResponse)
def account_sign_out_others(request: Request) -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return RedirectResponse(url="/login?next=/account", status_code=303)
    api = get_api(request)
    result = api.post("/v1/auth/sessions/revoke-others", cookies={SESSION_COOKIE_NAME: cookie})
    me_result = api.get_result("/v1/me", cookies={SESSION_COOKIE_NAME: cookie})
    if result.status_code != 200 or me_result.status_code != 200:
        return RedirectResponse(url="/login?next=/account", status_code=303)
    context = {
        "me": me_result.body.get("data", {}),
        "others_signed_out": int(result.body.get("revoked") or 0),
    }
    return templates.TemplateResponse(request, "auth/account.html", context)


# ---------------------------------------------------- delete one's own account (2026-10-09)
#: Display-only mirror of `services/api/auth_routes.py::STAFF_ROLES`: these roles see why the form is
#: not offered instead of a form the API would refuse with `409`. The API is the one enforcing it.
_STAFF_ROLES = frozenset({"operator", "legal", "owner"})


def _account_delete_page(
    request: Request, me: dict[str, Any], *, status_code: int = 200, **extra: Any
) -> HTMLResponse:
    role = str(me.get("user", {}).get("role", ""))
    context = {"me": me, "is_staff": role in _STAFF_ROLES, **extra}
    return templates.TemplateResponse(request, "auth/account_delete.html", context, status_code=status_code)


@router.get("/account/delete", response_class=HTMLResponse)
def account_delete_form(request: Request) -> Response:
    """The confirmation step: what goes, what stays, and the password field. Nothing is deleted
    until the form below is posted with the right password."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return RedirectResponse(url="/login?next=/account/delete", status_code=303)
    me_result = get_api(request).get_result("/v1/me", cookies={SESSION_COOKIE_NAME: cookie})
    if me_result.status_code != 200:
        return RedirectResponse(url="/login?next=/account/delete", status_code=303)
    return _account_delete_page(request, me_result.body.get("data", {}))


@router.post("/account/delete", response_class=HTMLResponse)
def account_delete_submit(request: Request, password: Annotated[str, Form()] = "") -> Response:
    """`DELETE /v1/me` with the password typed on the confirmation page. On success the API has
    deleted every session; this response clears the browser's cookie too and redirects to a page
    that says what happened, so a reload cannot resubmit and the header no longer shows "Account"."""
    if not _is_same_origin(request):
        return _csrf_rejection()
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return RedirectResponse(url="/login?next=/account/delete", status_code=303)
    api = get_api(request)
    cookies = {SESSION_COOKIE_NAME: cookie}
    result = api.request("DELETE", "/v1/me", json={"password": password}, cookies=cookies)
    if result.status_code == 200:
        redirect = RedirectResponse(url="/account/deleted", status_code=303)
        _relay_cookies(redirect, result.set_cookie)
        redirect.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return redirect
    if result.status_code == 401:
        return RedirectResponse(url="/login?next=/account/delete", status_code=303)
    me_result = api.get_result("/v1/me", cookies=cookies)
    if me_result.status_code != 200:
        return RedirectResponse(url="/login?next=/account/delete", status_code=303)
    return _account_delete_page(
        request,
        me_result.body.get("data", {}),
        status_code=result.status_code or 400,
        error_title=result.body.get("title", "Your account was not deleted"),
        error_detail=result.body.get("detail"),
        field_errors=_field_errors(result.body),
    )


@router.get("/account/deleted", response_class=HTMLResponse)
def account_deleted(request: Request) -> Response:
    """Shown after a deletion. A browser that is still signed in has an account, so it is sent to
    the account page rather than told its account is gone."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie:
        me_result = get_api(request).get_result("/v1/me", cookies={SESSION_COOKIE_NAME: cookie})
        if me_result.status_code == 200:
            return RedirectResponse(url="/account", status_code=303)
    return templates.TemplateResponse(request, "auth/account_deleted.html", {})


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "auth/forgot_password.html", {})


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(request: Request, email: Annotated[str, Form()] = "") -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    result = get_api(request).post("/v1/auth/password-reset/request", json={"email": email})
    if result.status_code == 202:
        dev_url = result.body.get("dev_reset_url")
        context = {
            "sent": True,
            "email": email,
            "dev_reset_url": web_relative_url(dev_url) if dev_url else None,
        }
        return templates.TemplateResponse(request, "auth/forgot_password.html", context)
    context = {
        "email": email,
        "error_title": result.body.get("title", "Could not send a reset link"),
        "error_detail": result.body.get("detail"),
        "field_errors": _field_errors(result.body),
    }
    return templates.TemplateResponse(
        request, "auth/forgot_password.html", context, status_code=result.status_code or 400
    )


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "auth/reset_password.html", {"token": request.query_params.get("token", "")}
    )


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password_submit(
    request: Request, token: Annotated[str, Form()] = "", password: Annotated[str, Form()] = ""
) -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    result = get_api(request).post("/v1/auth/password-reset", json={"token": token, "password": password})
    if result.status_code == 200:
        response = templates.TemplateResponse(request, "auth/reset_password.html", {"done": True})
        # Every session was revoked, this browser's included; drop its now-dead cookie too.
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response
    context = {
        "token": token,
        "error_title": result.body.get("title", "Could not reset the password"),
        "error_detail": result.body.get("detail"),
        "field_errors": _field_errors(result.body),
    }
    return templates.TemplateResponse(
        request, "auth/reset_password.html", context, status_code=result.status_code or 400
    )
