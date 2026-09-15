"""Playwright end-to-end smoke path (docs/04 E-10): public map -> list -> detail, with
attribution rendered, at desktop and 400px widths. Excluded from the strict mypy gate
(pyproject.toml) like `test_connector.py`; asserts are the point (S101 ignored per-file).

Chromium is at /opt/pw-browsers/chromium in this environment; the app is started as a real
uvicorn subprocess (Playwright drives a browser against an HTTP server, not the ASGI app
in-process) on a fixed local port.
"""

from __future__ import annotations

import functools
import http.server
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Route, sync_playwright

from services.db.session import get_engine, get_sessionmaker, init_db
from web.data_loading import load_dev_database, load_test_database

REPO_ROOT = Path(__file__).resolve().parent.parent
CHROMIUM_PATH = "/opt/pw-browsers/chromium"


def _launch_kwargs() -> dict[str, Any]:
    """The sandbox pre-installs Chromium at `CHROMIUM_PATH`; CI runs `playwright install chromium`
    and has nothing there, so it falls back to Playwright's own managed browser (first CI run,
    2026-09-15: "executable doesn't exist at /opt/pw-browsers/chromium")."""
    if os.environ.get("E2E_USE_PLAYWRIGHT_CHROMIUM") == "1":
        return {}  # reproduce the CI browser locally even where the sandbox build exists
    return {"executable_path": CHROMIUM_PATH} if os.path.exists(CHROMIUM_PATH) else {}


BASE_URL = "http://127.0.0.1:8799"
SCREENSHOT_DIR = REPO_ROOT / "web" / "screenshots"
DB_PATH = REPO_ROOT / "web" / ".data" / "e2e-test.db"
DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
NARROW_VIEWPORT = {"width": 400, "height": 850}
MAPLIBRE_VERSION = "5.24.0"  # must match templates/home_map.html
PMTILES_VERSION = "4.5.0"  # must match templates/home_map.html
BASEMAPS_VERSION = "5.7.2"  # must match templates/home_map.html
# Exercises the pmtiles basemap mode (web/README.md "Map basemap") end to end: the URL itself is
# never fetched (it only matters that it ends in `.pmtiles`, selecting that mode in
# `web/app.py::_tile_mode`), and the two CDN scripts that mode depends on are aborted below the
# same way the OSM tile host always has been, so this also proves the "keeps working if either
# script fails to load" fallback path (docs/40 §2.7 task item 1).
FAKE_PMTILES_URL = "https://tiles.example.invalid/basemap.pmtiles"

# Coordinator follow-up (2026-09-15): real end-to-end proof against an actual PMTiles archive,
# separate from the fake-URL fallback test above. Not checked into the repo -- extract one first:
#   /root/go/bin/go-pmtiles extract https://build.protomaps.com/20260914.pmtiles \
#     /tmp/bankable-e2e-pmtiles/austin.pmtiles --bbox=-98.1,30.0,-97.4,30.6 --maxzoom=10
# (a few MB, a few seconds; `go-pmtiles` is installed by the infra lane at `/root/go/bin/go-pmtiles`,
# not on PATH). `test_pmtiles_basemap_renders_real_labels_end_to_end` below is skipped, not failed,
# when this path does not exist, so the default suite stays fully offline.
PMTILES_ARCHIVE_PATH = Path(
    os.environ.get("PMTILES_TEST_ARCHIVE") or "/tmp/bankable-e2e-pmtiles/austin.pmtiles"  # noqa: S108 -- documented, overridable test fixture path
)
AUSTIN_CENTER = [-97.75, 30.3]
AUSTIN_ZOOM = 10
GLYPHS_SPRITE_HOST_PREFIX = "https://protomaps.github.io/basemaps-assets/"

# This sandbox's egress proxy resets Chromium's own TLS handshake to the CDN hosts the rendered
# page references (a proxy/browser interaction, not a product defect -- plain Python `urllib`
# through the same proxy works fine, as does `curl`). The smoke test fetches the two MapLibre
# assets once via Python and serves them to the browser from memory instead, so the check
# exercises the app's real markup without depending on this environment's browser egress.
_ASSET_CACHE: dict[str, bytes] = {}


