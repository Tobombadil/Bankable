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
| ERCOT | Active | studied | base |
| | Active + Approved for Energization (2) | under_construction | `ercot.energized` |
| | Completed | built | 580/580 coincide with a signed IA date |
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

Result, 14,388 rows: withdrawn 7,889 · studied 1,815 · built 1,674 · under_construction 1,022 · announced 688 ·
contracted 580 · filed 395 · permitted 242 · cancelled 47 · unknown 36. `status_conflict` = 325 rows.

The one number that matters most: **327 SPP rows (49.5% of what gridstatus calls "Completed" for SPP) have an
executed IA but no commercial operation**. Any product that reuses gridstatus's status as-is overstates SPP
completions by half.

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
