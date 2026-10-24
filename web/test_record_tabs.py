"""Record pages as URL tabs (Navigation v2, docs/30 §11.5, §11.10; lane V3): a project, a plant, pipeline
or line, and a grid connection point.

Pinned here, per the docs/30 §11.11 test table: every tab answers 200 with the shared header, one primary
action and its own credits line (a Sources tab's is its full panel, T-4, so it passes no separate line);
absent tabs are not linked and answer 404 with the header and one line;
`?tab=` answers 301 to the segment; a merged project's old slug answers 301 to the survivor on the same
tab. Also: only the Overview is indexable and carries JSON-LD (T-7), the sitemap lists Overviews and never
a tab, every status chip and technical term in a header links to `/glossary` (owner, 2026-10-23), the old
one-page anchors still land on something that links to their tab (T-8), and Track is the primary action
where it is offered.

Same pattern as `web/test_record_company.py`: `web.app.app` against a hand-written fake `Transport`, no
database; the fake is duplicated rather than imported (no `web/test_*.py` imports another).
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
from starlette.requests import Request

from web import record_tabs
from web.api_client import ApiClient
from web.app import app as web_app
from web.app import delayed_notice
from web.labels import LIFECYCLE_LABELS
from web.viewmodels import proposal_history

WEB = Path(__file__).resolve().parent
HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
}
PROV: dict[str, Any] = {
    "source_id": "us.ercot.gis_report",
    "source_name": "ERCOT GIS Report",
    "source_url": "https://www.ercot.com/gis#24INR0001",
    "retrieved_at": "2026-10-20T00:00:00Z",
    "reuse_class": "open",
    "attribution_text": None,
    "source_record_id": "24INR0001",
}
EIA: dict[str, Any] = {
    "source_id": "us.eia.860m",
    "source_name": "EIA-860M",
    "source_url": "https://www.eia.gov/electricity/data/eia860m/",
    "retrieved_at": "2026-10-09T00:00:00Z",
    "reuse_class": "open",
    "attribution_text": "EIA-860M",
    "source_record_id": "3456",
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
def client() -> Iterator[TestClient]:
    with TestClient(web_app) as c:
        yield c
    for key in ("api_client", "lag_days_default", "coverage_facts", "build_info", "sitemap_cache"):
        web_app.state.__dict__.pop(key, None)


def _install(responses: Mapping[str, tuple[int, Any]]) -> FakeTransport:
    transport = FakeTransport(
        {
            "/v1/health": (200, HEALTH),
            "/v1/sources": (200, {"data": []}),
            "/v1/sources/us.ercot.gis_report": (200, {"data": {"licence": {"quote_text": "Public."}}}),
            "/v1/sources/us.eia.860m": (200, {"data": {"licence": {"quote_text": "Public domain."}}}),
            **responses,
        }
    )
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    return transport


def _list(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": rows,
        "page": {"has_more": False, "next_cursor": None, "prev_cursor": None},
        "meta": {"total": len(rows), "total_is_estimate": False},
    }


# ------------------------------------------------------------------------------------------ fixtures
def _proposal(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "public_id": "prop_B1",
        "slug": "bearkat-solar-b1",
        "name_canonical": "Bearkat Solar",
        "kind": "generation",
        "technology": "solar",
        "lifecycle_state": "studied",
        "status_raw": "Facility Study",
        "capacity_mw": 250.0,
        "jurisdiction": "US-TX",
        "iso": "ERCOT",
        "proposed_online_date": "2027-06-30",
        "identifiers": {"queue_ids": [{"id": "24INR0001"}]},
        "location": {
            "geom": {"type": "Point", "coordinates": [-101.38, 32.29]},
            "state_code": "US-TX",
            "county_name": "Howard",
            "precision": "exact",
        },
        "provenance": [PROV],
    }
    row.update(overrides)
    return row


EVENTS = [
    {
        "event_type": "status_change",
        "observed_at": "2026-10-11T00:00:00Z",
        "changed_keys": ["lifecycle_state"],
        "before": {"lifecycle_state": "filed"},
        "after": {"lifecycle_state": "studied"},
        "provenance": PROV,
    }
]


def _proposal_routes(row: dict[str, Any], events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    pid = row["public_id"]
    return {
        "/v1/proposals": (200, _list([row])),
        f"/v1/proposals/{pid}": (200, {"data": row}),
        f"/v1/proposals/{pid}/events": (200, _list(EVENTS if events is None else events)),
    }


def _plant(**overrides: Any) -> dict[str, Any]:
    org = {"public_id": "org_LS", "slug": "lone-star-generation", "name_canonical": "Lone Star Generation"}
    other = {"public_id": "org_PP", "slug": "permian-power", "name_canonical": "Permian Power"}
    entity: dict[str, Any] = {
        "public_id": "asset_BS",
        "slug": "big-spring-us-tx",
        "name": "Big Spring Power Station",
        "asset_type": "power_plant",
        "status": "retiring",
        "technology": "natural_gas_steam",
        "capacity_mw": 480.0,
        "commissioned_year": 1972,
        "state_code": "US-TX",
        "county_name": "Howard",
        "geometry": {"type": "Point", "coordinates": [-101.41, 32.28]},
        "attributes": {
            "retirement": {
                "status_rule": "majority_mw_retiring",
                "retiring_mw": 320.0,
                "next_planned": "2028-12",
            },
            "grid": {"grid_voltage_kv": [345.0], "transmission_owner": "Oncor Electric Delivery"},
        },
        "owners": [
            {"organization": org, "role": "owner", "share_pct": 60.0, "as_of": "2025-12-31"},
            {"organization": other, "role": "owner", "share_pct": 40.0, "as_of": "2025-12-31"},
            {"organization": org, "role": "operator", "share_pct": None},
        ],
        "provenance": [EIA],
    }
    entity.update(overrides)
    return entity


NEARBY = {
    "public_id": "prop_N1",
    "slug": "nearby-solar",
    "name_canonical": "Nearby Solar",
    "lifecycle_state": "filed",
    "technology": "solar",
    "capacity_mw": 100.0,
    "distance_km": 3.2,
    "provenance": [PROV],
}


def _asset_routes(entity: dict[str, Any], nearby: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    aid = entity["public_id"]
    return {
        "/v1/assets": (200, _list([entity])),
        f"/v1/assets/{aid}": (200, {"data": entity}),
        f"/v1/assets/{aid}/nearby-proposals": (200, {"data": [NEARBY] if nearby is None else nearby}),
        f"/v1/assets/{aid}/nearby-grid": (
            200,
            {
                "data": {"radius_km": 25.0, "transmission_lines": [], "interconnection_points": []},
                "licence_summary": {"sources": []},
            },
        ),
    }


def _point(changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "data": {
            "public_id": "poi_1",
            "name": "59903 Bearkat 345kV",
            "iso": "ERCOT",
            "kind": "substation",
            "voltage_kv": 345.0,
            "jurisdiction": "US-TX",
            "totals": {
                "active_mw": 250.0,
                "active_count": 1,
                "withdrawn_mw": 0,
                "withdrawn_count": 0,
                "built_mw": 0,
                "built_count": 0,
                "proposal_count": 1,
                "by_technology": [{"technology": "solar", "active_mw": 250.0, "active_count": 1, "count": 1}],
            },
            "proposals": [
                {
                    "slug": "bearkat-solar-b1",
                    "name_canonical": "Bearkat Solar",
                    "lifecycle_state": "studied",
                    "technology": "solar",
                    "capacity_mw": 250.0,
                    "jurisdiction": "US-TX",
                }
            ],
            "recent_changes": changes or [],
            "provenance": [PROV],
        }
    }


CHANGE = {
    "id": "evt_1",
    "event_type": "status_change",
    "observed_at": "2026-10-11T00:00:00Z",
    "before": {"lifecycle_state": "filed"},
    "after": {"lifecycle_state": "studied"},
    "subject": {"name": "Bearkat Solar", "url": "https://inframation.io/proposals/bearkat-solar-b1"},
    "provenance": PROV,
}

TABS: dict[str, tuple[str, dict[str, Any], tuple[str, ...]]] = {
    "proposal": ("/proposals/bearkat-solar-b1", _proposal_routes(_proposal()), ("timeline", "sources")),
    "asset": (
        "/assets/big-spring-us-tx",
        _asset_routes(_plant()),
        ("retirement", "grid", "ownership", "nearby", "sources"),
    ),
    "point": (
        "/interconnection-points/poi_1",
        {"/v1/interconnection-points/poi_1": (200, _point([CHANGE]))},
        ("projects", "changes", "sources"),
    ),
}


def _header(body: str) -> str:
    start = body.index('<header class="record-header">')
    return body[start : body.index("</header>", start)]


def _robots(body: str) -> str:
    found = re.search(r'<meta name="robots" content="([^"]+)">', body)
    assert found is not None
    return found.group(1)


def _canonical(body: str) -> str:
    found = re.search(r'<link rel="canonical" href="([^"]+)">', body)
    assert found is not None
    return found.group(1)


# ------------------------------------------------------------------------------- every tab, every type
@pytest.mark.parametrize("kind", sorted(TABS))
def test_every_tab_answers_with_the_shared_header_one_action_and_its_own_credits(
    client: TestClient, kind: str
) -> None:
    base, routes, tabs = TABS[kind]
    _install(routes)
    headers = []
    for tab in ("overview", *tabs):
        path = base if tab == "overview" else f"{base}/{tab}"
        resp = client.get(path)
        assert resp.status_code == 200, (path, resp.text[:300])
        body = resp.text
        header = _header(body)
        headers.append(header)
        # T-1: breadcrumb from Market, one H1, the tier notice, then the tab bar with this tab current.
        assert '<nav class="breadcrumbs" aria-label="Breadcrumb"><a href="/">Market</a> / ' in header
        assert header.count("<h1>") == 1 and "delayed-notice" in header
        assert body.count('class="record-action"') == 1, path  # one primary action
        bar = body[body.index('<nav class="record-tabs"') :].split("</nav>")[0]
        current = re.findall(r'<a href="([^"]+)" aria-current="page">', bar)
        assert current == [path], (path, current)
        # T-4: the tab's own credits line (the full panel on Sources) and the record's attribution line.
        foot = body[body.index('<div class="record-foot">') :]
        if tab == "sources":
            assert '<section class="provenance-panel"' in body and "View at source" in body
        else:
            assert "Sources of this " in foot and ("ERCOT GIS Report" in foot or "EIA-860M" in foot)
            assert f'href="{base}/sources"' in foot
        assert '<p class="attribution-line">' in foot and "Retrieved" in foot
    # The same header on every tab, apart from which crumb links back to the Overview.
    assert len({re.sub(r'<a href="[^"]+">([^<]+)</a></nav>', r"\1</nav>", h) for h in headers}) == 1


def test_the_proposal_tab_bar_lists_its_tabs_in_order_with_counts(client: TestClient) -> None:
    _install(_proposal_routes(_proposal()))
    body = client.get("/proposals/bearkat-solar-b1").text
    bar = body[body.index('<nav class="record-tabs" aria-label="Bearkat Solar sections">') :].split("</nav>")[
        0
    ]
    assert re.findall(r'<a href="([^"]+)"', bar) == [
        "/proposals/bearkat-solar-b1",
        "/proposals/bearkat-solar-b1/timeline",
        "/proposals/bearkat-solar-b1/sources",
    ]
    assert 'Sources <span class="record-tabs__count">1</span>' in bar
    assert 'role="tablist"' not in body  # T-9: links, not a widget


def test_the_asset_tab_bar_holds_all_six_tabs_for_a_retiring_plant_with_owners_and_neighbours(
    client: TestClient,
) -> None:
    _install(_asset_routes(_plant()))
    body = client.get("/assets/big-spring-us-tx").text
    bar = body[body.index('<nav class="record-tabs"') :].split("</nav>")[0]
    assert [t.split("/")[-1] for t in re.findall(r'<a href="([^"]+)"', bar)] == [
        "big-spring-us-tx",
        "retirement",
        "grid",
        "ownership",
        "nearby",
        "sources",
    ]
    assert 'Ownership <span class="record-tabs__count">3</span>' in bar
    assert 'Nearby <span class="record-tabs__count">1</span>' in bar


# ------------------------------------------------------------------------------------- absent tabs
@pytest.mark.parametrize(
    ("routes", "path", "line"),
    [
        (
            _proposal_routes(_proposal()),
            "/proposals/bearkat-solar-b1/matches",
            "No matches are published for this project.",
        ),
        (
            _asset_routes(_plant(status="operating", attributes={}), nearby=[]),
            "/assets/big-spring-us-tx/retirement",
            "No retired or scheduled generators are recorded for this asset.",
        ),
        (
            _asset_routes(_plant(status="operating", attributes={}), nearby=[]),
            "/assets/big-spring-us-tx/grid",
            "No grid connection is recorded for this asset.",
        ),
        (
            _asset_routes(_plant(owners=_plant()["owners"][:1]), nearby=[]),
            "/assets/big-spring-us-tx/ownership",
            "Every ownership record is on the line under the name.",
        ),
        (
            _asset_routes(_plant(), nearby=[]),
            "/assets/big-spring-us-tx/nearby",
            "No exact-grade projects are recorded within 25 km.",
        ),
        (
            {"/v1/interconnection-points/poi_1": (200, _point([]))},
            "/interconnection-points/poi_1/changes",
            "No change events are recorded for the projects here yet.",
        ),
    ],
)
def test_an_absent_tab_is_not_linked_and_answers_404_with_the_header_and_one_line(
    client: TestClient, routes: dict[str, Any], path: str, line: str
) -> None:
    _install(routes)
    base = path.rsplit("/", 1)[0]
    overview = client.get(base).text
    assert f'href="{path}"' not in overview  # T-3: not linked
    resp = client.get(path)
    assert resp.status_code == 404
    assert '<header class="record-header">' in resp.text and '<nav class="record-tabs"' in resp.text
    assert f'<p class="record-absent" role="status">{line}</p>' in resp.text
    assert _robots(resp.text) == "noindex, follow"


def test_a_record_with_one_tab_draws_no_tab_bar(client: TestClient) -> None:
    """T-3: a bar only with two or more tabs. An operating plant with one owner and nothing nearby still
    has its Sources tab, so the smallest bar has two; the rule is pinned on the tab set itself."""
    tabs = record_tabs.Tabs("asset", "/assets/x", record_tabs.ASSET_TABS, present={}, current="overview")
    assert [t["key"] for t in tabs.shown] == ["overview"] and not tabs.bar


def test_an_unknown_segment_is_the_plain_not_found_page(client: TestClient) -> None:
    _install(_proposal_routes(_proposal()))
    for path in (
        "/proposals/bearkat-solar-b1/pipeline",
        "/assets/big-spring-us-tx/foo",
        "/interconnection-points/poi_1/x",
    ):
        resp = client.get(path)
        assert resp.status_code == 404 and '<header class="record-header">' not in resp.text, path


def test_a_missing_record_on_a_tab_is_the_records_not_found(client: TestClient) -> None:
    _install({"/v1/proposals": (200, _list([]))})
    resp = client.get("/proposals/nothing-here/timeline")
    assert resp.status_code == 404 and "This proposal does not exist" in resp.text


# --------------------------------------------------------------------------------------- redirects
@pytest.mark.parametrize(
    ("path", "location"),
    [
        ("/proposals/bearkat-solar-b1?tab=timeline", "/proposals/bearkat-solar-b1/timeline"),
        (
            "/proposals/bearkat-solar-b1?tab=Sources&utm_source=mail",
            "/proposals/bearkat-solar-b1/sources?utm_source=mail",
        ),
        ("/proposals/bearkat-solar-b1?tab=overview", "/proposals/bearkat-solar-b1"),
        ("/proposals/bearkat-solar-b1?tab=nonsense&x=1", "/proposals/bearkat-solar-b1?x=1"),
        ("/proposals/bearkat-solar-b1/sources?tab=timeline", "/proposals/bearkat-solar-b1/timeline"),
        # An empty or unknown `tab=` keeps the tab the path asked for, not the Overview.
        ("/proposals/bearkat-solar-b1/sources?tab=", "/proposals/bearkat-solar-b1/sources"),
        ("/proposals/bearkat-solar-b1/timeline?tab=nonsense&x=1", "/proposals/bearkat-solar-b1/timeline?x=1"),
        ("/assets/big-spring-us-tx/nearby?tab=", "/assets/big-spring-us-tx/nearby"),
        ("/assets/big-spring-us-tx?tab=ownership", "/assets/big-spring-us-tx/ownership"),
        ("/interconnection-points/poi_1?tab=changes&sort=x", "/interconnection-points/poi_1/changes?sort=x"),
    ],
)
def test_a_tab_parameter_answers_301_to_the_segment_keeping_the_rest(
    client: TestClient, path: str, location: str
) -> None:
    _install({})
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 301 and resp.headers["location"] == location


@pytest.mark.parametrize("tab", ["overview", "timeline", "sources"])
def test_a_merged_projects_old_slug_answers_301_to_the_survivor_on_the_same_tab(
    client: TestClient, tab: str
) -> None:
    survivor = _proposal()
    # The slug filter finds no current record under the old slug; the API's detail route follows the
    # merge to the survivor (services/api/merged_redirect.py), which the transport hands back.
    _install({"/v1/proposals": (200, _list([])), "/v1/proposals/old-bearkat": (200, {"data": survivor})})
    path = "/proposals/old-bearkat" + ("" if tab == "overview" else f"/{tab}")
    resp = client.get(path + "?utm_source=mail", follow_redirects=False)
    target = "/proposals/bearkat-solar-b1" + ("" if tab == "overview" else f"/{tab}")
    assert resp.status_code == 301 and resp.headers["location"] == target + "?utm_source=mail"


# ------------------------------------------------------------------------------- search engines (T-7)
@pytest.mark.parametrize("kind", sorted(TABS))
def test_only_the_overview_is_indexed_and_carries_json_ld(client: TestClient, kind: str) -> None:
    base, routes, tabs = TABS[kind]
    _install(routes)
    overview = client.get(base).text
    assert _robots(overview) == "index, follow"
    assert _canonical(overview) == f"http://testserver{base}"
    graphs = [
        json.loads(g) for g in re.findall(r'<script type="application/ld\+json">(.*?)</script>', overview)
    ]
    crumbs = next(g for g in graphs if g["@type"] == "BreadcrumbList")
    names = [i["name"] for i in crumbs["itemListElement"]]
    assert names[0] == "Market" and crumbs["itemListElement"][-1]["item"] == f"http://testserver{base}"
    for tab in tabs:
        body = client.get(f"{base}/{tab}").text
        assert _robots(body) == "noindex, follow", tab
        assert _canonical(body) == f"http://testserver{base}/{tab}", tab
        assert "application/ld+json" not in body, tab


def test_the_sitemap_lists_overviews_and_the_new_pages_that_exist_never_a_tab(client: TestClient) -> None:
    _install(
        {
            "/v1/proposals": (200, _list([_proposal()])),
            "/v1/opportunities": (200, _list([])),
            "/v1/assets": (200, _list([_plant()])),
            "/v1/organizations": (200, _list([])),
            "/v1/interconnection-points": (200, _list([_point()["data"]])),
        }
    )
    body = client.get("/sitemap.xml").text
    locs = re.findall(r"<loc>http://testserver([^<]+)</loc>", body)
    assert {
        "/proposals/bearkat-solar-b1",
        "/assets/big-spring-us-tx",
        "/interconnection-points/poi_1",
    } <= set(locs)
    assert not [
        loc
        for loc in locs
        if re.search(r"/(timeline|sources|matches|retirement|grid|ownership|nearby|projects|changes)$", loc)
    ]
    # docs/30 §11.10: the new section pages are listed once their routes exist, never before.
    assert "/prices" in locs and "/funding" not in locs


# ------------------------------------------------------------------------------------ plain words
def test_the_status_chip_and_the_technical_terms_link_to_the_glossary(client: TestClient) -> None:
    _install(_proposal_routes(_proposal()))
    header = _header(client.get("/proposals/bearkat-solar-b1/timeline").text)
    assert '<a class="record-header__chip" href="/glossary#studied">' in header
    assert "what this status means" in header
    for key, label in (
        ("capacity", "Capacity"),
        ("interconnection-queue", "Interconnection queue"),
        ("queue-position", "Queue position"),
        ("in-service-date", "In-service date"),
    ):
        assert f'<dt><a href="/glossary#{key}">{label}</a></dt>' in header, key
    assert '<time datetime="2027-06-30">30 Jun 2027</time> (stated)' in header
    assert "24INR0001" in header


def test_a_large_load_says_so_in_plain_words_and_why_it_counts(client: TestClient) -> None:
    load = _proposal(
        kind="load",
        technology="load",
        identifiers={"select_basis": {"us.ercot.gis_report": "name"}},
        iso=None,
    )
    _install(_proposal_routes(load))
    body = client.get("/proposals/bearkat-solar-b1").text
    assert '<dd class="tnum"><a href="/glossary#large-load">Large load</a></dd>' in _header(body)
    assert "Selected as a data centre because the facility name says data centre." in body


def test_the_glossary_keys_are_the_lifecycle_states_and_the_named_terms() -> None:
    assert record_tabs.GLOSSARY_TERMS == {
        "interconnection-queue",
        "queue-position",
        "capacity",
        "in-service-date",
        "large-load",
        "rfp",
        "funding-call",
    }
    for state in LIFECYCLE_LABELS:
        assert record_tabs.glossary_href(state) == f"/glossary#{state}"
    assert record_tabs.glossary_href(None) is None and record_tabs.glossary_href("retiring") is None


# --------------------------------------------------------------------------------- Overview and Timeline
def test_the_overview_holds_the_first_rows_of_each_other_tab(client: TestClient) -> None:
    _install(_asset_routes(_plant()))
    body = client.get("/assets/big-spring-us-tx").text
    for anchor, tab in (
        ("retirement", "retirement"),
        ("grid", "grid"),
        ("owners", "ownership"),
        ("nearby", "nearby"),
    ):
        block = body[body.index(f'id="{anchor}"') :].split("</section>")[0]
        assert f'href="/assets/big-spring-us-tx/{tab}"' in block, anchor
    assert "Nearby Solar" in body and "Lone Star Generation" in body
    # The attributes sit behind a closed disclosure; the grid call is the Grid tab's alone (T-10).
    assert '<details class="record-disclosure record-attributes" id="attributes">' in body


def test_each_tab_reads_only_its_own_data(client: TestClient) -> None:
    transport = _install(_asset_routes(_plant()))
    client.get("/assets/big-spring-us-tx")
    assert not any(url.endswith("/nearby-grid") for _, url, _ in transport.calls)
    client.get("/assets/big-spring-us-tx/grid")
    assert any(url.endswith("/nearby-grid") for _, url, _ in transport.calls)
    transport = _install(_proposal_routes(_proposal()))
    client.get("/proposals/bearkat-solar-b1/sources")
    assert not any(url.endswith("/events") for _, url, _ in transport.calls)


def test_the_timeline_is_one_dated_list_newest_first_with_the_stated_date_marked(client: TestClient) -> None:
    merge = [
        {"merged_at": "2026-10-01", "members": [{"name_canonical": "Bearkat", "source_id": "us.eia.860m"}]}
    ]
    row = _proposal(merge_history=merge)
    _install(_proposal_routes(row))
    body = client.get("/proposals/bearkat-solar-b1/timeline").text
    timeline = body[body.index('<ul class="timeline">') :].split("</ul>")[0]
    dates = re.findall(r'<time datetime="([^"]+)">', timeline)
    assert dates == sorted(dates, reverse=True) and dates[0] == "2027-06-30"
    assert '<span class="timeline__mark">Stated</span> In-service date' in timeline
    assert "(not a commitment)" in timeline and "Observed as studied" in timeline
    # The record's own bookkeeping is collapsed apart (docs/16 §4.2).
    assert "<summary>Record maintenance (1)</summary>" in body and "Merged into this record" not in timeline


def test_the_merge_wording_is_the_prefix_the_timeline_folds() -> None:
    record = {
        "public_id": "prop_X",
        "merge_history": [{"merged_at": "2026-10-01", "members": [{"name_canonical": "A"}]}],
    }

    class _NoEvents:
        def get(self, *_args: Any, **_kwargs: Any) -> Any:
            raise KeyError("no events")

    items = proposal_history(_NoEvents(), record)  # type: ignore[arg-type]
    assert items and str(items[0]["text"]).startswith(record_tabs.MERGE_PREFIX)


# ------------------------------------------------------------------------------- old anchors (T-8)
def test_the_old_one_page_anchors_stay_on_the_overview_and_site_js_moves_them_to_their_tab(
    client: TestClient,
) -> None:
    script = (WEB / "static" / "js" / "site.js").read_text(encoding="utf-8")
    expected = {
        "/proposals": ("sources", "timeline", "history"),
        "/assets": ("retirement", "grid", "owners", "nearby", "sources"),
        "/interconnection-points": ("projects", "changes", "sources"),
    }
    for prefix, anchors in expected.items():
        for anchor in anchors:
            assert f'"{prefix}/{{record}}#{anchor}":' in script, (prefix, anchor)
    # Moved only to a tab the page links, so a record without that tab keeps the reader in place.
    assert "document.querySelector('a[href=\"' + movedTo.split(\"#\")[0] + '\"]')" in script
    pages = (
        (_proposal_routes(_proposal()), "/proposals/bearkat-solar-b1", ("sources", "timeline", "history")),
        (
            _asset_routes(_plant()),
            "/assets/big-spring-us-tx",
            ("retirement", "grid", "owners", "nearby", "sources"),
        ),
        (
            {"/v1/interconnection-points/poi_1": (200, _point([CHANGE]))},
            "/interconnection-points/poi_1",
            ("projects", "changes", "sources"),
        ),
    )
    for routes, path, anchors in pages:
        _install(routes)
        body = client.get(path).text
        for anchor in anchors:
            assert f'id="{anchor}"' in body, (path, anchor)


# ------------------------------------------------------------------------------------ the action
def test_track_is_the_primary_action_where_it_is_offered(client: TestClient) -> None:
    _install(_proposal_routes(_proposal()))
    client.cookies.set("session", "x")
    try:
        overview = client.get("/proposals/bearkat-solar-b1").text
        timeline = client.get("/proposals/bearkat-solar-b1/timeline").text
    finally:
        client.cookies.clear()
    if "track-block" not in overview:
        pytest.skip("Track is not offered in this configuration (MY_PIPELINE off or another cookie name)")
    assert '<a class="record-action" href="#track-heading">Track</a>' in overview
    assert '<a class="record-action" href="/proposals/bearkat-solar-b1#track-heading">Track</a>' in timeline
    assert "track-block" not in timeline  # the block itself is on the Overview, after the facts


def test_without_track_the_action_shows_the_record_on_the_map(client: TestClient) -> None:
    _install(_proposal_routes(_proposal()))
    action = re.search(
        r'<a class="record-action" href="([^"]+)">([^<]+)</a>', client.get("/proposals/bearkat-solar-b1").text
    )
    assert action is not None and action.group(2) == "Show on map"
    assert action.group(1).startswith("/?") and "focus=prop_B1" in action.group(1).replace("&amp;", "&")
    _install(_asset_routes(_plant()))
    action = re.search(
        r'<a class="record-action" href="([^"]+)">([^<]+)</a>', client.get("/assets/big-spring-us-tx").text
    )
    assert (
        action is not None
        and action.group(2) == "Show on map"
        and "center=-101.4100%2C32.2800" in action.group(1)
    )
    _install({"/v1/interconnection-points/poi_1": (200, _point())})
    action = re.search(
        r'<a class="record-action" href="([^"]+)">([^<]+)</a>',
        client.get("/interconnection-points/poi_1").text,
    )
    assert action is not None and action.group(2) == "Open in Projects"
    assert action.group(1) == "/proposals?interconnection_point_id=poi_1"


# ------------------------------------------------------------------------------------ shared parts
def test_the_tier_notice_outside_app_py_is_the_same_as_app_pys(client: TestClient) -> None:
    _install({})
    request = Request(
        {"type": "http", "app": web_app, "headers": [], "method": "GET", "path": "/", "query_string": b""}
    )
    ours = record_tabs.tier_notice(request)
    theirs = delayed_notice(request, "proposal")
    assert ours == theirs


def test_credits_name_each_register_once_with_its_licence_and_latest_date() -> None:
    older = {**PROV, "retrieved_at": "2026-10-01T00:00:00Z"}
    rows = record_tabs.credits([older, PROV], [EIA], [{"source_name": "EIA-860M"}])
    assert [c["name"] for c in rows] == ["ERCOT GIS Report", "EIA-860M"]  # one each, first seen first
    ercot = rows[0]
    assert ercot["licence"] == "Open licence" and ercot["retrieved_at"] == PROV["retrieved_at"]
    assert record_tabs.credits([{"source_name": "Somewhere"}])[0]["licence"] is None


# ------------------------------------------------------------- review fixes: a tab only with content
def _retired(**overrides: Any) -> dict[str, Any]:
    """A plant that retired before EIA-860's report year: a Retirement block and no grid fields."""
    entity = _plant(
        status="retired",
        attributes={
            "retirement": {"status_rule": "all_units_retired", "retired_mw": 380.0, "last_retired": "2019-03"}
        },
        owners=_plant()["owners"][:1],
    )
    entity.update(overrides)
    return entity


