"""Tests for the navigation and discoverability lane (`docs/00-PLAN.md` 2026-09-19 item 4, closing
the `docs/50-audit-2026-09-18.md` §3.2 web bullet "no Open Graph or structured data"):

  - the `/assets` and `/organizations` index pages,
  - Open Graph / Twitter card / canonical meta on every public page,
  - JSON-LD (`BreadcrumbList`, `Organization`, `ItemList`),
  - `robots.txt`,
  - the sitemap once assets and companies push it past one file.

Same pattern as `web/test_asset_pages.py` and `web/test_map_layers.py`: `web.app.app` driven
against a hand-written fake `Transport` (`web/api_client.py`'s `Transport` protocol), no database
and no `services.api` import. The fake is duplicated rather than imported, per this repo's
convention that no `web/test_*.py` imports another one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app

#: A name carrying every character that could break an HTML attribute or close a `<script>` block
#: early. Task item 7 asks for exactly this to be pushed through the meta tags and the JSON-LD.
HOSTILE_NAME = 'Acme "Big" <Energy> & Co </script><script>alert(1)</script>'

DEFAULT_HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 14, "opportunities": 7},
}
DEFAULT_VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
    }
}
EMPTY_PAGE: dict[str, Any] = {"data": [], "page": {"has_more": False, "next_cursor": None}}


class FakeTransport:
    """Canned responses keyed by path, every call recorded so a test can assert what a route
    forwarded. A response may be a callable, so a path can answer differently per call."""

    def __init__(self, responses: Mapping[str, Any] | None = None) -> None:
        self.responses: dict[str, Any] = dict(responses or {})
        self.calls: list[tuple[str, str, Any, dict[str, str] | None]] = []

    def _respond(self, url: str) -> httpx.Response:
        canned = self.responses.get(url)
        if canned is None:
            return httpx.Response(404, json={"title": "not_found", "detail": url})
        if callable(canned):
            # The call was recorded before `_respond` ran, so "how many came before this one"
            # is one less than what the log holds.
            canned = canned(max(len([c for c in self.calls if c[1] == url]) - 1, 0))
        status, body = canned
        return httpx.Response(status, json=body)

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("GET", url, dict(params or {}), cookies))
        return self._respond(url)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("POST", url, json, cookies))
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
        self.calls.append((method, url, json if json is not None else dict(params or {}), cookies))
        return self._respond(url)

    def close(self) -> None:
        pass


def _transport(extra: Mapping[str, Any] | None = None) -> FakeTransport:
    return FakeTransport(
        {
            "/v1/health": (200, DEFAULT_HEALTH),
            "/v1/meta/vocabularies": (200, DEFAULT_VOCAB),
            "/v1/sources": (200, {"data": []}),
            **(extra or {}),
        }
    )


@pytest.fixture()
def web_client() -> Iterator[TestClient]:
    _reset_caches()
    with TestClient(web_app) as client:
        yield client
    _reset_caches()


def _reset_caches() -> None:
    """Every cache this lane's routes keep lives on `app.state` and would otherwise leak one
    test's canned data into the next (the sitemap cache is keyed on base URL, and every test
    here shares `http://testserver`)."""
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


def _install(transport: FakeTransport) -> None:
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None


# ---------------------------------------------------------------- fixtures shaped like the API
def _asset(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "asset_01JBQ7Z8KD",
        "slug": "roscoe-wind-farm-tx",
        "name": "Roscoe Wind Farm",
        "asset_type": "power_plant",
        "status": "operating",
        "operator_name": "Roscoe Wind Farm LLC",
        "technology": "wind",
        "capacity_mw": 781.5,
        "state_code": "US-TX",
        "county_name": "Nolan",
        "country": "US",
        "attributes": {},
        "provenance": [
            {
                "source_id": "us.eia.860m",
                "source_name": "EIA-860M",
                "source_url": "https://www.eia.gov/electricity/data/eia860m/",
                "retrieved_at": "2026-09-01T00:00:00Z",
                "reuse_class": "open",
                "source_record_id": "6452",
            }
        ],
    }
    base.update(overrides)
    return base


def _org(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "org_01JBQ8C4X1",
        "slug": "tallgrass-energy",
        "name_canonical": "Tallgrass Energy",
        "type": "other",
        "country": "US",
        "jurisdiction": None,
        "ids": {},
        "website": None,
        "provenance": [],
    }
    base.update(overrides)
    return base


def _geo_totals(counts: Mapping[str, int]) -> tuple[int, dict[str, Any]]:
    return (
        200,
        {
            "data": {
                "type": "FeatureCollection",
                "features": [],
                "totals": {"records": sum(counts.values()), "asset_type_counts": dict(counts)},
            },
            "meta": {},
        },
    )


def _page(rows: list[dict[str, Any]], *, has_more: bool = False, cursor: str | None = None) -> Any:
    return (200, {"data": rows, "page": {"has_more": has_more, "next_cursor": cursor}, "meta": {}})


def _asset_detail_transport(entity: Mapping[str, Any]) -> FakeTransport:
    """`/assets/{slug}` resolves the slug on the list endpoint, then re-reads the record on its
    own detail endpoint for owners and geometry (web/app.py::asset_detail)."""
    return _transport(
        {
            "/v1/assets": _page([dict(entity)]),
            f"/v1/assets/{entity['public_id']}": (200, {"data": dict(entity)}),
        }
    )


def _jsonld_graphs(body: str) -> list[Any]:
    return [
        json.loads(text)
        for text in re.findall(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)
    ]


def _meta(body: str, attr: str, value: str) -> str | None:
    match = re.search(rf'<meta {attr}="{re.escape(value)}" content="([^"]*)">', body)
    return match.group(1) if match else None


# =============================================================================== /assets index
def test_assets_index_lists_rows_with_type_counts_and_pagination(web_client: TestClient) -> None:
    transport = _transport(
        {
            "/v1/assets": _page([_asset(), _asset(slug="bowen", name="Bowen")], has_more=True, cursor="c2"),
            "/v1/assets/geo": _geo_totals({"power_plant": 14659, "gas_pipeline": 259}),
        }
    )
    _install(transport)

    resp = web_client.get("/assets")

    assert resp.status_code == 200
    body = resp.text
    assert '<a class="row-link" href="/assets/roscoe-wind-farm-tx">Roscoe Wind Farm</a>' in body
    assert '<a class="row-link" href="/assets/bowen">Bowen</a>' in body
    # Counts per type at the top, largest first, plus an "All types" total.
    assert "14,659" in body and "259" in body and "14,918" in body
    assert 'href="/assets?asset_type=power_plant"' in body
    # Same pager macro the proposals list uses, pointing back at /assets.
    assert 'cursor=c2" hx-get="/assets?cursor=c2"' in body


def test_assets_index_forwards_type_state_and_query_and_defaults_to_capacity_sort(
    web_client: TestClient,
) -> None:
    transport = _transport(
        {"/v1/assets": _page([_asset()]), "/v1/assets/geo": _geo_totals({"power_plant": 1})}
    )
    _install(transport)

    resp = web_client.get("/assets", params={"asset_type": "power_plant", "state": "US-CO", "q": "rex"})

    assert resp.status_code == 200
    params = next(c[2] for c in transport.calls if c[1] == "/v1/assets")
    assert params == {
        "asset_type": "power_plant",
        "state": "US-CO",
        "q": "rex",
        "sort": "-capacity_mw",
    }


def test_assets_index_falls_back_to_the_default_sort_for_a_token_the_api_rejects(
    web_client: TestClient,
) -> None:
    """`services/api/assets.py::ASSET_SORT_ALLOWLIST` 400s an unknown sort field, so the page
    never forwards one: an arbitrary `?sort=` in a crawled URL must not 500 the index."""
    transport = _transport(
        {"/v1/assets": _page([_asset()]), "/v1/assets/geo": _geo_totals({"power_plant": 1})}
    )
    _install(transport)

    resp = web_client.get("/assets", params={"sort": "length_miles"})

    assert resp.status_code == 200
    params = next(c[2] for c in transport.calls if c[1] == "/v1/assets")
    assert params["sort"] == "-capacity_mw"


def test_assets_index_empty_state_mirrors_the_proposals_list(web_client: TestClient) -> None:
    transport = _transport({"/v1/assets": _page([]), "/v1/assets/geo": _geo_totals({})})
    _install(transport)

    resp = web_client.get("/assets", params={"q": "nothing"})

    assert resp.status_code == 200
    assert '<div class="empty-state">' in resp.text
    assert "No assets match these filters." in resp.text
    assert 'href="/assets">Clear all filters</a>' in resp.text


def test_assets_index_renders_without_counts_when_the_map_totals_call_fails(
    web_client: TestClient,
) -> None:
    """The by-type counts are a nicety read from a second endpoint; losing them must cost the
    chips and nothing else."""
    transport = _transport({"/v1/assets": _page([_asset()]), "/v1/assets/geo": (500, {"title": "boom"})})
    _install(transport)

    resp = web_client.get("/assets")

    assert resp.status_code == 200
    assert "count-chips" not in resp.text
    assert "Roscoe Wind Farm" in resp.text


def test_assets_index_type_counts_are_fetched_once_and_cached(web_client: TestClient) -> None:
    transport = _transport(
        {"/v1/assets": _page([_asset()]), "/v1/assets/geo": _geo_totals({"power_plant": 3})}
    )
    _install(transport)

    web_client.get("/assets")
    web_client.get("/assets", params={"state": "US-TX"})

    assert len([c for c in transport.calls if c[1] == "/v1/assets/geo"]) == 1


# ========================================================================= /organizations index
def test_organizations_index_shows_descriptor_and_holdings_from_the_shared_helpers(
    web_client: TestClient,
) -> None:
    """`org_descriptor()` and `_org_summary_parts()` are the ownership lane's helpers; the index
    reuses them, so a row reads exactly as the company page's header and summary do."""
    counts = {"by_role_and_type": {"operator": {"gas_pipeline": 6}, "owner": {"gas_processing_plant": 2}}}
    transport = _transport(
        {
            "/v1/organizations": _page([_org()]),
            "/v1/organizations/org_01JBQ8C4X1": (200, {"data": {**_org(), "group_asset_counts": counts}}),
        }
    )
    _install(transport)

    resp = web_client.get("/organizations")

    assert resp.status_code == 200
    body = resp.text
    assert '<a class="row-link" href="/organizations/tallgrass-energy">Tallgrass Energy</a>' in body
    assert "Gas pipeline operator and Gas processing operator" in body
    assert "Operates 6 gas pipelines · Owns 2 gas processing plants" in body
    assert ">8<" in body  # the assets column: 6 + 2


