# Admin panel

Other agents append their own sections below this one (Tasks/Posts/People/Ops); this file starts
with only the section this task owns.

## Sources and records

Server-rendered screens for Sources, Records, Resolution and Extractions (Sprint 3 item 3;
`docs/30-design-ia.md` §1.3, §4.6, §5.5, §6) over the live `/admin/v1` API, built on the shared
shell in `web/admin/shell.py` (`require_operator`, `AdminApi`, `render`, `problem_notice`,
`require_same_origin`). No JavaScript; every filter lives in the URL query string; every write
form carries a `reason` and follows the shell's POST contract (`require_same_origin` first, then
the API call, then a 303 redirect with `?flash=` on success or the page re-rendered with
`problem_notice(result)` and the submitted values at the API's own status code on a problem).

### Screens

| Route | Purpose |
|---|---|
| `GET /admin/sources` | Health table (docs/30 §5.5): status glyph, name, last run, rows Δ, events, $/rec, publish state with `GATED`/`paused` markers; filters `health`, `publish_state`, `implemented`; "+ add issuer" link |
| `GET/POST /admin/sources/new` | Curated issuer form (`AdminSourceCreate`) |
| `GET /admin/sources/{source_id}` | Every admin field, licence block, last-10-runs table, action forms |
| `POST /admin/sources/{id}/run` | Run now |
| `POST /admin/sources/{id}/pause`, `/resume` | Pause/resume via `PATCH .../sources/{id}` |
| `POST /admin/sources/{id}/edit` | Cadence/lag edit |
| `POST /admin/sources/{id}/publish-state` | Publish-state select + reason; renders `422 gate_unmet` prominently |
| `POST /admin/sources/{id}/gate` | Licence gate clearance — rendered disabled with a note for every role but `legal` (D3 below); the API itself is the real enforcement point |
| `GET /admin/source-runs`, `/admin/source-runs/{run_id}` | Run history and detail, DQ report as a definition list, link to the snapshot when present |
| `GET /admin/snapshots/{snapshot_id}` | Snapshot metadata (linked from a run's detail page) |
| `GET /admin/records` | Public-id lookup form (`prop_`/`opp_`/`org_`) |
| `GET /admin/records/proposals/{public_id}`, `/opportunities/{public_id}` | Every field, sources table with licence class, event history, edit form, publish-state form, merge (preview then apply), unmerge |
| `GET /admin/records/organizations/{public_id}` | Minimal edit (name, website, reason) — no publish-state form (decision 5 below) |
| `GET /admin/resolution` | Candidate pairs, filter `status`, decide forms (`same`/`different`/`defer`) |
| `GET /admin/extractions` | Extraction queue, filters `status`/`subject_type`, accept/reject forms |

### Decisions

1. **Filters are single-valued `<select>`s in the query string**, not multi-select checkbox
   groups. The API's `health`/`publish_state`/`implemented`/`status`/`subject_type` parameters
   accept comma-separated OR lists, but one admin session narrowing "show me the failing ones"
   almost always wants one value, and a JS-free multi-select does not render usefully at 400px.
   Every filter still round-trips through the URL exactly as required.
2. **"Add issuer" takes `licence_id` as a plain text field, not a dropdown.** `GET /v1/licences`
   (`listLicences`) is `x-status: planned` in `api/openapi.yaml` — unimplemented — so a dropdown
   backed by it would silently break the moment this screen shipped. The admin adding a curated
   issuer already knows the licence id from another source's detail page.
3. **The gate-clearance form renders, disabled, for every non-`legal` role** rather than being
   hidden, so an operator can see what evidence a gate needs (docs/30 §4.6). The API's own
   `require_admin(roles=("legal",))` is the real enforcement point regardless of what this page
   renders.
4. **Health icon mapping is 1:1 with `SourceHealth`'s five values** (`ok`→✓ success, `degraded`→⚠
   progress, `failing`/`blocked`→✗ danger, `paused`→⏸ neutral), not the three-icon wireframe
   literally — that sketch predates `blocked`/`paused` joining the enum. Icon and text label are
   both always rendered (docs/31 §7 SC 1.4.1, "never colour alone").
