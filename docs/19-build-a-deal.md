# Build a deal: private projects on Inframation's graph

Status: **specification, 2026-10-22. Nothing here is built.**

- **The decision behind it:** the row of 2026-10-22 in `docs/00-PLAN.md`, "'Build a deal' is built natively in
  Inframation; bankablehq is the reference design, not an integration target". The ADR is
  `docs/adr/0009-build-a-deal-native.md`.
- **The reference design:** `docs/18-bankable-bridge.md` (§1 for what Bankable has, §3.3.5 for the evidence
  mapping, §4 for the securities flag).

The thinnest useful version comes first (§2). Everything else is named and deferred (§3).

**Fit with what exists.** The spec is written against Inframation's code as it is:

- accounts, sessions and the password flows: `services/api/auth.py`, `services/api/auth_routes.py`, `web/auth.py`;
- the record model and public ids: `services/db/models.py`, `services/ids.py`, `docs/21`;
- the API conventions: `docs/23`, `api/openapi.yaml`;
- the visibility predicate and field gating: `services/api/visibility.py`;
- saved searches and alerts: `services/api/pro.py`, `services/alerts/`;
- erasure: `services/api/account_erasure.py`;
- retention: `services/retention/run.py`;
- the logging and error hooks: `infra/logging_config.py`, `infra/observability.py`, `infra/compose/Caddyfile`;
- the page plan: `docs/30` §2.6–§2.7.

## 1. Users and jobs

**Developers first** (decision row 2026-10-21): a developer or IPP, working alone or with one or two partners.
Their jobs, in the order v1 serves them:

| # | Job | v1 answer |
|---|---|---|
| J1 | "I found something on the map (a queued project, a candidate site, a retiring plant, an open RFP) and want to start working it up, privately." | **Start a deal** on the record creates a private project linked to it |
| J2 | "Keep the public facts about my project in one place and current." | Linked records show their facts **live**, with their sources, not copies |
| J3 | "What is missing before a funder will look at this?" | The bankability checklist, with **suggested** public-record evidence that is never marked verified |
| J4 | "Work on it with a partner." | Private notes, and sharing with named collaborators by invitation |
| J5 | "See my projects among everything else." | The private "My projects" dots on the map, seen only by the user |

**The flow.** On a record page, "Start a deal" opens a short form prefilled from the record (§6.2). One tap
creates the project and links it to the record. The other route is "Start a blank project" on the user's project
list.

| Started from | Default link relation | Prefilled |
|---|---|---|
| Project (proposal page), any status except withdrawn and cancelled | "This is my project" (`this_project`), or "I'm working on something near it" (`nearby`) | name, technology, capacity, map point, a suggested stage |
| Site-finder corridor (once the finder is public) | `candidate_site` | "Site near {substation} ({kV} kV)", map point (the corridor centre) |
| Power plant (asset page), retiring or retired | `grid_connection` | "New project at {plant}", map point |
| RFP or funding notice (opportunity page), announced, open or reinstated | `offtake_or_rfp` (RFPs) or `funding` (funding notices) | "Bid: {title}", technology, capacity sought |
| Nothing (blank) | — | nothing |

The records follow the `docs/30` §2.6 table. The prefill obeys the licence rule in §7.

## 2. v1: the thinnest useful version

**Six capabilities.** Nothing else ships in v1.

### 2.1 Private project

A project is title, technology, capacity (MW, plus MWh for storage), a stage the user sets, and a map point.

- **Stages** (user-set, never inferred and never shown publicly): Prospecting, Site control, Development
  (interconnection and permits), Ready to build, Construction, Operating.
- **Technology** uses Inframation's classes (`pipeline/normalize.py`), so a project and its linked records speak
  one vocabulary.
- **The map point** is a latitude and longitude with a precision:
  - `user`: a pin the user placed or pasted;
  - `exact`, `county_centroid` or `state_centroid`: copied from the starting record's placement, which Inframation
    already draws (ADR 0008). Copied only under the §7 licence rule.

### 2.2 Links to public records, facts live

A project holds links: a record type, a public id and a relation.

- **The page re-reads every linked record on each view,** through the same serialisers and visibility predicate
  that serve its public page, at the viewer's own tier. It shows the record's key facts and its Sources line
  (`attribution_text`, retrieval date, licence), exactly as the public page shows them.
- **Nothing is copied,** so a correction, a takedown, a merge or a licence change reaches every project at once.
- **A merged record is followed to its survivor** (`services/api/merged_redirect.py`).
- **A record the viewer can no longer see** shows "This record is no longer available", and nothing else: not the
  old name, and not why (`docs/21` §8 item 3).

Linkable record types:

| Type | Public id |
|---|---|
| proposal | `prop_` |
| site | `site_` |
| asset | `asset_` |
| opportunity | `opp_` |
| organisation | `org_` |
| interconnection point | `poi_` |
| site-finder corridor | the finder lane's id, or its permalink parameters; §4.2 |

A user can add a link from any record page ("Add to my project") or by pasting an Inframation URL into the project.

### 2.3 The bankability checklist

- **The method reused.** From Bankable (`docs/18` §1.3): six folders of standard project-finance documents, read
  through the five pillars (completeness, fundamentals, counterparties, regulatory and site, sponsor).
- **What the public repo carries.** Structure and generic item names only. These are the industry's own terms, such
  as "Power Purchase Agreement", "Interconnection Agreement", "Land Use Permit", "EPC Contract" and "Financial
  Model": the 32 names and six folders listed in `docs/18` §1.3. That is file `data/checklist/generic-v1.yaml`,
  with a key, a folder, a pillar and a name per item, and the schema `data/checklist/pack.schema.json`.
