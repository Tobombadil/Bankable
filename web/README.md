# Sprint 2 public-site prototype

**Scope:** public delayed tier only -- no auth, no Pro, no admin. Built to `docs/30-design-ia.md` and
`docs/31-design-system.md` against `docs/04-standards.md`. Serves five proposal sources (ERCOT,
CAISO, NYISO, EIA-860M, NESO TEC) and four opportunity sources (grants.gov, TED, Find a Tender,
World Bank) from `data/normalized/`.

## Run it

```bash
# from the repo root, with the shared venv (fastapi/jinja2/uvicorn/playwright installed)
python -m web.build_data --no-lag          # writes web/static/data/{proposals.geojson,opportunities.json,stats.json}
uvicorn web.app:app --reload               # http://127.0.0.1:8000/
```

`--no-lag` is the prototype flag the task asked for: it shows today's connector run instead of
filtering everything out (all the sample data was retrieved today, and the real rule is a 14-day
supply / 7-day opportunity lag). Every page still renders the delayed-tier notice with the
*configured* lag, exactly as it will once the data is old enough for the lag to bite -- the notice
text does not change between `--no-lag` and the real filter, only the row count does. Drop
`--no-lag` to see the real filter (`web/test_build_data.py::test_lag_filtering_excludes...`
proves it empties the set against today's fixtures, which is the intended behaviour).

## Pages built

| Route | What |
|---|---|
| `/` (= `/map`) | Map-first home: MapLibre GL JS, filter bar, in-view list, legend, Unplaced accordion |
| `/proposals` | Filtered/sorted list (technology, lifecycle state, kind, jurisdiction, capacity range), htmx fragment swap, cursor-style pagination |
| `/proposals/{slug}` | Detail: fields, status chip, Sources panel, lifecycle timeline, raw fields (only where the licence allows), attribution line |
| `/opportunities` | Filtered/sorted list, default `status=open` sorted by due date |
| `/opportunities/{slug}` | Detail: fields, status chip, Sources panel, status timeline, attribution line |
| `/search` | Free-text search across both proposals and opportunities |
| `/about` | Method notes + the source/licence registry, rendered from `data/sources.yaml` and `stats.json`, not hard-coded |
| `/health` | Liveness + `data_as_of` |

## Feature counts rendered (from the real `data/normalized/` snapshots, `--no-lag`)

| Source | Kind | Rows visible |
|---|---|---|
| `us.iso.ercot.gen_queue` | proposal | 1,778 |
| `us.iso.caiso.gen_queue` | proposal | 2,278 |
| `us.iso.nyiso.gen_queue` | proposal | 1,814 |
| `us.eia.860m` | proposal | 2,341 |
| `gb.neso.tec_register` | proposal | 2,198 |
| `us.grants_gov.search2` | opportunity | 151 |
| `eu.ted.api` | opportunity | 698 |
| `gb.find_a_tender` | opportunity | 11 |
| `mdb.worldbank.procnotices` | opportunity | 100 |

**10,409 proposals, 960 opportunities**, all visible (no `--no-lag` filtering loss since every row
was retrieved today). Placement: 2,341 exact points (EIA-860M only -- the only `open` source with a
supplied coordinate), 5,464 county centroids, 378 folded into 8 state-aggregate markers, 2,207
listed under Unplaced (2,198 NESO GB rows with no US-style county/state; 9 CAISO/NYISO rows with
neither a matched county nor a state at all).

## Design system implementation

- Tokens as CSS custom properties in `web/static/css/styles.css` §1: colour (core + five status
  families), type scale (`--text-1`...`--text-7`, Newsreader/Plex Sans/Plex Mono), 4px spacing
  scale, radius, motion. Dark theme via `prefers-color-scheme` plus a `[data-theme]` override hook
  (no manual toggle control built this sprint -- see Gaps below).
- Status chip component (`_macros.html` `status_chip`): shared SVG icon + label per family, never
  colour alone (D-5); `status_raw` exposed to screen readers only where the source's licence
  allows raw (CAISO/NYISO withhold it).
- Provenance panel + attribution line render from each record's own `source_id`, `source_url`,
  `retrieved_at`, `licence_id`, `attribution_text` -- nothing is a template literal.
- Delayed-tier notice is one component, one wording, rendered on every list/map/detail page.
- No banned pattern from `docs/30` §9: table for tabular data (not cards), no gradient for status,
  no emoji, no glassmorphism, no marketing hero above the map.
- Responsive to 400px: table rows collapse to key/value cards, filter bar stacks, map keeps its
  side list as a stacked panel (not yet a true bottom sheet -- see Gaps).

## Quality

Run everything from the repo root with the shared venv:

```bash
python -m web.build_data --no-lag
ruff check web/
ruff format --check web/
mypy web
pytest web -v
```

Verbatim output from this build:

```
### ruff check web/
All checks passed!

### ruff format --check web/
8 files already formatted

### mypy web
Success: no issues found in 6 source files

### python -m web.build_data --no-lag
build complete
proposals: 10409/10409 visible | opportunities: 960/960 visible | no_lag=True

### pytest web -v
============================= test session starts ==============================
platform linux -- Python 3.11.15, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/user/Bankable
configfile: pyproject.toml
plugins: anyio-4.15.1
collected 8 items

web/test_build_data.py .......                                           [ 87%]
web/test_e2e.py .                                                        [100%]

============================== 8 passed in 31.68s ===============================
```

`mypy` is scoped to `pipeline`, `web` and `services` in `pyproject.toml` (three teams share the
repo this sprint); running the full `mypy` from the repo root also reports one pre-existing error
in `pipeline/connectors/us_ferc_elibrary/connector.py` unrelated to this task (a `pandas-stubs`
strictness finding in a connector this agent does not own) -- `mypy web` above is the scoped,
relevant gate and is clean. `pytest` at the repo root also runs `tests/test_social_queue.py`,
which belongs to a different in-progress agent's work in this shared session and is unrelated to
`web/`; `pytest web pipeline tests --ignore=tests/test_social_queue.py` is fully green.

### Playwright smoke path (`web/test_e2e.py`)

Starts a real `uvicorn` subprocess, drives Chromium (`/opt/pw-browsers/chromium`) through
map -> list -> detail at desktop (1440px) and 400px, and asserts: the `#map` container exists and
at least one cluster or marker actually renders (`queryRenderedFeatures`), the delayed-tier notice
is present, the provenance panel and attribution line render on a detail page, and there is no
horizontal overflow at 400px. Screenshots land in `web/screenshots/`:
`home-desktop.png`, `home-400px.png`, `list-desktop.png`, `list-400px.png`, `detail-desktop.png`,
`detail-400px.png`.

This sandbox's egress proxy resets Chromium's own TLS handshake to every external host the page
references (`cdn.jsdelivr.net` for MapLibre, `fonts.googleapis.com` for the type families,
`tile.openstreetmap.org` for the basemap) -- plain `curl`/`urllib` through the same proxy work
fine, so the test fetches the two MapLibre assets once via Python and serves them to the browser
from memory (`page.route`), and aborts the non-essential font/basemap-tile requests rather than
waiting out the same reset. The app's own markup is untouched -- a real browser with normal
internet access loads both from the CDN exactly as written in `templates/home_map.html`.

### Map basemap: why it rendered blank, and the fix

Early screenshots showed clusters floating on a blank white canvas. Diagnosis (each step
measured against this build, not assumed):

1. **Tile source blocked in this sandbox -- confirmed.** `curl`/`urllib` through the proxy
   fetch `tile.openstreetmap.org`, `tiles.openfreemap.org` (a vector alternative) and
   `demotiles.maplibre.org/style.json` successfully. The same three URLs opened directly in this
   Chromium build (`/opt/pw-browsers/chromium`) all reset mid-TLS-handshake
   (`net::ERR_CONNECTION_RESET`) regardless of provider -- a Chromium/proxy interaction specific
   to this sandbox, not a defect in any one tile host or in the app's style configuration.
2. **That exposed a real style-configuration fragility (not just a sandbox artefact).** With the
   OSM raster source declared in the map's *initial* style, MapLibre GL JS 5.24.0's `load` event
   never fired at all once every tile request failed -- confirmed by watching it hang for 40+
   seconds even after each individual tile logged its own `AJAXError` to the console. A raster
   source that fails outright (blocked host, offline, an ad-blocker, a flaky CDN) was silently
   taking the *entire* map down with it, data layers included, not just the basemap image. This
   would have hit real users too, not only this sandbox.
3. **Playwright's screenshot timing (`idle`) is not a safe fix for #2.** `idle` fires only once
   every requested tile has "loaded or errored out"; if a tile request never resolves at the
   network layer (as here), `idle` never fires either. Waiting for `idle` before capture was tried
   and does not help when the tile source itself is the thing hanging.

Fix applied in `web/static/js/map.js`: the OSM raster source/layer is no longer part of the
map's initial style. The initial style holds only a same-origin GeoJSON fallback layer (world
country fill + country/state outlines, `web/static/data/basemap_fallback.geojson`,
public-domain Natural Earth + US Census Bureau TIGERweb data, generated by
`web/data_ref/build_basemap_fallback.py`), so `load` fires as soon as that fast, reliable,
same-origin fetch resolves. The OSM tile source is then added with `map.addSource`/`addLayer`
*inside* the `load` handler, drawn above the fallback so real tiles cover it wherever they load,
and its success or failure can no longer block anything else on the page. OSM tiles remain the
production default basemap; the fallback is what keeps the map legible -- underneath real tiles
when they load, standing in for them when they do not. The Playwright smoke test proves this
directly: `queryRenderedFeatures({layers:['fallback-land']})` is asserted non-empty with the tile
layer deliberately unreachable.

