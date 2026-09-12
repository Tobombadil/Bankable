# Cross-discipline standards

**Status:** Sprint 1 deliverable, v1 · 2026-09-12 · owners: product-manager, solutions-architect, product-designer
(jointly) · reviewed by: owner
**Authority:** `docs/00-PLAN.md` decisions log, 2026-09-12: "every later deliverable is reviewed against it".
**Inputs:** `CLAUDE.md`, `docs/03-agent-operating-model.md` §2, `docs/10-prd-mvp.md` §4–§8, `docs/20-architecture.md`,
`docs/21-data-model.md`, `docs/23-api-spec-outline.md`, `docs/adr/0001–0006`, `docs/11-market-and-competition.md` §3,
`docs/13-legal-data-rights.md` §5.4, `docs/32-social-operating-playbook.md` §3–§4, `docs/33-gtm-and-sales-playbook.md` §8.

## 0. How to use this document

1. **Every rule has an id** (`P-n` product, `D-n` design, `E-n` engineering, `DA-n` data, `API-n` API, `S-n`
   security, `O-n` operations, `G-n` go-to-market, `R-n` review). Reviewers and authors cite ids (§9).
2. **MUST / SHOULD.** MUST blocks "done". SHOULD needs one sentence of justification to depart from.
3. **This document references, it does not restate.** The referenced section is normative; this document adds
   the rule and the check. Where docs disagree, §10 picks; anything unlisted there is a defect here — raise it.
4. **Precedence:** `CLAUDE.md` guardrails > `docs/00-PLAN.md` decisions log > this document > phase docs > code
   comments. A rule change is logged in `docs/00-PLAN.md` first, then edited here with a `docs/CHANGELOG.md` line.
5. Rationale is one line per rule, marked *Why*. If it stops being true, the rule is up for review.
6. **Naming.** "Bankable" is the repo codename, not the product name. Prose says "the platform"; where a
   literal name or hostname is unavoidable, `{{PRODUCT}}` and `{{DOMAIN}}` stand in until the owner names it.

## 1. Product and specification

**P-1 Every requirement is a user story with testable acceptance criteria.** Format is `docs/10` §4: stable id
(`US-nnn`, never renumbered), "As a {segment}, I want … so that …", then `AC1…ACn`, each phrased so QA can write
the test without asking the author (a fixture, a number, a state, an observable output). *Why:* the qa-engineer
turns ACs into tests directly (`docs/03` §2); an untestable AC is a conversation deferred to release week.

**P-2 Every story names its segment** from `docs/10` §2 (S0–S6; S0 is the **operations team** — the PRD's
"routing team" persona is renamed per owner direction 2026-09-12, and no rule may presume the existing deal
workflow is part of this platform — it is a later consideration, `docs/00-PLAN.md`). A feature that serves no listed job is out of
scope until the PRD adds the job. *Why:* the product-manager brief — "name which user every feature serves".

**P-3 Every metric names a source of truth** a machine can compute (a table, a log, a CRM query), in the
`docs/10` §5 shape: id, metric, source of truth, target, kill/review signal, origin. "We'll estimate" is not a
source. *Why:* M-1…M-13 are the kill signals of `docs/01` §6 and must compute without judgement.

**P-4 Acceptance criteria state tier and licence behaviour** wherever a surface shows source data: which tier
sees it, what is withheld, and the fixture that proves it (pattern: US-101 AC3/AC4, US-906). *Why:* M-11 is
zero breaches; a story silent on tier is a story that leaks.

**P-5 Scope changes go through one process.** (a) Author writes the change as a PRD diff (story added, cut or
changed, with affected ACs and metrics). (b) product-manager assesses against `docs/10` §3 and the sprint exit
criteria in §6. (c) Decision logged in `docs/00-PLAN.md` with rationale; `docs/10` §9 gets a row;
`docs/CHANGELOG.md` a line. (d) Moving a gated item (`docs/10` §3.3) out of "gated" needs legal-compliance
sign-off recorded in `data/sources.yaml`. No scope change by pull request alone. *Why:* the PLAN is the
project's memory; sessions are not.

**P-6 Decision logging.** Any choice a later agent could plausibly re-derive differently is a decision: log it in
`docs/00-PLAN.md` (date, decision, rationale) the day it is made; material technical choices also get an ADR
(ADR 0001 rules 3–5). A decision only in a chat transcript does not exist. *Why:* ADR 0001 context.

**P-7 Assumption tagging.** Assumptions carry an id in the doc that depends on them (`A-n` in `docs/10` and
`docs/20`, `D-n` in `docs/21`, `P-n` in `docs/23`), each stating what is assumed, which open owner question or
missing evidence it depends on, and what changes if wrong; new docs use the next free prefix and list
assumptions in their final section. *Why:* `CLAUDE.md`; the owner's answers must be mechanically re-plannable.

**P-8 Numbers carry confidence and a source.** Any quantitative claim states its source URL and retrieval date,
or is marked as an assumption with calibrated confidence (`docs/11` style: high / moderate / low, with reason).
*Why:* "evidence first, calibrated confidence" (`CLAUDE.md`).

**P-9 Definitions of done are written before the work starts.** The product-manager writes a numbered DoD per
deliverable (`docs/10` §8 is the model); the reviewer in `docs/03` §2 writes "met / not met (blocker)" per item;
silence means not met. *Why:* the only way a rotating team agrees on "done".

**P-10 Doc hygiene.** One topic per file, numbered by phase (`CLAUDE.md`); status line at the top (status, date,
owner, reviewer, inputs); section numbers stable once cited (append, never renumber — `docs/21` §10 C-1/C-2
show the cost); external sources cited with URL and date; British spelling in prose (`licence`, `normalise`),
identifiers exactly as `docs/21` spells them. *Why:* cross-references are the project's navigation.

## 2. Design and UX

### 2.1 Principles for a data-dense B2B product

**D-1 Density over decoration.** The user scans hundreds of rows to find five. Default views show more rows
with less chrome: 40–48 px table rows on desktop, no card grids for tabular data, no hero imagery on working
screens. *Why:* the ICP reads registers for a living (`docs/11` §6); finding the row is the problem.

**D-2 Evidence is visible, not hidden.** Every value can be traced to a source in ≤ 1 click: a provenance
affordance (source name, retrieved date, licence badge, link out) sits on the row, chip, marker or field, not
only in a footer. *Why:* provenance discipline is the differentiation (`docs/10` §1.2) and a guardrail.

**D-3 Delayed means visibly delayed.** Every public-tier surface carries the `data_as_of` / `lag_days` line from
the API envelope (`docs/23` §10) in a fixed position (page header on lists and map; record header on detail;
top of digest) with the "live in Pro" link (US-604, US-201 AC4). *Why:* the delay is the paid tier's value.

**D-4 One mental model across search, map and feed.** Same filter set, same URL parameters, same tier rules,
same result count; switching list ↔ map ↔ feed keeps filter state (US-102 AC2, US-104 AC2). *Why:* the owner
made the map a primary surface; a second filter grammar splits the product in two.

**D-5 Never colour alone.** Every state, tier and licence class is carried by a label or icon as well as colour.
*Why:* WCAG 2.2 SC 1.4.1; colour is also where chips break under dark mode.

**D-6 No default AI aesthetic; a documented reference study comes first.** After the name is chosen
(`docs/00-PLAN.md`: naming precedes the study) and before any screen is drawn, product-designer delivers
`docs/30-design-references.md`: a study of at least eight of the best interactive data
and map products (covering mapping/GIS tools, financial and energy data terminals, government data portals,
data-journalism interactives, developer documentation sites), each entry naming product, URL, date viewed,
what the platform takes (layout, density, interaction, typography, motion), what it avoids, and a screenshot
reference by link (no copied assets). Typefaces, spacing scale, type scale and page layouts in
`docs/31-design-system.md` derive from that study and cite it. *Why:* owner instruction 2026-09-12; a product
sold on provenance cannot look like a template.

**D-7 Banned patterns.** A reviewer rejects any screen, canvas or template showing: generic sans-serif type on
purple or blue gradients; uniform rounded cards in a three-tile hero; emoji as bullets or icons; stock
illustration or photography; glassmorphism or frosted panels; decorative blur, glow or shadow stacks; a
marketing hero above a working screen; auto-playing or looping animation; or anything the reviewer would call
"template". The rejection cites D-7 and the pattern. *Why:* owner instruction 2026-09-12.

### 2.2 The map as a primary navigation surface

**D-8 Everything with a place is on the map.** Proposals and opportunities are browsable on `/map` with the
same filters and tier rules as the lists (D-4). Placement precedence: `location.precision = exact` → point;
`county_centroid` → point at the centroid, marked as such; state only → listed under the state in the side
panel and drawn as a state count; no geography → listed under "Unplaced" with its count, never silently
dropped (US-104 AC1, `docs/21` §3.7). *Why:* US-104 AC1 and the PLAN decision.

**D-9 Restricted-precision rule.** Records whose geometry provenance is a source with `allows_raw_publication =
false` or `reuse_class ∈ {restricted, unknown}` never render an exact point on a non-admin surface; the county
centroid is used and the drawer says "location shown at county level (source licence)" (US-104 AC3, `docs/21`
D-7 and §8 `precise_geo`). *Why:* an exact coordinate is a raw field.

