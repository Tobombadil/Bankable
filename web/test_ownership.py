"""`web/ownership.py` and the company page's ownership tree.

**Fixtures, not data.** Every multi-level chain, every fund with a large portfolio and every
undated ownership claim below is canned. The loaded register carries 298 parent links whose
deepest real chain is three levels and whose reference case (Tallgrass Energy over nine operating
subsidiaries) is one level, and no scope in it holds enough assets to reach either rendering
threshold. So what these tests prove is that the page behaves correctly *if* such a group is
loaded, not that such a group exists.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.ownership import (
    GROUP_MAP_MAX_ASSETS,
    GROUP_TABLE_MAX_ASSETS,
    Claim,
    ancestor_claims,
    group_view,
    parent_claim,
    portfolio_rows,
    resolve_scope,
    scope_links,
)

DEFAULT_HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 14, "opportunities": 7},
}
DEFAULT_VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}, {"value": "wind"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
    }
}


class FakeTransport:
    """Canned responses keyed by path, every call recorded. Duplicated from
    `web/test_asset_pages.py` rather than imported: this repo's convention is that no
    `web/test_*.py` imports another one."""

    def __init__(self, responses: Mapping[str, tuple[int, Any]] | None = None) -> None:
        self.responses: dict[str, tuple[int, Any]] = dict(responses or {})
        self.calls: list[tuple[str, str, Any, dict[str, str] | None]] = []

    def _respond(self, url: str) -> httpx.Response:
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

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


@pytest.fixture()
def web_client() -> Iterator[TestClient]:
    with TestClient(web_app) as client:
        yield client
    web_app.state.__dict__.pop("api_client", None)
    web_app.state.__dict__.pop("lag_days_default", None)
    web_app.state.__dict__.pop("sitemap_cache", None)
    web_app.state.__dict__.pop("asset_type_counts_cache", None)


def _install(transport: FakeTransport) -> None:
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None


def _summary(public_id: str, slug: str, name: str) -> dict[str, Any]:
    return {"public_id": public_id, "slug": slug, "name_canonical": name, "type": "pipeline_operator"}


TALLGRASS = _summary("org_01TALLGRASS", "tallgrass-energy", "Tallgrass Energy")
BLACKSTONE = _summary("org_01BX", "blackstone-infrastructure", "Blackstone Infrastructure Partners")
TRAILBLAZER = _summary("org_01TB", "trailblazer-pipeline-co", "Trailblazer Pipeline Co")


def _scope_meta(**overrides: Any) -> dict[str, Any]:
    return {
        "scope": "all",
        "organizations": 3,
        "depth": 2,
        "depth_capped": False,
        "truncated": False,
        "cycle_detected": False,
        "max_depth": 10,
        "max_organizations": 500,
        **overrides,
    }


# ---------------------------------------------------------------- the scope token
@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "all"), ("", "all"), ("self", "self"), ("children", "children"), ("ALL", "all")],
)
def test_resolve_scope_reads_the_url(raw, expected):
    assert resolve_scope(raw) == expected


def test_an_unknown_scope_token_degrades_to_the_default_rather_than_an_error():
    """Same rule as the technology filter: a stale or hand-typed URL must never make the register
    look emptier than it is, and here the empty answer would be a holding company's own row."""
    assert resolve_scope("everything") == "all"
    assert resolve_scope("../../etc") == "all"


def test_scope_links_are_three_plain_urls_and_vanish_for_a_leaf():
    links = scope_links("/organizations/tallgrass-energy", "all", descendant_count=9)
    assert [link["scope"] for link in links] == ["self", "children", "all"]
    assert [link["href"] for link in links] == [
        "/organizations/tallgrass-energy?scope=self",
        "/organizations/tallgrass-energy?scope=children",
        "/organizations/tallgrass-energy",
    ]
    assert [link["current"] for link in links] == [False, False, True]
    assert scope_links("/organizations/x", "all", descendant_count=0) == []


# ---------------------------------------------------------------- the claim and its date
def test_a_dated_claim_says_when_and_an_undated_one_says_that_it_is_undated():
    dated = Claim(name="X", href=None, public_id=None, source_id="global.gleif.lei", as_of="2024-06-30")
    assert dated.dated is True
    assert dated.note == "Ownership stated as of 2024-06-30 from global.gleif.lei."

    undated = Claim(name="X", href=None, public_id=None, source_id="curated.organization_parents", as_of=None)
    assert undated.dated is False
    assert "no date stated by the source" in undated.note

    assert Claim(name="X", href=None, public_id=None, source_id=None, as_of=None).note == ""


def test_parent_claim_falls_back_to_the_bare_summary_and_reads_as_undated():
    """An API build that sends `parent` but no `parent_edge` is stating an ownership link with no
    date. The page must read that as "undated", not as "today"."""
    claim = parent_claim({"parent": TALLGRASS})
    assert claim is not None and claim.name == "Tallgrass Energy" and claim.dated is False
    assert parent_claim({}) is None


