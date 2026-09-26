# Back-end review, 2026-09-26 — measured hotspots and the refactor lanes

Owner, 2026-09-25 (`docs/00-PLAN.md` decisions log): "once we're done wiring the back end. Let's do a
complete review and refactor of the back end to make sure everything is clean, modular, and nicely
organized. Removing spaghetti code." The wiring landed in PR #14 (`main` at `c8c782a`). This document is
the review; the refactor is the lane plan in §7. Every number here was produced by
`scripts/backend_metrics.py` (per-module structure, layering, duplicates) and two one-off scripts kept
in the coordinator's scratchpad (route inventory and call graph; phase maps), all on `c8c782a`. Nothing
below is an impression.

## 0. How to read the numbers

- **Lines are physical lines** (docstrings and blanks included). The baseline row in `docs/00-PLAN.md`
  quoted 2,067 / 1,581 / 1,393 for the three largest modules; those were code lines from an earlier
  survey. On the same tree this pass measures 2,554 / 1,896 / 1,574. Both are true; every lane reports
  its delta with `scripts/backend_metrics.py` so the measure stays fixed from here.
- **Fan-in** is the number of modules whose parsed `import` / `from … import` statements resolve to the
  module (relative imports excluded). The earlier survey's "53 importers of `pipeline.connectors.base`"
  counted differently; this method gives 38. Same tree, same code.
- **Nesting** is the depth of `if/for/while/with/try/match` inside a function. **Duplicate bodies** are
  function bodies identical after normalising every identifier, attribute and constant, with ≥ 15
  statements.
- Scope: `pipeline/`, `services/`, `web/`; tests, `.venv` and `migrations/versions` excluded.
  **187 modules, 55,748 lines, 1,900 functions; 49 functions ≥ 80 lines; 9 with nesting ≥ 5.**

## 1. Where the size is

| module | lines | funcs | longest function | nesting | ≥80-line fns | fan-in | fan-out |
|---|---:|---:|---|---:|---:|---:|---:|
| `web/app.py` | 2,554 | 99 | `organization_detail` 224 (L1987) | 6 | 2 | 0 | 17 |
| `services/api/app.py` | 1,896 | 51 | `list_organization_proposals` 72 | 3 | 0 | 1 | 35 |
| `services/api/admin_people.py` | 1,574 | 42 | `admin_approve_intake` 207 (L1122) | 5 | 4 | 1 | 15 |
| `services/api/assets.py` | 1,553 | 53 | `list_organization_nearby_proposals` 109 | 4 | 2 | 2 | 12 |
| `services/db/models.py` | 1,542 | 6 | — (declarative models) | 1 | 0 | **51** | 3 |
| `services/api/admin_records.py` | 1,440 | 34 | `admin_update_opportunity` 107 | 4 | 6 | 2 | 13 |
| `services/ingest/loader.py` | 1,356 | 33 | **`load_dataframe` 325 (L969)** | **6** | 2 | 8 | 7 |
| `services/api/admin_sources.py` | 1,236 | 39 | `admin_get_costs` 174 | 3 | 2 | 1 | 11 |
| `services/social/editorial.py` | 1,166 | 46 | `from_diff_row` 88 | 3 | 1 | 7 | 4 |
| `services/api/admin_posts.py` | 1,062 | 37 | `admin_create_key` 74 | 2 | 0 | 1 | 13 |

Longest functions anywhere, beyond the table: `pipeline/connectors/runner.py::run` 205 lines,
`web/build_data.py::build_proposals` 194 (nesting 5), `pipeline/connectors/dq.py::run_gates` 177,
`services/api/admin_sources.py::admin_get_costs` 174, `pipeline/resolve.py::main` 147,
`services/crm/router.py::admin_create_lead` 140 (nesting 5), `pipeline/context/ghgrp.py::match_facilities`
128 (nesting 5), `services/ingest/ghgrp.py::load_ghgrp` 124.

Highest fan-in: `services.db.models` 51, `pipeline.connectors.base` 38, `services.api.common` 28,
`services.ids` 24, `pipeline.connectors.registry` 20, `pipeline.connectors.http` 18, `services.api.errors`
18, `services.db.session` 16, `services.api.serialize` 15. None of these is a problem in itself: the ORM
module, the connector base class and the tiny shared helpers are supposed to be imported everywhere. The
one to watch is `services.api.serialize` (834 lines, fan-in 15, fan-out 9) — a wide module that many
routers depend on, so a change there has a wide blast radius; it is not scheduled below because no lane
has a measured reason to touch it.

