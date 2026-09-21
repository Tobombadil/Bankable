# Sprint 2 backend — services/db, services/ingest, services/api

Public tier only, per the Sprint 2 backend brief. Pro, API-key and admin surfaces are not built
here. Read `docs/20-architecture.md`, `docs/21-data-model.md`, `docs/adr/0002-0004`,
`api/openapi.yaml` and `docs/04-standards.md` §3-§5 before changing anything in this tree — this
file explains how to run what those documents specify, not what the design is.

## Postgres vs SQLite — read this first

**`which postgres pg_ctl initdb` all fail in this environment: Postgres is not installed.**

- `services/db/migrations/versions/0001_initial_schema.py` is the **canonical schema**: real
  Postgres 16 types (`UUID`, `JSONB`, `ARRAY(Text)`, `geography(Point,4326)` via PostGIS,
  `TIMESTAMP(timezone=True)`), the `postgis`/`pg_trgm`/`btree_gin`/`pgcrypto` extensions, the
  `CHECK` vocabularies, and the indexes docs/21 §5.2 calls out that are feasible in one migration
  (GIN on `identifiers`/trigram columns, GiST on `location.geom`, the publish-state/public_at
  BTREEs). **It has not been executed against a live database** — there is no Postgres to run it
  against here. It has been checked for internal consistency (`alembic history` resolves the
  revision graph; `python -m py_compile` / AST-parses cleanly) but not applied. Before it is
  trusted in an environment with Postgres, run `alembic -c services/db/migrations/alembic.ini
  upgrade head` (with `DATABASE_URL` pointing at a real Postgres 16 + PostGIS instance) and fix
  whatever that surfaces.
- **The test suite (`services/db/test_models.py`, `services/ingest/test_loader.py`,
  `services/api/test_routes.py`, `tests/test_api_contract.py`) runs the same SQLAlchemy ORM
  models against SQLite**, because that's what's available. `services/db/types.py` supplies
  dialect-aware fallbacks (`GUID`, `JSONVariant`, `TextArray`, `GeographyPoint`) so the ORM layer
  produces working tables on both backends; the migration does not use these — it writes the
  literal Postgres types. The two are deliberately not the same code path: the migration is the
  spec, the ORM+SQLite pair is this sprint's test double for it.
- Consequence: geospatial bounding-box queries (`ST_Intersects`, GiST) are **not exercised** by
  this test suite — `services/api/geo.py`'s clustering is a pure-Python lon/lat grid that works
  identically on both backends, at the cost of not proving PostGIS-specific query performance or
  correctness. `event` is a normal table here, not the monthly-partitioned one docs/21 §5.3
  specifies (also called out as a known gap in the migration's docstring).

## Run the tests

```bash
cd /home/user/Bankable
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest services/db services/ingest services/api tests/test_api_contract.py -v
```

Verbatim output from this sprint's run:

```
============================= test session starts ==============================
platform linux -- Python 3.11.15, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/user/Bankable
configfile: pyproject.toml
plugins: anyio-4.15.1
collected 59 items

services/db/test_models.py ............                                  [ 20%]
services/ingest/test_loader.py .........                                 [ 35%]
services/api/test_routes.py ..................                           [ 66%]
tests/test_api_contract.py ....................                          [100%]

=============================== warnings summary ===============================
../.../starlette/testclient.py:37: DeprecationWarning: The anyio.abc.BlockingPortal alias is
  deprecated, use anyio.from_thread.BlockingPortal instead.
tests/test_api_contract.py: 19 warnings
  jsonschema.RefResolver is deprecated as of v4.18.0, in favor of the referencing library.
  (Kept anyway: it is the simplest way to resolve api/openapi.yaml's internal $refs against the
  whole document as one schema store; revisit if RefResolver is actually removed upstream.)

======================= 59 passed, 20 warnings in 2.92s ========================
```

Branch coverage (`coverage run --branch -m pytest ... && coverage report --include="services/*"`):
**83% overall** (2223 statements). `services/api/visibility.py` (the tier/licence predicate) is
**100%** lines and branches — docs/04 E-7 requires 100% branch coverage on the visibility
predicate and licence-gate modules; the gate-raising paths in `services/ingest/loader.py`
(`GateRefused` from both `_assert_not_gated` and the "both must hold" re-check inside
`upsert_licence_and_source`) are covered by dedicated tests
(`test_gate_refused_for_reuse_value_outside_the_known_vocabulary`,
`test_gate_refused_when_stored_licence_is_gated_even_if_the_manifest_now_says_open`). The
remaining loader.py gap is field-mapping edge cases (odd date/number shapes from connector rows)
that fall under the general 80% floor, not the stricter gate-module bar.

## Lint and types

```bash
.venv/bin/ruff check services/db services/ingest services/api services/ids.py tests/test_api_contract.py
.venv/bin/ruff format --check services/db services/ingest services/api services/ids.py tests/test_api_contract.py
.venv/bin/mypy services/db services/ingest services/api services/ids.py
```

Verbatim:

```
--- ruff check ---
All checks passed!
--- ruff format --check ---
27 files already formatted
--- mypy --strict ---
Success: no issues found in 21 source files
```

`pyproject.toml` was extended to add `"services"` to `[tool.mypy] files` and
`[tool.pytest.ini_options] testpaths`, plus per-file ruff ignores for the FastAPI `Depends(...)`
default-argument idiom (`B008`), test-file asserts (`S101`), and the Alembic `env.py`'s
sys-path-before-import ordering (`E402`) — the same conventions already used for `pipeline/` and
`web/` in that file.

## Layout

```
services/db/
  models.py          SQLAlchemy 2.0 ORM models for the 13 public-tier entities
  types.py           Dialect-aware GUID/JSONB/text[]/geography TypeDecorators (see caveat above)
  base.py            Declarative Base + naming convention
  session.py         Engine/session factory (DATABASE_URL from environment; SQLite default)
  test_models.py     Schema tests: provenance quartet, CHECK vocabularies, uniqueness, seq
  migrations/        Alembic; versions/0001_initial_schema.py is the canonical Postgres DDL

services/ingest/
  loader.py          Idempotent parquet+events -> store loader; the licence/reuse-class gate
  lag.py             public_at == published_at for every row, records and events alike;
                     no delay and no knob anywhere (owner 2026-09-19 and 2026-09-21)
  test_loader.py     Idempotency, gate refusal (both independent checks), lag computation

services/ids.py      Crockford-base32 public ids (prop_/opp_/org_/evt_) and slugify()

services/api/
  app.py             FastAPI app: every implemented route (see below)
  schemas live only in api/openapi.yaml — serialize.py builds dicts validated against it
  serialize.py       Envelope/meta/licence_summary/provenance + per-entity dict builders
  visibility.py       The public-tier predicate (docs/21 §5.4) — 100% branch coverage
  pagination.py      Generic keyset cursor pagination
  geo.py             GeoJSON FeatureCollection + grid clustering for /v1/*/geo
  feeds.py           RSS 2.0 / JSON Feed 1.1 rendering
  errors.py          RFC 9457 problem details
  params.py          Filter-grammar allowlist ("unknown parameter" is always 400, never dropped)
  deps.py            DB session FastAPI dependency
  conftest.py        Shared test fixtures (also imported by tests/test_api_contract.py)
  test_routes.py     Functional tests: envelope shape, tier/gate visibility, pagination, feeds

tests/test_api_contract.py   Loads api/openapi.yaml, validates real responses against its
                              schemas with jsonschema (docs/04 E-8)
```

## Endpoints implemented vs the spec

All `x-tier: public` in `api/openapi.yaml`. 25 operations implemented:

| Group | Operations |
|---|---|
| Proposals | `listProposals`, `getProposalsGeo`, `getProposal`, `listProposalEvents`, `listProposalSources` |
| Opportunities | `listOpportunities`, `getOpportunitiesGeo`, `getOpportunity`, `listOpportunityEvents`, `listOpportunitySources` |
| Organizations | `listOrganizations`, `getOrganization`, `listOrganizationProposals`, `listOrganizationOpportunities` |
| Events | `listEvents`, `getEvent` |
| Sources & licences | `listSources`, `getSource`, `listLicences`, `getLicence` |
| Meta | `getVocabularies`, `getHealth` |
| Feeds | `feedProposals`, `feedOpportunities`, `feedEvents` (RSS + JSON Feed) |

**Not implemented this sprint** (each is a deliberate scope cut, not an oversight):

- `listProposalMatches`, `listOpportunityMatches`, `listMatches`, `getMatch` — `api/openapi.yaml`
  marks these `x-sprint: 3`, and no writer for the `match` table exists yet (`pipeline/resolve.py`
  and a matching stage are later, data-scientist-owned work). The `match` **model** is
  implemented per the task brief; only the read endpoints are deferred.
- `getDocument` — the task's entity list for this sprint (`proposal` through `licence`, 13
  tables) does not include `document`, and no document ingestion pipeline exists yet.
- `/v1/intake/*`, `/v1/reports` — `x-sprint: 3` in the spec.
- Sitemaps, `/feeds/saved/{token}` — the first is a `web/` concern (`docs/23` §9.2 open decision
  11 says so explicitly); the second needs a Pro-tier `saved_search`, out of scope.
- Pro, API-key and admin surfaces — explicitly excluded by the task brief.

## Open decisions made this sprint

Recorded here rather than silently assumed, per `docs/04-standards.md` P-6/P-7. None of these
are architecture changes; all are bounded follow-ups.

1. **No cross-source entity resolution.** `pipeline/resolve.py` (fusing the same real-world
   project seen by multiple sources into one `proposal`/`opportunity` row) is a later,
   data-scientist-owned stage. The loader creates one record per `(source_id,
   source_record_id)` 1:1. The `field_provenance`/`min_reuse_class`/mixed-provenance machinery
   (docs/21 §8) is still exercised — it just has one source per record until resolve.py lands.
2. **No geocoding.** `location.geom` is left null; `precision` is `county_centroid` /
   `state_centroid` from the state/county strings a connector already parsed. The restricted
   -precision rule (docs/04 D-9) and the map's clustering both work correctly on null geometry
   (features are counted as "unplaced" rather than dropped, docs/04 D-8) but nothing plots on a
   real map yet without a geocoder.
3. **Record `publish_state` defaults to `public`.** Per-record admin publish/unpublish (US-905)
   is out of scope this sprint. Since the loader already refuses any source whose licence isn't
   `open`/`attribution` before a row is ever written, every record that reaches the store is, by
   construction, from a publishable source — so it is loaded straight to `public` rather than
   sitting in `pending_review` forever with no admin surface to move it. Once admin ships, it
   gains the ability to demote individual records without a loader change.
4. ~~**Public lag default: 14 days supply / 7 days opportunities**~~ **Superseded 2026-09-19 and
   closed 2026-09-21 by owner decision: the paywall is by shape, and nothing is delayed by time.**
   First "alerts, exports, API and watchlists are paid; free users see every record; the delay is
   kept only on ISO change events"; then, on the measurement that the surviving delay was
   recoverable from two public reads of an undelayed record page, that delay was dropped too —
   with its knob: the manifest's `change_event_lag_days`, `source.lag_days`/`lag_overrides`
   (migration `0019`) and the admin `PATCH` fields are all gone, so reintroducing a delay is a
   code change, not a setting. `record_public_at` is the identity and is what every row, record or
   event, is written with. The three-way 7-vs-14 conflict between `docs/20` A-8, `docs/21` D-1 and
   `docs/04` §10 pick 1 is closed rather than settled: none of the numbers applies to anything.
   Migrations `0016` and `0019` recomputed `public_at` on existing rows, because it is stored
   rather than computed. `services/api/visibility.py` was **not** edited for either decision — the
   predicate reads the same column it always did, which is why the licence gate provably cannot
   have loosened (`tests/test_licence_gate_survives_removing_the_delay.py`).
