"""Lists name a source by its short name, and the map names a technology as the server does.

Held in place here:
  * `/proposals`, `/opportunities` and `/assets` print a source's short name in the Source
    column, linked to the same URL as before, and fall back to the id for a source no map names;
  * the opportunity and asset label maps cover exactly the sources the dev loader loads, and
    every word of every label is already in that source's `data/sources.yaml` name, so a label
    cannot be made up;
  * the map page renders the label tables (`web/labels.py::map_labels`) into `#map-labels` for
    `map.js`, and `map.js` keeps no copy of the words (the in-view row itself is driven in `web/test_e2e.py`).

Same pattern as `web/test_data_centre_presentation.py`: `web.app.app` against a hand-written fake
`Transport`, no database. The fake is duplicated rather than imported, per this repo's convention
that no `web/test_*.py` imports another.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.build_data import OPPORTUNITY_SOURCE_IDS
from web.dev_up import _CONTEXT_ASSET_FILES, _ETHANOL_ATLAS_FILE, _ETHANOL_CAPACITY_FILE
from web.labels import map_labels
from web.page import _asset_extras, templates
from web.retirement import ASSET_STATUS_LABELS
from web.viewmodels import (
    ASSET_SOURCE_LABELS,
    OPPORTUNITY_SOURCE_LABELS,
    PROPOSAL_SOURCE_LABELS,
    TECHNOLOGY_LABELS,
    TECHNOLOGY_LOAD_LABEL,
    flatten_asset,
    map_labels_json,
    source_label,
    technology_label,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MAP_JS = REPO_ROOT / "web" / "static" / "js" / "map.js"
UNKNOWN = "xx.example.unlisted_register"

DEFAULT_HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
}
DEFAULT_VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "load"}, {"value": "solar"}],
        "proposal_kind": [{"value": "generation"}, {"value": "load"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [],
    }
}


class FakeTransport:
    def __init__(self, responses: Mapping[str, tuple[int, Any]] | None = None) -> None:
        self.responses: dict[str, tuple[int, Any]] = dict(responses or {})

    def _respond(self, url: str) -> httpx.Response:
        if url in self.responses:
            status, body = self.responses[url]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        return self._respond(url)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
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
        return self._respond(url)

    def close(self) -> None:
        pass


@pytest.fixture()
def web_client() -> Iterator[TestClient]:
    with TestClient(web_app) as client:
        yield client
    for key in ("api_client", "lag_days_default", "coverage_facts", "build_info", "sitemap_cache"):
        web_app.state.__dict__.pop(key, None)


def _install(proposals: list[dict[str, Any]], opportunities: list[dict[str, Any]]) -> None:
    transport = FakeTransport(
        {
            "/v1/health": (200, DEFAULT_HEALTH),
            "/v1/meta/vocabularies": (200, DEFAULT_VOCAB),
            "/v1/proposals": (200, _list_env(proposals)),
            "/v1/opportunities": (200, _list_env(opportunities)),
            "/v1/sources": (200, {"data": []}),
            "/v1/proposals/geo": (200, {"data": {"totals": {"lifecycle_state_counts": {}}}}),
        }
    )
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None


def _list_env(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows), "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": [],
    }


def _provenance(source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "source_name": f"Register {source_id}",
        "source_url": f"https://example.org/{source_id}",
        "retrieved_at": "2026-09-29T00:00:00Z",
        "reuse_class": "open",
        "attribution_text": source_id,
        "source_record_id": "X1",
    }


def _proposal(slug: str, source_id: str, technology: str = "solar") -> dict[str, Any]:
    return {
        "public_id": f"prop_{slug}",
        "slug": slug,
        "name_canonical": f"Site {slug}",
        "kind": "load" if technology == "load" else "generation",
        "technology": technology,
        "capacity_mw": 10.0,
        "jurisdiction": "US-VA",
        "lifecycle_state": "filed",
        "status_raw": "Filed",
        "identifiers": {},
        "source_count": 1,
        "provenance": [_provenance(source_id)],
    }


def _opportunity(slug: str, source_id: str) -> dict[str, Any]:
    return {
        "public_id": f"opp_{slug}",
        "slug": slug,
        "title": f"Tender {slug}",
        "kind": "rfp",
        "status": "open",
        "due_at": "2026-10-30T00:00:00Z",
        "technologies": [],
        "provenance": [_provenance(source_id)],
    }


def _source_cells(body: str) -> list[str]:
    return re.findall(r'<td data-label="Source"[^>]*>.*?</td>', body, flags=re.S)


def _cell_for(body: str, source_id: str) -> str:
    cells = [c for c in _source_cells(body) if f'href="https://example.org/{source_id}"' in c]
    assert len(cells) == 1, (source_id, _source_cells(body))
    return cells[0]


def _link_text(cell: str) -> str:
    match = re.search(r"<a [^>]*>(.*?)</a>", cell, flags=re.S)
    assert match, cell
    return html.unescape(match.group(1)).strip()


# ------------------------------------------------------------------------------ 1. the lists
def test_proposal_list_prints_the_short_name_linked_as_before(web_client: TestClient) -> None:
    _install([_proposal("a", "us.epa.echo.icis_air", "load"), _proposal("b", "us.iso.ercot.gen_queue")], [])
    body = web_client.get("/proposals").text
    icis = _cell_for(body, "us.epa.echo.icis_air")
    assert _link_text(icis) == "EPA ICIS-Air"
    assert 'rel="noopener nofollow"' in icis
    assert 'class="mono"' not in icis  # a name is prose; only an identifier is set in mono
    assert _link_text(_cell_for(body, "us.iso.ercot.gen_queue")) == "ERCOT"
    assert ">us.epa.echo.icis_air<" not in body


def test_proposal_list_falls_back_to_the_id_for_an_unnamed_source(web_client: TestClient) -> None:
    _install([_proposal("u", UNKNOWN)], [])
    cell = _cell_for(web_client.get("/proposals").text, UNKNOWN)
    assert _link_text(cell) == UNKNOWN
    assert 'class="mono"' in cell


def test_opportunity_list_prints_the_short_name_and_falls_back(web_client: TestClient) -> None:
    _install([], [_opportunity("t", "eu.ted.api"), _opportunity("u", UNKNOWN)])
    body = web_client.get("/opportunities").text
    assert _link_text(_cell_for(body, "eu.ted.api")) == "TED"
    assert _link_text(_cell_for(body, UNKNOWN)) == UNKNOWN


def _asset(slug: str, source_id: str) -> dict[str, Any]:
    return {
        "public_id": f"ast_{slug}",
        "slug": slug,
        "name": f"Asset {slug}",
        "asset_type": "gas_pipeline",
        "provenance": [_provenance(source_id)],
    }


def test_asset_rows_print_the_short_name_and_fall_back() -> None:
    records = []
    for entity in (
        _asset("p", "us.eia.atlas.gas_pipelines"),
        _asset("q", "us.eia.860m"),
        _asset("u", UNKNOWN),
    ):
        record = flatten_asset(entity)
        record.update(_asset_extras(entity))
        records.append(record)
    body = templates.get_template("partials/_asset_rows.html").render(
        records=records,
        filters={},
        sort_caption="by name",
        prev_cursor=None,
        next_cursor=None,
        querystring="",
    )
    assert _link_text(_cell_for(body, "us.eia.atlas.gas_pipelines")) == "EIA Energy Atlas"
    assert _link_text(_cell_for(body, "us.eia.860m")) == "EIA-860M"
    assert _link_text(_cell_for(body, UNKNOWN)) == UNKNOWN


def test_source_label_is_none_for_a_source_no_map_names() -> None:
    assert source_label(UNKNOWN) is None
    assert source_label(None) is None
    assert source_label("us.eia.860m") == PROPOSAL_SOURCE_LABELS["us.eia.860m"][0]


# ------------------------------------------------------------------------------ 2. the maps
def test_opportunity_labels_cover_the_sources_the_site_loads() -> None:
    assert set(OPPORTUNITY_SOURCE_LABELS) == set(OPPORTUNITY_SOURCE_IDS)


def test_asset_labels_cover_the_registries_the_dev_loader_loads() -> None:
    files = [name for name, _type in _CONTEXT_ASSET_FILES] + [_ETHANOL_ATLAS_FILE, _ETHANOL_CAPACITY_FILE]
    loaded = {name.removesuffix(".parquet") for name in files}
    assert set(ASSET_SOURCE_LABELS) == loaded
    # Power plants come from EIA-860M, which the proposal map names once for both.
    assert source_label("us.eia.860m") == "EIA-860M"
    assert not set(ASSET_SOURCE_LABELS) & set(PROPOSAL_SOURCE_LABELS)


def test_every_added_label_is_words_the_manifest_name_already_uses() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))
    names = {entry["id"]: str(entry["name"]).lower() for entry in manifest["sources"]}
    for source_id, label in {**OPPORTUNITY_SOURCE_LABELS, **ASSET_SOURCE_LABELS}.items():
        assert source_id in names, source_id
        for word in label.lower().split():
            assert word in names[source_id], (source_id, label, word)


# ------------------------------------------------------------------------------ 3. the map page
def test_map_page_renders_the_python_technology_labels_for_map_js(web_client: TestClient) -> None:
    _install([], [])
    body = web_client.get("/").text
    match = re.search(r'<script type="application/json" id="map-labels">(.*?)</script>', body, flags=re.S)
    assert match, "map page has no #map-labels"
    # Every table map.js prints words from, all from web/labels.py (audit 2026-09-30, F1).
    rendered = json.loads(match.group(1))
    assert rendered == map_labels()
    assert rendered["technology"] == TECHNOLOGY_LABELS
    assert rendered["asset_status"] == ASSET_STATUS_LABELS
    assert TECHNOLOGY_LABELS["load"] == TECHNOLOGY_LOAD_LABEL
    assert technology_label("load") == TECHNOLOGY_LOAD_LABEL
    assert technology_label("solar") == "Solar"
    assert technology_label("gas_cc") == "Gas, combined cycle"


def test_map_labels_json_cannot_close_its_script_tag() -> None:
    assert "</" not in map_labels_json()


def test_map_js_reads_the_labels_and_keeps_no_copy_of_them() -> None:
    """No `token: "Label"` pair from any server table appears in map.js: the words reach the
    browser only through `#map-labels`. (A bare word such as "Gas" in "Gas pipeline" is not a
    copy of the table, so the check is on the pair, not on the word.)"""
    source = MAP_JS.read_text(encoding="utf-8")
    assert 'getElementById("map-labels")' in source
    for table, words in map_labels().items():
        for token, label in words.items():
            key = r"[\"']?\b" + re.escape(token) + r"\b[\"']?"
            pair = re.compile(key + r"\s*:\s*[\"']" + re.escape(label) + r"[\"']")
            assert not pair.search(source), f"{table}.{token} -> {label!r} is hand-copied into map.js"
