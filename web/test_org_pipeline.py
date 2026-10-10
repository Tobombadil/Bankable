"""A company's pipeline at the top of its page (owner, 2026-10-10: "check a developer's or owner's
pipeline with a source for every claim"; `web/org_pipeline.py`, `partials/_org_pipeline.html`).

Held here: the counts and MW by status, technology and ISO are taken from the proposals the API
serves for the page's scope and nothing else; generation and storage MW are summed and other
kinds' MW (a data centre's demand, a line's transfer capability) is not; every count links to the
list filtered to the same rows (one sponsor id, or the group's ids); the registers behind the rows
are named with how many each backs; the read follows the API's cursor and says when it stopped
short. A fake `Transport` stands in for the API, as in `web/test_ownership.py` (duplicated, not
imported: no `web/test_*.py` imports another).
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
from web.org_pipeline import MAX_PAGES, PAGE_SIZE, fetch_proposals, pipeline_summary

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
SUB_ID = "org_01CYPRESSSUB"


def _prov(source_id: str, name: str, retrieved: str = "2026-10-01T00:00:00Z") -> dict[str, Any]:
    return {
        "source_id": source_id,
        "source_name": name,
        "source_url": f"https://example.org/{source_id}",
        "retrieved_at": retrieved,
        "reuse_class": "open",
        "attribution_text": name,
        "source_record_id": "X",
    }


def _proposal(
    n: int, state: str, technology: str | None, iso: str | None, mw: float | None, **extra: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "public_id": f"prop_{n:03d}",
        "slug": f"p-{n}",
        "name_canonical": f"Project {n}",
        "kind": extra.pop("kind", "generation"),
        "technology": technology,
        "capacity_mw": mw,
        "iso": iso,
        "jurisdiction": "US-TX",
        "lifecycle_state": state,
        "sponsor": {"public_id": extra.pop("sponsor", ORG_ID), "name_canonical": "Cypress Creek"},
        "provenance": extra.pop("provenance", [_prov("us.iso.ercot.gen_queue", "ERCOT GIS Report")]),
    }
    row.update(extra)
    return row


ROWS = [
    _proposal(1, "announced", "solar", "ERCOT", 100.0),
    _proposal(2, "filed", "solar", "ERCOT", 200.0),
    _proposal(3, "studied", "storage", "MISO", 50.5),
    _proposal(4, "contracted", "solar", "MISO", None),
    _proposal(5, "built", "wind", "ERCOT", 300.0),
    _proposal(6, "withdrawn", "solar", "ERCOT", 80.0),
    _proposal(7, "filed", "load", "ERCOT", 1000.0, kind="load"),  # demand: counted, never summed
    _proposal(
        8, "filed", None, None, 20.0, provenance=[_prov("us.eia.860m", "EIA-860M", "2026-09-12T00:00:00Z")]
    ),
]


# ------------------------------------------------------------------------------------- summary
def test_status_rows_count_and_sum_generation_and_storage_only() -> None:
    summary = pipeline_summary(ROWS)
    assert summary is not None
    by_status = {r["label"]: (r["count"], r["mw"]) for r in summary["by_status"]}
    assert by_status == {
        "Announced": (1, 100.0),
        "In process": (4, 270.5),  # 200 + 50.5 + 20; the 1,000 MW load is not added
        "Contracted": (1, None),  # no capacity stated: a dash, never a zero
        "Built or operating": (1, 300.0),
        "Withdrawn or cancelled": (1, 80.0),
    }
    assert (summary["total"]["count"], summary["total"]["mw"]) == (8, 750.5)
    assert (summary["active"]["count"], summary["active"]["mw"]) == (6, 370.5)
    assert summary["not_summed"] == [
        {"kind": "load", "label": "Load (data centres, large loads)", "count": 1}
    ]
    assert summary["no_capacity"] == 1
    assert [r["family"] for r in summary["by_status"]] == [
        "neutral",
        "progress",
        "committed",
        "success",
        "danger",
    ]


def test_technology_and_iso_rows_cover_the_active_pipeline_largest_first() -> None:
    summary = pipeline_summary(ROWS)
    assert summary is not None
    tech = [(r["label"], r["count"], r["mw"]) for r in summary["by_technology"]]
    # Built (wind) and withdrawn rows are not in the active pipeline; "not stated" goes last.
    assert tech == [
        ("Solar", 3, 300.0),
        ("Storage", 1, 50.5),
        ("Large load", 1, None),
        ("Not stated", 1, 20.0),
    ]
    iso = [(r["label"], r["count"], r["mw"]) for r in summary["by_iso"]]
    assert iso == [("ERCOT", 3, 300.0), ("MISO", 2, 50.5), ("None stated", 1, 20.0)]


def test_every_count_links_to_the_same_rows_in_the_list() -> None:
    summary = pipeline_summary(ROWS)
    assert summary is not None and summary["linked"]

    def query(href: str | None) -> dict[str, list[str]]:
        assert href is not None and href.startswith("/proposals?")
        return parse_qs(urlsplit(href).query)

    in_process = next(r for r in summary["by_status"] if r["label"] == "In process")
    assert query(in_process["href"]) == {
        "sponsor_id": [ORG_ID],
        "lifecycle_state": ["filed,studied,permitted,under_construction"],
    }
    # The active pipeline is the list's default view, so its links name no status.
    assert query(summary["active"]["href"]) == {"sponsor_id": [ORG_ID]}
    solar = next(r for r in summary["by_technology"] if r["label"] == "Solar")
    assert query(solar["href"]) == {"sponsor_id": [ORG_ID], "technology": ["solar"]}
    ercot = next(r for r in summary["by_iso"] if r["label"] == "ERCOT")
    assert query(ercot["href"]) == {"sponsor_id": [ORG_ID], "iso": ["ERCOT"]}
    # "Not stated" cannot be asked of the list, so it is not a link.
    assert next(r for r in summary["by_technology"] if r["label"] == "Not stated")["href"] is None
    total = query(summary["total"]["href"])
    assert total["lifecycle_state"][0].split(",") == [
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


def test_sources_name_each_register_with_how_many_proposals_it_backs() -> None:
    summary = pipeline_summary(ROWS)
    assert summary is not None
    sources = [(s["name"], s["count"], s["retrieved_at"]) for s in summary["sources"]]
    assert sources[0][0] == "ERCOT" and sources[0][1] == 7
    assert sources[1] == ("EIA-860M", 1, "2026-09-12T00:00:00Z")
    href = parse_qs(urlsplit(summary["sources"][1]["href"]).query)
    assert href["source_id"] == ["us.eia.860m"] and href["sponsor_id"] == [ORG_ID]


def test_a_group_page_links_with_every_sponsor_behind_the_rows() -> None:
    rows = [*ROWS, _proposal(9, "filed", "solar", "ERCOT", 10.0, sponsor=SUB_ID)]
    summary = pipeline_summary(rows, group=True)
    assert summary is not None and summary["group"]
    assert parse_qs(urlsplit(summary["active"]["href"]).query)["sponsor_id"] == [f"{ORG_ID},{SUB_ID}"]


def test_no_links_when_a_row_does_not_name_its_sponsor() -> None:
    """The list could not be filtered to that row, so a link would open fewer rows than counted."""
    rows = [*ROWS, _proposal(9, "filed", "solar", "ERCOT", 10.0) | {"sponsor": None}]
    summary = pipeline_summary(rows)
    assert summary is not None and not summary["linked"]
    assert all(r["href"] is None for r in summary["by_status"])


def test_nothing_sponsored_renders_no_summary() -> None:
    assert pipeline_summary([]) is None


# ------------------------------------------------------------------------------------- reading
def _paged(rows: list[dict[str, Any]], size: int) -> Callable[[Mapping[str, Any]], tuple[int, Any]]:
    def answer(params: Mapping[str, Any]) -> tuple[int, Any]:
        start = int(params.get("cursor") or 0)
        chunk = rows[start : start + size]
        more = start + size < len(rows)
        return 200, {
            "data": chunk,
            "page": {"next_cursor": str(start + size) if more else None, "has_more": more},
        }

    return answer


def test_the_read_follows_the_cursor_to_the_end() -> None:
    rows = [_proposal(n, "filed", "solar", "ERCOT", 1.0) for n in range(450)]
    transport = FakeTransport({f"/v1/organizations/{ORG_ID}/proposals": _paged(rows, PAGE_SIZE)})
    pages = fetch_proposals(ApiClient(transport), ORG_ID, {"scope": "all"})
    assert len(pages.rows) == 450 and pages.complete and not pages.failed
    assert [c[1].get("cursor") for c in transport.calls] == [None, "200", "400"]
    assert all(c[1]["limit"] == PAGE_SIZE and c[1]["scope"] == "all" for c in transport.calls)


def test_the_read_stops_at_its_cap_and_says_so() -> None:
    rows = [_proposal(n, "filed", "solar", "ERCOT", 1.0) for n in range(PAGE_SIZE * MAX_PAGES + 1)]
    transport = FakeTransport({f"/v1/organizations/{ORG_ID}/proposals": _paged(rows, PAGE_SIZE)})
    pages = fetch_proposals(ApiClient(transport), ORG_ID, {})
    assert len(pages.rows) == PAGE_SIZE * MAX_PAGES and not pages.complete
    summary = pipeline_summary(pages.rows, complete=pages.complete)
    assert summary is not None and summary["complete"] is False


def test_a_failed_read_leaves_the_section_out() -> None:
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


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(web_app) as test_client:
        yield test_client
    for key in ("api_client", "lag_days_default", "coverage_facts", "coverage_data", "build_info"):
        try:
            delattr(web_app.state, key)
        except (AttributeError, KeyError):
            pass


def _install(proposals: Answer) -> FakeTransport:
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
    _install((200, {"data": ROWS, "page": {"has_more": False, "next_cursor": None}}))
    body = client.get("/organizations/cypress-creek").text
    section = body.split('<section class="org-pipeline" id="pipeline"', 1)[1].split(">", 1)[1]
    section = section.split("</section>", 1)[0]
    # Near the top: before the assets, the nearby proposals and the proposal list.
    assert body.index('id="pipeline"') < body.index("Assets</h2>") < body.index("Proposals</h2>")
    assert _text(section).startswith(
        "Pipeline 8 proposals sponsored by this company, 6 of them in the active pipeline "
        "(announced, in process or contracted), 370.5 MW. Each count opens the matching list."
    )
    status = section.split('id="pipeline-by-status"', 1)[1].split("</table>", 1)[0]
    in_process = re.search(
        r"In process</th>\s*<td class=\"num tnum\">(.*?)</td>\s*<td class=\"num tnum\">(.*?)</td>",
        status,
        re.S,
    )
    assert in_process is not None
    assert (
        'href="/proposals?sponsor_id=org_01CYPRESS&amp;lifecycle_state=filed%2Cstudied%2Cpermitted%2Cunder_construction"'
        in in_process.group(1)
    )
    assert _text(in_process.group(1)) == "4 in process proposals" and in_process.group(2) == "270.5"
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
    sources = _text(section.split('class="org-pipeline__sources">', 1)[1].split("</p>", 1)[0])
    assert sources.startswith(
        "From ERCOT (7 proposals, retrieved 1 Oct 2026); EIA-860M (1 proposal, retrieved 12 Sep 2026)."
    )
    assert 'href="#sources"' in section and 'id="sources"' in body  # the Sources section's heading


def test_a_capitalised_company_name_reads_in_a_readable_case(client: TestClient) -> None:
    _install((200, {"data": ROWS}))
    body = client.get("/organizations/cypress-creek").text
    assert "<h1>Cypress Creek Renewables LLC</h1>" in body
    assert "<title>Cypress Creek Renewables LLC — Infraque</title>" in body
    assert body.count('As filed: <span class="as-filed__name">CYPRESS CREEK RENEWABLES LLC</span>') == 1


def test_the_list_section_says_when_it_shows_only_the_latest_hundred(client: TestClient) -> None:
    rows = [_proposal(n, "filed", "solar", "ERCOT", 1.0) for n in range(157)]
    _install(_paged(rows, PAGE_SIZE))
    body = client.get("/organizations/cypress-creek").text
    shown = re.search(r'<p class="attribution-line" id="proposals-shown">(.*?)</p>', body, re.S)
    assert shown is not None
    assert _text(shown.group(1)) == "The 100 most recently changed of 157; all of them in the list."
    assert body.count('<a href="/proposals/p-') == 100
    assert "157" in _text(body.split('id="pipeline"', 1)[1].split("</p>", 1)[0])


def test_a_company_that_sponsors_nothing_has_no_pipeline_section(client: TestClient) -> None:
    _install((200, {"data": []}))
    body = client.get("/organizations/cypress-creek").text
    assert 'id="pipeline"' not in body
    assert "No proposals sponsored by this company." in body
