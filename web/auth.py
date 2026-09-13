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
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from web.api_client import ApiClient, build_client
from web.viewmodels import web_relative_url

router = APIRouter()

_WEB_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_WEB_ROOT / "templates"))

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
    return value


def _field_errors(body: dict[str, Any]) -> dict[str, str]:
    return {e["field"]: e["message"] for e in body.get("errors", []) if "field" in e and "message" in e}


def _relay_cookies(response: Response, set_cookie: list[str]) -> None:
    for value in set_cookie:
        response.headers.append("set-cookie", value)


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
) -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    next_path = _safe_next(next)
    api = get_api(request)
    result = api.post("/v1/auth/login", json={"email": email, "password": password})
    if result.status_code == 200:
        redirect = RedirectResponse(url=next_path, status_code=303)
        _relay_cookies(redirect, result.set_cookie)
        return redirect
    context = {
        "next": next_path,
        "email": email,
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
        # literal `{{DOMAIN}}` placeholder (docs/00-PLAN.md) -- not a navigable link on whatever
        # host this site is actually served from. `web_relative_url` is the same fix
        # `web/viewmodels.py` already applies to every API-supplied `url` field the map renders.
        resend_body["dev_verification_url"] = web_relative_url(dev_url)
    context = {
        "me": me_result.body.get("data", {}),
        "resend_status": resend_result.status_code,
        "resend_body": resend_body,
    }
    return templates.TemplateResponse(request, "auth/account.html", context)