def test_organizations_index_search_forwards_q_and_shows_its_own_empty_state(
    web_client: TestClient,
) -> None:
    transport = _transport({"/v1/organizations": _page([])})
    _install(transport)

    resp = web_client.get("/organizations", params={"q": "nobody"})

    assert resp.status_code == 200
    params = next(c[2] for c in transport.calls if c[1] == "/v1/organizations")
    assert params["q"] == "nobody"
    assert params["limit"] == "25"
    assert "No companies match this search." in resp.text


def test_organizations_index_row_survives_a_failed_counts_call(web_client: TestClient) -> None:
    transport = _transport(
        {
            "/v1/organizations": _page([_org(type="developer")]),
            "/v1/organizations/org_01JBQ8C4X1": (500, {"title": "boom"}),
        }
    )
    _install(transport)

    resp = web_client.get("/organizations")

    assert resp.status_code == 200
    assert "Tallgrass Energy" in resp.text
    assert "developer" in resp.text  # the stored type, since no counts came back
    assert ">0<" not in resp.text  # never a fabricated zero


# ===================================================================== Open Graph / canonical
PUBLIC_PAGES = ("/", "/proposals", "/opportunities", "/assets", "/organizations", "/about", "/attribution")


def _full_transport() -> FakeTransport:
    return _transport(
        {
            "/v1/proposals": (
                200,
                {
                    "data": [],
                    "page": {"has_more": False, "next_cursor": None, "prev_cursor": None},
                    "meta": {"total": 0, "total_is_estimate": False},
                },
            ),
            "/v1/opportunities": (
                200,
                {
                    "data": [],
                    "page": {"has_more": False, "next_cursor": None, "prev_cursor": None},
                    "meta": {"total": 0, "total_is_estimate": False},
                },
            ),
            "/v1/assets": _page([]),
            "/v1/assets/geo": _geo_totals({"power_plant": 2}),
            "/v1/organizations": _page([]),
        }
    )


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_every_public_page_carries_open_graph_twitter_and_canonical(
    web_client: TestClient, path: str
) -> None:
    _install(_full_transport())

    resp = web_client.get(path)

    assert resp.status_code == 200
    body = resp.text
    title = re.search(r"<title>(.*?)</title>", body)
    assert title is not None and title.group(1).strip()
    description = _meta(body, "name", "description")
    assert description and len(description) > 20
    assert _meta(body, "property", "og:title") == title.group(1)
    assert _meta(body, "property", "og:description") == description
    assert _meta(body, "property", "og:site_name") == "Infraque"
    assert _meta(body, "property", "og:type") == "website"
    assert _meta(body, "property", "og:url") == f"http://testserver{path}"
    assert _meta(body, "name", "twitter:card") == "summary"
    assert _meta(body, "name", "twitter:title") == title.group(1)
    assert _meta(body, "name", "twitter:description") == description
    assert f'<link rel="canonical" href="http://testserver{path}">' in body
    # No image is promised, and nothing may reach out to a third-party host for one.
    assert "og:image" not in body


