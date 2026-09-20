# PRD — Bankable MVP (proposal graph + change feed)

**Status:** Sprint 0 deliverable, v0.1 · 2026-09-12 · owner: product-manager · reviewed by: owner
**Owner direction 2026-09-12:** the existing Bankable workflow (analyse → certify → route → fund) is a later
consideration, not a foundation. Wording below that treated it as the destination has been softened; the product
(graph, feed, tiers, alerts, syndication) is unchanged. "Operations team (S0)" replaces "routing team".
**Depends on:** `docs/00-PLAN.md` (vision, decisions, open questions), `docs/01-feasibility.md` (reframing, price
anchors, validation plan), `docs/02-data-sources.md` (source tiers, legal register, schema seed, ingestion
order), `docs/03-agent-operating-model.md` (roster, run-time loop, sprint cadence), `data/sources.yaml`.
**Does not settle:** pricing (Phase 1 business plan), technology (Phase 2 ADRs), UI (Phase 3 design).

---

## 1. Problem and thesis

### 1.1 Problem

Capital, offtake and grid access for energy and infrastructure projects are allocated through thousands of
public signals that nobody reads together: interconnection queues, EIA registrations, FERC dockets, permits,
NEPA notices, utility RFPs, federal and MDB funding notices, tenders, and large-load requests. Each signal
describes a piece of a real-world project or a real-world demand for one, under a different name, sponsor
spelling and status vocabulary (`docs/01-feasibility.md` §3.3). The people who need to act on those signals
(developers looking for offtake and money, lenders looking for deals at the right stage, utilities looking
for supply, EPCs looking for build pipeline, advisors looking for clients, data-centre operators looking for
power) either pay an enterprise incumbent, buy narrow trackers, or read the sources by hand.

Listing the sources is not a business. Interconnection.fyi already publishes cleaned queue data free and daily;
Cleanview sells a tracker at $9,000/yr; Halcyon sells filing intelligence; Enverus owns the enterprise budget
(`docs/01-feasibility.md` §3.1). A new site that lists the same rows has no pricing power.

### 1.2 Thesis (reused from `docs/01-feasibility.md` §5)

Bankable already exists as an analyse → improve → certify → route → fund workflow at bankablehq.com. The
proposals platform is the **top of that funnel and its data asset**, not a standalone product. The MVP builds:

1. **A proposal graph.** One entity per real-world project (supply) or opportunity (demand), stitched across
   queue, registry, permit, docket, funding and news sources, with one lifecycle vocabulary and full provenance
   on every field.
2. **A change feed.** Snapshot diffs turn static registers into events (filed, status change, withdrawn,
   RFP opened, award, cancellation, reinstatement). The product is the feed, not the table
   (`docs/00-PLAN.md`, standing principles).
3. **Proposal ↔ opportunity matches.** The demand side nobody has assembled cleanly (utility/co-op/CCA RFPs,
   DOE/USDA/MDB funding with status, tenders, large-load interconnection) matched to supply-side proposals. This
   is the wedge and it is exactly what makes a project "bankable".
4. **Three tiers.** Delayed free (attribution, SEO, social feed), live Pro (alerts, filters, exports), Team/API.
   Price anchors and the tier ladder are in `docs/01-feasibility.md` §3.4; the numbers are set in Phase 1.
5. **Light intake and lead hand-off (later-consideration hooks).** A light "submit a project" intake and a
   CRM lead hand-off for matched pairs. Any deeper deal workflow, including integration with the existing
   Bankable app, is a later consideration, not part of this platform's foundation.

### 1.3 What we are betting on

- Fusion + change detection + action is unoccupied; listing is commoditised (`docs/01` §3.1, confidence
  moderate-high).
- The demand side is thin but valuable in 2026 precisely because it is volatile (`docs/01` §3.6).
- The free tier (every record, undelayed since 2026-09-19) and the Bluesky/LinkedIn feed are marketing spend with a measured CAC for the paid tiers and
  for any later deal-workflow revenue (`docs/01` §4; later consideration).

The kill signals for each bet are the validation plan in `docs/01-feasibility.md` §6 and are restated as MVP
targets in §5 below.

---

## 2. Users and jobs-to-be-done

Segments follow `.claude/agents/product-manager.md` and `market-researcher.md`. Segment sizes are not stated
here; the market-researcher's Sprint 0 deliverable (§8.1) supplies them. Each job names the MVP feature that
serves it; a feature that serves no job below is out of scope.

| # | Segment | Jobs to be done | Served by (stories in §4) |
|---|---|---|---|
| S1 | **Developers / IPPs** (sponsors of queued or permitted projects) | (a) Find live offtake, funding and tender opportunities that fit my project's technology, jurisdiction, size and timing. (b) Know within a day when a competitor in my county/ISO changes status or withdraws. (c) See my own project's public record and correct it. (d) Get my project in front of capital. | Opportunities browse (US-3xx), matches (US-4xx), alerts (US-5xx), intake (US-10xx) |
| S2 | **Lenders / investors** (project finance, tax equity, infra funds, family offices) | (a) Screen proposals by stage evidence (LGIA filed, permit issued, offtake awarded), not by press release. (b) Watch a named sponsor's or region's pipeline for changes. (c) Get deal flow that is already matched to an opportunity. (d) Pull data into internal models via API. | Detail with lifecycle (US-2xx), alerts (US-5xx), API (US-7xx), routing view (US-9xx) |
| S3 | **Utilities / co-ops / CCAs** (procurement, resource planning, interconnection staff) | (a) See which proposals in my territory could respond to my RFP. (b) Benchmark my queue against neighbours. (c) Publish my RFP where sponsors will see it. | Browse (US-1xx), matches (US-4xx), opportunities submission via intake (US-10xx, later full) |
| S4 | **EPC / OEM** (build and equipment pipeline) | (a) Find proposals entering the stage where EPC/equipment is contracted. (b) Track a technology class (storage, gas, nuclear) across ISOs. (c) Alert on awards and permits. | Browse with technology/stage filters (US-1xx), alerts (US-5xx) |
| S5 | **Advisors / law firms** (transaction, regulatory, siting counsel) | (a) Spot new dockets, filings and RFPs in my practice area as they appear. (b) Reference a single provenance-backed record in client work. (c) Export and cite. | Detail with provenance (US-2xx), alerts (US-5xx), exports (US-6xx) |
| S6 | **Data-centre / large-load operators** | (a) Find where large-load capacity is being requested and granted (ERCOT large-load queue first, per `data/sources.yaml` `us.iso.ercot.large_load_queue`). (b) Track utility and ISO large-load rules and dockets. (c) Find generation/storage proposals near candidate sites that could co-locate or supply. | Browse with kind=load (US-1xx), detail (US-2xx), matches (US-4xx) |
| S0 | **Operations team** (internal) | (a) Turn a matched proposal + opportunity into a lead in the CRM. (b) Review intake submissions. (c) Keep sources healthy and the publish gate honest. (d) Approve social posts until channels are trusted. | Admin (US-9xx), social review queue (US-8xx), intake review (US-10xx), lead events written to CRM (§4.9) |

