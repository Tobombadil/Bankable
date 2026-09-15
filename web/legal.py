"""Public legal pages -- the two items `docs/40-launch-runbook.md` §4 named as hard blockers on
US-908's release gate: `GET /privacy` (row 5, "A `/privacy` page exists on the public site") and
`GET`/`POST /unsubscribe` (row 9, the human-facing side of `services/api/unsubscribe_routes.py`'s
token endpoint -- US-908 AC1, US-502 AC3).

Same page-rendering/`get_api`/CSRF pattern `web/auth.py` documents at length, including that
module's own stated reason for keeping a private copy of `get_api`/`is_preview_active`/
`footer_lag_days`/`Jinja2Templates` rather than importing them from `web/app.py`: `web/app.py`
mounts this router the same way (`app.include_router(web.legal.router)`), so importing back from
it at module level would be circular. This module duplicates that same small amount of glue rather
than importing the private, underscore-prefixed CSRF helpers out of `web/auth.py` -- one more
small, deliberate duplication of a few lines, not a second design.

**Privacy page content sources** (task instructions; nothing here is invented beyond what those
name): the data inventory is `docs/20-architecture.md` §11's "Personal data" paragraph plus this
task's own enumeration (accounts, alerts, intake/reports, CRM); the retention/deletion route cites
US-910 and mirrors docs/20 §11's "user row is anonymised, sessions and API keys revoked, alerts
suppressed, SoR deletion issued through the port"; the lawful bases follow
`docs/13-legal-outreach-and-social.md` §7.2's five-item list and its CAN-SPAM/CASL sections; the
postal address is the physical-address requirement that same document's §1 and §7.2 item 5 state
(CAN-SPAM: "Your message must include your valid physical postal address"; CASL/GDPR/UK-GDPR:
postal address and a privacy-notice link) -- like `infraque.com` (`services/api/common.py`), a
literal placeholder token this build has not resolved yet, not a real address.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from services.api.common import DOMAIN
from web.api_client import ApiClient, build_client

router = APIRouter()

_WEB_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_WEB_ROOT / "templates"))

#: CAN-SPAM/CASL/GDPR all require a real physical postal address in outbound mail and/or the
#: privacy notice (docs/13 §1, §7.2 item 5); the operator has not set one yet, so this is a
#: literal placeholder token in the same spirit as `services.api.common.DOMAIN` -- rendered
#: through a template variable, never written directly into template markup, so Jinja does not
#: try to parse the double braces as its own expression syntax.
POSTAL_ADDRESS_PLACEHOLDER = "{{POSTAL_ADDRESS}}"

#: docs/40-launch-runbook.md §4 row 5 names this as the privacy-notice contact route; US-910 AC1
#: is the deletion process it points to.
PRIVACY_EMAIL = f"privacy@{DOMAIN}"

LAST_UPDATED = "2026-09-13"


# ------------------------------------------------------------------- duplicated web/app.py glue
# (Identical to `web/auth.py`'s own copy of these three functions -- see that module's docstring
# for why each page-rendering router keeps one rather than importing from `web/app.py`.)
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
    """Identical rule to `web/auth.py`'s `_is_same_origin`: a same-origin `Origin` header, or (when
    absent) a same-origin `Referer`; anything else, including neither header present, fails
    closed."""
    candidate = request.headers.get("origin") or request.headers.get("referer")
    if not candidate:
        return False
    parsed = urlsplit(candidate)
    if not parsed.scheme or not parsed.netloc:
        return False
    return (parsed.scheme, parsed.netloc) == (request.url.scheme, request.url.netloc)


def _csrf_rejection() -> PlainTextResponse:
    return PlainTextResponse("Forbidden: this request did not come from the same site.", status_code=403)


# --------------------------------------------------------------------------------------- privacy
@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request) -> HTMLResponse:
    context = {
        "privacy_email": PRIVACY_EMAIL,
        "postal_address": POSTAL_ADDRESS_PLACEHOLDER,
        "last_updated": LAST_UPDATED,
    }
    return templates.TemplateResponse(request, "legal/privacy.html", context)


# ----------------------------------------------------------------------------------- unsubscribe
def _unsubscribe_context(token: str, *, status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "token": token,
        "resolved": True,
        "ok": status_code == 200,
        "body": body,
    }


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(request: Request) -> HTMLResponse:
    """Task: "`GET /unsubscribe?token=…` calls the API endpoint and renders confirmation or the
    not-found text" -- a bare `/unsubscribe` (no token, e.g. someone navigating there directly)
    renders the token-entry form instead of calling the API with an empty token."""
    token = request.query_params.get("token", "")
    if not token:
        return templates.TemplateResponse(request, "legal/unsubscribe.html", {"token": "", "resolved": False})
    api = get_api(request)
    result = api.get_result("/v1/alerts/unsubscribe", params={"token": token})
    status_code = result.status_code if result.status_code >= 400 else 200
    context = _unsubscribe_context(token, status_code=result.status_code, body=result.body)
    return templates.TemplateResponse(request, "legal/unsubscribe.html", context, status_code=status_code)


@router.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_submit(
    request: Request,
    token: Annotated[str, Form()] = "",
) -> Response:
    """Task: "`POST /unsubscribe` from a confirm button also works (same-origin check as
    `web/auth.py`)" -- the token-entry form's fallback submit path, guarded the same way every
    other state-changing route in this codebase is (`web/auth.py`'s CSRF docstring)."""
    if not _is_same_origin(request):
        return _csrf_rejection()
    api = get_api(request)
    result = api.post("/v1/alerts/unsubscribe", json={"token": token})
    status_code = result.status_code if result.status_code >= 400 else 200
    context = _unsubscribe_context(token, status_code=result.status_code, body=result.body)
    return templates.TemplateResponse(request, "legal/unsubscribe.html", context, status_code=status_code)


__all__ = ["router"]
