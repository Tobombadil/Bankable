"""`organization.parent_org_id` / `parent_source_id` (docs/21 §3.23, ADR 0008).

The GLEIF Level 2 direct accounting parent, where an LEI matches (the LEI itself lives in
`organization.ids["lei"]`, unchanged here). Plain nullable, self-referential FK columns on an
existing table — SQLite refuses a foreign-key-carrying `ADD COLUMN` outside batch mode ("No
support for ALTER of constraints in SQLite dialect", `alembic.ddl.sqlite`), so this uses
`op.batch_alter_table` the same way migration 0009 does, even though Postgres alone would have
allowed a plain `op.add_column`.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("organization", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column(
                "parent_org_id",
                GUID(),
                sa.ForeignKey("organization.id", name="fk_organization_parent_org_id_organization"),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "parent_source_id",
                sa.Text,
                sa.ForeignKey("source.id", name="fk_organization_parent_source_id_source"),
                nullable=True,
            )
        )
    op.create_index("ix_organization_parent_org_id", "organization", ["parent_org_id"])


def downgrade() -> None:
    op.drop_index("ix_organization_parent_org_id", table_name="organization")
    with op.batch_alter_table("organization", recreate="always") as batch_op:
        batch_op.drop_column("parent_source_id")
        batch_op.drop_column("parent_org_id")