Segments S1 and S2 are the pre-sell targets in `docs/01-feasibility.md` §6 (weeks 6–8) and drive the Pro
tier design. S3–S6 are served by the same features with filters; they are not given bespoke features in MVP.

---

## 3. Scope

### 3.1 In scope (MVP, Sprints 1–3)

| Area | Included |
|---|---|
| Sources | Tier-1 rows in `data/sources.yaml` whose `reuse` is `open` or `attribution` and whose legal evidence is recorded: CAISO, ERCOT GIS, ERCOT large load, EIA-860M, LBNL Queued Up (history), FERC eLibrary (ER/CP dockets), Permitting Dashboard, grants.gov, DOE eXCHANGE, curated utility/co-op/CCA RFP issuers (target 50), TED, World Bank procurement, NESO TEC, GEM trackers. SPP, NYISO and ISO-NE ingest in Sprint 1 but publish only after their terms are read and recorded (`docs/02` §4). |
| Graph | Proposal, opportunity, organization, event, match, source, proposal_source per the schema seed in `docs/02-data-sources.md` §5. Deterministic then probabilistic entity resolution with reversible merges. One lifecycle vocabulary. |
| Change feed | Snapshot diff per source run; typed events; per-proposal timeline; global and filtered feeds. |
| Public tier | Browse/search/filter/map-list of proposals and opportunities; detail pages with provenance and attribution; RSS per saved filter; **every record, visible as soon as it is ingested**. No login required. |
| Pro tier | The paid shapes, not fresher records: saved searches; email alerts (immediate and digest); CSV export; watchlists; seat-based subscription. Also the live view of ISO change events. |
| Team/API tier | Read API with keys, plan-based rate limits, shape entitlement enforced server-side. |
| Matching | Rule-based proposal ↔ opportunity matches with an explanation; model-assisted re-ranking only if the data-scientist's evaluation shows a measured gain (§8.4). |
| Syndication | Bluesky first, LinkedIn second (via approved API or an approved scheduler until MDP approval), X on a capped budget if the owner funds it. Every post goes through a review queue until the owner lifts review per channel. |
| Admin | Users; customers and subscriptions read from and written through the CRM/ERP adapter; source health; publish/unpublish per source and per record with a licence gate; post review queue; intake review. |
| Intake (light) | Public "submit a project" form → pending proposal → admin review → matches computed → CRM lead. |
| Billing | Monthly Pro seat subscription and annual API plan through the billing provider chosen in Phase 2 (ADR). |
| Compliance | Provenance fields on every record; automatic attribution; minimum personal data; deletion request handling; automated-account labelling. |

### 3.2 Out of scope (MVP)

| Item | Why | Where it lives |
|---|---|---|
| **PJM rows on any public or Pro surface** | `reuse: restricted`; redistribution prohibited without a PJM Redistribution License (`docs/01` §3.2, `docs/02` §4). Ingestion may be built and tested behind the gate; **publication is gated on a signed licence recorded in `sources.yaml`.** | Gated (§3.3) |
| **MISO rows on any public or Pro surface** | Terms unknown (`reuse: unknown`) and access blocked by Cloudflare. **Publication is gated on (a) terms read and recorded and (b) a compliant egress method approved by legal-compliance.** | Gated (§3.3) |
| **Private aggregators** (Interconnection.fyi, Cleanview, Energy Adepto, BidNet, Halcyon, Enverus) | Never scraped, in any phase. EU database right and ToS (`CLAUDE.md`, `docs/02` §4). Partner or buy only. | **Never** |
| Scoring, certification, capital-partner routing, integration with the existing Bankable app | Later consideration (`docs/00-PLAN.md` Phase 5). MVP only emits the lead. | Later |
| Full opportunity submission by issuers, sponsor self-service editing of records | Needs identity verification and moderation; MVP takes intake submissions only. | Later |
| Non-ISO utility OASIS queues, state siting boards, BLM/BOEM/USACE, NRC, EPA Class VI, gas/LNG/nuclear/CCS document pipelines | Ingestion order steps 6–7 (`docs/02` §6). | Later |
| State PUC dockets | Partner with or buy from Halcyon later (`docs/01` §5). | Later |
| Reddit and Meta automation | Terms and cost (`docs/01` §3.5). | Never automated; manual if at all |
| Open-web crawling | Useful universe is a few hundred structured sources (`docs/01` §7). | Later, news APIs only |
| Native mobile apps, multi-language UI, SSO/SAML | No named user job in MVP. | Later |
| Replacing or merging the existing Lovable app | Owner question 1 open (`docs/00-PLAN.md`). | Assumption in §7 |

### 3.3 Gated (built but not published until the gate clears)

| Gate | Condition to clear | Who clears |
|---|---|---|
| G-PJM | Signed PJM Redistribution License; `sources.yaml` `us.iso.pjm.gen_queue` updated with licence reference, date, permitted uses; legal-compliance sign-off. | Owner (signatory) + legal-compliance |
| G-MISO | Terms captured verbatim with URL/date in `sources.yaml`; classification recorded; egress method that does not circumvent access controls approved in writing; if terms forbid derived publication, MISO stays API-only or is dropped (`docs/01` §6 week 2–3). | legal-compliance + solutions-architect |
| G-SPP, G-NYISO, G-ISONE | Terms read and recorded (`docs/02` §4); classification derived-only or raw. Until then these three ingest into the store but the publish flag stays off. | legal-compliance |
| G-CAISO-raw | Raw rows never shown; derived records with CAISO credit and link-out only (`docs/02` §4). Already decided; listed so the publish gate encodes it. | — |

The publish gate is a product requirement (US-905), not a process note: the admin panel must be unable to set
a source to public while its gate is unmet.

### 3.4 Later (post-MVP, in order of pull)

1. Non-ISO OASIS queues (most defensible, most expensive; `docs/02` §3).
2. State siting boards; large-load dockets beyond ERCOT.
3. Gas/LNG/pipeline/nuclear/CCS document extraction.
4. Issuer self-service opportunity posting; sponsor record claims.
5. Later consideration: scoring, routing, any merge with the bankablehq.com app.
6. Additional international feeds by customer pull; GridTracker/Cleanview partnership if owner appetite (question 5).

---

## 4. User stories and acceptance criteria

Conventions: IDs are stable; do not renumber. "Segment" names who the story serves. Acceptance criteria are
written so QA can turn each into a test without asking the author. "Tier" says which tier sees the behaviour
(Public = free, every record, undelayed since 2026-09-19; Pro = paid workflow shapes; API = key-based; Admin = internal). Terms:

- **Lag** — the configured delay between a record or event becoming visible to Pro and becoming visible to
  Public. ~~Default 14 days, configurable per source and per event type, bounded 7–30 days (`docs/01` §3.4,
  assumption A-7 in §7).~~ **Superseded 2026-09-19 (owner): the paywall is by shape, not by time.** Verbatim:
  "alerts, exports, API and watchlists are paid; free users see every record; the delay is kept only on ISO
  change events. Supersedes the time-delay model in docs/10 and docs/41". A **record** therefore has no lag on
  any tier. A **change event** has a lag only when its source declares one (`data/sources.yaml`
  `change_event_lag_days`); today that is the eight `us.iso.*` interconnection-queue registers at 14 days.
  Every "lag" below is to be read against that rule; the paragraphs that assumed a blanket record delay are
  marked where they occur.
