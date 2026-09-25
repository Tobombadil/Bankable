# Data sources: catalogue, coverage, legal register and ingestion order

**Status:** Phase 0 deliverable · 2026-09-12 · machine-readable twin: `data/sources.yaml` (64 entries) ·
live-check evidence: `data/probes/2026-09-12.json` · re-run: `scripts/probe_sources.py --gridstatus`

## 1. What "a proposal" is, operationally

The platform tracks two object families and the links between them.

- **Supply-side proposals** — a sponsor wants to build something: a generator or storage project in an
  interconnection queue, a large load (data centre) in a load queue, a pipeline or LNG terminal in a FERC CP docket,
  a transmission line in a planning portfolio, a nuclear unit in an NRC docket, a CO₂ well in a Class VI application.
- **Demand-side opportunities** — someone will pay for, buy from, or permit such a thing: a utility all-source RFP,
  a DOE/USDA funding opportunity, an MDB procurement notice, an EU/UK tender, a capacity auction, a state programme.

Each object has a **lifecycle** (announced → filed → studied → permitted → contracted → built → withdrawn/cancelled)
and the value of the platform is in (a) stitching all evidence for one real-world object together and (b) emitting
the *change* events. The sources below are grouped by which of those they feed.

## 2. Tier-1 sources (MVP) with today's verification

| Source | Feeds | Access | Cadence | Reuse | Verified 2026-09-12 |
|---|---|---|---|---|---|
| CAISO Public Queue Report | supply | xlsx | weekly | attribution | ok, 2,278 rows |
| ERCOT GIS Report | supply | xlsx via MIS JSON | monthly | **open** | ok, 1,778 rows |
| ERCOT Large Load report | supply (load) | xlsx | monthly | open | page ok, product id to confirm |
| SPP GI summary CSV | supply | csv | daily | unknown | ok, 3,074 rows |
| NYISO Interconnection Queue | supply | xlsx | weekly | unknown | ok, 3,164 rows |
| ISO-NE IRTT | supply | xlsx | daily | unknown | ok, 1,751 rows |
| PJM queue (API portal) | supply | JSON, key | daily | **restricted** | 401 without key |
| MISO GI queue | supply | JSON | daily | unknown | **403 Cloudflare** |
| LBNL Queued Up 2026 | supply (history) | xlsx | annual | CC BY 4.0 | site 403 to bots; file public |
| EIA-860M Planned | supply (spine) | xlsx | monthly | public domain | ok |
| FERC eLibrary JSON backend | stage evidence | JSON (undocumented) | realtime | public domain | ok, hits returned |
| Federal Permitting Dashboard | stage evidence | Socrata CSV/JSON | weekly | public domain | ok |
| Grants.gov Search2 | demand | JSON POST | realtime | public domain | ok, 151 energy hits |
| DOE eXCHANGE portals | demand | HTML | weekly | public domain | ok |
| Utility / co-op / CCA RFPs | demand | curated HTML/PDF | continuous | attribution | not probed (curation task) |
| EU TED API v3 | demand | JSON POST | daily | attribution | ok, 785 energy-CPV notices since 1 Sep |
| World Bank procurement notices | demand | JSON | daily | CC BY 4.0 | ok (sector filter to fix) |
| NESO TEC Register | supply (GB) | CKAN CSV | twice weekly | NESO Open Data | ok, 11 Sep 2026 file |
| Global Energy Monitor trackers | supply (global spine) | xlsx | quarterly | CC BY 4.0 | ok |
| Bluesky / LinkedIn / owned RSS+email | distribution | API | — | — | Bluesky ok |

Tier 2 and Tier 3 entries (non-ISO OASIS queues, state siting boards, BLM/BOEM/USACE, NRC ADAMS, SAM.gov, USDA,
Find a Tender, ENTSO-E TYNDP, PCI/PMI, MaStR, IESO/AESO, Canada IAAC, AEMO, EPBC, SECI, ANEEL, REIPPPP, MDBs,
news feeds) are fully specified in the YAML with tier, effort and notes.

## 3. Coverage map and the gaps that matter

**Covered well by Tier 1:** ~70–75% of US queued generation/storage capacity (five ISOs live today, PJM once keyed,
MISO once unblocked); federal funding and permitting stage signals; EU/UK/MDB tenders; GB transmission queue.

**Gaps, in order of commercial importance:**

1. **Non-ISO US utilities** (Southeast, Mountain West, Pacific NW). ~40 OASIS sites, one layout each. Nobody free
   covers them well except Interconnection.fyi, which cannot be scraped. This is the most defensible thing to
   build, and the most expensive: plan roughly one connector-day per utility plus ongoing breakage.
2. **Utility RFPs.** No dataset exists; Energy Adepto is private. Requires a curated issuer list plus news-trigger
   discovery. Central to the marketplace thesis.
3. **State siting boards** (OH OPSB, NY ORES, IL, MI, MN, WI, CT, MA, CA CEC). Highest-signal state layer;
   one connector per board.
4. **Large-load queues beyond ERCOT.** Utilities disclose data-centre requests inconsistently (PJM, Dominion,
   AEP, Georgia Power, APS). Track via dockets and news until formal queues exist.
5. **Gas/LNG/pipelines/nuclear/CCS.** Sources exist (FERC CP, DOE FECM, NRC ADAMS, EPA Class VI) but require PDF
   extraction. Include in scope; schedule after the electricity core.
6. **Rest of world.** GEM provides the spine; live feeds exist for GB, DE, IE, CA, AU, IN, BR, ZA. Add by
   customer pull.

## 4. Legal register (summary — the YAML carries per-source detail)

| Class | Sources | Rule |
|---|---|---|
| Public domain | All US federal (FERC, EIA, DOE, EPA, BLM, BOEM, USACE, NRC, grants.gov, SAM.gov, USAspending, Permitting Dashboard) | Free to use, republish, resell. Be polite on rate. |
| Open with attribution | LBNL (CC BY), GEM facility trackers (CC BY — but see `13` §2.2 and §2.18), NESO Open Data, OGL (UK), OGL-Canada, dl-de/by (MaStR), EU reuse (TED), World Bank (CC BY) | Republish with credit line and licence link on every derived record. |
| Open licence asserted but not evidenced for the dataset | GEM **Global Energy Ownership Tracker** | Its own page states no licence; CC BY 4.0 is named only by GEM's download gate. `attribution-restricted`, derived-only, not ingested. Drop the S&P Capital IQ id column and all natural-person rows if it ever ships. `13` §2.18. |
| ISO — permissive | ERCOT | Raw data explicitly redistributable in compilations and analyses. |
| ISO — attribution with restrictions | CAISO | Credit CAISO; publish derived records, link to source for raw. |
| ISO — restricted | PJM (Data Miner / API) | No public redistribution without Redistribution License. Obtain terms before any PJM row is shown publicly. |
| ISO — unknown | MISO, SPP, NYISO, ISO-NE | Terms not retrievable headlessly. Read in browser, capture PDF, record in YAML before launch. |
| Private aggregators | Interconnection.fyi, Cleanview, Energy Adepto, BidNet, Halcyon, Enverus | Never scrape. EU database right and ToS apply. Partner or buy. |
| News | GDELT, Google News RSS, wire RSS, trade press | Headline, link, snippet, extracted facts only. No article bodies. Honour DSM Art. 4 opt-outs. |
| Personal data | filer contacts in dockets/notices | Store minimum; no marketing use without consent; honour deletion requests. |

Two engineering consequences: every record carries `source_id`, `source_url`, `retrieved_at`, `licence` so the
public page can render attribution automatically; and the public tier shows *derived* fields (normalised status,
stitched identity, change events) with a link out for raw rows on restricted sources.

## 5. Canonical schema seed

gridstatus's normalised queue columns are a good starting point for the supply-side table. Proposed core:

