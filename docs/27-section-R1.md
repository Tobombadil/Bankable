# 27 — Powered land, section R1: retired and retiring power plants

**Status:** built, tested and measured in lane R1 (2026-10-06). This is the R1 section of a new powered-land document, for the coordinator to merge into `docs/27`.
**Asked by:** the owner, 2026-10-06. The use case is a powered-land director looking for sites where interconnection rights and grid infrastructure already exist.
**Reads:** `docs/24` (assets compared with proposals; one record per plant), `docs/25` §1–§2 (interconnection points, transmission lines), `docs/22` §8.1 (FX1 restatement).

---

## R1.0 The answer in one page

A plant that has retired, or is scheduled to, leaves a substation and a transmission connection behind. It may also leave an interconnection agreement. EIA-860M already states both halves of this signal, in the same monthly workbook the platform fetches for planned units:

- **Retired:** the "Retired" sheet lists **7,324 retired generators at 2,658 plants (306.4 GW)**. Coverage runs from 2002 onwards, plus a short appendix of earlier nuclear retirements. **1,772 plants have no generator left in service.**
- **Retiring:** **531 operating generators at 237 plants (81.1 GW)** carry a planned retirement year. 204 of those plants have at least half their in-service MW scheduled to retire.

**Decision: extend the existing `power_plant` assets; do not add a new asset kind.** One EIA plant id stays one record whether it is operating, retiring or retired. Asset status gains `retiring`. `retirement_year` becomes a column, and the detail sits in `attributes.retirement`. Grid fields from EIA-860 Schedule 2 sit in `attributes.grid`. Planned, moved, withdrawn and actual retirements become **events through the runner's diff**, under a new source id `us.eia.860m.retirements`, with the FX1 restatement wired in. A first load writes **zero** events.

Live run, 2026-10-06. One workbook fetch: `august_generator2026.xlsx`, 13,955,142 bytes, 35,701 generator records, DQ pass. The asset frame has 16,472 plants: **14,496 operating, 204 retiring, 1,772 retired**. Ten sampled rows were checked cell by cell against EIA's sheet, and **10 of 10 match** (§R1.6).

## R1.1 The sources, measured

### EIA-860M monthly workbook (`us.eia.860m`, and the new `us.eia.860m.retirements`)

Live fetch at 2026-10-06T19:21:29Z with the platform User-Agent, robots.txt honoured and `/archive/` links dropped. The index listed future months first: `december_generator2026.xlsx` and `october_generator2026.xlsx` each answered 200 with a 55,938-byte HTML placeholder. The first real workbook was **`xls/august_generator2026.xlsx`** (200, 13,955,142 bytes, sha256 `b4b70abb…9f1c`; 4 requests in total). September 2026 was not yet published.

| Sheet | Rows | Columns |
|---|---|---|
| Operating | 28,377 generators, 14,700 plants, 1,411.5 GW | Entity ID/Name, Plant ID/Name, Plant State, County, **Balancing Authority Code**, Sector, Generator ID, Unit Code, Nameplate/Net Summer/Net Winter Capacity (MW), Technology, Energy Source Code, Prime Mover Code, Operating Month/Year, **Planned Retirement Month/Year**, Status, Nameplate Energy Capacity (MWh), DC Net Capacity (MW), Planned Derate/Uprate/Repower/Other Modification Year/Month, **Latitude, Longitude** (37 columns) |
| Retired | 7,336 rows; 7,324 after 12 rows with no plant id are dropped; 2,658 plants, 306.4 GW | The Operating identity, capacity and technology columns, Operating Month/Year, **Retirement Month/Year**, Latitude, Longitude (26 columns) |
| Planned | 2,311 | Already read by `us.eia.860m` |
| Canceled or Postponed | 1,743 | Not read |
| Operating_PR / Planned_PR / Retired_PR | 230 / 7 / 12 | Puerto Rico; not read (no PR plant is an asset today) |

Header row: the third row on every sheet, as the Planned sheet already uses. Operating Status values: OP 26,126; SB 1,497; OS 538; OA 216.

