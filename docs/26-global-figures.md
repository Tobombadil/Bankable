# 26 — Global figures: Ember's electricity data

Phase 2 data doc. Lane global-ember, 2026-10-23 (fix round 2026-10-24, after review). Status: connector, store,
loader, API and refresh job built; **no row fetched yet** (the API needs the owner's key, §2). Decision rows:
`docs/00-PLAN.md` 2026-10-23, "Global figures lead only where they are credible" and "Activity over time is tracked
from today".

Owner, verbatim: "If we have numbers that are credible for Global numbers then let's lead with those as the top
level global statistics you can drill into. But otherwise let's stick to the US."

## 1. What is built

| Part | Where |
|---|---|
| Manifest row `global.ember.electricity` (terms quoted, read 2026-10-23) | `data/sources.yaml` §N; register row `docs/13` §6 |
| Connector: plan, fetch, parse (API JSON, CSV in two layouts), normalise, checks, revisions; `check` for a download | `pipeline/connectors/global_ember_electricity/` |
| Store: `global_electricity_load`, `global_electricity_value` (migration 0045) | `services/global_figures/models.py` |
| Loader (`--latest`, one transaction per dataset) and reader | `services/global_figures/load.py`, `read.py` |
| `GET /v1/global/electricity` | `services/api/global_figures.py`, `api/openapi.yaml` |
| Refresh job, the 8th and 22nd at 09:38 UTC | `infra/scheduler/global_figures.py` |

## 2. Terms and routes

**Terms (no restriction on this use).** The API documentation, https://api.ember-energy.org/v1/openapi.json,
"License and Attribution" (read 2026-10-23): "Our data is published using the CC-BY-4.0 license. Anyone is able to
use our data for any purpose (personal, commercial, etc.). The only requirements by this license are that: 1. Ember
is cited as the data source. E.g. 'Monthly electricity generation data, Ember' 2. You may not add any additional
legal/technological restrictions to the data." https://ember-energy.org/creative-commons/: "free to share and adapt
our work – as long as you credit us"; "If you would like to use our logo, we ask that you request permission."

The API sign-up page, https://ember-energy.org/data/api/ ("Personal data disclaimer", read 2026-10-24 at about
01:50 UTC): "By requesting an API key, you agree to the following terms: Email verification is used to ensure that
API keys are secure and can only be overwritten by the owner of the email address. All email addresses are hashed
before being stored in our database. The email domain name (e.g. gmail.com, ember-energy.org) will be used
internally to help measure the impact of our data, but no processing will occur on any personal data. No data will
be shared with partner organisations or any third parties. You can request to be deleted from our database at any
time by contacting [an address the page obfuscates], but doing so will revoke access for your API key." These terms cover how Ember
handles the requester's email address; they restrict no reuse of the data. Deleting the address revokes the key, so
the owner keeps the address the key was requested with.

What that means here: every response credits Ember in its own form, links the licence and states the changes
(`changes_statement`); Ember's logo is never used; the values themselves carry no further restriction from us
(public tier, raw-ok), which counsel should confirm for any paid shape that would serve them (§10).

**Routes.** Ember offers two:

