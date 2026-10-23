"""The map page's opening figures and the `/texas` landing link (`web/headline.py`; owner, 2026-10-23:
"a site link that will do well when it lands in a contact's inbox").

`web.app.app` against a hand-written fake API whose counts depend on the parameters asked, so each
figure can be told apart. No database and no `services.api` import; the fake is written here
rather than imported, per this repo's convention that no `web/test_*.py` imports another one.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import QueryParams

from web import headline
from web.api_client import ApiClient
from web.app import app as web_app
from web.viewmodels import ACTIVE_PROPOSAL_STATES, map_description

HEALTH: dict[str, Any] = {"status": "ok", "data_as_of": "2026-10-20", "live_as_of": "2026-10-23T00:00:00Z"}
VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}],
        "proposal_kind": [{"value": "generation"}, {"value": "load"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
    }
}

Counts = Callable[[str, Mapping[str, Any]], int]


def default_counts(path: str, params: Mapping[str, Any]) -> int:
    """412 large loads, 9 open calls; the lifecycle notice's buckets count 100 each (so 100 active)."""
    scale = 1 if params.get("jurisdiction") in (None, "US-TX") else 0
    if path == "/v1/opportunities":
        return 9 * scale if params.get("status") == "open" else 0
    if params.get("kind") == "load":
        return 412 * scale
    return 100


class CountingTransport:
    """Answers every list with an empty page whose `meta.total` comes from `counts`; records calls."""

    def __init__(self, counts: Counts = default_counts, fail: set[str] | None = None) -> None:
        self.counts = counts
        self.fail = fail or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        query = dict(params or {})
        self.calls.append((url, query))
        if url == "/v1/health":
            return httpx.Response(200, json=HEALTH)
        if url == "/v1/meta/vocabularies":
            return httpx.Response(200, json=VOCAB)
        if url == "/v1/sources":
            return httpx.Response(200, json={"data": []})
        if url in ("/v1/proposals", "/v1/opportunities"):
            key = query.get("kind") or query.get("status") or ""
            if key in self.fail:
                return httpx.Response(500, json={"title": "error"})
            total = self.counts(url, query)
            return httpx.Response(
                200,
                json={"data": [], "page": {"has_more": False, "next_cursor": None}, "meta": {"total": total}},
            )
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def post(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, json={"title": "not_found"})

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self.get(url, params=kwargs.get("params"))

    def close(self) -> None:
        pass

    def figure_calls(self) -> list[tuple[str, dict[str, Any]]]:
        """The figures' own count calls: the lifecycle notice's buckets never name `kind=load`,
        and the map page asks `/v1/opportunities` for nothing else."""
        return [(url, q) for url, q in self.calls if url == "/v1/opportunities" or q.get("kind") == "load"]


@pytest.fixture()
def install() -> Iterator[Callable[[CountingTransport], TestClient]]:
    def _install(transport: CountingTransport) -> TestClient:
        web_app.state.api_client = ApiClient(transport)
        web_app.state.lag_days_default = None
        return TestClient(web_app)

    yield _install
    for key in ("api_client", "lag_days_default"):
        web_app.state.__dict__.pop(key, None)


def _meta(body: str, attr: str, name: str) -> str | None:
    m = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', body)
    return html.unescape(m.group(1)) if m else None


def _figures(body: str) -> list[tuple[str, str, str]]:
    """(key, number, words) per figure link, in page order."""
    return [
        (key, number, html.unescape(words))
        for key, number, words in re.findall(
            r'data-figure="(\w+)"><strong>([\d,]+)</strong> <span>([^<]+)</span>', body
        )
    ]


def test_the_plain_map_opens_with_three_live_figures_each_linking_to_its_list(install: Any) -> None:
    transport = CountingTransport()
    body = install(transport).get("/").text

    assert _figures(body) == [
        ("active", "100", "active projects"),
        ("loads", "412", "planned data centres and other large loads"),
        ("open", "9", "RFPs and funding calls open now"),
    ]
    hrefs = dict(re.findall(r'<a href="([^"]+)" data-figure="(\w+)"', body))
    links = {key: html.unescape(href) for href, key in hrefs.items()}
    assert links == {
        "active": "/proposals",
        "loads": "/proposals?kind=load",
        "open": "/opportunities?status=open",
    }
    # The active count is the notice's own (no call of its own); the other two are counted as an
    # anonymous visitor sees the active register, one call each.
    loads, open_calls = transport.figure_calls()
    assert loads == (
        "/v1/proposals",
        {"lifecycle_state": ",".join(ACTIVE_PROPOSAL_STATES), "kind": "load", "limit": 1, "include": "count"},
    )
    assert open_calls == ("/v1/opportunities", {"status": "open", "limit": 1, "include": "count"})


def test_the_preview_card_leads_with_the_figures_and_names_the_page(install: Any) -> None:
    body = install(CountingTransport()).get("/").text

    title = re.search(r"<title>(.*?)</title>", body)
    assert title is not None
    assert title.group(1) == "Inframation: planned energy projects, large loads and RFPs"
    description = _meta(body, "name", "description")
    assert description == (
        "100 active projects, 412 planned data centres and other large loads and 9 RFPs and "
        f"funding calls open now. {map_description()}"
    )
    assert _meta(body, "property", "og:description") == description
    assert _meta(body, "property", "og:title") == title.group(1)


