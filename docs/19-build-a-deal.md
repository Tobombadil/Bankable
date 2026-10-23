# Build a deal: private projects on Inframation's graph

Status: **specification, 2026-10-22; amended 2026-10-23 to what is built.** Lanes P0 (logs), P1 (data and
authorisation core) and P2 (API) are built, and the Tracker (lane tracker: the P3 pages with the item types of
the decision row of 2026-10-23, "My pipeline is one central tracker") is built behind the `MY_PIPELINE` switch
(§11). P4 to P6 are not.

- **The decision behind it:** the row of 2026-10-22 in `docs/00-PLAN.md`, "'Build a deal' is built natively in
  Inframation; bankablehq is the reference design, not an integration target". The ADR is
  `docs/adr/0009-build-a-deal-native.md`.
- **The reference design:** `docs/18-bankable-bridge.md` (§1 for what Bankable has, §3.3.5 for the evidence
  mapping, §4 for the securities flag).

The thinnest useful version comes first (§2). Everything else is named and deferred (§3).

**Amended 2026-10-23 (the Tracker, §11).** A private project is now one **item** of the user's tracker: a
**project**, a **bid** or an **investment**, each with its own stages. "Track" replaces "Start a deal" on record
pages; the pages live at `/my` and `/my/items/{id}` (docs/30 §11.10), not `/my/projects…`. Where §1 to §8 say
"project" or "deal" for the private record, read "item"; §11 states what changed and what was built.

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
| Site-finder corridor (once the finder is public) | `candidate_site` | "Site near {substation} ({kV} kV)", map point (the corridor centre). Lane P6; until then the API refuses corridor links (§4.2) |
| Interconnection point (grid-point page) | `grid_connection` | "Project at {point} ({kV} kV)"; no map point (a point has no coordinates of its own) |
| Power plant (asset page), retiring or retired | `grid_connection` | "New project at {plant}", map point |
| RFP or funding notice (opportunity page), announced, open or reinstated | `offtake_or_rfp` (RFPs) or `funding` (funding notices) | "Bid: {title}", technology, capacity sought |
| Nothing (blank) | — | nothing |

The records follow the `docs/30` §2.6 table. The prefill obeys the licence rule in §7.

## 2. v1: the thinnest useful version

**Six capabilities.** Nothing else ships in v1.

### 2.1 Private project

A project is title, technology, capacity (MW, plus MWh for storage), a stage the user sets, and a map point.

- **Stages** (user-set, never inferred and never shown publicly): Prospecting, Site control, Development
  (interconnection and permits), Ready to build, Construction, Operating. These are a project's; a bid and an
  investment have their own (§11.1).
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

**Ids.** Every private table's `id` is a random uuid4, not the uuid7 `docs/21` §1 uses for public records: item,
link and evidence ids appear in private URLs (`…/checklist/{item_id}`, `…/links/{link_id}`) and in activity
detail, and a time-ordered id would date the row. The `prj_`, `nte_` and `inv_` public ids are drawn from a fresh
uuid4 too, never from the row's key. Only `checklist_template_item`, which is public content, keeps uuid7
(`services/projects/models.py`).

