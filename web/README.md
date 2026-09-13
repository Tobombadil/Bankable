# Public site — reading `services/api`

**Scope:** public delayed tier only — no auth, no Pro, no admin. Built to `docs/30-design-ia.md`
and `docs/31-design-system.md` against `docs/04-standards.md`. This sprint's task: stop reading
`web/build_data.py`'s static JSON and read `services/api` instead, and fix three product defects
visible in the previous sprint's screenshots. See `docs/00-PLAN.md`'s decisions log (2026-09-13,
frontend-developer) for the one-paragraph summary and `docs/CHANGELOG.md` for the changelog line.

## Architecture

- **`web/api_client.py`** — `ApiClient` wraps either a real `httpx.Client` (when `API_BASE_URL` is
  set — HTTP mode, what a production deployment and `web/dev_up.py` use) or `services.api.app.app`
  mounted in-process via Starlette's `TestClient` (when it isn't — no socket at all; what `pytest`
  and a plain `uvicorn web.app:app --reload` use). Both expose the same `.get(path, params)`.
- **`web/viewmodels.py`** — turns API envelope dicts (`services/api/serialize.py`'s shape) into
  the flat dicts the Jinja templates read, and holds the frontend-only presentation rules: the
  lifecycle-family colour grouping (docs/31 §1.2), the default active-states filter (product
  defect A), and the collapsed provenance panel (product defect C). It decides nothing about
  *visibility* — that is entirely `services/api/visibility.py`.
- **`web/app.py`** — routes call `services/api` through `ApiClient` and render templates. The map
  page's own JS talks to a same-origin proxy, `GET /api/proposals/geo`, which forwards to
  `GET /v1/proposals/geo` and rewrites the API's absolute (and currently placeholder-hostname)
  `url` fields to relative paths — see "Missing from the API" below.
- **`web/static/js/map.js`** — fetches `/api/proposals/geo` on load and on every pan/zoom/filter
  change and renders exactly what comes back (individual points below the cluster threshold,
  cluster circles above it, per `docs/23` §3.1/docs/04 D-10). It does not compute clusters itself.
- **`web/data_loading.py`** — loads connector parquet into a `services.db` SQLite store through
  the real `services/ingest/loader.py`, for `web/dev_up.py` and the pytest suite. See "Data-layer
  corrections" below for what it does beyond a plain load, and why.
- **`web/store.py`** (the old in-memory filter/sort/paginate layer over the static JSON files) is
  removed — nothing reads it any more; `services/api` now does all of filtering, sorting and
  pagination, per the API's own filter grammar and cursor contract (`docs/23` §7, API-2).
- **`web/build_data.py`** — retained only for the vendored basemap fallback asset
  (`web/data_ref/build_basemap_fallback.py`'s output, `web/static/data/basemap_fallback.geojson`);
  no route in `web/app.py` reads its JSON output any more. `web/test_build_data.py` still exercises
  it directly and still passes — it is untouched.

## Run it

**For real**, loading `data/normalized/*` through `services/ingest/loader.py` into SQLite and
starting the API and the site as two processes talking over HTTP:

```bash
python -m web.dev_up                 # loads data, starts both servers on :8001/:8000, Ctrl-C stops both
python -m web.dev_up --preview       # also bypasses the publish delay so today's rows show now
python -m web.dev_up --sample-per-state 200   # work around the visibility-query perf gap below
```

**In-process**, no second server at all (leave `API_BASE_URL` unset):

```bash
DATABASE_URL=sqlite+pysqlite:///web/.data/dev.db uvicorn web.app:app --reload
```

## Product defects fixed

**A — the default map was a map of what died.** 7,889 of 14,388 normalised records (docs/22) are
withdrawn; the old prototype showed everything, so the map read as dominated by dead projects. The
default view (`web/viewmodels.py::ACTIVE_PROPOSAL_STATES`) now shows `announced` through
`under_construction` only; `withdrawn`/`cancelled` sit behind an explicit "Include withdrawn &
cancelled" checkbox on the map and the proposals list (same URL-param pattern the opportunities
list already used for `status=all`: an explicit `lifecycle_state=` still wins verbatim). The notice
above both views states the real counts, computed live from the API
(`lifecycle_breakdown()` calls `GET /v1/proposals/geo` with no lifecycle filter and sums
`totals.lifecycle_state_counts`), e.g. against the real per-source data loaded by `dev_up.py`:
*"Showing 872 active proposals (announced through under construction). 160 withdrawn/cancelled are
hidden by default … (320 built or unknown-state proposals are also outside this default view)."*
`built` is deliberately outside both the default and the toggle — the task's own wording bounded
the default range at `under_construction`; a future "show completed" control is a fair follow-up
but wasn't asked for here.

**B — the placeholder-name subtitle and "not a production launch" footer read as apologies.** The
wordmark no longer carries a subtitle; the placeholder-name fact moved to `/about`'s intro
paragraph (stated once, plainly). The footer is one calm line with the delayed-tier fact (read
live from `/v1/health`, never hard-coded) and a sources link: *"Public data is delayed 14 days for
proposals, 7 for opportunities. Sources and licences."*

**C — the licence quote block was right but long.** The provenance panel now collapses each
source behind its name; the reuse-class badge and retrieval date stay visible outside the
`<details>`, and the (now composed, not verbatim — see below) licence text is the expandable part,
matching `docs/31` §5.2's anatomy (source name, `retrieved_at`, licence badge, link-out; the quote
itself isn't part of that anatomy, but the task asked for it collapsed the same way).

## Delayed tier, for real

`--no-lag` is gone. `public_at` from the API governs what's visible everywhere; nothing in `web/`
filters further. Because `services/ingest/loader.py` computes `public_at = published_at +
lag_days` at *ingestion* time, freshly-loaded rows (whose `retrieved_at`/`published_at` is "now")
are correctly invisible for 14 (proposals) or 7 (opportunities) days — exactly the real behaviour.
For previewing what a fresh connector run looks like without waiting: `web/dev_up.py --preview`
(or `WEB_DEV_PREVIEW=1` for a manually-started `uvicorn`) pulls `public_at` back to "now" for rows
still in the future (`web/data_loading.py::apply_preview_lag_override`) and the site shows a
distinct, clearly-labelled banner wherever the delayed-tier notice renders: *"Preview: the publish
delay is bypassed for this data so today's rows are visible now (dev only). The notice above still
states the real configured lag."* The delayed-tier notice itself is unaffected by preview mode —
it always states the real configured lag from `/v1/health`.

## Data-layer corrections (`web/data_loading.py`) — and why they exist

None of these edit `services/` code; they are documented workarounds applied *after* calling the
real, unmodified `services.ingest.loader` functions, each tied to a gap already recorded in
`services/README.md`'s "Open decisions":

1. **Geocoding backfill.** `services/ingest/loader.py` never sets `location.geom` (open decision
   #2 — no geocoder exists yet), so nothing would plot on the map at all. `backfill_locations()`
   reuses the same public-domain US Census Gazetteer county-centroid table
   `web/build_data.py`'s prototype vendored. `backfill_eia_exact_points()` additionally promotes
   EIA-860M's exact `Latitude`/`Longitude` (present in the `raw` JSON the loader already stores,
   just not projected onto `location.geom`) to the `exact` precedence tier, matching the design's
   placement precedence (docs/04 D-8).
2. **CAISO/NYISO's derived-only status.** Open decision #11: the loader hardcodes
   `licence.allows_raw_publication = True` for *every* ingested licence, and never sets
   `location.precision_reason` for *any* source. Both together mean the restricted-precision note
   (docs/04 D-9) — the whole point of product defect C's redaction requirement — could never
   render, even for CAISO/NYISO, which the legal register (`docs/00-PLAN.md`, 2026-09-12) calls
   derived-only. `apply_derived_only_licence_correction()` flips the flag for those two sources
   and stamps their `location.precision_reason = "licence"`.
3. **Source `publish_state`.** Open decision #5: a newly-ingested source loads as `api_only` until
   an admin publish workflow (not built yet) flips it to `public`. Every source this loader
   touches already cleared the licence gate by construction, so `_flip_publish_state_public()`
   does what that admin action would.

`tests/test_web_provenance.py` proves defect C's redaction note works *independent* of these
corrections (built directly on `services/db` models via `services/api/conftest.py`'s helpers, with
a licence explicitly marked `allows_raw_publication=False`), so the test suite isn't just
validating my own workaround against itself.

## Quality

```bash
ruff check web/
ruff format --check web/
mypy web
pytest web tests/test_web_default_view.py tests/test_web_provenance.py -v
```

Verbatim output from this build:

```
### ruff check web/
All checks passed!

### ruff format --check web/
12 files already formatted

### mypy web
Success: no issues found in 10 source files

### pytest web tests/test_web_default_view.py tests/test_web_provenance.py -v
============================= test session starts ==============================
platform linux -- Python 3.11.15, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/user/Bankable
configfile: pyproject.toml
plugins: anyio-4.15.1
collected 18 items

web/test_build_data.py .......                                           [ 38%]
web/test_e2e.py .                                                        [ 44%]
tests/test_web_default_view.py .......                                   [ 83%]
tests/test_web_provenance.py ...                                         [100%]

======================== 18 passed in 55.62s ========================
```

`services/db services/ingest services/api tests/test_api_contract.py` (untouched by this task,
read only) still pass unchanged: 59 tests. `mypy` is scoped to `pipeline`, `web` and `services` in
`pyproject.toml`; `tests/` is intentionally not in that list (see `pyproject.toml`'s comment), so
the two new `tests/test_web_*.py` files are covered by the pytest run above, not the strict-mypy
gate — both import and run cleanly regardless.

### What each new/changed test proves (docs/00-PLAN.md task item 6)

- `tests/test_web_default_view.py` — loaded from `data/eval/normalized.parquet` (the task's named
  fixture) through the real loader, restricted to the non-gated sources
  (`web.build_data.EVAL_SHORT_ID_MAP`; SPP/ISO-NE dropped, never remapped). Asserts: the default
  `/api/proposals/geo` and `/proposals` exclude `withdrawn`/`cancelled`; the `include_withdrawn=1`
  toggle and an explicit `lifecycle_state=` both include them; the home page states the real
  active/withdrawn counts; a proposal detail page's rendered source name/URL/retrieved-date match
  the API envelope's own `provenance[0]` verbatim (not a template literal).
  `services/ingest/loader.py` upserts row-by-row (no bulk path — several flushes per row measures
  at ~100 rows/second here), so this file's fixture loads a `_stratified_sample` (60 rows per
  lifecycle state per source, real rows through the real loader) rather than all ~9,500 —
  documented in `web/data_loading.py::load_eval_fixture`. Full run: 7 tests in ~6s.
- `tests/test_web_provenance.py` — built directly on `services/db` models (no eval fixture),
  because the redaction case needs a licence with `allows_raw_publication=False`, which nothing in
  the real ingested data currently produces without this task's own data-loader correction (see
  above): proves the provenance panel collapses the licence quote behind the source name with
  classification and retrieval date visible (product defect C), the restricted-precision note
  renders when a location's `precision_reason` is `"licence"` (docs/04 D-9), and a gated/absent
  record reads as 404 indistinguishably (docs/21 §8 item 3).
