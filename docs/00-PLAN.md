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
| 2 | Architecture & specifications | system spec, domain model + ERD, API spec (OpenAPI), ingestion/normalisation/resolution design, security & privacy design, DevOps (IaC, CI/CD, observability, backups), ADRs | **done** incl. DevOps (`docs/60`, `infra/`): `20`, `21`, `22`, `23`, `api/openapi.yaml` (118 ops, validated), six ADRs |
| 3 | Product design | UX research, information architecture, UI system, public site, search/map/alerts, API docs, admin panel (users, customers, subscriptions, data ops), CRM/ERP integration as single source of truth, analytics/tracking plan | design docs **done**: `30-design-references`, `30-design-ia`, `31-design-system`; screens are Sprint 2 build |
| 4 | Build & launch MVP | Tier-1 ingestion, proposal graph, public delayed tier, Pro alerts, syndication, billing, launch runbook | **Sprint 2 done**: ingestion, store, resolution, public API, site on full data via API, syndication to the edge, Pro tier + alerts + webhooks, deployment code and CI. **Sprint 3**: billing, admin panel, login surface, alert/social workers, launch runbook |
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
| 2026-09-13 | **Site now reads the public API end to end** (`web/api_client.py`); default map shows active states only with withdrawn/cancelled behind a toggle and counts stated; delayed tier governed by the API with a labelled dev override; provenance panel collapsed. **Found in the process, being fixed in the backend:** (1) the visibility predicate costs 30–75 s per page over 10,400 rows, forcing the site onto a sample — a real bug; (2) the loader hardcodes every source as raw-publishable with exact precision, contradicting the licence classes for CAISO and NYISO — a licence-correctness bug the site had to patch around; (3) no geocoding in the loader; (4) geo clustering collapses the continent into three circles at default zoom; (5) API gaps: slug lookup, bbox enforcement, technologies filter, organisation detail, licence text on sources. Screenshots currently show sample data for this reason | frontend-developer; coordinator |
| 2026-09-13 | **Deployment design and infrastructure landed (`infra/`, `docs/60-deployment.md`, CI)**: three Hetzner VMs + Compose, Cloudflare DNS/R2, managed Postgres, SOPS/age secrets, manifest-driven scheduler, backup and restore drill, five runbooks; CI enforces every `docs/04` gate plus a nightly fixture-only connector run. Validated here: OpenTofu fmt/init/validate, Compose config merge, Dockerfile lint, workflow lint, SOPS round-trip, 13 scheduler tests. Not validated: a real `tofu apply` or `docker build` (no accounts, no daemon). Cost ≈ USD 80–170/month now, 140–320 once the model gateway ships, under the ceiling. ADR 0005 accepted. A throwaway private key committed by the agent was removed and the keys directory gitignored. Owner's first steps: Hetzner project + Cloudflare tokens as GitHub secrets, then `tofu plan`; a managed Postgres project and the first `alembic upgrade head`; real age keys via the bootstrap script | devops-engineer; coordinator |
| 2026-09-13 | **Backend fixes landed**: the 30–75 s page time was one missing index on the source-link tables (confirmed by query plan), now **21.84 s → 0.127 s** per list page on the full 10,409-row load; geo endpoint 0.35–0.53 s on the full set with column-limited loading and SQL aggregates; clustering retuned to 22 clusters at zoom 3 and 56 at zoom 4 with bbox enforced; raw-publication and precision now derived per source from the registry (CAISO and NYISO derived-only, GB NESO raw-ok, so the rule is per-source terms, not a blanket class rule); loader geocodes county and state centroids (8,183 of 8,211 US proposals placed); slug filter, technologies filter (a bind-type bug fixed on the way), licence quote text exposed; publish_state defaults from registry class. 487 core tests. Three of the site's four data-path overrides are now redundant; EIA exact-point promotion remains site-side until a later sprint | backend-developer, verified by coordinator |
| 2026-09-13 | **Site on the full dataset**: 10,409 proposals loaded; default view 5,837 active, 3,215 withdrawn/cancelled hidden behind the toggle, 1,357 built/unknown outside the default, 1,825 unplaced (NESO carries no usable UK geography; some US rows lack county and state); 22 clusters at default zoom; page times ≈0.5 s home and list, ≈0.02 s detail through the in-process API; licence text verbatim in provenance; redundant overrides removed. **Follow-up for Sprint 3:** the loader runs at ≈100 rows/s (the full-data test takes 451 s), fine for a daily batch but worth a bulk-insert pass; UK geography for NESO needs a postcode or substation gazetteer | frontend-developer, verified by coordinator |
| 2026-09-13 | **Pro tier and alerts landed (`services/api/pro.py`, `services/alerts/`)**: session and API-key auth with scopes, hashed keys, rotation and an audit log; tier-aware visibility (Pro and API see records at publication, public after the lag; restricted sources invisible on every non-admin tier); real per-tier token buckets; saved-search CRUD with preview; alert evaluation against change events with email dry-run and private token feeds; signed webhooks with retry and delivery log; an interim admin-only entitlement endpoint until billing exists. 97 new tests; 588 core tests; spec at 119 operations. **Sprint 2 is complete.** | backend-developer, verified by coordinator |
| 2026-09-13 | **CRM system of record is Attio, not HubSpot** (owner). Stripe Billing stays for subscriptions. The anti-corruption adapter in `docs/20` §9 / ADR 0006 was designed so this swap is bounded: `services/api` reads entitlements through a port; the Attio adapter is the first concrete CRM adapter. Attio exposes a public REST API v2 (objects, records, lists, notes, tasks, webhooks) with API-key auth; the Sprint 3 backend agent verifies the exact endpoints against https://docs.attio.com before coding and records rate limits. `docs/33` §7 CRM plan and `docs/20` §9 comparison table are updated by reference here rather than rewritten; HubSpot mentions elsewhere are superseded | Owner decision |
| 2026-09-13 | **Sprint 3 runs in a fresh session** (owner). This file is the hand-off; see "Sprint 3 kickoff" below | Owner decision |
| 2026-09-13 | **Sprint 3 started in the existing session** after all (owner: "let's start"). First wave on disjoint file areas: `services/crm/` (Attio adapter against `docs/34`), `services/billing/` (Stripe adapter), `web/` (login and registration). Each adapter exposes a router; the coordinator mounts them in `services/api/app.py` after both land to avoid a shared-file collision | Owner, in session |
| 2026-09-12 | **Design must be distinctive, not the default AI look.** A custom package of typefaces, spacing scale and layouts, built from a documented study of the best interactive data products in and around the industry. Explicit anti-patterns are banned (generic sans on purple gradients, uniform rounded cards, three-tile hero, emoji bullets, stock illustration). Deliverable: `docs/30-design-references.md` before any screen is drawn | Owner request |
| 2026-09-12 | ADRs 0001–0004 accepted as working decisions: Python 3.12 + FastAPI + SQLAlchemy; Postgres 16 with PostGIS + object storage for raw snapshots; Procrastinate (Postgres-backed) job queue. ADR 0005 (hosting) and 0006 (CRM/ERP adapter, HubSpot + Stripe recommended) remain proposed pending owner | `docs/adr/`; owner may overturn |
| 2026-09-12 | Working defaults pending owner ratification: public-tier lag is 7 days for opportunities and 14 days for supply rows (per `docs/11` §3, resolves docs/21 C-4); restricted or unknown-terms sources (PJM, MISO, SPP, NYISO, ISO-NE until terms recorded) return nothing on any tier, including Pro and API, until legal-compliance records permission (resolves docs/21 C-3 on the safer reading) | Safer reading; reversible configuration |

