"""Lane list-sort (2026-10-22): click-to-sort headings on the record lists (`web/list_sort.py`,
`_macros.html::sort_th`), compact two-line rows below 720px (docs/16 U-10, docs/17 M-8), and the RFPs
& funding page's energy-relevant default with an "All notices" switch (docs/16 U-7) and a Kind
filter that offers only kinds with notices.

The fake transport records every call, so a test can say what the page asked the API for. It is
duplicated, per this repo's convention that no `web/test_*.py` imports another.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import QueryParams

from web.api_client import ApiClient
from web.app import app as web_app
from web.list_sort import (
    ASSET_COLUMNS,
    OPPORTUNITY_COLUMNS,
    POINT_COLUMNS,
    PROPOSAL_COLUMNS,
    PROPOSAL_OTHER,
    sort_caption,
    sort_headers,
    valid_sort,
)

PROV = {
    "source_id": "us.iso.ercot.gen_queue",
    "source_name": "ERCOT GIS report",
    "source_url": "https://example.org/ercot",
    "retrieved_at": "2026-10-20T00:00:00Z",
    "reuse_class": "open",
    "attribution_text": "",
    "is_primary": True,
}
PROPOSAL = {
    "public_id": "prop_1",
    "slug": "brazos-solar-abc123",
    "name_canonical": "Brazos Solar",
    "kind": "generation",
    "technology": "solar_pv",
    "lifecycle_state": "filed",
    "capacity_mw": 250.0,
    "jurisdiction": "US-TX",
    "proposed_online_date": "2027-06-01",
    "location": None,
    "provenance": [PROV],
}
OPPORTUNITY = {
    "public_id": "opp_1",
    "slug": "solar-rfp-def456",
    "title": "Solar RFP",
    "kind": "tender",
    "status": "open",
    "due_at": "2026-11-14T12:00:00Z",
    "provenance": [PROV],
}
ASSET = {
    "public_id": "ast_1",
    "slug": "sizewell-b",
    "name": "Sizewell B",
    "asset_type": "power_plant",
    "capacity_mw": 1198.0,
    "provenance": [PROV],
}
POINT = {
    "public_id": "poi_1",
    "name": "59903 Bearkat 345kV",
    "iso": "ERCOT",
    "kind": "substation",
    "voltage_kv": 345,
    "jurisdiction": "US-TX",
    "totals": {
        "active_mw": 300.0,
        "active_count": 2,
        "withdrawn_count": 1,
        "built_count": 1,
        "by_technology": [],
    },
    "provenance": [PROV],
}
VOCAB = {
    "data": {
        "technology": [{"value": "solar_pv"}],
        "proposal_kind": [{"value": "generation"}],
        "opportunity_kind": [{"value": "rfp"}, {"value": "tender"}, {"value": "foa"}],
        "opportunity_status": [{"value": "open"}],
        "slip_bucket": [],
    }
}


def _envelope(rows: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {
        "data": rows,
        "meta": {"total": len(rows) if total is None else total, "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": {"sources": [], "attribution_line": ""},
    }


class FakeTransport:
    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: Any = None
    ) -> httpx.Response:
        self.calls.append((url, dict(params or {})))
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

    def params(self, url: str) -> list[dict[str, Any]]:
        return [p for called, p in self.calls if called == url]


#: The kinds the fake store has a notice of, and the "energy" and "all" totals the switch counts.
KINDS_PRESENT = {"tender"}
TOTALS = {"energy": 7, "all": 1349}


def _proposals(params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    if params.get("limit") == 1:  # the lifecycle notice's counts
        return 200, _envelope([])
    return 200, _envelope([PROPOSAL], total=6009)


def _opportunities(params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    if params.get("limit") == 1 and "include" not in params:  # the Kind filter's probes
        return 200, _envelope([OPPORTUNITY] if params.get("kind") in KINDS_PRESENT else [])
    scope = "energy" if params.get("relevance") == "energy" else "all"
    if params.get("limit") == 1:  # the switch's count of the other scope
        return 200, _envelope([], total=TOTALS[scope])
    return 200, _envelope([OPPORTUNITY], total=TOTALS[scope])


@pytest.fixture()
def fake() -> FakeTransport:
    return FakeTransport(
        {
            "/v1/health": (200, {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}),
            "/v1/meta/vocabularies": (200, VOCAB),
            "/v1/proposals": _proposals,
            "/v1/opportunities": _opportunities,
            "/v1/assets": (200, _envelope([ASSET])),
            "/v1/interconnection-points": (200, _envelope([POINT])),
        }
    )


@pytest.fixture()
def client(fake: FakeTransport) -> Iterator[TestClient]:
    web_app.state.api_client = ApiClient(fake)
    web_app.state.lag_days_default = None
    with TestClient(web_app) as c:
        yield c
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


def _th(html: str, label: str) -> str:
    """The `<th>` whose visible label is `label`."""
    for th in re.findall(r"<th scope=\"col\"[^>]*>.*?</th>", html, re.S):
        visible = re.sub(r'<span class="(visually-hidden|sort-link__short)"[^>]*>.*?</span>', "", th)
        if re.sub(r"<[^>]+>", "", visible).strip() == label:
            return th
    raise AssertionError(f"no heading {label!r}")


def _href(th: str) -> str:
    match = re.search(r'<a class="sort-link" href="([^"]+)"', th)
    assert match, th
    return unescape(match.group(1))


def _query(href: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(href).query)


def _main_call(fake: FakeTransport, url: str) -> dict[str, Any]:
    """The page's own list call (not a one-row probe or count)."""
    calls = [p for p in fake.params(url) if p.get("limit") != 1]
    assert calls, fake.calls
    return calls[0]


