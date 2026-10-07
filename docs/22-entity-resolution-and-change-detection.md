# Entity resolution and change detection: design, harmonisation table, measured results

**Status:** Phase 2 prototype · 2026-09-12 · data-scientist · code in `pipeline/`, tests in `tests/`,
evaluation data in `data/eval/` · companion to `docs/02` §5 (schema, key order) and `docs/20` §3.3–3.5.

Everything numeric below was measured on the 2026-09-12 pull. Nothing is extrapolated except the LLM
cost in §9, which is labelled as an estimate from list prices.

## 1. Inputs and the commands that produced every number here

```
.venv/bin/python -m pipeline.connectors run --all       # replaced pipeline/pull.py (Sprint 1); the evaluation
                                                       # inputs here came from the 2026-09-12 pull: data/eval/raw/manifest.2026-09-12.json
.venv/bin/python pipeline/normalize.py                  # -> data/eval/normalized.parquet
.venv/bin/python pipeline/resolve.py --sweep            # -> data/eval/matches.parquet, clusters.parquet; P/R vs labels.csv
.venv/bin/python pipeline/diff.py --demo                # -> data/eval/normalized.perturbed.parquet, events.parquet
.venv/bin/python -m pytest tests/ -q
```

| Source | Rows | Retrieved (UTC) | Note |
|---|---|---|---|
| CAISO public queue | 2,278 | 16:37:21 | gridstatus |
| ERCOT GIS report | 1,778 | 16:37:24 | gridstatus; **no withdrawn rows exist in the file** |
| SPP GI summary | 3,074 | 16:37:25 | gridstatus; no project names, no sponsors |
| NYISO queue | 3,164 | 16:37:27 | gridstatus; 1,350 rows carry only State + Status |
| ISO-NE IRTT | 1,751 | 16:37:34 | gridstatus; queue ids reused |
| EIA-860M Planned (July 2026) | 2,343 | 16:37:06 | `july_generator2026.xlsx`, header row 3 |
| **Total** | **14,388** | | 0.77 MB parquet |

PJM (API key) and MISO (Cloudflare 403) are out of scope until the gates in `docs/02` §2 clear.

## 2. Canonical record (`pipeline/normalize.py`)

One row per source record, columns from `docs/02` §5 plus resolution keys:
`record_id, source_id, source_record_id, source_url, retrieved_at, licence | kind, name_canonical, name_norm,
sponsor_name, sponsor_norm, technology, technology_raw, capacity_mw, storage_mwh | iso, state, county, county_norm |
lifecycle_state, status_raw, status_rule, status_conflict, queue_date, proposed_cod | queue_id, eia_plant_id,
eia_generator_id, cross_refs`.

Field coverage across the 14,388 records (non-null): state 97.5%, capacity 86.9%, county 89.0%, queue_date
74.0%, name **69.2%**, proposed_cod 68.3%, sponsor **39.8%** (only ERCOT and EIA publish one), eia_plant_id 16.3%
(EIA only). These two gaps, name and sponsor, bound what resolution can do (§7).

Decisions taken in normalisation, each of which changed a measured number:

- **Capacity 0.0 is missing, not zero.** ERCOT reports 0.0 MW for 75 co-located "SLF" storage additions and
  repowers; NYISO for 1,541 rows. Fallback order: nameplate → summer → winter; ISO-NE's 278 null nameplates
  recover 67 values from the summer/winter columns (the rest are null there too). Coverage moves from a misleading 98.1% to an honest 86.9%.
- **`record_id` must be unique.** ISO-NE reuses queue ids (92 ids over 242 rows, 27 of them across different
  states) and NYISO has 1,350 rows with no queue position at all. No-id rows get a content hash
  (`docs/20` §3.1); repeat ids get a suffix. 1,505 ids were disambiguated this way, 1,348 of which are NYISO
  withdrawn rows carrying only State + Status, which are indistinguishable from each other by design.
  **Changed 2026-09-18** (audit §3.1): the suffix was `#2, #3…` in file order, so a row reorder in the source
  swapped identities and fabricated a withdrawal plus a re-creation per repeated id. It is now
  `#h<10 hex>`, a hash of the row's stable fields (name, capacity, county/state, technology, sponsor —
  `pipeline/connectors/dedupe.py::content_disambiguator`), on every member of a repeated group; status and dates
  are excluded so a status change never becomes a new identity. Rows with identical stable fields fall back to
  the raw-row hash, byte-identical rows to their order among themselves (interchangeable, so harmless). Stored
  rows keyed the old way are not migrated: the runner re-keys a previous snapshot by the same hash before the diff
  (`align_previous_keys`), and the loader matches an incoming row against stored siblings of its natural key by
  stable fields (`services/ingest/loader.py::_match_link`), so the first run after the change updates in place.
- **Technology vocabulary** of 26 values via ordered regexes. The test suite caught that ERCOT's
  "Combustion (gas) Turbine, but not part of a Combined-Cycle" (52 rows) matched the combined-cycle rule
  first; fixed with explicit exclusion rules. `unknown` = 1,840 rows, of which NYISO 1,562 (the no-id rows)
  and ISO-NE 265.
  **Rule order corrected 2026-10-06** (audit 2026-09-30, data scientist F4). Offshore wind now matches either
  word order (NESO writes "Wind Offshore") and the permits dashboard's "Wind: Federal Offshore"; pumped storage
  includes NESO's "Pump Storage"; nuclear precedes the steam-turbine rules (ERCOT's STP Unit 2 uprate, "Nuclear -
  Steam Turbine other than Combined-Cycle", was `gas_steam`); a new class `marine` takes NESO "Tidal" (was
  `other`); an LNG terminal "in State Water" is `gas_other`, not `hydro`. Rows that change class, over the
  normalised frames: NESO 173 of 2,198 (`wind`→`wind_offshore` 145, `storage`→`pumped_storage` 20,
  `other`→`marine` 7, `wind_storage`→`pumped_storage` 1 for "Pump Storage;Wind Onshore"), permits dashboard 26 of
  104 (21 federal offshore wind, 5 LNG terminals), ERCOT 1 of 1,778; EIA-860M, its plant context, CAISO, NYISO and
  Virginia DEQ 0. Every distinct raw string in those frames (163) is pinned raw → class in
  `data/eval/technology_classes.csv` by `tests/test_technology_classifier.py`. Not fixed: a NESO compound string
  takes its first matching rule, so "CCGT;Demand;Energy Storage System;Hydrogen;OCGT" is `storage`.
- **Cross-references** in names (`"Chazy Lake BESS (NYISO-C24-308)"`) are extracted into `cross_refs`
  only when the id contains a digit; the first version matched prose ("PJM Rainey") and was wrong.

## 3. Status harmonisation (`pipeline/status_map.yaml`, versioned data, not code)

Vocabulary from `docs/02` §1 — `announced → filed → studied → permitted → contracted → built`, terminal
`withdrawn` / `cancelled`, plus `unknown` — with one documented extension, **`under_construction`** between
`contracted` and `built`, because EIA-860M reports it explicitly and it is the single most useful pre-COD signal.
Refine rules run first, in file order; then the base map; else `unknown` with `status_rule = <src>.unmapped`.

| Source | Raw value(s) | Canonical | Rule / caveat |
|---|---|---|---|
| CAISO | ACTIVE | studied | base |
| | ACTIVE + IA "Executed" (214 rows) | contracted | `caiso.active_ia_executed` |
| | ACTIVE + IA "Filed Unexecuted" (1) | permitted | `caiso.active_ia_filed` |
| | COMPLETED | built | |
| | WITHDRAWN | withdrawn | 65 withdrawn rows carry an executed IA → `status_conflict` |
| ERCOT | Approved for Synchronization present (117: 111 Completed, 6 Active) | built | `ercot.synchronized`; keyed on ERCOT's milestone dates, not gridstatus's Status (§3.1) |
| | Approved for Energization present, not synchronised (4) | under_construction | `ercot.energized` |
| | IA Signed present, neither later milestone (465) | contracted | `ercot.ia_signed`; gridstatus calls these "Completed" |
| | none of the three (1,192) | studied | base (`Active`) |
| | *(withdrawal)* | — | **not representable**: withdrawn projects vanish from the file; only a `removed` diff event can show it |
| SPP | WITHDRAWN | withdrawn | keyed on **Status (Original)**, not gridstatus's harmonised Status |
| | TERMINATED (47) | cancelled | |
| | IA FULLY EXECUTED/COMMERCIAL OPERATION (333) | built | |
| | IA FULLY EXECUTED/ON SCHEDULE (302) | contracted | gridstatus labels these "Completed" |
| | IA FULLY EXECUTED/ON SUSPENSION (25) | contracted | as above; suspension noted |
| | IA PENDING, DISIS STAGE, FACILITY STUDY STAGE, SPECIAL STUDY, ERAS, ICS | studied | |
| | blank original (34) | fallback to gridstatus Status → else unknown | |
| NYISO | Active / Completed / Withdrawn | studied / built / withdrawn | SGIA Tender Date is empty in all 3,164 rows, so no `contracted` refinement possible |
| ISO-NE | Withdrawn (sheet membership) | withdrawn | `isone.withdrawn_wins`; 214 rows also say Project Status "Under Study", 1 "In Service" → `status_conflict` |
| | Project Status In Service / Under Construction / Suspended | built / under_construction / contracted | |
| | Active + IA "Executed" | contracted | `isone.ia_executed` |
| | Active / Completed | studied / built | base |
| EIA-860M | (P) approvals not initiated (688) | announced | |
| | (L) approvals pending (395) | filed | |
| | (T) approvals received (241) | permitted | |
| | (U) ≤50% built (498), (V) >50% (367), (TS) complete, pre-COD (152) | under_construction | (TS) is **not** built: no COD yet |
| | *(completion)* | — | a unit leaves the Planned sheet on COD; only a `removed` event shows it |

Result, 14,388 rows (re-run 2026-09-30 with the §3.1 ERCOT rules): withdrawn 7,889 · studied 1,811 · built 1,211 ·
contracted 1,045 · under_construction 1,024 · announced 688 · filed 395 · permitted 242 · cancelled 47 · unknown 36.
`status_conflict` = 325 rows. (Before §3.1: studied 1,815 · built 1,674 · contracted 580 · under_construction 1,022.)

The one number that matters most: **327 SPP rows (49.5% of what gridstatus calls "Completed" for SPP) have an
executed IA but no commercial operation**. Any product that reuses gridstatus's status as-is overstates SPP
completions by half.

### 3.1 ERCOT: the lifecycle keys on ERCOT's milestone dates (2026-09-30, lane FX1)

**The defect (audit F1).** ERCOT's GIS report has no status column. gridstatus 0.36.0 derives one
(`ercot.py:1573-1581`): `Completed` whenever `IA Signed` is non-null, `Active` otherwise. The map read
`Completed` as `built`, so every project with only a signed interconnection agreement was published as built.
On the audit's store copy that is 352 live ERCOT proposals, 90,020.3 MW, 347 of them with a projected COD after
2026-09-30 (re-measured here on a copy of `audit/data_scientist/store.db`; same numbers). The Siete page read
"Observed as built" above a 2029 COD. The `ercot.energized` rule required `Active`, so it caught 2 rows.

**The rule now** (`pipeline/status_map.yaml` `ercot`, version 3). Refine rules, latest milestone first:
`Approved for Synchronization` → built (`ercot.synchronized`); `Approved for Energization` → under_construction
(`ercot.energized`); `IA Signed` → contracted (`ercot.ia_signed`, the vocabulary's state for an executed
agreement, `data/vocabulary/lifecycle_states.yaml`); none → studied (base map, `Active`). The base map's
`Completed` entry is now `contracted`, reachable only if gridstatus ever labels a row Completed without an IA
date. `pipeline/normalize.py::iso_status_contexts` passes `approved_for_synchronization` into the rule context
(it was the one milestone that did not reach it), and the connector lists the column in `key_source_columns`,
so its disappearance holds the run. `status_raw` keeps gridstatus's `Completed`/`Active` unaltered, and
`status_rule` names the milestone that decided the state.

**Why synchronisation is built.** `Approved for Synchronization` equals the report's own `Actual Completion Date`
on all 117 rows that carry either (2026-09-12 pull). It precedes a formal commercial-operation declaration, so an
ERCOT `built` row is grid-synchronised; the caveat says so. Six `Active` rows are synchronised with no IA date
(repowers, two CPS rotor replacements, one SLF addition, all under an existing agreement) and are built.

**Measured on the stored ERCOT frames** (1,778 rows; the 2026-09-12 eval pull and the stored
`normalized/us.iso.ercot.gen_queue/20260913T202528Z.parquet` give identical counts):

| gridstatus Status | Before | After |
|---|---|---|
| Completed (580) | built 580 | built 111 · under_construction 4 · contracted 465 |
| Active (1,198) | studied 1,196 · under_construction 2 | studied 1,192 · built 6 |
| MW | built 136,974.0 · under_construction 128.4 | built 20,471.7 · under_construction 719.0 · contracted 115,965.2 |

The 580 Completed rows split 111 / 4 / 465, exactly as the audit predicted. The six Active rows are the only
difference from its figure, and the audit counted Completed rows only. 469 of the 580 have an IA and no
synchronisation approval (116,684 MW; 464 with a projected COD after 2026-09-30).

**Downstream, measured.**
- Grid points: loading the stored frame and then the corrected one into an empty store and reading
  `services.api.interconnection_points.point_totals` over ERCOT's 1,248 points gives active 301,112.5 MW
  (1,198 proposals) → 417,614.8 MW (1,661), built 136,974.0 MW (580) → 20,471.7 MW (117). On the audit's
  multi-source store copy, with a survivor's state changed only where it was its ERCOT row's own: live
  `iso=ERCOT` active 369.8 → 459.7 GW (the audit's 369.8 reproduced), at ERCOT-operated points 328.6 → 418.4 GW,
  built 103.3 → 13.5 GW, 358 proposals changed.
- Resolver (§22 methods, threshold 75): tp/fp/fn/tn 37/1/3/44 before and after, the same 764 accepted pairs and
  identical scores on all 59,743 pairs. Store path (`services.resolve.report`): 33/0/4/40 before and after,
  output byte-identical. Only the resolver's informational "ACTIVE ISO records" count moves (2,401 → 2,864).
- Matching: `data/match_rules.yaml` excludes `built`, so 469 ERCOT rows become eligible and 6 leave.

**Not changed here.** The public page prints `status_raw` as "source status: \"Completed\"" next to a
contracted state; that text is gridstatus's label and reads as completion. The rendering belongs to the
frontend lane. `data/vocabulary/lifecycle_states.yaml`'s `studied` note names ERCOT's energisation approval as
its refine column; IA signed and synchronisation now also apply.

## 4. Entity resolution (`pipeline/resolve.py`)

Key order follows `docs/02` §5. Every pair written to `matches.parquet` carries `pass`, `score`, per-component
scores, `evidence`, a one-line `rationale`, `accepted`, and `cluster_id`; every accepted pair is therefore
reversible by pair.

| Pass | Key | Pairs found | Cross-source? |
|---|---|---|---|
| D1 | EIA plant id + generator id equal | **0** | no ISO feed carries an EIA id; D1 fires only once a second EIA-keyed source exists |
| D2 | ISO + queue id equal, guarded by same state, same county, name similarity ≥ 60 | 87 (96 groups/pairs rejected as id reuse) | within-source only; unguarded it produced 255 pairs, many wrong (§7.1) |
| D3 | queue id cited inside another source's project name | **2** (of 6 citations) | ISO-NE cites NYISO *cluster* ids (`C24-308`) that NYISO's public file mostly does not use as its queue id |
| B1 | block: state + technology + capacity ±10 % | | |
| B2 | block: state + county + capacity ±10 % | | |
| B3 | block: state + first ≥4-char name token, **no capacity constraint** | | added after measurement (§7.2) |
| F | fuzzy score, threshold 75 | 59,527 candidates → 725 accepted | |

Blocking yields 59,656 candidate pairs in 6 s. A **plant-level rollup** view of EIA (147 synthetic records for
the 326 plants with >1 planned generator of one technology) is added before blocking, because ISO rows are per
interconnection request and EIA rows are per generator; it contributes 18 accepted pairs.

**Score** = weighted mean over the components both sides carry: name 0.40, sponsor 0.25, county 0.20,
capacity 0.10, COD 0.05. `evidence` = the sum of weights actually available; a pair is accept-eligible only
when evidence ≥ 0.50, which is what stops SPP (no name, no sponsor; max evidence 0.35) being merged on
geography alone: 19,881 candidates involve SPP and none can be accepted. Name similarity is the mean of
rapidfuzz `token_set_ratio` and `token_sort_ratio` (set alone scores subsets as 100: "SANDOW" vs "SANDOW LAKES").

Three explainable rules were added after inspecting the labelled errors (§6 states the in-sample caveat):

- **P, phase conflict**: both names carry phase/unit numbers and the sets differ ("Lazy U Solar 1" / "2") →
  name score halved. Fired on 1,122 candidate pairs.
- **S, SPV vs developer**: name ≥ 90 and county equal but sponsor < 50 → sponsor dropped from the score
  (EIA lists "Freestone Solar LLC", ERCOT lists the developer, or vice versa). Fired on 207.
- **V, stale withdrawn**: one side withdrawn and CODs > 5 years apart → score × 0.8. Fired on 3,206.

Clusters are connected components over accepted pairs (union–find): **337 clusters covering 869 records,
292 spanning more than one source**, max size 9 (the Darden complex: one 1,150 MW CAISO position + 8 EIA
generators across Darden I–IV; a true cluster, at the wrong granularity for a "one record" view, see §7.4).

## 5. Measured results

| Measurement | Value |
|---|---|
| ISO queue records in a non-terminal lifecycle state ("ACTIVE") | 2,401 |
| … linked to ≥1 EIA-860M planned unit at threshold 75 | **174 (7.2 %)** |
| … by source: CAISO 46/265 (17.4 %), NYISO 39/210 (18.6 %), ISO-NE 10/66 (15.2 %), ERCOT 79/1,198 (6.6 %), SPP **0/662 (0 %)** | |
| Raw-status "Active" rows (1,936) linked | 174 (9.0 %) |
| EIA-860M planned generators linked to any ISO record | 374 / 2,343 (16.0 %) |
| EIA planned generators in the five ISO balancing authorities (ERCO, CISO, SWPP, NYIS, ISNE) linked | **370 / 1,067 (34.7 %)** |
| Accepted pairs total / cross-source | 814 / 727 |
| ISO-to-ISO accepted pairs (same project queued in two ISOs) | 8, all NYISO↔ISO-NE (e.g. "New Scotland BESS" ↔ "New Scotland BESS (NYISO-C24-045)", "Cricket Valley" QP444) |
| Within-source duplicate pairs (D2, ISO-NE units under one id) | 85 ISO-NE, 2 NYISO |
| Ambiguous band (score 60–75, evidence ≥ 0.5), the LLM adjudication candidates | 272 pairs |

Why the headline link rate is low, in measured order: (1) 662 of the 2,401 active ISO records are SPP, which
publishes no name and no sponsor; (2) ERCOT's 1,198 actives face only 532 EIA ERCO rows, and a name-only oracle
finds an EIA name at ≥90 similarity for just 181 of them, of which only 68 also sit within ±10 % capacity;
(3) 77 active records have no usable capacity. The 34.7 % figure on the EIA side is the fairer statement of
what the current keys achieve where both sides publish names.

## 6. Labelled set and precision/recall

`data/eval/labels.csv`: **88 pairs hand-labelled by inspection** of both records (name, sponsor, county,
technology, MW, COD, state), sampled stratified over ten score bands from the candidate set (10 per band down to
6 in the low bands, seed 7). 40 positive, 45 negative, 3 marked `-1` (uncertain: an ISO-NE row named only
"Wind" under a reused id; two withdrawn CAISO phases against EIA units with mismatched numbers) and excluded.

Labelling rule, stated because it is a modelling choice: *label 1 when a user tracking the project would want
one record*. Co-located solar + storage under one project name is one project ("Lupinus Solar 1" ↔ "Lupinus
Solar 1, LLC" storage); separately numbered phases are not ("Solar Star 3" vs "4"); a queued complex is not the
same record as one of its numbered components ("ATLAS COMPLEX" 3,200 MW vs "Atlas VIII").

Two views are reported. *sample* = on the 85 labelled pairs as they are. *weighted* = each pair weighted by
(population pairs in its score band ÷ labelled pairs in that band), which corrects for the stratified sampling;
band populations are 34,014 / 3,561 / 692 / 160 / 88 / 40 / 77 / 145 / 224 / 352 from low to high.

Before rules P/S/V (name scorer = mean, threshold 72): sample precision **0.783**, recall **0.900** (tp 36, fp 10,
fn 4); weighted 0.786 / 0.914. At 75: 0.769 / 0.750.

After rules P/S/V, full sweep (`resolve.py --sweep`):

```
 threshold  tp  fp  fn  tn  sample_precision  sample_recall  sample_f1  weighted_precision  weighted_recall  n
        65  40   8   0  37             0.833          1.000      0.909               0.818            1.000 85
        70  39   4   1  41             0.907          0.975      0.940               0.895            0.972 85
        75  38   3   2  42             0.927          0.950      0.938               0.915            0.946 85
        80  37   2   3  43             0.949          0.925      0.937               0.936            0.921 85
        85  33   1   7  44             0.971          0.825      0.892               0.957            0.845 85
        90  21   1  19  44             0.955          0.525      0.677               0.943            0.625 85
```

**Chosen threshold 75: precision 0.927, recall 0.950 (sample); 0.915 / 0.946 (weighted); n = 85.**
Caveat that must travel with these numbers: rules P/S/V were designed by reading the errors on this same set,
so the post-rule figures are in-sample and optimistic. The pre-rule 0.78 / 0.90 is the honest lower bound; the
truth on a fresh sample is expected between the two. Name-scorer ablation at 72, pre-rules: token_set
0.755 / 0.925, token_sort 0.780 / 0.800, mean 0.783 / 0.900.

Remaining errors at 75 (all five):

| Kind | Pair | Why |
|---|---|---|
| FP | `isone:84` ↔ `isone:84#5` (D2) | "Phase 3 – Nantucket Shouls" vs "Phase I – Nantucket Sound": same reused id, same county, misspelt name passes the ≥60 guard |
| FP | `nyiso:0477` ↔ `eia860m:70242-216` | "Riverhead Solar" (20 MW) vs "Riverhead – CVE" (4 MW storage): town name dominates, capacity 0 not weighted enough |
| FP | `nyiso:0287` ↔ `eia860m:69855-POMFR` | "Pomfret" wind (withdrawn) vs "Pomfret PV" 3 MW: rule S dropped the sponsor that would have separated them |
| FN | `eia860m:67782-GS22B` ↔ `ercot:27INR0125` (73.0) | "RedSun PV and BESS" vs "RedSun BESS": sponsor 25 (Gransolar SPV vs project LLC), name 82 |
| FN | `eia860m:69605-TENN` ↔ `ercot:21INR0253` (68.6) | "Tennyson Solar" vs "Ulysses Solar": renamed project, sponsor 100, county + MW match, name 27 |

## 7. Observed failure cases

1. **Queue-id reuse (ISO-NE).** Id 272 covers six rows: "Wind" in Franklin, Aroostook ×3, Somerset, and
   "Oakfield II Wind – Keene Road". Unguarded D2 hard-merged them. Guarding on state+county+name still lets
   through the Nantucket phases. Queue id + ISO is therefore *not* a deterministic key for ISO-NE; treat it as
   a blocking key there.
2. **Capacity ±10 % as a block is the main recall limiter.** Against a name-only oracle for ERCOT actives, 110
   name matches share the state but only 68 fall inside ±10 %: ISO queue MW is the interconnection request,
   EIA is nameplate per generator ("Broadleaf Solar" 101 vs 200 MW; "Sandow Solar" 287 vs 645 MW;
   "Naduah Solar" 77.6 vs 102.3 MW). B3 (state + name token, no capacity) recovers these: 363 of the 725 fuzzy
   acceptances come only from B3.
3. **Sponsor naming is systematically inconsistent across sources.** Same project, "Stoneridge Solar, LLC" on
   one side and "RWE Clean Energy" on the other; "Serrano Storage / Maverick ESS LLC" vs "Serrano BESS / RIC
   Development". A sponsor field can veto a correct match (RedSun) or, when ignored, admit a wrong one
   (Pomfret). Organisation resolution (`organization.aliases` in `docs/02` §5) is a prerequisite, not a
   by-product, of good proposal resolution.
4. **Granularity mismatch.** One CAISO queue position ("DARDEN", 1,150 MW; "SANBORN HYBRID 3", 1,400 MW;
   "ATLAS COMPLEX", 3,200 MW) maps to 2–8 EIA generators. Clusters are right; the "one record per project"
   promise needs an explicit parent/child relation, which the schema does not yet have.
5. **Numbered phases and renamed projects.** Phases share every attribute but the number ("Lazy U Solar 1/2",
   "KCE NY 27/31"); renames share every attribute but the name ("Tennyson" → "Ulysses", sponsor "BNB Tennyson
   Solar LLC" on both). Rule P handles the first; only sponsor + county + MW can catch the second.
6. **Encoded cross-keys exist and are worth harvesting.** EIA generator ids `Q584`, `Q581`, `Q#865`, `Q#913`
   are NYISO queue positions; ISO-NE names quote NYISO cluster ids; "Cricket Valley Project (QP444 in NYISO
   Queue)". D3 currently resolves 2; a per-source id-pattern table would turn several dozen fuzzy matches into
   deterministic ones.
7. **Stale withdrawn requests re-proposed years later** ("SIENNA" withdrawn 2018 at 66.7 MW vs "Sienna Solar
   Farm" 2028, 200 MW). Rule V demotes but does not settle them; whether a re-filing is the "same project" is a
   product decision (§11).
8. **Sources that cannot be matched at all today.** SPP: 3,074 rows, zero names, zero sponsors → 0 links; only
   county + MW + COD, which is below the evidence floor by design. NYISO: 1,350 withdrawn rows carry nothing but
   State and Status.

## 8. Change detection (`pipeline/diff.py`)

Deterministic, model-free (`docs/20` §3.3), keyed on `record_id`, one event row per changed field:
`new`, `removed`, `withdrawn` (lifecycle moved to withdrawn/cancelled), `status_change` (any other move),
`capacity_change` (> 0.5 MW **and** > 1 %, or null↔value), `cod_change`. A record can emit several events.
Removal is an event, never a delete: for ERCOT (no withdrawn rows in the file) and EIA-860M Planned (units leave
the sheet on COD) it is the *only* way those transitions are observable.

Event identity in the store (`services/ingest/loader.py`, changed 2026-09-18, audit §3.1):
`event.idempotency_key = source:record_id:event_type:field:sha1(before)[:12]:sha1(after)[:12]:observed_at`.
The key used to be `source:record_id:event_type:sha1(after)`, so a status that returned to an earlier value
(A→B→A) and a second removal after a re-sighting were dropped as duplicates; and `removed` events were skipped
altogether because their record is no longer in the frame. Re-loading one snapshot is still a no-op (same
observation time, same before/after); the subject of a `removed` event is found through the stored link for its
`record_id`, and a record seen again clears that link's `gone_at`. Tests: `services/ingest/test_loader_events.py`.

Demonstration: `diff.py --demo` perturbs today's normalised snapshot with disjoint, seeded edits
(seed 0) and diffs it against the original.

```
perturbed copy: 14,388 -> 14,408 rows
330 events -> data/eval/events.parquet
                 observed  expected    ok
new                    50        50  True
status_change          80        80  True
capacity_change        60        60  True
cod_change             70        70  True
withdrawn              40        40  True
removed                30        30  True
```

The first run recovered 56/60 capacity changes: a +20 % edit on records ≤ 2.5 MW sits under the 0.5 MW floor.
The perturbation now applies max(+20 %, +1 MW); the floor itself is kept, it is the point of the rule.

### 8.1 A status-map correction is a reclassification, not a change event (2026-09-30, lane FX1)

The diff compares the new frame with the stored one, and the stored one was harmonised under the map in force
when it was written. A corrected map therefore looks like a real-world change on every row it moves. Replaying
the stored 2026-09-13 ERCOT report through the runner, against the stored normalised frame, with the §3.1 rules:
**475 `status_change` events** (465 built→contracted, 4 built→under_construction, 4 studied→built,
2 under_construction→built). Loaded into an empty store after the stored frame, they become 475 `event` rows,
and `status_change` events feed `proposal.status_changed` social drafts, CRM signals and alert matching. No mechanism for silent reclassification existed: the loader writes every
field it is given without an event (`_update_existing_entity`), and all events come from the runner's diff.

**Mechanism added.** `Connector.restate_status(df)` returns each stored row's `lifecycle_state`/`status_rule`
recomputed from its own `raw` payload under the current map (default `None`: cannot restate, diff as stored).
The ERCOT connector implements it through `pipeline/connectors/iso_queue.py::restate_iso_status`, which runs the
same `iso_status_contexts` → `harmonise_status` path as normalisation. The runner restates the previous frame
right after key alignment, in `run` and `release_held`, and diffs against that. The same replay then emits
**0 events**; the run record carries `rows_reclassified: 475` and
`reclassified.transitions`, and the DQ block gains an `info` check `status_reclassified` (persisted on
`source_run.dq`, visible on the admin runs screen; the DQ status stays `pass`). The loader updates the 475
proposals' `lifecycle_state` as an ordinary field write, restamping `field_provenance.lifecycle_state`.

A real change in the same run is still published, with its before-state in the current vocabulary:
`tests/test_connector_e2e_ercot.py` energises one IA-only row while correcting the map and gets exactly one
event, contracted → under_construction, where the unrestated diff gives four (three spurious, and the real one
reported as built → under_construction).

**Limits.** (i) The runner short-circuits on an unchanged snapshot SHA, so a map change reaches the store with
the next ERCOT report that differs (monthly), not on the next scheduled tick; there is no force-renormalise
flag. *Superseded 2026-10-07 (§8.4):* the status map is part of the parser version, the short-circuit also
compares that version, and `run --reparse` re-normalises the stored snapshot without fetching. (ii) Only connectors that implement `restate_status` are covered; the other gridstatus queues (CAISO,
NYISO) can opt in with the same one-line override. (iii) A stored frame without a `raw` column is diffed as
stored.

### 8.2 A capacity-rule correction is restated too; an open notice past its deadline closes (2026-10-06)

**Capacity.** The NESO connector took `Cumulative Total Capacity (MW)` as every row's capacity, so a staged
project counted its earlier stages again (audit 2026-09-30, data scientist F3). It now takes the row's own MW,
`MW Connected` + `MW Increase / Decrease` (docs/25 §1.3 has the before/after). That correction would reach the
change feed as one `capacity_change` per moved row (136 on the 2026-09-13 register) on NESO's next run. The §8.1
mechanism is extended: `Connector.restate_capacity(df)` recomputes a stored frame's `capacity_mw` from `raw`
(NESO implements it; default `None`), the runner restates the previous frame before the diff, counts the moved
rows as `reclassified.capacity_rows` and records an `info` check `capacity_restated`. A row the restatement cannot
read keeps its stored value. `pipeline/connectors/gb_neso_tec_register/test_connector.py` replays a store written
under the cumulative rule: the 2 moved fixture rows emit nothing, and a real change in the same run is the one
`capacity_change`. The loader writes the corrected capacity as an ordinary field update.

**Opportunity deadlines.** `open` means the deadline has not passed (`data/vocabulary/lifecycle_states.yaml`;
docs/21 §7.2: "open --> closed: deadline passed", `closed` = "deadline passed, outcome unknown"). Each connector
applies that at fetch time, but an incremental source (TED, Find a Tender, World Bank) carries earlier rows
forward unchanged, so a notice never re-fetched stayed `open` for good (audit 2026-09-30, data engineer F4,
market M-7). The runner now applies `pipeline.connectors.opportunity.close_past_deadline` to the whole frame at
the run's `retrieved_at`, with `status_rule = opportunity.deadline_passed`. This one *is* news: the diff
publishes it as `status_change` open → closed, and the run records an `info` check `deadline_closed`. Measured on
a copy of the 2026-09-30 dev store: 90 `open` notices had a past `due_at` (TED 59, World Bank 16, grants.gov 15);
re-running each stored frame through the rule at 2026-09-30 and loading it leaves 0 (open 405 → 315). The stored
status still lags a deadline by at most one run of the notice's source; a read-time rule would close that gap
but needs the list filter and the alert matcher changed together (open item).

### 8.3 What is news inside one lifecycle state (2026-10-07, audit 2026-09-30 data engineer F7)

The diff compared three fields. Capacity and the target date are compared whatever the state does,
so a capacity or date change inside one state was already an event. The source's own status text
was not: on the stored EIA-860M frames (2026-09-13 → 2026-09-27, 2,283 common rows) **64** rows moved
inside `under_construction`, 23 of them `(V) Under construction, more than 50 percent complete` →
`(TS) Construction complete, but not yet in commercial operation`, the most useful late-stage signal
the register carries, and none reached the feed.

| Field | Inside one state | Decision |
|---|---|---|
| `lifecycle_state` | n/a | `status_change` / `withdrawn`, as before |
| `status_raw` | **news** | new `status_raw_change` (field `status_raw`), only when the state did not move (a state change is one event, not two) and the text differs after whitespace and case are normalised; loaded as `field_changed` with `changed_keys = [status_raw]`, so alerts and `/v1/events?changed_key=status_raw` see it |
| `capacity_mw` | news (unchanged rule) | `capacity_change` above 0.5 MW and 1 % |
| `proposed_cod` / `due_at` | news (unchanged rule) | `cod_change` |
| sponsor, name, county, point of connection | not news | written by the loader as field updates. Measured on the same two frames: 0 changes in `sponsor_name`, `name_canonical` or `county`; the noise these fields do carry is normalisation (below), which is not a source change |

Social drafting stays default-deny on `field_changed` (docs/32 §3.1), so a raw-status move is in the
feed, alerts and webhooks, not in posts. The restatement rules hold: a status-map correction never
changes `status_raw` (§8.1), and a parser change that rewrites the text is restated before the diff
(§8.4). `pipeline/diff.py`; tests `tests/test_parser_restatement.py`
(`test_the_source_status_text_is_news_inside_one_state`,
`test_construction_complete_inside_under_construction_is_published`).

### 8.4 A parser change is a restatement, and reaches unchanged sources (2026-10-07, audit F10)

Every connector recorded `parser_version = 1.0.0` and the unchanged short-circuit compared only the
SHA-256, so a parser fix reached a source only when its bytes next changed (a year for annual
sources), and lineage could not say which code produced a row: between the two EIA-860M runs the
normaliser rewrote `sponsor_norm` on 1,572 rows and `iso` on 1,039 (1,358 and 854 of them with a
byte-identical `raw`) under the same `@1.0.0`.

1. *Version.* The run records `{source_id}@{declared}+{digest}`: the connector's declared
   `parser_version` plus eight hex characters over the parser's code (every connector module in its
   MRO, its status map, and the shared parsing modules `base`, `canonical`, `dedupe`, `iso_queue`,
   `opportunity`, `pipeline/normalize.py`, `pipeline/status_map.yaml`). Python is digested as its AST
   without docstrings and YAML as parsed content, so a comment or docstring edit is not a version.
2. *Restatement.* When the version differs from the last promoted run's, the runner re-derives that
   run's output under the current code before the diff: a full-register source from its stored
   snapshot (parse + normalise), an incremental source from each stored row's own `raw` (normalise,
   grouped by fetch), opportunities re-closed at that run's time. The run records `parser_restated`
   (`from`, `to`, and `events_suppressed` by type) and an `info` DQ check; if the old output cannot
   be re-derived the check is `warn` and the diff compares as stored. After a successful
   restatement the §8.1/§8.2 map and capacity restatements are skipped (the re-derivation already
   used the current map and rule).
3. *Reaching unchanged sources.* The short-circuit is `unchanged` only when the SHA-256 *and* the
   parser version match. `python -m pipeline.connectors run <id> --reparse` runs the latest stored
   snapshot through the current parser without fetching; the run points at the original object and
   keeps its `retrieved_at`.

Consequence on deploy: every source's first run after this change restates its previous output once
(versions move from `@1.0.0` to `@1.0.0+digest`), with no events from the restatement. Limits: an
incremental source's restatement re-runs `normalize`, not `parse` (its stored rows are the parsed
form); a change to `fetch` is not a parser change. Tests: `tests/test_parser_restatement.py`.

