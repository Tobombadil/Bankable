"""Round-trip for migration 0025 (`source_run` release columns and the `trigger` CHECK).

Run here on SQLite through Alembic, in 0022's shape: the store starts at the schema
`Base.metadata` produces (which already carries the columns and the CHECK), is stamped `0025`,
and is walked `downgrade -1` -> (write the legacy `scheduled` trigger the scheduler used to
record) -> `upgrade 0025` -> `downgrade -1` -> `upgrade 0025`. Each step asserts on what the schema
actually admits, by inserting. The PostGIS half is CI's Postgres `migrations` job.
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
SOURCE_ID = "us.test.migration_0025"


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0025.db'}"


def _columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("source_run")}


def _has_trigger_check(engine: sa.Engine) -> bool:
    names = {c["name"] for c in sa.inspect(engine).get_check_constraints("source_run") if c.get("name")}
    return any(n == "trigger_vocab" or n.endswith("_trigger_vocab") for n in names)


def _insert_run(engine: sa.Engine, key: str, trigger: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO source_run (id, source_id, trigger, started_at, status, egress_class, "
                "rows_seen, rows_new, rows_changed, rows_gone, events_emitted, model_calls, cost_usd, "
                "worker_seconds, attempt, dead_lettered, created_at) VALUES (:id, :sid, :trigger, "
                "'2026-09-27 03:07:00', 'partial', 'plain', 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, "
                "'2026-09-27 03:07:00')"
            ),
            {"id": key.rjust(32, "0"), "sid": SOURCE_ID, "trigger": trigger},
        )


def _triggers(engine: sa.Engine) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT id, trigger FROM source_run")).all()
    return {str(r[0]).lstrip("0"): str(r[1]) for r in rows}


def _version(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(conn.scalar(sa.text("SELECT version_num FROM alembic_version")))


def test_0025_round_trip_normalises_the_legacy_trigger_and_enforces_the_vocabulary(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0025")
    # A plain SQLite engine does not enforce foreign keys, so no source/licence row is needed.
    _insert_run(engine, "a", "manual")

    # ---- down to 0024: no release columns, no CHECK; the rows survive
    command.downgrade(cfg, "-1")
    assert _version(engine) == "0024"
    assert not {"released_at", "released_by", "release_reason"} & _columns(engine)
    assert not _has_trigger_check(engine)
    _insert_run(engine, "b", "scheduled")  # what infra/scheduler/jobs.py recorded before 0025
    assert _triggers(engine) == {"a": "manual", "b": "scheduled"}

    # ---- up to 0025: `scheduled` becomes `schedule`, the columns arrive null, the CHECK bites
    command.upgrade(cfg, "0025")
    assert _version(engine) == "0025"
    assert {"released_at", "released_by", "release_reason"} <= _columns(engine)
    assert _has_trigger_check(engine)
    assert _triggers(engine) == {"a": "manual", "b": "schedule"}
    with engine.connect() as conn:
        released = conn.execute(
            sa.text("SELECT released_at, released_by, release_reason FROM source_run")
        ).all()
    assert all(tuple(r) == (None, None, None) for r in released)
    for ok in ("schedule", "backfill", "retry"):
        _insert_run(engine, f"c{ok}", ok)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_run(engine, "d", "scheduled")
    with pytest.raises(sa.exc.IntegrityError):
        _insert_run(engine, "e", "cron")

    # ---- and down and up again, idempotently, keeping every row
    command.downgrade(cfg, "-1")
    assert _version(engine) == "0024"
    assert "released_at" not in _columns(engine)
    command.upgrade(cfg, "0025")
    assert _triggers(engine)["b"] == "schedule"
    assert len(_triggers(engine)) == 5


def test_0025_is_a_no_op_on_a_store_the_orm_already_built(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0024")
    before = (_columns(engine), _has_trigger_check(engine))
    command.upgrade(cfg, "0025")
    assert (_columns(engine), _has_trigger_check(engine)) == before
    assert _version(engine) == "0025"
