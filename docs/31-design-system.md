# Design system

**Status:** Sprint 1 deliverable, v1 · 2026-09-12 · owner: product-designer · reviewed by: owner (pending)
**Authority:** `docs/04-standards.md` §2.4–2.7 (D-19…D-35); `docs/30-design-references.md` §4 (typeface), §5
(scales), §6 (colour), §7 (map model), §8 (motion), §9 (banned patterns) — this doc derives its tokens and
components from that study and does not re-derive it. IA and page inventory: `docs/30-design-ia.md`.
**Inputs:** `docs/10-prd-mvp.md` §4; `docs/23-api-spec-outline.md` §3–§4, §10; `docs/21-data-model.md` §8.
**Product name used in this doc:** Inframation.

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
| Neutral | announced, unknown | closed | `#4f5e6e` (was `#5b6b7c`, 4.47:1 on the chip fill `#e4e9ed`; changed 2026-09-27) | 6.6:1 / 6.0:1; 5.4:1 on the chip fill | `#c9d2da` | 8.6:1 |
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

**As built (2026-10-07, audit UX-15, UX-17).** `--text-1` is the floor: the 0.7rem (11.2px) mono labels in nine
rules (filter labels, the masthead, the "LIVE" stamp, in-view group names, form legends) are now `--text-1`, the
smallest size anywhere in `styles.css`. Filter-bar labels stay at `--text-1` mono upper case (a toolbar caption);
labels on full forms are `--text-2` as the table says. Plex ships as IBM's static split weights (Sans 400, 500, 600,
700; Mono 400, 500, 600), not the variable files named above, because Plex reserves its name and only IBM's own
files may carry it (`web/static/fonts/README.md`). Newsreader is set only at weight 600 and 23–33px, so its latin
file is an instance at wght 600, opsz 16–40 (50,680 bytes against 132,000), with `font-display: swap` onto the
metric-matched "Newsreader Fallback": with `optional`, a first visit on a throttled link (1.6 Mbit/s, 150 ms, cache
off) painted the H1 and wordmark in the fallback for the whole view on 12 of 12 loads; after, 0 of 12, CLS 0. Plex
keeps `optional`. Admin pages preload the same three files as public pages.

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
exactly what is drawn (placement on the map and the compact state with a layer on: §5.17). The pipeline group carries the two line samples (solid, dashed) with their words. The
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

**Retired and retiring plants (as built 2026-10-07; layer added by lane R1, 2026-10-06).** A separate layer
(`?layers=retired`), never drawn as an operating plant. Points are hollow rings sized by nameplate MW: a centre
dot for retiring (`--asset-retiring`, light `#a3480a`, dark `#f2a66b`) and a cross for retired (`--asset-retired`,
light `#7a2e5c`, dark `#e7a3c9`). **Clusters are dashed rings** with the count inside, in the retired hue on the
land colour; a proposals cluster is a solid ring with its family glyph above the count. Until 2026-10-07 both were
solid rings with a number and differed by hue alone (D-5); the hue gap is also small for colour-blind readers
(simulated deuteranopia, CIELAB ΔE: retired vs Neutral family 7.0 light / 7.7 dark, vs Progress 16.8 / 13.2;
protanopia vs Progress 14.0 / 6.4), so the dash, not the hue, carries the difference. Rings and legend text:
retired 7.62:1 / 8.05:1 (land / page, light), 7.11:1 / 8.40:1 (dark); retiring 5.19:1 / 5.48:1 (light),
7.09:1 / 8.37:1 (dark). Hovering a cluster says "N retired or retiring plants · nameplate MW · click to zoom
in". **Legend** (`#retired-legend`, a labelled group, shown whenever the layer is on): the dashed-ring cluster
mark, the retiring and retired marks, and the EIA-860M note; the proposal key's note now says "A solid ring
with a number is a cluster of proposals". The API's asset clusters carry no status counts, so a cluster cannot
say which status dominates; it reads as "retired and retiring" together.

**Third-party map files (2026-10-07, audit UX-19).** MapLibre, PMTiles and the Protomaps basemaps load from jsDelivr
with Subresource Integrity: each URL and its SHA-384 hash live together in `web/assets.py::CDN_ASSETS`, rendered by
the `cdn_script`/`cdn_style` macros with `crossorigin="anonymous"`; a version change records the new file's hash in
the same edit (`web/test_cdn_integrity.py` recomputes them when the network is there).

**Region placements on the map (2026-10-07, audit UX-5; §5.7 had no region anatomy).** A proposal its source
places only by area is drawn as the area, in `--region-fill`/`--region-line` (never a status hue). A **county** is a
fill stepped by its own count — 1: 0.12, 2–9: 0.2, 10–49: 0.3, 50+: 0.4 opacity — under a 1px solid outline. A
**state or country** is a dashed outline (`[3, 2]`, 1.2px) over a 0.04 wash, because one record placed "somewhere
in Indiana" says nothing about where in it; before, the fill scaled 0.15–0.7 against the busiest region in view and
a lone state-placed proposal painted the whole state slate grey over the lines and plants beneath. The count sits at
the area's representative point. A legend row ("Proposals the source places only by area: a shaded county (darker
holds more) or a dashed state or country outline; the number is how many") shows while any region is drawn; the
in-view list orders regions by count and names a state ("Indiana"), not its code.

**Mini-map (asset and company pages).** Server-rendered SVG of the record's geometry (`.mini-map__line`,
`--intrastate` dashed, `.mini-map__point`, `.mini-map__proposal` in the Progress family hue) as the no-JS
rendering; `asset_map.js` replaces it with a MapLibre map at a fixed fit on the shared basemap when it runs.

## 2. Typography scale rationale (carried forward, not restated)

Steps and ratio are `docs/30` §5's derivation of D-22; any future change to D-22's numbers updates that section
first, then this table. Newsreader's own optical-size axis means `--text-4` and up shift optical cut
automatically rather than needing a second display family (`docs/30` §4).

## 3. Grid and layout

12-column fluid grid, gutters on the space scale, breakpoints **360 / 720 / 1080 / 1440px** (D-32; the floor was
400px until 2026-10-22, `docs/17` M-15). Side gutters
≥16px at every width (D-32). Prose content (detail-page body, docs) caps at ~720px measure; tables and the map
run full-bleed inside the page gutters. Table rows 40–48px tall (D-1) with sticky headers (D-24). No card grid
for tabular data at any breakpoint (D-1, D-7, banned #13).

| Breakpoint | Layout behaviour |
|---|---|
| ≥1440px | 12-col grid; the map at the full content width with one panel, what is in view and the key, floating over its top right (§5.19; changed 2026-10-22, it was the map + side list side-by-side of §5.1's wireframe); table at full column set |
| 1080–1439px | 12-col grid; the panel 340px wide, open by default; table unaffected |
| 720–1079px | The panel 300px wide over the map, collapsed to its header by default; table scrolls horizontally inside its own container only |
| 360–719px | Map: the record drawer is a bottom sheet (§5.8), the controls one column on the map's right edge, the search field a bar above the canvas (§5.18) and the panel a bar under the map that opens in the page (§5.19; changed 2026-10-22: before, the key under the map, §5.17) (D-32); table collapses to key/value row cards; filter bar becomes a single "Filters" button opening a sheet |

**As built at 400–719px (2026-10-07, layout lane; owner to ratify):** the filter bar and the site navigation are
"Filters (N active)" and "Menu" disclosures that open in place rather than as sheets, so nothing is covered and
focus never lands behind an overlay; the in-view results list is paged (25 rows per group, then "Show N more",
focus moving to the first new row) rather than a bottom sheet. Collapse keys on a `.js` class set before first
paint, so a page without scripts stays fully open. Measured: the 400×800 home page went from 13,866 px to
3,377 px tall and the map from 1,658 px to 471 px down.

**As built at 720–1079px on the map page (2026-10-10, lane F):** the map's filter bar is the same "Filters (N
active)" disclosure as below 720px (`styles.css`: the toggle that controls `#map-filters` and
`.filter-bar--map[data-collapse]`, keyed on `.js`); list pages keep their bar open at these widths, and the site
navigation is unchanged. The in-view list sits under the map, not over it as the table above says. Map top, e2e
store with the dev-preview label: 634 → 554 px at 1024×768, 741 → 661 px at 768×1024, 761 → 680 px at 720×900
(`docs/30` §5.1).

