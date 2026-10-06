"""`/alerts`: save a list or map view as an email alert, and list, pause, resume and delete alerts
(owner decision 2026-09-30; docs/30 §3 `/alerts`, §4.3, §5.4; docs/31 §5.10; US-501, US-504).

Plain HTML forms over the existing saved-search contract (`GET/POST/PATCH/DELETE
/v1/saved-searches`, `services/api/pro.py`) -- no JavaScript is needed anywhere on these pages. The
browser's `session` cookie is forwarded per call, exactly as `web/auth.py` does; every
state-changing route checks the same-origin header first (`web/auth.py::_is_same_origin`).

**Who sees what.** Anonymous: a sign-in prompt that returns to the same page. Signed in with no
alert plan (a free account under the `commercial` posture): what alerts are and where they are
sold. Signed in with a plan (`GET /v1/me`'s `alert_plan`): the list, or the save form. The API
enforces every rule itself (cap, cadence, verified email); these pages only say the rules first,
and show the API's own problem text when it refuses.

**A view becomes a saved search** by the list pages' own passthrough lists
(`web/page.py::PROPOSAL_PASSTHROUGH_FILTERS`, `OPPORTUNITY_PASSTHROUGH_FILTERS`) and the same
lifecycle and status defaults the lists apply (`resolve_proposal_lifecycle_param`,
`opportunity_status_param`), so an alert watches exactly the records the page was showing -- the
list-to-matcher parity `tests/test_saved_search_parity.py` pins does the rest. `updated_since` and
`slug` are dropped (a sync cursor and a single record are not a watch), and so is the map's own
default `placement=exact,region`, which only says what the map can draw.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any
from urllib.parse import parse_qsl, quote, urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import QueryParams

from web.api_client import ApiClient, ApiResult
from web.auth import _csrf_rejection, _is_same_origin
from web.page import (
    OPPORTUNITY_PASSTHROUGH_FILTERS,
    PROPOSAL_PASSTHROUGH_FILTERS,
    get_api,
    templates,
)
from web.viewmodels import (
    ACTIVE_PROPOSAL_STATES,
    opportunity_status_param,
    resolve_proposal_lifecycle_param,
    technology_label,
)

router = APIRouter()

SESSION_COOKIE_NAME = "session"
ENTITIES = ("proposal", "opportunity")
#: Not a watch: a sync cursor, and a single record's own slug.
_NOT_WATCHED = frozenset({"updated_since", "slug"})
#: What the map writes into every URL by default (`web/static/js/map.js` `DEFAULT_PLACEMENT`).
_MAP_DEFAULT_PLACEMENT = "exact,region"
_SAVED_SEARCH_ID = re.compile(r"^[a-z]+_[A-Za-z0-9]{1,64}$")
_MAX_NAME_LENGTH = 120

ENTITY_LABELS = {"proposal": "Proposals", "opportunity": "Opportunities", "event": "Change events"}
ENTITY_PATHS = {"proposal": "/proposals", "opportunity": "/opportunities"}
MODE_LABELS = {
    "daily": "Daily digest",
    "weekly": "Weekly digest",
    "immediate": "Immediately (within 15 minutes)",
    "none": "No email",
}
FILTER_LABELS = {
    "technology": "Technology",
    "technologies": "Technology",
    "kind": "Kind",
    "jurisdiction": "Jurisdiction",
    "state": "State",
    "iso": "Grid operator",
    "capacity_mw[gte]": "Capacity at least (MW)",
    "capacity_mw[lte]": "Capacity at most (MW)",
    "capacity_sought_mw[gte]": "Capacity sought at least (MW)",
    "storage_mwh[gte]": "Storage at least (MWh)",
    "q": "Search",
    "lifecycle_state": "Status",
    "status": "Status",
    "placement": "Placement",
    "county_fips": "County (FIPS)",
    "slipped": "Schedule slipped",
    "slip_bucket": "Schedule",
    "source_id": "Source",
    "sponsor_id": "Sponsor",
    "issuer_id": "Issuer",
    "interconnection_point_id": "Grid point",
    "due_at[from]": "Due from",
    "due_at[to]": "Due by",
    "open_at[from]": "Opened from",
    "open_at[to]": "Opened by",
    "budget_currency": "Budget currency",
    "budget_amount[gte]": "Budget at least",
    "first_seen[from]": "First seen from",
    "first_seen[to]": "First seen by",
    "last_changed[from]": "Changed from",
    "last_changed[to]": "Changed by",
}
_ACTIVE_STATES_CSV = ",".join(ACTIVE_PROPOSAL_STATES)


# ------------------------------------------------------------------------------ view -> query
def saved_search_query(
    entity: str, params: Mapping[str, str], *, origin: str | None = None
) -> dict[str, str]:
    """The saved-search `query` for a list or map view (module docstring)."""
    if entity == "opportunity":
        query = {
            k: params[k] for k in OPPORTUNITY_PASSTHROUGH_FILTERS if params.get(k) and k not in _NOT_WATCHED
        }
        query["status"] = opportunity_status_param(params)
        return query
    query = {k: params[k] for k in PROPOSAL_PASSTHROUGH_FILTERS if params.get(k) and k not in _NOT_WATCHED}
    if origin == "map" and query.get("placement") == _MAP_DEFAULT_PLACEMENT:
        del query["placement"]
    query["lifecycle_state"], _explicit, _withdrawn = resolve_proposal_lifecycle_param(params)
    return query


def _value_words(key: str, value: str) -> str:
    if key == "lifecycle_state" and value == _ACTIVE_STATES_CSV:
        return "Active (announced through under construction)"
    if key in ("technology", "technologies"):
        return ", ".join((technology_label(v) or v).replace("_", " ") for v in value.split(","))
    return ", ".join(v.replace("_", " ") for v in value.split(","))


def filter_rows(query: Mapping[str, Any]) -> list[tuple[str, str]]:
    """`(label, value)` pairs a reader can check before saving, in the query's own order."""
    return [
        (FILTER_LABELS.get(k, k), _value_words(k, str(v))) for k, v in query.items() if v not in (None, "")
    ]


