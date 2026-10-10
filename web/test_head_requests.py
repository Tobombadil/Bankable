"""`HEAD` on the public site (`web/head_requests.py`).

Before 2026-10-07 every page route answered `HEAD` with `405` while `GET` was `200`; static files
were the only paths that answered it. These tests hold the fix in place:

  - every `GET` route answers `HEAD` with the `GET`'s status and headers and an empty body, pages,
    redirects, not-found pages, XML and static files alike;
  - a POST-only route still refuses `HEAD`, and so do the two `GET` routes that spend a token;
  - a `HEAD` is never counted as a page view, although the handler sees `GET`;
  - a streamed body is dropped whole, not just its first chunk.

Same fake-`Transport` pattern as `web/test_slippage_view.py`, duplicated rather than imported (no
`web/test_*.py` imports another).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route

from web.api_client import ApiClient
from web.app import app as web_app
from web.head_requests import NO_HEAD_PATHS, HeadAsGetMiddleware, is_head_request

DEFAULT_HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
}
DEFAULT_VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
    }
}
PROPOSAL: dict[str, Any] = {
    "public_id": "PROP_A",
    "slug": "prop-a",
    "name_canonical": "Project A",
    "kind": "generation",
    "technology": "solar",
    "capacity_mw": 120.0,
    "jurisdiction": "US-TX",
    "lifecycle_state": "under_construction",
    "status_raw": "Under Construction",
    "identifiers": {},
    "proposed_online_date": None,
    "schedule_slip": None,
    "source_count": 1,
    "provenance": [
        {
            "source_id": "us.iso.ercot.gen_queue",
            "source_name": "ERCOT Queue",
            "source_url": "https://example.org/q",
            "retrieved_at": "2026-09-13T00:00:00Z",
            "reuse_class": "open",
            "attribution_text": "ERCOT",
            "source_record_id": "23INR0001",
        }
    ],
}


def _list_env(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows), "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": [],
    }


class FakeTransport:
    """Canned responses keyed by path. A proposal slug other than `prop-a` resolves to nothing, so
    `/proposals/<anything else>` is the site's own 404 page."""

    def __init__(self, responses: Mapping[str, tuple[int, Any]] | None = None) -> None:
        self.responses: dict[str, tuple[int, Any]] = dict(responses or {})
        self.calls: list[tuple[str, str, Any]] = []

    def _respond(self, url: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        if url == "/v1/proposals" and params and params.get("slug") not in (None, "prop-a"):
            return httpx.Response(200, json=_list_env([]))
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("GET", url, dict(params or {})))
        return self._respond(url, params)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("POST", url, json))
        return self._respond(url)

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> httpx.Response:
        self.calls.append((method, url, json if json is not None else dict(params or {})))
        return self._respond(url, params)

    def close(self) -> None:
        pass


def _transport() -> FakeTransport:
    return FakeTransport(
        {
            "/v1/health": (200, DEFAULT_HEALTH),
            "/v1/meta/vocabularies": (200, DEFAULT_VOCAB),
            "/v1/sources": (200, {"data": []}),
            "/v1/proposals": (200, _list_env([PROPOSAL])),
            "/v1/proposals/geo": (
                200,
                {"data": {"totals": {"lifecycle_state_counts": {"under_construction": 1}}}},
            ),
            "/v1/opportunities": (200, _list_env([])),
            "/v1/assets": (200, _list_env([])),
            "/v1/organizations": (200, _list_env([])),
            "/v1/ui-events": (202, {}),
        }
    )


@pytest.fixture()
def transport() -> Iterator[FakeTransport]:
    fake = _transport()
    web_app.state.api_client = ApiClient(fake)
    web_app.state.lag_days_default = None
    yield fake
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


@pytest.fixture()
def web_client(transport: FakeTransport) -> Iterator[TestClient]:
    with TestClient(web_app) as client:
        web_app.state.api_client = ApiClient(transport)
        yield client


#: One of each kind of GET route the site serves: the map, list and record pages, a redirect, the
#: site's own 404 page, XML and text for crawlers, pages from each of the other Jinja environments
#: (legal, pricing, auth), and static files (stylesheet, font, the card image).
GET_PATHS = (
    "/",
    "/proposals",
    "/proposals/prop-a",
    "/proposals/no-such-proposal",
    "/opportunities",
    "/map",
    "/sitemap.xml",
    "/robots.txt",
    "/about",
    "/attribution",
    "/privacy",
    "/legal/reuse",
    "/pricing",
    "/submit",
    "/login",
    "/static/css/styles.css",
    "/static/fonts/IBMPlexSans-Regular-Latin1.woff2",
    "/static/img/og-card.png",
)


