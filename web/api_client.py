"""HTTP/in-process client for `services/api` (docs/23-api-spec-outline.md; docs/00-PLAN.md
Sprint 2 "wire site to API" item).

Two transports, selected by the `API_BASE_URL` environment variable:

  - **HTTP** (`API_BASE_URL` set): a real `httpx.Client` against a separately-running
    `uvicorn services.api.app:app` process. This is the shape a production deployment uses (the
    API is its own deployable per `docs/20-architecture.md`) and is what `web/dev_up.py` starts.
  - **In-process** (`API_BASE_URL` unset): `services.api.app.app` is mounted directly via
    Starlette's `TestClient`, which drives the ASGI app synchronously with no socket at all. This
    is the default for `pytest` (docs/00-PLAN task: "tests run against the in-process API") and
    also works fine as the default for local `uvicorn web.app:app --reload` against a file-backed
    SQLite `DATABASE_URL`, since both the API app and this client then read the same database.

Both transports expose the same `.get(path, params, cookies) -> httpx.Response`-shaped interface
(and, since Sprint 3's "login and registration surface" task, `.post`), so `web/app.py` and
`web/viewmodels.py` never need to know which one is active. `cookies` is always passed *per call*
(never written onto the shared `Transport`'s own cookie jar): this process serves every visitor
through one `ApiClient`, so mutating the client's persistent jar with one visitor's session cookie
would leak it to the next request from anyone else. httpx honours a per-call `cookies=` mapping for
exactly one request without merging it back into `Client.cookies` -- verified in
`web/test_auth.py`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class Transport(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response: ...
    def post(
        self,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response: ...
    def request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response: ...
    def close(self) -> None: ...


class ApiError(RuntimeError):
    """A non-2xx, non-404 response from the API (docs/23 §8 RFC 9457 problem body)."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"API {status_code}: {body.get('title', 'error')}")


class ApiNotFound(ApiError):
    """404 -- a gated or absent record (docs/21 §8 item 3: the two are indistinguishable)."""


@dataclass(frozen=True)
class ApiResult:
    """The raw shape of a response, for callers that must inspect a non-2xx status themselves
    instead of getting an exception -- `web/auth.py`'s POST routes, which re-render a form with
    the API's own problem `title`/`detail`/`errors[]` rather than raising (Sprint 3 "login and
    registration surface" task). `set_cookie` carries every raw `Set-Cookie` header value so the
    caller can relay the session cookie onto its own response unchanged."""

    status_code: int
    body: dict[str, Any]
    set_cookie: list[str] = field(default_factory=list)


class ApiClient:
    """Thin wrapper: JSON in, envelope dict out, `ApiNotFound`/`ApiError` on failure for `.get`;
    `.get_result`/`.post` return the raw `ApiResult` instead of raising, for callers that need to
    branch on the status code themselves."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        response = self._transport.get(path, params=clean, cookies=cookies)
        if response.status_code == 404:
            raise ApiNotFound(404, _safe_json(response))
        if response.status_code >= 400:
            raise ApiError(response.status_code, _safe_json(response))
        data: dict[str, Any] = response.json()
        return data

    def get_result(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> ApiResult:
        """Non-raising `.get` for a caller that wants to branch on the status code itself (e.g.
        `web/auth.py`'s `/account`, which treats a 401 as "not signed in", not an error)."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        response = self._transport.get(path, params=clean, cookies=cookies)
        return _to_result(response)

    def post(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> ApiResult:
        response = self._transport.post(path, json=json, cookies=cookies)
        return _to_result(response)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> ApiResult:
        """Any method with a non-raising result — the admin pages' PATCH/PUT/DELETE writes
        (`web/admin/shell.py` `AdminApi`); `get`/`post` above stay as they are for existing callers."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        response = self._transport.request(method, path, json=json, params=clean, cookies=cookies)
        return _to_result(response)

    def close(self) -> None:
        self._transport.close()


def _to_result(response: httpx.Response) -> ApiResult:
    return ApiResult(
        status_code=response.status_code,
        body=_safe_json(response),
        set_cookie=response.headers.get_list("set-cookie"),
    )


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    if not response.content:
        # A 204 (logout) or any other empty-bodied success has nothing to parse -- treating that
        # as a `{"title": "error", ...}` fallback would misrepresent a clean success as a problem
        # body to every caller that inspects `.body.get("title")`.
        return {}
    try:
        body: dict[str, Any] = response.json()
        return body
    except ValueError:
        return {"title": "error", "detail": response.text}


def build_client(*, api_base_url: str | None = None) -> ApiClient:
    """Build a fresh `ApiClient`. `api_base_url=None` (the default) reads `API_BASE_URL` from the
    environment; pass it explicitly in tests to force a mode regardless of the environment.
    """
    base_url = api_base_url if api_base_url is not None else os.environ.get("API_BASE_URL")
    if base_url:
        return ApiClient(httpx.Client(base_url=base_url, timeout=10.0))
    # In-process: import lazily so `DATABASE_URL` can be set by the caller (a dev script, or a
    # test fixture) before `services.api.deps` resolves its engine on first use.
    from starlette.testclient import TestClient

    from services.api.app import app as api_app

    return ApiClient(TestClient(api_app, base_url="http://api-internal"))