**Retired sheet range.** Retirement years run from 1963 to 2026. Only 32 rows predate 2002. They are an appendix of early nuclear units, 12 of which carry **no Plant ID** (Bonus, CVTR, Elk River, Fort St. Vrain, Hallam, Indian Point 1, Pathfinder, Piqua, Saxton, Shippingport, Shoreham, Vallecitos). The parser drops those 12 and counts them. Since 2002 there are **7,308 retired generators (297.7 GW)**.

| Retirement year | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 (to Aug) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Generators retired | 407 | 349 | 264 | 352 | 386 | 284 | 272 | 294 | 474 | 338 | 206 | 40 |
| MW retired | 25,714 | 18,912 | 13,933 | 26,181 | 21,550 | 19,148 | 11,101 | 18,953 | 17,342 | 11,482 | 5,709 | 910 |
| Plants that closed entirely (last unit) | 102 | 77 | 61 | 96 | 110 | 87 | 82 | 101 | 112 | 88 | 52 | 8 |

**Planned retirements on operating generators.** 531 generators at 237 plants, 81,125 MW. Seven of them state a year and no month (La Cygne 1 and 2, Lawrence 4 and 5, Iatan 1, Jeffrey 1 and 3).

| Planned year | 2026 | 2027 | 2028 | 2029 | 2030 | 2031 | 2032 | 2033 | 2034 | 2035 | 2036–2072 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Generators | 42 | 66 | 66 | 30 | 48 | 47 | 21 | 49 | 32 | 25 | 105 |
| Plants | 18 | 33 | 39 | 19 | 25 | 17 | 12 | 14 | 13 | 17 | — |
| MW | 8,023 | 10,770 | 15,594 | 6,829 | 6,292 | 6,351 | 4,590 | 4,850 | 5,674 | 4,468 | 7,684 |

**Duplicate keys.** `(Plant ID, Generator ID)` is unique on both sheets once the 12 plant-less rows are gone. A generator never sits on both sheets. A generator that retires keeps its id when it moves from Operating to Retired, which is what makes retirement a `status_change` in the diff.

**Terms.** US federal work, public domain (17 U.S.C. §105). The EIA reuse statement is quoted in `docs/13` §2.11. The manifest entry `us.eia.860m` is `reuse: open`, `publication: raw_ok`, confirmed. The new id `us.eia.860m.retirements` carries the same terms. I added it to the existing `us.eia.860m` row of `docs/13` §6 because `scripts/check_manifest_licences.py` requires a register row for every manifest id (PASS, 124 sources).

### EIA-860 annual (`us.eia.860`): what it adds

I read the 2025 final zip already snapshotted on 2026-09-19 (`xls/eia8602025.zip`) without a new fetch. Schedule 2 `2___Plant_Y2025.xlsx` (17,349 plants) states per plant:

- **NERC Region** (17,096);
- **Grid Voltage (kV)**, **Grid Voltage 2**, **Grid Voltage 3** (17,206 with at least one voltage above 0; 0 kV is read as "not stated");
- **Transmission or Distribution System Owner** with id and state (17,238);
- Balancing Authority Code and Name;
- Latitude and longitude.

Schedule 3 adds an RTO/ISO LMP node designation on 5,644 of 27,918 operable generators. For generators with a planned retirement it covers 238 of 573. It is not used yet.

Coverage against the 860M plants:

| 860M plant set | Plants | In the 860 plant file | With grid voltage |
|---|---|---|---|
| Operating (any unit in service) | 14,700 | 14,699 | 14,693 |
| Retiring (status) | 204 | 204 | 204 |
| Retired, all units | 1,772 | 751 | 696 |
| — retired since 2020 | 530 | 307 | — |
| — retired since 2024 | 148 | 119 | — |

The annual file lists plants that had a generator in the report year, so a plant that retired earlier is absent. **Retired-plant coordinates do not need EIA-860**: the Retired sheet itself carries latitude and longitude, and 1,754 of 1,772 wholly retired plants have them. The EIA-860 Retired-and-Canceled generator sheet adds nothing the monthly sheet lacks for this purpose.

## R1.2 The model

**Retired and retiring plants extend the existing `power_plant` assets.** They are keyed on the same EIA plant id (`asset.source_asset_id`, source `us.eia.860m`). A new asset kind would put one physical site in two records whenever a plant moved from operating to retired. That is the duplication `docs/24` §0 measured as structurally zero for power plants, and it should stay zero.

