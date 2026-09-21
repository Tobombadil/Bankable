"""Coverage, vintage and the status definitions as a reader meets them.

Three surfaces, and the argument for each is in `web/viewmodels.py::absence_note` and the
`/methodology` route: the page carries the whole statement; the in-place notes appear only where
an absence would otherwise read as a fact about the world; nothing appears beside a result that
already answers the reader's question.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.viewmodels import absence_note

HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-21",
    "live_as_of": "2026-09-21T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
    "build": {"commit": "abc123def456", "dirty": False},
    "source_data_as_of": "2026-09-13T20:25:39Z",
    "source_vintage": {
        "oldest": "2017",
        "oldest_label": "2017",
        "oldest_source_id": "us.eia.atlas.gas_processing_plants",
        "sources_stating_a_release": 5,
        "sources_stating_none": 11,
        "sources_undetermined": 5,
    },
}

COVERAGE: dict[str, Any] = {
    "data": {
        "sources": {
            "registered": 86,
            "with_rows": 16,
            "loaded_source_ids": ["us.eia.860m"],
            "withheld": [
                {
                    "source_id": "us.iso.pjm.gen_queue",
                    "name": "PJM New Services Queue",
                    "operator": "PJM Interconnection",
                    "jurisdiction": "US-PJM",
                    "category": "generation_queue",
                    "reuse": "restricted",
                    "publication": "none",
                    "url": "https://example.org/pjm",
                    "supply": True,
                    "reason": "terms require a licence or consent we do not hold",
                },
                {
                    "source_id": "us.iso.miso.gen_queue",
                    "name": "MISO Generator Interconnection Queue",
                    "operator": "MISO",
                    "jurisdiction": "US-MISO",
                    "category": "generation_queue",
                    "reuse": "unknown",
                    "publication": "none",
                    "url": "https://example.org/miso",
                    "supply": True,
                    "reason": "terms not retrievable, so nothing is assumed",
                },
            ],
        },
        "vintage": {
            "sources": [
                {
                    "source_id": "us.eia.860m",
                    "name": "EIA-860M",
                    "vintage": "2026-07",
                    "vintage_label": "July 2026",
                    "vintage_basis": "artefact_filename",
                    "fetched_at": "2026-09-13T20:25:39Z",
                },
                {
                    "source_id": "us.iso.caiso.gen_queue",
                    "name": "CAISO Public Queue Report",
                    "vintage": None,
                    "vintage_label": None,
                    "vintage_basis": "not_stated",
                    "fetched_at": "2026-09-13T20:25:21Z",
                },
            ],
            "oldest": {
                "source_id": "us.eia.860m",
                "name": "EIA-860M",
                "vintage": "2026-07",
                "vintage_label": "July 2026",
            },
            "sources_stating_a_release": 1,
            "sources_stating_none": 1,
            "sources_undetermined": 0,
        },
        "records": {
            "proposals": 10409,
            "opportunities": 707,
            "assets": 18055,
            "proposal_kinds": {"generation": 6692},
            "opportunity_kinds": {"tender": 553},
            "jurisdictions": {"US-TX": 2333},
        },
        "technologies": {
            "present": {"solar": 2824},
            "absent": ["load"],
            "vocabulary": ["solar", "load"],
        },
        "states": {
            "lifecycle_counts": {"filed": 1879, "studied": 1604},
            "lifecycle_absent": ["unknown"],
            "opportunity_status_counts": {"open": 405},
            "opportunity_status_absent": ["frozen", "reinstated"],
            "pre_construction_states": ["filed", "studied", "permitted", "contracted"],
            "pre_construction_count": 4111,
        },
        "ownership": {
            "organizations": 7412,
            "with_recorded_parent": 298,
            "without_recorded_parent": 7114,
            "parent_source_ids": ["global.gleif.lei"],
        },
        "notes": [
            {
                "id": "no_large_load",
                "headline": "No large-load or data-centre proposals.",
                "body": "No loaded source publishes large-load interconnection requests as data.",
                "written": "2026-09-20",
            }
        ],
    }
}

VOCABULARY: dict[str, Any] = {
    "data": {
        "definitions_version": 1,
        "definitions_written": "2026-09-21",
        "lifecycle_states": [
            {
                "state": "unknown",
                "definition": "The source publishes a status we could not place.",
                "excludes": "Not early stage.",
                "uncertainty": None,
                "is_fallback": True,
                "maps_from": [],
            },
            {
                "state": "studied",
                "definition": "Inside the operator's process.",
                "excludes": "Says nothing about the outcome.",
                "uncertainty": "The broadest state.",
                "is_fallback": False,
                "maps_from": [
                    {
                        "source_id": "us.iso.caiso.gen_queue",
                        "status_key": "caiso",
                        "field": "Status",
                        "raw": "ACTIVE",
                        "rule": "map",
                        "note": None,
                        "rows_published": True,
                    },
                    {
                        "source_id": None,
                        "status_key": "spp",
                        "field": "Status (Original)",
                        "raw": "DISIS STAGE",
                        "rule": "map",
                        "note": None,
                        "rows_published": False,
                    },
                ],
            },
        ],
        "opportunity_statuses": [
            {
                "state": "frozen",
                "definition": "The issuer has paused the process.",
                "excludes": None,
                "uncertainty": None,
                "is_fallback": False,
                "maps_from": [],
            }
        ],
        "pre_construction_states": ["filed", "studied", "permitted", "contracted"],
        "status_map_files": ["pipeline/status_map.yaml"],
    }
}


class FakeTransport:
    """Canned responses keyed by path. Duplicated rather than imported: this repo's convention is
    that no `web/test_*.py` imports another one."""

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
    _reset_caches()


def _reset_caches() -> None:
    """`starlette.datastructures.State` keeps its values in `_state`, not `__dict__`, so the
    `state.__dict__.pop(...)` idiom other web tests use is a no-op. Both caches treat `None` as
    "not cached" (`web/viewmodels.py`), so assigning `None` is the supported reset and keeps this
    file off the private attribute."""
    web_app.state.build_info = None
    web_app.state.coverage_facts = None


def _install(transport: FakeTransport) -> None:
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    _reset_caches()


def _transport(**extra: tuple[int, Any]) -> FakeTransport:
    return FakeTransport(
        {
            "/v1/health": (200, HEALTH),
            "/v1/coverage": (200, COVERAGE),
            "/v1/lifecycle-states": (200, VOCABULARY),
            **extra,
        }
    )


# ------------------------------------------------------------------------------- the footer
def test_the_footer_prints_the_release_beside_the_fetch_date(web_client: TestClient) -> None:
    """The bug: one date, labelled "sources last fetched", read as the data's age. Now two, and
    the older one is the sources' own."""
    _install(_transport())
    footer = web_client.get("/methodology").text.split('class="site-footer"')[1]
    assert "fetched" in footer
    assert "2026-09-13" in footer
    assert "oldest source release" in footer
    assert ">2017</a>" in footer