## 2. Duplication is rare

Four groups in 1,900 functions:

1. `services/api/admin_posts.py::submit_intake_proposal` L924 and `::submit_intake_opportunity` L959 —
   32 lines each, 18 statements, identical shape.
2. `pipeline/connectors/us_eia_860/connector.py::fetch` L120 and `us_eia_860m/connector.py::fetch` L64 —
   37 lines each, 17 statements.
3. `services/crm/router.py::_normalise_domain` L67 and `services/api/admin_people.py::_normalise_domain`
   L219 — 15 statements, the same function in two packages.
4. `services/api/app.py::list_proposal_events` L713 (48 lines) and `::list_opportunity_events` L981
   (50 lines) — 15 statements, the proposal/opportunity mirror.

So "spaghetti" here is **not** copy-paste. It is (a) three route modules that carry 70, 23 and 30 private
helpers respectively, (b) one 325-line loader function, (c) one 224-line page handler, (d) a 207-line
admin handler whose body is a 159-line `if/elif` on a decision string. Those are the lanes.

## 3. Layering, measured

`docs/20` §2 states one rule as a contract: `pipeline` must not import `services.api` (kept, 0 edges;
`infra/importlinter.ini`). The rest of the layering is convention. What the imports actually say:

- **`web → services.*`**: eight modules. `web.dev_up` imports ten `services.*` modules (`db.base`,
  `db.models`, `db.session`, `ingest.assets/enrich/ghgrp/midstream/organizations/ownership/plants`) — it
  is the composition root for the local rebuild and this is its job, not a defect. `web.data_loading`
  imports `services.ingest.loader`, `services.db.models`, `services.api.common` and
  `pipeline.connectors.registry` — same role. `web.admin.records` imports `services.api.admin_records`
  and `services.db.models` for vocabularies. `web.api_client` imports `services.api.app` to mount it
  in-process. The remaining four (`legal`, `pricing`, `viewmodels`, `admin.sources`) import only
  `services.api.common` constants and `services.posture`. **Candidate contract (§7 L6):** `web` must not
  import `services.ingest`, `services.resolve` or `services.db`, with `web.dev_up`, `web.data_loading`
  and `web.admin.records` named as the only permitted importers. Adding it now is "extend, never relax":
  those three are exactly today's importers.
- **`services.api → services.{ingest,resolve,social,alerts}`**: ten edges, all of them to shared
  vocabulary or to a service the route fronts (`ingest.lag`, `ingest.vintage`, `ingest.geocode`,
  `resolve.merge` from the merge admin route, `alerts.suppression` from unsubscribe, `social.models`
  from posts). None is a route reaching into another layer's internals. Not scheduled.
- **`services.ingest / services.resolve → services.api`**: **zero edges.** Worth locking in as a
  contract at no cost (§7 L6).
- **`services.* → pipeline.*`**: twelve modules, almost all importing `pipeline.normalize` (the canonical
  vocabulary) and `pipeline.connectors.registry` (the manifest). That is the intended direction:
  `pipeline` owns the vocabulary, `services` consumes it. `pipeline → services`: three edges
  (`registry → services.posture`, two ethanol context modules → `services.ids`, `fuels →
  services.ingest.geocode`). The `posture` import is by design (its docstring says so); the `ids` and
  `geocode` ones are small utilities that arguably belong under `pipeline` — noted, not scheduled.
- Inside `services`, the heaviest cross-package edges are `api → db` 24, `ingest → db` 17,
  `api → ids` 10, `alerts → api` 9, `billing → api` 8. `alerts`, `billing`, `crm` and `social` all
  import `services.api` — for `errors`, `common`, `deps`, `auth`. That is the "shared kernel lives in
  `services.api`" shape; renaming it is out of scope for a behaviour-preserving pass and is flagged
  as the one structural question the owner may want to decide later (§8).

## 4. The three route modules, by call graph

For each module the script asked: which private helpers does each route reach, transitively? A helper
reached by one route belongs with that route; a helper reached by many is genuinely shared.

### 4.1 `web/app.py` — 2,554 lines: 25 routes (833 lines), 70 helpers (1,055), 103 other statements