## 9. What an LLM adjudication step would add, and what it costs

Where it helps, from the errors above: the 272 candidates in the 60–75 band, the D2 pairs on reused ids, and
the granularity/re-filing questions, where the decision needs reading ("Vast Sands Power II (TEF-Due
Diligence)" is a unit of "Vast Sands Power"; "Ulysses Solar" is the renamed "Tennyson Solar"; a 3,200 MW
complex is the parent of "Atlas VIII"). It would return a typed verdict (`same | phase_of | component_of |
refiling_of | different`) plus rationale, which is richer than the binary label and directly fills the
parent/child gap in §7.4. It would not help SPP (nothing to read) and should not replace the deterministic and
blocking passes (`docs/20` §3.5: only ambiguous candidates go to the model gateway).

Cost per pair, **estimated from list prices, not measured**: a pair prompt is two normalised records plus a
stable instruction block, roughly 600 input tokens with the instruction block cached, and a ~150-token JSON
verdict. At the current Opus-tier list price ($5 / $25 per million input / output tokens) that is about
**$0.007 per pair** synchronous, about $0.0035 via the batch endpoint (50 % off); a Sonnet-tier model
($2 / $10) is about $0.003. Adjudicating today's 272-pair ambiguous band costs under $2; the whole 814-pair
accepted set under $6. At a weekly cadence over five ISOs the volume is the *new* ambiguous pairs, tens per
week, so the gateway's budget line (`docs/20` §6) is set by extraction, not by resolution.

## 10. Path to production quality

- **Labelled set.** 85 usable labels give ±0.06–0.10 on precision at 95 % confidence; a held-out set is needed
  before any post-rule number is quoted externally. Target **600 labelled pairs**: 300 stratified by score band
  as now, 200 uniformly from all accepted pairs (precision estimate), 100 from the name-only oracle's
  unaccepted matches (recall estimate), split 400 train / 200 test, labelled by two people with disagreement
  adjudicated. Every hand override in the admin panel becomes a label (`docs/20` §3.5).
- **Metric targets** on the held-out set: precision ≥ 0.95 on accepted pairs (a wrong merge is visible to
  customers; a miss is not), recall ≥ 0.85 on the oracle-recoverable set, and a separate D2 precision ≥ 0.99
  per source, else that source's queue id is demoted to a blocking key.
- **Prerequisites that move recall more than any scoring change**: an organisation alias table (§7.3); a
  per-source id-pattern harvester for encoded cross-keys (§7.6); EIA plant→generator parent links and an
  explicit `component_of` relation (§7.4); PJM and MISO once licensed (more queue rows, not more keys); and a
  source that carries EIA ids (state siting boards, EIA-860 annual), without which D1 stays at zero.
- **Monitoring**, per weekly run: candidate count and acceptance rate per source pair (a 2× move flags a
  parser change upstream); share of pairs in the ambiguous band; D2 rejection count; `status_rule = *.unmapped`
  and `*.blank` counts (a new raw status value must not silently become `unknown`); `status_conflict` count;
  diff event counts by type per source against a 4-week median (ERCOT `removed` is the withdrawal signal and
  must never be zero for long); and a 20-pair weekly spot-check of new acceptances, logged as labels.

## 11. Assumptions and decisions recorded here

- A-22-1: "one record per real-world project" treats co-located hybrid components under one name as one
  project and numbered phases as separate projects. Owner should confirm; the labels encode this choice.
- A-22-2: a request withdrawn and re-filed years later is a *new* proposal linked by a `refiling_of` relation,
  not the same record. Not yet implemented; rule V only demotes.
- A-22-3: `under_construction` is added to the `docs/02` §1 vocabulary; (TS) "construction complete, not yet
  in commercial operation" maps there, not to `built`.
- A-22-4: for ISO-NE, queue id + ISO is a blocking key, not a deterministic key.
- Licence note: nothing in `data/eval/` is published; SPP/NYISO/ISO-NE terms remain "unknown" in
  `data/sources.yaml` and must be recorded before any derived row from them is shown.

## 12. Test run (verbatim, `.venv/bin/python -m pytest tests/ -q`)

```
...............................................................          [100%]
63 passed in 0.58s
```

`tests/test_status_map.py` (54 cases): every mapped value is canonical; all six sources present; per-source
harmonisation incl. refine precedence; SPP original-status override and blank fallback; unmapped/blank/unknown
source; technology classifier incl. the ERCOT exclusion strings; state/county/org/name normalisers.
`tests/test_diff.py` (9 cases): identical snapshots emit nothing; new/removed; status_change vs withdrawn incl.
terminal→terminal; capacity noise floor and null transitions; COD null transitions; multi-event records;
determinism; perturbation round-trip counts exact; refusal when too few live rows. Two of these tests failed
on first run and exposed real bugs (§2 technology rule; SPP fallback map), both fixed in the mapping/code, not
in the tests.

## 13. Reversible merges in the store (`services/resolve/`, 2026-09-13)

**Status:** implemented and measured on the 2026-09-12 pull, code in `services/resolve/`, tests in
`tests/test_resolve_store*.py`, verbatim run output in `services/resolve/README.md` (not
duplicated here). This section records the rules and the measured counts; §10's prerequisites
(organisation aliases, the plant/generator parent link, PJM/MISO) are unchanged by it.

### 13.1 Merge rules

**Canonical record**, in order: (1) carries an EIA id; else (2) most recently retrieved; else (3)
lowest `source.id` string; else (4) lowest `proposal_id` (uuid7, so also earliest-created).
`services/resolve/merge.choose_canonical`.

**Merge** writes one `merged` event on the canonical row (docs/21 §6.3): `before.absorbed.entity`
is the absorbed row's **full** serialized column set (not just the changed keys) captured before
any mutation, which is what makes `unmerge` exact and total per invariant M1 without reading any
other row. The absorbed row's `proposal_source` rows are re-pointed and their ids recorded in the
same payload so `unmerge` can move them back. Nothing is deleted; the absorbed row's
`merged_into_id` is set and `publish_state` moves to `unpublished`. Idempotent per pair.

**Unmerge** restores every field of the absorbed row from that one event's `before` payload,
moves the `proposal_source` rows back, and restores the canonical's changed fields
(`source_count`, `resolution_confidence`, `last_changed`). Proven with an exact
`serialize_row(...) == serialize_row(...)` round trip in
`tests/test_resolve_store_unmerge.py::test_unmerge_proposal_round_trip`, plus a two-merges test
showing an unmerge of one absorbed record does not disturb a sibling merge into the same
canonical.

### 13.2 The confidence gate, and the id-reuse guard's wording

Merge requires both: minimum pairwise score >= 75 (this section's chosen threshold, §6) among the
edges actually available between the cluster's **loaded** members (a cluster connected only
through an excluded node — an EIA plant-rollup synthetic record or a gated SPP/ISO-NE record — is
refused, not trusted); and no id-reuse conflict.