- **How detailed content is loaded privately later.** Detailed content (guidance text per item, per-sector
  variants, examples) stays out of this public repository (decision row 2026-10-22):
  - It is a **content pack** in the same schema, kept outside the repository, in a private object or the owner's
    private repository.
  - `python -m services.projects.load_checklist --pack-url "$CHECKLIST_PACK_URL"` loads it at deploy.
    `CHECKLIST_PACK_URL` is a SOPS-managed secret (`docs/60`).
  - The loader validates the pack against the public schema and writes versioned rows to the template table
    (§4.1). An unmatched key is refused.
  - Until a pack is loaded, the generic names are what users see.
- **Item status is the user's own statement:** Not started · In progress · Done · Not applicable. **There is no
  "verified" status and no "certified" label in v1.** Verification needs documents and a reviewer, which come later
  (§3). The UI says "Done (your status)" wherever a status is shown to anyone else.
- **Readiness by pillar.** The project page shows, per pillar, "N of M items done (your status) · K with suggested
  public-record evidence". There is no single 0–100 score in v1, because a number built from self-reported ticks
  would read as an assessment (`docs/18` §4.2, "the labels"). Whether to add one is an owner question (§9).

v1 items carry pillar tags by folder. A content pack may override them per item.

| Folder | Pillar |
|---|---|
| Contracts | counterparties |
| Finance, Security | fundamentals |
| Permits | regulatory and site |
| Feasibility | fundamentals |
| Legal & Corporate | sponsor |

Completeness counts every item.

### 2.4 Suggested public-record evidence, never verified

When a linked record carries a fact that bears on a checklist item, the item shows a suggestion. Each suggestion
says what the record states, its source and its retrieval date. The rules (`docs/18` §3.3.5, unchanged):

| Linked record states | Item | Suggestion text (shape) |
|---|---|---|
| A queue position (lifecycle `filed` or later) | Interconnection Agreement | "{Operator} queue {id}, status as stated: {…}. A queue record, not an interconnection agreement." |
| An executed interconnection agreement, where the source states it | Interconnection Agreement | "{Source} reports an executed interconnection agreement (as of {date}). Your copy of the agreement is the evidence." |
| A GB TEC register entry | Grid Connection Agreement | "NESO TEC register lists a connection agreement: {MW} at {site}, connection date {date}." |
| An air permit record | Air Quality Permit | "Air permit {id}, {agency}, {date} (public record)." |
| A federal permitting or NEPA record, or a FERC docket | Environmental Approvals/Licenses | "{Agency} record {id}, status {…}." |

- **Never suggested from a public record:** land rights, land-use and construction permits, offtake, PPA, EPC,
  finance, security and legal items.
- **The label everywhere** is "Suggested from public records · not verified". **Accept** keeps the suggestion on the
  item and **Dismiss** hides it; neither changes the item's status.
- **Rebuilt each time.** The suggestion text is rebuilt live from the record. Only the reference and the rule that
  fired are stored, so it changes when the record does.

### 2.5 Private notes

Plain-text notes on a project, by any editor:

- shown newest first, with author and time;
- at most 20,000 characters each;
- rendered escaped, with links made clickable and `rel="noreferrer noopener"`;
- editable by their author; deletable by their author or the project owner.

### 2.6 Sharing with named collaborators

Roles: **owner** (one per project), **editor** and **viewer**.

| Action | Owner | Editor | Viewer |
|---|---|---|---|
| Read the project, links, checklist, notes, members | yes | yes | yes |
| Edit fields, links, checklist status, evidence decisions; add notes | yes | yes | no |
| Invite, revoke invitations, change roles, remove members, transfer ownership | yes | no | no |
| Delete or archive the project | yes | no | no |
| Leave the project | no (transfer first) | yes | yes |

**The invitation rules.** Each is pinned by a test (§5.6); they follow lessons from the Bankable review without
repeating its details:

1. **Tied to one address.** An invitation names one email address and a role. Only a signed-in user whose
   **verified** address (`user.email_verified_at` set) equals it, compared case-insensitively after trimming, can
   accept it.
2. **A real token, stored as a hash.** The token is 32 random bytes (`secrets.token_urlsafe(32)`), shown once, and
   stored only as its SHA-256 hash, as session tokens are (`UserSession.token_hash`). Lookup is by hash, so there is
   no timing oracle.
3. **Single use.** Acceptance locks the row, checks it is unused, unrevoked and unexpired, adds the membership,
   stamps `accepted_at` and clears the stored address, all in one transaction. A replay fails.
4. **Expires** after 7 days. The owner can revoke it at any time.
5. **No listing, no oracle.** Only the project owner can list a project's invitations. A wrong, expired, used or
   revoked token, or a wrong address, all get the same "This invitation is not valid for this account", with the
   same status code.
6. **Limits:** at most 10 pending invitations per project and 20 new invitations per user per day.
7. **Delivery.** In v1 the owner gets the invitation link to send themselves. The platform emails it only once the
   owner enables a named automated channel for invitations, with disclosure (`CLAUDE.md` outbound rule;
   `docs/13-legal-outreach-and-social.md`). That email would have a fixed template with no user HTML, go only to
   the invited address, never go to a suppressed address (`services/alerts/suppression.py`), and say who asked for
   it.

**Revocation.** Removing a member takes effect on their next request, because access is checked on every request
and never cached in the session.

### 2.7 "My projects" dots on the map

Inframation itself now serves the private layer (decision row 2026-10-22). The PLAN privacy rules apply in full:

- **One dot per project the signed-in user owns or is a member of,** placed at the project's map point. Projects
  without a point are counted, not drawn: "2 of your projects have no map position".
- **The dot's card** shows title, stage, checklist progress ("9 of 32 done, your status") and "Open". It shows no
  score (§2.3), no notes and no members.
