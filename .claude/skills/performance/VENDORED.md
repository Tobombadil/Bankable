# Vendored: performance (Web Quality Skills)

- Source: https://github.com/addyosmani/web-quality-skills (folder `skills/performance`), commit `afa8da9`
  (2026-08-24), copied 2026-10-24.
- Licence: MIT, copyright (c) 2026 Addy Osmani. The upstream folder carries no licence file, so `LICENSE` here is
  the repository root's, copied unchanged.
- Unmodified apart from this file and `LICENSE`. Update together with the sibling `core-web-vitals` folder (same
  upstream, same commit), reading every file, and record the new commit here and in `docs/43-skills-register.md`.
- Adopted 2026-10-24 under the owner's direction of that day ("Find and use or reference any other skills that
  will help you across any and all aspects of the product"). Register: `docs/43-skills-register.md`.
- Used for: page-weight, loading and runtime work, and above all its measurement discipline
  (`references/MEASUREMENT.md`): state the URL, state, viewport, throttling and cache; take the median of three
  equivalent runs; keep field, lab and source findings apart. That is the method behind `docs/31-design-practices.md`
  §3 ("every number measured, never estimated") applied to speed.
- Precedence: the guardrails in `CLAUDE.md`, then WCAG 2.2 AA, then the taste skill for anything visual, then this
  skill.
- Here, not upstream:
  - Project budgets win, as the skill itself says ("Preserve an existing project budget"): `docs/04` D-31 (50 KB
    gzipped first-party JavaScript on non-map pages) and D-13 (the map page, re-based to 350 KB gzipped on
    2026-09-15 in `docs/00-PLAN.md`). Its starting budget table (300 KB JavaScript) does not apply.
  - Fonts stay self-hosted (D-21). Ignore its `fonts.googleapis.com` preconnect example.
  - Real-user monitoring (`references/RUM.md`) adds collection of visitor data: it needs the owner's approval and
    the minimum-personal-data rule in `CLAUDE.md`. The reference says the same.
  - Lighthouse runs in CI already ("axe + Lighthouse CI" in `.github/workflows/ci.yml`). In a lane, use a
    Lighthouse or Chromium that is already installed; do not install one.
