# ADR 0001 — Record architecture decisions

**Status:** Accepted · 2026-09-12 · solutions-architect
**Supersedes:** none · **Superseded by:** none

## Context

Bankable is built by a rotating set of agents and contractors against a project memory in `docs/00-PLAN.md`
(`CLAUDE.md`: "Do not re-derive what those files already settle"). Sessions are ephemeral; the reasoning behind a
technology choice is not recoverable from the code, and re-deriving it costs more than recording it. The
solutions-architect brief already requires "one ADR per material choice (language/runtime, database, queue,
search, hosting, IaC, observability, billing, CRM/ERP integration pattern)". Several of those choices are also
**open owner questions** (`docs/00-PLAN.md`), so the record has to distinguish a decision that is made from a
recommendation that is waiting.

## Options considered

| Option | Trade-off |
|---|---|
| **Lightweight ADRs in `docs/adr/NNNN-*.md`** (Nygard format) | Small, diffable, reviewable in the same PR as the code that implements them; no tooling. Requires discipline to keep the index current. |
| Decisions recorded only in `docs/00-PLAN.md` decisions log | One place to look, already exists; but one-line entries carry no alternatives or consequences, and the log becomes unreadable past ~30 rows. |
| Decisions in the system spec (`docs/20-architecture.md`) only | Keeps everything in one document; but a spec describes the *current* system and loses the history of what was rejected and why, which is the expensive part to reconstruct. |
| An external tool (Notion, ADR Manager, Log4brains) | Nicer index; another system outside the repo, so it drifts from the code and is invisible to agents that read only the repo. |

## Decision

Record every material architecture decision as a numbered Markdown ADR in `docs/adr/`, with the sections
**Status · Context · Options considered · Decision · Consequences**. Rules:

1. Numbers are sequential and never reused. Filenames are `NNNN-kebab-case-title.md`.
2. Status is one of **Accepted**, **Proposed pending owner** (naming the owner question from
   `docs/00-PLAN.md` that blocks it), **Superseded by NNNN**, or **Rejected**. An ADR is never edited to reverse
   a decision; a new ADR supersedes it.
3. An ADR is required for anything that is expensive to reverse: language and runtime, datastore, orchestration,
   hosting and IaC, billing, the CRM/ERP integration pattern, search engine, observability vendor, and any
   deviation from "boring technology" (solutions-architect brief).
4. An ADR is **not** required for library choices inside a module, naming, or anything a single PR can undo.
5. When an ADR is accepted, a one-line entry goes in the `docs/00-PLAN.md` decisions log pointing at it, and a
   line goes in `docs/CHANGELOG.md`. The plan holds the index; the ADR holds the reasoning.
6. The technology table in `docs/20-architecture.md` §15 stays the single summary view; each row that is
   material links to its ADR, and the two must agree.

## Consequences

- Cheap: an ADR is 40–80 lines and is written when the decision is made, not afterwards.
- Owner review has an explicit surface: everything marked **Proposed pending owner** is the agenda for the next
  owner review, and `docs/20-architecture.md` §16 lists which owner question each waits on.
- Agents resuming work read `docs/00-PLAN.md` → ADR index → the ADR, and do not relitigate settled choices.
- Risk: ADRs go stale if a decision changes in code without a superseding ADR. Mitigation: the launch checklist
  (US-908) includes "ADRs match the running system", and any PR changing a `docs/20` §15 row must cite an ADR.
