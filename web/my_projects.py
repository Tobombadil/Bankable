"""My pipeline: the Tracker (decision rows 2026-10-23, "My pipeline is one central tracker" and
"Navigation v2"; docs/19 §6.2 as amended; docs/30 §11.4, §11.5 and §11.9.4).

One list of everything the signed-in user is working on: projects, bids and investments, each with
its own stages, sorted by next deadline. The pages:

- `/my`: the Tracker. A type filter (All · Projects · Bids · Investments, counts only the user sees),
  one row per item with its type, stage, next deadline, a "changed" badge with its reason and
  checklist progress: ruled cards in one column below 720px, a table from 720px. An empty state
  points to the map, the site finder and RFPs.
- `/my/items/new`: the type first, then a short form. "Track" on a record page posts here with the
  record in the body, and the form comes back prefilled (`POST /v1/my/prefill`); `POST /my/items`
  creates the item.
- `/my/items/{id}` and its URL tabs Overview · Linked records · Checklist · Notes · People (docs/30
  §11.5; Documents arrives with data rooms). Opening any tab is a visit: the API stamps it, and the
  Overview lists what changed on the linked records since the one before.
- `/my/invitations/accept?t=…`: who invited you, to what, and Accept (docs/19 §6.2).

**Through the API only.** Every read and write is a `/v1/my/` call with the visitor's own session
cookie, so the API's one authorisation function decides everything. This module never imports the
private package or the module that reads it (`infra/importlinter.ini`, `private-projects-isolated`).
Words and stages come from `GET /v1/my/kinds`, never copied here.

**Private by construction** (docs/19 §5.2): until `MY_PIPELINE=on` (the API's own switch, read the
same way) every route answers the very 404 an unknown address gets, byte for byte, and `robots.txt`
does not name them (`web/sitemaps.py`), so nothing says the pages exist. With the switch on, a page
is never linked for a signed-out visitor, carries `Cache-Control: private, no-store`, `X-Robots-Tag:
noindex, nofollow`, `Referrer-Policy: no-referrer` and `Vary: Cookie`, and `/my` is disallowed in
`robots.txt`. URLs carry opaque ids only. The page `<title>` names no item, so neither the head's
social tags nor a browser's history carry a private title. Every form must come from this site
(`same_origin_form`: the `Origin` check, or `Sec-Fetch-Site: same-origin`, since `no-referrer`
makes a browser send `Origin: null`), and no page needs a script.

**The Track block** (`partials/_track.html`, on project, RFP and funding notice, and plant pages)
replaces "Start a deal". It shows only to a visitor with a session cookie (the site's signed-in
test, the same one the header's Account link uses) while the switch is on, and never varies with
whether anyone tracks the record: `track_offer` reads the record and the request, nothing private.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Final
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from web.api_client import ApiClient, ApiError, ApiResult
from web.auth import _csrf_rejection, _is_same_origin
from web.labels import technology_label
from web.page import get_api, templates

#: The API's launch switch (`services/api/my_projects.py::MY_PIPELINE_ENV`), read the same way.
MY_PIPELINE_ENV: Final = "MY_PIPELINE"
SESSION_COOKIE_NAME: Final = "session"
#: docs/19 §5.2 item 6, on every `/my/` response.
PRIVATE_HEADERS: Final[Mapping[str, str]] = {
    "Cache-Control": "private, no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "Vary": "Cookie",
}
ITEM_KINDS: Final = ("project", "bid", "investment")
#: The item tabs (docs/30 §11.5), as URL path segments under `/my/items/{id}`.
TABS: Final = (
    ("overview", "Overview", ""),
    ("linked", "Linked records", "/linked"),
    ("checklist", "Checklist", "/checklist"),
    ("notes", "Notes", "/notes"),
    ("people", "People", "/people"),
)
TAB_PATHS: Final = {key: suffix for key, _label, suffix in TABS}
_PROJECT_ID = re.compile(r"prj_[0-9A-HJKMNP-TV-Z]{10,26}")
_ROW_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NOTE_ID = re.compile(r"nte_[0-9A-HJKMNP-TV-Z]{10,26}")
_INVITATION_ID = re.compile(r"inv_[0-9A-HJKMNP-TV-Z]{10,26}")
_USER_ID = re.compile(r"usr_[0-9A-HJKMNP-TV-Z]{10,26}")
_CROCKFORD = "[0-9A-HJKMNP-TV-Z]{10,26}"
#: A pasted public id, by the record type it names (`services/ids.py` prefixes).
_RECORD_IDS: Final = (
    ("proposal", re.compile(rf"\bprop_{_CROCKFORD}\b")),
    ("opportunity", re.compile(rf"\bopp_{_CROCKFORD}\b")),
    ("asset", re.compile(rf"\basset_{_CROCKFORD}\b")),
    ("site", re.compile(rf"\bsite_{_CROCKFORD}\b")),
    ("organization", re.compile(rf"\borg_{_CROCKFORD}\b")),
    ("interconnection_point", re.compile(rf"\bpoi_{_CROCKFORD}\b")),
)
#: A pasted page address whose last segment is a slug, by the API collection that resolves it.
_SLUG_PAGES: Final = {
    "proposals": ("proposal", "/v1/proposals", {}),
    "opportunities": ("opportunity", "/v1/opportunities", {"status": "all"}),
    "organizations": ("organization", "/v1/organizations", {}),
}
RELATION_LABELS: Final[Mapping[str, str]] = {
    "this_project": "This is the item's project",
    "nearby": "Something near it",
    "candidate_site": "A candidate site",
    "grid_connection": "Its grid connection",
    "offtake_or_rfp": "An RFP or offtake it answers",
    "funding": "A funding call",
    "counterparty": "A counterparty",
    "comparable": "A comparable",
}
RECORD_TYPE_LABELS: Final[Mapping[str, str]] = {
    "proposal": "Project",
    "opportunity": "RFP or funding notice",
    "asset": "Plant or line",
    "site": "Site",
    "organization": "Company",
    "interconnection_point": "Grid connection point",
    "corridor": "Site-finder corridor",
}
STATUS_CHOICES: Final = (
    ("not_started", "Not started"),
    ("in_progress", "In progress"),
    ("done", "Done"),
    ("not_applicable", "N/A"),
)
ROLE_LABELS: Final[Mapping[str, str]] = {"owner": "Owner", "editor": "Editor", "viewer": "Viewer"}
#: What each record offers "Track" (docs/19 §1; decision row 2026-10-23), mirrored from the API's
#: own rule so the block is never offered where the API would refuse it.
CLOSED_PROPOSAL_STATES: Final = frozenset({"withdrawn", "cancelled"})
OPEN_OPPORTUNITY_STATUSES: Final = frozenset({"announced", "open", "reinstated"})
TRACKABLE_ASSET_STATUSES: Final = frozenset({"retiring", "retired"})
TRACK_CHOICES: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    "proposal": (
        ("project", "I'm developing it, or something near it"),
        ("investment", "I'm looking to fund it"),
    ),
    "opportunity": (("bid", "A bid: my response to it"),),
    "asset": (("project", "A project to connect where it retires"),),
    "site": (("project", "A project at this site"),),
    "interconnection_point": (("project", "A project connecting here"),),
}
_DONE_NOTICES: Final[Mapping[str, str]] = {
    "created": "Added to your tracker. Only you can see it until you invite someone.",
    "stage": "Stage saved.",
    "saved": "Saved.",
    "unchanged": "Nothing changed.",
    "email": "Your choice is saved. Emails about changes are coming soon: none is sent yet.",
    "archived": "Archived. It no longer shows on your tracker; unarchive it here.",
    "unarchived": "Back on your tracker.",
    "deleted": "Deleted, with everything in it.",
    "linked": "Record linked.",
    "unlinked": "Link removed.",
    "checklist": "Checklist saved.",
    "evidence": "Saved. The item's status is unchanged.",
    "note": "Note saved.",
    "note_deleted": "Note deleted.",
    "revoked": "Invitation revoked.",
    "member": "Saved.",
    "left": "You left that item.",
    "joined": "You joined this item.",
}


def pipeline_on() -> bool:
    return os.environ.get(MY_PIPELINE_ENV, "").strip().lower() == "on"


def switched_on() -> None:
    """Every route's first check, before its own (the router's dependency): while the switch is off,
    Starlette's own 404, the one an address no route matches gets, so the response says nothing about
    these pages (no private headers, no page title naming them)."""
    if not pipeline_on():
        raise StarletteHTTPException(status_code=404)


router = APIRouter(dependencies=[Depends(switched_on)])


# --------------------------------------------------------------------------- the Track block
def track_offer(request: Request, record_type: str, record: object) -> dict[str, Any] | None:
    """What the Track block offers on a public record page, or `None` (no block at all): only to a
    visitor with a session cookie while the switch is on, only where the API offers Track. The
    cookie is not checked against the API here, as the header's Account link does not check it: an
    expired one gets the block, whose link and form lead to sign-in, and nothing private. It reads
    the record and the request alone, so the page is the same whether or not anyone tracks it."""
    if not pipeline_on() or not request.cookies.get(SESSION_COOKIE_NAME):
        return None
    if not isinstance(record, Mapping) or not isinstance(record.get("public_id"), str):
        return None
    if record_type == "proposal" and record.get("lifecycle_state") in CLOSED_PROPOSAL_STATES:
        return None
    if record_type == "opportunity" and record.get("status") not in OPEN_OPPORTUNITY_STATUSES:
        return None
    if record_type == "asset" and (
        record.get("asset_type") != "power_plant" or record.get("status") not in TRACKABLE_ASSET_STATUSES
    ):
        return None
    choices = TRACK_CHOICES.get(record_type)
    if choices is None:
        return None
    return {"record_type": record_type, "public_id": record["public_id"], "choices": choices}


templates.env.globals["track_offer"] = track_offer

#: `my.css` is versioned by its own content hash: `web/assets.py`'s `asset_version` covers the
#: site-wide files, and this module's pages and the Track block load this one file besides them.
MY_CSS_VERSION: Final = hashlib.sha256(
    (Path(__file__).resolve().parent / "static" / "css" / "my.css").read_bytes()
).hexdigest()[:12]
templates.env.globals["my_css_version"] = MY_CSS_VERSION


# ------------------------------------------------------------------------------------ helpers
def _cookies(request: Request) -> dict[str, str] | None:
    value = request.cookies.get(SESSION_COOKIE_NAME)
    return {SESSION_COOKIE_NAME: value} if value else None


def _private(response: Response) -> Response:
    for name, value in PRIVATE_HEADERS.items():
        response.headers[name] = value
    return response


def _page(request: Request, name: str, context: dict[str, Any], *, status_code: int = 200) -> Response:
    return _private(templates.TemplateResponse(request, name, context, status_code=status_code))


def _not_found(request: Request) -> Response:
    return _page(request, "my/not_found.html", {}, status_code=404)


def _redirect(url: str) -> Response:
    return _private(RedirectResponse(url=url, status_code=303))


def _signin(request: Request, path: str | None = None) -> Response:
    """To sign in, and back. Private paths carry opaque ids only, so `next` names nothing private."""
    return _redirect(f"/login?next={quote(path or request.url.path, safe='')}")


def _gate(request: Request) -> Response | dict[str, str]:
    """Every page's first check after the switch (`switched_on`): a session, else sign in. Returns
    the cookies to forward."""
    cookies = _cookies(request)
    if cookies is None:
        return _signin(request)
    return cookies


def same_origin_form(request: Request) -> bool:
    """`web/auth.py::_is_same_origin`, or a browser's own word that the form is this site's.

    These pages are served with `Referrer-Policy: no-referrer` (docs/19 §5.2 item 6), and under that
    policy a browser sends `Origin: null` and no `Referer` with a form it posts, so the usual check
    refuses every one of them (found in Chromium, 2026-10-23). The browser still says where the
    request came from in `Sec-Fetch-Site`, which no page script can set: `same-origin` passes, and
    anything else, or no header at all, fails closed."""
    if _is_same_origin(request):
        return True
    origin = request.headers.get("origin")
    return origin in (None, "null") and request.headers.get("sec-fetch-site") == "same-origin"


def _write_gate(request: Request) -> Response | dict[str, str]:
    if not same_origin_form(request):
        return _private(_csrf_rejection())
    cookies = _cookies(request)
    if cookies is None:
        return _signin(request, "/my")
    return cookies


def _problem(result: ApiResult) -> dict[str, Any]:
    body = result.body
    return {
        "title": body.get("title") or "That did not work",
        "detail": body.get("detail"),
        "request_id": body.get("request_id"),
        "field_errors": {e["field"]: e["message"] for e in body.get("errors", []) if "field" in e},
    }


def _failure(request: Request, result: ApiResult) -> Response:
    """A read the page cannot do without failed: sign in again, verify the address, a 404, or the
    unavailable page."""
    if result.status_code == 401:
        # After a form, back to the tracker: the form's own path answers no GET.
        return _signin(request, None if request.method == "GET" else "/my")
    if result.status_code == 403:
        return _page(request, "my/verify.html", {"problem": _problem(result)}, status_code=403)
    if result.status_code == 404:
        return _not_found(request)
    return _page(request, "my/unavailable.html", {}, status_code=503)


def _vocabulary(api: ApiClient, cookies: dict[str, str]) -> dict[str, Any]:
    result = api.get_result("/v1/my/kinds", cookies=cookies)
    if result.status_code != 200:
        raise ApiError(result.status_code, result.body)
    data: dict[str, Any] = result.body["data"]
    data["by_kind"] = {kind["kind"]: kind for kind in data["kinds"]}
    data["stage_labels"] = {
        stage["stage"]: stage["label"] for kind in data["kinds"] for stage in kind["stages"]
    }
    return data


def _date(value: object) -> dt.date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _short_date(value: object) -> str:
    day = _date(value)
    return f"{day.day} {day.strftime('%b')}" if day else ""


def deadline_view(deadline: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """`15 Dec · in 53 days`, and whose deadline it is."""
    if not deadline:
        return None
    days = int(deadline.get("days_left") or 0)
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    via = deadline.get("via_bid")
    return {
        "date": _short_date(deadline.get("due_at")),
        "iso": str(deadline.get("due_at") or "")[:10],
        "when": when,
        "record": deadline.get("record") or {},
        "via_bid": via,
    }


def changed_view(changed: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The badge: `Deadline moved, 20 Oct`, `2 status changes`, `Record updated, 21 Oct`."""
    if not changed or not changed.get("changed"):
        return None
    reason, count = changed.get("reason"), int(changed.get("count") or 1)
    plural = {"status": "status changes", "updated": "updates", "deadline": "deadline changes"}
    text = (
        f"{count} {plural.get(str(reason), 'changes')}"
        if count > 1
        else str(changed.get("text") or "Changed")
    )
    return {"reason": reason, "text": text, "date": _short_date(changed.get("at"))}


