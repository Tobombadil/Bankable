"""Worker watermarks: the highest event.seq each scanning worker has examined.

Added while closing Sprint 3 item 4: without a stored watermark the social draft worker derived
its position from the posts it had written, so a backlog of events that earn no post (gated,
duplicate, ineligible) longer than one page would be rescanned on every tick and newer events
never reached. Not run against a live database here (no Postgres installed).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "worker_watermark",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("seq", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("worker_watermark")
