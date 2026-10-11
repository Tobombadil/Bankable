"""`GET`/`POST /feedback`: "Tell us what you were trying to do", the private beta's feedback form
(2026-10-11; docs/65 §(a)). The beta's testers share one login, so usage counts are aggregate only
and what testers say is the main signal; "Report a problem with this record" catches a wrong record
but not "I couldn't find what I was looking for". This form catches that.

It exists only in beta mode (`web/beta.py::beta_mode`, `SITE_ACCESS=basic`): outside it both routes
answer the site's 404, and no page links here. It reuses the report pipeline end to end and stores
nothing itself:

1. Refuses a cross-site POST (`web/auth.py::_is_same_origin`, the site's one CSRF rule).
2. Checks the three answers before spending a request, so a typo does not use up one of the five
   submissions an address may make per hour (`INTAKE_RATE_LIMIT`, shared with reports and intake).
3. Relays to `POST /v1/reports` with `issue_type: beta_feedback` and no record
   (services/api/admin_posts.py, beta feedback note; api/openapi.yaml `ReportRequest`). The API
   creates a `report` task with no subject, which the admin task queue lists as "Beta feedback"
   (`/admin/tasks`). The honeypot (`website`) is relayed as typed; the Turnstile token
   (`cf-turnstile-response`) is relayed as `captcha_token` when the deployment sets a site key;
   the visitor's address reaches the API's rate limiter as `X-Visitor-IP` (`web/api_client.py`)
   and is stored nowhere.
4. Keeps only the minimum: what the reader was trying to do, what happened (optional), the page
   they came from as a bare path (`web/beta.py::site_path`: no query string, so no search they typed
   and no token), and an email only if they give one.

States follow docs/31 §6, as `/submit` does: success says what happens next; an error prints the
problem's `title` and `request_id`, lists every field at fault with a link to it, keeps what the
reader typed and marks each field `aria-invalid` with its message in words.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from web.api_client import ApiError
from web.auth import _csrf_rejection, _is_same_origin
from web.beta import FEEDBACK_PATH, beta_mode, came_from, site_path
from web.page import get_api, not_found_response, templates
from web.reports import TURNSTILE_SCRIPT, turnstile_site_key
from web.submit import posted_form

router = APIRouter()

REPORTS_PATH = "/v1/reports"
#: `ReportIssueType` for a report about the site rather than a record (api/openapi.yaml).
ISSUE_TYPE = "beta_feedback"
#: `ReportRequest.description` and `.what_happened` maxLength.
TEXT_MAX = 2000
#: The API's email shape (services/api/admin_posts.py `_EMAIL_RE`), checked here first.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TURNSTILE_FIELD = "cf-turnstile-response"
HONEYPOT_FIELD = "website"
CAPTCHA_FIELD = "captcha_token"

#: Form field → element id, label (as the error summary names it) and the words for its error.
FIELDS: dict[str, tuple[str, str]] = {
    "description": ("feedback-description", "What you were trying to do"),
    "what_happened": ("feedback-what-happened", "What happened"),
    "email": ("feedback-email", "Email"),
    CAPTCHA_FIELD: ("feedback-captcha", "Check that you are not a robot"),
    HONEYPOT_FIELD: ("feedback-form", "Your feedback"),
}
ERROR_WORDS: dict[str, str] = {
    "description": f"Say what you were trying to do, in up to {TEXT_MAX:,} characters.",
    "what_happened": f"Keep this to {TEXT_MAX:,} characters.",
    "email": "Enter an email address like name@example.com, or leave it empty.",
    CAPTCHA_FIELD: "Complete the check that you are not a robot, then send again.",
    HONEYPOT_FIELD: "This feedback could not be accepted.",
    # The page sends a clean path, so the API refusing one means the page is wrong, not the reader.
    "page_path": (
        "This feedback could not be accepted. Please send it again from the link at the top of the page."
    ),
}


def check(typed: dict[str, str], *, site_key: str | None, token: str) -> dict[str, str]:
    """The page's own checks, made before any request: `{field: message in words}`."""
    errors: dict[str, str] = {}
    description = typed["description"].strip()
    if not description or len(description) > TEXT_MAX:
        errors["description"] = ERROR_WORDS["description"]
    if len(typed["what_happened"].strip()) > TEXT_MAX:
        errors["what_happened"] = ERROR_WORDS["what_happened"]
    email = typed["email"].strip()
    if email and not EMAIL_RE.match(email):
        errors["email"] = ERROR_WORDS["email"]
    if site_key and not token:
        errors[CAPTCHA_FIELD] = ERROR_WORDS[CAPTCHA_FIELD]
    return errors