def test_canonical_url_keeps_the_pages_own_filters_and_drops_anything_else(
    web_client: TestClient,
) -> None:
    """A crawled URL carrying a campaign parameter must canonicalise to the clean filtered one,
    and two URLs differing only in parameter order must agree."""
    _install(_full_transport())

    resp = web_client.get(
        "/assets", params={"utm_source": "x", "state": "US-TX", "asset_type": "power_plant"}
    )

    assert resp.status_code == 200
    assert (
        '<link rel="canonical" href="http://testserver/assets?asset_type=power_plant&amp;state=US-TX">'
        in resp.text
    )


def test_a_record_page_canonicalises_to_its_slug_not_the_requested_url(
    web_client: TestClient,
) -> None:
    _install(_asset_detail_transport(_asset()))

    resp = web_client.get("/assets/roscoe-wind-farm-tx", params={"utm_campaign": "email"})

    assert resp.status_code == 200
    assert '<link rel="canonical" href="http://testserver/assets/roscoe-wind-farm-tx">' in resp.text


def test_auth_pages_and_search_results_are_noindex(web_client: TestClient) -> None:
    _install(_full_transport())

    assert 'name="robots" content="noindex, nofollow"' in web_client.get("/login").text
    assert 'name="robots" content="noindex, follow"' in web_client.get("/search?q=wind").text
    # The bare search page is in the sitemap, so it stays indexable.
    assert 'name="robots" content="index, follow"' in web_client.get("/search").text


