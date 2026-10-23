"""Layout-lane fixes from the 2026-09-30 platform audit, server side: the empty, invalid and error
states of the list and map pages (designer D-11, docs/31 §6 and §5.9), the "View as: Map · List"
cross-links and the proposal page's Location section (D-8), the 400px disclosures' markup (D-4),
and the self-hosted fonts that stop `/assets` shifting at 1440px (frontend F8).

The browser halves (the viewport in the URL, the collapsed header and filter bar at 400px, the
in-view list's "Show more", the map's error state) are in `web/test_e2e.py`. The fake transport is
duplicated from `web/test_map_filter_passthrough.py`, per this repo's convention that no
`web/test_*.py` imports another.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Callable, Iterator, Mapping
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import QueryParams

from web.api_client import ApiClient
from web.app import app as web_app
from web.app import map_view_href
from web.empty_state import range_error
from web.page import templates
from web.proposal_location import proposal_location
from web.viewmodels import PROPOSAL_GROUP_SUBJECTS, PROPOSAL_SOURCE_LABELS, map_description, map_heading

WEB = Path(__file__).resolve().parent

Answer = tuple[int, Any] | Callable[[Mapping[str, Any]], tuple[int, Any]]


class FakeTransport:
    def __init__(self, responses: Mapping[str, Answer]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _respond(self, url: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        if url in self.responses:
            answer = self.responses[url]
            status, body = answer(params or {}) if callable(answer) else answer
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"title": "not_found", "detail": url})

    def get(
        self, url: str, *, params: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("GET", url, dict(params or {})))
        return self._respond(url, params)

    def post(
        self, url: str, *, json: Mapping[str, Any] | None = None, cookies: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(("POST", url, dict(json or {})))
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
        self.calls.append((method, url, dict(json if json is not None else params or {})))
        return self._respond(url, params)

    def close(self) -> None:
        pass

    def params_for(self, url: str) -> list[dict[str, Any]]:
        return [params for _method, called, params in self.calls if called == url]


HEALTH: dict[str, Any] = {
    "status": "ok",
    "data_as_of": "2026-09-01",
    "live_as_of": "2026-09-15T00:00:00Z",
    "lag_days_default": {"supply": 0, "opportunities": 0},
    # A finished site-finder run, so the primary navigation lists it (`site_finder_listed`).
    "site_finder": {"computed": True, "segments": 120699},
}
VOCAB: dict[str, Any] = {
    "data": {
        "technology": [{"value": "solar"}, {"value": "storage"}],
        "proposal_kind": [{"value": "generation"}, {"value": "load"}],
        "opportunity_kind": [{"value": "rfp"}],
        "opportunity_status": [{"value": "open"}],
        "opportunity_technology": [{"value": "solar_pv"}],
        "slip_bucket": [],
    }
}


def _page(total: int) -> dict[str, Any]:
    return {
        "data": [],
        "meta": {"total": total, "total_is_estimate": False},
        "page": {"next_cursor": None, "prev_cursor": None, "has_more": False},
        "licence_summary": [],
    }


def _counted(params: Mapping[str, Any]) -> tuple[int, Any]:
    """Nothing matches a made-up jurisdiction; every other query counts 7."""
    return 200, _page(0 if params.get("jurisdiction") == "US-ZZ" else 7)


@pytest.fixture()
def transport() -> Iterator[FakeTransport]:
    fake = FakeTransport(
        {
            "/v1/health": (200, HEALTH),
            "/v1/meta/vocabularies": (200, VOCAB),
            "/v1/sources": (200, {"data": []}),
            "/v1/proposals": _counted,
            "/v1/opportunities": _counted,
            "/v1/interconnection-points": _counted,
        }
    )
    web_app.state.api_client = ApiClient(fake)
    web_app.state.lag_days_default = None
    yield fake
    for key in ("api_client", "lag_days_default", "sitemap_cache", "asset_type_counts_cache"):
        web_app.state.__dict__.pop(key, None)


def _text(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


# ---- docs/31 §5.9: an impossible range is said inline, not answered with "no proposals" ----


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        ("500", "10", "Minimum capacity (500) is above the maximum (10)"),
        ("-5", "", "Capacity cannot be negative."),
        ("abc", "", "Minimum capacity must be a number."),
        ("", "1e", "Maximum capacity must be a number."),
    ],
)
def test_range_error_names_what_is_wrong(low: str, high: str, expected: str) -> None:
    qp = {"capacity_mw[gte]": low, "capacity_mw[lte]": high}
    message = range_error(qp, "capacity_mw[gte]", "capacity_mw[lte]", noun="capacity")
    assert message is not None and message.startswith(expected)


@pytest.mark.parametrize(("low", "high"), [("10", "500"), ("10", "10"), ("", "500"), ("", "")])
def test_range_error_is_none_for_a_usable_pair(low: str, high: str) -> None:
    qp = {"capacity_mw[gte]": low, "capacity_mw[lte]": high}
    assert range_error(qp, "capacity_mw[gte]", "capacity_mw[lte]", noun="capacity") is None


def test_an_impossible_capacity_range_is_said_beside_the_control(transport: FakeTransport) -> None:
    """Designer D-11: `capacity_mw[gte]=500&capacity_mw[lte]=10` asked the API a question no row
    can answer and printed "No proposals match these filters." with no word about why."""
    with TestClient(web_app) as client:
        html = client.get("/proposals?capacity_mw[gte]=500&capacity_mw[lte]=10").text
    text = _text(html)
    assert "Minimum capacity (500) is above the maximum (10)" in text
    assert 'id="f-capacity-error"' in html and 'aria-invalid="true"' in html
    assert 'aria-describedby="f-capacity-error"' in html
    # Nothing was asked of the API for rows or counts, and no "Showing 0 active proposals" reads
    # as a fact about the register.
    assert all("capacity_mw[gte]" not in params for params in transport.params_for("/v1/proposals"))
    assert "Showing 0 active proposals" not in text
    assert "No proposals match these filters." not in text


# ---- docs/31 §6: an empty result names the facet that emptied it, with a way out ----


def test_an_empty_proposal_list_names_the_filter_that_emptied_it(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        html = client.get("/proposals?jurisdiction=US-ZZ&technology=solar").text
    text = _text(html)
    assert "Jurisdiction: “US-ZZ” · without it, 7 proposals match." in text
    assert "Jurisdiction codes look like US-TX, GB or DE." in text
    # "Remove this filter" keeps every other parameter of the view.
    assert 'href="/proposals?technology=solar"' in html
    # Removing the technology alone still leaves US-ZZ, which matches nothing.
    assert "removing it alone still matches nothing." in text
    assert "Clear all filters" in text


def test_the_list_notice_gives_the_list_its_own_reason(transport: FakeTransport) -> None:
    """D-11: the list page said withdrawn proposals are hidden "so the map isn't dominated by dead
    projects", which is the map's reason, on the list."""
    with TestClient(web_app) as client:
        text = _text(client.get("/proposals").text)
    assert "Withdrawn and cancelled proposals (7) are hidden by default" in text
    assert "dominated" not in text


