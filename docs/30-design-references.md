# Design references — the study before any screen

**Status:** Sprint 1 deliverable, v1 · 2026-09-12 · owner: product-designer · reviewed by: owner (pending)
**Authority:** `docs/04-standards.md` D-6 — this study must exist before any screen is drawn; `docs/31-design-system.md`
derives its typefaces, scales and layout from it and cites it.
**Inputs:** `CLAUDE.md`, `.claude/agents/product-designer.md` (design mandate), `docs/04-standards.md` §2 (D-1…D-36),
`docs/10-prd-mvp.md` §2 (segments) and US-104 (map).
**Product name used in this doc:** Infraqueue (placeholder, `docs/12-naming-shortlist.md`).

## 0. Method and an honesty note

Every product below was fetched live on 2026-09-12. Several returned nothing usable: a JS application shell with
no server-rendered content, an HTTP 403/429 block, or a 404 on a specific page. Where that happened this doc says
so and either (a) draws on the product's own documentation, GitHub repo, or style-guide pages, which did load, or
(b) states plainly that no design claim is made beyond what a search-indexed secondary source confirms. No detail
below is invented to fill a gap. Every entry names its source URL and retrieval date; where a secondary source
(docs subdomain, GitHub, search-indexed article) stands in for a blocked live app, that is stated in the entry.
Required set per the design mandate: Electricity Maps, Grid Status, Interconnection.fyi, Felt, Kepler.gl,
Observable, FT visual journalism, The Pudding, Linear, Stripe docs, Ember. Plus at least three found independently:
this doc adds Flightradar24, OpenInfraMap, Global Energy Monitor's tracker and Vercel's dashboard surfaces — four,
covering a live-tracking product, an open infrastructure map, a provenance-heavy tracker with a methodology page,
and a metrics dashboard, so the extra set is not redundant with the required one.

## 1. The brand's own site — andrewtgibson.com

Retrieved 2026-09-12. The page frames itself as a filed document, not a marketing page: the header reads
"ANDREW T. GIBSON · ORIGINATION · proposal rev N" — an explicit revision number on a business proposal, the
utility-filing idiom the brand is built on. Layout is a single left-aligned column with generous whitespace and
horizontal-rule dividers standing in for section breaks, never cards. The page discloses sourcing inline: hovering
an underlined figure reveals where it came from, rather than sending the reader to a footnote or a separate
methodology page — a working, load-bearing example of "provenance in ≤ 1 click" (D-2) that predates the standards
doc and is the reason D-2 reads the way it does.

**Takes:** the "rev N" document-identity convention for record and export headers; hover-to-reveal sourcing as one
concrete implementation of D-2's provenance affordance; dividers instead of card chrome as the default separator.
**Avoids:** the page's own density is intentionally sparse — right for a one-page proposal, wrong for a list of
hundreds of records (D-1). Infraqueue borrows the idiom, not the whitespace budget.

## 2. Product studies

### 2.1 Electricity Maps — electricitymaps.com / app.electricitymaps.com

`app.electricitymaps.com/map` returned HTTP 403 on fetch (2026-09-12); findings below are from the public
marketing page (`www.electricitymaps.com`, loaded) plus search-indexed descriptions of the live app (Feyno,
Cogimator, TechCrunch — retrieved 2026-09-12, cited for the app-only claims). The map colours grid zones on a
continuous scale from dark green (low gCO₂eq/kWh) to red (high, up to ~1500), refreshed every 15 minutes; clicking
a zone opens a breakdown panel with production/consumption mix and import/export flows; a timeline slider moves
between historical and near-term forecast states. It is built on MapLibre.

**Takes:** click-a-region-to-open-a-breakdown-panel is the same shape as Infraqueue's D-12 drawer, just triggered
by a filled polygon instead of a marker; a timeline control as a pattern worth considering for "as of" browsing
inside Pro, distinct from the tier-gated lag itself. **Rejects:** the green→red continuous choropleth is a scale
for one continuous physical quantity (carbon intensity). Lifecycle state is a discrete, unordered-ish categorical
value, not a "good→bad" gradient, so this scale must not be reused for D-25's status chips — a reviewer who paints
"withdrawn" bright red and "built" bright green because it "reads like Electricity Maps" has smuggled in a scale
that implies a false order between unrelated states.

### 2.2 Grid Status — gridstatus.io

`www.gridstatus.io` and `www.gridstatus.io/live` both returned HTTP 403 (2026-09-12). Findings are from
`docs.gridstatus.io` (loaded) and its own marketing copy indexed by search (retrieved 2026-09-12). The product is
built from named, reusable dashboard components — a "Nodal Price Map," a "Most Recent Value" tile, per-ISO live
dashboards with toggleable series — that a customer assembles rather than one fixed page per ISO; dashboards are
shareable by role within an org.

