# 24 — Asset identity and provenance: measured duplication, and what to do about it

**Status:** measurement and recommendation. No code, no migration, nothing shipped by this document.
**Written:** 2026-09-22, data-scientist lane, at the coordinator's request after the Tallgrass Trailblazer
ethanol query returned the wrong answer.
**Reads:** `docs/02` §5 (resolution keys), `docs/01` §3.3, `docs/21` §3.22–§3.23 and §5.4, `docs/04` D-9 and E-7,
`docs/22` §13 (reversible merges), ADR 0008.

---

## 0. The answer in one page

The duplication is real, it is **worse than the coordinator measured**, and it is **confined to one asset type**.

| Asset type | Rows | Sources | Cross-source duplication | Within-source redundancy |
|---|---|---|---|---|
| `ethanol_plant` | 388 | 2 | **187 links, 201 distinct plants, 48.2 % of rows redundant** | none |
| `rng_project` | 1,851 | 2 | **0 measured** (LMOP = landfills, AgSTAR = farm digesters; disjoint populations) | **575 of 1,353 LMOP rows (42.5 %) are an expansion vintage of a landfill already present** |
| `power_plant` | 14,659 | **1** | **structurally zero** | zero: 14,659 distinct EIA plant codes, 14,657 distinct (name, state) |
| `gas_processing_plant` | 478 | 1 | structurally zero | ≤ 6 rows (1.3 %) |
| `gas_storage` | 412 | 1 | structurally zero | ≤ 18 rows (4.4 %) |
| `gas_pipeline` | 259 | 1 | structurally zero | ≤ 3 rows (1.2 %) |
| `lng_terminal` | 8 | 1 | structurally zero | zero |

The premise "this is probably not ethanol-only" is **wrong**, and it is wrong for a structural reason worth
stating plainly: **five of the seven asset types have exactly one contributing source each**, so cross-source
duplication is not merely absent, it is currently impossible. `power_plant`, the row the coordinator most
wanted a number for, is one source (`us.eia.860m`) keyed on the EIA plant code, and its duplication is **0 %**.

That does not make the schema gap harmless. It makes it a **gap that has been hit exactly once so far, by the
first asset type to acquire a second source** — and the asset layer is supposed to keep acquiring sources.
Every future second source lands on the same wall.

**Recommendation: option (a), an `asset_source` link table carrying per-source `normalised` fields, built to the
proposal pattern — but not this sprint.** Ship (d) now, in the same week, because the coverage statement is
currently silent about a 48 % duplication rate on a live asset type and that is the part that misleads a reader
today. Do the field corrections in §7, which need no schema change. Then do (a) as its own change with its own
backfill and its own evaluation. Confidence: **high** on the measurements, **moderate-to-high** on the
recommendation. §6.5 says what would change my mind.

---

## 1. Method and inputs

Every number below was produced by me against `web/.data/dev.db`, opened read-only
(`sqlite3.connect('file:…?mode=ro', uri=True)`), on 2026-09-22. The database was not modified. Analysis scripts
were scratch-only and are not checked in; each figure states the method that produced it, and the SQL for the
simple counts is reproducible from the description.

**One caveat on the database itself, which the coordinator should know.** `web/.data/dev.db` has **no
`alembic_version` table** and its `source` table has **no `vintage` / `vintage_basis` columns**. It therefore
predates migration `0018_source_vintage` — the vintage labelling the brief refers to as shipped in PR #11 is in
the code but not in this database. The `asset` and `asset_owner` tables *do* match `services/db/models.py`
(including `geom_line`, migration 0013), so every asset-level number here is sound; but no statement about
source vintage can be measured from this file, and I make none.

---

## 2. Ethanol: re-derived independently

### 2.1 The coordinator's figures, corrected

| | Coordinator | Measured here |
|---|---|---|
| Rows | 388 (197 Atlas all with `geom`, 191 capacity none with `geom`) | **confirmed exactly** |
| Duplicate pairs | ~158 | **187 cross-source links** |
| Share of rows | ~41 % | **48.2 % redundant; 96.1 % of rows (373/388) have a cross-source twin** |
| Distinct plants | ~227 | **201** |

The coordinator's normalisation **under-counted by 29 links**. The cause is diagnosable and is the single most
useful thing in this section: a normalise-and-compare-names method fails on exactly the rows where the two
sources are most valuable, namely **the ones where the owner changed**. "Flint Hills Resources Fairmont LLC" and
"Poet Biorefining-Fairmont" share no token after normalisation; the name is the *least* stable field across
these two sources, and a method that weights it heavily will systematically miss the ownership-change cases that
are the whole reason to fuse the sources.