5. **The "GATED" chip is derived, not read off a single API field.** `AdminSource` embeds the
   licence's `reuse_class`/`gate_flag` but has no boolean `gated` field; `_is_gated` in
   `web/admin/sources.py` computes it the same way `services/api/admin_sources.py::
   _licence_gate_missing` does, so list and detail pages can never disagree.
6. **Vocab tuples (`PROPOSAL_KINDS`, `OPPORTUNITY_KINDS`, `ORGANIZATION_TYPES`) are imported
   directly from `services/api/admin_records.py`** rather than duplicated in the web layer, so a
   `<select>`'s options can never drift from what the API actually validates. This is a read
   import from another agent's file area — this module never edits that file.
7. **Merge is one route with a `step` field** (`preview` then `apply`), not two URLs. With no
   session-storage table in this task's file list, the preview response (conflicts,
   `preview_token`) is rendered directly as the POST's own 200 response, with the token carried
   forward as a hidden field for the second POST — the two-step shape the brief describes.
8. **The opportunity edit form's `technologies` field is one comma-separated text input**, split
   into a list on submit. `AdminOpportunityUpdate.technologies` has no fixed enum, and a
   repeatable-add widget would need JavaScript, which this screen may not use.
9. **Organisation records never show a publish-state form.** `PUT
   /admin/v1/records/{record_type}/{public_id}/publish-state` refuses `record_type=organizations`
   with `400` (no `publish_state` column on `organization` — `services/api/admin_records.py`
   decision 5); a form that always fails would be worse than omitting it.
10. **Resolution `defer` posts to the same `decide` endpoint with `decision=defer`** rather than
    being a client-side no-op, so the API's real behaviour (no state change) is what gets
    exercised, not a shortcut that never reaches it.
11. **The organisation detail screen reads the public `GET /v1/organizations/{public_id}`
    endpoint**, since no `adminGetOrganization` operation exists in `api/openapi.yaml` (only
    `PATCH /admin/v1/organizations/{public_id}` does) — verified live in `services/api/app.py`
    (implemented there despite the spec's stale `x-status: planned` tag on the operation).
12. **A `GET /admin/snapshots/{snapshot_id}` screen was added** (not named in the task's screen
    list, but `adminGetSnapshot` is named in the read list and the source-run screen's own spec
    line asks for "link to the snapshot when present"). It lives under
    `web/templates/admin/sources/` since no separate directory was named for it.

### Deferred / not done

- **Multi-value filters** (comma-separated OR on one query parameter) — the API supports them;
  this UI exposes one value per filter at a time (decision 1).
- **A licence-id picker on "add issuer"** — blocked on `listLicences` going live (decision 2).
- **`AdminProposalUpdate`'s `sponsor_org_id`, `identifiers`, `location` and `clear_overrides`
  fields**, and the equivalent `issuer_org_id`/`identifiers` on opportunities — the brief's field
  list for the edit forms names the scalar canonical fields only; these structured/reference
  fields are left for a later pass.
- **Merge `field_choices` UI only appears when the preview reports a conflict** — with no
  conflicting fields the apply form has no per-field radios to render, by design (nothing to
  choose).
- **Reverse pagination** (`page.prev_cursor`) on every list screen — `services/api/pagination.py`
  itself only implements forward pagination; this UI does not extend it.

### Tests

`web/test_admin_sources.py` (16 tests) and `web/test_admin_records.py` (17 tests) copy the
fixtures from `web/test_admin_shell.py` verbatim and mount both this task's web routers and the
matching `services/api/admin_sources.py` / `services/api/admin_records.py` API routers onto the
shared apps, guarded so re-running either module (or running them together) never double-registers
a route — the coordinator's real mount in `services/api/app.py` / `web/app.py` is a later step
neither suite depends on. Covered: every list renders rows and its named-filter empty state; a
gated source shows `GATED`; the source detail page shows the licence evidence fields; pause/resume
flips the source (re-read through the page); publish-state to `public` on a restricted source
renders `422`/"Licence gate unmet"; the gate form is refused (`403`) for `operator` and succeeds
for `legal`; run-now renders the sandbox's real `503` queue-unavailable notice cleanly (no
traceback) and, with `services.api.admin_sources.get_source_runner` overridden by a fake, the
`202`/redirect success path; records detail renders sources and events; edit changes the name;
merge preview then apply merges (asserted through a direct `/admin/v1` read), then unmerge restores
the absorbed row; resolution `same` merges and the empty state names the filter; extraction accept
changes the subject's field and reject leaves it; every POST without an `Origin` header is refused
`403`; every page returns `200` for an operator and the shell redirects anonymous visitors to
`/login`.