## Sprint 3 kickoff — read this first in the fresh session

**State:** 74 commits on `claude/energy-proposals-platform-lf7lhb`, everything verified green (588 core tests,
18 site tests, ruff, mypy strict on 95 files, OpenAPI valid at 119 operations, 44/44 stories). Nothing is
deployed; no accounts exist. Placeholder name Infraqueue; `{{DOMAIN}}` in code.

**Sprint 3 scope (in order):**
1. **Attio + Stripe adapters** behind the existing ports (Attio schema contract: `docs/34-crm-system-of-record.md`, built by the owner): `services/crm/attio.py` (accounts, contacts, deals,
   notes, tasks; webhooks inbound), `services/billing/stripe.py` (checkout, portal, subscription webhooks →
   `account.entitlement`). The interim admin entitlement endpoint stays as the manual override. Sales lead
   hand-off (`docs/10` US-403) writes to Attio. Verify Attio API v2 endpoints and rate limits first.
2. **Login and registration surface** in `web/` on the existing session auth; email verification through the
   EmailPort (Resend adapter, dry-run without a key).
3. **Admin panel** (`web/admin/`, `/admin/v1`): source health, publish gate with the `legal` role, merge review
   (`resolution_decision` rows), social post review queue, tasks/intake, users and accounts via the Attio and
   Stripe ports, cost log. Minimal UI in the same design system; every story US-901 to US-910.
4. **Workers**: scheduled alert cycle and delivery; social draft generation from events; loader bulk-insert
   pass (≈100 rows/s today); all wired into `infra/` scheduler.
