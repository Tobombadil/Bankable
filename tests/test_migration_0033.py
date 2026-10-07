"""Round-trip for migration 0033 (`post.fields_snapshot`, `post.gate_failures`,
`channel_config.auto_publish_event_types`; content audit F1, F5, F11).

0031's shape: the store starts at the schema `Base.metadata` produces (which already has the three
columns), is stamped `0033`, walked down and up again, and the rows survive. The Postgres half is
CI's `migrations` job.
"""

from __future__ import annotations

import os
import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

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
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0033.db'}"


def _columns(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def test_0033_follows_0032_on_a_single_line(sqlite_url: str) -> None:
    """0033 sits directly on 0032 and the chain has one head. Not "0033 is the head": that broke the
    moment 0034 landed (2026-10-07)."""
    script = ScriptDirectory.from_config(_alembic_config(sqlite_url))
    assert script.get_revision("0033").down_revision == "0032"
    assert len(script.get_heads()) == 1


def test_0033_round_trip(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0033")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO channel_config (channel, auto_publish, created_at, updated_at) "
                "VALUES ('bluesky', 1, '2026-10-07', '2026-10-07')"
            )
        )

    command.downgrade(cfg, "-1")
    assert not {"fields_snapshot", "gate_failures"} & _columns(engine, "post")
    assert "auto_publish_event_types" not in _columns(engine, "channel_config")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM channel_config")).scalar() == 1

    command.upgrade(cfg, "0033")
    assert {"fields_snapshot", "gate_failures"} <= _columns(engine, "post")
    assert "auto_publish_event_types" in _columns(engine, "channel_config")
    with engine.connect() as conn:
        # An existing switch lists no event type, so it auto-publishes nothing until re-enabled.
        assert conn.execute(sa.text("SELECT auto_publish_event_types FROM channel_config")).scalar() is None


def test_0033_is_a_no_op_on_a_store_the_orm_already_built(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0032")
    before = (_columns(engine, "post"), _columns(engine, "channel_config"))
    command.upgrade(cfg, "0033")
    assert (_columns(engine, "post"), _columns(engine, "channel_config")) == before
