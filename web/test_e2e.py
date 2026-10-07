"""Playwright end-to-end smoke path (docs/04 E-10): public map -> list -> detail, with
attribution rendered, at desktop and 400px widths. Excluded from the strict mypy gate
(pyproject.toml) like `test_connector.py`; asserts are the point (S101 ignored per-file).

Chromium is at /opt/pw-browsers/chromium in this environment; the app is started as a real
uvicorn subprocess (Playwright drives a browser against an HTTP server, not the ASGI app
in-process) on a fixed local port.
"""

from __future__ import annotations

import contextlib
import functools
import gzip
import hashlib
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Route, sync_playwright

from services.db.session import get_engine, get_sessionmaker, init_db
from web.data_loading import load_dev_database, load_test_database
from web.labels import map_labels
from web.retirement import ASSET_STATUS_LABELS
from web.viewmodels import TECHNOLOGY_LABELS, technology_label

REPO_ROOT = Path(__file__).resolve().parent.parent
CHROMIUM_PATH = "/opt/pw-browsers/chromium"


def _launch_kwargs() -> dict[str, Any]:
    """The sandbox pre-installs Chromium at `CHROMIUM_PATH`; CI runs `playwright install chromium`
    and has nothing there, so it falls back to Playwright's own managed browser (first CI run,
    2026-09-15: "executable doesn't exist at /opt/pw-browsers/chromium")."""
    if os.environ.get("E2E_USE_PLAYWRIGHT_CHROMIUM") == "1":
        return {}  # reproduce the CI browser locally even where the sandbox build exists
    return {"executable_path": CHROMIUM_PATH} if os.path.exists(CHROMIUM_PATH) else {}


# Overridable so two checkouts on one machine can run this suite at once without one server
# answering the other's browser (`E2E_PORT=8811`); 8799 otherwise, as before.
E2E_PORT = int(os.environ.get("E2E_PORT") or "8799")
BASE_URL = f"http://127.0.0.1:{E2E_PORT}"
# The committed reference screenshots in web/screenshots/ are refreshed only on request
# (`E2E_REFRESH_SCREENSHOTS=1`, run against the real normalized data). Every other run,
# including CI on the eval fixture, writes to the git-ignored web/.data/ tree so a test run
# never leaves fixture-based images as unstaged changes to the reference set.
SCREENSHOT_DIR = (
    REPO_ROOT / "web" / "screenshots"
    if os.environ.get("E2E_REFRESH_SCREENSHOTS") == "1"
    else REPO_ROOT / "web" / ".data" / "screenshots"
)
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

# The rendered pages load MapLibre from a CDN. The browser never reaches it: the two pinned MapLibre
# files are served from recorded copies under tests/fixtures/cdn (audit 2026-10-07 QA-4: a live
# fetch here made CI depend on the CDN), checked against their recorded SHA-256 before serving.
# Only the opt-in PMTiles proof test (marked `network`) still fetches anything from a real host.
RECORDED_CDN_DIR = REPO_ROOT / "tests" / "fixtures" / "cdn"
RECORDED_CDN = {
    f"https://cdn.jsdelivr.net/npm/maplibre-gl@{MAPLIBRE_VERSION}/dist/maplibre-gl.js": (
        f"maplibre-gl-{MAPLIBRE_VERSION}.js.gz",
        "45a9b07a9189ce56054c620a947ccf41e291e58c95e9b61533b740aaa65ee5cb",
    ),
    f"https://cdn.jsdelivr.net/npm/maplibre-gl@{MAPLIBRE_VERSION}/dist/maplibre-gl.css": (
        f"maplibre-gl-{MAPLIBRE_VERSION}.css.gz",
        "ab1e70d59ec40465bae7e7030da2f3ccf28133fd502e62bd598eefbadfd7a732",
    ),
}
_ASSET_CACHE: dict[str, tuple[int, bytes]] = {}


@functools.cache
def _recorded(url: str) -> bytes:
    """The recorded copy of a pinned CDN file, integrity-checked. A missing or changed recording is
    an error naming the file, never a silent fallback to the network (tests/fixtures/cdn/README.md)."""
    name, sha256 = RECORDED_CDN[url]
    path = RECORDED_CDN_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"no recorded copy of {url} at {path}; see tests/fixtures/cdn/README.md")
    body = gzip.decompress(path.read_bytes())
    if hashlib.sha256(body).hexdigest() != sha256:
        raise ValueError(f"{path} does not match its recorded SHA-256; see tests/fixtures/cdn/README.md")
    return body


