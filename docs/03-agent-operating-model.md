# Agent operating model

**Status:** adopted 2026-09-12 · roster in `.claude/agents/` · session instructions in `CLAUDE.md`

## 1. Two different things called "agents"

The brief asks for a team of agents to build the product and for agents that run the product forever. These are
different systems with different economics and must not be confused.

| | Build-time team | Run-time pipeline |
|---|---|---|
| What | Claude Code subagents defined in `.claude/agents/` (PM, researcher, architect, data engineer, data scientist, backend, frontend, designer, DevOps, QA, legal, content/social, sales/BD) | Scheduled workers in the deployed platform: connectors, resolver, change detector, post generator, alert sender, lead scorer |
| Runs where | This repo, in Claude Code sessions, on demand or on a Routine | Cloud infrastructure owned by the company, 24/7 |
| Uses LLMs for | Everything: writing specs, code, research | Only the steps that need judgement: extraction from PDFs/news, entity-match adjudication, post drafting, lead scoring. Fetching, diffing, storing and publishing are deterministic code |
| Cost driver | Tokens per session | Compute + tokens per record processed; must be metered per source |
| Human gates | Owner reviews merged docs/code; QA signs releases | Publish queue review until a channel is trusted; every outbound human-facing message is human-sent |

Running the production loop *as* Claude Code sessions would be expensive, non-deterministic and hard to
observe. The build-time team therefore builds a conventional pipeline that calls models through the API where
judgement is needed. A Routine (scheduled session) is appropriate for periodic *supervision*: reviewing source
health, triaging breakages, proposing fixes as pull requests.

## 2. Roster, ownership and hand-offs

```
market-researcher ──▶ product-manager ──▶ solutions-architect ──▶ data-engineer / data-scientist
legal-compliance ──▶      │                      │                  backend-developer / frontend-developer
                          ▼                      ▼                  devops-engineer
                   product-designer ──────▶ frontend-developer            │
                                                                          ▼
                   content-social ◀── run-time events ◀──────────── qa-engineer (release gate)
                   sales-bd       ◀── run-time events
```

| Agent | Owns | Produces | Reviewed by |
|---|---|---|---|
| product-manager | scope, PRD, decisions | `docs/10-*` | owner |
| market-researcher | evidence about market | `docs/11-*` | product-manager |
| legal-compliance | data rights, outreach rules | `docs/13-*`, legal fields in `sources.yaml` | owner (+ counsel where flagged) |
| solutions-architect | system, data model, API, ADRs | `docs/20-*`, `docs/21-*`, `docs/adr/` | owner |
| data-engineer | connectors, pipeline, DQ | `pipeline/`, `sources.yaml` | qa-engineer |
| data-scientist | resolution, extraction, matching, scoring | `docs/22-*`, `pipeline/resolve*`, `data/eval/` | qa-engineer |
| backend-developer | services, APIs, integrations | `services/` | qa-engineer |
| frontend-developer | web app, admin | `web/` | qa-engineer, product-designer |
| product-designer | IA, flows, design system | `docs/30-*`, `docs/31-*`, canvases | product-manager |
| devops-engineer | infra, CI/CD, observability | `infra/`, `docs/6x-*` | solutions-architect |
| qa-engineer | tests, release sign-off | `tests/`, release checklists | product-manager |
| content-social | editorial, channels, post pipeline spec | `docs/32-*` | owner |
| sales-bd | ICP, research, drafts, partnerships | `docs/33-*`, CRM | owner |

## 3. The always-on loop (what the run-time pipeline does)

1. **Fetch** each source on its cadence (`data/sources.yaml`), store a snapshot with provenance.
2. **Diff** against the previous snapshot → raw change events.
3. **Normalise** to the canonical schema; harmonise status vocabulary.
4. **Resolve** records to proposals/opportunities/organisations; record merges as events.
5. **Enrich** with extraction from linked documents and news (model-assisted, with confidence and citation).
6. **Publish** to the store; every tier sees it immediately — records since 2026-09-19 and change events since 2026-09-21, when the last delay and its per-source configuration were removed (`docs/21` §5.4, `services/ingest/lag.py`).
7. **Draft posts** per editorial rules; queue for review (or auto-publish where the owner has enabled it).
8. **Alert** subscribers whose saved searches match; write digests.
9. **Score leads** for sales-bd from events (sponsor activity, RFP openings); write to CRM.
10. **Observe**: per-source health, cost per record, post performance; escalate breakages to a supervision Routine.

## 4. What agents cannot do, and the human roles that remain

- **Accounts.** Agents cannot create social, developer, ISO, or API accounts (verification, CAPTCHAs, terms). The
  owner creates them; agents operate them through approved APIs with disclosure where required.
- **Outbound to people.** Sales and social replies are drafted, not sent, until the owner enables a named channel.
  Undisclosed AI personas are prohibited: they violate platform rules, several jurisdictions' marketing law, and
  they would spend the owner's professional reputation in a relationship-driven industry.
- **Licences and contracts.** PJM redistribution, data partnerships, customer terms need a human signatory.
- **Money.** Billing setup, API key purchases, ad spend.

## 5. Sprint cadence

- Sprint 0 (now): PRD v0, market and competition, legal register and outreach rules, architecture and ERD v0,
  entity-resolution prototype on real data, social playbook, GTM playbook.
- Sprint 1: ADRs, OpenAPI, design system and IA (map-first), `docs/04-standards.md` (cross-discipline best practices), connector framework with the five live ISO queues.
- Sprint 2: proposal graph, change events, public delayed pages, alerts, first syndication channel.
- Each sprint ends with QA sign-off and an owner review of `docs/00-PLAN.md`.

## 6. Cost discipline

Every run-time model call is logged with source, record id, tokens and cost. Sources whose per-record cost
exceeds the value of their records are demoted to weekly batch or dropped. Build-time sessions log which agent
produced which artifact in `docs/CHANGELOG.md`.
