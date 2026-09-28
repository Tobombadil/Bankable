"""`interconnection_point`, and `proposal.interconnection_point_id` (owner decision 2026-09-28).

Every US ISO queue row names its point of interconnection only inside the raw source record
(`Interconnection Location`; ERCOT 1,778 rows, CAISO 2,278, NYISO 1,804 measured 2026-09-27) and
the NESO TEC register names it as `Connection Site` (2,198). Nothing surfaced it. This revision
makes it a first-class record (docs/21 §3.24): one row per `(source_id, name_key)` -- one
register's name for one bus or line tap -- carrying the provenance quartet of that register, and a
nullable FK from `proposal`. A point is not a `location`: a project connects at a substation, it
is not built there (docs/21 §3.7).

No aggregate column is stored (queued MW, counts): they depend on the caller's tier and are
computed per request over visible proposals (`services/api/interconnection_points.py`), so a
stored total could never leak a hidden proposal's capacity. `substation_asset_id` is created
here, NULL everywhere, for lane G2's substation crosswalk to fill.

The table is filled by the loader (`services/ingest/interconnection.py::link_source_points`) on
every proposal load, and for a store loaded before this revision by
`python -m services.ingest.interconnection`. This migration writes no rows: parsing the raw text is
application code that will keep evolving (`KEY_RULE_VERSION`), and a migration that froze one
version of it would diverge from the loader the next time the rule changed.

Guards follow 0021/0025: the SQLite round-trip tests build the current `Base.metadata` (which
already has the table and the column) and stamp an earlier revision, so every step checks what is
present. The column is added with `batch_alter_table` so SQLite recreates `proposal` with the FK.

Downgrade drops the FK column, its index, and the table. It does not refuse: every point is
re-derivable from `proposal_source.raw` by re-running the backfill after an upgrade, so nothing is
lost that the store does not still hold.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "interconnection_point"
FK_COLUMN = "interconnection_point_id"
FK_NAME = "fk_proposal_interconnection_point_id_interconnection_point"
PROPOSAL_INDEX = "ix_proposal_interconnection_point_id"
#: Frozen here rather than imported from `services.db.models` (0020-0025's convention).
POINT_KINDS = ("substation", "line_tap", "unknown")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _proposal_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("proposal")}


def _proposal_indexes() -> set[str]:
    return {str(i["name"]) for i in sa.inspect(op.get_bind()).get_indexes("proposal") if i.get("name")}


def _fk_name() -> str | None:
    for fk in sa.inspect(op.get_bind()).get_foreign_keys("proposal"):
        if fk.get("constrained_columns") == [FK_COLUMN]:
            return str(fk["name"]) if fk.get("name") else None
    return None


def upgrade() -> None:
    if not _has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("public_id", sa.Text, nullable=False),
            sa.Column("operator", sa.Text, nullable=False),
            sa.Column("name_display", sa.Text, nullable=False),
            sa.Column("name_key", sa.Text, nullable=False),
            sa.Column("key_rule", sa.Text, nullable=False),
            sa.Column("voltage_kv", sa.Numeric(8, 3), nullable=True),
            sa.Column("bus_number", sa.Text, nullable=True),
            sa.Column("kind", sa.Text, nullable=False, server_default="unknown"),
            sa.Column("jurisdiction", sa.Text, nullable=True),
            sa.Column("substation_asset_id", GUID(), sa.ForeignKey("asset.id"), nullable=True),
            sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
            sa.Column("source_url", sa.Text, nullable=False),
            sa.Column("retrieved_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
            sa.Column(
                "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.CheckConstraint(f"kind IN {POINT_KINDS!r}", name="ck_interconnection_point_kind_vocab"),
            sa.UniqueConstraint("public_id", name="uq_interconnection_point_public_id"),
            sa.UniqueConstraint("source_id", "name_key", name="uq_interconnection_point_source_key"),
        )
        op.create_index("ix_interconnection_point_operator", TABLE, ["operator"])
        op.create_index("ix_interconnection_point_jurisdiction", TABLE, ["jurisdiction"])
    if not _has_table("proposal"):
        return  # the reduced baselines of services/db/test_migrations.py carry no `proposal`
    if FK_COLUMN not in _proposal_columns():
        with op.batch_alter_table("proposal") as batch:
            batch.add_column(sa.Column(FK_COLUMN, GUID(), nullable=True))
            batch.create_foreign_key(FK_NAME, TABLE, [FK_COLUMN], ["id"])
    if PROPOSAL_INDEX not in _proposal_indexes():
        op.create_index(PROPOSAL_INDEX, "proposal", [FK_COLUMN])


def downgrade() -> None:
    if _has_table("proposal"):
        _downgrade_proposal()
    if _has_table(TABLE):
        op.drop_index("ix_interconnection_point_jurisdiction", table_name=TABLE)
        op.drop_index("ix_interconnection_point_operator", table_name=TABLE)
        op.drop_table(TABLE)


def _downgrade_proposal() -> None:
    if PROPOSAL_INDEX in _proposal_indexes():
        op.drop_index(PROPOSAL_INDEX, table_name="proposal")
    if FK_COLUMN in _proposal_columns():
        fk = _fk_name()
        with op.batch_alter_table("proposal") as batch:
            if fk is not None:
                batch.drop_constraint(fk, type_="foreignkey")
            batch.drop_column(FK_COLUMN)
