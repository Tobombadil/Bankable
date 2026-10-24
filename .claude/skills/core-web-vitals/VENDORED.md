# Vendored: core-web-vitals (Web Quality Skills)

- Source: https://github.com/addyosmani/web-quality-skills (folder `skills/core-web-vitals`), commit `afa8da9`
  (2026-08-24), copied 2026-10-24.
- Licence: MIT, copyright (c) 2026 Addy Osmani. The upstream folder carries no licence file, so `LICENSE` here is
  the repository root's, copied unchanged.
- Unmodified apart from this file and `LICENSE`. Update together with the sibling `performance` folder (same
  upstream, same commit), reading every file, and record the new commit here and in `docs/43-skills-register.md`.
- Adopted 2026-10-24 under the owner's direction of that day ("Find and use or reference any other skills that
  will help you across any and all aspects of the product"). Register: `docs/43-skills-register.md`.
- Used for: diagnosing and fixing LCP, INP and CLS on the map, list and record pages against `docs/04` D-31 (p75:
  LCP 2.5 s, INP 200 ms, CLS 0.1) and the map panel's own CLS budget of 0.02 (`docs/30` §11.7, S-8). It changes
  how an agent works in one way that matters here: it separates field data from lab traces from source
  hypotheses, and forbids claiming a metric fails or improves without runtime evidence.
- Precedence: the guardrails in `CLAUDE.md`, then WCAG 2.2 AA, then the taste skill for anything visual, then this
  skill. Project budgets in `docs/04` and `docs/31` win over any generic figure here.
- Here, not upstream:
  - Its measurement workflow lives in the sibling skill (`../performance/references/MEASUREMENT.md` and
    `RUM.md`), vendored with it for that reason.
  - The framework snippets (Next.js, React, Vue, Astro) do not apply: the site is server-rendered Jinja with htmx
    and MapLibre (`docs/20` §15).
  - Until launch there is no CrUX record for the site. Treat field data as unavailable, never as passing, and
    report lab medians of three runs with their conditions.
  - Speculation Rules fire page-load side effects early, including `page.viewed`; gate any analytics on
    `document.prerendering` before adding them.
