# Vendored: verification-before-completion (Superpowers)

- Source: https://github.com/obra/superpowers (folder `skills/verification-before-completion`), commit `2ce8aa1`
  (2026-10-21), copied 2026-10-24.
- Licence: MIT, copyright (c) 2025 Jesse Vincent. The upstream folder carries no licence file, so `LICENSE` here
  is the repository root's, copied unchanged.
- Unmodified apart from this file and `LICENSE`. Update by copying the upstream folder again, reading it, and
  recording the new commit here and in `docs/43-skills-register.md`.
- Adopted 2026-10-24 under the owner's direction of that day ("Find and use or reference any other skills that
  will help you across any and all aspects of the product"). Register: `docs/43-skills-register.md`.
- Used for: every lane's hand-back and the coordinator's acceptance. No claim that tests pass, that a figure holds,
  that a page meets a budget or that a bug is fixed without the command run in that session and its output read;
  an agent's "done" is checked against the diff. It is the general form of `docs/31-design-practices.md` §3 step 5
  and of the coordinator's duty in `CLAUDE.md` ("the coordinator verifies (tests, lint, spec)").
- Precedence: the guardrails in `CLAUDE.md`, then WCAG 2.2 AA, then the taste skill for anything visual, then this
  skill.
- Here, not upstream:
  - Agents never commit (`CLAUDE.md`). Read the skill's "before committing or creating PRs" as "before handing the
    lane back"; only the coordinator commits.
  - Only this skill was taken from the Superpowers plugin. Its `using-superpowers` skill and SessionStart hook were
    not, because they instruct the agent to invoke skills "before ANY response" over its other instructions
    (`docs/43-skills-register.md` §7).