- **Provenance** — `source_id`, `source_url`, `retrieved_at`, `licence` on every stored record (`CLAUDE.md`).
- **Lifecycle vocabulary** — announced, filed, studied, permitted, contracted, built, withdrawn, cancelled
  (`docs/02` §1), plus `unknown` for rows with no mappable status (216 SPP rows have blank status, `docs/01` §3.3).

### 4.1 Browse and search proposals (US-1xx) — segments S1–S6

**US-101 Browse proposals list.** As any visitor, I want a paginated list of proposals with the core fields so I
can scan what is active.
- AC1: List shows name_canonical, kind, technology, capacity_mw (and storage_mwh where present), jurisdiction,
  state/county, lifecycle_state, last_changed, source count. Default sort: last_changed desc.
- AC2: Page size 50; total count shown; pagination is stable under concurrent ingestion (cursor, not offset).
- AC3: ~~Public tier shows only records whose `first_seen` is older than the lag and whose latest visible event
  is older than the lag; Pro shows all.~~ **Amended 2026-09-19 (paywall by shape):** the public tier shows every
  record the licence and source gates allow, with no age test; only an *event* from a source that declares a
  change-event lag is withheld, and then only from the event surfaces (timeline, `/v1/events`, feeds). Verified
  by a fixture with one ISO change event at lag−1 day and one at lag+1 day, and by a record fixture ingested
  seconds ago that the public tier returns.
- AC4: No record from a gated source (§3.3) appears in any tier while the gate is unmet. Verified by a fixture
  with a PJM-tagged record.

**US-102 Filter proposals.** As an EPC/OEM (S4) or lender (S2), I want filters so I can narrow to my market.
- AC1: Filters: kind, technology (multi), lifecycle_state (multi), jurisdiction/ISO, state, county, capacity
  range, first_seen range, last_changed range, sponsor organisation, source.
- AC2: Filters are reflected in the URL; a shared URL reproduces the same result set on the same tier.
- AC3: Combining filters is AND across facets and OR within a facet; a test confirms both.
- AC4: An empty result states which filter removed the last results (or simply "no results" plus a clear-all).

**US-103 Free-text search.** As an advisor (S5), I want to search by name, sponsor, queue ID, docket number or
EIA plant ID so I can find a specific project.
- AC1: Searching an exact queue ID, FERC docket number or EIA plant/generator ID returns the proposal holding
  that identifier as the first result (tested with one of each from the live dataset).
- AC2: Name search tolerates case, punctuation and common abbreviations (e.g. "BESS", "LLC"); tested with ten
  labelled queries from the data-scientist's evaluation set.
- AC3: Results return in under 500 ms p95 for the MVP dataset size (tens of thousands of records,
  `docs/01` §3.4) on the production environment; measured in CI against a seeded database.

**US-104 Map as a primary navigation surface.** As any user (S0–S6), I want to browse every proposal and
opportunity on a map with the same filters as search, so that geography is a first-class way in, not a
secondary view (owner decision 2026-09-12, `docs/00-PLAN.md`).
- AC1: Records with a point or county are shown on a map; records with state only are listed under the state
  and shown as a state-level aggregate marker.
- AC2: Map respects the active filters and tier rules of US-101/102; filter state is shared and URL-addressable
  between map and list views.
- AC3: Where a source's terms allow only derived data, no raw coordinates from a restricted source are exposed;
  county centroid is used. Legal-compliance confirms which sources this applies to.
- AC4: Markers cluster by lifecycle state and technology at low zoom; opportunities with a service territory or
  jurisdiction render as polygons; selecting a marker opens a detail drawer with provenance and attribution.
- AC5: Renders 20,000+ markers within the performance budget set in `docs/04-standards.md`; map content is
  reachable by keyboard and screen reader (a list equivalent is always available).

**US-105 Attribution on lists.** As the owner, I need every list and export to carry attribution so licences
are honoured automatically.
- AC1: Each list page renders a credit line per distinct source present in the result set, using the
  attribution text from the source registry (mirror of `sources.yaml`).
- AC2: CSV export (US-603) includes `source_id`, `source_url`, `licence` columns on every row.

### 4.2 Proposal detail with provenance and lifecycle timeline (US-2xx) — S1, S2, S5, S6

**US-201 Proposal detail page.** As a lender (S2), I want one page per real-world project with all evidence
stitched together.
- AC1: Page shows canonical fields; a "Sources" panel listing every `proposal_source` with source name,
  source record id, `retrieved_at`, licence badge, and a link to `source_url`.
- AC2: For restricted or attribution-with-restriction sources (CAISO), the raw row is not rendered; the page
  shows derived fields and a "view at source" link (`docs/02` §4).
- AC3: Page has a stable, human-readable URL that survives merges (old ids redirect 301 to the surviving id).
- AC4: ~~Public tier renders the record as of `now − lag`: fields and events newer than the lag are hidden and a
  banner says "Updated N days ago on the live tier" with a Pro call-to-action.~~ **Amended 2026-09-19 (paywall
  by shape):** the public tier renders the record's current fields. Only its *timeline* is partial, and only for
  sources that declare a change-event lag; the banner states that instead (`docs/04` D-3/D-28).
  **Invalidated by this amendment:** the argument that the record page is itself a conversion surface because a
  visitor is looking at stale fields. It is not any more — the page is current, so whatever conversion the
  record page produces has to come from the alert and watchlist calls-to-action, and the figures in `docs/11`
  that were reasoned from "free users see stale data" (§3's delay schedule and the conversion argument built on
  it) are not evidence for the new shape. They have not been re-derived; nothing here should be read as a
  measured conversion claim.

**US-202 Lifecycle timeline.** As a developer (S1), I want to see every event on a project in order so I know
its stage and history.
- AC1: Timeline lists events (type, observed_at, source, before → after for status changes) newest first.
- AC2: Merge and unmerge events are shown and labelled; a merge event links to the absorbed record.
- AC3: The timeline and the lifecycle_state agree: the state equals the `after` value of the latest
  status_change event, or `unknown`. A consistency test runs nightly and reports mismatches to source health.

**US-203 Linked organisations and opportunities.** As an advisor (S5), I want to see the sponsor and any
matched opportunities from the detail page.
- AC1: Sponsor organisation is a link to an organisation page listing its other proposals.
- AC2: Matched opportunities (US-401) appear with score and rationale; none shown if none.

**US-204 Report a problem.** As a sponsor (S1), I want to flag a wrong merge or stale status.
- AC1: A form captures record id, issue type (wrong merge, wrong status, wrong sponsor, other), free text and an
  optional email; it creates an admin task (US-907) and sends no outbound email automatically.
- AC2: The form stores the minimum personal data and shows the privacy notice link.

### 4.3 Opportunities browse and detail (US-3xx) — S1, S3, S6

