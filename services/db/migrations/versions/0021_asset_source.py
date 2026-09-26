"""`asset_source` — a link table carrying per-source provenance for an `asset` row (docs/24 §5(a)
option (a); docs/21 §3.22a).

`asset` carries one `(source_id, source_asset_id, source_url, retrieved_at, licence_id)` quartet,
predating any notion of a second source (docs/24 §4). The first asset type to acquire a second
source, `ethanol_plant` (388 rows, 48.2% measured cross-source duplication, docs/24 §2.3), showed
the gap concretely: with no link table, a plant listed by both the EIA Energy Atlas and the annual
capacity report is stored twice and counted twice. This table is built to the `proposal_source`
pattern the doc names as already proven for exactly this shape (a record with more than one
source carries no provenance columns of its own; every source's quartet lives on its link row).

A fresh table needs no `batch_alter_table` (no existing rows, no dialect-specific ALTER):
`services/db/types.GUID` makes it create identically on SQLite and Postgres, matching
`services/db/models.py::AssetSource` exactly.

Landing this table does **not** by itself mean every multi-source asset type is resolved: only the
loader that actually writes matched rows here (`services/ingest/assets.py::load_ethanol_plants`,
`ethanol_plant` only at this migration) does that work. `services/api/coverage.py::asset_sources`
reads "resolved" per asset type from whether that type has any row in this table, not from the
table's mere existence, so landing the schema does not silently claim a fusion that has not run
(docs/24 §5(a): "the day that table lands the fact flips" describes what should follow a real
resolution pass, not the migration alone).

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported from `services.db.models` (0020's own convention): a migration
#: states the vocabulary it wrote, and the model may move on after it. Reuses `LINK_METHODS`
#: (`proposal_source.link_method`'s vocabulary) rather than inventing a parallel one: an asset's
#: primary source is `deterministic_key` (its own `(source_id, source_asset_id)`, no ambiguity);
#: a fused secondary source is `rule` (the scored match, `pipeline/context/ethanol_match.py`).
MATCH_METHODS = ("deterministic_key", "rule", "model", "user")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    # Idempotent on a schema that already carries the table, as 0015-0020 are: the SQLite round-trip
    # tests (`tests/test_migration_0012.py`, `services/db/test_migrations.py`) build the *current*
    # `Base.metadata` first and stamp an earlier revision, so upgrading through this revision must
    # not try to create what `create_all` already made.
    if _has_table("asset_source"):
        return
    op.create_table(
        "asset_source",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("asset_id", GUID(), sa.ForeignKey("asset.id"), nullable=False),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_record_id", sa.Text, nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("match_method", sa.Text, nullable=False, server_default="deterministic_key"),
        sa.Column("match_score", sa.Numeric(4, 3), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"match_method IN {MATCH_METHODS!r}", name="ck_asset_source_match_method_vocab"),
        sa.UniqueConstraint("source_id", "source_record_id", name="uq_asset_source_source_record"),
    )
    op.create_index("ix_asset_source_asset_id", "asset_source", ["asset_id"])


def downgrade() -> None:
    if not _has_table("asset_source"):
        return
    op.drop_index("ix_asset_source_asset_id", table_name="asset_source")
    op.drop_table("asset_source")
