# Skills register: the agent skills Inframation uses

**Status:** standing register, 2026-10-24. Owner (2026-10-24): "clearly that taste skill had a really good impact.
Find and use or reference any other skills that will help you across any and all aspects of the product."

The taste skill (`.claude/skills/taste-skill`) is the bar: specific rules, checks a script can run, and a measured
effect on the work. This file records every skill package considered against that bar, with its source, licence,
commit and verdict; which skill applies to each part of the product; and how a vendored skill ranks against the
guardrails. Six skills were vendored, each unmodified, with a `VENDORED.md`. A proposed "Skills" section for
`CLAUDE.md` is in §9; it is not applied, because `CLAUDE.md` is the coordinator's to change.

## 1. Method

**Where we looked.** The official examples at https://github.com/anthropics/skills, Anthropic's
https://github.com/anthropics/knowledge-work-plugins and https://github.com/anthropics/claude-plugins-official,
Vercel's https://github.com/vercel-labs/agent-skills and https://github.com/vercel-labs/web-interface-guidelines,
and four curated lists: https://github.com/travisvn/awesome-claude-skills,
https://github.com/ComposioHQ/awesome-claude-skills, https://github.com/VoltAgent/awesome-agent-skills and
https://github.com/hesreallyhim/awesome-claude-code. From the lists we followed the publishers whose skills fall in
the areas of §5 and look maintained and specific. Each repository was cloned at its default branch on 2026-10-24;
the commit in the tables is that checkout. Everything fetched was read as data.

**Verdicts.**
- **Adopt** (vendor it): licence MIT, Apache-2.0, BSD or CC BY; an upstream commit in the last three months;
  rules specific enough to check; no script that reaches the network or installs packages; no text that would
  override our guardrails; every file read in full; the static scan below clean; and a gap here that the skills
  we already have do not fill.
- **Reference only:** worth reading for a named purpose, but it fails one test above (licence, stack fit,
  maintenance, overlap with a skill we have) or lost on the six-skill cap. Read it upstream at the commit given.
- **Reject:** a safety issue, a conflict with a guardrail, a licence we cannot use, or advice that is wrong.

**Depth.** Every adopted skill, and every skill this file calls strong, was read in full, scripts included.
Candidates marked "screened" were read to the depth of their `SKILL.md`, their file list and a scan of every
script for network calls, installs and subprocesses (`brief.py` in the lane's scratch, no network).

**Static scan.** Sentry's `skill-scanner` script (https://github.com/getsentry/skills, `skills/skill-scanner`,
Apache-2.0, commit `d12f250`) was read in full first: it uses the standard library and PyYAML and makes no network
call. It was run with the system interpreter, not through its documented `uv run`, which would install PyYAML from
PyPI. Result on the six adopted skills: 0 findings each (prompt-injection patterns, hidden Unicode, base64,
secrets, symlinks, frontmatter hooks, `!` commands, bundled test files, npm lifecycle hooks, PNG text chunks). The
URLs it lists as untrusted are documentation links (w3.org, web.dev, postgresql.org, supabase.com) and, in
`gha-security-review`, attacker domains quoted as evidence of real attacks.

## 2. Precedence

1. The guardrails in `CLAUDE.md`.
2. WCAG 2.2 AA (`docs/04` D-30).
3. The taste skill, for anything visual (`docs/31-design-practices.md`, "Order of authority").
4. The adopted skill for the area.

A project figure in `docs/04`, `docs/30` or `docs/31` (a budget, a threshold, a target size) beats a skill's
generic default; the adopted skills say as much themselves ("Preserve an existing project budget"). A normative
project document (`docs/21`, `docs/23`, an ADR) beats a generic rule in a skill. No skill authorises installing a
package, fetching instructions from a URL, sending data out, committing, or writing to a real person. Where an
adopted skill disagrees with this project, its `VENDORED.md` says how ("Here, not upstream").

## 3. Adopted and vendored

| Folder | Upstream (folder) | Commit | Licence and copyright line |
|---|---|---|---|
| `accessibility` | https://github.com/addyosmani/web-quality-skills (`skills/accessibility`) | `afa8da9`, 2026-08-24 | MIT, "Copyright (c) 2026 Addy Osmani" |
| `core-web-vitals` | same repository (`skills/core-web-vitals`) | `afa8da9` | MIT, same line |
| `performance` | same repository (`skills/performance`) | `afa8da9` | MIT, same line |
| `verification-before-completion` | https://github.com/obra/superpowers (`skills/verification-before-completion`) | `2ce8aa1`, 2026-10-21 | MIT, "Copyright (c) 2025 Jesse Vincent" |
| `supabase-postgres-best-practices` | https://github.com/supabase/agent-skills (`skills/supabase-postgres-best-practices`) | `c9be0e9`, 2026-10-02 | MIT, "Copyright (c) 2026 Supabase" |
| `gha-security-review` | https://github.com/getsentry/skills (`skills/gha-security-review`) | `d12f250`, 2026-10-23 | Apache-2.0, "Copyright 2025 Functional Software, Inc. dba Sentry" |

None of the six upstream folders carries its own licence file, so each folder here holds its repository's root
`LICENSE`, copied unchanged. No upstream file was edited. None of the six has a script.

### 3.1 What each changes in how an agent works