```
proposal            id, kind (generation|storage|load|transmission|pipeline|lng|nuclear|ccs|hydrogen|other),
                    name_canonical, sponsor_org_id, technology, capacity_mw, storage_mwh, jurisdiction,
                    location (point/county/state), lifecycle_state, first_seen, last_changed
proposal_source     proposal_id, source_id, source_record_id, source_url, retrieved_at, raw (jsonb), licence
opportunity         id, kind (rfp|foa|tender|auction|loan_program|procurement_notice), issuer_org_id, title,
                    jurisdiction, technologies[], capacity_sought_mw, budget, open_at, due_at, status, source refs
organization        id, name_canonical, aliases[], type (developer|utility|coop|agency|lender|epc), ids (LEI, SAM UEI, EIA)
event               id, subject_type, subject_id, event_type (filed|status_change|withdrawn|awarded|cancelled|...),
                    observed_at, source_id, before (jsonb), after (jsonb)
match               proposal_id, opportunity_id, score, rationale, created_by (rule|model|user)
source              id, ... (mirror of sources.yaml), last_success_at, last_error, schedule
```

Entity resolution keys, in order of reliability: EIA plant/generator ID; queue ID + ISO; FERC docket number;
sponsor + county + capacity ± 10% + technology; fuzzy name. Every merge is recorded as an event and reversible.

## 6. Ingestion order

1. gridstatus-backed ISO queues (5 live) + EIA-860M + LBNL history → one table, one status vocabulary.
2. FERC eLibrary ER/CP docket poller (0.5 rps) + Permitting Dashboard → stage events.
3. grants.gov + DOE eXCHANGE + curated RFP issuers + TED + FTS + World Bank → opportunities table.
4. Change detection and the public change feed (written as "delayed feed" in 2026-09-12; nothing is time-delayed since 2026-09-21); Bluesky + LinkedIn syndication; RSS/email alerts.
5. PJM (after licence), MISO (after terms + egress), NESO/GEM international spine.
6. Non-ISO OASIS queues, state siting boards, BLM/BOEM/USACE, large-load dockets.
7. Gas/LNG/nuclear/CCS document pipelines; remaining international feeds.

## 7. Operational notes from the probe

- A browser User-Agent is required for most sites; several (ferc.gov pages, emp.lbl.gov, aemo.com.au) still
  block datacentre IPs. Plan a headless-browser worker and, if needed, a residential egress provider for ≤20% of sources.
- GDELT enforces one request per five seconds; batch queries.
- EIA-860M file names must be scraped from the index page; guessed names return HTML with a 200.
- FERC's backend occasionally returns HTTP 200 with `success:false`; treat as error, retry with backoff.
- SAM.gov and EIA v2 need free API keys; register both now.

## 8. Reference data: GB substation and settlement gazetteers

`gb.neso.tec_register` rows carry a "Connection Site" — a transmission substation name — and nothing else
geographic. `services/ingest/data/gb_substations.tsv` (371 rows: name, latitude, longitude) is vendored from
NESO's own **FES 2024 Grid Supply Point Info** dataset, part of "Regional breakdown of FES data (Electricity)"
on the NESO Data Portal, under the same NESO Open Data Licence already quoted for the TEC register itself
(`13-legal-data-rights.md` §2.5; required attribution "Supported by National Energy SO Open Data"). It is a
reference table (`data/sources.yaml` id `gb.neso.fes_gsp_gazetteer`, category `registry`), not a proposal or
opportunity feed: `services/ingest/geocode.py`'s `SubstationGazetteer` uses it to resolve a Connection Site to
a point at `county_centroid` precision — never `exact`, since a substation is not the project's own location
(docs/21-data-model.md §3.7).

Measured coverage (2026-09-13): 6 of the 30 distinct Connection Site strings in the connector's own test
fixture resolve (20.0%); 346 of the 1,237 distinct Connection Site strings in the live TEC register resolve
(28.0%, 739 of 2,198 rows). The gap is structural: most unmatched sites are a substation built new for that
specific generator or BESS project, which no general gazetteer covers. Unmatched sites stay `unknown`, never
guessed. Full provenance, the other candidates checked and why they were not used (NESO's own GIS boundary
datasets, National Grid Electricity Transmission's open data), and the coverage measurement are in
`services/ingest/data/README.md`.

**Settlement fallback (2026-09-20).** Of the 1,459 TEC register rows the GSP list leaves unplaced, 1,010 name a
real UK settlement rather than a Grid Supply Point ("Cilfynydd 400kV Substation", "Navenby", "Chirk GSP") —
GB transmission sites are overwhelmingly named after the place they stand next to. `services/ingest/data/
gb_settlements.tsv` (52,110 rows: name, latitude, longitude, local authority district, and a count of the
places that bear the name) is vendored from the **ONS Index of Place Names in Great Britain (July 2024)** on
the ONS Open Geography Portal, under the Open Government Licence v3 (`13-legal-data-rights.md` §2.17; required
attribution "Source: Office for National Statistics licensed under the Open Government Licence v3.0" and
"Contains OS data © Crown copyright and database right 2024"). `data/sources.yaml` id `gb.ons.ipn_gazetteer`,
category `registry`. OS Open Names was assessed and is equally OGL v3, passed over on fit (British National
Grid coordinates, 103 MB including roads and postcodes); OpenStreetMap was excluded and never fetched (ODbL
share-alike, a question for counsel, and this is a commercial product).

`geocode()` consults it only after the GSP lookup misses — a substation's own coordinates beat a settlement
centroid — and reports the same `county_centroid` precision, which is a further step removed (the project is
near a substation which is near that town) but the same county-scale uncertainty. A name borne by more than
one place is left `unknown`, never tie-broken: "Thornton" is six places, "Overton" sixteen.

**The settlement tier places nothing unless it is given a region.** The caller must supply the transmission
owner the register carries in `HOST TO` (`NGET`, `SPT`, `SHET`, `OFTO`), and a match whose point falls in
another owner's area is refused. That is a precondition rather than a filter, so wiring `country` through at
the loader's call site cannot by itself switch on unconstrained placement. The area test is a
nearest-neighbour classifier over the 371 vendored GSPs, each carrying a transmission region derived from the
NESO dataset's own `GSP Group` column (`_P` north Scotland, `_N` south Scotland, the other twelve England and
Wales) — added to `gb_substations.tsv` on 2026-09-20 under the licence already recorded for that dataset. It
agrees with the register's own `HOST TO` on 345 of 347 site/owner pairs where both are known. The GSP tier
takes no region: those 371 names are NESO's own list of the substations the register connects into.

Measured (2026-09-20, stored snapshot): placement rises from 739 to 1,049 of 2,198 rows (33.6% → 47.7%) and
from 346 to 499 of 1,237 distinct Connection Sites (28.0% → 40.3%). 56 sites (105 rows) are rejected as
ambiguous, 15 rows are `OFTO` (no onshore area), and the region precondition refuses a further 13 sites (19
rows) — of which 8 sites were demonstrably wrong placements and 3 were correct placements lost to a
register-side `HOST TO` inconsistency or a border artefact. An independent latitude-band cross-check that
found 11 impossible placements in the unconstrained 166 finds 0 in the constrained 153. One known-wrong
placement survives, `Norton East`, because it is an England-to-England collision the region cannot separate.
`services/ingest/data/README.md` has the per-site inspection.

## 9. EIA-860M operating plants (context layer)

`docs/00-PLAN.md`'s 2026-09-14 decision ("Built-infrastructure context layer") calls for a layer of existing
generating plants drawn beneath the proposals map — US first, from EIA-860M, public domain. This is a context
source, not a proposal source: it has no lifecycle, no events, no matching against queue rows
(docs/21-data-model.md §3.20 `built_plant`). It reuses `us.eia.860m`'s existing registry row (`data/sources.yaml`)
and licence, but reads the workbook's "Operating" sheet instead of "Planned" — same file, same header-on-the-
third-row convention as `pipeline/connectors/us_eia_860m/connector.py`.

