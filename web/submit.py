"""`GET`/`POST /submit`: "Submit a project", the light intake form (US-1001; docs/30 §4.5).

The page relays to the API's existing public write, `POST /v1/intake/proposals`
(services/api/admin_posts.py), which creates an `intake_proposal` task and nothing else: no
proposal row exists until an editor approves the task (US-1002, `adminApproveIntake`). This module
stores nothing. What it does, in order:

1. Refuses a cross-site POST (`web/auth.py::_is_same_origin`, the site's one CSRF rule).
2. Builds the `IntakeProposalRequest` body (api/openapi.yaml) from the form. The form's field
   names are the schema's own keys; the two nested objects use dotted names (`contact.name`,
   `contact.email`, `identifiers.eia_plant_id`, ...), which are also the names the API's
   `errors[].field` uses, so an API error lands on the input that caused it.
3. Checks the answers before spending a request. The API admits five intake or report requests
   per visitor address per hour (`INTAKE_RATE_LIMIT`, shared), and its validators stop at the first
   bad field; relaying every attempt would show one error per round trip and lock a reader out after
   five. So the page checks every field the schema constrains (required, enums, the jurisdiction
   pattern, lengths, number types, the email shape, consent) and shows every problem at once
   without calling the API. The API stays the authority: whatever it rejects is printed on the
   field it names. Number and length checks the API does not make (it stores `capacity_mw`
   verbatim) are made here so the body sent always matches the schema.
4. Relays the honeypot (`website`) as typed, so the API's `_reject_honeypot` sees what a bot
   filled in, and the Turnstile token (`cf-turnstile-response`) as `captcha_token`. With no
   `TURNSTILE_SITE_KEY` there is no widget; the API still requires a non-empty `captcha_token`, so
   the page sends `NO_WIDGET_TOKEN`, which an API that verifies tokens rejects (fail closed: a
   site and an API configured differently refuse submissions rather than accept them unchecked).

States follow docs/31 §6: success says what happens next (an editor reviews it before anything is
published, US-1002; public only if the submitter opted in and an editor publishes it, AC3); an error
prints the problem's `title` and `request_id`, lists every field at fault with a link to it, keeps
everything the reader typed and marks each field `aria-invalid` with its message in words.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from web.api_client import ApiError
from web.auth import _csrf_rejection, _is_same_origin
from web.labels import LIFECYCLE_LABELS, PROPOSAL_KIND_LABELS, technology_label
from web.legal import LAST_UPDATED as PRIVACY_NOTICE_UPDATED
from web.page import API_UNAVAILABLE, get_api, templates
from web.reports import TURNSTILE_SCRIPT, turnstile_site_key
from web.viewmodels import iso_label

router = APIRouter()

INTAKE_PATH = "/v1/intake/proposals"

#: `IntakeProposalRequest.kind` (`ProposalKind`), in the order the list filter offers them.
PROPOSAL_KINDS: tuple[str, ...] = tuple(PROPOSAL_KIND_LABELS)
#: `IntakeProposalRequest.lifecycle_state` (`LifecycleState`), self-declared: the lifecycle in order,
#: then `unknown` last, for a submitter who cannot say.
LIFECYCLE_CHOICES: tuple[str, ...] = (
    "announced",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "under_construction",
    "built",
    "withdrawn",
    "cancelled",
    "unknown",
)
#: The market operators whose queue ids the resolver keys on (`ProposalIdentifiers.queue_ids[].iso`,
#: the API's `Iso` tokens), in the order of the names a reader sees (`iso_label`).
QUEUE_OPERATORS: tuple[str, ...] = ("CAISO", "ERCOT", "ISONE", "MISO", "NYISO", "PJM", "SPP")

NAME_MAX = 200  # project_name, sponsor_name, contact.name (maxLength in the schema)
DESCRIPTION_MAX = 2000  # description (maxLength; US-1001 AC1)
#: `IntakeProposalRequest.jurisdiction.pattern`, applied after upper-casing what was typed.
JURISDICTION_RE = re.compile(r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")
#: The API's own shape check for `contact.email` (services/api/admin_posts.py `_EMAIL_RE`).
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
#: A plain decimal: digits, at most one point, no sign, unit, separator or exponent. `float()` alone
#: would take "nan", "1e3" and "-5"; the schema wants a number at or above zero.
DECIMAL_RE = re.compile(r"^(\d+(\.\d*)?|\.\d+)$")
TOKEN_RE = re.compile(r"^[a-z0-9_]+$")
EIA_PLANT_ID_RE = re.compile(r"^\d{1,7}$")

#: Sent as `captcha_token` when the site renders no widget (module docstring, item 4).
NO_WIDGET_TOKEN = "no-widget"  # noqa: S105 -- a marker value, not a credential
#: What the consent box agrees to, recorded with the submission (`consent_version`).
CONSENT_VERSION = f"privacy-notice-{PRIVACY_NOTICE_UPDATED}"

#: Every form field the page renders, in page order: the error summary lists problems in this
#: order, and these are the names the payload is built from.
TEXT_FIELDS: tuple[str, ...] = (
    "project_name",
    "sponsor_name",
    "kind",
    "technology",
    "capacity_mw",
    "storage_mwh",
    "lifecycle_state",
    "description",
    "jurisdiction",
    "state",
    "county",
    "identifiers.queue_ids.iso",
    "identifiers.queue_ids.id",
    "identifiers.eia_plant_id",
    "identifiers.ferc_dockets",
    "contact.name",
    "contact.email",
)
CHECKBOX_FIELDS: tuple[str, ...] = ("public_opt_in", "consent")
HONEYPOT_FIELD = "website"
#: The API's name for the Turnstile token, and the error summary's key for the widget.
CAPTCHA_FIELD = "captcha_token"
TURNSTILE_FIELD = "cf-turnstile-response"

#: The words each field goes by in the error summary ("Project name: Enter the project's name.").
FIELD_LABELS: dict[str, str] = {
    "project_name": "Project name",
    "sponsor_name": "Sponsor or developer",
    "kind": "Kind of project",
    "technology": "Technology",
    "capacity_mw": "Capacity",
    "storage_mwh": "Storage",
    "lifecycle_state": "Status",
    "description": "Description",
    "jurisdiction": "Jurisdiction",
    "state": "State or province",
    "county": "County",
    "identifiers.queue_ids.iso": "Interconnection queue",
    "identifiers.queue_ids.id": "Queue position ID",
    "identifiers.eia_plant_id": "EIA plant ID",
    "identifiers.ferc_dockets": "FERC docket",
    "contact.name": "Your name",
    "contact.email": "Your email address",
    "public_opt_in": "Publication",
    "consent": "Privacy notice",
    "captcha_token": "Check that you are not a robot",
    "website": "Submission",
}


def field_id(name: str) -> str:
    """The input's `id`: `contact.email` -> `submit-contact-email`."""
    return "submit-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