FERC: dict[str, Any] = {
    "source_id": "us.ferc.form1_lines",
    "name": "FERC Form 1 lines",
    "reuse_class": "open",
    "retrieved_at_max": "2026-10-02T00:00:00Z",
}
EMPTY_GRID: dict[str, Any] = {
    "data": {"radius_km": 25.0, "transmission_lines": [], "interconnection_points": []}
}


def _grid_routes(entity: dict[str, Any], grid: tuple[int, Any] | None) -> dict[str, Any]:
    routes = _asset_routes(entity, nearby=[])
    key = f"/v1/assets/{entity['public_id']}/nearby-grid"
    if grid is None:
        routes.pop(key)
    else:
        routes[key] = grid
    return routes


@pytest.mark.parametrize(
    ("entity", "grid"),
    [
        # EIA-860 lists no grid fields for the plant, and nothing is mapped around it.
        (_retired(), None),
        (_retired(), (200, EMPTY_GRID)),
        # No mapped location, so the grid around it cannot be asked for.
        (_retired(geometry=None), None),
        # The call fails: the tab is not offered on a guess.
        (_retired(), (503, {"title": "unavailable"})),
    ],
)
def test_grid_is_absent_when_the_plant_reports_no_grid_and_nothing_is_listed_around_it(
    client: TestClient, entity: dict[str, Any], grid: tuple[int, Any] | None
) -> None:
    _install(_grid_routes(entity, grid))
    overview = client.get("/assets/big-spring-us-tx").text
    assert 'href="/assets/big-spring-us-tx/grid"' not in overview  # T-3: not linked
    assert 'id="grid"' not in overview and "Grid connection" not in overview  # no empty Grid block
    resp = client.get("/assets/big-spring-us-tx/grid")
    assert resp.status_code == 404
    line = record_tabs.ABSENT_LINES["asset", "grid"]
    assert f'<p class="record-absent" role="status">{line}</p>' in resp.text
    assert "EIA-860 states no grid voltage" not in resp.text
    # The note on reusing a retiring plant's connection is not lost: it stays on the Retirement tab.
    assert "Can the connection be reused?" in client.get("/assets/big-spring-us-tx/retirement").text


