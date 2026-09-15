"""The admin shell: operator guard, cookie-forwarding API helper, chrome and error rendering.

Every `web/admin/<area>.py` router depends on `require_operator` and calls the API through the
`AdminApi` it returns, so no page ever talks to the database or trusts anything but the API's
own answer (docs/20 §8: "all reading the same API"). Roles: `operator` and `owner` reach every
screen; `legal` reaches the shell too because the licence-gate clearance lives in the sources
screens (US-905 AC1) — the API still refuses a legal-only session anything else. The `admin.`
host and its second factor are the reverse proxy's job (docs/20 §7, ADR 0005), not this module's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from web.api_client import ApiClient, ApiResult
from web.auth import _csrf_rejection, _is_same_origin, get_api

SESSION_COOKIE_NAME = "session"
ADMIN_ROLES = frozenset({"operator", "owner", "legal"})

_WEB_ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(_WEB_ROOT / "templates"))

#: docs/30-design-ia.md §1.3 nav order: Sources → Records → Resolution/Extraction → Tasks → Posts →
#: Users/Customers → Keys/Costs/Audit. Each entry is (href, label, key); a page passes `nav_key`.
ADMIN_NAV: tuple[tuple[str, str, str], ...] = (
    ("/admin/sources", "Sources", "sources"),
    ("/admin/records", "Records", "records"),
    ("/admin/resolution", "Resolution", "resolution"),
    ("/admin/extractions", "Extractions", "extractions"),
    ("/admin/tasks", "Tasks", "tasks"),
    ("/admin/posts", "Posts", "posts"),
    ("/admin/users", "Users", "users"),
    ("/admin/customers", "Customers", "customers"),
    ("/admin/keys", "Keys", "keys"),
    ("/admin/costs", "Costs", "costs"),
    ("/admin/engagement", "Engagement", "engagement"),
    ("/admin/audit", "Audit", "audit"),
)
templates.env.globals["ADMIN_NAV"] = ADMIN_NAV

router = APIRouter()


class AdminApi:
    """`ApiClient` bound to the operator's session cookie. Every call returns an `ApiResult`;
    pages branch on `status_code` and render the RFC 9457 `title`/`detail` rather than raising."""

    def __init__(self, client: ApiClient, cookie: str) -> None:
        self._client = client
        self._cookies = {SESSION_COOKIE_NAME: cookie}

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> ApiResult:
        return self._client.get_result(path, params=params, cookies=self._cookies)

    def post(self, path: str, *, json: Mapping[str, Any] | None = None) -> ApiResult:
        return self._client.post(path, json=json, cookies=self._cookies)

    def patch(self, path: str, *, json: Mapping[str, Any] | None = None) -> ApiResult:
        return self._client.request("PATCH", path, json=json, cookies=self._cookies)

    def put(self, path: str, *, json: Mapping[str, Any] | None = None) -> ApiResult:
        return self._client.request("PUT", path, json=json, cookies=self._cookies)

    def delete(self, path: str, *, json: Mapping[str, Any] | None = None) -> ApiResult:
        return self._client.request("DELETE", path, json=json, cookies=self._cookies)


@dataclass(frozen=True)
class AdminContext:
    api: AdminApi
    user: dict[str, Any]  #: the `user` block of `GET /v1/me`
    account: dict[str, Any]

    @property
    def role(self) -> str:
        return str(self.user.get("role", ""))

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


class NotAnOperator(HTTPException):
    """Raised by `require_operator`; the shell's handler turns it into a redirect (no session) or
    a 403 page (a session without an admin role) — never a JSON problem on an HTML surface."""

    def __init__(self, *, redirect_to: str | None) -> None:
        super().__init__(status_code=403 if redirect_to is None else 303)
        self.redirect_to = redirect_to


def require_operator(request: Request) -> AdminContext:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    next_path = request.url.path
    if not cookie:
        raise NotAnOperator(redirect_to=f"/login?next={next_path}")
    api = AdminApi(get_api(request), cookie)
    me = api.get("/v1/me")
    if me.status_code == 401:
        raise NotAnOperator(redirect_to=f"/login?next={next_path}")
    if me.status_code != 200:
        raise NotAnOperator(redirect_to=None)
    data = me.body.get("data", {})
    user = data.get("user", {})
    if user.get("role") not in ADMIN_ROLES:
        raise NotAnOperator(redirect_to=None)
    return AdminContext(api=api, user=user, account=data.get("account", {}))


async def not_an_operator_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, NotAnOperator):  # pragma: no cover - registration guarantees this
        raise exc
    if exc.redirect_to is not None:
        return RedirectResponse(url=exc.redirect_to, status_code=303)
    return templates.TemplateResponse(request, "admin/forbidden.html", {}, status_code=403)


def require_same_origin(request: Request) -> PlainTextResponse | None:
    """Every admin form POST checks this first (same stance as `web/auth.py`)."""
    return None if _is_same_origin(request) else _csrf_rejection()


def problem_notice(result: ApiResult) -> dict[str, Any]:
    """The banner context for a non-2xx `ApiResult`: RFC 9457 `title` + `detail` + `request_id`
    (docs/30-design-ia.md §6 error state: never a stack trace)."""
    body = result.body
    return {
        "status": result.status_code,
        "title": body.get("title") or f"Request failed ({result.status_code})",
        "detail": body.get("detail"),
        "request_id": body.get("request_id"),
        "errors": {e.get("field"): e.get("message") for e in body.get("errors", []) if isinstance(e, dict)},
    }


def render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    ctx: AdminContext,
    nav_key: str,
    status_code: int = 200,
) -> HTMLResponse:
    merged = {"admin": ctx, "nav_key": nav_key, **context}
    return templates.TemplateResponse(request, template, merged, status_code=status_code)


@router.get("/admin")
def admin_home(request: Request) -> RedirectResponse:
    """docs/30-design-ia.md §1.3: the admin root is source health."""
    require_operator(request)
    return RedirectResponse(url="/admin/sources", status_code=303)


__all__ = [
    "ADMIN_NAV",
    "ADMIN_ROLES",
    "AdminApi",
    "AdminContext",
    "NotAnOperator",
    "not_an_operator_handler",
    "problem_notice",
    "render",
    "require_operator",
    "require_same_origin",
    "router",
    "templates",
]
