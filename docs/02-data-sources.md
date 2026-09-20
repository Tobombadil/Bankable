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
| Open with attribution | LBNL (CC BY), GEM (CC BY), NESO Open Data, OGL (UK), OGL-Canada, dl-de/by (MaStR), EU reuse (TED), World Bank (CC BY) | Republish with credit line and licence link on every derived record. |
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
4. Change detection and the public delayed feed; Bluesky + LinkedIn syndication; RSS/email alerts.
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