**Plant status rule** (`pipeline/context/retirements.py`):

| Status | Rule | Live count |
|---|---|---|
| `retired` | the plant has retired generators and none in service | 1,772 |
| `retiring` | generators with a planned retirement hold **at least half** of the plant's in-service nameplate MW | 204 |
| `operating` | otherwise, including the 33 plants retiring a minority of their MW (they keep the date and still match a year filter) | 14,496 |

The threshold is half of the MW, not "every unit" (180 plants) and not "any unit" (237). Under "any unit", a 2 MW diesel backup scheduled to retire would label a nuclear site `retiring`. Under "every unit", a 2,000 MW coal plant keeping one small combustion turbine would read as `operating`, which is wrong for a powered-land reader. Half of the MW keeps both right and fits in one sentence on the page.

**`retirement_year`** (new integer column, indexed): the year a retired plant finished retiring, else the year its next unit is scheduled to retire, else null.

**`attributes.retirement`:** status rule; in-service, retiring and retired MW; unit counts; first and last retirement month; next and last planned month (`YYYY-MM`, or `YYYY` where EIA states the year alone); MW retired and retiring by technology; the units; the workbook's "as of" month.

**`attributes.grid`** (from EIA-860 Schedule 2, through `services/ingest/enrich.py`): NERC region, balancing authority code and name, transmission or distribution system owner, grid voltages in kV, report year. **`attributes.balancing_authority_code`** comes from 860M for every plant.

**Generator state** comes from a status map, `pipeline/connectors/us_eia_860m_retirements/asset_status_map.yaml`:

- OP maps to `operating`; SB, OS and OA map to `standby`; the Retired sheet maps to `retired`;
- a planned retirement year on an in-service unit maps to `retiring`, a refine rule evaluated first.

The file is not named `status_map.yaml`, because `services/api/lifecycle.py` publishes every file of that name as a proposal lifecycle on /methodology, and these are asset states.

**Events through the diff.** `us.eia.860m.retirements` is one record per generator (`<Plant ID>-<Generator ID>`):

- `lifecycle_state` is the generator state;
- `capacity_mw` is the nameplate;
- `proposed_cod` is the retirement date, or the planned one.

The runner's unchanged `diff_snapshots` therefore emits `status_change` when a unit gains a date, loses it or retires, and `cod_change` when a planned date moves. `services/ingest/retirements.py` folds those into one `event` per plant and change type, with `subject_type = "asset"`:

| Generator diff | Asset event |
|---|---|
| → `retiring` | `retirement_planned` |
| `retiring` → in service | `retirement_cancelled` |
| → `retired` | `retired` |
| `retired` → in service | `returned_to_service` |
| `cod_change` on a `retiring` unit, both dates set | `retirement_date_changed` |
| new unit already `retiring` / retired this or last year | `retirement_planned` / `retired` |

Operating and standby flips, capacity re-ratings and units appearing or disappearing write nothing.

**No first-load flood.** The runner writes no events file when there is no previous snapshot. The connector test pins `events_emitted == 0` on a first run over the recorded workbook. The loader test pins zero `event` rows after the first load, and exactly the four expected rows after a month in which four planned retirements move.

**FX1 restatement.** `Connector.restate_status` recomputes every stored row's state from its own `raw` under the current map. The test re-reads the same workbook under a map that calls OS `operating`: two stored rows are reclassified (`rows_reclassified = 2`) and no `status_change` is emitted for them.

**Where events show.** On the plant's page, as `retirement_changes[]` in `GET /v1/assets/{id}`, with the event's own timing, licence and source clauses (`asset_event_visibility_filter`). `event_visibility_filter` still knows only proposal and opportunity subjects, so asset events are kept off `/v1/events`, RSS, alerts and webhooks; a test pins that. Opening that feed to assets is an owner decision (§R1.7).

## R1.3 What was built

