# Bankable — master plan (working memory)

This file is the durable memory for the project. Sessions are ephemeral; this is not. Update it whenever a
decision is made or a phase closes. Read it first when resuming.

## Vision (as briefed, 2026-09-12)

A platform that continuously discovers active energy and related-infrastructure **proposals** (supply) and
**opportunities** (demand: RFPs, funding, tenders) from public registers, dockets and the web; publishes them
with a delayed free tier and live paid/API tiers; and syndicates to social channels. It stands on its own as a
product. The existing Bankable deal workflow at bankablehq.com (analyse → certify → route → fund) is a **later
consideration** for integration, referenced where relevant, not a foundation of this platform (owner, 2026-09-12).

Feasibility verdict (Phase 0): **proceed** as a fused proposal-and-opportunity graph with a change feed, not a
bare listings site (`01-feasibility.md` §5). The feasibility study framed the graph as a lead engine for the
existing Bankable workflow; the owner has since directed that the platform stand alone and that the Bankable
workflow be a later consideration only. The graph, feed, tiers and distribution are unchanged by that.

## Phases

| # | Phase | Deliverables | Status |
|---|---|---|---|
| 0 | Feasibility & data sources | `01-feasibility.md`, `02-data-sources.md`, `data/sources.yaml`, `scripts/probe_sources.py`, probe evidence | **done 2026-09-12** |
| 1 | Business plan & model | market sizing, pricing ladder, unit economics, GTM, legal entity/licensing plan, 24-month financial model, risk register | market + pricing + GTM done (`11`, `33`); legal register in progress; financial model and risk register outstanding |
| 2 | Architecture & specifications | system spec, domain model + ERD, API spec (OpenAPI), ingestion/normalisation/resolution design, security & privacy design, DevOps (IaC, CI/CD, observability, backups), ADRs | **done** except DevOps design (blocked on ADR 0005 hosting decision): `20`, `21`, `22`, `23`, `api/openapi.yaml` (118 ops, validated), six ADRs |
| 3 | Product design | UX research, information architecture, UI system, public site, search/map/alerts, API docs, admin panel (users, customers, subscriptions, data ops), CRM/ERP integration as single source of truth, analytics/tracking plan | design docs **done**: `30-design-references`, `30-design-ia`, `31-design-system`; screens are Sprint 2 build |
| 4 | Build & launch MVP | Tier-1 ingestion, proposal graph, public delayed tier, Pro alerts, syndication, billing, launch runbook | ingestion **done** (12 sources, gated); store + public API **done** (`services/`); public site prototype **done** (`web/`, reads parquet, not yet the API); syndication to the edge of posting **done**; remaining: wire site to API, cross-source resolution in the store, alerts, Pro tier, billing, admin, DevOps, launch runbook |
| 5 | Later consideration: deal workflow | project intake beyond the light MVP form, scoring, capital-partner routing, and any integration with the existing Bankable app. Not foundational; revisit after Phase 4 on evidence | deferred by owner |
| 6 | Operations | SLAs, support, data QA ops, licence renewals, compliance calendar, content/social cadence | |

## Standing architecture principles (to be honoured in Phase 2)

- Modular backend: connectors → normalisers → resolver → enricher → store → API/publisher, each independently
  deployable and testable, with `sources.yaml` as the connector manifest.
- Every record carries provenance and licence; attribution is rendered automatically.
- Public tier = derived data; restricted raw rows link out.
- Change events are first-class; the product is the feed, not the table.
- Admin panel writes customers/subscriptions to the CRM/ERP system of record and reads back; the app database is
  not the SSOT for commercial records.

## Decisions log