### Verbatim tails

```
$ .venv/bin/python -m pytest web/test_admin_sources.py web/test_admin_records.py
.................................                                        [100%]
33 passed, 104 warnings in 8.02s
```

```
$ .venv/bin/ruff check web/admin web/test_admin_sources.py web/test_admin_records.py
All checks passed!
$ .venv/bin/ruff format --check web/admin web/test_admin_sources.py web/test_admin_records.py
10 files already formatted
```

```
$ .venv/bin/mypy --cache-dir /tmp/mypy-fe-a web/admin
Success: no issues found in 8 source files
```

```
$ .venv/bin/python -m pytest web --ignore=web/test_e2e.py
....F..
1 failed, 94 passed, 227 warnings in 41.47s
```

The one failure, `web/test_admin_people.py::test_customer_detail_shows_stale_banner`, is in
another agent's file area, is unaffected by anything this task touches (`services/api/admin_sources.py`
and `admin_records.py` are not imported by that test), and reproduces identically run alone —
confirmed pre-existing, not introduced here. This run includes the other agents'
`web/test_admin_posts.py`, `web/test_admin_tasks.py`, `web/test_admin_people.py`,
`web/test_admin_shell.py` and `web/test_auth.py` alongside this task's two new files; every other
test passes.

## Tasks and posts

Server-rendered screens for the task queue (reports, intake review, deletion requests) and the
social review queue plus channel switches (Sprint 3 item 3; `docs/30-design-ia.md` §1.3, §4.5,
§4.6; `docs/32-social-operating-playbook.md` §4) over the live `/admin/v1` API, on the same shared
shell (`require_operator`, `AdminApi`, `render`, `problem_notice`, `require_same_origin`). No
JavaScript; every filter lives in the URL query string; every write form carries a `reason` and
follows the shell's POST contract.

### Screens

| Route | Purpose |
|---|---|
| `GET /admin/tasks` | Queue table: type, status chip, subject link, assignee, created, due; filters `type`/`status`/`assignee_user_id`; cursor pager |
| `GET /admin/tasks/{task_id}` | Every field; pending record as a definition list (submission or admin-shaped keys, whichever are present); contact block; audit event ids; update form (status/assignee/notes/reason); intake tasks get an approve/link/reject form; deletion-request tasks get a redaction warning and the CRM's `503` renders through the shared notice |
| `POST /admin/tasks/{id}/update` | `PATCH /admin/v1/tasks/{id}` |
| `POST /admin/tasks/{id}/approve-intake` | `POST .../approve-intake`; on success the record's public id and link are carried through the redirect's query string and rendered as a callout on the task page |
| `GET /admin/posts` | Review queue: channel, state chip, subject, body preview, credit line, `gate_checked_at` age, `scheduled_for`; filters `channel`/`state`; cursor pager |
| `GET /admin/posts/{post_id}` | Full body in a monospace block with the channel's character count/limit, credit line, disclosure label, subject/event links; edit/approve/reject/schedule forms |
| `POST /admin/posts/{id}/edit`, `/approve`, `/reject`, `/schedule` | The matching `/admin/v1/posts/{id}/...` write |
| `GET /admin/posts/channels` | One row per channel with its real auto-publish state (see decision 1), read-only for non-owners |
| `POST /admin/posts/channels/{channel}/auto-publish` | `PUT /admin/v1/channels/{channel}/auto-publish` |

### Decisions

1. **The channels screen reads `GET /admin/v1/channels`.** This task's brief was written before
   that operation existed ("the API has no GET for channel config... say so in a note"); a
   coordinator follow-up landed it (`docs/CHANGELOG.md`, `services/api/admin_posts.py`
   `admin_list_channels`) partway through this work. The screen was built against the live
   endpoint instead of carrying the "unknown state" note the brief anticipated, since rendering
   real state is strictly better and the endpoint uses the same `ChannelConfig` shape the `PUT`
   already returns.
