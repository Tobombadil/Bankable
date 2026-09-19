# Design system

**Status:** Sprint 1 deliverable, v1 · 2026-09-12 · owner: product-designer · reviewed by: owner (pending)
**Authority:** `docs/04-standards.md` §2.4–2.7 (D-19…D-35); `docs/30-design-references.md` §4 (typeface), §5
(scales), §6 (colour), §7 (map model), §8 (motion), §9 (banned patterns) — this doc derives its tokens and
components from that study and does not re-derive it. IA and page inventory: `docs/30-design-ia.md`.
**Inputs:** `docs/10-prd-mvp.md` §4; `docs/23-api-spec-outline.md` §3–§4, §10; `docs/21-data-model.md` §8.
**Product name used in this doc:** Infraque.

## 1. Tokens

All colour, type, space, radius, motion values are CSS custom properties; components never hold raw hex/px
(D-19). Contrast computed by the WCAG 2.x relative-luminance formula against the exact hex (`docs/30` §6), not
estimated.

### 1.1 Colour — core (unchanged from D-20, confirmed in `docs/30` §6.1)

| Token | Hex | Role | Contrast |
|---|---|---|---|
| `--color-ink` | `#16324f` | text, primary surfaces | 13.1:1 on white; 11.9:1 on paper |
| `--color-paper` | `#f6f4ef` | page background | — |
| `--color-copper` | `#a8571c` | accent, primary action, links ≥16px | 5.2:1 white; 4.7:1 paper; **2.5:1 on ink — banned pairing (D-20)** |
| `--color-copper-deep` | `#8a4614` | small text/links on light | 7.1:1 on white |
| `--color-copper-tint` | `#e0b58a` | accent/links on ink/dark | 7.0:1 on ink |
| `--color-muted` | `#5b6b7c` | secondary text on light | 5.5:1 white; 5.0:1 paper (never lighter than `#6b7a8a`, 4.4:1) |

Copper never appears as text on navy in either theme (D-20); copper-tint does.

### 1.2 Colour — status families (adopted pending owner ratification, `docs/30` §6.2 REF-2)

Five families, not one hue per lifecycle value — D-5's "never colour alone" plus icon/label carries the fine
state within a family (D-25 as reinterpreted by `docs/30` REF-2).

| Family | Proposal states | Opportunity states | Text hex | Contrast (white/paper) | Dark tint hex | Contrast on ink |
|---|---|---|---|---|---|---|
| Neutral | announced, unknown | closed | `#5b6b7c` | 5.5:1 / 5.0:1 | `#c9d2da` | 8.6:1 |
| Progress | filed, studied, permitted, under_construction | reinstated | `#2f6480` | 6.5:1 / 5.9:1 | `#8fc4dd` | 6.9:1 |
| Committed | contracted | awarded | `#8a4614` | 7.1:1 / — | `#e0b58a` | 7.0:1 |
| Success | built | open | `#2f6f4f` | 6.0:1 / 5.4:1 | `#8fd6ac` | 7.7:1 |
| Danger | withdrawn, cancelled | frozen, cancelled | `#8c3a2e` | 7.6:1 / 6.9:1 | `#e0a08f` | 6.0:1 |

Chip fills (light theme, pale tint + `--color-ink` text, all ≥9.9:1): neutral `#e4e9ed` (10.7:1); progress
`#d6e6ee` (10.2:1); committed `#f1ddc9` (9.9:1); success `#d7ead9` (10.4:1); danger `#f1dbd6` (9.9:1). Committed
deliberately reuses the brand copper tokens — the moment a proposal becomes a contract, or an opportunity an
award, is the only place the brand accent appears as status colour. **Status: adopted pending owner/PM
ratification** (`docs/30` REF-2); until ratified, treat as the working default, not final canon.

### 1.3 Typography