1. **The API** (`api.ember-energy.org/v1`; robots.txt answers 404, no rules; "no rate limiting restrictions",
   polled at 0.5 requests a second). Needs a free key, requested with an email address at
   https://ember-energy.org/data/api/. Without `EMBER_API_KEY` in the worker's environment a run ends `blocked`
   (the source's health says so) and nothing is asked. The key travels only as the `api_key` query parameter and
   is removed from every stored body, URL and error text.
2. **The yearly CSV**, https://files.ember-energy.org/public-downloads/generation/outputs/release_generation_yearly_global.csv
   ("Yearly electricity data – Global (CSV)" on the yearly page; not the "Europe only" file,
   `release_generation_yearly_lower.csv`, whose few countries the country check would hold against the global load).
   That host's robots.txt is `User-agent: * / Disallow: / / Allow: /*.pdf$` (read 2026-10-23), so **no connector or
   probe fetches it**. A person downloads it and drops it at `<data root>/inbox/global.ember.electricity/` with
   `<name>.json` beside it: `{"downloaded_at": "<ISO time>", "dataset": "yearly", "reviewed_sha256": "<sha256>"}`.
   `downloaded_at` is required: it becomes the rows' `retrieved_at`, and a file without it is held (the file's
   modification time is recorded as `placed_at`, never as a retrieval time, since a copy resets it). `dataset` is
   optional (the name, else the structure, decides; §7). `reviewed_sha256` is needed once per dataset: the first
   download of the yearly (or monthly) file is held until a person has read what it parsed to and records its
   SHA-256 (§7). `python -m pipeline.connectors.global_ember_electricity check <file.csv> --downloaded-at <time>`
   prints that summary and the sidecar to place, writing nothing. The next run reads the file once; a file
   downloaded before the newest one of its dataset already loaded is skipped (`older_than_loaded`), so an old
   download copied in again never replaces newer figures. The inbox is
   wherever the connector store is: the `connector_data` volume (`/var/lib/infraque/data/inbox/...` in the
   containers), or, with `SNAPSHOT_STORE=s3`, the snapshot bucket under `<SNAPSHOT_STORE_PREFIX, default
   data/>inbox/global.ember.electricity/`. The inbox is read whatever the API answers, so a download is never stuck
   behind a refused key or a server error. Two HEAD
   requests reached the two CSV links on 2026-10-23 in the same command that first read that robots.txt, before
   its answer was read; nothing was downloaded, and nothing since.

The CSV matters because **Ember's API serves no yearly capacity**: its only capacity endpoint is monthly wind and
solar ("covering 25 countries"). Until a person's download is loaded, the response carries generation, shares and
monthly wind and solar capacity, and says why the yearly capacity block is empty.

## 3. What Ember's figures are

- Generation (TWh) and share of generation by nine types: solar, wind, hydro, nuclear, gas, coal, other renewables
  (geothermal, tidal, wave), bioenergy, other fossil. **No storage series exists** in the global data; none is shown.
- Capacity (GW): installed nameplate, from IRENA (non-fossil) and Global Energy Monitor (coal, gas over 50 MW);
  solar is AC or DC as each country reports it (methodology PDF, "Installed Capacity"), so two countries' solar
  figures need not share a basis; the ranking note and the spec say so.
