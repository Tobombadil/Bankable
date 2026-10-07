"""Admin tasks screens (Sprint 3 item 3, `docs/30-design-ia.md` §1.3/§4.5/§4.6, `docs/30` §6):
the queue for reports, intake review and deletion requests, server-rendered over `/admin/v1/tasks*`.

Privacy requests from people named in records (`/admin/v1/privacy-requests*`, US-910) are a queue
of their own (2026-09-30 legal audit L-6): `/admin/tasks/privacy` lists them with their 30-day
deadline and an overdue filter, the task list shows how many are open and overdue, and each
request's page carries the checklist of every record that names the subject and the form that
closes it (closing clears the contact address and the message).
Every route depends on `require_operator` (`web/admin/shell.py`) and reads/writes through
`ctx.api`; this module never touches the database or any other admin area's files.

Vocabulary tuples below mirror `services/db/models.py`'s `TASK_TYPES`/`TASK_STATUSES`/
`RECORD_PUBLISH_STATES` (read-only reference; not imported, since this module only ever talks to
the API -- same "hardcode the enum locally" call `services/api/admin_posts.py` decision 1 makes).
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from web.admin.shell import (
    AdminContext,
    problem_notice,
    render,
    require_operator,
    require_same_origin,
    templates,
)
from web.labels import REPORT_ISSUE_LABELS, humanise

router = APIRouter()

#: A task's subject, by `subject_type`: the admin record page's path segment and the public page's
#: (audit 2026-10-07 UX-3). The task screens linked `/admin/records/{id}`, which is no route: a
#: reader's report could not reach the record it was about.
_SUBJECT_PATHS: dict[str, str] = {
    "proposal": "proposals",
    "opportunity": "opportunities",
    "organization": "organizations",
}
_LOOKUP_PREFIXES = ("prop_", "opp_", "org_")


def record_admin_href(subject_type: str | None, subject_id: str | None) -> str | None:
    """The admin page of a task's subject, or `None` when the subject has none (a user, an event).
    An unknown type with a record id goes through the lookup, which redirects by prefix."""
    if not subject_id:
        return None
    segment = _SUBJECT_PATHS.get(subject_type or "")
    if segment:
        return f"/admin/records/{segment}/{subject_id}"
    if subject_id.startswith(_LOOKUP_PREFIXES):
        return "/admin/records/lookup?" + urlencode({"public_id": subject_id})
    return None


def issue_type_label(token: str | None) -> str:
    """A report's issue type in the words the reader picked it by (`REPORT_ISSUE_LABELS`)."""
    if not token:
        return ""
    return REPORT_ISSUE_LABELS.get(token) or humanise(token)


templates.env.globals["record_admin_href"] = record_admin_href
templates.env.filters["issue_type_label"] = issue_type_label


def _subject_summary(ctx: AdminContext, task: dict[str, Any] | None) -> dict[str, Any] | None:
    """Name, admin page, public page and (for a proposal) the history and unmerge anchors of the
    task's subject, so a "wrong merge" report is one click from the control that undoes it. The
    name and slug come from the admin record read; when that fails the links still stand."""
    if not task:
        return None
    subject_type, subject_id = task.get("subject_type"), task.get("subject_id")
    admin_href = record_admin_href(subject_type, subject_id)
    if admin_href is None:
        return None
    segment = _SUBJECT_PATHS.get(subject_type or "")
    record: dict[str, Any] = {}
    if segment:
        result = ctx.api.get(f"/admin/v1/{segment}/{subject_id}")
        if result.status_code == 200:
            record = result.body.get("data") or {}
    slug = record.get("slug")
    return {
        "name": record.get("name_canonical") or record.get("title") or record.get("name"),
        "admin_href": admin_href,
        "public_href": f"/{segment}/{slug}" if segment and slug else None,
        "history_href": f"{admin_href}#history" if segment in ("proposals", "opportunities") else None,
        "unmerge_href": f"{admin_href}#unmerge" if segment == "proposals" else None,
    }


