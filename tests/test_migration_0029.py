"""Round-trip for migration 0029 (`ui_event` admits `page.viewed`; revises lane P2's 0028).

SQLite through Alembic, in 0025's shape: the store starts at the schema `Base.metadata` produces
(which already carries the widened CHECK), is stamped `0029`, and is walked `downgrade -1` ->
`upgrade 0029` -> refused downgrade while a `page.viewed` row exists -> `downgrade -1` once it is
gone. Each step asserts on what the schema admits, by inserting. The PostGIS half is CI's Postgres
`migrations` job.
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


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0029.db'}"


def _insert(engine: sa.Engine, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ui_event (name, props, occurred_at) VALUES (:n, '{}', '2026-09-30 00:00:00')"
            ),
            {"n": name},
        )


def _admits(engine: sa.Engine, name: str) -> bool:
    try:
        _insert(engine, name)
    except sa.exc.IntegrityError:
        return False
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM ui_event WHERE name = :n"), {"n": name})
    return True


def _version(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(conn.scalar(sa.text("SELECT version_num FROM alembic_version")))


def test_0029_round_trip_widens_and_narrows_the_name_vocabulary(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0029")
    assert _admits(engine, "page.viewed"), "the model's own schema admits the new name"
    _insert(engine, "alert.created")

    command.downgrade(cfg, "-1")
    assert _version(engine) != "0029"
    assert not _admits(engine, "page.viewed"), "0026's vocabulary refuses it"
    assert _admits(engine, "map.layer_toggled")
    assert not _admits(engine, "not.a.name")

    command.upgrade(cfg, "0029")
    assert _version(engine) == "0029"
    assert _admits(engine, "page.viewed")
    assert not _admits(engine, "not.a.name"), "still a closed vocabulary"
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT count(*) FROM ui_event WHERE name = 'alert.created'")) == 1

    _insert(engine, "page.viewed")
    with pytest.raises(RuntimeError, match=r"page\.viewed"):
        command.downgrade(cfg, "-1")
    assert _version(engine) == "0029", "a refused downgrade leaves the revision where it was"

    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM ui_event WHERE name = 'page.viewed'"))
    command.downgrade(cfg, "-1")
    assert not _admits(engine, "page.viewed")
    command.upgrade(cfg, "0029")
    assert _admits(engine, "page.viewed")


def test_0029_upgrade_is_a_no_op_on_a_schema_that_already_admits_the_name(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0028")
    command.upgrade(cfg, "0029")
    assert _version(engine) == "0029"
    assert _admits(engine, "page.viewed")
