"""Round trip for migration 0037 (`site`, `site_member`, `site_audit`; docs/21 §3.25).

0035's shape: the store starts at the schema `Base.metadata` produces (which declares the three
tables), is stamped `0037`, walked down (the tables go) and up again (they return with their
constraints). The Postgres half ran against a copy of the beta store on 2026-10-10 (upgrade,
downgrade, upgrade, then the builder twice); CI's `migrations` job repeats it.
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
TABLES = {"site", "site_member", "site_audit"}


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0037.db'}"


def test_0037_round_trip(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    assert TABLES <= set(sa.inspect(engine).get_table_names())
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0037")

    command.downgrade(cfg, "0036")
    assert not TABLES & set(sa.inspect(engine).get_table_names())

    command.upgrade(cfg, "0037")
    inspector = sa.inspect(engine)
    assert TABLES <= set(inspector.get_table_names())
    member_columns = {c["name"] for c in inspector.get_columns("site_member")}
    assert {"group_key", "parent_proposal_id", "relation", "confidence", "basis"} <= member_columns
    assert {u["name"] for u in inspector.get_unique_constraints("site")} >= {
        "uq_site_public_id",
        "uq_site_slug",
    }
    assert "ix_site_member_site_id" in {i["name"] for i in inspector.get_indexes("site_member")}
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            sa.text(
                "INSERT INTO site (id, public_id, slug, name_display, rule_version, review_flag) "
                "VALUES ('a', 'site_1', 's1', 'x', 't', 'oversize')"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO site (id, public_id, slug, name_display, rule_version, review_flag) "
                    "VALUES ('b', 'site_2', 's2', 'x', 't', 'huge')"
                )
            )


def test_0037_follows_0036() -> None:
    from alembic.script import ScriptDirectory

    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    revision = ScriptDirectory.from_config(cfg).get_revision("0037")
    assert revision is not None and revision.down_revision == "0036"
