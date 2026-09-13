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

Both transports expose the same `.get(path, params) -> httpx.Response`-shaped interface, so
`web/app.py` and `web/viewmodels.py` never need to know which one is active.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Protocol

import httpx


class Transport(Protocol):
    def get(self, url: str, *, params: Mapping[str, Any] | None = None) -> httpx.Response: ...
    def close(self) -> None: ...


class ApiError(RuntimeError):
    """A non-2xx, non-404 response from the API (docs/23 §8 RFC 9457 problem body)."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"API {status_code}: {body.get('title', 'error')}")


class ApiNotFound(ApiError):
    """404 -- a gated or absent record (docs/21 §8 item 3: the two are indistinguishable)."""


class ApiClient:
    """Thin wrapper: JSON in, envelope dict out, `ApiNotFound`/`ApiError` on failure."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        response = self._transport.get(path, params=clean)
        if response.status_code == 404:
            raise ApiNotFound(404, _safe_json(response))
        if response.status_code >= 400:
            raise ApiError(response.status_code, _safe_json(response))
        data: dict[str, Any] = response.json()
        return data

    def close(self) -> None:
        self._transport.close()


def _safe_json(response: httpx.Response) -> dict[str, Any]:
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