| Table | Columns (types, constraints) | Notes |
|---|---|---|
| `private_project` | `id` uuid PK; `public_id` text unique (`prj_…`); `title` text NOT NULL (1–200); `technology` text NULL (Inframation classes plus `other`); `capacity_mw` numeric(12,3) NULL ≥0; `storage_mwh` numeric(12,3) NULL ≥0; `stage` text NOT NULL default `prospecting` (CHECK vocabulary §2.1); `lat`, `lon` numeric(9,6) NULL (both or neither; ranges checked); `point_precision` text NULL (CHECK `user`, `exact`, `county_centroid`, `state_centroid`, `corridor_centre`); `origin_type`, `origin_public_id` text NULL (what it was started from); `archived_at` timestamptz NULL; `created_at`, `updated_at` | There is no owner column: ownership is a member row, so one table answers every access question |
| `private_project_member` | `id`; `project_id` → `private_project` ON DELETE CASCADE; `user_id` → `user`; `role` text CHECK (`owner`, `editor`, `viewer`); `added_by_user_id` → `user` NULL; `added_at` | UNIQUE (`project_id`, `user_id`); partial unique index: one `owner` per project |
| `private_project_link` | `id`; `project_id` (cascade); `record_type` text CHECK (`proposal`, `site`, `asset`, `opportunity`, `organization`, `interconnection_point`, `corridor`); `record_public_id` text NULL; `params` jsonb NULL (corridor permalink parameters only); `relation` text CHECK (`this_project`, `nearby`, `candidate_site`, `grid_connection`, `offtake_or_rfp`, `funding`, `counterparty`, `comparable`); `added_by_user_id` NULL; `created_at` | UNIQUE (`project_id`, `record_type`, `record_public_id`, `relation`); at most 50 links per project. **No foreign key to public tables**, on purpose: a link is a reference resolved live, so merges, takedowns and source gating never cascade into private rows |
| `checklist_template_item` | `id`; `pack_version` text; `key` text; `folder` text CHECK (six folders); `pillar` text CHECK (`fundamentals`, `counterparties`, `regulatory_site`, `sponsor`); `name` text; `guidance` text NULL (private pack only); `sort_order` int; `technologies` text[] NULL | UNIQUE (`pack_version`, `key`). Public content, not user data |
| `private_project_item` | `id`; `project_id` (cascade); `template_key` text NULL (NULL for a custom item); `pack_version` text NULL; `name` text NOT NULL; `folder`, `pillar` text; `status` text CHECK (`not_started`, `in_progress`, `done`, `not_applicable`) default `not_started`; `sort_order`; `updated_by_user_id` NULL; `updated_at` | Created from the template when the project is created. **No `verified` value exists** |
| `private_project_evidence` | `id`; `project_id` (cascade); `item_id` → `private_project_item` (cascade); `record_type`, `record_public_id` text; `rule` text (which §2.4 rule fired); `state` text CHECK (`suggested`, `accepted`, `dismissed`); `decided_by_user_id` NULL; `decided_at` NULL; `created_at` | Reference only. The statement is rebuilt live |
| `private_project_note` | `id`; `public_id` (`nte_…`); `project_id` (cascade); `author_user_id` → `user` NULL (NULL after the author's erasure); `body` text NOT NULL (≤20,000); `created_at`, `updated_at` | Shown as "Former member" when the author is NULL |
| `private_project_invitation` | `id`; `public_id` (`inv_…`); `project_id` (cascade); `email` text NULL (cleared on accept, revoke or expiry); `email_hash` text NOT NULL (peppered: `services.projects.invitations.address_hash`, the pepper and construction of `services/api/audit.hash_identifier` over the trimmed, lower-cased address, never case-folded, because the account system identifies an address that way); `token_hash` char(64) unique; `role` text CHECK (`editor`, `viewer`); `invited_by_user_id`; `created_at`; `expires_at` (`created_at` + 7 days); `accepted_at`, `accepted_by_user_id`, `revoked_at` NULL | Partial unique index: one open invitation per (`project_id`, `email_hash`) |
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

As built (lane P2): the API answers the redirect case as `state: merged` with the survivor's summary and
`survivor_public_id`, leaving the stored reference as it is (an unmerge then restores the original); a retired
site is followed to its visible successor the same way. A site is read through `served_site`; assets and
organisations through the public tier their own detail routes read for every caller.

**Corridors.** If the site finder lane makes corridors addressable with a stable id, links store that id. If it
does not, the link stores the finder's permalink parameters (`params`), and the summary re-runs the finder for
them. Either way nothing about the corridor is copied.

**TODO (lane P6):** the finder lane has not settled corridor ids, so until it does `POST …/links` refuses
`record_type: corridor` with a `400`, `from` does not take a corridor, and a stored corridor link resolves as
`unavailable`. P6 adds the corridor read path to `resolve_link` and the corridor row to "Start a deal".

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

  As built (lane P2): an event is private when its request path is under `/my/`, `/v1/my/` or `/api/my/`, its
  transaction names such a path, a private module logged it, or any frame of its exception is in a private module.
  A private event keeps the request's method and its path with every id replaced by `{id}`, and loses its query
  string, headers, cookies, body and environment, its exception messages (types and frames stay), its log message
  and extras, and its breadcrumbs' text. Its trace context keeps only `trace_id` and `span_id`, and its spans
  only their ids, op, status and timing. Every other event has invitation tokens, addresses and SQL parameters
  redacted from its text, and any header naming a private page withheld. A `before_breadcrumb` hook drops a private
  module's breadcrumbs as they are recorded. An event that cannot be scrubbed is dropped. The same hook is
  installed as `before_send_transaction`. Tracing is off (`traces_sample_rate=0.0`), but with the real SDK and
  tracing on, a transaction skips `before_send`, and it carried a private request's full URL, query string and
  headers.
- **The proxy access log** (`infra/compose/Caddyfile`, JSON access log): a log filter redacts the value of a `t`
  query parameter on every path (not only `/my/invitations/*`), and a token-length path segment under
  `/my/invitations/`, keeping the parameter's name. It is the pattern `infra/logging_config.py` applies to the
  application's own logs (`INVITATION_TOKEN_PATTERN`), on the URI, `Referer`, `Hx-Current-Url` and `Location`,
  so an invitation token never reaches a log. The access log otherwise records paths that, under §5.2 item 5,
  carry no private content.

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

As built (lane P2), with what it adds to the list above:

- **Headers.** Every `/v1/my/` response, errors included, carries `Cache-Control: private, no-store`,
  `X-Robots-Tag: noindex, nofollow`, `Referrer-Policy: no-referrer` and `Vary: Cookie`, and no `ETag`
  (`PrivateHeadersMiddleware`, outermost in `services/api/app.py`). A `500` is sent by Starlette's error
  middleware from outside every user middleware, so `private_unhandled_exception_handler` sets the same headers
  on it. Before 2026-10-23 a private `500` carried none of them, and the proxy added its own `Referrer-Policy`.
- **Cross-site writes.** A `POST`, `PATCH` or `DELETE` needs an `Origin` naming the site (`WEB_HOST`, or the scheme
  and host this request was addressed to), unless it is the site's own server-side call (its `X-Internal-Token`;
  the page did its own same-origin check). It also needs a body that is `application/json`, and takes no query
  string. Each failure is a `400 validation_error`. Behind the proxy the request's scheme is the visitor's
  (uvicorn believes Caddy's `X-Forwarded-Proto`), so `Origin: http://<this host>` does not pass over TLS.
- **Concurrent requests.** Concurrent adds of the same link answer with the one stored. On Postgres,
  `lock_project` serialises them and the second looks again once it holds the lock. SQLite takes no row lock, so
  the insert also runs in a savepoint and a conflict returns the stored link. Members opening a checklist at
  once share one stored suggestion. A view with a new suggestion to store first holds the project
  (`lock_project`; on SQLite, the database's write lock). It then inserts under a savepoint and reads back any row
  another writer stored first. Before 2026-10-23 both races were `500`s: 8 parallel requests failed about half
  the time on SQLite and on Postgres.
- **What a viewer who cannot see a linked record is told.** A link to it shows its type, the message and no id
  (§2.2, "nothing else"). The project's `origin` and the history's `record_public_id` and `origin_public_id`
  are `null` for that viewer, so a free member is not told the id of a paid-only record in its delay, and
  uuid7 ids do not reveal when records were created. The stored references are unchanged. The history's user ids
  of someone no longer a member are `null`, as their name is "Former member".
- **`Idempotency-Key` is accepted and never replayed.** `services/api/idempotency.py` stores a replayed response
  in `idempotency_record`, which is not a private table, is not erased with the account, and would hold an
  invitation link in clear for a day (§2.6 rule 2). So no private response is stored. A retry is safe where it
  matters: the same link answers with the one already there, an evidence decision is idempotent, and a second
  invitation to one address replaces the first. A retried create or note can repeat; storing private responses
  safely (encrypted, erased with the account) is a change to `idempotency.py`, outside this lane.
- **Invitations** are also limited to 10 new an hour per user (`ratelimit.default_limiter`), and preview and
  accept to 30 attempts an hour per user, on top of rule 6.
- **Bodies** take only the keys their operation documents; any other key (`owner_user_id`, `project_id`,
  `point_precision`, …) is a `400`.

Two operations the pages need and the table above did not name, both `POST` so a record id or a token travels
in the body, never in a URL:

| Method and path | Purpose | Role |
|---|---|---|
| `POST /v1/my/prefill` | Body `{record_type, public_id, relation?}`: what "Start a deal" would prefill (the form of §6.2), under the §7 rule, creating nothing | signed in, verified |
| `POST /v1/my/invitations/preview` | Body `{token}`: who invited you, the project's title and the role (§6.2's accept page), consuming nothing | signed in, verified, matching email |

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

The export's route is in place; its document is `services.api.my_projects.export_user_data`, which lane P4
writes. Until then the route answers `503 unavailable`. It is a `GET` with a cost: `SameSite=Lax` sends the
session cookie with another site's top-level navigation, so a request the browser marks
`Sec-Fetch-Site: cross-site` is refused with a `400` before it counts against the daily limit. **For lane P4:**
once the export writes an `exported` activity row, consider making it a `POST`, so no navigation can trigger it.

### 6.2 Pages (`web/my_projects.py`; templates in `web/templates/my/`; `web/static/css/my.css`)

**Superseded in part, 2026-10-23:** the pages were built as the Tracker (§11.4), under docs/30 §11's URLs and
words. The design rules below stand; "Start a deal" became "Track".

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
workspace is **"My pipeline"** in the menu, the private half of the site's two modes "Market | My pipeline"
(owner, later the same day, amending "My deals"); each item in it is a deal, and the map layer's line reads "Your
pipeline"; (3)
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
| B7 | The bid pack's eight names are the generic steps of any utility RFP or public solicitation, not a product's content | Registration, bid fee, site control, interconnection, pricing, credit support, forms and submission are what solicitations themselves ask for (§11.1) |
| B8 | Browsers send `Sec-Fetch-Site` with form posts (Chromium, Firefox and Safari 16.4+), so a form posted under `no-referrer` can still be checked as same-origin | Measured in Chromium on 2026-10-23 (§11.4); an older browser without it is refused, not let through |

## 11. The Tracker, as built (lane tracker, 2026-10-23)

The decision rows of 2026-10-23 ("My pipeline is one central tracker", "Navigation v2", "Inframation serves the
owner's own powered-land deals first") and docs/30 §11.4, §11.5 and §11.9.4, built on P1 and P2. Everything
stays behind `MY_PIPELINE=on`: pages and API alike answer `404` until then. The pages' `404` is the one an
unknown address gets, byte for byte, and `robots.txt` names `/my` only while the switch is on, so a crawler or
visitor learns nothing about the pages before launch. The API's paths are in the published contract anyway.

**Turning it on.** On the one-server beta the owner sets the GitHub Environment variable `MY_PIPELINE` to `on`
(a variable, not a secret: it is not a credential) and runs `deploy-beta` with `deploy`. `infra/ci/beta.py`
writes `MY_PIPELINE=on` into the env file only for exactly `on` (any case); unset, `off` or anything else
renders `off`, and the inputs check refuses a value other than `on` or `off`. A SOPS-file deploy sets
`MY_PIPELINE: "on"` in its secrets file (`infra/sops/secrets.example.plain.yaml`); a local Compose run sets it in
`infra/compose/.env`. Editing the beta host's `.env` by hand does not last: every deploy rewrites it.

### 11.1 Item types and stages (migration `0042_tracker_kinds`, after 0041)

| Kind | Stages, in order | Closed stages (no next deadline) | Checklist |
|---|---|---|---|
| `project` | prospecting, site_control, development, ready_to_build, construction, operating (§2.1, unchanged) | none | the bankability pack (§2.3) |
| `bid` | considering, preparing, submitted, shortlisted, won, lost, withdrawn | won, lost, withdrawn | the bid pack, `data/checklist/bid-v1.yaml` |
| `investment` | screening, diligence, term_sheet, committed, closed, passed | closed, passed | the bankability pack |

- **`private_project`** gains `kind` (text NOT NULL, default `project`, so every earlier row is a project), one
  CHECK tying the stage to its kind's vocabulary (`stage_for_kind`, in place of 0040's `stage_vocab`),
  `parent_project_id` (a bid's project: uuid NULL, foreign key to `private_project` ON DELETE SET NULL, CHECKs
  `parent_bid_only` and `parent_not_self`) and `submitted_on` (date NULL, bids only).
- **`private_project_member`** gains the member's own view of the item: `last_viewed_at` (timestamptz NULL) and
  `email_changes` (boolean NOT NULL default false).
- **The kind is set at creation and never changes.** A bid's checklist is another pack, and only a project may
  have bids attached; an edit naming `kind` is a `400` that says so. Changing type means a new item.
- **A bid attaches to one of the user's own projects they can edit** (owner or editor, not archived), checked
  through the same authorisation function; any other id (unknown, someone else's, read-only, archived) is the
  same `400`, so the field is no oracle. One project may have many bids. Deleting the project leaves its bids
  standing alone. The details form never sends the bid's project (a collaborator who cannot see that project
  would detach it by saving); the bid card has its own "Attach to" form. That form names the project only to a
  member of it; anyone else reads "None of your projects", which is true whether or not one is attached. The
  form's preselected first choice writes nothing (the project's own name for a member of it, "Leave it as it
  is" for anyone else), so saving it unchanged neither detaches a project the viewer cannot see nor re-sends
  one since archived, which the API would refuse. Detaching is its own choice ("None: it stands alone").
- **The vocabulary lives in `services/projects/kinds.py`**; the migration freezes its own copy and
  `services/projects/test_migration.py` pins the two equal. The pages read it from `GET /v1/my/kinds`.
- **Downgrade** drops the columns and constraints and restores `stage_vocab`, and refuses while any bid or
  investment exists (0020's precedent: a schema rollback must not delete a user's items or turn a bid into a
  project). Before launch there are none; after, back up first.

**The bid pack** (`bid-v1`, the bankability pack's schema, names only, pillar by folder): notice of intent or
registration; bid fee or deposit; site control evidence; interconnection status; pricing; credit support;
required forms; submission confirmation. Its keys start `bid_` and share none with the bankability pack, so a
private content pack (whose keys must be bankability keys) cannot replace them, and `bid-v1` is a reserved pack
name like `generic-v1`. No public-record evidence rule attaches to a bid item yet.

### 11.2 The tracker's two live columns (`services/api/my_projects.py::TrackerColumns`)

- **Next deadline.** The soonest `due_at`, today or later, of a linked RFP or funding notice, read through the
  gated serialiser (`gated_record`) at the viewer's tier and following merges; `null` when there is none the
  viewer can see, or the item's stage is closed. A project also takes the soonest deadline of its open,
  unarchived bids that the viewer is a member of (`via_bid`), so the list and the project's own page agree. A
  past due date is not a next deadline.
- **Changed since your last visit.** True when a linked proposal, notice or plant has a public change event,
  visible at the viewer's tier through the predicate the feeds use (`event_visibility_filter`;
  `asset_event_visibility_filter` for plants), that became visible after the member last opened the item (or
  joined it, before any visit). The badge gives the reason that asks most of the user: `deadline` (`due_at`
  changed), then `status` (lifecycle, listing or retirement), then `updated`; the Overview lists every such
  change with its source. Opening an item (`GET /v1/my/projects/{id}`) is the visit: it computes the changes,
  then stamps `last_viewed_at`. The stamp is the member's own and is shown to no one else.
- **Order.** Next deadline soonest first; then, and for undated items, the most recently changed first.
- **Cost, measured 2026-10-23** on a copy of the e2e dev store (9,563 proposals) with 25 items of 5 links each
  (125 links, 25 added notices with due dates, 125 visible change events), in process on SQLite, on a shared
  machine: `GET /v1/my/projects` median 124 to 137 ms and p95 290 to 355 ms over 30 calls (two runs); an item's
  detail median 116 to 153 ms over 15. The list reads the links in one query, the linked proposals and notices
  in one query per type (their sources by `selectinload`), merged ids one by one, and the change events in one
  query per history.

### 11.3 API additions (the "Private projects" paths of `api/openapi.yaml`)

- `kind`, a stage of that kind, `parent_project_id` and `submitted_on` on create; `stage`, `parent_project_id`,
  `submitted_on` and the member's own `email_changes` on edit (any member may set their own; stored only, no
  email is sent).
