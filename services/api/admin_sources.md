# Admin source-health, licence-gate, cost and audit operations (Sprint 3 item 3)

Scope: `services/api/admin_sources.py`, its tests (`tests/test_api_admin_sources.py`), and this
note. All twelve `/admin/v1` operations named in the task brief, implemented exactly to the
request/response schemas already committed in `api/openapi.yaml` (all `x-status: planned` there;
the coordinator flips them to `live` once this module is mounted).

## What

| Operation | Method + path | Notes |
|---|---|---|
| `adminListSources` | `GET /admin/v1/sources` | filters: `category`, `health`, `publish_state`, `reuse_class`, `implemented`; cursor pagination on `Source.id` |
| `adminCreateSource` | `POST /admin/v1/sources` | US-303 curated issuer; `implemented=false`, `connector=null`, `publish_state=ingest_only` |
| `adminGetSource` | `GET /admin/v1/sources/{id}` | includes the latest `SourceRun` as `last_run` |
| `adminUpdateSource` | `PATCH /admin/v1/sources/{id}` | runtime fields only; at least one besides `reason` |
| `adminRunSource` | `POST /admin/v1/sources/{id}/run` | 202; never runs a connector inline (see Design: "run now" below) |
| `adminSetSourcePublishState` | `PUT /admin/v1/sources/{id}/publish-state` | `422 gate_unmet` naming the unmet condition(s) |
| `adminListSourceRuns` | `GET /admin/v1/source-runs` | filters: `source_id`, `status`, `started_at[from/to]` |
| `adminGetSourceRun` | `GET /admin/v1/source-runs/{run_id}` | `run_` id decoded via Crockford, like `mat_` ids |
| `adminGetSnapshot` | `GET /admin/v1/snapshots/{snapshot_id}` | `snap_` id, same decode scheme |
| `adminSetLicenceGate` | `PUT /admin/v1/licences/{id}/gate` | `legal` role only; reclassification creates a new licence row |
| `adminGetCosts` | `GET /admin/v1/costs` | aggregates `model_call` + `source_run` in Python, day-bucketed |
| `adminListAudit` | `GET /admin/v1/audit` | `event` rows, `actor_type = user`, cursor pagination on `seq` |

## Design: "run now" (US-904 AC2)

A `SourceRunner` Protocol (`enqueue(db, source, *, trigger, requested_by) -> SourceRun`) with:

- `QueuedSourceRunner` (production, returned by `get_source_runner()`): creates the `source_run`
  row (`trigger="manual"`, `status="running"`, `started_at=now()`) so the panel shows it
  immediately, then lazily imports `infra.scheduler.app.run_connector` and defers it with
  `infra.scheduler.cadence.queueing_lock_for(source_id)` on the queue `queue_for_source` picks.
  Every failure to reach the queue (no Postgres, a `procrastinate` import error, `AlreadyEnqueued`)
  is wrapped into a fixed `503 unavailable` `ProblemError` — no driver text or stack trace reaches
  the response. Because `services/api/deps.py`'s `get_db` rolls back the whole request on any
  raised exception, the `source_run` row created just before the failing `defer()` call never
  persists when the queue itself is unreachable — there is no orphan "running" row left behind.
- Tests override `get_source_runner` with a `_FakeSourceRunner` that only creates the row (no
  Procrastinate/Postgres involved) — the route is never exercised against a real queue in this
  sandbox, matching every other connector-adjacent test in this codebase.
