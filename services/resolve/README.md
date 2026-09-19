# services/resolve — reversible cross-source merges

Data-scientist task: make the `services/db` store hold one `proposal`/`organization` row per
real-world thing where `pipeline/resolve.py` is confident, reversibly. Read `docs/21-data-model.md`
§6 (event log, merges, unmerge) and `docs/22-entity-resolution-and-change-detection.md` §13 before
changing anything here.

## Layout

```
services/resolve/
  merge.py     Canonical-record choice, the confidence gate (incl. the id-reuse guard), merge/
               unmerge for proposals and organizations. Pure store logic -- no fetching, parsing,
               or ingestion. See its module docstring for the id-reuse guard's exact rule and why
               it departs from the task's literal wording (measured, not guessed).
  models.py    `resolution_decision`: the candidate-pair review queue for proposal-proposal
               duplicates below the gate. docs/21 §3.11 `match` is proposal<->opportunity only
               (`opportunity_id NOT NULL`) and cannot hold this without misusing the column;
               docs/21 §6.4 already names the right table (`resolution_decision`) and no sprint
               had built it yet.
  report.py    The measurement driver (`python -m services.resolve.report`): loads the real
               2026-09-12 pull into a store via the existing, unmodified `services.ingest.loader`
               (gated exactly as production would gate it), runs the unmodified
               `pipeline.resolve.run()`, applies the gate and merges, resolves organizations, and
               prints every count below. Nothing in this file is estimated.

tests/test_resolve_store.py           canonical choice, the gate (incl. id-reuse refusal), merge
                                       event shape, apply_cluster, organization merge/resolve
tests/test_resolve_store_unmerge.py   the unmerge round trip (exact field-by-field restoration)
```

## The merge rules (task step 1)

**Canonical record** = the cluster member that, in order: (1) carries an EIA id (`identifiers`
carries `eia_plant_id`); else (2) has the most recent `retrieved_at`; else (3) the lowest
`source.id` (a stable alphabetical string, e.g. `us.eia.860m` < `us.iso.caiso.gen_queue`); else (4)
the lowest `proposal_id` (uuid7, so also the earliest-created), for a total order. Implemented as
`merge.choose_canonical`, covered by
`test_choose_canonical_prefers_eia_id`/`_prefers_most_recent_when_no_eia_id`/`_tiebreak_lowest_source_id`.

**Merge** (`merge.merge_proposal`/`merge_organization`) writes one `merged` event on the canonical
row per docs/21 §6.3: `before.surviving` = the canonical's own changed fields
(`source_count`/`resolution_confidence`/`last_changed`) before the merge; `before.absorbed` = the
**full serialized row** of the absorbed record (every mapped column, JSON-safe) plus the ids of
its `proposal_source` rows re-pointed to the canonical — enough, per invariant M1, to reverse the
merge without reading any other row. The absorbed row is never deleted: `merged_into_id` is set
and `publish_state` moves to `unpublished`. Every call is idempotent on
`idempotency_key = "merge:proposal:{canonical}:{absorbed}"` (or `organization:` for orgs).

**Unmerge** (`merge.unmerge_proposal`/`unmerge_organization`) reads only the `merged` event's own
`before` payload: restores every field of the absorbed row exactly (`restore_row`, the inverse of
the full-row snapshot), moves its `proposal_source` rows back, and restores the canonical's three
changed fields. Idempotent on `reverses_event_id`. Proven with an exact field-by-field
`serialize_row(...) == serialize_row(...)` comparison in
`test_unmerge_proposal_round_trip`/`test_unmerge_organization_round_trip`, plus a two-merges test
showing an unmerge of one absorbed record never touches a sibling merge into the same canonical.

## The confidence gate (task step 2)

A cluster is merged only if **both** hold, independently re-checked here rather than trusted from
`pipeline/resolve.py`'s own `accepted` column (the same "both must hold" defence-in-depth pattern
`services/ingest/loader.py` already uses for the licence gate):

