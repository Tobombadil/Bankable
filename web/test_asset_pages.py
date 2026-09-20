"""Tests for ADR 0008 (`docs/adr/0008-assets-first-class-and-placement-grades.md`): the two new
proxies (`/api/assets/geo`, `/api/geo/regions`), the asset and organisation pages, the
`/search` organisations section, and `/sitemap.xml`.

Every test drives `web.app.app` against a hand-written fake `Transport` (`web/api_client.py`'s
`Transport` protocol), the same pattern `web/test_map_layers.py` uses -- no database, no
`services.api` import -- since the API lane is building `/v1/assets*`, `/v1/geo/regions` and
`/v1/organizations/{id}/assets` in parallel against the same `docs/23-api-spec-outline.md`
contract this file codes against.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app

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
    """Canned responses keyed by path; every call recorded so a test can assert what a proxy
    forwarded -- identical shape to `web/test_map_layers.py::FakeTransport`, duplicated rather
    than imported so this file stays a self-contained test module (this repo's convention: no
    `web/test_*.py` imports another one)."""

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


def _default_transport(extra: Mapping[str, tuple[int, Any]] | None = None) -> FakeTransport:
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
    with TestClient(web_app) as client:
        yield client
    web_app.state.__dict__.pop("api_client", None)
    web_app.state.__dict__.pop("lag_days_default", None)
    # The sitemap cache is keyed on base URL and every test here shares `http://testserver`,
    # so one test's canned build would otherwise be served to the next.
    web_app.state.__dict__.pop("sitemap_cache", None)
    web_app.state.__dict__.pop("asset_type_counts_cache", None)


def _install(transport: FakeTransport) -> None:
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None


def _asset_entity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "asset_01JBQ7Z8KD",
        "slug": "roscoe-wind-farm-tx",
        "name": "Roscoe Wind Farm",
        "asset_type": "power_plant",
        "status": "operating",
        "operator_name": "Roscoe Wind Farm LLC",
        "technology": "wind",
        "technology_raw": "Onshore Wind Turbine",
        "technologies": {"Onshore Wind Turbine": 781.5},
        "capacity_mw": 781.5,
        "capacity_value": None,
        "capacity_unit": None,
        "commissioned_year": 2007,
        "unit_count": 627,
        "state_code": "US-TX",
        "county_name": "Nolan",
        "county_fips": "48353",
        "country": "US",
        "attributes": {"capacity_factor_2025": 0.41},
        "owners": [
            {
                "public_id": "org_01JBQ8C4X1",
                "name": "NextEra Energy Resources, LLC",
                "role": "owner",
                "share_pct": 50.0,
                "as_of": "2024-12-31",
            }
        ],
        "provenance": [
            {
                "source_id": "us.eia.860m",
                "source_name": "EIA-860M",
                "source_url": "https://www.eia.gov/electricity/data/eia860m/",
                "retrieved_at": "2026-09-01T00:00:00Z",
                "reuse_class": "open",
                "attribution_text": "EIA-860M",
                "source_record_id": "6452",
            }
        ],
    }
    base.update(overrides)
    return base


def _org_entity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "org_01JBQ8C4X1",
        "slug": "nextera-energy-resources",
        "name_canonical": "NextEra Energy Resources, LLC",
        "type": "developer",
        "country": "US",
        "jurisdiction": "US-FL",
        "website": "https://www.nexteraenergyresources.com",
        "provenance": [],
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------------------ /api/assets/geo
def test_assets_geo_proxy_forwards_bbox_zoom_asset_type_technology_and_strips_cookies(
    web_client: TestClient,
) -> None:
    transport = _default_transport(
        {
            "/v1/assets/geo": (
                200,
                {"data": {"type": "FeatureCollection", "features": [], "totals": {}}, "meta": {}},
            )
        }
    )
    _install(transport)

    resp = web_client.get(
        "/api/assets/geo",
        params={"bbox": "1,2,3,4", "zoom": "6", "asset_type": "power_plant", "technology": "wind"},
        cookies={"session": "abc"},
    )

    assert resp.status_code == 200
    calls = [c for c in transport.calls if c[1] == "/v1/assets/geo"]
    assert len(calls) == 1
    _method, _url, params, cookies = calls[0]
    assert params == {"bbox": "1,2,3,4", "zoom": "6", "asset_type": "power_plant", "technology": "wind"}
    assert cookies is None


def test_assets_geo_proxy_defaults_bbox_zoom_and_omits_absent_filters(web_client: TestClient) -> None:
    transport = _default_transport(
        {
            "/v1/assets/geo": (
                200,
                {"data": {"type": "FeatureCollection", "features": [], "totals": {}}, "meta": {}},
            )
        }
    )
    _install(transport)

    resp = web_client.get("/api/assets/geo")

    assert resp.status_code == 200
    _method, _url, params, _cookies = transport.calls[-1]
    assert params["zoom"] == "3"
    assert "asset_type" not in params
    assert "technology" not in params


def test_assets_geo_proxy_relays_api_error_status(web_client: TestClient) -> None:
    transport = _default_transport({"/v1/assets/geo": (400, {"title": "bad_request"})})
    _install(transport)

    resp = web_client.get("/api/assets/geo")

    assert resp.status_code == 400


def test_context_plants_geo_proxy_still_works_unchanged(web_client: TestClient) -> None:
    """Task item 2: `/api/context/plants/geo` "keeping ... working as before" -- unaffected by
    adding the `/api/assets/geo` proxy alongside it."""
    transport = _default_transport(
        {
            "/v1/context/plants/geo": (
                200,
                {"data": {"type": "FeatureCollection", "features": [], "totals": {"records": 0}}, "meta": {}},
            )
        }
    )
    _install(transport)

    resp = web_client.get("/api/context/plants/geo")

    assert resp.status_code == 200


# ---------------------------------------------------------------------------- /api/geo/regions
def test_geo_regions_proxy_forwards_level_and_ids_only_and_strips_cookies(web_client: TestClient) -> None:
    transport = _default_transport(
        {"/v1/geo/regions": (200, {"data": {"type": "FeatureCollection", "features": []}})}
    )
    _install(transport)

    resp = web_client.get(
        "/api/geo/regions", params={"level": "county", "ids": "48353,48001"}, cookies={"session": "abc"}
    )

    assert resp.status_code == 200
    calls = [c for c in transport.calls if c[1] == "/v1/geo/regions"]
    assert len(calls) == 1
    _method, _url, params, cookies = calls[0]
    assert params == {"level": "county", "ids": "48353,48001"}
    assert cookies is None


def test_geo_regions_proxy_relays_api_error_status(web_client: TestClient) -> None:
    transport = _default_transport({"/v1/geo/regions": (400, {"title": "bad_request"})})
    _install(transport)

    resp = web_client.get("/api/geo/regions", params={"level": "county", "ids": "48353"})

    assert resp.status_code == 400


# ------------------------------------------------------------------------------- /proposals/geo
def test_proposals_geo_proxy_forwards_placement_and_county_fips(web_client: TestClient) -> None:
    transport = _default_transport(
        {
            "/v1/proposals/geo": (
                200,
                {"data": {"type": "FeatureCollection", "features": [], "totals": {}}, "meta": {}},
            )
        }
    )
    _install(transport)

    resp = web_client.get(
        "/api/proposals/geo", params={"placement": "exact,region,none", "county_fips": "48353"}
    )

    assert resp.status_code == 200
    _method, _url, params, _cookies = transport.calls[-1]
    assert params["placement"] == "exact,region,none"
    assert params["county_fips"] == "48353"


# ---------------------------------------------------------------------------------- /assets/{slug}
def test_asset_detail_renders_identity_attributes_owners_and_provenance(web_client: TestClient) -> None:
    entity = _asset_entity()
    transport = _default_transport(
        {
            "/v1/assets": (200, {"data": [entity]}),
            "/v1/assets/asset_01JBQ7Z8KD/nearby-proposals": (200, {"data": []}),
            "/v1/sources/us.eia.860m": (
                200,
                {"data": {"licence": {"quote_text": "Public domain."}}},
            ),
        }
    )
    _install(transport)

    resp = web_client.get("/assets/roscoe-wind-farm-tx")

    assert resp.status_code == 200
    body = resp.text
    assert "Roscoe Wind Farm" in body
    assert "781.5" in body  # capacity
    assert "capacity factor 2025" in body  # attributes table, underscore replaced
    assert "NextEra Energy Resources" in body
    assert 'href="/organizations/org_01JBQ8C4X1"' in body
    assert "50.0%" in body  # share
    assert "No exact-grade proposals nearby." in body
    assert "provenance-panel" in body


def test_asset_detail_slug_lookup_forwards_the_slug_filter(web_client: TestClient) -> None:
    transport = _default_transport({"/v1/assets": (200, {"data": [_asset_entity()]})})
    _install(transport)

    web_client.get("/assets/roscoe-wind-farm-tx")

    calls = [c for c in transport.calls if c[1] == "/v1/assets"]
    assert calls[0][2] == {"slug": "roscoe-wind-farm-tx", "limit": 1}


def test_asset_detail_404_when_slug_not_found(web_client: TestClient) -> None:
    transport = _default_transport({"/v1/assets": (200, {"data": []})})
    _install(transport)

    resp = web_client.get("/assets/does-not-exist")

    assert resp.status_code == 404
    assert "asset" in resp.text


# ----------------------------------------------------------------------- /organizations/{ident}
def test_organization_detail_resolves_by_slug(web_client: TestClient) -> None:
    entity = _org_entity()
    transport = _default_transport(
        {
            "/v1/organizations": (200, {"data": [entity]}),
            "/v1/organizations/org_01JBQ8C4X1/assets": (200, {"data": []}),
            "/v1/organizations/org_01JBQ8C4X1/proposals": (200, {"data": []}),
            "/v1/organizations/org_01JBQ8C4X1/opportunities": (200, {"data": []}),
        }
    )
    _install(transport)

    resp = web_client.get("/organizations/nextera-energy-resources")

    assert resp.status_code == 200
    assert "NextEra Energy Resources" in resp.text
    assert "developer" in resp.text


def test_organization_detail_falls_back_to_public_id_when_slug_lookup_misses(web_client: TestClient) -> None:
    """An asset page's owners table links by `public_id` (docs/23: the owners embed is only
    documented to carry that, not a slug) -- this route must resolve that identifier too."""
    entity = _org_entity()
    transport = _default_transport(
        {
            "/v1/organizations": (200, {"data": []}),  # slug filter misses
            "/v1/organizations/org_01JBQ8C4X1": (200, {"data": entity}),
            "/v1/organizations/org_01JBQ8C4X1/assets": (200, {"data": []}),
            "/v1/organizations/org_01JBQ8C4X1/proposals": (200, {"data": []}),
            "/v1/organizations/org_01JBQ8C4X1/opportunities": (200, {"data": []}),
        }
    )
    _install(transport)

    resp = web_client.get("/organizations/org_01JBQ8C4X1")

    assert resp.status_code == 200
    assert "NextEra Energy Resources" in resp.text


def test_organization_detail_renders_assets_proposals_opportunities(web_client: TestClient) -> None:
    entity = _org_entity()
    transport = _default_transport(
        {
            "/v1/organizations": (200, {"data": [entity]}),
            "/v1/organizations/org_01JBQ8C4X1/assets": (
                200,
                {
                    "data": [
                        {
                            "asset": {
                                "public_id": "asset_1",
                                "slug": "roscoe-wind-farm-tx",
                                "name": "Roscoe Wind Farm",
                                "asset_type": "power_plant",
                                "capacity_mw": 781.5,
                            },
                            "role": "owner",
                            "share_pct": 50.0,
                        }
                    ]
                },
            ),
            "/v1/organizations/org_01JBQ8C4X1/proposals": (
                200,
                {
                    "data": [
                        {
                            "public_id": "prop_1",
                            "slug": "some-proposal",
                            "name_canonical": "Some Proposal",
                            "provenance": [],
                        }
                    ]
                },
            ),
            "/v1/organizations/org_01JBQ8C4X1/opportunities": (
                200,
                {"data": [{"public_id": "opp_1", "slug": "some-rfp", "title": "Some RFP", "provenance": []}]},
            ),
        }
    )
    _install(transport)

    resp = web_client.get("/organizations/nextera-energy-resources")

    assert resp.status_code == 200
    body = resp.text
    assert 'href="/assets/roscoe-wind-farm-tx"' in body
    assert "Some Proposal" in body
    assert "Some RFP" in body


def test_organization_detail_404_when_not_found_by_slug_or_public_id(web_client: TestClient) -> None:
    transport = _default_transport({"/v1/organizations": (200, {"data": []})})
    _install(transport)

    resp = web_client.get("/organizations/does-not-exist")

    assert resp.status_code == 404
    assert "organisation" in resp.text


# ----------------------------------------------------------------------------------- /search
def test_search_renders_organizations_section(web_client: TestClient) -> None:
    transport = _default_transport(
        {
            "/v1/proposals": (200, {"data": []}),
            "/v1/opportunities": (200, {"data": []}),
            "/v1/organizations": (200, {"data": [_org_entity()]}),
        }
    )
    _install(transport)

    resp = web_client.get("/search", params={"q": "nextera"})

    assert resp.status_code == 200
    body = resp.text
    assert "Organisations" in body
    assert 'href="/organizations/nextera-energy-resources"' in body
    calls = [c for c in transport.calls if c[1] == "/v1/organizations"]
    assert calls[0][2]["q"] == "nextera"


def test_search_with_no_query_does_not_call_organizations(web_client: TestClient) -> None:
    transport = _default_transport()
    _install(transport)

    resp = web_client.get("/search")

    assert resp.status_code == 200
    assert not any(c[1] == "/v1/organizations" for c in transport.calls)


# --------------------------------------------------------------------------------- /sitemap.xml
def test_sitemap_lists_every_resource_and_caps_pagination(web_client: TestClient) -> None:
    proposal_page_1 = {
        "data": [{"slug": "prop-a"}],
        "page": {"has_more": True, "next_cursor": "c1"},
    }
    proposal_page_2 = {
        "data": [{"slug": "prop-b"}],
        "page": {"has_more": False, "next_cursor": None},
    }
    call_count = {"n": 0}

    transport = _default_transport(
        {
            "/v1/opportunities": (200, {"data": [{"slug": "opp-a"}], "page": {"has_more": False}}),
            "/v1/assets": (200, {"data": [{"slug": "asset-a"}], "page": {"has_more": False}}),
            "/v1/organizations": (200, {"data": [{"slug": "org-a"}], "page": {"has_more": False}}),
        }
    )

    # `/v1/proposals` needs two different pages back to back -- handled with a small override of
    # `_respond` rather than the fixed-response dict every other path uses.
    original_respond = transport._respond

    def _respond(url: str) -> httpx.Response:
        if url == "/v1/proposals":
            call_count["n"] += 1
            return httpx.Response(200, json=proposal_page_1 if call_count["n"] == 1 else proposal_page_2)
        return original_respond(url)

    transport._respond = _respond  # type: ignore[method-assign]
    _install(transport)

    resp = web_client.get("/sitemap.xml")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    body = resp.text
    assert "<urlset" in body
    assert "/proposals/prop-a" in body
    assert "/proposals/prop-b" in body
    assert "/opportunities/opp-a" in body
    assert "/assets/asset-a" in body
    assert "/organizations/org-a" in body
    proposal_calls = [c for c in transport.calls if c[1] == "/v1/proposals"]
    assert len(proposal_calls) == 2  # followed exactly one `next_cursor` hop, then stopped


def test_sitemap_skips_a_resource_whose_list_call_errors(web_client: TestClient) -> None:
    transport = _default_transport(
        {
            "/v1/proposals": (500, {"title": "error"}),
            "/v1/opportunities": (200, {"data": [{"slug": "opp-a"}], "page": {"has_more": False}}),
            "/v1/assets": (200, {"data": [], "page": {"has_more": False}}),
            "/v1/organizations": (200, {"data": [], "page": {"has_more": False}}),
        }
    )
    _install(transport)

    resp = web_client.get("/sitemap.xml")

    assert resp.status_code == 200
    assert "/opportunities/opp-a" in resp.text


# ------------------------------------------------------------------- home_map(): new controls
def test_home_map_has_placement_checkboxes_default_exact_and_region_checked(web_client: TestClient) -> None:
    _install(_default_transport())
    resp = web_client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="mf-placement-exact"' in body
    assert 'id="mf-placement-region"' in body
    assert 'id="mf-placement-none"' in body
    exact_tag = body.split('id="mf-placement-exact"')[1].split(">")[0]
    region_tag = body.split('id="mf-placement-region"')[1].split(">")[0]
    none_tag = body.split('id="mf-placement-none"')[1].split(">")[0]
    assert "checked" in exact_tag
    assert "checked" in region_tag
    assert "checked" not in none_tag


SEVEN_LIVE_ASSET_TYPES = (
    "power_plant",
    "gas_pipeline",
    "gas_processing_plant",
    "gas_storage",
    "lng_terminal",
    "ethanol_plant",
    "rng_project",
)


def test_home_map_lists_asset_type_checkboxes_all_seven_live(web_client: TestClient) -> None:
    """Midstream slice (docs/00-PLAN.md 2026-09-19, option (a)): the type control is a checkbox
    per type so pipelines draw beside plants; ethanol and RNG are enabled since the second slice
    loaded them (EIA Atlas ethanol plants, EPA LMOP, EPA AgSTAR) -- nothing is "(coming)" today."""
    _install(_default_transport())
    resp = web_client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="mf-asset-types"' in body
    assert "Existing assets" in body and "Existing assets (EIA)" not in body
    for value in SEVEN_LIVE_ASSET_TYPES:
        tag = body.split(f'id="mf-asset-type-{value}"')[1].split(">")[0]
        assert "disabled" not in tag, value
    assert "(coming)" not in body and "is-disabled" not in body
    assert 'id="mf-asset-type-refinery"' not in body
    # One legend group per type, each hidden/shown by map.js with the checkbox; the ethanol and
    # RNG groups say which rows are not points (state-grade capacity table, county-grade digesters).
    for value in SEVEN_LIVE_ASSET_TYPES:
        assert f'data-legend-type="{value}"' in body, value
    assert "Interstate" in body and "Intrastate" in body
    ethanol_group = body.split('data-legend-type="ethanol_plant"')[1].split("</div>")[0]
    assert "state grade" in ethanol_group and 'class="legend__note"' in ethanol_group
    rng_group = body.split('data-legend-type="rng_project"')[1].split("</div>")[0]
    assert "county grade" in rng_group
    assert 'src="/static/js/basemap.js' in body


def test_map_js_reads_placement_from_url_and_writes_it_back(web_client: TestClient) -> None:
    """No JS test harness exists (task brief: "assert the template and the proxy contract"
    instead) -- these assert the shipped script contains the placement/region wiring the task
    describes, at the level of "the right identifiers and endpoints appear", not behaviour."""
    from pathlib import Path

    source = (Path(__file__).parent / "static" / "js" / "map.js").read_text()
    assert '"/api/assets/geo' in source
    assert '"/api/geo/regions' in source
    assert "PLACEMENT_GRADES" in source
    assert 'params.set("placement"' in source
    assert "region_opacity" in source
    assert "county_fips=" in source
    assert "jurisdiction=" in source


# ============================================================ midstream slice (2026-09-19)
# Pipelines as a line layer, gas processing / storage / LNG as point layers, operator edges, the
# pipeline asset page and the company page (docs/00-PLAN.md 2026-09-19, option (a)). The fakes
# follow the API lane's stated contract: `feature_kind: "asset_line"` with LineString geometry on
# `/v1/assets/geo`, `geometry` + `length_miles` + line-aware `nearby_proposals` (`distance_km`)
# on asset detail, `owner`/`operator` role annotations and `asset_counts` on organisations.
_REX_LINE = {
    "type": "MultiLineString",
    "coordinates": [
        [[-108.6, 40.1], [-104.8, 40.6], [-100.2, 40.9]],
        [[-100.2, 40.9], [-95.9, 40.4], [-82.7, 39.9]],
    ],
}


def _pipeline_entity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "asset_01REX",
        "slug": "rockies-express-pipeline",
        "name": "Rockies Express Pipeline",
        "asset_type": "gas_pipeline",
        "status": "operating",
        "operator_name": "Tallgrass Energy",
        "technology": None,
        "technology_raw": None,
        "technologies": {},
        "capacity_mw": None,
        "capacity_value": None,
        "capacity_unit": None,
        "commissioned_year": 2009,
        "unit_count": None,
        "state_code": None,
        "county_name": None,
        "county_fips": None,
        "country": "US",
        "length_miles": 1712.4,
        "geometry": _REX_LINE,
        "attributes": {
            "interstate": "Interstate",
            "diameter_in": 42,
            "states": ["CO", "WY", "NE", "KS", "MO", "IL", "IN", "OH"],
        },
        "owners": [
            {
                "organization": {
                    "public_id": "org_01TALLGRASS",
                    "slug": "tallgrass-energy",
                    "name_canonical": "Tallgrass Energy",
                },
                "role": "operator",
                "share_pct": None,
                "as_of": "2025-12-31",
            }
        ],
        "provenance": [
            {
                "source_id": "us.eia.atlas.pipelines",
                "source_name": "EIA U.S. Energy Atlas — natural gas pipelines",
                "source_url": "https://atlas.eia.gov/",
                "retrieved_at": "2026-09-18T00:00:00Z",
                "reuse_class": "open",
                "attribution_text": "EIA U.S. Energy Atlas",
                "source_record_id": "rex-1",
            }
        ],
    }
    base.update(overrides)
    return base


def _nearby_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "prop_01NEAR",
        "slug": "cheyenne-storage-1",
        "name_canonical": "Cheyenne Storage 1",
        "technology": "storage",
        "capacity_mw": 120.0,
        "lifecycle_state": "filed",
        "jurisdiction": "US-WY",
        "location": {
            "geom": {"type": "Point", "coordinates": [-104.7, 41.1]},
            "state_code": "US-WY",
            "precision": "exact",
        },
        "distance_km": 12.345,
        "provenance": [],
    }
    base.update(overrides)
    return base


def test_asset_detail_pipeline_renders_map_length_diameter_states_and_operator_link(
    web_client: TestClient,
) -> None:
    transport = _default_transport(
        {
            "/v1/assets": (200, {"data": [_pipeline_entity()]}),
            "/v1/assets/asset_01REX/nearby-proposals": (200, {"data": [_nearby_row()]}),
        }
    )
    _install(transport)

    resp = web_client.get("/assets/rockies-express-pipeline")

    assert resp.status_code == 200
    body = resp.text
    assert "Rockies Express Pipeline" in body
    assert "Gas pipeline" in body
    assert "interstate" in body  # pipeline class row
    assert "1,712" in body  # length, whole miles
    assert "42 in" in body  # diameter
    assert "CO, WY, NE, KS, MO, IL, IN, OH" in body  # states crossed
    assert 'href="/organizations/tallgrass-energy"' in body  # operator linked to the company page
    assert "Tallgrass Energy" in body
    assert "EIA U.S. Energy Atlas" in body and "2026-09-18" in body  # source and retrieval date
    # Promoted attributes are not repeated in the generic table, and a list never renders as a repr.
    attributes_table = body.split("2.</span> Attributes")[1].split("</section>")[0]
    assert "diameter in" not in attributes_table and "['CO'" not in body
    # Static SVG of the route (no-JS rendering) plus the GeoJSON the mini-map script reads.
    assert 'id="asset-map"' in body and "<svg" in body and 'class="mini-map__line"' in body
    assert 'id="asset-map-data"' in body and '"MultiLineString"' in body
    assert 'src="/static/js/asset_map.js' in body
    assert "pipeline&rsquo;s route" in body  # line-aware nearby wording


def test_asset_detail_nearby_proposals_carry_distance_and_draw_on_the_map(web_client: TestClient) -> None:
    transport = _default_transport(
        {
            "/v1/assets": (200, {"data": [_pipeline_entity()]}),
            "/v1/assets/asset_01REX/nearby-proposals": (200, {"data": [_nearby_row()]}),
        }
    )
    _install(transport)

    body = web_client.get("/assets/rockies-express-pipeline").text

    assert "Cheyenne Storage 1" in body
    assert "12.3 km away" in body
    assert 'class="mini-map__proposal"' in body
    assert "1 exact-grade proposal within 25 km" in body


def test_asset_detail_drops_rows_for_absent_fields_and_never_renders_none(web_client: TestClient) -> None:
    """The "None" gate rule (blockers sprint) applied to the asset page: a field the API does not
    send is not a row -- no "None", no "—" placeholder."""
    entity = _pipeline_entity(
        length_miles=None, attributes={}, operator_name=None, owners=[], commissioned_year=None, geometry=None
    )
    transport = _default_transport(
        {
            "/v1/assets": (200, {"data": [entity]}),
            "/v1/assets/asset_01REX/nearby-proposals": (200, {"data": []}),
        }
    )
    _install(transport)

    body = web_client.get("/assets/rockies-express-pipeline").text

    assert "<dt>Length" not in body
    assert "<dt>Diameter" not in body
    assert "<dt>States" not in body
    assert "<dt>Operator" not in body
    assert "<dt>Commissioned" not in body
    assert "<dt>Capacity" not in body
    assert "<dt>Technology" not in body
    assert ">None<" not in body and "None</" not in body
    assert "<dd>—</dd>" not in body and 'class="tnum">—' not in body
    assert 'id="asset-map"' not in body  # no geometry -> no map at all


def test_asset_detail_reads_the_owner_organisation_embed(web_client: TestClient) -> None:
    """`serialize_asset_owner` nests the organisation under `organization` -- the owners table
    must name and link it from that shape as well as from the flat one the older test uses."""
    transport = _default_transport(
        {
            "/v1/assets": (200, {"data": [_pipeline_entity()]}),
            "/v1/assets/asset_01REX/nearby-proposals": (200, {"data": []}),
        }
    )
    _install(transport)

    body = web_client.get("/assets/rockies-express-pipeline").text

    assert body.count("Tallgrass Energy") >= 2  # operator field + owners table
    assert '<td data-label="Role">operator</td>' in body


def _tallgrass_org(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "org_01TALLGRASS",
        "slug": "tallgrass-energy",
        "name_canonical": "Tallgrass Energy",
        "type": "pipeline_operator",
        "country": "US",
        "jurisdiction": "US-CO",
        "website": "https://www.tallgrass.com",
        "parent": {
            "public_id": "org_01BLACKSTONE",
            "slug": "blackstone-infrastructure",
            "name_canonical": "Blackstone Infrastructure Partners",
        },
        "subsidiaries": [
            {
                "public_id": "org_01REXLLC",
                "slug": "rockies-express-pipeline-llc",
                "name_canonical": "Rockies Express Pipeline LLC",
                "type": "pipeline_operator",
            }
        ],
        "asset_counts": {
            "assets": 6,
            "by_role": {"operator": 4, "owner": 2},
            "by_type": {"gas_pipeline": 3, "gas_processing_plant": 1, "power_plant": 2},
            "by_role_and_type": {
                "operator": {"gas_pipeline": 3, "gas_processing_plant": 1},
                "owner": {"power_plant": 2},
            },
        },
        "subsidiary_count": 1,
        "provenance": [],
    }
    base.update(overrides)
    return base


def _tallgrass_asset_rows() -> list[dict[str, Any]]:
    def line(public_id: str, slug: str, name: str, miles: float) -> dict[str, Any]:
        return {
            "asset": {
                "public_id": public_id,
                "slug": slug,
                "name": name,
                "asset_type": "gas_pipeline",
                "capacity_mw": None,
                "length_miles": miles,
                "state_code": None,
                "attributes": {"interstate": "Interstate", "states": ["CO", "NE"]},
                "geometry": {"type": "LineString", "coordinates": [[-108.0, 40.0], [-100.0, 40.5]]},
                "provenance": [
                    {
                        "source_id": "us.eia.atlas.pipelines",
                        "source_name": "EIA Atlas pipelines",
                        "source_url": "https://atlas.eia.gov/",
                        "retrieved_at": "2026-09-18T00:00:00Z",
                        "reuse_class": "open",
                    }
                ],
            },
            "role": "operator",
            "share_pct": None,
        }

    return [
        line("asset_01REX", "rockies-express-pipeline", "Rockies Express Pipeline", 1712.0),
        line(
            "asset_01TIGT",
            "tallgrass-interstate-gas-transmission",
            "Tallgrass Interstate Gas Transmission",
            4400.0,
        ),
        line("asset_01TPP", "trailblazer-pipeline", "Trailblazer Pipeline", 436.0),
        {
            "asset": {
                "public_id": "asset_01PROC",
                "slug": "casper-processing",
                "name": "Casper Gas Processing Plant",
                "asset_type": "gas_processing_plant",
                "capacity_mw": None,
                "state_code": "US-WY",
                "geometry": {"type": "Point", "coordinates": [-106.3, 42.9]},
                "provenance": [
                    {
                        "source_id": "us.eia.atlas.processing",
                        "source_name": "EIA Atlas processing plants",
                        "source_url": "https://atlas.eia.gov/",
                        "retrieved_at": "2026-09-18T00:00:00Z",
                        "reuse_class": "open",
                    }
                ],
            },
            "role": "operator",
            "share_pct": None,
        },
        {
            "asset": {
                "public_id": "asset_01PLT",
                "slug": "some-plant",
                "name": "Some Plant",
                "asset_type": "power_plant",
                "capacity_mw": 50.0,
                "state_code": "US-CO",
                "geometry": None,
                "provenance": [],
            },
            "role": "owner",
            "share_pct": 100.0,
        },
    ]


def _tallgrass_transport(**org_overrides: Any) -> FakeTransport:
    return _default_transport(
        {
            "/v1/organizations": (200, {"data": [_tallgrass_org(**org_overrides)]}),
            "/v1/organizations/org_01TALLGRASS/assets": (200, {"data": _tallgrass_asset_rows()}),
            "/v1/organizations/org_01TALLGRASS/proposals": (200, {"data": []}),
            "/v1/organizations/org_01TALLGRASS/opportunities": (200, {"data": []}),
            "/v1/organizations/org_01TALLGRASS/nearby-proposals": (
                200,
                {
                    "data": [
                        _nearby_row(
                            nearest_asset={
                                "public_id": "asset_01REX",
                                "slug": "rockies-express-pipeline",
                                "name": "Rockies Express Pipeline",
                            }
                        )
                    ]
                },
            ),
        }
    )


def test_organization_detail_groups_assets_by_role_and_type_with_summary_line(web_client: TestClient) -> None:
    _install(_tallgrass_transport())

    resp = web_client.get("/organizations/tallgrass-energy")

    assert resp.status_code == 200
    body = resp.text
    # Summary from the API's `asset_counts.by_role_and_type` (role -> type -> n), operator first.
    summary = body.split('id="org-summary">')[1].split("</p>")[0]
    assert summary == "Operates 3 gas pipelines · Operates 1 gas processing plant · Owns 2 power plants"
    # One table per (role, type) group, in that order, with the columns the group's rows fill.
    assert body.index('id="assets-operator-gas_pipeline"') < body.index(
        'id="assets-operator-gas_processing_plant"'
    )
    assert body.index('id="assets-operator-gas_processing_plant"') < body.index(
        'id="assets-owner-power_plant"'
    )
    assert "Operates 3 gas pipelines</h3>" in body
    assert "Owns 1 power plant</h3>" in body  # counted over the page's rows, singular
    pipelines_table = body.split('id="assets-operator-gas_pipeline"')[1].split("</table>")[0]
    assert "Length (miles)" in pipelines_table and "1,712" in pipelines_table and "CO, NE" in pipelines_table
    assert "Capacity (MW)" not in pipelines_table
    plants_table = body.split('id="assets-owner-power_plant"')[1].split("</table>")[0]
    assert "Capacity (MW)" in plants_table and "100.0%" in plants_table
    assert 'href="/assets/rockies-express-pipeline"' in body
    assert ">None<" not in body


def test_organization_detail_counts_rows_when_the_api_sends_no_asset_counts(web_client: TestClient) -> None:
    _install(_tallgrass_transport(asset_counts=None))

    body = web_client.get("/organizations/tallgrass-energy").text

    summary = body.split('id="org-summary">')[1].split("</p>")[0]
    assert summary == "Operates 3 gas pipelines · Operates 1 gas processing plant · Owns 1 power plant"


def test_organization_detail_shows_asset_sources_instead_of_a_licence_claim(web_client: TestClient) -> None:
    """The bug this closes: an organisation row carries no provenance of its own, and the panel
    used to render "Sources withheld under licence" for it -- a licence claim that was not true."""
    _install(_tallgrass_transport())

    body = web_client.get("/organizations/tallgrass-energy").text

    assert "Sources withheld under licence" not in body
    assert "provenance-panel" in body
    assert "EIA Atlas pipelines" in body and "EIA Atlas processing plants" in body
    assert body.count("EIA Atlas pipelines") == 1  # de-duplicated across the three pipelines
    assert "registers behind this organisation" in body


def test_organization_detail_with_no_sources_at_all_renders_no_sources_panel(web_client: TestClient) -> None:
    transport = _default_transport(
        {
            "/v1/organizations": (200, {"data": [_org_entity()]}),
            "/v1/organizations/org_01JBQ8C4X1/assets": (200, {"data": []}),
            "/v1/organizations/org_01JBQ8C4X1/proposals": (200, {"data": []}),
            "/v1/organizations/org_01JBQ8C4X1/opportunities": (200, {"data": []}),
        }
    )
    _install(transport)

    body = web_client.get("/organizations/nextera-energy-resources").text

    assert "Sources withheld under licence" not in body
    assert 'class="provenance-panel" aria-label="Sources"' not in body  # no panel at all
    assert 'id="asset-map"' not in body
    assert "No assets recorded for this organisation." in body


def test_organization_detail_renders_parent_and_subsidiaries(web_client: TestClient) -> None:
    _install(_tallgrass_transport())

    body = web_client.get("/organizations/tallgrass-energy").text

    assert 'href="/organizations/blackstone-infrastructure"' in body
    assert "Blackstone Infrastructure Partners" in body
    assert "Subsidiaries" in body
    assert 'href="/organizations/rockies-express-pipeline-llc"' in body


def test_organization_detail_embeds_asset_geojson_for_the_map(web_client: TestClient) -> None:
    _install(_tallgrass_transport())

    body = web_client.get("/organizations/tallgrass-energy").text

    assert 'id="asset-map"' in body and body.count('class="mini-map__line"') == 3
    assert 'class="mini-map__point"' in body
    data = body.split('id="asset-map-data">')[1].split("</script>")[0]
    assert data.count('"LineString"') == 3
    assert data.count('"Point"') == 2  # the processing plant + the one nearby proposal
    assert "4 of 5 assets with a mapped location; 1 without one is listed below" in body
    assert "listed below, and 1 exact-grade proposal within 25 km" in body


def test_organization_detail_fetches_geometry_per_asset_when_rows_lack_it(web_client: TestClient) -> None:
    """`/v1/organizations/{id}/assets` rows omit geometry (`include_geometry=False`), so the map
    reads each asset's detail response, and skips one that 404s (gated) without failing the page."""
    rows = _tallgrass_asset_rows()
    for r in rows:
        r["asset"].pop("geometry", None)
    transport = _default_transport(
        {
            "/v1/organizations": (200, {"data": [_tallgrass_org()]}),
            "/v1/organizations/org_01TALLGRASS/assets": (200, {"data": rows}),
            "/v1/organizations/org_01TALLGRASS/proposals": (200, {"data": []}),
            "/v1/organizations/org_01TALLGRASS/opportunities": (200, {"data": []}),
            "/v1/assets/asset_01REX": (200, {"data": _pipeline_entity()}),
        }
    )
    _install(transport)

    body = web_client.get("/organizations/tallgrass-energy").text

    detail_calls = [c[1] for c in transport.calls if c[1].startswith("/v1/assets/asset_")]
    assert detail_calls == [
        "/v1/assets/asset_01REX",
        "/v1/assets/asset_01TIGT",
        "/v1/assets/asset_01TPP",
        "/v1/assets/asset_01PROC",
        "/v1/assets/asset_01PLT",
    ]
    assert 'id="asset-map"' in body and body.count('class="mini-map__line"') == 1
    assert "1 of 5 assets with a mapped location; 4 without one are listed below" in body


