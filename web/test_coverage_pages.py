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
from web.viewmodels import absence_note, asset_count_note

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
        # The dev load as measured 2026-09-22 (docs/24): ethanol is two overlapping sources, RNG
        # is two disjoint ones, power plants are one.
        "assets": {
            "resolution": {"exists": False, "mechanism": "asset_source"},
            "by_type": {
                "power_plant": {
                    "rows": 14659,
                    "located": 14659,
                    "sources": {"us.eia.860m": {"rows": 14659, "located": 14659}},
                    "source_count": 1,
                    "resolved": False,
                },
                "ethanol_plant": {
                    "rows": 388,
                    "located": 197,
                    "sources": {
                        "us.eia.atlas.ethanol_plants": {"rows": 197, "located": 197},
                        "us.eia.ethanol_capacity": {"rows": 191, "located": 0},
                    },
                    "source_count": 2,
                    "resolved": False,
                },
                "rng_project": {
                    "rows": 1851,
                    "located": 1339,
                    "sources": {
                        "us.epa.agstar": {"rows": 498, "located": 0},
                        "us.epa.lmop": {"rows": 1353, "located": 1339},
                    },
                    "source_count": 2,
                    "resolved": False,
                },
            },
            "unresolved_multi_source": ["ethanol_plant", "rng_project"],
        },
        "notes": [
            {
                "id": "no_large_load",
                "headline": "No large-load or data-centre proposals.",
                "body": "No loaded source publishes large-load interconnection requests as data.",
                "written": "2026-09-20",
                "applies_to": {"absent_technology": "load"},
                "figures": {},
            },
            {
                "id": "ethanol_two_sources",
                "headline": (
                    "Ethanol plants: 388 rows from two EIA sources, not yet resolved to one record per plant."
                ),
                "body": "About 201 distinct plants (measured 2026-09-22, docs/24).",
                "written": "2026-09-25",
                "applies_to": {"unresolved_asset_type": "ethanol_plant"},
                "figures": {"measured": "2026-09-22", "distinct_estimate": 201, "redundant_share": 0.482},
            },
            {
                "id": "rng_two_sources",
                "headline": "RNG projects: two EPA sources, covering different populations.",
                "body": "No project was found in both (0 cross-source links, measured 2026-09-22, docs/24).",
                "written": "2026-09-25",
                "applies_to": {"unresolved_asset_type": "rng_project"},
                "figures": {"measured": "2026-09-22", "cross_source_links": 0},
            },
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
    # `asset_type_counts()` keeps its cache in `__dict__` directly (web/app.py), so this one
    # does pop.
    web_app.state.__dict__.pop("asset_type_counts_cache", None)


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


# ------------------------------------------------- sources per asset type, on /methodology
def test_the_page_states_sources_and_rows_per_asset_type_with_resolution(web_client: TestClient) -> None:
    """Derived: rows per source, located rows, and whether anything resolves two sources."""
    _install(_transport())
    section = web_client.get("/methodology").text.split('id="asset-sources"')[1].split("</table>")[0]
    assert "No such resolution layer exists today" in section.split("<table")[0]
    ethanol = section.split('id="asset-sources-ethanol_plant"')[1].split("</tr>")[0]
    assert "388" in ethanol
    assert "197" in ethanol and "with a location" in ethanol
    assert "us.eia.atlas.ethanol_plants" in ethanol and "us.eia.ethanol_capacity" in ethanol
    assert "191" in ethanol
    assert "<strong>Not yet</strong>" in ethanol
    assert 'href="#note-ethanol_two_sources">measured</a>' in ethanol
    power = section.split('id="asset-sources-power_plant"')[1].split("</tr>")[0]
    assert "One source, so nothing to resolve" in power
    assert "with a location" not in power, "every power plant is located; no badge to print"


def test_the_ethanol_note_prints_the_estimate_beside_the_derived_count(web_client: TestClient) -> None:
    """Both numbers: the measured 201 in the written note, the derived 388 and 197 beside it,
    so a reader can see whether the two still agree."""
    _install(_transport())
    block = web_client.get("/methodology").text.split('id="note-ethanol_two_sources"')[1].split("</div>")[0]
    assert "not yet resolved to one record per plant" in block
    assert "About 201 distinct plants" in block
    assert 'On this load: <span class="tnum">388</span> rows from 2 sources' in block
    assert '<span class="tnum">197</span> with a location' in block
    assert "withdrawn automatically" in block
    assert 'Written <span class="tnum">2026-09-25</span>' in block


def test_a_note_without_an_asset_fact_gets_no_derived_line(web_client: TestClient) -> None:
    _install(_transport())
    block = web_client.get("/methodology").text.split('id="note-no_large_load"')[1].split("</div>")[0]
    assert "On this load:" not in block


def test_the_page_survives_a_coverage_body_with_no_asset_fact(web_client: TestClient) -> None:
    """An API a release behind this template must cost the table, not the page."""
    data = {k: v for k, v in COVERAGE["data"].items() if k != "assets"}
    data["notes"] = [n for n in data["notes"] if "unresolved_asset_type" not in n.get("applies_to", {})]
    _install(_transport(**{"/v1/coverage": (200, {"data": data})}))
    resp = web_client.get("/methodology")
    assert resp.status_code == 200
    assert 'id="asset-sources"' not in resp.text


# --------------------------------------- the corrected count, only where the count is wrong
def _notes_without(note_id: str) -> list[dict[str, Any]]:
    return [n for n in COVERAGE["data"]["notes"] if n["id"] != note_id]


def test_asset_count_note_gives_both_numbers_for_an_unresolved_type_with_an_estimate() -> None:
    note = asset_count_note(COVERAGE["data"], {"ethanol_plant"})
    assert note == {
        "rows": 388,
        "located": 197,
        "source_count": 2,
        "estimate": 201,
        "measured": "2026-09-22",
        "href": "/methodology#note-ethanol_two_sources",
    }


def test_asset_count_note_is_silent_for_two_sources_that_do_not_overlap() -> None:
    """RNG has the derived fact (two sources, no resolution) and a note, but no estimate,
    because its sources are disjoint and its row count is right. A line there would be the
    disclaimer the proposals rule forbids."""
    assert asset_count_note(COVERAGE["data"], {"rng_project"}) is None


def test_asset_count_note_is_silent_for_a_single_source_type_and_for_mixed_views() -> None:
    assert asset_count_note(COVERAGE["data"], {"power_plant"}) is None
    assert asset_count_note(COVERAGE["data"], set()) is None, "the unfiltered index mixes seven types"
    assert asset_count_note(COVERAGE["data"], {"ethanol_plant", "rng_project"}) is None


def test_asset_count_note_retires_with_the_fact_or_the_note() -> None:
    """The web side follows the API's retirement: a resolved fact, or a note the API stopped
    returning, and the line is gone."""
    resolved = {
        **COVERAGE["data"],
        "assets": {
            **COVERAGE["data"]["assets"],
            "by_type": {
                **COVERAGE["data"]["assets"]["by_type"],
                "ethanol_plant": {**COVERAGE["data"]["assets"]["by_type"]["ethanol_plant"], "resolved": True},
            },
        },
    }
    assert asset_count_note(resolved, {"ethanol_plant"}) is None
    without_note = {**COVERAGE["data"], "notes": _notes_without("ethanol_two_sources")}
    assert asset_count_note(without_note, {"ethanol_plant"}) is None


def test_asset_count_note_degrades_to_silence_when_coverage_is_unavailable() -> None:
    assert asset_count_note({}, {"ethanol_plant"}) is None


ASSET_VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}],
        "proposal_kind": [],
        "opportunity_kind": [],
        "opportunity_status": [],
        "slip_bucket": [],
    }
}