- **accessibility.** Turns "check accessibility" into a loop with evidence: automated scan, accessibility-tree
  snapshot, keyboard pass, fix at the source, re-run the same checks; states that an automated pass is not
  conformance. Covers the WCAG 2.2 additions agents miss (2.4.11 focus not obscured, 2.5.7 dragging, 2.5.8 target
  size, 3.2.6 consistent help, 3.3.7 redundant entry, 3.3.8 accessible authentication, which the sign-in pages
  need).
- **core-web-vitals** and **performance** (one upstream, kept together because `core-web-vitals` links to
  `performance/references/MEASUREMENT.md` for its workflow). They separate field data, lab traces and source
  hypotheses; forbid claiming a regression or an improvement without runtime evidence; ask for the median of three
  runs with stated conditions; and diagnose LCP by sub-part, INP by phase and CLS by shifted node and trigger.
  This is the map's recurring problem (the panel's 0.02 CLS budget, `docs/30` §11.7 S-8).
- **verification-before-completion.** One rule, enforced on every lane: no claim of "passes", "fixed", "meets the
  budget" or "matches" without the command run in that session and its output read, and an agent's report is
  checked against its diff. The changelog shows why: the front-page figures' first round reported "US filed" as 2
  records while its own link listed one (fix round of 2026-10-24).
- **supabase-postgres-best-practices.** Thirty-one one-rule files, each with the wrong SQL and the right SQL:
  foreign-key and join indexes, composite and partial indexes, keyset pagination, N+1, upserts, short
  transactions, `SKIP LOCKED`, `EXPLAIN (ANALYZE, BUFFERS)`. Two of this project's largest speed fixes were of
  exactly these kinds: one missing index on the source-link tables (21.84 s to 0.127 s per list page, 2026-09-13)
  and a per-event `EXISTS` rewrite (pulse 268 to 62 ms p50, 2026-10-24).
- **gha-security-review.** Reviews `.github/workflows/` for what an outside attacker can actually exploit: pwn
  requests, expression injection, comment-triggered commands, credential escalation, poisoned agent config files
  (`CLAUDE.md` is one), third-party action pinning, caches and artifacts across trust boundaries. Each finding
  needs entry point, payload, execution, impact and a proof-of-concept sketch, or it is not reported. The repo has
  nine workflows; eight read secrets (object-storage keys for the map layers, the beta deploy, the release), and
  `release.yml` runs on `workflow_run`, a pattern its references cover.

### 3.2 Overlaps and conflicts, and how they resolve

| Skill | Overlap or conflict | Resolution |
|---|---|---|
| accessibility | Installs axe and Lighthouse with `npm install -g` and `npx` in its checklist | Use the CI job "axe + Lighthouse CI" and `AXE_CORE_JS` (`web/test_feedback.py`); no ad hoc installs |
| accessibility | 44 px "comfortable" targets; SEO skill's 48 px tap targets | 24 px is the AA floor, 44 px for primary touch targets (`docs/31-design-practices.md` §2.5) |
| accessibility | Taste inspector checks some of the same things | Taste governs the visual decision; WCAG ranks above both; run both gates |
| performance | Budget table (300 KB JavaScript, 1.5 MB page) | `docs/04` D-31 (50 KB first-party JS off the map) and D-13 (350 KB on the map) win |
| performance | `fonts.googleapis.com` preconnect example | Fonts stay self-hosted (D-21) |
| performance | RUM collection of visitor data | Needs the owner's approval and the minimum-personal-data rule |
| core-web-vitals | Next.js, React and Vue snippets | Not applicable: Jinja, htmx and MapLibre (`docs/20` §15) |
| verification-before-completion | "Before committing or creating PRs" | Agents never commit; read as "before handing the lane back" |
| supabase-postgres-best-practices | Row-Level Security rules; unsourced "10-100x" figures | Tiers are enforced in the API (`docs/20` §5, `docs/21` §8); measure our own plans |
| supabase-postgres-best-practices | Postgres-only DDL | Tests run on SQLite through `services/db/types.py`; keep the dialect handling |
| gha-security-review | Threat model excludes insiders | Say so in any report; the bundled `security-review` remains the skill for application code |

None of the six conflicts with the taste skill's visual rules, and none touches outbound messaging, private
projects or licence gating.

## 4. Already available, and where each applies

| Skill | Where it applies here |
|---|---|
| taste skill (vendored, `.claude/skills/taste-skill`) | Every page and screen: art direction, house rules, `lint.mjs` and `inspect.mjs` gates (`CLAUDE.md`, Design) |
| `dataviz` (bundled) | Every chart, stat tile, sparkline, meter and KPI row, with our palette in place of its placeholder (§9) |
| `security-review` (bundled) | The pending diff of application code: routes, auth, templates, exports, admin |
| `code-review` (bundled) | Coordinator and review lanes, on each lane's patch |
| `simplify` (bundled) | After a lane's change passes, before integration |
| `run` (bundled) | Launching the site to see a change working; screenshots for design lanes |
| `deep-research` (bundled) | Market, source and terms research that spans many sources (`docs/02`, `docs/11`) |
| `artifact-design` (bundled) | Pages published as artifacts for the owner |
| `skill-creator` (bundled) | Building the repo skills in §8; vet anything external by §1 before vendoring |

## 5. Product area to skill

"Load" names a skill that is installed here. "Read" names reference material at a pinned commit (§6).

