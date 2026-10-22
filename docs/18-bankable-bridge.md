# The Bankable bridge: from a prospect on the map to a deal in Bankable

Status: draft, 2026-10-22. Decision rows of 2026-10-22 in `docs/00-PLAN.md` ("Bankable is the origination and
syndication half"; "The site is organised by development phase"). Waits on Bankable's source code, which the owner
is getting out of Lovable.

## 1. What Bankable already has (observed, 2026-10-22)

Source: the owner's Lovable preview of the Bankable app, read through the main JavaScript bundle the preview
serves. The page chunks that load after sign-in were not read: the code itself is the right source and the owner
is obtaining it. Everything below is **fact as of that bundle**; the meaning of each field is **inference** until
the code is read.

- **Stack.** A Lovable (React) app on a Supabase backend: Postgres tables reached from the browser, a storage
  bucket `project-documents`, and two server functions, `route-deal` and `send-notification`.
- **Routes.** Public: `/`, `/about`, `/methodology`, `/membership`, `/assessment`, `/services`, `/partners`,
  `/partner-application`, `/affiliates`, `/white-label`, `/contact`, `/privacy`, `/terms`. Signed in:
  `/dashboard`, `/projects`, `/project/:id`, `/pipeline`, `/marketplace`, `/showroom`, `/show`, `/roadshow`,
  `/roadshow/:id`, `/fund`, `/rfp`, `/engage`, `/engagements`, `/schedule`, `/manage`, `/notifications`,
  `/settings/profile`, `/settings/company`, `/invite/:token`, `/admin`.
- **A project is a data room.** Table `projects`: `developer_id`, `title`, `description`, `sector`, `location`,
  `total_capex`, `bankability_score`, `pillar_scores`, `fundamentals`, `metadata` (holds `document_progress`),
  `slug`, `is_marketplace_listed`, `is_archived`. With `project_documents`, `project_members` and
  `project_invitations`. A plan limits active projects per account.
- **The bankability score has five pillars**, default weights: completeness 0.35, fundamentals 0.25,
  counterparties 0.15, regulatory & site 0.15, sponsor 0.10.
- **Funders.** `funder_profiles` (criteria: investment types and stages, custom pillar weights). `pipeline_deals`
  pairs a funder with each marketplace-listed project, with a `compatibility_score`, a `match_rationale` and a
  status (new, routed, watching, accepted, passed). Passing a deal calls `route-deal` to send it on to
  better-fit funders.
- **Syndication surfaces.** Marketplace, showroom and roadshow pages, engagements with messages, and
  `executed_agreements` (inference: NDAs or engagement letters).
- **Bankable has an RFP page** (`/rfp`, a `useRfp` hook). Inframation's RFP and funding feed may be able to feed
  it. To check against the code.

## 2. How the phases map

| Phase | Where it lives | What exists | What the bridge adds |
|---|---|---|---|
| Prospect | Inframation | Base map (every lifecycle state, operating and retiring included), site finder, prospect score, resource layers, lists | A "Start a deal in Bankable" action on a site, project or opportunity |
| Originate | Bankable | Data room, documents, bankability score, assessment | A data room that starts from Inframation's facts instead of an empty form |
| Syndicate | Bankable | Marketplace, showroom, roadshow, funder pipeline, routing | Nothing at first. Later, perhaps a link from Inframation records to a listed deal, only with the deal owner's consent |
| Operate | Inframation | The same base map at later lifecycle states | Later: a deal that reaches financial close in Bankable updates the project's lifecycle on Inframation, with the owner's consent |

## 3. The hand-off (proposed)

**A pull, not a push.** Inframation never writes into Bankable's database and holds no Bankable credentials:

1. On a site, project or opportunity page, "Start a deal in Bankable" creates a hand-off record on Inframation:
   `GET /v1/handoff/{id}` returns the facts below, each with its source, URL, date and licence. The id is random
   and the record expires. It holds only public data, so a leaked link exposes nothing private.
2. The button opens `bankablehq.com/projects/new?from=inframation&handoff={id}`. Bankable (a change on its side)
   signs the visitor in or up, fetches the hand-off server side, and creates a project under that visitor's
   `developer_id`.
3. Bankable keeps the provenance in `metadata.inframation` and shows it in the data room, so a funder sees where
   each fact came from.

**Fields (first cut).**

| Bankable field | From Inframation |
|---|---|
| `title` | project name, or "Site near {substation}, {county}, {state}" for a finder corridor |
| `sector` | technology, mapped to Bankable's sector list (to read from the code) |
| `location` | county, state, country; the coordinates stay in `metadata` |
| `description` | one generated paragraph from the facts below, labelled as generated |
| `total_capex` | empty: Inframation does not know it, and a guess would mislead a funder |
| `fundamentals` | capacity MW, voltage and distance of the nearest line and substation, interconnection queue position and status, flood status, solar irradiance or wind speed, energy-community status, prospect score with its breakdown |
| `metadata.inframation` | every value with `source_id`, `source_url`, `retrieved_at`, `licence`; the Inframation record URL |

**Rules.**

- Only fields whose source licence allows commercial reuse cross the bridge. A row of reuse class `noncommercial`
  (`docs/26`) never does, and nor does any field from a source whose terms are not yet recorded.
- No personal data crosses: no landowner names, no contact people.
- Inframation's facts can pre-fill the "regulatory & site" and "fundamentals" pillars, but **Bankable decides
  what counts as evidence**. A screening value (a 4 km irradiance average, a modelled wind speed) is never shown
  as a certified fact.

## 4. Syndication and the law (flag, not advice)

Bankable's marketplace and roadshow put deals in front of funders. If any of that becomes visible to the public,
or if Bankable is paid per deal funded, it may meet securities rules on general solicitation (US Regulation D,
Rule 506(b) against 506(c)) and the rules on who may act as a broker. The coordinator is not counsel. Inframation
will not list deals seeking investment on its public pages until counsel has read it. A gated listing inside
Bankable, for invited funders, is the default.

## 5. What is needed from the owner

1. Bankable's source code. In Lovable, connecting the project to GitHub creates a repository. Once it exists,
   it can be added to this project's environment and read.
2. Confirmation of the field meanings in §1, and of Bankable's sector list.
3. Who may change Bankable (the developer the owner mentioned), so the import route in §3 step 2 is built there
   or by this team with access.
4. Whether accounts should be shared between the two sites later (single sign-on). Not needed for the first cut.

## 6. Assumptions

- A1. Bankable's Supabase tables are protected by row-level security, so the browser's public key cannot read
  other users' rooms. Not tested here, and not to be probed. To confirm in the code.
- A2. The owner owns or controls the Bankable app and may change it.