- Helpers reached by **one** route: 16 (275 lines). Ten of them serve `organization_detail` /
  `organizations_list` alone (`organization_jsonld` 47, `_org_asset_groups` 36, `_resolve_organization`
  24, `_org_holdings` 23, `_org_asset_row` 18, `_derived_org_provenance` 17, `_org_subsidiaries` 12,
  `_held_by` 11, `_count_or` 8); four serve the asset pages (`asset_type_counts` 31,
  `_asset_index_description` 16, `_resolve_asset_by_slug`, `_asset_index_default_sort`).
- Helpers shared by 2+ routes: 53 (769 lines). The wide ones are page plumbing (`get_api` ×23,
  `canonical_url`, `jsonld_block`, `_prune` ×8 each, `get_lag_days`, `is_preview_active` ×6) and
  asset presentation used by 3–4 routes (`_fuel_fields` 86, `_geometry_svg` 53, `_asset_extras` 43,
  `_normalise_owners` 30, `item_list_jsonld` 32, `group_nearby_proposals` 31). Sitemaps are a closed
  cluster: five helpers (81 lines) + three routes reached by nothing else.
- `organization_detail` (L1987–2210): 46 top-level blocks; **34 locals are used more than one block
  after assignment**, `record` in 10 blocks, `assets` in 8, `api` in 8, `nearby` in 7. The structure is
  fetch (blocks 15–17, 28: four `try` blocks against the API) → compose (18–44) → a 50-line
  `TemplateResponse` context (45). A phase split falls out of that map.
- `web/` already has the router-per-area pattern: `web/auth.py`, `web/legal.py`, `web/pricing.py` and
  every `web/admin/*.py` are `APIRouter`s included from `web/app.py` (L949–980).

### 4.2 `services/api/app.py` — 1,896 lines: 28 routes (938), 23 helpers (473), 83 other statements

- Routes by resource: `v1/organizations` 4 (180 lines), `v1/opportunities` 5 (162), `v1/proposals` 5
  (155), feeds 3 (105), `v1/events` 2 (68), health 1 (60), licences 2 (42), sources 2 (39), meta 1 (31),
  coverage / lifecycle-states 1 each, plus the 71-line `standard_headers` middleware.
- Helpers: 10 single-route (185 lines), 13 shared (288). The shared ones are the proposal/opportunity
  filter stack (`_apply_slip_filter` 55, `_apply_proposal_filters` 49, `_opportunity_query_with_filters`
  44, `_apply_placement_filter` 34) used by list, geo and organisation routes alike.
- `services/api/` already has a router per area (`assets`, `pro`, `admin_*`, `crm`, `billing`, `geo`,
  `feeds` for rendering, …) included at L116–172, and `app.py`'s own header says "extend, don't rewrite".
  What is left in `app.py` is the public record surface (proposals, opportunities, organisations,
  events) plus the health/meta/coverage endpoints.
- The one mirrored pair (§2 item 4) is a two-call-site abstraction: one `_list_subject_events(kind, …)`
  with two thin routes.

### 4.3 `services/api/admin_people.py` — 1,574 lines: 12 routes (776), 30 helpers (538)

- `admin_approve_intake` (L1122–1328, 207 lines) reaches **13 helpers that nothing else reaches** (407
  lines: `_create_proposal_from_pending` 68, `_create_opportunity_from_pending` 68,
  `_get_or_create_intake_source` 36, `_resolve_org` 34, `_resolve_intake_source` 17, …). Its body is
  one 159-line `if decision == "reject" / "link" / "approve"` block (L1155–1313) with six locals crossing
  it. That is a module (intake approval) living inside another (people).
- The deletion flow is a second closed cluster: `admin_update_task` 82 + `_complete_deletion_task` 79 +
  `_cancel_billing_for_erased_user` 28 + `_tombstone_email`.
- Customers/subscriptions is a third: four routes (228 lines) + `_serialize_customer` 42,
  `_subscription_status_map` 12, `_domain_from_email`.
- Shared across the file: `serialize_user_admin_view`, `_require_reason`, `_find_user`, `serialize_task`,
  `_task_subject_public_id` (each ×5–6).

## 5. `services/ingest/loader.py::load_dataframe` (L969–1293, 325 lines, nesting 6)

Phase map from the AST (top-level blocks of the function body):