@pytest.mark.parametrize("path", GET_PATHS)
def test_head_answers_like_get_with_no_body(web_client: TestClient, path: str) -> None:
    got = web_client.get(path, follow_redirects=False)
    head = web_client.head(path, follow_redirects=False)

    assert head.status_code == got.status_code
    assert head.status_code != 405
    assert dict(head.headers) == dict(got.headers)
    assert head.content == b""
    # The length a client is told is the GET body's, not the empty HEAD body's.
    if "content-length" in got.headers:
        assert int(head.headers["content-length"]) == len(got.content)


def test_the_proposal_page_and_its_404_answer_head_with_their_own_statuses(web_client: TestClient) -> None:
    """The two statuses the defect was reported on, stated without the comparison above."""
    assert web_client.head("/proposals/prop-a").status_code == 200
    assert web_client.head("/proposals/no-such-proposal").status_code == 404


@pytest.mark.parametrize("path", ["/api/ui-events", "/logout", "/report", "/pricing/checkout"])
def test_head_on_a_post_only_route_is_still_refused(web_client: TestClient, path: str) -> None:
    resp = web_client.head(path)

    assert resp.status_code == 405
    assert resp.headers["allow"] == "POST"
    assert resp.content == b""


@pytest.mark.parametrize("path", sorted(NO_HEAD_PATHS))
def test_head_never_spends_a_token(web_client: TestClient, transport: FakeTransport, path: str) -> None:
    """`/verify` and `/unsubscribe` call the API to spend the token in the link; a mail scanner's
    HEAD must not verify an address or unsubscribe a reader."""
    before = len(transport.calls)

    resp = web_client.head(path, params={"token": "t0k3n"})

    assert resp.status_code == 405
    assert not [call for call in transport.calls[before:] if call[1] != "/v1/health"]


def test_head_on_a_record_page_is_not_counted_as_a_view(
    web_client: TestClient, transport: FakeTransport
) -> None:
    def views() -> int:
        return len([c for c in transport.calls if c[:2] == ("POST", "/v1/ui-events")])

    web_client.head("/proposals/prop-a", headers={"user-agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    assert views() == 0
    # Control: the same request as GET from the same agent is counted, so the zero above is the
    # HEAD being recognised, not the counter being off.
    web_client.get("/proposals/prop-a", headers={"user-agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    assert views() == 1


def test_the_middleware_is_registered_once_on_the_site() -> None:
    assert [m.cls for m in web_app.user_middleware].count(HeadAsGetMiddleware) == 1


# ------------------------------------------------------------- the middleware on its own
def _tiny_app() -> Starlette:
    seen: list[tuple[str, bool]] = []

    async def page(request: Request) -> PlainTextResponse:
        seen.append((request.method, is_head_request(request)))
        return PlainTextResponse("hello", headers={"x-page": "1"})

    async def stream(request: Request) -> StreamingResponse:
        async def chunks() -> Any:
            for part in (b"one,", b"two,", b"three"):
                yield part

        return StreamingResponse(chunks(), media_type="text/csv")

    async def submit(request: Request) -> PlainTextResponse:
        return PlainTextResponse("posted")

    app = Starlette(
        routes=[
            Route("/page", page, methods=["GET"]),
            Route("/stream", stream, methods=["GET"]),
            Route("/submit", submit, methods=["POST"]),
        ]
    )
    app.add_middleware(HeadAsGetMiddleware)
    app.state.seen = seen
    return app


def test_the_handler_sees_get_and_can_still_tell_it_was_head() -> None:
    app = _tiny_app()
    with TestClient(app) as client:
        head = client.head("/page")
        client.get("/page")

    assert head.status_code == 200
    assert head.headers["x-page"] == "1"
    assert head.headers["content-length"] == "5"
    assert head.content == b""
    assert app.state.seen == [("GET", True), ("GET", False)]


def test_a_streamed_body_is_dropped_whole() -> None:
    with TestClient(_tiny_app()) as client:
        head = client.head("/stream")
        got = client.get("/stream")

    assert got.content == b"one,two,three"
    assert head.status_code == 200
    assert head.headers["content-type"] == got.headers["content-type"]
    assert head.content == b""


def test_post_only_and_other_methods_pass_through_untouched() -> None:
    with TestClient(_tiny_app()) as client:
        assert client.head("/submit").status_code == 405
        assert client.post("/submit").text == "posted"
        assert client.put("/page").status_code == 405
