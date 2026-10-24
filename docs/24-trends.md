# Trends: the daily record, the official archives and `GET /v1/trends`

Owner, 2026-10-23: "If we can track any of these numbers over time as well then that would be compelling.
People always wondering if the boom is growing shrinking or still strong. Any way we can track activity
over time would be good." A day not recorded is lost, so the first lane only records: one document per UTC
day of the front page's numbers, as the public tier serves them (§1 to §8; lane metrics-recorder,
2026-10-23; schema 2 from that day's review fix round). The history before the clock started comes from the
official monthly archives, EIA-860M since July 2015 and ERCOT's GIS reports since December 2018 (§9), and
`GET /v1/trends` serves both as named series (§10; lane trends-history, 2026-10-23).
`GET /v1/stats/planned-vs-operating` sets EIA-860M's newest month of planned capacity, by stage, against its
operating capacity, and data centres by power (§11; lane planned-operating, 2026-10-24).

Code: `services/trends/snapshot.py` (what is counted), `services/trends/store.py` (where it is kept and how
it is read back), `infra/scheduler/jobs.py::trends_tick_job` and `tick_trends` in `infra/scheduler/app.py`
(when); `pipeline/connectors/history/` (the archives), `infra/scheduler/history.py` (their ticks),
`services/trends/series.py` and `services/api/trends.py` (the series and the route).

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

The charts (the front page reads `GET /v1/trends`, §10, once a page lane draws them); recomputing past
days' activity from the event log (a schema-1 day's dropped backfill diffs included); rows for
`trends_tick` and `history_fetch` in `docs/60` §6.3's job table and `docs/63`. `jobs.trends_tick_job`
still reads a naive `now` (its test seam; production passes none) as the host's local time before
`compute_snapshot` sees it; `infra/scheduler/jobs.py` was outside both lanes' paths.

