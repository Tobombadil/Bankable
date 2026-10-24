# 28 — Filter and tag audit (2026-10-23)

Owner, 2026-10-23: "go over every drop down and filterable tag. If a value/tag produces a zero result then look into
why that is. If it's not a bug and we are missing data, then go find some data sources and add the data. If that
data doesn't exist then remove the tag and do a pass of all the data to make sure we have good tags for all the data
we do have and the names of the tags make sense."

This is the audit and the plan. No product code changed. The machine-readable twin (every control, value, count,
class, bug, source, tag set) is `filter-audit.json` in the lane's scratch folder; the coordinator launches fix lanes
from §10.

## 0. Verdict

- **45 controls, 314 distinct values** across the map, Projects, RFPs & funding, Companies, Infrastructure, grid
  points, Prices, Site finder, Alerts, Search and `GET /v1/meta/vocabularies`.
- **47 values return zero** under their page's defaults: 13 bugs, 28 missing data, 4 to remove, 1 to hide until
  data, 1 already hidden. **30 return one or two**: 26 are small but real, 2 missing data, 1 bug, 1 to remove.
- **The worst defect is not a zero but a false count.** On RFPs & funding every Technology option returns at least
  82 notices, because untagged notices match every technology (B-1): "Offshore wind (82)" holds no offshore wind
  notice. And of the 197 "Solar PV" notices in the default view, a seeded hand-labelled sample finds 1 of 25 is a
  solar project (B-3, B-4); 17 of the other 24 are contracts to buy electricity or heat.
- **Five proposal kinds can never hold a record** as the classifier is written (B-2). Nuclear and hydrogen are
  technologies, not kinds; transmission, pipeline and LNG are missing data with public-domain sources (§5).
- **Two connected registers have never loaded in production** (CAISO, NYISO queues), so a hard-coded Queue select
  offers two empty values (B-10); EPA Class VI and the Permitting Dashboard, which would fill Carbon storage and
  Transmission, fail the same way.
- **Tags overlap across pages**: "Solar" (projects) and "Solar PV" (RFPs), "Gas, combined cycle" and "Natural gas",
  "Storage" and "Battery storage". One technology family set (§8.1) for projects, plants, companies and RFPs fixes it.
- **UI rule**: live counts per option, zero options hidden except the one in force. It is right for this site under
  five conditions (§9), with `include=facets` on every list endpoint computed at the reader's tier.

## 1. Evidence and method

| What | Measured |
|---|---|
| Register pulled (public tier, saved by the first run of this lane) | 6,633 proposals (34 pages), 1,809 opportunities (10 pages, every status), vocabularies, 42 public sources; 23:16–23:21 UTC, 46 requests |
| Live pages loaded | 38, 23:21–23:29 UTC (one 503 on `/prices?market=ERCOT`, then 200) |
| Further requests this run | 34 organisation count probes (`include=count&limit=1`), 23:53 UTC, sequential; RateLimit-Remaining stayed 27–59. No more pages loaded |
| Offline reproduction | Counts computed from the saved rows equal every live page checked: `/proposals` 5,629; `/opportunities` 510; `technologies=solar_pv` 279, `metering` 83, `ccs` 82, `wind_offshore` 82, `coal` 88; `status=frozen` 0, `awarded` 474, `all` 1,014; `kind=rfp` 1, `auction` 0; `/proposals?kind=nuclear`, `transmission`, `hydrogen` 0; `technology=hydrogen` 2 |
| Relevance verdict | Recomputed with the repo's own `services/api/opportunity_relevance.py::is_relevant_row` over the saved fields |

Page defaults the counts use, read from the code: **proposals** (map and list) `lifecycle_state` in announced,
filed, studied, permitted, contracted, under construction, and `listed=true` (`web/viewmodels.py:337`,
`web/unlisted.py:71`); the map's count line counts every placement grade, while it draws `exact,region`.
**Opportunities** `status=open,announced` and `relevance=energy` (`web/viewmodels.py`
`OPPORTUNITY_PAGE_DEFAULT_STATUSES`, `web/app.py:1151`). **Companies** `scope=group`.

Production state that explains several zeros: every proposal was first seen 2026-10-19 to 2026-10-22, no source link
has `gone_at`, and the public proposal sources are six: EIA-860M (2,311 rows), NESO TEC register (2,227), ERCOT GIS
(1,758), EPA ICIS-Air data centres (517), Virginia DEQ (206), Texas RRC Class VI (20). `us.iso.caiso.gen_queue`,
`us.iso.nyiso.gen_queue`, `us.epa.class_vi` and `us.permits_dashboard` are implemented and public with
`freshness: never` (`/v1/sources`, 23:20 UTC).

## 2. Inventory of controls

Option source is where the list of choices comes from; "vocab" is `GET /v1/meta/vocabularies`.

