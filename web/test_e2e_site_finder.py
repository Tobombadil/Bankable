"""Playwright end-to-end for the site finder (`/site-finder`, docs/25): at 1440x900 and on a phone
(390x844, and the 360px floor), the map draws the bands, a change of criteria redraws the list and
the map and rewrites the URL, a click on a heading sorts, a click on a row opens the band's details
(beside the map from 720px, a bottom sheet below), and the URL alone brings the same view back.

Same pattern as `web/test_e2e_alerts.py`: a real uvicorn subprocess driven by Chromium, with its
own server on `E2E_SITE_FINDER_PORT`, else two below `E2E_PORT`. Its store is the site finder's
fixture world (`services/site_finder/testing.py`), computed once; `E2E_SITE_FINDER_DATABASE_URL` and
`E2E_SITE_FINDER_DATA_DIR` point it at a real store instead (the Texas build the screenshots in the
lane report came from). `E2E_SITE_FINDER_SHOTS` names a folder for the screenshots.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import sync_playwright

from web.test_e2e import E2E_PORT, _install_offline_routes, _launch_kwargs, _wait_for_server

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("E2E_SITE_FINDER_PORT") or str(E2E_PORT - 2))
BASE_URL = f"http://127.0.0.1:{PORT}"
WORK = REPO_ROOT / "web" / ".data" / "e2e-site-finder"
SHOTS = Path(os.environ["E2E_SITE_FINDER_SHOTS"]) if os.environ.get("E2E_SITE_FINDER_SHOTS") else None


def _fixture_store() -> tuple[str, Path]:
    from services.db.session import get_engine, get_sessionmaker, init_db
    from services.site_finder.compute import refresh
    from services.site_finder.testing import FakeSampler, seed

    WORK.mkdir(parents=True, exist_ok=True)
    db_path = WORK / "store.db"
    db_path.unlink(missing_ok=True)
    url = f"sqlite+pysqlite:///{db_path}"
    engine = get_engine(url)
    init_db(engine)
    factory = get_sessionmaker(engine)
    with factory() as db:
        world = seed(db, WORK / "data")
    refresh(factory, world.data_root, sampler=FakeSampler())
    engine.dispose()
    return url, world.data_root


@pytest.fixture(scope="module")
def finder_server() -> Any:
    real_url = os.environ.get("E2E_SITE_FINDER_DATABASE_URL")
    if real_url:
        database_url, data_root = real_url, Path(os.environ["E2E_SITE_FINDER_DATA_DIR"])
    else:
        database_url, data_root = _fixture_store()
    env = dict(
        os.environ,
        DATABASE_URL=database_url,
        INFRAQUE_DATA_DIR=str(data_root),
        PLATFORM_POSTURE="noncommercial",
    )
    for name in ("API_BASE_URL", "MAP_TILE_URL"):
        env.pop(name, None)
    WORK.mkdir(parents=True, exist_ok=True)
    server_log = (WORK / "server.log").open("wb")
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv; the port is an int
        [sys.executable, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=REPO_ROOT,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_server(BASE_URL + "/health", timeout_s=60.0)
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        server_log.close()


def _shot(page: Any, name: str, *, full: bool = False) -> None:
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full)


def _no_sideways_scroll(page: Any) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"page scrolls sideways by {overflow}px"


def _query(page: Any) -> dict[str, list[str]]:
    return parse_qs(urlsplit(page.url).query)


def _wait_map(page: Any) -> None:
    page.wait_for_function("() => window.__sfReady === true", timeout=30_000)


def _bands_drawn(page: Any) -> int:
    return int(page.evaluate("() => window.__sfMap.querySourceFeatures('sf-bands').length"))


def _rows(page: Any) -> list[str]:
    return [t.strip() for t in page.locator("#sf-rows .sf-band-link").all_inner_texts()]


def test_desktop_criteria_sort_drawer_and_url(finder_server: Any) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            _install_offline_routes(page)
            page.goto(BASE_URL + "/site-finder")
            _wait_map(page)
            page.wait_for_function(
                "() => window.__sfMap.querySourceFeatures('sf-bands').length > 0", timeout=30_000
            )
            first_rows = _rows(page)
            assert first_rows, "no bands listed at the defaults"
            assert page.locator(".primary-nav a[aria-current='page']").inner_text() == "Site finder"
            # The count line is in the first screen at 1440x900.
            count_top = page.locator(".sf-count").bounding_box()
            assert count_top is not None and count_top["y"] < 900, count_top
            _no_sideways_scroll(page)
            _shot(page, "desktop-01-defaults")

            # A criterion applies at once: the URL, the list and the map follow.
            with page.expect_response(lambda r: "/api/site-finder/rows" in r.url):
                page.select_option("#sf-min-kv", "230")
            page.wait_for_function("() => new URLSearchParams(location.search).get('min_kv') === '230'")
            page.wait_for_function("() => !document.getElementById('sf-rows').hasAttribute('aria-busy')")
            assert _query(page) == {"min_kv": ["230"]}
            # A higher floor never lists more bands (the page lists at most 25 either way).
            high = _rows(page)
            if high:
                assert len(high) <= len(first_rows)
            else:
                assert "No band meets every criterion." in page.inner_text("#sf-rows")

            # The floodplain box off: written to the URL, only because it differs from the default.
            with page.expect_response(lambda r: "/api/site-finder/rows" in r.url):
                page.uncheck("#sf-flood")
            page.wait_for_function(
                "() => new URLSearchParams(location.search).get('exclude_flood') === 'false'"
            )

            # A heading sorts: nearest substation first; the URL keeps the criteria.
            with page.expect_response(lambda r: "/api/site-finder/rows" in r.url):
                page.locator("th.sortable a.sort-link", has_text="Nearest substation").click()
            page.wait_for_function(
                "() => new URLSearchParams(location.search).get('sort') === 'substation_mi'"
            )
            th = page.locator("th.sortable", has_text="Nearest substation")
            page.wait_for_function("() => !!document.querySelector('th[aria-sort=\"ascending\"]')")
            assert th.get_attribute("aria-sort") == "ascending"

            # A row opens its band beside the map; the map moves to it and draws its outline.
            link = page.locator("#sf-rows .sf-band-link").first
            name = link.inner_text().strip()
            with page.expect_response(lambda r: "/api/site-finder/band" in r.url):
                link.click()
            drawer = page.locator("#sf-drawer")
            drawer.wait_for(state="visible")
            assert name in drawer.locator(".sf-band__title").inner_text()
            assert "band" in _query(page)
            for text in ("Nearest substation", "Floodplain", "County", "Look closer"):
                assert drawer.get_by_text(text, exact=False).first.is_visible(), text
            hrefs = drawer.locator(".sf-links a").evaluate_all("els => els.map(e => e.href)")
            assert any(h.startswith("https://msc.fema.gov/portal/search?AddressQuery=") for h in hrefs), hrefs
            assert any(h.startswith("https://www.google.com/maps/search/?api=1&query=") for h in hrefs), hrefs
            box = drawer.bounding_box()
            map_box = page.locator("#sf-map").bounding_box()
            assert box is not None and map_box is not None
            assert box["x"] > map_box["x"] + map_box["width"] / 2, "the drawer sits over the map's right"
            page.wait_for_function(
                "() => window.__sfMap.querySourceFeatures('sf-selected').length > 0", timeout=15_000
            )
            _no_sideways_scroll(page)
            _shot(page, "desktop-02-band-open")

            # The URL alone brings the same view back: criteria, sort and the open band.
            shared = page.url
            page.goto(shared)
            _wait_map(page)
            assert page.locator("#sf-min-kv").input_value() == "230"
            assert not page.locator("#sf-flood").is_checked()
            assert page.locator("#sf-drawer").is_visible()
            assert name in page.locator("#sf-drawer .sf-band__title").inner_text()
            assert (
                page.locator("th.sortable", has_text="Nearest substation").get_attribute("aria-sort")
                == "ascending"
            )

            # Close: the band leaves the URL; the criteria stay.
            page.locator("#sf-drawer-close").click()
            page.wait_for_function("() => !new URLSearchParams(location.search).has('band')")
            assert _query(page).get("min_kv") == ["230"]
            page.locator("#sf-drawer").wait_for(state="hidden")
        finally:
            browser.close()


@pytest.mark.parametrize(("label", "viewport"), [("phone", (390, 844)), ("floor", (360, 740))])
def test_phone_filters_list_and_bottom_sheet(
    finder_server: Any, label: str, viewport: tuple[int, int]
) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            ctx = browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]}, has_touch=True, is_mobile=True
            )
            page = ctx.new_page()
            _install_offline_routes(page)
            page.goto(BASE_URL + "/site-finder")
            _wait_map(page)
            _no_sideways_scroll(page)
            # The criteria fold behind "Filters"; the key folds under the map.
            assert page.locator("#sf-criteria").is_hidden()
            toggle = page.locator("[data-filter-toggle][aria-controls='sf-criteria']")
            assert toggle.is_visible()
            _shot(page, f"{label}-01-defaults")
            toggle.tap()
            assert page.locator("#sf-criteria").is_visible()
            _no_sideways_scroll(page)
            _shot(page, f"{label}-02-filters", full=True)
            with page.expect_response(lambda r: "/api/site-finder/rows" in r.url):
                page.locator("label[for='sf-unknown']").tap()
            page.wait_for_function(
                "() => new URLSearchParams(location.search).get('include_unknown_voltage') === 'true'"
            )
            page.locator("#sf-criteria [data-filter-done]").last.tap()
            assert page.locator("#sf-criteria").is_hidden()

            # A row opens the band as a bottom sheet over the lower part of the screen.
            link = page.locator("#sf-rows .sf-band-link").first
            link.scroll_into_view_if_needed()
            with page.expect_response(lambda r: "/api/site-finder/band" in r.url):
                link.tap()
            sheet = page.locator("#sf-drawer")
            sheet.wait_for(state="visible")
            page.wait_for_timeout(400)
            box = sheet.bounding_box()
            assert box is not None
            assert box["y"] >= viewport[1] * 0.5 - 2, box
            assert abs(box["x"]) < 1 and abs(box["width"] - viewport[0]) < 2, box
            _no_sideways_scroll(page)
            _shot(page, f"{label}-03-sheet")
            expand = page.locator("#sf-drawer-expand")
            if expand.is_visible():
                expand.tap()
                page.wait_for_timeout(300)
                full = sheet.bounding_box()
                assert full is not None and full["y"] < 2
                _shot(page, f"{label}-04-sheet-expanded")
            page.locator("#sf-drawer-close").tap()
            page.wait_for_function("() => !new URLSearchParams(location.search).has('band')")
            ctx.close()
        finally:
            browser.close()


def test_without_the_map_the_criteria_list_and_drawer_still_work(finder_server: Any) -> None:
    """MapLibre blocked: no script error, a row still opens its band, a change of criteria still
    redraws the list and refreshes the open drawer (the change handler used to throw on the missing
    map before it reached the drawer)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            _install_offline_routes(page)
            page.route("**/maplibre-gl.js", lambda route: route.abort())  # registered last, so it wins
            page.goto(BASE_URL + "/site-finder")
            page.wait_for_selector("#sf-map.is-unavailable")
            link = page.locator("#sf-rows .sf-band-link").first
            with page.expect_response(lambda r: "/api/site-finder/band" in r.url):
                link.click()
            page.locator("#sf-drawer").wait_for(state="visible")
            with page.expect_response(lambda r: "/api/site-finder/band" in r.url):
                page.select_option("#sf-min-kv", "115")
            page.wait_for_function("() => new URLSearchParams(location.search).get('min_kv') === '115'")
            page.wait_for_function("() => !document.getElementById('sf-rows').hasAttribute('aria-busy')")
            assert page.locator("#sf-drawer").is_visible()
            assert errors == [], errors
        finally:
            browser.close()