- Coverage: 215 countries from 2000. "Annual data is generally available until 2025"; for the latest year,
  "regional and world figures for this year are estimated" where countries have not reported (methodology,
  https://files.ember-energy.org/public-downloads/ember_electricity_data_methodology.pdf, read 2026-10-23).

## 4. Operating, not pipeline

Ember's numbers are what is built and running. Inframation's US numbers are what is proposed. The two are never one
measure:

- fields say `operating_capacity_gw` and `generation_twh`, and every response carries `measure: operating`;
- for `area=US` the response links to the US pipeline figure as `us_pipeline`, labelled `measure: pipeline`, and
  never adds, subtracts or ranks across the two. The link is the active pipeline only:
  `/v1/stats/capacity?country=US&lifecycle_state=announced,filed,studied,permitted,contracted,under_construction&listed=true`
  (lane front-api's endpoint; the states are `services.api.org_pipeline.ACTIVE_LIFECYCLE_STATES`, the shape
  `organizations.list_queries` serves). Without the filter that endpoint counts every lifecycle state, `built`
  (operating), `withdrawn`, `cancelled` and `unknown` included, and records no register lists any more, which would
  put operating MW inside a figure labelled pipeline;
- a page that shows both shows them under separate labels (for example "Operating, 2025 (Ember)" and "Proposed,
  in queues (Inframation)"), never in one total or one chart axis.

## 5. Definitions

- **Clean share**: generation from solar, wind, hydro, nuclear, bioenergy and other renewables over generation from
  the nine types. Computed from the parts (`ember.CLEAN_SOURCES`); it equals Ember's own `Clean` aggregate.
- **Latest year** (of an area): its newest year with a generation value. A country can be a year behind World.
- **Latest complete year**: scanning down from the newest year, the first whose countries with a generation value
  are at least 90 % of the year before's. `latest_year_estimated` is true for World or a region whose latest year
  is newer: that figure is Ember's estimate. Every block (`generation`, `clean_share`, `operating_capacity`) and
  every series row of World or a region for such a year carries `estimated: true` itself, so a page quoting one
  block keeps the caveat.
- **Year-on-year change**: the same area, source and metric in consecutive years of the same load. Series from two
  datasets or two routes are never spliced (owner, 2026-10-23). A total's or a share's change (total generation,
  each source's share, the clean share, total capacity) compares two years only when both carry the same
  generation types; otherwise it is null and `sources_differ` names the types present in one year only.
- **Which load a figure reads**: of a dataset's loads carrying the metric, the one with the newest period; on a tie
  the API's (a person's download can be an older file copied in later), then the newest retrieved.
- **Wind and solar added**: end-of-year solar plus wind capacity minus the same a year earlier, by country, a **net**
  change (retirements subtract), ranked (top 10) in the latest complete capacity year, so a ranking never compares
  a handful of early reporters. Solar is AC or DC as each country reports it. From the yearly capacity; without it,
  from the monthly capacity, December against December, with a note that it covers fewer countries.

## 6. Store

Tables, not files, as the prices slice does (`services/prices`): the API reads Postgres in every topology, and in
the three-VM one it shares no volume with the worker (`infra/compose/compose.single.yml`). The package is
`services/global_figures`, not `services/global` (`global` is a Python keyword). `global_electricity_load` holds
one row per dataset (`yearly`, `monthly`, `monthly_capacity`) and route (`api`, `file`) currently served;
`global_electricity_value` its values, each with `source_id`, `licence_id`, `source_url`, `retrieved_at`. A new
run of a dataset replaces its load inside one transaction. The connector store keeps the evidence: each answer
(key removed), the normalised parquet with each value's source row (`raw`), revisions and the run record.

Migration **0045**, `down_revision` 0043 (the head on this lane's base, `integ-w1-trends`). Lane front-api adds no
migration (its CHANGELOG line: "No migration"), so 0045 chains straight from 0043; 0044 stays unused unless a later
lane takes it.

## 7. Fixtures

No real file could be recorded on 2026-10-23 (no key; the CSV host forbids robots). The fixtures in
`pipeline/connectors/global_ember_electricity/fixtures/` are **synthetic**: Ember's published shapes (OpenAPI 1.2.3
field names; the July 2026 CSV layout as Ember's page describes it; the earlier long layout) with invented round
values, named `*_synthetic.*`, as the Global Wind Atlas fixture is. Once the key exists, record a real one:

    python -m pipeline.connectors.global_ember_electricity fixture <answer.json> <fixture.json> --areas World,USA,CHN,DEU,IND --years 2023-2025

The CSV parser finds columns by header and fails naming any it lacks. The country code column is required in both
layouts (`ISO 3 code`, `Country code`, `ISO code`, `Code`, ...): without it every country would load as a region,
`area=US` would not exist, and coverage and the ranking would be empty. Which dataset a download is comes from its
sidecar's `dataset`, else its name (`release_generation_yearly_*`, `*monthly*`), else its structure: Ember's yearly
and monthly pages describe their July 2026 files in the same words ("One row per area, month and electricity
source"), so a yearly file may date its years `2025-01-01`. One period per area, year and source, all January, is
yearly data; a file named monthly that looks like that, or that carries capacity values, is held with the reason; a
yearly API answer dated that way is read as years too.

**The first real download is a person's check.** The parser has not met a real file, so the first download of each
dataset is held (`first_file_review`) until a person has read what it parsed to: the dataset and why, areas by kind,
countries, the periods, and World's newest year by source (a year should read about 30,000 TWh, never one month's
share of it). If it is right, they add `reviewed_sha256` to its sidecar (the hold names the value, and `check`
prints it) and the next run loads it. Later downloads of the same dataset load without a review: the checks (§8)
compare them with the reviewed one.

The API's first answer is not held for a person: its field names are Ember's documented schema (OpenAPI 1.2.3),
and the checks hold an answer with no country, unknown series on more than 5 % of rows or values out of range,
and warn on areas Ember flags as countries without a code. The owner reads the figures of the first run with the
key once (§10, item 1) before the front page quotes them.

## 8. Refresh, failure and checks

The tick runs on the 8th and 22nd at 09:38 UTC ("updated twice a month with an update in the first week of the month
followed by a second update in the third week", Ember's data page). The job runs the connector CLI, records the
outcome on `source_run` and the source's health (`jobs.fetch_outcome`), then the loader. **A failed download keeps
the last good data**: no API value is stored unless every API request answered, a held dataset is not written, and
the loader loads only what a run wrote, so a failed API answer changes nothing served. Transient failures retry with
the fetch backoff; a missing or refused key is `blocked`.

Checks per dataset (`dq.py`), against the last run that wrote it: no rows, provenance, duplicate keys, rows and
countries falling more than 30 % (hold) or moving 10 % (warn), no country at all (hold, even on a first load), the
latest period going backwards, unknown series names (hold over 5 % of rows), a metric's missing share rising 10
points, values out of range, and areas Ember flags as countries that carry no usable code (warn: they drop out of
coverage and the ranking). On a route's first load the country count compares with the other route's last load of
the same dataset, so a first download keying far fewer countries than the API's answer is held. A download is also
held without a recorded download time, without its first review (§7), or when its dataset cannot be told. An API
answer holding fewer records than its own `stats.number_of_records` fails the run (a cut-off answer would load as a
shorter series). The monthly series is a rolling 36 months, so a new year never drops a year of rows (a window
starting each January would lose a third of its rows on 1 January and hold); a month that leaves the window is not
counted `gone`. A revised value is counted (`rows_changed`) and kept in `revisions/`; it is never an event: a
statistical revision is not news.

An API failure keeps no API value, but the inbox is still read; the refresh job loads whatever the run wrote, and
the run's status still says what the API did (a transient failure is retried).

## 9. API

`GET /v1/global/electricity?area=World|<ISO2>|<region>&years=<window>`: the latest year's operating capacity and
generation by source with year-on-year change, the clean share and its change, the series since 2000, the latest
month, the top 10 countries by net wind and solar added, coverage, `us_pipeline` for `area=US`, and Ember's credit
in `licence_summary` and `notes.credits`; `estimated` on every block and series row, and `sources_differ` where a
change is withheld. Before the first load: `data.loaded: false`, credit still present.

## 10. Open items

1. **Owner: request the API key** at https://ember-energy.org/data/api/ (an email address of the owner's choosing)
   and set `EMBER_API_KEY` in the worker's environment file. Until then the job ends `blocked` twice a month.
   After the first run with the key, read `GET /v1/global/electricity` for World and US once (World's latest year
   should read about 30,000 TWh, the US about 4,000-4,500 TWh) before the front page quotes it (§7).
2. **Owner: one download of the yearly CSV** into the inbox gives yearly capacity for every area; repeat after each
   Ember release while §2's route is manual. Run `check` on it first, read the summary, and place the sidecar it
   prints (§2, §7). Then record a real fixture from it (§7).
3. **Owner, optional: ask Ember** whether yearly capacity can be served by the API, or the CSV fetched by a robot
   despite the robots.txt. Draft, for the owner to send to data@ember-energy.org: "We use Ember's yearly and monthly
   electricity data, credited as you ask, on inframation.io. Your API serves monthly wind and solar capacity but no
   yearly capacity by source, and files.ember-energy.org's robots.txt disallows automated downloads of the yearly
   CSV. Could yearly capacity be added to the API, or may we fetch the CSV automatically twice a month?"
4. **Counsel**: CC BY's "no additional restrictions" if Ember's values are ever served in a paid shape, and reliance
   on Ember's grant for values it compiles from IRENA and Global Energy Monitor (`docs/13` §6 row).