def _fetch(url: str) -> tuple[int, bytes]:
    """`(status, body)` rather than raising on a non-2xx -- a glyph range the real Protomaps
    assets host does not carry (task follow-up: confirmed only "Noto Sans Regular"/"Medium"/
    "Italic" are hosted there, not "Bold") must reach the browser as the same 404 a live deploy
    would give it, not blow up this route handler. Retries transient network errors (a handful
    of real third-party hosts are fetched here, through this sandbox's proxy) a couple of times
    before giving up -- a real timeout/connection error still raises, since that is a genuine
    test-environment failure this function has no honest fallback body for."""
    if url in RECORDED_CDN:
        return 200, _recorded(url)
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
            # The pages load these with `crossorigin="anonymous"` and an `integrity` hash (UX-19), so
            # the stand-in must answer as the CDN does, with CORS, or the browser refuses the file.
            route.fulfill(
                status=status,
                content_type=_guess_content_type(url),
                body=body,
                headers={"Access-Control-Allow-Origin": "*"},
            )

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
    file-backed SQLite database, with the dev-only preview override (web/README.md "Tier visibility")
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
        # TimeoutError too: under load a server that has bound its port can take longer than the 1 s
        # read timeout to answer its first /health, which is "not ready yet", not a failure (seen
        # 2026-09-26 with six test suites running on one machine).
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_error = e
            time.sleep(0.3)
    raise RuntimeError(f"server did not start in time: {last_error}")


@pytest.fixture(scope="module")
def server() -> object:
    database_url = _ensure_db_loaded(DB_PATH)
    env = dict(os.environ, DATABASE_URL=database_url, WEB_DEV_PREVIEW="1", MAP_TILE_URL=FAKE_PMTILES_URL)
    env.pop("API_BASE_URL", None)  # in-process API mount, backed by the same SQLite file
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv; the port is an int parsed above
        [sys.executable, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", str(E2E_PORT)],
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
        " window.__map.queryRenderedFeatures({layers:['points']}).length > 0 || "
        " window.__map.queryRenderedFeatures({layers:['region-fill']}).length > 0)",
        timeout=15000,
    )
    # docs/04 D-3/D-28: the tier line is always on screen and always true. Since the ISO
    # change-event delay was dropped (owner, 2026-09-21) there is no delay left to name, so the
    # line states what the page is -- live -- and what a paid plan adds.
    tier_line = page.locator(".delayed-notice").inner_text()
    assert "published as soon as it is ingested" in tier_line
    assert "days" not in tier_line, "no surface may claim a delay the product does not apply"
    assert "Alerts and API in Pro" in tier_line
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
        " window.__map.queryRenderedFeatures({layers:['points']}).length > 0 || "
        " window.__map.queryRenderedFeatures({layers:['region-fill']}).length > 0)",
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


_COUNT_READY = "() => /match these filters/.test(document.getElementById('map-result-count').textContent)"


def _geo_records(query: str) -> int:
    with urllib.request.urlopen(f"{BASE_URL}/api/proposals/geo?{query}", timeout=30) as r:  # noqa: S310 -- localhost only
        return int(json.load(r)["data"]["totals"]["records"])


def _notice_active_count(page: Any) -> int:
    match = re.search(r"Showing (\d+) active proposals", page.inner_text("#map-notice"))
    assert match, page.inner_text("#map-notice")
    return int(match.group(1))


