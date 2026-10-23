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
import itertools
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
# when this path does not exist, so the default suite stays fully offline. Protomaps keeps only
# recent daily builds (20260914 was gone by 2026-10-21; 20261020 worked): pick a current one from
# https://build-metadata.protomaps.dev/builds.json. `infra/ci/beta.sh install-pmtiles` installs the
# pinned, checksummed go-pmtiles (as `go-pmtiles`).
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
    # The server's log goes to a file, never an unread pipe: a full 64 KiB pipe buffer blocks the
    # server's next write, so every later request (and SIGTERM's graceful shutdown) hangs.
    server_log = DB_PATH.with_suffix(".server.log").open("wb")
    proc = subprocess.Popen(  # noqa: S603 -- fixed argv; the port is an int parsed above
        [sys.executable, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", str(E2E_PORT)],
        cwd=REPO_ROOT,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_server(BASE_URL + "/health")
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        server_log.close()


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
    # Review 2026-10-10 §2.6 item 2: the map page opens on what the product does, and says how
    # current the sources are once, in the masthead; the tier line is on the lists and records
    # (checked on /proposals below). The panel holding what is in view and the key floats over the
    # map (owner, 2026-10-22), its header inside the first viewport.
    assert page.locator(".delayed-notice").count() == 0
    assert page.locator(".map-intro__lede").inner_text().startswith("Projects from public grid-queue")
    panel_top = page.evaluate("document.getElementById('map-panel').getBoundingClientRect().top")
    assert panel_top < DESKTOP_VIEWPORT["height"] - 100, (
        f"the map's panel starts at {panel_top}px at 1440x900"
    )
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

    # ---- desktop: the existing-asset and retired layers, on by default (owner, 2026-10-20) ----
    plants_checkbox = page.locator("#mf-layer-plants")
    assert plants_checkbox.count() == 1, "existing-plants layer checkbox missing"
    assert plants_checkbox.is_checked(), "the existing-assets layer is on when the link names no layers="
    assert page.locator("#mf-layer-retired").is_checked(), "the retired layer is on by default"
    page.wait_for_function("() => window.__map.getLayoutProperty('plant-points', 'visibility') === 'visible'")
    assert "layers=" not in page.url, "the default writes no layers= to the URL"
    plants_checkbox.uncheck()
    assert "layers=retired" in page.url, "turning one layer off keeps the other in the URL"
    plants_checkbox.check()

    # ---- desktop: proposals list, attribution rendered ----
    page.goto(BASE_URL + "/proposals")
    page.wait_for_selector(".record-table tbody tr")
    assert page.locator(".record-table tbody tr").count() > 0
    # docs/04 D-3/D-28: the tier line is on screen and always true. Since the ISO change-event
    # delay was dropped (owner, 2026-09-21) there is no delay left to name, so the line states what
    # the page is -- live -- and what a paid plan adds (this server runs the commercial default).
    tier_line = page.locator(".delayed-notice").inner_text()
    assert "published as soon as it is ingested" in tier_line
    assert "days" not in tier_line, "no surface may claim a delay the product does not apply"
    assert "Alerts and API in Pro" in tier_line
    page.screenshot(path=str(SCREENSHOT_DIR / "list-desktop.png"), full_page=True)

    # ---- desktop: proposal detail, provenance panel + attribution ----
    first_row_link = page.locator(".record-table a.row-link").first
    detail_href = first_row_link.get_attribute("href")
    first_row_link.click()
    page.wait_for_selector(".provenance-panel")
    assert page.locator(".attribution-line").count() > 0
    page.screenshot(path=str(SCREENSHOT_DIR / "detail-desktop.png"), full_page=True)

    # ---- desktop: list -> record -> company, one company linked at each step (docs/16 U-2) ----
    page.goto(BASE_URL + "/proposals")
    page.wait_for_selector(".record-table tbody tr")
    assert page.locator(".record-table thead th", has_text="Company").count() == 1
    company_cell = "td[data-label='Company'] a"
    row = page.locator(".record-table tbody tr", has=page.locator(company_cell)).first
    company_href = row.locator(company_cell).get_attribute("href")
    assert company_href and company_href.startswith("/organizations/")
    row.locator("a.row-link").click()
    page.wait_for_selector("#record-company")
    header_link = page.locator("#record-company a").first
    assert header_link.get_attribute("href") == company_href
    assert page.locator(".provenance-panel").count() == 1, "the record keeps its sources"
    header_link.click()
    page.wait_for_selector("h1")
    assert urlsplit(page.url).path == company_href

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
# Below 720px the panel opens under the map, in the page, and "Key" scrolls it into view (docs/17
# M-1; owner, 2026-10-22: the key is the panel's Key tab). With the layers on the Key tab is taller
# than a phone's screen, so what must be in view is the projects' key, with the panel's top on screen.
_KEY_IN_VIEW = (
    "() => { const p = document.getElementById('map-panel').getBoundingClientRect();"
    " const k = document.getElementById('lifecycle-legend').getBoundingClientRect();"
    " return p.top >= -1 && k.bottom <= innerHeight + 1; }"
)
_GROW_KEY = "(px) => { document.getElementById('lifecycle-legend').style.paddingBottom = px + 'px'; }"
_MAP_BOTTOM_AND_KEY_TOP = (
    "[document.getElementById('map').getBoundingClientRect().bottom,"
    " document.getElementById('lifecycle-legend').getBoundingClientRect().top]"
)


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
            shown = int(page.inner_text("#map-result-count").split()[0].replace(",", ""))
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
            page.wait_for_function(f"() => {count_js}.startsWith('{generation:,} ')")
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
    the URL and the summary say so, and "Open these in the list" opens the list with Built still ticked."""
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
            page.click("#map-list-link")
            page.wait_for_load_state("domcontentloaded")
            assert page.is_checked("#f-status-built") and page.is_checked("#f-status-announced")
            assert not page.is_checked("#f-status-withdrawn")
        finally:
            browser.close()


def test_unlisted_toggle_on_the_map_and_the_list_keeps_it_both_ways(server: object) -> None:
    """Owner, 2026-10-11 (`web/unlisted.py`): a view of active states hides the records no register
    lists any more; a box in the Status disclosure shows them. Walks the real pages from the
    keyboard: on the map, Space on the box asks the site's geo route with `unlisted=include` (the
    site sends the API `listed=true` without it, never `unlisted`), the count is that read's own
    total, the URL, the summary and the List link carry it, the feed does not and the alert link
    stays (the feed is the alert page's since 2026-10-22); the list opens with the box ticked and
    "Clear all" offered, and unticking it there (then
    "Apply filters", the next stop) drops it from the URL and from the Map link. The e2e store holds
    no dropped record, so the counts match either way; `web/test_default_view.py` holds the counts
    on a store that has them."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            _ok_ui_events(page)
            page.goto(BASE_URL + "/")
            page.wait_for_selector("#map canvas", timeout=10000)
            page.wait_for_function("() => window.__map && window.__map.isStyleLoaded()", timeout=15000)
            page.wait_for_function(_COUNT_READY, timeout=15000)
            assert page.inner_text("#mf-status [data-status-summary]") == "active"
            assert not page.is_checked("#mf-unlisted")
            page.click("#mf-status summary")
            page.focus("#mf-unlisted")
            with page.expect_response(
                lambda r: "/api/proposals/geo?" in r.url and "unlisted=include" in r.url, timeout=30000
            ) as geo_info:
                page.keyboard.press("Space")
            assert page.is_checked("#mf-unlisted")
            page.wait_for_function(_COUNT_READY, timeout=15000)
            shown = int(page.inner_text("#map-result-count").split()[0].replace(",", ""))
            assert shown == geo_info.value.json()["data"]["totals"]["records"]
            assert parse_qs(urlsplit(page.url).query).get("unlisted") == ["include"], page.url
            assert page.inner_text("#mf-status [data-status-summary]") == "active, no longer listed"
            list_query = parse_qs(urlsplit(page.get_attribute("#map-list-link", "href") or "").query)
            assert list_query.get("unlisted") == ["include"], list_query
            # The map has no feed link of its own since 2026-10-22: the alert page offers it.
            assert page.locator("#view-as-feed").count() == 0
            assert page.is_visible("#mf-save-alert")

            page.click("#map-list-link")
            page.wait_for_selector("#view-as-map")
            assert page.is_checked("#f-unlisted")
            assert page.inner_text("#f-status [data-status-summary]") == "active, no longer listed"
            assert page.is_visible("a.filter-bar__clear")
            page.click("#f-status summary")
            page.focus("#f-unlisted")
            page.keyboard.press("Space")
            # The next stop is "Apply filters": the list's form is a plain GET form.
            page.keyboard.press("Tab")
            assert page.evaluate("() => document.activeElement.textContent.trim()") == "Apply filters"
            with page.expect_navigation(timeout=30000):
                page.keyboard.press("Enter")
            assert "unlisted" not in parse_qs(urlsplit(page.url).query), page.url
            assert not page.is_checked("#f-unlisted")
            back = parse_qs(urlsplit(page.get_attribute("#view-as-map", "href") or "").query)
            assert "unlisted" not in back, back
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


def _rgb_hex(css_colour: str) -> str:
    r, g, b = (int(v) for v in re.findall(r"\d+", css_colour)[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _contrast(a: str, b: str) -> float:
    def luminance(hex_colour: str) -> float:
        channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_map_hover_card_text_is_readable_in_both_themes(server: object, scheme: str) -> None:
    """Owner report 2026-10-21: in dark mode the hover card's text could not be seen. MapLibre's
    stylesheet, loaded after styles.css, paints the card and its tip #fff, and the text inherited
    the dark theme's light --text."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT, color_scheme=scheme)
            _install_offline_routes(page)
            page.route(
                "**/api/ui-events",
                lambda r: r.fulfill(status=202, content_type="application/json", body="{}"),
            )
            page.goto(BASE_URL + "/")
            page.wait_for_function("() => window.__map && window.__map.isStyleLoaded()")
            # The Popup map.js opens on hover, anchored below its point so the tip shows.
            page.evaluate(
                "() => new maplibregl.Popup({closeButton: false, closeOnClick: false, anchor: 'top'})"
                ".setLngLat(window.__map.getCenter())"
                ".setHTML('<strong>Plant</strong><br>Operator: Example')"
                ".addTo(window.__map)"
            )
            colours = page.locator(".maplibregl-popup-content").evaluate(
                "el => { const card = getComputedStyle(el);"
                " const tip = getComputedStyle(el.parentElement.querySelector('.maplibregl-popup-tip'));"
                " return [card.color, card.backgroundColor, tip.borderBottomColor]; }"
            )
        finally:
            browser.close()
    text, background, tip = (_rgb_hex(c) for c in colours)
    assert _contrast(text, background) >= 4.5, (scheme, text, background)
    assert tip == background, f"the tip ({tip}) does not join the card ({background})"


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
            assert page.inner_text("#map-result-count").startswith("2 projects match")
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
    filters, restored on load, and carried by "Open these in the list" and back (designer D-8)."""
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

            list_href = page.get_attribute("#map-list-link", "href") or ""
            assert parse_qs(urlsplit(list_href).query) == {
                "technology": ["solar"],
                "center": ["-77.5000,38.9000"],
                "zoom": ["8.00"],
            }
            page.click("#map-list-link")
            page.wait_for_selector("#view-as-map")
            back = parse_qs(urlsplit(page.get_attribute("#view-as-map", "href") or "").query)
            assert back == {"technology": ["solar"], "center": ["-77.5000,38.9000"], "zoom": ["8.00"]}
        finally:
            browser.close()


def test_map_is_above_the_fold_and_400px_collapses_nav_filters_and_the_list(server: object) -> None:
    """Designer D-3/D-4, frontend F6. Measured on the base build: at 1440x900 the map started at
    787px (113px visible); at 400x800 at 1,658px, below a 421px header and a 600px filter bar,
    with an 11,121px in-view list beneath it. Owner, 2026-10-22: what is in view and the key are one
    panel over the map's top right, open from 1080px and collapsed from 720 to 1079px; below 720px a
    bar under the map that opens in the page."""
    many = _geo_envelope(
        [_in_view_feature(f"row-{i:02d}", "solar", -77.6 + i * 0.004) for i in range(60)], records=60
    )
    panel_box = (
        "(() => { const b = document.getElementById('map-panel').getBoundingClientRect();"
        " return [b.top, b.width, b.height]; })()"
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
            # Open from 1080px, 340px wide, inset 12px from the map's top right, never taller than it.
            # Read after the data lands (the count line above the map can wrap when it does) and in one
            # frame: two reads either side of a layout change failed on a loaded machine (14px apart).
            wide.wait_for_function(_COUNT_READY, timeout=30000)
            (panel_top, panel_w, panel_h), (map_top, map_h) = wide.evaluate(
                "(() => { const p = document.getElementById('map-panel').getBoundingClientRect();"
                " const m = document.getElementById('map').getBoundingClientRect();"
                " return [[p.top, p.width, p.height], [m.top, m.height]]; })()"
            )
            assert wide.get_attribute("#map-panel-toggle", "aria-expanded") == "true"
            assert panel_w == 340 and abs(panel_top - (map_top + 12)) <= 2, (panel_top, map_top, panel_w)
            assert panel_h <= map_h - 20, (panel_h, map_h)
            # The key is its second tab; a layer's key shows while the layer is on (from when the
            # layer is drawn: both are on when a link names none, owner 2026-10-20).
            wide.click("#map-tab-key")
            wide.wait_for_function("() => !document.getElementById('retired-legend').hidden")
            assert (
                wide.locator("#lifecycle-legend").is_visible()
                and wide.locator("#retired-legend").is_visible()
            )
            wide.uncheck("#mf-layer-retired")
            assert wide.locator("#retired-legend").is_hidden()
            assert wide.locator("#map-key-toggle").is_hidden()  # the rail's "Key" is a phone's

            # Lane F: from 720 to 1079px the map's filter bar is the "Filters" disclosure, as below
            # 720px (before: the map started at 634px of 768 at 1024x768, under two rows of filters).
            # Budget 600: at these widths the page is one column, as at 400px, and 768 - 600 keeps
            # the 400x800 budget's 160px of canvas in view. The panel starts collapsed to its header.
            tablet = browser.new_page(viewport={"width": 1024, "height": 768})
            _install_offline_routes(tablet)
            _ok_ui_events(tablet)
            tablet.goto(BASE_URL + "/")
            tablet.wait_for_selector("#map canvas", timeout=10000)
            top = tablet.evaluate("document.getElementById('map').getBoundingClientRect().top")
            assert top < 600, f"map starts at {top}px at 1024x768"
            assert tablet.get_attribute("#map-panel-toggle", "aria-expanded") == "false"
            panel_top, panel_w, panel_h = tablet.evaluate(panel_box)
            assert top <= panel_top < 768 and panel_w == 300 and panel_h < 140, (panel_top, panel_w, panel_h)
            assert tablet.evaluate("document.documentElement.scrollWidth") <= 1025
            filters = tablet.locator("[data-filter-toggle]")
            assert filters.is_visible() and filters.get_attribute("aria-expanded") == "false"
            assert tablet.locator("#map-filters").is_hidden()
            filters.click()
            assert tablet.locator("#map-filters").is_visible()
            assert filters.get_attribute("aria-expanded") == "true"
            tablet.select_option("#mf-technology", "solar")
            assert "(1 active)" in filters.inner_text()
            tablet.focus("#mf-jurisdiction")
            tablet.keyboard.press("Escape")
            assert tablet.locator("#map-filters").is_hidden()
            assert tablet.evaluate("document.activeElement.hasAttribute('data-filter-toggle')")
            # Only the map's bar: the proposal list keeps its filters open at this width.
            tablet.goto(BASE_URL + "/proposals")
            assert tablet.locator("#filters").is_visible()
            assert tablet.locator("[data-filter-toggle]").is_hidden()

            page = browser.new_page(viewport={"width": 400, "height": 800})
            _install_offline_routes(page)
            _ok_ui_events(page)
            page.route(
                "**/api/proposals/geo?**",
                lambda r: r.fulfill(status=200, content_type="application/json", body=many),
            )
            # Zoomed in, so the 60 rows are listed rather than summed up (map.js `chooseInViewMode`).
            page.goto(BASE_URL + "/?center=-77.5,38.95&zoom=9")
            page.wait_for_function("() => document.querySelectorAll('#in-view-items a.name-link').length > 0")
            top = page.evaluate("document.getElementById('map').getBoundingClientRect().top")
            assert top < 700, f"map starts at {top}px at 400x800"
            assert page.evaluate("document.documentElement.scrollWidth") <= 401
            # Below 720px the panel is a closed bar under the map (UX review 2026-10-20, docs/16 M-2;
            # docs/17 M-1); "Key" opens its Key tab under the map, in the page, brings it into view,
            # and closes it again.
            key_toggle = page.locator("#map-key-toggle")
            assert key_toggle.is_visible() and key_toggle.get_attribute("aria-expanded") == "false"
            assert page.locator("#map-key").is_hidden()
            key_toggle.click()
            assert page.locator("#map-key").is_visible()
            page.wait_for_function(_KEY_IN_VIEW)
            map_bottom, key = page.evaluate(_MAP_BOTTOM_AND_KEY_TOP)
            assert map_bottom <= key < 800, f"map key at {key}px, map ends at {map_bottom}px at 400x800"
            key_toggle.click()
            assert page.locator("#map-key").is_hidden()
            assert key_toggle.get_attribute("aria-expanded") == "false"

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

            # The in-view list is a page of 25 with "Show more", not every row, in the opened bar.
            page.click("#map-panel-toggle")
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
            assert page.inner_text("#map-result-count") == "1 project matches these filters."
            assert page.locator("#in-view-items a.name-link").inner_text() == "after-retry"
        finally:
            browser.close()


# UX review 2026-10-20 (docs/16 §2 M-1, U-1): with the archive answering 404, the maps showed their
# data on a blank plane: the Protomaps style's first layer, an opaque `background` that needs no
# source, painted over the same-origin outline whether or not the tiles loaded. The two pmtiles-mode
# scripts are not recorded (tests/fixtures/cdn/README.md), so these tests stand in for them with the
# surface basemap.js calls and the shape of what it gets back. `basemaps.layers()` puts the opaque,
# sourceless `background` first and then layers drawn from the archive, as
# @protomaps/basemaps@5.7.2 does. `pmtiles.Protocol().tile` reads the archive with a Range request,
# as pmtiles@4.5.0 does, so the route on the archive URL decides whether the basemap loads. The
# opt-in real-archive tests at the end of this file repeat the checks with the real scripts.
_PMTILES_STAND_INS = r"""
(() => {
  const archiveOf = (url) => url.replace(/^pmtiles:\/\//, "").replace(/\/\d+\/\d+\/\d+$/, "");
  window.pmtiles = {
    Protocol: function () {
      this.tile = async (params, abortController) => {
        const resp = await fetch(archiveOf(params.url), {
          headers: { Range: "bytes=0-16383" }, signal: abortController.signal
        });
        if (!resp.ok) throw new Error("Bad response code: " + resp.status);
        if (params.type !== "json") return { data: new ArrayBuffer(0) };
        return { data: {
          tilejson: "3.0.0", tiles: [params.url + "/{z}/{x}/{y}"], minzoom: 0, maxzoom: 5,
          vector_layers: [{ id: "earth", fields: {} }, { id: "water", fields: {} }]
        } };
      };
    }
  };
  window.basemaps = {
    namedFlavor: () => ({ background: "#000000", earth: "#000000", water: "#000000", landcover: {} }),
    layers: (source, flavor) => [
      { id: "background", type: "background", paint: { "background-color": flavor.background } },
      { id: "earth", type: "fill", source, "source-layer": "earth", paint: { "fill-color": flavor.earth } },
      { id: "water", type: "fill", source, "source-layer": "water", paint: { "fill-color": flavor.water } }
    ]
  };
})();
"""
_BASEMAP_UNAVAILABLE = "Base map unavailable; showing country and state outlines."
# The style's layer ids, a trailing `*` on each that needs no source; `name` is the map's window handle.
_LAYER_IDS = "(name) => window[name].getStyle().layers.map((l) => l.id + (l.source ? '' : '*'))"
# How many lines an element's text wraps to.
_LINE_COUNT = (
    "(n) => { const r = document.createRange(); r.selectNodeContents(n); return r.getClientRects().length; }"
)


def _stand_in_pmtiles_scripts(page: Any) -> None:
    """The stand-ins above in place of the (aborted) CDN scripts; the Protomaps glyph and sprite
    host they point the style at is aborted too, so the suite stays offline."""
    page.add_init_script(_PMTILES_STAND_INS)
    page.route(f"{GLYPHS_SPRITE_HOST_PREFIX}**", lambda r: r.abort())


def _archive_answers(page: Any, status: int) -> None:
    """The archive URL answers `status` (registered last, so it overrides the suite's abort)."""
    headers = {"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": "Content-Range"}
    if status == 206:
        headers["Content-Range"] = "bytes 0-15/16"
    page.route("**/*.pmtiles", lambda r: r.fulfill(status=status, body=b"\0" * 16, headers=headers))


def _basemap_failed_beacons(page: Any) -> list[str]:
    calls: list[str] = []

    def record(route: Route) -> None:
        if "map.basemap_failed" in (route.request.post_data or ""):
            calls.append(route.request.post_data or "")
        route.fulfill(status=202, content_type="application/json", body="{}")

    page.route("**/api/ui-events", record)
    return calls


def _outline_is_uncovered(ids: list[str]) -> None:
    """Every layer above the outline's land fill is drawn from a source, and the archive's layers
    were added (so the check is not passing on a basemap that never ran)."""
    land = ids.index("fallback-land")
    assert [i for i in ids[land + 1 :] if i.endswith("*")] == [], ids
    assert "earth" in ids and "water" in ids, ids


def test_a_failed_basemap_archive_leaves_the_outline_in_view_and_says_so(server: object) -> None:
    """With the archive answering 404 the home map keeps the outline under its data, and one line
    under the map says why it looks plain, at 1440 and at 400 wide. With the archive answering,
    the same layers are drawn as before, less the sourceless one, and nothing is said."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            for viewport in (DESKTOP_VIEWPORT, {"width": 400, "height": 800}):
                page = browser.new_page(viewport=viewport)
                _install_offline_routes(page)
                _stand_in_pmtiles_scripts(page)
                beacons = _basemap_failed_beacons(page)
                _archive_answers(page, 404)
                page.goto(BASE_URL + "/")
                note = page.locator(".map-basemap-note")
                note.wait_for(timeout=15000)
                page.wait_for_function("() => window.__map.isStyleLoaded()", timeout=15000)
                _outline_is_uncovered(page.evaluate(_LAYER_IDS, "__map"))
                assert note.inner_text() == _BASEMAP_UNAVAILABLE
                map_box = page.locator("#map").bounding_box()
                note_box = note.bounding_box()
                assert map_box and note_box and note_box["y"] >= map_box["y"] + map_box["height"] - 1
                lines = note.evaluate(_LINE_COUNT)
                assert lines == 1, f"the note takes {lines} lines at {viewport['width']}px"
                page.wait_for_timeout(300)
                assert len(beacons) == 1, beacons
                page.close()

            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            _stand_in_pmtiles_scripts(page)
            beacons = _basemap_failed_beacons(page)
            _archive_answers(page, 206)
            page.goto(BASE_URL + "/")
            page.wait_for_function("() => window.__map && window.__map.isSourceLoaded('protomaps')")
            page.wait_for_function("() => window.__mapIdle", timeout=15000)
            ids = page.evaluate(_LAYER_IDS, "__map")
            outline = ["fallback-water*", "fallback-land", "fallback-country-lines", "fallback-state-lines"]
            assert ids[:6] == [*outline, "earth", "water"], ids
            page.wait_for_timeout(300)
            assert page.locator(".map-basemap-note").count() == 0 and beacons == []

            # One failed tile on a working base map is a gap, not an outage: it is still reported,
            # but the map does not claim the base map is unavailable (review of lane w1-map).
            page.evaluate(
                "() => { window.__map.fire('error', {sourceId: 'protomaps', error: new Error('one tile')});"
                " window.__map.triggerRepaint(); }"
            )
            page.wait_for_function("() => window.__map.loaded()", timeout=15000)
            page.wait_for_timeout(500)
            assert page.locator(".map-basemap-note").count() == 0
            assert len(beacons) == 1, beacons
        finally:
            browser.close()


def test_the_panel_is_closed_on_a_phone_before_the_map_script_runs(server: object) -> None:
    """Review of lane w1-map: map.js runs only after the MapLibre and basemap CDN scripts, so on a
    slow phone the open key covered the map until then. The key is the panel's tab since 2026-10-22:
    below 1080px the panel's body waits for map.js (a phone's bar and the 720-1079px panel start
    closed); from 1080px the panel shows open from first paint."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            for viewport, shown in (({"width": 400, "height": 800}, False), (DESKTOP_VIEWPORT, True)):
                page = browser.new_page(viewport=viewport)
                _install_offline_routes(page)
                page.route("**/static/js/map.js*", lambda r: r.abort())
                page.goto(BASE_URL + "/")
                assert page.evaluate("document.documentElement.classList.contains('js')")
                assert page.locator("#map-panel-body").is_visible() is shown, viewport
                assert page.locator("#map-key").is_hidden(), viewport  # its tab waits for map.js
                page.close()
        finally:
            browser.close()


def _mini_map_page() -> str:
    """The mini-map as the asset and company pages render it (the `_macros.html` macro over
    `web/page.py::_mini_map`, then the scripts those pages load), alone on a page: the e2e store has
    no asset with a location to open a real asset page on."""
    from web.page import _mini_map, templates

    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-97.75, 30.3]},
        "properties": {"kind": "asset", "name": "E2E plant", "asset_type": "power_plant"},
    }
    mini_map = _mini_map([feature], label="Map of E2E plant", caption="E2E plant: location.")
    macro = templates.env.get_template("_macros.html").module.mini_map(mini_map, FAKE_PMTILES_URL, "pmtiles")  # type: ignore[attr-defined]
    maplibre = f"https://cdn.jsdelivr.net/npm/maplibre-gl@{MAPLIBRE_VERSION}/dist/maplibre-gl"
    scripts = [
        f"{maplibre}.js",
        f"https://cdn.jsdelivr.net/npm/pmtiles@{PMTILES_VERSION}/dist/pmtiles.js",
        f"https://cdn.jsdelivr.net/npm/@protomaps/basemaps@{BASEMAPS_VERSION}/dist/basemaps.js",
        "/static/js/basemap.js",
        "/static/js/asset_map.js",
    ]
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Mini-map</title>'
        f'<link rel="stylesheet" href="/static/css/styles.css"><link rel="stylesheet" href="{maplibre}.css">'
        f"</head><body><main>{macro}</main>"
        + "".join(f'<script src="{src}"></script>' for src in scripts)
        + "</body></html>"
    )