**Takes:** a small named component library (map, latest-value tile, table) reused across every ISO page is the
right model for Infraqueue's shared filter/tier/URL grammar (D-4, D-19) — one map component and one table
component, parameterised by filter, not sixteen bespoke pages. **Rejects/uncertain:** the live app did not load,
so no claim is made about its typography, colour palette or density; this is a documented gap, not a rejection.

### 2.3 Interconnection.fyi

Loaded fully (2026-09-12). A free, daily-updated US/Canada interconnection-queue tracker: "Tracking 44,042
interconnection queue requests," a US county map togglable to state view, and collapsible filters by region,
state, market (CAISO/SPP/PJM/NYISO/ERCOT/…), project type, energy source and status. A fixed "Data last updated
today" line sits near the top of the page; the site states plainly that data is "compiled daily from U.S. and
Canadian ISO and utility interconnection queues," with GridTracker as the underlying commercial operator offering
deeper CSV/Snowflake access. Visual design is deliberately plain — text links and structural headers carry almost
all of the hierarchy, with very little colour.

**Takes:** a fixed, always-visible freshness/provenance line near the top of every list and map page is exactly
D-3's requirement, just running the opposite direction (this site claims "live," Infraqueue's public tier must
say "delayed"); the county/state toggle is close to D-8's placement-precedence ladder and validates that fallback
as a real, shipped pattern, not a theoretical one; the filter taxonomy (region/state/market/type/status) maps
closely onto US-102's filter list. **Rejects:** near-zero colour differentiation between states works for
Interconnection.fyi's audience (people who already know the vocabulary and are scanning link lists), but
Infraqueue's map and list need D-25's status chips precisely because lifecycle state is the thing being scanned
for — text alone would regress the one advantage a fused, chip-coded record has over a raw queue export.

### 2.4 Felt — felt.com

Loaded (2026-09-12). Felt positions itself as a "Web GIS" and dashboard product: dark-chrome marketing site, a
top nav of Platform / Industries / Resources / pricing, and copy emphasising "automatic legends" and "strong
cartographic defaults" so a non-GIS user gets a legible map without manual styling. Dashboards pair the map with
time-series charts, categorical filters and histograms as first-class panels beside the canvas, not a separate
page. Sharing is "zero-click" — a link with view/comment/edit permissions, no export step.

**Takes:** the map-plus-stat-panel layout, where a histogram or filter list sits directly beside (not below or
behind) the canvas, is the model for how Infraqueue's map page should present the "in view" counts and the
filter rail together (D-4, D-14's synchronised results list); automatic-legend generation is the right instinct
for D-10's cluster glyph — the legend should be computed from what's on screen, not hand-authored per screen.
**Rejects:** the marketing site's decorative "island-like" organic shapes are ornamental illustration adjacent to
the stock-illustration ban (D-7); Infraqueue's utility-filing idiom has no use for them.

### 2.5 Kepler.gl — kepler.gl / docs.kepler.gl

`kepler.gl` itself returned only a bare title on fetch (2026-09-12, a client-rendered app shell); `docs.kepler.gl`
loaded with substantive content. Kepler.gl is built on MapLibre GL plus deck.gl specifically so it can "render
millions of points … and perform spatial aggregations on the fly" in WebGL, not as DOM markers. Its UI is a side
panel (map-style picker, per-layer colour/filter controls, a themeable header) that a power user configures to
build a view; the panel and its colours are themeable via a JS theme object (`sidePanelBg`, `titleTextColor`, …).

**Takes:** the deck.gl/MapLibre GL WebGL-with-runtime-aggregation architecture is direct evidence that D-13's
budget (20,000+ records, canvas/WebGL, never one DOM node per marker above 500) is achievable at far larger scale
elsewhere — Kepler.gl's own headline use case is an order of magnitude past Infraqueue's target; a themeable
side-panel shell (one component, swappable tokens) is the right shape for a shared filter rail (D-19). **Rejects:**
Kepler.gl's panel is a configuration surface for an analyst building a view (pick a field, pick a colour ramp,
pick an aggregation) — Infraqueue's public and Pro map users are reading a curated view, not building one; that
configuration surface, however elegant, is the wrong job for a reader and must not appear on non-admin surfaces.

### 2.6 Observable — observablehq.com