# ------------------------------------------------------------------------------- web/list_sort.py
def test_valid_sort_keeps_an_offered_field_and_falls_back_otherwise() -> None:
    assert valid_sort("-proposed_online_date", PROPOSAL_COLUMNS, "-capacity_mw") == "-proposed_online_date"
    assert valid_sort("name_canonical,-capacity_mw", PROPOSAL_COLUMNS, "-capacity_mw") == "name_canonical"
    # A field the API sorts by that no heading offers still sorts a linked view.
    assert valid_sort("-last_changed", PROPOSAL_COLUMNS, "-capacity_mw", PROPOSAL_OTHER) == "-last_changed"
    for raw in (None, "", "-commercial_operation_date", "capacity"):
        assert valid_sort(raw, PROPOSAL_COLUMNS, "-capacity_mw") == "-capacity_mw"


def test_a_first_click_sorts_the_obvious_way_and_a_second_reverses() -> None:
    qp = QueryParams("")
    # Nothing sorted by these yet: numbers and dates largest or newest first, names A to Z,
    # a deadline soonest first.
    first = sort_headers("/x", qp, {**PROPOSAL_COLUMNS, **OPPORTUNITY_COLUMNS}, "-last_changed")
    assert _query(first["capacity"]["href"]) == [("sort", "-capacity_mw")]
    assert _query(first["online"]["href"]) == [("sort", "-proposed_online_date")]
    assert _query(first["name"]["href"]) == [("sort", "name_canonical")]
    assert _query(first["due"]["href"]) == [("sort", "due_at")]
    assert all(h["direction"] is None and h["aria_sort"] is None for h in first.values())
    # The column that sorts the view shows its order, and its link is the reverse.
    current = sort_headers("/x", qp, PROPOSAL_COLUMNS, "-capacity_mw")
    assert current["capacity"]["direction"] == "desc" and current["capacity"]["aria_sort"] == "descending"
    assert _query(current["capacity"]["href"]) == [("sort", "capacity_mw")]
    assert current["capacity"]["hint"] == "Sort by capacity, smallest first"
    reverse = sort_headers("/x", qp, PROPOSAL_COLUMNS, "capacity_mw")
    assert reverse["capacity"]["aria_sort"] == "ascending"
    assert _query(reverse["capacity"]["href"]) == [("sort", "-capacity_mw")]


