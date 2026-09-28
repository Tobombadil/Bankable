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

**Top 15 points by active queued MW (public tier).** All 15 are NESO points:

| # | Point | Active MW | Active projects |
|---|---|---|---|
| 1 | Alverdiscott 400kV | 13,365.9 | 16 |
| 2 | Creyke Beck 400kV | 9,078.4 | 7 |
| 3 | Norwich Main 400kV | 8,354.0 | 7 |
| 4 | Branxton 400kV | 6,976.6 | 7 |
| 5 | Grimsby West 400kV | 6,555.0 | 7 |
| 6 | Longside 400kV | 6,500.0 | 6 |
| 7 | Trent Valley South Connection Node D 400kV | 6,420.0 | 4 |
| 8 | East Claydon 400kV | 6,175.0 | 7 |
| 9 | Navenby 400kV | 5,569.9 | 9 |
| 10 | Birkhill Wood 400kV | 5,150.0 | 5 |
| 11 | Cheshire Connection Node A 400kV | 5,130.0 | 4 |
| 12 | Greens 400kV | 5,100.0 | 4 |
| 13 | South Anglia Connection Node C 400kV | 5,074.0 | 6 |
| 14 | Sizewell 400kV | 5,010.0 | 2 |
| 15 | Shurton 400kV | 5,010.0 | 2 |

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

## 2. Substations and transmission lines

*Pending: lane G2.*

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
| 2 | EPA ECHO ICIS-Air national download (`us.epa.echo.icis_air`) | https://echo.epa.gov/files/echodownloads/ICIS-AIR_downloads.zip | zip of CSV, 70 MB | national; NAICS 518210 or a "data center" name selects 537 facilities in 43 states (VA 144, IL 45, CO 35, GA 31, OH 24, TX 12); 21 Planned + 3 Under Construction (15 of those in Georgia) | weekly | name, street address, county, ZIP, NAICS, operating status; no coordinates in this file (FRS join needed); no MW | echo.epa.gov allows `/files/` (Crawl-delay 10); the REST API host `echodata.epa.gov` answers `Disallow: *` | US federal work, 17 U.S.C. §105 | **Next build.** Public domain and national. Thin before construction, and blind where states do not report minor sources. `PGM_SYS_ID` = DEQ `PLA_ICIS_ID`, so it joins to #1 deterministically |
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