| Piece | Where |
|---|---|
| Shared parsing, generator state, plant summary (pure) | `pipeline/context/retirements.py` |
| Connector, new source id, kind `document` (generic loader refuses it) | `pipeline/connectors/us_eia_860m_retirements/connector.py`, `asset_status_map.yaml`; manifest entry in `data/sources.yaml` |
| Asset frame: retired-only plants, status, `retirement_year`, `attributes.retirement`, BA code | `pipeline/context/eia_plants.py` (now reads the Retired sheet) |
| EIA-860 Schedule 2 grid fields | `pipeline/context/eia860_plants.py`, `services/ingest/enrich.py::apply_eia860_plants` |
| `retiring` status, `retirement_year` column, asset event vocabulary | `services/db/models.py`; migration `0030_asset_retirement.py` (refuses to downgrade while any asset is `retiring`) |
| Loader: assets plus events, idempotent | `services/ingest/retirements.py`; routed by `services/ingest/loader.py::SPECIALISED_LOADERS`, so the scheduler's `load_source` job loads it |
| API | `GET /v1/assets?status=retired,retiring&retirement_year[gte]=…&retirement_year[lte]=…&sort=retirement_year`; `GET /v1/assets/geo?status=…`; detail `retirement_year` and `retirement_changes[]`; new `GET /v1/assets/{id}/nearby-grid`; `api/openapi.yaml` updated |
| Web | map toggle "Retired & retiring plants" (its own source and layers, hollow rings: cross = retired, dot = retiring, sized by MW, own legend, in-view rows, drawer rows); the existing-assets layer now leaves retired plants out; asset page "Retirement" section; `/assets` passes `status` and the year bounds through and labels the status; all words from `web/retirement.py` |

**Nearby grid** reuses the nearby machinery. Transmission lines are `transmission_line` assets (the LBNL FERC Form 1 subset, `docs/25` §2, not the whole grid) within 25 km, measured point-to-line with `point_to_parts_km` over the cached line index. Interconnection points are those named by the exact-grade, publicly visible proposals within 25 km (the `nearby-proposals` candidates), with a count and the nearest distance. A point has no coordinates of its own (`docs/25` §1.1), so the page says "named by queue requests within 25 km", never "located at".

**Interconnection reuse note** on the page, worded as a possibility: "A plant that retires usually leaves its substation, transmission connection and interconnection agreement behind, and those may be reusable by a new project at the same site. FERC Order No. 845 lets an interconnection customer use surplus interconnection service at an existing site, and some transmission providers' tariffs let a replacement resource take over a retiring unit's interconnection service, usually within a set time after the retirement. Whether any such right still exists here, who holds it and on what terms depends on the owner, the transmission provider and timing; this page does not establish any of that." Source for Order No. 845: FERC, 163 FERC ¶ 61,043 (2018), https://www.ferc.gov/media/order-no-845. Individual tariffs' replacement provisions were not read in this lane; the note names none.

## R1.4 Live counts

Plant status: 14,496 operating, 204 retiring, 1,772 retired. Of the retired, 1,754 have coordinates.

**By state.** Top states by MW scheduled to retire, alongside MW retired since 2015:

| State | Scheduled MW | Retired since 2015, MW |
|---|---|---|
| TX | 8,412 | 14,307 |
| TN | 6,898 | 4,470 |
| MI | 6,273 | 7,043 |
| MN | 5,422 | 2,644 |
| CO | 4,428 | 1,516 |
| PA | 4,105 | 6,589 |
| IN | 4,094 | 6,554 |
| KS | 3,643 | 1,877 |
| CA | 3,526 | 13,225 |
| LA | 3,448 | 6,982 |
| FL | 3,294 | 13,420 |
| IL | 3,196 | 12,143 |

Other states with large retirements since 2015: OH 13,071, GA 7,212, KY 6,678, VA 6,676.

**By technology** (scheduled MW; retired MW since 2015):

| Technology | Scheduled MW | Retired since 2015, MW |
|---|---|---|
| Conventional steam coal | 46,982 | 111,649 |
| Natural gas steam turbine | 21,272 | 33,463 |
| Natural gas combustion turbine | 4,828 | 12,233 |
| Petroleum liquids | 2,061 | 10,779 |
| Nuclear | 1,871 | 4,712 |
| Natural gas combined cycle | 1,546 | 8,744 |
| Onshore wind | 1,249 | 1,814 |
| Batteries | 425 | 500 |
| Solar PV | 403 | 114 |

