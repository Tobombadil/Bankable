# Information architecture and flows

**Status:** Sprint 1 deliverable, v1 · 2026-09-12 · owner: product-designer · reviewed by: owner (pending)
**Authority:** `docs/04-standards.md` §2.2–2.3 (D-8…D-18); `docs/30-design-references.md` §4–§9 (typography, scales,
map model, motion, banned patterns) are the design inputs this doc builds screens against.
**Inputs:** `CLAUDE.md`; `.claude/agents/product-designer.md`; `docs/10-prd-mvp.md` §4 (US-101…US-1003);
`docs/23-api-spec-outline.md` §3–§4; `docs/21-data-model.md` §8.
**Product name used in this doc:** Inframation (owner, 2026-10-16; `docs/12-naming-shortlist.md` §12).

## 1. Three surfaces, one grammar

D-16 fixes the shape: Public (`inframation.io`, no key), Pro (same host, entitlement-gated routes),
Admin (`admin.` host, role `operator`/`owner`). D-4 and D-17 fix the rule governing every screen below: one filter
vocabulary, one URL grammar (`docs/23` §7), shared across list, map and feed, on every tier — a tier changes what
a query returns, never its shape. Primary nav is identical across public and Pro. ~~**Proposals · Opportunities ·
Map · Feed · Alerts · API**~~, then ~~**Map · Proposals · Opportunities · Assets · Grid connection points · Companies ·
About & sources · Pricing · Alerts · Sign in**~~ (as built 2026-10-07, audit UX-7) — **as built 2026-10-21
(`docs/16` §3.1, U-6): Map · Projects · Companies · RFPs & funding · Infrastructure · Alerts**, then Pricing (only
under the `commercial` posture, below), Sign in and the search box. Routes are unchanged: `/`, `/proposals`,
`/organizations`, `/opportunities`, `/assets`, `/alerts`. **Changed 2026-10-22 (§2.1–§2.8, owner's phase decision):**
these six are the first phase, **Find** (the owner's "Prospect"), of a site organised by development phase. **Site
finder** (`/site-finder`) becomes the second link on the day its page ships with results, giving seven. The two later
phases, **Build a deal** and **Get funded** (the owner's "Originate" and "Syndicate"), happen in Bankable and are not
nav items: Build a deal is an action on eligible records, and Get funded is not linked from Inframation at all.
Operations is not a section: operating, retiring and retired assets are the later lifecycle states on the one base map
and in the lists (§2.5). **Infrastructure** is one H1, "Existing infrastructure",
over two tabs that are plain links to their own URLs, *Plants, pipelines & lines* (`/assets`) and *Grid connection
points* (`/interconnection-points`), in a `<nav aria-label="Infrastructure sections">`, not a `role="tablist"`
(`docs/16` §3.2). The link to the current page carries `aria-current="page"`; the link to the section holding it (a
record page, the other Infrastructure tab) carries `aria-current="true"`; weight and an underline are the visible echo.
**About & sources** is reference, not a destination: a utility link in the masthead beside the freshness line, in the
Menu panel below 720px (where the masthead is hidden), and in the footer. The names were the coordinator's
recommendation pending the owner's ratification (`docs/16` §7 question 1); **the owner ratified them on 2026-10-22**
(`docs/00-PLAN.md`, "The section names stand"). They live in one place, `web/labels.py` `SECTION_LABELS`, read by
`web/templates/partials/_sections.html` (nav, masthead, footer, list headers) and by the routes' JSON-LD: a rename is
one line.
Feed and API are not nav items: every list's "View as" switch carries its RSS twin, and the footer links **API and
feeds** (`/docs/api`). Depth ≤ 3 clicks to any record (D-16); breadcrumbs on every record page. The five list pages
carry none (from 2026-10-21; `/proposals` and `/opportunities` never did): the nav marks where the reader is, and the
space goes to data (U-17: the first row now starts under 450px at 1440 on all five, against 488–689px in `docs/16`
§1.2). **As built, 2026-10-10 (pre-beta, owner: paid tiers stay
off and must not distract the beta):** under the `noncommercial` posture "Pricing" is left out of the primary nav on
every page (`web/page.py::paid_tiers_offered`, read from `GET /v1/health` `posture` once per request and registered on
all four environments that render `base.html`); `/pricing` still answers by URL with its posture notice. The footer
never carried it. Under the same posture no page offers "Alerts and API in Pro" or "Live in Pro" (even when the API
does not report the free-alert offer), `/docs/api` says API keys belong to the paid tiers that are not offered instead
of pointing at Pricing, the alerts pages drop "See what the plans include", and `/about` drops its "The paid tier is
workflow" line. Under `commercial` all of it is as before (`web/test_paid_tiers_posture.py`).

**Names (2026-10-07, audit UX-7; sections renamed 2026-10-21, `docs/16` §3.1).** One reader-facing term per concept,
in nav, H1, breadcrumbs, column heads and search headings. Where this doc did not settle a term, the choice and its
reason are recorded here. Section names (nav, list H1s, tab labels) are read from
`web/templates/partials/_sections.html`, never spelled in a page template.

| Concept | Term on public pages | Not used | Note |
|---|---|---|---|
| Proposal (docs/21 `proposal`), the supply side | **Project(s)**: nav "Projects", list H1 "Projects in development", standfirst "Supply side: …" | Proposal(s) as a section name | 2026-10-21, `docs/16` §3.1: "project" is the developers', investors' and utilities' word, and "Proposals" beside "Opportunities" read as synonyms. Pending the owner's ratification (§7 question 1). Routes, API and data model keep `proposal`. As built in the nav, the list H1 and title, and the footer; record-page breadcrumbs, the company and connection-point list headings (`web/list_subject.py`), counts, column heads and JSON-LD names still say "Proposals" and follow in the lanes that own them. |
| Opportunity (docs/21 `opportunity`), the demand side | **RFPs & funding** (nav); list H1 "RFPs, tenders and funding", standfirst "Demand side: …" | Opportunities as a section name | Same date, reason and status as the row above: it says what the rows are (RFPs, tenders, grants). Record-page breadcrumbs and JSON-LD names still say "Opportunities". |
| Existing assets and grid connection points, together | **Infrastructure** (nav); H1 "Existing infrastructure"; tabs *Plants, pipelines & lines* (`/assets`) · *Grid connection points* (`/interconnection-points`) | Assets as a section name | Same date and status. One home for what already exists; "asset" stays the word for one plant, pipeline or line (row counts, record pages, the type chips' "Assets by type"). |
| Organisation (docs/21 `organization`) | **Company / Companies** | Organisation(s), Organization | §3.2 already named the nav item and index "Companies". A public body that issues tenders is still listed under it. Admin keeps the API's word, "Organization". Third in the nav from 2026-10-21 (`docs/16` §3.1: the join between projects, RFPs and assets). |
| Interconnection point (docs/21 §3.24) | **Grid connection point(s)** | Grid points, Grid interconnection points, Interconnection points | This doc never named it; the nav said "Grid points" and the page "Grid interconnection points". Chosen as plain words that fit the nav; the URL `/interconnection-points` and the entity name are unchanged, and the proposal field "Connects at" already uses the verb. From 2026-10-21 the second Infrastructure tab; the list's empty state still says "interconnection points" (`partials/_interconnection_point_rows.html`). |
| Lifecycle state | **Status** (the filter), with the legend's groups: Announced · In process · Contracted · Built or operating · Withdrawn or cancelled · Status unknown | Lifecycle (on public pages) | Groups, not the eleven tokens, as the map legend already does (docs/31 §1.2). |
| Location | Open | — | "Jurisdiction" (proposals), "State" (asset filter), "Location" (asset list) still all hold `US-TX`; not settled in this lane. |
| Development phase (added 2026-10-22, §2.2) | **Find · Build a deal · Get funded**, on About (`/about#deals`) and in the record action; never a nav item while only one phase has a page on Inframation | Prospect, Originate, Syndicate (on public pages) | The owner's words stay the internal names: decision rows, `docs/18`, code keys (`prospect`, `originate`, `syndicate`). Reader words in `web/labels.py` `PHASE_LABELS`. Reason and desk test in §2.2. |
| The finder and its results (added 2026-10-22) | **Site finder** (nav, H1); a result is a **candidate site** | Sites (for finder results), Powered land finder | "Site" already names `/sites/{id}`, the records that sit at one physical site (docs/21 §3.25). A finder result is a corridor band, not a parcel and not a site record, so it is a *candidate* site. The finder lane names its own page parts. |
| The hand-off to Bankable (added 2026-10-22) | Heading **Build a deal**; link **Start a deal in Bankable ↗** | Originate, Send to deal builder | Names the destination, because the link leaves the site (§2.6). |

### 1.1 Public sitemap

**Updated 2026-10-22 (§2.1–§2.7).** The public site grouped by development phase. Routes are as built on
2026-10-22, plus `/site-finder` (being built by the site finder lane) and the `#deals` section of `/about`. Dashed
edges leave Inframation.

```mermaid
flowchart TD
  subgraph FIND["Find (the owner's 'Prospect'): Inframation"]
    H["/ : the base map, every lifecycle state (US-104)<br/>/map answers 301 to /"]
    SF["/site-finder (new; in the nav once it has results)"]
    P["/proposals : Projects"]
    ORGS["/organizations : Companies"]
    O["/opportunities : RFPs & funding"]
    A["/assets and /interconnection-points : Infrastructure (two URL tabs)"]
    PD["/proposals/{slug}"]
    SD["/sites/{id}"]
    OD["/opportunities/{slug}"]
    AD["/assets/{slug}"]
    IPD["/interconnection-points/{id}"]
    ORG["/organizations/{slug}"]
  end
  subgraph DEAL["Build a deal, then Get funded ('Originate', 'Syndicate'): Bankable, bankablehq.com"]
    BK["/assessment until the bridge exists;<br/>the hand-off route of docs/18 §3 after it"]
    BKF["showroom, funder pipeline, roadshow:<br/>signed in, invited funders only"]
  end
  H --> SF & P & ORGS & O & A
  H -.->|"marker, cluster or region"| DR["drawer; bottom sheet below 720px (D-12)"]
  DR --> PD & AD & OD
  P --> PD --> SD
  O --> OD
  A --> AD & IPD
  ORGS --> ORG
  PD & OD & AD & SD --> ORG
  SF -.->|"Start a deal in Bankable"| BK
  PD -.->|"Start a deal in Bankable (§2.6 rules)"| BK
  OD -.->|"open RFPs only"| BK
  AD -.->|"retiring and retired plants only"| BK
  BK --> BKF
  H --> S["/search?q= (US-103)"]
  H --> AL["/alerts, /alerts/new : Alerts, across phases"]
  H --> AB["/about (#sources, #deals), /methodology, /attribution"]
  H --> F["/feeds/* in RSS and JSON, /docs/api (US-503, US-704)"]
  H --> U["/pricing (by URL under noncommercial), /submit, /login, /account,<br/>/privacy, /legal/reuse, /legal/api-licence, /contact, /bot"]
  PD --> RPT["POST /report (US-204)"]
  H --> RB["/robots.txt, /sitemap.xml, /sitemaps/{n}.xml"]
```

What changed from the 2026-09-12 diagram (updated 2026-09-19), which is in git history: the nodes are grouped by
phase; `/interconnection-points`, `/sites/{id}`, `/methodology`, `/attribution`, `/about`, `/alerts` and the utility
pages are drawn as built (they were missing); `/plans` is `/pricing` and `/legal/privacy` is `/privacy`, as built;
the drawer is a bottom sheet below 720px (`docs/17` M-4, built 2026-10-22); `/site-finder` and the Bankable edges are
new. The 2026-09-12 diagram is kept below, folded and superseded, only so its user-story references stay findable.

<details><summary>Superseded: the 2026-09-12 sitemap</summary>

```mermaid
flowchart TD
  H["/  home = /map (US-104)"] --> M["/map"]
  H --> P["/proposals (US-101,102,103)"]
  H --> O["/opportunities (US-301)"]
  H --> S["/search?q= (US-103)"]
  H --> A["/assets (index, 2026-09-19)"]
  H --> ORGS["/organizations (index, 2026-09-19)"]
  P --> PD["/proposals/{slug} (US-201,202,203,204)"]
  O --> OD["/opportunities/{slug} (US-302)"]
  A --> AD["/assets/{slug} (ADR 0008)"]
  ORGS --> ORG["/organizations/{slug} (US-203)"]
  PD --> ORG
  OD --> ORG
  AD --> ORG
  H --> RB["/robots.txt -> /sitemap.xml -> /sitemaps/{n}.xml"]
  M -.->|"select marker/cluster/polygon"| DR["detail drawer (D-12)"]
  DR --> PD
  DR --> OD
  H --> F["/feeds/* .rss/.json (US-503)"]
  H --> DOCS["/docs/api (US-704)"]
  H --> PLAN["/plans (tiers, US-604)"]
  H --> LEGAL["/legal/privacy, /legal/api-licence"]
  PD --> RPT["report a problem (US-204)"]
  P --> INT["/submit (US-1001,1003)"]
  O --> INT
```

</details>

Every node is reachable from `/` in ≤ 3 clicks; `/` (the map) is the landing surface per the owner's map-first
decision (`docs/00-PLAN.md`, US-104), with `/proposals` and `/search` as co-equal entries into the same filtered result
set (D-4) — the map is not "browse" with search as a separate mode, it is one more view of the same query. From
2026-10-22 the map is also the one base map of the Find phase: projects in every lifecycle state and the existing
infrastructure they connect to, so operating, retiring and retired assets are reached on it and in the lists, not in a
section of their own (§2.5).

### 1.2 Pro (live) sitemap

Adds an entitlement-gated layer over the same routes (more fields and more shapes — not fresher data, `docs/23` §3.2):

```mermaid
flowchart TD
  L["/account/login (magic link / Google)"] --> ACC["/account"]
  ACC --> SS["/alerts — saved searches (US-501,504)"]
  ACC --> KEYS["/account/keys (US-701)"]
  ACC --> SUB["/account/subscription (US-602)"]
  ACC --> EXP["export from any list/map (US-603)"]
  SS --> SSN["/alerts/new — save from current filter (US-501)"]
  SS --> SSD["/alerts/{id} — history, pause, edit (US-502,504)"]
  M2["/map, /proposals, /opportunities (Pro fields)"] --> DR2["drawer (adds field_provenance)"]
  M2 --> SSN
  DOCS2["/docs/api"] --> KEYS
```

Pro reuses every public route; the only new top-level surfaces are `/account` and `/alerts`. A Pro user's `/map`
is the identical URL a public visitor sees, with `lag_days = 0` in the envelope (`docs/23` §10) — no separate
"Pro map" page exists, per D-4.

### 1.3 Admin sitemap

```mermaid
flowchart TD
  AH["admin.inframation.io / — source health (US-904)"] --> SRC["/sources/{id} (US-904,905)"]
  AH --> RUNS["/source-runs (DQ, snapshots)"]
  SRC --> GATE["publish-state + gate clearance (US-905, legal role)"]
  AH --> REC["/records — proposals/opportunities/orgs edit+merge (US-905,202)"]
  AH --> RES["/resolution-candidates (entity-resolution review)"]
  AH --> EXT["/extractions (low-confidence review)"]
  AH --> TASKS["/tasks — reports, intake, deletions (US-907,204,1002,910)"]
  TASKS --> INTREV["approve/link/reject intake (US-1002)"]
  AH --> POSTS["/posts — social review queue (US-802)"]
  POSTS --> CHAN["/channels/{channel}/auto-publish (owner only, US-802 AC2)"]
  AH --> USERS["/users (US-901)"]
  AH --> CUST["/customers, /subscriptions — read via adapter (US-902,903)"]
  AH --> LEADS["/leads — create from match (US-403)"]
  AH --> KEYS2["/keys — issue/revoke on behalf of account (US-701)"]
  AH --> COSTS["/costs (US-909)"]
  AH --> AUDIT["/audit (US-901 AC2, S-4)"]
```

Admin nav order follows `docs/23` §4's grouping exactly: Sources → Records → Resolution/Extraction → Tasks →
Posts → Users/Customers → Keys/Costs/Audit. Every admin write is an audited event (S-4); nothing here is reachable
without `operator`/`owner` role and the `admin.` host's second factor (S-2).

## 2. Navigation model: map and search as co-equal entries

D-8…D-14 govern this. The nav bar is not "Proposals, Opportunities, **Map**" with Map as a third list — Map is
the home route (`/`), carrying the same filter rail `/proposals` and `/search` render inline. List ↔ map ↔ feed
preserve `?kind=&technology=&lifecycle_state=&jurisdiction=&bbox=…` verbatim (D-17, US-104 AC2); a "View as: Map ·
List · Feed" control sits beside the filter bar, not in a menu (US-104 AC1–AC2). **Changed 2026-10-22 (decision rows
"The home map drops its In view side panel" and "The home map keeps 'what is in view'"; being built by the home map
panel lane):** the home map loses its "View as" row; its RSS link moves into "Save this search as an alert", and the
list pages keep their switch and their feed link. The map is also the one base map of the Find phase, and the site
finder (`/site-finder`) is a second map-shaped entry beside it, with search (§2.1, §2.3).

Placement precedence (D-8, US-104 AC1): `exact` → marker; `county_centroid` → centroid marker, visibly labelled;
state-only → state aggregate marker plus a side-list row; no geography → "Unplaced (N)", never dropped. Validated
as a shipped pattern by Interconnection.fyi's county/state toggle (`docs/30-design-references` §2.3). **Changed
2026-10-22:** the side list is gone from the home map; "what is in view" is one floating panel that summarises at
national and multi-state zooms and lists projects once zoomed in (a bar under the map on phones). Where that panel
shows the unplaced count is the home map panel lane's choice; the rule that an unplaced record is never dropped
stands, and the Projects list carries every record.

Restricted-precision (D-9, US-104 AC3): a record whose only geometry provenance is `restricted`/`unknown` never
renders an exact point on public or Pro surfaces regardless of source precision — county centroid, drawer states
"location shown at county level (source licence)" (`docs/30-design-references` §7, §2.13).

Clustering (D-10, US-104 AC4): ≤500 visible records → individual focusable markers; above it, server clusters
from `/v1/proposals/geo?bbox&zoom` carrying `{count, lifecycle_state_counts, technology_counts, capacity_mw_sum}`.
Glyph = size (count) + chip colour/label (dominant lifecycle family) + shared icon (dominant technology,
consolidated per Flightradar24, `docs/30-design-references` §2.12) — never a bespoke icon per fine-grained value.

Selection (D-12): marker, cluster-row or polygon opens a drawer, not a navigation — `role="dialog"`, focus trap,
fields, chip, Sources, last three events, D-3 line, link to the full record; `Escape`/close returns focus. **Changed
2026-10-22 (`docs/17` M-2, M-4; built):** below 720px the drawer is a bottom sheet (`docs/31` §5.8), and one tap does
one thing (one click dispatcher with a priority order; a region tap no longer leaves the map).

Keyboard/screen-reader path (D-14, US-104 AC5): a synchronised "in view" list, reachable by skip link — arrow
keys pan, `+`/`-` zoom, `Enter` opens the focused marker, roving tabindex ≤500 rendered, `aria-live="polite"`
announces "N proposals, M opportunities in view" debounced 500ms; above 500, the list is the primary path.
**Retired 2026-10-22 (owner: "Lets not optimize on keyboard users for now. Lets assume all users have a mouse or are
on mobile"):** D-14's in-view list and its keyboard-path tests are retired, not skipped. The Projects list is the
text equivalent of the map, and the map's label points to it. The floating panel that returns "what is in view" is
built for mouse and phone users; keyboard-only and screen-reader optimisation stays deferred, to revisit before any
public-sector or utility procurement.

### 2.1 Phases: the site by stage of development (added 2026-10-22)

**Amended the same day (owner, decisions log 2026-10-22, "The site has two modes"):** above the phases sit two modes,
**Market | My pipeline**. Market is the flat Find nav below and all a signed-out visitor sees; My pipeline (Build a
deal, later Get funded) appears once signed in, as a two-way switch at the top of the header. The nav table and
wireframes in §2.3 show Market; the implementation lane adds the switch.

**Asked by the owner, verbatim:** "Could we split the site into each phase of development? from Prospecting (Map of
prospective development sites and what makes them valuable), Origination (Bankable style; data goes from the map into
deal builder), Syndication (built deals in bankable style now showcased), Operation (actively tracking sites under
development, operating, decommissioning or repowering)", then "consider putting operations under the prosposector or
just as one base map". With it, the owner's priorities for this design: "Leave the highest priority to be a good
experience for the average user not the marginal disabled user"; mouse and phone users first; early credibility, so
no empty sections.

**Decision rows this section applies** (`docs/00-PLAN.md`, all 2026-10-22 unless dated): "The site is organised by
development phase: Prospect, Originate, Syndicate. Operations is not a fourth section" (a section shows in the
navigation only once it has something in it; until the hand-off exists, Originate is an action on records, not a
tab); "Bankable is the origination and syndication half"; "Prospecting layers"; "Powered-land site finding is a
product goal"; "The site finder ranks corridors by a prospect score"; "The home map keeps 'what is in view', as one
floating panel"; "The section names stand"; "The responsive floor is 360px"; and, from 2026-10-21, "Company matching
and subscriptions are free" (the owner: "I need something to give me early credibility") and "Matching targets
developers first".

**Status: confirmed by the owner, 2026-10-22** (decisions log), **except row 5**: the "Start a deal in Bankable" block is
held until the six `noncommercial` sources have granted commercial reuse or been withdrawn, and a legal read of the
link (decisions log, 2026-10-22, "The Bankable bridge waits on the six noncommercial-only sources").

| # | Proposal | Where argued |
|---|---|---|
| 1 | The phases are shown to readers as **Find · Build a deal · Get funded**. "Prospect, Originate, Syndicate" stay the internal names. | §2.2 |
| 2 | The nav stays flat and holds only the sections that have pages on Inframation, all of them Find's: **Map · Site finder · Projects · Companies · RFPs & funding · Infrastructure · Alerts**, then Sign in and search. Site finder joins on the day its page has results. No phase labels in the nav while only one phase has pages here. | §2.3 |
| 3 | RFPs & funding is a Find list. An open RFP's page is one of the doorways to Build a deal. | §2.4 |
| 4 | Operations is the later lifecycle states on the one base map and in the lists: the map's Status control covers projects and existing infrastructure; Infrastructure gains a Status control. No section, no URL. | §2.5 |
| 5 | Build a deal, before the bridge: a short "Build a deal" block with **Start a deal in Bankable ↗** on candidate sites, retiring and retired plants, projects and open RFPs, only where every source is open- or attribution-class. It opens Bankable's own start page and says that nothing is carried across yet. Get funded: not linked from Inframation at all. | §2.6 |
| 6 | Every current URL keeps working. New: `/site-finder`, the `#deals` section of `/about`. No phase URLs and no new redirects. | §2.7 |
| 7 | Four lanes. The navigation lane goes first; it touches none of the files the home map, site finder and wave 2 lanes hold. | §2.8 |

### 2.2 The phase words: a desk test

The reader is a first-time visitor: a developer or IPP first (2026-10-21), then infrastructure investors
(`docs/51`), utilities, landowners and the press. Nielsen Norman Group's rule for category names is the test:
"Descriptive category names that people understand are better than made-up words or internal jargon", and categories
must not overlap (Loranger and Dykes, "5 Tips for Avoiding Confusing Category Names", 2013, reviewed 2024,
https://www.nngroup.com/articles/category-names-suck/).

| Criterion | Prospect · Originate · Syndicate (owner's) | Find · Build a deal · Fund | **Find · Build a deal · Get funded** (chosen) | Explore · Deals · Capital |
|---|---|---|---|---|
| Plain to a first-time visitor | Low. "Originate" and "syndicate" are finance words; to many readers a "prospect" is a sales lead | High | High | Medium. "Explore" says nothing a nav does not already say |
| One meaning to developers and to investors | No. At a developer or IPP, "origination" is offtake sales: a "Head of PPA Origination" at a renewables IPP is responsible for "Originating, structuring and negotiating long-term PPAs" (https://climatechangejobs.com/jobs/444042150-head-of-ppa-origination-renewables-ipp, retrieved 2026-10-22; other PPA originator postings found the same day read alike, a sample, not a survey). In lending, a syndicated loan is "an arrangement whereby a group of lenders (the syndicate) … provide funds to a borrower" (Ropes & Gray, "Legal Lingo: What Is A Syndicated Loan?", 18 January 2023, https://www.mondaq.com/uk/project-financeppp-amp-pfi/1272364/legal-lingo-what-is-a-syndicated-loan). Neither is "build a deal in Bankable" or "show finished deals to capital partners" | Mostly. "Fund" reads three ways: an investor's "fund this", a developer's "get funding", and the noun "a fund" | Yes. Both later words name the developer's step, and developers are the first customer | "Deals" yes; "Capital" is a thing, not a step |
| Distinct from the section names | Yes | No. "Fund" sits beside "RFPs & funding", whose grants and loans are Find records | Mostly. "Get funded" brushes "funding"; the About section separates public funding notices (listed here) from private capital (in Bankable) in one sentence | Yes |
| Continuity with Bankable | Bankable never prints these words. Its own copy says Analyze, Certify, Route, Fund and "Get Bankable. Get Funded." (Bankable repository, github.com/Tobombadil/bankablehq, `src/components/landing/HeroSection.tsx` and `src/pages/Signup.tsx`, read 2026-10-22) | "Fund" is Bankable's last step | "Get funded" is Bankable's tagline, so the hand-off reads as one journey across two sites | None |
| Tone, given `docs/18` §4 (general solicitation) | "Syndicate" suggests offering participations | Neutral | Neutral. It names the developer's goal, not an offer to investors | Neutral |

**Chosen: Find · Build a deal · Get funded.** The owner's words stay right as internal names: the decisions log,
`docs/18` and code keys keep `prospect`, `originate`, `syndicate`; public pages print `PHASE_LABELS`. **Confidence:
medium.** This is a desk test against stated criteria, not a test with readers. The cheap check is a first-click test
with the beta testers (`docs/65`): show the About section and a record's Build a deal block and ask "where would you go
to put a deal together for this site?". A change is one line in `web/labels.py`.

### 2.3 The navigation at 1440px and at 360px

**Which items.** The rule is the decision row's: a section is in the nav only once its page has something in it.

- **Find** has pages here: Map, Site finder (once it has results), Projects, Companies, RFPs & funding, Infrastructure.
- **Build a deal** has no page on Inframation, before or after the bridge: the deal lives in Bankable. It is an action
  on records (§2.6). A nav link to Bankable's start page would be the same link with no record behind it.
- **Get funded** is Bankable's showroom, funder pipeline and roadshow: signed in and by invitation. A link would take
  almost every visitor to a sign-in wall, and it would read as a public listing of deals seeking investment, which
  `docs/18` §4 rules out until counsel has read it. Not linked.
- **Alerts** tracks records in any phase (the owner's "actively tracking sites under development, operating,
  decommissioning or repowering"), so it follows the Find sections rather than sitting in one.
- **No phase labels.** With one phase in the nav, a label over it adds a word and no choice. A flat row of named
  top-level sections is the pattern `docs/30-design-references` adapts from Stripe's docs (D-16). A three-step
  "Find › Build a deal › Get funded" strip in the header was considered and rejected: two of its three steps would
  leave the site or end at a sign-in, it costs a row on every page, and a step strip above a working screen is a
  marketing device (D-7).

**Order: Map · Site finder · Projects · Companies · RFPs & funding · Infrastructure · Alerts**, then Pricing (only
under `commercial`), Sign in and search, as today. The order of the six is the one the owner let stand on
2026-10-22 ("do what will create the best product"); `docs/16` §3.1's reasons for it (Companies third, as the join
between projects, RFPs and assets) do not change with phases. Site finder goes second because it is the other
map-shaped Find tool, and powered land for large loads is the use the owner will sell first.

**Words.** The six keep their names. The new one is **Site finder** (names table, §1).

**At 1440px**, measured (method below). The header stays 103px; the nav is 723px wide (633px today) and stays on one
row with the search box.

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ INFRAMATION REGISTER · PUBLIC EDITION             ALL N SCHEDULED SOURCES FETCHED ON SCHEDULE    ABOUT & SOURCES │
├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Inframation  Map  Site finder  Projects  Companies  RFPs & funding  Infrastructure  Alerts  Sign in  [Search…]   │
│                   ━━━━━━━━━━━ the current section: weight and underline (aria-current)                           │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
  Find (Prospect): Map … Infrastructure · across phases: Alerts · Build a deal and Get funded: not in the nav
```

**At 360px.** The links sit behind Menu, as today (`docs/31` §3). The open panel is the seven links, Sign in, the
search box and About & sources, in that order. Only Site finder is new. The home page keeps its one-line source
freshness link above the bar (`masthead--keep`).

```
┌──────────────────────────────────┐    ┌──────────────────────────────────┐
│ (home only) SOURCE FRESHNESS LINK│    │ (home only) SOURCE FRESHNESS LINK│
├──────────────────────────────────┤    ├──────────────────────────────────┤
│ Inframation              [Menu]  │    │ Inframation              [Menu]  │
└──────────────────────────────────┘    ├──────────────────────────────────┤
  closed: bar 65px                      │ Map                     41→44 px │
                                        │ Site finder                      │
                                        │ Projects                         │
                                        │ Companies                        │
                                        │ RFPs & funding                   │
                                        │ Infrastructure                   │
                                        │ Alerts                           │
                                        │ Sign in                          │
                                        │ [Search by name, sponsor…][Go]   │
                                        │ About & sources                  │
                                        └──────────────────────────────────┘
                                          open: panel 474px (433px today)
```

**Measured.** Headless Chromium 141 rendering `/login` from this branch through the in-process app, with
`web/static` served as built and no API (so the masthead reads "Source fetch dates"); the Site finder link injected
second; one run per width. Header height in px; the "One row from" row brackets the width from which the header
is a single row.

| Width | Six links (today) | Seven (with Site finder) | Seven, plus a later "Your deals" link in the bar |
|---|---:|---:|---:|
| 1440 | 103 | 103 | 103 |
| 1280 | 103 | 103 | 168 |
| One row from | 1150–1180 | 1230–1260 | 1300–1340 |
| 1080, 1024 | 168 | 168 | 168 |
| 720 | 225 (links one row) | 274 (links two rows) | 274 |
| 390 and 360, Menu open | panel 433 | panel 474 | panel 515 |

What that costs: the fold budgets hold (map top ≤ 450px at 1440×900 and ≤ 600px at 1024×768, `web/test_e2e.py`),
because neither width changes. From about 1165 to 1240px the search box drops to its own row (+65px), and from 720 to
about 770px the links take two rows (+49px). Both are accepted: those desktop and tablet widths are a minority beside
1280, 1366, 1440 and 1536, and hiding the links behind Menu at tablet widths would cost more than the height, because
hidden navigation gets used less on desktop as well as on phones (Pernice and Budiu, "How to Make Navigation (Even a
Hamburger) Discoverable on Mobile", 2016, https://www.nngroup.com/articles/find-navigation-mobile-even-hamburger/).
The frontend lane may narrow the search field at 1080–1279px if the header then stays one row from 1165px; measure
before and after. Phone menu rows are 41px as built; `docs/17` M-10 asks for 44px, which makes the open panel about
498px, ending near 563px on an 800px screen. A later "Your deals" link (§2.6) goes in the masthead's utility row
beside About & sources, not in the bar, because in the bar it moves the one-row threshold to about 1320px.

### 2.4 Where each page sits

| Page | URL | Phase | Reached from | Change |
|---|---|---|---|---|
| Map (home) | `/` (`/map` answers 301) | Find: the one base map, every lifecycle state | Nav "Map", wordmark | Status control covers existing infrastructure (§2.5, C1) |
| Site finder | `/site-finder` | Find | Nav "Site finder", second, once it has results | New (site finder lane). Its results are candidate sites, each with Build a deal (§2.6) |
| Projects | `/proposals`, `/proposals/{slug}`, `/sites/{id}` | Find | Nav "Projects"; record pages mark it `aria-current="true"` | Project pages gain Build a deal (§2.6) |
| Companies | `/organizations`, `/organizations/{slug}` | Find | Nav "Companies" | None. A company is not a deal |
| RFPs & funding | `/opportunities`, `/opportunities/{slug}` | Find | Nav "RFPs & funding" | Open notices gain Build a deal (§2.6) |
| Infrastructure | `/assets`, `/interconnection-points`, their records | Find, including the operating and end-of-life states | Nav "Infrastructure"; two URL tabs | Status control on `/assets` (§2.5, C2); retiring and retired plants gain Build a deal |
| Alerts | `/alerts`, `/alerts/new` | Across phases | Nav "Alerts"; "Follow" and "Save this search" | None |
| Search | `/search` | Across phases | Header search box | None |
| About & sources | `/about` | Reference | Masthead, Menu panel, footer | New section `#deals`: the three phases in plain words (§2.6) |
| Methodology, attribution, reuse, API and feeds, privacy, contact, bot, pricing, submit, sign-in and account | as today | Utility | Footer and page links | None |
| Bankable | `bankablehq.com/assessment` (until the bridge) | Build a deal; Get funded beyond it | The Build a deal block only | Off-site |

**RFPs & funding is a Find list, not the doorway to Originate.** Four reasons. (1) It lists public records discovered
from registers, filtered and sorted like Projects; reading and screening them is prospecting, and the decision row
already names "RFPs & funding" among Prospect's lists. (2) A nav group "Build a deal" whose only item was a list would
promise a deal builder and deliver a table. (3) Funding notices (grants, loans, programmes) are also screening
inputs, beside the incentive layers the "Prospecting layers" row adds. (4) The doorway is the record, not the list:
an open RFP's page is where a developer decides to respond, so that is where Build a deal sits. Bankable's own RFP tab
(`/engage?tab=rfp`, signed in) may later be fed from this list through the bridge (`docs/18` §1); that changes
nothing here.

### 2.5 Operations: the later lifecycle states on the one base map

**What the data holds.** A project's lifecycle runs unknown, announced, filed, studied, permitted, contracted, under
construction, built, withdrawn, cancelled (`data/vocabulary/lifecycle_states.yaml`); there is no "operating",
"retiring" or "repowering" project state. Operating and end-of-life states belong to existing assets: EIA-860M gives
operating 25,625, standby 2,221, retiring 531 and retired 7,324 generator records (`us.eia.860m.retirements`, verified
2026-10-07 in `data/sources.yaml`). Repowering has no state anywhere: it is a new queue request at an existing plant,
which the site rules mark (`expansion_marker`, `services/sites/rules.py`; `docs/21` §3.25). "Decommissioning" is
EIA's retiring and retired; the site uses EIA's words.

**How a visitor reaches each state.**

| State | Record | On the map | In a list | On a record page |
|---|---|---|---|---|
| Under construction | Project | Status: In process (on by default) | Projects, Status | Status chip and timeline |
| Built | Project | Status: Built or operating (off by default) | Projects, Status | Status chip |
| Operating, standby | Plant, pipeline, line | "Existing assets" layer (on by default); C1 moves it into Status | Infrastructure; C2 adds Status: Operating, Standby | Asset page status |
| Retiring, retired | Power plant | "Retired & retiring plants" layer (on by default); C1 moves it into Status | Infrastructure, C2: Retiring, Retired (`/assets?asset_type=power_plant&status=retiring,retired` already answers, with no control on the page) | Asset page; Build a deal (§2.6) |
| Repowering, expansion | A new project at an existing plant | Not filterable yet | Not filterable yet | The plant's Nearby proposals; the project's "At this site" names the relationship |

**C1, the map (after the home map panel lane lands; `home_map.html`, `map.js`).** The Status disclosure becomes the
one lifecycle control. Under its project groups (Announced, In process, Contracted, Built or operating, Withdrawn or
cancelled, Status unknown, unchanged), a second group, **Existing infrastructure**, holds the two layer boxes that sit
outside it today: "Plants, pipelines and lines" (the Existing assets layer) and "Retiring and retired plants". The
summary reads, for the default, "Status: active projects, existing, retiring". The URL grammar does not change: the
project rows write `lifecycle_state`, the infrastructure rows `layer_plants`, `layer_retired` and `asset_type` (D-17).
The floating panel's breakdown uses the same group names.

**C2, Infrastructure (now; `assets_list.html`, `web/assets_pages.py`).** A Status control, Operating · Standby ·
Retiring · Retired, writing the `status` parameter `/assets` already forwards (`ASSET_INDEX_FILTERS`, lane R1). Default:
all statuses, as today.

**C3, repowering (later; API lane).** A filter for projects at an existing plant (a site that holds an operating,
standby or retiring plant, or a member with `expansion_marker`). It needs an API parameter. It is a filter, not a
status and not a section.

### 2.6 Build a deal and Get funded before the Bankable bridge

**What Bankable offers a newcomer today** (its code, github.com/Tobombadil/bankablehq, branch `main`, read
2026-10-22). Its public "Get Started" button goes to `/assessment`, a sign-up flow that asks the visitor's role
(Sponsor, Funder, Counterparty, Advisor, Service) and, for a sponsor, company, sector, location and capex, then
creates the project (`src/pages/Assessment.tsx`). Signed in, its nav is Projects (`/dashboard`, `/manage`, `/show`),
Pipeline for funders (`/fund`) and Community (`/engage`, with an RFP tab). `/projects`, `/marketplace` and `/rfp`
now redirect to `/show` and `/engage` (`src/App.tsx`), so `docs/18` §1's route list and §3's proposed
`/projects/new` intake need checking against the code.

**Decision: shown before the bridge, as a link to Bankable's start page with a plain note.** Hidden was the
alternative. It is rejected because the decision row makes Originate "an action on records" until the hand-off
exists; the link leads to a working page, not an empty one; and the owner wants the loop now. The cost is that the
visitor retypes the facts, and the note says so, so no promise is broken.

**Where.** After the record's key facts (the field grid or summary strip), before the timeline, matches and Sources,
at every width. On a phone it is in the page flow, not sticky. Not in the map drawer: the drawer is a preview, and its
"Open full record" leads to the block.

**Which records.**

| Record | Shown when | First line |
|---|---|---|
| Candidate site (site finder result) | Always, once the finder is public | "Pursuing this site?" |
| Power plant (asset page) | Status retiring or retired | "A retiring plant's site and grid connection can host a new project." |
| Project (proposal page) | Any status except withdrawn and cancelled | "Is this your project?" |
| RFP or funding notice (opportunity page) | Status announced, open or reinstated | "Responding to this?" |
| Company, grid connection point, operating asset, site record (`/sites/{id}`), lists, search, map drawer | Never before the bridge | — |

**Licence rule.** The block shows only when every source behind the record is reuse class `open` or `attribution`.
It is the test the bridge will apply to each field (`docs/18` §3), so the action never appears now on a record it
could not serve later. It also keeps a link to a commercial service off pages built from `noncommercial` rows, which
is the use `docs/26` §3 (ii) and (iii) worry about ("directed towards commercial advantage"). Today it hides the block
on the four AEP utilities' RFPs (class `noncommercial`) and on records from sources whose terms are not yet recorded.

**Anatomy and words** (link mode). A ruled aside, not a card: a section heading, one paragraph, one secondary button
(`docs/31` §5.16: outlined, sentence case), one muted note. No Bankable colours or logo (D-7, D-19). The arrow and
"in Bankable" say the link leaves the site; it opens in the same tab, so Back returns.

```
── Build a deal ─────────────────────────────────────────────────────
Is this your project? Bankable turns it into a deal: a data room, a
bankability score and a route to the funders it works with. Bankable
is run by the team behind Inframation.

[ Start a deal in Bankable ↗ ]          ← 44px tall below 720px, full width

Nothing on this page is sent to Bankable yet. Paste this page's link
into your deal so funders can trace each fact to its source.
─────────────────────────────────────────────────────────────────────
```

The affiliation sentence is there because a page recommending a service run by the same people should say so; the
owner confirms the wording, and the legal lane can confirm it is enough.

**Link and settings.** `BANKABLE_START_URL`, default `https://bankablehq.com/assessment` (Bankable's own "Get
Started"), with `utm_source=inframation&utm_medium=referral&utm_campaign=build-a-deal&utm_content={record kind}`. No
record identifier and no personal data in the URL. `BANKABLE_HANDOFF` is `off` (nothing rendered anywhere), `link`
(this section, the default) or `bridge` (the `docs/18` hand-off: a form post that creates the hand-off record and
redirects to Bankable; the note line goes). In bridge mode a signed-in reader who has started a deal sees **Your deals
in Bankable ↗** in the masthead's utility row.

**About, `#deals`.** One numbered list in the filing idiom: **1. Find**, here: the map, the site finder and the lists,
every fact with its source. **2. Build a deal**, in Bankable: a data room, a bankability score and a route to
funders. **3. Get funded**, in Bankable: it shows finished deals to the funders it invites; public grants and loans are
listed here, under RFPs & funding. Then the affiliation sentence and one link to Bankable. No link to Bankable's
showroom or funder pages.

**Get funded.** Not linked from Inframation, before or after the bridge, until counsel has read `docs/18` §4. After
that, a record may say it is listed in Bankable only with the deal owner's consent (`docs/18` §2).

### 2.7 URLs

- **Every current URL keeps working and keeps its meaning.** No route is renamed or moved: `/`, `/map` (301 to `/`),
  `/proposals`, `/proposals/{slug}`, `/sites/{id}`, `/opportunities`, `/opportunities/{slug}`, `/organizations`,
  `/organizations/{ident}`, `/assets`, `/assets/{slug}`, `/assets/by-id/{public_id}`, `/interconnection-points`,
  `/interconnection-points/{id}`, `/search`, `/alerts`, `/alerts/new`, `/about`, `/methodology`, `/attribution`,
  `/docs/api`, `/feeds/{name}.{fmt}`, `/pricing`, `/submit`, `/login`, `/register`, `/account`, `/privacy`,
  `/legal/reuse`, `/legal/api-licence`, `/contact`, `/bot`, `/sitemap.xml`, `/sitemaps/{n}.xml`, `/robots.txt`.
- **New:** `/site-finder` (the site finder lane; any result permalinks are that lane's, under `/site-finder/`) and the
  `#deals` anchor on `/about`. `/site-finder` joins the sitemap when it is public.
- **Bridge mode only, later:** `POST /handoff` on the site (creates the hand-off, answers 303 to Bankable; disallowed
  in `robots.txt`) and `GET /v1/handoff/{id}` on the API (`docs/18` §3).
- **Redirects:** none new. No `/prospect`, `/originate`, `/syndicate` or `/operations`: a phase is not a page, and a
  redirect would invite links to a section that does not exist. A campaign that names a phase links to `/` or
  `/about#deals`.
- **Parameters:** none new. C1 reuses `lifecycle_state`, `layer_plants`, `layer_retired`, `asset_type`; C2 reuses
  `status`.

### 2.8 Implementation plan for the frontend lanes

**Lane A, navigation (now).** It touches none of the files named below as held by other lanes, except as noted there.

1. `web/labels.py`: add `"site_finder": "Site finder"` to `SECTION_LABELS` after `"map"`, and a new table beside it.
   The comment above `SECTION_LABELS` drops "pending the owner's ratification" and cites the 2026-10-22 row.

   ```python
   #: The development phases (docs/00-PLAN.md 2026-10-22; docs/30 §2.2): the owner's names as keys, the
   #: words a reader sees as values. Printed on /about#deals and in the Build a deal block; never in the
   #: primary nav while only one phase has pages on Inframation.
   PHASE_LABELS: dict[str, str] = {"prospect": "Find", "originate": "Build a deal", "syndicate": "Get funded"}
   ```
2. Jinja globals, registered wherever `section_labels` is today (`web/page.py` line 310, `web/auth.py` 94,
   `web/legal.py` 58, `web/pricing.py` 256, `web/admin/shell.py` 53): `phase_labels`, and `section_live(key)`, one
   helper in `web/page.py` that is true for every section except `site_finder`, which reads the site finder lane's
   switch (proposed name `SITE_FINDER_ENABLED`; the finder lane owns it and turns it on only when the page has
   results).
3. `web/templates/partials/_sections.html`: `href` gains `"site_finder": "/site-finder"`; `primary` becomes
   `["map", "site_finder", "projects", "companies", "rfps", "infrastructure", "alerts"]`; `holds` gains
   `"site_finder": ["/site-finder"]`; the header comment says the names were ratified on 2026-10-22, every primary
   section is in the Find phase, Alerts spans phases, and a section is listed only while `section_live` holds. No
   phase grouping markup.
4. `web/templates/base.html`: the primary loop becomes `{%- for key in sections.primary if section_live(key) %}`; its
   comment "Six destinations" becomes "The sections with something in them, in order (docs/30 §2.3)". No Bankable
   link anywhere in the header.
5. Breadcrumbs: no phase level (a phase has no page, and a `BreadcrumbList` item needs a URL); "Home" stays the
   first crumb. `web/templates/organization_detail.html` line 18 hard-codes `<a href="/organizations">Companies</a>`:
   read `sections.href.companies` and `sections.label.companies`. `web/organizations.py` line 839 hard-codes
   `("Companies", "/organizations")`: read `labels.SECTION_LABELS["companies"]`. A finder result page, if there is
   one, is "Home / Site finder / {name}".
6. JSON-LD `ItemList` names follow the section names (the §1 names table left them "to the lanes that own them"):
   `web/app.py` line 825 "Proposals" and line 982 "Opportunities", `web/assets_pages.py` line 233 "Assets",
   `web/interconnection_points.py` line 210 and `web/organizations.py` line 476 read `SECTION_LABELS` ("Projects",
   "RFPs & funding", "Plants, pipelines & lines", "Grid connection points", "Companies").
7. Tests that pin the nav (found by grepping `web/test_*.py` for the section names):
   - `web/test_layout_states.py` line 672 `PRIMARY` and line 720
     `test_the_primary_nav_is_six_sections_in_order_and_marks_where_the_reader_is`: run with the finder switch off
     (the six, as today) and on (seven, `/site-finder` second and marked `page` on `/site-finder`); slice by
     `len(PRIMARY)`, not 6; drop the count from the name.
   - Same file, line 748 `test_a_link_is_marked_on_its_page_and_on_every_page_its_section_holds`: add
     `("site_finder", "/site-finder", ' aria-current="page"')` and `("map", "/site-finder", "")`.
   - Same file, line 757 `test_a_section_is_named_in_one_place`: "Site finder" and the three phase words appear once
     in `labels.py` and in no template; add `organization_detail.html` to the record templates checked for
     `>Companies</a>` and `organizations.py` to the modules checked for `("Companies", "/`.
   - `web/test_e2e.py` line 2936 `SECTIONS` and line 2948 `test_nav_is_six_sections_with_…`: `SECTIONS` follows the
     switch; the `[:6]` slices at lines 2968 and 3008 become `[:len(SECTIONS)]`; add a 360px run (D-32 since
     2026-10-22) beside the 400px one.
   - `web/test_paid_tiers_posture.py` line 121, `hrefs[:6] == ["/", "/proposals", …]`: the same list with
     `/site-finder` second when the switch is on.
   - `web/test_seo.py` line 547, `item_list["name"] == "Assets"`: becomes "Plants, pipelines & lines"; add the
     `ItemList` name for `/proposals` and `/opportunities`.
   - New: no page's primary nav or masthead links to `bankablehq.com` (Build a deal is not a nav item; Get funded is
     never linked), and no Site finder link renders while the switch is off.

**Lane B, the Build a deal block (after the owner confirms the start URL and the affiliation sentence).** A partial
`web/templates/partials/_build_a_deal.html`; includes in `proposal_detail.html`, `asset_detail.html` and
`opportunity_detail.html`; the `#deals` section in `about.html`; settings and one eligibility helper in `web/page.py`
(record kind, state, the reuse classes of its sources, `BANKABLE_HANDOFF`); one rule set at the end of
`web/static/css/styles.css`. Tests: shown and hidden by record kind, state and reuse class; nothing when `off`; the
UTM parameters and no record identifier in link mode; the note line in link mode only. The site finder lane includes
the same partial in its results once both have landed.

**Lane C, operations.** C2 now (`assets_list.html`, `web/assets_pages.py`). C1 after the home map panel lane lands.
C3 waits for an API parameter.

**Lane D, the bridge** (`docs/18`), later: `POST /handoff`, `bridge` mode, "Your deals in Bankable".

**Files other lanes hold now; these lanes wait for them.**

| Lane holding them | Files | Who waits |
|---|---|---|
| Home map panel | `web/templates/home_map.html`, `web/static/js/map.js` (and probably the map rules in `web/static/css/styles.css` and `web/templates/_map_notice.html`) | C1 |
| Site finder | `web/templates/site_finder.html`, `web/static/js/site_finder.js`, its route module and switch | Lane A reads the switch by name only; the finder lane includes Lane B's partial later |
| Wave 2 pages | The companies and RFP list templates, `web/templates/organizations_list.html` and `web/templates/opportunities_list.html`, with their row partials `partials/_organization_rows.html` and `partials/_opportunity_rows.html` | Nobody touches them. Ask the coordinator whether wave 2 also holds `web/organizations.py`, `organization_detail.html` or `opportunity_detail.html`: if so, Lane A items 5 and 6 for Companies and Lane B's opportunity include wait for it |
| Shared | `web/static/css/styles.css`, `web/templates/base.html` | Lane A needs no CSS. Lane B appends one rule set. Confirm no other lane has `base.html` open before Lane A edits it |

**Docs that need matching edits, outside this lane:** `docs/04` D-16 (names six sections and calls them pending the
owner; becomes seven when the finder ships); `docs/18` §1 and §3 (Bankable's routes as the code has them; `/assessment`
as the natural place to read a hand-off); `docs/26` §3 (iii), whose Bankable row says the workflow "must not be built
at all" under the `noncommercial` posture, against the 2026-10-22 row that lets commercial-reuse fields cross.

### 2.9 Standards check (`docs/04`)

| Rule | This design |
|---|---|
| D-16 one nav on public and Pro; current section marked | Holds. A seventh section on a switch. D-16's text is out of date when the finder ships (above) |
| D-17 every filter in the URL | C1 and C2 reuse existing parameters |
| D-2, D-34 provenance on every data element | The Build a deal block carries no data. Sources panels are unchanged. Under the bridge, every value carries its source, URL, date and licence (`docs/18` §3) |
| D-3, D-28 tier notice | Unchanged; nothing is delayed on any tier since 2026-09-21 |
| D-7 banned patterns | No step strip, no card, no hero, no borrowed Bankable styling |
| D-30 WCAG 2.2 AA | The owner deferred keyboard-only and screen-reader optimisation (2026-10-22). Nothing here removes a route that exists: links stay links, Menu stays a disclosure, the new button is a link with its destination in its name |
| D-32 360px | Menu panel 474px at 360×800 with seven links; no sideways scroll at 360 or 390 on the measured page (`/login`) |

## 3. Page inventory

| Page | Surface | Purpose | PRD stories | API reads | Tier-gated elements |
|---|---|---|---|---|---|
| `/map` (home) | Public/Pro | Map-first browse, filters, in-view list | US-104, US-101, US-102 | `GET /v1/proposals/geo`, `/v1/opportunities` (polygon) | D-3 delay banner (public); exact points withheld for restricted sources (D-9) at any tier |
| `/site-finder` (added 2026-10-22, being built) | Public/Pro | Candidate sites for powered land: corridor bands near 69 kV-and-up lines and substations, outside the FEMA floodplain; later the prospect score. In the nav, second, once it has results (§2.3). Each result offers Build a deal (§2.6) | None yet (decision rows of 2026-10-22) | The site finder lane's | Screening grade labelled on every value; the prospect score stays off until its back-test passes |
| `/proposals` | Public/Pro | Filtered, sorted list | US-101, US-102, US-103 | `GET /v1/proposals` | No age test on any tier since 2026-09-21 (US-101 AC3); gated-source rows never present (AC4) |
| `/proposals/{slug}` | Public/Pro | Canonical record, provenance, timeline | US-201, US-202, US-203, US-204 | `GET /v1/proposals/{id}`, `/events`, `/sources`, `/matches` | US-201 AC4 banner states the page is live (no delay on any tier); Sources panel omits gated rows (D-27); `field_provenance` Pro+ only |
| `/opportunities` | Public/Pro | Filtered, sorted opportunity list | US-301 | `GET /v1/opportunities` | Same tier rules as US-101 AC3–4 |
| `/opportunities/{slug}` | Public/Pro | Record, provenance, status timeline, matches | US-302, US-402 | `GET /v1/opportunities/{id}`, `/events`, `/sources` | Linked documents = title+link only (no article bodies, `docs/02` §4) |
| `/assets` | Public/Pro | Asset index: by-type counts, filters (type, state, name/operator), sort, paged rows | US-104, US-203 | `GET /v1/assets`, `GET /v1/assets/geo` (by-type totals only) | None; no lag |
| `/organizations` | Public/Pro | Company index: name search, what each company holds, paged rows | US-203, US-303 | `GET /v1/organizations`, `GET /v1/organizations/{id}` per row (counts) | None; no lag |
| `/organizations/{slug}` | Public/Pro | Company page: summary line by role and type, map of every asset with geometry, assets grouped by role then type, parent/subsidiaries, proposals, opportunities, sources of those records | US-203 AC1, US-303 | `GET /v1/organizations/{id}`, `/assets`, `/proposals`, `/opportunities`; `GET /v1/assets?organization=` when asset rows carry no geometry | None beyond list-level tier rules; no lag (assets are public-domain / CC BY) |
| `/assets/{slug}` | Public/Pro | Existing-asset page (ADR 0008): fields by type, static map (SVG, MapLibre-enhanced), attributes, owners/operators, nearby exact-grade proposals with distance, sources | US-104, US-203 | `GET /v1/assets?slug=`, `/v1/assets/{id}/nearby-proposals` | None; no lag |
| `/search` | Public/Pro | Free-text + identifier search | US-103 | `GET /v1/proposals?q=`, `/v1/opportunities?q=` | Same as list |
| `/alerts` | Pro | Saved searches, delivery mode, history | US-501, US-502, US-504 | `GET/POST/PATCH/DELETE /v1/saved-searches`, `/v1/alerts` | Pro-only route; entitlement check (US-602) |
| `/account` | Pro | Seats, subscription, keys, export log | US-602, US-603, US-701 | `GET /v1/me`, `/v1/keys` | Pro/API scopes; billing read-through CRM adapter |
| `/docs/api` | Public/Pro | Generated API reference | US-704 | `api/openapi.yaml` (Redoc) | Licence-acceptance gate before key creation |
| `/feeds/*` | Public | RSS/JSON Feed twins of every list | US-503 | Same list endpoints, `.rss`/`.json` | Feed title states "Public feed, live" (D-28) |

**As built, 2026-10-07 (audit UX-6).** `/docs/api` is a site page, not a rendered Redoc: it lists the three public
feeds in both formats with an example filtered feed, names this deployment's API base URL, and links the API's own
live reference (`{API}/redoc`) and OpenAPI description (`{API}/openapi.json`), where `{API}` is `PUBLIC_API_URL` or
the host beside the site (`api.`, `api-staging.`). It is in the footer ("API and feeds") and the sitemap. Each list
links its feed twin with the same filters, the lifecycle or status choice spelt out because a feed has no default
view of its own: "View as: Map · List · RSS feed" on `/` and `/proposals` (below 720px the "View as" text is
visually hidden, the control keeps it as its accessible name, so the three links fit beside "Save this search as an
alert" and the map stays above the fold at 400×800), a link above the rows on
`/opportunities`, and `<link rel="alternate" type="application/rss+xml">` in each page's head. In production Caddy
sends `/feeds/*` to the API; the site's own `/feeds/*` route only relays it where no proxy stands in front
(`dev_up`, the in-process client). Rendering `api/openapi.yaml` itself on the site stays open.
| `admin/sources` | Admin | Source health, cadence, gate | US-904, US-905 | `GET/PATCH /admin/v1/sources`, `/source-runs` | `legal` role required to clear a gate flag |
| `admin/records` | Admin | Edit, merge/unmerge with reason | US-201–202 (admin actions), US-905 AC3 | `PATCH /admin/v1/proposals`, `POST …/merge` | Audit-logged; mandatory reason field |
| `admin/tasks` | Admin | Reports, intake review, deletions | US-907, US-204, US-1002, US-910 | `GET/PATCH /admin/v1/tasks`, `POST …/approve-intake` | — |
| `admin/posts` | Admin | Social review queue | US-802, US-803, US-804 | `GET/PATCH /admin/v1/posts`, `PUT …/auto-publish` | `auto-publish` toggle is owner-role only, default off per channel |
| `admin/users` `admin/customers` | Admin | Roles, entitlement, CRM read-through | US-901, US-902, US-903 | `GET/PATCH /admin/v1/users`, `/customers`, `/subscriptions` | Staleness banner + refused writes if adapter down (US-902 AC3) |

### 3.1 Asset and company pages (added 2026-09-19, midstream slice)

Both pages are server-rendered records in the `/proposals/{slug}` idiom (D-18), reachable from the map drawer
("Open asset page"), the in-view list, `/search` (which now has an **Assets** section beside Organisations, so a
pipeline name and its operator both resolve) and `/sitemap.xml`.

- **Asset page** `/assets/{slug}`: header with type, pipeline class and status; a key/value grid whose rows
  exist only where the source carries the value (the "None" gate: no "None", no "—" rows); for a pipeline —
  operator (linked to the company page through its `operator` edge), class, length, diameter, states crossed,
  status, source and retrieval date; for a plant — technology, capacity, commissioned year, units, county,
  state; for an ethanol plant — nameplate capacity with its unit, feedstock, PADD, capacity as-of year,
  operator; for an RNG project — project type, technology family (landfill gas to electricity / direct use,
  renewable natural gas, farm digester), rated MW and/or LFG flow, biogas end use, host landfill or digester
  type, feedstock, start and shutdown year (second midstream slice, 2026-09-19). Then **Map** (SVG of the geometry with nearby proposals as dots; MapLibre replaces it when scripts
  run), Attributes, Owners and operators, Nearby proposals (nearest first, each with its distance; the
  wording says "route" for a line, "point" for a point), Sources. No map section when the record has no
  geometry. Nearby rows are one per project, not per EIA-860M generator unit: rows sharing (name, sponsor,
  county) collapse to one row with "× n units", the summed capacity and the nearest unit's distance
  (`web/app.py::group_nearby_proposals`; the map's in-view list applies the same key).
- **Company page** `/organizations/{slug}`: header whose badge, for an organisation typed `other` that holds
  assets, is a descriptor derived from the holdings ("Ethanol producer", "Gas pipeline operator and Power plant
  owner"; the raw type stays in the fields table), a one-line summary "Operates 3 gas pipelines · Owns 12
  power plants" (from the API's `asset_counts`, else counted over the page's rows), fields present only where
  set, parent and subsidiaries where the API embeds them, **Assets** (map of everything with geometry, then one
  table per role → type group with the columns that group fills: length and states for pipelines, capacity and
  share for plants), Proposals, Opportunities, and **Sources** listing the distinct registers behind the
  organisation's assets, proposals and opportunities — an organisation row carries no sources of its own, and
  the page never shows the "withheld under licence" empty state for that case; with no sources anywhere it shows
  no panel at all. **Follow (2026-10-07):** under the summary, "Follow this company's proposals" (and "Follow its
  opportunities" where it issues any) opens `/alerts/new` with `sponsor_id` (or `issuer_id`) set to the
  organisation's **public id** and a name ("Proposals sponsored by …"); an alert saved with a slug matched nothing
  while the id matched 157, so the alert form also resolves a slug to the id before saving. Not offered on the page of
  a natural person named in a register.
  **Pipeline (as built 2026-10-10, owner: "check a developer's or owner's pipeline with a source for every claim").**
  Under the fields and scope links, before the portfolio and the numbered sections, a **Pipeline** block: one line
  (proposals sponsored at this scope, how many in the active pipeline, their MW), then three small tables, **By
  status** (the six status groups of §4.1, each with its chip icon, plus "All statuses"), **Active pipeline by
  technology** and **Active pipeline by ISO / operator** (announced, in process or contracted only), each row a count
  and MW. Every count links to `/proposals` filtered to the same rows: `sponsor_id` (the company's id, or every
  sponsor id behind the rows when the scope includes subsidiaries, since `sponsor_id` matches exact organisations),
  plus `lifecycle_state`, `technology`, `iso` or `source_id`; the technology and ISO links name no status because the
  active pipeline is the list's default view. Checked on the e2e store: every count on two company pages equals the
  count its link opens. MW sums generation and storage only; load, transmission, pipeline, LNG, CCS and hydrogen
  records are counted and named in a note, never added (a data centre's MW is demand). "—" where no record states
  capacity. Under the tables, the registers behind the rows with how many proposals each backs and its latest
  retrieval date, each linking to the list filtered by that source, and a link to the Sources section (`#sources`)
  for licences. Built only from `GET /v1/organizations/{id}/proposals`, read page by page (200 a page, at most five;
  past 1,000 the block says it is partial). The Proposals section still lists 100 and now says "The 100 most recently
  changed of N; all of them in the list" when there are more (`web/org_pipeline.py`,
  `partials/_org_pipeline.html`). Gaps for the API lane: no `include=count` and no aggregate (counts and MW by status,
  technology, ISO) on that endpoint, so a large sponsor costs up to five reads; `iso` holds EIA balancing-authority
  codes for EIA-860M rows (`ERCO`, `NYIS`, `CISO`, `SWPP`) beside ISO names (`ERCOT`, `NYISO`), so one grid can appear
  as two rows.
  **As built, 2026-10-10 (lane P), closing those gaps.** The block is one read, `GET /v1/organizations/{id}/pipeline`
  at the page's scope, and is never partial. Its links carry the API's `list_query`: one `sponsor_id`, plus
  `sponsor_scope=children|all` when the scope spans subsidiaries (no longer every subsidiary's id). The active status
  groups count only records some register still lists (`listed=true`); a record every register has dropped keeps
  its last status but sits in its own **No longer listed** row (`listed=false`), and the opening line says how many
  there are and that they are not counted as active. Built, withdrawn and unknown count every record in those states.
  The technology and ISO tables cover the listed active pipeline. A grid is one row under its ISO's name: `iso=ERCOT`
  also opens the `ERCO` rows. `tests/test_api_org_pipeline.py` holds every link against the real API at each scope.
  The 6, 15 and 17 links of the fixture company's self, children and all scopes each open exactly their count.
  `/proposals` filtered to one company, its group or one connection point is headed "Proposals sponsored by <name>
  (and its subsidiaries)" or "Proposals connecting at <point>", with a link back. The filter form keeps that subject.
  A view an alert cannot watch yet (`sponsor_scope`, `listed`) offers no "save as alert". A record page whose record
  no register lists says "No longer listed in any register since <date>" under its status (`web/list_subject.py`,
  `proposals_list.html`, `proposal_detail.html`).
- **Map** (`/`): the "Existing assets" toggle now carries an **Asset types** checkbox set (power plants, gas
  pipelines, gas processing, gas storage, LNG terminals, ethanol plants, RNG projects), written to the URL as
  `asset_type=` csv. Ethanol capacity-table plants (state grade) and AgSTAR digesters (county grade) are never
  points and are not returned by `/v1/assets/geo`; the legend says so and the lists carry them. The in-view list gains labelled groups for **Regions** (links to the filtered list) and
  **Existing assets** (points and lines; each row links to the asset page and has a Details button that opens
  the same drawer a map click does), so both are keyboard-reachable (audit finding 2026-09-18).

### 3.2 Discoverability: index pages, machine-readable meta, robots and sitemap (added 2026-09-19)

Closes the `docs/50` §3.2 web bullet ("no Open Graph or structured data") and item 4 of the 2026-09-19 plan.
The thesis it serves is `docs/50` §4.4: the asset map and the crawlable asset and company pages are the
acquisition surface; proposals are the paid product. A page nobody can find, and a link that pastes into a chat
as a bare URL, are both failures of that surface.

**Index pages.** `/assets` and `/organizations` are the crawl entries to the record pages that previously had
none — before this, an asset page was reachable only from the map drawer, `/search` or the sitemap, so a crawler
following links found none of them, and neither did a reader without JavaScript. Both mirror `/proposals`
exactly: the same `.filter-bar`, the same `.record-table` in `.table-scroll`, the same `pager` macro on cursor
pagination, the same `.empty-state`. Both are in the primary nav, as **Assets** and **Companies**.

- `/assets` — a row of by-type count links above the filters (largest type first, plus an "All types" total),
  then filters for type, state and a name/operator query, and a sort control. Sorting is bounded by the API's
  allowlist (`last_changed`, `first_seen`, `name`, `capacity_mw`): capacity descending is the default, but a
  view filtered to line assets alone defaults to name, because a pipeline carries no `capacity_mw` and
  `length_miles` is not a sort field — its mileage shows in the row and orders nothing. The counts come from
  `GET /v1/assets/geo`'s `totals.asset_type_counts` (the one call that counts by type; `GET /v1/assets` has no
  `include=count`), whose denominator is assets with a published location. That is a smaller set than the list
  — an asset whose licence forbids raw publication keeps its row and page but is off the map — so the page says
  so in a line under the counts rather than implying the two agree. Measured 2026-09-19: 17,352 counted against
  18,055 listed.
- `/organizations` — a single name search (`GET /v1/organizations`'s `q` is a `name_canonical` substring;
  promising more would be a filter the API cannot honour), and per row the same descriptor and holdings clauses
  the company page shows, from the same helpers (`org_descriptor()`, `_org_type_counts()`,
  `_org_summary_parts()`). List rows carry no `asset_counts`, so each row costs one `GET /v1/organizations/{id}`;
  the page is 25 rows to bound that at 26 calls. **Ask of the API lane:** `asset_counts` on the
  `GET /v1/organizations` list row removes the second call and lets this page grow.

**Meta on every public page.** Title, description, `rel=canonical`, `og:site_name`, `og:type`, `og:title`,
`og:description`, `og:url`, `og:locale`, and `twitter:card|title|description`. Title and description have one
source each — the template's own `title`/`meta_description` blocks, re-read in `base.html` — so a description is
always built from the record the API returned, never a constant, and cannot leak a field the visibility gate
withheld (the gate runs before the template sees the row). `og:image` is the site's own 1200×630 card,
`web/static/img/og-card.png`, drawn by `python -m web.og_card` (docs/31 §5.15); a third-party image URL is not
permitted. Canonical URLs are absolute and carry only the parameters the page
understands, so `?utm_source=` collapses to the clean URL and two orderings of the same filters agree. Session
and result pages carry `noindex` (`/login`, `/register`, `/verify`, `/account`, the privacy and unsubscribe
forms, `/search?q=`); the bare `/search` stays indexable because it is in the sitemap.

**Structured data.** `BreadcrumbList` on every record page (proposal, opportunity, asset, company);
`Organization` on a company page — name, URL, the identifiers actually stored (`lei`, `sam_uei`,
`eia_utility_id`, `cik`, `duns` as `PropertyValue`), `sameAs` for a stored website, `addressCountry`,
`parentOrganization` — with every absent field dropped rather than emitted empty; `ItemList` on each index and
list page, naming the rows that page renders, with `numberOfItems` only where the count is known and exact.
Every block is serialised with `<`, `>` and `&` escaped to `\uXXXX`, so a record named
`Acme "Big" <Energy> & Co </script>` can neither close the script element nor reach the document as markup.

**robots.txt.** None existed. Public pages allowed; `/admin`, `/api/` (this site's own same-origin relay, not
the documented API), the session routes, `/health` and `/search?` (the query form only, to bound crawl space)
disallowed; `Sitemap:` absolute, from the request's base URL.

**Sitemap.** Assets and organisations were already in the builder but capped at 5,000 URLs each, which
truncated both. The per-resource cap is now 30,000 URLs, and above 25,000 URLs in total `/sitemap.xml` becomes a
`<sitemapindex>` over `/sitemaps/{n}.xml` — half the protocol's 50,000-URL limit and about 2 MB against its
50 MB. Measured on the full dev load, 2026-09-19: 34,692 URLs (18,055 assets, 10,409 proposals, 5,513
companies, 707 opportunities, 8 static pages) in two files of 25,000 and 9,692, a 20-second cold build and
8 ms thereafter. The existing per-base-URL cache now holds the whole build rather than one file, so a crawler
fetching the index and every chunk still costs one walk.


## 4. Core-job flows

### 4.1 Find proposals by geography and filters (US-101, US-102, US-104)

```mermaid
flowchart TD
  A["Land on / (map, default filters)"] --> B{"Refine?"}
  B -->|"set filters"| C["Filter bar: kind, technology, lifecycle_state,\njurisdiction, capacity range, dates (US-102 AC1)"]
  C --> D["URL updates: ?technology=&jurisdiction=&bbox= (D-17)"]
  D --> E["Map + in-view list re-render together (D-4)"]
  B -->|"switch view"| F["Toggle Map / List / Feed — same filter state carried (US-104 AC2)"]
  F --> E
  E --> G{"Result count"}
  G -->|"0"| H["Empty state: names the filter that emptied it,\noffers Clear all (US-102 AC4, D-29)"]
  G -->|">0, over cluster threshold"| I["Clustered glyphs by lifecycle+technology (D-10)"]
  G -->|">0, under threshold"| J["Individual focusable markers"]
  I --> K["Hover/focus cluster: breakdown tooltip, no click needed"]
  J --> L["Select marker/cluster row → drawer (D-12)"]
  K --> L
  L --> M["Drawer: fields, chip, Sources, last 3 events, D-3 line, link to full record"]
```

**Uncovered ISO areas (2026-10-07, expert review).** A list or map filtered by `iso=` for an ISO whose own queue has
no rows here (PJM, MISO, SPP, ISO-NE today) says so above the rows: "PJM's own interconnection queue is not covered
here. The proposals listed for PJM come from EIA-860M, an inventory of planned and operating generators in its area,
not from the queue." It is derived from `/v1/coverage`'s loaded sources and the manifest, so it disappears when a
queue starts loading (`web/coverage_statement.py`). The same inputs write `/pricing`'s coverage line, which claimed
"every major US interconnection queue" (docs/41's sentence; docs/41 itself is for its owner to correct).

**Large-load views (2026-10-07, lane L11).** No load record carries MW (0 of 578 live), so a `kind=load` or
`technology=load` list sorts by **name, A to Z** by default instead of capacity descending, and has no Capacity
column; the map drawer drops the Capacity row for a load with no MW (the in-view row already printed none). Name was
chosen over "last changed" because every record's `last_changed` is currently its load date, which orders nothing;
revisit once change events flow. `/methodology` explains why there is no Texas large-load request: ERCOT publishes its
large-load queue only as system-wide charts in PDF decks (`data/vocabulary/coverage_notes.yaml`,
`ercot_large_load_not_request_level`), and drops the note by itself once the ERCOT large-load source has a
published row (`applies_to.source_without_published_rows`).

**Status control, as built 2026-10-07 (audit UX-1).** The `lifecycle▾` filter of §5.1 is a "Status: active"
disclosure on both the map and the list bar (one macro, `status_filter`), with one checkbox per group: Announced,
In process, Contracted (the default, `ACTIVE_PROPOSAL_STATES`), Built or operating, Withdrawn or cancelled, Status
unknown. It replaces the "Include withdrawn & cancelled" box. The URL keeps the docs/23 grammar and its shortest
form: nothing for the default, `include_withdrawn=1` for the default plus withdrawn, otherwise one
`lifecycle_state` csv; a list form that sends one `lifecycle_state` per box is read as their union. The default view
is unchanged, and leaving the boxes as they are is still the default (the notice stays). The notice now counts built
and unknown-state proposals apart and links the built count to the same view with Built added (on the map it ticks
the box in place). Before, the 1,844 built proposals (484 of them operating data centres) were reachable only by
typing `?lifecycle_state=built`.

### 4.2 Open a proposal, read provenance and lifecycle (US-201, US-202, US-203)

```mermaid
flowchart TD
  A["Arrive from list, map drawer, search or feed link"] --> B["/proposals/{slug} (US-201 AC1)"]
  B --> C{"Tier"}
  C -->|"Public"| D["Record as it stands, full timeline;\nbanner: published as soon as ingested,\nalerts and API in Pro (US-201 AC4, D-3)"]
  C -->|"Pro"| E["Live record, field_provenance exposed (D-27)"]
  D --> F["Sources panel: name, retrieved_at, licence badge,\nlink out; gated sources omitted not greyed (D-27, US-201 AC1)"]
  E --> F
  F --> G{"Source is restricted/attribution-raw-withheld?"}
  G -->|"yes"| H["Derived fields shown; 'view at source' link;\nno raw row rendered (US-201 AC2)"]
  G -->|"no"| I["Full derived+identifying fields"]
  B --> J["Lifecycle timeline, newest first (US-202 AC1)"]
  J --> K{"Event type"}
  K -->|"status_change"| L["before → after chips"]
  K -->|"merged/unmerged"| M["Labelled, links to absorbed record (US-202 AC2)"]
  B --> N["Sponsor org link (US-203 AC1)"]
  B --> O["Matched opportunities with score+rationale, or none shown (US-203 AC2)"]
  B --> P["Report a problem form → admin task, no auto-email (US-204)"]
```

### 4.3 Track changes via saved search and alerts (US-501–504)

```mermaid
flowchart TD
  A["Pro user on a filtered list/map/feed"] --> B["Save search action"]
  B --> C{"Under 25 saved searches? (US-501 AC1)"}
  C -->|"no"| D["Refused: at limit, manage existing"]
  C -->|"yes"| E["Name it; store filter definition, not results (US-501 AC2)"]
  E --> F["Choose delivery: immediate / daily / weekly (default daily, US-502 AC1)"]
  F --> G["Matcher runs on new events; watermark advances (docs/21 §3.15)"]
  G --> H{"New matches since watermark?"}
  H -->|"no"| I["No email sent"]
  H -->|"yes"| J["Immediate: batched ≤1 email/15min window (US-502 AC2)"]
  H -->|"yes"| K["Daily/weekly digest at scheduled time"]
  J --> L["Email: each event, link to detail, source credit,\nunsubscribe link + sender identity (US-502 AC2-3)"]
  K --> L
  L --> M["Delivery logged: alert id, event ids, sent_at,\nprovider message id (US-502 AC4)"]
  A --> N["/alerts settings: edit, pause, delete (US-504 AC1)"]
  N --> G
```

### 4.4 Browse opportunities and see matches (US-301, US-302, US-401, US-402)

```mermaid
flowchart TD
  A["/opportunities, default status=open sort due_at asc (US-301 AC2)"] --> B["Filter: kind, issuer, due date, technology (US-102 shape)"]
  B --> C["List: kind, issuer, title, jurisdiction, technologies,\ncapacity/budget, open_at, due_at, status, source (US-301 AC1)"]
  C --> D["Open detail (US-302 AC1)"]
  D --> E["Full fields, Sources panel, status timeline\n(frozen→cancelled→reinstated as events, AC2), linked docs (title+link)"]
  D --> F["On a proposal detail, matched opportunities list (US-203 AC2)"]
  F --> G["Score + plain-language rationale\n('storage, TX, 50-500MW, due in 45 days') (US-402 AC1)"]
  G --> H{"Pro user dismisses?"}
  H -->|"yes"| I["Dismissal stored per-user; global match unchanged (US-402 AC2)"]
  H -->|"no"| J["Match stays visible"]
  G --> K["Admin: create lead action → CRM via adapter (US-403 AC1)"]
```

### 4.5 Submit a project — light intake (US-1001, US-1002)

```mermaid
flowchart TD
  A["Developer clicks Submit on /proposals or /opportunities"] --> B["/submit form: name, kind, technology, capacity_mw,\nstorage_mwh, jurisdiction, state, county, self-declared\nlifecycle_state, optional identifiers, sponsor org,\ncontact name+email, description ≤2000 chars, consent (US-1001 AC1)"]
  B --> C["Submit"]
  C --> D["Creates proposal: created_by=user, publish_state=pending_review;\ninvisible on public/Pro/API (US-1001 AC2)"]
  D --> E["Contact stored on task + CRM only,\nnever on organization (US-1001 AC2, docs/21 C-7)"]
  D --> F["Admin task created (US-907)"]
  C --> G["Submitter gets one transactional confirmation email,\nno other automated outbound (US-1001 AC3)"]
  F --> H["Admin reviews task (US-1002 AC1)"]
  H --> I{"Decision"}
  I -->|"link to existing"| J["Resolution suggestions shown, same keys as resolver"]
  I -->|"approve as new"| K["Proposal created"]
  I -->|"reject"| L["Reason recorded"]
  J --> M["Matches computed, CRM lead created (US-1002 AC2, US-403)"]
  K --> M
  M --> N["Submitter sees approved record via private link;\npublic only if opted in AND admin sets public (US-1002 AC3)"]
```

**As built, 2026-10-09 (lane C, `web/submit.py`, `web/templates/submit.html`).** These are the choices this
flow did not settle.
- **Entry.** One form at `/submit` for proposals (US-1001). It is linked from the page header of `/proposals`
  ("Know of a project that is not listed?") and of `/opportunities` ("Developing a project? ... we match it
  against opportunities like these"), which follows this flow's first node. US-1003's opportunity form is not
  built, so IA-3's utility framing from `/opportunities` is still open. It is not in the primary nav or the
  footer. `GET /submit` is indexable and in the sitemap; every POST result is `noindex` and `no-store`.
- **Fields.** Input names are the `IntakeProposalRequest` keys. The two nested objects use dotted names
  (`contact.email`, `identifiers.eia_plant_id`), which are also what the API's `errors[].field` uses.
  - Identifiers. "Interconnection queue" is a select of the seven ISO tokens plus "Not in an ISO queue, or
    not sure". With "Queue position ID" it becomes `queue_ids: [{iso, id}]`, and `iso` stays empty when the
    submitter is unsure, which is a shape the resolver already reads. "EIA plant ID" (digits) becomes
    `eia_plant_id`. "FERC docket" becomes `ferc_dockets`; a state docket goes in the description.
  - Sponsor is a name only. There is no organisation picker, and `sponsor_org_id` is never sent.
  - Jurisdiction is a code typed as the list filter takes it (`US-TX`, `US`, `GB`), upper-cased before sending.
    State and county are optional free text.
  - Technology is optional. Its options are the register's own (`/v1/meta/vocabularies`), so a submitted token
    is one the list can filter by.
  - Status is a self-declared select with Unknown last, linked to `/methodology#lifecycle`.
  - Capacity and storage are text inputs with `inputmode="decimal"`, not `type="number"`. A browser sends an
    invalid number input as empty, which would drop "1,200 MW" without a word; a text input lets the page
    say what is wrong.
  - Every answer is required unless its label says "(optional)". The publication opt-in is unticked by
    default.
- **Checks before sending.** The API admits five intake or report requests per visitor address per hour, and
  it stops at the first bad field. So the page checks every field the schema constrains, lists every problem
  at once in an error summary that has focus and links to each field, and spends none of the allowance on
  answers it can already see are wrong. Whatever the API still refuses lands on the field it names (422).
  - Textarea line breaks are counted as the browser counts them.
  - Nothing typed is lost on any error: 422, 429, an unreachable API (502) or a refused captcha. The honeypot
    is never echoed back.
- **Consent.** The consent box links the privacy notice only, because the site has no terms page.
  `consent_version` is `privacy-notice-<the notice's date>`.
- **Success.** The page says what happens next: an editor reviews it, it appears nowhere until then, it is
  linked, added or declined, then matched, and published only if the submitter opted in. It shows the task
  reference. It says no confirmation email is sent, because US-1001 AC3's email is not built. It does not
  link a private status page, because US-1002 AC3's `status_url` (`/status/{task_id}`) is not served. Both
  are recorded in `docs/40` §6 item 1.

### 4.6 Admin: source gate, publish decision, post queue (US-905, US-802)

**Report → record → unmerge (2026-10-07, audit UX-3).** A reader's report lands in `admin/tasks` with its issue type
in words ("Two different projects were merged into this record") and a subject link to the record's admin page by
kind (`/admin/records/{proposals|opportunities|organizations}/{id}`; the screens used to link `/admin/records/{id}`,
which is no route). The task page names the record, links its public page and, for a `wrong_merge` report, its
merge history and the unmerge form on the record page (`#history`, `#unmerge`). A privacy request's "Record named"
links the same way.

```mermaid
flowchart TD
  A["admin/sources — health list (US-904 AC1)"] --> B["Select source"]
  B --> C{"Attempt publish-state = public/api_only"}
  C --> D{"reuse ∈ {open, attribution} AND legal evidence\nrecorded AND gate flag clear? (US-905 AC1)"}
  D -->|"no"| E["422 gate_unmet — refused, names the unmet gate"]
  D -->|"yes, gate present (PJM/MISO default)"| F["Only legal role can clear gate:\nevidence URL, date, classification (docs/21 §8)"]
  F --> C
  D -->|"yes, no gate"| G["Publish-state transitions; records/events\nvisible within one cache TTL"]
  A --> H["admin/posts — review queue (US-802 AC1)"]
  H --> I["Draft generated from qualifying event (US-801 AC1);\nnever from gated/restricted record (US-801 AC3)"]
  I --> J{"Channel auto-publish on? (default off, US-802 AC2)"}
  J -->|"no"| K["Operator: approve / edit-then-approve / reject (reason) / schedule"]
  J -->|"yes, owner enabled"| L["Publishes without human review; still gate-checked"]
  K --> M["Approved: published via channel API,\ncredit line + disclosure baked in (US-803 AC1-3)"]
  M --> N["post id, published_at, metrics stored;\nUTM attributes sign-ups (US-803 AC2)"]
```

## 5. Wireframes — five key screens

Structured-text wireframes, not pixel layouts; component names match `docs/31-design-system.md` §5 inventory.
Grid and breakpoints per `docs/30` §5 (400/720/1080/1440px, D-32).

### 5.1 Map-first home (`/`)

```
┌───────────────────────────────────────────────────────────────────────┐
│ Inframation     Proposals · Opportunities · Map · Feed · Alerts      │ nav, identical all tiers
│ D-3 banner: "Every record and change event live. Alerts and API in Pro" │ fixed, public tier only
│ Filter bar: kind▾ technology▾ lifecycle▾ jurisdiction▾ capacity– –     │ D-19 tokens, URL-bound (D-17)
│ View: [Map] List Feed              1,834 in view                       │
│ ┌─────────────────────────────────────┬─────────────────────────────┐│
│ │ [MapLibre canvas]                    │ In view (142)               ││
│ │  ● cluster: size=count, colour=chip  │ chip│name    │state│MW      ││ synced list = accessible
│ │    family, icon=technology (D-10)    │ Prog│Gemini… │NV   │690     ││ path (D-14)
│ │  ▲ county-centroid marker, labelled  │ Neut│…       │TX   │—       ││
│ │  ▦ opportunity service territory     │ "Unplaced (12)" ▾           ││
│ └─────────────────────────────────────┴─────────────────────────────┘│
│ Legend (on-screen, D-10): ● Neutral ● Progress ● Committed ● Success  │
│ ● Danger [icons]     Basemap © OpenStreetMap contributors, ODbL (D-13)│
│ Attribution: "Sources: CAISO; ERCOT; EIA (public domain)." (D-27)     │
└───────────────────────────────────────────────────────────────────────┘
```
**As built, 2026-10-10 (review `docs/51` §2.6 item 2: the first viewport explained lateness before value; the key sat
below the fold).** Under the H1 (unchanged, `map_heading()`), one statement of value for the reader, then the
instruction: "Projects from public grid-queue and permit filings in the US and GB, matched across registers: every
claim linked to its source, every status change dated, and alerts when something you follow moves. Click a cluster to zoom
in, or a dot to open its record." (before: "Each dot is a proposal filed with a public grid queue or permit register
in the US or GB. Click a cluster…", then the tier notice). The map page has no tier notice block any more: how
current the sources are is said once, by the masthead link ("N of M sources behind their fetch schedule", to
`/methodology#vintage`), which this page keeps below 720px (`masthead--keep`, edition stamp hidden) where every other
page drops it; lists and records keep their notice. The dev-preview label still shows (`preview_banner` macro). The
key is drawn on the map: a `.map-key` row in the map overlay under the zoom and region controls, top left, where the
default view has ocean rather than the dense Northeast (a top-right panel hid New York and New England): from 720px a
17rem panel holding the status key, the region row and, when their layers are on, the asset and retired keys
(scrolls past the map's height); below 720px a strip of short labels ("In process"; the full words stay in the
accessible text). Page ground behind the words, so each hue keeps its measured contrast on `--bg`. A "Key" button
(map.js; hidden without scripts, when the key simply stays open) hides and shows it. Measured in headless Chromium on
the e2e store, dev-preview banner on as in the e2e server: map top 432 → 409 px at 1440×900 (budget 450), key top
1,070 → 470 px; 601 → 592 px at 400×800 (budget 640), key 1,073 → 649 px; axe (WCAG 2.2 AA tags) 0 violations on `/`
in both themes at both sizes. Open: with layers on, the panel covers the West Coast at the default view until "Key"
is pressed; at 720–1079px the expanded filter bar still puts the map at about 760 px.

**As built, 2026-10-10 (lane F), closing both.** With the existing-asset or retired layer on, the key is compact
(`docs/31` §5.17). The status key becomes the strip of short words, each layer's key is a section that closes, and the
panel stops at 15rem and scrolls inside, as a tab stop named "Map key". From 720 to 1079px the map's filter bar is
the "Filters (N active)" disclosure it already is below 720px; list pages keep theirs open. Measured as lane H did
(headless Chromium 141, a copy of the e2e store, dev-preview label on, app in process):

| | Before | After |
|---|---|---|
| Key panel, 1440×900, both layers on | 272×554 px, 150,688 px², 23.2 % of the canvas; Seattle, Portland, Sacramento, San Francisco, Los Angeles, San Diego under it | 272×240 px, 65,280 px², 10.0 %; Seattle, Portland (196 px tall with both sections closed) |
| Key panel, 1440×900, no layer | 272×276 px, 75,140 px², 11.6 %; Seattle, Portland | unchanged |
| Map top, 1024×768 | 634 px | 554 px |
| Map top, 768×1024 / 720×900 / 1079×800 | 741 / 761 / 577 px | 661 / 680 / 497 px |
| Map top, 1440×900 / 400×800 | 409 / 616 px | unchanged (budgets 450 / 640) |

At 1024×768 the budget is 600 px. At these widths the page is one column, as at 400px, so the 400×800 budget's
rule applies: keep 160 px of canvas (the zoom row and the key's first rows) in the first viewport, and 768 − 160 =
608, rounded down to 600. The measured 554 leaves 46 px, about the headroom lane H's budgets had. `web/test_e2e.py`
asserts it. axe-core 4.13.0 (WCAG 2.0–2.2 A and AA tags) in light and dark at 1440×900, 1024×768 and 400×800, in six
states each (no layer; both layers; retired only; both sections closed; Filters open, with and without layers):
0 violations in 36 runs. Before, the panel had a serious `scrollable-region-focusable` violation (it scrolled with
nothing a keyboard could reach) at all three sizes in both themes when layers were on.

Still open:

- At 1024×768 the header takes 233 px in three rows (wordmark, links, search). Collapsing it behind "Menu" up to
  1079px, as below 720px, would put the map at 432 px (measured with the rule injected; 490 px at 768×1024). That
  is a site-wide change and was not made.
- With "Existing assets" on, the asset-type row moves the map to 528 px at 1440×900 (budget 450).

**As built, 2026-10-11 (owner decision: records no register lists any more are hidden by default on the map and the
list, with a toggle; `web/unlisted.py`).** A project every register has dropped keeps the status its register last
stated (a removal is not a withdrawal), so it read as active pipeline. The rule: a view whose lifecycle choice is made
only of active states (the default, or any subset of announced through under construction) asks the API for
`listed=true` on every read it makes: the list's rows, the map's geo and cluster reads through `/api/proposals/geo`,
the lifecycle notice's active count and the empty-state facet counts, so "Showing N active proposals", the count line
and the rows are one set. A view that names any other state (built, withdrawn, cancelled, unknown, or "include
withdrawn") hides nothing, and a link that sets `listed` itself (a company page's "No longer listed" row) keeps it.
The toggle is `unlisted=include`, a site-only parameter never sent to the API; it is a box at the foot of the Status
disclosure on both bars ("Include proposals no register lists any more"; the summary then reads "active, no longer
listed"; a note says the box has no effect while a status outside the default is ticked). "View as: Map · List"
carries it both ways, `map.js` keeps it in the URL, "Clear all" clears it. Under the lifecycle notice, while the view
hides some, one line says how many with a link that shows them: "335 proposals no register lists any more are hidden.
Show them" (on the map the link ticks the box in place). Unchanged: "Save this search as an alert" stays on the
default view (the hiding is not in the query string) and an alert still matches every record, since a departure is
what a follower wants; the RSS and JSON feeds keep every record; the company pipeline and record pages already say
"no longer listed". The list offers no CSV or JSON export link (exports are the signed-in `POST /v1/exports`); a
future one must be built from the list's own API query.

The count costs one `limit=1&include=count` call, as each notice bucket does. Measured on a SQLite copy of the e2e
store with 335 of 4,014 active records' links marked gone (in-process app, median of 15): that call 70–79 ms. On the
list it adds no call: the default view's own total is the notice's active count, which is no longer asked for
separately (rows plus four counts, as before); `/proposals` 416–419 → 434–459 ms, most of it the `listed` predicate
the rule needs anyway. On the map it is a fifth count: `/` 316–323 → 416–436 ms, the notice fragment map.js fetches
after a filter change 248–274 → 356–363 ms (it runs beside the geo read). With the line, the map starts 19 px lower
(1440×900 409 → 428 px, 1024×768 554 → 573, 400×800 616 → 635; budgets 450, 600 and 640); the e2e store holds no
dropped record, so its pages show no line. axe-core 4.13.0 (WCAG 2.0–2.2 A and AA, best practice) on `/` and
`/proposals`, toggle off and on, Status open, light and dark, 400×800 and 1440×900: 0 violations in 18 runs.

States: empty — zero markers, filter bar names the facet + Clear all (D-29). Loading — skeleton tiles + list rows,
no spinner. Error — RFC 9457 `title`/`request_id` in place of the map; list loads independently. Delayed-tier —
D-3 banner, never dismissible on public. Restricted-precision — centroid marker labelled "county level (source
licence)", same wording in the drawer (D-9). Unplaced — "Unplaced (N)" collapsible row, never omitted (D-8).

### 5.2 Proposal detail with drawer variant (`/proposals/{slug}`, and the drawer opened from `/map`)

```
Full page (server-rendered, D-18):                    Drawer (from a map marker):
┌───────────────────────────────────────────────┐     ┌─────────────────────────┐
│ Home / Proposals / Gemini Solar     [breadcrumb│     │ [×] Gemini Solar + Storg│ role="dialog"
│ Gemini Solar + Storage  [chip: Progress·studied│     │ chip: Progress · studied│ focus trap (D-12)
│ Updated 12d ago (live tier) · [Live in Pro →]  │     │ D-3 line if public tier │
│ ─────────────────────────────────────────────│     │ fields · Sources ·      │
│ Fields (2-col, tabular-nums): kind, technology,│     │ last 3 events only      │
│  capacity_mw, jurisdiction, sponsor [link]     │     │ (condensed)             │
│ ─────────────────────────────────────────────│     │ [Open full record →]    │
│ Sources (D-27): CAISO Public Queue Report ·    │     └─────────────────────────┘
│  retrieved 11 Sep · [attribution]; derived     │
│  fields shown, raw withheld, [view at source→] │
│  (US-201 AC2). PJM row absent, not greyed (D-27│
│  item 3)                                        │
│ ─────────────────────────────────────────────│
│ Timeline (newest first, D-26): 2026-08-20      │
│  status_change filed→studied CAISO [licence];  │
│  2026-03-04 created CAISO [licence]            │
│ Matched opportunities: none shown (omitted,    │
│  not "0 found", US-203 AC2)                    │
│ [Report a problem →]   Attribution line (D-27) │
└───────────────────────────────────────────────┘
```
**As built, 2026-10-10 (lane S, owner decision on docs/51 §7 Q6; `web/sites.py`, `web/templates/_site_panel.html`,
`site_detail.html`).** After the facts block, "At this site" lists the record's site (docs/21 §3.25) when the viewer may
see two or more of its members: the lead first, each EIA plant's units under its first unit (summarised as a count above
20 records), every other member with its relationship to the lead, a confidence word, MW and status chip, each linking
to its record; neighbours at the same grid connection point sit apart in a collapsed "Also at this interconnection
point" list; a credit line names the registers. `/sites/{public_id}` adds the totals, sponsors (linked), the EIA plant
ids, connection points and operating-plant asset with its owners, and the Sources panel. `SITES_ENABLED=0` hides both.

States: loading — skeleton of the key/value grid and timeline rows. Error — RFC 9457 title, no stack trace; a
gated record renders identically to not-found (D-21/API-4). Delayed — n/a since 2026-09-21; nothing is absent by age,
not shown-then-blurred. Restricted-precision — "view at source" language, not a map label. Unplaced — n/a (no
map here); location falls back to text ("Clark County, NV — centroid").

**As built, 2026-10-10 (review `docs/51` §2.6 item 7).** A name its register files in capitals ("TRENT WIND FARM LLC")
is set in a readable case in the H1, the breadcrumb and `<title>` (so `og:title`) of proposal, opportunity, asset and
company pages ("Trent Wind Farm LLC"; `web/formatting.py::readable_name`): words of four letters or fewer stay in
capitals unless they are ordinary words (BESS, SLF, LLC, KCE stay; WIND, FARM, BLUE become words), Roman numerals,
initials and letters joined to digits are untouched, and a name with any lower-case letter is printed exactly as
filed. The name as filed is printed once under the H1 ("As filed: TRENT WIND FARM LLC"). The meta description,
JSON-LD, search, URLs and every other field keep the register's spelling. Grid connection point names are left as
filed (bus names such as `W_LOMA_ALTA_138` read worse cased). The proposal page's capacity and storage rows and its
meta description print through `mw` ("3,200", not "3200.0"). Every `<title>` separates page and site with a spaced em
dash ("Pricing — Inframation"), the dash the site's prose already uses, instead of " -- ".

### 5.3 Opportunity list (`/opportunities`)

```
┌─────────────────────────────────────────────────────────────────────┐
│ D-3 banner (public tier)                                              │
│ Filter bar: kind▾ issuer▾ technologies▾ due date range                │
│ Default: status=open, sort due_at asc (US-301 AC2)                    │
│ chp│ title                │ issuer │ tech       │ due    │ source     │ sticky header, 40-48px
│ Cmt│ 2027 All-Source RFP  │ APSC   │ solar,bess │ 15 Dec │ TED [i]    │ rows, numbers right-
│ Neu│ …                    │ …      │ …          │ …      │ …          │ aligned (D-24)
│ Page size 50, cursor pagination — no "page 7 of 40" (D-24)            │
│ Attribution line (footer)                                              │
└─────────────────────────────────────────────────────────────────────┘
```
As built (2026-10-07, audit UX-2, expert review): the Issuer column shows only when a row on the page has an issuer
(the linked organisation, else a Grants.gov notice's agency code, marked as a code); TED and World Bank buyer names
are in the normalised rows but are neither linked nor served by the API (reported to the API lane). A row whose due
date has passed while its source has not closed, cancelled or awarded it carries "Deadline passed {date}" beside the
status chip, here, on the record page and in search; the status itself stays the source's. Due dates do not wrap.
States: empty — "No open opportunities match these filters" + which facet + Clear all. Loading — skeleton rows
at final row height (no CLS). Error — RFC 9457 banner above table, table area empty. Delayed — D-3 banner;
delayed — n/a since 2026-09-21 (the record shows its current state and its full timeline). Restricted-precision — n/a here.
Unplaced — opportunities without a territory show jurisdiction as text only.

### 5.4 Pro alerts (`/alerts`)

```
┌───────────────────────────────────────────────────────────────────┐
│ Alerts                                        [+ New from filter] │
│ [card] TX storage 50–500 MW   daily·active·last run 06:00         │ saved-search card
│        7 new matches              [edit] [pause] [delete]         │ (docs/31 §5)
│ [card] Nevada BESS filings    immediate·paused·last run —         │
│        0 new matches              [edit] [resume] [delete]        │
│ Quota: 2 of 25 saved searches used                                 │
└───────────────────────────────────────────────────────────────────┘
```
States: empty — "No saved searches yet. Save a filter from any list or map to get alerted," linked to `/map`.
Loading — skeleton cards. Error — RFC 9457 banner; existing cards keep last-known state, staleness noted.
Delayed-tier — n/a (nothing is delayed). Restricted-precision, unplaced — n/a. At-quota — "New" disabled with
quota text, not silently hidden.

### 5.5 Admin source health (`admin/sources`)

```
┌───────────────────────────────────────────────────────────────────────┐
│ Source health                                        [+ add issuer]   │
│ St│ source          │last run │rows Δ│events│$/rec  │publish          │
│ ✓ │ CAISO gen_queue │ 05:00   │ +12  │ 12   │$0.004 │ public          │
│ ⚠ │ PJM interconn.  │ —       │ —    │ —    │ —     │ GATED (visible) │
│ ✗ │ MISO queue      │ 3 fails │ —    │ —    │ —     │ unknown         │
│ Row detail: cadence, licence class, legal evidence link,               │
│ [run now] [pause] [resume] [edit cadence]                              │
│ Gate clearance (legal role only): evidence URL + date + classification │
└───────────────────────────────────────────────────────────────────────┘
```
States: empty — n/a (registry always populated from `sources.yaml`; unimplemented shows literally as
"unimplemented", never absent, DA-12). Loading — skeleton rows. Error — adapter/API failure banner, table falls
back to last successful poll with a staleness timestamp. Delayed-tier — n/a (admin always live).
Restricted-precision, unplaced — n/a. Gated — publish column reads "GATED", not blank or a false "public".

## 6. State inventory summary

| State | Applies to | Rule |
|---|---|---|
| Empty | every list/map/feed | Names the filter that emptied results, offers Clear all (D-29, US-102 AC4) |
| Loading | every screen | Skeleton of final layout, never a spinner over a table (D-29, CLS) |
| Error | every screen | RFC 9457 `title` + `request_id`; never a stack trace; gated record = not-found, indistinguishable (D-29, API-4) |
| Tier notice | every public list/detail/feed (not the map page since 2026-10-10, §5.1 as built) | Fixed-position D-3 line: "Every record and every change event is published as soon as it is ingested. Alerts and API in Pro." (D-3, D-28). Since 2026-10-07 the stamp says "Live" only while every scheduled source is within its fetch allowance, else "No delay" with the late sources named (docs/31 §5.4) |
| Restricted-precision notice | map markers, detail location field | "location shown at county level (source licence)" wherever the geometry renders (D-9) |
| Unplaced-record fallback | map side panel only | "Unplaced (N)" collapsible, never silently dropped (D-8) |

## 7. Assumptions

| ID | Assumption | Basis | Affected |
|---|---|---|---|
| IA-1 | `/` is the map route rather than a separate marketing landing page, since US-104 makes the map primary and D-7 bans a marketing hero above a working screen | US-104, `docs/04` D-7 | `docs/31` home-screen component budget |
| IA-2 | Admin nav order follows the `docs/23` §4 endpoint grouping literally (Sources → Records → Resolution/Extraction → Tasks → Posts → Users/Customers → Keys/Costs/Audit) since no separate admin IA exists yet | `docs/23` §4 | `docs/20` §8 if that doc states a different order |
| IA-3 | The `/submit` intake route is reachable from both `/proposals` and `/opportunities` list toolbars (developer vs. utility framing) rather than a single generic entry, since US-1001 and US-1003 are separate forms with different fields | US-1001, US-1003 | Nav component in `docs/31` |
| IA-4 (2026-10-22) | Bankable's `/assessment` is the right pre-bridge landing for "Start a deal in Bankable": it is Bankable's public "Get Started" and creates a sponsor's project. Read in Bankable's code, not tested live | github.com/Tobombadil/bankablehq `src/App.tsx`, `src/components/landing/Header.tsx`, `src/pages/Assessment.tsx`, read 2026-10-22 | §2.6 link target; `BANKABLE_START_URL` changes it without code |
| IA-5 (2026-10-22) | Bankable ignores unknown query parameters, so the UTM parameters cannot break its start page | React Router passes query strings through; `Assessment.tsx` reads none | §2.6 |
| IA-6 (2026-10-22) | "Bankable is run by the team behind Inframation" is true, and the owner accepts it on public pages | `docs/18` §6 A2 (the owner owns or controls Bankable) | §2.6 copy; the owner confirms before Lane B ships |
| IA-7 (2026-10-22) | The site finder lane ships `/site-finder` as a server-rendered page behind a switch it turns on only when the page has results | The decision rows of 2026-10-22; the rule that a section shows only once it has something in it | §2.3, §2.8 Lane A item 2 |
| IA-8 (2026-10-22) | Desktop windows between about 1165 and 1240px, and tablets between 720 and 770px, are few enough that a taller header there costs less than hiding the links | Common laptop widths are 1280 and up; not measured on Inframation's traffic, which does not exist yet | §2.3; revisit with analytics |

## 8. Change history

- 2026-09-12 v1 — first draft (product-designer), following `docs/30-design-references.md` v1.
- 2026-09-19 — §3 page inventory rows for `/assets/{slug}` and the company page updated, §3.1 added
  (frontend-developer, midstream slice).
- 2026-09-19 — §3.1 updated (frontend-developer, second midstream slice): ethanol and RNG asset rows, the
  company-page descriptor for `other`-typed holders, grouped nearby rows, ethanol/RNG live on the map.
- 2026-09-19 — §1.1, §3 and new §3.2 (frontend-developer, navigation and discoverability lane): `/assets` and
  `/organizations` index pages, Open Graph and Twitter card meta, JSON-LD, `robots.txt`, and the split sitemap.
- 2026-10-07 — §1 nav as built and a names table, §3 `/docs/api` and `/feeds/*` as built, §4.1 status control,
  §4.6 report → record → unmerge (frontend-developer, lane L5, design audit UX-1, UX-3, UX-6, UX-7); §3.1 Follow,
  §4.1 uncovered ISO areas and large-load views, §5.3 opportunity issuer and deadline, §6 tier notice freshness
  (same lane, expert reviews 2026-10-07; lane L11).
- 2026-10-10 — as built (frontend-developer, pre-beta lane H, review `docs/51` §2.6): §1 Pricing out of the nav under
  the noncommercial posture; §3.1 company-page pipeline; §5.1 map page opening line, lateness once in the masthead,
  the key on the map; §5.2 readable record names, `mw` on the proposal page, title dash; §6 tier notice row.
- 2026-10-10 — as built (frontend-developer, lane F): §5.1 the compact key with a layer on and the map's filter
  bar as a disclosure at 720–1079px, with before/after measurements and the 1024×768 budget.
- 2026-10-10 — as built (backend, lane P): §3.1 the company pipeline from one aggregate, group links by
  `sponsor_scope`, "No longer listed" apart from the active pipeline, one name per grid; labelled company and
  connection-point lists; the record page's "no longer listed" line.
- 2026-10-11 — as built (frontend-developer, lane U, owner decision): §5.1 records no register lists any more are
  hidden from the map and the list on a view of active states, with the `unlisted=include` toggle, the hidden-records
  line and its measured cost.
- 2026-10-22 — design (product-designer, lane ia-phases, owner's phase decision): new §2.1–§2.9, the site by
  development phase (reader words Find · Build a deal · Get funded; a flat nav of the sections with pages, Site finder
  second once live; operations as lifecycle states on the one base map; the Build a deal block before the Bankable
  bridge; URLs; the implementation plan and the files other lanes hold); §1 nav paragraph and names table (three
  rows); §1.1 sitemap redrawn by phase and as built; §2 marked where the 2026-10-22 decisions changed it (view row,
  side list, bottom sheet, D-14 retired); §3 `/site-finder` row; §7 IA-4 to IA-8.