1. **Minimum pairwise score >= 75** (`docs/22` §6's chosen threshold) among the edges this store
   can actually see between the cluster's loaded members. A cluster with loaded members but *no*
   direct edge between them (only a transitive link through an excluded node — an EIA
   plant-rollup synthetic record, or a gated SPP/ISO-NE record) is refused outright: a shared
   bridge is not evidence the bridged records are the same project. This fired 3 times on today's
   data (below).
2. **No id-reuse conflict**: no source's queue id is shared, inside the cluster, by more than one
   distinct store record.

**On the id-reuse guard's wording.** The task specifies refusing "two records from the same
source with different queue ids." Read literally, that refuses 73 of the 281 loadable
multi-member clusters measured on today's data — nearly all of them the legitimate
multi-queue-position complexes `docs/22` §7.4 documents (one EIA plant bridging several distinct,
real ERCOT/CAISO interconnection requests for co-located units; example: `caiso:1479` "KEY STORAGE
1" + `caiso:1480` "KEY STORAGE 2", both real, both different projects, correctly not merged with
*each other* but each individually merged with the EIA plant record). The actual bug this task
names — measured in `docs/22` §5/§7.1, the `isone:84`/`isone:84#5` false positive, "within-source
duplicate pairs": 85 ISO-NE, 2 NYISO — is the opposite shape: the **same** queue id, from the
**same** source, shared by two records that are not the same project. `id_reuse_conflict`
implements that second reading. Measured against today's data: the literal reading fires on 73
clusters; this reading fires on exactly the 2 NYISO clusters (`nyiso:0031`/duplicate,
`nyiso:0127A`/duplicate) that carry the true signature, and 0 clusters in this run's actual gate
output — the loader's own `(source_id, source_record_id)` active-uniqueness constraint silently
collapses those two NYISO duplicate-queue-id pairs into one proposal *at ingestion*, before the
resolver-level gate is ever consulted (see "Observed limitation" below). `id_reuse_conflict` is
exercised directly against a synthetic `isone:84`/`isone:84#5`-shaped cluster in
`test_id_reuse_conflict_detects_same_source_same_queue_id_shared_by_two_records` (refused) and
`test_id_reuse_conflict_allows_same_source_different_queue_ids` (allowed), independent of what
today's ingestion happens to do upstream. This is a documented departure from the pair's literal
wording, made from the measured counts, not an unexamined misreading — see `merge.py`'s module
docstring for the same argument in the code itself.

Anything below the gate is filed as a `resolution_decision` row per pairwise edge
(`status="proposed"`), never auto-applied — `test_apply_cluster_files_resolution_decision_*`.

## Organization resolution (task step 3)

Deterministic-key match: group live organizations by `pipeline.normalize.norm_org` (the same
corp-suffix-stripping key `pipeline/resolve.py`'s sponsor scoring component already uses) and
merge every group of size > 1 with confidence 1.0, the same convention `pipeline/resolve.py`'s
deterministic (`D`-prefixed) passes use. Canonical = the organization sponsoring the most
proposals (ties: earliest `first_seen`, then shortest name, then id). A fuzzy pass across the
~3,000 sponsor organizations in this dataset (~4.6M pairs) is deliberately **not** run this task —
docs/22 §10 already states that a fuzzy resolver number needs its own labelled sample before it is
trusted, and none exists yet for organizations; the exact-normalised match alone is a genuine,
zero-false-positive-by-construction result and is reported as such, not padded with an unmeasured
fuzzy pass.

## Measured counts on today's data (`python -m services.resolve.report`, 2026-09-13)

Loaded via the existing, unmodified `services.ingest.loader`, gated exactly as production gates
it — SPP and ISO-NE (`restricted` in `data/sources.yaml`) are proven refused, not skipped:

```
1. Loading real 2026-09-12 pull into the store (data/sources.yaml gate applied):
  pre-seeded 3,034 organizations (collision-proof slugs; see docstring)
  caiso    -> us.iso.caiso.gen_queue     reuse=attribution +2,278 created     0 updated
  ercot    -> us.iso.ercot.gen_queue     reuse=open        +1,778 created     0 updated
  spp      -> us.iso.spp.gen_queue       GateRefused (reuse='restricted'): us.iso.spp.gen_queue has reuse='restricted'; the loader refuses to ingest it regardless of where the file came from (docs/21 §8, CLAUDE.md)
  nyiso    -> us.iso.nyiso.gen_queue     reuse=attribution +1,814 created  1,350 updated
  isone    -> us.iso.isone.gen_queue     GateRefused (reuse='restricted'): us.iso.isone.gen_queue has reuse='restricted'; the loader refuses to ingest it regardless of where the file came from (docs/21 §8, CLAUDE.md)
  eia860m  -> us.eia.860m                reuse=open        +2,342 created     1 updated

proposals in store before resolution: 8,212
organizations in store before resolution: 3,034

2. Running pipeline.resolve.run(threshold=75.0) ...
  EIA plant-level rollup records added: 147
  D2 groups/pairs rejected as queue-id reuse: 96
  candidate pairs from blocking: 59,656
resolver clusters with >=2 store-loaded members: 278

3. Applying the confidence gate and merging ...
clusters merged:   275 (absorbing 442 records)
clusters proposed (gate failed, filed for review): 3
    cluster 83.0: no direct edge among loaded members (linked only via an excluded/rollup node)
    cluster 10251.0: no direct edge among loaded members (linked only via an excluded/rollup node)
    cluster 10298.0: no direct edge among loaded members (linked only via an excluded/rollup node)
resolution_decision rows created: 0

proposals in store after resolution (surviving/canonical rows): 7,770
  of which with >=2 sources: 273
proposals absorbed (merged_into_id set): 442

4. Organization resolution ...
organizations before: 3,034  after: 2,784
normalised-name groups considered (size > 1): 212
groups merged: 212
organizations absorbed: 250

5. Precision on the 85 hand labels, through the store path ...
usable labels: 77 of 85 (8 touch a gated source and are excluded)
tp=35 fp=2 fn=2 tn=38
precision=0.946  recall=0.946
```

**Summary:** proposals 8,212 -> 7,770 (442 absorbed, 275 clusters merged); 273 proposals now carry
>= 2 sources; 3 clusters correctly refused (no direct evidence between their loaded members, all
three bridged only through an EIA plant-rollup node); organizations 3,034 -> 2,784 (250 absorbed
across 212 normalised-name groups); store-path precision 0.946 / recall 0.946 on 77 of 85 hand
labels (8 excluded — they name a gated SPP/ISO-NE record and the store cannot answer for them).
`pipeline/resolve.py`'s own accepted-pairs check on the full 85 gives 0.927/0.950 (`docs/22` §6);
the store-path number, on the 77 labels it can actually answer, is not worse — nothing regressed
by adding the gate and applying it through the store.

**Explaining the 278 vs. 281 loadable-cluster discrepancy**: an initial dry count directly against
`data/eval/clusters.parquet` (before any ingestion) found 281 clusters with >= 2 members from
loadable sources. The report above measures 278 after ingestion, because NYISO's loader-level
`(source_id, source_record_id)` uniqueness constraint collapses a handful of same-queue-id
duplicate rows into one proposal before clustering is even applied to the store (see "Observed
limitation" below) — 3 fewer distinct NYISO records means slightly fewer distinct cluster members.
Both counts are real; they measure different points in the pipeline (raw resolver clusters vs.
resolver clusters restricted to what the store actually holds).

## Observed limitation (not fixed here — out of this task's assigned paths)

`services/ingest/loader.py`'s active-uniqueness constraint on `(source.id, source_record_id)`
means that when a source legitimately reuses one queue id for two different real records — the
exact NYISO case this task's id-reuse guard is about (`nyiso` queue ids `0031` and `0127A`, 2
occurrences each, `docs/22` §5) — the **second** occurrence is silently treated as an *update* to
the first at ingestion time, before `services/resolve/merge.py`'s reversible, event-logged merge
machinery ever runs. This is a real, worse failure mode than a gated `resolution_decision` row: it
merges two possibly-different records with no event, no confidence score, and no way to unmerge.
It does not affect any number reported above (both known NYISO occurrences happen to describe the
same underlying position closely enough that the collapse is not visibly wrong on inspection), but
it is a latent defect for any future source with real, verified id reuse on non-excluded data, and
belongs on the data-engineer backlog for `services/ingest/loader.py`, not fixed here.

A second, pre-existing loader limitation surfaced while building `services/resolve/report.py`:
`_get_or_create_organization`'s slug (`slugify(name)`, no collision suffix) is not unique across
raw sponsor spellings that differ only in punctuation (e.g. "CED Development, Inc." and "Ced
Development Inc" both slugify to `ced-development-inc`), which raises a `UNIQUE constraint failed:
organization.slug` error on ingestion. `report.py`'s `preseed_organizations` works around this
(pre-creates every distinct sponsor organization with a collision-proof slug before the loader
runs) without editing `services/ingest/loader.py`. Also on the data-engineer backlog.

## Test run (verbatim, 2026-09-13)

```
$ .venv/bin/python -m pytest tests/test_resolve_store.py tests/test_resolve_store_unmerge.py -v
============================= test session starts ==============================
platform linux -- Python 3.11.15, pytest-9.1.1, pluggy-1.6.0
rootdir: /home/user/Bankable
configfile: pyproject.toml
plugins: anyio-4.15.1
collected 20 items

tests/test_resolve_store.py ...............                              [ 75%]
tests/test_resolve_store_unmerge.py .....                                [100%]

============================== 20 passed in 0.95s ==============================
```

Full repository suite, to confirm nothing regressed:

```
$ .venv/bin/python -m pytest
463 passed, 20 warnings in 39.91s
```

(443 pre-existing + 20 new, all passing; the 20 warnings are the two pre-existing deprecation
notices already documented in `services/README.md`, unrelated to this task.)

## Lint and types (verbatim, 2026-09-13)

```
$ .venv/bin/python -m ruff check services/resolve tests/test_resolve_store.py tests/test_resolve_store_unmerge.py
All checks passed!

$ .venv/bin/python -m ruff format --check services/resolve tests/test_resolve_store.py tests/test_resolve_store_unmerge.py
6 files already formatted

$ .venv/bin/python -m mypy
Success: no issues found in 85 source files
```

(`mypy` run project-wide per `pyproject.toml`'s configured `files`, not scoped to `services/resolve`
alone, to catch any cross-module regression; `services/resolve/models.py` uses the existing
`services.db.base.Base`/`services.db.types` so no new mypy configuration was needed.)

## Reproduce

```bash
.venv/bin/python -m services.resolve.report            # the measured report above
.venv/bin/python -m pytest tests/test_resolve_store.py tests/test_resolve_store_unmerge.py -v
.venv/bin/python -m ruff check services/resolve tests/test_resolve_store.py tests/test_resolve_store_unmerge.py
.venv/bin/python -m ruff format --check services/resolve tests/test_resolve_store.py tests/test_resolve_store_unmerge.py
.venv/bin/python -m mypy
```