- `GET /v1/my/projects` takes `kind` (one or several) and returns `kind`, `stage_label`, `closed`,
  `next_deadline` and `changed_since_last_visit` on every row; the filter narrows rows, never deadlines.
- `GET /v1/my/projects/{id}` adds `stages`, `parent` (only for a member of that project), `bids` (only those the
  viewer is a member of, archived ones marked `archived`), `next_deadline`, `changes`, `last_viewed_at` (the
  previous visit) and `email_changes`.
- `GET /v1/my/kinds`: the kinds, their stages, the technologies, the link relations and what Track makes from
  each record type.
- "Track" (`from`): an RFP or funding notice makes a bid (`offtake_or_rfp` or `funding`); a project page a
  project (`this_project` or `nearby`) or an investment (`this_project`); a site a project (`candidate_site` or
  `nearby`); a retiring or retired plant or a grid point a project (`grid_connection`). `POST /v1/my/prefill`
  takes `kind`. An item's `url` is `/my/items/{id}`.

### 11.4 Pages (`web/my_projects.py`, `web/templates/my/`, `web/static/css/my.css`)

| Page | What it does |
|---|---|
| `/my` | The Tracker: a type filter (All · Projects · Bids · Investments, counts only the user sees), one row per item with type, stage, next deadline, a "changed" badge with its reason, and checklist progress. Ruled two-line cards below 720px, a table from 720px. The empty state points to the map, the site finder and RFPs. `?archived=1` lists archived items |
| `/my/items/new` | The type first (Project · Bid · Investment), then title, stage, a bid's project and submitted date, technology, capacity and map point. "Track" posts here with the record in the body and gets the form back prefilled under §7; `POST /my/items` creates |
| `/my/items/{id}` | Overview: set the stage (primary action), next deadline, changes since your last visit with their sources, a project's bids or a bid's project, readiness by pillar ("your status, not verified"), details, "Email me changes" ("Coming soon: no email is sent yet"), archive and delete |
| `…/linked`, `…/checklist`, `…/notes`, `…/people` | Linked records with Sources and Remove, and "Link a record" by pasting an Inframation address; the checklist in one form (only changed statuses are written) with Accept and Dismiss on suggestions; notes newest first with links clickable; members, roles, transfer, leaving, and invitations by a link shown once. Documents is not shown until data rooms ship |
| `/my/invitations/accept?t=…` | Who invited you, to what and as what, once token and address check out; Accept |