# ============================================================================ JSON-LD (task 4)
def test_record_pages_emit_a_breadcrumb_list(web_client: TestClient) -> None:
    _install(_asset_detail_transport(_asset()))

    graphs = _jsonld_graphs(web_client.get("/assets/roscoe-wind-farm-tx").text)

    crumbs = next(g for g in graphs if g["@type"] == "BreadcrumbList")
    assert crumbs["@context"] == "https://schema.org"
    assert [(i["position"], i["name"], i["item"]) for i in crumbs["itemListElement"]] == [
        (1, "Home", "http://testserver/"),
        (2, "Assets", "http://testserver/assets"),
        (3, "Roscoe Wind Farm", "http://testserver/assets/roscoe-wind-farm-tx"),
    ]
    assert all(item["@type"] == "ListItem" for item in crumbs["itemListElement"])


def test_company_page_organization_jsonld_emits_only_stored_fields(web_client: TestClient) -> None:
    entity = _org(
        ids={"lei": "5493001KJTIIGC8Y1R12", "cik": None},
        website="https://www.tallgrassenergy.com",
        parent={"public_id": "org_PARENT", "slug": "blackstone", "name_canonical": "Blackstone"},
    )
    _install(
        _transport(
            {
                "/v1/organizations": _page([entity]),
                "/v1/organizations/org_01JBQ8C4X1/assets": _page([]),
                "/v1/organizations/org_01JBQ8C4X1/proposals": _page([]),
                "/v1/organizations/org_01JBQ8C4X1/opportunities": _page([]),
                "/v1/organizations/org_01JBQ8C4X1/nearby-proposals": _page([]),
            }
        )
    )

    graphs = _jsonld_graphs(web_client.get("/organizations/tallgrass-energy").text)

    org = next(g for g in graphs if g["@type"] == "Organization")
    assert org["name"] == "Tallgrass Energy"
    assert org["url"] == "http://testserver/organizations/tallgrass-energy"
    assert org["sameAs"] == ["https://www.tallgrassenergy.com"]
    assert org["address"] == {"@type": "PostalAddress", "addressCountry": "US"}
    assert org["parentOrganization"] == {
        "@type": "Organization",
        "name": "Blackstone",
        "url": "http://testserver/organizations/blackstone",
    }
    # Only the identifier actually stored; the null `cik` is not emitted as an empty PropertyValue.
    assert org["identifier"] == [
        {"@type": "PropertyValue", "propertyID": "LEI", "value": "5493001KJTIIGC8Y1R12"}
    ]
    assert "numberOfEmployees" not in org and "foundingDate" not in org
    assert any(g["@type"] == "BreadcrumbList" for g in graphs)


