"""`organization.parent_as_of` — the date the parent link is stated as of (docs/21 §3.5, docs/22 §17).

Migration 0011 gave `organization` the parent edge (`parent_org_id`) and a provenance pointer
(`parent_source_id`). That is enough to say *where* a link came from and not enough to say *when*
it was true, which for an ownership graph is half the fact: GLEIF Level 2 publishes a relationship
period start for every consolidation record, and a company page that shows "parent: X" without a
date is a claim about today made from a file that may state 2013. `asset_owner` has carried `as_of`
since 0010 for exactly this reason; this column is its counterpart on the organisation.

Nullable, because the curated file (`curated.organization_parents`) states a page's
`retrieved_at`, not the date the ownership began, and a curated row therefore leaves it NULL rather
than asserting a start date nobody published.

A plain nullable `Date` column with no constraint, so no `batch_alter_table` is needed on SQLite
(0011 needed it because its columns carried foreign keys, which SQLite cannot ALTER in).

Both directions check the live column list first, the same way 0013 and 0014 guard theirs: a
database built by `Base.metadata.create_all` and then stamped at an earlier revision — which is how
`tests/test_migration_0012.py` and `services/db/test_migrations.py` exercise the chain on SQLite —
already has the column, and `ADD COLUMN` would fail on it.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COLUMN = "parent_as_of"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return COLUMN in {c["name"] for c in inspector.get_columns("organization")}


def upgrade() -> None:
    if not _has_column():
        op.add_column("organization", sa.Column(COLUMN, sa.Date(), nullable=True))


def downgrade() -> None:
    if _has_column():
        op.drop_column("organization", COLUMN)
