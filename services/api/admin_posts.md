# `services/api/admin_posts.py` — social review queue, channel switches, admin keys, public intake/reports

Sprint 3 item 3. Implements, to `api/openapi.yaml`'s schemas exactly (the coordinator has since
flipped `adminListPosts`/`adminGetPost`/`adminUpdatePost`/`adminApprovePost`/`adminRejectPost`/
`adminSchedulePost`/`adminSetChannelAutoPublish` to `x-status: live` in the committed spec;
`GET /admin/v1/channels` below is new this pass and ships only in `api/fragments/channels.yaml`
until the coordinator merges it):

- `GET/PATCH /admin/v1/posts[/{post_id}]`, `POST .../approve|reject|schedule` — the social review
  queue (US-801–804).
- `GET /admin/v1/channels`, `PUT /admin/v1/channels/{channel}/auto-publish` — read and (owner-only)
  toggle the per-channel auto-publish switch.
- `GET/POST /admin/v1/keys`, `DELETE /admin/v1/keys/{key_id}` — admin-issued API keys (US-701).
- `POST /v1/intake/proposals`, `POST /v1/intake/opportunities`, `POST /v1/reports` — public,
  unauthenticated write endpoints that feed the task queue (US-1001, US-1003, US-204).

Every write requires a non-empty `reason` where the spec's request schema carries one field for
it, and lands a `record_audit_event` call with `before`/`after`. `services/social` publishers are
untouched — nothing in this module calls a social platform; these routes only change rows in the
`post`, `channel_config`, `api_key` and `task` tables.

## Decisions

1. **Vocabulary source of truth.** `POST_CHANNELS` and `POST_STATES` are imported from
   `services.db.models` — the module whose `CHECK` constraints they mirror. `REJECT_REASONS` is
   the one constant imported from `services.social.models`; that module's dataclasses and
   file-queue code (`PostDraft`, `Post` dataclass, `ReviewMetadata`, `ReviewQueue`, ...) are never
   pulled in, per the task brief's "constants only" instruction. `PROPOSAL_KINDS`,
   `OPPORTUNITY_KINDS`, `LIFECYCLE_STATES` (the last mirrors `services.db.models.LIFECYCLE_STATES`
   exactly, redeclared locally to avoid importing a name already imported for something else) and
   `REPORT_ISSUE_TYPES` are declared locally because `api/openapi.yaml` defines these enums but no
   Python module exports them as tuples — `services/db/models.py` has no `CHECK` constraint on
   `Proposal.kind`/`Opportunity.kind` (an existing gap in that file, not introduced here).

2. **`POST /admin/v1/posts/{post_id}/reject`'s body is `ReasonRequest`** (per the spec — one
   `reason` string), not a separate `reject_reason` field, even though the endpoint's own
   description says "`reject_reason` is mandatory". Resolution: the `reason` string *is* the
   reject-reason code — its value is required to be one of `REJECT_REASONS`
   (`wrong_fact | not_newsworthy | source_doubt | style | duplicate | other`) and is stored
   verbatim on both `Post.reject_reason` and the audit event's `reason` column. This satisfies the
   task brief's "reject (ReasonRequest; reject_reason required)" literally: one field serving both
   roles, matching what the schema actually declares.

3. **`POST /admin/v1/posts/{post_id}/approve` has no request body in the spec** — no field exists
   to carry a caller-supplied `reason`. The audit event for an approval therefore records a fixed
   system string (`_APPROVE_REASON = "Approved via the social review queue (US-802)."`). Every
   *other* write in this module whose schema has a `reason` property enforces it as required
   (`400 validation_error` if missing/empty), per the task brief's "every admin write requires
   `reason`" rule — approve is the one write where the spec itself gives no place to put one.

