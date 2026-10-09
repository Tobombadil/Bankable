"""Feed links and the API page (audit 2026-10-07 UX-6; docs/30 §1.1, §2, §3).

Before: the API served `/feeds/{proposals,opportunities,events}.{rss,json}` and `/pricing` sold
"RSS feeds of new proposals and opportunities", yet no page linked a feed, no page carried
`<link rel="alternate" type="application/rss+xml">`, and nothing linked the API reference. These
pin the feed twin of each list (same filters, the lifecycle or status spelt out), the "View as"
feed link, the footer link, the `/docs/api` page and the development relay. The fake transport is
duplicated, per this repo's convention that no `web/test_*.py` imports another.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.viewmodels import ACTIVE_PROPOSAL_STATES

EMPTY = {
    "data": [],
    "meta": {"total": 0, "total_is_estimate": False},
    "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
    "licence_summary": {"sources": []},
}
VOCAB = {
    "data": {
        "technology": [{"value": "solar"}],
        "proposal_kind": [{"value": "load"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [],
    }
}
HEALTH = {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}
RSS = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Proposals</title></channel></rss>'


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: Any = None
    ) -> httpx.Response:
        self.calls.append((url, dict(params or {}), cookies))
        if url.startswith("/feeds/"):
            return httpx.Response(200, content=RSS, headers={"content-type": "application/rss+xml"})
        if url == "/v1/meta/vocabularies":
            return httpx.Response(200, json=VOCAB)
        if url == "/v1/health":
            return httpx.Response(200, json=HEALTH)
        if url == "/v1/sources":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json=EMPTY)

    def post(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, json={"title": "not_found"})

    def request(self, method: str, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, json={"title": "not_found"})

    def close(self) -> None:
        pass


@pytest.fixture()
def transport() -> Iterator[FakeTransport]:
    fake = FakeTransport()
    web_app.state.api_client = ApiClient(fake)
    web_app.state.lag_days_default = None
    yield fake
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


def _alternate(html: str) -> str:
    match = re.search(
        r'<link rel="alternate" type="application/rss\+xml" title="[^"]*" href="([^"]+)">', html
    )
    assert match, "no RSS alternate link"
    return match.group(1).replace("&amp;", "&")


def test_proposal_list_links_its_feed_twin(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        html = client.get("/proposals?kind=load&jurisdiction=US-VA&cursor=x").text
    href = _alternate(html)
    parts = urlsplit(href)
    assert parts.path == "/feeds/proposals.rss"
    query = parse_qs(parts.query)
    assert query["kind"] == ["load"] and query["jurisdiction"] == ["US-VA"]
    assert query["lifecycle_state"] == [",".join(ACTIVE_PROPOSAL_STATES)]
    assert "cursor" not in query
    assert 'id="view-as-feed"' in html


def test_map_and_opportunity_list_link_their_feeds(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        home = client.get("/?kind=load").text
        opps = client.get("/opportunities?kind=rfp").text
    assert urlsplit(_alternate(home)).path == "/feeds/proposals.rss"
    assert 'id="view-as-feed"' in home
    opp_href = _alternate(opps)
    assert urlsplit(opp_href).path == "/feeds/opportunities.rss"
    assert parse_qs(urlsplit(opp_href).query) == {"kind": ["rfp"], "status": ["open"]}
    assert "RSS feed of these opportunities" in opps


def test_footer_links_the_api_page_and_it_lists_every_feed(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        about = client.get("/proposals").text
        page = client.get("/docs/api")
    assert '<a href="/docs/api">API and feeds</a>' in about
    assert page.status_code == 200
    for name in ("proposals", "opportunities", "events"):
        for fmt in ("rss", "json"):
            assert f'href="/feeds/{name}.{fmt}"' in page.text
    assert 'href="https://api.infraque.com/redoc"' in page.text


def test_api_page_names_this_deployments_api_host(transport: FakeTransport, monkeypatch: Any) -> None:
    with TestClient(web_app, base_url="https://staging.infraque.com") as client:
        assert "https://api-staging.infraque.com/redoc" in client.get("/docs/api").text
    monkeypatch.setenv("PUBLIC_API_URL", "http://127.0.0.1:8000/")
    with TestClient(web_app) as client:
        assert 'href="http://127.0.0.1:8000/openapi.json"' in client.get("/docs/api").text


def test_feed_relay_forwards_the_query_and_no_cookies(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        client.cookies.set("session", "secret")
        response = client.get("/feeds/proposals.rss?kind=load")
        unknown = client.get("/feeds/secrets.rss")
    assert response.status_code == 200
    assert response.content == RSS
    assert response.headers["content-type"].startswith("application/rss+xml")
    url, params, cookies = next(c for c in transport.calls if c[0].startswith("/feeds/"))
    assert (url, params, cookies) == ("/feeds/proposals.rss", {"kind": "load"}, None)
    assert unknown.status_code == 404