| blocks | lines | what | locals it creates that later blocks read |
|---|---:|---|---|
| 01–08 | 11 | clocks, result, id maps, entity/link classes by `kind` | `now`, `result`, `record_id_to_internal`, `key_by_record_id`, `claimed`, `entity_cls`, `link_cls`, `fk_name` |
| 09–10 | 7 | `set_source_vintage`, `_build_load_cache` | `cache` |
| 11–16 | 8 | rows → dicts, distinct record ids per key, `dup_naturals`, `warned_reuse`, `pending_links` | `records`, `dup_naturals`, `warned_reuse`, `pending_links` |
| 17 | **168** | `with session.no_autoflush:` upsert loop — claim keys, resolve links, provenance, publish state, per-row `public_at` / `published_at` | 15 loop locals; `record_id_to_internal` (filled here) is read by block 19. *Corrected 2026-09-26 by lane L1:* `public_at` and `published_at` do **not** cross — block 19 assigns both afresh (`published_at = now`; `public_at = record_public_at(published_at)`); the phase script flagged name reuse, not a data flow. |
| 18 | 1 | `_flush_pending` | — |
| 19 | **81** | events: idempotency keys, before/after values, observed tokens | — |
| 20 | 1 | `return result` | — |

16 locals cross a block boundary (18 by name; two of them, `public_at` and `published_at`, are reassigned rather
than read — see the table); the upsert loop and the events block are each already a function in
everything but name. The extraction (§7 L1) is three named steps with the phase's inputs as parameters
and a small frozen context for the eight values every step reads — three call sites, so it clears the
"no new abstraction without two call sites" rule. Behaviour oracle: `services/ingest/test_loader.py`,
`test_loader_events.py`, `test_vintage.py`, `tests/test_free_tier_change_reconstruction.py`,
`tests/test_publication_is_never_time_delayed.py`, `tests/test_resolve_store.py`,
`tests/test_resolve_store_unmerge.py`, `tests/test_platform_posture.py`, `tests/test_api_geo_performance.py`
(nine files import the module; five reference `load_dataframe` by name).

## 6. Test oracle per hotspot (files that import the module)

| module | test files importing it | pins the longest function |
|---|---:|---|
| `services.ingest.loader` | 9 | yes — five files call `load_dataframe` directly |
| `web.app` | 23 | `organization_detail`: `web/test_ownership.py`, `test_nearby_relevance.py`, `test_seo.py`, `test_coverage_pages.py`, `test_asset_pages.py`, `tests/test_org_key.py` |
| `services.api.app` | 27 | route inventory pinned by `tests/test_spec_operation_inventory.py` and `tests/test_api_contract.py` (the OpenAPI operation set must not change) |
| `services.api.admin_people` | 1 (`tests/test_api_admin_people.py`) + `tests/test_api_admin_intake_flow.py` for the approve flow | yes |
| `services.api.assets` | 1 direct + the `tests/test_api_assets_*.py` family through the app | partial |
| `services.api.admin_records` | 1 (`tests/test_api_admin_records.py`) | partial |

Coverage floors that gate every lane: 80 % branch on `pipeline/*,services/*`; 100 % on
`services/api/visibility.py`. Both are CI's exact commands.

## 7. Lanes, in order

Rules for every lane (from the plan row): behaviour-preserving — the full suite and the floors are the
oracle and no test is weakened; one lane per file area, no two lanes in the same file; every split
cites the baseline number it moves; import-linter contracts extended, never relaxed; no new
abstraction without two call sites; each lane reports its delta with `scripts/backend_metrics.py`.

