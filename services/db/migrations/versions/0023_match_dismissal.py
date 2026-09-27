"""`match_dismissal` -- per-user dismissal of a proposal <-> opportunity match (docs/10 US-402 AC2;
docs/21 §3.11 "per-user dismissals live in the supporting table `match_dismissal (user_id,
match_id, dismissed_at)` so that a personal action never mutates global data"; §4.4).

The `match` table itself has existed since 0001 (`services/db/models.py::Match`, docs/21 §3.11) but
had no writer until `services/match/run.py`; this migration adds the one supporting table the
dismiss/undismiss routes (`services/api/matches.py`) need. Unique on `(user_id, match_id)` so
`POST /v1/matches/{id}/dismiss` is idempotent by construction; indexed on `match_id` because the
list routes anti-join on it for every row of a page.

A fresh table needs no `batch_alter_table`: `services/db/types.GUID` creates identically on SQLite
and Postgres, matching `services/db/models.py::MatchDismissal` exactly.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    # Idempotent on a schema that already carries the table, as 0015-0021 are: the SQLite round-trip
    # tests build the *current* `Base.metadata` first and stamp an earlier revision, so upgrading
    # through this revision must not try to create what `create_all` already made.
    if _has_table("match_dismissal"):
        return
    op.create_table(
        "match_dismissal",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("user_id", GUID(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("match_id", GUID(), sa.ForeignKey("match.id"), nullable=False),
        sa.Column("dismissed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "match_id", name="uq_match_dismissal_user_match"),
    )
    op.create_index("ix_match_dismissal_match_id", "match_dismissal", ["match_id"])


def downgrade() -> None:
    if not _has_table("match_dismissal"):
        return
    op.drop_index("ix_match_dismissal_match_id", table_name="match_dismissal")
    op.drop_table("match_dismissal")