def test_a_failed_basemap_archive_is_said_under_the_asset_and_company_mini_map(server: object) -> None:
    """The asset and company mini-maps share basemap.js: with the archive answering 404 the outline
    stays in view and the note sits between the map and its caption; answering, nothing is said."""
    html, url = _mini_map_page(), BASE_URL + "/e2e-mini-map"
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            for status in (404, 206):
                page = browser.new_page(viewport={"width": 400, "height": 800})
                _install_offline_routes(page)
                _stand_in_pmtiles_scripts(page)
                _archive_answers(page, status)
                page.route(url, lambda r: r.fulfill(content_type="text/html", body=html))
                page.goto(url)
                page.wait_for_function(
                    "() => window.__assetMap && window.__assetMap.isStyleLoaded()"
                    " && window.__assetMap.getSource('protomaps')",
                    timeout=15000,
                )
                note = page.locator(".map-basemap-note")
                if status == 404:
                    note.wait_for(timeout=15000)
                    _outline_is_uncovered(page.evaluate(_LAYER_IDS, "__assetMap"))
                    assert note.inner_text() == _BASEMAP_UNAVAILABLE
                    assert note.evaluate("(n) => n.previousElementSibling.id") == "asset-map"
                    assert note.evaluate("(n) => n.nextElementSibling.className") == "mini-map__caption"
                else:
                    page.wait_for_function("() => window.__assetMap.isSourceLoaded('protomaps')")
                    page.wait_for_timeout(300)
                    assert note.count() == 0
                    assert "background*" not in page.evaluate(_LAYER_IDS, "__assetMap")
                page.close()
        finally:
            browser.close()


def test_map_key_starts_closed_below_720px_and_opens_from_the_keyboard(server: object) -> None:
    """UX review 2026-10-20 (docs/16 M-2, U-12): at 400x800 the open key covered about half of the
    464px map. The key is the panel's Key tab since 2026-10-22 (owner): below 720px the panel is a
    closed bar under the map, and the rail's "Key", a tab stop in the map's controls, opens the Key
    tab in place; from 1080px the panel starts open. The default follows the width until the reader
    presses "Key" (or the header); after that their choice stands."""
    narrow = {"width": 400, "height": 800}
    collapsed = "() => document.getElementById('map-panel').classList.contains('is-collapsed')"
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            # Reduced motion makes the reveal one instant scroll (map.js `revealPanelBelowMap`). A smooth
            # scroll that a slow runner cuts short is not repeated until the panel resizes, which failed
            # CI on PR #92 while the same test passed locally; what is asserted is where the panel ends up.
            page = browser.new_page(viewport=narrow, reduced_motion="reduce")
            _install_offline_routes(page)
            _ok_ui_events(page)
            page.goto(BASE_URL + "/")
            page.wait_for_selector("#map canvas", timeout=10000)
            # The data first: it grows the panel (the status families, the region row), and below
            # 720px the panel is in the page, so one opened before it lands can end below the screen.
            # map.js keeps the panel's foot in view as it grows after a press; this keeps the test
            # off that race as well.
            page.wait_for_function(_COUNT_READY, timeout=30000)
            toggle = page.locator("#map-key-toggle")
            toggle.wait_for()
            key = page.locator("#map-key")
            assert toggle.get_attribute("aria-expanded") == "false" and key.is_hidden()
            assert page.evaluate(collapsed)

            # Not chosen yet: the default follows the width both ways.
            page.set_viewport_size(DESKTOP_VIEWPORT)
            page.wait_for_function(f"() => !({collapsed})()")
            page.set_viewport_size(narrow)
            page.wait_for_function(collapsed)
            assert toggle.get_attribute("aria-expanded") == "false"

            # In the right-edge column (docs/17 M-1), Tab from zoom out passes "Jump to" (its list
            # closed) and reaches "Key"; Enter opens the Key tab under the map, brought into view.
            page.focus("#zoom-out")
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id") == "map-jump-toggle"
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id") == "map-key-toggle"
            page.keyboard.press("Enter")
            assert toggle.get_attribute("aria-expanded") == "true" and key.is_visible()
            assert page.get_attribute("#map-tab-key", "aria-selected") == "true"
            page.wait_for_function(_KEY_IN_VIEW)
            map_bottom, key_top = page.evaluate(_MAP_BOTTOM_AND_KEY_TOP)
            assert map_bottom <= key_top < narrow["height"], (key_top, map_bottom)
            # A panel that grows after the press (the region row arrives with its outlines) is brought
            # back into view, until the reader does something; then the page stays where they left it.
            page.evaluate(_GROW_KEY, 240)
            page.wait_for_function(_KEY_IN_VIEW, timeout=5000)
            page.keyboard.press("Shift")
            page.wait_for_timeout(400)  # the smooth scroll settles
            scrolled = page.evaluate("window.scrollY")
            page.evaluate(_GROW_KEY, 480)
            page.wait_for_timeout(600)
            assert page.evaluate("window.scrollY") == scrolled
            page.evaluate(_GROW_KEY, 0)

            # Chosen: a change of width leaves it open.
            page.set_viewport_size(DESKTOP_VIEWPORT)
            page.set_viewport_size(narrow)
            page.wait_for_timeout(200)
            assert toggle.get_attribute("aria-expanded") == "true" and key.is_visible()
            page.focus("#map-key-toggle")
            page.keyboard.press("Space")
            assert toggle.get_attribute("aria-expanded") == "false" and key.is_hidden()
        finally:
            browser.close()


# ============================================================================================
# Mobile review 2026-10-21 (docs/17): one tap does one thing (M-2), one finger scrolls the page past
# a map on a touch screen (M-3), the in-view header carries a cluster's breakdown (M-5), the default
# view fits the lower 48 (M-7), and an asset request for a view already left is aborted (§1).

#: A phone as docs/17 measured it: 390x844, touch, coarse pointer.
PHONE = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True}
_EMPTY_GEO = json.dumps({"data": {"type": "FeatureCollection", "features": [], "totals": {}}, "meta": {}})


def _cluster(lon: float, lat: float, counts: dict[str, int], mw: float, expands_to: int) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "feature_kind": "cluster",
            "count": sum(counts.values()),
            "dominant_lifecycle_state": max(counts, key=lambda k: counts[k]),
            "lifecycle_state_counts": counts,
            "capacity_mw_sum": mw,
            "expands_to_zoom": expands_to,
        },
    }