def test_map_keeps_and_forwards_a_linked_kind_filter(server: object) -> None:
    """2026-09-29: `/?kind=load` drew every proposal (5,853) under a notice counting 46, because
    map.js forwarded four filters by hand, and `writeFilters` then rewrote the URL without `kind`.
    The page now hands map.js the passthrough names; this walks the real page: the geo request
    carries the linked filter, the count line is the API's own total for that query and agrees
    with the server notice, the URL still names the filter once map.js has rewritten it, and the
    Kind select drives the same round trip (notice included) after a change."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            page.route(
                "**/api/ui-events",
                lambda route: route.fulfill(status=202, content_type="application/json", body="{}"),
            )
            with page.expect_response(lambda r: "/api/proposals/geo?" in r.url, timeout=30000) as geo_info:
                page.goto(BASE_URL + "/?kind=load")
            page.wait_for_selector("#map canvas", timeout=10000)
            page.wait_for_function("() => window.__map && window.__map.isStyleLoaded()", timeout=15000)
            page.wait_for_function(_COUNT_READY, timeout=15000)

            geo = geo_info.value
            assert parse_qs(urlsplit(geo.url).query).get("kind") == ["load"], geo.url
            shown = int(page.inner_text("#map-result-count").split()[0])
            assert shown == geo.json()["data"]["totals"]["records"]
            # The same query asked independently of the page, and the unfiltered total beside it:
            # the count is the filtered answer, not the everything-answer it used to show.
            api_load = _geo_records("kind=load&placement=exact,region&bbox=-179,-85,179,85&zoom=3")
            api_all = _geo_records("placement=exact,region&bbox=-179,-85,179,85&zoom=3")
            assert shown == api_load
            assert api_load < api_all
            assert _notice_active_count(page) == shown  # the server breakdown says the same
            assert page.eval_on_selector("#mf-kind", "el => el.value") == "load"
            assert parse_qs(urlsplit(page.url).query).get("kind") == ["load"], page.url

            # The control: picking another kind refetches with it, rewrites the URL with it and
            # re-renders the notice for it.
            def _generation_geo(r: Any) -> bool:
                return "/api/proposals/geo?" in r.url and "kind=generation" in r.url

            with (
                page.expect_response(_generation_geo) as geo2,
                page.expect_response(lambda r: "/api/proposals/notice?" in r.url) as notice_info,
            ):
                page.select_option("#mf-kind", "generation")
            generation = geo2.value.json()["data"]["totals"]["records"]
            assert "kind=generation" in notice_info.value.url
            count_js = "document.getElementById('map-result-count').textContent"
            notice_js = "document.getElementById('map-notice').textContent"
            page.wait_for_function(f"() => {count_js}.startsWith('{generation} ')")
            page.wait_for_function(f"() => {notice_js}.includes('Showing {generation:,} active')")
            assert parse_qs(urlsplit(page.url).query).get("kind") == ["generation"], page.url
        finally:
            browser.close()


def _in_view_feature(name: str, technology: str | None, lon: float) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": f"prop_{name}",
        "geometry": {"type": "Point", "coordinates": [lon, 38.95]},
        "properties": {
            "feature_kind": "proposal",
            "public_id": f"prop_{name}",
            "name": name,
            "url": f"/proposals/{name}",
            "kind": "load" if technology == "load" else "generation",
            "technology": technology,
            "lifecycle_state": "filed",
            "capacity_mw": 12.0,
            "county_name": "Loudoun",
            "state_code": "US-VA",
            "precision": "exact",
            "provenance": [],
        },
    }


def test_map_status_control_draws_built_proposals_and_the_list_keeps_the_choice(server: object) -> None:
    """Audit 2026-10-07 UX-1: no control on the map or the list could show a built proposal. Walks
    the real page: MapLibre loads under its integrity hash (UX-19), ticking Built in the Status
    disclosure asks the geo API for the active states plus `built`, the count is the API's answer,
    the URL and the summary say so, and the List link opens the list with Built still ticked."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            page.route(
                "**/api/ui-events",
                lambda route: route.fulfill(status=202, content_type="application/json", body="{}"),
            )
            page.goto(BASE_URL + "/")
            page.wait_for_selector("#map canvas", timeout=10000)
            page.wait_for_function("() => window.__map && window.__map.isStyleLoaded()", timeout=15000)
            page.wait_for_function(_COUNT_READY, timeout=15000)
            assert page.inner_text("#mf-status [data-status-summary]") == "active"
            page.click("#mf-status summary")
            with page.expect_response(lambda r: "/api/proposals/geo?" in r.url, timeout=30000) as geo_info:
                page.check("#mf-status-built")
            states = parse_qs(urlsplit(geo_info.value.url).query)["lifecycle_state"][0].split(",")
            assert "built" in states and "announced" in states and "withdrawn" not in states
            page.wait_for_function(_COUNT_READY, timeout=15000)
            shown = int(page.inner_text("#map-result-count").split()[0].replace(",", ""))
            assert shown == geo_info.value.json()["data"]["totals"]["records"]
            assert "built" in parse_qs(urlsplit(page.url).query)["lifecycle_state"][0].split(",")
            assert page.inner_text("#mf-status [data-status-summary]") == "active, built"
            page.click("#view-as-list")
            page.wait_for_load_state("domcontentloaded")
            assert page.is_checked("#f-status-built") and page.is_checked("#f-status-announced")
            assert not page.is_checked("#f-status-withdrawn")
        finally:
            browser.close()


