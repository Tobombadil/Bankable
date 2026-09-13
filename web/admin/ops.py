"""Admin panel: Keys, Costs and Audit screens (Sprint 3 item 3, this agent's slice: docs/30 §1.3
nav order, US-701/909/901 AC2). `router = APIRouter()`; the coordinator mounts it next to
`web/admin/people.py`'s. Every route depends on `require_operator` and renders through
`web/admin/shell.py`'s `render()`/`problem_notice()`, reading only through `ctx.api` — same rule
as `web/admin/people.py`.

Decisions:

1. **The key-issue form posts back to `GET /admin/keys` itself** rather than a separate route, so a
   failed issue re-renders the same list+form the operator was already looking at (keeping their
   input) and a successful one renders the one-time secret page directly -- not a `303` redirect,
   which would either lose the secret (nowhere to carry it but the URL/a session, both wrong for a
   value the API itself never stores) or require inventing a second place to hold it. Every other
   write in this module still follows the shell's normal "303 with `?flash=` on 2xx, re-render with
   `notice=` otherwise" pattern; this is the one deliberate exception, matching the task brief's own
   carve-out ("render a one-time page ... it will not be shown again").
2. **`licence_accepted_version` is never a form field.** `ApiKeyCreate.licence_accepted_version`
   must equal `services.api.pro.API_LICENCE_VERSION` exactly or the write is refused (400) --
   there is nothing for an operator to choose, so the form sends the current constant automatically
   rather than exposing a text box that only ever has one valid value.
3. **The keys list's "status" filter is the API's own `revoked` boolean** (`GET /admin/v1/keys`
   has no `status` query param) -- rendered as an active/revoked choice rather than inventing a
   status vocabulary the API does not have.
4. **`GET /admin/audit/{event_id}` has no backing single-item API operation** (`admin_list_audit`
   is the only audit read; `api/openapi.yaml` never grew an `adminGetAuditEvent`). This module
   fetches pages of `GET /admin/v1/audit` (unfiltered by default, `subject_type`/`subject_id` from
   the query string when the caller supplies them to narrow the search) and scans for a row whose
   `id` matches, capped at `_AUDIT_DETAIL_PAGE_CAP` pages so a miss fails fast instead of walking
   the whole table. **Flagged for the coordinator**: an `adminGetAuditEvent` operation (or an `id`
   filter on the list) would make this exact and O(1) instead of a bounded scan.
5. **Costs totals are summed in this module from the rows the API returns**, per the task brief
   ("a totals row computed server-side in the page module"), rather than trusting
   `CostReportResponse.data.totals` outright. `cost_usd`/`model_calls`/`input_tokens`/
   `output_tokens`/`records_changed` are exact sums (the same values that produced the API's own
   totals). `cache_hit_rate` is a `model_calls`-weighted average across rows that have one -- an
   approximation, since a `CostRow` carries only the ratio, not the underlying hit/attempt counts,
   so an exact reconstruction is not possible from the rows alone; documented here rather than
   silently presented as exact.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from web.admin.shell import AdminContext, problem_notice, render, require_operator, require_same_origin

router = APIRouter()

#: services.api.pro.API_LICENCE_VERSION -- not imported (that module is outside this agent's read
#: scope beyond admin_posts.py's keys routes) but this literal is the same constant
#: `services/api/admin_posts.py`'s `admin_create_key` checks `licence_accepted_version` against;
#: kept as a named constant here so a bump is a one-line change, not a scattered literal.
_API_LICENCE_VERSION = "api-licence-1.0"

_COST_PURPOSES: tuple[str, ...] = ("extract", "adjudicate", "draft", "classify", "other")
_SUBJECT_TYPES: tuple[str, ...] = (
    "proposal",
    "opportunity",
    "organization",
    "match",
    "document",
    "source",
    "licence",
    "post",
    "user",
    "account",
    "api_key",
)

_AUDIT_DETAIL_PAGE_CAP = 10  # decision 4: bounded scan, not an unbounded table walk
_AUDIT_DETAIL_PAGE_LIMIT = 200


def _clean(**kwargs: str | None) -> dict[str, str]:
    return {k: v for k, v in kwargs.items() if v}


def _flash(request: Request) -> str | None:
    return request.query_params.get("flash")


def _redirect_with_flash(path: str, message: str) -> RedirectResponse:
    return RedirectResponse(url=f"{path}?{urlencode({'flash': message})}", status_code=303)


def _next_href(request: Request, next_cursor: str | None) -> str | None:
    if not next_cursor:
        return None
    params = dict(request.query_params)
    params["cursor"] = next_cursor
    return f"{request.url.path}?{urlencode(params)}"


def _describe_filters(filters: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in filters.items())


# ================================================================================================= keys
_KEY_LIST_FILTER_KEYS = ("account_id", "status")


@router.get("/admin/keys")
def list_keys(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    filters = {k: v for k in _KEY_LIST_FILTER_KEYS if (v := request.query_params.get(k))}
    revoked = "true" if filters.get("status") == "revoked" else None
    params = {
        **_clean(account_id=filters.get("account_id"), revoked=revoked),
        **_clean(cursor=request.query_params.get("cursor")),
    }
    result = ctx.api.get("/admin/v1/keys", params=params)
    if result.status_code != 200:
        return render(
            request,
            "admin/keys/list.html",
            {
                "notice": problem_notice(result),
                "keys": [],
                "filters": filters,
                "next_href": None,
                "values": {},
            },
            ctx=ctx,
            nav_key="keys",
            status_code=result.status_code,
        )
    keys = result.body.get("data", [])
    page = result.body.get("page", {})
    return render(
        request,
        "admin/keys/list.html",
        {
            "flash": _flash(request),
            "keys": keys,
            "filters": filters,
            "filters_description": _describe_filters(filters),
            "next_href": _next_href(request, page.get("next_cursor")),
            "values": {},
        },
        ctx=ctx,
        nav_key="keys",
    )


def _rerender_keys_list(
    request: Request, ctx: AdminContext, *, notice: dict[str, Any], values: dict[str, str], status_code: int
) -> Response:
    filters = {k: v for k in _KEY_LIST_FILTER_KEYS if (v := request.query_params.get(k))}
    current = ctx.api.get("/admin/v1/keys")
    keys = current.body.get("data", []) if current.status_code == 200 else []
    return render(
        request,
        "admin/keys/list.html",
        {
            "notice": notice,
            "keys": keys,
            "filters": filters,
            "filters_description": _describe_filters(filters),
            "next_href": None,
            "values": values,
        },
        ctx=ctx,
        nav_key="keys",
        status_code=status_code,
    )


@router.post("/admin/keys")
def issue_key(
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    name: Annotated[str, Form()],
    account_id: Annotated[str, Form()],
    licence_acceptance_ref: Annotated[str, Form()],
    reason: Annotated[str, Form()],
    rate_limit_per_hour: Annotated[str, Form()] = "",
    daily_quota: Annotated[str, Form()] = "",
) -> Response:
    rejection = require_same_origin(request)
    if rejection is not None:
        return rejection
    values = {
        "name": name,
        "account_id": account_id,
        "licence_acceptance_ref": licence_acceptance_ref,
        "reason": reason,
        "rate_limit_per_hour": rate_limit_per_hour,
        "daily_quota": daily_quota,
    }
    body: dict[str, Any] = {
        "name": name,
        "account_id": account_id,
        "licence_acceptance_ref": licence_acceptance_ref,
        "reason": reason,
        "licence_accepted_version": _API_LICENCE_VERSION,  # decision 2
    }
    if rate_limit_per_hour.strip():
        try:
            body["rate_limit_per_hour"] = int(rate_limit_per_hour)
        except ValueError:
            body["rate_limit_per_hour"] = rate_limit_per_hour
    if daily_quota.strip():
        try:
            body["daily_quota"] = int(daily_quota)
        except ValueError:
            body["daily_quota"] = daily_quota

    result = ctx.api.post("/admin/v1/keys", json=body)
    if result.status_code == 201:
        return render(
            request,
            "admin/keys/issued.html",
            {"key": result.body.get("data", {})},
            ctx=ctx,
            nav_key="keys",
            status_code=201,
        )
    return _rerender_keys_list(
        request, ctx, notice=problem_notice(result), values=values, status_code=result.status_code
    )


@router.post("/admin/keys/{key_id}/revoke")
def revoke_key(
    key_id: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    reason: Annotated[str, Form()],
) -> Response:
    rejection = require_same_origin(request)
    if rejection is not None:
        return rejection
    result = ctx.api.delete(f"/admin/v1/keys/{key_id}", json={"reason": reason})
    if result.status_code == 204:
        return _redirect_with_flash("/admin/keys", "Key revoked.")
    return _rerender_keys_list(
        request, ctx, notice=problem_notice(result), values={}, status_code=result.status_code
    )


# ================================================================================================ costs
def _compute_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Decision 5: sums computed here from the rows the page already has, not a pass-through of
    the API's own `totals` field."""
    cost_usd = sum(float(r.get("cost_usd") or 0) for r in rows)
    model_calls = sum(int(r.get("model_calls") or 0) for r in rows)
    input_tokens = sum(int(r.get("input_tokens") or 0) for r in rows)
    output_tokens = sum(int(r.get("output_tokens") or 0) for r in rows)

    changed_rows = [r for r in rows if r.get("records_changed") is not None]
    records_changed = sum(int(r["records_changed"]) for r in changed_rows) if changed_rows else None

    hit_rows = [r for r in rows if r.get("cache_hit_rate") is not None]
    hit_weight = sum(int(r.get("model_calls") or 0) for r in hit_rows)
    cache_hit_rate = (
        sum(float(r["cache_hit_rate"]) * int(r.get("model_calls") or 0) for r in hit_rows) / hit_weight
        if hit_weight
        else None
    )
    cost_per_changed_record = cost_usd / records_changed if records_changed else None
    return {
        "cost_usd": round(cost_usd, 6),
        "model_calls": model_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "records_changed": records_changed,
        "cache_hit_rate": cache_hit_rate,
        "cost_per_changed_record": cost_per_changed_record,
    }


