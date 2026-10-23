"""Shared helpers for the price connector tests: recorded fixtures (`fixtures/README.md`) and a
fake HTTP session that answers URLs from them, so `fetch` never touches the network in tests."""

from __future__ import annotations

import datetime as dt
import pathlib
from collections.abc import Callable, Mapping
from typing import Any

import pytest
import requests

from pipeline.connectors.base import RawSnapshot
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.registry import Registry

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
RECORDED_AT = dt.datetime(2026, 10, 22, 22, 0, tzinfo=dt.UTC)


def fixture_bytes(name: str) -> bytes:
    path = FIXTURES / name
    if not path.exists():
        raise FileNotFoundError(f"missing recorded fixture {path}")
    return path.read_bytes()


def snap(name: str, url: str | None = None, *, status: int = 200, **meta: Any) -> RawSnapshot:
    return RawSnapshot(
        content=fixture_bytes(name),
        content_type="application/octet-stream",
        url=url or f"https://fixture.test/{name}",
        retrieved_at=RECORDED_AT,
        http_status=status,
        ext=name.rsplit(".", 1)[-1],
        meta=meta,
    )


class FakeHttp(PoliteSession):
    """A `PoliteSession` whose GETs are answered by `routes` (a URL substring -> fixture name, or
    a callable returning `(status, bytes, headers)`); anything unrouted is a 404. Records every
    request it answered."""

    def __init__(self, routes: Mapping[str, str | Callable[..., tuple[int, bytes, dict[str, str]]]]) -> None:
        super().__init__(sleep=lambda _s: None)
        self.routes = dict(routes)
        self.calls: list[dict[str, Any]] = []

    def request(
        self, method: str, url: str, *, honour_robots: bool = True, **kwargs: Any
    ) -> requests.Response:
        self.requests_made += 1
        params = kwargs.get("params") or {}
        headers = kwargs.get("headers") or {}
        self.calls.append({"method": method, "url": url, "params": dict(params), "headers": dict(headers)})
        resp = requests.models.Response()
        resp.url = url
        for key, answer in self.routes.items():
            if key in url:
                if callable(answer):
                    status, body, extra = answer(url=url, params=params, headers=headers)
                else:
                    status, body, extra = 200, fixture_bytes(answer), {}
                resp.status_code = status
                resp._content = body
                resp.headers.update(extra)
                return resp
        resp.status_code = 404
        resp._content = b"not found"
        return resp


@pytest.fixture(scope="session")
def registry() -> Registry:
    return Registry()
