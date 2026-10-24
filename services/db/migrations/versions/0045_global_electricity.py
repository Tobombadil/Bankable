"""Global electricity figures: Ember's operating capacity, generation and shares (docs/26-global-figures.md).

Two tables, each value carrying `source_id`/`licence_id` (the visibility predicate's keys) and its
`source_url`/`retrieved_at`:

* `global_electricity_load` — one row per dataset (`yearly`, `monthly`, `monthly_capacity`) and route
  (`api`, `file`) currently served, unique on the pair: a new load replaces the old in one transaction.
* `global_electricity_value` — that load's values, one per (area, period, source, metric).

Operating figures, never proposals: no `event` rows, nothing linked to a record. The migration writes no
rows; `services/global_figures/load.py` does.

Revision: 0045 (lane global-ember). `down_revision` is 0043 (data rooms), the head on this lane's base.
Lane front-api numbers its migration 0044 from the same head; whichever of the two lands second
re-parents onto the other, keeping one head (as 0039 and 0041 did). Guards follow 0041: a SQLite store
built by `create_all` already has the tables and is stamped. Downgrade drops both tables; every row is
re-derivable from the normalised runs in the connector store.

Revision ID: 0045
Revises: 0043
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import JSONVariant

revision: str = "0045"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported (0020-0041's convention).
DATASETS = ("yearly", "monthly", "monthly_capacity")
ORIGINS = ("api", "file")
AREA_KINDS = ("world", "region", "country")
METRICS = ("capacity_gw", "generation_twh", "share_of_generation_pct")
TABLES = ("global_electricity_value", "global_electricity_load")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _provenance() -> list[sa.Column]:
    return [
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", sa.TIMESTAMP(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    if not _has_table("global_electricity_load"):
        op.create_table(
            "global_electricity_load",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("dataset", sa.Text, nullable=False),
            sa.Column("origin", sa.Text, nullable=False),
            sa.Column("run_ts", sa.Text, nullable=False),
            sa.Column("run_id", sa.Text, nullable=True),
            sa.Column("metrics", sa.Text, nullable=False),
            sa.Column("rows", sa.Integer, nullable=False),
            sa.Column("areas", sa.Integer, nullable=False),
            sa.Column("first_period", sa.Text, nullable=True),
            sa.Column("last_period", sa.Text, nullable=True),
            sa.Column("countries_by_year", JSONVariant(), nullable=False),
            *_provenance(),
            sa.Column("loaded_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                _in_list("dataset", DATASETS), name="ck_global_electricity_load_dataset_vocab"
            ),
            sa.CheckConstraint(_in_list("origin", ORIGINS), name="ck_global_electricity_load_origin_vocab"),
            sa.UniqueConstraint("dataset", "origin", name="uq_global_electricity_load_dataset_origin"),
        )
    if not _has_table("global_electricity_value"):
        op.create_table(
            "global_electricity_value",
            sa.Column(
                "load_id",
                sa.Text,
                sa.ForeignKey("global_electricity_load.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("area_id", sa.Text, primary_key=True),
            sa.Column("period", sa.Text, primary_key=True),
            sa.Column("source", sa.Text, primary_key=True),
            sa.Column("metric", sa.Text, primary_key=True),
            sa.Column("dataset", sa.Text, nullable=False),
            sa.Column("area_name", sa.Text, nullable=False),
            sa.Column("area_kind", sa.Text, nullable=False),
            sa.Column("iso3", sa.Text, nullable=True),
            sa.Column("year", sa.Integer, nullable=False),
            sa.Column("month", sa.Integer, nullable=True),
            sa.Column("source_label", sa.Text, nullable=False),
            sa.Column("is_aggregate", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("value", sa.Float, nullable=False),
            sa.Column("unit", sa.Text, nullable=False),
            *_provenance(),
            sa.CheckConstraint(
                _in_list("area_kind", AREA_KINDS), name="ck_global_electricity_value_area_kind_vocab"
            ),
            sa.CheckConstraint(_in_list("metric", METRICS), name="ck_global_electricity_value_metric_vocab"),
        )
        op.create_index(
            "ix_global_electricity_value_area",
            "global_electricity_value",
            ["load_id", "area_id", "metric", "year"],
        )
        op.create_index(
            "ix_global_electricity_value_kind_year",
            "global_electricity_value",
            ["load_id", "area_kind", "metric", "year"],
        )


def downgrade() -> None:
    for table in TABLES:
        if _has_table(table):
            op.drop_table(table)
