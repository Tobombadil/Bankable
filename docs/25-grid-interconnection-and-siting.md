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
5. **Floodplain (lane flood, 2026-10-22).** FEMA National Flood Hazard Layer Special Flood Hazard Areas near
   69 kV-and-up lines, and the footprint of FEMA's digital maps, for the powered-land site finder (§5).
6. **Prospecting layers (resources lane, 2026-10-22).** Solar irradiance, wind speed, oil and gas basins, plays and
   reserves, IRA energy communities, protected lands, water stress and seismic hazard, sampled at a point and
   along a corridor for the site finder; screening grade only (section 5).
7. **Site finder (lane site-finder, 2026-10-23).** Corridor bands of land beside 69 kV-and-up lines, near a
   substation and out of the floodplain, with what lies within 5 miles: the candidates job, `GET /v1/site-finder`
   and the `/site-finder` page (section 7).

**Out of scope.** Internet exchanges and colocation facilities (PeeringDB's AUP forbids commercial application
and bulk redistribution: https://www.peeringdb.com/aup). Anything from the private aggregators named in
CLAUDE.md. OpenStreetMap substations and lines were out of scope (ODbL share-alike) until the owner's
2026-10-22 decisions brought them in for the powered-land site finder: §2.7.

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

**Changed 2026-10-22 (§2.7).** The owner's powered-land site finder needs the whole network and substations, so
both now come from OpenStreetMap (`us.osm.power`, ODbL 1.0): lines of 69 kV and up and substations, built
nationally in GitHub Actions and pulled by the server. The LBNL layer stays loaded for its FERC owner data; the
map draws OpenStreetMap's lines wherever an OpenStreetMap extract covers the line's states (§2.7, "Which layer is
shown where"). What §2.1–§2.6 say about DHS's restricted substation layer still holds: it is never used, nor is
anything derived from it, and the LBNL endpoint names are still not assembled into substation points.

### 2.1 Sources and terms (read 2026-09-28, platform user-agent, robots.txt first)

| Candidate | What it says | Route | Decision |
|---|---|---|---|
| EIA US Energy Atlas | Publishes neither layer (Hub catalogue 76 items; EIA ArcGIS org's 79 services; `www.eia.gov/maps/map_data/` guesses all 404) | — | nothing to use |
| DHS HIFLD, transmission lines | data.gov DCAT `MGMT-GMO-HIFLD-847169`: `"license": "https://www.usa.gov/government-works"`, `"accessLevel": "public"`; item use limitation "None (Public Use)" | Canonical item and GeoPlatform service withdrawn; Data Rescue Project archive → DataLumos project 240591, **Cloudflare challenge, not bypassed**, DataLumos terms unread; HSDL's HIFLD aggregation has no line layer; `web.archive.org` resets through this egress | terms clear, route blocked: recorded `open`/`raw_ok`, `verified: blocked` |
| Third-party ArcGIS copies of HIFLD lines | Esri "(Archive)" copy: "licensed under the Esri Master License Agreement"; GeoPlatform user upload and HARC copy: no licence, no provenance | — | never used |
| DHS HIFLD, substations | data.gov DCAT `MGMT-GMO-HIFLD-546955`: `"accessLevel": "restricted public"`; DHS GMO: HIFLD Secure holds "commercially licensed and FOUO … data … approved Data Use Agreement" | withdrawn from HIFLD Open; no archive (DRP sitemap, HSDL group checked) | `restricted`/`none`; **do not ingest, do not derive** |
| NOAA OCM "Electric Power Substations" (coastal, 2017) | data.gov: CC0; InPort: "Use Constraints: For coastal and ocean planning"; lineage: the HIFLD substations layer | zip not fetched | `unknown`/`none` (conflicting terms, restricted lineage) |
| LBNL, "A Harmonized Geospatial Dataset of U.S. Transmission Lines: Linking FERC Form 1 and HIFLD, 1994-2024" (OEDI 8742) | dataset page links "License" to CC BY 4.0; LBNL's own DCAT: `"license": "https://creativecommons.org/licenses/by/4.0/"` | `data.openei.org/files/8742/…csv`, fetched after three HTTP 429s (one request a minute gets through); OpenEI general disclaimer 404 (browser task) | **used**: `attribution`/`raw_ok`, credit Yin, Nait Belaid & Heleno (2026), LBNL, OEDI, CC BY 4.0 |
| OpenStreetMap power features via Geofabrik's US state extracts (added 2026-10-22, §2.7) | openstreetmap.org/copyright: ODbL, "credit OpenStreetMap and its contributors", "you may distribute the result only under the same license"; Geofabrik: "Derived databases have to retain the same license" (quoted in `data/sources.yaml` `us.osm.power`) | `download.geofabrik.de/north-america/us/<state>-latest.osm.pbf`, built in GitHub Actions; the server pulls the published GeoParquet | **used** (owner, "Public now"): `attribution`/`raw_ok`, credit "© OpenStreetMap contributors", share-alike offer of the derived database |

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
   *Answered 2026-10-22:* OpenStreetMap, §2.7.
3. **Owner links** need a reviewed alias table from LBNL's 259 owner strings to existing organisations (73 already
   key-match); then `owner_name` can be filled and the generic edge loader used.
4. OpenEI's general disclaimer (incl. "Generative AI Terms and Conditions") answered 404 — browser task.
5. `source_vintage` for this source is unset (no vocabulary for a dataset-page modified date).
6. The national line payload (1.8 MB for 1,500 lines) argues for the vector-tile path ADR 0008 deferred.

### 2.7 The OpenStreetMap network: lines of 69 kV and up, and substations (lane grid-osm, 2026-10-22)

Owner decisions 2026-10-22 (`docs/00-PLAN.md`): the powered-land site finder (within 1 mile of a 69 kV-or-higher
line, within 5 miles of a substation, outside the FEMA floodplain; all three adjustable), and substations from
OpenStreetMap, public now. Source row `us.osm.power` (`data/sources.yaml`, ODbL quoted), register row
`docs/13` §6. Code: `pipeline/context/osm_power.py` (build, pull, normalise), `services/ingest/osm_power.py`
(load, withdraw), `infra/scripts/build_grid_layers.sh` and `.github/workflows/grid-layers.yml` (the national
build), `services/api/assets.py` (map, page, nearby grid).

#### What is kept

- **Lines**: ways tagged `power=line` or `power=cable` whose highest stated voltage is 69 kV or more, and every
  such way with no usable voltage (`voltage_unknown`, kept so the finder can include them on request). Cables
  are underground or submarine by definition and carry `underground: true`, unless an explicit `location` says
  otherwise: `location=overhead` or `overground` (an aerial cable) is not underground, and `underground` or
  `underwater` on a `power=line` is. `line=busbar` and `line=bay` are a
  substation's own conductors and are dropped. `voltage` is parsed as OpenStreetMap writes it: volts, `;` between
  circuits (`138000;69000` -> highest 138 kV, `voltages_kv` [138, 69]), `kV` strings (`345 kV`, `138kV`). Every
  stated value is kept, a repeated one too: `138000;138000` is two 138 kV circuits, `voltages_kv` [138, 138] (fix
  round 2: the first version deduplicated it to [138]; 1,217 Texas ways repeat a value). A comma
  is read by what surrounds it: a list separator when four or more digits follow it or the number before it is
  already 1,000 or more (`12470,69000` -> [69, 12.47], so the 69 kV circuit is kept), a thousands mark when
  exactly three digits follow a one-to-three-digit number (`138,000` -> 138 kV), else a decimal mark (`12,47 kV`,
  `13,8`). A bare
  number under 1,000 is read as kilovolts, and flagged `voltage_assumed_kv`, only when it is a US transmission
  nominal (69, 115, 138, 161, 230, 287, 345, 500, 765) or has a decimal part (`34.5`); any other bare integer is
  volts. The Texas run showed why: every bare integer there (`240`, `208`, `480`, 29 ways) is a service drop
  tagged `power=line`, and read as kilovolts they had passed as 230 kV- and 500 kV-class lines. `0` and words
  (`medium`) are no voltage. Kept tags: `operator`, `name`, `ref`, `circuits`, `cables`, `frequency`, `location`,
  `line` and the raw `voltage`.
- **Substations**: `power=substation` nodes (the point) and areas (closed ways and multipolygon relations: the
  area-weighted centroid is the point, the outline rides in `attributes.outline` as GeoJSON), with `voltage`,
  `name`, `operator`, `ref` and the `substation=*` subtype. `traction` and `minor_distribution` are dropped;
  every other subtype (transmission, distribution, converter, generation, switching, industrial, ...) and
  untagged ones are kept with the subtype recorded. No voltage floor: a 138/12.47 kV distribution substation is
  what a large load connects to. A substation mapped twice, as a closed way and as the multipolygon relation
  whose only outer is that way, is one substation: the relation is kept, takes any tag only the way states (old
  multipolygons carry the name on the outer way), and the way is dropped, so nearby-grid and the subtype counts
  see it once.
- **Never**: DHS's restricted HIFLD substation layer or anything derived from it; OpenInfraMap (a renderer of the
  same OpenStreetMap data, never scraped); Geofabrik's paid "Power Networks" product.

#### Build, publish, pull

1. **Build** (GitHub Actions, monthly on the 2nd, `grid-layers.yml`): for each of the 52 Geofabrik US extracts
   (50 states, DC, Puerto Rico) in turn: download, filter to the power features and everything they reference
   with pyosmium (`BackReferenceWriter`, the equivalent of `osmium tags-filter`), delete the raw extract, read the
   filtered file with node locations and area assembly. The runner's disk holds one state at a time. Sequential
   downloads, five seconds apart, robots.txt honoured. Each state is checked against Geofabrik's `<file>.md5`
   (read before the file, so a file replaced between the two requests fails the check) and against its
   Content-Length; a transfer reset part-way, cut short or not matching its checksum is downloaded again for that
   state alone, three attempts 30 s and 120 s apart, the partial file deleted each time (fix round 2: one reset in
   state 40 of 52 used to abort the whole run, and a short file was caught only when pyosmium failed to read it).
   A refusal (robots.txt, a challenge page, HTTP 404) is not retried. An element in two extracts (a way across a state line, or
   one that Geofabrik's slightly generous state polygons both hold) is kept once, from the more complete copy
   (more resolved vertices; an assembled outline), with every extract that held it recorded.

   **Regions: one rule** (fix round, 2026-10-22). The first version gave a border way to whichever state came
   first in the alphabetical extract table (Arkansas, Louisiana, New Mexico and Oklahoma all precede Texas), so a
   `GRID_LAYERS_REGIONS=US-TX` server lost every way across the Red River, the Sabine and the New Mexico line,
   while still hiding LBNL's Texas lines. Now each row carries `region` (the vendored state polygon at the line's
   representative point or the substation's point, else the extract that held it: this is `state_code`),
   `states_crossed` (every state a vertex falls in, plus `region`), `extract_regions` (every extract that held
   it) and `scope_regions` (the last two together). A load of states *W* holds exactly the rows whose
   `scope_regions` meet *W*; the loader withdraws only rows whose `scope_regions` meet *W*; the manifest's
   per-state `lines_kept` and `substations_kept` count rows the same way, so the promotion gate compares what a
   one-state server would get. Including `extract_regions` matters because the vendored polygons are coarse
   (Census 1:20m; Texas has 551 vertices, and the Red River and the Sabine meander inside that tolerance): a way
   wholly inside Texas that the polygon misplaces across the river is still in Texas's scope, because the Texas
   extract holds it. The cost is a few lines just outside the state in a one-state load, which is harmless (they
   are real lines, drawn and listed as such).
2. **Publish**: `lines.parquet` and `substations.parquet` (GeoParquet 1.1, WKB, zstd; OpenStreetMap's own
   seven-decimal coordinates), `manifest.json` (per-state data timestamps, kept and dropped counts by rule, sha256
   of each file) and `README.txt` (the ODbL notice) go to the tiles bucket under
   `grid/osm-power/v1/<data timestamp>-<build time>/`, a key never overwritten. The version is promoted to
   `grid/osm-power/latest.json` only when the gate passes (`osm_power check`): no state the published build has
   may be missing, no state's kept lines or substations may fall by more than 10% and at least 50 rows (the floor
   keeps a handful of ordinary edits in DC or Rhode Island from blocking the national promotion), and none may fall
   to zero. A build of a subset of states is uploaded and never promoted.
3. **Pull** (the server's monthly `context_build`): `python -m pipeline.context.osm_power pull` reads
   `latest.json` from `$GRID_LAYERS_URL`, else `https://tiles.$BANKABLE_DOMAIN/grid/osm-power`, fetches the two
   files it names, refuses any whose sha256 differs from the manifest, and writes the asset frames
   `normalized/context/us.osm.power.{lines,substations}.parquet` plus the manifest. A version already held is not
   fetched again. `GRID_LAYERS_REGIONS` (`pull --regions`, ISO codes such as `US-TX`, or `all` for every state)
   **must be set**: the pull refuses to run without it, because read as national it would put the line index the
   beta server cannot hold (limit 3) behind a missing environment variable. It keeps the rows in those states'
   scope (the region rule above), filtered in pyarrow batch by batch so a one-state pull never holds the national
   table as Python objects, and narrows the manifest copied beside them, so the loader withdraws nothing outside
   that scope: the lever for a server that cannot hold the national line index yet. Narrowing it later (from
   `US-TX,US-OK` to `US-TX`, say) removes the dropped states' rows at the next load (step 4), so they neither go
   stale nor keep counting as covered in the map's source rule. The two frames and the manifest copy are written
   as temporary files and renamed into place, the manifest last, all three stamped with one `frame_set` token; a
   crash part-way leaves a mix the loader refuses (step 4) rather than new frames paired with the previous load's
   covered regions. **Wired 2026-10-23** (lane sf-golive, limit 1 below): the pull is a `context_build` builder,
   so `context_load` and then `site_finder_refresh` follow it. Before the first national build is promoted,
   `latest.json` answers 404 and the pull reports `status: not_published` (exit 0), writes nothing and leaves the
   frames the server holds; any other answer is still an error.
4. **Load** (`context_load`, `services/ingest/osm_power.py`): every row through `load_assets`, so each carries
   `source_id` `us.osm.power`, `source_url` the element's openstreetmap.org page (linked, never fetched: robots.txt
   disallows `/way/`, `/node/`, `/relation/` to crawlers), `retrieved_at` the extract's replication timestamp and
   the licence; `source_asset_id` `way/<id>` etc., so a reload inserts nothing. An element in a covered state's
   scope that the new build no longer has is **withdrawn** (its row deleted, owner and source links with it, an
   interconnection point that named it unlinked), never one wholly outside the states the load covers, and never
   more than 10% of a state's rows in one load when that is 50 rows or more (above that the state is held, with
   every missing row in its scope, and logged for a person). The share is of the rows the state held before the
   load, read before the upsert, so a load's own inserts cannot dilute it. When the manifest says the load was
   narrowed to named states (`regions_loaded`), rows in none of their scopes are removed as well, outside the churn
   guard because that is a configuration change, with the count per state in the report; nothing is removed
   without a manifest, after an unnarrowed load, or for a requested state the build lacks (`regions_missing`).
   Frames and manifest whose `frame_set` tokens differ load and withdraw nothing, and the store keeps what it
   holds until the next pull writes a whole set. The licence row is marked
   `share_alike`. Operators stay text (`attributes.operator`): linking free-text operator strings would mint
   duplicate utilities, as LBNL's owner strings would have (§2.2).

