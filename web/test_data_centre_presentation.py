"""How the data-centre records read on public pages (lane H6, 2026-09-29; docs/25 §3.3, §3.7).

Three things are held in place here:
  * the copy that names the proposal sources (map header, `/proposals` meta description) is built
    from `web/viewmodels.py::PROPOSAL_SOURCE_LABELS`, which is pinned to the list the dev loader
    loads, so a new source cannot leave it stale again;
  * kinds (and the `load` technology token) render as labels on the list, the filter selects and
    the detail page, never as bare tokens, and `kind == technology` prints one row, not two;
  * a `load` record's source row says why that source counts it as a data centre, with the
    measured caveat on the NAICS-only basis, and nothing is said on any other record.

Same pattern as `web/test_slippage_view.py`: `web.app.app` against a hand-written fake
`Transport`, no database. The fake is duplicated rather than imported, per this repo's convention
that no `web/test_*.py` imports another.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import _PROPOSAL_SOURCE_IDS
from web.app import app as web_app
from web.build_data import PROPOSAL_SOURCE_IDS as LOADED_PROPOSAL_SOURCE_IDS
from web.viewmodels import (
    PROPOSAL_KIND_LABELS,
    PROPOSAL_SOURCE_LABELS,
    SELECT_BASIS_TEXT,
    TECHNOLOGY_LOAD_LABEL,
    attach_select_basis,
    proposal_sources_phrase,
    select_basis_line,
)

ICIS = "us.epa.echo.icis_air"
DEQ = "us.va.deq.data_center_air_sites"
LOAD_LABEL = PROPOSAL_KIND_LABELS["load"]

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
        "opportunity_kind": [{"value": "rfp"}, {"value": "loan_program"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [],
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


def _install(transport: FakeTransport) -> None:
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None


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


def _data_centre(slug: str, basis: dict[str, str], sources: list[str]) -> dict[str, Any]:
    return {
        "public_id": f"prop_{slug}",
        "slug": slug,
        "name_canonical": f"Site {slug}",
        "kind": "load",
        "technology": "load",
        "technology_raw": "Data Center",
        "capacity_mw": None,
        "jurisdiction": "US-VA",
        "lifecycle_state": "built",
        "status_raw": "Operating",
        "identifiers": {"select_basis": basis} if basis else {},
        "source_count": len(sources),
        "provenance": [_provenance(s) for s in sources],
    }


def _solar(slug: str = "solar-one", identifiers: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "public_id": f"prop_{slug}",
        "slug": slug,
        "name_canonical": "Solar One",
        "kind": "generation",
        "technology": "solar",
        "technology_raw": "Solar Photovoltaic",
        "capacity_mw": 120.0,
        "jurisdiction": "US-TX",
        "lifecycle_state": "filed",
        "status_raw": "Filed",
        "identifiers": identifiers or {},
        "source_count": 1,
        "provenance": [_provenance("us.iso.ercot.gen_queue")],
    }


def _list_env(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows), "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": [],
    }


def _base(rows: list[dict[str, Any]]) -> FakeTransport:
    return FakeTransport(
        {
            "/v1/health": (200, DEFAULT_HEALTH),
            "/v1/meta/vocabularies": (200, DEFAULT_VOCAB),
            "/v1/proposals": (200, _list_env(rows)),
            "/v1/opportunities": (200, _list_env([])),
            "/v1/sources": (200, {"data": []}),
            "/v1/proposals/geo": (
                200,
                {"data": {"totals": {"lifecycle_state_counts": {"built": len(rows)}}}},
            ),
        }
    )


def _meta_description(html: str) -> str:
    match = re.search(r'<meta name="description" content="([^"]*)"', html)
    assert match, "page has no meta description"
    return match.group(1)


# ------------------------------------------------------------------------------ 1. source copy
def test_the_named_sources_are_the_sources_the_site_loads() -> None:
    """The copy's list, `/about`'s proposal classifier and the dev loader's list are one set."""
    assert set(PROPOSAL_SOURCE_LABELS) == set(LOADED_PROPOSAL_SOURCE_IDS)
    assert set(_PROPOSAL_SOURCE_IDS) == set(PROPOSAL_SOURCE_LABELS)


def test_sources_phrase_names_every_source_once() -> None:
    phrase = proposal_sources_phrase()
    assert phrase == (
        "Interconnection queue and generator proposals from ERCOT, CAISO, NYISO, EIA-860M and NESO, "
        "data-centre sites from Virginia DEQ and EPA ICIS-Air, "
        "and CO2 storage permit applications from EPA Class VI"
    )
    for label, _group in PROPOSAL_SOURCE_LABELS.values():
        assert phrase.count(label) == 1


def test_map_header_and_list_meta_description_carry_the_phrase(web_client: TestClient) -> None:
    _install(_base([_solar()]))
    phrase = proposal_sources_phrase()
    home = web_client.get("/").text
    assert phrase in home
    listing = web_client.get("/proposals").text
    assert _meta_description(listing).startswith(phrase)
    for html in (home, listing):
        assert "Proposals from ERCOT, CAISO, NYISO, EIA-860M and NESO" not in html
        assert "Virginia DEQ" in html and "EPA ICIS-Air" in html


# -------------------------------------------------------------------------------- 2. labels
def test_list_shows_the_load_label_not_the_token(web_client: TestClient) -> None:
    _install(_base([_data_centre("dc-one", {ICIS: "name"}, [ICIS]), _solar()]))
    html = web_client.get("/proposals?kind=load").text
    cells = re.findall(r'<td data-label="Technology">([^<]*)</td>', html)
    assert cells == [TECHNOLOGY_LOAD_LABEL, "Solar"]


