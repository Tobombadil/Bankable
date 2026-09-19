"""Round-trip for migration 0012 (`suppression`, `privacy_request`) on SQLite: `upgrade`,
`downgrade`, `upgrade` again through Alembic — the same contract `services/db/test_migrations.py`
proves for 0009-0011, scoped to this one revision. The baseline (a database at 0011) is the ORM
schema minus the two tables 0012 creates, stamped `0011`; the Postgres run is the CI job's
`upgrade head / downgrade -1 / upgrade head` against the local PostGIS, recorded in the sprint
report rather than here (no Postgres in the default test run)."""

from __future__ import annotations

import os
import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from services.db.base import Base
from services.db.models import PrivacyRequest, Suppression

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0012.db'}"


def test_0012_upgrade_downgrade_upgrade(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    skip = {Suppression.__table__, PrivacyRequest.__table__}
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if t not in skip])
    assert "suppression" not in inspect(engine).get_table_names()

    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0011")

    command.upgrade(cfg, "head")
    insp = inspect(engine)
    assert {"suppression", "privacy_request"} <= set(insp.get_table_names())
    assert {c["name"] for c in insp.get_columns("suppression")} == {
        "id",
        "email_hash",
        "reason",
        "created_at",
    }
    assert {"kind", "record_public_id", "contact_email", "message", "status", "completed_at"} <= {
        c["name"] for c in insp.get_columns("privacy_request")
    }
    assert {i["name"] for i in insp.get_indexes("suppression")} >= {"ix_suppression_email_hash"}
    assert {i["name"] for i in insp.get_indexes("privacy_request")} >= {
        "ix_privacy_request_status_created",
        "ix_privacy_request_record",
    }
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO suppression (id, email_hash, reason, created_at) "
                "VALUES ('00000000000000000000000000000001', 'abc', 'erasure', '2026-09-18 00:00:00')"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO suppression (id, email_hash, reason, created_at) "
                    "VALUES ('00000000000000000000000000000002', 'abc', 'because', '2026-09-18 00:00:00')"
                )
            )

    command.downgrade(cfg, "0011")
    names = set(inspect(engine).get_table_names())
    assert "suppression" not in names and "privacy_request" not in names

    command.upgrade(cfg, "head")
    assert {"suppression", "privacy_request"} <= set(inspect(engine).get_table_names())
