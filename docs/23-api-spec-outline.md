# API surface outline

**Status:** Phase 2 draft v0 · 2026-09-12 · solutions-architect · reviewed by: owner (pending)
**Inputs:** `docs/20-architecture.md` §5 (tiering), §7 (auth, keys, rate limiting), §8 (admin surface),
`docs/21-data-model.md` (resources, gating), `docs/10-prd-mvp.md` §4 (US-1xx–US-10xx).
**Deliverable this outlines:** `api/openapi.yaml` — **the full OpenAPI 3.1 document is a Sprint 1 deliverable**
(`docs/10` §6 Sprint 1: "ADRs, OpenAPI v1 draft"). It is generated from the FastAPI/Pydantic models (ADR 0002),
not written by hand, so the running API and the published contract cannot drift. This document fixes the shapes
that generation must produce; where the two disagree, the generated document is wrong until it matches this one.

## 1. Conventions

| Item | Rule |
|---|---|
| Base URLs | `https://api.bankablehq.com/v1` (public, Pro, API tiers) · `https://admin.bankablehq.com/admin/v1` (operators only, separate hostname, `docs/20` §7) · `https://bankablehq.com/feeds/*` (RSS, JSON Feed, sitemaps) |
| Versioning | Path-versioned. `v1` is frozen at MVP launch; breaking changes require `v2` (US-703 AC3). Additive fields are not breaking. |
| Identifiers | Public ids only (`prop_…`, `opp_…`, `org_…`, `evt_…`, `mat_…`). Internal UUIDs never appear. |
| Transport | HTTPS only, HTTP/2, JSON (`application/json`) request and response. `Accept: text/csv` on list endpoints returns an export (Pro+). |
| Time | RFC 3339 UTC with `Z`. Dates without a time are `YYYY-MM-DD`. |
| Casing | `snake_case` fields, matching `docs/21`. |
| Idempotency | All mutating endpoints accept `Idempotency-Key`; replays return the original response for 24 h. |
| Request id | Every response carries `X-Request-Id`; it appears in error bodies and in support requests. |
| Compression | `gzip` and `br`. Bulk endpoints stream NDJSON. |
| Caching | Public GETs carry `Cache-Control: public, max-age=300` and an `ETag`; the delayed feed is served from an hourly materialised view (`docs/20` §5). Pro/API responses are `private, no-store`. |

## 2. Resources

| Resource | Path root | Backing entity (`docs/21`) | Tiers |
|---|---|---|---|
| Proposal | `/proposals` | `proposal`, `proposal_source` | public · pro · api · admin |
| Opportunity | `/opportunities` | `opportunity`, `opportunity_source` | public · pro · api · admin |
| Organisation | `/organizations` | `organization`, `organization_alias` | public · pro · api · admin |
| Event | `/events` | `event` | public (delayed) · pro · api · admin |
| Match | `/matches` | `match`, `match_dismissal` | public (read-only on a detail page) · pro · api · admin |
| Document | `/documents` | `document` | metadata only, all tiers; bytes via pre-signed URL where the licence allows |
| Source | `/sources` | `source`, `licence` | public (registry + attribution) · admin (health, control) |
| Licence | `/licences` | `licence` | public |
| Saved search | `/saved-searches` | `saved_search` | pro · api |
| Alert | `/alerts` | `alert` | pro · api |
| Export | `/exports` | `export` | pro · api |
| API key | `/keys` | `api_key` | api (self-service) · admin |
| Webhook | `/webhooks` | `webhook_endpoint`, `webhook_delivery` | api |
| Account / me | `/me`, `/account` | `user`, `account`, `subscription` | pro · api · admin |
| Post | `/admin/v1/posts` | `post` | admin only |
| Task | `/admin/v1/tasks` | `task` | admin only |
| Intake | `/intake/proposals`, `/intake/opportunities` | `task` + pending `proposal`/`opportunity` | public (write-only) |

## 3. Endpoints

Every endpoint below returns the envelope of §10 and enforces the visibility predicate of `docs/21` §5.4. "Tier"
is the minimum entitlement; a higher tier sees the same shape with `lag = 0` and more fields.

### 3.1 Public — delayed, no key required

