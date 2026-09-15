"""Tests for the map task's Sprint 4 additions: the basemap (`MAP_TILE_URL` modes), the existing-
plants context layer's same-origin proxy, `/api/ui-events`, `/attribution`, and which regional
quick-view buttons `home_map()` renders.

Every test here drives `web.app.app` against a small hand-written fake `Transport`
(`web/api_client.py`'s `Transport` protocol) rather than a real API app -- no database, no
`services.api` import at all -- to keep these fast, isolated tests of this file's own proxy/route
logic (forwarding, cookie-stripping, defaulting, error relay), independent of the API lane's own
test suite. `GET /v1/context/plants/geo` and `POST /v1/ui-events` (task contract,
`docs/00-PLAN.md` 2026-09-15) had not landed on `services/api` when this module was first written
(the "monkeypatch `ApiClient`" case the task brief calls out) and landed in a parallel lane
partway through (`docs/CHANGELOG.md` 2026-09-15 backend-developer) — the fakes' response shapes
below were then cross-checked field-for-field against the real `services/api/context_geo.py` and
`services/api/ui_events.py`, and `web/test_e2e.py`'s whole suite now runs both proxies against the
real, mounted, in-process API. `web/test_auth.py`'s `auth.registered` tests drive the real
`POST /v1/ui-events` directly, asserting on the `UiEvent` row it writes.
"""

from __future__ import annotations

import json as jsonlib
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.regions import REGIONS, jurisdiction_matches_region, regions_with_data

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


def _source(
    source_id: str,
    *,
    jurisdiction: str | None,
    publish_state: str = "public",
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "name": name or source_id,
        "jurisdiction": jurisdiction,
        "category": "generation_queue",
        "operator": "Example Operator",
        "url": f"https://example.org/{source_id}",
        "access": "api",
        "cadence": "daily",
        "tier": 1,
        "licence": {
            "licence_id": "lic_example",
            "name": "Example Licence",
            "url": "https://example.org/licence",
            "reuse_class": "open",
            "attribution_required": True,
            "attribution_text": "Example credit",
            "requires_link_back": False,
            "allows_derived_publication": True,
            "allows_raw_publication": True,
            "allows_api_redistribution": True,
            "allows_bulk_export": True,
            "allows_commercial_use": True,
            "share_alike": False,
            "gate_flag": False,
        },
        "attribution_text": "Example credit",
        "publish_state": publish_state,
        "lag_days": None,
        "lag_overrides": {},
        "implemented": True,
        "last_success_at": None,
        "provenance": {"manifest_version": "2026-09-12", "manifest_hash": "0" * 64},
    }


class FakeTransport:
    """A minimal stand-in for `web/api_client.py`'s `Transport` protocol: canned responses keyed
    by path, every call recorded (method, url, payload, cookies) so a test can assert what a proxy
    route forwarded -- in particular, that it never forwards the caller's cookies."""

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


