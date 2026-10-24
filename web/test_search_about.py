"""Search tabs, About and Sources & licences (Navigation v2 lane V7; docs/30 §11.4, §11.10).

Search: one field and one kind at a time (lane stacked-lists, 2026-10-24): every kind with its count in a
fixed order (Companies · Projects · Infrastructure · RFPs · Funding), the page opening on the kind with the
most matches, one list under a head that names its kind and count, a top hit of another kind above it when
the words start a company or asset name, a kind that fails costing only itself, and every full-list link
naming exactly the set its count counted. About: where the register reaches, US first, each region's figure
the number of published registers its table on /about/sources lists. Sources & licences: by region,
published registers first by records, each count a link to the list that is that count.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
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
    search_opening_kind,
    search_top_hit,
    source_region,
)
from web.app import app as web_app
from web.sitemaps import SITEMAP_STATIC_PATHS, _serves
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


def _kinds(html: str) -> list[tuple[str | None, str, bool]]:
    """The kinds down the left (a row of chips on phones): (href, words, current) for each, in order; a
    kind with no match is words and a zero, not a link, so its href is None."""
    nav = html.split('<nav class="search-kinds"', 1)[1].split("</nav>", 1)[0]
    out: list[tuple[str | None, str, bool]] = []
    for item in re.findall(r"<li>(.*?)</li>", nav, re.S):
        link = re.match(r'\s*<a href="([^"]+)"( aria-current="page")?>', item)
        words = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", item)).strip()
        href = link.group(1).replace("&amp;", "&") if link else None
        out.append((href, words, bool(link and link.group(2))))
    return out


def _tables(html: str) -> int:
    return html.count('<table class="record-table record-table--compact search-table')


# ------------------------------------------------------------------------------------------- search
def test_search_with_no_words_reads_nothing_and_offers_the_glossary(
    client: TestClient, transport: FakeTransport
) -> None:
    body = client.get("/search").text
    assert "<h1>Search</h1>" in body and 'href="/glossary"' in body
    assert not [u for u, _ in transport.calls if u not in ("/v1/health", "/v1/coverage")]


def test_the_search_field_and_its_hints_name_no_company() -> None:
    """Owner, 2026-10-24: no fixed example company names in a placeholder, a hint or an empty state (one
    asked to lose its place as the example). The field says what it searches: names, companies and queue
    IDs, which is what `?q=` matches (services/api/records.py); a county or state is the map's to find."""
    template = (Path(__file__).resolve().parent / "templates" / "search.html").read_text(encoding="utf-8")
    placeholders = re.findall(r'placeholder="([^"]*)"', template)
    assert placeholders == ["Name, company or queue ID"]
    assert "e.g." not in template and "Tallgrass" not in template


def test_the_kinds_come_in_one_order_with_counts_and_the_page_opens_on_the_most_matches(
    client: TestClient,
) -> None:
    """One kind at a time (owner, 2026-10-24: "different stacked lists are really confusing"): every kind
    with its count, in the fixed order, and the page opens on the one with the most matches. A kind with
    none is a word and a zero, not a link to an empty list."""
    kinds = _kinds(client.get("/search", params={"q": "sun"}).text)
    assert [(words, current) for _, words, current in kinds] == [
        ("Companies 1", False),
        ("Projects 60", True),
        ("Infrastructure 1", False),
        ("RFPs 1", False),
        ("Funding 0", False),
    ]
    assert [href for href, _, _ in kinds] == [
        "/search?q=sun&type=companies",
        "/search?q=sun&type=projects",
        "/search?q=sun&type=infrastructure",
        "/search?q=sun&type=rfps",
        None,
    ]
    on_rfps = _kinds(client.get("/search", params={"q": "sun", "type": "rfps"}).text)
    assert [words for _, words, current in on_rfps if current] == ["RFPs 1"]


def test_the_page_shows_one_list_under_a_head_that_names_its_kind_and_count(client: TestClient) -> None:
    """Never two kinds' lists stacked: one table, its head naming the kind and the count, its caption the
    rows shown of that count, so a screen reader hears what the rows are and how many there are."""
    body = client.get("/search", params={"q": "sun"}).text
    assert _tables(body) == 1 and "search-bucket" not in body
    head = body.split('<div class="search-list__head">', 1)[1].split("</div>", 1)[0]
    assert re.sub(r"<[^>]+>", "", head.split("</h2>", 1)[0]).split() == ["Projects", "60", "for", "“sun”"]
    assert '<caption class="visually-hidden">Projects matching “sun”: 50 of 60</caption>' in body
    columns = re.findall(r'<th scope="col"[^>]*>([^<]+)</th>', body)
    assert columns == ["Status", "Project", "Technology", "Where", "MW", "Source"]


