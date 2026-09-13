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
  lag.py             public_at = published_at + lag(kind) (14d supply / 7d opportunities)
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
4. **Public lag default: 14 days supply / 7 days opportunities**, per this task's brief and
   `docs/04-standards.md` §10 pick 1. `docs/20-architecture.md` A-8 (7 days flat) and
   `docs/21-data-model.md` §9 D-1 (14 days flat) disagree with each other and with this; that
   three-way conflict already exists in `docs/21` §10 (correction C-4) and is not resolved here —
   this sprint follows the more specific, later standards document rather than picking a third
   answer.
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
