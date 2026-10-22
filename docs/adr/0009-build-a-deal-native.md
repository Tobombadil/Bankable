# ADR 0009 — "Build a deal" is a private project on Inframation's own graph

**Status:** Accepted · 2026-10-22 · solutions-architect; decided by the owner in `docs/00-PLAN.md` (2026-10-22,
"'Build a deal' is built natively in Inframation; bankablehq is the reference design, not an integration target")

**Supersedes:** the integration design in `docs/18-bankable-bridge.md` §3 as the plan. It stays as the reference
and contingency design. **Superseded by:** none

**Related:**

- `docs/19-build-a-deal.md` (the spec);
- `docs/18-bankable-bridge.md` §1 (what Bankable has: the method reused) and §4 (the securities flag, still
  binding for "Get funded");
- `docs/26-platform-posture.md` §3;
- `docs/21-data-model.md` §5.4 and §8 (visibility; withheld rows);
- ADR 0006 (ports for commercial systems; unchanged).

**Implemented by:** migration `0040_private_projects`, `services/projects/`, `services/api/my_projects.py`,
`web/my_projects.py` and `web/templates/my/` (`docs/19` §8). Not built yet.

## Context

The owner wants the second phase, shown to readers as "Build a deal", to turn a record on the map into a deal.
Earlier the same day the plan was to hand records to Bankable (bankablehq.com), a separate Lovable and Supabase
app, through a pull-based hand-off (`docs/18` §3). The code review of Bankable (`docs/18` §1) found three things:

- **Little to keep.** Bankable has a usable method: a document checklist in six folders, read through five
  bankability pillars. But it has little working code worth keeping.
- **A second stack to secure.** The review found access-control and server-function issues, held privately
  with the owner, that would have to be fixed before any integration.
- **A posture problem.** The bridge conflicts with Inframation's `noncommercial` posture (`docs/26` §3 (iii)).

The owner then judged that Bankable's industry graph "was not complete according to the vision and may be very
buggy or incomplete", and that it holds test and demo data only.

Inframation already has what a deal workspace needs underneath it:

- one record per real-world project, with companies, plants, lines, interconnection points and opportunities,
  each fact with its source and licence;
- accounts and sessions with argon2 passwords and revocable server-side sessions (`services/api/auth.py`);
- a visibility predicate that decides what each tier may read (`services/api/visibility.py`);
- an erasure procedure (`services/api/account_erasure.py`) and a retention job (`services/retention/run.py`);
- tests, CI gates and an import-linter that already enforce boundaries.

## Options considered

1. **Bridge to Bankable as built** (`docs/18` §3). Inframation hands a public-data snapshot to Bankable, which owns
   the deal.
   - *For:* it reuses an existing app.
   - *Against:* Bankable's issues must be fixed first; there are two stacks, two account systems and two security
     surfaces; facts are copied at hand-off and go stale; it waits on the six `noncommercial` sources (decision row
     2026-10-22); and its integration with a commercial service is exactly what `docs/26` §3 (iii) forbids under the
     current posture.
2. **Fix Bankable, then bridge.**
   - *For:* it keeps the owner's existing product.
   - *Against:* the fix is most of a rebuild (authorisation, storage, scoring, invitations) on a stack this team
     does not run, and it still leaves two products, the copy problem and the posture conflict.
3. **A separate private service inside Inframation**, with its own database and deployable.
   - *For:* the strongest isolation of private data.
   - *Against:* a second database, migrations, backups and deploy path for a v1 with six small features; links to
     public records would cross a network boundary on every view; and it duplicates the auth the main API already
     has.
4. **Private projects as tables in Inframation's database, behind one authorisation function and an import
   boundary** (chosen).
   - *For:* one stack and one account system. Links to public records resolve live through the same predicate the
     public pages use, so facts are never copied. It reuses sessions, erasure, retention, logging and CI.
   - *Against:* private and public data share a database, so isolation rests on code boundaries and tests, not on
     separate infrastructure.

## Decision

Option 4. A deal is a **private project** linked to public records. It is stored in new `private_*` tables
(migration `0040`, models in `services/projects/models.py`) and served under one path prefix, `/my/` on the site
and `/v1/my/` on the API.

1. **Access.** Every private read and write goes through `services/projects/authz.authorize`, which denies by
   default and answers an outsider with the same `404` as a missing id. Ownership is a member row, so one table
   answers every access question. No admin route reads private projects.
2. **Isolation.** Private data never enters the public paths. An import-linter contract forbids the public read
   paths, alerts, social, matching, CRM and sitemaps from importing `services.projects`. No private operation
   writes an `event`. A canary test seeds sentinel content and asserts it never appears on any public surface,
   count, feed, log or error report (`docs/19` §5.2, §5.6).
3. **Live links.** A link is a reference (record type and public id), resolved live at the viewer's tier and
   following merges. Values are copied only to prefill the project's own fields at creation, and only from `open`
   or `attribution` sources.
4. **Method, not content.** Bankable's checklist structure, generic item names and five pillars are reused.
   Detailed template content is a content pack loaded from outside this public repository. v1 has no
   "verified" status and no single score.
5. **Invitations.** Sharing is by invitation bound to a verified email address, using a single-use, hashed,
   expiring token.
6. **Later work, gated.** Documents, AI analysis and the funder side come later, each behind a stated bar
   (`docs/19` §3). "Get funded" waits on counsel (`docs/18` §4).

## Consequences

- **Inframation's security bar rises.** It now holds private user content. The spec's tests (`docs/19` §5.6) are
  the bar for v1, and document uploads have their own bar (`docs/19` §3) before the first file is stored.
- **Error reporting and logging tighten for every route** (no frame locals or request bodies in error reports; a
  private-field log filter; the invitation token filtered from the proxy log). These are changes worth having
  regardless.
- **Isolation is by code and tests, not infrastructure.** Database operators can read private rows. A separate
  database role or schema for private tables, and field-level encryption, are the hardening path when documents
  arrive. Option 3 stays the fallback if that bar cannot be met in one database.
- **One user, one account, one map.** The private "My projects" dots are served by Inframation itself under the
  PLAN privacy rules, with no third-party sign-in.
- **What happens to Bankable.** The `docs/18` hand-off and the "Start a deal in Bankable" link (`docs/30` §2.6)
  are not built. Bankable stays the reference design. If the owner later keeps Bankable as a separate product,
  `docs/18` is the contingency design, and its gates (the posture conflict, the private security fixes) still
  apply.
- **The posture question changes shape but does not go away.** A free private workspace copies no
  `noncommercial` values. Whether it keeps the platform within the `noncommercial` posture is a question for
  counsel (`docs/19` §7), and the permission letters to the six `noncommercial` sources remain the route that
  removes it.