def test_a_heading_link_keeps_the_filters_and_drops_the_page_and_the_old_order() -> None:
    qp = QueryParams("technology=solar_pv&jurisdiction=US-TX&cursor=abc&sort=-capacity_mw&q=")
    href = sort_headers("/proposals", qp, PROPOSAL_COLUMNS, "-capacity_mw")["name"]["href"]
    assert urlsplit(href).path == "/proposals"
    assert _query(href) == [("technology", "solar_pv"), ("jurisdiction", "US-TX"), ("sort", "name_canonical")]


def test_the_caption_says_the_order_in_words() -> None:
    assert sort_caption("-capacity_mw", PROPOSAL_COLUMNS) == "sorted by capacity, largest first"
    assert sort_caption("name", ASSET_COLUMNS) == "sorted by name, A to Z"
    assert (
        sort_caption("-proposed_online_date", PROPOSAL_COLUMNS)
        == "sorted by proposed online date, latest first"
    )
    assert sort_caption("due_at", OPPORTUNITY_COLUMNS) == "sorted by deadline, soonest first"
    assert sort_caption("-withdrawn_count", POINT_COLUMNS) == "sorted by withdrawn projects, largest first"


# ------------------------------------------------------------------------------------ /proposals
def test_a_projects_heading_sorts_through_the_api_and_keeps_the_filters(
    client: TestClient, fake: FakeTransport
) -> None:
    html = client.get("/proposals?technology=solar_pv&sort=-proposed_online_date&cursor=abc").text
    assert _main_call(fake, "/v1/proposals")["sort"] == "-proposed_online_date"
    online = _th(html, "Proposed online")
    assert 'aria-sort="descending"' in online and "is-sorted" in online
    assert _query(_href(online)) == [("technology", "solar_pv"), ("sort", "proposed_online_date")]
    assert _query(_href(_th(html, "Capacity (MW)"))) == [("technology", "solar_pv"), ("sort", "-capacity_mw")]
    assert _query(_href(_th(html, "Name"))) == [("technology", "solar_pv"), ("sort", "name_canonical")]
    # The filter form carries the chosen order, so applying a filter keeps it.
    assert '<input type="hidden" name="sort" value="-proposed_online_date">' in html
    assert "sorted by proposed online date, latest first." in html
    # Status, Technology, Location, Company and Source do not sort: plain headings, no link.
    for label in ("Status", "Technology", "Location", "Company", "Source"):
        assert "sort-link" not in _th(html, label)


def test_an_unknown_sort_falls_back_to_the_default_and_is_not_carried(
    client: TestClient, fake: FakeTransport
) -> None:
    html = client.get("/proposals?sort=-commercial_operation_date").text
    assert _main_call(fake, "/v1/proposals")["sort"] == "-capacity_mw"
    assert 'aria-sort="descending"' in _th(html, "Capacity (MW)")
    assert 'name="sort"' not in html.split('<form id="filters"')[1].split("</form>")[0]


def test_the_count_line_groups_thousands(client: TestClient) -> None:
    assert "6,009 proposals match these filters." in client.get("/proposals").text


# ------------------------------------------------------------------------- compact rows (U-10)
@pytest.mark.parametrize("path", ["/proposals", "/opportunities", "/assets", "/interconnection-points"])
def test_every_record_list_renders_the_compact_two_line_row(client: TestClient, path: str) -> None:
    html = client.get(path).text
    table = html[html.index('<table class="record-table record-table--compact">') :].split("</table>")[0]
    row = table.split("<tbody>")[1].split("</tr>")[0]
    # Line one is the title (after the status chip where the list has one); line two the rest.
    assert len(re.findall(r'class="[^"]*\bcell-title\b', row)) == 1
    assert len(re.findall(r'class="[^"]*\bcell-meta\b', row)) >= 3