### 2.2 What the deterministic key already shows, before any fuzzy matching

- **100 of 388 rows** share an identical derived `source_asset_id` across the two sources
  (`{ST}-{company-city-slug}`). The `UniqueConstraint("source_id", "source_asset_id")` permits this because the
  `source_id` differs.
- **100 ethanol rows carry a `-N` disambiguating suffix on `slug`.** The loader has already detected every one
  of these collisions and resolved each by writing a second row with a suffixed slug. The duplication is not
  hidden from the system; it is recorded in the slug column and then discarded.
- Only **75** of those 100 have byte-identical `name`. The other 25 differ in case or punctuation only.

### 2.3 Scored matching and hand adjudication

Blocking on `state_code`. Features per candidate pair: company-token Jaccard (three variants: legal forms
stripped, generic industry words also stripped, and a character-level `SequenceMatcher` on the full company
string — the max of the three is used), city-string similarity parsed out of the `Company (City, ST)` name
pattern, and a nameplate-capacity ratio binned at 0.90 / 0.75 / 0.60. Weights 0.35 name / 0.35 city / 0.30
capacity; threshold 0.30. Assignment is **global greedy** (all candidate pairs sorted by score, each row
consumed once) rather than per-row greedy, which by itself fixed two mis-assignments that a row-at-a-time pass
made.

That produced **187 candidate links**, which I then adjudicated by hand, one at a time, using public knowledge
of the US fuel-ethanol industry.

- **Precision: 183 / 187 = 97.9 %.** Four are wrong, and all four are instructive: `Green Plains Ord LLC (Ord,
  NE)` ↔ `Green Plains York LLC (York, NE)` (same owner, two different plants); `Elkhorn Valley Ethanol
  (Elkhorn, NE)` ↔ `Sandhills Renewable Energy LLC (Atkinson, NE)`; `Pacific Ethanol Aurora East LLC` ↔ `CIE
  Norfolk GNS LLC`; `Ingredion Inc (Ingredion, IA)` ↔ `Verbio North America LLC (Nevada, IA)`. Three of the four
  are **a shared owner plus a capacity coincidence with no shared location** — the exact failure mode the
  `docs/02` §5 key ordering predicts when county is unavailable.
