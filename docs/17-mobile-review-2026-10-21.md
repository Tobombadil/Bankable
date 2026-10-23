# Mobile review of the public site, 2026-10-21

**Status:** review, v1 · 2026-10-21 · author: product-designer · for: owner and coordinator
**Asked by:** owner: "could we run an optimization for mobile view of the site. For example, currently the key takes up
most of the map".
**Judged against:** `docs/04-standards.md` D-10, D-12, D-14, D-15, D-30, D-32; `docs/31-design-system.md` §3 (breakpoints),
§5.8 (drawer), §5.17 (map key), §7 (WCAG 2.2 AA); `docs/16-ux-review-2026-10-20.md` (its backlog IDs U-n are reused).
**Scope:** the live public site, https://inframation.io, build `f75dc7d` (footer build line, 21 Oct 2026). Phones only.
This document recommends; it changes no code.

## 0. Summary: the five changes that matter most on a phone

1. **Clear the map (§2.1).** At 390×844, on arrival, the map overlay covers **55.6%** of the visible map. The key
   alone covers **41.4%**: a 342×144 px panel with 743 px of content that scrolls inside itself. At 360×800 the overlay
   covers 57.6%. Closing the key by default (U-12, in flight) brings this to 14.1% (390) and 23.5% (360). The three rows
   of buttons above the key (zoom, three region jumps, Key) still take the top 132 px of a 490 px map. Further changes:
   the controls go into one 44 px column on the right edge; on phones the key opens **below** the map, not over it; and
   the key's layer sections start closed. Target: under 6% covered, open or closed.
2. **One tap does one thing (§2.2).** A tap on a proposal cluster zoomed to level 10 **and** opened an unrelated
   transmission-line drawer ("Antelope Valley Station - Red 345 kV"). This reproduced in both 390 px runs. A tap on a
   power plant in Fort Worth opened nothing and **left the map** for `/proposals?county_fips=48439`, because the
   shaded Tarrant County region lay under the plant. Every layer's click handler fires on the same tap. One dispatcher
   with a priority order fixes both.
3. **Let the page scroll past the map (§2.3).** A one-finger swipe that starts on the map pans it (12.7° of
   longitude and latitude) and scrolls the page **0 px**. The canvas sets `touch-action: none`. The same happens on the
   287 px mini-maps of company and asset pages. A swipe on the key scrolls the key (122 px), again not the page. Once
   the reader has scrolled to the map, it fills 53% of the screen, and every swipe there is lost. MapLibre's
   `cooperativeGestures` on touch devices fixes this (one finger scrolls the page, two pan the map): one option per map.
4. **Open records in a bottom sheet, and give touch a way to reach the hover-only information (§2.4).** The drawer
   is a 351×844 side panel that covers **93.3%** of the map, including the point that was tapped. On touch no hover
   card ever appeared, so a cluster's status breakdown and capacity sum, and a region's name and count, cannot be
   reached.
5. **Shorten the tops of pages and make list rows compact (§3).** The home map starts at 512 px (390×844), and at
   583 px at 360×800. Safari's visible viewport on an iPhone 14 is about 664 px tall, so a reader sees about 150 px of
   map. The first `/proposals` row is at 761 px. List rows are 178–367 px tall, so `/proposals` runs to 14,239 px
   (17 screens) and `/interconnection-points` to 19,320 px (23 screens). U-17 (in flight) shortens list headers for
   1440 px; it needs a phone target as well. U-10 (compact rows, backlog, not started) is the largest single cut.

**What already works:** no page scrolls sideways at 390 or 360 px (`scrollWidth` equals the viewport on all 46 page
measurements). No visible text is under 12 px; the smallest is `--text-1`, 12.8 px. Every form field the reader can
reach is 16 px, so iOS does not zoom on focus. No sticky element eats the screen. DOM text has no contrast failure in
dark mode on any of 23 dark page measurements. The company pipeline tables fit at 358 px.

## 1. Method

- **Browser:** Chromium (Playwright, `/opt/pw-browsers/chromium`), `is_mobile`, `has_touch`, device scale factor 3,
  through the session proxy. Viewports: 390×844 (iPhone 14, iOS Safari user agent) and 360×800 (Android, Chrome user
  agent). Light and dark through `color_scheme`.
- **Runs:**
  - 390 light: 21 page types.
  - 360 dark: 19 page types.
  - 390 dark: the home map, `/proposals`, a proposal and NextEra.
  - 360 light: the home map and `/proposals`.
