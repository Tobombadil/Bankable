"""Admin posts screens (Sprint 3 item 3, `docs/30-design-ia.md` §1.3/§4.6, `docs/32` §4): the
social review queue and per-channel auto-publish switches, server-rendered over `/admin/v1/posts*`
and `/admin/v1/channels/*`. Every route depends on `require_operator`; every write goes through
`ctx.api`, never the database. The channels screen reads `GET /admin/v1/channels` (a coordinator
follow-up landed after this screen's brief was written, which assumed no such read existed) so the
current auto-publish state is real, not "unknown".

Vocabulary and the channel body-length limits mirror `services/db/models.py`'s
`POST_CHANNELS`/`POST_STATES` and `services/api/admin_posts.py`'s `CHANNEL_BODY_LIMITS` (read-only
reference; declared locally rather than imported -- this module only ever talks to the API).
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from web.admin.shell import AdminContext, problem_notice, render, require_operator, require_same_origin

router = APIRouter()

POST_CHANNELS: tuple[str, ...] = ("bluesky", "linkedin", "x")
POST_STATES: tuple[str, ...] = (
    "draft",
    "approved",
    "scheduled",
    "published",
    "rejected",
    "withdrawn",
    "failed",
)
REJECT_REASONS: tuple[str, ...] = (
    "wrong_fact",
    "not_newsworthy",
    "source_doubt",
    "style",
    "duplicate",
    "other",
)
#: docs/32 §4.3 item 1 -- the same limits `services/api/admin_posts.py` enforces server-side.
CHANNEL_BODY_LIMITS: dict[str, int] = {"bluesky": 300, "x": 280, "linkedin": 3000}

#: docs/31 §1.2 five status families; D-25 (label + family, never colour alone).
POST_STATE_FAMILY: dict[str, str] = {
    "draft": "neutral",
    "approved": "progress",
    "scheduled": "progress",
    "published": "success",
    "rejected": "danger",
    "withdrawn": "neutral",
    "failed": "danger",
}

_LIST_FILTER_KEYS = ("channel", "state")


def _active_filters(request: Request) -> dict[str, str]:
    return {k: v for k in _LIST_FILTER_KEYS if (v := request.query_params.get(k))}


def _redirect_with_flash(path: str, flash: str, **extra: str) -> RedirectResponse:
    query = urlencode({"flash": flash, **{k: v for k, v in extra.items() if v}})
    return RedirectResponse(url=f"{path}?{query}", status_code=303)


def _age(value: str | None) -> str | None:
    """A short "N ago" string for `gate_checked_at` (screen brief: "gate_checked_at age") --
    computed once at render time, no client-side clock (CLAUDE.md/docs/31: no JS-only interaction)."""
    if not value:
        return None
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    delta = dt.datetime.now(dt.UTC) - when
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _datetime_local_to_rfc3339(value: str) -> str | None:
    """`<input type=datetime-local>` has no timezone; treated as UTC (screen brief: "converted to
    RFC 3339 UTC"). Returns `None` on anything that does not parse as a bare local datetime,
    leaving the raw value's validation to the API (it renders as a 400 either way)."""
    if not value:
        return None
    try:
        when = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return when.isoformat().replace("+00:00", "Z")


# =============================================================================================== list
@router.get("/admin/posts")
def posts_list(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    filters = _active_filters(request)
    params: dict[str, Any] = dict(filters)
    cursor = request.query_params.get("cursor")
    if cursor:
        params["cursor"] = cursor
    filters_qs = f"{urlencode(filters)}&" if filters else ""

    result = ctx.api.get("/admin/v1/posts", params=params)
    context: dict[str, Any] = {
        "filters": filters,
        "filters_qs": filters_qs,
        "channels": POST_CHANNELS,
        "states": POST_STATES,
        "state_family": POST_STATE_FAMILY,
        "channel_limits": CHANNEL_BODY_LIMITS,
    }
    if result.status_code != 200:
        context.update({"notice": problem_notice(result), "posts": [], "page": {}})
        return render(
            request,
            "admin/posts/list.html",
            context,
            ctx=ctx,
            nav_key="posts",
            status_code=result.status_code,
        )
    body = result.body
    rows = []
    for row in body.get("data", []):
        row = dict(row)
        row["gate_age"] = _age(row.get("gate_checked_at"))
        row["body_preview"] = (row.get("body") or "")[:140]
        rows.append(row)
    context.update({"posts": rows, "page": body.get("page") or {}})
    return render(request, "admin/posts/list.html", context, ctx=ctx, nav_key="posts")


# ============================================================================================ channels
# Declared before the `/admin/posts/{post_id}` routes below: FastAPI resolves paths in
# registration order, and `/admin/posts/channels` would otherwise be swallowed by the dynamic
# `{post_id}` route (post_id="channels") since both patterns match the same literal path.
@router.get("/admin/posts/channels")
def channels_list(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    flash = request.query_params.get("flash")
    result = ctx.api.get("/admin/v1/channels")
    context: dict[str, Any] = {"channels": POST_CHANNELS, "is_owner": ctx.is_owner}
    if result.status_code == 200:
        context["channel_rows"] = {row["channel"]: row for row in result.body.get("data", [])}
    else:
        context["channel_rows"] = {}
        context["notice"] = problem_notice(result)
    if flash:
        context["flash"] = flash
    return render(
        request,
        "admin/posts/channels.html",
        context,
        ctx=ctx,
        nav_key="posts",
        status_code=result.status_code if result.status_code != 200 else 200,
    )


@router.post("/admin/posts/channels/{channel}/auto-publish")
def channel_set_auto_publish(
    channel: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    auto_publish: Annotated[str, Form()] = "",
    disclosure_label_confirmed: Annotated[str, Form()] = "",
    daily_cap: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    origin_error = require_same_origin(request)
    if origin_error is not None:
        return origin_error

    body: dict[str, Any] = {
        "auto_publish": bool(auto_publish),
        "disclosure_label_confirmed": bool(disclosure_label_confirmed),
        "reason": reason,
    }
    if daily_cap.strip():
        try:
            body["daily_cap"] = int(daily_cap.strip())
        except ValueError:
            body["daily_cap"] = daily_cap.strip()  # left as-is; the API renders the 400

    result = ctx.api.put(f"/admin/v1/channels/{channel}/auto-publish", json=body)
    if result.status_code == 200:
        return _redirect_with_flash("/admin/posts/channels", f"{channel} updated.")

    refreshed = ctx.api.get("/admin/v1/channels")
    channel_rows: dict[str, Any] = {}
    if refreshed.status_code == 200:
        channel_rows = {row["channel"]: row for row in refreshed.body.get("data", [])}
    context: dict[str, Any] = {
        "channels": POST_CHANNELS,
        "is_owner": ctx.is_owner,
        "channel_rows": channel_rows,
        "notice": problem_notice(result),
        "notice_channel": channel,
    }
    return render(
        request,
        "admin/posts/channels.html",
        context,
        ctx=ctx,
        nav_key="posts",
        status_code=result.status_code,
    )


# ============================================================================================= detail
def _detail_context(
    ctx: AdminContext, post_id: str, *, form_values: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = ctx.api.get(f"/admin/v1/posts/{post_id}")
    post = result.body.get("data") if result.status_code == 200 else None
    limit = CHANNEL_BODY_LIMITS.get(post["channel"]) if post else None
    context: dict[str, Any] = {
        "post": post,
        "post_id": post_id,
        "state_family": POST_STATE_FAMILY,
        "reject_reasons": REJECT_REASONS,
        "channel_limit": limit,
        "body_length": len(post["body"]) if post else 0,
        "form_values": form_values or {"body": post.get("body", "") if post else ""},
    }
    if result.status_code != 200:
        context["notice"] = problem_notice(result)
    return context


@router.get("/admin/posts/{post_id}")
def post_detail(
    post_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    context = _detail_context(ctx, post_id)
    flash = request.query_params.get("flash")
    if flash:
        context["flash"] = flash
    post = context["post"]
    status_code = 200 if post is not None else (context.get("notice", {}).get("status") or 404)
    return render(
        request, "admin/posts/detail.html", context, ctx=ctx, nav_key="posts", status_code=status_code
    )


@router.post("/admin/posts/{post_id}/edit")
def post_edit(
    post_id: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    body: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    origin_error = require_same_origin(request)
    if origin_error is not None:
        return origin_error

    result = ctx.api.patch(f"/admin/v1/posts/{post_id}", json={"body": body, "reason": reason})
    if result.status_code == 200:
        return _redirect_with_flash(f"/admin/posts/{post_id}", "Post updated.")

    context = _detail_context(ctx, post_id, form_values={"body": body})
    context["notice"] = problem_notice(result)
    return render(
        request, "admin/posts/detail.html", context, ctx=ctx, nav_key="posts", status_code=result.status_code
    )


@router.post("/admin/posts/{post_id}/approve")
def post_approve(
    post_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    origin_error = require_same_origin(request)
    if origin_error is not None:
        return origin_error

    result = ctx.api.post(f"/admin/v1/posts/{post_id}/approve")
    if result.status_code == 200:
        return _redirect_with_flash(f"/admin/posts/{post_id}", "Post approved.")

    context = _detail_context(ctx, post_id)
    context["notice"] = problem_notice(result)
    return render(
        request, "admin/posts/detail.html", context, ctx=ctx, nav_key="posts", status_code=result.status_code
    )


@router.post("/admin/posts/{post_id}/reject")
def post_reject(
    post_id: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    reason: Annotated[str, Form()] = "",
) -> Response:
    origin_error = require_same_origin(request)
    if origin_error is not None:
        return origin_error

    result = ctx.api.post(f"/admin/v1/posts/{post_id}/reject", json={"reason": reason})
    if result.status_code == 200:
        return _redirect_with_flash(f"/admin/posts/{post_id}", "Post rejected.")

    context = _detail_context(ctx, post_id)
    context["notice"] = problem_notice(result)
    context["reject_reason_value"] = reason
    return render(
        request, "admin/posts/detail.html", context, ctx=ctx, nav_key="posts", status_code=result.status_code
    )


@router.post("/admin/posts/{post_id}/schedule")
def post_schedule(
    post_id: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    scheduled_for: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    origin_error = require_same_origin(request)
    if origin_error is not None:
        return origin_error

    when = _datetime_local_to_rfc3339(scheduled_for) or scheduled_for
    result = ctx.api.post(
        f"/admin/v1/posts/{post_id}/schedule", json={"scheduled_for": when, "reason": reason}
    )
    if result.status_code == 200:
        return _redirect_with_flash(f"/admin/posts/{post_id}", "Post scheduled.")

    context = _detail_context(ctx, post_id)
    context["notice"] = problem_notice(result)
    context["schedule_value"] = scheduled_for
    return render(
        request, "admin/posts/detail.html", context, ctx=ctx, nav_key="posts", status_code=result.status_code
    )


__all__ = ["router"]