- **Private by construction:** with the switch on, every response carries `Cache-Control: private, no-store`,
  `X-Robots-Tag: noindex, nofollow`, `Referrer-Policy: no-referrer` and `Vary: Cookie`, and `robots.txt`
  disallows `/my` and `/my/` (`web/sitemaps.py::MY_PIPELINE_DISALLOW`); the `<title>` never names an item; URLs
  carry opaque ids only; the pages read nothing private but through the API. With it off, every route is
  Starlette's own `404`, before any check of its own (`web/my_projects.py::switched_on`, the router's
  dependency).
- **Forms under `no-referrer`.** A browser posting a form from a page served with `no-referrer` sends
  `Origin: null` and no `Referer` (found driving Chromium), so the usual same-origin check refused every form.
  The pages accept a form when the `Origin` check passes or the browser says `Sec-Fetch-Site: same-origin`
  (`web/my_projects.py::same_origin_form`, assumption B8), and refuse it otherwise.
- **The Track block** (`web/templates/partials/_track.html`) sits after the key facts on project, RFP or funding
  notice, and retiring or retired plant pages, only for a visitor with a session cookie while the switch is on
  (the site's signed-in test, as the header's Account link uses: an expired cookie gets the block, whose form
  and link lead to sign-in and show nothing private), and links "Your tracker". It never shows a count or "already tracked": `web/test_my_projects.py` and
  `services/api/test_my_projects.py` pin that a public record's page HTML and API answers are byte-identical
  before and after two users track it. Site pages offer Track through the API and the form; the block's include
  on `site_detail.html` is left to the record-tabs lane (V3), which holds that template.