def test_grid_shows_for_a_plant_without_grid_fields_when_lines_are_listed_around_it(
    client: TestClient,
) -> None:
    grid = {
        "data": {
            "radius_km": 25.0,
            "transmission_lines": [
                {"slug": "line-a", "name": "Line A", "voltage_kv": 345, "distance_km": 2.1}
            ],
            "interconnection_points": [],
        },
        "licence_summary": {"sources": [FERC]},
    }
    _install(_grid_routes(_retired(), (200, grid)))
    overview = client.get("/assets/big-spring-us-tx").text
    block = overview[overview.index('id="grid"') :].split("</section>")[0]
    assert '<dt>Transmission lines within 25&nbsp;km</dt><dd class="tnum">1</dd>' in block
    assert 'href="/assets/big-spring-us-tx/grid"' in block
    assert "EIA-860 states no grid voltage" not in block
    # The count on the Overview is credited to the register behind the lines.
    assert "FERC Form 1 lines" in overview[overview.index("Sources of this page") :]
    tab = client.get("/assets/big-spring-us-tx/grid")
    assert tab.status_code == 200 and "Line A" in tab.text
    assert "Can the connection be reused?" not in client.get("/assets/big-spring-us-tx/retirement").text


def test_a_plant_that_reports_its_grid_keeps_the_grid_call_to_the_grid_tab(client: TestClient) -> None:
    """T-10 holds where it can: the grid around a plant is read on every tab only when the plant's own
    EIA-860 fields cannot show that Grid has content."""
    transport = _install(_asset_routes(_plant()))
    client.get("/assets/big-spring-us-tx/retirement")
    assert not any(url.endswith("/nearby-grid") for _, url, _ in transport.calls)
    transport = _install(_grid_routes(_retired(), (200, EMPTY_GRID)))
    client.get("/assets/big-spring-us-tx/retirement")
    assert any(url.endswith("/nearby-grid") for _, url, _ in transport.calls)


