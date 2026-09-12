# ADR 0002 — Language and runtime

**Status:** Accepted · 2026-09-12 · solutions-architect
**Ratification:** requested from the owner per `docs/20-architecture.md` §16; no owner question blocks it.
**Related:** `docs/20-architecture.md` §15 (Language/runtime, Web/API framework, ORM), ADR 0003, ADR 0004.

## Context

The system is four things: ~64 connectors that fetch and parse spreadsheets, CSV, JSON and PDFs
(`data/sources.yaml`); an entity-resolution and enrichment layer that is the actual cost centre (`docs/01` §3.3:
"20% fetch, 30% normalise, 50% resolve/enrich/QA"); a read-heavy API and a server-rendered public site
(`docs/20` §2); and a set of publishers. `CLAUDE.md` already fixes "Python for data work (`.venv`,
`pip install -r requirements.txt`)". gridstatus — which normalises seven ISO queues and is wrapped rather than
replaced (`docs/02` §6) — is a Python library, as are pandas, openpyxl, pdfplumber, Playwright's Python binding
and every model SDK. The team is one founder plus contractors (**[A-2]**), so a single language across pipeline,
API and web is worth more than a per-tier optimum.

## Options considered

| Option | For | Against |
|---|---|---|
| **Python 3.12 everywhere** (FastAPI + Pydantic v2, SQLAlchemy 2.0 + Alembic, Jinja2 + htmx for pages) | gridstatus, pandas, openpyxl, pyarrow, pdfplumber, Playwright, model SDKs are all Python-first; one language for connectors, resolver, API and publisher; FastAPI generates the OpenAPI document from the code so `docs/23` cannot drift from the running API; the agent roster is briefed for Python | Slower per-core than Go or Rust; async discipline needed to keep I/O-bound connectors from blocking; a rich client UI would need a second language later |
| TypeScript/Node everywhere | Better front-end story; one language if the web app becomes a React SPA; strong types | The data ecosystem is thin: no gridstatus, weaker xlsx/PDF tooling, pandas-equivalents immature. Would mean reimplementing the ISO queue normalisation that already exists |
| Go for the pipeline, TypeScript for the web | Fast, small containers, excellent concurrency for fetchers | Two languages for one operator; the expensive half of the work (resolution, extraction, QA) has almost no Go ecosystem; contractors for this domain are Python people |
| Python pipeline + TypeScript web app | Best-of-both on paper | Two build chains, two deploy paths, duplicated model types across the boundary, for an MVP whose public pages are documents, not an application |

## Decision

**Python 3.12** for all services, with **FastAPI + Pydantic v2** (API and server-rendered pages via Jinja2 +
htmx), **SQLAlchemy 2.0 + Alembic** (ORM and migrations), **httpx + tenacity** (HTTP), **Playwright for Python**
(browser egress), **pandas + openpyxl + pyarrow + pdfplumber** (parsing), **ruff + mypy + pytest** (quality gate).
All processes ship as one container image with different entrypoints (`docs/20` §4.1).

## Consequences

- One image, one dependency lockfile, one test suite; a contractor can work on a connector and an endpoint in
  the same repo without a context switch.
- The OpenAPI document is generated from Pydantic models, so `docs/23-api-spec-outline.md` becomes executable in
  Sprint 1 rather than a document to maintain by hand.
- CPU-bound work (fuzzy matching over ~10⁵ rows) must stay in vectorised pandas/`rapidfuzz` or move to SQL; a
  naive Python loop will be the first performance problem. Budgeted in the resolver's design, not here.
- Model SDK imports are confined to `services/modelgw` by an import-linter rule in CI (`docs/20` §4.5); no
  provider identifier appears in code or logs (`CLAUDE.md`).
- If the owner answers open question 1 by rebuilding the bankablehq.com front end as a React application
  (**[A-1]**), that front end may be TypeScript against this API. That does not reopen this ADR: the backend
  language is unaffected, and the API is the contract.
- Reversal cost: a rewrite. This is the most expensive decision in the set, which is why it follows the existing
  ecosystem rather than a preference.
