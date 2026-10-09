"""Round-trip for migration 0035 (list-sort, paid-tier timing and data-version indexes; backend
audit 2026-10-07, PERF-3, PERF-2, PERF-12).

0028's shape: the store starts at the schema `Base.metadata` produces (which declares every index),
is stamped `0035`, and is walked down and up again. The Postgres half (`NULLS LAST` on the capacity
index) is CI's `migrations` job.
"""

from __future__ import annotations

import os
import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from services.db.base import Base

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"

NEW = {
    "proposal": {
        "ix_proposal_publish_published_at",
        "ix_proposal_public_last_changed",
        "ix_proposal_public_capacity_mw",
        "ix_proposal_public_first_seen",
        "ix_proposal_public_name_canonical",
        "ix_proposal_updated_at",
    },
    "opportunity": {"ix_opportunity_publish_published_at"},
    "event": {"ix_event_published_at"},
}
#: Created by earlier migrations and now declared on the models too (PERF-12).
DECLARED_SINCE = {
    "asset_owner": {"ix_asset_owner_asset_id", "ix_asset_owner_organization_id"},
    "organization": {"ix_organization_parent_org_id"},
    "location": {"ix_location_geom"},
}


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0035.db'}"


def _indexes(engine: sa.Engine, table: str) -> dict[str, dict[str, object]]:
    return {str(i["name"]): dict(i) for i in sa.inspect(engine).get_indexes(table)}


def test_0035_round_trip(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    for table, names in {**NEW, **DECLARED_SINCE}.items():
        assert names <= set(_indexes(engine, table)), table
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0035")

    command.downgrade(cfg, "-1")
    for table, names in NEW.items():
        assert not names & set(_indexes(engine, table)), table

    command.upgrade(cfg, "0035")
    for table, names in NEW.items():
        assert names <= set(_indexes(engine, table)), table
    with engine.connect() as conn:
        sql = conn.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE name = 'ix_proposal_public_last_changed'")
        ).scalar()
    assert "last_changed DESC" in sql and "publish_state = 'public'" in sql


def test_0035_is_a_no_op_on_a_store_the_orm_already_built(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0034")
    before = {t: set(_indexes(engine, t)) for t in NEW}
    command.upgrade(cfg, "0035")
    assert {t: set(_indexes(engine, t)) for t in NEW} == before
