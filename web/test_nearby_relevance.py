"""The company page's technology filter on "Proposals near these assets" (owner, 2026-09-20:
"given tallgrass doesnt do solar, I dont want them seeing solar ... or at least have the ability
to just see whats relevant to them").

Two halves: `web/relevance.py` against the shipped mapping
(`data/vendored/relevance/asset_technology_relevance.yaml`), and `/organizations/{ident}` driven
against a fake `Transport` -- the same pattern as `web/test_asset_pages.py`, duplicated rather
than imported because no `web/test_*.py` imports another (this repo's convention).

The page behaviour these tests exist to hold in place:
  * a company whose asset types all map gets the default filter, and the page says so in words
    with both counts and a one-click undo;
  * a company with an unmapped asset type -- or with none at all -- sees everything;
  * `?nearby_technology=all` turns the default off and `?nearby_technology=<t>` picks by hand;
  * all of it through a GET form with query parameters, so it works with JavaScript off.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.relevance import (
    ALL,
    PARAM,
    load_relevance,
    nearby_notice,
    parse_relevance,
    resolve_nearby_filter,
)

DEFAULT_HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 14, "opportunities": 7},
}
TECHNOLOGIES = [
    "gas_cc",
    "gas_ct",
    "gas_steam",
    "gas_ice",
    "gas_other",
    "hydrogen",
    "fuel_cell",
    "solar",
    "storage",
]
DEFAULT_VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": t} for t in TECHNOLOGIES],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
    }
}
#: What the shipped mapping resolves a pure gas-midstream holding to. Pinned here so a change to
#: the YAML shows up as a failing page test, not only as a changed file.
GAS_DEFAULT = ("gas_cc", "gas_ct", "gas_steam", "gas_ice", "gas_other", "hydrogen", "fuel_cell")


class FakeTransport:
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


def _org(by_type: Mapping[str, int]) -> dict[str, Any]:
    counts = {
        "assets": sum(by_type.values()),
        "by_role": {"operator": sum(by_type.values())},
        "by_type": dict(by_type),
        "by_role_and_type": {"operator": dict(by_type)},
    }
    return {
        "public_id": "org_01MIDSTREAM",
        "slug": "midstream-co",
        "name_canonical": "Midstream Co",
        "type": "other",
        "country": "US",
        "jurisdiction": "US-CO",
        "asset_counts": counts,
        "group_asset_counts": counts,
        "subsidiary_count": 0,
        "provenance": [],
    }


def _row(public_id: str, technology: str) -> dict[str, Any]:
    return {
        "public_id": public_id,
        "slug": public_id.lower(),
        "name_canonical": f"Project {public_id}",
        "technology": technology,
        "capacity_mw": 100.0,
        "lifecycle_state": "filed",
        "jurisdiction": "US-CO",
        "location": {"geom": None, "state_code": "US-CO", "precision": "exact"},
        "distance_km": 4.2,
        "nearest_asset": {"public_id": "asset_01P", "slug": "a-pipe", "name": "A Pipe"},
        "provenance": [],
    }


def _transport(
    by_type: Mapping[str, int],
    *,
    nearby: list[dict[str, Any]],
    totals: Mapping[str, Any] | None,
) -> FakeTransport:
    envelope: dict[str, Any] = {"data": nearby}
    if totals is not None:
        envelope["totals"] = dict(totals)
    return FakeTransport(
        {
            "/v1/health": (200, DEFAULT_HEALTH),
            "/v1/meta/vocabularies": (200, DEFAULT_VOCAB),
            "/v1/sources": (200, {"data": []}),
            "/v1/organizations": (200, {"data": [_org(by_type)]}),
            "/v1/organizations/org_01MIDSTREAM/assets": (200, {"data": []}),
            "/v1/organizations/org_01MIDSTREAM/proposals": (200, {"data": []}),
            "/v1/organizations/org_01MIDSTREAM/opportunities": (200, {"data": []}),
            "/v1/organizations/org_01MIDSTREAM/nearby-proposals": (200, envelope),
        }
    )


GAS_ONLY = {"gas_pipeline": 3, "gas_processing_plant": 1, "gas_storage": 2}
MIXED = {"gas_pipeline": 3, "power_plant": 2}


def _nearby_params(transport: FakeTransport) -> dict[str, Any]:
    return next(
        params for _method, url, params, _cookies in transport.calls if url.endswith("/nearby-proposals")
    )


def _notice(body: str) -> str:
    assert 'id="nearby-filter-notice"' in body, "the page must state what it is showing and why"
    return body.split('id="nearby-filter-notice"')[1].split("</p>")[0]


# ----------------------------------------------------------------- the mapping file itself
def test_shipped_mapping_covers_every_asset_type_in_the_model() -> None:
    """ADR 0008's asset types are either grouped or explicitly unfiltered. A thirteenth type
    added to the model without a line here fails this test rather than quietly inheriting
    somebody else's relevance rule."""
    from services.api.assets import ASSET_TYPES

    relevance = load_relevance()
    missing = set(ASSET_TYPES) - set(relevance.known_asset_types)
    assert not missing, f"asset types with no relevance decision: {sorted(missing)}"
    assert set(relevance.unfiltered) >= {"power_plant", "transmission_line", "substation"}