def test_the_footer_says_no_release_rather_than_repeating_the_fetch_date(
    web_client: TestClient,
) -> None:
    no_vintage = {**HEALTH, "source_vintage": {"oldest": None, "oldest_label": None}}
    _install(_transport(**{"/v1/health": (200, no_vintage)}))
    footer = web_client.get("/methodology").text.split('class="site-footer"')[1]
    assert "no source states a release" in footer


def test_the_footer_links_the_coverage_page_from_every_page(web_client: TestClient) -> None:
    _install(_transport())
    footer = web_client.get("/methodology").text.split('class="site-footer"')[1]
    assert 'href="/methodology"' in footer


# -------------------------------------------------------------------------- /methodology
def test_the_page_sets_each_source_release_against_the_date_we_fetched(
    web_client: TestClient,
) -> None:
    _install(_transport())
    body = web_client.get("/methodology").text
    section = body.split('id="vintage"')[1].split("</section>")[0]
    assert "July 2026" in section
    assert "2026-09-13" in section
    assert "the release is in the name of the file we fetch" in section


def test_a_source_stating_no_release_is_labelled_not_left_blank(web_client: TestClient) -> None:
    _install(_transport())
    section = web_client.get("/methodology").text.split('id="vintage"')[1].split("</section>")[0]
    assert "states none" in section
    assert "the source publishes no release label" in section


def test_the_page_states_the_absent_technologies_and_the_withheld_registers(
    web_client: TestClient,
) -> None:
    _install(_transport())
    section = web_client.get("/methodology").text.split('id="absences"')[1].split('id="states"')[0]
    assert "load" in section
    assert "PJM New Services Queue" in section
    assert "MISO Generator Interconnection Queue" in section
    assert "terms require a licence or consent we do not hold" in section


def test_the_page_states_ownership_depth_with_measured_numbers(web_client: TestClient) -> None:
    _install(_transport())
    section = web_client.get("/methodology").text.split('id="ownership"')[1]
    assert "7,114" in section
    assert "7,412" in section


