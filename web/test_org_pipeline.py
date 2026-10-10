"""A company's pipeline at the top of its page (owner, 2026-10-10: "check a developer's or owner's
pipeline with a source for every claim"; `web/org_pipeline.py`, `partials/_org_pipeline.html`).

Held here: the counts and MW by status, technology and ISO are the API's aggregate
(`GET /v1/organizations/{id}/pipeline`) and nothing else, so the summary is never partial; the
active status groups count only records a register still lists, and the ones every register has
dropped are their own row; every count links to the list with the API's `list_query` and the
filter the API documents for its bucket (one sponsor id, or the id and `sponsor_scope` for a
group); the registers behind the rows are named with how many each backs. That each link's total
equals its count against a real API is `tests/test_api_org_pipeline.py`. A fake `Transport` stands
in for the API, as in `web/test_ownership.py` (duplicated, not imported: no `web/test_*.py` imports
another).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.org_pipeline import PAGE_SIZE, fetch_pipeline, fetch_proposals, pipeline_summary

Answer = tuple[int, Any] | Callable[[Mapping[str, Any]], tuple[int, Any]]


class FakeTransport:
    def __init__(self, responses: Mapping[str, Answer]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _respond(self, url: str, params: Mapping[str, Any] | None) -> httpx.Response:
        if url in self.responses:
            answer = self.responses[url]
            status, body = answer(params or {}) if callable(answer) else answer
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append((url, dict(params or {})))
        return self._respond(url, params)

    def post(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(202, json={})

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self._respond(url, kwargs.get("params"))

    def close(self) -> None:
        pass


ORG_ID = "org_01CYPRESS"
ACTIVE = ["announced", "filed", "studied", "permitted", "contracted", "under_construction"]


def _t(
    records: int, mw: float | None = None, capacity: int | None = None, not_summed: int = 0
) -> dict[str, Any]:
    return {
        "records": records,
        "capacity_mw": mw,
        "capacity_records": capacity if capacity is not None else (records if mw is not None else 0),
        "not_summed_records": not_summed,
    }


def _state(state: str, listed: dict[str, Any], not_listed: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"lifecycle_state": state, "listed": listed, "not_listed": not_listed or _t(0)}


#: What the API answers for a company with 9 records: 1 announced (100 MW); in process 2 filed
#: (200 MW + a 1,000 MW load, not summed), 1 studied (50.5 MW) and 1 filed with no technology or
#: operator (20 MW); 1 contracted with no capacity; 1 built; 1 withdrawn; and 1 filed solar project
#: (40 MW) every register has dropped.
AGGREGATE: dict[str, Any] = {
    "organization": {"public_id": ORG_ID, "slug": "cypress-creek", "name_canonical": "Cypress Creek"},
    "scope": {"scope": "self", "organizations": 1},
    "list_query": {"sponsor_id": ORG_ID},
    "active_states": ACTIVE,
    "capacity_not_summed_kinds": ["load", "transmission", "pipeline", "lng", "ccs", "hydrogen"],
    "totals": _t(9, 790.5, capacity=7, not_summed=1),
    "active": _t(6, 370.5, capacity=4, not_summed=1),
    "not_listed": _t(1, 40.0),
    "by_lifecycle_state": [
        _state("announced", _t(1, 100.0)),
        _state("filed", _t(3, 220.0, capacity=2, not_summed=1), _t(1, 40.0)),
        _state("studied", _t(1, 50.5)),
        _state("contracted", _t(1, None)),
        _state("built", _t(1, 300.0)),
        _state("withdrawn", _t(1, 80.0)),
    ],
    "by_technology": [
        {"technology": "solar", **_t(2, 300.0, capacity=1)},
        {"technology": "storage", **_t(1, 50.5)},
        {"technology": "load", **_t(1, None, capacity=0, not_summed=1)},
        {"technology": None, **_t(1, 20.0)},
    ],
    "by_iso": [
        {"iso": "ERCOT", **_t(3, 300.0, capacity=2, not_summed=1)},
        {"iso": "MISO", **_t(2, 50.5, capacity=1)},
        {"iso": None, **_t(1, 20.0)},
    ],
    "not_summed_by_kind": [{"kind": "load", "records": 1}],
    "sources": [
        {
            "source_id": "us.iso.ercot.gen_queue",
            "name": "ERCOT GIS Report",
            "records": 8,
            "retrieved_at_max": "2026-10-01T00:00:00Z",
            "licence_id": "us.iso.ercot.gen_queue#59c5de49c6",
            "reuse_class": "open",
            "attribution_text": None,
        },
        {
            "source_id": "us.eia.860m",
            "name": "EIA-860M",
            "records": 1,
            "retrieved_at_max": "2026-09-12T00:00:00Z",
            "licence_id": "us.eia.860m#f142611d41",
            "reuse_class": "open",
            "attribution_text": None,
        },
    ],
}


def _query(href: str | None) -> dict[str, list[str]]:
    assert href is not None and href.startswith("/proposals?")
    return parse_qs(urlsplit(href).query)


# ------------------------------------------------------------------------------------- summary
def test_status_rows_count_listed_active_records_and_drop_the_unlisted_into_their_own_row() -> None:
    summary = pipeline_summary(AGGREGATE)
    assert summary is not None
    by_status = {r["label"]: (r["count"], r["mw"]) for r in summary["by_status"]}
    assert by_status == {
        "Announced": (1, 100.0),
        "In process": (4, 270.5),  # 3 filed + 1 studied, listed; the dropped filed project is not here
        "Contracted": (1, None),  # no capacity stated: a dash, never a zero
        "No longer listed": (1, 40.0),
        "Built or operating": (1, 300.0),
        "Withdrawn or cancelled": (1, 80.0),
    }
    assert sum(r["count"] for r in summary["by_status"]) == summary["total"]["count"] == 9
    assert (summary["active"]["count"], summary["active"]["mw"]) == (6, 370.5)
    assert summary["not_listed"]["count"] == 1
    assert summary["not_summed"] == [
        {"kind": "load", "label": "Load (data centres, large loads)", "count": 1}
    ]
    assert summary["no_capacity"] == 1  # 9 records, 7 with a summed capacity, 1 not summed
    assert [r["family"] for r in summary["by_status"]] == [
        "neutral",
        "progress",
        "committed",
        "neutral",
        "success",
        "danger",
    ]


def test_technology_and_iso_rows_are_the_apis_buckets_in_its_order() -> None:
    summary = pipeline_summary(AGGREGATE)
    assert summary is not None
    tech = [(r["label"], r["count"], r["mw"]) for r in summary["by_technology"]]
    assert tech == [
        ("Solar", 2, 300.0),
        ("Storage", 1, 50.5),
        ("Large load", 1, None),
        ("Not stated", 1, 20.0),
    ]
    iso = [(r["label"], r["count"], r["mw"]) for r in summary["by_iso"]]
    assert iso == [("ERCOT", 3, 300.0), ("MISO", 2, 50.5), ("None stated", 1, 20.0)]


def test_every_count_links_with_the_apis_list_query_and_its_buckets_filter() -> None:
    summary = pipeline_summary(AGGREGATE)
    assert summary is not None and summary["linked"]
    rows = {r["label"]: r for r in summary["by_status"]}
    assert _query(rows["In process"]["href"]) == {
        "sponsor_id": [ORG_ID],
        "lifecycle_state": ["filed,studied,permitted,under_construction"],
        "listed": ["true"],
    }
    assert _query(rows["No longer listed"]["href"]) == {
        "sponsor_id": [ORG_ID],
        "lifecycle_state": [",".join(ACTIVE)],
        "listed": ["false"],
    }
    # Built, withdrawn and unknown count every record in those states, listed or not.
    assert _query(rows["Withdrawn or cancelled"]["href"]) == {
        "sponsor_id": [ORG_ID],
        "lifecycle_state": ["withdrawn,cancelled"],
    }
    # The active pipeline is the list's default view of the listed records.
    assert _query(summary["active"]["href"]) == {"sponsor_id": [ORG_ID], "listed": ["true"]}
    solar = next(r for r in summary["by_technology"] if r["label"] == "Solar")
    assert _query(solar["href"]) == {"sponsor_id": [ORG_ID], "technology": ["solar"], "listed": ["true"]}
    ercot = next(r for r in summary["by_iso"] if r["label"] == "ERCOT")
    assert _query(ercot["href"]) == {"sponsor_id": [ORG_ID], "iso": ["ERCOT"], "listed": ["true"]}
    # "Not stated" cannot be asked of the list, so it is not a link.
    assert next(r for r in summary["by_technology"] if r["label"] == "Not stated")["href"] is None
    assert _query(summary["total"]["href"])["lifecycle_state"][0].split(",") == [
        "announced",
        "filed",
        "studied",
        "permitted",
        "contracted",
        "under_construction",
        "withdrawn",
        "cancelled",
        "built",
        "unknown",
    ]


def test_a_group_links_with_one_id_and_its_scope() -> None:
    group = {
        **AGGREGATE,
        "scope": {"scope": "all", "organizations": 4},
        "list_query": {"sponsor_id": ORG_ID, "sponsor_scope": "all"},
    }
    summary = pipeline_summary(group)
    assert summary is not None and summary["group"]
    assert _query(summary["active"]["href"]) == {
        "sponsor_id": [ORG_ID],
        "sponsor_scope": ["all"],
        "listed": ["true"],
    }


def test_sources_name_each_register_with_how_many_proposals_it_backs() -> None:
    summary = pipeline_summary(AGGREGATE)
    assert summary is not None
    sources = [(s["name"], s["count"], s["retrieved_at"]) for s in summary["sources"]]
    assert sources[0][0] == "ERCOT" and sources[0][1] == 8
    assert sources[1] == ("EIA-860M", 1, "2026-09-12T00:00:00Z")
    href = _query(summary["sources"][1]["href"])
    assert href["source_id"] == ["us.eia.860m"] and href["sponsor_id"] == [ORG_ID]
    assert len(href["lifecycle_state"][0].split(",")) == 10


def test_nothing_sponsored_or_nothing_read_renders_no_summary() -> None:
    assert pipeline_summary(None) is None
    assert pipeline_summary({**AGGREGATE, "totals": _t(0)}) is None


# ------------------------------------------------------------------------------------- reading
def test_the_pipeline_is_one_read_at_the_pages_scope() -> None:
    transport = FakeTransport({f"/v1/organizations/{ORG_ID}/pipeline": (200, {"data": AGGREGATE})})
    assert fetch_pipeline(ApiClient(transport), ORG_ID, {"scope": "all"}) == AGGREGATE
    assert transport.calls == [(f"/v1/organizations/{ORG_ID}/pipeline", {"scope": "all"})]


def test_a_failed_pipeline_read_leaves_the_section_out() -> None:
    transport = FakeTransport({f"/v1/organizations/{ORG_ID}/pipeline": (500, {"title": "boom"})})
    assert fetch_pipeline(ApiClient(transport), ORG_ID, {}) is None


def test_the_list_section_reads_one_page() -> None:
    rows = [{"public_id": f"prop_{n}", "slug": f"p-{n}"} for n in range(PAGE_SIZE)]
    transport = FakeTransport(
        {f"/v1/organizations/{ORG_ID}/proposals": (200, {"data": rows, "page": {"has_more": True}})}
    )
    pages = fetch_proposals(ApiClient(transport), ORG_ID, {"scope": "all"})
    assert len(pages.rows) == PAGE_SIZE and not pages.complete and not pages.failed
    assert transport.calls == [
        (f"/v1/organizations/{ORG_ID}/proposals", {"limit": PAGE_SIZE, "scope": "all"})
    ]


def test_a_failed_list_read_leaves_the_section_empty() -> None:
    transport = FakeTransport({f"/v1/organizations/{ORG_ID}/proposals": (500, {"title": "boom"})})
    pages = fetch_proposals(ApiClient(transport), ORG_ID, {})
    assert pages.rows == [] and pages.failed


# ---------------------------------------------------------------------------------------- page
ORG = {
    "public_id": ORG_ID,
    "slug": "cypress-creek",
    "name_canonical": "CYPRESS CREEK RENEWABLES LLC",
    "type": "developer",
    "country": "US",
    "provenance": [],
    "descendant_count": 0,
    "subsidiary_count": 0,
}


def _proposal(n: int) -> dict[str, Any]:
    return {
        "public_id": f"prop_{n:03d}",
        "slug": f"p-{n}",
        "name_canonical": f"Project {n}",
        "kind": "generation",
        "technology": "solar",
        "capacity_mw": 1.0,
        "iso": "ERCOT",
        "jurisdiction": "US-TX",
        "lifecycle_state": "filed",
        "listed": True,
        "delisted_at": None,
        "sponsor": {"public_id": ORG_ID, "name_canonical": "Cypress Creek"},
        "provenance": [],
    }


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(web_app) as test_client:
        yield test_client
    for key in ("api_client", "lag_days_default", "coverage_facts", "coverage_data", "build_info"):
        try:
            delattr(web_app.state, key)
        except (AttributeError, KeyError):
            pass


def _install(aggregate: Answer, proposals: Answer) -> FakeTransport:
    transport = FakeTransport(
        {
            "/v1/health": (200, {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}),
            "/v1/meta/vocabularies": (200, {"data": {"technology": [{"value": "solar"}]}}),
            "/v1/organizations": (200, {"data": [ORG]}),
            f"/v1/organizations/{ORG_ID}": (200, {"data": ORG}),
            f"/v1/organizations/{ORG_ID}/assets": (
                200,
                {"data": [], "totals": {}, "scope": {"organizations": 1}},
            ),
            f"/v1/organizations/{ORG_ID}/pipeline": aggregate,
            f"/v1/organizations/{ORG_ID}/proposals": proposals,
            f"/v1/organizations/{ORG_ID}/opportunities": (200, {"data": []}),
            f"/v1/organizations/{ORG_ID}/nearby-proposals": (200, {"data": [], "totals": {}}),
            "/v1/sources/us.iso.ercot.gen_queue": (200, {"data": {"licence": {"quote_text": "Open."}}}),
            "/v1/sources/us.eia.860m": (200, {"data": {"licence": {"quote_text": "Public domain."}}}),
        }
    )
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    return transport


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", "", html)).split())


def test_the_company_page_opens_with_its_pipeline_and_the_registers_behind_it(client: TestClient) -> None:
    rows = [_proposal(n) for n in range(9)]
    transport = _install((200, {"data": AGGREGATE}), (200, {"data": rows, "page": {"has_more": False}}))
    body = client.get("/organizations/cypress-creek").text
    # The page's default scope (`all`) is asked of the aggregate once; no paging through the list.
    assert [c for c in transport.calls if c[0].endswith("/pipeline")] == [
        (f"/v1/organizations/{ORG_ID}/pipeline", {"scope": "all"})
    ]
    assert len([c for c in transport.calls if c[0].endswith("/proposals")]) == 1
    section = body.split('<section class="org-pipeline" id="pipeline"', 1)[1].split(">", 1)[1]
    section = section.split("</section>", 1)[0]
    # Near the top: before the assets, the nearby proposals and the proposal list.
    assert body.index('id="pipeline"') < body.index("Assets</h2>") < body.index("Proposals</h2>")
    assert _text(section).startswith(
        "Pipeline 9 proposals sponsored by this company, 6 of them in the active pipeline "
        "(announced, in process or contracted), 370.5 MW. 1 more was last stated as active but is no "
        "longer listed in any register, so it is not counted as active. Each count opens the matching list."
    )
    status = section.split('id="pipeline-by-status"', 1)[1].split("</table>", 1)[0]
    in_process = re.search(
        r"In process</th>\s*<td class=\"num tnum\">(.*?)</td>\s*<td class=\"num tnum\">(.*?)</td>",
        status,
        re.S,
    )
    assert in_process is not None
    assert (
        'href="/proposals?sponsor_id=org_01CYPRESS&amp;lifecycle_state=filed%2Cstudied%2Cpermitted%2Cunder_construction'
        '&amp;listed=true"' in in_process.group(1)
    )
    assert _text(in_process.group(1)) == "4 in process proposals" and in_process.group(2) == "270.5"
    dropped = re.search(r"No longer listed</th>\s*<td class=\"num tnum\">(.*?)</td>", status, re.S)
    assert dropped is not None and "listed=false" in dropped.group(1)
    contracted = re.search(
        r"Contracted</th>\s*<td[^>]*>.*?</td>\s*<td class=\"num tnum\">(.*?)</td>", status, re.S
    )
    assert contracted is not None and contracted.group(1) == "&mdash;"
    assert 'id="pipeline-by-technology"' in section and 'id="pipeline-by-iso"' in section
    notes = _text(section.split('org-pipeline__notes">', 1)[1].split("</p>", 1)[0])
    assert notes == (
        "MW is the sum of the capacity each register states for generation and storage; "
        "1 load (data centres, large loads) proposal is counted but not added to it; 1 states no capacity."
    )
    assert "partial" not in _text(section) and "most recently changed" not in _text(section)
    sources = _text(section.split('class="org-pipeline__sources">', 1)[1].split("</p>", 1)[0])
    assert sources.startswith(
        "From ERCOT (8 proposals, retrieved 1 Oct 2026); EIA-860M (1 proposal, retrieved 12 Sep 2026)."
    )


def test_a_capitalised_company_name_reads_in_a_readable_case(client: TestClient) -> None:
    _install((200, {"data": AGGREGATE}), (200, {"data": [_proposal(1)]}))
    body = client.get("/organizations/cypress-creek").text
    assert "<h1>Cypress Creek Renewables LLC</h1>" in body
    assert "<title>Cypress Creek Renewables LLC — Infraque</title>" in body
    assert body.count('As filed: <span class="as-filed__name">CYPRESS CREEK RENEWABLES LLC</span>') == 1


def test_the_list_section_counts_against_the_aggregates_total(client: TestClient) -> None:
    aggregate = {**AGGREGATE, "totals": _t(157, 157.0)}
    rows = [_proposal(n) for n in range(PAGE_SIZE)]
    _install((200, {"data": aggregate}), (200, {"data": rows, "page": {"has_more": True}}))
    body = client.get("/organizations/cypress-creek").text
    shown = re.search(r'<p class="attribution-line" id="proposals-shown">(.*?)</p>', body, re.S)
    assert shown is not None
    # The aggregate counted all 157, so the line says how many there are, not "or more".
    assert _text(shown.group(1)) == "The 100 most recently changed of 157; all of them in the list."
    assert body.count('<a href="/proposals/p-') == 100


def test_a_company_that_sponsors_nothing_has_no_pipeline_section(client: TestClient) -> None:
    _install((200, {"data": {**AGGREGATE, "totals": _t(0)}}), (200, {"data": []}))
    body = client.get("/organizations/cypress-creek").text
    assert 'id="pipeline"' not in body
    assert "No proposals sponsored by this company." in body


def test_an_unreadable_aggregate_leaves_the_section_out_and_the_list_in(client: TestClient) -> None:
    _install((500, {"title": "boom"}), (200, {"data": [_proposal(1)], "page": {"has_more": False}}))
    body = client.get("/organizations/cypress-creek").text
    assert 'id="pipeline"' not in body
    assert '<a href="/proposals/p-1">' in body