# ------------------------------------------------------------- review fixes: every row attributed
PUCT: dict[str, Any] = {
    "source_id": "us.tx.puc_large_load",
    "source_name": "PUCT Large Load Docket",
    "source_url": "https://interchange.puc.texas.gov/x",
    "retrieved_at": "2026-10-15T00:00:00Z",
    "reuse_class": "attribution",
    "attribution_text": "Source: Public Utility Commission of Texas.",
}


def test_the_point_overview_credits_the_register_of_each_change_it_lists(client: TestClient) -> None:
    """T-4, D-2/D-34: the Overview lists the first change rows, so its credits line names the register
    that recorded them, with that licence's own credit text, not only the point's register."""
    _install({"/v1/interconnection-points/poi_1": (200, _point([{**CHANGE, "provenance": PUCT}]))})
    body = client.get("/interconnection-points/poi_1").text
    assert "Status changed from filed to studied" in body[body.index('id="changes"') :]
    foot = body[body.index("Sources of this page") :].split("</p>")[0]
    assert "ERCOT GIS Report" in foot
    assert "PUCT Large Load Docket" in foot and "Source: Public Utility Commission of Texas" in foot


def test_the_point_changes_tab_carries_no_count_that_reads_as_a_total(client: TestClient) -> None:
    _install({"/v1/interconnection-points/poi_1": (200, _point([CHANGE] * 7))})
    body = client.get("/interconnection-points/poi_1").text
    bar = body[body.index('<nav class="record-tabs"') :].split("</nav>")[0]
    assert '<a href="/interconnection-points/poi_1/changes">Changes</a>' in bar
    block = body[body.index('id="changes"') :].split("</section>")[0]
    assert ">Open Changes</a>" in block and "All 7" not in block


