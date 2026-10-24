"""Search tabs, About and Sources & licences (Navigation v2 lane V7; docs/30 §11.4, §11.10).

Search: one field, tabs by kind with counts in a fixed order (All · Companies · Projects · Infrastructure ·
RFPs · Funding), a top hit when the words start a company or asset name, five rows per kind on All and
fifty on a kind's tab, a kind that fails costing only itself, and every "All N" and full-list link naming
exactly the set its count counted. About: where the register reaches, US first, each region's figure the
number of published registers its table on /about/sources lists. Sources & licences: by region, published
registers first by records, each count a link to the list that is that count.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from services.api.app import OPPORTUNITY_KIND_VALUES
from web.api_client import ApiClient, ApiError
from web.app import (
    SEARCH_FUNDING_KINDS,
    SEARCH_RFP_KINDS,
    SOURCE_REGIONS,
    search_top_hit,
    source_region,
)
from web.app import app as web_app
from web.viewmodels import ALL_PROPOSAL_LIFECYCLE_STATES

Answer = tuple[int, Any] | Callable[[Mapping[str, Any]], tuple[int, Any]]


class FakeTransport:
    """Canned responses keyed by path, a callable answer seeing the params; every call recorded.
    Duplicated rather than imported: this repo's convention is that no `web/test_*.py` imports another."""

    def __init__(self, responses: Mapping[str, Answer]) -> None:
        self.responses: dict[str, Answer] = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _respond(self, url: str, params: Mapping[str, Any] | None) -> httpx.Response:
        self.calls.append((url, dict(params or {})))
        answer = self.responses.get(url)
        if answer is None:
            return httpx.Response(404, json={"title": "not_found", "detail": url})
        status, body = answer(params or {}) if callable(answer) else answer
        return httpx.Response(status, json=body)

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: Any = None
    ) -> httpx.Response:
        return self._respond(url, params)

    def post(self, url: str, *, json: Any = None, cookies: Any = None) -> httpx.Response:
        return self._respond(url, None)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self._respond(url, kwargs.get("params"))

    def close(self) -> None:
        pass

    def params(self, url: str) -> list[dict[str, Any]]:
        return [p for u, p in self.calls if u == url]


HEALTH = {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}


def _page(rows: list[dict[str, Any]], *, total: int | None = None, more: bool = False) -> dict[str, Any]:
    meta = {"total": total} if total is not None else {}
    return {
        "data": rows,
        "meta": meta,
        "page": {"has_more": more, "next_cursor": "c2" if more else None},
        "licence_summary": {"sources": []},
    }


def _org(name: str, slug: str) -> dict[str, Any]:
    return {"public_id": f"org_{slug}", "slug": slug, "name_canonical": name, "type": "developer"}


def _proposal(n: int) -> dict[str, Any]:
    return {
        "public_id": f"prop_{n}",
        "slug": f"sun-farm-{n}",
        "name_canonical": f"Sun Farm {n}",
        "kind": "generation",
        "technology": "solar_pv",
        "lifecycle_state": "studied",
        "capacity_mw": 100.0 + n,
        "jurisdiction": "US-TX",
        "provenance": [],
    }


def _asset(name: str, slug: str) -> dict[str, Any]:
    return {
        "public_id": f"asset_{slug}",
        "slug": slug,
        "name": name,
        "asset_type": "power_plant",
        "provenance": [],
    }


def _notice(title: str, kind: str) -> dict[str, Any]:
    return {
        "public_id": f"opp_{kind}",
        "slug": f"{kind}-notice",
        "title": title,
        "kind": kind,
        "status": "open",
    }


def _opportunities(params: Mapping[str, Any]) -> tuple[int, Any]:
    if "rfp" in str(params.get("kind")):
        return 200, _page([_notice("Sun RFP", "rfp")], total=1)
    return 200, _page([], total=0)


def _proposals(params: Mapping[str, Any]) -> tuple[int, Any]:
    limit = int(params.get("limit") or 50)
    return 200, _page([_proposal(n) for n in range(min(limit, 60))], total=60)


@pytest.fixture()
def transport() -> FakeTransport:
    return FakeTransport(
        {
            "/v1/health": (200, HEALTH),
            "/v1/organizations": (200, _page([_org("Sun Power Partners", "sun-power-partners")], total=1)),
            "/v1/proposals": _proposals,
            "/v1/assets": (200, _page([_asset("Sunflower Wind", "sunflower-wind")])),
            "/v1/opportunities": _opportunities,
        }
    )