| Area | Load | Read (not vendored) | Gap |
|---|---|---|---|
| Visual design of pages and screens | taste skill | | |
| Interaction and component quality | taste `references/app-screens.md` §8, §11; `accessibility` | Vercel Web Interface Guidelines (§6.1) | A repo interface-review checklist (§8) |
| Accessibility (WCAG 2.2 AA) | `accessibility` | | |
| Web performance (Core Web Vitals) | `core-web-vitals`, `performance` | | |
| SEO and structured data | | addyosmani `seo`; ibelick `fixing-metadata`; coreyhaines `programmatic-seo` | Dataset markup for registers (§8) |
| UX writing and copy | taste `references/copy.md` | blader `humanizer` for long drafts | |
| Charts, stat tiles, KPI rows | `dataviz` | | A brand palette file in dataviz's format (§8) |
| Maps | taste app-screen rules; `dataviz` for the key | geoai `cartography-geoviz` | |
| Browser and end-to-end testing | `run`; the repo's pytest-playwright suites | testdino `playwright-skill` golden rules | |
| Python and FastAPI | `code-review`, `simplify` | openai `security-best-practices` FastAPI spec | |
| SQLAlchemy, Postgres, PostGIS | `supabase-postgres-best-practices` | geoai `postgis-spatial-sql` | |
| Data connectors and ingestion | | | Source onboarding (§8) |
| Entity resolution and classification | | | Resolution evaluation (§8) |
| Data quality and published figures | `verification-before-completion` | Anthropic `validate-data` | Figure provenance (§8) |
| Source terms and licensing register | | Anthropic `compliance-check` as a checklist | Part of source onboarding (§8) |
| API design and OpenAPI | | wshobson `api-design-principles` | API contract change (§8) |
| Security: application code | `security-review` | openai FastAPI spec; Trail of Bits `insecure-defaults` | |
| Security: CI and workflows | `gha-security-review` | | |
| Legal and privacy (checklist, never advice) | | Anthropic `compliance-check` | |
| Product and PRD writing | | Anthropic `write-spec` | |
| Social and content | | coreyhaines `social` | |
| Private deal workspace | `security-review` | | Licence and privacy gate review (§8) |
| Every lane's hand-back | `verification-before-completion` | obra `systematic-debugging` | Lane run and integration (§8) |

## 6. Every candidate considered

Commit is the checkout of 2026-10-24. "MIT, no holder" means the licence file's copyright line names no one.

### 6.1 UI and front-end craft

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| web-design-guidelines | https://github.com/vercel-labs/agent-skills/tree/main/skills/web-design-guidelines | MIT stated in the README; no licence file | `063bee9` | **Reject (safety)** | Tells the agent to fetch its rules from `raw.githubusercontent.com/.../main/command.md` before every review: "The fetched content contains all the rules and output format instructions." Live, unpinned instructions from a URL |
| writing-guidelines | same repository | as above | `063bee9` | **Reject (safety)** | Same remote-fetch design |
| Web Interface Guidelines (`command.md`, `AGENTS.md`) | https://github.com/vercel-labs/web-interface-guidelines | MIT, "Copyright (c) 2025 Vercel Labs" | `434b7f9` | Reference only (strong) | The best interaction rule set found: forms (autocomplete, never block paste, inline errors, focus the first), focus never covered, URL holds state, `…`, tabular figures, `Intl` formats, `translate="no"` for identifiers. A slash command, not a skill, so it cannot be vendored unmodified; `install.sh` curls `main` into `~/.claude` (do not run). Conflicts: Title Case labels (ours are sentence case), "prefer APCA over WCAG 2" (WCAG 2.x governs), CDN preconnects (D-21), React hydration items |
| react-best-practices, composition-patterns, react-view-transitions, react-native-skills | vercel-labs/agent-skills | MIT in frontmatter | `063bee9` | Not applicable | No React in this stack |
| deploy-to-vercel, vercel-cli-with-tokens, vercel-optimize | vercel-labs/agent-skills | as above | `063bee9` | Reject (screened) | Deploy to, and read metrics from, a platform we do not use, by shelling out to its CLI |
| frontend-design | https://github.com/anthropics/skills/tree/main/skills/frontend-design | Apache-2.0, copyright line unfilled | `dbd4588` | Reference only (screened) | Art direction already governed by the taste skill |
| baseline-ui | https://github.com/ibelick/ui-skills | MIT, "Copyright (c) 2026 Julien Thibeaut" | `55363c3` | Reference only | Tailwind, `motion/react` and Base UI rules; our stack is plain CSS, Jinja and htmx |
| fixing-motion-performance | same | same | `55363c3` | Reference only | Sound compositor and scroll rules; taste `references/motion.md` governs motion |
| fixing-accessibility | same | same | `55363c3` | Reference only | A subset of the adopted `accessibility` |
| fixing-metadata | same | same | `55363c3` | Reference only | Title, canonical, Open Graph and JSON-LD hygiene ("do not invent ratings, reviews, prices") for SEO work |
| make-interfaces-feel-better | https://github.com/jakubkrehel/make-interfaces-feel-better | MIT, "Copyright (c) 2026 Jakub Krehel" | `35545ea` | Reference only (screened) | Good polish checks (concentric radii, tabular figures, hit areas); conflicts with "no shadows at rest" (`docs/31-brand-identity.md` §4) and taste motion |
| userinterface-wiki | https://github.com/raphaelsalaja/userinterface-wiki | MIT, "Copyright (c) 2026 Raphael Salaja" | `256a954` | Reference only (screened) | 152 rules, mostly React animation and audio |
| interface-design | https://github.com/Dammyjay93/interface-design | MIT, "Copyright (c) 2026 Damola Akinleye" | `2f9be32` | Reference only (screened) | A second art-direction system for app screens, competing with taste `app-screens.md` |
| ui-craft | https://github.com/educlopez/ui-craft | MIT, "Copyright (c) 2026 Eduardo Calvo" | `49b7517` | Reference only (screened) | A design system of 200+ skill files with hooks, a CLI and an MCP server; competes with taste |
| ui-ux-pro-max | https://github.com/nextlevelbuilder/ui-ux-pro-max-skill | MIT, "Copyright (c) 2024 Next Level Builder" | `d14efff` | Reject (screened) | A second design-direction catalogue (conflicts with taste); Google Fonts CDN imports (D-21); ships `test_*.py` files pytest would collect if placed in the tree |
| dembrandt-skills | https://github.com/dembrandt/dembrandt-skills | MIT, "Copyright (c) 2026 Dembrandt" | `c4ad177` | Reference only (screened) | Overlaps taste and `accessibility`; ships a `package.json` and scripts |
| platform-design-skills (`web`) | https://github.com/ehmo/platform-design-skills | MIT, no holder | `dc2be82` | Reference only (screened) | Sound web and WCAG 2.2 rules; the repository also redistributes Apple's `Apple_HIG.pdf`, whose terms are Apple's, not the repository's MIT |