#: Every field's id, plus the captcha's wrapper (a link target in the error summary).
FIELD_IDS: dict[str, str] = {name: field_id(name) for name in (*TEXT_FIELDS, *CHECKBOX_FIELDS)}
FIELD_IDS[CAPTCHA_FIELD] = "submit-captcha"
FIELD_IDS["website"] = "submit-status"

#: The API's `errors[].field` values, in words, for a field the API refuses after the page's own
#: checks passed (a captcha Cloudflare rejected, a honeypot a bot filled, a rule the API tightened).
#: A field not listed prints the API's own message.
API_ERROR_WORDS: dict[str, tuple[str, str]] = {
    "project_name": ("project_name", "Enter the project's name."),
    "kind": ("kind", "Choose what kind of project it is."),
    "jurisdiction": ("jurisdiction", "Enter a country code, or a country and state code, like US or US-TX."),
    "lifecycle_state": ("lifecycle_state", "Choose the project's status."),
    "sponsor_name": ("sponsor_name", "Enter the sponsor or developer's name."),
    "contact": ("contact.name", "Enter your name and your email address."),
    "contact.email": ("contact.email", "Enter an email address like name@example.com."),
    "description": ("description", f"Shorten the description to {DESCRIPTION_MAX:,} characters or fewer."),
    "consent": (
        "consent",
        "Tick the box to agree to the privacy notice. We cannot keep a submission without it.",
    ),
    "captcha_token": (
        "captcha_token",
        "The check that you are not a robot did not pass. Complete it again, then send.",
    ),
    "website": ("website", "This submission could not be accepted."),
}


