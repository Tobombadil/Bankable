"""Admin panel: Users and Customers (with subscriptions) screens (Sprint 3 item 3, this agent's
slice: docs/30-design-ia.md §1.3 nav order, US-901/902/903). `router = APIRouter()`; the
coordinator mounts it onto `web/app.py` next to `web/admin/shell.py`'s own `router`. Every route
depends on `require_operator` (`web/admin/shell.py`) and renders through its `render()`/
`problem_notice()` helpers; nothing here touches the database or trusts anything the live
`/admin/v1` API on `ctx.api` did not itself answer (shell.py's own rule, "docs/20 §8: all reading
the same API").

**Vocabulary shown in `<select>` options is a display-only mirror of `services/db/models.py`'s
CHECK-constraint tuples** (`USER_ROLES`, the `admin_update_user` status pair, `ACCOUNT_KINDS`,
`ACCOUNT_ENTITLEMENT_SOURCES`) and `services.sor.ports.PLAN_TIERS` — this module never imports
`services.db`/`services.sor` (that would cross the web/API boundary shell.py's docstring draws);
the API is still the one enforcing these values on every write, so a stale local tuple only ever
produces a form option the API then rejects with its own validation notice, never a silent wrong
write.

Decisions (fuller reasoning inline at first use):

1. **The keys issue form lives in `web/admin/ops.py`** (task brief's file split); this module
   only links to `/admin/keys?account_id=...` from a customer's keys table rather than duplicating
   the issue form here.
2. **The `POST /admin/v1/users/{id}` role/status writes are two separate forms** posting to two
   separate routes (`/admin/users/{id}/role`, `/admin/users/{id}/status`) even though both reach
   the same `PATCH /admin/v1/users/{id}` — `admin_update_user` accepts either field alone, and two
   small single-purpose forms keep each one's own `reason` unambiguous (docs/31 SC 3.3.1: the error
   belongs next to the field that caused it).
3. **Customer detail always fetches with `detail=true`** (the API's `GET /admin/v1/customers/{id}`
   always returns `users`/`keys`; only the *list* endpoint omits them) — no separate "expand" step.
4. **A stale `sor` block shows the exact US-902 AC3 wording** ("writes are refused while the
   adapter is unavailable") next to the create-subscription form, not just a generic banner, since
   that form is the one write this screen offers and the one the staleness actually blocks.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from web.admin.shell import AdminContext, problem_notice, render, require_operator, require_same_origin

router = APIRouter()

# Mirrors services/db/models.py USER_ROLES; the `owner` option is only ever rendered for an owner
# session (module docstring point 2; PRD US-901 "change role").
_USER_ROLES: tuple[str, ...] = ("viewer", "member", "operator", "legal", "owner")
#: `admin_update_user` (services/api/admin_people.py) only accepts these two on `status` -- the
#: third vocabulary member, `anonymised`, is a terminal state only the deletion flow reaches.
_USER_STATUSES: tuple[str, ...] = ("active", "disabled")
_ACCOUNT_KINDS: tuple[str, ...] = ("personal", "organization")
_ENTITLEMENT_SOURCES: tuple[str, ...] = ("sor", "manual_grant", "trial")
#: services.sor.ports.PLAN_TIERS -- the vocabulary `admin_create_subscription` validates
#: `plan_code` against (services/api/admin_people.py).
_PLAN_CODES: tuple[str, ...] = ("pro", "team", "api", "enterprise")

_USER_LIST_FILTER_KEYS = ("q", "role", "status", "account_id")
_CUSTOMER_LIST_FILTER_KEYS = ("q", "entitlement", "status")


def _clean(**kwargs: str | None) -> dict[str, str]:
    """Drop `None`/blank values so an unset filter never becomes a literal empty-string query
    param (which `csv_param`/`ilike` on the API side would treat as "match nothing", not "no
    filter")."""
    return {k: v for k, v in kwargs.items() if v}


def _filters_from(request: Request, keys: tuple[str, ...]) -> dict[str, str]:
    return {k: v for k in keys if (v := request.query_params.get(k))}


def _describe_filters(filters: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in filters.items())


def _next_href(request: Request, next_cursor: str | None) -> str | None:
    if not next_cursor:
        return None
    params = dict(request.query_params)
    params["cursor"] = next_cursor
    return f"{request.url.path}?{urlencode(params)}"


def _flash(request: Request) -> str | None:
    return request.query_params.get("flash")


def _redirect_with_flash(path: str, message: str) -> RedirectResponse:
    return RedirectResponse(url=f"{path}?{urlencode({'flash': message})}", status_code=303)


# ================================================================================================ users
@router.get("/admin/users")
def list_users(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    filters = _filters_from(request, _USER_LIST_FILTER_KEYS)
    params = {
        **_clean(
            q=filters.get("q"),
            role=filters.get("role"),
            status=filters.get("status"),
            account_id=filters.get("account_id"),
        ),
        **_clean(cursor=request.query_params.get("cursor")),
    }
    result = ctx.api.get("/admin/v1/users", params=params)
    if result.status_code != 200:
        return render(
            request,
            "admin/users/list.html",
            {"notice": problem_notice(result), "users": [], "filters": filters, "next_href": None},
            ctx=ctx,
            nav_key="users",
            status_code=result.status_code,
        )
    users = result.body.get("data", [])
    page = result.body.get("page", {})
    return render(
        request,
        "admin/users/list.html",
        {
            "flash": _flash(request),
            "users": users,
            "filters": filters,
            "filters_description": _describe_filters(filters),
            "next_href": _next_href(request, page.get("next_cursor")),
            "roles": _USER_ROLES,
            "statuses": _USER_STATUSES,
        },
        ctx=ctx,
        nav_key="users",
    )


@router.get("/admin/users/{user_id}")
def get_user(
    user_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    result = ctx.api.get(f"/admin/v1/users/{user_id}")
    if result.status_code != 200:
        return render(
            request,
            "admin/users/detail.html",
            {"notice": problem_notice(result), "user": None},
            ctx=ctx,
            nav_key="users",
            status_code=result.status_code,
        )
    return render(
        request,
        "admin/users/detail.html",
        {
            "flash": _flash(request),
            "user": result.body.get("data", {}),
            "roles": _USER_ROLES,
            "statuses": _USER_STATUSES,
        },
        ctx=ctx,
        nav_key="users",
    )


def _rerender_user_detail(
    request: Request, ctx: AdminContext, user_id: str, *, notice: dict[str, Any], status_code: int
) -> Response:
    """A role/status write failed: re-fetch the user so the page still has real data to show
    alongside the API's own error notice (docs/30-design-ia.md §6: never a stack trace, and never a
    stale/blank detail page either)."""
    current = ctx.api.get(f"/admin/v1/users/{user_id}")
    return render(
        request,
        "admin/users/detail.html",
        {
            "notice": notice,
            "user": current.body.get("data") if current.status_code == 200 else None,
            "roles": _USER_ROLES,
            "statuses": _USER_STATUSES,
        },
        ctx=ctx,
        nav_key="users",
        status_code=status_code,
    )


@router.post("/admin/users/{user_id}/role")
def update_user_role(
    user_id: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    role: Annotated[str, Form()],
    reason: Annotated[str, Form()],
) -> Response:
    rejection = require_same_origin(request)
    if rejection is not None:
        return rejection
    result = ctx.api.patch(f"/admin/v1/users/{user_id}", json={"role": role, "reason": reason})
    if result.status_code == 200:
        return _redirect_with_flash(f"/admin/users/{user_id}", "Role updated.")
    return _rerender_user_detail(
        request, ctx, user_id, notice=problem_notice(result), status_code=result.status_code
    )


@router.post("/admin/users/{user_id}/status")
def update_user_status(
    user_id: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    status: Annotated[str, Form()],
    reason: Annotated[str, Form()],
) -> Response:
    rejection = require_same_origin(request)
    if rejection is not None:
        return rejection
    result = ctx.api.patch(f"/admin/v1/users/{user_id}", json={"status": status, "reason": reason})
    if result.status_code == 200:
        return _redirect_with_flash(f"/admin/users/{user_id}", "Status updated.")
    return _rerender_user_detail(
        request, ctx, user_id, notice=problem_notice(result), status_code=result.status_code
    )


@router.get("/admin/users/{user_id}/delete")
def delete_user_confirm(
    user_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    result = ctx.api.get(f"/admin/v1/users/{user_id}")
    if result.status_code != 200:
        return render(
            request,
            "admin/users/delete_confirm.html",
            {"notice": problem_notice(result), "user": None},
            ctx=ctx,
            nav_key="users",
            status_code=result.status_code,
        )
    return render(
        request,
        "admin/users/delete_confirm.html",
        {"user": result.body.get("data", {})},
        ctx=ctx,
        nav_key="users",
    )


@router.post("/admin/users/{user_id}/delete")
def delete_user(
    user_id: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    reason: Annotated[str, Form()],
) -> Response:
    rejection = require_same_origin(request)
    if rejection is not None:
        return rejection
    result = ctx.api.delete(f"/admin/v1/users/{user_id}", json={"reason": reason})
    if result.status_code == 202:
        task_id = result.body.get("data", {}).get("task_id")
        if task_id:
            return RedirectResponse(url=f"/admin/tasks/{task_id}", status_code=303)
        return _redirect_with_flash("/admin/users", "Deletion started.")
    current = ctx.api.get(f"/admin/v1/users/{user_id}")
    return render(
        request,
        "admin/users/delete_confirm.html",
        {
            "notice": problem_notice(result),
            "user": current.body.get("data") if current.status_code == 200 else None,
        },
        ctx=ctx,
        nav_key="users",
        status_code=result.status_code,
    )


# ============================================================================================ customers
@router.get("/admin/customers")
def list_customers(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    filters = _filters_from(request, _CUSTOMER_LIST_FILTER_KEYS)
    params = {
        **_clean(q=filters.get("q"), entitlement=filters.get("entitlement"), status=filters.get("status")),
        **_clean(cursor=request.query_params.get("cursor")),
    }
    result = ctx.api.get("/admin/v1/customers", params=params)
    if result.status_code != 200:
        return render(
            request,
            "admin/customers/list.html",
            {"notice": problem_notice(result), "customers": [], "filters": filters, "next_href": None},
            ctx=ctx,
            nav_key="customers",
            status_code=result.status_code,
        )
    customers = result.body.get("data", [])
    page = result.body.get("page", {})
    return render(
        request,
        "admin/customers/list.html",
        {
            "flash": _flash(request),
            "customers": customers,
            "filters": filters,
            "filters_description": _describe_filters(filters),
            "next_href": _next_href(request, page.get("next_cursor")),
        },
        ctx=ctx,
        nav_key="customers",
    )


@router.get("/admin/customers/new")
def new_customer_form(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    return render(
        request,
        "admin/customers/new.html",
        {"kinds": _ACCOUNT_KINDS, "entitlement_sources": _ENTITLEMENT_SOURCES, "values": {}},
        ctx=ctx,
        nav_key="customers",
    )


@router.post("/admin/customers/new")
def create_customer(
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    name: Annotated[str, Form()],
    kind: Annotated[str, Form()],
    organization_id: Annotated[str, Form()] = "",
    primary_contact_email: Annotated[str, Form()] = "",
    entitlement_source: Annotated[str, Form()] = "sor",
    reason: Annotated[str, Form()] = "",
) -> Response:
    rejection = require_same_origin(request)
    if rejection is not None:
        return rejection
    body = {
        "name": name,
        "kind": kind,
        "reason": reason,
        **_clean(
            organization_id=organization_id or None,
            primary_contact_email=primary_contact_email or None,
            entitlement_source=entitlement_source or None,
        ),
    }
    result = ctx.api.post("/admin/v1/customers", json=body)
    if result.status_code == 201:
        account_id = result.body.get("data", {}).get("account", {}).get("account_id")
        target = f"/admin/customers/{account_id}" if account_id else "/admin/customers"
        return _redirect_with_flash(target, "Customer created.")
    return render(
        request,
        "admin/customers/new.html",
        {
            "notice": problem_notice(result),
            "kinds": _ACCOUNT_KINDS,
            "entitlement_sources": _ENTITLEMENT_SOURCES,
            "values": {
                "name": name,
                "kind": kind,
                "organization_id": organization_id,
                "primary_contact_email": primary_contact_email,
                "entitlement_source": entitlement_source,
                "reason": reason,
            },
        },
        ctx=ctx,
        nav_key="customers",
        status_code=result.status_code,
    )


@router.get("/admin/customers/{account_id}")
def get_customer(
    account_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    result = ctx.api.get(f"/admin/v1/customers/{account_id}")
    if result.status_code != 200:
        return render(
            request,
            "admin/customers/detail.html",
            {"notice": problem_notice(result), "customer": None},
            ctx=ctx,
            nav_key="customers",
            status_code=result.status_code,
        )
    return render(
        request,
        "admin/customers/detail.html",
        {
            "flash": _flash(request),
            "customer": result.body.get("data", {}),
            "plan_codes": _PLAN_CODES,
        },
        ctx=ctx,
        nav_key="customers",
    )


@router.post("/admin/customers/{account_id}/subscriptions")
def create_subscription(
    account_id: str,
    request: Request,
    ctx: Annotated[AdminContext, Depends(require_operator)],
    plan_code: Annotated[str, Form()],
    seats: Annotated[str, Form()],
    trial_days: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
) -> Response:
    rejection = require_same_origin(request)
    if rejection is not None:
        return rejection
    body: dict[str, Any] = {"account_id": account_id, "plan_code": plan_code, "reason": reason}
    try:
        body["seats"] = int(seats)
    except ValueError:
        body["seats"] = seats  # let the API's own validation_error name the bad value
    if trial_days.strip():
        try:
            body["trial_days"] = int(trial_days)
        except ValueError:
            body["trial_days"] = trial_days
    result = ctx.api.post("/admin/v1/subscriptions", json=body)
    if result.status_code == 201:
        return _redirect_with_flash(f"/admin/customers/{account_id}", "Subscription created.")
    current = ctx.api.get(f"/admin/v1/customers/{account_id}")
    return render(
        request,
        "admin/customers/detail.html",
        {
            "notice": problem_notice(result),
            "customer": current.body.get("data") if current.status_code == 200 else None,
            "plan_codes": _PLAN_CODES,
        },
        ctx=ctx,
        nav_key="customers",
        status_code=result.status_code,
    )


__all__ = ["router"]