**Parsing** (`pipeline/context/eia_plants.py`): `parse_operating_sheet` reads sheet "Operating" with `header=2`
and drops rows with no Plant ID (the workbook's trailing note rows, same shape the Planned-sheet connector
already drops). `aggregate_plants` then collapses the sheet from one row per generator to one row per Plant ID:

- **Dominant technology**: each unit's raw `Technology` label is summed by nameplate MW across the plant; the
  label with the largest summed MW is the plant's `technology_raw`, run through `pipeline.normalize.classify_tech`
  for the canonical `technology` token. Ties keep the first-encountered label (file order) — deterministic, not
  yet observed to matter in the live workbook.
- **Technologies split**: every raw label seen at the plant, summed and rounded to 3 dp — a plant with mixed
  units (e.g. a coal unit + a co-located BESS) reads honestly instead of collapsing to one label.
- **Capacity rule**: reuses `pipeline.normalize.capacity`'s existing rule — a reported `0` or blank nameplate is
  treated as missing, not a real zero-MW unit, and is excluded from the summed `capacity_mw` (though the unit's
  raw label still appears, at 0 MW, in the technologies split above; that split is a per-label ledger, not a
  capacity total).
- **Generator count**: number of generator rows at the plant, regardless of whether each unit's nameplate was
  valid.
- **Earliest operating year**: `min` of `Operating Year` across all units, independent of the capacity rule.
- **Coordinate rule**: identical to `services/ingest/loader.py::_extract_exact_point`'s validity check for this
  same workbook's `Latitude`/`Longitude` columns on the Planned sheet — numeric, finite, inside world bounds, and
  not the `(0, 0)` placeholder. A plant failing this check gets `lon`/`lat` = `None` (NaN once assembled into the
  output frame) rather than a wrong point; nothing is guessed.

**Loading** (`services/ingest/plants.py`): `load_plants` upserts by `(source_id, source_plant_id)` — no
`proposal_source`/`event` rows, since `built_plant` carries none. A re-run always overwrites every mapped field
on an existing row (see that module's docstring for why this is simpler than a field diff here, and still
idempotent because of the unique constraint) and reports it as `updated`, not skipped.

**Measured** (2026-09-15, `data/snapshots/us.eia.860m/20260913T202539Z.xlsx`, the July 2026 workbook already on
disk from the Planned-sheet connector's last run):

```
{"plants": 14659, "with_coordinates": 14659, "without_coordinates": 0, "by_technology": {"solar": 7397,
"hydro": 1394, "wind": 1365, "oil": 867, "gas_ct": 720, "storage": 655, "gas_cc": 528, "biomass": 527,
"gas_ice": 388, "gas_steam": 211, "coal": 192, "gas_other": 118, "other": 74, "geothermal": 66, "nuclear": 56,
"waste": 55, "pumped_storage": 33, "solar_storage": 8, "wind_offshore": 4, "unknown": 1}, "elapsed_s": 13.58}
{"plants_seen": 14659, "inserted": 14659, "updated": 0, "placed": 14659, "unplaced": 0, "elapsed_s": 1.83}
```

Every one of the 14,659 operating plants in this workbook carries a real coordinate (EIA-860M's own
`Latitude`/`Longitude` columns are populated for the whole Operating sheet, unlike the Planned sheet's
mixed completeness), so this run has no unplaced plants to fall back on a county/state centroid for. A
second loader run against the same parquet reported `inserted: 0, updated: 14659` — no duplicate rows,
confirming the unique constraint on `(source_id, source_plant_id)` holds across re-runs.

## 10. Ethanol and RNG point layers (fuels lane, 2026-09-19)

Owner decision 2026-09-19 option (a): ethanol and RNG points join the midstream wave as `asset` rows (ADR 0008),
objective features only, no valuation. Four `data/sources.yaml` section K sources, one parser each under
`pipeline/context/` on a shared seam (`fuels.py`: column contract, manifest gate, polite fetch, snapshot/run
records, coordinate rule, county placement). `us.anl.rng_database` stays out: its terms are unread (docs/13 §2.14).
Every parquet below loads through `services/ingest/assets.py::load_assets_parquet` unchanged (verified into an
in-memory SQLite, re-run reports `inserted 0 / updated N`); the two loader limits this exposes are in §10.5.

### 10.1 Sources, runs and terms (measured 2026-09-19, sandbox proxy)

| `source_id` | Fetched | `retrieved_at` | Bytes | Asset rows | Parquet | Terms observed |
|---|---|---|---|---|---|---|
| `us.eia.ethanol_capacity` | `https://www.eia.gov/petroleum/ethanolcapacity/ethanolcapacity.xlsx` (as of January 1, 2025) | 15:49:01Z | 23,446 | 191 | 46,519 B | US federal work; EIA reuse statement (docs/13 §2.11). Footnote: "Data Source: Form 819". |
| `us.epa.lmop` | `https://www.epa.gov/system/files/documents/2024-09/lmopcompositedata.xlsx`, link followed from the database page (page "Last updated on July 31, 2026") | 15:49:08Z | 1,465,367 | 1,353 (+129 proposals) | 1,006,024 B (+135,678 B) | §105 under the EPA hedge (docs/13 §2.12). Cover sheet ("Summary") carries the status vocabulary and a data-quality caveat, no copyright or reuse notice; page text: "Because the data are compiled from a variety of voluntary sources ... cannot guarantee the accuracy". |
| `us.epa.agstar` | `https://www.epa.gov/sites/default/files/2020-10/agstar-livestock-ad-database.xlsx`, link followed from the page (page "Last updated on July 10, 2026", file "based on data available through June 2024") | 15:49:18Z | 98,977 | 498 (+73 proposals) | 170,711 B (+43,160 B) | Same hedge. No cover sheet; page carries a data-accuracy caveat and "does not constitute or imply the endorsement or recommendation of EPA"; no copyright or reuse notice. |
| `us.eia.atlas.ethanol_plants` | `https://www.eia.gov/maps/map_data/Ethanol_Plants_US_EIA.zip` (EIA's shapefile copy, data "As of January 1, 2021"), after the ArcGIS service answered `Token Required` | 15:49:24Z | 37,644 | 197 | 65,797 B | Shapefile metadata `useLimit`: "None (public use). Users are advised to thoroughly review the metadata ... The U.S. Energy Information Administration gives no warranty ..."; credit "U.S. Energy Information Administration". |

**Atlas access finding.** The feature service every Atlas item and third-party copy points at
(`services7.arcgis.com/FGr1D95XCGALKXqM/.../Ethanol_Plants_US_EIA/FeatureServer/112`; org `FGr1D95XCGALKXqM` is EIA,
urlKey `eia`) answered `{"error": {"code": 499, "message": "Token Required"}}` anonymously, as did the sibling
biodiesel, pipelines and processing-plant services. None of them is among the org's 79 public feature-service items,
the Atlas DCAT feed (101 datasets, no infrastructure layers) or a Hub v3 search for "ethanol plants" (151,405 items,
no EIA-owned hit); the Hub slug lookup answered 403 and the `.geojson` download route 500. The connector treats the
token reply as a block (never a retry) and falls through to EIA's own `www.eia.gov/maps/map_data/*.zip` copy, whose
FGDC metadata carries the dataset-level terms the docs/40 §0 browser task was meant to read (quoted above). That copy
is a 2021-01-01 vintage; the annual capacity table is the current capacity primary and the layer supplies coordinates.
`atlas.eia.gov/robots.txt` sets `Crawl-delay: 60` for every agent; the lane's session honours it (one request a minute
there; EPA and EIA hosts at 0.5 rps).

### 10.2 Column contract and placement

Same loader-mapped columns as the EIA-860M plants frame (`source_asset_id`, `name`, `operator_name`, `status`,
`technology`, `technology_raw`, `technologies`, `capacity_mw`, `capacity_value`, `capacity_unit`, `commissioned_year`,
`unit_count`, `lon`, `lat`, `state_code`, `county_name`, `county_fips`, `country`, `attributes`, `source_url`,
`retrieved_at`) plus the provenance and feature columns the loader ignores today: `source_id`, `licence`
(`public-domain`), `licence_id` (the registry key the loader mints), `attributes_text` (JSON of the string-valued
features), `feedstock`, `owner_raw`, `developer_raw`, `status_raw`, `placement_precision`, `centroid_lon`,
`centroid_lat`, `raw` (the source row, verbatim).

- `attributes` holds numbers only: the loader's `_to_json_dict` casts every value with `float()`, so a string
  there would abort the load. String features live in `attributes_text` and the dedicated columns.
- `lon`/`lat` are set only from a coordinate the source itself states (the `eia_plants` validity rule: numeric,
  in bounds, not `(0, 0)`). A source that gives county or city only is left unplaced for the loader — never a point
  at a centroid (ADR 0008) — and carries its county FIPS and centroid with `placement_precision`
  (`county_centroid` | `state_centroid` | `unknown`) for a region-grade rendering.
- `status` is the `asset.status` vocabulary. Rows whose registry status is planned or under construction are
  proposals (ADR 0008 §1): each EPA parser returns them separately and writes them to
  `data/normalized/context/<source_id>.proposals.parquet` for the proposals lane; they never enter an asset frame.

### 10.3 Objective feature sets

- **Ethanol** (`ethanol_plant`; `ethanol_capacity.py`, `ethanol_plants.py`): nameplate capacity in MMgal/yr
  (`capacity_value`, `attributes.nameplate_capacity_mmgal_yr`), as-of year / data period, respondent or company
  string (`operator_name`, `owner_raw`), city or site, PADD, and — from the Atlas layer only — the exact point.
  Neither EIA product carries a feedstock field, so `feedstock` is null rather than assumed. Every listed plant is
  operating by construction of both products, so `status = operating`. Identity: `<STATE>-<respondent|company slug>-<city|site slug>`.
  The capacity table has no coordinate (city is not a coordinate): 191 rows at `state_centroid`; the Atlas copy has
  197 plants, all exact. EIA's `(s)` symbol ("less than 0.5 MMgal/yr") is a suppressed value, not zero (none in the
  2025 table).