The asset row's line is Douglas-Peucker simplified at about 5 m (`ASSET_SIMPLIFY_DEG`) and stored at six
decimals; the published GeoParquet keeps every vertex. Towers along a straight run carry nothing the map or the
finder uses, and the line index `/v1/assets/geo` holds in memory grows with vertex count (measured below).

#### Which layer is shown where

| Surface | Lines it shows |
|---|---|
| Map, `GET /v1/assets/geo` (no `source`) | OpenStreetMap's lines; an LBNL line only where OpenStreetMap does not cover every state it crosses (coverage = the states the loaded build covers, the union of its rows' `attributes.coverage_regions`, which is each row's scope narrowed to the states loaded: a US-TX load covers Texas only, even though it holds the ways that cross into Oklahoma). With the national build loaded that is nowhere; with Texas alone (the beta) LBNL still draws the other 48 states and the LBNL lines that cross the Texas border, while OpenStreetMap draws every line in Texas's scope. |
| `GET /v1/assets/geo?source=<id>[,<id>]` | Exactly the named sources, everywhere. |
| `GET /v1/assets/{id}/nearby-grid` | The map's rule: one physical line is not listed twice. Plus `substations` (OpenStreetMap). The candidate box round the point follows the sphere (fix round 2: it was padded `radius_km / 100` degrees both ways, which east-west at 30.7 N is 28.7 km for a 30 km radius, so a substation or line just inside the radius due east or west was dropped before the distance test; the same helper serves `nearby-proposals` and the organisation's nearby proposals, which had the same shortfall). |
| Asset pages, `GET /v1/assets`, search | Both sources, each line its own record: LBNL's pages keep the FERC owner (`attributes.owner`). |
| Interconnection-point matcher (§2.5) | LBNL endpoint names, unchanged. Matching to OpenStreetMap substation names is a later lane. |

The licence summary covers the whole filter match, not the viewport (as before this lane), less the rows the
source rule and the voltage filter leave out: LBNL is credited while any LBNL line is drawn under the same
filters (with Texas alone loaded, the other states' lines), and not once OpenStreetMap covers every state.
"© OpenStreetMap contributors" travels in `licence_summary` and in each OpenStreetMap feature's
`source.attribution_text`, followed by the ODbL statement (`data/sources.yaml` `changes_statement`). **Web lane**:
the map legend's "lines FERC Form 1 filers own (LBNL); not the whole grid" (§2.2) no longer describes what is
drawn once OpenStreetMap is loaded, and substations need a marker and legend entry; neither is in this lane's
files. Also web-lane: `web/labels.py` `labelled_attributes` recurses into dicts and lists, so an asset page would
print a substation's `outline` coordinates and the bookkeeping keys in its Attributes table; `outline`,
`vertex_count_source`, `osm_id`, `osm_type`, `extract_regions`, `scope_regions` and `coverage_regions` belong in
`INTERNAL_ATTRIBUTE_KEYS` (the voltage fields, `substation_type`, `operator`, `states_crossed` and `region` are for
readers).

#### Share-alike: how the derived database is offered

The API and the map serve a Derivative Database of OpenStreetMap (ODbL §4.4(b), quoted in `docs/13` §2.10), so
it must be offered under ODbL 1.0 with access to it in machine-readable form (§4.6). The build's own public files
are that offer: `https://tiles.<DOMAIN>/grid/osm-power/<version>/lines.parquet` and `substations.parquet`, the
manifest that names the rules and every count, and the README that carries the licence. Until the page below
exists, the credit itself says where they are: the statement of changes (`data/sources.yaml`
`changes_statement`, which every OpenStreetMap feature's `source.attribution_text` and the `licence_summary`
carry) names `https://tiles.inframation.io/grid/osm-power/` and its `latest.json`. Proposed, for the legal
lane to word: a short "OpenStreetMap data" page linked wherever the credit renders (map attribution, asset page,
API `licence_summary` via `licence_url`), naming the licence and linking the current version's files and
`pipeline/context/osm_power.py` as the method. Joining these rows with Inframation's own records (the finder's
corridor bands, queue context) raises the Collective Database question (§4.5(a)); the critical-infrastructure
question the owner's decision sends to the legal lane (pairing substation locations with queue and capacity
context) is open too.

#### Measured on Texas (development, 2026-10-22)

**Route.** From the lane's sandbox, `download.geofabrik.de` and `overpass-api.de` were closed at the egress
proxy straight after the TLS hello on every attempt (eleven to Geofabrik over the day, the last three at
17:49 UTC). OpenStreetMap France's extracts (`download.openstreetmap.fr/robots.txt`: `Disallow: /*.pbf$` for
every agent) and `planet.openstreetmap.org` (`robots.txt` disallows every agent but wget) were not fetched. The
public Overpass instances timed out or answered 502 and 504. The development run therefore read the same
OpenStreetMap elements from Overture Maps' base/infrastructure theme, release 2026-10-21.0 (ODbL, public bucket
`overturemaps-us-west-2`). Only the row groups whose bounding box meets Texas's were range-read: about 420 MB,
and 50,905 `power_line`, `cable` and `substation` rows, each with its OSM element id, version and full tag set.
Those rows were written back as OSM elements, with synthetic ids only for vertex nodes, because Overture carries
way geometry, not node ids. The real build then ran on that file (`build --file US-TX=... --clip`, pyosmium
4.3.2, then `normalise`). The data is current to at least 2026-10-03T15:21:49Z, the newest element edit. Nothing
from this route is published: production reads Geofabrik (step 1 above), and the first national run's
`manifest.json` gives the Geofabrik counts for Texas to compare with these.

**Lines kept: 21,233** (490 cables), out of the Texas extract's power ways. Dropped: 13,069 busbars and bays,
840 below 69 kV (29 of them the volt-tagged service drops above), and 6,974 wholly outside Texas (the bounding
box clip). 1,666 ways state more than one voltage value (476 of them more than one distinct voltage, 1,217 a
repeated one such as `138000;138000`), 7,240 state circuits, 4,254 an operator and 207 a name. The
5 m simplification keeps 110,271 of 525,502 vertices (21%).

| Voltage class (highest stated) | Lines | Miles, whole ways | Miles inside Texas | Of which underground |
|---|---:|---:|---:|---:|
| 69-99 kV | 4,456 | 12,202 | 12,095 | 14 |
| 100-161 kV | 12,480 | 26,062 | 25,820 | 48 |
| 220-287 kV | 455 | 2,120 | 1,988 | 0 |
| 345 kV | 2,286 | 12,852 | 12,469 | 1 |
| 500 kV | 10 | 180 | 103 | 0 |
| voltage not stated | 1,546 | 1,829 | 1,829 | 36 |
| **All** | **21,233** | **55,245** | **54,303** | **99** |

**Against LBNL in Texas** (the same method on `us.lbnl.ferc_hifld_transmission_lines`, fetched the same day: one
request, 26,932,786 bytes, the same sha256 as on 2026-09-28): LBNL has 1,390 lines and **12,581 miles** inside
Texas, all at 100 kV and up (100-161 kV 6,574; 220-287 kV 1,128; 345 kV 4,778; 500 kV 100). OpenStreetMap
has 40,380 miles at 100 kV and up (3.2 times LBNL), and 54,303 miles with the 69-99 kV lines and the unstated
ones (4.3 times). By class: 345 kV 12,469 against 4,778 (2.6 times); 100-161 kV 25,820 against 6,574 (3.9
times). LBNL has no line under 100 kV in Texas (its national file starts at 70 kV), so the 69 kV network that
large loads often connect to comes only from OpenStreetMap. "Inside Texas" means a segment counts when its
midpoint falls in the vendored US-TX polygon, at geodesic length. OpenStreetMap is measured on its stored 5 m
line and LBNL on its own vertices.

**Substations kept: 6,205**, of which 6,204 were mapped as areas (centroid plus outline) and 1 as a node. 47
`minor_distribution` were dropped; Texas has no `traction` substation in this data. Subtypes: distribution
2,928; transmission 1,322; untagged 908; industrial 706; generation 236; compensation 33; switching 22;
transition 18; `yes` 17; and 15 others (generator 8, converter 3, switch_yard 2, switchyard 1,
generation;switching 1), each kept as tagged. 5,503 state 69 kV or more, 689 state no voltage, 3,289 are
named and 3,160 name an operator.

**Sizes and timings** (this sandbox). Build of the Texas file 23.5 s (filter, read, assemble areas, clip);
`normalise` 11.8 s. GeoParquet: lines 7.8 MB, substations 0.72 MB; asset frames 3.4 MB and 1.1 MB. Load into a
fresh SQLite store already holding LBNL: 20.1 s; the reload took 25.8 s with 0 inserted, 27,438 updated and 0
withdrawn. Store 73.9 MB (LBNL plus OpenStreetMap Texas). The licence row comes out `share_alike: true`.

`/v1/assets/geo?asset_type=transmission_line` through the app (TestClient), on that store:

| Request | Time | Bytes | Features |
|---|---:|---:|---|
| Cold first request (builds the line index) | 4.1-4.3 s (LBNL alone 2.1-2.3 s) | | |
| National, zoom 4, warm | 0.21 s | 1,473,287 | 1,057 (892 LBNL, 165 OpenStreetMap) |
| Texas, zoom 6 (first at this zoom) | 1.3 s | 2,161,901 | 1,500, the cap (1,284 OpenStreetMap; 216 LBNL in the box's other states or crossing the border) |
| Central Texas, zoom 8 (first) | 2.0 s | 613,628 | 418, all OpenStreetMap |
| Houston, zoom 10 (first) | 2.3 s | 320,768 | 211, all OpenStreetMap |
| Oklahoma, zoom 7 (first) | 1.4 s | 254,600 | 175 LBNL, 2 OpenStreetMap (border ways in the Texas extract) |
| `asset_type=substation`, Texas, zoom 6 | 0.15 s | 27,657 | 69 clusters for 6,205; Houston at zoom 11: 116 points |
| `nearby-grid`, 10 km round a substation | 0.14 s | | 10 lines, 10 substations |

Each first request at a new zoom pays one simplification pass over every line, then is cached. Steady-state
memory of the API process (VmRSS after garbage collection, after the first national request): +144 MB for the
line index with LBNL alone, +258 MB with LBNL plus OpenStreetMap Texas.

**Fix round re-measure (2026-10-22, the same development file, pyosmium 4.3.2).** The fixed build
(`build --file US-TX=... --clip`) gives the figures above unchanged: 21,233 lines, 6,205 substations, 55,245 whole
miles and 54,303 miles inside Texas. The Texas data has no comma-written voltage and no substation mapped as both a
way and its relation, so those two rules change nothing here. Nine cables tagged `location=overground` (0.15 miles
in all) are no longer flagged underground; the underground column above rounds the same.

**The border loss the fix removes, measured on the same file.** Read as five extracts in table order (Arkansas,
Louisiana, New Mexico, Oklahoma, Texas), each the file clipped to that state's polygon, so each holds the border
ways the way Geofabrik's do, then loaded as `GRID_LAYERS_REGIONS=US-TX`:

| Build | US-TX load: lines | Whole miles | Miles inside Texas |
|---|---:|---:|---:|
| v1 (first extract keeps the way) | 21,161 | 53,628 | 53,492 |
| Fixed (scope rule) | 21,240 | 55,262 | 54,303 |

v1 lost **79 lines, 1,635 miles (812 inside Texas)**: 26 first held by New Mexico, 24 by Oklahoma, 15 by Louisiana
and 14 by Arkansas. Of the 79, 39 lie mostly in Texas (their representative point is there) and 19 are 345 kV, 2 are
500 kV, 6 are 220-287 kV, 31 are 100-161 kV, 18 are under 100 kV and 3 state no voltage. With v1 the map, nearby-grid
and the finder would have lost them with no warning, while LBNL's Texas lines stayed hidden. The fixed US-TX load
holds 7 more lines than the single-file build (21,240 against 21,233; 0.2 more miles inside Texas): border ways
that the state lookup's 1 km cache cell (`pipeline/context/geo.py` `StateIndex`) puts in Texas when another state's
points filled the cell first. That is the harmless direction.

**Fix round 2 (2026-10-23), re-run with the current module on the same file:** the same figures (five extracts
loaded as US-TX: 21,240 lines, 6,209 substations, 55,262 whole miles, 54,303 inside Texas; v1 lost 79 lines,
1,635 miles, 27 of them at 230 kV or more). The second review measured the same loss another way, on the lane's
own clipped build against the state polygons: 74 kept ways that cross into a neighbour sorting before Texas,
1,603 whole-way miles, 26 at 230 kV or more. So the Texas-only figures in this section (the table, +114 MB, the
Oklahoma zoom-7 row) are what a US-TX server loads from the national build, within those 7 lines: the clipped
development build kept every way with a vertex in Texas, which is the scope rule's set less the few ways only
the Texas extract holds.

#### Limits and open items

1. ~~**Wire the pull into `context_build`**~~ **Closed 2026-10-23 (lane sf-golive).** In one change:
   `infra/scheduler/jobs.py::CONTEXT_BUILDERS` ends with three pulls, `("pipeline.context.osm_power", "pull")`,
   `("pipeline.context.fema_nfhl", "pull")` and `("pipeline.context.prospect.pull",)` (since 2026-10-23 a fourth,
   the parcels pull, follows them: `docs/25-siting-parcels.md` §8.9), after every builder, so
   the chain is pulls, then `context_load`, then `site_finder_refresh`; `services/ingest/context_layers.py::
   expected_files()` lists `OSM_POWER_FILES`; `infra/scheduler/test_context_and_match.py` maps both frames to
   `osm_power` and runs the three pulls' real CLIs against a published grid build and an empty private bucket.
   The flood pull (`fema_nfhl pull`, new) reads `data/normalized/context/us.fema.nfhl/latest.json` from the
   private bucket (`R2_BUCKET`, with the R2 key the env file already carries), fetches only the states the
   server loads at the keys the manifest names for each (sha256 checked, written by rename), and writes the local
   `latest.json` last, narrowed to those states, which is what the site finder reads (§5.5). The prospect pull
   (`pipeline/context/prospect/pull.py`, a thin wrapper over `services.context_sample.sync`) reads
   `prospect/current/texas.json` for a Texas-only server and `conus.json` for any other setting; a Texas
   server never falls back to `conus` (review 2026-10-23: the national build is unmeasured, prospect item 9,
   and would land in a site-finder run that already peaks at 1.2 GB), so until a `texas` build is dispatched
   it answers "not published" and names the dispatch. Both read `GRID_LAYERS_REGIONS`
   (no second setting) and refuse to run without it. A layer whose workflow has not published yet answers
   `status: not_published` and exits 0: `context_build` lists it under `not_published`, never under `failed`,
   and the other builders and the load run as before. The variable reaches the worker through the deploy
   (limit 3). The monthly tick moved from the 3rd to the 4th at 02:43 UTC (`infra/scheduler/app.py`
   `CONTEXT_CRON`): on the 3rd it fired before the flood (quarterly, the 3rd at 04:41) and prospect (the 3rd at
   06:41) builds, so each scheduled build reached the server a month late; a test holds every build's schedule
   plus its jobs' time limits before the tick.
2. **pyosmium is not in `requirements.txt`**: only the build reads OSM files, and it installs `osmium==4.3.2`
   itself. The OSM-file tests skip in `ci.yml`; `grid-layers.yml` runs them in a light `test-osm-files` job on
   every pull request that touches the build module, its fixtures, `geo.py`, the state polygons or the workflow
   (no secrets, no build), and fails if they skipped.
3. **The national map payload is too large for the in-memory line index.** Texas holds 8.6% of LBNL's national
   route miles. Scaling the Texas build by that share gives roughly 250,000 lines and 1.3 million stored vertices
   nationally: about 1.3 GB more resident memory per API process, a cold first map request of about 25 s, and
   each first request at a new zoom growing the same way. That does not fit the one-server beta. Until the map's
   line layers move to vector tiles (ADR 0008's deferred path; the GeoParquet on R2 is the input such a build
   would take, in the same Actions job), the server should load only the states it can hold:
   **`GRID_LAYERS_REGIONS=US-TX` for the beta** (the map opens on Texas; +114 MB, +2 s cold, measured above on
   the clipped file, which a US-TX load of the national build matches within 7 lines).
   LBNL keeps drawing every other state through the map's source rule. The published files stay national
   either way. The setting is required (unset is refused), and the pull's region filter runs in pyarrow before
   any row becomes a Python object, so a US-TX pull of the national file peaks at Texas's size plus one
   65,536-row batch, not at the nation's. **On the beta since 2026-10-23 (lane sf-golive)**: the GitHub
   Environment variable `GRID_LAYERS_REGIONS` (a variable, not a secret) is checked by deploy-beta's inputs
   check (ISO codes or `all`) and rendered into the env file every container reads (`infra/ci/beta.py`),
   `US-TX` when unset, so the worker's pulls always have it (`docs/64` §2 inputs table). `all` passes the check
   with a warning on the run, since national does not fit this server. The pulls still refuse an empty value; a
   store built by hand or another route must set it (staging and production through their SOPS file, where
   the key is optional for `deploy.sh` but required wherever the site finder runs: without it the three pulls
   fail every month).
4. **Geofabrik's usage notes and robots.txt, read second-hand**: `download.geofabrik.de` is still closed at the
   sandbox's egress (connection reset after the TLS hello, 2026-10-22 evening), so on 2026-10-22 about 23:50 UTC
   they were read through a fetch service that returns a summary with quotes, not the raw bytes.
   `https://download.geofabrik.de/robots.txt` answered HTTP 404 (no robots file, so nothing is disallowed). The
   front page: "This open data download service is offered free of charge by Geofabrik GmbH."; "License: ODbL
   1.0"; the files "do not contain the user names, user IDs and changeset IDs of the OSM objects". The technical
   page (`technical.html`) and the US index (`north-america/us.html`) state no rate limit, no rule on parallel or
   automated downloads, and no bulk-download policy; the data is updated "Once a day, around 21:00 CET". The
   licence sentences on `www.geofabrik.de/data/download.html` re-read the same way match those quoted in
   `data/sources.yaml`. The build's own politeness (one state at a time, five seconds apart, robots.txt checked at
   run time) is therefore the platform's default, not a Geofabrik figure. **Blocking for the first national run**:
   a human opens the front page and `robots.txt` in a browser, confirms these quotes against the raw text, and
   records the result in `data/sources.yaml` `verified`. Whether GitHub's runners reach the download server is
   not verified from here; the build fails rather than publishes if they do not. The owner's steps, in order
   with the national run after them, are `docs/66` "Site finder go-live" (still open until that check is
   recorded). The check comes before `grid-layers.yml` reaches `main`: from then its own monthly run (the 2nd,
   06:41 UTC) makes the first national build without anyone dispatching it.