5. **Data follow-ups**: EIA exact-point promotion in the loader; NESO geography via a substation/postcode
   gazetteer; FERC multi-year filer-name backfill to retest docket linkage.
6. **Launch runbook** (`docs/4x`): coverage statement (S3-4), pricing page copy from `docs/11` §3, the owner's
   account checklist (`docs/32` §2), first deploy per `docs/60` §11, go/no-go checklist from `docs/04` §9.

**Working rules that kept this repo clean, keep them:**
- One agent per file area; the brief names exactly which paths it may write. Collisions were zero across 20+
  agents because of this. `docs/CHANGELOG.md` is append-only and shared.
- Briefs say "read only these files"; agents on Sonnet for execution; the coordinator verifies every
  deliverable (tests, lint, spec) before committing, and commits with descriptive messages.
- Never let an agent commit; never let a private key, `.coverage`, `web/.data/` or per-run outputs into git.
- Measured numbers only; a rule the agent finds wrong by measurement is changed and the change is logged here.

**Owner actions still open:** task #4 legal items (PJM licence enquiry, SPP authorisation, MISO terms in a
browser, counsel brief on 22 items); free API keys (EIA v2, SAM.gov, NRC, regulations.gov); LinkedIn MDP
application; Bluesky account; Attio workspace and API key; Stripe account; cloud accounts and tokens for the
first deploy; register infraqueue.com and infrafeed.com.

## Sprint 2 close-out (2026-09-13) and the decisions that gate Sprint 3

Sprint 2 delivered the working system end to end on real data: 12 connectors gated by licence class; store, loader
and resolution with reversible merges; public API at 119 operations, contract-tested; the map-first public site on
the full 10,409-proposal load with the design pass applied; syndication to the edge of posting; Pro tier, alerts
and webhooks; deployment code, CI gates and runbooks. Every layer is verified by the coordinator, not only by the
agent that wrote it.

Sprint 3 is billing, admin and launch. Decisions that shape it:

| # | Decision | Default if the owner says nothing | Constrains |
|---|---|---|---|
| S3-1 | Billing provider: Stripe Billing (recommended in `docs/20` §15) or a merchant-of-record (Paddle, Lemon Squeezy) for EU VAT handling | Stripe | Entitlement adapter, checkout, portal |
| S3-2 | CRM/ERP system of record | **Decided: Attio (CRM) + Stripe (billing)**; accounting stays out of scope until revenue exists | Attio adapter, admin customer views, sales playbook §7 |
| S3-3 | Admin panel scope for launch: source health, publish gate, merge review, post review queue, tasks, users/accounts, cost log (`docs/10` US-901 to US-910) | All ten stories, minimal UI | Sprint 3 size |
| S3-4 | Launch coverage statement: ERCOT raw, CAISO and NYISO derived, EIA, NESO, funding and tenders; SPP/ISO-NE/PJM/MISO absent until cleared (task #4) | As stated | Pricing page copy, sales talk-track |
| S3-5 | Name made permanent (Infraqueue, or Infrafeed) after counsel search and domain registration | Register both .coms now; decide before wordmark | Wordmark, handles, `{{DOMAIN}}` replacement |
| S3-6 | Fresh session for Sprint 3: this session's coordinator context is very long and every turn re-sends it; the plan file is the memory, so nothing is lost | Start fresh | Token cost |

Owner-only actions carried forward: task #4 legal items; free API keys (EIA v2, SAM.gov, NRC, regulations.gov);
LinkedIn Marketing Developer Platform application; Bluesky account; cloud accounts and tokens for the first deploy.

Engineering follow-ups queued for Sprint 3 (no decision needed): loader bulk-insert pass (≈100 rows/s today);
NESO geography via a substation or postcode gazetteer; EIA exact-point promotion into the loader; the FERC
multi-year filer-name backfill to retest docket linkage; a scheduled worker for the alert cycle and social drafts;
a real login and registration surface; seat limits.

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
docs/34-crm-system-of-record.md      Attio contract: objects, attributes, relationships, lists, adapter behaviour
pipeline/                  ingestion and resolution code; status_map.yaml is versioned data
.claude/agents/            agent role definitions (invoke via the Agent tool or /agents)
CLAUDE.md                  instructions every session/agent loads first
data/sources.yaml          machine-readable source registry (connector manifest)
data/probes/<date>.json    evidence from scripts/probe_sources.py
scripts/probe_sources.py   reproducible reachability + gridstatus check
.github/workflows/         existing Cloudflare worker proxying to the Lovable app
```
