# ADR 0008 — Assets are first-class entities; proposals are drawn at their placement grade

**Status:** Accepted · 2026-09-18 · coordinator; decided by the owner in `docs/00-PLAN.md`
(2026-09-18 entries "Midstream and fuels context layers" and "The map is asset-first navigation")
**Supersedes:** the "context layer" framing of `built_plant` in `docs/21` §3.20 (the table is
generalised, not removed) · **Superseded by:** none
**Related:** `docs/21-data-model.md` §3.7 (`location.precision`), §3.22 (`asset`), §3.23
(`asset_owner`); `docs/23-api-spec-outline.md` §3.1 (assets, regions, organisation assets);
`docs/33-gtm-and-sales-playbook.md` §1.8; `docs/adr/0007` (tiles bucket the line layers will use).
**Implemented by:** migrations 0009–0011 (`services/db/`), `services/api/assets.py`,
`services/api/geo.py` (region features), `services/api/regions.py`, `services/ingest/assets.py`,
`services/ingest/ownership.py`, `pipeline/connectors/us_eia_860/`, `pipeline/context/regions.py`,
`web/` asset and company pages and the region layer in `web/static/js/map.js`.

## Context

The product was built proposals-first: proposals and opportunities are the entities with identity,
pages, events and alerts; operating plants arrived on 2026-09-14 as a "context layer", a side table
drawn under the proposals map with no pages and no owners. On 2026-09-18 the owner reframed the
product: the energy-infrastructure map with owners is the acquisition surface, the proposals are the
paid product, and proposals appear on the map only where they can honestly be placed. Two facts
shaped the decision. Every asset layer we would draw is already a free EIA Energy Atlas layer
(twelve probed on 2026-09-18), so the map itself is not scarce; the join to owners and to nearby
proposals is. And proposals mostly do not have exact locations: 2,490 of 7,659 active proposals on
the owner's 2026-09-15 load had no usable county or state, and most of the rest are county-level
queue rows. Drawing those as points next to exactly-located assets would misrepresent them.

The owner asked whether this warranted a rewrite. It does not: the change is a table
generalisation, an edge table, a precision vocabulary and two page templates inside the existing
architecture, and a rewrite would discard the tests, connectors, provenance model, resolver,
adapters, CI and legal register that are independent of the framing.

## Decision

1. **`asset` replaces `built_plant`** (migration renames and widens the table; the plants API path
   stays as an alias). An asset has a `public_id`, a `slug`, an `asset_type`, a point geometry and an
   optional line geometry, an operator, a status, the type-specific objective feature set in
   `attributes`, and the provenance quartet. Asset types at this decision: `power_plant`,
   `gas_pipeline`, `gas_processing_plant`, `gas_storage`, `lng_terminal`, `compressor_station`,
   `ethanol_plant`, `biodiesel_plant`, `rng_project`, `transmission_line`, `substation`,
   `refinery`. Assets have no lifecycle, events, matches or alerts; they are not proposals. A
   registry row whose status is planned or under construction is a proposal and goes through the
   normal lifecycle, not into `asset`.
2. **`asset_owner` records ownership and operation edges** from `asset` to `organization` with a
   share percentage where the source gives one (EIA-860 Schedule 4), a role (`owner` or
   `operator`), an as-of date and the provenance quartet. `organization` gains `parent_org_id` for
   GLEIF Level 2 direct-parent links and stores the LEI in `ids`. Company pages list an
   organisation's assets and its proposals together; asset pages list owners and nearby
   exact-grade proposals.
3. **Placement grades on proposals** are derived from `location.precision` and never stored
   separately: `exact` → **exact** (drawn as a point, joinable to assets); `county_centroid`,
   `state_centroid` and the new `country_centroid` → **region** (drawn as the highlighted county,
   state or country polygon carrying a count, never as a point, never joined to an asset);
   `unknown` → **none** (list, search and alerts only, reason shown on the record). The map's
   proposals response carries region features with counts; polygons come from a vendored Census
   cartographic-boundary file for US counties and states and from the existing country outlines,
   served by a regions endpoint, not inlined per feature.
4. **Objective features only.** `attributes` holds what a public registry states or what can be
   derived from one (capacity, vintage, technology, capacity factor and heat rate from EIA-923, RIN
   pathway, mileage, diameter mix, incident counts, working gas). No valuation, tariff, contract or
   throughput economics (owner, 2026-09-18). A valuation layer is a separate future decision.
5. **Opportunities stay off the map.** An RFP or grant has a jurisdiction, not a site. Drawing
   utility opportunities as the issuer's service territory (EIA Atlas polygon layer) is recorded as
   an option, not scheduled.

## Options considered

| Option | For | Against |
|---|---|---|
| **Generalise `built_plant` to `asset` with owner edges; derive placement grades** (chosen) | One table for every registry-sourced asset; the plants lane's parse-geocode-load-cluster pattern carries over unchanged; grades cost no migration on `location` beyond one vocabulary value | Line geometry on SQLite is text, so the test target cannot exercise spatial operations on lines (accepted: lines are drawn from tiles, not queried) |
| Keep `built_plant` and add sibling tables per asset class | No migration of existing rows | Twelve near-identical tables, twelve geo endpoints, no single asset page or owner edge; exactly the drift the owner's reframing is meant to prevent |
| Store the placement grade as a column | Simple filter | Duplicates `precision`; the two would diverge; derivation is one function |
| Draw region-grade proposals at centroids with an uncertainty ring (coordinator's first proposal) | Cheap; no polygon data | Owner rejected: a state-level or country-level record has no honest centroid; a highlighted region is what the data supports |
| Rewrite the platform around assets | Clean model from day one | Nothing in the new model conflicts with the old; the cost is the whole verified codebase and the benefit is not measurable |

## Consequences

- `services/ingest/plants.py` becomes the `power_plant` case of `services/ingest/assets.py`; the
  EIA-860M Operating sheet remains the first source. New point layers are one parser each.
- `GET /v1/context/plants/geo` stays for the shipped map and forwards to `GET /v1/assets/geo`
  with `asset_type=power_plant`.
- The map gains a placement filter with three grades; region-grade proposals render as polygons
  with counts and click through to the filtered list. The "Unplaced" note keeps counting the
  `none` grade only.
- The plants-engagement gate recorded on 2026-09-15 applies to *new context layers*. The ownership
  graph, asset and company pages and placement grades serve proposals directly and are not gated.
- Line layers (pipelines, transmission) are pre-built vector tiles on the tiles bucket (ADR 0007);
  the `asset` row for a line carries the line geometry for pages and joins, not for drawing.
- Measured facts to record when implemented: row counts per asset type, the share of proposals in
  each placement grade, and the size of the regions payload at the default zoom.
