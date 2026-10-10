"""Migration 0036: NESO links move from `<pid>/h<hash>` to the bare Project ID parser 2.0.0 emits,
only where that is unambiguous. The migration reads and writes `proposal_source` alone, so the
store here is that table's key columns, stamped `0035` and upgraded."""

from __future__ import annotations

import os
import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
NESO = "gb.neso.tec_register"


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def store(tmp_path: pathlib.Path) -> tuple[sa.Engine, Config]:
    url = f"sqlite+pysqlite:///{tmp_path / 'migration_0036.db'}"
    engine = sa.create_engine(url, future=True)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE proposal_source (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, "
                "source_record_id TEXT NOT NULL, active BOOLEAN NOT NULL)"
            )
        )
    cfg = _alembic_config(url)
    command.stamp(cfg, "0035")
    return engine, cfg


def _keys(engine: sa.Engine) -> dict[str, str]:
    with engine.connect() as conn:
        return dict(conn.execute(sa.text("SELECT id, source_record_id FROM proposal_source")).all())


def test_0036_renames_only_unambiguous_neso_hash_keys(store: tuple[sa.Engine, Config]) -> None:
    engine, cfg = store
    rows = [
        ("single", NESO, "PID1/h0123456789ab", True),  # renamed
        ("staged-1", NESO, "PID2/1", True),  # a staged project: untouched
        ("staged-h", NESO, "PID2/h0123456789ab", True),  # shares PID2 with a staged row: kept
        ("bare", NESO, "PID3", True),
        ("bare-h", NESO, "PID3/hffffffffffff", True),  # a bare PID3 already exists: kept
        ("twice-a", NESO, "PID4/haaaaaaaaaaaa", True),  # two active hash keys: ambiguous, kept
        ("twice-b", NESO, "PID4/hbbbbbbbbbbbb", True),
        ("gone", NESO, "PID5/hcccccccccccc", False),  # history keeps its key
        ("live", NESO, "PID5/hdddddddddddd", True),  # renamed despite the inactive sibling
        ("other-source", "us.iso.ercot.gen_queue", "INR1/h0123456789ab", True),
    ]
    with engine.begin() as conn:
        for row in rows:
            conn.execute(
                sa.text("INSERT INTO proposal_source VALUES (:id, :source, :key, :active)"),
                dict(zip(("id", "source", "key", "active"), row, strict=True)),
            )

    command.upgrade(cfg, "0036")

    keys = _keys(engine)
    assert keys["single"] == "PID1"
    assert keys["live"] == "PID5"
    assert keys["gone"] == "PID5/hcccccccccccc"
    original = {row[0]: row[2] for row in rows}
    for unchanged in ("staged-1", "staged-h", "bare", "bare-h", "twice-a", "twice-b", "other-source"):
        assert keys[unchanged] == original[unchanged], unchanged

    command.downgrade(cfg, "0035")  # a no-op by design
    assert _keys(engine) == keys


def test_0036_on_a_store_without_neso_is_a_no_op(store: tuple[sa.Engine, Config]) -> None:
    engine, cfg = store
    command.upgrade(cfg, "0036")
    assert _keys(engine) == {}
