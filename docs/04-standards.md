# Cross-discipline standards

**Status:** Sprint 1 deliverable, v1 · 2026-09-12 · owners: product-manager, solutions-architect, product-designer
(jointly) · reviewed by: owner
**Authority:** `docs/00-PLAN.md` decisions log, 2026-09-12: "every later deliverable is reviewed against it".
**Inputs:** `CLAUDE.md`, `docs/03-agent-operating-model.md` §2 (who owns and reviews what), `docs/10-prd-mvp.md`
§4–§8, `docs/20-architecture.md`, `docs/21-data-model.md`, `docs/23-api-spec-outline.md`, `docs/adr/0001–0006`,
`docs/11-market-and-competition.md` §3, `docs/13-legal-data-rights.md` §5.4, `docs/32-social-operating-playbook.md`
§3–§4, `docs/33-gtm-and-sales-playbook.md` §8.

## 0. How to use this document

1. **Every rule has an id** (`P-n` product, `D-n` design, `E-n` engineering, `DA-n` data, `API-n` API, `S-n`
   security, `O-n` operations, `G-n` go-to-market, `R-n` review). Reviewers cite the id; authors cite the id
   when they claim compliance or request an exception (§9.4).
2. **MUST / SHOULD.** MUST is checked line by line and blocks "done". SHOULD is checked and a departure needs
   one sentence of justification in the PR or doc, not an exception record.
3. **This document references, it does not restate.** Where a rule says "per `docs/21` §8", the referenced
   section is the normative text; this document adds the rule and the check. If a referenced doc and this one
   disagree, §10 says which wins and why; anything not listed in §10 is a defect in this document — raise it.
4. **Precedence:** `CLAUDE.md` guardrails > `docs/00-PLAN.md` decisions log > this document > the phase docs
   > code comments. A decision that changes a rule here is logged in `docs/00-PLAN.md` first, then edited here
   with a line in `docs/CHANGELOG.md`.
5. Rationale is one line per rule, marked *Why*. If the rationale stops being true, the rule is up for review.

---

## 1. Product and specification

**P-1 Every requirement is a user story with testable acceptance criteria.** Format is `docs/10` §4: stable id
(`US-nnn`, never renumbered), "As a {segment}, I want … so that …", then `AC1…ACn`, each phrased so QA can write
the test without asking the author (a fixture, a number, a state, an observable output). *Why:* the qa-engineer
turns ACs into tests directly (`docs/03` §2); an untestable AC is a conversation deferred to release week.

**P-2 Every story names its segment** from `docs/10` §2 (S0–S6). A feature that serves no listed job is out of
scope until the PRD adds the job. *Why:* the product-manager brief — "name which user every feature serves".

**P-3 Every metric names a source of truth** that a machine can compute (a table, a log, a CRM query), in the
`docs/10` §5 table shape: id, metric, source of truth, target, kill/review signal, origin. A metric whose source
is "we'll estimate" is not a metric. *Why:* M-1…M-13 are the kill signals of `docs/01` §6; they only work if
nobody has to interpret them.

**P-4 Acceptance criteria must state tier and licence behaviour** wherever a surface shows source data: which
tier sees it, what is withheld, and the fixture that proves it (pattern: US-101 AC3/AC4, US-906). *Why:* M-11 is
zero breaches; a story that is silent on tier is a story that leaks.

**P-5 Scope changes go through one process.** (a) Author writes the change as a PRD diff (story added, cut or
changed, with the affected ACs and metrics). (b) product-manager assesses against `docs/10` §3 scope tables and
the sprint exit criteria in §6. (c) Decision is logged in `docs/00-PLAN.md` decisions log with rationale; the PRD
change history (`docs/10` §9) gets a row; `docs/CHANGELOG.md` gets a line. (d) Anything that moves a gated item
(`docs/10` §3.3) out of "gated" needs legal-compliance sign-off recorded in `data/sources.yaml`. No scope change
by pull request alone. *Why:* the PLAN is the project's memory; sessions are not.

**P-6 Decision logging.** Any choice that a later agent could plausibly re-derive differently is a decision:
log it in `docs/00-PLAN.md` (date, decision, rationale) the day it is made. Material technical choices also get an
ADR (ADR 0001 rules 3–5). A decision that is only in a chat transcript does not exist. *Why:* ADR 0001 context.

**P-7 Assumption tagging.** Assumptions carry an id in the doc that depends on them (`A-n` in the PRD, `A-n` in
`docs/20` §16, `D-n` in `docs/21` §9, `P-n` in `docs/23` §13), each with: what is assumed, which open owner
question or missing evidence it depends on, and what changes if wrong. New docs use the next free prefix and
list assumptions in their own final section. *Why:* `CLAUDE.md`: "record any assumption in the doc that depends
on it"; the owner's answers must be mechanically re-plannable.

**P-8 Numbers carry confidence and a source.** Any quantitative claim in a product, market or plan doc states
its source URL and retrieval date, or is marked as an assumption with calibrated confidence
(`docs/11` style: high / moderate / low with the reason). *Why:* "evidence first, calibrated confidence"
(`CLAUDE.md`).

**P-9 Definitions of done are written before the work starts.** The product-manager writes a numbered DoD for
each deliverable (`docs/10` §8 is the model) and the reviewer named in `docs/03` §2 checks each item, writing
"met / not met (blocker)" per item. Silence on an item means not met. *Why:* it is the only way a rotating team
agrees on "done".

**P-10 Doc hygiene.** One topic per file, numbered by phase (`CLAUDE.md` conventions); a status line at the top
(status, date, owner, reviewer, inputs); section numbers stable once another doc cites them (append, do not
renumber; `docs/21` §10 C-1/C-2 show the cost of renumbering); external sources cited with URL and date;
British spelling in prose (`licence`, `normalise`), identifiers exactly as `docs/21` spells them. *Why:*
cross-references are the project's navigation.

---

## 2. Design and UX

### 2.1 Principles for a data-dense B2B product

**D-1 Density over decoration.** The user is scanning hundreds of rows to find five. Default views show more
rows with less chrome: 40–48 px table rows on desktop, no card grids for tabular data, no hero imagery on
working screens. *Why:* the ICP reads registers for a living (`docs/11` §6 interview guide); whitespace is not
their problem, finding the row is.

**D-2 Evidence is visible, not hidden.** Every value the platform shows can be traced to a source in ≤ 1 click:
a provenance affordance (source name, retrieved date, licence badge, link out) sits on the row, chip, marker or
field, not only in a footer. *Why:* the product's differentiation is provenance discipline (`docs/10` §1.2); the
guardrail "attribution renders automatically" (`CLAUDE.md`).

**D-3 Delayed means visibly delayed.** Every public-tier surface carries the `data_as_of` / `lag_days` line from
the API envelope (`docs/23` §10) in a fixed position (page header on lists and map; record header on detail;
top of digest) with the "live in Pro" link (US-604, US-201 AC4). *Why:* the delay is the paid tier's value; an
invisible delay reads as stale data.

**D-4 One mental model across search, map and feed.** The same filter set, the same URL parameters, the same
tier rules, the same result count; switching between list, map and feed keeps the filter state (US-102 AC2,
US-104 AC2). *Why:* the owner elevated the map to a primary navigation surface; a second filter grammar would
split the product in two.

**D-5 Never colour alone.** Every state, tier and licence class is carried by a label or icon as well as
colour. *Why:* WCAG 2.2 SC 1.4.1; also colour is where status chips go wrong under dark mode.

**D-6 No default AI aesthetic; a documented reference study comes first.** Before any screen is drawn,
product-designer delivers `docs/30-design-references.md`: a study of at least eight of the best interactive
data and map products (categories to cover: mapping and GIS tools, financial and energy data terminals,
government data portals, data-journalism interactives, developer documentation sites), each entry naming the
product, URL, date viewed, what Bankable takes from it (layout, density, interaction, typography, motion), what
it avoids, and a screenshot reference by link (no copied assets). Typefaces, spacing scale, type scale and
page layouts in `docs/31-design-system.md` are derived from that study and cite it. *Why:* owner instruction
2026-09-12; a product sold on provenance cannot look like a template.

**D-7 Banned patterns.** A reviewer rejects any screen, canvas or template that shows: generic sans-serif type
on purple or blue gradients; uniform rounded cards in a three-tile hero; emoji as bullets or icons; stock
illustration or stock photography; glassmorphism or frosted panels; decorative blur, glow or drop-shadow
stacks; a marketing hero above a working screen; auto-playing or looping animation; or anything the reviewer
would describe as "template". The rejection cites D-7 and the pattern. *Why:* owner instruction 2026-09-12.

### 2.2 The map as a primary navigation surface

**D-8 Everything with a place is on the map.** Proposals and opportunities are browsable on `/map` with the
same filters and tier rules as `/proposals` and `/opportunities` (D-4). Placement precedence per record:
`location.precision = exact` → point; `county_centroid` → point at the county centroid, marked as such; state
only → listed under the state in the side panel and drawn as a state-level count; no geography → listed under
"Unplaced" in the panel with the count shown, never silently dropped (US-104 AC1, `docs/21` §3.7). *Why:*
US-104 AC1 and the PLAN decision.

**D-9 Restricted-precision rule.** Records whose geometry provenance is a source with
`allows_raw_publication = false` or `reuse_class ∈ {restricted, unknown}` never render an exact point on any
non-admin surface; the county centroid is used and the drawer says "location shown at county level (source
licence)" (US-104 AC3, `docs/21` D-7, `docs/21` §8 `precise_geo` class). *Why:* an exact coordinate is a raw
field.

**D-10 Clustering by lifecycle state and technology.** Clusters are computed server-side by
`/v1/proposals/geo?bbox&zoom` (`docs/23` §7) and returned as features with `{count, lifecycle_state_counts,
technology_counts, capacity_mw_sum}`. Cluster glyphs encode count (size), dominant lifecycle state (chip
colour + label) and technology (icon); hovering or focusing a cluster shows the breakdown. The switch from
clusters to individual markers happens at a documented zoom threshold (initial: 500 visible records or fewer)
and the threshold is a config value, not a constant. *Why:* 20k+ records cannot be individual markers; a
cluster that hides state and technology hides the product.