- **LMOP** (`rng_project`; `lmop.py`): project type category → `technology` (`lfg_electricity` | `rng` |
  `lfg_direct_use`), LFG energy project type (`technology_raw`), RNG delivery method, LFG use details, rated MW
  (`capacity_mw`) and actual MW, LFG flow to project in mmscfd (`capacity_value`), start/shutdown years
  (`commissioned_year`), end users, owner and developer strings (`owner_raw`, `developer_raw`; `operator_name` =
  owner, else developer), plus the host landfill's name, ids, status, ownership type, owner/operator, waste in
  place, LFG generated/collected and the current-year emission reductions. Identity is `Project ID`; a project fed
  by several landfills (25 assets) collapses to one row keeping the first landfill's point and county and listing
  all landfills. Of 3,399 source rows: 669 Operational + 712 Shutdown rows → 652 + 701 assets (1,339 exact points,
  14 at county centroid — all 15 asset-status rows without a Latitude are Shutdown); 133 Planned/Construction rows →
  129 proposals; 1,883 landfill-potential rows (Candidate, Low Potential, Future Potential, Unknown) dropped with a
  count. One Shutdown project (`65-0`) has no category → `technology = unknown`.
- **AgSTAR** (`rng_project`; `agstar.py`): digester type (`technology_raw`; `technology = farm_digester`),
  animal/farm type as `feedstock`, biogas end use(s), project type, biogas generation estimate in cu-ft/day
  (`capacity_value`), electricity generated kWh/yr, head counts, co-digestion, LCFS pathway and USDA funding flags,
  receiving utility, total emission reductions, year operational (`commissioned_year`), year and reason of shutdown,
  designer/developer string (`developer_raw`; the farm is the operator and is named in the project name, so
  `operator_name` is null). Identity is a content key over (Project Name, State) — no duplicates in the 2024-06 build.
  571 rows: 400 Operational + 98 Shut down → 498 assets, 73 Construction → proposals. No coordinates: 482 at county
  centroid, 16 at state centroid where the source county does not resolve (typos such as "Niagra", "Caroll",
  "Mantiowoc"; Connecticut's four legacy counties, which the 2024 Gazetteer replaced with planning regions; one blank).

### 10.4 Tests and fixtures

`pipeline/context/test_{fuels,ethanol_capacity,lmop,agstar,ethanol_plants}.py` (113 tests in `pipeline/context`,
all fixture-based, no network). Fixtures under `pipeline/context/fixtures/` are the real 2026-09-19 downloads,
trimmed: `eia_ethanol_capacity_sample.xlsx` (title/header, PADD 1, Illinois' first row, Kansas with the `(s)` and
formula cells, PADD 5, total, footnotes; 19 plants), `lmop_composite_sample.xlsx` (whole Summary and Field
Descriptions sheets, 27 database rows including both multi-landfill projects and a no-Latitude Shutdown project),
`agstar_digesters_sample.xlsx` (18 + 7 rows including three Construction rows and the city-less row),
`eia_atlas_ethanol_plants_sample.zip` (12 real shapefile records with the `.prj`). The feature-service GeoJSON shape
could not be recorded (token block) and is exercised in-test with a synthetic two-feature collection, labelled as such.

### 10.5 Open for the coordinator

1. `services/ingest/assets.py::ASSET_TYPE_SOURCE_IDS` maps one source per asset type; `rng_project` has two here
   (LMOP, AgSTAR) and `ethanol_plant` two (capacity table, Atlas copy). Reading `source_id` from the frame, or a
   `(asset_type, source_id)` map, is the one-line loader change; until then a load attaches rows to the mapped source.
2. `_to_json_dict` in the same loader floats every attribute; folding `attributes_text` into `asset.attributes`
   needs that cast relaxed for strings.
3. `data/sources.yaml` `verified` rows for the four sources should record the URLs, `retrieved_at`, byte counts and
   the Atlas token finding above (the file was mid-edit by another lane during this run and unparsable; the lane's
   CLIs took `--manifest` pointing at the committed copy).
4. Browser task (docs/40 §0): confirm whether EIA now requires a login on the Atlas feature services or has moved
   them; until then the `map_data` zips are the route for every Atlas point layer.

## 11. CCS and CO₂ infrastructure — source map by data type (2026-09-25)

**Question (owner, 2026-09-25):** carbonstorage.io's data is visible but its sources are not; what does it hold, by
type, and where does each type come from for our own use? **Method:** carbonstorage.io was not fetched (it is
`NEVER_INGEST`, id-pinned); its nine asset classes and five feature types are as the coordinator recorded them from
its public navigation. Every primary source below was probed on 2026-09-25 through the repo's own
`PoliteSession` (robots.txt first per host, no retries, challenge pages reported as blocks); every probe's URL,
HTTP status, bytes and, for files, sha256 are in the `verified` block of its `data/sources.yaml` entry, and the
raw log sits in the lane's scratchpad. **Markers:** **M** = measured today; **I** = inferred (not fetched, or
fetched but the claim goes beyond what the bytes show). "Reg." = registered in the manifest; "Built" = a connector
exists. Terms are quoted verbatim where a clause was readable; `reuse: unknown` means no clause was read.
**Posture** (added 2026-09-25 under the owner's noncommercial decision, §11.6): `open` = federal §105, NLOD, CC BY and
similar; `usable-NC` = the grant is noncommercial-only, usable under the posture once the `noncommercial` reuse class
exists; `restricted-or-silent` = the terms restrict, or none were found; `blocked` = robots-disallowed, challenge page
or 403. Filled from the terms column and the manifest entries, nothing re-probed; where a row spans sources with
different postures they are listed; "—" where the row is not a source.

### 11.1 The nine asset classes

**Class VI (permits and applications)**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| UIC Class VI permit tracker (68 projects, 235 applications; excludes the six primacy states) — M | US EPA | Qlik engine JSON over websocket, anonymous — M | 17 U.S.C. §105 — M (`docs/13` §2.12 hedge) | `open` | app "Last Updated" 2026-09-11 — M | `us.epa.class_vi` | yes | high |
| Class VI application list PDF (18 rows, 14 columns) — M | Texas RRC | dated PDF table, page scraped for the URL — M | **read 2026-09-25**: "RRC grants permission to copy and distribute the information on its website for noncommercial use, as long as the content remains unaltered" — M; manifest `reuse: unknown` until the `noncommercial` class exists (see 11.4, 11.6) | `usable-NC` | file dated 2026-03-11 — M | `us.tx.rrc.class_vi` | no | mod |
| Class VI document folders, 7 storage facilities, per-facility injected-CO₂ PDFs — M | ND DMR | HTML document library — M | unread — `unknown` | `restricted-or-silent` | per case — I | `us.nd.dmr.class_vi` | no | mod |
| SONRIS Class VI applications — I (not fetched) | Louisiana DCE | Oracle APEX; **robots `Disallow: /`** — M (2026-09-22) | unread — `unknown` | `blocked` | unknown | `us.la.dce.class_vi` | no (never fetch) | n/a |
| Class VI lists at AZ (Cloudflare challenge), WV (no list published), WY (JS popup, intermittent interstitial) — M (2026-09-22) | state DEQs | browser only | unread — `unknown` | `blocked` (AZ challenge, WY interstitial) / `restricted-or-silent` (WV) | unknown | `us.az/wv/wy.*.class_vi` | no | low |
| Primacy grants (ND 2018, WY 2020, LA 2024, WV 2025-02, AZ 2025-09, TX 2025-11; **Colorado proposed 2026-03-19**) — M | Federal Register | JSON API — M | §105 — M | `open` | daily — M | `us.federalregister.api` | no | high |

The 127-vs-68 gap the coordinator measured is the primacy states (Texas alone has 50 Class VI wells in EPA's
FY2024 inventory, Illinois 22 — the latter already in the EPA tracker). Colorado will be a seventh primacy
state; the Federal Register feed is how the primacy set stays current — I.

**Operational storage**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GHGRP Subpart RR — sheet "Geologic Sequestration of CO2": **20 facilities RY2023** with total mass sequestered (Hobbs Field 5.20 Mt, Seminole SAU 3.93 Mt, Wasson 3.67 Mt, ADM Decatur 0.54 Mt, Shute Creek 0.44 Mt, Red Trail 0.16 Mt, Blue Flint 0.03 Mt …) — M | US EPA | annual zip (28,389,973 B, sha256 `895349c8…bf8345`); Envirofacts `pub_dim_facility` filter `reported_subparts CONTAINING RR` (63 facility-years, 20 in 2023, 15 with `rr_mrv_plan_url`) — M | §105 — M | `open` | RY2023; **no RY2024 file published as of 2026-09-25** — M | `us.epa.ghgrp.subpart_rr` | no | high |
| Subpart RR Annual Monitoring Reports page: 19 facilities, one PDF per facility-year 2016–2023 — M | US EPA | HTML table → PDFs — M | §105 — M | `open` | page updated 2026-02-19 — M | same id | no | high |
| Per-facility injected volumes (ND CO2Reporting folder) — M | ND DMR | PDFs — M | unread | `restricted-or-silent` | per report — I | `us.nd.dmr.class_vi` | no | mod |

Twelve of the twenty RR reporters are CO₂-EOR fields (Occidental, CapturePoint, Core Energy, Petra Nova); the
dedicated/saline set is ADM, Red Trail, Blue Flint, Barnett RDC, SPG Bowie, Great Plains — M. "Operational
storage" in the US is that short. No Tallgrass / Eastern Wyoming row exists through RY2023 — M.

**Planned storage**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Class VI applications (rows above) — M | EPA + primacy states | as above | as above | as above: `open` (EPA) / `usable-NC` (TX) / `restricted-or-silent` (ND, WV) / `blocked` (LA, AZ, WY) | as above | as above | partly | high |
| CarbonSAFE / storage-hub awards: `DEFE0032442` Four Corners Phase III $42.7M, `DEFE0032444` Polk $55.8M, `DEFE0032340` Illinois Basin West $20.5M, `DEFE0032449` Paradise KY $9.0M, `DEFE0032625` Meriden Carbon WY $2.25M … — M | US Treasury (USAspending) | JSON POST, slow (45 s timeouts; one 502) — M | §105 — M | `open` | daily — M | `us.usaspending` | no | high |
| CO₂ storage exploration/exploitation licences, complete history (first exploitation licence 2019, first exploration licence 2022) — M | Norwegian Offshore Directorate | FactPages HTML (list page timed out ×2, index read) — M | "may be used in accordance with Norwegian Licence for Open Government Data (NLOD)" — M; NLOD text unread | `open` | daily sync — M | `no.sodir.factpages_co2_storage` | no | mod |
| Carbon storage licence areas (2023 round) — I | NSTA (GB) | ArcGIS Hub, client-rendered; no licence clause located — M | unread — `unknown` | `restricted-or-silent` | per round — I | `gb.nsta.carbon_storage_licences` | no | low |
| NETL carbon-storage portfolio / CarbonSAFE pages — **unpublished** (Drupal "You are not authorized to access this page", 403 ×3; `/project-information` 200 but empty) — M | NETL | n/a | §105 | `blocked` (403; §105 if republished) | n/a | `us.doe.netl.carbon_storage_portfolio` | no | high (that it is gone) |

**Capture**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GHGRP Subpart PP — suppliers of CO₂ (capture/separation): **138 facilities RY2023**, 1,771 facility-years; "Suppliers" sheet column "GHG Quantity Associated with CO2 Supply" — M | US EPA | same zip; Envirofacts `pp_subpart_level_information` — M | §105 — M | `open` | RY2023 — M | `us.epa.ghgrp.subpart_uu_pp` | no | high |
| Facility-level `co2_captured` flag and parent-company shares (136,005 facility-years) — M (2026-09-22) | US EPA | Envirofacts REST — M | §105 | `open` | RY2023 | `us.epa.ghgrp` | no | high |
| OCED Carbon Capture Demonstration / Large-Scale Pilot selections — page lists funding announcements only, no selections; portfolio is client-rendered — M | DOE OCED (now `energy.gov/cmei/oced`) | browser task | §105 | `open` | volatile (2025-10-01: 321 awards terminated, list not published) — M | `us.doe.oced.portfolio` | no | mod |
| Planned capture at ethanol plants (Gevo, REX 8-Ks) — M | SEC EDGAR full-text search (48 hits/12 mo for "Class VI" "carbon dioxide") | JSON, declared UA, ≤10 rps — M | "Anyone can access and download this information for free" — M; derived-only | `open` | realtime — M | `us.sec.edgar_fts` | no | high |

**EOR (CO₂ enhanced oil recovery)**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GHGRP Subpart UU — "CO2 Injection" sheet: **81 facilities RY2023** (TX 34, WY 14, NM 9, MS 8, ND 3, MI 3, MT 3, LA 2, CO 2, OK 2, AR 1) with lat/lon and quantity received for injection; 1,207 facility-years — M | US EPA | same zip; Envirofacts filter — M | §105 — M | `open` | RY2023 — M | `us.epa.ghgrp.subpart_uu_pp` | no | high |
| UIC well inventory FY2024: Class II recovery wells by state (national 106,656; TX 36,852, CA 23,923, KS 11,336, IL 6,656, OK 6,251, WY 4,292) — M | US EPA | xlsx (16,905 B, sha256 `bd6ff9d9…6d66`) — M | §105 — M | `open` | annual, posted 2026-02-05 — M | `us.epa.uic.well_inventory` | no | high |
| Per-well Class II: RRC UIC database (monthly EBCDIC/ASCII dumps) — M | Texas RRC | bulk fixed-width — M | noncommercial-only clause (quoted above) — M | `usable-NC` | monthly — M | `us.tx.rrc.datasets` | no | mod |
| Per-well UIC injection volumes 2011–2025 (xlsx) + RBDMS wells zip — M | Oklahoma OCC | bulk xlsx/zip — M | no reuse clause; site notice is a vendor DMCA procedure — M | `restricted-or-silent` | annual — M | `us.ok.occ.well_data` | no | mod |
| OCD FTP data sets — M (page) | New Mexico OCD | FTP bulk — M | unread | `restricted-or-silent` | unknown | `us.nm.ocd.data` | no | low |
| ks_wells.zip master list — M (page) | Kansas Geological Survey | bulk — M | unread (university) | `restricted-or-silent` | monthly — M | `us.ks.kgs.wells` | no | low |
| WOGCC DataExplorer (offline on probe day); legacy `pipeline.wyo.gov` **robots `Disallow: /`** — M | Wyoming OGCC | JS app / blocked | unread | `blocked` (legacy host robots; DataExplorer offline, terms unread) | unknown | `us.wy.ogcc.data` | no | low |
| NDIC well search, hearing dockets — M (page) | ND DMR | HTML | unread | `restricted-or-silent` | daily — M | `us.nd.dmr.oilgas` | no | low |
| Louisiana Class II — not fetched (SONRIS robots) | Louisiana DCE | n/a | unread | `blocked` | n/a | (see `us.la.dce.class_vi`) | no | n/a |

**Carbon removal (DAC)**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Regional DAC Hubs page — FOAs only (DE-FOA-0002735 $3.5B Dec 2022; $1.8B NOFO Dec 2024), no selections listed — M | DOE OCED | HTML | §105 | `open` | stale — M | `us.doe.oced.portfolio` | no | mod |
| DAC awards by keyword ("direct air capture": `DEFE0032375` Illinois Basin Regional DAC Hub $2.9M seen in the CarbonSAFE query; the dedicated query timed out) — M/I | USAspending | JSON POST — M | §105 | `open` | daily | `us.usaspending` | no | mod |
| Class VI applications by DAC sponsors (the storage half of a DAC project) — I | EPA / states | as above | as above | as above (mixed) | as above | as above | partly | mod |
| "Carbon Negative Shot" — a DOE goal, not a dataset; its page 404s after the reorganisation — M | DOE | n/a | n/a | — | n/a | not registered (nothing to register) | — | high |

Private DAC registries (cdr.fyi, Puro, Isometric) were not probed: aggregators or registries with their own terms,
same class as `global.carbonstorage_io` until read — I.

**CO₂ pipelines**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Hazardous liquid annual reports, Part D mileage by commodity ("Carbon Dioxide") by operator, state, diameter — **tabular, no geometry** — I (file not yet fetched) | PHMSA | zip; origin 403 (AkamaiGHost) honoured; one archive attempt 2026-09-25 → connection reset, not retried — M | §105; `docs/13` §2.16 route reasoning — M | `open` (origin 403 honoured; archive route) | annual (due March 15) — I | `us.phmsa.hazardous_liquid_annual` | no | high (data) / low (route) |
| NPMS / PIMMA geometry — **closed**: "The general public and private companies may not access PIMMA" — M (coordinator, 2026-09-25) | PHMSA | never | n/a | `restricted-or-silent` | n/a | not registered (never a route) | — | high |
| Hydrocarbon and Carbon Dioxide Pipeline Dockets: **HP22-001 SCS Carbon Transport filed 02/07/22 closed 10/13/23; HP22-002 Navigator Heartland Greenway filed 09/27/22 closed 10/26/23**; 2024 folder present — M | South Dakota PUC | static HTML index per year — M | unread (footer "Disclaimer" not read) | `restricted-or-silent` | per filing — M | `us.sd.puc.co2_pipeline_dockets` | no | high |
| Case search + case-detail pages (full filing ladder with filer, type, pages, date) — M | North Dakota PSC | HTML — M | unread | `restricted-or-silent` | per filing | `us.nd.psc.case_search` | no | mod |
| EFS dockets HLP-2021-0001 (Summit), Navigator, Wolf — **robots `User-agent: * / Disallow: / / Allow: /$`**, nothing fetched — M | Iowa Utilities Commission (renamed from IUB; `iub.iowa.gov` → `iuc.iowa.gov`) | blocked | unread | `blocked` | n/a | `us.ia.iuc.efs` | no (never fetch) | n/a |
| e-Docket (SAFE CCS Act certificates) — SPA, served HTML empty; guessed API path 500 — M | Illinois Commerce Commission | browser only | unread | `restricted-or-silent` | per filing | `us.il.icc.edocket` | no | low |
| eDockets (Summit Otter Tail–Wilkin) — **bare 403** — M | Minnesota PUC | blocked | unread | `blocked` | n/a | `us.mn.puc.edockets` | no | n/a |
| Who regulates: PHMSA safety (49 U.S.C. ch. 601); **STB** common-carrier rates, 49 U.S.C. §15301(a) "transportation by pipeline … when transporting a commodity other than water, gas, or oil" — M (uscode.house.gov, in force 2026-09-24); siting = states; **FERC: no role** (neither NGA nor ICA reaches CO₂) — M/I | — | — | — | — | — | `us.federalregister.api` (PHMSA rulemaking docket, 270 docs) | — | high |

**e-Fuels (and hydrogen)**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Regional Clean Hydrogen Hubs page (programme text; selections not listed on the page) — M | DOE OCED | HTML | §105 | `open` | volatile | `us.doe.oced.portfolio` | no | mod |
| Global hydrogen tracker (CC BY 4.0; TZ rows dropped) — recorded 2026-09-12/20 | Global Energy Monitor | xlsx | CC BY 4.0 — `docs/13` §2.2 | `open` / `usable-NC` (TZ-ID rows, CC BY-NC 4.0) | quarterly | `global.gem.trackers` | partly | high |
| RFS Part 80 registrations (fuel producers incl. e-fuel pathways) — recorded 2026-09-19 | US EPA | xlsx | §105 | `open` | continuous | `us.epa.rfs_public_data` | no | mod |
| 8-K/10-K announcements (Gevo etc.) — M | SEC EDGAR | as above | as above | `open` | realtime | `us.sec.edgar_fts` | no | high |
| Grants.gov / DOE eXCHANGE FOAs — recorded | DOE | JSON/HTML | §105 | `open` | daily | `us.grants_gov.search2`, `us.doe.exchange_portals` | no | high |

There is no e-fuels register anywhere; the class is announcements plus funding plus RFS registration — I.

**Stratigraphic wells**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| State oil-and-gas well permits (a stratigraphic test well for a storage site is an ordinary state-permitted well: NDIC well search; RRC drilling-permit master with lat/long; OCC RBDMS; KGS master list; WOGCC) — M (pages) / I (that the strat wells are findable by type) | state commissions | bulk / HTML per state | unread or noncommercial (TX) | `restricted-or-silent` (ND, OK, KS) / `usable-NC` (TX) / `blocked` (WY legacy host) | monthly–daily | `us.nd.dmr.oilgas`, `us.tx.rrc.datasets`, `us.ok.occ.well_data`, `us.ks.kgs.wells`, `us.wy.ogcc.data` | no | mod |
| Class VI applications' well counts and injection intervals (EPA tracker "# of Permit Applications"; RRC list "No. of Inj. Well", "Inj. Interval (TVD)") — M | EPA / RRC | as above | as above | `open` (EPA) / `usable-NC` (RRC) | as above | as above | partly | high |
| CarbonSAFE site-characterisation awards (each Phase II/III award drilled or drills a strat well) — M (awards) / I (well link) | USAspending | as above | §105 | `open` | daily | `us.usaspending` | no | mod |
| CO₂ storage wellbores (FactPages shortcut "CO2 storage wellbores") — M (link) | Norwegian Offshore Directorate | FactPages | NLOD — M | `open` | daily | `no.sodir.factpages_co2_storage` | no | mod |
| Louisiana strat wells — not fetched (SONRIS) | Louisiana DCE | n/a | unread | `blocked` | n/a | — | no | n/a |

"Global stratigraphic wells" as a single dataset does not exist publicly; it is a per-jurisdiction join of well
registers, and outside ND/TX/OK/KS/NO the terms are unread — I.

### 11.2 The five feature types (and DOE funding)

**FOIA results**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EPA FOIA Reading Room (records already released, searchable by office "file cabinet"; Office of Water holds UIC) — M (room reached, search not run) | US EPA | ASP.NET app (`securefoia.epa.gov`) — M | §105 for EPA-authored; **applicant-authored documents keep their copyright** → derived-only — I | `open` | continuous | `us.epa.foia.reading_room` | no | mod |
| EPA FOIA logs FY2009–FY2026 Q3 — quarterly **PDFs** (2.4–16 MB), requester names inside — M | US EPA | PDF extraction — M | §105; requester names are personal data (`docs/13` §5.4) | `open` | quarterly — M | same id | no | high |
| DOE FOIA responses page — 2021-era items, **no CCS entry** — M | DOE | HTML | §105 | `open` | irregular | `us.doe.foia.reading_room` | no | high |
| FOIA.gov — annual-report data and an agency API, no request logs — M | DOJ OIP | JSON (key) | §105 | `open` | annual | not registered (nothing project-level) | — | high |
| A FOIA request of our own — an owner action with a fee and a queue, **not a feed** | — | — | — | — | months | — | — | — |

**News coverage**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SEC EDGAR full-text search: 48 hits/12 months for "Class VI" "carbon dioxide" (Gevo, REX American …) — M | SEC | JSON; declared UA; ≤10 rps ("The SEC does not allow botnets or automated tools to crawl the site … outside of the acceptable policy") — M | free access; registrant-authored → derived-only — M | `open` | realtime | `us.sec.edgar_fts` | no | high |
| Federal Register: 63 "Class VI" "carbon dioxide" documents, 270 PHMSA CO₂-pipeline documents — M | OFR/GPO | JSON API | §105 | `open` | daily | `us.federalregister.api` | no | high |
| GDELT DOC (1 request / 5 s) — recorded 2026-09-12 | GDELT | JSON | attribution; derived-only | `open` | 15-min | `news.gdelt.doc` | no | high |
| OCED news listing (RSS at `/rss/cmei-oced/4820508`, newest item 2026-08-21) — M | DOE | HTML/RSS | §105 | `open` | irregular | `us.doe.oced.portfolio` | no | mod |
| EPA news-release search RSS — **robots `Disallow: /newsreleases/search/`** (also `/publicnotices/notices-search/`) — M | US EPA | blocked path | §105 | `blocked` | — | not registered | — | high |
| Google News RSS, wires, trade press — recorded | various | RSS | restricted / DSM Art. 4 | `restricted-or-silent` | realtime | `news.google_rss`, `news.wires`, `news.trade_press` | no | — |

The lawful CCS news feed is therefore: EDGAR + Federal Register (both public domain, structured, realtime) for
the listed and the regulated, GDELT headlines for everyone else, and never article bodies — I.

**Permits**

Covered by the Class VI, EOR (Class II) and CO₂-pipeline tables above; the one addition is the Subpart RR MRV-plan
approval (`rr_mrv_plan_url`, 15 of 20 RY2023 facilities), which is EPA's permit-like decision for storage
accounting — M.

**Downloadable files**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Class VI permit and application PDFs (ND folders, 67 links; EPA per-project `udr.epa.gov` links; RRC per-application PDFs) — M | ND DMR / EPA / RRC | HTML → PDF | ND unread; EPA §105; TX noncommercial-only | `restricted-or-silent` (ND) / `open` (EPA) / `usable-NC` (TX) | per case | `us.nd.dmr.class_vi`, `us.epa.class_vi`, `us.tx.rrc.class_vi` | partly | mod |
| Subpart RR annual monitoring reports (one PDF per facility-year) and MRV plans — M | US EPA | HTML → PDF | §105 | `open` | annual | `us.epa.ghgrp.subpart_rr` | no | high |
| State docket filings (SD, ND) — M | state PUCs | HTML → PDF | unread | `restricted-or-silent` | per filing | `us.sd.puc.*`, `us.nd.psc.*` | no | mod |

**Global dataset of point-source emissions and emitting facilities**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GHGRP facilities: 136,005 facility-years, 11,281 for RY2023, lat/lon, NAICS, parent company with shares, `co2_captured`, `rr_mrv_plan_url` — M (2026-09-22) | US EPA | Envirofacts REST + annual zip — M | §105 | `open` | RY2023; RY2024 not yet published — M | `us.epa.ghgrp` | no | high |
| Non-US point sources (EU ETS registry, Climate TRACE, national inventories) — not probed | various | — | unread | `restricted-or-silent` (not probed) | — | not registered | — | n/a |

The "global" claim is not checkable from outside; the US half is free and public domain, and it is the half where
storage and capture actually join (RR/UU/PP rows are GHGRP facility ids) — I.

**DOE funding**

| Primary source | Publisher | Retrieval | Terms | Posture | Freshness | Reg. | Built | Conf. |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| USAspending award search (keyword "CarbonSAFE": 10+ awards with recipient, amount, dates, place of performance, description) — M; "Class VI" query 502 | US Treasury | JSON POST, ≥120 s timeout advised — M | §105 | `open` | daily | `us.usaspending` | no | high |
| Grants.gov Search2 / DOE eXCHANGE (FOAs) — recorded | DOE / HHS | JSON / HTML | §105 | `open` | daily | `us.grants_gov.search2`, `us.doe.exchange_portals` | no | high |
| OCED portfolio and map — client-rendered; programme pages list FOAs, not selections — M | DOE OCED | browser task | §105 | `open` | volatile | `us.doe.oced.portfolio` | no | mod |
| NETL carbon-storage portfolio — unpublished (403 "not authorized") — M | NETL | n/a | §105 | `blocked` (403; §105 if republished) | n/a | `us.doe.netl.carbon_storage_portfolio` | no | high |
| **DOE reorganisation (M):** `energy.gov/oced/*` → `/cmei/oced/*` (Office of Critical Minerals and Energy Innovation); `energy.gov/fecm/*` → `/hgeo/*` (Hydrocarbons and Geothermal Energy Office), whose focus areas are Coal, Oil & Gas, Geothermal, SPR, Natural Gas Regulation — **no carbon-management programme page survives** | DOE | — | — | — | — | — | — | high |

**45Q (asked in the brief).** No per-project public record exists — M/I. Verified by absence at the IRS: the 45Q
credit page URLs 404; the Form 8933 page is live and describes Schedules A/B/C (owner, disposal-operator and EOR-
operator *certifications*) — tax-return attachments, never published. The public proxy is Subpart RR: Treas. Reg.
§1.45Q-3 makes an RR MRV plan (or CSA/ISO 27916) the secure-storage test, so the RR facility list is the set of
sites that *can* claim 45Q for geologic storage — I. Aggregate claim totals exist only in SOI tables and oversight
reports (not fetched) — I.

### 11.3 What has no public primary source

The honest list of what carbonstorage.io's FOIA requests and hand-curation produce that no feed does — I unless marked:

1. **Lateral geometry and named laterals.** PHMSA Part D gives operator/state/diameter mileage totals; the Trailblazer
   breakdown (mainline 349.1 mi + ADM Columbus 83.1 + SIRE 130.0 + smaller laterals = 734.0) comes from operator
   statements, FERC abandonment/conversion filings and project pages, stitched by hand. NPMS geometry is closed — M.
2. **Contracted-plant lists** (which ethanol plants are signed to which pipeline or hub). Sponsor press releases,
   8-Ks for the listed minority, and state docket exhibits; no register.
3. **Hub detail** (sponsor consortia, capture-source rosters, phase capacities, "Permitted" status for a state-primacy
   site such as Eastern Wyoming Sequestration Hub). Wyoming's list is behind a JS popup — M; the rest is announcements.
4. **Pre-application and withdrawn intent** (projects announced but never filed). News only.
5. **Application documents in primacy states with closed systems** — Louisiana (SONRIS robots-disallowed — M),
   Arizona (challenge — M), Minnesota and Iowa dockets (403 / robots — M). FOIA or state public-records requests are
   the only route, and they are owner actions.
6. **Per-project 45Q claims** — M/I (above).
7. **Non-US "global" coverage beyond Norway (open) and GB (terms unread)** — everything else is announcements or
   restricted compilations (IEA challenge-blocked — M; GCCSI CO2RE and private DAC registries not probed).

### 11.4 Build order (rows unlocked per unit of effort)

| # | Source(s) | Rows unlocked (M unless marked) | Effort | Blocker / owner action |
|---|---|---|---|---|
| 1 | `us.epa.ghgrp.subpart_rr` + `us.epa.ghgrp.subpart_uu_pp` (one connector, one zip already hashed) | 20 storage + 81 injection + 138 supplier facilities with coordinates and quantities; the emitter↔storage join | S | none — public domain, file fetched today |
| 2 | `us.federalregister.api` | primacy-state map (7th state pending) + PHMSA rulemaking events | S | none |
| 3 | `us.sec.edgar_fts` | ~50 CCS filings/yr from listed companies, realtime | S | none (declared UA, ≤1 rps) |
| 4 | `us.usaspending` CCS queries (CarbonSAFE, DAC, capture demos) | tens of awards with amounts, dates, places; status changes after the 2025 terminations | S | none; slow endpoint, long timeout |
| 5 | `us.tx.rrc.class_vi` (+ `us.tx.rrc.datasets` later) | 18 Class VI applications now; 36,852 Class II recovery wells and every drilling permit with lat/long later | M | **unblocked** by the owner's 2026-09-25 noncommercial posture (`docs/00-PLAN.md` decisions log; §11.6); waits only on the `noncommercial` reuse class another lane is adding — the connector stays gated to quarantine until then |
| 6 | `us.epa.uic.well_inventory` | 58 state rows sizing Class II/VI per state | S | none |
| 7 | `us.sd.puc.co2_pipeline_dockets` + `us.nd.psc.case_search` | a handful of CO₂ pipeline dockets with lifecycle dates | S–M | state terms read (SD "Disclaimer", ND) |
| 8 | `us.nd.dmr.class_vi` document ingest (+ `us.nd.dmr.oilgas`) | 7 facilities' orders/permits + injected volumes | M | ND terms read |
| 9 | `us.ok.occ.well_data` | per-well UIC injection volumes 2011–2025 | M | OK terms (none found — owner/counsel) |
| 10 | `us.phmsa.hazardous_liquid_annual` | CO₂ mileage by operator/state | M | archive route retry (features lane) |
| 11 | `no.sodir.factpages_co2_storage` | Norwegian CO₂ storage licences with history | M | NLOD text read and credit line fixed |
| 12 | `us.epa.foia.reading_room` | already-released UIC records + FOIA-log subjects | M | derived-only rule; requester names never stored |
| 13 | Louisiana | the largest primacy-state Class VI set | — | **owner: data-sharing request to DCE** (SONRIS is robots-disallowed; nothing else exists) |
| 14 | AZ / WY / IL ICC / MN / OCED portfolio / NSTA | small lists each | L | browser worker (`docs/40` §0) + terms |
| 15 | Class VI in Colorado (proposed primacy) | future | — | watch `us.federalregister.api` |

### 11.5 Measured differences from the 2026-09-25 brief

- Envirofacts has **no** `rr_`/`uu_subpart_level_information` tables (404); RR/UU come from the `pub_dim_facility`
  `reported_subparts` filter or the annual zip's sheets; `pp_subpart_level_information` exists — M.
- Subpart RR is not "operational storage" in the saline sense: 12 of 20 reporters are EOR fields — M.
- NETL's project portfolio and CarbonSAFE pages are **unpublished** (403 "not authorized"), not merely unregistered; DOE's
  FECM no longer exists as a web presence (→ HGEO, no carbon-management area) — M.
- The Texas RRC terms *were* readable headlessly and are quoted: noncommercial-only — M (the brief had them
  "noncommercial-only, parked" as a claim; now evidenced).
- Colorado Class VI primacy is proposed (2026-03-19): the primacy set is seven-in-waiting, not six — M.
- The Iowa siting authority is the Iowa Utilities *Commission* and its docket system is robots-disallowed; SD's docket
  index is a plain HTML list — M.
- FERC's non-role is confirmed by statute rather than by a FERC page (ferc.gov and CRS were not reachable: CRS answered a
  challenge) — M/I.