### 6.2 Accessibility

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| accessibility | https://github.com/addyosmani/web-quality-skills | MIT, "Copyright (c) 2026 Addy Osmani" | `afa8da9` | **Adopt** | §3 |
| accessibility-review | https://github.com/anthropics/knowledge-work-plugins (`design`) | Apache-2.0, copyright line unfilled | `33d3dea` | Reject (inaccurate) | Audits to WCAG 2.1, not 2.2, and lists 2.5.5 (44 px, Level AAA) as an AA check |
| wcag-audit-patterns, screen-reader-testing | https://github.com/wshobson/agents | MIT, "Copyright (c) 2024 Seth Hobson" | `0874a27` | Reference only (screened) | Generic; the adopted skill is tighter |

### 6.3 Web performance

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| core-web-vitals, performance | https://github.com/addyosmani/web-quality-skills | MIT, "Copyright (c) 2026 Addy Osmani" | `afa8da9` | **Adopt** | §3 |
| web-quality-audit | same | same | `afa8da9` | Reference only | Umbrella over the others; its `analyze.sh` is read-only and needs `jq` |
| best-practices | same | same | `afa8da9` | Reference only | CSP, Trusted Types, SRI, headers; overlaps `security-review`; `npm audit` maps to our `pip-audit` |
| web-perf | https://github.com/cloudflare/skills | Apache-2.0, copyright line unfilled | `86505e4` | Reference only | Assumes Chrome DevTools MCP, installed with `npx -y chrome-devtools-mcp@latest` (unpinned) and an MCP config edit |

### 6.4 SEO and structured data

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| seo | https://github.com/addyosmani/web-quality-skills | MIT, "Copyright (c) 2026 Addy Osmani" | `afa8da9` | Reference only (strong; next for a slot) | Honest (no ranking promises, AI crawler controls by user agent); the site's SEO is already built and pinned by `web/test_seo.py`, and the skill has no Dataset markup, the one type a data site most needs |
| programmatic-seo, schema, seo-audit | https://github.com/coreyhaines31/marketingskills | MIT, "Copyright (c) 2025 Corey Haines" | `e85f3cb` | Reference only (screened) | Page-at-scale checks fit our record pages (unique value per page, noindex thin variations, sitemaps by page type). Marketing framing; lists scraped data as a source, which our guardrails forbid for aggregators |
| claude-seo | https://github.com/AgriciDaniel/claude-seo | MIT, "Copyright (c) 2026 agricidaniel" | `6be93f4` | **Reject (safety; screened)** | Scripts call third-party APIs (Moz, DataForSEO, Bing, Google, PageSpeed); installers and hooks |
| audit-website | https://github.com/squirrelscan/skills | MIT, "Copyright (c) 2025 squirrelscan" | `4595577` | Reject (screened) | Needs a third-party crawler CLI installed |
| seo-aeo-best-practices | https://github.com/sanity-io/agent-toolkit | MIT, "Copyright (c) 2025 Sanity" | `571a2d0` | Reference only (screened) | Written for the Sanity CMS |
| seo-audit | anthropics/knowledge-work-plugins (`marketing`) | Apache-2.0 | `33d3dea` | Reference only (screened) | Generic |

### 6.5 UX writing and copy

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| humanizer | https://github.com/blader/humanizer | MIT, "Copyright (c) 2025 Siqi Chen" | `8c307f3` | Reference only (screened) | Built on Wikipedia's "Signs of AI writing"; useful on long docs and social drafts. Taste `references/copy.md` and `docs/31-design-practices.md` §2.6 govern interface words |
| avoid-ai-writing | https://github.com/conorbronsdon/avoid-ai-writing | MIT, "Copyright (c) 2026 Conor Bronsdon" | `209815c` | Reject for vendoring (screened) | Its `bin/` scripts run `npx --package avoid-ai-writing-detector` (an npm install at run time) |
| unslop | https://github.com/MohamedAbdallah-14/unslop | MIT, "Copyright (c) 2026 Mohamed Abdallah" | `ad13500` | **Reject (guardrail; screened)** | An "anti-detector" mode rewrites text to defeat AI-text detectors and advises a second model pass for the same end. Our automated accounts are labelled as automated; we do not disguise machine-drafted text |
| ux-copy | anthropics/knowledge-work-plugins (`design`) | Apache-2.0 | `33d3dea` | Reference only | Generic; taste `copy.md` is stricter |