| Date | Decision | Rationale |
|---|---|---|
| 2026-09-12 | Repo `tobombadil/bankable` holds the project; `tobombadil/tobombadil` (personal site) untouched | Bankable brand already exists at bankablehq.com |
| 2026-09-12 | Scope is technology-agnostic incl. load/data centres, transmission, gas/LNG, nuclear, storage, CCS | Policy lens: growth is outside wind/solar in 2026 |
| 2026-09-12 | US-first; three international API feeds (TED, FTS, NESO) in MVP to prove multi-market model | Engineering vs market lens resolution |
| 2026-09-12 | Never scrape private aggregators; PJM only after licence; MISO only after terms + compliant egress | Legal lens |
| 2026-09-12 | Social at launch = Bluesky + LinkedIn (+ X on capped budget); owned RSS/email is the primary channel | Media lens |
| 2026-09-12 | No new repo: `tobombadil/bankable` (one worker commit) becomes the platform monorepo; the Cloudflare worker moves to `infra/` later | Empty repo already carries the brand; a second repo would split history and access |
| 2026-09-12 | Build-time agent team lives in `.claude/agents/`; the run-time loop is a conventional pipeline that calls models only where judgement is needed; humans send all outbound messages until a channel is explicitly enabled | `docs/03-agent-operating-model.md` |
| 2026-09-12 | **Owner confirmed the Phase 0 reframe**: build the fused proposal graph feeding Bankable's route-to-capital workflow, with the public listings site as the free delayed tier and audience engine, not as the main event | Owner, in session. Settles the branch point that architecture and pricing both depend on |
| 2026-09-12 | **Map is a primary navigation surface**, not a secondary view: every proposal and opportunity with a point, county or service territory is browsable on a map with the same filters and tier rules as search (elevates PRD US-104) | Owner request |
| 2026-09-12 | Sprint 1 adds `docs/04-standards.md`: the cross-discipline best-practice standard the whole team follows (design system and accessibility, engineering and testing, data modelling and provenance, API design, security/privacy, DevOps, GTM and content). Owned jointly by product-manager, solutions-architect, product-designer; every later deliverable is reviewed against it | Owner request |
| 2026-09-12 | **The existing Bankable workflow is not foundational.** The platform is designed, priced and built to stand alone; the analyse → certify → route → fund workflow at bankablehq.com is a later consideration referenced only where a hook is cheap (the light intake form, a CRM lead hand-off). Supersedes the wording of the reframe row above; the graph-plus-feed product is unchanged | Owner direction |
| 2026-09-12 | **Legal register outcome (`docs/13-legal-data-rights.md`)**: only ERCOT is cleanly raw-publishable; CAISO and NYISO derived-only with credit; SPP and ISO-NE are *restricted* (SPP: any commercial use needs written authorisation; ISO-NE: duplication clause); PJM gated on licence; MISO unknown (site unreachable headlessly, robots.txt carries a DSM Art. 4 reservation). Under the working default, launch supply coverage on public pages is ERCOT raw + CAISO/NYISO derived + LBNL history + GEM; SPP, ISO-NE, PJM, MISO show nothing on any tier until cleared. Google News RSS is disallowed by robots.txt and is dropped as a connector; wire services bar scraping, so news is a lookup trigger only, never content. Twelve counsel items listed in doc A, ten in doc B; counsel question #1 is whether PJM's planning pages (site notice has no redistribution clause) can be used without a Redistribution Licence | Legal lens; changes launch scope and the pricing-ladder ISO row |
| 2026-09-12 | **The platform gets a new name.** "Bankable" is the repo and working codename only. Every hostname, handle, sender address and brand reference in `docs/23`, `docs/32`, `docs/33` and the agent briefs is a placeholder to be replaced by a single find-and-replace once the name is chosen. Naming precedes the design references study, because the typographic identity depends on the wordmark | Owner direction |
| 2026-09-12 | **Resolution prototype measured (`docs/22`)**: 14,388 records normalised; precision 0.927 / recall 0.950 at threshold 75 on 85 hand labels (in-sample, must be re-measured held-out); 337 clusters, 8 double-queued NYISO↔ISO-NE projects found. Queue-to-EIA link rate is **7.2%** of active ISO records, far below the PRD's 60% bar, but the bar was mis-specified: EIA-860M only lists units near construction, so early-stage queue rows have no counterpart to link to; on the EIA side 34.7% of planned units in these five areas link. SPP links 0% because SPP publishes no names or sponsors (and is legally restricted anyway). **Proposed, pending owner:** recalibrate PRD M-1 to (a) link rate among records at `contracted`/`under_construction`, (b) precision ≥0.95 / recall ≥0.85 on a 600-label held-out set, (c) add FERC docket linkage, untested so far. The fusion thesis survives; the metric definition did not | data-scientist measurement; coordinator proposal |
| 2026-09-12 | **Connector framework live (`pipeline/connectors/`)**: 10 connectors for open-licence sources ran against live endpoints today — ERCOT GIS 1,778 rows, CAISO 2,278, NYISO 1,814, EIA-860M 2,341, NESO TEC 2,198, grants.gov 151, TED 698, Find a Tender 11, World Bank 100, ERCOT large-load 0. Publication gating is enforced in code: restricted/unknown sources raise unless explicitly allowed and can only write to quarantine; private aggregators cannot register. DQ gates: 9 pass, NYISO warn (2 duplicated queue positions, 1,350 padding rows dropped), 0 held. 208 tests, ruff and strict mypy clean. Finding: ERCOT publishes no large-load status report as a data product (the registry's product id did not exist; NPRR1267 is met by a monthly PDF deck); the connector watches the catalogue and emits an event when one appears | data-engineer, verified by coordinator |
| 2026-09-12 | **Placeholder name adopted: Infraqueue** (round three, `docs/12`). Unregistered .com, free handle, no collisions found. Placeholder only; counsel search and alternate-TLD re-check precede permanent adoption. Unblocks the design references study | Coordinator, per owner's "pick a placeholder after this pass" |
| 2026-09-12 | **Sprint 2 started, public site prototype first.** Owner chose to see the map-first site against real connector data before the backend. Decisions D1–D6 proceed on their defaults (VMs + Compose; HubSpot + Stripe via adapter; five-family status colours; Newsreader + IBM Plex; M-1 recalibrated; Infraqueue placeholder). D7 and D8 remain owner actions. One agent at a time | Owner, in session |
| 2026-09-12 | **Public site prototype landed (`web/`)**: map-first public delayed tier on today's real data — 10,409 proposals (2,341 exact points, 5,464 county centroids, 378 in state aggregates, 2,207 unplaced; NESO's 2,198 rows carry no usable UK geography), 960 opportunities; provenance, attribution and the source licence quote render from data on every page; tests, lint and a Playwright smoke path pass. Known defects: basemap tiles did not render in the sandbox (fix in progress); Google Fonts CDN instead of self-hosted (pre-production fix); client-side clustering until the geo endpoint exists; no manual dark toggle | frontend-developer, verified by coordinator |
| 2026-09-12 | **Docket linkage measured (`pipeline/link_dockets.py`)**: FERC eLibrary 131 filings (30-day ER/CP window) and Permitting Dashboard 104 energy/transmission projects fetched live; **1 of 1,673 active queue records (0.06%) gained a docket link**. FERC search descriptions rarely name a queue id or project, so the description field is a weak key; a full-text or filer-name backfill over years, not 30 days, is the honest next test before docket linkage is counted in M-1. Two registry corrections: FERC's filterDate has no effect (filter client-side) and any sortBy other than empty returns success:false; the Permitting Dashboard resource id was retired, live table is mcm3-xbid | data-engineer, measured |
| 2026-09-12 | **Backend store and public API landed (`services/`)**: 13 tables from `docs/21` with provenance and licence not-null, append-only events, canonical Postgres 16 + PostGIS migration (not yet run against a live database; tests run on SQLite through dialect-aware types), an idempotent loader that mirrors the connector gate so a restricted source cannot enter the store by a second path, and 25 public-tier operations from `api/openapi.yaml` with a contract test validating every response against the spec, including geo clustering and RSS/JSON feeds. 59 tests; 83% branch coverage, 100% on the visibility predicate and both gate-refusal paths. Eleven decisions taken and listed in `services/README.md`, the material ones being: no cross-source resolution in the store yet (one record per source), records default to public until the admin publish workflow exists, unimplemented filters return 400 rather than silently ignoring | backend-developer, verified by coordinator |
| 2026-09-12 | **Syndication pipeline landed (`services/social/`)** to the edge of posting: nine event types earn drafts, per-channel templates from structured fields only, review queue with dedupe and a graduation check that refuses auto-publish, dry-run publishers that never send. 138 tests. Owner actions before the first real post: create accounts per `docs/32` §2, hand back secrets, decide review-mode channels | content-social, verified by coordinator |
| 2026-09-13 | **Cross-source resolution in the store (`services/resolve/`)**, reversible: 8,212 proposals → 7,770 after 275 clusters merged (442 records absorbed); 273 proposals now carry two or more sources; 3 clusters routed to human review; organisations 3,034 → 2,784. Every merge is an append-only event holding the absorbed row; unmerge proven exact in a test. Precision/recall through the store on the usable labels 0.946/0.946. **Assumption A-22-5:** the id-reuse guard was implemented as "same source, same queue id, two distinct records" rather than the brief's literal "same source, different queue ids", because the literal rule would have refused 73 of 281 legitimate clusters; the implemented rule fires on exactly the 2 real NYISO cases. Two loader limitations documented for the backend (id-uniqueness collapses true duplicates before the gate; org slugs collide on punctuation) | data-scientist, measured; coordinator accepts A-22-5 |
| 2026-09-12 | **Design must be distinctive, not the default AI look.** A custom package of typefaces, spacing scale and layouts, built from a documented study of the best interactive data products in and around the industry. Explicit anti-patterns are banned (generic sans on purple gradients, uniform rounded cards, three-tile hero, emoji bullets, stock illustration). Deliverable: `docs/30-design-references.md` before any screen is drawn | Owner request |
| 2026-09-12 | ADRs 0001–0004 accepted as working decisions: Python 3.12 + FastAPI + SQLAlchemy; Postgres 16 with PostGIS + object storage for raw snapshots; Procrastinate (Postgres-backed) job queue. ADR 0005 (hosting) and 0006 (CRM/ERP adapter, HubSpot + Stripe recommended) remain proposed pending owner | `docs/adr/`; owner may overturn |
| 2026-09-12 | Working defaults pending owner ratification: public-tier lag is 7 days for opportunities and 14 days for supply rows (per `docs/11` §3, resolves docs/21 C-4); restricted or unknown-terms sources (PJM, MISO, SPP, NYISO, ISO-NE until terms recorded) return nothing on any tier, including Pro and API, until legal-compliance records permission (resolves docs/21 C-3 on the safer reading) | Safer reading; reversible configuration |