def suggested_name(entity: str, query: Mapping[str, str]) -> str:
    """A readable default: "Storage in US-TX, 50-500 MW". The reader can change it."""
    subject = query.get("technology") or query.get("technologies") or query.get("kind")
    head = _value_words("technology", subject).capitalize() if subject else ENTITY_LABELS[entity]
    if query.get("q"):
        head = f'{head} matching "{query["q"]}"'
    place = query.get("jurisdiction") or query.get("state") or query.get("iso")
    parts = [f"{head} in {place}" if place else head]
    low, high = query.get("capacity_mw[gte]"), query.get("capacity_mw[lte]")
    if low and high:
        parts.append(f"{low}-{high} MW")
    elif low:
        parts.append(f"at least {low} MW")
    elif high:
        parts.append(f"up to {high} MW")
    return ", ".join(parts)[:_MAX_NAME_LENGTH]


def results_href(entity: str, query: Mapping[str, Any]) -> str:
    path = ENTITY_PATHS.get(entity, "/proposals")
    encoded = urlencode({k: v for k, v in query.items() if v not in (None, "")})
    return path + (f"?{encoded}" if encoded else "")


def alert_card(search: Mapping[str, Any]) -> dict[str, Any]:
    """docs/31 §5.10's saved-search card: name, entity, mode, status, last run, last match count."""
    entity = str(search.get("entity") or "proposal")
    raw_query = search.get("query")
    query: dict[str, Any] = raw_query if isinstance(raw_query, dict) else {}
    last_run = search.get("last_run_at")
    return {
        "id": search.get("saved_search_id"),
        "name": search.get("name"),
        "entity_label": ENTITY_LABELS.get(entity, entity),
        "filters": filter_rows(query),
        "mode_label": MODE_LABELS.get(str(search.get("delivery_mode")), str(search.get("delivery_mode"))),
        "status": search.get("status"),
        "last_run": str(last_run)[:16].replace("T", " ") if last_run else None,
        "last_match_count": int(search.get("last_match_count") or 0),
        "results_href": results_href(entity, query) if entity in ENTITY_PATHS else None,
    }