2. **Vocabulary tuples (`TASK_TYPES`, `TASK_STATUSES`, `POST_CHANNELS`, `POST_STATES`,
   `REJECT_REASONS`, `CHANNEL_BODY_LIMITS`) are hardcoded locally**, not imported from
   `services/db/models.py` or `services/api/admin_posts.py`, mirroring
   `services/api/admin_posts.py`'s own decision 1 ("this module only ever talks to the API") — the
   web layer never imports server-side modules.
3. **Status/state chips use five families** (`docs/31` §1.2) with the exact API vocabulary as the
   label: tasks `open`→neutral, `in_progress`→progress, `done`→success, `rejected`→danger; posts
   `draft`/`withdrawn`→neutral, `approved`/`scheduled`→progress, `published`→success,
   `rejected`/`failed`→danger. No per-value hue — several values share a family, distinguished by
   the label text (D-25).
4. **The task update form only sends `assignee_user_id` when the field is non-empty, or `null`
   when an explicit "Unassign" checkbox is ticked** — leaving the field blank means "don't touch
   the assignee", matching `TaskUpdate`'s `minProperties: 1` optional-field semantics rather than
   clobbering an existing assignment on every save.
5. **The approve-intake success redirect carries the created/linked record's public id and URL as
   extra query parameters** (`record_id`, `record_url`, `record_action`) rather than only a flash
   string, so the screen brief's "rendering the `IntakeDecisionResponse`'s record link on success"
   is satisfiable after the required 303-with-`?flash=` redirect (a real link cannot live inside
   the plain-text flash paragraph `_notice.html` renders).
6. **`<input type=datetime-local>` on the schedule form is treated as UTC**, not the browser's
   local zone — the screen brief says "converted to RFC 3339 UTC" and this repo's admin surface has
   no per-operator timezone preference to convert from/to.
7. **Channel body-length limits (`bluesky` 300, `x` 280, `linkedin` 3000) count Python string
   length**, matching `services/api/admin_posts.py`'s own interim measure (its decision 8, no
   grapheme-clustering library in `requirements.txt`) so the count shown here never disagrees with
   what the API enforces.
8. **`gate_checked_at`'s "age" column is computed once per request**, not a live client-side
   clock — this screen area uses no JavaScript, so "N ago" is only ever as fresh as the last page
   load.

### Deferred / not done

