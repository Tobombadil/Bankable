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

**Privacy notice** (`/privacy`, redrafted 2026-10-07 from `docs/13-legal-data-rights.md` §5 for
the 2026-09-30 legal audit's L-7; counsel review pending, `docs/13` §7 item 17). The controller is
rendered from `SENDER_LEGAL_NAME` and `SENDER_POSTAL_ADDRESS`, the same two settings the alert mail
footer uses (`services/alerts/mail.py`; owner decision 2026-09-18 (5): rendered from config, never
hard-coded). When either is unset the page says so in plain words; it never prints a template token.

**Reuse conditions** (`/legal/reuse`, 2026-10-07, L-3): a factual summary, generated from
`data/sources.yaml`, of each published source's licence, the credit it requires and any duty that
passes to whoever reuses the data. Every `meta.terms_url` points here (`services/api/common.py`)
until counsel approves real customer terms and an API licence (`docs/13-legal-customer-terms.md`,
a draft). The page says it is a summary of other people's licences and not a contract.
`/legal/api-licence`, which every API response cited before and which never existed, answers a
301 to it so the old links resolve.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from services.api.common import DOMAIN
from web import labels
from web.api_client import ApiClient, build_client
from web.assets import ASSET_VERSION
from web.page import get_lag_days, paid_tiers_offered
from web.viewmodels import footer_build as vm_footer_build
from web.viewmodels import source_freshness as vm_source_freshness

router = APIRouter()

_WEB_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_WEB_ROOT / "templates"))
labels.install(templates.env)
templates.env.globals["asset_version"] = ASSET_VERSION

#: docs/40-launch-runbook.md §4 row 5 names this as the privacy-notice contact route; US-910 AC1
#: is the deletion process it points to.
PRIVACY_EMAIL = f"privacy@{DOMAIN}"

LAST_UPDATED = "2026-10-10"
REUSE_LAST_UPDATED = "2026-10-07"

_MANIFEST = Path(__file__).resolve().parents[1] / "data" / "sources.yaml"


def controller_identity() -> dict[str, str | None]:
    """The controller named on the privacy notice: `SENDER_LEGAL_NAME` and `SENDER_POSTAL_ADDRESS`,
    read at request time like `services/alerts/mail.py` reads them. `None` for an unset value; the
    template then says the value is not yet published rather than printing a placeholder."""
    name = os.environ.get("SENDER_LEGAL_NAME", "").strip() or None
    address = os.environ.get("SENDER_POSTAL_ADDRESS", "").strip() or None
    return {"name": name, "address": address}


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


templates.env.globals["is_preview_active"] = is_preview_active
templates.env.globals["footer_lag_days"] = get_lag_days
# The navigation leaves "Pricing" out under the noncommercial posture (`web/page.py`).
templates.env.globals["paid_tiers_offered"] = paid_tiers_offered
templates.env.globals["footer_build"] = lambda request: vm_footer_build(request, get_api(request))
# The header, tier notice and footer state how current the sources are (web/viewmodels.py).
templates.env.globals["source_freshness"] = lambda request: vm_source_freshness(request, get_api(request))


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
        "controller": controller_identity(),
        "last_updated": LAST_UPDATED,
    }
    return templates.TemplateResponse(request, "legal/privacy.html", context)


# ------------------------------------------------------------------------------- reuse conditions
#: The manifest's `reuse` classes whose rows can reach a reader, in the order the page lists them.
#: `restricted` and `unknown` sources publish nothing (the loader refuses them), so they have no
#: conditions to pass on and are counted, not listed.
_REUSE_ORDER = ("open", "attribution", "noncommercial")
_REUSE_LABELS = {
    "open": "Public domain or no conditions stated",
    "attribution": "Reuse with credit",
    "noncommercial": "Noncommercial reuse only",
}
_PUBLICATION_LABELS = {
    "raw_ok": "Records published as the source states them",
    "derived_only": "Derived fields only; the source's own rows are not republished",
}


def _pass_through_duties(entry: dict[str, Any]) -> list[str]:
    """What a person reusing this source's data from us must also do, in the source's terms as the
    register records them (`docs/13` §6, §6.3). Stated, not interpreted: each line comes from a
    manifest field or the reuse class."""
    duties: list[str] = []
    credit = str(entry.get("attribution") or "").strip()
    reuse = str(entry.get("reuse") or "")
    if credit:
        duties.append(f"Credit the source with: \u201c{credit}\u201d.")
    elif reuse in ("attribution", "noncommercial"):
        operator = str(entry.get("operator") or entry.get("name") or "the source")
        duties.append(f"Credit {operator} as the source.")
    if entry.get("changes_statement"):
        duties.append(
            f"Say what was changed. Our statement of changes: \u201c{entry['changes_statement']}\u201d."
        )
    if reuse == "noncommercial":
        duties.append(
            "Noncommercial use only, and the source's content must stay unaltered and not be presented in a "
            "misleading way (the source's own condition)."
        )
    if entry.get("publication") == "derived_only":
        duties.append("Do not republish the source's own table; link to the source for its rows.")
    return duties