- **Checked in Chromium** at 360, 720 and 1440px: no horizontal scroll on the tracker, the item tabs and a
  record page with the block, a 40-character unbroken word in a title included (it wraps in the breadcrumbs and
  the Overview's fields); cards below 720px, a table from 720px.
- **Not built here:** the `/my/invitations` list of invitations waiting for the user (it needs an API listing),
  the top bar's "My pipeline" link (lane V1 holds `base.html`), the email of changes (it needs a named automated
  channel with disclosure, `CLAUDE.md`), and Documents (data rooms, §3).

### 11.5 Tests

`services/projects/test_kinds.py` (vocabularies, packs, the parent rules, the last visit and the email opt-in),
`services/projects/test_migration.py` (0042 up and down with rows, the refusal while bids exist, Postgres types
and constraints), `services/api/test_my_projects.py` (Track kinds, the deadline order, changes per member and per
tier, the email opt-in and its rollback with a refused edit, an editor's bid the project's owner never sees,
archived bids, the kinds route, byte-identical public answers), `web/test_my_projects.py` (the switch and its
unknown-address `404`, sign-in, headers, robots, forms, every tab, the "Attach to" form that never detaches a
project the viewer cannot see, invitations, byte-identical public pages) and `infra/ci/test_beta.py` (the
beta's env file carries `MY_PIPELINE`, `off` unless the variable is `on`).

**Known limits.** "Today", "tomorrow" and the cut-off for a past deadline are UTC dates, so near midnight UTC a
reader in the Americas can see a day's difference. The site-wide footer on these pages still says every record
is public as soon as it is ingested (it means the public records; `base.html` belongs to lane V1). The private
transaction names in `infra/observability.py` scrub this lane's new path segments to `{id}`: safe, less
readable.

## 12. Data rooms, as built (lane data-rooms, 2026-10-23)

Next-wave item C of the decision row of 2026-10-23 ("Next wave"), to §3's security bar, with the deviations named
below. Every tracked item (a project, a bid or an investment) has a **Data room** tab: folders, files in them,
downloads and an access log. Nothing is AI-read yet (item D) and nothing is shown to funders (item E).

**Two switches, both off by default.** The data room answers only while `DATA_ROOMS=on` *and* `MY_PIPELINE=on`.
Off, every data-room API route is the `404` an unknown project gets, every page is the `404` an unknown address
gets, and the item shows no Data room tab. That includes the upload's length check: off, an upload with no
`Content-Length` or one longer than any file may be meets the same `404`, with the same headers, as a POST to an
unknown project, and its body is never read (review of 2026-10-23, finding 2; before, it was a `400` that named the
upload, which showed the route existed). On the beta neither `DATA_ROOMS` nor `DATA_ROOM_KEY` is rendered into
the env file yet, so it stays off until the coordinator wires them (below, "Turning it on").

### 12.1 What is built

| Part | Where |
|---|---|
| Tables `private_project_folder`, `private_project_document`, `private_project_document_event` (migration `0043_data_rooms`, after 0042; downgrade refused while a document is stored) | `services/projects/models.py` (`DATA_ROOM_TABLES`), `services/db/migrations/versions/0043_data_rooms.py` |
| The type allowlist, read from the bytes | `services/projects/data_room_files.py` |
| Storage, sealing, deletes after the commit, transfer slots | `services/projects/data_room_store.py` |
| The scanning hook and the clamd adapter | `services/projects/data_room_scan.py` |
| Folders, uploads, downloads, deletes, the access log, the allowance, erasure | `services/projects/data_room.py` |
| Roles: `download`, `upload`, `manage_files`, `audit` | `services/projects/authz.py` (`ROLE_ACTIONS`) |
| Nine routes under `/v1/my/projects/{project_id}/documents` (the "Private projects" paths of `api/openapi.yaml`) | `services/api/my_projects.py` |
| The Data room tab: `/my/items/{id}/documents`, `…/folders/{folder_id}`, `…/log`; upload, download, delete and folder forms | `web/my_projects.py`, `web/templates/my/_documents.html`, `web/static/css/my.css` |
| Account erasure (data-room rows and objects) | `services/api/account_erasure.py` |
| The retention job's orphan sweep | `services/retention/data_rooms.py`, `services/retention/run.py` |

### 12.2 Folders and roles

- **Folders.** The checklist's six (Contracts, Finance, Security, Permits, Feasibility, Legal & Corporate; docs/30
  §11.9.4 "the folders are the checklist's six", Bankable's six categories in `docs/18` §1.3) and **Other** (Bankable's
  "Reference": files outside the checklist). They are created the first time anyone opens the item's data room. Each
  checklist folder shows its file count and the item's checklist count there ("3 files · 2 of 8 done", your status).
  An owner or editor may add folders (1 to 80 characters, unique in the item in any case, at most 30 an item) and
  rename the ones added; the default folders keep their names and cannot be deleted, so the gap analysis (item D)
  can rely on them. The database holds each name case-folded (`name_key`, unique per item), so two members adding
  one name at once get one folder and a refusal, not two folders.