**D-11 Opportunities draw as service territories.** An opportunity whose issuer has a service-territory polygon
(`location.kind = service_territory`) is drawn as a polygon with a hatched fill and an outline in the
opportunity colour, with a label at the centroid; overlapping polygons are ordered by `due_at` ascending on top.
Opportunities without a polygon fall back to D-8 precedence. Polygon sources carry provenance like any other
record (`docs/21` §3.7 provenance quartet). *Why:* S3 job (a) — "which proposals in my territory could respond".

**D-12 Detail drawer with provenance.** Selecting a marker, cluster item or polygon opens a drawer (not a new
page) showing: canonical fields, lifecycle chip, `Sources` list (name, retrieved date, licence badge, link out —
omitting gated sources entirely per `docs/21` §8 item 3), the last three events, the delayed-tier line (D-3),
and a link to the full record. The drawer is a `role="dialog"` with focus trap and returns focus to the marker
on close. *Why:* US-201 AC1 on the map without leaving the map.

**D-13 Map performance budget.** Measured on the seeded production-shaped dataset (≥ 20,000 placed records):
first map paint counts toward the page LCP budget (D-31); a viewport request returns ≤ 2,000 features and
≤ 300 KB gzipped; pan or zoom to updated markers ≤ 200 ms (INP); rendering is canvas/WebGL (MapLibre GL JS or
equivalent), never one DOM node per marker above 500 markers; basemap tiles are self-hosted or from a provider
whose terms permit commercial use, with the basemap attribution rendered (OpenStreetMap ODbL,
https://www.openstreetmap.org/copyright). The map JS bundle is ≤ 250 KB gzipped including the map library.
*Why:* Core Web Vitals thresholds (D-31); the map is the heaviest page and the one the owner made primary.

**D-14 Keyboard and screen-reader access to map content.** The map has a synchronised results list ("in view")
that is the accessible path to the same records and is reachable by a skip link. Map controls are buttons with
names; arrow keys pan, `+`/`-` zoom, `Enter` opens the focused marker; markers and clusters are focusable via
roving tabindex when ≤ 500 are rendered, otherwise the list is the path. An `aria-live="polite"` region
announces "N proposals, M opportunities in view" after each move (debounced 500 ms). Every marker's accessible
name is `{name}, {lifecycle_state}, {capacity} MW, {county}, {state}`. *Why:* WCAG 2.2 SC 2.1.1, 4.1.3; the map
must not be the only way to reach anything.

**D-15 Motion on the map and feed.** `flyTo` ≤ 600 ms ease-out; cluster split/merge ≤ 300 ms; drawer open 200 ms,
close 150 ms; feed inserts never move content the user is reading — new events appear behind a "N new events"
control at the top, not by auto-scrolling; no looping or ambient animation anywhere. `prefers-reduced-motion`
disables all of the above (instant transitions). *Why:* WCAG 2.2 SC 2.3.3; a moving table is unreadable.

### 2.3 Information architecture

**D-16 Three surfaces, one URL scheme.** Public (`bankablehq.com`), Pro (same host, entitlement-gated routes),
Admin (`admin.` host, `docs/20` §7). Canonical routes: `/proposals`, `/proposals/{slug}`, `/opportunities`,
`/opportunities/{slug}`, `/organizations/{slug}`, `/map`, `/search`, `/feeds/*`, `/alerts`, `/account`,
`/docs/api`. Slugs are `docs/21` §3.1 `slug`; merged slugs 301 to the survivor (US-201 AC3). *Why:* stable URLs
are the SEO asset of the free tier.

**D-17 Every filter, sort, tab and map viewport lives in the URL** as `docs/23` §7 parameters; a copied URL
reproduces the view on the same tier; no filter state in local storage only. *Why:* US-102 AC2; sharing a view
is the S2 workflow.

**D-18 Navigation depth ≤ 3** from any top-level route to any record; breadcrumbs on every record page; the
primary nav is the same on all public and Pro pages (Proposals · Opportunities · Map · Feed · Alerts ·
API). Admin nav is `docs/20` §8's list, in that order. *Why:* the IA is small; keep it small.

**D-19 Detail pages are the unit of SEO** and therefore server-rendered with the canonical fields, the
`Sources` panel, the timeline and the attribution line in HTML (no client-side rendering of the primary
content); `<link rel="canonical">`, Open Graph fields (used by the social embeds, `docs/32` §1.3) and JSON-LD
`Dataset`/`Event` where accurate. *Why:* `docs/20` §15 web app row; social posts link here (`docs/32` §3.2).

### 2.4 Tokens, typography and colour

**D-20 Tokens are the only way to reference colour, type, space and radius.** `docs/31-design-system.md`
defines them as CSS custom properties in one file; components never use raw hex or px for these. Naming:
`--color-*`, `--font-*`, `--space-*`, `--text-*`, `--radius-*`, `--shadow-*`, `--motion-*`. *Why:* dark mode,
contrast fixes and brand changes become one-file edits.

**D-21 Brand palette and verified contrast.** Base tokens (light theme; dark theme swaps roles):

| Token | Value | Use | Contrast (WCAG 2.x relative luminance) |
|---|---|---|---|
| `--color-ink` | `#16324f` slate navy | text, primary surfaces | 13.1:1 on white; 11.9:1 on `--color-paper` |
| `--color-paper` | `#f6f4ef` warm off-white | page background | — |
| `--color-copper` | `#a8571c` | accent, primary action, links ≥ 16 px | 5.2:1 on white, 4.7:1 on paper — passes AA for normal text, **fails on ink (2.5:1)** |
| `--color-copper-deep` | `#8a4614` | links and small text on light | 7.1:1 on white |
| `--color-copper-tint` | `#e0b58a` | accent and links on ink/dark | 7.0:1 on ink |
| `--color-muted` | `#5b6b7c` | secondary text on light | 5.5:1 on white (do not go lighter than `#6b7a8a`, 4.4:1, for text) |

Rule: copper never appears as text on navy; copper-tint does. Any new token pair used for text is checked at
4.5:1 (normal) or 3:1 (≥ 24 px / 18.66 px bold and UI components/graphics, SC 1.4.3 and 1.4.11) and the ratio
is written next to the token in `docs/31`. *Why:* the numbers above are computed, not assumed; the copper/navy
pairing is the one designers reach for and it fails.

**D-22 Typefaces and licensing.** Display: Newsreader; UI and body: IBM Plex Sans; identifiers, numbers,
code: IBM Plex Mono. All three are under the SIL Open Font License 1.1 (https://openfontlicense.org/); `docs/31`
records the exact version and file names used, the licence file is vendored next to the fonts, fonts are
self-hosted (no third-party font CDN on production pages), and variable-font files are used wherever the family
ships one. Adding or replacing a family requires the licence check recorded in `docs/31` before use. *Why:*
brand context (product-designer brief); a licence surprise on a typeface is an avoidable launch blocker.

**D-23 Modular, documented scales.** Type scale: base 16 px, ratio 1.2 (minor third) for UI steps
(12.8 / 16 / 19.2 / 23 / 27.6 / 33.2 / 39.8), display steps in Newsreader from 27.6 upward; line height 1.5 body,
1.25 display; numbers in tables and chips use `font-variant-numeric: tabular-nums`. Spacing scale: 4 px base
(`--space-1` = 4 through `--space-12` = 64, steps 4/8/12/16/20/24/32/40/48/64). Both scales are written in
`docs/31` with the rationale from `docs/30-design-references.md`, and the layout grid (column count, gutters,
max content width per breakpoint) is specified there too. Any value not on the scale needs a comment. *Why:*
owner instruction 2026-09-12; consistency is what makes density readable.

**D-24 Dark and light themes are both first-class.** Every token has a value in both; the theme follows the
system preference with a manual override; contrast is verified in both (D-21). *Why:* frontend-developer brief
("dark/light aware"); operators run the admin panel for hours.

### 2.5 Data-display rules

**D-25 Tables.** Column set and default sort per list are those in the PRD ACs (US-101 AC1, US-301); numeric
columns right-aligned with units in the header, not the cell; identifiers in Plex Mono; sticky header;
row-level provenance affordance (D-2); sortable columns are those in the API allowlist (`docs/23` §7) and no
others; page size 50 with cursor pagination (no "page 7 of 40"); empty, loading and error states per D-30.
*Why:* the table and the API must agree, or the UI promises sorts the API cannot do.

**D-26 Status chips per lifecycle state.** One chip component; the state vocabulary is exactly `docs/21` §7.1
plus `under_construction` (§10 conflict 3) and `unknown`; each state has a fixed token pair, an icon and the
label text — never colour alone (D-5). `unknown` is visibly neutral and shows `status_raw` on hover/focus where
the licence allows raw (`docs/21` §8). Opportunity chips use `docs/21` §7.2 states. *Why:* the chip is the
single most repeated element; one component, one vocabulary.

**D-27 Timelines.** Newest first (US-202 AC1); each item shows type, `observed_at`, source (with licence badge),
`before → after` for status changes; merge and unmerge events are labelled and link to the absorbed record
(US-202 AC2); public tier hides items newer than the lag and shows the D-3 banner in their place. *Why:* US-202.

**D-28 Provenance panel.** The `Sources` panel lists every visible `proposal_source` / `opportunity_source` with
source name, `source_record_id` where the licence allows, `retrieved_at`, licence badge, link out (US-201
AC1); gated sources are omitted, not greyed (`docs/21` §8 item 3); on Pro+ detail pages a per-field provenance
toggle shows `field_provenance` (`docs/21` §3.1). The attribution line from `licence_summary.attribution_line`
renders in the page footer of every list, detail, map and feed view (US-105 AC1). *Why:* the API supplies it;
the page prints it; omission is a launch-blocking bug (`docs/21` §8 last paragraph).

**D-29 Delayed-tier notices** use one component and one wording family across page banner, drawer, RSS item,
CSV header and social post: "Public data is {lag_days} days delayed (as of {data_as_of}). Live in Pro." (US-604,
`docs/32` §3.2 item 5). *Why:* one sentence the market learns to recognise.

**D-30 Empty, loading and error states are specified per screen** in `docs/31`: empty states say which filter
removed the last result or offer "clear all" (US-102 AC4); loading uses skeletons of the final layout (no
spinners over tables; prevents CLS); errors show the RFC 9457 `title` and `request_id` from the API
(`docs/23` §8) and never a stack trace; a gated or not-visible record is indistinguishable from not-found
(`docs/23` §8 `not_found`). *Why:* these states are most of what a new user sees in the first minute.

### 2.6 Accessibility, responsiveness, performance

**D-31 WCAG 2.2 AA is the bar** (https://www.w3.org/TR/WCAG22/) on every public, Pro and admin screen, including
the map (D-14). Checks in the DoD: contrast (D-21); target size ≥ 24 × 24 CSS px (SC 2.5.8); visible focus
(SC 2.4.11); no keyboard traps; drag operations have a single-pointer alternative (SC 2.5.7, map pan has
buttons); form errors identified in text; accessible names on every control and marker; `axe` automated scan
with zero serious/critical findings in CI plus a manual keyboard pass per screen. Core Web Vitals at p75 on
the proposal detail, list and map pages: LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1 (https://web.dev/articles/vitals),
measured with Lighthouse CI on the preview environment and with field data once live. *Why:* frontend brief;
the free tier is an SEO product and CWV is a ranking input.

**D-32 Responsive to 400 px.** Every screen works at 400 px width without horizontal page scroll: tables
collapse to a two-column key/value list or scroll inside their own container; the map keeps the results list
as a bottom sheet; side gutters ≥ 16 px; no fixed widths wider than the viewport. Breakpoints: 400 / 720 / 1080 /
1440. *Why:* frontend brief; owners and operators check alerts on phones.

**D-33 JavaScript budget.** Public non-map pages ≤ 50 KB gzipped of first-party JS (htmx + islands, `docs/20`
§15); the map page ≤ 250 KB including the map library (D-13); admin unbounded but measured. *Why:* server-rendered
pages are the architecture; the budget keeps it that way.

### 2.7 Content style

**D-34 Plain, evidence-first, no adjectives.** Product copy follows `docs/32` §3.4 style guide (banned words,
units, dates, no exclamation marks, no emoji, no hashtags outside the two social exceptions). Sentences state
what a register says, when, and where. *Why:* one voice across page, alert, post and email.

**D-35 Attribution on every data element**, not every page: a chart, a count, a chip, a map cluster, an alert
line and a CSV row each carry or link to their source. Aggregates state "computed over visible sources"
where gated sources are excluded (`docs/21` §8 item 4). *Why:* the guardrail is per record; the UI must not
launder provenance through aggregation.

**D-36 Microcopy is versioned strings**, not inline literals: disclosure text (`docs/32` §2.2, `docs/33` §8),
delayed-tier notice (D-29), attribution formats and error titles live in one strings file with a version;
the review queue cannot edit attribution or disclosure strings (`docs/32` §3.4). *Why:* legal wording must
change in one place with an audit trail.

### 2.8 When to use the design canvas

**D-37 Use the `design` skill canvas** when the owner must judge layout or interaction visually: the map page
and drawer, the proposal detail page, the list/table density options, the admin review queue, and the social
post card templates. Do not use it for tokens, tables of rules, flows or IA — Mermaid or ASCII in
`docs/30-*`/`docs/31-*` is sufficient and diffable. A canvas is linked from the doc that it illustrates; the
doc remains the record; a canvas that departs from D-6/D-7/D-21 is rejected like any other artifact. *Why:*
product-designer brief; canvases are review aids, not sources of truth.

---

## 3. Engineering

### 3.1 Repository layout

**E-1 Layout follows `docs/20` §2's component table** with these paths, each independently testable:

```
api/openapi.yaml            generated, committed, CI-checked (docs/23 §12)
data/sources.yaml           connector manifest (single source of truth for sources)
data/eval/                  labelled samples and evaluation artefacts (DA-9)
docs/                       numbered by phase (CLAUDE.md)
infra/                      OpenTofu modules, Compose files per environment, SOPS-encrypted secrets (ADR 0005)
pipeline/connectors/<source_id>/   connector.py, status_map.yaml, fixtures/, test_connector.py
pipeline/{snapshot,diff,normalize,resolve,enrich}/
services/{store,api,alerts,social,sor,modelgw}/
web/                        templates, static assets, islands; web/admin for the admin panel
scripts/                    one-off and operator scripts (probe_sources.py lives here)
tests/                      cross-cutting: contract, integration, e2e; unit tests live next to code
```

Rules: tests next to the code they test (`CLAUDE.md`), cross-cutting suites under `tests/`; the social
publisher modules named in `docs/32` §4.1 (`filter`, `draft`, `validate`, `queue`, `adapters/*`, `metrics`)
live under `services/social/`; `config/social.yaml` lives under `services/social/config/`; the existing
`pipeline/normalize.py` and `pipeline/status_map.yaml` move into this layout in the Sprint 1 connector
framework (per-source `status_map.yaml` next to each connector, `docs/20` §3.4). *Why:* one image, many
entrypoints (ADR 0002); a contractor must find a connector by its `source_id`.

**E-2 Import boundaries are enforced**, not described: `import-linter` contracts in CI forbid `pipeline/*` from
importing `services/api`, forbid anything but `services/modelgw` from importing a model-provider SDK
(`docs/20` §4.5), and forbid `services/*` from importing vendor CRM/billing SDKs outside `services/sor/adapters`
(ADR 0006). *Why:* the boundaries carry the cost and licence controls.

### 3.2 Language, framework and style

**E-3 Python 3.12, FastAPI + Pydantic v2, SQLAlchemy 2.0 + Alembic, httpx + tenacity, Playwright, pandas /
pyarrow / openpyxl / pdfplumber** per ADR 0002 and `docs/20` §15. Deviations need an ADR (ADR 0001 rule 3). The
interpreter is pinned in `.python-version`; dependencies are declared in `pyproject.toml` and locked with hashes
into `requirements.txt` (`pip-compile --generate-hashes`), so `pip install -r requirements.txt` in `CLAUDE.md`
stays true. *Why:* one runtime, one lockfile.

**E-4 Conventions.** Async for I/O in connectors and the API; sync pandas in parse and resolve; SQLAlchemy 2.0
typed API only (no legacy `Query`); Pydantic models are the API contract and the OpenAPI source; domain field
names are `docs/21` spellings verbatim (`licence_id`, `lifecycle_state`, `public_at`); module and package names
are `snake_case`; no `print` (ruff `T20`); UTC-aware datetimes only (ruff `DTZ`); money as `Decimal`, never
float (`docs/21` §1). *Why:* the ORM and the API generator do the enforcement if the code stays typed.

**E-5 Lint and type gates.** `ruff` with rule sets `E, F, W, I, N, UP, B, S, C4, DTZ, T20, RUF`, line length 110,
`ruff format` for formatting; `mypy --strict` on `pipeline/` and `services/`, with `ignore_missing_imports`
allowed only per untyped third-party module (gridstatus) in `pyproject.toml`, and `# type: ignore[code]` only
with an error code and a reason comment; both clean before commit (`CLAUDE.md`) and blocking in CI (O-3).
*Why:* strict typing is the cheapest review the team has.

### 3.3 Testing pyramid (named minimums)

**E-6 Unit tests for every parser with recorded fixtures.** Each connector ships ≥ 1 fixture per file format
and layout variant it handles, plus one "HTTP 200 with an error body" fixture where the source has that failure
(FERC `success:false`, EIA index HTML, `docs/02` §7); `parse()` runs against fixtures only, `fetch()` never runs
in CI (`docs/20` §3.1). Fixture files are ≤ 1 MB, trimmed to representative rows, carry a `README` line with
source URL and retrieval date, and contain no personal data (DA-13) and no rows from `restricted`/`unknown`
sources unless the fixture is used only to prove the gate (US-906). *Why:* parser regressions are the dominant
failure mode (`docs/20` §12 row 1).

**E-7 Coverage floors.** Line coverage ≥ 80 % on `pipeline/` and `services/`; 100 % branch coverage on the
visibility predicate, licence-gate and redaction modules (`docs/21` §5.4, §6.6, §8); coverage is reported per
package in CI and a drop below the floor blocks merge. *Why:* the gate modules are where a miss is a legal
event.

**E-8 Contract tests against OpenAPI 3.1.** `api/openapi.yaml` (OpenAPI 3.1, https://spec.openapis.org/oas/v3.1.1)
is generated at build and compared byte-for-byte to the committed file; a property-based contract suite
(schemathesis or equivalent) exercises every operation for schema conformance, the §10 envelope, RFC 9457
error bodies and rate-limit headers; every operation has an example response that is itself validated
(US-704 AC1). *Why:* `docs/23` §12 — the running API and the published contract cannot drift.

**E-9 Integration fixtures for tier and gate.** The seeded test database contains at least one record per
`publish_state`, per `reuse_class`, one at lag−1 day and one at lag+1 day, one mixed-provenance proposal
(ERCOT + PJM), one merged pair; tests assert visibility on web, RSS, API, export, webhook and post-draft paths
(US-601 AC1, US-906 AC1, `docs/21` §8 mixed-provenance case). *Why:* M-11 = 0 is proved here, not in review.

**E-10 End-to-end smoke** (Playwright, run on preview and after every production deploy): public list →
detail → attribution rendered; map → cluster → drawer → provenance; search by queue id returns the record
first (US-103 AC1); Pro login → saved search → alert preview; admin publish of a gated source is refused with
`gate_unmet` (US-905 AC1); a social draft cannot be created from a gated record (US-801 AC3). ≤ 10 minutes
total. *Why:* the launch checklist (US-908) is executable, not a spreadsheet.

**E-11 Migrations are tested both ways** on an empty and a seeded database (`alembic upgrade head`,
`downgrade -1`, `upgrade head`); a migration that cannot be downgraded says so in its docstring and is
deployed expand/contract (add, backfill, switch, remove in separate releases). *Why:* rollback (O-5) depends
on it.

**E-12 Data-quality and evaluation tests are tests.** The DQ gates (DA-6) and the resolver evaluation (DA-9)
run in CI against fixtures and fail the build on threshold regression. No test is skipped or quarantined to go
green (qa-engineer brief). *Why:* a flaky gate is no gate.

### 3.4 Code review

**E-13 Review checklist** (reviewer ticks each in the PR; "n/a" needs a reason):
1. Story and AC ids named; the change does what the AC says and nothing else.
2. Provenance quartet present on every new stored record type; visibility predicate used on every new read path.
3. No raw payload, coordinate or identifier from a `restricted`/`unknown` source reaches a non-admin surface.
4. Tests added at the right level (E-6…E-11); fixtures recorded, not live.
5. Types and lint clean; no new `type: ignore` without a code.
6. Errors are RFC 9457 with a `code` from `docs/23` §8; no new ad-hoc error shapes.
7. Logs structured, no secrets, no personal data, no prompts, no model identifiers (E-17).
8. Migration reversible or expand/contract; index changes justified against `docs/21` §5.2.
9. Performance budget (E-16) unaffected or re-measured.
10. Docs and `docs/CHANGELOG.md` updated; decisions logged; ADR cited if `docs/20` §15 changes.
11. No model identifiers or provider names in code, comments, fixtures, commit messages or artefacts.
*Why:* a checklist a reviewer can run in ten minutes beats a norm nobody can check.

**E-14 Review routing** is `docs/03` §2 (R-2). At least one reviewer other than the author; the qa-engineer
signs releases, not individual PRs. Self-merge is not allowed on `main`. *Why:* the operating model already
assigns reviewers; this makes it mechanical.

### 3.5 Commits, branches, secrets, dependencies

**E-15 Commits and branches.** Conventional Commits (https://www.conventionalcommits.org/en/v1.0.0/):
`type(scope): subject`, types `feat, fix, data, docs, test, refactor, perf, build, ci, chore`, scope = the
top-level path or `source_id`; body cites `US-nnn`, ADR or DA rule ids. Branches `type/short-slug`; `main` is
protected; squash merge; PR title = the squashed commit subject. **No model identifiers, model names, versions or
provider names in any commit message, trailer, branch name, code comment, docstring, fixture, log line, test
name or shipped artefact** (`CLAUDE.md`); the `docs/CHANGELOG.md` line names the *agent role*
(`data-engineer:`), which is how build-time attribution is recorded (`docs/03` §6). *Why:* the guardrail is
absolute and applies to trailers as much as to bodies.

**E-16 Performance budgets** (measured in CI against the seeded 10⁵-record database, and in production
dashboards): API p95 — list endpoints ≤ 300 ms, detail ≤ 200 ms, search `q=` ≤ 500 ms (US-103 AC3), `/geo`
≤ 400 ms, bulk first byte ≤ 1 s; search p95 > 300 ms sustained for a week triggers the `docs/20` §13 step 4
evaluation; pages per D-31; a `fetch` job ≤ 10 min, browser jobs ≤ 5 min (`docs/20` §4.2). A change that
regresses a budget by > 10 % needs a comment in the PR and, if accepted, an exception (R-6). *Why:* budgets
without a measurement are wishes.

**E-17 Error handling.** API errors are RFC 9457 problem details (https://www.rfc-editor.org/rfc/rfc9457)
exactly as `docs/23` §8, with `code` from that table; adding a code edits `docs/23` §8 first. Internally,
exceptions are typed per layer (`ConnectorError`, `ParseError`, `GateViolation`, `SorUnavailable`,
`BudgetExceeded`); a stage fails closed (nothing partial committed — `docs/20` §3.7 transactions) and records
the failure on `source_run`. Never catch-and-continue on a gate violation; it raises to the job runner and
pages. *Why:* a swallowed gate error is a leak with no log.

**E-18 Logging and tracing.** Structured JSON to stdout (`docs/20` §10) with required keys `ts, level, event,
service, env, request_id, trace_id, job_id, source_id, run_id` (null where not applicable); OpenTelemetry
traces on the API (sampled) and pipeline jobs (full), `traceparent` carried in the job payload so a request →
job → model call chain is one trace; OpenTelemetry semantic conventions for names
(https://opentelemetry.io/docs/specs/semconv/). Never logged: secrets, session or key material, raw payloads
from `restricted`/`unknown` sources, personal data, prompt or completion text (the gateway stores a prompt
hash and the `model_call` row, `docs/20` §4.5), provider model ids (aliases only). Log retention 14 days
(`docs/20` §10). *Why:* observability that leaks is worse than none.

**E-19 Secrets.** Environment variables injected at deploy from SOPS + age files under `infra/` (ADR 0005);
never in code, fixtures, tests, notebooks, screenshots or canvases; a secret scanner (gitleaks) blocks the
commit and the CI run; `.env` files are git-ignored and never contain production values; API keys and webhook
secrets are shown once and stored hashed (`docs/23` §5); every secret has a named owner, a rotation date (S-7)
and an environment. *Why:* `docs/20` §11; one operator cannot afford a credential incident.

**E-20 Dependencies.** Pinned with hashes (E-3); Dependabot weekly; `pip-audit` in CI blocks on known
vulnerabilities with a fix available; new direct dependencies need: an OSI licence compatible with commercial
use (no AGPL in services without an ADR), ≥ 1 year of releases or a stated reason, and a one-line
justification in the PR; container base images pinned by digest and scanned (`docs/20` §11 supply chain).
*Why:* the supply chain is the least-watched attack surface for a solo operator.

**E-21 Feature flags and tier gates.** Tier and licence behaviour is *data* — `source.publish_state`, `licence`
flags, lag configuration (`docs/21` §3.19, §5.4) — never a code flag. Code feature flags (`settings.features.*`,
env-driven, boolean, default off) may hide an unfinished surface or narrow what a tier sees; a flag may never
widen visibility or bypass the predicate (a test asserts the predicate is applied regardless of flag state).
Each flag has an owner, a purpose and a removal date ≤ 2 sprints after full rollout; no third-party flag
service. *Why:* a flag that can widen visibility is a gate with two keys.

---

## 4. Data

### 4.1 Canonical schema discipline

**DA-1 `docs/21` is the schema; the Alembic migration is its executable twin.** A field, vocabulary value,
index or constraint exists only if it is in both; a PR that changes one changes the other. The `docs/21` §1
conventions (uuid v7, `public_id`, `timestamptz` UTC, `numeric` money, `text + CHECK` enums, `jsonb` raw) are
mandatory for every new table. *Why:* `docs/21` preamble — "must not diverge silently".

**DA-2 Provenance and licence on every record, per field on fused entities.** Every row originating outside
the platform carries `source_id, source_url, retrieved_at, licence_id` (the `CLAUDE.md` quartet; the store
column is `licence_id` per `docs/21`, and exports render it as the `licence` column plus the licence summary
header, `docs/23` §10); fused entities carry `field_provenance` for every canonical field (`docs/21` §3.1);
`licence_id` on an observation row is immutable (`docs/21` invariant L2). A table that holds source data
without the quartet fails the schema test. *Why:* the gate works at field granularity or not at all
(`docs/21` §8 mixed-provenance case).

**DA-3 Append-only events; nothing deleted.** `event` is append-only with the trigger and role grants of
`docs/21` §6.1; entity tables are a fold of events and are rebuildable (`docs/21` §6.5); unpublish, withdraw,
takedown and correction are events; the only destructive operation is the redaction procedure (`docs/21` §6.6),
and it writes an event. Idempotency keys make re-runs free (`docs/21` §3.10). *Why:* "the event log is the
product".

**DA-4 Merges are reversible by construction.** A `merged` event satisfies invariant M1 (`docs/21` §6.3: enough
`before` state to unmerge without reading any other row) or it is rejected in code and in tests; human
decisions win and are recorded (`docs/21` §6.4). *Why:* wrong merges are visible to sponsors (`docs/10` A-9).

**DA-5 Status harmonisation is versioned data.** Source status → lifecycle mappings live in `status_map.yaml`
next to each connector (`docs/20` §3.4, `docs/21` §7.4), with `version`, `updated`, per-rule `id` and `note`
as `pipeline/status_map.yaml` already does; the canonical vocabulary is `docs/21` §7.1 plus `under_construction`
(§10 conflict 3) and `unknown`; unmapped values map to `unknown` with a DQ warning, never silently coerced;
a mapping change bumps `version`, is reviewed by data-scientist, and triggers a `reprocess` (`docs/21` §6.5)
that writes new events rather than rewriting old ones. *Why:* the mapping is a judgement call that changes;
code deploys should not be needed to change it.

### 4.2 Quality gates, resolution, retention

**DA-6 Data-quality gates run at the end of every source run** and write `source_run.dq` (`docs/20` §10):

| Check | Warning | Hold (snapshot stored, diff **not** applied, task opened) |
|---|---|---|
| Row-count drift vs previous successful run | ± 10–30 % | > 30 % in either direction (`docs/20` §12) |
| Vocabulary drift (`status_raw`, `technology_raw`, kind) | any new value | unmapped values > 5 % of rows |
| Null spike on a required canonical field | +5 pp vs trailing median of 5 runs | +10 pp |
| Duplicate `source_record_id` within one snapshot | — | any (the parser must dedupe deterministically or the source needs a composite key documented in the connector docstring) |
| Provenance completeness | — | any row missing the quartet |
| Schema drift (new/removed source columns) | any | removed column that feeds a canonical field |

A hold is released by a human from the task queue (US-907) with a reason; `removed` events are never emitted
from a held run. Thresholds are configuration per source, defaults as above. *Why:* silent partial files
produce false withdrawals (`docs/20` §12).

**DA-7 Entity-resolution rules.** Keys in `docs/02` §5 order (EIA id → queue id + ISO → FERC docket →
sponsor + county + capacity ± 10 % + technology → fuzzy name); deterministic keys link with confidence 1.0;
probabilistic candidates between configured thresholds go to the model gateway with the pair and rationale
logged (`docs/20` §3.5); a human `resolution_decision` short-circuits later automated decisions on the same
pair (`docs/21` §6.4); `link_method` and `link_confidence` recorded on every link (`docs/21` §3.2). A resolver
change ships only with a re-run evaluation (DA-9). *Why:* precision ≥ 0.9 is a product commitment (M-2).

**DA-8 Labelling protocol.** Labels live in `data/eval/labels.csv` with columns `pair_id, left_id, right_id,
label ∈ {match, non_match, unsure}, key_type, labeller, labelled_at, evidence_url, note`; sample stratified by
key type with ≥ 500 pairs and ≥ 100 hard negatives (same sponsor or same county, different project); a 10 %
overlap is double-labelled and Cohen's κ ≥ 0.8 is required before the set is used (else revise the labelling
guide and relabel); `unsure` is excluded from metrics and reported; labels are never edited in place — a
correction is a new row that supersedes by `pair_id` + later `labelled_at`. *Why:* a metric on an unreliable
label set is a number, not evidence.

**DA-9 Evaluation before shipping.** Every resolver, extraction or matching version has an evaluation report
under `data/eval/reports/<version>.md` with measured (never estimated) precision, recall, per-key-type
breakdown, confusion examples and the exact command to reproduce; ship gates: resolution precision ≥ 0.9,
recall ≥ 0.7 (`docs/10` A-9); extraction per-field precision thresholds set per field before enabling auto-accept
(`docs/20` §3.6); matching only replaces rules if the measured gain is reported (`docs/10` §3.1). *Why:*
data-scientist brief — "report measured numbers".

**DA-10 Snapshot and document retention.** Raw snapshots 24 months then monthly samples; `snapshot` rows
forever (`docs/20` §3.2, A-7); documents retained per their licence's `allows_*` flags; `model_call` prompts 90
days, outputs kept; sessions 30 days; alerts 12 months (`docs/20` §11). Retention runs as a scheduled job with
its own `source_run`-style log. *Why:* snapshots are the evidence in a licence dispute (`docs/20` §12 last row).

**DA-11 Publication follows the reuse-class checklist.** Before any source moves to `api_only` or `public`,
invariant L1 holds (`docs/21` §3.19: reuse class `open`/`attribution`, `gate_flag = false`, evidence URL, date
and reviewer) and the per-class behaviour table of `docs/21` §8 is what the surfaces do — including the six
"may not show" items for `restricted`/`unknown`, and the credit line for `attribution`. The publication matrix
in `docs/13` §6 is the legal register's view; when it and `data/sources.yaml` disagree, the YAML is corrected
by legal-compliance before publication, never worked around in code. *Why:* the gate is a product requirement
(US-905), enforced by a `CHECK` and a trigger, not by the UI.

**DA-12 Registry hygiene.** `data/sources.yaml` is the single source of truth for sources; every field in its
field guide is filled; `verified` is updated after every probe; `egress` is added per `docs/20` §4.3
(`docs/21` C-6, Sprint 1); a source without a connector is `unimplemented` in admin, never silently absent;
private aggregators (`CLAUDE.md`) fail to register (`docs/20` §4.3). *Why:* the manifest drives scheduling,
egress and gating; a stale manifest is a stale gate.

**DA-13 Personal-data minimisation** is `docs/13` §5.4 rules 1–7, adopted verbatim: strip contact identifiers
at ingest; retain name/role/organisation only for professional filers, flagged `personal_data: true`; never
publish contact details on any tier; never use ingested personal data for outreach (separate store, no join
key); privacy notice before first public page; deletion/objection within 30 days with a salted-hash
suppression list that survives re-ingestion; annual retention review and 24-month purge on withdrawn projects.
Submitted intake contacts are consented, deletable and live on `task` + CRM, never on `organization`
(US-1001 AC2, `docs/21` C-7). The personal-data inventory (`docs/10` §8.2 item 5) is enforced by the schema
check in US-910 AC2. *Why:* it moves Bankable from "data broker" to "publisher of project records"
(`docs/13` §5.4 inference).

### 4.3 Onboarding a new source

**DA-14 A source ships only through these eight steps, in order**, each leaving an artefact:
1. **Registry entry** in `data/sources.yaml` with every field-guide field, `reuse` set honestly (`unknown` if
   terms not read), and a probe result in `data/probes/`.
2. **Legal review** by legal-compliance: operative clause quoted verbatim with URL and retrieval date in the
   registry `notes`/`license` fields and in `docs/13`; classification; whether raw, derived, exact geo, bulk and
   API redistribution are allowed (the `licence` row fields, `docs/21` §3.19); counsel flag if needed.
3. **Connector** under `pipeline/connectors/<source_id>/` implementing `docs/20` §3.1's protocol, with a stable
   `source_record_id` strategy in the docstring, egress class declared, host politeness limits configured.
4. **Fixtures** per E-6 and a `status_map.yaml` per DA-5.
5. **DQ baseline**: three successful runs recorded with row counts and vocabulary; DA-6 thresholds set for the
   source; cost per changed record measured (`docs/20` §6).
6. **Resolution check**: which `docs/02` §5 keys the source provides; evaluation sample extended if it adds a
   new key type (DA-8).
7. **Publication decision** recorded in admin (`publish_state`) and, for `attribution` sources, the exact credit
   string and link-back rule; invariant L1 satisfied; US-906 fixture extended if the source adds a new
   reuse class or field class.
8. **Docs and changelog**: `docs/02` coverage map updated if the source changes coverage; `docs/CHANGELOG.md` line.
*Why:* `docs/10` §3.3 gates and `docs/03` §3 loop; a source that skips step 2 is the one that ends up in a
takedown letter.

---

## 5. API

**API-1 `docs/23` is normative; the generated `api/openapi.yaml` must match it** (`docs/23` preamble, §12); the
Redoc page at `/docs` is the public documentation; every operation carries `x-tier`, `x-stories`, an example
and the `x-status: planned` marker until its sprint. *Why:* the contract is the product for S2 (d).

**API-2 Cursor pagination only** (`docs/23` §7): `limit` default 50, max 200 (1,000 on `/bulk/*`); opaque
cursor valid 24 h; `page.{next_cursor, prev_cursor, has_more}`; `meta.total` only with `include=count` and
`total_is_estimate` honest above 10,000. No offset parameter exists. *Why:* concurrent ingestion (US-101 AC2).

**API-3 Filter grammar** is `docs/23` §7 exactly: `field=value`, `field=a,b` (OR within facet), `field[op]=value`
with `gte, lte, gt, lt, from, to`; facets AND together; `sort=-field,field` from the per-resource allowlist;
unknown parameters → `400 unknown_parameter`, never ignored. The web UI uses the same parameter names in its
URLs (D-17). *Why:* a silently dropped filter on a licence-sensitive surface is a leak (`docs/23` §7).

**API-4 Error model** is RFC 9457 with the `docs/23` §8 code table (E-17); `not_found` for gated records
(existence must not leak); `redactions[]` in a `200` envelope for partial withholding, never an error. *Why:*
`docs/21` §8 item 3.

**API-5 Attribution envelope on every response** — list, detail, feed item, export row, webhook delivery:
`data`, `page`, `meta` (`tier, lag_days, data_as_of, generated_at, request_id, terms_url`), `licence_summary`
(per-source entries plus `attribution_line`), `redactions[]`, and per-record `provenance[]` (`docs/23` §10).
CSV: provenance columns on every row plus the `#` header block (US-105 AC2, US-603 AC2). A response that omits
attribution for a source present in the payload is a release-blocking defect (`docs/21` §8). *Why:* the API
states attribution so no client has to remember to.

**API-6 Rate limits and quotas** are the `docs/23` §6 table (from `docs/10` A-8), configurable per plan and key;
headers `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`, `RateLimit-Policy` on every response
(IETF draft, https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/); `429` with `Retry-After`;
every request logged with key id, endpoint, status, latency (US-702 AC3). *Why:* M-7's source and the free
tier's anti-mining control.

**API-7 Versioning and deprecation.** Path-versioned; `v1` frozen at MVP launch; additive changes (new optional
fields, new enum values in vocabularies that are documented as open, new endpoints) are non-breaking;
removing or renaming a field, changing a type, tightening validation or changing default sort is breaking and
requires `v2`. Deprecation: announce in the changelog and `/docs`, add `Deprecation` and `Sunset` headers
(RFC 8594, https://www.rfc-editor.org/rfc/rfc8594) on affected operations, keep the old version ≥ 6 months
after `Sunset` announcement with an email to every key owner who used it in the prior 90 days. Vocabulary
additions (a new `lifecycle_state`) are announced ≥ 30 days ahead because clients switch on them. *Why:*
US-703 AC3; S2 integrations are internal models nobody wants to rebuild.

**API-8 Keys and scopes** per `docs/23` §5: `bk_live_`/`bk_test_` prefixes, shown once, SHA-256 stored, ≤ 5 per
user (US-701), scopes `read:public, read:live, read:bulk, write:webhooks, admin:*`, `scopes ∩ plan_tier`
evaluated per request from the entitlement mirror (TTL ≤ 15 min), revocation ≤ 60 s, licence acceptance
recorded with version and timestamp at key creation (US-704 AC2). `admin:*` is never issued to a customer key.
*Why:* keys are the paid product's boundary.

**API-9 Idempotency, request ids, caching, transport** per `docs/23` §1: `Idempotency-Key` on every mutating
endpoint (24 h replay), `X-Request-Id` on every response and in every error, public GETs cacheable for 300 s
with `ETag`, Pro/API `private, no-store`, HTTPS + HTTP/2 only, RFC 3339 UTC timestamps, `snake_case`. Webhooks
signed per `docs/23` §5 (`X-Bankable-Signature`, 5-minute tolerance). *Why:* these are the defaults integrators
assume; deviating costs support time.

**API-10 Public ids only.** Internal UUIDs never appear in URLs, responses, logs shipped to customers or error
bodies (`docs/23` §1). *Why:* enumeration and information-leak surface.

---

## 6. Security and privacy

**S-1 Threat model (summary; full model is a Sprint 1 solutions-architect deliverable).** Assets: the canonical
store and event log; licence-compliance evidence (snapshots, `licence` rows); customer personal data (`user`,
`alert.recipient`, intake contacts); API keys, sessions and webhook secrets; social and source credentials; the
model budget; the owner's professional reputation. Threat actors and vectors: bulk miners of the free tier
(rate limits, edge rules); credential stuffing and session theft (magic links, HTTP-only cookies, revocable
sessions); licence disputes (evidence and gates); SSRF and egress abuse through connector URLs, including
curated issuer URLs added in admin (host allowlist compiled from the registry, `docs/20` §11); prompt injection
from source documents into extraction and drafting (typed JSON outputs validated against schema, no tool use
from extraction, post drafting never sees source text — `docs/32` §4.2); supply-chain compromise (E-20);
operator or agent error (audit events, human gates). Target: OWASP ASVS 5.0 Level 2 for the API and admin
surfaces, Level 1 for public pages (https://owasp.org/www-project-application-security-verification-standard/).
*Why:* the controls below each map to one of these.

**S-2 Authentication.** Passwordless email magic link (single use, 15-minute expiry, bound to the requesting
browser via a nonce cookie) and Google sign-in; sessions are signed HTTP-only `Secure` `SameSite=Lax` cookies
backed by revocable server rows, idle expiry 30 days (`docs/20` §7, §11); admin requires role `operator`/`owner`,
the separate `admin.` hostname, and a second factor via the identity provider (`docs/20` A-10); seat limits
enforced (US-602 AC2). *Why:* ASVS V2/V3 at L2 without a password database to breach.

**S-3 Authorisation is the visibility predicate plus roles and scopes.** Every read path calls `visible(r, t,
now)` (`docs/21` §5.4); roles `viewer, member, operator, owner` on `user.role`, entitlements
`public, pro, api, admin` on `account.entitlement` (`docs/21` C-5); key scopes per API-8; Postgres roles
`api, worker, admin_api, readonly` with least privilege (`docs/20` §11). Authorisation decisions are never made
in templates or client code. *Why:* one predicate, one place to test.

**S-4 Audit logging.** Every admin write is an `event` with `actor_type = user`, `actor_user_id`, `reason`
(required, US-905 AC3), `before`/`after` (US-901 AC2); key issue/revoke, auto-publish toggles, gate clearances
and lag changes are events; audit events are append-only like all events and readable in admin. *Why:* the
audit chain is the same log as the product — no second system to drift.

**S-5 Egress control.** Workers reach only hosts allowlisted from `data/sources.yaml` per egress class
(`docs/20` §4.3, §11); no CAPTCHA solving or challenge bypass; residential egress only per source after
legal-compliance records the terms, never for `restricted`; the residential credential exists on exactly one
pool. *Why:* the legal posture of `docs/13` §3 depends on never circumventing an access control.

**S-6 Data protection.** TLS everywhere including database connections; object storage private with pre-signed
URLs (`docs/20` §4.4); personal data limited to the inventory (DA-13); no personal data or raw restricted
payloads in logs, error bodies, analytics events, canvases or fixtures. *Why:* ASVS V6/V8; the inventory is the
control surface.

**S-7 Key and secret rotation.** Platform secrets (database, object storage, provider API keys, social tokens,
SOPS age keys): rotate ≤ 90 days and immediately on any personnel change, suspected exposure or vendor
incident; rotation is a runbook (O-9) with a dated log under `infra/`. Customer API keys: self-service
rotation with overlap (create new, revoke old); revocation ≤ 60 s. Webhook secrets rotate with a
dual-signature window of 24 h. Session signing keys rotate quarterly with a grace window. *Why:* a rotation
that has never been practised will fail during the incident that needs it.

**S-8 Deletion requests.** A task type (US-910) with a 30-day SLA (`docs/13` §5.4 rule 6): redaction procedure
(`docs/21` §6.6), sessions and keys revoked, alerts suppressed, CRM/billing deletion issued through the ports
(ADR 0006), suppression hash written, confirmation sent; a test proves the fields are gone from every surface
and export (US-910 AC1). Requests from named filers follow the same path for the personal fields on the
record. *Why:* "honour deletion requests" is a guardrail, and a deletion undone by the next crawl is not one.

**S-9 Incident response.** Severities: **S1** any gated or restricted data visible on a non-admin surface
(M-11 breach), credential exposure, an outbound message sent without a human (`docs/33` §8), personal data
exposure; **S2** licence-dispute letter, source-terms change affecting a published source, API 5xx > 1 % for
30 min, backup failure; **S3** single-source failures, cost-ceiling alerts. S1 response: within 1 hour set the
source or record to `unpublished` (an event), revoke exposed credentials, preserve evidence (snapshots, logs),
notify the owner; within 24 h notify affected customers or persons where required; within 5 business days a
postmortem under `docs/6x-incidents-YYYY.md` (timeline, cause, what the gate missed, fixes with PR links) and
a `docs/CHANGELOG.md` line. Legal-compliance is on every S1/S2. *Why:* the guardrails define what an incident
is; this defines what happens next.

**S-10 Outbound and automation boundary.** No reply, DM, follow or like endpoints exist in the integration
layer (US-804 AC1, tested); `auto_publish` per channel is owner-only and audited (`docs/20` §11); disclosure
strings per G-3. *Why:* `CLAUDE.md` guardrail; platform rules; the owner's reputation is an asset (S-1).

---

## 7. DevOps and operations

**O-1 Environments.** `local` (Compose with Postgres + object-storage containers, fixtures only), `ci`
(ephemeral per run), `preview` (per PR, web + API against a seeded fixture database; no outbound to sources,
social or CRM — adapters in dry-run), `staging` (full pipeline against fixtures plus ≤ 5 open sources on their
real cadence; social and CRM adapters in dry-run; production-shaped secrets, separate credentials), `production`.
Configuration is twelve-factor (https://12factor.net/): environment variables only, no per-environment code
paths. *Why:* ADR 0005; preview environments are the design review surface (D-37) and the CWV measurement
point (D-31).

**O-2 Everything reproducible from the repo.** OpenTofu for cloud resources, Compose files per environment,
SOPS + age for secrets, one container image with per-process entrypoints (`docs/20` §4.1); no console-clicked
resource survives a `tofu plan` diff. *Why:* devops brief; a solo operator cannot reconstruct undocumented
infrastructure.

**O-3 CI gates that block merge** (all required on `main`): `ruff` + `ruff format --check`; `mypy --strict`;
`pytest` unit + contract + integration (E-6…E-9, E-12) with coverage floors (E-7); OpenAPI generated == committed
(E-8); migration up/down (E-11); `import-linter` (E-2); `gitleaks`; `pip-audit`; image build + `trivy` scan;
licence-gate fixture (US-906); `axe` zero serious/critical and Lighthouse CI budgets on the three key pages
(D-31); docs link check on `docs/`. E2E smoke (E-10) runs on preview and post-deploy, not on every push. *Why:*
"lint before commit" is a habit; a required check is a gate.

**O-4 Deploy.** Tag on `main` → image built once, pushed by digest → `docker compose up` over SSH per VM in
order `worker-*` (drain, stop), `api`/`web` (behind the edge cache), `scheduler`; migrations run before the
new image starts (expand phase) and never as part of app startup; a deploy is recorded (tag, digest, who,
when, migration ids) in the deploy log under `infra/`. *Why:* `docs/20` §12 — public pages serve from the edge
throughout.

**O-5 Rollback.** Re-deploy the previous digest (kept ≥ 5 releases); downgrade a migration only if its
docstring says reversible, otherwise the expand/contract discipline (E-11) means the old image runs on the new
schema; a `replay` (`docs/21` §6.5) repairs entity tables after a bad deploy; rollback is rehearsed on staging
each sprint. *Why:* a rollback that needs a migration downgrade under pressure is the wrong design.

**O-6 Observability minimums.** Per source (dashboards + alerts, `docs/20` §10): success rate, run latency,
rows seen/new/changed/gone, DQ status (DA-6), `blocked`/`failing` flags, egress class, cost per changed record;
alert on failing > 2 cycles, queue age > 2× cadence, DQ hold. Per model call (`model_call` row, `docs/20` §4.5):
purpose, alias (never a provider id), prompt-template version, subject (`source_id`, entity, snapshot), input
and output tokens, USD, latency, cache hit, error; dashboards show USD per new/changed record per source over
30 days and daily budget consumption; alert at 80 % of any budget. Per API: requests by tier and status, p95
per endpoint (E-16), 5xx rate; alert at 5xx > 1 %. Per publisher: publish success, expired drafts, budget holds
(`docs/32` §6.1). Logs and traces per E-18. *Why:* `docs/03` §6 cost discipline and M-9/M-10/M-12 are computed
from these.

**O-7 Backups and restore drills.** Managed Postgres daily snapshot + PITR (RPO ≤ 1 h); object storage
versioned; RTO ≤ 4 h for the database; a monthly restore drill into a scratch database that runs the
integration fixtures and records date, duration, restored LSN/time and outcome in the runbook; alert if backup
age > 26 h (`docs/20` §10, §11). *Why:* the store is the system of record for a data product; snapshots are
legal evidence.

**O-8 Cost ceilings.** Core infrastructure ≤ USD 415/month (`docs/20` §14 upper bound; exceeding it needs an
owner decision in `docs/00-PLAN.md`); model spend: per-source daily budget and a global daily budget in
gateway config, demotion threshold USD 0.50 per changed record (`docs/20` §6, A-9); X posting credit cap
USD 250/month in `config/social.yaml` (`docs/32` §4.7; `docs/20` §14's USD 300 is the estimate at 50 posts/day,
not the cap); per-post model cost ≤ USD 0.01 (`docs/32` §4.2). Every ceiling has a metric and an 80 % alert
(O-6); monthly cost review in the sprint review (G-11). *Why:* `docs/01` §3.4 ceiling; a source that costs more
than its records are worth is dropped (`docs/03` §6).

**O-9 Runbook format.** One file per procedure under `docs/6x-runbooks/` (or one section each in
`docs/60-runbooks.md` until there are more than ten), with the headings: **Title · Trigger / symptom ·
Severity · Preconditions and access needed · Steps** (numbered, copy-paste commands, expected output after
each) **· Verification · Rollback · Escalation · Last executed (date, by role, outcome)**. Required at launch:
deploy, rollback, restore drill, secret rotation, source-blocked triage, DQ-hold release, unmerge, takedown /
unpublish, deletion request, incident (S-9), residential-egress enable, X-budget hold. A runbook not executed
or drilled in 6 months is marked stale in its header. *Why:* devops brief ("runbooks in `docs/6x-*.md`"); a
runbook is only trusted if it has been run.

**O-10 Supervision.** A scheduled supervision session reviews source health, DQ holds and cost dashboards and
opens triage PRs (`docs/03` §1); it never publishes, sends or clears a gate. *Why:* the human gates in
`docs/03` §4 stay human.

---

## 8. Go-to-market and content

**G-1 ICP discipline.** Every account in the CRM carries one segment from `docs/33` §1 (S1–S6 mapping to
`docs/10` §2), a lead band from the `docs/33` §2.3 rubric, and the `docs/33` §2.2 fields; research uses public
sources only (`docs/33` §2.1); no account is worked outside the 90-day segment priority (`docs/33` §1.7)
without a note saying why. *Why:* the pre-sell (M-5) is measured per segment.

**G-2 The human-sends rule.** No agent, pipeline or scheduler sends an email, DM, connection request, comment
or reply to a person; drafts land in the CRM (`draft_by = agent`) or the review queue and a named human sends
from their own account (`docs/33` §8, `docs/32` §5, `CLAUDE.md`). Alerts and digests subscribers opted into are
the one automated channel and carry the disclosure footer. A send without a human is an S1 incident (S-9).
*Why:* platform rules, marketing law and the owner's reputation (`docs/03` §4).

**G-3 Disclosure text is fixed and versioned** (D-36): outreach email footer and profile statement from
`docs/33` §8; channel bios and the email footer from `docs/32` §2.2; the automated-channel sentence from
`docs/33` §8 if the owner ever enables one, with the channel named in writing in `docs/00-PLAN.md`. Automated
social accounts carry the platform's automation label where it exists (X) and state it in the bio elsewhere
(`docs/32` §1.3–1.5). *Why:* disclosure is a guardrail and a launch-checklist item (US-803 AC3).

**G-4 Editorial standards for posts and alerts** are `docs/32` §3: which events earn a post (§3.1), the post
anatomy (§3.2), the per-event templates (§3.3), the style guide and corrections policy (§3.4); the hard
validation gates in §4.3 all pass before a draft reaches the queue; graduation to auto-publish only per §4.6
with the owner's name and date in config; LinkedIn never auto-publishes. Alerts (Pro) use the same fact-line
grammar and attribution as posts. *Why:* posts are generated from structured fields only; the style guide is
what keeps them defensible.

**G-5 Nothing from gated sources is ever posted, alerted or digested** (`docs/32` §3.1 "never posted",
US-801 AC3, `docs/21` §8 item 5); event names used by the publisher map to `docs/21` §7.3 (§10 conflict 4).
*Why:* the social feed is a public surface under the same gate.

**G-6 Pricing and packaging change control.** The reference ladder is `docs/11` §3 (Free / Pro USD 149 /
Team USD 9,000 / API +5,000 or 25,000) with the delay schedule there; the talk-track in `docs/33` §6 and the
billing configuration must match it. A change requires: an owner decision in `docs/00-PLAN.md`, edits to
`docs/11` §3 and `docs/33` §6 in the same PR, the billing-provider plan change through `BillingPort` config, a
`docs/CHANGELOG.md` line, and a note to existing customers if their price changes. No agent commits a price or
a discount; pilot terms stay within `docs/33` §6.3. *Why:* two documents and a billing console that disagree
is how a customer gets three prices.

**G-7 CRM hygiene.** Minimum fields per `docs/33` §7.1; pipeline stages per §7.2 with agents allowed only 0 → 1
and activity logging, humans moving ≥ 2; opt-outs processed immediately and verified weekly, honoured within
10 business days (`docs/33` §3.1, §7.3); dedupe weekly; no personal data beyond business contact details from
public professional sources (sales-bd brief); no field outside the `docs/33` §7.1 lists without a note in that
doc; the app stores only `sor_ref`, `billing_ref`, `crm_lead_ref` (ADR 0006). *Why:* the CRM is the system of
record for M-4, M-5 and M-13.

**G-8 Partnership and licensing approvals.** Term sheets follow the outlines in `docs/33` §5 and never offer
what §5.3 excludes (raw restricted rows, personal data, first-year exclusivity, attribution removal); sequence:
sales-bd drafts → legal-compliance reviews (counsel where flagged, `docs/13` §7) → owner negotiates and signs
(needs the entity, open question 3); a signed data licence becomes a `licence` row with `contract_ref` and
`expires_at` (`docs/21` §3.19) and a `docs/00-PLAN.md` decision. Renewal dates are on the compliance calendar
(Phase 6). *Why:* `docs/03` §4 — licences and contracts need a human signatory.

**G-9 Outreach compliance.** Every sequence obeys `docs/33` §3.1 (fact with `source_url` in every message,
three touches then stop, CAN-SPAM footer, no LinkedIn automation, TCPA rules for calls); EU/UK sends wait for
legal-compliance's outreach checklist (`docs/10` §8.2 item 6). *Why:* legal exposure sits with the sender, who
is the owner.

**G-10 Content and post templates are artefacts under review** (R-1): a post card template ships with the
attribution and disclosure baked in (product-designer brief), a rendered example per channel at that channel's
limits, and the validation gates run against the example. *Why:* templates are where a missing credit line
replicates a thousand times.

**G-11 Metrics review cadence.** Weekly: social report Monday 08:00 ET (`docs/32` §6.2) and pipeline report
Monday (`docs/33` §7.3), both agent-drafted, owner-read. Sprint end: M-1…M-13 against targets and kill signals
(`docs/10` §5) in the owner review of `docs/00-PLAN.md` (`docs/03` §5), plus the cost ceilings (O-8). Monthly:
lead-scoring weights (`docs/33` §8). Quarterly: ICP priority, pricing ladder, delay schedule. A metric with no
number at review is reported as "not measured" with the blocker, never omitted. *Why:* the kill signals only
work if someone reads them on schedule.

---

## 9. Review and enforcement

### 9.1 Definition of done per artifact type

**R-1** An artifact is done when every applicable line below is met or explicitly marked "not met — blocker:
…"; the reviewer (R-2) records the check in the PR or in the doc's status line.

| Artifact | Done when |
|---|---|
| **Doc** (`docs/*.md`) | Status line (P-10); one topic; sections numbered and stable; every number sourced or tagged as assumption (P-7, P-8); references by section not restatement; decisions in `docs/00-PLAN.md`; `docs/CHANGELOG.md` line; British spelling in prose; no model identifiers; reviewer named in `docs/03` §2 has read it |
| **Connector** (`pipeline/connectors/<source_id>/`) | DA-14 steps 1–8 artefacts present; `docs/20` §3.1 protocol; stable `source_record_id` documented; fixtures per E-6 with no personal or gated data; `status_map.yaml` per DA-5; DQ thresholds set; egress class declared and host limits configured; three baseline runs logged; `mypy --strict` and `ruff` clean; CHANGELOG line |
| **API endpoint** | In `docs/23` (or `docs/23` updated first); generated OpenAPI matches (E-8); envelope and provenance (API-5); visibility predicate applied and E-9 fixtures pass; RFC 9457 errors with table codes; rate-limit headers; cursor pagination; contract test and example; p95 within E-16; request logging; `x-tier`/`x-stories` set; no internal ids |
| **UI screen** | Derived from `docs/30-design-references.md` (D-6) and no banned pattern (D-7); tokens only (D-20); contrast recorded (D-21); WCAG 2.2 AA checks and `axe` clean (D-31); keyboard pass done; 400 px verified (D-32); CWV budgets on preview (D-31); provenance affordance on every data element and attribution line in footer (D-2, D-28, D-35); delayed-tier notice (D-3, D-29); empty/loading/error states (D-30); filters in URL (D-17); JS budget (D-33); motion rules and reduced-motion (D-15); component tests + smoke path (E-10); frontend and product-designer review |
| **Post template** | Matches `docs/32` §3.2 anatomy and §3.3 template for its event type; attribution and disclosure strings from the versioned strings file (D-36); rendered example per channel within limits; `docs/32` §4.3 gates pass on the example; lag notice present for `proposal.*`; no banned words; UTM per §3.2; content-social and product-designer review |
| **Outreach sequence** | Segment named (G-1); every touch has a graph fact with `source_url` and a public company fact (`docs/33` §3.1); three touches max; compliance footer and disclosure line (G-3); opt-out route; personalisation tokens only from the `docs/33` §3.1 list; marked `draft_by = agent`, sent-by field empty; EU/UK gated on the legal checklist; legal-compliance review where flagged; owner is the sender |

### 9.2 Who reviews what

**R-2** Review routing is the `docs/03` §2 table, restated only as a lookup:

| Producer | Reviewed by | Also consulted when |
|---|---|---|
| product-manager | owner | — |
| market-researcher | product-manager | — |
| legal-compliance | owner (+ counsel where flagged, `docs/13` §7) | any source publication (DA-14 step 2), any outbound channel (G-2, G-3) |
| solutions-architect | owner | any PR changing `docs/20` §15 rows or an ADR |
| data-engineer, data-scientist, backend-developer | qa-engineer | legal-compliance on any gate/licence code path; solutions-architect on schema changes (DA-1) |
| frontend-developer | qa-engineer, product-designer | — |
| product-designer | product-manager | frontend-developer on feasibility; owner on canvases (D-37) |
| devops-engineer | solutions-architect | — |
| qa-engineer | product-manager | release sign-off against US-908 |
| content-social, sales-bd | owner | legal-compliance on disclosure, footer or template changes |

**R-3** A reviewer checks against this document by rule id and the R-1 row; "looks good" is not a review.
Findings are written as `rule id — what fails — what would pass`. *Why:* the owner asked for a standard a
reviewer can check line by line.

**R-4** The qa-engineer's release sign-off (US-908) additionally confirms: E-9/E-10 green on production-shaped
data, M-11 = 0 in the nightly audit, attribution on every surface, privacy notice and deletion route live,
automation labels set, rate limits active, unsubscribe works, backup age < 26 h, ADRs match the running system
(ADR 0001), and the exceptions register (§9.4) has no expired entries. *Why:* the launch checklist is the
enforcement point for everything above.

### 9.3 Self-check before requesting review

**R-5** The author runs the R-1 row for their artifact type and lists the rule ids they claim, the ones they
believe do not apply (with a reason), and any they are asking an exception for. Reviews of artefacts without
this list are returned unread. *Why:* it halves review time and catches the "did not know the rule" class.

### 9.4 Exceptions

**R-6** A departure from a MUST is recorded in the exceptions register below (and nowhere else) with: id,
rule id, artifact, reason, compensating control, approver (the reviewer in R-2, plus the owner for anything
touching gates, licences, personal data or outbound messaging), date, expiry (≤ 1 sprint by default). An
expired exception is a release blocker (R-4). Exceptions never apply to `CLAUDE.md` guardrails, to the
visibility predicate, to the six "may not show" items (`docs/21` §8) or to the human-sends rule.

| Id | Rule | Artifact | Reason | Compensating control | Approver | Date | Expiry |
|---|---|---|---|---|---|---|---|
| — | — | — | (none recorded) | — | — | — | — |

---

## 10. Conflicts between existing docs, and what this standard picks

| # | Where | Conflict | Pick | Why |
|---|---|---|---|---|
| 1 | `docs/20` §5 and A-8 (7 days) vs `docs/10` A-7 / `docs/21` D-1 (14 days) vs `docs/11` §3 (7 for opportunities, 14 for supply, 30 weekly, none quarterly) | Default public lag | `docs/11` §3 schedule, defaults 7 opportunities / 14 supply, per source class | It is the `docs/00-PLAN.md` working default (2026-09-12) and the only one tied to a pricing argument; `docs/21` §5.4 already makes lag configuration per source and event type, so nothing structural changes. US-101 AC3's lag±1 fixture holds per class |
| 2 | `docs/20` §5 (PJM derived aggregates to Pro/API) vs `docs/10` §3.2–3.3, `docs/21` D-2/C-3, `docs/23` P-4 (nothing on any non-admin tier) | Restricted/unknown sources on Pro/API | Nothing on any non-admin surface | `docs/00-PLAN.md` working default takes the safer reading; `CLAUDE.md` says PJM is not public until a licence exists and the terms question is open in `docs/13` §7 item 1. Reversible in configuration when counsel answers |
| 3 | `docs/02` §1 / `docs/10` §4 / `docs/21` §7.1 (eight states + `unknown`) vs `pipeline/status_map.yaml` v2 (adds `under_construction`) | Lifecycle vocabulary | Adopt `under_construction` between `contracted` and `built`; `docs/21` §7.1 and the `vocabulary` table to be updated by solutions-architect | The status map's note is right: EIA-860M distinguishes it and it is the most commercially useful signal (S4 job a). Vocabularies are `text + CHECK`, so the addition is cheap; API-7 requires the 30-day announcement before it appears in `v1` responses |
| 4 | `docs/32` §3.1 event names (`proposal.new`, `proposal.status_changed`, `opportunity.rfp_opened`…) vs `docs/21` §7.3 (`created`, `status_change`, `opened`…) | Event-type vocabulary | `docs/21` §7.3 is the vocabulary; the publisher's filter maps `docs/21` types to `docs/32` template names in `config/social.yaml` | `docs/33` §9 already defers final names to the architect; the store cannot carry two vocabularies |
| 5 | `docs/32` §4.1 (`publisher/*`, `config/social.yaml`) vs `docs/20` §2 (`services/social`) | Code location for the social publisher | `services/social/` with `docs/32`'s module names inside it (E-1) | `docs/20` §2 is the component map the deploy and import rules are built on |
| 6 | `docs/13` §6 (SPP and ISO-NE `restricted`, NYISO `attribution-restricted`) vs `data/sources.yaml` (`unknown` for all three) and `docs/21` §8 | Reuse class of SPP / NYISO / ISO-NE | The YAML is the runtime truth and currently says `unknown`, which gates identically to `restricted`; legal-compliance updates the YAML `reuse` and evidence fields from `docs/13` §6 before any of the three is published (DA-11, DA-12) | The store reads the registry, not a doc; the standard fixes who corrects which |
| 7 | `CLAUDE.md` quartet field `licence` vs `docs/21` store column `licence_id` vs CSV column `licence` | Field naming | Store: `licence_id` (FK to `licence`); API: `licence_id` + `reuse_class` + `licence_summary`; CSV: `licence` column (DA-2, API-5) | `docs/21` is the schema; the guardrail names the concept, not the column |
| 8 | `docs/20` §14 (X ≈ USD 300/mo at 50 posts/day) vs `docs/32` §4.7 (X credit cap USD 250/mo) | X spend | Cap USD 250 in config (O-8); `docs/20`'s figure is an estimate, not a limit | A cap must be a single configured number |
| 9 | `docs/10` US-103 AC3 (search < 500 ms p95) vs `docs/20` §4.4 (< 100 ms achievable) and §13 (300 ms trigger) | Search latency | Budget 500 ms (the contract), scaling trigger 300 ms sustained (E-16) | The PRD number is the promise; the architecture number is the alarm |
| 10 | `docs/20` §11 (filer contacts never stored) vs US-1001 (intake collects contact name and email) | Personal data | `docs/21` C-7 distinction: scraped contacts never stored, submitted contacts stored with consent on `task` + CRM (DA-13) | Already resolved in `docs/21`; restated so the inventory check reads one rule |
| 11 | content-social agent brief reads `docs/30-social-*`; product-designer owns `docs/30-design-*`; `docs/03` §2 puts the social playbook at `docs/32` | Doc numbering | `docs/30-design-references.md`, `docs/30-design-ia.md`, `docs/31-design-system.md` are design; `docs/32` is social (`docs/10` A-13); the content-social agent file should be corrected to read `docs/32-*` | `docs/03` §2 is authoritative on ownership |

Items 3, 6 and 11 require edits to other agents' files; they are recorded here and in `docs/CHANGELOG.md`
rather than made silently.

---

## 11. External standards referenced

| Standard | URL | Used by |
|---|---|---|
| WCAG 2.2 (W3C Recommendation) | https://www.w3.org/TR/WCAG22/ — target size 2.5.8: https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html | D-5, D-14, D-15, D-31 |
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
| SIL Open Font License 1.1 | https://openfontlicense.org/ | D-22 |
| OpenStreetMap copyright and attribution (ODbL) | https://www.openstreetmap.org/copyright | D-13 |
| FTC CAN-SPAM compliance guide | https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business | G-2, G-9 |

Thresholds for Core Web Vitals, WCAG 2.2 target size and the ASVS version were confirmed against the linked
pages on 2026-09-12; contrast ratios in D-21 were computed with the WCAG 2.x relative-luminance formula.

## 12. Assumptions in this document

| Id | Assumption | Depends on | Effect if wrong |
|---|---|---|---|
| ST-1 | MapLibre GL JS (or an equivalent canvas/WebGL renderer) is acceptable as the one JS-heavy island in an otherwise htmx site | product-designer's IA (`docs/30-*`), `docs/20` §15 web row, owner question 1 | D-13/D-33 budgets re-based; no rule changes |
| ST-2 | Newsreader, IBM Plex Sans and IBM Plex Mono ship variable fonts under OFL 1.1 in the versions chosen | D-22 licence check in `docs/31` | Static instances used; licence recorded either way |
| ST-3 | Coverage floors (80 % / 100 % on gate modules) and the DQ thresholds in DA-6 are starting values | first three sprints of measurements | Tuned by decision in `docs/00-PLAN.md`; never loosened on the gate modules |
| ST-4 | ASVS 5.0 Level 2 is reachable for API and admin by launch with the controls in `docs/20` §7, §11 | Sprint 1 threat model | Gaps listed as exceptions (R-6) with expiry, or launch slips |
| ST-5 | The owner ratifies conflict picks 1–3 in `docs/00-PLAN.md` | owner review | Affected rules (D-3, D-26, DA-5, DA-11, G-5) are configuration or vocabulary changes, not structural |