@pytest.fixture()
def client(transport: FakeTransport) -> Iterator[TestClient]:
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    with TestClient(web_app) as c:
        yield c
    web_app.state.build_info = None
    web_app.state.coverage_facts = None


def _tabs(html: str) -> list[tuple[str, str, bool]]:
    nav = html.split('<nav class="search-tabs"', 1)[1].split("</nav>", 1)[0]
    return [
        (href.replace("&amp;", "&"), re.sub(r"<[^>]+>", "", text).strip(), bool(current))
        for href, current, text in re.findall(r'<a href="([^"]+)"( aria-current="page")?>(.*?)</a>', nav)
    ]


# ------------------------------------------------------------------------------------------- search
def test_search_with_no_words_reads_nothing_and_offers_the_glossary(
    client: TestClient, transport: FakeTransport
) -> None:
    body = client.get("/search").text
    assert "<h1>Search</h1>" in body and 'href="/glossary"' in body
    assert not [u for u, _ in transport.calls if u not in ("/v1/health", "/v1/coverage")]


def test_the_tabs_come_in_one_order_with_counts_and_mark_the_view(client: TestClient) -> None:
    tabs = _tabs(client.get("/search", params={"q": "sun"}).text)
    assert [(label, current) for _, label, current in tabs] == [
        ("All", True),
        ("Companies 1", False),
        ("Projects 60", False),
        ("Infrastructure 1", False),
        ("RFPs 1", False),
        ("Funding 0", False),
    ]
    assert [href for href, _, _ in tabs][1] == "/search?q=sun&type=companies"
    on_projects = _tabs(client.get("/search", params={"q": "sun", "type": "projects"}).text)
    assert [label for _, label, current in on_projects if current] == ["Projects 60"]


def test_all_shows_five_per_kind_with_all_n_and_a_kind_tab_fifty_with_its_full_list(
    client: TestClient, transport: FakeTransport
) -> None:
    body = client.get("/search", params={"q": "sun"}).text
    projects = body.split("<h2>Projects</h2>", 1)[1].split("</section>", 1)[0]
    assert projects.count('class="search-row"') == 5
    assert '<a class="search-bucket__all" href="/search?q=sun&amp;type=projects">All 60</a>' in projects
    assert transport.params("/v1/proposals")[-1] == {"q": "sun", "limit": 5, "include": "count"}

    tab = client.get("/search", params={"q": "sun", "type": "projects"}).text
    assert tab.count('class="search-row"') == 50
    link = re.search(r'<a href="([^"]+)">Open all 60 in Projects</a>', tab)
    assert link is not None
    # The full list names every lifecycle state, so it is not narrowed to the list's own default view
    # (active, listed): it shows exactly the 60 the tab counted.
    query = parse_qs(urlsplit(link.group(1).replace("&amp;", "&")).query)
    assert query == {"q": ["sun"], "lifecycle_state": [",".join(ALL_PROPOSAL_LIFECYCLE_STATES)]}
    assert transport.params("/v1/proposals")[-1]["limit"] == 50


def test_rfps_and_funding_are_read_by_kind_and_every_status(
    client: TestClient, transport: FakeTransport
) -> None:
    client.get("/search", params={"q": "sun"})
    kinds = sorted(p["kind"] for p in transport.params("/v1/opportunities"))
    assert kinds == ["foa,loan_program,program", "rfp,tender,auction,procurement_notice"]
    for p in transport.params("/v1/opportunities"):
        assert "open" in p["status"] and "awarded" in p["status"] and p["include"] == "count"
    org = transport.params("/v1/organizations")[-1]
    assert org["scope"] == "group" and org["include"] == "count"


def test_the_rfps_and_funding_tabs_read_every_notice_kind_once() -> None:
    """A kind the vocabulary gains must join one tab, or `/search` silently stops finding it."""
    rfps, funding = SEARCH_RFP_KINDS.split(","), SEARCH_FUNDING_KINDS.split(",")
    assert not set(rfps) & set(funding)
    assert sorted(rfps + funding) == sorted(OPPORTUNITY_KIND_VALUES)


def test_the_kinds_with_no_match_share_one_line(client: TestClient) -> None:
    body = client.get("/search", params={"q": "sun"}).text
    assert "<h2>Funding</h2>" not in body
    assert '<p class="search-empty search-none">No match in Funding.</p>' in body