# ------------------------------------------------------------------------------------ helpers
def _cookie(request: Request) -> dict[str, str] | None:
    value = request.cookies.get(SESSION_COOKIE_NAME)
    return {SESSION_COOKIE_NAME: value} if value else None


def _me(api: ApiClient, cookies: dict[str, str] | None) -> dict[str, Any] | None:
    if cookies is None:
        return None
    result = api.get_result("/v1/me", cookies=cookies)
    if result.status_code != 200:
        return None
    data = result.body.get("data")
    return data if isinstance(data, dict) else None


def _this_url(request: Request) -> str:
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def _signin(request: Request, *, next_url: str) -> HTMLResponse:
    encoded = quote(next_url, safe="")
    return templates.TemplateResponse(
        request,
        "alerts/signin.html",
        {"login_href": f"/login?next={encoded}", "register_href": f"/register?next={encoded}"},
    )


def _problem(result: ApiResult) -> dict[str, Any]:
    body = result.body
    return {
        "title": body.get("title") or "The alert could not be saved",
        "detail": body.get("detail"),
        "request_id": body.get("request_id"),
        "field_errors": {e["field"]: e["message"] for e in body.get("errors", []) if "field" in e},
    }


def _plan_context(me: Mapping[str, Any]) -> dict[str, Any]:
    raw_plan, raw_quota, raw_user = me.get("alert_plan"), me.get("saved_search_quota"), me.get("user")
    plan: dict[str, Any] | None = raw_plan if isinstance(raw_plan, dict) else None
    quota: dict[str, Any] = raw_quota if isinstance(raw_quota, dict) else {}
    user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
    used = int(quota.get("used") or 0)
    limit = int(plan["quota"]) if plan else int(quota.get("limit") or 0)
    return {
        "me": me,
        "plan": plan,
        "free": bool(plan and plan.get("basis") == "free"),
        "used": used,
        "limit": limit,
        "at_quota": bool(plan) and used >= limit,
        "needs_verification": bool(
            plan and plan.get("requires_verified_email") and not user.get("email_verified_at")
        ),
        "mode_options": [(m, MODE_LABELS.get(m, m)) for m in (plan or {}).get("delivery_modes", [])],
    }


# --------------------------------------------------------------------------------------- list
_DONE_NOTICES = {
    "created": "Alert saved. The first digest covers changes from now on.",
    "paused": "Alert paused. No email is sent for it until you resume it.",
    "resumed": "Alert resumed.",
    "deleted": "Alert deleted.",
}


@router.get("/alerts", response_class=HTMLResponse)
def alerts_index(request: Request) -> Response:
    api = get_api(request)
    cookies = _cookie(request)
    me = _me(api, cookies)
    if me is None:
        return _signin(request, next_url="/alerts")
    context = _plan_context(me)
    context["cards"] = []
    context["error"] = None
    if context["plan"] is not None:
        result = api.get_result("/v1/saved-searches", cookies=cookies)
        if result.status_code == 200:
            context["cards"] = [alert_card(s) for s in result.body.get("data", []) if isinstance(s, dict)]
            context["used"] = max(context["used"], len(context["cards"]))
            context["at_quota"] = context["used"] >= context["limit"]
        else:
            context["error"] = _problem(result)
    context["done"] = _DONE_NOTICES.get(request.query_params.get("done", ""))
    return templates.TemplateResponse(request, "alerts/index.html", context)


# ---------------------------------------------------------------------------------------- new
def _entity(value: str | None) -> str:
    return value if value in ENTITIES else "proposal"