4. **`ChannelAutoPublishRequest` carries no disclosure-label text**, only the
   `disclosure_label_confirmed` boolean gate. There is nowhere in the request schema for the
   owner to supply the actual label copy a platform requires (CLAUDE.md: "Automated social
   accounts are labelled as automated where the platform requires it"). Enabling a channel with
   confirmation stores a fixed placeholder (`_DEFAULT_DISCLOSURE_LABEL = "Automated post — see
   disclosure"`); disabling clears it to `null`. The real per-platform label text is a follow-up
   (see Deferred).

5. **`channel_config` has no surrogate UUID primary key** (its PK is the channel name, a `Text`
   column) but `record_audit_event`'s `subject_id` parameter is a UUID (`event.subject_id` is
   `GUID` in `services/db/models.py`). Audit events for a channel switch use
   `uuid.uuid5(uuid.NAMESPACE_DNS, f"channel_config:{channel}")` — deterministic per channel name,
   so every audit event for e.g. `bluesky` shares the same synthetic `subject_id` and a caller can
   still find "every change to this channel" via `event.subject_id`.

6. **Public write endpoints create a `Task` only — never a `Proposal`/`Opportunity` row.**
   `docs/10-prd-mvp.md` US-1001 AC2 says "submission creates a proposal with ... publish_state
   pending_review"; the task brief given for this file instead says each submission "creates a
   Task (`intake_proposal` / `intake_opportunity` with `pending_record` = the validated request
   body ...)". `services/db/models.py`'s own `Task` docstring resolves the tension: *"`pending_record`
   holds the submission until approved"* — i.e. turning the task into a real `Proposal`/
   `Opportunity` row is `adminApproveIntake`'s job (a different operation, in a different agent's
   file, out of scope here per the "one agent per file area" rule). `Task.pending_record` in this
   module holds the intake request body as submitted (validated, not yet normalised into the
   `AdminProposal`/`AdminOpportunity` shape `api/openapi.yaml`'s `Task.pending_record` schema
   ultimately declares) — the intake-review step that reads `pending_record` is expected to do that
   normalisation. No test in `tests/test_api_admin_posts.py` exercises `GET /admin/v1/tasks` (not
   in this module), so this does not conflict with the contract test.

7. **Captcha is accepted, not verified.** `captcha_token` is a required, non-empty opaque string
   per the schema; no captcha provider is wired up this sprint (none is named anywhere in
   `docs/00-PLAN.md` or `docs/23`). A `website` honeypot field (any truthy value → `400
   validation_error`) and a shared `intake:<ip>` bucket on `services.api.ratelimit.default_limiter`
   (5 requests/hour, the `docs/23` §7 P-5 / US-204 figure, shared across all three public write
   endpoints) stand in for the abuse controls the spec otherwise assigns to captcha. Recorded as a
   follow-up below, not a silent gap.

8. **Channel body-length checks count Python string length** (Unicode code points via `len()`),
   not extended grapheme clusters, for the `docs/32` §4.3 item 1 limits (bluesky 300, x 280,
   linkedin 3000). No grapheme-clustering library (`regex`, `grapheme`, ...) is in
   `requirements.txt`; adding one is out of scope for a single admin-edit length check. A body
   using multi-codepoint emoji or combining characters could therefore pass this check while
   exceeding the platform's actual grapheme-based limit at publish time — the publisher (out of
   scope, dry-run only) is the last line of defence there regardless.

9. **`GET /admin/v1/posts` and `GET /admin/v1/keys` use `tier="admin"` unconditionally** in
   `meta`, not `ctx.entitlement` — every route in this module requires `require_admin()`, so the
   caller's tier is always effectively admin; this mirrors `services/api/pro.py`'s
   `admin_set_account_entitlement`, which does the same.

