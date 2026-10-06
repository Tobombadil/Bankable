# 25 — Grid interconnection points and siting layers

Owner request (2026-09-28): track where projects and data centres connect to the grid, and the fiber that
data centres need. The owner clarified "interconnection sites" as **grid** interconnection points (the
substation or bus a queue position connects into), not internet exchanges or carrier hotels. Decision row:
`docs/00-PLAN.md` decisions log, 2026-09-28.

**Scope, in the order it is built.** Each section below is written by the lane that builds it; the coordinator
verifies and merges.

1. **Interconnection points (G1).** Every US ISO queue row already carries its point of interconnection as text
   (`Interconnection Location`: ERCOT 1,778 rows, CAISO 2,278, NYISO 1,804, measured 2026-09-27 on the stored
   frames), and every NESO TEC row its `Connection Site` (2,198). Surface it and roll up queued MW per point.
   No new source.
2. **Substations and transmission lines (G2).** Built-infrastructure context layers, and the coordinates that
   place an interconnection point on the map. Candidate source: the HIFLD Open layers, archived after DHS
   discontinued HIFLD Open on 2025-08-26 — usable only after their terms are read and recorded (CLAUDE.md).
3. **Data-centre demand (G3).** Public filings that reveal a large load before it is built: state air permits for
   backup generators, utility large-load tariff dockets, and large-load queues where published as data.