| Page | Control | URL param → API param | Option source |
|---|---|---|---|
| Map `/`, Projects `/proposals` | Technology | `technology` → `technology` | vocab `technology`: `SELECT DISTINCT` over every row, any tier (`services/api/app.py:977`); words `services/labels.py` (`web/app.py:529`) |
| Map, Projects | Kind | `kind` → `kind` | vocab `proposal_kind`, fixed list (`services/api/app.py:849`); words `web/labels.py` (`web/app.py:524`) |
| Map, Projects | Jurisdiction (text box) | `jurisdiction` → `jurisdiction`, exact | free text (`home_map.html:59`, `proposals_list.html:36`) |
| Map, Projects | Status (6 boxes) | `lifecycle_state` csv → same | `web/viewmodels.py` `PROPOSAL_STATUS_GROUPS` |
| Map, Projects | Include proposals no register lists | `unlisted=include` (web only) → drops `listed=true` | `web/unlisted.py:82` |
| Projects | Capacity min/max | `capacity_mw[gte]`, `[lte]` → same | number inputs |
| Projects | Schedule | `slip_bucket` → same | vocab `slip_bucket` |
| Map | Location precision (3 boxes) | `placement` → same | hard-coded (`home_map.html:109`) |
| Map | Existing assets, Retired & retiring plants | `layers` (web only) → `/v1/assets/geo` | hard-coded |
| Map | Asset types (8 boxes) | `asset_type` → same | `web/app.py:141` `HOME_MAP_ASSET_TYPES` |
| Map | Plant type | `plant_technology` (web only) → `technology` csv of the family | `web/labels.py` `PLANT_FAMILY_LABELS`, `map.js:87` |
| RFPs & funding | Kind | `kind` → `kind` | vocab, then one `limit=1` probe per kind; empty kinds dropped (`web/app.py:1070`) |
| RFPs & funding | Status | `status` → `status` | vocab `opportunity_status` plus "Open and announced", "All" (`opportunities_list.html:31`) |
| RFPs & funding | Technology | `technologies` → `technologies`, any-of, empty array matches all | vocab `opportunity_technology` = `OPPORTUNITY_TECHNOLOGIES` (`pipeline/connectors/opportunity.py:58`) |
| RFPs & funding | Jurisdiction (text box) | `jurisdiction` → exact | free text, placeholder "e.g. US-AZ" |
| RFPs & funding | Energy-related · All notices | `relevance` → same | `web/app.py:1085` |
| Companies | Type | `type` → same | vocab, probed per type, cached 15 min (`web/organizations.py:1030`) |
| Companies | State | `state` → any-of over stats states | all 51 `REGION_NAMES` (`web/organizations.py:1042`) |
| Companies | Technology | `technology` → any-of over stats technologies | vocab proposal `technology` (`web/organizations.py:1043`) |
| Companies | Role, Has projects in development | `role`, `has_pipeline` → same | `web/organizations.py:762` |
| Infrastructure `/assets` | Type | `asset_type` → same | `/v1/assets/geo` totals, types with a count (`web/assets_pages.py:126`) |
| Infrastructure | State (text box) | `state` → same | free text |
| Grid points | Queue | `iso` → same | hard-coded ERCOT, CAISO, NYISO, NESO (`web/interconnection_points.py:52`) |
| Grid points | Kind | `kind` → same | `web/interconnection_points.py:53` |
| Grid points | Jurisdiction (text), Active MW at least, Name | `jurisdiction`, `min_active_mw`, `q` | free text, number |
| Prices | Market | `market` → same | `web/prices.py:58`, filtered to the markets the API lists |
| Prices | Kind | `kind` → same | hard-coded Hubs, Zones |
| Site finder | Voltage, distances, acres, owners, 3 boxes, State, County | `min_kv`, `adjacent_mi`, `max_substation_mi`, `min_acres`, `max_owners`, `exclude_flood`, `include_unknown_voltage`, `exclude_protected`, `state`, `county` | `web/site_finder.py:59-65` and the run summary |
| Alerts | Delivery mode; filter summary | `delivery_mode`; the saved query | `web/alerts.py:64`, `:124` |
| Search | Result sections | `q` on four lists | `search.html:13,25,39,57` |
| API | Vocabularies | — | `services/api/app.py:944` `_vocab`, `:975` |

## 3. Zero and near-zero values

Counts are what the option returns under the page's defaults. Classes: **bug** (data exists, the filter misses it,
or the option cannot work), **missing** (the tag is right, no records), **remove** (no data will exist, or the value
duplicates another dimension).

### 3.1 Map and Projects