### 6.6 Data visualisation and maps

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| cartography-geoviz | https://github.com/muend/geoai-skills | MIT, "Copyright (c) 2026 Muhammed Enes Duran" | `19937ce` | Reference only (strong) | Maps as honest claims: normalise choropleths, fixed breaks across a series, NoData not zero, colour-blind checks, equal-area for area comparisons. Overlaps `docs/31-design-practices.md` §2.4; suggests a third-party basemap where we draw our own |
| postgis-spatial-sql | same | same | `19937ce` | Reference only (strong; next for a slot) | Correct on geometry versus geography, SRIDs, `ST_DWithin` over `ST_Distance <`, GiST, `ST_Subdivide`, validity. Spatial SQL is light here today (about a dozen non-test calls; most geometry runs in shapely) |
| geo-data-engineering | same | same | `19937ce` | Reference only (screened) | Has `scripts/clean_vector.py`, not read in full |
| mapbox-cartography, mapbox-data-visualization-patterns, mapbox-web-performance-patterns | https://github.com/mapbox/mapbox-agent-skills | MIT, "Copyright (c) 2026 Mapbox, Inc." | `f71cd72` | Reference only (screened) | Clustering, legend and layer performance patterns carry over to MapLibre. Vendor-centred (tokens, a skill that moves MapLibre users to Mapbox); its colour advice suggests red and green for emphasis, which `docs/31-design-practices.md` §2.3 rules out |
| data-storytelling, kpi-dashboard-design | wshobson/agents | MIT | `0874a27` | Reference only (screened) | `dataviz` and taste `app-screens.md` §5 to §6 cover this |

### 6.7 Browser and end-to-end testing

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| webapp-testing | https://github.com/anthropics/skills/tree/main/skills/webapp-testing | Apache-2.0, "Copyright 2026 Anthropic, PBC." | `dbd4588` | Reference only | Thin: waits on `networkidle` and fixed timeouts, which Playwright advises against. Its script starts local servers with `shell=True` and nothing else (it tells agents not to read it; we did). The repo's Python Playwright suites and bundled `run` cover this |
| playwright-skill | https://github.com/testdino-hq/playwright-skill | MIT, "Copyright (c) 2026 TestDino" | `178d1cb` | Reference only (screened) | The most thorough Playwright guidance found; its golden rules (role locators, no fixed waits, web-first assertions, isolated tests) carry over to our Python suites. TypeScript only and 2 MB; one guide's example sends test data to the publisher's API |
| playwright-skill | https://github.com/lackeyjb/playwright-skill | MIT, "Copyright (c) 2025 lackeyjb" | `dd47a6a` | **Reject (safety rule; screened)** | Installs Playwright and Chromium from the network on first run |
| agent-skills (playwright, pytest, api, accessibility, ...) | https://github.com/LambdaTest/agent-skills | MIT, "Copyright (c) 2025 TestMu AI / LambdaTest" | `992638b` | **Reject (safety; screened)** | `scaffold-project.sh` runs `npm install` and `npx playwright install --with-deps`; routes tests to the vendor's cloud grid with credentials |
| test-driven-development | obra/superpowers | MIT, "Copyright (c) 2025 Jesse Vincent" | `2ce8aa1` | Reference only (screened) | The repo's "a test that fails on the previous code" rule already carries the point |
| tdd | https://github.com/mattpocock/skills | MIT, "Copyright (c) 2026 Matt Pocock" | `5beb35b` | Reference only (screened) | As above |

### 6.8 Python, FastAPI and SQLAlchemy

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| supabase-postgres-best-practices | https://github.com/supabase/agent-skills | MIT, "Copyright (c) 2026 Supabase" | `c9be0e9` | **Adopt** | §3 |
| security-best-practices (FastAPI spec) | https://github.com/openai/skills (`skills/.curated/security-best-practices`) | Apache-2.0, copyright line unfilled | `49f948f` | Reference only (strong) | `references/python-fastapi-web-server-security.md` is the most specific FastAPI security spec found (cookie sessions and CSRF, template injection, Host header, proxy trust, SSRF, upload limits). The repository is deprecated and the skill is not in its successor (https://github.com/openai/plugins, `0722921`). It also says to hide API docs in production, where ours are the product (`docs/04` API-1), and writes its report to the repository root |
| fastapi-templates | wshobson/agents | MIT, "Copyright (c) 2024 Seth Hobson" | `0874a27` | Reject (fit) | Async SQLAlchemy throughout; this API uses synchronous `Session` with `def` routes |
| fastapi-expert | https://github.com/Jeffallan/claude-skills | MIT, no holder | `02b0502` | Reject (inaccurate) | `from pydantic import model_config` does not exist; uses python-jose; forbids synchronous database access outright |
| postgres-pro | same | same | `02b0502` | Reference only (screened) | The Supabase rules are tighter |
| python-testing-patterns, python-anti-patterns, postgresql-table-design, sql-optimization-patterns, database-migration | wshobson/agents | MIT | `0874a27` | Reference only (screened) | Generic |
| modern-python | https://github.com/trailofbits/skills | CC BY-SA 4.0 | `196a907` | Reference only (licence) | ShareAlike is outside the licences we adopt; it also moves projects to uv and ty and ships hooks, where we use pip, ruff and mypy |
| fastapi-router-py, pydantic-models-py | https://github.com/microsoft/skills | MIT, "Copyright (c) Microsoft Corporation." | `2130750` | Reference only (screened) | Part of an Azure SDK plugin |