def test_map_in_view_list_names_a_technology_as_the_server_does(server: object) -> None:
    """Lane H7: the in-view row printed the bare token ("load · US-VA"). It now prints the label
    the proposal list prints, read from the `#map-labels` tag the server renders from
    `web/viewmodels.py::TECHNOLOGY_LABELS`; an absent one reads as a dash. The geo answer is
    fixed here so the rows do not depend on which sources are loaded."""
    features = [
        _in_view_feature("dc-one", "load", -77.5),
        _in_view_feature("solar-one", "solar", -77.4),
        _in_view_feature("blank-one", None, -77.3),
    ]
    envelope = {
        "data": {
            "type": "FeatureCollection",
            "features": features,
            "totals": {"records": 3, "clustered": False},
        },
        "meta": {},
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            page.route(
                "**/api/ui-events",
                lambda route: route.fulfill(status=202, content_type="application/json", body="{}"),
            )
            page.route(
                "**/api/proposals/geo?**",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(envelope)
                ),
            )
            page.goto(BASE_URL + "/?kind=load")
            labels = json.loads(page.inner_text("#map-labels"))
            # Every table the map prints words from (web/labels.py, audit 2026-09-30 F1).
            assert labels == map_labels()
            assert labels["technology"] == TECHNOLOGY_LABELS
            assert labels["asset_status"] == ASSET_STATUS_LABELS
            page.wait_for_function(
                "() => document.querySelectorAll('#in-view-items .meta').length === 3", timeout=15000
            )
            by_name = {
                name: page.inner_text(f"#in-view-items li:has(a[href='/proposals/{name}']) .meta")
                for name in ("dc-one", "solar-one", "blank-one")
            }
            # MW as the server prints it (web/formatting.py `mw`, UX-8): "12 MW", not "12.0 MW".
            assert by_name["dc-one"] == f"{technology_label('load')} · US-VA · 12 MW"
            assert by_name["dc-one"].startswith(TECHNOLOGY_LABELS["load"])
            assert by_name["solar-one"] == "Solar · US-VA · 12 MW"  # every token in words now
            assert by_name["blank-one"] == "— · US-VA · 12 MW"
        finally:
            browser.close()


# ============================================================================================
# 2026-09-30 audit, UX lane: words not tokens (F1/D-7), the map's keyboard path and drawer (F5,
# D-13), stale responses (F3), what "in view" announces (F4), and the report form (D-6).

#: A snake_case token in reader-visible text: `gas_cc`, `under_construction`, `us.eia.atlas`'s
#: `ethanol_plants`. Identifiers are printed in Plex Mono by rule (docs/31 §4) and licence quotes
#: are verbatim legal text, so those two are the only places a token may stand. A file name a
#: source publishes under (`ks_wells.zip`, in that register's own title) is the source's word.
_TOKEN = re.compile(r"\b[a-z0-9]+_[a-z0-9_]+\b(?!\.[a-z]{2,4}\b)")
_VISIBLE_TEXT_JS = """() => {
  const skip = (el) => el.closest('script,style,template,.mono,code,.provenance-panel__quote,[hidden]');
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (node.parentElement && !skip(node.parentElement)) out.push(node.textContent);
  }
  document.querySelectorAll('option').forEach((o) => out.push(o.textContent));
  return out.join(' ');
}"""