def test_a_looser_criterion_offers_the_counties_it_brings_in(finder_server: Any) -> None:
    """The county picker follows the criteria without a reload: letting in the floodplain and the
    lines with no stated voltage brings a second county, which can then be picked."""
    if os.environ.get("E2E_SITE_FINDER_DATABASE_URL"):
        pytest.skip("written for the fixture world's two counties")
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            _install_offline_routes(page)
            page.goto(BASE_URL + "/site-finder")

            def counties() -> list[str]:
                values = page.locator("#sf-county option").evaluate_all("els => els.map(e => e.value)")
                return [v for v in values if v]

            assert counties() == ["48999"]
            with page.expect_response(lambda r: "/api/site-finder/rows" in r.url):
                page.uncheck("#sf-flood")
            with page.expect_response(lambda r: "/api/site-finder/rows" in r.url):
                page.check("#sf-unknown")
            page.wait_for_function(
                "() => document.querySelectorAll('#sf-county option[value]:not([value=\"\"])').length === 2"
            )
            other = next(c for c in counties() if c != "48999")
            with page.expect_response(lambda r: "/api/site-finder/rows" in r.url):
                page.select_option("#sf-county", other)
            page.wait_for_function(f"() => new URLSearchParams(location.search).get('county') === '{other}'")
            page.wait_for_function("() => !document.getElementById('sf-rows').hasAttribute('aria-busy')")
            assert _rows(page) == ["Charlie (voltage not stated)"]
            assert page.locator("#sf-county").input_value() == other
        finally:
            browser.close()