**US-301 Browse opportunities.** As a developer (S1), I want a list of open opportunities filtered to my
technologies and jurisdictions.
- AC1: List shows kind (rfp, foa, tender, auction, loan_program, procurement_notice), issuer, title,
  jurisdiction, technologies, capacity_sought_mw or budget, open_at, due_at, status (open, closed, frozen,
  cancelled, reinstated, awarded), source.
- AC2: Default view is status=open sorted by due_at asc; filters as in US-102 plus kind, issuer, due date range.
- AC3: Tier rules of US-101 AC3–AC4 apply.

**US-302 Opportunity detail.** As a utility (S3) checking a competitor's RFP, I want the full record with
provenance.
- AC1: Shows all fields, the Sources panel (as US-201 AC1), the status timeline (as US-202), and linked
  documents (title + link; no article bodies or copied PDFs, per `docs/02` §4 news/personal-data rules).
- AC2: Status changes such as frozen → cancelled → reinstated are events with source and date, not overwrites.

**US-303 Curated issuer registry.** As the operations team (S0), I need the 50-issuer RFP list to be a first-class
source so its records carry provenance like any other.
- AC1: Each curated issuer is a row in the source registry with `source_id`, URL, cadence, `licence:
  attribution`; records ingested from it carry those fields.
- AC2: Admin can add or pause an issuer without a code change (US-904).

### 4.4 Proposal ↔ opportunity matches (US-4xx) — S1, S2, S3, S0

**US-401 Rule-based matches.** As a developer (S1), I want to see opportunities my project is eligible for.
- AC1: A match exists when technology, jurisdiction, size window and timing rules pass; the rule set is a
  versioned config file, and each match stores `score`, `rationale` (which rules passed/failed) and
  `created_by = rule`.
- AC2: Matches are recomputed when either side changes; a change in match set emits a `match_added` or
  `match_removed` event visible in both timelines.
- AC3: On a labelled sample of ≥100 proposal–opportunity pairs supplied by the data-scientist (§8.4),
  precision ≥ 0.7 at the default threshold; the measured number is recorded in `docs/22-*`.

**US-402 Match explanation.** As a lender (S2), I want to know why a match was made before I trust it.
- AC1: Each match renders the rationale in plain language ("storage, TX, 50–500 MW, due in 45 days").
- AC2: A user on Pro can dismiss a match; dismissal is stored per user and does not alter the global match.

**US-403 Lead hand-off.** As the operations team (S0), I want a matched pair to become a lead.
- AC1: An admin action "create lead" writes a lead to the CRM through the adapter (US-903) with proposal id,
  opportunity id, score, rationale and a link back; the app stores only the CRM lead id.
- AC2: Sponsor activity events (status_change, filed) on a proposal with an existing lead update the lead's
  activity in the CRM within one pipeline run (`docs/03` §3 step 9).

### 4.5 Saved searches and alerts (US-5xx) — S1, S2, S4, S5

**US-501 Save a search.** As a Pro user, I want to save a filter set so I can be alerted on it.
- AC1: Any filtered list (proposals, opportunities, feed) can be saved with a name; a user may hold up to 25
  saved searches (limit configurable).
- AC2: A saved search stores the filter definition, not the result set; re-running it reflects current data.

**US-502 Email alerts.** As a lender (S2), I want an email when a saved search gains a new record or event.
- AC1: Delivery modes per saved search: immediate (within 15 minutes of the event being published to the
  user's tier), daily digest, weekly digest. Default: daily.
- AC2: An alert email lists each new record/event with a link to the detail page and the source credit line;
  no more than one immediate email per saved search per 15-minute window (batched).
- AC3: Every email carries an unsubscribe link and the sender identity; unsubscribing stops that alert within
  one delivery cycle (CAN-SPAM checklist from legal-compliance, §8.2).
- AC4: Delivery is logged (alert id, user, saved search, event ids, sent_at, provider message id); the log is
  the source of truth for metrics M-5.

**US-503 RSS feeds.** As a public visitor, I want an RSS feed for a filter so I can follow without an account.
- AC1: Every public list URL has an RSS equivalent; the feed contains every item whose source publishes change
  events live, and items from a source with a change-event lag once that lag has elapsed (amended 2026-09-19).
- AC2: Every social post links to a page that offers the RSS feed and the alert sign-up (`content-social.md`).
- AC3: Feed items carry the source credit line.

**US-504 Manage alerts.** As a Pro user, I want to see, edit, pause and delete my alerts.
- AC1: A settings page lists saved searches with mode, last run, last match count; edit/pause/delete work and
  are reflected in the next cycle.

### 4.6 Delayed vs live tier behaviour (US-6xx) — all segments, S0

**US-601 Tier enforcement is server-side.** As the owner, I need the lag enforced in the data layer so no
client can bypass it.
- AC1: A single visibility function decides what a given tier sees; every read surface (web, RSS, API, export)
  calls it. ~~A test proves an unauthenticated API request for a record newer than the lag returns the delayed
  view, and the same request with a Pro key returns the live view.~~ **Amended 2026-09-19:** the test proves it
  for an *ISO change event*; the same request for a record newly ingested returns the record on both tiers.
  The licence and source clauses of the predicate are unchanged and still exclude restricted/unknown sources on
  every non-admin tier.
- AC2: The change-event lag is stored per source and per event type in the source registry
  (`change_event_lag_days` / `lag_overrides`); changing it needs no deploy. There is no record-level lag to
  configure.

**US-602 Pro entitlement.** As a Pro subscriber, my seat unlocks the live tier on web and API.
- AC1: Entitlement is read from the subscription record via the CRM/ERP adapter (US-903) and cached with a
  TTL ≤ 15 minutes; cancellation downgrades the user within that TTL.
- AC2: A seat is one login; concurrent sessions above the seat count are refused with a clear message.

**US-603 Export.** As an advisor (S5), I want CSV export of any filtered list on the Pro tier.
- AC1: Export respects tier visibility, filter set and a row cap (10,000 default, configurable per plan).
- AC2: Exported rows include provenance columns (US-105 AC2) and a header line with the licence summary.
- AC3: Exports are logged per user (metric M-6 source).

**US-604 Public tier messaging.** As a public visitor, I understand what I am not seeing.
- AC1: Every public page states its tier line with a link to the tiers page: "Every record is published as soon
  as it is ingested; ISO queue change events are held N days on the free tier" where no record delay applies,
  and "Public data is N days delayed" on a surface where one does (amended 2026-09-19; `docs/04` D-3/D-28).

### 4.7 API keys and rate limits (US-7xx) — S2, developers integrating

**US-701 API keys.** As a Team/API customer, I want keys I can create and revoke.
- AC1: A user with an API plan can create up to 5 keys, label them, see created_at and last_used_at, and revoke
  any; revocation takes effect within 60 seconds.
- AC2: Keys are shown once at creation and stored hashed.
- AC3: A key inherits the tier of its plan (delayed or live); the plan is read via the CRM/ERP adapter.

**US-702 Rate limits.** As the owner, I need limits so the free surfaces cannot be mined and paid plans are fair.
- AC1: Limits are per plan and configurable: defaults Public (no key) 60 requests/hour per IP on read
  endpoints; Pro key 600/hour; API plan 6,000/hour and a daily cap. (Assumption A-8; solutions-architect
  finalises in the OpenAPI spec.)
- AC2: Exceeding a limit returns HTTP 429 with `Retry-After` and standard rate-limit headers.
- AC3: Every request is logged with key id, endpoint, status, latency; this log is the source for metric M-7.

**US-703 Read endpoints.** As an integrator, I want stable read endpoints for proposals, opportunities, events,
matches and organisations.
- AC1: Endpoints exist for list (with the same filters as the web) and get-by-id for each entity, plus an
  events endpoint with `since` cursor; all documented in the OpenAPI spec (Sprint 1).
- AC2: Responses include provenance fields and a `licence_summary` object; restricted raw fields are never
  returned (US-201 AC2).
- AC3: Breaking changes require a versioned path; v1 is frozen at MVP launch.

**US-704 API docs and terms.** As an integrator, I need docs and the licence I am accepting.
- AC1: Docs are generated from the OpenAPI spec; each endpoint shows an example.
- AC2: Key creation requires accepting the API licence drafted by legal-compliance (§8.2); acceptance is
  recorded with timestamp and version.

### 4.8 Social post review queue (US-8xx) — S0, content-social

**US-801 Post drafts from events.** As the content operator, I want drafts generated from change events using
the editorial templates so I only review, not write.
- AC1: Event types that generate drafts and their templates are defined in the social playbook (§8.5); the
  pipeline creates one draft per qualifying event per channel.
- AC2: Every draft contains: text within channel limits, the detail-page link (which offers RSS/alert sign-up,
  US-503 AC2), the source credit, and the disclosure text where the channel requires it.
- AC3: A draft is never created from a record of a gated or restricted source (§3.3).

**US-802 Review queue.** As the operations team (S0), I want to approve, edit, reject or schedule each draft.
- AC1: Queue shows drafts by channel and event type with the underlying event and record; actions: approve,
  edit-then-approve, reject (with reason), schedule.
- AC2: Nothing is published from a channel unless the owner has set that channel to "auto-publish" in admin;
  the default for every channel is "review required" (`docs/03` §1, §3 step 7).
- AC3: Rejections with reasons are stored and reported weekly to the content operator.

**US-803 Publish and measure.** As the content operator, I want approved posts published and their outcome
recorded.
- AC1: Approved posts are published via the channel's approved API (Bluesky; LinkedIn via approved API or an
  approved scheduler; X only if the owner has funded it, with a per-day cap enforced in config).