def test_index_pages_emit_an_item_list_of_the_rows_they_render(web_client: TestClient) -> None:
    _install(
        _transport(
            {
                "/v1/assets": _page([_asset(), _asset(slug="bowen", name="Bowen")]),
                "/v1/assets/geo": _geo_totals({"power_plant": 17352}),
            }
        )
    )

    graphs = _jsonld_graphs(web_client.get("/assets").text)

    item_list = next(g for g in graphs if g["@type"] == "ItemList")
    assert item_list["name"] == "Assets"
    assert item_list["url"] == "http://testserver/assets"
    assert item_list["numberOfItems"] == 17352
    assert [(i["position"], i["name"], i["url"]) for i in item_list["itemListElement"]] == [
        (1, "Roscoe Wind Farm", "http://testserver/assets/roscoe-wind-farm-tx"),
        (2, "Bowen", "http://testserver/assets/bowen"),
    ]


def test_filtered_index_omits_number_of_items_rather_than_quoting_the_corpus_total(
    web_client: TestClient,
) -> None:
    _install(
        _transport({"/v1/assets": _page([_asset()]), "/v1/assets/geo": _geo_totals({"power_plant": 17352})})
    )

    graphs = _jsonld_graphs(web_client.get("/assets?state=US-TX").text)

    item_list = next(g for g in graphs if g["@type"] == "ItemList")
    assert "numberOfItems" not in item_list


# ================================================ task item 7: a hostile name through both paths
def test_name_with_a_quote_and_an_angle_bracket_breaks_neither_meta_tags_nor_jsonld(
    web_client: TestClient,
) -> None:
    entity = _asset(name=HOSTILE_NAME)
    _install(_asset_detail_transport(entity))

    resp = web_client.get("/assets/roscoe-wind-farm-tx")

    assert resp.status_code == 200
    body = resp.text
    # 1. Nothing injected: the payload's own script tags never reach the document as markup.
    assert "<script>alert(1)</script>" not in body
    assert body.count("<script") == body.count("</script>")
    # 2. The meta tags carry the name escaped exactly once -- not `&amp;#34;`, which is what a
    #    second escaping pass would produce and what a card renderer would show literally.
    og_title = _meta(body, "property", "og:title")
    assert og_title is not None
    assert "&#34;Big&#34;" in og_title and "&lt;Energy&gt;" in og_title
    assert "&amp;#34;" not in body and "&amp;lt;" not in body
    # 3. The JSON-LD still parses, and round-trips to the original characters.
    crumbs = next(g for g in _jsonld_graphs(body) if g["@type"] == "BreadcrumbList")
    assert crumbs["itemListElement"][2]["name"] == HOSTILE_NAME
    # 4. The emitted JSON itself contains no markup characters at all.
    raw = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)
    assert raw is not None
    assert "<" not in raw.group(1) and ">" not in raw.group(1) and "&" not in raw.group(1)