def test_credits_do_not_repeat_a_register_name_given_as_its_credit_text() -> None:
    rows = record_tabs.credits([EIA, PUCT])
    assert rows[0]["name"] == "EIA-860M" and rows[0]["credit"] is None
    assert rows[1]["credit"] == "Source: Public Utility Commission of Texas."  # kept exactly (L-2)


# ------------------------------------------------------------- review fixes: no content lost
KINDER = {"public_id": "org_KM", "slug": "kinder-morgan", "name_canonical": "Kinder Morgan"}


def _pipeline(owners: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "public_id": "asset_PL",
        "slug": "permian-highway",
        "name": "Permian Highway Pipeline",
        "asset_type": "gas_pipeline",
        "status": "operating",
        "length_miles": 428.0,
        "states": "TX",
        "geometry": {"type": "LineString", "coordinates": [[-103, 31], [-100, 30], [-97, 29.5]]},
        "attributes": {},
        "owners": owners,
        "provenance": [EIA],
    }


def _edge(role: str, **extra: Any) -> dict[str, Any]:
    return {"organization": KINDER, "role": role, "provenance": {"source_name": "EIA-860M"}, **extra}


def _company_line(body: str) -> str:
    return body[body.index('id="record-company"') :].split("</p>")[0]


def test_an_operator_only_asset_keeps_its_records_source_and_date_on_the_header(client: TestClient) -> None:
    """A pipeline whose register names only an operator: no Ownership tab, so the header line carries
    that record's source and date on every tab (docs/30 §11.5), and the old `#owners` anchor (T-8)."""
    _install(_asset_routes(_pipeline([_edge("operator", as_of="2025-01-01")]), nearby=[]))
    for path in ("/assets/permian-highway", "/assets/permian-highway/sources"):
        line = _company_line(_header(client.get(path).text))
        assert 'Operated by</span> <a href="/organizations/kinder-morgan">Kinder Morgan</a>' in line, path
        assert '<span class="record-company__source tnum">EIA-860M, as of 1 Jan 2025</span>' in line, path
    overview = client.get("/assets/permian-highway").text
    assert 'href="/assets/permian-highway/ownership"' not in overview
    assert overview.count('id="owners"') == 1 and '<span id="owners">' in _company_line(overview)
    resp = client.get("/assets/permian-highway/ownership")
    assert resp.status_code == 404
    assert record_tabs.ABSENT_LINES["asset", "ownership"] in resp.text