def test_written_prose_carries_its_date_and_derived_numbers_do_not(web_client: TestClient) -> None:
    _install(_transport())
    body = web_client.get("/methodology").text
    assert "No large-load or data-centre proposals." in body
    assert 'Written <span class="tnum">2026-09-20</span>' in body


def test_the_page_defines_each_status_and_shows_the_raw_values_that_map_into_it(
    web_client: TestClient,
) -> None:
    _install(_transport())
    section = web_client.get("/methodology").text.split('id="states"')[1]
    assert "Inside the operator&#39;s process." in section or "Inside the operator's process." in section
    assert "ACTIVE" in section
    assert "DISIS STAGE" in section
    assert "source withheld, produces no rows" in section, (
        "a mapping whose source is withheld is shown, not hidden"
    )


def test_the_fallback_state_says_why_it_has_no_mappings(web_client: TestClient) -> None:
    _install(_transport())
    block = web_client.get("/methodology").text.split('id="state-unknown"')[1].split("</div>")[0]
    assert "falling through every mapping" in block


def test_a_state_no_source_produces_says_so(web_client: TestClient) -> None:
    _install(_transport())
    block = web_client.get("/methodology").text.split('id="state-frozen"')[1].split("</div>")[0]
    assert "No loaded source maps into this state." in block


def test_the_page_makes_the_four_state_pre_construction_claim_with_a_measured_count(
    web_client: TestClient,
) -> None:
    _install(_transport())
    section = web_client.get("/methodology").text.split('id="states"')[1]
    assert "4,111" in section
    assert "permitted" in section


# --------------------------------------------------------------- the note where it bites
def test_absence_note_names_a_technology_no_source_produces() -> None:
    note = absence_note(COVERAGE["data"], {"technology": "load"})
    assert note is not None
    assert any("load proposals at all" in line for line in note["lines"])


def test_absence_note_lists_the_withheld_supply_registers_by_operator() -> None:
    note = absence_note(COVERAGE["data"], {})
    assert note is not None
    joined = " ".join(note["lines"])
    assert "PJM Interconnection" in joined
    assert "MISO" in joined
    assert "on any tier" in joined


def test_absence_note_says_nothing_when_there_is_nothing_specific_to_say() -> None:
    facts = {
        "technologies": {"absent": []},
        "sources": {"withheld": [{"source_id": "x", "name": "X", "supply": False}]},
    }
    assert absence_note(facts, {"technology": "solar"}) is None


def test_absence_note_degrades_to_silence_when_coverage_is_unavailable() -> None:
    """A note about coverage is worth having; it is not worth a 500."""
    assert absence_note({}, {"technology": "load"}) is None


def test_a_filtered_out_technology_is_not_reported_as_absent() -> None:
    note = absence_note(COVERAGE["data"], {"technology": "solar"})
    assert note is not None
    assert not any("solar" in line for line in note["lines"])


# ----------------------------------------------------- the note, rendered only where it bites
EMPTY_PROPOSALS: dict[str, Any] = {
    "data": [],
    "meta": {"total": 0, "total_is_estimate": False},
    "page": {"has_more": False, "next_cursor": None, "prev_cursor": None},
}
VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}, {"value": "load"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [],
    }
}
GEO_TOTALS: dict[str, Any] = {"data": {"totals": {"lifecycle_state_counts": {}}}}


def test_an_empty_proposal_list_says_what_could_not_have_matched(web_client: TestClient) -> None:
    _install(
        _transport(
            **{
                "/v1/proposals": (200, EMPTY_PROPOSALS),
                "/v1/proposals/geo": (200, GEO_TOTALS),
                "/v1/meta/vocabularies": (200, VOCAB),
            }
        )
    )
    body = web_client.get("/proposals", params={"technology": "load"}).text
    assert "No proposals match these filters." in body
    assert "load proposals at all" in body
    assert "interconnection registers are withheld" in body
    assert 'href="/methodology#absences"' in body


def test_a_list_with_rows_carries_no_coverage_caveat(web_client: TestClient) -> None:
    """A caveat printed beside matching records is a disclaimer: skimmed, and it costs the page
    more than it pays. The statement lives on `/methodology`, linked from every footer."""
    populated = {
        **EMPTY_PROPOSALS,
        "data": [
            {
                "public_id": "prop_1",
                "slug": "p-1",
                "name_canonical": "P",
                "kind": "generation",
                "technology": "solar",
                "lifecycle_state": "filed",
                "jurisdiction": "US-TX",
                "capacity_mw": 100.0,
                "provenance": [],
            }
        ],
        "meta": {"total": 1, "total_is_estimate": False},
    }
    _install(
        _transport(
            **{
                "/v1/proposals": (200, populated),
                "/v1/proposals/geo": (200, GEO_TOTALS),
                "/v1/meta/vocabularies": (200, VOCAB),
            }
        )
    )
    body = web_client.get("/proposals").text
    assert "interconnection registers are withheld" not in body


