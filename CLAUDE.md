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

## Conventions
- Docs are numbered by phase: `0x` feasibility, `1x` business/product, `2x` architecture/data, `3x` go-to-market,
  `4x` build/launch, `5x` marketplace, `6x` operations. One topic per file. Cite sources with URLs.
- Python for data work (`.venv`, `pip install -r requirements.txt`). Tests next to code. Lint before commit.
- Each agent writes only to the paths named in its task and appends a line to `docs/CHANGELOG.md`. One agent per
  file area at a time; the coordinator verifies (tests, lint, spec) and commits — agents never commit.
- CRM system of record is Attio; billing is Stripe; both sit behind the ports in `docs/20` §9 (owner, 2026-09-13).
- Record any decision in the decisions log in `docs/00-PLAN.md`. Record any assumption in the doc that depends on it.
- Preferred tone in docs: direct, evidence first, calibrated confidence, no filler.
