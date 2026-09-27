"""Round-trip for migration 0023 (`match_dismissal`) on SQLite: `upgrade`, `downgrade`, `upgrade`
again through Alembic, in the pattern of `tests/test_migration_0012.py`. The baseline is the ORM
schema minus the one table 0023 creates, stamped at the revision 0023 revises (read from the
migration module itself, so the test follows `down_revision` when the chain in front of it lands).
The Postgres run is the CI job's `upgrade head / downgrade -1 / upgrade head` (no Postgres here)."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import uuid

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from services.db.base import Base
from services.db.models import MatchDismissal

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
MIGRATION_0023 = MIGRATIONS_DIR / "versions" / "0023_match_dismissal.py"


def _down_revision() -> str:
    spec = importlib.util.spec_from_file_location("migration_0023_under_test", MIGRATION_0023)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0023"
    return str(module.down_revision)


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0023.db'}"


def test_0023_upgrade_downgrade_upgrade(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    skip = {MatchDismissal.__table__}
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if t not in skip])
    assert "match_dismissal" not in inspect(engine).get_table_names()

    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, _down_revision())

    command.upgrade(cfg, "head")
    insp = inspect(engine)
    assert "match_dismissal" in insp.get_table_names()
    assert {c["name"] for c in insp.get_columns("match_dismissal")} == {
        "id",
        "user_id",
        "match_id",
        "dismissed_at",
    }
    assert {i["name"] for i in insp.get_indexes("match_dismissal")} >= {"ix_match_dismissal_match_id"}
    uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints("match_dismissal")}
    assert ("user_id", "match_id") in uniques

    # Foreign keys on, as `services/db/session.py` sets for every SQLite connection: the unique pair
    # holds, and neither side may dangle.
    user_id, match_id = uuid.uuid4().hex, uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            sa.text(
                "INSERT INTO match_dismissal (id, user_id, match_id, dismissed_at) "
                "VALUES (:id, :u, :m, '2026-09-26 00:00:00')"
            ),
            {"id": uuid.uuid4().hex, "u": user_id, "m": match_id},
        )
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO match_dismissal (id, user_id, match_id, dismissed_at) "
                    "VALUES (:id, :u, :m, '2026-09-26 00:00:01')"
                ),
                {"id": uuid.uuid4().hex, "u": user_id, "m": match_id},
            )

    command.downgrade(cfg, _down_revision())
    assert "match_dismissal" not in inspect(engine).get_table_names()

    command.upgrade(cfg, "head")
    assert "match_dismissal" in inspect(engine).get_table_names()
