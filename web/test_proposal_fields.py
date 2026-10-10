"""Which field rows a proposal page shows for its kind, and the map heading that names what is on
the map (2026-10-07, after EPA Class VI and the retired-plants layer went live).

* A Class VI CO2 storage well printed five rows that can never hold a value for it ("Capacity (MW)
  —", "Storage (MWh) —", "ISO / operator —", "Connects at —", "EIA plant / generator ID —"). The
  rows a kind cannot carry now come from one table (`web/viewmodels.py`
  `PROPOSAL_FIELDS_NOT_APPLICABLE`) that the page and the map drawer both read. A row that applies
  but is empty keeps its "—", and a row with a value is never hidden.
* Its identifier row said "Queue ID" for EPA's GSDT project id; a Class VI id is a permit file
  number.
* Its technology printed through the fallback as "Co2 geologic sequestration".

Same pattern as `web/test_data_centre_presentation.py`: `web.app.app` against a hand-written fake
`Transport`, no database; the fake is duplicated rather than imported (no `web/test_*.py` imports
another).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from services.api.app import PROPOSAL_KIND_VALUES
from web.api_client import ApiClient
from web.app import app as web_app
from web.viewmodels import (
    PROPOSAL_FIELD_ROWS,
    PROPOSAL_FIELDS_NOT_APPLICABLE,
    PROPOSAL_ID_LABELS,
    flatten_proposal,
    proposal_field_rows,
    proposal_fields_json,
)

WEB = Path(__file__).resolve().parent
DETAIL = (WEB / "templates" / "proposal_detail.html").read_text(encoding="utf-8")
MAP_JS = (WEB / "static" / "js" / "map.js").read_text(encoding="utf-8")

DEFAULT_HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
}
DEFAULT_VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}, {"value": "co2_geologic_sequestration"}],
        "proposal_kind": [{"value": "generation"}, {"value": "ccs"}, {"value": "load"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [],
    }
}

#: Row label on the page for each `PROPOSAL_FIELD_ROWS` entry the kind table can hide.
ROW_LABELS = {
    "capacity_mw": "<dt>Capacity (MW)</dt>",
    "storage_mwh": "<dt>Storage (MWh)</dt>",
    "iso": "<dt>ISO / operator</dt>",
    "connects_at": "<dt>Connects at</dt>",
    "eia_ids": "<dt>EIA plant / generator ID</dt>",
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
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("GET", url, dict(params or {})))
        return self._respond(url)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("POST", url, json))
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
        self.calls.append((method, url, json if json is not None else dict(params or {})))
        return self._respond(url)

    def close(self) -> None:
        pass


@pytest.fixture()
def web_client() -> Iterator[TestClient]:
    with TestClient(web_app) as client:
        yield client
    for key in ("api_client", "lag_days_default", "coverage_facts", "build_info", "sitemap_cache"):
        web_app.state.__dict__.pop(key, None)


def _provenance(source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "source_name": f"Register {source_id}",
        "source_url": f"https://example.org/{source_id}",
        "retrieved_at": "2026-10-07T00:00:00Z",
        "reuse_class": "open",
        "attribution_text": source_id,
        "source_record_id": "X1",
    }


def _proposal(slug: str, kind: str, technology: str, source_id: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "public_id": f"prop_{slug}",
        "slug": slug,
        "name_canonical": f"Record {slug}",
        "kind": kind,
        "technology": technology,
        "technology_raw": None,
        "capacity_mw": None,
        "storage_mwh": None,
        "iso": None,
        "jurisdiction": "US-IN",
        "lifecycle_state": "permitted",
        "status_raw": "Final Permit Decisions Issued",
        "identifiers": {},
        "source_count": 1,
        "provenance": [_provenance(source_id)],
    }
    row.update(extra)
    return row


def _class_vi(**extra: Any) -> dict[str, Any]:
    return _proposal(
        "wabash-carbon-services-3jm4dv",
        "ccs",
        "co2_geologic_sequestration",
        "us.epa.class_vi",
        identifiers={"queue_ids": [{"id": "R05-IN-0001"}]},
        sponsor={"name_canonical": "Wabash Carbon Services"},
        **extra,
    )


def _data_centre() -> dict[str, Any]:
    return _proposal("dc-one", "load", "load", "us.epa.echo.icis_air", lifecycle_state="built")


def _solar() -> dict[str, Any]:
    return _proposal(
        "solar-one",
        "generation",
        "solar",
        "us.iso.ercot.gen_queue",
        capacity_mw=120.0,
        iso="ERCOT",
        identifiers={"queue_ids": [{"id": "24INR0001"}], "eia_plant_id": 69662, "eia_generator_id": "PV1"},
    )


def _install(rows: list[dict[str, Any]]) -> None:
    env = {
        "data": rows,
        "meta": {"total": len(rows), "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": [],
    }
    web_app.state.api_client = ApiClient(
        FakeTransport(
            {
                "/v1/health": (200, DEFAULT_HEALTH),
                "/v1/meta/vocabularies": (200, DEFAULT_VOCAB),
                "/v1/proposals": (200, env),
                "/v1/sources": (200, {"data": []}),
            }
        )
    )
    web_app.state.lag_days_default = None


def _grid(html: str) -> str:
    match = re.search(r'<dl class="field-grid">(.*?)</dl>', html, re.S)
    assert match, "page has no field grid"
    return match.group(1)


# ---------------------------------------------------------------------------- the table itself
def test_the_table_names_real_kinds_and_real_rows() -> None:
    assert set(PROPOSAL_FIELDS_NOT_APPLICABLE) <= set(PROPOSAL_KIND_VALUES)
    assert set(PROPOSAL_ID_LABELS) <= set(PROPOSAL_KIND_VALUES)
    for kind, rows in PROPOSAL_FIELDS_NOT_APPLICABLE.items():
        assert rows <= set(PROPOSAL_FIELD_ROWS), kind
        # Kind, technology, place and sponsor apply to every record.
        assert not rows & {"kind", "technology", "jurisdiction", "county", "sponsor"}, kind


def test_a_class_vi_well_has_no_grid_or_generator_rows() -> None:
    record = flatten_proposal(_class_vi())
    rows = proposal_field_rows(record)
    assert not rows & {"capacity_mw", "storage_mwh", "iso", "connects_at", "eia_ids"}
    assert {"jurisdiction", "county", "sponsor", "queue_ids"} <= rows
    assert record["queue_id_label"] == "Permit project ID"
    assert record["technology_label"] == "CO2 geologic sequestration"


def test_a_data_centre_keeps_the_rows_a_load_can_carry() -> None:
    """A data centre's MW, ISO, grid point and load-queue id are real values that are often
    unstated, so they keep their "—"; stored energy and EIA-860 generator ids cannot apply."""
    rows = proposal_field_rows(flatten_proposal(_data_centre()))
    assert {"capacity_mw", "iso", "connects_at", "queue_ids"} <= rows
    assert not rows & {"storage_mwh", "eia_ids"}


def test_kinds_without_an_entry_show_every_row() -> None:
    for kind in ("generation", "storage", "other"):
        record = flatten_proposal(_proposal("x", kind, "solar", "us.eia.860m"))
        assert proposal_field_rows(record) == frozenset(PROPOSAL_FIELD_ROWS), kind
        assert record["queue_id_label"] == "Queue ID"


def test_a_value_is_never_hidden_by_the_table() -> None:
    """An admin override or a merge can put a value in a row the kind does not usually carry; the
    table hides only empty rows."""
    record = flatten_proposal(_class_vi(capacity_mw=12.5, iso="MISO"))
    rows = proposal_field_rows(record, connected=True)
    assert {"capacity_mw", "iso", "connects_at"} <= rows
    assert "storage_mwh" not in rows


def test_the_id_label_is_plural_for_several_ids() -> None:
    record = flatten_proposal(
        _proposal(
            "y",
            "ccs",
            "co2_geologic_sequestration",
            "us.epa.class_vi",
            identifiers={"queue_ids": [{"id": "A"}, {"id": "B"}]},
        )
    )
    assert record["queue_id_label"] == "Permit project IDs"


# ---------------------------------------------------------------------------- rendered pages
def test_the_class_vi_page_leaves_out_rows_that_cannot_apply(web_client: TestClient) -> None:
    _install([_class_vi()])
    resp = web_client.get("/proposals/wabash-carbon-services-3jm4dv")
    assert resp.status_code == 200
    grid = _grid(resp.text)
    for row, label in ROW_LABELS.items():
        assert label not in grid, row
    assert "<dt>Technology</dt><dd>CO2 geologic sequestration" in grid
    assert "Co2 geologic" not in resp.text
    assert '<dt>Permit project ID</dt><dd class="mono">R05-IN-0001</dd>' in grid
    assert "Queue ID" not in grid
    assert "<dt>Sponsor</dt><dd>Wabash Carbon Services</dd>" in grid


def test_the_data_centre_page_keeps_capacity_and_iso_as_unstated(web_client: TestClient) -> None:
    _install([_data_centre()])
    grid = _grid(web_client.get("/proposals/dc-one").text)
    assert '<dt>Capacity (MW)</dt><dd class="tnum">—</dd>' in grid
    assert "<dt>ISO / operator</dt><dd>—</dd>" in grid
    assert "<dt>Connects at</dt>" in grid
    assert ROW_LABELS["storage_mwh"] not in grid and ROW_LABELS["eia_ids"] not in grid


def test_the_generation_page_keeps_every_row(web_client: TestClient) -> None:
    _install([_solar()])
    grid = _grid(web_client.get("/proposals/solar-one").text)
    for row, label in ROW_LABELS.items():
        assert label in grid, row
    assert '<dt>Queue ID</dt><dd class="mono">24INR0001</dd>' in grid
    assert '<dd class="mono">69662 / PV1</dd>' in grid


def test_the_template_gates_rows_on_the_table_not_on_the_kind() -> None:
    """One mechanism: every hideable row tests membership of `fields`; no row tests a kind."""
    for row in ROW_LABELS:
        assert f"{{% if '{row}' in fields %}}" in DETAIL, row
    assert "record.kind ==" not in DETAIL.replace("record.technology == record.kind", "")


# ---------------------------------------------------------------------------- the map drawer
def test_the_map_page_hands_the_drawer_the_same_table(web_client: TestClient) -> None:
    _install([_solar()])
    html = web_client.get("/").text
    match = re.search(r'<script type="application/json" id="proposal-fields">(.*?)</script>', html, re.S)
    assert match
    table = json.loads(match.group(1))["not_applicable"]
    assert {k: set(v) for k, v in table.items()} == {
        k: set(v) for k, v in PROPOSAL_FIELDS_NOT_APPLICABLE.items()
    }
    assert "</" not in proposal_fields_json()
    assert 'fieldApplies(p.kind, "capacity_mw", p.capacity_mw)' in MAP_JS


# ---- review 2026-10-10 §2.6 item 7: capacity through `mw`, names in a readable case ----
def test_the_field_grid_and_description_print_capacity_through_mw(web_client: TestClient) -> None:
    """Before: "3200.0" in the grid and "3200.0 MW" in the meta description (the one page left
    on `'%.1f'`, docs/31 §4)."""
    _install(
        [
            _solar()
            | {"capacity_mw": 3200.0, "storage_mwh": 1200.5, "kind": "storage", "technology": "storage"}
        ]
    )
    body = web_client.get("/proposals/solar-one").text
    grid = _grid(body)
    assert re.search(r"<dt>Capacity \(MW\)</dt><dd class=\"tnum\">3,200\b", grid)
    assert re.search(r"<dt>Storage \(MWh\)</dt><dd class=\"tnum\">1,200\.5<", grid)
    assert "3200.0" not in body
    description = re.search(r'<meta name="description" content="([^"]*)"', body)
    assert description is not None and "3,200 MW" in description.group(1)


def test_a_name_filed_in_capitals_is_readable_in_the_heading_and_title_and_shown_once_as_filed(
    web_client: TestClient,
) -> None:
    _install([_solar() | {"name_canonical": "SUNSETTER BESS SOLAR"}])
    body = web_client.get("/proposals/solar-one").text
    assert "<h1>Sunsetter BESS Solar</h1>" in body
    assert "<title>Sunsetter BESS Solar — Infraque</title>" in body
    assert re.search(r'<nav class="breadcrumbs"[^>]*>.*/ Sunsetter BESS Solar</nav>', body)
    assert (
        body.count(
            '<p class="as-filed">As filed: <span class="as-filed__name">SUNSETTER BESS SOLAR</span></p>'
        )
        == 1
    )
    # Search engines and the URL keep the register's own spelling.
    description = re.search(r'<meta name="description" content="([^"]*)"', body)
    assert description is not None and description.group(1).startswith("SUNSETTER BESS SOLAR:")


def test_a_name_in_a_readable_case_has_no_as_filed_line(web_client: TestClient) -> None:
    _install([_solar()])
    body = web_client.get("/proposals/solar-one").text
    assert "<h1>Record solar-one</h1>" in body and "as-filed" not in body


def test_no_visible_title_uses_a_double_hyphen() -> None:
    """docs/31: a title separates the page from the site with a dash, not " -- "."""
    offenders = []
    for path in (WEB / "templates").rglob("*.html"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ("{% block title %}" in line or "<title>" in line) and " -- " in line:
                offenders.append(f"{path.relative_to(WEB)}:{number}")
    assert offenders == []