def test_ancestor_claims_put_each_date_on_the_company_the_link_is_about():
    """The API's `ancestors[i]` names a parent and carries the date of the link reaching it from
    below, so rendering it where it arrives labels the wrong company."""
    entity = {
        "ancestors": [
            {"organization": BLACKSTONE, "source_id": "gleif", "as_of": "2024-01-31", "share_pct": None},
            {"organization": TALLGRASS, "source_id": "curated", "as_of": None, "share_pct": None},
        ]
    }
    crumbs = ancestor_claims(entity)
    assert [c.name for c in crumbs] == ["Blackstone Infrastructure Partners", "Tallgrass Energy"]
    # Blackstone is the top of the chain: its own parent link is outside it, so no note.
    assert crumbs[0].note == ""
    # Tallgrass's own parent link is the Blackstone entry's date.
    assert crumbs[1].as_of == "2024-01-31" and "2024-01-31" in crumbs[1].note
    assert ancestor_claims({}) == []


# ---------------------------------------------------------------- company page vs fund page
def _totals(assets: int, holders: int) -> dict[str, Any]:
    return {
        "assets": assets,
        "organization_count": holders,
        "by_organization": [
            {"organization": _summary(f"org_{i}", f"co-{i}", f"Portfolio Co {i}"), "assets": 1, "by_type": {}}
            for i in range(holders)
        ],
    }


def test_a_single_company_renders_as_it_always_did_however_much_it_holds():
    """The eight scopes in the current data above the map threshold are all single companies with
    no subsidiaries (the largest, WM Renewable Energy, holds 102 landfill-gas assets). The dot
    scatter the owner described is a fund problem; a company page is not touched."""
    view = group_view(
        scope="all",
        totals=_totals(assets=102, holders=1),
        scope_meta=_scope_meta(organizations=1, depth=0),
    )
    assert view.is_group is False
    assert view.show_map is True and view.show_asset_table is True and view.notes == ()


def test_a_group_keeps_its_map_while_every_asset_can_be_drawn():
    view = group_view(
        scope="all", totals=_totals(assets=GROUP_MAP_MAX_ASSETS, holders=9), scope_meta=_scope_meta()
    )
    assert view.is_group is True and view.show_map is True and view.notes == ()
    assert view.holder_count == 9 and view.organization_count == 3


def test_a_group_past_the_map_threshold_drops_the_map_and_says_why():
    view = group_view(
        scope="all", totals=_totals(assets=GROUP_MAP_MAX_ASSETS + 1, holders=30), scope_meta=_scope_meta()
    )
    assert view.show_map is False and view.show_asset_table is True
    assert "No map" in view.notes[0] and "30 companies" in view.notes[0]


def test_a_group_past_the_table_threshold_becomes_a_portfolio_of_companies():
    view = group_view(
        scope="all",
        totals=_totals(assets=GROUP_TABLE_MAX_ASSETS + 1, holders=60),
        scope_meta=_scope_meta(),
    )
    assert view.show_map is False and view.show_asset_table is False
    assert any("each company's own page" in n for n in view.notes)


@pytest.mark.parametrize(
    ("meta_key", "fragment"),
    [
        ("truncated", "larger than 500 companies"),
        ("depth_capped", "deeper than the 10 levels"),
        ("cycle_detected", "point back on themselves"),
    ],
)
def test_every_way_the_walk_can_stop_short_is_stated_on_the_page(meta_key, fragment):
    view = group_view(
        scope="all", totals=_totals(assets=4, holders=3), scope_meta=_scope_meta(**{meta_key: True})
    )
    assert any(fragment in n for n in view.notes), view.notes


def test_portfolio_rows_mark_the_page_the_reader_is_standing_on():
    rows = portfolio_rows(
        {
            "by_organization": [
                {"organization": TALLGRASS, "assets": 3, "by_type": {"gas_pipeline": 3}},
                {"organization": TRAILBLAZER, "assets": 1, "by_type": {"gas_pipeline": 1}},
                {"not": "a mapping"},
            ]
        },
        subject_public_id="org_01TB",
    )
    assert [r.name for r in rows] == ["Tallgrass Energy", "Trailblazer Pipeline Co"]
    assert [r.is_subject for r in rows] == [False, True]
    assert rows[0].href == "/organizations/tallgrass-energy"
    assert portfolio_rows(None, subject_public_id=None) == []


# ---------------------------------------------------------------- through the page
def _org_entity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        **TRAILBLAZER,
        "country": "US",
        "jurisdiction": "US-CO",
        "website": None,
        "ids": {},
        "is_curated_issuer": False,
        "provenance": [],
        "parent": TALLGRASS,
        "parent_edge": {
            "organization": TALLGRASS,
            "source_id": "curated.organization_parents",
            "as_of": None,
            "share_pct": None,
        },
        "ancestors": [
            {"organization": BLACKSTONE, "source_id": "global.gleif.lei", "as_of": "2024-02-29"},
            {
                "organization": TALLGRASS,
                "source_id": "curated.organization_parents",
                "as_of": None,
                "share_pct": None,
            },
        ],
        "subsidiaries": [],
        "subsidiary_count": 0,
        "descendant_count": 2,
        "asset_counts": _totals(assets=2, holders=2),
        "group_asset_counts": _totals(assets=2, holders=2),
        "group_scope": _scope_meta(),
    }
    base.update(overrides)
    return base


