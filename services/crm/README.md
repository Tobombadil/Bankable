# CRM adapter (Attio)

## What this is

The Attio adapter behind `services.sor.ports.CrmPort` (docs/34-crm-system-of-record.md), its
in-memory fake, the pure lead-scoring-signal mapping, and the two HTTP routes that touch the
CRM boundary: the inbound Attio webhook and the US-403 lead hand-off endpoint.

| File | Contents |
|---|---|
| `attio.py` | `AttioCrmAdapter` (`sor_kind = "attio"`) and `build_crm_port()` |
| `fake.py` | `InMemoryCrm` (`sor_kind = "attio_fake"`) — dry-run fallback and test double |
| `signals.py` | `signal_from_event`: pure platform-event → `LeadSignal` mapping (docs/33 §2.3) |
| `router.py` | `POST /webhooks/attio`, `POST /admin/v1/leads` |
| `test_*.py` | Adapter, fake, signal-mapping and router tests |

Everything here is vendor-shaped on the inside only. No `CrmPort` caller anywhere else in the
platform imports `services.crm.attio` directly — they depend on the port
(`services.sor.ports.CrmPort`) and get an implementation from `services.sor.wiring.get_crm_port`.

## Endpoints used (Attio REST API v2, `https://api.attio.com`)

Verified against the vendor's OpenAPI document and rate-limits/webhooks guide pages (fetched
2026-09-13; see the coordinator's vendor evidence). Docs root: `https://docs.attio.com`.

| Method & path | Purpose | Scope needed |
|---|---|---|
| `PUT /v2/objects/{object}/records?matching_attribute=<slug>` | Upsert Company / Lead Signal / Subscription | `record_permission:read-write`, `object_configuration:read` |
| `POST /v2/objects/{object}/records` | Create Deal | `record_permission:read-write` |
| `GET /v2/objects/{object}/records/{id}` | Read a Company back | `record_permission:read` |
| `POST /v2/objects/{object}/records/query` | Find a Company or Lead Signal by attribute | `record_permission:read` |
| `PUT /v2/lists/{list}/entries` | Add an unresolved Lead Signal to "Unmatched signals" | `list_entry:read-write`, `list_configuration:read` |
| `POST /v2/notes` | Log an activity note on a Company; the deal hand-off note | `note:read-write` |
| `POST /v2/tasks` | Open a personal-data-deletion task | `task:read-write` |
| (owner-managed, not called by this adapter) `POST /v2/webhooks` | Registers the two outbound webhooks (docs/34 §6 item 4) | `webhook:read-write` |

`docs/api/rest-api/*` on `docs.attio.com` documents each of these; `docs/api/webhook-reference`
documents delivery, signing and filtering for the last row.

## Rate limits and how the adapter handles them

Attio: 100 req/s read, 25 req/s write, workspace-wide; webhook delivery to one target URL capped
at 25/s (owner's concern, not the adapter's). A `429` always carries `Retry-After`, documented by
Attio as an HTTP-date but accepted here as either an HTTP-date or a plain integer-seconds value
(defensive — the task spec calls for both). The adapter:

- Retries a `429` up to 3 times, sleeping `min(Retry-After, 5s)` between attempts (the cap and the
  `sleep` injection point are both in code, never `time.sleep` directly, so tests never wait) —
  exhausting the budget raises `SorUnavailable`.
- Retries a `5xx` or a transport-level failure (connection error, timeout) once, then raises
  `SorUnavailable`.
- Raises `SorRejected` on any other `4xx`, with the vendor's response body in the exception
  message only — never surfaced in an API response (docs/23 §8: router maps it to a generic
  `409 conflict` or logs it, per endpoint).

## Webhook verification

Header `Attio-Signature` (duplicated as `X-Attio-Signature` for legacy middleware) = lowercase hex
HMAC-SHA256 of the raw request body using `ATTIO_WEBHOOK_SECRET`, no timestamp component. The
adapter:

- Rejects (`WebhookRejected`) when no secret is configured — an unsigned webhook is never
  accepted, even in an environment that hasn't set the secret yet.
- Rejects on a missing or mismatched signature (`hmac.compare_digest`, constant-time).
- Accepts either payload shape: a single bare event object, or `{"events": [...]}`.
- Maps only `record.updated` on Companies → `company.updated`, and `record.updated` /
  `record.created` on Lead Signals → `lead_signal.updated`; everything else is silently skipped
  (not an error — an unsubscribed event type reaching the endpoint is not a client mistake).

## Slugs and the object-id assumption (A-34-1)