_TARRANT = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [-97.2, 32.77]},
    "properties": {
        "feature_kind": "region",
        "region_level": "county",
        "region_id": "48439",
        "name": "Tarrant",
        "count": 4,
        "lifecycle_state_counts": {"filed": 2, "studied": 1, "contracted": 1},
        "capacity_mw_sum": 791.6,
    },
}
_TARRANT_POLYGON = json.dumps(
    {
        "data": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-97.6, 32.55], [-97.0, 32.55], [-97.0, 33.0], [-97.6, 33.0], [-97.6, 32.55]]
                        ],
                    },
                    "properties": {"region_id": "48439"},
                }
            ],
        }
    }
)
_PLANT = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [-97.33, 32.75]},
    "properties": {
        "feature_kind": "asset",
        "asset_type": "power_plant",
        "name": "WAL266",
        "public_id": "ast_wal266",
        "technology": "gas_cc",
        "capacity_mw": 120,
        "status": "operating",
        "state_code": "TX",
        "county_name": "Tarrant",
        "source": {
            "source_name": "EIA-860M",
            "source_url": "https://www.eia.gov/electricity/data/eia860m/",
            "retrieved_at": "2026-10-19T00:00:00Z",
            "reuse_class": "open",
        },
    },
}
_LINE = {
    "type": "Feature",
    "geometry": {"type": "LineString", "coordinates": [[-104.0, 40.0], [-96.0, 40.0]]},
    "properties": {
        "feature_kind": "asset_line",
        "asset_type": "transmission_line",
        "name": "Antelope Valley Station - Red 345 kV",
        "public_id": "ast_line_av",
        "voltage_kv": 345,
    },
}


def _assets_router(points: list[dict[str, Any]], lines: list[dict[str, Any]]) -> Any:
    """`/api/assets/geo` as the map asks it: plants on their own request, the other types on a
    second, retired plants on a third (map.js `assetsGeoUrls`, `retiredGeoUrl`)."""

    def answer(route: Route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)
        if "retired" in query.get("status", [""])[0]:
            features: list[dict[str, Any]] = []
        elif query.get("asset_type") == ["power_plant"]:
            features = points
        else:
            features = lines
        body = {
            "data": {"type": "FeatureCollection", "features": features, "totals": {"records": len(features)}}
        }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    return answer


def _stub_map(
    page: Any, proposals: str, *, points: Any = (), lines: Any = (), regions: str | None = None
) -> None:
    _install_offline_routes(page)
    _ok_ui_events(page)
    page.route(
        "**/api/proposals/geo?**",
        lambda r: r.fulfill(status=200, content_type="application/json", body=proposals),
    )
    page.route("**/api/assets/geo?**", _assets_router(list(points), list(lines)))
    page.route(
        "**/api/geo/regions?**",
        lambda r: r.fulfill(status=200, content_type="application/json", body=regions or _EMPTY_GEO),
    )


def _rendered(*layers: str) -> str:
    return "() => window.__map && " + " && ".join(
        f"window.__map.queryRenderedFeatures({{layers: ['{layer}']}}).length > 0" for layer in layers
    )


def _map_on_top(page: Any) -> None:
    """Scrolls the page so the map's top edge is at the top of the screen (docs/17 §2.1 row 3)."""
    page.evaluate(
        "window.scrollTo(0, document.getElementById('map').getBoundingClientRect().top + window.scrollY)"
    )
    page.wait_for_timeout(100)


def _screen_point(page: Any, lnglat: list[float]) -> list[float]:
    return page.evaluate(
        "(ll) => { const p = window.__map.project(ll);"
        " const r = document.getElementById('map').getBoundingClientRect();"
        " return [r.left + p.x, r.top + p.y]; }",
        lnglat,
    )


_DRAWER_IS_OPEN = "() => document.getElementById('map-drawer').classList.contains('is-open')"
# Its slide-in has run (a fixed wait read the box mid-slide on a loaded machine).
_DRAWER_SETTLED = "() => document.getElementById('map-drawer').getAnimations().length === 0"
# After a tap: the drawer opened, or the page left the map (what a tap on a region used to do).
_LEFT_OR_OPENED = (
    "() => location.pathname !== '/' || document.getElementById('map-drawer').classList.contains('is-open')"
)


def _drawer_open(page: Any) -> bool:
    return bool(page.evaluate(_DRAWER_IS_OPEN))


def test_a_tap_on_a_cluster_lying_over_a_line_only_zooms(server: object) -> None:
    """docs/17 §2.2 / M-2: a tap on a proposal cluster zoomed in and also opened the drawer for the
    transmission line beneath it, which then covered the zoomed map. One tap now does one thing."""
    proposals = _geo_envelope([_cluster(-100.0, 40.0, {"filed": 9}, 900.0, 6)], records=9, clustered=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_context(**PHONE).new_page()
            _stub_map(page, proposals, lines=[_LINE])
            page.goto(BASE_URL + "/?center=-100,40&zoom=4")
            page.wait_for_function(_rendered("clusters", "asset-lines-hit"), timeout=30000)
            _map_on_top(page)
            under = page.evaluate(
                "(ll) => { const p = window.__map.project(ll);"
                " return window.__map.queryRenderedFeatures(p).map((f) => f.layer.id); }",
                [-100.0, 40.0],
            )
            assert "clusters" in under and "asset-lines-hit" in under, under  # the line does lie beneath
            x, y = _screen_point(page, [-100.0, 40.0])
            page.touchscreen.tap(x, y)
            page.wait_for_function("() => window.__map.getZoom() > 5.99 && !window.__map.isMoving()")
            page.wait_for_timeout(300)
            assert not _drawer_open(page), page.inner_text("#map-drawer")
            assert page.evaluate("document.getElementById('map-drawer').hidden") is True
        finally:
            browser.close()


def test_a_tap_on_a_plant_in_a_shaded_county_opens_the_plant_and_stays_on_the_map(server: object) -> None:
    """docs/17 §2.2 / M-2: a tap on power plant WAL266 in Fort Worth opened nothing and left the map
    for `/proposals?county_fips=48439`, because the shaded Tarrant County lay under it."""
    proposals = _geo_envelope([_TARRANT], records=4)
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_context(**PHONE).new_page()
            _stub_map(page, proposals, points=[_PLANT], regions=_TARRANT_POLYGON)
            page.goto(BASE_URL + "/?center=-97.33,32.75&zoom=11")
            page.wait_for_function(_rendered("plant-points", "region-fill"), timeout=30000)
            _map_on_top(page)
            x, y = _screen_point(page, [-97.33, 32.75])
            page.touchscreen.tap(x, y)
            page.wait_for_function(_LEFT_OR_OPENED)
            assert page.evaluate("location.pathname") == "/"
            assert page.inner_text("#map-drawer h2") == "WAL266"
            source = page.inner_text("#map-drawer .drawer-source")  # provenance stays in the drawer
            assert "EIA-860M" in source and "Open licence" in source and "retrieved 19 Oct 2026" in source
        finally:
            browser.close()


def test_a_tap_on_a_region_opens_its_summary_and_the_list_link_and_the_keyboard_reaches_it(
    server: object,
) -> None:
    """docs/17 M-2: a region tap opens the drawer with what the area holds and a link to its list,
    instead of navigating; the region's in-view row opens the same summary from the keyboard (D-14)."""
    proposals = _geo_envelope([_TARRANT], records=4)
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_context(**PHONE).new_page()
            _stub_map(page, proposals, points=[_PLANT], regions=_TARRANT_POLYGON)
            page.goto(BASE_URL + "/?center=-97.33,32.75&zoom=11")
            page.wait_for_function(_rendered("plant-points", "region-fill"), timeout=30000)
            _map_on_top(page)
            x, y = _screen_point(page, [-97.33, 32.70])  # inside the county, 170px from the plant
            page.touchscreen.tap(x, y)
            page.wait_for_function(_LEFT_OR_OPENED)
            assert page.evaluate("location.pathname") == "/"
            assert page.inner_text("#map-drawer h2") == "Tarrant County, TX"
            summary = page.inner_text("#map-drawer .drawer-region")
            assert summary.startswith("4 proposals the source places by county, not at a site."), summary
            assert "3 In process · 1 Contracted · 791.6 MW" in summary, summary
            link = page.locator("#map-drawer .drawer-open-link")
            assert link.inner_text() == "Open the list →"
            assert link.get_attribute("href") == "/proposals?county_fips=48439"
            assert "on the list and on its record" in page.inner_text("#map-drawer .drawer-source")
            # The summary fits the sheet and has no rows that wait: "Expand" would show nothing more.
            # Close stays at the right of the bar without it.
            assert page.locator("#drawer-expand").is_hidden()
            close_left, _t, close_width, _h = page.locator("#drawer-close").evaluate(_BOX)
            assert close_left + close_width >= 390 - 24, (close_left, close_width)

            page.keyboard.press("Escape")
            page.wait_for_function("() => document.getElementById('map-drawer').hidden")
            page.click("#map-panel-toggle")  # a phone's panel is a closed bar under the map
            details = page.get_by_role("button", name="Details for Tarrant County, TX")
            details.focus()
            page.keyboard.press("Enter")
            page.wait_for_function(_DRAWER_IS_OPEN)
            assert page.inner_text("#map-drawer h2") == "Tarrant County, TX"
            assert page.evaluate("document.activeElement.id") == "drawer-close"
            page.keyboard.press("Escape")
            page.wait_for_function("() => document.getElementById('map-drawer').hidden")
            focused = page.evaluate("document.activeElement.getAttribute('aria-label')")
            assert focused == "Details for Tarrant County, TX", focused
            # The dialog is named for what it holds: a region's summary, then a record again.
            assert page.get_attribute("#map-drawer", "aria-label") == "Region summary"
            _map_on_top(page)  # focus went back to the row's Details button, below the map
            x, y = _screen_point(page, [-97.33, 32.75])
            page.touchscreen.tap(x, y)
            page.wait_for_function(_DRAWER_IS_OPEN)
            assert page.inner_text("#map-drawer h2") == "WAL266"
            assert page.get_attribute("#map-drawer", "aria-label") == "Record detail"
        finally:
            browser.close()


_TEXAS = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [-99.3, 31.4]},
    "properties": {
        "feature_kind": "region",
        "region_level": "state",
        "region_id": "US-TX",
        "name": "US-TX",
        "count": 7,
        "lifecycle_state_counts": {"announced": 7},
        "capacity_mw_sum": 0,
    },
}
_TEXAS_POLYGON = json.dumps(
    {
        "data": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-106.6, 25.8], [-93.5, 25.8], [-93.5, 36.5], [-106.6, 36.5], [-106.6, 25.8]]
                        ],
                    },
                    "properties": {"region_id": "US-TX"},
                }
            ],
        }
    }
)


def _regions_by_level(bodies: dict[str, str]) -> Any:
    """`/api/geo/regions` answered per `level`, as map.js asks it (one request per level)."""

    def answer(route: Route) -> None:
        level = parse_qs(urlsplit(route.request.url).query).get("level", [""])[0]
        route.fulfill(status=200, content_type="application/json", body=bodies.get(level, _EMPTY_GEO))

    return answer


_REGION_LEVELS_AT = (
    "(ll) => window.__map.queryRenderedFeatures(window.__map.project(ll), {layers: ['region-fill']})"
    ".map((f) => f.properties.region_level)"
)


def test_a_tap_on_a_county_inside_a_state_placement_opens_the_county(server: object) -> None:
    """Review of lane m-behaviour: where a state is placed as a region too, its near-transparent wash
    lies over the shaded county, and the region tier took the topmost polygon, so a tap or click in
    Calaveras County opened California. The most specific region under the pointer now wins (county
    over state over country); away from the county, the state's wash still opens the state."""
    # The state comes after the county, so its wash is drawn on top: the order that showed the bug.
    proposals = _geo_envelope([_TARRANT, _TEXAS], records=11)
    in_county, in_state = [-97.3, 32.78], [-98.5, 32.75]
    both_drawn = (
        "() => window.__map && new Set(window.__map.queryRenderedFeatures({layers: ['region-fill']})"
        ".map((f) => f.properties.region_level)).size === 2"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            for device, how in ((PHONE, "tap"), ({"viewport": DESKTOP_VIEWPORT}, "click")):
                page = browser.new_context(**device).new_page()
                _stub_map(page, proposals)
                page.route(
                    "**/api/geo/regions?**",
                    _regions_by_level({"state": _TEXAS_POLYGON, "county": _TARRANT_POLYGON}),
                )
                # z6: the county is about 55 px across, and the state point 80 px west of it is on a
                # 358 px phone canvas.
                page.goto(BASE_URL + "/?center=-97.33,32.75&zoom=6")
                page.wait_for_function(both_drawn, timeout=30000)
                _map_on_top(page)
                under = page.evaluate(_REGION_LEVELS_AT, in_county)
                assert under == ["state", "county"], under  # the state's wash is the topmost polygon

                for lnglat, expected in ((in_county, "Tarrant County, TX"), (in_state, "Texas")):
                    x, y = _screen_point(page, lnglat)
                    if how == "tap":
                        page.touchscreen.tap(x, y)
                    else:
                        page.mouse.click(x, y)
                    page.wait_for_function(_LEFT_OR_OPENED)
                    assert page.evaluate("location.pathname") == "/"
                    assert page.inner_text("#map-drawer h2") == expected, (how, lnglat)
                    assert page.get_attribute("#map-drawer", "aria-label") == "Region summary"
                    page.keyboard.press("Escape")
                    page.wait_for_function("() => document.getElementById('map-drawer').hidden")
                page.context.close()
        finally:
            browser.close()


def test_a_country_summary_still_offers_the_country_s_quick_view(server: object) -> None:
    """Before docs/17 M-2 a click on a country placement with a quick-view button of its own moved the
    map there. Its summary now offers the same move, and taking it closes the drawer."""
    country = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-98.5, 39.8]},
        "properties": {
            "feature_kind": "region",
            "region_level": "country",
            "region_id": "US",
            "name": "US",
            "count": 2,
            "lifecycle_state_counts": {"announced": 2},
            "capacity_mw_sum": 0,
        },
    }
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _stub_map(page, _geo_envelope([country], records=2))
            page.goto(BASE_URL + "/?center=-20,30&zoom=3")
            details = page.locator("#in-view-items li[data-group='regions'] .details-btn")
            details.wait_for(timeout=30000)
            details.click()
            page.wait_for_function(_DRAWER_IS_OPEN)
            assert "2 proposals the source places by country, not at a site." in page.inner_text(
                "#map-drawer"
            )
            assert "2 Announced" in page.inner_text("#map-drawer .drawer-region")
            assert page.get_attribute("#map-drawer .drawer-open-link", "href") == "/proposals?jurisdiction=US"
            jump = page.get_by_role("button", name="Show United States on the map")
            jump.click()
            page.wait_for_function("() => document.getElementById('map-drawer').hidden")
            page.wait_for_function("() => location.search.includes('region=us')", timeout=10000)
            page.wait_for_function("() => !window.__map.isMoving()")
            # The middle of the map left of the open panel, which the fit keeps clear (docs/31 §5.19).
            centre = page.evaluate(
                "() => { const m = document.getElementById('map').getBoundingClientRect();"
                " const p = document.getElementById('map-panel').getBoundingClientRect();"
                " return window.__map.unproject([(p.left - m.left) / 2, m.height / 2]).lng; }"
            )
            assert -105 < centre < -90, centre  # the United States quick view, not mid-Atlantic
        finally:
            browser.close()


def _touch_swipe_up(page: Any, cdp: Any, x: float, y: float, distance: float) -> None:
    """One finger, upward, through CDP `Input.dispatchTouchEvent` (docs/17 §1's method)."""
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for step in range(1, 11):
        point = {"x": x, "y": y - distance * step / 10}
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
        page.wait_for_timeout(16)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(500)


_SCROLL_AND_CENTRE = "(name) => [window.scrollY, window[name].getCenter().lng, window[name].getCenter().lat]"