- AC2: Post id, channel, published_at, and any available reach/click metrics are stored; a UTM parameter on the
  link attributes web visits and alert sign-ups to the post (metric M-8).
- AC3: Automated accounts are labelled as automated where the platform requires it; the label is checked in
  the launch checklist (US-908).

**US-804 Replies are drafted, not sent.** As the owner, I need no automated replies or DMs.
- AC1: The system does not call reply, DM, follow or like endpoints on any channel; a test asserts the
  integration layer exposes only "create post" and "read metrics".

### 4.9 Admin (US-9xx) — S0

**US-901 Users.** As an admin, I want to see and manage application users.
- AC1: List users with email, `user.role` (viewer, member, operator, owner per `docs/20` §5) and
  `account.entitlement` (public, pro, api, admin per `docs/21`; the two are distinct, see `docs/21` §10 C-5),
  created_at, last_login, subscription status (read via adapter); actions: change role, disable, delete
  (deletion also runs the personal-data deletion process, US-910).
- AC2: Admin actions are audit-logged (who, what, when, before/after).

**US-902 Customers and subscriptions are read from the CRM/ERP.** As the owner, the app is not the system of
record for commercial data (`docs/00-PLAN.md` standing principles).
- AC1: The admin "Customers" and "Subscriptions" views render data fetched through the adapter (US-903);
  the app database holds only foreign keys (customer id, subscription id) and a cache with TTL.
- AC2: Creating a customer or subscription in admin calls the adapter's write; the local cache is refreshed
  from the read-back, not from the request payload.
- AC3: If the adapter is unavailable, admin shows the cached data with a staleness banner and refuses writes.

**US-903 CRM/ERP adapter.** As the architect, I need one interface so the system of record can change
(owner question 4 open; assumption A-4 in §7).
- AC1: An interface with operations: get/list/create/update customer; get/list/create/update subscription;
  create/update lead; create activity. One concrete implementation for the assumed system and one in-memory
  fake used by tests.
- AC2: Swapping implementations is a configuration change; the test suite passes against the fake.

**US-904 Source health.** As the operations team (S0), I want to see each source's status and act on it.
- AC1: Per source: last_success_at, last_error, schedule, rows in last run, rows changed, events emitted,
  model cost per record in last run (`docs/03` §6), tier/publish state, licence class, legal evidence link.
- AC2: Actions: run now, pause, resume, edit cadence, edit lag (US-601 AC2), add curated issuer (US-303 AC2).
- AC3: A source that fails N consecutive runs (default 3) is flagged and raises a notification to the
  supervision Routine (`docs/03` §1); the flag clears on the next success.

**US-905 Publish/unpublish with licence gate.** As the owner, I need the gates in §3.3 enforced by software.
- AC1: A source has a publish state: `ingest_only`, `api_only`, `public`. Transition to `public` or `api_only`
  is refused unless the source registry row has `reuse` in {open, attribution} **and** a legal evidence entry
  (URL, retrieval date, classification, reviewer) **and** no open gate flag. PJM and MISO carry a gate flag by
  default that only an admin with the `legal` role can clear.
- AC2: Unpublishing a source hides all its records and events from every non-admin surface within one cache
  TTL and removes them from RSS and API responses; records merged with other sources remain visible with the
  unpublished source's fields removed.
- AC3: Per-record publish/unpublish exists for takedown or correction; it is audit-logged with a reason.

**US-906 Publish gate on records and posts.** As QA, I can prove nothing leaks.
- AC1: An integration test seeds one record per publish state and asserts visibility on web, RSS, API, export
  and post drafts matches the state.

**US-907 Task queue.** As the operations team (S0), I want reported problems, intake submissions and resolution
disputes in one queue.
- AC1: Tasks have type, subject record, status (open, in progress, done, rejected), assignee, notes; actions
  from a task (unmerge, edit status, publish/unpublish) are audit-logged.

**US-908 Launch checklist.** As QA, I need a release gate.
- AC1: A checklist in the repo lists: tier test (US-601), gate test (US-906), attribution render on all
  surfaces, privacy notice and deletion route live, automated-account labels set, rate limits active, alert
  unsubscribe works, backups verified. Release is signed by qa-engineer against it (`docs/03` §5).

**US-909 Model-call cost log.** As the owner, I need cost per source visible.
- AC1: Every run-time model call logs source, record id, tokens, cost; admin shows cost per source per day and
  per record (`docs/03` §6).

**US-910 Personal data and deletion.** As the owner, I must honour deletion requests.
- AC1: A deletion request (from a user or from a named person appearing in a filing) is a task type; completing
  it removes or redacts the personal data fields and records the action; a test proves the fields are gone
  from all surfaces and exports.