def test_public_pages_print_words_not_vocabulary_tokens(server: object) -> None:
    """F1 / D-7: technology, lifecycle and kind tokens (`gas_cc`, `solar_storage`,
    `under_construction`) reached filter selects, list columns, detail pages and the map. Every
    reader-visible string on the main public pages, select options included, is now free of them."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            paths = ["/", "/proposals", "/proposals?include_withdrawn=1", "/opportunities", "/methodology"]
            page.goto(BASE_URL + "/proposals")
            paths.append(page.locator("#result-rows a[href^='/proposals/']").first.get_attribute("href"))
            page.goto(BASE_URL + "/opportunities")
            opportunity = page.locator("a[href^='/opportunities/']").first
            if opportunity.count():
                paths.append(opportunity.get_attribute("href"))
            found: dict[str, list[str]] = {}
            for path in paths:
                page.goto(BASE_URL + path)
                tokens = sorted(set(_TOKEN.findall(page.evaluate(_VISIBLE_TEXT_JS))))
                if tokens:
                    found[path] = tokens
            assert not found, f"raw vocabulary tokens shown to readers: {found}"
        finally:
            browser.close()


def _geo_envelope(features: list[dict[str, Any]], *, records: int, clustered: bool = False) -> str:
    totals: dict[str, Any] = {
        "records": records,
        "clustered": clustered,
        "lifecycle_state_counts": {"filed": records},
    }
    return json.dumps(
        {"data": {"type": "FeatureCollection", "features": features, "totals": totals}, "meta": {}}
    )


def _feature_with_source(name: str, lon: float) -> dict[str, Any]:
    feature = _in_view_feature(name, "solar", lon)
    feature["properties"]["provenance"] = [
        {
            "source_name": "Virginia DEQ air permits",
            "source_url": "https://example.invalid/deq",
            "retrieved_at": "2026-09-28T00:00:00Z",
            "reuse_class": "open",
            "attribution_text": None,
        }
    ]
    return feature


def test_map_keyboard_path_reaches_results_and_the_drawer_and_the_closed_drawer_is_gone(
    server: object,
) -> None:
    """F5 + D-13. Closed, the drawer used to stay focusable and exposed as an `aria-modal` dialog;
    proposals opened it only by mouse; it named no licence; no skip link reached the results."""
    envelope = _geo_envelope(
        [_feature_with_source("deq-one", -77.5), _feature_with_source("deq-two", -77.4)], records=2
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            page.route(
                "**/api/ui-events",
                lambda r: r.fulfill(status=202, content_type="application/json", body="{}"),
            )
            page.route(
                "**/api/proposals/geo?**",
                lambda r: r.fulfill(status=200, content_type="application/json", body=envelope),
            )
            page.goto(BASE_URL + "/")
            page.wait_for_function(
                "() => document.querySelectorAll('#in-view-items .details-btn').length === 2"
            )

            # Closed: hidden, not modal, and not a tab stop.
            drawer = page.locator("#map-drawer")
            assert drawer.evaluate("d => d.hidden") is True
            assert drawer.get_attribute("aria-modal") is None
            assert page.locator("#drawer-close").is_hidden()

            # The second skip link lands on the results.
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.textContent") == "Skip to main content"
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.textContent") == "Skip to results in view"
            page.keyboard.press("Enter")
            assert page.evaluate("document.activeElement.id") == "in-view-list"

            # A proposal row opens the drawer from the keyboard; the drawer names the licence.
            details = page.get_by_role("button", name="Details for deq-one")
            details.focus()
            page.keyboard.press("Enter")
            page.wait_for_function(
                "() => document.getElementById('map-drawer').classList.contains('is-open')"
            )
            assert drawer.get_attribute("aria-modal") == "true" and drawer.get_attribute("role") == "dialog"
            assert page.evaluate("document.activeElement.id") == "drawer-close"
            source_line = page.inner_text("#map-drawer .drawer-source")
            assert "Virginia DEQ air permits" in source_line and "Open licence" in source_line

            # Escape closes it, focus returns to the button, and it leaves the page again.
            page.keyboard.press("Escape")
            page.wait_for_function("() => document.getElementById('map-drawer').hidden === true")
            assert page.evaluate("document.activeElement.getAttribute('aria-label')") == "Details for deq-one"
            assert drawer.get_attribute("aria-modal") is None

            # Tabbing the whole page never lands inside the closed drawer.
            for _ in range(80):
                page.keyboard.press("Tab")
                assert not page.evaluate("!!document.activeElement.closest('#map-drawer')")
        finally:
            browser.close()


def test_map_drops_a_slow_earlier_proposals_response(server: object) -> None:
    """F3: hold the first `/api/proposals/geo` answer, move the map, let the second answer land,
    then release the first: the list and the count must still describe the current view."""
    national = _geo_envelope(
        [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-98.5, 39.8]},
                "properties": {"feature_kind": "cluster", "count": 5000, "dominant_lifecycle_state": "filed"},
            }
        ],
        records=5000,
        clustered=True,
    )
    local = _geo_envelope(
        [_feature_with_source("near-one", -77.5), _feature_with_source("near-two", -77.4)], records=2
    )
    held: list[Route] = []

    def answer(route: Route) -> None:
        if not held:
            held.append(route)  # the first (national) request: answered later, out of order
            return
        route.fulfill(status=200, content_type="application/json", body=local)

    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            page.route(
                "**/api/ui-events",
                lambda r: r.fulfill(status=202, content_type="application/json", body="{}"),
            )
            page.route("**/api/proposals/geo?**", answer)
            page.goto(BASE_URL + "/")
            page.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            assert held, "the first geo request was not issued"
            page.evaluate("window.__map.jumpTo({center: [-77.45, 38.95], zoom: 9})")
            page.wait_for_function(
                "() => document.querySelectorAll('#in-view-items a.name-link').length === 2"
            )
            # The page may already have aborted the superseded request, which is also correct.
            with contextlib.suppress(PlaywrightError):
                held[0].fulfill(status=200, content_type="application/json", body=national)
            page.wait_for_timeout(800)
            assert page.locator("#in-view-items a.name-link").count() == 2
            assert page.inner_text("#map-result-count").startswith("2 proposals match")
            assert "5000" not in page.inner_text("#map-live-region")
        finally:
            browser.close()


def test_map_announces_what_is_in_view_and_names_and_counts_lines(server: object) -> None:
    """F4: the live region said `totals.records` (every match, anywhere) was "in view", called
    every line a pipeline, and never said when the API had capped the lines it sent."""
    proposals = _geo_envelope(
        [
            _feature_with_source("in-a", -77.5),
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-77.3, 38.9]},
                "properties": {"feature_kind": "cluster", "count": 40, "dominant_lifecycle_state": "filed"},
            },
        ],
        records=5651,
        clustered=False,
    )

    def line(n: int) -> dict[str, Any]:
        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[-77.6, 38.8 + n / 100], [-77.2, 38.9 + n / 100]],
            },
            "properties": {
                "feature_kind": "asset_line",
                "asset_type": "transmission_line",
                "name": f"Line {n}",
                "public_id": f"ast_line{n}",
                "voltage_kv": 230,
            },
        }

    assets = json.dumps(
        {
            "data": {
                "type": "FeatureCollection",
                "features": [line(1), line(2)],
                "totals": {"records": 13290, "clustered": False, "line_count": 13290, "lines_shown": 1500},
            },
            "meta": {},
        }
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            page.route(
                "**/api/ui-events",
                lambda r: r.fulfill(status=202, content_type="application/json", body="{}"),
            )
            page.route(
                "**/api/proposals/geo?**",
                lambda r: r.fulfill(status=200, content_type="application/json", body=proposals),
            )
            page.route(
                "**/api/assets/geo?**",
                lambda r: r.fulfill(status=200, content_type="application/json", body=assets),
            )
            page.goto(BASE_URL + "/?layers=plants&asset_type=transmission_line")
            page.wait_for_function(
                "() => /transmission lines/.test(document.getElementById('map-live-region').textContent)",
                timeout=30000,
            )
            live = page.inner_text("#map-live-region")
            assert live.startswith("41 proposals in view."), live  # 1 point + a cluster of 40, not 5651
            assert "2 existing assets in view (2 transmission lines)." in live
            assert "pipeline" not in live
            note = "Showing the longest 1,500 of 13,290 lines in this view; zoom in to see them all."
            assert note in live
            assert page.inner_text("#lines-note") == note
        finally:
            browser.close()


def test_report_a_problem_creates_a_review_task(server: object) -> None:
    """D-6: the "Report a problem" link was `mailto:` with no recipient. The form now posts through
    the site to `POST /v1/reports`; the answer replaces the form and takes focus."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=NARROW_VIEWPORT)
            _install_offline_routes(page)
            page.goto(BASE_URL + "/proposals")
            href = page.locator("#result-rows a[href^='/proposals/']").first.get_attribute("href")
            page.goto(BASE_URL + href)
            assert page.locator("a[href^='mailto:']").count() == 0
            page.get_by_text("Something wrong with this record? Report a problem").click()
            page.get_by_label("The status is wrong").check()
            page.get_by_label("What should it say, and where did you see that?").fill(
                "E2E check: the register lists this project as withdrawn."
            )
            page.get_by_role("button", name="Send report").click()
            status = page.locator("#report-status")
            status.wait_for(state="visible")
            assert "Thanks. Your report has reached our editors." in status.inner_text()
            assert page.evaluate("document.activeElement.id") == "report-status"
        finally:
            browser.close()
    engine = get_engine(f"sqlite+pysqlite:///{DB_PATH}")
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT issue_type, subject_type FROM task"
            " WHERE type = 'report' AND description LIKE 'E2E check:%'"
        ).fetchall()
    assert [tuple(r) for r in rows] == [("wrong_status", "proposal")]