| # | lane | files | baseline → target | contract it enables | gating tests | order |
|---|---|---|---|---|---|---|
| L1 | Extract `load_dataframe` into named steps | `services/ingest/loader.py` only | longest function 325 → ≤ 80 (orchestrator); nesting 6 → ≤ 3 per step | — | the nine files in §5 | first: highest risk, best-pinned |
| L2 | Organisation pages out of `web/app.py` | new `web/organizations.py` (router); `web/app.py` shrinks | `web/app.py` 2,554 → ≈ 2,000; `organization_detail` 224 → fetch / compose / render of ≤ 80 each | — | `web/test_ownership.py`, `test_nearby_relevance.py`, `test_seo.py`, `test_coverage_pages.py`, `tests/test_org_key.py`, `tests/test_web_default_view.py` | after L1 (independent files; sequenced only so one full suite runs at a time) |
| L3 | Asset pages, asset presenters and sitemaps out of `web/app.py` | new `web/assets_pages.py`, `web/sitemaps.py`; `web/app.py` | `web/app.py` ≈ 2,000 → ≈ 1,300; the three-route asset cluster and the five-helper sitemap cluster move whole | — | `web/test_assets.py`, `test_asset_pages.py`, `test_seo.py`, `tests/test_api_assets_lines.py`, `tests/test_api_assets_licence_gate.py` | after L2 (same file) |
| L4 | Intake approval out of `admin_people.py` | new `services/api/admin_intake.py`; `admin_people.py` | `admin_people.py` 1,574 → ≈ 1,100; `admin_approve_intake` 207 → three decision functions ≤ 60 each; `_normalise_domain` dedup (§2 item 3) into `services/api/common.py`, imported by both `crm.router` and `admin_people` | — | `tests/test_api_admin_people.py`, `tests/test_api_admin_intake_flow.py`, `tests/test_api_admin_posts.py` | parallel with L2 (disjoint files) |
| L5 | Public record routes out of `services/api/app.py` | new `services/api/records.py` (proposals, opportunities, their events/sources/geo + the shared filter stack); `app.py` keeps app wiring, middleware, health/meta/coverage, organisations, events, feeds | `app.py` 1,896 → ≈ 1,000; §2 item 4 becomes one `_list_subject_events` with two routes | — | `tests/test_spec_operation_inventory.py`, `tests/test_api_contract.py`, `tests/test_paid_shapes_are_gated.py`, `tests/test_api_geo_performance.py`, `tests/test_platform_posture.py` | after L4 |
| L6 | Lock the layering in | `infra/importlinter.ini` only | contracts: `web` ↛ `services.ingest/resolve/db` except `web.dev_up`, `web.data_loading`, `web.admin.records`; `services.ingest`, `services.resolve` ↛ `services.api` (0 edges today) | the two contracts themselves | `lint-imports` in CI | last, after L2–L5 so the exception list is measured on the final tree |
| L7 | Customers/subscriptions and the deletion flow out of `admin_people.py` | new `services/api/admin_customers.py`; `admin_people.py` | `admin_people.py` ≈ 1,100 → ≈ 700 | — | `tests/test_api_admin_people.py` | optional; after L4, only if L4's delta and suite time justify a second pass on the same file |

**L1 outcome (2026-09-26).** `load_dataframe` 325 → 37 lines; module nesting 6 → 3; functions ≥ 80 lines 2 → 3 (`upsert_licence_and_source` 109 pre-existing, `_create_entity_and_link` 97, `_load_one_event` 81); every new function ≤ 6 parameters; module 1,356 → 1,480 lines (+124: twelve new definitions with short docstrings; the +80 target was missed by 44 and accepted). Two small objects, each with three call sites: a frozen `_LoadContext` for the per-load constants and a mutable `_UpsertState` for the loop's four containers. Three passes were needed: the first threaded the context's contents by hand (15-, 13- and 10-parameter helpers); the second folded them in; the third removed a duplicated per-row `fields` computation. **Throughput, measured with `services/ingest/bench_loader.py`, interleaved before/after on the same filesystem with a `/tmp` control worktree: 1,332–1,378 rows/s before, 1,282–1,310 after, ≈ 3.5 % slower.** `cProfile` shows +28k calls in 18.7 M and no new hot spot (SQLAlchemy attribute machinery dominates both); the cause was not isolated after three benchmark rounds and the delta is accepted for a batch path (≈ 0.25 s per 9,563 rows). Full suite on the final tree: core exit 0, 88 %, `visibility.py` 100 %, `loader.py` 89 % branch, web exit 0. One correction to §5 came out of the lane (see the table note).