- **Roles.**

  | Action | Owner | Editor | Viewer |
  |---|---|---|---|
  | See folders and files, download | yes | yes | yes |
  | Upload, add or rename a folder | yes | yes | no |
  | Delete a file | any | the ones they uploaded | no |
  | Delete a folder they or a member added, with its files | yes | no | no |
  | Read the access log | yes | no | no |

  An archived item still lists and downloads (and its owner reads the log); nothing in it changes, and opening it
  writes nothing: the default folders are made on first opening only while the item is not archived, so an item
  archived before its data room was ever opened shows no folders. Views and downloads of an archived item are still
  logged. Anyone else gets the one `404`, for a real item and a missing one alike, and a file or folder id from
  another item is that `404` too.

### 12.3 Upload checks

- **Size.** One file at most `DATA_ROOM_MAX_FILE_MB` (default 25, ceiling 50); each user's allowance, across every
  item, at most `DATA_ROOM_QUOTA_MB` (default 1,024). An upload must send `Content-Length`; a body longer than the
  configured cap plus 64 KiB for the form (`data_room_store.upload_body_limit`) is refused before a byte is read
  (the app-wide `Idempotency-Key` handling reads a whole body first, so the bound sits in
  `PrivateHeadersMiddleware`); a file over the cap inside that margin is refused on the file and logged. The site's
  relay applies the same bound before it reads anything (`web/my_projects.py::upload_body_limit`).
- **The allowance** counts the files a user uploaded to items they are still a member of and, in the items they
  own, the files whose uploader has left, been removed or been erased ("Former member"): a departed collaborator's
  files count against the owner, who can delete them, not against someone who can no longer reach them. On
  PostgreSQL the check runs under a transaction-level advisory lock on the user (`data_room.lock_allowance`), so two
  uploads by one user in two API processes cannot both pass it and together overshoot it.
- **Rates and concurrency.** At most 1,000 files an item; per user an hour, 60 uploads, 300 downloads, 300 file
  deletions and 60 folder changes (adding, renaming and deleting folders together). One upload in progress per user
  per API process (a second is a `429` with `Retry-After`). An upload takes one of the process's transfer slots
  (`DATA_ROOM_TRANSFERS`, default 2) before its body is read, waiting up to 20 s without holding a worker thread,
  then `503`; a download takes one the same way. The site relays at most four uploads at once per web process and
  asks a fifth to try again.
- **Type, from the bytes, never the name.** Accepted: PDF; Word, Excel and PowerPoint (`.docx`, `.xlsx`, `.pptx`)
  without macros, ActiveX controls, embedded objects or links that load from outside the file; PNG, JPEG and TIFF
  images; CSV and plain text (UTF-8, named `.csv` or `.txt`, not starting like markup, a script or RTF). Refused, each
  with its reason in words: programs (Windows, ELF, Mach-O, Java, Android, WebAssembly), scripts (`#!`; a PDF naming
  `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile` or `/RichMedia`, hex escapes decoded), archives (ZIP, RAR, 7-Zip,
  gzip, bzip2, xz, cabinet, zstd, tar, and any accepted file that is also a ZIP), older and password-protected Office
  files (the OLE2 format), encrypted ZIP entries, SVG and HTML.
- **Office files are read as packages** (`data_room_files._ooxml`; review of 2026-10-23, finding 1: byte patterns
  over the raw `[Content_Types].xml` were bypassed with XML character references, part names outside the patterns,
  and a remote template that needs no macro at all). `[Content_Types].xml` and every relationships part (`*.rels`,
  or a part declared as one) are parsed as XML with a defused parser (stdlib `expat` with DTDs, entity and notation
  declarations and external entities refused, as `defusedxml` does; parts at most 1 MiB, all together 16 MiB), so
  `macro&#69;nabled` reads `macroEnabled` and a comment is not content. The format is the content type of the part
  the package's one `officeDocument` relationship points at, which must exist and be exactly that format's main
  type. Refused as macros or active content: any macro-enabled, VBA, ActiveX or OLE-object content type, the content
  type of a whole document inside the package (an embedded package), parts named like a VBA project, ActiveX or
  `embeddings/` (percent-escapes decoded), and relationships of type `oleObject`, `package`, `vbaProject` (and any
  VBA type), `activeXControl` (and `control`), `aFChunk` and `subDocument`. Refused as loading from outside the file
  (`external_content`): `attachedTemplate` (remote-template injection, which hands a network share the user's
  Windows credentials when the file opens), `externalLink` (links to other workbooks, which also carry DDE and OLE
  links), and any relationship whose target is external (`TargetMode` other than `Internal`, in any case, or a
  target that is a URL, a drive letter or a network path), except a hyperlink, which opens only when clicked. Also
  refused: two ZIP entries for one part (in any case), two overrides or defaults that disagree, two `Type` or
  `ContentType` attributes on one element, and XML that is not well-formed. The eleven workbooks among the
  repository's fixtures (EIA, EPA and ISO files) and a new openpyxl workbook with a hyperlink are all still accepted
  (measured 2026-10-23; no real Word or PowerPoint file was at hand to try). A name is cleaned (its last path segment, no control or
  bidirectional-override characters) and always ends in its type's extension (`report.docx` that is a PDF is kept as
  `report.docx.pdf`).
- **A refusal** writes its line in the access log and nothing else (no object, no row).

### 12.4 Storage, sealing and downloads

- **Private storage.** Objects go to the private bucket (`R2_BUCKET`: the raw-snapshot bucket, never the public
  tiles bucket) under `data-rooms/`, each under 128 random bits (`data-rooms/<32 hex>`); no key derives from a name,
  a title or an id. `DATA_ROOM_STORE=local` (a directory) exists for development and the tests and is refused in any
  environment that is not a development one, so a deployment never keeps documents on a container's disk. An upload
  is read in memory by the site and by the API (never spooled to a temporary file).