def test_a_company_name_is_the_top_hit_and_its_open_button_the_one_primary(client: TestClient) -> None:
    body = client.get("/search", params={"q": "sun power"}).text
    hit = body.split('<section class="top-hit"', 1)[1].split("</section>", 1)[0]
    assert '<a href="/organizations/sun-power-partners">Sun Power Partners</a>' in hit
    assert '<a class="top-hit__open" href="/organizations/sun-power-partners">Open company</a>' in hit
    assert (
        "top-hit"
        not in client.get("/search", params={"q": "sun", "type": "companies"}).text.split("<main", 1)[1]
    )


def test_top_hit_rules() -> None:
    orgs = [_org("Sun Power Partners", "sun-power-partners")]
    assets = [{"name": "Sunflower Wind", "slug": "sunflower-wind"}]
    assert search_top_hit("SUN-POWER", orgs, assets)["kind"] == "company"  # case and punctuation ignored
    assert search_top_hit("sunflower", orgs, assets) == {"kind": "asset", "record": assets[0]}
    assert search_top_hit("su", orgs, assets) is None  # too short to mean a name
    assert search_top_hit("power", orgs, assets) is None  # a name must start with the words


def test_a_kind_that_fails_costs_only_itself(client: TestClient, transport: FakeTransport) -> None:
    transport.responses["/v1/proposals"] = (503, {"title": "unavailable"})
    resp = client.get("/search", params={"q": "sun"})
    assert resp.status_code == 200
    assert "Projects could not be searched just now." in resp.text
    assert ("Projects —", False) in [(label, c) for _, label, c in _tabs(resp.text)]
    assert "Sun Power Partners" in resp.text


def test_a_client_error_is_a_bug_and_stays_loud(client: TestClient, transport: FakeTransport) -> None:
    transport.responses["/v1/proposals"] = (400, {"title": "bad_request"})
    with pytest.raises(ApiError):
        client.get("/search", params={"q": "sun"})


def test_an_unknown_type_is_all(client: TestClient) -> None:
    tabs = _tabs(client.get("/search", params={"q": "sun", "type": "nonsense"}).text)
    assert [label for _, label, current in tabs if current] == ["All"]


def test_a_results_page_is_not_indexed_and_the_empty_page_is(client: TestClient) -> None:
    assert '<meta name="robots" content="noindex, follow">' in client.get("/search", params={"q": "sun"}).text
    assert '<meta name="robots" content="index, follow">' in client.get("/search").text


# -------------------------------------------------------------------------------- About and sources
def _sources(*rows: tuple[str, str, str, str]) -> list[dict[str, Any]]:
    return [
        {
            "source_id": sid,
            "name": f"Register {sid}",
            "jurisdiction": jurisdiction,
            "category": category,
            "operator": "Operator",
            "url": f"https://example.org/{sid}",
            "publish_state": state,
            "attribution_text": None,
            "licence": {"reuse_class": "open", "allows_raw_publication": True, "allows_commercial_use": True},
        }
        for sid, jurisdiction, category, state in rows
    ]


SOURCES = _sources(
    ("us.iso.ercot.gen_queue", "US-TX", "generation_queue", "public"),
    ("us.osm.power_grid", "US", "registry", "public"),
    ("us.iso.pjm.gen_queue", "US-PJM", "generation_queue", "ingest_only"),
    ("gb.neso.tec_register", "GB", "generation_queue", "public"),
    ("eu.ted.api", "EU/EEA", "procurement", "public"),
    ("mdb.worldbank.projects", "global", "funding", "public"),
)


@pytest.fixture()
def about_client(transport: FakeTransport) -> Iterator[TestClient]:
    transport.responses["/v1/sources"] = (200, {"data": SOURCES, "page": {"has_more": False}})
    transport.responses["/v1/proposals"] = (200, _page([], total=7))
    transport.responses["/v1/opportunities"] = (200, _page([], total=3))
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    web_app.state.__dict__.pop("source_records_cache", None)
    with TestClient(web_app) as c:
        yield c
    web_app.state.__dict__.pop("source_records_cache", None)


def _paged_sources(rows: list[dict[str, Any]], split: int) -> Callable[[Mapping[str, Any]], tuple[int, Any]]:
    """`/v1/sources` in two pages, as the API pages it by id: the rows before `split`, then the rest."""

    def pages(params: Mapping[str, Any]) -> tuple[int, Any]:
        if params.get("cursor"):
            return 200, {"data": rows[split:], "page": {"has_more": False}}
        return 200, {"data": rows[:split], "page": {"has_more": True, "next_cursor": "c2"}}

    return pages