def _render_new(
    request: Request,
    me: Mapping[str, Any],
    *,
    entity: str,
    query: dict[str, str],
    name: str,
    delivery_mode: str,
    error: dict[str, Any] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    context = _plan_context(me)
    context.update(
        {
            "entity": entity,
            "entity_label": ENTITY_LABELS[entity],
            "query_encoded": urlencode(query),
            "filters": filter_rows(query),
            "name": name,
            "delivery_mode": delivery_mode,
            "error": error,
            "back_href": results_href(entity, query),
        }
    )
    return templates.TemplateResponse(request, "alerts/new.html", context, status_code=status_code)


@router.get("/alerts/new", response_class=HTMLResponse)
def alerts_new(request: Request) -> Response:
    api = get_api(request)
    cookies = _cookie(request)
    me = _me(api, cookies)
    if me is None:
        return _signin(request, next_url=_this_url(request))
    qp: QueryParams = request.query_params
    entity = _entity(qp.get("entity"))
    query = saved_search_query(entity, qp, origin=qp.get("origin"))
    return _render_new(
        request, me, entity=entity, query=query, name=suggested_name(entity, query), delivery_mode="daily"
    )


@router.post("/alerts", response_class=HTMLResponse)
def alerts_create(
    request: Request,
    name: Annotated[str, Form()] = "",
    entity: Annotated[str, Form()] = "proposal",
    query: Annotated[str, Form()] = "",
    delivery_mode: Annotated[str, Form()] = "daily",
) -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    api = get_api(request)
    cookies = _cookie(request)
    me = _me(api, cookies)
    if me is None:
        return RedirectResponse(url="/login?next=/alerts", status_code=303)
    entity = _entity(entity)
    stored = dict(parse_qsl(query))
    name = name.strip()
    if not name or len(name) > _MAX_NAME_LENGTH:
        error = {
            "title": "Give the alert a name",
            "detail": None,
            "request_id": None,
            "field_errors": {"name": f"Enter a name of 1 to {_MAX_NAME_LENGTH} characters."},
        }
        return _render_new(
            request,
            me,
            entity=entity,
            query=stored,
            name=name,
            delivery_mode=delivery_mode,
            error=error,
            status_code=400,
        )
    result = api.post(
        "/v1/saved-searches",
        json={
            "name": name,
            "entity": entity,
            "query": stored,
            "delivery_mode": delivery_mode,
            "channels": ["email"],
        },
        cookies=cookies,
    )
    if result.status_code == 201:
        return RedirectResponse(url="/alerts?done=created", status_code=303)
    return _render_new(
        request,
        me,
        entity=entity,
        query=stored,
        name=name,
        delivery_mode=delivery_mode,
        error=_problem(result),
        status_code=result.status_code if result.status_code >= 400 else 400,
    )


# --------------------------------------------------------------------- pause, resume, delete
def _change(
    request: Request, saved_search_id: str, method: str, body: dict[str, Any] | None, done: str
) -> Response:
    if not _is_same_origin(request):
        return _csrf_rejection()
    cookies = _cookie(request)
    if cookies is None:
        return RedirectResponse(url="/login?next=/alerts", status_code=303)
    if not _SAVED_SEARCH_ID.match(saved_search_id):
        return RedirectResponse(url="/alerts", status_code=303)
    result = get_api(request).request(
        method, f"/v1/saved-searches/{saved_search_id}", json=body, cookies=cookies
    )
    if result.status_code == 401:
        return RedirectResponse(url="/login?next=/alerts", status_code=303)
    return RedirectResponse(
        url=f"/alerts?done={done}" if result.status_code < 400 else "/alerts", status_code=303
    )


@router.post("/alerts/{saved_search_id}/pause")
def alerts_pause(request: Request, saved_search_id: str) -> Response:
    return _change(request, saved_search_id, "PATCH", {"status": "paused"}, "paused")


@router.post("/alerts/{saved_search_id}/resume")
def alerts_resume(request: Request, saved_search_id: str) -> Response:
    return _change(request, saved_search_id, "PATCH", {"status": "active"}, "resumed")


@router.post("/alerts/{saved_search_id}/delete")
def alerts_delete(request: Request, saved_search_id: str) -> Response:
    return _change(request, saved_search_id, "DELETE", None, "deleted")