def test_a_long_notice_title_is_cut_at_a_word_on_the_phone_row_and_whole_in_the_table(
    client: TestClient, fake: FakeTransport
) -> None:
    long_title = (
        "Poland – Construction work for power plant – Wykonanie robót budowlanych w formule zaprojektuj "
        "i wybuduj: instalacji fotowoltaicznej pn. PV Skalin 1 o mocy zainstalowanej do 5,61MW"
    )
    rows = [{**OPPORTUNITY, "title": long_title}, {**OPPORTUNITY, "public_id": "opp_2", "slug": "short-x"}]
    fake.responses["/v1/opportunities"] = lambda params: (200, _envelope(rows))
    html = client.get("/opportunities").text
    cells = re.findall(r'<td data-label="Title" class="cell-title">(.*?)</td>', html, re.S)
    full = re.search(r'<span class="cell-text-full">(.*?)</span>', cells[0])
    short = re.search(r'<span class="cell-text-short">(.*?)</span>', cells[0])
    assert full and short and unescape(full.group(1)) == long_title
    cut = unescape(short.group(1))
    assert cut.endswith("…") and len(cut) <= 100 and long_title.startswith(cut[:-1])
    assert long_title[len(cut) - 1] == " "  # cut at a word, not inside one
    # A title that fits is printed once, as it was.
    assert "cell-text" not in cells[1] and ">Solar RFP</a>" in cells[1]


def test_the_online_date_joins_the_phone_line_only_when_it_sorts_the_view(client: TestClient) -> None:
    def online_cell(html: str) -> str:
        match = re.search(r'<td data-label="Proposed online" class="([^"]*)"', html)
        assert match, html
        return match.group(1)

    assert "cell-opt" in online_cell(client.get("/proposals").text).split()
    assert "cell-opt" not in online_cell(client.get("/proposals?sort=proposed_online_date").text).split()


# ------------------------------------------------------------------------- /assets and points
def test_the_asset_headings_replace_the_sort_select(client: TestClient, fake: FakeTransport) -> None:
    html = client.get("/assets?state=US-TX&sort=name").text
    assert fake.params("/v1/assets")[0]["sort"] == "name"
    assert 'id="f-sort"' not in html
    name = _th(html, "Name")
    assert 'aria-sort="ascending"' in name and _query(_href(name)) == [("state", "US-TX"), ("sort", "-name")]
    assert _query(_href(_th(html, "Capacity"))) == [("state", "US-TX"), ("sort", "-capacity_mw")]
    assert "sort-link" not in _th(html, "Operator")  # a withheld name must not order the rows


def test_the_grid_point_count_headings_sort(client: TestClient, fake: FakeTransport) -> None:
    html = client.get("/interconnection-points?iso=ERCOT&sort=-withdrawn_count").text
    assert fake.params("/v1/interconnection-points")[0]["sort"] == "-withdrawn_count"
    assert 'id="f-poi-sort"' not in html
    assert 'aria-sort="descending"' in _th(html, "Withdrawn")
    for label, token in (
        ("Active MW", "-active_mw"),
        ("Active projects", "-active_count"),
        ("Built", "-built_count"),
    ):
        assert _query(_href(_th(html, label))) == [("iso", "ERCOT"), ("sort", token)]
    # The order is part of the page's canonical URL, as on /proposals and /assets (a cursor needs it).
    assert (
        '<link rel="canonical" href="http://testserver/interconnection-points?iso=ERCOT&amp;sort=-withdrawn_count">'
        in html
    )


