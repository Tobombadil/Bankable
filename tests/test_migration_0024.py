"""Round-trip for migration 0024 (`export`, the CSV export log of US-603) on SQLite: `upgrade`,
`downgrade`, `upgrade` again through Alembic -- the `tests/test_migration_0012.py` contract scoped
to this one revision. The baseline is the ORM schema minus the table 0024 creates, stamped at
0024's own `down_revision` (read from the revision graph rather than hard-coded, so the test stays
right whichever revision 0024 sits on). The Postgres run is the CI `migrations` job's
`upgrade head / downgrade -1 / upgrade head` against PostGIS, not asserted here."""

from __future__ import annotations

import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from services.db.base import Base
from services.db.models import Export

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"


def _alembic_config(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    monkeypatch.setenv("DATABASE_URL", db_url)
    return cfg


def _insert(conn: sa.Connection, *, id_hex: str, status: str = "ready", entity: str = "proposal") -> None:
    conn.execute(
        sa.text(
            "INSERT INTO export (id, public_id, account_id, user_id, entity, query, tier, status, "
            "row_cap, truncated, created_at) VALUES (:id, :pid, :acc, :usr, :entity, '{}', 'pro', "
            ":status, 10000, 0, '2026-09-26 00:00:00')"
        ),
        {
            "id": id_hex,
            "pid": f"exp_{id_hex[-10:].upper()}",
            "acc": "0" * 31 + "a",
            "usr": "0" * 31 + "b",
            "entity": entity,
            "status": status,
        },
    )


def test_0024_upgrade_downgrade_upgrade(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'migration_0024.db'}"
    engine = sa.create_engine(url, future=True)
    Base.metadata.create_all(
        engine, tables=[t for t in Base.metadata.sorted_tables if t is not Export.__table__]
    )
    assert "export" not in inspect(engine).get_table_names()

    cfg = _alembic_config(url, monkeypatch)
    revision = ScriptDirectory.from_config(cfg).get_revision("0024")
    assert revision is not None
    previous = revision.down_revision
    assert isinstance(previous, str)
    command.stamp(cfg, previous)

    command.upgrade(cfg, "0024")
    insp = inspect(engine)
    assert "export" in insp.get_table_names()
    assert {c["name"] for c in insp.get_columns("export")} == {c.name for c in Export.__table__.columns}
    assert {i["name"] for i in insp.get_indexes("export")} >= {
        "ix_export_user_created",
        "ix_export_account_id",
    }

    with engine.begin() as conn:
        _insert(conn, id_hex="0" * 31 + "1")
    for bad in ({"status": "done"}, {"entity": "match"}):
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            _insert(conn, id_hex="0" * 31 + "2", **bad)

    command.downgrade(cfg, previous)
    assert "export" not in inspect(engine).get_table_names()

    command.upgrade(cfg, "0024")
    assert "export" in inspect(engine).get_table_names()
    # Idempotent on a schema that already has the table (the convention 0015-0021 follow).
    command.downgrade(cfg, previous)
    Base.metadata.create_all(engine, tables=[Export.__table__])
    command.upgrade(cfg, "0024")
    assert "export" in inspect(engine).get_table_names()