- **Recall: 183 / 187 = 97.9 %** against a gold set of 187 true links (the 183 correct, plus four the scorer
  missed: `Green Plains Ord` ↔ `Green America Biofuels Ord`, `Elkhorn Valley Ethanol` ↔ `CIE Norfolk GNS`
  (Elkhorn Valley's plant is at Norfolk NE), `Green Plains Atkinson` ↔ `Sandhills Renewable Energy`, and the
  second leg of the Pekin split below).
- **Entity count: 201 distinct plants** — 185 two-row clusters, one three-row cluster, 15 singletons. 388 − 201
  = **187 redundant rows, 48.2 %**.
- **Sensitivity.** Six links rest on industry knowledge rather than on the data (`Hereford Renewable Energy` ↔
  `Hereford Ethanol Partners`; `Aemetis Keyes` ↔ `Aemetis Ceres`; `Corn Plus` ↔ `Greenfield Global Corn Plus`;
  `Pro Corn` ↔ `Poet Preston`; both ADM dry mills ↔ `VCP Corn Processing`; `Sioux River Ethanol` ↔ `Poet
  Hudson`). If all six were wrong the count is 207 distinct plants and 46.6 % redundancy. **The floor is 46 %.**

Trailblazer corridor states specifically: **NE + CO + KS hold 80 ethanol rows, 35 cross-source links, 45
distinct plants.** Nebraska alone: 49 rows, 20 links, **29 distinct plants**. Any count returned from these rows
without resolution over-states Nebraska's ethanol plant count by 69 %.

### 2.4 Cardinality is not always 1:1 — which matters for the schema choice

Pekin, IL: Atlas holds `Pacific Ethanol Pekin LLC` (153 MMgal) as one row; the capacity report holds
`Alto Pekin LLC Dry Mill` (63) and `Alto Pekin LLC Wet Mill` (100) as two. One Atlas row is evidenced by two
capacity-report rows.

This is a 1:*n* relation between a real asset and its source records, and it is the reason a **link table**
(option (a)) fits and a **one-to-one canonical pointer** (option (b)) does not: `merged_into_id` on `asset`
models "row X is really row Y", which cannot express "these two registry rows are two halves of one plant" any
better than it can express the reverse. `proposal_source` already carries this shape.

---

## 3. Every other asset type

### 3.1 `power_plant` — 14,659 rows, one source, no duplication

`us.eia.860m` is the only contributing source. `source_asset_id` holds the **EIA plant code** (`'1'`, `'2'`,
`'3'`, …): 14,659 rows, **14,659 distinct codes**. Distinct `(lower(name), state_code)` = 14,657; the two
repeats are `Dudley` in MA and `Viola` in WI, which are different plants at different coordinates in different
places with the same town name. **Duplication estimate: 0 %.**

466 rows sit in 212 **shared-coordinate** clusters. I inspected the largest six. They are not duplicates; they
are distinct EIA plant codes at one site — `Baltimore City B / D / F / G` (four separate 2 MW municipal solar
registrations at one address), `RE McKenzie 1…6 LLC` (six 5 MW CA solar entities), `Pleinmont Solar 1` and
`Pleinmont Solar 2` alongside `Richmond Spider Solar` and `Highlander Solar Energy Station 1` at one Virginia
site. Collapsing them would be *wrong*: EIA treats each as a plant, each has its own capacity and technology.

They do create a distinct, milder user-facing problem — "how many plants near X" counts registration phases, not
sites — but that is a **presentation** question (cluster by coordinate in the geo response), not a provenance
one, and it does not need a schema change.

### 3.2 `rng_project` — 1,851 rows, two sources, no *cross-source* duplication

This is the other two-source type, so I tested it the same way. **Zero of 498 AgSTAR rows** have a same-county
LMOP row at name-Jaccard ≥ 0.5. The blocking was not the limitation: **220 of the 498 AgSTAR rows sit in a
county that does contain an LMOP row**, and none of those matched. The two registers describe disjoint
populations — EPA LMOP covers landfill-gas projects, EPA AgSTAR covers livestock anaerobic digesters. AgSTAR's
498 rows are 498 distinct `(name, state)`. **Cross-source duplication: 0 %.**

**Within LMOP there is a real and larger problem, of a different kind.** 1,353 LMOP rows resolve to:

- **989** distinct LMOP project ids once the `-N` expansion suffix is stripped from `source_asset_id`;
- **778** distinct `(landfill name, coordinate)` pairs;
- **747** distinct coordinates (14 rows have none).

924 rows sit in 332 shared-coordinate clusters, up to **nine rows at one landfill**: `East Duval SLF` carries
`Project #1` plus `De-Expansion #1` through `#8`, all `retired`, capacities stepping 1.32 → 0.25 mmscfd.
`Dane County LF #2-Rodefeld` carries eight rows spanning three projects and their expansions, mixed retired and
operating.

This is **granularity, not duplication**: each row is a genuine, separately-reported LMOP record. But at the
landfill level, **575 of 1,353 rows (42.5 %) are an expansion vintage of a site already present**. A user asking
"RNG projects near here" gets the site counted up to nine times. Option (a) does not fix this — it is one source
disagreeing with itself about what a row is — and it should not be conflated with the ethanol problem. It is a
**normalisation** decision at ingest (does an asset row mean a site, a project, or a project vintage?), and
ADR 0008 does not settle it.

### 3.3 The single-source midstream types

| Type | Rows | Distinct `(name, state)` | Rows in shared-coordinate clusters |
|---|---|---|---|
| `gas_processing_plant` | 478 | 475 | 12 in 6 clusters |
| `gas_storage` | 412 | 394 | 11 in 3 clusters |
| `gas_pipeline` | 259 | 256 | 2 in 1 cluster |
| `lng_terminal` | 8 | 8 | 0 |

Upper bound on within-source redundancy if every repeated name and every shared coordinate were a duplicate
(they are not — `gas_storage` name repeats are typically distinct fields operated under one name): 6, 18, 3, 0
rows respectively, i.e. **1.2 %–4.4 %**. Not material, and not worth a schema change on its own.

---

## 4. What is structurally missing, precisely

The brief says `asset` "carries a single `source_id`" and there is "no `asset_source` link table". True, but
understated. Comparing `asset` with `proposal` in the live schema, `asset` is missing **five** things, not one:

| Capability | `proposal` | `asset` |
|---|---|---|
| Many sources per record | `proposal_source` (with `raw`, `normalised`, `link_method`, `link_confidence`, `link_event_id`, `active`) | — |
| Strictest licence across sources | `min_reuse_class` | — (single `licence_id`) |
| Per-field provenance | `field_provenance` | — |
| Number of corroborating sources | `source_count`, `resolution_confidence` | — |
| Reversible canonical pointer | `merged_into_id` | — |

Note that `proposal` has **no `source_id` / `source_url` / `retrieved_at` / `licence_id` columns at all** — the
provenance quartet CLAUDE.md requires lives entirely on `proposal_source`, one row per source, and `proposal`
carries `min_reuse_class` as the licence gate. **That is the shape CLAUDE.md's guardrail actually takes when a
record has more than one source**, and it is already proven in this codebase. Any claim that merging assets
"would destroy provenance" assumes the fields must stay inlined on `asset`; the proposal pattern shows they
need not.

Two further facts the brief did not mention, both relevant:

- **`resolution_decision` cannot hold an asset pair.** Both `left_proposal_id` and `right_proposal_id` are
  `NOT NULL` foreign keys to `proposal`, with `UNIQUE(left_proposal_id, right_proposal_id)`. The merge-review
  queue and the `services/resolve/` merge/unmerge machinery described in `docs/22` §13 are proposal-shaped. The
  table is empty (0 rows).
- **`asset` has no `location_id`, no `precision`, no `precision_reason`.** It stores `geom` directly. There is
  nowhere on an asset row to say "this coordinate is a centroid". See §8.

---

## 5. The options

### (a) `asset_source` link table, mirroring `proposal_source`

**What it costs.** A migration creating `asset_source` (`asset_id`, `source_id`, `source_record_id`,
`source_url`, `retrieved_at`, `licence_id`, `raw`, `normalised`, `link_method`, `link_confidence`,
`link_event_id`, `active`, plus the partial unique index on `(source_id, source_record_id) WHERE active` that
`proposal_source` already uses, and the index on `asset_id` that `proposal_source`'s own comment records as
worth 14–75 s per page). A backfill writing one `asset_source` row per existing `asset` row. `min_reuse_class`
on `asset`. A resolution pass to write the links. Re-pointing `asset_owner` rows onto surviving assets.
Redirects so an absorbed row's `public_id` and `slug` keep resolving.

**What it breaks, and the coverage-gate question specifically.** `asset_visibility_filter` would change its
source clause from "this asset's `source_id` is in a permitted `publish_state`" to "**some active** source of
this asset is". That is a genuine widening of the predicate's meaning and deserves to be argued, not assumed.

But the *mechanical* gate risk in the brief is overstated, and I checked it rather than repeating it. The
generic helper already exists:

```python
def _has_permitted_source(link_model, fk, entitlement) -> ColumnElement[bool]:
```

It is a single `exists(...)` expression with **no branches**, already used by the proposal and opportunity
filters, already inside the 100 % gate. `asset_visibility_filter` itself contains no conditional today. CI
measures `coverage report --include="services/api/visibility.py" --fail-under=100`, and the workflow comment
records the module as *45 statements, 2 branches, nothing excluded*. Adding an `asset_source` arm adds
statements and a call site, **not branches**; any test that calls `asset_visibility_filter` covers them. The
risk in option (a) is the **semantic** one — the predicate starts meaning something different, and a
restricted-source asset could become visible because an open source also evidences it. That is exactly the case
`min_reuse_class` exists to handle on proposals, and it must be carried across with the link table, not after
it. Every source in scope today (`us.eia.*`, `us.epa.*`) is `reuse_class = 'open'` with
`allows_raw_publication = 1`, so nothing is actually gated right now — which means this is the cheap moment to
get the predicate right, and the worst moment to rely on testing it against live data.

**What it gives that nothing else does.** `asset_source.normalised` is per-source field provenance. With it,
"Atlas gave the coordinate, the capacity report gave the owner and the nameplate" is a stored fact, not a
convention. That kills option (c)'s premise.

### (b) Canonical pointer, `asset.merged_into_id`

**What it costs.** One nullable self-referencing column plus an index; the cheapest migration of the four. It
reuses a pattern this schema already has in three places (`organization`, `proposal`, `opportunity`) and a read
filter the code already spells in five files (`Organization.merged_into_id.is_(None)`). Crucially, **every row
keeps its own complete provenance quartet** — nothing is destroyed, and `docs/22` §13's exact-round-trip
unmerge proof transfers.

**What it breaks.** Every asset read path in `services/api/assets.py` (list, detail, geo index *and its cache
key*, nearby, organisation assets, the plants alias) needs the `IS NULL` clause; miss one and duplicates
reappear on that surface only. `asset_owner` edges are keyed on `asset_id` and must be re-pointed or read
through the pointer. It does not represent the Pekin 1:2 case. And on its own it **does not fix the wrong
data**: the surviving row still shows one source's fields, so if Atlas survives, the stale owner survives with
it. It fixes counting, not correctness.

### (c) One canonical source per asset type, enrich fields from the others

**What it costs.** No migration. A per-type precedence table in the loader, and field-level merge rules.

**What it breaks.** The brief names the objection and it is decisive: **`asset` has no per-field provenance.**
Writing the capacity report's owner onto a row stamped `source_id = 'us.eia.atlas.ethanol_plants'`,
`source_url = …Ethanol_Plants_US_EIA.zip` produces a row that **cites the wrong source for the field it is
showing**. That is not a tidiness complaint; the attribution rendering in CLAUDE.md's guardrail would print a
false citation. Option (c) is only safe *after* option (a) supplies `normalised` per source — at which point it
is the field-selection policy layered on (a), not an alternative to it.

### (d) Do nothing structural; surface the duplication in the coverage statement

**What it costs.** A `coverage.py` fact and a note in `data/vocabulary/coverage_notes.yaml`. The mechanism is
already built for exactly this: `_applicable_notes` drops a note automatically when its fact stops holding, so
a duplication note disappears by itself once the duplication does. No migration, no visibility change, no
backfill, no risk to the gate module.

**What it breaks.** Nothing — and that is also its limit. The ethanol query still returns 49 Nebraska rows for
29 Nebraska plants. It converts a silent error into a disclosed one.

---

## 6. Recommendation

### 6.1 Ship (d) this week

`coverage.py` today publishes vintage, withheld sources and technology absence. It says nothing about the fact
that one live asset type is **48 % redundant**. A reader who trusts the coverage statement is being misled by
omission right now, and that is fixable in a day with no schema risk. The facts to publish are measurable
straight from the table: rows per `asset_type`, contributing sources per `asset_type`, and — for any type with
more than one source — the measured redundancy with its method named. The note mechanism will retire the note
when (a) lands.

### 6.2 Do the §7 field corrections alongside it

They need no schema change and they fix the part of the Trailblazer answer that was *wrong* rather than merely
*doubled*.

### 6.3 Then do (a), as its own change, with its own evaluation

`asset_source` with `raw` and `normalised`, plus `min_reuse_class` on `asset`, plus widening
`resolution_decision` to carry a subject type rather than two proposal FKs. Build it to the proposal pattern
because the proposal pattern is proven here, its performance trap is documented (the `asset_id` index), its
reversibility is proven by an exact round-trip test, and the visibility helper it needs is already written and
already at 100 %.

**Do not ship the link table and the resolution pass in one change.** Land the table, backfill it one-to-one
(every asset gets exactly one `asset_source`, nothing merges, nothing user-visible changes), and move the
visibility predicate onto it under the gate. Only then run resolution, which is where the precision/recall risk
actually lives. A regression in the first step is a schema bug; a regression in the second is a wrong merge, and
mixing them makes both harder to diagnose.

### 6.4 Reject (b) and (c) as the primary answer

(b) is genuinely cheaper and genuinely reversible, and if the decision were "duplication is only ever going to
be ethanol, and we need it fixed on Tuesday", (b) is the right call. I do not believe that premise: the asset
layer's stated direction is more registers, and the second source is the one that creates the problem. (b) also
cannot express Pekin, and it leaves the stale-owner defect untouched. (c) is a policy that (a) enables, not an
option at the same level.

### 6.5 Confidence, and what would change my mind

**High** on §2 and §3 — they are counts and a hand-adjudicated sample, and the 46 % floor holds even if every
judgement call I made is wrong. **Moderate-to-high** on the recommendation. What would change it:

- **If the asset layer is not going to acquire more multi-source types.** (a)'s value is almost entirely
  forward-looking; one type at 388 rows does not justify it. If the roadmap says the asset layer is
  feature-complete at seven types and seven-plus-two sources, take (b) and move on.
- **If `services/api/assets.py` turns out to have read paths that cannot take a join.** The geo index is cached
  on the visible set and keyed on it; if that cache key cannot accommodate a link-table join without a
  measurable latency regression, (b)'s per-row filter is cheaper and the trade changes. I did not measure
  query latency — that is a backend-developer measurement and I am not in that file area.
- **If a restricted-licence asset source is imminent.** Then the `min_reuse_class` semantics in (a) must be
  designed and tested against a real case first, and shipping (d) plus (b) while that is worked out is the safer
  order.
- **If `resolution_decision` cannot be widened cheaply.** Merges with no review queue and no reversible event
  would violate `docs/02` §5's "every merge is recorded as an event and reversible". If widening that table is
  expensive, the sequencing changes — the queue comes before the merges, not with them.

---

## 7. What is safe to do now, with no schema change

### 7.1 The stale owner is measured, and the fix is already in the schema

**Measured:** across the 183 hand-confirmed pairs, the two sources disagree on the operator name, after
stripping legal forms and normalising punctuation, on **40 pairs — 21.9 %**. The four examples in the brief are
all confirmed exactly as stated (Fairmont NE 127/128, Aurora NE 110/109.5, Columbus NE 313/313, Atkinson NE
47/52.5), and the direction of staleness is confirmed: in each, the capacity-report name is the current owner.

**The important finding: the current owner is already in the database.** `asset_owner` holds **197 `operator`
edges from the Atlas source and 191 from the capacity report** — every one of the 388 ethanol assets has an
ownership edge, and both sources' views are already stored with their own `source_id`, `source_url`,
`retrieved_at` and `licence_id`. The coordinator's instinct in §4 of the brief is right, and stronger than
stated: this is not "`asset_owner` may be the right place" — **it already holds the data, correctly attributed,
today.** What is stale is the denormalised `asset.operator_name` copy on the Atlas row.

So the safe correction is: **stop reading `asset.operator_name` as current ownership on the asset surfaces, and
read the `asset_owner` edges instead, most recent source first.** No migration. It is a read-path change in
`services/api/assets.py` and `serialize.py` (another lane's file area this week).

**One gap to close in the same pass, and it is the blocker:** `asset_owner.as_of` is **NULL on all 388 ethanol
edges** (and on 3,217 of 6,336 edges overall — only the EIA-860 Schedule 4 load populates it). Without `as_of`
there is no stored basis for preferring one edge over the other; "most recent source" would have to be inferred
from `retrieved_at`, which records when *we* fetched, not when the ownership was true. `as_of` is an existing
nullable column, so populating it from each source's stated reference date is a loader change, not a migration —
and it should be done before, not after, anything starts ranking owner edges.

### 7.2 Nameplate capacity: the sources disagree more than the owner does

**79 of 183 confirmed pairs (43.2 %) disagree on nameplate by more than 5 %.** The capacity report is higher on
95, lower on 14, equal on 74. The pattern (systematically higher, with the Atlas figure often a round number) is
consistent with the Atlas layer carrying an older nameplate and the annual capacity report carrying the current
one — but I have not verified that against EIA's own documentation and it is **not** safe to assert as a rule.
Fourteen pairs go the other way (e.g. Poet Fostoria OH: Atlas 109, capacity report 90). Record the disagreement;
do not silently prefer one.

### 7.3 Status is wrong on both sources, and this is the cheapest real correction available

**All 388 ethanol rows carry `status = 'operating'`, from both sources.** That includes `Attis Ethanol Fulton
LLC` and `Tyton NC Biofuels LLC` — neither of which ever reached commercial operation — plus `Pinal Energy LLC`
(AZ) and `Agri-Energy LLC` (Luverne, MN). The vocabulary supports `retired` and `standby`; the LMOP loader uses
it correctly (its rows include `retired`). The ethanol loaders simply default everything to `operating`.

There is a usable signal for this that needs no new source. EIA's capacity report lists plants **operating as of
1 January**; presence in it is evidence of current operation, and absence is a prompt to check. The asymmetry
falls out of the resolution already done here:

- **11 Atlas-only rows** — plants the current capacity report does not list. These are the closure candidates
  and they are a hand-checkable list: Pinal Energy (AZ), Pacific Ethanol Madera and Stockton (CA), Ese Alcohol
  (KS), Valero Riga (MI), Agri-Energy Luverne (MN), Tyton NC Biofuels (NC), Alten LLC and Pacific Ethanol Aurora
  East (NE), Attis Ethanol Fulton (NY), Ingredion (IA).
- **4 capacity-report-only rows** — plants the 2020-vintage Atlas layer never had: Verbio North America
  (Nevada, IA), Green Plains York (NE), Poet Biorefining-Cloverdale (IN), Crysalis Biosciences (Sauget, IL).
  **All four are invisible to any map or radius query**, because they have no coordinate. One of them, Green
  Plains York NE, is in the Trailblazer corridor. This is a second, independent way the original answer was
  wrong, and it is not fixed by de-duplication.

Setting a status other than `operating` is a **data correction, not a schema change** — but it is a claim about
the real world that no source in the database states, so it needs a cited basis per row, not a bulk update. The
honest interim, which I recommend: leave `status` alone and publish the 11-row list as a known-uncertainty note
under (d).

---

## 8. Geocoding the capacity-report rows: assessed, not built

### 8.1 The input is better than expected

All **191/191** capacity-report names parse as `Company (City, ST)`. A token-overlap test flags 73 of 191 as
possibly repeating the company rather than naming a place, but inspection shows that test is mostly firing on
companies named after their town (Sterling, Yuma, Cedar Rapids, Clinton, Shenandoah, Arthur, Corning) — these
**are** cities. **For `us.eia.ethanol_capacity` the parenthetical is EIA's own `City` column and is a genuine
city in every row I inspected.**

The Atlas source is the opposite and should not be geocoded this way: 96 of 197 of its parentheticals share a
token with the company, and inspection shows many are facility names, not places — `Keyes Plant`,
`Cedar Rapids Dry Mill`, `Elite Octane LLC`, `Cargill Corn Milling Ft Dodge`. Atlas rows already have exact
coordinates, so this is moot, but it is a trap for anyone reusing the parser.

Known spelling defects in the capacity-report city column, found by inspection: `Bringham Lake` (Bingham Lake,
MN), `Fairibault` (Faribault, MN), `Steamboat` (Steamboat Rock, IA), `Ft Dodge` / `Fort Dodge`. Observed rate
~1.6 %, which is a floor, not an estimate — the true exact-match miss rate against a gazetteer is unmeasured
and is the first thing to measure.

### 8.2 What it would take

`services/ingest/geocode.py` has `CountyGazetteer` (US Census Gazetteer 2024 counties, public domain, vendored
as a TSV under `services/ingest/data/`) and a GB `SettlementGazetteer`. **There is no US place gazetteer.** The
missing input is the Census **National Places Gazetteer** — same publisher, same public-domain status, same
file shape (`GEOID`, `NAME`, `USPS`, `INTPTLAT`, `INTPTLONG`), ~30k rows. Vendoring it follows a pattern this
module already has twice. The work is: vendor the file, write a `PlaceGazetteer` keyed on `(state, normalised
place name)`, reuse `normalize_settlement_name`, and handle the ambiguity case the GB gazetteer already models
(`is_ambiguous` — repeated place names within one state).

That is perhaps a day. **It is not the hard part.**

### 8.3 The hard part: there is nowhere to record that the point is a centroid

This is the finding that should govern the decision.

- `location` has `precision` and `precision_reason`, with the vocabulary
  `exact | county_centroid | state_centroid | country_centroid | unknown`.
- **`asset` has neither.** It has no `location_id` either. It stores `geom` as a bare coordinate and nothing
  else. `asset_geometry_permitted` gates whether the coordinate may be *served*; nothing describes what the
  coordinate *is*.

So writing a city centroid into `asset.geom` would produce a row **indistinguishable at every API boundary from
an exact EIA Atlas plant coordinate** — same column, same type, same gate, same rendering. It would appear on
the map as a pin on a town centre that a reader would reasonably take for the plant. `docs/04` D-9 governs
exactly this ("never render an exact point… the county centroid is used and the drawer says 'location shown at
county level'"), and the `asset` table cannot satisfy D-9 because it cannot express the distinction. This is the
class of error the brief correctly identifies, and the schema currently makes it unavoidable rather than merely
easy.

Second, smaller problem: the precision vocabulary has **no `place_centroid` value**. A city centroid is much
tighter than a county centroid and materially looser than exact. The GB settlement path resolves this by
stamping a settlement hit as `county_centroid`, which understates its precision but never overstates it — a
defensible convention, and the one to copy rather than widening the CHECK constraint without a decision.

### 8.4 Recommendation on geocoding

**Do not geocode the capacity-report rows into `asset.geom`** under the current schema, at any precision, for
any tier. The right sequence is:

1. Resolve the duplicates (§6). **187 of the 191 capacity-report rows already have an exact coordinate** from
   their Atlas twin. Resolution delivers ~98 % of the geocoding benefit and delivers it at `exact` precision
   from a real source. Geocoding first would fabricate 187 approximate points where exact ones already exist in
   the same table.
2. That leaves **four** rows genuinely without a coordinate. Four rows do not justify a gazetteer. They justify
   a hand-checked coordinate with a cited basis, or being left unplaced and disclosed under (d).
3. If a US place gazetteer is wanted for other reasons — and there are good ones, notably that the capacity
   report is annual and will keep introducing new plants before the Atlas layer sees them — then give `asset` a
   precision field, or a `location_id`, **in the same change that introduces the first non-exact asset
   coordinate.** Not after.

---

## 9. Corrections to the brief

The coordinator asked to be told what is wrong. In order of how much it matters:

1. **"I suspect this is not ethanol-only." It is ethanol-only.** Five of seven asset types have one source each.
   `power_plant` (14,659 rows) has **zero** duplication: one source, 14,659 distinct EIA plant codes. §3.
2. **"~158 duplicate pairs, about 41 %, likely ~227 distinct plants." Under-counted.** Measured: **187 links,
   48.2 %, 201 distinct plants**, with a 46 % floor under the most pessimistic reading of my judgement calls.
   The named cause is that name-normalisation systematically misses the ownership-change pairs. §2.
3. **"Merging these rows would either destroy provenance or need a schema change."** The first half is not so.
   `proposal` carries **no** `source_id` / `source_url` / `retrieved_at` / `licence_id` at all — the quartet
   lives on `proposal_source`, one row per source. Multi-source provenance is a solved problem in this schema;
   `asset` just does not use the solution. §4.
4. **The 100 %-branch-coverage risk on `asset_visibility_filter` is smaller than implied.**
   `_has_permitted_source` is a single branch-free `exists(...)` already inside the gate; an asset arm adds
   statements, not branches. The real risk in option (a) is **semantic** — "some source is permitted" is a
   wider predicate than "the source is permitted" — and it needs `min_reuse_class` carried across with the
   table. §5(a).
5. **`asset_owner` is not merely "may already be the right place" — it already holds the data.** 197 Atlas +
   191 capacity operator edges, all 388 assets covered, each correctly attributed. The blocker is that `as_of`
   is NULL on all 388. §7.1.
6. **`resolution_decision` cannot hold an asset pair** (both FKs are `NOT NULL` to `proposal`). Any merge design
   must widen it, or `docs/02` §5's reversibility requirement is unmet. §4.
7. **`web/.data/dev.db` predates migration 0018** — no `alembic_version` table, no `source.vintage` column. The
   asset tables are current, so the asset numbers hold, but nothing about source vintage is measurable from that
   file. §1.
8. **A second defect, independent of duplication, caused part of the wrong Trailblazer answer.** Four plants
   exist only in the capacity report and therefore have **no coordinate at all**, so they cannot appear in any
   radius or map query. One of them, **Green Plains York (NE)**, is in the corridor. De-duplication alone would
   not have surfaced it. §7.3.

---

## 10. Assumptions recorded

- **A-24-1.** The gold set in §2.3 is my own adjudication from public knowledge of the US fuel-ethanol industry,
  not an external authority. Six links are judgement calls; §2.3 states the sensitivity and the 46 % floor. A
  labelled set under `data/eval/` with a second reviewer would make the precision/recall figures citable rather
  than indicative, and should exist before any resolution pass ships. *Depends on:* nothing; it is a known
  limitation of this memo.
- **A-24-2.** "The capacity report reflects current ownership and the Atlas layer is stale" is confirmed on the
  four cases in the brief and consistent across the 40 disagreeing pairs, but is **not** verified against EIA's
  own documentation for either product. §7.2 shows 14 pairs where the Atlas nameplate is the higher figure, so
  the staleness is not uniform across fields. Do not encode "capacity report always wins" as a loader rule on
  this evidence. *Depends on:* reading both EIA products' documentation, which this memo did not do.
- **A-24-3.** §3.2's claim that LMOP and AgSTAR describe disjoint populations rests on zero measured matches
  (0/498 at county + name-Jaccard ≥ 0.5, with 220 of the 498 in a county that does contain an LMOP row) plus the
  two programmes' stated scopes. A digester that both reports would be missed by a name-based test if both
  spellings were unrecognisable; the county blocking makes that unlikely but not impossible.
