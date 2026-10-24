# Vendored: supabase-postgres-best-practices

- Source: https://github.com/supabase/agent-skills (folder `skills/supabase-postgres-best-practices`), commit
  `c9be0e9` (2026-10-02), copied 2026-10-24.
- Licence: MIT, copyright (c) 2026 Supabase. The upstream folder carries no licence file, so `LICENSE` here is the
  repository root's, copied unchanged.
- Unmodified apart from this file and `LICENSE`. Update by copying the upstream folder again, reading every file,
  and recording the new commit here and in `docs/43-skills-register.md`. The frontmatter says version 1.1.1 while
  `CHANGELOG.md` reaches 1.6.0; the commit above is the pin.
- Adopted 2026-10-24 under the owner's direction of that day ("Find and use or reference any other skills that
  will help you across any and all aspects of the product"). Register: `docs/43-skills-register.md`.
- Used for: tables, columns, indexes, migrations, queries and connection settings in the Postgres 16 and PostGIS
  store (`services/db`, `services/api`, `docs/21`). One rule per file, each with the wrong and the right SQL.
  The rules that have already cost this project time: index foreign-key and join columns (the 21.84 s list page
  of 2026-09-13 was one missing index on the source-link tables), keyset pagination (`docs/04` API-2), short
  transactions, and reading `EXPLAIN (ANALYZE, BUFFERS)` before changing a query.
- Precedence: the guardrails in `CLAUDE.md`, then WCAG 2.2 AA, then the taste skill for anything visual, then this
  skill. `docs/21`, `docs/23` and the ADRs are the project's own decisions and win over a generic rule here.
- Here, not upstream:
  - The impact figures on each rule ("10-100x") are the upstream authors' heuristics, not measurements. Report
    this store's plans and p50/p95 timings, as the lanes already do.
  - The Row-Level Security rules are written for Supabase. This store enforces tiers and licence gating in the API
    (`docs/20` §5, `docs/21` §8); adopting RLS would be an ADR, not a consequence of this skill.
  - Tests run on SQLite through dialect-aware types (`services/db/types.py`). Postgres-only DDL (`INCLUDE`,
    `DO` blocks, extensions) belongs in the canonical migration with the dialect handling the migrations
    already use.
  - Postgres settings (`max_connections`, `work_mem`, timeouts) are infrastructure changes under `docs/60`, not a
    lane's edit.