async def posted_form(request: Request) -> dict[str, str]:
    """The submitted form as plain strings (an async dependency, so the route itself stays sync
    and runs in the threadpool like every other page route: the API calls it makes block)."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def technology_options(request: Request, typed: str = "") -> list[tuple[str, str]]:
    """`(value, label)` for every proposal technology the register holds, from the same
    `/v1/meta/vocabularies` read the list filter uses, so a submitted token is one the list can
    filter by. `unknown` is the empty choice ("Not stated"), not an option of its own. If the API
    cannot answer, the form still renders: `other` stays offered, and a value already chosen is
    kept so a re-rendered form never drops it."""
    try:
        rows = get_api(request).get("/v1/meta/vocabularies")["data"]["technology"]
        values = [str(row["value"]) for row in rows if row.get("value")]
    except (*API_UNAVAILABLE, KeyError, TypeError):
        values = []
    values = [v for v in values if v != "unknown"]
    for extra in ("other", typed):
        if extra and extra not in values and TOKEN_RE.match(extra):
            values.append(extra)
    options = [(v, technology_label(v) or v) for v in values]
    return sorted(options, key=lambda option: (option[0] == "other", option[1].lower()))


def _decimal(value: str) -> float | None:
    return float(value) if DECIMAL_RE.match(value) else None


def build_payload(
    typed: Mapping[str, str], *, captcha_token: str, site_key: str | None
) -> tuple[dict[str, Any], dict[str, str]]:
    """`(IntakeProposalRequest body, {field: message})` from what was typed. A non-empty error map
    means the body is not sent (module docstring, item 3)."""
    errors: dict[str, str] = {}

    def text(name: str) -> str:
        return typed.get(name, "").strip()

    def required(name: str, missing: str, *, limit: int | None = None) -> str:
        value = text(name)
        if not value:
            errors[name] = missing
        elif limit is not None and len(value) > limit:
            errors[name] = f"Shorten this to {limit:,} characters or fewer (it has {len(value):,})."
        return value

    project_name = required("project_name", "Enter the project's name.", limit=NAME_MAX)
    sponsor_name = required("sponsor_name", "Enter the sponsor or developer's name.", limit=NAME_MAX)

    kind = text("kind")
    if kind not in PROPOSAL_KINDS:
        errors["kind"] = "Choose what kind of project it is."

    technology = text("technology") or None
    if technology is not None and not TOKEN_RE.match(technology):
        errors["technology"] = "Choose a technology from the list, or leave it as not stated."

    numbers: dict[str, float | None] = {}
    units = (("capacity_mw", "megawatts, like 150 or 87.5"), ("storage_mwh", "megawatt-hours, like 400"))
    for name, unit in units:
        raw = text(name)
        numbers[name] = _decimal(raw) if raw else None
        if raw and numbers[name] is None:
            errors[name] = f"Enter a number of {unit}, without units or commas."

    lifecycle_state = text("lifecycle_state")
    if lifecycle_state not in LIFECYCLE_CHOICES:
        errors["lifecycle_state"] = "Choose the project's status."

    # US-1001 AC1 caps the description at 2,000 characters. Browsers send a textarea's line breaks
    # as CRLF but count them as one character against `maxlength`, so they are normalised first.
    description = typed.get("description", "").replace("\r\n", "\n").strip()
    if len(description) > DESCRIPTION_MAX:
        errors["description"] = (
            f"Shorten the description to {DESCRIPTION_MAX:,} characters or fewer "
            f"(it has {len(description):,})."
        )

    jurisdiction = text("jurisdiction").upper()
    if not jurisdiction:
        errors["jurisdiction"] = "Enter where the project is, like US-TX for Texas."
    elif not JURISDICTION_RE.match(jurisdiction):
        errors["jurisdiction"] = "Enter a country code, or a country and state code, like US or US-TX."

    identifiers: dict[str, Any] = {}
    queue_iso, queue_id = text("identifiers.queue_ids.iso"), text("identifiers.queue_ids.id")
    if queue_iso and queue_iso not in QUEUE_OPERATORS:
        errors["identifiers.queue_ids.iso"] = "Choose a queue from the list."
    elif queue_iso and not queue_id:
        errors["identifiers.queue_ids.id"] = (
            f"Enter the project's {iso_label(queue_iso)} queue position ID, or set the queue back to "
            "“Not in an ISO queue, or not sure”."
        )
    if queue_id:
        # An id without an operator keeps an empty `iso`, the shape the resolver already reads.
        identifiers["queue_ids"] = [{"iso": queue_iso, "id": queue_id}]
    eia_plant_id = text("identifiers.eia_plant_id")
    if eia_plant_id and not EIA_PLANT_ID_RE.match(eia_plant_id):
        errors["identifiers.eia_plant_id"] = "Enter the EIA plant ID as digits only, like 57701."
    elif eia_plant_id:
        identifiers["eia_plant_id"] = eia_plant_id
    ferc_docket = text("identifiers.ferc_dockets")
    if ferc_docket:
        identifiers["ferc_dockets"] = [ferc_docket]

    contact_name = required("contact.name", "Enter your name.", limit=NAME_MAX)
    contact_email = text("contact.email")
    if not contact_email:
        errors["contact.email"] = "Enter your email address."
    elif not EMAIL_RE.match(contact_email):
        errors["contact.email"] = "Enter an email address like name@example.com."

    consent = typed.get("consent") == "true"
    if not consent:
        errors["consent"] = (
            "Tick the box to agree to the privacy notice. We cannot keep a submission without it."
        )

    if site_key and not captcha_token:
        errors[CAPTCHA_FIELD] = (
            "Complete the check that you are not a robot, then send again. It needs JavaScript, and it may "
            "take a few seconds to appear."
        )

    payload: dict[str, Any] = {
        "project_name": project_name,
        "kind": kind,
        "technology": technology,
        "capacity_mw": numbers["capacity_mw"],
        "storage_mwh": numbers["storage_mwh"],
        "jurisdiction": jurisdiction,
        "state": text("state") or None,
        "county": text("county") or None,
        "lifecycle_state": lifecycle_state,
        "sponsor_name": sponsor_name,
        "contact": {"name": contact_name, "email": contact_email},
        "description": description or None,
        "public_opt_in": typed.get("public_opt_in") == "true",
        "consent": consent,
        "consent_version": CONSENT_VERSION,
        CAPTCHA_FIELD: captcha_token or NO_WIDGET_TOKEN,
        HONEYPOT_FIELD: typed.get(HONEYPOT_FIELD, ""),
    }
    if identifiers:
        payload["identifiers"] = identifiers
    return payload, errors


def api_field_errors(body: Mapping[str, Any]) -> dict[str, str]:
    """`errors[]` of an RFC 9457 validation problem as `{form field: message in words}`."""
    out: dict[str, str] = {}
    for err in body.get("errors") or []:
        field = str(err.get("field") or "")
        fallback = (field, str(err.get("message") or "This answer is not valid."))
        target, words = API_ERROR_WORDS.get(field, fallback)
        # A field the form does not render (a rule on a key it never sends) is still said, under
        # the submission as a whole, never dropped.
        out.setdefault(target if target in FIELD_IDS else HONEYPOT_FIELD, words)
    return out


#: The order the error summary lists problems in: the page's.
_SUMMARY_ORDER: tuple[str, ...] = (*TEXT_FIELDS, *CHECKBOX_FIELDS, CAPTCHA_FIELD, HONEYPOT_FIELD)


def _ordered(errors: Mapping[str, str]) -> list[dict[str, str]]:
    """The error summary's rows, in page order, each with the label and the id it links to."""
    rows = sorted(errors.items(), key=lambda item: _SUMMARY_ORDER.index(item[0]))
    return [
        {"field": name, "label": FIELD_LABELS[name], "href": "#" + FIELD_IDS[name], "message": message}
        for name, message in rows
    ]