- **A user-picker for the assignee filter/field** — both take a raw `usr_...` id in a text input;
  no `GET /admin/v1/users` call was added to back a dropdown (out of this task's screen list).
- **Resolver suggestions** (`Task.resolver_suggestions`) are not rendered on the task detail page —
  the screen brief's field list for task detail names the pending record, contact and audit ids
  only.
- **Post metrics** render when present but nothing here polls for them; that is the (separate,
  out-of-scope) metrics-collection worker's job per `docs/32` §4.9.

### Tests

`web/test_admin_tasks.py` (12 tests) and `web/test_admin_posts.py` (14 tests) copy the fixtures
from `web/test_admin_shell.py` and mount this task's two routers onto `web_app`, guarded against
double-registration. Intake tasks are created through the real public
`POST /v1/intake/proposals` endpoint (a second `TestClient` over the same mounted, dependency-
overridden `api_app`); posts are seeded by inserting `Post` rows against an `Event` from
`services.api.conftest.make_event`, since no draft-generation worker exists yet
(`services/api/admin_posts.md` "Deferred"). Covered: both lists render rows and their
named-filter empty states; task detail shows the pending record and contact; the update form
changes status and rejects a missing reason with `400`; approve-intake creates the record and the
redirect target shows its link; reject records the reason; the deletion-request warning renders
and completing it anonymises the user, while a flaky CRM port (`SorUnavailable`) renders the `503`
and leaves the task open; post detail shows the character count/limit; approve flips state, and
approve on an unpublished subject renders `422 gate_unmet`; reject without a valid reason code is
`400` and with one closes the post; schedule with a past time is `400`, with a future time
succeeds; the channels page is read-only for an operator (and its `PUT` is refused `403`), an
owner's `PUT` without `disclosure_label_confirmed` is `422`, and with it succeeds and the new state
reads back on `GET /admin/v1/channels`; every POST without an `Origin` header is `403`; anonymous
visitors are redirected to `/login`.

### Verbatim tails

```
$ .venv/bin/python -m pytest web/test_admin_tasks.py web/test_admin_posts.py
..........................                                               [100%]
26 passed, 76 warnings in 8.10s
```

```
$ .venv/bin/ruff check web/admin/tasks.py web/admin/posts.py web/test_admin_tasks.py web/test_admin_posts.py
All checks passed!
$ .venv/bin/ruff format --check web/admin/tasks.py web/admin/posts.py web/test_admin_tasks.py web/test_admin_posts.py
4 files already formatted
```

```
$ .venv/bin/mypy --cache-dir /tmp/mypy-fe-b web/admin/tasks.py web/admin/posts.py
Success: no issues found in 2 source files
```

```
$ .venv/bin/python -m pytest web --ignore=web/test_e2e.py
3 failed, 92 passed, 227 warnings in 44.80s
```

The three failures are all in `web/test_admin_people.py` (`test_customer_detail_shows_stale_banner`,
`test_subscription_create_without_billing_ref_is_409`,
`test_subscription_create_with_billing_ref_flips_entitlement`) against `web/admin/people.py` — a
different concurrent agent's file area, untouched by this task. Verified in isolation
(`.venv/bin/python -m pytest web/test_admin_people.py -k test_customer_detail_shows_stale_banner`)
that the failure reproduces with no tasks/posts files involved at all.

## Users, customers, keys, costs, audit

Sprint 3 item 3 (admin panel), my slice: `web/admin/people.py` (Users, Customers with
subscriptions) and `web/admin/ops.py` (Keys, Costs, Audit), each `router = APIRouter()`, mounted by
the coordinator next to `web/admin/shell.py`'s own router. `web/test_admin_people.py` (26 shared
with ops, 15 of them here) and `web/test_admin_ops.py` (11) mount their router onto `web_app`
inside the test module when not already mounted, exactly as the task brief describes. Every route
depends on `require_operator`, reads/writes only through `ctx.api`, and renders through
`web/admin/shell.py`'s `render()`/`problem_notice()` -- no page here talks to the database or
trusts anything the live `/admin/v1` API on `services/api/admin_people.py`/`admin_posts.py`
(keys)/`admin_sources.py` (costs, audit) did not itself answer.

### What

- **Users** (`GET /admin/users`, `GET/PATCH /admin/users/{id}`,
  `GET/POST /admin/users/{id}/delete`): list with role/status/account_id filters and paging; detail
  with two single-purpose forms (change role, change status) instead of one combined form, so each
  keeps its own `reason` unambiguous (docs/31 SC 3.3.1); a delete confirm page explaining the
  deletion-request/disable flow, then a `POST` that redirects to the API's own returned
  `/admin/tasks/{task_id}`.
- **Customers** (`GET /admin/customers`, `GET/POST /admin/customers/new`,
  `GET /admin/customers/{id}`, `POST /admin/customers/{id}/subscriptions`): list with
  account/kind/entitlement/seats/billing-ref-present/CRM-ref-present/stale-marker columns; detail
  with the account block, the `sor` block (a staleness banner in the exact US-902 AC3 wording when
  `stale`), subscriptions/users/keys tables, and a create-subscription form that explains the
  billing-reference prerequisite and renders the API's `409` when it is missing.
- **Keys** (`GET/POST /admin/keys`, `POST /admin/keys/{id}/revoke`): list with
  account_id/status(active|revoked) filters; an issue form that, on `201`, renders a one-time
  secret page directly rather than the usual `303` redirect (the secret is never stored anywhere to
  redirect *to*) -- the one deliberate exception to the shell's normal write pattern, called out in
  `web/admin/ops.py`'s module docstring decision 1.
