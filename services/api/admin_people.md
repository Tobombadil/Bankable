# admin_people — users, task queue, customers, subscriptions (`/admin/v1`)

Sprint 3 item 3 (admin panel), my slice: `services/api/admin_people.py` (new, 1487 lines),
`tests/test_api_admin_people.py` (new, 1395 lines, 60 tests). `router = APIRouter()`; the
coordinator mounts it onto `services.api.app.app` and flips the operations' `x-status` in
`api/openapi.yaml` from `planned` to `live`.

## What

Twelve operations under `/admin/v1`, all behind `require_admin()` (`operator`/`owner` session):

- **Users** (US-901): `GET /users`, `GET /users/{id}`, `PATCH /users/{id}` (role/status, owner-only
  grant of `owner`, disabling revokes sessions), `DELETE /users/{id}` (opens a `deletion_request`
  task, disables the user, revokes sessions and the user's own API keys; 202).
- **Tasks** (US-907, US-204, US-910, US-1002): `GET /tasks`, `GET /tasks/{id}`, `PATCH /tasks/{id}`
  (status/assignee/notes; completing a `deletion_request` runs the redaction procedure through the
  CRM port first, then anonymises the row — see "Erasure (2026-09-19)" below),
  `POST /tasks/{id}/approve-intake` (approve creates a Proposal/Opportunity from `pending_record`,
  link merges identifiers into an existing record, reject just closes the task; optional
  `create_lead` and `add_curated_issuer`).
- **Customers** (US-902/903): `GET /customers`, `POST /customers` (creates the Account, then a CRM
  company through the port; refuses with `503` while the adapter is down), `GET /customers/{id}`.
- **Subscriptions**: `POST /subscriptions` (adapter `create_subscription` +
  `apply_entitlement_change`, reusing `services/billing/router.py`'s `_serialize_subscription`).

Every write requires `reason` (`400` if missing), lands an audit event with before/after via
`services.api.audit.record_audit_event`, and is `assert_valid`-checked against `api/openapi.yaml`
in the test suite.

## Decisions (numbered; mirrors the module docstring, with more of the "why")

1. **`Task.pending_record`'s real shape.** The schema types it `AdminProposal | AdminOpportunity |
   null`, not the raw `IntakeProposalRequest`/`IntakeOpportunityRequest` submission shape — read
   literally, it previews the record-to-be. The intake-submission endpoints that would populate it
   (`POST /v1/intake/proposal|opportunity`, US-1001/1003) are unbuilt this sprint
   (`x-status: planned`), so there is no real producer to match against. I made `admin_people.py`
   read defensively — `name_canonical` *or* `project_name`, `sponsor.public_id` *or*
   `sponsor_org_id` *or* a flat `sponsor_name`, similarly for opportunities/issuers — and to accept
   extra top-level convenience keys (`source_id`, `licence_id`, `source_url`, `retrieved_at`) that
   neither schema forbids (no `additionalProperties: false` on either). My own test fixtures
   (`_pending_proposal`/`_pending_opportunity` in the test file) build the full schema-conformant
   shape (placeholder `public_id`/`slug`/`url`/`provenance`/...) so `assert_valid` on
   `TaskDetailResponse`/`IntakeDecisionResponse` actually exercises the contract rather than a
   shape I invented. **Flag for the coordinator/whoever builds the intake-submission endpoint**:
   confirm this reading of `pending_record` against whatever that endpoint actually stores — if it
   turns out to store the raw request body instead, `admin_people.py`'s field-mapping functions
   (`_create_proposal_from_pending`, `_create_opportunity_from_pending`) need their `.get()`
   fallback keys adjusted, not a rewrite.
2. **On-demand intake source id.** Task brief: "a source with id `platform.intake`". Literal id
   fails `SourceIdValue`'s pattern (`^[a-z]{2,6}(\.[a-z0-9_]+){1,4}$` — first segment capped at 6
   lower-case letters; `platform` is 8), which would make every contract check on a
   `ProvenanceRow.source_id` fail for an approved intake record. Used `intake.platform` instead
   (`_INTAKE_SOURCE_ID`), same on-demand-creation behaviour, same open licence
   (`platform-open`, `_INTAKE_LICENCE_ID`, `reuse_class: open`, no gate).
3. **`IntakeDecisionResponse.lead` is always `null`.** `Lead` (the schema referenced) requires
   `match_id` — a `mat_...` reference to the US-401/403 match-hand-off flow. An intake-approval
   lead has no match. The CRM writes (`upsert_company`, `create_lead_signal`) still happen when
   `create_lead: true` and are tested (`test_approve_intake_create_lead_lands_company_and_signal`);
   only the response's `lead` field cannot honestly reuse a schema built for a different flow.
4. **`create_lead` only fires on `decision: approve`.** The request field's own description says
   "subject the new record"; `link` has no new record and `reject` obviously creates nothing.
5. **`add_curated_issuer` is a silent no-op on a proposal intake** (only an opportunity has an
   issuer to flag) rather than a `400` — the request schema defaults the flag `true` regardless of
   intake type, so treating it as an error would make the default request fail on half the tasks.
6. **`Task.subject_id` and the `AnyPublicIdValue` gap.** That schema's pattern accepts only
   `prop_ | opp_ | org_ | mat_ | evt_ | doc_`. A `deletion_request` task's subject is a user
   (`usr_...`), which the pattern cannot express. `_task_subject_public_id` returns `null` for any
   `subject_type` not in its small prefix map rather than emit a string that fails contract
   validation. **Flag for the coordinator**: `AnyPublicIdValue` likely needs `usr_`/`acc_`/`key_`
   added if a deletion task's subject should ever be visible in `GET /admin/v1/tasks`.
7. **Only a user's own API keys are revoked** on disable/delete/redaction
   (`ApiKey.created_by_user_id == user.id`), not every key on the account — deleting one member of
   a multi-user account must not cut every other member's API access. Sessions are still all of the
   user's own sessions (a user has no "other users'" sessions to accidentally revoke).
8. **`CustomerCreate.primary_contact_email` creates the first `User` row.** Its own schema
   description says it is "stored on the resulting user row only" — never on `Account`. Built as
   role `member`, `password_hash = None` (no invitation/reset flow exists this sprint; that user
   cannot log in until one does — an open gap, not hidden).
9. **Organisation-type defaults for intake approval**: `developer` for a proposal's sponsor,
   `other` for an opportunity's issuer (the vocab — `developer, ipp, utility, coop, cca, agency,
   lender, investor, epc, oem, offtaker, other` — has no generic "any issuer" bucket).
   `country` comes from the two-letter prefix of `jurisdiction` (`US-TX` → `US`), else `US` (every
   source this sprint is US-only).
10. **All-or-nothing on adapter failure.** Every CRM/billing call here runs inside the request's one
    `Session` (`services/api/deps.py` `get_db`, commit-on-clean-return / rollback-on-exception): a
    `SorUnavailable`/`SorRejected` raised after other writes (a new Proposal, a Task update) rolls
    all of it back. This matches the deletion-task rule ("nothing half-applied") for free, without
    per-route transaction bookkeeping — verified in
    `test_complete_deletion_task_sor_unavailable_leaves_user_and_task_untouched` and
    `test_create_customer_sor_unavailable_refuses_write`.
11. **`matches_computed` is always `0`.** The US-401 rule engine is a different area entirely;
    nothing in this module recomputes matches. The field says so honestly rather than a fabricated
    count.
12. **Subscription status on `GET /users`/`UserAdminView.subscription_status`** reads the
    *local* `Subscription` mirror (most recently `mirrored_at` row per account), per the task
    brief — not a live adapter call. `GET /customers`/`GET /customers/{id}` similarly build `sor`
    from `account.sor_kind`/`sor_ref`/`entitlement_checked_at`/`entitlement_stale` (mapped onto the
    schema's `kind`/`ref`/`fetched_at`/`stale`), never a live read-through; only the two `POST`s
    (customer, subscription) touch the ports.
13. **`Customer.open_in_crm_url` is always `null`.** `docs/34` records only Attio object/list ids,
    no vendor workspace URL pattern; a guessed link would 404. Said so rather than fabricating one
    (task brief explicitly allows "else null and say so").
14. **`admin_update_task`'s `assignee_user_id`/reason validation errors are `400`, not `404`.**
    An unresolvable `assignee_user_id` in the body is treated like any other bad field value
    (`validation_error`), consistent with how `sponsor_org_id`/`organization_id` are handled
    elsewhere in this module — a referenced-but-missing id in a request body is a client error on
    the body, not a "the resource doesn't exist" on the path.

## Deferred (named, not silently dropped)

- **Idempotency-Key header.** `api/openapi.yaml`'s `IdempotencyKey` parameter ("required on every
  mutating call from an API key; recommended from sessions") is not implemented — every mutating
  route here is `AdminSession`-only (no `ApiKey` security option), and the one precedent in this
  codebase for an admin write (`services/api/pro.py`'s `admin_set_account_entitlement`) does not
  implement it either. Left as a follow-up alongside that existing gap, not invented fresh here.
- **`GET /admin/v1/customers`'s N+1 subscription/company reads.** `_serialize_customer` calls
  `subscriptions_for_account` per row; fine at this sprint's data volume, a candidate for a
  `GROUP BY`-style batch fetch (the pattern `services/api/serialize.py`'s
  `licence_summary_from_source_aggregates` already uses elsewhere) if the customer list grows.
- **A real invitation/reset flow** for the `User` row `POST /admin/v1/customers` creates from
  `primary_contact_email` (decision 8) — that user has no `password_hash` and cannot sign in until
  one exists.
- **`POST /admin/v1/sources`** (the curated-issuer source-registry row, US-1003 AC1) is explicitly
  another agent's area (`services/api/admin_sources.py`); `add_curated_issuer` here only flips
  `organization.is_curated_issuer`, as the task brief says to.
- **US-401 match recomputation** on approve/link — a different sprint's area (decision 11).

## Verbatim tails

`.venv/bin/python -m pytest tests/test_api_admin_people.py`:

```
............................................................             [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/starlette/testclient.py:37
  /home/user/Bankable/.venv/lib/python3.11/site-packages/starlette/testclient.py:37: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = typing.Callable[[], typing.ContextManager[anyio.abc.BlockingPortal]]

tests/test_api_admin_people.py: 15 warnings
  /home/user/Bankable/tests/test_api_contract.py:43: DeprecationWarning: jsonschema.RefResolver is deprecated as of v4.18.0, in favor of the https://github.com/python-jsonschema/referencing library, which provides more compliant referencing behavior as well as more flexible APIs for customization. A future release will remove RefResolver. Please file a feature request (on referencing) if you are missing an API for the kind of customization you need.
    resolver = jsonschema.validators.RefResolver.from_schema(spec)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
60 passed, 15 warnings in 8.04s
```

`.venv/bin/python -m coverage run --source=services.api.admin_people -m pytest
tests/test_api_admin_people.py -q` then `coverage report -m`:

```
Name                           Stmts   Miss  Cover   Missing
------------------------------------------------------------
services/api/admin_people.py     653     55    92%   204, 212-213, 261, 265-267, 381-382, 411, 413, 480-486, 497, 501, 725, 784, 805, 863, 896, 907, 910, 934, 963, 988, 990, 1005, 1051, 1062, 1103-1113, 1134, 1211, 1217-1225, 1301, 1395, 1418, 1449
------------------------------------------------------------
TOTAL                            653     55    92%
```

(Remaining misses are mostly the "unreachable: require_admin() already refused an unauthenticated
caller" defensive branches, and a couple of rare adapter-failure sub-branches during
`create_lead`.)

`.venv/bin/ruff check services/api/admin_people.py tests/test_api_admin_people.py` and
`.venv/bin/ruff format --check` on both: `All checks passed!` / `2 files already formatted`.

`.venv/bin/mypy --cache-dir /tmp/mypy-adm-ppl services/api/admin_people.py`:

```
Success: no issues found in 1 source file
```

`.venv/bin/python -m pytest tests/test_api_contract.py services/api services/billing/test_router.py
services/crm/test_router.py`:

```
........................................................................ [ 85%]
............                                                             [100%]
=============================== warnings summary ===============================
.venv/lib/python3.11/site-packages/starlette/testclient.py:37
  /home/user/Bankable/.venv/lib/python3.11/site-packages/starlette/testclient.py:37: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = typing.Callable[[], typing.ContextManager[anyio.abc.BlockingPortal]]

tests/test_api_contract.py: 26 warnings
services/billing/test_router.py: 1 warning
  /home/user/Bankable/tests/test_api_contract.py:43: DeprecationWarning: jsonschema.RefResolver is deprecated as of v4.18.0, in favor of the https://github.com/python-jsonschema/referencing library, which provides more compliant referencing behavior as well as more flexible APIs for customization. A future release will remove RefResolver. Please file a feature request (on referencing) if you are missing an API for the kind of customization you need.
    resolver = jsonschema.validators.RefResolver.from_schema(spec)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
84 passed, 28 warnings in 9.28s
```

Nothing in the wider suite (existing `services/api`, `services/billing`, `services/crm` tests, or
the contract test) broke — 84 passed, 0 failed, before my own 60 are added on top.

## Erasure (2026-09-19; docs/50-audit-2026-09-18.md §3.1)

The audit found three defects in the deletion flow and this revision fixes them in
`_complete_deletion_task` / `admin_delete_user`:

1. **Identifiers in the append-only log.** `before` used to hold the email and name in the clear.
   Now `before = {"email_hash", "name_hash", "status"}` via `services.api.audit.hash_identifier`
   (SHA-256 over `AUDIT_HASH_PEPPER`; dev fallback pepper is low-entropy and refused when
   `ENVIRONMENT=production` (alias `APP_ENV`); tests set an obviously fake pepper). `after` names outcomes, never values.
2. **Anonymised, not flagged.** `DELETE /users/{id}` still opens the 30-day task and disables the
   user, but now also pauses the user's saved searches and writes the address to the `suppression`
   table (reason `erasure`) immediately. Completion sets `email` to the tombstone
   `erased-<hash16>@erased.invalid` (RFC 2606 domain; derived from the peppered hash, so "was this
   one of ours?" stays answerable without the address), nulls `name`/`password_hash`, clears
   `marketing_consent`, revokes sessions and keys, strips `email` from and pauses every saved search,
   and nulls `task.contact`. A downstream test asserting `user.email is None` after redaction should
   assert `user.email.endswith("@erased.invalid")` instead.
3. **Subscriptions.** For a `personal` account the erased user's active subscriptions are cancelled
   through the billing port's `cancel_subscription(ref=...)` when the adapter provides it;
   `services.sor.ports.BillingPort` does not declare that operation yet, so with today's adapters
   the audit `after` records `billing: cancellation_pending` plus the refs for the operator, never a
   silent skip. Organisation accounts are untouched (the subscription is the organisation's).
