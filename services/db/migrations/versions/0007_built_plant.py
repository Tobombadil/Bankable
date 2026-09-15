"""Built-infrastructure context layer: `built_plant`.

One operating generating plant per row, drawn beneath the proposals map as context (docs/00-PLAN.md
decision 2026-09-14). Provenance quartet as on every stored record. Not run against a live
database here (no Postgres installed); the SQLite test target uses `init_db`.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _geography_point() -> Any:
    from geoalchemy2 import Geography

    return Geography(geometry_type="POINT", srid=4326)


def upgrade() -> None:
    op.create_table(
        "built_plant",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_plant_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("operator_name", sa.Text),
        sa.Column("technology", sa.Text),
        sa.Column("technology_raw", sa.Text),
        sa.Column("technologies", pg.JSONB, nullable=False, server_default="{}"),
        sa.Column("capacity_mw", sa.Numeric(12, 3)),
        sa.Column("generator_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("earliest_operating_year", sa.Integer),
        sa.Column("geom", _geography_point()),
        sa.Column("state_code", sa.Text),
        sa.Column("county_name", sa.Text),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("source_id", "source_plant_id", name="uq_built_plant_source_record"),
    )
    op.create_index("ix_built_plant_country_technology", "built_plant", ["country", "technology"])
    op.create_index("ix_built_plant_geom", "built_plant", ["geom"], postgresql_using="gist")


def downgrade() -> None:
    op.drop_index("ix_built_plant_geom", table_name="built_plant")
    op.drop_index("ix_built_plant_country_technology", table_name="built_plant")
    op.drop_table("built_plant")