- The worker (a later sprint's wave) is the thing that updates the row it finds when it actually
  runs the connector — this endpoint's job ends at "queued and visible", never at "ran".

## Decisions (numbered, with reasons — mirrored as inline comments in the module docstring)

1. **Every write requires a non-blank `reason`**, even where the spec's own request schema does not
   mark it `required` (`AdminSourceRunRequest`). The task brief's hard rule ("every write body
   carries `reason`; 400 if missing/blank") is stricter than the spec here; followed as the
   tightening it is, not a contradiction — every other admin write in this codebase (`docs/04` S-4,
   `services/api/pro.py`'s entitlement-grant endpoint) already assumes this.
2. **`Source.id`/`Licence.id` are text primary keys; `event.subject_id` is a `uuid` column** with no
   FK. Audit events on a source or licence use a deterministic `uuid5` derived from the text id
   (`_source_subject_uuid` / `_licence_subject_uuid`), so the same source/licence always audits to
   the same synthetic subject id across calls; the real identifier is always also present in
   `before`/`after` so nothing is lost. `SourceRun`/`Snapshot` have the same text-vs-uuid non-issue
   in reverse — they have no `public_id` column at all — so `run_`/`snap_` ids are synthesised from
   the real uuid with `services.ids.public_id` and decoded the same way
   `services/crm/router.py`'s `_find_match_by_public_id` decodes `mat_` ids (Crockford digits →
   `int` → `uuid.UUID(int=n)`).
3. **`api/openapi.yaml`'s `SubjectType` enum has no `licence` value.** `Licence` is a distinct
   entity from `Source` (one licence can cover many sources) and US-905's gate lives on the
   licence, not on any one source. Audit rows for `PUT .../licences/{id}/gate` are written with
   `subject_type = "licence"` — accurate — rather than misattributing the change to `subject_type =
   "source"`, which would be wrong the moment a licence covers more than one source. **This is a
   genuine, narrow spec gap** (`SubjectType` should grow a `licence` value); flagged for the
   architect rather than silently worked around.
4. **`AnyPublicIdValue`'s pattern (`^(prop|opp|org|mat|evt|doc)_...$`) does not cover `source`,
   `licence`, `account`, `user` or `api_key` subjects**, even though `SubjectType`'s enum lists all
   of those as valid `Event.subject_type` values. Consequently `GET /admin/v1/audit` rows whose
   `subject_type` is `source` or `licence` cannot validate `subject_id` against the `Event` schema
   at all — no rendering choice fixes this, since every candidate string fails the fixed prefix
   list. **Verbatim proof this is a spec issue, not an implementation bug**: `subject_type` values
   `account`/`user`/`api_key`/`source` are all valid per `SubjectType`'s own enum
   (`api/openapi.yaml` line 6063-6066 area), yet none of their natural id prefixes (`acc_`, `usr_`,
   `key_`, a dotted source id) matches `AnyPublicIdValue`. `tests/test_api_admin_sources.py` handles
   this by validating the full `EventListResponse` envelope + a fully spec-compliant row
   (`subject_type = "match"`, which *is* covered by the pattern) with `assert_valid`, and separately
   asserting the *content* (reason, before/after, filtering) of source/licence audit rows without
   full JSON-Schema validation on those specific rows.
5. **A licence reclassification (`reuse_class` changes) creates a new `Licence` row** (invariant L2,
   per the operation's own spec description: "A reclassification that changes `reuse_class` creates
   a new licence row"), rather than mutating `reuse_class` in place — so that historical `snapshot`
   / `proposal_source` / `opportunity_source` rows keep the licence that applied at fetch time
   (they store `licence_id` at write time and are never touched). Every `Source` currently pointing
   at the old licence id is repointed to the new one, so future fetches and gate checks use the
   fresh classification. The new id is `{old_id}-{reuse_class}`, de-duplicated with a numeric
   suffix on collision (tested: `test_next_licence_id_deduplicates_on_collision`).
6. **No idempotency-key replay is implemented** for the `Idempotency-Key` header on any operation
   here. Checked: `services/api/pro.py` accepts the same header on several `api/openapi.yaml`
   operations without implementing replay either (`grep -n "Idempotency-Key\|idempotency"
   services/api/pro.py services/api/app.py` returns nothing) — this is an existing, pre-sprint gap
   across the codebase, not one introduced by this module.
7. **No `relag` job is enqueued** when `PATCH .../sources/{id}` changes `lag_days`/`lag_overrides`
   (docs/21 §5.4 describes one recomputing `public_at` on historical rows asynchronously). No such
   job exists anywhere in this repo yet — `infra/scheduler` only defines `run_connector` and the
   periodic bucket-tick tasks. The runtime columns are updated immediately; historical `public_at`
   recomputation is deferred to whichever sprint adds the job, and building it is out of this
   module's read/write scope (it would touch `infra/scheduler`, which the task brief restricts to
   "only for the 'run now' design").
8. **`AdminSource.host`/`schedule_cron` are required, non-nullable strings** in the spec, but
   `Source.host`/`schedule_cron` are nullable manifest/runtime columns the loader (out of scope
   here) may not yet have populated for every row (e.g. a source seeded directly by a test or an
   older manifest load). Serialization falls back to `""` rather than crashing or inventing a
   cadence-derived cron / URL-derived host for an existing row. The one place this module *does*
   compute a real host is source **creation** (`admin_create_source`), because the spec's own
   description explicitly needs it there: "The issuer URL host is added to the egress allowlist for
   the `plain` pool only."
9. **`GET /admin/v1/costs`**: `CostRow.day` is required and non-nullable in the schema, so even a
   `group_by` that omits `day` still returns one row per day (day cannot be suppressed) — only
   `source_id`, `purpose` and `model_alias` are actually elided by `group_by`. Aggregation is done
   in Python over the matching `model_call` and `source_run` rows (not SQL `date_trunc`/`date()`)
   to stay portable across the Postgres target and this sprint's SQLite test database — the same
   trade-off `services/api/common.py`'s `ensure_aware` docstring makes for the identical reason.
   `flagged_sources` reads the existing rolling `source.cost_per_changed_record_30d` column against
   the threshold (default USD 0.50, `[A-9]`) rather than recomputing it from the windowed rows,
   since docs/21 §4.1 documents it as a separately-maintained 30-day metric; it is optionally
   narrowed by the `source_id` filter.
10. **`Problem.gate`/`Problem.missing` (the `gate_unmet` example's extra fields) are not emitted.**
    `ProblemError` (`services/api/errors.py`, read-only for this module) has no keyword for either.
    Both fields are optional in the `Problem` schema, so omitting them is schema-valid; the unmet
    condition(s) are named in `detail` instead (e.g. `"...licence 'x' is missing reuse_class (is
    'restricted', needs open or attribution)."`), which satisfies US-905 AC1's "refused ... naming
    the unmet condition" and the task brief's per-condition test requirement (three separate tests,
    one per condition).
11. **Curated issuers (`POST /admin/v1/sources`) get `tier=3`, `format=null`, `schedule_cron=null`**
    (decision 8 explains why null/empty is acceptable) **and `egress="plain"`** — not a guess: the
    spec's own description says the issuer host is added to "the egress allowlist for the `plain`
    pool only", which only makes sense if the row's own egress class is `plain`.

## Deferred (not implemented this sprint, and why)

- **Idempotency-key replay** (decision 6) — a pre-existing, codebase-wide gap; a follow-up task,
  not specific to this module.
- **`relag` job** (decision 7) — no such job exists in `infra/scheduler` yet; adding one is outside
  this module's file scope.
- **Pre-signed snapshot download URLs** — `Snapshot.download_url` is always `null`; object-storage
  pre-signing is not wired this sprint (no bucket/adapter exists to sign against yet).
- **`SubjectType` gap for `licence`** (decision 3) and **`AnyPublicIdValue` gap for
  `source`/`licence`/`account`/`user`/`api_key`** (decision 4) — spec changes, not this module's to
  make; both are precisely named above for whoever owns `api/openapi.yaml` next.

## Verbatim check output (tails)

```
$ .venv/bin/python -m pytest tests/test_api_admin_sources.py
...............................................................          [100%]
63 passed, 16 warnings in 6.85s
```

```
$ .venv/bin/ruff check services/api/admin_sources.py tests/test_api_admin_sources.py
All checks passed!
$ .venv/bin/ruff format --check services/api/admin_sources.py tests/test_api_admin_sources.py
2 files already formatted
```

```
$ .venv/bin/mypy --cache-dir /tmp/mypy-adm-src services/api/admin_sources.py
Success: no issues found in 1 source file
```

```
$ .venv/bin/python -m coverage run --branch -m pytest tests/test_api_admin_sources.py
63 passed, 16 warnings in 10.72s
$ .venv/bin/python -m coverage report --include="services/api/admin_sources.py"
Name                            Stmts   Miss Branch BrPart  Cover
-----------------------------------------------------------------
services/api/admin_sources.py     482     20    180      9    96%
-----------------------------------------------------------------
TOTAL                             482     20    180      9    96%
```

The 20 remaining missed lines are defensive branches that need either a real Postgres/Procrastinate
connection to exercise the success path of `QueuedSourceRunner.enqueue` past its `except` (the
`except` clause itself is excluded from the coverage requirement via its own `# pragma: no cover`,
and its *failure* path — the realistic one in this sandbox — is exercised directly by
`test_queued_source_runner_wraps_failures_as_unavailable`), or malformed-input parsing corners
(`_parse_dt`/`_parse_date`/`_decode_crockford_uuid` failure branches) that are not reachable through
this module's own validated call sites without contrived, low-value inputs.

```
$ .venv/bin/python -m pytest tests/test_api_contract.py services/api
.................................................                        [100%]
49 passed, 27 warnings in 4.08s
```

No regressions in either. (`tests/test_api_admin_records.py` and `tests/test_web_default_view.py`
fail/error independently of this change — confirmed by running `tests/test_api_admin_records.py`
alone, which fails identically on `AdminProposalDetailResponse: None is not of type 'string' at
['data', 'sources', 0, 'link_event_id']`, and `tests/test_web_default_view.py`, which errors on a
missing `data/normalized/*` fixture in this sandbox. Both are pre-existing, in another agent's file
area (`admin_records.py`) or unrelated to any file this task touches; not fixed here.)

## Not done / out of scope

- `services/api/app.py` is not touched — mounting `admin_sources.router` and flipping the twelve
  `x-status` values to `live` in `api/openapi.yaml` is the coordinator's job per the task brief.
  `tests/test_api_admin_sources.py` mounts the router onto the shared app itself so it can run
  standalone in the meantime.
- Everything named under "Deferred" above.