def test_organization_detail_lists_proposals_near_its_assets_with_nearest_asset(
    web_client: TestClient,
) -> None:
    transport = _tallgrass_transport()
    _install(transport)

    body = web_client.get("/organizations/tallgrass-energy").text

    section = body.split("Proposals near these assets")[1].split("</section>")[0]
    assert "Cheyenne Storage 1" in section and "12.3 km away" in section
    assert 'nearest: <a href="/assets/rockies-express-pipeline">Rockies Express Pipeline</a>' in section
    assert 'class="mini-map__proposal"' in body  # drawn on the company map too
    calls = [c for c in transport.calls if c[1].endswith("/nearby-proposals")]
    assert calls and calls[0][2] == {"limit": 50, "include_subsidiaries": "true"}


def test_organization_detail_survives_a_missing_nearby_endpoint(web_client: TestClient) -> None:
    """An API without `/v1/organizations/{id}/nearby-proposals` yet (404) still renders the page."""
    transport = _tallgrass_transport()
    transport.responses.pop("/v1/organizations/org_01TALLGRASS/nearby-proposals")
    _install(transport)

    resp = web_client.get("/organizations/tallgrass-energy")

    assert resp.status_code == 200
    assert "No exact-grade proposals within 25" in resp.text


def test_asset_by_public_id_redirects_to_the_slug_page(web_client: TestClient) -> None:
    """Geo features carry `public_id` only; the drawer links through this redirect."""
    transport = _default_transport({"/v1/assets/asset_01REX": (200, {"data": _pipeline_entity()})})
    _install(transport)

    resp = web_client.get("/assets/by-id/asset_01REX", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/assets/rockies-express-pipeline"
    missing = web_client.get("/assets/by-id/asset_nope", follow_redirects=False)
    assert missing.status_code == 404


def test_search_resolves_a_pipeline_operator_and_a_pipeline_asset(web_client: TestClient) -> None:
    """Task item 4: "Tallgrass" resolves to the operator's company page and "Rockies Express" to
    the pipeline's asset page once the data lane loads them."""
    transport = _default_transport(
        {
            "/v1/proposals": (200, {"data": []}),
            "/v1/opportunities": (200, {"data": []}),
            "/v1/organizations": (200, {"data": [_tallgrass_org()]}),
            "/v1/assets": (200, {"data": [_pipeline_entity()]}),
        }
    )
    _install(transport)

    resp = web_client.get("/search", params={"q": "Tallgrass"})

    assert resp.status_code == 200
    body = resp.text
    assert 'href="/organizations/tallgrass-energy"' in body and "Tallgrass Energy" in body
    assert "pipeline operator" in body
    assert 'href="/assets/rockies-express-pipeline"' in body and "Rockies Express Pipeline" in body
    assert "Gas pipeline · Tallgrass Energy · CO, WY" in body
    assert "1 asset, 1 organisation" in body
    asset_calls = [c for c in transport.calls if c[1] == "/v1/assets"]
    assert asset_calls[0][2] == {"q": "Tallgrass", "limit": 50}


def test_search_survives_an_assets_list_error(web_client: TestClient) -> None:
    transport = _default_transport(
        {
            "/v1/proposals": (200, {"data": []}),
            "/v1/opportunities": (200, {"data": []}),
            "/v1/organizations": (200, {"data": []}),
            "/v1/assets": (500, {"title": "error"}),
        }
    )
    _install(transport)

    resp = web_client.get("/search", params={"q": "Rockies Express"})

    assert resp.status_code == 200
    assert "No matching assets." in resp.text


def test_map_script_draws_asset_lines_and_the_new_point_types() -> None:
    """No JS harness (see `test_map_js_reads_placement_from_url_and_writes_it_back`): assert the
    shipped script carries the line layer, the per-type icons, the hover/click wiring and the
    keyboard-reachable list rows the midstream task describes."""
    from pathlib import Path

    source = (Path(__file__).parent / "static" / "js" / "map.js").read_text()
    assert '"asset_line"' in source
    for layer in (
        "asset-lines-casing",
        "asset-lines",
        "asset-lines-intrastate",
        "asset-lines-hit",
        "asset-line-labels",
    ):
        assert f'id: "{layer}"' in source, layer
    assert '["interpolate", ["linear"], ["zoom"]' in source  # width by zoom
    for shape in ("asset-diamond", "asset-ring", "asset-triangle", "asset-hexagon", "asset-pentagon"):
        assert shape in source, shape
    for asset_type in ("gas_pipeline", "gas_processing_plant", "gas_storage", "lng_terminal"):
        assert f"{asset_type}:" in source, asset_type
    assert 'params.set("asset_type"' in source
    assert '"/assets/by-id/" + encodeURIComponent(String(p.public_id))' in source
    assert "showAssetTooltip" in source and 'map.on("mousemove", "asset-lines-hit"' in source
    assert 'map.on("click", "asset-lines-hit"' in source and "renderAsset" in source
    assert 'btn.className = "details-btn"' in source  # in-view rows open the drawer by keyboard
    assert 'appendGroupName("Regions (' in source  # region records reachable from the list
    assert "data-legend-type" in source
    # Escaping discipline (web audit 2026-09-18) holds for the new markup.
    assert "esc(p.operator_name)" in source and "esc(assetTypeLabel(" in source
    basemap = (Path(__file__).parent / "static" / "js" / "basemap.js").read_text()
    assert "window.InfraqueBasemap" in basemap and "fallbackStyle" in basemap
    mini = (Path(__file__).parent / "static" / "js" / "asset_map.js").read_text()
    assert 'getElementById("asset-map-data")' in mini and "InfraqueBasemap" in mini


# ------------------------------------------------------------ integration fixes, 2026-09-19 evening
def test_asset_detail_reads_the_detail_envelope_for_owners_and_geometry(web_client: TestClient) -> None:
    """First Tallgrass screenshots: the page resolved the slug through the list endpoint and
    rendered that row, which carries neither `owners` nor `geometry`, so a pipeline with an
    operator edge said "No ownership records". The page now fetches `/v1/assets/{public_id}`
    after resolving the slug and falls back to the list row only when that call fails."""
    detail = _asset_entity()
    list_row = {k: v for k, v in detail.items() if k not in ("owners", "geometry")}
    transport = _default_transport(
        {
            "/v1/assets": (200, {"data": [list_row]}),
            f"/v1/assets/{detail['public_id']}": (200, {"data": detail}),
        }
    )
    _install(transport)

    resp = web_client.get(f"/assets/{detail['slug']}")

    assert resp.status_code == 200
    body = resp.text
    assert "No ownership records" not in body
    raw_owner = detail["owners"][0]
    owner_org = raw_owner.get("organization") or raw_owner  # both spellings, see `_normalise_owners`
    owner_name = owner_org.get("name_canonical") or owner_org.get("name")
    assert owner_name and owner_name in body
    assert ("GET", f"/v1/assets/{detail['public_id']}") in [(c[0], c[1]) for c in transport.calls]


def test_asset_detail_falls_back_to_the_list_row_when_the_detail_call_fails(web_client: TestClient) -> None:
    detail = _asset_entity()
    list_row = {k: v for k, v in detail.items() if k not in ("owners", "geometry")}
    _install(_default_transport({"/v1/assets": (200, {"data": [list_row]})}))  # detail path 404s

    resp = web_client.get(f"/assets/{detail['slug']}")

    assert resp.status_code == 200
    assert detail["name"] in resp.text


def test_organization_detail_requests_the_group_and_names_the_holding_subsidiary(
    web_client: TestClient,
) -> None:
    """A parent such as Tallgrass Energy holds no `asset_owner` edge itself (curated parents link
    the Atlas operator strings beneath it), so the page asks for the group on both the assets and
    the nearby-proposals calls and shows which subsidiary holds each row."""
    rows = _tallgrass_asset_rows()
    rows[0]["held_by"] = {
        "public_id": "org_01REX",
        "slug": "rockies-express-pipeline",
        "name_canonical": "Rockies Express Pipeline",
    }
    transport = _tallgrass_transport()
    transport.responses["/v1/organizations/org_01TALLGRASS/assets"] = (200, {"data": rows})
    _install(transport)

    resp = web_client.get("/organizations/tallgrass-energy")

    assert resp.status_code == 200
    asset_calls = [c for c in transport.calls if c[1] == "/v1/organizations/org_01TALLGRASS/assets"]
    assert asset_calls and asset_calls[0][2].get("include_subsidiaries") == "true"
    nearby_calls = [c for c in transport.calls if c[1].endswith("/nearby-proposals")]
    assert nearby_calls and nearby_calls[0][2].get("include_subsidiaries") == "true"
    pipelines_table = resp.text.split('id="assets-operator-gas_pipeline"')[1].split("</table>")[0]
    assert "Held by" in pipelines_table
    assert 'href="/organizations/rockies-express-pipeline">Rockies Express Pipeline</a>' in pipelines_table
    assert ">None<" not in resp.text


# ============================================================ second midstream slice (2026-09-19)
# Ethanol plants (us.eia.atlas.ethanol_plants exact points; us.eia.ethanol_capacity at state grade)
# and RNG projects (us.epa.lmop landfill-gas projects with exact points; us.epa.agstar digesters at
# county grade). The fakes below carry the fields the live API serialises today (checked against
# the running dev API, 2026-09-19 evening): numeric `attributes` only -- `attributes_text` (PADD,
# data period, biogas end use, landfill name) is not exposed yet, so the rows that need it are
# absent, never placeholders.
_FUEL_SOURCE = {
    "source_id": "us.eia.atlas.ethanol_plants",
    "source_name": "EIA US Energy Atlas — Ethanol Plants",
    "source_url": "https://www.eia.gov/maps/map_data/Ethanol_Plants_US_EIA.zip",
    "retrieved_at": "2026-09-19T15:49:24Z",
    "reuse_class": "open",
    "attribution_text": None,
}


def _ethanol_entity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "asset_01ETH",
        "slug": "absolute-energy-llc-st-ansgar-ia-us-ia",
        "name": "Absolute Energy LLC (St Ansgar, IA)",
        "asset_type": "ethanol_plant",
        "status": "operating",
        "operator_name": "Absolute Energy LLC",
        "technology": "ethanol",
        "technology_raw": None,
        "technologies": {},
        "capacity_mw": None,
        "capacity_value": 125.0,
        "capacity_unit": "MMgal/yr",
        "commissioned_year": None,
        "unit_count": None,
        "state_code": "US-IA",
        "county_name": None,
        "county_fips": None,
        "country": "US",
        "attributes": {"nameplate_capacity_mmgal_yr": 125.0, "as_of_year": 2025.0},
        "geometry": {"type": "Point", "coordinates": [-92.9445, 43.4997]},
        "owners": [
            {
                "organization": {
                    "public_id": "org_01ABS",
                    "slug": "absolute-energy-llc",
                    "name_canonical": "Absolute Energy LLC",
                    "type": "other",
                },
                "role": "operator",
                "share_pct": None,
                "as_of": None,
            }
        ],
        "provenance": [_FUEL_SOURCE],
    }
    base.update(overrides)
    return base


