"""The transmission-line context layer on the public site (grid lane G2, 2026-09-28): the home map
lists it live with its own legend group and stroke pattern, map.js draws it in its own dash-dot
layer, and an asset page for a line renders voltage, endpoints and a line-aware nearby sentence.

Same fake-`Transport` pattern as `web/test_asset_pages.py` (duplicated, not imported -- this repo's
convention for `web/test_*.py`). The entity below is the shape `/v1/assets/{id}` returns for the
first row of `pipeline/context/fixtures/lbnl_ferc_hifld_transmission_lines_sample.csv`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import HOME_MAP_ASSET_TYPES
from web.app import app as web_app

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


class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]] | None = None) -> None:
        self.responses: dict[str, tuple[int, Any]] = dict(responses or {})
        self.calls: list[tuple[str, str, Any]] = []

    def _respond(self, url: str) -> httpx.Response:
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: Any = None
    ) -> httpx.Response:
        self.calls.append(("GET", url, dict(params or {})))
        return self._respond(url)

    def post(self, url: str, *, json: Any = None, cookies: Any = None) -> httpx.Response:
        self.calls.append(("POST", url, json))
        return self._respond(url)

    def request(
        self, method: str, url: str, *, json: Any = None, params: Any = None, cookies: Any = None
    ) -> httpx.Response:
        self.calls.append((method, url, json if json is not None else dict(params or {})))
        return self._respond(url)

    def close(self) -> None:
        pass


@pytest.fixture()
def web_client() -> Iterator[TestClient]:
    with TestClient(web_app) as client:
        yield client
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


def _install(extra: Mapping[str, tuple[int, Any]] | None = None) -> None:
    transport = FakeTransport(
        {
            "/v1/health": (200, DEFAULT_HEALTH),
            "/v1/meta/vocabularies": (200, DEFAULT_VOCAB),
            "/v1/sources": (200, {"data": []}),
            **(extra or {}),
        }
    )
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None


def _line_entity() -> dict[str, Any]:
    return {
        "public_id": "asset_01SCRIBA",
        "slug": "scriba-fitzpatrick-345-kv-ny",
        "name": "Scriba - Fitzpatrick 345 kV",
        "asset_type": "transmission_line",
        "status": "unknown",
        "operator_name": None,
        "technology": None,
        "technologies": {},
        "capacity_mw": None,
        "capacity_value": None,
        "capacity_unit": None,
        "state_code": "US-NY",
        "country": "US",
        "geometry": {
            "type": "MultiLineString",
            "coordinates": [[[-76.399867, 43.522412], [-76.400505, 43.514701], [-76.407225, 43.513202]]],
        },
        "attributes": {
            "voltage_kv": 345.0,
            "voltage_class": "345 kV",
            "sub_1": "Scriba",
            "sub_2": "Fitzpatrick",
            "miles": 1.1,
            "states_crossed": ["US-NY"],
            "owner": "Niagara Mohawk Power",
            "owner_raw": "niagara mohawk power",
            "hifld_line_id": "100024",
        },
        "owners": [],
        "provenance": [
            {
                "source_id": "us.lbnl.ferc_hifld_transmission_lines",
                "source_name": "LBNL — harmonized FERC Form 1 x HIFLD transmission lines",
                "source_url": "https://data.openei.org/files/8742/ferc_eia_transmission_lines_public.csv",
                "retrieved_at": "2026-09-28T13:55:17Z",
                "reuse_class": "attribution",
                "attribution_text": (
                    "Source: Lawrence Berkeley National Laboratory (Yin, Nait Belaid and Heleno), "
                    "Open Energy Data Initiative, CC BY 4.0"
                ),
                "source_record_id": "100024",
            }
        ],
    }


def test_home_map_lists_transmission_lines_live_with_a_legend_group(web_client: TestClient) -> None:
    assert ("transmission_line", "Transmission lines", True) in HOME_MAP_ASSET_TYPES
    assert all(value != "substation" for value, _, _ in HOME_MAP_ASSET_TYPES)
    _install()
    body = web_client.get("/").text
    tag = body.split('id="mf-asset-type-transmission_line"')[1].split(">")[0]
    assert "disabled" not in tag
    group = body.split('data-legend-type="transmission_line"')[1].split("</div>")[0]
    assert "var(--asset-transmission)" in body.split('data-legend-type="transmission_line"')[1][:80]
    assert 'stroke-dasharray="8 3 2 3"' in group and "not the whole grid" in group


def test_map_script_draws_transmission_in_its_own_dash_dot_layer() -> None:
    source = (Path(__file__).parent / "static" / "js" / "map.js").read_text()
    live = source.split("var ASSET_TYPES_LIVE = [")[1].split("]")[0]
    assert '"transmission_line"' in live
    assert 'token: "--asset-transmission", line: true' in source
    layer = source.split('id: "asset-lines-transmission"')[1].split('}, "clusters")')[0]
    assert '"line-dasharray": [4, 1.5, 1, 1.5]' in layer
    # The pipeline layers exclude it, and the visibility toggle knows the new layer id.
    assert '["!", isTransmission]' in source
    ids = source.split("var ASSET_LAYER_IDS = [")[1].split("]")[0]
    assert '"asset-lines-transmission"' in ids
    css = (Path(__file__).parent / "static" / "css" / "styles.css").read_text()
    assert css.count("--asset-transmission:") == 3  # light, dark (media query), dark (data-theme)


def test_asset_page_for_a_transmission_line(web_client: TestClient) -> None:
    _install(
        {
            "/v1/assets": (200, {"data": [_line_entity()]}),
            "/v1/assets/asset_01SCRIBA/nearby-proposals": (200, {"data": []}),
        }
    )
    resp = web_client.get("/assets/scriba-fitzpatrick-345-kv-ny")
    assert resp.status_code == 200
    body = resp.text
    assert "Scriba - Fitzpatrick 345 kV" in body and "Transmission line" in body
    assert '<dt>Voltage</dt><dd class="tnum">345 kV</dd>' in body
    assert "Scriba to Fitzpatrick" in body
    assert "Pipeline class" not in body and "pipeline&rsquo;s route" not in body
    assert "this line&rsquo;s route" in body
    assert 'class="mini-map__line mini-map__line--transmission"' in body
    assert "<dt>Owner (as the source names it)</dt><dd>Niagara Mohawk Power</dd>" in body
    assert 'href="/organizations/' not in body.split('<dl class="field-grid">')[1].split("</dl>")[0]
    assert "Lawrence Berkeley National Laboratory" in body and "28 Sep 2026" in body  # docs/31 §4
    # Promoted values are not repeated in the generic Attributes table.
    table = body.split("2.</span> Attributes")[1].split("</section>")[0]
    assert (
        "voltage kv" not in table.lower()
        and ">Scriba<" not in table
        and ">Niagara Mohawk Power<" not in table
    )
