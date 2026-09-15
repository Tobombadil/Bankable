"""Identifier-free interaction counters: `ui_event`.

A row is an allowlisted name, a small JSON property bag and a time; never a user, session, IP,
user agent or referrer (docs/21 §3.21). Added so the context layer's engagement claim is measured
(docs/00-PLAN.md decision 2026-09-14). Not run against a live database here.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UI_EVENT_NAMES = (
    "map.layer_toggled",
    "map.region_jumped",
    "map.basemap_failed",
    "auth.registered",
    "alert.created",
)


def upgrade() -> None:
    op.create_table(
        "ui_event",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("props", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "occurred_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint(f"name IN {UI_EVENT_NAMES!r}", name="name_vocab"),
    )
    op.create_index("ix_ui_event_name_occurred_at", "ui_event", ["name", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_ui_event_name_occurred_at", table_name="ui_event")
    op.drop_table("ui_event")