- AC2: The data inventory from legal-compliance (§8.2) lists every field that holds personal data; storage of
  any field not on the inventory fails a schema check.

### 4.10 Submit-a-project intake, light (US-10xx) — S1, S3, S0

**US-1001 Intake form.** As a developer (S1), I want to submit my project so it can be matched to
opportunities.
- AC1: Fields: project name, kind, technology, capacity_mw, storage_mwh (optional), jurisdiction, state, county,
  lifecycle_state (self-declared), existing identifiers (queue id, EIA id, docket) optional, sponsor
  organisation, contact name and email, free-text description (≤ 2,000 chars), consent checkbox linking to the
  privacy notice and terms. No file upload in MVP.
- AC2: Submission creates a proposal with `created_by = user`, publish state `pending_review`, not visible on
  any public, Pro or API surface, and a task (US-907). The contact name and email are stored on the task and
  in the CRM through the adapter, never on `organization`; they are *submitted* personal data (consented,
  deletable), distinct from *scraped* filer contacts, which are never stored (`docs/21` §10 C-7).
- AC3: The submitter gets one confirmation email (transactional, not marketing); no other automated outbound.

**US-1002 Intake review and matching.** As the operations team (S0), I want to review, link and match a submission.
- AC1: From the task, an admin can: link the submission to an existing proposal (resolution suggestions shown
  using the same keys as the resolver), approve as a new proposal, or reject with reason.
- AC2: On approve or link, matches (US-401) are computed and a CRM lead is created via US-403.
- AC3: An approved submission is visible to the submitter via a private link; it is public only if the
  submitter opted in and an admin sets publish state to `public`.

**US-1003 Opportunity intake (utility).** As a utility (S3), I want to notify Bankable of an RFP.
- AC1: Same form pattern with opportunity fields (kind, title, technologies, capacity/budget, open_at, due_at,
  URL); creates a pending opportunity and a task; on approval it is added to the curated issuer registry if
  the issuer is new (US-303).

---

## 5. Success metrics

Targets come from the validation plan in `docs/01-feasibility.md` §6 where one exists; others are marked as
assumptions. Every metric names its source of truth so it can be computed without judgement.

| ID | Metric | Source of truth | MVP target | Kill / review signal | From |
|---|---|---|---|---|---|
| M-1 | Queue-row link rate: share of CAISO + ERCOT queue rows resolved to an EIA-860M or docket record | `proposal_source` joins in the app DB; `data/eval/` labelled sample | ≥ 60% | < 60% → narrow scope | `docs/01` §6 wk 1–2 |
| M-2 | Resolution precision / recall on the labelled sample | `data/eval/` + data-scientist report | precision ≥ 0.9, recall ≥ 0.7 (assumption A-9) | precision < 0.85 → block public merges | §8.4 |
| M-3 | Live relevant opportunities at any time (status=open, energy-relevant) | `opportunity` table nightly count | ≥ 30 | < 30 → pivot to supply-side alerts | `docs/01` §6 wk 3–4 |
| M-4 | Customer conversations naming a paid workflow | CRM (via adapter) — discovery call records tagged by sales-bd | ≥ 10% of 20 conversations, i.e. ≥ 2, target 5 | < 10% → stay internal tool | `docs/01` §6 wk 4–6 |
| M-5 | Paid Pro commitments from the pre-sell | CRM subscription records with status paid or signed | ≥ 3 of 10 pitched at $150–250/month | < 3 → revisit pricing/scope | `docs/01` §6 wk 6–8 |
| M-6 | Pro activation: seats with ≥ 1 saved search and ≥ 1 export or alert delivered in first 14 days | Alert delivery log + export log + subscription records | ≥ 70% of paid seats (assumption) | — | — |
| M-7 | API usage: keys with ≥ 100 requests in a week | Request log (US-702 AC3) | ≥ 3 active keys by end of Sprint 3 (assumption) | — | — |
| M-8 | Alert sign-ups attributed to social posts (UTM) | Web analytics per the Phase 3 tracking plan + alert sign-up table | ≥ 25% of public alert/RSS sign-ups carry a social UTM (assumption); reported weekly by channel | Social is measured as CAC, not vanity | `docs/01` §4 |
| M-9 | Source health: Tier-1 sources with a successful run inside 2× cadence | Source health table (US-904) | ≥ 90% of published sources on any day | 3 consecutive failures → flag | — |
| M-10 | Freshness: time from source publication to live-tier visibility | Event `observed_at` vs `retrieved_at`; source cadence | ≤ 24 h for daily sources, ≤ 7 d for weekly | — | — |
| M-11 | Publish-gate breaches: gated or restricted records visible on any non-admin surface | US-906 integration test + nightly audit query | 0 | Any breach → block release | `CLAUDE.md` |
| M-12 | Cost per record: model cost per processed record per source | Model-call log (US-909) | Recorded for every source; any source above the value threshold set in Phase 1 is demoted | `docs/03` §6 |
| M-13 | Leads created from matches or intake | CRM lead records with origin = platform | ≥ 10 by end of Sprint 3 (assumption) | — | Thesis §1.2 |

---

## 6. Release plan

Sprint cadence follows `docs/03-agent-operating-model.md` §5. Sprints 1–2 are as defined there; Sprint 3 is
proposed here and needs owner confirmation in the `docs/00-PLAN.md` decisions log. Each sprint ends with QA
sign-off and an owner review of `docs/00-PLAN.md`. Weeks map to the validation plan in `docs/01` §6.

| Sprint | Weeks | Scope (stories) | Exit criteria |
|---|---|---|---|
| **0 (now)** | — | PRD v0 (this), market research, legal register, architecture/ERD v0, resolution prototype, social playbook, GTM playbook | All §8 definitions of done met; owner review |
| **1** | 1–3 | ADRs, OpenAPI v1 draft (US-703/704 shapes), design system and IA (product-designer), connector framework with the five live ISO queues (CAISO, ERCOT, SPP, NYISO, ISO-NE) + EIA-860M + LBNL into one table with one status vocabulary; ERCOT large-load; FERC/Permitting Dashboard pollers started; source registry mirrored from `sources.yaml`; publish states and gates modelled (US-905 data model); terms for MISO/SPP/NYISO/ISO-NE read and recorded; PJM licence enquiry opened; free API keys registered (`docs/00-PLAN.md` next actions) | M-1 measured on real data and ≥ 60%; M-2 reported; legal register complete for all Tier-1; ADRs accepted |
| **2** | 3–6 | Proposal graph with reversible merges (US-201–203), change events and timelines (US-202), public delayed pages and search (US-101–105, US-604), RSS (US-503), opportunities table from grants.gov + DOE + curated issuers + TED + World Bank (US-301–303), tier enforcement (US-601), first syndication channel (Bluesky) with review queue (US-801–804), source health (US-904), publish gate enforced (US-905/906), 20 customer conversations started (sales-bd) | M-3 ≥ 30; M-11 = 0; public site live on the delayed tier with attribution on every surface; first posts published from the queue; M-4 measured |
| **3** | 6–9 | Pro live tier with seats, saved searches, email alerts, export (US-501–504, US-602–603); API keys, rate limits, docs (US-701–704); billing; admin users/customers/subscriptions via CRM/ERP adapter (US-901–903); matches v1 with explanation and CRM lead hand-off (US-401–403); intake light (US-1001–1003); task queue, deletion, cost log (US-907, 909, 910); LinkedIn as second channel (via approved API or scheduler); launch checklist signed (US-908); pre-sell to 10 | M-5 ≥ 3; M-6, M-7, M-13 measured; launch runbook executed; owner review decides Phase 5 entry |