def test_list_and_map_filter_selects_use_labels_and_keep_tokens_as_values(web_client: TestClient) -> None:
    _install(_base([_solar()]))
    listing = web_client.get("/proposals?kind=load").text
    assert f'<option value="load" selected>{LOAD_LABEL}</option>' in listing
    assert '<option value="generation" >Generation</option>' in listing
    assert f'<option value="load" >{TECHNOLOGY_LOAD_LABEL}</option>' in listing  # technology
    assert ">load</option>" not in listing
    home = web_client.get("/").text
    assert f'<option value="load">{LOAD_LABEL}</option>' in home  # kind
    assert f'<option value="load">{TECHNOLOGY_LOAD_LABEL}</option>' in home  # technology
    assert ">load</option>" not in home


def test_opportunity_kind_select_uses_labels(web_client: TestClient) -> None:
    _install(_base([]))
    html = web_client.get("/opportunities?kind=loan_program").text
    assert '<option value="loan_program" selected>Loan programme</option>' in html
    assert "Request for proposals (RFP)</option>" in html
    assert ">loan_program</option>" not in html


def test_detail_load_record_prints_kind_once_with_the_source_wording(web_client: TestClient) -> None:
    _install(_base([_data_centre("dc-one", {ICIS: "name"}, [ICIS])]))
    html = web_client.get("/proposals/dc-one").text
    assert "<dt>Kind</dt>" in html
    assert "<dt>Technology</dt>" not in html
    assert LOAD_LABEL in html
    assert "source: &ldquo;Data Center&rdquo;" in html
    assert "<dd>load" not in html
    assert f"Site dc-one: {LOAD_LABEL} proposal" in _meta_description(html)


def test_detail_generation_record_keeps_both_rows(web_client: TestClient) -> None:
    _install(_base([_solar()]))
    html = web_client.get("/proposals/solar-one").text
    assert "<dt>Kind</dt><dd>Generation</dd>" in html
    assert "<dt>Technology</dt><dd>Solar" in html


# ---------------------------------------------------------------------- 3. why a data centre
@pytest.mark.parametrize(
    ("source_id", "basis", "expected"),
    [
        (ICIS, "name", "Selected as a data centre because the facility name says data centre."),
        (ICIS, "naics_518210", "NAICS 518210 (data processing and hosting)"),
        (ICIS, "naics_541513_operator", "known data-centre operator"),
        (DEQ, "deq_flag", "Selected as a data centre because Virginia DEQ flags it as a data centre."),
        (DEQ, "principal_product", "principal product as a data centre"),
    ],
)
def test_detail_states_each_basis_in_its_source_row(
    web_client: TestClient, source_id: str, basis: str, expected: str
) -> None:
    _install(_base([_data_centre("dc-basis", {source_id: basis}, [source_id])]))
    html = web_client.get("/proposals/dc-basis").text
    rows = re.findall(r'<li class="provenance-panel__row">(.*?)</li>', html, flags=re.S)
    assert len(rows) == 1
    assert "provenance-panel__basis" in rows[0]
    assert expected in rows[0]


def test_the_naics_only_basis_carries_its_measured_caveat() -> None:
    line = select_basis_line("naics_518210")
    assert line is not None
    assert "offices with a server room" in line
    assert "39 of 49" in line
    for basis in ("name", "naics_541513_operator", "deq_flag", "principal_product"):
        assert "server room" not in (select_basis_line(basis) or "")


def test_merged_record_states_each_sources_reason_in_its_own_row(web_client: TestClient) -> None:
    record = _data_centre("dc-merged", {DEQ: "deq_flag", ICIS: "naics_518210"}, [DEQ, ICIS])
    _install(_base([record]))
    html = web_client.get("/proposals/dc-merged").text
    rows = re.findall(r'<li class="provenance-panel__row">(.*?)</li>', html, flags=re.S)
    assert len(rows) == 2
    assert "Virginia DEQ flags it" in rows[0] and "NAICS" not in rows[0]
    assert "NAICS 518210" in rows[1] and "DEQ flags" not in rows[1]


def test_no_basis_line_on_a_generation_record_even_if_identifiers_carry_one(web_client: TestClient) -> None:
    _install(_base([_solar(identifiers={"select_basis": {"us.iso.ercot.gen_queue": "name"}})]))
    html = web_client.get("/proposals/solar-one").text
    assert "provenance-panel__basis" not in html
    assert "Selected as a data centre" not in html


def test_no_basis_line_for_a_source_without_a_visible_row() -> None:
    """A gated source's link is dropped from provenance by the API; its basis must not surface."""
    rows = [{"source_id": DEQ}]
    out = attach_select_basis({"kind": "load", "select_basis": {ICIS: "naics_518210"}}, rows)
    assert "select_basis_line" not in out[0]


def test_unknown_basis_or_none_says_nothing() -> None:
    assert select_basis_line(None) is None
    assert select_basis_line("some_future_rule") is None
    assert set(SELECT_BASIS_TEXT) == {
        "name",
        "naics_518210",
        "naics_541513_operator",
        "deq_flag",
        "principal_product",
    }


def test_load_record_without_a_stored_basis_renders_no_line(web_client: TestClient) -> None:
    _install(_base([_data_centre("dc-bare", {}, [ICIS])]))
    html = web_client.get("/proposals/dc-bare").text
    assert "provenance-panel__basis" not in html
