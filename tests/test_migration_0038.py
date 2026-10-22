"""Round trip for migration 0038 (`organization_stats`, the companies index's figures).

0037's shape: the store starts at the schema `Base.metadata` produces (which declares the table), is
stamped `0038`, walked down (the table goes) and up again (it returns with its key and vocabularies),
and the ORM model agrees with what the migration created, column for column.
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
TABLE = "organization_stats"


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0038.db'}"


def test_0038_round_trip(sqlite_url: str) -> None:
    import services.db.models  # noqa: F401 -- registers the table on Base.metadata

    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    model_columns = {c["name"] for c in sa.inspect(engine).get_columns(TABLE)}
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0038")

    command.downgrade(cfg, "0037")
    assert TABLE not in sa.inspect(engine).get_table_names()

    command.upgrade(cfg, "0038")
    inspector = sa.inspect(engine)
    assert {c["name"] for c in inspector.get_columns(TABLE)} == model_columns
    assert inspector.get_pk_constraint(TABLE)["constrained_columns"] == ["organization_id", "scope", "tier"]
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            sa.text(
                "INSERT INTO organization_stats (organization_id, scope, tier, gate_key) "
                "VALUES ('a', 'group', 'pro', 'k')"
            )
        )
        row = conn.execute(
            sa.text("SELECT assets, states, sources, organizations FROM organization_stats")
        ).one()
        assert (row.assets, row.states, row.organizations) == (0, "", 1)
    insert = sa.text(
        "INSERT INTO organization_stats (organization_id, scope, tier, gate_key) "
        "VALUES ('b', :scope, :tier, 'k')"
    )
    for scope, tier in (("children", "public"), ("self", "api")):
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
            conn.execute(insert, {"scope": scope, "tier": tier})
    engine.dispose()


def test_0038_follows_0037() -> None:
    from alembic.script import ScriptDirectory

    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    revision = ScriptDirectory.from_config(cfg).get_revision("0038")
    assert revision is not None and revision.down_revision == "0037"
