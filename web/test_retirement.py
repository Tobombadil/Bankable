"""Lane R1 on the site: the map's "Retired & retiring plants" layer, the asset page's Retirement
section, and the words both use instead of tokens. Drives `web.app.app` against a fake transport,
like `web/test_asset_pages.py` (self-contained by this repo's convention)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.retirement import INTERCONNECTION_REUSE_NOTE, month_label, retirement_view

HEALTH = {"status": "ok", "data_as_of": "2026-09-01", "live_as_of": "2026-09-15T00:00:00Z"}
VOCAB: dict[str, Any] = {
    "data": {"technology": [], "proposal_kind": [], "opportunity_kind": [], "opportunity_status": []}
}


class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, Any]] = []

    def _respond(self, url: str) -> httpx.Response:
        status, body = self.responses.get(url, (404, {"title": "not_found"}))
        return httpx.Response(status, json=body)

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: Any = None
    ) -> httpx.Response:
        self.calls.append((url, dict(params or {})))
        return self._respond(url)

    def post(self, url: str, *, json: Any = None, cookies: Any = None) -> httpx.Response:
        self.calls.append((url, json))
        return self._respond(url)

    def request(
        self, method: str, url: str, *, json: Any = None, params: Any = None, cookies: Any = None
    ) -> httpx.Response:
        self.calls.append((url, json if json is not None else dict(params or {})))
        return self._respond(url)

    def close(self) -> None:
        pass


@pytest.fixture()
def web_client() -> Iterator[TestClient]:
    with TestClient(web_app) as client:
        yield client
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


def _install(extra: Mapping[str, tuple[int, Any]]) -> FakeTransport:
    transport = FakeTransport({"/v1/health": (200, HEALTH), "/v1/meta/vocabularies": (200, VOCAB), **extra})
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    return transport


ROCKPORT_RETIREMENT = {
    "status_rule": "majority_mw_retiring",
    "in_service_mw": 2600.0,
    "retiring_mw": 2600.0,
    "retiring_units": 2,
    "retiring_share": 1.0,
    "next_planned": "2028-12",
    "last_planned": "2028-12",
    "retired_mw": 0.0,
    "retired_units": 0,
    "first_retired": None,
    "last_retired": None,
    "by_technology": [{"technology": "Conventional Steam Coal", "retired_mw": 0.0, "retiring_mw": 2600.0}],
    "units": [
        {"generator_id": g, "technology": "Conventional Steam Coal", "capacity_mw": 1300.0,
         "state": "retiring", "date": "2028-12"}
        for g in ("1", "2")
    ],
    "as_of": "2026-08",
}  # fmt: skip


def _plant(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "asset_01ROCKPORT",
        "slug": "rockport-us-in",
        "name": "Rockport",
        "asset_type": "power_plant",
        "status": "retiring",
        "retirement_year": 2028,
        "operator_name": "Indiana Michigan Power Co",
        "technology": "coal",
        "technology_raw": "Conventional Steam Coal",
        "technologies": {"Conventional Steam Coal": 2600.0},
        "capacity_mw": 2600.0,
        "state_code": "US-IN",
        "county_name": "Spencer",
        "country": "US",
        "geometry": {"type": "Point", "coordinates": [-87.0336, 37.9256]},
        "attributes": {
            "balancing_authority_code": "PJM",
            "retirement": ROCKPORT_RETIREMENT,
            "grid": {
                "nerc_region": "RFC",
                "balancing_authority_name": "PJM Interconnection, LLC",
                "transmission_owner": "Indiana Michigan Power Co",
                "grid_voltage_kv": [765.0],
                "report_year": 2025,
                "source_id": "us.eia.860",
            },
        },
        "retirement_changes": [
            {
                "event_type": "retirement_date_changed",
                "observed_at": "2026-10-27T15:15:00Z",
                "summary": "x",
                "units": [{"generator_id": "1", "technology": "Conventional Steam Coal",
                           "capacity_mw": 1300.0, "date_before": "2028-06", "date_after": "2028-12"}],
                "capacity_mw": 1300.0,
                "plant_status": "retiring",
            }
        ],
        "owners": [],
        "provenance": [
            {"source_id": "us.eia.860m", "source_name": "EIA-860M", "source_url": "https://www.eia.gov/electricity/data/eia860m/",
             "retrieved_at": "2026-09-27T00:00:00Z", "reuse_class": "open", "source_record_id": "6166"}
        ],
    }  # fmt: skip
    base.update(overrides)
    return base


GRID = {
    "data": {
        "radius_km": 25.0,
        "transmission_lines": [
            {
                "public_id": "asset_01LINE",
                "slug": "rockport-jefferson-765",
                "url": "https://infraque.com/assets/rockport-jefferson-765",
                "name": "Rockport - Jefferson",
                "voltage_kv": 765.0,
                "distance_km": 0.4,
            }
        ],
        "interconnection_points": [
            {
                "public_id": "poi_01RP",
                "url": "https://infraque.com/interconnection-points/poi_01RP",
                "name": "Rockport 765kV",
                "iso": "MISO",
                "kind": "substation",
                "voltage_kv": 765.0,
                "proposals_nearby": 2,
                "nearest_proposal_km": 1.2,
            }
        ],
    }
}


def _detail(web_client: TestClient, entity: dict[str, Any], grid: Any = GRID) -> tuple[str, FakeTransport]:
    transport = _install(
        {
            "/v1/assets": (200, {"data": [entity]}),
            f"/v1/assets/{entity['public_id']}": (200, {"data": entity}),
            f"/v1/assets/{entity['public_id']}/nearby-proposals": (200, {"data": []}),
            f"/v1/assets/{entity['public_id']}/nearby-grid": (200, grid) if grid is not None else (500, {}),
        }
    )
    resp = web_client.get(f"/assets/{entity['slug']}")
    assert resp.status_code == 200, resp.text
    return resp.text, transport


def _section(body: str) -> str:
    m = re.search(r'<section aria-label="Retirement".*?</section>', body, re.S)
    assert m is not None, "no Retirement section"
    return m.group(0)


def test_retiring_plant_page_has_a_retirement_section_in_words(web_client: TestClient) -> None:
    body, transport = _detail(web_client, _plant())
    section = _section(body)
    for words in (
        "Retiring",
        "Next scheduled retirement",
        "December 2028",
        "2,600 MW",
        "Conventional Steam Coal",
        "765 kV",
        "Indiana Michigan Power Co",
        "RFC",
        "Rockport - Jefferson",
        "Rockport 765kV",
        "Planned retirement date moved",
        "June 2028 to December 2028",
        "FERC Order No. 845",
    ):
        assert words in section, words
    # Tokens never reach the page.
    for token in ("retiring_mw", "majority_mw_retiring", "retirement_date_changed", "2028-12", ">retiring<"):
        assert token not in section, token
    assert "may be reusable" in section and "does not establish" in section
    assert any(url.endswith("/nearby-grid") for url, _ in transport.calls)
    # Nor anywhere else on the page: the Attributes table leaves both blocks to the section.
    for token in ("majority_mw_retiring", "us.eia.860<", "retiring_share"):
        assert token not in body, token
    # The header badge and the Status row print the label, not the token.
    assert "&middot; Retiring</span>" in body


def test_retired_plant_says_when_and_how_much(web_client: TestClient) -> None:
    retired = dict(ROCKPORT_RETIREMENT)
    retired.update(
        status_rule="all_units_retired",
        in_service_mw=0.0,
        retiring_mw=0.0,
        retiring_units=0,
        retiring_share=None,
        next_planned=None,
        last_planned=None,
        retired_mw=2012.0,
        retired_units=3,
        first_retired="2023-07",
        last_retired="2024-04",
        units=[],
        by_technology=[{"technology": "Conventional Steam Coal", "retired_mw": 2012.0, "retiring_mw": 0.0}],
    )
    entity = _plant(
        public_id="asset_01HOMER",
        slug="homer-city-us-pa",
        name="Homer City Generating Station",
        status="retired",
        retirement_year=2024,
        attributes={"retirement": retired},
        retirement_changes=[],
    )
    body, _ = _detail(
        web_client,
        entity,
        grid={"data": {"radius_km": 25.0, "transmission_lines": [], "interconnection_points": []}},
    )
    section = _section(body)
    assert "April 2024" in section and "July 2023" in section and "2,012 MW" in section
    assert "Every generator EIA lists at this plant has retired." in section
    assert "states no grid voltage" in section  # not in the 2025 annual file
    assert INTERCONNECTION_REUSE_NOTE[:60] in section


def test_a_plant_with_no_retirement_has_no_section_and_no_grid_call(web_client: TestClient) -> None:
    entity = _plant(
        status="operating",
        retirement_year=None,
        attributes={"balancing_authority_code": "ERCO"},
        retirement_changes=[],
    )
    body, transport = _detail(web_client, entity)
    assert 'aria-label="Retirement"' not in body
    assert not any(url.endswith("/nearby-grid") for url, _ in transport.calls)


def test_a_failed_grid_call_keeps_the_dates(web_client: TestClient) -> None:
    body, _ = _detail(web_client, _plant(), grid=None)
    section = _section(body)
    assert "December 2028" in section and "Rockport - Jefferson" not in section


def test_month_labels() -> None:
    assert month_label("2028-12") == "December 2028"
    assert month_label("2032") == "2032"
    assert month_label("soon") is None and month_label(None) is None


def test_retirement_view_is_none_for_other_asset_types() -> None:
    assert (
        retirement_view({"asset_type": "gas_pipeline", "attributes": {"retirement": ROCKPORT_RETIREMENT}})
        is None
    )


# ------------------------------------------------------------------------------------------ map
def test_map_has_the_retired_layer_toggle_legend_and_status_labels(web_client: TestClient) -> None:
    _install({"/v1/sources": (200, {"data": []})})
    resp = web_client.get("/?layers=retired")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="mf-layer-retired"' in body and "Retired &amp; retiring plants" in body
    # The marks take their colour from classes, not `style` attributes (the CSP allows none, docs/60 §2).
    assert 'id="retired-legend"' in body
    assert "legend__item--retired" in body and "legend__item--retiring" in body
    css = (Path(__file__).parent / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert ".legend__item--retired { color: var(--asset-retired); }" in css.replace(
        '.legend__item[data-legend-mark="retired-cluster"], ', ""
    )
    assert ".legend__item--retiring { color: var(--asset-retiring); }" in css
    labels = json.loads(
        re.search(r'<script type="application/json" id="map-labels">(.*?)</script>', body, re.S).group(1)
    )  # type: ignore[union-attr]
    assert labels["asset_status"]["retiring"] == "Retiring" and labels["asset_status"]["retired"] == "Retired"


def test_map_script_keeps_retired_plants_off_the_existing_layer_and_asks_for_them_apart() -> None:
    source = (Path(__file__).parent / "static" / "js" / "map.js").read_text()
    assert 'var EXISTING_PLANT_STATUSES = ["operating", "standby", "retiring", "unknown"];' in source
    assert 'var RETIRED_LAYER_STATUSES = ["retired", "retiring"];' in source
    assert 'params.set("status", RETIRED_LAYER_STATUSES.join(","));' in source
    assert '"retired-crossed"' in source and '"retiring-dot"' in source
    # Status words come from the server's labels, never the raw token.
    assert "statusName(p.status)" in source


def test_stylesheet_defines_the_retired_tokens_in_light_and_both_dark_blocks() -> None:
    css = (Path(__file__).parent / "static" / "css" / "styles.css").read_text()
    for token in ("--asset-retired", "--asset-retiring"):
        assert css.count(f"{token}:") == 3, token


def test_assets_geo_proxy_forwards_status(web_client: TestClient) -> None:
    transport = _install(
        {"/v1/assets/geo": (200, {"data": {"type": "FeatureCollection", "features": [], "totals": {}}})}
    )
    resp = web_client.get(
        "/api/assets/geo", params={"asset_type": "power_plant", "status": "retired,retiring"}
    )
    assert resp.status_code == 200
    calls = [params for url, params in transport.calls if url == "/v1/assets/geo"]
    assert calls[-1]["status"] == "retired,retiring"


def test_asset_index_forwards_the_retirement_filters_and_labels_the_status(web_client: TestClient) -> None:
    row = _plant(
        status="retired", retirement_year=2024, name="Homer City Generating Station", slug="homer-city-us-pa"
    )
    transport = _install(
        {
            "/v1/assets": (200, {"data": [row], "page": {"has_more": False, "next_cursor": None}}),
            "/v1/assets/geo": (200, {"data": {"type": "FeatureCollection", "features": [], "totals": {}}}),
            "/v1/coverage": (200, {"data": {}}),
        }
    )
    params = {"asset_type": "power_plant", "status": "retired,retiring", "retirement_year[gte]": "2020"}
    resp = web_client.get("/assets", params=params)
    assert resp.status_code == 200, resp.text
    sent = [p for url, p in transport.calls if url == "/v1/assets"][-1]
    assert sent["status"] == "retired,retiring" and sent["retirement_year[gte]"] == "2020"
    assert "(retired 2024)" in resp.text


# ------------------------------------------------- 2026-10-07: retired clusters vs proposal clusters
# At national and regional zoom the retired layer is all clusters, and they were drawn as the same
# ring-with-a-number a proposals cluster is, told apart only by hue (magenta against navy): D-5
# breaks, and under deuteranopia the magenta sits within 7-17 CIELAB units of the neutral and
# progress family hues. A retired cluster is now a dashed ring; the legend shows that mark.
_WEB = Path(__file__).parent
_MAP_JS = (_WEB / "static" / "js" / "map.js").read_text(encoding="utf-8")
_CSS = (_WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")


def _css_block(start: str) -> dict[str, str]:
    i = _CSS.index(start)
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", _CSS[i : _CSS.index("}", i)]))


def _contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        c = [int(h[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


_LIGHT = _css_block(":root {")
_THEMES = {
    "light": _LIGHT,
    "dark (media)": {**_LIGHT, **_css_block(':root:not([data-theme="light"]) {')},
    "dark (toggle)": {**_LIGHT, **_css_block(':root[data-theme="dark"] {')},
}


def test_retired_clusters_are_dashed_rings_and_proposal_clusters_stay_solid() -> None:
    retired = _MAP_JS.split("function addRetiredLayers() {")[1].split("\n  }\n")[0]
    assert 'id: "retired-clusters", type: "symbol"' in retired
    assert '"icon-image": "retired-cluster"' in retired
    assert "circle-stroke" not in retired  # the old solid ring
    image = _MAP_JS.split("function addRetiredClusterImage() {")[1].split("\n  }\n")[0]
    assert "ctx.setLineDash([" in image and "retiredColors.retired" in image and "mapColors.land" in image
    proposals = _MAP_JS.split('id: "clusters", type: "circle"')[1].split("});")[0]
    assert '"circle-stroke-width": 2' in proposals and "dash" not in proposals.lower()
    # Hover says what the dashed ring is, as a proposals cluster's tooltip does (D-10).
    assert 'map.on("mouseenter", "retired-clusters"' in _MAP_JS
    assert '"retired or retiring plant"' in _MAP_JS


def test_the_retired_key_shows_the_cluster_mark_and_follows_the_layer(web_client: TestClient) -> None:
    _install({"/v1/sources": (200, {"data": []})})
    body = web_client.get("/?layers=retired").text
    legend = body.split('id="retired-legend"')[1].split("</div>\n    </div>")[0]
    assert 'role="group" aria-label="Retired and retiring plants key"' in legend
    cluster = re.search(
        r'<span class="legend__item" data-legend-mark="retired-cluster"[^>]*>(.*?)</span>', legend, re.S
    )
    assert cluster is not None and "stroke-dasharray" in cluster.group(1)
    assert "Dashed ring with a number" in cluster.group(1)
    assert "Retiring (most capacity scheduled to retire)" in legend and "> Retired</span>" in legend
    lifecycle = body.split('id="lifecycle-legend"')[1].split("</div>")[0]
    assert "A solid ring with a number is a cluster of proposals" in lifecycle
    # Shown whenever the layer is on, including from `?layers=retired` on load.
    assert 'retiredToggle.checked = filters.layers.indexOf("retired") !== -1;' in _MAP_JS
    assert "setRetiredLayerVisible(retiredToggle.checked);" in _MAP_JS
    visible = _MAP_JS.split("function setRetiredLayerVisible(on) {")[1].split("\n  }\n")[0]
    assert "retiredLegend.hidden = !on;" in visible


@pytest.mark.parametrize("theme", sorted(_THEMES))
def test_retired_hues_read_on_the_land_and_as_legend_text(theme: str) -> None:
    """Rings are graphical objects (SC 1.4.11, 3:1 on the land); the legend prints its words in the
    same hue on the page background (SC 1.4.3, 4.5:1)."""
    tokens = _THEMES[theme]
    for token in ("--asset-retired", "--asset-retiring"):
        assert _contrast(tokens[token], tokens["--map-land"]) >= 3.0, (theme, token)
        assert _contrast(tokens[token], tokens.get("--bg", tokens["--color-paper"])) >= 4.5, (theme, token)
