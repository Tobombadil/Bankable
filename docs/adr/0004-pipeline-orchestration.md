# ADR 0004 — Pipeline orchestration and job queue

**Status:** Accepted · 2026-09-12 · solutions-architect
**Ratification:** requested from the owner per `docs/20-architecture.md` §16; no owner question blocks it.
**Related:** `docs/20-architecture.md` §3 (module contracts), §4.2 (scheduler and queue), §12 (failure modes),
ADR 0003.

## Context

The pipeline is `connectors → snapshot → diff → normalise → resolve → enrich → store/publish → alerts, posts,
API, CRM sync` (`docs/20` §3). Its properties are fixed by the architecture and constrain the orchestrator:

- Each stage is a pure function over persisted inputs; stages communicate only through Postgres and object
  storage, never in memory. A stage can be re-run from its inputs.
- The store/publish step must enqueue alerts, post drafts, webhook fan-out and CRM sync **in the same
  transaction** as the entity and event writes (`docs/20` §3.7). An outbox that can be lost or double-sent is a
  product defect: it either drops a customer's alert or double-posts to a social channel.
- Scheduling is per-source cadence from `data/sources.yaml` (15-min to annual) with jitter, per-source and
  per-host concurrency limits (FERC ≤ 0.5 rps, GDELT 1 per 5 s, PJM 6 connections/min), retries with backoff and
  a dead-letter at five failures (`docs/20` §4.2, `docs/02` §7).
- Volume is tiny: a few thousand jobs a day at MVP, one operator (**[A-2]**).

## Options considered

| Option | For | Against |
|---|---|---|
| **Postgres-backed queue (Procrastinate) with its built-in periodic tasks** | Transactional enqueue with the data write — the requirement above, for free; no new service; jobs are inspectable in SQL and in the admin panel; `LISTEN/NOTIFY` gives low latency; Python-native, async-friendly | Throughput ceiling in the thousands of jobs/minute (far above our need); fewer batteries than Celery; smaller community; long-running jobs need explicit heartbeats |
| Celery + Redis | The default, huge community, mature monitoring (Flower) | A second stateful service to run and back up; **no transactional enqueue** — the classic dual-write problem needs an outbox table, which means implementing half of a Postgres queue anyway; Redis persistence semantics are a footgun for at-least-once delivery |
| Temporal / Prefect / Dagster | Real workflow engines: retries, backfills, lineage, a UI | Heavier than the problem by an order of magnitude. Temporal needs its own cluster; Prefect/Dagster add a control plane and their own deployment model. The DAG here is six deterministic stages, and `source_run` already gives lineage |
| Cron + shell scripts, state in Postgres | Nothing to learn | Reimplements retries, locking, backoff, concurrency limits and dead-lettering by hand, badly |
| Cloud-native queue (SQS / Cloud Tasks) | Managed, scales | Ties the design to one cloud, still no transactional enqueue, and adds egress and IAM surface for no gain at this size |

## Decision

**Procrastinate** (Postgres-backed job queue) with its periodic-task scheduler, running on the same Postgres
instance as the canonical store (ADR 0003). A singleton `scheduler` process holds a Postgres advisory leader lock
and enqueues `fetch` jobs from `source.schedule_cron` with jitter. Job types: `fetch`, `diff`, `normalise`,
`resolve`, `enrich`, `alert`, `post_draft`, `publish_post`, `sor_sync`, `webhook`, `relag`, `export`.
Every job is idempotent on `(type, key)`. Host politeness is a token bucket in Postgres keyed on
`source.host`. Workers are pooled by egress class (`worker-plain`, `worker-browser`, `worker-model`) so that a
Chromium job cannot starve the plain-HTTP pool and the model budget can cap one pool independently.

## Consequences

- The hardest correctness requirement — "publish, alert and post exactly once, or not at all" — becomes a plain
  `INSERT` inside the same transaction. No outbox, no dual-write reconciliation.
- Queue depth and job age are ordinary SQL queries, so source health and the admin panel need no extra
  integration (`docs/20` §8, §10).
- Backfills run as the same job types with `trigger = 'backfill'`; the model gateway's batch mode absorbs the
  cost (`docs/20` §4.5).
- Coupling: pipeline throughput now shares a database with the API. Mitigation: separate roles and connection
  pools, `UNLOGGED` rate-limit tables, and the scaling path's read replica when measured (`docs/20` §13 step 3).
- Losing a workflow engine means no visual DAG and no automatic lineage graph. `source_run` plus `event.run_id`
  and `event.job_id` cover the lineage question that is actually asked ("where did this value come from").
- Reversal cost: moderate. Job handlers are ordinary Python functions; moving to Celery or Temporal later is a
  change to the enqueue and worker bootstrap, plus reintroducing an outbox for the transactional cases.