TASK_TYPES: tuple[str, ...] = (
    "report",
    "intake_proposal",
    "intake_opportunity",
    "deletion_request",
    "resolution_dispute",
)
TASK_STATUSES: tuple[str, ...] = ("open", "in_progress", "done", "rejected")
RECORD_PUBLISH_STATES: tuple[str, ...] = (
    "pending_review",
    "ingest_only",
    "api_only",
    "public",
    "unpublished",
)
PRIVACY_REQUEST_STATUSES: tuple[str, ...] = ("open", "in_progress", "done", "rejected")
#: The admin list's page size is the API's; the summary counts up to this many overdue requests and
#: says "or more" beyond it rather than walking every page on each task-list view.
PRIVACY_SUMMARY_LIMIT = 100
INTAKE_TASK_TYPES = ("intake_proposal", "intake_opportunity")
OPEN_TASK_STATUSES = ("open", "in_progress")

#: docs/31 §1.2 five status families; D-25 (label + family, never colour alone).
TASK_STATUS_FAMILY: dict[str, str] = {
    "open": "neutral",
    "in_progress": "progress",
    "done": "success",
    "rejected": "danger",
}

_LIST_FILTER_KEYS = ("type", "status", "assignee_user_id")


def _active_filters(request: Request) -> dict[str, str]:
    return {k: v for k in _LIST_FILTER_KEYS if (v := request.query_params.get(k))}


def _redirect_with_flash(path: str, flash: str, **extra: str) -> RedirectResponse:
    query = urlencode({"flash": flash, **{k: v for k, v in extra.items() if v}})
    return RedirectResponse(url=f"{path}?{query}", status_code=303)


def _privacy_summary(ctx: AdminContext) -> dict[str, Any] | None:
    """Open and overdue privacy-request counts for the task list's banner; `None` when the API
    refuses (the banner is then left out rather than showing a wrong zero)."""
    counts: dict[str, Any] = {}
    for key, params in (
        ("open", {"status": "open,in_progress", "limit": str(PRIVACY_SUMMARY_LIMIT)}),
        ("overdue", {"overdue": "true", "limit": str(PRIVACY_SUMMARY_LIMIT)}),
    ):
        result = ctx.api.get("/admin/v1/privacy-requests", params=params)
        if result.status_code != 200:
            return None
        rows = result.body.get("data") or []
        more = bool((result.body.get("page") or {}).get("has_more"))
        counts[key] = f"{len(rows)}+" if more else len(rows)
    return counts


