# `services/api/admin_records.py` — admin record editing, publish/unpublish, merge/unmerge, resolution and extraction review

Sprint 3 item 3 (`docs/00-PLAN.md` "Sprint 3 kickoff" item 3). Implements the thirteen `/admin/v1`
operations named in the task brief, to the schemas already committed in `api/openapi.yaml`
(`x-status: planned`; the coordinator flips them). Mounted onto `services.api.app.app` with one
`app.include_router(admin_records.router)` call — this module never imports or edits `app.py`.

## What

| Method | Path | Notes |
|---|---|---|
| GET/PATCH | `/admin/v1/proposals/{public_id}` | Full row regardless of `publish_state`; every source and event; `admin_edit` audit event, `overrides` tracking, `clear_overrides` |
| GET/PATCH | `/admin/v1/opportunities/{public_id}` | Same pattern, demand side |
| PATCH | `/admin/v1/organizations/{public_id}` | Same audit pattern; no `overrides`/`publish_state` — see decision 5 |
| PUT | `/admin/v1/records/{record_type}/{public_id}/publish-state` | `proposals`/`opportunities` only (decision 5); `422 gate_unmet` on a restricted/unknown `min_reuse_class`; `takedown` nulls event visibility (decision 6) |
| POST | `/admin/v1/proposals/{public_id}/merge` | Preview (signed, 15-minute token) then apply; calls `services.resolve.merge.merge_proposal` |
| POST | `/admin/v1/proposals/{public_id}/unmerge` | Calls `services.resolve.merge.unmerge_proposal`, exact restore |
| GET | `/admin/v1/resolution-candidates` | `ResolutionDecision` rows, paginated by score |
| POST | `/admin/v1/resolution-candidates/{candidate_id}/decide` | `same`/`different`/`defer` (the real enum — see decision 10) |
| GET | `/admin/v1/extractions` | `Extraction` rows, paginated by ascending confidence |
| POST | `/admin/v1/extractions/{extraction_id}/accept` | Applies `payload[field_path]` to the subject; `422 gate_unmet` on a licence that withholds derived publication |
| POST | `/admin/v1/extractions/{extraction_id}/reject` | Leaves the subject untouched |

## Numbered decisions