def test_source_region_reads_the_jurisdiction_us_first() -> None:
    assert [r for r, _ in SOURCE_REGIONS] == ["united-states", "great-britain", "europe", "international"]
    cases = {
        "US-TX": "united-states",
        "US (conterminous)": "united-states",
        "GB": "great-britain",
        "GB-SCT": "great-britain",
        "EU/EEA": "europe",
        "DE": "europe",
        "global": "international",
        "AU": "international",
        None: "international",
        "Global (US clipped)": "united-states",  # a global layer loaded for the US only
        "EU/GB": "europe",
    }
    for jurisdiction, region in cases.items():
        assert source_region({"jurisdiction": jurisdiction}) == region, jurisdiction
    # No jurisdiction: YAML reads Norway's `NO` as false, so the register's id names the country, when its
    # first segment is a two-letter code; `mdb.` or `social.` is no country.
    assert source_region({"source_id": "no.sodir.factpages_co2_storage", "jurisdiction": None}) == "europe"
    assert source_region({"source_id": "no.sodir.factpages_co2_storage", "jurisdiction": False}) == "europe"
    assert source_region({"source_id": "gb.example", "jurisdiction": ""}) == "great-britain"
    assert source_region({"source_id": "social.bluesky", "jurisdiction": None}) == "international"


def test_about_counts_published_registers_by_region_each_a_link_to_its_table(
    about_client: TestClient,
) -> None:
    body = about_client.get("/about").text
    reach = body.split('id="sources"', 1)[1].split("</section>", 1)[0]
    figures = re.findall(
        r'<a class="about-reach__figure" href="([^"]+)"><span class="tnum">(\d+)</span> registers?'
        r'<span class="visually-hidden">, ([^<]+)</span></a>',
        reach,
    )
    assert figures == [
        ("/about/sources#united-states", "2", "United States"),  # PJM is listed but not published
        ("/about/sources#great-britain", "1", "Great Britain"),
        ("/about/sources#europe", "1", "Europe"),
        ("/about/sources#international", "1", "International"),
    ]
    for anchor in ('id="deals"', 'id="tiers"', 'id="who"'):
        assert anchor in body