def _lmop_entity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "asset_01LMOP",
        "slug": "project-2-city-of-charleston-landfill-us-wv",
        "name": "Project #2 - City of Charleston Landfill",
        "asset_type": "rng_project",
        "status": "operating",
        "operator_name": "Tallarico Energy",
        "technology": "rng",
        "technology_raw": "Vehicle Fuel",
        "technologies": {},
        "capacity_mw": None,
        "capacity_value": 0.677,
        "capacity_unit": "mmscfd",
        "commissioned_year": 2018,
        "unit_count": None,
        "state_code": "US-WV",
        "county_name": "Kanawha",
        "county_fips": "54039",
        "country": "US",
        "attributes": {
            "lfg_flow_to_project_mmscfd": 0.677,
            "landfill_count": 1.0,
            "project_start_year": 2018.0,
            "landfill_year_opened": 1971.0,
            "landfill_waste_in_place_tons": 5743184.0,
        },
        "geometry": {"type": "Point", "coordinates": [-81.61854, 38.31279]},
        "owners": [],
        "provenance": [
            {
                **_FUEL_SOURCE,
                "source_id": "us.epa.lmop",
                "source_name": "EPA LMOP Landfill and Project Database",
                "source_url": "https://www.epa.gov/lmop",
                "retrieved_at": "2026-09-19T15:49:08Z",
            }
        ],
    }
    base.update(overrides)
    return base