- **Pages:** `/`; `/proposals`; `/proposals/bute-hydrogen-project-1-w4patj` (the first list row);
  `/proposals/homer-city-generating-station-tad9g2` (it has an "At this site" table); its site
  `/sites/site_1M5B47C9FFPXAMCWBN6RQ1H6V`; `/opportunities`; its first row
  `/opportunities/poland-bullet-proof-vests-…-km4cqx`; `/organizations`; `/organizations?q=NextEra` and `?q=Invenergy`,
  which gave the slugs `nextera-energy-inc` and `invenergy-llc`; both company pages; `/assets`;
  `/assets/grand-coulee-us-wa`; `/interconnection-points`; `/interconnection-points/poi_1M5B46FSVE8VA038NY5BX5AB1`;
  `/search?q=solar`; `/alerts`; `/about`; `/methodology`; `/login`.
- **Load on the server:** one page at a time, 2.5 s apart. In all, 50 browser page loads and 14 plain requests (the
  sitemap and HEAD checks to find record URLs): about 64 documents. Three of the 50 loads were navigations caused by
  the plant tap in §2.2.
- **Measured on each page:**
  - Overflow: document `scrollWidth`, and every visible element whose right edge passes the viewport without a
    clipping ancestor. Internal horizontal scrollers were recorded too.
  - Tables: width against the viewport.
  - Tap targets: every link, button, field, `summary` and tab stop under 24×24, after the SC 2.5.8 inline and spacing
    exceptions (a 24 px circle on the target's centre must not meet another target). Named primary controls were
    checked against 44×44.
  - Form fields: font size.
  - Layout: sticky and fixed elements; the offset of each header block, of the first data row and of the map.
  - Text: any run under 12 px, and the contrast of every visible text run against its composited background, with
    opacity applied. The contrast checker was first run on a known-bad test page, which it flagged.
- **Map:** the share of the visible map that each overlay covers, from a 2 px sampling grid: the zoom buttons, the
  region buttons, the Key button, the key panel, the drawer and the filter bar. Overlays were measured on arrival, with
  the key flipped, with the filters open and with the drawer open.
  - Gestures: touch swipes sent through CDP `Input.dispatchTouchEvent`. On a local test page, before the live runs,
    this method scrolled a plain area by 309 px and a `touch-action: none` area by 0 px. Each swipe recorded `scrollY`
    and the map centre before and after.
  - Taps: on a cluster and on a plant, through the touchscreen API, recording popups, the drawer and the URL.
- **Limits:**
  - Headless Chromium is not iOS Safari. Safari's toolbars leave about 390×664 visible, not 844, so every
    "first screen" figure here flatters a real iPhone. The 664 figure is the visible area in Playwright's iPhone 14
    descriptor.
  - Map canvas labels are pixels, not DOM, so their contrast was judged by eye from the dark screenshots (legible).
  - Scripts are outside the repo:
    `/tmp/claude-0/-home-user/9d34d572-ce78-5368-b168-493b630e66f5/scratchpad/mobile-audit-scripts/`.

## 2. The map

### 2.1 What covers the map

| State (390×844 unless noted) | Map on screen | Covered | Key | Region buttons | Zoom buttons | Key button | Drawer / filters |
|---|---:|---:|---:|---:|---:|---:|---:|
| Arrival (map top 512 px) | 332 px tall, 36.1% of screen | **55.6%** | 41.4% | 8.8% | 3.9% | 1.5% | – |
| Arrival, 360×800 (map top 583 px) | 217 px, 24.7% of screen | **57.6%** | 34.0% | 14.6% | 6.4% | 2.5% | – |
| Scrolled so the map is at the top | 490 px, 53.2% of screen | 37.7% | 28.1% | 6.0% | 2.6% | 1.0% | – |
| Key closed (U-12 behaviour), arrival | 332 px | 14.1% | – | 8.8% | 3.9% | 1.5% | – |
| Key closed, 360×800, arrival | 217 px | 23.5% | – | 14.6% | 6.4% | 2.4% | – |
| Drawer open (point tapped at zoom 9) | 489 px | **95.6%** | 28.1% | 6.0% | 2.6% | 1.0% | 93.3% |
| Filters open | 0 px: the map moves to 1,055 px (360: 1,126 px) | – | – | – | – | – | the bar is 535 px tall |

Why the key is so large on a phone:

- **The key is full width.** Below 720 px, `.map-key { flex: 0 0 100% }`, and `.map-key__panel` has no width, so it
  spans 342 of 358 px. It is capped at `max-height: 9rem` (144 px). `styles.css` lines 1312, 1331–1340.
- **The asset layers now draw on arrival (owner, 2026-10-20), so the key arrives compact and long.** It holds the
  status strip, then "Existing assets" (11 plant families and 7 asset types) and "Retired & retiring plants". Both
  layer sections are `<details open>` (`home_map.html` 178, 218). That is 743 px of content behind a 142 px window
  (762 px at 360), so the reader sees four plant families and must scroll inside the key to see the rest.
- **The controls stack in three rows above the key**: zoom and Reset (36 px), then "United States · United Kingdom ·
  Europe" (36 px), then "Key" (36 px). The key starts 132 px down the map.

The owner's description is accurate. On arrival at either width, more than half of the visible map is covered. Even
after scrolling to the map, the top 284 px of its 490 px carries controls or the key.

**Default view at phone width.** The map opens at a fixed centre and zoom (−98.5, 39.8, z 3.2; `map.js` 853–857,
`WORLD_CENTER`/`WORLD_ZOOM`). On a 358 px canvas that shows North Dakota to the Gulf: California, the Northeast and
PJM are off screen, and they hold most queue rows. Fitting the contiguous US to the canvas would give about z 2.1 at
358 px.

**Copy.** The intro says "Click a cluster to zoom in, or a dot to open its record."; a phone reader taps.

### 2.2 Taps that do two things, or the wrong thing

MapLibre fires a layer's `click` handler for every layer with a feature under the point. `map.js` registers
independent handlers: `region-fill` (1635, navigates), `plant-points` and `asset-lines-hit` (1887–1891, open the asset
drawer), `plant-clusters` (1894, zoom), `retired-points`/`retired-clusters` (1961–1970), `clusters` (2087, zoom) and
`points` (2094, open the drawer).

| Tap | What happened | Runs |
|---|---|---|
| Proposal cluster "9" at 390 px, national view | Zoomed 3.2 → 10 **and** opened the drawer for "Antelope Valley Station - Red 345 kV" (transmission line), which then covered the zoomed map | 390 light, 390 dark |
| Power plant WAL266 (Fort Worth, z 11) | No drawer: navigated to `/proposals?county_fips=48439`. Features under the point: `plant-points:WAL266`, `region-fill:Tarrant County, TX` | 360 light (URL recorded); 360 dark and 390 dark also navigated |

This is not phone-only (a mouse click does the same), but touch makes it worse. A finger lands on wide line hit areas
and shaded counties far more often than a cursor does. Phone readers also cannot see a hover card first to learn what
is under their finger.

### 2.3 Gesture conflict

| Swipe (one finger, upward) | Page scrolled | Map moved |
|---|---:|---:|
| On bare map canvas, home (390 dark; 360 light) | **0 px** | 12.7° |
| On the key panel | 0 px | 0 (the key scrolled 122–132 px inside) |
| On the intro text above the map | 367–475 px | 0 |
| On the NextEra / Invenergy mini-map (358×287) | **0 px** | 36–42° |
| On the Grand Coulee asset mini-map | **0 px** | 0.34° |

The home canvas and container, and the mini-map canvases, compute `touch-action: none` (MapLibre with `dragPan` and
`touchZoomRotate` on; `map.js` 853–861, `asset_map.js` 63–73). The page can only be scrolled from outside the map. At
360×800, once the map is in view, that is the 336 px of the screen it does not fill. `asset_map.js` already turns off
`scrollZoom`, which handles mouse wheels only; touch drag is still on.

### 2.4 Drawer and hover-only information

- **Drawer.** `width: min(26rem, 90vw)`, full height, from the right (`styles.css` 942–948). At 390 it is 351×844 and
  leaves a 39 px strip of map. The tapped point is hidden, so the record has no spatial context. Close is a quiet text
  button, 43×35. Focus moves to Close, and the source line ("EIA-860M … · Open licence · retrieved 19 Oct 2026") is
  present. Provenance holds on phones.
- **Hover cards never appear on touch.** After a tap on a cluster there was no `.maplibregl-popup` at 120 ms or at
  1.9 s, in either theme. So on touch:
  - A cluster's status breakdown and capacity sum (`showClusterTooltip`, 2116) cannot be reached. A tap zooms instead.
    D-10 and docs/31 §5.7 promise the breakdown "without a click".
  - A region's name and count (`showRegionTooltip`, 1659) cannot be reached. A tap navigates (§2.2).
  - Retired-cluster breakdowns (1964) cannot be reached.
  - The asset hover card (name, type, operator) can, because its drawer carries the same fields.
- **Dark mode.** PR #85's themed hover cards are not in build `f75dc7d`, so desktop dark mode still shows white cards
  with near-white text. On touch this does not arise today, because no card appears. If M-5 adds touch summaries, they
  must use the themed rules.

### 2.5 Filter disclosure on the map

"Filters" opens in place (docs/31 §3 as built) as a 535 px block, which pushes the map to 1,055 px. Its three
disclosures ("Status: active", "Asset types", "Location precision") sit right-aligned with no visible marker.
`.filter-more { align-self: end }` (`styles.css` 1279) is meant for the desktop row; in the phone column it pushes them
to the right edge, where they read as stray links. "Show the map" is at the foot of the block and works.

## 3. Pages

### 3.1 Where content starts (390×844; Safari shows about 664 px of this)

| Page | Above the content | First row / map at | Row height | Page height (screens at 844) |
|---|---|---:|---:|---:|
| `/` | masthead 47 (freshness line, 2 lines in mono caps) · wordmark bar 67 · intro 222 (H1 58 + lede 6 lines) · Filters 40 · status 117 | **map 512** (360: 583) | – | 6,061 (7.2) |
| `/proposals` | header 66 · page header 295 · tier stamp 54 · Filters 40 · status notice 101 · count + view switch 71 · save alert 48 | **761** (360: 799) | 252 | 14,239 (16.9) |
| `/opportunities` | header 66 · page header 271 · tier 54 · Filters 40 · count · save alert | 602 | 295 (title wraps 3–4 lines) | 16,795 (19.9) |
| `/assets` | header 66 · page header 207 · count chips 178 (8 chips, 3 rows) · Filters 40 | 668 | 274 | 15,336 (18.2) |
| `/interconnection-points` | header 66 · page header 231 · Filters 40 | 540 | **367** | 19,320 (22.9) |
| `/organizations` | header 66 · page header 207 · search bar 140 | 511 | 178 | 5,669 (6.7) |
| `/search?q=solar` | header 66 · H1 · count | first group 231 | – | 17,538 (20.8) |
| `/about`, `/methodology` | header · page header 207 | 874, 883 | 344, 302 | 16,881 (20.0), 29,653 (35.1) |
| NextEra | H1 98 · field grid 325 · scope links 507 (176 px, stacked) · Pipeline 777 | proposal table 1,609 | 126 | 14,316 (17.0) |
| Invenergy | as NextEra | 1,859 | 114 | 8,966 (10.6) |
| Proposal / site / grid point / asset | H1 98–117 | field grid 310 / 379 / 341 / 190 | – | 2,083 / 5,299 / 5,606 / 2,632 |

At 360×800 every list is 1–2 screens longer: `/interconnection-points` 19,817 px (24.8 screens) and `/methodology`
31,175 px (39 screens).

### 3.2 Other measurements

- **Horizontal overflow:** none at either width.
  - **Inner scrollers:** About's sources table scrolls 629 px inside 358 px; Methodology's tables 479 and 537 px.
  - **Cause:** the rows are cards below 720 px, but unbroken file paths and mono source IDs keep the card wider than
    the screen. "data/vendored/organizations/aliases.yaml" is cut mid-word at the card edge, with no sign that it
    scrolls.
  - **Methodology:** the mono ID squeezes the source name to one word per line ("NESO / Transmission / Entry / …").
- **Tables:** the record tables all collapse to cards (U-10 territory). The three company pipeline tables (3 columns)
  fit, 358 px wide inside the gutters.
- **Tap targets failing SC 2.5.8** (after the inline and spacing exceptions):
  - `/search?q=solar`: 18. Each result's "TED" source link is 24×17, stacked line on line.
  - Invenergy: 9 (11 at 360). Filer links in the proposal meta line are 137×17, directly under the row link.
  - NextEra: 0 at 390, 1 at 360.
  - Every other page: 0.
  - Passing only by the spacing exception: the single-digit count links in the company pipeline tables (10×20 and
    19×20); breadcrumbs (17 px tall); "View at source →" (100×19).
- **Primary controls under 44×44** (390 and 360 alike):

  | Control | Size (px) |
  |---|---|
  | Menu | 74×40 |
  | Filters | 83×40 |
  | Map zoom | 36×36 |
  | Reset | 55×36 |
  | Region buttons | 36 tall |
  | Key | 48×36 |
  | In-view "Details" | 58×24 |
  | View switch (Map · List · RSS feed) | 32 tall |
  | Pager "Next →" | **54×24**, the only way past row 50 on a 17-screen page |
  | `/assets` count chips | 29 tall |
  | Mini-map zoom | 29×29 |
  | Drawer Close | 43×35 |
  | "Follow this company's proposals" | 268×36 |
  | Sign in | 90×42 |
  | Nav links in the open menu | 41 tall |

- **Form fields:** every reachable field is 16 px (site search, filter selects and inputs, sign-in, report form). The
  only field under 16 px is the off-screen honeypot `#report-website` (13.3 px), which readers never reach.
- **Sticky and fixed:** none on any page at either width; the table heads are hidden in card mode. The drawer is the
  only fixed element.
- **Text under 12 px:** none.
- **Headings:** the H1 stays at `--text-6` (33.2 px) on phones. The opportunity H1 runs to 4 lines (166 px) and
  proposal, site and grid-point H1s to 2 lines (83 px).
- **Company scope switch:** "This company only · With direct subsidiaries · Whole group" uses `.filter-bar`, which is
  a column below 720 px, so three short options take 176 px stacked.
- **Open menu:** 422 px tall with 9 links (U-6 cuts this to 6, about 290 px).
- **Dark mode:** no DOM text under its contrast threshold on any page at 360 or 390. The map labels are legible in the
  dark screenshots.
- **Errors seen:** `/favicon.ico` 404 on every load (U-24, known). In the first 390 run, after the cluster tap zoomed
  into North Dakota, at least five `/api/assets/geo` requests answered 503 (the relay's "API did not answer" path). It did not
  recur in later runs. This is not a phone issue; worth a look in the API logs.

## 4. Findings

Severity: **High** blocks or misleads a core job on a phone. **Medium** slows it or hides information. **Low** is
polish. "In flight" means work already scheduled from docs/16.

| # | Page | Viewport | What | Measured | Severity | File to change |
|---|---|---|---|---|---|---|
| F-1 | `/` | 390, 360 | Key and controls cover the map on arrival | 55.6% / 57.6% covered; key 41.4% / 34.0% | High | `styles.css` 1295–1340, 1474–1507; `home_map.html` 134–229; `map.js` 1262–1316 |
| F-2 | `/` | 390, 360 | Key content scrolls inside a 142 px window | 743 / 762 px of content; layer sections open | Medium | `home_map.html` 178, 218; `map.js` `syncKeyCompact` |
| F-3 | `/` | 390 | Cluster tap also opens an unrelated line drawer | zoom 3.2 → 10 + "Antelope Valley Station - Red 345 kV" drawer, 2 of 2 runs | High | `map.js` 1887–1897, 2087–2096 |
| F-4 | `/` | 360 | Plant tap in a shaded county leaves the map | → `/proposals?county_fips=48439`, 3 of 3 runs | High | `map.js` 1635, 1650–1657, 1887 |
| F-5 | `/` | 390, 360 | One-finger swipe on the map cannot scroll the page | page 0 px, map 12.7°; canvas `touch-action: none` | High | `map.js` 853–861 |
| F-6 | company, asset | 390, 360 | Same trap on 287 px mini-maps | page 0 px, map 36–42° | Medium | `asset_map.js` 63–73 |
| F-7 | `/` | 390 | Drawer covers the map and the tapped point | 351×844; 93.3% of map | Medium | `styles.css` 942–972; `map.js` drawer open/close |
| F-8 | `/` | all touch | Cluster breakdown, capacity sum and region name and count are hover-only | no popup at 120 ms or 1.9 s after tap | Medium | `map.js` 1659, 1964, 2116 |
| F-9 | `/` | 390, 360 | Map starts low | top 512 / 583 px; ~150 px visible in Safari | Medium | `home_map.html` 19–22, 116–127; `_map_notice.html`; `base.html` masthead; `styles.css` 1250–1361 |
| F-10 | `/` | 390, 360 | Default view cuts off both coasts | fixed z 3.2 at −98.5, 39.8 on a 358 px canvas | Medium | `map.js` `WORLD_CENTER`/`WORLD_ZOOM`, Reset 866 |
| F-11 | `/` | 390, 360 | Filter disclosures right-aligned in the phone column | 3 summaries at the right edge | Low | `styles.css` 1279–1285 |
| F-12 | lists | 390, 360 | First row below the first screen | 511–761 px (390), 535–799 px (360) | Medium | list templates; `styles.css` |
| F-13 | lists, About, Methodology | 390, 360 | Card rows 178–367 px; pages 17–39 screens | see §3.1 | Medium | `styles.css` 752–763; row partials |
| F-14 | `/search`, company pages | 390, 360 | Stacked 17 px meta links fail SC 2.5.8 | 18 (search), 9–11 (Invenergy) | Medium | `search.html`; company proposal rows; `styles.css` `.in-view-list .meta` |
| F-15 | all | 390, 360 | Primary controls under 44 px; pager 24 px tall | see §3.2 | Low (pager Medium) | `styles.css` 765–767, 823–830, 879–884, 949–955, 1237–1241, 1372–1377, 714–719; MapLibre ctrl buttons |
| F-16 | About, Methodology | 390, 360 | Card contents overflow; paths cut mid-word | 629 / 479 / 537 px inside 358 | Low | `styles.css` 752–763 |
| F-17 | company | 390, 360 | Scope switch stacks into a column | 176 px for three options | Low | `organization_detail.html`; `styles.css` 725–728 |
| F-18 | record pages | 390, 360 | H1 33.2 px wraps to 4 lines | 166 px (opportunity); 83 px (proposal, site, grid point) | Low | `styles.css` 513–514 |
| F-19 | `/` | all touch | Intro says "Click" | copy | Low | `home_map.html` 21 |
| F-20 | all | 360 | D-32 sets 400 px as the floor; Android's common width is 360 | holds today: no overflow at 360 | Low (standards) | `docs/04-standards.md` D-32, `docs/31` §3 |

## 5. Fix list

Size: S under a day, M one to three days, L more. **Cover** says whether in-flight or known work already covers it.
The order is priority for a phone reader.

| ID | Change | Files | Size | Cover |
|---|---|---|---|---|
| **M-1** | **Map overlay on phones.** Below 720 px: (a) the key starts closed (**U-12, in flight**). (b) The controls move into one column on the right edge, each 44×44: zoom in, zoom out, a "Jump to" button opening the three regions plus "Whole map" (this replaces Reset and the region row), and Key. (c) "Key" opens the panel **in flow under the map frame**, not over the canvas, as the filter bar and menu already open in place (docs/31 §3 as built). (d) `details.map-key__section` starts closed below 720 px, so the status strip shows first. Expected: about 5–6% of the map covered whether the key is open or closed (a 44×252 px column on 358×490), against 55.6% today and 14.1% after U-12 alone. | `home_map.html` (overlay markup), `styles.css` (§8 map overlay and key rules, 1295–1340, 1474–1507), `map.js` (key toggle 1262–1316, region buttons), `web/test_e2e.py` (fold budget, key state at 390) | M | (a) U-12; (b)–(d) new |
| **M-2** | **One tap, one action.** Replace the per-layer click handlers with one `map.on("click")` that queries rendered features at the point, within an 8 px box on coarse pointers. It acts on the first hit in this order: proposal point › proposal cluster › asset point › retired point or cluster › asset line › region. A region tap opens the drawer with a summary ("Tarrant County, TX · N proposals the source places by county · Open the list") instead of navigating. Tests: a tap on a cluster that lies over a line only zooms; a tap on a plant in a shaded county opens the plant and leaves the URL path at `/`. | `map.js` 1635–1657, 1887–1897, 1961–1970, 2087–2096; `web/test_e2e.py` | M | new |
| **M-3** | **Scroll past maps.** `cooperativeGestures: true` when `matchMedia("(pointer: coarse)")` matches, on the home map and the mini-maps (MapLibre 5.24 supports it; it shows its own "Use two fingers to move the map" hint, to be styled with tokens). One finger then scrolls the page; two fingers pan; pinch zooms. The single-pointer alternatives (zoom buttons, region jumps, tap a cluster to zoom, the in-view list per D-14) are unchanged, so SC 2.5.1 and 2.5.7 stand as before. Test: a CDP touch swipe on the canvas increases `scrollY`. | `map.js` 853–861, `asset_map.js` 63–73, `styles.css` (hint colours), `web/test_e2e.py` | S | new |
| **M-4** | **Drawer as a bottom sheet below 720 px.** It opens to at most 45% of the viewport with the name, status chip, capacity, location and the one-line source, licence and retrieved date, plus "Open full record". An expand control takes it to full height. The map eases the selected feature into the uncovered upper half. Close is 44×44. It keeps `role="dialog"`, the focus trap, focus return, and 200/150 ms motion that is instant under reduced motion (D-12, D-15). Amend docs/31 §5.8. | `styles.css` 941–972, `map.js` (drawer open, focus trap near 2330), `home_map.html` | M | new |
| **M-5** | **Touch equivalents for hover-only information.** The in-view header states what is in view ("287 proposals in view · 120 in process · 98 announced · 6.2 GW"), which carries the cluster breakdown on any device. Region summaries come through M-2's drawer. Amend docs/31 §5.7 and D-10 so that on touch the breakdown lives in the in-view header, not a tooltip. Any new card or sheet uses the PR #85 themed rules. | `map.js` 1659, 1964, 2116, in-view list renderer; `home_map.html` 235–242 | S–M | new |
| **M-6** | **Home: map into the first screen.** Below 720 px: shorten the masthead freshness line to one line ("Sources on schedule · 21 Oct"); cut the lede to one sentence, with the rest behind "About this map"; "Tap" or "Select" instead of "Click" on coarse pointers; put the count and lifecycle notice on one line, with the hidden-records detail behind "Why?"; put the save link and view switch on one row. Target: map top ≤ 300 px at 390×844 (from 512), so at least 360 px of map shows in Safari's 664 px. | `home_map.html` 19–22, 116–127; `_map_notice.html`; `base.html` masthead; `styles.css` 1250–1261, 1269–1294, 1356–1388; `web/test_e2e.py` fold budget | S–M | new; U-17 covers lists only |
| **M-7** | **Default view fits the contiguous US.** With no view in the URL, `fitBounds` to the lower-48 box with 16 px padding instead of a fixed zoom. That is about z 2.1 at 358 px, with both coasts in view. Reset ("Whole map", M-1) does the same. | `map.js` (`WORLD_CENTER`, `WORLD_ZOOM`, Reset 866–869) | S | new |
| **M-8** | **Compact rows below 720 px.** Two-line rows (status and name; technology · MW · location · company). The measured basis: `/proposals` 252 px per row, `/opportunities` 295, `/interconnection-points` 367, `/assets` 274, `/organizations` 178, About 344, Methodology 302. The in-view list already does this at 63 px per row. Expected: `/proposals` from 14,239 to about 4,500 px. | as U-10: `styles.css` 752–763, row partials | M | **U-10, backlog, not started**: schedule it next |
| **M-9** | **Phone target for list headers.** First row ≤ 400 px at 390×844 (today 511–761): the intro becomes one line plus a disclosure; count, view switch and save alert share one row; the lifecycle notice is one line with "Why?"; `/assets` count chips scroll in one row. | `proposals_list.html`, `opportunities_list.html`, `assets_list.html`, `interconnection_points_list.html`, `organizations_list.html`, `styles.css` | S | **U-17, in flight** (1440 target): add this target to the same change |
| **M-10** | **Tap targets.** Give meta-line links (search source links, company filer links) a 24 px minimum height, or move them to their own line. Below 720 px, make primary controls 44 px tall: pager links, in-view "Details", view switch, count chips, map controls, drawer Close, Menu and Filters, Follow, the nav links (with U-6). Pad the pipeline count links to at least 24×24. Style MapLibre's mini-map controls to 44 px. | `styles.css` (lines in F-15), `search.html`, company proposal row markup, `organization_detail.html` / `_org_pipeline.html` | S | nav links: **U-6, in flight**; the rest new |
| **M-11** | **Filter disclosures in the phone column.** Below 720 px, `.filter-more` and the Status disclosure are full-width, left-aligned rows with their marker. Repeat "Show the map" at the top of the open block, or make it stick to the block's foot. | `styles.css` 725–728, 1279–1285; `_macros.html` `status_filter` | S | new |
| **M-12** | **Card overflow on About and Methodology.** Below 720 px, `.record-table td` gets `min-width: 0; overflow-wrap: anywhere`, and the mono source ID wraps under the name instead of beside it. | `styles.css` 752–763; `about.html`, `methodology.html` if the ID needs its own cell label | S | new |
| **M-13** | **Company scope switch as one row** (a three-part segmented control, about 110 px each) below 720 px. | `organization_detail.html`, `styles.css` | S | new; U-4 (company tabs, backlog) will move it |
| **M-14** | **H1 one step smaller below 720 px** (`--text-5`, 27.6 px) on record pages. | `styles.css` 513–516 | S | coordinate with the in-flight record headers (**U-2, U-14, U-20–U-23**) so it lands once |
| **M-15** | **Standards.** Amend D-32 and docs/31 §3 to "responsive to 360 px" (it holds today). Record M-1, M-4 and M-5 in docs/31 §5.17, §5.8 and §5.7. Add the phone fold budgets (map top ≤ 300 px; first list row ≤ 400 px at 390×844) to `web/test_e2e.py`. | `docs/04-standards.md`, `docs/31-design-system.md`, `web/test_e2e.py` | S | new |

**Suggested order.** M-2 and M-3 go in the next deploy: they are small and fix behaviour that misleads today. M-1
then lands on top of U-12, so the key is changed once. M-6, M-7 and M-11 go together as one small home-page lane. M-9
joins U-17. M-4 and M-5 follow. M-8 (U-10) is the largest win for list pages and should be scheduled next in the
frontend lane. M-14 rides with the record-header work.

**Changed since, 2026-10-22 (lane home-map; owner decisions of 2026-10-22, `docs/00-PLAN.md`; docs/31 §5.18,
§5.19).** The key and the in-view list are one panel: from 720px it floats over the map's top right, and below
720px it is a bar under the map that opens in the page.
- M-1 (c) stands for the panel: the rail's "Key" opens its Key tab under the map frame. (d) is gone: the key's
  layer sections no longer close, so the Key tab is about 1,200px at 400px wide with the layers on, and "Key"
  brings in the projects' key with the panel's top on screen rather than the whole panel. Coverage holds: 5.1% of
  the visible map at 390x844 on arrival and 4.6% with the map at the top, panel open or closed; 7.1% at 360x800
  on arrival.
- M-5: the breakdown lives in the panel's header ("N projects in view · X GW", a bar by status family and its
  words), not `#in-view-summary`.
- M-6: the view switch left the home map, so the save link shares its line with "Open these in the list"; the
  map's search field is a 44px bar above the canvas, so the map now starts at 404px at 390x844 (360px before).
- M-15: the phone fold budget in `web/test_e2e.py` (`PHONE_MAP_TOP_BUDGET`) moves from 368 to 412px with it.
- M-6, 2026-10-23 (owner: "a site link that will do well when it lands in a contact's inbox"): the home page's
  opening figures (`web/headline.py`: projects added this week, planned large loads, open RFPs) sit on one
  28px line under the lede on a phone, scrolling sideways rather than wrapping. The map starts at 432px at
  390x844 on the dev store; `PHONE_MAP_TOP_BUDGET` moves from 412 to 440px. A view someone filtered shows no
  figures, so it keeps the 404px start.

**Not duplicated here:**
- U-6: menu contents.
- U-12: the key's default state.
- U-17: list headers at 1440.
- U-2, U-14, U-20–U-23: record headers.
- U-10: compact rows.
- U-4: company tabs, which cut NextEra's 17 phone screens.
- U-8: search tabs, which cut `/search`'s 21 screens.
- U-24: favicon.

## 6. Standards check

| Rule | Today on phones | After the fix list |
|---|---|---|
| D-32 no sideways page scroll; tables collapse or scroll inside | Met at 390 and 360. Inner scrollers on About and Methodology clip content (F-16). "The map keeps the results list as a bottom sheet" is still unmet (docs/31 §3 records the in-flow list as built). | Met; M-12 removes the clipped scrollers; M-15 sets the floor to 360 px |
| D-30, SC 2.5.8 target size | 27 failing targets at 390 (30 at 360), all on search and company pages | M-10 |
| D-30, SC 2.5.1 and 2.5.7 | Single-pointer alternatives exist: zoom buttons, region jumps, cluster tap, in-view list | Unchanged by M-3 |
| D-10 cluster breakdown without a click | Not reachable on touch (F-8) | M-5 |
| D-12 drawer | Dialog, focus, provenance met; covers 93% of the map | M-4 |
| D-14 accessible map equivalent | In-view list present; 63 px rows when zoomed in | Unchanged |
| D-2, D-34 provenance on every screen | Met: the drawer source line carries licence and retrieved date; list rows show Source; the map attribution sits under the map | M-4's sheet must keep the source line in its first state |
| D-3, D-28 tier line | Lists show the "LIVE" stamp; the home page carries freshness in the masthead | M-6 shortens the masthead line but keeps the link |
| D-7 banned patterns | No glass, gradients or card grids. The record tables' card mode is the D-32 fallback, not a tile grid. | M-1 controls are plain bordered buttons; M-4 sheet uses `--surface` and a rule, no blur |

## 7. Assumptions

- Production renders the repository's templates as of build `f75dc7d`. The live stylesheet differs from the repo only
  by PR #85's popup rules, which are not deployed (checked by diff on 2026-10-21).
- Safari's visible viewport on an iPhone 14 is taken as 390×664 (toolbars shown). The measurements use 390×844, so
  real first screens are smaller than stated.
- The M-1 coverage target is computed from the proposed column size, not a built page.
- The 503s in §3.2 were seen once, so they are not characterised.

## 8. Screenshots

All are under `/tmp/claude-0/-home-user/9d34d572-ce78-5368-b168-493b630e66f5/scratchpad/mobile-audit/`, in one folder
per run: `390-light/`, `390-dark/`, `360-light/`, `360-dark/`. Each page has `{page}-first.png` (first screen, device
scale 3) and `{page}-full.jpg` (full page, CSS scale). The page names are `home`, `proposals`, `proposal`, `homer`,
`site`, `opportunities`, `opportunity`, `organizations`, `nextera`, `invenergy`, `assets`, `asset`, `pois`, `poi`,
`search`, `alerts`, `about`, `methodology`, `login`, and in 390 light also `org-search-nextera` and
`org-search-invenergy`.

Map states:

| State | Files |
|---|---|
| Arrival | `390-light/home-first.png`, `360-dark/home-first.png` |
| Map scrolled to the top of the screen | `{run}/home-map-scrolled.png` |
| Key flipped (closed) | `{run}/home-key-flipped.png` |
| Filters open | `{run}/home-filters-open.png`, `{run}/home-filters-open-full.jpg` |
| Cluster tap | `390-light/home-cluster-tap.png`, `390-light/home-cluster-tap-after.png` (zoomed, line drawer open); same pair in `390-dark/` |
| Drawer | `390-light/home-point-drawer.png` (map point), `390-dark/home-drawer.png` (from the in-view list) |
| Zoomed to z 9–11 over Dallas–Fort Worth | `{run}/home-zoomed.png`; full page with the in-view list: `360-light/`, `360-dark/`, `390-dark/home-zoomed-full.jpg` |
| Plant tap that left the map | `360-light/home-plant-tap-navigated.png`, `360-dark/home-plant-tap.png` (both show the Tarrant County list) |
| Mini-maps after the swipe | `390-light/` and `360-dark/`: `nextera-minimap.png`, `invenergy-minimap.png`, `asset-minimap.png`; `390-dark/nextera-minimap.png` |
| Menu and list filters open | `{run}/nav-open.png`, `{run}/proposals-filters-open.png` |

Crops: `crop-about-sources.png` (paths cut at the card edge), `crop-methodology.png`, `crop-invenergy-pipeline.png`,
`crop-opps.png`, `crop-search.png`.

Raw measurements: `results-390-light.json`, `results-390-dark.json`, `results-360-light.json`, `results-360-dark.json`.

## 9. Change history

- 2026-10-21 · v1 · product-designer · mobile review of the live site at 390×844 and 360×800, light and dark.
- 2026-10-22 · v1.1 · frontend-developer, lane home-map · §5 "Changed since": M-1 (c) and (d), M-5, M-6 and
  M-15 after the map's panel and search field.