- `web/test_e2e.py` (updated, not new) — now loads a real file-backed SQLite database via
  `web/data_loading.py::load_dev_database(preview=True)` instead of building static JSON, and
  starts `uvicorn web.app:app` as a real subprocess with `DATABASE_URL` set and `API_BASE_URL`
  unset (in-process API mount inside that one process). Regenerates all six screenshots in
  `web/screenshots/`: `home-{desktop,400px}.png`, `list-{desktop,400px}.png`,
  `detail-{desktop,400px}.png`.

## Missing from the API — for the backend team

1. **No slug-keyed lookup.** `/v1/proposals/{public_id}` and `/v1/opportunities/{public_id}` are
   the only detail routes; there is no `slug=` filter and no slug-keyed alias. The site's URLs are
   SEO-friendly slugs per `docs/30-design-ia.md` (`/proposals/{slug}`), so
   `web/app.py::_resolve_proposal_by_slug`/`_resolve_opportunity_by_slug` derive a search phrase
   from the slug and scan up to 200 `q=` search results for an exact match — a real, working,
   but best-effort substitute that can miss a record whose derived phrase doesn't rank in the
   first 200 results. Recommend a `slug=` filter or a slug-keyed alias route.
2. **Visibility-predicate performance at real volume.** `services/api/visibility.py`'s
   `_has_public_source` (a correlated `EXISTS` per candidate row) measures at 30-75 seconds per
   page over this site's real ~10,400-row proposal set in SQLite — confirmed the cost is the
   predicate itself, not `services/api/geo.py`'s clustering (a plain `include=count` list query on
   `/v1/proposals` costs the same). Unusable for an interactive site at this volume; likely needs
   an index or a rewritten join, and should be re-measured against real Postgres/PostGIS before
   trusting SQLite's number either way. `web/data_loading.py` and `web/dev_up.py --sample-per-state`
   exist specifically to keep the dev/test site responsive around this gap; it isn't fixed.
