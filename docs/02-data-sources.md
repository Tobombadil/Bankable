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

## 8. Reference data: GB substation gazetteer

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