- **The data is fetched by script after the page loads.** It comes from `GET /api/my/pins` (the web host's
  same-origin relay of `GET /v1/my/pins`), with `Cache-Control: private, no-store` and `Vary: Cookie`. The map
  page's HTML is byte-identical for every visitor.
- **Exclusions** are in §5.2. They show as their own line in the in-view panel: "Your projects: N in view (only
  you can see these)".
- **Withdrawal is immediate.** Pins are fetched fresh on each load. Removing a member or deleting a project removes
  the dot on the next load; signing out removes them all.

## 3. Explicitly later

| Next | What it needs first |
|---|---|
| **Document uploads** | The security bar below, in full, before the first byte is stored |
| **AI analysis** of a project and its documents | A model gateway with no training on customer data, no prompt retention beyond `docs/04` DA-10's 90 days, processing terms in the customer terms, and the owner's explicit opt-in per project |
| **Funders and "Get funded"** | Counsel's reading of `docs/18` §4 (general solicitation, broker rules, the labels). Until then nothing about a private project is shown to anyone the owner did not invite |
| A single readiness score; "verified" items | Documents and a stated reviewer (who verifies, against what) |
| Alerts about changes near a private project | The project owner's explicit opt-in (`CLAUDE.md` guardrail), and a design that keeps the project's point out of the shared alert machinery |
| API-key access to private projects | A new scope (`read:private`) and its own review. v1 is session only |
| Organisation workspaces (a team owning projects) | Account seats (`account.seats`) and an owner decision on billing |

**The security bar for documents.**

- **Private storage.** A private object bucket with no public access path. Object keys are random and never derived
  from file names or titles.
- **Short-lived signed URLs.** Download URLs are signed, last 5 minutes or less, and are issued only after the
  authorisation function (§5.1) says yes, for every download. Upload URLs carry size and content-type limits.
  Files are always served with `Content-Disposition: attachment` and never rendered inline.
- **Encryption at rest:** the provider's, plus per-project data keys held outside the database, so a database dump
  alone does not open documents.
- **Malware scanning before availability.** A file is `quarantined` until a scanner worker passes it. It is then
  `available`, or `rejected` and deleted.
- **An audit log** of every upload, download, share and delete: who, when, which file. Readable by the project
  owner, kept for the life of the project.
- **Retention.** Files are deleted with their project or on account erasure. A 30-day restore window in backups,
  and erasure re-applied on restore (`docs/13` §5.5.5 item 5).
- **An independent security review** before the feature is offered, and a stated staff-access policy.

## 4. Data model

### 4.1 New tables (migration `0040_private_projects`)

**Numbering.** The newest migration is `0037_site` (`services/db/migrations/versions/`). Parallel lanes may add
`0038` (wave 2: companies and RFP lists) and `0039` (site finder), so this takes `0040`. Its `down_revision` is
whichever of those has landed when it merges; the coordinator sets it.

**Where the models live.** The ORM classes go in a new module, `services/projects/models.py`, on the shared
`Base`, not in `services/db/models.py`. That keeps them out of the file other lanes edit, and lets one import
contract (§5.2) say who may touch them.

**Time and storage.** Every time column is `timestamptz`. Coordinates are `numeric(9,6)`, not PostGIS, because v1
needs no spatial query and the tests run on SQLite.