def test_a_malformed_point_filter_shows_the_index_in_the_order_the_headings_say(
    client: TestClient, fake: FakeTransport
) -> None:
    def refuse_a_bad_number(params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if params.get("min_active_mw") == "lots":
            return 400, {"title": "validation_error", "code": "validation_error"}
        return 200, _envelope([POINT])

    fake.responses["/v1/interconnection-points"] = refuse_a_bad_number
    html = client.get("/interconnection-points?min_active_mw=lots&sort=name").text
    refused, fallback = fake.params("/v1/interconnection-points")
    assert refused["sort"] == fallback["sort"] == "name" and "min_active_mw" not in fallback
    assert 'aria-sort="ascending"' in _th(html, "Point") and "59903 Bearkat 345kV" in html


# ------------------------------------------------------------------------ /opportunities (U-7)
def test_rfps_open_on_energy_notices_with_an_all_notices_switch(
    client: TestClient, fake: FakeTransport
) -> None:
    html = client.get("/opportunities?jurisdiction=US-AZ").text
    assert _main_call(fake, "/v1/opportunities")["relevance"] == "energy"
    switch = html.split('<nav class="view-switch" aria-label="Which notices to show">')[1].split("</nav>")[0]
    links = dict(re.findall(r'href="([^"]+)"[^>]*>(Energy-related|All notices)', switch))
    assert {v: unescape(k) for k, v in links.items()} == {
        "Energy-related": "/opportunities?jurisdiction=US-AZ",
        "All notices": "/opportunities?jurisdiction=US-AZ&relevance=all",
    }
    assert 'aria-current="page">Energy-related <span class="scope-switch__n tnum">7</span>' in switch
    assert '>All notices <span class="scope-switch__n tnum">1,349</span>' in switch
    assert '<input type="hidden" name="relevance"' not in html


def test_all_notices_is_kept_in_the_url_and_the_filter_form(client: TestClient, fake: FakeTransport) -> None:
    html = client.get("/opportunities?relevance=all&sort=-due_at").text
    main = _main_call(fake, "/v1/opportunities")
    assert "relevance" not in main and main["sort"] == "-due_at"
    assert '<input type="hidden" name="relevance" value="all">' in html
    assert '<input type="hidden" name="sort" value="-due_at">' in html
    # The deadline heading keeps the scope; the scope switch keeps the order.
    assert _query(_href(_th(html, "Due"))) == [("relevance", "all"), ("sort", "due_at")]
    assert 'href="/opportunities?sort=-due_at">Energy-related' in html
    # The all-notices view is its own canonical page; an explicit energy scope is the default page.
    assert 'rel="canonical" href="http://testserver/opportunities?relevance=all&amp;sort=-due_at"' in html
    explicit = client.get("/opportunities?relevance=energy").text
    assert 'rel="canonical" href="http://testserver/opportunities"' in explicit


def _kind_options(html: str) -> list[str]:
    select = html.split('<select id="f-kind" name="kind">')[1].split("</select>")[0]
    return re.findall(r'<option value="([^"]*)"', select)


def _kind_probes(fake: FakeTransport) -> list[dict[str, Any]]:
    return [p for p in fake.params("/v1/opportunities") if p.get("limit") == 1 and "include" not in p]


def test_the_kind_filter_offers_only_kinds_with_notices(client: TestClient, fake: FakeTransport) -> None:
    assert _kind_options(client.get("/opportunities").text) == ["", "tender"]
    # One one-row probe per kind, in the page's own scope (energy, open), the kind replaced.
    probes = _kind_probes(fake)
    assert sorted(p["kind"] for p in probes) == ["foa", "rfp", "tender"]
    assert all(p["relevance"] == "energy" and p["status"] == "open" for p in probes)
    # The kind in force stays offered, so the reader can see what is selected.
    assert _kind_options(client.get("/opportunities?kind=rfp").text) == ["", "rfp", "tender"]
    # An htmx filter change swaps only the rows, so it asks nothing for a select it does not draw.
    fake.calls.clear()
    client.get("/opportunities?kind=tender", headers={"HX-Request": "true"})
    assert _kind_probes(fake) == []


def test_the_kind_filter_offers_every_kind_when_the_api_cannot_say(
    client: TestClient, fake: FakeTransport
) -> None:
    def failing_probes(params: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if params.get("limit") == 1 and "include" not in params:
            return 503, {"title": "unavailable"}
        return _opportunities(params)

    fake.responses["/v1/opportunities"] = failing_probes
    assert _kind_options(client.get("/opportunities").text) == ["", "rfp", "tender", "foa"]
    # The probes were made (and failed): the fallback is why every kind is offered.
    assert len(_kind_probes(fake)) == 3