**L2 outcome (2026-09-26).** `web/app.py` 2,554 → 1,423 lines (99 → 49 functions); `organization_detail` 224 lines / nesting 6 → an orchestrator of 22 lines over `_organization_fetch` (70), `_organization_compose` (61), `_organization_map` (45), `_organization_nearby` (51) and `_organization_render` (58), with two dataclasses (`_OrgFetch`, `_OrgCompose`) and one intermediate (`_OrgMap`) carrying what 34 locals used to; `api` and `entity` threaded explicitly. New `web/organizations.py` (856 lines, the two routes and the 14 helpers plus 8 constants grep showed only they reach) and `web/page.py` (554 lines: the `Jinja2Templates` instance and its globals, `get_api`, the canonical-URL and JSON-LD helpers, `not_found_response`, and the map/geometry presenter cluster that `asset_detail` also calls) — the shared home that `web/legal.py` and `web/pricing.py` can adopt later instead of their own template duplication. Verified by AST, not by reading: of the 142 top-level definitions in the original `web/app.py`, 140 are node-for-node identical in their new home and the two that differ are the two routes (`@app.get` → `@router.get`; the split). Repo totals: functions ≥ 80 lines 49 → 48; nesting ≥ 5 unchanged at 9 (`_org_type_counts` depth 6 and `_org_summary_parts` depth 5 moved untouched — out of this lane's mandate). Coordinator cleanups: two test imports retargeted to the new modules instead of a `noqa` re-export, and `WEB_ROOT` imported rather than defined twice. The lane reported catching itself retyping two helpers from memory (`_geometry_of` lost the legacy `geom` key; `_count_or` lost its negative-value clamp) before it diffed against the original — the reason the acceptance check is the AST comparison, not the suite.

**L4 outcome (2026-09-26).** `services/api/admin_people.py` 1,574 → 1,002 lines (42 → 27 functions; longest 207 → 98, `admin_create_customer`); new `services/api/admin_intake.py` (672 lines, 19 functions, longest 68) holds the approve route and the thirteen helpers grep showed only it reaches — `_decode_public_id` measured as `admin_list_tasks`'s and left behind, as §4.3 flagged. The 159-line `if decision == …` body became `_apply_reject_decision` (16 lines), `_apply_link_decision` (49) and `_apply_approve_decision` (43, after `_record_approve_audit_events` (31) and `_create_intake_lead_signal` (45) were lifted out of it), sharing a frozen `_IntakeDecisionContext` (five fields); no new function over 4 parameters. §2 item 3 closed: the two `_normalise_domain` bodies were AST-identical and now live once as `services/api/common.py::normalise_domain`, imported by `crm/router.py` and the intake module — duplicate groups 4 → 3. Verified by AST: 47 of 49 original definitions node-identical (the two that differ are the split route and the one-line rename in `admin_create_customer`); `crm/router.py` 8 of 9 (same rename). Repo: functions ≥ 80 lines 49 → 48, nesting ≥ 5 9 → 8. One fixture change the lane flagged and the coordinator accepted: `tests/test_api_admin_people.py` builds a standalone app carrying only `admin_people`'s router, so it now mounts the intake router too, as `services/api/app.py` does — a fixture, not an assertion; four assertion lines call the renamed public `normalise_domain` with unchanged arguments and expectations.

Estimated diff sizes (moved lines count twice): L1 ≈ 400, L2 ≈ 1,200, L3 ≈ 1,400, L4 ≈ 1,000,
L5 ≈ 1,900, L6 ≈ 30, L7 ≈ 700. Each lane is one PR, merged on green.

## 8. Measured, not scheduled — and why

- `services/api/assets.py` (1,553 lines, 53 functions, longest 109): large but its longest functions are
  under 110 lines and nesting is 4; splitting it would be a size-driven move with no coupling evidence.
  Revisit if L3 moves presenters that it also needs.
- `services/db/models.py` (1,542 lines, fan-in 51): declarative models; a split by aggregate would touch
  51 importers for no behavioural gain. Leave.
- `pipeline/connectors/runner.py::run` 205, `dq.py::run_gates` 177, `pipeline/resolve.py::main` 147: CLI
  orchestration with straight-line structure (nesting 2–4). A lane could name their phases, but the
  test oracle for the runner is thinner than for the loader; not this pass.
- `web/build_data.py::build_proposals` 194 lines, nesting 5 — the static-site build path. Worth a lane
  only if that path is still shipped; confirm before scheduling.
- The `alerts/billing/crm/social → services.api` dependency for `errors`, `common`, `deps`, `auth` means
  `services.api` is also the shared kernel. Moving those four into a `services/kernel` (or similar) is
  a rename with 28+ importers and belongs to a decision, not a behaviour-preserving lane. **Owner
  decision, not urgent.**
- `web/dev_up.py` importing ten `services` modules is the composition root doing its job.

## 9. Method notes

- Scripts: `scripts/backend_metrics.py` (committed); the route inventory / call-graph and phase-map
  scripts are one-offs in the coordinator's scratchpad and their outputs are the tables above.
- Not measured: per-module branch coverage (the last run's data file was taken in a worktree that no
  longer exists and `coverage` needs the source paths to report); the lanes use the CI floors and the
  test-file oracle instead. Runtime behaviour (latency, query counts) is out of scope for a
  structure review.