def _default_transport(sources: list[dict[str, Any]] | None = None) -> FakeTransport:
    return FakeTransport(
        {
            "/v1/health": (200, DEFAULT_HEALTH),
            "/v1/meta/vocabularies": (200, DEFAULT_VOCAB),
            "/v1/sources": (200, {"data": sources if sources is not None else []}),
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


# ---------------------------------------------------------------------- web/regions.py (unit)
def test_jurisdiction_matches_region_is_a_case_insensitive_prefix_match() -> None:
    assert jurisdiction_matches_region("US-TX", "us")
    assert jurisdiction_matches_region("US+CA", "us")
    assert jurisdiction_matches_region("GB (England/Wales)", "gb")
    assert jurisdiction_matches_region("CA-ON", "ca")
    assert not jurisdiction_matches_region("EU/EEA", "us")
    assert not jurisdiction_matches_region(None, "us")


def test_regions_with_data_excludes_gated_and_unpublished_sources() -> None:
    sources = [
        _source("us.iso.ercot.gen_queue", jurisdiction="US-TX", publish_state="public"),
        _source("gb.neso.tec_register", jurisdiction="GB", publish_state="ingest_only"),
        _source("us.iso.pjm.gen_queue", jurisdiction="US-PJM", publish_state="api_only"),
    ]
    regions = regions_with_data(sources)
    codes = [r.code for r in regions]
    assert codes == ["us"]  # order follows REGIONS, not input order
    assert "gb" not in codes  # gated (ingest_only)


def test_regions_with_data_returns_nothing_for_no_published_sources() -> None:
    assert regions_with_data([]) == []
    assert regions_with_data([_source("x", jurisdiction="US", publish_state="ingest_only")]) == []


def test_region_table_has_the_five_owner_named_regions() -> None:
    assert [r.code for r in REGIONS] == ["us", "gb", "eu", "ca", "au"]


# --------------------------------------------------------------------- home_map(): tile config
def test_home_map_dev_mode_when_map_tile_url_unset(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAP_TILE_URL", raising=False)
    _install(_default_transport())
    resp = web_client.get("/")
    assert resp.status_code == 200
    assert 'data-tile-mode="dev"' in resp.text
    assert "pmtiles@4.5.0" not in resp.text
    assert "@protomaps/basemaps" not in resp.text


def test_home_map_pmtiles_mode_reaches_template_and_loads_extra_scripts(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tile_url = "https://tiles.example.com/basemap.pmtiles"
    monkeypatch.setenv("MAP_TILE_URL", tile_url)
    _install(_default_transport())
    resp = web_client.get("/")
    assert resp.status_code == 200
    assert f'data-tile-url="{tile_url}"' in resp.text
    assert 'data-tile-mode="pmtiles"' in resp.text
    assert "pmtiles@4.5.0" in resp.text
    assert "@protomaps/basemaps@5.7.2" in resp.text
    assert "Basemap: Protomaps" in resp.text


def test_home_map_raster_mode_names_the_tile_host_in_attribution(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAP_TILE_URL", "https://api.maptiler.example/tiles/{z}/{x}/{y}.png")
    _install(_default_transport())
    resp = web_client.get("/")
    assert resp.status_code == 200
    assert 'data-tile-mode="raster"' in resp.text
    assert "api.maptiler.example" in resp.text
    assert "pmtiles@4.5.0" not in resp.text


# -------------------------------------------------------------- home_map(): regional quick views
def test_home_map_only_shows_regions_with_published_data(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAP_TILE_URL", raising=False)
    sources = [
        _source("us.iso.ercot.gen_queue", jurisdiction="US-TX", publish_state="public"),
        _source("gb.neso.tec_register", jurisdiction="GB", publish_state="ingest_only"),
    ]
    _install(_default_transport(sources))
    resp = web_client.get("/")
    assert resp.status_code == 200
    assert 'data-region="us"' in resp.text
    assert 'data-region="gb"' not in resp.text


def test_home_map_shows_no_region_group_when_nothing_published(
    web_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAP_TILE_URL", raising=False)
    _install(_default_transport([]))
    resp = web_client.get("/")
    assert resp.status_code == 200
    assert "Jump to region" not in resp.text


# ------------------------------------------------------------------------------ plants geo proxy
def test_plants_geo_proxy_forwards_bbox_zoom_technology_and_strips_cookies(
    web_client: TestClient,
) -> None:
    transport = _default_transport()
    transport.responses["/v1/context/plants/geo"] = (
        200,
        {"data": {"type": "FeatureCollection", "features": [], "totals": {"records": 0}}, "meta": {}},
    )
    _install(transport)

    resp = web_client.get(
        "/api/context/plants/geo",
        params={"bbox": "1,2,3,4", "zoom": "6", "technology": "solar"},
        cookies={"session": "abc"},
    )

    assert resp.status_code == 200
    calls = [c for c in transport.calls if c[1] == "/v1/context/plants/geo"]
    assert len(calls) == 1
    _method, _url, params, cookies = calls[0]
    assert params == {"bbox": "1,2,3,4", "zoom": "6", "technology": "solar"}
    assert cookies is None  # never forwards the browser's cookies


def test_plants_geo_proxy_defaults_bbox_and_zoom_when_absent(web_client: TestClient) -> None:
    transport = _default_transport()
    transport.responses["/v1/context/plants/geo"] = (
        200,
        {"data": {"type": "FeatureCollection", "features": [], "totals": {}}, "meta": {}},
    )
    _install(transport)

    resp = web_client.get("/api/context/plants/geo")

    assert resp.status_code == 200
    _method, _url, params, _cookies = transport.calls[-1]
    assert params["zoom"] == "3"
    assert "technology" not in params


def test_plants_geo_proxy_relays_api_error_status(web_client: TestClient) -> None:
    transport = _default_transport()
    transport.responses["/v1/context/plants/geo"] = (400, {"title": "bad_request"})
    _install(transport)

    resp = web_client.get("/api/context/plants/geo")

    assert resp.status_code == 400


# ---------------------------------------------------------------------------------- ui-events
def test_ui_events_proxy_forwards_name_and_props_and_returns_202(web_client: TestClient) -> None:
    transport = _default_transport()
    transport.responses["/v1/ui-events"] = (202, {})
    _install(transport)

    resp = web_client.post(
        "/api/ui-events",
        content=jsonlib.dumps({"name": "map.layer_toggled", "props": {"layer": "plants", "on": True}}),
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 202
    calls = [c for c in transport.calls if c[1] == "/v1/ui-events"]
    assert len(calls) == 1
    _method, _url, body, cookies = calls[0]
    assert body == {"name": "map.layer_toggled", "props": {"layer": "plants", "on": True}}
    assert cookies is None


def test_ui_events_proxy_returns_202_even_when_api_call_fails(web_client: TestClient) -> None:
    transport = _default_transport()
    transport.responses["/v1/ui-events"] = (500, {"title": "error"})
    _install(transport)

    resp = web_client.post(
        "/api/ui-events",
        content=jsonlib.dumps({"name": "map.basemap_failed", "props": {}}),
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 202


def test_ui_events_proxy_returns_202_on_malformed_body_without_calling_the_api(
    web_client: TestClient,
) -> None:
    transport = _default_transport()
    _install(transport)

    resp = web_client.post(
        "/api/ui-events", content=b"not json", headers={"content-type": "application/json"}
    )

    assert resp.status_code == 202
    assert not any(c[1] == "/v1/ui-events" for c in transport.calls)


# ---------------------------------------------------------------------------------- attribution
def test_attribution_page_renders_sources_and_basemap_and_context_sections(
    web_client: TestClient,
) -> None:
    sources = [_source("us.eia.860m", jurisdiction="US", name="EIA-860M")]
    _install(_default_transport(sources))

    resp = web_client.get("/attribution")

    assert resp.status_code == 200
    body = resp.text
    assert "EIA-860M" in body
    assert "Example Operator" in body
    assert "Example Licence" in body
    assert "Basemap" in body
    assert "Protomaps" in body
    assert "OpenStreetMap contributors" in body
    assert "Natural Earth" in body
    assert "Context layers" in body


def test_footer_links_to_attribution(web_client: TestClient) -> None:
    _install(_default_transport())
    resp = web_client.get("/about")
    assert resp.status_code == 200
    assert 'href="/attribution"' in resp.text