**As built on touch screens, and the map's default view (2026-10-21, lane m-behaviour; `docs/17` M-3, M-7):**
- *Gestures.* Where `(pointer: coarse)` matches, the home map and the asset and company mini-maps use MapLibre's
  cooperative gestures: one finger scrolls the page, two fingers pan, a pinch zooms. A one-finger swipe on a map
  shows MapLibre's hint, "Use two fingers to move the map", as a `--surface` label with `--border`, `--radius-2`
  and `--shadow-1`, in place of MapLibre's black wash with white text (its timing is MapLibre's; the global
  reduced-motion rule makes the fade instant). A mouse drags the map as before. The single-pointer ways round are
  unchanged (zoom and region buttons, a tap on a cluster, the in-view list), so SC 2.5.1 and 2.5.7 stand. Measured
  at 390×844, touch, on a copy of the dev store, with a 250 px upward swipe on the home canvas: the page scrolled
  0 px and the map moved 16° before; the page scrolled 235–321 px (two runs) and the map did not move after. On
  the Grand Coulee mini-map the page scrolled 178 px (0 px in `docs/17` §2.3).
- *Default view.* With no `center`/`zoom` (or `region`) in the link, the map fits the contiguous US
  (−124.8, 24.4 to −66.9, 49.4) with 16 px padding, instead of a fixed z 3.2 on −98.5, 39.8. Where the open key
  lies over the canvas (from 720 px), its width is added to the left padding, so the key does not cover the West
  Coast (§5.17). "Reset" fits the same box again, for the canvas as it is then, and so does "Key" while the map
  still shows that view (§5.17, with the cost on canvases under about 1,000 px). While the map shows that view the
  URL carries no `center`/`zoom`. Measured: 390×844 opens at z 1.98 showing −127.7° to −64.0° (before: z 3.2,
  −112.1° to −84.9°, both coasts cut off); 1440×900 opens at z 3.12 with the coast right of the key (before:
  z 3.2, the key over California).

**As built on the map page below 720px (2026-10-22, lane m-layout; `docs/17` M-1, M-4, M-6, M-11, M-15):** the
map's controls are one column on its right edge and the key opens under it (§5.17), the drawer is a bottom sheet
(§5.8), and the words above the map are shorter. From 720px nothing here applies and the page is as it was (at
1440×900 the map top, 336px, the key and the controls measured the same before and after). `styles.css` §8.3.
- *Above the map (M-6).* The lede is its first sentence, "Projects from public grid-queue and permit filings in the
  US and GB, matched across registers." "About this map" (a disclosure button) opens the rest of it, which ends
  with how to use the map; on a touch screen (`pointer: coarse`) that reads "Tap a cluster to zoom in, or a dot to
  open its record." in place of "Click…", at any width. The count and a short form of the lifecycle notice share
  one line ("6009 proposals match these filters. Active only. Why?"); "Why?" opens the notice itself, with what it
  hides, and it stays open across a filter change. At 360×800 that line still fits, but the save link and the view
  switch take a line each, so the status block is 83px against 60px at 390, and the map starts at 431px. "Withdrawn included." is the short form while withdrawn proposals are
  shown, and "Some proposals hidden." when the only notice is the hidden-records line. "Save this search as an
  alert" and the view switch share the next line. Without scripts all of it shows and neither button does.
- *Coverage notes are never behind "Why?"* A view filtered to an ISO whose own queue has no rows here (`?iso=PJM`)
  says so, and that its rows are EIA-860M units (`web/coverage_statement.py` `uncovered_iso_notes`; expert review
  2026-10-07). That is provenance, not a reason something is hidden, so it shows at every width on its own line
  under the count and the short form, and "Why?" opens only the lifecycle and hidden-records lines (D-2, D-34,
  §6). An earlier build of this lane put it behind "Why?", so `/?iso=PJM` at 390px read only "Active only. Why?"
  (review 2026-10-22). The cost falls on those filtered views only: at 390×844 the note is four lines and the map
  starts at 441px against 360px on the unfiltered map.
- *Known gap.* "Why?" and "About this map" are 24px tall (`min-height: 1.5rem`): they meet SC 2.5.8 as inline
  targets in a sentence, but not the 44px used for every other control on the phone page. A 44px box would make
  the count line 44px tall (M-6's one line), and a padded 44px hit area would overlap the control below each: at
  390px the view switch by 6px under "Why?" and the Filters button by 10px under "About this map".
- *The filter bar (M-11).* Its disclosures (Status, Asset types, Location precision) are full-width rows, 44px
  tall, left-aligned, each with a disclosure chevron and a rule above. The chevron is drawn in CSS (two borders in
  `currentColor`, pointing right closed and down open) with both native markers off: Chromium and Firefox draw a
  summary's triangle as a list-item marker and WebKit as `::-webkit-details-marker`, so styling one could show two
  or none in the other (checked in Chromium only; no WebKit build was available). They sat at the right edge
  (`align-self: end`, 243–291px from the left at 390px) and read as stray links. "Show the map" is at the top of
  the open bar as well as its foot (the bar is 535px tall). The proposal list's Status disclosure is unchanged:
  that is the list lane's.
- *Measured* on a copy of the dev store at 390×844 (touch, light): the map's top 493 → 360px (`docs/17`: 512px
  on the live site); intro 222 → 146px; the status lines 117 → 60px. At 360×800 nothing scrolls sideways. The 300px
  target in `docs/17` M-6 is not met: the masthead line (28px) and the header bar (67px) are base.html's, and the
  two-line H1 (58px) and the Filters button row (52px) are what is left above the map. `web/test_e2e.py` holds the
  map top to 368px (`PHONE_MAP_TOP_BUDGET`: 360 measured, 8px for a fallback font).

**As built 2026-10-22 (lane home-map; owner decisions of 2026-10-22, `docs/00-PLAN.md`).** The home map takes
the full content width: the side list (`.map-layout`, `.map-side`) is gone, and what it held returns as the
"In view" tab of one panel floating over the map, beside the key (§5.19). A search field joins the map's
controls (§5.18). The "View as Map / List / RSS feed" row left the map: "Open these in the list" now sits
beside "Save this search as an alert" under the count, and the alert page offers the same search as an RSS
feed ("Or follow it by RSS"); the proposals list keeps its own switch. Below 720px the search field is a
bar above the canvas, so the map starts 44px lower (404px at 390x844 on the dev store, 360px before; the
line under the count is 8px shorter without the switch), and `web/test_e2e.py`'s phone fold budget
(`PHONE_MAP_TOP_BUDGET`) moves with it. Every Status box can be unticked (`lifecycle_state=none`): no
project is drawn, the count, the notice and the panel say "No status chosen: projects are hidden.", a
reload keeps it, and "Clear all" returns to the default (§5.9).

No fixed width wider than the viewport at any breakpoint; only tables, the map canvas, and code blocks may
scroll horizontally, each inside its own `overflow-x` container — the page body never scrolls horizontally
(D-32).

## 4. Data-display rules