def _asset_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "public_id": "asset_01FAIRMONT",
        "slug": "poet-fairmont-ne",
        "name": "Poet Biorefining-Fairmont",
        "asset_type": "ethanol_plant",
        "status": "operating",
        "operator_name": "Poet",
        "technology": None,
        "capacity_mw": None,
        "state_code": "US-NE",
        "county_name": None,
        "country": "US",
        "attributes": {},
        "provenance": [
            {
                "source_id": "us.eia.atlas.ethanol_plants",
                "source_name": "EIA Energy Atlas",
                "source_url": "https://atlas.eia.gov/",
                "retrieved_at": "2026-09-19T00:00:00Z",
                "reuse_class": "open",
                "source_record_id": "NE-poet-fairmont",
            }
        ],
    }
    base.update(overrides)
    return base


def _asset_transport(rows: list[dict[str, Any]], counts: dict[str, int]) -> FakeTransport:
    return _transport(
        **{
            "/v1/meta/vocabularies": (200, ASSET_VOCAB),
            "/v1/assets": (200, {"data": rows, "page": {"has_more": False, "next_cursor": None}, "meta": {}}),
            "/v1/assets/geo": (
                200,
                {
                    "data": {
                        "type": "FeatureCollection",
                        "features": [],
                        "totals": {"records": sum(counts.values()), "asset_type_counts": counts},
                    },
                    "meta": {},
                },
            ),
        }
    )