def test_shipped_mapping_uses_only_real_technology_tokens() -> None:
    """An invented token would make the API return 400 and the company page fall back to the
    unfiltered list -- a silent failure. Pin every one against the API's own vocabulary."""
    from services.api.assets import TECHNOLOGY_VOCAB

    for group in load_relevance().groups:
        unknown = [t for t in group.technologies if t not in TECHNOLOGY_VOCAB]
        assert not unknown, f"{group.id}: {unknown}"


def test_gas_midstream_default_is_the_gas_family_and_excludes_standalone_storage() -> None:
    default = load_relevance().default_for(GAS_ONLY)

    assert default is not None
    assert default.technologies == GAS_DEFAULT
    # The deliberate exclusion, and the one the owner is most likely to want moved: standalone
    # storage is the biggest bucket in the register and is not a midstream operator's business.
    assert "storage" not in default.technologies
    assert "solar" not in default.technologies
    assert default.label == "gas-related technologies"


def test_a_company_holding_an_unmapped_asset_type_gets_no_default() -> None:
    """Rule 2 of the mapping: hold one thing with no rule and the whole company goes unfiltered.
    A pipeline-and-power-plant owner is not a midstream pure-play, and a gas-only list would hide
    the neighbours of its own plants."""
    relevance = load_relevance()

    assert relevance.default_for(MIXED) is None
    assert relevance.default_for([]) is None
    assert relevance.default_for(["something_invented_later"]) is None


def test_multiple_matching_groups_union_their_technologies() -> None:
    default = load_relevance().default_for({"gas_pipeline": 1, "refinery": 1})

    assert default is not None
    assert set(GAS_DEFAULT) <= set(default.technologies) and "oil" in default.technologies
    assert default.reason == "operates gas infrastructure and operates refining assets"


def test_a_malformed_mapping_raises_rather_than_filtering_on_half_a_rule() -> None:
    with pytest.raises(ValueError, match="asset_types and technologies"):
        parse_relevance({"groups": [{"id": "broken", "asset_types": ["gas_pipeline"]}]})
    with pytest.raises(ValueError, match="both grouped and unfiltered"):
        parse_relevance(
            {
                "groups": [
                    {
                        "id": "g",
                        "asset_types": ["gas_pipeline"],
                        "technologies": ["gas_ct"],
                        "filter_label": "x",
                        "reason": "y",
                    }
                ],
                "unfiltered": {"gas_pipeline": "contradiction"},
            }
        )


# --------------------------------------------------------------------- resolving one request
def test_resolve_degrades_an_unknown_token_to_everything_not_to_nothing() -> None:
    default = load_relevance().default_for(GAS_ONLY)

    resolved = resolve_nearby_filter("unobtanium", default=default, vocabulary=TECHNOLOGIES)

    # Not a 400 and not an empty list: a stale or hand-typed URL must never make the register
    # look emptier than it is.
    assert resolved.technologies is None and resolved.mode == "off"