def _asset_row() -> dict[str, Any]:
    return {
        "asset": {
            "public_id": "asset_01TB",
            "slug": "trailblazer-pipeline",
            "name": "Trailblazer Pipeline",
            "asset_type": "gas_pipeline",
            "capacity_mw": None,
            "state_code": "US-NE",
            "length_miles": 400.0,
            "geometry": None,
            "provenance": [],
        },
        "role": "operator",
        "share_pct": None,
    }


def _transport(entity: Mapping[str, Any] | None = None, **envelope: Any) -> FakeTransport:
    org = dict(entity or _org_entity())
    pid = org["public_id"]
    return FakeTransport(
        {
            "/v1/health": (200, DEFAULT_HEALTH),
            "/v1/meta/vocabularies": (200, DEFAULT_VOCAB),
            "/v1/sources": (200, {"data": []}),
            "/v1/organizations": (200, {"data": [org]}),
            f"/v1/organizations/{pid}/assets": (
                200,
                {
                    "data": [_asset_row()],
                    "totals": envelope.get("totals", org["asset_counts"]),
                    "scope": envelope.get("scope", org["group_scope"]),
                },
            ),
            f"/v1/organizations/{pid}/proposals": (200, {"data": []}),
            f"/v1/organizations/{pid}/opportunities": (200, {"data": []}),
            f"/v1/organizations/{pid}/nearby-proposals": (200, {"data": [], "totals": {}}),
        }
    )


def test_the_page_renders_the_ownership_chain_as_breadcrumb_links(web_client: TestClient) -> None:
    _install(_transport())
    body = web_client.get("/organizations/trailblazer-pipeline-co").text
    crumbs = body.split('aria-label="Breadcrumb"')[1].split("</nav>")[0]
    assert '<a href="/organizations/blackstone-infrastructure"' in crumbs
    assert '<a href="/organizations/tallgrass-energy"' in crumbs
    assert crumbs.index("blackstone-infrastructure") < crumbs.index("tallgrass-energy")
    # The date sits on the company the link is about, not on the company it names.
    assert 'title="Ownership stated as of 2024-02-29 from global.gleif.lei."' in crumbs


def test_the_page_says_when_an_ownership_claim_carries_no_date(web_client: TestClient) -> None:
    """The curated parent file cites when a page was read, not when the ownership began, so nine
    of the loaded links have no date. The page says so rather than letting the reader read the
    claim as current."""
    _install(_transport())
    body = web_client.get("/organizations/trailblazer-pipeline-co").text
    panel = body.split('id="parent-provenance"')[1].split("</span>")[0]
    assert "no date stated by the source" in panel
    assert "ownership-claim--undated" in body


def test_the_page_forwards_the_scope_from_the_url_and_keeps_it_in_its_own_links(
    web_client: TestClient,
) -> None:
    transport = _transport()
    _install(transport)
    resp = web_client.get("/organizations/trailblazer-pipeline-co", params={"scope": "children"})
    assert resp.status_code == 200
    forwarded = [c[2] for c in transport.calls if c[1].endswith("/assets")]
    assert forwarded and forwarded[0].get("scope") == "children"
    # Every link the page emits stays at this level of the tree, including the filter form's
    # hidden field -- a GET form replaces the whole query string.
    assert '<input type="hidden" name="scope" value="children">' in resp.text
    assert 'href="/organizations/trailblazer-pipeline-co?scope=self"' in resp.text


def test_scope_self_asks_the_api_for_nothing_because_self_is_its_default(
    web_client: TestClient,
) -> None:
    transport = _transport()
    _install(transport)
    resp = web_client.get("/organizations/trailblazer-pipeline-co", params={"scope": "self"})
    forwarded = [c[2] for c in transport.calls if c[1].endswith("/assets")]
    assert forwarded and "scope" not in forwarded[0]
    # But the page's own URLs still carry it, or a filter click would silently widen the view.
    assert '<input type="hidden" name="scope" value="self">' in resp.text


def test_a_group_page_lists_its_portfolio_of_companies(web_client: TestClient) -> None:
    _install(_transport())
    body = web_client.get("/organizations/trailblazer-pipeline-co").text
    assert 'id="portfolio-table"' in body
    assert "Portfolio Co 0" in body and "Portfolio Co 1" in body
    assert "2 assets held by 2 companies in a group of 3" in body


def test_a_fund_sized_group_drops_the_map_and_prints_the_reason(web_client: TestClient) -> None:
    big = _totals(assets=GROUP_MAP_MAX_ASSETS + 60, holders=40)
    entity = _org_entity(asset_counts=big, group_asset_counts=big)
    _install(_transport(entity, totals=big))
    body = web_client.get("/organizations/trailblazer-pipeline-co").text
    assert "mini-map" not in body
    assert "No map:" in body and "40 companies" in body
    assert 'id="portfolio-table"' in body
