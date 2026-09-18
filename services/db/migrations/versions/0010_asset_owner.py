"""`asset_owner` — ownership and operation edges (docs/21 §3.23, ADR 0008).

One row per asset/organisation/role/source edge (`share_pct` set only where the source states one
— EIA-860 Schedule 4; NULL for `operator` edges and share-less registries). A fresh table, so no
batch mode is needed — `services.db.types.GUID`/dialect-neutral column types make it create
identically on SQLite (this sprint's test target) and Postgres, matching
`services/db/models.py::AssetOwner` exactly.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ASSET_OWNER_ROLES = ("owner", "operator")


def upgrade() -> None:
    op.create_table(
        "asset_owner",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("asset_id", GUID(), sa.ForeignKey("asset.id"), nullable=False),
        sa.Column("organization_id", GUID(), sa.ForeignKey("organization.id"), nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("share_pct", sa.Numeric(6, 3), nullable=True),
        sa.Column("as_of", sa.Date, nullable=True),
        sa.Column("owner_name_raw", sa.Text, nullable=False),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.CheckConstraint(f"role IN {ASSET_OWNER_ROLES!r}", name="role_vocab"),
        sa.UniqueConstraint("asset_id", "organization_id", "role", "source_id", name="uq_asset_owner_edge"),
    )
    op.create_index("ix_asset_owner_asset_id", "asset_owner", ["asset_id"])
    op.create_index("ix_asset_owner_organization_id", "asset_owner", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_asset_owner_organization_id", table_name="asset_owner")
    op.drop_index("ix_asset_owner_asset_id", table_name="asset_owner")
    op.drop_table("asset_owner")