def test_the_page_reads_five_of_each_kind_to_count_it_and_fifty_of_the_kind_it_opens(
    client: TestClient, transport: FakeTransport
) -> None:
    body = client.get("/search", params={"q": "sun"}).text
    assert body.count('class="search-row"') == 50
    reads = transport.params("/v1/proposals")
    assert reads[0] == {"q": "sun", "limit": 5, "include": "count"}
    assert [p["limit"] for p in reads] == [5, 50]
    assert [p["limit"] for p in transport.params("/v1/organizations")] == [5]
    link = re.search(r'<a class="search-list__all" href="([^"]+)">Open all 60 in Projects</a>', body)
    assert link is not None
    # The full list names every lifecycle state, so it is not narrowed to the list's own default view
    # (active, listed): it shows exactly the 60 the kind counted.
    query = parse_qs(urlsplit(link.group(1).replace("&amp;", "&")).query)
    assert query == {"q": ["sun"], "lifecycle_state": [",".join(ALL_PROPOSAL_LIFECYCLE_STATES)]}

    transport.calls.clear()
    tab = client.get("/search", params={"q": "sun", "type": "projects"}).text
    assert tab.count('class="search-row"') == 50
    assert [p["limit"] for p in transport.params("/v1/proposals")] == [50]
    assert 'href="' + link.group(1) + '">Open all 60 in Projects</a>' in tab


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


def test_a_kind_with_no_match_is_one_word_and_a_zero_not_a_heading(client: TestClient) -> None:
    body = client.get("/search", params={"q": "sun"}).text
    assert "<h2>Funding</h2>" not in body and 'id="search-list-title">Funding' not in body
    zero = '<span class="search-kinds__none">Funding <span class="search-kinds__n tnum">0</span></span>'
    assert zero in body


def test_nothing_matching_says_so_once_and_opens_no_empty_list(
    client: TestClient, transport: FakeTransport
) -> None:
    for path in ("/v1/organizations", "/v1/proposals", "/v1/assets", "/v1/opportunities"):
        transport.responses[path] = (200, _page([], total=0))
    body = client.get("/search", params={"q": "zzz"}).text
    assert _tables(body) == 0 and 'id="search-list-title"' not in body
    none = re.search(r'<p class="search-empty search-none" role="status">(.*?)</p>', body, re.S)
    assert none is not None and "Nothing matches “zzz”" in none.group(1) and 'href="/"' in none.group(1)
    assert [href for href, _, _ in _kinds(body)] == [None] * 5


def test_search_opening_kind_rules() -> None:
    def kind(key: str, total: int, error: bool = False) -> dict[str, Any]:
        return {"key": key, "total": total, "error": error}

    assert search_opening_kind([kind("companies", 3), kind("projects", 8)]) == "projects"
    # A tie goes to the fixed order: companies first.
    tie = [kind("companies", 1), kind("infrastructure", 1), kind("rfps", 1)]
    assert search_opening_kind(tie) == "companies"
    # A kind that could not be searched is never opened on its own; nothing matched opens nothing.
    assert search_opening_kind([kind("companies", 0), kind("projects", 0, error=True)]) is None
    assert search_opening_kind([kind("rfps", 2), kind("projects", 0, error=True)]) == "rfps"


def test_a_company_name_is_the_top_hit_and_its_open_button_the_one_primary(client: TestClient) -> None:
    body = client.get("/search", params={"q": "sun power"}).text
    hit = body.split('<section class="top-hit"', 1)[1].split("</section>", 1)[0]
    assert '<a href="/organizations/sun-power-partners">Sun Power Partners</a>' in hit
    assert '<span class="top-hit__kind">Developer</span>' in hit  # its kind, in words, beside its name
    assert '<a class="top-hit__open" href="/organizations/sun-power-partners">Open company</a>' in hit
    assert hit and body.index('class="top-hit"') < body.index('id="search-list-title"')
    assert (
        "top-hit"
        not in client.get("/search", params={"q": "sun", "type": "companies"}).text.split("<main", 1)[1]
    )


def test_a_top_hit_of_the_open_kind_is_one_of_its_rows_not_a_second_copy(
    client: TestClient, transport: FakeTransport
) -> None:
    transport.responses["/v1/proposals"] = (200, _page([], total=0))
    body = client.get("/search", params={"q": "sun power"}).text
    assert [words for _, words, current in _kinds(body) if current] == ["Companies 1"]
    assert 'class="top-hit"' not in body
    assert body.count("/organizations/sun-power-partners") == 1


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
    assert ("/search?q=sun&type=projects", "Projects —", False) in _kinds(resp.text)
    assert "Sun Power Partners" in resp.text
    tab = client.get("/search", params={"q": "sun", "type": "projects"}).text
    assert "Projects could not be searched just now." in tab and _tables(tab) == 0