@router.get("/admin/costs")
def get_costs(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    qp = request.query_params
    filters = _clean(
        **{
            "day[from]": qp.get("day[from]"),
            "day[to]": qp.get("day[to]"),
            "source_id": qp.get("source_id"),
            "purpose": qp.get("purpose"),
        }
    )
    group_by_selected = qp.getlist("group_by")
    params = dict(filters)
    if group_by_selected:
        params["group_by"] = ",".join(group_by_selected)

    result = ctx.api.get("/admin/v1/costs", params=params)
    if result.status_code != 200:
        return render(
            request,
            "admin/costs/list.html",
            {
                "notice": problem_notice(result),
                "rows": [],
                "totals": None,
                "filters": filters,
                "purposes": _COST_PURPOSES,
                "group_by_selected": group_by_selected,
            },
            ctx=ctx,
            nav_key="costs",
            status_code=result.status_code,
        )
    data = result.body.get("data", {})
    rows = data.get("rows", [])
    return render(
        request,
        "admin/costs/list.html",
        {
            "rows": rows,
            "totals": _compute_totals(rows),
            "filters": filters,
            "filters_description": _describe_filters(filters),
            "purposes": _COST_PURPOSES,
            "group_by_selected": group_by_selected or ["source_id"],
            "threshold_usd_per_changed_record": data.get("threshold_usd_per_changed_record"),
            "flagged_sources": data.get("flagged_sources", []),
        },
        ctx=ctx,
        nav_key="costs",
    )


# ================================================================================================ audit
_AUDIT_LIST_FILTER_KEYS = ("subject_type", "event_type", "actor_user_id", "since")


@router.get("/admin/audit")
def list_audit(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    filters = {k: v for k in _AUDIT_LIST_FILTER_KEYS if (v := request.query_params.get(k))}
    params = {**filters, **_clean(cursor=request.query_params.get("cursor"))}
    result = ctx.api.get("/admin/v1/audit", params=params)
    if result.status_code != 200:
        return render(
            request,
            "admin/audit/list.html",
            {
                "notice": problem_notice(result),
                "events": [],
                "filters": filters,
                "next_href": None,
                "subject_types": _SUBJECT_TYPES,
            },
            ctx=ctx,
            nav_key="audit",
            status_code=result.status_code,
        )
    events = result.body.get("data", [])
    page = result.body.get("page", {})
    return render(
        request,
        "admin/audit/list.html",
        {
            "flash": _flash(request),
            "events": events,
            "filters": filters,
            "filters_description": _describe_filters(filters),
            "next_href": _next_href(request, page.get("next_cursor")),
            "subject_types": _SUBJECT_TYPES,
        },
        ctx=ctx,
        nav_key="audit",
    )


def _find_audit_event(ctx: AdminContext, event_id: str, *, subject_type: str | None) -> dict[str, Any] | None:
    """Decision 4: page through `GET /admin/v1/audit`, narrowed by `subject_type` when the caller
    (the audit list's link) supplies one, scanning at most `_AUDIT_DETAIL_PAGE_CAP` pages for a row
    whose `id` matches. Narrowing by `subject_id` too would be more precise but is not safe to add:
    `admin_list_audit`'s own `_resolve_any_subject_uuid` only decodes `prop_`/`opp_`/`org_`/`mat_`/
    `evt_`/`doc_` ids (falling back to hashing anything else as if it were a literal source id) --
    passing a `usr_`/`acc_`/`key_` subject id (exactly the ones `AnyPublicIdValue` was widened to
    cover for the *response*) silently matches nothing rather than narrowing correctly. Flagged for
    the coordinator alongside decision 4's `adminGetAuditEvent` gap."""
    params = _clean(subject_type=subject_type)
    cursor: str | None = None
    for _ in range(_AUDIT_DETAIL_PAGE_CAP):
        page_params = {**params, "limit": str(_AUDIT_DETAIL_PAGE_LIMIT)}
        if cursor:
            page_params["cursor"] = cursor
        result = ctx.api.get("/admin/v1/audit", params=page_params)
        if result.status_code != 200:
            return None
        for row in result.body.get("data", []):
            if row.get("id") == event_id:
                return dict(row)
        page = result.body.get("page", {})
        cursor = page.get("next_cursor")
        if not page.get("has_more") or not cursor:
            break
    return None


@router.get("/admin/audit/{event_id}")
def get_audit_event(
    event_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    event = _find_audit_event(ctx, event_id, subject_type=request.query_params.get("subject_type"))
    if event is None:
        return render(
            request,
            "admin/audit/detail.html",
            {
                "notice": {
                    "status": 404,
                    "title": "Audit event not found",
                    "detail": (
                        "No audit event with this id was found in the most recent "
                        f"{_AUDIT_DETAIL_PAGE_CAP * _AUDIT_DETAIL_PAGE_LIMIT} events. Narrow the "
                        "search from the audit list's subject filter and follow its link instead."
                    ),
                    "request_id": None,
                    "errors": {},
                },
                "event": None,
            },
            ctx=ctx,
            nav_key="audit",
            status_code=404,
        )
    return render(request, "admin/audit/detail.html", {"event": event}, ctx=ctx, nav_key="audit")


__all__ = ["router"]
