"""The map landing's front strip and the `/texas` landing link (`web/headline.py`; owner, 2026-10-23:
"a site link that will do well when it lands in a contact's inbox"; 2026-10-24: direction A).

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
#: The strip's scope on the plain map: every US jurisdiction code, as one comma-separated value.
US_ALL = ",".join(headline.US_JURISDICTIONS)
#: Scheduled sources fetched on 22 and 24 October 2026, and one nobody polls (not counted).
COVERAGE: dict[str, Any] = {
    "data": {
        "vintage": {
            "sources": [
                {"source_id": "a", "fetched_at": "2026-10-24T05:00:00Z", "freshness": {"status": "fresh"}},
                {"source_id": "b", "fetched_at": "2026-10-22T05:00:00Z", "freshness": {"status": "stale"}},
                {
                    "source_id": "c",
                    "fetched_at": "2025-01-01T00:00:00Z",
                    "freshness": {"status": "unscheduled"},
                },
            ]
        }
    }
}

Counts = Callable[[str, Mapping[str, Any]], int]


def default_counts(path: str, params: Mapping[str, Any]) -> int:
    """412 large loads, 9 open calls and 100 of anything else, in the US or Texas; none elsewhere."""
    scale = 1 if params.get("jurisdiction") in (None, "US-TX", US_ALL) else 0
    if path == "/v1/opportunities":
        return 9 * scale if params.get("status") == "open" else 0
    if params.get("kind") == "load":
        return 412 * scale
    return 100 * scale


class CountingTransport:
    """Answers every list with an empty page whose `meta.total` comes from `counts`; records calls."""

    def __init__(
        self,
        counts: Counts = default_counts,
        fail: set[str] | None = None,
        coverage: dict[str, Any] | None = None,
    ) -> None:
        self.counts = counts
        self.fail = fail or set()
        self.coverage = coverage
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
        if url == "/v1/coverage" and self.coverage is not None:
            return httpx.Response(200, json=self.coverage)
        if url in ("/v1/proposals", "/v1/opportunities"):
            key = query.get("kind") or query.get("status") or ""
            if key in self.fail:
                return httpx.Response(500, json={"title": "error"})
            total = self.counts(url, query)
            return httpx.Response(
                200,
                json={
                    "data": [],
                    "page": {"has_more": False, "next_cursor": None, "prev_cursor": None},
                    "meta": {"total": total},
                },
            )
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def post(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, json={"title": "not_found"})

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self.get(url, params=kwargs.get("params"))

    def close(self) -> None:
        pass

    def figure_calls(self) -> list[tuple[str, dict[str, Any]]]:
        """The figures' own count calls. They carry the strip's scope (every US code on the plain map,
        a landing's code on a landing), which no other call on the plain map does; on a landing the
        lifecycle notice's buckets carry it too, and the active figure's call is the active bucket's."""
        return [
            (url, q)
            for url, q in self.calls
            if url in ("/v1/proposals", "/v1/opportunities")
            and q.get("include") == "count"
            and q.get("limit") == 1
            and (url == "/v1/opportunities" or q.get("kind") == "load" or q.get("jurisdiction") == US_ALL)
        ]


def _forget(name: str) -> None:
    """Drop one of the app's cached values (Starlette keeps `app.state` in its own mapping)."""
    if hasattr(web_app.state, name):
        delattr(web_app.state, name)


@pytest.fixture()
def install() -> Iterator[Callable[[CountingTransport], TestClient]]:
    def _install(transport: CountingTransport) -> TestClient:
        web_app.state.api_client = ApiClient(transport)
        web_app.state.lag_days_default = None
        _forget("coverage_data")  # the coverage read is cached per process
        return TestClient(web_app)

    yield _install
    for key in ("api_client", "lag_days_default", "coverage_data"):
        _forget(key)


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


def _links(body: str) -> dict[str, str]:
    return {
        key: html.unescape(href) for href, key in re.findall(r'<a href="([^"]+)" data-figure="(\w+)"', body)
    }


def test_the_plain_map_opens_with_three_live_figures_each_linking_to_its_list(install: Any) -> None:
    transport = CountingTransport()
    body = install(transport).get("/").text

    # The share card's words (owner, 2026-10-24): projects, data centres, open calls.
    assert _figures(body) == [
        ("active", "100", "projects"),
        ("loads", "412", "data centres"),
        ("open", "9", "open calls"),
    ]
    # Each link names the US by every code, readable (commas are not escaped).
    assert _links(body) == {
        "active": f"/proposals?jurisdiction={US_ALL}",
        "loads": f"/proposals?jurisdiction={US_ALL}&kind=load",
        "open": f"/opportunities?jurisdiction={US_ALL}&status=open",
    }
    # Every figure is counted with its own call, as an anonymous visitor sees the register.
    active, loads, open_calls = transport.figure_calls()
    active_states = ",".join(ACTIVE_PROPOSAL_STATES)
    assert active == (
        "/v1/proposals",
        {
            "jurisdiction": US_ALL,
            "lifecycle_state": active_states,
            "listed": "true",
            "limit": 1,
            "include": "count",
        },
    )
    assert loads == (
        "/v1/proposals",
        {
            "jurisdiction": US_ALL,
            "kind": "load",
            "lifecycle_state": active_states,
            "listed": "true",
            "limit": 1,
            "include": "count",
        },
    )
    # Energy-relevant notices only, as the RFPs page lists by default, so the figure and its list agree.
    assert open_calls == (
        "/v1/opportunities",
        {"jurisdiction": US_ALL, "status": "open", "relevance": "energy", "limit": 1, "include": "count"},
    )


def test_each_figure_counts_exactly_what_its_link_lists(install: Any) -> None:
    """A figure is the "N match" of the list it opens: following each link, the list page asks the API
    for the same filters the figure was counted with (only paging and sorting differ)."""
    for path in ("/", headline.LANDINGS[0].href):
        transport = CountingTransport()
        client = install(transport)
        body = client.get(path).text
        # Every count call the landing made (on a landing the active figure's is the notice's active
        # bucket's own query).
        counted = {
            (url, tuple(sorted((k, v) for k, v in q.items() if k not in ("limit", "include"))))
            for url, q in transport.calls
            if url in ("/v1/proposals", "/v1/opportunities")
            and q.get("include") == "count"
            and q.get("limit") == 1
        }
        links = _links(body)
        assert len(links) == 3, path
        for key, href in links.items():
            transport.calls.clear()
            assert client.get(href).status_code == 200, href
            endpoint = "/v1/opportunities" if href.startswith("/opportunities") else "/v1/proposals"
            rows_call = next(q for url, q in transport.calls if url == endpoint and "sort" in q)
            listed = tuple(
                sorted(
                    (k, v)
                    for k, v in rows_call.items()
                    if k not in ("sort", "cursor", "include", "limit") and v
                )
            )
            assert (endpoint, listed) in counted, (path, key, listed, counted)


def test_the_strip_line_is_the_pages_h1_and_names_its_place(install: Any) -> None:
    body = install(CountingTransport()).get("/").text
    heading = re.search(r'<h1 class="map-strip__title" id="map-title">(.*?)</h1>', body)
    assert heading is not None
    assert heading.group(1) == "Planned US energy projects, each linked to its source."
    assert 'aria-label="In the United States now"' in body
    assert (
        headline.strip_line(headline.LANDINGS[0])
        == "Planned Texas energy projects, each linked to its source."
    )
    # A filtered view keeps the map's own heading: its notice counts what it shows.
    body = install(CountingTransport()).get("/?technology=solar").text
    heading = re.search(r'<h1 class="map-strip__title" id="map-title">(.*?)</h1>', body)
    assert heading is not None and "each linked to its source" not in heading.group(1)


def test_the_strip_says_when_the_sources_were_read(install: Any) -> None:
    body = install(CountingTransport(coverage=COVERAGE)).get("/").text
    stamp = re.search(r'<p class="map-strip__stamp">(.*?)</p>', body, re.S)
    assert stamp is not None
    assert re.sub(r"<[^>]+>", "", stamp.group(1)) == "Sources read 22–24 Oct 2026"
    assert "<a " not in stamp.group(1)  # a statement; the credits hold the sources' pages
    assert '<time datetime="2026-10-22">22</time>' in stamp.group(1)
    assert '<time datetime="2026-10-24">24 Oct 2026</time>' in stamp.group(1)
    # Without coverage the strip claims nothing about when.
    body = install(CountingTransport()).get("/").text
    assert "map-strip__stamp" not in body


def test_the_sources_read_stamp_names_a_month_or_year_once() -> None:
    def text(oldest: str, newest: str) -> str:
        stamp = headline.sources_read({"available": True, "oldest_fetch": oldest, "newest_fetch": newest})
        assert stamp is not None
        return stamp.text

    assert text("2026-10-24T01:00:00Z", "2026-10-24T09:00:00Z") == "24 Oct 2026"
    assert text("2026-10-22", "2026-10-24") == "22–24 Oct 2026"
    assert text("2026-09-30", "2026-10-02") == "30 Sep – 2 Oct 2026"
    assert text("2025-12-30", "2026-01-02") == "30 Dec 2025 – 2 Jan 2026"
    assert headline.sources_read({"available": False}) is None
    assert headline.sources_read({"available": True, "oldest_fetch": None, "newest_fetch": None}) is None


def test_the_preview_card_leads_with_the_figures_and_names_the_page(install: Any) -> None:
    body = install(CountingTransport()).get("/").text

    title = re.search(r"<title>(.*?)</title>", body)
    assert title is not None
    # docs/30 §11.6: the home title is the copy rule's.
    assert html.unescape(title.group(1)) == "Inframation — Energy market intelligence"
    description = _meta(body, "name", "description")
    # The data centres are among the projects, and the sentence says so (review of lane v2-landing).
    assert description == (
        f"100 projects (412 of them data centres) and 9 open calls in the US. {map_description()}"
    )
    assert _meta(body, "property", "og:description") == description
    assert _meta(body, "property", "og:title") == html.unescape(title.group(1))


def test_a_zero_count_is_left_out_and_no_figures_means_the_usual_description(install: Any) -> None:
    def no_rfps(path: str, params: Mapping[str, Any]) -> int:
        return 0 if path == "/v1/opportunities" else default_counts(path, params)

    body = install(CountingTransport(no_rfps)).get("/").text
    assert [key for key, _n, _w in _figures(body)] == ["active", "loads"]
    description = _meta(body, "name", "description")
    assert description is not None
    assert description.startswith("100 projects (412 of them data centres) in the US.")

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
    assert len(transport.figure_calls()) == 3

    fresh = CountingTransport()
    install(fresh).get("/")
    assert len(fresh.figure_calls()) == 3  # a new client never reads another client's counts


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
    assert title.group(1) == "Texas energy projects, data centres and open calls | Inframation"
    heading = re.search(r'<h1 class="map-strip__title" id="map-title">(.*?)</h1>', body)
    assert (
        heading is not None
        and heading.group(1) == "Planned Texas energy projects, each linked to its source."
    )
    assert 'aria-label="In Texas now"' in body
    texas_calls = [
        (u, q)
        for u, q in transport.calls
        if u in ("/v1/proposals", "/v1/opportunities") and q.get("include") == "count" and q.get("limit") == 1
    ]
    assert texas_calls and all(q.get("jurisdiction") == "US-TX" for _u, q in texas_calls)
    links = list(_links(body).values())
    assert len(links) == 3 and all("jurisdiction=US-TX" in h for h in links)
    description = _meta(body, "name", "description")
    assert description is not None and description.startswith("100 projects (412 of them data centres) and ")
    assert " open calls in Texas. " in description


def test_another_states_jurisdiction_is_a_filter_not_a_landing() -> None:
    assert headline.landing_for(QueryParams("jurisdiction=US-CA")) is None
    assert not headline.shows_figures(QueryParams("jurisdiction=US-CA"))
    assert headline.landing_for(QueryParams("jurisdiction=us-tx&zoom=5&utm_source=x")) is headline.LANDINGS[0]
    # A Texas view with a filter on top is that filter's view, not the landing.
    assert headline.landing_for(QueryParams("jurisdiction=US-TX&kind=load")) is None
    assert headline.shows_figures(QueryParams("center=1,2&zoom=3&layers=plants&placement=exact"))
    assert headline.shows_figures(QueryParams(""))


def test_the_sentence_reads_singular_and_plural_and_names_the_place() -> None:
    """The data centres are counted among the projects (`kind=load` of the same list), so the sentence
    says "of them" rather than reading as three separate sets (review of lane v2-landing)."""
    active = headline.Figure("active", 3787, "project", "projects", "/")
    loads = headline.Figure("loads", 73, "data centre", "data centres", "/")
    calls = headline.Figure("open", 10, "open call", "open calls", "/")
    assert (
        headline.figures_sentence([active, loads, calls], None)
        == "3,787 projects (73 of them data centres) and 10 open calls in the US."
    )
    one = headline.Figure("loads", 1, "data centre", "data centres", "/")
    two = headline.Figure("active", 2, "project", "projects", "/")
    assert headline.figures_sentence([two, one], None) == "2 projects (1 of them a data centre) in the US."
    single = headline.Figure("active", 1, "project", "projects", "/")
    assert headline.figures_sentence([single], headline.LANDINGS[0]) == "1 project in Texas."
    # Without the projects' figure the data centres stand on their own.
    assert headline.figures_sentence([loads, calls], None) == "73 data centres and 10 open calls in the US."
    assert headline.figures_sentence([], None) == ""


def test_the_us_scope_is_every_us_code_and_nothing_else() -> None:
    codes = headline.US_JURISDICTIONS
    assert len(codes) == len(set(codes)) == 59  # the country, 50 states, DC, 6 outlying areas, OCS
    assert codes[0] == "US" and {"US-TX", "US-DC", "US-PR", "US-OCS"} <= set(codes)
    assert all(c == "US" or c.startswith("US-") for c in codes)


def test_the_cache_expires_after_its_window() -> None:
    now = [0.0]
    # The refresh runs where the request finds the counts stale (a thread in the app, here at once).
    cache = headline.FigureCache(clock=lambda: now[0], spawn=lambda work: work())
    transport = CountingTransport()
    api = ApiClient(transport)
    cache.get(api, None)
    cache.get(api, None)
    assert len(transport.figure_calls()) == 3
    now[0] = headline.CACHE_SECONDS + 1
    cache.get(api, None)
    assert len(transport.figure_calls()) == 6


def test_an_ingest_reaches_the_figures_within_a_minute_and_no_visitor_waits_for_the_recount() -> None:
    """Review of lane v2-landing: the figures were kept ten minutes while the lists they open are live,
    so after an hourly ingest the strip could disagree with its list for up to ten minutes. They are now
    asked again after a minute, and the request that finds them stale is served the last counts while
    one refresh runs beside it; the next render reads the new counts."""
    now = [0.0]
    pending: list[Any] = []
    cache = headline.FigureCache(clock=lambda: now[0], spawn=pending.append)
    transport = CountingTransport()
    api = ApiClient(transport)
    first = cache.get(api, None)
    assert len(transport.figure_calls()) == 3  # the first render of a process counts while it renders
    assert headline.CACHE_SECONDS <= 60
    now[0] = 61.0
    stale = cache.get(api, None)
    assert stale is first and len(transport.figure_calls()) == 3  # served at once, nothing asked yet
    assert len(pending) == 1
    assert cache.get(api, None) is first and len(pending) == 1  # one refresh at a time
    pending.pop()()
    assert len(transport.figure_calls()) == 6
    fresh = cache.get(api, None)
    assert fresh is not first and not pending
    now[0] = 61.0 + headline.CACHE_SECONDS - 1
    assert cache.get(api, None) is fresh and not pending


def test_a_refresh_that_cannot_start_or_belongs_to_a_replaced_client_never_blocks_the_next() -> None:
    """Code review of the fix round: a refresh whose thread cannot start left its key marked as running
    for good (the counts never refreshed again), and a refresh for a client the cache had moved past
    could clear the new client's marker, so two refreshes of one key ran at once."""
    now = [0.0]
    attempts: list[Any] = []

    def no_thread(work: Any) -> None:
        attempts.append(work)
        raise RuntimeError("can't start new thread")

    cache = headline.FigureCache(clock=lambda: now[0], spawn=no_thread)
    api = ApiClient(CountingTransport())
    first = cache.get(api, None)
    now[0] = 61.0
    assert cache.get(api, None) is first and len(attempts) == 1  # the last counts stand
    assert cache.get(api, None) is first and len(attempts) == 2  # and the next request tries again

    pending: list[Any] = []
    cache = headline.FigureCache(clock=lambda: now[0], spawn=pending.append)
    old_api, new_api = ApiClient(CountingTransport()), ApiClient(CountingTransport())
    now[0] = 0.0
    cache.get(old_api, None)
    now[0] = 61.0
    cache.get(old_api, None)  # the old client's refresh is waiting to run
    old_refresh = pending.pop()
    cache.get(new_api, None)  # the cache moves to a new client and counts afresh
    now[0] = 122.0
    cache.get(new_api, None)  # the new client's refresh is now running
    assert len(pending) == 1
    old_refresh()  # the old client's refresh ends late
    cache.get(new_api, None)
    assert len(pending) == 1, "a second refresh of the same key started beside the first"