3. **No free-text licence quote/notes field in the public schema.** `Licence`'s embedded shape
   (`serialize_licence_embedded`) is boolean permission flags + `attribution_text` — no field
   carries `data/sources.yaml`'s free-text `license`/`notes` clauses the design's provenance panel
   implies ("licence quote"). `web/viewmodels.py::_compose_licence_quote` composes a plain-language
   paraphrase from the flags instead of quoting anything verbatim. Recommend exposing the
   registry's free-text terms (a `notes`/`quote_text` field) if a literal quote is wanted.
4. **`bbox` on `/v1/proposals/geo` is accepted and echoed but not enforced** — `services/api/geo.py`
   never filters `proposals` by the given bounding box before clustering, so panning the map
   currently re-fetches the same full result set every time (zoom still changes cluster
   granularity, since grid-cell size is zoom-dependent). Not a correctness bug for what's rendered,
   but a real gap for map performance at scale.
5. **`technologies` (opportunities) is allowlisted but not implemented.** `services/api/app.py`'s
   `OPPORTUNITY_FILTERS` includes `technologies`, and `check_allowed` accepts it, but
   `_opportunity_query_with_filters` never applies it — passing it silently changes nothing. The
   opportunities list's technology filter is wired to send this exact parameter name, so it will
   start working the moment the backend implements it; today it's a no-op, not an error.
