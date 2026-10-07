"""Credit lines under lists and search sections (audit 2026-10-07 UX-4; docs/31 §5.3, D-27).

Before: `/proposals`, `/opportunities`, `/organizations` and `/search?q=` rendered no attribution
line at all, while `/proposals` opens on about 30 NESO rows whose licence requires the statement
"Supported by National Energy SO Open Data" wherever they appear, and search rows carried no source.
These pin: each list names the registers behind the rows it prints, from the API's own
`licence_summary` (or, for assets, the rows' provenance); a mandated credit appears verbatim; a
register not behind any printed row is not named; search rows link their source as list rows do.
The fake transport is duplicated, per this repo's convention that no `web/test_*.py` imports another.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.viewmodels import page_credits

NESO_CREDIT = "Supported by National Energy SO Open Data"
NESO = {
    "source_id": "gb.neso.tec_register",
    "name": "NESO Transmission Entry Capacity (TEC) Register",
    "reuse_class": "attribution",
    "attribution_text": NESO_CREDIT,
}
TED = {
    "source_id": "eu.ted.api",
    "name": "EU Tenders Electronic Daily (TED) API v3",
    "reuse_class": "attribution",
    "attribution_text": "Source: Publications Office of the EU",
}


def _prov(src: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_id": src["source_id"],
        "source_name": src["name"],
        "source_url": "https://example.org/register",
        "retrieved_at": "2026-10-07T00:00:00Z",
        "licence_id": "x",
        "reuse_class": src["reuse_class"],
        "attribution_text": src["attribution_text"],
        "is_primary": True,
    }


PROPOSAL = {
    "public_id": "prop_1",
    "slug": "sizewell-c-abc123",
    "name_canonical": "Sizewell C",
    "kind": "generation",
    "technology": "nuclear",
    "lifecycle_state": "contracted",
    "capacity_mw": 3260.0,
    "jurisdiction": "GB",
    "location": None,
    "provenance": [_prov(NESO)],
}
OPPORTUNITY = {
    "public_id": "opp_1",
    "slug": "grid-tender-def456",
    "title": "Grid tender",
    "kind": "tender",
    "status": "open",
    "due_at": "2026-11-14T12:00:00Z",
    "provenance": [_prov(TED)],
}
ASSET = {
    "public_id": "ast_1",
    "slug": "sizewell-b",
    "name": "Sizewell B",
    "asset_type": "power_plant",
    "provenance": [
        _prov({"source_id": "us.eia.860m", "name": "EIA-860M", "reuse_class": "open", "attribution_text": ""})
    ],
}

ORG = {"public_id": "org_1", "slug": "edf-energy", "name_canonical": "EDF Energy", "type": "developer"}


def _envelope(rows: list[dict[str, Any]], sources: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows), "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": {
            "sources": [{**s, "record_count": 1} for s in sources],
            "attribution_line": "Sources: " + "; ".join(s["name"] for s in sources) + ".",
        },
    }


class FakeTransport:
    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: Any = None
    ) -> httpx.Response:
        answer = self.responses.get(url)
        if answer is None:
            return httpx.Response(404, json={"title": "not_found", "detail": url})
        status, body = answer(params or {}) if callable(answer) else answer
        return httpx.Response(status, json=body)

    def post(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, json={"title": "not_found"})

    def request(self, method: str, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, json={"title": "not_found"})

    def close(self) -> None:
        pass


def _proposals(params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    if params.get("limit") == 1:  # the lifecycle notice's counts
        return 200, _envelope([], [])
    return 200, _envelope([PROPOSAL], [NESO])


VOCAB = {
    "data": {
        "technology": [{"value": "nuclear"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "tender"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [],
    }
}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    fake = FakeTransport(
        {
            "/v1/health": (200, {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}),
            "/v1/meta/vocabularies": (200, VOCAB),
            "/v1/proposals": _proposals,
            "/v1/opportunities": (200, _envelope([OPPORTUNITY], [TED])),
            "/v1/assets": (200, {**_envelope([ASSET], []), "licence_summary": {"sources": []}}),
            "/v1/organizations": (200, _envelope([ORG], [])),
            "/v1/organizations/org_1": (200, {"data": {**ORG, "asset_counts": {}}}),
        }
    )
    web_app.state.api_client = ApiClient(fake)
    web_app.state.lag_days_default = None
    with TestClient(web_app) as c:
        yield c
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


def _credit_lines(html: str) -> list[str]:
    import re

    return re.findall(r'<p class="attribution-line attribution-line--list">(.*?)</p>', html, re.S)


def test_page_credits_reads_the_summary_then_the_rows_once_each() -> None:
    credits = page_credits(_envelope([PROPOSAL], [NESO]), [PROPOSAL, ASSET])
    assert [c["source_id"] for c in credits] == ["gb.neso.tec_register", "us.eia.860m"]
    assert credits[0]["credit"] == NESO_CREDIT
    assert page_credits({"licence_summary": []}, []) == []


def test_proposal_list_credits_neso_verbatim(client: TestClient) -> None:
    lines = _credit_lines(client.get("/proposals").text)
    assert len(lines) == 1
    assert NESO_CREDIT in lines[0]
    assert "/attribution" in lines[0]
    assert "Publications Office" not in lines[0]  # only the registers behind these rows


def test_opportunity_list_credits_its_registers(client: TestClient) -> None:
    lines = _credit_lines(client.get("/opportunities").text)
    assert len(lines) == 1 and "Source: Publications Office of the EU" in lines[0]


def test_company_index_says_where_names_come_from(client: TestClient) -> None:
    lines = _credit_lines(client.get("/organizations").text)
    assert len(lines) == 1 and "each company's page credits those registers" in lines[0].replace("&#39;", "'")


def test_search_sections_carry_credits_and_rows_link_their_source(client: TestClient) -> None:
    html = client.get("/search?q=sizewell").text
    lines = _credit_lines(html)
    assert any(NESO_CREDIT in line for line in lines)
    assert any("Publications Office of the EU" in line for line in lines)
    assert any("EIA-860M" in line for line in lines)  # from the asset rows' provenance
    assert 'href="https://example.org/register"' in html  # the row's own source link
    assert "<h2>Companies</h2>" in html
    assert "3,260 MW" in html  # one capacity format (UX-8)
    assert "due 14 Nov 2026" in html  # docs/31 §4 date format (UX-8)


# ---- audit 2026-10-07 UX-2: a deadline the source has not caught up with, and an empty column ----


def test_deadline_passed_only_for_open_notices_with_a_past_date() -> None:
    import datetime as dt

    from web.viewmodels import deadline_passed

    today = dt.date(2026, 10, 7)
    assert deadline_passed("open", "2026-09-14T17:00:00Z", today)
    assert not deadline_passed("open", "2026-10-07", today)  # due today is not yet past
    assert not deadline_passed("closed", "2026-09-14", today)
    assert not deadline_passed("awarded", "2026-09-14", today)
    assert deadline_passed("frozen", "2026-09-14", today)  # any state still awaiting responses
    assert deadline_passed("unknown", "2026-09-14", today)
    assert not deadline_passed("open", None, today)
    assert not deadline_passed("open", "not a date", today)


def test_opportunity_list_says_a_deadline_passed_and_drops_an_empty_issuer_column(client: TestClient) -> None:
    past = {**OPPORTUNITY, "due_at": "2020-09-14T12:00:00Z"}
    responses = web_app.state.api_client._transport.responses
    responses["/v1/opportunities"] = (200, _envelope([past], [TED]))
    html = client.get("/opportunities").text
    assert "Deadline passed 14 Sep 2020" in html
    assert "the source still lists it as open" in html
    assert '<th scope="col">Issuer</th>' not in html  # no row on the page names an issuer
    with_issuer = {**past, "issuer": {"name_canonical": "Arizona Public Service"}}
    web_app.state.api_client._transport.responses["/v1/opportunities"] = (
        200,
        _envelope([with_issuer], [TED]),
    )
    html = client.get("/opportunities").text
    assert '<th scope="col">Issuer</th>' in html and "Arizona Public Service" in html


# ---- audit 2026-10-07 UX-11: one Sources row per register ----


def test_identical_source_rows_collapse_into_one_counted_row() -> None:
    from web.viewmodels import group_source_rows

    unit = {
        "source_id": "us.eia.860m",
        "source_url": "https://eia.gov/860m",
        "reuse_class": "open",
        "allows_raw": True,
    }
    rows = [
        {**unit, "record_id": f"69661-G{i}", "retrieved_at": f"2026-10-0{i}T00:00:00Z"} for i in range(1, 9)
    ]
    rows.append(
        {**unit, "source_id": "us.iso.caiso.gen_queue", "record_id": "1949", "retrieved_at": "2026-10-07"}
    )
    grouped = group_source_rows(rows)
    assert [g["source_id"] for g in grouped] == ["us.eia.860m", "us.iso.caiso.gen_queue"]
    assert grouped[0]["count"] == 8 and grouped[0]["record_ids"][:2] == ["69661-G1", "69661-G2"]
    assert grouped[0]["retrieved_at"] == "2026-10-08T00:00:00Z"  # the latest retrieval
    assert grouped[1]["count"] == 1
    css = (Path(__file__).resolve().parent / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert ".provenance-panel__name::before" in css and "summary::before" not in css


def test_a_grants_gov_agency_code_stands_in_for_an_unlinked_issuer(client: TestClient) -> None:
    coded = {**OPPORTUNITY, "identifiers": {"agency_code": "DOE-GFO"}}
    web_app.state.api_client._transport.responses["/v1/opportunities"] = (200, _envelope([coded], [TED]))
    html = client.get("/opportunities").text
    assert '<th scope="col">Issuer</th>' in html and "DOE-GFO" in html and "(agency code)" in html


# ---- lane L11: a large-load list neither sorts by nor prints a capacity no record carries ----


def test_a_load_list_sorts_by_name_and_has_no_capacity_column(client: TestClient) -> None:
    load = {**PROPOSAL, "kind": "load", "technology": "load", "capacity_mw": None}
    calls: list[dict[str, Any]] = []

    def proposals(params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        calls.append(dict(params))
        return 200, (_envelope([], []) if params.get("limit") == 1 else _envelope([load], [NESO]))

    web_app.state.api_client._transport.responses["/v1/proposals"] = proposals
    html = client.get("/proposals?kind=load").text
    assert calls[0]["sort"] == "name_canonical"
    assert "Capacity (MW)</th>" not in html and "sorted by name, A to Z by default" in html
    calls.clear()
    html = client.get("/proposals?kind=generation").text
    assert calls[0]["sort"] == "-capacity_mw" and "Capacity (MW)</th>" in html
    js = (Path(__file__).resolve().parent / "static" / "js" / "map.js").read_text(encoding="utf-8")
    assert 'p.technology === "load" && !p.capacity_mw ? ""' in js  # the drawer drops the row too