def test_a_hostile_name_on_an_index_row_is_safe_in_the_item_list(web_client: TestClient) -> None:
    _install(
        _transport(
            {
                "/v1/assets": _page([_asset(name=HOSTILE_NAME)]),
                "/v1/assets/geo": _geo_totals({"power_plant": 1}),
            }
        )
    )

    body = web_client.get("/assets").text

    assert "<script>alert(1)</script>" not in body
    item_list = next(g for g in _jsonld_graphs(body) if g["@type"] == "ItemList")
    assert item_list["itemListElement"][0]["name"] == HOSTILE_NAME


# ==================================================================================== robots.txt
def test_robots_txt_allows_public_pages_blocks_private_ones_and_names_the_sitemap(
    web_client: TestClient,
) -> None:
    _install(_transport())

    resp = web_client.get("/robots.txt")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    lines = resp.text.splitlines()
    assert lines[0] == "User-agent: *"
    assert "Allow: /" in lines
    for blocked in ("/admin", "/api/", "/login", "/register", "/logout", "/account", "/verify"):
        assert f"Disallow: {blocked}" in lines
    assert "Sitemap: http://testserver/sitemap.xml" in lines
    # The public surface itself is never disallowed.
    for public in ("/proposals", "/opportunities", "/assets", "/organizations", "/about"):
        assert f"Disallow: {public}" not in lines


# ====================================================================================== sitemap
def _many(prefix: str, count: int) -> list[dict[str, Any]]:
    return [{"slug": f"{prefix}-{n}"} for n in range(count)]


def test_sitemap_includes_assets_and_organizations(web_client: TestClient) -> None:
    _install(
        _transport(
            {
                "/v1/proposals": _page([{"slug": "prop-a"}]),
                "/v1/opportunities": _page([{"slug": "opp-a"}]),
                "/v1/assets": _page([{"slug": "asset-a"}]),
                "/v1/organizations": _page([{"slug": "org-a"}]),
            }
        )
    )

    body = web_client.get("/sitemap.xml").text

    assert "<urlset" in body
    for loc in ("/assets/asset-a", "/organizations/org-a", "/proposals/prop-a", "/opportunities/opp-a"):
        assert f"<loc>http://testserver{loc}</loc>" in body
    # The index pages themselves are listed, so a crawler has a path to every record page.
    assert "<loc>http://testserver/assets</loc>" in body
    assert "<loc>http://testserver/organizations</loc>" in body


def test_sitemap_splits_into_an_index_once_the_urls_exceed_one_file(web_client: TestClient) -> None:
    """25,001 asset URLs is one past `SITEMAP_URLS_PER_FILE`, so `/sitemap.xml` becomes a
    `<sitemapindex>` and the URLs move into `/sitemaps/{n}.xml`, each well inside the protocol's
    50,000-URL and 50 MB limits."""
    pages = [_page(_many("asset", 25000), has_more=True, cursor="c2"), _page(_many("more", 1))]
    _install(
        _transport(
            {
                "/v1/proposals": _page([]),
                "/v1/opportunities": _page([]),
                "/v1/organizations": _page([]),
                "/v1/assets": lambda n: pages[min(n, 1)],
            }
        )
    )

    index = web_client.get("/sitemap.xml")

    assert index.status_code == 200
    assert "<sitemapindex" in index.text
    assert "<loc>http://testserver/sitemaps/1.xml</loc>" in index.text
    assert "<loc>http://testserver/sitemaps/2.xml</loc>" in index.text
    first = web_client.get("/sitemaps/1.xml")
    second = web_client.get("/sitemaps/2.xml")
    assert first.text.count("<loc>") == 25000
    assert second.text.count("<loc>") == 9  # 8 static + 25,001 assets = 25,009; 9 overflow
    assert "<urlset" in first.text and "<urlset" in second.text
    assert len(first.content) < 50 * 1024 * 1024