## What could not be honoured this sprint, and why

- **D-21 self-hosted fonts.** The task instruction for this build was explicit: "Newsreader, IBM
  Plex Sans and IBM Plex Mono from Google Fonts". `docs/31` D-21 calls for self-hosting with a
  vendored licence file. Flagged here as a departure to fix before production, not a design-system
  change.
- **Clustering is client-side (MapLibre's built-in `cluster: true`), not the server-computed
  `/v1/proposals/geo` endpoint** `docs/23` §3.1 describes. There is no database or API service in
  this sprint's scope (`docs/20` §15 row is a Sprint 2+ item across several agents); the dominant-
  family colour for a cluster is computed with a `clusterProperties` sum per family and a
  `max`-comparison expression, which ties in a fixed priority order (danger > committed > success
  > progress > neutral) rather than the server deciding it.
- **D-14 keyboard/roving-tabindex on individual map markers** is not implemented -- MapLibre
  markers are canvas-rendered (correctly, per D-13's "never one DOM node per marker"), which means
  they are not natively focusable. The synchronised, always-present "in view" list *is* built and
  is the full keyboard/screen-reader path to every record (arrow-key map panning and per-marker
  roving tabindex are not); this is a narrower reading of D-14 than the standard intends.
- **D-32's "map keeps the results list as a bottom sheet" at 400px** renders the side list as a
  stacked panel below the map instead of a true overlay bottom sheet. No horizontal overflow, all
  content reachable, just a simpler vertical layout.
- **D-23 manual dark/light override control** is not built; the page follows
  `prefers-color-scheme` only. Both palettes are complete and verified (see the dark-mode
  screenshot check performed during development).
- **NESO TEC Register has no county/state field usable by the D-8 precedence chain** -- its
  `county` column holds a connection-site or substation name, not a UK administrative county, and
  this build has no UK gazetteer. All 2,198 rows are listed under Unplaced (GB) rather than
  guessed at; documented in `/about`.
- **Sitemaps, RSS/JSON feeds** (a general frontend-developer responsibility) are out of this
  sprint's explicit page list (`docs/30-design-ia.md` limits it to map/list/detail/about) and were
  not built.
- **Sort/filter pagination uses a plain integer offset** exposed as `cursor=` rather than a signed
  opaque token (`docs/23` API-2). The UI never shows "page N of M" and cursors are not reused
  across data changes in this static-file prototype, so the simplification is invisible to a user
  but is not the real cursor contract.