# ============================================================================================
# Real-archive proof (coordinator follow-up, 2026-09-15): the fake-URL test above proves the
# *fallback* path (both CDN scripts blocked); this proves pmtiles mode actually renders a real
# basemap, with real glyphs, end to end. Its own DB (a 5-row sample, not the full load above) and
# its own uvicorn subprocess/port, both module-scoped so the whole class of tests here pays the
# archive-serving setup cost once.
def _ok_ui_events(page: Any) -> None:
    page.route(
        "**/api/ui-events", lambda r: r.fulfill(status=202, content_type="application/json", body="{}")
    )


def test_map_viewport_round_trips_through_the_url_and_the_list(server: object) -> None:
    """Frontend audit F7 (docs/04 D-17): a pan or zoom changed nothing in the URL, so a shared or
    reloaded link opened on the national view. The view is now `center=&zoom=` beside the
    filters, restored on load, and carried by "View as: List" and back (designer D-8)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            _ok_ui_events(page)
            page.goto(BASE_URL + "/?technology=solar")
            page.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            page.evaluate("window.__map.jumpTo({center: [-77.5, 38.9], zoom: 8})")
            page.wait_for_function("() => location.search.includes('zoom=8.00')", timeout=10000)
            query = parse_qs(urlsplit(page.url).query)
            assert query["center"] == ["-77.5000,38.9000"] and query["technology"] == ["solar"]

            page.reload()
            page.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            view = page.evaluate(
                "() => { const c = window.__map.getCenter(); return [c.lng, c.lat, window.__map.getZoom()]; }"
            )
            assert [round(v, 2) for v in view] == [-77.5, 38.9, 8.0]
            assert page.input_value("#mf-technology") == "solar"

            list_href = page.get_attribute("#view-as-list", "href") or ""
            assert parse_qs(urlsplit(list_href).query) == {
                "technology": ["solar"],
                "center": ["-77.5000,38.9000"],
                "zoom": ["8.00"],
            }
            page.click("#view-as-list")
            page.wait_for_selector("#view-as-map")
            back = parse_qs(urlsplit(page.get_attribute("#view-as-map", "href") or "").query)
            assert back == {"technology": ["solar"], "center": ["-77.5000,38.9000"], "zoom": ["8.00"]}
        finally:
            browser.close()


def test_map_is_above_the_fold_and_400px_collapses_nav_filters_and_the_list(server: object) -> None:
    """Designer D-3/D-4, frontend F6. Measured on the base build: at 1440x900 the map started at
    787px (113px visible); at 400x800 at 1,658px, below a 421px header and a 600px filter bar,
    with an 11,121px in-view list beneath it."""
    many = _geo_envelope(
        [_in_view_feature(f"row-{i:02d}", "solar", -77.6 + i * 0.004) for i in range(60)], records=60
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            wide = browser.new_page(viewport={"width": 1440, "height": 900})
            _install_offline_routes(wide)
            _ok_ui_events(wide)
            wide.goto(BASE_URL + "/")
            wide.wait_for_selector("#map canvas", timeout=10000)
            top = wide.evaluate("document.getElementById('map').getBoundingClientRect().top")
            assert top < 450, f"map starts at {top}px at 1440x900"
            assert wide.locator(".nav-toggle").is_hidden() and wide.locator("#map-filters").is_visible()

            page = browser.new_page(viewport={"width": 400, "height": 800})
            _install_offline_routes(page)
            _ok_ui_events(page)
            page.route(
                "**/api/proposals/geo?**",
                lambda r: r.fulfill(status=200, content_type="application/json", body=many),
            )
            page.goto(BASE_URL + "/")
            page.wait_for_function("() => document.querySelectorAll('#in-view-items a.name-link').length > 0")
            top = page.evaluate("document.getElementById('map').getBoundingClientRect().top")
            assert top < 640, f"map starts at {top}px at 400x800"
            assert page.evaluate("document.documentElement.scrollWidth") <= 401

            # Menu: a disclosure; the links are out of the way until it opens.
            menu = page.locator(".nav-toggle")
            assert page.locator(".primary-nav").is_hidden() and menu.get_attribute("aria-expanded") == "false"
            menu.click()
            assert menu.get_attribute("aria-expanded") == "true" and page.locator(".primary-nav").is_visible()
            # Escape from inside the open panel closes it and returns focus to "Menu".
            page.locator(".primary-nav a").first.focus()
            page.keyboard.press("Escape")
            assert page.locator(".primary-nav").is_hidden()
            assert page.evaluate("document.activeElement.classList.contains('nav-toggle')")

            # Filters: one button naming how many are set, opening the bar in place.
            toggle = page.locator("[data-filter-toggle]")
            assert page.locator("#map-filters").is_hidden()
            toggle.click()
            page.select_option("#mf-technology", "solar")
            assert "(1 active)" in toggle.inner_text()
            page.click("[data-filter-done]")
            assert page.locator("#map-filters").is_hidden()
            assert page.evaluate("document.activeElement.hasAttribute('data-filter-toggle')")

            # The in-view list is a page of 25 with "Show more", not every row.
            page.wait_for_function(
                "() => document.querySelectorAll('#in-view-items a.name-link').length === 25"
            )
            more = page.locator(".show-more-btn")
            assert more.inner_text() == "Show 25 more proposals (35 not listed)"
            more.click()
            page.wait_for_function(
                "() => document.querySelectorAll('#in-view-items a.name-link').length === 50"
            )
            # Focus lands on the first new row, so a keyboard reader carries on from there.
            assert page.evaluate("document.activeElement.textContent") == "row-25"
        finally:
            browser.close()


def test_map_says_when_proposals_fail_to_load_with_the_request_id_and_a_retry(server: object) -> None:
    """Designer D-11 (docs/31 §6): with `/api/proposals/geo` failing, the count line said "Loading
    proposals..." for good and the in-view list stayed empty with no word of why."""
    failing = {"on": True}
    good = _geo_envelope([_in_view_feature("after-retry", "solar", -77.5)], records=1)
    problem = json.dumps(
        {"type": "about:blank", "title": "Internal server error", "status": 500, "request_id": "req_e2e500"}
    )

    def answer(route: Route) -> None:
        if failing["on"]:
            route.fulfill(status=500, content_type="application/problem+json", body=problem)
        else:
            route.fulfill(status=200, content_type="application/json", body=good)

    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            _ok_ui_events(page)
            page.route("**/api/proposals/geo?**", answer)
            page.goto(BASE_URL + "/")
            page.wait_for_selector("#map-error:not([hidden])", timeout=30000)
            error = page.inner_text("#map-error")
            assert "Couldn\u2019t load proposals for this view." in error
            assert "Internal server error" in error and "req_e2e500" in error
            assert page.get_attribute("#map-error", "role") == "alert"
            assert page.inner_text("#map-result-count") == "Proposals could not be loaded."
            assert page.get_attribute("#map-error a", "href", timeout=1000)

            failing["on"] = False
            page.click(".map-error__retry")
            page.wait_for_selector("#map-error", state="hidden")
            assert page.inner_text("#map-result-count") == "1 proposal match these filters."
            assert page.locator("#in-view-items a.name-link").inner_text() == "after-retry"
        finally:
            browser.close()


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


