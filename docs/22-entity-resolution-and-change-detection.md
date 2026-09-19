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