def test_resolve_modes() -> None:
    default = load_relevance().default_for(GAS_ONLY)

    assert resolve_nearby_filter(None, default=default).mode == "default"
    assert resolve_nearby_filter("", default=default).technologies == GAS_DEFAULT
    assert resolve_nearby_filter(ALL, default=default).technologies is None
    picked = resolve_nearby_filter("gas_ct,solar", default=default, vocabulary=TECHNOLOGIES)
    assert picked.mode == "manual" and picked.technologies == ("gas_ct", "solar")
    # No default at all: there is nothing to explain, so the page says "all", not "unfiltered".
    assert resolve_nearby_filter(None, default=None).mode == "none"


def test_notice_states_both_counts_and_the_reason() -> None:
    default = load_relevance().default_for(GAS_ONLY)
    notice = nearby_notice(
        resolve_nearby_filter(None, default=default),
        shown=18,
        total=67,
        path="/organizations/tallgrass-energy",
        listed_cap=50,
    )

    assert notice is not None
    assert notice["text"] == (
        "Showing 18 of 67 nearby proposals, narrowed to gas-related technologies "
        "because this company operates gas infrastructure."
    )
    assert notice["link_text"] == "Show all 67"
    assert notice["link_href"] == f"/organizations/tallgrass-energy?{PARAM}=all"


def test_notice_says_when_the_list_is_also_capped_by_limit() -> None:
    notice = nearby_notice(
        resolve_nearby_filter(ALL, default=None),
        shown=67,
        total=67,
        path="/x",
        listed_cap=50,
        listed_projects=50,
    )

    assert notice is not None
    assert notice["text"] == "Showing all 67 nearby proposals. The 50 nearest are listed."


# -------------------------------------------------------------------------- the company page
def test_company_page_applies_the_default_and_says_so_with_both_counts(web_client: TestClient) -> None:
    transport = _transport(
        GAS_ONLY,
        nearby=[_row("prop_01A", "gas_ct"), _row("prop_01B", "gas_cc")],
        totals={
            "assets_considered": 6,
            "proposals_within_radius": 2,
            "proposals_within_radius_unfiltered": 9,
        },
    )
    _install(transport)

    body = web_client.get("/organizations/midstream-co").text

    # The filter reached the API as a CSV `technology` parameter.
    assert _nearby_params(transport)["technology"] == ",".join(GAS_DEFAULT)
    # And the page states it where a reader cannot miss it: both numbers, the narrowing, the why.
    notice = _notice(body)
    assert "Showing 2 of 9 nearby proposals" in notice
    assert "narrowed to gas-related technologies" in notice
    assert "because this company operates gas infrastructure" in notice
    assert f'href="/organizations/midstream-co?{PARAM}=all">Show all 9</a>' in notice
    assert "Project prop_01A" in body


def test_company_page_with_an_unmapped_asset_type_shows_everything(web_client: TestClient) -> None:
    transport = _transport(
        MIXED,
        nearby=[_row("prop_01A", "solar"), _row("prop_01B", "gas_ct")],
        totals={
            "assets_considered": 5,
            "proposals_within_radius": 2,
            "proposals_within_radius_unfiltered": 2,
        },
    )
    _install(transport)

    body = web_client.get("/organizations/midstream-co").text

    assert "technology" not in _nearby_params(transport)
    assert "Showing all 2 nearby proposals" in _notice(body)
    assert "narrowed" not in _notice(body)
    assert "Project prop_01A" in body and "Project prop_01B" in body


def test_show_all_link_turns_the_default_off(web_client: TestClient) -> None:
    transport = _transport(
        GAS_ONLY,
        nearby=[_row("prop_01A", "solar")],
        totals={
            "assets_considered": 6,
            "proposals_within_radius": 9,
            "proposals_within_radius_unfiltered": 9,
        },
    )
    _install(transport)

    body = web_client.get(f"/organizations/midstream-co?{PARAM}=all").text

    assert "technology" not in _nearby_params(transport)
    notice = _notice(body)
    assert "Showing all 9 nearby proposals, unfiltered." in notice
    # ...and offers the way back, so the escape is reversible in one click too.
    assert 'href="/organizations/midstream-co">Narrow to gas-related technologies</a>' in notice


