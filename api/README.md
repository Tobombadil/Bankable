# `api/openapi.yaml` — the platform API contract

OpenAPI 3.1 for the public (delayed), Pro (live), API-plan and admin surfaces. Written to
`docs/23-api-spec-outline.md` (normative), with field-level shapes from `docs/21-data-model.md`, the API rules of
`docs/04-standards.md` §5, tier and auth mechanics from `docs/20-architecture.md` §5 and §7, and story ids from
`docs/10-prd-mvp.md` §4.

`docs/23` preamble stands: **where this file and `docs/23` disagree, `docs/23` is right and this file is wrong.**
From Sprint 1 the document is generated from the FastAPI/Pydantic models (ADR 0002) and CI compares the generated
file byte-for-byte with the committed one (`docs/23` §12, `docs/04` E-8). This hand-written version fixes the shapes
that generation must produce.

| | |
|---|---|
| Operations | 118 (113 in `paths`, 5 outbound `webhooks`) |
| Schemas | 248 · Parameters 117 · Reusable responses 16 · Examples 9 |
| PRD stories covered | 44 / 44 |
| Validator | `openapi-spec-validator` 0.9.0 — `api/openapi.yaml: OK` |
| Product name | pending (`docs/00-PLAN.md`). Every hostname and URL is `infraque.com`; see [Placeholders](#placeholders) |

## Placeholders

The name is not chosen, so the document never coins one. Replace by find-and-replace when it is:

| Placeholder | Appears in | Replace with |
|---|---|---|
| `infraque.com` | `servers`, `info.termsOfService`, `info.license.url`, `info.contact`, every `type` URI in `Problem`, every example URL | the product domain |
| `X-Platform-Signature`, `X-Platform-Delivery-Id`, `X-Platform-Event-Seq` | outbound webhook headers | the branded header prefix (`docs/23` §5 writes these with the codename — see open decision 2) |
| `_platform` | JSON Feed extension key on every feed item | the branded extension key (`docs/23` §9.2 writes `_bankable`) |
| `bk_live_` / `bk_test_` | `api_key.prefix`, the `ApiKey` security scheme, the key example | kept verbatim from `docs/23` §5 and `docs/21` §3.17 — see open decision 3 |

Descriptions say "the platform". No product name, and no model identifier, appears anywhere in the file.

## View it (no build step)

Both viewers are a single HTML file plus a CDN script. The spec is fetched over HTTP, so serve the directory
rather than opening the file from disk (`file://` fetches are blocked by CORS).

```bash
cd api && python3 -m http.server 8000
# then open http://localhost:8000/redoc.html or http://localhost:8000/swagger.html
```

`api/redoc.html`:

```html
<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Platform API</title><meta name="viewport" content="width=device-width,initial-scale=1"></head>
  <body style="margin:0">
    <redoc spec-url="./openapi.yaml"></redoc>
    <script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
  </body>
</html>
```

`api/swagger.html`:

```html
<!doctype html>
<html>
  <head>
    <meta charset="utf-8"><title>Platform API</title><meta name="viewport" content="width=device-width,initial-scale=1">
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
  </head>
  <body>
    <div id="ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>SwaggerUIBundle({url: './openapi.yaml', dom_id: '#ui', deepLinking: true});</script>
  </body>
</html>
```

In production the generated Redoc page is served at `/docs` and is the public API documentation
(`docs/23` §12, US-704 AC1).

**The file uses YAML anchors and merge keys** (`&std_headers` / `<<: *common_errors`) so that the nine standard
response headers and the four ubiquitous error responses are stated once. PyYAML, js-yaml (Redoc, Swagger UI) and
`openapi-spec-validator` all resolve them. A generator that cannot emit them should expand them — the resolved
document is identical either way.

## Validate it

```bash
python3 -m venv .venv && .venv/bin/pip install openapi-spec-validator jsonschema
.venv/bin/openapi-spec-validator --schema 3.1 --validation-errors all --subschema-errors all api/openapi.yaml
.venv/bin/python api/check_story_coverage.py
```

`api/check_story_coverage.py` is the second gate and is the script that proves the 44-story claim. It checks that

1. the document parses and **every internal `$ref` resolves**;
2. every operation carries `summary`, `x-tier`, `x-prd-stories` and `x-status`, `x-tier` is one of the four tiers,
   and no `operationId` is duplicated;
3. **every named example validates against the schema it illustrates** and every problem example against `Problem`
   (`docs/04` E-8 requires this of the generated document too);
4. every PRD §4 story id is claimed by at least one operation and no operation claims an id the PRD does not have.

It exits non-zero on any failure, so it drops straight into CI next to the byte-for-byte generation check.

### Validator output, verbatim

```
$ python -m venv /tmp/claude-0/-home-user/3c9831de-7860-54c7-a3c3-ba9689ba4e8d/scratchpad/venv
$ /tmp/claude-0/-home-user/3c9831de-7860-54c7-a3c3-ba9689ba4e8d/scratchpad/venv/bin/pip install openapi-spec-validator
$ /tmp/claude-0/-home-user/3c9831de-7860-54c7-a3c3-ba9689ba4e8d/scratchpad/venv/bin/openapi-spec-validator --version
openapi-spec-validator 0.9.0
$ /tmp/claude-0/-home-user/3c9831de-7860-54c7-a3c3-ba9689ba4e8d/scratchpad/venv/bin/openapi-spec-validator --schema 3.1 --validation-errors all --subschema-errors all api/openapi.yaml
api/openapi.yaml: OK
(exit 0)
```

### Story-coverage script output, verbatim

```
Specification : api/openapi.yaml
PRD           : docs/10-prd-mvp.md §4
Operations    : 118   Schemas: 248   Parameters: 117   Responses: 16   Examples: 9
PRD stories   : 44

Story     Ops  Operations
----------------------------------------------------------------------------------------------------
US-101      1  listProposals
US-102      3  listProposals, getProposalsGeo, getVocabularies
US-103      1  listProposals
US-104      2  getProposalsGeo, getOpportunitiesGeo
US-105     11  listProposals, listProposalSources, listOpportunities, listOpportunitySources, listSources, getSource, listLicences, createExport, bulkProposals, bulkOpportunities, feedProposals
US-201      3  getProposal, listProposalSources, adminMergeProposal
US-202      7  listProposalEvents, listOpportunityEvents, listEvents, adminMergeProposal, adminUnmergeProposal, adminListResolutionCandidates, adminDecideResolutionCandidate
US-203      5  getProposal, listProposalMatches, listOrganizations, getOrganization, listOrganizationProposals
US-204      2  createReport, adminListTasks
US-301      3  listOpportunities, getOpportunitiesGeo, feedOpportunities
US-302      5  listProposalEvents, getOpportunity, listOpportunityEvents, listOpportunitySources, getDocument
US-303     11  listOpportunities, getOpportunity, listOpportunitySources, listOrganizations, getOrganization, listOrganizationOpportunities, listSources, adminListSources, adminCreateSource, adminUpdateSource, adminUpdateOrganization
US-401      5  listProposalMatches, listOpportunityMatches, listMatches, webhookMatchAdded, webhookMatchRemoved
US-402      6  listProposalMatches, listOpportunityMatches, listMatches, getMatch, dismissMatch, undismissMatch
US-403      2  adminApproveIntake, adminCreateLead
US-501      4  listSavedSearches, createSavedSearch, previewSavedSearch, feedSavedSearch
US-502      6  createSavedSearch, updateSavedSearch, listAlerts, getAlert, createWebhook, webhookEventPublished
US-503      6  feedProposals, feedOpportunities, feedEvents, feedSavedSearch, sitemapIndex, sitemapPage
US-504      5  listSavedSearches, getSavedSearch, updateSavedSearch, deleteSavedSearch, listAlerts
US-601      9  listProposals, getProposalsGeo, getProposal, listProposalEvents, listOpportunities, getOpportunity, listOpportunityEvents, listEvents, adminUpdateSource
US-602      5  getMe, getAccount, adminListCustomers, adminListSubscriptions, adminCreateSubscription
US-603      3  listExports, createExport, getExport
US-604      9  listProposals, getProposal, getOpportunity, listEvents, getHealth, feedProposals, feedOpportunities, feedEvents, sitemapIndex
US-701      7  getMe, listKeys, createKey, revokeKey, adminListKeys, adminCreateKey, adminRevokeKey
US-702      1  getMe
US-703     28  listProposals, getProposal, listProposalSources, listOpportunities, getOpportunity, listOrganizations, getOrganization, listEvents, getEvent, getVocabularies, listMatches, getMatch, bulkProposals, bulkOpportunities, bulkEvents, listWebhooks, createWebhook, getWebhook, deleteWebhook, testWebhook, replayWebhook, listWebhookDeliveries, feedEvents, webhookEventPublished, webhookMatchAdded, webhookMatchRemoved, webhookRecordUnpublished, webhookTest
US-704      6  listSources, getSource, listLicences, getLicence, createKey, adminCreateKey
US-801      2  adminListPosts, adminUpdatePost
US-802      6  adminListPosts, adminUpdatePost, adminApprovePost, adminRejectPost, adminSchedulePost, adminSetChannelAutoPublish
US-803      5  adminListPosts, adminGetPost, adminApprovePost, adminSchedulePost, adminSetChannelAutoPublish
US-804      2  adminListPosts, adminSetChannelAutoPublish
US-901      9  adminUpdateProposal, adminUpdateOpportunity, adminUpdateOrganization, adminListUsers, adminGetUser, adminUpdateUser, adminDeleteUser, adminGetCustomer, adminListAudit
US-902      6  getAccount, adminListCustomers, adminCreateCustomer, adminGetCustomer, adminListSubscriptions, adminCreateSubscription
US-903      5  adminListCustomers, adminCreateCustomer, adminListSubscriptions, adminCreateSubscription, adminCreateLead
US-904     10  getHealth, adminListSources, adminCreateSource, adminGetSource, adminUpdateSource, adminRunSource, adminListSourceRuns, adminGetSourceRun, adminGetSnapshot, adminGetCosts
US-905     11  adminListSources, adminSetSourcePublishState, adminSetLicenceGate, adminGetProposal, adminUpdateProposal, adminGetOpportunity, adminUpdateOpportunity, adminUpdateOrganization, adminSetRecordPublishState, adminListAudit, webhookRecordUnpublished
US-906      3  adminSetSourcePublishState, adminSetLicenceGate, adminSetRecordPublishState
US-907     14  createReport, adminGetProposal, adminUpdateProposal, adminMergeProposal, adminUnmergeProposal, adminListResolutionCandidates, adminDecideResolutionCandidate, adminListExtractions, adminAcceptExtraction, adminRejectExtraction, adminListTasks, adminGetTask, adminUpdateTask, adminListAudit
US-908      1  getHealth   [release gate (docs/23 §11) — exposed as launch-checklist probes on GET /v1/health]
US-909      3  adminListSourceRuns, adminListExtractions, adminGetCosts
US-910      3  adminListTasks, adminUpdateTask, adminDeleteUser
US-1001     2  submitIntakeProposal, adminApproveIntake
US-1002     7  submitIntakeProposal, submitIntakeOpportunity, adminMergeProposal, adminSetRecordPublishState, adminListTasks, adminGetTask, adminApproveIntake
US-1003     2  submitIntakeOpportunity, adminApproveIntake

RESULT: PASS — 44/44 PRD stories covered by 118 operations; all $refs resolve
```

## What is in the document

### Endpoint groups

| Group | `docs/23` | Paths | Tier |
|---|---|---|---|
| Proposals: list, geo, detail, events, sources, matches | §3.1 | `/v1/proposals…` | public |
| Opportunities: list, geo, detail, events, sources, matches | §3.1 | `/v1/opportunities…` | public |
| Organizations: list, detail, proposals, opportunities | §3.1 | `/v1/organizations…` | public |
| Events: global feed, get-by-id | §3.1 | `/v1/events…` | public |
| Documents | §3.1 | `/v1/documents/{document_id}` | public |
| Sources and licences registers | §3.1 | `/v1/sources…`, `/v1/licences…` | public |
| Vocabularies, health | §3.1 | `/v1/meta/vocabularies`, `/v1/health` | public |
| Intake and problem reports | §3.1 | `/v1/intake/…`, `/v1/reports` | public (write-only) |
| Account, saved searches, alerts, exports, matches, dismissals | §3.2 | `/v1/me`, `/v1/account`, `/v1/saved-searches…`, `/v1/alerts…`, `/v1/exports…`, `/v1/matches…` | pro |
| Keys, bulk NDJSON, webhooks | §3.2 | `/v1/keys…`, `/v1/bulk/…`, `/v1/webhooks…` | api |
| Admin sources, runs, snapshots, gates | §4 | `/admin/v1/sources…`, `/admin/v1/source-runs…`, `/admin/v1/snapshots/…`, `/admin/v1/licences/…/gate` | admin |
| Admin records: edit, merge/unmerge, publish state, resolution, extraction | §4 | `/admin/v1/proposals…`, `/admin/v1/opportunities…`, `/admin/v1/organizations…`, `/admin/v1/records/…`, `/admin/v1/resolution-candidates…`, `/admin/v1/extractions…` | admin |
| Admin social | §4 | `/admin/v1/posts…`, `/admin/v1/channels/{channel}/auto-publish` | admin |
| Admin tasks and intake review | §4 | `/admin/v1/tasks…` | admin |
| Admin users, customers, subscriptions, leads, keys | §4 | `/admin/v1/users…`, `/admin/v1/customers…`, `/admin/v1/subscriptions…`, `/admin/v1/leads`, `/admin/v1/keys…` | admin |
| Admin costs and audit | §4 | `/admin/v1/costs`, `/admin/v1/audit` | admin |
| RSS, JSON Feed, private saved feeds, sitemaps | §9.2 | `/feeds/…`, `/sitemap.xml`, `/sitemaps/…` | public (saved feeds: pro) |
| Outbound webhooks | §9.1 | `webhooks:` — `event.published`, `match.added`, `match.removed`, `record.unpublished`, `webhook.test` | api |

### The things the contract has to get right

**Attribution envelope on every data response** (`docs/23` §10, `docs/04` API-5). `Envelope` (`data`, `meta`,
`licence_summary`, `redactions[]`) and `ListEnvelope` (adds `page`) are the base of every response schema —
including feed items, CSV exports and webhook deliveries, and including the first line of every NDJSON bulk page
(`BulkMetaLine`). Every record carries `provenance[]` (`source_id`, `source_url`, `retrieved_at`, `licence_id`,
`reuse_class`, `attribution_text`); Pro+ detail responses add `field_provenance`. `licence_summary.sources[]`
carries one entry per distinct source in the payload plus a ready-to-print `attribution_line`.

**`redactions[]`** says what exists on the record but did not travel, and why — `licence`, `licence_precision`,
`tier`, `personal_data`, `publish_state`. Partial withholding is a `200` with a redaction entry, never an error
(`docs/23` §10 last line). A record the caller may not see is `404`, never `403`, so existence does not leak
(`docs/21` §8 item 3).

**Tier behaviour** is a reusable `x-tier` on every operation plus the `x-tier-definitions` extension at the root,
which states the predicate, field classes and `lag_days` per tier and the `public_at` semantics in full: `public_at`
is materialised `published_at + lag(source_id, event_type)` and is the only column the public predicate reads;
Pro/API/admin read `published_at`; a lag change enqueues a `relag` job and is not instantaneous over history.
`meta.tier`, `meta.lag_days` and `meta.data_as_of` carry it into every response for the "live in Pro" banner.

**Map** (`GET /v1/proposals/geo`, `GET /v1/opportunities/geo`) takes `bbox` + `zoom` plus the full list filter set
and returns a GeoJSON `FeatureCollection`. Cluster features carry `count`, `lifecycle_state_counts`,
`technology_counts` (opportunities: `status_counts`), `capacity_mw_sum`, their own `bbox` and `expands_to_zoom`;
below the split threshold individual record features are returned. The **restricted-precision rule** (`docs/04` D-9,
US-104 AC3) is in the schema, not only the prose: `precision`, `precision_reason: licence` and `precision_note`
on the feature, a `licence_precision` entry in `redactions[]`, and county centroids in place of exact points.
Records with no geography are counted in `meta.unplaced_count` rather than dropped.

**Auth** — `securitySchemes` `ApiKey` (bearer `bk_live_`/`bk_test_`), `Session` (cookie), `AdminSession` (cookie on
the admin host). The `docs/23` §5 scopes `read:public`, `read:live`, `read:bulk`, `write:webhooks`, `admin:*` are
carried as the role list on each operation's security requirement, which OpenAPI 3.1 permits for non-OAuth schemes.

**Rate limits** — `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`, `RateLimit-Policy` are documented
response headers on every operation; `Retry-After` on `429`. The per-tier table of `docs/23` §6 is the root
`x-rate-limits` extension.

**Pagination and filters** — cursor only, no offset parameter exists. `limit`/`cursor`/`include`, the range
operators `[gte] [lte] [gt] [lt] [from] [to]`, comma-separated OR within a facet, and per-resource `sort`
allowlists enforced by `pattern`. The grammar is restated once in the root `x-filter-grammar` extension. Unknown
parameters are `400 unknown_parameter`, never ignored.

**Errors** — one `Problem` schema (RFC 9457, `application/problem+json`) with the 15 `code` values of `docs/23` §8
and a `pattern` binding `type` to `https://api.infraque.com/errors/{code}`, so a typo in a `type` URI fails
validation. Every `4xx`/`5xx` in the document uses it.

**Deprecation** — `Deprecation` and `Sunset` (RFC 8594) are documented headers on every operation, absent unless the
operation or version is deprecated; the policy (≥ 6 months, email to keys used in the prior 90 days, ≥ 30 days for
vocabulary additions) is in `info.description` per `docs/04` API-7.

**`x-prd-stories`** on every operation, including the five outbound webhooks, plus `x-status` and `x-sprint` so the
Sprint 1 / 2 / 3 split of `docs/10` §6 is machine-readable. Everything is `x-status: planned` today.

## Story coverage

Generated by `api/check_story_coverage.py`; titles from `docs/10-prd-mvp.md` §4.

| Story | Title | Ops | Operations |
|---|---|---:|---|
| US-101 | Browse proposals list | 1 | `listProposals` |
| US-102 | Filter proposals | 3 | `listProposals`, `getProposalsGeo`, `getVocabularies` |
| US-103 | Free-text search | 1 | `listProposals` |
| US-104 | Map as a primary navigation surface | 2 | `getProposalsGeo`, `getOpportunitiesGeo` |
| US-105 | Attribution on lists | 11 | `listProposals`, `listProposalSources`, `listOpportunities`, `listOpportunitySources`, `listSources`, `getSource`, `listLicences`, `createExport`, `bulkProposals`, `bulkOpportunities`, `feedProposals` |
| US-201 | Proposal detail page | 3 | `getProposal`, `listProposalSources`, `adminMergeProposal` |
| US-202 | Lifecycle timeline | 7 | `listProposalEvents`, `listOpportunityEvents`, `listEvents`, `adminMergeProposal`, `adminUnmergeProposal`, `adminListResolutionCandidates`, `adminDecideResolutionCandidate` |
| US-203 | Linked organisations and opportunities | 5 | `getProposal`, `listProposalMatches`, `listOrganizations`, `getOrganization`, `listOrganizationProposals` |
| US-204 | Report a problem | 2 | `createReport`, `adminListTasks` |
| US-301 | Browse opportunities | 3 | `listOpportunities`, `getOpportunitiesGeo`, `feedOpportunities` |
| US-302 | Opportunity detail | 5 | `listProposalEvents`, `getOpportunity`, `listOpportunityEvents`, `listOpportunitySources`, `getDocument` |
| US-303 | Curated issuer registry | 11 | `listOpportunities`, `getOpportunity`, `listOpportunitySources`, `listOrganizations`, `getOrganization`, `listOrganizationOpportunities`, `listSources`, `adminListSources`, `adminCreateSource`, `adminUpdateSource`, `adminUpdateOrganization` |
| US-401 | Rule-based matches | 5 | `listProposalMatches`, `listOpportunityMatches`, `listMatches`, `webhookMatchAdded`, `webhookMatchRemoved` |
| US-402 | Match explanation | 6 | `listProposalMatches`, `listOpportunityMatches`, `listMatches`, `getMatch`, `dismissMatch`, `undismissMatch` |
| US-403 | Lead hand-off | 2 | `adminApproveIntake`, `adminCreateLead` |
| US-501 | Save a search | 4 | `listSavedSearches`, `createSavedSearch`, `previewSavedSearch`, `feedSavedSearch` |
| US-502 | Email alerts | 6 | `createSavedSearch`, `updateSavedSearch`, `listAlerts`, `getAlert`, `createWebhook`, `webhookEventPublished` |
| US-503 | RSS feeds | 6 | `feedProposals`, `feedOpportunities`, `feedEvents`, `feedSavedSearch`, `sitemapIndex`, `sitemapPage` |
| US-504 | Manage alerts | 5 | `listSavedSearches`, `getSavedSearch`, `updateSavedSearch`, `deleteSavedSearch`, `listAlerts` |
| US-601 | Tier enforcement is server-side | 9 | `listProposals`, `getProposalsGeo`, `getProposal`, `listProposalEvents`, `listOpportunities`, `getOpportunity`, `listOpportunityEvents`, `listEvents`, `adminUpdateSource` |
| US-602 | Pro entitlement | 5 | `getMe`, `getAccount`, `adminListCustomers`, `adminListSubscriptions`, `adminCreateSubscription` |
| US-603 | Export | 3 | `listExports`, `createExport`, `getExport` |
| US-604 | Public tier messaging | 9 | `listProposals`, `getProposal`, `getOpportunity`, `listEvents`, `getHealth`, `feedProposals`, `feedOpportunities`, `feedEvents`, `sitemapIndex` |
| US-701 | API keys | 7 | `getMe`, `listKeys`, `createKey`, `revokeKey`, `adminListKeys`, `adminCreateKey`, `adminRevokeKey` |
| US-702 | Rate limits | 1 | `getMe` |
| US-703 | Read endpoints | 28 | `listProposals`, `getProposal`, `listProposalSources`, `listOpportunities`, `getOpportunity`, `listOrganizations`, `getOrganization`, `listEvents`, `getEvent`, `getVocabularies`, `listMatches`, `getMatch`, `bulkProposals`, `bulkOpportunities`, `bulkEvents`, `listWebhooks`, `createWebhook`, `getWebhook`, `deleteWebhook`, `testWebhook`, `replayWebhook`, `listWebhookDeliveries`, `feedEvents`, `webhookEventPublished`, `webhookMatchAdded`, `webhookMatchRemoved`, `webhookRecordUnpublished`, `webhookTest` |
| US-704 | API docs and terms | 6 | `listSources`, `getSource`, `listLicences`, `getLicence`, `createKey`, `adminCreateKey` |
| US-801 | Post drafts from events | 2 | `adminListPosts`, `adminUpdatePost` |
| US-802 | Review queue | 6 | `adminListPosts`, `adminUpdatePost`, `adminApprovePost`, `adminRejectPost`, `adminSchedulePost`, `adminSetChannelAutoPublish` |
| US-803 | Publish and measure | 5 | `adminListPosts`, `adminGetPost`, `adminApprovePost`, `adminSchedulePost`, `adminSetChannelAutoPublish` |
| US-804 | Replies are drafted, not sent | 2 | `adminListPosts`, `adminSetChannelAutoPublish` |
| US-901 | Users | 9 | `adminUpdateProposal`, `adminUpdateOpportunity`, `adminUpdateOrganization`, `adminListUsers`, `adminGetUser`, `adminUpdateUser`, `adminDeleteUser`, `adminGetCustomer`, `adminListAudit` |
| US-902 | Customers and subscriptions are read from the CRM/ERP | 6 | `getAccount`, `adminListCustomers`, `adminCreateCustomer`, `adminGetCustomer`, `adminListSubscriptions`, `adminCreateSubscription` |
| US-903 | CRM/ERP adapter | 5 | `adminListCustomers`, `adminCreateCustomer`, `adminListSubscriptions`, `adminCreateSubscription`, `adminCreateLead` |
| US-904 | Source health | 10 | `getHealth`, `adminListSources`, `adminCreateSource`, `adminGetSource`, `adminUpdateSource`, `adminRunSource`, `adminListSourceRuns`, `adminGetSourceRun`, `adminGetSnapshot`, `adminGetCosts` |
| US-905 | Publish/unpublish with licence gate | 11 | `adminListSources`, `adminSetSourcePublishState`, `adminSetLicenceGate`, `adminGetProposal`, `adminUpdateProposal`, `adminGetOpportunity`, `adminUpdateOpportunity`, `adminUpdateOrganization`, `adminSetRecordPublishState`, `adminListAudit`, `webhookRecordUnpublished` |
| US-906 | Publish gate on records and posts | 3 | `adminSetSourcePublishState`, `adminSetLicenceGate`, `adminSetRecordPublishState` |
| US-907 | Task queue | 14 | `createReport`, `adminGetProposal`, `adminUpdateProposal`, `adminMergeProposal`, `adminUnmergeProposal`, `adminListResolutionCandidates`, `adminDecideResolutionCandidate`, `adminListExtractions`, `adminAcceptExtraction`, `adminRejectExtraction`, `adminListTasks`, `adminGetTask`, `adminUpdateTask`, `adminListAudit` |
| US-908 | Launch checklist | 1 | `getHealth` *(release gate, docs/23 §11 — the launch-checklist probes)* |
| US-909 | Model-call cost log | 3 | `adminListSourceRuns`, `adminListExtractions`, `adminGetCosts` |
| US-910 | Personal data and deletion | 3 | `adminListTasks`, `adminUpdateTask`, `adminDeleteUser` |
| US-1001 | Intake form | 2 | `submitIntakeProposal`, `adminApproveIntake` |
| US-1002 | Intake review and matching | 7 | `submitIntakeProposal`, `submitIntakeOpportunity`, `adminMergeProposal`, `adminSetRecordPublishState`, `adminListTasks`, `adminGetTask`, `adminApproveIntake` |
| US-1003 | Opportunity intake (utility) | 2 | `submitIntakeOpportunity`, `adminApproveIntake` |

## Open decisions for the solutions-architect

Places where `docs/23` left a choice open, or where following it literally would have broken something. Each says
what this file does now, so reversing any of them is a bounded edit.

**1. One document or three.** `docs/23` §1 gives three base URLs. OpenAPI path keys must be unique across a single
document, and the public and admin groups reuse names (`/sources`, `/keys`). This file therefore carries the version
prefix in the path keys (`/v1/…`, `/admin/v1/…`), leaves `servers` as bare hosts, and puts a path-level `servers`
override on every admin and feed path. It is one document, one Redoc page, one contract test. The alternative is
three documents (public, admin, feeds) with three Redoc pages. **Decide before Sprint 1 generation**, because
FastAPI's mount structure follows from it.

**2. Branded header names.** `docs/23` §5 and §9.1 spell the webhook headers with the codename
(`X-Bankable-Signature`, `X-Bankable-Delivery-Id`, `X-Bankable-Event-Seq`) and the JSON Feed extension key
`_bankable`. The naming decision (`docs/00-PLAN.md`) makes those placeholders. This file uses `X-Platform-*` and
`_platform` and lists them in the Placeholders table. Alternative worth considering: keep a brand-free prefix
permanently, so the rename never touches integrator code.

**3. `bk_live_` / `bk_test_` key prefixes kept verbatim.** They encode the codename but they are also the value of
`api_key.prefix` in `docs/21` §3.17 and appear in stored rows. This file keeps them so the schema matches the data
model. If they are to be renamed with everything else, say so now — a key prefix cannot change after the first key
is issued.

**4. The `legal` role does not exist in the data model.** US-905 AC1 and `docs/23` §4 require the `legal` role to
clear a licence gate, but `user.role` in `docs/21` §3.12 is `viewer | member | operator | owner`. This file requires
`AdminSession: [legal]` on `PUT /admin/v1/licences/{licence_id}/gate` and leaves `user.role` unchanged, so the
document is currently ahead of the schema. Either add `legal` to the `user.role` vocabulary, or model it as a
separate grant (a `permissions[]` column, or an `account`-level flag). **This blocks implementing US-905 AC1.**

**5. Default lag: class-split, not a single number.** `docs/23` §10 and P-2 show 14 days; `docs/04` §10 pick 1
(the working default) is 7 days for opportunities and 14 for supply, per source class. This file follows `docs/04`:
`x-tier-definitions` and `HealthResponse.lag_days_default` carry both, the proposal examples show `lag_days: 14`
and the opportunity example `lag_days: 7`. `docs/23` §10's single number should be updated to match, or `docs/04`
overturned.

**6. An opportunities map endpoint that `docs/23` does not list.** §3.1 has only `GET /v1/proposals/geo`, but
`docs/04` D-11 requires opportunity service territories on the map and D-4 requires one filter grammar across list,
map and feed. This file adds `GET /v1/opportunities/geo` with the same contract plus polygon features. The
alternative is one `/v1/map` returning both families in one `FeatureCollection`, which is fewer round trips for the
map page but mixes two filter sets in one query string. **Confirm the shape before the map page is designed.**

**7. bbox + zoom, no tile endpoint.** `docs/23` §7 and `docs/04` D-10 both name `bbox` and `zoom`, so there is no
`/tiles/{z}/{x}/{y}` here. If the front end ends up on vector tiles (MapLibre's natural input, `docs/04` ST-1), a
tile endpoint returning MVT is an additive change but needs a different cache story — worth settling with
product-designer before D-13's budgets are measured.

**8. `meta.total` is opt-in but US-101 AC2 asks for a total.** `docs/23` §7 says `meta.total` appears only with
`include=count`. This file keeps that, which means the web list view must pass `include=count` on every request and
pay the documented extra query. Confirm that reading, or make `count` the default for the list endpoints and opt
*out* for integrators.

**9. Synchronous CSV and the async export job coexist.** `docs/23` §1 says `Accept: text/csv` on list endpoints
returns an export (Pro+); §3.2 also has `POST /v1/exports`. Both are in the document. No threshold is fixed at
which a synchronous CSV must be refused in favour of a job — needs a number (row count or estimated bytes) before
the export service is built.

**10. Bulk NDJSON carries the envelope as its first line.** `docs/23` §10 requires the attribution envelope on
every response; NDJSON has no envelope slot. This file makes the first line of every `/v1/bulk/*` page a `meta`
record (`BulkMetaLine`) with `page`, `meta`, `licence_summary` and `redactions`, so a consumer that stops early
still holds the credit lines. The alternative is response headers plus a `Link` header, which is harder to archive
with the data. Confirm.

**11. Feeds and sitemaps are in this document.** `docs/23` §9.2 lists `/feeds/*` and the sitemaps; they return XML
and RSS, not JSON, and they live on the product host rather than `api.`. They are included so the contract covers
US-503 end to end. If the web app owns those routes, they should move out of the OpenAPI document and into the web
app's route table, and US-503's contract test moves with them.

**12. Operations `docs/23` implies but does not enumerate.** Added on the strength of the acceptance criteria:
`GET /v1/events/{event_id}` and `GET /v1/matches/{match_id}` (US-703 AC1 "get-by-id for each entity"),
`GET /v1/opportunities/{public_id}/matches` (the demand-side twin of US-401), `GET /v1/account`,
`GET /v1/saved-searches/{id}`, `POST /v1/webhooks/{id}/replay` (`docs/23` §9.1 prose),
`GET /admin/v1/source-runs/{id}`, `GET /admin/v1/users/{id}`, `GET /admin/v1/customers/{account_id}`,
`POST /admin/v1/customers`, `POST /admin/v1/subscriptions` (US-902 AC2 writes), and
`POST /admin/v1/sources` (US-303 AC2 / US-904 AC2 "add a curated issuer without a code change", restricted to
`category = procurement` and `access ∈ {html, rss, api}`). Each should be confirmed or struck.

**13. Merge preview staleness needs a mechanism, so this file invents one.** `docs/23` §8 lists "merge preview
stale" under `409 conflict` without saying how staleness is detected. This file returns a `preview_token` from a
preview (valid 15 minutes) which must be sent with the write; if either record changed since, the write is `409`.
Confirm the token and its window.

**14. Captcha field name.** `docs/23` P-5 says intake is captcha-gated but does not name the field or the provider.
This file adds `captcha_token` to both intake requests and to `POST /v1/reports`. The provider is unnamed.

**15. Per-record publish-state path renamed for consistency.** `docs/23` §4 writes
`PUT /admin/v1/records/{type}/{id}/publish-state`. This file uses `{record_type}` (plural:
`proposals | opportunities | organizations`) and `{public_id}`, matching every other path in the document.

**16. US-908 has no endpoint, by design.** `docs/23` §11 says so. Rather than special-casing the coverage check,
US-908 is attached to `GET /v1/health`, which exposes the launch-checklist probes (rate limiter active, backup age,
queue age) as booleans, and the script prints the note next to the row. If the architect prefers 43 endpoint-backed
stories plus one explicitly excluded, change `NOTE` into an exclusion list in `check_story_coverage.py`.

**17. Rate-limit header form.** `docs/04` API-6 cites the IETF RateLimit header-fields draft. Later revisions of
that draft replace the four separate fields with one `RateLimit` structured field. This file documents the four
fields of `docs/23` §6 (`RateLimit-Limit`, `-Remaining`, `-Reset`, `-Policy`), which is what integrators' clients
expect today. Revisit when the draft reaches RFC.

**18. Scopes are carried in the security requirement role list.** OpenAPI 3.1 permits a list of role names on a
non-OAuth scheme, so `ApiKey: [read:live, read:bulk]` and `Session: [pro]` are legal and readable. No tooling
enforces them — enforcement is `scopes ∩ plan_tier` in code. If the architect would rather they be machine-checked,
the alternative is an `x-scopes` extension plus a lint rule, or modelling keys as OAuth2 client credentials.

**19. `restricted` and `unknown` sources are invisible on Pro and API too.** The document follows `docs/21` D-2 and
`docs/04` §10 pick 2 (the stricter reading), not `docs/20` §5's "derived aggregates to Pro/API". Nothing in the
contract exposes a restricted source at any non-admin tier — including `source_count`, map cluster counts and
`meta.total`. If counsel later permits derived aggregates for Pro, the change is a licence-flag change plus one
paragraph in `x-tier-definitions`, not a schema change.

## Related

- `docs/23-api-spec-outline.md` — normative outline; this file implements it
- `docs/21-data-model.md` — entities, field tables, §5.4 visibility predicate, §7 vocabularies, §8 gating
- `docs/04-standards.md` §5 — API-1…API-9, the rules this file is reviewed against
- `docs/10-prd-mvp.md` §4 — the 44 stories `x-prd-stories` points at
- `docs/adr/0002-language-and-runtime.md` — why the document is generated from Pydantic models from Sprint 1