def test_one_company_that_owns_and_operates_stays_the_header_line_with_both_records(
    client: TestClient,
) -> None:
    """§11.5: a sole owner and operator stays the header line, so one company's two records open no
    Ownership tab; the operator record's own date is kept beside the owner's."""
    owners = [_edge("owner", share_pct=100.0, as_of="2025-12-31"), _edge("operator", as_of="2025-01-01")]
    _install(_asset_routes(_pipeline(owners), nearby=[]))
    body = client.get("/assets/permian-highway").text
    line = _company_line(body)
    assert "Owned and operated by</span>" in line
    assert "100.0%, EIA-860M, as of 31 Dec 2025; operator: EIA-860M, as of 1 Jan 2025" in line
    assert 'href="/assets/permian-highway/ownership"' not in body
    assert client.get("/assets/permian-highway/ownership").status_code == 404
    # The same two records with one date say it once.
    owners[1]["as_of"] = "2025-12-31"
    _install(_asset_routes(_pipeline(owners), nearby=[]))
    line = _company_line(client.get("/assets/permian-highway").text)
    assert '<span class="record-company__source tnum">100.0%, EIA-860M, as of 31 Dec 2025</span>' in line


def test_an_asset_with_no_ownership_record_says_so_on_its_ownership_url(client: TestClient) -> None:
    _install(_asset_routes(_pipeline([]), nearby=[]))
    resp = client.get("/assets/permian-highway/ownership")
    assert resp.status_code == 404
    assert '<p class="record-absent" role="status">No ownership records for this asset.</p>' in resp.text


