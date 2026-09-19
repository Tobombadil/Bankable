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

## 3. Public, Pro and API endpoints

Every endpoint in §3 and §4 returns the envelope of §10 and enforces the visibility predicate of `docs/21` §5.4. "Tier"
is the minimum entitlement; a higher tier sees the same shape with `lag = 0` and more fields.

### 3.1 Public — delayed, no key required

| Method & path | Purpose | Stories |
|---|---|---|
| `GET /v1/proposals` | List with filters, sort, cursor pagination | US-101, US-102 |
| `GET /v1/proposals/{public_id}` | Detail with canonical fields, sources panel, linked orgs | US-201, US-203 |
| `GET /v1/proposals/{public_id}/events` | Lifecycle timeline, newest first | US-202 |
| `GET /v1/proposals/{public_id}/sources` | Provenance rows (licence-gated, `docs/21` §8) | US-201 AC1 |
| `GET /v1/proposals/{public_id}/matches` | Matches with score and rationale | US-203 AC2, US-402 |
| `GET /v1/proposals/geo` | Map payload within a bbox. Exact-grade proposals as `proposal`/`cluster` features; region-grade proposals as `region` features (`feature_kind: region`, `region_level`, `region_id`, `name`, `count`, `lifecycle_state_counts`, `technology_counts`, `capacity_mw_sum`; geometry is the region's representative point, the polygon comes from `/v1/geo/regions`); none-grade counted in `meta.unplaced_count` only (the pre-existing key; `totals` stays the per-viewport aggregate). New filters: `placement` (csv of `exact \| region \| none`, default `exact,region`) and `county_fips` (csv) on this and on `GET /v1/proposals` | US-104 |
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
| `GET /v1/context/plants/geo` | Built-infrastructure context layer (docs/00-PLAN.md 2026-09-14): clustered `built_plant` points under the proposals map. No lag, no tier gating — every source in scope (EIA-860M) is public domain | US-101, US-104 |
| `GET /v1/assets` · `/{public_id}` | Asset list and page (ADR 0008): filters `asset_type`, `technology`, `state`, `organization` (public id), `q` (name, operator, owner); detail carries `owners[]` (organisation public id, name, role, share_pct, as_of, source) and `attributes`. No lag, no tier gating: sources are public domain or CC BY | US-104, US-203 |
| `GET /v1/assets/geo` | Clustered asset points within a bbox: same envelope and cluster shape as `/v1/context/plants/geo`, plus `asset_type` (csv) and `technology` (csv) filters; `feature_kind` is `asset` or `asset_cluster` with `dominant_asset_type`. `/v1/context/plants/geo` stays as an alias for `asset_type=power_plant` | US-104 |
| `GET /v1/assets/{public_id}/nearby-proposals` | Exact-grade proposals within `radius_km` (default 25, max 100) of the asset's point, public-tier rules applied; region-grade and none-grade proposals are never returned here | US-104, US-203 |
| `GET /v1/organizations/{public_id}/assets` | The organisation's assets through `asset_owner`, with role and share; cursor-paginated | US-203 AC1 |
| `GET /v1/geo/regions` | Region polygons for the map: `level` (`county \| state \| country`) and `ids` (csv of `county_fips`, `state_code` or `country`, max 500) → GeoJSON `MultiPolygon` features with `region_id`, `name`, `level`; from the vendored Census cartographic boundaries (20m, US) and the existing country outlines; cacheable for a day | US-104 |

| `POST /v1/ui-events` | Identifier-free interaction counter for the context layer (`services/db/models.py::UI_EVENT_NAMES`); no auth, rate-limited 60/min per IP for limiting purposes only (the IP is never stored) | US-104 |

Public tier behaviour is fixed by `docs/21` §5.4 and §8: records and events only where `public_at <= now()`;
nothing from a gated, restricted or unknown-terms source; derived fields only where the licence withholds raw;
county centroids instead of exact coordinates for restricted geo; attribution rendered in every response.
`GET /v1/context/plants/geo` and `POST /v1/ui-events` are the two exceptions to "delayed": `built_plant` and
`ui_event` are not proposals or opportunities, so neither the lag nor the tier/licence-gating rules apply to them.

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

## 4. Admin endpoints — `admin.` hostname, role `operator` or `owner`

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
| `GET /admin/v1/ui-events/summary` | Weekly counts of `ui_event` rows per name, with `map.layer_toggled` split on/off; the context layer's engagement measurement (docs/00-PLAN.md 2026-09-14) | US-909 |

## 5. Authentication and key scopes

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
`RateLimit-Policy` naming the window. On breach: `429` with `Retry-After` and the problem body of §8
(US-702 AC2). Feeds (`/feeds/*`) are edge-cached and counted per IP at 120/hour. Every request is logged with
key id, endpoint, status and latency — the source for metric M-7 (US-702 AC3).
## 7. Pagination, filtering and sorting

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


## 8. Error model

Errors are RFC 9457 problem details, `Content-Type: application/problem+json`, with a stable machine-readable
`code`. No stack traces, no internal ids, no source payloads in error bodies.

```json
{
  "type": "https://api.bankablehq.com/errors/licence_gated",
  "title": "Field withheld under source licence",
  "status": 403,
  "code": "licence_gated",
  "detail": "Raw fields from source us.iso.caiso.gen_queue are not redistributable; derived fields are returned.",
  "instance": "/v1/proposals/prop_01JBQ7Z8KD/sources",
  "request_id": "req_9f2c1a",
  "errors": [{"field": "capacity_mw[gte]", "message": "must be a number"}],
  "docs": "https://bankablehq.com/docs/api/errors#licence_gated"
}
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `validation_error` | Bad body or parameter; `errors[]` lists fields |
| 400 | `unknown_parameter` | Unrecognised filter — never silently ignored (§7) |
| 400 | `invalid_cursor` | Expired or malformed cursor |
| 401 | `unauthenticated` | Pro/API/admin path without a valid credential |
| 403 | `forbidden_tier` | Credential valid but plan does not include the endpoint or scope; body names the required tier |
| 403 | `licence_gated` | Requested representation withheld by licence; the response may still carry the derived view |
| 403 | `seat_limit` | Concurrent sessions above the seat count (US-602 AC2) |
| 404 | `not_found` | No such public id, or the record is not visible to this tier (indistinguishable by design — a 403 on a gated record would disclose its existence, `docs/21` §8 item 3) |
| 301 | — | Merged record: `Location` points at the surviving public id (US-201 AC3) |
| 410 | `unpublished` | Record withdrawn by takedown or unpublish; the id is reserved, nothing is returned |
| 409 | `conflict` | Idempotency-key reuse with a different body; merge preview stale |
| 422 | `gate_unmet` | Admin attempt to publish a source whose licence gate is unmet (US-905 AC1); body names the gate |
| 429 | `rate_limited` | Sliding window exceeded; `Retry-After` set (§6) |
| 429 | `quota_exceeded` | Daily cap exhausted; `Retry-After` to midnight UTC |
| 503 | `sor_unavailable` | CRM/ERP adapter down and no cached value acceptable (US-902 AC3) |
| 503 | `unavailable` | Maintenance; public reads keep serving from the edge cache |

Partial redaction is not an error. When a record is returned with fields withheld, the envelope's
`redactions[]` (§10) says which and why, and the status is `200`.

## 9. Webhooks and feeds

### 9.1 Webhooks (API tier, scope `write:webhooks`)

One endpoint per subscription, up to 10 per account, each with a filter of the same shape as a saved-search
`query` (`docs/21` §3.15) so that a webhook is literally a saved search with a URL as its channel.

```json
{
  "id": "whd_01JBQ9…",
  "type": "event.published",
  "created_at": "2026-09-11T05:04:13Z",
  "api_version": "v1",
  "data": {
    "event": { "id": "evt_01JBQ8…", "seq": 4812993, "subject_type": "proposal", "subject_id": "prop_01JBQ7Z8KD",
               "event_type": "status_change", "observed_at": "2026-09-10T00:00:00Z",
               "before": {"lifecycle_state": "filed"}, "after": {"lifecycle_state": "studied"},
               "provenance": {"source_id": "us.iso.caiso.gen_queue", "source_url": "…", "retrieved_at": "…", "licence_id": "caiso-tou"} },
    "subject": { "public_id": "prop_01JBQ7Z8KD", "name_canonical": "Gemini Solar + Storage", "url": "https://bankablehq.com/p/gemini-solar-bess-clark-nv" }
  },
  "licence_summary": { "…as §10…" }
}
```

- Types: `event.published`, `match.added`, `match.removed`, `record.unpublished`, `webhook.test`.
- Delivery: `POST`, JSON, `X-Bankable-Signature` (§5), `X-Bankable-Delivery-Id`, `X-Bankable-Event-Seq`.
  A `2xx` within 10 s is success. Retries: 5 attempts over ~6 hours with exponential backoff and jitter; then
  the delivery is marked `failed` and, after 24 consecutive failures, the endpoint is paused and the account
  emailed. `POST /v1/webhooks/{id}/replay?since=<seq>` resends from a cursor.
- Ordering is by `seq` per endpoint but not guaranteed across retries; consumers dedupe on `data.event.id`.
- The payload is tier-filtered and licence-gated exactly like a `GET` on the same key: a webhook cannot deliver
  what the key could not read (`docs/21` §5.4).

### 9.2 RSS and JSON Feed (public, delayed)

Every public list URL has a feed twin (US-503 AC1): append `.rss` or `.json`, or use `/feeds/<resource>` with
the same query string. Pro users get private live feeds at `/feeds/saved/<rss_token>` (`docs/21` §3.15).

| Feed | Format | Item content |
|---|---|---|
| `/feeds/proposals.rss?…` · `/feeds/opportunities.rss?…` · `/feeds/events.rss?…` | RSS 2.0 with `atom:link rel="self"`, `dc:creator` = source credit, `pubDate` = `public_at` | Title = event headline (`Permit issued: Gemini Solar + Storage`), link = detail page, description = derived fields + the credit line (US-503 AC3), `guid` = `evt_…`, `category` = event type and kind |
| Same paths with `.json` | JSON Feed 1.1 | `id`, `url`, `title`, `content_text`, `date_published` (= `public_at`), `tags`, `_bankable` extension with `event_type`, `subject`, `provenance`, `licence_summary` |
| `/sitemap.xml`, `/sitemaps/proposals-{n}.xml` | Sitemap protocol | Detail pages visible on the public tier only; regenerated hourly with the delayed view |

Feed rules: items appear at the public lag, never earlier; every item carries the source credit line and the
`data_as_of` date; feeds for a filter that returns only gated sources are empty, not `404`; the feed `<title>`
states "Public feed, N days delayed — live in Pro" (US-604). Social posts link to the detail page, which offers
the feed and the alert sign-up (US-503 AC2).

## 10. Attribution and licence fields on every response

Every response — list, detail, feed item, export row, webhook delivery — carries the same envelope. Rendering
attribution is not a client concern; the API states it and the web app, RSS, CSV and post templates print it.

```json
{
  "data": { "…resource or array…" },
  "page": { "next_cursor": "…", "prev_cursor": null, "has_more": true },
  "meta": {
    "tier": "public",
    "lag_days": 14,
    "data_as_of": "2026-08-29T05:00:00Z",
    "generated_at": "2026-09-12T09:00:00Z",
    "request_id": "req_9f2c1a",
    "terms_url": "https://bankablehq.com/legal/api-licence",
    "total": 1834, "total_is_estimate": false
  },
  "licence_summary": {
    "sources": [
      { "source_id": "us.iso.caiso.gen_queue", "name": "CAISO Public Queue Report", "operator": "California ISO",
        "licence_id": "caiso-tou", "licence_name": "CAISO Terms of Use", "licence_url": "https://www.caiso.com/…",
        "reuse_class": "attribution", "attribution_text": "Source: California ISO", "requires_link_back": true,
        "record_count": 412, "retrieved_at_max": "2026-09-11T05:00:00Z" }
    ],
    "attribution_line": "Sources: California ISO; ERCOT; U.S. Energy Information Administration (public domain).",
    "redistribution": "derived fields under source terms; raw rows withheld for: us.iso.caiso.gen_queue"
  },
  "redactions": [
    { "public_id": "prop_01JBQ7Z8KD", "field": "sources[0].raw", "reason": "licence", "source_id": "us.iso.caiso.gen_queue" },
    { "public_id": "prop_01JBQ7Z8KD", "field": "location.geom", "reason": "licence_precision", "note": "county centroid returned" }
  ]
}
```

Per-record fields, always present:

| Field | On | Meaning |
|---|---|---|
| `provenance[]` | proposal, opportunity, organization | One entry per visible source row: `source_id`, `source_name`, `source_record_id` (where the licence allows), `source_url`, `retrieved_at`, `licence_id`, `reuse_class`, `attribution_text` (`docs/21` §3.2) |
| `provenance` | event, document, alias, extraction | The single provenance quartet |
| `field_provenance` | detail responses on Pro+ | Which source and snapshot each canonical field came from (`docs/21` §3.1) |
| `licence_summary` | every envelope | As above; the list of distinct sources present in the payload, so a list page renders one credit line per source (US-105 AC1) |
| `redactions[]` | every envelope | What was withheld and why — the record exists, the field does not travel (`docs/21` §8) |
| `data_as_of`, `lag_days`, `tier` | every envelope | Public messaging and the "live in Pro" banner (US-604, US-201 AC4) |

CSV exports carry `source_id`, `source_url`, `retrieved_at`, `licence` as columns on every row and a leading
`#` comment block with the licence summary and the attribution line (US-105 AC2, US-603 AC2).

## 11. Story map: endpoint groups to PRD user stories

| Endpoint group | Section | Stories satisfied |
|---|---|---|
| Proposals list, filters, search, geo | §3.1 | US-101, US-102, US-103, US-104, US-105 |
| Proposal detail, events, sources, matches | §3.1 | US-201, US-202, US-203; US-204 via `/reports` |
| Opportunities list and detail; sources registry (curated issuers are sources) | §3.1 | US-301, US-302, US-303 |
| Matches (read), dismiss | §3.1, §3.2 | US-401, US-402 |
| Saved searches, alerts, preview | §3.2 | US-501, US-502, US-504 |
| Feeds (RSS, JSON Feed, sitemaps) | §9.2 | US-503, US-604 |
| Visibility predicate on every read path; `meta.tier`, `lag_days`, `data_as_of` | §3, §10 | US-601, US-604 |
| `/me`, entitlement from the mirror, seat check | §3.2, §5 | US-602 |
| Exports | §3.2, §10 | US-603 |
| Keys, scopes, licence acceptance | §3.2, §5 | US-701, US-704 |
| Rate limits, headers, request log | §6 | US-702 |
| Read endpoints, events `since` cursor, bulk, webhooks, versioning | §3.1, §3.2, §7, §9.1 | US-703 |
| Admin posts, channels auto-publish, no reply/DM endpoints exist | §4 | US-801, US-802, US-803, US-804 |
| Admin users, customers/subscriptions via adapter, leads | §4 | US-901, US-902, US-903, US-403 |
| Admin sources, runs, snapshots, publish state, gates | §4 | US-904, US-905, US-906 |
| Admin tasks, costs, audit, user delete → redaction | §4 | US-907, US-909, US-910; US-908 is a release gate, not an endpoint |
| Intake submissions and their review | §3.1, §4 | US-1001, US-1002, US-1003 |

All 44 stories (US-101…US-1003) map to at least one row; US-908 (launch checklist) is satisfied by tests over the
endpoints above, not by an endpoint.

## 12. The OpenAPI document (Sprint 1 deliverable)

`api/openapi.yaml` — OpenAPI 3.1, generated from the FastAPI application at build time and committed, with a CI
check that the committed file equals the generated one. Sprint 1 scope (`docs/10` §6): all §3.1 read endpoints,
`/v1/events` with `since`, `/v1/sources`, `/v1/licences`, the envelope of §10, the error schema of §8, security
schemes of §5, rate-limit headers of §6, and every §3.2 and §4 path stubbed with its request/response schema and
marked `x-status: planned` until its sprint. Every operation carries `x-stories: [US-…]` from §11, an example
response (US-704 AC1), and `x-tier: public|pro|api|admin`. The generated Redoc page at `/docs` is the public API
documentation; key creation links to the API licence version it requires (US-704 AC2).

## 13. Assumptions

| Id | Assumption | Depends on |
|---|---|---|
| P-1 | Rate-limit defaults as in §6, from `docs/10` A-8 | Phase 1 pricing; configuration only |
| P-2 | Public lag shown as 14 days in examples (`docs/21` D-1) | Owner decision on `docs/20` A-8 vs `docs/10` A-7 |
| P-3 | Hostnames `api.` and `admin.` under bankablehq.com; the Lovable app consumes `/v1` read-only | **[A-1]** |
| P-4 | Restricted and unknown-terms sources return nothing on Pro/API, not derived aggregates (`docs/21` C-3) | Owner and legal-compliance |
| P-5 | Intake endpoints are public but captcha-gated and rate-limited at 5/hour per IP | product-designer flow |
