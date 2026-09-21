"""`organization.parent_share_pct` — the stake the parent holds in the child, where a source states one.

Migration 0011 gave `organization` a boolean-shaped parent edge (`parent_org_id` plus
`parent_source_id`); 0015 added `parent_as_of` so a link can say *when* it was true. This column
adds the third thing an ownership graph needs and the one the model still could not express: *how
much*. `asset_owner.share_pct` has carried the same fact on the asset edge since 0010; this is its
counterpart on the organisation edge.

Nothing populates it today, and nothing here invents a value. The two loaded parent sources state
no percentage: GLEIF Level 2 publishes an accounting-consolidation relationship, which is a
control assertion rather than a stake, and the curated file
(`curated.organization_parents`) cites a company's own list of the systems it operates. The column
exists now because the ownership dataset most likely to land next (Global Energy Monitor, if its
licence clears) models ownership as chains of percentage stakes above a 5% threshold, and a
boolean-only edge would have to be torn up to hold it. Adding a nullable column that every read
path already ignores when absent is the cheap half of that change; the expensive half — a
percentage edge belonging to a *set* of parents rather than one — is deliberately not taken here,
because no loaded source has produced a second parent for any organisation (docs/21 §3.5).

`Numeric(6, 3)`: 0.000–100.000, matching `asset_owner.share_pct` exactly so the two edges compare
and serialise the same way. NULL means "no source stated a stake", never "zero" and never "100".

A plain nullable column with no foreign key, so no `batch_alter_table` is needed on SQLite (0011
needed it because its columns carried foreign keys, which SQLite cannot ALTER in); 0015 is the
precedent this follows line for line, including the live-column guard both directions, because a
database built by `Base.metadata.create_all` and then stamped at an earlier revision — how
`services/db/test_migrations.py` exercises the chain on SQLite — already has the column and
`ADD COLUMN` would fail on it.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COLUMN = "parent_share_pct"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return COLUMN in {c["name"] for c in inspector.get_columns("organization")}


def upgrade() -> None:
    if not _has_column():
        op.add_column("organization", sa.Column(COLUMN, sa.Numeric(6, 3), nullable=True))


def downgrade() -> None:
    if _has_column():
        op.drop_column("organization", COLUMN)