# ------------------------------------------------------------- review fixes: true facts in the header
def test_a_retired_plants_header_says_commissioned_not_in_service_since(client: TestClient) -> None:
    _install(_grid_routes(_retired(), (200, EMPTY_GRID)))
    for path in (
        "/assets/big-spring-us-tx",
        "/assets/big-spring-us-tx/retirement",
        "/assets/big-spring-us-tx/grid",  # the 404 tab page carries the header too
    ):
        header = _header(client.get(path).text)
        assert "In service since" not in header, path
        assert '<div><dt>Commissioned</dt><dd class="tnum">1972</dd></div>' in header, path


def test_the_overview_leads_retirement_with_how_much_by_when_not_the_status(client: TestClient) -> None:
    entity = _plant()
    entity["attributes"]["retirement"].update(retired_mw=60.0, retired_units=1)
    _install(_asset_routes(entity))
    body = client.get("/assets/big-spring-us-tx").text
    block = body[body.index('id="retirement"') :].split("</section>")[0]
    assert re.findall(r"<dt>([^<]+)</dt>", block) == [
        "Capacity scheduled to retire",
        "Next scheduled retirement",
        "Capacity retired",
    ]
    assert '<dd class="tnum">320 MW</dd>' in block and '<dd class="tnum">December 2028</dd>' in block
    _install(_grid_routes(_retired(), (200, EMPTY_GRID)))
    body = client.get("/assets/big-spring-us-tx").text
    block = body[body.index('id="retirement"') :].split("</section>")[0]
    assert re.findall(r"<dt>([^<]+)</dt>", block)[:2] == ["Retired", "Capacity retired"]