def submit_context(request: Request, **state: Any) -> dict[str, Any]:
    """Everything `submit.html` reads."""
    typed: dict[str, str] = state.get("typed") or {}
    errors: dict[str, str] = state.get("errors") or {}
    return {
        "typed": typed,  # not "values": Jinja would find dict.values
        "errors": errors,
        "error_rows": _ordered(errors),
        "problem": state.get("problem"),
        "sent": state.get("sent", False),
        "reference": state.get("reference"),
        "request_id": state.get("request_id"),
        "opted_in": typed.get("public_opt_in") == "true",
        "ids": FIELD_IDS,
        "kinds": [(value, PROPOSAL_KIND_LABELS[value]) for value in PROPOSAL_KINDS],
        "lifecycle_states": [(value, LIFECYCLE_LABELS[value]) for value in LIFECYCLE_CHOICES],
        "technologies": [] if state.get("sent") else technology_options(request, typed.get("technology", "")),
        "queue_operators": [(value, iso_label(value)) for value in QUEUE_OPERATORS],
        "name_max": NAME_MAX,
        "description_max": DESCRIPTION_MAX,
        "site_key": turnstile_site_key(),
        "turnstile_script": TURNSTILE_SCRIPT,
    }


def _render(request: Request, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    response = templates.TemplateResponse(
        request, "submit.html", {"submit": context}, status_code=status_code
    )
    if request.method == "POST":
        # The page carries what the reader typed (their name and email) or their reference.
        response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/submit", response_class=HTMLResponse)
def submit_form(request: Request) -> HTMLResponse:
    return _render(request, submit_context(request))


@router.post("/submit", response_class=HTMLResponse)
def submit_project(request: Request, posted: Annotated[dict[str, str], Depends(posted_form)]) -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    typed = {name: posted.get(name, "") for name in (*TEXT_FIELDS, *CHECKBOX_FIELDS, HONEYPOT_FIELD)}
    site_key = turnstile_site_key()
    token = posted.get(TURNSTILE_FIELD, "").strip()
    payload, errors = build_payload(typed, captcha_token=token, site_key=site_key)
    if errors:
        # The page's own checks: no request was made, so there is no API title or request id.
        problem: dict[str, Any] = {"title": None, "request_id": None, "rate_limited": False}
        return _render(request, submit_context(request, typed=typed, errors=errors, problem=problem), 422)

    try:
        result = get_api(request).post(INTAKE_PATH, json=payload)
        status, body = result.status_code, result.body
    except ApiError as exc:
        status, body = exc.status_code, exc.body
    except httpx.HTTPError:  # an unreachable API is an error state, never a stack trace
        status, body = 503, {"title": "The submission service is unavailable"}

    if status < 300:
        context = submit_context(
            request,
            typed=typed,
            sent=True,
            reference=body.get("task_id"),
            request_id=body.get("request_id"),
        )
        return _render(request, context)
    problem = {
        "title": body.get("title") or "The submission could not be sent",
        "request_id": body.get("request_id") or (body.get("meta") or {}).get("request_id"),
        "rate_limited": status == 429,
    }
    field_errors = api_field_errors(body) if status == 400 else {}
    http_status = 422 if status == 400 else 429 if status == 429 else 502
    context = submit_context(request, typed=typed, errors=field_errors, problem=problem)
    return _render(request, context, http_status)


__all__ = ["router"]