def _fetch(url: str) -> tuple[int, bytes]:
    """`(status, body)` rather than raising on a non-2xx -- a glyph range the real Protomaps
    assets host does not carry (task follow-up: confirmed only "Noto Sans Regular"/"Medium"/
    "Italic" are hosted there, not "Bold") must reach the browser as the same 404 a live deploy
    would give it, not blow up this route handler. Retries transient network errors (a handful
    of real third-party hosts are fetched here, through this sandbox's proxy) a couple of times
    before giving up -- a real timeout/connection error still raises, since that is a genuine
    test-environment failure this function has no honest fallback body for."""
    if url not in _ASSET_CACHE:
        last_exc: OSError | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 -- pinned https/local urls
                    _ASSET_CACHE[url] = (r.status, r.read())
                break
            except urllib.error.HTTPError as exc:
                _ASSET_CACHE[url] = (exc.code, b"")
                break
            except OSError as exc:
                last_exc = exc
                time.sleep(0.5 * (attempt + 1))
        else:
            raise RuntimeError(f"failed to fetch {url} after 3 attempts") from last_exc
    return _ASSET_CACHE[url]


def _guess_content_type(url: str) -> str:
    if url.endswith(".pbf"):
        return "application/x-protobuf"
    if url.endswith(".json"):
        return "application/json"
    if url.endswith(".png"):
        return "image/png"
    if url.endswith(".js"):
        return "application/javascript"
    if url.endswith(".css"):
        return "text/css"
    return "application/octet-stream"