@pytest.mark.network  # fetches the pmtiles/basemaps scripts and Protomaps glyphs from their real hosts
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


def test_smoke_pricing_page_reached_from_the_nav_and_legible_at_400px(server: object) -> None:
    """The pricing page (`web/pricing.py`, docs/41) in a real browser at the narrow width docs/31
    §4 sets as the floor: reached by clicking the nav link rather than typed in, all four tiers
    on screen, no horizontal scroll, and the signed-out control carrying the chosen tier into
    registration. `web/test_pricing.py` covers the states and the checkout hand-off; this is the
    end-to-end path a visitor actually walks."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=NARROW_VIEWPORT)
            _install_offline_routes(page)
            page.goto(BASE_URL + "/about")
            # Below 720px the links sit behind "Menu" (designer audit D-4, docs/31 §3).
            page.click(".nav-toggle")
            page.click('.primary-nav a[href="/pricing"]')
            page.wait_for_url("**/pricing")

            for tier in ("Free", "Pro", "Team", "API / Data"):
                assert page.locator(f'.tier h2:text-is("{tier}")').count() == 1, f"{tier} tier missing"
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            assert overflow <= 0, f"page scrolls horizontally at 400px by {overflow}px"

            # Signed out: the control says what it does and comes back to the tier that was picked.
            href = page.get_attribute('.tier[aria-labelledby="tier-pro"] .tier__cta a', "href")
            assert href == "/register?next=%2Fpricing%3Fplan%3Dpro", href

            # docs/41's do-not-publish rule, checked once against the rendered page too.
            rendered = page.inner_text("main").lower()
            assert "export" not in rendered
            assert "watchlist" not in rendered

            page.screenshot(path=str(SCREENSHOT_DIR / "pricing-400.png"), full_page=True)
        finally:
            browser.close()