def test_land_parcels_filter_the_list_draw_from_zoom_12_and_open_a_card(finder_server: Any) -> None:
    """Lane parcels: "Contiguous acres, at least" applies at once and stays in the URL; the drawer lists
    the holdings with the appraisal-district link and the no-owner note; from zoom 12 the parcels in
    view are drawn, and a click inside one names its acres and its holding (never an owner)."""
    if os.environ.get("E2E_SITE_FINDER_DATABASE_URL"):
        pytest.skip("written for the fixture world's parcels beside line A")
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            _install_offline_routes(page)
            page.goto(BASE_URL + "/site-finder")
            _wait_map(page)
            with page.expect_response(lambda r: "/api/site-finder/rows" in r.url):
                page.select_option("#sf-min-acres", "500")
            page.wait_for_function("() => new URLSearchParams(location.search).get('min_acres') === '500'")
            page.wait_for_function("() => !document.getElementById('sf-rows').hasAttribute('aria-busy')")
            assert _rows(page) == ["Alpha 138 kV"]
            assert page.locator("#sf-owners-hint").count() == 0
            with page.expect_response(lambda r: "/api/site-finder/band" in r.url):
                page.locator("#sf-rows .sf-band-link").first.click()
            drawer = page.locator("#sf-drawer")
            drawer.wait_for(state="visible")
            land = drawer.locator(".sf-land")
            assert "Largest holding" in land.inner_text()
            assert "Inframation does not store them." in land.inner_text()
            assert land.locator(".sf-holding").count() >= 5
            _shot(page, "desktop-03-parcels-drawer")
            # Zoom to line A at 13: the parcel outlines arrive.
            page.evaluate("() => window.__sfMap.jumpTo({center: [-97.99, 31.0], zoom: 13.2})")
            page.wait_for_function(
                "() => window.__sfMap.querySourceFeatures('sf-parcels').length > 0", timeout=30_000
            )
            page.evaluate("() => window.__sfMap.jumpTo({center: [-97.99, 31.0], zoom: 9})")
            page.wait_for_function(
                "() => window.__sfMap.querySourceFeatures('sf-parcels').length === 0", timeout=30_000
            )
            # With no band left (no band reaches 1,000 contiguous acres), a click finds the parcel beneath.
            page.locator("#sf-drawer-close").click()
            with page.expect_response(lambda r: "/api/site-finder/geo" in r.url):
                page.select_option("#sf-min-acres", "1000")
            page.wait_for_function("() => new URLSearchParams(location.search).get('min_acres') === '1000'")
            page.evaluate("() => window.__sfMap.jumpTo({center: [-97.9942, 30.9919], zoom: 14})")
            page.wait_for_function(
                "() => window.__sfMap.querySourceFeatures('sf-parcels').length > 0"
                " && window.__sfMap.querySourceFeatures('sf-bands').length === 0",
                timeout=30_000,
            )
            page.wait_for_timeout(400)
            # Inside Delta's ranch, south of line A.
            point = page.evaluate(
                "() => { const p = window.__sfMap.project([-97.9942, 30.9919]); return [p.x, p.y]; }"
            )
            box = page.locator("#sf-map").bounding_box()
            assert box is not None
            page.mouse.click(box["x"] + point[0], box["y"] + point[1])
            popup = page.locator(".sf-popup")
            popup.wait_for(state="visible", timeout=10_000)
            text = popup.inner_text()
            assert "Parcel," in text and "Holding" in text and "does not store them" in text
            assert "OWNER" not in text.upper().replace("OWNER NAMES", "")
            _shot(page, "desktop-04-parcel-card")
            assert errors == [], errors
        finally:
            browser.close()