- **Numbers** — `tabular-nums` everywhere; right-aligned in table columns with the unit in the column header
  (`Capacity (MW)`), never repeated per cell (D-24). Exception: a column whose rows carry different units (the
  `/assets` Capacity column: MW for plants, MMgal/yr for ethanol) keeps the unit in each cell.
- **Formatters (2026-10-07, audit UX-8).** One Jinja filter each, registered on every template environment
  (`web/formatting.py`): `mw` (thousands separators, one decimal at most, no trailing `.0`: `4,800`, `1,150.5`),
  `thousands` (counts: `1,844`), `display_date` (`11 Sep 2026`; `Sep 2026` for a year-month), `rfc3339` (admin
  timestamps to the second, `2026-10-07T18:23:12Z`, never microseconds). `map.js` mirrors `mw` and `display_date`
  (`fmtMW`, `fmtDate`). ISO dates stay in `datetime` attributes and admin screens only. The proposal page's field
  grid prints through `mw` since 2026-10-10 (lane H).
  **`map.js` capacity, as built 2026-10-10 (lane F).** One formatter, `fmtMWNumber` (`fmtMW` adds " MW"), between
  two marker comments. It rounds as Python's `round(x, 1)` does: the exact binary value to the nearest tenth, and
  the only exact ties (x.25, x.75) to the even tenth. The `toLocaleString` it replaces rounded the shortest decimal
  half up (2.25: "2.3" on the map, "2.2" on the page; −0.04: "-0"). Every capacity `map.js` prints goes through it:
  the proposal, asset and retired-plant rows of the in-view list (the retired rows printed `toFixed(1)`,
  "4800.0 MW"), both cluster tooltips (whole MW in the browser's locale before), the drawer's capacity and
  per-technology rows, and the RNG rated capacity. `web/test_formatting.py` runs the marked block in node
  against `mw` over a fixed table and 1,715 seeded values: 0 differ (the three old paths differed on 196, 1,567
  and 1,452). Without node, it checks that the only " MW" in `map.js` is `fmtMW`'s. Not converted (not MW): the
  drawer's other quantities (MMgal/yr, MMscf/d, cu ft/day; `fmtNumber`) still use the browser's locale.
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

**Fields by kind (as built 2026-10-07).** A proposal page's field grid leaves out a row that cannot apply to
the record's kind, rather than printing "—"; "—" keeps the meaning "the register did not state it". The rows
come from one table, `web/viewmodels.py::PROPOSAL_FIELDS_NOT_APPLICABLE`, read by the record page and the map
drawer alike: `ccs` (Class VI CO2 storage wells) has no Capacity (MW), Storage (MWh), ISO / operator, Connects
at or EIA plant / generator ID row; `load` (data centres, large loads) and `transmission` have no Storage (MWh)
or EIA row. A data centre keeps Capacity (MW), ISO and Connects at, shown as "—" when unstated, because a load
has a real MW figure and an ISO. A row is hidden only when it is empty, so an override or merged value always
shows. Kinds not in the table show every row. Class VI ids are labelled "Permit project ID" (EPA GSDT project id,
Texas RRC tracking number), not "Queue ID". The same rule as the asset pages' "None gate" (`docs/30` §3.1);
`docs/30` §5.2 lists fields but does not require every record to show the same set.

**Chemical formulae.** "CO2", not "CO₂": U+2082 is in none of the self-hosted IBM Plex `unicode-range` blocks,
so a subscript would render in the fallback face. Technology `co2_geologic_sequestration` reads
"CO2 geologic sequestration".

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
**Grouped by source (2026-10-07, audit UX-11).** Rows from one register with the same link, licence terms and
basis line are one row, "EIA-860M · 8 records (69661-G1, 69661-G2, 69661-G3, …) · Open licence · retrieved 7 Oct
2026", with the latest retrieval date (`web/viewmodels.py::group_source_rows`); the per-record detail is in "What this
record combines". The "+" disclosure mark sits on the source name, so it no longer stands alone on a line at 400px.
**Rule satisfied:** D-2, D-27, `docs/21` §8.

### 5.3 Attribution line

**Anatomy:** single text line, versioned microcopy (D-35): `"Sources: {source names joined}."` for `open`, with
credit text substituted per `attribution_text` for `attribution`-class sources (`docs/23` §10
`licence_summary.attribution_line`). Renders in the footer of every list, detail, map and feed view (D-27,
US-105 AC1) — not optional, not a footnote link.
**States:** single source; multiple sources (semicolon-joined); no rendering condition where this is blank — a
payload with zero sources cannot exist per DA-2.
**Lists and search (2026-10-07, audit UX-4).** `/proposals`, `/opportunities` and each `/search` section print one
line under their rows: "Sources of these rows: {short name} ({credit text, verbatim, where the licence sets one});
…. Licences and full credits: Attribution." The registers are the API's `licence_summary.sources` for exactly the
rows on the page, or, where an envelope's summary is empty (`/v1/assets`), the rows' own provenance
(`web/viewmodels.py::page_credits`), so a register behind no printed row is never named and a gated one never is.
NESO's mandated "Supported by National Energy SO Open Data" appears there exactly as written. Company rows carry no
source of their own, so `/organizations` and the search Companies section say where the names come from instead.
Search rows link their source as list rows do.
**Rule satisfied:** D-27, D-34, API-5.

### 5.4 Tier notice

**Anatomy:** one component, one wording (D-28, versioned string, D-35). Since 2026-09-21 nothing is delayed on
any tier, so the rendered string is `"Every record and every change event is published as soon as it is
ingested. Alerts and API in Pro."` + link. The delayed wording — `"Public data is {lag_days} days delayed (as
of {data_as_of}). Live in Pro."` — stays in the component keyed on `lag_days` from the API envelope, so the
page states what the predicate does rather than a constant, and would tell the truth if a delay ever returned.
Fixed position: page header on lists, record header on detail, top of digest email, RSS `<title>`, CSV
header block. The map page has no notice block since 2026-10-10 (below).
**States:** public tier (shown, non-dismissible, live wording); Pro/API tier (not rendered); the
record-specific "Updated N days ago on the live tier" variant (US-201 AC4) is retired with the delay.
**Freshness (2026-10-07, expert review: interconnection finding 9, large-load finding 8).** "No delay" is not "up to
date": the stamp reads **Live** only while every scheduled source is within its fetch allowance, read from
`/v1/coverage` (`vintage.sources[].fetched_at` and `.freshness.status`, the scheduler's allowance per cadence;
unscheduled reference files are not counted). Otherwise the stamp reads **No delay**, the sentence is unchanged, and a
closed disclosure "N sources are behind their fetch schedule" lists each late source with its last fetch ("CAISO
(last fetched 13 Sep 2026)", "Find a Tender (never fetched)") and links the per-source table. When the API cannot
say, the stamp reads "No delay" and nothing is claimed. The masthead says the same in a few words ("8 of 12 sources
behind their fetch schedule", or "All N scheduled sources fetched on schedule") instead of the fixed "Every record
and every change live", and the footer gives the range of last fetches ("sources last fetched between 13 Sep 2026
and 7 Oct 2026") instead of the newest fetch alone, which was EIA-860M's and read as the queues' date. The coverage
read is cached per process for 10 minutes and refreshed in the background (it costs about 1.3 s on the full store).
**Gap for the API lane:** `/v1/sources` returns `last_success_at: null` for every source on file loads, and
`/v1/health` `data_as_of` is the request time; the pages use `/v1/coverage`, which falls back to the newest
`retrieved_at` per source.
**On the map page (as built 2026-10-10, lane H; `docs/30` §5.1).** `/` opens with its statement of value, not a
notice. How current the sources are is said once, by the masthead link ("N of M sources behind their fetch
schedule", to `/methodology#vintage`). The map page keeps that link below 720px (`.masthead--keep`, the edition
stamp `.masthead__ref` hidden), where every other page drops the masthead. List and record pages keep the notice in
their header. The dev-preview label (`preview_banner`) still shows on `/`. The place the notice held now goes to
the filters, one status row and the map with its key drawn on it (§5.17).
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
**States:** populated; ~~delayed-tier truncation~~ retired 2026-09-21 — no item is ever withheld by age, so
every tier sees the whole timeline (D-3, D-26); empty (a `created` event always exists, so this state is
theoretical — documented for completeness per D-29).
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

**As built (2026-10-06, audit UX lane; owner to ratify):** the shape-by-precision anatomy above was never drawn (the map drew circles, with a thicker stroke for licence-limited precision). Markers are now small status chips: the lifecycle family's fill, a family-coloured ring and the family glyph, so status is never colour alone (D-5); licence-limited precision keeps a second, wider ring (D-9) and the drawer states the precision in words (`precision_note`); the technology badge sits beside the marker from zoom 9; clusters show the dominant family's glyph above the count. Marker contrast in dark mode is 6.32–7.88:1 (`web/test_map_a11y.py`). Either this section is rewritten to match, or the shapes are drawn; until the owner decides, the as-built chips stand.

**One tap, one action (2026-10-21, lane m-behaviour; `docs/17` M-2):** one `map.on("click")` handler
(`onMapClick`) acts on the first hit in this order: proposal point › proposal cluster (zooms in) › existing-asset
point or cluster › retired point or cluster › asset line › region (opens its summary, §5.8). Within a tier it takes
the feature nearest the point. A touch asks for features in a box 8 px each side of the tap, because a finger is
wider than a cursor; a mouse asks at the point itself, as before. Before, every layer's own click handler ran: a
tap on a proposal cluster also opened the transmission line beneath it, and a tap on a plant in a shaded county
left the map for that county's list (`docs/17` §2.2). Hover cards and the pointer cursor stay per layer.
In the region tier the most specific level wins (map.js `mostSpecificRegion`): county, then state, then country.
Among regions of that level, one that holds the tapped point beats one that only meets the touch box; after that,
the topmost. A state or country placement's wash covers the whole area and is drawn over any county shaded inside
it, so taking the topmost polygon opened the state. On a copy of the dev store at 1440×900, a click in the shaded
Calaveras County, CA (under it: California's wash, then the county) opened "California" before and opens
"CALAVERAS County, CA" now. The region hover card picks the same way where the pointer enters the region layer; it
is not redrawn as the pointer moves on from a state's wash into a county inside it (as before).

**The breakdown on touch (2026-10-21, lane m-behaviour; `docs/17` M-5).** D-10 asks for the cluster breakdown on
hover or focus, without a click. A touch screen never shows a hover card, so the breakdown also lives in the in-view
header (`#in-view-summary`, under "In view"; since 2026-10-22 the panel's header, `#map-panel-count`, the bar
and its words under it, which count region-placed projects in the total, §5.19). On every device that header says what the drawn markers stand for (a
cluster for its `count`, a point for one): the number in view, one part per lifecycle family (most first, with the
key's glyph and words), the stated capacity (GW from 1,000 MW, through `fmtMWNumber`), then any proposals placed only
by area. On the dev store at the national view: "2,229 proposals in view · 1,595 in process · 561 announced or
unknown · 73 contracted · 277.1 GW · 1,909 more placed by county or region". It is not a live region; the existing
`aria-live` line still announces each move. A region's name, count and breakdown are in its drawer summary (§5.8). The
desktop hover cards are unchanged and use the themed popup rules (`styles.css`, owner report 2026-10-21). D-10's own
text in `docs/04` is not changed here.

### 5.8 Detail drawer

**Anatomy:** `role="dialog"`, focus trap, close control (`×` + `Escape`), canonical fields, status chip (§5.1),
Sources panel (§5.2, condensed to visible rows), last three timeline items (§5.6), D-3 line (§5.4) on public
tier, "Open full record" link.
**States:** loading (skeleton of the same layout); error (RFC 9457 banner inside the drawer, close still works);
open/closed (200ms open, 150ms close, D-15); reduced-motion (instant, no easing).
**Rule satisfied:** D-12, D-14, D-15.

**Region summary (2026-10-21, lane m-behaviour; `docs/17` M-2).** A tap or click anywhere in a shaded county opens
its summary in the drawer. So does one anywhere in a state or country placement: the hit area is the whole
near-transparent wash inside the dashed outline, not the outline alone, so a tap on empty map in a state that is
placed as a region opens that state. A county inside such a state opens the county (§5.7). It used to go straight to
the region's list. The dialog is named "Region summary" (a record's or asset's stays "Record detail"). The summary
holds:
- the region's name ("Tarrant County, TX");
- "N proposals the source places by county, not at a site.";
- its status parts and stated capacity (§5.7);
- a Sources line: each proposal's source, licence and retrieved date are on the list and on its record. A region is
  many records, so the line names none (D-2);
- "Open the list →", to `/proposals?county_fips=` or `?jurisdiction=`.

A country with a quick-view button of its own also offers "Show {region} on the map", which is what a click on it
used to do. The region's row in the in-view list has a Details button that opens the same summary from the keyboard
(D-14). Dialog, focus trap, focus return and motion are as for a record. Record drawers are unchanged and keep their
source line.

**In the panel's place from 720px (2026-10-22, lane home-map).** The drawer no longer slides over the page's
right edge: it opens in the box of the map's panel (§5.19), on the plain `--surface`, as tall as the map less
the insets, with "Back to what's in view" beside Close, and the panel comes back as it was when it closes.
At 720 to 1079px that box is 300px wide and the title steps down to `--text-4`. Below 720px it is the bottom
sheet, unchanged.

**Bottom sheet below 720px (2026-10-22, lane m-layout; `docs/17` M-4).** At 390×844 the drawer was a 351×844 side
panel over 93% of the map, the tapped point under it. Below 720px it is a sheet across the foot of the screen, at
most 45% of its height (`45dvh`), on `--surface` with a top rule and `--radius-3` top corners; no blur (D-7).
- *First state.* A bar holding "Expand" and Close (each at least 44×44; Close stays where focus lands), then the
  record's name, status chip, Capacity, Location, the source line on one line where it fits ("Source EIA-860M … ·
  Open licence · retrieved 9 Oct 2026", the label beside it), and "Open full record →". A proposal's Technology
  row waits for "Expand" (`.drawer-fields__row--more`). An asset or region whose rows run longer scrolls inside the
  sheet under its bar. The provenance stays in the first state (D-2, D-34).
- *Expand.* "Expand" takes the sheet to the full height and becomes "Collapse"; every open starts collapsed. It is a
  disclosure button (`aria-expanded`), and it is drawn only when it shows more: a proposal's Technology row, or rows
  the sheet cuts off. A region summary, or an asset whose rows fit, has none (before, it only made the sheet full
  height).
- *The map.* When a tap on the map opened it and the tapped feature lies under the sheet (or within 24px of its
  edge), the map eases it, over 600ms (`--motion-map`, instant under reduced motion), to the middle of the map left
  showing above the sheet, at the same distance from the left edge. It does not move when the feature already shows
  there, when under 64px of map shows, or when a row of the in-view list opened the drawer (the reader is reading
  the list, and a move would redraw the row focus returns to). A point asset's sheet is drawn again when its detail
  row lands (`/api/assets/{id}`) and can grow then; the map eases the feature above the new height (map.js
  `refit`).
- *Unchanged.* `role="dialog"`, `aria-modal` while open, the focus trap (now over the controls that are drawn, so
  the "Expand" kept in the markup at every width never breaks it from 720px), focus return (a tap on the map while
  the sheet is open shows the next record and keeps the first return target), Escape, and 200ms open / 150ms close
  (`translateY` in place of `translateX`).
- *Measured* on a copy of the dev store at 390×844: proposal sheets 390×309 and 390×340 (37% and 40% of the screen;
  the taller with a two-line source name); with the sheet open, 36% of the map on screen is covered (95.6% in
  `docs/17` §2.1). From 720px the drawer is the 416×900 side panel, as before, and the map does not move.

### 5.9 Filter bar

**Anatomy:** one row of named filter controls (multi-select chips for `technology`/`lifecycle_state`, range
inputs for capacity/dates, single-select for `jurisdiction`/`iso`), all bound to URL query parameters verbatim
(D-17), identical vocabulary and allowlist to `docs/23` §7. A "Clear all" control is always present once any
filter is active.
**States:** default (no filters — full vocabulary shown collapsed); active (chips shown with counts where cheap
to compute); at-400px (collapses to a single "Filters" button opening a full-height sheet, D-32); invalid
combination (e.g., `capacity_mw[gte] > capacity_mw[lte]`) shown as inline validation text, not a silent no-op.
**As built (2026-10-07):** at 400px the bar is an in-place "Filters (N active)" disclosure, not a full-height sheet
(see §3); an impossible range is `aria-invalid` inline validation, server-rendered and live, and the API is not
called. **2026-10-10 (lane F):** the map page's bar is that disclosure up to 1079px; list bars collapse only below
720px (§3). Open, it ends with "Show the map", which closes it and returns focus to "Filters"; Escape does the same.
**Status control (2026-10-07, audit UX-1).** The `lifecycle_state` chips are one "Status: {summary}" disclosure on
both bars (`_macros.html` `status_filter`), with six checkboxes in the legend's words and order: Announced, In
process, Contracted, Built or operating, Withdrawn or cancelled, Status unknown. **Default set:** the first three,
i.e. `announced, filed, studied, permitted, contracted, under_construction` (product defect A); built, withdrawn,
cancelled and unknown are off. The summary reads "active" for the default and "active, built" and so on beyond it;
the 400px "Filters (N active)" count counts a change from the default. The map and the list now share this control;
they still differ in capacity and schedule (list only) and layers and placement (map only).
**Records no register lists any more (2026-10-11, owner decision; `docs/30` §5.1).** At the foot of the Status
disclosure, outside the status fieldset, one checkbox: "Include proposals no register lists any more" (`unlisted`,
value `include`; `{prefix}-unlisted`). It is not a status group, so it has its own name and no `data-default`: the
"Filters (N active)" count counts it when ticked, and the summary adds "no longer listed". Under it a muted note,
"Always included while Built, Withdrawn or Status unknown is ticked.", shown by CSS (`:has`) only while a box outside
the default is ticked, because such a view hides nothing whatever the box says. With the lifecycle notice, the
**hidden-records line** (`unlisted_notice` macro): `view-notice--unlisted`, `role="status"`, "N proposals no register
lists any more are hidden. Show them", the link carrying the view with `unlisted=include`; rendered only while the
view hides at least one. Tokens: none new.
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
badge (`ingest_only`/`api_only`/`public`/**GATED**), action row (run now, pause, resume, edit cadence).
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

### 5.14 Count chips (added 2026-09-19, navigation and discoverability lane)

The by-type totals above `/assets`. Links, never buttons or a segmented control: each chip is a real URL
(`/assets?asset_type=…`) a crawler can follow and a reader can copy, which is the point of an index page
(`docs/50` §4.4).
**Anatomy:** a `<nav aria-label="Assets by type">` wrapping a wrapping flex list; each chip is a bordered pill
holding a label and a tabular-numeral count, largest type first, with an "All types" chip carrying the sum. A
type with no rows behind it is absent rather than shown as zero. One line beneath states the counts'
denominator, because it is not the list's (see `docs/30` §3.2).
**States:** default; hover/focus (border and text take `--link`); **selected** — `aria-current="page"`, which is
what a screen reader announces; the heavier border and weight are the visible echo of it, never the only signal.
**Tokens only:** `--border`, `--link`, `--text`, `--text-muted`, `--radius-3`, `--space-1/2/3`, `--text-1`. No
fill, so the chip reads identically in both themes.
**Rule satisfied:** D-24 (no card grid for data), D-29 (never a fabricated zero), §7 AA (1.4.1 — state is not
carried by colour alone; the chips wrap rather than scroll at 400px).

### 5.15 Share card (added 2026-10-07, as built)

The one Open Graph / Twitter card image every public page links (`og:image`, `twitter:card=summary_large_image`).
It replaces the "no `og:image`" line in `docs/30` §3.2, which predates it.
**Anatomy:** 1200x630 PNG, ink ground so it reads in light and dark feeds; masthead line in Plex Mono Medium,
upper case and tracked, in copper-tint; a hairline rule; the wordmark in Plex Sans SemiBold; the default meta
description's first sentence in Plex Sans Regular; a lifecycle strip (announced, filed, permitted, contracted,
built) using the `base.html` status icons in the §1.2 dark tints, each state named in Plex Mono beside its icon;
a hairline rule and the line "Every record attributed to its source register". No domain, no map tiles, no
imagery or marks of any other organisation.
**Source:** drawn by `web/og_card.py` (`python -m web.og_card`) from the §1.1/§1.2 hex values and the self-hosted
Plex files in `web/static/fonts/`; output `web/static/img/og-card.png`, 39,972 bytes (budget 150 KB). Linked
absolutely under the request's base URL, the same one `rel=canonical` uses, with `og:image:width/height/type/alt`
and `twitter:image(:alt)`. Per-page `og:title`/`og:description` stay the page's own `<title>` and meta
description (`docs/30` §3.2). Plex rather than Newsreader for the wordmark: the card brief named the Plex files.
**Rule satisfied:** D-20 (copper is never drawn on ink; the accent is copper-tint, 6.95:1); every text colour
≥4.5:1 on ink (paper 11.92:1, neutral tint 8.55:1, copper-tint 6.95:1; `web/test_og_card.py` recomputes them);
D-5 (each state carried by word and shape, never hue alone); D-21 (self-hosted fonts only). The URL carries no
version query, so cached cards stay valid; a picture that must replace cached copies gets a new file name.

### 5.16 Button (added 2026-10-07, audit UX-16)

Three treatments, sentence case, `--radius-2`, at least 2.25rem tall (SC 2.5.8), the 2px focus outline of §7:
**primary** — filled `--link`, `--surface` text (form submit, pricing calls to action, "Report a problem"; admin
`.admin-button--primary`); **secondary** — 1px `--link` border, transparent, `--link` text, filling on hover (Search,
Apply filters, Clear all, Details, admin `.admin-button`); **quiet** — text only (the drawer's Close). Disclosure
toggles and map controls (Menu, Filters, Map/List, zoom) keep their neutral bordered look as controls, not actions.
Before: six treatments, including upper-case outlined copper, filled navy with underlined link text in admin, and
filled ink on the report form.

### 5.17 Map key (added 2026-10-10, as built: placement lane H, compact state lane F)

**Superseded 2026-10-22 (lane home-map).** The key is the "Key" tab of the map's panel (§5.19), in plain words;
the floating key (`.map-key__panel`), its "Key" toggle from 720px, the compact state with a layer on and its
layer sections are gone (the panel scrolls). Below 720px the rail's "Key" stays and opens the Key tab under the
map. The fit for the open key (M-7) is now the fit for the panel. The record below is kept as history.

**Anatomy:** `.map-key` (`#map-key`), its own row in the map frame's overlay (`.map-overlay`) under the zoom and
region controls, top left, where the national view has ocean rather than the dense Northeast. One
`.map-key__panel` on the page ground (`--bg`, so each legend hue keeps the contrast measured for it on `--bg`),
`--border`, `--radius-2`, `--shadow-1`. Inside, in order: the proposal status key (`#lifecycle-legend`, a labelled
group; map.js hides a family row with nothing drawn), the region row while regions are drawn, the existing-asset
key (`#plants-legend`, one group per checked type, §1.6) and the retired-plant key (`#retired-legend`, §1.6), each
while its layer is on. "Key" (`#map-key-toggle`, `.map-controls--key`, `aria-expanded`) hides and shows the panel.
Without scripts it is not shown and the key stays open.
**States:**
- *No layer, from 720px:* a 17rem panel, one item per line, full words (`.legend__long`), as tall as its content
  up to `calc(70vh - 2.25rem - var(--space-8))`. At 1440×900 on the e2e store it is 272×276px.
- *No layer, below 720px:* a strip at most 9rem tall with short words (`.legend__short`, "In process"). The full
  words and the notes are visually hidden but still read.
- *A layer on (compact):* map.js `syncKeyCompact` sets `.map-key--compact` while the existing-asset or retired key
  shows, and removes it with the last layer. The status key is the strip at every width. Each layer's key is a
  native disclosure, open on arrival: `details.map-key__section`, `summary.map-key__section-name` ("Existing
  assets", "Retired & retiring plants"; its triangle kept, 1.5rem tall), body `.map-key__section-body`. Groups
  flow in rows. From 720px the panel stops at `max-height: 15rem` and scrolls inside. It is then a tab stop
  (`tabindex="0"`; the panel is a group named "Map key" in every state), so a keyboard can scroll it. A
  `clip-path` inset to the panel and its shadow stops a scrolling panel painting a copy of its background below
  itself. That artifact was seen in headless Chromium, before this change too, and has not been checked on a
  GPU-composited browser.
  At 1440×900 the compact panel is 272×240px, or 196px with both sections closed.
- *Hidden:* "Key" pressed, `aria-expanded="false"`.
No new tokens; the classes above are the additions. Measurements and the axe runs: `docs/30` §5.1.
**Rule satisfied:** D-5 (shape and words with every hue), D-10, D-14; SC 2.1.1 (the scrolling panel is
reachable), SC 2.5.8, SC 4.1.2.

**The key and the default view (2026-10-21, lane m-behaviour; `docs/17` M-7).** The national view fits the lower 48
(§3). Where the open key lies over the canvas, which is from 720 px, map.js `homePadding` adds the key's width to
the fit's left padding. The key therefore sits over ocean, not California, as this section's anatomy intends. A key
that is closed, or not over the canvas, adds nothing. The key itself is unchanged.
- *Opening or closing the key.* While the map still shows the national view, "Key" fits it again for the new state
  (`setMapKeyOpen`, 300 ms, instant under reduced motion), so a reopened key never lies over the West Coast and a
  closed one leaves no empty band. A view the reader has moved stays where it is; "Reset" then fits for the key as it
  is. The URL stays clean throughout. Before this, the fit was worked out only on load and on Reset.
- *The trade-off.* The padding is taken only while at least half the canvas is left beside the key. The key is
  about 300 px wide, so on a canvas under about 1,000 px the lower 48 is drawn small, with open Pacific on the left.
  Measured on a copy of the dev store, key open, then closed: 720×900 (678 px canvas) z 2.15, the lower 48 across
  54% of the canvas, west edge −171.6°, then z 2.97 and 95%; 900×900 z 2.73, 64%, then z 3.33, 96%; 1024×768
  z 3.02, 68%, then z 3.48, 93%; 1100×900 (690 px canvas beside the Filters column) z 2.20, 55%, then z 3.00, 95%;
  1280×800 z 2.76, 64%, then z 3.35, 96%; 1440×900 z 3.12, 70%, then z 3.60, 97%. With the key open the coast is at x 316, right of
  the key's edge at 300, at every one of these sizes. This is deliberate: the alternative is the key over
  Washington, Oregon and northern California on first load. A smaller national view is the cost, and "Key" removes
  it.

**Below 720px: the controls in one column, the key under the map (2026-10-22, lane m-layout; `docs/17` M-1).**
At 390×844 the zoom row, the region row and "Key" were three rows of 36px buttons over the top of the map, and the
open key a 342×144px strip over the canvas with 743px of layer keys scrolling inside it: 38% of the map covered
with the key open, 10–14% with it closed. The states *No layer, below 720px* and the strip's 9rem cap above are
replaced by:
- *The column* (`.map-overlay__rail`). One column of 44×44 buttons, 8px in from the map's top right corner: zoom
  in, zoom out, "Jump to", "Key"; 44×200px in all. "Reset" is not drawn (a page with no region jumps keeps it in
  the column). "Jump to" is a disclosure button (`aria-expanded`) that opens a list beside the column, over the map
  while open: the region jumps the page offers ("United States", "United Kingdom", "Europe") and "Whole map", which
  does what Reset does (the fit to the lower 48, §3). Each choice is a 44px row. A choice, Escape, a tap elsewhere or focus moving on closes it; a choice or
  Escape returns focus to "Jump to". From 720px the column and the "Jump to" wrapper lay out as if absent
  (`display: contents`), "Jump to" and "Whole map" are not drawn, and the zoom row, Reset, the region row and Key
  are as before.
- *The key* opens under the map frame, in the page (after the basemap note when there is one), not over the canvas,
  as the filter bar and the menu open in place (§3). No height cap, no inner scroll and no shadow, so it is never a
  tab stop (map.js `syncKeyFocusable` keys the compact key's tab stop on the cap). "Key" brings it into view when
  it opens below the screen, only as far as needed, smoothly unless reduced motion is set. The key can still grow
  or move down after the press (the status families and the region row arrive with the data, the region row only
  once its outlines load; the count above the map can wrap): at 400×800 that left its foot about 20px below the
  screen. So for 10s after "Key" opens it, each change in the size of the key or of the status block above the map
  brings its foot back into view, until the reader scrolls, touches or types, or closes the key (map.js
  `keepKeyInView`). The status key stays the strip of short words.
- *Layer sections* (`details.map-key__section`) start closed below 720px and open from 720px; as with the key, the
  default follows the width until the reader opens or closes one, and then their choice stands. Closed, the key at
  390px is 358×158px: the status strip, then "Existing assets" and "Retired & retiring plants".
- *Measured* on a copy of the dev store at 390×844 (touch): the map covered on arrival 14.1% → 4.7%; with the map
  at the top of the screen 10.1% → 4.6% with the key closed and 38.1% → 4.6% with it open (`docs/17` asked for under
  6%). At 1440×900 the key and the controls cover 14.3% of the map before and after.

### 5.18 Map search field (added 2026-10-22, lane home-map; owner decision of 2026-10-22)

**Anatomy:** `form.map-search` (`role="search"`, "Find on the map"), first in the map's overlay: a search
icon, the box (`#map-search-input`, `role="combobox"`, placeholder "Find a project, company or place"), a
clear button, and the suggestions (`#map-search-list`, a `listbox` of three named groups of `option`s:
Places, Companies, Projects). From 720px it is in the controls' row on the map, top left, 20rem wide
(14rem at least), as tall as the map controls; below 720px it is a full-width bar above the canvas, 44px
tall, so it covers none of the map, and the right-edge column starts under it. Without scripts it is the
site search (`action="/search"`).
**Behaviour:** suggestions after two characters and a 250ms pause, from `GET /api/map/search`
(`web/map_search.py`): places are the US states and counties the map already shades (the vendored Census
files `GET /v1/geo/regions` serves) and the region quick views on the page; companies are
`GET /v1/organizations?q=` (an organisation that is a natural person is not offered, `docs/13` §5.5);
projects are `GET /v1/proposals?q=` in every lifecycle state, each with its status chip. No third-party
geocoder is called. A newer query aborts the one before it, and only the newest answer is shown. A place is
fitted; a project opens its drawer (provenance included) and the map flies to its site, or fits its county
or state, with a ring round it; a company's projects under the current filters are fitted, or its page
opens when none is drawn. Escape or the clear button empties the box and lifts the ring.
**Rule satisfied:** D-2 (the drawer's source line), D-8, SC 4.1.2 (combobox, listbox and options named).

### 5.19 The map's panel: what is in view, and the key (added 2026-10-22, lane home-map)

The owner, 2026-10-22: "the right panel is kinda nice, just a bit confusing while zoomed all the way out.
could we keep the big map view but add a floating transparent box for what is in view? Further layering in
the key and making UI improvements so its easier to understand?" It replaces the side list beside the map
(§3) and the floating key (§5.17): one overlay instead of two.

**Anatomy:** `section.map-panel` (`#map-panel`), after the canvas in the map frame.
- *Header:* one button (`#map-panel-toggle`, `aria-expanded`) holding "N projects in view · X GW" and a
  chevron, as a disclosure's (up while open, down while collapsed); a thin bar by status family in the families' own colours (`--family-*-text`, the markers'
  rings), 6px tall, 1px of panel between parts; under it the same counts in words with each family's
  glyph ("561 announced or unknown · 2,974 in process · 603 contracted"), so the bar is never colour alone
  (D-5). "Key" (`#map-panel-keybtn`) shows beside the count while the panel is collapsed. Each button
  opens what it names: the count opens "In view" and "Key" the Key tab (a phone's bar reopened on the Key
  tab after the rail's "Key" had closed it). Each tab keeps its scroll across a collapse.
- *Tabs:* "In view" and "Key" (`role="tablist"`, one tab stop, Left and Right switch). The body scrolls
  inside the panel, each tab keeping its own scroll.
- *In view* follows the zoom. At national and multi-state views it is an overview; zoomed in, the list
  (below). *Key* is the key, below.

**Size and place.** From 720px it floats over the map's top right, inset 12px (`--map-panel-inset`):
340px wide from 1080px, 300px from 720 to 1079px; it is as tall as the map less the insets, never taller,
and scrolls inside. Collapsed, it is its header only. Open by default from 1080px, collapsed from 720 to
1079px; the reader's choice is kept in this browser (`localStorage` "inframation.mapPanel", read and written
in `try`/`catch`, so a blocked store falls back to the width). Until the reader chooses, the default
follows the width. The map's controls and the search field wrap short of it. A fitted view (the national
view, Reset, a region or a place) keeps clear of it: the open panel's width on the right, the collapsed
panel's header at the top (map.js `homePadding`, `fitPadding`; the M-7 rule of §3, which kept the key's
width on the left before).

**Surface.** `color-mix(in srgb, var(--surface) 88%, transparent)` over a 6px backdrop blur, `--border`,
`--radius-3`, `--shadow-1`; the plain `--surface` where `prefers-reduced-transparency: reduce`. The blur is
an owner-directed exception to D-7's "frosted panels" (§9). Every word on it keeps 4.5:1 whatever the map
draws behind it: the ground under a word lies between the surface mixed at 88% with black and with white, and
each text colour is measured against both (`web/test_map_a11y.py` from the tokens, `web/test_e2e.py` on the
page, both themes). To hold that, the panel's muted words take `--family-neutral-text` (6.6:1 on white;
worst case 5.04:1 light, 6.72:1 dark) instead of `--text-muted` (4.14:1 at worst), and the key's words are
`--text` beside a glyph or swatch in the mark's hue (the plant hues fall to 3.9:1 at worst, so they colour
the swatch only: an `aria-hidden` square, a non-text mark held to SC 1.4.11's 3:1, with the fuel's name in
words beside it). Lowest measured worst cases: light 4.54:1 (`--family-success-text`), dark 4.70:1
(`--family-danger-text`).

**In view: the overview.** Shown below zoom 5 or with more than 300 projects in view, and whenever the
answer is clustered (more than 500 placed at a site in view, `services/api/geo.py` `SPLIT_THRESHOLD`); a
view of 25 projects or fewer is listed at any zoom, being shorter as a list. Tuned on the dev store: at the
national view 4,138 projects are in view; a state view (Texas at zoom 6.5: 1,129; New York fitted: 478)
stays an overview; a metropolitan view (Austin at zoom 8: 160; zoom 9: 31) lists. It holds:
- *First,* "Zoom in or search to list projects.", so the reason there is no list, and the way to one, read
  without a scroll (at the foot it was under the panel's fold at 1440x900).
- *By status:* the Status filter's own groups, named as its boxes are ("Announced", "In process",
  "Contracted", "Built or operating", "Withdrawn or cancelled", "Status unknown"), each with its count and a
  meter of its share.
- *Technology (top four, by projects):* counts.
- *Busiest areas:* the five states (or countries) holding the most projects in view, busiest first, each
  a button that fits the map to it, with its count and stated capacity, from `GET /api/map/areas`
  (`web/map_search.py` `busiest_areas`): a cluster carries no state, so the route asks the API once more for
  the same view at its finest grid (about 10 km cells) and puts each cell in the state or country whose
  outline holds it; region-placed projects carry their own. Answers are kept two minutes per query. Five rows
  are reserved, so the answer landing moves nothing. Measured on the dev store: 2.2s for the first answer
  after the process starts (it reads the county, state and country files), 0.13 to 0.19s for a new view
  after that, 3ms from the cache.
- How many of them are placed only by county or state, and what else is in view (existing assets, retired
  plants).
GW per status and per technology are not shown, only the total and each area's: a cluster or region
feature of the API's geo answer carries one capacity sum and counts by state and by technology, so the
capacity cannot be split exactly (at the finest grid 612 of the dev store's 2,229 sited projects still share
a cell with another technology; hybrid sites list a record per technology at one point). Splitting it needs
`capacity_mw_by_lifecycle_state` and `capacity_mw_by_technology` on those features (`services/api/geo.py`,
a backend change); the overview can then show both with no request of its own. **API side built 2026-10-23
(lane front-api, docs/30 §11.11 B-4; docs/22 §26.3):** every cluster and region feature carries both, generating
and storage MW only (a data centre's MW is `demand_mw_sum`, apart; a line's is in neither), each part to the
column's 0.001 MW; `capacity_mw_sum` keeps its meaning (every stated MW). A whole view's figures, with the list
each one is the total of, are `GET /v1/stats/capacity`. Showing them in the panel is V2's.
**In view: the list.** The side list's own code, kept: projects first (one row per project, not per
generator unit), then regions, existing assets and retired plants as labelled groups, 25 rows per group then
"Show N more"; each row's status chip (or kind) and Details on its first line, the name under them, the
muted line last. The switch between overview and list costs no request (both are built from the answers in
hand on each move) and moves nothing: the panel keeps its box.

**Key.** In words for a first visitor: "Each dot is one project. Its colour and mark show its status:",
then one row per status family named as the Status filter names its states ("Announced, or Status unknown",
"In process (filed, studied, permitted or under construction)", "Contracted", "Built or operating",
"Withdrawn or cancelled"); a ring with a number ("a group of nearby projects ... Click it to zoom in", "Tap"
on a touch screen); the second, thinner ring of a licence-limited site; the shaded county and dashed outline
of region-placed projects ("Click it for a summary"); then, while their layers are on, existing
infrastructure (power plants by fuel, gas pipelines solid and dashed, the point types, transmission lines
"only the lines FERC Form 1 filers own (LBNL), not the whole grid") and retired and retiring plants. A status
row with nothing drawn is hidden, as before (designer D-2).

**Hover.** Hovering a key row lights the marks it names and dims every other map layer to 15% of its drawn
opacity (map.js `setHighlight`, `highlightMatch`; clusters and regions carry flat `fam_<family>` counts so a
status row lights every cluster holding that status). Hovering a row of the list rings its mark on the map
(a point's ring, a line's or a region's outline). Mouse only; a touch never hovers, and the words say the
same.

**The drawer.** From 720px the record drawer (§5.8) opens in the panel's place, in the same box, with
"Back to what's in view" beside Close; the covered panel is `visibility: hidden`, so closing the drawer (by
that link, Close or Escape) brings it back as it was: the same tab, scrolled to the same place, open or
collapsed. Focus returns to what opened the drawer without scrolling the panel.

**Below 720px.** The panel does not float: it is a bar directly under the map frame, "N projects in view ·
X GW" with "Key", 62px tall at 390px, that opens in the page (no inner scroll), as the key did (docs/17 M-1).
The rail's "Key" opens it on the Key tab; opening it brings its foot into view, and for 10s after a press
keeps it there as it grows, until the reader scrolls, touches or types (map.js `keepPanelInView`, the key's
old rule). A panel taller than the screen (the Key tab with the layers on is about 1,200px at 400px wide,
where the old key closed its layer sections) brings in only the projects' key, or on "In view" its first
rows, with the panel's top on screen and the rest of the screen left to the map (at 400x800 the map's
bottom 320px stay in view). It starts closed and is not remembered. Nothing but the right-edge column lies on the canvas: at
390x844, 5.1% of the visible map covered on arrival and 4.6% with the map at the top of the screen, panel
open or closed (docs/17 asked for under 6%).

**Measured** on a copy of the dev store, no dev-preview label: at 1440x900 the map is 1400x630 with its top
at 336px, and the open panel 340x606 at its top right, 27% of the map (the side list it replaces took 22rem
beside a narrower map); at 1024x768 the collapsed panel is 300x118 (12% of the map with the map at the top
of the screen); at 390x844 the map starts at 404px (360px before: the search bar above it is 44px and 8px
of gap); at 360x800 nothing scrolls sideways.
**Rule satisfied:** D-5 (glyph and words with every hue), D-10 (the breakdown in the header on touch),
D-14 deferred (owner, 2026-10-22: keyboard-only and screen-reader optimisation; the list stays reachable
by the skip link, which opens the panel's "In view" tab), SC 1.4.3 (measured as above), SC 2.5.8.

**Polish, 2026-10-22 (review of lane home-map; supersedes the matching lines above).**
- Hover on key rows and list rows is mouse-only (pointer events with `pointerType === "mouse"`), so a tap on a
  phone never leaves the map dimmed or a dot ringed.
- While the floating panel is open (720px and up), "in view" counts only what lies left of it: the header, the
  overview, the list and the busiest areas use the uncovered rectangle. Marks under the panel are still drawn.
  Region links, area buttons, search and cluster zooms fit clear of the panel.
- The count line reads "N projects match these filters." with "M of them can't be placed on the map (the Projects
  list has them)." under it from 720px; on phones that sentence opens the In view tab instead, so the count row
  stays one line. Every count has thousands separators.
- The header's status words are the Status filter's own names ("Announced", "In process", "Contracted").
- The busiest-areas list reserves five 32px rows, so nothing moves when it arrives.
- The panel's title is an `h2`, so headings keep their order (axe heading-order clean at 1440 and 390).
- GW per status and per technology needs `capacity_mw_by_lifecycle_state` and `capacity_mw_by_technology` on the
  geo features; until then the overview gives counts for those and GW only for the whole view and each area. The
  API serves both since 2026-10-23 (§5.19 above); the overview's use of them is V2's.

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
| Visible focus outline, `--radius-2` + 2px ink outline | SC 2.4.11 (focus appearance) | All interactive components; since 2026-10-07 also the filter inputs and the site search, which had replaced it with a 1px underline change (UX-15) |
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
in a chip, icon or label; no glassmorphism/blur on the drawer (§5.8 uses a flat `--shadow-1` only; one owner-directed
exception since 2026-10-22: the map's panel, §5.19, is translucent over a 6px blur because the owner asked for "a
floating transparent box"; its words are held at 4.5:1 against the worst ground a map could put behind it); no analyst
configuration panel appears on public/Pro map or list surfaces (`docs/30` §2.5 rejection carried forward — filter
bar §5.9 offers named, bounded facets only, never a field/ramp/aggregation picker).

## 10. Standards checklist — `docs/04` §2 rule to implementation

| Rule | Satisfied by |
|---|---|
| D-1 Density over decoration | §3 grid (40–48px rows, no card grid), §5.5 table |
| D-2 Evidence visible in ≤1 click | §5.2 provenance panel on row/chip/marker/field |
| D-3 Delayed means visibly delayed | §5.4 tier notice, fixed position (states live; nothing is delayed) |
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
- 2026-09-19 — §5.14 added (frontend-developer, navigation and discoverability lane): count chips for the
  `/assets` index. The `/assets` and `/organizations` indexes otherwise add no components — they are §5.5
  (table), §5.9 (filter bar) and §6 (empty state) as already specified.
- 2026-10-07 — §5.15 added (frontend-developer, public-site defects lane): the share card image and the
  meta that links it, as built.
- 2026-10-07 — as built (frontend-developer, Class VI and retired-layer follow-ups): §1.6 retired and retiring
  plants (dashed-ring clusters, legend, contrast and colour-blind figures); §4 fields by kind and the "CO2"
  rule. The map heading is built from the proposal source groups (`web/viewmodels.py::map_heading`), and the
  map page's meta, `og:` and `twitter:` descriptions from the same table.
- 2026-10-07 — §1.3 type floor and fonts as built, §1.6 third-party integrity and region anatomy, §4 formatters,
  §5.2 grouped sources, §5.3 lists and search, §5.9 status control and its default set, §5.16 Button, §7 input focus
  (frontend-developer, lane L5, design audit UX-1, UX-4, UX-5, UX-8, UX-11, UX-15, UX-16, UX-17, UX-19); §5.4
  freshness-derived stamp, masthead and footer (same lane, expert review 2026-10-07).
- 2026-10-10 — as built (frontend-developer, lane F): §3 and §5.9 the map page's filter bar is a disclosure up to
  1079px; §4 `map.js` capacity through one formatter matching `mw`; §5.4 the map page's tier notice as lane H
  built it (lateness once, in the masthead); §5.17 the map key added (lane H's placement, the compact state with
  a layer on). No new tokens.
- 2026-10-11 — as built (frontend-developer, lane U): §5.9 the Status disclosure's "Include proposals no register
  lists any more" box and its note, and the hidden-records line under the lifecycle notice. No new tokens.
- 2026-10-22 — as built (frontend-developer, lane home-map): §3 the full-width home map, the search bar on phones and
  untick-all; §5.7 the breakdown in the panel's header; §5.8 the drawer in the panel's place from 720px; §5.17
  superseded by §5.19; §5.18 the map's search field and §5.19 the panel (what is in view and the key) added; §9 the
  panel's blur as the one exception. No new tokens: the panel's custom properties (`--map-panel-inset`,
  `--map-panel-width`, `--map-height`) sit on `.map-frame` and are built from §1's.