| Method & path | Purpose | Stories |
|---|---|---|
| `GET /v1/proposals` | List with filters, sort, cursor pagination | US-101, US-102 |
| `GET /v1/proposals/{public_id}` | Detail with canonical fields, sources panel, linked orgs | US-201, US-203 |
| `GET /v1/proposals/{public_id}/events` | Lifecycle timeline, newest first | US-202 |
| `GET /v1/proposals/{public_id}/sources` | Provenance rows (licence-gated, `docs/21` §8) | US-201 AC1 |
| `GET /v1/proposals/{public_id}/matches` | Matches with score and rationale | US-203 AC2, US-402 |
| `GET /v1/proposals/geo` | Map payload: clustered points/county aggregates within a bbox | US-104 |
| `GET /v1/opportunities` · `/{public_id}` · `/{public_id}/events` · `/{public_id}/sources` | Same shapes for the demand side | US-301, US-302 |
| `GET /v1/organizations` · `/{public_id}` · `/{public_id}/proposals` · `/{public_id}/opportunities` | Sponsor and issuer pages | US-203 AC1, US-303 |
| `GET /v1/events` | Global change feed, `since` cursor, filterable by subject and type | US-703 AC1 |
| `GET /v1/documents/{id}` | Document metadata and a link; bytes only where `storage_policy = stored` and the licence allows | US-302 AC1 |
| `GET /v1/sources` · `/{source_id}` | Source registry: name, operator, cadence, licence, attribution text, publish state, lag | US-105 AC1, US-704 |
| `GET /v1/licences` · `/{licence_id}` | Licence register with reuse class and permissions | US-105, US-704 AC2 |
| `GET /v1/meta/vocabularies` | Enum vocabularies (kinds, technologies, lifecycle states, event types) so integrators can build filters | US-102, US-703 |
| `POST /v1/intake/proposals` · `/v1/intake/opportunities` | Submit-a-project / submit-an-RFP; creates a pending record and an admin task; rate-limited and captcha-gated | US-1001, US-1003 |
| `POST /v1/reports` | Report a problem on a record | US-204 |
| `GET /v1/health` | Liveness and `data_as_of` | US-604, US-904 |

Public tier behaviour is fixed by `docs/21` §5.4 and §8: records and events only where `public_at <= now()`;
nothing from a gated, restricted or unknown-terms source; derived fields only where the licence withholds raw;
county centroids instead of exact coordinates for restricted geo; attribution rendered in every response.

### 3.2 Pro and API — live, key or session required

Everything in §3.1 with `lag = 0`, plus:

| Method & path | Purpose | Tier | Stories |
|---|---|---|---|
| `GET /v1/me` | Current user, account, entitlement, seat usage, limits | pro | US-602 |
| `GET /v1/saved-searches` · `POST` · `PATCH /{id}` · `DELETE /{id}` | Saved searches (max 25), delivery mode, channels | pro | US-501, US-504 |
| `POST /v1/saved-searches/{id}/preview` | Run the query now and return the matches it would alert on | pro | US-501 AC2 |
| `GET /v1/alerts` · `GET /v1/alerts/{id}` | Delivery history with event ids and provider message id | pro | US-502 AC4 |
| `POST /v1/exports` → `GET /v1/exports/{id}` | Async CSV of any filtered list; row cap 10,000 default; provenance columns and licence header | pro | US-603 |
| `POST /v1/matches/{id}/dismiss` · `DELETE …/dismiss` | Per-user dismissal, never global | pro | US-402 AC2 |
| `GET /v1/matches` | Cross-entity match list with filters | pro | US-401 |
| `GET /v1/keys` · `POST` · `DELETE /{id}` | Self-service API keys, shown once, revoked ≤ 60 s | api | US-701 |
| `GET /v1/bulk/proposals` · `/bulk/opportunities` · `/bulk/events` | NDJSON stream, `updated_since` + cursor, 1,000-row pages | api (`read:bulk`) | US-703 |
| `GET /v1/webhooks` · `POST` · `DELETE /{id}` · `POST /{id}/test` | Webhook endpoints and secrets | api (`write:webhooks`) | US-703 |
| `GET /v1/webhooks/{id}/deliveries` | Delivery attempts and responses for debugging | api | — |

### 3.3 Admin — `admin.` hostname, role `operator` or `owner`