Display: Newsreader (variable, optical size 6–72 + weight 200–800, SIL OFL 1.1). UI/body: IBM Plex Sans
(variable, weight 100–700, SIL OFL 1.1). Identifiers/numbers/code: IBM Plex Mono (variable, weight 100–700, SIL
OFL 1.1). All self-hosted, no third-party font CDN (D-21). Kept over a replacement per `docs/30` §4: the pairing
already matches the formula Stripe docs, FT tooling and the brand's own site converge on, and all three ship as
self-hostable variable files under one licence.

| Step | Size | Line height | Family | Use |
|---|---|---|---|---|
| `--text-1` | 12.8px | 1.5 | Plex Sans | captions, table meta, licence badges |
| `--text-2` | 16px (base) | 1.5 | Plex Sans | body, table cells, form labels |
| `--text-3` | 19.2px | 1.5 | Plex Sans | list item titles, filter bar labels |
| `--text-4` | 23px | 1.25 | Newsreader (optical ≥27.6 starts here in display contexts) | card/section headings |
| `--text-5` | 27.6px | 1.25 | Newsreader | page H2, drawer titles |
| `--text-6` | 33.2px | 1.25 | Newsreader | page H1 |
| `--text-7` | 39.8px | 1.25 | Newsreader | rare — landing-style single statement, never a marketing hero (D-7) |

Ratio 1.2 ("minor third"), base 16px (D-22) — deliberately tighter than editorial ratios (1.25–1.333) because
most text in this product is body/label/chip, not headline (D-1). `font-variant-numeric: tabular-nums` on every
table and chip. Monospace (Plex Mono) reserved strictly for identifiers, queue IDs, docket numbers, API examples
and code — never prose (D-21, D-24; Stripe docs precedent, `docs/30` §2.10).

### 1.4 Spacing

4px base, steps `--space-1`…`--space-12` = 4/8/12/16/20/24/32/40/48/64px (D-22). Any off-scale value carries a
code comment naming the exception.

### 1.5 Radius, shadow, motion tokens

| Token | Value | Use |
|---|---|---|
| `--radius-1` | 2px | chips, badges |
| `--radius-2` | 4px | inputs, buttons, table cells (focus outline) |
| `--radius-3` | 8px | drawer panel, modal |
| `--shadow-1` | `0 1px 2px rgba(22,50,79,0.12)` | drawer/modal only — no decorative shadow stacks (D-7, banned #5) |
| `--motion-fast` | 150ms | drawer close, chart/count cross-fade |
| `--motion-base` | 200ms | drawer open |
| `--motion-slow` | 300ms | cluster split/merge |
| `--motion-map` | 600ms ease-out | `flyTo` on selection/filter change |

No card-grid radius token exists for tabular data — D-1/D-7 ban card grids for lists at any breakpoint; radius
tokens above 8px are not defined, which is deliberate (no pill buttons, no rounded hero tiles).

### 1.6 Colour and icons — existing-asset layers (added 2026-09-19, midstream slice)

The map's "Existing assets" control draws power plants (per-technology palette, `--plant-*`, unchanged) and,
per the owner's 2026-09-19 option (a), gas pipelines as a **line layer** and gas processing plants, gas storage,
LNG terminals, ethanol plants and RNG projects as **point layers** (ethanol and RNG live since the second
midstream slice, 2026-09-19 evening). One hue per type, dimmer than the
status families (§1.2) so an existing asset never reads as a proposal state; the type is always carried by
**shape + label**, never hue alone (D-5). Contrast by the §1.1 method (relative luminance against the exact hex).

| Type | Token | Light | On paper | Dark | On dark bg | Icon (SDF, `map.js`) |
|---|---|---|---|---|---|---|
| Gas pipeline | `--asset-gas-pipeline` | `#6e4b7a` | 6.48:1 | `#c9a6d6` | 7.95:1 | line — solid = interstate or unclassified, dashed `[3,2]` = intrastate |
| Gas processing plant | `--asset-gas-processing` | `#8a4a5a` | 5.96:1 | `#d9a3b0` | 7.87:1 | diamond |
| Gas storage | `--asset-gas-storage` | `#4f5a8a` | 6.04:1 | `#a9b3dc` | 8.15:1 | ring |
| LNG terminal | `--asset-lng-terminal` | `#2f6a7a` | 5.52:1 | `#8ec6d3` | 8.96:1 | triangle |
| Ethanol plant | `--asset-ethanol` | `#7a6a2a` | 4.87:1 | `#cfc07a` | 9.20:1 | hexagon |
| RNG project | `--asset-rng` | `#2f7a5a` | 4.71:1 | `#8fd0b0` | 9.48:1 | pentagon |
| Power plant | `--plant-*` (§ above) | — | — | — | — | square, family hue |

**Pipeline line rule.** Width by zoom, linear: z3 0.8px, z6 1.4px, z9 2.4px, z12 4px; a casing in `--map-land`
2–3px wider beneath it keeps the line legible over region fills and basemap roads; a 16px invisible hit layer
gives a hairline the ≥24px pointer target SC 2.5.8 asks for. Interstate vs intrastate differ by dash and by the
word in the tooltip, drawer and legend — same hue. Names label along the line from z7. Hover shows name, type,
class and operator; click opens the §5.8 drawer with the asset's fields (rows present only where the source
carries the value). Point types share the plant square's opacity (0.7) and label rule (z9+).

