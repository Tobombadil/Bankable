# Trends: the daily metrics snapshot

Owner, 2026-10-23: "If we can track any of these numbers over time as well then that would be compelling.
People always wondering if the boom is growing shrinking or still strong. Any way we can track activity
over time would be good." A day not recorded is lost, so this lane only records: one document per UTC day
of the front page's numbers, as the public tier serves them. The trends API, the charts and any backfill
from official archives come later. Built by lane metrics-recorder, 2026-10-23; schema 2 from that day's
review fix round (state breakdowns, served-value count, distinct subjects, backfill rule, §2 and §6).

Code: `services/trends/snapshot.py` (what is counted), `services/trends/store.py` (where it is kept and how
it is read back), `infra/scheduler/jobs.py::trends_tick_job` and `tick_trends` in `infra/scheduler/app.py`
(when).

## 1. The convention

The document for UTC day D (`as_of`) holds the store as an anonymous visitor was served it at
`generated_at`, 05:58 UTC on D, after the day's loads, and the change events of D-1, the last complete UTC
day (`metrics.activity.window`). `compute_snapshot` takes only the moment it measures at and derives both
from it, so no document carries one day's state under another day's name. That moment must be
timezone-aware: `compute_snapshot` and `activity_window` refuse a naive one (`ValueError`), since its UTC
day depends on the zone it was meant in and reading it as the host's local time would file a day under
the wrong date. A day is never invented: a day the scheduler did not run has no file, and the reader skips
it.

## 2. Where it is kept

