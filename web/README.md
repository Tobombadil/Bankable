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
  the real `services/ingest/loader.py`, for `web/dev_up.py`, `web/test_e2e.py` and the pytest
  suite — all three now load the full, unsampled `data/normalized/*` set by default (see "Full-data
  run" below). See "Data-layer corrections" below for the one correction it still applies on top of
  a plain load, and why.
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
python -m web.dev_up                 # loads the full data set, starts both servers on :8001/:8000
python -m web.dev_up --preview       # also bypasses the publish delay so today's rows show now
python -m web.dev_up --sample 200    # cap each source to 200 rows/lifecycle-state, for fast iteration
```

`--sample` is not a performance workaround any more (see "Full-data run" below) — it exists purely
for a faster local edit-reload loop, since `services/ingest/loader.py`'s row-by-row upsert (no bulk
path) costs a couple of minutes wall clock over the full ~11,400-row set regardless of query speed.

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
`totals.lifecycle_state_counts`), e.g. against the **full** per-source data loaded by `dev_up.py`
(see "Full-data run" below for how this was measured): *"Showing 5837 active proposals (announced
through under construction). 3215 withdrawn/cancelled are hidden by default … (1357 built or
unknown-state proposals are also outside this default view)."* Those three lifecycle-state counts
sum to the full 10,409-proposal load (5837 + 3215 + 1357 = 10,409). Separately, of the proposals
matching the *default* (active-only) filter, 1825 have no usable county or state on their source
record to plot at all — the map's own "In view" panel reports them as Unplaced and points to the
list instead. `built` is deliberately outside both the default and the toggle — the task's own wording bounded
the default range at `under_construction`; a future "show completed" control is a fair follow-up
but wasn't asked for here.

**B — the placeholder-name subtitle and "not a production launch" footer read as apologies.** The
wordmark no longer carries a subtitle; the placeholder-name fact moved to `/about`'s intro
paragraph (stated once, plainly). The footer is one calm line with the delayed-tier fact (read
live from `/v1/health`, never hard-coded) and a sources link: *"Public data is delayed 14 days for
proposals, 7 for opportunities. Sources and licences."*

**C — the licence quote block was right but long.** The provenance panel now collapses each
source behind its name; the reuse-class badge and retrieval date stay visible outside the
`<details>`, and the licence quote text is the expandable part, matching `docs/31` §5.2's anatomy
(source name, `retrieved_at`, licence badge, link-out; the quote itself isn't part of that anatomy,
but the task asked for it collapsed the same way). The quote itself is now `Licence.quote_text`
rendered verbatim (`data/sources.yaml`'s free-text `license` clause, exposed by
`services/README.md`'s "Sprint 2 fixes" #5) rather than a paraphrase composed from boolean
permission flags — see "Data-layer corrections" and "Full-data run" below.

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

## Data-layer corrections (`web/data_loading.py`) — and why one remains

This used to be three documented workarounds applied *after* calling the real, unmodified
`services.ingest.loader` functions, each tied to a gap `services/README.md`'s "Open decisions" once
recorded. `services/README.md`'s "Sprint 2 fixes" (2026-09-13, backend-developer) closed the
underlying gap for two of them directly in the loader, and confirmed (by re-running the unmodified
`web`/`tests/test_web_*` suite against its changes) that re-running this site's own versions on top
would now be a redundant no-op — so they are removed here, not merely unused:

1. ~~**Geocoding backfill** (`backfill_locations()`).~~ `services/ingest/loader.py` now geocodes
   county/state centroids itself at ingest time (`services/ingest/geocode.py`, a copy of the same
   public-domain US Census Gazetteer logic this function used to vendor). **Removed.**
2. ~~**CAISO/NYISO's derived-only status** (`apply_derived_only_licence_correction()`).~~ The loader
   now derives `licence.allows_raw_publication` and `location.precision_reason` per source from the
   registry (a `_DERIVED_ONLY_RE` match on a source's own `notes`/`license` text, per
   `services/README.md`'s open decision #12), rather than hardcoding every source as raw-allowed.
   **Removed.**
3. ~~**Source `publish_state`** (`_flip_publish_state_public()`).~~ Also superseded — the loader now
   sets `publish_state` from the registry's reuse class at ingest time (`services/README.md`
   "Sprint 2 fixes" #6). **Removed.**

**`backfill_eia_exact_points()` stays.** EIA-860M's raw `Latitude`/`Longitude` promotion to `exact`
location precision (docs/04 D-8's placement precedence) is explicitly out of
`services/ingest/loader.py`'s scope this sprint — its own geocoder only ever produces
`county_centroid`/`state_centroid`/`unknown` (`services/README.md`'s "Sprint 2 fixes" says so
directly, and repeats the same verdict on this specific function). This one correction remains a
real, documented frontend-side patch until the loader promotes EIA-860M's exact points itself; it
is what gives EIA-860M's ~2,341 proposals exact-point placement on the map instead of falling back
to a county centroid like every other source.

`tests/test_web_provenance.py` proves defect C's redaction note works *independent* of any
data-loading correction (built directly on `services/db` models via `services/api/conftest.py`'s
helpers, with a licence explicitly marked `allows_raw_publication=False`), so the test suite isn't
just validating this site's own workaround against itself — and continues to pass now that the
workaround for that specific case no longer exists.

## Quality

```bash
ruff check web/
ruff format --check web/
mypy web
pytest web tests/test_web_default_view.py tests/test_web_provenance.py -v
```

See "Full-data run" below for this sprint's verbatim output — the pytest command above now loads
the full `data/normalized/*` set (no sample), so it costs minutes, not seconds; run
`pytest -k "not (test_e2e or test_web_default_view)"` for a fast local loop that skips both
full-data loads.

### What each test proves (docs/00-PLAN.md task item 6, updated by "Switch to full data")

- `tests/test_web_default_view.py` — now loads the full, unsampled `data/normalized/*` connector
  output (via `web/data_loading.py::load_dev_database`, the same path `web/dev_up.py` and
  `web/test_e2e.py` use), not a sampled fixture — see "Full-data run" below for why that switch was
  possible. Asserts: the default `/api/proposals/geo` and `/proposals` exclude
  `withdrawn`/`cancelled`; the `include_withdrawn=1` toggle and an explicit `lifecycle_state=` both
  include them; the home page states the real active/withdrawn counts; a proposal detail page's
  rendered source name/URL/retrieved-date match the API envelope's own `provenance[0]` verbatim
  (not a template literal). `services/ingest/loader.py` upserts row-by-row (no bulk path — several
  flushes per row measures at ~100 rows/second here, unaffected by the query-time fix below), so
  this file's module-scoped fixture costs about three minutes to load the full ~11,400-row set once
  for its seven tests.
- `tests/test_web_provenance.py` — built directly on `services/db` models (no fixture file at all),
  because the redaction case needs a licence with `allows_raw_publication=False`, which nothing in
  the real ingested data currently produces: proves the provenance panel collapses the licence quote
  behind the source name with classification and retrieval date visible (product defect C), that
  quote is `Licence.quote_text` rendered verbatim when a source sets one and an honest fallback
  string when it doesn't, the restricted-precision note renders when a location's
  `precision_reason` is `"licence"` (docs/04 D-9), and a gated/absent record reads as 404
  indistinguishably (docs/21 §8 item 3).
- `web/test_e2e.py` — loads the full data set via `web/data_loading.py::load_dev_database(preview=True)`
  (no sample any more, see "Full-data run" below) and starts `uvicorn web.app:app` as a real
  subprocess with `DATABASE_URL` set and `API_BASE_URL` unset (in-process API mount inside that one
  process). Regenerates all six screenshots in `web/screenshots/`: `home-{desktop,400px}.png`,
  `list-{desktop,400px}.png`, `detail-{desktop,400px}.png` — all six now show the real full-volume
  data and the retuned map clustering, not the earlier sample.

## Full-data run (2026-09-13, frontend-developer)

`services/README.md`'s "Sprint 2 fixes" closed the `services/api/visibility.py` query-time gap
(30-75s/page unsampled, down to 0.127s/page and 0.35-0.53s/geo-request on the full load) that used
to force this site's dev script and test suite onto a sample. This task switched `web/dev_up.py`
(now `--sample` instead of `--sample-per-state`, unrelated to that fix — see "Data-layer
corrections" above), `web/test_e2e.py` and `tests/test_web_default_view.py` to the full, unsampled
`data/normalized/*` load — 10,409 proposals across ERCOT/CAISO/NYISO/EIA-860M/NESO TEC, 960
opportunities across grants.gov/TED/Find a Tender/World Bank, matching `services/README.md`'s
counts exactly.

### Default-view counts, full data

From the regenerated `home-desktop.png` screenshot's own live notice (`lifecycle_breakdown()`
against the full load):

| | count |
|---|---|
| Active shown by default (announced through under construction) | 5,837 |
| Withdrawn/cancelled hidden by default | 3,215 |
| Built/unknown-state, outside the default and the toggle | 1,357 |
| **Total proposals** | **10,409** |
| Unplaced (no usable county/state to plot), within the default view | 1,825 |

### Cluster count at the default zoom

The home page's default view (zoom 3, continental-US-scale bbox from `web/static/js/map.js`'s
initial `WORLD_CENTER`/`WORLD_ZOOM`) renders **22 clusters** — visually confirmed in
`web/screenshots/home-desktop.png` — matching `services/README.md`'s own measurement of 22 clusters
at zoom 3 over the same bbox on the same full load. This replaces the earlier three-circle defect
(`cell_deg = 360 / 2**zoom` covered the whole continental US in 2-6 cells at zoom 3); the backend's
retuned grid (`cell_deg = max(36.0 / 2**(zoom-1), MIN_CELL_DEG)`) is what produces the tens-of-
clusters result confirmed here from the site side.

### Page render times, full data, in-process API (wall clock, three runs each)

Measured with a fresh `starlette.testclient.TestClient(web.app)` (no HTTP socket — same in-process
mode `pytest` and a plain `uvicorn web.app:app --reload` use) against the full-data SQLite database
`web/test_e2e.py`'s own run had already loaded, one warm-up request discarded before timing:

| Page | Run 1 | Run 2 | Run 3 | Average |
|---|---|---|---|---|
| `GET /` (home) | 0.512s | 0.510s | 0.492s | 0.505s |
| `GET /proposals?sort=-capacity_mw` (list) | 0.603s | 0.545s | 0.487s | 0.545s |
| `GET /proposals/bute-hydrogen-project-1-ssfv10` (detail) | 0.033s | 0.013s | 0.014s | 0.020s |

Home and list both make at least one `GET /v1/proposals/geo` call (`lifecycle_breakdown()`'s
sitewide notice), which is the dominant cost in both and lands in the same 0.46-0.53s band
`services/README.md` reports for that endpoint on the full load; the list page's own
`GET /v1/proposals` page query is comparatively free (~0.15-0.19s there). The detail page needs no
geo call at all — it resolves the record with the new `slug=` filter (a single indexed lookup, see
below) and stays under 35ms every run.

### Slug lookup and licence quote, confirmed against the full data

`_resolve_proposal_by_slug`/`_resolve_opportunity_by_slug` (`web/app.py`) now call
`GET /v1/proposals?slug=...&limit=1` / `GET /v1/opportunities?slug=...&limit=1&status=<all>`
(`api/openapi.yaml`'s `SlugFilter`, `services/README.md` "Sprint 2 fixes" #5) instead of deriving a
search phrase and scanning up to 200 `q=` results for an exact match. Every detail-page navigation
in the regenerated `web/test_e2e.py` run (list → detail, both viewports) resolved through this path
against the full 10,409-proposal set. The provenance panel (`web/templates/_macros.html`'s
`provenance_panel` macro, `web/viewmodels.py::_licence_quote_text`) renders `Licence.quote_text`
verbatim inside the collapsed `<details>` — visible in `detail-desktop.png`'s "Sources" section
(collapsed by default, matching product defect C) — instead of the composed permission-flag
paraphrase this module used to build.

### Lint, types and tests (verbatim)

```
$ ruff check web/
All checks passed!

$ ruff format --check web/
11 files already formatted

$ mypy web
services/alerts/feed.py:56: error: Incompatible types in assignment (expression has type "Select[tuple[Opportunity]]", variable has type "Select[tuple[Proposal]]")  [assignment]
services/alerts/feed.py:59: error: Argument 1 to "_opportunity_feed_item" has incompatible type "Proposal"; expected "Opportunity"  [arg-type]
services/alerts/feed.py:61: error: Incompatible types in assignment (expression has type "Select[tuple[Event]]", variable has type "Select[tuple[Proposal]]")  [assignment]
services/alerts/feed.py:61: error: Argument 1 to "event_visibility_filter" has incompatible type "str"; expected "Literal['public', 'pro', 'api']"  [arg-type]
services/alerts/feed.py:62: error: Argument 1 to "event_matches_query" has incompatible type "Proposal"; expected "Event"  [arg-type]
services/alerts/feed.py:63: error: "Proposal" has no attribute "seq"  [attr-defined]
services/alerts/feed.py:64: error: Argument 1 to "_event_feed_item" has incompatible type "Proposal"; expected "Event"  [arg-type]
services/alerts/feed.py:74: error: Argument 1 to "proposal_visibility_filter" has incompatible type "str"; expected "Literal['public', 'pro', 'api']"  [arg-type]
services/alerts/feed.py:75: error: Argument 1 to "opportunity_visibility_filter" has incompatible type "str"; expected "Literal['public', 'pro', 'api']"  [arg-type]
services/api/app.py:175: error: Argument 1 to "proposal_visibility_filter" has incompatible type "str"; expected "Literal['public', 'pro', 'api']"  [arg-type]
services/api/app.py:242: error: Argument 1 to "proposal_visibility_filter" has incompatible type "str"; expected "Literal['public', 'pro', 'api']"  [arg-type]
services/api/app.py:268: error: Argument 1 to "proposal_visibility_filter" has incompatible type "str"; expected "Literal['public', 'pro', 'api']"  [arg-type]
services/api/app.py:404: error: Argument 1 to "proposal_visibility_filter" has incompatible type "str"; expected "Literal['public', 'pro', 'api']"  [arg-type]
services/api/app.py:418: error: Argument 1 to "proposal_visibility_filter" has incompatible type "str"; expected "Literal['public', 'pro', 'api']"  [arg-type]
Found 14 errors in 2 files (checked 9 source files)

$ mypy --follow-imports=silent web
Success: no issues found in 9 source files
```

The plain `mypy web` run above surfaces 14 errors, all in `services/alerts/feed.py` (a module this
task never touched) and `services/api/app.py`; both are outside this task's assigned paths (`web/` and
`tests/test_web_*.py` only — `services/` was read-only) and reflect another agent's concurrent,
in-progress work there, confirmed by `mypy --follow-imports=silent web`, which scopes strictly to
`web/`'s own nine source files and is clean. Reported honestly rather than only running the
narrower command, since the plain `mypy web` invocation is what `pyproject.toml`'s configured
command actually does (it follows imports into `services/` by default) and a reader re-running it
today would see the same result until the other agent's work lands cleanly.

```
$ pytest web tests/test_web_default_view.py tests/test_web_provenance.py -v
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

======================== 18 passed, 1 warning in 451.41s (0:07:31) ========================
```

18 tests, same count as before this task — the switch was in what data they load, not how many
tests exist. `services/db services/ingest services/api tests/test_api_contract.py` (untouched by
this task, read only) are each another agent's concurrently-changing tree this task did not run,
per the task brief's "do not touch services/". `mypy` is scoped to `pipeline`, `web` and `services`
in `pyproject.toml`; `tests/` is intentionally not in that list (see `pyproject.toml`'s comment), so
the two `tests/test_web_*.py` files are covered by the pytest run above, not the strict-mypy gate —
both import and run cleanly regardless.

## Missing from the API — for the backend team

Items 1, 3, 4 and 5 below were fixed by `services/README.md`'s "Sprint 2 fixes" (2026-09-13,
backend-developer) and are consumed by this task's changes to `web/app.py` and
`web/viewmodels.py` — kept here, marked resolved, for the history of what the gap was and how it
was closed. Item 2's underlying query-time problem is also fixed there (a missing index, not a
rewrite); item 6 remains open, unrelated to this task.

1. **RESOLVED — slug-keyed lookup.** `GET /v1/proposals`/`GET /v1/opportunities` now accept a
   `slug=` query parameter (`api/openapi.yaml`'s `SlugFilter`), an exact match on the record's own
   slug. `web/app.py::_resolve_proposal_by_slug`/`_resolve_opportunity_by_slug` now call it directly
   instead of deriving a search phrase from the slug and scanning up to 200 `q=` results for an
   exact match — a real fix, not a bigger workaround: every slug lookup is now one indexed query
   instead of a best-effort text search that could miss a record outside the first 200 results.
2. **Visibility-predicate performance at real volume — the query-time part is fixed.** The 30-75
   second/page cost over the full ~10,400-row proposal set was one missing index on
   `proposal_source.proposal_id`/`opportunity_source.opportunity_id` (`services/README.md`'s
   migration `0002_licence_quote_and_visibility_indexes.py`), now 0.127s/page and 0.35-0.53s per
   `/v1/proposals/geo` call on the same full load — see "Full-data run" above for this task's own
   confirmation of those numbers end to end. `web/dev_up.py --sample` (renamed from
   `--sample-per-state`) remains, but purely as a fast local-iteration option now, not a
   correctness-forcing workaround: `services/ingest/loader.py`'s row-by-row upsert rate
   (~100 rows/second) is a separate, still-real cost this fix didn't touch.
3. **RESOLVED — no free-text licence quote/notes field in the public schema.** `Licence.quote_text`
   (new column, same migration as item 2) now carries `data/sources.yaml`'s free-text `license`
   clause verbatim on the embedded licence shape. `web/viewmodels.py` renders it as-is in the
   provenance panel instead of composing a paraphrase from the boolean permission flags — see
   "Full-data run" above.
4. **RESOLVED — `bbox` on `/v1/proposals/geo` is now enforced**, and the clustering grid was
   retuned (`cell_deg = max(36.0 / 2**(zoom-1), MIN_CELL_DEG)` in place of `360 / 2**zoom`, which
   covered the whole continental US in 2-6 cells at zoom 3). This task confirmed 22 clusters at the
   default zoom on the full load (see "Full-data run" above), matching `services/README.md`'s own
   measurement.
5. **RESOLVED — `technologies` (opportunities) is now implemented** (`_opportunity_technologies_filter`,
   any-of match over `Opportunity.technologies[]`), applied everywhere opportunities are queried.
   The opportunities list's technology filter (already wired to send this exact parameter name)
   now actually filters.
6. **`GET /v1/organizations/{id}` has no public detail route rendered by this site** — sponsor and
   issuer names render as plain text (`serialize_organization_summary`'s `url` field is unused);
   an `/organizations/{slug}` page is in `docs/30-design-ia.md`'s page inventory but remains out of
   this task's explicit scope. (`services/README.md` notes the API route itself already exists.)

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