def test_a_one_finger_swipe_on_a_map_scrolls_the_page_on_touch_and_a_mouse_drag_pans(server: object) -> None:
    """docs/17 §2.3 / M-3: a one-finger swipe on the home map panned it 12.7 degrees and scrolled the
    page 0px, and on the 287px company and asset mini-maps the same. On a touch screen one finger now
    scrolls the page (two move the map, with MapLibre's hint in the site's tokens); a mouse drags."""
    proposals = _geo_envelope([_in_view_feature("swipe-one", "solar", -100.0)], records=1)
    spacer = '<div style="height: 1600px"></div></main>'
    mini = _mini_map_page().replace("</main>", spacer)
    mini_url = BASE_URL + "/e2e-mini-map"
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            context = browser.new_context(**PHONE)
            page = context.new_page()
            _stub_map(page, proposals)
            page.goto(BASE_URL + "/?center=-100,40&zoom=4")
            page.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            _map_on_top(page)
            box = page.locator("#map").bounding_box()
            before = page.evaluate(_SCROLL_AND_CENTRE, "__map")
            _touch_swipe_up(
                page, context.new_cdp_session(page), box["x"] + box["width"] / 2, box["y"] + 400, 250
            )
            after = page.evaluate(_SCROLL_AND_CENTRE, "__map")
            assert after[0] - before[0] > 150, (before, after)  # the page scrolled
            assert after[1:] == before[1:], (before, after)  # the map did not move
            assert page.evaluate("window.__map.cooperativeGestures.isEnabled()") is True
            # MapLibre's hint, in the site's tokens: the words on a surface label, no black wash.
            hint = page.evaluate(
                "() => { const s = document.querySelector('#map .maplibregl-cooperative-gesture-screen');"
                " const m = s.querySelector('.maplibregl-mobile-message');"
                " const probe = document.createElement('div'); probe.style.background = 'var(--surface)';"
                " document.body.appendChild(probe); const surface = getComputedStyle(probe).backgroundColor;"
                " probe.remove();"
                " return [getComputedStyle(s).backgroundColor, getComputedStyle(m).backgroundColor, surface,"
                " getComputedStyle(m).display, m.textContent, s.getAttribute('aria-hidden')]; }"
            )
            wash, label, surface, display, words, hidden = hint
            assert wash == "rgba(0, 0, 0, 0)" and label == surface and display == "block", hint
            assert words == "Use two fingers to move the map" and hidden == "true", hint

            mini_page = context.new_page()
            _install_offline_routes(mini_page)
            mini_page.route(mini_url, lambda r: r.fulfill(content_type="text/html", body=mini))
            mini_page.goto(mini_url)
            mini_page.wait_for_function(
                "() => window.__assetMap && window.__assetMap.loaded()", timeout=15000
            )
            mbox = mini_page.locator("#asset-map").bounding_box()
            before = mini_page.evaluate(_SCROLL_AND_CENTRE, "__assetMap")
            cdp = context.new_cdp_session(mini_page)
            _touch_swipe_up(
                mini_page, cdp, mbox["x"] + mbox["width"] / 2, mbox["y"] + mbox["height"] - 20, 200
            )
            after = mini_page.evaluate(_SCROLL_AND_CENTRE, "__assetMap")
            assert after[0] - before[0] > 100, (before, after)
            assert after[1:] == before[1:], (before, after)
            assert mini_page.evaluate("window.__assetMap.cooperativeGestures.isEnabled()") is True
            context.close()

            # A mouse on a desktop drags the map, as before; nothing is cooperative there.
            desk = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _stub_map(desk, proposals)
            desk.goto(BASE_URL + "/?center=-100,40&zoom=4")
            desk.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            assert desk.evaluate("window.__map.cooperativeGestures.isEnabled()") is False
            dbox = desk.locator("#map").bounding_box()
            before = desk.evaluate(_SCROLL_AND_CENTRE, "__map")
            x, y = dbox["x"] + dbox["width"] * 0.7, dbox["y"] + dbox["height"] * 0.6
            desk.mouse.move(x, y)
            desk.mouse.down()
            desk.mouse.move(x - 200, y - 100, steps=10)
            desk.mouse.up()
            desk.wait_for_timeout(300)
            after = desk.evaluate(_SCROLL_AND_CENTRE, "__map")
            assert after[0] == before[0], (before, after)
            assert after[1] - before[1] > 5, (before, after)  # dragged left: the centre moved east
        finally:
            browser.close()


