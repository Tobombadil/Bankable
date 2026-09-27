"""`export` -- CSV export jobs and their per-user audit trail (docs/21 §4.4; US-603 AC1-AC3).

One row per export a Pro/API caller asks for through `POST /v1/exports` (`services/api/exports.py`),
kept whether it succeeded or not: the row is the per-user log metric M-6 reads (`user_id`,
`api_key_id`, `entity`, `query`, `row_count`, `tier`, `created_at`), and the daily quota of
docs/23 §6 ("5 exports / day") is a count over it. `query` is the filter definition in the
`SavedSearchQuery` grammar, never the result set; `object_key` is the generated file's path
relative to the export store (a local directory today, R2 later -- the column is the same either
way); `row_cap` is the plan's cap at the time of the request, so a later plan change never rewrites
what an old export was allowed to contain.

Mirrors `services/db/models.py::Export` exactly. A fresh table needs no `batch_alter_table`;
`services/db/types.GUID`/`JSONVariant` make it create identically on SQLite and Postgres, as
0021 does.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID, JSONVariant

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported from `services.db.models` (0020/0021's convention): a
#: migration states the vocabulary it wrote, and the model may move on after it.
EXPORT_STATUSES = ("queued", "running", "ready", "failed", "expired")
EXPORT_ENTITIES = ("proposal", "opportunity", "event")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    # Idempotent on a schema that already carries the table (0015-0021's convention): the SQLite
    # round-trip tests build the *current* `Base.metadata` first and stamp an earlier revision.
    if _has_table("export"):
        return
    op.create_table(
        "export",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("account_id", GUID(), sa.ForeignKey("account.id"), nullable=False),
        sa.Column("user_id", GUID(), sa.ForeignKey("user.id"), nullable=False),
        sa.Column("api_key_id", GUID(), sa.ForeignKey("api_key.id"), nullable=True),
        sa.Column("entity", sa.Text, nullable=False),
        sa.Column("query", JSONVariant(), nullable=False, server_default="{}"),
        sa.Column("tier", sa.Text, nullable=False, server_default="pro"),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("row_cap", sa.Integer, nullable=False),
        sa.Column("row_count", sa.Integer, nullable=True),
        sa.Column("truncated", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("object_key", sa.Text, nullable=True),
        sa.Column("byte_size", sa.BigInteger, nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(f"entity IN {EXPORT_ENTITIES!r}", name="ck_export_entity_vocab"),
        sa.CheckConstraint(f"status IN {EXPORT_STATUSES!r}", name="ck_export_status_vocab"),
        sa.CheckConstraint("row_cap > 0", name="ck_export_row_cap_positive"),
    )
    op.create_index("ix_export_user_created", "export", ["user_id", "created_at"])
    op.create_index("ix_export_account_id", "export", ["account_id"])


def downgrade() -> None:
    if not _has_table("export"):
        return
    op.drop_index("ix_export_account_id", table_name="export")
    op.drop_index("ix_export_user_created", table_name="export")
    op.drop_table("export")