10. **`admin_list_keys` and `admin_list_posts` pick their own default sort order** since the spec
    states an order for posts ("oldest draft first") but not for keys. Posts: `created_at` **ascending**
    (spec's explicit rule). Keys: `created_at` **descending** (most recent first) — a reasonable
    admin-UI default; no spec text prescribes otherwise.

11. **`AdminApiKeyCreate.rate_limit_per_hour` is only set on the new `ApiKey` row when the caller
    supplies it.** `ApiKey.rate_limit_per_hour` is `NOT NULL` with a model-level Python default of
    `6000`; passing `rate_limit_per_hour=None` explicitly to the SQLAlchemy constructor would count
    as "the caller set it" and skip the column default, inserting `NULL` and failing the
    constraint. The key is only included in the constructor kwargs when present in the request
    body, so the model's own default applies otherwise.

12. **Coordinator follow-up: `GET /admin/v1/channels`.** The admin UI needs to read auto-publish
    state before an owner toggles it; the spec had only the `PUT`. Added `GET /admin/v1/channels`
    (`operationId adminListChannels`, `security: AdminSession[operator, owner]` — any admin session
    may *read*; only `owner` may still *write*, unchanged on the `PUT`) returning one row per
    `POST_CHANNELS` entry: the stored `channel_config` row if one exists, else the all-defaults
    state (`auto_publish: false`, `disclosure_label`/`daily_cap`/`changed_by_user_id`/`event_id`/
    `updated_at` all `null`, `review_required: true`). `serialize_channel_config` is now shared by
    both operations (refactored from the PUT-only helper it used to be, same output shape) so the
    two can never drift apart — verified directly in
    `test_admin_list_channels_shows_the_stored_row_after_a_put`, which asserts the `GET` row for a
    just-`PUT` channel equals the `PUT` response field-for-field. `event_id` is resolved by finding
    the latest `admin_edit` audit event whose `subject_id` is that channel's deterministic `uuid5`
    (decision 5) — there is no `event_id` column on `channel_config` itself, so a never-configured
    channel's `event_id` and `changed_by_user_id` are `null` rather than a real value.

    `api/openapi.yaml` cannot be edited directly per this task's rules, so the new operation and
    its two schemas (`ChannelConfig` — the shared per-channel object shape, extracted as a named
    schema since `ChannelConfigResponse.data` was inline — and `ChannelConfigListResponse`, a
    `ListEnvelope` of it) live in **`api/fragments/channels.yaml`** for the coordinator to merge,
    with a merge note asking them to also refactor `ChannelConfigResponse.data` to `$ref` the new
    `ChannelConfig` schema so the `PUT`'s contract test keeps validating against the same
    definition `GET` uses (no behavioural change: `serialize_channel_config` already emits this
    exact shape for the `PUT`, including the new `updated_at` field, which is a backward-compatible
    addition since the current inline schema has no `additionalProperties: false`).

## Deferred (follow-ups, not silent gaps)

- **Captcha provider.** No provider (hCaptcha, Turnstile, reCAPTCHA, ...) is integrated; `captcha_token`
  is accepted and stored implicitly (inside `pending_record`/never separately) but never checked
  against a service. Honeypot + rate limit are the interim abuse controls (decision 7).
- **The worker that drafts posts into the `post` table** (`docs/32` §4: `publisher/draft.py` →
  review queue) does not exist yet — this module only ever reads/edits/transitions rows that some
  other process (or, in tests, a factory) has already inserted. Nothing here ever calls
  `publisher/adapters/*` or any social platform.
- **True grapheme-cluster counting** for channel body-length limits (decision 8).
- **Per-platform disclosure-label copy** — only a fixed placeholder string exists; the owner has
  not supplied the actual label text each platform's disclosure policy requires (decision 4).
- **Full `Idempotency-Key` replay semantics for admin write endpoints** (approve/reject/schedule/
  PATCH/channel-switch/key create&revoke). The spec lists the header as a parameter on all of
  these, but no other module in this codebase implements replay-with-409-on-mismatch for admin
  writes yet (`services/api/pro.py`'s `POST /v1/keys` and `PUT .../entitlement` don't either); this
  module follows that precedent and implements the header only where the task brief explicitly
  asked for it (the three public intake/report endpoints).
- **`_resolve_reportable_subject` only resolves `prop_`/`opp_`/`org_` prefixes** for
  `POST /v1/reports`. `AnyPublicIdValue` also allows `mat_`/`evt_`/`doc_`; a report against a match,
  event or document 404s today. US-204's own text ("flag a wrong merge or stale status") is about
  proposals/opportunities (and, for "wrong sponsor", organizations); widening this is a small,
  isolated follow-up if the product ever wants it.
- **`sponsor_org_id`/`issuer_org_id` on intake requests are accepted but not resolved/validated**
  against an existing `Organization` row — the spec gives intake no `404` response, and resolving
  them is the intake-review step's job once `pending_record` is normalised (decision 6).

## Verbatim tails

`services/api/admin_posts.py` (last lines):

```python
    task = Task(
        public_id="",
        type="report",
        status="open",
        subject_type=subject_type,
        subject_id=subject_id,
        issue_type=issue_type,
        description=description,
        contact=contact,
        idempotency_key=idempotency_key,
    )
    db.add(task)
    db.flush()
    task.public_id = public_id("task", task.id)
    db.flush()
    return _report_accepted_response(task)


__all__ = ["router"]
```

`tests/test_api_admin_posts.py` (last lines, unchanged by this follow-up — the new
`test_admin_list_channels_*` tests were inserted earlier in the file, under the existing
`# channel switches` section):

```python
def test_create_report_replays_on_same_idempotency_key(client, db):
    _proposal, _event = _seed_event(db)
    db.commit()
    payload = {
        "public_id": _proposal.public_id,
        "issue_type": "wrong_sponsor",
        "description": "Sponsor is listed incorrectly.",
    }
    first = client.post("/v1/reports", json=payload, headers={"Idempotency-Key": "idem-report-1"})
    second = client.post("/v1/reports", json=payload, headers={"Idempotency-Key": "idem-report-1"})
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    assert db.query(Task).filter_by(type="report").count() == 1
```

`api/fragments/channels.yaml` (last lines):

```yaml
    ChannelConfigListResponse:
      allOf:
        - $ref: '#/components/schemas/ListEnvelope'
        - type: object
          properties:
            data:
              type: array
              items:
                $ref: '#/components/schemas/ChannelConfig'
```

## Test run

```
.venv/bin/python -m pytest tests/test_api_admin_posts.py
75 passed
```

```
.venv/bin/python -m coverage run --source=services.api.admin_posts -m pytest tests/test_api_admin_posts.py -q
.venv/bin/python -m coverage report -m
Name                          Stmts   Miss  Cover   Missing
-----------------------------------------------------------
services/api/admin_posts.py     476      0   100%
-----------------------------------------------------------
TOTAL                           476      0   100%
```

```
.venv/bin/python -m pytest tests/test_api_contract.py services/api tests/test_api_pro_keys.py tests/test_api_admin_posts.py
133 passed
```

`ruff check`, `ruff format --check` and
`.venv/bin/mypy --cache-dir /tmp/mypy-adm-posts services/api/admin_posts.py` all pass clean on
both `services/api/admin_posts.py` and `tests/test_api_admin_posts.py`.

## Not done / out of scope

- Router is **not mounted** on `services/api/app.py` — per the task brief, the coordinator does
  that (one `include_router` call, same pattern as `pro_router`/`auth_router`/`crm_router`/
  `billing_router`). `tests/test_api_admin_posts.py` mounts it onto the shared `app` object itself
  at import time (guarded against double-mounting) purely so its own `TestClient` calls have
  something to hit; this does not touch `app.py`.
- `GET /admin/v1/channels` is **not in the committed `api/openapi.yaml`** — it exists only in
  `api/fragments/channels.yaml` for the coordinator to merge (this follow-up's hard rule: do not
  edit `api/openapi.yaml`). `tests/test_api_admin_posts.py`'s `assert_valid_fragment_schema` helper
  validates its response against a copy of the committed spec with the fragment's two new schemas
  merged in, exactly the pattern `assert_valid` uses for everything already committed.
- No changes to `services/db/models.py`, migrations, `services/social/*`, `api/openapi.yaml`,
  `app.py`, `pro.py`, or any conftest file — confirmed by `git status`-equivalent review of what
  this task touched (only the four paths named in the original "Write ONLY" rule, plus
  `api/fragments/channels.yaml` this follow-up explicitly added to that list).
