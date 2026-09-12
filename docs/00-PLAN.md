# Bankable — master plan (working memory)

This file is the durable memory for the project. Sessions are ephemeral; this is not. Update it whenever a
decision is made or a phase closes. Read it first when resuming.

## Vision (as briefed, 2026-09-12)

A platform that continuously discovers active energy and related-infrastructure **proposals** (supply) and
**opportunities** (demand: RFPs, funding, tenders) from public registers, dockets and the web; publishes them
with a delayed free tier and live paid/API tiers; syndicates to social channels; and grows into the existing
Bankable product (bankablehq.com: analyse → improve → certify → route → fund) where sponsors submit projects
tailored to specific opportunities and capital partners.

Feasibility verdict (Phase 0): **proceed, reframed** as Bankable's proposal graph and lead engine rather than a
standalone listings site. See `01-feasibility.md` §5.

## Phases

| # | Phase | Deliverables | Status |
|---|---|---|---|
| 0 | Feasibility & data sources | `01-feasibility.md`, `02-data-sources.md`, `data/sources.yaml`, `scripts/probe_sources.py`, probe evidence | **done 2026-09-12** |
| 1 | Business plan & model | market sizing, pricing ladder, unit economics, GTM, legal entity/licensing plan, 24-month financial model, risk register | market + pricing + GTM done (`11`, `33`); legal register in progress; financial model and risk register outstanding |
| 2 | Architecture & specifications | system spec, domain model + ERD, API spec (OpenAPI), ingestion/normalisation/resolution design, security & privacy design, DevOps (IaC, CI/CD, observability, backups), ADRs | Sprint 0 in progress (architecture, ERD, ADRs, resolution prototype) |
| 3 | Product design | UX research, information architecture, UI system, public site, search/map/alerts, API docs, admin panel (users, customers, subscriptions, data ops), CRM/ERP integration as single source of truth, analytics/tracking plan | |
| 4 | Build & launch MVP | Tier-1 ingestion, proposal graph, public delayed tier, Pro alerts, syndication, billing, launch runbook | |
| 5 | Bankable marketplace | project submission, opportunity matching, scoring/certification, capital-partner routing; merge with the current Lovable app or replace it | |
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

## Open questions for the owner (answers change Phase 1–3 work)

1. Relationship to the current Lovable app at bankablehq.com: rebuild on the new architecture, or keep it as the
   front door and feed it via API? What does it contain today (users, data, code ownership)?
2. Budget and team: solo founder plus contractors, or a funded build? This sets the Phase 4 scope.
3. Legal entity and jurisdiction for data licensing contracts (PJM, GridTracker partnership, customer terms).
4. CRM/ERP system of record. Candidates: HubSpot (CRM) + QuickBooks/Xero (finance) for speed; Odoo or ERPNext for
   one open-source CRM+ERP; Twenty (open-source CRM) + Stripe Billing. Decision needed before admin-panel design.
5. Appetite for a GridTracker/Cleanview data partnership versus building non-ISO coverage in-house.
6. Geography priority after US: EU/UK first (APIs exist) or Canada/Australia (adjacent markets, your network)?

## Immediate next actions (Phase 1 entry)

- Register free API keys: PJM Tools, EIA v2, SAM.gov (api.data.gov), NRC ADAMS, regulations.gov; apply for
  LinkedIn Marketing Developer Platform; create Bluesky account.
- Read and record terms for MISO, SPP, NYISO, ISO-NE in `sources.yaml`; open PJM Redistribution License enquiry.
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