# =============================================================================================== list
@router.get("/admin/tasks")
def tasks_list(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    filters = _active_filters(request)
    privacy_summary = _privacy_summary(ctx)
    params: dict[str, Any] = dict(filters)
    cursor = request.query_params.get("cursor")
    if cursor:
        params["cursor"] = cursor

    filters_qs = f"{urlencode(filters)}&" if filters else ""

    result = ctx.api.get("/admin/v1/tasks", params=params)
    if result.status_code != 200:
        return render(
            request,
            "admin/tasks/list.html",
            {
                "notice": problem_notice(result),
                "tasks": [],
                "filters": filters,
                "filters_qs": filters_qs,
                "page": {},
                "task_types": TASK_TYPES,
                "task_statuses": TASK_STATUSES,
                "status_family": TASK_STATUS_FAMILY,
                "privacy_summary": privacy_summary,
            },
            ctx=ctx,
            nav_key="tasks",
            status_code=result.status_code,
        )
    body = result.body
    return render(
        request,
        "admin/tasks/list.html",
        {
            "tasks": body.get("data", []),
            "filters": filters,
            "filters_qs": filters_qs,
            "page": body.get("page") or {},
            "task_types": TASK_TYPES,
            "task_statuses": TASK_STATUSES,
            "status_family": TASK_STATUS_FAMILY,
            "privacy_summary": privacy_summary,
        },
        ctx=ctx,
        nav_key="tasks",
    )


# ==================================================================================== privacy requests
# Registered before `/admin/tasks/{task_id}` so "privacy" is never read as a task id.
_PRIVACY_FILTER_KEYS = ("status", "kind", "overdue")


@router.get("/admin/tasks/privacy")
def privacy_requests_list(
    request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    filters = {k: v for k in _PRIVACY_FILTER_KEYS if (v := request.query_params.get(k))}
    params: dict[str, Any] = dict(filters)
    if cursor := request.query_params.get("cursor"):
        params["cursor"] = cursor
    result = ctx.api.get("/admin/v1/privacy-requests", params=params)
    context: dict[str, Any] = {
        "requests": result.body.get("data", []) if result.status_code == 200 else [],
        "page": (result.body.get("page") or {}) if result.status_code == 200 else {},
        "filters": filters,
        "filters_qs": f"{urlencode(filters)}&" if filters else "",
        "statuses": PRIVACY_REQUEST_STATUSES,
        "status_family": TASK_STATUS_FAMILY,
    }
    if result.status_code != 200:
        context["notice"] = problem_notice(result)
    return render(
        request,
        "admin/tasks/privacy_list.html",
        context,
        ctx=ctx,
        nav_key="tasks",
        status_code=200 if result.status_code == 200 else result.status_code,
    )


def _privacy_detail_context(ctx: AdminContext, request_id: str) -> tuple[dict[str, Any], int]:
    result = ctx.api.get(f"/admin/v1/privacy-requests/{request_id}/checklist")
    if result.status_code != 200:
        return {
            "privacy_request": None,
            "notice": problem_notice(result),
            "request_id": request_id,
        }, result.status_code
    data = result.body.get("data") or {}
    return (
        {
            "privacy_request": data.get("request"),
            "items": data.get("items") or [],
            "remaining": data.get("remaining", 0),
            "notes": data.get("notes") or [],
            "request_id": request_id,
            "statuses": PRIVACY_REQUEST_STATUSES,
            "status_family": TASK_STATUS_FAMILY,
        },
        200,
    )


@router.get("/admin/tasks/privacy/{request_id}")
def privacy_request_detail(
    request_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    context, status_code = _privacy_detail_context(ctx, request_id)
    if flash := request.query_params.get("flash"):
        context["flash"] = flash
    return render(
        request, "admin/tasks/privacy_detail.html", context, ctx=ctx, nav_key="tasks", status_code=status_code
    )


@router.post("/admin/tasks/privacy/{request_id}/update")
def privacy_request_update(
    request_id: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    status: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    origin_error = require_same_origin(request)
    if origin_error is not None:
        return origin_error
    result = ctx.api.patch(
        f"/admin/v1/privacy-requests/{request_id}", json={"status": status, "reason": reason}
    )
    if result.status_code == 200:
        return _redirect_with_flash(f"/admin/tasks/privacy/{request_id}", "Privacy request updated.")
    context, _ = _privacy_detail_context(ctx, request_id)
    context["notice"] = problem_notice(result)
    return render(
        request,
        "admin/tasks/privacy_detail.html",
        context,
        ctx=ctx,
        nav_key="tasks",
        status_code=result.status_code,
    )


# ============================================================================================= detail
def _detail_context(
    request: Request, ctx: AdminContext, task_id: str, *, form_values: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = ctx.api.get(f"/admin/v1/tasks/{task_id}")
    task = result.body.get("data") if result.status_code == 200 else None
    context: dict[str, Any] = {
        "task": task,
        "task_id": task_id,
        "subject": _subject_summary(ctx, task),
        "task_statuses": TASK_STATUSES,
        "publish_states": RECORD_PUBLISH_STATES,
        "status_family": TASK_STATUS_FAMILY,
        "form_values": form_values
        or {
            "status": task.get("status") if task else "",
            "assignee_user_id": task.get("assignee_user_id") or "" if task else "",
            "notes": task.get("notes") or "" if task else "",
        },
        "intake_form_values": {
            "decision": "approve",
            "link_to_public_id": "",
            "publish_state": "",
            "create_lead": True,
            "add_curated_issuer": True,
            "reason": "",
        },
    }
    if result.status_code != 200:
        context["notice"] = problem_notice(result)
    return context


@router.get("/admin/tasks/{task_id}")
def task_detail(
    task_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    context = _detail_context(request, ctx, task_id)
    flash = request.query_params.get("flash")
    if flash:
        context["flash"] = flash
    record_id = request.query_params.get("record_id")
    record_url = request.query_params.get("record_url")
    if record_id:
        context["record_id"] = record_id
        context["record_url"] = record_url
        context["record_action"] = request.query_params.get("record_action") or "updated"
    task = context["task"]
    status_code = 200 if task is not None else (context.get("notice", {}).get("status") or 404)
    return render(
        request, "admin/tasks/detail.html", context, ctx=ctx, nav_key="tasks", status_code=status_code
    )


@router.post("/admin/tasks/{task_id}/update")
def task_update(
    task_id: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    status: Annotated[str, Form()] = "",
    assignee_user_id: Annotated[str, Form()] = "",
    clear_assignee: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    origin_error = require_same_origin(request)
    if origin_error is not None:
        return origin_error

    body: dict[str, Any] = {"reason": reason}
    if status:
        body["status"] = status
    if clear_assignee:
        body["assignee_user_id"] = None
    elif assignee_user_id.strip():
        body["assignee_user_id"] = assignee_user_id.strip()
    if notes.strip():
        body["notes"] = notes

    result = ctx.api.patch(f"/admin/v1/tasks/{task_id}", json=body)
    if result.status_code == 200:
        return _redirect_with_flash(f"/admin/tasks/{task_id}", "Task updated.")

    form_values = {
        "status": status,
        "assignee_user_id": assignee_user_id,
        "notes": notes,
    }
    context = _detail_context(request, ctx, task_id, form_values=form_values)
    context["notice"] = problem_notice(result)
    return render(
        request, "admin/tasks/detail.html", context, ctx=ctx, nav_key="tasks", status_code=result.status_code
    )


@router.post("/admin/tasks/{task_id}/approve-intake")
def task_approve_intake(
    task_id: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    decision: Annotated[str, Form()] = "",
    link_to_public_id: Annotated[str, Form()] = "",
    publish_state: Annotated[str, Form()] = "",
    create_lead: Annotated[str, Form()] = "",
    add_curated_issuer: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    origin_error = require_same_origin(request)
    if origin_error is not None:
        return origin_error

    body: dict[str, Any] = {
        "decision": decision,
        "reason": reason,
        "create_lead": bool(create_lead),
        "add_curated_issuer": bool(add_curated_issuer),
    }
    if decision == "link" and link_to_public_id.strip():
        body["link_to_public_id"] = link_to_public_id.strip()
    if decision == "approve" and publish_state:
        body["publish_state"] = publish_state

    result = ctx.api.post(f"/admin/v1/tasks/{task_id}/approve-intake", json=body)
    if result.status_code == 200:
        record = result.body.get("data", {}).get("record") or {}
        record_id = record.get("public_id", "")
        record_url = record.get("url", "")
        record_action = {"approve": "created", "link": "linked"}.get(decision, "")
        flash = f"Decision recorded: {decision}."
        return _redirect_with_flash(
            f"/admin/tasks/{task_id}",
            flash,
            record_id=record_id,
            record_url=record_url,
            record_action=record_action,
        )

    form_values = {
        "status": "",
        "assignee_user_id": "",
        "notes": "",
    }
    context = _detail_context(request, ctx, task_id, form_values=form_values)
    context["notice"] = problem_notice(result)
    context["intake_form_values"] = {
        "decision": decision,
        "link_to_public_id": link_to_public_id,
        "publish_state": publish_state,
        "create_lead": bool(create_lead),
        "add_curated_issuer": bool(add_curated_issuer),
        "reason": reason,
    }
    return render(
        request, "admin/tasks/detail.html", context, ctx=ctx, nav_key="tasks", status_code=result.status_code
    )


__all__ = ["router"]