def test_a_zero_count_is_left_out_and_no_figures_means_the_usual_description(install: Any) -> None:
    def no_rfps(path: str, params: Mapping[str, Any]) -> int:
        return 0 if path == "/v1/opportunities" else default_counts(path, params)

    body = install(CountingTransport(no_rfps)).get("/").text
    assert [key for key, _n, _w in _figures(body)] == ["active", "loads"]
    description = _meta(body, "name", "description")
    assert description is not None and description.startswith("100 active projects and 412 planned")

    body = install(CountingTransport(lambda path, params: 0)).get("/").text
    assert "map-figures" not in body
    assert _meta(body, "name", "description") == map_description()


def test_a_count_the_api_refuses_is_left_out_and_the_page_still_renders(install: Any) -> None:
    response = install(CountingTransport(fail={"load"})).get("/")
    assert response.status_code == 200
    assert [key for key, _n, _w in _figures(response.text)] == ["active", "open"]


def test_a_filtered_view_shows_no_figures_and_asks_for_none(install: Any) -> None:
    transport = CountingTransport()
    body = install(transport).get("/?technology=solar&utm_source=newsletter").text
    assert "map-figures" not in body
    assert transport.figure_calls() == []
    assert _meta(body, "name", "description") == map_description()


def test_figures_are_counted_once_per_ten_minutes_per_api_client(install: Any) -> None:
    transport = CountingTransport()
    client = install(transport)
    client.get("/")
    client.get("/?center=-99.3000,31.3000&zoom=4.90&utm_campaign=intro")
    assert len(transport.figure_calls()) == 2

    fresh = CountingTransport()
    install(fresh).get("/")
    assert len(fresh.figure_calls()) == 2  # a new client never reads another client's counts


def test_texas_is_a_short_link_to_the_map_filtered_and_framed_on_texas(install: Any) -> None:
    client = install(CountingTransport())
    response = client.get("/texas?utm_source=intro&utm_medium=email&kind=load", follow_redirects=False)

    assert response.status_code == 302
    target = urlsplit(response.headers["location"])
    assert target.path == "/"
    assert parse_qs(target.query) == {
        "jurisdiction": ["US-TX"],
        "center": ["-99.3000,31.3000"],
        "zoom": ["4.90"],
        "utm_source": ["intro"],
        "utm_medium": ["email"],
    }


def test_the_texas_view_names_texas_and_counts_only_texas(install: Any) -> None:
    transport = CountingTransport()
    body = install(transport).get(headline.LANDINGS[0].href + "&utm_source=intro").text

    title = re.search(r"<title>(.*?)</title>", body)
    assert title is not None
    assert title.group(1) == "Texas energy projects, large loads and RFPs | Inframation"
    heading = re.search(r'<h1 class="map-intro__title">(.*?)</h1>', body)
    assert heading is not None and heading.group(1).endswith(" in Texas")
    assert 'aria-label="In Texas now"' in body
    assert transport.figure_calls() and all(
        q.get("jurisdiction") == "US-TX" for _u, q in transport.figure_calls()
    )
    links = [html.unescape(h) for h in re.findall(r'<a href="([^"]+)" data-figure', body)]
    assert len(links) == 3 and all("jurisdiction=US-TX" in h for h in links)
    description = _meta(body, "name", "description")
    assert description is not None and description.startswith("100 active projects, 412 planned ")
    assert " open now in Texas. " in description


def test_another_states_jurisdiction_is_a_filter_not_a_landing() -> None:
    assert headline.landing_for(QueryParams("jurisdiction=US-CA")) is None
    assert not headline.shows_figures(QueryParams("jurisdiction=US-CA"))
    assert headline.landing_for(QueryParams("jurisdiction=us-tx&zoom=5&utm_source=x")) is headline.LANDINGS[0]
    # A Texas view with a filter on top is that filter's view, not the landing.
    assert headline.landing_for(QueryParams("jurisdiction=US-TX&kind=load")) is None
    assert headline.shows_figures(QueryParams("center=1,2&zoom=3&layers=plants&placement=exact"))
    assert headline.shows_figures(QueryParams(""))


def test_the_sentence_reads_singular_and_plural_and_names_the_place() -> None:
    figures = [
        headline.active_figure(1, None),
        headline.Figure("loads", 1204, "large load", "large loads", "/"),
    ]
    assert headline.figures_sentence(figures, None) == "1 active project and 1,204 large loads."
    assert headline.figures_sentence(figures[:1], headline.LANDINGS[0]) == "1 active project in Texas."
    assert headline.figures_sentence([], None) == ""


def test_the_cache_expires_after_its_window() -> None:
    now = [0.0]
    cache = headline.FigureCache(clock=lambda: now[0])
    transport = CountingTransport()
    api = ApiClient(transport)
    cache.get(api, None)
    cache.get(api, None)
    assert len(transport.figure_calls()) == 2
    now[0] = headline.CACHE_SECONDS + 1
    cache.get(api, None)
    assert len(transport.figure_calls()) == 4