def build_payload(typed: dict[str, str], *, page_path: str | None, token: str) -> dict[str, Any]:
    """The `ReportRequest` body for beta feedback: no `public_id`, and each optional key only when
    it has a value, so nothing empty is stored."""
    payload: dict[str, Any] = {
        "issue_type": ISSUE_TYPE,
        "description": typed["description"].strip(),
        HONEYPOT_FIELD: typed[HONEYPOT_FIELD],
    }
    optional = {
        "what_happened": typed["what_happened"].strip(),
        "email": typed["email"].strip(),
        "page_path": page_path,
        CAPTCHA_FIELD: token,
    }
    payload.update({key: value for key, value in optional.items() if value})
    return payload


def api_field_errors(body: dict[str, Any]) -> dict[str, str]:
    """`errors[]` of an RFC 9457 validation problem as `{form field: message in words}`; a field the
    form does not show is said under the form as a whole."""
    out: dict[str, str] = {}
    for err in body.get("errors") or []:
        field = str(err.get("field") or "")
        words = ERROR_WORDS.get(field) or str(err.get("message") or "This answer is not valid.")
        out.setdefault(field if field in FIELDS else HONEYPOT_FIELD, words)
    return out


def feedback_context(page_path: str | None, **state: Any) -> dict[str, Any]:
    """Everything `feedback.html` reads."""
    errors: dict[str, str] = state.get("errors") or {}
    order = list(FIELDS)
    rows = sorted(errors.items(), key=lambda item: order.index(item[0]))
    return {
        "page_path": page_path,
        "typed": state.get("typed") or {},  # not "values": Jinja would find dict.values
        "errors": errors,
        "error_rows": [
            {"label": FIELDS[name][1], "href": "#" + FIELDS[name][0], "message": message}
            for name, message in rows
        ],
        "problem": state.get("problem"),
        "sent": state.get("sent", False),
        "reference": state.get("reference"),
        "text_max": TEXT_MAX,
        "site_key": turnstile_site_key(),
        "turnstile_script": TURNSTILE_SCRIPT,
    }


def _render(request: Request, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    response = templates.TemplateResponse(
        request, "feedback.html", {"feedback": context}, status_code=status_code
    )
    # The page can carry what the reader typed, including their email: never kept by a cache.
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(FEEDBACK_PATH, response_class=HTMLResponse)
def feedback_form(request: Request) -> HTMLResponse:
    if not beta_mode():
        return not_found_response(request, "page")
    return _render(request, feedback_context(came_from(request)))


@router.post(FEEDBACK_PATH, response_class=HTMLResponse)
def send_feedback(request: Request, posted: Annotated[dict[str, str], Depends(posted_form)]) -> Response:
    if not beta_mode():
        return not_found_response(request, "page")
    if not _is_same_origin(request):
        return _csrf_rejection()
    typed = {name: posted.get(name, "") for name in ("description", "what_happened", "email", HONEYPOT_FIELD)}
    page_path = site_path(posted.get("page_path"))
    token = posted.get(TURNSTILE_FIELD, "").strip()
    errors = check(typed, site_key=turnstile_site_key(), token=token)
    if errors:
        # The page's own checks: no request was made, so there is no API title or request id.
        problem: dict[str, Any] = {"title": None, "request_id": None, "rate_limited": False}
        return _render(request, feedback_context(page_path, typed=typed, errors=errors, problem=problem), 422)

    try:
        result = get_api(request).post(
            REPORTS_PATH, json=build_payload(typed, page_path=page_path, token=token)
        )
        status, body = result.status_code, result.body
    except ApiError as exc:
        status, body = exc.status_code, exc.body
    except httpx.HTTPError:  # an unreachable API is an error state, never a stack trace
        status, body = 503, {"title": "The feedback service is unavailable"}

    if status < 300:
        return _render(request, feedback_context(page_path, sent=True, reference=body.get("task_id")))
    problem = {
        "title": body.get("title") or "Your feedback could not be sent",
        "request_id": body.get("request_id") or (body.get("meta") or {}).get("request_id"),
        "rate_limited": status == 429,
    }
    field_errors = api_field_errors(body) if status == 400 else {}
    http_status = 422 if status == 400 else 429 if status == 429 else 502
    context = feedback_context(page_path, typed=typed, errors=field_errors, problem=problem)
    return _render(request, context, http_status)


__all__ = ["router"]