def test_about_offers_build_a_deal_only_while_my_pipeline_is_on(
    about_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_PIPELINE", "on")
    assert "<dt>Build a deal</dt>" in about_client.get("/about").text
    monkeypatch.setenv("MY_PIPELINE", "off")
    assert "Build a deal" not in about_client.get("/about").text


def test_about_names_who_runs_it_when_the_entity_is_set(
    about_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "Inframation is run by a small team." in about_client.get("/about").text
    monkeypatch.setenv("SENDER_LEGAL_NAME", "Example Energy Data LLC")
    assert "Inframation is run by Example Energy Data LLC." in about_client.get("/about").text


def test_the_about_page_reads_every_page_of_the_register(transport: FakeTransport) -> None:
    def pages(params: Mapping[str, Any]) -> tuple[int, Any]:
        if params.get("cursor"):
            return 200, {"data": SOURCES[3:], "page": {"has_more": False}}
        return 200, {"data": SOURCES[:3], "page": {"has_more": True, "next_cursor": "c2"}}

    transport.responses["/v1/sources"] = pages
    web_app.state.api_client = ApiClient(transport)
    with TestClient(web_app) as c:
        body = c.get("/about").text
    assert 'href="/about/sources#international"' in body
    assert [p.get("cursor") for p in transport.params("/v1/sources")] == [None, "c2"]


def test_sources_page_groups_by_region_published_first_and_links_each_count(
    about_client: TestClient, transport: FakeTransport
) -> None:
    body = about_client.get("/about/sources").text
    order = re.findall(r'<h2 id="([a-z-]+)">', body)
    assert order == ["united-states", "great-britain", "europe", "international"]
    us = body.split('<h2 id="united-states">', 1)[1].split("</section>", 1)[0]
    # ERCOT backs 7 projects, a link to exactly those (every lifecycle state); the OSM grid is an asset
    # register, counted on its own list, so it gets a dash and no count call of its own.
    assert us.index("Register us.iso.ercot.gen_queue") < us.index("Register us.osm.power_grid")
    states = ",".join(ALL_PROPOSAL_LIFECYCLE_STATES).replace(",", "%2C")
    assert f'href="/proposals?source_id=us.iso.ercot.gen_queue&amp;lifecycle_state={states}">7</a>' in us
    assert '<code class="sources-table__id">us.osm.power_grid</code>' in us
    assert "1 more listed, not published" in us and "Register us.iso.pjm.gen_queue" in us
    counted = [p["source_id"] for u, p in transport.calls if u in ("/v1/proposals", "/v1/opportunities")]
    assert sorted(counted) == [
        "eu.ted.api",
        "gb.neso.tec_register",
        "mdb.worldbank.projects",
        "us.iso.ercot.gen_queue",
    ]
    intl = body.split('<h2 id="international">', 1)[1].split("</section>", 1)[0]
    assert (
        'href="/opportunities?source_id=mdb.worldbank.projects&amp;status=all&amp;relevance=all">3</a>'
        in intl
    )


NOT_REGISTERS = _sources(
    ("news.gdelt.doc", "global", "news", "public"),
    ("social.bluesky", "", "social_channel", "public"),
)


def test_news_and_social_channels_are_not_counted_as_a_regions_registers(transport: FakeTransport) -> None:
    """A news feed or one of the site's own channels is not a register: `/about` leaves them out of its
    figures, so each region's table on `/about/sources` still holds exactly what its figure counts. The
    news feeds follow the regions in a section of their own; the channels, where the site posts rather
    than where a fact comes from, are not listed there (`/attribution` lists every source)."""
    transport.responses["/v1/sources"] = (200, {"data": SOURCES + NOT_REGISTERS, "page": {"has_more": False}})
    transport.responses["/v1/proposals"] = (200, _page([], total=7))
    transport.responses["/v1/opportunities"] = (200, _page([], total=3))
    web_app.state.api_client = ApiClient(transport)
    web_app.state.__dict__.pop("source_records_cache", None)
    with TestClient(web_app) as c:
        about = c.get("/about").text
        sources = c.get("/about/sources").text
    figures = re.findall(
        r'<span class="tnum">(\d+)</span> registers?<span class="visually-hidden">, ([^<]+)<', about
    )
    assert figures == [
        ("2", "United States"),
        ("1", "Great Britain"),
        ("1", "Europe"),
        ("1", "International"),
    ]
    intl = sources.split('<h2 id="international">', 1)[1].split("</section>", 1)[0]
    assert "news.gdelt.doc" not in intl
    news = sources.split('<h2 id="news">News</h2>', 1)[1].split("</section>", 1)[0]
    assert "news.gdelt.doc" in news
    assert re.findall(r'<h2 id="([a-z-]+)">', sources)[-1] == "news"
    assert "social.bluesky" not in sources
    web_app.state.__dict__.pop("source_records_cache", None)


def test_the_attribution_page_credits_every_register_on_every_page(transport: FakeTransport) -> None:
    """`/v1/sources` pages by id. The page read one page of 100, which with 180 registers stopped at
    `us.ferc.elibrary`: no credit line for any ISO queue, LBNL's Queued Up or OpenStreetMap, although
    `/about/sources` and the glossary send a reader here for exactly those (review of 2026-10-23)."""
    transport.responses["/v1/sources"] = _paged_sources(SOURCES, 3)
    web_app.state.api_client = ApiClient(transport)
    with TestClient(web_app) as c:
        body = c.get("/attribution").text
    for source in SOURCES:
        assert f">{source['source_id']}</span>" in body, source["source_id"]
    assert [p.get("cursor") for p in transport.params("/v1/sources")] == [None, "c2"]


def test_the_register_counts_are_kept_between_loads_and_a_failed_count_is_not(
    about_client: TestClient, transport: FakeTransport
) -> None:
    """One count read per published register on a cold page (the API has no grouped count); a second
    load within `SOURCE_RECORDS_CACHE_SECONDS` reads none, and a count that failed is asked again."""

    def counted() -> list[str]:
        return [p["source_id"] for u, p in transport.calls if u in ("/v1/proposals", "/v1/opportunities")]

    about_client.get("/about/sources")
    assert len(counted()) == 4
    transport.calls.clear()
    assert "lifecycle_state=" in about_client.get("/about/sources").text
    assert counted() == []

    web_app.state.__dict__.pop("source_records_cache", None)
    transport.responses["/v1/opportunities"] = (503, {"title": "unavailable"})
    about_client.get("/about/sources")
    transport.calls.clear()
    about_client.get("/about/sources")
    assert sorted(counted()) == ["eu.ted.api", "mdb.worldbank.projects"]  # the two that failed, again


def test_the_old_about_anchors_still_answer(about_client: TestClient) -> None:
    """docs/30 §11.10: `/about#sources` (the footer, the RFP list, Pricing), `#tiers` and `#deals`."""
    body = about_client.get("/about").text
    sources = body.split('id="sources"', 1)[1][:2500]
    assert 'href="/about/sources"' in sources
