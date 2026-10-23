# Inframation in the Compass family

**Status:** brand system, chosen by the owner on 2026-10-23 ("Go with B. Compliment. If anything compliment and
improve on"), drafted by product-designer (lane brand). Not yet applied to the product: the build lane applies §7
and §8. Spec for that lane; nothing here changes product code.
**Order of authority:** `CLAUDE.md` guardrails, then WCAG 2.2 AA (`docs/04`), then the taste skill
(`.claude/skills/taste-skill`, vendored, MIT), then `docs/31-design-practices.md`, then `docs/31-design-system.md`
where it has not been brought into line (§9 lists every conflict).
**Evidence:** screenshots, mocks and the full token and contrast data are in the lane's scratch folder,
`brand/directions/directions.json` (key `compass_system`).

**The owner's words this applies.** "the site doesn't have a very unique branding and UI. It looks ALOT like
cleanview.co's site"; "their layout is so much better in direct comparison ... We should similarly declutter the
page ... Way less words"; "Inframation.io will be a compass entity, so branding should match", then "Or at least
compliment"; "Treat compass as a reference, not a law"; "lean into the rules of that skill to create a
professional design"; spur.us as an aspirational reference.

## 1. The system in one paragraph

Inframation keeps its name and gains the endorsement "a Compass company". The chrome is drawn in Compass's navy on
a cool blue-grey ledger ground, with no colour of its own. The one hue is an evergreen taken from Compass's forest,
and it goes only on the data: the active projects on the map and the lead figure that counts them. Compass's orange
survives as the nod: the dot of the i in the wordmark is an orange north pointer. Type is Geist, one family, with
Geist Mono for identifiers only. The front page is the full-screen map of `docs/30` §11.6 with three live figures
on top, one floating bar and a collapsed panel. Every figure links to its list, and the sources' read date sits
beside the figures.

## 2. Evidence

### 2.1 Distance audit (public homepages, one screenshot each, 1440x900, 2026-10-23)

| Site | Ground | Accent | Type | Composition |
|---|---|---|---|---|
| Cleanview, https://cleanview.co/ | `#f7f3ed` warm cream | `#c66627` orange (HSL 24°) | Hanken Grotesk | Headline and two sales buttons over a colourful basemap with orange, yellow and green dots |
| GridStatus, https://www.gridstatus.io/ | not measured: a Cloudflare block page, not retried | from its open-source repo header (https://github.com/gridstatus/gridstatus): orange `#fe6100`, cyan `#43c9dd` | grey grotesque logo | not observed |
| Interconnection.fyi, https://www.interconnection.fyi/ | `#ffffff`, hero `#f7fafc` | royal blue `#1d3fa7` (225°), orange CTA | system sans, Noto Sans | Centred count headline, freshness pill, change ticker, filter pills, blue county choropleth |
| Electricity Maps, https://www.electricitymaps.com/ | `#0f0f0f` | violet gradient cards (235°) | Inter, Formular | Centred headline, pill CTAs, card row |
| Modo Energy, https://modoenergy.com/ | `#0e0f0f` video hero | none | Reckless, DM Sans, Fragment Mono | Serif headline over a video, logo wall |
| Compass (parent), https://www.compassitg.com/ | forest photograph, navy `#0b1023` band | orange `#e68023` (29°), royal-blue gradient button | light geometric sans, Avenir-like | Forest hero, orange headline, gradient pill, floating chat pill. 390px capture only; the 1440px load answered HTTP 429 and was not retried |

The coordinator measured the old overlap: our `#f6f4ef` against Cleanview's `#f7f3ed` is 0.98 ΔE2000, invisible.

### 2.2 Distance of the chosen system

| Measure | Chosen system | Old brand | Variant A (dropped) |
|---|---|---|---|
| Ground vs Cleanview, ΔE2000 | 5.5 (`#eef1f5`) | 0.98 | 5.5 |
| Accent hue vs Cleanview orange, HSL / OKLCH | 136° / 116° | 1° | 4.8° / 6.7° |
| Ink vs Compass navy, ΔE2000 | 0 | — | 0 |
| Data hue vs Compass forest, HSL | 2.8° | — | — |

Variant A (Compass's palette as is, orange as the accent) was rendered and dropped. Orange on the main data element
reproduced Cleanview's look, orange dots on a map. Compass orange is 2.81:1 on white, so as text or a mark it has to
darken to about `#b45f0c`, which is Cleanview's and our old copper's colour. Its dark variant also fails the
taste skill's terracotta check.

### 2.3 Compass cues: kept and dropped

**Kept:**
- **Navy `#0b1023`,** as the ink of text, the wordmark and the one primary button. It is Compass's exact navy.
- **Orange `#e68023`,** as the needle over the i, in the mark and the wordmark only.
- **The forest hue.** The data colour sits 2.8° from Compass's forest photograph, and About opens Compass's world with a
  Colorado pine forest.
- **Power and land photography on read pages,** credited.
- **The endorsement,** in the lockup, the footer and About.

**Dropped:**
- **Orange as an accent.** It is 4.8° from Cleanview's orange.
- **The royal-blue links and the gradient pill button.** Gradients are banned. A second hue on the chrome breaks the
  style. And the blue is 8° from Interconnection.fyi's brand blue.
- **A navy or forest ground on app screens.** The skill bans navy dark themes. Navy appears as a field only on read
  pages and the share card.
- **The Avenir-like light face.** It is replaced by the face the skill dealt (§4).
- **The floating "Let's Talk" pill and a photograph hero on the map.** Floating pills are banned. On the front page
  the map is the picture.

## 3. How it was chosen (taste skill)

| Step | Command | Result | Choice and reason |
|---|---|---|---|
| App style, free | `draw.mjs --app --kind analytics --options 3` | accent-ladder (DM Sans), framed-sheet (IBM Plex Sans), achromatic-ledger (Geist) | **achromatic-ledger**. Its move is the positioning: no colour in the chrome, one hue on the main data element, a small signal where a figure changes. The data carries the energy |
| App style, markets kind | `draw.mjs --app --kind markets --options 3` | true-black-flat, data-terminal | Not taken. Dark-first does not suit the inbox-link first impression or readers outside the trade, and 17 of the 23 studied app screens are light |
| App sheet | `--style achromatic-ledger` | seed 1784729574 | Adopted whole |
| Read pages, free | `draw.mjs --sector saas --sections intro,about,list,faq,footer --have product-ui --options 3` | type-only-statement, quiet-instrument, sentence-as-layout | **quiet-instrument**. Where competitors sell, the register under-states. Its small exact demonstrations are real records with their sources |
| About sheet | `--keep palette,type --direction quiet-instrument` | seed 1189651238 | **Redraw.** The free deal gave Young Serif and Source Serif 4 on bone, green and chocolate. The skill requires pages and app screens to share one palette and one face |

**Palette checked against the subject** (skill, step 3). The dealt proportions are adopted: ink chrome, one hue on the
data, colour under 3% of the screen. The colours come from the subject:
- Compass navy replaces the dealt near-black ink.
- Compass's forest evergreen replaces the dealt chart hue `#1d5fbf`.
- A blue-grey "ledger" ground replaces `#f8f8f8`. This is the sheet's own "face" option.

**The idea, by the skill's sentence.** On this page, the register's public filings become the one hue on an ink
ledger: the colour is the data. **The competitor test:** Cleanview's page is sales-led and orange-led, so the
sentence fails for it. **The one live thing:** find a project, company or place and see it on the map at once, with
the record's source and retrieval date in the panel.

## 4. Tokens

These are exact values, generated from the lane's single source (`brand/work/system.py`). Each contrast is the WCAG
2.x ratio against the page ground (`--bg`) and against panels (`--surface`).

| Token | Light | Dark | Role | Light: on bg / on surface | Dark: on bg / on surface |
|---|---|---|---|---|---|
| `--color-ink` | `#0b1023` | `#f2f3f6` | brand ink (Compass navy); text, the primary action, the wordmark | 16.66 / 18.87 | 17.28 / 16.00 |
| `--color-paper` | `#eef1f5` | `#0e0f11` | page ground (blue-grey ledger) | 1.00 / 1.13 | 1.00 / 1.08 |
| `--color-muted` | `#4a5063` | `#b0b5bf` | secondary text | 7.08 / 8.02 | 9.32 / 8.63 |
| `--bg` | `#eef1f5` | `#0e0f11` | page ground | 1.00 / 1.13 | 1.00 / 1.08 |
| `--surface` | `#ffffff` | `#17181b` | panels, sheets, the floating bar | 1.13 / 1.00 | 1.08 / 1.00 |
| `--raised` | `#f5f7fa` | `#1f2024` | a filled field on a surface | 1.06 / 1.07 | 1.18 / 1.09 |
| `--active` | `#e8ebf1` | `#26282d` | selected row, pressed secondary | 1.05 / 1.19 | 1.30 / 1.20 |
| `--text` | `#0b1023` | `#f2f3f6` | primary text | 16.66 / 18.87 | 17.28 / 16.00 |
| `--text-muted` | `#4a5063` | `#b0b5bf` | secondary text | 7.08 / 8.02 | 9.32 / 8.63 |
| `--link` | `#0b1023` | `#f2f3f6` | links, underlined | 16.66 / 18.87 | 17.28 / 16.00 |
| `--link-visited` | `#4a5063` | `#b0b5bf` | visited links, underlined | 7.08 / 8.02 | 9.32 / 8.63 |
| `--border` | `#dde1e8` | `#2a2c31` | panel and card edges (1px) | 1.16 / 1.31 | 1.37 / 1.27 |
| `--rule` | `#eceff3` | `#1d1e22` | row rules in tables | 1.02 / 1.15 | 1.15 / 1.07 |
| `--control` | `#7d8496` | `#7b808b` | input and outlined-control borders | 3.30 / 3.74 | 4.84 / 4.48 |
| `--focus-ring` | `#1d7356` | `#5fcf9f` | 2px focus outline | 5.09 / 5.77 | 9.96 / 9.22 |
| `--primary` | `#0b1023` | `#f2f3f6` | the one filled button | 16.66 / 18.87 | 17.28 / 16.00 |
| `--primary-ink` | `#ffffff` | `#0e0f11` | text on the primary button | 18.87 on primary | 17.28 on primary |
| `--secondary` | `#e8ebf1` | `#26282d` | grey secondary button fill | 1.05 / 1.19 | 1.30 / 1.20 |
| `--secondary-ink` | `#0b1023` | `#f2f3f6` | text on the secondary button | 15.80 on secondary | 13.29 on secondary |
| `--accent` (was `--color-copper`) | `#1d7356` | `#5fcf9f` | the one data hue: active projects, the lead figure on hover, positive change | 5.09 / 5.77 | 9.96 / 9.22 |
| `--accent-deep` (was `--color-copper-deep`) | `#16624a` | `#5fcf9f` | the accent as small text | 6.44 / 7.29 | 9.96 / 9.22 |
| `--accent-tint` (was `--color-copper-tint`) | `#5fcf9f` | `#5fcf9f` | the accent on navy or dark fields | 9.81 on navy | 9.96 / 9.22 |
| `--accent-ink` | `#ffffff` | `#0e0f11` | text on an accent fill | 5.77 on accent | 9.96 on accent |
| `--needle` | `#e68023` | `#e68023` | Compass orange: the mark's north pointer only, never text or data | 2.48 / 2.81 (a mark, not text) | 6.83 / 6.32 |
| `--negative` | `#c4242a` | `#f0857a` | a change in the bad direction, with its sign | 5.10 / 5.78 | 7.62 / 7.05 |
| `--dot` | `#1d7356` | `#5fcf9f` | project marker, in process | 5.62 on land | 8.87 on land |
| `--select` | `#0b1023` | `#f2f3f6` | the 2px selected-tab line | 16.66 / 18.87 | 17.28 / 16.00 |
| `--map-land` | `#fbfcfd` | `#1a1c20` | basemap land | — | — |
| `--map-water` | `#dfe5ee` | `#0b0c0e` | basemap water | — | — |
| `--map-border` | `#c3cad6` | `#32353c` | state and country lines | — | — |
| `--ctx` | `#5b6275` | `#8b909b` | existing infrastructure, drawn at 24-34% opacity | 5.37 / 6.09 | 5.99 / 5.55 |

**Status families.** Each family pairs a text colour (the chip text and the map mark) with a pale fill (the chip).
The ring, half-ring, square, check and triangle glyphs carry each state with its word (D-5).

| Family | States | Light text / fill | Text on fill | Dark text / fill | Text on fill |
|---|---|---|---|---|---|
| Neutral | announced, unknown, closed | `#3d4254` / `#eceef3` | 8.59 | `#c2c7d0` / `#24262b` | 8.92 |
| Progress | filed, studied, permitted, under construction, reinstated | `#1d7356` / `#e0f2e9` | 4.96 | `#6fd8a8` / `#15302a` | 8.11 |
| Committed | contracted, awarded | `#0b1023` / `#dde2ee` | 14.55 | `#cdd5ea` / `#232a3d` | 9.73 |
| Success | built, open | `#0e3f2c` / `#d6efe1` | 9.79 | `#a8e8c8` / `#1a352b` | 9.47 |
| Danger | withdrawn, cancelled, frozen | `#a3211b` / `#fbe4e1` | 6.19 | `#f3a39a` / `#3a201e` | 7.48 |

Progress, the in-process states, is the main data element, so it takes the accent. Committed moves from copper to
navy. That is the one moment a project becomes contracted, drawn as a filled navy square.

**Existing-infrastructure palette.** The plant, asset and region colours stay as they are in `styles.css`. Each one
still reaches 4.5:1 as legend text on the new grounds: light 4.57 to 7.81 on `--bg`, dark 7.18 to 11.16. On the map
they draw faint (§5.3). One watch item: `--asset-rng` `#2f7a5a` sits near the accent. RNG is drawn as a pentagon, so
its shape tells it apart, but the build lane may move it to `#2f6a7a`.

**Type.** Geist and Geist Mono, SIL OFL 1.1 (https://github.com/vercel/geist-font, licence
https://github.com/vercel/geist-font/blob/main/LICENSE.TXT), variable `wght` 100-900. They are self-hosted from
the release's woff2 files, with no font CDN (D-21). The mocks used the fontsource latin subset.
- **App screens:** 12, 14, 16, 24, 28 and 36px. Body 14-16. The title is 24/600. The figures are 28 and 36 at 400,
  with tabular figures.
- **Read pages:** 14, 16, 22, 28 and 40px.
- **Geist Mono** sets identifiers only (queue IDs, docket numbers, keys).
- **The rejected alternative.** If the owner wants a closer type match to Compass, Nunito Sans is the measured
  nearest OFL face to Compass's Avenir-like headline. Its proportion distance is 0.027, against Figtree 0.029, Mulish
  0.092 and Albert Sans 0.126. Albert Sans also lacks tabular figures. The owner could supply an Avenir web licence
  instead. The skill's dealt Geist was kept because all 23 studied app screens use a neo-grotesk, and a light
  geometric face is weaker at 12-14px.

**Radius, space and motion.** Cards take an 8px radius and controls 6px. Space keeps the 4px base. There are no
shadows at rest. A floating menu or the drawer takes `--shadow-float: 0 8px 24px rgba(11,16,35,.14)`. The motion
tokens are unchanged. One addition: a map marker keeps roughly its screen size on zoom, its radius scaled by
zoom^-0.8.

## 5. The marks and the signature

### 5.1 Wordmark, mark and lockup

- **Wordmark.** "inframation" in lower case, tracking -0.03em, set in two parts that name what it joins:
  **infra**structure and infor**mation** (owner, 2026-10-23: "emphasize the two parts of the word Infra and mation";
  variant B chosen, "I love it"). "infra" is Geist 700 in `--text` (navy `#0b1023`; `#eef1f5` on dark); "mation" is
  Geist 400 in `--text-muted` (slate `#4a5063`, 8.5:1 on white; `#a6acb9` on dark). One word, no space or rule between
  the parts; the accessible name stays "Inframation". The first i is dotless, and its dot is an orange north pointer.
  The pointer is a triangle 0.32em wide and 0.38em tall, with its foot 0.07em above the x-height. Variants A (weight
  only), C (mation in evergreen: rejected, green is the data colour) and D (mation bold) are in the review page.
- **Mark (16px favicon).** The same i, orange pointer over a navy stem:
  `<path fill="#e68023" d="M8 0 11.9 6.6H4.1Z"/><rect x="5.7" y="8" width="4.6" height="8" rx=".7"/>`. The stem turns
  `#f1f3f6` under `prefers-color-scheme: dark`. Checked at 16, 32 and 64px on light and dark tabs.
- **Lockup.** The wordmark, then "a Compass company" in Geist 500, `--text-muted`, at 0.58 of the wordmark's size. It
  sits on the wordmark's baseline, 14px after it. The accessible name is "Inframation, a Compass company".
- **Endorsement.** The footer and About carry "Inframation is a Compass company. Compass International Trading
  Group, LLC, Denver, Colorado."
- **The Compass logo is never drawn or traced.** The endorsement is text until the owner supplies Compass's official
  SVG.
- **Is the needle wise?** Yes, with one condition: the owner, who controls both brands, approves it. A north pointer
  is a generic map symbol, it points straight up where Compass's needle points north-east, and it is the one element
  the two brands share. Endorsed brands commonly share one such element. If Compass ever wants its orange needle
  exclusive, the dot becomes a plain navy dot, and nothing else changes.

### 5.2 Signature

- **The front page leads with three live figures.** The lead figure, 5,629 active projects, is set at 36px; the two
  siblings are at 28px.
- **The map draws the projects as its only saturated layer.** The map, the figure and the panel bar share one
  evergreen, so the colour is the data.
- **The needle-dotted i** names the family in every tab and card.

### 5.3 Basemap and markers

- **Land** is `--map-land` and **water** is `--map-water`. **State lines** are 0.75px in `--map-border`.
- **Projects** are drawn at the county where the source gives only a county (D-8):
  - in process: filled evergreen discs;
  - contracted: navy squares;
  - announced: hollow rings with a 1.5px edge.
- **Mark size** scales by area with the count, and holds its screen size on zoom.
- **Existing plants** are 1.5px dots and **pipelines and lines** are 0.75px strokes, all in `--ctx` at 24% opacity on
  light and 30-34% on dark. They are present on arrival, as the owner asked on 2026-10-20, and they recede.

### 5.4 Share card

The card is 1200x630 on a navy field, a read surface, where navy may be a ground. It carries:
- the lockup in white, with the orange pointer;
- the line "Planned US energy projects, each linked to its source.";
- "From grid queues, permits, dockets and RFPs";
- the national project map in `#5fcf9f` over faint grey context.

It carries no live figures, because a cached card would show stale numbers; the page's description carries them
(PLAN, inbox-link row). Contrast on the navy field: near-white text `#f2f3f6` 17.01:1, the endorsement `#b0b5bf` 9.17:1, the
evergreen tint 9.81:1, the orange pointer 6.72:1. `web/og_card.py` redraws it in Pillow from these values, and the file name changes so cached
cards refresh.

## 6. Declutter rules (every page)

The owner's comparison with Cleanview: "much less cluttered ... Way less words. Fewer descriptions of titles."

1. **A title stands alone.** No description sentence under a page or section title.
2. **A standfirst is at most one line** at 1440px, and it appears only where the title alone cannot say what the
   page is.
3. **Labels over sentences.** A label is a noun phrase in sentence case, never tracked capitals.
4. **Progressive disclosure.** Explanations sit behind "Why?", "About this map" or the glossary, never on the
   working surface.
5. **One figure, one place.** A number appears once per screen. The floating bar's count says what the map draws
   ("4,633 mapped"), not the headline figure again.
6. **Budgets for the first screen.** Measured with the coordinator's counter (`brand/fold.py`).

| | Words at 1440 | Controls at 1440 | Words at 390 | Controls at 390 |
|---|---|---|---|---|
| Budget | ≤60 | ≤25 | ≤30 | ≤25 |
| Live site, 2026-10-23 | 304 | 60 | 79 | 18 |
| Local dev store | 260 | 57 | 69 | 16 |
| Current brand, decluttered layout | 43 | 24 | 27 | 21 |
| Chosen system | 49 | 25 | 30 | 21 |

Primary text is 16.66:1 on the ground and 18.87:1 on panels; on dark it is 17.28:1 and 16.00:1. There is one accent.

## 7. Component changes for the build lane

Each row names the file, the selector and the change. `brand/work/component_pass.css` is a working draft of rows
C-2 to C-5, and `brand/css/system.css` is the token override used for the renders.

| ID | File | Selector or place | Change |
|---|---|---|---|
| T-1 | `web/static/css/styles.css` | `:root` (lines 181-326) | Replace the values with §4's light column. Rename the copper tokens to `--color-accent`, `--color-accent-deep` and `--color-accent-tint`, keeping the old names as aliases for one release. Add `--raised`, `--active`, `--rule`, `--control`, `--primary`, `--primary-ink`, `--secondary`, `--secondary-ink`, `--needle`, `--negative` and `--shadow-float`. Set `--shadow-1: none` |
| T-2 | `web/static/css/styles.css` | both dark blocks (lines 329-460) | Replace with §4's dark column: a true-neutral dark, not navy |
| T-3 | `web/static/css/styles.css` | `@font-face` blocks and the `--font-*` tokens | Geist and Geist Mono woff2, self-hosted; `--font-display` and `--font-body` become Geist, `--font-mono` becomes Geist Mono. Compute a metric-matched "Geist Fallback" by the method of the existing fallback faces |
| T-4 | `web/templates/base.html` | font preloads | Preload the Geist latin file only |
| B-1 | `web/templates/base.html` | `.masthead` | Remove it. The V1 shell (`docs/30` §11.2) already drops it. The freshness line moves beside the figures as "Sources read {date}" |
| B-2 | `web/templates/base.html`, `styles.css` `.wordmark` (line 509) | wordmark | The lockup markup of §5.1: a dotless ı with the inline pointer SVG, then "nframation", then a `span` "a Compass company". A visually hidden accessible name "Inframation, a Compass company" |
| B-3 | `web/static/img/favicon.svg`, `favicon.ico`, `apple-touch-icon.png` | favicon | The §5.1 mark |
| B-4 | `web/templates/base.html` | `.site-footer` | First line: "Inframation is a Compass company. Compass International Trading Group, LLC, Denver, Colorado." |
| B-5 | `web/og_card.py` | the card | Redraw it to §5.4 with the Geist files. Write it as `og-card-2.png` so cached cards refresh, and update the `og:image` paths and the test |
| C-1 | `styles.css` | `h1` (line 546) | `--text-6` becomes 28px. An app title stays at 32px or less |
| C-2 | `styles.css` | `.stamp-label`, `.provenance-panel__licence`, `.filter-field label`, `.filter-field__label`, `.filter-field--placement legend`, `.record-table--compact thead tr::before`, `.map-panel__heading`, `.filter-field--asset-types legend`, `.in-view-list__group-name`, `.pipeline-table caption`, `.drawer-source__label`, `.reuse-badge`, `.field label`, `.alert-card__filters dt`, `.field--choices legend`, `.map-search__group-name`, `.view-switch__label` | `text-transform: none; letter-spacing: 0; font-family: var(--font-body); font-size: 13px; font-weight: 500`. Clears the inspector's micro-label FAIL |
| C-3 | `styles.css` | `.section-num`, `.timeline time`, `.provenance-panel__name::before`, and every mono label in C-2 | The text face with tabular figures. Mono stays only on `.record-table .mono`, `.record-company .mono`, `.field-grid .mono`, `.value-source .mono`, `.raw-fields`, `.submit-ref` and `.map-error .mono` (identifiers) |
| C-4 | `styles.css` | `.map-panel` (981), `.map-search__input` (1801), `.map-search__option.is-active` (1841), `.map-overlay .map-controls button` (1846), `.sf-key` (2111), `.sf-key__substation` (2129) | No shadow: a 1px `--border` only. `.map-drawer` (1285), `.map-search__list` and `.map-search__empty` (1818), and `.map-controls--regions.is-open` (1904) take `--shadow-float`. Clears the button-fx FAIL |
| C-5 | `web/templates/*` (record pages) | `.section-num` numbering ("1. Sources") | Remove the numbers. The skill bans index numbering |
| C-6 | `styles.css` | `.button-link` (1583), `.button-secondary` (1574) | Primary: `--primary` fill, `--primary-ink` text, 6px radius. Secondary: `--secondary` fill, `--secondary-ink` text, no border. The accent never fills a button |
| C-7 | `styles.css` | links | `--link` is navy with a 0.07em underline at a 0.18em offset. On hover the underline takes `--accent` |
| M-1 | `web/static/js/map.js` | marker paint | Project markers: in process uses `--accent`; contracted is a navy square; announced is a hollow ring. Radius scales by area and holds its screen size on zoom (`circle-radius` interpolated by zoom) |
| M-2 | `web/static/js/map.js`, `basemap.js` | context layers | Plants, pipelines, transmission and retired plants draw at 24% opacity on light and 30-34% on dark, 0.75-1.5px, growing with zoom. Their layer order sits under the projects |
| M-3 | `web/templates/home_map.html` (V2) | the front strip | The h1 "Planned US energy projects". Three figures, each a link: 36px lead in `--text`, siblings at 28px, labels at 14px in `--text-muted`. Then "Sources read {date}". Nothing else above the map |

## 8. Before and after

- **Before.** Live homepage, inspected by the coordinator: 2 FAIL (micro-labels, button shadows) and 3 WARN
  (cream ground, three typefaces, a 33px title). Local dev store, `inspect.mjs --app`: 3 FAIL (micro-labels,
  button-fx, four typefaces at 390) and 2 WARN.
- **After.** The front-page mock, light and dark, `inspect.mjs --app` at 1440, 768 and 390: 0 FAIL, 0 WARN, Geist
  100%, six sizes. `lint.mjs --app`: 0 FAIL, 0 WARN. axe-core: 0 serious or critical. CLS 0.
- **About** (`inspect.mjs`, page rules): 0 FAIL, 0 WARN, 4 real pictures and one timelapse. It has no live thing.
- **The record page** with the tokens and the C-2 to C-5 draft: rendered at 1440 and 390, light and dark. It was not
  gated with the inspector, because it is today's template with an override, not a build.
- **Critique** (`references/critique.md`, three rounds on the front page, two on About):
  - front page 6.6, then 7.1, then 7.55;
  - About 7.1, then 7.45.

  What still falls short: the front page's idea is the category's (a dot map with figures), so the face carries the
  difference. There is no weekly pulse, because the stores hold no change events. On a phone, a band of Mexico shows
  under the US at the national view.

The screenshots are listed in `directions.json` under `current.screenshots` and `compass_system.complement.screenshots`.

## 9. Conflicts between `docs/31-design-system.md` and the taste skill

The build lane updates `docs/31` and `docs/04` (D-20, D-21, D-22) with the changes below once the owner's pick is
applied.

| `docs/31` place | Conflict | Change |
|---|---|---|
| §1.1 core colour, D-20 | Cream paper `#f6f4ef` with a copper accent is the skill's banned cream-and-terracotta pair (palette-cream WARN; terracotta FAIL when copper is text) | §4 tokens |
| §1.1, DS-2 dark theme | A navy dark theme `#0e1e30` is banned on app screens | True-neutral dark `#0e0f11` |
| §1.2 status families | Committed reuses copper; the progress blue `#2f6480` is a second hue beside the accent | §4 families: progress takes the one hue, committed goes navy |
| §1.3 typography, D-21 | Three faces (Newsreader, Plex Sans, Plex Mono). The skill allows two at most, and one grotesk on app screens | Geist, plus Geist Mono for identifiers |
| §1.3 as built | Mono upper-case labels at 12.8px (filter labels, masthead, "LIVE" stamp, in-view group names, legends) are tracked capitals and monospace labels | C-2, C-3 |
| §1.3, D-22 scale | Seven steps on a 1.2 ratio with display steps to 39.8px. App titles run 18-32px and use about six sizes | 12/14/16/24/28/36 on app screens, 14/16/22/28/40 on read pages |
| §1.5 shadow | `--shadow-1` sits on resting controls and panels | `none`; `--shadow-float` on menus and the drawer only (C-4) |
| §3, §5 masthead | "Inframation register · public edition" in tracked mono capitals | Removed (B-1); the edition becomes the sentence-case "Sources read {date}" |
| §5.15 share card | Masthead caps, Plex, copper tint on ink | §5.4 card |
| §5.16 buttons | The primary is filled with the accent and the secondary outlined in copper; the skill wants a near-black primary and no accent on chrome | C-6 |
| §5.17, §5.19 map key and panel | Upper-case group headings and a panel shadow | C-2, C-4 |
| §6, §7 numbered sections | "1. Sources" index numbering | C-5 |
| §1.1 basemap tokens | Tinted to the cream paper | §4 map tokens |
| `docs/30` §5 rationale | The 1.2 modular ratio | Replaced by "few sizes, far apart" (skill `references/type.md` §2) |

## 10. How we design

Every design lane works to `docs/31-design-practices.md`, the standing brief. Its order of authority:
1. the `CLAUDE.md` guardrails;
2. WCAG 2.2 AA;
3. the taste skill;
4. the brief itself;
5. `docs/31-design-system.md`.

It adds the data-product practices: the product as the hero with one live thing, provenance and freshness beside the
figures, encodings by perceptual accuracy, data-ink, colour never alone, map layer hierarchy, Core Web Vitals and
target sizes, and every state designed.

**What spur.us contributes.** The owner's aspirational reference serves a bot checkpoint to automated browsers, which
we do not bypass, so nothing of its detail is imitated. We take only what a data-intelligence company's site is
admired for: the product's own live data as the hero, crisp restraint, and credibility from specifics.

**Where this system departs from the brief:**
- At the national view, county dots scale by area without numerals, to meet the word budget. Counts show in the panel
  and on hover.
- No weekly pulse is drawn. A sample would be an invented count.
- The panel's loading, empty and error states are specified (`docs/30` §11.7, S-7) but not drawn in the mock.
- Only CLS was measured. LCP and INP need the built page.

## 11. Keep as convention

The identity changes; the interactions do not. Professionals rely on these patterns (Jakob's law,
https://lawsofux.com/jakobs-law/):
- the full-screen map with one floating panel, collapsed to a summary by default;
- search, Filters and the count in one floating bar;
- zoom controls and Reset;
- the record drawer, and the bottom sheet on phones;
- the Map and List switch;
- the attribution strip at the map's foot.

**The edition masthead.** It is not brought back as a tracked-capitals line. The edition idea survives as one stamp
beside the figures, "Sources read 23 Oct 2026", linking to each source's fetch dates. It is dated and verifiable,
which is the positioning.

## 12. Open questions for the owner

1. **Approve the orange pointer** as the shared Compass element in Inframation's mark (§5.1), or ask for a navy dot.
2. **Supply Compass's official logo SVG** if the footer or About should carry it beside the text endorsement.
3. **Keep Geist,** or switch to Nunito Sans for a closer Compass type match, or supply an Avenir web licence.
4. **Accept the green data hue.** Some readers will see "green energy" in it (§2.2). The fallback is Compass's royal
   blue, at the cost of resembling Interconnection.fyi.

## 13. Assumptions

| ID | Assumption | Basis | Affects |
|---|---|---|---|
| BR-1 | The owner may lawfully use "a Compass company" and Compass's navy and orange for Inframation | The owner's direction of 2026-10-23 that Inframation will be a Compass entity | The lockup, the footer, About |
| BR-2 | Geist's OFL 1.1 has no Reserved Font Name blocking a subset under its name | The licence file's copyright line names none | T-3 |
| BR-3 | The mock's dots come from the e2e test store's located active proposals (3,983 county points), drawn as stand-ins. The figures are the live site's of 2026-10-23 (the coordinator's capture) | A static mock; the build draws from the API | The front-page renders |
| BR-4 | Compass's type looks like Avenir from a 390px screenshot only | The 1440px load was challenged | §4 type note |

## 14. Sources

- Taste skill: `.claude/skills/taste-skill` (https://github.com/Leonxlnx/taste-skill, MIT). Its `SKILL.md`,
  `references/app-screens.md`, `colour.md`, `type.md`, `layout.md` and `critique.md`.
- WCAG 2.2 contrast and non-text contrast: https://www.w3.org/TR/WCAG22/#contrast-minimum and
  https://www.w3.org/TR/WCAG22/#non-text-contrast
- CIEDE2000: Sharma, Wu and Dalal (2005), *The CIEDE2000 color-difference formula*,
  https://hajim.rochester.edu/ece/sites/gsharma/ciede2000/
- OKLCH: Björn Ottosson, *A perceptual color space for image processing*, https://bottosson.github.io/posts/oklab/
- Geist: https://github.com/vercel/geist-font. Nunito Sans: https://github.com/Fonthausen/NunitoSans. Figtree:
  https://github.com/erikdkennedy/figtree. Mulish: https://github.com/googlefonts/mulish. Albert Sans:
  https://github.com/usted/Albert-Sans
- Photographs and footage on About, all via Wikimedia Commons:
  - "Transmission towers at sunset in East Texas", Matthew T Rader, CC BY-SA 4.0,
    https://commons.wikimedia.org/wiki/File:Transmission_towers_at_sunset_in_East_Texas.jpg
  - "Roscoe Wind Farm in West Texas", Matthew T Rader, CC BY-SA 4.0,
    https://commons.wikimedia.org/w/index.php?curid=101799395
  - "Topaz Solar Farm, maintenance of a huge solar farm with 5 million solar panels", Sarah Swenty / USFWS, CC BY 2.0
  - "Pine forest - Colorado (44561126871)", Heath Cajandig, CC BY 2.0
  - "Alta Wind Farm Kern County California Google Earth Timelapse 1984-2018", Google Earth, CC BY 3.0
- Map outlines: Natural Earth and US Census Bureau (`data/vendored/regions`). Plants: EIA-860M. Pipelines: the EIA
  US Energy Atlas. All public domain.