### 6.9 Data engineering and data quality

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| validate-data | https://github.com/anthropics/knowledge-work-plugins (`data`) | Apache-2.0, copyright line unfilled | `33d3dea` | Reference only (strong; first in line for a slot) | The pre-delivery checklist for a figure: subtotals sum, the right denominator, partial periods, joins that multiply rows, two ways to the same number. It would have caught the front-page "US filed" count. Not vendored only for the cap; it also points at the plugin's `CONNECTORS.md` placeholders. Installable as Anthropic's `data` plugin |
| data-quality-frameworks | wshobson/agents | MIT | `0874a27` | Reference only (screened) | The quality dimensions table is useful; Great Expectations and dbt would be new dependencies |
| dbt-agent-skills | https://github.com/dbt-labs/dbt-agent-skills | Apache-2.0, "Copyright 2026 dbt Labs" | `b8facde` | Not applicable | No dbt here |

### 6.10 API design and OpenAPI

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| api-design-principles, openapi-spec-generation | wshobson/agents | MIT | `0874a27` | Reference only (screened) | Generic REST advice; `docs/23` and `docs/04` §5 are normative and stricter |
| api-skill suite | LambdaTest/agent-skills | MIT | `992638b` | Reject | As §6.7 |
| document-api-endpoint | getsentry/skills | Apache-2.0 | `d12f250` | Reference only (screened) | Written for Sentry's API |

### 6.11 Security

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| gha-security-review | https://github.com/getsentry/skills | Apache-2.0, "Copyright 2025 Functional Software, Inc. dba Sentry" | `d12f250` | **Adopt** | §3 |
| security-review | getsentry/skills | CC BY-SA 4.0 (OWASP-derived content) | `d12f250` | Reference only (licence) | ShareAlike; the bundled `security-review` covers the role |
| skill-scanner | getsentry/skills | Apache-2.0 | `d12f250` | Reference only (used in this lane) | §1. Its documented `uv run` installs PyYAML from PyPI; run it with the project interpreter |
| insecure-defaults, sharp-edges, supply-chain-risk-auditor, agentic-actions-auditor, differential-review | https://github.com/trailofbits/skills | CC BY-SA 4.0 | `196a907` | Reference only (licence) | Strong security work under a licence we do not adopt |
| claude-security | https://github.com/anthropics/claude-plugins-official | Apache-2.0, copyright line unfilled | `5826794` | Reference only (screened) | A plugin with its own agents and workflows, not a standalone skill; install as a plugin if wanted |
| security-threat-model | openai/skills | Apache-2.0 | `49f948f` | Reference only (screened) | Deprecated repository |
| cso, review, qa, benchmark and the rest of gstack | https://github.com/garrytan/gstack | MIT, "Copyright (c) 2026 Garry Tan" | `6f7488a` | **Reject (safety; screened)** | Runs binaries built and installed under `~/.claude/skills/gstack` (setup downloads a browser); usage telemetry (opt-in); its install text tells the agent to edit `CLAUDE.md` and commit |
| stride-analysis-patterns, threat-mitigation-mapping | wshobson/agents | MIT | `0874a27` | Reference only (screened) | Generic |

### 6.12 Legal and privacy (a checklist, never advice)

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| compliance-check | https://github.com/anthropics/knowledge-work-plugins (`legal`) | Apache-2.0, copyright line unfilled | `33d3dea` | Reference only | GDPR and CCPA request timelines, a DPA checklist (Article 28), with a "not legal advice" disclaimer. Use beside `docs/13` and the legal-compliance agent. The repository's root `LICENSE` has ten lines of unrelated text after the Apache terms; the plugin's own `LICENSE` is clean |
| legal-risk-assessment, vendor-check | same | same | `33d3dea` | Reference only (screened) | As above |
| gdpr-data-handling | wshobson/agents | MIT | `0874a27` | Reference only (screened) | Generic |
| privacy-policy | https://github.com/phuryn/pm-skills | MIT, "Copyright (c) 2026 Pawel Huryn" | `07b34f6` | Reject (guardrail) | Drafts privacy policies: legal drafting, which this project treats as counsel's work (`docs/13`) |

### 6.13 Product and PRD writing

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| write-spec | anthropics/knowledge-work-plugins (`product-management`) | Apache-2.0 | `33d3dea` | Reference only | A sound PRD shape (goals, non-goals, P0 to P2, given/when/then); `docs/10` already fixes ours |
| Product-Manager-Skills | https://github.com/deanpeters/Product-Manager-Skills | CC BY-NC-SA 4.0 | `cfcb697` | **Reject (licence)** | NonCommercial |
| create-prd, pre-mortem | phuryn/pm-skills | MIT | `07b34f6` | Reference only (screened) | As write-spec |
| grill-with-docs | mattpocock/skills | MIT | `5beb35b` | Reference only (screened) | Interview-style spec questioning |

### 6.14 Social and content

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| social | coreyhaines31/marketingskills | MIT | `e85f3cb` | Reference only (screened) | Platform limits and norms; `docs/32-social-operating-playbook.md` governs and a human sends |
| social-publishing | wshobson/agents | MIT | `0874a27` | **Reject (safety, guardrail)** | Publishes through a third party's API with a workspace key, after an npm install; our publishers are dry-run and a human sends |
| hermes-tweet | wshobson/agents | MIT | `0874a27` | **Reject (safety)** | Reads and acts on X through a third-party API after a plugin install |