| Method & path | Purpose | Stories |
|---|---|---|
| `GET /admin/v1/sources` · `/{id}` · `PATCH /{id}` | Health, cadence, lag, egress class, enrichment toggle, pause/resume | US-904 |
| `POST /admin/v1/sources/{id}/run` | Run now | US-904 AC2 |
| `PUT /admin/v1/sources/{id}/publish-state` | `ingest_only \| api_only \| public`; refused while the licence gate is unmet | US-905 AC1 |
| `GET /admin/v1/source-runs` · `/{id}` · `GET /admin/v1/snapshots/{id}` | Run history, DQ warnings, raw snapshot links | US-904 AC1 |
| `PUT /admin/v1/licences/{id}/gate` | Clear or set a gate (G-PJM, G-MISO…); `legal` role only; requires evidence URL, retrieval date, classifier | US-905 AC1 |
| `PATCH /admin/v1/proposals/{id}` · `/opportunities/{id}` · `/organizations/{id}` | Edit with a mandatory reason; writes an `admin_edit` event and a field override | US-905 AC3, US-901 AC2 |
| `POST /admin/v1/proposals/{id}/merge` · `/unmerge` | Merge with preview; unmerge reverses from the merge event's `before` | US-202 AC2, `docs/21` §6.3 |
| `PUT /admin/v1/records/{type}/{id}/publish-state` | Per-record publish, unpublish, takedown with reason | US-905 AC2–AC3 |
| `GET /admin/v1/resolution-candidates` · `POST /{id}/decide` | Ambiguous pairs with the model's rationale; human decision wins thereafter | `docs/20` §3.5 |
| `GET /admin/v1/extractions` · `POST /{id}/accept` · `/reject` | Low-confidence extraction review | `docs/20` §8.4 |
| `GET /admin/v1/posts` · `PATCH /{id}` · `POST /{id}/approve` · `/reject` · `/schedule` | Social review queue | US-802 |
| `PUT /admin/v1/channels/{channel}/auto-publish` | Owner role only; audited event; default off for every channel | US-802 AC2 |
| `GET /admin/v1/tasks` · `PATCH /{id}` | Reported problems, intake submissions, deletion requests | US-907, US-204, US-1002 |
| `POST /admin/v1/tasks/{id}/approve-intake` | Approve or link an intake submission, compute matches, create the lead | US-1002 |
| `GET /admin/v1/users` · `PATCH /{id}` · `DELETE /{id}` | Roles, disable, delete (runs the redaction procedure) | US-901, US-910 |
| `GET /admin/v1/customers` · `/subscriptions` | Read-through the CRM/ERP adapter with a staleness banner when stale | US-902, US-903 |
| `POST /admin/v1/leads` | Create a CRM lead from a match; stores only the lead id | US-403 |
| `GET /admin/v1/keys` · `POST` · `DELETE /{id}` | Issue and revoke keys on behalf of an account | US-701 |
| `GET /admin/v1/costs` | Model spend by purpose, source and day; cost per changed record | US-909, `docs/20` §6 |
| `GET /admin/v1/audit` | `event` filtered to `actor_type = user` | US-901 AC2 |

## 4. Authentication and key scopes

| Credential | Used by | Mechanism |
|---|---|---|
| Session cookie | Web (public, Pro, admin) | Passwordless email magic link or Google sign-in; signed HTTP-only cookie plus a revocable server-side `session` row (`docs/20` §7). Roles: `viewer`, `member`, `operator`, `owner`. |
| API key | API tier, integrations | `Authorization: Bearer bk_live_<32 random bytes, base62>`. `bk_test_` keys read the same data with a separate quota. Shown once, stored as SHA-256 (US-701 AC2). Revocation effective ≤ 60 s. |
| Webhook signature | Outbound to customers | `X-Bankable-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "t.body">`, 5-minute tolerance, secret shown once. |
| Admin second factor | `operator`, `owner` | Enforced by the identity provider (**[A-10]**), plus the separate `admin.` hostname and an optional IP allowlist / Cloudflare Access. |

Scopes on a key (`docs/20` §7):

| Scope | Grants |
|---|---|
| `read:public` | Delayed tier only — the default for a free key |
| `read:live` | Zero-lag reads across proposals, opportunities, events, matches, organisations |
| `read:bulk` | `/v1/bulk/*` NDJSON streams and CSV export endpoints |
| `write:webhooks` | Manage webhook endpoints and replay deliveries |
| `admin:*` | Admin API; never issued to a customer key; operator sessions only |

A key never exceeds its plan's tier: `scopes ∩ plan_tier` is evaluated at request time, and the plan is read from
the entitlement mirror (`docs/21` §3.13, US-701 AC3). Entitlement changes take effect within the 15-minute cache
TTL (US-602 AC1). Missing or invalid credentials fall back to the public tier rather than failing — a public read
is always available — except on Pro/API/admin-only paths, which return `401`.

## 5. Pagination, filtering and sorting