def test_the_panel_header_says_what_is_in_view_by_status_and_capacity(server: object) -> None:
    """docs/17 §2.4 / M-5: a cluster's status breakdown and capacity sum were on a hover card only,
    and a touch screen never shows one. The panel's header (owner, 2026-10-22) says them for
    everything in view, on a phone's closed bar too: how many projects (a cluster for its count, a
    point for one, a shaded county for the projects placed there) and their stated capacity, then a
    bar by status family in the map's colours with the same counts in words."""
    proposals = _geo_envelope(
        [
            _cluster(-100.0, 40.0, {"filed": 25, "studied": 5, "announced": 10}, 5000.0, 6),
            _in_view_feature("header-one", "solar", -101.0),
            _in_view_feature("header-two", "solar", -99.0),
            _TARRANT,
        ],
        records=46,
        clustered=False,
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_context(**PHONE).new_page()
            _stub_map(page, proposals)
            page.goto(BASE_URL + "/?center=-100,39&zoom=5")
            count = page.locator("#map-panel-count")
            page.wait_for_function(
                "() => /in view/.test(document.getElementById('map-panel-count').textContent)", timeout=30000
            )
            # 40 in the cluster, two points, the county's four; 5,000 + 2 x 12 + 791.6 MW.
            assert count.inner_text() == "46 projects in view · 5.8 GW"
            assert count.get_attribute("aria-live") is None  # the live region announces moves
            assert page.locator("#map-panel-toggle").is_visible()  # the phone's bar, under the map
            page.click("#map-panel-toggle")
            # In the Status filter's own words, as the key and the overview name them (review
            # 2026-10-22: "announced or unknown" and "built" were the header's own).
            assert " ".join(page.inner_text("#map-panel-families").split()) == (
                "10 Announced 35 In process 1 Contracted"
            )
            assert page.locator("#map-panel-families svg use").evaluate_all(
                "(uses) => uses.map((u) => u.getAttribute('href'))"
            ) == ["#icon-neutral", "#icon-progress", "#icon-committed"]
            segments = page.locator("#map-panel-bar .map-panel__seg").evaluate_all(
                "(s) => s.map((x) => [x.className.split('--')[1], x.style.flexGrow])"
            )
            assert segments == [["neutral", "10"], ["progress", "35"], ["committed", "1"]], segments
        finally:
            browser.close()


def test_with_no_view_in_the_link_the_map_fits_the_lower_48_and_reset_returns_there(server: object) -> None:
    """docs/17 §2.1 / M-7: the map opened at z3.2 on -98.5, 39.8, which on a 358px canvas showed North
    Dakota to the Gulf with both coasts cut off. It now fits the contiguous US with 16px to spare (and,
    where the panel lies over the canvas, clear of it: the open panel's width on the right, the collapsed
    one's header at the top; owner, 2026-10-22); "Reset" fits it again; the URL stays clean."""
    conus = {"west": -124.8, "south": 24.4, "east": -66.9, "north": 49.4}
    bounds = (
        "() => { const b = window.__map.getBounds(); return {west: b.getWest(), south: b.getSouth(),"
        " east: b.getEast(), north: b.getNorth(), zoom: window.__map.getZoom()}; }"
    )
    proposals = _geo_envelope([_in_view_feature("fit-one", "solar", -100.0)], records=1)
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_context(**PHONE).new_page()
            _stub_map(page, proposals)
            page.goto(BASE_URL + "/")
            page.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            home = page.evaluate(bounds)
            assert home["west"] <= conus["west"] and home["east"] >= conus["east"], home
            assert home["south"] <= conus["south"] and home["north"] >= conus["north"], home
            assert 1.8 < home["zoom"] < 2.3, home  # docs/17: "about z 2.1 at 358 px"
            page.evaluate("window.__map.jumpTo({center: [-80, 35], zoom: 6})")
            page.wait_for_function("() => location.search.includes('zoom=6.00')", timeout=10000)
            # On a phone Reset is "Whole map", in "Jump to" (docs/17 M-1).
            page.click("#map-jump-toggle")
            page.click("#map-jump-home")
            page.wait_for_function("() => !location.search.includes('center=')", timeout=10000)
            assert abs(page.evaluate(bounds)["zoom"] - home["zoom"]) < 0.01

            wide = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _stub_map(wide, proposals)
            wide.goto(BASE_URL + "/")
            wide.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            view = wide.evaluate(bounds)
            assert view["west"] <= conus["west"] and view["east"] >= conus["east"], view
            # The open panel lies over the canvas, top right (owner, 2026-10-22); the East Coast is
            # drawn clear of it.
            coast, panel_left = wide.evaluate(
                "() => { const r = document.getElementById('map').getBoundingClientRect();"
                " const k = document.getElementById('map-panel').getBoundingClientRect();"
                " return [r.left + window.__map.project([-66.9, 44.8]).x, k.left]; }"
            )
            assert coast < panel_left, (coast, panel_left)
            assert "center=" not in wide.url

            # At 1024x768 the panel starts collapsed to its header, and Maine is drawn under the header,
            # not behind it. On the untouched view, opening the panel fits the map clear of it, and
            # collapsing it fits the map under the header again.
            mid = browser.new_page(viewport={"width": 1024, "height": 768})
            _stub_map(mid, proposals)
            mid.goto(BASE_URL + "/")
            mid.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            maine_under_header = (
                "() => { const r = document.getElementById('map').getBoundingClientRect();"
                " const k = document.getElementById('map-panel').getBoundingClientRect();"
                " return r.top + window.__map.project([-68.9, 47.4]).y > k.bottom; }"
            )
            east_clear = (
                "() => { const r = document.getElementById('map').getBoundingClientRect();"
                " const k = document.getElementById('map-panel').getBoundingClientRect();"
                " return r.left + window.__map.project([-66.9, 44.8]).x < k.left; }"
            )
            assert mid.get_attribute("#map-panel-toggle", "aria-expanded") == "false"
            assert mid.evaluate(maine_under_header)
            collapsed = mid.evaluate(bounds)["zoom"]
            for press, open_after in ((1, True), (2, False)):
                mid.click("#map-panel-toggle")
                mid.wait_for_function("() => !window.__map.isMoving()")
                mid.wait_for_timeout(400)  # past the URL's 300 ms debounce
                assert (mid.get_attribute("#map-panel-toggle", "aria-expanded") == "true") is open_after, (
                    press
                )
                assert "center=" not in mid.url, (press, mid.url)
                zoom = mid.evaluate(bounds)["zoom"]
                if open_after:
                    assert zoom < collapsed - 0.02, (collapsed, zoom)  # the panel's band is not map
                    assert mid.evaluate(east_clear)
            assert abs(zoom - collapsed) < 0.01, (collapsed, zoom)
            assert mid.evaluate(maine_under_header)
        finally:
            browser.close()


def test_a_newer_view_aborts_the_asset_requests_of_the_one_it_replaces(server: object) -> None:
    """docs/17 §1: under a fast zoom the asset and retired-plant requests for views already left kept
    running, and the one API worker answered `/api/assets/geo` with 503. As the proposals request
    already was, each is now aborted when a newer view supersedes it, without a word on the page."""
    proposals = _geo_envelope([_in_view_feature("abort-one", "solar", -100.0)], records=1)
    held: list[Route] = []
    failed: list[str] = []

    def assets(route: Route) -> None:
        if parse_qs(urlsplit(route.request.url).query).get("zoom") == ["4"]:
            held.append(route)  # the first view's requests: never answered, so only an abort ends them
            return
        route.fulfill(status=200, content_type="application/json", body=_EMPTY_GEO)

    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _stub_map(page, proposals)
            page.route("**/api/assets/geo?**", assets)
            page.on(
                "requestfailed",
                lambda r: failed.append(r.url) if "/api/assets/geo" in r.url else None,
            )
            page.goto(BASE_URL + "/?center=-100,40&zoom=4")
            page.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            deadline = time.monotonic() + 15
            while len(held) < 3 and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            # Existing assets: plants and the other types on two requests; retired plants on a third.
            assert len(held) == 3, [r.request.url for r in held]
            page.evaluate("window.__map.jumpTo({center: [-90, 35], zoom: 7})")
            deadline = time.monotonic() + 15
            while len(failed) < 3 and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            assert sorted(failed) == sorted(r.request.url for r in held)
            assert page.locator("#map-error").is_hidden()
            for route in held:
                with contextlib.suppress(PlaywrightError):
                    route.fulfill(status=200, content_type="application/json", body=_EMPTY_GEO)
        finally:
            browser.close()


# ============================================================================================
# Mobile review 2026-10-21 (docs/17), lane m-layout: the map's controls in one right-edge column and
# the key under the map (M-1), the drawer as a bottom sheet (M-4), the map into the first screen
# (M-6, M-15's fold budget) and the filter disclosures as rows (M-11).

#: The map top at 390x844 (docs/17 M-6, M-15). docs/17 measured 512px on the live site (the key,
#: lede and notice above the map); the e2e store measured 493px before this lane and 360px after it,
#: with the current masthead (the 28px freshness line and the 67px header bar, base.html, are
#: another lane's) and without the dev-preview label, which production never shows. docs/17's target
#: is 300px; the masthead line and the header bar are what remains between the two. 8px of slack for
#: a fallback font on a first load (`font-display: optional`). Owner, 2026-10-22: the map's search
#: field is a 44px bar above the canvas on a phone (docs/31 §5.18), and the view switch left the line
#: under the count, so the map starts 44px lower: 404px measured on the dev store. Owner, 2026-10-23
#: ("a site link that will do well when it lands in a contact's inbox"): the opening figures
#: (web/headline.py) take one 28px line under the lede on a phone, so 432px measured, budget 440.
PHONE_MAP_TOP_BUDGET = 440
#: Share of the visible map any control or panel covers: docs/17 asks for under 6%, open or closed.
#: The column is 44x200px on a 358x490 canvas, 4.6% of it; 55.6% on the live site before.
PHONE_MAP_COVER_BUDGET = 6.0

# The share (percent) of the on-screen part of the map that something other than the map covers,
# sampled every 4px (docs/17 §1's method, at 2px there).
_MAP_COVERED = """() => {
  const m = document.getElementById('map').getBoundingClientRect();
  const top = Math.max(m.top, 0), bottom = Math.min(m.bottom, innerHeight);
  let n = 0, hit = 0;
  for (let y = top + 1; y < bottom; y += 4) for (let x = m.left + 1; x < m.right; x += 4) {
    n++;
    const el = document.elementFromPoint(x, y);
    if (el && !el.closest('#map')) hit++;
  }
  return 100 * hit / n;
}"""
_BOX = "(el) => { const b = el.getBoundingClientRect(); return [b.left, b.top, b.width, b.height]; }"
#: Several elements' boxes read in one frame, so a scroll or animation between reads cannot skew a comparison.
_BOXES = (
    "(sels) => sels.map((s) => { const b = document.querySelector(s).getBoundingClientRect();"
    " return [b.left, b.top, b.width, b.height]; })"
)
_DROP_PREVIEW_LABEL = "() => { const b = document.querySelector('.preview-banner'); if (b) b.remove(); }"
#: A map-bar summary's marker: no native one (list-item or `::-webkit-details-marker`), and the
#: chevron its `::after` draws (first in its flex row), whose turn says closed or open.
_MARKER = (
    "(s) => { const b = getComputedStyle(s, '::after');"
    " return [getComputedStyle(s).listStyleType, b.borderRightStyle, b.transform]; }"
)


def _phone_page(browser: Any, **extra: Any) -> Any:
    page = browser.new_context(**{**PHONE, **extra}).new_page()
    _install_offline_routes(page)
    _ok_ui_events(page)
    return page


def test_phone_fold_budget_the_map_starts_high_with_one_sentence_and_one_status_line(server: object) -> None:
    """docs/17 §3.1 / M-6, M-15: at 390x844 the map started at 512px, under a six-line lede, the count
    and a three-line notice. Below 720px the lede is one sentence ("About this map" opens the rest,
    which says "Tap" on a touch screen), the count and the notice's short form share one line ("Why?"
    opens the notice, and it stays open across a filter change), and "Open these in the list" and the
    save link share the next. The map's search field is a full-width bar above the canvas (owner,
    2026-10-22). From 720px the page reads as before."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = _phone_page(browser)
            page.goto(BASE_URL + "/")
            page.wait_for_function(_COUNT_READY, timeout=30000)
            page.evaluate("document.fonts.ready.then(() => true)")
            page.evaluate(_DROP_PREVIEW_LABEL)
            top = page.evaluate("document.getElementById('map').getBoundingClientRect().top")
            assert top <= PHONE_MAP_TOP_BUDGET, f"map starts at {top}px at 390x844"
            assert page.evaluate("document.documentElement.scrollWidth") <= 390

            lede = page.locator(".map-intro__lede")
            about = page.locator("#map-intro-about")
            assert " ".join(lede.inner_text().split()) == (
                "Projects from public grid-queue and permit filings in the US and GB,"
                " matched across registers. About this map"
            )
            assert about.get_attribute("aria-expanded") == "false"
            about.click()
            assert about.get_attribute("aria-expanded") == "true"
            text = " ".join(lede.inner_text().split())
            assert "matched across registers: every claim linked to its source" in text, text
            assert "Tap a cluster to zoom in, or a dot to open its record." in text, text
            about.click()
            assert "every claim" not in lede.inner_text()

            count, brief = page.locator("#map-result-count"), page.locator(".map-notice__brief")
            count_box, brief_box = count.evaluate(_BOX), brief.evaluate(_BOX)
            middles = [count_box[1] + count_box[3] / 2, brief_box[1] + brief_box[3] / 2]
            assert abs(middles[0] - middles[1]) <= 2, (count_box, brief_box)  # one line
            assert count_box[3] <= 24 and brief_box[3] <= 26, (count_box, brief_box)
            assert " ".join(brief.inner_text().split()) == "Active only. Why?"
            detail, why = page.locator(".map-notice__detail"), page.locator(".map-notice__why")
            assert detail.is_hidden() and why.get_attribute("aria-expanded") == "false"
            why.click()
            assert detail.is_visible() and "Showing" in detail.inner_text()
            assert why.get_attribute("aria-expanded") == "true"
            # A filter change refills the notice; it stays open, and its new button says so.
            page.click("[data-filter-toggle]")
            with page.expect_response(lambda r: "/api/proposals/notice?" in r.url):
                page.select_option("#mf-technology", "solar")
            page.wait_for_function(
                "() => document.querySelector('.map-notice__why').getAttribute('aria-expanded') === 'true'"
            )
            assert page.locator(".map-notice__detail").is_visible()
            page.locator("[data-filter-done]").first.click()

            # "Open these in the list" and the save link share the next line (owner, 2026-10-22: the
            # view switch left the map).
            save, listed = page.locator("#mf-save-alert"), page.locator("#map-list-link")
            save_box, list_box = save.evaluate(_BOX), listed.evaluate(_BOX)
            middles = [save_box[1] + save_box[3] / 2, list_box[1] + list_box[3] / 2]
            assert abs(middles[0] - middles[1]) <= 2, (save_box, list_box)
            assert page.locator(".view-switch").count() == 0

            # 360px, the floor docs/04 D-32 now names: nothing scrolls sideways.
            narrow = _phone_page(browser, viewport={"width": 360, "height": 800})
            narrow.goto(BASE_URL + "/")
            narrow.wait_for_function(_COUNT_READY, timeout=30000)
            assert narrow.evaluate("document.documentElement.scrollWidth") <= 360
            assert narrow.locator("#map-key-toggle").is_visible()

            # From 720px: the whole lede with "Click", the notice in full, no short forms.
            wide = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(wide)
            _ok_ui_events(wide)
            wide.goto(BASE_URL + "/")
            wide.wait_for_function(_COUNT_READY, timeout=30000)
            wide_lede = " ".join(wide.locator(".map-intro__lede").inner_text().split())
            how_to = "moves. Click a cluster to zoom in, or a dot to open its record."
            assert wide_lede.endswith(how_to), wide_lede
            assert wide.locator("#map-intro-about").is_hidden()
            assert wide.locator(".map-notice__brief").is_hidden()
            assert "Showing" in wide.locator(".map-notice__detail").inner_text()
        finally:
            browser.close()


def test_phone_coverage_note_shows_without_why(server: object) -> None:
    """Review 2026-10-22 (lane m-layout): the note that a view's ISO queue is not covered, and that
    its rows are EIA-860M units (expert review 2026-10-07), sat behind "Why?", so on a phone
    `/?iso=PJM` read "Active only. Why?" and nothing more. It is provenance (docs/17 §6, D-2/D-34),
    so it shows on its own line under the count and the short form; "Why?" opens only the rest."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = _phone_page(browser)
            page.goto(BASE_URL + "/?iso=PJM")
            page.wait_for_function(_COUNT_READY, timeout=30000)
            gap = page.locator("#map-notice .view-notice--gap")
            assert gap.count() == 1 and gap.is_visible()
            assert "come from EIA-860M" in gap.inner_text()
            brief = page.locator(".map-notice__brief")
            assert " ".join(brief.inner_text().split()) == "Active only. Why?"
            assert page.locator(".map-notice__detail").is_hidden()
            brief_box, gap_box = brief.evaluate(_BOX), gap.evaluate(_BOX)
            assert gap_box[1] >= brief_box[1] + brief_box[3] - 1, (brief_box, gap_box)
            assert page.evaluate("document.documentElement.scrollWidth") <= 390
        finally:
            browser.close()


def test_phone_map_controls_are_one_right_edge_column_and_the_key_opens_under_the_map(server: object) -> None:
    """docs/17 §2.1 / M-1 (b)-(d): at 390x844 three rows of buttons and the key covered 38-56% of the
    map, and the key held 743px of layer keys in a 142px window. Below 720px the controls are one
    column of 44x44 buttons on the right edge (zoom in, zoom out, "Jump to" with the regions and
    "Whole map", Key); "Key" opens the panel's Key tab under the map frame (owner, 2026-10-22). From
    720px the controls are as before and the panel floats over the map's top right."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = _phone_page(browser)
            page.goto(BASE_URL + "/")
            page.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            page.wait_for_function(_COUNT_READY, timeout=30000)  # the data, which grows the key
            _map_on_top(page)
            map_box = page.locator("#map").evaluate(_BOX)
            ids = ("zoom-in", "zoom-out", "map-jump-toggle", "map-key-toggle")
            column = [page.locator(f"#{i}").evaluate(_BOX) for i in ids]
            for left, _top, width, height in column:
                assert abs(width - 44) < 0.6 and abs(height - 44) < 0.6, column
                assert abs(left + width - (map_box[0] + map_box[2] - 8)) <= 1.5, (column, map_box)
            assert [b[1] for b in column] == sorted(b[1] for b in column)
            assert page.locator("#zoom-reset").is_hidden()
            assert all(not b.is_visible() for b in page.locator(".region-btn").all())
            covered = page.evaluate(_MAP_COVERED)
            assert covered <= PHONE_MAP_COVER_BUDGET, f"{covered:.1f}% of the map covered, key closed"

            # "Jump to": the region jumps and "Whole map" beside the column; Escape closes and returns.
            jump = page.locator("#map-jump-toggle")
            jump.click()
            assert jump.get_attribute("aria-expanded") == "true"
            choices = page.locator("#map-regions button")
            labels = [b.inner_text() for b in choices.all()]
            assert len(labels) >= 2 and labels[-1] == "Whole map", labels
            for b in choices.all():
                left, _top, _width, height = b.evaluate(_BOX)
                assert height >= 43.5 and left < column[2][0], labels
            page.keyboard.press("Escape")
            assert jump.get_attribute("aria-expanded") == "false" and choices.first.is_hidden()
            assert page.evaluate("document.activeElement.id") == "map-jump-toggle"
            jump.click()
            choices.first.click()
            page.wait_for_function("() => location.search.includes('region=')", timeout=10000)
            assert jump.get_attribute("aria-expanded") == "false"
            assert page.evaluate("document.activeElement.id") == "map-jump-toggle"

            # "Key": the panel opens under the map on its Key tab, in view; nothing more of the map
            # is covered, and nothing in it scrolls inside (owner, 2026-10-22: on a phone the panel
            # does not float).
            page.locator("#map-key-toggle").click()
            page.wait_for_function(_KEY_IN_VIEW)
            map_bottom, key_top = page.evaluate(_MAP_BOTTOM_AND_KEY_TOP)
            assert key_top >= map_bottom, (key_top, map_bottom)
            assert page.get_attribute("#map-tab-key", "aria-selected") == "true"
            scroll = page.locator("#map-panel-scroll")
            assert scroll.evaluate("(s) => s.scrollHeight <= s.clientHeight + 1")  # in the page, not a window
            _map_on_top(page)
            covered = page.evaluate(_MAP_COVERED)
            assert covered <= PHONE_MAP_COVER_BUDGET, f"{covered:.1f}% of the map covered, key open"

            # From 720px: Reset and the region row on the map, no "Jump to" and no rail "Key"; the
            # panel floats over the map's top right with the key as its second tab.
            wide = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(wide)
            _ok_ui_events(wide)
            wide.goto(BASE_URL + "/")
            wide.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            assert wide.locator("#zoom-reset").is_visible() and wide.locator(".region-btn").first.is_visible()
            assert wide.locator("#map-jump-toggle").is_hidden() and wide.locator("#map-jump-home").is_hidden()
            assert wide.locator("#map-key-toggle").is_hidden()
            wide_map = wide.locator("#map").evaluate(_BOX)
            panel = wide.locator("#map-panel").evaluate(_BOX)
            assert wide_map[1] < panel[1] < wide_map[1] + wide_map[3], (panel, wide_map)
            assert abs(panel[0] + panel[2] - (wide_map[0] + wide_map[2] - 12)) <= 2, (panel, wide_map)
        finally:
            browser.close()


#: A proposal 195px below the centre of a z4 view on -100, 40 (Mercator), near the canvas's foot.
_LOW_POINT = [-100.0, 33.13]
_CENTRE = "() => [window.__map.getCenter().lng, window.__map.getCenter().lat]"


def test_phone_drawer_is_a_bottom_sheet_that_keeps_the_tapped_point_in_view(server: object) -> None:
    """docs/17 §2.4 / M-4: the drawer was a 351x844 side panel over 93% of the map, the tapped point
    under it. Below 720px it is a bottom sheet of at most 45% of the screen holding the name, status,
    capacity, place, source line and record link; "Expand" takes it to the full height; the map eases
    a tapped point that lies under it into the part left showing; Close is 44x44; dialog, focus trap
    and focus return hold. From 720px it opens in the panel's place, and the map does not move."""
    feature = _feature_with_source("sheet-one", _LOW_POINT[0])
    feature["geometry"]["coordinates"] = _LOW_POINT
    proposals = _geo_envelope([feature], records=1)
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_context(**PHONE).new_page()
            _stub_map(page, proposals)
            page.goto(BASE_URL + "/?center=-100,40&zoom=4")
            page.wait_for_function(_rendered("points"), timeout=30000)
            # The map's top 200px down the screen: the point is near its foot, about 640px down.
            page.evaluate(
                "window.scrollTo(0,"
                " document.getElementById('map').getBoundingClientRect().top + scrollY - 200)"
            )
            page.wait_for_timeout(200)
            x, y = _screen_point(page, _LOW_POINT)
            page.touchscreen.tap(x, y)
            page.wait_for_function(_DRAWER_IS_OPEN)
            page.wait_for_timeout(250)  # past the 200ms slide-in
            sheet = page.locator("#map-drawer")
            left, top, width, height = sheet.evaluate(_BOX)
            assert left == 0 and width == 390 and abs(top + height - 844) <= 1, (left, top, width, height)
            assert height <= 0.45 * 844 + 1, height
            assert y > top, (y, top)  # the sheet did open over the point
            assert sheet.get_attribute("role") == "dialog" and sheet.get_attribute("aria-modal") == "true"
            assert page.evaluate("document.activeElement.id") == "drawer-close"
            close_box = page.locator("#drawer-close").evaluate(_BOX)
            assert close_box[2] >= 44 and close_box[3] >= 44, close_box
            # Its first state: name, status, capacity, place, the source line and the record link, all
            # inside the sheet; the Technology row waits for "Expand".
            assert page.inner_text("#map-drawer h2") == "sheet-one"
            assert page.locator("#map-drawer .chip").is_visible()
            rows = page.locator("#map-drawer .drawer-fields__row")
            shown_rows = [r.inner_text().split("\n")[0] for r in rows.all() if r.is_visible()]
            assert shown_rows == ["Capacity", "Location"], shown_rows
            for selector in (".drawer-source", ".drawer-open-link"):
                box = page.locator(f"#map-drawer {selector}").evaluate(_BOX)
                assert box[1] >= top and box[1] + box[3] <= top + height + 1, (selector, box, top, height)
            source = page.inner_text("#map-drawer .drawer-source")
            assert "Virginia DEQ air permits" in source and "Open licence" in source and "retrieved" in source
            # The map eased the point into the part of it left showing above the sheet.
            page.wait_for_function("() => !window.__map.isMoving()")
            page.wait_for_timeout(700)
            _px, py = _screen_point(page, _LOW_POINT)
            map_top = page.evaluate("document.getElementById('map').getBoundingClientRect().top")
            assert max(map_top, 0) < py <= top - 24, (py, map_top, top)

            # Focus stays in the dialog: Shift+Tab from Close is "Expand"; Tab from the last link wraps.
            page.keyboard.press("Shift+Tab")
            assert page.evaluate("document.activeElement.id") == "drawer-expand"
            page.locator("#map-drawer .drawer-open-link").focus()
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id") == "drawer-expand"
            # "Expand": the full height, the Technology row too; "Collapse" goes back. A disclosure,
            # so its state is `aria-expanded` as well as its words.
            expand = page.locator("#drawer-expand")
            assert expand.get_attribute("aria-expanded") == "false"
            page.keyboard.press("Enter")
            _l, top2, _w, height2 = sheet.evaluate(_BOX)
            assert top2 == 0 and abs(height2 - 844) <= 1, (top2, height2)
            assert page.locator("#map-drawer .drawer-fields__row--more").is_visible()
            assert expand.inner_text() == "Collapse" and expand.get_attribute("aria-expanded") == "true"
            page.keyboard.press("Enter")
            assert sheet.evaluate(_BOX)[3] <= 0.45 * 844 + 1
            assert expand.get_attribute("aria-expanded") == "false"
            page.keyboard.press("Escape")
            page.wait_for_function("() => document.getElementById('map-drawer').hidden")
            assert sheet.get_attribute("aria-modal") is None

            # From 720px: the drawer opens in the panel's place over the map (owner, 2026-10-22), with
            # no "Expand", and the map stays where it was.
            wide = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _stub_map(wide, proposals)
            wide.goto(BASE_URL + "/?center=-100,40&zoom=4")
            wide.wait_for_function(_rendered("points"), timeout=30000)
            _map_on_top(wide)
            centre = wide.evaluate(_CENTRE)
            slot = wide.locator("#map-panel").evaluate(_BOX)
            x, y = _screen_point(wide, _LOW_POINT)
            wide.mouse.click(x, y)
            wide.wait_for_function(_DRAWER_IS_OPEN)
            wide.wait_for_timeout(700)
            panel = wide.locator("#map-drawer").evaluate(_BOX)
            assert [round(v) for v in panel[:3]] == [round(v) for v in slot[:3]], (panel, slot)
            assert wide.locator("#drawer-expand").is_hidden()
            assert wide.locator("#map-drawer .drawer-fields__row--more").is_visible()
            assert wide.evaluate(_CENTRE) == centre
            wide.keyboard.press("Shift+Tab")  # from Close: "Back to what's in view", in the dialog
            assert wide.evaluate("!!document.activeElement.closest('#map-drawer')")
            assert wide.evaluate("document.activeElement.id") == "drawer-back"
        finally:
            browser.close()


def test_phone_map_filter_disclosures_are_full_width_rows_and_show_the_map_is_at_the_top(
    server: object,
) -> None:
    """docs/17 §2.5 / M-11: in the phone column "Status: active", "Asset types" and "Location
    precision" sat at the right edge with no marker, and "Show the map" was at the foot of a 535px
    block. Below 720px they are full-width rows, left-aligned, each with its marker, and "Show the
    map" is at the top of the open bar as well. From 720px the bar is as before."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = _phone_page(browser)
            page.goto(BASE_URL + "/")
            page.wait_for_function(_COUNT_READY, timeout=30000)
            toggle = page.locator("[data-filter-toggle]")
            toggle.click()
            form = page.locator("#map-filters")
            form_left, form_top, form_width, _h = form.evaluate(_BOX)
            first_done = page.locator("#map-filters [data-filter-done]").first
            done_box = first_done.evaluate(_BOX)
            assert first_done.is_visible() and done_box[1] - form_top <= 16, (done_box, form_top)
            summaries = [
                s for s in page.locator("#map-filters .filter-more > summary").all() if s.is_visible()
            ]
            assert len(summaries) >= 2
            turns = set()
            for summary in summaries:
                left, _top, width, height = summary.evaluate(_BOX)
                where = (summary.inner_text(), left, width)
                assert abs(left - form_left) <= 1 and abs(width - form_width) <= 1, where
                assert height >= 44
                list_style, chevron, turn = summary.evaluate(_MARKER)
                assert list_style == "none" and chevron == "solid", (list_style, chevron)
                turns.add(turn)
            assert len(turns) == 1 and "none" not in turns, turns  # every closed one points the same way
            (closed_turn,) = turns
            # Open, the chevron turns (points down), and back when closed.
            summaries[0].click()
            assert summaries[0].evaluate(_MARKER)[2] not in ("none", closed_turn)
            summaries[0].click()
            assert summaries[0].evaluate(_MARKER)[2] == closed_turn
            first_done.click()
            assert form.is_hidden()
            assert page.evaluate("document.activeElement.hasAttribute('data-filter-toggle')")

            # 720-1079px: the bar opens as before, "Show the map" at its foot only.
            tablet = browser.new_page(viewport={"width": 1024, "height": 768})
            _install_offline_routes(tablet)
            _ok_ui_events(tablet)
            tablet.goto(BASE_URL + "/")
            tablet.locator("[data-filter-toggle]").click()
            done = tablet.locator("#map-filters [data-filter-done]")
            assert [d.is_visible() for d in done.all()] == [False, True]
            # 1440px: the disclosures are links in the bar's row, not full-width rows.
            wide = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(wide)
            _ok_ui_events(wide)
            wide.goto(BASE_URL + "/")
            width = wide.locator("#mf-more > summary").evaluate("(s) => s.getBoundingClientRect().width")
            assert width < 300, width
        finally:
            browser.close()


# ============================================================================================
# Owner, 2026-10-22 (docs/00-PLAN.md; docs/31 §5.18, §5.19): the map takes the full width, one panel
# floating over its top right holds what is in view and the key, "In view" follows the zoom, the
# drawer opens in the panel's place, a phone's panel is a bar under the map, the search field finds a
# project, a company or a place, every Status box can be unticked, and a touch never leaves a hover
# card on the map.

#: An element's box by id: left, top, width, height.
_RECT = (
    "(id) => { const b = document.getElementById(id).getBoundingClientRect();"
    " return [b.left, b.top, b.width, b.height]; }"
)
#: Every visible run of text in the panel, with the colour it is drawn in, and the panel's own ground.
_PANEL_WORDS = """() => {
  const panel = document.getElementById('map-panel');
  const words = [];
  const walker = document.createTreeWalker(panel, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const el = node.parentElement;
    if (!node.textContent.trim() || !el || el.closest('[aria-hidden="true"], svg, [hidden]')) continue;
    if (!el.getClientRects().length) continue;
    words.push([node.textContent.trim().slice(0, 40), getComputedStyle(el).color]);
  }
  return {ground: getComputedStyle(panel).backgroundColor, words: words};
}"""


def _css_rgba(css_colour: str) -> tuple[tuple[float, float, float], float]:
    """`rgb()`, `rgba()` or `color(srgb r g b / a)` as 0-255 channels and an alpha."""
    if css_colour.startswith("color(srgb"):
        values = [float(v) for v in re.findall(r"[\d.]+", css_colour.split("srgb", 1)[1])]
        rgb = (values[0] * 255, values[1] * 255, values[2] * 255)
        return rgb, values[3] if len(values) > 3 else 1.0
    values = [float(v) for v in re.findall(r"[\d.]+", css_colour)]
    return (values[0], values[1], values[2]), values[3] if len(values) > 3 else 1.0


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(c):02x}" for c in rgb)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_map_panel_text_is_readable_over_any_basemap_in_both_themes(server: object, scheme: str) -> None:
    """Owner, 2026-10-22: the panel is a translucent box over the map. Measured on the page, as the
    hover card is: its ground is --surface at under 100% opacity, and every word on it, in both tabs,
    keeps 4.5:1 or more against that ground laid over black and over white, the darkest and the
    lightest a basemap could put behind it."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT, color_scheme=scheme)
            _install_offline_routes(page)
            _ok_ui_events(page)
            page.goto(BASE_URL + "/")
            page.wait_for_function(_COUNT_READY, timeout=30000)
            page.wait_for_function(
                "() => document.querySelectorAll('#map-overview-areas .map-overview__area').length > 0",
                timeout=30000,
            )
            seen = page.evaluate(_PANEL_WORDS)
            page.click("#map-tab-key")
            key = page.evaluate(_PANEL_WORDS)
        finally:
            browser.close()
    ground, alpha = _css_rgba(seen["ground"])
    assert 0.8 <= alpha < 1, seen["ground"]  # translucent, and mostly the surface
    assert seen["words"] and key["words"]  # both tabs were read
    words = seen["words"] + key["words"]
    for backdrop in ((0.0, 0.0, 0.0), (255.0, 255.0, 255.0)):
        composite = _hex(tuple(alpha * g + (1 - alpha) * b for g, b in zip(ground, backdrop, strict=True)))
        for text, colour in words:
            rgb, text_alpha = _css_rgba(colour)
            assert text_alpha == 1, (text, colour)
            ratio = _contrast(_hex(rgb), composite)
            assert ratio >= 4.5, (scheme, backdrop, text, colour, composite, round(ratio, 2))


def test_in_view_is_an_overview_at_national_zoom_and_a_list_zoomed_in(server: object) -> None:
    """Owner, 2026-10-22: the panel was "confusing while zoomed all the way out". At the national view
    "In view" is an overview (projects by status in the Status filter's words, the top four
    technologies, the busiest states, each a button that flies there, and "Zoom in or search to list
    projects"); zoomed in, it lists projects. The switch moves nothing: the panel keeps its box."""
    # The panel's box against the map's, so a page scroll (a click brings its target into view)
    # is not read as the panel moving.
    box = (
        "() => { const p = document.getElementById('map-panel').getBoundingClientRect();"
        " const m = document.getElementById('map').getBoundingClientRect();"
        " return [p.left - m.left, p.top - m.top, p.width, p.height]; }"
    )
    mode = "() => document.getElementById('map-panel').getAttribute('data-mode')"
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            _ok_ui_events(page)
            page.goto(BASE_URL + "/")
            page.wait_for_function(_COUNT_READY, timeout=30000)
            page.wait_for_function(f"() => ({mode})() === 'overview'", timeout=30000)
            assert page.locator("#map-overview").is_visible() and page.locator("#in-view-items").is_hidden()
            filter_words = {
                "Announced",
                "In process",
                "Contracted",
                "Built or operating",
                "Withdrawn or cancelled",
                "Status unknown",
            }
            statuses = [
                " ".join(t.split())
                for t in page.locator("#map-overview-status .map-overview__name").all_inner_texts()
            ]
            assert statuses and set(statuses) <= filter_words, statuses
            assert 1 <= page.locator("#map-overview-tech li").count() <= 4
            areas = page.locator("#map-overview-areas .map-overview__area")
            areas.first.wait_for(timeout=30000)
            assert 1 <= areas.count() <= 5
            counts = [
                int(t.split()[0].replace(",", ""))
                for t in page.locator("#map-overview-areas .map-overview__count").all_inner_texts()
            ]
            assert counts == sorted(counts, reverse=True), counts
            assert page.inner_text("#map-overview-hint") == "Zoom in or search to list projects."
            national = page.evaluate(box)
            zoom = page.evaluate("window.__map.getZoom()")

            # The busiest area is a button that flies there.
            areas.first.click()
            page.wait_for_function(f"() => window.__map.getZoom() > {zoom} + 0.5 && !window.__map.isMoving()")
            page.wait_for_function("() => location.search.includes('zoom=')", timeout=10000)

            # Zoomed in: the list, in the same box.
            page.evaluate("window.__map.jumpTo({center: [-97.75, 30.3], zoom: 9})")
            page.wait_for_function(f"() => ({mode})() === 'list'", timeout=30000)
            page.wait_for_function("() => document.querySelectorAll('#in-view-items li').length > 0")
            assert page.locator("#map-overview").is_hidden() and page.locator("#in-view-items").is_visible()
            assert page.evaluate(box) == national
            # And back out: the overview again, still in the same box.
            page.evaluate("window.__map.jumpTo({center: [-97, 38], zoom: 3})")
            page.wait_for_function(f"() => ({mode})() === 'overview'", timeout=30000)
            assert page.evaluate(box) == national
        finally:
            browser.close()


def test_the_drawer_opens_in_the_panels_place_and_closing_it_brings_the_panel_back(server: object) -> None:
    """Owner, 2026-10-22: the record drawer and the panel never overlap. From 720px the drawer opens
    in the panel's place, with "Back to what's in view"; closing it, by that link, Close or Escape,
    brings the panel back as it was: the same tab, scrolled to the same place, open or collapsed."""
    rows = [_feature_with_source(f"drawer-{i:02d}", -77.6 + i * 0.006) for i in range(40)]
    proposals = _geo_envelope(rows, records=40)
    drawer_box = f"() => ({_RECT})('map-drawer')"
    panel_box = f"() => ({_RECT})('map-panel')"
    panel_shows = "() => getComputedStyle(document.getElementById('map-panel')).visibility === 'visible'"
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _stub_map(page, proposals)
            page.goto(BASE_URL + "/?center=-77.48,38.95&zoom=9")
            page.wait_for_function(
                "() => document.querySelectorAll('#in-view-items .details-btn').length === 25"
            )
            _map_on_top(page)
            page.evaluate("document.getElementById('map-panel-scroll').scrollTop = 300")
            slot = page.evaluate(panel_box)
            # Pressed from script, so the browser does not scroll the row into view first; focused
            # first, as a mouse press focuses a button (a scripted click alone does not).
            page.evaluate(
                "(() => { const b = document.querySelector('[aria-label=\"Details for drawer-12\"]');"
                " b.focus({preventScroll: true}); b.click(); })()"
            )
            page.wait_for_function(_DRAWER_IS_OPEN)
            page.wait_for_function(_DRAWER_SETTLED)
            assert [round(v) for v in page.evaluate(drawer_box)] == [round(v) for v in slot]
            assert not page.evaluate(panel_shows)
            assert page.inner_text("#map-drawer h2") == "drawer-12"
            back = page.locator("#drawer-back")
            assert back.is_visible() and back.inner_text() == "← Back to what’s in view"
            back.click()
            page.wait_for_function("() => document.getElementById('map-drawer').hidden")
            assert page.evaluate(panel_shows)
            assert page.evaluate("document.getElementById('map-panel-scroll').scrollTop") == 300
            assert page.get_attribute("#map-tab-inview", "aria-selected") == "true"
            assert (
                page.evaluate("document.activeElement.getAttribute('aria-label')") == "Details for drawer-12"
            )

            # From the Key tab, scrolled, and a click on the map: Escape brings back the Key tab.
            page.click("#map-tab-key")
            page.evaluate("document.getElementById('map-panel-scroll').scrollTop = 120")
            x, y = _screen_point(page, rows[3]["geometry"]["coordinates"])
            page.mouse.click(x, y)
            page.wait_for_function(_DRAWER_IS_OPEN)
            assert not page.evaluate(panel_shows)
            page.keyboard.press("Escape")
            page.wait_for_function("() => document.getElementById('map-drawer').hidden")
            assert page.get_attribute("#map-tab-key", "aria-selected") == "true"
            assert page.evaluate("document.getElementById('map-panel-scroll').scrollTop") == 120

            # Collapsed: the drawer still takes the panel's full place, and the panel comes back collapsed.
            page.click("#map-panel-toggle")
            assert page.get_attribute("#map-panel-toggle", "aria-expanded") == "false"
            page.wait_for_function("() => !window.__map.isMoving()")
            x, y = _screen_point(page, rows[3]["geometry"]["coordinates"])
            page.mouse.click(x, y)
            page.wait_for_function(_DRAWER_IS_OPEN)
            page.wait_for_function(_DRAWER_SETTLED)
            assert [round(v) for v in page.evaluate(drawer_box)] == [round(v) for v in slot]
            page.click("#drawer-close")
            page.wait_for_function("() => document.getElementById('map-drawer').hidden")
            assert page.evaluate(panel_shows)
            assert page.get_attribute("#map-panel-toggle", "aria-expanded") == "false"
        finally:
            browser.close()


def test_phone_panel_is_a_bar_under_the_map_and_nothing_covers_the_canvas(server: object) -> None:
    """Owner, 2026-10-22: on a phone the panel does not float. It is a bar directly under the map ("N
    projects in view", with "Key") that opens in the page; the search field is a bar above the canvas;
    the drawer stays the bottom sheet. Nothing but the right-edge column lies on the canvas, open or
    closed, and nothing scrolls sideways at 390 or 360px."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = _phone_page(browser)
            page.goto(BASE_URL + "/")
            page.wait_for_function(_COUNT_READY, timeout=30000)
            page.wait_for_function(
                "() => /in view/.test(document.getElementById('map-panel-count').textContent)"
            )
            map_box = page.locator("#map").evaluate(_BOX)
            bar = page.locator("#map-panel").evaluate(_BOX)
            search = page.locator("#map-search").evaluate(_BOX)
            assert bar[1] >= map_box[1] + map_box[3], (bar, map_box)  # under the map
            assert search[1] + search[3] <= map_box[1], (search, map_box)  # above the canvas
            assert abs(search[2] - map_box[2]) <= 1, (search, map_box)  # full width
            assert re.fullmatch(r"[\d,]+ projects? in view( · .+)?", page.inner_text("#map-panel-count"))
            assert page.locator("#map-panel-keybtn").is_visible()  # "Key" on the closed bar
            assert page.locator("#map-panel-body").is_hidden()
            assert bar[3] <= 64, bar  # one row
            _map_on_top(page)
            covered = page.evaluate(_MAP_COVERED)
            assert covered <= PHONE_MAP_COVER_BUDGET, f"{covered:.1f}% of the map covered, panel closed"
            page.click("#map-panel-toggle")
            assert page.locator("#map-panel-body").is_visible()
            assert page.get_attribute("#map-tab-inview", "aria-selected") == "true"
            # Opening it scrolls the page (smoothly) to bring it in, so both boxes are read again, at once.
            map_bottom, panel_top = page.evaluate(
                "[document.getElementById('map').getBoundingClientRect().bottom,"
                " document.getElementById('map-panel').getBoundingClientRect().top]"
            )
            assert panel_top >= map_bottom - 1, (panel_top, map_bottom)
            _map_on_top(page)
            covered = page.evaluate(_MAP_COVERED)
            assert covered <= PHONE_MAP_COVER_BUDGET, f"{covered:.1f}% of the map covered, panel open"
            assert page.evaluate("document.documentElement.scrollWidth") <= 390
            # The bar's "Key" opens the Key tab; its count, "N projects in view", opens "In view" again.
            page.click("#map-panel-toggle")
            page.click("#map-panel-keybtn")
            assert page.get_attribute("#map-tab-key", "aria-selected") == "true"
            assert page.locator("#lifecycle-legend").is_visible()
            page.click("#map-panel-toggle")
            assert page.locator("#map-panel-body").is_hidden()
            page.click("#map-panel-toggle")
            assert page.get_attribute("#map-tab-inview", "aria-selected") == "true"
            assert page.locator("#map-overview").is_visible()

            narrow = _phone_page(browser, viewport={"width": 360, "height": 800})
            narrow.goto(BASE_URL + "/")
            narrow.wait_for_function(_COUNT_READY, timeout=30000)
            narrow.click("#map-panel-toggle")
            assert narrow.evaluate("document.documentElement.scrollWidth") <= 360
        finally:
            browser.close()


def test_unticking_every_status_hides_projects_and_survives_a_reload(server: object) -> None:
    """Owner report 2026-10-22: "anytime I do all three each repopulates as clicked". Unticking the
    last Status box no longer ticks the default ones again: the URL says `lifecycle_state=none`, no
    project is drawn, the count, the notice and the panel agree, a reload keeps it, and "Clear all"
    returns to the default view."""
    default_states = ("announced", "in_process", "contracted")
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            _ok_ui_events(page)
            page.goto(BASE_URL + "/")
            page.wait_for_function(_COUNT_READY, timeout=30000)
            default_count = page.inner_text("#map-result-count")
            page.click("#mf-status summary")
            for key in default_states:
                page.uncheck(f"#mf-status-{key}")
            page.wait_for_function("() => location.search.includes('lifecycle_state=none')", timeout=10000)
            page.wait_for_function(
                "() => document.getElementById('map-result-count').textContent"
                " === '0 projects match these filters.'"
            )
            page.wait_for_function(
                "() => /No status chosen/.test(document.getElementById('map-notice').textContent)",
                timeout=10000,
            )
            for key in default_states:
                assert not page.is_checked(f"#mf-status-{key}"), key  # none ticked itself again
            assert page.inner_text("#mf-status [data-status-summary]") == "none"
            assert page.inner_text("#map-panel-count") == "No status chosen: projects are hidden."
            assert page.locator("#map-status-actions").is_hidden()
            assert _geo_records("lifecycle_state=none&bbox=-179,-85,179,85&zoom=3") == 0

            page.reload()
            page.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            page.wait_for_function(
                "() => /No status/.test(document.getElementById('map-panel-count').textContent)"
            )
            assert parse_qs(urlsplit(page.url).query).get("lifecycle_state") == ["none"]
            assert page.inner_text("#map-result-count") == "0 projects match these filters."
            assert page.inner_text("#map-panel-count") == "No status chosen: projects are hidden."
            for key in default_states:
                assert not page.is_checked(f"#mf-status-{key}"), key

            page.click("#map-notice a[data-clear-all]")
            page.wait_for_function("() => !location.search.includes('lifecycle_state')", timeout=10000)
            page.wait_for_function(
                f"() => document.getElementById('map-result-count').innerText === {json.dumps(default_count)}"
            )
            for key in default_states:
                assert page.is_checked(f"#mf-status-{key}"), key
            assert page.locator("#map-status-actions").is_visible()
        finally:
            browser.close()


#: What a key row's hover changes: the drawn opacity of the projects', regions' and plants' layers
#: (map.js `HIGHLIGHT_PAINT`), as one string to compare before and after.
_HOVER_PAINT = (
    "() => JSON.stringify([['clusters', 'circle-opacity'], ['points', 'icon-opacity'],"
    " ['region-fill', 'fill-opacity'], ['plant-points', 'icon-opacity']].map(([l, p]) =>"
    " window.__map.getLayer(l) ? window.__map.getPaintProperty(l, p) : null))"
)
#: How many marks a list row's hover has ringed (map.js `showHoverRing`).
_HOVER_RING = "async () => (await window.__map.getSource('hover-ring').getData()).features.length"


def test_a_tap_on_a_key_row_or_a_list_row_leaves_the_map_as_drawn(server: object) -> None:
    """Review 2026-10-22: hovering a key row lights the marks it names and dims the rest, and
    hovering a row of the list rings its mark, for a mouse. On a phone a tap is followed by the
    browser's emulated mouse events, so a tap on a key row left the map dimmed, and a tap on a list
    row left its ring, with no mouse to leave the row. Only a mouse's pointer lights anything now,
    and a mouse still does."""
    rows = [_in_view_feature(f"tap-{i}", "solar", -77.52 + i * 0.01) for i in range(4)]
    proposals = _geo_envelope(rows, records=4)
    key_row = "#lifecycle-legend [data-legend-family='progress']"
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            # A mouse: hovered, the key row dims the rest and the list row rings its mark; leaving
            # either puts the map back as drawn.
            desk = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _stub_map(desk, proposals)
            desk.goto(BASE_URL + "/?center=-77.48,38.95&zoom=9")
            desk.wait_for_function(_rendered("points"), timeout=30000)
            desk.wait_for_function(
                "() => document.querySelectorAll('#in-view-items .details-btn').length === 4"
            )
            drawn = desk.evaluate(_HOVER_PAINT)
            desk.click("#map-tab-key")
            desk.hover(key_row)
            assert desk.evaluate(_HOVER_PAINT) != drawn
            desk.mouse.move(5, 5)
            assert desk.evaluate(_HOVER_PAINT) == drawn
            desk.click("#map-tab-inview")
            desk.hover("#in-view-items li .meta >> nth=0")
            assert desk.evaluate(_HOVER_RING) == 1
            desk.mouse.move(5, 5)
            assert desk.evaluate(_HOVER_RING) == 0

            # A finger: a tap on a key row and on a list row leaves the map as drawn.
            phone = browser.new_context(**PHONE).new_page()
            _stub_map(phone, proposals)
            phone.goto(BASE_URL + "/?center=-77.48,38.95&zoom=9")
            phone.wait_for_function(_rendered("points"), timeout=30000)
            drawn = phone.evaluate(_HOVER_PAINT)
            phone.tap("#map-key-toggle")  # the rail's "Key" opens the Key tab under the map
            phone.locator(key_row).wait_for()
            phone.locator(key_row).tap()
            phone.wait_for_timeout(300)
            assert phone.evaluate(_HOVER_PAINT) == drawn
            phone.tap("#map-tab-inview")
            phone.locator("#in-view-items li .meta").first.tap()
            phone.wait_for_timeout(300)
            assert phone.evaluate(_HOVER_RING) == 0
            assert phone.evaluate(_HOVER_PAINT) == drawn
        finally:
            browser.close()


#: Where the open panel starts on the canvas (px from the map's left edge), and the canvas's width.
_PANEL_EDGE = (
    "() => { const m = document.getElementById('map').getBoundingClientRect();"
    " const k = document.getElementById('map-panel').getBoundingClientRect();"
    " return [k.left - m.left, m.width]; }"
)


def _by_zoom(local: str, national: str) -> Any:
    """`/api/proposals/geo` answering `local` from zoom 8 and `national` below it."""

    def answer(route: Route) -> None:
        zoom = float(parse_qs(urlsplit(route.request.url).query).get("zoom", ["3"])[0])
        route.fulfill(status=200, content_type="application/json", body=local if zoom >= 8 else national)

    return answer


def _areas_body(*areas: tuple[str, int, list[float]]) -> str:
    return json.dumps(
        {
            "areas": [
                {"id": label, "label": label, "count": count, "capacity_mw": count * 10.0, "bbox": bbox}
                for label, count, bbox in areas
            ],
            "total": sum(count for _label, count, _bbox in areas),
        }
    )


def test_in_view_leaves_out_what_lies_under_the_open_panel(server: object) -> None:
    """Review 2026-10-22: from 1080px the open panel covers the map's right 340px, and "In view"
    counted what lay under it. The header, the list and the busiest areas now count only what is left
    of the open panel, and the marks under it are still drawn; collapsed (its header over the corner
    only), the whole map is in view again."""
    left = [_in_view_feature(f"open-{i}", "solar", -77.6 + i * 0.05) for i in range(6)]
    under = [_in_view_feature(f"under-{i}", "solar", -76.85 + i * 0.05) for i in range(4)]
    local = _geo_envelope(left + under, records=10)
    national = _geo_envelope(
        [
            _cluster(-100.0, 40.0, {"filed": 40}, 4000.0, 6),
            _cluster(-75.0, 40.0, {"filed": 30}, 3000.0, 6),
        ],
        records=70,
    )
    asked: list[str] = []

    def areas(route: Route) -> None:
        asked.append(route.request.url)
        body = _areas_body(("Kansas", 40, [-102.05, 36.99, -94.59, 40.0]))
        route.fulfill(status=200, content_type="application/json", body=body)

    names = (
        "() => Array.from(document.querySelectorAll('#in-view-items a.name-link')).map((a) => a.textContent)"
    )
    count = "#map-panel-count"
    says = "(words) => document.getElementById('map-panel-count').textContent.startsWith(words)"
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _stub_map(page, local)
            page.route("**/api/proposals/geo?**", _by_zoom(local, national))
            page.route("**/api/map/areas?**", areas)
            page.goto(BASE_URL + "/?center=-77.48,38.95&zoom=9")
            page.wait_for_function(_rendered("points"), timeout=30000)
            page.wait_for_function("() => document.querySelectorAll('#in-view-items a.name-link').length > 0")
            edge, width = page.evaluate(_PANEL_EDGE)
            assert edge < width, (edge, width)  # the panel lies over the canvas
            xs = [_screen_point(page, f["geometry"]["coordinates"])[0] - 20 for f in under]
            assert all(edge < x < width for x in xs), (edge, xs)  # the four lie under the panel
            assert page.inner_text(count) == "6 projects in view · 72 MW"
            assert page.evaluate(names) == [f"open-{i}" for i in range(6)]
            assert "under-" not in page.inner_text("#map-live-region")
            # Still drawn under the panel: a pan brings them out at once.
            x, y = _screen_point(page, under[0]["geometry"]["coordinates"])
            assert page.evaluate(
                "([x, y]) => { const r = document.getElementById('map').getBoundingClientRect();"
                " const at = [x - r.left, y - r.top];"
                " return window.__map.queryRenderedFeatures(at, {layers: ['points']}).length; }",
                [x, y],
            )

            # Collapsed, the panel is its header over the top right corner: all ten are in view.
            page.click("#map-panel-toggle")
            page.wait_for_function(says, arg="10 projects")
            assert page.inner_text(count) == "10 projects in view · 120 MW"
            page.click("#map-panel-toggle")
            page.wait_for_function(says, arg="6 projects")
            assert page.evaluate(names) == [f"open-{i}" for i in range(6)]

            # The overview and its busiest areas: the cluster under the panel is left out, and the
            # areas are asked for the map left of the panel.
            page.evaluate("window.__map.jumpTo({center: [-95, 39], zoom: 4})")
            page.wait_for_function(says, arg="40 projects")
            assert page.get_attribute("#map-panel", "data-mode") == "overview"
            page.locator("#map-overview-areas .map-overview__area").first.wait_for(timeout=30000)
            bbox = [float(v) for v in parse_qs(urlsplit(asked[-1]).query)["bbox"][0].split(",")]
            west, south, east, north = page.evaluate(
                "(edge) => { const b = window.__map.getBounds(); const e = window.__map.unproject([edge, 0]);"
                " return [b.getWest(), b.getSouth(), e.lng, b.getNorth()]; }",
                edge,
            )
            assert abs(bbox[2] - east) < 0.01 and abs(bbox[0] - west) < 0.01, (bbox, west, east)
            assert abs(bbox[1] - south) < 0.01 and abs(bbox[3] - north) < 0.01, (bbox, south, north)
            assert bbox[2] < -75.0 < page.evaluate("window.__map.getBounds().getEast()")
        finally:
            browser.close()


def test_a_flown_to_view_lands_clear_of_the_open_panel(server: object) -> None:
    """Review 2026-10-22: what the map flies to keeps clear of the open panel, so a flown-to state is
    never behind it: a region named in the link (it was fitted with 24px all round, under the panel),
    a busiest area's button, and a cluster's zoom, which lands in the middle of the map the panel
    leaves rather than the canvas's."""
    national = _geo_envelope([_cluster(-100.0, 40.0, {"filed": 400}, 4000.0, 6)], records=400)
    virginia = [-83.68, 36.54, -75.24, 39.47]

    def areas(route: Route) -> None:
        body = _areas_body(("Virginia", 400, virginia))
        route.fulfill(status=200, content_type="application/json", body=body)

    project = (
        "([w, s, e, n]) => { const a = window.__map.project([w, s]); const b = window.__map.project([e, n]);"
        " return [a.x, b.y, b.x, a.y]; }"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _stub_map(page, national)
            page.route("**/api/map/areas?**", areas)
            page.goto(BASE_URL + "/?region=us")
            page.wait_for_function("() => window.__map && window.__map.loaded()", timeout=30000)
            edge, _width = page.evaluate(_PANEL_EDGE)
            us_box = page.get_attribute(".region-btn[data-region='us']", "data-bbox") or ""
            us = [float(v) for v in us_box.split(",")]
            left, _top, right, _bottom = page.evaluate(project, us)
            assert left >= 0 and right <= edge + 1, (left, right, edge)

            # A busiest area's button.
            page.evaluate("window.__map.jumpTo({center: [-95, 39], zoom: 3.5})")
            area = page.locator("#map-overview-areas .map-overview__area")
            area.first.wait_for(timeout=30000)
            area.first.click()
            page.wait_for_function("() => window.__map.getZoom() > 5 && !window.__map.isMoving()")
            left, _top, right, _bottom = page.evaluate(project, virginia)
            assert left >= 0 and right <= edge + 1, (left, right, edge)

            # A cluster's zoom: the cluster lands in the middle of the map left of the panel.
            page.evaluate("window.__map.jumpTo({center: [-95, 39], zoom: 4})")
            page.wait_for_function(_rendered("clusters"), timeout=30000)
            x, y = _screen_point(page, [-100.0, 40.0])
            page.mouse.click(x, y)
            page.wait_for_function("() => window.__map.getZoom() > 5.99 && !window.__map.isMoving()")
            landed = page.evaluate("() => window.__map.project([-100, 40]).x")
            assert abs(landed - edge / 2) <= 3, (landed, edge)
        finally:
            browser.close()


def _with_unplaced(envelope: str, unplaced: int) -> str:
    body = json.loads(envelope)
    body["meta"] = {"unplaced_count": unplaced}
    return json.dumps(body)


def test_the_count_line_says_what_matches_and_what_no_map_can_place(server: object) -> None:
    """Review 2026-10-22: "6009 proposals match these filters" above the map and "4,138 projects in
    view" on the panel confused a first-time reader, with the 1,000 not on the map named only at the
    foot of the overview. The count line now says both in one place, in the panel's noun and with
    thousands separators; below 720px, where it shares a row with "Active only. Why?", the panel's
    "In view" opens with the second sentence instead. The panel's title is an h2 under the page's h1,
    so the headings keep their order (axe `heading-order`)."""
    proposals = _with_unplaced(
        _geo_envelope([_cluster(-100.0, 40.0, {"filed": 4000, "announced": 138}, 742700.0, 6)], records=6009),
        1000,
    )
    second = "1,000 of them can’t be placed on the map (the Projects list has them)."
    levels = (
        "() => Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6'))"
        ".filter((h) => h.getClientRects().length).map((h) => Number(h.tagName[1]))"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _stub_map(page, proposals)
            page.goto(BASE_URL + "/?center=-98,39&zoom=3.6")
            page.wait_for_function(_COUNT_READY, timeout=30000)
            text = page.inner_text("#map-result-count")
            assert text == "6,009 projects match these filters.\n" + second, text
            first_line = page.evaluate(
                "() => { const r = document.createRange();"
                " const p = document.getElementById('map-result-count');"
                " r.setStart(p.firstChild, 0); r.setEnd(p.firstChild, 1);"
                " return r.getBoundingClientRect().top; }"
            )
            assert page.locator(".map-status__unplaced").evaluate(_BOX)[1] > first_line + 10  # its own line
            assert page.inner_text("#map-panel-count") == "4,138 projects in view · 742.7 GW"
            assert page.locator("#unplaced-note").is_hidden()  # the count line says it here
            seen = page.evaluate(levels)
            assert seen[:2] == [1, 2] and all(b <= a + 1 for a, b in itertools.pairwise(seen)), seen
            assert page.locator("h2.map-panel__title #map-panel-toggle").count() == 1

            phone = _phone_page(browser)
            phone.route(
                "**/api/proposals/geo?**",
                lambda r: r.fulfill(status=200, content_type="application/json", body=proposals),
            )
            phone.goto(BASE_URL + "/")
            phone.wait_for_function(_COUNT_READY, timeout=30000)
            assert phone.inner_text("#map-result-count") == "6,009 projects match these filters."
            # Both boxes are read in one frame: two separate reads can straddle a smooth scroll and
            # compare positions from different moments (CI, 2026-10-23).
            count_box, brief_box = phone.evaluate(_BOXES, ["#map-result-count", ".map-notice__brief"])
            middles = [count_box[1] + count_box[3] / 2, brief_box[1] + brief_box[3] / 2]
            assert abs(middles[0] - middles[1]) <= 2, (count_box, brief_box)  # one line, as before
            phone.click("#map-panel-toggle")
            note = phone.locator("#unplaced-note")
            assert note.is_visible()
            assert note.inner_text() == (
                "1,000 of the 6,009 that match these filters can’t be placed on the map"
                " (the Projects list has them)."
            )
            note_box, overview_box = phone.evaluate(_BOXES, ["#unplaced-note", "#map-overview"])
            assert note_box[1] < overview_box[1], (note_box, overview_box)  # first
        finally:
            browser.close()


def test_the_busiest_areas_landing_moves_nothing_in_the_overview(server: object) -> None:
    """Review 2026-10-22: five rows were kept for the busiest areas at 28px each (140px), and an
    area's row is drawn 32px tall (a 24px button and 4px above and below), so the answer landing
    pushed what follows down by 20px. The rows kept are the rows drawn."""
    national = _geo_envelope([_cluster(-100.0, 40.0, {"filed": 400}, 4000.0, 6)], records=400)
    held: list[Route] = []
    five = _areas_body(*((f"Area {i}", 100 - i, [-100.0 - i, 35.0, -95.0 - i, 40.0]) for i in range(5)))
    height = "() => document.getElementById('map-overview-areas').getBoundingClientRect().height"
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _stub_map(page, national)
            page.route("**/api/map/areas?**", lambda route: held.append(route))
            page.goto(BASE_URL + "/?center=-98,39&zoom=3.6")
            page.wait_for_function(
                "() => document.getElementById('map-panel').getAttribute('data-mode') === 'overview'"
            )
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(100)
            assert held, "the overview asked for no busiest areas"
            page.wait_for_timeout(300)
            waiting = page.evaluate(height)
            for route in held[:-1]:
                with contextlib.suppress(PlaywrightError):  # aborted by the page for the newest
                    route.abort()
            held[-1].fulfill(status=200, content_type="application/json", body=five)
            page.wait_for_function(
                "() => document.querySelectorAll('#map-overview-areas .map-overview__area').length === 5"
            )
            rows = page.locator("#map-overview-areas li").evaluate_all(
                "(li) => li.map((r) => r.getBoundingClientRect().height)"
            )
            assert page.evaluate(height) == waiting == sum(rows), (waiting, rows)
        finally:
            browser.close()


def _pick(page: Any, query: str, label: str) -> None:
    """Types into the map's search field and picks the suggestion named `label`."""
    page.fill("#map-search-input", query)
    option = page.locator("#map-search-list [role='option']", has_text=label).first
    option.wait_for(timeout=15000)
    option.click()


_CENTRE_ZOOM = "() => [window.__map.getCenter().lng, window.__map.getCenter().lat, window.__map.getZoom()]"


def test_the_search_finds_a_county_a_project_and_a_company_and_drops_a_stale_answer(server: object) -> None:
    """Owner, 2026-10-22: one box on the map finds a place, a project or a company from the site's own
    search API and the county outlines the map already shades, never a third-party geocoder, and
    moves the map there. A project opens its drawer; a company's projects are fitted. An answer for
    what was typed earlier never overwrites the answer for what is typed now."""
    held: list[Route] = []
    outside: list[str] = []

    def search(route: Route) -> None:
        if "q=texa" in route.request.url and not held:
            held.append(route)  # answered last, out of order
            return
        route.continue_()

    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            _ok_ui_events(page)
            page.on(
                "request",
                lambda r: (
                    outside.append(r.url)
                    if not r.url.startswith(BASE_URL) and "geocod" in r.url.lower()
                    else None
                ),
            )
            page.route("**/api/map/search?**", search)
            page.goto(BASE_URL + "/")
            page.wait_for_function(_COUNT_READY, timeout=30000)
            assert page.get_attribute("#map-search", "action") == "/search"  # without scripts: site search

            # A county: the map fits it.
            _pick(page, "travis", "Travis County, TX")
            page.wait_for_function("() => !window.__map.isMoving()")
            page.wait_for_timeout(300)
            lng, lat, zoom = page.evaluate(_CENTRE_ZOOM)
            assert -98.3 < lng < -97.3 and 30.0 < lat < 30.7 and zoom > 7, (lng, lat, zoom)

            # A project: its drawer opens, provenance included, and the map goes to its county.
            _pick(page, "vaquero 1", "Vaquero 1 Wind")
            page.wait_for_function(_DRAWER_IS_OPEN)
            assert page.inner_text("#map-drawer h2") == "Vaquero 1 Wind"
            assert "retrieved" in page.inner_text("#map-drawer .drawer-source")
            page.wait_for_function("() => !window.__map.isMoving()")
            page.wait_for_timeout(300)
            lng, lat, _zoom = page.evaluate(_CENTRE_ZOOM)
            assert -100.0 < lng < -98.5 and 26.0 < lat < 27.8, (lng, lat)  # Zapata County, TX
            page.keyboard.press("Escape")

            # A company: its projects under the current filters are fitted; the page stays the map.
            _pick(page, "engie distributed", "ENGIE Distributed Renewables Development LLC")
            page.wait_for_timeout(1500)
            page.wait_for_function("() => !window.__map.isMoving()")
            assert urlsplit(page.url).path == "/"
            assert page.input_value("#map-search-input") == "ENGIE Distributed Renewables Development LLC"

            # Stale: "texa" is answered after "travi"; the list still shows what "travi" found.
            page.fill("#map-search-input", "texa")
            deadline = time.monotonic() + 10
            while not held and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            assert held, "the first search was not asked"
            page.fill("#map-search-input", "travi")
            page.locator("#map-search-list [role='option']", has_text="Travis County, TX").first.wait_for(
                timeout=15000
            )
            with contextlib.suppress(PlaywrightError):
                held[0].continue_()
            page.wait_for_timeout(800)
            options = page.locator("#map-search-list [role='option']").all_inner_texts()
            assert any("Travis County, TX" in o for o in options) and not any(
                o.startswith("Texas") for o in options
            )
            assert outside == []
        finally:
            browser.close()


def test_a_transmission_line_hover_card_closes_on_the_next_tap_or_a_map_move(server: object) -> None:
    """Owner report 2026-10-22: on a phone a tap on a transmission line left its hover card on the
    map, with no mouse to move off it. A tap opens no card (the browser's emulated mouse events after
    it are ignored); a card that a mouse-like pointer opened closes on the next tap anywhere on the
    page, and when the map moves."""
    proposals = _geo_envelope([], records=0)
    card = "() => document.querySelectorAll('.maplibregl-popup').length"
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_context(**PHONE).new_page()
            _stub_map(page, proposals, lines=[_LINE])
            page.goto(BASE_URL + "/?center=-100,40&zoom=5")
            page.wait_for_function(_rendered("asset-lines-hit"), timeout=30000)
            _map_on_top(page)
            x, y = _screen_point(page, [-100.0, 40.0])
            page.touchscreen.tap(x, y)
            page.wait_for_function(_DRAWER_IS_OPEN)
            page.wait_for_timeout(400)
            assert page.evaluate(card) == 0  # the tap's own mouse events opened no card
            page.keyboard.press("Escape")
            page.wait_for_function("() => document.getElementById('map-drawer').hidden")

            page.wait_for_timeout(900)  # past the moment after a touch when no card opens
            page.mouse.move(x - 30, y)
            page.mouse.move(x, y)
            page.wait_for_function(f"() => ({card})() === 1", timeout=5000)
            page.touchscreen.tap(20, 20)  # anywhere else on the page
            page.wait_for_function(f"() => ({card})() === 0", timeout=5000)

            page.wait_for_timeout(900)
            page.mouse.move(x - 30, y)
            page.mouse.move(x, y)
            page.wait_for_function(f"() => ({card})() === 1", timeout=5000)
            page.evaluate("window.__map.panBy([60, 0], {duration: 0})")
            page.wait_for_function(f"() => ({card})() === 0", timeout=5000)
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
    # The server's log goes to a file, never an unread pipe: a full 64 KiB pipe buffer blocks the
    # server's next write, so every later request (and SIGTERM's graceful shutdown) hangs.
    server_log = _PMTILES_PROOF_DB_PATH.with_suffix(".server.log").open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1", "--port", "8798"],
        cwd=REPO_ROOT,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_server(_PMTILES_PROOF_BASE_URL + "/health")
        yield proc
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        server_log.close()


_NEEDS_REAL_ARCHIVE = pytest.mark.skipif(
    not PMTILES_ARCHIVE_PATH.exists(),
    reason=(
        f"real PMTiles archive not present at {PMTILES_ARCHIVE_PATH} -- extract one first: "
        "go-pmtiles extract https://build.protomaps.com/<a current YYYYMMDD>.pmtiles "
        f"{PMTILES_ARCHIVE_PATH} --bbox=-98.1,30.0,-97.4,30.6 --maxzoom=10"
    ),
)


@pytest.mark.network  # fetches the pmtiles/basemaps scripts and Protomaps glyphs from their real hosts
@_NEEDS_REAL_ARCHIVE
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

            # (f) UX review 2026-10-20 (docs/16 §2 M-1): the real style's sourceless `background`
            # layer is not added, so nothing without a source sits over the outline; no note.
            _outline_is_uncovered(page.evaluate(_LAYER_IDS, "__map"))
            assert page.locator(".map-basemap-note").count() == 0

            # (g) ... and with the tiles in, the map is what it was with that layer: the archive's
            # fills cover the outline. Put the layer back where it stood and compare the canvas.
            page.evaluate("() => new Promise((done) => window.__map.once('idle', done).triggerRepaint())")
            without = page.locator("#map").screenshot()
            page.evaluate(
                "() => new Promise((done) => { const m = window.__map;"
                " const css = getComputedStyle(document.documentElement);"
                " const paint = {'background-color': css.getPropertyValue('--map-land').trim()};"
                " const first = m.getStyle().layers.find((l) => l.source === 'protomaps').id;"
                " m.addLayer({id: 'background', type: 'background', paint}, first);"
                " m.once('idle', done); })"
            )
            assert page.locator("#map").screenshot() == without, "dropping the background changed the map"

            page.screenshot(path=str(SCREENSHOT_DIR / "pmtiles-austin-real.png"), full_page=True)
        finally:
            browser.close()


@pytest.mark.network  # fetches the pmtiles/basemaps scripts from their real host
@_NEEDS_REAL_ARCHIVE
def test_pmtiles_archive_failure_keeps_the_outline_with_real_scripts(pmtiles_proof_server: object) -> None:
    """UX review 2026-10-20 (docs/16 §2 M-1), with the real pinned pmtiles and basemaps scripts in
    place of the offline suite's stand-ins: the archive answering 404 leaves no sourceless layer
    over the outline, and the note under the map says the base map is unavailable."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page, serve_pmtiles_scripts=True)
            _install_glyphs_and_sprite_routes(page)
            beacons = _basemap_failed_beacons(page)
            page.route(f"**/{PMTILES_ARCHIVE_PATH.name}", lambda r: r.fulfill(status=404, body=""))
            page.goto(_PMTILES_PROOF_BASE_URL + "/")
            note = page.locator(".map-basemap-note")
            note.wait_for(timeout=15000)
            page.wait_for_function("() => window.__map.isStyleLoaded()", timeout=15000)
            _outline_is_uncovered(page.evaluate(_LAYER_IDS, "__map"))
            assert note.inner_text() == _BASEMAP_UNAVAILABLE
            assert page.evaluate("window.__map.queryRenderedFeatures({layers: ['fallback-land']}).length") > 0
            page.wait_for_timeout(300)
            assert len(beacons) == 1, beacons
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


# ============================================================================================
# Navigation (docs/16 §3.1-3.2, U-6, U-17, U-24): six sections named for supply and demand, the
# Infrastructure tabs, list headers short enough to show data in the first screen, a favicon.

#: The dev store has no site-finder run, so the site finder is not listed (`site_finder_listed`,
#: web/page.py; web/test_layout_states.py pins both states).
SECTIONS = ["Map", "Projects", "Companies", "RFPs & funding", "Infrastructure", "Alerts"]
LIST_PATHS = ("/proposals", "/opportunities", "/organizations", "/assets", "/interconnection-points")
#: Measured as production lays the page out: this server runs with WEB_DEV_PREVIEW=1, whose notice
#: (`.preview-banner`, about 53px) no production page shows, so it is taken out first.
_FIRST_ROW_JS = """() => {
  document.querySelectorAll('.preview-banner').forEach((el) => el.remove());
  const top = (el) => el ? el.getBoundingClientRect().top + window.scrollY : null;
  const row = document.querySelector('table.record-table tbody tr');
  return [top(row), top(document.getElementById('result-rows'))];
}"""


def test_nav_is_six_sections_with_infrastructure_tabs_short_list_headers_and_a_favicon(
    server: object,
) -> None:
    """Owner, 2026-10-20: "proposals vs opportunities are a bit confusing". At 1440: six links in
    order, the current one marked; About & sources in the masthead; on every list the first row
    starts under 450px (it was 488-689px, docs/16 §1.2); the favicon loads, so no page logs a 404
    for it. At 400: the six links and About & sources behind Menu, the tabs on one screen width."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            wide = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(wide)
            icon_responses: list[tuple[str, int]] = []
            wide.on(
                "response", lambda r: icon_responses.append((r.url, r.status)) if "icon" in r.url else None
            )
            measured = 0
            for path in LIST_PATHS:
                wide.goto(BASE_URL + path)
                links = wide.locator(".primary-nav a")
                assert links.all_inner_texts()[:6] == SECTIONS, path
                current = wide.locator(".primary-nav a[aria-current]")
                assert current.count() == 1, path
                expected = {"/proposals": "Projects", "/opportunities": "RFPs & funding"}.get(path)
                expected = expected or ("Companies" if path == "/organizations" else "Infrastructure")
                assert current.inner_text() == expected, path
                assert wide.locator(".masthead__about").is_visible(), path
                assert wide.locator(".site-nav-panel__about").is_hidden(), path
                row, rows_block = wide.evaluate(_FIRST_ROW_JS)
                # Not every list has rows in the e2e store; where none, the block they would fill.
                assert (row if row is not None else rows_block) < 450, (
                    f"{path}: first row at {row}, rows at {rows_block}"
                )
                measured += row is not None
            assert measured, "no list had a row to measure"
            wide.goto(BASE_URL + "/interconnection-points")
            tabs = wide.locator('nav.section-tabs[aria-label="Infrastructure sections"] a')
            assert tabs.all_inner_texts() == ["Plants, pipelines & lines", "Grid connection points"]
            assert tabs.nth(1).get_attribute("aria-current") == "page"
            assert tabs.nth(0).get_attribute("aria-current") is None
            assert wide.locator('[role="tablist"], [role="tab"]').count() == 0
            tabs.nth(0).click()
            wide.wait_for_url("**/assets")
            assert (
                wide.locator(".section-tabs a[aria-current='page']").inner_text()
                == "Plants, pipelines & lines"
            )
            assert wide.locator("h1").inner_text() == "Existing infrastructure"
            svg = wide.request.get(BASE_URL + "/static/img/favicon.svg")
            assert svg.status == 200 and svg.headers["content-type"].startswith("image/svg+xml")
            assert not [u for u, status in icon_responses if status >= 400], icon_responses
            wide.screenshot(path=str(SCREENSHOT_DIR / "nav-assets-1440.png"))

            narrow = browser.new_page(viewport=NARROW_VIEWPORT)
            _install_offline_routes(narrow)
            narrow.goto(BASE_URL + "/interconnection-points")
            assert narrow.locator(".masthead").is_hidden()
            menu = narrow.locator(".nav-toggle")
            assert narrow.locator(".primary-nav").is_hidden()
            menu.click()
            assert narrow.locator(".primary-nav a").all_inner_texts()[:6] == SECTIONS
            about = narrow.locator(".site-nav-panel__about a")
            assert about.is_visible() and about.inner_text() == "About & sources"
            menu.click()
            tab_boxes = [narrow.locator(".section-tabs a").nth(i).bounding_box() for i in range(2)]
            assert all(box is not None and box["x"] + box["width"] <= 400 for box in tab_boxes), tab_boxes
            overflow = narrow.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            assert overflow <= 0, f"page scrolls horizontally at 400px by {overflow}px"
            narrow.screenshot(path=str(SCREENSHOT_DIR / "nav-points-400.png"))
        finally:
            browser.close()


def test_list_pages_never_scroll_sideways_at_the_breakpoint(server: object) -> None:
    """Review of lane w1-nav: from 720px the list heads do not wrap, so the visually hidden spans
    in them escaped `.table-scroll` and widened the page (856px at a 720px viewport once the
    Company column landed). docs/31 §3: the page body never scrolls horizontally."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            for width in (720, 723, 1024):
                page = browser.new_page(viewport={"width": width, "height": 900})
                _install_offline_routes(page)
                page.goto(BASE_URL + "/proposals")
                page.wait_for_selector(".record-table")
                assert page.evaluate("document.documentElement.scrollWidth") <= width, width
                page.close()
        finally:
            browser.close()


def _capacities(page: Any) -> list[float]:
    """The Capacity column of the rows on the page, as numbers ("—" cells left out)."""
    texts = page.locator('#result-rows td[data-label="Capacity (MW)"]').all_inner_texts()
    return [float(t.replace(",", "").replace("MW", "").strip()) for t in texts if any(c.isdigit() for c in t)]


def _sorted_heading(page: Any) -> tuple[str, str]:
    th = page.locator("#result-rows th[aria-sort]")
    assert th.count() == 1
    return th.locator(".sort-link").get_attribute("href") or "", th.get_attribute("aria-sort") or ""


def test_a_heading_click_sorts_the_list_and_a_second_reverses_it_and_phone_rows_are_two_lines(
    server: object,
) -> None:
    """Lane list-sort (2026-10-22): a click on a sortable heading sorts the list by that column, a
    second click reverses it, the arrow and `aria-sort` say the order, and the URL carries `sort=`
    beside the filters, so the view can be shared and a filter change keeps the order. Below 720px
    (docs/16 U-10, docs/17 M-8) the rows are two lines, not label/value cards, and the sortable
    headings are a "Sort by" row; the page never scrolls sideways."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**_launch_kwargs())
        try:
            page = browser.new_page(viewport=DESKTOP_VIEWPORT)
            _install_offline_routes(page)
            page.goto(BASE_URL + "/proposals?capacity_mw%5Bgte%5D=1")
            # Default: capacity, largest first.
            href, order = _sorted_heading(page)
            assert order == "descending" and "sort=capacity_mw" in href
            caps = _capacities(page)
            assert len(caps) > 2 and caps == sorted(caps, reverse=True)
            # A click on the sorted heading reverses it, and keeps the filter.
            page.locator("#result-rows th.is-sorted .sort-link").click()
            page.wait_for_url(re.compile(r"sort=capacity_mw"))
            assert "capacity_mw%5Bgte%5D=1" in page.url
            assert _sorted_heading(page)[1] == "ascending"
            caps = _capacities(page)
            assert caps == sorted(caps)
            # Another heading sorts by itself, its own first order (names A to Z), then reverses.
            page.locator('#result-rows th.sortable .sort-link:has-text("Name")').click()
            page.wait_for_url(re.compile(r"sort=name_canonical"))
            assert _sorted_heading(page)[1] == "ascending"
            page.locator("#result-rows th.is-sorted .sort-link").click()
            page.wait_for_url(re.compile(r"sort=-name_canonical"))
            assert _sorted_heading(page)[1] == "descending" and "capacity_mw%5Bgte%5D=1" in page.url
            assert page.locator('#filters input[type="hidden"][name="sort"]').get_attribute("value") == (
                "-name_canonical"
            )
            page.screenshot(path=str(SCREENSHOT_DIR / "list-sorted-1440.png"))
            page.close()

            phone = browser.new_context(**PHONE).new_page()
            _install_offline_routes(phone)
            phone.goto(BASE_URL + "/proposals")
            phone.wait_for_selector(".record-table--compact")
            assert (
                phone.locator(".record-table--compact thead th.sortable").evaluate_all(
                    "ths => ths.filter(th => th.offsetParent !== null).length"
                )
                == 3
            )  # Name, Capacity, Online
            heights = sorted(
                phone.locator(".record-table--compact tbody tr").evaluate_all(
                    "rows => rows.map(r => r.getBoundingClientRect().height)"
                )
            )
            # 277px a row as label/value cards at this width (2026-10-22, the full store); two lines
            # of text, or three where a long place name wraps, are under half that.
            assert heights and heights[len(heights) // 2] < 120, heights
            first = phone.locator(".record-table--compact tbody tr").first
            chip = first.locator(".cell-lead").bounding_box()
            title = first.locator(".cell-title").bounding_box()
            assert chip and title and abs(chip["y"] - title["y"]) < 12  # status and name share line one
            overflow = phone.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            assert overflow <= 0, f"/proposals scrolls sideways at 390px by {overflow}px"
            phone.screenshot(path=str(SCREENSHOT_DIR / "list-compact-390.png"))
        finally:
            browser.close()