5. **Untagged voltage**: in Texas, 1,546 lines (7.3% of lines, 1,829 miles, 3.4% of the miles) state no usable
   voltage (1,536 have no `voltage` tag, 9 say `0`, 1 says `medium`), and so do 689 substations (11%). They are
   kept with `voltage_unknown: true`; `voltage_kv[gte]` leaves them out unless `include_voltage_unknown=true`.
   The finder should offer that switch, because a 1,536-way gap is visible on a map and some of it is high voltage.
6. **Substation names to interconnection points**: OpenStreetMap substations are named and placed; matching queue
   POI strings to them (the §2.5 matcher against a much larger node table) is the next grid lane, and placing a
   point by a substation's location is now the owner's "public now" decision applied, not a derived layer.
7. **The development counts came from Overture's copy, not Geofabrik's extract** (route above). Both carry the
   same OpenStreetMap elements, but Overture's pipeline may drop an element it cannot build a geometry for.
   Compare the first national run's Texas counts with the table above; a gap over 1% is worth a look. Since the
   fix round the manifest's US-TX `lines_kept` counts every row in Texas's scope, as a US-TX server loads it,
   which is what the clipped development build counted (every way with a vertex in the Texas polygon), plus the
   few ways wholly outside the polygon that Geofabrik's Texas extract also holds. Expect the national count to
   be at or slightly above the table's, never below it because of the border.
8. **The server's pull downloads the national files, whatever `GRID_LAYERS_REGIONS` says** (a measurement to
   take). A subset build is never promoted, so a US-TX server's `osm_power pull` fetches the national
   `lines.parquet` and `substations.parquet` and keeps Texas in pyarrow (limit 3). It runs as its own process
   inside the worker's container. Since 2026-10-23 (lane sf-golive) it streams each file to
   `<data root>/tmp/osm-power-pull/` in 1 MB chunks, hashing on the way, instead of holding the body whole
   (`requests` builds `.content` by joining its chunks, about twice the body), and deletes both files when the
   frames are written. Record after the first national build:

   | Measure (US-TX server, national build) | Estimate | Measured |
   |---|---|---|
   | `lines.parquet` | 90-100 MB (Texas's 7.8 MB at 8.6% of LBNL's route miles) | not yet |
   | `substations.parquet` | about 8 MB (Texas's 0.72 MB scaled the same way) | not yet |
   | pull, elapsed and peak RSS in the worker container (`/usr/bin/time -v python -m pipeline.context.osm_power pull`) | time not estimated; memory about Texas's rows plus one 65,536-row batch, plus pyarrow itself | not yet |
   | disk in `<data root>/tmp/` while it runs | the two files, about 100 MB | not yet |

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
| 5 | ERCOT large-load reporting (`us.iso.ercot.large_load_queue`, existing) | https://www.ercot.com/services/rq/large-load-integration | monthly PDF slide deck (aggregates, no rows) | Texas, aggregated | monthly | none per project | — | ERCOT clause 5, open | The existing watch stays. No per-project rows exist (rechecked 2026-10-07, §3.10.1; its catalogue rows are never loaded as projects) |
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
- Precedent: `us.iso.ercot.large_load_queue` already emits `kind = load` rows (catalogue products, which since
  2026-10-07 are never loaded as projects, §3.10.1), and `pipeline/normalize.py`'s
  technology rules map `load|data\s*cent` to `load`. **No new table and no migration are needed.** Everything
  loads through `services/ingest/loader.py` unchanged. The DEQ point reaches `location` at `exact` precision via the
  loader's existing raw `Latitude`/`Longitude` promotion, which applies only because the source is `raw_ok`.

### 3.4 What a record means, and what it does not

It **means** that DEQ has an air-programme facility record for this site, and that DEQ, or DEQ's own
principal-product text, says the site is a data centre. The status says whether DEQ records the site as planned,
under construction or operating. The point is DEQ's facility reference point, rounded to 6 decimals because the
server's reprojection adds noise beyond that. Of the 205 points, 180 carry 6 decimals, 19 carry 5 and 6 carry 4.
The county is our derivation (point-in-polygon on the vendored Census counties), spelled as ICIS-Air and EIA-860M
spell it: "Loudoun", "Manassas city" (§3.10.3; "Loudoun County" until 2026-10-07). It agrees with EPA ICIS-Air's
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
same-facility ICIS duplicates under re-padded programme ids (audit F8) are not addressed here; lane L11 folds them
in the connector (§3.10.4).

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

### 3.10 Review fixes: large-load data, MW, county text, ICIS-Air precision (lane L11, 2026-10-07)

An expert buyer review (2026-10-07, large-load planner persona) found no ERCOT large-load requests, no MW on any
load record, one county spelled two ways, every Fairfax data centre on Fairfax *city*'s FIPS code, and offices among
the ICIS-Air data centres. Numbers below compare two full dev stores built with `web.dev_up.build_store`: base
`acfa192`, and this lane's code with the DEQ and ICIS-Air stored snapshots re-normalised (nothing written under
`data/normalized`). Live means unmerged and public.

#### 3.10.1 ERCOT large-load requests: there is no data to load