**Legend.** One group per type, shown only while its checkbox is on (`data-legend-type`), so the legend names
exactly what is drawn. The pipeline group carries the two line samples (solid, dashed) with their words. The
ethanol and RNG groups carry a one-line muted note (`.legend__note`) saying which rows are *not* points: EIA
capacity-table plants placed at state grade and AgSTAR digesters placed at county grade never render as points
(`/v1/assets/geo` omits them; there is no region feature for assets) and live on the asset, company and search
lists instead.

**Ethanol and RNG rows (tooltip, drawer, page).** Hue by type, hexagon (ethanol) and pentagon (RNG); the RNG
technology family (landfill gas to electricity, landfill gas direct use, renewable natural gas, farm digester)
is carried by words in the tooltip, drawer subtitle, in-view row and page badge, never by a sub-hue. Promoted
rows, each present only where the source carries the value: ethanol — nameplate capacity (MMgal/yr), feedstock,
PADD, capacity as-of year, operator; RNG — project type, technology, rated capacity (MW), LFG flow to project
(MMscf/d), biogas generation estimate (cu ft/day, digesters), biogas end use, host landfill or digester type,
feedstock (AgSTAR herd counts when no text), start and shutdown year. The drawer renders from the geo feature
first and again with the asset's detail row merged (`/api/assets/{public_id}`), since geo point features carry
no capacity value, unit or attributes. Units: `MMgal/yr`, `MMscf/d`, `cu ft/day`, numbers with thousands
separators and at most one decimal (three for MMscf/d).

**Mini-map (asset and company pages).** Server-rendered SVG of the record's geometry (`.mini-map__line`,
`--intrastate` dashed, `.mini-map__point`, `.mini-map__proposal` in the Progress family hue) as the no-JS
rendering; `asset_map.js` replaces it with a MapLibre map at a fixed fit on the shared basemap when it runs.

## 2. Typography scale rationale (carried forward, not restated)

Steps and ratio are `docs/30` §5's derivation of D-22; any future change to D-22's numbers updates that section
first, then this table. Newsreader's own optical-size axis means `--text-4` and up shift optical cut
automatically rather than needing a second display family (`docs/30` §4).

## 3. Grid and layout