| Table | Columns (types, constraints) | Notes |
|---|---|---|
| `private_project` | `id` uuid PK; `public_id` text unique (`prj_…`); `title` text NOT NULL (1–200); `technology` text NULL (Inframation classes plus `other`); `capacity_mw` numeric(12,3) NULL ≥0; `storage_mwh` numeric(12,3) NULL ≥0; `stage` text NOT NULL default `prospecting` (CHECK vocabulary §2.1); `lat`, `lon` numeric(9,6) NULL (both or neither; ranges checked); `point_precision` text NULL (CHECK `user`, `exact`, `county_centroid`, `state_centroid`, `corridor_centre`); `origin_type`, `origin_public_id` text NULL (what it was started from); `archived_at` timestamptz NULL; `created_at`, `updated_at` | There is no owner column: ownership is a member row, so one table answers every access question |
| `private_project_member` | `id`; `project_id` → `private_project` ON DELETE CASCADE; `user_id` → `user`; `role` text CHECK (`owner`, `editor`, `viewer`); `added_by_user_id` → `user` NULL; `added_at` | UNIQUE (`project_id`, `user_id`); partial unique index: one `owner` per project |
| `private_project_link` | `id`; `project_id` (cascade); `record_type` text CHECK (`proposal`, `site`, `asset`, `opportunity`, `organization`, `interconnection_point`, `corridor`); `record_public_id` text NULL; `params` jsonb NULL (corridor permalink parameters only); `relation` text CHECK (`this_project`, `nearby`, `candidate_site`, `grid_connection`, `offtake_or_rfp`, `funding`, `counterparty`, `comparable`); `added_by_user_id` NULL; `created_at` | UNIQUE (`project_id`, `record_type`, `record_public_id`, `relation`); at most 50 links per project. **No foreign key to public tables**, on purpose: a link is a reference resolved live, so merges, takedowns and source gating never cascade into private rows |
| `checklist_template_item` | `id`; `pack_version` text; `key` text; `folder` text CHECK (six folders); `pillar` text CHECK (`fundamentals`, `counterparties`, `regulatory_site`, `sponsor`); `name` text; `guidance` text NULL (private pack only); `sort_order` int; `technologies` text[] NULL | UNIQUE (`pack_version`, `key`). Public content, not user data |
| `private_project_item` | `id`; `project_id` (cascade); `template_key` text NULL (NULL for a custom item); `pack_version` text NULL; `name` text NOT NULL; `folder`, `pillar` text; `status` text CHECK (`not_started`, `in_progress`, `done`, `not_applicable`) default `not_started`; `sort_order`; `updated_by_user_id` NULL; `updated_at` | Created from the template when the project is created. **No `verified` value exists** |
| `private_project_evidence` | `id`; `project_id` (cascade); `item_id` → `private_project_item` (cascade); `record_type`, `record_public_id` text; `rule` text (which §2.4 rule fired); `state` text CHECK (`suggested`, `accepted`, `dismissed`); `decided_by_user_id` NULL; `decided_at` NULL; `created_at` | Reference only. The statement is rebuilt live |
| `private_project_note` | `id`; `public_id` (`nte_…`); `project_id` (cascade); `author_user_id` → `user` NULL (NULL after the author's erasure); `body` text NOT NULL (≤20,000); `created_at`, `updated_at` | Shown as "Former member" when the author is NULL |
| `private_project_invitation` | `id`; `public_id` (`inv_…`); `project_id` (cascade); `email` text NULL (cleared on accept, revoke or expiry); `email_hash` text NOT NULL (peppered, `services/api/audit.hash_identifier`); `token_hash` char(64) unique; `role` text CHECK (`editor`, `viewer`); `invited_by_user_id`; `created_at`; `expires_at` (`created_at` + 7 days); `accepted_at`, `accepted_by_user_id`, `revoked_at` NULL | Partial unique index: one open invitation per (`project_id`, `email_hash`) |
| `private_project_activity` | `id`; `project_id` (cascade); `actor_user_id` NULL; `action` text CHECK (vocabulary below); `detail` jsonb (field names and ids only, **never values**); `occurred_at` | The project's own history, shown to its members. **Never written to `event`** |

The `private_project_activity` action vocabulary is: `created`, `field_changed`, `link_added`, `link_removed`,
`item_status_changed`, `evidence_accepted`, `evidence_dismissed`, `note_added`, `note_edited`, `note_deleted`,
`member_invited`, `member_joined`, `member_removed`, `role_changed`, `ownership_transferred`,
`invitation_revoked`, `archived`, `unarchived`, `exported`.

**Why private work never touches `event`.** The `event` table is append-only and feeds the public feed, alerts,
webhooks and social drafting. Keeping private work out of it removes that whole class of leak by construction,
instead of filtering it later. Admin audit events (`services/api/audit.py`) are not used for private work either.

### 4.2 How links work across record types

One resolver, `resolve_link(db, link, entitlement)` in `services/api/my_projects.py`, dispatches on `record_type`
to the read path each record's public detail route already uses:

| Record type | Read path |
|---|---|
| proposals and opportunities | the gated serialisers (`gated_proposal`, `gated_opportunity`) |
| sites | `services/api/sites.py` |
| assets | `services/api/assets.py` |
| interconnection points | `interconnection_point_visible` |
| organisations | `visible_organization_or_404` |

It returns one of three outcomes: a summary (name, status, the record's key facts, its Sources line and its public
URL), a redirect to a merged survivor, or `unavailable`.

**Corridors.** If the site finder lane makes corridors addressable with a stable id, links store that id. If it
does not, the link stores the finder's permalink parameters (`params`), and the summary re-runs the finder for
them. Either way nothing about the corridor is copied.

**Facts are never written into private tables,** with one exception: the starting record's values that prefill
the project's own fields at creation, under the §7 licence rule. Those then become the user's values, editable
like any other.

## 5. Access control and privacy, designed to be testable

### 5.1 One authorisation function, deny by default

```python
# services/projects/authz.py
Action = Literal["read", "edit", "invite", "manage_members", "delete", "leave"]

def authorize(db: Session, ctx: AuthContext, project_public_id: str, action: Action) -> ProjectAccess:
    """The only way any code reaches a private project. Returns (project, member, role) when the
    signed-in user holds a role that allows `action` (§2.6 table); raises the same `not_found`
    problem an unknown id gets in every other case."""
```

**Every route goes through it.** Every API route under `/v1/my/projects/{project_id}` takes it as a FastAPI
dependency (`project_access(action)`). Every write takes `project_id` from the returned `ProjectAccess`, never from
the request body.

**Refused in every case not explicitly allowed:**

- no session, or a session for an anonymised or disabled user;
- an API key, even an `admin` one (v1 is session only);
- a non-member;
- a removed member;
- a role below the action;
- an archived project for anything but read, unarchive and delete;
- a malformed id.

All of these return the same `404`, so an outsider cannot tell a private project from a missing one. A member
without enough role gets `403 forbidden_role`.

**Operators get no route into private projects.** No `/admin/v1` route reads a private project, a note, a link or a
member list, and admin reads do not bypass this function the way they bypass the visibility predicate.

**Database access is another matter.** Operators with direct database access can read rows. v1 relies on database
access control for that; field-level encryption arrives with documents (§3), and the staff-access policy says so.

**Cross-site requests.** State-changing `/v1/my/*` requests must send JSON and an `Origin` that names the site.
`web/my_projects.py` forms use the existing same-origin check (`web/auth.py::_is_same_origin`).

### 5.2 Private data never enters the public paths

Each item below is pinned by a test in §5.6.

1. **The public read paths.** `services/api/visibility.py`, `listing.py`, `geo.py`, `feeds.py`, `records.py`,
   `matches.py`, `serialize.py`, `services/alerts/`, `services/social/`, `services/match/`, `services/crm/` and
   `web/sitemaps.py` must not import `services.projects`. Only `services.api.my_projects`,
   `services.api.account_erasure`, `services.retention` and `services.projects` itself may. This is a new
   import-linter contract, `private-projects-isolated`, in `infra/importlinter.ini`. Web code reads only through the
   API, as the existing "web reads through the API" contract already requires.
2. **Counts, the in-view summary, search, sitemaps, feeds and JSON-LD** are unchanged by any number of private
   projects. Private ids, titles and points never appear in them.
3. **Social, matching, alerts and outreach.** Saved-search entities stay `proposal`, `opportunity`, `event` and
   `match` (`SAVED_SEARCH_ENTITIES`); a private project is not one. No private operation writes an `event`. The CRM
   lead signals (`services/crm/signals.py`) never read private tables.
4. **Analytics.** No `ui_event` name refers to private projects (`UI_EVENT_NAMES`), and `services/api/ui_events.py`
   rejects any property value matching a private id pattern (`prj_`, `nte_`, `inv_`).
5. **URLs and the map state.**
   - Private pages live under one prefix, `/my/` (site) and `/v1/my/` (API), so one rule covers caching, robots,
     logs and error reports.
   - Private URLs carry opaque ids only: never a title, slug, coordinate or note text.
   - The map's shareable state (`map.js` writes filters to the query string) never includes the private layer. Its
     on and off state lives in `localStorage`.
6. **Headers on every `/my/` and `/v1/my/` response:**
   - `Cache-Control: private, no-store`
   - `X-Robots-Tag: noindex, nofollow`
   - `Referrer-Policy: no-referrer`, so clicking a source link from a private page sends no private path to another
     site

   `/my/` joins `ROBOTS_DISALLOW` (`web/sitemaps.py`). Private pages carry no Open Graph tags, and the social card
   renderer (`web/og_card.py`) never sees a private id.
7. **Page HTML.** A public page's HTML never varies with the viewer's private data; the private dots arrive only
   by script (§2.7).

### 5.3 Log redaction

- **Application logs.** Private routes log the route name, the outcome, the latency and opaque ids. They never log
  a request or response body, a title, a note, an email or a coordinate. A logging filter in
  `infra/logging_config.py` drops record attributes named `body`, `note`, `title`, `email`, `lat`, `lon` and
  `token` from any record whose logger or path is private. The test fails if such a field appears anyway.
- **Error reports** (`infra/observability.py`):
  - `include_local_variables=False`, so stack frames do not carry locals;
  - `max_request_body_size="never"`;
  - a `before_send` hook that removes the request data, query string and breadcrumbs messages for `/my/` and
    `/v1/my/` paths.

  The current setting, `send_default_pii=False`, excludes cookies and addresses. It does not stop frame locals
  from being sent, and it may not stop request bodies. This change also benefits every other route.
- **The proxy access log** (`infra/compose/Caddyfile`, JSON access log): a log filter deletes the `t` query
  parameter on `/my/invitations/*`, so an invitation token never reaches a log. The access log otherwise records
  paths that, under §5.2 item 5, carry no private content.

### 5.4 Account deletion cascades

`erase_user` (`services/api/account_erasure.py`) gains one step, `erase_private_projects(db, user)`, in the same
transaction:

1. **Owned projects are deleted, with everything in them** (members, links, items, evidence, notes, invitations,
   activity). Collaborators lose access, and the deletion confirmation page lists those projects by title with
   their member counts first. Transferring ownership beforehand (§2.6) keeps a project alive.
2. **Memberships in other people's projects are removed.**
3. **The user's notes in other people's projects stay as project content,** with `author_user_id` set to NULL and
   shown as "Former member". Whether a departing collaborator's notes should instead be deleted is an owner
   question (§9).
4. **Invitations.** Open invitations the user sent are revoked. Invitations addressed to the user's email are
   revoked, and their address cleared.
5. **The erasure result** (`ErasureResult`) gains counts for each of the above.

**Retention** (a new rule in `services/retention/run.py`). An invitation's address is cleared on accept, revoke or
expiry, and the row is deleted 90 days after that. Projects and their contents are kept until the owner deletes
them or the account is erased: they are the user's own data, not ours to expire.

### 5.5 Export of the user's own data

`GET /v1/my/export` returns one JSON document (`schema: inframation.my-export/1`), as a download, with
`Cache-Control: no-store`. It is limited to 5 per day and recorded as `exported` in each owned project's activity.

**Included:**

- for each project the user owns: its fields, members (names and roles; emails only for invitations the user
  sent), links (`record_type`, `public_id`, Inframation URL, relation), checklist items and statuses, evidence
  references and decisions, notes and activity;
- for each project the user is a member of: their membership and their own notes.

**Never included:**

- **values from linked records:** a link exports as a reference and its URL;
- **other members' email addresses.**

### 5.6 Tests that pin each rule

| Rule | Test (file :: name) |
|---|---|
| One authorisation function; every route uses it | `services/api/test_my_projects.py::test_every_project_route_depends_on_project_access` (walks the router's routes) |
| Deny by default, role matrix | `services/projects/test_authz.py::test_role_action_matrix` (role × action, including non-member, removed member, anonymised user, API key, admin session, archived project, malformed id) |
| No existence oracle | `services/projects/test_authz.py::test_outsider_gets_identical_404_for_real_and_missing_ids` |
| No owner id from the body | `services/api/test_my_projects.py::test_body_cannot_set_owner_or_project_id` |
| Invitation rules 1–6 | `services/projects/test_invitations.py`: `test_token_is_single_use`, `test_expired_token_refused`, `test_revoked_token_refused`, `test_wrong_or_unverified_email_refused`, `test_errors_are_indistinguishable`, `test_token_stored_only_as_hash`, `test_pending_and_daily_limits`, `test_only_owner_lists_invitations` |
| Removal takes effect on the next request | `services/projects/test_authz.py::test_removed_member_refused_on_next_request` |
| Isolation contract | `infra/importlinter.ini` `private-projects-isolated`, run in CI with the other contracts |
| Nothing on public surfaces | `tests/test_private_isolation.py::test_sentinel_never_on_public_surfaces`. It seeds projects, links, notes and invitations carrying a sentinel string and coordinates, then fetches every public API operation in `api/openapi.yaml`, every feed, sitemap, `robots.txt`, the search and list pages, and record pages' JSON-LD and Open Graph tags; it asserts the sentinel and the private ids never appear |
| Counts unchanged | `tests/test_private_isolation.py::test_public_counts_unchanged_by_private_projects` (list totals, geo clusters, in-view summary inputs, coverage) |
| No `event` rows | `tests/test_private_isolation.py::test_private_operations_write_no_event` |
| Analytics refuses private ids | `services/api/test_ui_events.py::test_rejects_private_id_values` |
| Logs and error reports | `tests/test_private_isolation.py::test_logs_carry_no_private_content` (JSON formatter, CRUD plus a forced exception); `infra/test_observability.py::test_error_reports_drop_locals_and_bodies`; `infra/test_caddyfile.py::test_invitation_token_filtered_from_access_log` |
| Headers and robots | `web/test_my_projects.py::test_private_pages_are_no_store_noindex_no_referrer`; `web/test_seo.py` (adds `/my/` to the disallowed paths it checks) |
| Map dots | `web/test_e2e.py::test_private_dots_absent_from_url_html_and_public_counts` (signed in: dots drawn, URL and page HTML unchanged; signed out: none) |
| Erasure cascade | `tests/test_api_account_deletion.py::test_erasure_deletes_owned_projects_and_memberships`, `::test_erasure_anonymises_notes_in_others_projects` |
| Export scope | `services/api/test_my_projects.py::test_export_has_only_own_data_no_linked_values_no_other_emails` |
| Live links | `services/api/test_my_projects.py::test_link_follows_merge`, `::test_link_to_hidden_record_shows_unavailable_only` |
| Licence rule (§7) | `services/api/test_my_projects.py::test_prefill_skips_noncommercial_fields` |
| No "verified" | `services/projects/test_models.py::test_item_status_vocabulary_has_no_verified` |

## 6. API and pages

### 6.1 API (`services/api/my_projects.py`; a new "Private projects" tag in `api/openapi.yaml`)

**Conventions for every operation:**

- session only (`Session: []`), and the signed-in user must have a verified email;
- `Cache-Control: private, no-store`;
- `Idempotency-Key` accepted on POST (`services/api/idempotency.py`);
- RFC 9457 problems, as everywhere (`docs/23` §8);
- the same `404` for unknown and forbidden, as in §5.1.

| Method and path | Purpose | Role |
|---|---|---|
| `GET /v1/my/projects` | The user's projects: owned and shared, archived on request | signed in |
| `POST /v1/my/projects` | Create: blank, or `{from: {record_type, public_id, relation}}`; returns the project with prefilled fields marked | signed in, verified email; at most 25 active projects per user in v1 |
| `GET /v1/my/projects/{project_id}` | Detail, with the links resolved live (§4.2), readiness by pillar, member count | viewer |
| `PATCH /v1/my/projects/{project_id}` | Edit title, technology, capacity, stage, map point; archive and unarchive | editor (archive: owner) |
| `DELETE /v1/my/projects/{project_id}` | Delete, permanently | owner |
| `GET`, `POST /v1/my/projects/{project_id}/links`; `DELETE …/links/{link_id}` | Links | viewer reads, editor writes |
| `GET /v1/my/projects/{project_id}/checklist` | Items, statuses, live suggestions | viewer |
| `PATCH …/checklist/{item_id}`; `POST …/checklist` (custom item) | Status; add a custom item | editor |
| `POST …/checklist/{item_id}/evidence/{evidence_id}/{accept or dismiss}` | Evidence decision | editor |
| `GET`, `POST …/notes`; `PATCH`, `DELETE …/notes/{note_id}` | Notes | viewer reads; editor adds; author edits; author or owner deletes |
| `GET …/members`; `PATCH`, `DELETE …/members/{user_id}` | Members, role change, ownership transfer, removal, leaving | viewer reads; owner manages; a member may remove themselves |
| `GET`, `POST …/invitations`; `DELETE …/invitations/{invitation_id}` | Invitations | owner |
| `POST /v1/my/invitations/accept` | Body `{token}`; the token is never in the API path | signed in, verified, matching email |
| `GET …/activity` | The project's history | viewer |
| `GET /v1/my/pins` | Map dots: `{public_id, title, stage, lat, lon, precision, progress, role}` only | signed in |
| `GET /v1/my/export` | Own-data export (§5.5) | signed in |

### 6.2 Pages (`web/my_projects.py`; templates in `web/templates/my/`; `web/static/css/my.css`)

**Design rules.** Average user first, phone first at 360px:

- plain forms, no JavaScript needed for any core step;
- 44px tap targets;
- one column below 720px;
- the `docs/31` components and tokens, with no new colours except the private-dot token.

**"Start a deal" on record pages.** It replaces `docs/30` §2.6's Bankable link. The block keeps that section's
placement (after the key facts, before the timeline), records table and anatomy. The words change:

```
── Build a deal ─────────────────────────────────────────────────────
Is this your project? Start a private deal: link it to this record,
track what a funder will ask for, and share it with your partners.
Only you, and the people you invite, can see it.

[ Start a deal ]                        ← 44px tall below 720px, full width
─────────────────────────────────────────────────────────────────────
```

- **Mechanics.** The button is a POST form to `/my/projects/new`. The record id travels in the form body, not the
  URL, so access logs do not record intent.
- **Signed out.** A short-lived signed cookie (one hour, `HttpOnly`, `SameSite=Lax`) holds the record, and the
  visitor goes to `/login?next=/my/projects/new`. `next` is validated by the existing `_safe_next`.
- **Other changes to `docs/30` §2.6.** The affiliation sentence and the "in Bankable" arrow go. Its licence rule is
  replaced by §7's per-field prefill rule: there is no longer a commercial service behind the link, and §7
  explains what still cannot be copied. "Add to my project" appears on the same records once the user has a
  project.

**The pages.**

| Page | What it shows (360px first) |
|---|---|
| `/my/projects` | "Your projects": one card per project (title, stage, "N of M done", last change, a "Shared" mark). "Start a blank project". An empty state that points to the map and the site finder |
| `/my/projects/new` | The short form: title, technology, capacity, stage, map point ("from the record" or "I'll set it later"), and, when started from a record, "Linked to: {record}" with the relation choice. One primary button, "Create project". Prefilled fields say "from {record}" |
| `/my/projects/{id}` | Header: title, stage (editable), technology, capacity. Then **Linked records** (each a compact card: key facts, Sources line, "Open", "Remove"); **Readiness** (five pillar rows, counts, a link to the checklist); **Notes** (newest first, an add box); **People** (avatars or initials, "Invite"); "Show on map" (opens `/`; the layer turns on through `localStorage`, never the URL) |
| `/my/projects/{id}/checklist` | Six folders as collapsible sections, each with "N of M done". Each item: its name, the private-pack guidance under "What this is", once loaded; a four-way status control (Not started · In progress · Done · N/A); suggested evidence as a muted chip, "Suggested from public records · not verified", with **Accept** and **Dismiss**. The page header says "Statuses are what you and your collaborators have marked. Nothing here is verified" |
| `/my/projects/{id}/people` | Members and roles; pending invitations, with "Copy link" and "Revoke"; the invite form (email, role); "Transfer ownership"; "Leave project" |
| `/my/invitations/accept?t=…` | Who invited you, the project title and the role, shown only after the token and address both check out; "Accept" or "Decline". A signed-out visitor is sent to sign in or register with the invited address |
| `/account` (existing) | Gains "Download your data" (§5.5). `/account/delete` lists the projects that will be deleted (§5.4) |

**Map dots.**

- **The module.** `web/static/js/my_projects.js` runs on the home map. It fetches `/api/my/pins` and adds its own
  source and layer: a diamond in the private-dot token, hollow for centroid precision. It adds a "My projects"
  toggle to the layer list, and one line to the in-view panel (§2.7).
- **On phones** the dot card opens in the same bottom sheet the record drawer uses.
- **Needed from the home map lane** (`web/static/js/map.js`): a small hook. That means a `map:ready` `CustomEvent`
  carrying the map, plus a container for one extra in-view line, so this module never edits `map.js` or the
  shareable-state code.

## 7. Posture and licences

| What | Rule |
|---|---|
| Linking a private project to any public record | Allowed: a link is a reference (a public id), not a copy |
| Showing a linked record's facts on the project page | Allowed: they are read live through the visibility predicate at the viewer's tier, as the public page shows them. When the posture flips (`docs/26` §5), `noncommercial` records disappear here too, and their links show "no longer available" |
| Prefilling the project's own fields from the starting record | **Only from fields whose sources are all `open` or `attribution`** (`GatedRecord` field provenance in `services/api/visibility.py`). A field from a `noncommercial` source is left blank, with the live value shown beside it |
| Copying values into notes or exports | **Not offered.** v1 has no "copy into note" action, and the export carries references, not linked-record values (§5.5). A user may type what they like into a note; the platform never copies a `noncommercial` value into private storage |
| The map point from a record | Copied only under the prefill rule, and only at the precision the record is published at (a `derived_only` source never yields `exact`) |
| Suggested evidence | Rebuilt live from the record; only a reference is stored |

**The posture question, for counsel, not answered here.** Does a free private workspace keep Inframation within
its noncommercial posture? `docs/26` §3 (ii) and (iii) and the decision row of 2026-10-22 both point at it. The
coordinator's inference is "plausibly, for longer than a link to a commercial service would", because the
workspace is free and copies no `noncommercial` values. But Inframation is run by a company, its users are
commercial developers, and "Get funded" is planned. CC BY-NC's "not primarily intended for or directed towards
commercial advantage", and the Texas RRC's undefined "noncommercial use", may be read against the platform as a
whole. Counsel should answer it before the workspace is promoted. The permission letters to the six
`noncommercial` sources stay the route that removes the question (decision row 2026-10-22).

**Privacy notice and terms.** Before launch the legal lane updates `web/templates/legal/privacy.html` and the
customer terms (`docs/13-legal-customer-terms.md`). They must say what a private project stores, who can see it,
that it never feeds matching, alerts, outreach or analytics, how long it is kept, how to export and delete it, and
the staff-access policy (§5.1).

## 8. Build plan

**Each lane is sized for one agent in about a day,** owns the files listed, and ships with its tests. The
coordinator mounts routers, merges `api/openapi.yaml`, and sets the migration's `down_revision`.

**Files other lanes hold right now:**

- **The home map lane:** `web/templates/home_map.html`, `web/static/js/map.js`.
- **The site finder lane:** `web/templates/site_finder.html`, `web/static/js/site_finder.js`, `services/site_finder/`,
  migration `0039`.
- **Wave 2:** the companies and RFP list templates (`web/templates/organizations_list.html`,
  `web/templates/opportunities_list.html`), migration `0038`.

No lane below writes any of those.

| Order | Lane | Owns (writes) | Tests | Depends on |
|---|---|---|---|---|
| 1 | **P0 · observability hardening** | `infra/observability.py`, `infra/logging_config.py` (the private-field filter), `infra/compose/Caddyfile` (the token filter) | `infra/test_observability.py`, `infra/test_logging_config.py`, `infra/test_caddyfile.py` additions | none; benefits every route |
| 1 | **P1 · data and authorisation core** | `services/projects/` (`__init__.py`, `models.py`, `authz.py`, `checklist.py`, `load_checklist.py`, `invitations.py`), `services/db/migrations/versions/0040_private_projects.py`, `data/checklist/generic-v1.yaml`, `data/checklist/pack.schema.json`, the `private-projects-isolated` contract in `infra/importlinter.ini` | `services/projects/test_models.py`, `test_authz.py`, `test_invitations.py`, `test_checklist.py`; migration up and down on SQLite and Postgres | none |
| 2 | **P2 · API** | `services/api/my_projects.py` (routes, `resolve_link`, prefill, live evidence), the "Private projects" paths in `api/openapi.yaml` (handed to the coordinator as a fragment), `services/api/ui_events.py` (the id-pattern refusal) | `services/api/test_my_projects.py`, `services/api/test_ui_events.py` addition, `tests/test_private_isolation.py`, the contract tests both ways | P1 |
| 3 | **P3 · pages** | `web/my_projects.py`, `web/templates/my/*.html`, `web/templates/partials/_start_deal.html`, `web/static/css/my.css`, `web/sitemaps.py` (`/my/` in `ROBOTS_DISALLOW`); includes of the partial in `proposal_detail.html`, `opportunity_detail.html` and `asset_detail.html` | `web/test_my_projects.py` (360px layout states, forms, same-origin, headers), `web/test_seo.py` addition | P2 |
| 3 | **P4 · erasure, export, retention** | `services/api/account_erasure.py` (`erase_private_projects`), the export route's body in `services/api/my_projects.py` (handed over by P2 at a named function), `services/retention/run.py` (the invitation rule), `web/templates/auth/account.html` and `account_delete.html` | `tests/test_api_account_deletion.py` additions, `services/retention/test_retention.py` addition, `web/test_auth.py` addition | P2 |
| 4 | **P5 · map dots** | `web/static/js/my_projects.js`, the `/api/my/pins` relay in `web/my_projects.py` (P3 hands it over) | `web/test_e2e.py::test_private_dots_absent_from_url_html_and_public_counts` | P2, and the home map lane's hook (§6.2) or its release of `home_map.html` and `map.js` |
| 5 | **P6 · corridors** | "Start a deal" on finder results, and corridor links | In the finder lane's tests | The site finder lane releases its files; corridor ids settled (§4.2) |

**Outside the lanes:**

- the legal lane's privacy notice and terms changes (§7);
- `docs/30` §2.6–§2.7, which the design lane updates to §6.2's words and the `/my/` URLs;
- `docs/23` and `docs/21` rows for the new resource and tables, which the coordinator adds with the merge.

**Go-live gate.** The workspace is Inframation's own, so the Bankable bridge gate does not apply. Before it is
offered to beta testers, three things must hold: every §5.6 test passes, the §7 counsel question has an answer or
an owner decision to proceed, and the privacy notice is updated.

## 9. Open questions for the owner

**Answered by the owner, 2026-10-22** (decisions log): (1) counts by pillar only, no single score in v1; (2) the
workspace is **"My deals"** in the menu and on its pages (the map layer's "Your projects" line reads "Your deals"); (3)
invitations start as a copied private link, no email; (4) a departing collaborator's notes stay, shown as "Former
member". Questions 5 to 7 stay open; the v1 limits in 6 stand as defaults until the owner says otherwise.

1. **A readiness score in v1, or counts by pillar only?** The recommendation is counts only, until documents and a
   reviewer exist (§2.3).
2. **What to call it.** The public nav already says "Projects", meaning everyone's projects. The private ones need
   a name that cannot be confused with it: "My deals", or keep "My projects" as the map layer's name (decision row
   2026-10-22)?
3. **Invitation email.** Enable a named automated channel for invitation emails now, or start with "copy the
   link" (§2.6 rule 7)?
4. **A departing collaborator's notes:** keep them as project content, shown as "Former member", or delete them
   (§5.4)?
5. **Where the private checklist pack lives:** a private object store or the owner's private repository; and who
   writes it (§2.3).
6. **Limits for v1:** 25 active projects per user, 50 links per project, 10 pending invitations. Right for the
   beta?
7. **Counsel on §7's posture question**, before the workspace is promoted beyond the private beta.

## 10. Assumptions

| # | Assumption | Basis |
|---|---|---|
| B1 | Session cookies reach `/v1/*` through the site host, so the browser calls the API same-origin | `infra/compose/Caddyfile` routes `/v1/*` on the site host to the API; `web/auth.py` relays the cookie |
| B2 | No numbered migration after `0037_site` exists yet; `0038` and `0039` are reserved by other lanes | `services/db/migrations/versions/` on 2026-10-22; the coordinator's note |
| B3 | Users are never hard-deleted (they are anonymised), so private rows reference `user.id` without cascades, and erasure deletes them in code | `services/api/account_erasure.py` |
| B4 | The site finder may or may not give corridors stable ids; §4.2 covers both | PLAN row 2026-10-22 (the finder); the lane has not landed |
| B5 | v1 private data is small (tens of projects per user), so resolving links live on each view costs a few in-process reads | Measure on the beta; cap of 50 links per project |
| B6 | The generic checklist item names are industry terms, not Bankable's proprietary content | They are the standard project-finance document names; the decision row keeps "detailed template content" private, not the names |