**D-10 Clustering by lifecycle state and technology.** Clusters are computed server-side by
`/v1/proposals/geo?bbox&zoom` (`docs/23` §7) as features with `{count, lifecycle_state_counts,
technology_counts, capacity_mw_sum}`. Glyphs encode count (size), dominant lifecycle state (chip colour +
label) and technology (icon); hover/focus shows the breakdown. Clusters split into markers at a configured
threshold (initial: ≤ 500 visible records), not a constant. *Why:* 20k+ records cannot be individual markers;
a cluster that hides state and technology hides the product.

**D-11 Opportunities draw as service territories.** An opportunity whose issuer has a polygon
(`location.kind = service_territory`) is drawn as a hatched polygon with an outline in the opportunity colour
and a centroid label; overlaps stack by `due_at` ascending on top; no polygon → D-8 precedence. Polygon sources
carry the provenance quartet like any record. *Why:* S3 job (a) — "which proposals in my territory".

**D-12 Detail drawer with provenance.** Selecting a marker, cluster item or polygon opens a drawer (not a page)
showing canonical fields, lifecycle chip, `Sources` list (name, retrieved date, licence badge, link out; gated
sources omitted per `docs/21` §8 item 3), the last three events, the delayed-tier line (D-3) and a link to the
full record. The drawer is `role="dialog"` with focus trap; closing returns focus to the marker. *Why:* US-201
AC1 without leaving the map.

