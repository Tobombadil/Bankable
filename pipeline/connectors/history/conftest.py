"""Shared helpers for the history connector tests: the recorded fixtures (`*/fixtures/README.md`), a
fake HTTP session that answers URLs from them (so `fetch` never touches the network), and a store
under the test's temporary directory."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import itertools
import json
import pathlib
import zipfile
from collections.abc import Callable, Mapping
from typing import Any

import pytest
import requests

from pipeline.connectors.http import PoliteSession
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store, ts_token

HERE = pathlib.Path(__file__).resolve().parent
EIA_FIXTURES = HERE / "eia860m" / "fixtures"
ERCOT_FIXTURES = HERE / "ercot_gis" / "fixtures"
RECORDED_AT = dt.datetime(2026, 10, 23, 22, 0, tzinfo=dt.UTC)

Answer = str | bytes | Callable[..., tuple[int, bytes, dict[str, str]]]


def fixture_bytes(path: pathlib.Path) -> bytes:
    if not path.exists():
        raise FileNotFoundError(f"missing recorded fixture {path}")
    return path.read_bytes()


class FakeHttp(PoliteSession):
    """A `PoliteSession` whose requests are answered by `routes`: a URL substring -> a fixture path
    (str), raw bytes, or a callable returning `(status, bytes, headers)`. Anything unrouted is a
    404. Records every request; a URL under `/archive/` on eia.gov fails the test outright."""

    def __init__(self, routes: Mapping[str, Answer]) -> None:
        super().__init__(sleep=lambda _s: None)
        self.routes = dict(routes)
        self.calls: list[dict[str, Any]] = []

    def request(
        self, method: str, url: str, *, honour_robots: bool = True, **kwargs: Any
    ) -> requests.Response:
        if "eia.gov" in url and "/archive/" in url:
            raise AssertionError(f"a request under eia.gov /archive/ (robots.txt disallows it): {url}")
        self.requests_made += 1
        headers = dict(kwargs.get("headers") or {})
        self.calls.append({"method": method, "url": url, "headers": headers, "honour_robots": honour_robots})
        resp = requests.models.Response()
        resp.url = url
        for key, answer in self.routes.items():
            if key in url:
                if callable(answer):
                    status, body, extra = answer(url=url, headers=headers)
                elif isinstance(answer, bytes):
                    status, body, extra = 200, answer, {}
                else:
                    status, body, extra = 200, fixture_bytes(pathlib.Path(answer)), {}
                resp.status_code = status
                resp._content = body
                resp._content_consumed = True  # `iter_content` (a streamed download) reads `_content`
                resp.headers.update(extra)
                return resp
        resp.status_code = 404
        resp._content = b"not found"
        resp._content_consumed = True
        return resp


@pytest.fixture(scope="session")
def registry() -> Registry:
    return Registry()


@pytest.fixture()
def store(tmp_path: pathlib.Path) -> Store:
    return Store(tmp_path / "data")


_TICKS = itertools.count()


def clock(at: dt.datetime | None = None) -> Callable[[], dt.datetime]:
    """A fixed clock for one run. Each call without `at` is a minute later than the last, so two
    runs of one test never share a run token (`ts_token`), and their records stay in order."""
    when = at or RECORDED_AT + dt.timedelta(minutes=next(_TICKS))
    return lambda: when


def stored_run(
    store: Store,
    source_id: str,
    content: bytes,
    *,
    url: str,
    at: dt.datetime = RECORDED_AT,
    meta: Mapping[str, Any] | None = None,
) -> str:
    """Store `content` as `source_id`'s snapshot with a run record, as the record runner leaves
    them (`pipeline/connectors/runner.py`); returns the object key."""
    ts = ts_token(at)
    path = store.write_snapshot(source_id, ts, "xlsx", content)
    record = {
        "id": f"run-{ts}",
        "status": "ok",
        "started_at": at.isoformat(),
        "snapshot": {
            "object_key": store.locate(path),
            "sha256": hashlib.sha256(content).hexdigest(),
            "fetched_url": url,
            "retrieved_at": at.isoformat().replace("+00:00", "Z"),
            "meta": dict(meta or {}),
        },
    }
    store.write_run(source_id, ts, record)
    return store.key(path)


def zip_of(members: Mapping[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buf.getvalue()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value).encode("utf-8")