**A-34-1.** docs/34 §6 tells the owner to use exact slugs so the mapping is a constant, not
config. This adapter's constants: custom objects `lead_signals`, `subscriptions`; list
`unmatched_signals`; standard objects `companies`, `deals` (Attio's own slugs). If the owner's
workspace ends up with different slugs, every `PUT`/`POST .../objects/{object}/...` call in
`attio.py` needs the corresponding constant updated — there is no per-call configuration.

The inbound webhook payload identifies the changed object by **UUID**, not slug (verified in the
OpenAPI document: `id.object_id` is `format: uuid`). The constructor accepts
`object_ids: Mapping[str, str]` (slug → UUID), loaded by `build_crm_port()` from the environment
variable `ATTIO_OBJECT_IDS` as a JSON object, e.g. `{"companies": "97052eb9-...", "lead_signals":
"c412...")`. An object id with no entry in the map resolves to the sentinel slug `"unknown"` and
the event is skipped — this is expected on day one before the owner has recorded the real UUIDs
from their workspace, not a bug.

## Decisions and deviations

1. **D-1 — `InMemoryCrm.sor_kind = "attio_fake"`, not `"attio"`.** The fake stands in for Attio in
   dry-run mode and in tests; giving it a distinct `sor_kind` means an `account.sor_kind` /
   `match.crm_lead_ref` written while the fake is active can never be mistaken for a real Attio
   reference once the workspace is wired up and `ATTIO_API_KEY` is set.
2. **D-2 — `CompanyUpsert.name` is never written**, by either the real adapter or the fake, even
   though the port dataclass carries the field. docs/34 §5 lists exactly which attributes the
   adapter may set on Company (`platform_org_id`, `platform_account_id`, `lead_score`,
   `lead_band`, `top_event*`); `name` is not among them — it is human/Attio-managed. The task
   brief's own instruction is explicit on this point; `_ADAPTER_OWNED_COMPANY_FIELDS` in
   `attio.py` is the single place this allow-list lives.
3. **D-3 / D-4 — event-type mapping gaps filled by the closest reasonable signal** (`signals.py`
   module docstring has the full table and rationale): platform `proposal.cancelled` is treated
   like `withdrawn` (docs/34 §2.4 has no separate `proposal.cancelled` signal type); platform
   `opportunity.closed` and `opportunity.due_date_changed` both map to `opportunity.rfp_closing`
   (docs/33 §2.3's scoring table has no explicit "closing" row; this reuses the
   `cancelled`/`reinstated` row's 18 points as the closest urgency signal). `load.request_filed`
   and `org.first_seen` from that same table have no platform `Event` today and are not
   approximated — they simply never fire until a `load` subject or an `organization`-kind Lead
   Signal exists (the latter is disallowed by docs/34 §2.4's `subject_kind` vocabulary anyway).
4. **D-6 — `LeadCreate.notes` is carried into the audit event's `after` payload**, not into any
   CRM record. The task brief lists `notes` as an accepted request field but does not say where it
   goes; docs/34 has no "notes" attribute on Deal or Lead Signal, so inventing one would violate
   "never any human-owned field" by a different route. Filing it on the audit trail keeps the
   operator's context queryable without adding an unreviewed field to Attio.
5. **D-7 — `LeadSignal.signal_id` for the US-403 hand-off is the match's own public id**
   (`public_id("mat", match.id)`), not a platform event id. `create_lead_signal` runs before the
   audit event is written (task's own ordering), so there is no event id yet to key off; the
   match-based id is deterministic and the 409-on-`crm_lead_ref`-already-set check makes the whole
   endpoint idempotent per match regardless.
6. **D-9 — no company upsert when the sponsor organisation has no usable website.**
   `CompanyUpsert.domain` is required (non-`None`) by the port; rather than inventing a
   placeholder domain, the hand-off simply skips `upsert_company` and proceeds — the lead signal
   and deal still get created, and (if `platform_org_id` doesn't resolve to an existing company
   either) land on Attio's "Unmatched signals" list per docs/34 §5, which is the documented,
   correct outcome for a sponsor the CRM doesn't know yet, not an error condition.
7. **D-10 — record-query filter grammar, verified.** Attio's filtering-and-sorting guide
   (`https://docs.attio.com/rest-api/guides/filtering-and-sorting`, fetched 2026-09-13) documents
   two forms: a shorthand (`{"name": "John Smith"}`, an implied `$eq` on the attribute's default
   property) and a verbose form that nests an explicit operator, required whenever filtering on a
   *property* of the attribute value rather than its default (e.g. `domain` on a `domains`
   attribute) — `$eq` is supported by every attribute type. `_query_company` (and the
   `lead_signals` lookup in `create_deal`) use the verbose form throughout:
   `{"filter": {"domains": {"domain": {"$eq": "<value>"}}}, "limit": 1}` for the domain attribute,
   `{"filter": {"platform_org_id": {"$eq": "<value>"}}, "limit": 1}` for a plain text attribute
   (`platform_org_id`, `platform_account_id`, `signal_id`). The previous revision of this file
   used an unverified mixed shorthand/property shape (`{"domains": {"domain": "<value>"}}`, a
   property key with no operator) — the guide does not show that as valid; this replaces it.
8. **`DealCreate` carries no monetary `value` field.** The task brief's field list for the Deal
   write mentions `value`, but `services.sor.ports.DealCreate` (owned by the coordinator) has no
   such field — only `name`, `proposal_public_id`, `opportunity_public_id`, `score`, `rationale`,
   `link_url`, `company_domain`, `platform_org_id`, `originating_signal_id`, `tier`. Nothing is
   sent for Attio's `value` attribute; adding it needs a port change first.
9. **Webhook `applied` semantics.** `received` counts every event `parse_webhook` mapped to a
   known kind; `applied` counts only the subset that caused a platform-side write. Today that is
   `company.updated` changes where the cached `account.name` actually differed — `lead_signal.
   updated` is logged (see "what is deferred" below) but never increments `applied`, since there
   is nothing yet on the platform side for it to change.
10. **D-11 — vendor exception text never reaches a response body, in `router.py` too.** The
    adapter's `SorRejected`/`SorUnavailable` messages carry the vendor's own response text
    (`attio.py`'s `_request`), which is exactly why `docs/23` §8 keeps it out of API responses. All
    three router branches that catch a port error (`admin_create_lead`'s `SorUnavailable`, its
    `SorRejected`, and the webhook handler's `WebhookRejected` and `SorUnavailable`) log the
    exception at `WARNING` and raise a fixed, generic `detail` (`"The CRM could not be reached; try
    again shortly."` for `sor_unavailable`; no `detail` at all for the webhook's `401`, matching
    `"conflict"`'s existing generic detail for `SorRejected`) — never `str(exc)`.

## What is deferred

- **`lead_signal.updated` → closing the platform task when `handled` flips true.** docs/34 §5
  describes this inbound webhook effect, but the platform task table it needs (`task` in docs/21
  §7's table list) arrives with the admin-panel wave, not this one. `router.py`'s webhook handler
  already receives and counts these events (see D-9/`applied` above) so no webhook redelivery is
  lost; wiring the actual task-close is a follow-up, noted in both the router's docstring and
  here.
- **The scheduled worker that calls `signal_from_event` for every qualifying pipeline event and
  feeds the result into `CrmPort.create_lead_signal`.** This wave ships the pure mapping function
  and its exhaustive tests only (task instruction: "you only ship the function"); wiring it into a
  pipeline step or scheduled job is a later wave.
- **The full docs/33 §2.3 lead-scoring rubric** (fit 0-40, timing 0-40, engagement 0-20).
  `signal_from_event` computes only the timing component (base points × recency decay) because the
  fit and engagement components need account-level CRM data (`segment`, `tools_in_use`,
  `owner_relationship`, reply history) that a pure per-event function does not have access to —
  that combination is the scheduled worker's job, once it exists.
- **Stale-signal housekeeping** (docs/34 §7: mark Lead Signals older than 90 days
  `handled=false, stale=true`) — no `stale` attribute is written anywhere in this wave; it needs
  the owner to add the field to the Lead Signals object first (docs/34 §6 item 1).

Two live-workspace checks remain open (both are the owner's exact slugs/ids, not something a
sandbox test run can confirm): the object-id → slug mapping in `ATTIO_OBJECT_IDS` (A-34-1) once
the owner has created the custom objects, and the record-webhook payload shape (`services/crm/
attio.py`'s `parse_webhook`), reconstructed from Attio's list-entry-event example rather than a
directly fetched `record.*` example. The record-query filter grammar (D-10 above) is no longer on
this list — it is verified against Attio's own guide, not an assumption.

## Verbatim output of the final checks

`.venv/bin/python -m pytest services/crm -q`:

```
........................................................................ [ 82%]
...............                                                          [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/starlette/testclient.py:37
  /home/user/Bankable/.venv/lib/python3.11/site-packages/starlette/testclient.py:37: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = typing.Callable[[], typing.ContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
```

(87 passed, 1 warning, in the equivalent non-`-q` run — this pytest/config combination does not
print a final count line under `-q`.)

`.venv/bin/ruff check services/crm && .venv/bin/ruff format --check services/crm`:

```
All checks passed!
9 files already formatted
```

`.venv/bin/mypy --cache-dir /tmp/mypy-crm services/crm`:

```
Success: no issues found in 5 source files
```

`.venv/bin/python -m coverage run --branch -m pytest services/crm -q && .venv/bin/python -m coverage report --include="services/crm/*"`:

```
Name                           Stmts   Miss Branch BrPart  Cover
----------------------------------------------------------------
services/crm/__init__.py           1      0      0      0   100%
services/crm/attio.py            286     10    116     15    94%
services/crm/fake.py             197      5     60      8    95%
services/crm/router.py           134      9     48     10    90%
services/crm/signals.py           31      1     10      1    95%
services/crm/test_attio.py       434      6     32      6    97%
services/crm/test_fake.py        164      2      0      0    99%
services/crm/test_router.py      326      2      0      0    99%
services/crm/test_signals.py     107      0      6      0   100%
----------------------------------------------------------------
TOTAL                           1680     35    272     40    96%
```