Not scheduled: PJM and MISO publication (gated, §3.3); anything in §3.4.

---

## 7. Open questions and stated assumptions

The six owner questions in `docs/00-PLAN.md` are unanswered. This PRD proceeds under the assumptions below.
Each is recorded here (the doc that depends on it) per `CLAUDE.md`; the owner's answer supersedes it and the
affected stories are listed so re-planning is mechanical.

| ID | Owner question (`docs/00-PLAN.md`) | Assumption this PRD proceeds under | Affected if the answer differs |
|---|---|---|---|
| A-1 | 1. Relationship to the current Lovable app at bankablehq.com | The new platform is built on the new architecture as its own web app; bankablehq.com keeps its front door and marketing pages and links to the new app; no data is migrated in MVP because the app's contents are unknown. The Cloudflare worker in `.github/workflows/` keeps proxying bankablehq.com during Sprints 1–3 and moves to `infra/` afterwards (decisions log, `docs/00-PLAN.md`). | §3.2 last row, US-1001 (where intake lives), Sprint 3 launch runbook |
| A-2 | 2. Budget and team | Solo founder plus the agent team and contractors; no funded build. Hence: managed services, one syndication channel per sprint, no SSO, no mobile, and the Sprint 3 scope above is the ceiling. | §6 all sprints; §3.4 ordering |
| A-3 | 3. Legal entity and jurisdiction | Assume a US entity for customer terms, API licence and the PJM enquiry; EU/UK data reuse handled by attribution licences that do not require an EU entity. Legal-compliance flags where counsel is needed. | US-704, G-PJM, §8.2 |
| A-4 | 4. CRM/ERP system of record | Assume HubSpot (CRM) + Stripe Billing for subscriptions, on speed of integration; the adapter (US-903) isolates the choice so Odoo/ERPNext/Twenty remain possible. "Subscription" lives in the billing provider and is surfaced through the same adapter. | US-602, US-701 AC3, US-901–903, US-403, M-4/M-5/M-13 sources of truth |
| A-5 | 5. GridTracker/Cleanview partnership appetite | No partnership in MVP; non-ISO coverage stays in §3.4. Sales-bd prepares a term-sheet outline only (§8.6). | §3.4 item 1 and 6 |
| A-6 | 6. Geography after US | EU/UK first because APIs exist and three feeds (TED, FTS, NESO) are already in the plan; no new connectors added for other regions in MVP. | §3.1 sources; Sprint 2 opportunities |

Further assumptions made in this document:

| ID | Assumption | Basis | Affected |
|---|---|---|---|
| A-7 | ~~Public lag defaults to 14 days, configurable 7–30 per source/event type~~ **Retired 2026-09-19 by owner decision.** Records have no lag; 14 days survives only as the ISO change-event lag | The assumption's basis (`docs/01` §3.4's 7–30 day range) was about how long a free tier can be held back. The owner replaced the question: the free tier is a marketing surface, the paid tier is workflow | US-101 AC3, US-601, US-604 — all amended above |
| A-8 | Default rate limits: Public 60/h per IP, Pro 600/h, API 6,000/h + daily cap | No prior evidence; sized to allow browsing without bulk mining; architect finalises | US-702 |
| A-9 | Resolution quality bar: precision ≥ 0.9, recall ≥ 0.7 on the labelled sample | No prior evidence; precision prioritised because wrong merges are visible to sponsors and damage trust | M-2, US-401 AC3 |
| A-10 | Find a Tender is an MVP feed | `docs/00-PLAN.md` decisions and `docs/01` §4 name FTS in MVP, but `data/sources.yaml` `gb.find_a_tender` is `tier: 2`. PRD follows the PLAN; the YAML tier should be corrected to 1 by the data-engineer or the PLAN amended | §3.1, Sprint 2 |
| A-11 | Pro pricing tested at $150–250/month per seat; API plans in the $5–15k/yr band | `docs/01` §3.4 and §6; Phase 1 sets the final ladder | M-5, billing |
| A-12 | LinkedIn publishing at launch goes through an approved scheduler until Marketing Developer Platform approval (2–8 weeks) | `docs/01` §3.4–3.5, `sources.yaml` `social.linkedin` notes | US-803, Sprint 3 |
| A-13 | The content-social agent definition reads `docs/30-social-*` but its owned output is `docs/32-*` (`docs/03` §2). The social playbook is expected at `docs/32-social-playbook.md` | Roster table in `docs/03` §2 is treated as authoritative | §8.5 |

Open questions raised by this PRD for the owner (new, not in `docs/00-PLAN.md`):

1. Confirm Sprint 3 scope and the 9-week horizon in §6, or cut Sprint 3 to Pro + alerts + billing and defer API,
   admin CRM views and intake to a Sprint 4.
2. Will X be funded (~$300/month at 50 link posts/day, `docs/01` §3.4)? If not, X is removed from US-803.
3. Should public detail pages for user-submitted (intake) proposals ever be public, or only sponsor-visible?
   PRD assumes opt-in public (US-1002 AC3).

---

## 8. Definition of done for the other Sprint 0 deliverables

Each deliverable is reviewed by the product-manager (or owner where `docs/03` §2 says so) against the list
below. "Done" means every item is present and checkable; items that cannot be met are stated as such with the
blocker named, not left silent.

### 8.1 Market research — market-researcher → `docs/11-market-*.md`

Done when:
1. Bottom-up counts of organisations for each of the six segments S1–S6 (§2), each with a source URL and date,
   and a calibrated confidence per number. The 1,000–2,000 US organisation bound from `docs/01` §3.4 is
   confirmed, narrowed or refuted with evidence.
2. Competitor teardown for at least Interconnection.fyi/GridTracker, Cleanview, Halcyon, Enverus PRISM, Energy
   Adepto, Paces: product, coverage, pricing, customers, funding, weaknesses, each fact from a primary source.
   No content obtained by scraping these products.
3. Pricing validation: the $150–250/month Pro and $5–15k/yr API bands from `docs/01` §3.4 are each supported or
   challenged with at least two anchors; willingness-to-pay by segment is stated with confidence.
4. A customer-discovery interview guide for the 20 conversations (Sprint 2) with questions that can produce a
   yes/no on M-4 ("names a workflow they would pay for").
5. The strongest case against the fusion thesis, updated from `docs/01` §5, in its own section.
6. A policy watch list (federal funding, ISO reforms, large-load rules) with the signal each item would send to
   M-3.
7. Recommends which segment leads the pre-sell (M-5) and why.
8. Line appended to `docs/CHANGELOG.md`.

### 8.2 Legal register and outreach rules — legal-compliance → `docs/13-legal-*.md`, `data/sources.yaml`