Both `observablehq.com` and `observablehq.com/explore` returned HTTP 429 on fetch (2026-09-12, two attempts each).
Findings are from Observable's own documentation pages that loaded via search (`observablehq.com/documentation/
notebooks`, `observablehq.com/@tmcw/responsive-notebook-design-protips`, `observablehq.com/@davidrleonard/styles`
— retrieved 2026-09-12) and are limited to what those pages state. A notebook interleaves markdown prose, code
cells (JS/SQL/HTML) and their rendered output; "Observable Inputs" is a small, fixed set of interface primitives
(buttons, sliders, dropdowns, tables) reused across notebooks rather than bespoke widgets per notebook; page-level
CSS sets type, colour and spacing once for the whole document.

**Takes:** one small, fixed set of interaction primitives reused everywhere (their Inputs) is the same discipline
D-19 asks for (tokens are the only way to reference colour/type/space) applied to interactive controls, not just
static styling — Infraqueue should have one filter-chip, one date-range, one multi-select, not a bespoke control
per screen. **Rejects/uncertain:** no claim is made about Observable's live typography or colour palette, since
the live app did not load either time; the one thing safe to reject on principle regardless is a notebook's
code-cell-and-run-button surface leaking into a reader-facing product — Infraqueue's detail pages are
server-rendered per D-18, not executed client-side documents.

### 2.7 FT visual and data journalism — ft.com / ig.ft.com

Both `ig.ft.com` and `www.ft.com/visual-and-data-journalism` failed to fetch outright (2026-09-12, "unable to
fetch"). Findings are from the FT's own published style-guide repositories, which did load via search:
`github.com/Financial-Times/graphics-style-guide` and `github.com/Financial-Times/chart-doctor/tree/main/
visual-vocabulary` (retrieved 2026-09-12). The FT's public Visual Vocabulary organises chart choice by the
message a chart needs to make — deviation, correlation, ranking, distribution, change-over-time, part-to-whole,
magnitude, spatial — rather than by chart type or decoration; the FT also runs an internal tool ("Nightingale")
that takes a reporter's data and enforces the house typeface, palette and layout automatically rather than
leaving style to the individual.

**Takes:** "choose the chart by the point the reader needs to see" is a direct content-design instinct that
matches D-33 (plain, evidence-first, no adjectives) applied to graphics instead of prose; a single enforced style
tool, rather than a guideline a designer might skip, is the precedent for treating D-19's tokens as a build-time
gate (lint/CI-checked), not a style-guide page nobody reads. **Rejects/uncertain:** no claim is made about the
FT's live in-article typography, colour rendering or annotation style, because the article pages themselves did
not load; this is a documented gap.

### 2.8 The Pudding — pudding.cool

The homepage loaded (thin content); a specific story URL guessed for this study returned HTTP 404 (2026-09-12).
The homepage self-describes as long-form, data-driven visual essays, browsable by format (video, audio, "your
input") with titles that imply scrollytelling and mapping techniques ("mapping," "tracking," "similes").

**Takes:** format-as-a-facet (video/audio/interactive) is a reasonable secondary filter dimension in principle,
though not one Infraqueue's filter grammar needs (API-3 already fixes the facets). **Rejects:** no firsthand claim
is made about The Pudding's actual typography or motion, since only a thin homepage loaded — but the pattern that
must be rejected on principle regardless of what loaded is scrollytelling itself: step-triggered reveal-on-scroll
animation is built for one-time narrative consumption of a single essay, and D-15 already bans anything on
working screens beyond short, functional, interruptible transitions. A register a user rereads daily is not an
essay a reader scrolls through once.

### 2.9 Linear — linear.app

Loaded (2026-09-12). The marketing site shows the in-app board (Backlog/Todo/In Progress/Done columns, scannable
issue cards with assignee/label/date metadata) inside a dark-mode-first interface, generous whitespace, and status
colour used only functionally (priority levels, a fixed small label-chip set: "Bug," "Design," "AI," etc.). Copy
frames the whole product around speed — "restores momentum" — implying minimal, fast, non-decorative motion.

**Takes:** dark-mode-as-first-class rather than an afterthought (matches D-23 exactly); one small chip component
reused for every label/status with a fixed colour-per-value (matches D-25's "one chip component, fixed token pair"
almost verbatim); motion kept minimal and purposeful rather than ambient (matches D-15). **Rejects:** the
marketing site itself leans on a single accent hue (purple) carrying the entire brand signal, with smooth
cross-fade hero transitions — a single-hue-does-all-the-work marketing treatment with looping/cross-fade motion is
the shape D-7 and D-15 both ban; only the in-app density and chip discipline is borrowed, not the marketing
surface.

### 2.10 Stripe documentation — docs.stripe.com

Loaded (2026-09-12). The docs are organised as a flat set of named product categories (Payments, Revenue,
Platforms and marketplaces, Money management, Prebuilt components) rather than a deep feature tree, each linking
to a focused guide; Stripe's actual rendered docs pages (known publicly, not re-verified pixel-for-pixel here)
pair a left category nav with centre prose and a right-hand pinned code sample per language, monospace reserved
strictly for code/identifiers.

**Takes:** monospace exclusively for identifiers and code, never for prose, is exactly D-21/D-24's Plex Mono
rule; a flat, named top-level category list (not a deep tree) is the model for Infraqueue's own primary nav
(D-16 caps depth at 3 to any record) — Stripe's category page is the thing to copy, not its full nested reference
tree. **Rejects:** Stripe's complete documentation set is many hundreds of pages deep in places; copied literally
for Infraqueue's public site nav, that depth would breach D-16's 3-click rule. The flat top level is the takeaway;
the long tail underneath it is not a model to reproduce.

### 2.11 Ember — ember-energy.org

Attempted fetch failed with HTTP 403 (2026-09-12); no substitute documentation page was found that describes
Ember's visual design specifically (its reports and the underlying data are public, but a page describing its own
chart/type/colour system was not located). No design claim is made about Ember beyond noting the attempt and the
block; this is a gap, not a finding, and should be revisited before `docs/31` is finalised if Ember's house style
is wanted as a fifth energy-data reference.

### 2.12 Flightradar24 — flightradar24.com (found independently)

The live map returned HTTP 403 on fetch (2026-09-12, two attempts); findings are from Flightradar24's own blog
(`www.flightradar24.com/blog/inside-flightradar24/supercharging-flightradar24s-data-display/`) and its support
site, both indexed and retrieved via search (2026-09-12). The map renders tens of thousands of live, moving
aircraft in WebGL over vector basemap tiles with no fixed, discrete zoom levels (continuous zoom instead).
With roughly a thousand distinct aircraft types in the world, Flightradar24 deliberately maps many real types onto
a small set of shared icons rather than drawing a bespoke glyph per type; users can switch between three label
styles (text, airline logo, registration flag). Coverage of the product's own philosophy states it "does not
overexplain… instead letting the user click, follow, zoom, pan, filter, and wander, learning by touching the
map."

**Takes:** consolidating many real-world subtypes into a small, deliberately shared icon set is the direct model
for D-10's technology glyphs — Infraqueue should not attempt one unique icon per fine-grained technology value,
only per broad category, with the fine value in the tooltip/drawer; the WebGL-at-extreme-marker-count evidence
reinforces that D-13's 20k-record budget is conservative, not aspirational, relative to what this category of
product already ships. **Rejects:** the "no overexplaining, learn by touching" onboarding philosophy fits a
hobbyist, exploratory audience with no accessibility mandate stated; Infraqueue's map serves due-diligence and
compliance workflows under D-14, where every control must be a named, discoverable, keyboard-operable button —
implicit discovery-by-clicking is not an acceptable substitute for that.

### 2.13 OpenInfraMap — openinframap.org (found independently)

Loaded (2026-09-12, homepage and `/about`). The site renders OpenStreetMap-sourced power infrastructure (lines,
substations, plants) using MapLibre GL JS on the client with Tegola serving vector tiles. The about page states
attribution in the exact wording OSM's own licence requires: "All the data currently displayed on Open
Infrastructure Map is sourced directly from OpenStreetMap," credited "© OpenStreetMap contributors, ODbL." The
about page does not describe its colour-by-voltage legend or marker styling in text, so no claim is made about
those specifics.

**Takes:** an unambiguous, plainly worded attribution statement, on the page itself rather than buried in a
footer link, is the exact standard D-13's basemap-attribution clause and D-27's provenance panel are asking for —
and it is the same OSM/ODbL licence Infraqueue's own basemap must credit, so this is a working example of the
compliance obligation Infraqueue already has, not just an aesthetic borrow. **Rejects/uncertain:** no claim is
made about its legend or colour system, since the about page didn't describe it and the live map's rendered
legend was not separately verified; a gap, not a finding.

### 2.14 Global Energy Monitor's tracker — globalenergymonitor.org (found independently)

Loaded (2026-09-12, the Global Integrated Power Tracker project page). GEM tracks 182,400+ facilities across 200
areas (August 2026 release, per the page) with unit-level metadata — capacity, status, ownership, fuel type,
start year, retirement date, geolocation — that maps almost field-for-field onto Infraqueue's own provenance
quartet plus canonical fields. The page offers both a map ("Open Tracker map") and a dashboard/spreadsheet view
("Open Dashboard," summary tables by technology and status) as two views of the same underlying dataset, plus a
methodology section addressing coverage and accuracy limits. A FAQ entry, "What do the colored dots mean on the
map?", exists on the page, but its answer was not present in the fetched content — the legend itself is not
surfaced where the map is.

**Takes:** map-or-table as two interchangeable views of one dataset, with an explicit written methodology section
disclosing coverage and accuracy limits, is the direct precedent for D-4 (one mental model across search, map,
feed) and for how Infraqueue should write up D-9's restricted-precision fallback in plain language rather than
leaving it to a drawer footnote. **Rejects:** a legend whose meaning lives in an FAQ answer rather than on the map
itself is precisely the failure D-10 is trying to prevent — a cluster or dot whose colour code isn't visible where
the colour is used forces the reader to go hunting; Infraqueue's legend must render with the map, always.

### 2.15 Vercel dashboard surfaces — vercel.com/products/observability (found independently)

`vercel.com/dashboard` requires a login and returned only the login screen (2026-09-12); the observability product
page loaded with described dashboard screenshots. Vercel's status/monitoring UI uses a light, neutral-grey chrome
with a small, fixed, semantic colour set for HTTP status bands — green for 2xx, yellow for 4xx, red for 5xx — sits
over tabular-numeral time-series charts; monospace is reserved for paths/codes, sans for labels; Alerts, Query,
Logs and Analytics are presented as separate named sections rather than one dense mega-grid.

**Takes:** a small, fixed, semantic colour set tied to a real, bounded vocabulary (HTTP status classes) is a
validated precedent for D-25's status-chip system — a 2xx/4xx/5xx-style three-or-four-way split, not a unique
hue per fine-grained value; separating a data-dense screen into a handful of named sections (rather than one
giant table) is the right layout instinct for the admin source-health screen (US-904). **Rejects:** the observed
marketing copy around the screenshots ("Proactive Anomaly Detection and Alerting") is the generic, adjective-first
SaaS voice D-33 already bans in Infraqueue's own copy; the layout is worth borrowing, the copy register is not.

## 3. Synthesis — borrow, adapt, reject

| Pattern | Source(s) | Disposition | Rule |
|---|---|---|---|
| Fixed, always-visible freshness/provenance line at the top of every list/map/detail page | Interconnection.fyi | Borrow (inverted: Infraqueue's says "delayed," not "live") | D-3 |
| Detail drawer opened from a marker/polygon/zone, never a page navigation | Electricity Maps, Felt | Borrow | D-12 |
| WebGL/canvas rendering with server- or runtime-side aggregation, never one DOM node per marker | Kepler.gl, Flightradar24 | Borrow | D-13 |
| Small, shared icon set covering many real-world subtypes (not one glyph per fine value) | Flightradar24 | Borrow | D-10 |
| Map-plus-stat-panel layout: filters, histograms and counts beside the canvas, not below or hidden | Felt, Grid Status | Adapt | D-4, D-14 |
| Small, named, reusable component library (map, table, tile) parameterised by filter, not bespoke per page | Grid Status | Adapt | D-4, D-19 |
| Unambiguous, on-page attribution statement in the licence's own wording | OpenInfraMap | Borrow | D-13 attribution clause, D-27 |
| Map-or-table as two interchangeable views of one dataset, with a written methodology/limitations section | Global Energy Monitor | Borrow | D-4, D-9 |
| Small, fixed, semantic colour set tied to a bounded, real vocabulary (not a unique hue per value) | Vercel, Linear | Adapt | D-25, D-5 |
| One small set of reusable interaction primitives applied everywhere | Observable | Borrow | D-19 |
| Monospace reserved strictly for identifiers/code, never prose | Stripe docs | Borrow | D-21, D-24 |
| Flat, named top-level navigation categories (not necessarily their full nested depth) | Stripe docs | Adapt | D-16 |
| Chart/graphic chosen by the message the reader needs, not by decoration; one enforced style system | FT visual vocabulary | Borrow | D-33, D-19 |
| Document-filing identity: revision numbers, hover-to-reveal sourcing, dividers not cards | andrewtgibson.com | Borrow (brand-native) | D-2, D-6 |
| Dark-mode-first theming, minimal purposeful motion, one repeated chip component | Linear | Adapt | D-23, D-15, D-25 |
| Continuous green→red gradient mapped to a single physical quantity | Electricity Maps | Reject for status | D-25 (fixed token pair per discrete state, no implied order) |
| Decorative organic/blob illustration on marketing chrome | Felt | Reject | D-7 |
| Single-hue-carries-the-brand marketing hero with cross-fade animation | Linear (marketing site) | Reject | D-7, D-15 |
| Step-triggered scroll-reveal narrative motion | The Pudding | Reject on working screens | D-15 |
| Deep, many-level nested reference navigation copied literally | Stripe docs (full tree) | Reject | D-16 |
| "Click and wander, learn by touching" as the only onboarding | Flightradar24 | Reject | D-14 |
| A legend whose meaning lives off the map (FAQ, footnote) instead of on it | Global Energy Monitor | Reject | D-10 |
| An analyst-facing raw configuration panel (pick field, pick ramp, pick aggregation) on a reader surface | Kepler.gl | Reject for public/Pro map | D-1, D-4 |

## 4. Typeface decision — keep Newsreader + IBM Plex

**Decision: keep** the brand's existing pairing. Display: Newsreader. UI and body: IBM Plex Sans. Identifiers,
numbers and code: IBM Plex Mono. No replacement family is proposed.

**Licence and variable-font check** (all retrieved 2026-09-12):
- Newsreader — SIL Open Font License 1.1, https://openfontlicense.org/; Google Fonts specimen
  https://fonts.google.com/specimen/Newsreader; ships as a genuine variable font with **two** axes — optical
  size 6–72 and weight 200–800 — confirmed via `fontsource.org/fonts/newsreader` and Production Type's own page
  (`productiontype.com/font/newsreader`). The optical-size axis means one file can serve both small dense table
  captions and large display headings without a second family.
- IBM Plex Sans — SIL Open Font License 1.1, https://github.com/IBM/plex; ships a variable font with a weight
  axis 100–700, confirmed via `fontsource.org/fonts/ibm-plex-sans`.
- IBM Plex Mono — SIL Open Font License 1.1, https://github.com/IBM/plex; ships a variable font with a weight
  axis 100–700 (release `@ibm/plex-mono-variable@1.0.0`, "IBM Plex Mono in variable font format containing a
  weight axis," with predefined instances Thin through Bold), confirmed via `github.com/IBM/plex/releases`.

**Reason.** The study above repeatedly validates the same formula this pairing already is: a distinct
display/serif voice plus a workhorse sans plus a monospace reserved for identifiers is exactly what Stripe's docs
(§2.10), the FT's own style tooling (§2.7) and andrewtgibson.com's own filing idiom (§1) all converge on for
credible, data-heavy work. Newsreader's transitional-serif display register echoes the "filed proposal" identity
the owner's own site already uses; it is not a generic template sans. All three families are self-hostable variable
files under one open licence with no third-party font CDN needed (satisfies D-21's self-hosting rule) and, being
variable, keep the font payload small against the D-31 JS/asset budget relative to shipping multiple static
weights per family. `docs/31-design-system.md` records exact file names and versions when fonts are vendored.

## 5. Modular type and spacing scale

**Type:** base 16px, ratio **1.2** (a conservative "minor third" ratio, deliberately tighter than the 1.25–1.333
ratios editorial/marketing sites use for dramatic headline jumps — D-1's density-over-decoration argues for a
tight ratio in a table-and-chip-dense product where most text is body and label size, not headline). Steps:
12.8 / 16 / 19.2 / 23 / 27.6 / 33.2 / 39.8 px. Display steps (Newsreader, using its own optical-size axis) start at
27.6px and up, so headings shift optical cut automatically rather than just scaling. Line height: 1.5 body, 1.25
display. `font-variant-numeric: tabular-nums` in every table and chip (so a column of numbers aligns, matching the
tabular-numeral pattern observed in Vercel's dashboards, §2.15).

**Space:** 4px base, steps 4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48 / 64 (`--space-1` … `--space-12`). This is the
same scale already recorded in `docs/04-standards.md` D-22; this document is its cited derivation and rationale,
not a competing proposal — any future change to D-22's numbers must update this section too.

**Grid and layout:** a 12-column fluid grid with gutters on the space scale; breakpoints 400 / 720 / 1080 / 1440px
(D-32). Prose content (detail-page body, docs) caps at ~720px measure for readability; tables and the map run
full-bleed inside the page's side gutters (≥16px, D-32). Table rows are 40–48px tall (D-1) with sticky headers
(D-24). No card-grid layout for tabular data at any breakpoint (D-1, D-7).

## 6. Colour system — derived from navy and copper, contrast computed

Method: WCAG 2.x relative luminance on sRGB (linearise each channel, `L = 0.2126R + 0.7152G + 0.0722B`; contrast
`= (L_lighter + 0.05) / (L_darker + 0.05)`). Every ratio below was computed by this formula against the exact hex
values, not estimated; the brand-pair ratios independently confirm the values already recorded in D-20.

### 6.1 Core brand tokens (unchanged from D-20, recomputed and confirmed)

| Token | Hex | Role | Contrast |
|---|---|---|---|
| `--color-ink` | `#16324f` | text, primary surfaces | 13.1:1 on white; 11.9:1 on paper |
| `--color-paper` | `#f6f4ef` | page background | — |
| `--color-copper` | `#a8571c` | accent, action, links ≥16px | 5.2:1 white; 4.7:1 paper; **2.5:1 on ink — fails, banned pairing** |
| `--color-copper-deep` | `#8a4614` | small text/links on light | 7.1:1 on white |
| `--color-copper-tint` | `#e0b58a` | accent/links on ink/dark | 7.0:1 on ink |
| `--color-muted` | `#5b6b7c` | secondary text on light | 5.5:1 white; 5.0:1 paper |