@functools.lru_cache(maxsize=1)
def reuse_conditions() -> dict[str, Any]:
    """Every published source in `data/sources.yaml`, grouped by reuse class, with its licence, the
    credit it requires and the duties that pass to a reuser. Read from the file, not the store, so the
    page states the terms we hold even for a source with no rows loaded yet."""
    import yaml

    entries = (yaml.safe_load(_MANIFEST.read_text(encoding="utf-8")) or {}).get("sources") or []
    groups: dict[str, list[dict[str, Any]]] = {k: [] for k in _REUSE_ORDER}
    withheld = 0
    for entry in entries:
        reuse = str(entry.get("reuse") or "unknown")
        publication = str(entry.get("publication") or "none")
        if reuse not in groups or publication == "none":
            withheld += 1
            continue
        groups[reuse].append(
            {
                "source_id": str(entry.get("id")),
                "name": str(entry.get("name") or entry.get("id")),
                "operator": str(entry.get("operator") or ""),
                "url": str(entry.get("url") or ""),
                "licence_url": str(entry.get("licence_url") or ""),
                "publication": _PUBLICATION_LABELS.get(publication, publication),
                "duties": _pass_through_duties(entry),
            }
        )
    for rows in groups.values():
        rows.sort(key=lambda r: r["name"].lower())
    return {
        "groups": [
            {"reuse": k, "label": _REUSE_LABELS[k], "sources": groups[k]} for k in _REUSE_ORDER if groups[k]
        ],
        "withheld": withheld,
    }


@router.get("/legal/reuse", response_class=HTMLResponse)
def reuse_page(request: Request) -> HTMLResponse:
    context = {"last_updated": REUSE_LAST_UPDATED, **reuse_conditions()}
    return templates.TemplateResponse(request, "legal/reuse.html", context)


@router.get("/legal/api-licence")
def api_licence_redirect() -> RedirectResponse:
    """The URL every API response and CSV cited until 2026-10-07; no licence was ever served there.
    It now leads to what is actually true today (`/legal/reuse`)."""
    return RedirectResponse(url="/legal/reuse", status_code=301)


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


# --------------------------------------------------------------------------- privacy request
def _privacy_request_context(
    form: dict[str, str], *, submitted: bool, status_code: int = 0, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "form": form,
        "submitted": submitted,
        "ok": submitted and 200 <= status_code < 300,
        "body": body or {},
    }


@router.get("/privacy/request", response_class=HTMLResponse)
def privacy_request(request: Request) -> HTMLResponse:
    """The form behind the privacy notice's "someone named in a filing we index" sentence
    (`services/api/privacy_routes.py`; docs/50-audit-2026-09-18.md §3.1). Renders the empty form;
    the API is not called until the form is submitted."""
    return templates.TemplateResponse(
        request, "legal/privacy_request.html", _privacy_request_context({}, submitted=False)
    )


@router.post("/privacy/request", response_class=HTMLResponse)
def privacy_request_submit(
    request: Request,
    kind: Annotated[str, Form()] = "erasure",
    record_public_id: Annotated[str, Form()] = "",
    contact_email: Annotated[str, Form()] = "",
    message: Annotated[str, Form()] = "",
) -> Response:
    """Same-origin-guarded like every other state-changing route here; forwards to
    `POST /v1/privacy/requests` and re-renders the form with the API's own problem on a 4xx so
    the person can fix the field, or the request id on success."""
    if not _is_same_origin(request):
        return _csrf_rejection()
    form = {
        "kind": kind,
        "record_public_id": record_public_id.strip(),
        "contact_email": contact_email.strip(),
        "message": message.strip(),
    }
    api = get_api(request)
    result = api.post(
        "/v1/privacy/requests",
        json={
            "kind": form["kind"],
            "record_public_id": form["record_public_id"],
            "contact_email": form["contact_email"],
            "message": form["message"] or None,
        },
    )
    status_code = result.status_code if result.status_code >= 400 else 200
    context = _privacy_request_context(form, submitted=True, status_code=result.status_code, body=result.body)
    return templates.TemplateResponse(request, "legal/privacy_request.html", context, status_code=status_code)


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
