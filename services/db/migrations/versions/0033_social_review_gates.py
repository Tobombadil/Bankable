"""`post.fields_snapshot`, `post.gate_failures`, `channel_config.auto_publish_event_types`
(content audit 2026-09-30 F1, F5, F11; legal L-8).

What it does
------------
1. `post.fields_snapshot` (JSON, nullable): the structured fields a draft was rendered from, so a
   reviewer's edit is checked against the same facts the template used
   (`services/social/editorial.py::event_from_snapshot`). Before it, an edit could only be checked
   by substring, and every draft the worker wrote failed that check (F5).
2. `post.gate_failures` (JSON, nullable): the docs/32 §4.3 gates a draft fails. The worker keeps a
   failing draft for review instead of dropping it; before, the event was consumed and lost (F1).
   Such a draft cannot be approved until an edit clears every failure.
3. `channel_config.auto_publish_event_types` (JSON, nullable): the (channel, event type) pairs the
   owner enabled after each passed its docs/32 §4.6 graduation. Before, one switch turned on a whole
   channel, LinkedIn included, without consulting graduation (F11, L-8).

All three are nullable with no backfill: existing posts have no snapshot (their edits keep the
substring checks) and no recorded failures; an existing `auto_publish = true` row lists no event
type, so it auto-publishes nothing until the owner re-enables it through the graduation check.
Each step checks what is present first (0031's convention), because the SQLite round-trip tests
build the current `Base.metadata` and stamp an earlier revision.

Downgrade drops the three columns. Nothing in them is source data.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import JSONVariant

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS: tuple[tuple[str, str], ...] = (
    ("post", "fields_snapshot"),
    ("post", "gate_failures"),
    ("channel_config", "auto_publish_event_types"),
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for table, column in COLUMNS:
        if _has_table(table) and column not in _columns(table):
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column(column, JSONVariant(), nullable=True))


def downgrade() -> None:
    for table, column in reversed(COLUMNS):
        if _has_table(table) and column in _columns(table):
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column)