def _install_glyphs_and_sprite_routes(page: Any) -> None:
    """Real Protomaps-hosted glyphs/sprites (task follow-up: PMTiles mode must render basemap
    labels) hit the same sandbox TLS problem as the CDN scripts above -- fetched once via Python
    `urllib` (through the working proxy) and served back from memory, keyed by URL in the same
    `_ASSET_CACHE` `_fetch` already uses, rather than routed straight through to the real host."""

    def serve(route: Route) -> None:
        status, body = _fetch(route.request.url)
        route.fulfill(
            status=status,
            content_type=_guess_content_type(route.request.url),
            body=body,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    page.route(f"{GLYPHS_SPRITE_HOST_PREFIX}**", serve)


class _RangeCorsHandler(http.server.SimpleHTTPRequestHandler):
    """Minimal Range-capable static file server (task follow-up: stdlib `http.server` has no
    `Range` support, which `pmtiles.js`'s partial archive fetches need) with permissive CORS --
    this runs on its own local port, a different origin from the app under test in the browser's
    eyes, and `Range` is not a CORS-safelisted header so a preflight `OPTIONS` must succeed too."""

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        self._serve(body=True)

    def do_HEAD(self) -> None:
        self._serve(body=False)

    def _serve(self, *, body: bool) -> None:
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        file_size = os.path.getsize(path)
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            start_s, _, end_s = range_header[len("bytes=") :].partition("-")
            start = int(start_s) if start_s else 0
            end = min(int(end_s), file_size - 1) if end_s else file_size - 1
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if body:
                with open(path, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(length))
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if body:
                with open(path, "rb") as f:
                    self.wfile.write(f.read())

    def log_message(self, format: str, *args: object) -> None:
        pass  # keep pytest's own -s output focused on this test's prints, not access logs


def _start_range_server(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = functools.partial(_RangeCorsHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def _install_offline_routes(page: Any, *, serve_pmtiles_scripts: bool = False) -> None:
    """`serve_pmtiles_scripts=False` (default, the fallback-path smoke test): both pmtiles-mode
    CDN scripts are aborted, exercising `map.js`'s "keeps working if either script fails to load"
    path. `serve_pmtiles_scripts=True` (the real-archive proof test below): the same two scripts
    are served for real instead, via `_fetch` -- they must actually run for that test to prove
    anything."""
    js_url = f"https://cdn.jsdelivr.net/npm/maplibre-gl@{MAPLIBRE_VERSION}/dist/maplibre-gl.js"
    css_url = f"https://cdn.jsdelivr.net/npm/maplibre-gl@{MAPLIBRE_VERSION}/dist/maplibre-gl.css"
    pmtiles_js_url = f"https://cdn.jsdelivr.net/npm/pmtiles@{PMTILES_VERSION}/dist/pmtiles.js"
    basemaps_js_url = f"https://cdn.jsdelivr.net/npm/@protomaps/basemaps@{BASEMAPS_VERSION}/dist/basemaps.js"

    def serve(url: str) -> Any:
        def _handler(route: Route) -> None:
            status, body = _fetch(url)
            route.fulfill(status=status, content_type=_guess_content_type(url), body=body)

        return _handler

    page.route(js_url, serve(js_url))
    page.route(css_url, serve(css_url))
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
    if serve_pmtiles_scripts:
        page.route(pmtiles_js_url, serve(pmtiles_js_url))
        page.route(basemaps_js_url, serve(basemaps_js_url))
    else:
        # The `server` fixture runs with `MAP_TILE_URL` set to a `.pmtiles` URL (pmtiles basemap
        # mode) -- both CDN scripts that mode depends on are aborted here too, and any tile-shaped
        # host generically, so `web/static/js/map.js`'s "keeps working if either script fails to
        # load" fallback path is what this whole suite always exercises, not a separately-configured
        # raster/dev path.
        page.route(pmtiles_js_url, lambda route: route.abort())
        page.route(basemaps_js_url, lambda route: route.abort())
        page.route("**/*.pmtiles", lambda route: route.abort())
        page.route("https://*.tile.*/**", lambda route: route.abort())


def _ensure_db_loaded(db_path: Path) -> str:
    """Load the real per-source `data/normalized/*` connector output -- the full ~11,400-row set
    across all nine sources, unsampled -- through `services/ingest/loader.py` into a fresh
    file-backed SQLite database, with the dev-only preview override (web/README.md "Delayed tier")
    so today's rows are visible without waiting out the real 14/7-day lag. Returns the
    `DATABASE_URL` the app subprocess should use.

    Full data, not a sample: `services/README.md`'s "Sprint 2 fixes" closed the
    `services/api/visibility.py` query-time gap (30-75s/page unsampled) that used to make a full
    load unusable for this smoke test's navigation timeouts -- 0.127s/page and 0.35-0.53s/geo-
    request measured there on the same full load this now performs. The load step itself
    (`services/ingest/loader.py`'s row-by-row upsert, ~100 rows/second, unaffected by that fix)
    is the real cost now, at roughly two minutes for the module-scoped `server` fixture below.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    database_url = f"sqlite+pysqlite:///{db_path}"
    engine = get_engine(database_url)
    init_db(engine)
    session = get_sessionmaker(engine)()
    try:
        data_root = Path(os.environ.get("E2E_DATA_ROOT", str(REPO_ROOT / "data")))
        status = load_dev_database(session, data_root=data_root, preview=True)
        if _rows_loaded(status) == 0:
            # A fresh checkout (CI) has no `data/normalized/*` -- that tree is git-ignored, only
            # connector runs create it -- so the map would render no marker and the smoke test's
            # "cluster or point rendered" wait would time out (second CI run, 2026-09-15). The
            # committed entity-resolution fixture (`data/eval/normalized.parquet`, docs/22) is
            # loaded instead: real rows, real geometry, no network.
            load_test_database(session, sample_per_state=None, include_opportunities=False)
    finally:
        session.close()
    return database_url


def _rows_loaded(status: dict[str, Any]) -> int:
    """`load_dev_database` reports `{source_id: "loaded" | "missing" | "skipped: ..."}` per source."""
    return sum(1 for value in (status.get("sources") or {}).values() if value == "loaded")


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
    database_url = _ensure_db_loaded(DB_PATH)
    env = dict(os.environ, DATABASE_URL=database_url, WEB_DEV_PREVIEW="1", MAP_TILE_URL=FAKE_PMTILES_URL)
    env.pop("API_BASE_URL", None)  # in-process API mount, backed by the same SQLite file
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", "8799"],
        cwd=REPO_ROOT,
        env=env,
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
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            _check_desktop_and_narrow(browser)
        finally:
            browser.close()


def _check_desktop_and_narrow(browser: object) -> None:
    # ---- desktop: home/map ----
    page = browser.new_page(viewport=DESKTOP_VIEWPORT)  # type: ignore[attr-defined]
    _install_offline_routes(page)
    # docs/40 §2.7 task item 1's done-check for the fallback path: the pmtiles CDN scripts are
    # aborted (`_install_offline_routes`), so `map.js` must beacon `map.basemap_failed` exactly
    # once via `POST /api/ui-events` -- intercepted here rather than left to hit the real proxy,
    # matching the task instruction to "intercept `/api/ui-events`".
    basemap_failed_calls: list[str] = []

    def _record_ui_event(route: Route) -> None:
        post_data = route.request.post_data or ""
        if "map.basemap_failed" in post_data:
            basemap_failed_calls.append(post_data)
        route.fulfill(status=202, content_type="application/json", body="{}")

    page.route("**/api/ui-events", _record_ui_event)
    page.goto(BASE_URL + "/")
    map_container = page.locator("#map")
    assert map_container.count() == 1, "map container missing"
    assert page.get_attribute("#map", "data-tile-mode") == "pmtiles"
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
    # The pmtiles CDN scripts are aborted above (see _install_offline_routes) -- this proves the
    # same-origin fallback outline layer is what keeps the map from rendering blank.
    fallback_rendered = page.evaluate(
        "window.__map.queryRenderedFeatures({layers:['fallback-land']}).length > 0"
    )
    assert fallback_rendered, "fallback basemap layer did not render behind the (unavailable) tiles"
    page.wait_for_timeout(300)  # let the sendBeacon's fulfilled route above finish recording
    assert len(basemap_failed_calls) == 1, (
        f"expected map.basemap_failed beaconed exactly once, got {len(basemap_failed_calls)}"
    )
    page.screenshot(path=str(SCREENSHOT_DIR / "home-desktop.png"), full_page=True)

    # ---- desktop: existing-plants context layer toggle (task item 2) ----
    plants_checkbox = page.locator("#mf-layer-plants")
    assert plants_checkbox.count() == 1, "existing-plants layer checkbox missing"
    plants_checkbox.check()
    page.wait_for_function("() => window.__map.getLayoutProperty('plant-points', 'visibility') === 'visible'")
    assert "layers=plants" in page.url
    plants_checkbox.uncheck()

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


# ============================================================================================
# Real-archive proof (coordinator follow-up, 2026-09-15): the fake-URL test above proves the
# *fallback* path (both CDN scripts blocked); this proves pmtiles mode actually renders a real
# basemap, with real glyphs, end to end. Its own DB (a 5-row sample, not the full load above) and
# its own uvicorn subprocess/port, both module-scoped so the whole class of tests here pays the
# archive-serving setup cost once.
_PMTILES_PROOF_DB_PATH = REPO_ROOT / "web" / ".data" / "pmtiles-proof-test.db"
_PMTILES_PROOF_PORT = 8798
_PMTILES_PROOF_BASE_URL = f"http://127.0.0.1:{_PMTILES_PROOF_PORT}"


@pytest.fixture(scope="module")
def pmtiles_range_server() -> object:
    httpd, port = _start_range_server(PMTILES_ARCHIVE_PATH.parent)
    try:
        yield port
    finally:
        httpd.shutdown()


@pytest.fixture(scope="module")
def pmtiles_proof_server(pmtiles_range_server: int) -> object:
    _PMTILES_PROOF_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PMTILES_PROOF_DB_PATH.unlink(missing_ok=True)
    database_url = f"sqlite+pysqlite:///{_PMTILES_PROOF_DB_PATH}"
    engine = get_engine(database_url)
    init_db(engine)
    session = get_sessionmaker(engine)()
    try:
        # A small sample, not the full ~11,400-row set: this test proves the basemap, not the
        # data layer (already proven above) -- the full load's ~2 minutes would be paid for
        # nothing this test asserts on.
        load_dev_database(session, preview=True, sample_per_state=5)
    finally:
        session.close()

    tile_url = f"http://127.0.0.1:{pmtiles_range_server}/{PMTILES_ARCHIVE_PATH.name}"
    env = dict(os.environ, DATABASE_URL=database_url, WEB_DEV_PREVIEW="1", MAP_TILE_URL=tile_url)
    env.pop("API_BASE_URL", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", "8798"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_server(_PMTILES_PROOF_BASE_URL + "/health")
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.skipif(
    not PMTILES_ARCHIVE_PATH.exists(),
    reason=(
        f"real PMTiles archive not present at {PMTILES_ARCHIVE_PATH} -- extract one first: "
        "/root/go/bin/go-pmtiles extract https://build.protomaps.com/20260914.pmtiles "
        f"{PMTILES_ARCHIVE_PATH} --bbox=-98.1,30.0,-97.4,30.6 --maxzoom=10"
    ),
)
def test_pmtiles_basemap_renders_real_labels_end_to_end(pmtiles_proof_server: object) -> None:
    """Coordinator follow-up (2026-09-15): closes the "basemap glyph/sprite gap not done" item
    and proves pmtiles mode end to end against a real, small (~1.3 MB) archive -- not merely that
    the fallback survives the mode being unreachable (the test above), but that the real thing
    renders: a real vector tile fetched over HTTP Range, real glyphs from the real Protomaps
    assets host, real basemap features query-able back out of MapLibre.
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page, serve_pmtiles_scripts=True)
            _install_glyphs_and_sprite_routes(page)

            tile_range_requests: list[int] = []
            glyph_statuses: list[int] = []
            basemap_failed_calls: list[str] = []

            def _on_response(response: Any) -> None:
                url = response.url
                if url.endswith(PMTILES_ARCHIVE_PATH.name):
                    tile_range_requests.append(response.status)
                elif url.startswith(GLYPHS_SPRITE_HOST_PREFIX) and url.endswith(".pbf"):
                    glyph_statuses.append(response.status)

            page.on("response", _on_response)

            def _record_ui_event(route: Route) -> None:
                post_data = route.request.post_data or ""
                if "map.basemap_failed" in post_data:
                    basemap_failed_calls.append(post_data)
                route.fulfill(status=202, content_type="application/json", body="{}")

            page.route("**/api/ui-events", _record_ui_event)

            page.goto(_PMTILES_PROOF_BASE_URL + "/")
            assert page.get_attribute("#map", "data-tile-mode") == "pmtiles"
            page.wait_for_selector("#map canvas", timeout=10000)
            page.wait_for_function("() => window.__map && window.__map.isStyleLoaded()", timeout=15000)
            # (a) MapLibre `load` fired and the first render settled -- polled rather than read at
            # one instant, since `loaded()` is false whenever any tile or glyph is still in flight
            # (it flickered false under CPU load from a parallel data reload, 2026-09-15).
            page.wait_for_function("() => window.__map.loaded()", timeout=30000)

            # Zoom to the extracted archive's own coverage (Austin, TX, up to zoom 10) -- the
            # world-view default the page loads at is below the archive's data.
            page.evaluate(f"() => window.__map.jumpTo({{center: {AUSTIN_CENTER}, zoom: {AUSTIN_ZOOM}}})")
            page.wait_for_timeout(1500)  # let the range-fetched tile and glyphs finish rendering

            # (b) basemap source is pmtiles://... and at least one tile request returned 206
            source_url = page.evaluate("() => window.__map.getSource('protomaps').serialize().url")
            assert source_url.startswith("pmtiles://"), source_url
            assert 206 in tile_range_requests, f"no 206 Range response for the archive: {tile_range_requests}"

            # (c) real basemap features rendered from the archive at zoom 10 over Austin
            basemap_feature_count = page.evaluate(
                "() => window.__map.queryRenderedFeatures("
                "{layers: ['water', 'roads_minor', 'roads_major', 'earth', 'buildings']}"
                ").length"
            )
            assert basemap_feature_count > 0, "no basemap features rendered from the real archive"

            # (d) a symbol layer rendered text -- a glyph .pbf request returned 200
            assert 200 in glyph_statuses, f"no glyph request returned 200: {glyph_statuses}"

            # (e) no basemap_failed beacon -- the real thing worked, nothing to report as failed
            page.wait_for_timeout(300)
            assert basemap_failed_calls == [], basemap_failed_calls

            page.screenshot(path=str(SCREENSHOT_DIR / "pmtiles-austin-real.png"), full_page=True)
        finally:
            browser.close()
