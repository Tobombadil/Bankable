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
    assert "No open opportunities match these filters." in text
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
    with TestClient(web_app) as client:
        html = client.get("/?kind=load&placement=exact,region&layers=plants").text
    href = re.search(r'id="view-as-list" href="([^"]+)"', html)
    assert href is not None
    # The map's default placement is a drawing choice and layers are not proposal filters.
    assert href.group(1) == "/proposals?kind=load"
    assert re.search(r'id="view-as-map" href="/" aria-current="page"', html)


# ---- D-4: the 400px disclosures are in the markup, and inert without scripts ----


def test_every_page_has_the_menu_disclosure_and_list_pages_a_filters_button(transport: FakeTransport) -> None:
    with TestClient(web_app) as client:
        for path, form_id in (("/", "map-filters"), ("/proposals", "filters"), ("/opportunities", "filters")):
            html = client.get(path).text
            nav_toggle = (
                '<button type="button" class="nav-toggle" aria-expanded="false" '
                'aria-controls="site-nav-panel">'
            )
            assert nav_toggle in html
            assert 'id="site-nav-panel"' in html
            assert f'aria-controls="{form_id}" data-filter-toggle' in html, path
            assert re.search(rf'<form id="{form_id}" class="[^"]*" data-collapse', html), path
            assert '<script>document.documentElement.classList.add("js")</script>' in html
            assert "/static/js/site.js" in html
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    # Every collapsed rule is keyed on `.js`, so a page without scripts keeps its links and filters.
    assert ".js .site-nav-panel { display: none; }" in css
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
    for attr in ('name="description"', 'property="og:description"', 'name="twitter:description"'):
        match = re.search(rf'<meta {attr} content="([^"]*)"', html)
        assert match is not None and unescape(match.group(1)) == map_description(), attr
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


def test_the_map_page_states_source_lateness_once_in_the_masthead(late: FakeTransport) -> None:
    with TestClient(web_app) as client:
        home = client.get("/").text
        listing = client.get("/proposals").text
    # Home: no tier notice block, one short masthead link, kept below 720px on this page only.
    assert 'class="delayed-notice"' not in home
    assert home.count("behind their fetch schedule") == 1
    lag = re.search(r'<span class="masthead__lag">(.*?)</span>', home, re.S)
    assert lag is not None and '<a href="/methodology#vintage">' in lag.group(1)
    assert _text(lag.group(1)) == "1 of 2 sources behind their fetch schedule"
    assert 'class="masthead masthead--keep"' in home
    assert 'class="masthead masthead--keep"' not in listing and 'class="masthead"' in listing
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert ".masthead.masthead--keep { display: flex;" in css
    assert ".masthead--keep .masthead__ref { display: none; }" in css
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


def test_the_map_key_is_drawn_on_the_map_not_under_it(transport: FakeTransport) -> None:
    """The key sat under the canvas, below the fold at 1440x900 and 400x800. It is now in the map
    frame's overlay, under the zoom controls, with every legend inside it and a "Key" toggle."""
    with TestClient(web_app) as client:
        html = client.get("/").text
    frame = html.split('<div class="map-frame">', 1)[1].split('<div id="map"', 1)[0]
    assert '<div class="map-key" id="map-key">' in frame
    for legend in ('id="lifecycle-legend"', 'id="plants-legend"', 'id="retired-legend"'):
        assert legend in frame, legend
    assert html.index('id="zoom-in"') < html.index('id="map-key"') < html.index('id="map"')
    toggle = re.search(r'<button type="button" id="map-key-toggle"[^>]*>', frame)
    assert toggle is not None
    assert 'aria-expanded="true"' in toggle.group(0) and 'aria-controls="map-key"' in toggle.group(0)
    assert "hidden" in toggle.group(0)  # map.js shows it; without scripts the key just stays open
    # Nothing between the canvas and the attribution line any more.
    after = html.split('<div id="map"', 1)[1].split('<p class="map-attribution">', 1)[0]
    assert "legend" not in after
    # The narrow strip prints short words; the full ones stay in the accessible name.
    progress = frame.split('data-legend-family="progress"', 1)[1].split("</span></span>", 1)[0]
    assert "In process: filed, studied, permitted, under construction" in _text(progress)
    assert '<span class="legend__short" aria-hidden="true">In process' in progress


def test_the_map_key_styles_and_toggle_are_in_place() -> None:
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    map_js = (WEB / "static" / "js" / "map.js").read_text(encoding="utf-8")
    wide = css.split("@media (min-width: 720px) {\n  .map-key__panel {", 1)
    assert len(wide) == 2 and "width: 17rem;" in wide[1].split("}", 1)[0]
    narrow = css.split("@media (max-width: 719px) {\n  .map-key__panel {", 1)
    assert len(narrow) == 2 and "max-height: 9rem;" in narrow[1].split("}", 1)[0]
    panel = css.split(".map-key__panel {", 1)[1].split("}", 1)[0]
    # The page ground behind the words: every legend hue's contrast is measured on --bg.
    assert "background: var(--bg);" in panel and "pointer-events: auto;" in panel
    assert 'document.getElementById("map-key-toggle")' in map_js
    assert "mapKey.hidden = !open;" in map_js