def _agstar_entity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "asset_01AGSTAR",
        "slug": "tinedale-farms-digester-us-wi",
        "name": "Tinedale Farms Digester",
        "asset_type": "rng_project",
        "status": "retired",
        "operator_name": None,
        "technology": "farm_digester",
        "technology_raw": "Fixed Film/Attached Media",
        "technologies": {},
        "capacity_mw": None,
        "capacity_value": 200000.0,
        "capacity_unit": "cu-ft/day",
        "commissioned_year": 1999,
        "unit_count": None,
        "state_code": "US-WI",
        "county_name": "Jackson",
        "county_fips": "55053",
        "country": "US",
        "attributes": {
            "biogas_generation_estimate_cuft_day": 200000.0,
            "electricity_generated_kwh_yr": 5584500.0,
            "cattle": 0.0,
            "dairy": 1800.0,
            "poultry": 0.0,
            "swine": 0.0,
            "year_operational": 1999.0,
            "year_shutdown": 2005.0,
        },
        "geometry": None,  # county grade: no point, so no map section
        "owners": [],
        "provenance": [
            {
                **_FUEL_SOURCE,
                "source_id": "us.epa.agstar",
                "source_name": "EPA AgSTAR Livestock Anaerobic Digester Database",
                "source_url": "https://www.epa.gov/agstar",
                "retrieved_at": "2026-09-19T15:49:18Z",
            }
        ],
    }
    base.update(overrides)
    return base


