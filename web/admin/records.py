"""Records, resolution and extraction review screens (Sprint 3 item 3; docs/30-design-ia.md
§1.3, §4.6; docs/10-prd-mvp.md US-201, US-202, US-905, US-906, US-907, US-1002). Server-rendered
over `/admin/v1` through `ctx.api` (`web/admin/shell.py`), same POST contract as
`web/admin/sources.py`: `require_same_origin` first, then the API call, then a 303 redirect with
`?flash=` on success or the page re-rendered with `problem_notice(result)` and the submitted
values at the API's own status code on a problem.

Decisions
---------
D1. **Vocab tuples (`PROPOSAL_KINDS`, `OPPORTUNITY_KINDS`, `ORGANIZATION_TYPES`) are imported
    directly from `services/api/admin_records.py`** rather than duplicated here. That module is a
    read-only file this task already depends on for the exact request shape its handlers accept;
    importing its three plain tuples keeps this page's `<select>` options from silently drifting
    out of sync with what the API actually validates, at the cost of one import from another
    agent's file area (a read import, not a write — this module never edits that file).
D2. **Merge is one route with a `step` field** (`preview` then `apply`), not two URLs. The preview
    response (conflicts, `preview_token`) has nowhere durable to live between two separate page
    loads without session storage this task's file list does not include, so the preview is
    rendered directly as the POST response body (200, not a redirect) with the conflicts, a
    per-field `surviving`/`absorbed` choice, and the `preview_token` carried forward as a hidden
    field for the second POST — exactly the two-step shape the task brief describes ("preview
    true, render the preview... in a hidden field; step 2: POST with preview false + token").
D3. **The opportunity edit form's `technologies` field is one comma-separated text input**, split
    into a list on submit — `AdminOpportunityUpdate.technologies` is an array of free-text tokens
    with no fixed enum in `api/openapi.yaml`, so a repeatable-add widget would need JavaScript this
    screen is not allowed to use (hard rule: "no JavaScript needed").
D4. **Organisation records never show a publish-state form.** `PUT
    /admin/v1/records/{record_type}/{public_id}/publish-state` refuses `record_type=organizations`
    with `400` (`services/api/admin_records.py` decision 5: no `publish_state` column exists on
    `organization`); rendering a form that always fails would be a worse UX than omitting it, and
    the organisation edit form here is deliberately the minimal one the task brief asks for (name,
    website, reason).
D5. **Resolution `defer` posts to the same `decide` endpoint with `decision=defer`** rather than
    being a no-op link, so the API's actual behaviour (no state change, candidate stays `pending`)
    is exercised through one consistent form rather than a client-side skip that never touches
    the API at all.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from services.api.admin_records import OPPORTUNITY_KINDS, ORGANIZATION_TYPES, PROPOSAL_KINDS
from services.db.models import LIFECYCLE_STATES, OPPORTUNITY_STATUSES, RECORD_PUBLISH_STATES
from web.admin.shell import (
    AdminContext,
    problem_notice,
    render,
    require_operator,
    require_same_origin,
    templates,
)

router = APIRouter()

_EXTRACTION_STATUS_VALUES = ("proposed", "accepted", "rejected", "superseded")
_RESOLUTION_STATUS_VALUES = ("pending", "decided")


def _count_citations(extraction: dict[str, Any]) -> int:
    return len(extraction.get("citations") or [])


templates.env.globals["count_citations"] = _count_citations


# ======================================================================================= lookup
@router.get("/admin/records", response_class=HTMLResponse)
def records_lookup_form(
    request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    return render(
        request,
        "admin/records/lookup.html",
        {"notice": None, "value": request.query_params.get("public_id", "")},
        ctx=ctx,
        nav_key="records",
    )


@router.get("/admin/records/lookup", response_class=HTMLResponse)
def records_lookup(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    public_id = request.query_params.get("public_id", "").strip()
    if public_id.startswith("prop_"):
        return RedirectResponse(url=f"/admin/records/proposals/{public_id}", status_code=303)
    if public_id.startswith("opp_"):
        return RedirectResponse(url=f"/admin/records/opportunities/{public_id}", status_code=303)
    if public_id.startswith("org_"):
        return RedirectResponse(url=f"/admin/records/organizations/{public_id}", status_code=303)
    return render(
        request,
        "admin/records/lookup.html",
        {
            "notice": {
                "title": "Unrecognised id",
                "detail": "Enter a proposal (prop_...), opportunity (opp_...) or organization (org_...) id.",
                "status": 400,
                "request_id": None,
                "errors": {},
            },
            "value": public_id,
        },
        ctx=ctx,
        nav_key="records",
        status_code=400,
    )


# ---------------------------------------------------------------------------------- shared helper
def _redirect(path: str, message: str) -> RedirectResponse:
    return RedirectResponse(url=f"{path}?flash={message}", status_code=303)


# ==================================================================================== proposals
def _get_proposal(ctx: AdminContext, public_id: str) -> tuple[dict[str, Any] | None, Any]:
    result = ctx.api.get(f"/admin/v1/proposals/{public_id}")
    return (result.body.get("data") if result.status_code == 200 else None), result


def _proposal_context(
    ctx: AdminContext,
    public_id: str,
    *,
    flash: str | None = None,
    notice: dict[str, Any] | None = None,
    merge_preview: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    proposal, result = _get_proposal(ctx, public_id)
    context = {
        "proposal": proposal,
        "notice": notice
        if notice is not None
        else (None if proposal is not None else problem_notice(result)),
        "flash": flash,
        "publish_state_values": RECORD_PUBLISH_STATES,
        "proposal_kinds": PROPOSAL_KINDS,
        "lifecycle_states": LIFECYCLE_STATES,
        "merge_preview": merge_preview,
    }
    return context, (200 if proposal is not None else result.status_code)


@router.get("/admin/records/proposals/{public_id}", response_class=HTMLResponse)
def proposal_detail(
    public_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    context, status_code = _proposal_context(ctx, public_id, flash=request.query_params.get("flash"))
    return render(
        request,
        "admin/records/proposal_detail.html",
        context,
        ctx=ctx,
        nav_key="records",
        status_code=status_code,
    )


@router.post("/admin/records/proposals/{public_id}/edit", response_class=HTMLResponse)
async def edit_proposal(
    public_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    body: dict[str, Any] = {"reason": str(form.get("reason", ""))}
    for field in ("name_canonical", "kind", "jurisdiction", "iso", "lifecycle_state"):
        value = str(form.get(field, "")).strip()
        if value:
            body[field] = value
    for field in ("technology",):
        value = str(form.get(field, "")).strip()
        body[field] = value or None
    for field in ("capacity_mw", "storage_mwh"):
        raw = str(form.get(field, "")).strip()
        body[field] = float(raw) if raw else None
    proposed_online_date = str(form.get("proposed_online_date", "")).strip()
    body["proposed_online_date"] = proposed_online_date or None
    result = ctx.api.patch(f"/admin/v1/proposals/{public_id}", json=body)
    if result.status_code == 200:
        return _redirect(f"/admin/records/proposals/{public_id}", "Proposal+updated")
    context, status_code = _proposal_context(ctx, public_id, notice=problem_notice(result))
    return render(
        request,
        "admin/records/proposal_detail.html",
        context,
        ctx=ctx,
        nav_key="records",
        status_code=status_code,
    )


@router.post("/admin/records/proposals/{public_id}/publish-state", response_class=HTMLResponse)
async def set_proposal_publish_state(
    public_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    body = {
        "publish_state": str(form.get("publish_state", "")),
        "takedown": str(form.get("takedown", "")) == "on",
        "reason": str(form.get("reason", "")),
    }
    result = ctx.api.put(f"/admin/v1/records/proposals/{public_id}/publish-state", json=body)
    if result.status_code == 200:
        return _redirect(f"/admin/records/proposals/{public_id}", "Publish+state+updated")
    context, status_code = _proposal_context(ctx, public_id, notice=problem_notice(result))
    return render(
        request,
        "admin/records/proposal_detail.html",
        context,
        ctx=ctx,
        nav_key="records",
        status_code=status_code,
    )


@router.post("/admin/records/proposals/{public_id}/merge", response_class=HTMLResponse)
async def merge_proposal_route(
    public_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    step = str(form.get("step", "preview"))
    absorb_public_id = str(form.get("absorb_public_id", "")).strip()

    field_choices: dict[str, str] = {}
    for key, value in form.items():
        if key.startswith("field_choice__"):
            field_choices[key[len("field_choice__") :]] = str(value)

    if step == "apply":
        body: dict[str, Any] = {
            "absorb_public_id": absorb_public_id,
            "preview": False,
            "preview_token": str(form.get("preview_token", "")),
            "field_choices": field_choices,
            "reason": str(form.get("reason", "")),
        }
        result = ctx.api.post(f"/admin/v1/proposals/{public_id}/merge", json=body)
        if result.status_code == 200:
            return _redirect(f"/admin/records/proposals/{public_id}", "Merge+applied")
        context, status_code = _proposal_context(ctx, public_id, notice=problem_notice(result))
        return render(
            request,
            "admin/records/proposal_detail.html",
            context,
            ctx=ctx,
            nav_key="records",
            status_code=status_code,
        )

    body = {"absorb_public_id": absorb_public_id, "preview": True, "field_choices": field_choices}
    result = ctx.api.post(f"/admin/v1/proposals/{public_id}/merge", json=body)
    if result.status_code != 200:
        context, status_code = _proposal_context(ctx, public_id, notice=problem_notice(result))
        return render(
            request,
            "admin/records/proposal_detail.html",
            context,
            ctx=ctx,
            nav_key="records",
            status_code=status_code,
        )
    data = result.body.get("data", {})
    merge_preview = {
        "absorb_public_id": absorb_public_id,
        "preview_token": data.get("preview_token"),
        "conflicts": data.get("conflicts", []),
        "absorbed": data.get("absorbed"),
    }
    context, status_code = _proposal_context(ctx, public_id, merge_preview=merge_preview)
    return render(
        request,
        "admin/records/proposal_detail.html",
        context,
        ctx=ctx,
        nav_key="records",
        status_code=status_code,
    )


@router.post("/admin/records/proposals/{public_id}/unmerge", response_class=HTMLResponse)
async def unmerge_proposal_route(
    public_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    body = {"merge_event_id": str(form.get("merge_event_id", "")), "reason": str(form.get("reason", ""))}
    result = ctx.api.post(f"/admin/v1/proposals/{public_id}/unmerge", json=body)
    if result.status_code == 200:
        return _redirect(f"/admin/records/proposals/{public_id}", "Merge+reversed")
    context, status_code = _proposal_context(ctx, public_id, notice=problem_notice(result))
    return render(
        request,
        "admin/records/proposal_detail.html",
        context,
        ctx=ctx,
        nav_key="records",
        status_code=status_code,
    )


# ================================================================================= opportunities
def _get_opportunity(ctx: AdminContext, public_id: str) -> tuple[dict[str, Any] | None, Any]:
    result = ctx.api.get(f"/admin/v1/opportunities/{public_id}")
    return (result.body.get("data") if result.status_code == 200 else None), result


def _opportunity_context(
    ctx: AdminContext, public_id: str, *, flash: str | None = None, notice: dict[str, Any] | None = None
) -> tuple[dict[str, Any], int]:
    opportunity, result = _get_opportunity(ctx, public_id)
    context = {
        "opportunity": opportunity,
        "notice": notice
        if notice is not None
        else (None if opportunity is not None else problem_notice(result)),
        "flash": flash,
        "publish_state_values": RECORD_PUBLISH_STATES,
        "opportunity_kinds": OPPORTUNITY_KINDS,
        "opportunity_statuses": OPPORTUNITY_STATUSES,
    }
    return context, (200 if opportunity is not None else result.status_code)


@router.get("/admin/records/opportunities/{public_id}", response_class=HTMLResponse)
def opportunity_detail(
    public_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    context, status_code = _opportunity_context(ctx, public_id, flash=request.query_params.get("flash"))
    return render(
        request,
        "admin/records/opportunity_detail.html",
        context,
        ctx=ctx,
        nav_key="records",
        status_code=status_code,
    )


@router.post("/admin/records/opportunities/{public_id}/edit", response_class=HTMLResponse)
async def edit_opportunity(
    public_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    body: dict[str, Any] = {"reason": str(form.get("reason", ""))}
    for field in ("title", "kind", "jurisdiction", "status", "budget_currency"):
        value = str(form.get(field, "")).strip()
        if value:
            body[field] = value
    summary = str(form.get("summary", "")).strip()
    body["summary"] = summary or None
    technologies_raw = str(form.get("technologies", "")).strip()
    if technologies_raw:
        body["technologies"] = [t.strip() for t in technologies_raw.split(",") if t.strip()]
    for field in ("capacity_sought_mw", "budget_amount"):
        raw = str(form.get(field, "")).strip()
        body[field] = float(raw) if raw else None
    open_at = str(form.get("open_at", "")).strip()
    body["open_at"] = open_at or None
    due_at = str(form.get("due_at", "")).strip()
    body["due_at"] = f"{due_at}T00:00:00Z" if due_at else None
    result = ctx.api.patch(f"/admin/v1/opportunities/{public_id}", json=body)
    if result.status_code == 200:
        return _redirect(f"/admin/records/opportunities/{public_id}", "Opportunity+updated")
    context, status_code = _opportunity_context(ctx, public_id, notice=problem_notice(result))
    return render(
        request,
        "admin/records/opportunity_detail.html",
        context,
        ctx=ctx,
        nav_key="records",
        status_code=status_code,
    )


@router.post("/admin/records/opportunities/{public_id}/publish-state", response_class=HTMLResponse)
async def set_opportunity_publish_state(
    public_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    body = {
        "publish_state": str(form.get("publish_state", "")),
        "takedown": str(form.get("takedown", "")) == "on",
        "reason": str(form.get("reason", "")),
    }
    result = ctx.api.put(f"/admin/v1/records/opportunities/{public_id}/publish-state", json=body)
    if result.status_code == 200:
        return _redirect(f"/admin/records/opportunities/{public_id}", "Publish+state+updated")
    context, status_code = _opportunity_context(ctx, public_id, notice=problem_notice(result))
    return render(
        request,
        "admin/records/opportunity_detail.html",
        context,
        ctx=ctx,
        nav_key="records",
        status_code=status_code,
    )


# ================================================================================= organizations
@router.get("/admin/records/organizations/{public_id}", response_class=HTMLResponse)
def organization_detail(
    public_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    result = ctx.api.get(f"/v1/organizations/{public_id}")
    org = result.body.get("data") if result.status_code == 200 else None
    return render(
        request,
        "admin/records/organization_detail.html",
        {
            "organization": org,
            "notice": None if org is not None else problem_notice(result),
            "flash": request.query_params.get("flash"),
            "organization_types": ORGANIZATION_TYPES,
        },
        ctx=ctx,
        nav_key="records",
        status_code=200 if org is not None else result.status_code,
    )


@router.post("/admin/records/organizations/{public_id}/edit", response_class=HTMLResponse)
async def edit_organization(
    public_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    body: dict[str, Any] = {"reason": str(form.get("reason", ""))}
    name_canonical = str(form.get("name_canonical", "")).strip()
    if name_canonical:
        body["name_canonical"] = name_canonical
    website = str(form.get("website", "")).strip()
    body["website"] = website or None
    result = ctx.api.patch(f"/admin/v1/organizations/{public_id}", json=body)
    if result.status_code == 200:
        return _redirect(f"/admin/records/organizations/{public_id}", "Organization+updated")
    org_result = ctx.api.get(f"/v1/organizations/{public_id}")
    org = org_result.body.get("data") if org_result.status_code == 200 else None
    return render(
        request,
        "admin/records/organization_detail.html",
        {
            "organization": org,
            "notice": problem_notice(result),
            "flash": None,
            "organization_types": ORGANIZATION_TYPES,
        },
        ctx=ctx,
        nav_key="records",
        status_code=result.status_code,
    )


# =================================================================================== resolution
@router.get("/admin/resolution", response_class=HTMLResponse)
def resolution_list(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    status_filter = request.query_params.get("status", "pending")
    result = ctx.api.get("/admin/v1/resolution-candidates", params={"status": status_filter})
    candidates = result.body.get("data", []) if result.status_code == 200 else []
    return render(
        request,
        "admin/resolution/list.html",
        {
            "candidates": candidates,
            "status_filter": status_filter,
            "status_values": _RESOLUTION_STATUS_VALUES,
            "notice": None if result.status_code == 200 else problem_notice(result),
            "flash": request.query_params.get("flash"),
        },
        ctx=ctx,
        nav_key="resolution",
        status_code=200 if result.status_code == 200 else result.status_code,
    )


@router.post("/admin/resolution/{candidate_id}/decide", response_class=HTMLResponse)
async def decide_resolution_candidate(
    candidate_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    body: dict[str, Any] = {
        "decision": str(form.get("decision", "")),
        "reason": str(form.get("reason", "")),
    }
    surviving = str(form.get("surviving_public_id", "")).strip()
    if surviving:
        body["surviving_public_id"] = surviving
    result = ctx.api.post(f"/admin/v1/resolution-candidates/{candidate_id}/decide", json=body)
    status_filter = request.query_params.get("status", "pending")
    if result.status_code == 200:
        return _redirect(f"/admin/resolution?status={status_filter}", "Decision+recorded")
    list_result = ctx.api.get("/admin/v1/resolution-candidates", params={"status": status_filter})
    candidates = list_result.body.get("data", []) if list_result.status_code == 200 else []
    return render(
        request,
        "admin/resolution/list.html",
        {
            "candidates": candidates,
            "status_filter": status_filter,
            "status_values": _RESOLUTION_STATUS_VALUES,
            "notice": problem_notice(result),
            "flash": None,
        },
        ctx=ctx,
        nav_key="resolution",
        status_code=result.status_code,
    )


# =================================================================================== extractions
@router.get("/admin/extractions", response_class=HTMLResponse)
def extractions_list(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    qp = request.query_params
    filters = {"status": qp.get("status", ""), "subject_type": qp.get("subject_type", "")}
    params = {k: v for k, v in filters.items() if v}
    result = ctx.api.get("/admin/v1/extractions", params=params)
    extractions = result.body.get("data", []) if result.status_code == 200 else []
    return render(
        request,
        "admin/extractions/list.html",
        {
            "extractions": extractions,
            "filters": filters,
            "status_values": _EXTRACTION_STATUS_VALUES,
            "notice": None if result.status_code == 200 else problem_notice(result),
            "flash": request.query_params.get("flash"),
        },
        ctx=ctx,
        nav_key="extractions",
        status_code=200 if result.status_code == 200 else result.status_code,
    )


def _extractions_redirect_context(
    request: Request, ctx: AdminContext, result: Any
) -> tuple[dict[str, Any], int]:
    qp = request.query_params
    filters = {"status": qp.get("status", ""), "subject_type": qp.get("subject_type", "")}
    params = {k: v for k, v in filters.items() if v}
    list_result = ctx.api.get("/admin/v1/extractions", params=params)
    extractions = list_result.body.get("data", []) if list_result.status_code == 200 else []
    return (
        {
            "extractions": extractions,
            "filters": filters,
            "status_values": _EXTRACTION_STATUS_VALUES,
            "notice": problem_notice(result),
            "flash": None,
        },
        result.status_code,
    )


@router.post("/admin/extractions/{extraction_id}/accept", response_class=HTMLResponse)
async def accept_extraction(
    extraction_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    reason = str(form.get("reason", "")).strip()
    body = {"reason": reason} if reason else {}
    result = ctx.api.post(f"/admin/v1/extractions/{extraction_id}/accept", json=body)
    if result.status_code == 200:
        return _redirect("/admin/extractions", "Extraction+accepted")
    context, status_code = _extractions_redirect_context(request, ctx, result)
    return render(
        request,
        "admin/extractions/list.html",
        context,
        ctx=ctx,
        nav_key="extractions",
        status_code=status_code,
    )


@router.post("/admin/extractions/{extraction_id}/reject", response_class=HTMLResponse)
async def reject_extraction(
    extraction_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    body = {"reason": str(form.get("reason", ""))}
    result = ctx.api.post(f"/admin/v1/extractions/{extraction_id}/reject", json=body)
    if result.status_code == 200:
        return _redirect("/admin/extractions", "Extraction+rejected")
    context, status_code = _extractions_redirect_context(request, ctx, result)
    return render(
        request,
        "admin/extractions/list.html",
        context,
        ctx=ctx,
        nav_key="extractions",
        status_code=status_code,
    )


__all__ = ["router"]