**Top 15 plants by MW scheduled to retire in 2026–2031.** All 15 have status `retiring`.

| # | Plant | State | MW | Units | Date(s) | Main technology | BA | NERC | Grid kV | Operator (EIA-860M) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Rockport | IN | 2,600.0 | 2 | 2028-12 | Coal | PJM | RFC | 765 | Indiana Michigan Power Co |
| 2 | Gallatin (TN) | TN | 1,918.4 | 12 | 2031-12 | Coal and combustion turbines | TVA | SERC | 161 | Tennessee Valley Authority |
| 3 | Sherburne County | MN | 1,704.0 | 2 | 2026-12 to 2030-12 | Coal | MISO | MRO | 345 | Northern States Power Co - Minnesota |
| 4 | Monroe (MI) | MI | 1,639.8 | 2 | 2028-12 | Coal | MISO | RFC | 345 | DTE Electric Company |
| 5 | Four Corners | NM | 1,636.2 | 2 | 2031-12 | Coal | AZPS | WECC | 500 | Arizona Public Service Co |
| 6 | Ormond Beach | CA | 1,612.0 | 2 | 2027-01 | Gas steam | CISO | WECC | 220 | Ormond Beach Power, LLC |
| 7 | J H Campbell | MI | 1,560.8 | 3 | 2026-11 | Coal | MISO | RFC | 345 | Consumers Energy Co |
| 8 | Craig (CO) | CO | 1,427.6 | 3 | 2026-09 to 2029-12 | Coal | WACM | WECC | 345, 230 | Tri-State G & T Assn, Inc |
| 9 | Belle River | MI | 1,395.0 | 2 | 2028-12 | Coal | MISO | RFC | 345 | DTE Electric Company |
| 10 | Brandon Shores | MD | 1,370.2 | 2 | 2029-06 | Coal | PJM | RFC | 230 | Brandon Shores LLC |
| 11 | Kincaid Generation | IL | 1,319.0 | 2 | 2027-12 | Coal | PJM | RFC | 345 | Dynegy Kincaid Generation |
| 12 | Sabine | TX | 1,304.4 | 3 | 2026-09 | Gas steam | MISO | SERC | 138 | Entergy Texas Inc. |
| 13 | Cumberland (TN) | TN | 1,300.0 | 1 | 2028-12 | Coal | TVA | SERC | 161 | Tennessee Valley Authority |
| 14 | Winyah | SC | 1,275.8 | 4 | 2030-12 | Coal | SC | SERC | 230, 115 | South Carolina Public Service Authority |
| 15 | Baldwin Energy Complex | IL | 1,259.6 | 2 | 2027-12 | Coal | MISO | SERC | 345 | Dynegy Midwest Generation Inc |

Some dates had already passed when the workbook was published (Craig 1 and Sabine, both 2026-09). In the August workbook these units are still on the Operating sheet with that planned date. The next release decides whether they become `retired` or the date moves; either way it is an event.

## R1.5 Measured behaviour

- **Connector run:** 27.8 s for 35,701 records; DQ pass; 0 unmapped statuses; 0 duplicate keys.
- **Asset frame:** `python -m pipeline.context.eia_plants --workbook …` produced 16,472 plants in 33 s.
- **Grid frame:** `python -m pipeline.context.eia860_plants --archive …` produced 17,349 plants in 8 s.
- **Store migration:** migration 0027 on a 150 MB copy of the screenshot store (SQLite, no `alembic_version`, stamped 0026) took 2.0 s and recreated the asset table with all of its rows.
- **Asset load:** 16,472 plants (1,814 inserted, 14,658 updated) took 9.3 s.
- **Enrichment:** 15,450 plants matched EIA-860, 15,389 with a grid voltage. One plant from the 2026-09-15 frame is no longer in the workbook and stays `operating`; it is stale, not wrong (§R1.7).

## R1.6 Sample check against EIA's sheet

I took ten generator records of the normalised frame (seeded random: five `retiring`, five `retired`) and compared them with the workbook's own cells, read with openpyxl rather than the parser's pandas path. **10 of 10 match** on state, technology, nameplate MW and retirement year/month.

