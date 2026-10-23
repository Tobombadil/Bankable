"""The site finder page (`web/site_finder.py`) over the real API in process, on the site finder's
fixture world (`services/site_finder/testing.py`): the page renders the bands with their facts and
sources, the criteria round-trip through the URL (snapped to what the API accepts, only non-defaults
written), the headings sort, a band's drawer opens from its URL and from the fragment, the map
proxies forward only what they should, and the nav names the page after Map."""

from __future__ import annotations

import pathlib
import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import QueryParams

from services.api import site_finder as api_site_finder
from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.models import SiteCandidate, SiteFinderRun
from services.db.session import get_engine, get_sessionmaker, init_db
from services.site_finder.compute import refresh
from services.site_finder.testing import FakeSampler, World, seed
from web import site_finder as page
from web.api_client import ApiClient
from web.app import app as web_app
from web.page import templates


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def factory(tmp_path: pathlib.Path) -> Iterator[sessionmaker[Session]]:
    engine = get_engine(f"sqlite+pysqlite:///{tmp_path / 'sf-web.db'}")
    init_db(engine)
    yield get_sessionmaker(engine)
    engine.dispose()


@pytest.fixture()
def world(factory: sessionmaker[Session], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> World:
    with factory() as db:
        w = seed(db, tmp_path / "data")
    refresh(factory, w.data_root, sampler=FakeSampler())
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(w.data_root))
    api_site_finder.clear_caches()
    api_site_finder.clear_caches()
    return w


@pytest.fixture()
def client(factory: sessionmaker[Session], world: World) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        s = factory()
        try:
            yield s
        finally:
            s.close()

    api_app.dependency_overrides[get_db] = _override
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app, follow_redirects=False) as c:
        yield c
    api_app.dependency_overrides.clear()
    web_app.state.__dict__.pop("api_client", None)


def _rows(html: str) -> list[str]:
    """The band names the results table lists, in order."""
    body = html.split('<table class="record-table record-table--compact sf-table">', 1)[1].split(
        "</table>", 1
    )[0]
    return re.findall(r'class="row-link sf-band-link" href="[^"]*" data-band="[^"]*"[^>]*>([^<]+)</a>', body)


def _band_ids(html: str) -> list[str]:
    return re.findall(r'<tr data-band="([^"]+)"', html)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


# ----------------------------------------------------------------------------------- the page
def test_the_page_lists_the_bands_that_match_with_the_map_and_its_credits(client: TestClient) -> None:
    resp = client.get("/site-finder")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert '<h1 class="sf-intro__title">Site finder</h1>' in html
    # Bravo (345 kV) before Alpha (138 kV) under the default sort; Charlie states no voltage, Delta is
    # under 69 kV.
    assert _rows(html) == ["Bravo 345 kV", "Alpha 138 kV"]
    text = _text(html)
    assert "2 bands along 2.1 miles of line match, sorted by voltage, largest first." in text
    # Each row in the reader's words: voltage, band length, nearest substation, floodplain, county,
    # retiring MW.
    alpha = html.split('data-band="' + _band_ids(html)[1] + '"', 1)[1].split("</tr>", 1)[0]
    assert "138 kV" in alpha and "1.5 mi" in alpha and "1.0 mi" in alpha
    assert "Partly in, cut out" in alpha and "Fixture County, TX" in alpha and "500 MW" in alpha
    # The map, its key and the credits: OpenStreetMap's notice and its licence, FEMA, the Census.
    assert 'id="sf-map"' in html and "/static/js/site_finder.js" in html and "/static/js/basemap.js" in html
    assert "&copy; OpenStreetMap contributors" in html and "Open Database License" in html
    assert "FEMA National Flood Hazard Layer" in html and "TIGER/Line" in html
    assert "Sources of these rows:" in html
    # No band is open: the drawer is in the page, hidden.
    assert re.search(r'id="sf-drawer" role="dialog" aria-label="Band details" hidden', html)