12-column fluid grid, gutters on the space scale, breakpoints **400 / 720 / 1080 / 1440px** (D-32). Side gutters
≥16px at every width (D-32). Prose content (detail-page body, docs) caps at ~720px measure; tables and the map
run full-bleed inside the page gutters. Table rows 40–48px tall (D-1) with sticky headers (D-24). No card grid
for tabular data at any breakpoint (D-1, D-7, banned #13).

| Breakpoint | Layout behaviour |
|---|---|
| ≥1440px | 12-col grid, map + side list side-by-side (§5.1 wireframe), table at full column set |
| 1080–1439px | 12-col grid, map/list panel ratio narrows, table unaffected |
| 720–1079px | Side list becomes a collapsible panel over the map; table scrolls horizontally inside its own container only |
| 400–719px | Map keeps the results list as a bottom sheet (D-32); table collapses to key/value row cards; filter bar becomes a single "Filters" button opening a sheet |

No fixed width wider than the viewport at any breakpoint; only tables, the map canvas, and code blocks may
scroll horizontally, each inside its own `overflow-x` container — the page body never scrolls horizontally
(D-32).

## 4. Data-display rules

- **Numbers** — `tabular-nums` everywhere; right-aligned in table columns with the unit in the column header
  (`Capacity (MW)`), never repeated per cell (D-24).
- **Dates** — absolute date shown always (`11 Sep 2026`), relative shown alongside where it aids scanning
  (`11 Sep 2026 (12 days ago)`); timestamps in API/admin contexts are RFC 3339 UTC in Plex Mono (`docs/23` §1).
- **Capacity units** — `capacity_mw` and `storage_mwh` render with their unit suffix in the header only, values
  as tabular numerals, one decimal max on public/Pro (source precision kept in the tooltip/drawer if it differs).
- **Identifiers** — queue IDs, FERC dockets, EIA plant/generator IDs, public IDs (`prop_…`, `opp_…`) always in
  Plex Mono, never in Plex Sans (D-21, D-24).
- **Currency** — `budget_amount`/`budget_currency` render as `$35,000,000` with ISO 4217 code where not USD,
  tabular numerals, no decimals unless the source states cents.
- **Counts** — `source_count`, cluster counts and aggregates are computed over **visible sources only**; a
  gated source is never counted (D-34, `docs/21` §8 item 4).

## 5. Component inventory

Anatomy, states, and the rule each component satisfies. Every component pulls colour/type/space only from §1
tokens (D-19).

### 5.1 Status chip

**Anatomy:** icon (shared per family, `docs/30` §7) + label (exact vocabulary, `docs/21` §7.1/§7.2, plus
`under_construction` and `unknown`) + family colour pair (§1.2). One component, parameterised by state — never a
bespoke chip per screen (D-25).
**States:** default; `unknown` — visibly neutral, `status_raw` revealed on hover/focus only where the source
licence allows raw (`docs/21` §8); focus-visible outline (`--radius-2`, 2px `--color-ink` outline, SC 2.4.11);
disabled/dismissed (Pro match dismissal, US-402 AC2) — reduced opacity, label unchanged.
**Rule satisfied:** D-5 (label + icon, never colour alone), D-25.

### 5.2 Provenance panel ("Sources")

**Anatomy:** list of source rows, each: source name, `retrieved_at` (absolute date), licence badge (reuse class
label), link-out icon+URL. Gated-source rows are **omitted entirely**, never greyed (`docs/21` §8 item 3 — the
existence of the row is itself a disclosure).
**States:** populated; single-source (no plural chrome); zero-visible-sources (only occurs on a mixed-provenance
record whose only visible evidence was just gated — shows "Sources withheld under licence" rather than an empty
list, distinguishing from a genuinely sourceless record, which cannot exist per DA-2).
**Rule satisfied:** D-2, D-27, `docs/21` §8.

### 5.3 Attribution line

**Anatomy:** single text line, versioned microcopy (D-35): `"Sources: {source names joined}."` for `open`, with
credit text substituted per `attribution_text` for `attribution`-class sources (`docs/23` §10
`licence_summary.attribution_line`). Renders in the footer of every list, detail, map and feed view (D-27,
US-105 AC1) — not optional, not a footnote link.
**States:** single source; multiple sources (semicolon-joined); no rendering condition where this is blank — a
payload with zero sources cannot exist per DA-2.
**Rule satisfied:** D-27, D-34, API-5.

### 5.4 Delayed notice

**Anatomy:** one component, one wording (D-28, versioned string, D-35): `"Public data is {lag_days} days
delayed (as of {data_as_of}). Live in Pro."` + link. Fixed position: page header on lists/map, record header on
detail, top of digest email, RSS `<title>`, CSV header block.
**States:** public tier (shown, non-dismissible); Pro/API tier (not rendered — `lag_days=0`); record-specific
variant on detail pages reads "Updated N days ago on the live tier" (US-201 AC4) using the same token pair.
**Rule satisfied:** D-3, D-28, US-604 AC1.

### 5.5 Table with sticky header and density toggle

**Anatomy:** header row (sticky, `--color-paper` background, `--color-ink` text), body rows 40–48px, numeric
columns right-aligned with unit in header (§4), row-level provenance affordance (small licence-badge icon per
row, D-2), density toggle (comfortable 48px / compact 40px, persisted per-viewer only — not in the URL, since
it's a preference not a filter).
**States:** loading (skeleton rows at the target row height — never a spinner overlay, D-29); empty (names the
filter that emptied it + Clear all, D-29); error (RFC 9457 title + `request_id` banner above an empty table
area); populated; at-cap (bulk/export row-cap reached — banner states the cap and how to narrow).
**Rule satisfied:** D-1, D-24, D-29.

### 5.6 Timeline

**Anatomy:** vertical list, newest first, each item: type icon, `observed_at` (absolute), source name + licence
badge, `before → after` chips for status changes (using §1.2 chip component). Merge/unmerge items are visually
distinct (dashed connector) and link to the absorbed/absorbing record.
**States:** populated; delayed-tier truncation — items newer than the lag are replaced by the D-3 banner in
their place, not hidden with no explanation (D-3, D-26); empty (a `created` event always exists, so this state
is theoretical — documented for completeness per D-29).
**Rule satisfied:** D-26, US-202.

### 5.7 Map marker and cluster

**Anatomy — marker:** shape by `location.precision` (solid dot = exact; outlined triangle = county centroid,
always labelled "centroid" in its accessible name and tooltip; square = state aggregate), fill = dominant §1.2
family, small technology icon overlay (shared set, never one glyph per fine-grained value, `docs/30` §2.12).
**Anatomy — cluster:** circle sized by `count`, fill = dominant lifecycle family, label = count, technology icon
overlay for the dominant technology; hover/focus reveals a breakdown tooltip (`lifecycle_state_counts`,
`technology_counts`, `capacity_mw_sum`) without a click (D-10).
**States:** default; hover/focus (tooltip); selected (opens drawer, §5.8); restricted-precision (centroid shape
+ "county level (source licence)" label, D-9); reduced-motion (no `flyTo` easing, instant reposition, D-15).
**Rule satisfied:** D-5, D-9, D-10, D-14.

### 5.8 Detail drawer

**Anatomy:** `role="dialog"`, focus trap, close control (`×` + `Escape`), canonical fields, status chip (§5.1),
Sources panel (§5.2, condensed to visible rows), last three timeline items (§5.6), D-3 line (§5.4) on public
tier, "Open full record" link.
**States:** loading (skeleton of the same layout); error (RFC 9457 banner inside the drawer, close still works);
open/closed (200ms open, 150ms close, D-15); reduced-motion (instant, no easing).
**Rule satisfied:** D-12, D-14, D-15.

### 5.9 Filter bar

**Anatomy:** one row of named filter controls (multi-select chips for `technology`/`lifecycle_state`, range
inputs for capacity/dates, single-select for `jurisdiction`/`iso`), all bound to URL query parameters verbatim
(D-17), identical vocabulary and allowlist to `docs/23` §7. A "Clear all" control is always present once any
filter is active.
**States:** default (no filters — full vocabulary shown collapsed); active (chips shown with counts where cheap
to compute); at-400px (collapses to a single "Filters" button opening a full-height sheet, D-32); invalid
combination (e.g., `capacity_mw[gte] > capacity_mw[lte]`) shown as inline validation text, not a silent no-op.
**Rule satisfied:** D-4, D-17, API-3.

### 5.10 Saved-search card

**Anatomy:** name, `entity` type, delivery mode + channel icons, `last_run_at`, `last_match_count`, actions
(edit, pause/resume, delete). One card component reused in the `/alerts` list and in any "save this view"
confirmation toast.
**States:** active; paused (muted chip, actions read "resume" not "pause"); at-quota (the "+ New" affordance
disabled elsewhere, this card unaffected); zero-matches (shows "0 new matches", not blank).
**Rule satisfied:** US-501, US-504, D-1 (density — a card here is a *list item*, not a decorative tile; it
appears in a single-column list, never a three-tile grid, so it does not trip the banned three-tile-hero pattern).

### 5.11 Alert row

**Anatomy:** one row per delivered alert in `/alerts/{id}` history: `window_start`–`window_end`, event count,
`sent_at`, delivery `status` chip (queued/sent/bounced/failed/suppressed, reusing §1.2's Neutral/Success/Danger
families), provider message id (Plex Mono, admin/debug view only).
**States:** sent; bounced/failed (Danger family, error text visible, not just the chip); suppressed (Neutral,
"unsubscribed" note).
**Rule satisfied:** US-502 AC4, D-25 (status vocabulary reuses the same five families, not a sixth ad hoc set).

### 5.12 Source health card

**Anatomy:** source name, status icon (✓/⚠/✗ mapped to Success/Progress/Danger families, never colour alone —
icon shape differs per state), `last_success_at`, rows changed, events emitted, `$/record`, `publish_state`
badge (`ingest_only`/`api_only`/`public`/**GATED**), action row (run now, pause, resume, edit cadence, edit lag).
**States:** healthy; degraded (⚠, DA-6 warning threshold crossed); failing (✗, 3 consecutive failures, US-904
AC3); gated (publish_state badge reads "GATED", distinguishable from "public" — never a blank cell, D-29).
**Rule satisfied:** US-904, D-25, D-29.

### 5.13 Post review card

**Anatomy:** channel icon, rendered `body` (within channel limit, counted live), detail-page `link_url` with
UTM preview, `credit_line`, `disclosure_label` where required, action row (approve, edit-then-approve, reject +
reason field, schedule). Never editable: the credit line and disclosure text (D-35 — the review queue cannot
edit attribution/disclosure strings).
**States:** draft; edited (shows a "modified from template" note); approved; scheduled (`scheduled_for` shown);
rejected (reason visible, required at reject time, US-802 AC3); published (metrics panel: impressions, clicks,
likes, reposts, `fetched_at`).
**Rule satisfied:** US-801, US-802, US-803, D-35.

## 6. Empty, loading and error states — cross-component rule

Per D-29, specified once here and referenced, not restated per component: **empty** states name the filter
facet that removed the last result and offer "Clear all"; **loading** states are a skeleton of the final layout
at its final row/card height (never a spinner over a table, to hold CLS ≤0.1 per D-31); **error** states show
the RFC 9457 `title` and `request_id`, never a stack trace or raw exception text; a **gated** record is
indistinguishable from **not-found** on every non-admin surface (`docs/23` §8, `docs/21` §8 item 3) — admin
surfaces are the only place "GATED" is shown as distinct from "does not exist".

## 7. Accessibility — WCAG 2.2 AA mapping

| Rule here | WCAG 2.2 criterion | Where enforced |
|---|---|---|
| Contrast ratios computed per token (§1.1–1.2) | SC 1.4.3 (contrast minimum), SC 1.4.11 (non-text contrast) | Every chip, chart mark and UI border |
| Colour never the only carrier of state/tier/licence | SC 1.4.1 (use of colour) | Status chip (§5.1), source health card (§5.12), alert row (§5.11) |
| Visible focus outline, `--radius-2` + 2px ink outline | SC 2.4.11 (focus appearance) | All interactive components |
| No keyboard traps; drawer focus trap releases on close | SC 2.1.2 (no keyboard trap) | Detail drawer (§5.8) |
| Map pan/zoom has a button alternative to drag | SC 2.5.7 (dragging movements) | Map controls (`docs/30` §7) |
| Target size ≥24×24 CSS px | SC 2.5.8 (target size minimum) | Chips, buttons, map controls |
| Form errors described in text, not colour alone | SC 3.3.1 (error identification) | Filter bar (§5.9), intake forms |
| Accessible names on every marker/cluster/control | SC 4.1.2 (name, role, value) | Map marker/cluster (§5.7): `{name}, {lifecycle_state}, {capacity} MW, {county}, {state}` |
| No content that moves without user control | SC 2.2.2 (pause, stop, hide); SC 2.3.3 (animation from interactions) | Feed/timeline inserts (§5.6), motion tokens (§8) |
| `aria-live="polite"` announces map viewport changes | SC 4.1.3 (status messages) | Map "in view" region (`docs/30` §7, D-14) |

DoD per screen (`docs/04` D-30): contrast pass, target size, visible focus, no keyboard trap, drag alternative,
form errors in text, accessible names, `axe` zero serious/critical, manual keyboard pass.

## 8. Motion tokens and rules

Extends §1.5's tokens with the rules from `docs/30` §8 (all D-15-derived):

- Map `flyTo` on selection/filter change: `--motion-map` (≤600ms) ease-out. Cluster split/merge: `--motion-slow`
  (≤300ms).
- Drawer open `--motion-base` (200ms); close `--motion-fast` (150ms); focus moves in on open, back to the
  trigger on close.
- Feed/timeline inserts never move content mid-read: new items queue behind an "N new" control, no auto-scroll.
- No ambient or looping animation anywhere (banned #7); no parallax, no scroll-triggered reveal on any working
  screen (banned #8).
- `prefers-reduced-motion` makes every transition instant (snap, no easing) — no exception.
- Chart/count updates cross-fade over `--motion-fast` (≤150ms); a number never animates counting up from zero.

## 9. Banned patterns — enforcement note

The full list and rationale is `docs/30` §9; this system does not restate it. Every component in §5 above was
checked against it at design time: no rounded-card grid stands in for a table (§5.5 is a table, not cards); no
gradient scale is used for lifecycle/status (§1.2 is five discrete families, not a continuum); no emoji anywhere
in a chip, icon or label; no glassmorphism/blur on the drawer (§5.8 uses a flat `--shadow-1` only); no analyst
configuration panel appears on public/Pro map or list surfaces (`docs/30` §2.5 rejection carried forward — filter
bar §5.9 offers named, bounded facets only, never a field/ramp/aggregation picker).

## 10. Standards checklist — `docs/04` §2 rule to implementation

| Rule | Satisfied by |
|---|---|
| D-1 Density over decoration | §3 grid (40–48px rows, no card grid), §5.5 table |
| D-2 Evidence visible in ≤1 click | §5.2 provenance panel on row/chip/marker/field |
| D-3 Delayed means visibly delayed | §5.4 delayed notice, fixed position |
| D-4 One mental model across search/map/feed | §5.9 filter bar bound to shared URL grammar |
| D-5 Never colour alone | §5.1, §5.7, §5.11, §5.12 — icon/label with every colour use |
| D-6 Reference study precedes screens | `docs/30-design-references.md`, cited throughout |
| D-7 Banned patterns | §9 |
| D-8 Placement precedence | §5.7 marker anatomy (shape by precision) |
| D-9 Restricted-precision rule | §5.7 states, §1.2 (no exact point rendered) |
| D-10 Clustering by state and technology | §5.7 cluster anatomy |
| D-11 Opportunities as service territories | §5.7 note (polygon variant), `docs/30` §7 |
| D-12 Detail drawer with provenance | §5.8 |
| D-13 Map performance budget | Token/asset budget (§1.5 no heavy shadow/blur; MapLibre per `docs/30` §7) |
| D-14 Keyboard/screen-reader map access | §7 WCAG mapping row 8 |
| D-15 Motion rules | §8 |
| D-16 Three surfaces, one URL scheme | `docs/30-design-ia.md` §1 (this doc does not restate IA) |
| D-17 Filter/sort/viewport in URL | §5.9 |
| D-18 Detail pages server-rendered for SEO | `docs/30-design-ia.md` page inventory (implementation note, not a token) |
| D-19 Tokens are the only reference | §1 |
| D-20 Brand palette, verified contrast | §1.1 |
| D-21 Typefaces and licensing | §1.3 |
| D-22 Modular scales | §1.3, §1.4, §2 |
| D-23 Dark and light both first-class | §1 tokens define both roles (dark values to be enumerated in the CSS file at build time; every token above has a stated light value and an inversion role) |
| D-24 Tables | §5.5, §4 |
| D-25 Status chips | §5.1, §1.2 |
| D-26 Timelines | §5.6 |
| D-27 Provenance panel | §5.2, §5.3 |
| D-28 Delayed-tier notice wording | §5.4 |
| D-29 Empty/loading/error states | §6 |
| D-30 WCAG 2.2 AA | §7 |
| D-31 Core Web Vitals / JS budget | §1.5 (no shadow/blur cost), component list kept to §5's 13 named components rather than bespoke per-screen elements |
| D-32 Responsive to 400px | §3 |
| D-33 Plain, evidence-first copy | §5.3, §5.4, §5.13 wording is literal and versioned (D-35), no adjectives |
| D-34 Attribution on every data element | §4 counts rule, §5.2, §5.3 |
| D-35 Microcopy is versioned strings | §5.3, §5.4, §5.13 note versioned/uneditable strings |
| D-36 When to use the design canvas | Not applicable to this doc (tokens/rules); canvas use is for the five screens named in `docs/30-design-ia.md` §5 |

## 11. Assumptions

| ID | Assumption | Basis | Affected |
|---|---|---|---|
| DS-1 | The five-family status-colour system (§1.2) is used as the working default across every component in §5 pending the owner/PM ratification `docs/30` REF-2 flags | `docs/30` §6.2, REF-2 | §5.1, §5.7, §5.11, §5.12; a ratification change edits token values only, not component anatomy |
| DS-2 | Dark-theme token values are declared as an inversion role per light token (§1) rather than enumerated hex-by-hex in this doc, since no dark-mode screen has been drawn yet | D-23, `docs/30` §2.9 (Linear precedent) | CSS token file at build time; revisit if the owner wants dark hexes fixed before Sprint 1 build |
| DS-3 | The density toggle (§5.5) persists per-viewer only (not in the URL) because it is a display preference, not a filter, distinguishing it from D-17's URL-state rule which governs filters/sort/viewport | D-17 (scope: filter, sort, viewport), §5.5 | Table component implementation |

## 12. Change history

- 2026-09-12 v1 — first draft (product-designer), following `docs/30-design-references.md` v1 and
  `docs/30-design-ia.md` v1.
- 2026-09-19 — §1.6 added (frontend-developer, midstream slice): existing-asset type palette and icons,
  pipeline line rule, per-type legend groups, mini-map classes.
- 2026-09-19 — §1.6 updated (frontend-developer, second midstream slice): ethanol and RNG live (no longer
  "coming"), legend notes for unplaced rows, the ethanol/RNG tooltip, drawer and page row set and units.
