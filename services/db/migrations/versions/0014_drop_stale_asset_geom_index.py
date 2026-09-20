"""Drop the stale `idx_built_plant_geom` GiST index left on `asset` by the 0009 rename.

Migration 0007 created `built_plant` with a `geoalchemy2.Geography` column. GeoAlchemy2 adds its
own spatial index for such a column through an `after_create` hook, named by its convention
`idx_<table>_<column>` — so the table was created with **two** GiST indexes over `geom`:
`idx_built_plant_geom` (implicit, from geoalchemy2) and `ix_built_plant_geom` (explicit, line 59
of 0007). Migration 0009 renamed the table to `asset`, dropped the explicit index by name and
created `ix_asset_geom` in its place; the implicit one was never named anywhere, so it survived
the rename under its original name and Postgres has been maintaining two identical GiST indexes
over `asset.geom` ever since — twice the write cost and twice the disk for one access path.

Confirmed on the CI PostGIS at revision 0013 before this migration was written::

    indexname            | indexdef
    ---------------------+--------------------------------------------------------------------
    idx_built_plant_geom | CREATE INDEX idx_built_plant_geom ON public.asset USING gist (geom)
    ix_asset_geom        | CREATE INDEX ix_asset_geom ON public.asset USING gist (geom)

`ix_asset_geom` is the one `services/db/models.py::Asset.__table_args__` declares and the one
docs/21 §3.22 names, so it is the one that stays.

Postgres only, and `IF EXISTS`/`IF NOT EXISTS` on both directions: on SQLite `geom` is TEXT
(`services.db.types.GeographyPoint`), geoalchemy2 never ran, and no such index exists — the
migration is a no-op there apart from the version-table row, the same way 0013 handles the two
dialects. `IF EXISTS` also covers a Postgres database built after this migration landed, or one
whose `built_plant` predates the geoalchemy2 hook, where the stale index is already absent.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STALE_INDEX = "idx_built_plant_geom"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite: `geom` is TEXT and carries no spatial index to drop
    op.execute(sa.text(f"DROP INDEX IF EXISTS {STALE_INDEX}"))


def downgrade() -> None:
    """Recreates the duplicate exactly as geoalchemy2 left it, so `downgrade` restores the schema
    this migration found rather than a tidier one."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {STALE_INDEX} ON asset USING gist (geom)"))