def test_the_nav_names_the_site_finder_after_map_and_marks_it_here(client: TestClient) -> None:
    html = client.get("/site-finder").text
    # The Market section bar (Navigation v2, docs/30 §11.3): Map, then the site finder.
    nav = html.split('<ul class="section-bar__items">', 1)[1].split("</ul>", 1)[0]
    links = re.findall(r'<a href="([^"]+)"(?: aria-current="([a-z]+)")?>([^<]*)</a>', nav)
    assert [(h, t) for h, _, t in links[:2]] == [("/", "Map"), ("/site-finder", "Site finder")]
    assert {h: c for h, c, _ in links if c} == {"/site-finder": "page"}
    render = templates.env.from_string(
        '{% import "partials/_sections.html" as s %}{{ s.current(key, path) }}'
    )
    assert render.render(key="site_finder", path="/site-finder") == ' aria-current="page"'
    assert render.render(key="site_finder", path="/") == ""


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", ["Bravo 345 kV", "Alpha 138 kV"]),
        ("min_kv=230", ["Bravo 345 kV"]),
        # 200 snaps down to the 138 kV floor, which keeps Alpha.
        ("min_kv=200", ["Bravo 345 kV", "Alpha 138 kV"]),
        ("max_substation_mi=1.2", ["Alpha 138 kV"]),
        (
            "include_unknown_voltage=true&exclude_flood=false",
            ["Bravo 345 kV", "Alpha 138 kV", "Charlie (voltage not stated)"],
        ),
        (
            "include_unknown_voltage=true&exclude_flood=false&exclude_protected=true",
            ["Bravo 345 kV", "Alpha 138 kV"],
        ),
        ("state=TX", ["Bravo 345 kV", "Alpha 138 kV"]),
        ("state=OK", []),
        ("county=48999", ["Bravo 345 kV", "Alpha 138 kV"]),
    ],
)
def test_each_criterion_filters_the_list(client: TestClient, query: str, expected: list[str]) -> None:
    html = client.get("/site-finder?" + query).text
    assert (_rows(html) if expected else []) == expected
    if not expected:
        assert "No band meets every criterion." in html


def test_the_headings_sort_and_say_how(client: TestClient) -> None:
    html = client.get("/site-finder?sort=substation_mi").text
    assert _rows(html) == ["Alpha 138 kV", "Bravo 345 kV"]
    th = re.search(
        r'<th scope="col" class="sortable num is-sorted" aria-sort="ascending">'
        r'<a class="sort-link" href="([^"]+)"',
        html,
    )
    assert th is not None
    # A second click on the sorted heading reverses it; the criteria stay in the link.
    assert dict(parse_qsl(urlsplit(th.group(1).replace("&amp;", "&")).query)) == {"sort": "-substation_mi"}
    reversed_html = client.get("/site-finder?sort=-substation_mi&min_kv=115").text
    assert _rows(reversed_html) == ["Bravo 345 kV", "Alpha 138 kV"]
    hrefs = re.findall(r'class="sort-link" href="([^"]+)"', reversed_html)
    assert hrefs and all("min_kv=115" in h for h in hrefs)
    # The longest band first, and an unknown sort falls back to the default.
    assert _rows(client.get("/site-finder?sort=-length_mi").text) == ["Alpha 138 kV", "Bravo 345 kV"]
    assert _rows(client.get("/site-finder?sort=-score").text) == ["Bravo 345 kV", "Alpha 138 kV"]