For the archives (§9, §10), outside lane trends-history's paths: the admin sources view lists a
history source as having no implemented connector (`Source.implemented` is the record registry's
reading, which finds no `pipeline/connectors/<id>/connector.py`), so its "Run now" answers 409 and
the weekly tick or `python -m pipeline.connectors.history run` is the only route
(`services/api/admin_sources.py` would defer `history_fetch` for a `runner: history` row); the
monthly documents live on the `connector_data` volume only, not in the private bucket (a lost volume
re-plans every month: ERCOT reads the queue connector's stored reports, EIA reads the mirror again);
`services.api.trends` is not in the import-linter contract's list of public read paths
(`infra/importlinter.ini`; `services.trends`, which it imports, is).

Found in the trends-history fix round (2026-10-24), outside the lane's paths:

- **The register's ERCOT technology.** `pipeline/vendor/gridstatus/queues.py` builds the same
  `Fuel - Technology` label for the register's ERCOT rows and `pipeline.normalize.classify_tech`
  reads it by prime mover first, so the map most likely shows ERCOT's coal and biomass steam turbines
  as `gas_steam` and its oil turbines and engines as `gas_ct` and `gas_ice`. The history connector
  now reads the fuel first (§9.2); until the register does too, an ERCOT coal unit is `coal` in
  `ercot_queue_mw` and `gas_steam` on its record page.
- **Retention of history snapshots.** DA-10 compaction (`services/retention/snapshots.py`) keeps a
  history snapshot only because its name gives it a file extension of its own (§9.3); the history
  run records name no snapshot for its in-use rule. The explicit fix is for that module to read
  every monthly document's `raw.snapshot` (and the shared register snapshots they name) as in use.
  A test here pins the naming (`test_every_months_snapshot_carries_an_extension_of_its_own`); the
  retention module may not be imported from the pipeline or `services.trends` (it reads private
  projects, `infra/importlinter.ini`).

## 9. History from the official archives

The daily record starts on 2026-10-23. Two official archives reach back years, monthly, and are read into
one document per source and month (`pipeline/connectors/history/documents.py`):

    <data root>/normalized/trends/monthly/<source_id>/YYYY-MM.json

A document reduces one month's file to tallies (rows counted, MW summed as the source lists them) in
total and by each dimension the source states. It carries the provenance quartet (`source_id`,
`source_url`, `retrieved_at`, `licence_id`) and the credit, `retrieved_from` (the route and the file),
`raw` (the stored snapshot's key, sha256 and size, so every figure traces to its bytes), the parser
version, the layout facts the parser read, and `dq` (§9.4). Writes are atomic (temporary file, fsync,
rename, directory fsync), as the daily documents' are. Nothing here is a change event: a month of an
archive is context, and a source's first load is not news (docs/00-PLAN.md, 2026-10-23).

### 9.1 Sources

| `source_id` | What | Months | Routes |
|---|---|---|---|
| `us.eia.860m.history` | EIA-860M, the Preliminary Monthly Electric Generator Inventory: the Operating, Planned, Retired and Canceled or Postponed sheets | 2015-07 (the earliest EIA offers) to the latest release | EIA's current month (`origin`); every workbook `us.eia.860m` already stored (`shared`); a person's download (`inbox`); Catalyst Cooperative's Zenodo archive of EIA's files (`mirror`, backfill only) |
| `us.iso.ercot.gen_queue.history` | ERCOT's monthly Generation Interconnection Status report: the project-detail sheets | 2018-12 (the earliest on the MIS listing) to the latest report | the MIS listing and download (`origin`, the route `us.iso.ercot.gen_queue` reads); every report that connector already stored, by DocID (`shared`) |

Both are public: EIA's files are US federal works (17 U.S.C. §105; EIA asks for "Source: U.S. Energy
Information Administration (<date>)", the publication date, docs/13 §2.11, so the series' credit
carries the month its latest month was published, §10);
ERCOT's raw data "may be used, reproduced, and redistributed in compilations, charts, and analyses"
(Website User Agreement clause 5, docs/13 §1.1). Both rows are `reuse: open`, `publication: raw_ok`,
`runner: history` in `data/sources.yaml`, and the history runner refuses a row the terms gate (`base.gate`).

**EIA's archive years and robots.txt.** EIA keeps every month but the newest under
`/electricity/data/eia860m/archive/xls/`, and eia.gov's robots.txt says `Disallow: /*archive/` (read
2026-10-23). docs/13 §2.15 settled that this path is never crawled and that older years need "a
different, permitted route". Nothing here requests it: the connector drops `/archive/` links before any
request, as `us.eia.860m` does (Python 3.11's robots parser cannot read the wildcard), and the tests fail
on any request to it. Three routes reach the archive years instead:

1. **Shared snapshots.** Every month `us.eia.860m` fetched since it started (monthly, from EIA's own
   `xls/` path) is read from the snapshot store, without a request.
2. **The inbox.** A person may download months in a browser (robots.txt binds crawlers, not people) into
   `<data root>/inbox/us.eia.860m.history/`, as EIA names them (`july_generator2015.xlsx`) or as the
   mirror's yearly zips; each file is read once (by sha256) and stored as a snapshot.
3. **The mirror** (backfill only): Catalyst Cooperative's "PUDL Raw EIA Form 860M" record on Zenodo
   (DOI 10.5281/zenodo.22995197, version 50.0.0, created 2026-09-27), twelve yearly zips of EIA's monthly
   workbooks, 2015-07 to 2026-08, licence "U.S. Government Works". Zenodo's Terms of Use v1.3 (read
   2026-10-23): "Access to Zenodo's content is open to all, for non-military purposes only." and "Users of
   content ("Users") shall respect applicable license conditions."; its robots.txt allows
   `/records/<id>/files/...` and asks `Crawl-delay: 10`, which the session honours (0.1 rps). Each zip is
   checked against the record's md5. The published `source_url` stays EIA's own address for the month;
   `retrieved_from` names the record, DOI, file and member. This follows the precedent of docs/13 §2.16
   (a public-domain federal file read from a public mirror, the mirror's terms governing the fetch).
   **It ships switched off** (`mirror.enabled: false` in `data/sources.yaml`, fix round 2026-10-24):
   using it is a decision for the docs/00-PLAN.md decisions log, proposed by the lane and not yet
   recorded. Off, a backfill reads routes 1 and 2 and EIA's current month, and the archive years wait.
   `enabled: true` is the one switch: each run records what its backfill read (`backfill_key`, the
   record's DOI), so the next weekly `auto` run after the switch, or after the block names a newer
   record, is a backfill again (`runner.effective_mode`) and reads only the zips holding a month no
   document holds yet (about 1.16 GB for the whole archive at 0.1 rps, §9.6).

**A month missed on every route.** EIA's index links only the newest month. A month EIA publishes
while both this source's weekly tick and `us.eia.860m` are down for more than a month leaves the
index before either reads it, and from then on is only under `/archive/`. Monthly runs never read the
mirror, and the pinned record (version 50.0.0) stops at 2026-08. The recovery route is the inbox: a
person downloads the month in a browser (`<month>_generator<year>.xlsx` from EIA's page, or the
year's zip from a newer mirror record) into `<data root>/inbox/us.eia.860m.history/`, and the next
run reads it; or the mirror block is moved to a newer record that holds the month (above).

A month held from several routes keeps the most authoritative copy (`base.ROUTE_RANK`): EIA's own server
(`origin`, `shared`) over a person's download over the mirror. A mirror copy never replaces EIA's.

### 9.2 What a month holds, and like for like

**EIA-860M.** Each sheet is a block: `operating` (by technology, state, status: OP, SB, OS, OA),
`planned` (by technology, state, status: P, L, T, U, V, TS, OT, and expected year), `retired` (by
technology, state, retirement year), `canceled_postponed` (by technology, state), and
`planned_retirements` (Operating-sheet generators with a planned retirement year, by that year). The
`operating` and `planned` blocks are also tallied by state and status together and by technology and
status together (`by_state_status`, `by_technology_status`; parser 1.1.0, 2026-10-24): each key is the
pair joined by `|` (`US-TX|U`, `solar|TS`; a row with no status is `<key>|_unstated`, as the status
tally counts it), so a state's or a technology's planned capacity splits by stage as the national
figure does (§11.1). Each generator is in exactly one pair, so a paired tally summed onto either of
its keys is the single tally (the tests pin it on both fixtures, the measurement on all 135 months,
§9.6). A tally is generators counted and net summer MW summed; nameplate MW beside it where the workbook states it.
Technology is the register's classifier (`pipeline.normalize.classify_tech`) except four EIA labels it
misreads, read by the label (`EIA_TECHNOLOGY`: "Solar Thermal without Energy Storage" matches its storage
rule, "Coal Integrated Gasification Combined Cycle" its combined-cycle rule, and flywheels are storage);
each document records the label-to-bucket map it used. A row with a blank Technology is counted in
`unknown`, recorded as the label `(blank)` with its rows and MW per block, and warned when a block's
blank rows carry 100 MW or more (§9.4; 2026-09's Planned sheet: four Willow Rock air-storage units,
500 MW). A row EIA lists without a Plant ID but with a technology and a state or MW is a generator, not
a note: it has no key, so it stays out of the tallies and is counted under `excluded.no_plant_id`. Only
2026-08 has such rows, on its Retired sheet: 12 early nuclear units (Indian Point 1, Fort St. Vrain,
Hallam, Pathfinder, Elk River, Shoreham, ...), 11 counted there and Puerto Rico's Bonus under
`excluded.puerto_rico`.

What changed across the archive, found by reading all 135 months (2026-10-23), and how the basis holds it:

| Change | First month | Handling |
|---|---|---|
| Header on the second row, net summer MW only, no county or coordinates | 2015-07 | The header row is found, not assumed; every block sums net summer MW, the one measure in every month |
| Nameplate MW stated | 2016-04 | Kept beside net summer as `nameplate_mw`; absent before, never zero |
| The Retired sheet lists every retirement since 2002 ("Starting with March 2017 data", EIA's index page) | 2017-03 | `retired` basis `.../retired-partial` before, `.../retired-since-2002` from; no change computed across |
| Puerto Rico on separate `*_PR` sheets ("Starting with March 2018 data", same page) | 2018-03 | Never read; a Puerto Rico row on a main sheet is left out and counted under `excluded`, so every national figure is the 50 states and DC in every month |
| Header on the third row, after a blank row | 2020-11 | Found, as above |

**ERCOT GIS.** The `projects` block is the report's project-detail rows, which every report since 2018
says are "only those projects for which a Full Interconnection Study has been requested", by technology
(below), status (the register's milestone
rule, `pipeline/status_map.yaml` `ercot`: synchronised `built`, energised `under_construction`, IA signed
`contracted`, else `studied`; ERCOT's `1-1-1900` "date not available" counts as reached), study phase
(ERCOT's own label as a token), CDR zone and projected COD year. MW is as listed: a repowering is "a net
change ... and thus may have zero or negative values". Where ERCOT re-issued a month (`_revised`,
`_Correction`, `_CORRECTION2`, `_Corrected`: five months on the 2026-10-23 listing), the latest published
document is the month, and one published later replaces it. "Latest" compares `PublishDate` as an
instant: ERCOT writes Central time with its offset (`-05:00` in CDT, `-06:00` in CST), so two re-issues
of one day can sort wrongly as text.

*Technology* is read from ERCOT's fuel code first (`ercot_gis/connector.py` `FUEL_TECHNOLOGY`). The
register's reading, gridstatus's `Fuel - Technology` label through the classifier, tests the prime
mover before the fuel, so it read `COA ST` and `COAL ST` (coal steam turbines) and `BIO ST` as
`gas_steam`, `OIL GT` as `gas_ct` and `OIL IC` as `gas_ice`: no ERCOT month had a coal or biomass key,
and in 2020-03 the 553 MW `gas_steam` bucket held a 493 MW coal unit (fix round, 2026-10-24). Coal,
biomass, fuel oil, petcoke (petroleum, as EIA groups it), geothermal, hydrogen and nuclear name the
plant whatever its prime mover; concentrated solar power is `solar_thermal`; gas, solar, wind, water
and other keep the register's label reading. Three fuel codes the archive uses are not on ERCOT's own
list (the report's Acronyms sheet): `COAL` (2020-03), read as coal; `BAT` (2024-10, 2024-11, 2025-07),
every row a battery; and `MWH` (2025-09 to 2026-09), every row a project named "... Storage" (Montezuma,
Falls County, Kalina, Mistflower, Lynx), both read as storage, as are the unlisted technology codes
`BL`, `BO` and `SO` they come with. Each such month is warned (`code_vocabulary`). The test pins all 26
fuel and technology pairs the archive holds. Measured on the restated archive (2026-10-24): coal in 12
months (2019-01 to 2020-09), biomass in 21 (2022-05 to 2024-01), oil in 22; 2020-03's `gas_steam` is
60 MW and its `coal` 493 MW. Until the register reads the fuel first too (§8), the two disagree on
these units.

| Change | First month | Basis |
|---|---|---|
| One "Project Details" sheet, every size | 2018-12 | `ercot-gis/details-all-sizes/capacity-mw/fis-requested` |
| The note "Excludes projects that have a status of Inactive" | 2019-08 | `.../fis-requested/excl-inactive` |
| "Project Details - Large Gen" and "- Small Gen" | 2022-02 | `ercot-gis/details-large-gen/...`; small generators are their own block (`small_generators`), under their own inclusion rule (owners' authorisation or model-ready) |

So `ercot_queue_mw` runs from 2018-12 with two breaks, and no change is computed across either.

### 9.3 Runs: idempotent, resumable, a failed fetch keeps the last good data

`python -m pipeline.connectors.history run <source_id> --mode backfill|monthly|auto` (`runner.py`). A run
plans what it needs, then for each file: fetch (or read stored bytes), store the bytes as a snapshot,
summarise (in a forked child process for a file of 2 MB or more, so the parse's memory goes back to the
system), check (§9.4), write the month's document. Then the run record, last.

- **Idempotent.** The same bytes under the same parser for the same month (and, for ERCOT, the same
  DocID) are `unchanged` without a parse; a 304 to a conditional request reads the stored copy. Run
  twice, the second run writes nothing (ERCOT: one request, the listing).
- **Resumable.** Each month commits on its own; a killed run keeps every month it finished, and the next
  plans only what is still missing. `auto` runs `backfill` until a backfill has ended `ok` or
  `unchanged` with the same `backfill_key` (the mirror record it read, none while the mirror is off),
  then `monthly` (the mirror is read in `backfill` only).
- **A held reading never replaces a served month.** A reading its checks hold (§9.4) of a month that
  already has a served document is written beside it as `YYYY-MM.held.json`, and the served document
  stays; `release` makes the held reading the month (fix round, 2026-10-24: a held re-issue,
  correction or restatement used to be written over the good month, which then dropped out of the
  series, and the next run called the same bytes unchanged). The same file is then neither fetched
  nor parsed again (its sha256, parser and DocID are known); the run record lists every month that
  waits on an operator (`awaiting_release`), and `GET /v1/trends` lists them under `held_readings`. A
  later file of the month that passes replaces the month and discards the held reading.
- **A month comes from one file only.** A file whose bytes are already another month's served
  document is held (`duplicate_file`): ERCOT's names are typed by hand (`Jun2026`, `December_2018`,
  `_CORRECTION2`), and September's report re-posted under an August name would otherwise put
  September's figures on August, lose the real August and read Aug->Sep as no change.
- **A failed fetch keeps the last good data.** Nothing deletes a document; a month is replaced only by a
  successful parse of a copy at least as authoritative, a re-release or correction, or a restatement.
  A copy of the same file from an equally authoritative route that was retrieved earlier than the
  one held never replaces it, and is neither stored nor parsed (`runner.kept_copy`): without this,
  `us.eia.860m`'s stored copy of a month EIA later re-issued would put the old figures back every
  week, and the origin's fetch of the re-issue would undo it in the same run. A different file of the
  publisher's for the month (ERCOT's corrected re-issue, a new DocID) is the publisher's own choice
  and always replaces. An item that failed in three consecutive runs is skipped
  (`skipped_repeated_failure`) until `--retry-failed`, so a broken file is not downloaded every week;
  EIA's index page is never skipped, since every new month comes by it, so an outage there fails each
  run and alerts.
- **Conditional requests.** The origin's validators (`ETag`, `Last-Modified`) are recorded on every
  run that read the publisher's current file, written or unchanged, so the next request is
  conditional on the newest ones (a 304 reads the stored snapshot).
- **Restatement.** The parser version is the declared version plus a digest of the code that makes the
  documents (the connector, `base.py`, `documents.py`, `dq.py`, the classifiers): a code change, a
  changed check included, re-reads every stored month from its snapshot, without a request. A
  restatement keeps what the bytes alone do not say: how they first arrived and, for ERCOT, the
  listing's name, publication date and variant for the same DocID (documents an earlier runner wrote
  without them get them back). When a month's own snapshot is gone (a lost volume, a retention
  run), the restatement reads another stored copy with the same sha256 (the register source's,
  `shared_source_id`), and ERCOT downloads the same DocID again; only an EIA archive month with no
  copy left stays under its old parser (recovery: the inbox, above). Two months read by different
  parsers are never compared: `mom` and `yoy` need one parser on both points, and the line lists
  the break with both versions, so a month left behind, or a restatement in progress, keeps its
  value and makes no false change. EIA's parser 1.1.0 (2026-10-24, the paired tallies of §9.2) is
  such a change: the next run after it lands restates every stored month, July 2015 on, from the
  snapshot its document's `raw.snapshot` names, without a request for the file (§9.6). A month still
  under 1.0.0 has no point on the paired lines and no filtered planned-against-operating figure
  (§11.1) until it is restated.
- **Snapshot names.** Each month's snapshot is `<ts>.<n>-<YYYY-MM>.xlsx`. DA-10 compaction
  (`services/retention/snapshots.py`) keeps the newest snapshot of each extension (the name after its
  first dot), so each of these, an extension of its own, is never compacted; a test pins the naming,
  and §8 names the explicit fix in the retention module.
- **Status**, in `source_run`'s vocabulary: `ok`, `unchanged`, `partial` (an item failed, a route could
  not be planned, or a month was held), `failed`, `blocked`. The CLI prints the `result` line
  `infra/scheduler/jobs.py::fetch_outcome` reads, so a failing archive alerts as a failing queue does.

### 9.4 Data-quality checks

Each month is checked against the month before, like for like only (`dq.py`). These hold the month
(`dq.status: held`; readers skip it, or keep the month's earlier document, until
`python -m pipeline.connectors.history release <source_id> <YYYY-MM> --by <name>`): a block that
counted no row; rows moved by more than 50 %; a month filed under a file name its own date disputes
(ERCOT, whose month is the report's name: the Summary sheet's "as of" date must fall in it); and a
file that is already another month's served document (`duplicate_file`). These warn: rows moved by
more than 20 %; a rise of more than 5 points in the share of rows without MW; a repeated key; a
status code outside the vocabulary; a fuel or technology code outside the publisher's own list
(`code_vocabulary`, ERCOT); a technology label read as `unknown`; a block whose rows with no
technology carry 100 MW or more (`technology_blank`: EIA lists one to nine such rows in 76 of 135
months, almost all small, and 100 MW or more in 2016-12, 2023-05, 2023-12 and 2026-09); a file whose
name disagrees with the month its own content states (EIA, whose month is the workbook's title); and,
for a list that only grows (EIA's Retired sheet since March 2017), a month in which it lost rows
(`cumulative_shrank`) or in which its settled years, those before last year, moved by more than 1 % of
its MW (`cumulative_restated`). Drift is judged only from 50 rows up (ERCOT's small-generator sheet went
from 4 to 10 rows in March 2022). Found on the full archives (2026-10-23): EIA's Planned sheet carries an
`OP` row in 2015-11, 2018-11 and 2026-06, its Operating sheet `OT` rows in 2016-04 to 2016-07, and its
Planned sheet an `OA` row in 2017-03 (each counted under its own code, and warned); the planned
retirements (Operating rows with a planned retirement date) moved by 21 %, 29 % and 24 % in 2016-04,
2019-01 and 2026-02 (warned). The Retired list's settled years moved by under 0.7 % in every month but
four: 2024-11 (277 generators, 5.2 GW, left out) and 2024-12 (put back), 2026-08 (25 generators, 9.4 GW,
mostly old nuclear, added) and 2026-09 (taken out again), 2 % to 3.2 % each; it lost rows in 2024-01 (6),
2024-08 (2), 2024-11 (270) and 2026-09 (3). Those months are warned, and `us_retired_capacity_mw` still
serves their changes (-2.0 % and +2.9 %, +3.6 % and -3.2 %), now with the warning on the month's
document (`dq_warnings` on each point of a warned month, §10). No month of either archive is held; on
the restated archives (2026-10-24) ERCOT has 82 months that pass and 12 that warn, each for an unlisted
code only, and EIA 116 and 19.

### 9.5 Schedule

`infra/scheduler/history.py`: one weekly tick per source, `--mode auto`, as its own process with a
two-hour timeout, its outcome recorded as a `source_run` row: EIA on Thursdays at 06:28 UTC (EIA releases
in the last week of the following month: September 2026 on 23 October), ERCOT on Tuesdays at 06:49 UTC
(published on the 1st or 2nd). The generic bucket ticks skip `runner: history` rows
(`app.py::due_sources`). An unchanged week costs EIA's index page and a 304, and ERCOT's 80 KB listing.
Each run holds the execution lock of the register source whose snapshots it reads (`us.eia.860m`,
`us.iso.ercot.gen_queue`; `HistoryTick.reads_from`), so it never reads a snapshot that source's fetch
is writing, and the two never ask ERCOT at once (0.5 rps in all). The one-off backfill holds it for
its length (EIA 37 minutes, measured below), and that source's fetch and load wait.

### 9.6 Measured (2026-10-23, this sandbox, into a scratch data root)

| Run | Requests | Bytes | Months | Time |
|---|---|---|---|---|
| ERCOT backfill | 95 (the listing, 94 reports at 0.5 rps) | 49,119,704 | 94 written (2018-12 to 2026-09) | 3 min 17 s |
| ERCOT restatement (a parser change) | 1 | 0 | 94 replaced from snapshots | 34 s |
| ERCOT, again | 1 | 0 | 0 (`unchanged`) | under 1 s |
| EIA backfill | 19 (the index and its placeholder months, the record's file list, 12 zips at 0.1 rps) | 1,162,721,641 | 135 written (2015-07 to 2026-09; the newest from EIA itself) | 37 min 13 s |
| EIA restatement (a parser change; the run `auto` still ran as `backfill`, since the first ended `partial`) | 7 (the index and its placeholder months, a 304 for September's file, the record's file list) | 0 | 135 replaced from snapshots, 125 `pass`, 10 `warn`, none held; the run `ok`, so the next `auto` is `monthly` | 18 min 21 s |
| ERCOT `auto` (now `monthly`), after the runner fix round | 1 (the listing) | 0 | 0 (`unchanged`) | under 1 s |
| EIA `auto` (now `monthly`), after the runner fix round | 5 (the index, its placeholder months, a 304 for September's file) | 0 | 0 (`unchanged`); the origin's validators recorded on the unchanged run | 10 s |
| ERCOT `auto`, fix round (2026-10-24; a hardlinked copy of the same data root): the fuel-first technology, the new checks | 1 (the listing) | 0 | 94 replaced from snapshots, 82 `pass`, 12 `warn` (unlisted codes only), none held; every document has the listing's name and publication date again | 29 to 31 s |
| EIA `auto`, fix round, first | 5 (the index, its placeholder months, September's file) | 14,013,970 | 135 replaced from snapshots; the origin's September answered 200, not 304: the 23:52 run's 304 had recorded the ETag alone, and asked with If-None-Match alone EIA's server sends the whole file (fixed: a 304 keeps the validators it does not repeat) | 19 min 59 s |
| ERCOT and EIA `auto` again, under the final code of the round (its last changes restate every month once more) | ERCOT 1; EIA 5, September a 304 | 0 | ERCOT 94 replaced, 82 `pass`, 12 `warn`; EIA 135 replaced, 116 `pass`, 19 `warn` (the Retired list's four restated and four shrunken months, the planned retirements' three drifts, eight status codes, four months of 100 MW or more with no technology), none held | ERCOT 29 s, EIA 16 min 58 s |
| EIA `monthly`, parser 1.1.0 (2026-10-24, lane eia-tally: the paired tallies of §9.2), against copies of the shared data root's documents and run records, its snapshots read in place; the origin item refused offline on purpose, so the run is `partial` | 0 (one refused) | 0 | 135 replaced from the snapshots their `raw.snapshot` names (2015-07 to 2026-09), each `dq` status as before (116 `pass`, 19 `warn`), none held | 18 min 30 s |

Read against the documents parser `1.0.0+8ad6632f` wrote, the 1.1.0 documents hold every earlier
key unchanged except one: the starting commit's classifier (`pipeline/normalize.py`, changed by an
earlier lane, not by 1.1.0) reads EIA's "Other Gases" as `gas_other`, not `other`, so `by_technology`
moves in every month (September 2026's operating `gas_other` 343.6 to 2,115.9 MW, `other` 3,241.5 to
1,469.2 MW; planned in 42 months, planned retirements in 30). Every month's paired tallies, summed
onto either key, are its state, technology and status tallies, generators and MW exactly.

A document is 4.3 to 7.6 KB (ERCOT) and, with the paired tallies, 73 to 141 KB (EIA; 35 to 65 KB
before them); the two archives' documents take 18.0 MB on disk (EIA 17.4 MB, 7.9 MB before; ERCOT
0.6 MB), the snapshots 1.2 GB (EIA's yearly zips are not kept, only their members, one
snapshot per month). The trends reader's cache of the 135 EIA months is 23.2 MB per API process
(9.3 MB before; 52.6 MB at its peak while the documents are read, 23.4 MB before) and a cold read
1.4 s (0.6 s), measured with `tracemalloc` on the scratch root. Headline figures, as each source lists them: ERCOT's queue held 367 projects and
70,634 MW in December 2018 and 1,758 projects and 438,496 MW in September 2026 (three bases, §9.2);
EIA-860M's planned generators were 1,150 and 95,655 MW net summer in July 2015 and 2,358 and 282,834 MW
in September 2026, and operating capacity 1,063,722 MW and 1,324,125 MW (50 states and DC).

## 10. `GET /v1/trends`

`services/api/trends.py`, the series in `services/trends/series.py`, the contract in `api/openapi.yaml`
(`TrendResponse`).

- **Without `metric`**: the catalogue, every series the caller may read, each with `source` (id, name,
  operator, licence, credit), `method`, `frequency` (`monthly` or `daily`), `dimensions`, `first`,
  `last` and `points`.
- **With `metric`**: the series, one line (`total`) or one per key of `by`, bounded by `from` and `to`
  (inclusive; `YYYY-MM` or `YYYY-MM-DD`, read at the series' grain). Each point has `period`, `value`,
  `basis`, `mom` and `yoy`, and its provenance: a monthly point the publisher's file for that month
  (`source_url`; an EIA month older than the newest is linked at `archive/xls/`, where EIA keeps it once
  the next month is out), `retrieved_at`, and `dq_warnings` when its checks warned on that figure's
  block or on the whole month; a daily point the day's document and the list query whose total it
  was (`inframation_changes` names none: `activity.visible_events` lists the left-out events too, so
  its total is not the figure, and a point names only what its link shows). Each line lists its
  `breaks`; the series lists the months withheld by their checks (`withheld`) and the served months
  whose newer reading is held beside them (`held_readings`).

| Series | Source | Unit | Breakdowns |
|---|---|---|---|
| `us_operating_capacity_mw`, `us_operating_generators` | EIA-860M | MW net summer, generators | technology, state, status |
| `us_planned_capacity_mw`, `us_planned_generators` | EIA-860M | MW, generators | technology, state, status, expected_year |
| `us_retired_capacity_mw` | EIA-860M | MW | technology, state, retirement_year |
| `us_planned_retirements_mw` | EIA-860M | MW | technology, state, planned_retirement_year |
| `us_canceled_postponed_mw` | EIA-860M | MW | technology, state |
| `ercot_queue_mw`, `ercot_queue_projects` | ERCOT GIS | MW, projects | technology, status, phase, zone, cod_year |
| `inframation_active_projects`, `inframation_active_capacity_mw` | the daily record | projects, MW | kind, technology, status, state, grid_operator |
| `inframation_withdrawn_projects`, `inframation_built_projects` | the daily record | projects | kind, state |
| `inframation_open_energy_notices` | the daily record | notices | kind, state |
| `inframation_changes` | the daily record (dated by the day the change was seen, `as_of` minus one) | events | event_type |

**Like for like.** `mom` compares a point with the point exactly one month before and `yoy` with the
point exactly twelve months before (a daily series: the same day of the month, so a 31st has none in a
30-day month), only when both exist, both have a value and both have the same basis, and, for a monthly
series, one parser read both months (§9.3); otherwise null. A gap is never filled. In a breakdown a key
a month's complete tally does not hold is a zero; a document that did not tally the dimension at all (a
schema-1 daily document has no `by_state` on the withdrawn block) has no point for any key, neither a
zero nor a null. Series from different sources are never joined into one line.

A daily point's basis is the posture it was computed under, its rule (the active rule, the energy
relevance rules, the activity rule), so a `genuine-change-v1` day and a `-v2` day never compare, and
its **coverage**: how many sources contributed and a digest of which (`sources-<n>-<digest>`, the
day's `sources` with a link to a counted proposal for a proposal figure, to a counted notice for a
notice figure, to either for the activity), and how many registered sources the tier withheld
(`withheld-<n>`, `visibility.withheld_sources`). The daily figures count Inframation's register, not
an industry, so a register connector going live, or MISO's terms recorded and its rows published,
moves them without anything changing in the world: such a day is a break, listed with
`sources_added`, `sources_removed` and the withheld counts before and after, and no change is
computed across it (fix round, 2026-10-24; before, it read as like-for-like growth, the opposite of
what `notes.like_for_like` promised). Counting the withheld sources is deliberately conservative: a
source registered but not yet readable breaks the line too, though no figure moved; a break never
misleads, a false comparison does. Schema-1 documents carry both fields and are read the same way.

**Stocks by year.** A line by a year a stock waits for (`us_planned_*` by `expected_year`,
`us_planned_retirements_mw` by `planned_retirement_year`, `ercot_queue_*` by `cod_year`) empties as that
year's plants come online, retire or reach operation: the 2026 planned line fell 47.7 % on the year to
2026-09 because most of it was built. No change is computed on a year's line from the start of that
year, and each series' `method` says so; later years' lines keep theirs.

**Who sees what.** A monthly series is served only while its source's `source` row is visible at the
caller's tier (`visibility.source_visible`: `publish_state` and the licence class under the posture), so an
operator who takes a source off the surface takes its series with it; the row exists once the history job
has run (`jobs.fetch_outcome` creates it). Daily documents computed under `noncommercial` are not read
under the `commercial` posture (§5).

**Attribution.** The licence summary has one row per document the response read, per source
(`record_count` is how many months or days it contributed to; `retrieved_at_max` the latest
retrieval among them): a monthly series' months under its source; a daily series' days under every
source that contributed to that day's counts, from the day's `sources`. So the daily series carry the
credits of the derived-only sources they count, `us.iso.caiso.gen_queue` ("Source: California ISO")
and `us.iso.nyiso.gen_queue` ("Source: NYISO") among them (fix round, 2026-10-24; before, a daily
series' summary was empty and its only credit the recorder's list of names). A source the store still
holds is credited as its row credits it today, as every other route does; one it no longer holds, as
the day's document recorded it. The catalogue's summary covers every month and day it describes. The
daily series' `source.credit` joins the contributing sources' own credits. EIA's credit names the
publication month of the latest month read, as EIA's acknowledgment asks ("Source: U.S. Energy
Information Administration (Oct 2026)" for September's data, published 23 October 2026: the data month
plus one, EIA's release schedule).

**Where it reads.** The documents under the connector data root, which the single-host deployment mounts
read-only into the api container (`infra/compose/compose.single.yml`); the three-VM topology shares no
volume, so there the route has no documents until a later lane reads them from the private bucket. Reads
are cached per source by a fingerprint of the files (name, mtime, size): a new or replaced document is
read on the next request, an unchanged directory never twice. Measured on the full archives (135 EIA
months, 94 ERCOT; `TestClient`, in-process, this sandbox), cold (cache cleared) then warm: the catalogue
0.49 s / 0.013 s (15 KB); `us_planned_capacity_mw` 0.17 s / 0.018 s (51 KB); `by=technology` 0.20 s /
0.09 s (0.9 MB); `us_operating_capacity_mw` `by=state` 0.31 s / 0.26 s (2.4 MB, 51 lines of 135 points,
the largest response; a warm request is serialisation); `ercot_queue_mw` `by=technology` 0.04 s / 0.04 s
(0.4 MB). Remeasured in the fix round (2026-10-24, the restated archives, `services.trends.series`
called in-process, without the HTTP layer), cold then warm: the catalogue 0.12 s / 0.003 s (20 KB);
`us_planned_capacity_mw` 0.10 s / 0.002 s (50 KB); `by=technology` 0.15 s / 0.09 s (1.0 MB);
`us_operating_capacity_mw` `by=state` 0.15 s / 0.06 s (2.6 MB); `ercot_queue_mw` `by=technology`
0.03 s / 0.01 s (0.6 MB); `us_planned_capacity_mw` `by=expected_year` 0.20 s / 0.02 s (1.0 MB). What the
series say (2026-10-23, unchanged by the round): EIA-860M's planned capacity was 282,834 MW in September
2026, up 1,864 MW (0.7 %) on August and 23,833 MW (9.2 %) on September 2025, gas more than the whole rise
(combined cycle +19,990 MW, combustion turbine +9,167 MW) while planned wind fell 6,811 MW, storage 2,566
MW and solar 543 MW; ERCOT's queue was 438,496 MW, up 0.1 % on the month and 10.6 % on the year (same
basis both ways). By expected year, the 2027 line was 90,769 MW, up 19.1 % on the year; the 2026 line
shows no change (§10, stocks). `us_retired_capacity_mw` serves 2024-11 (-2.0 %), 2024-12 (+2.9 %), 2026-08
(+3.6 %) and 2026-09 (-3.2 %) with `dq_warnings` naming the restated or shrunken list.

## 11. Planned against operating: `GET /v1/stats/planned-vs-operating`

Owner, 2026-10-23: "could we show planned projects against active project? 1000 planned projects is even more
impressive when you see there's only 1000 active running operations today ... Plus it helps put the datacenter
build out in comparison." The rules are the decisions log row "Planned is shown against operating, so the scale
of the build-out reads at a glance, and honestly" (docs/00-PLAN.md, 2026-10-23). Code:
`services/api/planned_operating.py` and the month reader `services/trends/series.py::latest_month`; contract
`api/openapi.yaml` (`PlannedVsOperatingResponse`). Built after the front-page data (`GET /v1/stats/capacity`)
and this file's series; read by the full-screen map's front strip (lane V2) and a Market page.

### 11.1 Power plants: one survey, one unit, one month

Both sides come from EIA-860M, from the newest month the monthly documents serve (§9; a month its checks hold
is skipped and listed under `newer_months_withheld`, a newer reading held beside the month under
`held_reading`). Operating is the whole Operating sheet, as `us_operating_capacity_mw` counts it and as EIA
titles it ("Inventory of Operating Generators"): operating (OP), standby (SB) and out of service (OS, OA),
split by status under `operating.by_status`. OS is "NOT expected to return to service in next calendar year"
(9,171.9 MW in September 2026), so a line for readers says "in EIA's operating inventory", not "running".
Planned is the Planned sheet. Both count generators and sum net summer MW for the 50 states and DC. Nothing
from the queue register is mixed in: the register counts projects, merges sources and holds queue requests
that never reach EIA's survey.

**Every figure is a series point.** Each figure names the `GET /v1/trends` series it is a point of (`series`:
one query for MW and one for the generator count, both bounded to the month) and the `lines` whose values at
the month add up to it: `total` for a national total, the state or technology key for a filtered one, the
status codes for a stage, and under a filter the pairs of the keys asked for and the stage's codes
(`US-TX|U`, `US-TX|V` for Texas under construction, read from `by=state_status`). A line the series does not have reads as zero only where that zero is true: a
state is one of the 50 and DC, every one of which the month's state tally covers, and a technology must be a
key of the month's technology tally on the operating or the planned side. The register's vocabulary
(`services/labels.py` `TECHNOLOGY_LABELS`) also holds the opportunity and asset tokens (`solar_pv`, `bess`,
`gas`), `load` and `transmission`, which EIA-860M's classifier never produces: 31 of its 51 tokens are a key in
none of the 135 monthly documents, July 2015 to September 2026, that a scratch data root holds from the real
archive (2026-10-24), whose 20 technology keys, operating or planned, are `biomass`,
`coal`, `gas_cc`, `gas_ct`, `gas_ice`, `gas_other`, `gas_steam`, `geothermal`, `hydro`, `nuclear`, `oil`,
`other`, `pumped_storage`, `solar`, `solar_thermal`, `storage`, `unknown`, `waste`, `wind`, `wind_offshore`.
Until the 2026-10-24 review fix, `technology=gas` answered 0 MW on both sides against 295,138.4 MW of
operating `gas_cc`, and `solar,gas` served solar's figures alone; now any token the served month does not
tally is a 400 that names the month's tokens. A token held on one side only (coal, not planned in September
2026) is a true zero on the other. With no month served, the register's vocabulary is the only check and no
figure is served. The tests ask every figure's series through the app and compare, and the measurement did
the same on the September 2026 document: 169 figures across the national view, three states and one
technology filter matched their series, and on the restated document (parser 1.1.0) 1,063 figures across
the nation, every state and every technology, the stage splits included (§11.5).

**Planned is split by stage.** The mapping is served under `stage_mapping`:

| Stage (`stage`) | EIA-860M planned status codes (EIA's own labels) |
|---|---|
| Construction complete, not yet in commercial operation (`construction_complete`) | TS "Construction complete, but not yet in commercial operation" |
| Under construction (`under_construction`) | U "Under construction, less than or equal to 50 percent complete"; V "Under construction, more than 50 percent complete" |
| Regulatory approvals received (`approvals_received`) | T "Regulatory approvals received. Not under construction" |
| Regulatory approvals pending (`approvals_pending`) | L "Regulatory approvals pending. Not under construction" |
| Early or other (`early_or_other`) | P "Planned for installation, but regulatory approvals not initiated"; OT "Other" |

TS is its own stage (review fix, 2026-10-24). The register's lifecycle reads it as `under_construction`
(`pipeline/status_map.yaml` `eia860m`: built but not in commercial operation is not `built`), and the first
version of this endpoint did the same, but EIA's label says construction is complete, so "under construction"
over U, V and TS said something EIA does not: 17,155.2 MW of September 2026's 96,236.0 MW was TS. EIA-860M
has no interconnection-agreement field, so the PLAN row's "approved or with an interconnection agreement" stage
is read as regulatory approvals received (T) alone; both readings are proposed for the decisions log (the
lane hand-back, 2026-10-24). A code outside the vocabulary (EIA's Planned sheet carried an `OP` row in 2015-11,
2018-11 and 2026-06, §9.4) is its own `unmapped` row with its codes, so the stages always add up to the planned
total.

**Filters.** `state` (US-XX in either case, comma-separated, the 50 states and DC; anything else is a 400)
reads the state lines; `technology` (the served month's tokens, above) reads the technology lines. Planned is
split by stage under either filter as without one, and operating by status: the month's paired tallies
(`by_state_status`, `by_technology_status`, §9.2) are served as the `state_status` and `technology_status`
breakdowns of the same four series, so `planned.by_stage` under `state=US-TX` is the Texas pairs of each
stage's codes, every stage listed (zeros too), shares of Texas's planned MW, and the stages add up to Texas's
total like every other split (coordinator's call (a), 2026-10-24, which closed the open breach of the PLAN
row's "planned is always split by stage" found by the review of the same day). Under a state filter the
technology breakdown is null, under a technology filter the state breakdown; `state` and `technology` together
are refused (the document tallies no state by technology). A served month without the paired tally a filter
reads (one still under parser 1.0.0, §9.3) serves no figure under that filter: `power_plants` is null and
`power_plants_note` names the month, so a state's planned MW is never served unsplit; the national view, which
reads the `status` lines, is served as before.

### 11.2 The historical completion rate: one fixed, cited line

"Of the capacity that requested interconnection in 2000-2020, 13% had reached commercial operation by the end
of 2025; 75% had been withdrawn and 10% was still active." Berkeley Lab, *Queued Up: 2026 Edition,
Characteristics of Power Plants Seeking Transmission Interconnection As of the End of 2025* (May 2026;
https://emp.lbl.gov/publications/queued-2026-edition-characteristics; read 2026-10-24 at
https://eta-publications.lbl.gov/publications/queued-2026-edition-characteristics, which serves the same page,
because emp.lbl.gov answers non-browser clients with a challenge, docs/13 §2.1). The page's own words, read twice
on 2026-10-24 (the review and the coordinator): "Only 13% of capacity that submitted interconnection requests from
2000-2020 had reached commercial operations by the end of 2025; 75% of that capacity had been withdrawn and 10% was
still active." Served as constants, never computed. It describes interconnection requests, not EIA-860M's planned
generators, and `applies_to` says so: it is context for how much proposed capacity has been built, not a forecast
of the planned figures. The page states no licence for the report; the figure is quoted with credit
(`licence.basis: citation`).

The PLAN row of 2026-10-23 named the 2025 edition (cohort 2000-2019, to the end of 2024: 13% built, 77%
withdrawn), the newest when it was written. Coordinator, 2026-10-24: the line follows the newest edition, so it
quotes 2026; the headline 13% is unchanged.

### 11.3 Data centres: by power, never by site counts

`planned` is the demand MW of the register's `load` records (data centres and other large loads, as the site's
own headline counts them) in the 50 states and DC, in an active lifecycle state, announced to under
construction, that a register still lists. It is `GET /v1/stats/capacity`'s own tally (`front.served_rows`,
`capacity_summary`, `CapacitySelection`), so it carries the list query whose `include=count` total it is
(`kind=load&jurisdiction=<the 51 US-XX codes>&lifecycle_state=<active>&listed=true`, or the states asked for
under a state filter), counts a value a withheld register supplied as served, never counts a restricted,
unknown-terms or uncleared register's record or one not yet public, and tallies the records no register lists
any more apart (`planned_not_listed`). `demand_records` is how many state MW and `mw_missing` how many do not;
a sum over no stated MW is null, never zero. The first version selected `country=US`, which also holds Puerto
Rico, the territories and records placed only to `US`; the 51 jurisdictions match the EIA side and the state
filter's domain (review fix, 2026-10-24). On the e2e store both selections plan the same (SQLite: the search
runs on `ix_proposal_publish_public_at`, the jurisdiction test is a residual filter either way; no proposal
index covers `jurisdiction`, and `country=` is `upper(...) LIKE`, not indexable either) and answer in 32.4 /
46.0 ms against 33.7 / 77.3 ms p50/p95 through the list. Postgres's plan was not read: the sandbox's cluster
is down and the store is SQLite.

**Neither data-centre register states MW.** Both registers that write `load` records today publish no
capacity (`us_epa_echo_icis_air`: "ICIS-Air publishes no capacity"; `us_va_deq_data_center_air_sites`: "DEQ
publishes no generator or load capacity on this layer"), so their records carry `capacity_mw` null. Unless
a merged source supplies MW, `planned.demand_mw` is null and `mw_missing` counts every record. The power
comparison has no planned figure until a register that states MW lands.

Against it, today's average US data-centre demand and its 2028 range, each cited to the Department of Energy's
release of Berkeley Lab's report (20 December 2024,
https://www.energy.gov/articles/doe-releases-new-report-evaluating-increase-electricity-demand-data-centers):
176 TWh in 2023 (4.4% of US electricity), 20.1 GW on average (176,000 GWh over 8,760 hours); 325 to 580 TWh in
2028 (6.7% to 12%), 37.0 to 66.0 GW (over 8,784 hours, 2028 being a leap year). `comparison_note` says what the
two are: a record's stated MW is often a requested or permitted figure, average demand is energy over hours,
and a site adds its stated MW to average demand only if it draws it around the clock. The figures are for the
US as a whole, with no split by state, so under a state filter `average_demand` is null and
`average_demand_note` says why: a state's planned MW is never set against all-US demand (review fix,
2026-10-24; the first version served the national figures beside a state's planned MW, the move it refused
for the stage split).

Site counts appear only as `sites_we_track`: every lifecycle state, each with its list, the coverage stated (no
complete public inventory of US data centres exists; these are the registers' sites, not a census), and the
sources behind them. A `technology` filter leaves data centres out (`data_centres_note`).

### 11.4 Who sees what, and attribution

The power plants are served only while `us.eia.860m.history` is visible at the caller's tier
(`visibility.source_visible`, as for `GET /v1/trends`); otherwise `power_plants` is null and
`power_plants_note` says which of three reasons holds: the archive is not served at the tier, no month of it
has been read on this store (or it has no source row yet), or the newest served month lacks a sheet or a
tally the figures read (it names the month). Every figure carries its source, as-of month, licence and
credit: the EIA side its source row's licence, the month's workbook URL and retrieval, and EIA's dated
credit ("Source: U.S. Energy Information Administration (Oct 2026)" for September's data); the data centres
the licence summary of the records read (`sources`) and the month of their newest retrieval; the two fixed
lines their publication. The envelope's licence summary has one row for the EIA month read and one per source
of the data-centre records.

### 11.5 Measured (2026-10-24, this sandbox)

EIA-860M September 2026 (EIA's `september_generator2026.xlsx`, 14,013,970 bytes, read through the history
connector into a scratch data root; the same totals as §9.6):

| | Generators | MW (net summer) |
|---|---|---|
| Operating (whole sheet, EIA's operating inventory) | 28,484 | 1,324,124.6 |
| of which operating (OP) | 26,235 | 1,300,414.3 |
| of which standby (SB) | 1,497 | 7,787.6 |
| of which out of service, not expected back next year (OS) | 536 | 9,171.9 |
| of which out of service, expected back next year (OA) | 216 | 6,750.8 |
| Planned | 2,358 | 282,834.4 (21.4% of operating) |
| Construction complete, not yet in commercial operation (TS) | 157 | 17,155.2 (6.1% of planned) |
| Under construction (U, V) | 872 | 79,080.8 (28.0%) |
| Regulatory approvals received (T) | 228 | 26,192.3 (9.3%) |
| Regulatory approvals pending (L) | 406 | 62,872.6 (22.2%) |
| Early or other (P, OT) | 695 | 97,533.5 (34.5%) |

The stages add up to the total (2,358 generators, 282,834.4 MW), and TS plus U and V are the first version's
96,236.0 MW "under construction" (1,029 generators). Since parser 1.1.0 (lane eia-tally, 2026-10-24, on the
135 restated documents, §9.6) a state's and a technology's planned capacity splits by stage too. MW, with
generators and the stage's share of the state's or technology's own planned MW:

| | Operating MW (generators) | Planned MW (generators) | Complete, not operating (TS) | Under construction (U, V) | Approvals received (T) | Approvals pending (L) | Early or other (P, OT) |
|---|---|---|---|---|---|---|---|
| US-TX | 196,421.3 (2,501) | 91,077.4 (539), 46.4% of operating | 9,785.6 (52), 10.7% | 19,958.5 (108), 21.9% | 6,099.0 (33), 6.7% | 15,629.0 (83), 17.2% | 39,605.3 (263), 43.5% |
| US-CA | 104,627.9 (3,243) | 19,937.0 (217), 19.1% | 1,833.8 (26), 9.2% | 6,350.7 (98), 31.9% | 3,913.8 (25), 19.6% | 2,215.8 (16), 11.1% | 5,622.9 (52), 28.2% |
| US-VA | 29,940.6 (756) | 7,300.4 (62), 24.4% | 50.2 (1), 0.7% | 3,498.6 (23), 47.9% | 308.1 (6), 4.2% | 1,581.0 (14), 21.7% | 1,862.5 (18), 25.5% |
| solar | 169,644.1 (8,355) | 121,024.3 (1,100) | 10,711.5 (97), 8.9% | 35,755.0 (514), 29.5% | 14,197.6 (140), 11.7% | 31,528.0 (182), 26.1% | 28,832.2 (167), 23.8% |
| storage | 55,779.8 (1,153) | 63,916.4 (483) | 2,785.2 (29), 4.4% | 20,295.0 (210), 31.8% | 5,295.5 (41), 8.3% | 15,831.1 (85), 24.8% | 19,709.6 (118), 30.8% |
| gas_cc | 295,138.4 (1,945) | 44,258.1 (236) | 1,696.5 (4), 3.8% | 5,066.4 (17), 11.4% | 2,629.9 (6), 5.9% | 6,125.6 (16), 13.8% | 28,739.7 (193), 64.9% |
| wind | 165,284.1 (1,577) | 17,865.5 (73) | 1,363.6 (6), 7.6% | 7,987.9 (23), 44.7% | 1,638.7 (7), 9.2% | 3,409.1 (17), 19.1% | 3,466.2 (20), 19.4% |

Under construction is 28.0% of planned nationally but 21.9% in Texas, where 43.5% is early or other (P,
regulatory approvals not initiated, or OT); 64.9% of planned gas combined cycle is at that stage. Each row's stages add up to its planned
figure. On the restated documents, for the nation, each of the 51 jurisdictions, each of the month's 20
technology tokens, `state=US-TX,US-CA,US-VA` and `technology=solar,storage` (74 requests), every figure served
(1,063: both sides, every stage and status row, every breakdown row) is the sum of its named lines at 2026-09
in the series it names, asked through the app (24 distinct series queries), with no mismatch, and every split
adds up to its figure. The 51 states' stages sum to the national stages exactly, and so do the 20
technologies' (157 generators and 17,155.2 MW complete, 872 and 79,080.8 under construction, 228 and 26,192.3
approvals received, 406 and 62,872.6 pending, 695 and 97,533.5 early). No state has no planned generator in
September 2026; a state with none would list every stage at zero.

By technology, planned storage (63,916.4 MW) exceeds operating storage (55,779.8 MW); planned solar is
121,024.3 MW against 169,644.1 MW operating; planned gas combined cycle 44,258.1 MW against 295,138.4 MW. The
Planned sheet's four rows with no technology (500 MW) are counted under `unknown`, and the month's
`technology_blank` warning rides on `planned.dq_warnings`.

The technology filter on the same document: `gas`, `solar_pv`, `bess`, `load`, `transmission` and `solar,gas`
each answer 400 naming the month's 20 tokens; `gas_cc` answers 295,138.4 MW operating against 44,258.1 MW
planned, `solar` 169,644.1 against 121,024.3, `coal` 167,790.5 against 0.0 (no coal generator is planned).
`state=US-TX` serves `data_centres.average_demand` null with its note.

Response time on a copy of the e2e store (`web/.data/e2e-test.db`, 9,563 proposals) with the
`us.eia.860m.history` source row added as the history job adds it, in process (`TestClient`), 50 warm requests
each after one cold. The review fix round, 2026-10-24, the code before and after the fix in the same session,
the machine shared with other lanes: national 29.7 / 49.9 ms p50/p95 before, 29.3 / 44.8 ms after (29 KB;
the process's first request about 400 ms, the document read and the app's first request); US-TX 29.1 / 40.9
ms (9 KB); US-CA 28.3 / 40.2 ms; US-VA 26.1 / 31.0 ms; `technology=solar` 7.5 / 14.9 ms (7 KB, no data-centre
query). The first round measured 24.4 / 33.8 ms national on a quieter machine.

Lane eia-tally (2026-10-24, the same method, the machine shared with other lanes): the starting code on the
1.0.0 documents (the shared data root, read only) before, this lane's code on the restated 1.1.0 documents
after, p50 / p95 over 50 warm requests:

| Request | Before | After |
|---|---|---|
| national | 34.2 / 44.0 ms (28.7 KB) | 35.9 / 51.6 ms (28.6 KB) |
| `state=US-TX` | 33.4 / 41.8 ms (9.4 KB, unsplit) | 34.6 / 44.3 ms (11.1 KB, split) |
| `state=US-CA` | 35.7 / 44.3 ms | 35.4 / 40.2 ms |
| `state=US-VA` | 33.1 / 48.6 ms | 33.7 / 57.4 ms |
| `technology=solar` | 12.1 / 20.8 ms (6.5 KB) | 14.8 / 26.5 ms (8.1 KB) |
| `technology=gas_cc` | 12.7 / 23.6 ms | 13.3 / 15.2 ms |
| Texas's `planned.series`, `GET /v1/trends` `by=state`, one month | 68.2 / 218.8 ms (25 KB) | 19.5 / 25.7 ms |
| Texas's `planned.by_stage.series`, `by=state_status`, one month | (no such series) | 49.9 / 180.6 ms (143 KB, 331 lines) |
| Texas's `operating.by_status.series`, `by=state_status`, one month | (no such series) | 38.3 / 46.2 ms (80 KB) |

The endpoint itself is unchanged within the noise: it reads the cached month, not the series. A request
after the cache is dropped (all 135 documents read again) took 0.11 to 0.41 s before and 0.21 to 0.27 s after,
the documents being 2.2 times larger. A bounded `GET /v1/trends` now reads only its window and the year before
it (its changes need that year; its breaks are still the whole archive's): before that change, the one-month
`state_status` series took 536 / 692 ms, all 135 months of its 331 lines being built to serve one. 296 bounded
queries over every EIA series, breakdown and eight windows matched the read of every month exactly (2.5 s
against 23.2 s). Unbounded, `us_planned_capacity_mw` `by=state_status` is 16.6 MB and 0.66 s in process
(`by=state` 2.6 MB and 0.07 s). The e2e store holds no `load`
record (it is built from the 2026-09-12 evaluation fixture, before the data-centre sources), so its data-centre
block reads zero records, null demand MW and zero sites, and neither the data-centre figure, the reproduction
of its list on real rows nor its timing with real rows has been measured (the dev store has none either). The
production figure (582 large-load projects, 73 active, on 2026-10-23, from two registers) is to be measured on
the production store, and by §11.3 its demand MW will be null unless a merged source states MW.

**The front strip's headline line**, suggested (the strip itself is lane V2's): "282.8 GW of power plants
planned, 79.1 GW of it under construction, against 1,324.1 GW in EIA's operating inventory (EIA-860M,
September 2026)". Each number is one served figure: `planned.mw`, the `under_construction` stage (U and V),
`operating.mw`. The completion rate sits behind its "Why?". There is no data-centre line yet: the planned
demand MW has no number behind it (§11.3), and none should be written until it does; once a register states
MW it would read "<planned demand> GW of data centres planned against about 20 GW of US data-centre demand
today (DOE, 2024)", national only.

### 11.6 Not yet

- A state's planned MW by technology (and so by technology and stage within a state): the document tallies no
  state by technology, and the two filters together are refused (§11.1).
- The production store's data-centre figure; the e2e and dev stores have no load records, and the two
  data-centre registers state no MW (§11.3).
- A data-centre-only selection: `kind=load` is data centres and other large loads, as the site counts them.
  Every load record on 2026-10-23 came from a data-centre register (EPA ECHO ICIS-Air, Virginia DEQ), and no
  list filter selects a register's data-centre basis on its own. ERCOT's large-load queue connector also
  writes `kind = load` rows, but they are catalogue products and its loader is the catalogue watch, which
  writes no proposal (`services/ingest/loader.py` `SPECIALISED_LOADERS`). A guard test
  (`test_only_data_centre_registers_feed_the_data_centre_side`) pins the connectors that write `kind = load`
  and that route, so a new emitter fails until someone decides whether its records are data centres; one that
  is not would need `data_centres` narrowed by `source_id`. Queue rows whose technology text reads "load" or
  "data centre" are also classified `load` by `pipeline/normalize.py` `TECH_RULES`; none is in any local store.