- **Costs** (`GET /admin/costs`): filters forward the API's own parameter names verbatim
  (`day[from]`, `day[to]`, `source_id`, `purpose`, `group_by`); a totals row is summed in
  `web/admin/ops.py` from the rows the page already has (task brief: "computed server-side in the
  page module"), not a pass-through of the API's own `totals` field -- `cache_hit_rate` in that
  totals row is a `model_calls`-weighted average, an approximation documented in the module's
  decision 5 since a `CostRow` carries only the ratio, not the underlying hit/attempt counts. Empty
  state says plainly that an empty cost log is the expected state today (US-909).
- **Audit** (`GET /admin/audit`, `GET /admin/audit/{event_id}`): list with
  subject_type/event_type/actor_user_id/since filters and paging; a detail page rendering
  `before`/`after` as two side-by-side definition lists (`admin-cols`). **`GET /admin/v1/audit` has
  no single-item counterpart** -- `api/openapi.yaml` never grew an `adminGetAuditEvent` operation --
  so the detail route pages through the list (narrowed by `subject_type` when the list's own link
  supplies one, capped at 10 pages of 200) scanning for a matching `id`. Narrowing by `subject_id`
  too was tried and reverted: `admin_list_audit`'s `_resolve_any_subject_uuid` only decodes
  `prop_`/`opp_`/`org_`/`mat_`/`evt_`/`doc_` ids, so a `usr_`/`acc_`/`key_` subject id (exactly the
  ones `AnyPublicIdValue` was widened to cover for the *response*) silently matches nothing as a
  *filter* rather than narrowing correctly -- caught by `test_audit_list_filters_and_detail_renders_before_after`
  failing with a `404` before the fix. Both gaps (no get-by-id operation, no matching subject_id
  decode for the newer prefixes) are flagged for the coordinator in `web/admin/ops.py`'s decision 4.

### Decisions

1. Vocabulary in `<select>` options (`USER_ROLES`, the role/status pairs, `ACCOUNT_KINDS`,
   `ACCOUNT_ENTITLEMENT_SOURCES`, `PLAN_TIERS`) is hardcoded locally in `web/admin/people.py`/
   `web/admin/ops.py` as small display-only tuples rather than imported from `services.db.models`/
   `services.sor.ports` -- importing those would cross the web/API boundary
   `web/admin/shell.py`'s docstring draws ("no page ever talks to the database"). The API still
   enforces every value on write, so drift here only ever produces a form option the API rejects
   with its own validation notice, never a silent wrong write.
2. The keys issue form's `licence_accepted_version` is sent as a hidden constant
   (`api-licence-1.0`, mirroring `services.api.pro.API_LICENCE_VERSION`), never a form field --
   `AdminApiKeyCreate` requires it to equal that exact value, so there is nothing for an operator to
   choose.
3. The keys list's "status" filter is the API's own `revoked` boolean (`GET /admin/v1/keys` has no
   `status` parameter), rendered as an active/revoked choice.
4. Found and fixed a real bug flagged by a concurrent agent's own verbatim-tail note in this same
   README (see the "Tasks and posts" section above, "three failures ... in
   `web/test_admin_people.py`"): `customers/detail.html`'s stale-banner sentence line-wrapped across
   two lines in the template source, so the literal AC3 wording never appeared as one contiguous
   string in the rendered HTML even though the banner itself rendered correctly. Fixed by keeping
   the sentence on one source line; `test_customer_detail_shows_stale_banner` and the two
   subscription tests now pass.

### Verbatim tails

```
$ .venv/bin/python -m pytest web/test_admin_people.py web/test_admin_ops.py
..........................                                               [100%]
26 passed, 69 warnings in 5.68s
```

```
$ .venv/bin/ruff check web/admin/people.py web/admin/ops.py web/test_admin_people.py web/test_admin_ops.py
All checks passed!
$ .venv/bin/ruff format --check web/admin/people.py web/admin/ops.py web/test_admin_people.py web/test_admin_ops.py
4 files already formatted
```

```
$ .venv/bin/mypy --cache-dir /tmp/mypy-fe-c web/admin/people.py web/admin/ops.py
Success: no issues found in 2 source files
```

```
$ .venv/bin/python -m pytest web --ignore=web/test_e2e.py
106 passed, 260 warnings in 40.99s
```

### Not done / deferred

- `web/static/css/admin-people.css` and `admin-ops.css` are small (a couple of layout-only rules
  each) -- everything else reuses `web/static/css/admin.css`/`styles.css` unchanged, as intended.
- No JavaScript anywhere, including the one-time key-secret page (no copy-to-clipboard button; the
  field is a plain readonly `<input>` the operator selects manually).
- The audit detail page's bounded page-scan (decision above) is a real, documented limitation, not
  a hidden one -- see `web/admin/ops.py`'s decision 4 for the exact API-side gaps that would remove
  the need for it.