6. **`GET /v1/organizations/{id}` has no public detail route rendered by this site** — sponsor and
   issuer names render as plain text (`serialize_organization_summary`'s `url` field is unused);
   an `/organizations/{slug}` page is in `docs/30-design-ia.md`'s page inventory but was out of
   this task's explicit scope.

## Simplifications carried over or newly made, and why

- **No separate "state aggregate" marker.** The old client-side prototype drew a distinct square
  glyph for records placed only at state level. The API's location model has no aggregate-marker
  concept (only a `precision` value); state-centroid records now render as ordinary points (or
  fold into a cluster like anything else), which is a smaller vocabulary than the old prototype
  but consistent with the server owning clustering (item 4 above governs a fuller fix).
- **`/proposals`'s "Previous" pager control is always disabled.** `services/api/pagination.py`
  only implements forward iteration (`page.prev_cursor` is always `null`, `services/README.md`
  open decision #6) — carried through honestly rather than faked.
- **Jurisdiction is a free-text input, not a populated dropdown**, on both list pages and the map
  filter bar: `/v1/meta/vocabularies` has no `jurisdiction` vocabulary to populate one from.
- **The raw-JSON "Raw source fields" debug panel from the old prototype is gone** — the public API
  never exposes a record's full raw payload (by design; only `provenance[].source_record_id`,
  gated by licence), so there was nothing left to show.

## Design system, accessibility and responsiveness (carried forward, updated where the API changed it)

Unchanged from the previous sprint except where noted above: tokens as CSS custom properties
(`web/static/css/styles.css`), the status chip/provenance-panel/delayed-notice/table components
from `docs/31` §5, `dark`/`light` via `prefers-color-scheme`, responsive to 400px (filter bars
stack, tables collapse to key/value rows, the map keeps its side list as a stacked panel). Google
Fonts CDN instead of self-hosted (docs/31 D-21) remains a pre-production departure, unchanged by
this task.