def test_an_empty_opportunity_list_names_the_filter(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        html = client.get("/opportunities?jurisdiction=US-ZZ").text
    text = _text(html)
    # The page opens on open and announced energy-related notices (docs/16 U-7; lane rfp-view),
    # and the sentence says so.
    assert "No open or announced energy-related notices match these filters right now." in text
    assert "Jurisdiction: “US-ZZ” · without it, 7 opportunities match." in text


def test_an_empty_point_list_says_so_once_and_names_the_filter(transport: FakeTransport) -> None:
    """D-11: `/interconnection-points?jurisdiction=US-ZZ` printed its empty sentence twice."""
    with TestClient(web_app) as client:
        html = client.get("/interconnection-points?jurisdiction=US-ZZ&iso=ercot").text
    text = _text(html)
    assert text.count("No interconnection points match these filters.") == 1
    assert "Jurisdiction: “US-ZZ”" in text
    assert 'href="/interconnection-points?iso=ercot"' in html


def test_the_map_names_the_filter_that_emptied_it(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        html = client.get("/?jurisdiction=US-ZZ").text
    assert 'class="map-empty"' in html
    assert "Jurisdiction: “US-ZZ” · without it, 7 proposals match." in _text(html)


def test_the_map_page_has_an_error_slot_and_no_world_geo_query(transport: FakeTransport) -> None:
    """D-11: with `/api/proposals/geo` failing the map said "Loading proposals…" for good; map.js
    now fills `#map-error` (an alert region). F9: the page no longer waits on a world-wide
    clustered geo query to count lifecycle states."""
    with TestClient(web_app) as client:
        html = client.get("/").text
    assert re.search(r'<div class="map-error" id="map-error" role="alert" hidden>', html)
    assert transport.params_for("/v1/proposals/geo") == []


# ---- D-8: "View as: Map · List", each carrying the query ----


def test_the_list_links_back_to_the_map_with_its_filters_and_viewport(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        html = client.get("/proposals?technology=solar&center=-77.5000,38.9000&zoom=8.00&cursor=abc").text
    href = re.search(r'id="view-as-map" href="([^"]+)"', html)
    assert href is not None
    query = parse_qs(urlsplit(href.group(1).replace("&amp;", "&")).query)
    assert query == {"technology": ["solar"], "center": ["-77.5000,38.9000"], "zoom": ["8.00"]}
    assert re.search(r'id="view-as-list" href="[^"]*" aria-current="page"', html)
    # The viewport rides along in the filter form, so it survives "Apply filters".
    assert '<input type="hidden" name="center" value="-77.5000,38.9000">' in html


def test_the_map_view_link_drops_a_malformed_viewport() -> None:
    qp = QueryParams("kind=load&center=<script>&zoom=8")
    assert map_view_href(qp) == "/?kind=load"


def test_the_map_links_to_the_list_with_its_filters(transport: FakeTransport) -> None:
    """Owner, 2026-10-22: the map's "View as" row is gone; "Open these in the list" beside "Save this
    search as an alert" links the list with the same filters (map.js rewrites it with every change)."""
    with TestClient(web_app) as client:
        html = client.get("/?kind=load&placement=exact,region&layers=plants").text
    href = re.search(r'id="map-list-link" href="([^"]+)"', html)
    assert href is not None
    # The map's default placement is a drawing choice and layers are not proposal filters.
    assert href.group(1) == "/proposals?kind=load"
    assert 'id="view-as-map"' not in html and 'id="view-as-list"' not in html


def test_no_status_chosen_shows_nothing_from_first_paint(transport: FakeTransport) -> None:
    """Owner report 2026-10-22: unticking all three default Status boxes ticked them all again. Every
    box unticked is now `lifecycle_state=none`: the count line, the notice and the Status summary
    say so from first paint, the list and save links wait, and the map's geo route answers an empty
    view without asking the API."""
    with TestClient(web_app) as client:
        html = client.get("/?lifecycle_state=none").text
        notice = client.get("/api/proposals/notice?lifecycle_state=none").text
        geo = client.get("/api/proposals/geo?lifecycle_state=none&bbox=-179,-85,179,85&zoom=3").json()
    count = re.search(r'<p class="result-count" id="map-result-count"([^>]*)>([^<]*)</p>', html)
    assert count is not None and count.group(2) == "0 projects match these filters."
    assert "aria-hidden" not in count.group(1)
    for page in (html, notice):
        assert "No status chosen: projects are hidden." in page
    summary = re.search(r"<span data-status-summary>([^<]*)</span>", html)
    assert summary is not None and summary.group(1) == "none"
    assert not re.search(r'id="mf-status-\w+"[^>]*\schecked[\s>]', html)  # no box ticked
    actions = re.search(r'<p class="map-status__actions" id="map-status-actions"([^>]*)>', html)
    assert actions is not None and "hidden" in actions.group(1)
    assert geo["data"]["features"] == [] and geo["data"]["totals"]["records"] == 0
    assert not [p for p in transport.params_for("/v1/proposals/geo") if p.get("lifecycle_state") == "none"]


# ---- D-4: the 400px disclosures are in the markup, and inert without scripts ----


def test_every_page_has_the_menu_disclosure_and_list_pages_a_filters_button(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        for path, form_id in (("/", "map-filters"), ("/proposals", "filters"), ("/opportunities", "filters")):
            html = client.get(path).text
            nav_toggle = (
                '<button type="button" class="nav-toggle" aria-expanded="false" aria-controls="site-menu">'
                "Menu</button>"
            )
            assert nav_toggle in html
            assert '<div class="site-menu" id="site-menu">' in html
            search_toggle = (
                '<button type="button" class="search-toggle" aria-expanded="false" '
                'aria-controls="site-search">'
            )
            assert search_toggle in html and '<form class="site-search" id="site-search"' in html
            assert f'aria-controls="{form_id}" data-filter-toggle' in html, path
            assert re.search(rf'<form id="{form_id}" class="[^"]*" data-collapse', html), path
            assert '<script>document.documentElement.classList.add("js")</script>' in html
            assert "/static/js/site.js" in html
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    # Every collapsed rule is keyed on `.js`, so a page without scripts keeps its links, search and filters.
    assert ".js .site-menu:not(.is-open) { display: none; }" in css
    assert ".js .site-search { display: none; }" in css
    assert ".js .filter-bar[data-collapse]:not(.is-open) { display: none; }" in css


# ---- D-3: the map page opens with one plain line and the primary action, then the map ----


def test_the_map_heading_names_every_group_of_sources_on_the_map() -> None:
    """2026-10-07: EPA Class VI wells were drawn under "Planned power plants, batteries and data
    centres". The heading is built from the source groups, so a new group without words fails."""
    groups = {group for _, group in PROPOSAL_SOURCE_LABELS.values()}
    assert groups <= set(PROPOSAL_GROUP_SUBJECTS)
    assert map_heading() == "Planned power plants, batteries, data centres and CO2 storage wells"
    assert map_description().startswith(map_heading() + ", ")


def test_the_map_page_heading_and_its_meta_share_one_source(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        html = client.get("/").text
    title = re.search(r'<h1 class="map-intro__title">(.*?)</h1>', html, re.S)
    assert title is not None and unescape(title.group(1)) == map_heading()
    # The description ends with the map's own sentence; on the plain map it opens with the live
    # figures first (web/headline.py, owner 2026-10-23), so a link preview says something specific.
    for attr in ('name="description"', 'property="og:description"', 'name="twitter:description"'):
        match = re.search(rf'<meta {attr} content="([^"]*)"', html)
        assert match is not None and unescape(match.group(1)).endswith(map_description()), attr
    assert "Planned power plants, batteries and data centres<" not in html


def test_the_map_page_opens_on_a_plain_line_not_a_source_list(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        html = client.get("/").text
    intro = re.search(r'<div class="map-intro">(.*?)</div>', html, re.S)
    assert intro is not None
    text = _text(intro.group(1))
    assert text.startswith(map_heading())
    assert "Click a cluster to zoom in, or a dot to open its record." in text
    assert not re.search(r"\b(ERCOT|CAISO|NYISO|EIA-860M|NESO)\b", text)
    # The map comes before the in-view list and nothing but the filters and one status row sit
    # between the intro and the canvas.
    assert html.index('id="map-filters"') < html.index('class="map-status"') < html.index('id="map"')


# ---- Review 2026-10-10 §2.6 item 2: value first, lateness once, the key on the map ----


def _late_transport() -> FakeTransport:
    """The `transport` answers plus `/v1/coverage` naming one source behind its fetch schedule."""
    rows = [
        {
            "source_id": "us.eia.860m",
            "name": "EIA-860M",
            "fetched_at": "2026-10-07T10:36:13",
            "freshness": {"status": "fresh"},
        },
        {
            "source_id": "us.iso.caiso.gen_queue",
            "name": "CAISO Public Queue Report",
            "fetched_at": "2026-09-13T20:25:21",
            "freshness": {"status": "stale"},
        },
    ]
    return FakeTransport(
        {
            "/v1/health": (200, HEALTH),
            "/v1/meta/vocabularies": (200, VOCAB),
            "/v1/sources": (200, {"data": []}),
            "/v1/proposals": _counted,
            "/v1/opportunities": _counted,
            "/v1/coverage": (200, {"data": {"vintage": {"sources": rows}}}),
        }
    )


def _forget(key: str) -> None:
    """Starlette's `State` keeps attributes in its own dict, so `delattr` is the way to drop one."""
    try:
        delattr(web_app.state, key)
    except (AttributeError, KeyError):
        pass


@pytest.fixture()
def late() -> Iterator[FakeTransport]:
    fake = _late_transport()
    _forget("coverage_data")
    web_app.state.api_client = ApiClient(fake)
    web_app.state.lag_days_default = None
    yield fake
    for key in ("api_client", "lag_days_default", "coverage_data", "build_info"):
        _forget(key)


def test_the_map_page_says_what_the_product_does_before_anything_else(transport: FakeTransport) -> None:
    """Before: "Each dot is a proposal filed with a public grid queue or permit register in the US or
    GB", then a tier notice about delay and late sources. Now one statement of value for the
    reader: projects matched across registers, each claim sourced, each status change dated, alerts.
    "One record per project" is not claimed until it is measured (docs/51 §2.8)."""
    with TestClient(web_app) as client:
        html = client.get("/").text
    intro = re.search(r'<div class="map-intro">(.*?)</div>', html, re.S)
    assert intro is not None
    lede = _text(intro.group(1)).removeprefix(map_heading()).strip()
    assert lede.startswith("Projects from public grid-queue and permit filings in the US and GB, matched")
    for claim in ("every claim linked to its source", "every status change dated", "alerts when"):
        assert claim in lede
    assert '<a href="/alerts">alerts</a>' in intro.group(1)
    assert "Each dot is a proposal filed" not in html


def test_the_map_page_states_source_lateness_in_the_header_not_in_a_notice(late: FakeTransport) -> None:
    """docs/30 §11.2: the masthead line goes; the freshness line keeps its strings and its link, at the
    section bar's right end from 1280px and in the phone Menu. The two are never drawn at one width:
    the section bar's copy is hidden below 1280px, and the Menu exists only below 720px."""
    with TestClient(web_app) as client:
        home = client.get("/").text
        listing = client.get("/proposals").text
    # Home: no tier notice block; the line once in the section bar and once in the Menu.
    assert 'class="delayed-notice"' not in home and 'class="masthead' not in home
    assert home.count("behind their fetch schedule") == 2
    bar = home.split('<nav class="section-bar"', 1)[1].split("</nav>", 1)[0]
    menu = home.split('<div class="site-menu" id="site-menu">', 1)[1].split("</header>", 1)[0]
    for place in (bar, menu):
        lag = re.search(r'<span class="fresh-line">(.*?)</span>', place, re.S)
        assert lag is not None and '<a href="/methodology#vintage">' in lag.group(1)
        assert _text(lag.group(1)) == "1 of 2 sources behind their fetch schedule"
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert ".section-bar__util .fresh-line { display: none; }" in css
    assert (
        "@media (min-width: 1280px) {\n  .section-bar__title { display: block; }\n"
        "  .section-bar__util .fresh-line { display: inline; }\n}"
    ) in css
    assert ".site-menu { display: none; }" in css
    # The list keeps its tier notice, which names the late source.
    assert 'class="delayed-notice"' in listing and "CAISO" in listing


def test_the_dev_preview_label_survives_without_the_tier_notice(transport: FakeTransport) -> None:
    web_app.state.preview_active = True
    try:
        with TestClient(web_app) as client:
            html = client.get("/").text
    finally:
        _forget("preview_active")
    assert 'class="preview-banner"' in html and 'class="delayed-notice"' not in html


def _media_block(css: str, query: str) -> str:
    """The body of the first `@media <query> {` block, up to its closing brace at column 0."""
    start = css.index(f"@media {query} {{\n")
    return css[start : css.index("\n}\n", start)]


# ---- Owner, 2026-10-22: one panel over the map holds what is in view and the key ----


def test_the_key_is_a_tab_of_the_panel_floating_over_the_map(transport: FakeTransport) -> None:
    """Owner, 2026-10-22: the map takes the full width, and one panel over its top right holds what
    is in view and the key, as two tabs under one header; the separate floating key and its toggle
    are gone. The rail's "Key" (drawn below 720px) opens the panel's Key tab."""
    with TestClient(web_app) as client:
        html = client.get("/").text
    assert 'class="map-layout"' not in html and 'class="map-side"' not in html
    frame = html.split('<div class="map-frame" id="map-frame">', 1)[1].split(
        '<p class="map-attribution">', 1
    )[0]
    panel = frame.split('<section class="map-panel" id="map-panel"', 1)[1]
    assert frame.index('<div id="map"') < frame.index('id="map-panel"')  # after the canvas: a phone's bar
    for tab, controls in (("map-tab-inview", "map-tabpanel-inview"), ("map-tab-key", "map-tabpanel-key")):
        button = re.search(
            rf'<button type="button" role="tab" class="map-panel__tab" id="{tab}"[^>]*>', panel
        )
        assert button is not None and f'aria-controls="{controls}"' in button.group(0), tab
    key = panel.split('id="map-tabpanel-key"', 1)[1]
    for legend in (
        'id="lifecycle-legend"',
        'id="plants-legend"',
        'id="retired-legend"',
        'id="region-legend"',
    ):
        assert legend in key, legend
    assert "map-key__panel" not in html and "map-key--compact" not in html
    assert 'id="in-view-items"' in panel.split('id="map-tabpanel-key"', 1)[0]
    toggle = re.search(r'<button type="button" class="map-panel__toggle" id="map-panel-toggle"[^>]*>', panel)
    assert toggle is not None and 'aria-controls="map-panel-body"' in toggle.group(0)
    rail_key = re.search(r'<button type="button" id="map-key-toggle"[^>]*>', frame)
    assert rail_key is not None and 'aria-controls="map-panel-body"' in rail_key.group(0)
    assert "hidden" in rail_key.group(0)  # map.js shows it; without scripts the panel shows its key


def test_the_key_names_each_status_as_the_status_filter_does(transport: FakeTransport) -> None:
    """Owner, 2026-10-22: the key in plain words for a first-time visitor. Each status row names its
    states exactly as the Status filter's boxes do; the key says what a ring with a number is and
    that clicking it zooms in, and what the existing-infrastructure and retired layers draw."""
    with TestClient(web_app) as client:
        html = client.get("/").text
    filter_labels = [
        _text(label)
        for label in re.findall(r'<label for="mf-status-\w+">\s*<input[^>]*>(.*?)</label>', html, re.S)
    ]
    assert filter_labels[:6] == [
        "Announced",
        "In process",
        "Contracted",
        "Built or operating",
        "Withdrawn or cancelled",
        "Status unknown",
    ], filter_labels
    lifecycle = html.split('id="lifecycle-legend"', 1)[1].split('id="plants-legend"', 1)[0]
    rows = {
        family: _text(re.sub(r"<span class=\"legend__detail\">.*", "", row, flags=re.S))
        for family, row in re.findall(r'data-legend-family="(\w+)"[^>]*>(.*?)</span></span>', lifecycle, re.S)
    }
    assert rows == {
        "neutral": "Announced, or Status unknown",
        "progress": "In process",
        "committed": "Contracted",
        "success": "Built or operating",
        "danger": "Withdrawn or cancelled",
    }, rows
    words = _text(lifecycle)
    assert "Each dot is one project." in words
    assert "A ring with a number is a group of nearby projects" in words and "Click it to zoom in." in words
    plants = _text(html.split('id="plants-legend"', 1)[1].split('id="retired-legend"', 1)[0])
    assert "Power plants: small squares, coloured by fuel" in plants and "Transmission lines" in plants
    retired = _text(html.split('id="retired-legend"', 1)[1].split("</section>", 1)[0])
    assert "Plants that have closed or are scheduled to." in retired
    # Every row a reader can hover lights its marks on the map (map.js `setHighlight`).
    assert lifecycle.count("data-highlight=") >= 7


def test_the_panel_floats_translucent_and_is_a_bar_below_720px() -> None:
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    panel = css.split("\n.map-panel {\n", 1)[1].split("\n}\n", 1)[0]
    for rule in (
        "position: absolute;",
        "top: var(--map-panel-inset); right: var(--map-panel-inset);",
        "height: calc(var(--map-height) - 2 * var(--map-panel-inset)); width: var(--map-panel-width);",
        "background: color-mix(in srgb, var(--surface) 88%, transparent);",
        "backdrop-filter: blur(6px);",
        # Muted words take the neutral family's darker grey, so they keep 4.5:1 over any basemap.
        "--text-muted: var(--family-neutral-text);",
    ):
        assert rule in panel, rule
    assert (
        ".map-frame { --map-panel-inset: var(--space-3); --map-panel-width: 18.75rem;"
        " --map-height: max(70vh, 24rem); }" in css
    )
    assert "@media (min-width: 1080px) { .map-frame { --map-panel-width: 21.25rem; } }" in css
    assert ".map-panel.is-collapsed { height: auto; }" in css
    assert "@media (prefers-reduced-transparency: reduce) {" in css
    phone = _media_block(css, "(max-width: 719px)")
    phone_83 = css.split("/* ---------- 8.3 The map page below 720px", 1)[1]
    phone_83 = phone_83[phone_83.index("@media (max-width: 719px) {") :]
    bar = phone_83.split("  .map-panel, .map-panel.is-collapsed {\n", 1)[1].split("}", 1)[0]
    assert "position: static; order: 1;" in bar and "backdrop-filter: none;" in bar
    assert ".js .map-panel:not([data-ready]) .map-panel__body { display: none; }" in phone_83
    assert phone  # the generic phone block still exists
    # From 720px the drawer opens in the panel's place, and covers it.
    assert ".map-frame > .map-drawer {" in css and ".map-panel.is-covered { visibility: hidden; }" in css


def test_map_js_keeps_the_panel_choice_per_browser_and_follows_the_width() -> None:
    map_js = (WEB / "static" / "js" / "map.js").read_text(encoding="utf-8")
    default = map_js.split("function panelDefaultOpen() {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (isPhone()) return false;" in default
    assert "return !!(widePanel && widePanel.matches);" in default
    assert 'window.matchMedia("(min-width: 1080px)")' in map_js
    stored = map_js.split("function storedPanelChoice() {", 1)[1].split("\n  }\n", 1)[0]
    assert (
        "try {" in stored and "window.localStorage.getItem(PANEL_STORE)" in stored and "catch (e)" in stored
    )
    store = map_js.split("function storePanelChoice(open) {", 1)[1].split("\n  }\n", 1)[0]
    assert "try {" in store and "catch (e)" in store
    assert "if (remember && !isPhone()) storePanelChoice(open);" in map_js


def test_the_map_filter_bar_is_a_disclosure_from_720_to_1079_px(transport: FakeTransport) -> None:
    """Designer D-3 at tablet widths: two rows of filters put the map's top at 634px of 768 at
    1024x768 (761px at 720x900). From 720 to 1079px the map's bar is the "Filters (N active)"
    disclosure it already is below 720px; the list pages keep their bar open at these widths."""
    with TestClient(web_app) as client:
        home = client.get("/").text
    opening = r'<button type="button" class="filter-toggle"[^>]*aria-controls="map-filters"[^>]*>'
    toggle = re.search(opening, home)
    assert toggle is not None and 'aria-expanded="false"' in toggle.group(0)
    assert home.index('aria-controls="map-filters"') < home.index('<form id="map-filters"')
    form = home.split('<form id="map-filters"', 1)[1].split("</form>", 1)[0]
    assert form.startswith(' class="filter-bar filter-bar--map" data-collapse') and "data-filter-done" in form
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    tablet = _media_block(css, "(min-width: 720px) and (max-width: 1079px)")
    assert '.js .filter-toggle[aria-controls="map-filters"] { display: inline-flex;' in tablet
    assert ".js .filter-bar--map[data-collapse]:not(.is-open) { display: none; }" in tablet
    assert ".js .filter-bar--map[data-collapse].is-open .filter-bar__done {" in tablet
    # Only the map's bar: neither the generic toggle nor a list bar collapses at these widths, and
    # every collapsed rule keys on `.js`, so without scripts the bar stays open.
    assert ".js .filter-toggle {" not in tablet and ".js .filter-bar[data-collapse]" not in tablet
    rules = [line.strip() for line in tablet.splitlines()[1:] if line.strip()]
    assert rules and all(rule.startswith(".js ") for rule in rules)


# ---- D-8: the proposal page says where the record is and links to the map ----


def _entity(precision: str, lon: float, lat: float, **extra: Any) -> dict[str, Any]:
    return {
        "public_id": "prop_abc123",
        "name_canonical": "Example Solar",
        "lifecycle_state": extra.pop("lifecycle_state", "filed"),
        "jurisdiction": "US-VA",
        "location": {
            "precision": precision,
            "precision_reason": extra.pop("precision_reason", None),
            "geom": {"type": "Point", "coordinates": [lon, lat]},
            "county_name": "Loudoun County",
            "state_code": "VA",
        },
    }


def test_an_exact_proposal_gets_a_site_dot_and_a_map_link_that_opens_it() -> None:
    location = proposal_location(_entity("exact", -77.6, 39.1))
    assert location is not None
    assert location["place"] == "Loudoun County, VA"
    assert location["grade"] == "exact point (source coordinate)"
    query = parse_qs(urlsplit(location["map_href"]).query)
    assert query == {"center": ["-77.6000,39.1000"], "zoom": ["10.00"], "focus": ["prop_abc123"]}
    assert 'class="mini-map__site"' in location["svg"]
    assert "Example Solar in Virginia" in location["caption"]


def test_a_county_centroid_is_drawn_as_a_ring_and_says_it_is_not_the_site() -> None:
    location = proposal_location(_entity("county_centroid", -77.6, 39.1))
    assert location is not None
    assert location["grade"] == "county centroid, not the site"
    assert 'class="mini-map__centroid"' in location["svg"]
    assert "focus" not in location["map_href"]


def test_a_licence_downgrade_says_why_and_a_withdrawn_record_stays_drawn() -> None:
    location = proposal_location(
        _entity("county_centroid", -77.6, 39.1, precision_reason="licence", lifecycle_state="withdrawn")
    )
    assert location is not None
    assert location["grade"] == "shown at county level (source licence)"
    assert "include_withdrawn=1" in location["map_href"]


def test_a_record_without_coordinates_says_so_and_offers_no_map_link() -> None:
    entity = _entity("unknown", 0, 0)
    entity["location"]["geom"] = None
    location = proposal_location(entity)
    assert location is not None
    assert location["map_href"] is None and location["svg"] is None
    assert proposal_location({"location": None}) is None


def test_the_location_partial_renders_the_line_and_the_locator() -> None:
    location = proposal_location(_entity("exact", -77.6, 39.1))
    html = templates.get_template("partials/_proposal_location.html").render(
        location=location, record={"jurisdiction": "US-VA", "state": "VA"}
    )
    text = _text(html)
    assert "Location" in text
    assert "Loudoun County, VA · exact point (source coordinate) · Show on map" in text
    assert 'href="/proposals?jurisdiction=US-VA"' in html
    assert "<figure" in html and 'role="img"' in html


def test_the_proposal_detail_template_includes_the_location_partial() -> None:
    detail = (WEB / "templates" / "proposal_detail.html").read_text(encoding="utf-8")
    assert detail.count('{% include "partials/_proposal_location.html" %}') == 1


# ---- F8: fonts are self-hosted, preloaded and cached, so no swap moves the page ----


def test_fonts_are_self_hosted_and_every_face_has_its_file() -> None:
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in base and "fonts.gstatic.com" not in base
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    sources = re.findall(r'url\("/static/fonts/([^"]+)"\)', css)
    assert len(sources) == 23  # Newsreader latin + latin-ext; Plex Sans 4 and Mono 3 weights x Latin1-3
    for name in sources:
        assert (WEB / "static" / "fonts" / name).is_file(), name
    # Plex carries a Reserved Font Name: only IBM's own split files, under IBM's own names.
    plex = [name for name in sources if "Plex" in name or "plex" in name]
    assert plex and all(re.fullmatch(r"IBMPlex(Sans|Mono)-\w+-Latin[123]\.woff2", name) for name in plex)
    on_disk = {path.name for path in (WEB / "static" / "fonts").glob("*.woff2")}
    assert on_disk == set(sources), "every shipped font file is declared, and nothing else is shipped"
    for preloaded in re.findall(r'<link rel="preload" href="/static/fonts/([^"]+)" as="font"', base):
        assert preloaded in sources
        face = css[css.rindex("@font-face", 0, css.index(preloaded)) : css.index(preloaded)]
        # Plex keeps `optional`; the display face swaps onto a metric-matched fallback (UX-17).
        expected = "font-display: swap" if preloaded.startswith("newsreader") else "font-display: optional"
        assert expected in face, preloaded
    for licence in ("OFL-Newsreader.txt", "LICENSE-IBM-Plex-Sans.txt", "LICENSE-IBM-Plex-Mono.txt"):
        assert "SIL Open Font License" in (WEB / "static" / "fonts" / licence).read_text(encoding="utf-8")
    readme = (WEB / "static" / "fonts" / "README.md").read_text(encoding="utf-8")
    assert "@ibm/plex-sans" in readme and "@ibm/plex-mono" in readme and "unmodified" in readme
    # Only the instanced Newsreader file is modified, and the README says how it was made.
    assert "newsreader-latin-600.woff2" in readme and "instancer" in readme


def test_font_files_are_served_with_a_long_lived_cache() -> None:
    with TestClient(web_app) as client:
        response = client.get("/static/fonts/IBMPlexSans-Regular-Latin1.woff2")
        css = client.get("/static/css/styles.css")
    assert response.status_code == 200
    assert response.headers["content-type"] == "font/woff2"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "immutable" not in css.headers.get("cache-control", "")


# ---- Navigation v2 (docs/30 §11.2-§11.3, §11.8; lane V1): the top bar, the section bars, the phone
# Menu, named in one place; U-24 favicon; U-17 list headers ----

#: The Market bar in order (docs/30 §11.3), as the page prints it. RFPs & funding keeps its name until
#: grants, loans and programmes move to Funding (lane V4).
MARKET = [
    ("/", "Map"),
    ("/site-finder", "Site finder"),
    ("/proposals", "Projects"),
    ("/organizations", "Companies"),
    ("/opportunities", "RFPs &amp; funding"),
    ("/assets", "Infrastructure"),
    ("/prices", "Prices"),
]
#: The About bar: the reference pages, reached from the footer, the Menu and "About & sources".
ABOUT = [
    ("/about", "About"),
    ("/methodology", "Coverage &amp; methodology"),
    ("/attribution", "Attribution"),
    ("/docs/api", "API &amp; feeds"),
]
LISTS = ("/proposals", "/opportunities", "/organizations", "/assets", "/interconnection-points")


@pytest.fixture()
def lists(transport: FakeTransport) -> FakeTransport:
    """The fake transport with the two index endpoints the asset and company lists read."""
    transport.responses.update({"/v1/assets": _counted, "/v1/organizations": _counted})
    return transport


def _top_sections(html: str) -> str:
    return html.split('<nav class="top-bar__sections" aria-label="Site sections">', 1)[1].split("</nav>", 1)[
        0
    ]


def _section_bar(html: str) -> str | None:
    """The section bar, from its `<nav>` to its end, or None when the page has none."""
    if '<nav class="section-bar"' not in html:
        return None
    return html.split('<nav class="section-bar"', 1)[1].split("</nav>", 1)[0]


def _bar_items(html: str) -> str:
    bar = _section_bar(html)
    assert bar is not None
    return bar.split('<ul class="section-bar__items">', 1)[1].split("</ul>", 1)[0]


def _header(html: str) -> str:
    """Everything the shell draws above `<main>`: the beta bar, the top bar, the Menu, the section bar."""
    return html.split('<a class="skip-link"', 1)[1].split('<main id="main"', 1)[0]


def _links(fragment: str) -> list[tuple[str, str | None, str]]:
    """(href, aria-current or None, text) for each link, in order."""
    found = re.findall(r'<a href="([^"]+)"(?: aria-current="([a-z]+)")?>([^<]*)</a>', fragment)
    return [(href, current or None, text.strip()) for href, current, text in found]


def _h1(html: str) -> str:
    match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    assert match is not None
    return _text(match.group(1))


@pytest.mark.parametrize(
    ("path", "top", "current"),
    [
        ("/", "page", {"/": "page"}),
        ("/proposals", "true", {"/proposals": "page"}),
        ("/proposals?technology=solar", "true", {"/proposals": "page"}),
        ("/opportunities", "true", {"/opportunities": "page"}),
        ("/organizations", "true", {"/organizations": "page"}),
        ("/assets", "true", {"/assets": "page"}),
        # The second Infrastructure tab: its section's link is marked, though it points elsewhere.
        ("/interconnection-points", "true", {"/assets": "true"}),
    ],
)
def test_the_market_bar_lists_its_pages_in_order_and_marks_where_the_reader_is(
    lists: FakeTransport, path: str, top: str, current: dict[str, str]
) -> None:
    """Signed out: the top bar holds Market alone (My pipeline is signed-in only; Funding waits for
    `/funding`), marked `page` on the map and `true` on every other Market page; the section bar under
    it lists Market's pages in order and marks the one the reader is on."""
    with TestClient(web_app) as client:
        html = client.get(path).text
    assert _links(_top_sections(html)) == [("/", top, "Market")], path
    bar = _section_bar(html)
    assert bar is not None and bar.startswith(' aria-label="Market pages">'), path
    assert '<span class="section-bar__title">Market intelligence</span>' in bar
    links = _links(_bar_items(html))
    assert [(href, text) for href, _, text in links] == MARKET, path
    assert {href: mark for href, mark, _ in links if mark} == current, path


@pytest.mark.parametrize("finder", [None, {"computed": False, "segments": 0}])
def test_the_site_finder_is_not_listed_until_it_has_a_run_to_show(
    lists: FakeTransport, finder: dict[str, Any] | None
) -> None:
    """Owner, 2026-10-22: no empty sections. Without a finished run with segments (`GET /v1/health`
    `site_finder`, or an API that does not report it), neither the Market bar nor the Menu lists it,
    and the page still answers by URL."""
    health = {k: v for k, v in HEALTH.items() if k != "site_finder"}
    if finder is not None:
        health["site_finder"] = finder
    lists.responses["/v1/health"] = (200, health)
    with TestClient(web_app) as client:
        html = client.get("/proposals").text
    links = _links(_bar_items(html))
    assert [(href, text) for href, _, text in links] == [p for p in MARKET if p[0] != "/site-finder"]
    assert 'href="/site-finder"' not in _header(html)


@pytest.mark.parametrize(
    ("path", "bar"),
    [
        ("/about", ABOUT),
        ("/methodology", ABOUT),
        ("/attribution", ABOUT),
        ("/search", None),
        ("/alerts", None),
        ("/login", None),
        ("/pricing", None),
        ("/privacy", None),
        ("/submit", None),
    ],
)
def test_reference_pages_share_the_about_bar_and_utilities_have_none(
    lists: FakeTransport, path: str, bar: list[tuple[str, str]] | None
) -> None:
    """docs/30 §11.3: the About bar holds the reference pages; search, Alerts, the account, sign-in,
    Pricing and the legal pages have no section bar. No top-bar
    section is marked on any of them."""
    with TestClient(web_app) as client:
        html = client.get(path).text
    assert all(mark is None for _, mark, _ in _links(_top_sections(html))), path
    if bar is None:
        assert _section_bar(html) is None, path
        return
    section = _section_bar(html)
    assert section is not None and section.startswith(' aria-label="About pages">'), path
    links = _links(_bar_items(html))
    assert [(href, text) for href, _, text in links] == bar, path
    assert [href for href, mark, _ in links if mark] == [path], path
    # The About bar is itself the reference: its right end carries the freshness line, not a second link.
    assert 'class="section-bar__about"' not in section


@pytest.mark.parametrize(("path", "mark"), [("/alerts", "page"), ("/proposals", None), ("/about", None)])
def test_alerts_is_marked_in_the_top_bar_and_the_menu_on_its_own_page(
    lists: FakeTransport, path: str, mark: str | None
) -> None:
    """Review of lane nav-shell (2026-10-23): with Alerts a top-bar utility rather than a section, the
    rendered `/alerts` page must still mark it, in the top bar and in the phone Menu, and no other page
    may (the macro's `/alerts/new` -> `true` is pinned below)."""
    with TestClient(web_app) as client:
        html = client.get(path).text
    link = r'<a (?:class="[^"]*" )?href="/alerts"( aria-current="([a-z]+)")?>Alerts</a>'
    alerts = re.findall(link, _header(html))
    assert len(alerts) == 2, (path, alerts)  # the top bar's link and the Menu's row
    assert [found or None for _, found in alerts] == [mark, mark], path


@pytest.mark.parametrize(
    ("switch", "cookie", "listed"),
    [("on", True, True), ("on", False, False), ("off", True, False), (None, True, False)],
)
def test_my_pipeline_is_listed_only_signed_in_with_its_switch_on(
    lists: FakeTransport, monkeypatch: pytest.MonkeyPatch, switch: str | None, cookie: bool, listed: bool
) -> None:
    """docs/30 §11.2: My pipeline is not shown to a signed-out visitor (a login wall for every one of
    them), and while `MY_PIPELINE` is off nothing names it (docs/19 §5.2). The rule is the Track block's:
    a session cookie, the switch on."""
    if switch is None:
        monkeypatch.delenv("MY_PIPELINE", raising=False)
    else:
        monkeypatch.setenv("MY_PIPELINE", switch)
    with TestClient(web_app, cookies={"session": "opaque"} if cookie else None) as client:
        html = client.get("/proposals").text
    header = _header(html)
    top = _links(_top_sections(html))
    if listed:
        assert top == [("/", "true", "Market"), ("/my", None, "My pipeline")]
        # The Menu holds it as a second group, shut, with the Tracker inside.
        menu = header.split('<nav class="site-menu__sections" aria-label="Menu">', 1)[1].split("</nav>", 1)[0]
        groups = re.findall(r'<details class="site-menu__group"( open)?>\s*<summary>([^<]+)</summary>', menu)
        assert groups == [(" open", "Market"), ("", "My pipeline")]
        assert '<li><a href="/my">Tracker</a></li>' in menu
    else:
        assert top == [("/", "true", "Market")]
        assert "My pipeline" not in header and 'href="/my"' not in header


def test_the_my_pipeline_bar_is_shown_under_its_pages_only_while_it_is_listed() -> None:
    """The section bar of `/my` comes from the same rule: listed, the Tracker bar shows under `/my` and
    `/my/items/...` with the Tracker marked; not listed, `/my` holds nothing, so a switched-off or
    signed-out `/my` draws the header of an unknown address."""
    render = templates.env.from_string(
        '{% import "partials/_sections.html" as s %}'
        '{{ s.section_of(path, live) | trim }}|{{ s.current("tracker", path) }}'
    )
    live = {"market": True, "my_pipeline": True, "funding": False, "about": True}
    assert render.render(path="/my", live=live) == 'my_pipeline| aria-current="page"'
    assert render.render(path="/my/items/itm_1/notes", live=live) == 'my_pipeline| aria-current="true"'
    assert render.render(path="/my", live={**live, "my_pipeline": False}) == '| aria-current="page"'
    assert render.render(path="/myself", live=live) == "|"


@pytest.mark.parametrize(
    ("path", "section"),
    [
        ("/", "market"),
        ("/site-finder", "market"),
        ("/proposals/gonzaga-ridge-battery-facility-tnksj5", "market"),
        ("/sites/site_1M5CV3A40ETXA6NQ2TPMWXMQV", "market"),
        ("/organizations/nextera-energy", "market"),
        ("/opportunities/opp_1", "market"),
        ("/assets/grand-coulee-us-wa", "market"),
        ("/interconnection-points/poi_1", "market"),
        ("/prices", "market"),
        ("/prices/states", "market"),
        ("/funding", ""),  # not live until lane V4 builds it
        ("/about", "about"),
        ("/methodology", "about"),
        ("/docs/api", "about"),
        ("/docs/api-keys", ""),
        ("/search", ""),
        ("/alerts", ""),
        ("/proposals-archive", ""),
        ("/texas", ""),  # a redirect to the map, never rendered
    ],
)
def test_one_section_holds_each_page(path: str, section: str) -> None:
    render = templates.env.from_string(
        '{% import "partials/_sections.html" as s %}{{ s.section_of(path, live) | trim }}'
    )
    live = {"market": True, "my_pipeline": False, "funding": False, "about": True}
    assert render.render(path=path, live=live) == section


@pytest.mark.parametrize(
    ("key", "path", "expected"),
    [
        ("projects", "/proposals", ' aria-current="page"'),
        ("projects", "/proposals/gonzaga-ridge-battery-facility-tnksj5", ' aria-current="true"'),
        ("projects", "/sites/site_1M5CV3A40ETXA6NQ2TPMWXMQV", ' aria-current="true"'),
        ("projects", "/proposals-archive", ""),
        ("infrastructure", "/assets", ' aria-current="page"'),
        ("infrastructure", "/assets/grand-coulee-us-wa", ' aria-current="true"'),
        ("infrastructure", "/interconnection-points/poi_1", ' aria-current="true"'),
        ("points", "/assets", ""),
        ("map", "/", ' aria-current="page"'),
        ("map", "/proposals", ""),
        ("prices", "/prices", ' aria-current="page"'),
        ("prices", "/prices/states", ' aria-current="true"'),
        ("about", "/about", ' aria-current="page"'),
        ("about_page", "/about", ' aria-current="page"'),
        ("methodology", "/methodology/gaps", ' aria-current="true"'),
        ("alerts", "/alerts/new", ' aria-current="true"'),
    ],
)
def test_a_link_is_marked_on_its_page_and_on_every_page_its_section_holds(
    key: str, path: str, expected: str
) -> None:
    render = templates.env.from_string(
        '{% import "partials/_sections.html" as s %}{{ s.current(key, path) }}'
    )
    assert render.render(key=key, path=path) == expected


@pytest.mark.parametrize(
    ("key", "path", "here", "expected"),
    [
        ("market", "/", "market", ' aria-current="page"'),
        ("market", "/organizations/nextera-energy", "market", ' aria-current="true"'),
        ("market", "/about", "about", ""),
        ("my_pipeline", "/my", "my_pipeline", ' aria-current="page"'),
        ("my_pipeline", "/my/items/itm_1", "my_pipeline", ' aria-current="true"'),
        ("my_pipeline", "/proposals", "market", ""),
    ],
)
def test_a_top_bar_section_is_marked_page_on_its_landing_and_true_below_it(
    key: str, path: str, here: str, expected: str
) -> None:
    render = templates.env.from_string(
        '{% import "partials/_sections.html" as s %}{{ s.section_current(key, path, here) }}'
    )
    assert render.render(key=key, path=path, here=here) == expected


def test_the_landing_link_opens_inside_the_shell_with_no_place_in_the_navigation(
    lists: FakeTransport,
) -> None:
    """PR 96's short link (`/texas`, web/headline.py) still lands on the map, where Map and Market are
    marked; the navigation names no state (owner, 2026-10-23: national and international messaging)."""
    with TestClient(web_app) as client:
        short = client.get("/texas", follow_redirects=False)
        assert short.status_code == 302 and short.headers["location"].startswith("/?jurisdiction=US-TX")
        html = client.get(short.headers["location"]).text
    assert _links(_top_sections(html)) == [("/", "page", "Market")]
    assert [(href, mark) for href, mark, _ in _links(_bar_items(html)) if mark] == [("/", "page")]
    for state in ("Texas", "ERCOT", "US-TX"):
        assert state not in _header(html), state
    footer = html.split('<footer class="site-footer">', 1)[1]
    assert "United States and Great Britain, with EU and World Bank tenders" in footer


def test_nav_state_reads_the_switch_as_my_pipeline_does(monkeypatch: pytest.MonkeyPatch) -> None:
    """`web/page.py` cannot import `web/my_projects.py` (that module imports it), so it reads
    `MY_PIPELINE` itself; the two readings must agree for every value."""
    from starlette.requests import Request

    from web.my_projects import pipeline_on
    from web.page import my_pipeline_listed

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"cookie", b"session=x")]})
    for value in ("on", "ON", " on ", "off", "", "1", "true"):
        monkeypatch.setenv("MY_PIPELINE", value)
        assert my_pipeline_listed(request) is pipeline_on(), value


def test_a_section_is_named_in_one_place() -> None:
    """A rename must be one line: the top bar, section bars, Menu, footer and list headers take the
    words from web/labels.py through the partial."""
    partial = (WEB / "templates" / "partials" / "_sections.html").read_text(encoding="utf-8")
    labels_py = (WEB / "labels.py").read_text(encoding="utf-8")
    # Section and page names live in web/labels.py (shared with the JSON-LD breadcrumbs); list headings
    # in the partial.
    names = (
        "RFPs & funding",
        "Plants, pipelines & lines",
        "Grid connection points",
        "Market intelligence",
        "My pipeline",
        "Grants & loans",
        "Coverage & methodology",
        "API & feeds",
        "Sources & licences",
    )
    for name in names:
        assert labels_py.count(f'"{name}"') == 1 and f'"{name}"' not in partial, name
    for heading in ("Projects in development", "Existing infrastructure"):
        assert partial.count(f'"{heading}"') == 1, heading
    spellings = (
        *names,
        "Projects in development",
        "Existing infrastructure",
        "RFPs &amp; funding",
        ">Projects<",
        ">Infrastructure<",
        ">Market<",
        ">Funding<",
        ">Prices<",
        "About &amp; sources",
    )
    records = ("proposal_detail.html", "site_detail.html", "opportunity_detail.html", "asset_detail.html")
    shell = ("base.html", "partials/_top_bar.html", "partials/_section_bar.html")
    pages = (*shell, *(f"{p.strip('/').replace('-', '_')}_list.html" for p in LISTS), *records)
    for page in pages:
        # What a template prints, not what its comments say about it.
        text = re.sub(r"\{#.*?#\}", "", (WEB / "templates" / page).read_text(encoding="utf-8"), flags=re.S)
        assert not [s for s in spellings if s in text], page
    # Review of lane w1-nav: record breadcrumbs kept the old names ("Home / Proposals / …" under "Projects").
    old = (">Proposals</a>", ">Opportunities</a>", ">Assets</a>")
    for page in (*records, "interconnection_point_detail.html"):
        text = (WEB / "templates" / page).read_text(encoding="utf-8")
        assert not [s for s in old if s in text], page
    for module in ("app.py", "assets_pages.py", "sites.py", "interconnection_points.py"):
        text = (WEB / module).read_text(encoding="utf-8")
        assert not [s for s in ('("Proposals", "/', '("Opportunities", "/', '("Assets", "/') if s in text], (
            module
        )


def test_favicon_ico_is_served_for_clients_that_ask_for_it_directly() -> None:
    """Review of lane w1-nav (docs/16 U-24): pages declare their icons, but feed readers, crawlers and
    the admin pages fetch /favicon.ico, which answered 404."""
    with TestClient(web_app) as client:
        response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/x-icon"
    assert response.content[:4] == b"\x00\x00\x01\x00"


def test_infrastructure_is_one_heading_over_two_tab_links(lists: FakeTransport) -> None:
    with TestClient(web_app) as client:
        pages = {path: client.get(path).text for path in ("/assets", "/interconnection-points")}
    for path, html in pages.items():
        assert _h1(html) == "Existing infrastructure", path
        tabs = html.split('<nav class="section-tabs" aria-label="Infrastructure sections">', 1)[1]
        tabs = tabs.split("</nav>", 1)[0]
        marked = "/assets" if path == "/assets" else "/interconnection-points"
        assert _links(tabs) == [
            ("/assets", "page" if marked == "/assets" else None, "Plants, pipelines &amp; lines"),
            ("/interconnection-points", "page" if marked != "/assets" else None, "Grid connection points"),
        ], path
        # Tabs are navigations between URLs, not a scripted widget (docs/16 §3.2).
        assert 'role="tab' not in html, path
        # A list reached from the nav carries no breadcrumb; the nav marks where the reader is.
        assert 'class="breadcrumbs"' not in html, path
    assert (
        "<title>Plants, pipelines &amp; lines: existing infrastructure — Inframation</title>"
        in pages["/assets"]
    )
    titled = "<title>Grid connection points: existing infrastructure — Inframation</title>"
    assert titled in pages["/interconnection-points"]


@pytest.mark.parametrize(
    ("path", "heading", "side"),
    [
        (
            "/proposals",
            "Projects in development",
            "Supply side: grid-queue requests, permits and planned plants",
        ),
        (
            "/opportunities",
            "RFPs, tenders and funding",
            "Demand side: buyers and funders asking for projects",
        ),
    ],
)
def test_the_two_record_lists_say_in_one_line_which_side_of_the_market_they_hold(
    transport: FakeTransport, path: str, heading: str, side: str
) -> None:
    with TestClient(web_app) as client:
        html = client.get(path).text
    header = html.split('<div class="page-header">', 1)[1].split("</div>", 1)[0]
    assert _h1(header) == heading
    assert f"<title>{heading} — Inframation</title>" in html
    # One paragraph under the H1 (U-17): the standfirst, ending on the intake link (IA-3).
    [standfirst] = re.findall(r"<p>(.*?)</p>", header, re.S)
    assert _text(standfirst).startswith(side)
    assert '<a href="/submit">Submit it for review</a>' in standfirst


def test_only_the_five_lists_take_the_compact_list_header(lists: FakeTransport) -> None:
    with TestClient(web_app) as client:
        for path in LISTS:
            assert '<main id="main" class="list-page">' in client.get(path).text, path
        for path in ("/", "/alerts", "/login"):
            assert '<main id="main">' in client.get(path).text, path
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert ".list-page .page-header h1 { font-size: var(--text-5);" in css


def test_about_and_sources_is_a_utility_link_not_a_section(transport: FakeTransport) -> None:
    """docs/30 §11.2: About & sources is a utility, not a top-bar section. It sits at the section bar's
    right end from 1080px, in the phone Menu and in the footer, with the reference and legal links."""
    with TestClient(web_app) as client:
        html = client.get("/proposals").text
        about = client.get("/about").text
    assert 'href="/about"' not in _top_sections(html)
    bar = _section_bar(html)
    assert bar is not None and '<a class="section-bar__about" href="/about">About &amp; sources</a>' in bar
    menu = html.split('<div class="site-menu" id="site-menu">', 1)[1].split("</header>", 1)[0]
    assert '<li><a href="/about">About &amp; sources</a></li>' in menu
    footer = html.split('<footer class="site-footer">', 1)[1]
    assert '<a href="/about">About &amp; sources</a>' in footer
    links = [href for href, _, _ in _links(footer.split("</ul>", 1)[0])]
    assert links == [
        "/about",
        "/about#sources",
        "/methodology",
        "/attribution",
        "/legal/reuse",
        "/docs/api",
        "/privacy",
    ]
    # On /about the Menu marks it, and the About bar's own first page is the current one.
    assert '<li><a href="/about" aria-current="page">About &amp; sources</a></li>' in about
    assert '<li><a href="/about" aria-current="page">About</a></li>' in _bar_items(about)
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert "@media (min-width: 1080px) { .section-bar__util { display: flex; } }" in css
    # The current page and section carry weight and a rule as well as colour (SC 1.4.1).
    marked = "{ color: var(--text); font-weight: 600; border-bottom-color: currentColor; }"
    assert f".section-bar__items a[aria-current] {marked}" in css
    marked = "{ font-weight: 600; border-bottom-color: currentColor; }"
    assert f".top-bar__sections a[aria-current], .top-bar__link[aria-current] {marked}" in css


def test_every_page_declares_the_favicon_and_its_three_files_are_served(transport: FakeTransport) -> None:
    """docs/16 §2 (U-24): `/favicon.ico` answered 404 on every page load, a console error each time."""
    with TestClient(web_app) as client:
        head = client.get("/proposals").text.split("</head>", 1)[0]
        svg = client.get("/static/img/favicon.svg")
        ico = client.get("/static/img/favicon.ico")
        touch = client.get("/static/img/apple-touch-icon.png")
    assert '<link rel="icon" href="/static/img/favicon.ico" sizes="32x32">' in head
    assert '<link rel="icon" href="/static/img/favicon.svg" type="image/svg+xml">' in head
    assert '<link rel="apple-touch-icon" href="/static/img/apple-touch-icon.png">' in head
    assert svg.status_code == ico.status_code == touch.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    # The site's palette (styles.css :root), and a variant for a dark browser theme.
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    for token in ("--color-ink", "--color-paper", "--color-copper-tint", "--color-copper-deep"):
        colour = re.search(rf"{token}: (#[0-9a-f]{{6}});", css)
        assert colour is not None and colour.group(1) in svg.text, token
    assert "@media (prefers-color-scheme: dark)" in svg.text
    # ICO: reserved 0, type 1, then 16, 32 and 48px frames.
    reserved, kind, count = struct.unpack("<HHH", ico.content[:6])
    sizes = sorted(ico.content[6 + 16 * i] for i in range(count))
    assert (reserved, kind, sizes) == (0, 1, [16, 32, 48])
    # PNG signature, then IHDR width and height.
    assert touch.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", touch.content[16:24]) == (180, 180)
