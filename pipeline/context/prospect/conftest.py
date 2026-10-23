"""A fake web for the prospect builders' fetch paths: the real `PoliteSession` (robots.txt, retries,
challenge checks, streaming) over a `requests` transport that answers from a route table, so a build
runs end to end with no socket opened (conftest.py's socket guard stays armed)."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from pipeline.connectors.http import PoliteSession

Answer = tuple[int, bytes, dict[str, str]]
Handler = Callable[[requests.PreparedRequest], Answer]


class FakeWeb(BaseAdapter):
    """Routes are keyed by the full URL (query included) or by the URL without its query string; an
    unrouted URL answers 404, so a robots.txt a test does not route is permissive, as on the web."""

    def __init__(self) -> None:
        super().__init__()
        self.routes: dict[str, Handler] = {}
        self.requests: list[requests.PreparedRequest] = []

    # ------------------------------------------------------------------ routes
    def route(self, url: str, handler: Handler) -> None:
        self.routes[url] = handler

    def page(
        self,
        url: str,
        body: bytes | str,
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.routes[url] = lambda _req: (status, data, {"Content-Type": content_type})

    def json(self, url: str, doc: Any, *, status: int = 200) -> None:
        self.page(url, json.dumps(doc), status=status, content_type="application/json; charset=utf-8")

    def file(self, url: str, data: bytes, *, last_modified: str | None = None, ranges: bool = True) -> None:
        """A static file; with `ranges`, `Range: bytes=a-b` and `bytes=-n` answer 206 with a total size."""
        base = {"Content-Type": "application/zip", "Accept-Ranges": "bytes"}
        if last_modified:
            base["Last-Modified"] = last_modified

        def answer(req: requests.PreparedRequest) -> Answer:
            spec = str(req.headers.get("Range") or "")
            if not ranges or not spec.startswith("bytes="):
                return 200, data, {**base, "ETag": '"v1"'}
            first, last = spec.removeprefix("bytes=").split("-")
            if first == "":
                start, end = max(0, len(data) - int(last)), len(data) - 1
            else:
                start, end = int(first), min(int(last), len(data) - 1)
            return 206, data[start : end + 1], {**base, "Content-Range": f"bytes {start}-{end}/{len(data)}"}

        self.routes[url] = answer

    # ------------------------------------------------------------------ what was asked
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def params(self, path: str) -> list[dict[str, str]]:
        """The query parameters of every request whose URL (without query) is `path`, in order."""
        out = []
        for r in self.requests:
            parts = urlsplit(str(r.url))
            if f"{parts.scheme}://{parts.netloc}{parts.path}" == path:
                out.append({k: v[0] for k, v in parse_qs(parts.query).items()})
        return out

    # ------------------------------------------------------------------ transport
    def send(  # type: ignore[override]
        self, request: requests.PreparedRequest, stream: bool = False, **_kwargs: Any
    ) -> requests.Response:
        self.requests.append(request)
        url = str(request.url)
        handler = self.routes.get(url) or self.routes.get(url.split("?", 1)[0])
        status, body, headers = handler(request) if handler else (404, b"not found", {})
        resp = requests.Response()
        resp.status_code = status
        resp.headers = CaseInsensitiveDict(headers)
        resp.raw = io.BytesIO(body)
        resp.url = url
        resp.request = request
        resp.encoding = "utf-8"
        return resp

    def close(self) -> None:
        pass

    def session(self) -> PoliteSession:
        transport = requests.Session()
        transport.trust_env = False
        transport.mount("https://", self)
        transport.mount("http://", self)
        return PoliteSession(max_attempts=2, sleep=lambda _s: None, session=transport)


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()