def test_a_sitemap_chunk_that_is_not_in_the_build_is_a_404_not_an_empty_urlset(
    web_client: TestClient,
) -> None:
    _install(
        _transport(
            {
                "/v1/proposals": _page([{"slug": "prop-a"}]),
                "/v1/opportunities": _page([]),
                "/v1/assets": _page([]),
                "/v1/organizations": _page([]),
            }
        )
    )

    resp = web_client.get("/sitemaps/7.xml")

    assert resp.status_code == 404
    assert "<urlset" not in resp.text


def test_sitemap_index_and_its_chunks_share_one_cached_walk(web_client: TestClient) -> None:
    """The per-base-URL cache predates this change and must keep working now that it holds a
    whole build: a crawler fetching the index and then every chunk must cost one walk."""
    transport = _transport(
        {
            "/v1/proposals": _page([{"slug": "prop-a"}]),
            "/v1/opportunities": _page([]),
            "/v1/assets": _page([]),
            "/v1/organizations": _page([]),
        }
    )
    _install(transport)

    web_client.get("/sitemap.xml")
    web_client.get("/sitemap.xml")
    web_client.get("/sitemaps/1.xml")

    assert len([c for c in transport.calls if c[1] == "/v1/proposals"]) == 1


def test_a_pipeline_only_view_defaults_to_a_sort_that_actually_orders_it(
    web_client: TestClient,
) -> None:
    """A line asset has no `capacity_mw`, and `length_miles` is not in the API's sort allowlist,
    so the capacity default would order a pipeline-only view by nothing."""
    transport = _transport(
        {"/v1/assets": _page([_asset()]), "/v1/assets/geo": _geo_totals({"gas_pipeline": 259})}
    )
    _install(transport)

    web_client.get("/assets", params={"asset_type": "gas_pipeline"})

    assert next(c[2] for c in transport.calls if c[1] == "/v1/assets")["sort"] == "name"


def test_a_filtered_index_describes_itself_rather_than_the_whole_register(
    web_client: TestClient,
) -> None:
    _install(
        _transport(
            {
                "/v1/assets": _page([_asset()]),
                "/v1/assets/geo": _geo_totals({"power_plant": 14659, "gas_pipeline": 259}),
            }
        )
    )

    unfiltered = _meta(web_client.get("/assets").text, "name", "description")
    by_type = _meta(web_client.get("/assets?asset_type=gas_pipeline").text, "name", "description")
    by_state = _meta(
        web_client.get("/assets?asset_type=gas_pipeline&state=US-CO").text, "name", "description"
    )

    assert unfiltered is not None and unfiltered.startswith("Power plants, gas pipelines")
    assert "14,918 of them" in unfiltered
    assert by_type is not None and by_type.startswith("Gas pipelines from US public registers")
    assert "259 of them" in by_type
    # A count `asset_type_counts()` does not know is never guessed at.
    assert by_state is not None and "in US-CO" in by_state and "of them" not in by_state


def test_the_selected_count_chip_is_marked_for_a_screen_reader_not_only_visually(
    web_client: TestClient,
) -> None:
    _install(
        _transport(
            {
                "/v1/assets": _page([_asset()]),
                "/v1/assets/geo": _geo_totals({"power_plant": 5, "gas_pipeline": 2}),
            }
        )
    )

    body = web_client.get("/assets", params={"asset_type": "gas_pipeline"}).text

    chips = re.search(r'<nav class="count-chips".*?</nav>', body, re.S)
    assert chips is not None
    assert chips.group(0).count('aria-current="page"') == 1
    selected = re.search(r'<a href="/assets\?asset_type=gas_pipeline" aria-current="page"', body)
    assert selected is not None