4. **Fiber availability (G4).** An area-level "is fiber service present here" layer from the FCC Broadband Data
   Collection. **No fiber routes**: there is no open, current, route-level national dataset (InterTubes is
   2014–15 vintage; carrier maps are the carriers'), and precise routes are security-sensitive.

**Out of scope.** Internet exchanges and colocation facilities (PeeringDB's AUP forbids commercial application
and bulk redistribution: https://www.peeringdb.com/aup). Anything from the private aggregators named in
CLAUDE.md. OpenStreetMap substations and lines (ODbL share-alike; excluded in `data/sources.yaml`).

## 1. Interconnection points

**Status:** built 2026-09-28 (lane G1), owner decision of the same day. Code: `services/ingest/interconnection.py`
(parse, key, linking), `services/api/interconnection_points.py` (API), `web/interconnection_points.py` (pages),
migration `0026`. Model: `docs/21` §3.24 and D-14 to D-18.

### 1.1 What a point is

A grid interconnection point (POI) is where a proposed project connects to the grid: a substation bus, or a tap
on (or loop-in to) a line between two substations. A project connects there; it is not built there. So a point
is not the project's `location` (`docs/21` §3.7) and never places a record on the map.

Every US ISO queue row stores its POI only inside the raw source record, in the gridstatus column
`Interconnection Location`. On the dev store that is ERCOT 1,778 rows (e.g. "59903 Bearkat 345kV"), CAISO 2,278
("Birds Landing 230 kV") and NYISO 1,804 of 1,814 ("Cortland - Fenner 115kV"). The NESO TEC register stores it as
`Connection Site` (2,198 rows). EIA-860M carries no POI. SPP, ISO-NE, MISO and PJM use the same gridstatus
column, so their rows gain points with no code change once their licences clear; until then the loader refuses
them (`CLAUDE.md`, PJM rows are not public).

A point is a record of its own (`interconnection_point`, `poi_…` public id). It carries the provenance quartet
of the register that names it, so attribution renders the same way it does for a proposal.
`proposal.interconnection_point_id` links each proposal to its point. `substation_asset_id` is left NULL for
lane G2's substation crosswalk to fill.

### 1.2 The grouping key

A point is unique per **(register, key)**. It is never merged across ISOs, and never across two registers of one
ISO. The key is derived from the register's text, in this order (`KEY_RULE_VERSION = 2026-09-28.1`):

1. **Clean.** Mis-decoded cp1252 dashes (`\x96`) and en or em dashes become `-`. Whitespace runs, newlines
   included, become one space. Trailing `.,;` is dropped and case is folded. A trailing utility tag
   ("…, ONCOR", "…; AEP") and a comma before a voltage ("Colton - Battle Hill, 115 kV") are punctuation.
2. **Voltage.** The first `N kV` expression becomes `voltage_kv`. All spellings match: `345kV`, `345 kV`,
   `345-kV`, `13.2kV`. In a slash group such as `275/132kV`, the highest value is used. **Voltage is part of the
   key**, because a point is a bus: `Gates 230 kV` and `Gates 500 kV` are two points. No stated voltage is its
   own value, so `Bellota` never merges with `Bellota 115 kV`.
3. **Bus numbers** (ERCOT PSS/E numbering). These forms are removed from the name: a leading 3–6 digit number,
   `#nnnn`, `bus nnnn`, `PSSE nnnn`, `(nnnn)` and `<nnnn>`. A substation's bus number becomes `bus_number` and
   is **part of its key** when stated. Each line endpoint's bus number is part of that endpoint's key.
4. **Kind.** `unknown` when the text names several points or describes one in prose. The markers are `;`,
   ` and `, ` or `, `&`, a comma, a slash between words, `approx`, `located`, `mile(s)`, `between`, `system`, or
   a length over 100 characters. `line_tap` when the text says tap, line or circuit, or joins two names with
   `-` or ` to `. Otherwise `substation`. A NESO `Connection Site` is always a substation, because the register
   names one site per row (e.g. "Sussex and Romney Connection Node A").
5. **Names.** Brackets are dropped and `&` reads as `and`. Other punctuation becomes a space. Generic words are
   removed: substation, sub, station, stn, switching, switch, switchyard, yard, sw, ss, swyd, bus, gsp,
   existing, proposed, poi. `new` is kept, because "New Cumnock" is a place.
6. **Lines.** The endpoints are split on `-` and ` to ` and **sorted**, so A–B is B–A. The key also keeps the
   voltage, and the circuit designator (`#1`, `ckt 2`, `No.1`, `line 4`) when one is stated. A *tap* that names
   one place is that substation. A *line* that names one place ("Warners 69kV line") stays a single-ended
   `line_tap`.
7. **`unknown`** groups only identical cleaned text.

The display name is the register's own spelling. When several spellings group, the most common one is shown.
Strings that name no place produce no point: `TBD`, a bare voltage such as `34.5kV`, `46kV line`, `Line 123975`.

### 1.3 Measured on the dev store (a copy of `web/.data/dev.db`, 2026-09-28)

| Register | Rows with a POI | Distinct spellings | Points | Groups merging ≥ 2 spellings | Rows with no point | Point kinds (substation / line_tap / unknown) |
|---|---|---|---|---|---|---|
| ERCOT | 1,778 | 1,416 | 1,248 | 142 (310 spellings) | 0 | 519 / 644 / 85 |
| CAISO | 2,278 | 1,656 | 1,109 | 281 (828 spellings) | 0 | 589 / 455 / 65 |
| NYISO | 1,804 | 1,441 | 1,181 | 180 (440 spellings) | 32 | 505 / 518 / 158 |
| NESO | 2,198 | 1,237 | 1,204 | 32 (65 spellings) | 0 | 1,204 / 0 / 0 |

- **Proposals linked:** 8,026 of 10,379, or 77.3%. By register: ERCOT, CAISO and NESO 100%, NYISO 97.7%
  (1,772 of 1,814). EIA-860M is 0% because it carries no POI.
- **Points with two or more proposals:** CAISO 366, ERCOT 362, NESO 452, NYISO 296.

**False-merge check.** The false merges below were measured on the rule as first written; each change listed was
made before the final sample was drawn.

- **Exhaustive bus check.** Before bus numbers were in the key, 4 ERCOT substation groups each held two
  different stated buses: Roma 8795 and 8796, Midlothian 1939 and 1940, Cenizo 80220 and 80223, and
  TNCENTRY1 38431 and 38432. So did 2 line groups: Venus 1906 and 1907, and Shamburger 3103 and 3223. These are
  false merges. With the bus in the key, 0 groups in any register hold two different stated buses.
- **Hand check of 30 groupings.** The sample was 30 groupings that merge two or more distinct spellings, drawn
  at random (seed 20260928): 8 ERCOT, 8 CAISO, 8 NYISO and 6 NESO. **All 30 are one bus or one line spelled
  differently.** Examples:
  - "WA Parish 345 kV Bus #44000" and "44000 Wa Parish 345 kV";
  - "Mohican to Battenkill 115 kV Line #15" and "Battenkill to Mohican 115kV line#15";
  - "Walpole 400kV Substation" and "Walpole 400kV substations";
  - "Coalburn 400kV Substation" and "Coalburn 400/132kV".
- **Earlier sample rounds** found three cases that the rule now handles:
  - one false merge, Roma 8795 and 8796, which the bus rule above fixed;
  - one ambiguous merge, "Warners 69kV line" joining the Warners substation, now a single-ended line;
  - one latent risk, "New Cumnock" keyed as "cumnock", which is why `new` is no longer dropped.
- **One junk point.** "Ngrid 115kV" (NYISO) names a utility, not a site. Its two identical spellings group
  correctly, but the point means little.

The cost of this conservatism is misses, not false merges. A spelling that omits the voltage or the bus number
stays a separate point, as do "W 49th St" and "W49th St". Unifying spellings like these is the substation
crosswalk's job.

**Top 15 points by active queued MW (public tier).** All 15 are NESO points. *Corrected 2026-10-06* (audit
2026-09-30, data scientist F3): the NESO connector used each row's `Cumulative Total Capacity (MW)` as its
capacity, so a project split into stages counted its earlier stages again on every later stage. It now uses the
stage's own MW (`MW Connected` + `MW Increase / Decrease`). On the 2026-09-13 register that moves 136 of 2,198
rows and 105 of 1,204 NESO points; all of NESO goes from 731,513.8 MW to 680,306.9 MW (−51,206.9 MW), and the
123 multi-row projects from 136,499.2 MW to 85,292.3 MW. Measured on a copy of the 2026-09-30 dev store with the
NESO frame re-normalised from its own `raw` rows and reloaded:

| # | Point | Active MW | Active projects | Before (cumulative rule) |
|---|---|---|---|---|
| 1 | Alverdiscott 400kV | 13,316.0 | 16 | 13,365.9 |
| 2 | Creyke Beck 400kV | 7,578.4 | 7 | 9,078.4 |
| 3 | Grimsby West 400kV | 6,555.0 | 7 | 6,555.0 |
| 4 | Trent Valley South Connection Node D 400kV | 6,420.0 | 4 | 6,420.0 |
| 5 | Longside 400kV | 5,500.0 | 6 | 6,500.0 |
| 6 | East Claydon 400kV | 5,150.0 | 7 | 6,175.0 |
| 7 | Cheshire Connection Node A 400kV | 5,130.0 | 4 | 5,130.0 |
| 8 | South Anglia Connection Node C 400kV | 4,874.0 | 6 | 5,074.0 |
| 9 | Trent Valley South Connection Node B 400kV | 4,870.0 | 9 | not in top 15 |
| 10 | Branxton 400kV | 4,676.6 | 7 | 6,976.6 |
| 11 | Trent Valley South Connection Node C 400kV | 4,588.9 | 10 | not in top 15 |
| 12 | Norwich Main 400kV | 4,435.0 | 7 | 8,354.0 |
| 13 | Navenby 400kV | 4,369.9 | 9 | 5,569.9 |
| 14 | Birkhill Wood 400kV | 4,350.0 | 5 | 5,150.0 |
| 15 | Drax 400kV | 4,306.0 | 2 | not in top 15 |

Greens, Sizewell and Shurton 400kV (5,100.0, 5,010.0 and 5,010.0 before) fall to 3,500, 3,340 and 3,340 MW.

The largest US point in each ISO:

| ISO | Point | Active MW | Active projects |
|---|---|---|---|
| ERCOT | #3308 Pin Oak 345KV | 3,789.0 | 3 |
| CAISO | Delaney–Colorado River 500 kV line | 3,200.0 | 1 (6 withdrawn) |
| NYISO | East Garden City 345 kV | 1,321.0 | 1 (10 withdrawn) |

**Timing.** Measured in-process on SQLite (TestClient), median of 7 runs, over 4,742 visible points:

| Request | Median |
|---|---|
| `GET /v1/interconnection-points` (default page of 50) | 86 ms |
| with `include=count` | 131 ms |
| second page (cursor) | 101 ms |
| `iso=CAISO&kind=substation` | 80 ms |
| `sort=name&limit=200` | 128 ms |
| detail of the top point | 49 ms |

Backfilling all four registers took 5.3 s (`link_all_points`), and an idempotent re-run 3.5 s.

### 1.4 Aggregation and visibility

- **Totals are computed per request, at the caller's tier, over visible proposals only.** They use
  `proposal_visibility_filter`: record `publish_state`, timing, licence class and a permitted source link. Totals
  are never stored, because a stored total would carry a hidden proposal's capacity (`docs/21` §8 item 4). An
  unpublished 900 MW row, or a Pro-only row that is not yet public, adds nothing to a public total. The tests
  pin this.
- **Buckets.** The buckets are the public list's:
  - *active*: announced through under construction, the web list's default view (pinned equal by a test);
  - *withdrawn*: withdrawn or cancelled;
  - *built*;
  - *other*: `unknown`.

  Megawatts are the sum of `capacity_mw` where stated. A proposal with no capacity counts but adds no MW.
  `by_technology` gives count, active count and active MW per technology.
- **A point exists on a tier only when two conditions hold.** Its naming register must pass `source_permits`
  and `licence_permits`, and at least one proposal at the point must be visible at that tier
  (`interconnection_point_visibility_filter`). If either fails:
  - the point is absent from the list;
  - its detail answers the same 404 an unknown id gets;
  - `?interconnection_point_id=` selects nothing, exactly as an unknown id does. This holds on the proposal
    list, the map, the feed and the alert matcher.

  A point whose only proposals are unpublished, and a PJM-named point beside a visible proposal, are therefore
  both dark (D-17).
- **`derived_only` registers (CAISO, NYISO).** The POI name is published as a normalised place name and voltage,
  in the same class as `name_canonical`. `location.raw_place` stays gated (D-18). This is recorded as an
  assumption for counsel.

### 1.5 API and pages

- **`GET /v1/interconnection-points`.**
  - Filters: `iso`, `jurisdiction`, `kind`, `q` (name substring or exact bus number) and `min_active_mw`.
  - Sort: `sort=-active_mw` (the default), `name` or `voltage_kv`.
  - Paging and count: keyset cursor paging over the aggregate, and `include=count`.
- **`GET /v1/interconnection-points/{public_id}`.** Returns the point, its `totals`, and `proposals[]`: active
  first, then built, other and withdrawn, each by capacity, capped at 500 with `proposals_truncated`.
- **`GET /v1/proposals/{id}`.** Now carries `interconnection_point`: id, name, url, kind, voltage, and active MW
  and counts at the caller's tier. It is `null` when there is no point or its register is not visible.
  `GET /v1/bulk/proposals` carries the same field, since its contract is the detail shape. It is batched per page,
  and it is `null` there when the point's licence does not allow API redistribution.
- **`interconnection_point_id` filter.** It applies on `GET /v1/proposals`, `/v1/proposals/geo`, the proposal
  feed, exports, saved searches and webhooks. The alert matcher implements it (parity test).
- All paths are documented `x-status: live` in `api/openapi.yaml`.
- **Web pages.**
  - `/interconnection-points` has a filter bar, a table and a pager, and is linked from the primary nav as
    "Grid points".
  - `/interconnection-points/{public_id}` shows the fields, totals, a technology table, the projects table and
    the Sources panel.
  - On a proposal page, a "Connects at" row links the point with its active MW and project count.
  - `/proposals?interconnection_point_id=` passes the filter through.

### 1.6 Limits and open items

- **Misses by design.** Spelling variants that omit the voltage or the bus number, and the same substation under
  two registers, stay separate points. The G2 crosswalk (`substation_asset_id`) is the unifying layer. The API
  already embeds `substation_asset` when that column is set and the asset is visible.
- **`unknown` points.** 308 of 4,742 points (6.5%) are prose or multi-point strings. They group only identical
  text; the page labels them "Unparsed". Splitting the multi-point strings into several points is not built.
- **Stored point facts.** `jurisdiction` and `operator` are the mode over all linked proposals at load time,
  including proposals later unpublished. Within one register these agree (for example, every ERCOT point is
  US-TX), but they are not recomputed per tier.
- **Emptied points stay stored.** A point whose register revises every row away keeps its row and becomes
  invisible. Nothing deletes it.
- **Not built:**
  - no sitemap entries for points;
  - no map layer (that is lane G2's area);
  - no events on a point.
- **Not in CI's axe scan.** The CI axe job does not scan `/interconnection-points` yet (`.github/workflows/ci.yml`
  is outside this lane). The pages use the existing list and detail idioms only.
- **Counsel read.** D-18, the POI name for `derived_only` registers.

### 1.7 Sitemap, M-11 coverage and recent changes (lane H2, 2026-09-29)

**Sitemap.** `/interconnection-points` and every point detail page are in the sitemap (`web/sitemaps.py`). The
sitemap reads `GET /v1/interconnection-points` at the anonymous tier, like the other resources, so it lists a
point only when that list does. The list admits a point only when its register passes `source_permits` and
`licence_permits` and at least one of its proposals is visible (`listed_points`). A gated or emptied point is
therefore absent, and the sitemap cannot be used to test whether a point id exists. Rows are keyed by
`public_id`, because a point has no slug. A test pins the sitemap's point set to the set that
`interconnection_point_visibility_filter` admits, so it is not checked against a hand-written list.

**M-11 audit.** `services/visibility_audit/run.py` covers points on both passes. The counts gain an
`interconnection_points` surface.

- *Store pass.* The audit states the rule independently of the predicate. A *clean* proposal is public, past
  `public_at`, of a publishable class, and has an active link to an ungated source. A *clean* point has an
  ungated register and licence and at least one clean proposal. The audit renders the surfaces through the
  builders they call and compares each result with that rule:
  - the detail's point set (`point_shown_printed`) and the list's (`point_listed_printed`);
  - `point_totals`, against the sum over clean proposals (`point_total_printed:totals:<field>`);
  - the detail's proposal rows (`point_proposal_printed`) and its `recent_changes` (`point_change_printed`);
  - the proposal-detail and bulk embeds (`point_embed_printed`, `point_total_printed:{proposal,bulk}_embed:<field>`).
- *Served pass.* The audit sends real anonymous requests:
  - the list's first page;
  - for up to 5 hidden points, the API detail and the web page, which must be the unknown id's 404
    (`point_hidden_served`);
  - `GET /v1/proposals?interconnection_point_id=` for each of those points, which must return exactly the page an
    unknown id gets (`point_oracle_served`);
  - for the 5 busiest clean points, the API detail, one proposal's embed and the web page's active MW
    (`point_total_served`, `point_proposal_served`, `point_change_served`).
- *Budget.* Points cost at most 22 anonymous requests. Web pages go through the site's service identity.
- *Persisted row.* Breach rows carry public ids and field names only. They never carry a point's name or a
  megawatt figure (tested).

**Recent changes at this point.** `GET /v1/interconnection-points/{id}` carries `recent_changes[]`. It holds the
newest 10 change events (by `observed_at`, then `seq`) whose subject is one of the point's visible proposals.

- The event types are `created` and the proposal-lifecycle group of `docs/21` §7.3. The loader emits `created`,
  `status_change` and `withdrawn` today.
- Each event is in the `Event` shape.
- Events are filtered by the same `event_visibility_filter` as `GET /v1/events`, and by `point_proposal_filter`,
  the one clause list that all point numbers use. No row can name a proposal that `proposals[]` withholds.
- The field adds no event type and no table.
- The page has a "Recent changes at this point" table with four columns: date observed, change, linked project
  and credited source. Each event's source joins the Sources panel. When the table is empty, the page says so in
  one line.

### Measured (copies of `web/.data/dev.db` in the lane's scratch space, 2026-09-29)

| Measure | Result |
|---|---|
| Sitemap point URLs | 4,742, equal to the stored points and to the predicate's set (sitemap: an index plus 3 files, 54,421 URLs) |
| Audit on the dev store | `m11 = 0`; points shown 4,742; 16 served point checks, 0 leaks, 0 inconclusive; `web_pages = checked` |
| Points with change events on the dev store | 0 of 4,742 (see below) |
| Recent changes after one perturbed ERCOT snapshot | 84 points with rows; API detail median 57–67 ms on 3 points; audit `m11 = 0` |

- **No events on the dev store.** The queue registers have one snapshot each. The store's 369 events come from
  EIA-860M and matching, and none of their proposals carries a POI.
- **Perturbed snapshot.** This run uses synthetic changes applied through real code. `pipeline/diff.py::perturb`
  (seed 20260929) was applied to the real ERCOT snapshot, the result was diffed by `diff_snapshots`, and it was
  loaded through `load_dataframe` into a second scratch copy. That produced 105 events, of which 85 are
  queue-news types.
- **Rendered rows.** Three points were rendered with their rows:
  - "Tap 345kV 39950 TNP ONE PLANT - 3400 TWIN OAK Ckt 2": 2 rows, "Entered the queue here as studied";
  - "60400 Lynx 138kV": 1 row, "Withdrawn";
  - "44200 Hillje 345kV": 1 row, "Entered the queue here as built".
- **Axe.** The page is axe-clean at 1440 and 390 px, both with rows and in the empty state.

### Limits and open items

- **The audit takes about 35 s on the SQLite dev copy, against 3 s before.** Almost all of that time is one
  query. It evaluates `interconnection_point_visibility_filter` over every point, and SQLite takes 26 s because
  the dev store has no `ANALYZE` statistics. Without them the planner uses `ix_proposal_publish_public_at` for the
  correlated EXISTS instead of `ix_proposal_interconnection_point_id`. After `ANALYZE` on a throwaway copy, the
  same query takes 0.21 s. Postgres is expected to plan it on the FK index; this was not measured here. Running
  `ANALYZE` after `web/dev_up.py` loads the store would fix the dev case. That is outside this lane.
- **No web-page probes in the nightly job.** The scheduler image (`infra/docker/Dockerfile`) copies `services`
  but not `web`, so the nightly audit reports `served.web_pages = unavailable` and makes none. The API probes and
  the store pass still run. Web probes run in the test suite and wherever `web` is installed.
- **Bulk's embed is checked on the store pass only.** Bulk needs an API key, and the served pass is anonymous.
- **The proposal-list oracle check is served-only.** It covers the proposal list alone. The map, feed, export
  and alert matcher share the same clause and are pinned by the existing parity tests, not by the audit.

## 2. Substations and transmission lines (lane G2, 2026-09-28)

Owner decision 2026-09-28: substations and transmission lines become built-infrastructure context layers, for
the map and to place grid interconnection points. Outcome: **transmission lines ship, as a subset; substations do
not, because the only national layer is restricted by its publisher.** Terms are quoted in full in
`docs/13-legal-data-rights.md` §2.19; the manifest entries are `us.lbnl.ferc_hifld_transmission_lines`,
`us.dhs.hifld.transmission_lines`, `us.dhs.hifld.electric_substations` and `us.noaa.ocm.electric_substations`.

### 2.1 Sources and terms (read 2026-09-28, platform user-agent, robots.txt first)

| Candidate | What it says | Route | Decision |
|---|---|---|---|
| EIA US Energy Atlas | Publishes neither layer (Hub catalogue 76 items; EIA ArcGIS org's 79 services; `www.eia.gov/maps/map_data/` guesses all 404) | — | nothing to use |
| DHS HIFLD, transmission lines | data.gov DCAT `MGMT-GMO-HIFLD-847169`: `"license": "https://www.usa.gov/government-works"`, `"accessLevel": "public"`; item use limitation "None (Public Use)" | Canonical item and GeoPlatform service withdrawn; Data Rescue Project archive → DataLumos project 240591, **Cloudflare challenge, not bypassed**, DataLumos terms unread; HSDL's HIFLD aggregation has no line layer; `web.archive.org` resets through this egress | terms clear, route blocked: recorded `open`/`raw_ok`, `verified: blocked` |
| Third-party ArcGIS copies of HIFLD lines | Esri "(Archive)" copy: "licensed under the Esri Master License Agreement"; GeoPlatform user upload and HARC copy: no licence, no provenance | — | never used |
| DHS HIFLD, substations | data.gov DCAT `MGMT-GMO-HIFLD-546955`: `"accessLevel": "restricted public"`; DHS GMO: HIFLD Secure holds "commercially licensed and FOUO … data … approved Data Use Agreement" | withdrawn from HIFLD Open; no archive (DRP sitemap, HSDL group checked) | `restricted`/`none`; **do not ingest, do not derive** |
| NOAA OCM "Electric Power Substations" (coastal, 2017) | data.gov: CC0; InPort: "Use Constraints: For coastal and ocean planning"; lineage: the HIFLD substations layer | zip not fetched | `unknown`/`none` (conflicting terms, restricted lineage) |
| LBNL, "A Harmonized Geospatial Dataset of U.S. Transmission Lines: Linking FERC Form 1 and HIFLD, 1994-2024" (OEDI 8742) | dataset page links "License" to CC BY 4.0; LBNL's own DCAT: `"license": "https://creativecommons.org/licenses/by/4.0/"` | `data.openei.org/files/8742/…csv`, fetched after three HTTP 429s (one request a minute gets through); OpenEI general disclaimer 404 (browser task) | **used**: `attribution`/`raw_ok`, credit Yin, Nait Belaid & Heleno (2026), LBNL, OEDI, CC BY 4.0 |

Substations are critical infrastructure. The rule applied: publish only what a public source itself published, at
its precision. DHS itself restricts the substation layer, so the `substation` asset type stays empty, is not wired
in `services/ingest/assets.py::ASSET_TYPE_SOURCE_IDS` (loading one raises), and is not listed on the home map.
The line layer carries HIFLD's endpoint *names*; they are not assembled into a substation point layer, because
that would republish, assembled, what the publisher restricted.

### 2.2 What the transmission layer is, and is not

Fetched 2026-09-28T13:55:17Z: 26,932,786 bytes, sha256 `ec066aa3…4f50a84`, Last-Modified 2026-09-22. 13,084 rows,
one per HIFLD line (`GlobalID`), 49 states, 70–765 kV (138 kV 5,752 lines; 230 kV 3,325; 345 kV 1,422; 500 kV
369; 765 kV 30), 146,870 geodesic route miles, 540,930 vertices (at most 200 per line). **It is a subset**: only
the HIFLD lines LBNL linked to a FERC Form 1 respondent's line record (171 respondents, investor-owned utilities
mostly) — roughly a fifth of HIFLD's line mileage. Co-ops, municipal systems, the federal power marketing
administrations and most non-IOU western lines are thin or absent. The home-map legend says "lines FERC Form 1
filers own (LBNL); not the whole grid".

Only the HIFLD side of each row is published (endpoints SUB_1/SUB_2, voltage, owner, geometry). LBNL's FERC
linkage is scored and sometimes wrong: the FERC respondent and the HIFLD owner differ on 2,151 of the 10,439 rows
where HIFLD states an owner, and some links join different lines (GlobalID 162595, tier `low`: FERC
"gateway"–"massac", HIFLD "pana"–"faraday"). FERC costs, conductor and structure fields are therefore dropped.
HIFLD's STATUS is not in LBNL's file, so `status` is `unknown`. The source release vintage has nowhere to go in
`services/ingest/vintage.py`'s vocabulary (no filename token, no shapefile member), so `source_vintage` is unset
and the page says "no release stated"; the dataset's own modified date (2026-09-22) is in the run record.

**Owners are shown, not linked.** LBNL lower-cased HIFLD's OWNER and stripped "&", "and" and legal forms
("florida power light"). Against the 2026-09-27 dev store only 73 of 259 distinct owner strings (3,292 of 10,439
rows) key-match an existing organisation (`pipeline.normalize.org_key`: "Florida Power & Light Co" keys to FLORIDA
POWER AND LIGHT, LBNL's string to FLORIDA POWER LIGHT); running the generic edge loader would mint ~186 new
organisations, most of them duplicates of utilities already held. `owner_name` is left empty (the edge loader
writes nothing: measured 0 edges, 0 organisations), and the owner rides as `attributes.owner` (title-cased) and
`attributes.owner_raw`. The asset page shows it as "Owner (as the source names it)".

### 2.3 Loader, sizes and timings (measured 2026-09-28, this sandbox)

`pipeline/context/lbnl_transmission.py` (fetch → snapshot + run record → parse → normalise →
`data/normalized/context/us.lbnl.ferc_hifld_transmission_lines.parquet`), loaded by `web/dev_up.py` through
`services.ingest.assets.load_assets_parquet` like every other context file.

- **No dissolve.** The gas-pipeline layer dissolves 32,961 unnamed segments to 259 operator rows because a segment
  has no identity. A transmission line has one: its endpoints, voltage and owner, which is what a reader and the
  matcher need, and dissolving by owner would destroy all three. 13,084 rows is the scale of the `power_plant`
  layer (14,659). The only reduction is 6-decimal coordinates (~0.1 m); LBNL already caps a line at 200 vertices.
- Normalise: 12.9 s (13.5 s wall incl. state point-in-polygon for `states_crossed`). Parquet 8,658,026 bytes
  (gas pipelines: 2.3 MB; budget 40 MB).
- Load into a fresh SQLite store: assets 4.5 s, edge step 3.2 s (writes nothing). SQLite file 5.8 MB (gas only) →
  38.5 MB (gas + transmission).
- `/v1/assets/geo`: the line index is built once per process and cached. Cold first request 1.19 s (gas only) →
  3.73 s (gas + transmission); each first request at a new zoom pays one simplification pass (~0.8–1.3 s more than
  gas alone), cached after. Warm, `asset_type=transmission_line`: national (zoom 4) 0.20 s, 1,794,739 bytes, 1,500
  features (13,032 lines in view, capped at `LINE_FEATURE_CAP`, longest kept); zoom 7 over central Texas 0.15–0.37 s,
  1.1 MB, 925 lines; zoom 10 over Houston 0.14–0.19 s, 390 KB, 310 lines. Gas pipelines national: 0.45 s, 923 KB.
  The national transmission payload is the largest line response on the site; vector tiles (ADR 0008 §2's "later")
  are the fix if it matters.

### 2.4 Map and pages

`HOME_MAP_ASSET_TYPES` gains `("transmission_line", "Transmission lines", True)`. `web/static/js/map.js` draws it
in its own layer, `asset-lines-transmission`: **dash-dot** (`[4, 1.5, 1, 1.5]`) so the stroke pattern tells a power
line from a pipeline (solid interstate, dashed intrastate), in a new token `--asset-transmission` (amber `#8a6300`,
4.94:1 on paper; dark `#e8c15f`, 9.80:1 on `--bg`), chosen far from the pipeline purple because the two line layers
overlap everywhere at national zoom (a first magenta attempt was indistinguishable in a screenshot). The feature's
`line_class` is its voltage ("345 kV"), shown in the tooltip, in-view row and drawer. The asset page adds Voltage,
"Substations at the ends (as the source names them)" and "Owner (as the source names it)" rows, a dash-dot mini-map
line (static SVG and `asset_map.js`), and says "this line's route" rather than "this pipeline's route". axe-core
4.10.2 in Chromium against a local stack: 0 violations on `/?layers=plants&asset_type=transmission_line,gas_pipeline`,
`/assets?asset_type=transmission_line`, and two line pages (`/assets/scriba-fitzpatrick-345-kv-us-ny`,
`/assets/cloverdale-jacksons-ferry-765-kv-us-va`); no page errors.

### 2.5 Interconnection-point matching (measurement only; nothing written to the database)

`pipeline/context/poi_match.py` (pure) + `python -m pipeline.context.poi_match` →
`data/normalized/context/poi_substation_crosswalk.parquet` (5,800 rows: one per distinct source × POI string ×
state, matched or not; 224 KB). The node table is the **named line endpoints** of the transmission layer
(`lbnl_transmission.endpoint_nodes`: 5,709 (name, state) nodes; 3,927 placed at the endpoint every line carrying the
name shares, 1,782 named by one line and unplaced because LBNL's file does not say which end is SUB_1).

Rules: a POI naming two places ("A - B 115kV", "to", "tap", "line", "ckt") is a line tap and is never forced onto a
node; several places (";", ",", "&", "and", "via") is `multiple`. Names are normalised identically on both sides
(kV figures, bus numbers, ERCOT mnemonics `WEIMAR8` → weimar, node-kind words, owner prefixes such as "SCE owned").
Blocking by the row's state, else the ISO footprint (CAISO: CA, NV, AZ). Exact name first; an exact match at a
voltage the node does not show is kept at score 0.8 only if the node is placed (a node's voltages are only those of
the subset's lines ending there), otherwise rejected; fuzzy (`rapidfuzz.fuzz.ratio` ≥ 92, 3-point margin, voltage
must not conflict) second; same-named nodes more than 3 km apart are `ambiguous`.

Match rate on distinct POI strings (per ISO; "substation-kind" excludes line taps, multiples and blanks):

| ISO | Distinct POIs | Substation-kind | Line tap | Multiple | Matched | Rate (all) | Rate (substation-kind) | Queue rows covered |
|---|---|---|---|---|---|---|---|---|
| CAISO | 1,671 | 991 | 665 | 14 | 366 (363 exact, 3 fuzzy) | 21.9% | 36.9% | 645 of 2,278 |
| NYISO | 1,465 | 660 | 716 | 64 | 124 (119 exact, 5 fuzzy) | 8.5% | 18.8% | 156 of 1,804 |
| ERCOT | 1,427 | 588 | 817 | 20 | 64 (61 exact, 3 fuzzy) | 4.5% | 10.9% | 84 of 1,778 |
| NESO | 1,237 | 1,223 | 3 | 11 | 0 | 0% | 0% | 0 of 2,198 |

The ceiling is the node table, not the matcher: 1,634 of the 2,239 US substation-kind POIs name nothing in the
subset (`below_threshold`), because the lines of co-ops, munis and non-FERC owners are not in LBNL's file — ERCOT
worst. NESO is GB; no US node can match it (the NESO GSP gazetteer already places those rows).

**Precision, hand-checked on 40 matches** (random, CAISO 20 / NYISO 10 / ERCOT 10, drawn after the rules were
final; judged on name, voltage, the queue rows' county against the node's county, and knowledge of the named
substation): **39 correct (97.5%)**. The one error: #33, ERCOT "#80064 Chocolate Bayou 138kV" (an AEP bus, project
in Victoria County) matched to CenterPoint's Chocolate Bayou 138 kV endpoint in Harris County — same name, different
place; the node was unplaced (one line). An earlier 40-sample on a looser rule set found 3 errors and drove two rule
changes: "Paris Switch" → "Parish" (fuzzy 0.909; threshold raised to 92, which also drops the correct "El Sequndo" →
"El Segundo") and SCE "Antelope" → PG&E's 70 kV "Antelope" (exact name, voltage conflict, unplaced node; now
rejected). Automated cross-check on the 544 of 554 matches whose queue rows state a county: the node (or, unplaced, one of
its lines' ends) lies in a county one of the POI's projects is in for 484 (89%); a POI substation legitimately sits in a neighbouring county, so the 11% that disagree is an upper bound on the error, not an estimate of it.

The 40 (✓ correct, ✗ wrong): 1 ✓ Highwind Sub 220kV Bus → Highwind CA; 2 ✓ Rector Substation 230 kV → Rector CA;
3 ✓ Mohave Substation 500kV → Mohave NV; 4 ✓ Whirlwind 220kV → Whirlwind CA (0.8, node 500 kV); 5 ✓ Whirlwind
Substation 500 kV → Whirlwind; 6 ✓ Red Bluff Sub 230 kV Bus → Red Bluff CA (0.8); 7 ✓ Vincent Substation → Vincent
CA; 8 ✓ Palermo 115 kV → Palermo CA; 9 ✓ Sycamore Canyon Substation → Sycamore Canyon CA (unplaced); 10 ✓ Wilson
Substation 230kV Bus → Wilson CA; 11 ✓ Cantua Substation 115 kV → Cantua CA (unplaced); 12 ✓ Whirlwind Sub 220kV
bus → Whirlwind (0.8); 13 ✓ Pleasant Grove Substation 115 kV → Pleasant Grove CA; 14 ✓ Morro Bay Substation 230kV →
Morro Bay CA; 15 ✓ Pleasant Grove Sub Station → Pleasant Grove; 16 ✓ Midway 115 kV → Midway CA; 17 ✓ Windhub
Substation 230 kV → Windhub CA; 18 ✓ Red Bluff Substation 220kV → Red Bluff (0.8); 19 ✓ Whirlwind Substation 230 kV
bus → Whirlwind (0.8); 20 ✓ Miguel Substation 69 kV → Miguel CA (0.8); 21 ✓ Greenbush 115 kV Substation →
Greenbush NY; 22 ✓ Craryville 115kV → Craryville NY; 23 ✓ Millwood 345 kV Substation → Millwood NY; 24 ✓ Robinson
Road 115 kV → Robinson Road NY; 25 ✓ Oswego 115 kV Substation → Oswego NY (0.8); 26 ✓ Coopers Corner 345 kV
Substation → Coopers Corner NY; 27 ✓ Sithe 345kV Substation → Sithe NY; 28 ✓ Oakdale 115 kV → Oakdale NY; 29 ✓
Sugarloaf 138 kV Substation → Sugarloaf NY; 30 ✓ Clay 345 kV Substation → Clay NY; 31 ✓ 7042 Zorn 345kV → Zorn TX;
32 ✓ 40700 Greens Bayou 345kV → Greens Bayou TX; 33 ✗ #80064 Chocolate Bayou 138kV → Chocolate Bayou TX (Harris;
unplaced); 34 ✓ (#170174) LOST PINES 345 kV → Lost Pine TX (fuzzy 0.947); 35 ✓ 1444 Brown 345kV → Brown TX; 36 ✓
5475 Braunig 345kV → Braunig TX (0.8); 37 ✓ 40011 Cedar Bayou 138kV → Cedar Bayou TX; 38 ✓ 7150 Kendall 138kV →
Kendall TX; 39 ✓ 11420 Sweetwater East 345kV → Sweetwater East TX; 40 ✓ Bus# 1048 - Tonkawa → Tonkawa TX (Scurry
County, projects in Nolan).

The crosswalk carries `node_lon`/`node_lat` for placed nodes. **Using them to place an interconnection point
publishes a substation location derived from the line layer** — the thing §2.1 declines to publish as a layer. That
is a decision for the owner before lane G1 wires the link (docs/00-PLAN.md decision note below); the crosswalk's
name link alone (POI → node name → the lines that end there) does not have that problem.

### 2.6 Limits and open items

1. **Full-network lines** need the HIFLD archive: a human reads DataLumos's terms (project 240591) in a browser and
   records them in `data/sources.yaml` before any connector touches it. Never a third-party ArcGIS copy.
2. **Substations** stay empty until a source that itself publishes substations openly is found and its terms read.
3. **Owner links** need a reviewed alias table from LBNL's 259 owner strings to existing organisations (73 already
   key-match); then `owner_name` can be filled and the generic edge loader used.
4. OpenEI's general disclaimer (incl. "Generative AI Terms and Conditions") answered 404 — browser task.
5. `source_vintage` for this source is unset (no vocabulary for a dataset-page modified date).
6. The national line payload (1.8 MB for 1,500 lines) argues for the vector-tile path ADR 0008 deferred.

## 3. Data-centre demand signals

Owner decision 2026-09-28: track data-centre demand from public filings. Measured 2026-09-28 from this environment
through the platform User-Agent (`pipeline/connectors/http.py::user_agent()`); no private aggregator
(CLAUDE.md list, datacenterHawk, Kadoa, Cleanview) was opened.

### 3.1 Why air permits first

A hyperscale or colocation data centre installs tens to hundreds of diesel (increasingly gas) backup generators.
In most states that needs a minor New Source Review air permit, applied for before construction, and state air
agencies keep a facility register keyed by that permit. That makes the air register the earliest public,
machine-readable, reusable trace of a data centre, earlier than the building permit. The other routes are later or
closed: utility large-load disclosures sit in PDF dockets, ERCOT publishes a monthly slide deck, county zoning is one
portal per county. Air registers have two limits. They give no electrical MW, and they miss sites that need no
generator permit (Texas permit-by-rule, see below).

### 3.2 Survey

Ranked by value × feasibility × clarity of terms. "Value" means pre-construction signal, national share of the
market and location precision.

| # | Source (manifest id) | URL | Format / access | Coverage (measured) | Cadence | Fields: name / operator / location / capacity | robots.txt | Terms, quoted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Virginia DEQ Air Sites (Daily), layer 294 (`us.va.deq.data_center_air_sites`) | https://gisdata.deq.virginia.gov/arcgis/rest/services/public/EDMA/MapServer/294 | ArcGIS REST query, JSON, WGS84 points on request | Virginia, all 4,043 air sites; DEQ's own `PLA_DATA_CENTER_YN` flag marks 198. Connector keeps 205: 45 Planned, 1 Under Construction, 159 operating or shut down | daily | facility name (company + campus code); no separate operator column; **exact point** from DEQ; no MW (free-text `PLA_DESC` sometimes counts engines); NAICS, ICIS-Air id, permit class | `gisdata.deq.virginia.gov/robots.txt` 404 (no rule). The sibling host `apps.deq.virginia.gov` answers `Disallow: /` and is not used | DEQ Data and Web GIS Tools Terms of Use, https://geohub-vadeq.hub.arcgis.com/pages/terms-of-use: "GIS information is in the public domain and may be copied without permission; citation of the source is suggested." Plus: no deriving personal information, no overburdening, "as is" | **Built.** Highest value (Northern Virginia is the largest market; DEQ flags data centres itself; Planned rows come before construction), trivial access, clear terms |
| 2 | EPA ECHO ICIS-Air national download (`us.epa.echo.icis_air`) | https://echo.epa.gov/files/echodownloads/ICIS-AIR_downloads.zip | zip of CSV, 70 MB | national; NAICS 518210 or a "data center" name selects 537 facilities in 43 states (VA 144, IL 45, CO 35, GA 31, OH 24, TX 12); 21 Planned + 3 Under Construction (15 of those in Georgia) | weekly | name, street address, county, ZIP, NAICS, operating status; no coordinates in this file (FRS join needed); no MW | echo.epa.gov allows `/files/` (Crawl-delay 10); the REST API host `echodata.epa.gov` answers `Disallow: *` | US federal work, 17 U.S.C. §105 | **Built 2026-09-29 (§3.7).** Public domain and national. Thin before construction, and blind where states do not report minor sources. `PGM_SYS_ID` = DEQ `PLA_ICIS_ID`, so it joins to #1 deterministically |
| 3 | Georgia EPD Air Protection Branch public advisories (`us.ga.epd.air_permit_advisories`) | https://epd.georgia.gov/forms-permits/air-protection-branch-forms-permits/air-permits (e.g. https://epd.georgia.gov/document/document/pa1225-3/download) | text PDF, one block per application | Georgia; applications received and under review. Example: DCB Atlanta West, 76 emergency generators, Douglas County | biweekly | facility name, application no., street address, county, description (generator counts); no MW | `epd.georgia.gov` allows `/document/` | **Not found.** `epd.georgia.gov` links only accessibility and privacy pages; https://georgia.gov/privacy-and-security covers the Georgia Open Records Act, not reuse | **Gated** (`reuse: unknown`). Best pre-construction signal after Virginia; read the state's terms first |
| 4 | Federal Permitting Dashboard (FAST-41), sectors "Data Storage and Data Management" and "High-performance computing …" | https://data.permits.performance.gov/resource/mcm3-xbid.json | Socrata | 2 projects: QTS Richmond Campus 5 (VA), PORTS Technology Campus (OH) | weekly | title, sponsor, state/county, lat/lon, milestones | Socrata API | public domain | Low volume. Already inside `us.permits_dashboard`'s dataset; its sector filter excludes these. A one-line scope change for that connector's owner, not a new source |
| 5 | ERCOT large-load reporting (`us.iso.ercot.large_load_queue`, existing) | https://www.ercot.com/services/rq/large-load-integration | monthly PDF slide deck (aggregates, no rows) | Texas, aggregated | monthly | none per project | — | ERCOT clause 5, open | The existing watch stays. No per-project rows exist |
| 6 | TCEQ New Source Review permits (`us.tx.tceq.air_permits`) | https://www2.tceq.texas.gov/airperm/index.cfm | HTML query application | Texas | daily | — | **`Disallow: /`** on www2 and www15 | not retrieved (site-policies page 404) | **Not usable.** Robots-excluded. Most Texas data-centre emergency engines are Permit by Rule 30 TAC §106.511, which needs no registration and leaves no record |
| 7 | Virginia DEQ "Issued Air Permits for Data Centers" web page | https://www.deq.virginia.gov/news-info/shortcuts/permits/air/issued-air-permits-for-data-centers | HTML list (177 permits, Nov 2024 per press) | Virginia | irregular | — | robots.txt itself answered Akamai 403 | not reached | **Blocked** (Akamai 403 to this IP, WebFetch too). #1 covers the same facilities from DEQ's own register |
| 8 | Ohio EPA issued air permits (eDocument) | https://edocpub.epa.ohio.gov/publicportal/edochome.aspx | PDF search UI | Ohio | continuous | per-document PDFs | not assessed | not retrieved | Not machine-readable. ICIS-Air (#2) covers 24 Ohio data-centre facilities |
| 9 | State PUC large-load dockets (Dominion/VA SCC, Georgia PSC, AEP Ohio/PUCO) | `us.state.puc_dockets` (existing, `unknown`) | PDF filings | per state | per filing | utility aggregates, sometimes counterparties | per site | per state, not retrieved | Document-level evidence, not a register. Belongs to the docket lanes |
| 10 | County zoning and site-plan portals (Loudoun, Prince William, Fairfax) | e.g. geohub.loudoun.gov | ArcGIS hubs / Accela | per county | varies | parcel-level | Loudoun hub answered proxy 502 | not retrieved | One connector per county. Later, for the counties #1 shows matter (Loudoun 87 sites, Prince William 32, Fairfax 32) |

A process note for the record: during the survey the root JSON of `apps.deq.virginia.gov/.../EDMA/MapServer` was
fetched once in the same command as that host's robots.txt, before the robots answer (`Disallow: /`) had been read.
Nothing from that host is used or stored. The connector uses `gisdata.deq.virginia.gov`, the host the DEQ hub itself
lists for the layer.

### 3.3 The chosen source and the model decision

`us.va.deq.data_center_air_sites` (`pipeline/connectors/us_va_deq_data_center_air_sites/`). The selection rule is
DEQ's flag `PLA_DATA_CENTER_YN = 'Y'` (198 rows), or DEQ's own principal-product text naming a data centre (7 rows,
e.g. "Amazon Data Services Inc - DCA-062", flag `N`). NAICS 518210 and name keywords were tested and not used,
because they caught office buildings. Identity is the DEQ air registration number. The status map
(`status_map.yaml`) is Planned → `filed`, Under Construction → `under_construction`, and
Operating / Seasonal / Temporarily Shutdown → `built`.

**A data-centre record is a `proposal` with `kind = load`, `technology = load`. It is not an `opportunity`.**
The reasoning, against the definitions already in force:
- docs/21 §3.1 defines a proposal as "one real-world project" with a sponsor, a site, capacity and a §7.1 lifecycle,
  and lists `load` among its `kind` values (docs/02 §5). A data centre has all of these, and the air register carries
  exactly that lifecycle (planned → under construction → operating).
- docs/21 §3.3 defines an opportunity as a solicitation: an issuer, a title "as issued", a due date and the §7.2
  lifecycle (open/closed/awarded). A data-centre filing solicits nothing and has no deadline. Calling it an
  opportunity would put it in the RFP lists and break the §7.2 state machine.
- CLAUDE.md's split of supply (proposals) and demand (opportunities) is about the *document type*, not the direction
  of energy flow. The large *load* is the project here. The demand it creates for generation, gas and transmission is
  what the match engine (`services/match`) should derive: data-centre proposal ↔ nearby generation/pipeline assets.
  A second record type is not the way to express it.
- Precedent: `us.iso.ercot.large_load_queue` already emits `kind = load` proposals, and `pipeline/normalize.py`'s
  technology rules map `load|data\s*cent` to `load`. **No new table and no migration are needed.** Everything
  loads through `services/ingest/loader.py` unchanged. The DEQ point reaches `location` at `exact` precision via the
  loader's existing raw `Latitude`/`Longitude` promotion, which applies only because the source is `raw_ok`.

### 3.4 What a record means, and what it does not

It **means** that DEQ has an air-programme facility record for this site, and that DEQ, or DEQ's own
principal-product text, says the site is a data centre. The status says whether DEQ records the site as planned,
under construction or operating. The point is DEQ's facility reference point, rounded to 6 decimals because the
server's reprojection adds noise beyond that. Of the 205 points, 180 carry 6 decimals, 19 carry 5 and 6 carry 4.
The county is our derivation (point-in-polygon on the vendored Census counties). It agrees with EPA ICIS-Air's
county on 157 of the 160 rows that join. The 3 that disagree sit on the Manassas city / Prince William line.

It **does not mean**:
- **A permit has been issued.** `filed` means "a filing exists". The layer does not say whether the minor-NSR
  permit is granted.
- **A capacity.** `capacity_mw` is null on every row. DEQ publishes generator descriptions, not electrical load, and
  the connector never converts engine counts into MW.
- **One building.** One DEQ registration often covers a campus ("IAD-110, IAD-111, IAD-112, and IAD-113"). Two
  registrations can also be one campus in phases.
- **Who owns it.** `sponsor_name` is null. The facility name mixes the company, a special-purpose LLC
  ("MECP1 Ashburn 2 LLC", "Peanut LLC") and the campus code. Resolving the owner is an organisation-graph task, not
  this connector's job.
- **Grid demand.** A site with a generator permit may take its power from any utility. Some flagged sites are
  enterprise, hospital or bank IT rooms, not hyperscale. DEQ's flag includes "Bank of America - Sandston",
  "Sentara Healthcare", "Inova Health Care Systems - IT Operations" and one "Northrop Grumman" site whose principal
  product reads "Office Building". The flag is kept as DEQ set it.
- **A cancellation, when a site disappears.** The layer holds only active and planned sites. A site that drops out
  becomes a `removed` event, which is not evidence that a built data centre was cancelled.

### 3.5 Limits

- Virginia only. The national extension is ICIS-Air (#2), then Georgia (#3) once its terms are read. Texas cannot be
  covered through air permits (robots-excluded, and permit-by-rule engines are not registered).
- Coverage is limited to what DEQ has flagged: an unflagged data centre with no data-centre principal product is
  missed. On the measurement date, 109 sites had a blank flag and 3,736 had `N`. A server-side search of both
  (NAICS 518*, or "data" in name or principal product, or "data center" in the description) found 11 candidates, all
  flagged `N`. The 7 whose principal product names a data centre are included. The other 4 are left out: an office
  building, a telecom site, a tyre-recycling plant that "supplies a data center", and a "Data Processing" office park.
- Before-construction lead time is DEQ's. A site enters when its application is logged, typically months before the
  permit and a year or more before energisation. The layer does not publish the application date, so `queue_date`
  is null. The first time a row appears (`first_seen`) is the best date we have.
- `PLA_DESC` sometimes counts generators ("280 diesel engines", "Four 500 kWe and One Hundred Ten 3000 kWe
  engine-generator sets"). Extracting that is a later, separately verified step. Engine rating is not load, so it
  must never be presented as MW.
- Attribution: "Virginia Department of Environmental Quality". The terms only suggest it; it is rendered as policy.

### 3.6 Run, 2026-09-28

One live run (`python -m pipeline.connectors run us.va.deq.data_center_air_sites`, into a scratch data root so
nothing was written to the shared `data/`). Result: HTTP 200, 288,467 bytes, sha256 `43aba64f…9c22`, 205 rows, DQ
**pass**, with no unmapped status, no duplicate ids and the provenance quartet on every row. Lifecycle: built 159,
filed 45, under construction 1. Precision after load: 205 `exact` (`geocoder = source_provided`), 205 with a county
FIPS. Counties: Loudoun 87, Fairfax 32, Prince William 32, Mecklenburg 10, Henrico 8, Fauquier 5. The rows were
loaded through `web.data_loading.load_real_normalized_sources(source_ids=[...])` into a scratch SQLite store, and
`GET /v1/proposals?kind=load` returns them.

### 3.7 National follow-on: EPA ICIS-Air (lane H1, 2026-09-29)

`us.epa.echo.icis_air` (`pipeline/connectors/us_epa_echo_icis_air/`) extends §3.3 nationally. It uses the same
record model: one `proposal` per facility, `kind = technology = load`, no MW, no sponsor. Everything below was
measured on 2026-09-29 from this environment through the platform User-Agent. No private aggregator was opened.

#### Sources, access and terms

| File | Size (Last-Modified) | What it gives | Access used |
|---|---|---|---|
| `https://echo.epa.gov/files/echodownloads/ICIS-AIR_downloads.zip` | 70,252,181 bytes (2026-09-27), 10 CSVs | `ICIS-AIR_FACILITIES.csv`: 280,208 facilities, 19 columns, no coordinates, `REGISTRY_ID` (the FRS id) | Two ranged GETs: the zip tail (central directory), then the facilities member only (10,555,976 bytes). The member is checked against the directory's CRC-32. A server that ignores `Range` gets the whole zip read normally |
| `https://echo.epa.gov/files/echodownloads/echo_exporter.zip` | 443,452,854 bytes (2026-09-27), one 2.14 GB CSV | 3,221,273 facilities. `FAC_LAT`/`FAC_LONG` are "from the FRS EPA Locational Reference Tables (LRT) file which represents the most accurate value for the facility", with `FAC_COLLECTION_METHOD`, `FAC_REFERENCE_POINT` and `FAC_ACCURACY_METERS` (ECHO column dictionary, `https://echo.epa.gov/system/files/echo_exporter_columns_7-16-2025_0.xlsx`) | One whole GET. The file is streamed row by row and only 10 identity and location columns are kept, for the candidates' FRS ids |
| `https://echo.epa.gov/files/echodownloads/frs_downloads.zip` | 368,601,232 bytes (2026-09-27) | `FRS_FACILITIES.csv`: `LATITUDE_MEASURE`/`LONGITUDE_MEASURE` only (ECHO's FRS data dictionary, `https://echo.epa.gov/tools/data-downloads/frs-download-summary`) | **Rejected.** The file has no collection method or accuracy, so it cannot say which points are real |

- robots: `echo.epa.gov/robots.txt` allows `/files/` and sets `Crawl-delay: 10`. The manifest's 0.1 rps is that
  delay: 4 requests per run, 78.4 s. `echodata.epa.gov` (the ECHO REST host) answers `Disallow: *` and is never
  used.
- Terms: US federal work, public domain (17 U.S.C. §105; docs/13 §2.12 hedge). Manifest `reuse: open`,
  `publication: raw_ok`.
- Snapshot: the stored snapshot is the filtered pair of tables (633 ICIS candidates, 614 Exporter rows, 258,585
  bytes), not the 513 MB of upstream bytes. The Virginia connector makes the same choice with its server-side
  `where`. The fetch pre-filter (`is_candidate`: a data-centre name, or NAICS 518210/541513/541519) is looser than
  the selection rule, and `parse` applies the rule, so the fixture tests it. Upstream sizes, Last-Modified and
  row totals go in the run record.
- Personal data: `ICIS-AIR_FACILITIES.csv` has no contact, person or phone column. The Exporter is read for
  identity and location only; its demographic and compliance columns are never read. `redact` strips any
  contact-like column if one ever appears.

#### Selection rule, precision and misses

A facility is kept when it is **not `Permanently Closed`** and meets one of three conditions. Each row records
which one as `select_basis`:

1. `name`: `FACILITY_NAME` matches `DATA CENTER|CENTRE`, `DATACENTER` or `DATA CTR`, or ends in `DATA CENT`
   because ICIS cut the name at 40 characters ("CENTRA HEALTH ADMINISTRATION - DATA CENT") (162 rows).
2. `naics_518210`: `NAICS_CODES` contains 518210 (326 rows), except in two cases. The first is a co-code in
   211/212/213 (mining and oil and gas). The second is a name that states a non-data-centre use: headquarters,
   BPO, paper mill, oil and gas, or a well pad / "DFM" (digital flare mitigation). Together these exclusions
   remove 11 rows: eight flare-gas crypto-mining generator sets at Colorado and New Mexico well sites (NYDIG DFM
   ×6, GRMR Oil and Gas, Gold State Facility), plus FDR Headquarters, Philcade/IBM BPO and Lufkin Paper Mill.
3. `naics_541513_operator`: NAICS 541513 or 541519, plus a name that contains a colocation or hyperscale
   operator (Equinix, Digital Realty, QTS, CyrusOne, Cologix, EdgeConneX, T5, Flexential, …) (26 rows). On its
   own, 541513 also selects offices such as L.L. Bean, Deere & Co and an IBM environmental-affairs office.

The survey's rule (NAICS 518210 or a "data center" name) selected 537. The final rule selects **514**:
537 − 38 permanently closed − 11 excluded + 26 from the operator basis.

**Precision, hand-checked.** Two random samples of 40 were drawn. The first came from the survey rule (seed
20260929). The second came from the final rule before the well-pad exclusion was added (seed 20260929; one
sampled row, NYDIG Surprise S9, was then excluded). Each row was judged from its name, NAICS, status and ICIS
programme subparts. The unclear ones were looked up (web search; no aggregator):

| | Data centre | Uncertain | Not a data centre |
|---|---|---|---|
| Survey-rule sample (40) | 36 | 2 (First Data Resources Omaha, Nalco Water Northlake) | 2 (NYDIG well pads) |
| Final-rule sample (39 kept) | 31 | 3 (Amazon.com DK01 Littleton MA; Chicago Enterprise LLC; Innovation 2201 LLC, an office/lab building now proposed for a data-centre campus) | 5 (Northrop Grumman Fairfax and Falls Church offices, US Liability Insurance Wayne PA, Concordance Healthcare Grapevine TX, FCA US Auburn Hills) |
| **Distinct rows under the final rule (73)** | **63 (86 %)** | 5 | 5 |

By basis, among the 73 distinct rows: `name` 22 of 22, `naics_541513_operator` 2 of 2, and `naics_518210` 39 of
49, with all 5 non-data-centres and all 5 uncertain rows in this basis. Two further lookups confirm that the
residual class is **corporate offices and campuses with an IT room coded 518210**. IBM Dulles Station West is an
office building with "a data center" on two floors (Stantec, `https://www.stantec.com/en/projects/united-states-projects/i/ibm-dulles-station-west`,
and Work Design Magazine). R&R Realty Urbandale is an office developer. Pollutant class does not separate them:
the offices are MIN, SMI and MAJ, and so are real data centres. No further rule removes them without dropping
real data centres. The basis travels on every row so a consumer can apply its own threshold. Enterprise data
centres (banks, insurers, hospitals, universities, state IT) are counted as data centres, as DEQ's own flag
counts them (§3.4).

**Cross-check against Virginia DEQ's own flag.** Of the 146 Virginia rows the rule selects, DEQ flags 138 as data
centres (95 %). The other 8 are IBM Dulles Station West, Sungard Availability Services, Northrop Grumman Falls
Church, The World Bank (Planned), First Health Services, Rockingham Memorial Hospital Data Center, Centra Health
Administration Data Center and Edge Connex Data Center Norfolk (Planned). Most are DEQ gaps rather than rule
errors; IBM Dulles Station West and Northrop Grumman Falls Church are offices. Of DEQ's 205 data centres, 160 appear in
ICIS-Air, and the rule selects **138 of those 160 (86 % recall)**.

**What it misses.**
- Data centres whose ICIS record has no data-centre name and is coded outside 518210/541513/541519. Of the 22
  DEQ data centres missed in Virginia: 6 are telecom-coded 517110/517111/517112 (Verizon, Level 3, Zayo,
  Chantilly Technology Partners/H5, Equinix LLC, 21571 Beaumeade Circle); 6 are 541513/541519 under an owner or
  SPV name the operator list does not know (Oath, Comcast, Freddie Mac ×2, and the Digital Realty SPVs Digital
  Loudoun Pkwy Center N and Digital Western Lands); 2 banks (522xxx: Capital One, Bank of America Sandston); 2 real
  estate (531xxx: New Dominion Technology Park, Captone Mission Ridge); and one each of 334111 (Plaza Office
  Realty), 511110 (Valo Park), 541511 (Verisign), 611310 (George Washington University), 927110 and 928110
  (Aerospace Corporation sites). The operator list only fixes named operators.
- Data centres that are **not in ICIS-Air yet**. 45 of DEQ's 205 are Planned sites with no ICIS record. This is
  the main reason ICIS is thin before construction: nationally 21 Planned + 3 Under Construction, 15 of them in
  Georgia.
- States that do not report minor sources to ICIS-Air, and sites that need no registration (Texas
  permit-by-rule engines; Texas has 12 selected facilities).
- Permanently closed facilities, by design.

#### Reviewed suppressions (2026-10-06)

The residual false positives are offices, and no rule removes them (above), so the verified ones are suppressed
by hand: `data/vendored/data_centres/icis_air_not_data_centres.yaml`, keyed by FRS registry id, each entry with
its reason and evidence. `services/resolve/suppress.py::apply_suppressions` runs in the resolution step
(`infra/scheduler/jobs.py::default_resolve`, so also `web.dev_up`). A proposal whose only active sources are listed
ICIS records becomes `unpublished`, with one `unpublished` event (actor `pipeline`, the reason, before/after
publish state; not itself published). The connector still selects the row and the link keeps its raw payload; an
admin re-publish stands, because the suppression event is written once per proposal and id. A proposal another
source supports stays published and is reported. Listed (5): IBM Dulles Station West, Northrop Grumman Falls
Church, US Liability Insurance Wayne PA, Concordance Healthcare Grapevine TX, FCA US Auburn Hills. Not listed:
Northrop Grumman Fairfax, which this section's hand-check called an office but Virginia DEQ's own register flags
as a data centre (contested), and Deere & Co Moline, Northrop Grumman McLean and Lebanon (unverified). Measured on a
copy of the 2026-09-30 dev store: public live `load` proposals 581 → 576; a second run changes nothing. The
same-facility ICIS duplicates under re-padded programme ids (audit F8) are not addressed here.

#### Placement (coordinates) and precision grades

Of the 514 selected rows, 509 join to the Exporter on `REGISTRY_ID`. The other 5 have no FRS id in ICIS. A row
gets `Latitude`/`Longitude` in `raw`, which the loader promotes to `exact`, only when FRS says the point is the
site. That requires all three of the following:
(a) a site-specific collection method: address match to the house number, geocoded address, digitised
address, photo, satellite or map interpolation, classical survey, or GPS;
(b) a stated `FAC_ACCURACY_METERS` of at most **200 m**;
(c) a point inside the facility's own state (vendored Census state boundaries).
Every other row carries **no coordinate at all**, only FRS's method and accuracy. The loader places it from its
county name (county centroid) or its state (state centroid). A ZIP-code or county centroid from FRS is never
passed off as a point.

| `placement` (parse) | Rows | FRS collection method (all 514) | Rows | Precision after load (scratch SQLite) | Rows |
|---|---|---|---|---|---|
| `exact` | 379 | ADDRESS MATCHING-HOUSE NUMBER | 338 | `exact` | 379 |
| `method_not_site_specific` | 122 | Zip Code Centroid | 64 | `county_centroid` | 129 |
| `accuracy_not_stated_or_coarse` | 7 | INTERPOLATION-PHOTO | 39 | `state_centroid` | 6 |
| `no_frs_record` | 5 | (none) | 33 | `unknown` | 0 |
| `point_outside_state` | 1 (Wal-Mart North Data Center, Pineville MO) | ADDRESS MATCHING-OTHER 14, UNKNOWN 8, BLOCK FACE 8, INTERPOLATION-SATELLITE 4, GDT geocoding 3, GPS 1, ADDRESS MATCHING (GEOCODING) 1, INTERPOLATION-MAP 1 | 40 | | |

The house-number address matches carry stated accuracies of 30 m (median) and 180 m (maximum). The FRS reference
point is the centre of the facility for 207 selected rows and the entrance for 162. `exact` here therefore
means a facility-level point good to a couple of hundred metres. It does not mean a surveyed building footprint.

County: for an exact point, the county is the one the point falls in, so point and county never disagree.
Otherwise it is ICIS-Air's `COUNTY_NAME`, with "Undetermined" and blank read as missing. The two agree on 365 of
the 376 exact rows that carry an ICIS county. The 11 that disagree include the Manassas city / Prince William
line and ICIS entries that name the wrong county: Microsoft MKE 3B at Mount Pleasant, WI is "Richland" in ICIS
but the point is in Racine County.

#### De-duplication with Virginia

DEQ's `PLA_ICIS_ID` equals ICIS-Air's `PGM_SYS_ID`. Both connectors emit `cross_refs` `icis_air:<id>`, and ICIS
also emits `frs:<REGISTRY_ID>`. Two changes in `pipeline/resolve.py` make the scheduler's `resolve_tick`
(`infra/scheduler/jobs.py::default_resolve` → `pipeline.resolve.run` → `services/resolve/merge.py`) merge the
overlap. Both are keyed on an explicit `SHARED_ID_NAMESPACES = {icis_air, frs}`, so name-derived citations
(`NYISO:…`) are untouched and the evaluation set's numbers do not move:
- **D3, shared registry id**: records of two sources that cite the same `icis_air:`/`frs:` id pair
  deterministically (score 100). The group must be unambiguous: one record per source. A source that repeats the
  id is left to review.
- **Id-conflict veto**: a fuzzy pair whose sides cite *different* ids in the same namespace is never accepted.
  Without it, the name-token block (B3) merged neighbouring campuses of one operator on name + county alone.
  Examples are "Microsoft Corp - LVL Data Center" with "Microsoft Corp - AVC17 Datacenter", and CyrusOne NVA14
  with CyrusOne Kincora. Measured on the real frames, this produced clusters of 3, 4, 5, 7, 9 and 17 members.
  With the veto, 306 pairs are refused (101 of them above the threshold of 75).

Verified on the real data. Both connectors were run live into a scratch data root (ICIS 514 rows; Virginia 205
rows, re-run the same day) and loaded through `web.data_loading.load_real_normalized_sources` into a scratch
SQLite store, which gave 719 live proposals. `default_resolve` was then run over the same data root: 138
clusters, 138 merges, 0 review decisions, **581 live proposals**. All 138 Virginia facilities present in both
sources sit on exactly one proposal. No proposal has more than two source links. Every cluster has two members:
one DEQ record and one ICIS record with the same id. The survivor follows `choose_canonical`, which picks the
most recently retrieved record, and keeps both source links. ICIS adds 8 Virginia facilities DEQ does not flag,
listed above.

#### Run, 2026-09-29

`python -m pipeline.connectors run us.epa.echo.icis_air --data-dir <scratch>`: run `768140bc`, status ok, DQ
**pass** (no unmapped status, no duplicate id, provenance quartet on every row), 514 rows, snapshot sha256
`96782ebd…8d55`. This is the second live run of the day: the first, `84c2398a`, gave 513 rows and missed the
truncated Centra Health name, which led to the rule fix above.

- By state (42): VA 146, IL 49, GA 30, OH 24, CO 21, NJ 19, PA 19, NE 19, AZ 17, IA 16, MN 13, MD 12, TX 12, IN 10,
  MO 9, NC 9, TN 9, OK 9, MA 7, NH 7, NM 6, KS 5, NV 5, DE 5, MI 4, WY 4, AL 3, WI 3, SC 3, and 1–2 each in AR, WA,
  NY, CA, KY, MS, CT, FL, ME, ID, OR, UT, SD.
- Lifecycle, from `AIR_OPERATING_STATUS_DESC` via `status_map.yaml`: built 466 (Operating 464, Temporarily Closed
  2), unknown 24 (blank status), filed 21 (Planned Facility; GA 15, VA 2, NM 2, NE 2), under_construction 3
  (Woodland Caribou IN, Valara Holdings HPC SC, Amazon IAD-264 VA). Seasonal is mapped but does not occur in the
  selection. Permanently Closed is never selected.

#### Limits and open items

- Precision of the `naics_518210` basis is about 80–90 % (offices with an IT room). The basis is on every row, but
  the public page does not yet show it.
- The web dev loader (`web/data_loading.py`) and `web/build_data.py` do not run the resolver. Until a
  `resolve_tick` runs, the dev store and the static build show the 138 Virginia overlaps twice. This is true of
  every multi-source overlap today, not only this one.
- The Exporter costs 443 MB a week because its single deflate stream cannot be ranged. Two cheaper options are a
  conditional GET on Last-Modified and a streaming download path in `PoliteSession`, which today reads the whole
  body because its challenge check touches `.content`.
- Georgia's 15 Planned sites are the richest pre-construction signal outside Virginia. The Georgia EPD
  advisories (§3.2 #3) remain gated on terms.

### 3.8 Dev and static builds run resolution (lane H4, 2026-09-29)

§3.7 left one item open: the dev store did not run the resolver, so every cross-source overlap showed
twice. `python -m web.dev_up` now runs the scheduler's own resolution step after the load. That step is
`infra/scheduler/jobs.py::default_resolve`, the body of `resolve_tick`. `--no-resolve` skips it.

**Order.** The steps run as: load the sources (or the committed fixture), load the context layers,
resolve, link interconnection points, compute matches, then `ANALYZE`. Resolution runs before the
point and match passes so that both see only surviving proposals. `ANALYZE` stays last.
`web/dev_up.py::build_store` holds this sequence and `main` calls it.

**Seam.** `web.dev_up` imports `default_resolve` lazily from `infra.scheduler.jobs`. It adds no rule of
its own. `infra` is not a root package in `infra/importlinter.ini`, and the `services.resolve` and
`pipeline.resolve` imports stay inside `default_resolve`. So the `web-reads-through-the-api` contract
is not widened: it still has 4 contracts kept, 0 broken, and no new ignore line. The resolver reads the
same frames the dev loader loaded: the latest `ok` run per implemented proposal source under
`--data-dir`. On the measured root, all seven proposal sources resolve to the same file.

**Schema fix.** A fresh dev store had no `resolution_decision` table. The model is declared in
`services/resolve/models.py`, and `init_db` imported only `services.db.models`. Until now the API
server created the table at startup, after the load, which is why no dev load had failed on it. The
first cluster the gate files for review raised `no such table`. `services/db/session.py::init_db` now
imports both modules. Postgres is unaffected, because migration 0005 creates the table there.

**Static build.** `web/build_data.py` reads parquet directly and does not resolve. It is not given a
dedupe, because nothing serves its output. `web/app.py` reads only the API, and the only reader of
`proposals.geojson` / `stats.json` is `web/test_build_data.py`.

#### Measured (scratch copy of the dev data root plus the §3.7 ICIS-Air run, 2026-09-29)

The data root was every `data/normalized/*` source, plus `us.epa.echo.icis_air` run `768140bc` (514
rows). Timings are from one run each on this sandbox.

| | Value |
|---|---|
| Full `dev_up` load, before (no resolution) | 162 s |
| Full `dev_up` load, with resolution | 165 s |
| `default_resolve` alone on the loaded store | 13.2 s (13.8 s inside the load) |
| Live proposals, before → after | **11,098 → 10,524** (−574) |
| Resolver clusters / merged / refused | 416 / 409 / 7 (6 bridged only through an unloaded record, 1 filed for review) |
| Organisations merged | 0 |
| Virginia DEQ + ICIS-Air proposals | 719 → 581, as in §3.7 |

Merges by source pair. Each row is one `merged` event, with the absorbed record paired to the survivor's
own source:

| Pair | Merges |
|---|---|
| EIA-860M + ERCOT | 226 |
| ICIS-Air + Virginia DEQ (shared `icis_air:` id) | 138 |
| EIA-860M + EIA-860M (generators of one plant that match the same queue request) | 82 |
| EIA-860M + NYISO | 75 |
| EIA-860M + CAISO | 52 |
| EIA-860M + ICIS-Air | 1 (wrong, below) |

So 436 of the 574 were pre-existing overlaps that the dev store had shown twice, mostly EIA-860M
generators that are also ISO queue requests. NESO has none: it has no cross-source counterpart.

**Hand-check.** Ten merged pairs outside ICIS/VA were drawn at random (seed 20260929, from 666 merged edges):
7 correct, 1 likely correct (Gonzaga Wind Farm 76 MW / Gonzaga Ridge 58 MW, same Merced wind site),
2 uncertain, 0 clearly wrong. The uncertain two are "ROUGH HAT 2" (200 MW) against the 400 MW EIA plant
"Rough Hat", and "South Ripley BESS" (20 MW storage) against "South Ripley Solar" (270 MW).

**Wrong merges, found by targeted checks.** The resolver was not tuned in this lane; these are findings.
- A data centre merged with solar: "FRANKLIN PARK (CHI22) DATA CENTER" (ICIS, load) with "Franklin Park
  2-SLCHI802" (EIA, 1.2 MW solar, Prologis), Cook County. The evidence was name 66 and county 100. No
  technology-class guard exists.
- One side carries a phase number and the other does not. "BELLEFIELD SOLAR FARM" (CAISO 1510) merged
  into EIA plant "Bellefield 2" together with CAISO 1631 "BELLEFIELD 2". "BONANZA SOLAR" and
  "BONANZA SOLAR 2" (300 MW each) both merged into the one 300 MW EIA plant.
- A capacity score of 0 is not a veto. "Indigo Solar 3" (ERCOT, 800 MW) merged into the 150/180 MW EIA
  plant "Indigo Solar & Storage", in a 9-record cluster with six other Indigo requests.
- A town name plus a county: five NYISO "Riverhead" requests (7.5–100 MW, solar and storage) merged with
  a 4–5 MW EIA "Riverhead - CVE".
- A B2 county block at score 76.7: "DRACKER SOLAR" (built, 485 MW) merged into "Grace Energy Center".
- Transitive chaining: the resolver unions every accepted pair, and nothing checks that a cluster holds
  together as a whole. 66 of the 409 merged clusters hold two or more requests from one ISO queue, and
  they absorb 189 of the 574 records. In a random 10 of those 66, 8 were sound and 2 had one wrong
  member (Bellefield, Dracker). The sound ones were hybrid solar and storage requests of one EIA
  plant, and phases that add up to the plant.
- In dev only: 7 clusters join loaded records only through a record that is not loaded
  (`us.permits_dashboard`). `apply_cluster` refuses 6 of them. One, Bonanza, merged because it also had
  one direct edge.

Estimate: roughly 2–5 % of the 574 merges put a wrong record in a cluster, concentrated in the
multi-request clusters. This is in line with the store-path precision of 0.946 recorded on 2026-09-13
(**historical**: that is the 2026-09-13 resolver; just before lane H5 the same path measured 1.000 / 0.973,
docs/22 §22.2 "Before").
Production's `resolve_tick` already applies the same merges. Each merge is a reversible `merged` event.

**Current figures (docs/22 §22.2, measured by lane H5 on 2026-09-29 after its rules; re-run unchanged by
lane I3 the same day).** Threshold 75, labels `data/eval/labels.csv`:

| Path | Precision | Recall | tp / fp / fn / tn | Labels |
|---|---|---|---|---|
| Store, `python -m services.resolve.report` | 1.000 | 0.892 | 33 / 0 / 4 / 40 | 77 usable of 85 |
| Resolver, `python pipeline/resolve.py --sweep` | 0.974 | 0.925 | 37 / 1 / 3 / 44 | 85 |

Both are in-sample (A-22-H5-2). The store's 0 false positives come from 33 predicted merges, so they do not
rule out a false-positive rate of up to about 9 % (rule of three, 3/33). Out of sample, lane H5's rules
removed 39 wrong merges from the dev store and lost 11 correct ones, judged by hand (docs/22 §22.8). The
2–5 % estimate above describes the resolver before those rules.

**Also found.** `pipeline/resolve.py::eia_plant_rollup` selects `source_id == "eia860m"`, the short id
of the evaluation fixture. Frames with registry ids (`us.eia.860m`, the only kind the scheduler and dev
pass) never get plant-level rollup records.

#### Open

- The resolver's precision items listed above are for a resolver lane: a technology-class guard, an
  asymmetric phase token, a capacity-0 veto, a cluster-coherence check, and the registry-id rollup.
- `default_resolve` reads `Registry()` from `data/sources.yaml` and the store `SNAPSHOT_STORE` selects.
  It ignores `dev_up --sources-yaml`.

### 3.9 Refresh cost and resolver inputs (lane H8, 2026-09-29)

For the coordinator to merge after §3.8. It closes two items left open by lanes H1 (§3.7) and H5 (`docs/22` §22.10).

#### 3.9.1 An unchanged ECHO week now costs two requests and no body

**Before.** Every run of `us.epa.echo.icis_air` made two ranged GETs on the 70 MB ICIS-Air zip (64 KiB tail plus the
10.5 MB facilities member) and one GET of the whole 443 MB ECHO Exporter, whether or not EPA had published anything new.
`PoliteSession` read that body into memory whole. `requests` builds `.content` by joining its chunks, so the peak is
about twice the file. Measured locally on a 200 MB body: 410 MB peak RSS whole, 28 MB streamed. At 443 MB that is roughly
900 MB, against a 1 GB worker limit in `compose.prod.yml` and 512 MB in the base compose file.

**What the server honours.** Measured 2026-09-29 18:36 UTC with the platform User-Agent and robots.txt checked
(`/files/` allowed, `Crawl-delay: 10`, honoured at 0.1 rps). Five requests: robots.txt, two HEADs, and two conditional
GETs. Each conditional GET also carried `Range: bytes=-65536`, so a server that ignored the condition would have sent 64 KiB,
not the file.

| File | HEAD `Last-Modified` | HEAD `ETag` | `Content-Length` | Conditional GET (`If-None-Match` + `If-Modified-Since` + `Range`) |
|---|---|---|---|---|
| `echo_exporter.zip` | Sun, 27 Sep 2026 10:11:26 GMT | `"1a6e8db6-65c742c25691f"` | 443,452,854 | **304**, 0 bytes; the 304 repeats `ETag` and omits `Last-Modified` |
| `ICIS-AIR_downloads.zip` | Sun, 27 Sep 2026 10:23:36 GMT | `"42ff695-65c7457a64285"` | 70,252,181 | **304**, 0 bytes; same headers |

The server is `Apache/2.4.37 (Red Hat Enterprise Linux)`. Other headers: `Accept-Ranges: bytes`, `Cache-Control: private,
max-age=0, must-revalidate, max-age=604800`, and `Vary: Referer,Access-Control-Request-Headers`. The ETag is Apache's
size-mtime form with no inode, so it should agree across hosts behind one name. A HEAD comparison would work too, but it
costs a request more than a conditional GET when the file has changed, so the connector uses conditional GETs.

**What the connector does now** (`pipeline/connectors/us_epa_echo_icis_air/connector.py`):

- Both GETs carry `If-None-Match` and `If-Modified-Since` from the previous run record's `snapshot.meta.{icis,exporter}`,
  which now store `etag` next to `last_modified`. The ICIS tail GET keeps its `Range`, so a changed file costs no extra
  request.
- **Both 304.** `fetch` returns the stored snapshot's bytes verbatim. The runner's existing SHA-256 short-circuit (§3.2
  of `docs/20`) then records the run `unchanged`. No second path decides this. The record carries `http_status: 304` and
  `snapshot.meta.upstream: "unchanged"`, with each file's `not_modified: true` and `bytes_fetched: 0`. The previous
  validators and row totals are carried forward, because Apache's 304 omits `Last-Modified`. Cost: two requests plus
  robots.txt, no body.
- **ICIS 304, Exporter changed.** The stored candidate table is reused, and the Exporter is downloaded and filtered as
  before.
- **ICIS changed, Exporter 304.** The stored FRS rows are reused and filtered to the new candidates, but only when they
  already cover every registry id now wanted. They were cut for the previous candidates, so an id they lack might exist
  upstream. The rows are byte-identical to a full fetch, and a test pins that.
- **ICIS changed, with a new registry id.** The Exporter is downloaded unconditionally, as today.
- **Plumbing.** The runner hands the connector its `PreviousSnapshot` before `fetch()`
  (`pipeline/connectors/base.py`). That is the run record `Store.last_snapshot_sha` already names, plus a loader for its
  bytes (`Store.last_snapshot`), with the SHA-256 checked on read. A snapshot made by other fetch code (`FETCH_VERSION`,
  which the lane H1 runs lack) is never reused. Neither is one without both files' validators, or one whose object is
  gone; each of those costs one full fetch.
- **Streaming.** The Exporter is streamed to a temporary file in 1 MiB pieces (`PoliteSession.get(..., stream=True)`).
  A streamed response is scanned for a challenge only when it is labelled HTML, and `cf-mitigated` is checked either way.
  A retried streamed response is closed first. A short body against `Content-Length` fails the run.

Tests with fakes, no network: `pipeline/connectors/us_epa_echo_icis_air/test_connector.py` runs the real runner against
`FakeEcho` (eight cases), and `tests/test_connector_http.py` covers streaming (three cases).

#### 3.9.2 The resolver sees only sources the loader loads

`infra/scheduler/jobs.py::_latest_proposal_frames` now asks `services.ingest.loader.load_refusal(entry)`. That is the
loader's own refusal rule, extracted from `upsert_licence_and_source` so that one definition serves both: a gated reuse
class, `publication: none`, a reuse class outside the posture's publishable set, or an unknown `publication` value.
A refused source contributes no frame to `pipeline.resolve.run`. `infra/scheduler/test_loop.py` pins it: a
`reuse: restricted` source and a `publication: none` source are both excluded, and the old code let the second through.

**Correction to `docs/22` §22.10.** Gated rows did not reach the resolver through this path. `registry.status()` already
marks a `GATED_REUSE` source `gated`, not `implemented`, and PJM has no connector. The gap was narrower. A source with an
open reuse class and `publication: none`, or a reuse value outside both sets, would have passed. No implemented connector
is in that state today.

**Measured.** `default_resolve` ran on fresh copies of the H4 pre-resolution dev store (`h4/dev_before.db`, 11,098 live
proposals), with the H4 data root, before and after:

| | Before (7f9427e) | After |
|---|---|---|
| Frames (rows) | 8 sources, 11,202 | 8 sources, 11,202 |
| Proposal clusters | 407 | 407 |
| Proposals merged | 531 | 531 (the same 531 pairs) |
| Decisions sent to review | 22 | 22 |
| Live proposals after | 10,567 | 10,567 |
| Time | 14.5 s | 13.6 s (noise) |

The delta is zero, for the reason above. `us.permits_dashboard` (104 rows) stays in the frames. Its terms load
(`open`, `raw_ok`), and production's scheduler loads it. Only the dev store leaves it out, and rule L (`docs/22` §22)
already keeps an unloaded record from bridging clusters.

#### 3.9.3 Assumptions and open items

- **A-25-H8-1.** echo.epa.gov keeps answering conditional GETs as measured. If it stops, the connector falls back to a
  full fetch every run, which is correct but costly. The `bytes_fetched` field in each run's meta shows which happened.
- **Open.** The first production run after this change fetches everything once, because the lane H1 records carry no
  `fetch_version` or `etag`.
- **Open.** The streamed Exporter needs about 443 MB of temporary disk in the worker, once a week when it changes. No
  compose volume limits `/tmp` today.

## 4. Fiber availability by area

Owner decision 2026-09-28: add **fiber availability by area**, not fiber routes. No open, current,
route-level fiber dataset exists, and precise routes are security-sensitive, so no route geometry is
fetched, stored or derived anywhere in this layer. This section records the source, what its terms
say (and what could not be read), what the metric means, and how it should be served.

Code: `pipeline/context/fcc_bdc.py` (fetch, parse, county metrics, join coverage, CLI),
`services/ingest/fiber_availability.py` (load contract and gate), tests beside each, fixture
`pipeline/context/fixtures/fcc_bdc_fixed_summary_sample.zip`. Manifest id `us.fcc.bdc.fixed_summary`.

### 4.1 Source

The FCC Broadband Data Collection (BDC), published on the National Broadband Map
(https://broadbandmap.fcc.gov/data-download/nationwide-data). Providers file twice a year where they
make mass-market broadband available "as of" 30 June and 31 December (FCC public notice DA 25-1080,
https://docs.fcc.gov/public/attachments/DA-25-1080A1.pdf, read 2026-09-28: the eighth window covers
"broadband availability and other data as of December 31, 2025").

Beside the location-level files, the FCC publishes pre-aggregated **Summary by Geography Type**
files. The national one, `bdc_us_fixed_broadband_summary_by_geography_<J|D><yy>_<ddmonyyyy>.csv`
(zipped), has one row per geography x area type x residential/business x technology group:

| Column | Values seen |
|---|---|
| `area_data_type` | Total, Urban, Rural, Tribal, Nontribal |
| `geography_type` | National, State, County, CBSA (MSA), Congressional District, Tribal |
| `geography_id`, `geography_desc`, `geography_desc_full` | county FIPS, "Autauga County", "Autauga County, AL" |
| `total_units` | units in broadband-serviceable locations (BSLs) in the geography |
| `biz_res` | R (residential), B (business) |
| `technology` | Any Technology, All Wired, Any Terrestrial, All Wired and Licensed Fixed Wireless, Copper, Cable/Fiber, Cable, **Fiber**, All Satellite, GSO Satellite, NGSO Satellite, All Fixed Wireless, Unlicensed Fixed Wireless, Licensed Fixed Wireless, Other |
| `speed_02_02`, `speed_10_1`, `speed_25_3`, `speed_100_20`, `speed_250_25`, `speed_1000_100` | share (0-1) of `total_units` with that technology group at or above that download/upload tier |

Evidence for that layout is third-party, because the FCC's own specification
(`https://www.fcc.gov/sites/default/files/bdc-data-downloads-output.pdf`) answered 403 from this
egress: public repositories that committed copies or read the file (a J24 county extract with this
exact 14-column header; a DC place file with all 15 technology groups; a notebook reading
`bdc_us_fixed_broadband_summary_by_geography_J25_31mar2026.zip` that reports 616,080 rows, 364,890 of
them County, 6,464 Total-area rows per technology = 3,232 counties x R/B). File names with suffixes
`J25_03feb2026`, `J25_31mar2026` and `D25_15sep2026` show that one filing is re-issued with revisions.
The FCC's **H3 hexagon** view is not a published summary download as far as could be established:
Esri's Living Atlas composite has an H3 resolution-8 sublayer, but that is Esri's product (4.2).
`fetch` records every download subcategory the API lists, so the first authenticated run settles it.

**Chosen over the alternatives.** The location-level availability files (per state, per technology;
tens of millions of location x provider rows nationally) would allow a distinct-provider count but
cost a multi-gigabyte download per filing for a metric the summary already gives; they also sit on
the CostQuest-licensed Fabric's location ids. Esri's Living Atlas feature service
(`FCC_Broadband_Data_Collection_December_2024_View`, item `e1343efcefc344709057260ee57290a0`, titled
"FCC Broadband Data Collection December 2025 (Latest)") is open to anonymous queries but is licensed
under Esri's terms, not the FCC's, so it is not used.

### 4.2 Terms and access, quoted

Retrieved 2026-09-28 from this egress, with the project User-Agent
(`pipeline/connectors/http.py::user_agent()`; no personal email in any header):

- Every page on `broadbandmap.fcc.gov` and `www.fcc.gov`, **including `robots.txt`**, the data
  download page, the specification PDFs and the map's own `/nbm/map/api/*` endpoints:
  `HTTP 403`, `<TITLE>Access Denied</TITLE>` "You don't have permission to access
  "http://broadbandmap.fcc.gov/robots.txt" on this server." (Akamai edge). A plain `curl` User-Agent
  gets the same answer, so this is an egress-level block, not a UA filter; nothing was retried or
  routed around. `robots.txt` therefore cannot be read; `PoliteSession` treats an unreadable
  robots file as permissive, and the connector calls only API endpoints (`honour_robots=False`, the
  repo convention for APIs).
- Public Data API, `https://broadbandmap.fcc.gov/api/public/map/listAsOfDates` and
  `.../downloads/listAvailabilityData/2025-06-30`: `HTTP 401`,
  `{"status":"fail","status_code":401,"message":"Unauthorized"}` (nginx/Express). Reachable, and
  token-gated.
- FCC public notice DA 25-1080 (docs.fcc.gov, HTTP 200), on the Fabric that underlies
  `total_units`: "Entities that have not yet entered into a license agreement with CostQuest for
  Fabric data (including broadband service providers, state, local, or Tribal governmental entities,
  or other entities wishing to use the Fabric data for purposes of participating in the BDC or
  non-commercial academic/public policy broadband research) may do so by following the instructions
  for obtaining access to the Fabric in the BDC Help Center." Location-level Fabric data is licensed;
  this layer never fetches it, only the FCC's published county aggregates.
- Esri item metadata (`hub.arcgis.com/api/v3/datasets/e1343efcefc344709057260ee57290a0`, HTTP 200):
  `licenseInfo` "This work is licensed under the Esri Master License Agreement". Recorded to explain
  why that copy is not used.

**Not retrieved:** the National Broadband Map terms of use, the data-download specification, the
Public Data API specification (`https://www.fcc.gov/sites/default/files/bdc-public-data-api-spec.pdf`),
and the text the FCC shows before issuing a token. Secondary sources (web search summaries of the API
spec and of third-party client code) agree on the mechanism: sign in, open "Manage API Access",
agree to the FCC's disclaimer ("I Agree"), generate a token; every call sends headers `username`
(the account's registration email) and `hash_value` (the token).

**Classification.** The summaries are FCC-compiled aggregates of provider filings, a work of the
United States Government (17 U.S.C. §105): `public-domain`, manifest `reuse: open`. Publication is
held at **`derived_only`** until the token clickwrap is read: the county metrics publish with the
credit "Source: FCC National Broadband Map, Broadband Data Collection (December 2025)" and "not
endorsed by the FCC"; the source cells (`raw`) stay in the parquet; every county links out to its
FCC area-summary page. `docs/13` §6 carries the row; `scripts/check_manifest_licences.py` passes.
Confidence: moderate (the §105 reading is strong; the clickwrap is unread).

**What the owner must obtain** before a live run:

1. An FCC user account registered with a **role address** (for example a shared data mailbox), not
   a person's: the address is sent as the `username` header on every request.
2. Sign in on broadbandmap.fcc.gov, "Manage API Access", **read the terms shown before "I Agree" and
   send them to the register** (docs/13 §6 row `us.fcc.bdc.fixed_summary`; if they permit it,
   publication can move to `raw_ok`), then generate the token.
3. Store `FCC_BDC_USERNAME` and `FCC_BDC_API_TOKEN` in the environment's secret store. The code reads
   only those two variables and never writes them to a run record (tested).
4. The run itself must go out from an egress the FCC's edge does not block for the downloads; the API
   host answered 401 (not 403) from here, so the three API calls should work as they are.

### 4.3 Metric, and what it means

One row per county (`county_fips`, five digits, zero-padded), read from the `Total` area rows:

| Column | Definition |
|---|---|
| `fiber_share` | `speed_02_02` on the Total / R / `Fiber` row: the share of units in BSLs where at least one provider reports residential fiber-to-the-premises service (BDC technology code 50) at any speed. The headline metric. |
| `fiber_share_business` | the same on the B (business) row |
| `fiber_gigabit_share` | `speed_1000_100` on the Total / R / `Fiber` row: fiber at 1000/100 Mbps or better |
| `served_100_20_share` | `speed_100_20` on Total / R / `Any Technology`: the FCC's "served" benchmark from any technology, for context |
| `bsl_units` | `total_units`: units (homes, apartments, business units) in BSLs, the FCC's denominator |
| `rural_bsl_units`, `rural_fiber_share` | the same two from the `Rural` area rows, where the county has any |
| `vintage`, `vintage_label`, `as_of_date` | the filing: `2025-12`, "December 2025", `2025-12-31` |
| `file_name`, `file_revision` | the FCC file read, and its revision date (`2026-09-15`), which is not the vintage |
| `fcc_area_url` | `https://broadbandmap.fcc.gov/area-summary/fixed?type=county&geoid=<fips>` (link-out) |
| `source_id`, `source_url`, `retrieved_at`, `licence`, `licence_id` | provenance; `source_url` is the nationwide-data page for the filing (`?version=dec2025`), the token-gated API URL stays in the run record |
| `raw` | JSON of the FCC cells each number was read from (not published) |

Rules: `Cable/Fiber` is a union group and is never read as fiber (tested); a county whose file has no
`Fiber` row gets `fiber_share = null`, not 0; shares outside [0, 1], renamed columns, duplicate keys
or a file name that names no filing all fail the parse.

**What it means.** Availability *to premises*: a provider has told the FCC it has a fiber subscriber
at, or could connect within 10 days at a standard installation charge, that share of the
county's homes and businesses. As a signal of "is there fiber in this area", it is the best open,
current, national measure.

**What it does not mean.**

- Not backbone, middle-mile or long-haul routes, dark fibre, route diversity, capacity, or the
  distance from any site to a fiber line or carrier hotel. A county at 0.9 can be far from long-haul
  fiber; a county at 0.1 can have a long-haul route crossing it with no local service.
- Not what a large load could buy: enterprise, wavelength and dark-fibre services are outside the BDC
  (it covers mass-market service).
- Provider-reported availability, subject to challenge and revision; the FCC revises a filing in place
  (two revision dates of the J25 file were seen, 03feb2026 and 31mar2026).
- **Units, not locations.** The summaries count units in BSLs (an apartment building is one location
  with many units). The task asked for locations; the FCC summaries publish units only, so the column
  is named `bsl_units` rather than pretending otherwise.
- **Denominator of the R and B shares is not settled.** In every copy seen, `total_units` is the same
  on the R and B rows of a county, so whether each share is over all units or over residential /
  business units only is defined in the spec PDF that could not be read. For that reason no
  "fiber units" count is derived (share x units) until the spec is read (open item 2).
- No per-county provider count: the geography summaries publish none (below).

**Distinct fiber providers per county: not built.** The provider summary
(`bdc_us_provider_summary_by_geography_*`) is read by third-party code as per provider x geography
with `data_type` ("Fixed Broadband") and `res_st_pct`, and no technology column appears in that use;
if its header does carry a technology field, a fiber-provider count per county is a cheap addition on
the first authenticated run (open item 3). Otherwise it needs the location-level FTTP files, not
measured here: order of magnitude, one row per location x provider with fiber, which is tens of
millions of rows per filing.

### 4.4 Vintage

From the file name, which is the FCC's own label: `J<yy>` = as of 30 June, `D<yy>` = as of 31
December; `D25_15sep2026` -> vintage `2025-12`, "December 2025", revision 2026-09-15. When fetched
live the API's `as_of_date` must agree with the name or the run fails. The loader records it on
`source` as `vintage = 2025-12`, `vintage_basis = artefact_filename` (`services/ingest/vintage.py`);
the fetch date never stands in for it. Latest filing at 2026-09-28, per the file names above:
**December 2025**.

### 4.5 Measured

**No real FCC file was read.** No credentials exist, and the FCC's pages are blocked from this
egress; no numbers below are FCC data. What was measured:

| Run | Input | Result |
|---|---|---|
| live, no credentials | `python -m pipeline.context.fcc_bdc` | refused before any request: `CredentialsMissing: FCC_BDC_USERNAME, FCC_BDC_API_TOKEN not set ...` |
| fixture | 264-row synthetic zip in the real layout (2,537 bytes) | 8 counties, 1 without a Fiber row, parquet 24,658 bytes, 0.37-0.47 s; join 7 of 8 matched, `60010` (American Samoa) has no polygon |
| cost | synthetic file at the J25 national size: 616,080 rows, 84.9 MB CSV, 17.9 MB zip | parse 2.0-2.35 s, whole run 2.5-3.1 s, peak RSS 386 MB, parquet 1.55 MB for 3,201 counties (most of it `raw`) |

So reading the published summary is seconds per filing; there is no case for aggregating location
files for this metric.

**Coverage against the map's counties** (`pipeline/context/regions.py` ->
`data/vendored/regions/us_counties.geojson`, Census 2024 1:20m, 3,222 counties: 50 states, DC, Puerto
Rico). Real coverage is not measured. Expected from the third-party J25 count (3,232 county
geographies): the territories outside the 1:20m file (American Samoa 5, Guam 1, Northern Mariana
Islands 4, US Virgin Islands 3) will be FCC rows without a polygon; and if the FCC keys Connecticut by
the eight pre-2022 counties while the vendored file has the nine planning regions (`09110`-`09190`),
Connecticut will not join at all (8 FCC rows unmatched, 9 polygons empty). `run()` reports both lists
(`coverage.fcc_without_polygon`, `coverage.polygons_without_fcc_row`) on every run, plus the
unit-weighted national fiber share and the top and bottom 10 counties by `fiber_share` with
`bsl_units` (`ranked`), so the first authenticated run produces the tables this section still lacks.
Top/bottom 10: **pending the first authenticated run**.

### 4.6 Serving proposal (for the coordinator; no migration or route in this lane)

**Storage: a typed table, not region attributes.** Region polygons are vendored GeoJSON files
served from a process cache (`services/api/regions.py`), not database rows, so "attributes on the
region structure" would mean either baking FCC numbers into `us_counties.geojson` (couples a
twice-yearly, separately licensed dataset to a geometry file regenerated on its own schedule, and
loses per-row provenance) or a generic key-value table. Proposed instead, after 0026:

`area_fiber_availability` — `id`; `source_id` (FK `source`); `level` ('county'; 'state' later from
the same file); `region_id` (text, the vendored `region_id`, not an FK, since regions are files);
`vintage` ('2025-12'); `as_of_date`; `bsl_units`, `fiber_share`, `fiber_share_business`,
`fiber_gigabit_share`, `served_100_20_share`, `rural_bsl_units`, `rural_fiber_share` (numeric, CHECK
0-1 on shares); `fcc_area_url`; `source_url`, `retrieved_at`, `licence_id` (FK `licence`); `file_name`;
unique (`source_id`, `level`, `region_id`, `vintage`). Filings accumulate (about 6,500 rows a year),
so "latest" is `max(vintage)` and a trend is a join. `raw` is not stored (publication `derived_only`).
The loader writes it from `CountyFiberBatch.rows` (`services/ingest/fiber_availability.py`), which
already carries exactly these fields; `load_county_fiber` already registers the source and vintage.
A generic `region_metric` (long form) table only pays off with a second area metric; none exists.

**API.** `GET /v1/context/fiber-availability?level=county&state=US-TX&vintage=latest` (next to
`/v1/context/plants/geo`): list envelope with `meta.vintage`, `meta.as_of_date`, the licence summary
and the attribution string; each item `{region_id, name, state_code, fiber_share,
fiber_share_business, fiber_gigabit_share, bsl_units, rural_fiber_share, fcc_area_url}`. A compact
form for the map (`?format=map`: `{region_id: fiber_share}` for all 3.2k counties, about 60 KB)
avoids resending geometry: the client already holds the county polygons from `GET /v1/geo/regions`
and joins on `region_id`. `GET /v1/context/fiber-availability/{region_id}` for a county panel,
optionally with every filing for a trend line. Same on every tier (nothing time-delayed); the
publication gate is `derived_only`, so `raw` is never served.

**Map.** A county choropleth layer, off by default, "Fiber to the premises (share of homes and
businesses)": fixed breaks 0-0.2-0.4-0.6-0.8-1.0 rather than quantiles, so a filing-to-filing change
reads as a change; counties with no FCC row hatched grey with "no FCC data" (never coloured as 0);
tooltip with share, units, filing and the FCC link; the legend carries the sentence "Availability to
premises reported by providers. Not fiber routes or capacity." Rendered only at county zooms; at
national zoom a state summary from the same file keeps the layer light. The layer belongs to the map
lane (G2 owns `map.js`).

### 4.7 Open items

1. Owner: create the role-address FCC account, read and record the token clickwrap in docs/13, set
   the two secrets (4.2). Then run `python -m pipeline.context.fcc_bdc` and fill 4.5 (coverage, top
   and bottom 10, national share) and the manifest `verified` note.
2. Read `bdc-data-downloads-output.pdf` (browser task; 403 here) for the exact denominator of the R
   and B shares and whether `total_units` includes both; only then derive fiber unit counts.
3. On the first authenticated run, read the provider summary header: if it has a technology field,
   add `fiber_provider_count`.
4. Connecticut: if the FCC keys the 2020 counties, add a county-to-planning-region crosswalk or
   serve Connecticut at state level; decide after the first run shows which.
5. Territories without a 1:20m polygon (AS, GU, MP, VI): either vendor their polygons from the
   Census 500k file or leave them table-only.
6. The FCC API's own rate limit is in the unread spec; the connector uses one request per five
   seconds (three requests per run).

## Assumptions

- A queue's point-of-interconnection text names a substation or bus, sometimes with a bus number and voltage
  ("59903 Bearkat 345kV"), sometimes a line tap ("Cortland - Fenner 115kV"). A line tap has no single
  substation; it is kept as text and not placed.
- A point of interconnection is where a project connects, not where it is built. Placing a proposal at its
  substation would be wrong; the point is its own record, linked from the proposal (the same rule
  `docs/21` §3.7 applies to NESO connection sites).
