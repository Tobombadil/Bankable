"""Playwright end-to-end smoke path (docs/04 E-10): public map -> list -> detail, with
attribution rendered, at desktop and 400px widths. Excluded from the strict mypy gate
(pyproject.toml) like `test_connector.py`; asserts are the point (S101 ignored per-file).

Chromium is at /opt/pw-browsers/chromium in this environment; the app is started as a real
uvicorn subprocess (Playwright drives a browser against an HTTP server, not the ASGI app
in-process) on a fixed local port.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Route, sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
CHROMIUM_PATH = "/opt/pw-browsers/chromium"
BASE_URL = "http://127.0.0.1:8799"
SCREENSHOT_DIR = REPO_ROOT / "web" / "screenshots"
DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
NARROW_VIEWPORT = {"width": 400, "height": 850}
MAPLIBRE_VERSION = "5.24.0"  # must match templates/home_map.html

# This sandbox's egress proxy resets Chromium's own TLS handshake to the CDN hosts the rendered
# page references (a proxy/browser interaction, not a product defect -- plain Python `urllib`
# through the same proxy works fine, as does `curl`). The smoke test fetches the two MapLibre
# assets once via Python and serves them to the browser from memory instead, so the check
# exercises the app's real markup without depending on this environment's browser egress.
_ASSET_CACHE: dict[str, bytes] = {}


def _fetch(url: str) -> bytes:
    if url not in _ASSET_CACHE:
        with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 -- pinned https CDN urls
            _ASSET_CACHE[url] = r.read()
    return _ASSET_CACHE[url]


def _install_offline_routes(page: Any) -> None:
    js_url = f"https://cdn.jsdelivr.net/npm/maplibre-gl@{MAPLIBRE_VERSION}/dist/maplibre-gl.js"
    css_url = f"https://cdn.jsdelivr.net/npm/maplibre-gl@{MAPLIBRE_VERSION}/dist/maplibre-gl.css"

    def serve_js(route: Route) -> None:
        route.fulfill(status=200, content_type="application/javascript", body=_fetch(js_url))

    def serve_css(route: Route) -> None:
        route.fulfill(status=200, content_type="text/css", body=_fetch(css_url))

    page.route(js_url, serve_js)
    page.route(css_url, serve_css)
    # Fonts fall back gracefully. OSM tiles fail the same way whether aborted here or left to hit
    # the real proxy -- confirmed by testing tile.openstreetmap.org, tiles.openfreemap.org and
    # demotiles.maplibre.org directly from this Chromium build: all reset mid-handshake even
    # though plain `curl`/`urllib` through the same proxy succeed (web/README.md "Map basemap").
    # Aborting keeps the test fast rather than waiting out the real timeout; the map stays legible
    # regardless because web/static/js/map.js (a) draws a same-origin fallback outline layer
    # underneath the tile layer (web/data_ref/build_basemap_fallback.py) and (b) adds the tile
    # source only after `load` fires instead of in the initial style, since a raster source whose
    # tiles all fail otherwise keeps MapLibre from ever reaching `load` at all -- measured directly
    # against this build, not assumed.
    page.route("https://fonts.googleapis.com/**", lambda route: route.abort())
    page.route("https://tile.openstreetmap.org/**", lambda route: route.abort())


def _ensure_data_built() -> None:
    data_dir = REPO_ROOT / "web" / "static" / "data"
    if (data_dir / "proposals.geojson").exists():
        return
    from web.build_data import build_all

    build_all(
        data_dir=REPO_ROOT / "data" / "normalized",
        sources_yaml=REPO_ROOT / "data" / "sources.yaml",
        eval_parquet=None,
        out_dir=data_dir,
        no_lag=True,
    )


def _wait_for_server(url: str, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1.0)  # noqa: S310 -- localhost only
            return
        except (urllib.error.URLError, ConnectionError) as e:
            last_error = e
            time.sleep(0.3)
    raise RuntimeError(f"server did not start in time: {last_error}")


@pytest.fixture(scope="module")
def server() -> object:
    _ensure_data_built()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", "8799"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_server(BASE_URL + "/health")
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_smoke_map_list_detail_with_attribution(server: object) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        try:
            _check_desktop_and_narrow(browser)
        finally:
            browser.close()


def _check_desktop_and_narrow(browser: object) -> None:
    # ---- desktop: home/map ----
    page = browser.new_page(viewport=DESKTOP_VIEWPORT)  # type: ignore[attr-defined]
    _install_offline_routes(page)
    page.goto(BASE_URL + "/")
    map_container = page.locator("#map")
    assert map_container.count() == 1, "map container missing"
    page.wait_for_selector("#map canvas", timeout=10000)
    # docs/04 E-10: map -> cluster/marker rendered. MapLibre exposes rendered features via the
    # `window.__map` handle map.js sets for exactly this check.
    page.wait_for_function(
        "() => window.__map && window.__map.isStyleLoaded() && "
        "(window.__map.queryRenderedFeatures({layers:['clusters']}).length > 0 || "
        " window.__map.queryRenderedFeatures({layers:['points']}).length > 0)",
        timeout=15000,
    )
    assert "days delayed" in page.locator(".delayed-notice").inner_text()
    # The OSM tile layer is aborted above (see _install_offline_routes) -- this proves the
    # same-origin fallback outline layer is what keeps the map from rendering blank.
    fallback_rendered = page.evaluate(
        "window.__map.queryRenderedFeatures({layers:['fallback-land']}).length > 0"
    )
    assert fallback_rendered, "fallback basemap layer did not render behind the (unavailable) tiles"
    page.screenshot(path=str(SCREENSHOT_DIR / "home-desktop.png"), full_page=True)

    # ---- desktop: proposals list, attribution rendered ----
    page.goto(BASE_URL + "/proposals")
    page.wait_for_selector(".record-table tbody tr")
    assert page.locator(".record-table tbody tr").count() > 0
    page.screenshot(path=str(SCREENSHOT_DIR / "list-desktop.png"), full_page=True)

    # ---- desktop: proposal detail, provenance panel + attribution ----
    first_row_link = page.locator(".record-table a.row-link").first
    detail_href = first_row_link.get_attribute("href")
    first_row_link.click()
    page.wait_for_selector(".provenance-panel")
    assert page.locator(".attribution-line").count() > 0
    page.screenshot(path=str(SCREENSHOT_DIR / "detail-desktop.png"), full_page=True)

    # ---- 400px: home, list, detail ----
    narrow = browser.new_page(viewport=NARROW_VIEWPORT)  # type: ignore[attr-defined]
    _install_offline_routes(narrow)
    narrow.goto(BASE_URL + "/")
    narrow.wait_for_selector("#map canvas", timeout=10000)
    narrow.wait_for_function(
        "() => window.__map && window.__map.isStyleLoaded() && "
        "(window.__map.queryRenderedFeatures({layers:['clusters']}).length > 0 || "
        " window.__map.queryRenderedFeatures({layers:['points']}).length > 0)",
        timeout=15000,
    )
    body_width = narrow.evaluate("document.documentElement.scrollWidth")
    assert body_width <= NARROW_VIEWPORT["width"] + 1, f"horizontal overflow at 400px: {body_width}px"
    narrow.screenshot(path=str(SCREENSHOT_DIR / "home-400px.png"), full_page=True)

    narrow.goto(BASE_URL + "/proposals")
    narrow.wait_for_selector(".record-table")
    narrow.screenshot(path=str(SCREENSHOT_DIR / "list-400px.png"), full_page=True)

    assert detail_href
    narrow.goto(BASE_URL + detail_href)
    narrow.wait_for_selector(".provenance-panel")
    narrow.screenshot(path=str(SCREENSHOT_DIR / "detail-400px.png"), full_page=True)
    page.close()
    narrow.close()