def _fuel_transport(entity: dict[str, Any], nearby: list[dict[str, Any]] | None = None) -> FakeTransport:
    return _default_transport(
        {
            "/v1/assets": (200, {"data": [entity]}),
            f"/v1/assets/{entity['public_id']}": (200, {"data": entity}),
            f"/v1/assets/{entity['public_id']}/nearby-proposals": (200, {"data": nearby or []}),
        }
    )


def _rows(body: str) -> dict[str, str]:
    """`{dt: dd}` of the page's field grid, tags stripped, for row-level assertions."""
    import re

    grid = body.split('<dl class="field-grid">')[1].split("</dl>")[0]
    out: dict[str, str] = {}
    for dt, dd in re.findall(r"<dt>(.*?)</dt><dd[^>]*>(.*?)</dd>", grid, re.S):
        out[dt] = re.sub(r"<[^>]+>", "", dd).strip()
    return out


def test_asset_detail_ethanol_promotes_nameplate_as_of_and_operator(web_client: TestClient) -> None:
    _install(_fuel_transport(_ethanol_entity()))

    resp = web_client.get("/assets/absolute-energy-llc-st-ansgar-ia-us-ia")

    assert resp.status_code == 200
    body = resp.text
    rows = _rows(body)
    assert rows["Type"] == "Ethanol plant"
    assert rows["Nameplate capacity"] == "125 MMgal/yr"
    assert rows["Capacity as of"] == "2025"
    assert rows["Operator"] == "Absolute Energy LLC"
    assert 'href="/organizations/absolute-energy-llc"' in body
    assert rows["State"] == "US-IA"
    # The plant-shaped rows do not double up the same numbers, and "ethanol" is not a Technology row.
    assert "Capacity (MMgal/yr)" not in rows and "Capacity (native unit)" not in rows
    assert "Technology" not in rows and "Commissioned" not in rows
    # Consumed attribute keys leave the generic Attributes table; nothing shows twice.
    assert "nameplate capacity mmgal yr" not in body and "as of year" not in body
    assert "No attributes recorded" in body
    assert 'retrieved <span class="tnum">2026-09-19' in body
    assert 'id="asset-map"' in body  # exact point -> map section
    assert "125 MMgal/yr" in body.split('name="description"')[1].split(">")[0]
    assert ">None<" not in body and "None</" not in body