### 6.15 Working practice and agent process

| Candidate | Source | Licence | Commit | Verdict | Reason |
|---|---|---|---|---|---|
| verification-before-completion | https://github.com/obra/superpowers | MIT, "Copyright (c) 2025 Jesse Vincent" | `2ce8aa1` | **Adopt** | §3 |
| systematic-debugging | same | same | `2ce8aa1` | Reference only (strong) | Root cause before fixes; three failed fixes means question the design. Links other Superpowers skills that are not here; `find-polluter.sh` runs `npm test`; the folder also holds the publisher's pressure-test prompts ("This is a real scenario. You must choose and act"), which should not load as working context |
| using-superpowers and its SessionStart hook | same | same | `2ce8aa1` | **Reject (instruction override)** | "IF A SKILL APPLIES TO YOUR TASK, YOU DO NOT HAVE A CHOICE. YOU MUST USE IT", injected at every session start, before any response |
| finishing-a-development-branch, using-git-worktrees | same | same | `2ce8aa1` | Reject (guardrail) | Merge, push and commit; agents here never commit |
| mcp-builder | https://github.com/anthropics/skills/tree/main/skills/mcp-builder | Apache-2.0, "Copyright 2026 Anthropic, PBC." | `dbd4588` | Reference only (screened) | For an MCP server over the public API, if one is built. `scripts/evaluation.py` calls a model API |
| doc-coauthoring | anthropics/skills | No licence file in the folder or the repository | `dbd4588` | Reference only (licence unclear) | |
| brand-guidelines | anthropics/skills | Apache-2.0 | `dbd4588` | Not applicable | Anthropic's own brand |

### 6.16 Lists used for discovery (not skills)

| List | Licence | Commit |
|---|---|---|
| https://github.com/VoltAgent/awesome-agent-skills | MIT, "Copyright (c) 2025 VoltAgent" | `f737f28` |
| https://github.com/travisvn/awesome-claude-skills | No licence file | `1da55aa` |
| https://github.com/ComposioHQ/awesome-claude-skills | No licence file | `be2a406` |
| https://github.com/hesreallyhim/awesome-claude-code | CC BY-NC-ND 4.0 | `3426cf1` |

## 7. Rejected for safety or a guardrail conflict

| Candidate | Issue |
|---|---|
| Vercel `web-design-guidelines`, `writing-guidelines` | Instructions fetched live from an unpinned URL at every run |
| `using-superpowers` and its SessionStart hook | Text that tells the agent to put skill invocation above everything else, injected at session start |
| `unslop` | A mode built to evade AI-text detection, against our disclosure rules for automated content |
| gstack | Global install of built binaries and a browser download; telemetry; install text that edits `CLAUDE.md` and commits |
| `claude-seo` | Scripts that send site data to third-party SEO APIs; installers; hooks |
| LambdaTest `agent-skills` | Package installs in a script; tests routed to a vendor cloud with credentials |
| `lackeyjb/playwright-skill` | Network install of Playwright and Chromium on first run |
| wshobson `social-publishing`, `hermes-tweet` | Posting and reading through third-party APIs with keys, against the human-sends rule |
| `avoid-ai-writing` (scripts only) | `npx` package install at run time; the markdown is readable |
| `phuryn privacy-policy` | Drafts legal documents |

Rejected for licence: `deanpeters/Product-Manager-Skills` (NonCommercial). Reference only for licence: Trail of
Bits and Sentry's `security-review` (ShareAlike), `doc-coauthoring` (none stated).

## 8. Repo skills to build with `skill-creator`

Each holds what the project already repeats by hand. None is built yet.

| Name | What it would hold |
|---|---|
| `onboard-source` | `docs/04` DA-14's eight steps as a run: read the terms and quote the operative clause with URL and date (`docs/13`); fill the `data/sources.yaml` row honestly (`reuse: unknown` until read); probe into `data/probes/`; connector skeleton under `pipeline/connectors/<source_id>/` with the `source_record_id` strategy, egress class and politeness limits; fixtures and `status_map.yaml`; three logged baseline runs and the DA-6 thresholds; resolution keys; the publication decision and credit string; the private-aggregator and PJM checks; the `docs/02` and `CHANGELOG` lines; and the tests every connector carries |
| `lane-run` | How a build, review or fix lane runs here: the brief's owned paths, worktree setup, before-and-after measurements, a regression test that fails on the previous code for every fix, the gates (`make lint`, `make typecheck`, `make test-core`, `make test-web`, taste `lint.mjs` and `inspect.mjs`, axe), the `CHANGELOG` line, saving the patch, and the hand-back report; review rounds report findings by severity with `file:line` |
| `integrate-lanes` | The coordinator's merge: apply lane patches in order, the files lanes collide on (`base.html`, `styles.css`, the sitemap, the menu tests), the combined suites, re-running every gate, recording integration fixes, the PLAN row, and commits that pass `scripts/check_commit_messages.py` |
| `api-contract-change` | `docs/23` first, then `api/openapi.yaml`, then code; the operation-inventory test; the attribution envelope (API-5), cursor pagination (API-2), the filter grammar (API-3), RFC 9457 errors (API-4), versioning and deprecation (API-7), `list_query` on every figure, the strict-YAML duplicate-key test |
| `figure-provenance` | Every published figure: the list that reproduces it, its as-of date, source and units; denominators, partial periods, no spliced series; the test that compares the figure with `include=count`; adapted from `validate-data` (§6.9) |
| `record-page-seo` | This site's patterns: canonical and `noindex` for record tabs, what the sitemap lists, the JSON-LD types in use (BreadcrumbList, ItemList, Organization with LEI, DefinedTermSet) and safe embedding (`web/page.py`), share-card rules, and schema.org `Dataset` and `DataCatalog` for the registers, which no external skill covers (Google's guide: https://developers.google.com/search/docs/appearance/structured-data/dataset) |
| `interface-review` | A `file:line` review of templates, CSS and `site.js` for interaction quality: the Web Interface Guidelines rules that fit this stack (forms, focus never covered, URL holds state, `…`, tabular figures, `Intl` formats, `translate="no"`), restated in sentence case with this file's conflicts resolved, plus the taste app-screen states (empty, zero, loading, error) and the htmx specifics (swaps keep focus, `aria-live` on swapped regions, history entries for filter changes) |
| `brand-dataviz` | Our palette in the bundled `dataviz` skill's `references/palette.md` format (surface and text roles, `--accent` as the one series, `--negative`, the five status families, the infrastructure colours, a sequential ramp from the accent), its validator's output for light and dark, and the chart rules in `docs/31-design-practices.md` §2.3 |
| `licence-gate-review` | A check for any new field, endpoint, export, feed, alert or log line: restricted and unknown sources invisible on every non-admin tier, PJM rows unpublished, `noncommercial` rows never in commercial use, nothing from a private project in public pages, matching, alerts, outreach, analytics or logs; the fixtures to extend |
| `resolution-eval` | For changes to matching: held-out labels, precision and recall at the threshold, review routing, reversible merge events (`docs/22`) |