def _row(item: Mapping[str, Any]) -> dict[str, Any]:
    progress = item.get("progress") or {}
    return {
        **item,
        "deadline": deadline_view(item.get("next_deadline")),
        "changed": changed_view(item.get("changed_since_last_visit")),
        "checklist": f"{progress.get('done', 0)} of {progress.get('total', 0)}",
        "href": f"/my/items/{item['public_id']}",
    }


# ----------------------------------------------------------------------------------- tracker
@router.get("/my", response_class=HTMLResponse)
def tracker(request: Request) -> Response:
    gate = _gate(request)
    if isinstance(gate, Response):
        return gate
    api = get_api(request)
    archived = request.query_params.get("archived") == "1"
    params = {"include_archived": "true"} if archived else None
    result = api.get_result("/v1/my/projects", params=params, cookies=gate)
    if result.status_code != 200:
        return _failure(request, result)
    rows = [row for row in result.body.get("data", []) if isinstance(row, dict)]
    if archived:
        rows = [row for row in rows if row.get("archived")]
    wanted = request.query_params.get("type")
    wanted = wanted if wanted in ITEM_KINDS else None
    counts = {kind: sum(1 for row in rows if row.get("kind") == kind) for kind in ITEM_KINDS}
    shown = [_row(row) for row in rows if wanted is None or row.get("kind") == wanted]
    return _page(
        request,
        "my/tracker.html",
        {
            "rows": shown,
            "total": len(rows),
            "counts": counts,
            "wanted": wanted,
            "archived": archived,
            "done": _DONE_NOTICES.get(request.query_params.get("done", "")),
        },
    )