This task's brief named the guard "two records from the same source with different queue ids ...
the ISO-NE id-reuse failure case." Measured against today's 281 loadable multi-member clusters,
that literal reading refuses 73 of them (26%) — almost all the legitimate multi-queue-position
complexes §7.4 already documents (one EIA plant bridging several distinct, real ERCOT/CAISO
interconnection requests for co-located units; e.g. `caiso:1479`/`caiso:1480`, "KEY STORAGE 1"/"2",
two real, different projects that should each merge with the EIA record but never with each
other). The bug this task actually names, measured in §5 ("within-source duplicate pairs": 85
ISO-NE, 2 NYISO) and §7.1 (`isone:84`/`isone:84#5`, "Phase 3 – Nantucket Shoals" vs "Phase I –
Nantucket Sound", the same reused queue id covering two different projects), is the opposite
shape: the **same** queue id, from the **same** source, shared by two records that are not the
same project. `services/resolve/merge.id_reuse_conflict` implements that reading: it fires on
exactly the 2 NYISO clusters carrying the true signature and 0 of the 71 legitimate
multi-queue-position clusters. This is a deliberate, measured departure from the pair's literal
wording, argued in full in `merge.py`'s module docstring and `services/resolve/README.md`, not an
unexamined misreading.

Anything below the gate is filed as a `resolution_decision` row per pairwise edge
(`status="proposed"`) for human review, never auto-applied. `docs/21` §3.11 `match` is
proposal↔opportunity only (`opportunity_id NOT NULL`) and cannot hold a proposal-proposal
candidate without misusing the column; `docs/21` §6.4 already names the right table
(`resolution_decision`) and no sprint had built it, so `services/resolve/models.py` adds a minimal
version of it rather than repurpose `match` or extend `services/db/models.py`.

### 13.3 Organisation resolution

Deterministic-key match on `pipeline.normalize.norm_org` (the same corp-suffix-stripping key the
sponsor scoring component already uses), confidence 1.0, the same convention the `D`-prefixed
deterministic passes use. A fuzzy pass across the ~3,000 sponsor organisations in this dataset
(~4.6M candidate pairs) is deliberately not run this task: per §10, a fuzzy resolver number needs
its own labelled sample before it is quoted, and none exists yet for organisations.

### 13.4 Measured counts (2026-09-12 pull, run 2026-09-13)

Loaded through the existing, unmodified `services.ingest.loader`, gated exactly as production
gates it — SPP and ISO-NE (`restricted` in `data/sources.yaml`) are proven refused, not skipped.

| Measurement | Value |
|---|---|
| Proposals in store before resolution (caiso+ercot+nyiso+eia860m only; SPP/ISO-NE gated) | 8,212 |
| Resolver clusters with >= 2 store-loaded members | 278 |
| Clusters merged | 275 (absorbing 442 records) |
| Clusters refused by the gate | 3 (all: no direct edge between loaded members — bridged only via an excluded rollup node) |
| `resolution_decision` rows created | 0 (the 3 refused clusters had no scored edge to file) |
| Proposals after resolution (surviving/canonical rows) | 7,770 |
| … of which with >= 2 sources | 273 |
| Organizations before / after | 3,034 / 2,784 (250 absorbed across 212 normalised-name groups) |
| Store-path precision / recall, 85 hand labels (77 usable; 8 touch a gated source) | 0.946 / 0.946 |

For comparison, `pipeline/resolve.py`'s own accepted-pairs check on the full 85 labels (§6) gives
0.927/0.950 sample. The store-path number, on the subset it can answer, is not worse — the gate
and its application through the store do not regress the measured precision/recall.

**On 278 vs. 281**: a direct count against `clusters.parquet` before any ingestion finds 281
loadable multi-member clusters; after ingestion the store holds 278, because NYISO's
`(source_id, source_record_id)` uniqueness constraint collapses its two true id-reuse duplicate
pairs (`nyiso` queue ids `0031`, `0127A`) into one proposal each at ingestion — before this
section's own gate is ever consulted. Both numbers are real; see §13.5.

### 13.5 Observed limitation, on the data-engineer backlog

`services/ingest/loader.py`'s active-uniqueness constraint on `(source.id, source_record_id)`
means a source's legitimate id reuse — the exact case §13.2's guard is about — silently updates
the first record in place on the second occurrence, rather than creating a second row for the
resolver-level gate to ever see. This is a *worse* failure mode than a filed
`resolution_decision`: no event, no confidence score, no way to unmerge. It affects nothing
reported in §13.4 (both known NYISO occurrences are close enough that the collapse is not
visibly wrong here), but is a latent defect for a future source with materially different id-reuse
data. A second, unrelated loader limitation (organisation slugs not unique across punctuation-only
spelling variants) was found and worked around in `services/resolve/report.py` without editing the
loader; both are recorded in full, with the exact failing case, in `services/resolve/README.md`.

### 13.6 Assumption recorded

- A-22-5: the id-reuse guard refuses a cluster only when one source's queue id is shared, inside
  that cluster, by more than one distinct store record — not merely when a cluster contains two
  different queue ids from the same source. This departs from this task's literal wording; the
  measured justification is §13.2 above. Owner should confirm; `services/resolve/merge.py`'s
  module docstring and `services/resolve/README.md` carry the same argument for any future review.

### 13.7 Provenance on resolver events (as built 2026-10-07)

**The defect.** `CLAUDE.md` requires `source_id`, `source_url`, `retrieved_at`, `licence` on every stored record;
docs/21 §3.10 lets an `event` leave them null "only for `actor_type = user` events". The resolver wrote its
machine events with none. Measured on the full committed eval pull (`data/eval/normalized.parquet`, the
`services/resolve/report.py` chain in an in-memory store, then 25 proposal and 25 organisation unmerges and the
reviewed ICIS-Air suppression list applied to rows carrying its 5 registry ids):

| Event (subject / type / actor) | Rows | Before: quartet | After: quartet |
|---|---|---|---|
| proposal / `merged` / model | 407 | null 407 | complete 407 |
| proposal / `unmerged` / user | 25 | null 25 | complete 25 |
| organization / `merged` / pipeline | 151 | null 12, **`licence_id` only** 139 | complete 151 |
| organization / `unmerged` / user | 25 | null 25 | complete 25 |
| proposal / `unpublished` (suppression) / pipeline | 5 | null 5 | complete 5 |

The 139 licence-only rows were a second bug: in `merge_organization` the spelling alias's provenance tuple was
unpacked into a variable named `licence_id`, rebinding the function's own argument, so the event got the alias's
licence and no source. Field survivorship (§23) writes no events, and `resolution_decision` rows are a review
queue, not records (no quartet columns; each names two proposals whose links carry theirs), so neither changes.

**How these events are served, before and after.** None reaches a non-admin reader: the resolver never sets
`published_at`/`public_at`, which `event_visibility_filter`'s timing clause requires on `/v1/events`, a record's
events route, feeds/RSS, saved-search alerts and webhooks; the same filter's `EXISTS` on `licence_id` and
`source_id` also dropped every null-quartet event, so the gap was filtered, fail closed, rather than rendered. The
social worker maps only `created`/`status_change`/`withdrawn`/`cancelled` and checks the event's licence.
Organisation-subject events are invisible on every non-admin surface by design. The admin timeline
(`serialize_event`) rendered `provenance: null` for all of them, the licence-only ones included (it needs both a
source and a licence); it now renders the cited record's quartet and credit. The public `merge_history` reads the
moved links' own provenance and is unchanged. Stamping the quartet changes no visibility: no timing column is set.

**The rule** (`services/resolve/provenance.py`). docs/20 and docs/21 settle that a derived artefact is gated by
the most restrictive source it rests on (docs/21 §8 checklist item 2, "no `event` whose `licence_id` resolves to
such a licence"; the mixed-provenance paragraph) and that a reversal restores provenance (§6.2), but not which
quartet a merge of several sources carries. Chosen, consistent with survivorship's per-field provenance (§23.1)
and the spelling alias's "the record the spelling was read from" (§20.2):

1. *Evidence*: proposal merge and unmerge, the active links of the survivor and of the absorbed record (inactive
   links only when neither has an active one); organisation merge and unmerge, every stored row naming either
   organisation: links of the proposals it sponsors (a sponsored proposal merged away is followed through its own
   merge event's `proposal_source_ids`; without that step 4 of the 151 eval-store merges have no evidence on either
   side), its `asset_owner` edges and its `organization_alias` rows; suppression, the suppressed
   record's active links.
2. *One member's whole quartet, never a mix*, so the event's URL, retrieval time and licence describe one stored
   record and the credit line is one a source actually states. Guard: every resolver event's `licence_id` equals
   its source's (0 of 613 differ).
3. *The most restrictive licence wins*: reuse class (`open` < `attribution` < `noncommercial` < `restricted` <
   `unknown`), then an uncleared gate, then each `allows_*` permission withheld. A merge with a `restricted` or
   `unknown` member carries that member's licence and is therefore off every non-admin surface, so a restricted
   `source_url` sits only on an event no public reader is served. Among publishable classes a raw-withheld licence
   (CAISO, NYISO) outranks an open one.
4. *Ties*: the triggering evidence (the absorbed side; the listed registry id), then the most recent retrieval,
   then source id and record key.
5. An unmerge copies the quartet of the merge it reverses; a merge stored before this rule has none, and its
   unmerge derives one by rules 1-4. A caller's explicit quartet (curated merges, §20.9) is used whole and must
   have all four values.

Citations on the eval store after the change: proposal merges ERCOT 231, CAISO 80, NYISO 54, EIA-860M 42;
organisation merges NYISO 89, ERCOT 56, EIA-860M 6; suppressions ICIS-Air 5. No `restricted` licence is cited
because the loader refuses restricted sources at ingest; the restricted path is exercised by a licence
reclassified after load (`services/resolve/test_provenance.py`).

**Refusals.** A proposal merge or unmerge with no stored link on either side raises (every loader and the admin
intake write a link, so this is a store defect, not a state to attribute). An organisation can exist with nothing
naming it (a parent known only through children's `parent_source_id`, which records no URL or retrieval time); its
merge is written without a quartet and logged, never given an invented one. Measured: 0 such events of 151 on the
eval store; every loader that creates an organisation also writes an alias with a full quartet.

**Tests.** `services/resolve/test_provenance.py` (10; all fail on `52fba5d`): each event type, the
pre-rule-merge unmerge, a merged-away sponsor, a restricted absorbed and a restricted survivor link, a
raw-withheld licence, and the explicit-quartet contract. `services/resolve/test_provenance_guard.py` (3; the two
quartet guards fail on `52fba5d`): the production chain on the 279 loadable multi-member eval clusters plus their
plants' generators, 10 unmerges of each kind and the real suppression list; asserts every resolver event type
occurs, none lacks a quartet value, and each cites a registered source under that source's own licence (about 11 s).

**Not done here.** Events already stored are not rewritten (`event` is append-only, docs/21 §6.1); their unmerges
derive a quartet. A source unpublished by an admin (`source.publish_state`, mutable) is not considered when
choosing: the licence is the immutable gate (invariant L2) and the visibility filter reads the cited source's
state at serve time; if merge events are ever published, the choice should also prefer a member whose source is
hidden, and `before.absorbed.entity` would need the publish-time field-class gating docs/21 §6.2 describes.
docs/21 §6.3's `link_event_id` on re-pointed links was not set by `merge_proposal`; closed below.

**`link_event_id` on re-pointed links (as built 2026-10-07, later the same day).** A merge now stamps every
`proposal_source` row it moves with its own event id and records each row's previous value under
`before.absorbed.proposal_source_link_event_ids`; an unmerge moves back the rows still stamped with it (or carried
onward by a later merge, followed through that merge's recorded previous value) and restores each value. Rules and
departures are in docs/21 §6.3. The quartet rule above is unchanged: it is chosen before the links move, and an
unmerge still copies its merge's quartet (all 10 `test_provenance.py` tests pass unchanged).

Measured on the same full eval run (`data/eval/normalized.parquet`, 9,563 proposals and links; 407 proposal
merges; 25 unmerges):

| | Before | After |
|---|---|---|
| Re-pointed links with no `link_event_id` | 407 of 407 | 0 of 407 |
| Re-pointed links naming their own merge | 0 | 407 |
| Links carrying any `link_event_id` | 0 of 9,563 | 407 of 9,563 (only re-pointed ones) |
| Unmerged links back on their record, value restored to null | 25 of 25 | 25 of 25 |

The automatic run has no chained merges (0 merges absorb an earlier survivor: `apply_cluster` merges every member
into one canonical), so the chain and out-of-order cases arise from admin merges and later runs; they are covered by
tests. Before the change, unmerging B out of A after C had already been unmerged out of B moved C's links to B.

Tests: `services/resolve/test_link_event_id.py` (6; all fail on `583e9ee`): a merge stamps every moved link and no
other; each hop of C into B into A records its own merge and the previous one; unmerging the later merge moves back
only its links (another merge's link and the survivor's own stay); out-of-order unmerges leave C's links with C; a
merge written without stamps unmerges by its listed ids, alone and under a later stamped merge. The realistic-run
guard (`test_provenance_guard.py`) gains a fourth test: every link a standing merge moved names it, every link an
unmerge returned is back with null (390 standing, 10 returned; fails on `583e9ee`).

Not done: `merge_history` (`services/api/proposal_members.py`) still selects a merge's links from its listed ids
intersected with the record's own links, which gives the same answer; it could read `link_event_id` instead.
Re-merging a pair after an unmerge is a silent no-op that returns the reversed merge event (the idempotency key is
per pair), observed while testing and left as found.

- **A-22-27:** a resolver event's provenance is one evidence row's whole quartet, the most restrictive licence
  first and the triggering record on a tie (rules above). docs/20 and docs/21 settle gating by the most
  restrictive source but not which quartet a multi-source event cites. If the owner prefers the triggering
  record's URL under the strictest member's licence (a mixed quartet), only `provenance.choose` changes; the
  visibility outcome is the same, the credit line would no longer be one any source states.

## 14. FERC filer-name backfill (Sprint 3)

**Status:** measured 2026-09-13, live against FERC eLibrary and the three reachable ISO queues (ERCOT,
CAISO, NYISO — SPP/ISO-NE/PJM/MISO stay gated or unimplemented, `data/sources.yaml`, `CLAUDE.md`), code in
`pipeline/connectors/us_ferc_elibrary/connector.py` (`.window` override), `pipeline/link_dockets.py` (filer
index, per-docket dedupe, `ER` docket-year filter), `pipeline/backfill_ferc.py` (the runner), evidence in
`data/probes/ferc-backfill-2026-09-13.json`. This is the "full-text or filer-name backfill over years, not 30
days" the 2026-09-12 "Docket linkage measured" decision row (`docs/00-PLAN.md`) called for before docket
linkage is counted in M-1.

### 14.1 What ran

```
.venv/bin/python -m pipeline.backfill_ferc --years 3
```

| Source | Rows | Requests | Elapsed |
|---|---|---|---|
| ERCOT GIS report (live) | 1,778 | 2 | 3.3 s |
| CAISO public queue (live) | 2,278 | 2 | 2.3 s |
| NYISO queue (live) | 1,814 | 2 | 1.7 s |
| **ISO total** | **5,870** | **6** | **7.3 s** |
| FERC eLibrary, 2025-09-13→2026-09-13 | 895 | 18 (18 pages) | 126.0 s |
| FERC eLibrary, 2024-09-13→2025-09-13 | 311 | 18 (18 pages) | 35.8 s |
| FERC eLibrary, 2023-09-14→2024-09-13 | 87 | 18 (18 pages) | 127.4 s |
| **FERC total (deduped across windows)** | **1,290** | **54** | **289.2 s** |
| **Grand total** | | **60 requests** | **301.6 s wall time** |

No window was skipped: three years fetched inside the ~20-minute time-box with headroom to spare (301.6 s
used of a 1,200 s budget). `.window` replaced the connector's default 30-day cadence for each of the three
one-year sub-windows in turn; the 18 requests/window figure (2 docket classes × 2 calendar years touched by
a rolling 365-day window × up to 3 pages, plus 3 description terms × 2 pages) is exactly what
`pipeline/connectors/us_ferc_elibrary/connector.py`'s "Window override" docstring predicted — `filterDate`
still does nothing server-side (confirmed again in passing: `totalHits` did not move with the window), so
every one of those 54 requests was a real page fetched, not a narrowed one.

### 14.2 Link rate

`pipeline.link_dockets.run` (both methods active: explicit `name_or_queue_id` and the filer-name method,
`sponsor_fuzzy`, threshold 85) over the 1,290 FERC documents and 5,870 ISO queue records produced **81
links** (20 explicit, 61 filer-name; none by both methods this run).

| | Linked | Active | Rate | 30-day test (2026-09-12) |
|---|---|---|---|---|
| **All active (non-terminal) records** | 21 | 1,673 | **1.26%** | 1 / 1,673 = 0.06% |
| **`contracted` / `under_construction` only** (recalibrated M-1 bar) | 3 | 216 | **1.39%** | not measured |

Per-ISO (active, non-terminal):

| ISO | Linked | Active | Rate |
|---|---|---|---|
| ERCOT | 17 | 1,198 | 1.42% |
| CAISO | 3 | 265 | 1.13% |
| NYISO | 1 | 210 | 0.48% |

`contracted`/`under_construction` per-ISO: CAISO 3/214 (1.40%), ERCOT 0/2 (0%); NYISO carries no active record
in either of those two states today, so it is absent from that breakdown rather than reported as 0/0.

The multi-year, filer-name-primary backfill moved the link rate from 0.06% to 1.26% — a genuine ~21×
increase, and it is the *same* 1,673-record active denominator as the 30-day test (ERCOT/CAISO/NYISO did
not materially change size in the intervening day), so the two rates are directly comparable, not an
artefact of a different queue snapshot.

### 14.3 Precision: 50-link hand review

A random sample of 50 of the 81 links (`pipeline.backfill_ferc.precision_sample`, `random_state=20260913`,
proportion by method preserved: 13 explicit / 37 filer-name, matching the 20/61 population split) was
inspected by hand against the `rationale`, `ferc_title` and `docket_refs` fields recorded for each link in
`data/probes/ferc-backfill-2026-09-13.json` — this is a hand read of the recorded text, not a fresh lookup
against FERC eLibrary or the ISO queue websites, and is recorded here as such a limitation, not as a
certified label set (`docs/22` §10's 600-label protocol is the certified version of this exercise).

| Method | n | Correct | Precision |
|---|---|---|---|
| `name_or_queue_id` (explicit) | 13 | 10 | 76.9% |
| `sponsor_fuzzy` (filer-name) | 37 | 14 | 37.8% |
| **Overall** | **50** | **24** | **48.0%** |

Every verdict and its one-line reasoning is in `data/probes/ferc-backfill-2026-09-13.json`'s
`precision_sample[].verdict` (each row also carries `precision_review` with the table above). The 26
incorrect links are not scattered noise; 21 of them (81% of all errors, 42% of the whole sample) are three
already-diagnosed, narrow patterns:

- **Degenerate two-letter residual ("S S" from "S&S Renewables, LLC")** — 10/50 rows. `_distinctive` strips
  "Renewables"/"LLC" from "S&S Renewables, LLC" down to "S S", which is two single-character tokens; the
  `MIN_SPONSOR_WORDS = 2` gate counts tokens, not token length, so "S S" passes as "distinctive" and then
  `token_set_ratio`s to 100 against an unrelated filer ("Maryland Office of People's Counsel",
  "TransAlta Energy Marketing (U.S.) Inc.") on no real shared content.
- **Numbered shell-company siblings ("Fresh Air Energy II" vs "Fresh Air Energy XXIII")** — 4/50 rows, all
  four different ERCOT storage queue records (`Borderland ESC`, `Lincoln ESC`, `Moffitt ESC`,
  `Caracara Energy Storage Center`) matched to the same one filing from a *different* numbered entity in
  the same shelf-company family. `token_set_ratio` treats "II" and "XXIII" as ordinary tokens with no
  numeric comparison, so two siblings that share every word except the roman numeral score as a strong
  match.
- **Generic marketing-entity residual ("E Marketing" from "Electric E Power Marketing")** — 7/50 rows, all
  against different real companies (TransAlta, Brookfield, TransGrid, Sempra) whose only shared text is the
  word "Marketing" plus a single stray letter.

The remaining 5 incorrect rows are `name_or_queue_id` bugs, both narrower than the sponsor-fuzzy patterns
above: two are **word-boundary-free substring matches** ("Alden Solar" found inside "**W**alden Solar PA
Jefferson LLC"; "Diamond Solar" found inside "Black **Diamond Solar** Power, LLC", an unrelated ComEd-area
project), and one is a **docket-class gap** — `_explicit_matches` has no ER-only restriction the way
`_sponsor_matches` does, so a NYISO record literally named "Greene County" (a placeholder name, not a real
project name) matched an unrelated `CP`-class gas-well filing in Greene County, Pennsylvania. The remaining
one (`Ash Creek Project` vs `Willow Creek Wind Project`, a coincidental shared "Creek Project") is a case of
the same class as the marketing-residual pattern, just too small a sample to name its own bucket (1 row).

The 24 correct links split further: 8 are exact self-references (a project's own tariff filing, or an
executed interconnection agreement naming it directly — the strongest possible evidence), 2 are the same
site's sibling phase named in an "et al." notice, and the remaining 14 are same-real-company sponsor
matches (Consolidated Edison ×9, Orsted ×2, Calpine ×2, Avangrid ×1) where the filing itself is a
company-wide rate schedule or administrative filing, not evidence for the specific queue project — correct
about the sponsor, weaker as project-level evidence.

### 14.4 The honest read

**Recall:** the "over years, not days" hypothesis in the 2026-09-12 decision row is confirmed — a 3-year,
filer-name-primary backfill moved the active link rate from 0.06% to 1.26%, a ~21× increase on the same
denominator, at a modest cost (60 requests, 5 minutes wall time, no time-box breach). The filer-name method
(`sponsor_fuzzy`) is now the majority of links (61 of 81, 75%), which is what "filer-name backfill" as the
next test was supposed to establish.

**Precision:** 48.0% measured on a 50-link hand sample is far below the `docs/22` §10 bar (≥0.95 on accepted
pairs) for a signal counted with confidence, and even the stronger explicit method alone (76.9%) does not
clear it. **Docket linkage does not clear a bar worth counting toward M-1 today**, as tuned. This is not,
however, evidence that the filer-name approach is unsound: 81% of the sampled errors trace to three narrow,
already-diagnosed defects (a token-length gap in the sponsor-residual gate, no numeral-awareness against
shell-company families, and a docket-class gap in the explicit method) rather than to the method being
wrong in general — every one of the 24 correct links is a link an EIA-plant-id-only or queue-id-only
resolution path could never have produced, which is the whole point of adding a document-kind source.

### 14.5 Next step and request budget

**Next step (not done here — this task's scope was the measurement, `docs/00-PLAN.md`):**

1. In `_sponsor_matches`/`_distinctive`, require each surviving "distinctive" token to be at least 3
   characters (kills the "S S" bug, 10/50 of this sample's errors) and detect a trailing-numeral-only
   difference between two otherwise-identical distinctive strings as a *reason to refuse*, not accept, a
   pair (kills the "Fresh Air II/XXIII" bug, 4/50). Both are narrow, targeted changes to the existing gate,
   not a new scoring method.
2. Restrict `_explicit_matches` to `ER`-class dockets the way `_sponsor_matches` already is (kills the
   "Greene County" bug, 1/50), and require the substring to start at a word boundary (kills "Alden"-inside-
   "Walden" and "Diamond Solar"-inside-"Black Diamond Solar Power", 2/50).
3. Re-run `pipeline/backfill_ferc.py` unchanged after those fixes and recount; removing 3 of the 4 known
   false-positive patterns (17/26 of this sample's errors, all in an already-small threshold change) is the
   single highest-leverage next measurement, not a full 600-label programme — that stays the right target
   for the *held-out* number `docs/22` §10 asks for once a signal clears this bar.
4. The 14 "correct but company-level, not project-level" sponsor matches (§14.3) are a real signal but a
   weaker one than an exact self-reference; whether the platform should surface them differently (e.g. a
   lower-confidence `sponsor_only` tag) is a product question for the owner, not a data question this
   backfill resolves.

**Request budget:** 60 requests total (6 ISO + 54 FERC), 301.6 s wall time, 2026-09-13 — well inside the
polite rate limits (`data/sources.yaml`: FERC eLibrary 0.5 rps, ERCOT 0.5 rps, CAISO/NYISO default 1 rps)
and the ~20-minute time-box; no throttling, no `success: false` retries, no window skipped.

### 14.6 Assumption recorded

- A-22-6: the 50-link precision sample (§14.3) was graded by reading the `rationale`/`ferc_title`/
  `docket_refs` text already captured in `data/probes/ferc-backfill-2026-09-13.json`, not by an independent
  lookup against FERC eLibrary or the ISO queue sites. Treat 48.0% as a directionally reliable estimate from
  the evidence on hand, not a certified precision number — `docs/22` §10's two-person, held-out labelling
  protocol is what turns an estimate like this into one quotable externally. Owner/data-scientist should
  confirm before this number is cited outside this document.

## 15. Operator edges and curated parent links for the midstream layers (2026-09-19)

Inputs: the four EIA Atlas natural gas parquets written by `pipeline/context/eia_atlas.py` on 2026-09-19
(pipelines 259 rows, processing plants 478, underground storage 412, LNG terminals 8; `data/sources.yaml` §K
`verified` notes carry the URLs, byte counts and vintages). Loader: `services/ingest/midstream.py`. Numbers
below are from one run against a fresh SQLite database, `python -m services.ingest.assets` then
`python -m services.ingest.midstream edges` per layer, then `python -m services.ingest.midstream parents`.

### 15.1 What an operator string becomes

Every non-blank `Operator` (pipelines, processing plants, LNG) or `Company` (storage) string is one
`asset_owner` edge with `role = operator`, and every non-blank `Owner` string (processing plants, LNG) one with
`role = owner`; `share_pct` is NULL because the Atlas states no shares, `owner_name_raw` is the source
spelling, and the edge's `source_id`/`source_url`/`retrieved_at`/`licence_id` are the layer's own (the same
`source` row the asset carries), never a synthetic id. The string resolves to an `organization` through the
same key §13.3 uses — `pipeline.normalize.norm_org`, corp suffixes and punctuation stripped, over
`organization.name_canonical` and every `organization_alias.alias` — so the Atlas strings join the sponsors and
filers the proposal loaders already created rather than duplicating them, and a string that matches nothing
creates one organisation plus a `filing_spelling` alias (confidence 0.9, the same reasoning
`services/ingest/ownership.py` records for its lower-than-exact confidence).

Measured: 1,126 operator edges and 457 owner edges over 1,157 assets; 628 organisations created from 253 +
191 + 133 + 8 distinct raw strings (the four layers share operators, and case/suffix variants collapse). A
re-run wrote 0 new organisations and 0 duplicate edges (edges are keyed by the table's unique constraint
`(asset_id, organization_id, role, source_id)` and looked up before insert).

Two limits worth knowing before a company page is read literally:

- The Atlas truncates some strings. EIA-191's company field gives `TALLGRASS INTERSTATE GAS TRANSMISSIO`
  (36 characters) where the pipeline layer gives `Tallgrass Interstate Gas Transmission`; `norm_org` cannot
  equate a truncated token with its full form, so these are two organisations. The curated parent file (§15.2)
  links both to the parent, which is what the company page needs; a merge of the two children is a §13
  resolver decision, not something this loader guesses.
- An owner string and an operator string that are the same organisation produce two edges with different
  roles on the same asset (Douglas Plant: owner and operator both `Tallgrass Energy Midstream LLC`). That is
  the intended shape — the roles are facts the registry states separately.

### 15.2 Curated parent links (interim for GLEIF Level 2)

`data/vendored/organizations/parents.yaml` holds rules of the form (child name pattern, parent canonical
name, source URL, retrieved_at, note). `load_parents` matches each pattern (a case-insensitive regular
expression) against every organisation's canonical name and aliases, creates the parent organisation if no
existing organisation normalises to its name, and sets `organization.parent_org_id` plus
`parent_source_id = curated.organization_parents` — a source registered in `data/sources.yaml` §K with
reuse `open`, publication `raw_ok` and the licence text "curated by Infraque from the companies' own
published statements; each row cites its URL", so the provenance rule (every stored fact names its source)
holds for a fact a human read off a company page. A child that is itself the parent is skipped; a rule that
matches nothing is reported, not silently accepted.

Seed, 2026-09-19: six rules for Tallgrass Energy from
<https://www.tallgrass.com/energy-solutions/natural-gas>, read live that day ("Rockies Express Pipeline (REX),
Ruby, Tallgrass Interstate Gas Transmission, Trailblazer, Cheyenne Connector, and East Cheyenne Gas Storage",
plus the gathering/processing statement). Against the loaded Atlas data the rules linked nine organisations:
`Rockies Express Pipeline`, `Rockies Express (Entrega)`, `Rockies Express (Echo Springs Lateral)`,
`Tallgrass Interstate Gas Transmission`, `TALLGRASS INTERSTATE GAS TRANSMISSIO`, `Trailblazer Pipeline Co`,
`Ruby Pipeline LLC`, `EAST CHEYENNE GAS STORAGE LLC`, `Tallgrass Energy Midstream LLC` — which is every
Tallgrass-related string the four layers contain (Cheyenne Connector post-dates the 202001 pipeline vintage
and has no row to match; `Cheyenne Plains Pipeline Co` is a different company and is deliberately not
matched). A re-run changed nothing (0 created, 0 linked, 9 unchanged).

What replaces it, and by how much: GLEIF Level 2 relationship records (CC0), loaded 2026-09-20 by
`services/ingest/organizations.py` with `parent_source_id = global.gleif.lei` (§17). The answer measured
there is that it replaces **none** of these nine rows — five of the children hold an LEI and not one files a
Level 2 record — which is the general case for operating subsidiaries, so the curated file is a permanent
mechanism rather than a placeholder. Where GLEIF does have a record it overwrites the curated link, and
`load_parents` refuses to overwrite a GLEIF one, so the two are order-independent. Nothing in the curated file asserts a share, a legal form or an ownership chain beyond one parent
hop, and none of it is inferred from a name alone — each row is a statement the company itself published.


### 15.3 EIA-860 Schedule 4 owner shares (2026-09-19)

The Schedule 4 connector now fetches its own archive. EIA publishes the current final release at
`xls/eia860<year>.zip`, which robots.txt allows; only past years move to the disallowed `archive/xls/`
path. `python -m pipeline.connectors run us.eia.860` took the 2025 final release
(<https://www.eia.gov/electricity/data/eia860/xls/eia8602025.zip>, 23,622,347 bytes, server date 2026-09-10,
retrieved 2026-09-19T20:48:22Z, sha256 `2b27929d…`), 5,680 Ownership rows, DQ pass.
`pipeline/context/eia_owners.py --latest-snapshot` wrote 5,680 owner rows over 2,534 plants, 4,018
generators and 2,038 distinct owner strings. Two columns are new: `generator_status` (the sheet's own
status code) and the generator/plant nameplate joined from the same zip's `3_1_Generator_Y2025.xlsx`
"Operable" sheet (4,983 of 5,680 rows carry a generator nameplate; the rest are retired, cancelled or
proposed units).

**The share a company owns of a plant.** Schedule 4 reports a percentage per *generator*, and its title
row says it lists "Jointly or Third-Party Owned" generators only. So a plant-level share is neither the
mean of the generator percentages nor a number that sums to 100 across the listed owners. The loader
computes `share_pct = Σ(pct_g × nameplate_g) / plant_nameplate`, where the denominator is the nameplate of
**every** operable generator at the plant, listed on Schedule 4 or not. A plant whose other units are
wholly owned by its operator therefore shows its joint owners at their true share of the site, and the
unlisted remainder stays implicit — no edge is invented for an owner the registry does not name. Where the
parquet carries no nameplate (a fixture, or an owner whose rows are all retired units) the loader falls back
to the unweighted mean, or writes a NULL share; `OwnershipLoadResult` counts which path each edge took
(3,024 nameplate-weighted, 27 unweighted mean, 67 without a share, on the run below).

Over-allocated generators are **flagged, never normalised**: the 2025 release has two whose listed shares
sum above 100 (plant 341 generator CT5 at 150.0, plant 70387 BESS1 at 100.09). They are reported in the load
result and logged, and their rows are used exactly as stated. Rescaling would hide a registry error behind a
plausible number on a company page. Rows whose owner is the placeholder `Other` (35 rows on 11 plants,
Ownership ID 99999, no address — EIA's filing for an unnamed minority owner) are counted and skipped rather
than resolved into an organisation called "Other".

Measured, loading `data/normalized/context/us.eia.860.owners.parquet` into a copy of the dev store
(`web/.data/dev-shots2.db`, which already held 14,659 `power_plant` assets and 5,513 organisations):
5,680 rows seen, 35 placeholder rows skipped, **2,315 of 2,534 plants matched** an EIA-860M asset by plant
id (91.4 %), **3,118 owner edges** written over 1,867 organisations, of which **1,735 were created and 132
matched organisations the proposal and midstream lanes had already made**. The 219 unmatched plants are
plants EIA-860M does not carry as operating: 200 have only retired or cancelled generators on Schedule 4,
17 only proposed ones, 2 are mixed; none has an operable nameplate. A second run wrote 0 new organisations
and 0 duplicate edges (33.6 s then 3.4 s). Top ten owners by owned MW (share × the asset's EIA-860M
nameplate): Constellation Nuclear 6,228 MW; Georgia Power Co 4,678; Oglethorpe Power Corporation 4,082;
Virginia Electric & Power Co 3,873; ArcLight Capital Partners LLC 3,596; MidAmerican Energy Co 3,424;
PacifiCorp 3,321; Evergy Metro 3,093; Cornerstone Generation 2,718; Comanche Peak Power Co, LLC 2,430 —
the nuclear and large-coal joint ventures, which is what a jointly-owned-generators register should surface.

### 15.4 What the ownership load taught us about the organisation key

**The key must be one function.** The index was built on `norm_org` alone while the lookup fell back to the
upper-cased raw string when `norm_org` returned nothing. `norm_org("US Solar")` is `None` — both tokens are
on the suffix list — so that owner was created, indexed under a key nothing would look up, and re-created on
every run: 1 organisation and 20 edges churned per load. `services/ingest/ownership.py::org_key` is now the
single function the index, the lookup and the aggregation group key all use, and a regression test covers
exactly this name. The same bug shape exists in `services/ingest/midstream.py::_get_or_create_parent`
(line 285), which still inlines the fallback; it is that lane's file, recorded here rather than edited.

**The group key was too narrow.** Aggregation grouped on the raw case-folded owner string, so two spellings
of one owner at one plant were two groups and the second edge write overwrote the first. It now groups on
`org_key`, the same key resolution uses.

**How much collapses, and how much should not.** 2,038 distinct owner strings resolve to 2,020 keys — 18
keys carry more than one spelling. Some are exactly what the key is for (`Wellhead Services, Inc.` /
`Wellhead Services, Inc`; `Vistra Corp` / `Vistra Energy`; `Nextera Energy Resources` / `NextEra Energy
Resources, LLC`; `Clearway Energy, Inc` / `Clearway Energy Group`). Others are **over-merges**: `norm_org`
strips `energy`, `power`, `solar`, `wind`, `renewables`, `storage`, `holdings`, `partners` and `project`
along with the corporate suffixes, so `MidAmerican Energy Co` and `MidAmerican Solar LLC` both reduce to
MIDAMERICAN, as do `Entergy Corp` / `Entergy Power, LLC`, `Prairie Power Inc` / `Prairie Solar LLC`,
`Shell Renewables` / `Shell Wind Energy Inc.`, `BP America Inc` / `BP Wind Energy North America Inc`,
`SunRay Power LLC` / `Sunray Energy Inc` and `Anderson Wind Project, LLC` / `Anderson North Solar Project,
LLC`. These are separate legal entities inside one family, or two unrelated projects sharing a first word —
a parent link or nothing, not an identity. Seven of the eighteen multi-spelling keys are of this kind, and
each costs a company page precision (one "MidAmerican" row instead of a utility and its solar affiliate). Narrowing the suffix list is a §13 resolver decision
with consequences for every source, so it is recorded here as a measured cost, not changed by this lane.

**What the key cannot reach** goes in `data/vendored/organizations/aliases.yaml` (new, same contract as
`parents.yaml`: one entity per row, a cited document per row, nothing inferred from a name). Four rules,
all read from SEC EDGAR submissions metadata on 2026-09-19: `Wisconsin Power and Light Co` →
`Wisconsin Power & Light Co` (one registrant, CIK 0000107832; "and" survives `norm_org` where "&" becomes a
space, so the two spellings split); `Kansas City Power & Light Co` → `Evergy Metro` and `Westar Energy Inc`
/ `Western Resources Inc` → `Evergy Kansas Central, Inc` (EDGAR `formerNames`, renamed 2019 — no string
similarity to reach across, and older registry vintages still carry the former names). No loader reads the
file yet. Two candidates were left out for want of a source: `Farm Credit Leasing Service Corp` /
`Farm Credit Leasing Services Corporation` (farmcreditleasing.com serves an expired certificate; the FCA
institution directory does not name it in fetchable text) and `John Hancock` / `John Hancock Funding
Company` / `Manulife Infrastructure II Holdings A, L.P.` (manulife.com and johnhancock.com answer 403 to a
scripted request) — the last of which is the 31-plant and 30-plant pair in the top twenty, so it is worth a
human minute in a browser.

The twenty most frequent owner strings are dominated by financing and municipal-aggregation entities, not
utilities: `GSRP Project Holdings I LLC` (49 rows, 37 plants), `Nordic Solar, LLC` (40 rows, 9 plants),
`John Hancock Funding Company` (31 plants), `Manulife Infrastructure II Holdings A, L.P.` (30),
`Generate C&I Warehouse, LLC` (28), `Hunt Energy Network, LLC` (27), `NJR Clean Energy Ventures III
Corporation` (23), `Generate NY Community Solar Lessor III` (20), eight Ohio municipalities
(`City of Hamilton - (OH)` and siblings, 8–11 plants each), `Public Service Co of NM` (14),
`Wisconsin Public Service Corp` (13), `FirstLight Hydro Generating Company` (8) and
`Florida Municipal Power Agency (FL)` (7).
Three of the twenty matched an organisation another lane had already created — `Hunt Energy Network, LLC`
(ERCOT queue sponsor), `Public Service Co of NM` (EIA Atlas pipeline operator) and
`NJR Clean Energy Ventures III Corporation` (NYISO queue sponsor) — which is the join the ownership graph
exists for: a tax-equity or IPP name on an operating plant is the same row as the sponsor on a queued one.
The other seventeen were new, as expected for financing vehicles and municipalities that never file a queue
request.

## 16. The organisation key: legal forms only (2026-09-19)

**Status:** measured and applied. Code `pipeline/normalize.py` (`norm_org`, `org_key`), consumers
`services/resolve/merge.py`, `services/ingest/ownership.py`, `services/ingest/midstream.py` and
`sponsor_norm` in the connectors (not `services/ingest/loader.py` — see §16.3). Labelled data `data/eval/organization_pairs.csv` (757 hand-labelled
pairs). Tests `tests/test_org_key.py`. Every number below was measured on the 7,642 distinct
organisation strings reachable today: the 5,513 live `organization` rows and 5,702 distinct
canonical-plus-alias spellings in the dev store (`web/.data/dev-shots2.db`, 2026-09-19 build)
together with the 2,038 distinct EIA-860 Schedule 4 owner strings.

### 16.1 The defect

`norm_org` stripped, alongside the legal forms, this list: `energy, energies, power, renewables,
solar, wind, storage, development, developments, partners, group, usa, us, america, american,
north, project, projects, holdings`. Those are not legal forms; they are exactly the words that
distinguish one legal entity from another inside a corporate family, and one project SPV from its
sibling. §15.4 measured 7 over-merges over the EIA owner strings alone. Over the full corpus the
number is much larger, and it is on a live product surface: a company page lists the assets of
every organisation that collides on the key, and `asset_owner` edges are attributed to whichever
of them the loader created first.

### 16.2 Both error rates, measured

**Over-merge (the key merges two names).** This is a *census*, not a sample: the old key put
7,642 names into 7,189 keys, 380 of which hold more than one name, giving **557 name pairs** —
all of them labelled by hand. Labelling rule, stated because it is a modelling choice: `same` =
one legal entity (punctuation, case, spacing, legal form, abbreviation, `&`/`and`, or a plural
spelling); `different` = distinct legal persons, including two SPVs of one developer that differ
by technology word, and two unrelated companies sharing a first token; `family` = the ambiguous
middle, a parent or brand against its named affiliate (`AES` / `AES Energy Storage, LLC`, `RWE` /
`RWE Renewables`, `Chevron` / `Chevron USA Inc`, the 21 Invenergy affiliate pairs). **The decision
for `family` is split**: they are separate legal persons that file separately and own different
assets, so one identity is wrong; the relationship belongs in `parents.yaml`
(`organization.parent_org_id`), which already exists for exactly this.

| Label | Pairs | Share of the 557 |
|---|---|---|
| `same` — one legal entity | 299 | 53.7 % |
| `family` — parent/affiliate, ambiguous | 137 | 24.6 % |
| `different` — distinct companies | 121 | 21.7 % |

**46.3 % of what the old key merged is not one company** (258/557; no confidence interval is
quoted because this is the whole population, not a sample).

**Over-split (the key keeps two names apart).** No census is possible — the non-merged pairs are
~29 million. Candidates were generated by blocking on `rapidfuzz.token_sort_ratio >= 88` over all
7,642 names: 1,813 pairs, of which **1,610 are split by the old key**. A **random sample of 200**
(seed 11) was labelled by the same rule: **3 `same`, 7 `family`, 190 `different`**. The
over-split rate among high-similarity candidates is therefore **1.5 % (3/200), 95 % Wilson CI
0.51 %–4.32 %**, or 5.0 % (2.74 %–8.96 %) counting `family`. Scaled to the 1,610 candidates that
is ~24 truly-same pairs split today (CI ~8–70). **Caveat that must travel with this number:** it
only covers pairs that *look* alike. A rename (`Westar Energy` → `Evergy Kansas Central`) has no
string similarity at all and is invisible to this measurement; those are alias work, §16.5.

The asymmetry is the finding. Over-merge is ~46 % of a small population of merges; over-split is
~1.5 % of a small population of look-alike splits, and its mass is nearly all numbered sibling
SPVs (`Cottontail Solar 1` / `Cottontail Solar 8`, `KCE NY 22` / `KCE NY 28`, `ENSO GREEN HOLDINGS
J` / `... L`) which *must* stay apart. Tightening the key is clearly the right trade.

### 16.3 The corrected key

`norm_org` now does, in order: lower-case; `&` → ` and `; non-alphanumerics → space; fold
co-op/co op/cooperative to one spelling; remove legal-form tokens repeatedly until stable; fold a
**closed list** of business-vocabulary plurals to the singular; upper-case.

- `LEGAL_FORM_TOKENS` is `llc, l l c, inc, incorporated, corp, corporation, co, company,
  companies, lp, l p, llp, lllp, ltd, limited, plc, pllc` — and nothing else. `RESERVED_CONTENT_WORDS`
  names the words that must never join it, and `tests/test_org_key.py` fails if one does.
  Rejected from the list after measurement: `gp` (it merges `CMD Carson GP LLC` into `CMD Carson
  LLC`, which are a general partner and the partnership it manages — two entities); `lc` and `pc`
  (too easily a pair of initials); and the non-US forms `sa/nv/bv/ag/kg/oy/ab`, where `ag` alone
  reduces `AG Energy Inc` to `ENERGY`.
- The `&`/`and` fold: `Wisconsin Power & Light Co` == `Wisconsin Power and Light Co`, the split
  the alias file was seeded to patch; also `Louisville Gas & Electric`.
- The plural fold is a closed list (`renewables→renewable`, `holdings→holding`, `services→service`,
  …, 24 entries), **not** a "drop a trailing s" rule, which would destroy Texas, Kansas, Illinois,
  Dallas, Hess and Atlas. It recovers 12 of the 19 same-company pairs a legal-forms-only key would
  otherwise split (`EDF Renewable Development Inc.` / `EDF Renewables Development`; `NextEra …
  Interconnection Holding, LLC` / `… Holdings, LLC`; `Valero Renewable Fuels` / `Valero
  Renewables Fuels LLC`; `Renegade Renewable LLC` / `Renegade Renewables, LLC`; `SED NY Holding
  LLC` / `SED NY Holdings LLC`).
- Iterated stripping handles stacked forms: `Astoria Generating Company, L.P.` == `Astoria
  Generating Co.`.
- A leading article is dropped (2026-10-06, audit 2026-09-30 data scientist F9): `THE SOUTHERN CO` ==
  `SOUTHERN CO`. Only the first word, so `Bank of the West` keeps its `the`. On a copy of the 2026-09-30 dev store
  it joins exactly 4 pairs of the 8,289 live organisations: Southern Co (3 / 30 asset edges), Williams /
  The Williams Companies, Inc. (9 / 2), Dayton Power & Light Co / The Dayton Power & Light Co (1 / 2), Medical
  Center Co / The Medical Center Company (1 / 0, with 4 proposals). The fuzzy census beyond the article (892 pairs
  at `token_sort_ratio >= 92`, 4 of a 60-pair sample the same entity) is not acted on: it needs labels.
- `norm_org` still returns `None` for a string that is nothing but legal forms (`"LLC"`).
  `pipeline.normalize.org_key` is the total version, and **it is now the one function** the
  resolver, the ownership index, the midstream parent lookup and their group keys all call —
  `services/ingest/ownership.py::org_key` re-exports it and `midstream.py`'s inlined,
  divergent fallback (§15.4 flagged it as the same bug shape) is gone.

One correction to the brief this lane was given: `services/ingest/loader.py` is **not** a
`norm_org` consumer. Its `_org_punct_key` is a punctuation-and-case-only key, deliberately
conservative at insert time, with `resolve_organizations` doing the suffix-level merging
afterwards as a recorded, reversible event. So the loader is unaffected by this change; the four
call sites that move are `services/resolve/merge.py`, `services/ingest/ownership.py`,
`services/ingest/midstream.py`, and `sponsor_norm` in `pipeline/normalize.py` and the connectors
(which feeds `pipeline/resolve.py`'s sponsor scoring component).

### 16.4 What the change does, quantified

On the 557-pair census:

| | Old key | Corrected key |
|---|---|---|
| Pairs merged | 557 | 305 |
| … `same` | 299 | **292** (7 lost) |
| … `family` | 137 | 13 (124 split) |
| … `different` | **121** | **0** (all 121 split) |
| Precision of the merge decision (`same` / merged) | 0.537 | **0.957** |
| … counting `family` as acceptable | 0.783 | **1.000** |

On the 200-pair over-split sample the corrected key is also *better*, not worse: it merges 2 of
the 3 `same` pairs the old key split (`Astoria Generating Company LP` / `… Company, L.P.`;
`Farm Credit Leasing Service Corp` / `… Services Corporation`, the pair §15.4 could not find a
source for) and merges none of the 190 `different` ones. Across the whole corpus it gains 17 new
merges, every one of them hand-checked and a true same-company pair.

**Effect on the resolver's own evaluation** (`pipeline/resolve.py --sweep` against
`data/eval/labels.csv`, 85 usable labels, chosen threshold 75 — the §6 numbers):

| | precision | recall | F1 | weighted P | weighted R | tp/fp/fn/tn |
|---|---|---|---|---|---|---|
| Before | 0.927 | 0.950 | 0.938 | 0.915 | 0.946 | 38/3/2/42 |
| **After** | **0.975** | **0.975** | **0.975** | **0.961** | **0.976** | 39/1/1/44 |

Two false positives and one false negative are removed: the sponsor component can now tell two
companies apart, which is what §7.3 said organisation resolution was a prerequisite for.
`data/eval/normalized.parquet`, `matches.parquet` and `clusters.parquet` were regenerated.

**Effect on the store path** (`python -m services.resolve.report`, in-memory store, same 2026-09-12
pull, same gate, run both ways on 2026-09-19 — the §13.4 measurement repeated):

| | Before | After |
|---|---|---|
| Organizations before / after resolution | 3,034 / 2,784 | 3,034 / **2,883** |
| Normalised-name groups merged | 212 (250 absorbed) | **133 (151 absorbed)** |
| Store-path precision / recall, 77 usable labels | 0.946 / 0.946 (tp35 fp2 fn2 tn38) | **1.000 / 0.973** (tp36 fp0 fn1 tn40) |

99 of the 250 organisation merges the store used to apply were wrong by the census above, and the
proposal-level precision through the store rises to 1.000 on the labels it can answer.

**Effect on today's loaded data** (dev store, 2026-09-19 build):

- 5,513 live organisations today. Rebuilt under the corrected key the 5,702 name strings yield
  **5,455 keys instead of 5,318 — about +137 organisations (+2.6 %)**.
- 101 groups of live organisations (224 rows) that `resolve_organizations` would have merged stay
  apart.
- **122 of the 3,217 `asset_owner` edges (3.8 %) move to a different organisation** — their raw
  owner string no longer keys to the organisation they currently hang off. Examples, each a wrong
  attribution on a live company page today: `WM` (24 edges) → under `WM Renewable Energy, LLC`; `Dominion Energy Transmission, Inc.` (19 edges across its two
  spellings) → under `Dominion Transmission Co`; `BLACK HILLS ENERGY CORP` (7) →
  under `Black Hills Power, Inc.`; `Williams Partners LP` (4) → under `Williams`;
  `North American Power Systems` (4) → under `Renewable Energy Systems Limited`, an unrelated
  company.

### 16.5 What the key deliberately cannot reach, and belongs in `organization_alias`

Seven pairs in the census (five distinct entities) are one legal entity that no legal-form key can merge, because the
difference is a content word or a rename. They are alias work, not key work — a key loose enough
to reach them merges MidAmerican Energy with MidAmerican Solar again:

| Pair | Evidence read 2026-09-19 |
|---|---|
| `Vistra Energy` → `Vistra Corp` | SEC EDGAR CIK 0001692819: conformed name "Vistra Corp.", formerName "Vistra Energy Corp." through 2020-06-29 — one registrant renamed. |
| `Enable Midstream` → `Enable Midstream Partners` | SEC EDGAR CIK 0001591763, conformed name "Enable Midstream Partners, LP"; the bare form is a short spelling in EIA-860 Schedule 4. |
| `Noble Environmental` → `Noble Environmental Power, LLC` | SEC EDGAR CIK 0001381415, conformed name "NOBLE ENVIRONMENTAL POWER LLC", no former names. |
| `Dominion Transmission Co` → `Dominion Energy Transmission, Inc.` | **Not yet sourced.** The May-2017 Dominion rebrand is visible on EDGAR for sibling registrants (CIK 0001603291 "Dominion Gas Holdings" → "Dominion Energy Gas Holdings", CIK 0001603286 "Dominion Midstream Partners" → "Dominion Energy Midstream Partners", both 2017-05-16) but DTI itself has no EDGAR registration, so the rule is *not* written: inference by analogy is what the curated-file contract forbids. Worth a FERC Form 2 minute. 19 `asset_owner` edges hang on it. |
| `NY Power Authority & LS Power Grid NY Corporation II` / `… Development & …` | Not sourced; a JV filing would settle it. |

**Correction (2026-09-20, §17.5):** `data/vendored/organizations/aliases.yaml` was *not* re-purposed
— it kept this schema throughout; the features lane's PHMSA↔Atlas operator file is the separate
`external_operator_aliases.yaml`, with its own loader in
`services/ingest/enrich.py::apply_context_features`. The three sourced rules above are now **written
to `aliases.yaml` and applied** by `services/ingest/organizations.py::load_aliases` — three rows,
`kind="filing_spelling"`, `confidence=1.0`, each carrying its `data.sec.gov` URL, under the new
source id `curated.organization_aliases`. `tests/test_org_key.py` (`test_rename_and_short_form_pairs_are_alias_work_not_key_work`) pins all four pairs as *not* key merges so a later "loosen the key a
little" is measured against them.

### 16.6 A production migration, if there is ever data to migrate

There is none today: the dev store is rebuilt from parquet on every `web/dev_up.py` run, and no
production database exists. When one does, the change is **not** a re-key in place, because
splitting an organisation is not expressible as an `unmerge` unless the merge was recorded as an
event. A migration would have to:

1. Recompute `org_key` for every `organization.name_canonical` and every `organization_alias.alias`
   and find the groups that the old key had collapsed (the 101 groups / 224 rows measured above).
2. For every organisation that is a *merge product*, reverse it: where §13's
   `resolve_organizations` produced the merge there is an `organization` `merged` event carrying a
   full `before` payload, so `unmerge_organization` restores the absorbed row exactly (docs/21 §6.3
   invariant M1), and that covers every proposal-side organisation, because the proposal loader
   inserts on the punctuation-only `_org_punct_key` and leaves the suffix-level merge to §13.
   The ownership and midstream loaders are the other case: `_resolve_organization` matches on
   `org_key` *at insert time*, so one row was created for two spellings with no event and no
   `before` state. There the only correct fix is to create the new organisation, move the affected
   `asset_owner` / `proposal.sponsor_org_id` / operator edges by re-keying their stored raw
   strings (`asset_owner.owner_name_raw` exists for exactly this; the proposal side would need the
   `proposal_source` raw sponsor), and write a `split`-reason `alias_removed`/`created` event pair
   so the move is itself reversible.
3. Re-point the ~3.8 % of edges whose raw string re-keys elsewhere, then recompute
   `organization.asset_count`-style derived fields and invalidate every company-page cache.
4. Keep the old key available for one release as `legacy_org_key` so a URL or public id that was
   issued against a collapsed organisation can still redirect.

The cheap alternative, while no durable rows exist: **rebuild**. That is what should happen now.

### 16.7 Assumptions recorded

- A-22-8: `family` pairs (a parent or brand against its named affiliate) are split, not merged.
  This is a product judgement as much as a data one — a user looking at "Invenergy" probably wants
  the whole family, and the answer to that is `parent_org_id` plus a rolled-up company page, not
  one identity. 137 of the 557 census pairs turn on it; if the owner decides the other way, the
  right implementation is still a parent link, never a looser key.
- A-22-9: the over-split rate (1.5 %, n=200) is measured only over pairs with
  `token_sort_ratio >= 88`. Renames and abbreviations below that threshold are not counted and are
  not reachable by any key; their size is unknown and would need a different sampling frame
  (e.g. pairs that share an EIA plant or a FERC docket).

## 17. The ownership graph: GLEIF Level 2 parents, an alias loader, a corrections route (2026-09-20)

**Status:** measured and applied, loader on by default. Code `pipeline/context/gleif.py` (the connector),
`services/ingest/organizations.py` (both loaders), `services/ingest/midstream.py` (deference), migration
`0015` (`organization.parent_as_of`), wiring `web/dev_up.py::_load_organization_graph`. Labelled data
`data/eval/gleif_matches.csv` (272 hand-labelled pairs). Tests `pipeline/context/test_gleif.py`,
`services/ingest/test_organizations.py`. Every number below was measured on the 2026-09-20 GLEIF publish
and a **copy** of `web/.data/dev-shots2.db` (5,513 organisations, 3,217 `asset_owner` edges, 9 curated
parent links), never the live dev file.

### 17.1 The fetch: the small file plus one streaming pass over the large one

GLEIF publishes the whole corpus as CC0 golden-copy zips, indexed at
<https://goldencopy.gleif.org/api/v2/golden-copies/publishes?format=json> (`data/sources.yaml`
`global.gleif.lei`; the CC0 statement is quoted in docs/13 §2.13). Two files, both fetched 2026-09-20T02:19Z
from the 2026-09-20T00:00Z publish:

| File | URL | Bytes | Records | sha256 |
|---|---|---|---|---|
| Relationships (`rr`, RR_2.1) | `…/2026/09/20/1278560/20260920-0000-gleif-goldencopy-rr-golden-copy.csv.zip` | 24,368,364 | 488,439 | `430f0995469775c40a9d14877e6a8f129220ef3e1648d16ddee34df6d1624d16` |
| Entities (`lei2`, LEI_3.1) | `…/2026/09/20/1278515/20260920-0000-gleif-goldencopy-lei2-golden-copy.csv.zip` | 504,405,368 | 3,435,980 | `3bb4c1e3b571ab75d3e31adcf11f3a8d954e6a3b41a6fe2c6ececcf19b70ad9b` |

**Which route, and why.** The relationship file *is* the filtered download: 23 MB holds every Level 2 record
there is, so nothing is paged and the result is reproducible from one artefact. The entity file is not small,
and it is needed because a relationship record carries LEIs and no names, and a name is all the loader can
match on. The alternative — `api.gleif.org/api/v1/lei-records?filter[lei]=…`, 200 LEIs per request — is about
920 requests against a rate-limited host for the same answer, so one 481 MB download is both faster and more
polite. `read_entities` streams it: 1 MB chunks through `csv.reader`, keeping only the 183,855 LEIs that
appear in a consolidation relationship, never the 5 GB of decompressed CSV. It asserts the seven column
positions it uses against the header first, so a CDF change fails loudly instead of writing the wrong column
onto a company page.

**What comes out.** `python -m pipeline.context.gleif` → `data/normalized/context/global.gleif.lei.parents.parquet`,
68.9 s, **259,784 rows**. Of the 488,439 relationship records, 259,790 are the two types that are ownership
(`IS_DIRECTLY_CONSOLIDATED_BY` 126,807, `IS_ULTIMATELY_CONSOLIDATED_BY` 132,983); the other 228,649 are
`IS_FUND-MANAGED_BY`, `IS_SUBFUND_OF`, `IS_FEEDER_TO` and `IS_INTERNATIONAL_BRANCH_OF` — fund administration
and branch registrations, not ownership, and dropped. 140,431 distinct children, 54,186 distinct parents, 6 rows
dropped because an LEI has no entity record. GLEIF's own as-of dates are all kept per row (relationship period
start and end, accounting period end, record last-update); the loader picks. In GLEIF's model the **start node
is the child**, which is why the type reads `IS_…_CONSOLIDATED_BY`.

### 17.2 The labelled census, and the threshold

The brief that created the curated file said GLEIF goes in "behind a 200-pair labelled sample". The candidate
population turned out to be **272 pairs**, so this is a census, not a sample: every candidate the loader can
draw was labelled by hand. `data/eval/gleif_matches.csv` holds all 272 with the evidence each judgement was
made on (both names, LEI, GLEIF legal address, the platform organisation's evidence countries, its edge and
proposal counts, the proposed parent) and a `note` on every non-`same` row.

**How candidates are drawn** — exactly as the loader draws them: `pipeline.normalize.org_key` (the corrected
legal-forms-only key, §16.3) over `organization.name_canonical` and every `organization_alias.alias`, against
`org_key` of the GLEIF child legal name, after filtering GLEIF to live records (`relationship_status ACTIVE`,
`registration_status PUBLISHED`, `child_entity_status ACTIVE`) and keeping one row per child, direct parent
preferred over ultimate.

**Labelling rule**, the same three-way rule §16.2 used, stated because it is a modelling choice: `same` = one
legal person (differences of punctuation, case, legal form, `&`/`and`, plural or abbreviation); `family` = one
corporate family, two legal persons (a brand or short name against a named affiliate, a parent against its own
subsidiary); `different` = unrelated legal persons, **including two same-named entities in different
jurisdictions**. Per A-22-8 `family` counts as *not* a match in the headline precision, and is reported
separately.

| Slice | n | `same` | `family` | `different` | Precision | 95 % Wilson CI | Counting `family` |
|---|---|---|---|---|---|---|---|
| Key equality alone (whole census) | 272 | 258 | 7 | 7 | 0.949 | 0.916–0.969 | 0.974 |
| Key equality, the loader's own candidate set | 209 | 199 | 5 | 5 | 0.952 | 0.914–0.974 | 0.976 |
| **What the loader accepts** | **201** | **198** | **3** | **0** | **0.985** | **0.957–0.995** | **1.000** |

**The two gates, and what each buys.**

1. **Country evidence.** `organization.country` is `US` on all 5,513 rows — the ownership and midstream
   loaders hard-code it — so it carries no information at all, including for the 2,198 GB proposals' sponsors.
   The organisation's *evidence* does: the two-letter prefix of every `proposal.jurisdiction` it sponsors, plus
   the `country` of every asset it owns or operates. The GLEIF entity's legal-address country must be one of
   them. This gate removes **all seven** `different` pairs and four of the seven `family` ones, and every one
   of them is a real defect it prevents: `Ameresco, Inc.` (37 `asset_owner` edges) would have been given the
   LEI of `AMERESCO LIMITED` of Leeds, whose parent is Ameresco, Inc.; `Cargill Inc.` that of `CARGILL PLC`;
   `SPIRE INC` that of `SPIRE LIMITED` of Trinidad and Tobago; `DOW CHEMICAL COMPANY` that of the British
   `DOW CHEMICAL COMPANY LIMITED`; `Constellation` and `Atlantic Energy, LLC` (US-NY sponsors) their French
   namesakes; and `LAKESIDE ENERGY STORAGE LIMITED`, a GB proposal sponsor, a Delaware LLC of Jupiter Power.
   It costs exactly one true match: a US-NY queue sponsor spelled `Brookfield Renewable Power`, against the
   Canadian `BROOKFIELD RENEWABLE POWER INC.`. That is the trade, and it is clearly the right one.
2. **One GLEIF entity per key.** Where two GLEIF entities survive the country gate on one key the loader links
   neither. It fires on `Air Products`, which keys to a US LLC, a Belgian and a French entity; the country gate
   already leaves one, so the measured count is 0 — the guard is for the day it is not.

**Would I stake a company page on 0.985?** Yes, with the caveat that the three remaining errors are all
`family`, not `different`: `Air Products` → `AIR PRODUCTS HELIUM, INC.` (the only one that is plainly wrong —
the platform's bare brand string is really Air Products and Chemicals, Inc.), `ConocoPhillips` →
`CONOCOPHILLIPS COMPANY`'s parent, and `Archaea Energy, LLC` → `BP PRODUCTS NORTH AMERICA INC.`. Two of the
three still name the right corporate family; none puts one company's assets on an unrelated company's page,
which is the failure mode §16 was about. The loader is therefore **on by default**. What would change that
judgement is a `different` error surviving the gates; there is none in the census, and the CI's lower bound
(0.957) is the number to re-measure against when the corpus grows.

**Caveat that travels with the number.** This measures *precision*, not recall. The frame is "pairs the key
already matches", so a rename or a short form that no key can reach is invisible here, exactly as A-22-9 says
for the organisation key. 98,651 GLEIF children survive the status filter and only 220 keys touch the platform
at all — coverage, not correctness, is the open problem, and §17.6's corrections route is part of the answer.

### 17.3 What GLEIF covers, what the curated file still does — the Tallgrass verdict

**GLEIF covers none of the nine.** Five of the curated file's nine children hold an LEI —
`TALLGRASS INTERSTATE GAS TRANSMISSION, LLC` (`5493001PPWSDLETMIS87`), `ROCKIES EXPRESS PIPELINE LLC`
(`W2ZGZGZKY5GGNY6F3V51`), `TRAILBLAZER PIPELINE COMPANY LLC` (`549300R4CX55WWLTX861`), `RUBY PIPELINE, L.L.C.`
(`549300VXRTBPBK07QT94`) and `EAST CHEYENNE GAS STORAGE, LLC` (`MVIXRNC8YS7D5NZBAG33`) — and **not one of them
is the start node of any Level 2 relationship record**. The only Tallgrass consolidation edges in the whole
corpus are for entities the platform does not carry (`TALLGRASS MLP OPERATIONS, LLC`,
`STANCHION GAS MARKETING, LLC`, `STANCHION ENERGY, LLC` and `HIGH PLAINS CARBON FINANCING, LLC`, all into
`TALLGRASS ENERGY PARTNERS, LP`). So the question "does GLEIF agree with the curated rules" does not arise:
there is nothing to agree or disagree with, all nine curated rules stand, and none of the 199 GLEIF links
touches an organisation any curated pattern matches (measured: 0 overlap, and `load_parents`'
`children_deferred_to_gleif` is 0). The curated file is not a placeholder that GLEIF retires; it is the
mechanism for the operating subsidiaries that hold an LEI but file no Level 2 record, which on this evidence
is most of the midstream layer.

**How the two coexist.** `parent_source_id` tells them apart. `load_gleif_parents` overwrites a curated link
only for an organisation it matches (counting it in `children_relinked_from_curated`); `load_parents` skips any
organisation already carrying `parent_source_id = global.gleif.lei` (`children_deferred_to_gleif`). Order is
therefore a preference, not a correctness requirement, and re-running either writes nothing.

**Why status filtering matters here.** 75,420 of the 259,790 consolidation records carry
`Registration.RegistrationStatus = LAPSED` — GLEIF still publishes them, but no LOU has re-validated them. They
stay in the parquet (an expired parent is a fact about the past) and the loader drops them. That filter
takes the candidate population from 272 pairs to 209.

### 17.4 Measured before and after, on a copy of the dev store

`cp web/.data/dev-shots2.db …/measure.db`, then `python -m services.ingest.organizations parents` (5.2 s) and
`… aliases` (0.5 s). The live file and the servers on 8100/8101 were not touched.

| | Before | After |
|---|---|---|
| Organisations | 5,513 | **5,600** (+87 parents GLEIF named that nothing had created) |
| Organisations with a parent | 9 | **208** |
| … from `curated.organization_parents` | 9 | 9 (unchanged) |
| … from `global.gleif.lei` | 0 | **199** (165 direct, 34 ultimate) |
| Organisations with `parent_as_of` | — | 199 (range 1912-08-17 … 2026-03-17) |
| Organisations carrying `ids.lei` | 0 | **304** (199 children + 105 distinct parents) |
| `organization_alias` rows | 5,702 | 5,793 (+87 GLEIF `legal_name`, +4 curated) |
| `asset_owner` edges | 3,217 | 3,217 (unchanged — this lane writes no edges) |

Reach: **143 `asset_owner` edges and 484 proposals** now hang off an organisation with a named parent. The
largest families are RWE (18 children), Jupiter Power (13), ScottishPower (7), Ørsted, Entergy, Energix, Drax
and Brookfield (5 each). Disagreement between the two sources: **0** (§17.3). Idempotence: a second GLEIF run
reports 0 linked / 199 unchanged / 0 organisations created; a second alias run 0 written.

Wiring: `web/dev_up.py::_load_organization_graph` runs after the owner shares, the context features and the
curated parents, guarded exactly as its neighbours are — a missing module, a missing parquet or an unreadable
YAML is one log line, never a failure of the rest of `dev_up`.

### 17.5 The alias loader, and the three rules that were waiting for it

`data/vendored/organizations/aliases.yaml` now has a loader
(`python -m services.ingest.organizations aliases`) and the three SEC-sourced rules §16.5 left for it:
`Vistra Energy` → `Vistra Corp` (CIK 0001692819), `Enable Midstream` → `Enable Midstream Partners`
(CIK 0001591763), `Noble Environmental` → `Noble Environmental Power, LLC` (CIK 0001381415). The file keeps its
schema and its contract — one legal entity per row, a cited filing per row, nothing inferred from a name. Its
header now also states the split from `external_operator_aliases.yaml`, which is the features lane's
cross-dataset operator file with its own loader in `services/ingest/enrich.py`; the two are not merged, and
§16.5's note that this file had been re-purposed was out of date.

Provenance needed a source id, so `curated.organization_aliases` is registered in `data/sources.yaml` §K and in
the docs/13 §6 matrix (permissive, raw-ok, facts not expression), mirroring `curated.organization_parents`.
Each written row is `kind = filing_spelling`, `confidence = 1.0` — higher than the ownership loader's 0.9,
because this match is a statement read off a filing the row cites, not a key collision — and carries the rule's
own `source_url` and `retrieved_at`.

Measured on the dev-store copy: 7 rules, **4 alias rows written, 2 already present, 1 inert**. The inert one is
`Kansas City Power & Light Co` → `Evergy Metro`: no organisation named Evergy Metro is loaded yet, so the rule
attaches nothing and is *reported*, not an error — writing the rule before the data arrives is the point of the
file. Three rules are also reported as `rules_already_one_organization`: the corrected key's `&`/`and` fold
already reaches `Wisconsin Power and Light Co`, and the Enable/Noble pairs already share an organisation
through an existing alias; the rows are still written so the spelling carries its citation. A rule whose alias
already keys to a *different* live organisation is reported as a conflict and skipped — merging two live
organisations is a §13 `resolve_organizations` decision with a reversible event behind it, never something an
alias loader does silently.

### 17.6 A corrections route for ownership, specified not built

Ownership is the part of this graph most likely to be wrong in a way an outsider can see and we cannot: a
registry vintage lags a sale, a name is truncated, a parent changed last quarter. §17.2's precision is about
the links we make, not the ones we miss, and §17.3 shows how thin Level 2 coverage is for operating
subsidiaries. A public "this ownership is wrong" route is the cheapest source of the corrections neither
registry publishes.

**It reuses the privacy-request pattern exactly** (`services/api/privacy_routes.py`, US-910 — that is another
lane's file and nothing here is implemented in it). That pattern already solves every hard part: a public,
unauthenticated `POST` that stores a row and answers `202` with its own public id; no email sent, because
CLAUDE.md says outbound communication to real people is drafted by an agent and sent by a human; no IP, user
agent or referrer recorded; no confirmation of whether the referenced record exists, so the endpoint cannot be
used to probe for records the caller cannot see; per-IP rate limiting through the shared `default_limiter` at
the same 20-per-window shape as the other public routes; and an operator queue under `/admin/v1/…` with a
status filter, oldest-open-first, where closing the request clears the one piece of personal data it holds.

**What it would store.** A `correction_request` table shaped like `privacy_request` and sharing its statuses
(`open | in_progress | done | rejected`):

| Field | Type | Null | Meaning |
|---|---|---|---|
| `id`, `public_id` | uuid, text | No | As `privacy_request` |
| `subject_kind` | text | No | `organization_parent \| asset_owner \| organization_alias` — the vocabulary of things this route accepts a correction for, so a submission always points at a fact with a source, not at free text |
| `subject_public_id` | text | No | The organisation's or asset's public id, as shown on the page the reader was looking at |
| `claim` | text | No | `wrong_parent \| wrong_owner \| wrong_share \| not_this_entity \| out_of_date` |
| `proposed_value` | text | Yes | What the submitter says is right (a parent name, an owner, a share, a date) |
| `evidence_url` | text | Yes | Where they read it. **The field that decides whether the row is usable**: a correction with a citable public document can be applied under the same contract as `parents.yaml`; one without it can only prompt an operator to go looking |
| `observed_source_id`, `observed_as_of` | text, date | No | The `parent_source_id`/`as_of` the page was showing, captured by the form, so an operator can tell a stale render from a real defect without re-deriving it |
| `contact_email` | text | Yes | Optional, cleared on close, exactly as `privacy_request.contact_email` is |
| `message` | text | Yes | ≤ 4,000 chars, same cap |
| `status`, `completed_at`, `created_at` | — | — | As `privacy_request` |

**How it reaches an operator.** The same way privacy requests do, and no other way: rows land in the admin
queue, oldest open first, with the subject's current values rendered beside the claim. There is no automatic
write to `organization.parent_org_id` from a submission — an anonymous POST must never be able to move an
ownership edge on a public page. An accepted correction is applied by the operator through the existing
curated files (a new `parents.yaml` or `aliases.yaml` row citing `evidence_url`, which is already the
"one entity per row, one cited document per row" contract), so the correction arrives in the graph with
provenance and is reversible by deleting a YAML row. A rejected one is closed with a `reason`, and an audit
event carries only the hash of the contact address (`services/api/audit.hash_identifier`).

**Two cheap wins it also buys.** A correction that names a parent GLEIF does not publish is exactly the
`parents.yaml` row §17.3 says the midstream layer needs; and the count of open corrections per source is a
data-quality signal the admin source-health page can show without any new instrumentation.

### 17.7 Assumptions recorded

- A-22-10: a GLEIF legal entity and a platform organisation are the same entity when the corrected `org_key`
  matches **and** the entity's legal-address country is one the organisation's own evidence names. Measured
  precision 0.985 (198/201, 95 % Wilson CI 0.957–0.995) on the 272-pair census in `data/eval/gleif_matches.csv`.
  If the corpus grows past the US/GB mix it is measured on — say a Canadian or Mexican queue — the gate should
  be re-measured before it is trusted, because its power comes entirely from evidence countries being few.
- A-22-11: a Level 2 record is used for a live parent link only when the relationship is `ACTIVE`, its
  registration `PUBLISHED` and the child entity `ACTIVE`. 75,420 of the 259,790 consolidation records are
  `LAPSED` alone. The rows stay in the parquet, so the decision is reversible by changing one filter; the
  filter costs 63 of the 272 candidate pairs.
- A-22-12: `ids["lei"]` is written only for organisations on one end of a link this loader made. A free-standing
  name match against all 3.4 M LEI records would be a different and unmeasured precision claim, and is not made.
- A-22-13: `parent_org_id` stays one hop. GLEIF's ultimate parent is used only where no direct record exists;
  the chain is not walked, because each hop would compound the match error and nothing on a company page needs
  it yet.

---

## 18. Schedule slippage: a project past its own stated date (2026-09-21)

**Status:** built · code in `services/api/slippage.py`, rendering in `web/viewmodels.py` and
`web/templates/_macros.html`, tests in `services/api/test_slippage.py` and `web/test_slippage_view.py`.
Every number below was measured on the 2026-09-21 load (`web/.data/dev.db`, 10,409 proposals) with
`.venv/bin/python` against the ORM; the commands are in §18.7.

### 18.1 What prompted it, and the direction the gap actually runs

Global Energy Monitor splits cancellation and shelving into an **announced** state and one **inferred from
lack of observed progress**. The first proposal was to split our `withdrawn` state the same way. Measured,
that is wrong and there is nothing to do: all **3,215** `withdrawn` proposals carry a literal `WITHDRAWN`
(1,762) or `Withdrawn` (1,453) in `status_raw`, straight from the ISO queue. Every one is announced. A split
would produce an empty second bucket.

The gap runs the other way. We hold no **inferred** signal of any kind, and there is one available:

| | |
|---|---|
| proposals in an active lifecycle state with a `proposed_online_date` already past | **469** |
| proposals carrying a `proposed_online_date` at all | **8,180** |
| of the 469, overdue by more than three years | **14** |

The 469 break down `under_construction` 316, `permitted` 47, `studied` 36, `filed` 36, `contracted` 34.
(`announced` contributes 0: all 688 `announced` rows carry a date and all 688 are in the future.)

`last_changed` cannot stand in for this. It records when *we* observed a change, not when the project moved.
On the current load every one of the 10,409 rows has `first_seen` and `last_changed` inside a seven-second
window on 2026-09-21 — the load itself — so a staleness query over it returns zero rows by construction.
The `event` table is also empty (0 rows): there has been one load, so there is no change history to infer
absence of progress from. Both are recorded here so that the next person does not re-derive them.

### 18.2 Definition

A proposal is **slipped** when all three hold:

1. its `lifecycle_state` is one of `announced`, `filed`, `studied`, `permitted`, `contracted`,
   `under_construction` (`SLIP_ACTIVE_STATES`) — `built` is excluded because a finished project's target
   date is spent, `withdrawn`/`cancelled` because a dead project is not late, `unknown` because we cannot
   claim it is active;
2. `proposed_online_date` is not null — **nothing promised is never late**, and this is the single rule the
   whole signal depends on not getting wrong;
3. `(today - proposed_online_date).days > 90`.

It is **not** a lifecycle state. `LIFECYCLE_STATES` is unchanged, there is no migration, and nothing about
it is stored.

### 18.3 The grace period is the whole design, and 90 days is measured

A bare `proposed_online_date < today` test is mostly artefact. Sensitivity of the flagged count to the grace
period, and how much of it comes from EIA-860M:

| grace | flagged | `us.eia.860m` share | `under_construction` |
|------:|--------:|--------------------:|---------------------:|
|   0 d |     469 |        308 (66 %)   |                  316 |
|  31 d |     283 |        129 (46 %)   |                  138 |
|  60 d |     146 |          5 (3 %)    |                   14 |
|**90 d**|**129** |      **0**          |                  **9** |
| 120 d |     121 |          0          |                    9 |
| 365 d |      62 |          0          |                    6 |

Two source artefacts produce that cliff, and neither is slippage:

- **Month-granularity dates.** All 2,341 `us.eia.860m` dates and all 201 `us.iso.nyiso.gen_queue` dates fall
  on the first of a month, because the source field is a (`Planned Operation Month`, `Planned Operation Year`)
  pair. **182** of the naive 469 carry `2026-09-01` — the current month, which has not finished. Those cannot
  be late under any reading. (CAISO 892/2,022, ERCOT 433/1,778 and NESO 104/1,838 are first-of-month, so those
  registers carry real per-project days.)
- **Report vintage.** The loaded EIA-860M file is `july_generator2026.xlsx` — the **July 2026** report,
  retrieved 2026-09-13. A further **125** rows carry `2026-08-01`, a month the evidence itself predates.
  Separately, **111** of the 316 flagged `under_construction` rows carry the source status
  `(TS) Construction complete, but not yet in commercial operation`: construction finished, commercial
  operation not yet declared. That is the reporting lag, not a slip.

90 days is where the curve flattens (129 → 121 over the next 30 days), it is longer than any calendar month
so it clears the first artefact outright, and it is longer than EIA's two-month publication lag so it clears
the second. One number for every source, so no per-source table can rot.

### 18.4 How much of the 469 is real slippage, stated with the evidence

- **307 of 469 (65 %) are month-granularity artefacts**, named individually: 182 at `2026-09-01` (current
  month, 178 of them `under_construction`) and 125 at `2026-08-01` (one month back, from a report
  published later). Not slippage.
- **111 of 469 (24 %)** — overlapping the above — are EIA `(TS) Construction complete, but not yet in
  commercial operation`. Not slippage even where the date is genuinely past.
- **129 of 469 (28 %) survive the 90-day grace**, and **none of them come from `us.eia.860m`**: 79 from
  `gb.neso.tec_register`, 33 CAISO, 16 ERCOT, 1 NYISO. Their source statuses are `Consents Approved` (39),
  `ACTIVE` (33), `Scoping` (25), `Active` (17), `Awaiting Consents` (8), `Under Construction/Commissioning`
  (7) — live projects, per-project dates, dates missed.
- **62 of those are more than a year past** and **14 more than three years**, including dates in 2012, 2016,
  2020 and 2021. Those are not explicable as reporting lag.

**Judgement, calibrated:** of the naive 469, roughly two thirds is source-date artefact and the 129 that
survive the grace are defensible as real slippage — with the caveat that we cannot yet *prove* the 67 in the
`under_1y` bucket individually, because we hold one snapshot per source and no change history to corroborate
them with. The 62 beyond a year are as close to certain as a single snapshot allows. What the grace cannot
fix is a register that stops maintaining `proposed_online_date` once a project completes; that would keep a
finished project reading as overdue for ever. Nothing in the current load exhibits it (73 `built` proposals
have past dates and are excluded by rule 1), but it is the mode to watch, and it is why the UI always prints
the target date beside the verdict rather than a bare badge.

### 18.5 Where it is computed, and why

**Read time, in `services/api/slippage.py`.** A stored `is_overdue` column would be wrong the day after it
was written: the record does not change, the calendar does. Keeping one true needs a daily recompute job
that does not exist, and a wrong flag on a published record is worse than no flag, because a caller cannot
tell a stale one from a fresh one. Both things a column would buy are unnecessary here — the predicate is a
plain range comparison on `proposed_online_date` (sargable, no function on the column), and the working set
is 129 rows out of 10,409. No migration was written.

There are two implementations and they are twins, the same pattern `services/api/geo.py::effective_placement`
and `visibility.location_exact_permitted()` already use: `slip_days()` in Python for serialisation,
`slip_filter()` in SQL for filtering. `test_sql_twin_agrees_with_the_python_twin` pins them together, because
a disagreement would mean a filtered page whose rows contradict the filter that selected them. Both are
expressed in whole-day arithmetic against literal date bounds computed in Python, so SQLite and Postgres
behave identically and no SQL date function is involved.

One SQL trap is worth recording: `NULL < date` is NULL and `NOT NULL` is NULL, so a naive negation for
`slipped=false` would silently drop all **2,229** proposals with no target date out of the complement.
`_slipped_clause` leads with `is_not(None)` so the conjunction is FALSE (never NULL) for those rows and
their negation is TRUE; `test_slipped_false_keeps_undated_rows` holds it.

### 18.6 API and UI

**Both a boolean and buckets**, justified by the distribution rather than symmetry. 129 of 5,826 dated
active proposals are slipped (2.2 %), rare enough that `?slipped=true` is a useful way to find them at all;
but within the 129 the split is **67 / 48 / 14** and the buckets do not mean the same thing. A NESO
`Consents Approved` row eight months past its date is a live project with a late connection; the 14 rows
beyond three years are a different prospect. With only a boolean a caller wanting those 14 must pull all 129
and re-derive the threshold client-side, which is exactly the rule that then disagrees with ours.

- `?slipped=true|false` — anything else is a `400 validation_error` naming the value.
- `?slip_bucket=under_1y,1_to_3y,over_3y` — `csv_param`, unknown token is a 400 naming the token. Day
  boundaries 91–365 / 366–1,095 / 1,096+.
- `slipped=false` with a bucket list is a **400 naming the conflict**, not a 200 with an empty page: an
  empty page reads as "no such records", which is a different and wrong answer.
- `GET /v1/meta/vocabularies` publishes `slip_bucket[]` with `min_days_late`, `max_days_late` and
  `grace_days`, so a caller drawing its own line can see where ours is instead of guessing.
- `Proposal.schedule_slip` is `{target_date, days_late, bucket, grace_days}` or `null` — null, not a zeroed
  object, so "no promise on record" can never be read as "on schedule".

**Rendering is graded, because a signal that cries wolf is ignored.** The 90-day grace already does most of
that work: it takes `under_construction` from 316 flagged rows to **9 of 1,038**, so no badge lands on the
ordinary EIA construction population at all. Beyond that, `under_1y` (67 rows) renders as plain outlined
text with no colour — a connection date moving a few months is ordinary for a consented project — and
`1_to_3y` and `over_3y` (48 and 14) take the warning colour. It is deliberately **not** a status chip: the
chip states what the source reports, this states an inference of ours, and merging the two would dress a
derivation as a source fact. The target date is printed beside it every time, and the detail page carries a
sentence saying the verdict is derived at read time and that a register which stops maintaining the date
would produce exactly this appearance.

It is on the list and the detail page and **not** on the map, deliberately. The map's feature payload is
column-narrowed on purpose (`services/api/app.py::_GEO_PROPOSAL_COLUMNS`, with a comment recording what
unused-column hydration cost), `proposed_online_date` is not in it, and a per-row marker is unreadable at
cluster scale anyway. The list and the detail page are where someone is judging one project; the filter
works on `/v1/proposals/geo` regardless, so a caller can still draw a map of only the slipped ones.

### 18.7 Commands

```
.venv/bin/python -m pytest services/api/test_slippage.py web/test_slippage_view.py -q
DATABASE_URL="sqlite+pysqlite:///$PWD/web/.data/dev.db" \
  .venv/bin/python scripts/measure_slippage.py --on 2026-09-21    # every count in §18.1, §18.3, §18.4
```

`scripts/measure_slippage.py` reproduces the whole of §18.3 and §18.4 from a loaded store and writes the
grace sweep to `data/eval/slippage_distribution.csv`. It exists because 90 days is a judgement backed by one
curve: if a register changes its date granularity or its publication lag the curve moves, and this is the
command that shows it rather than a paragraph nobody can re-run.

### 18.8 Assumptions recorded

- **A-22-14:** a developer's own published `proposed_online_date`, more than 90 days past, is evidence that
  a project has slipped. It is a *proxy* for GEM's "inferred from lack of observed progress", not the same
  inference: GEM reasons from absence of news, we reason from a commitment the developer published. Ours is
  better-grounded evidence (a dated claim by the party who would know, not our failure to find news) and
  narrower in reach — it can only speak about the 8,180 records that carry a date at all, and it inherits
  whatever date-maintenance discipline each register has.
- **A-22-15:** 90 days of grace, applied uniformly. Re-measure it if a register with finer date granularity
  than a month or a shorter publication lag becomes a large share of the corpus; the table in §18.3 is the
  measurement to repeat.
- **A-22-16:** `SLIP_ACTIVE_STATES` duplicates `web.viewmodels.ACTIVE_PROPOSAL_STATES` (services must not
  import web). `test_active_states_match_the_web_default_view` fails if they diverge, because a row hidden
  from the default list while still being flagged on it would be incoherent.
- **A-22-17 (not built, deliberate):** queue age is the obvious second signal and is not used yet. `Queue
  Date` is present on **1,673 of 5,837** active proposals (28.7 %) and only from the three US ISO queues —
  nothing in EIA-860M or the NESO register carries one — against 5,826 of 5,837 (99.8 %) for
  `proposed_online_date`. Its distribution is informative (616 under two years, 494 two-to-four, 280
  four-to-six, **283 over six years**), but at a third of the coverage and on one region it would be a
  second, differently-shaped signal rather than a better version of this one, and it is not parsed out of
  the raw payload into a column today. Recorded as the next thing to build, not as a substitute.
- **A-22-18:** the *structural* analogue of GEM's "inferred from lack of observed progress" is already
  in the schema and is empty for want of a second pull, not for want of a design: `proposal_source.gone_at`
  (a row that stopped appearing in its register's file) and the `event` log. Measured 2026-09-21 — 10,409
  `proposal_source` rows, **0** with `gone_at` set, **0** inactive, **0** snapshots, **0** events, all
  `first_seen` values on one day. That signal is not a competitor to this one and should not be traded
  against it: a disappearance is evidence about the *register*, a missed date is evidence about the
  *project*. Build it when a second pull exists; nothing in this section blocks it.

---

## 19. Tallgrass Development, and the organisations a registry truncated (2026-09-26)

Prompted by the Tallgrass company page under-reporting. Measured on a copy of the coordinator's `web/.data/dev.db`
(8,374 organisations, 9,346 `asset_owner` edges, 0 merge events), five organisations carry "tallgrass": three
under `Tallgrass Energy` through the curated file, `Tallgrass Energy` itself under Blackstone Infrastructure
Partners, and `TALLGRASS DEVELOPMENT LP` — one GHGRP owner edge (Douglas Plant, Converse County WY, 100 %) and no
parent. Two questions, answered separately because they have different mechanisms.

### 19.1 (a) `TALLGRASS DEVELOPMENT LP` sits under Tallgrass Energy — on the registrant's own account, dated 2018-02-07

**The primary source is Tallgrass Energy, LP's Form 10-K for 2018** (CIK 1633651, filed 2019-02-08,
<https://www.sec.gov/Archives/edgar/data/1633651/000163365119000009/tge2018123110k.htm>; the 10-K for 2019 repeats
it verbatim). It says three things the rule needs, quoted in `parents.yaml`:

1. *Identity.* "References to 'Tallgrass Development' or 'TD' refer to Tallgrass Development, LP."
2. *The edge, and its date.* "On February 7, 2018, Tallgrass Development merged into Tallgrass Development
   Holdings, LLC, a wholly-owned subsidiary of Tallgrass Equity", where Tallgrass Equity, LLC is the subsidiary
   "through" which TGE's operations are conducted, TGE holding "an approximate 55.79% membership interest as of
   December 31, 2018" and being its managing member.
3. *The asset.* "We own a 100% membership interest in Tallgrass Midstream, LLC ('TMID') ... TMID also owns and
   operates natural gas processing plants in Casper and Douglas, Wyoming" — the plant GHGRP files under the string.

So the answer is yes, with one qualification that the rule carries as `as_of`: **before 2018-02-07 the answer was
no.** Tallgrass Development, LP was the *sponsor* above TEP — "Prior to the February 2018 merger discussed below,
Tallgrass Energy Holdings was the general partner of Tallgrass Development", and "Historically, TEP acquired a
number of its assets from Tallgrass Development" (TIGT and TMID at the 2013 IPO, Trailblazer 2014, Pony Express,
REX interests, Terminals). An undated rule would have described 2012–2018 backwards. This is the second dated rule in
the file, and the first whose child is a legal entity that no longer exists: GHGRP's 2023 `parent_company` field
names a partnership that merged away five reporting years earlier, which is why the registry string and the filing
disagree, and why the pattern requires the LP suffix (`^tallgrass development,? ?l\.?p\.?$`).

What was checked and does **not** support it, recorded so nobody re-checks: GLEIF (api.gleif.org, 2026-09-26)
holds no LEI for Tallgrass Development, LP; its fulltext search returns 17 "tallgrass" entities of which the only
similar name, `TALLGRASS DEVELOPMENT, INC.` (5493004S0XNITWHOGX17, Lincoln NE, alongside Tallgrass Senior/Family
Housing LPs), is a different company that the anchored pattern does not match. EDGAR's entity index has no
registrant of the name. Tallgrass's own natural-gas page names systems, not holding entities. The hop is one, to
`Tallgrass Energy`, by the file's existing convention; the two intermediate entities (Tallgrass Development
Holdings, LLC; Tallgrass Equity, LLC) own nothing on the platform and are not created. No share: the filing's
100 % is TGE's interest in TMID and the successor's status under Tallgrass Equity, TGE's own interest in Tallgrass
Equity was 55.79 %, and no source states one number for this string. `parent_share_pct` stays NULL.

### 19.2 (b) The 25 prefix pairs, classified

Reproduced the scan (names ≥ 30 characters, case-insensitive prefix, merged rows excluded): 25 pairs, the same 25.
They fall into five classes, and only one of them is alias work:

| Class | Pairs | What they are | Where they belong |
|---|---|---|---|
| **Fixed-width truncation of one legal entity** | `TALLGRASS INTERSTATE GAS TRANSMISSIO` (36) / `…Transmission`; `Markwest Liberty Midstream & Res` (32) / `MarkWest Liberty Midstream & Resources`; `Green Knight Economic Development Corpor` (40) / `…Corporation` | A registry cut the string; the remainder is not a legal form, so no key reaches it | **`aliases.yaml`**, three rules written (§19.3) |
| Same key already | `Kerrville Public Utility Board Public Facility Corp` / `…Corporation`; `Black Mountain Energy Storage II` / `… II LLC`; `Greenalia Solar Power Ratcliff` / `…, LLC`; `Momentum Energy Storage Partners` / `…,LLC`; `NextEra Energy Interconnection Holdings` / `…, LLC`; `North Bergen Liberty Generating` / `…, LLC` | `org_key` is already equal (Corp/Corporation and the LLC suffix are legal forms); two rows exist because the proposal loaders insert on the punctuation key and §13 `resolve_organizations` has not run on this store (0 events) | §13 resolver, not this file — an alias rule would itself be reported as a conflict |
| Different entities | `Generate NY Community Solar Lessor II` / `III`; `NY Power Authority & LS Power Grid NY Corporation I` / `II` | Numbered vehicles | Nothing; must never merge |
| Joint filings | `Anbaric Development Partners, LLC` / `…, NY OceanGrid, LLC`; `Braintree Electric Light Department` / `…; International Finance Corporation (IFC)`; `Integrys Energy Services, Inc.` / `…; LGS Development, L.P.`; `Northeast Maryland Waste Disposal Authority` / `…, MD; SCS Engineers`; `Siemens Building Technologies, Inc.` / `…; Sustainable Energy Solutions LLC` | LMOP's "X; Y" owner strings are two parties, a split problem in the LMOP loader | The LMOP connector, not this file |
| Annotation suffixes | `Cape May County MUA` / `…, NJ`; `Florida Municipal Power Agency` / `… (FL)`; `Golden Triangle …` / `… (GTR Solid Waste), MS`; `Los Angeles Department of Water & Power` / `… (LADWP)`; `Milwaukee Metropolitan Sewerage District` / `…, WI`; `Southern Minnesota Municipal Power Agency` / `… (SMMPA)`; `Ventura Regional Sanitation District` / `…, CA`; `Graphic Packaging International` / `… - WACO`; `Prologis Logistics Services Incorporated` / `… BESS` | LMOP appends a state or an acronym to a name; the last two append a *site*, which is not the same entity | The first seven are one entity each and decidable from LMOP's own row, but they are a fold of a source convention (strip a trailing `, ST` / `(ACRONYM)`), not seven citations — left for a normaliser rule, recorded here, not written |

Where the truncations come from, measured rather than assumed. The Atlas underground-storage layer's operator
field is exactly 36 characters on **43 of 412** rows and never longer, while the shapefile's own DBF field width is
254, so the cut is upstream, in EIA-191's company field; TIGT is the only one of those 43 the platform also
carries in full. The processing-plant layer's owner field runs to 54 characters, so the 32-character MarkWest
string is a one-row cut (the same field spells the owner in full on five sister plants). EIA-860M entity names
pile up at exactly 40 characters (**199 rows**, against 115 at 39 and 13 at 41); the Schedule 4 owner names the
Green Knight string sits in do not (31 at 40, 40 at 41), so that width is EIA's somewhere upstream but the field
that applied it is not established. The identity in each case is established by a document, not by the width.

### 19.3 The three alias rules, and what the loader does with them on a real store

Each rule cites the document that names the legal entity **and places the truncated string's asset with it**,
which is the §16.5 bar: TIGT — the 2018 10-K, "The TIGT System includes the Huntsman natural gas storage facility
located in Cheyenne County, Nebraska", the exact EIA-191 field (340291) carrying the 36-character string, plus GLEIF
`5493001PPWSDLETMIS87`; MarkWest — MarkWest Energy Partners' 10-K for 2014 listing "the Mobley Complex located in
Wetzel County, West Virginia" among the Marcellus complexes of "MarkWest Liberty Midstream & Resources, L.L.C.",
plus GLEIF `549300E43NX2PKICX492` (the only entity with that prefix; EDGAR's only other "MarkWest Liberty" registrant
is MarkWest Liberty Gas Gathering, L.L.C., which the prefix excludes); Green Knight — the corporation's own pages
(<https://gkedc.org/energy-center/>, <https://gkedc.org/about/>), which give the full name, the 501(c)(3) form,
the March 2001 opening and "Waste Management is currently contracted to operate and maintain the facility" — the
arrangement EIA-860 records as owner `Green Knight Economic Development Corpor` / 860M operator `Waste Management
Inc` on plant 55765.

**Measured, `python -m services.ingest.organizations aliases` on the copy** (`DATABASE_URL` pointed at it; the
live file untouched):

| | Before (7 rules) | After (10 rules) |
|---|---|---|
| `aliases_written` | 0 | **0** |
| `aliases_unchanged` | 3 | 3 |
| `rules_without_organization` | 0 | **0** |
| `rules_already_one_organization` | 3 | 3 |
| `conflicts` | 4 | **7** (+ the three truncations) |
| `organization_alias` rows | 8,702 | 8,702 |

Every one of the three is reported as a conflict: *the alias already keys to a live organisation* — the truncated
string itself, which the storage / processing / ownership loader turned into an `organization` before the alias
loader ran. That is not a defect in the rules; it is the alias loader's contract (§17.5: a rule whose alias is a
live organisation is never merged silently), and on a `web/dev_up.py` store it is the **normal** case, because the
edge loaders run first. The four pre-existing conflicts (`Westar Energy Inc`, `Vistra Energy`, `Enable Midstream`,
`Noble Environmental`) are the same shape: §17.5 measured them as written on 2026-09-20 against a store where the
short spellings were not yet organisations; today's store has them as organisations with 1–2 edges each, and the
rules have gone from applied to conflicting with no change to the file. The conflict line now says what a merge
would move (`… a live organisation holding 1 asset_owner edge(s) and 0 proposal(s)`); before, it named the
organisation only.

The rules stay in the file for three reasons: they carry the citation a merge needs; the loader re-applies them the
moment the absorbed row is merged away (a merged organisation leaves `org_key_multimap`, so the alias then attaches
to the canonical and the *next* rebuild resolves the string at edge-insert time); and the conflict list is the
queue of merges to make, with sizes.

### 19.4 What would make the truncated organisations resolve, and why it is not done here

The mechanism is `services/resolve/merge.py::merge_organization` (docs/21 §6.3, §6.5): one `merged` event carrying
the absorbed row in `before`, `merged_into_id` set, reversible by `unmerge_organization`. Three merges, each with a
`rationale` citing the alias rule's document:

| Absorb | Into | Moves |
|---|---|---|
| `TALLGRASS INTERSTATE GAS TRANSMISSIO` | `Tallgrass Interstate Gas Transmission` | 1 edge (Huntsman, `gas_storage`) |
| `Markwest Liberty Midstream & Res` | `MarkWest Liberty Midstream & Resources` | 1 edge (Mobley Plant, `gas_processing_plant`) |
| `Green Knight Economic Development Corpor` | `Green Knight Economic Development Corporation` | 1 edge (Green Knight Energy Center, `power_plant`, owner 100 %) |

**It is not run in this lane, and should not be run by anyone yet, for a reason found while checking:
`merge_organization` re-points `proposal.sponsor_org_id` and does not touch `asset_owner.organization_id`**, and its
`before` payload records `sponsored_proposal_ids` only. On these three organisations, which sponsor nothing and own
one edge each, a merge today would move nothing forward and would move something *backward*: `services/api/orgtree.py`
excludes merged rows from the descent (`_children_of`: "a merged row is not a company, it is a redirect"), so the
absorbed organisation's edge would drop out of the Tallgrass page's `scope=all` rather than join it, and an unmerge
could not know to put it back because the event never recorded it. The alias it writes on the survivor also depends
on the absorbed row sponsoring a proposal (it takes the source quartet from the first active `proposal_source`), so
for an edge-only organisation no alias would be written either. `services/resolve/merge.py` is outside this lane's
files; the fix it needs is stated, not made: carry `asset_owner_ids` in `before.absorbed`, re-point them to the
survivor, restore them on unmerge (invariant M1: enough state to reverse without reading another row), and take the
alias's provenance from the edge's source when there is no proposal. Until that lands, the three rules do their job
as a documented, cited queue, and the parent link keeps both spellings on the Tallgrass page.

### 19.5 Measured before and after on the Tallgrass page

`cp web/.data/dev.db …/e5.db`, then `python -m services.ingest.midstream parents` and `… organizations aliases`,
each run twice:

| | Before | After |
|---|---|---|
| Organisations under `Tallgrass Energy` (direct = all, one level) | 9 | **10** (`TALLGRASS DEVELOPMENT LP`, `parent_as_of` 2018-02-07) |
| `asset_owner` edges reachable from the page, `scope=all` | 11 | **12** |
| Distinct assets reachable | 10 | 10 (Douglas Plant was already reached through `Tallgrass Energy Midstream LLC`'s operator and owner edges; the GHGRP 100 % owner edge now joins them) |
| Under Blackstone Infrastructure Partners, `scope=all` | 10 organisations, 11 edges | 11, 12 |
| Organisations with a parent (store-wide) | 318 | 319 |
| `parents` report | 7 rules, 10 unchanged | 8 rules, 1 linked, 1 dated, 11 unchanged on re-run, 0 `rules_without_match`, 0 deferred to GLEIF |
| `aliases` report | §19.3 | §19.3; second run identical |

So the page gains the entity and the edge the coordinator saw missing, and gains nothing spurious. The two
spellings of TIGT remain two children of one parent, which is what §15.1 said the file would do until a merge
event with edge support exists.

### 19.6 Commands

```
cp /home/user/Bankable/web/.data/dev.db $SCRATCH/e5.db
DATABASE_URL="sqlite+pysqlite:///$SCRATCH/e5.db" .venv/bin/python -m services.ingest.midstream parents
DATABASE_URL="sqlite+pysqlite:///$SCRATCH/e5.db" .venv/bin/python -m services.ingest.organizations aliases
.venv/bin/python -m pytest services/ingest/test_organizations.py -q
```

### 19.7 Assumptions recorded

- **A-22-19:** a parent rule may name a child that no longer exists as a legal entity when a registry still files
  under that name, provided the rule is dated to the event that put the successor under the parent and the note
  says the entity merged away. The alternative — leaving GHGRP's stale string unparented — under-reports a fact
  the registrant states. If GHGRP later corrects the string to the successor, the rule becomes inert
  (`rules_without_match`) and is deleted then, not now.
- **A-22-20:** a fixed-width truncation is alias work only when a document places the truncated string's *asset*
  with the entity; the width alone (36, 32, 40) is evidence about the source, not about identity. The seven LMOP
  annotation-suffix pairs in §19.2 are one entity each on LMOP's own row, but are a source convention to fold, not
  seven citations, and are deliberately not in `aliases.yaml`.
- **A-22-21 (found here; fixed the same day in §20 — merges now carry edges, children and aliases):** `merge_organization` does
  not re-point `asset_owner` edges or record them for unmerge. No organisation merge should be run against an
  organisation holding edges until it does; the three truncation merges in §19.4 wait on it.

## 20. Organisation merges carry the whole graph, and become part of the build (2026-09-26)

Numbered 20 because the entity-identity lane's section on the same day (which records this defect as §19.4
and assumption A-22-21) takes 19 on the branch; renumber on merge if that lands differently.

### 20.1 The defect, measured

`services/resolve/merge.py::merge_organization` re-pointed `proposal.sponsor_org_id` and nothing else, and its
`before` payload listed `sponsored_proposal_ids` only. Three consequences, each reproduced on a copy of the dev
store (`web/.data/dev.db`, 2026-09-26) by applying the three merges below with the pre-fix code:

- **Ownership edges stranded on a redirect.** `asset_owner.organization_id` stayed on the absorbed row, which
  `services/api/orgtree.py::_children_of` excludes ("a merged row is not a company, it is a redirect"). Tallgrass
  Energy's page at `scope=all` went from **10 organisations / 11 edges / 10 assets** to **9 / 10 / 9**: the
  Huntsman storage field, operated by `TALLGRASS INTERSTATE GAS TRANSMISSIO`, vanished from the group, and it was
  not on the survivor either (survivor `api_self_assets` stayed 1). MarkWest Liberty's survivor stayed at 5
  assets (the Mobley plant lost), Green Knight's at 1 (the EIA-860 power plant lost).
- **Unmerge could not restore them**, because the event never listed them. Invariant M1 (docs/21 §6.3) held for
  proposals only.
- **No alias for an edge-only organisation.** The alias for the absorbed spelling took its provenance from the
  absorbed row's first active `proposal_source`; all three absorbed rows sponsor nothing, so none got one, and
  their own alias rows stayed on the redirect, where `org_key_multimap` (live rows only) no longer reads them.

Two further gaps on the same principle, found while fixing it: the absorbed row's **children**
(`organization.parent_org_id = absorbed`) were left hanging off the redirect, so a merged-away parent's
subsidiaries dropped out of every descent; and `restore_row` did not convert `parent_as_of` (date) or
`parent_org_id` (UUID) back from JSON, so unmerging any organisation carrying a dated parent link failed at
flush on SQLite ("SQLite Date type only accepts Python date objects", reproduced) and left a string where a
UUID belongs until the row was re-read.

### 20.2 What a merge now moves, and what the event records

Everything that points at the absorbed row by foreign key moves to the survivor, and `before.absorbed` lists each
moved row's id, so `unmerge_organization` moves exactly those rows back without reading any other row (M1):

| Row | Column moved | `before.absorbed` key |
|---|---|---|
| `proposal` | `sponsor_org_id` | `sponsored_proposal_ids` (unchanged) |
| `asset_owner` | `organization_id` | `asset_owner_ids` (+ `asset_owner_collisions`) |
| `organization` (children) | `parent_org_id` | `child_organization_ids` |
| `organization_alias` (the absorbed row's own) | `organization_id` | `organization_alias_ids` (+ `organization_alias_collisions`) |

A child's `parent_source_id` / `parent_as_of` / `parent_share_pct` are not rewritten: the claim still comes from
the same source; only its target, now one row instead of two, changes.

**Parent link.** If the survivor has no parent and the absorbed row has one, the survivor takes all four parent
columns (the claim was made about the same legal entity); `before.surviving.parent` keeps the survivor's prior
(empty) link. If the survivor's parent *is* the absorbed row, the link would be a self-loop: the survivor takes
the absorbed row's own parent (the next link up the same chain) or none, same record. If both have different parents the survivor's stands and the absorbed row's remains in its snapshot.

**The spelling alias** is written only when the survivor carries no alias with that normalised spelling after the
moves — usually the absorbed row's own alias has just moved and already records the spelling with its original
provenance, which is better than a derived one. Otherwise its provenance is the first active `proposal_source` of
a sponsored proposal, else **the first ownership edge** (the record the spelling was read from); its id goes in
`after.written_alias_id`. On unmerge it stays on the survivor, as before (a spelling recorded once is still a
spelling that organisation is known by — `tests/test_resolve_store_unmerge.py` pins that).

**Backward compatibility.** Events already written carry only `sponsored_proposal_ids`; unmerge reads every new
key with an empty default, so an old event reverses exactly what the old merge moved and leaves the stranded
edges where they are (`test_an_old_shape_event_still_unmerges`). The event shape only gains keys.

### 20.3 The collision rule: keep both rows, never collapse

A collision is an absorbed-row edge to an asset and role the survivor already holds.

- **Same asset, same role, different source** is not a conflict: the edge moves and the survivor carries both
  rows, one per source. That is what several `asset_owner` rows for one fact are for (provenance per source), and
  `organization_asset_totals` counts distinct asset ids, so nothing is double-counted
  (`test_collision_keeps_both_rows_and_unmerge_is_exact` asserts 2 assets, not 3).
- **Same asset, same role, same source** cannot move: `uq_asset_owner_edge` is unique on
  `(asset_id, organization_id, role, source_id)`. The row stays on the absorbed organisation, untouched, and is
  listed in `asset_owner_collisions` with the survivor row it duplicates. It is **kept, not collapsed**, for two
  reasons: collapsing means deleting a row, which this module never does (docs/21 §6.1, §6.3); and the two rows
  can differ in `share_pct`, `as_of` and `owner_name_raw`, which the kept row and the event still show. Unmerge is
  exact because a collided row never moved.
- Aliases follow the same rule under `one_alias_per_org` (`organization_alias_collisions`).

The cost, stated: where one source lists the same company twice under two spellings with split shares (e.g.
EIA-860 Schedule 4 at 60 % and 40 %), the survivor's page shows the survivor row's share only. Summing them would
assert a number no source states. None of the three merges below collides, so the case is pinned by a test, not
observed in data.

### 20.4 Tests

`tests/test_resolve_org_merge_graph.py` (14 tests): the edge-only round trip with the org tree asserted
(`org_scope(..., "all")` and `organization_asset_totals` see the edge on the survivor after merge and on the
absorbed row after unmerge, row snapshot identical); the spelling alias taking the edge's provenance; children
re-pointed and restored; parent inheritance and both self-loop cases, each restored; the collision case with the full
`asset_owner` table compared before and after the round trip; an old-shape event unmerging cleanly; and the
curated-merge loader below. Run against the pre-fix `merge.py`, 8 of the 14 fail; the six that pass on both are
the old-shape event, the loader's conflict and file-parsing tests, which is as it should be.

### 20.5 Making merges part of the reproducible build

The coordinator rebuilds the dev store from scratch with `web/dev_up.py`, so a merge applied to one store is lost
at the next build. The mechanism: **`data/vendored/organizations/merges.yaml`**, one rule per merge
(`absorb`, `into`, `rationale`, `source_url`, `retrieved_at`), applied by
`services/ingest/organizations.py::load_merges` (CLI: `python -m services.ingest.organizations merges`), which
`web/dev_up.py::_load_organization_graph` runs after the alias loader — last, because it reads the rows every
earlier loader created. Each rule is one `merge_organization` call with `actor_type="user"` (a human decision,
docs/21 §6.4), the rationale in `reason`, and the cited document's `source_url` / `retrieved_at` on the event.
Idempotent; it reports `merged`, `already_merged`, `missing` (either name not loaded: inert, the `aliases.yaml`
contract) and `conflicts` (a name matching several rows, an absorbed row already redirecting elsewhere, a survivor
that is itself a redirect, both names one row) and never creates an organisation.

Why a separate file and not a flag in `aliases.yaml`: an alias rule and a merge rule do different things (one
attaches a spelling to an existing organisation; the other retires a second organisation row), the alias loader
already reports exactly these cases as conflicts it must not resolve, and keeping the two apart keeps
`read_alias_rules` and its tests unchanged.

Why the event carries no `source_id`: a new `source` row needs a `data/sources.yaml` entry, and every entry needs
a docs/13 §6 register row (`tests/test_manifest_licences.py`, rule R1). Neither is this lane's file. Borrowing
`curated.organization_aliases` would point the event's provenance at the wrong file, and its register row says
every rule cites `data.sec.gov`, which `gkedc.org` does not. The event therefore carries `source_url` and
`retrieved_at` of the cited document and a NULL `source_id` until a `curated.organization_merges` entry and
register row exist (A-22-23). The alias rows that move keep their own full provenance quartet.

The three rules, each read on 2026-09-26 and quoted in the file:

| Absorbed (truncated source spelling) | Survivor | Document | What ties the absorbed row's asset to the entity |
|---|---|---|---|
| `TALLGRASS INTERSTATE GAS TRANSMISSIO` (36 chars, EIA Atlas storage operator) | `Tallgrass Interstate Gas Transmission` | [Tallgrass Energy LP 10-K 2018](https://www.sec.gov/Archives/edgar/data/1633651/000163365119000009/tge2018123110k.htm) | "The TIGT System includes the Huntsman natural gas storage facility located in Cheyenne County, Nebraska" |
| `Markwest Liberty Midstream & Res` (EIA Atlas processing owner) | `MarkWest Liberty Midstream & Resources` | [MarkWest Energy Partners 10-K 2014](https://www.sec.gov/Archives/edgar/data/1166036/000104746915001157/a2223168z10-k.htm) | names "MarkWest Liberty Midstream & Resources, L.L.C." and the Mobley Complex, Wetzel County, WV |
| `Green Knight Economic Development Corpor` (40 chars, EIA-860 Schedule 4 owner) | `Green Knight Economic Development Corporation` | [gkedc.org/energy-center](https://gkedc.org/energy-center/) | "the Green Knight Economic Development Corporation (GKEDC) gas-to-energy facility", fuelled from Grand Central Sanitary Landfill |

### 20.6 Measured before and after, on a copy of the dev store

Copy of `web/.data/dev.db` taken 2026-09-26; merges applied with `DATABASE_URL` pointed at the copy through the
CLI above; page numbers read through `TestClient` on `GET /v1/organizations/{id}/assets`.

| Measure | Before | Old code | Fixed code |
|---|---|---|---|
| Tallgrass Energy `scope=all`: organisations | 10 | 9 | 9 |
| Tallgrass Energy `scope=all`: `asset_owner` edges (raw / API rows) | 11 / 11 | 10 / 10 | **11 / 11** |
| Tallgrass Energy `scope=all`: `totals.assets` (operator / owner) | 10 (9 / 2) | 9 (8 / 2) | **10 (9 / 2)** |
| TIGT survivor: edges / page assets / aliases | 1 / 1 / 1 | 1 / 1 / 1 | **2 / 2 / 2** |
| MarkWest Liberty survivor: edges / page assets / aliases | 10 / 5 / 2 | 10 / 5 / 2 | **11 / 6 / 3** |
| Green Knight survivor: edges / page assets / aliases | 1 / 1 / 1 | 1 / 1 / 1 | **2 / 2 / 2** |
| Edges left on the three absorbed rows | 3 | 3 (invisible) | 0 |

Each of the three events moved one edge and one alias; no sponsored proposals, children, collisions or written
alias. A second run of the loader reported all three `already_merged` and wrote nothing. Unmerging all three
events on the same copy (inside a rolled-back transaction) returned Tallgrass Energy to 10 organisations and
11 edges. Tallgrass Energy's `by_organization` now lists `Tallgrass Interstate Gas Transmission` with 2 assets
instead of the full and truncated spellings with 1 each.

### 20.7 Open, not in this lane's files

- **Loader re-runs write onto redirects.** *Fixed 2026-09-27 (lane E14), see §20.9.* `services/ingest/ownership.py::_build_norm_org_index` indexes every
  organisation, merged or not, and never follows `merged_into_id`. Measured on the merged copy: all three absorbed
  spellings resolve to the merged row, not the survivor. `dev_up` rebuilds and runs merges last, so it is
  unaffected; a scheduled re-run of the ownership, midstream, GHGRP or ethanol loaders against a merged store would
  write new edges and aliases onto the redirect, where the tree cannot see them — and an unmerge could then hit
  `uq_asset_owner_edge` moving a row back. Fix: resolve each index entry to its live terminal
  (`merged_into_id` chain) in that function.
- **docs/21 §6.3** shows the proposal payload only; the organisation payload keys in §20.2 belong there.
- **Registration** of `curated.organization_merges` in `data/sources.yaml` plus a docs/13 §6 row (A-22-23).
  *Done 2026-09-27 (lane E14), see §20.9.*

### 20.8 Assumptions recorded

- **A-22-22:** where the survivor and the absorbed row hold the same asset in the same role from the same source,
  the survivor's row is the one the page shows and the absorbed row's is kept on the redirect (§20.3). This is
  right when the two rows are one statement spelled twice; it under-reports a split stake listed under two
  spellings. Revisit if a collision is ever observed with differing `share_pct`.
- **A-22-23:** a curated merge event without a `source_id` is acceptable provenance for now because it carries
  the document URL, the read time and the rationale; it becomes a full quartet when the source is registered.
  *Closed 2026-09-27:* registered, and `load_merges` now writes `source_id` and `licence_id` (§20.9).

### 20.9 Loader re-runs follow the redirect; the merge file is a registered source (2026-09-27, lane E14)

**Redirects.** `services/ingest/org_redirects.py::OrgRedirects` reads every `merged_into_id` once per loader
call and returns the end of the chain for any organisation (a cycle, which no merge writes, is logged and the
row found is returned unchanged). Every name lookup that can attach a record to an organisation now goes
through it:

| Lookup | Before | After |
|---|---|---|
| `loader.py` sponsor, exact spelling (`org_by_exact`, all rows) | the redirect: new proposals sponsored by the absorbed row | the survivor |
| `loader.py` sponsor, punctuation key (`org_by_punct`, live rows only) | a punctuation variant of an absorbed spelling created a **new** organisation | live rows first, then absorbed spellings -> survivor |
| `ownership._build_norm_org_index` (ownership, midstream edges and parents, GHGRP, ethanol operator edges) | first row seen per key, redirect included: new edges and aliases on the absorbed row | live rows first, then redirects -> survivor; aliases -> survivor |
| `organizations.org_key_multimap` (GLEIF parents, alias rules) | live rows only: an absorbed spelling no alias carries created a duplicate | also absorbed spellings and their leftover aliases -> survivor, where no live row holds the key |

`load_merges` and `_orgs_named` deliberately still see redirects (they must report `already_merged`), and
`services/ingest/enrich.py` creates nothing, so neither changed. Tests: `services/ingest/test_org_redirects.py`
(7; 6 fail on the previous code) merge A into B and re-run the loader that created A: no new proposal, edge or
alias on A, no second copy of A, and a two-step chain lands on its end.

**Registration.** `curated.organization_merges` is in `data/sources.yaml` §K (reuse `open`, `raw_ok`,
mirroring `curated.organization_aliases`) and in the docs/13 §6 matrix; `load_merges` upserts the source and
passes its id and licence to `merge_organization` (two new optional arguments), so a curated merge event
carries a full provenance quartet. Events written before this carry a NULL `source_id` and are not rewritten
(the loader is idempotent: a rule already applied is reported `already_merged`).

## 21. Proposal–opportunity matching v1 (2026-09-26)

> **Mandatory caveat.** The 103 labels in `data/eval/match_labels.csv` were produced by the lane that
> implemented the rule set, not by an independent labeller, and that lane had read every notice title in the
> corpus before it labelled. US-401 AC3 asks for a sample "supplied by the data-scientist"; this is not that.
> Treat every number below as a first measurement to be repeated on independent labels, not as a pass or fail
> of the rule set against the acceptance criterion. `services/match/eval.py::draw_sample` re-draws the same
> frame from a loaded store with the label column empty, so an independent labeller can start from it.

### 21.1 What was built, and what the corpus can test

The rule set is `data/match_rules.yaml` (`match-rules@v1`): four rules — technology family, jurisdiction,
size window, timing — each yielding a credit in [0, 1]; a pair matches when all four pass and the weighted
score reaches 0.70 (`services/README.md` "Matches" has the rules in full). `python -m services.match.run
--all` on the store `web/dev_up.py` builds from data/normalized (10,409 proposals, 707 opportunities),
run on 2026-09-26, scores **1,199** pairs and produces **20** active matches (40 `match_added` events).

The corpus decides what that number can mean. Of 7.4 million proposal × opportunity pairs, only
1.22 million share a country, because the demand side is EU TED (458 notices), World Bank (100) and
grants.gov (149, every one `US`-national) and the supply side is US ISO queues, EIA-860M and the GB NESO
register: no GB notice exists (Find a Tender returned 0 rows) and no EU proposal exists. Of the US-national
notices, 142 of 149 carry no technology tag and the seven that do are four nuclear, two `efficiency` and one
`gas`. Measured over the 1.22 million same-country pairs at `now = 2026-09-26`:

| Rules failed | Pairs |
|---|---|
| technology and timing | 668,328 |
| technology only | 553,912 |
| timing only | 1,179 (1,155 of them the one gas notice, closed 2026-09-22) |
| none — the 20 matches | 20 |

The size-window rule is never exercised by this corpus: no opportunity row carries `capacity_sought_mw`, so
every pair passes it on the "size not stated" credit (0.5). Its branches are pinned by unit tests only.

### 21.2 The sample and the labelling rule

103 pairs, 22 proposal jurisdictions, 17 proposal technologies, 54 distinct notices, drawn by
`draw_sample(as_of=2026-09-26)` in five strata (deterministic: hash order, per-notice caps, one row per
proposal technology × jurisdiction):

| Stratum | n | What it is for |
|---|---|---|
| `predicted_match` | 20 | every pair the rules accept — a census, so precision has no sampling error from this stratum |
| `timing_only_fail` | 13 | technology and jurisdiction pass; would a looser timing rule have been right? |
| `technology_fail_same_country` | 30 | same country, technology fails — the bulk of the frame |
| `probe_same_country` | 20 | four untagged or `efficiency` notices the labeller named in advance as possibly project-eligible (ARPA-E SCALEUP READY, USDA PART, the two USDA REAP postings): where recall errors would be |
| `cross_country_same_family` | 20 | shared technology family, different country |

**Labelling rule** (a modelling choice, stated): `true` when the proposal's sponsor could plausibly apply to or
bid the notice *with this project*, judged from the notice's title, issuer and programme and the project's
technology, place, size and lifecycle state on `as_of`. A withdrawn, cancelled or built project is never
`true`; a closed notice is never `true`. Every row carries its reason in `note`.

**Split:** per notice, `tune` when the first hex digit of `sha256("<source_id>:<source_record_id>")` is even,
so every pair of one notice falls in the same half (48 tune, 55 test).

### 21.3 Results

`python -m services.match.eval --sweep`:

| Split | n | Positives | TP | FP | FN | Precision (95 % Wilson CI) | Recall (95 % Wilson CI) |
|---|---|---|---|---|---|---|---|
| tune | 48 | 0 | 0 | 5 | 0 | 0.000 (0.000–0.434) | n/a |
| **test** | **55** | **5** | **5** | **10** | **0** | **0.333 (0.152–0.583)** | **1.000 (0.566–1.000)** |
| all | 103 | 5 | 5 | 15 | 0 | 0.250 (0.112–0.469) | 1.000 (0.566–1.000) |

**US-401 AC3 (precision ≥ 0.7 at the default threshold) is not met**, on either half or on the whole sample.

**Tuning did nothing, and could not.** The tune half holds no positive pair, and every pair the rules accept
there scores 0.865, so the sweep (0.50–0.95) is 0 precision at every threshold that predicts anything. The
threshold stays at the a-priori 0.70.

**Where the errors are.** All five true matches are the DOE *Advanced Nuclear Energy Licensing Cost-Share
Grant Program* (DE-FOA-0003339, open to 2026-09-30) against the five pre-construction US nuclear
proposals (EIA-860M Kemmerer Unit 1 and the four Project Matador units). All fifteen false positives are the
three other nuclear-tagged notices: a university scholarship and fellowship programme, an NIH HAZMAT worker
training programme and a staff-support grant for regional radioactive-materials transport planning. The
technology tag is right (they are about nuclear) and the eligibility is wrong (they fund people, not
projects). No threshold separates them: they score the same as the true matches. Recall is 5 of 5, which
says little — the probe stratum found no project-eligible notice the rules missed, because the notices in
the corpus that fund deployment (REAP) are stale FY2016/FY2019 postings and the rest are research
programmes.

### 21.4 The publish gate: computed, stored, not shown

Coordinator decision, 2026-09-26: **matches from `match-rules@v1` are not shown to readers.** A precision
of 0.25 against AC3's 0.7 means three of every four matches a reader saw would be a wrong claim — "this
project is eligible for that programme" when it is not — and the errors are the confident kind (they score
exactly as high as the right ones). `data/match_rules.yaml` therefore carries `publish: false`, and absent
means false. While it is false:

- `services/match/run.py` still computes and stores every match and writes every `match_added` /
  `match_removed` event, with `public_at` and `published_at` NULL, so no event of a withheld rule set
  reaches `/v1/events`, a record timeline, `/feeds/events.*` or a webhook on any tier;
- `GET /v1/matches`, `/v1/proposals/{id}/matches` and `/v1/opportunities/{id}/matches` return an empty
  `data` with a top-level `matches_withheld` (`reason: pending_evaluation`, the rule-set version, a sentence)
  to every caller but an operator, and the by-id routes (`/v1/matches/{id}`, dismiss, undismiss) are `404`;
- operator sessions see every match, and the CRM hand-off (`POST /admin/v1/leads`, US-403) works, so the
  operations team can still review and route matches by hand.

The gate belongs to the rule set, not to the deployment: a new `rule_set_version` whose own measurement
clears AC3 is the thing that sets `publish: true`.

### 21.5 What would move the number

1. **A demand side the matcher is for.** US utility RFPs and state solicitations (the curated issuer
   registry, US-303) do not exist in data/normalized; until they do, matching measures grants.gov's
   topical tagging, not the rule set.
2. **Tag eligibility, not topic, at extraction.** The grants.gov extractor sets `technologies` from title
   keywords. A notice that funds scholarships, training or staff support should carry no technology tag
   (or a `non_project` flag the technology rule reads). This is a v2 candidate for the rule file too — an
   opportunity-title exclusion list — but it was **not** added: the tune half offers one false-positive
   notice ("Staff Support ..."), a term list derived from it would not generalise to the test half's
   scholarship and training notices, and a list derived from all four would be fitted to the labels it is
   then measured on.
3. **Independent labels**, drawn from the same frame (`draw_sample`) and a corpus with (1) in it.

### 21.6 Assumptions recorded

- **A-22-24:** an empty `opportunity.technologies` list means "not extracted", not "all-source", for
  matching. The public list filter (`GET /v1/opportunities?technologies=`) treats empty as all-source per
  `api/openapi.yaml`; the matcher does not, because on this corpus 142 of 149 empty rows are research,
  health or diplomacy programmes. An all-source solicitation is matched only when it carries `all_source`.
- **A-22-25:** matching recomputes at 2026-09-26's clock; the timing rule makes the match set date-dependent
  without any row changing. Two of the four nuclear notices close on 2026-09-30, so the 20 above drops to
  10 on the first run after that date. `run_matches` handles this by re-scoring, on every incremental run,
  each opportunity that holds an active match and whose `due_at` has passed.
- **A-22-26:** opening the gate does not retro-publish. Events written while a rule set was withheld keep
  NULL stamps for good (the event log is append-only, docs/21 §3.10); the matches themselves appear at once,
  because the match routes read the gate at request time. A timeline therefore starts showing match changes
  from the first run after the gate opens, which is the honest reading: nothing was claimed publicly before.

## 22. Resolver hardening against the wrong merges of lane H4 (lane H5, 2026-09-29)

**Status:** implemented and measured. The code is in `pipeline/resolve.py`, `services/resolve/merge.py` and
`services/resolve/report.py`. The tests are `tests/test_resolve_rules.py` (16) and
`tests/test_resolve_cluster_gate.py` (14). Lane H4 (`docs/25` §3.8) listed eight defects that let wrong
records into dev-store clusters. This section adds four pairwise vetoes (T, N, C, Q), one whole-cluster
check (K) and a cluster builder that uses loaded records only (L). It also fixes the EIA rollup (E).
Deterministic passes (D1–D3) are untouched: no veto applies to a `D` pair, and a test pins this.
Rules K and Q carry the coordinator's two loosenings of 2026-09-29 (K-v1, Q-v2), each chosen from the
out-of-sample check in §22.8.

**Every threshold in this section (70, 4×, 0.9 with ±10 %, 2.0) was set on the dev frames and the 85 eval
labels, so it is in-sample. The out-of-sample evidence is §22.8: every dev-store record whose merge the
rules changed, judged by hand.**

### 22.1 Rules

| Rule | Where | What it refuses | Constant | Why this value |
|---|---|---|---|---|
| **T** technology class | `score_pair` → `veto` | `load` or `transmission` against anything but itself; two different generation families (solar, wind, thermal, hydro, nuclear, geothermal); storage against thermal | `TECH_FAMILIES` | Storage *is* allowed with solar, wind and hydro, because hybrids file their halves separately (Harryoung, Cuchillas, Lupinus). Every storage-vs-thermal candidate scoring ≥ 70 in both corpora is two projects (2 of 2: "Montgomery Energy Storage" vs biomass "TBE-Montgomery LLC"; "MERCED POWER" vs "Merced BESS") |
| **N** name floor | `score_pair` → `veto` | a pair with no sponsor component and name < 70 | `NAME_FLOOR_NO_SPONSOR = 70` | Five accepted dev pairs had name < 70. The three with no sponsor were all wrong (Dracker/Grace 67, Franklin Park 66, Desert Bell/Desert Charger 67). The two with a sponsor were right (Harryoung 62, Alina/Anila 60) |
| **C** capacity factor | `score_pair` → `veto` | a request more than 4× the **whole** EIA plant (every generator); between two non-EIA records, a ratio above 4 either way | `CAP_VETO_FACTOR = 4` | The largest legitimate request-to-plant ratio is 2.42: CAISO 1632 "SANBORN HYBRID 3", 1,400 MW, against the 578 MW EIA plant of the same name and number. The wrong ones start at 7.1 (Somerset 706 vs 100). A request much *smaller* than the plant (a phase, or a storage add-on) is never vetoed |
| **Q** phase surplus | `phase_surplus` after scoring | an eligible pair (p, q) whose phase numbers differ, when q already has eligible partners **from p's own source** with q's phase whose MW cover ≥ 90 % of q, counted within p's technology family, **unless p completes q**: p's MW plus the partners' within ±10 % of q (Q-v2) | `PHASE_COVER = 0.9`, `CAP_TOL = 0.10` | 0.9 and ±10 % are the band of docs/02 §5. Q-v2 keeps Sunrise Wind II (880 + 44 = the 924 MW plant) and still refuses Agricola Wind 2 (97 + 79.3 against 99). A lone "1" reads as unnumbered. With a capacity missing the rule abstains |
| **K** coherence | `merge.gate_cluster` → review | a cluster where one source's requests **in one technology family, two or more of them** (K-v1), add up to more than 2× the cluster's EIA capacity in that family | `COHERENCE_FACTOR = 2.0` | Every sound dev cluster the check judges is ≤ 1.36 (Gonzaga); above it are 2.97 (Rolling Upland re-filings), 5.61 (Cody Road re-filings) and 10.51 (Riverhead). A family with a single request is rule C's job, so Briggs (one 336 MW storage request against 70.5 MW of EIA storage) now merges |
| **L** loaded-only clusters | `report.build_clusters` | a record the store did not load (a gated source, or one dev does not load) bridging two loaded records; a pipeline cluster that falls apart splits into components `<id>`, `<id>#1`, … | — | Production's `_latest_proposal_frames` feeds every implemented proposal source, gated or not, and only the loader refuses gated rows. So the builder, not the frame list, is where this belongs, and it covers both cases |
| **E** rollup on registry ids | `eia_plant_rollup`, `build_clusters` | — (a fix) | `EIA_SOURCE_IDS` | The rollup matched only the fixture's `eia860m`, so the scheduler and dev never had it. `build_clusters` now projects an edge to a rollup row onto each loaded generator it sums |

Rule K counts per technology family for three reasons. ERCOT files a hybrid as two requests. EIA-860M may list
only the storage half of a plant whose solar already runs (Duffy). A hybrid request's MW is the POI total, so it
counts once, in whichever of its families holds the most EIA capacity. Two rows of one source with the same
technology and MW count once, because NYISO lists a project under both its cluster id and its queue position
("KCE NY 30" as C24-008 and 1448). Rule K judges only what `ClusterMember` carries, and since rule L the builder
fills in MW, technology and plant id. A cluster that fails goes through the existing review path: one
`resolution_decision` per edge, nothing merged.

**Rule Q, and why the simpler forms were rejected.** Measured on the dev frames and the eval labels:

| Variant | Dev pairs this rule vetoes | Eval tp/fp/fn | Legitimate patterns broken |
|---|---|---|---|
| **Q as shipped** (coverage ≥ 0.9, within the technology family, Q-v2) | 33 | 37/1/3 | none |
| Q as first shipped (without Q-v2) | 34 | 37/1/3 | none; but refuses Sunrise Wind II (§22.8) |
| Any matching-phase partner vetoes (the brief's (b), no coverage test) | 54 | 34/1/6 | Roseland II, Vast Sands II, Mulqueeney 2, Indigo Storage 2, Darden II |
| Coverage, but blind to technology | 38 | 36/1/4 | Indigo Storage 2 |
| Adding "capacity score 0 = veto" (the brief's (c) read literally) | 139 more than shipped | 28/1/12 | Roseland II, Mulqueeney 2, Indigo Storage 2, Darden II, Rock Island |

Coverage is what separates a surplus phase from phases that add up. The other rows were measured against
the first-shipped Q. Bonanza is surplus: "BONANZA SOLAR" 300 MW
alone covers the 300 MW plant, so "BONANZA SOLAR 2" is refused. Roseland adds up: 254 + 254 MW against the
500 MW plant, so both are kept. Coverage uses the plant's `plant_poi_mw`, the largest per-technology total,
for a hybrid or unknown request, because EIA lists Bellefield 2 as 500 MW solar plus 500 MW storage behind
one 500 MW request.

**Smaller changes.** `run()` no longer raises `KeyError: 'rationale'` when blocking yields no candidate pair.
`evaluate()` now leaves vetoed and `id_conflict` pairs out of its predictions, because until now it ignored
`id_conflict`. No label is affected by `id_conflict`. `matches.parquet` gains a `veto` column, and every
`rationale` names the veto that fired.

### 22.2 Measured before and after

Before is HEAD `dd8bdf3`. After is this lane. Every number comes from a run.

**Evaluation labels** (`data/eval/labels.csv`, 85 usable, threshold 75; the store path uses the 77 labels the
store can answer):

| | Before | After |
|---|---|---|
| Resolver pairs, sample P / R (tp/fp/fn/tn) | 0.975 / 0.975 (39/1/1/44) | **0.974 / 0.925** (37/1/3/44) |
| … weighted P / R | 0.961 / 0.976 | 0.958 / 0.910 |
| Store path, `python -m services.resolve.report` (tp/fp/fn/tn) | 1.000 / 0.973 (36/0/1/40) | **1.000 / 0.892** (33/0/4/40) |
| Resolver accepted pairs / clusters | 814 / 338 | 764 / 333 |
| Store: proposals after resolution (of 9,563) / clusters merged (records absorbed) | 9,122 / 274 (441) | 9,156 / 271 (407) |
| Store: clusters refused | 3 (no direct edge) | 2 (coherence: Cody Road, Rolling Upland), 15 `resolution_decision` rows |

The false-positive count holds at 1 in the resolver: `isone:84`/`isone:84#5`, a D2 pair that is gated out of
the store. It holds at 0 through the store. The precision ratio moves by 0.001 only because two true positives
were lost. The lost labels are:

- `caiso:1797` ↔ `eia860m:66908-BZPV` and `ercot:25INR0547` ↔ `eia860m:66891-245BS`, refused by rule Q.
  **Both labels are contested (§22.7).**
- `nyiso:0520` ↔ `eia860m:62262-GEN1`, store path only. Rule K sends the whole Rolling Upland cluster to
  review: three withdrawn NYISO filings of 2009, 2015 and 2023 (59.9 + 72.6 + 79.8 MW) against one 71.4 MW
  plant. Re-filings are the open product question of §7.7 and §11. Until it is settled, they go to a person
  and are not merged.

With only the two contested labels flipped (an overlay; `labels.csv` is unchanged):

| | Before | After |
|---|---|---|
| Resolver pairs P / R (tp/fp/fn) | 0.925 / 0.974 (37/3/1) | **0.974 / 0.974** (37/1/1) |
| Store path P / R (tp/fp/fn) | 0.944 / 0.971 (34/2/1) | **1.000 / 0.943** (33/0/2) |

**Dev store** (`web/.data/dev.db` as lane H4 built it, 11,098 live proposals, data root = every source plus
the §3.7 ICIS-Air run; `default_resolve` run on a fresh copy each time):

| | Before | After |
|---|---|---|
| Live proposals after resolution | 10,524 | **10,567** |
| `merged` events | 574 | **531** |
| Loaded multi-member clusters | 416 | 407 |
| Refused: no direct edge / id reuse / coherence | 6 / 1 / – | 0 / 1 / 3 |
| `resolution_decision` rows | 1 | 22 |
| Merged clusters holding ≥ 2 requests from one source (records absorbed) | 66 (189) | 49 (128) |
| EIA plant-rollup records | 0 | 141 |
| Resolver candidate pairs / accepted pairs | 37,717 / 836 | 38,181 / 806 |
| `default_resolve` wall time (one run each) | 13.4 s | 13.4 s |

Merges by source pair. As in §3.8 of docs/25, each absorbed record is paired with the survivor's own source:

| Pair | Before | After | Δ |
|---|---|---|---|
| EIA-860M + ERCOT | 226 | 222 | −4 |
| ICIS-Air + Virginia DEQ | 138 | 138 | 0 |
| EIA-860M + EIA-860M | 82 | 75 | −7 |
| EIA-860M + NYISO | 75 | 52 | −23 |
| EIA-860M + CAISO | 52 | 44 | −8 |
| EIA-860M + ICIS-Air | 1 | 0 | −1 |
| **Total** | **574** | **531** | **−43** |

### 22.3 What each rule does (ablation)

Each rule was switched on alone on top of HEAD, and all rules were switched on with one left out. The table
shows dev-store merges, with eval changes where there are any.

| Rule | Alone | All but this one | Notes |
|---|---|---|---|
| T | 571 (−3) | 534 | Franklin Park, Pomfret (wind vs PV), Montgomery (storage vs biomass) |
| N | 572 (−2) | 532 | Dracker/Grace |
| C | 562 (−12) | 535 | Riverhead ×2, Somerset 706 MW, Little Falls, Manorville, Elevate Arthur Kill II, Sand Hill C vs A/B, Gaskell West |
| Q | 553 (−21) | 527, 10 to review | Eval −2 tp (the contested labels). Without Q, rule K refuses the phase clusters whole |
| K | 574 (0) | 544 | Needs L's member fields. Eval store −1 tp (Rolling Upland) |
| L | 573 (−1) | 540 | Bonanza's permits bridge. The 6 no-edge refusals become separate components |
| E | 574 (0) | 526 | Needs L's projection. Adds KEY STORAGE 1 ↔ Key Energy Storage (3 units) and ERCOT BasRanch ↔ CPV Basin Ranch (2 units) |
| All | **531 (−43)** | — | Eval: resolver 37/1/3, store 33/0/4 |

### 22.4 The eight H4 findings

Each outcome was checked on the after-store by testing whether the two records share a live proposal.

| # | Finding | Outcome | By |
|---|---|---|---|
| 1 | ICIS "FRANKLIN PARK (CHI22) DATA CENTER" with EIA 68135 solar | **fixed**: apart | T (N also fires) |
| 2 | CAISO 1510 into EIA "Bellefield 2"; "BONANZA SOLAR 2" into the one 300 MW plant | **fixed**: 1510 and 1797 apart; 1631 and 1649 still merged with their plants | Q |
| 3 | ERCOT 28INR0067 "Indigo Solar 3" (800 MW) in the Indigo cluster | **fixed**: apart. "Indigo solar 2" (180 MW solar, separate sponsor) is also out; Indigo Solar and Indigo Storage 1–4 stay with the plant | Q |
| 4 | Five NYISO "Riverhead" requests with EIA "Riverhead - CVE" | **fixed**: all five apart. 0762 and 1307 are refused pairwise; the rest of the cluster (51.5 MW solar against 4.9) goes to review | C, K |
| 5 | CAISO 294 "DRACKER SOLAR" into Grace Energy Center | **fixed**: apart; CAISO 1761 still merged with Grace | N |
| 6 | Transitive chaining, no whole-cluster check | **fixed** as rule K. Multi-request merged clusters 66 → 49, records in them 189 → 128. Three clusters now go to review (Riverhead, Cody Road, Rolling Upland) | K |
| 7 | Unloaded `us.permits_dashboard` bridging 7 clusters | **fixed**: 0 clusters are joined through an unloaded record. Bonanza's EIA storage generator is now its own proposal, as is every EIA generator no loaded record ties to its plant | L |
| 8 | `eia_plant_rollup` never fires on registry ids | **fixed**: 141 rollup records on the dev frames, 16 accepted rollup edges, 5 merges only they explain | E |

### 22.5 Legitimate patterns still merge

These 22 record pairs were checked on the after-store, and every one shares a proposal:

- Hybrids: Harryoung solar and BESS requests with the EIA plant; Cuchillas solar with BESS; Bellefield 2 and
  Grace Energy Center EIA solar with storage.
- Phases that add up: Roseland I and II; Mulqueeney Ranch Wind 1 and 2; Vast Sands Power I and II, each with
  its plant.
- Darden: CAISO 1949 with each of Darden I–IV.
- Rock Island: ERCOT 27INR0321 with RIG1 and RIG6, and RIG1 with RIG6.
- One-plant EIA+EIA: Darden I solar with storage; the Bellefield 2 CAISO 1631, Bonanza 1649 and Indigo Solar
  anchors.

The tests hold minimal frames for Harryoung, Rock Island, Roseland, Vast Sands, Grace and Indigo Storage 2.

### 22.6 Hand-check: 15 random merges, after

The sample is 15 of the 389 merged pairs outside ICIS/VA (those are exact shared-id matches), seed 20260930.
It was drawn from the store as first shipped (527 merges). The 4 merges K-v1 and Q-v2 add back (Briggs 3,
Sunrise Wind II 1) are judged correct in §22.8.

| # | Survivor ← absorbed | Verdict |
|---|---|---|
| 1 | EIA 68851 Hermes Solar PV ← ERCOT 23INR0344 Hermes Solar (same LLC, 100.4 MW) | correct |
| 2 | EIA 67650 Bear Ridge 93 MW ← NYISO 0704 Bear Ridge Solar 100 MW, Niagara | correct |
| 3 | EIA 68485 Rexford 2 storage ← EIA 68485 Rexford 2 solar (one plant) | correct |
| 4 | EIA 69506 Gail Mountain Solar ← ERCOT 28INR0176 (244.4 MW both) | correct |
| 5 | EIA 66805 Lycan Solar Project ← CAISO 1643 LYCAN SOLAR (400 MW, Riverside) | correct |
| 6 | EIA 67776 Cuchillas PV and BESS ← ERCOT 27INR0077 Cuchillas BESS (306.9 MW) | correct |
| 7 | EIA 70200 Lucy Solar ← ERCOT 25INR0225 Lucy Solar (same LLC) | correct |
| 8 | EIA 66896 Lupinus Solar 2 storage ← ERCOT 24INR0154 Lupinus Solar 2 | correct (numbered hybrid) |
| 9 | EIA 69636 Main Horn Solar ← ERCOT 28INR0467 (same LLC) | correct |
| 10 | EIA 69640 Cazadores Solar ← ERCOT 29INR0160 (300 / 301.6 MW, Duval) | correct |
| 11 | EIA 68349 Avant Prairie ESS ← ERCOT 27INR0316 Avant Prairie BESS (same LLC) | correct |
| 12 | EIA 69245 Nightfall Solar ← ERCOT 21INR0334 (180 / 180.9 MW, Uvalde) | correct |
| 13 | EIA 65901 Hatchery Solar ← NYISO 0932 Hatchery Solar (20 MW, Livingston) | correct |
| 14 | EIA 66390 MRG Goody Solar Project Hybrid storage ← ERCOT 24INR0305 MRG Goody Storage | correct |
| 15 | EIA 68851 Hermes Solar PV ← ERCOT 24INR0365 Hermes Storage (co-located, Bell 1 LLCs) | correct by the §6 labelling rule (co-located hybrid) |

15 of 15 are correct. H4's random ten found 0 clear errors among 7 correct, 1 likely and 2 uncertain; both of
its uncertain pairs (Rough Hat 2, South Ripley BESS) are unchanged by this lane.

### 22.7 Two contested labels (proposal, not applied)

Both labels were made on 2026-09-12 from the pair alone. Neither labeller saw the sibling request, and both
contradict the §6 labelling rule that "separately numbered phases are not" one record:

- `caiso:1797` "BONANZA SOLAR 2" ↔ EIA "Bonanza Solar and Storage Project", labelled 1. 1797 is **withdrawn**
  (COD 2024-12). CAISO 1649 "BONANZA SOLAR" is active, 300 MW, COD 2028-09, and matches the 300 MW EIA plant
  (COD 2027-12) on its own.
- `ercot:25INR0547` "Indigo solar 2" ↔ EIA 66891 storage generator, labelled 1. The request is solar, 180 MW,
  sponsor "Indigo Solar 2". The EIA plant's solar is 150 MW, which is ERCOT 21INR0031 "Indigo Solar" under the
  plant's own sponsor, Innovative Solar 245. The 180 MW match is with the plant's *storage*.

Proposed: flip both to 0 and record why in the `rationale` column. That is the owner's call, not this lane's, so
the file is unchanged and §22.2 reports both views.

### 22.8 Recall cost on the dev store (out of sample)

This is the out-of-sample evidence for the in-sample thresholds above.
- Every record whose merge changed between HEAD and this lane was listed: 48 absorbed before and not after,
  5 absorbed after and not before, and 66 once membership changes are counted.
- Each was attributed to a rule by leaving one rule out at a time, and judged from the records' own fields.
  No private aggregator or web lookup was used.
- The per-record table and a seeded sample (21 of the 59 un-merged records, seed 20261001) are in the lane's
  `recall_cost.md`.

| Rule (final) | Wrong merges removed | Correct merges lost | Uncertain | Moved to the right cluster / gained |
|---|---|---|---|---|
| T | 6 | 0 | 0 | 0 |
| N | 1 | 0 | 0 | 0 |
| C | 12 | 0 | 2 (Gaskell West) | 1 |
| Q (with Q-v2) | 15 (2 are the contested labels) | 0 | 3 (Quantum II ×2, Baldy Mesa 2) | 2 |
| K (with K-v1) | 5 (Riverhead) | **10**, all in review | 1 (Cody Road 0739) | 0 |
| L | 0 | **1** | 0 | 0 |
| E | 0 | 0 | 0 | 7 gained, all correct |
| **All** | **39** | **11** | **6** | **3 moved, 7 gained** |

**The rules as first shipped (527 merges) lost 16 correct merges.** Two changes from this check were applied:
- **K-v1** gives back Briggs (4 records).
- **Q-v2** gives back Sunrise Wind II (1 record).

Neither re-admits a wrong merge, and eval does not move.

**The 11 correct merges still lost, and where each sits:**
- **Review cluster 8525.0, Cody Road (6 records):** EIA 61592-WT1/WT2/WT3, NYISO 0131, 0180A and 1156.
  These are withdrawn re-filings of the EIA plant by its own sponsor, 40.4 MW of requests against 7.2 MW
  (x5.61). NYISO 0739 (Hecate) sits in the same cluster and is uncertain.
- **Review cluster 5402.0, Rolling Upland (4 records):** EIA 62262-GEN1, NYISO 0322, 0520 and 1549. These are
  three withdrawn filings of one project (x2.97); the eval label 0520–GEN1 is 1.
- **Separate proposal (1 record):** EIA 66908-BZES, Bonanza's storage generator. It is the same plant as its
  solar generator, which is merged with CAISO 1649. At baseline its only link was an unloaded Permitting
  Dashboard record.

The 10 in review are `resolution_decision` rows a person can accept. They wait on the re-filing decision
(A-22-H5-4; K-v2 is held for it). The separate one needs a measured same-EIA-plant link (docs/22 §7.4).

Four further sibling links were lost as collateral. Each is between two records joined only through a wrong
one:
- CVE 215–216 (in review with Riverhead)
- Manorville II solar and storage
- Elevate Arthur Kill II's two NYISO rows
- Mill Point II's two NYISO rows

**K and L still lose more correct merges than they remove wrong ones on this store.**
- K: 5 wrong removed, 10 correct in review.
- L: 0 wrong removed, 1 correct lost.

Both are kept on the coordinator's decision: K because without it Riverhead's three wrong requests merge
again, and L as a soundness rule. Of K's three review clusters, one (Riverhead) is really incoherent and two
(Cody Road, Rolling Upland) are sound re-filings.

### 22.9 Assumptions recorded

- **A-22-H5-1:** a storage request can be one project with co-located solar, wind or hydro, but not with
  thermal generation. This rests on 2 of 2 storage-thermal candidates being wrong, and there are no positive
  examples.
- **A-22-H5-2:** the thresholds (70, 4×, 0.9 with ±10 %, 2.0) were set on the dev frames and the 85 labels, so
  they are in-sample; §22.8 is the only out-of-sample evidence so far. The M-1 recalibration's 600-label held-out set (docs/00 2026-09-12) is where they get tested.
  Until then, treat the after numbers as in-sample, as §6 does.
- **A-22-H5-3:** a lone phase "1" or "I" means the same as no number. ~~Number words ("Two") and letters
  ("Sand Hill C") are not phase tokens.~~ Superseded by lane I3 (2026-09-29): number words one to ten and
  letters are phase tokens under conservative rules (`pipeline/resolve.py::phase_tokens`, `lettered_bases`).
  The eval is unchanged at threshold 75 (37/1/3 resolver, 33/0/4 store).
- **A-22-H5-4:** several withdrawn filings of one project (Rolling Upland, Cody Road) go to review, not merge,
  until the §7.7 and §11 decision on re-filings is made. K-v2 (count a source's re-filings once, at their largest
  MW) is the prepared change for that decision; by hand it would release Rolling Upland (1.12x) and keep Cody Road
  (2.76x) and Riverhead (4.9x) in review.
- **A-22-H5-5:** an accepted edge to an EIA plant-rollup row stands for an edge to each generator that row sums
  (one plant, one technology).

### 22.10 Open

- The two contested labels (§22.7).
- ~~`infra/scheduler/jobs.py::_latest_proposal_frames` does not filter on `reuse`; gated rows reach
  `pipeline.resolve.run`.~~ Corrected by lane H8 (docs/25 §3.9): `registry.status()` already marks a gated source
  `gated`, not `implemented`, so gated reuse classes never reached the resolver. The narrower gap, a
  `publication: none` source, is now closed: the frame list uses the loader's own `load_refusal`. On the dev
  store this changed nothing (531 merges before and after).
- ~~Phase words ("Attentive Energy **Two** Offshore Wind") and letter phases are not parsed.~~ Resolved by
  lane I3; the Attentive Energy 1 / Two pair is now refused.
- Ambiguous merges or refusals, left as they fall: Gaskell West (a 125 MW request against a 21.6 MW
  storage plant, refused by C), Quantum II and Baldy Mesa 2 (refused by Q, uncertain), High Bridge Battery with High Bridge Wind, South Ripley BESS, Callisto ID.
- EIA generators of one plant that no loaded record ties together stay separate proposals, as before. Bonanza's
  storage generator is now one of them.
- 10 correct merges sit in review and 1 as a separate proposal (§22.8). K-v2 waits on A-22-H5-4.
- Unchanged from §6: the Tennyson → Ulysses rename (a false negative) and `isone:84` (a D2 false positive,
  gated).

### 22.11 Commands

```
python -m services.resolve.report                     # store path on the eval pull (P/R, clusters, review)
python pipeline/resolve.py --sweep                    # resolver sweep (writes data/eval/{matches,clusters}.parquet;
                                                      # not regenerated by this lane)
python -m pytest tests/test_resolve_rules.py tests/test_resolve_cluster_gate.py -q
```

The dev-store runs used a scratch copy of the H4 pre-resolution store and data root, with
`infra.scheduler.jobs.default_resolve(factory, data_root=...)`. That is the same call `web.dev_up` makes.

### 22.12 Rule T over proposal kinds (2026-10-07)

**Defect.** Rule T judged technologies only. `us.epa.class_vi` writes its own technology token
(`co2_geologic_sequestration`), which `TECH_FAMILIES` does not list, so rule T read it as unknown and let
every pair through. With Class VI loaded into a copy of the dev store, three CO2 storage projects merged into
CAISO generation requests at 0.81 to 0.91, and the merged record took kind `generation`:

| Class VI project | Merged into | Why it scored |
|---|---|---|
| Tulare County Carbon Storage Project (Tulare County Carbon Storage Project LLC) | CAISO 857 TULARE SOLAR, 150 MW, withdrawn | name token + county |
| Sutter Decarbonization Project (Calpine California CCUS Holdings) | CAISO 379 SUTTER ENERGY CENTER, 600 MW gas, withdrawn | name token + county |
| Montezuma Carbon LLC (Montezuma NorCal Carbon Sequestration Hub) | CAISO 22 MONTEZUMA (HIGH WINDS III), then 222 MONTEZUMA II and 489 MONTEZUMA II EXPANSION through it | name token + county |

A CCS project at a power plant (Calpine's Sutter project captures that plant's CO2) is still a different
project: a well and a permit, not the plant.

**Rule.** `pipeline/resolve.py::KIND_CLASSES` maps each proposal kind (docs/02 §5) to an identity class:
`generation`, `storage` and `nuclear` are one class, `power` (hybrids file generation and storage
separately; the technology families still judge those pairs); `load`, `transmission`, `pipeline`, `lng`,
`ccs` and `hydrogen` each stand alone; `other` and a missing kind match anything. Rule T now refuses a fuzzy
pair whose kinds are in different classes (`class_compatible`, veto `veto_tech_class`), before it looks at
technology. Because `other` matches both sides, a record of kind `other` could still chain a CCS project into a
plant. `kind_chain_veto` closes that: after the pairwise vetoes it walks the accepted pairs (deterministic
passes first, never refused, then by descending score) and refuses a fuzzy pair that would put two kind
classes in one cluster (rationale `veto_kind_chain`). It is rule T over a cluster, not a new rule.

**Measured** (store with the dev sources loaded, plus Class VI, then `default_resolve`; data root
2026-10-07):

| | Before | After |
|---|---|---|
| Live proposals whose members span two kind classes | 3 (5 records absorbed across kinds) | **0** |
| Class VI records on their own proposal | 65 of 68 | **68 of 68** |
| Proposals merged | 537 | 532 |
| Proposal clusters vs the same store without Class VI | 3 differ | **identical** (404 multi-member proposals, 936 records) |
| The same store without Class VI, before vs after the change | — | identical clusters (the rule changes nothing on the existing sources) |
| Evaluation (85 labels, threshold 75) | 37/1/3/44, 764 accepted pairs | 37/1/3/44, 764 accepted pairs |

The two CAISO Montezuma requests that left the cluster were joined to each other only through the Class VI
record (one source's requests are never paired directly), so both were wrong merges. Tests:
`tests/test_resolve_rules.py` (the three real pairs with their real names, counties, MW and technologies;
the `other` bridge; the ordering of the cluster check).

**Assumption A-22-T-1:** a record of a non-power kind is never the same real-world project as a power record,
even when the two share a site and a sponsor. If the product later wants "CCS at plant X" linked to plant X,
that is a relation between two proposals, not a merge.

## 23. Field survivorship on a merged proposal (lane FS, 2026-10-07)

**Status:** implemented and measured. Code: `services/resolve/survivorship.py` (the rules), called from
`services/resolve/merge.py` (merge, unmerge), `services/ingest/loader.py::load_dataframe` (after every proposal
load), `services/resolve/report.py::apply_all_clusters` (store-wide at every resolve tick) and
`services/api/visibility.py::GatedRecord` (the served fallback). Read side: `services/api/proposal_members.py`,
`web/viewmodels.py`, `web/templates/proposal_detail.html`. Tests: `services/resolve/test_survivorship.py` (13),
`tests/test_field_survivorship.py` (9; 8 fail on `daeae11`, the ninth guards that no event is added).

**The defect** (audit 2026-09-30, data scientist F2, designer D-5). `merge_proposal` moved source links and
recomputed nothing, so the survivor (the EIA-keyed member, `choose_canonical`) kept one generator's name, MW,
status and date. Every later load of any member then wrote that member's row over the record
(`_update_existing_entity`), so the record showed whichever source loaded last. `darden-ii-solar-vdtde2` showed
342.7 MW (one EIA solar generator) against CAISO's 1,150 MW request, and the timeline credited CAISO with EIA's
"(T) Regulatory approvals received", because `web/viewmodels.py` credited `provenance[0]`. The proposal page
showed nothing of what had been merged.

### 23.1 The rules

A member is one active `proposal_source` link, read from its own `normalised` row. Its role comes from the
source's `category`: `generation_queue`/`load_queue` is a **request**, `registry` (EIA-860M) the **inventory**,
anything else **other**. A request is *live* unless its state is withdrawn/cancelled or it has left its register
(`gone_at`).

| Field | Rule | Why |
|---|---|---|
| `capacity_mw` (and `storage_mwh`) | Sum of the live requests, one source's rows with the same technology, MW and phase number counted once; plus any inventory generator of a technology family no live request covers. Else the inventory summed (the plant rollup). Else every request summed. Else the largest stated value. | The request is what is proposed at the grid: its MW is the point-of-interconnection limit. EIA nameplate counts a hybrid's halves separately (§22.1: "Bellefield 2: 500 MW solar + 500 MW storage behind one 500 MW request"; `coherence_ratio`: "its MW is the point-of-interconnection total"). So the inventory total is shown beside it, not instead of it (§23.4). The phase-aware dedupe is §22.1's NYISO reading ("KCE NY 30" as C24-008 and 1448) without collapsing equal phases (Roseland Solar and Roseland Solar II, 254 MW each). The uncovered-family term exists because ERCOT files a hybrid as two requests and may hold only one (Albatross: a 50.4 MW storage request beside EIA's 101 MW solar). |
| `technology`, `technology_raw`, `kind` | From the members that supplied capacity: one value when they agree; solar + storage is `solar_storage`, wind + storage `wind_storage`; otherwise the largest member's. | The class describes the MW printed beside it. |
| `lifecycle_state` with `status_raw` | One member supplies both: the most advanced state among members still on their register (announced < filed < studied < permitted < contracted < under_construction < built). A withdrawn member does not end a project another member shows progressing; the record is withdrawn/cancelled only when every member is. `unknown` never wins. Ties: most recent retrieval, larger member, source id, record id. | Registers lag rather than regress: an EIA-860M "(T) approvals received" row beside a CAISO request with an executed IA is the same project behind on paperwork. The audit's 20 survivors reading `announced` while ERCOT had the IA signed are this case. The conflict is not hidden: the rule is `most_advanced_of_conflicting` and the page lists every member's own status. |
| `proposed_online_date` | The lifecycle winner's date, else the next member in the same order that states one. | The status line and the date read with it come from one register (the slip note says "X still reports ..."). |
| `name_canonical` | The inventory plant with the most MW (the stored name kept when it is a tied candidate); else the largest live request's; else the stored name when a member states it; else the most recent member's. | Queue names are often codes or upper case ("DARDEN", "NY128 - Foothills Solar"). Keeping a tied stored name means a restatement does not rename a record for nothing; slugs never change. |
| `jurisdiction`, `iso` | The most common subdivision-level value (stored wins a tie); the requests' operator. | |
| `identifiers` | Union: every request's `queue_ids`; `eia_plant_id` = the plant the name came from; `eia_generator_id` only when the record holds one generator; `eia_generators` listing all when it holds several. `select_basis` and admin keys are left as stored. | The Queue ID used to render "—" on Darden. |
| `first_seen`, `location_id` (store only) | Earliest `first_seen` of the record and the rows merged into it (the store's clock; a link's `first_seen` is its source's retrieval time and is not compared). The most precise location among them. | |

A field no member states keeps its stored value. `field_provenance[field]` is `{source_id, licence_id,
retrieved_at, rule}`, plus `source_ids` when several sources supplied it (a sum, a union, a hybrid).

### 23.2 When it runs

- **Merge:** after links and overrides move. The merge event's `before.surviving.survivorship` snapshots the
  survivor's fields and provenance, and `after.surviving.survivorship` lists the fields that moved.
- **Unmerge:** the snapshot is restored, then the rule reruns when the survivor still holds more than one link.
  Exact reversal holds for the merge being undone (tested through two stacked merges).
- **A member's source changes:** after every proposal load, for each touched record with more than one active
  link. A reload of one member no longer writes its row over the record. The loader now also records each link's
  own identifiers in `normalised.identifiers`. Links stored before this read their ids from the record id
  (EIA-860M `plant-generator`, US ISO queue ids).
- **Every resolve tick:** `restate_all` over every live multi-source record. This is how a rule change reaches
  records merged earlier. On an already consistent store it is a no-op: the second pass on the measured copy
  changed 0 records and restamped 0 provenance entries.

### 23.3 With the served view and admin overrides

Survivorship decides the **stored** value. `GatedRecord` still decides what is **served**:

1. An admin override is served as made. Survivorship never writes an overridden field. Overrides carried by a
   merge (W3) are pinned before the rule runs, and unmerge gives them back.
2. A stored value is served only when **every** source its provenance names is readable at the caller's tier
   (bulk and export: and permits the shape).
3. Otherwise the value is re-derived by **the same rule over the readable links only**. With CAISO unpublished,
   the Darden fixture serves EIA's 646.4 MW plant sum, EIA's status and EIA's date, never the 1,150 MW request.
   (`tests/test_field_survivorship.py::test_a_hidden_supplier_falls_back_to_the_rule_over_readable_members`.)

`hidden_provenance_clause` (the SQL test by which whole-set surfaces, the map and grid-point totals, pick the
rows to re-read through the served view) now also matches an id listed under `source_ids`. Without that, a sum
with a hidden secondary supplier would have been totalled as stored. The visibility audit's store restatement
(`services/visibility_audit/run.py`) uses the same rule, so it does not report a summed value as a leak.

### 23.4 What the page and the API show (D-5)

`GET /v1/proposals/{id}`, and each bulk line, which keeps the detail shape, gains three parts. All three are
built from readable links only:

- `field_sources`: per served field, the sources that supplied it and the rule that chose it (`override` for
  an admin value). The timeline's status and date lines name these sources. `provenance[0]` is no longer used:
  on a record with several provenance rows and no named supplier, the page names none.
- `members`: one row per link, with that source's own name, technology, MW, MWh, state, COD and dates.
  Raw-class values, record ids and identifiers follow the provenance row's licence rule.
- `merge_history`: unreversed merges that brought a readable link. The event payload itself is never served.

The proposal page adds:

- a capacity note naming the rule: on Darden, the CAISO request, beside the 8 EIA-860M generators that total
  2,585.6 MW nameplate;
- a "What this record combines" table, one row per member, each with its own figures;
- a History list, built from the merges and the record's public change events.

### 23.5 A restatement, not news

Survivorship writes fields and no events. Change events come from each source's own snapshot diff
(`pipeline/diff.py`, per source frame), never from record-level values. So a recomputed canonical value cannot
surface as `capacity_changed`: it reclassifies what the store already held, as a status-map correction (§8.1,
FX1) and a capacity-rule correction (§8.2, W1) do. Real news about a member (a CAISO request revised from 1,150
to 1,200 MW) is still that source's own `capacity_change` with its own before and after. The record then follows
it through the load step above. `last_changed` moves forward on a record whose values moved, so `updated_since`
and bulk sync pick up the correction. The first tick after deploy therefore re-syncs the corrected records once.
The measured copy wrote 981 → 981 events.

### 23.6 Measured on a store copy

Copy of the 2026-09-30 dev store (`shots_main.db`), stamped at 0026 and upgraded to 0032. Its ERCOT links still
carried the pre-FX1 map ("Completed" → built), so they were first restated under the current map, which is what
ERCOT's next load does: 465 built → contracted, 4 built → under_construction, 2 + 4 → built. Then
`restate_all`. Script and outputs are in the lane's scratch directory; numbers are from the run.

- **Records.** 403 live records hold more than one active link: 265 generation records and 138 data-centre
  records (VA DEQ + ICIS-Air, no MW). All **265 of 265** changed at least one served field (identifiers on all
  265); 245 changed a non-identifier field. The 138 data-centre records changed no served value. All 403 had
  `first_seen` moved, by microseconds on this copy, where every row came from one load.
- **Fields (265).** `capacity_mw` 190 (150 up, 40 down; 40,017.9 → 53,480.5 MW over those 190),
  `lifecycle_state` 175, `status_raw` 176, `proposed_online_date` 167, `technology` 76, `kind` 31, `iso` 4,
  `name_canonical` 3.
- **Capacity rule used (265):** request 211, request sum 32, request plus uncovered inventory 15, inventory sum 2,
  single inventory 5.
- **Served MW against the audit's yardstick**, max(EIA plant sum, largest request), 265 records, before → after:
  under 50 % 20 → 3; 50–75 % 43 → 22; 75–99.9 % 88 → 33; exactly 100 % 114 → 180; over 100 % 0 → 27. The 63
  under 75 % become 25. They are hybrids whose single request is the POI limit while EIA lists both halves
  (Darden 1,150 vs 2,585.6; Bellefield 2 500 vs 1,000; Grace, Lycan, Purple Sage), or a live request smaller than
  the plant (Hoffman Falls Wind 2: the live NYISO request is 29.8 MW, its 102.5 MW predecessor is withdrawn).
  Over 100 % are ERCOT hybrids filed as two requests (Duffy 502.46 + 241.05 = 743.51 MW; Briggs 323.7 + 336).
  "Served" equals "stored" on this copy because every source of these records is public.
- **Lifecycle (265):** announced → studied 47, announced → contracted 28, filed → contracted 28, filed → studied
  28, under_construction → built 29, permitted → contracted 13, filed → under_construction 1, filed → built 1.
- **Darden** (`darden-ii-solar-vdtde2`):
  - before: 342.7 MW, solar, permitted, "(T) Regulatory approvals received. Not under construction", COD
    2028-03-01, no queue id;
  - after: 1,150 MW, solar_storage, contracted, "ACTIVE" (CAISO), COD 2027-07-19, queue 1949, 8 EIA generators
    across plants 69661–69664 listed;
  - name unchanged.
- **Sanborn Hybrid 3:** 55 → 1,400 MW, solar_storage. Its status stays under construction, now from EIA's
  largest generator.
- **Attribution.** After the restatement, 91 of 265 generation records have a status supplier that is not
  `provenance[0]`; on each of them the old page named the wrong register. (The 138 data-centre records differ
  too, but both of their sources say "Operating".)
- **Grid points:** the public active MW of 186 of 4,730 points moves. Those 186 total 62,900.7 → 68,103.6 MW;
  all points together 1,077,944.1 → 1,083,147.0 MW (+0.48 %). Largest moves:
  - Windhub 500 kV: 405 → 1,750;
  - Manning-Midway 500 kV, Darden's point: 342.7 → 1,150;
  - Riverton–Sand Lake tap: 722.5 → 1,350;
  - Elm Creek–Old Hickory tap: 600 → 1,202.02;
  - Hillje 345 kV: 1,150.14 → 550.14, because Danish Fields Solar is built per ERCOT's synchronisation
    milestone and leaves "active".
- **Events:** 981 before, 981 after.
- **Request dedupe:** 7 groups of one source's same-technology, same-MW requests sit on one record. 4 count once
  (KCE NY 30, KCE NY 31, Foothills, Moonlight Flats). 3 keep every request (Roseland Solar / II, Indigo Storage
  1–4, Vast Sands Power I / II).

### 23.7 Assumptions and open items

- A-23-1: registers lag rather than regress, so the most advanced state wins. A stale EIA "operating" against a
  request withdrawn for a *different* phase would read built. The member table shows both.
- A-23-2: a live request's MW is the project's capacity even when the plant inventory is larger. A wrong merge
  of a phase-2 request with a phase-1 plant (Rough Hat: CAISO "ROUGH HAT 2" 200 MW with EIA "Rough Hat"
  400 MW) shows the request's MW under the plant's name. That is a resolver question (§22, rule Q), not a
  survivorship one.
- Open: a record holding several EIA plants (Darden I–IV) is named after one of them ("Darden II Solar"). A
  project-level name would need the request's name cleaned or an admin override.
- Open: the web page fetches the detail route a second time (the slug lookup goes through the list, which does
  not carry `members`). `proposal_connection` makes the same call; the two could share it.
- Open: the store-wide restatement runs at every resolve tick over all multi-source records (403 here: 1.4 s for
  the first pass, 0.9 s for a no-op pass on the SQLite copy, with absorbed rows read in one query). If that
  grows, scope it to records touched since the last tick.