### 11.6 Unlocked by the noncommercial posture (2026-09-25)

Owner, 2026-09-25, verbatim: "Let's move forward as a noncommercial platform for now for maximum and best data
access. Then decide how to proceed once we're done." Under it a source whose only obstacle is a noncommercial grant is
usable, not dead. Nothing is reclassified here — the vocabulary has no `noncommercial` reuse class yet (another lane is
adding one with a platform-posture setting); the posture column above marks the map so that reclassification is
mechanical. Measured over the 106 probes of this lane:

- `us.tx.rrc.class_vi` — 18–20 Class VI applications now (the committed connector reads the 2026-09-22 release: 20
  rows); `us.tx.rrc.datasets` — 36,852 Class II recovery wells (EPA FY2024 inventory) via the monthly UIC database
  dumps, plus every drilling permit with lat/long (stratigraphic test wells included). Obstacle was solely the RRC
  "noncommercial use" grant. Both manifest entries carry the marker `noncommercial (pending class)` in `notes`.
- `global.gem.trackers` TZ-ID rows — CC BY-NC 4.0 (`docs/13` §2.2, §6 row: "drop TZ rows at ingest"); the register
  carries no row count for the dropped set, so the count is unmeasured here.
- gem.wiki prose — CC BY-NC-SA (`docs/13` §2.2), never ingested; usable for derived facts under the posture, still
  share-alike on any republished text.
- Nothing else in the 106 probes was blocked only by a noncommercial grant: every other non-federal source is either
  silent on terms (`unknown`), robots-disallowed (SONRIS, `efs.iowa.gov`, `pipeline.wyo.gov`), challenge/403-blocked
  (AZ DEQ, IEA, CRS, MN eDockets), or open (federal §105, Sodir NLOD, SEC).

The posture is true only if three preconditions the coordinator has put to the owner hold — the pricing surfaces
suspended or marked inactive; counsel's confirmation that a pre-revenue LLC feeding a commercial deal workflow can
hold noncommercial status; and a firewall keeping noncommercial rows out of any downstream commercial use — which the
posture lane is writing up in `docs/26`.
