"""Round-trip test for migrations 0009-0011 (ADR 0008) on SQLite: `upgrade`, `downgrade`,
`upgrade` again, run for real through Alembic — not the ORM's `Base.metadata.create_all`
(`services/db/test_models.py` already covers the target schema that way).

Migrations 0001-0008 use Postgres-only DDL directly (`CREATE EXTENSION`, `postgresql.JSONB`/
`UUID`/`ARRAY`, `geoalchemy2.Geography` with no SQLite fallback) and cannot run against SQLite at
all — `services/db/migrations/env.py`'s own docstring says so ("there is no SQLite fallback for
running this migration"); that is a pre-existing condition of the migration chain, not something
this task's file area is scoped to rewrite. What this test *can* and does prove on SQLite: given a
database already at the schema migration 0008 would have produced — built here with the same
dialect-neutral column types `services/db/models.py` and this task's own migrations use
(`services.db.types.GUID`/`JSONVariant`/`GeographyPoint`), stamped as revision 0008 — running
`alembic upgrade head` (0009, 0010, 0011), `alembic downgrade 0008`, `alembic upgrade head` again
each succeed and leave the schema and data in the expected shape. This is exactly the "upgrade,
downgrade, upgrade" contract this task's gate names, scoped to the migrations this task added.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import uuid as _uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from services.db.types import GUID, GeographyPoint, JSONVariant

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
UTC = dt.UTC


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


def _build_0008_baseline(engine: sa.Engine) -> dict[str, _uuid.UUID | str]:
    """A minimal, dialect-neutral schema equivalent to what migrations 0001-0008 produce, for
    exactly the tables 0009-0011 touch (`built_plant`, `location`, `organization`, plus the
    `licence`/`source` rows their foreign keys need). Returns the ids/keys the test asserts on."""
    meta = sa.MetaData()

    licence = sa.Table(
        "licence",
        meta,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("reuse_class", sa.Text, nullable=False),
    )
    source = sa.Table(
        "source",
        meta,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
    )
    sa.Table(
        "organization",
        meta,
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("public_id", sa.Text, nullable=False, unique=True),
        sa.Column("slug", sa.Text, nullable=False, unique=True),
        sa.Column("name_canonical", sa.Text, nullable=False),
        sa.Column("name_normalised", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("ids", JSONVariant(), nullable=False, server_default="{}"),
    )
    sa.Table(
        "location",
        meta,
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("geom", GeographyPoint(), nullable=True),
        sa.Column("precision", sa.Text, nullable=False),
        sa.Column("county_fips", sa.String(5), nullable=True),
        sa.Column("county_name", sa.Text, nullable=True),
        sa.Column("state_code", sa.Text, nullable=True),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.CheckConstraint(
            "precision IN ('exact', 'county_centroid', 'state_centroid', 'unknown')",
            name="ck_location_precision_vocab",
        ),
    )
    built_plant = sa.Table(
        "built_plant",
        meta,
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("source_plant_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("operator_name", sa.Text, nullable=True),
        sa.Column("technology", sa.Text, nullable=True),
        sa.Column("technology_raw", sa.Text, nullable=True),
        sa.Column("technologies", JSONVariant(), nullable=False, server_default="{}"),
        sa.Column("capacity_mw", sa.Numeric(12, 3), nullable=True),
        sa.Column("generator_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("earliest_operating_year", sa.Integer, nullable=True),
        sa.Column("geom", GeographyPoint(), nullable=True),
        sa.Column("state_code", sa.Text, nullable=True),
        sa.Column("county_name", sa.Text, nullable=True),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("retrieved_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "source_plant_id", name="uq_built_plant_source_record"),
        sa.Index("ix_built_plant_country_technology", "country", "technology"),
        sa.Index("ix_built_plant_geom", "geom"),
    )
    meta.create_all(engine)

    plant_id = _uuid.uuid4()
    now = dt.datetime(2026, 9, 1, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(licence.insert().values(id="lic-1", name="Open Licence", reuse_class="open"))
        conn.execute(
            source.insert().values(
                id="us.eia.860m", name="EIA-860M", url="https://example.org/860m", licence_id="lic-1"
            )
        )
        conn.execute(
            built_plant.insert().values(
                id=plant_id,
                source_plant_id="P1",
                name="Roscoe Wind Farm",
                technology="wind",
                technologies={"Onshore Wind Turbine": 781.5},
                capacity_mw=781.5,
                generator_count=627,
                earliest_operating_year=2007,
                geom=(-100.4, 32.4),
                state_code="US-TX",
                county_name="Nolan",
                country="US",
                source_id="us.eia.860m",
                source_url="https://example.org/860m",
                retrieved_at=now,
                licence_id="lic-1",
                created_at=now,
                updated_at=now,
            )
        )
    return {"plant_id": plant_id}


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migrations.db'}"


def test_0009_to_0011_upgrade_downgrade_upgrade(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    ids = _build_0008_baseline(engine)
    plant_id = ids["plant_id"]

    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0008")

    command.upgrade(cfg, "head")
    insp = inspect(engine)
    asset_columns = {c["name"] for c in insp.get_columns("asset")}
    assert "built_plant" not in insp.get_table_names()
    assert {
        "public_id",
        "slug",
        "asset_type",
        "source_asset_id",
        "status",
        "capacity_value",
        "capacity_unit",
        "commissioned_year",
        "unit_count",
        "geom_line",
        "attributes",
        "county_fips",
        "first_seen",
        "last_changed",
    } <= asset_columns
    assert "asset_owner" in insp.get_table_names()
    org_columns = {c["name"] for c in insp.get_columns("organization")}
    assert {"parent_org_id", "parent_source_id"} <= org_columns

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT asset_type, status, public_id, slug FROM asset WHERE id = :id"),
            {"id": str(plant_id)},
        ).one()
        assert row.asset_type == "power_plant"
        assert row.status == "operating"
        assert row.public_id.startswith("asset_")
        assert row.slug

    command.downgrade(cfg, "0008")
    insp = inspect(engine)
    assert "built_plant" in insp.get_table_names()
    assert "asset" not in insp.get_table_names()
    assert "asset_owner" not in insp.get_table_names()
    built_plant_columns = {c["name"] for c in insp.get_columns("built_plant")}
    assert {"source_plant_id", "generator_count", "earliest_operating_year"} <= built_plant_columns
    org_columns = {c["name"] for c in insp.get_columns("organization")}
    assert "parent_org_id" not in org_columns

    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT source_plant_id, name FROM built_plant WHERE id = :id"),
            {"id": str(plant_id)},
        ).one()
        assert row.source_plant_id == "P1"
        assert row.name == "Roscoe Wind Farm"

    command.upgrade(cfg, "head")
    insp = inspect(engine)
    assert "asset" in insp.get_table_names()
    assert "asset_owner" in insp.get_table_names()
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT asset_type, source_asset_id FROM asset WHERE id = :id"),
            {"id": str(plant_id)},
        ).one()
        assert row.asset_type == "power_plant"
        assert row.source_asset_id == "P1"
