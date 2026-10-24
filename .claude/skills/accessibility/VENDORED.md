# Vendored: accessibility (Web Quality Skills)

- Source: https://github.com/addyosmani/web-quality-skills (folder `skills/accessibility`), commit `afa8da9`
  (2026-08-24), copied 2026-10-24.
- Licence: MIT, copyright (c) 2026 Addy Osmani. The upstream folder carries no licence file, so `LICENSE` here is
  the repository root's, copied unchanged.
- Unmodified apart from this file and `LICENSE`. Update by copying the upstream folder again, reading every file,
  and recording the new commit here and in `docs/43-skills-register.md`.
- Adopted 2026-10-24 under the owner's direction of that day ("Find and use or reference any other skills that
  will help you across any and all aspects of the product"). Every candidate and the reasons are in
  `docs/43-skills-register.md`.
- Used for: WCAG 2.2 AA audits and fixes on every page and screen, the map included (`docs/04` D-30). It adds an
  evidence-led loop (automated scan, accessibility-tree snapshot, keyboard pass, fix, re-run the same checks) and
  the 2.2 criteria agents tend to miss: 2.4.11 focus not obscured, 2.5.7 dragging, 2.5.8 target size, 3.2.6
  consistent help, 3.3.7 redundant entry, 3.3.8 accessible authentication.
- Precedence: the guardrails in `CLAUDE.md`, then WCAG 2.2 AA itself, then the taste skill for anything visual,
  then this skill.
- Here, not upstream:
  - Run axe through the paths the repo already has: the CI job "axe + Lighthouse CI" and `AXE_CORE_JS` in the web
    tests (`web/test_feedback.py`). Do not run the skill's `npm install -g @axe-core/cli` or an ad hoc
    `npx lighthouse`: no global or unpinned installs in a lane.
  - Target size: 24 x 24 CSS px is the AA floor (SC 2.5.8) and 44 px applies to primary touch targets
    (`docs/31-design-practices.md` §2.5). The skill's 44 px "comfortable" size is advice, not a gate.
  - Its "References" list links `../web-quality-audit/SKILL.md`, a sibling that was not vendored. Read it upstream
    at the commit above if needed.