def test_the_retirement_tab_leaves_the_status_to_the_header_badge(client: TestClient) -> None:
    """The status is the header's badge on every tab; the Retirement tab's facts do not say it again."""
    _install(_asset_routes(_plant()))
    body = client.get("/assets/big-spring-us-tx/retirement").text
    assert "· Retiring</span>" in _header(body)
    tab = body[body.index('<section class="record-tab retirement"') :].split("</section>")[0]
    assert "<dt>Status</dt>" not in tab
    assert "<dt>Capacity scheduled to retire</dt>" in tab and "<dt>Next scheduled retirement</dt>" in tab


def test_the_overview_keeps_the_retirement_block_when_the_plant_states_no_figure(client: TestClient) -> None:
    """T-8: a plant with a Retirement tab keeps `#retirement` on its Overview, linking to the tab, even
    when its source states no date or capacity for the Overview to lead with."""
    _install(_asset_routes(_plant(attributes={"retirement": {"status_rule": "some_units_retiring"}})))
    body = client.get("/assets/big-spring-us-tx").text
    assert 'href="/assets/big-spring-us-tx/retirement"' in body[body.index('<nav class="record-tabs"') :]
    block = body[body.index('id="retirement"') :].split("</section>")[0]
    assert '<a href="/assets/big-spring-us-tx/retirement">Open Retirement</a>' in block
    assert "<dt>" not in block


# ------------------------------------------------------------- review fixes: one address per page
@pytest.mark.parametrize(
    ("routes", "path", "location"),
    [
        (
            _proposal_routes(_proposal()),
            "/proposals/bearkat-solar-b1/overview?utm_source=x",
            "/proposals/bearkat-solar-b1?utm_source=x",
        ),
        (_asset_routes(_plant()), "/assets/big-spring-us-tx/overview", "/assets/big-spring-us-tx"),
        (
            {"/v1/interconnection-points/poi_1": (200, _point())},
            "/interconnection-points/poi_1/overview",
            "/interconnection-points/poi_1",
        ),
    ],
)
def test_an_overview_segment_answers_301_to_the_record(
    client: TestClient, routes: dict[str, Any], path: str, location: str
) -> None:
    _install(routes)
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 301 and resp.headers["location"] == location


@pytest.mark.parametrize(
    ("routes", "base", "tabs", "page_type"),
    [
        (_proposal_routes(_proposal()), "/proposals/bearkat-solar-b1", ("timeline", "sources"), "proposal"),
        (_asset_routes(_plant()), "/assets/big-spring-us-tx", ("retirement", "nearby", "sources"), "asset"),
        (
            {"/v1/interconnection-points/poi_1": (200, _point([CHANGE]))},
            "/interconnection-points/poi_1",
            ("projects", "changes", "sources"),
            "point",
        ),
    ],
)
def test_a_visit_across_a_records_tabs_counts_one_view(
    client: TestClient, routes: dict[str, Any], base: str, tabs: tuple[str, ...], page_type: str
) -> None:
    """PM-5's `page.viewed` series counts views of a record: the Overview counts, its tabs do not, so
    moving through Overview, Timeline and Sources is one view, as the one-page record was."""
    transport = _install(routes)
    for path in (base, *(f"{base}/{tab}" for tab in tabs)):
        assert client.get(path).status_code == 200, path
    views = [body for method, url, body in transport.calls if method == "POST" and url == "/v1/ui-events"]
    assert views == [{"name": "page.viewed", "props": {"page_type": page_type}}]