**D-13 Map performance budget** (measured on the seeded dataset, ≥ 20,000 placed records): first map paint
within the page LCP budget (D-31); a viewport request returns ≤ 2,000 features and ≤ 300 KB gzipped; pan/zoom
to updated markers ≤ 200 ms; canvas/WebGL rendering (MapLibre GL JS or equivalent), never one DOM node per
marker above 500; map bundle ≤ 250 KB gzipped including the library; basemap tiles self-hosted or from a
provider whose terms permit commercial use, with attribution rendered (OpenStreetMap ODbL,
https://www.openstreetmap.org/copyright). *Why:* the heaviest page is the one the owner made primary.

**D-14 Keyboard and screen-reader access to map content.** A synchronised "in view" results list is the
accessible path to the same records, reachable by a skip link. Map controls are named buttons; arrow keys pan,
`+`/`-` zoom, `Enter` opens the focused marker; markers and clusters are focusable (roving tabindex) when
≤ 500 are rendered, otherwise the list is the path. An `aria-live="polite"` region announces "N proposals, M
opportunities in view" after each move (debounced 500 ms). Marker accessible name: `{name}, {lifecycle_state},
{capacity} MW, {county}, {state}`. *Why:* WCAG 2.2 SC 2.1.1, 4.1.3; the map is never the only way to anything.

**D-15 Motion on the map and feed.** `flyTo` ≤ 600 ms ease-out; cluster split/merge ≤ 300 ms; drawer open
200 ms, close 150 ms; feed inserts never move content being read — new events sit behind an "N new events"
control, no auto-scroll; no looping or ambient animation. `prefers-reduced-motion` makes every transition
instant. *Why:* WCAG 2.2 SC 2.3.3; a moving table is unreadable.

### 2.3 Information architecture

**D-16 Three surfaces, one URL scheme.** Public (`{{DOMAIN}}`), Pro (same host, entitlement-gated routes),
Admin (`admin.` host, `docs/20` §7). Canonical routes: `/proposals`, `/proposals/{slug}`, `/opportunities`,
`/opportunities/{slug}`, `/organizations/{slug}`, `/map`, `/search`, `/feeds/*`, `/alerts`, `/account`,
`/docs/api`; slugs per `docs/21` §3.1; merged slugs 301 to the survivor (US-201 AC3). Primary nav is identical on
all public and Pro pages (Proposals · Opportunities · Map · Feed · Alerts · API); admin nav is `docs/20` §8's
list in that order; depth ≤ 3 to any record; breadcrumbs on record pages. *Why:* stable URLs are the SEO asset.

**D-17 Every filter, sort, tab and map viewport lives in the URL** as `docs/23` §7 parameters; a copied URL
reproduces the view on the same tier; no filter state held only in local storage. *Why:* US-102 AC2; sharing
a view is the S2 workflow.

**D-18 Detail pages are the unit of SEO**: server-rendered with canonical fields, `Sources` panel, timeline and
attribution line in HTML (no client-side rendering of primary content); `<link rel="canonical">`, Open Graph
(used by social embeds, `docs/32` §1.3) and JSON-LD where accurate. *Why:* `docs/20` §15; posts link here.

### 2.4 Tokens, typography and colour

**D-19 Tokens are the only way to reference colour, type, space, radius and motion.** `docs/31` defines them as
CSS custom properties in one file (`--color-*`, `--font-*`, `--space-*`, `--text-*`, `--radius-*`,
`--shadow-*`, `--motion-*`); components never use raw hex or px for these. *Why:* dark mode, contrast fixes
and brand changes become one-file edits.

**D-20 Brand palette and verified contrast** (light theme; dark swaps roles; ratios by the WCAG 2.x formula):

| Token | Value | Use | Contrast |
|---|---|---|---|
| `--color-ink` | `#16324f` slate navy | text, primary surfaces | 13.1:1 on white; 11.9:1 on paper |
| `--color-paper` | `#f6f4ef` | page background | — |
| `--color-copper` | `#a8571c` | accent, primary action, links ≥ 16 px | 5.2:1 on white, 4.7:1 on paper; **2.5:1 on ink — fails** |
| `--color-copper-deep` | `#8a4614` | small text and links on light | 7.1:1 on white |
| `--color-copper-tint` | `#e0b58a` | accent and links on ink / dark | 7.0:1 on ink |
| `--color-muted` | `#5b6b7c` | secondary text on light | 5.5:1 on white (never lighter than `#6b7a8a`, 4.4:1) |

Copper never appears as text on navy; copper-tint does. Every new text pair is checked at 4.5:1 (normal) or 3:1
(≥ 24 px / 18.66 px bold, and UI components — SC 1.4.3, 1.4.11) and the ratio is written beside the token in
`docs/31`. *Why:* computed, not assumed; copper-on-navy is the pairing designers reach for and it fails.

**D-21 Typefaces and licensing.** Display: Newsreader; UI and body: IBM Plex Sans; identifiers, numbers, code:
IBM Plex Mono — all SIL Open Font License 1.1 (https://openfontlicense.org/). `docs/31` records exact version
and file names; the licence file is vendored beside the fonts; fonts are self-hosted (no third-party font CDN
in production); variable-font files are used wherever the family ships one. Adding or replacing a family
requires the licence check recorded in `docs/31` first. *Why:* brand context; a typeface licence surprise is an
avoidable launch blocker.

**D-22 Modular, documented scales.** Type: base 16 px, ratio 1.2 (12.8 / 16 / 19.2 / 23 / 27.6 / 33.2 / 39.8),
display steps in Newsreader from 27.6 up; line height 1.5 body, 1.25 display; `font-variant-numeric:
tabular-nums` in tables and chips. Space: 4 px base, steps 4/8/12/16/20/24/32/40/48/64 (`--space-1…12`). Both
scales, the layout grid (columns, gutters, max width per breakpoint) and their rationale from
`docs/30-design-references.md` are written in `docs/31`; any off-scale value carries a comment. *Why:* owner
instruction 2026-09-12; consistency is what makes density readable.

**D-23 Dark and light themes are both first-class.** Every token has a value in both; theme follows system
preference with a manual override; contrast verified in both (D-20). *Why:* operators run admin for hours.

### 2.5 Data-display rules

**D-24 Tables.** Columns and default sort per the PRD ACs (US-101 AC1, US-301); numeric columns right-aligned
with units in the header; identifiers in Plex Mono; sticky header; row-level provenance affordance (D-2);
sortable columns are exactly the API allowlist (`docs/23` §7); page size 50 with cursor pagination (no "page 7
of 40"); states per D-29. *Why:* the table and the API must agree.

**D-25 Status chips per lifecycle state.** One chip component; vocabulary exactly `docs/21` §7.1 plus
`under_construction` (§10 item 3) and `unknown`; each state has a fixed token pair, icon and label (D-5);
`unknown` is visibly neutral and reveals `status_raw` on hover/focus only where the licence allows raw
(`docs/21` §8); opportunity chips use `docs/21` §7.2. *Why:* the most repeated element — one component, one
vocabulary.

**D-26 Timelines.** Newest first; each item shows type, `observed_at`, source with licence badge, `before →
after` for status changes; merge/unmerge labelled and linked to the absorbed record (US-202 AC1–AC2); public
tier hides items newer than the lag and shows the D-3 banner in their place. *Why:* US-202.

**D-27 Provenance panel.** Lists every visible `proposal_source` / `opportunity_source` with source name,
`source_record_id` where allowed, `retrieved_at`, licence badge, link out (US-201 AC1); gated sources omitted,
not greyed (`docs/21` §8 item 3); Pro+ detail pages expose `field_provenance` (`docs/21` §3.1). The
`licence_summary.attribution_line` renders in the footer of every list, detail, map and feed view (US-105
AC1). *Why:* the API supplies it, the page prints it; omission is launch-blocking (`docs/21` §8).

**D-28 Delayed-tier notices** use one component and wording across banner, drawer, RSS item, CSV header and
post: "Public data is {lag_days} days delayed (as of {data_as_of}). Live in Pro." (US-604, `docs/32` §3.2 item
5). *Why:* one sentence the market learns to recognise.

**D-29 Empty, loading and error states are specified per screen** in `docs/31`: empty states say which filter
removed the last result or offer "clear all" (US-102 AC4); loading uses skeletons of the final layout (no
spinners over tables — prevents CLS); errors show the RFC 9457 `title` and `request_id` (`docs/23` §8), never a
stack trace; a gated record is indistinguishable from not-found (`docs/23` §8). *Why:* these states are most
of what a new user sees in the first minute.

### 2.6 Accessibility, responsiveness, performance

**D-30 WCAG 2.2 AA is the bar** (https://www.w3.org/TR/WCAG22/) on every public, Pro and admin screen including
the map (D-14). DoD checks: contrast (D-20); target size ≥ 24 × 24 CSS px (SC 2.5.8); visible focus (SC 2.4.11);
no keyboard traps; single-pointer alternative to drag (SC 2.5.7 — map pan buttons); form errors in text;
accessible names on every control and marker; `axe` scan with zero serious/critical findings in CI plus a
manual keyboard pass per screen. *Why:* frontend brief; the free tier is an SEO product.

**D-31 Core Web Vitals at p75** on the proposal detail, list and map pages: LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1
(https://web.dev/articles/vitals), measured with Lighthouse CI on preview and field data once live. JavaScript
budget: public non-map pages ≤ 50 KB gzipped first-party JS (htmx + islands, `docs/20` §15); map page ≤ 250 KB
including the library (D-13); admin measured but unbounded. *Why:* CWV is a ranking input; the budget keeps the
architecture server-rendered.

**D-32 Responsive to 400 px.** No horizontal page scroll at 400 px: tables collapse to key/value lists or scroll
inside their own container; the map keeps the results list as a bottom sheet; side gutters ≥ 16 px; no fixed
width wider than the viewport. Breakpoints 400 / 720 / 1080 / 1440. *Why:* alerts are read on phones.

### 2.7 Content style

**D-33 Plain, evidence-first, no adjectives.** Product copy follows the `docs/32` §3.4 style guide (banned
words, units, dates, no exclamation marks, no emoji, no hashtags outside the two social exceptions): what a
register says, when, and where. *Why:* one voice across page, alert, post and email.

**D-34 Attribution on every data element**, not every page: a chart, count, chip, cluster, alert line and CSV
row each carry or link to their source; aggregates say "computed over visible sources" where gated sources are
excluded (`docs/21` §8 item 4). *Why:* the guardrail is per record; aggregation must not launder provenance.

**D-35 Microcopy is versioned strings**, not inline literals: disclosure text (`docs/32` §2.2, `docs/33` §8),
the delayed notice (D-28), attribution formats and error titles live in one strings file with a version; the
review queue cannot edit attribution or disclosure strings (`docs/32` §3.4). *Why:* legal wording changes in
one place with an audit trail.

### 2.8 When to use the design canvas

**D-36 Use the `design` skill canvas** when the owner must judge layout or interaction visually: the map page
and drawer, the proposal detail page, list density options, the admin review queue, the social post cards. Do
not use it for tokens, rule tables, flows or IA — Mermaid or ASCII in `docs/30-*`/`docs/31-*` is sufficient
and diffable. A canvas is linked from the doc it illustrates; the doc remains the record; a canvas that breaks
D-6/D-7/D-20 is rejected like any artifact. *Why:* canvases are review aids, not sources of truth.

## 3. Engineering

### 3.1 Repository layout

**E-1 Layout follows `docs/20` §2's component table**, each path independently testable:

```
api/openapi.yaml            generated, committed, CI-checked (docs/23 §12)
data/sources.yaml           connector manifest — single source of truth for sources
data/eval/                  labelled samples, evaluation reports (DA-8, DA-9)
docs/                       numbered by phase (CLAUDE.md)
infra/                      OpenTofu modules, Compose per environment, SOPS-encrypted secrets (ADR 0005)
pipeline/connectors/<source_id>/   connector.py, status_map.yaml, fixtures/, test_connector.py
pipeline/{snapshot,diff,normalize,resolve,enrich}/
services/{store,api,alerts,social,sor,modelgw}/
web/                        templates, static assets, islands; web/admin for the admin panel
scripts/                    operator and one-off scripts (probe_sources.py)
tests/                      cross-cutting: contract, integration, e2e; unit tests live next to code
```

Tests next to the code they test (`CLAUDE.md`), cross-cutting suites under `tests/`; the publisher modules of
`docs/32` §4.1 (`filter`, `draft`, `validate`, `queue`, `adapters/*`, `metrics`) and `config/social.yaml` live
under `services/social/`; the existing `pipeline/normalize.py` and `pipeline/status_map.yaml` move into this
layout in the Sprint 1 connector framework (per-source `status_map.yaml`, `docs/20` §3.4). *Why:* one image,
many entrypoints (ADR 0002); a contractor finds a connector by `source_id`.

**E-2 Import boundaries are enforced**: `import-linter` contracts in CI forbid `pipeline/*` importing
`services/api`, anything but `services/modelgw` importing a model-provider SDK (`docs/20` §4.5), and anything
but `services/sor/adapters` importing a CRM or billing SDK (ADR 0006). *Why:* the boundaries carry the cost and
licence controls.

### 3.2 Language, framework and style

**E-3 Python 3.12, FastAPI + Pydantic v2, SQLAlchemy 2.0 + Alembic, httpx + tenacity, Playwright, pandas /
pyarrow / openpyxl / pdfplumber** per ADR 0002 and `docs/20` §15; deviations need an ADR (ADR 0001 rule 3).
Interpreter pinned in `.python-version`; dependencies declared in `pyproject.toml` and locked with hashes into
`requirements.txt` (`pip-compile --generate-hashes`), so the `CLAUDE.md` install command stays true. *Why:*
one runtime, one lockfile.

**E-4 Conventions.** Async for I/O in connectors and the API; sync pandas in parse and resolve; SQLAlchemy 2.0
typed API only; Pydantic models are the API contract and the OpenAPI source; domain field names are `docs/21`
spellings verbatim (`licence_id`, `lifecycle_state`, `public_at`); `snake_case` modules; no `print` (ruff `T20`);
UTC-aware datetimes only (ruff `DTZ`); money as `Decimal` (`docs/21` §1). *Why:* the ORM and the API generator
enforce the rest if the code stays typed.

**E-5 Lint and type gates.** `ruff` rule sets `E, F, W, I, N, UP, B, S, C4, DTZ, T20, RUF`, line length 110,
`ruff format`; `mypy --strict` on `pipeline/` and `services/`, `ignore_missing_imports` only per named untyped
module (gridstatus) in `pyproject.toml`; `# type: ignore[code]` only with a code and a reason. Clean before
commit (`CLAUDE.md`), blocking in CI (O-3). *Why:* strict typing is the cheapest review the team has.

### 3.3 Testing pyramid (named minimums)

**E-6 Unit tests for every parser with recorded fixtures.** Each connector ships ≥ 1 fixture per file format and
layout variant, plus one "HTTP 200 with an error body" fixture where the source has that failure (FERC
`success:false`, EIA index HTML — `docs/02` §7); `parse()` runs on fixtures only, `fetch()` never runs in CI
(`docs/20` §3.1). Fixtures ≤ 1 MB, trimmed, with a `README` line (source URL, retrieval date), no personal data
(DA-13), no rows from `restricted`/`unknown` sources except to prove the gate (US-906). *Why:* parser regressions
are the dominant failure mode (`docs/20` §12).

**E-7 Coverage floors.** ≥ 80 % lines on `pipeline/` and `services/`; 100 % branches on the visibility predicate,
licence-gate and redaction modules (`docs/21` §5.4, §6.6, §8); reported per package; a drop blocks merge.
*Why:* the gate modules are where a miss is a legal event.

**E-8 Contract tests against OpenAPI 3.1.** `api/openapi.yaml` (https://spec.openapis.org/oas/v3.1.1) is
generated at build and compared byte-for-byte with the committed file; a property-based suite (schemathesis or
equivalent) exercises every operation for schema conformance, the §10 envelope, RFC 9457 bodies and rate-limit
headers; every example response is itself validated (US-704 AC1). *Why:* `docs/23` §12 — contract and API
cannot drift.

**E-9 Integration fixtures for tier and gate.** The seeded database holds ≥ 1 record per `publish_state` and
per `reuse_class`, one at lag−1 day and one at lag+1 day, one mixed-provenance proposal (ERCOT + PJM), one
merged pair; tests assert visibility on web, RSS, API, export, webhook and post-draft paths (US-601 AC1, US-906
AC1, `docs/21` §8). *Why:* M-11 = 0 is proved here, not in review.

**E-10 End-to-end smoke** (Playwright; on preview and after every production deploy, ≤ 10 min): public list →
detail → attribution rendered; map → cluster → drawer → provenance; queue-id search returns the record first
(US-103 AC1); Pro login → saved search → alert preview; admin publish of a gated source refused with
`gate_unmet` (US-905 AC1); no draft from a gated record (US-801 AC3). *Why:* US-908 becomes executable.

**E-11 Migrations are tested both ways** on empty and seeded databases (`upgrade head`, `downgrade -1`,
`upgrade head`); an irreversible migration says so in its docstring and ships expand/contract (add, backfill,
switch, remove in separate releases). *Why:* rollback (O-5) depends on it.

**E-12 DQ gates (DA-6) and evaluations (DA-9) run in CI** against fixtures and fail on threshold regression; no
test is skipped or quarantined to go green (qa-engineer brief). *Why:* a flaky gate is no gate.

### 3.4 Code review

**E-13 Review checklist** (ticked per PR; "n/a" needs a reason):
1. Story/AC ids named; the change does what the AC says and nothing else.
2. Provenance quartet on every new stored record type; visibility predicate on every new read path.
3. Nothing raw, precise-geo or identifying from a `restricted`/`unknown` source reaches a non-admin surface.
4. Tests at the right level (E-6…E-11); fixtures recorded, not live.
5. Types and lint clean; no new `type: ignore` without a code; errors RFC 9457 with a `docs/23` §8 code.
6. Logs structured; no secrets, personal data, prompts or model identifiers (E-18).
7. Migration reversible or expand/contract; index changes justified against `docs/21` §5.2.
8. Performance budget (E-16) unaffected or re-measured.
9. Docs, `docs/CHANGELOG.md`, decisions log and ADR (if a `docs/20` §15 row changes) updated.
10. No model identifiers or provider names in code, comments, fixtures, commit messages or artefacts.
*Why:* a checklist a reviewer can run in ten minutes beats a norm nobody checks.

**E-14 Review routing** is `docs/03` §2 (R-2): at least one reviewer other than the author; the qa-engineer
signs releases, not PRs; no self-merge to `main`. *Why:* the operating model already assigns reviewers.

### 3.5 Commits, branches, secrets, dependencies, budgets

**E-15 Commits and branches.** Conventional Commits (https://www.conventionalcommits.org/en/v1.0.0/):
`type(scope): subject`, types `feat, fix, data, docs, test, refactor, perf, build, ci, chore`, scope = top-level
path or `source_id`; body cites `US-nnn`, ADR or rule ids. Branches `type/short-slug`; `main` protected; squash
merge; PR title = commit subject. **No model identifiers, model names, versions or provider names in any commit
message, trailer, branch name, code comment, docstring, fixture, log line, test name or shipped artefact**
(`CLAUDE.md`); `docs/CHANGELOG.md` lines name the *agent role* (`data-engineer:`), which is how build-time
attribution is recorded (`docs/03` §6). *Why:* the guardrail is absolute and covers trailers.

**E-16 Performance budgets** (CI against the seeded 10⁵-record database; production dashboards): API p95 — list
≤ 300 ms, detail ≤ 200 ms, search `q=` ≤ 500 ms (US-103 AC3), `/geo` ≤ 400 ms, bulk first byte ≤ 1 s; search
p95 > 300 ms sustained a week triggers `docs/20` §13 step 4; pages per D-31; `fetch` jobs ≤ 10 min, browser jobs
≤ 5 min (`docs/20` §4.2). A > 10 % regression needs a PR comment and, if accepted, an exception (R-6). *Why:*
budgets without measurement are wishes.

**E-17 Error handling.** API errors are RFC 9457 (https://www.rfc-editor.org/rfc/rfc9457) exactly as `docs/23`
§8; a new `code` edits `docs/23` §8 first. Internally, typed exceptions per layer (`ConnectorError`, `ParseError`,
`GateViolation`, `SorUnavailable`, `BudgetExceeded`); a stage fails closed (`docs/20` §3.7 transactions) and
records the failure on `source_run`; a gate violation is never caught-and-continued. *Why:* a swallowed gate
error is a leak with no log.

**E-18 Logging and tracing.** Structured JSON to stdout (`docs/20` §10) with keys `ts, level, event, service,
env, request_id, trace_id, job_id, source_id, run_id`; OpenTelemetry traces (API sampled, jobs full) with
`traceparent` carried in job payloads so request → job → model call is one trace; semantic conventions
(https://opentelemetry.io/docs/specs/semconv/). Never logged: secrets, session/key material, raw payloads from
`restricted`/`unknown` sources, personal data, prompt or completion text (the gateway keeps a hash and the
`model_call` row), provider model ids (aliases only). Retention 14 days. *Why:* observability that leaks is
worse than none.

**E-19 Secrets.** Environment variables injected at deploy from SOPS + age files under `infra/` (ADR 0005); never
in code, fixtures, tests, notebooks, screenshots or canvases; `gitleaks` blocks commit and CI; `.env` files
git-ignored and never hold production values; keys and webhook secrets shown once and stored hashed (`docs/23`
§5); every secret has an owner, environment and rotation date (S-7). *Why:* `docs/20` §11.

**E-20 Dependencies.** Pinned with hashes (E-3); Dependabot weekly; `pip-audit` blocks on fixable
vulnerabilities; a new direct dependency needs an OSI licence compatible with commercial use (no AGPL in
services without an ADR), ≥ 1 year of releases or a stated reason, and a one-line PR justification; base images
pinned by digest and scanned (`docs/20` §11). *Why:* the least-watched attack surface for a solo operator.

**E-21 Feature flags and tier gates.** Tier and licence behaviour is *data* — `source.publish_state`, `licence`
flags, lag configuration (`docs/21` §3.19, §5.4) — never a code flag. Code flags (`settings.features.*`,
env-driven, boolean, default off) may hide an unfinished surface or narrow what a tier sees, never widen
visibility or bypass the predicate (a test asserts the predicate applies regardless of flag state); each flag
has an owner, purpose and removal date ≤ 2 sprints after rollout; no third-party flag service. *Why:* a flag
that can widen visibility is a gate with two keys.

## 4. Data

### 4.1 Canonical schema discipline

**DA-1 `docs/21` is the schema; the Alembic migration is its executable twin.** A field, vocabulary value, index
or constraint exists only in both; a PR changing one changes the other. `docs/21` §1 conventions (uuid v7,
`public_id`, `timestamptz` UTC, `numeric` money, `text + CHECK` enums, `jsonb` raw) apply to every new table.
*Why:* `docs/21` preamble — "must not diverge silently".

**DA-2 Provenance and licence on every record, per field on fused entities.** Every row originating outside the
platform carries `source_id, source_url, retrieved_at, licence_id` (the `CLAUDE.md` quartet; store column
`licence_id` per `docs/21`; exports render `licence` plus the summary header, `docs/23` §10); fused entities
carry `field_provenance` for every canonical field (`docs/21` §3.1); `licence_id` on an observation is immutable
(invariant L2). A source-data table without the quartet fails the schema test. *Why:* the gate works at field
granularity or not at all (`docs/21` §8 mixed-provenance case).

**DA-3 Append-only events; nothing deleted.** `event` is append-only with the trigger and grants of `docs/21`
§6.1; entity tables are a rebuildable fold (§6.5); unpublish, withdraw, takedown and correction are events; the
only destructive operation is redaction (§6.6), itself an event; idempotency keys make re-runs free (§3.10).
*Why:* "the event log is the product".

**DA-4 Merges are reversible by construction.** A `merged` event satisfies invariant M1 (`docs/21` §6.3) or is
rejected in code and tests; human decisions win and are recorded (§6.4). *Why:* wrong merges are visible to
sponsors (`docs/10` A-9).

**DA-5 Status harmonisation is versioned data.** Source status → lifecycle mappings live in `status_map.yaml`
next to each connector (`docs/20` §3.4, `docs/21` §7.4) with `version`, `updated`, per-rule `id` and `note` as
`pipeline/status_map.yaml` already does; vocabulary is `docs/21` §7.1 plus `under_construction` (§10 item 3)
and `unknown`; unmapped values → `unknown` with a DQ warning, never coerced; a mapping change bumps `version`,
is reviewed by data-scientist and triggers a `reprocess` (`docs/21` §6.5) that writes new events rather than
rewriting old ones. *Why:* a judgement call that changes should not need a deploy.

### 4.2 Quality gates, resolution, retention

**DA-6 Data-quality gates run at the end of every source run** and write `source_run.dq` (`docs/20` §10):

| Check | Warning | Hold (snapshot stored, diff **not** applied, task opened) |
|---|---|---|
| Row-count drift vs previous successful run | ± 10–30 % | > 30 % either direction (`docs/20` §12) |
| Vocabulary drift (`status_raw`, `technology_raw`, kind) | any new value | unmapped > 5 % of rows |
| Null spike on a required canonical field | +5 pp vs trailing median of 5 runs | +10 pp |
| Duplicate `source_record_id` within one snapshot | — | any (parser must dedupe deterministically or document a composite key) |
| Provenance completeness | — | any row missing the quartet |
| Schema drift (source columns added/removed) | any | a removed column that feeds a canonical field |

A hold is released by a human from the task queue (US-907) with a reason; `removed` events are never emitted
from a held run; thresholds are per-source configuration with these defaults. *Why:* silent partial files
produce false withdrawals (`docs/20` §12).

**DA-7 Entity-resolution rules.** Keys in `docs/02` §5 order (EIA id → queue id + ISO → FERC docket → sponsor +
county + capacity ± 10 % + technology → fuzzy name); deterministic links at confidence 1.0; ambiguous candidates
between configured thresholds go to the model gateway with pair and rationale logged (`docs/20` §3.5); a human
`resolution_decision` short-circuits later automation on that pair (`docs/21` §6.4); `link_method` and
`link_confidence` on every link (§3.2); a resolver change ships only with a re-run evaluation (DA-9). *Why:*
precision ≥ 0.9 is a product commitment (M-2).

**DA-8 Labelling protocol.** `data/eval/labels.csv` columns: `pair_id, left_id, right_id, label ∈ {match,
non_match, unsure}, key_type, labeller, labelled_at, evidence_url, note`; stratified by key type, ≥ 500 pairs,
≥ 100 hard negatives (same sponsor or county, different project); 10 % double-labelled with Cohen's κ ≥ 0.8
before use, else revise the guide and relabel; `unsure` excluded from metrics and reported; labels never edited
in place — a correction is a new row superseding by `pair_id` + later `labelled_at`. *Why:* a metric on an
unreliable label set is a number, not evidence.

**DA-9 Evaluation before shipping.** Every resolver, extraction or matching version has
`data/eval/reports/<version>.md` with measured (never estimated) precision, recall, per-key-type breakdown,
confusion examples and the reproduction command; ship gates: precision ≥ 0.9, recall ≥ 0.7 (`docs/10` A-9);
per-field extraction thresholds set before auto-accept (`docs/20` §3.6); model re-ranking of matches only with
a reported measured gain (`docs/10` §3.1). *Why:* data-scientist brief — "report measured numbers".

**DA-10 Retention.** Raw snapshots 24 months then monthly samples, `snapshot` rows forever (`docs/20` §3.2, A-7);
documents per their licence flags; `model_call` prompts 90 days, outputs kept; sessions 30 days; alerts 12
months (`docs/20` §11); retention runs as a scheduled job with its own run log. *Why:* snapshots are the evidence
in a licence dispute.

**DA-11 Publication follows the reuse-class checklist.** Before a source moves to `api_only` or `public`,
invariant L1 holds (`docs/21` §3.19: class `open`/`attribution`, `gate_flag = false`, evidence URL, date and
reviewer) and the surfaces behave per the `docs/21` §8 class table, including the six "may not show" items for
`restricted`/`unknown` and the credit line for `attribution`. Where `docs/13` §6 and `data/sources.yaml`
disagree, legal-compliance corrects the YAML before publication; code never works around it. *Why:* the gate is
a product requirement (US-905) enforced by a `CHECK` and a trigger.

**DA-12 Registry hygiene.** `data/sources.yaml` is the single source of truth for sources: every field-guide field
filled; `verified` updated after each probe; `egress` added per `docs/20` §4.3 (`docs/21` C-6, Sprint 1); a
source without a connector shows as `unimplemented`, never silently absent; private aggregators fail to register
(`docs/20` §4.3). *Why:* the manifest drives scheduling, egress and gating.

**DA-13 Personal-data minimisation** is `docs/13` §5.4 rules 1–7, adopted verbatim: strip contact identifiers at
ingest; keep name/role/organisation only for professional filers, flagged `personal_data: true`; never publish
contact details on any tier; never use ingested personal data for outreach (separate store, no join key);
privacy notice before the first public page; deletion/objection within 30 days with a salted-hash suppression
list that survives re-ingestion; annual review, 24-month purge on withdrawn projects. Submitted intake contacts
are consented and live on `task` + CRM, never `organization` (US-1001 AC2, `docs/21` C-7). The personal-data
inventory is enforced by the US-910 AC2 schema check. *Why:* it moves the platform from "data broker" to
"publisher of project records" (`docs/13` §5.4).

### 4.3 Onboarding a new source

**DA-14 A source ships only through these eight steps, in order**, each leaving an artefact:
1. **Registry entry** in `data/sources.yaml`, every field filled, `reuse` honest (`unknown` if terms unread),
   probe result in `data/probes/`.
2. **Legal review** by legal-compliance: operative clause quoted verbatim with URL and date in the registry and
   `docs/13`; classification; the `licence` row flags (`docs/21` §3.19); counsel flag if needed.
3. **Connector** under `pipeline/connectors/<source_id>/` per `docs/20` §3.1, `source_record_id` strategy in the
   docstring, egress class declared, host politeness limits configured.
4. **Fixtures** per E-6 and `status_map.yaml` per DA-5.
5. **DQ baseline**: three successful runs logged with row counts and vocabulary; DA-6 thresholds set; cost per
   changed record measured (`docs/20` §6).
6. **Resolution check**: which `docs/02` §5 keys it supplies; evaluation sample extended for a new key type (DA-8).
7. **Publication decision** in admin (`publish_state`), exact credit string and link-back rule for `attribution`
   sources, invariant L1 satisfied, US-906 fixture extended for any new class.
8. **Docs**: `docs/02` coverage map updated if coverage changes; `docs/CHANGELOG.md` line.
*Why:* the source that skips step 2 is the one that ends in a takedown letter.

## 5. API

**API-1 `docs/23` is normative; the generated `api/openapi.yaml` must match it** (`docs/23` preamble, §12); Redoc
at `/docs` is the public documentation; every operation carries `x-tier`, `x-stories`, an example and
`x-status: planned` until its sprint. *Why:* the contract is the product for S2 (d).

**API-2 Cursor pagination only** (`docs/23` §7): `limit` default 50, max 200 (1,000 on `/bulk/*`); opaque cursor
valid 24 h; `page.{next_cursor, prev_cursor, has_more}`; `meta.total` only with `include=count`, honest
`total_is_estimate` above 10,000; no offset parameter exists. *Why:* concurrent ingestion (US-101 AC2).

**API-3 Filter grammar** is `docs/23` §7 exactly: `field=value`, `field=a,b` (OR within facet), `field[op]=value`
with `gte, lte, gt, lt, from, to`; facets AND; `sort=-field,field` from the per-resource allowlist; unknown
parameters → `400 unknown_parameter`, never ignored; the web UI uses the same names in its URLs (D-17). *Why:*
a silently dropped filter on a licence-sensitive surface is a leak.

**API-4 Error model** is RFC 9457 with the `docs/23` §8 code table (E-17); `not_found` for gated records
(existence must not leak); `redactions[]` in a `200` envelope for partial withholding. *Why:* `docs/21` §8 item 3.

**API-5 Attribution envelope on every response** — list, detail, feed item, export row, webhook delivery:
`data`, `page`, `meta` (`tier, lag_days, data_as_of, generated_at, request_id, terms_url`), `licence_summary`
(per-source entries plus `attribution_line`), `redactions[]`, per-record `provenance[]` (`docs/23` §10); CSV
carries provenance columns and the `#` header block (US-105 AC2, US-603 AC2). Missing attribution for a source
present in the payload is release-blocking (`docs/21` §8). *Why:* the API states attribution so no client has to.

**API-6 Rate limits and quotas** are the `docs/23` §6 table (from `docs/10` A-8), configurable per plan and key;
`RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`, `RateLimit-Policy` on every response (IETF draft,
https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/); `429` with `Retry-After`; every request
logged with key id, endpoint, status, latency (US-702 AC3). *Why:* M-7's source and the anti-mining control.

**API-7 Versioning and deprecation.** Path-versioned; `v1` frozen at MVP launch. Additive changes (optional
fields, new endpoints, new values in vocabularies documented as open) are non-breaking; removing or renaming a
field, changing a type, tightening validation or changing a default sort is breaking → `v2`. Deprecation:
changelog and `/docs` notice, `Deprecation` and `Sunset` headers (RFC 8594, https://www.rfc-editor.org/rfc/rfc8594),
old version kept ≥ 6 months after the `Sunset` announcement, email to every key that used it in the prior 90
days; vocabulary additions (a new `lifecycle_state`) announced ≥ 30 days ahead. *Why:* US-703 AC3; S2
integrations are internal models nobody wants to rebuild.

**API-8 Keys and scopes** per `docs/23` §5: `bk_live_`/`bk_test_` prefixes, shown once, SHA-256 stored, ≤ 5 per
user (US-701); scopes `read:public, read:live, read:bulk, write:webhooks, admin:*`; `scopes ∩ plan_tier`
evaluated per request from the entitlement mirror (TTL ≤ 15 min); revocation ≤ 60 s; licence acceptance recorded
with version and timestamp at creation (US-704 AC2); `admin:*` never on a customer key. *Why:* keys are the
paid product's boundary.

**API-9 Transport conventions** per `docs/23` §1: `Idempotency-Key` on every mutating endpoint (24 h replay);
`X-Request-Id` on every response and error; public GETs cacheable 300 s with `ETag`, Pro/API `private,
no-store`; HTTPS + HTTP/2; RFC 3339 UTC; `snake_case`; webhooks signed per `docs/23` §5; public ids only —
internal UUIDs never appear in URLs, responses, customer-visible logs or error bodies. *Why:* integrator
defaults; UUID exposure is enumeration surface.

## 6. Security and privacy

**S-1 Threat model (summary; the full model is a Sprint 1 solutions-architect deliverable).** Assets: canonical
store and event log; licence-compliance evidence (snapshots, `licence` rows); customer personal data; API keys,
sessions, webhook secrets; source and social credentials; the model budget; the owner's reputation. Vectors and
the control that answers each: bulk mining of the free tier (rate limits, edge rules); credential stuffing and
session theft (magic links, HTTP-only cookies, revocable sessions); licence disputes (evidence, gates); SSRF and
egress abuse via connector or admin-entered issuer URLs (host allowlist compiled from the registry, `docs/20`
§11); prompt injection from source documents (typed JSON outputs validated against schema, no tool use from
extraction, post drafting never sees source text — `docs/32` §4.2); supply chain (E-20); operator or agent error
(audit events, human gates). Target: OWASP ASVS 5.0 Level 2 for API and admin, Level 1 for public pages
(https://owasp.org/www-project-application-security-verification-standard/). *Why:* each control below maps to
one of these.

**S-2 Authentication.** Passwordless magic link (single use, 15-minute expiry, bound to the requesting browser
by a nonce cookie) and Google sign-in; signed HTTP-only `Secure` `SameSite=Lax` cookies backed by revocable
server rows, 30-day idle expiry (`docs/20` §7, §11); admin needs role `operator`/`owner`, the `admin.` host and a
second factor via the identity provider (`docs/20` A-10); seat limits enforced (US-602 AC2). *Why:* ASVS V2/V3
at L2 without a password database to breach.

**S-3 Authorisation is the visibility predicate plus roles and scopes.** Every read path calls `visible(r, t,
now)` (`docs/21` §5.4); roles `viewer, member, operator, owner` on `user.role`, entitlements on
`account.entitlement` (`docs/21` C-5); key scopes per API-8; Postgres roles `api, worker, admin_api, readonly`
(`docs/20` §11). Authorisation is never decided in templates or client code. *Why:* one predicate, one test.

**S-4 Audit logging.** Every admin write is an `event` with `actor_type = user`, `actor_user_id`, required
`reason` (US-905 AC3), `before`/`after` (US-901 AC2); key issue/revoke, auto-publish toggles, gate clearances and
lag changes are events; append-only like all events. *Why:* the audit chain is the product's own log.

**S-5 Egress control.** Workers reach only hosts allowlisted from `data/sources.yaml` per egress class (`docs/20`
§4.3, §11); no CAPTCHA solving or challenge bypass; residential egress only per source after legal-compliance
records the terms, never for `restricted`; the residential credential exists on exactly one pool. *Why:* the
legal posture of `docs/13` §3 depends on never circumventing an access control.

**S-6 Data protection.** TLS everywhere including database connections; object storage private with pre-signed
URLs (`docs/20` §4.4); personal data limited to the inventory (DA-13); none of it, and no raw restricted
payload, in logs, error bodies, analytics events, canvases or fixtures. *Why:* ASVS V6/V8; the inventory is the
control surface.

**S-7 Key and secret rotation.** Platform secrets (database, object storage, provider keys, social tokens, SOPS
age keys): ≤ 90 days and immediately on personnel change, suspected exposure or vendor incident, by runbook
(O-9) with a dated log under `infra/`. Customer API keys: self-service rotation with overlap; revocation ≤ 60 s.
Webhook secrets: 24 h dual-signature window. Session signing keys: quarterly with a grace window. *Why:* a
rotation never practised fails during the incident that needs it.

**S-8 Deletion requests.** A task type (US-910) with a 30-day SLA (`docs/13` §5.4 rule 6): redaction procedure
(`docs/21` §6.6), sessions and keys revoked, alerts suppressed, CRM/billing deletion through the ports (ADR
0006), suppression hash written, confirmation sent; a test proves the fields are gone from every surface and
export (US-910 AC1); named filers follow the same path for their personal fields. *Why:* a deletion undone by
the next crawl is not a deletion.

**S-9 Incident response.** **S1**: gated or restricted data visible on a non-admin surface (M-11 breach),
credential exposure, an outbound message sent without a human (`docs/33` §8), personal-data exposure. **S2**:
licence-dispute letter, terms change on a published source, API 5xx > 1 % for 30 min, backup failure. **S3**:
single-source failures, cost alerts. S1 response: within 1 h unpublish the source or record (an event), revoke
exposed credentials, preserve evidence, notify the owner; within 24 h notify affected customers or persons
where required; within 5 business days a postmortem in `docs/6x-incidents-YYYY.md` (timeline, cause, what the
gate missed, fixes with PR links) and a `docs/CHANGELOG.md` line; legal-compliance on every S1/S2. *Why:* the
guardrails define an incident; this defines what happens next.

**S-10 Outbound and automation boundary.** No reply, DM, follow or like endpoints exist in the integration layer
(US-804 AC1, tested); `auto_publish` per channel is owner-only and audited (`docs/20` §11); disclosure strings
per G-3. *Why:* `CLAUDE.md` guardrail; the owner's reputation is an asset (S-1).

## 7. DevOps and operations

**O-1 Environments.** `local` (Compose with Postgres and object-storage containers, fixtures only); `ci`
(ephemeral); `preview` (per PR, web + API on a seeded fixture database; no outbound to sources, social or CRM —
adapters in dry-run); `staging` (full pipeline on fixtures plus ≤ 5 open sources at real cadence, social and
CRM adapters in dry-run, separate credentials); `production`. Twelve-factor configuration
(https://12factor.net/): environment variables only, no per-environment code paths. *Why:* ADR 0005; preview is
the design-review surface (D-36) and the CWV measurement point (D-31).

**O-2 Everything reproducible from the repo.** OpenTofu for cloud resources, Compose per environment, SOPS + age
secrets, one image with per-process entrypoints (`docs/20` §4.1); no console-clicked resource survives a
`tofu plan` diff. *Why:* a solo operator cannot reconstruct undocumented infrastructure.

**O-3 CI gates that block merge** (all required on `main`): `ruff` + `ruff format --check`; `mypy --strict`;
`pytest` unit + contract + integration (E-6…E-9, E-12) with coverage floors (E-7); OpenAPI generated == committed
(E-8); migration up/down (E-11); `import-linter` (E-2); `gitleaks`; `pip-audit`; image build + `trivy`;
licence-gate fixture (US-906); `axe` zero serious/critical and Lighthouse CI budgets on the three key pages
(D-30, D-31); docs link check. E2E smoke (E-10) runs on preview and post-deploy. *Why:* "lint before commit" is
a habit; a required check is a gate.

**O-4 Deploy.** Tag on `main` → image built once, pushed by digest → `docker compose up` over SSH per VM in order
`worker-*` (drain, stop), `api`/`web` (behind the edge cache), `scheduler`; migrations run before the new image
starts (expand phase), never at app startup; each deploy recorded (tag, digest, who, when, migration ids) in the
deploy log under `infra/`. *Why:* `docs/20` §12 — public pages serve from the edge throughout.

**O-5 Rollback.** Re-deploy the previous digest (≥ 5 kept); downgrade a migration only if its docstring says
reversible, otherwise expand/contract (E-11) lets the old image run on the new schema; `replay` (`docs/21` §6.5)
repairs entity tables after a bad deploy; rollback rehearsed on staging each sprint. *Why:* a rollback that
needs a downgrade under pressure is the wrong design.

**O-6 Observability minimums.** Per source (`docs/20` §10): success rate, run latency, rows seen/new/changed/gone,
DQ status (DA-6), `blocked`/`failing`, egress class, cost per changed record; alerts on failing > 2 cycles, queue
age > 2× cadence, DQ hold. Per model call (`model_call`, `docs/20` §4.5): purpose, alias (never a provider id),
prompt-template version, subject (`source_id`, entity, snapshot), tokens in/out, USD, latency, cache hit, error;
dashboards show USD per new/changed record per source over 30 days and daily budget use; alert at 80 % of any
budget. Per API: requests by tier and status, p95 per endpoint (E-16), 5xx rate (alert > 1 %). Per publisher:
publish success, expired drafts, budget holds (`docs/32` §6.1). Logs and traces per E-18. *Why:* `docs/03` §6
and M-9/M-10/M-12 are computed from these.

**O-7 Backups and restore drills.** Managed Postgres daily snapshot + PITR (RPO ≤ 1 h); object storage versioned;
RTO ≤ 4 h; a monthly restore into a scratch database runs the integration fixtures and records date, duration,
restored point and outcome in the runbook; alert if backup age > 26 h (`docs/20` §10–§11). *Why:* the store is
the system of record for a data product; snapshots are legal evidence.

**O-8 Cost ceilings.** Core infrastructure ≤ USD 415/month (`docs/20` §14 upper bound; exceeding it needs an
owner decision in `docs/00-PLAN.md`); model spend: per-source and global daily budgets in gateway config,
demotion at USD 0.50 per changed record (`docs/20` §6, A-9); X credit cap USD 250/month in `config/social.yaml`
(`docs/32` §4.7; `docs/20` §14's USD 300 is an estimate at 50 posts/day, not the cap); per-post model cost ≤ USD
0.01 (`docs/32` §4.2). Every ceiling has a metric and an 80 % alert (O-6); monthly review (G-11). *Why:*
`docs/01` §3.4 ceiling; a source that costs more than its records are worth is dropped (`docs/03` §6).

**O-9 Runbook format.** One file per procedure under `docs/6x-runbooks/` (or one section each in
`docs/60-runbooks.md` until there are more than ten) with headings **Title · Trigger / symptom · Severity ·
Preconditions and access · Steps** (numbered, copy-paste commands, expected output) **· Verification · Rollback ·
Escalation · Last executed (date, role, outcome)**. Required at launch: deploy, rollback, restore drill, secret
rotation, source-blocked triage, DQ-hold release, unmerge, takedown/unpublish, deletion request, incident (S-9),
residential-egress enable, X-budget hold. A runbook not run in 6 months is marked stale. *Why:* a runbook is
only trusted if it has been run.

## 8. Go-to-market and content

**G-1 ICP discipline.** Every CRM account carries one segment from `docs/33` §1 (mapping to `docs/10` §2), a lead
band from the `docs/33` §2.3 rubric and the §2.2 fields; research uses public sources only (§2.1); no account is
worked outside the 90-day segment priority (§1.7) without a note saying why. *Why:* M-5 is measured per segment.

**G-2 The human-sends rule.** No agent, pipeline or scheduler sends an email, DM, connection request, comment or
reply to a person; drafts land in the CRM (`draft_by = agent`) or the review queue and a named human sends from
their own account (`docs/33` §8, `docs/32` §5, `CLAUDE.md`). Alerts and digests subscribers opted into are the
one automated channel and carry the disclosure footer. A send without a human is an S1 incident (S-9). *Why:*
platform rules, marketing law and the owner's reputation (`docs/03` §4).

**G-3 Disclosure text is fixed and versioned** (D-35): outreach footer and profile statement from `docs/33` §8;
channel bios and email footer from `docs/32` §2.2; the automated-channel sentence from `docs/33` §8 if the owner
ever enables one, with the channel named in `docs/00-PLAN.md`. Automated accounts carry the platform label where
one exists (X) and state it in the bio elsewhere (`docs/32` §1.3–1.5). *Why:* a guardrail and a launch-checklist
item (US-803 AC3).

**G-4 Editorial standards** are `docs/32` §3: which events earn a post (§3.1), post anatomy (§3.2), templates
(§3.3), style and corrections (§3.4); the §4.3 gates all pass before a draft reaches the queue; auto-publish only
per §4.6 with the owner's name and date in config; LinkedIn never auto-publishes. Pro alerts use the same
fact-line grammar and attribution. *Why:* posts are generated from structured fields only; the style guide keeps
them defensible.

**G-5 Nothing from gated sources is posted, alerted or digested** (`docs/32` §3.1, US-801 AC3, `docs/21` §8 item
5); publisher event names map to `docs/21` §7.3 (§10 item 4). *Why:* the feed is a public surface under the
same gate.

**G-6 Pricing and packaging change control.** The ladder is `docs/11` §3 (Free / Pro USD 149 / Team USD 9,000 /
API +5,000 or 25,000) with its delay schedule; `docs/33` §6 and billing configuration must match it. A change
needs an owner decision in `docs/00-PLAN.md`, edits to `docs/11` §3 and `docs/33` §6 in one PR, the billing plan
change through `BillingPort` config, a `docs/CHANGELOG.md` line, and notice to affected customers. No agent
commits a price or discount; pilot terms stay within `docs/33` §6.3. *Why:* two documents and a billing console
that disagree is how a customer gets three prices.

**G-7 CRM hygiene.** Minimum fields per `docs/33` §7.1; stages per §7.2, agents only 0 → 1 plus activity logging,
humans ≥ 2; opt-outs processed immediately, verified weekly, honoured within 10 business days (§3.1, §7.3);
dedupe weekly; no personal data beyond business contact details from public professional sources; the app
stores only `sor_ref`, `billing_ref`, `crm_lead_ref` (ADR 0006). *Why:* the CRM is the source of truth for M-4,
M-5 and M-13.

**G-8 Partnership and licensing approvals.** Term sheets follow `docs/33` §5 and never offer what §5.3 excludes
(raw restricted rows, personal data, first-year exclusivity, attribution removal); sequence: sales-bd drafts →
legal-compliance reviews (counsel where `docs/13` §7 flags) → owner negotiates and signs (needs the entity, open
question 3); a signed data licence becomes a `licence` row with `contract_ref` and `expires_at` (`docs/21` §3.19)
and a `docs/00-PLAN.md` decision; renewals go on the Phase 6 compliance calendar. *Why:* `docs/03` §4 — licences
need a human signatory.

**G-9 Outreach compliance.** Every sequence obeys `docs/33` §3.1 (a graph fact with `source_url` in every message,
three touches then stop, CAN-SPAM footer, no LinkedIn automation, TCPA rules for calls); EU/UK sends wait for
legal-compliance's outreach checklist (`docs/10` §8.2 item 6). *Why:* exposure sits with the sender — the owner.

**G-10 Post templates are artefacts under review** (R-1): attribution and disclosure baked in
(product-designer brief), a rendered example per channel at that channel's limits, the `docs/32` §4.3 gates run
on the example. *Why:* a missing credit line in a template replicates a thousand times.

**G-11 Metrics review cadence.** Weekly: social report Monday 08:00 ET (`docs/32` §6.2) and pipeline report
Monday (`docs/33` §7.3), agent-drafted, owner-read. Sprint end: M-1…M-13 against targets and kill signals
(`docs/10` §5) in the owner review of `docs/00-PLAN.md` (`docs/03` §5), plus cost ceilings (O-8). Monthly:
lead-scoring weights (`docs/33` §8). Quarterly: ICP priority, pricing ladder, delay schedule. A metric with no
number is reported as "not measured" with the blocker, never omitted. *Why:* kill signals only work if read on
schedule.

## 9. Review and enforcement

### 9.1 Definition of done per artifact type

**R-1** An artifact is done when every applicable line below is met or marked "not met — blocker: …"; the
reviewer (R-2) records the check in the PR or the doc's status line.

| Artifact | Done when |
|---|---|
| **Doc** (`docs/*.md`) | Status line (P-10); one topic; sections numbered and stable; every number sourced or tagged as assumption (P-7, P-8); references by section, not restatement; decisions in `docs/00-PLAN.md`; `docs/CHANGELOG.md` line; British spelling in prose; no model identifiers; the `docs/03` §2 reviewer has read it |
| **Connector** | DA-14 steps 1–8 artefacts present; `docs/20` §3.1 protocol; stable `source_record_id` documented; fixtures per E-6 with no personal or gated data; `status_map.yaml` per DA-5; DQ thresholds set; egress class and host limits configured; three baseline runs logged; `mypy --strict` and `ruff` clean; CHANGELOG line |
| **API endpoint** | In `docs/23` (updated first if new); generated OpenAPI matches (E-8); envelope and provenance (API-5); visibility predicate applied and E-9 fixtures pass; RFC 9457 errors with table codes; rate-limit headers; cursor pagination; contract test and example; p95 within E-16; request logging; `x-tier`/`x-stories` set; no internal ids |
| **UI screen** | Derived from `docs/30-design-references.md` (D-6), no banned pattern (D-7); tokens only (D-19); contrast recorded (D-20); WCAG 2.2 AA checks and `axe` clean, keyboard pass done (D-30); 400 px verified (D-32); CWV and JS budgets on preview (D-31); provenance on every data element and attribution line in footer (D-2, D-27, D-34); delayed notice (D-3, D-28); empty/loading/error states (D-29); filters in URL (D-17); motion and reduced-motion (D-15); component tests + smoke path (E-10); qa-engineer and product-designer review |
| **Post template** | Matches `docs/32` §3.2 anatomy and the §3.3 template for its event type; attribution and disclosure from the versioned strings (D-35); rendered example per channel within limits; `docs/32` §4.3 gates pass on the example; lag notice on `proposal.*`; no banned words; UTM per §3.2; content-social and product-designer review |
| **Outreach sequence** | Segment named (G-1); every touch has a graph fact with `source_url` and a public company fact (`docs/33` §3.1); three touches max; compliance footer and disclosure line (G-3); opt-out route; personalisation tokens only from the `docs/33` §3.1 list; `draft_by = agent`, sent-by empty; EU/UK gated on the legal checklist; legal-compliance review where flagged; the owner is the sender |

### 9.2 Who reviews what

**R-2** Routing is the `docs/03` §2 table, restated as a lookup:

| Producer | Reviewed by | Also consulted when |
|---|---|---|
| product-manager | owner | — |
| market-researcher | product-manager | — |
| legal-compliance | owner (+ counsel where `docs/13` §7 flags) | any source publication (DA-14 step 2); any outbound channel (G-2, G-3) |
| solutions-architect | owner | any PR changing a `docs/20` §15 row or an ADR |
| data-engineer, data-scientist, backend-developer | qa-engineer | legal-compliance on gate/licence code paths; solutions-architect on schema changes (DA-1) |
| frontend-developer | qa-engineer, product-designer | — |
| product-designer | product-manager | frontend-developer on feasibility; owner on canvases (D-36) |
| devops-engineer | solutions-architect | — |
| qa-engineer | product-manager | release sign-off against US-908 (R-4) |
| content-social, sales-bd | owner | legal-compliance on disclosure, footer or template changes |

**R-3** A reviewer checks by rule id and the R-1 row; findings are written as `rule id — what fails — what would
pass`. "Looks good" is not a review. *Why:* the owner asked for a standard checkable line by line.

**R-4** The qa-engineer's release sign-off (US-908) additionally confirms: E-9/E-10 green on production-shaped
data; M-11 = 0 in the nightly audit; attribution on every surface; privacy notice and deletion route live;
automation labels set; rate limits active; unsubscribe works; backup age < 26 h; ADRs match the running system
(ADR 0001); no expired exception (§9.4). *Why:* the launch checklist is the enforcement point for everything
above.

### 9.3 Self-check before requesting review

**R-5** The author lists the rule ids they claim, the ones they judge not applicable (with a reason), and any
they are requesting an exception for; reviews of artefacts without this list are returned unread. *Why:* it
halves review time and catches the "did not know the rule" class.

### 9.4 Exceptions

**R-6** A departure from a MUST is recorded in the register below, and nowhere else, with rule id, artifact,
reason, compensating control, approver (the R-2 reviewer, plus the owner for anything touching gates, licences,
personal data or outbound messaging), date and expiry (≤ 1 sprint by default). An expired exception blocks
release (R-4). No exception applies to `CLAUDE.md` guardrails, the visibility predicate, the six "may not show"
items (`docs/21` §8) or the human-sends rule.

| Id | Rule | Artifact | Reason | Compensating control | Approver | Date | Expiry |
|---|---|---|---|---|---|---|---|
| — | — | — | (none recorded) | — | — | — | — |

## 10. Conflicts between existing docs, and what this standard picks

| # | Where | Conflict | Pick | Why |
|---|---|---|---|---|
| 1 | `docs/20` §5 and A-8 (7 days) vs `docs/10` A-7 / `docs/21` D-1 (14 days) vs `docs/11` §3 (7 opportunities, 14 supply, 30 weekly, none quarterly) | Default public lag | The `docs/11` §3 schedule: 7 opportunities / 14 supply by default, per source class | It is the `docs/00-PLAN.md` working default (2026-09-12) and the only one tied to a pricing argument; `docs/21` §5.4 already makes lag per source and event type, so nothing structural changes; US-101 AC3's lag±1 fixture holds per class |
| 2 | `docs/20` §5 (PJM derived aggregates to Pro/API) vs `docs/10` §3.2–3.3, `docs/21` D-2/C-3, `docs/23` P-4 | Restricted/unknown sources on Pro/API | Nothing on any non-admin surface | `docs/00-PLAN.md` working default takes the safer reading; `CLAUDE.md` says PJM is not public until a licence exists and the terms question is open (`docs/13` §7 item 1); reversible in configuration when counsel answers |
| 3 | `docs/02` §1 / `docs/10` §4 / `docs/21` §7.1 (eight states + `unknown`) vs `pipeline/status_map.yaml` v2 (adds `under_construction`) | Lifecycle vocabulary | Adopt `under_construction` between `contracted` and `built`; solutions-architect updates `docs/21` §7.1 and the `vocabulary` table | The status map's note is right: EIA-860M distinguishes it and it is the most useful signal for S4; vocabularies are `text + CHECK`, so the addition is cheap; API-7 requires the 30-day announcement before it appears in `v1` |
| 4 | `docs/32` §3.1 event names (`proposal.new`, `proposal.status_changed`, `opportunity.rfp_opened`…) vs `docs/21` §7.3 (`created`, `status_change`, `opened`…) | Event-type vocabulary | `docs/21` §7.3 is the vocabulary; the publisher maps `docs/21` types to `docs/32` template names in `config/social.yaml` | `docs/33` §9 already defers final names to the architect; the store cannot carry two vocabularies |
| 5 | `docs/32` §4.1 (`publisher/*`, `config/social.yaml`) vs `docs/20` §2 (`services/social`) | Publisher code location | `services/social/` with `docs/32`'s module names inside it (E-1) | `docs/20` §2 is the component map the deploy and import rules are built on |
| 6 | `docs/13` §6 (SPP, ISO-NE `restricted`; NYISO `attribution-restricted`) vs `data/sources.yaml` (`unknown` for all three) and `docs/21` §8 | Reuse class of SPP / NYISO / ISO-NE | The YAML is the runtime truth and currently says `unknown`, which gates identically to `restricted`; legal-compliance updates YAML `reuse` and evidence from `docs/13` §6 before any of the three is published (DA-11, DA-12) | The store reads the registry, not a doc; this fixes who corrects which |
| 7 | `CLAUDE.md` quartet field `licence` vs `docs/21` column `licence_id` vs CSV column `licence` | Field naming | Store `licence_id` (FK); API `licence_id` + `reuse_class` + `licence_summary`; CSV `licence` (DA-2, API-5) | `docs/21` is the schema; the guardrail names the concept, not the column |
| 8 | `docs/20` §14 (X ≈ USD 300/mo at 50 posts/day) vs `docs/32` §4.7 (X cap USD 250/mo) | X spend | Cap USD 250 in config (O-8); `docs/20`'s figure is an estimate | A cap must be one configured number |
| 9 | `docs/10` US-103 AC3 (search < 500 ms p95) vs `docs/20` §4.4 (< 100 ms achievable) and §13 (300 ms trigger) | Search latency | Budget 500 ms (the contract); scaling trigger 300 ms sustained (E-16) | The PRD number is the promise; the architecture number is the alarm |
| 10 | `docs/20` §11 (filer contacts never stored) vs US-1001 (intake collects contact name and email) | Personal data | `docs/21` C-7: scraped contacts never stored; submitted contacts stored with consent on `task` + CRM (DA-13) | Already resolved in `docs/21`; restated so the inventory check reads one rule |
| 11 | content-social brief reads `docs/30-social-*`; product-designer owns `docs/30-design-*`; `docs/03` §2 puts the social playbook at `docs/32` | Doc numbering | `docs/30-design-references.md`, `docs/30-design-ia.md`, `docs/31-design-system.md` are design; `docs/32` is social (`docs/10` A-13); the content-social agent file should be corrected to `docs/32-*` | `docs/03` §2 is authoritative on ownership |

Items 3, 6 and 11 need edits to other agents' files; they are recorded here and in `docs/CHANGELOG.md` rather
than made silently.

## 11. External standards referenced

| Standard | URL | Used by |
|---|---|---|
| WCAG 2.2 (W3C Recommendation); target size 2.5.8 | https://www.w3.org/TR/WCAG22/ · https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html | D-5, D-14, D-15, D-30 |
| Core Web Vitals thresholds (LCP 2.5 s, INP 200 ms, CLS 0.1, p75) | https://web.dev/articles/vitals | D-13, D-31, E-16 |
| RFC 9457 Problem Details for HTTP APIs | https://www.rfc-editor.org/rfc/rfc9457 | E-17, API-4 |
| RFC 8594 Sunset header | https://www.rfc-editor.org/rfc/rfc8594 | API-7 |
| RFC 3339 timestamps | https://www.rfc-editor.org/rfc/rfc3339 | API-9 |
| IETF RateLimit header fields (draft) | https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/ | API-6 |
| OpenAPI Specification 3.1.1 | https://spec.openapis.org/oas/v3.1.1 | E-8, API-1 |
| OWASP ASVS 5.0.0 (2025-05-30) | https://owasp.org/www-project-application-security-verification-standard/ | S-1…S-6 |
| Conventional Commits 1.0.0 | https://www.conventionalcommits.org/en/v1.0.0/ | E-15 |
| OpenTelemetry semantic conventions | https://opentelemetry.io/docs/specs/semconv/ | E-18 |
| The Twelve-Factor App | https://12factor.net/ | O-1 |
| SIL Open Font License 1.1 | https://openfontlicense.org/ | D-21 |
| OpenStreetMap copyright and attribution (ODbL) | https://www.openstreetmap.org/copyright | D-13 |
| FTC CAN-SPAM compliance guide | https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business | G-2, G-9 |

Core Web Vitals thresholds, the WCAG 2.2 target-size criterion and the ASVS version were confirmed against the
linked pages on 2026-09-12; D-20 contrast ratios were computed with the WCAG 2.x relative-luminance formula.

## 12. Assumptions in this document

| Id | Assumption | Depends on | Effect if wrong |
|---|---|---|---|
| ST-1 | A canvas/WebGL map renderer (MapLibre GL JS or equivalent) is acceptable as the one JS-heavy island in an otherwise htmx site | product-designer's IA (`docs/30-*`), `docs/20` §15 web row, owner question 1 | D-13/D-31 budgets re-based; no rule change |
| ST-2 | Newsreader, IBM Plex Sans and IBM Plex Mono ship variable fonts under OFL 1.1 in the versions chosen | D-21 licence check in `docs/31` | Static instances used; licence recorded either way |
| ST-3 | Coverage floors (E-7), DQ thresholds (DA-6) and ASVS L2 reachability (S-1) are starting values for the first three sprints | measurement | Tuned by a `docs/00-PLAN.md` decision; never loosened on the gate modules; ASVS gaps become expiring exceptions (R-6) |
| ST-4 | The owner ratifies §10 picks 1–3 in `docs/00-PLAN.md` | owner review | Affected rules (D-3, D-25, DA-5, DA-11, G-5) are configuration or vocabulary changes, not structural |
