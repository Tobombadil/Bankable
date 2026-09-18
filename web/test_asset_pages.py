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


def test_home_map_has_asset_type_select_with_only_power_plant_enabled(web_client: TestClient) -> None:
    _install(_default_transport())
    resp = web_client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="mf-asset-type"' in body
    assert "Existing assets (EIA)" in body
    assert '<option value="power_plant">Power plant</option>' in body
    assert 'value="gas_pipeline" disabled title="coming"' in body
    assert 'value="refinery" disabled title="coming"' in body


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