## Sprint 1 close-out (2026-09-12) and the decisions that gate Sprint 2

Sprint 1 delivered: `04-standards` (126 rules), `12-naming` (three rounds; Infraqueue placeholder), `13-legal` ×2,
`22-resolution` (measured), `30-design-references`, `30-design-ia`, `31-design-system`, `api/openapi.yaml`
(118 operations, 44/44 stories, validates), `pipeline/connectors` (10 sources live, 208 tests, lint and strict
types clean). Sprint 2 is the first *build* sprint: Postgres store and migrations from `docs/21`, the FastAPI
service from `api/openapi.yaml`, the map-first public site from `docs/30`/`31`, change events end to end,
alerts, and the first syndication channel. It is also the most expensive sprint in tokens.

Decisions that should be made before Sprint 2 starts, in order of how much they constrain the build:

| # | Decision | Default if the owner says nothing | Constrains |
|---|---|---|---|
| D1 | Hosting posture (ADR 0005): VMs + Compose + OpenTofu (solo/contractor) vs managed container platform (funded) | VMs + Compose | DevOps design, CI/CD, cost ceiling |
| D2 | CRM/ERP system of record (ADR 0006): HubSpot + Stripe / Odoo / ERPNext / Twenty + Stripe | HubSpot + Stripe (adapter isolates the choice) | Admin panel, billing, Sprint 3 |
| D3 | Ratify the five-family status colour system (`docs/30` §6.2, `docs/31` §1.2) | Adopted | Every status chip, map marker, legend |
| D4 | Ratify keeping Newsreader + IBM Plex (`docs/30` §4) | Keep | Wordmark, all typography |
| D5 | Metric M-1 recalibration (`docs/22`; late-stage link rate, 0.95/0.85 on 600 held-out labels, docket linkage) | Adopt recalibration | What "resolution good enough to publish merges" means |
| D6 | Name: Infraqueue permanent, or Infrafeed, or keep placeholder through Sprint 2 | Placeholder through Sprint 2; register both .coms now | Wordmark timing, handle registration |
| D7 | Legal actions (task #4): PJM licence enquiry, SPP authorisation, MISO terms in a browser, counsel brief on 22 items | Owner-only; nothing in Sprint 2 depends on them except PJM/SPP rows appearing | Launch coverage |
| D8 | Free API keys to register: EIA v2, SAM.gov, NRC ADAMS, regulations.gov; LinkedIn MDP application; Bluesky account | Owner-only | Tier-2 connectors, syndication |

## Open questions for the owner (answers change Phase 1–3 work)

1. **Name.** Placeholder is **Infraqueue** (`docs/12` round three); `{{PRODUCT}}` / `{{DOMAIN}}` remain in code and
   specs until permanent adoption. Owner may swap to Infrafeed or another at any time before the wordmark is designed. Checks before adoption: .com or equivalent domain, USPTO and EUIPO
   trademark clearance in class 42/35, handle availability on Bluesky, LinkedIn, X, and no collision with an
   energy-sector company. The bankablehq.com app is unrelated to this platform's foundation (see decision above).
2. Budget and team: solo founder plus contractors, or a funded build? This sets the Phase 4 scope.
3. Legal entity and jurisdiction for data licensing contracts (PJM, GridTracker partnership, customer terms).
4. CRM/ERP system of record. Candidates: HubSpot (CRM) + QuickBooks/Xero (finance) for speed; Odoo or ERPNext for
   one open-source CRM+ERP; Twenty (open-source CRM) + Stripe Billing. Decision needed before admin-panel design.
5. Appetite for a GridTracker/Cleanview data partnership versus building non-ISO coverage in-house.
6. Geography priority after US: EU/UK first (APIs exist) or Canada/Australia (adjacent markets, your network)?

## Immediate next actions (Phase 1 entry)

- Register free API keys: PJM Tools, EIA v2, SAM.gov (api.data.gov), NRC ADAMS, regulations.gov; apply for
  LinkedIn Marketing Developer Platform; create Bluesky account.
- Terms recorded for all but MISO (`docs/13-legal-data-rights.md`). Open the PJM Redistribution License enquiry and the SPP written-authorisation request; obtain MISO terms in a browser; brief counsel on the twelve listed items before any restricted row is shown.
- Draft the curated RFP issuer list (target 50) from your origination network.
- Start Phase 1 business plan using the price anchors and TAM bounds in `01-feasibility.md` §3.4.

## Repo layout

```
docs/00-PLAN.md            this file
docs/01-feasibility.md     Phase 0 feasibility study
docs/02-data-sources.md    Phase 0 source catalogue, legal register, schema seed, ingestion order
docs/03-agent-operating-model.md  build-time agent team vs run-time pipeline; roster, hand-offs, human gates
docs/10-prd-mvp.md         MVP requirements: 44 user stories, scope gates, metrics
docs/11-market-and-competition.md  competitors, bottom-up sizing, pricing ladder, delay schedule
docs/20-architecture.md    run-time system specification
docs/21-data-model.md      ERD, field tables, lifecycle state machines, event log, tier gating
docs/23-api-spec-outline.md  API surface mapped to PRD stories
docs/22-entity-resolution-and-change-detection.md  measured resolution and diff results
docs/13-legal-data-rights.md  per-source quoted terms, publication matrix, counsel items
docs/13-legal-outreach-and-social.md  channel gate checklist, human-sends rules
docs/adr/                  architecture decision records
docs/32-social-operating-playbook.md  channels, account checklist, editorial, post pipeline
docs/33-gtm-and-sales-playbook.md     ICPs, outreach, partnerships, automation boundary
pipeline/                  ingestion and resolution code; status_map.yaml is versioned data
.claude/agents/            agent role definitions (invoke via the Agent tool or /agents)
CLAUDE.md                  instructions every session/agent loads first
data/sources.yaml          machine-readable source registry (connector manifest)
data/probes/<date>.json    evidence from scripts/probe_sources.py
scripts/probe_sources.py   reproducible reachability + gridstatus check
.github/workflows/         existing Cloudflare worker proxying to the Lovable app
```