def test_asset_detail_rng_landfill_project_promotes_project_fields(web_client: TestClient) -> None:
    _install(_fuel_transport(_lmop_entity()))

    body = web_client.get("/assets/project-2-city-of-charleston-landfill-us-wv").text

    rows = _rows(body)
    assert rows["Type"] == "RNG project"
    assert rows["Project type"] == "Vehicle Fuel"
    assert rows["Technology"] == "Renewable natural gas"
    assert rows["LFG flow to project"] == "0.677 MMscf/d"
    assert rows["Host landfill"] == "City of Charleston Landfill"
    assert rows["Start year"] == "2018"
    assert rows["Operator"] == "Tallarico Energy"
    assert rows["County"] == "Kanawha"
    # No rated MW on a vehicle-fuel project, no end use in today's payload: no such rows.
    assert "Rated capacity" not in rows and "Biogas end use" not in rows and "Digester type" not in rows
    assert "Capacity (mmscfd)" not in rows and "Commissioned" not in rows
    # Header badge carries the family in words (D-5), not a hue.
    badge = body.split('<span class="reuse-badge">')[1].split("</span>")[0]
    assert "RNG project" in badge and "Renewable natural gas" in badge and "operating" in badge
    # Unconsumed landfill numbers stay in the Attributes table.
    assert "landfill waste in place tons" in body and "5743184" in body
    assert "lfg flow to project mmscfd" not in body
    assert ">None<" not in body


def test_asset_detail_rng_landfill_electricity_shows_rated_mw(web_client: TestClient) -> None:
    entity = _lmop_entity(
        technology="lfg_electricity",
        technology_raw="Reciprocating Engine",
        capacity_mw=5.6,
        attributes={"rated_mw": 5.6, "lfg_flow_to_project_mmscfd": 1.725, "project_start_year": 2012.0},
    )
    _install(_fuel_transport(entity))

    rows = _rows(web_client.get("/assets/project-2-city-of-charleston-landfill-us-wv").text)

    assert rows["Technology"] == "Landfill gas to electricity"
    assert rows["Project type"] == "Reciprocating Engine"
    assert rows["Rated capacity"] == "5.6 MW"
    assert rows["LFG flow to project"] == "1.725 MMscf/d"
    assert rows["Start year"] == "2012"
    assert "Capacity (MW)" not in rows  # promoted once as "Rated capacity", never twice