| Record | Plant | State | Sheet | Nameplate MW | EIA year/month | Ours |
|---|---|---|---|---|---|---|
| 54349-GTA | Nevada Cogen Associates 2 Black Mountain | NV | Operating | 22.2 | 2030/10 | 2030-10 |
| 62438-C | Heller 400M | NJ | Operating | 0.2 | 2028/5 | 2028-05 |
| 2454-3 | Cunningham | NM | Operating | 126.9 | 2040/12 | 2040-12 |
| 3986-3 | Ladysmith Dam | WI | Operating | 1.6 | 2035/12 | 2035-12 |
| 1403-6(4) | Nine Mile Point | LA | Operating | 895.1 | 2031/6 | 2031-06 |
| 55774-MO5 | Morris Genco LLC | IL | Retired | 1.1 | 2018/5 | 2018-05 |
| 50112-GEN2 | Sierra Pacific Quincy Facility | CA | Retired | 7.5 | 2018/3 | 2018-03 |
| 2385-GT3 | Werner | NJ | Retired | 53.0 | 2015/5 | 2015-05 |
| 3317-2 | Dolphus M Grainger | SC | Retired | 81.6 | 2012/12 | 2012-12 |
| 54766-GEN4 | Boydton Plank Road Cogen Plant Hybrid | VA | Retired | 0.3 | 2023/3 | 2023-03 |

## R1.7 Limits and open items

1. **Asset events are not on the global feed, alerts or webhooks.** They are served on the asset page only. Opening `/v1/events`, saved searches and webhooks to `subject_type = "asset"` touches the visibility audit (M-11), alert parity and the webhook vocabulary. It is a decision for the owner and its own lane.
2. **One release per month.** The workbook is the August 2026 release. Events need a second release to diff against: the first real ones appear when September's workbook is published and the scheduler runs this source.
3. **Plants that drop out of the workbook entirely** keep their last asset state; neither path deletes an asset. One plant measured.
4. **The asset frame and the retirement loader** derive status with the same function from the same workbook. A context reload (`eia_plants`) and a retirement load run months apart can disagree until both run on the same release. The scheduler runs only the connector; the context load is still manual (`docs/63`). This happened: the shared data root's `context/us.eia.860m.plants.parquet` was the 2026-09-15 build from the July workbook (14,659 operating plants, no status column) until 2026-10-07, so loading the retirement run found no asset for 1,814 plants (1,771 wholly retired, 43 operating plants added to the Operating sheet since July). Rebuilt that day with `python -m pipeline.context.eia_plants --data-root <root> --latest-snapshot` (`--data-root` added so a worktree can rebuild a shared root with that root's run record): 16,472 plants, every plant of the retirement run has an asset; 18 wholly retired plants have no coordinates on EIA's sheet and stay unplaced (Zion, Trojan, Rancho Seco, Maine Yankee, Haddam Neck, Yankee Rowe, Big Rock Point and 11 small, mostly industrial plants). `web/dev_up.py` now loads the retirement run right after the plants layer.
5. **EIA-860 grid fields** cover only 751 of 1,772 wholly retired plants (the annual file's scope). Older annual years live under `archive/`, which robots.txt disallows; a browser download handed to `eia860_plants --archive` would fill more of them.
6. **Not used yet:** Schedule 3's LMP node designation (238 of 573 planned-retirement generators have one). It would join a retiring unit to its pricing node.
7. **Puerto Rico sheets** are not read, consistent with the operating assets today.
8. **Counts on `/assets`** now include retired plants among power plants: 16,472 rows rather than 14,700. The map's existing-assets layer leaves them out, and the list labels them "(retired 2024)".
9. **Migration number 0027** may collide with another lane's migration; renumber on merge if so. The CHECK keeps the name it found.
10. **Tariff terms behind the reuse note** (individual transmission providers' replacement-interconnection provisions) were not read here; the note names none.

## Assumptions

- The half-of-MW threshold is a presentation rule, not a regulatory one. The units, MW and dates are always shown, so a reader can apply their own.
- A planned retirement date reported to EIA is the owner's stated intention as of the release month. It is not a commitment, and it moves often; each move is an event.