# ---------------------------------------------------- the company page's empty parent field
def _org_entity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "org_01ACME",
        "slug": "acme-power",
        "name_canonical": "Acme Power LLC",
        "type": "developer",
        "country": "US",
        "jurisdiction": "US-TX",
        "website": None,
        "ids": {},
        "provenance": [],
        "parent": None,
        "parent_edge": None,
        "ancestors": [],
        "subsidiaries": [],
        "subsidiary_count": 0,
        "descendant_count": 0,
        "asset_counts": {},
        "group_asset_counts": {},
        "group_scope": {
            "scope": "all",
            "organizations": 1,
            "depth": 0,
            "depth_capped": False,
            "truncated": False,
            "cycle_detected": False,
            "max_depth": 10,
            "max_organizations": 500,
        },
    }
    base.update(overrides)
    return base


def _org_transport(entity: dict[str, Any]) -> FakeTransport:
    pid = entity["public_id"]
    return _transport(
        **{
            "/v1/meta/vocabularies": (200, VOCAB),
            "/v1/sources": (200, {"data": []}),
            "/v1/organizations": (200, {"data": [entity]}),
            f"/v1/organizations/{pid}/assets": (
                200,
                {"data": [], "totals": {}, "scope": entity["group_scope"]},
            ),
            f"/v1/organizations/{pid}/proposals": (200, {"data": []}),
            f"/v1/organizations/{pid}/opportunities": (200, {"data": []}),
            f"/v1/organizations/{pid}/nearby-proposals": (200, {"data": [], "totals": {}}),
        }
    )


def test_a_company_with_no_parent_says_that_is_the_usual_case(web_client: TestClient) -> None:
    """An empty parent field reads as "independent"; it almost always means "not in GLEIF". The
    ratio is measured, so the sentence cannot drift away from the data."""
    _install(_org_transport(_org_entity()))
    panel = web_client.get("/organizations/acme-power").text.split('id="parent-provenance"')[1]
    assert "No parent recorded" in panel
    assert "7114 of 7412" in panel
    assert 'href="/methodology#ownership"' in panel


def test_a_company_with_a_parent_is_unchanged(web_client: TestClient) -> None:
    parent = {"public_id": "org_01HOLD", "slug": "holdco", "name_canonical": "Holdco", "type": "fund"}
    entity = _org_entity(
        parent=parent,
        parent_edge={
            "organization": parent,
            "source_id": "global.gleif.lei",
            "as_of": "2024-02-29",
            "share_pct": None,
        },
    )
    _install(_org_transport(entity))
    body = web_client.get("/organizations/acme-power").text
    assert "No parent recorded" not in body
    assert "Holdco" in body


# --------------------------------------------------------------------- the attribution page
def test_the_attribution_page_carries_the_release_to_cite(web_client: TestClient) -> None:
    """A citation names a release ("... July 2026 release"), not the day the citer downloaded it.
    The existing attribution rendering is extended, not reworded."""
    sources = [
        {
            "source_id": "us.eia.860m",
            "name": "EIA-860M",
            "operator": "EIA",
            "url": "https://example.org/860m",
            "licence": {
                "name": "Public domain",
                "url": None,
                "reuse_class": "open",
                "attribution_text": None,
            },
            "attribution_text": "Source: US EIA",
            "vintage": {
                "value": "2026-07",
                "label": "July 2026",
                "basis": "artefact_filename",
                "stated": True,
            },
        },
        {
            "source_id": "us.iso.caiso.gen_queue",
            "name": "CAISO Public Queue Report",
            "operator": "California ISO",
            "url": "https://example.org/caiso",
            "licence": {
                "name": "CAISO Terms",
                "url": None,
                "reuse_class": "attribution",
                "attribution_text": "Source: California ISO",
            },
            "attribution_text": None,
            "vintage": {"value": None, "label": None, "basis": "not_stated", "stated": False},
        },
    ]
    _install(_transport(**{"/v1/sources": (200, {"data": sources})}))
    body = web_client.get("/attribution").text
    assert "July 2026" in body
    assert "states none" in body
    # The credit lines themselves are untouched.
    assert "Source: US EIA" in body
    assert "Source: California ISO" in body