def test_asset_detail_rng_digester_promotes_digester_type_feedstock_and_years(web_client: TestClient) -> None:
    _install(_fuel_transport(_agstar_entity()))

    body = web_client.get("/assets/tinedale-farms-digester-us-wi").text

    rows = _rows(body)
    assert rows["Technology"] == "Farm digester"
    assert rows["Digester type"] == "Fixed Film/Attached Media"
    assert rows["Biogas generation (est.)"] == "200,000 cu ft/day"
    assert rows["Feedstock"] == "Dairy (1,800 head)"  # from the herd counts; zero herds omitted
    assert rows["Start year"] == "1999" and rows["Shutdown year"] == "2005"
    assert rows["Status"] == "retired"
    assert "Project type" not in rows and "Host landfill" not in rows and "Operator" not in rows
    # Herd-count columns are consumed by the Feedstock row; the kWh figure is not, so it stays.
    assert ">dairy<" not in body and ">swine<" not in body
    assert "electricity generated kwh yr" in body
    assert 'id="asset-map"' not in body  # county grade -> no geometry -> no map section
    assert ">None<" not in body and "None</" not in body


def test_asset_detail_fuel_rows_drop_absent_values(web_client: TestClient) -> None:
    """The "None" gate on the promoted fuel rows: a capacity-table ethanol row without an as-of
    year, or a landfill project without flow, start year or a parseable landfill name, has no
    such rows -- and never "None", "—" or "0"."""
    ethanol = _ethanol_entity(
        attributes={}, capacity_value=None, capacity_unit=None, geometry=None, owners=[]
    )
    _install(_fuel_transport(ethanol))
    rows = _rows(web_client.get("/assets/absolute-energy-llc-st-ansgar-ia-us-ia").text)
    assert "Nameplate capacity" not in rows and "Capacity as of" not in rows and "PADD" not in rows
    assert rows["Operator"] == "Absolute Energy LLC"

    lmop = _lmop_entity(
        name="Charleston Gas Project",
        attributes={},
        capacity_value=None,
        capacity_unit=None,
        commissioned_year=None,
        technology_raw=None,
    )
    _install(_fuel_transport(lmop))
    body = web_client.get("/assets/project-2-city-of-charleston-landfill-us-wv").text
    rows = _rows(body)
    assert rows["Technology"] == "Renewable natural gas"
    for label in ("Project type", "LFG flow to project", "Host landfill", "Start year", "Rated capacity"):
        assert label not in rows, label
    assert ">None<" not in body and "None</" not in body and "<dd>—</dd>" not in body


def test_group_nearby_proposals_collapses_generator_units() -> None:
    """EIA-860M lists a project once per generator unit: same name, sponsor and county, one row
    per unit with its own capacity and distance. One list row, "× n units", summed capacity,
    nearest distance and that unit's link; unrelated rows and same-named rows in another county
    stay separate; order (nearest first, as the API returns it) is kept."""
    from web.app import group_nearby_proposals

    rows = [
        {
            "slug": "a-1",
            "name": "Danish Fields Solar",
            "sponsor": "Acme",
            "county": "Wharton",
            "capacity_mw": 100.0,
            "distance_km": 3.2,
        },
        {
            "slug": "b-1",
            "name": "Other Wind",
            "sponsor": "Beta",
            "county": "Wharton",
            "capacity_mw": 50.0,
            "distance_km": 4.0,
        },
        {
            "slug": "a-2",
            "name": "Danish Fields Solar",
            "sponsor": "Acme",
            "county": "Wharton",
            "capacity_mw": 200.0,
            "distance_km": 2.9,
        },
        {
            "slug": "a-3",
            "name": "Danish Fields Solar",
            "sponsor": "Acme",
            "county": "Wharton",
            "capacity_mw": None,
            "distance_km": 5.0,
        },
        {
            "slug": "a-x",
            "name": "Danish Fields Solar",
            "sponsor": "Acme",
            "county": "Matagorda",
            "capacity_mw": 10.0,
            "distance_km": 9.0,
        },
    ]

    grouped = group_nearby_proposals(rows)

    assert [g["slug"] for g in grouped] == ["a-2", "b-1", "a-x"]
    danish = grouped[0]
    assert danish["unit_count"] == 3
    assert danish["capacity_mw"] == 300.0
    assert danish["distance_km"] == 2.9
    assert grouped[1]["unit_count"] == 1 and grouped[1]["capacity_mw"] == 50.0
    assert grouped[2]["unit_count"] == 1 and grouped[2]["county"] == "Matagorda"
    # No capacity on any member -> None, not 0.
    assert (
        group_nearby_proposals([{"name": "x", "sponsor": None, "county": None, "capacity_mw": None}])[0][
            "capacity_mw"
        ]
        is None
    )
    assert group_nearby_proposals([]) == []


def test_group_nearby_proposals_keeps_nearest_units_nearest_asset() -> None:
    from web.app import group_nearby_proposals

    rows = [
        {
            "slug": "u1",
            "name": "P",
            "sponsor": "S",
            "county": "C",
            "capacity_mw": 1.0,
            "distance_km": 8.0,
            "nearest_asset_name": "Far Pipe",
            "nearest_asset_slug": "far",
        },
        {
            "slug": "u2",
            "name": "P",
            "sponsor": "S",
            "county": "C",
            "capacity_mw": 1.0,
            "distance_km": 1.5,
            "nearest_asset_name": "Near Pipe",
            "nearest_asset_slug": "near",
        },
    ]
    [g] = group_nearby_proposals(rows)
    assert g["distance_km"] == 1.5 and g["nearest_asset_slug"] == "near" and g["slug"] == "u2"


def test_asset_detail_nearby_list_shows_units_and_summed_capacity(web_client: TestClient) -> None:
    sponsor = {"name_canonical": "Acme Solar LLC"}
    units = [
        _nearby_row(
            public_id="prop_u1",
            slug="danish-fields-1",
            name_canonical="Danish Fields Solar",
            technology="solar",
            capacity_mw=100.0,
            distance_km=3.2,
            sponsor=sponsor,
            location={
                "geom": {"type": "Point", "coordinates": [-92.9, 43.5]},
                "state_code": "US-IA",
                "county_name": "Mitchell",
                "precision": "exact",
            },
        ),
        _nearby_row(
            public_id="prop_u2",
            slug="danish-fields-2",
            name_canonical="Danish Fields Solar",
            technology="solar",
            capacity_mw=200.0,
            distance_km=2.9,
            sponsor=sponsor,
            location={
                "geom": {"type": "Point", "coordinates": [-92.91, 43.51]},
                "state_code": "US-IA",
                "county_name": "Mitchell",
                "precision": "exact",
            },
        ),
        _nearby_row(
            public_id="prop_o",
            slug="lone-wind",
            name_canonical="Lone Wind",
            technology="wind",
            capacity_mw=50.0,
            distance_km=6.0,
        ),
    ]
    _install(_fuel_transport(_ethanol_entity(), nearby=units))

    body = web_client.get("/assets/absolute-energy-llc-st-ansgar-ia-us-ia").text

    nearby = body.split('aria-label="Nearby proposals"')[1].split("</section>")[0]
    assert nearby.count("<li>") == 2
    assert "&times; 2 units" in nearby and "300.0 MW" in nearby and "2.9 km away" in nearby
    assert 'href="/proposals/danish-fields-2"' in nearby  # the nearest unit's page
    assert "100.0 MW" not in nearby and "200.0 MW" not in nearby
    assert "Lone Wind" in nearby and "units" not in nearby.split("Lone Wind")[1]
    # The mini-map still draws every unit's dot: grouping is a list rule, not a map rule.
    geojson = body.split('id="asset-map-data">')[1].split("</script>")[0]
    assert geojson.count('"kind":"proposal"') == 3
    assert "3 exact-grade proposals within 25" in body


def test_org_descriptor_from_holdings() -> None:
    from web.app import org_descriptor

    assert org_descriptor("other", {"ethanol_plant": 1}) == "Ethanol producer"
    assert org_descriptor("other", {"rng_project": 4}) == "RNG developer"
    assert (
        org_descriptor("other", {"gas_pipeline": 3, "power_plant": 12})
        == "Power plant owner and Gas pipeline operator"
    )
    # Top two by count; ties fall back to the descriptor table's order.
    assert (
        org_descriptor("other", {"ethanol_plant": 2, "rng_project": 2, "power_plant": 1})
        == "Ethanol producer and RNG developer"
    )
    # A real type is left alone; no holdings -> nothing to say; unknown types contribute nothing.
    assert org_descriptor("developer", {"ethanol_plant": 1}) is None
    assert org_descriptor("other", {}) is None
    assert org_descriptor("other", {"widget": 5}) is None
    assert org_descriptor(None, {"gas_pipeline": 1}) == "Gas pipeline operator"
    # Two types that share a descriptor collapse to one.
    assert org_descriptor("other", {"gas_pipeline": 1, "compressor_station": 9}) == "Gas pipeline operator"