def test_manual_technology_choice_overrides_the_default(web_client: TestClient) -> None:
    transport = _transport(
        GAS_ONLY,
        nearby=[_row("prop_01A", "solar")],
        totals={
            "assets_considered": 6,
            "proposals_within_radius": 1,
            "proposals_within_radius_unfiltered": 9,
        },
    )
    _install(transport)

    body = web_client.get(f"/organizations/midstream-co?{PARAM}=solar").text

    assert _nearby_params(transport)["technology"] == "solar"
    assert "Showing 1 of 9 nearby proposals, narrowed to the technology you chose: solar." in _notice(body)
    assert '<option value="solar" selected>solar</option>' in body


def test_a_filter_matching_nothing_explains_itself_instead_of_looking_empty(
    web_client: TestClient,
) -> None:
    transport = _transport(
        GAS_ONLY,
        nearby=[],
        totals={
            "assets_considered": 6,
            "proposals_within_radius": 0,
            "proposals_within_radius_unfiltered": 9,
        },
    )
    _install(transport)

    body = web_client.get("/organizations/midstream-co").text

    assert "Showing 0 of 9 nearby proposals" in _notice(body)
    assert "No proposals of the chosen technologies within 25" in body
    assert f'href="/organizations/midstream-co?{PARAM}=all"' in body


def test_the_filter_works_with_javascript_off(web_client: TestClient) -> None:
    """docs/04 D-30: this site's filtering is GET forms and query parameters. The nearby filter
    adds no new idiom -- a `<form method="get">` with a `<select>` and a submit button, no `hx-`
    attributes, and every state reachable as a plain URL."""
    transport = _transport(
        GAS_ONLY,
        nearby=[_row("prop_01A", "gas_ct")],
        totals={
            "assets_considered": 6,
            "proposals_within_radius": 1,
            "proposals_within_radius_unfiltered": 9,
        },
    )
    _install(transport)

    form = (
        web_client.get("/organizations/midstream-co").text.split('id="nearby-filters"')[1].split("</form>")[0]
    )

    assert 'method="get"' in web_client.get("/organizations/midstream-co").text
    assert "hx-get" not in form and "<script" not in form
    assert f'name="{PARAM}"' in form
    assert '<option value="all"' in form and '<option value="" selected>' in form
    assert '<button class="filter-bar__submit" type="submit">Apply</button>' in form
    # Every technology in the API vocabulary is pickable by hand, not just the default set.
    for technology in TECHNOLOGIES:
        assert f'<option value="{technology}"' in form


def test_notice_reconciles_the_counts_with_the_rows_the_reader_can_see() -> None:
    """Three numbers can differ and all three have to be said, or the sentence looks wrong next
    to the list: matched records, the `limit` cap, and the projects those records collapse into
    (a register lists a project once per generator unit)."""
    plain = nearby_notice(
        resolve_nearby_filter(ALL, default=None),
        shown=67,
        total=67,
        path="/x",
        listed_cap=50,
        listed_projects=36,
    )
    assert plain is not None
    assert plain["text"] == (
        "Showing all 67 nearby proposals. The 50 nearest are listed, as 36 projects: "
        "a register lists a project once per generator unit."
    )

    # Under the cap, only the grouping needs explaining.
    grouped_only = nearby_notice(
        resolve_nearby_filter(ALL, default=None),
        shown=18,
        total=67,
        path="/x",
        listed_cap=50,
        listed_projects=8,
    )
    assert grouped_only is not None
    assert grouped_only["text"].endswith(
        "Listed as 8 projects: a register lists a project once per generator unit."
    )

    # Nothing collapsed and nothing capped: one sentence, no arithmetic to explain.
    exact = nearby_notice(
        resolve_nearby_filter(ALL, default=None),
        shown=3,
        total=3,
        path="/x",
        listed_cap=50,
        listed_projects=3,
    )
    assert exact is not None and exact["text"] == "Showing all 3 nearby proposals."