# ---- Lane F (2026-10-10): the compact key with a layer on; the map's Filters at 720-1079px ----


def _media_block(css: str, query: str) -> str:
    """The body of the first `@media <query> {` block, up to its closing brace at column 0."""
    start = css.index(f"@media {query} {{\n")
    return css[start : css.index("\n}\n", start)]


def test_with_a_layer_on_each_layer_key_is_a_section_that_closes(transport: FakeTransport) -> None:
    """With the existing-asset or retired layer on, the key grew to the map's foot at 1440x900
    (272x554px, 23% of the canvas, Seattle to San Diego under it) and scrolled with nothing in it a
    keyboard could reach (axe `scrollable-region-focusable`, serious, at every size and theme).
    Each layer's key is now a native disclosure, open on arrival; the status key has none, so the
    no-layer key is unchanged."""
    with TestClient(web_app) as client:
        html = client.get("/?layers=plants,retired").text
    plants = html.split('id="plants-legend"', 1)[1].split('id="retired-legend"', 1)[0]
    retired = html.split('id="retired-legend"', 1)[1].split('<div id="map"', 1)[0]
    for legend, name in ((plants, "Existing assets"), (retired, "Retired &amp; retiring plants")):
        assert '<details class="map-key__section" open>' in legend, name
        assert f'<summary class="map-key__section-name">{name}</summary>' in legend
    # The section names its key once: the retired group no longer repeats it as a group name.
    assert '<span class="legend__group-name">Retired' not in retired
    lifecycle = html.split('id="lifecycle-legend"', 1)[1].split('id="plants-legend"', 1)[0]
    assert "map-key__section" not in lifecycle and "<summary" not in lifecycle
    # The panel is a group named "Map key" in every state; map.js only adds the tab stop.
    assert '<div class="map-key__panel" role="group" aria-label="Map key">' in html


def test_the_compact_key_is_smaller_than_the_no_layer_key_and_a_named_tab_stop() -> None:
    css = (WEB / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    map_js = (WEB / "static" / "js" / "map.js").read_text(encoding="utf-8")
    # The no-layer panel is as lane H built it: 17rem wide, as tall as its words.
    wide = css.split("@media (min-width: 720px) {\n  .map-key__panel {", 1)[1].split("}", 1)[0]
    assert "width: 17rem;" in wide and "max-height: calc(70vh - 2.25rem - var(--space-8));" in wide
    # Compact: 15rem at most from 720px (the no-layer panel is 276px at 1440x900), later in the
    # sheet and more specific, so it wins; the status key is the narrow strip of short words.
    compact = ".map-key--compact .map-key__panel { max-height: 15rem; }"
    no_layer = "@media (min-width: 720px) {\n  .map-key__panel {"
    assert compact in css and css.index(compact) > css.index(no_layer)
    assert ".map-key--compact .legend__short { display: inline; }" in css
    assert ".map-key--compact .legend--lifecycle > .legend__note {" in css
    assert ".map-key--compact .map-key__panel { clip-path: inset(" in css
    summary = css.split(".map-key__section > summary {", 1)[1].split("}", 1)[0]
    assert "min-height: 1.5rem;" in summary and "display:" not in summary  # keeps its triangle
    # map.js: compact, and a named tab stop, only while a layer key shows; both layer setters and
    # both before-load branches keep `aria-hidden` and the compact state in step.
    sync = map_js.split("function syncKeyCompact() {", 1)[1].split("\n  }\n", 1)[0]
    assert "var compact = !plantsLegend.hidden || !retiredLegend.hidden;" in sync
    assert 'mapKey.classList.toggle("map-key--compact", compact);' in sync
    assert 'if (compact) mapKeyPanel.setAttribute("tabindex", "0");' in sync
    assert 'else mapKeyPanel.removeAttribute("tabindex");' in sync
    for setter in ("function setRetiredLayerVisible(on) {", "function setPlantsLayerVisible(on) {"):
        assert "syncKeyCompact();" in map_js.split(setter, 1)[1].split("\n  }\n", 1)[0], setter
    for legend in ("plantsLegend", "retiredLegend"):
        before_load = (
            f"else {{ {legend}.hidden = !on; "
            f'{legend}.setAttribute("aria-hidden", on ? "false" : "true"); syncKeyCompact(); }}'
        )
        assert before_load in map_js, legend


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