def test_an_unread_kind_with_no_other_match_says_which_kinds_were_searched(
    client: TestClient, transport: FakeTransport
) -> None:
    """Reviewer, 2026-10-24: with Projects unread and every other kind empty, the page said "Nothing
    matches" and then that Projects could not be searched; the first line claimed more than the page knew.
    It names the kinds it searched, as the page before lane stacked-lists did."""
    for path in ("/v1/organizations", "/v1/assets", "/v1/opportunities"):
        transport.responses[path] = (200, _page([], total=0))
    transport.responses["/v1/proposals"] = (503, {"title": "unavailable"})
    body = client.get("/search", params={"q": "zzz"}).text
    assert "Nothing matches" not in body
    none = re.search(r'<p class="search-empty search-none" role="status">(.*?)</p>', body, re.S)
    assert none is not None and none.group(1) == "No match in Companies, Infrastructure, RFPs or Funding."
    assert "Projects could not be searched just now." in body
    assert body.index("No match in") < body.index("Projects could not be searched")
    # Nothing read at all: no claim about matches, only what could not be searched.
    for path in ("/v1/organizations", "/v1/assets", "/v1/opportunities"):
        transport.responses[path] = (503, {"title": "unavailable"})
    body = client.get("/search", params={"q": "zzz"}).text
    assert "search-none" not in body and "Nothing matches" not in body
    assert body.count("could not be searched just now.") == 5


def test_a_client_error_is_a_bug_and_stays_loud(client: TestClient, transport: FakeTransport) -> None:
    transport.responses["/v1/proposals"] = (400, {"title": "bad_request"})
    with pytest.raises(ApiError):
        client.get("/search", params={"q": "sun"})


def test_an_unknown_type_opens_the_kind_with_the_most_matches(client: TestClient) -> None:
    kinds = _kinds(client.get("/search", params={"q": "sun", "type": "nonsense"}).text)
    assert [words for _, words, current in kinds if current] == ["Projects 60"]


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


# ------------------------------------------------------------------------- old links, the sitemap
#: Every anchor lane V7 moved off `/about` and `/methodology`, and where its content now is (docs/30
#: §11.10). The old page keeps the id on a link onward; site.js sends a reader who arrives on it there.
MOVED_ANCHORS = {
    "/about#sources": "/about/sources",
    "/methodology#absences": "/methodology/gaps#absences",
    "/methodology#absent-technologies": "/methodology/gaps#absent-technologies",
    "/methodology#withheld": "/methodology/gaps#withheld",
    "/methodology#ownership": "/methodology/gaps#ownership",
    "/methodology#asset-sources": "/methodology/gaps#asset-sources",
    "/methodology#absent-states": "/methodology/gaps#absent-states",
    "/methodology#states": "/methodology/status-words#states",
    "/methodology#lifecycle": "/methodology/status-words#lifecycle",
    "/methodology#opportunity-statuses": "/methodology/status-words#opportunity-statuses",
}
#: Ids named by their data (a method note, an asset type, a status key), moved as a family.
MOVED_FAMILIES = {
    "/methodology#note-": "/methodology/gaps#note-",
    "/methodology#asset-sources-": "/methodology/gaps#asset-sources-",
    "/methodology#state-": "/methodology/status-words#state-",
}


def test_site_js_moves_each_old_about_and_methodology_anchor_to_its_new_page() -> None:
    script = (Path(__file__).resolve().parent / "static" / "js" / "site.js").read_text(encoding="utf-8")
    rows = script.split("var MOVED_FRAGMENTS = {", 1)[1].split("};", 1)[0]
    families = script.split("var MOVED_PREFIXES = {", 1)[1].split("};", 1)[0]
    for old, new in MOVED_ANCHORS.items():
        assert f'"{old}": "{new}"' in rows, old
    for old, new in MOVED_FAMILIES.items():
        assert f'"{old}": "{new}"' in families, old
    # Lane V3's rule, for every row: a reader is moved only to a page the one they are on links.
    guard = "document.querySelector('a[href=\"' + movedTo.split(\"#\")[0] + '\"]')) movedTo = null;"
    assert f"if (movedTo && !{guard}" in script


def test_the_sitemap_lists_every_reference_page_and_only_routes_the_site_serves(client: TestClient) -> None:
    web_app.state.__dict__.pop("sitemap_cache", None)
    locs = re.findall(r"<loc>http://testserver([^<]+)</loc>", client.get("/sitemap.xml").text)
    reference = [
        "/about",
        "/about/sources",
        "/methodology",
        "/methodology/gaps",
        "/methodology/status-words",
        "/glossary",
        "/attribution",
        "/docs/api",
    ]
    assert [loc for loc in locs if loc in reference] == reference
    assert [path for path in SITEMAP_STATIC_PATHS if not _serves(web_app, path)] == []
    web_app.state.__dict__.pop("sitemap_cache", None)