1. **`services.ids.public_id` imported as `make_public_id`.** Several handlers below take a path
   parameter literally named `public_id` (matching `api/openapi.yaml`'s path template: the merge,
   unmerge, publish-state and get/patch routes all have `{public_id}`) and also need to encode a
   *different* row's id (an event, a candidate) inside the same function body. `services/api/
   app.py`'s handlers with a `public_id` parameter never also call the encoder inside themselves,
   so this alias is this module's own necessity, not a departure from that codebase's convention.

2. **`rc_`/`evt_` ids are decoded, not looked up by column.** `ResolutionDecision`
   (`services/resolve/models.py`) and `Event` have no `public_id` column (same situation `Match`
   is in). `_decode_crockford(prefix, value)` reverses the Crockford walk exactly the way
   `services/api/app.py::_find_event_by_public_id` and `services/crm/router.py::
   _find_match_by_public_id` do — both read-only files named by the task specifically for this
   pattern. `Extraction` does carry its own stored `public_id` column, so its lookups are a plain
   `WHERE public_id = :value`.

3. **Merge preview/apply has no server-side "preview session" row.** The preview token is a
   self-contained, HMAC-signed (`SESSION_SECRET`, same source and dev-only fallback literal as
   `services/api/auth.py::_serializer`), 15-minute-lived credential over `(surviving_public_id,
   absorbed_public_id, field_choices, both rows' updated_at)`. On apply: a missing, malformed,
   badly-signed or expired token, or one whose `field_choices` no longer match the request, is
   `400 validation_error` (`"invalid"`); a valid signature over field choices and ids but either
   row's `updated_at` moved since is `409 conflict` (`"stale"`) — reusing the spec's own "stale
   merge preview" `Conflict` example verbatim. `MergeResponse.preview_token`'s spec text ("valid
   15 minutes") is honoured by embedding and checking a plaintext timestamp inside the signed
   payload, not by any external state.

4. **`merge_proposal`/`unmerge_proposal` already write the audit-shaped event; this module does
   not call `record_audit_event` a second time for the same action.** Both functions
   (`services/resolve/merge.py`) write one `merged`/`unmerged` event with `before`/`after`, a
   `reason` and `actor_type` per docs/21 §6.3. A second call to `record_audit_event` would create
   a second, indistinguishable `merged`/`unmerged` event on the same subject's timeline — breaking
   docs/21 §6.1's "one code path, one predicate" reuse of the event table and US-202 AC1's
   newest-first, one-row-per-change timeline (a human clicking "merge" once should not produce two
   merge events). Instead, this module sets `actor_user_id` directly on the event object those
   functions already flushed (a plain attribute assignment inside the same request's transaction,
   flushed again before the response is built) — the "reason" and "before"/"after" the audit rule
   asks for are the ones `merge_proposal`/`unmerge_proposal` already wrote. The resolution-
   candidate "confirm" path (`decision=same`, which also calls `merge_proposal`) does the same.
   Its "reject" path (`decision=different`) has no proposal-level field change to attach an event
   to, so it calls `record_audit_event` once, with `subject_type="proposal"` (the pair's left
   member) and `event_type="admin_edit"` — `resolution_decision` is not itself in docs/21 §3.10's
   `subject_type` vocabulary (`proposal | opportunity | organization | match | document | source |
   post | user | account | api_key`), and `admin_edit` is the closest existing event type for "a
   human recorded a decision, with a reason, and nothing else moved."

5. **`organization` has no `publish_state` in `services/db/models.py`.** Only `proposal` and
   `opportunity` carry `publish_state`/`published_at`/`public_at`/`min_reuse_class`; no visibility
   predicate anywhere in the codebase reads a publish state for `organization`. This task's
   writable paths cannot add a column or a migration. `PUT .../publish-state` with
   `record_type=organizations` therefore answers `400 validation_error` naming the gap explicitly,
   rather than accepting the request and silently doing nothing (a fabricated 200 would be a worse
   lie than an honest 400). `docs/00-PLAN.md`'s decisions log is the right place to record this as
   an open item for whichever sprint adds organisation-level publication; not done here since it is
   out of this task's three writable paths.

6. **Takedown nulls every event's visibility, including the one just written.** `docs/21` has no
   separate "published" flag on `event` (docs/21 §3.10's own `published_at`/`public_at` pair is the
   mechanism, per this task's own instruction: "if events have no publish flag, set `public_at` to
   null"). `takedown: true` therefore nulls `published_at` and `public_at` on every event whose
   `subject_type`/`subject_id` match the record, run *after* the `published`/`unpublished` audit
   event is flushed so it is included — a takedown notice that stayed publicly visible on the
   record it concerns would itself be the leak `docs/21` §8's checklist exists to prevent.

7. **Admin proposal/opportunity detail always includes `sources` and `events`, and never withholds
   `raw`/`source_record_id` behind a source's licence.** `adminGetProposal`'s own spec description
   says "the full row regardless of `publish_state`, with `overrides`, `field_provenance`, gated
   sources and raw present. Admin only" and `docs/20` §8 item 2 lists "view provenance and event
   history" as a base admin capability, not something behind an `include=` query parameter (which
   the operation's parameter list does not even offer). `_admin_provenance_row`/`_admin_source_row`
   are therefore deliberately not `services.api.serialize.provenance_row` (which the public/Pro
   tiers correctly still use) — they never hide `source_record_id` regardless of
   `licence.allows_raw_publication`.

8. **Extraction `accept` applies only scalar canonical fields.** `field_path` must be a member of
   a fixed per-`subject_type` set — a curated subset of `AdminProposalUpdate`/
   `AdminOpportunityUpdate`/`AdminOrganizationUpdate`'s own properties, deliberately excluding
   organisation/issuer references and the nested `location` object. One extraction proposes one
   field from one document (docs/21 §3.9); applying an org reference or a structured location from
   a single extracted value is a different, unbuilt shape of review (which record does the
   reference resolve against? by what key?) and is left as a follow-up rather than guessed at.
   Anything outside the allowed set, or a `field_path` the payload does not actually carry a value
   for, is `409 conflict` per the task's own rule.

9. **Manually setting a proposal's `location` still needs a real, NOT NULL provenance quartet**
   (docs/21 §3.7). A brand-new `Location` row is attributed to the proposal's most recently
   retrieved active source (`source_id`/`source_url`/`retrieved_at`/`licence_id` copied from that
   `ProposalSource`), with `geocoder="manual"` marking it as hand-entered rather than that source's
   own geocoding, and `precision_reason="geocoder"` — the *only* value in `api/openapi.yaml`'s
   fixed `precision_reason` enum (`licence | source | geocoder | null`) that fits "an admin set
   this place by hand" (there is no `manual` option in that enum). A proposal with no active source
   at all cannot be given a manual location this way; that edge case is `400 validation_error`
   rather than inventing a placeholder source.

10. **`ResolutionDecisionRequest.decision`'s real enum is `same | different | defer`**, not
    `confirm | reject` as an earlier plain-English description of this task suggested — the task
    itself said to "read the enum," and `api/openapi.yaml` line ~9591 is authoritative. `same` maps
    to `ResolutionDecision.status = "confirmed"` and calls `merge_proposal`; `different` maps to
    `status = "rejected"`; `defer` is implemented as a genuine no-op (no write, no audit event,
    `status` stays `"proposed"` — the API-level `status` reported is `"pending"`) since the model
    has no third persisted state for "looked at but not decided," and inventing one was out of
    scope for a table this task may not alter (`services/resolve/models.py` is read-only).
    `ResolutionCandidate.status` (`pending | decided`) is a small derived view over
    `ResolutionDecision.status` (`proposed | confirmed | rejected`) computed at serialization time,
    never stored twice.

11. **`ResolutionCandidate.decided_by_user_id` is encoded the same way `Extraction.
    accepted_by_user_id` already is** — `make_public_id("usr", decision.decided_by_user_id)`, no
    database round trip needed (`User.public_id` is itself the Crockford encoding of `User.id`,
    docs/21 §1), rather than a `db.get(User, ...)` join just to read back a value this module can
    already compute from the stored uuid alone.

12. **`ResolutionCandidate.model_alias` is always `null`.** The gate that produces a
    `resolution_decision` row (`services/resolve/merge.py::gate_cluster`) is a deterministic
    threshold-and-guard function, not a model call — there is no alias to report, and reporting one
    would fabricate provenance CLAUDE.md's "no model identifiers" guardrail exists to prevent
    misrepresenting. Left null rather than invented; a later model-adjudication stage (docs/22 §9,
    not built this sprint) would populate it honestly.

## Deferred / not done

- **Organization publish-state** (decision 5): needs a schema change (new columns, a migration)
  outside this task's writable paths.
- **Extraction accept on organisation/issuer references or the nested `location` object**
  (decision 8): out of scope for one single-field extraction row.
- **Organization merge/unmerge endpoints**: `services/resolve/merge.py` already exports
  `merge_organization`/`unmerge_organization`, but no `/admin/v1` path for them exists in
  `api/openapi.yaml` (the operations list this task names is proposal-only), so none was added.
- **Reverse pagination** (`page.prev_cursor`) on the two list endpoints: `services/api/
  pagination.py::paginate` itself only implements forward pagination (its own docstring says so);
  this module does not extend it, matching the rest of the codebase.

## Tests

`tests/test_api_admin_records.py`: 82 tests, all passing; 96% line coverage of
`services/api/admin_records.py` (`coverage run --source=services.api.admin_records -m pytest
tests/test_api_admin_records.py`; 601 statements, 26 missed — the residue is Crockford-decode
error branches for a garbage-prefixed id, a couple of `_jsonable` type branches never exercised by
this suite's fixtures, and the `unmerge_proposal` `ValueError` re-raise path, none reachable
without either malformed input already caught earlier in the same function or a store-level bug
`services/resolve/merge.py`'s own tests already cover). Builds a standalone `FastAPI` app carrying
only `admin_records.router` plus the `ProblemError` handler (mirrors `services/crm/
test_router.py`'s "option B" — this task's own read-only files show three other agents mounting
their own `/admin/v1` routers concurrently, so this suite must not depend on `services.api.app.app`
already carrying this router, nor mutate that shared object itself). Reuses `login`/`make_account`/
`make_user` from `tests/conftest.py` and the entity factories from `services/api/conftest.py`
directly (both plain functions, safe to reuse against this file's own `client`/`db` fixtures);
`assert_valid` is imported directly from `tests/test_api_contract.py` per the task's read list, and
`spec` is a small local fixture that reloads the same `api/openapi.yaml` file rather than importing
that module's own `spec` fixture object, which triggers a spurious ruff `F811` on every test
function that also names its own `spec` parameter.

Covered per the task's list: happy path + `assert_valid` for every operation; 401 (no session) and
403 (non-operator role) on proposal GET as the representative case (the same `require_admin()`
dependency guards every other route, so this is not tested exhaustively — see below); 404 for every
lookup-by-id operation; 400 for missing `reason` and for a bad vocab value, on every write
operation; the audit event's `before`/`after` asserted directly against the DB for the update,
publish-state, extraction-accept and resolution-reject operations; merge preview then apply (the
survivor's `source_count` and the absorbed row's `merged_into_id`/`publish_state` asserted, plus
`field_choices` actually changing the survivor's field); apply without a valid `preview_token` (and
separately, a stale one, and a garbage one) all `-> 400`/`409` as specified; unmerge restoring the
absorbed row exactly (via the same merge/apply/unmerge round trip); resolution `same` merging and
`different` not (and `defer` changing nothing); extraction accept changing the field and reject
leaving it; and takedown nulling every event's `published_at`/`public_at`.

Not exhaustively duplicated across all thirteen operations (would multiply the file several times
for the same `require_admin()`/`not_found()`/`validation_error()` code paths already proven once):
the 401/403 pair is asserted on `GET /admin/v1/proposals/{public_id}` and
`GET /admin/v1/resolution-candidates`/`GET /admin/v1/extractions` (three different dependency call
sites, enough to prove the pattern generalises) rather than on all thirteen routes individually.

## Verbatim tails

`services/api/admin_records.py` (1438 lines):
```python
    record_audit_event(
        db,
        subject_type=extraction.subject_type,
        subject_id=extraction.subject_id,
        event_type="admin_edit",
        actor=ctx.user,  # type: ignore[arg-type]
        reason=reason,
        before={"extraction_status": "proposed", "extraction_id": extraction.public_id},
        after={"extraction_status": "rejected", "extraction_id": extraction.public_id},
    )
    extraction.status = "rejected"
    db.flush()

    return build_envelope(
        _serialize_extraction(db, extraction), meta=_admin_meta(), licence_summary=_empty_licence_summary()
    )


__all__ = ["router"]
```

`tests/test_api_admin_records.py` (1560 lines):
```python
    # merge `left` into `third` first, so `left` is already absorbed elsewhere
    preview = client.post(
        f"/admin/v1/proposals/{third.public_id}/merge",
        json={"absorb_public_id": left.public_id, "preview": True},
    ).json()
    client.post(
        f"/admin/v1/proposals/{third.public_id}/merge",
        json={
            "absorb_public_id": left.public_id,
            "preview": False,
            "preview_token": preview["data"]["preview_token"],
            "reason": "prior unrelated merge",
        },
    )

    resp = client.post(
        f"/admin/v1/resolution-candidates/{public_id('rc', decision.id)}/decide",
        json={"decision": "same", "reason": "x"},
    )
    assert resp.status_code == 409
```

## Checks run

- `.venv/bin/python -m pytest tests/test_api_admin_records.py` — 82 passed.
- `.venv/bin/ruff check services/api/admin_records.py tests/test_api_admin_records.py` — clean.
- `.venv/bin/ruff format --check services/api/admin_records.py tests/test_api_admin_records.py` —
  clean.
- `.venv/bin/mypy --cache-dir /tmp/mypy-adm-rec services/api/admin_records.py` — clean, strict.
- `.venv/bin/python -m pytest tests/test_api_contract.py tests/test_resolve_store.py
  tests/test_resolve_store_unmerge.py services/api tests/test_api_admin_records.py` — 151 passed,
  nothing broken by this module's addition (it is not imported by any of those files; the router
  is not mounted onto `services.api.app.app` yet — that is the coordinator's step).