| Copy | Path | Written |
|---|---|---|
| Local, the record | `<data root>/normalized/trends/daily/YYYY-MM-DD.json` (`INFRAQUE_DATA_DIR`; the worker's `connector_data` volume) | temp file in the same directory, fsync, rename, fsync of the directory (the rename survives a power cut): a second write of a day replaces it, a reader never sees half of one |
| Private bucket, the copy | `s3://$R2_BUCKET/trends/daily/YYYY-MM-DD.json` | when `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and `R2_ENDPOINT` or `R2_ACCOUNT_ID` are set (the names the flood and parcels pulls read) |

A write killed between its temporary file (`.<name>.<32 hex>.tmp`) and the rename leaves that file behind;
every run removes such files once they are a day old (`TrendStore.sweep_temporaries`, called by `write` and
by `sync_uploads`, so a `fill` that writes nothing sweeps too). A younger one may be a write in flight and is
left alone; a filesystem that refuses a directory fsync (`EINVAL`, `ENOTSUP`) still gets the write.

The bucket is never the public tiles bucket: a `R2_BUCKET` equal to `R2_TILES_BUCKET` is refused, and so is
any `R2_BUCKET` whose name contains `tiles` in any case, because the public bucket is
`<prefix>-tiles-production` (`infra/ci/beta.sh` `BUCKET_PREFIX`) and only the deploy's basemap job is given
`R2_TILES_BUCKET`, so the worker's environment may not carry it for the first check to compare against.
`normalized/trends/uploaded.json` records the sha256 of each uploaded document, so a failed upload (store
unreachable, credentials refused) is retried by the next run, a replaced document is uploaded again, and a
bucket configured later receives the whole series (at most 400 documents a run). An upload never fails the
job. The `connector_data` volume is not in the Postgres backup (`infra/scripts/backup.sh`), so until the
bucket is configured on a deployment a lost volume loses the series.

Each document is JSON with sorted keys, one per line, so two days diff line by line: `schema_version`,
`kind`, `as_of`, `generated_at`, `convention`, `visibility`, `queries`, `bucket_filters`, `activity_rule`,
`metrics`, `sources`, `attribution_line`. `TrendStore.days()`, `.load(day)` and `.series(start, end)` read
it back.

| `schema_version` | Written | What changed |
|---|---|---|
| 1 | by the recorder's first version (2026-10-23), wherever it ran before this round | |
| 2 | from the review fix round (2026-10-23) on | `by_state` on every proposal block and on the opportunity blocks; `by_state_kind` on both active blocks; `bucket_filters.by_state_kind` is a list, one filter per level; `visibility.served_not_stored`; the activity's distinct-subject counts and `by_run_trigger`; activity rule `genuine-change-v2` (§6) |

The reader loads every version ever written (`snapshot.READABLE_SCHEMA_VERSIONS`, a recorded version-1
document is the test fixture `services/trends/fixtures/daily-v1-2026-10-23.json`) and refuses a newer one
rather than misread it. A key version 1 did not record is absent from those documents, and absent means not
recorded, never zero: `figure(document, path, default)` answers `default` for it.

## 3. When

`tick_trends` fires every hour at minute 58, a minute no other tick uses. The 05:58 run (`daily`) measures
and writes, replacing the day's document if one exists. Every other run (`fill`) writes today's document
only if it is missing and 05:58 has passed: that is the first run after a deploy that missed 05:58, or after
a failed daily run. The scheduler has no start-up hook and Procrastinate defers a missed periodic job only
within ten minutes (`procrastinate.periodic.MAX_DELAY`), so without the fill a deploy at 06:00 would lose the
day. A fill before 05:58 writes nothing, so the daily run is not preceded by a provisional document every
night.

Why 05:58: after the daily fetch bucket (03:07) and its load, resolve, enrich and match chain, the weekly
bucket (Monday 04:13), the daily `tick_resolve` (04:37) and the visibility audit (04:52). The monthly
bucket's EIA-860M fetch (the 1st, 05:21) loads in under a minute (`docs/64` §4: a load 23.6 s), so it is in
that day's figures. Queue `audit`, `queueing_lock` `trends_tick`, no store-wide lock (it only reads; on
Postgres every statement reads one REPEATABLE READ snapshot), 15-minute timeout, `retry=0`: the next hour is
the retry.

## 4. What is counted

Every figure is computed at the public tier, through the predicate the public routes apply
(`services/api/visibility.py`), and is the `include=count` total of the public list query the document
names in `queries`, filtered by the parameter `bucket_filters` names for its breakdown, or by one parameter
per level for a two-level breakdown (`by_state_kind: ["jurisdiction", "kind"]`;
`services/trends/test_snapshot.py` checks each one through the real app). Two exceptions, stated in the
document: the `_unstated` bucket (a null technology, operator or state), which no filter asks for; and a
record whose counted value a withheld source supplied, which the list filters by its stored value and this
counts as served (`GatedRecord`), as the map's totals and the company pipeline do.
`visibility.served_not_stored` counts those records, per subject, in all and by field (`kind`,
`technology`, `lifecycle_state`, `capacity_mw`, `storage_mwh`, `jurisdiction`, `iso`; for a notice `kind`,
`status`, `jurisdiction` and `relevance`, the energy verdict): a figure filtered on those fields can differ
from its list query's total by at most that many, and the parity test holds every figure to that bound. A record
counts there only when the served value differs from the stored one; one is read through its served view
only when its `field_provenance` names a withheld source, as on the map and the company pipeline. The
normal store has none (`0`).

| Key | What | List query |
|---|---|---|
| `proposals.active` | announced through under construction, still listed by a register the public tier reads (the map's and list's default view, `web/unlisted.py`); by kind, technology, lifecycle state, country, US state, grid operator, and US state then kind | `lifecycle_state=<active>&listed=true` |
| `proposals.active_not_listed` | the same states, every register has dropped them (hidden by the map); by kind, country, US state, grid operator, US state then kind | `...&listed=false` |
| `proposals.withdrawn`, `.built`, `.unknown` | withdrawn or cancelled; built; unknown (their views hide nothing); each by US state among its breakdowns | `lifecycle_state=withdrawn,cancelled` etc. |
| `proposals.visible` | every proposal served; the five blocks above partition it | `/v1/proposals` |
| `opportunities.open`, `.announced` | `all` and `energy` (`relevance=energy`, the RFPs page's default and the homepage figure), by kind, country and US state | `status=open[&relevance=energy]` |
| `activity` | change events of D-1 counted under the rule below: events and distinct subjects, each in all and by event type, also per subject type; `by_run_trigger`; `visible_events` and `left_out` by reason | `/v1/events?observed_at[from]=..&observed_at[to]=..` |

Any US state's landing figures rebuild from one document. For `/texas` (`web/headline.py`), with `S` =
`US-TX`: active projects = `proposals.active.by_state.S.records` (the map's notice hides the records no
register lists); planned data centres and other large loads = `proposals.active.by_state_kind.S.load` plus
`proposals.active_not_listed.by_state_kind.S.load` (that figure's query has no `listed` filter); RFPs and
funding calls open now = `opportunities.open.energy.by_state.S`. A test asks the API each figure as the page
does and compares. The state's lifecycle notice (active, withdrawn, built, unknown) is the four blocks'
`by_state.S`.

Activity has two counts side by side: `events` and `subjects`, the distinct projects or notices those
events changed (ten changes to one project are ten events and one subject), in all, by event type
(`by_event_type`, `subjects_by_event_type`) and per subject type. The document holds the counts only, never
a subject's id.

Each proposal bucket is a tally: `records`; `capacity_mw` summed over generation, storage, nuclear and other
(`services/api/org_pipeline.py::CAPACITY_NOT_SUMMED_KINDS`); `demand_mw` for loads, apart, because a data
centre's MW is demand; `not_summed_records` for lines, pipelines, LNG, CCS and hydrogen, whose MW is never
summed; `mw_missing` for a summed kind that states no MW; `storage_mwh` where stated. `records` is always
`capacity_records + demand_records + mw_missing + not_summed_records`, and a sum over nothing stated is
`null`, never `0`. Country is the jurisdiction's country part (`US`, `GB`, else `other`); a US state is the
jurisdiction (`US-TX`, the `/texas` landing's filter); a grid operator is `canonical_iso` (`ERCO` reads
`ERCOT`, as `iso=` matches).

## 5. Privacy and licence

Nothing is read but the public record: proposals, opportunities, their source links, events, sources, runs,
licences, locations and organisations (a test pins the tables the computation touches, and that it writes
none). No private project, data room, account, alert or person, and the document holds counts, never a
record's name or id. `services.trends` is in the import-linter contract that keeps public paths from
importing private projects (`infra/importlinter.ini`). A row from a restricted or off-surface source is not
counted, and a value such a source supplied is replaced by the value the public tier serves. `sources` lists
every source behind a counted figure with its licence, reuse class, credit line and latest retrieval;
`visibility` records the tier, the publishable reuse classes the predicate applied (read from the predicate
itself), how many registered sources were withheld, and how many visible records were counted by a served
value that is not the stored one (`served_not_stored`, §4). The beta runs `PLATFORM_POSTURE=noncommercial`, so
its documents count `noncommercial` sources' rows: any commercial use of the series must use documents
whose `visibility.platform_posture` is `commercial`, or recompute (`CLAUDE.md`: noncommercial rows never
flow to a commercial use).

## 6. The activity rule (`genuine-change-v2`)

A change event counts on the UTC day its source showed it (`observed_at`, the fetch that saw it) when the
public tier serves it (`event_visibility_filter`: a public subject, a publishable source and licence, never
`removed_from_source`) and it came from a source run that compared against a snapshot this store already
held. Left out, each counted under `left_out`:

- `first_load`: events of a source's first run here, its earliest `source_run` that promoted output (a seed
  or a first fetch, whose rows are all new to the store), whatever its trigger label. The runner already
  emits no event for a source's first run; this also covers a store seeded from a data root whose latest
  run is loaded first;
- `released_hold`: a run released from a data-quality hold (the row-drift and churn gates hold re-keys and
  scope changes, `pipeline/connectors/dq.py`);
- `no_source_run`: an event no source run wrote (matches, merges, edits are not changes at a source).

**Backfill-labelled runs count (decided 2026-10-23, review fix round).** `genuine-change-v1` (schema 1) also
left out every run whose `source_run.trigger` is `backfill`. That label is what an admin "Run now" records
when the operator picks it (`services/api/admin_sources.py::admin_run_source`, `manual` or `backfill`), and
the run is an ordinary fetch: `pipeline/connectors/runner.py::run` only records the trigger. The fetch window
is the same (`connector.watermark`, from the last promoted run), the diff is against the same latest stored
snapshot, and the same DQ gates hold a scope change. So its events are the changes the source showed since
the previous run, exactly as a scheduled run's would have been, and v1 lost them for good: the next run
diffs against the backfill's snapshot and never sees them again. v2 counts such a run unless it is the
source's first promoted run (then it is `first_load`, as any first run is), and `counted.by_run_trigger`
says how many counted events came from each label. Tested: `test_activity_counts_genuine_changes_and_says_what_it_left_out`
and `test_a_backfill_labelled_first_run_is_still_a_first_load`.

Assumption this depends on: the `backfill` trigger changes nothing a connector fetches. True on 2026-10-23:
no connector under `pipeline/connectors/` reads it (only `runner.py` and the CLI, to record it); the price
runner's `backfill` mode does fetch history, but prices write no change events (`pipeline/connectors/prices/`). If a connector ever reads the trigger to widen its fetch, its
backfill runs bring rows the store never held as `created`, and the rule needs a new name and a new reason.

Limits, stated so nobody reads more into the figure than it holds:

1. The date is the fetch's, not the register's own action date. A weekly-polled register shows its week on
   its poll day; a register that was down for days shows the gap's changes on the day it recovers.
2. Each day's activity is computed once, at 05:58 the next day. An event written later for that day (a load
   that ran late, a replayed failed load, `services/ingest/loader.py::runs_to_load`) is in the log but not in
   the recorded figure. The log is append-only and the rule reads stored columns only, so a later lane can
   recompute past days, with visibility judged at that time.
3. `first_load` leaves out the whole first run, even a seed run whose diff held genuine changes. A connector
   change that widens a source's scope by less than the 30 % row-drift hold is not detected: its new rows
   count as `created`.
4. `released_hold` leaves out everything in a released run, genuine changes too; the count says how many.
5. Visibility is judged when the document is computed: a record taken down later stays in the day it was
   served on, as the page showed it that day.
6. An admin "Run now" labelled `trigger=backfill` is an ordinary fetch. Schema-1 documents
   (`genuine-change-v1`) dropped its diffs under `left_out.backfill`; from schema 2 they count (above). A
   series that spans both versions changes rule where `activity.rule` changes, and a schema-1 day with a
   non-zero `left_out.backfill` undercounts by that many.
7. `subjects` counts distinct subjects within one day. Summing it over days counts a project that changed
   on two days twice; a distinct count over a week needs the event log, not the documents.

## 7. Measured (2026-10-23, SQLite, this checkout)

Schema 2, measured after the review fix round (the state-by-kind breakdowns, the served-value comparison and
the distinct subjects are in these figures):

| Store | Rows | Compute, first / median of 5 | Document |
|---|---|---|---|
| `web/.data/dev.db` (copy) | 775 proposals, no opportunities or events | 0.133 s / 0.029 s; the whole job 1.29 s with imports | 63,252 bytes |
| `web/.data/e2e-test.db` (copy) | 9,563 proposals | 0.404 s / 0.304 s | 94,820 bytes |
| the same plus 1,500 opportunities, 5,000 events in the window, 300 proposals with a value from a withheld register (synthetic) | | 0.844 s / 0.733 s | 96,715 bytes |

Schema 1 on the same copies: 0.022 s and 42,201 bytes; 0.287 s and 58,657 bytes (medians). The
`by_state_kind` breakdowns are most of the growth. About 95 KB a day is about 35 MB a year, locally and in
the bucket. Neither real store held opportunities or events; the synthetic one exercises those paths (its
generator is not in the repository: 1,500 notices over five jurisdictions, 5,000 events from one scheduled
run on random visible proposals, 300 capacities from an `ingest_only` register). Not measured on Postgres.
`infra/scheduler/app.py`'s timeout comment cites the two medians on the 9,563-proposal store as written
here (0.304 s and 0.733 s).

## 8. Not yet

The trends API and charts; backfills from official archives (EIA-860M monthly files, queue snapshots) for
the days before the clock started; recomputing past days' activity from the event log (a schema-1 day's
dropped backfill diffs included); a row for `trends_tick` in `docs/60` §6.3's job table and `docs/63`.
`jobs.trends_tick_job` still reads a naive `now` (its test seam; production passes none) as the host's
local time before `compute_snapshot` sees it; `infra/scheduler/jobs.py` was outside this round's paths.