| Control | Value | Default view | Any state | Class | Why |
|---|---|---:|---:|---|---|
| Kind | Transmission | 0 | 0 | missing | No public source emits it; Permitting Dashboard (transmission sector) never ran in production |
| Kind | Pipeline | 0 | 0 | missing | No connector produces it; no rule in `TECH_RULES` |
| Kind | LNG | 0 | 0 | bug | `pipeline/normalize.py:209` files LNG as (gas_other, generation) |
| Kind | Nuclear | 0 | 0 | bug | 12 nuclear projects exist (18 any state), all kind=generation (`normalize.py:193`) |
| Kind | Hydrogen | 0 | 0 | bug | 3 hydrogen projects are kind=generation (`normalize.py:207`) |
| Technology | Hydrogen | 2 (1 drawn) | 3 | missing | NESO "Hydrogen", "Demand;Hydrogen", ERCOT "Hydrogen - Other" |
| Technology | Not stated | 1 | 1 | remove | One ERCOT row with no fuel |
| Technology | Gas, steam turbine | 3 | 4 | remove (merge) | Too fine; into "Gas, other" |
| Technology | Waste to energy | 4 | 5 | remove (merge) | Too fine; into "Biomass and waste" |
| Technology | Tidal and wave | 12 (3 drawn) | 12 | ok | 9 NESO rows have no location |
| Status | Withdrawn or cancelled | 2 | 2 | missing | ERCOT drops inactive projects; EIA-860M read from its Planned sheet only; NESO removes withdrawn rows |
| Include no-longer-listed | include | 0 hidden | — | hide until data | All 6,633 are listed; no link has `gone_at` |
| Jurisdiction | "TX", "us-tx", "Texas" | 0 | — | bug | Exact match; "US-TX" returns 1,948 |
| Jurisdiction | "UK", "GB-ENG" | 0 | — | bug | Only "GB" matches (1,842) |

Every other Technology value has 9 or more records in the default view; Large load shows 73 of 582 because 487 data
centres are built (EPA ICIS-Air "Operating"), which the count line already says. Location precision: at a site 2,301,
by county or state 2,332, no location 996. Asset types and plant types are all non-zero (smallest: 8 LNG terminals,
56 nuclear plants).

### 3.2 RFPs & funding

| Control | Value | Default view (tagged) | Shown today | Class | Why |
|---|---|---:|---:|---|---|
| Technology | Offshore wind, Pumped hydro storage, Tidal and wave, Carbon capture (CCS), Microgrids | 0 | 82 each | missing (and B-1) | No notice carries them in any status; the 82 are untagged notices |
| Technology | Smart metering | 1 | 83 | ok, merge into family | |
| Technology | Hydrogen, Geothermal | 2, 1 | 84, 83 | ok | 4 and 9 in any status |
| Technology | Coal (not offered) | 6 | 88 by URL | bug | Stored on 28 notices, not in the vocabulary; 26 are wood fuels (B-5) |
| Kind | Auction, Loan programme | 0 | hidden | missing | No connector emits them |
| Kind | Programme | 0 | hidden | remove | No source; overlaps FOA and loan programme |
| Kind | Request for proposals (RFP) | 1 | 1 | bug | 6 of 16 utility RFPs fail the relevance gate (B-7) |
| Status | Frozen, Reinstated | 0 | offered | remove | No source emits them |
| Status | Cancelled | 0 | offered | missing | No connector maps a cancellation |
| Status | Unknown | 0 | offered | remove | 1 non-energy notice |
| Jurisdiction | "US-AZ" (the placeholder) | 0 | — | bug | No notice is in Arizona |

### 3.3 Companies, grid points, prices, site finder

| Page | Control | Value | Count | Class | Why |
|---|---|---|---:|---|---|
| Companies | Technology | Large load | 0 | bug | All 582 data-centre records have no sponsor (B-9) |
| Companies | Technology | Coal, Gas other (not offered) | 372, 46 | bug | Held in companies' sets, absent from the select (B-8) |
| Companies | Type | 10 of 12 types (developer, utility, ipp, ...) | 0 | missing | 13,854 of 13,856 companies are "other"; agency 2. The page already drops empty types |
| Grid points | Queue | CAISO, NYISO | 0 | bug | Registers implemented, never loaded in production (B-10) |
| Prices | Market | CAISO | 0 | ok | Already dropped from the select; the description still names it (B-20) |
| Prices | Market | Great Britain | 1 | ok | One system price by design |
| Site finder | every control | — | 0 | missing | "The site finder has not been computed yet" (live, 23:27 UTC) |

Companies probed: technology biomass 487, gas_cc 786, gas_ct 837, solar 4,568, storage 1,869, wind 1,196, hydrogen 3,
marine 3, unknown 3; role operator 6,914, owner 3,028, sponsor 3,474; has projects 3,159; state DC 21, RI 108 (the two
smallest by proposals; every state holds EIA-860 plants). Grid points: ERCOT 1,085, NESO 1,224; substation 1,686,
line tap 542, unparsed 81.

## 4. Bugs