Done when:
1. Every Tier-1 row in `data/sources.yaml` has: `reuse` class, licence name, evidence URL, retrieval date, a
   verbatim quote of the operative clause, and a recommendation (`derived_only` | `raw_ok` | `api_only` |
   `do_not_publish`).
2. MISO, SPP, NYISO and ISO-NE terms are read from a browser and recorded as in item 1, or the blocker is
   recorded and the owner action named (`docs/02` §4).
3. PJM: the Redistribution License enquiry text is drafted for the owner to send, and the gate condition G-PJM
   (§3.3) is restated in the register.
4. Attribution text per source class (public domain, CC BY, OGL, NESO Open Data, EU reuse, CAISO credit) is
   supplied as strings the product can render (US-105).
5. Personal-data inventory: every field that may hold personal data across the schema seed and intake form,
   with retention and deletion rule (US-910 AC2), plus privacy-notice inputs.
6. Outreach checklist covering CAN-SPAM, PECR/GDPR, TCPA and platform automation/disclosure rules for Bluesky,
   LinkedIn, X, with a pass/fail item list every channel and every email template must satisfy (US-502 AC3,
   US-803 AC3, US-804).
7. Drafts of customer terms, API licence (US-704 AC2), and a data-partner term-sheet outline; each marked where
   counsel review is required.
8. Every item separates what the text says from what is inferred. Line appended to `docs/CHANGELOG.md`.

### 8.3 Architecture and ERD v0 — solutions-architect → `docs/20-architecture.md`, `docs/21-data-model.md`, `docs/adr/`

Done when:
1. Components follow connectors → normalisers → resolver → enricher → store → API/publisher with
  `data/sources.yaml` as the connector manifest (`docs/00-PLAN.md` principles); each component's deployment
  unit, failure mode and test seam is named.
2. ERD (Mermaid) covers proposal, proposal_source, opportunity, organization, event, match, source (`docs/02`
   §5) plus user, api_key, saved_search, alert_delivery, post_draft, task, and the CRM/ERP foreign-key
   boundary; every table carries `source_id`, `source_url`, `retrieved_at`, `licence` where it holds source
   data, and the doc says which tables do not and why.
3. Tier mechanism: the single visibility function (US-601) is specified, including where lag configuration
   lives and how RSS, API and export call it.
4. Publish states and gates (US-905) are modelled with the invariant stated formally.
5. API surface: list of v1 endpoints and auth/rate-limit approach sufficient to start the OpenAPI spec in
   Sprint 1 (US-702/703).
6. CRM/ERP adapter interface (US-903) defined with the operations listed there and the assumption A-4 named.
7. Per-source egress policy (plain HTTP, headless browser, residential egress) with the rule that no method
   circumvents an access control a source's terms forbid; MISO's Cloudflare case handled explicitly.
8. Cost estimate for the MVP footprint consistent with "low hundreds of dollars per month" in `docs/01` §3.4,
   or an explanation of the deviation.
9. ADR list with at least: language/runtime, database, queue/scheduler, search, hosting, IaC, observability,
   billing, CRM/ERP integration pattern; each ADR names alternatives considered.
10. Security and privacy design: secrets handling, key hashing (US-701 AC2), audit log (US-901 AC2), deletion
    (US-910). Line appended to `docs/CHANGELOG.md`.

### 8.4 Entity-resolution prototype — data-scientist → `docs/22-*.md`, `pipeline/resolve*`, `data/eval/`

Done when:
1. Runs on real data: CAISO and ERCOT queues via gridstatus plus EIA-860M, pulled with the conventions in
   `scripts/probe_sources.py`; the run is reproducible from a documented command.
2. Deterministic keys implemented in the order of `docs/02` §5 (EIA id; queue id + ISO; FERC docket; sponsor +
   county + capacity ± 10% + technology; fuzzy name); every merge is an event with before/after and a documented
   unmerge path.
3. Labelled evaluation sample of at least 200 record pairs in `data/eval/` with labelling rules written down;
   precision and recall reported as measured numbers against A-9; link rate reported against M-1 (≥ 60%).
4. Status harmonisation mapping table for the five live ISOs and EIA-860M into the lifecycle vocabulary (§4
   conventions), including the handling of blank SPP statuses and per-source caveats.
5. A first rule set for proposal ↔ opportunity matching (US-401 AC1) and a labelled sample of ≥ 100 pairs
   with measured precision, or a stated plan to reach it in Sprint 2 if opportunities data is not yet loaded.
6. Every number in the doc was produced by a run in this repo, not estimated. Line appended to `docs/CHANGELOG.md`.

### 8.5 Social playbook — content-social → `docs/32-social-playbook.md` (see A-13)

Done when:
1. Editorial rule for which event types merit a post, with a template per type: new proposal, status change,
   RFP opened, award, cancellation/freeze/reinstatement, weekly digest (US-801 AC1).
2. Channel playbooks for Bluesky, LinkedIn (approved API or approved scheduler), X (capped budget, per-day cap
   value), and owned RSS/email as the primary channel; each with cadence, format limits, cost and the
   disclosure/automated-account label text (`docs/01` §3.5, `sources.yaml` social entries).
3. Post pipeline spec: event → draft → review queue → publish → metrics, matching US-801–803, with the
   default "review required" and the owner-only lift per channel.
4. Engagement rules: replies drafted for human approval only; never argue; never speculate about parties named
   in filings; corrections policy with a correction template (US-804).
5. Every post links to a page with provenance and an alert sign-up (US-503 AC2, US-801 AC2).
6. Weekly report spec: reach, clicks, alert sign-ups per channel via UTM (M-8), and the fields the pipeline
   must store to compute it (US-803 AC2).
7. Passes the legal-compliance outreach checklist (§8.2 item 6). Line appended to `docs/CHANGELOG.md`.

### 8.6 GTM playbook — sales-bd → `docs/33-gtm-*.md`

Done when:
1. ICP and a one-page playbook per segment S1–S6: who inside the organisation buys, the trigger event on the
   platform that justifies contact (`docs/03` §3 step 9), the offer (Pro, API, or routing), and objections.
2. Curated RFP issuer list started toward the target of 50 (`docs/00-PLAN.md` next actions) with issuer, URL,
   cadence and how it was found; this list seeds US-303.
3. Plan for the 20 customer conversations in Sprint 2 (names or roles by segment, sequencing, the interview
   guide from §8.1 item 4) and for the 10-organisation pre-sell in Sprint 3 at $150–250/month (M-5).
4. Outreach sequences and talk tracks drafted for the owner to send from their own accounts, personalised with
   cited platform facts, each passing the outreach checklist (§8.2 item 6); no message is sent by an agent.
5. Partnership targets (GridTracker, Cleanview, law firms, lenders) with a term-sheet outline per target type;
   no scraping of partner products.
6. CRM field list needed for M-4, M-5 and M-13 to be computed from the system of record via the adapter (A-4),
   and a weekly pipeline report template.
7. Only business contact details from public professional sources. Line appended to `docs/CHANGELOG.md`.

---

## 9. Change history

- 2026-09-12 v0 — first draft for owner review (product-manager).