**Pagination is cursor-based.** Offset pagination is not offered: ingestion inserts continuously and offsets
would duplicate or skip rows under concurrent writes (US-101 AC2).

```
GET /v1/proposals?limit=50&cursor=eyJzIjoxNjkyLCJpIjoicHJvcF8wMUpCIn0
```

| Parameter | Default | Max | Notes |
|---|---|---|---|
| `limit` | 50 | 200 (1,000 on `/bulk/*`) | Page size |
| `cursor` | — | — | Opaque base64 of `(sort key, tiebreaker id)`; valid for 24 h; invalid cursor → `400 invalid_cursor` |
| `include` | — | — | `count`, `sources`, `matches`, `events` — opt-in expansions, each costing a documented extra query |

Response paging block: `page: {next_cursor, prev_cursor, has_more}` and, when `include=count`,
`meta.total` with `meta.total_is_estimate` (exact at ≤ 10,000 rows, estimated above — US-101 AC1 asks for a total,
and an exact count over a large filtered set is not worth the scan).

**Filtering.** `field=value` for equality, `field=a,b` for OR within a facet, and `field[op]=value` for ranges:
`gte`, `lte`, `gt`, `lt`, `from`, `to`. Facets combine with AND, values within one facet with OR (US-102 AC3).

| Filter | Applies to | Example |
|---|---|---|
| `kind`, `technology`, `lifecycle_state`, `jurisdiction`, `iso`, `state`, `county_fips`, `sponsor_id`, `source_id` | proposals | `?technology=bess,solar_pv&jurisdiction=US-TX` |
| `capacity_mw[gte]`, `capacity_mw[lte]`, `storage_mwh[gte]` | proposals | `?capacity_mw[gte]=50&capacity_mw[lte]=500` |
| `first_seen[from]`, `first_seen[to]`, `last_changed[from]`, `updated_since` | proposals, opportunities | `?updated_since=2026-09-01T00:00:00Z` |
| `kind`, `issuer_id`, `status`, `technologies`, `due_at[from]`, `due_at[to]` | opportunities | `?status=open&due_at[to]=2026-12-31` |
| `subject_type`, `subject_id`, `event_type`, `since` (cursor or timestamp) | events | `?event_type=status_change&since=4812993` |
| `q` | all searchable resources | Full-text + trigram + exact identifier match (US-103 AC1–AC2) |
| `bbox`, `zoom` | `/proposals/geo` | `?bbox=-106.6,25.8,-93.5,36.5` |

Unknown filter parameters are a `400 unknown_parameter`, never silently ignored — a silently dropped filter on a
licence-sensitive surface is a leak. Filters round-trip in the URL so a shared link reproduces the result set on
the same tier (US-102 AC2).

**Sorting.** `sort=-last_changed,name_canonical`; `-` is descending. Allowlisted per resource: proposals
(`last_changed`, `first_seen`, `capacity_mw`, `name_canonical`), opportunities (`due_at`, `open_at`,
`last_changed`, `budget_amount`), events (`seq`, `observed_at`), matches (`score`, `first_matched_at`). Defaults:
proposals `-last_changed` (US-101 AC1), opportunities `due_at` with `status=open` (US-301 AC2), events `-seq`.

## 6. Rate limits and quotas

Two layers (`docs/20` §7): Cloudflare edge rules for anonymous abuse per IP, and an application sliding window
per key or session in Postgres. Defaults, configurable per plan and per key (US-702 AC1, assumption A-8 in
`docs/10` §7):

| Tier | Read endpoints | Search (`q=`) | Bulk / export | Writes | Daily cap |
|---|---|---|---|---|---|
| Public (no key), per IP | 60 / hour | 20 / hour | — | 5 / hour (intake, reports) | 1,000 |
| Free account (session) | 300 / hour | 60 / hour | — | 10 / hour | 3,000 |
| Pro (session or key) | 600 / hour | 120 / hour | 5 exports / day, 10,000 rows each | 60 / hour | 10,000 |
| API plan (key) | 6,000 / hour | 600 / hour | 20 bulk requests / hour | 600 / hour | 50,000 |
| Admin (operator session) | 1,200 / hour | — | — | — | — |

Headers on every response: `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` (delta-seconds), and
`RateLimit-Policy` naming the window. On breach: `429` with `Retry-After` and the problem body of §7
(US-702 AC2). Feeds (`/feeds/*`) are edge-cached and counted per IP at 120/hour. Every request is logged with
key id, endpoint, status and latency — the source for metric M-7 (US-702 AC3).
