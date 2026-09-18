"""`built_plant` -> `asset` (ADR 0008, 2026-09-18): assets are first-class entities.

Renames `built_plant` to `asset` and widens it to docs/21 §3.22: `public_id`/`slug` (public
identity, backfilled below), `asset_type` (check constraint over the twelve ADR 0008 types,
existing rows become `power_plant`), `source_asset_id` (renamed from `source_plant_id`), `status`
(existing rows become `operating`), `capacity_value`/`capacity_unit` (the registry's native
capacity for non-electrical types), `commissioned_year` (renamed from `earliest_operating_year`),
`unit_count` (renamed from `generator_count`, now nullable per §3.22), `geom_line` (line geometry
for pipelines/transmission — text WKT on SQLite, `geography(LineString,4326)` on Postgres,
`services.db.types.GeographyLine`), `attributes` (objective feature set, default `{}`),
`county_fips`, and `first_seen`/`last_changed` (replacing the old `TimestampMixin`
`created_at`/`updated_at`, dropped here — `first_seen` backfilled from `created_at`,
`last_changed` from `updated_at`).

Also widens `location.precision`'s check constraint to accept `country_centroid` (ADR 0008's third
region-grade precision, docs/21 §3.7) — folded into this migration rather than a separate one
since it is one `CHECK` clause on a table this migration already touches structurally adjacent
code for (`services/db/models.py::LOCATION_PRECISIONS`).

Written with `op.batch_alter_table(..., recreate=_recreate())` throughout (not plain `op.add_column`/
`op.alter_column`) so the same code path renames columns, drops the old unique constraint/indexes
and adds the new check constraints on **both** SQLite (which cannot do most of this with a plain
`ALTER TABLE`) and Postgres (where `recreate=_recreate()` costs a table copy this migration only pays
once, in exchange for one tested code path instead of two). `public_id`/`slug` are added nullable
first, backfilled with `services.ids.public_id`/`slugify` (slug = name + state, de-duplicated with
a numeric suffix on collision, matching `services/ingest/loader.py`'s organisation-slug
convention), then tightened to `NOT NULL` + unique in a second batch pass.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-18
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from services.db.types import GeographyLine, JSONVariant
from services.ids import public_id as make_public_id
from services.ids import slugify

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ASSET_TYPES = (
    "power_plant",
    "gas_pipeline",
    "gas_processing_plant",
    "gas_storage",
    "lng_terminal",
    "compressor_station",
    "ethanol_plant",
    "biodiesel_plant",
    "rng_project",
    "transmission_line",
    "substation",
    "refinery",
)
ASSET_STATUSES = ("operating", "standby", "retired", "unknown")
OLD_LOCATION_PRECISIONS = ("exact", "county_centroid", "state_centroid", "unknown")
NEW_LOCATION_PRECISIONS = ("exact", "county_centroid", "state_centroid", "country_centroid", "unknown")


def _in_condition(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _now_default() -> Any:
    dialect = op.get_bind().dialect.name
    return sa.text("now()") if dialect == "postgresql" else sa.text("CURRENT_TIMESTAMP")


def _recreate() -> str:
    """SQLite cannot ALTER most of what these batches do, so the table is rebuilt there. Postgres
    can, and rebuilding it there trips geoalchemy2's `after_create` hook on the TypeDecorator-wrapped
    geometry column (CI, 2026-09-18: `AttributeError: 'Text' object has no attribute
    'spatial_index'`), so Postgres gets plain ALTER statements via `recreate="auto"`."""
    return "always" if op.get_bind().dialect.name == "sqlite" else "auto"


def upgrade() -> None:
    op.rename_table("built_plant", "asset")

    # ---- pass 1: add every new column (nullable where a backfill or default fills it), rename
    # the three columns whose meaning survives under a new name, and drop the constructs whose
    # names/definitions change. `recreate=_recreate()` so SQLite (no ALTER for most of this) and
    # Postgres run one identical code path.
    with op.batch_alter_table("asset", recreate=_recreate()) as batch_op:
        batch_op.add_column(sa.Column("public_id", sa.Text, nullable=True))
        batch_op.add_column(sa.Column("slug", sa.Text, nullable=True))
        batch_op.add_column(sa.Column("asset_type", sa.Text, nullable=False, server_default="power_plant"))
        batch_op.alter_column("source_plant_id", new_column_name="source_asset_id")
        batch_op.add_column(sa.Column("status", sa.Text, nullable=False, server_default="operating"))
        batch_op.add_column(sa.Column("capacity_value", sa.Numeric(14, 3), nullable=True))
        batch_op.add_column(sa.Column("capacity_unit", sa.Text, nullable=True))
        batch_op.alter_column("earliest_operating_year", new_column_name="commissioned_year")
        batch_op.alter_column(
            "generator_count", new_column_name="unit_count", nullable=True, server_default=None
        )
        batch_op.add_column(sa.Column("geom_line", GeographyLine(), nullable=True))
        batch_op.add_column(sa.Column("attributes", JSONVariant(), nullable=False, server_default="{}"))
        batch_op.add_column(sa.Column("county_fips", sa.String(5), nullable=True))
        batch_op.add_column(
            sa.Column(
                "first_seen", sa.TIMESTAMP(timezone=True), nullable=False, server_default=_now_default()
            )
        )
        batch_op.add_column(
            sa.Column(
                "last_changed",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=_now_default(),
            )
        )
        batch_op.drop_constraint("uq_built_plant_source_record", type_="unique")
        batch_op.drop_index("ix_built_plant_country_technology")
        batch_op.drop_index("ix_built_plant_geom")

    # ---- data migration: backfill first_seen/last_changed from the dropped TimestampMixin
    # columns, then public_id/slug (services.ids, per the task brief) before either is made
    # NOT NULL/unique below. Slugs are de-duplicated with a numeric suffix, mirroring
    # services/ingest/loader.py's organisation-slug collision handling.
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE asset SET first_seen = created_at, last_changed = updated_at"))

    rows = conn.execute(sa.text("SELECT id, name, state_code FROM asset")).fetchall()
    used_slugs: set[str] = set()
    for row_id, name, state_code in rows:
        as_uuid = row_id if isinstance(row_id, _uuid.UUID) else _uuid.UUID(str(row_id))
        pid = make_public_id("asset", as_uuid)
        base = slugify(f"{name} {state_code}" if state_code else str(name))
        slug = base
        suffix = 2
        while slug in used_slugs:
            slug = f"{base}-{suffix}"
            suffix += 1
        used_slugs.add(slug)
        conn.execute(
            sa.text("UPDATE asset SET public_id = :pid, slug = :slug WHERE id = :id"),
            {"pid": pid, "slug": slug, "id": row_id},
        )

    # ---- pass 2: tighten public_id/slug, drop the columns they replace, add the vocab checks
    # and the renamed unique constraint/indexes (docs/21 §3.22: "Unique: (source_id,
    # source_asset_id). Index: asset_type, state_code, geom").
    with op.batch_alter_table("asset", recreate=_recreate()) as batch_op:
        batch_op.alter_column("public_id", nullable=False)
        batch_op.alter_column("slug", nullable=False)
        batch_op.drop_column("created_at")
        batch_op.drop_column("updated_at")
        batch_op.create_unique_constraint("uq_asset_public_id", ["public_id"])
        batch_op.create_unique_constraint("uq_asset_slug", ["slug"])
        batch_op.create_unique_constraint("uq_asset_source_record", ["source_id", "source_asset_id"])
        batch_op.create_check_constraint("asset_type_vocab", _in_condition("asset_type", ASSET_TYPES))
        batch_op.create_check_constraint("status_vocab", _in_condition("status", ASSET_STATUSES))
        batch_op.create_index("ix_asset_asset_type", ["asset_type"])
        batch_op.create_index("ix_asset_state_code", ["state_code"])
        batch_op.create_index("ix_asset_geom", ["geom"], postgresql_using="gist")

    # ---- ADR 0008's third region-grade precision, folded in here (module docstring).
    with op.batch_alter_table("location", recreate=_recreate()) as batch_op:
        batch_op.drop_constraint("ck_location_precision_vocab", type_="check")
        batch_op.create_check_constraint(
            "precision_vocab", _in_condition("precision", NEW_LOCATION_PRECISIONS)
        )


def downgrade() -> None:
    with op.batch_alter_table("location", recreate=_recreate()) as batch_op:
        batch_op.drop_constraint("precision_vocab", type_="check")
        batch_op.create_check_constraint(
            "ck_location_precision_vocab", _in_condition("precision", OLD_LOCATION_PRECISIONS)
        )

    with op.batch_alter_table("asset", recreate=_recreate()) as batch_op:
        batch_op.drop_index("ix_asset_geom")
        batch_op.drop_index("ix_asset_state_code")
        batch_op.drop_index("ix_asset_asset_type")
        batch_op.drop_constraint("uq_asset_source_record", type_="unique")
        batch_op.drop_constraint("status_vocab", type_="check")
        batch_op.drop_constraint("asset_type_vocab", type_="check")
        batch_op.drop_constraint("uq_asset_slug", type_="unique")
        batch_op.drop_constraint("uq_asset_public_id", type_="unique")
        batch_op.add_column(
            sa.Column(
                "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=_now_default()
            )
        )
        batch_op.add_column(
            sa.Column(
                "updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=_now_default()
            )
        )

    conn = op.get_bind()
    conn.execute(sa.text("UPDATE asset SET created_at = first_seen, updated_at = last_changed"))

    # Two passes, not one (module note): a batch that both renames a column and adds a
    # constraint referencing the new name in the *same* `batch_alter_table` block silently drops
    # the constraint on SQLite (reproduced with a minimal `alter_column(new_column_name=...)` +
    # `create_unique_constraint` repro) — the same reason `upgrade()` above already splits into
    # two passes.
    with op.batch_alter_table("asset", recreate=_recreate()) as batch_op:
        batch_op.drop_column("last_changed")
        batch_op.drop_column("first_seen")
        batch_op.drop_column("county_fips")
        batch_op.drop_column("attributes")
        batch_op.drop_column("geom_line")
        batch_op.alter_column(
            "unit_count", new_column_name="generator_count", nullable=False, server_default="0"
        )
        batch_op.alter_column("commissioned_year", new_column_name="earliest_operating_year")
        batch_op.drop_column("capacity_unit")
        batch_op.drop_column("capacity_value")
        batch_op.drop_column("status")
        batch_op.alter_column("source_asset_id", new_column_name="source_plant_id")
        batch_op.drop_column("asset_type")
        batch_op.drop_column("slug")
        batch_op.drop_column("public_id")

    with op.batch_alter_table("asset", recreate=_recreate()) as batch_op:
        batch_op.create_unique_constraint("uq_built_plant_source_record", ["source_id", "source_plant_id"])
        batch_op.create_index("ix_built_plant_country_technology", ["country", "technology"])
        batch_op.create_index("ix_built_plant_geom", ["geom"], postgresql_using="gist")

    op.rename_table("asset", "built_plant")