- **Encryption at rest.** R2 encrypts every object and its metadata at rest with AES-256 in GCM mode, keys held by
  Cloudflare (https://developers.cloudflare.com/r2/reference/data-security/, read 2026-10-23). On top of that each
  file is sealed in the API before it leaves: AES-256-GCM under its own random key, and that key sealed under the
  deployment's `DATA_ROOM_KEY` (32 bytes, a secret in the env file only) and kept on the document's row; both seals
  bind the object key. What that protects, stated without overreach (review of 2026-10-23): **file contents need
  `DATA_ROOM_KEY`**, which is in neither the bucket, the database nor its backups, so the bucket, a database dump or
  both together still open no file without it. **A bucket credential is more than ciphertext**: the nightly backup
  (`infra/scripts/backup.sh`) is a plain `pg_dump` in the same `R2_BUCKET`, under `postgres/`, so whoever holds a
  credential for that bucket (the R2 keys the layer-build workflows hold, a leaked token) also gets every file's
  name, type, size and uploader, the access logs and the wrapped keys. Deleting a row destroys its file's key in the
  live database; a backup from before the delete keeps the wrapped key for its 35 days, which opens nothing once
  the object is gone (it goes when the delete commits). Encrypting the backup, or moving it to its own bucket, is
  the deploy lane's (an owner question). Without a valid key the data room answers `503`; it never stores a file
  unsealed. Every download checks the SHA-256 taken at upload.
- **Downloads** go only through `GET …/documents/{document_id}/download`, which authorises the caller, reads and
  opens the object and streams it back (the site relays it). No signed URL is issued: a browser never addresses the
  store, and the response carries headers a bucket would not: `Content-Disposition: attachment`,
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox; default-src 'none'; frame-ancestors 'none'`,
  `Cross-Origin-Resource-Policy: same-origin`, and the private `no-store`, `noindex` and `no-referrer`. The type the
  bytes were recognised as sets `Content-Type`. **No response header names the file**: the proxy's JSON access log
  records response headers, so the page names it through the link's `download` attribute (Chromium saves it under
  that name: `web/test_e2e_data_room.py`). A request the browser marks `Sec-Fetch-Site: cross-site` is refused.

### 12.5 Malware scanning: what is and is not scanned

No antivirus engine runs on the one-server beta. With `DATA_ROOM_SCANNER` unset or `off` nothing is scanned, every
file is stored `not_scanned`, and every Data room view says so: "Files are not scanned for viruses: no virus scanner
runs on this server yet. Uploads are checked for type …, not for malware. Open files only from people you trust."
With `DATA_ROOM_SCANNER=clamd` and `CLAMD_ADDRESS` (`host:port` or a Unix socket), every upload is scanned before
anything is stored, over clamd's `INSTREAM`; a flagged file is refused and logged, and a scanner that does not
answer is a `503` with nothing stored (fail closed). clamd's `StreamMaxLength` must be at least the file cap. Not
scanned: files stored while no scanner was set (no rescan job yet), downloads (not rescanned), and anything a
signature engine cannot know. **What the type checks do not read** (say so to anyone relying on them): in a PDF,
compressed object streams, and the actions that only open something when the reader allows it (`/OpenAction` with
`/URI`, `/AA` additional actions, `/XFA` forms, `/GoToR` remote go-to, `/SubmitForm`; `/JavaScript`, `/JS`,
`/Launch`, `/EmbeddedFile` and `/RichMedia` *are* refused); in a CSV, formula and DDE injection (a cell starting
`=`, `+`, `-` or `@`, such as `=cmd|' /C calc'!A0`), which a spreadsheet program runs only if the user allows it; in
Word, field codes (`DDEAUTO`, `INCLUDETEXT`, `INCLUDEPICTURE`) inside the document text; in Excel, data connections
(`connections.xml`, Power Query), refreshed only if the user allows it.
This replaces §3's "a file is quarantined until a scanner worker passes it" while no scanner exists; with one, a
file is never available before it passes.

### 12.6 The access log

`private_project_document_event`: who opened a folder's file list (once per member and folder in ten minutes),
downloaded, uploaded or deleted which file (its name at the time), refused uploads with the reason, and folder
changes, with when. The item's owner reads it on `…/documents/log`; it is kept for the life of the item. Nothing
from it is written to `event`, the admin audit log, analytics or an application log.

### 12.7 Deletion and retention

- A file, a folder (with its files), an item (`data_room.forget_project` before the delete) or an account removes
  the rows in its transaction and the objects once the **root** transaction commits (`delete_after_commit`). A
  savepoint's release decides nothing (SQLAlchemy fires `after_commit` on it too, which before the review of
  2026-10-23 removed objects whose rows a later rollback restored); a savepoint rolled back forgets the deletes made
  in it and orphans the objects written in it. An upload whose row never commits (a rollback, or a session closed
  without a commit) removes its object when the root transaction ends.
- **Account erasure** (`erase_user`): in the items the user owns, every file, folder and log line goes; in other
  people's items the files they uploaded stay as that item's content, shown as uploaded by "Former member", and
  their log lines lose their author (the rule the owner set for notes, 2026-10-22). The rest of a private project's
  erasure (the items, memberships, notes) is still lane P4's.
- **Retention** (`data_room_orphans`, daily with the other DA-10 rules): an object no row names is deleted once 6
  hours old; rows whose object is missing are counted, not changed. It runs only where data rooms are in use
  (`DATA_ROOMS=on` or `DATA_ROOM_STORE` named). The report holds counts only.
- **Backups.** The nightly database dump keeps a deleted file's row (and sealed key) for its 35 days; its object is
  gone, so a restore brings back rows without files, which download as `503`. A restore must re-apply erasures
  (`docs/13` §5.5.5 item 5) and should be followed by deleting rows without objects.

### 12.8 Privacy

File names and contents never reach a log or an error report: object keys are random, no response header names a
file, no log line or exception message carries a name, the logging filter treats `services.projects` as private,
and `infra/observability.py` strips a private request's body. `services/api/test_my_projects.py` pins it: an
upload, a download, a folder view, a refusal, a delete and a forced `500` with a sentinel name and sentinel
content leave the sentinel in no log record, and an error report built for the same request carries none of it.
Public answers and pages are byte-identical before and after an item tracking a record gets a data room with a file
(`services/api/test_my_projects.py`, `web/test_my_projects.py`).

### 12.9 Measured

- **Latency, 10 MiB** (a seeded-random PDF), through the API in process (multipart parse, type check, seal, the
  real S3 client to a local S3 stand-in on loopback, then GET, open, hash check), five runs each, on a shared 4-core
  Xeon at 2.1 GHz, three runs of the test: upload median 0.09 to 0.10 s (max 0.23 to 0.25 s, the first including the
  S3 client's start), download median 0.047 to 0.048 s (max 0.052 to 0.057 s)
  (`services/projects/test_data_room_latency.py`). After the fix round, three runs: upload median 0.09 to 0.11 s
  (max 0.28 to 0.33 s), download median 0.050 to 0.055 s (max 0.053 to 0.066 s). The site's relay adds one
  in-memory copy over the internal network; not measured separately.
- **Memory, one 24 MiB upload** (tracemalloc peak through the API in process, local store): 96 MiB before the fix
  round (the review's figure: the test client's body, the form, the ciphertext and the ciphertext again with its
  header), 72 MiB after (the form reader keeps the file once, the seal writes header, ciphertext and tag into one
  buffer and opening reads through a view). One of those copies is the test client's, so the API holds about two
  copies of a file per transfer, and at most `DATA_ROOM_TRANSFERS` transfers at once (pinned:
  `test_one_upload_holds_a_bounded_number_of_copies_of_the_file`).
- **Phone first.** Chromium at 360 and 1440px: the Data room tab, a folder with a 64-character file name and the
  access log scroll no wider than the screen; folder rows are at least 44px tall (`web/test_e2e_data_room.py`).

### 12.10 Turning it on (coordinator and owner)

1. Generate a key: `python -m services.projects.data_room_store new-key`, and store it as the GitHub Environment
   secret `DATA_ROOM_KEY` (a SOPS deploy: `DATA_ROOM_KEY` in its secrets file).
2. Pass `DATA_ROOMS: ${{ vars.DATA_ROOMS }}` and `DATA_ROOM_KEY: ${{ secrets.DATA_ROOM_KEY }}` to
   `infra/ci/beta.py` in `.github/workflows/deploy-beta.yml`, render both (`DATA_ROOMS` `off` unless exactly `on`;
   the key only when set) and add them to `infra/sops/secrets.example.plain.yaml`. Not done in this lane: those files
   belong to the deploy lane, and their tests (`infra/test_deploy_beta_workflow.py`, `infra/ci/test_beta.py`) pin the
   exact lists.
3. The store needs nothing new: the beta's env file already carries `R2_BUCKET`, `R2_ACCESS_KEY_ID`,
   `R2_SECRET_ACCESS_KEY` and `R2_ACCOUNT_ID`.
4. Optional: `DATA_ROOM_MAX_FILE_MB`, `DATA_ROOM_QUOTA_MB`, `DATA_ROOM_TRANSFERS` (files sealed or opened at once per
   API process, default 2), and a scanner (`DATA_ROOM_SCANNER=clamd`, `CLAMD_ADDRESS`). A ClamAV container needs
   about 1 GB of memory for its signatures, which the 8 GB beta host can spare only with care.
5. Before offering it beyond the owner's own use: §3's independent security review and stated staff-access policy,
   and the privacy notice (§7).

### 12.11 Known limits

- A file is sealed and opened whole in memory (about two copies at once on the API), so the file ceiling is 50 MiB
  and two transfers run at once per API process by default. Chunked sealing would lift both.
- **A Word or PowerPoint file with a native chart is refused**: the chart's data is an embedded workbook (an
  `embeddings/` part with a `package` relationship), which these checks do not open. Saving as PDF works; accepting
  charts would mean checking the embedded workbook by the same rules (an owner question).
- **Corporate Word files attached to a template on a share** (`attachedTemplate` with an external target) are
  refused, by design: that is the remote-template vector. The refusal says to remove the link or save as PDF.
- One `DATA_ROOM_KEY` at a time: a document records its key's fingerprint, but there is no keyring yet, so rotating
  the key needs a re-seal job first.
- GIS files (KML, KMZ, shapefiles) are not accepted: they are archives or XML (an owner question).
- `idempotency.py` reads a whole upload body when an `Idempotency-Key` is sent; while the data room is on the
  middleware's bound keeps that to the configured cap and the form. Off, the upload path is like every other path,
  and no path's body size is bounded before the app reads it (FastAPI reads JSON bodies before dependencies run;
  neither Caddy nor the app sets a limit). That predates the data room and is its own task (for example Caddy's
  `request_body max_size` on every path but uploads).

### 12.12 Review of 2026-10-23: what changed

1. Office files are read as packages with a defused XML parser (§12.3); the three reviewed files (an OLE object by
   character reference, a remote template, macros by character reference with the main type in a comment) are
   tests in `services/projects/test_data_room_files.py`, and the third, stored by the API before as
   `x.docm.docx`, is refused through it (`services/api/test_my_projects.py`). New refusal reason
   `external_content`.
2. Off, the upload route's length check says nothing (§12 "Two switches").
3. Nits: deletes act on the root transaction's commit only (§12.7); the upload bound is the configured cap; an
   upload takes its transfer slot before its body is read, one upload in progress per user, waits hold no worker
   thread, and the site relays at most four at once (§12.3); the allowance is locked per user on PostgreSQL and a
   departed collaborator's files count against the owner (§12.3); file and folder deletes are rate-limited; the
   threat statement names the backup in the bucket (§12.4); the "not checked" list names PDF actions, CSV formulas
   and DDE, Word fields and Excel connections (§12.5); a `HEAD` on a download through the site is a `405` that reads
   and logs nothing (it ran the download and wrote "downloaded"); opening an archived item writes nothing (§12.2);
   folder names are unique in the schema (§12.2); one fewer copy of a file in memory per transfer (§12.9).