def test_organization_detail_shows_descriptor_for_other_typed_holder_and_keeps_raw_type(
    web_client: TestClient,
) -> None:
    org = _tallgrass_org(
        public_id="org_01POET",
        slug="poet-biorefining-north-manches",
        name_canonical="Poet Biorefining - North Manches",
        type="other",
        subsidiaries=[],
        subsidiary_count=0,
        asset_counts={
            "assets": 1,
            "by_role": {"operator": 1},
            "by_type": {"ethanol_plant": 1},
            "by_role_and_type": {"operator": {"ethanol_plant": 1}},
        },
    )
    asset_row = {
        "asset": _ethanol_entity(
            public_id="asset_01POET",
            slug="poet-north-manchester",
            name="Poet Biorefining (North Manchester, IN)",
        ),
        "role": "operator",
        "share_pct": None,
    }
    _install(
        _default_transport(
            {
                "/v1/organizations": (200, {"data": [org]}),
                "/v1/organizations/org_01POET/assets": (200, {"data": [asset_row]}),
                "/v1/organizations/org_01POET/proposals": (200, {"data": []}),
                "/v1/organizations/org_01POET/opportunities": (200, {"data": []}),
                "/v1/organizations/org_01POET/nearby-proposals": (200, {"data": []}),
            }
        )
    )

    body = web_client.get("/organizations/poet-biorefining-north-manches").text

    header = body.split('<div class="detail-header">')[1].split("</div>")[0]
    assert 'id="org-descriptor">Ethanol producer</span>' in header
    assert ">other<" not in header
    assert "<dt>Type</dt><dd>other</dd>" in body  # the raw type stays in the fields table
    summary = body.split('id="org-summary">')[1].split("</p>")[0]
    assert summary == "Operates 1 ethanol plant"
    assert ">None<" not in body


def test_organization_detail_keeps_a_real_type_badge(web_client: TestClient) -> None:
    _install(_tallgrass_transport(type="pipeline_operator"))

    body = web_client.get("/organizations/tallgrass-energy").text

    header = body.split('<div class="detail-header">')[1].split("</div>")[0]
    assert 'id="org-descriptor"' not in header and ">pipeline operator<" in header


def test_organization_detail_two_part_descriptor_counts_rows_without_asset_counts(
    web_client: TestClient,
) -> None:
    _install(_tallgrass_transport(type="other", asset_counts=None))

    body = web_client.get("/organizations/tallgrass-energy").text

    header = body.split('<div class="detail-header">')[1].split("</div>")[0]
    # 3 pipelines, 1 processing plant, 1 power plant on the page: pipelines first, then the tie
    # between processing and power plant falls to the table order (gas processing first).
    assert 'id="org-descriptor">Gas pipeline operator and Gas processing operator</span>' in header


def test_organization_nearby_list_groups_generator_units(web_client: TestClient) -> None:
    sponsor = {"name_canonical": "Acme Solar LLC"}
    nearest = {
        "public_id": "asset_01REX",
        "slug": "rockies-express-pipeline",
        "name": "Rockies Express Pipeline",
    }
    rows = [
        _nearby_row(
            public_id="prop_u1",
            slug="unit-1",
            name_canonical="Prairie Solar",
            capacity_mw=80.0,
            distance_km=4.0,
            sponsor=sponsor,
            nearest_asset=nearest,
        ),
        _nearby_row(
            public_id="prop_u2",
            slug="unit-2",
            name_canonical="Prairie Solar",
            capacity_mw=70.0,
            distance_km=3.5,
            sponsor=sponsor,
            nearest_asset=nearest,
        ),
    ]
    transport = _tallgrass_transport()
    transport.responses["/v1/organizations/org_01TALLGRASS/nearby-proposals"] = (200, {"data": rows})
    _install(transport)

    body = web_client.get("/organizations/tallgrass-energy").text

    nearby = body.split('aria-label="Nearby proposals"')[1].split("</section>")[0]
    assert nearby.count("<li>") == 1
    assert "&times; 2 units" in nearby and "150.0 MW" in nearby and "3.5 km away" in nearby
    assert 'href="/proposals/unit-2"' in nearby and "Rockies Express Pipeline" in nearby


def test_search_assets_section_labels_ethanol_and_rng_types(web_client: TestClient) -> None:
    transport = _default_transport(
        {
            "/v1/proposals": (200, {"data": []}),
            "/v1/opportunities": (200, {"data": []}),
            "/v1/organizations": (200, {"data": []}),
            "/v1/assets": (
                200,
                {"data": [_ethanol_entity(), _lmop_entity(technology="lfg_electricity"), _agstar_entity()]},
            ),
        }
    )
    _install(transport)

    body = web_client.get("/search?q=energy").text

    section = body.split("<h2>Assets</h2>")[1].split("<h2>Organisations</h2>")[0]
    assert "3 assets" in body
    ethanol = section.split("Absolute Energy LLC (St Ansgar, IA)</a>")[1].split("</li>")[0]
    assert "Ethanol plant · Absolute Energy LLC · US-IA" in ethanol
    lmop = section.split("City of Charleston Landfill</a>")[1].split("</li>")[0]
    assert "RNG project · Landfill gas to electricity · Tallarico Energy · US-WV" in lmop
    agstar = section.split("Tinedale Farms Digester</a>")[1].split("</li>")[0]
    assert "RNG project · Farm digester · US-WI" in agstar
    assert "—" not in section and ">None<" not in section


def test_asset_detail_proxy_relays_the_detail_envelope_without_cookies(web_client: TestClient) -> None:
    """The drawer's enrichment call (`/api/assets/{public_id}`): geo point features carry no
    capacity value, unit or attributes, so the drawer fetches the detail row and re-renders."""
    entity = _lmop_entity()
    transport = _default_transport({"/v1/assets/asset_01LMOP": (200, {"data": entity})})
    _install(transport)

    resp = web_client.get("/api/assets/asset_01LMOP", cookies={"session": "secret"})

    assert resp.status_code == 200
    assert resp.json()["data"]["attributes"]["lfg_flow_to_project_mmscfd"] == 0.677
    call = next(c for c in transport.calls if c[1] == "/v1/assets/asset_01LMOP")
    assert not call[3]  # no cookies forwarded
    assert call[2] == {}
    # The geo proxy still answers its own route: `/api/assets/geo` is not swallowed by the id route.
    transport.responses["/v1/assets/geo"] = (200, {"data": {"type": "FeatureCollection", "features": []}})
    assert web_client.get("/api/assets/geo?bbox=-1,-1,1,1&zoom=4").json()["data"]["features"] == []


def test_asset_detail_proxy_relays_api_error_status(web_client: TestClient) -> None:
    transport = _default_transport({"/v1/assets/asset_MISSING": (404, {"title": "not_found"})})
    _install(transport)

    resp = web_client.get("/api/assets/asset_MISSING")

    assert resp.status_code == 404
    assert resp.json()["title"] == "not_found"


def test_map_script_enables_ethanol_and_rng_with_fuel_rows_and_unit_grouping() -> None:
    """No JS harness (see `test_map_js_reads_placement_from_url_and_writes_it_back`): the shipped
    script lists both fuel types as live, carries the RNG family words, the fuel drawer rows and
    their detail enrichment, the tooltip family line, and the in-view unit grouping."""
    from pathlib import Path

    source = (Path(__file__).parent / "static" / "js" / "map.js").read_text()
    live = source.split("var ASSET_TYPES_LIVE = [")[1].split("]")[0]
    assert '"ethanol_plant"' in live and '"rng_project"' in live
    for word in (
        "Landfill gas to electricity",
        "Landfill gas direct use",
        "Renewable natural gas",
        "Farm digester",
    ):
        assert word in source, word
    for label in (
        "Nameplate capacity",
        "Capacity as of",
        "Project type",
        "LFG flow to project",
        "Biogas generation (est.)",
        "Host landfill",
        "Digester type",
        "Start year",
        "Shutdown year",
    ):
        assert '"' + label + '"' in source, label
    assert "function fuelRows(p)" in source and "fuelRows(p).map" in source
    assert 'fetch("/api/assets/" + encodeURIComponent(id))' in source
    assert "function groupProposalFeatures(" in source and '"× " + g.unit_count + " units"' in source
    assert "rngTechLabel(p.technology)" in source and "esc(family)" in source
    # Escaping discipline holds for the new rows: every fuel value passes through esc().
    assert "row(r[0], esc(r[1]), r[2])" in source
    # `Number(null)` is 0, so an absent value must be caught before it is formatted -- otherwise
    # the "None" gate leaks a real-looking "0 MMgal/yr" row (found driving the drawer).
    assert 'if (value == null || value === "") return null;' in source
    # "In view" counts what came back, not `/v1/assets/geo`'s dataset-wide `totals.records`
    # (it does not narrow to the bbox); a cluster stands for its own `count`.
    assert "function assetsInView(features)" in source
    assert "latestPlantsTotal = assetsInView(features);" in source
    css = (Path(__file__).parent / "static" / "css" / "styles.css").read_text()
    assert ".in-view-list__units" in css and ".legend__note" in css