ASSET_COUNTS = {"power_plant": 14659, "ethanol_plant": 197, "rng_project": 1339}


def test_the_ethanol_list_prints_the_row_count_and_the_measured_plant_count(web_client: TestClient) -> None:
    """The one populated list that carries a coverage line, because its count is the thing that
    is wrong: both numbers, the fusion-state wording, the date, and the link to the method."""
    _install(_asset_transport([_asset_row()], ASSET_COUNTS))
    body = web_client.get("/assets", params={"asset_type": "ethanol_plant"}).text
    line = body.split('id="count-note"')[1].split("</p>")[0]
    assert "388 rows" in line
    assert 'about <strong class="tnum">201</strong> ethanol plants' in line
    assert "Two sources, not yet resolved to one record per asset" in line
    assert "2026-09-22" in line
    assert "The count above is of the 197 rows with a location." in line
    assert 'href="/methodology#note-ethanol_two_sources"' in line
    assert "duplicate" not in line.lower() and "bug" not in line.lower()
    # The chips and the per-page count are unchanged: the line is added beside them, not in place of them.
    assert "1 asset on this page" in body
    assert 'href="/assets?asset_type=ethanol_plant"' in body


def test_the_unfiltered_index_and_a_single_source_type_carry_no_count_line(web_client: TestClient) -> None:
    _install(_asset_transport([_asset_row()], ASSET_COUNTS))
    assert 'id="count-note"' not in web_client.get("/assets").text
    assert 'id="count-note"' not in web_client.get("/assets", params={"asset_type": "power_plant"}).text


def test_the_rng_list_carries_no_count_line_because_its_count_is_right(web_client: TestClient) -> None:
    _install(_asset_transport([_asset_row(asset_type="rng_project")], ASSET_COUNTS))
    assert 'id="count-note"' not in web_client.get("/assets", params={"asset_type": "rng_project"}).text


def test_the_ethanol_list_line_disappears_when_the_note_retires(web_client: TestClient) -> None:
    """Simulated at the API boundary: `/v1/coverage` no longer returns the note (the type has
    one source, or a resolution exists) and the page prints nothing, with no edit here."""
    data = {**COVERAGE["data"], "notes": _notes_without("ethanol_two_sources")}
    transport = _asset_transport([_asset_row()], ASSET_COUNTS)
    transport.responses["/v1/coverage"] = (200, {"data": data})
    _install(transport)
    assert 'id="count-note"' not in web_client.get("/assets", params={"asset_type": "ethanol_plant"}).text


def test_the_asset_list_renders_when_coverage_is_unavailable(web_client: TestClient) -> None:
    transport = _asset_transport([_asset_row()], ASSET_COUNTS)
    transport.responses["/v1/coverage"] = (500, {"title": "boom"})
    _install(transport)
    resp = web_client.get("/assets", params={"asset_type": "ethanol_plant"})
    assert resp.status_code == 200
    assert 'id="count-note"' not in resp.text