# ---------------------------------------------------------------------------------- new item
def _editable_projects(
    api: ApiClient, cookies: dict[str, str], exclude: str | None = None
) -> list[dict[str, Any]]:
    """The caller's projects a bid may attach to: ones they own or edit, not archived."""
    result = api.get_result("/v1/my/projects", params={"kind": "project"}, cookies=cookies)
    if result.status_code != 200:
        return []
    return [
        row
        for row in result.body.get("data", [])
        if isinstance(row, dict)
        and row.get("role") in ("owner", "editor")
        and row.get("public_id") != exclude
    ]


def _new_form(
    request: Request,
    api: ApiClient,
    cookies: dict[str, str],
    *,
    kind: str,
    values: Mapping[str, Any] | None = None,
    prefill: Mapping[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    vocabulary = _vocabulary(api, cookies)
    marks = prefill or {}
    return _page(
        request,
        "my/new.html",
        {
            "kind": kind,
            "kinds": vocabulary["kinds"],
            "stages": vocabulary["by_kind"][kind]["stages"],
            "technologies": [
                (t, technology_label(t) or t.replace("_", " ")) for t in vocabulary["technologies"]
            ],
            "relation_labels": RELATION_LABELS,
            "projects": _editable_projects(api, cookies) if kind == "bid" else [],
            "values": dict(values or {}),
            "prefill": marks,
            "prefilled": set(marks.get("fields") or []),
            "left_blank": {row["field"] for row in marks.get("left_blank") or []},
            "record": marks.get("record"),
            "record_type_labels": RECORD_TYPE_LABELS,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/my/items/new", response_class=HTMLResponse)
def new_item(request: Request) -> Response:
    gate = _gate(request)
    if isinstance(gate, Response):
        return gate
    kind = request.query_params.get("type")
    kind = kind if kind in ITEM_KINDS else "project"
    try:
        return _new_form(request, get_api(request), gate, kind=kind)
    except ApiError as exc:
        return _failure(request, ApiResult(exc.status_code, exc.body))


@router.post("/my/items/new", response_class=HTMLResponse)
def track_start(
    request: Request,
    record_type: Annotated[str, Form()] = "",
    public_id: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
) -> Response:
    """The Track button: the record travels in the body, never the URL (docs/19 §6.2), and the
    form comes back prefilled under the licence rule."""
    gate = _write_gate(request)
    if isinstance(gate, Response):
        return gate
    api = get_api(request)
    body: dict[str, Any] = {"record_type": record_type, "public_id": public_id}
    if kind:
        body["kind"] = kind
    result = api.post("/v1/my/prefill", json=body, cookies=gate)
    try:
        if result.status_code != 200:
            if result.status_code in (401, 403):
                return _failure(request, result)
            return _new_form(request, api, gate, kind="project", error=_problem(result), status_code=400)
        prefill = result.body["data"]
        return _new_form(request, api, gate, kind=prefill["kind"], values=prefill["values"], prefill=prefill)
    except ApiError as exc:
        return _failure(request, ApiResult(exc.status_code, exc.body))


def _number(value: str) -> float | str | None:
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return text  # the API names the field and refuses it


def _item_fields(
    *,
    title: str,
    technology: str,
    capacity_mw: str,
    stage: str,
    parent_project_id: str | None,
    submitted_on: str,
    lat: str,
    lon: str,
    kind: str,
) -> dict[str, Any]:
    """The JSON body for an item's fields. `parent_project_id=None` leaves a bid's project as it is:
    the details form never sends it, because a collaborator who cannot see that project would detach
    it by saving their edit (the bid card has its own form)."""
    fields: dict[str, Any] = {
        "title": title,
        "technology": technology or None,
        "capacity_mw": _number(capacity_mw),
    }
    if stage:
        fields["stage"] = stage
    if kind == "bid":
        if parent_project_id is not None:
            fields["parent_project_id"] = parent_project_id or None
        fields["submitted_on"] = submitted_on or None
    if lat.strip() or lon.strip():
        fields["lat"], fields["lon"] = _number(lat), _number(lon)
    return fields


@router.post("/my/items", response_class=HTMLResponse)
def create_item(
    request: Request,
    kind: Annotated[str, Form()] = "project",
    title: Annotated[str, Form()] = "",
    technology: Annotated[str, Form()] = "",
    capacity_mw: Annotated[str, Form()] = "",
    stage: Annotated[str, Form()] = "",
    parent_project_id: Annotated[str, Form()] = "",
    submitted_on: Annotated[str, Form()] = "",
    lat: Annotated[str, Form()] = "",
    lon: Annotated[str, Form()] = "",
    point: Annotated[str, Form()] = "",
    from_record_type: Annotated[str, Form()] = "",
    from_public_id: Annotated[str, Form()] = "",
    relation: Annotated[str, Form()] = "",
) -> Response:
    gate = _write_gate(request)
    if isinstance(gate, Response):
        return gate
    api = get_api(request)
    kind = kind if kind in ITEM_KINDS else "project"
    body = _item_fields(
        title=title,
        technology=technology,
        capacity_mw=capacity_mw,
        stage=stage,
        parent_project_id=parent_project_id,
        submitted_on=submitted_on,
        lat=lat,
        lon=lon,
        kind=kind,
    )
    body["kind"] = kind
    prefill: dict[str, Any] | None = None
    if from_record_type and from_public_id:
        body["from"] = {"record_type": from_record_type, "public_id": from_public_id}
        if relation:
            body["from"]["relation"] = relation
        if point == "later":
            body["lat"] = body["lon"] = None
        elif point == "record":
            body.pop("lat", None)
            body.pop("lon", None)
    result = api.post("/v1/my/projects", json=body, cookies=gate)
    if result.status_code == 201:
        return _redirect(f"/my/items/{result.body['data']['public_id']}?done=created")
    if result.status_code in (401, 403):
        return _failure(request, result)
    if body.get("from"):
        again = api.post(
            "/v1/my/prefill",
            json={"record_type": from_record_type, "public_id": from_public_id, "kind": kind},
            cookies=gate,
        )
        prefill = again.body.get("data") if again.status_code == 200 else None
    values = {
        "title": title,
        "technology": technology,
        "capacity_mw": capacity_mw,
        "stage": stage,
        "parent_project_id": parent_project_id,
        "submitted_on": submitted_on,
        "lat": lat,
        "lon": lon,
    }
    try:
        return _new_form(
            request,
            api,
            gate,
            kind=kind,
            values=values,
            prefill=prefill,
            error=_problem(result),
            status_code=400,
        )
    except ApiError as exc:
        return _failure(request, ApiResult(exc.status_code, exc.body))


# -------------------------------------------------------------------------------- item pages
def _tab_href(public_id: str, tab: str) -> str:
    return f"/my/items/{public_id}{TAB_PATHS[tab]}"


def _item_page(
    request: Request,
    public_id: str,
    tab: str,
    *,
    error: dict[str, Any] | None = None,
    issued_link: str | None = None,
    status_code: int = 200,
) -> Response:
    gate = _gate(request)
    if isinstance(gate, Response):
        return gate
    if not _PROJECT_ID.fullmatch(public_id):
        return _not_found(request)
    api = get_api(request)
    detail = api.get_result(f"/v1/my/projects/{public_id}", cookies=gate)
    if detail.status_code != 200:
        return _failure(request, detail)
    item: dict[str, Any] = detail.body["data"]
    context: dict[str, Any] = {
        "item": item,
        "tab": tab,
        "tabs": [(key, label, _tab_href(public_id, key)) for key, label, _suffix in TABS],
        "error": error,
        "done": _DONE_NOTICES.get(request.query_params.get("done", "")),
        "can_edit": item["role"] in ("owner", "editor") and not item["archived"],
        "is_owner": item["role"] == "owner",
        "deadline": deadline_view(item.get("next_deadline")),
        "changed": changed_view(item.get("changed_since_last_visit")),
        "relation_labels": RELATION_LABELS,
        "record_type_labels": RECORD_TYPE_LABELS,
        "status_choices": STATUS_CHOICES,
        "role_labels": ROLE_LABELS,
        "issued_link": issued_link,
    }
    try:
        if tab == "overview":
            vocabulary = _vocabulary(api, gate)
            context["technologies"] = [
                (t, technology_label(t) or t.replace("_", " ")) for t in vocabulary["technologies"]
            ]
            context["bids"] = [
                {**bid, "deadline": deadline_view(bid.get("next_deadline"))} for bid in item["bids"]
            ]
            context["projects"] = (
                _editable_projects(api, gate, exclude=public_id) if item["kind"] == "bid" else []
            )
            context["changes"] = [
                {**change, "date": _short_date(change.get("at"))} for change in item["changes"]
            ]
        elif tab == "linked":
            context["relations"] = _vocabulary(api, gate)["relations"]
        elif tab == "checklist":
            result = api.get_result(f"/v1/my/projects/{public_id}/checklist", cookies=gate)
            if result.status_code != 200:
                return _failure(request, result)
            context["checklist"] = result.body["data"]
        elif tab == "notes":
            result = api.get_result(f"/v1/my/projects/{public_id}/notes", cookies=gate)
            if result.status_code != 200:
                return _failure(request, result)
            context["notes"] = result.body["data"]
        elif tab == "people":
            members = api.get_result(f"/v1/my/projects/{public_id}/members", cookies=gate)
            if members.status_code != 200:
                return _failure(request, members)
            context["members"] = members.body["data"]
            context["invitations"] = []
            if item["role"] == "owner":
                pending = api.get_result(f"/v1/my/projects/{public_id}/invitations", cookies=gate)
                if pending.status_code == 200:
                    context["invitations"] = pending.body["data"]
    except ApiError as exc:
        return _failure(request, ApiResult(exc.status_code, exc.body))
    return _page(request, "my/item.html", context, status_code=status_code)


@router.get("/my/items/{public_id}", response_class=HTMLResponse)
def item_overview(request: Request, public_id: str) -> Response:
    return _item_page(request, public_id, "overview")


@router.get("/my/items/{public_id}/{tab}", response_class=HTMLResponse)
def item_tab(request: Request, public_id: str, tab: str) -> Response:
    if tab not in TAB_PATHS or tab == "overview":
        return _not_found(request)
    return _item_page(request, public_id, tab)


# ------------------------------------------------------------------------------ item writes
def _send(
    request: Request,
    public_id: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    *,
    tab: str,
    done: str,
    then: str | None = None,
) -> Response:
    """One write through the API, then back to the tab with a notice, or the tab with the API's
    own problem text."""
    gate = _write_gate(request)
    if isinstance(gate, Response):
        return gate
    if not _PROJECT_ID.fullmatch(public_id):
        return _not_found(request)
    result = get_api(request).request(method, f"/v1/my/projects/{public_id}{path}", json=body, cookies=gate)
    if result.status_code < 400:
        return _redirect(then or f"{_tab_href(public_id, tab)}?done={done}")
    if result.status_code in (401, 404):
        return _failure(request, result)
    return _item_page(request, public_id, tab, error=_problem(result), status_code=result.status_code)


@router.post("/my/items/{public_id}/stage")
def set_stage(request: Request, public_id: str, stage: Annotated[str, Form()] = "") -> Response:
    return _send(request, public_id, "PATCH", "", {"stage": stage}, tab="overview", done="stage")


@router.post("/my/items/{public_id}/edit")
def edit_item(
    request: Request,
    public_id: str,
    kind: Annotated[str, Form()] = "project",
    title: Annotated[str, Form()] = "",
    technology: Annotated[str, Form()] = "",
    capacity_mw: Annotated[str, Form()] = "",
    submitted_on: Annotated[str, Form()] = "",
    lat: Annotated[str, Form()] = "",
    lon: Annotated[str, Form()] = "",
) -> Response:
    body = _item_fields(
        title=title,
        technology=technology,
        capacity_mw=capacity_mw,
        stage="",
        parent_project_id=None,
        submitted_on=submitted_on,
        lat=lat,
        lon=lon,
        kind=kind,
    )
    if not (lat.strip() or lon.strip()):
        body["lat"] = body["lon"] = None
    return _send(request, public_id, "PATCH", "", body, tab="overview", done="saved")


#: The "Attach to" choice that detaches a bid; an empty choice ("Leave it as it is") writes nothing.
STANDS_ALONE: Final = "none"


@router.post("/my/items/{public_id}/parent")
def set_parent(request: Request, public_id: str, parent_project_id: Annotated[str, Form()] = "") -> Response:
    """A bid's project. The form's preselected choice is empty (the project's name, or "Leave it as
    it is" when the viewer can see none) and writes nothing: the bid may be attached to a project
    another member attached and this viewer is not a member of, and saving the form unchanged must
    not detach it, nor re-send a project since archived. Detaching is its own choice
    (`STANDS_ALONE`)."""
    if not parent_project_id:
        gate = _write_gate(request)
        if isinstance(gate, Response):
            return gate
        if not _PROJECT_ID.fullmatch(public_id):
            return _not_found(request)
        return _redirect(f"{_tab_href(public_id, 'overview')}?done=unchanged")
    body = {"parent_project_id": None if parent_project_id == STANDS_ALONE else parent_project_id}
    return _send(request, public_id, "PATCH", "", body, tab="overview", done="saved")


@router.post("/my/items/{public_id}/email-changes")
def set_email_changes(
    request: Request, public_id: str, email_changes: Annotated[str, Form()] = ""
) -> Response:
    wanted = email_changes == "on"
    return _send(request, public_id, "PATCH", "", {"email_changes": wanted}, tab="overview", done="email")


@router.post("/my/items/{public_id}/archive")
def archive_item(request: Request, public_id: str, archived: Annotated[str, Form()] = "1") -> Response:
    wanted = archived == "1"
    done = "archived" if wanted else "unarchived"
    return _send(request, public_id, "PATCH", "", {"archived": wanted}, tab="overview", done=done)


@router.post("/my/items/{public_id}/delete")
def delete_item(request: Request, public_id: str, confirm: Annotated[str, Form()] = "") -> Response:
    if confirm != "delete":
        gate = _write_gate(request)
        if isinstance(gate, Response):
            return gate
        problem: dict[str, Any] = {
            "title": "Not deleted",
            "detail": "Tick the box to confirm: deleting an item removes everything in it, for every member.",
            "request_id": None,
            "field_errors": {},
        }
        return _item_page(request, public_id, "overview", error=problem, status_code=400)
    return _send(
        request, public_id, "DELETE", "", None, tab="overview", done="deleted", then="/my?done=deleted"
    )


def _resolve_reference(api: ApiClient, text: str) -> tuple[str, str] | None:
    """A pasted Inframation address or id as `(record_type, public_id)`: an id anywhere in the
    text, else a project, notice or company page's slug resolved through the public API."""
    for record_type, pattern in _RECORD_IDS:
        match = pattern.search(text)
        if match:
            return record_type, match.group(0)
    path = urlsplit(text.strip()).path.strip("/").split("/")
    if len(path) >= 2 and path[0] in _SLUG_PAGES and path[1]:
        record_type, collection, extra = _SLUG_PAGES[path[0]]
        try:
            rows = api.get(collection, params={"slug": path[1], "limit": 1, **extra}).get("data") or []
        except ApiError:
            rows = []
        if rows and isinstance(rows[0], dict) and isinstance(rows[0].get("public_id"), str):
            return record_type, str(rows[0]["public_id"])
    return None


@router.post("/my/items/{public_id}/links")
def add_link(
    request: Request,
    public_id: str,
    reference: Annotated[str, Form()] = "",
    relation: Annotated[str, Form()] = "nearby",
) -> Response:
    gate = _write_gate(request)
    if isinstance(gate, Response):
        return gate
    resolved = _resolve_reference(get_api(request), reference)
    if resolved is None:
        problem: dict[str, Any] = {
            "title": "No record found",
            "detail": (
                "Paste the address of a project, RFP, company, plant, site or grid point page on Inframation."
            ),
            "request_id": None,
            "field_errors": {"reference": "Paste an Inframation page address."},
        }
        return _item_page(request, public_id, "linked", error=problem, status_code=400)
    record_type, record_id = resolved
    body = {"record_type": record_type, "public_id": record_id, "relation": relation}
    return _send(request, public_id, "POST", "/links", body, tab="linked", done="linked")


@router.post("/my/items/{public_id}/links/{link_id}/remove")
def remove_link(request: Request, public_id: str, link_id: str) -> Response:
    if not _ROW_ID.fullmatch(link_id):
        return _not_found(request)
    return _send(request, public_id, "DELETE", f"/links/{link_id}", None, tab="linked", done="unlinked")


@router.post("/my/items/{public_id}/checklist")
def save_checklist(
    request: Request,
    public_id: str,
    status: Annotated[list[str] | None, Form()] = None,
    was: Annotated[list[str] | None, Form()] = None,
) -> Response:
    """One form for the whole checklist: each select sends `item_id:status`, each hidden `was` the
    status the page showed; only the items that changed are written."""
    gate = _write_gate(request)
    if isinstance(gate, Response):
        return gate
    if not _PROJECT_ID.fullmatch(public_id):
        return _not_found(request)
    before = dict(pair.split(":", 1) for pair in (was or []) if ":" in pair)
    api = get_api(request)
    for pair in status or []:
        item_id, _sep, chosen = pair.partition(":")
        if not _ROW_ID.fullmatch(item_id) or before.get(item_id) == chosen:
            continue
        result = api.request(
            "PATCH", f"/v1/my/projects/{public_id}/checklist/{item_id}", json={"status": chosen}, cookies=gate
        )
        if result.status_code >= 400:
            if result.status_code in (401, 404):
                return _failure(request, result)
            return _item_page(
                request, public_id, "checklist", error=_problem(result), status_code=result.status_code
            )
    return _redirect(f"{_tab_href(public_id, 'checklist')}?done=checklist")


@router.post("/my/items/{public_id}/checklist/items")
def add_checklist_item(
    request: Request, public_id: str, name: Annotated[str, Form()] = "", folder: Annotated[str, Form()] = ""
) -> Response:
    return _send(
        request,
        public_id,
        "POST",
        "/checklist",
        {"name": name, "folder": folder},
        tab="checklist",
        done="checklist",
    )


@router.post("/my/items/{public_id}/checklist/{item_id}/evidence/{evidence_id}/{decision}")
def decide_evidence(
    request: Request, public_id: str, item_id: str, evidence_id: str, decision: str
) -> Response:
    if decision not in ("accept", "dismiss") or not (
        _ROW_ID.fullmatch(item_id) and _ROW_ID.fullmatch(evidence_id)
    ):
        return _not_found(request)
    path = f"/checklist/{item_id}/evidence/{evidence_id}/{decision}"
    return _send(request, public_id, "POST", path, None, tab="checklist", done="evidence")


@router.post("/my/items/{public_id}/notes")
def add_note(request: Request, public_id: str, body: Annotated[str, Form()] = "") -> Response:
    return _send(request, public_id, "POST", "/notes", {"body": body}, tab="notes", done="note")


@router.post("/my/items/{public_id}/notes/{note_id}/edit")
def edit_note(request: Request, public_id: str, note_id: str, body: Annotated[str, Form()] = "") -> Response:
    if not _NOTE_ID.fullmatch(note_id):
        return _not_found(request)
    return _send(request, public_id, "PATCH", f"/notes/{note_id}", {"body": body}, tab="notes", done="note")


@router.post("/my/items/{public_id}/notes/{note_id}/delete")
def delete_note(request: Request, public_id: str, note_id: str) -> Response:
    if not _NOTE_ID.fullmatch(note_id):
        return _not_found(request)
    return _send(request, public_id, "DELETE", f"/notes/{note_id}", None, tab="notes", done="note_deleted")


@router.post("/my/items/{public_id}/invitations", response_class=HTMLResponse)
def invite(
    request: Request,
    public_id: str,
    email: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "viewer",
) -> Response:
    """The link is shown once, on this response, for the owner to copy and send themselves
    (docs/19 §2.6 rules 2 and 7): never stored, emailed or put in a redirect."""
    gate = _write_gate(request)
    if isinstance(gate, Response):
        return gate
    if not _PROJECT_ID.fullmatch(public_id):
        return _not_found(request)
    result = get_api(request).post(
        f"/v1/my/projects/{public_id}/invitations", json={"email": email, "role": role}, cookies=gate
    )
    if result.status_code == 201:
        return _item_page(request, public_id, "people", issued_link=str(result.body["data"]["link"]))
    if result.status_code in (401, 404):
        return _failure(request, result)
    return _item_page(request, public_id, "people", error=_problem(result), status_code=result.status_code)


@router.post("/my/items/{public_id}/invitations/{invitation_id}/revoke")
def revoke(request: Request, public_id: str, invitation_id: str) -> Response:
    if not _INVITATION_ID.fullmatch(invitation_id):
        return _not_found(request)
    return _send(
        request, public_id, "DELETE", f"/invitations/{invitation_id}", None, tab="people", done="revoked"
    )


@router.post("/my/items/{public_id}/members/{user_id}/role")
def change_role(
    request: Request, public_id: str, user_id: str, role: Annotated[str, Form()] = ""
) -> Response:
    if not _USER_ID.fullmatch(user_id):
        return _not_found(request)
    return _send(
        request, public_id, "PATCH", f"/members/{user_id}", {"role": role}, tab="people", done="member"
    )


@router.post("/my/items/{public_id}/members/{user_id}/remove")
def remove_member(
    request: Request, public_id: str, user_id: str, leaving: Annotated[str, Form()] = ""
) -> Response:
    if not _USER_ID.fullmatch(user_id):
        return _not_found(request)
    then = "/my?done=left" if leaving == "1" else None
    return _send(
        request, public_id, "DELETE", f"/members/{user_id}", None, tab="people", done="member", then=then
    )


# ------------------------------------------------------------------------------- invitations
@router.get("/my/invitations/accept", response_class=HTMLResponse)
def invitation(request: Request) -> Response:
    """Who invited you, the item's title and the role, once the token and your verified address
    both check out; the same words for every other case (docs/19 §2.6 rule 5)."""
    token = request.query_params.get("t", "")
    cookies = _cookies(request)
    if cookies is None:
        here = f"/my/invitations/accept?t={quote(token, safe='')}"
        encoded = quote(here, safe="")
        return _page(
            request,
            "my/invitation.html",
            {
                "signed_out": True,
                "login_href": f"/login?next={encoded}",
                "register_href": f"/register?next={encoded}",
            },
        )
    result = get_api(request).post("/v1/my/invitations/preview", json={"token": token}, cookies=cookies)
    if result.status_code == 200:
        return _page(request, "my/invitation.html", {"preview": result.body["data"], "token": token})
    if result.status_code == 401:
        return _signin(request, "/my")
    return _page(request, "my/invitation.html", {"problem": _problem(result)}, status_code=result.status_code)


@router.post("/my/invitations/accept", response_class=HTMLResponse)
def accept_invitation(request: Request, token: Annotated[str, Form()] = "") -> Response:
    gate = _write_gate(request)
    if isinstance(gate, Response):
        return gate
    result = get_api(request).post("/v1/my/invitations/accept", json={"token": token}, cookies=gate)
    if result.status_code == 200:
        project = result.body["data"]["project"]
        return _redirect(f"/my/items/{project['public_id']}?done=joined")
    return _page(request, "my/invitation.html", {"problem": _problem(result)}, status_code=result.status_code)