### 6.2 Proposed status-colour families (new — for D-25's chip vocabulary, needs owner/PM ratification, see REF-2)

Rather than one unique hue per lifecycle value (which would produce eight-plus barely-distinguishable colours and
violate D-5's "never colour alone" safety net by overloading it), this study proposes **five colour families**,
each covering several states that share one broad meaning; icon and label (already required by D-25) carry the
fine distinction within a family.

| Family | Covers (proposal) | Covers (opportunity) | Light-mode text hex | Contrast (white / paper) | Dark-mode tint hex | Contrast on ink |
|---|---|---|---|---|---|---|
| Neutral | announced, unknown | closed | `#5b6b7c` (existing `--color-muted`) | 5.5:1 / 5.0:1 | `#c9d2da` | 8.6:1 |
| Progress | filed, studied, permitted, under_construction | reinstated | `#2f6480` (`--color-progress`) | 6.5:1 / 5.9:1 | `#8fc4dd` (`--color-progress-tint`) | 6.9:1 |
| Committed | contracted | awarded | `#8a4614` (existing `--color-copper-deep`) | 7.1:1 / — | `#e0b58a` (existing `--color-copper-tint`) | 7.0:1 |
| Success | built | open | `#2f6f4f` (`--color-success`) | 6.0:1 / 5.4:1 | `#8fd6ac` (`--color-success-tint`) | 7.7:1 |
| Danger | withdrawn, cancelled | frozen, cancelled | `#8c3a2e` (`--color-danger`) | 7.6:1 / 6.9:1 | `#e0a08f` (`--color-danger-tint`) | 6.0:1 |

Chip fill backgrounds (light theme, very pale tint of the family hue, with `--color-ink` text on top — all
computed): neutral fill `#e4e9ed` → ink text 10.7:1; progress fill `#d6e6ee` → 10.2:1; committed fill `#f1ddc9` →
9.9:1; success fill `#d7ead9` → 10.4:1; danger fill `#f1dbd6` → 9.9:1. Every pair clears WCAG AA (4.5:1 normal
text) with margin; the "Committed" family deliberately reuses the brand's own copper tokens so the one moment the
product is named for — a proposal turning into a signed contract, an opportunity turning into an award — is the
only place the brand accent appears as a status colour, never as decoration elsewhere. Copper never appears as
chip text on navy in either theme (D-20's existing rule); copper-tint does.

## 7. Map interaction model

**Zoom and clustering.** Below the configured marker-count threshold (initially ≤500 visible records, D-10) every
proposal and opportunity with a point renders as an individual, focusable marker; above it, the server-side
`/v1/proposals/geo?bbox&zoom` endpoint (`docs/23` §7) returns clustered features carrying `{count,
lifecycle_state_counts, technology_counts, capacity_mw_sum}`. A cluster glyph encodes count by size, the dominant
lifecycle state by chip colour and label (§6.2's families), and dominant technology by a small, shared icon set —
following Flightradar24's principle of consolidating many real-world subtypes into a handful of glyphs (§2.12),
never one bespoke icon per fine-grained technology value.

**Hover.** Hovering (or focusing via keyboard) a cluster or marker shows a lightweight tooltip with the count
breakdown by state and technology — the legend rendered where the data is, never in a separate FAQ or footnote
(the failure this study documents in Global Energy Monitor's tracker, §2.14). No click is required to see what a
cluster contains.

**Select.** Clicking a marker, cluster-list item, or opportunity polygon opens the detail drawer (D-12): canonical
fields, lifecycle/status chip from §6.2, a Sources list (name, retrieved date, licence badge, link-out; gated
sources omitted per `docs/21` §8), the last three timeline events, the D-3 delayed-tier line, and a link to the
full record. The drawer is `role="dialog"` with a focus trap; `Escape` or the close control returns focus to the
marker that opened it (D-12, D-14). This is the same pattern Electricity Maps uses for its zone-click breakdown
panel (§2.1) and Felt uses for its map-adjacent stat panels (§2.4) — select opens context in place, it never
navigates away from the map.

**Fallback ladder for unplaced or imprecise records (D-8).** `location.precision = exact` → a point marker.
`county_centroid` → a point at the county centroid, visibly marked as a centroid (not implied to be exact) — the
same fallback interconnection.fyi ships as its county view (§2.3), evidence this is a normal, expected pattern for
this data category, not a compromise to apologise for. State-only records are listed under their state in the side
panel and drawn as a state-level aggregate marker, never as an invented point. Records with no geography at all
are listed under "Unplaced" with a visible count — never silently dropped.

**Restricted-precision rendering (D-9).** A record whose geometry provenance comes from a source with
`allows_raw_publication = false` or `reuse_class ∈ {restricted, unknown}` never renders an exact point on any
non-admin surface, regardless of what precision the source actually recorded: it renders at the county centroid,
and the drawer states the reason in plain language — "location shown at county level (source licence)" — following
OpenInfraMap's own model of stating the licence constraint in the product's own words, on the page, in full
(§2.13), not as a generic disclaimer.

**Opportunities as service territories (D-11).** An opportunity whose issuer has a polygon renders as a hatched
fill with an outline in the Committed/opportunity family colour and a centroid label; overlapping territories
stack by `due_at` ascending; an opportunity with no polygon falls back through the same D-8 ladder as a proposal.

**Basemap.** MapLibre GL JS (or equivalent) rendering vector tiles, following the architecture Kepler.gl (§2.5),
Electricity Maps (§2.1) and OpenInfraMap (§2.13) all converge on independently; basemap tiles are self-hosted or
served by a provider whose terms permit commercial use, with OpenStreetMap ODbL attribution rendered on the map
itself (https://www.openstreetmap.org/copyright) — the same explicit, on-page statement OpenInfraMap makes for
the same licence (§2.13), not a buried footer link.

## 8. Motion rules

Extending D-15 with the specifics this study surfaces:

- Map `flyTo` on selection or filter change: ≤600ms ease-out. Cluster split/merge on zoom: ≤300ms.
- Drawer open: 200ms; close: 150ms. Focus moves into the drawer on open and back to the trigger on close.
- Feed and timeline inserts never move content mid-read: new items queue behind an "N new" control the user
  clicks to reveal; there is no auto-scroll, ever (this is the direct rejection of scrollytelling's
  reveal-as-you-go model from The Pudding, §2.8 — a register read daily is not a story scrolled once).
- No ambient or looping animation anywhere (rejects the cross-fade marketing hero pattern seen on Linear's
  marketing site, §2.9, and any auto-playing element per D-7).
- No parallax and no scroll-triggered narrative reveal on any working screen (§2.8's rejection, restated as a
  rule: motion here is functional feedback for a state change the user caused, never a storytelling device).
- `prefers-reduced-motion` makes every transition above instant (opacity/position snap, no easing) — no exception.
- Chart and count updates (e.g., a live-tier value ticking) fade/cross-fade over ≤150ms; they never animate a
  number counting up from zero, which reads as decoration, not data.

## 9. Banned-patterns checklist

A reviewer applies this list to any screen, canvas or template before it ships. Each item cites the rule it
enforces; an item marked "(this study)" is an addition this document derived from the products above, to be
folded into D-7 by the standards owners if agreed.

1. Generic sans-serif type on a purple or blue gradient — D-7.
2. Uniform rounded cards in a three-tile hero — D-7.
3. Emoji as bullets or icons anywhere in product or docs — D-7, `CLAUDE.md` tone.
4. Stock illustration or photography, including decorative organic/blob shapes (§2.4) — D-7 (this study).
5. Glassmorphism, frosted panels, decorative blur/glow/shadow stacks — D-7.
6. A marketing hero sitting above a working screen — D-7.
7. Auto-playing or looping animation, including a cross-fading marketing hero (§2.9) — D-7, D-15 (this study).
8. Scroll-triggered narrative reveal ("scrollytelling") on any working screen (§2.8) — D-15 (this study).
9. A continuous colour gradient (e.g., green→red) applied to a discrete, unordered categorical value like
   lifecycle state (§2.1) — D-25 (this study).
10. A legend, colour key, or "what does this mean" answer that lives off the screen it applies to — FAQ, footer
    link, separate help page (§2.14) — D-10 (this study).
11. An analyst-facing raw configuration panel (field picker, colour-ramp picker, aggregation picker) on a public
    or Pro reader surface (§2.5) — D-1, D-4 (this study).
12. Colour as the only carrier of a state, tier or licence class, with no label or icon — D-5.
13. A card grid used for tabular data at any breakpoint — D-1.
14. Copper text or a copper background element on navy — D-20.
15. Any layout, canvas or template a reviewer would independently call "template" — D-7 (this is the residual,
    catch-all clause; citing another numbered item is preferred where one applies).

## 10. Assumptions

| ID | Assumption | Basis | Affected |
|---|---|---|---|
| REF-1 | This task's instruction to study "at least three" independently found products supersedes the design mandate's "at least five" as the more specific, current directive; four were studied here (Flightradar24, OpenInfraMap, Global Energy Monitor, Vercel) | Task instruction vs. `.claude/agents/product-designer.md` mandate text | `docs/31` — extend to five (e.g. Bloomberg NEF, Kinsta) before final sign-off if the owner wants the mandate's number met literally |
| REF-2 | §6.2's five-family status-colour system (grouping several lifecycle/opportunity values under one hue, distinguished by icon/label within the family) reinterprets D-25's "fixed token pair per state" as "fixed token pair per family, fixed icon per state" | No existing docs/31 exists yet to settle this; proposed here for owner/PM ratification | `docs/31-design-system.md` D-25 implementation; needs sign-off before it is treated as canon |
| REF-3 | Several required references (Grid Status live app, Electricity Maps app, Flightradar24 map, Windy — attempted and dropped from the final product list after two unusable fetches, OpenInfraMap's legend specifically, FT's article pages, Observable's live app, The Pudding's story pages) returned HTTP 403/429/404 or an empty JS shell; findings for those are drawn from documentation, GitHub or search-indexed secondary sources, cited inline per entry, not the live rendered UI | WebFetch/WebSearch results, 2026-09-12, this session | Re-verify against the live product before `docs/31` if pixel-level fidelity (exact spacing, exact hex values in their UI) is ever needed; no fidelity claim beyond what's cited is made here |

## 11. Change history

- 2026-09-12 v1 — first draft (product-designer), ahead of any screen work per D-6.
