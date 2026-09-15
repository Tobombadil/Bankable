# ADR 0003 — Primary datastore

**Status:** Accepted · 2026-09-12 · solutions-architect
**Ratification:** requested from the owner per `docs/20-architecture.md` §16; no owner question blocks it.
**Related:** `docs/20-architecture.md` §4.4, §13 (scaling path), `docs/21-data-model.md`, ADR 0004.

## Context

The data is relational and small: ~10⁵ entities, ~10⁶ events per year, ~100 sources, single-digit requests per
second (`docs/20` §13). It needs, at once: a canonical store with foreign keys and constraints; an append-only
event log that is the product; geospatial queries for the map (US-104); full-text and fuzzy search over names,
sponsors and identifiers (US-103, and fuzzy name matching is the last resolution key in `docs/02` §5); a job
queue that can be enqueued in the same transaction as a data write (`docs/20` §3.7); rate-limit counters and
sessions. The operator is one person (**[A-2]**) with an infrastructure ceiling in the low hundreds of dollars a
month (`docs/01` §3.4). Every additional datastore is another thing to back up, monitor, upgrade and reason about.

## Options considered

| Option | For | Against |
|---|---|---|
| **Postgres 16, managed, with PostGIS + pg_trgm + btree_gin + pgcrypto** | One store for canonical data, events, queue, search, geo, sessions and counters; transactional enqueue; PITR from the provider; partitioning for the event table; every contractor knows it; a clear exit to read replicas, Meilisearch and Redis when measured | Full-text search is weaker than a dedicated engine (no good faceting, no typo tolerance beyond trigram); a single instance is a single failure domain; large `jsonb` payloads need care |
| MySQL/MariaDB | Ubiquitous, cheap managed tiers | No PostGIS parity, weaker `jsonb`, no transactional advisory-lock queue idiom, worse FTS. No upside for this workload |
| Postgres + Elasticsearch/OpenSearch from day one | Best search and facets | A second stateful system, its own backups and memory footprint, doubling the ops surface before anyone has asked for a facet |
| Document store (MongoDB, DynamoDB) | Flexible raw payloads | The domain is relational (proposal ↔ source ↔ organisation ↔ event ↔ match); we would rebuild joins and constraints in application code, and lose the transactional enqueue |
| Postgres + a separate time-series or event store (Kafka, ClickHouse) | Scales the event log far past our needs | ~10⁶ events/year fits comfortably in a partitioned Postgres table; Kafka is an operational commitment with no MVP payoff |

## Decision

**Postgres 16** as the single primary datastore, managed by the provider with daily snapshots and
point-in-time recovery, extensions `postgis`, `pg_trgm`, `btree_gin`, `pgcrypto`. Search at MVP is Postgres FTS
(`tsvector` generated columns) plus trigram indexes. Object storage (**Cloudflare R2**, S3 API) holds raw
snapshots, documents, exports and post media — bytes belong in object storage, rows in Postgres. Separate
Postgres roles for `api`, `worker`, `admin_api` and `readonly` (`docs/20` §11).

## Consequences

- One backup, one restore drill, one set of credentials to rotate; the monthly restore drill is a DevOps runbook
  item (`docs/20` §11).
- The licence and tier predicates live in the store layer as SQL, which is what makes "the public API code path
  cannot construct an ungated query" testable (`docs/21` §5.4, §8).
- `event` is range-partitioned monthly on `observed_at` from the first migration, because converting a large
  table to partitioned later is a rewrite (`docs/21` §5.3).
- Search will hit limits before anything else does. The trigger is measured, not planned: p95 > 300 ms or a
  product requirement for facets moves search to Meilisearch fed from the event log (`docs/20` §13 step 4). The
  event log makes any derived index disposable, which is what makes this safe to defer.
- Managed Postgres with PITR is the largest single infrastructure line item (USD 35–70/mo, `docs/20` §14). Running
  it on our own VM would halve that and cost the restore guarantees; not worth it for the system of record of a
  data product.
- Risk: a single instance is a single failure domain. Accepted at MVP — the public site is edge-cached and the
  workers pause safely (`docs/20` §12) — and revisited when an SLA exists.
