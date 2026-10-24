# Bankable — instructions for any Claude session or agent in this repo

Read `docs/00-PLAN.md` first. It is the project's memory: vision, phases, decisions, open questions.
Then read the document for the phase you are working in. Do not re-derive what those files already settle.

## What this is
A platform that continuously discovers energy and infrastructure **proposals** (supply: interconnection queues,
permits, dockets, announcements) and **opportunities** (demand: RFPs, funding, tenders, large-load requests),
fuses them into one lifecycle record per real-world project, publishes them (delayed free / live paid / API),
and syndicates to social channels. It stands alone. The site is organised by phase, shown to readers as Find · Build a deal · Get funded (internally
Prospect, Originate, Syndicate); operations is the later lifecycle states on the one base map. "Build a deal" is a
private project on Inframation's own graph; the Bankable app at bankablehq.com is the reference design, not an
integration target (owner, 2026-10-22; `docs/19`).

## Non-negotiable guardrails
- Never scrape private aggregators (Interconnection.fyi, Cleanview, Energy Adepto, BidNet, Halcyon, Enverus).
- Respect per-source reuse terms in `data/sources.yaml`. PJM rows are not public until a licence exists.
  MISO/SPP/NYISO/ISO-NE terms must be read and recorded before their rows are published.
- Every stored record carries `source_id`, `source_url`, `retrieved_at`, `licence`. Attribution renders automatically.
- Public tier shows derived data; restricted raw rows link out.
- Outbound communication to real people (email, DMs, comments) is drafted by agents and sent by a human unless
  the owner has explicitly enabled a named automated channel with disclosure. No impersonation, no fake personas.
- Automated social accounts are labelled as automated where the platform requires it.
- Store the minimum personal data; honour deletion requests.
- Nothing from a private project or data room (Inframation's own or Bankable's) feeds public pages, matching, alerts,
  outreach, analytics or logs without the project owner's explicit consent (owner, 2026-10-22). `noncommercial` rows
  never flow to any commercial use.
- Never put model identifiers in commits, code comments, or shipped artifacts.

## Skills
Vendored skills sit in `.claude/skills/`, each with a `VENDORED.md` (source, commit, licence, local overrides);
`docs/43-skills-register.md` lists every candidate considered and its verdict. Precedence: the guardrails and WCAG 2.2
AA first, then the taste skill for anything visual, then the area's skill; a figure in `docs/04`, `docs/30` or `docs/31`
beats a skill's default. No skill authorises installing packages (no `npm -g`, no `npx`), fetching instructions from a
URL, sending data out or committing.
- Pages and screens: the taste skill (Design, above). Explore at least three directions before converging (`docs/31-design-practices.md` §3).
- Accessibility: `accessibility` for any template, component, form or map-control change and before every design gate.
- Speed: `core-web-vitals` (with `performance`) when a change touches the map, list or record pages, fonts, images or
  scripts; report lab medians of three with their conditions against D-13 and D-31.
- Charts, stat tiles, KPI rows: the bundled `dataviz` for every one, with `docs/31-brand-identity.md` §4 tokens in place
  of its placeholder palette, checked with its validator in light and dark.
- Database: `supabase-postgres-best-practices` before any table, index, migration or slow-query change; show
  `EXPLAIN (ANALYZE, BUFFERS)` and p50/p95, not the skill's impact figures.
- Python and the API: `docs/04` §3 and §5 and `docs/23`; the bundled `code-review` and `simplify` on the diff.
- Security: the bundled `security-review` for application code; `gha-security-review` for any change in `.github/workflows/`.
- Figures, counts and claims of done: `verification-before-completion` in every lane before it hands back; every
  published figure checked against the list that reproduces it.
- New sources: `docs/04` DA-14, in order. Legal and privacy: the legal-compliance agent and `docs/13`; an outside
  checklist is a checklist, never advice.
- Research, shared pages, new skills: the bundled `deep-research`, `artifact-design`, `skill-creator` (vet any new
  outside skill by `docs/43` §1 before vendoring it).

## Conventions
- Docs are numbered by phase: `0x` feasibility, `1x` business/product, `2x` architecture/data, `3x` go-to-market,
  `4x` build/launch, `5x` marketplace, `6x` operations. One topic per file. Cite sources with URLs.
- Python for data work (`.venv`, `pip install -r requirements.txt`). Tests next to code. Lint before commit.
- Each agent writes only to the paths named in its task. One agent per file area at a time; the coordinator
  verifies (tests, lint, spec) and commits — agents never commit. Lanes (2026-10-24, to cut integration
  conflicts and reruns without dropping a gate): return the `docs/CHANGELOG.md` line and any decisions-log rows in
  the result, and the coordinator writes both files; cut the patch once against the commit the lane started from and
  never rebase (the coordinator merges); run the tests for the lane's own area plus the API contract and register
  tests with a `--basetemp` of the lane's own, never the full suite (the coordinator runs it once per batch, and CI
  on every push); read measurement data from the shared copy the task names, never a private copy.
- CRM system of record is Attio; billing is Stripe; both sit behind the ports in `docs/20` §9 (owner, 2026-09-13).
- Record any decision in the decisions log in `docs/00-PLAN.md`. Record any assumption in the doc that depends on it.
- Preferred tone in docs: direct, evidence first, calibrated confidence, no filler.
- Asking the owner for anything: make each ask self-contained (owner, 2026-10-24: "don't send it without context ...
  let's not assume I read everything you send"). Say what it is, why it matters, how long it takes and exactly what to
  send back, with the links, every time; never point at an earlier message or a doc as the only explanation.
- Design: every page and screen follows the taste skill (`.claude/skills/taste-skill`, vendored, MIT; owner,
  2026-10-23). Screens people work in (the map, lists, record pages, My pipeline, admin) follow its
  `references/app-screens.md`; pages people read (About, Methodology, a landing section) follow its run. Gate UI work
  with its `scripts/lint.mjs` and `scripts/inspect.mjs` (`--app` for app screens; in cloud sessions set
  `CHROME_PATH=/opt/pw-browsers/chromium-1194/chrome-linux/chrome`) and report before and after. Precedence: the
  guardrails above and WCAG 2.2 AA (`docs/04`) first, then the skill's house rules, then `docs/31` where it has not
  yet been brought into line. Fonts stay self-hosted (D-21): replace any font CDN link the skill's scripts emit.
  Found pictures carry their credit and licence like any other source. Design lanes also follow
  `docs/31-design-practices.md` (data-product practices, references, how a design lane works).