`us.iso.ercot.large_load_queue` has two normalised frames and both have **0 rows**. The connector is a catalogue
watch (§3.2 row 5), not a queue parser: it emits one row per ERCOT EMIL *product* that looks like the Nodal
Protocol 3.2.7 status report, and no such product exists. Rechecked 2026-10-07:
- The [large-load integration page](https://www.ercot.com/services/rq/large-load-integration) lists Batch Zero
  forms, RFI guides and attestations, and no status report.
- The [September 2026 LLWG meeting](https://www.ercot.com/calendar/09172026-LLWG-Meeting) lists no status report.
- The queue appears only as charts in PDF decks, for example the
  [March 2026 TAC report](https://www.ercot.com/files/docs/2026/03/12/March-TAC-Report.pdf), page 3: system-wide MW
  by study status (observed energized, approved to energize, planning studies approved, under ERCOT review, no
  studies submitted). There is no county, zone table or project row. Board updates give totals in prose
  ("~438 GW of Large Load Interconnection requests", June 2026).
- The EMIL catalogue API answered HTTP 403 with an Incapsula bot challenge to one GET from this environment. The
  catalogue was not re-read, and the connector's next run from this host is expected to fail. That is a source
  breakage to watch, not a reason to work around the challenge.

So the dev and static loads still carry no ERCOT large loads (0 before, 0 after), and `web/build_data.py`'s
`PROPOSAL_SOURCE_IDS` does not list the source. Listing it would load nothing today, and the public source sentence
would then name a source with no rows. **Model when data exists:** request-level rows become `kind = load`
proposals. Aggregate rows (by load zone, TSP, status or size band, which is what NPRR1267 asks ERCOT to publish)
become totals on a zone or county, never invented projects. The resolver keeps loads apart from generation
either way: `KIND_CLASSES` puts `load` in its own class (A-22-T-1). On both stores, no load-source link sits on a
non-load proposal and no load proposal carries a generation source.

One defect fixed. Through the generic loader, the day the catalogue lists the product, the product row would have
been published as a `load` project named "Large Load Interconnection Status Report", sponsor ERCOT, with a `new`
event. `services/ingest/large_load_watch.py` is now the source's `SPECIALISED_LOADERS` branch. It records the run,
writes no proposal, and warns with the product names. The scheduler's resolution step still reads the watch frame
as a proposal frame (`infra/scheduler/jobs.py::_latest_proposal_frames`). It is empty today. Skipping
specialised-loader sources there is an open item for the scheduler's owner.

#### 3.10.2 MW on data-centre loads: no capacity signal worth exposing

All 578 live load records (582 before) have a null `capacity_mw`, by design (§3.4).
- **ICIS-Air** publishes no capacity in any column the connector reads.
- **Virginia DEQ:** 4 of 205 `PLA_DESC` texts state engine ratings, for example "Six 3000 kW and one 500 kW emergency
  generators" (CLOP Ashland), "Four 500 kWe and One Hundred Ten 3000 kWe engine-generator sets" (Microsoft LYH03).
  7 more give only a count ("280 diesel engines"). Ratings are permitted backup generation, not IT or grid load.
- **Decision.** At 2% coverage, extracting them would make a column that is empty on 98% of rows and invites
  being read as load. The 2026-09-28 rule stands: nothing is extracted.
- **For the web lane.** The load list's default "capacity descending" sort, and any capacity column, should be
  absent for `kind = load`.

#### 3.10.3 County text and FIPS

- **Spelling.** Virginia DEQ wrote "Loudoun County"; ICIS-Air and EIA-860M write "Loudoun". After resolution a
  county showed up two ways, so a roll-up on the county column double-counted. Both data-centre connectors now
  take their county text from `pipeline/context/geo.py::county_label`. A county is its bare Census name; an
  independent city is "<Name> city" ("Manassas city", "Fairfax city"). ICIS-Air's "Harrisonburg (city)" is
  written "Harrisonburg city". `county_norm`, which the resolver blocks on, is unchanged.
- **FIPS.** The Census Gazetteer holds "Fairfax County" (51059) and "Fairfax city" (51600). Both normalised to
  ("VA", "FAIRFAX"), and the city's later row won the FIPS and the centroid. `services/ingest/geocode.py` now keys
  an independent city as "<NAME> CITY" (`county_lookup_keys`). Text ending in "city" asks for the city first,
  then the bare name, so "Carson City" and "James City" still resolve. A bare name means the county. The static
  builder's copy (`web/build_data.py`) applies the same rule. The same collision hit Franklin, Richmond and
  Roanoke (VA), Baltimore (MD) and St. Louis (MO).
- **Repair of stored rows.** `backfill_county_fips` only fills NULLs, so `correct_county_fips`
  (`python -m services.ingest.geocode correct-fips`) re-derives every US row's code from its own county text. It
  also moves a `county_centroid` point to the right centroid. The production store needs one run of it.

| Dev store, live proposals | before | after |
|---|---|---|
| VA load FIPS codes shown under two spellings | 10 (Loudoun 41/46, Prince William 14/17, Fairfax 18/16, Mecklenburg 4/6, Henrico 3/6, Fauquier 3/2, Culpeper 3/1, Chesterfield 2/2, Louisa 1/2, Spotsylvania 1/1) | 0 |
| Fairfax load records on 51600 (Fairfax city) | 34 | 0 (all 34 on 51059) |
| Records moved from a city code to its county (every one checked by its exact point) | — | 41: Fairfax 34, St. Louis 3 (Castle Bluff), Richmond 2 (Moon Corner, Cerulean Solar), Franklin 1 (Willow Solar), Baltimore 1 (Control Data Corp.) |
| Exact US points whose FIPS matches the county the point falls in | 2,538 of 2,698 | 2,577 of 2,696 |

The 119 remaining disagreements are the sources' own county text (EIA-860M, ICIS-Air) against their points, for
example Allegheny vs Westmoreland. They are not a normalisation error.

#### 3.10.4 ICIS-Air: re-registrations folded; a name rule rejected on evidence

- **Re-registrations (audit RES-14).** ICIS keeps some facilities twice, under re-padded or re-prefixed programme
  ids. `fold_reregistrations` keeps one row when two selected rows share the FRS `REGISTRY_ID` *and* a normalised
  name. The longest programme id is kept, then the lowest. The other id goes to `raw.duplicate_pgm_sys_ids`, with
  its own status, and into `cross_refs`, so a Virginia DEQ record citing either id still pairs (D3).
- **Fold result.** On the 2026-10-07 snapshot it folds 4: Nebraska Colocation Center, ConAgra Foods Data Center,
  DataBank MCI 3 and Woodland Caribou. Rows go 521 → 517. FRS ids on more than one live proposal go 8 → 4, and live
  ICIS-Air proposals 504 → 500.
- **Not folded, because the names differ:** CyrusOne Florence ("CYRUSONE LLC - FLORENCE DATA CENTER" / "CYRUS ONE
  LLC"), Amazon IAD-6/13/54 and "VADATA INC MEG FOUR" (same street address, two registrations), Microsoft Alluvium
  and Ginger West, and Iowa Health System / UnityPoint Health (a renamed entity at one address). An address rule
  would also merge separate permits at one address, so these are left for review.

**Selection basis.** The review names six sites as false positives. The evidence on each:

| Named in the review | Basis | Evidence | Verdict |
|---|---|---|---|
| The World Bank, 4120 Lafayette Center Dr, Chantilly VA | NAICS 518210 | ICIS-Air's own record (programme id VA0000005105901126): a Planned Facility, minor source, NAICS 518210. Virginia DEQ holds no air site for it on either public layer (294 Air Sites, 298 Planned Air Sites; queried by that id and by name, 2026-10-24). No primary public source naming the building's use was found; the floor-area and power figures this row used to quote came from a private aggregator and are withdrawn (2026-10-24) | unverified |
| The Travelers Indemnity Co, 14715 Schram Rd, Omaha NE | NAICS 518210 | Travelers' $200 M Tier III data centre on Schram Road, with its own OPPD substation ([DCD](https://datacenterdynamics.com/en/news/insurance-company-opens-200m-data-center-in-nebraska)) | data centre |
| Sentara Healthcare, Virginia Beach | NAICS 518210 | Virginia DEQ's own register flags it as a data centre (§3.4) | data centre (regulator's flag) |
| Bank of America – Villa Park Data Center, Richmond | name | named a data centre by the source; DEQ also flags "Bank of America - Sandston" | enterprise data centre |
| Rockingham Memorial Hospital Data Center | name | named a data centre by the source | enterprise data centre |
| Concordance Healthcare Solutions | NAICS 518210 | already suppressed (lane W1) | not a data centre; unpublished |

A name rule was tested and rejected: drop NAICS 518210 rows whose names declare a non-hosting industry (bank,
insurer, health, hospital, institution, customs, school) unless they also name computing ("technology center",
"IT operations", "info sys"). It would drop 12 rows, and the evidence splits them three ways.
- **5 are data centres.** Travelers (above). Sentara, the Smithsonian at Herndon and US Customs and Border
  Protection at Springfield, all flagged by DEQ. Optum / UnitedHealth Group at 1707 W Creek Ln, Chaska MN, which
  the Minnesota Pollution Control Agency's own permit record names "United HealthCare Services Inc - Data Center"
  (registration permit 01900066-001, EPA id MN0000002701900066, NAICS 518210;
  [MPCA, What's in My Neighborhood, site 132882](https://webapp.pca.state.mn.us/wimn/site/132882), a page that
  renders by script, so read through the
  [MPCA Air Quality Permits layer](https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_pca/env_air_quality_permits/FeatureServer/0)
  on the Minnesota Geospatial Commons, 2026-10-24).
- **3 are not.** Health and Hospital Corporation (below), and two already suppressed (US Liability Insurance,
  Concordance).
- **4 are unverified.** The World Bank (above), Iowa Health System, UnityPoint Health and First Health Services.

Separately, "NALCO WATER" at 505 N Railroad Ave, Northlake IL is the address Digital Realty gives for its ORD23
data centre ("505 North Railroad Avenue, Northlake, IL 60164",
[Digital Realty, ORD23](https://www.digitalrealty.com/data-centers/americas/chicago/ord23), read 2026-10-24),
which shows that the operator's name is not the occupant's industry. The selection rule is unchanged. The review's
examples are mostly *enterprise* data centres, not offices. The distinction a planner needs is enterprise vs
colocation or hyperscale, which is a classification for later, not a selection rule.

**Hand check.** One new verified false positive was found. HEALTH AND HOSPITAL CORPORATION, 3838 N Rural St,
Indianapolis (FRS 110055591328) is the Marion County Public Health Department's building
([Marion County Health Department listing](https://directory.yext.com/en/marion-county-health-department-140729995)).
It belongs on `data/vendored/data_centres/icis_air_not_data_centres.yaml`, but `services/resolve/test_suppress.py`
pins that list, so the change goes to the resolver lane. Checked and still unverified: First Health Services, Glen
Allen VA; Iowa Health System / UnityPoint Health, Des Moines; BP Amoco shared service centre, Tulsa; Deere & Co,
Moline. None was found to be a data centre or an office.

**Precision on a random sample.** 40 rows (`DataFrame.sample(40, random_state=20261007)` of the 521):

| Sample | before | after |
|---|---|---|
| Published | 39 (Northrop Grumman Falls Church already suppressed) | 39 |
| Judged data centres | 36 | 36 |
| Unverified | 3: Deere & Co Moline; Gannett, 2240 Broadbirch Dr, next to a data centre at 2220; First Data Resources, 7305 Pacific St, Omaha | 3 |
| Judged not data centres | 0 | 0 |
| Precision | 92% strict (unverified counted wrong), 100% lenient | same |

The fold changes no sampled row: the one duplicate drawn, "CYRUS ONE LLC", has a different name. On the
review's six named rows, precision is 4 of 6 strict (the World Bank unverified) and 5 of 6 lenient, before and
after, because the sixth was already unpublished.

*Sources (2026-10-24).* This section cited datacenterHawk, Baxtel and datacentermap as evidence until 2026-10-24.
Those are private aggregators, which this repo never uses as sources (CLAUDE.md guardrail 1; the survey's list in
`docs/08-data-centre-sources.md` §1), so each citation was replaced (Chaska: the MPCA permit record; Northlake: the
operator's own page) or its claim removed (Chantilly: the building's floor area and delivered power; the verdict
moves to unverified). Chaska and Northlake were re-checked against those primary sources on 2026-10-24. For
Chantilly no primary public source naming the building's use was found (ICIS-Air's own record and both Virginia
DEQ air-site layers were searched), so the row stays unverified until one is.

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

## 5. Floodplain: FEMA National Flood Hazard Layer (lane flood, 2026-10-22)

Owner decision 2026-10-22 (`docs/00-PLAN.md`): the powered-land site finder looks for sites within a
mile of a 69 kV-or-higher line, within 5 miles of a substation, **outside the FEMA floodplain**; where FEMA
has no digital map the result says "flood data unavailable", never "outside". This lane builds the
floodplain files the finder reads. It adds no table and no API.

Code: `pipeline/context/fema_nfhl.py` (route, range reads, geodatabase catalog, classification, clipping,
GeoParquet, manifest, CLI, and `FloodLayer.assess`, the finder's rule as a reference implementation), tests
beside it with recorded fixtures under `pipeline/context/fixtures/fema_nfhl/`,
`infra/scripts/build_flood_layer.sh` and `.github/workflows/flood-layer.yml` (the production build), manifest
id `us.fema.nfhl`, register row in `docs/13` §6.

### 5.1 Source, route and terms

The National Flood Hazard Layer is FEMA's compilation of the effective Flood Insurance Rate Map (FIRM)
databases and Letters of Map Revision, "the current effective flood data for the country, where maps have
been modernized" (FEMA's NFHL web map description, ArcGIS item `fa23d2702e244058ab47eb62a991d600`, read
2026-10-22). FEMA publishes it per state as a zipped Esri file geodatabase and per county as shapefile zips.
On 2026-10-22 the 52 state files (50 states, DC, Puerto Rico) were **37,120 MB** of zips: Florida 2,693 MB,
Iowa 2,262, Georgia 2,125, Texas 2,031, Virginia 1,657, down to DC at 4 MB. 29 were dated within 30 days and
38 within 60; the oldest is Puerto Rico's (2021-12-16). Each is in NAD83 geographic coordinates (EPSG:4269).

**robots.txt decides the route** (both hosts read 2026-10-22):

| Path | robots.txt | Used |
|---|---|---|
| NFHL ArcGIS REST services, `hazards.fema.gov/arcgis/...` and anything with a query string | `Disallow: /arcgis`, `Disallow: /*?*` | never |
| MSC direct download, `msc.fema.gov/portal/downloadProduct?productID=...` | `Disallow: /portal/downloadProduct?productID=`, `/*?*` | never |
| MSC "Search All Products" form, `POST msc.fema.gov/portal/advanceSearch` | path allowed | one POST per state: for a community in the state (`STATE_PROBES`), the answer names the current state file |
| The file, `hazards.fema.gov/nfhlv2/output/State/NFHL_<FIPS>_<YYYYMMDD>.zip` | path allowed | HTTP range requests: the zip's central directory, then only the members used |

The probe communities were found by product-ID searches of countywide products (`NFHL_<county FIPS>C`),
whose answers name a community; Connecticut needs its pre-2022 county code, DC and Puerto Rico their
community-wide ids. The search is a page form, not a documented API: if it changes, `--product-file` names
the file by hand and the workflow fails loudly rather than guessing. The stdlib robots parser does not
implement `*` wildcards, so the module refuses any FEMA URL with a query string itself (`fema_url_allowed`).

**Only two layers are fetched.** A state geodatabase stores each layer as a table file (`aXXXXXXXX.gdbtable`
and companions). The module reads the zip's directory, extracts the system tables (ids 1-8, about 300 kB),
reads `GDB_SystemCatalog` in Python to learn which numbers hold `S_FLD_HAZ_AR` and `S_FIRM_PAN` (GDAL refuses
a geodatabase holding only system tables), and extracts those two. Texas: **1,005 MB fetched of 2,031 MB**,
90 requests, 404 s through the development egress (2.5 MB/s), 1,277 MB written. Every range request carries
`If-Range` with the first ETag, so a file FEMA replaces mid-read fails the run instead of mixing two files.
The raw tables are deleted after clipping whether the build succeeds or not. A county product takes the same
path as shapefiles: Taylor County, `48441C_20260611.zip`, 8.8 MB fetched of 16.1 MB in 7 requests.

**Terms** (quoted in full in `data/sources.yaml` and `docs/13` §6): a US government work (17 U.S.C. §105);
DHS's DCAT record states `"license": "https://www.usa.gov/government-works"`, `"accessLevel": "public"`; the
MSC says NFHL geodatabases "are available for free download". Credit rendered: "Source: FEMA National Flood
Hazard Layer". FEMA's NFHL map adds the one condition that shapes the product: a map on another base map
"should be used for general reference only, and not official NFIP purposes", so every finder result says it
is not a flood determination. FEMA's website policies on www.fema.gov answered 403 from this egress (browser
task). No use constraint is embedded in the data itself.

### 5.2 What counts as floodplain

`S_FLD_HAZ_AR` polygons are classified per row (`classify`):

| Category (file) | Rule | Texas, statewide rows |
|---|---|---|
| `sfha` (`sfha/`) | `SFHA_TF` is true **or** `FLD_ZONE` is A, AE, AH, AO, AR, A99, V or VE | 69,459 (AE 48,325 incl. floodway, A 19,659, VE 1,107, AO 235, AH 131, and 2 zone X rows flagged SFHA) |
| `shaded_x` (`zones_other/`) | zone X with any subtype but "area of minimal flood hazard": 0.2 % annual chance (the "500-year"), levee-reduced risk, 1 % future conditions, 1 % shallower than a foot | 136,463 |
| `zone_d` | zone D: hazard undetermined | 0 |
| `open_water` | zone OPEN WATER | 19 |
| `not_included` | AREA NOT INCLUDED: inside a map's extent, mapped by nobody | 9 |
| `unrecognised` | any other zone (vocabulary drift; reported in the run summary) | 0 |
| not written | zone X, "area of minimal flood hazard" or no subtype (unshaded X) | 30,017 |

Where `SFHA_TF` and the zone disagree the row counts as floodplain: the safe reading for an "outside the
floodplain" claim. Texas has three such rows (two zone X rows flagged true, one AE floodway flagged false).
Texas's 4,186 "1 PCT FUTURE CONDITIONS" polygons are shaded X on the effective FIRM and are reported, not
excluded. The rows sum to the layer's 235,967.

The geodatabase read skips only rows `classify` would drop: zone `X` with no subtype or the minimal-hazard
one, and `SFHA_TF` NULL or `F` (`ZONE_WHERE`). Every clause is NULL-safe. Until 2026-10-23 the filter used
`FLD_ZONE <> 'X'`, which is NULL, not true, for a NULL zone, so on GDAL's path a row with no zone (an
"unrecognised" row, flood data unavailable) and a zone X row flagged SFHA with a value other than `T` were
never read. Texas's statewide count above has no unrecognised row. A test writes such rows to an OpenFileGDB
table and reads them back through GDAL.

Invalid source polygons are repaired by structure (`make_valid(method="structure")`): overlapping parts are
unioned. The default "linework" repair keeps area by the even-odd rule and drops what two parts share (two
overlapping 2 x 2 squares: union 7, repaired 6), which would turn floodplain into "outside". On Harris County's
product (`48201C_20260517.zip`, read by range 2026-10-23, 56 MB of 103 MB, then deleted) 16 of 10,806 zone
polygons are invalid, 7 of them SFHA, and the two repairs agree within 1 m² in all: self-touching rings, not
overlapping parts. Texas's 264 were not re-checked (§5.7).

### 5.3 What "outside the floodplain" means

The zone files hold rows **only inside the areas of interest** (§5.4); the coverage files are statewide. So a
covering panel alone says nothing about a site outside the areas of interest: there is no `sfha` row there to
find, floodplain or not. On the recorded Taylor County fixture with a 0.5-mile area of interest, 0.67 km² of
the 1.68 km² of source SFHA inside covering panels lies outside the area of interest; the rule as first
written (covering panel, no zone polygon) called all of it "outside" (review, 2026-10-23).

The rule, in order (`FINDER_RULE` in the manifest; `FloodLayer.assess` implements it and the tests hold it to
the recorded county product, with a grid of 1,681 points none of which reads "outside" inside the source SFHA):

1. **In the floodplain** when the site comes within the tolerance (10 m) of an `sfha` piece of any state.
2. **Flood data unavailable (not computed)** when any part of the site lies outside every state's area of
   interest: the state's row of `aoi.parquet` under that state's own version (`states[ST].aoi.key`).
3. **Flood data unavailable (no digital map)** when any part lies outside the union, over states, of *that
   state's* area of interest intersected with *its* covering panels (grown by the tolerance, which closes the
   seams independently simplified panels leave). Never one state's area of interest with another state's
   panels: near a border the other state's zone files were not computed there.
4. **Flood data unavailable** when the site comes within the tolerance of a `zone_d`, `not_included` or
   `unrecognised` piece (the reason names which).
5. Otherwise **outside the floodplain**: outside the Special Flood Hazard Area on the effective FIRM, at the
   10 m precision of these files. Shaded X and open water within the tolerance are reported beside the answer.

The finder should load every state of the manifest (a band near a border needs both states). Until the areas
of interest are rebuilt from the OpenStreetMap lines (§5.4), most bands near OSM-only lines will read "not
computed"; that is the honest answer, and the next build removes it. "Outside the floodplain" does not mean:

- **Not a flood determination.** Lenders and insurers use a Standard Flood Hazard Determination; owners amend
  a map by LOMA. LOMAs (single lots or structures removed from the SFHA by letter) are not in
  `S_FLD_HAZ_AR` and are not applied. LOMRs are: FEMA folds them into the NFHL.
- **Not "no flood risk".** The 0.2 % annual chance area (shaded X) is reported beside the result
  ("in the 500-year floodplain"), never used to exclude a site, as the owner asked. Unshaded X can flood too.
- **Effective maps only.** Preliminary FIRMs (the proposed next map, before it takes effect) are not read; a
  site outside today's SFHA can be inside the next effective map.
- **Edges are 10 m.** A site within 10 m of an `sfha` edge counts as touching it (rule 1, measured in metres
  at the site's latitude), which errs toward "in the floodplain". Where FEMA's digital map ends, the grown
  coverage can read as mapped up to about 20 m past the true edge.

"Flood data unavailable" is the answer outside the areas of interest (not computed), where no covering panel is
present (FEMA has no digital map there: paper-only or unmapped communities), and inside `zone_d`,
`not_included` or `unrecognised`. A site the reader sees should say which.

### 5.4 Areas of interest

The national NFHL is not needed whole: the finder asks about land within 2 miles of a line at most. `aoi`
buffers every line of 69 kV and up by 2 miles plus a 200 m margin, assigns each buffer to every state within
10 km of it, and dissolves per state. Until the OpenStreetMap lines land, the lines are the LBNL x HIFLD subset
(§2), about a fifth of US transmission mileage, so these files cover floodplain near those lines only, and the
finder answers "not computed" elsewhere (§5.3). The workflow's plan job takes `LINES_PARQUET` and the next
build covers the OSM network. The coverage files are statewide and do not depend on the lines.

- **Metric buffers, at each line's own latitudes.** Longitude is scaled by the cosine of the latitude, per
  band of at most half a degree of latitude (the bands overlap slightly, because GEOS's rectangle clip drops
  a segment lying exactly on the rectangle's edge). One cosine for a whole line left the 3.15-degree Leland
  Olds - Fort Thompson 345 kV line's buffer 104 m short east-west at its north end. Measured over all 13,084
  LBNL lines, due east and west of every vertex: worst shortfall 104 m before, 43 m with bands. The rest is
  the polygonal arc, not the latitude: GEOS rounds a join's arc to whole steps, so at `quad_segs` 8 a chord
  can cut about 37 m inside the true circle (on the worst line, 64 segments per quarter circle leave 11 m).
  The 200 m margin covers both.
- **States within 10 km** (`STATE_SLACK_M`). The state shapes are the Census 1:20,000,000 cartographic
  boundaries (median segment 7 km), which can sit kilometres off the true border. A buffer that touched no
  generalised shape would be in no state's flood files.
- **The lines input is checked** before any buffer: a GeoParquet `crs` other than OGC:CRS84 or EPSG:4326, a
  voltage column that is not numeric (OSM's `138000;69000` or `115 kV` must be parsed to one kV value first),
  a kV value above 1,500 (volts), geometries that are not lines, and coordinates outside lon/lat range (a
  projected file with no `crs`) each stop the plan with a message saying so. The LBNL asset parquet
  (`geom_line_wkt`, `attributes.voltage_kv`) is read as it is; a recorded three-line sample tests that path.
- **Provenance per state.** Each `aoi.parquet` row is a record derived from the lines buffered into that state
  and carries their `source_id`, `source_url`, `retrieved_at` and `licence`, not those of every line read.

### 5.5 Outputs: the finder's contract

R2, object store bucket (`R2_BUCKET`, not the public tiles bucket), under the pipeline's `data/` prefix:

```
data/normalized/context/us.fema.nfhl/<version>/aoi.parquet, aoi.json
data/normalized/context/us.fema.nfhl/<version>/sfha/<ST>.parquet
data/normalized/context/us.fema.nfhl/<version>/zones_other/<ST>.parquet
data/normalized/context/us.fema.nfhl/<version>/coverage/<ST>.parquet
data/normalized/context/us.fema.nfhl/<version>/states/<ST>.json, manifest.json
data/normalized/context/us.fema.nfhl/latest.json     (copy of the newest complete manifest)
```

`<version>` is the build time, `20261022T0441Z`. Read `latest.json`, then **each state's files at the keys
the manifest names for that state**: `states[ST].files[sfha|zones_other|coverage].key` and
`states[ST].aoi.key` (the state's row of that `aoi.parquet`). A state carried forward by a partial rebuild
(§5.6) keeps its own `version` and `prefix`, so one manifest can name files under several versions;
`r2_prefix` is only where this build's own files and the manifest live. `latest.json` moves only after every
state of a build succeeded and the quality gates passed. The local layout is the same under the data root
(`INFRAQUE_DATA_DIR`; `local_path` maps a key to it).

The manifest also carries `layers` (what each file holds, including that the zone files stop at the areas of
interest and the coverage does not), `finder_rule` (§5.3, in order), `reference_implementation`
(`FloodLayer.assess`), `states_built`, `states_carried`, `states_dropped` and `previous_version`.

Every file is GeoParquet 1.1: `geometry` (WKB, Polygon or MultiPolygon, OGC:CRS84 lon/lat; NFHL's NAD83
coordinates carried unchanged, 1-2 m from WGS84 in the contiguous US), a `bbox` covering struct, rows in
Z-order so row-group statistics prune by area.

| Column | `sfha/`, `zones_other/` | `coverage/` |
|---|---|---|
| `piece_id` | `<ST>:<FLD_AR_ID>:<FIRM_PAN>:<n>`, unique (FEMA reuses an `FLD_AR_ID` for several polygons of a panel: 26 times in Texas) | — |
| `category`, `sfha` | §5.2; `sfha` true only in `sfha/` | — |
| `zone`, `zone_subtype`, `sfha_tf` | FEMA's `FLD_ZONE`, `ZONE_SUBTY`, `SFHA_TF` | — |
| `fld_ar_id`, `dfirm_id`, `version_id` | FEMA's ids | `dfirm_id`, `version_id` |
| `firm_panel`, `panel_effective_date` | the FIRM panel the piece lies on and its effective date (each piece is split along panel edges); `firm_panel` is the MSC product id for a link to the FIRM | the panel itself, `panel_type`, `covers` (false for "Area Not Included" panels) |
| `state`, `area_m2` | postal code; area (local scale, within 0.5 %) | same |
| `nfhl_product`, `source_id`, `source_url`, `retrieved_at`, `licence`, `raw` | provenance: the state file, `us.fema.nfhl`, its URL, fetch time, the manifest's licence text, FEMA's attributes as JSON | same |

`aoi.parquet`: one row per state, `state`, `buffer_miles`, `margin_m`, `min_kv`, `line_count`, `area_m2`, and
the provenance of the lines buffered into that state (`source_id`, `source_url`, `retrieved_at`, `licence`).

### 5.6 Refresh

FEMA reissues a state file when a study or LOMR goes effective (29 of 52 files were under 30 days old on
2026-10-22). Siting does not need monthly floodplain: the workflow runs **quarterly** (04:41 UTC on the 3rd of
January, April, July and October) and on demand for chosen states. One job per state, at most three at a time
(each at most one range request per 2 s), a plan job for the areas of interest, and a publish job that writes
the manifest and moves `latest.json` only when every state succeeded. R2 credentials are the `production`
Environment's, as for the basemap; without them the plan job warns and the rest is skipped. The plan step
captures the state list before writing it out, so a failed plan fails its own step (inside
`echo "states=$(...)"` echo's exit status would have hidden it until the matrix failed to parse).

Publishing refuses, and leaves `latest.json` as it was, when:

- a state's SFHA row count fell by more than half since the previous build (a truncated download, not a
  remapping);
- a **full** build (the quarterly run, or a dispatch with no `states`) did not rebuild a state the previous
  manifest lists, unless `allow_dropped_states` names it. A plan that loses a state (the lines input changed,
  a state shape missed) would otherwise remove that state's flood data silently.

A **partial** rebuild (`workflow_dispatch` with `states=TX`) rebuilds those states and carries every other state
of `latest.json` forward unchanged, under its own version's keys, so the finder never loses a state between
quarterly runs. A version prefix stays needed for as long as `latest.json` names it: do not put an expiry
rule on these keys.

Every build starts with a `test` job: `pipeline/context/test_fema_nfhl.py` with shapely and pyogrio at the
versions the build installs, recorded fixtures only, no network. It runs with no secret: the R2 account and
bucket are set on the build jobs, not the workflow. The same job is the workflow's only job on a pull request
that touches the flood files or a file they read (the module, its tests and fixtures, `http.py`,
`registry.py`, `store.py`, `lbnl_transmission.py`, `data/sources.yaml`, the state shapes, `requirements.txt`,
the build script, the workflow). A failing test stops the build before any request to FEMA.

### 5.7 Measured: Texas, 2026-10-22 (this sandbox)

The development area is Texas, whole: its 2,031 MB file fit, because only 1,005 MB of it is fetched.

| Step | Result |
|---|---|
| Areas of interest | 1,398 LBNL lines (1,391 in Texas, 7 in neighbours within reach), 103,948 km² (about 15 % of the state), 95 parts, 23,213 vertices; 7.7 s; 355 kB |
| Fetch | 1,005 MB of 2,031 MB, 90 range requests, 404 s; `S_FLD_HAZ_AR` 235,967 polygons (1,270 MB table), `S_FIRM_PAN` 5,445 panels |
| Clip | 80,079 polygons read (all but unshaded X, intersecting the areas of interest) into 134,581 pieces on 2,850 panels; 264 invalid source polygons repaired, 7,810 slivers under 1 m² dropped; vertices 81.3 M to 5.1 M; 309 s, 3.35 GB peak memory |
| `sfha/TX.parquet` | 56,158 pieces, 18,409 km² (17.7 % of the areas of interest), 23.6 MB |
| `zones_other/TX.parquet` | 78,423 pieces: shaded X 3,144 km², open water 108 km², area not included 79 km², Zone D none; 24.4 MB |
| `coverage/TX.parquet` | 5,445 panels, all covering (Texas has no "Area Not Included" panel), effective dates 2001-08-23 to 2026-08-04; 0.28 MB |
| Total | about 12 minutes and 48 MB for the largest-but-three state |

**Simplification checked** on 400 random SFHA polygons (708 pieces), against the same pieces unsimplified,
distances in metres with longitude scaled: largest distance from an original vertex to the simplified edge
9.99 m (99th percentile 9.94 m, median 8.31 m); area +0.077 %; vertices 551k to 31k. The first build used GEOS's
topology-preserving simplifier, which broke the bound on 29 of the 708 pieces (up to 16.0 m, 1.57 times the
tolerance); the module now runs plain Douglas-Peucker ring by ring and keeps any ring that would collapse
(`simplify_polygonal`; four of those pieces are the fixture `tps_violations_nfhl_48.json`). All 56,158 SFHA
pieces are valid; 2,000 sampled lie inside the areas of interest plus 10 m. The sizes above include the
`piece_id` column, added after the run and measured by rewriting the same rows.

**National, estimated from Texas** by zip size (37,120 / 2,031 = 18.3): about 3.7 hours of job time, about
1.3 hours of wall time at three states in parallel plus setup, and about 0.9 GB of output with the LBNL areas
of interest; the OSM network will enlarge both. Not yet run in Actions (no R2 credentials here).

**Review fixes, 2026-10-23: Texas not re-run.** This machine had 2.8 GB of disk free, shared with other lanes,
and a Texas run writes 1.3 GB of raw tables, so the figures above are from the 2026-10-22 code. What the fixes
can move there: the read filter (no change, Texas has no unrecognised row); the structure repair of the 264
invalid polygons (it only adds back area the linework repair dropped, so the 18,409 km² is if anything low;
on Harris County's 16 invalid polygons the two agree within 1 m², §5.2); the 10 km state slack (more neighbouring lines in Texas's areas of interest, and Texas lines
in its neighbours'); banded buffers (a slightly larger area for tall lines). The first Actions run re-measures.
Measured this round, on recorded fixtures and the local LBNL parquet: the rule's grid test (§5.3), the buffer
shortfall (§5.4), and the memory of the clip-and-write step (§5.8 item 6).

### 5.8 Limits and open items

1. `shapely` is not in `requirements.txt`, so `ci.yml`'s core suite skips the test module (it names the
   skip in the log). The tests run in `flood-layer.yml`'s `test` job instead (§5.6). Adding `shapely==2.1.2`
   to `requirements.txt` would run them in the core suite too, and the finder will need shapely anyway to read
   these files. `pyogrio` (GDAL, for the state geodatabases) stays a build-time install; without it the
   county fixture is read with pyshp.
2. Rebuild the areas of interest from the OpenStreetMap lines when that lane lands (`LINES_PARQUET`).
   **Still open (2026-10-23, lane sf-golive):** the server now pulls this build (`fema_nfhl pull`, §2.7 limit 1),
   but `flood-layer.yml` passes no `LINES_PARQUET`, so its plan job buffers the LBNL lines (about a fifth of US
   transmission miles) and the site finder's bands along OpenStreetMap-only lines read "flood data unavailable
   (not computed)" and are left out at its defaults. The development run that measured 15,188 Texas bands
   (§7) had the OpenStreetMap areas of interest. The change, in the flood lane's files: when `LINES_PARQUET`
   is unset, the plan step reads `https://tiles.$BANKABLE_DOMAIN/grid/osm-power/latest.json`, fetches the
   `lines.parquet` it names (sha256 checked; its `voltage_kv` column is numeric kV, which `lines_from_parquet`
   accepts), and buffers that, falling back to LBNL only while the grid build is unpublished.
3. LOMAs are not applied; preliminary FIRMs are not read (§5.3).
4. FEMA's website policies (www.fema.gov, 403 here): read in a browser and add to the register row.
5. The MSC search form can change without notice; `STATE_PROBES` and `--product-file` are the fallbacks.
6. Memory: 3.35 GB for Texas; Florida's file is a third larger, and OSM areas of interest will be about five
   times the LBNL ones. Since 2026-10-23 each batch's pieces go to Arrow as they are made (repeated strings,
   such as the 1.5 kB licence on every row, dictionary-encoded until written) and the file is written one
   row group at a time, instead of every piece of the state held as Python objects. On a synthetic
   Texas-shaped workload with the real provenance (jagged polygons, 58 vertices kept per piece), peak memory
   of the clip-and-write step: 135,000 pieces, 916 MB before and 781 MB after; 270,000 pieces, 1,672 MB
   before and 1,206 MB after. Each further 135,000 pieces cost 756 MB before and 425 MB after, and that
   growth is what a 5x area of interest multiplies. The files hold the same table, metadata and row groups as
   before. What makes up the rest of Texas's 3.35 GB was not measured: the synthetic's source polygons have
   80 vertices, Texas's averaged about 1,000 (81.3 M over the 80,079 read), so per-batch work on large
   source polygons is the likely part. The state job runs on a standard runner: watch Florida and Texas in
   the first Actions run.
7. Texas was not re-run after the 2026-10-23 review fixes (disk; §5.7). The first Actions run re-measures it.

### 5.9 Assumptions

- The union of effective FIRM panels in a state's NFHL is where FEMA has a digital map. FEMA's own
  availability layer is an ArcGIS service robots.txt disallows, so it was not used to check this.
- A 0.2 % annual chance area is information for the reader, not a reason to drop a site (owner's instruction).
- Floodplain within 2 miles of a line is enough: the finder's largest adjacency is 2 miles.

## 5. Prospecting layers (resources lane, 2026-10-22)

Owner, verbatim: "Could we also track things like solar irradiance, windspeeds, oil/gas reserves and any other
aspect that goes into pre-origination (prospecting & feasibility?)". Decision row: `docs/00-PLAN.md`, 2026-10-22,
"Prospecting layers: resource, constraint and incentive data join the base map and the site finder, screening
grade only". This lane builds the data and the sampling; the map display and the site finder's use of it are
later lanes (5.6 is their contract).

**Everything served here is screening grade.** Every value carries `screening: "screening"` and `not`, one plain
sentence of what the value is not, and `provenance` (`source_id`, `source_url`, `retrieved_at`, `licence`,
`licence_id`, the dataset's own `vintage`, the credit line and the licence URL). A source whose row in
`data/sources.yaml` does not allow publication is answered `status: "withheld"` with no value and no credit.

### 5.1 Layers, sources and terms

Terms are quoted in full in `data/sources.yaml` (rows under "M. PROSPECTING LAYERS") and classified in
`docs/13-legal-data-rights.md` §6; each build re-reads the terms page and stops that source if a quoted clause is
gone (`pipeline/context/prospect/common.py` `verify_terms`).

| Layer id | What it is | Source (`source_id`) | Terms | Grid or geometry | Vintage |
|---|---|---|---|---|---|
| `solar_ghi`, `solar_dni` | Annual average daily global horizontal and direct normal irradiance, kWh/m²/day | NLR (formerly NREL) NSRDB PSM v3 GIS rasters (`us.nrel.nsrdb.psm3_annual`) | NLR data notice: free use and copying with the entire notice in every copy and credit to DOE/NLR/ALLIANCE; `derived_only` (values served with the notice; the raster stays in the private bucket) | 0.04° (about 4 km) | 1998-2016 average (Sengupta et al. 2018) |
| `wind_speed_100m` | Mean wind speed at 100 m, m/s | Global Wind Atlas v4.0, DTU with the World Bank Group (`global.dtu.global_wind_atlas`) | CC BY 4.0, confirmed on its own Terms of Use; the same Terms forbid automated access to the application, so **a person downloads the file** | 0.0025° (250 m) | GWA 4.0 long-term mean |
| `oil_gas_basins` | Lower 48 sedimentary basins with shale plays | EIA US Energy Atlas (`us.eia.atlas.sedimentary_basins`) | Public domain (EIA reuse page; acknowledgement with date) | polygons | Feb 2024 |
| `oil_gas_plays` | Tight oil and shale gas (low-permeability) plays | EIA US Energy Atlas (`us.eia.atlas.shale_plays`) | Public domain | polygons (they overlap: benches stack) | 2021 update |
| `oil_gas_reserves_state` | The state's proved reserves: crude oil and lease condensate (MMbbl), natural gas wet after lease separation (Bcf) | EIA-23L tables 6 and 8 (`us.eia.proved_reserves`), joined to the vendored Census state polygons | Public domain | state polygons; the full table (states, RRC districts, offshore areas) as `oil_gas_reserves_by_state.csv` | Year-end 2024, released 2026-03-25 |
| `energy_community_coal_closure` | IRA coal-closure census tracts and the tracts adjoining them | DOE NETL ArcGIS layers (`us.doe.netl.energy_communities`) | Creative Commons Attribution (no version named) with NETL's citation; every value repeats NETL's line that taxpayers may not rely on the data | tract polygons | as of 2024-06-07 |
| `energy_community_ffe` | Statistical areas (MSA/non-MSA, by county) meeting the fossil-fuel employment threshold; `is_energy_community` where they also meet the unemployment test | same | same | county polygons | as of 2024-05-31 (energy communities) and 2024-06-07 (threshold only) |
| `protected_lands` | PAD-US fee and designation features; `hard_constraint` = GAP status 1 or 2 | USGS PAD-US 4.1 (`us.usgs.padus`) | Public domain (ScienceBase rights) | polygons, simplified 0.00005° (about 5 m) | revision of 2025-03-31 |
| `water_stress` | Baseline annual water stress per HydroBASINS level 6 sub-basin: score 0-5, category, label, raw ratio | WRI Aqueduct 4.0 (`global.wri.aqueduct40`) | CC BY 4.0 (WRI's stated standard; the dataset page says "Creative Commons" only) | polygons | 2023 release, baseline 1979-2019 |
| `seismic_pga_2pct50` | Peak ground acceleration with a 2% chance of exceedance in 50 years, site class BC (Vs30 760 m/s), g | USGS 2023 NSHM, revision CONUS.2023.R2 (`us.usgs.nshm2023`) | CC0 | 0.05° nodes, nearest node served | data release 2026-08-10 |

Not built: **EIA oil and gas field boundaries** (requested; EIA's map pages answer 404, and none of the 130 items
EIA.gov owns on ArcGIS Online is a field outline: `us.eia.oil_gas_field_boundaries`, `verified: not_found`). Its
row is `reuse: unknown`, `publication: none` (corrected 2026-10-23 from open/raw-ok): no file was found, so its own
terms were never read, and EIA said some field shapefiles were held back for the data source's copyright.
Candidates are USGS assessment units or state agencies, each with its own terms (Texas RRC data is
`noncommercial`).

The proved-reserves table is the union of EIA's crude oil and natural gas sheets: Pennsylvania (95,135 Bcf, about
16% of US proved gas at year-end 2024) and New York (124 Bcf) are on the gas sheet only, Nebraska and South Dakota on
the crude sheet only (fixed 2026-10-23; before, gas-only states were dropped). `row_kind` separates states from
subdivisions, federal offshore areas, "Other States" and the totals; only states join to a polygon, and the layer's
provenance names the Census outlines (`geometry_source_id`) beside EIA's values.

### 5.2 What each value is not

The sentence travels with every value (`services/context_sample/catalog.py`):

- Solar: "A satellite-modelled 1998-2016 average over a 4 km cell, not a bankable resource assessment, which needs
  on-site measurement."
- Wind: "A modelled long-term mean wind speed on a 250 m grid, not a bankable resource assessment, which needs
  on-site measurement (a met mast or lidar)."
- Energy communities: "A screening map of IRA energy-community areas, not tax advice: DOE NETL says taxpayers may
  not rely on it to substantiate a tax return position."
- Basins, plays: an outline is not evidence of producible oil or gas at the site, of mineral ownership or of
  drilling activity. Reserves: a statewide total, not what lies under the site.
- Protected lands: a national inventory compiled from many managers, not a title or survey record; land outside
  the polygons can still carry easements.
- Water stress: a sub-basin index, not a water-rights or supply assessment. Aqueduct scores "Arid and Low Water
  Use" sub-basins 5 with category -1 (most of West Texas): show the label, not the bare score.
- Seismic: a probabilistic value for a reference site class, not a site-specific geotechnical or design value.

### 5.3 Build: Texas here, CONUS in GitHub Actions

`python -m pipeline.context.prospect.build --region texas|conus [--sources ...] [--snapshots DIR] [--out-dir DIR]`
runs one module per source (`pipeline/context/prospect/`: `nrel_nsrdb`, `gwa_wind`, `eia_oil_gas`,
`eia_reserves`, `netl_energy_communities`, `usgs_padus`, `wri_aqueduct`, `usgs_nshm`) and writes one compact file
per layer plus `layers.json`, the manifest the sampler reads (file, sha256, bytes, provenance, build summary).
The manifest is merged, not replaced: a source that fails keeps its previous entry, is listed under `failures`,
and the command exits 1.

Fetch routes, all through `pipeline.connectors.http.PoliteSession` (the platform User-Agent; robots.txt honoured
for pages, ArcGIS REST and range reads treated as APIs as elsewhere in the repo): NLR's two GIS zips (140 MB
each; only the annual raster is extracted); EIA's Atlas feature services and the reserves xlsx (124 KB); NETL's
ArcGIS server (EDX refuses the platform User-Agent and its robots.txt disallows resource downloads); the PAD-US
state or national geodatabase from ScienceBase; Aqueduct's 261 MB zip; and for NSHM only the one 10 MB `map.csv`
member of a 1.9 GB zip, by HTTP range requests (`remote_zip.py`). Raw downloads are working files in a temporary
directory, deleted as soon as the layer is written.

**Re-measured 2026-10-23: PAD-US and Aqueduct have no permitted automated route today.** ScienceBase's catalogue
route for the PAD-US zips (`catalog/file/get/<item>?name=...`), which answered the Texas zip on 2026-10-22, now
answers 404; the item JSON points at its new file manager (`sciencebase.usgs.gov/manager/...`), whose file and
download links answer a JavaScript application, and the S3 keys it names answer 403. The Aqueduct zip is on
`files.wri.org`, whose robots.txt is `User-agent: *` / `Disallow: /` (only Twitterbot allowed), so the polite
session refuses it. Both builders keep their automated route (it will work again if the hosts change) and fail
that source cleanly when it does not; until then each takes a file a person downloaded, like the wind file below
(`prospect/inbox/padus/`, `prospect/inbox/wri_aqueduct/`, step 2b of the build script).

**Wind is manual.** No code contacts globalwindatlas.info. A person downloads the United States "Mean wind speed,
100 m" GeoTIFF from the application in a browser and uploads it to the private bucket at
`prospect/inbox/global_wind_atlas/<name>.tif`, with an optional `<name>.json` `{"downloaded_at": "<ISO time>"}`
that becomes the value's `retrieved_at` (otherwise the file's modification time, and the provenance says so). The
same sidecar works for the PAD-US and Aqueduct zips. `data/sources.yaml` gives the wind row no probe block, so the
weekly probe never sends an automated request to the application either.

**GitHub Actions** (`.github/workflows/prospect-layers.yml`, monthly on the 3rd and on demand; region `conus` or
`texas`): installs `requirements.txt` plus pyogrio 0.13.0 and pyproj 3.7.2 (geodatabase reading, build only; each
pin has a wheel for the job's Python 3.11 -- pyproj 3.8.0 needs 3.12, which is why the first draft's pin could not
install -- and `test_build_script.py` holds the pins to their Requires-Python) and gdal-bin (only
`gdal_translate`, which clips the wind file to the region and re-encodes it as tiled Deflate so the pure-Python
reader never decodes a national LZW file), then runs `infra/scripts/build_prospect_layers.sh`:

1. seed the output with the served build, so a source that fails this month keeps last month's layer; only a 404
   on the pointer means "nothing served" -- any other error reading it (403, timeout) stops the run before
   anything is built, so a partial build can never pass the layer-count check against a served count of zero;
2. take the inbox files; 3. build; 4. check every file against its sha256 and refuse to publish fewer layers
   than are served;
5. upload to `prospect/builds/<region>/<build_id>/` in the **private** bucket (`R2_BUCKET`, never the public
   tiles bucket, because NLR's notice keeps the solar rasters out of public redistribution), then write the
   pointer `prospect/current/<region>.json` last;
6. copy the run records to the bucket's `data/runs/`.

The job stops green with a warning until the R2 credentials and bucket exist, as `basemap.yml` does. A source
failure turns the run red but the pointer still moves when the new manifest holds as many layers as the served one.

**Servers** follow the pointer with `python -m services.context_sample.sync [--region conus]` (environment: the
`R2_*` names the beta deploy already renders). It downloads a new build into `<PROSPECT_DATA_DIR>/<build_id>/`,
checks the manifest against the pointer's `manifest_sha256` and every file against the manifest, rewrites the
file paths, and replaces `layers.json` last; a running sampler reopens when the manifest's mtime changes. It keeps
the served build and the one it replaced (by role, so a pointer rolled back to an older build is kept) and
deletes the rest. Wiring the sync into `infra/scripts/deploy.sh` or the scheduler is for the coordinator (open
item 7).

### 5.4 Formats, and why no GDAL at serving time

The API containers carry numpy and pyarrow, not GDAL or shapely (ADR 0008), so both sides are pure Python:

- **Rasters** are single-band integer COGs written by `pipeline/context/prospect/cog.py`: uint16, 256 × 256
  tiles, Deflate with predictor 2, overviews halving to one tile, every IFD before the tile data and the smallest
  overview first, georeferenced by ModelPixelScale/ModelTiepoint with GeoKeys for EPSG:4326, `GDAL_NODATA`, and
  `GDAL_METADATA` carrying the scale and offset plus the provenance items (NLR's notice and Sengupta et al.'s
  citation inside the solar files). Stored precision equals the source's: NSRDB 0.001 kWh/m²/day, GWA 0.01 m/s,
  NSHM 0.0001 g. The build reads sources band by band (`rasters.read_scaled`), so a CONUS wind clip (about
  23,000 × 10,000 cells) needs about 470 MB, not 1.9 GB per float64 copy.
- **Polygons** are GeoParquet 1.1 (`geoparquet.py`): WKB MultiPolygon, 6-decimal coordinates, a `bbox` struct
  column (`xmin`/`ymin`/`xmax`/`ymax`, float64) declared as the bbox covering in the form the specification
  defines (`"xmin": ["bbox", "xmin"]`, so GDAL 3.9+ and DuckDB can prune with it; the first draft used top-level
  columns, which the specification does not define), rows sorted along a Z-order curve in row groups of 256 so a
  query reads one or two groups, then the attribute columns and the five provenance columns on every row (a row
  may carry its own `source_url`: the statistical-area layer joins two NETL services). The file's metadata also
  records the layer's **coverage** (`coverage.py`): the region box, or for a source fetched per state (NETL and
  PAD-US in the Texas build) the states and their outline from the vendored Census file. The writer streams
  (`write_geoparquet_features`): a national geodatabase never sits in memory as Python coordinates.
- PAD-US arrives from GDAL's Arrow stream in batches of 2,000, filtered by GDAL to the region box in the layer's
  own Albers CRS (so a CONUS build never decodes, reprojects or simplifies Alaska, Hawaii or the territories); its
  curved features (309 of Texas's 11,864 fee features, arcs in park and lake outlines) are re-read through
  pyogrio's row reader, which linearises them, so none is dropped. Twelve Texas features (under about 5 m across)
  collapse under simplification and are counted as `dropped_geometry`.
- `services/context_sample/geotiff.py` reads the COGs and the source rasters (classic and BigTIFF; strips or
  tiles; none, LZW, Deflate or PackBits; predictors 1-3), checked against four GDAL-written fixtures.
  `services/context_sample/vectors.py` prunes row groups by the statistics of the columns the file's covering
  names and runs a vectorised even-odd point-in-polygon test, exact for valid polygons with holes. Every read of a
  shared Parquet file goes through a per-layer lock: one pyarrow `ParquetFile` read from several threads at once
  raised `ReadRangeCache did not find matching cache entry`, returned wrong rows, or aborted the interpreter
  (reviewer's reproduction, 16 threads; the API serves this route from a threadpool). The GeoTIFF reader already
  held its lock across seek and read.

### 5.5 Sampling

`services/context_sample` (`sample`, `sample_line`, `SITE_FINDER_LAYERS`):

- **At a point**: a raster's cell value (`value`, rounded to the source's precision; `no_data` outside coverage);
  a polygon layer's containing features (`inside`, `features` with the layer's fields, up to 10), plus
  `hard_constraint` on protected lands and `qualifies` on the statistical-area layer. A polygon layer answers
  `no_data` (with `coverage` and a reason), not `inside: false`, at a point outside the area it was built for:
  in the Texas build, PAD-US and NETL in New Mexico.
- **Along a corridor**: the buffer around a GeoJSON LineString or MultiLineString (or a list of `[lon, lat]`) is
  covered by a square lattice in an equal-area (sinusoidal) plane per group of nearby parts, and each lattice
  point is kept by its distance from the line measured in a plane centred on the nearest piece of it (pieces about
  two buffers long), so the band is the true buffer at any length, direction or latitude (measured: within 0.05%
  of the buffer's distance at 2 and 10 miles on 210-mile diagonal lines at 26-28.5 N and 47-49.4 N; the previous
  single equirectangular plane was about 1.6% off at the ends of a 250-mile north-south line). Far-apart parts
  of a MultiLineString are laid out separately, and the work follows the band's area, not its bounding box, so the
  automatic spacing is never refused. The spacing aims for about 1,500 points but is never coarser than a sixth of
  the band's width (a long narrow corridor gets more points rather than a band one or two cells wide) and never
  under 30 m. A polygon layer's `share` is the share of points inside any feature (an estimate of the share of the
  buffer's area, error falling with spacing), with each feature's own `share`, and `coverage_share`, the share of
  points inside the area the layer was built for: below 1 the status is `partial` and the shares are lower bounds;
  at 0 it is `no_data`. A raster's `summary` is `mean`, `min`, `max`, `p10`, `p90` and `coverage_share` over the
  points with data. The answer states its point count and spacing (`method`). Limits: buffer at most 10 miles, line
  at most 250 miles and 5,000 vertices, at most 400,000 candidate cells (a caller-chosen `spacing_m` finer than
  that is refused with the smallest that fits; the automatic spacing coarsens itself instead).
- **Energy community, combined**: `energy_community` in both answers takes the two NETL layers together: at a
  point `qualifies` (a coal-closure tract or adjoining tract, or a statistical area that is an energy community
  in the list's year); along a corridor `share`, each lattice point counted once. Every area in the
  statistical-area layer meets the employment threshold; only `is_energy_community` areas qualify
  (`qualifying_share`), so the statistical-area layer's own `share` is not the incentive answer. The block names
  its `source_id` and, per layer used, the build's `vintage` and the dataset's own `dataset_as_of`
  (2024-06-07 and 2024-05-31 in the layers NETL serves), because Treasury revises the statistical-area list every
  year: in October 2026 the 2024 list is not the one that applies to a project placed in service now, and NETL's
  ArcGIS server still has only the 2024 layers (its `EAT_Online_2025_Update` layer 19 is dataset version
  2023.2, per the review of 2026-10-23). `status: "partial"` when only one layer could be served or a corridor is
  only partly covered (a lower bound).
- **Threads**: a `Sampler` is safe to share. The API serves the point route from FastAPI's threadpool, and the
  site finder may call `sample_line` from several threads; Parquet reads are serialised per layer, GeoTIFF reads
  per file, and decoded row groups and tiles are cached per process.

### 5.6 Contract for the site finder

Per corridor segment (a band along a line at the finder's buffer, 0.25-2 miles), call in process:

```python
from services.context_sample import SITE_FINDER_LAYERS, sample_line

result = sample_line(segment, buffer_mi, layers=list(SITE_FINDER_LAYERS))
```

`segment` is the segment's GeoJSON LineString (EPSG:4326). Keep segments at or under about 25 miles: the lattice
aims for about 1,500 points (more for a narrow band), so a longer segment is sampled more coarsely. One `Sampler`
may be shared across the finder's threads. Read, per segment:

| Purpose | Field | Use |
|---|---|---|
| Hard constraint | layer `protected_lands`: `hard_constraint_share` | share of the band on GAP 1-2 land; exclude or shrink the band; `features[].unit_name`, `manager`, `gap_status` to name it |
| Protected land of any kind | `protected_lands`: `share` | a fact, not an exclusion (GAP 3-4 is multiple-use land) |
| IRA bonus | top-level `energy_community`: `share`, `status`, `dataset_as_of` | "x% of the band is in an energy community (NETL data as of <the latest date in `dataset_as_of`, one per layer>; not tax advice)": render the dates from the answer, never a fixed year; `status: partial` means a lower bound |
| Coverage | every polygon layer: `coverage_share`, `status` | `partial` (part of the band is outside the area the layer was built for) means the shares are lower bounds: say "no data for x% of the band", never 0% |
| Solar resource | `solar_ghi`: `summary.mean` (and `min`/`max`) | kWh/m²/day |
| Wind resource | `wind_speed_100m`: `summary.mean` | m/s; `unavailable` until the manual file is uploaded |
| Water | `water_stress`: `features[0].label` (largest share first), `features[0].share` | show the label |
| Seismic | `seismic_pga_2pct50`: `summary.max` | g |
| Subsurface | `oil_gas_plays`: `share`, `features[].name`; `oil_gas_basins`: `features[0].name` | context, not a constraint |
| Every layer | `status`, `not`, `provenance`, `screening` | use a value when `status` is `ok`, or `partial` as a lower bound; show `not` beside it; credit each `provenance.source_id` (the API's `licence_summary` shows the shape) |

Never treat `withheld`, `unavailable` or `no_data` as zero. `solar_dni` and `oil_gas_reserves_state` are left out
of `SITE_FINDER_LAYERS` (DNI matters only for concentrating solar; reserves are statewide); ask for them by id
when needed. Measured cost on Texas (5.8): 12-317 ms per segment the first time its area is read, 10-56 ms warm,
so a corridor search that samples a few hundred segments takes seconds; the sampler caches decoded tiles and row
groups per process.

### 5.7 API

`GET /v1/context/sample?lat=&lon=[&layers=]` (`services/api/context_sample.py`, `api/openapi.yaml`
`getContextSample`, public tier, no database) returns `sample(lon, lat)` inside the attribution envelope, with
`layers_built` and a `licence_summary` crediting each source that supplied a value. Unknown layer ids, missing or
out-of-range coordinates and unknown parameters are 400. Corridors are not on the API; the site finder samples
them in process.

### 5.8 Measured on Texas (re-measured 2026-10-23 after the review fixes; this sandbox, 4 shared cores)

Build, 2026-10-23: EIA basins and plays, reserves, NETL and NSHM rebuilt live with the current code (terms pages
re-read) in 39 s: EIA 5.4 s, reserves 3.2 s, NETL 10.6 s, NSHM 4.6 s. PAD-US and Aqueduct could not be fetched
(5.3: ScienceBase 404, files.wri.org robots), so their 2026-10-22 Texas outputs (built by the same builders from the
real inputs) were rewritten row for row in the current GeoParquet layout with their coverage, and the two NSRDB
COGs (format unchanged) were carried over. The 2026-10-22 build of every layer except wind took 85 s from local
copies of the NSRDB, PAD-US and Aqueduct inputs: NSRDB 2.4 s for both layers, Aqueduct 21 s, PAD-US 38 s (11,864
fee and 80 designation features). The national PAD-US geodatabase is several times larger (1.52 GB zipped against
Texas's 261 MB); the workflow allows 180 minutes.

Values (`sample`, all layers; places approximate), unchanged by the fixes:

| Place | GHI / DNI kWh/m²/day | Basin; plays | Energy community | Protected | Water stress | PGA 2%/50 yr |
|---|---|---|---|---|---|---|
| West Texas solar, Pecos County near Fort Stockton (-102.95, 30.95) | 5.81 / 7.13 | Permian; Wolfcamp | no (West Texas nonmetro area meets the FFE threshold only) | no | Arid and Low Water Use | 0.084 g |
| Panhandle wind, Oldham County near Wildorado (-102.20, 35.21) | 5.45 / 6.89 | none | no (Amarillo MSA, FFE threshold only) | no | Extremely High (>80%) | 0.152 g |
| Permian, Midland (-102.078, 31.997) | 5.64 / 6.89 | Permian; Spraberry, Wolfcamp - Midland | no (Midland MSA, FFE only) | no | Extremely High (>80%) | 0.048 g |
| Gulf Coast, Port Arthur (-93.94, 29.885) | 4.90 / 4.94 | Western Gulf; none | **yes** (Beaumont-Port Arthur MSA) | no | Low - Medium (10-20%) | 0.051 g |
| Austin, Texas Capitol (-97.7404, 30.2747) | 4.99 / 5.26 | Western Gulf; none | no (Austin-Round Rock MSA, FFE only) | no | Medium - High (20-40%) | 0.036 g |

Every point: Texas proved reserves 19,644 MMbbl crude and lease condensate, 138,670 Bcf wet gas (year-end 2024);
`energy_community` names `us.doe.netl.energy_communities` and `dataset_as_of` 2024-06-07 (coal closure) and
2024-05-31 (statistical areas); the four FFE-threshold-only areas carry their own service's `source_url`; wind
`unavailable` (no Global Wind Atlas file yet). Hobbs, New Mexico (-103.14, 32.70), inside the Texas box: basins,
plays, reserves and water answer; PAD-US and both NETL layers answer `no_data` (outside Texas), and
`energy_community` is `unavailable`, not "no". The reserves table has 51 rows, Pennsylvania's among them (95,135
Bcf, `row_kind: state`, "no row on the crude oil sheet"). Checks: the seismic values equal the nearest nodes of
USGS's `map.csv` (0.1516, 0.0838, 0.0480, 0.0363 g); the clip's maximum, 0.712 g, is at (-98.40, 34.75), the Meers
fault in south-west Oklahoma inside the Texas bounding box. The Pecos GHI equals the NSRDB raster's 5.808.

Timing: first point in a new process 1.3 s (most of it parsing `data/sources.yaml` once and opening files; 3.6 s
on 2026-10-22 under heavier load); warm, all layers, median 2.7 ms, p95 11 ms (200 points). A point answer is
about 19 KB of JSON (NLR's notice is 2.4 KB of it, twice). Threads: the reviewer's reproduction on this build
(5 trials of 16 threads sampling 400 random Texas points from one fresh `Sampler`) gave 0 answers different from a
single thread's. Corridors with the 9 site-finder layers (first call in a fresh sampler at 0.25 mi / warm):

| Corridor | 0.25 mi buffer | 1 mi | 2 mi | Result at 1 mi |
|---|---|---|---|---|
| Pecos County, 20 mi east-west | 1,470 pts; 290 / 15 ms | 1,510; 12 / 10 ms | 1,478; 12 / 11 ms | GHI 5.81; plays 77%; protected 0% |
| Big Bend, 25 mi | 1,724; 235 / 43 ms | 1,502; 45 / 39 ms | 1,501; 87 / 46 ms | GAP 1-2 protected 100% (Big Bend National Park) |
| Panhandle, 30 mi, bent | 2,699; 151 / 26 ms | 1,500; 30 / 12 ms | 1,499; 16 / 12 ms | basin 25%; PGA 0.149 g |
| Houston Ship Channel, 10 mi | 1,493; 317 / 24 ms | 1,502; 69 / 25 ms | 1,500; 29 / 25 ms | protected 0.1% (3.0% at 2 mi); energy community 100% |
| Permian, 50 mi diagonal | 3,947; 309 / 56 ms | 1,503; 42 / 29 ms | 1,501; 32 / 21 ms | plays 72%; protected 6% |
| Pecos River into New Mexico, 25 mi north-south | 1,772; 135 / 28 ms | 1,527; 41 / 23 ms | 1,516; 26 / 27 ms | PAD-US and NETL `partial`, `coverage_share` 0.56; energy community `partial` |

The 0.25-mile Panhandle and Permian bands hold 2,699 and 3,947 points because the spacing is capped at a sixth of
the band's width (134 m). Output sizes, Texas: protected lands 7.3 MB, NETL statistical areas 2.4 MB and coal tracts
0.99 MB, Aqueduct 1.0 MB, plays 94 KB, basins 61 KB, reserves 30 KB plus the 18 KB table, DNI 72 KB, GHI 51 KB, PGA
42 KB, manifest 28 KB: 12.2 MB in all (the bbox struct and the coverage outline add under 1%). Raw inputs on this
machine on 2026-10-22: 708 MB of extracted files (the two NSRDB annual rasters, the Texas PAD-US and the global
Aqueduct geodatabases; each zip deleted on extraction), deleted once the clips were written; on 2026-10-23 only the
124 KB reserves workbook (deleted) and the 10 MB NSHM range reads.

### 5.9 Priority 3: wetlands, land cover and slope (design and Actions steps only)

Terms read 2026-10-22: Annual NLCD is CC0 (ScienceBase rights) and 3DEP is "free of charge and without use
restrictions" (USGS); both rows are open, and nothing is served until a layer is in the catalogue. NWI's pages
carry limitations but no licence statement, so `us.usfws.nwi` stays `unknown` (nothing published) until one is
quoted. All three build only in Actions; nothing national is downloaded on a development machine.

**Status (2026-10-23): design only.** No workflow step builds these yet: the GDAL commands below have not run
anywhere (this machine has no GDAL command-line tools), and an untested job that publishes nothing would only
look finished. The inputs are confirmed, so the next lane starts from known files: Annual NLCD Collection 1.2
land cover is ScienceBase item `697b9279b66b0197c3043cc3` ("Conterminous United States Collection 1.2 Land
Cover"), latest year `Annual_NLCD_LndCov_2025_CU_C1V2.zip` (1,457,983,950 bytes; listed 2026-10-23), which
has the same ScienceBase download problem as PAD-US (5.3) and so arrives through the inbox; the 3DEP 1
arc-second tiles are public objects such as
`https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/TIFF/current/n31w104/USGS_1_n31w104.tif`
(HEAD answered 200, 2026-10-23), readable by `/vsicurl/` range requests without a full download. NWI is not
fetched at all while its row is `unknown`.

- **Wetlands (`us.usfws.nwi`)**: per-state file geodatabases from FWS (the national file is too large for one
  runner). A matrix job per state streams the wetlands polygons with pyogrio (as PAD-US), keeps `WETLAND_TYPE` and
  the Cowardin `ATTRIBUTE`, simplifies to about 5 m and writes one GeoParquet per state; the manifest lists the
  state files with their bboxes and the sampler gains a multi-file polygon layer that opens only the parts a query
  touches. Sampled like PAD-US: `inside` and the wetland type at a point; `share` of the band and the share per
  wetland type along a corridor. Not-sentence: "A wetland mapped from aerial imagery on a biological definition,
  not a jurisdictional delineation under the Clean Water Act."
- **Land cover (`us.mrlc.nlcd`)**: the latest Annual NLCD land cover year (30 m, Albers). In Actions,
  `gdalwarp -t_srs EPSG:4326 -tr 0.0009 0.0009 -r mode` (about 90 m) straight to a tiled Deflate COG
  (`-of COG -co BLOCKSIZE=256`), then the provenance items with `gdal_edit.py -mo`; at CONUS size GDAL writes the
  COG, not `cog.py` (a 90 m grid is about 2.3 billion cells), and `geotiff.py` already reads GDAL's COG and BigTIFF
  output tile by tile. Sampled as a class code mapped to its NLCD legend at a point, and the share of the band per
  class group (developed, water, wetland, forest, shrub and grass, agriculture, barren) along a corridor.
  Not-sentence: "A 30 m satellite classification for one year, not a survey of current land use or zoning."
- **Slope (`us.usgs.3dep_slope`)**: the 1 arc-second DEM tiles from The National Map's staged products (one
  1° × 1° GeoTIFF each, read by `/vsicurl/` range requests). A matrix job per latitude band warps each tile to its
  UTM zone at 30 m, runs `gdaldem slope -p` (percent), aggregates to 90 m (`-r average` for the mean and a second
  band from `-r max`), warps back to EPSG:4326 at 0.0009° and writes a band COG; a final job mosaics the bands
  (`gdalbuildvrt` + `gdal_translate -of COG`) and stores percent slope as uint16 at 0.01%. Sampled as the value at
  a point; along a corridor the mean, p90 and the shares of the band above 5% and 10% (the usual solar and
  large-load grading thresholds). Not-sentence: "A slope computed from a 30 m elevation model and averaged to
  90 m, not a site grading survey."

### 5.10 Test fixtures (offline; `pipeline/context/prospect/fixtures/`, `services/context_sample/fixtures/`)

All small (113 KB in all) and recorded from the real sources except where marked: NSRDB GHI and DNI excerpts
around Pecos County, rewritten by GDAL in the source's own layout (float64, uncompressed strips, nodata 0); EIA
basin and play and NETL pages recorded from the services with server-side generalisation and trimmed to a few
features; sheets 6 and 8 of the real reserves workbook trimmed to a dozen areas, plus the real gas-only
Pennsylvania row and crude-only Nebraska row (added 2026-10-23, so the union of the two sheets is tested); PAD-US and Aqueduct features read
from the Texas and global geodatabases, simplified and written as GeoJSON; 99 rows of the real NSHM `map.csv`. The **wind fixture is synthetic**: a GDAL-written float32 LZW grid on the Global Wind Atlas lattice near
Amarillo (value = 5 + col × 0.01 + row × 0.1 m/s), because no Global Wind Atlas file was downloaded. The four
reader fixtures are one 37 × 23 grid written by GDAL 3.12.2 through rasterio with `compress=LZW` (strips),
`compress=DEFLATE, predictor=3, tiled, 16 × 16`, `driver=COG, compress=LZW, predictor=2, blocksize=16` (int16 with
scale 0.01) and `compress=PACKBITS, BIGTIFF=YES`. `pipeline/context/prospect/test_build_script.py` runs
`infra/scripts/build_prospect_layers.sh` end to end against a fake `aws` and then syncs and samples the result.

### 5.11 Limits and open items

1. **Wind is unmeasured.** The owner (or someone they name) downloads the Global Wind Atlas United States mean wind
   speed at 100 m GeoTIFF in a browser and uploads it with its `.json` download date to
   `prospect/inbox/global_wind_atlas/`; the next Actions run builds it. The Terms were read from the application
   bundle that renders the Terms page (it is client-rendered); a person should confirm the CC BY 4.0 clause on the
   rendered page at the same time. NLR's WIND Toolkit is the US alternative the plan names; not built.
2. **Solar rasters stay private** (`derived_only`): the map lane must not hand out the COG URL or tiles of it; a
   map layer of irradiance needs a derived product (classed areas, or server-rendered tiles carrying the notice),
   which is a question for the map lane and the legal lane.
3. **Energy communities are the 2024 layers**, the latest on NETL's ArcGIS server, and in October 2026 the
   2024 statistical-area list is not the one that applies to a project placed in service now. Whether a 2025 or
   2026 update is on EDX is a browser check (EDX refuses automated access). The dataset's own date shows on
   every value and in the combined `energy_community` block (`dataset_as_of`), and the finder renders it.
4. Aqueduct's licence version is inferred from WRI's standard; NETL's names no CC version (docs/13 §6 rows).
5. Field boundaries: not available from EIA (5.1).
6. The NSRDB vintage is 1998-2016; a newer NSRDB summary with GIS files would replace it when NLR publishes one.
7. ~~**Serving needs the sync wired**~~ **Closed for the site finder 2026-10-23 (lane sf-golive):** the worker's
   monthly `context_build` runs `python -m pipeline.context.prospect.pull` (a thin wrapper over the sync that
   picks the build from `GRID_LAYERS_REGIONS`: `texas` for a Texas-only server, `conus` for any other setting,
   and answers "not published" while that pointer does not exist; §2.7 limit 1) into
   `<data root>/normalized/prospect` on the worker's `connector_data` volume, before `site_finder_refresh`
   samples it. A Texas server follows `prospect/current/texas.json` only, never `conus` (item 9: the national
   build is unmeasured), and only a dispatch with region `texas` moves that pointer; the monthly scheduled run
   builds `conus`, so a Texas server's layers are as fresh as its last `texas` dispatch. Closed 2026-10-23 (lane parcels-wire): on the single host the api mounts the worker's `connector_data` volume
   read-only at the same path (`infra/compose/compose.single.yml`), so `GET /v1/context/sample` reads these files;
   the three-VM `compose.prod.yml` shares no volume and has no such mount.
8. No OpenStreetMap data is used by these layers; the "© OpenStreetMap contributors" rule applies to the map and
   site-finder lanes that draw lines and substations under them.
9. The national builds are designed and tested end to end against a fake bucket, but have not yet run in Actions:
   the first run's timings and sizes belong in 5.8.
10. **PAD-US and Aqueduct need a person's download** until ScienceBase's catalogue route returns or WRI's file
    host allows automated access (5.3): upload the Texas or national PAD-US zip to `prospect/inbox/padus/` and
    the Aqueduct zip to `prospect/inbox/wri_aqueduct/`, each with its `.json` download date. Without them the
    CONUS run reports both sources as failed and serves neither layer.
11. **Solar values at any point** (docs/13 §6 row): `derived_only` withholds the raster, but the point route
    returns a cell's value anywhere, which a client could repeat across the grid. It fits NLR's notice (copying
    with the notice) and is rate-limited, but it is looser than the class name; the legal lane confirms the
    reading or moves the row to raw-ok with the notice.
12. **Coverage outlines** are Census's 1:20,000,000 state shapes: within about a kilometre of a state line a
    point a state-filtered layer does cover can read `no_data` (never the reverse: a point inside one of the
    layer's own polygons always counts as covered). The CONUS build's coverage is the region box, so a point
    in Canada or Mexico inside that box reads as covered with no polygon (not protected, not an energy
    community); a states outline for CONUS (the 48 states and DC from the same vendored file) would close that
    if the finder ever samples near the borders.

## 7. Site finder (lane site-finder, 2026-10-23)

Owner decision 2026-10-22 (`docs/00-PLAN.md`): find land within a chosen distance of a 69 kV-or-higher line, near
a substation, out of the floodplain. Code: `services/site_finder/` (job), `services/api/site_finder.py` (API),
`web/site_finder.py` (page).

**Method.** Every OpenStreetMap power line (`us.osm.power`) of 69 kV and up, and every line that states no
voltage (flagged, shown only on request), is cut into `max(1, round(length / 0.5 mi))` equal segments per part.
Per segment the job stores (`site_candidate`, one row per segment per run, migration 0039): the nearest OSM
substation within 10 miles and its distance to the segment; the share of the segment's band in FEMA's 1%
annual-chance floodplain at 0.25, 0.5, 1 and 2 miles, with the share of each band FEMA's digital maps cover; the
county of the segment's midpoint (Census TIGER/Line 2026, `us.census.tiger_counties`; the vendored cartographic
boundary file when the TIGER file is not built); within 5 miles, retired and retiring plants (EIA-860M, MW and
owners), data-centre and large-load requests, the queue requests that name a grid connection point, and county
fiber availability; and the prospecting layers over the 1-mile band (§5 above, `sample_line`). A run replaces the
previous run's rows only when it finishes; the scheduler defers it after each context load
(`site_finder_refresh`, queue `resolve`, lock `site_finder`).

**Criteria and bands.** A segment passes when its line meets the voltage floor, its nearest substation is within
the distance chosen (1 to 10 miles), and, with the floodplain left out (the default), FEMA's digital maps cover its
whole band (cover >= 0.999) and at most 90% of the band is floodplain. Adjacent passing segments of one line part
form one band. The band's outline is its centreline buffered by the distance chosen, less the floodplain (cut on
request from the per-state SFHA GeoParquet; where the API server does not hold the flood files the outline is the
plain buffer and says so). "Leave out bands mostly on protected land" drops segments whose 1-mile band is more than
half GAP 1-2 land (PAD-US).

**Texas, measured 2026-10-23** (development store: Geofabrik Texas extract, FEMA NFHL for Texas, TIGER counties,
prospect build `tx2`): 21,233 lines, 55,243 line-miles, 120,699 segments (4,496 voltage not stated) in 1,966 s
(peak 1.2 GB). At the 1-mile band: 8,953 segments outside the floodplain, 68,733 partly in, 43,013 without flood
data, of which 40,755 (19,117 miles) are in areas with no effective digital FEMA map. At the defaults (69 kV,
1 mile, substation within 5 miles, floodplain out): **15,188 bands along 30,816 miles of line**; 15,362 miles that
meet the other criteria are left out for want of a flood map and counted on the page. On SQLite, warm, a page of
25 bands answers in 0.3-0.5 s, one band by id in about 0.04 s, the state-wide map view (2,000 longest of 15,188
bands) in 0.5 s.

**Texas, re-measured after the fix round (2026-10-23)**, same store, run recomputed with the stored vertices and
per-request entries: the same 120,699 segments in 2,051 s (peak 1.2 GB; the flood files read once, one layer
built), the same 15,188 bands along 30,816 miles at the defaults. The default sort now leads with the longest
500 kV bands (15.5, 13.0 and 7.6 miles) instead of switchyard stubs. On SQLite, in process: the first request after
a run 5.5 s (the store-wide window query and facets, once per run and criteria but the voltage floor; a new floor
afterwards 0.7 s), a warm page of 25 bands 0.45-0.8 s with outlines and 0.57 s without (the page's list), sorted
by substation 0.25 s, one county 0.24 s, one band by id 0.06 s, the state-wide map 0.46 s, an Austin view 0.11 s.
Of the first 1,000 bands by substation distance with the floodplain in, 175 name a grid connection point, every
one from requests within 5 miles (at most 2 counted).

**Requests at the caller's tier (fix round, 2026-10-23).** Nothing derived from a proposal is stored but its id
and its distance: a segment keeps each large-load request within 5 miles as (proposal, miles) and each queue request
naming a grid connection point as (proposal, point, miles), whatever their visibility. The API keeps a request only
when the caller's tier may see it (`proposal_visibility_filter`: published, out of the free tier's delay, from a
source the tier may read) and may be served its exact location (`location_exact_permitted`, the nearby-proposals
rule), and takes the point's nearest distance, queued MW and count from those survivors only. Before this, a
point kept public by one request 200 miles away showed the distance of a hidden request a mile from the band, and
the far request's MW as if it were nearby. A band's retiring MW is the sum over the distinct visible plants near
any of its segments, the figure shown and sorted on alike. Bands are drawn from the vertices the run stored, so a
context load that moves a line cannot misplace a band the run measured.

**On the one-server beta (lane sf-golive, 2026-10-23).** The run happens inside the worker process
(`infra/scheduler/jobs.py` `site_finder_refresh_job`), after the monthly pulls and load (§2.7 limit 1). *Time
fits:* `SITE_FINDER_TIMEOUT_S` is 3 hours (`infra/scheduler/app.py`) against the 1,966-2,051 s measured above, so
a host about five times slower than the development machine still finishes; the run is single-threaded, and the
worker has 1.5 CPUs (`compose.prod.yml`). *Memory does not fit yet:* the 1.2 GB peak above is more than the
worker's container limit, `mem_limit: 1g` in `compose.prod.yml`, which `compose.single.yml` does not override.
Before this lane the beta held no `us.osm.power` rows, so the run computed nothing; once the pulls land Texas's
lines it computes everything, and at 1 GB the kernel would kill the worker with every job in flight, and
`queue_maintenance.recover_stalled_jobs` would start the run again up to three times a month. The change needed,
outside this lane's files: `compose.single.yml` worker `mem_limit: 3g` (the host's limits then total 6.25 of its
8 GB). Until it is deployed, `site_finder_refresh` reads its container's limit from the cgroup
(`jobs.container_memory_limit`) and, below `SITE_FINDER_MIN_MEMORY_BYTES` (2 GiB: the 1.2 GB peak plus the
worker's own memory and one concurrent job), does not start: it logs a warning naming the limit and the fix and
reports `skipped: memory`; the pulls and the load still run, and the API keeps the run it had (none on the beta
today). Assumption: the development peak (one process, SQLite store) is close to the worker's; the first run on
the beta should record its own peak beside the figures above.

**Not what it is.** A screening list, not a site assessment: it cannot see spare substation capacity, zoning,
ownership, easements or water rights, and the floodplain shares are not a flood determination (§5). Every value
the API serves carries its source quartet; a value whose source the register gates is withheld, never estimated.

## Assumptions

- Site finder (§7): half-mile segments, a 10-mile substation search and a 5-mile context radius are the owner's
  2026-10-22 brief turned into fixed numbers; a band is "outside the floodplain" only where FEMA's digital maps
  cover all of it (0.999) and at most 90% of it is SFHA, so a band beside a mapped river still passes with the
  floodplain cut out. Where FEMA has no effective digital map (about a third of Texas line-miles), the finder
  leaves the band out by default rather than guess.

- A queue's point-of-interconnection text names a substation or bus, sometimes with a bus number and voltage
  ("59903 Bearkat 345kV"), sometimes a line tap ("Cortland - Fenner 115kV"). A line tap has no single
  substation; it is kept as text and not placed.
- A point of interconnection is where a project connects, not where it is built. Placing a proposal at its
  substation would be wrong; the point is its own record, linked from the proposal (the same rule
  `docs/21` §3.7 applies to NESO connection sites).