| # | Defect | Where | Evidence | Fix |
|---|---|---|---|---|
| B-1 | Untagged notices match every technology | `services/api/records.py:1212` | 82 of 510 default-view notices are untagged; every option returns them | Match tagged notices only; an explicit `all_source` tag (set by the RFP registers) matches everything. Lands with B-3/B-4, which raise the untagged share from 82 to 247 in a simulation |
| B-2 | Five kinds no record can carry | `web/app.py:524`, `services/api/app.py:849`, `pipeline/normalize.py:193,207,209` | §3.1 | Options from facet counts; drop nuclear and hydrogen kinds; LNG rule to (lng_terminal, lng) |
| B-3 | Electricity-supply contracts tagged Solar PV | `pipeline/connectors/eu_ted_api/connector.py:82` | `"093": "solar_pv"` catches CPV 09310000 "Electricity": 138 of 197 Solar PV notices carry no solar CPV; sample precision 1/25 | Map `0933` only |
| B-4 | CPV label words in TED titles are classified | `eu_ted_api/connector.py:270` | Titles read "Country – CPV label – buyer title"; 34 of 36 Nuclear notices are tagged from "Electricity, heating, solar and nuclear energy"; 2 are nuclear | Classify the buyer's own segment and the codes |
| B-5 | Wood fuels tagged coal; coal not filterable | `eu_ted_api/connector.py:100`, `opportunity.py:58` | `"09111"` catches 09111400 wood fuels: 26 of 28; 0 coal CPVs | 091111/091112 coal, 091114 biomass |
| B-6 | Transmission keywords over-match | `pipeline/connectors/opportunity.py:42` | Sample: 12 of 25 are grid work (rail electrification, roads, cycle paths, a cable car tagged) | Require voltage, substation, transformer, switchgear or grid words, or a grid CPV |
| B-7 | Relevance gate drops utility RFPs | `services/match/relevance.py:198` | 6 of 16: "2025 SPP Capacity RFP" (x2), "2026 All-Source RFP" (x2), "2023 Accredited Deliverable Capacity RFP", "2024 RFPs" | `us.rfp.*` notices are energy-relevant by source; "all-source" and "capacity" set `all_source` |
| B-8 | Companies Technology uses the proposal vocabulary | `web/organizations.py:1043` | Coal (372 companies), gas other (46) cannot be chosen | Options from the companies' technology facet, by family |
| B-9 | No company linked to a data centre | ICIS-Air and Virginia DEQ connectors | 582 load records, sponsor null; 248 names start with a known operator (Amazon 73, Microsoft 28, Equinix 17, QTS 15, CyrusOne 14) | Resolve permittee or name operator as sponsor (entity resolution, docs/22) |
| B-10 | Empty queues offered on grid points | `web/interconnection_points.py:52` | CAISO 0, NYISO 0 (live) | Ops: fix the production fetch (`source.last_error` now records the edge's answer); options from facets |
| B-11 | Free-text jurisdiction, exact match | `proposals_list.html:36`, `home_map.html:59`, `opportunities_list.html:39`, `interconnection_points_list.html:33`, `assets_list.html:74` | §3.1, §3.2 | State or country select with names and counts; API case-insensitive, accepts "TX" |
| B-12 | Mechanical vocabulary labels | `services/api/app.py:944` | "Gas Cc", "Ccs", "Lng", "Rfp", "Foa", "Bess", "Solar Pv", "Ipp", "Aeci", "Ercot" | Labels from `services/labels.py` |
| B-13 | Four empty statuses offered | `opportunities_list.html:34` | frozen, reinstated, cancelled, unknown: 0 | Options from facets, as Kind already is |
| B-14 | "Public Comment" maps to unknown | `pipeline/connectors/us_tx_rrc_class_vi/status_map.yaml:68` | 1 well; the map covers "Comment Period for Draft Pemit" only | Add it as studied |
| B-15 | Grid equipment and CHP filed as Other | `pipeline/normalize.py:213` | NESO "Reactive Compensation" 17, "CHP" 12: 29 of 45 Other | Reactive compensation → (grid_equipment, transmission); CHP its own tag |
| B-16 | Alert summary prints tokens | `web/alerts.py:124` | "load", "filed, studied", "1 to 3y" | Label every value |
| B-17 | World Bank codes as countries | `pipeline/connectors/mdb_worldbank_procnotices/connector.py:255` | 3W 10, ZR 5, 6R 3, 3E 2 | ZR → CD; regional codes to a region token |
| B-18 | Site finder reachable, never computed | `web/site_finder.py:883` | Live page says so | Run it in production; 404 until then |
| B-19 | "No longer listed" box changes nothing | `web/unlisted.py:82` | 0 hidden | Show only when the count is above zero |
| B-20 | Prices description names CAISO | `web/templates/prices.html:4` | Hard-coded sentence; CAISO has no hubs and is dropped from the select (`web/prices.py:305`) | Build it from `notes.markets` |

Labelled samples (seeded, `random.seed(20261023)`, judged from title and CPV): Solar PV 1 correct of 25 (0.04;
Wilson 95% CI about 0.01–0.20), Transmission 12 of 25 (0.48; about 0.30–0.67). Ids and labels are in
`filter-audit.json` `eval_samples`, for `data/eval/` in lane F3.

## 5. Missing data: candidate sources

Only public primary sources; none of the private aggregators in CLAUDE.md or the lane brief. Yields marked
"estimate" were not measured.

| Tag | Now | Candidate sources (terms) | Yield |
|---|---:|---|---|
| Kind Pipeline | 0 | FERC Major Pipeline Projects Pending, <https://www.ferc.gov/industries-data/natural-gas/major-pipeline-projects-pending> (public domain; `us.ferc.pipelines_pending`, WAF needs a browser fetch). EIA natural gas pipeline projects tracker, <https://www.eia.gov/naturalgas/data.php> (public domain; not in `data/sources.yaml`, file URL to confirm). Permitting Dashboard pipelines sector (`us.permits_dashboard`, failing in production) | Estimate: tens (FERC), order 100 (EIA) |
| Kind LNG | 0 | DOE FECM LNG applications, <https://www.energy.gov/hgeo/liquefied-natural-gas-lng> (public domain; `us.doe.lng_exports`, no connector). FERC LNG terminals, <https://www.ferc.gov/natural-gas/lng> (public domain; new) | Estimate: 20–40 US projects |
| Kind Transmission | 0 | Remap NESO "Reactive Compensation" (held). Permitting Dashboard transmission sector (failing in production). GB NSIP register, <https://national-infrastructure-consenting.planninginspectorate.gov.uk/project-search> (OGL v3; `gb.pins.nsip`). DOE Transmission Facilitation Program and GRIP selections, <https://www.energy.gov/gdo/transmission-facilitation-program> (public domain; new). ERCOT Regional Planning Group list, <https://www.ercot.com/gridinfo/planning> (ERCOT clause 5; product to confirm) | 17 at once (measured); the rest estimate tens each |
| Carbon storage (17, Texas only) | 17 | EPA Class VI projects, <https://www.epa.gov/uic/current-class-vi-projects-under-review-epa> (public domain; implemented, failing in production) | 68 projects, 235 well applications (2026-10-07 connector run, measured) |
| Nuclear (12) | 12 | NRC new and advanced reactor applications, <https://www.nrc.gov/reactors/new-reactors.html> (public domain; `us.nrc.adams` for documents) | Estimate: 20–30 US projects |
| Hydrogen (2) | 2 | DOE Regional Clean Hydrogen Hubs, <https://www.energy.gov/oced/regional-clean-hydrogen-hubs-0> (public domain). IEA hydrogen projects database (terms not read) | 7 hubs; IEA order 10² if its terms allow |
| Withdrawn or cancelled (2) | 2 | EIA-860M "Canceled or Postponed" sheet, same workbook (public domain) | About 1,743 rows (August 2026, docs/27 §R1.1); "postponed" needs its own state first |
| Queue CAISO, NYISO | 0 | `us.iso.caiso.gen_queue`, `us.iso.nyiso.gen_queue` (attribution, derived only; implemented) | 2,278 and 1,839 rows (2026-10-23 sandbox runs, measured) |
| Company types | 2 typed | EIA-860 Schedule 1 entity type (already loaded). EIA-861 ownership (`us.eia.861`, pending) | Most of the 6,914 operators and 3,028 owners (estimate) |
| Companies in Large load | 0 | Names already held (VA DEQ, ICIS-Air). In progress, not missing: EPA NPDES construction stormwater, FAA Part 77, Loudoun County lanes | At least 248 of 582 (measured by keyword) |
| RFP kinds Auction, Loan programme | 0 | BOEM lease sales, <https://www.boem.gov/renewable-energy/state-activities> (`us.boem.renewables`). USDA RD energy, <https://www.rd.usda.gov/programs-services/energy-programs> (`us.usda.rd_energy`). DOE LPO, <https://www.energy.gov/lpo/loan-programs-office> (new). All public domain | Single digits to tens a year: hide until a row exists |
| Site finder | 0 | The production run over layers already licensed | Not measured |

## 6. Remove or hide

Remove: Kind **Nuclear** and **Hydrogen** (technologies, B-2); opportunity kind **Programme**; opportunity statuses
**Frozen** and **Reinstated** from the select (keep the vocabulary values for a funding-freeze source); technology
**Not stated**, **Gas, steam turbine** and **Waste to energy** as separate options (merged, §8.2).

Hide until data: Kind Transmission, Pipeline, LNG; RFP kinds Auction, Loan programme; RFP status Cancelled; RFP
technologies Offshore wind, Pumped hydro storage, Tidal and wave, CCS, Microgrids; Companies Type (whole select);
Queue CAISO, NYISO; the no-longer-listed box; the Site finder page. With facets (§9) "hide until data" is the rule,
not a list someone maintains.

## 7. Tag quality over the data held

| Dimension | Unknown, other or blank | Finding |
|---|---|---|
| Proposal technology | other 45, unknown 1 of 6,633 (0.7%) | 29 of the 45 have a real home (B-15). Hybrids split real sets: "Solar" (1,536) excludes "Solar + storage" (601) |
| Proposal kind | other 46 | 17 are grid equipment; five kinds unreachable (B-2); kind repeats technology for load, storage and CCS one-to-one |
| Lifecycle | unknown 23 (22 ICIS-Air with no status, 1 RRC drift) | Withdrawn 2, cancelled 0: the state is defined but unsourced |
| Jurisdiction | proposals none blank; NESO rows carry "GB" with no region | Opportunities mix ISO codes with World Bank codes (B-17) |
| ISO | null 609 (582 data centres, 20 wells, 7 EIA rows) | 48 values: 7 markets and 41 balancing authorities on EIA-860M units; the vocabulary labels them "Aeci", "Ava" |
| Sponsor | null 582 (every load record) | All 6,051 linked sponsors are type "other" |
| Organisation type | other 13,854 of 13,856 | The Type filter cannot work until typed |
| Opportunity technology | untagged 853 of 1,809 (82 of 510 in the default view) | Tags are wrong more often than right for Solar PV and Nuclear (B-3, B-4); opportunity tags use other words than proposals |
| Opportunity issuer | null 1,793 of 1,809 | Only the 16 utility RFPs name an issuer; TED buyers are not resolved, so Companies' "RFPs issued" is near empty |

Raw values that map to Other or Unknown but have a real tag: NESO "Reactive Compensation" (17) → grid equipment;
NESO "CHP (Combined Heat and Power)" (12) → combined heat and power; Texas RRC "Public Comment" (1) → studied. NESO
"Thermal" (7), EIA "All Other" (5) and ERCOT "Other - Other" (4) stay Other. The full raw → tag table for every
source and value is `tag_sets.proposal_technology.raw_mapping` and `tag_sets.lifecycle_state.raw_mapping` in the JSON.

## 8. Proposed tag sets

### 8.1 Technology family (one set for projects, plants, companies and RFPs)

Filter key `family`, expanding to member tokens; the option list groups the fine tags under it. Counts: projects in
the default view; plants operating with a location; RFPs default view, current tags and after B-3–B-5 (simulated).

| Key | Label | Description | Members | Projects | Plants | RFPs now → fixed |
|---|---|---|---|---:|---:|---|
| solar | Solar | Solar farms, including solar with batteries | solar, solar_storage, solar_thermal, solar_pv | 2,137 | 7,437 | 197 → 60 |
| wind | Wind | Onshore and offshore wind, with or without batteries | wind, wind_offshore, wind_storage | 619 | 1,371 | 7 → 6 |
| storage | Storage | Batteries and pumped hydro | storage, pumped_storage, bess | 1,929 | 694 | 18 → 15 |
| gas | Gas | Gas-fired plants of every type, and gas supply work | gas_cc, gas_ct, gas_ice, gas_steam, gas_other, fuel_cell, gas | 618 | 1,966 | 94 → 61 |
| nuclear | Nuclear | Nuclear plants, new reactors and restarts | nuclear | 12 | 56 | 36 → 2 |
| hydro | Hydro and tidal | Dams, run-of-river and tidal power | hydro, marine | 132 | 1,394 | 14 → 14 |
| bioenergy | Biomass and waste | Plants that burn wood, crops, landfill gas or rubbish | biomass, waste | 35 | 581 | 8 → 8 |
| geothermal | Geothermal | Power from underground heat | geothermal | 9 | 66 | 1 → 1 |
| oil | Oil | Oil- and diesel-fired plants | oil | 24 | 868 | 0 |
| coal | Coal | Coal-fired plants (no coal projects held) | coal | 0 | 192 | 6 → 0 |
| hydrogen | Hydrogen | Electrolysers and hydrogen-fired plants | hydrogen | 2 | 0 | 2 → 2 |
| large_load | Large load (data centres) | Big electricity users asking to connect | load | 73 | — | — |
| carbon_storage | Carbon storage | Storing captured CO2 underground | co2_geologic_sequestration, ccs | 17 | — | 0 |
| grid | Grid and transmission | Power lines, substations and grid equipment | transmission, grid_equipment | 10 | — | 101 → 93 |
| demand_side | Efficiency and demand side | Efficiency, heat networks and heat pumps, EV charging, microgrids, metering | efficiency, heat, ev_charging, microgrid, metering | 0 | — | 62 → 57 |
| other | Other or not stated | Mixed sites and records whose source names no technology | other, unknown, chp | 12 | 75 | — |

### 8.2 Fine tags, kinds and states

Proposal technology, renamed and merged (default view): Battery storage 1,901; Solar 1,536; Solar + storage 601;
Onshore wind 409; Gas, combined cycle 297; Gas, simple cycle 224; Hydro 120; Offshore wind 108; Wind + storage 102;
Gas engines 94; Large load (data centres) 73; Biomass 31; Pumped hydro storage 28; Oil 24; CO2 storage wells 17;
Nuclear 12; Tidal and wave 12; Other or not stated 12; Grid equipment 10; Geothermal 9; Waste to energy 4; Gas,
other 3; Hydrogen 2; Combined heat and power 0 (12 built). Waste and the small gas tags stay as data, shown only
inside their family.

Proposal kind (default view): Generation 3,578; Storage 1,939; Large load (data centres) 73; Carbon storage 17;
Other 12; Grid 10; Pipeline 0 and LNG terminal 0 (hidden). Nuclear and Hydrogen removed.

Status groups: Announced 610; **In development** (was "In process") 4,594; Contracted 425; Built or operating 979;
Withdrawn or cancelled 2; Status unknown 23.

RFP kinds (default view): Tender 483; **Advance notice** (was "Procurement notice") 16; **Funding opportunity** (was
"Funding opportunity (FOA)") 10; Request for proposals (RFP) 1. Auction and Loan programme hidden; Programme removed.

New RFP dimension, **Type of work**, from fields already stored (TED contract nature and CPV, kind, source), default
view: Equipment 201; Electricity and gas supply 114; Build and install 109; Services and studies 64; Funding 10;
Power procurement 1; Not stated 11. Owner decision: 114 of the 510 "energy-related" notices are contracts to buy
power, heat or gas for public buildings, which the page's own rule ("fuel purchases are left out") arguably covers.
This filter lets a developer leave them out without the platform deciding for everyone.

Company type, mapped from EIA-860 Schedule 1 entity type (already loaded): cooperative → Cooperative; investor-owned
→ Utility; municipal, political subdivision, state, federal → Public agency; independent power producer →
Independent power producer; industrial and commercial → Other. Curated RFP issuers → Utility.

### 8.3 Renames

| Where | From | To |
|---|---|---|
| Technology `storage` | Storage | Battery storage |
| Technology `wind` | Wind | Onshore wind |
| Technology `gas_ct` | Gas, combustion turbine | Gas, simple cycle |
| Technology `gas_ice` | Gas, reciprocating engine | Gas engines |
| Technology `co2_geologic_sequestration` | CO2 geologic sequestration | CO2 storage wells |
| Kind `ccs` | Carbon capture (CCS) | Carbon storage |
| Kind and technology `load` | Load (data centres, large loads) / Large load | Large load (data centres) |
| Status group | In process | In development |
| Plant family | Biomass / waste | Biomass and waste |
| RFP technology `solar_pv`, `gas`, `transmission` | Solar PV, Natural gas, Transmission | Solar, Gas, Grid and transmission (family labels) |
| RFP kind `foa`, `procurement_notice` | Funding opportunity (FOA), Procurement notice | Funding opportunity, Advance notice |
| Grid point kind `unknown` | Unparsed | Not identified |
| Grid points control | Queue (options ERCOT, NESO) | Grid operator (ERCOT (Texas), NESO (Great Britain)) |
| Map asset types | RNG, Ethanol | Renewable natural gas, Ethanol plants |
| Map layer | Retired & retiring plants | Retired and retiring plants |
| Search sections | Proposals, Opportunities, Assets | Projects, RFPs & funding, Infrastructure |
| Jurisdiction boxes (four pages) | Jurisdiction | State or country (a select) |
| Vocabulary endpoint | Gas Cc, Ccs, Lng, Rfp, Foa, Bess, Solar Pv, Ipp, Aeci, ... | The site's labels |

Checked against `web/labels.py` and `services/labels.py`. No glossary lane output is on this branch; when it lands,
"Advance notice", "Gas, simple cycle" and "Large load" need entries.

## 9. UI rule and the API it needs

**Rule.** Every select and checkbox group shows the count each option would return under the page's other filters
and defaults: "Solar (2,137)". An option with zero is hidden, except the one in force, which stays with "(0)" so the
reader sees why the list is empty. Fixed-meaning checkbox groups (Status, Location precision) keep every box and
disable a zero one. A free-text box over a closed vocabulary becomes a select.

**It is right here, under five conditions** this audit measured:

1. Counts are at the reader's tier under the page's defaults, or "Nuclear (18)" opens 12.
2. Each dimension is counted without its own filter (disjunctive facets), so choosing Solar does not hide Wind.
3. A multi-valued tag counts tagged records only (B-1), or every RFP option reads 82.
4. Counts refresh when htmx swaps the rows: send the filter bar out of band, as `/prices` already does for its
   credits. Stale counts are why `opportunity_kind_options` left numbers out.
5. Not on the vocabulary endpoint (what a value means, not what is held) or the site finder's thresholds.

**API.** `include=facets` on `GET /v1/proposals`, `/v1/opportunities`, `/v1/organizations`, `/v1/assets` and
`/v1/interconnection-points`, and `GET /v1/proposals/facets` (no rows) for the map:

```json
"meta": {
  "total": 5629,
  "facets_as_of": "<data version>",
  "facets": {
    "family":          {"basis": "other_filters", "values": [{"value": "solar", "label": "Solar", "count": 2137}]},
    "technology":      {"basis": "other_filters", "values": [{"value": "solar", "label": "Solar", "family": "solar", "count": 1536}]},
    "kind":            {"basis": "other_filters", "values": [{"value": "generation", "label": "Generation", "count": 3578}]},
    "lifecycle_state": {"basis": "other_filters", "values": [{"value": "filed", "label": "Filed", "count": 1813}]},
    "state":           {"basis": "other_filters", "values": [{"value": "US-TX", "label": "Texas", "count": 1948}]}
  }
}
```

- Same visibility predicate and served view as the list (`GatedRecord`), so a single-valued facet sums to
  `meta.total`; a parity test checks every value against `?<facet>=<value>&include=count`.
- `facets=` names which to compute; labels from `services/labels.py`; cached by data version like
  `/v1/proposals/geo`; one `GROUP BY` per facet over the filtered set.
- It replaces four `lifecycle_counts` calls per page (`web/app.py:395`), the per-kind probes (`web/app.py:1070`) and
  the per-type probes (`web/organizations.py:1030`).
- Also needed: `family=` on proposals, assets, companies and RFPs; `technologies` no longer matching untagged notices;
  case-insensitive `jurisdiction`/`state` accepting "TX"; `include=count` on `GET /v1/assets`.

## 10. Fix lanes

| Lane | Scope | Files it owns |
|---|---|---|
| F1 API facets and vocabulary | `include=facets`, `/v1/proposals/facets`, `family=`, B-1, B-12, case-insensitive jurisdiction, assets count | `services/api/facets.py` (new), `services/api/records.py`, `services/api/organizations.py`, `services/api/interconnection_points.py`, `services/api/assets.py`, `services/api/app.py` (vocab), `services/labels.py` (families, moved tables), `api/openapi.yaml`, tests beside them |
| F2 Filter UI | counts and hiding on every page, selects for jurisdiction, out-of-band filter refresh, B-8, B-10 (UI), B-13, B-16, B-19, B-20, renames in §8.3 | `web/app.py`, `web/organizations.py`, `web/interconnection_points.py`, `web/assets_pages.py`, `web/prices.py`, `web/alerts.py`, `web/unlisted.py`, `web/labels.py`, `web/templates/{home_map,proposals_list,opportunities_list,organizations_list,interconnection_points_list,assets_list,search,_macros}.html`, `web/static/js/map.js`; after F1 |
| F3 RFP tagging and relevance | B-3 to B-7, B-17, all-source tag, Type of work, eval set | `pipeline/connectors/eu_ted_api/connector.py`, `pipeline/connectors/opportunity.py`, `pipeline/connectors/mdb_worldbank_procnotices/connector.py`, `pipeline/connectors/us_rfp/`, `services/match/relevance.py`, `data/eval/opportunity_technology_tags.csv` (new); ships with F1's B-1 |
| F4 Project taxonomy and status | B-2 (classifier side), B-14, B-15, EIA-860M cancelled sheet, kind vocabulary | `pipeline/normalize.py`, `pipeline/status_map.yaml`, `pipeline/connectors/us_tx_rrc_class_vi/status_map.yaml`, `pipeline/connectors/us_eia_860m/`, `data/vocabulary/lifecycle_states.yaml` (if a postponed state is chosen) |
| F5 Production ingest | CAISO, NYISO, EPA Class VI, Permitting Dashboard fetches; site-finder run (B-10 ops, B-18) | `pipeline/connectors/http.py`, `infra/` scheduler and host config; no product code |
| F6 Entity enrichment | B-9 data-centre sponsors, company types from EIA-860 Schedule 1 | `services/resolve/`, `pipeline/connectors/us_va_deq_data_center_air_sites/`, `pipeline/connectors/us_epa_echo_icis_air/`, `data/vendored/organizations/` |
| F7 New sources (one lane each) | FERC pending pipelines, EIA pipeline tracker, DOE/FERC LNG, NRC reactors, DOE transmission and hydrogen selections, GB NSIP | a new `pipeline/connectors/<id>/` each, its `data/sources.yaml` row and docs/13 §6 row |

Order: F1 and F3 together (B-1 without B-3/B-4 would show more noise, not less), then F2; F4, F5, F6 in parallel; F7
after the owner picks which sources first.

## 11. Assumptions and open questions

- Counts are the public tier on 2026-10-23 23:16–23:21 UTC. A Pro reader sees the same sources today (no gated
  proposal source has public rows), so the zeros hold for every tier until PJM, MISO, SPP or ISO-NE rows publish.
- The candidate-source yields marked "estimate" were not fetched. The EIA pipeline tracker URL and the ERCOT RPG
  product must be confirmed by probe before a lane starts.
- The precision figures rest on 25-notice samples judged by one reader; F3 should grow them into a labelled set of
  at least 100 per tag before claiming a measured precision.
- **Owner decisions needed:** (1) whether electricity, heat and gas supply contracts (114 of 510) stay in the
  energy-related view, behind the new Type of work filter; (2) whether EIA-860M "postponed" units read as cancelled
  or get a state of their own; (3) the order of the new sources in F7.