5. **Source-level gate for the public tier requires `publish_state = 'public'` exactly**, not
   `api_only` (docs/21 §5.4's `source_permits`). The loader's own default for a newly-seen source
   is `api_only` (open decision 3's flip side: a source is publishable-by-licence before an
   operator has necessarily reviewed it for the public surface specifically). This means freshly
   ingested data needs one more step — a real admin `publish_state` flip — before the public API
   shows it; `services/api/test_routes.py::test_source_gate_refusal_hides_records_from_public_api`
   asserts this boundary explicitly so it isn't loosened by accident later.
6. **Single-field sort, forward-only cursor pagination.** `sort=a,b` (multi-field) is parsed but
   only the first field is applied; `page.prev_cursor` is always `null` (reverse iteration is not
   implemented). Both are honestly represented in the response shape (no fabricated cursor, no
   silently-ignored second sort key beyond dropping it) rather than pretended-complete.
7. **Filter coverage is a named subset, not the full grammar.** Every filter parameter this
   service *declares* as allowed is real, tested and effective; parameters `api/openapi.yaml`
   lists but this sprint does not implement (e.g. `changed_key`, `updated_since`, `county_fips`,
   `sponsor_id`/`issuer_id`, `budget_amount[gte]`, `capacity_sought_mw[gte]`) are **not** in this
   service's allowlist and so answer `400 unknown_parameter` rather than being silently accepted
   and ignored — docs/04 API-3 treats a silently-dropped filter as a licence-sensitive leak, and
   an honest 400 is safer than a filter that looks like it works and doesn't.
8. **Geo clustering is a pure-Python lon/lat grid**, not PostGIS `ST_ClusterKMeans`/
   `ST_SnapToGrid` — see the Postgres/SQLite section above. Correct on both backends; not
   representative of production query performance at the `docs/04` D-13 budget (≤400 ms p95,
   ≥20,000 records).
9. **`event.seq`** is assigned by an ORM `before_insert` listener (`MAX(seq)+1` in the current
   transaction) rather than a database `IDENTITY` column, because a Postgres `bigint identity`
   default has no SQLite equivalent this sprint's test target can exercise. The canonical
   migration still declares a real Postgres `IDENTITY` column; a production deployment should
   prefer that default over the ORM-side assignment (documented in `services/db/models.py`'s
   `Event.seq` docstring, including the one operational caveat: events must be flushed one at a
   time, not batched into a single multi-row INSERT, for the listener's `MAX()` read to be
   correct).
10. **Rate-limit headers are static**, per the task brief ("rate-limit headers (static values for
    now)") — `RateLimit-Limit: 60`, `RateLimit-Remaining: 59` on every response, not a real
    sliding-window counter. `docs/23` §6's actual per-tier/per-key limits are a follow-up once
    Pro/API auth exists.
11. **Licence flags are derived generically from `reuse_class`**, not read from
    `data/sources.yaml`'s free-text `license` field. `allows_raw_publication` is `True` for every
    ingested source this sprint (all are `open`/`attribution` by the gate), so the CAISO-style
    "attribution licence that still withholds raw rows" case from docs/21 §8 is not yet
    distinguished from a plain attribution licence — the schema and the API's raw-gating code
    path both support it (`Licence.allows_raw_publication`, `provenance_row`'s check), it just
    needs the per-source override wired from the registry (a data-engineer task, not a backend
    typing/logic gap).

## Migrations

```bash
# Canonical (requires a real Postgres 16 + PostGIS instance; not run in this sandbox):
DATABASE_URL=postgresql+psycopg://user:pass@host/db \
  .venv/bin/alembic -c services/db/migrations/alembic.ini upgrade head

# Sanity-check the revision graph without a database:
.venv/bin/alembic -c services/db/migrations/alembic.ini history
```

## Loader fixes (2026-09-13)

Fixed the two `services/ingest/loader.py` limitations `services/resolve/README.md` documented but
left unfixed (out of that task's assigned paths). Neither changes the loader's public functions
(`upsert_licence_and_source`, `load_dataframe`, `load_from_files`) — both are internal to the
row loop and `_get_or_create_organization`.

**1. Intra-run id reuse** (docs/22 §5/§7.1). The loader now tells apart, per row, three cases
using both the natural key (`source_id`, `source_record_id`) and the connector's own composite
`record_id`:
- A row whose natural key already has an active link **from an earlier `load_dataframe` call**:
  an update, through the unchanged diff/event path — no behaviour change.
- A row whose natural key was already assigned to the **same** `record_id` earlier **in this same
  call** (a dataframe that bundles more than one historical observation of one record, as
  `services/resolve/report.py`'s evaluation snapshot does): also an ordinary update against the
  same stored key — not reuse. (`test_repeated_record_id_in_one_dataframe_is_a_sequential_update_not_reuse`.)
- A row whose natural key was already assigned to a **different** `record_id` earlier in this
  call — true intra-run id reuse, the ISO-NE/NYISO signature — is never merged: its
  `source_record_id` is suffixed deterministically (`#2`, `#3`, ...), the same convention
  `pipeline.connectors.base.Connector.finalize` already applies to `record_id` for
  `dedupe_strategy = "suffix"` sources, and a warning is appended to both `LoadResult.warnings`
  and, when a `SourceRun` is passed, `source_run.dq`/`dq_status` (`{check, level, detail, data}`,
  the same shape `pipeline.connectors.dq.Check` uses).
  (`test_intra_run_id_reuse_keeps_both_records_and_warns`,
  `test_intra_run_id_reuse_does_not_confuse_a_later_run_update`.)

**2. Organisation slug/punctuation collisions** (docs/21 §3.5/§3.6). `_get_or_create_organization`
now matches in two tiers: an exact case-folded spelling match first (unchanged fast path, keeps
compatibility with rows already keyed on plain `name.lower()`, e.g.
`services/resolve/report.py`'s pre-seeding), then, on a miss, a case/whitespace/punctuation-only
normalised match (`_org_punct_key`) against organisations not yet merged away. Two spellings that
match only on the second tier resolve to the *same* organisation, and every distinct raw spelling
seen — including the one an organisation was first created from — is recorded as its own
`organization_alias` row, so a punctuation-only pair leaves the org with two aliases rather than
raising `UNIQUE constraint failed: organization.slug`. Two names differing in letters still create
two organisations; `services/resolve/merge.py`'s separate, heavier corp-suffix-stripping fuzzy pass
is unchanged and out of scope here.
(`test_organization_punctuation_only_spellings_merge_with_two_aliases`,
`test_organization_letter_distinct_names_stay_separate`,
`test_organization_repeated_alternate_spelling_does_not_duplicate_alias`.)

### `services/resolve/report.py` before/after

Before (verbatim from `services/resolve/README.md`, pre-fix):

```
  nyiso    -> us.iso.nyiso.gen_queue     reuse=attribution +1,814 created  1,350 updated
  ...
proposals in store before resolution: 8,212
...
proposals in store after resolution (surviving/canonical rows): 7,770
  of which with >=2 sources: 273
proposals absorbed (merged_into_id set): 442
...
organizations before: 3,034  after: 2,784
```

After (this fix, same `data/eval/normalized.parquet` pull, `python -m services.resolve.report`):

```
  nyiso    -> us.iso.nyiso.gen_queue     reuse=attribution +3,164 created     0 updated

proposals in store before resolution: 9,563
organizations in store before resolution: 3,034

resolver clusters with >=2 store-loaded members: 278
clusters merged:   275 (absorbing 442 records)
clusters proposed (gate failed, filed for review): 3

proposals in store after resolution (surviving/canonical rows): 9,121
  of which with >=2 sources: 273
proposals absorbed (merged_into_id set): 442

organizations before: 3,034  after: 2,784
usable labels: 77 of 85 (8 touch a gated source and are excluded)
tp=35 fp=2 fn=2 tn=38
precision=0.946  recall=0.946
```

**Yes, the counts changed — by more than "slightly"**: `nyiso` created rows go from 1,814 to
3,164 (+1,350), so proposals before resolution go from 8,212 to 9,563 and after resolution from
7,770 to 9,121. This is not a fix-caused regression; it is 1,350 real rows the old loader was
silently collapsing. Measured directly against `data/eval/normalized.parquet`: `nyiso` has 3,164
rows but only 1,814 distinct `source_record_id` values, and the old loader kept exactly one stored
row per distinct `source_record_id` — the rest were silently folded in as "updates" inside the
same single `load_dataframe` call. Of the four `source_record_id` values with more than one
occurrence, two (`0031`, `0127A`, 2 each) are the documented ISO-NE/NYISO case (docs/22 §5); the
other two are large content-hash collisions (`content_hash()`, `pipeline/connectors/base.py`) among
genuinely blank rows — 1,063 rows under `he0f6cee65a13` and 287 under `h07cc5855b76c`, every field
null except a single shared `retrieved_at` and `lifecycle_state = "withdrawn"` — which the
connector's own `dedupe_strategy = "suffix"` already gave distinct, sequential `record_id`s
(`nyiso:he0f6cee65a13` .. `#1063`) because they are, in fact, 1,063 distinct rows in the source
register, even though their content happens to be indistinguishable. Per the task's rule ("two
distinct records share an id... keep both... never merge them"), this loader now keeps all of
them rather than silently discarding 1,349 of them with no event and no warning.

Everything downstream of ingestion is unaffected: clusters merged (275, absorbing 442), the 3
gate-refused clusters, proposals with >= 2 sources (273), organization resolution (3,034 -> 2,784,
212 groups), and the 85-label precision/recall (0.946/0.946) are all identical before and after —
the newly-preserved blank rows carry no content the resolver can match against, so they never join
a cluster; they just stop being invisible.

### Full suite (verbatim, 2026-09-13, this fix)

Excludes `web/` (another agent mid-edit there) and `tests/test_web_default_view.py` (imports
`web.data_loading`, which currently fails on an unrelated pre-existing bug — a naive/aware
`datetime` comparison in `apply_preview_lag_override` — not touched by this task):

```
$ .venv/bin/python -m pytest tests pipeline services --ignore=tests/test_web_default_view.py
............................................................................... [ 15%]
............................................................................... [ 31%]
............................................................................... [ 46%]
............................................................................... [ 62%]
............................................................................... [ 77%]
............................................................................... [ 93%]
................................                                                 [100%]
464 passed, 20 warnings in 9.11s
```

`services/ingest/test_loader.py` itself grew from 9 to 15 tests (the 6 new ones listed above), all
passing. The repo-wide "463 passed" figure recorded in `services/resolve/README.md` the same day
was a *different* `pytest` invocation (plain `pytest`, over `testpaths` including `web/`) run
before this fix, so it is not a like-for-like baseline for the 464 above (a different scope,
deliberately excluding `web/` per this task's instructions); it is included only to show that nothing
this fix touches regressed the count that mattered before.

### Lint and types (verbatim, 2026-09-13)

```
$ .venv/bin/python -m ruff check services/db services/ingest services/ids.py
All checks passed!

$ .venv/bin/python -m ruff format --check services/db services/ingest services/ids.py
13 files already formatted

$ .venv/bin/python -m mypy services/db services/ingest services/ids.py
Success: no issues found in 10 source files
```

## Sprint 2 fixes (2026-09-13, backend-developer)

Seven fixes requested against the real, full `data/normalized/*` data (10,409 proposals / 960
opportunities once every source is loaded through the unmodified loader — matches the counts
`docs/00-PLAN.md`'s 2026-09-12 decision log already recorded). Each is measured against that real
volume, not a synthetic fixture, except where noted.

### 1. Visibility-predicate performance (fixed: an index, not a rewrite)

`services/api/visibility.py`'s `_has_public_source` (`EXISTS (SELECT ... FROM proposal_source JOIN
source ...)`) was already the right shape — a set-based semi-join, not a per-row Python loop — and
`public_at` was already materialised (docs/21 §5.4). The 30-75s/page report was one missing index:
neither `proposal_source.proposal_id` nor `opportunity_source.opportunity_id` had one, so both
SQLite and (structurally) Postgres plan a full table scan of `proposal_source` for *every*
candidate proposal row. Confirmed directly with `EXPLAIN QUERY PLAN` before/after:

```
-- before: SCAN proposal_source (no usable index on the correlated join column)
-- after:  SEARCH proposal_source USING INDEX ix_proposal_source_proposal_id (proposal_id=?)
```

Fix: `sa.Index("ix_proposal_source_proposal_id", "proposal_id")` and
`sa.Index("ix_opportunity_source_opportunity_id", "opportunity_id")` on the two link tables, plus
`sa.Index("ix_source_publish_state", "publish_state")` on `source` (the other predicate leg;
`source` is tiny so this one is defence-in-depth, not the fix). Migration
`services/db/migrations/versions/0002_licence_quote_and_visibility_indexes.py`. Measured on the
real full load (10,409 proposals) with the DB warm in both runs:

```
BEFORE (no visibility index), GET /v1/proposals?limit=50 over 10,409 proposals: 21.84s
AFTER  (with visibility index), same query:                                     0.127s
```

That reproduces the reported bug directly (the 30-75s range in `web/README.md` depends on exact
row counts/machine; this sandbox's full load reproduces it at ~22s per page, ~170x slower than
after the fix) and confirms the index alone closes it — no query rewrite was needed.

A second, independent cost showed up once the index made the query itself fast: `GET
/v1/proposals/geo` loads *every* visible proposal (not one page), and two ORM defaults that are
fine at list-page size become the dominant cost at ~10,400 rows: `Proposal.sponsor`'s `lazy=
"joined"` eager load (never read by the geo response) and materialising every column of every
`Proposal`/`Location` row (`services/api/geo.py` reads about a dozen of ~30). Fixed in
`services/api/app.py`:
- `_proposal_geo_plottable_query` restricts to placeable rows in SQL (`Location.geom.is_not(None)`,
  an inner join) and `load_only`s exactly the columns `services/api/geo.py` reads
  (`_GEO_PROPOSAL_COLUMNS`/`_GEO_LOCATION_COLUMNS`); `Proposal.sponsor` is dropped entirely
  (`noload`); `Location.source`/`Location.licence` fall back to a per-record lazy load, bounded by
  `SPLIT_THRESHOLD` (≤ 500) since only the below-threshold individual-feature path needs them.
- `_proposal_geo_totals` computes `records`/`lifecycle_state_counts`/`technology_counts`/
  `unplaced_count` over the *whole* filtered set with one plain-column query (not full ORM
  entities), replacing a Python loop over every `Proposal` object.
- `_source_licence_aggregate` (`services/api/serialize.py::licence_summary_from_source_aggregates`)
  computes `licence_summary` with one `GROUP BY source_id` SQL aggregate instead of loading every
  visible `proposal_source` row as an ORM object to fold in Python.

Measured end to end (`GET /v1/proposals/geo`, full 10,409-proposal load, FastAPI `TestClient`, DB
warm — see "Cluster counts per zoom" below for the same run's feature counts):

```
zoom=1  0.515s   zoom=2  0.511s   zoom=3  0.465s   zoom=4  0.512s
zoom=5  0.529s   zoom=6  0.489s   zoom=8  0.477s
GET /v1/proposals?limit=50            -> 0.12-0.19s (well under the 200ms budget)
GET /v1/proposals?limit=50&include=count -> 0.16s
```

Direct function-level profiling (bypassing `TestClient`'s synchronous-over-async thread portal,
which this sandbox measures at 50-100ms of its own overhead) shows the query/business-logic layer
itself at ~0.35-0.46s — comfortably under the 500ms target. The end-to-end numbers above are
consistently in the 0.46-0.53s band: right at, and on a couple of samples very slightly over, the
500ms target through this specific test harness. Reported honestly rather than rounded down: this
is a ~150x improvement (21.8s -> ~0.5s) and the remaining margin is framework/threading overhead in
`starlette.testclient.TestClient`, not the database layer this task asked to fix — a real ASGI
server measured the same query directly at 0.44-0.57s over HTTP (`uvicorn` + `curl`, same data),
consistent with the function-level number plus real HTTP overhead. `tests/test_api_geo_performance.py`
asserts a 1.0s/0.5s budget (with the stated margin) against the real EIA-860M file rather than a
flaky exact-500ms assertion, so CI does not fail on ordinary machine variance while still catching
a real regression.

**Postgres plan.** The same two indexes apply verbatim (`CREATE INDEX` is dialect-portable here);
Postgres's planner already prefers a hash or merge join over `proposal_source` when a btree index
exists on the join column, so the mechanism (avoid a sequential scan of `proposal_source` per outer
row) is identical, and should be faster given Postgres's parallel query and better statistics —
this is written from first principles, not measured, since Postgres is not installed in this
environment (see the top of this file). The `load_only`/`noload`/aggregate-query changes are
ORM-level and apply unchanged; the SQLite-specific `technologies` filter branch (item 5 below) does
not, and is written to spec but unexercised there. Before trusting either number in production: run
`EXPLAIN (ANALYZE, BUFFERS)` on the same queries against a real Postgres 16 + PostGIS instance with
representative statistics (`ANALYZE proposal_source;`).

### 2. Licence correctness: derived from the registry, not hardcoded

`upsert_licence_and_source` no longer hardcodes `allows_raw_publication=True`. `open` sources are
always raw-allowed; `restricted`/`unknown` are refused before reaching this code at all (unchanged).
For `attribution` sources: docs/21 §8's general row was "everything, at lag, with credit" (raw
**allowed**) — only sources whose own recorded terms say otherwise are a *derived-only override*
(the CAISO/NYISO row, "attribution, raw withheld"). That override is per-source, not a blanket rule
over the whole `attribution` class: `data/sources.yaml` has no dedicated boolean for it yet
(recorded as a data-engineer follow-up, was open decision #11 below), so `_is_derived_only_override`
reads the signal that already exists — CAISO's and NYISO's own `notes` text literally says
"derived-only" (`docs/00-PLAN.md`'s 2026-09-12 legal register: "CAISO and NYISO derived-only with
credit"), and no other currently-loaded attribution source's notes do. This is a deliberate,
documented departure from a literal "attribution ⇒ derived-only" reading of this task's brief:
that reading would also demote GB NESO (a real, currently-loaded attribution source whose own
terms are raw-ok with mandatory credit, no reuse restriction) to derived-only for no reason in the
registry or the legal register — `test_plain_attribution_source_without_a_derived_only_note_stays_raw_allowed`
proves the distinction holds.

`Licence.quote_text` (new column, item 5) is populated from `SourceEntry.license` at the same time.
`Source.publish_state` now defaults from the registry class too (item 6, see below), fixed in the
same function.

Verified against the real registry (`test_licence_flags_derived_from_registry_reuse_not_hardcoded`,
`services/ingest/test_loader.py`):

| Source | `reuse` | `allows_raw_publication` | note |
|---|---|---|---|
| `us.iso.caiso.gen_queue` | attribution | **False** | derived-only override (own terms say so) |
| `us.iso.nyiso.gen_queue` | attribution | **False** | derived-only override (own terms say so) |
| `us.iso.ercot.gen_queue` | open | **True** | unaffected |
| synthetic plain-attribution (GB-NESO-shaped) | attribution | **True** | no override signal |

### 3. Geocoding

`services/ingest/geocode.py` (new): a straight copy of `web/build_data.py`'s `CountyGazetteer` /
`normalize_county_name` logic and the vendored public-domain table
(`services/ingest/data/us_county_centroids.tsv`, copied from `web/data_ref/`), so `services/`
no longer depends on `web/` to plot anything. `_get_or_create_location` (`services/ingest/loader.py`)
now calls it: a resolvable county -> its centroid (`county_centroid`); an unresolvable county with a
resolvable state -> the state centroid (`state_centroid`); neither -> `unknown`, still counted (D-8,
never dropped). A derived-only-override source's locations get `precision_reason = "licence"`
(docs/04 D-9) regardless of which of those three tiers applies, since raw/exact geo is withheld
structurally for that source, not because this sprint's geocoder happens to produce only
county/state tiers anyway. Never `exact` — that needs real coordinates from the source itself
(EIA-860M's raw `Latitude`/`Longitude`); promoting those is an explicitly out-of-scope follow-up
(`web/data_loading.py::backfill_eia_exact_points` remains a real gap, not fixed here — see the
worklist at the end of this section).

Measured against the real full load: 8,183 of 8,211 US proposals from the four ISO/EIA sources
placed (99.7%); 7,787 `county_centroid`, 396 `state_centroid`, 24 truly `unknown` (multi-county
NYISO spans and similar, honestly unplaced rather than guessed). 9 new tests in
`services/ingest/test_loader.py` cover the gazetteer, the fallback chain, the NYISO borough alias,
and the loader integration end to end.

### 4. Geo clustering granularity

`services/api/geo.py`'s grid was `cell_deg = 360 / 2**zoom`, clamped to zoom 1-12 — at zoom 3 that's
a 45° cell, covering the entire continental US (~60°×24°) in 2-6 cells; `web/README.md`'s reported
"three clusters" defect. Replaced with `cell_deg = max(BASE_CELL_DEG / 2**(zoom-1), MIN_CELL_DEG)`,
`BASE_CELL_DEG = 36.0`, tuned empirically against the real placed-point set (8,154 continental-US
points once EIA-860M is included — the earlier three sources alone are too geographically
concentrated to reach 20 clusters at zoom 3 no matter the constant, since ERCOT/CAISO/NYISO are
each confined to one state or region). `bbox` is now enforced (`web/README.md` item 4: previously
accepted, echoed, and never applied) — `_in_bbox` filters plottable points before clustering, so
panning/zooming the map changes what's returned; `totals`/`unplaced_count` stay scoped to the whole
filter match rather than the viewport (a record with no geometry can't be "in" any bbox, and D-8
requires it stays counted), which is unobservable difference for `web/app.py`'s own sitewide-notice
call since it already passes a whole-world bbox.

Cluster counts per zoom, full real dataset (10,409 proposals, `bbox=-125,24,-66,50` = continental
US), `GET /v1/proposals/geo`:

| zoom | clusters | in target range (20-60)? |
|---|---|---|
| 1 | 5 | below (expected — whole-world-scale cell) |
| 2 | 7 | below |
| **3** | **22** | **yes** |
| **4** | **56** | **yes** |
| 5 | 159 | above (finer than the task's named range, by design — zoom 5 should show more detail than zoom 4) |
| 6 | 390 | above |
| 8 | 911 | above |

`tests/test_api_geo_performance.py::test_geo_clustering_zoom_3_and_4_yield_20_to_60_clusters_over_conus`
asserts the zoom-3/4 range against the real, unmodified `data/normalized/us.eia.860m` file loaded
through the real loader (not a synthetic fixture) — chosen over loading all five proposal sources
because EIA-860M alone (nationwide coverage) already produces the same 20-60 range the full set
does, at roughly a quarter of the loader's real ingest time (~16s here vs. ~100s+ for
ERCOT+CAISO+NYISO+EIA, since `services/ingest/loader.py` upserts row-by-row). `services/api/test_routes.py`
adds fast synthetic tests for the mechanism itself: `test_geo_bbox_is_enforced` (a Texas-bbox query
returns only the Texas point, not the New York one, while `totals.records` stays at 2),
`test_geo_unplaced_records_are_counted_not_dropped`, and
`test_geo_restricted_precision_reason_renders_for_derived_only_sources`.

### 5. API gaps from `web/README.md`

- **Slug-keyed lookup** (item 1): added a `slug` query parameter to `GET /v1/proposals` and `GET
  /v1/opportunities` (`api/openapi.yaml`'s new `SlugFilter` component parameter) rather than a
  migration — **`proposal.slug`/`opportunity.slug`/`organization.slug` already existed**, already
  unique, and were already maintained by the loader (`services/db/models.py`,
  `services/ingest/loader.py`'s `entity.slug = ...` assignment); the task brief assumed they did
  not. What was actually missing, per `web/README.md`, was a way to *look up* by slug without
  scanning up to 200 search results — `?slug=<value>` does that directly. This does not implement
  `slug_history`/301-redirect semantics for merged/renamed records (no `slug_history` table exists
  yet; `merged_into_id` does, but no merge writer runs this sprint) — documented as a real, narrower
  gap in the new `SlugFilter` parameter's description, not silently pretended complete.
- **`technologies` filter on opportunities** (item 5): now applied
  (`_opportunity_technologies_filter`, `services/api/app.py`) everywhere opportunities are queried
  (`GET /v1/opportunities`, `/geo`, `/organizations/{id}/opportunities`). Any-of match; an
  all-source opportunity (`technologies = []`) matches every value, per `api/openapi.yaml`'s
  `Technologies` parameter description. Dialect-specific: Postgres uses the native `&&` array
  overlap operator (written to spec, unexercised here); SQLite (`technologies` is a JSON-encoded
  `TEXT` column, `services/db/types.py TextArray`) matches the JSON-quoted token as a substring
  after casting the column to `Text` — casting matters: `.like()` directly on the `TextArray`
  column binds the pattern *through the column's own type*, JSON-encoding `%"wind"%` into a JSON
  array of individual characters and silently matching nothing (caught by
  `test_opportunities_technologies_filter_is_enforced` before the cast fix was added).
- **Organization detail route** (item 6): `GET /v1/organizations/{public_id}` was **already
  implemented** (`services/api/app.py::get_organization`, present before this task) — `web/README.md`'s
  item 6 is about the *site* having no page for it, not the API lacking the route.
  `test_organization_detail_route` asserts the counts and the 404 boundary explicitly since nothing
  previously exercised this route directly in `services/api/test_routes.py`.
- **Licence quote/notes field** (item 3): `Licence.quote_text` (new column, migration
  `0002_licence_quote_and_visibility_indexes.py`) carries `data/sources.yaml`'s free-text `license`
  clause verbatim, populated by the loader from `SourceEntry.license`. Exposed on both the embedded
  `LicenceSummaryEmbedded` schema (so it appears on `Source.licence` and the standalone `Licence`
  resource) — `notes` (data-engineer commentary, sometimes about in-progress legal review, e.g.
  CAISO's "Publish derived-only until counsel resolves...") deliberately stays out of the public
  shape (still admin-only, `AdminLicence`); `test_source_and_licence_expose_a_quote_text_field`
  proves both halves.
- **`bbox` enforcement on `/v1/proposals/geo`**: covered under item 4 above (same fix, same tests).

### 6. `publish_state` default from the registry class

`upsert_licence_and_source` now sets a new source's `publish_state` to `"public"` when its registry
`reuse` is `open`/`attribution` (both already gate-cleared by the time this line runs —
`_assert_not_gated` and the "both must hold" re-check both raise first for anything else), `
"ingest_only"` otherwise (defensively; that branch is not reachable through the public loader path,
same reasoning as `services/api/visibility.py`'s own drift-guard comment). This **supersedes**
Sprint 1's open decision #5 below, which deliberately kept the old `"api_only"` default and pushed
the flip onto a not-yet-built admin action; `web/data_loading.py::_flip_publish_state_public`'s
docstring already argued this exact change was "the same call an admin publish action would do" —
this task makes that argument the loader's own behaviour instead of a frontend workaround.
`test_source_publish_state_defaults_from_registry_class` and the updated
`test_load_from_files_reads_parquet_and_run_json` (now asserts `"public"`, not `"api_only"`) cover
it.

### `web/data_loading.py` overrides this makes unnecessary

Per-correction verdict, for the web team (`web/README.md`'s numbered list references these same
three):

1. **Geocoding backfill** — `backfill_locations()` is now unnecessary (the loader geocodes at
   ingest time, item 3 above). `backfill_eia_exact_points()` is **not** unnecessary — EIA-860M's raw
   `Latitude`/`Longitude` promotion to `exact` precision is explicitly out of this sprint's scope
   (documented above); the site still needs it for EIA-860M's exact points until a real geocoder or
   a similar per-source raw-coordinate promotion lands in `services/ingest/loader.py`.
2. **CAISO/NYISO's derived-only correction** — `apply_derived_only_licence_correction()` is now
   unnecessary. The loader derives `allows_raw_publication` and stamps `location.precision_reason`
   correctly at ingest time (item 2 above); re-running the site's version on top would be an
   idempotent no-op (confirmed: `pytest web tests/test_web_default_view.py
   tests/test_web_provenance.py` — 18 tests — still pass unchanged against this sprint's loader).
3. **Source `publish_state`** — `_flip_publish_state_public()` is now unnecessary (item 6 above);
   same idempotent-no-op confirmation.

None of `web/`'s files were edited to reach this verdict — `web/data_loading.py` was read only, and
the full `web`/`tests/test_web_*` suite (18 tests) was re-run unmodified against this sprint's
`services/` changes to confirm nothing regressed.

### Full suite and lint (verbatim, 2026-09-13, this task)

```
$ .venv/bin/python -m pytest tests pipeline services --ignore=tests/test_web_default_view.py
............................................................................... [ 14%]
............................................................................... [ 29%]
............................................................................... [ 44%]
............................................................................... [ 59%]
............................................................................... [ 73%]
............................................................................... [ 88%]
.......................................................                        [100%]
487 passed, 20 warnings in 26.79s
```

487 (up from 464 before this task): +9 `services/ingest/test_loader.py` (licence derivation,
geocoding, publish-state default), +8 `services/api/test_routes.py` (bbox enforcement, unplaced
counting, restricted-precision rendering, slug filter, technologies filter, organization detail,
licence quote field), +5 `tests/test_api_geo_performance.py` (real-data cluster ranges and latency
budgets), +1 net from updating `test_load_from_files_reads_parquet_and_run_json`'s `publish_state`
assertion. `tests/test_api_geo_performance.py` is intentionally slower than the rest of the suite
(~16s, dominated by the loader's real ~100-rows/second ingest path over the real EIA-860M file, not
this task's own code) — flagged in its own module docstring; run `pytest -k "not
geo_performance"` for a fast local loop.

`web tests/test_web_default_view.py tests/test_web_provenance.py` (18 tests, another agent's tree,
read-only for this task): unchanged, still pass — see the `web/data_loading.py` verdict above.

```
$ .venv/bin/python -m ruff check services/db services/ingest services/api services/ids.py tests/test_api_contract.py tests/test_api_geo_performance.py
All checks passed!

$ .venv/bin/python -m ruff format --check services/db services/ingest services/api services/ids.py tests/test_api_contract.py tests/test_api_geo_performance.py
30 files already formatted

$ .venv/bin/python -m mypy services/db services/ingest services/api services/ids.py
Success: no issues found in 22 source files
```

```
$ .venv/bin/python -m openapi_spec_validator api/openapi.yaml
api/openapi.yaml: OK

$ .venv/bin/python api/check_story_coverage.py
RESULT: PASS — 44/44 PRD stories covered by 118 operations; all $refs resolve
```

`api/README.md`'s stated parameter count (117) is now stale by one (`SlugFilter`); not updated here
since it is outside this task's assigned paths — flagged for whoever owns `api/README.md` next.

### New open decision

12. **A "derived-only" licence override is detected from free text** (`_DERIVED_ONLY_RE` matching
    "derived-only"/"derived only" in a source's `notes`/`license`), not a dedicated
    `data/sources.yaml` field — the same gap `services/README.md` open decision #11 already named
    ("needs the per-source override wired from the registry"), now wired to the only signal that
    actually exists rather than left hardcoded. A dedicated boolean field
    (`derived_only_override: true`) would be more robust than text matching against two known
    real-world phrasings, and is a fair follow-up for whoever owns `data/sources.yaml` next; until
    then, a new attribution source that should be derived-only must say so in its `notes` or
    `license` text using that phrase, or it will load as raw-allowed by default (matching docs/21
    §8's general attribution row, the safer default for a source nobody has flagged otherwise).

## Pro tier and alerts

Sprint 2 backend brief: auth and API keys, an `entitlement` parameter on the visibility predicate,
saved searches and alerts, and API-tier webhooks. Builds on the Sprint 2 public-tier store and API
above without rewriting it — `services/api/app.py`, `services/api/visibility.py` and
`services/api/serialize.py` are extended in place; the new surface lives in its own modules.

### What was added

```
services/db/models.py            + account, user, session (as UserSession), api_key, saved_search,
                                    alert, webhook_endpoint, webhook_delivery (docs/21 §3.12-§3.17)
services/db/migrations/versions/0003_pro_tier_and_alerts.py   canonical Postgres DDL for the above
services/api/auth.py             argon2 password hashing; signed-cookie sessions backed by a
                                  revocable server row; API-key generation/hashing/resolution; a
                                  dry-run EmailPort + ResendEmailAdapter; AuthContext and the
                                  require_authenticated/require_entitlement/require_scope/
                                  require_session_only/require_admin FastAPI dependencies
services/api/ratelimit.py        InMemoryRateLimiter (real token buckets, one process-wide
                                  instance) behind a RateLimiter protocol a Redis implementation
                                  can satisfy identically
services/api/audit.py            record_audit_event — writes the admin/key audit trail as
                                  ordinary `event` rows (actor_type='user', required reason)
services/api/visibility.py       extended: every *_visibility_filter takes an `entitlement`;
                                  *_public_filter kept as the entitlement="public" case
services/api/serialize.py        extended: build_meta takes `tier`; serializers for
                                  User/Account/SavedSearch/Alert/ApiKey/WebhookEndpoint/
                                  WebhookDelivery
services/api/pro.py               the new router (mounted from services/api/app.py) — see the
                                  endpoint table below
services/api/errors.py           extended: ProblemError carries optional `headers` (Retry-After)
services/api/feeds.py            extended: render_rss/render_json_feed/feed_title take `live`
services/api/deps.py             get_db now commits on a clean return / rolls back on exception
                                  (Sprint 2's routes were all reads; this sprint's are the first
                                  writes, and a bare `finally: db.close()` silently discards them)
services/alerts/matching.py      saved-search/webhook query grammar evaluated against one row
services/alerts/evaluate.py      run_alert_cycle: matches new events since each saved search's
                                  watermark_seq, digests every match in one pass into one Alert
                                  per channel, sends through EmailPort
services/alerts/feed.py          rt_ token generation; the private live saved-search feed
services/alerts/webhooks.py      HMAC signing/verification, endpoint matching, enqueue/replay,
                                  attempt_delivery/deliver_pending with exponential backoff
```

### Endpoints (all `x-status: live` in `api/openapi.yaml` as of this sprint)

| Method & path | Notes |
|---|---|
| `GET /v1/me` | Any authenticated session or key; a key-only caller renders as its own creator |
| `GET /v1/account` | Pro+; `subscriptions` is always `[]` this sprint (no billing adapter) |
| `GET/POST /v1/saved-searches`, `GET/PATCH/DELETE /v1/saved-searches/{id}` | CRUD; quota 25/user |
| `POST /v1/saved-searches/{id}/preview` | Runs the stored query live, does not move the watermark |
| `GET /v1/alerts`, `GET /v1/alerts/{id}` | Delivery history for the caller |
| `GET/POST /v1/keys`, `DELETE /v1/keys/{id}` | Session-only create/revoke; 5/account; `admin:*` rejected |
| `GET/POST /v1/webhooks`, `GET/DELETE /v1/webhooks/{id}` | Session (api entitlement) or `write:webhooks` key; 10/account |
| `POST /v1/webhooks/{id}/test`, `.../replay`, `GET .../deliveries` | Enqueue only — delivery is a separate worker tick (`deliver_pending`) |
| `GET /feeds/saved/{rss_token}` | No auth; the token is the credential; `lag_days=0`, as every feed is |
| `PUT /admin/v1/accounts/{account_id}/entitlement` | New this sprint, operator/owner only — see decision 1 below |

### Open decisions

1. **No billing/CRM adapter this sprint (as the task brief instructs).** Entitlements are set
   directly by `PUT /admin/v1/accounts/{account_id}/entitlement` with
   `entitlement_source = "manual_grant"` — the same escape hatch docs/21 §3.13 already models,
   audited as an `admin_edit` event. Superseded once Sprint 3 wires Stripe/HubSpot behind
   `POST /admin/v1/customers` and `/admin/v1/subscriptions`; this endpoint should then be
   restricted or removed, not left as a parallel path to `sor`-sourced entitlements.
2. **Password auth, not docs/20 §7's passwordless magic-link/Google.** The task brief explicitly
   asks for "signed cookie, argon2 password hashing"; `User.auth_provider` gained a `password`
   value alongside `magic_link`/`google` rather than replacing them. No public registration/login
   HTTP endpoint exists — `api/openapi.yaml` (normative per docs/04 API-1) has no `/v1/auth/*`
   path, and inventing one without a `docs/23` entry would itself put the contract out of sync.
   `register_user`/`authenticate_user`/`create_session` are tested library functions a web front
   end (or a future spec'd endpoint) calls; this sprint's tests call them directly, the same
   pattern `services/api/conftest.py`'s fixtures already use for domain rows.
3. **Rate limiting is real for anonymous public traffic and for every `services/api/pro.py`
   route, not yet for a Pro/API credential calling one of the original Sprint 2 public routes
   directly** (`GET /v1/proposals` etc. with a session or key attached). Crediting that call to
   the anonymous per-IP bucket would double-count against unrelated anonymous traffic on the same
   IP, and this sprint does not thread `AuthContext` resolution into the shared middleware.
   Follow-up: one rate-limiting dependency shared by both routers.
4. **`saved_search.channels` supports `email` and `rss`, not `webhook`.** Docs/23 §9.1 describes
   a webhook as "a saved search with a URL as its channel" — that description is about the
   separate `webhook_endpoint` mechanism (its own `query`/`entity` fields), not a literal
   `saved_search.channels` entry; this sprint keeps the two mechanisms distinct rather than
   collapsing them, since `webhook_endpoint` already carries what a channel entry would need
   (URL, secret, delivery log) and `alert` does not model any of that.
5. **`WebhookEndpoint.secret` is stored in the clear**, not hashed like `ApiKey.key_hash`.
   Signing an outbound HMAC delivery needs the actual secret every time — a one-way hash cannot
   work here, unlike a bearer credential the platform only ever verifies. "Shown once" in
   `api/openapi.yaml` describes the client-facing UX (Stripe/GitHub webhooks work the same way),
   not server-side discard. This sprint has no envelope-encryption/KMS story for secrets at rest;
   flagged as a follow-up, the same gap docs/04 E-19 already calls out for deploy-time secrets.
6. **No scheduled worker.** `run_alert_cycle` and `deliver_pending` are functions a Procrastinate
   job (ADR 0004) should call on an interval in production; this sprint ships and tests the
   functions, not the periodic-task wiring (`infra/scheduler/` is devops-engineer's tree, out of
   this task's scope).
7. **Matching re-implements a named subset of the filter grammar** (`services/alerts/matching.py`)
   rather than generalising `services/api/app.py`'s `_apply_proposal_filters` et al. to also drive
   in-Python matching against one already-loaded row. Reusing those would have meant restructuring
   Sprint 2's already-shipped, already-tested public list endpoints; this sprint's matcher covers
   the same named subset of parameters (docs/04 §10 "Filter coverage is a named subset" decision,
   unchanged) plus `q`. A follow-up could unify both under one grammar module.
8. **Rotation is create-then-revoke**, not a dedicated endpoint — `api/openapi.yaml` defines only
   `createKey` (session-only) and `revokeKey`; task brief calls for a "rotation endpoint" and this
   is the workflow that satisfies it without adding an undocumented path (tested end to end in
   `tests/test_api_pro_keys.py::test_rotation_is_revoke_then_create_and_only_the_new_secret_works`).
9. **`event.seqs` on `alert`, docs/21 §3.16's `bigint[]`, is stored as `jsonb`** (`JSONVariant`),
   matching this sprint's one other integer-array need rather than adding a new column type for
   both.
10. **The admin surface has no second host/second-factor enforcement here** — `require_admin`
    checks `user.role` only. docs/20 §7's `admin.` hostname and MFA are an infra/reverse-proxy
    concern (ADR 0005, `infra/`), out of this task's scope; this sprint's admin check is a
    necessary-but-not-sufficient building block, not a claim that the full control is in place.

### Verbatim output (this sprint, 2026-09-13)

```
$ pytest tests --ignore=tests/test_web_default_view.py services/db services/ingest services/api pipeline
588 passed, 27 warnings in 48.26s

$ ruff check services/api services/db services/alerts services/ids.py tests
All checks passed!

$ ruff format --check services/api services/db services/alerts services/ids.py
33 files already formatted

$ mypy services
Success: no issues found in 46 source files

$ python api/check_story_coverage.py --quiet
RESULT: PASS — 44/44 PRD stories covered by 119 operations; all $refs resolve
```

97 new tests: `tests/test_api_pro_auth.py` (24), `tests/test_api_pro_tier.py` (5),
`tests/test_api_pro_keys.py` (9), `tests/test_api_pro_saved_searches.py` (12),
`tests/test_api_pro_webhooks.py` (10), `tests/test_api_pro_admin.py` (5),
`tests/test_api_pro_ratelimit.py` (7), `tests/test_alerts_webhooks.py` (15),
`tests/test_alerts_evaluate.py` (10) — plus 6 new Pro-tier schema checks added to
`tests/test_api_contract.py` (`MeResponse`, `SavedSearchDetailResponse`/`ListResponse`,
`ApiKeyCreatedResponse`/`ListResponse`, `WebhookEndpointCreatedResponse`/`ListResponse`).
Covers: tier visibility (public vs pro vs api on the same record, `api_only`-source visibility,
restricted-source invisibility on every tier); key scopes, quota, revocation and rotation; the
audit trail on key issue/revoke and the admin entitlement grant; saved-search CRUD, quota, query
matching (kind/jurisdiction/capacity/technologies/due date/event type), RSS-token minting and
toggling; alert digest grouping (multiple matches → one `Alert` per channel) and the exactly-once
watermark; webhook HMAC sign/verify, endpoint matching by type+query, retry backoff scheduling,
pause-after-24-failures, and replay-from-seq; the real per-tier token bucket (allow/block
boundary, per-key isolation, `Retry-After`) at both the middleware and route level.

### Remaining for billing (Sprint 3)

**Update 2026-09-13 (Sprint 3 first wave):** the first three items below are done — the Attio and
Stripe adapters live in `services/crm/` and `services/billing/` behind `services/sor/ports.py`
(each tree has its own README with the numbered decisions), `POST /v1/auth/*` in
`services/api/auth_routes.py` enforces the seat limit, and `web/` has the login surface. The interim
`PUT /admin/v1/accounts/{id}/entitlement` is kept deliberately as the manual override (docs/00-PLAN.md
Sprint 3 kickoff), not removed. The last two items remain open for the workers wave.

- The real CRM/ERP + Stripe adapter (ADR 0006) behind `POST/GET /admin/v1/customers` and
  `/admin/v1/subscriptions`, writing `account.entitlement_source = "sor"`; the interim
  `PUT /admin/v1/accounts/{id}/entitlement` (`manual_grant`) should then be scoped down or removed.
- Seat-limit enforcement (`account.seats`/`seats_used`, `403 seat_limit`) — modelled in the schema,
  not enforced by any route yet.
- Wiring a real login/registration surface (web-facing), and deciding whether magic-link/Google
  ship alongside password auth or are dropped from the spec.
- A scheduled job actually invoking `run_alert_cycle`/`deliver_pending` (Procrastinate).
- Metering Pro/API credentials on the original Sprint 2 public routes (open decision 3 above).

## Bulk-insert pass (Sprint 3)

Sprint 3 item 4 (`docs/00-PLAN.md`'s "Sprint 3 kickoff" and 2026-09-13 decision log row "Site now
reads the public API end to end"): the loader ran at ≈100 rows/second (the full-data site test
took 451s). Task: measure, speed up `services/ingest/loader.py`'s row-loop hot path without
changing behaviour, and measure again. Method followed exactly as briefed — measured numbers only,
recorded before any change.

### Benchmark input

`data/normalized/*` (the real connector output the site loads) does not exist in this environment
— no connector has run here this session, so `tests/test_web_default_view.py` and
`web/dev_up.py` cannot run either (confirmed: `ls data/normalized` → "No such file or directory").
`data/eval/normalized.parquet` (14,388 Phase 2 resolution-evaluation records, docs/22) is the only
ingest-scale fixture actually present, so it is the benchmark input, exactly as the task named it.
Two of its six short source ids — `spp` and `isone` — are `restricted` under
`docs/13-legal-data-rights.md` and must never enter the store (CLAUDE.md's guardrail); the new
`services/ingest/bench_loader.py` excludes them rather than remapping them, the same scope
`web/build_data.py::EVAL_SHORT_ID_MAP` already uses for this exact fixture (reproduced, not
imported, to keep the benchmark script independent of `web/`). That leaves four sources — `ercot`,
`caiso`, `nyiso`, `eia860m` — **9,563 rows**, matching the count this repo already recorded for the
same fixture (`services/README.md`'s own "Loader fixes" section above: "proposals in store before
resolution: 9,563"). Run with:

```
.venv/bin/python -m services.ingest.bench_loader --runs 2
```

### Baseline (measured before any change)

Loaded into a fresh SQLite **file** database (not `:memory:` — closer to Postgres's real commit
costs, per the task's method), through the unmodified loader at that day's commit (retrieved via
`git show HEAD:services/ingest/loader.py` into a throwaway module so the working tree never had to
be reverted to measure it):

```
baseline: 9563 rows across 4 sources
run 1: 9563 rows in 98.299s (97.3 rows/s)  [setup 0.055s, load 98.218s, commit 0.026s]
run 2: 9563 rows in 102.436s (93.4 rows/s) [setup 0.013s, load 102.391s, commit 0.032s]
```

**93–97 rows/second**, consistent with the ≈100 rows/s already on record and with `docs/00-PLAN.md`'s
451s figure for the larger, nine-source real site load (different, larger scope — 5 proposal + 4
opportunity sources plus the EIA exact-point backfill and preview-lag pass — so not a like-for-like
number, but the same order of magnitude at the same "row-at-a-time, no bulk path" rate the PLAN
already named).

### Profile: the top costs

`cProfile` over just the `ercot` slice of the fixture (1,778 rows, one source, small enough to
profile without the profiler's own overhead dominating the wall clock) against the unmodified
baseline loader:

```
1778 calls to _get_or_create_organization: 30.516s cumulative of 37.603s total (81%)
  -> each miss re-ran `select(Organization).where(merged_into_id.is_(None))` and iterated the
     *entire* result to find a punctuation-normalised match -- an O(n) scan repeated once per row,
     so total work grows O(rows x organisations) as the organisation table fills up.
  -> 864,714 ORM row instantiations across those re-queries (orm/loading.py:_instance,
     instrumentation.py:new_instance), each paying JSON-decode of the `ids` JSONB column
     (867,280 json.loads calls, 4.24s cumulative) and a UUID parse (877,044 calls, 2.28s) for data
     the caller never uses beyond `name_canonical`.
18,574 session.flush() calls: 5.65s cumulative (three per newly created proposal — one to learn
  the generated id for `public_id`/`slug`, one after setting them, one after creating the link —
  plus one per update row's implicit autoflush on the next SELECT).
```

Exactly the suspects the task brief named: a per-row (here, per-miss) linear scan standing in for
an index, per-row `flush()`, and autoflush-triggered round trips. Per-row geocoding was **not** a
measurable cost — `services/ingest/geocode.py` already process-wide-caches the county gazetteer
(`default_gazetteer()`), so that suspect was already closed before this task started.

### What changed, and why

All in `services/ingest/loader.py`; no public function's signature lost compatibility
(`load_dataframe` gained one optional keyword, `batch_size`, default 500) and no gate, dedupe,
event-emission or `SourceRun` behaviour changed:

1. **One `_LoadCache` built once per `load_dataframe` call** (not per row): the active
   `proposal_source`/`opportunity_source` links for this source, the entities those links point
   at, every organisation indexed by exact and by punctuation-normalised name, every
   `organization_alias` key already on file, and every `organization.slug` in use. Four `SELECT`s
   total, replacing what used to be one to several `SELECT`s *per row* — the exact-match
   organisation lookup, the O(n) punctuation-match scan above, the alias existence check, and the
   slug-collision check are now dict/set membership tests. The county gazetteer is fetched once
   into the cache too (`gaz=cache.gaz`) instead of a `default_gazetteer()` cache-check per row.
2. **Every id generated client-side before the row that needs it**, via the same `new_uuid()`
   callable the ORM column `default=` already used (`services/db/models.py`), instead of at flush
   time. `public_id`/`slug` no longer need a flush-then-overwrite dance (the original code wrote a
   throwaway random `public_id`, flushed to learn the real id, then overwrote it) — they are
   computed once, correctly, at construction. This is what makes batching possible at all: nothing
   in the row loop depends on a flush to learn an id it needs for the next row.
3. **`itertuples`-equivalent row access**: `records_df.to_dict("records")` / `events_df.to_dict("records")`
   once per call, replacing `.iterrows()`'s per-row `Series` construction. `_row_get` and the two
   `_fields_from_row` helpers now type against `Mapping[str, Any]` so the same code reads either a
   `pandas.Series` or a plain `dict`.
4. **`session.no_autoflush` around the row loop**, with `session.flush()` called explicitly every
   `batch_size` rows (default 500, `DEFAULT_BATCH_SIZE`) via `_flush_pending`, plus once more after
   the loop. Two flushes, not one, at each boundary: entities/organisations/locations/aliases
   first, then the `ProposalSource`/`OpportunitySource` rows created alongside them
   (`pending_links`, added to the session only at that second step). This order is load-bearing,
   not stylistic — `ProposalSource`/`OpportunitySource` carry no ORM `relationship()` back to
   `Proposal`/`Opportunity` (only the reverse, `Proposal.sources`, and that one is `viewonly=True`,
   deliberately excluded from SQLAlchemy's flush-dependency tracking), so without this split a link
   and its entity flushed in the same statement batch can be sent in either order and the link's
   foreign key trips against a proposal that doesn't exist yet — hit and fixed during this task
   (first pass batched both together and every `test_loader.py` create test failed on
   `IntegrityError: FOREIGN KEY constraint failed`). `Organization`/`Location` *do* carry a real
   (non-`viewonly`) `relationship()` from `Proposal`, so they were never affected and needed no
   such split.
5. **`Event.seq` is deliberately NOT batched.** `services/db/models.py`'s `_assign_event_seq`
   `before_insert` listener computes `MAX(seq) + 1` from the database at insert time and says so
   explicitly: "events must be flushed one at a time... or several rows in the same batch would
   compute the same MAX() and collide." The event loop keeps the original per-event
   `session.add(event); session.flush()` unchanged; the only optimisation there is preloading
   `Event.idempotency_key` for this `source.id` once before the loop (one `SELECT`) instead of one
   `SELECT` per event to check idempotency, and the same preloaded set for the `diff_type ==
   "removed"` branch's link lookup (`cache.links_by_entity`) instead of a `SELECT` per removal.

Nothing else changed: the licence gate (`_assert_not_gated`, the "both must hold" re-check in
`upsert_licence_and_source`) still runs, and still runs, before any row of a call is written; the
intra-run id-reuse suffixing, the organisation punctuation-match/alias logic's *matching rules*,
the derived-only licence/precision-reason stamping, and the diff→event type mapping are all
untouched — only how each of those looks things up changed.

### After (measured, same benchmark, same machine)

```
services/ingest/bench_loader: 9563 rows across 4 sources, batch_size=500
run 1: 9563 rows in 4.059s (2356.1 rows/s) [setup 0.065s, load 3.924s, commit 0.070s]
run 2: 9563 rows in 3.975s (2405.5 rows/s) [setup 0.014s, load 3.903s, commit 0.059s]
```

**≈2,380 rows/second average (2356–2406 across the two runs) versus ≈95 rows/second average
(93–97) before — a ≈25x speedup**, well past the task's 5x bar. The same `ercot`-slice `cProfile`
run against the optimised loader drops from 37.6s to **2.15s** (a consistent ≈17x on that smaller,
profiler-overhead-inflated slice) with no single function left dominating — remaining cost is
ordinary SQLAlchemy attribute-set/flush/object-construction overhead spread across the two
`_flush_pending` calls that batch of 1,778 rows triggers, not a per-row query or scan.

### Decisions

- **Benchmark source**: `data/eval/normalized.parquet`, four open/attribution sources
  (9,563 rows), for the reason given above (`data/normalized/*` absent in this environment). The
  full ~11,400-row, nine-source site load `docs/00-PLAN.md` originally measured (451s) was not
  re-run — extrapolating this benchmark's ≈2,380 rows/s to that row count gives roughly 5s, but
  that is an extrapolation, not a measurement, and is reported as one in the "Deferred" note below.
- **`batch_size` default is 500**, matching the task brief; exposed as a keyword argument on
  `load_dataframe` (`DEFAULT_BATCH_SIZE` in `services/ingest/loader.py`) so a caller can tune it
  without a code change. `test_batch_size_does_not_change_what_gets_written` pins that
  `batch_size=1` and `batch_size=500` write byte-identical content (a deterministic digest
  excluding wall-clock columns and random ids) for the same input.
  `test_rerun_with_default_batch_size_is_idempotent` and
  `test_gate_refusal_writes_nothing_to_the_store` pin the other two behaviours the task asked to be
  pinned (idempotent re-run; the gate refuses before any write).
- **`Event` inserts stay one-at-a-time.** Documented above as a hard constraint from
  `services/db/models.py`'s `seq` listener, not a missed optimisation — batching them would risk
  duplicate `seq` values under Postgres concurrency semantics the SQLite test target doesn't
  surface. A real fix (a Postgres `bigint identity`/sequence column instead of the portable
  `MAX()+1` stand-in) is a `services/db/*` change, out of this task's write scope.
- **No location dedupe added.** The task's preload list names "locations" alongside organisation
  keys/aliases; the existing loader never deduplicated locations (every new proposal gets its own
  `Location` row even if another proposal already has the identical county centroid) and this task
  did not change that — only removed the per-location `flush()` (the id is generated client-side
  like everything else now) and passed one shared `CountyGazetteer` instance through the cache
  instead of a `default_gazetteer()` cache-check per row. Adding real location dedupe would change
  which rows get written (fewer `location` rows, `location_id` reused across proposals) and so,
  per the task's rule, was left alone rather than done as a drive-by.

### Deferred / follow-ups

- Re-run `tests/test_web_default_view.py` and `web/dev_up.py`'s full ~11,400-row load once
  `data/normalized/*` exists in an environment that has it, to get a measured (not extrapolated)
  number for the real site load `docs/00-PLAN.md`'s 451s referred to.
- `Event.seq`'s `MAX()+1` stand-in (see Decisions) should become a real sequence/identity column
  in the canonical Postgres migration so event inserts can eventually batch too — `services/db/*`
  is out of this task's write scope.
- Real location dedupe (see Decisions) — a separate, semantics-changing task, not a speed-only one.

### Verbatim: full check, this task

```
$ .venv/bin/python -m pytest services/ingest tests/test_connector_gating.py tests/test_resolve_store.py
............................................................                                     [100%]
60 passed, 1 warning in 2.30s

$ .venv/bin/python -m pytest tests/test_api_contract.py services/api/test_routes.py
........................................................................ [ 66%]
.....................................                                    [100%]
109 passed, 27 warnings in 5.85s

$ .venv/bin/ruff check services/ingest && .venv/bin/ruff format --check services/ingest
All checks passed!
6 files already formatted

$ .venv/bin/mypy --cache-dir /tmp/mypy-ingest services/ingest
Success: no issues found in 5 source files
```

## EIA exact-point promotion (Sprint 3)

Sprint 3 item 5, data follow-ups: `services/ingest/loader.py`'s own geocoder only ever produced
`county_centroid`/`state_centroid`/`unknown` (the "Loader fixes" section above and this module's
own docstring both said so directly); `web/data_loading.py::backfill_eia_exact_points` patched
EIA-860M's exact points onto the store from the frontend side, after the fact, as a documented
stopgap. Task: promote a row's real coordinates inside the loader itself so that frontend
correction is no longer needed for any source, not just EIA-860M's special case.

### What changed

`services/ingest/loader.py` only:

- **`_extract_exact_point(raw_payload)`** (new): a plain `raw_payload["Latitude"]`/`["Longitude"]`
  lookup (verified against `pipeline/connectors/us_eia_860m/connector.py`'s `parse()` output —
  the Planned sheet's own column names, carried through unchanged onto `proposal_source.raw` by
  `Connector.finalize`), validated numeric, inside world bounds (`-90..90` / `-180..180`), and not
  the `(0, 0)` placeholder an unset field commonly serialises to. Written as a key lookup rather
  than an EIA-specific branch, so any other source whose raw payload already carries the same two
  keys is promoted identically with no further loader change. Note on how "checked" this claim is:
  `raw` is a pass-through of each connector's parsed source columns verbatim (`Connector.finalize`
  -> `raw_json(r)`), so a source's raw key names live in its *data*, not in connector code — a
  source code grep for "atitude"/"ongitude" across `pipeline/connectors` and `pipeline/normalize.py`
  (both come back empty) rules out a connector that *renames* a coordinate column onto those two
  keys itself, but cannot rule out an upstream source file that happens to use the same column
  headers EIA-860M does. No other source in `data/sources.yaml`'s current registry publishes
  coordinate columns per `docs/13-legal-data-rights.md`'s source notes, so this is not expected in
  practice this sprint — flagged as the honest limit of what a static check here can confirm.
- **`_get_or_create_location`**: tries `_extract_exact_point` first, before the county/state
  geocoder, *unless* the source is `derived_only` (docs/04 D-9 — an exact coordinate is a raw
  field, withheld exactly like any other raw field for a source whose licence withholds raw geo,
  regardless of what its raw payload carries). A validated point wins outright (docs/04 D-8):
  `precision = "exact"`, `kind = "point"`, `geocoder = "source_provided"`. An invalid or missing
  pair, or a `derived_only` source, falls through to the existing county/state geocoding
  unchanged — same call, same gazetteer, same `precision_reason = "licence"` stamping.
- **`LoadResult.locations_exact_promoted`** (new field): counts rows promoted to `exact` in the
  call, alongside the existing `locations_created`.
- The bulk-insert pass's batching and the licence gate are untouched — `_extract_exact_point` is a
  pure function over one row's already-parsed `raw` dict, so it works identically at any
  `batch_size` (pinned by `test_exact_promotion_batch_size_does_not_change_what_gets_written`).

`web/data_loading.py::backfill_eia_exact_points` and its call in `load_dev_database` are removed
(the loader now does this at ingest time, for every source, not just EIA-860M); the
`eia_exact_points` report key is dropped with it — no test asserts that key (checked
`tests/test_web_default_view.py`, `web/test_e2e.py`, and every other `load_dev_database(...)`
call site before removing it), so nothing had to keep a placeholder value for it.

### Tests

Nine new tests in `services/ingest/test_loader.py`: a valid EIA-shaped raw payload promotes to
`exact`/`point`/`source_provided` with the right `geom`; five invalid-pair cases (`(0, 0)`, lat out
of range, lon out of range, a non-numeric value, a missing half of the pair) each fall back to
`county_centroid` and are not counted as promoted; a row with no raw coordinates at all is
unaffected (`geocoder` stays `None`, matching the pre-existing path byte-for-byte); a
`derived_only` source with a *valid* raw pair still falls back, with `precision_reason = "licence"`
(docs/04 D-9); re-running the same EIA frame is idempotent (one `location` row, still `exact`, and
the update path — which never touches `location` — creates zero new promotions); `batch_size=1`
vs `500` over a frame mixing a promoted and a non-promoted row produce byte-identical stores (same
digest helper the bulk-insert pass's own pinning test uses); and one true end-to-end test through
the real connector (`pipeline.connectors.us_eia_860m.connector.Connector.parse`/`normalize` over
`tests/fixtures/eia860m_planned.xlsx`, via the root `conftest.py` helpers the connector's own test
uses) — all 25 fixture rows promote to `exact`, confirming the raw key names match production, not
just this file's own hand-built fixture rows.

### Benchmark: before / after

`services/ingest/bench_loader.py` was not modified (out of this task's write scope) and its input,
`data/eval/normalized.parquet`, predates the `raw` payload column entirely for every one of its
four sources (`"raw" in frame.columns` is `False`) — a Phase 2 entity-resolution fixture from
before `pipeline.connectors.base.Connector.finalize` started attaching `raw`, not merely an
EIA-specific gap. So the benchmark's own EIA-860M rows carry nothing to promote, by construction of
its fixture, and this change is measurably free on it — confirmed, not assumed, by instrumenting a
throwaway run of the same four frames and reading `LoadResult.locations_exact_promoted` off each
(all zero). Measured with `.venv/bin/python -m services.ingest.bench_loader --runs 3`, before
(`git show HEAD:services/ingest/loader.py` swapped in, then the working edit restored and
re-verified byte-identical against the pre-swap copy — the same technique the "Bulk-insert pass"
baseline above used, so the working tree was never actually reverted):

```
before: run 1: 9563 rows in 4.151s (2303.7 rows/s) [setup 0.062s, load 4.010s, commit 0.080s]
        run 2: 9563 rows in 3.953s (2419.4 rows/s) [setup 0.013s, load 3.868s, commit 0.071s]
        run 3: 9563 rows in 4.111s (2326.1 rows/s) [setup 0.014s, load 4.033s, commit 0.064s]
after:  run 1: 9563 rows in 4.213s (2270.0 rows/s) [setup 0.054s, load 4.098s, commit 0.061s]
        run 2: 9563 rows in 4.015s (2381.9 rows/s) [setup 0.013s, load 3.936s, commit 0.066s]
        run 3: 9563 rows in 3.983s (2401.2 rows/s) [setup 0.013s, load 3.904s, commit 0.065s]
```

**~2,270–2,420 rows/s both before and after** (run-to-run noise, no measurable regression); **0 of
9,563 benchmark rows promoted** for the reason above. For a number that actually exercises
promotion, the same connector-fixture path the new end-to-end test uses (`tests/fixtures/
eia860m_planned.xlsx`, real `raw` payload, real `Latitude`/`Longitude` keys): **25 of 25 rows
promoted to `exact`** (`locations_created == locations_exact_promoted == 25`). The real
`data/normalized/us.eia.860m/*.parquet` this promotion is built for is absent from this
environment (same gap the "Bulk-insert pass" section above recorded for the full nine-source
site load), so this 25-row connector-fixture run is the closest available measurement of real
production behaviour; a future run with real connector output should re-measure against it and
should see the fraction of EIA-860M rows carrying a valid pair, not exactly 25/25 (a small curated
sample skews cleaner than the wild).

### Decisions

- **Scope kept to record creation, not update.** `_get_or_create_location` is only called when a
  new `Proposal` is created; re-ingesting a row whose entity already exists never touches its
  existing `location` (true before this task, unchanged by it). A location created before this
  promotion existed (e.g. a store loaded under the previous loader) is not retroactively upgraded
  by a later re-ingest of the same row — only a genuinely new record is promoted. Nothing in the
  task brief or the existing test suite required upgrade-on-update, and adding it would touch the
  update branch's behaviour for every source, not just this one's slice — flagged here rather than
  done silently; a follow-up one-off backfill (in the shape `backfill_eia_exact_points` used to be,
  but calling the loader's own `_extract_exact_point`/`_get_or_create_location` instead of
  reimplementing the check) is the natural way to upgrade pre-existing rows, if wanted.
- **Key lookup, not a source-id branch.** `_extract_exact_point` does not check `source.id ==
  "us.eia.860m"` anywhere — it looks for `Latitude`/`Longitude` on whatever `raw` payload it is
  given, gated only by `derived_only`. No other connector's own code names a coordinate column
  (grep for "atitude"/"ongitude" across `pipeline/connectors` and `pipeline/normalize.py` is
  empty), so today this promotes EIA-860M rows only in practice; a future source whose raw payload
  happens to carry the same two keys (renamed by its own connector, or a new source using EIA's
  column names) is promoted with no loader change — the behaviour the task brief asked for ("any
  other source whose normalised frame already has them").
- **`geocoder` only set on the new path.** The pre-existing county/state branch still leaves
  `Location.geocoder` as `None` (unchanged) rather than retroactively backfilling `"census_tiger"`
  onto it — out of this task's scope ("otherwise fall through to the existing county/state
  geocoding unchanged").

## Context layer endpoints (2026-09-15)

`services/api/context_geo.py`, `services/api/context_routes.py` and `services/api/ui_events.py`
implement the two API surfaces `docs/00-PLAN.md`'s 2026-09-14 "Built-infrastructure context layer"
decision needs: `built_plant` (docs/21 §3.20) drawn under the proposals map, and `ui_event`
(docs/21 §3.21) as its identifier-free engagement measurement. Neither table is a proposal or
opportunity: no lifecycle, no lag, no tier or licence gating (every source in scope this sprint,
EIA-860M, is public domain) — both endpoints are public on every tier.

### Endpoints

| Method & path | Notes |
|---|---|
| `GET /v1/context/plants/geo` | `bbox`/`zoom` required; `technology` (csv, `pipeline.normalize.classify_tech` vocabulary, unknown → 400) and `country` (csv) optional. Same grid clustering as `services/api/geo.py` (`_grid_cell`/`SPLIT_THRESHOLD`/`_in_bbox` imported, not copied); ≤ 2,000 features |
| `POST /v1/ui-events` | No auth; 202, empty body; `name` must be in `UI_EVENT_NAMES`, `props` allowlisted per name and rejected outright (not merely dropped) if any string value looks like an email, IPv4/IPv6, UUID or a 20+ char token; 60/min per IP (IP used for limiting only, never stored) |
| `GET /admin/v1/ui-events/summary` | Operator/owner only; `weeks` (default 8, 1-52); weekly counts per event name, `map.layer_toggled` split into `count_on`/`count_off` |

`services/api/pro.py`'s `POST /v1/saved-searches` (the product's "alert") now also writes a
`UiEvent(name="alert.created", props={})` in the same session on success.

### Response shapes

`GET /v1/context/plants/geo` returns the standard envelope
(`{data, meta, licence_summary, redactions}`) with `data` a GeoJSON `FeatureCollection`:
- individual: `feature_kind: "plant"`, `id` = the plant's uuid as a string, `properties` carries
  `name, operator_name, technology, technology_raw, technologies (object), capacity_mw,
  generator_count, earliest_operating_year, state_code, county_name, source: {source_id,
  source_name, source_url, retrieved_at, licence_id, licence_name}`.
- cluster: `feature_kind: "plant_cluster"`, `properties` carries `count, technology_counts,
  capacity_mw_sum, dominant_technology, bbox, expands_to_zoom`.
- `totals: {records, clustered, technology_counts}` — `records`/`technology_counts` are the full
  filter match (not bbox-scoped), matching `services/api/geo.py`'s convention for the proposals map.

`GET /admin/v1/ui-events/summary` returns the standard envelope with `data` an array of
`{week: "2026-Www", name, count, count_on?, count_off?}` (the `on`/`off` split present only for
`map.layer_toggled` rows).

### Pan/zoom performance (coordinator follow-up, 2026-09-15)

**Measured problem.** Verified against the real load (14,659 `built_plant` rows,
`DATABASE_URL=sqlite+pysqlite:///web/.data/dev.db`, in-process `TestClient`): the original
`GET /v1/context/plants/geo` — one `select(BuiltPlant)` with `Source`/`Licence` eagerly joined (the
model's own default relationship strategy) for *every visible, placed plant*, on *every request*,
regardless of viewport or how many features that request actually rendered — cost 860-960 ms per
pan/zoom (~60 µs/row of ORM hydration). docs/04 D-13's budget for a pan/zoom to updated markers is
≤ 200 ms.

**Fix.** `services/api/context_routes.py` now keeps a process-local cache
(`_index_cache`/`_get_plant_index`) of the *whole* placed-plant table as `PlantIndexRow` — a small
dataclass (`id, lon, lat, technology, capacity_mw, country`, `services/api/context_geo.py`) built
from one plain `select()` of five columns, no join, no `BuiltPlant` ORM hydration. The cache is
keyed on `(bind_id, row_count, max(updated_at))` over the placed rows — an insert, update or delete
always changes one of those two aggregates (`updated_at` has `onupdate=utcnow`), so a stale cache
is never served, and the one aggregate query that checks the key on every request is cheap (no join,
no per-row hydration). `technology`/`country` filtering happens in Python over the cached index,
never as a second SQL query, so re-filtering never touches the database and `records_total`/
`technology_counts` stay correct for the request's own filters (only the licence-summary aggregate,
already a cheap `GROUP BY` over a handful of sources, still queries live). A full `BuiltPlant` (with
its `Source`/`Licence` join, for `name`/`technologies`/`source` block/…) is fetched by id — bounded
to at most `SPLIT_THRESHOLD` (500) rows — only when the in-view count is at or below that threshold,
i.e. only for plants that actually render as individual markers; a clustered response never touches
`BuiltPlant` at all. `id`/`geom` are additionally selected `CAST(... AS TEXT)` rather than through
their mapped `GUID`/`GeographyPoint` columns: profiling showed that even the join-free five-column
index query paid roughly half its cost in those two `TypeDecorator`s' per-value `process_result_value`
dispatch over 14,659 rows; casting to plain text and parsing directly (`id` is already the
decorator's own `str(uuid)` string, so no parsing at all; `geom`'s JSON is one `json.loads`) cut the
index rebuild from ~140 ms to ~50-110 ms depending on OS page-cache state.

**Measured before/after** (same four requests, same real 14,659-row file, in-process `TestClient`;
feature counts match the coordinator's own measurement exactly — 62/83/18/45 — proving the
refactor changed nothing observable about clustering):

| Request | Before (every request paid full ORM+join hydration) | After — index cache cold, connection warm | After — index cache warm (steady-state pan/zoom) |
|---|---|---|---|
| `zoom=4  bbox=-125,24,-66,50` (62 clusters) | 959 ms | 246 ms *(see note)* | **37 ms** |
| `zoom=6  bbox=-104,25,-93,37` (83 clusters) | 863 ms | 156 ms | **29 ms** |
| `zoom=12 bbox=-97.9,30.1,-97.5,30.5` (18 plants) | 893 ms | 105 ms | **28 ms** |
| `technology=wind` (45 clusters, 1,365 matching rows) | 77 ms | 127 ms | **11 ms** |

**Note on the one number still over budget.** The 246 ms figure is the *very first* query this
process ever runs against a freshly-copied 78 MB SQLite file — a one-time OS page-cache miss
reading the `built_plant` table's pages off disk for the first time (confirmed by isolating it: a
trivial `SELECT 1` as the first query on the same fresh connection costs < 1 ms, but the index's
`WHERE geom IS NOT NULL` / `max(updated_at)` aggregate — a genuine full-table scan, no index covers
either condition — costs ~50 ms cold vs ~3 ms once those pages are warm). Every other number in the
"index cache cold" column already reflects this same connection with its page cache warm, and is
comfortably under 200 ms; the *steady-state* condition the D-13 budget actually describes
("pan/zoom to updated markers") is the warm-cache column, at 11-37 ms — 23-87x faster than the
original implementation. Eliminating the one-time cold-file number too would need either a
`(geom, updated_at)` index (a migration — `services/db/models.py` and the migrations directory were
not in this follow-up's file scope) or a startup warm-query (`services/api/app.py` — also out of
this follow-up's stated scope, "same file area ... nothing else"); flagged for the coordinator to
scope as a separate task if wanted.

**Tests added** (`services/api/test_context_routes.py`):
- `test_index_cache_invalidates_after_row_update` — changes a plant's `technology` and explicitly
  bumps `updated_at` by a day (so the assertion can never be a false pass from wall-clock
  resolution), and asserts the very next request reflects the change rather than serving a stale
  cached index.
- `test_cached_and_uncached_index_paths_agree` — 700 plants (above `SPLIT_THRESHOLD`, exercising
  the clustered path the coordinator's own zoom=4/zoom=6 measurements used), two identical
  requests, and asserts both (a) `resp1.json()["data"] == resp2.json()["data"]` byte-for-byte and
  (b) `_rebuild_plant_index` (monkeypatched with a call counter) ran exactly once across the two
  requests — proving the second request genuinely hit the cache rather than a key-computation bug
  silently rebuilding every time.
- The existing 9 tests were re-run unchanged against the refactored implementation (same public
  route, same response shapes) and pass with zero modifications — the caching change is invisible
  to any caller.

### Decisions

1. **`validation_error` is 400, not 422, everywhere it is used in this codebase**
   (`services/api/errors.py::ERROR_CODES["validation_error"] = 400`, confirmed by every existing
   caller and by `services/api/test_routes.py::test_unknown_query_parameter_is_400`). The task
   brief describes the technology-vocabulary and `ui-events` `props` rejections as "422"; this
   implementation follows the shared helper's actual, codebase-wide status (400) rather than
   introduce a one-off inconsistent code, since `errors.py` was read-only for this task. Flagging
   for the architect: if 422 is genuinely wanted here, `errors.py` needs a second helper (or
   `ERROR_CODES` needs a second `validation_error`-shaped code), not a local workaround.
2. **`_grid_cell`/`_in_bbox` imported directly from `services/api/geo.py`, not aliased.** Both are
   already private-by-convention but not access-restricted by any ruff rule in this repo's
   `pyproject.toml`; importing them directly (rather than duplicating the tuned grid formula, or
   renaming with a compatibility alias the task allowed as a fallback) keeps the one tuned
   implementation in one place and `services/api/geo.py`'s own tests are unaffected — verified by
   running its test module unchanged as part of the full suite.
3. **`GET /v1/context/plants/geo` has no `AuthContext`/tier dependency at all** — not even the
   `ctx.entitlement` used purely for `meta.tier` elsewhere. The layer is public on every tier by
   design (docs/00-PLAN.md 2026-09-14), so there is no behaviour a caller's credential could
   change; `meta` is built with `tier="public", lag_days=0` unconditionally. The endpoint still
   goes through `services/api/app.py`'s `standard_headers` middleware, so anonymous callers are
   still metered by the existing per-IP public bucket (60/hour) like every other route.
4. **`country` filter is CSV (`in_(...)`)**, matching this codebase's "comma-separated for OR"
   convention (docs/23 §7) even though the task brief's wording ("optional, default none = all")
   reads as a single value; a single value is also valid CSV, so this is a superset, not a
   deviation.
5. **`records_total`/`technology_counts` in `totals` are restricted to `geom IS NOT NULL` rows**,
   same as the plottable set, rather than counting every matching plant regardless of placement —
   docs/21 §3.20 has no "unplaced" concept for context plants (every row in scope this sprint has a
   source-supplied coordinate), unlike `services/api/geo.py`'s proposals, which do count unplaced
   rows into `meta.unplaced_count`.
6. **`x-sprint: 4`** on the three new OpenAPI operations — the first operations in the repo past
   Sprint 3's close; no sprint number is otherwise assigned to this post-launch work in
   `docs/00-PLAN.md` yet, so this documents "the sprint after Sprint 3" rather than inventing a
   name for it.
7. **`build_plant_feature_collection`'s signature changed** (coordinator follow-up, "pan/zoom
   performance" above): it now takes `index: list[PlantIndexRow]` and an optional
   `plant_details: dict[str, BuiltPlant]` instead of `plants: list[BuiltPlant]`. This is a breaking
   change to a function with no other caller in the repo (`services/api/context_routes.py` is the
   only importer), made because the whole point of the fix is that the common (clustered) case
   never needs a `BuiltPlant` at all — keeping the old signature and only optimizing the caller
   would have left `context_geo.py` still declaring a `BuiltPlant`-shaped contract it no longer
   needs for most calls.
8. **The in-view/`SPLIT_THRESHOLD` decision is made twice** — once in
   `services/api/context_routes.py` (to decide whether to query `plant_details` at all) and once
   inside `build_plant_feature_collection` itself (to decide whether to build individual or cluster
   features). Both use the same imported `_in_bbox`/`SPLIT_THRESHOLD`, so they cannot disagree, and
   the repeated bbox filter costs microseconds over an in-memory list of ≤ ~15,000 lightweight
   tuples — not worth the alternative (returning a "here are the ids you need" intermediate result
   and calling back into the builder a second time), which would make the pure builder function
   stateful across two calls for no measured benefit.
9. **The licence-summary aggregate query was left as a live per-request query, not cached.** It is
   already a `GROUP BY source_id, licence_id` over a handful of distinct sources (one, in the real
   load) — not the measured bottleneck (`_source_licence_aggregate`-style aggregates were never the
   860-960 ms cost; the full `BuiltPlant`+join ORM hydration was) — and caching it would add a
   second cache-key coordination point for a query that is already fast and must stay exactly
   correct under the request's own filters.
10. **`PlantIndexRow` stayed a `@dataclass(frozen=True, slots=True)`, not a plain tuple or
    `NamedTuple`.** Profiled against the real load: building 14,659 plain tuples instead of
    `PlantIndexRow` instances measured ~24 ms vs ~27 ms — not a meaningful difference — so the
    self-documenting, attribute-accessed dataclass was kept rather than trading readability for an
    unmeasurable gain.

### Verbatim gates (this task, 2026-09-15)

```
$ .venv/bin/ruff check services api && .venv/bin/ruff format --check services api
All checks passed!
118 files already formatted

$ .venv/bin/mypy services
Success: no issues found in 77 source files

$ .venv/bin/lint-imports --config infra/importlinter.ini
Contracts: 2 kept, 0 broken.

$ .venv/bin/python -m openapi_spec_validator api/openapi.yaml && .venv/bin/python api/check_story_coverage.py --quiet
api/openapi.yaml: OK
RESULT: PASS — 44/44 PRD stories covered by 134 operations; all $refs resolve

$ .venv/bin/python -m pytest services/api -q
....................................................................... [100%]
```

Operation count rose from 131 to 134 (exactly 3: the geo op, the ui-events POST, the admin
summary), as required. Full-repo `pytest tests pipeline web services` also run: one pre-existing,
unrelated failure (`web/test_e2e.py::test_smoke_map_list_detail_with_attribution`, a Playwright/
chromium browser assertion — `docs/00-PLAN.md`'s "Sprint 3 closed" row already records this smoke
path as "not verified ... no browser run this session"); everything else, including every other
`services/`, `tests/` and `pipeline/` test, passed.

### Verbatim gates — pan/zoom performance follow-up (2026-09-15)

```
$ .venv/bin/ruff check services api && .venv/bin/ruff format --check services api
All checks passed!
115 files already formatted

$ .venv/bin/mypy services
Success: no issues found in 77 source files

$ .venv/bin/lint-imports --config infra/importlinter.ini
Contracts: 2 kept, 0 broken.

$ .venv/bin/python -m openapi_spec_validator api/openapi.yaml && .venv/bin/python api/check_story_coverage.py --quiet
api/openapi.yaml: OK
RESULT: PASS — 44/44 PRD stories covered by 134 operations; all $refs resolve
(unchanged by this follow-up — no spec edits were needed)

$ .venv/bin/python -m pytest services/api -q
.........................................................  (57 passed)
```

The pre-existing 9 `test_context_routes.py` tests pass unmodified against the refactored
implementation; 2 new tests were added for the cache specifically (see "Pan/zoom performance"
above). `services/api/test_ui_events.py` is untouched by this follow-up and still passes.

## Ownership tree: recursive scope, dated edges (2026-09-20)

`services/api/orgtree.py`. The walk down `organization.parent_org_id` was one level
(`_subsidiary_ids` in `services/api/assets.py`) and the walk up was one field (`parent`); both are
now recursive, and every ownership claim the API emits carries the provenance of the edge.

**The parameter.** `scope=self|children|all` on `GET /v1/organizations/{id}/assets`,
`/nearby-proposals`, `/proposals` and `GET /v1/assets/geo?organization=`.
`include_subsidiaries` **keeps the meaning it was published with** — `true` is one level, exactly
`scope=children` — and is marked `deprecated` in the spec rather than quietly re-pointed at the
recursive walk, because that would change what an existing caller's numbers mean with nothing in
the response saying so. A test pins that the legacy spelling still fails to reach a grandchild.
Sending both parameters is a 400, not a precedence rule nobody would remember.
`/proposals` is new to scoping altogether: it filtered `sponsor_org_id` single-org, so a holding
company's proposals page was an empty page that was technically correct and useless.

**Safety, and why it is not theoretical.** Every walk carries a visited set. Two loaders write
`parent_org_id` (`global.gleif.lei` and `curated.organization_parents`) and neither sees the
other's rows, so a two-node cycle is a plausible data state, not a hypothetical; without the
visited set it is an unreturning request. Tested against a two-node cycle, a self-parent row and a
rejoining diamond. `MAX_DEPTH = 10` levels and `MAX_SCOPE_ORGS = 500` organisations bound the
walk, and **both are reported, never silent**: `scope.depth_capped`, `.truncated` and
`.cycle_detected` ride on every scoped response and the page prints them in words. The depth cap
is set at ten because the deepest real chain in the data is three levels — three times the
observed depth, while still bounding a request at ten queries.

**Cost, measured.** The descent is breadth-first *by level*: one
`WHERE parent_org_id IN (level ids)` per level, so the **query count scales with depth, not with
node count**. Counted with a `before_cursor_execute` hook on the 2026-09-20 load (7,412
organisations, 298 parent links): `scope=self` 0 queries, `children` 1, `all` on
`tallgrass-energy` 2, a synthetic 500-wide fan-out 1, a synthetic 12-deep chain 11. The consuming
query is unchanged — one `IN` over the id list — which is what `MAX_SCOPE_ORGS` protects.

A **recursive CTE was benchmarked and rejected.** Median of 25 runs on SQLite,
`WITH RECURSIVE … UNION` (the dedup form, so it terminates on a cycle), BFS vs CTE:
`tallgrass-energy` 2.4 vs 6.4 ms; THE SOUTHERN COMPANY (15 organisations, 3 levels) 3.6 vs 6.0;
RWE (19) 2.4 vs 6.3; synthetic 500-wide plus 12-deep 2.4 vs 10.3. Slower at every size in the
data, and it can express neither the depth cap nor which bound stopped the walk. **Re-measure on
Postgres at the first deploy** — a round trip costs more there than SQLite's in-process call, so
the crossover is closer.

**Dated edges.** `GET /v1/organizations/{id}` gains `parent_edge` (the direct link with
`source_id`, `as_of`, `share_pct`), `ancestors` (root-first, for breadcrumbs) and
`descendant_count`. The nulls are **kept, not omitted**: a rendered ownership claim with no date
is a statement about today made from a file that states some other year, so the null travels to
the renderer and the renderer says so. 289 of the 298 loaded links are dated (GLEIF, from the
relationship period start); 9 are not (the curated file cites when a page was read).

**Migration 0017** adds `organization.parent_share_pct` (`Numeric(6,3)`, nullable, mirroring
`asset_owner.share_pct`). It is **NULL on every row and nothing infers a value** — neither loaded
source states a percentage. It exists so the percentage-stake chains GEM's ownership dataset
models do not require the edge to be rebuilt if that licence clears. The expensive half of that
future change, a stake belonging to a *set* of parents, is deliberately not taken: no loaded
source has produced a second parent for any organisation.

**`totals.by_organization`** (the portfolio breakdown, largest holding first, cap 100) rides on the
existing totals query — the same single query already read one row per (edge, asset); only the
group-by in Python is new. It is what makes a fund-level page renderable: at a scope spanning
dozens of portfolio companies, "1,400 assets" is not a page and "Tallgrass Energy 10, Rockies
Express 49, …" is.

`services/api/visibility.py` has zero changes; the licence gate is untouched on every path here.