## 9. Proposed `CLAUDE.md` section (not applied)

```markdown
## Skills
Vendored skills sit in `.claude/skills/`, each with a `VENDORED.md` (source, commit, licence);
`docs/43-skills-register.md` lists every candidate and verdict. Precedence: the guardrails and WCAG 2.2 AA first, then
the taste skill for anything visual, then the area's skill; a figure in `docs/04`, `docs/30` or `docs/31` beats a
skill's default. No skill authorises installing packages, fetching instructions from a URL, sending data out or
committing.
- Pages and screens: the taste skill (Design, above).
- Accessibility: `accessibility` for any template, component, form or map-control change and before every design gate.
- Speed: `core-web-vitals` (with `performance`) when a change touches the map, list or record pages, fonts, images or
  scripts; report lab medians of three with their conditions against D-13 and D-31.
- Charts, stat tiles, KPI rows: the bundled `dataviz` for every one, with `docs/31-brand-identity.md` §4 tokens in
  place of its placeholder palette (`--accent` the one data hue, `--negative` with its sign, the status families for
  lifecycle states), checked with its validator in light and dark.
- Maps: the taste app-screen rules and `docs/31-design-practices.md` §2.4; `dataviz` for the key.
- Database: `supabase-postgres-best-practices` before any table, index, migration or slow-query change; show
  `EXPLAIN (ANALYZE, BUFFERS)` and p50/p95, not the skill's impact figures.
- Python and the API: `docs/04` §3 and §5 and `docs/23`; bundled `code-review` and `simplify` on the diff.
- Security: bundled `security-review` for application code; `gha-security-review` for any change in
  `.github/workflows/`.
- Browser checks: bundled `run`; the repo's pytest-playwright suites for tests.
- New sources: `docs/04` DA-14, in order (until the `onboard-source` repo skill exists).
- Figures and counts: `verification-before-completion`, and the `validate-data` checklist named in
  `docs/43-skills-register.md`.
- Legal and privacy: the legal-compliance agent and `docs/13`; any outside checklist is a checklist, never advice.
- Specs, social: `docs/10`'s format; `docs/32-social-operating-playbook.md`, drafted by agents, sent by a human.
- Research, shared pages, new skills: bundled `deep-research`, `artifact-design`, `skill-creator` (vet by the
  register, §1).
- Every lane, before handing back: `verification-before-completion`.
```

## 10. Keeping it current

- **Update a vendored skill** by copying its upstream folder again at a new commit, reading every changed file,
  re-running the static scan, and recording the commit in its `VENDORED.md` and in §3. Never pull a skill from a
  branch at run time.
- **Review this register** when a lane hits a conflict with a vendored skill, when an upstream renames or
  deprecates one, and at least every three months (next: 2027-01-24).
- **Next in line** if the owner allows more than six: `validate-data` (§6.9), `seo` (§6.4),
  `postgis-spatial-sql` (§6.6), `systematic-debugging` (§6.15).
- **Follow-up for the coordinator:** run `gha-security-review` over the nine workflows once; this lane vendored it
  and did not review them.

## 11. Assumptions

- Claude Code discovers project skills at `.claude/skills/<name>/SKILL.md`, one level deep, which is why each
  vendored skill keeps its upstream folder name and why the two performance skills are separate folders.
- "Maintained" means an upstream commit within three months of 2026-10-24. All six adopted repositories had a
  commit between 2026-08-24 and 2026-10-23.
- A licence is as the repository states it at the commit given. Where a licence file's copyright line was left as
  the template, the table says "copyright line unfilled".
- The candidates are what the four lists and the official repositories lead to on 2026-10-24; skills published
  only on marketplaces with no repository were not considered.
- This file takes number 32 as its brief named it; `docs/32-social-operating-playbook.md` already uses 32. The
  coordinator may renumber.
