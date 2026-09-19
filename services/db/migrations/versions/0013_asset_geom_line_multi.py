"""`asset.geom_line`: `geography(LineString,4326)` -> `geography(MultiLineString,4326)`.

The midstream lane (2026-09-19) loads EIA Atlas pipelines dissolved to one `asset` row per
(operator, pipeline type) -- 259 rows from 32,961 segments -- and a dissolved pipeline is a
`MULTILINESTRING`, which PostGIS refuses to store in a LineString-typed geography column
("Geometry type (MultiLineString) does not match column type (LineString)"). Widening the column
keeps ADR 0008's "point plus optional line geometry" and docs/21 §3.22 as written; the Python-side
value becomes a WKT string on both dialects (`services/db/types.py::GeographyLine`).

On Postgres this is one `ALTER COLUMN ... TYPE` with `ST_Multi` for any existing LineString rows
(none exist at this migration: no line-typed asset had been loaded before it). On SQLite the
column is plain `TEXT` holding WKT, so there is nothing to alter: the migration is a no-op there
apart from being recorded in the version table, which is why it does not use `batch_alter_table`
at all (there is no batch to run). Downgrade reverses the type with `ST_LineMerge`, which turns
a single-part multilinestring back into a LineString; a genuinely multi-part row cannot be
represented in the old column and is set to NULL rather than mis-stored -- recorded in the
downgrade docstring, since a downgrade after pipelines have loaded loses their lines.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite: `geom_line` is TEXT (WKT) and already holds either shape
    op.execute(
        sa.text(
            "ALTER TABLE asset ALTER COLUMN geom_line TYPE geography(MultiLineString,4326) "
            "USING ST_Multi(geom_line::geometry)::geography"
        )
    )


def downgrade() -> None:
    """Multi-part rows cannot fit `geography(LineString)`; they are nulled, not merged into a
    false single line. Single-part rows survive via `ST_LineMerge`."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            "UPDATE asset SET geom_line = NULL WHERE geom_line IS NOT NULL "
            "AND ST_NumGeometries(geom_line::geometry) > 1"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE asset ALTER COLUMN geom_line TYPE geography(LineString,4326) "
            "USING ST_LineMerge(geom_line::geometry)::geography"
        )
    )
