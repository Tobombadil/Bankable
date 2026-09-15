"""Syndication pipeline: change event -> draft -> review queue -> publish -> metrics.

Scope (Sprint 2, per `.claude/agents/content-social.md` and `docs/32-social-operating-playbook.md`):
this package builds the pipeline **up to but not including** actually posting to any network. No
social accounts exist yet (the owner creates them per `docs/32` §2); every publisher in
`services.social.publishers` is dry-run only.

Modules:
    models      -- `PostDraft` / `Post` / review metadata (docs/21 §3.18).
    editorial   -- which events earn a post, per-channel templates, rendering, validation gates.
    queue       -- the review queue store, duplicate suppression, graduation checks, scheduling
                   stubs, cost estimates.
    publishers  -- an abstract `Publisher` plus three dry-run stubs (bluesky, linkedin, x).

Nothing in this package sends a message to a real person or platform (`CLAUDE.md` guardrails).
"""

from __future__ import annotations