def test_pages_of_results_walk_the_list(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(page, "PAGE_SIZE", 1)
    first = client.get("/site-finder").text
    assert _rows(first) == ["Bravo 345 kV"]
    nxt = re.search(r'<a href="([^"]+)" data-sf-page>Next 1 bands', first)
    assert nxt is not None
    second = client.get(nxt.group(1).replace("&amp;", "&")).text
    assert _rows(second) == ["Alpha 138 kV"]
    assert '<a href="/site-finder" data-sf-page>&larr; First page</a>' in second
    # A cursor that no longer fits (another sort) starts again at the first page instead of failing.
    stale = client.get(nxt.group(1).replace("&amp;", "&") + "&sort=substation_mi")
    assert stale.status_code == 200 and _rows(stale.text) == ["Alpha 138 kV"]


# ------------------------------------------------------------------------------ URL round trip
@pytest.mark.parametrize(
    ("query", "criteria", "canonical"),
    [
        ("", page.DEFAULTS, ""),
        (
            "min_kv=120&adjacent_mi=0.8&max_substation_mi=7.3",
            page.Criteria(min_kv=115, adjacent_mi=1.0, max_substation_mi=7.5),
            "min_kv=115&max_substation_mi=7.5",
        ),
        (
            "min_kv=20&adjacent_mi=9&max_substation_mi=40",
            page.Criteria(min_kv=69, adjacent_mi=2.0, max_substation_mi=10.0),
            "adjacent_mi=2&max_substation_mi=10",
        ),
        (
            "adjacent_mi=0.25&max_substation_mi=0.2",
            page.Criteria(adjacent_mi=0.25, max_substation_mi=1.0),
            "adjacent_mi=0.25&max_substation_mi=1",
        ),
        # The form without scripts: each box's hidden `false`, then `true` where ticked.
        (
            "exclude_flood=false&include_unknown_voltage=false&include_unknown_voltage=true&exclude_protected=false",
            page.Criteria(exclude_flood=False, include_unknown_voltage=True),
            "exclude_flood=false&include_unknown_voltage=true",
        ),
        ("exclude_flood=false&exclude_flood=true", page.DEFAULTS, ""),
        (
            "state=us-tx&county=48453&sort=-retiring_mw",
            page.Criteria(state="TX", county="48453", sort="-retiring_mw"),
            "state=TX&county=48453&sort=-retiring_mw",
        ),
        ("state=Texas&county=Travis&sort=bogus&min_kv=abc", page.DEFAULTS, ""),
    ],
)
def test_criteria_snap_and_round_trip_through_the_url(
    query: str, criteria: page.Criteria, canonical: str
) -> None:
    read = page.read_criteria(QueryParams(query))
    assert read == criteria
    assert page.Criteria.href(read) == "/site-finder" + ("?" + canonical if canonical else "")
    # What the page writes, read back, is the same view.
    assert page.read_criteria(QueryParams(canonical)) == read


def test_the_form_shows_the_criteria_it_was_given(client: TestClient) -> None:
    html = client.get(
        "/site-finder?min_kv=345&adjacent_mi=0.5&max_substation_mi=3&exclude_flood=false&state=TX"
    ).text
    assert '<option value="345" selected>345 kV</option>' in html
    assert re.search(r'id="sf-adjacent" type="range" name="adjacent_mi"[^>]*value="0.5"', html)
    assert re.search(r'id="sf-substation" type="range" name="max_substation_mi"[^>]*value="3"', html)
    assert re.search(
        r'id="sf-flood" type="checkbox" name="exclude_flood" value="true" data-default="checked">', html
    )
    assert '<option value="TX" selected>Texas</option>' in html
    assert '<option value="48999" data-state="TX">Fixture County</option>' in html
    # The drawer's close link and every row link keep the same criteria.
    close = re.search(r'id="sf-drawer-close"', html)
    assert close is not None
    row_links = re.findall(r'class="row-link sf-band-link" href="([^"]+)"', html)
    for href in row_links:
        q = dict(parse_qsl(urlsplit(href.replace("&amp;", "&")).query))
        assert (
            q["min_kv"] == "345"
            and q["adjacent_mi"] == "0.5"
            and q["exclude_flood"] == "false"
            and "band" in q
        )


# ---------------------------------------------------------------------------------- the drawer
def test_a_band_opens_from_its_url_with_each_fact_its_source_and_its_links(client: TestClient) -> None:
    listing = client.get("/site-finder").text
    alpha_id = _band_ids(listing)[1]
    html = client.get(f"/site-finder?band={alpha_id}").text
    assert 'class="map-drawer sf-drawer is-open" id="sf-drawer"' in html
    assert 'class="is-selected"' in html and 'aria-current="true">Alpha 138 kV' in html
    drawer = html.split('id="sf-drawer-body">', 1)[1].split("</section>", 1)[0]
    text = _text(drawer)
    assert "Alpha 138 kV" in text and "138 kV line" in text
    assert "Alpha North" in text and "1.00 mi from the line" in text
    assert "Partly in the floodplain" in text and "FEMA" in text
    assert "Fixture County, Texas" in text
    assert "500 MW" in text and "retired" in text
    # Every fact's source line names its register and when it was retrieved.
    assert drawer.count('class="sf-fact__source"') >= 4 and "retrieved" in text
    # And the drawer carries the credits of the registers behind this band, wherever it opens.
    assert (
        "Sources of these rows:" in drawer and "OpenStreetMap" in drawer.split("Sources of these rows:", 1)[1]
    )
    for host in (
        "https://msc.fema.gov/portal/search?AddressQuery=",
        "https://www.google.com/maps/search/?api=1&amp;query=",
        "https://www.openstreetmap.org/way/",
        "https://data.census.gov/profile?g=050XX00US48999",
    ):
        assert host in drawer, host
    # The prospecting layers and the screening wording.
    assert "Prospecting layers" in text and "not a site assessment" in text
    # The map gets the band's own outline, floodplain cut out.
    assert '<script type="application/json" class="sf-band-map">' in drawer


def test_the_drawer_fragment_answers_a_click_and_says_when_a_band_no_longer_matches(
    client: TestClient,
) -> None:
    alpha_id = _band_ids(client.get("/site-finder").text)[1]
    frag = client.get(f"/api/site-finder/band?band={alpha_id}")
    assert frag.status_code == 200 and "Alpha North" in frag.text and "<html" not in frag.text
    gone = client.get(f"/api/site-finder/band?band={alpha_id}&min_kv=230")
    assert gone.status_code == 200 and "This band no longer matches" in gone.text
    assert client.get("/api/site-finder/band?band=not-a-band").status_code == 400
    page_gone = client.get(f"/site-finder?band={alpha_id}&min_kv=230").text
    assert "This band no longer matches" in page_gone and _rows(page_gone) == ["Bravo 345 kV"]


def test_the_rows_fragment_is_the_list_the_page_renders(client: TestClient) -> None:
    frag = client.get("/api/site-finder/rows?min_kv=230&sort=-length_mi")
    assert frag.status_code == 200 and "<html" not in frag.text
    assert _rows(frag.text) == ["Bravo 345 kV"]
    assert "1 band along 0.6 miles of line matches." in _text(frag.text)


# ------------------------------------------------------------------------------- the proxies
def test_the_map_proxies_forward_the_criteria_and_the_view(client: TestClient) -> None:
    geo = client.get("/api/site-finder/geo", params={"bbox": "-100,29,-96,33", "zoom": "8", "min_kv": "230"})
    assert geo.status_code == 200
    body = geo.json()
    names = sorted(f["properties"]["name"] for f in body["data"]["features"])
    assert names == ["Bravo 345 kV"]
    assert body["meta"]["site_finder"]["criteria"]["min_kv"] == 230
    # Zoom 12: at state-wide zooms a line shorter than a few pixels is counted, not drawn.
    lines = client.get(
        "/api/site-finder/grid", params={"layer": "lines", "bbox": "-100,29,-96,33", "zoom": "12"}
    )
    assert lines.status_code == 200
    drawn = {
        f["properties"]["name"]
        for f in lines.json()["data"]["features"]
        if f["properties"].get("feature_kind") == "asset_line"
    }
    # Lines of 69 kV and up, and the one that states no voltage; never the 34.5 kV line.
    assert {"Alpha 138 kV", "Bravo 345 kV", "Charlie (voltage not stated)"} <= drawn
    assert not any("34.5" in n for n in drawn)
    subs = client.get(
        "/api/site-finder/grid", params={"layer": "substations", "bbox": "-100,29,-96,33", "zoom": "9"}
    )
    assert subs.status_code == 200
    assert (
        client.get(
            "/api/site-finder/grid", params={"layer": "plants", "bbox": "-100,29,-96,33", "zoom": "9"}
        ).status_code
        == 400
    )
    assert client.get("/api/site-finder/grid", params={"layer": "lines"}).status_code == 400


# ------------------------------------------------------------------------------ the edge cases
def test_before_any_run_the_page_says_so(client: TestClient, factory: sessionmaker[Session]) -> None:
    with factory() as db:
        db.execute(sa.delete(SiteCandidate))
        db.execute(sa.delete(SiteFinderRun))
        db.commit()
    api_site_finder.clear_caches()
    html = client.get("/site-finder").text
    assert "The site finder has not been computed yet." in html
    assert "No band meets every criterion." not in html


def test_an_api_failure_is_the_unavailable_page(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from web.api_client import ApiError

    def boom(*_: Any, **__: Any) -> Any:
        raise ApiError(500, {"title": "boom"})

    monkeypatch.setattr(ApiClient, "get", boom)
    assert client.get("/site-finder").status_code == 503
    assert client.get("/api/site-finder/rows").status_code == 503


def test_view_models_never_print_none() -> None:
    band = {
        "id": "asset_X.0-0",
        "line": {"name": None, "voltage_kv": None, "voltage_unknown": True, "voltage_class": "unknown"},
        "substation": None,
        "flood": {"status": "unavailable", "reason_label": None},
        "place": {},
        "context": {},
        "prospect": {"layers": [{"layer": "solar_ghi", "title": "Solar", "status": "withheld"}]},
        "links": {},
        "provenance": [],
    }
    row = page.row_view(band, page.DEFAULTS)
    assert (
        row["name"] == "Unnamed line"
        and row["voltage_label"] == "Not stated"
        and row["flood_label"] == "No flood data"
    )
    view = page.band_view(band)
    assert view["voltage"] == "Voltage not stated" and view["substation"] is None
    assert view["layers"][0]["value"].startswith("Not shown")
    rendered = templates.get_template("partials/_site_finder_band.html").render(band=view, band_missing=False)
    assert "None" not in _text(rendered)


# ------------------------------------------------------------------------- fix round (2026-10-23)
def _place_options(html: str) -> tuple[list[str], list[str], str]:
    """The state and county values the rows fragment carries, and its counties-need-state flag."""
    m = re.search(
        r'<template id="sf-place-options" data-counties-need-state="(\w+)">(.*?)</template>', html, re.S
    )
    assert m is not None, "the fragment carries no place choices"
    need, body = m.group(1), m.group(2)
    state_sel = body.split('data-for="sf-county"', 1)[0]
    county_sel = body.split('data-for="sf-county"', 1)[1]
    return (
        re.findall(r'<option value="(\w*)"', state_sel),
        re.findall(r'<option value="(\w*)"', county_sel),
        need,
    )


def test_the_rows_fragment_brings_the_place_choices_for_the_new_criteria(client: TestClient) -> None:
    """A county a looser criterion brings in is offered after the change, without a reload: the page
    lists only Fixture County at the defaults; with unknown voltages and the floodplain in, Charlie's
    county (named by the 1:20m counties) is offered too."""
    page_html = client.get("/site-finder").text
    county_select = page_html.split('id="sf-county"', 1)[1].split("</select>", 1)[0]
    assert re.findall(r'<option value="(\w+)"', county_select) == ["48999"]
    states, counties, need = _place_options(client.get("/api/site-finder/rows").text)
    assert (states, counties, need) == (["", "TX"], ["", "48999"], "false")
    _, wider, _ = _place_options(
        client.get("/api/site-finder/rows?include_unknown_voltage=true&exclude_flood=false").text
    )
    assert "48999" in wider and len([c for c in wider if c]) == 2


def test_across_many_counties_the_county_picker_asks_for_a_state(
    client: TestClient, factory: sessionmaker[Session], world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the API's county cap with no state chosen (a national run), the picker asks for a state;
    the cap is lowered here so the fixture's two counties pass it."""
    monkeypatch.setattr(api_site_finder, "COUNTY_FACET_CAP", 1)
    with factory() as db:
        db.execute(
            sa.update(SiteCandidate)
            .where(SiteCandidate.line_asset_id == world.line_c.id)
            .values(state_code="US-OK", county_fips="40109", county_name="Oklahoma County")
        )
        db.commit()
    api_site_finder.clear_caches()
    query = "include_unknown_voltage=true&exclude_flood=false"
    html = client.get(f"/site-finder?{query}").text
    county_select = html.split('id="sf-county"', 1)[1].split("</select>", 1)[0]
    assert 'aria-describedby="sf-county-hint"' in county_select
    assert '<option value="">Choose a state first</option>' in county_select
    assert "Choose a state to list its counties." in html
    states, counties, need = _place_options(client.get(f"/api/site-finder/rows?{query}").text)
    assert states == ["", "OK", "TX"] and counties == [""] and need == "true"
    _, ok_counties, ok_need = _place_options(client.get(f"/api/site-finder/rows?{query}&state=OK").text)
    assert ok_counties == ["", "40109"] and ok_need == "false"


def test_the_drawer_measures_from_the_line_and_counts_the_queue_nearby(client: TestClient) -> None:
    bravo_id = _band_ids(client.get("/site-finder").text)[0]
    text = _text(client.get(f"/api/site-finder/band?band={bravo_id}").text)
    assert "3.00 mi from the line." in text
    assert "200 MW queued within 5 mi in 1 active request" in text
    assert "the nearest request naming it is 1.00 mi from the line." in text
    assert "from the band" not in text


def test_the_key_names_each_voltage_class_by_its_range() -> None:
    html = (pathlib.Path(page.__file__).parent / "templates" / "site_finder.html").read_text()
    for words in (
        "400 kV and up",
        "288&ndash;399 kV",
        "162&ndash;287 kV",
        "100&ndash;161 kV",
        "69&ndash;99 kV",
    ):
        assert words in html
    assert "220&ndash;287" not in html


def test_the_map_proxy_passes_a_bad_zoom_back_as_a_400_not_a_500(client: TestClient) -> None:
    for zoom in ("inf", "1e999"):
        resp = client.get("/api/site-finder/geo", params={"bbox": "-100,29,-96,33", "zoom": zoom})
        assert resp.status_code == 400, (zoom, resp.status_code)


# ------------------------------------------------------------------------- land parcels (lane parcels)
def _owner_strings() -> set[str]:
    import json

    fixture = (
        pathlib.Path(__file__).resolve().parent.parent
        / "pipeline"
        / "context"
        / "fixtures"
        / "txgio_parcels"
        / "synthetic_county.geojson"
    )
    out: set[str] = set()
    for f in json.loads(fixture.read_text(encoding="utf-8"))["features"]:
        for name in ("OWNER_NAME", "NAME_CARE", "SITUS_ADDR", "MAIL_ADDR", "LEGAL_DESC"):
            v = f["properties"].get(name)
            if v and str(v).strip() and "CONFIDENTIAL" not in str(v).upper():
                out.add(str(v).strip().upper())
    return out | {"OWNER ALPHA", "OWNER DELTA", "SYNTH SITUS", "SYNTH MAIL"}


@pytest.mark.parametrize(
    ("query", "acres", "owners"),
    [
        ("min_acres=100", 100, None),
        # Snapped toward the stricter choice: at least 75 acres reads as at least 100, never 50.
        ("min_acres=75", 100, None),
        ("min_acres=150", 200, None),
        ("min_acres=5", 20, None),
        ("min_acres=5000", 1000, None),
        ("min_acres=0", None, None),
        ("min_acres=none&max_owners=any", None, None),
        # At most 2 owners reads as at most 1, never 3; 9 as 5; 10 or more is "any" (up to 10).
        ("min_acres=500&max_owners=2", 500, 1),
        ("min_acres=500&max_owners=9", 500, 5),
        ("min_acres=500&max_owners=10", 500, None),
        ("min_acres=500&max_owners=0", 500, None),
    ],
)
def test_the_land_criteria_snap_to_the_offered_choices(
    query: str, acres: int | None, owners: int | None
) -> None:
    c = page.read_criteria(QueryParams(query))
    assert (c.min_acres, c.max_owners) == (acres, owners)
    params = c.api_params()
    assert params.get("min_acres") == (str(acres) if acres else None)
    assert params.get("max_owners") == (str(owners) if owners else None)


def test_the_land_criteria_filter_the_list_and_stay_in_the_url(client: TestClient) -> None:
    html = client.get("/site-finder?min_acres=500").text
    assert _rows(html) == ["Alpha 138 kV"]
    assert '<option value="500" selected>500 acres</option>' in html
    assert 'name="max_owners"' in html and "Applies with a minimum acreage." not in html
    assert "Applies with a minimum acreage." in client.get("/site-finder").text
    links = re.findall(r'href="(/site-finder\?[^"]*band=[^"]*)"', html)
    assert links and all("min_acres=500" in link for link in links)
    none = _text(client.get("/site-finder?min_acres=1000").text)
    assert "No band meets every criterion." in none and "fewer contiguous acres or more owners" in none


def test_the_list_shows_and_sorts_by_the_largest_holding(client: TestClient) -> None:
    html = client.get("/site-finder?sort=-largest_holding_acres").text
    assert _rows(html) == ["Alpha 138 kV", "Bravo 345 kV"]
    text = _text(html)
    assert "632 ac largest holding" in text
    assert "Largest holding" in html and "largest_holding_acres" in html


def test_the_drawer_lists_the_holdings_with_their_record_links_and_names_no_owner(client: TestClient) -> None:
    import html as html_lib

    ids = _band_ids(client.get("/site-finder").text)
    alpha_id = ids[1]
    html = client.get(f"/api/site-finder/band?band={alpha_id}").text
    text = html_lib.unescape(_text(html))
    assert "Land beside the line" in text and "Largest holding 632 ac" in text
    assert "Owner names are on the appraisal district's record; Inframation does not store them." in text
    assert "118 ac 3 parcels; Agricultural open-space land; $3,949/ac; Fixture County." in text
    assert 'href="https://comptroller.texas.gov/taxes/property-tax/county-directory/fixture.php"' in html
    assert "property id 50015" in text
    assert "TxGIO StratMap Land Parcels" in text
    assert not sorted(v for v in _owner_strings() if v in html.upper())
    # A band whose county has no parcels on this server says so by name.
    every = client.get("/site-finder?include_unknown_voltage=true&exclude_flood=false").text
    charlie = next(i for i in _band_ids(every) if i not in ids)
    drawer = _text(
        client.get(
            f"/api/site-finder/band?band={charlie}&include_unknown_voltage=true&exclude_flood=false"
        ).text
    )
    assert "Parcels not loaded for" in drawer


def test_the_parcels_proxy_forwards_bbox_and_zoom_only(client: TestClient) -> None:
    assert client.get("/api/site-finder/parcels", params={"zoom": "13"}).status_code == 400
    body = client.get(
        "/api/site-finder/parcels", params={"bbox": "-98.01,30.98,-97.96,31.02", "zoom": "13", "x": "y"}
    ).json()
    assert body["meta"]["site_finder"]["parcels"]["status"] == "ok" and len(body["data"]["features"]) == 16
    far = client.get(
        "/api/site-finder/parcels", params={"bbox": "-98.01,30.98,-97.96,31.02", "zoom": "10"}
    ).json()
    assert far["data"]["features"] == []


def test_the_page_credits_the_parcels_and_its_key_names_them(client: TestClient) -> None:
    html = client.get("/site-finder").text
    assert "Land parcels: Texas Geographic Information Office (TWDB), StratMap Land Parcels" in html
    assert "Land parcel, Texas (from zoom 12)" in html
    assert '"parcels_min_zoom":12' in html


def test_the_drawer_counts_the_holdings_touching_and_those_left_out() -> None:
    land = {
        "status": "ok",
        "counties_missing": [],
        "outside_texas": [],
        "note": page.OWNER_NOTE,
        "largest_holding_acres": 632.0,
        "holdings_touching": 12,
        "holdings_touching_exact": False,
        "holdings_20_acres_plus": 9,
        "holdings_count_exact": True,
        "smaller_holdings_at_least": 2,
        "left_out_holdings_at_least": 1,
        "assembly": {},
        "top_holdings": [],
    }
    view = page.parcels_view(land)
    assert view["touching"] == "at least 12 holdings touch the band"
    assert view["left_out"] == (
        "At least 1 holding beside the band (right-of-way strips, exempt or utility land) left out of these "
        "figures."
    )
    exact = page.parcels_view({**land, "holdings_touching_exact": True, "left_out_holdings_at_least": 0})
    assert exact["touching"] == "12 holdings touch the band" and exact["left_out"] is None


def test_a_band_outside_texas_says_parcels_are_texas_only() -> None:
    view = page.parcels_view(
        {
            "status": "not_loaded",
            "counties_missing": [],
            "outside_texas": [{"fips": "22019", "name": "Calcasieu Parish"}],
        }
    )
    assert view["missing"] == "Parcels cover Texas only (not Calcasieu Parish)."
    both = page.parcels_view(
        {
            "status": "partial",
            "counties_missing": [{"fips": "48361", "name": "Orange County"}],
            "outside_texas": [{"fips": "22019", "name": "Calcasieu Parish"}],
        }
    )
    assert both["missing"] == (
        "Parcels not loaded for Orange County. Parcels cover Texas only (not Calcasieu Parish)."
    )
