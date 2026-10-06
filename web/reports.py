"""`POST /report`: "Report a problem with this record" (designer audit 2026-09-30 D-6; US-204 AC1).

Until 2026-10-06 the link under a record was `mailto:?subject=...` with no recipient, so a reader's
report of a wrong merge went nowhere. The form now relays to the API's existing public write,
`POST /v1/reports` (services/api/admin_posts.py), which creates an admin task of type `report` and
nothing else. This module adds no store, no queue and no second validation rulebook: the API
decides what is valid and the page prints its answer.

Abuse handling is the intake's, end to end:

* the per-visitor rate limit (`INTAKE_RATE_LIMIT`, shared by intake and reports) applies to the
  reader's own address, because `web/api_client.py` sends it as `X-Visitor-IP` on every call;
* the `website` honeypot field is rendered hidden and relayed as typed, so the API's
  `_reject_honeypot` sees exactly what a bot filled in;
* the Cloudflare Turnstile widget is rendered when `TURNSTILE_SITE_KEY` is set, and its token is
  relayed as `captcha_token`. Whether `/v1/reports` verifies that token is the API's decision
  (today it does not; the intake routes do -- recorded as an open item for the backend lane);
* a cross-site POST is refused (`web/auth.py::_is_same_origin`, the site's one CSRF rule).

States follow docs/31 §6: success says what happens next; an error prints the API problem's
`title` and `request_id`, keeps what the reader typed, and names the field at fault.

No personal data is kept here. The optional email goes to the API as typed and only there.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from web.api_client import ApiError
from web.auth import _csrf_rejection, _is_same_origin
from web.labels import REPORT_ISSUE_LABELS
from web.page import get_api, templates

router = APIRouter()

#: The API's issue types (services/api/admin_posts.py REPORT_ISSUE_TYPES), in the order the form
#: offers them, with the words a reader sees (web/labels.py).
ISSUE_TYPES: tuple[str, ...] = tuple(REPORT_ISSUE_LABELS)
DESCRIPTION_MAX = 2000
#: The record kinds a report can name, by public-id prefix, and where each one's page lives.
_PREFIXES = {"prop_": "/proposals/", "opp_": "/opportunities/", "org_": "/organizations/"}

#: The client-side Turnstile script, loaded only when a site key is configured.
TURNSTILE_SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js"


def turnstile_site_key() -> str | None:
    value = os.environ.get("TURNSTILE_SITE_KEY", "").strip()
    return value or None


def report_context(public_id: str, record_name: str, return_path: str, **state: Any) -> dict[str, Any]:
    """Everything `partials/_report_problem.html` reads, for a record page or for this route."""
    return {
        "public_id": public_id,
        "record_name": record_name,
        "return_path": return_path,
        "issue_types": [(value, REPORT_ISSUE_LABELS[value]) for value in ISSUE_TYPES],
        "description_max": DESCRIPTION_MAX,
        "site_key": turnstile_site_key(),
        "turnstile_script": TURNSTILE_SCRIPT,
        "typed": state.get("typed") or {},  # not "values": Jinja would find dict.values
        "errors": state.get("errors") or {},
        "problem": state.get("problem"),
        "sent": state.get("sent", False),
        "request_id": state.get("request_id"),
    }


def _safe_return_path(public_id: str, value: str | None) -> str:
    """Back to the record's own page only: a site path under the collection the id names."""
    prefix = next((path for id_prefix, path in _PREFIXES.items() if public_id.startswith(id_prefix)), None)
    if value and prefix and value.startswith(prefix) and not value.startswith("//"):
        return value
    return prefix + public_id if prefix else "/"


def _field_errors(body: dict[str, Any]) -> dict[str, str]:
    """`errors[]` of an RFC 9457 validation problem as `{field: message in words}`."""
    words = {
        "issue_type": "Choose what is wrong.",
        "description": f"Describe the problem in up to {DESCRIPTION_MAX:,} characters.",
        "email": "Enter an email address like name@example.com, or leave it empty.",
        "captcha_token": "Complete the check that you are not a robot, then send again.",
        "website": "This report could not be accepted.",
    }
    out: dict[str, str] = {}
    for err in body.get("errors") or []:
        field = str(err.get("field") or "")
        out[field] = words.get(field) or str(err.get("message") or "This field is not valid.")
    return out


@router.post("/report", response_class=HTMLResponse)
def submit_report(
    request: Request,
    public_id: str = Form(""),
    issue_type: str = Form(""),
    description: str = Form(""),
    email: str = Form(""),
    website: str = Form(""),
    return_path: str = Form(""),
    record_name: str = Form(""),
    captcha_token: str = Form("", alias="cf-turnstile-response"),
) -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    back = _safe_return_path(public_id, return_path)
    values = {"issue_type": issue_type, "description": description, "email": email}
    payload: dict[str, Any] = {
        "public_id": public_id,
        "issue_type": issue_type,
        "description": description.strip(),
        "website": website,
    }
    if email.strip():
        payload["email"] = email.strip()
    if captcha_token:
        payload["captcha_token"] = captcha_token
    try:
        result = get_api(request).post("/v1/reports", json=payload)
        status, body = result.status_code, result.body
    except ApiError as exc:
        status, body = exc.status_code, exc.body
    except httpx.HTTPError:  # an unreachable API is an error state, never a stack trace
        status, body = 503, {"title": "The report service is unavailable"}

    if status < 300:
        context = report_context(
            public_id, record_name, back, sent=True, request_id=body.get("request_id") or body.get("task_id")
        )
        http_status = 200
    else:
        errors = _field_errors(body) if status == 400 else {}
        problem = {
            "title": body.get("title") or "The report could not be sent",
            "request_id": body.get("request_id") or (body.get("meta") or {}).get("request_id"),
            "rate_limited": status == 429,
            "not_found": status == 404,
        }
        context = report_context(public_id, record_name, back, typed=values, errors=errors, problem=problem)
        http_status = 200 if status == 400 else status if status in (404, 429) else 502
    fragment = request.headers.get("x-report-fragment") == "1"
    template = "partials/_report_problem.html" if fragment else "report_problem.html"
    return templates.TemplateResponse(request, template, {"report": context}, status_code=http_status)
