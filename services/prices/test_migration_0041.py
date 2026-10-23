"""Migration 0041 (the price tables) on SQLite: upgrade from 0040, the columns match the models,
downgrade drops them, upgrade again."""

from __future__ import annotations

import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
TABLES = ("price_location", "price_daily", "price_stat", "price_latest", "retail_price")


def _config(url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    monkeypatch.setenv("DATABASE_URL", url)
    return cfg


def test_upgrade_matches_the_models_and_downgrade_drops_the_tables(tmp_path, monkeypatch) -> None:
    import services.prices.models  # noqa: F401 - registers the price tables
    from services.db.models import Base

    url = f"sqlite+pysqlite:///{tmp_path / 'm.db'}"
    engine = sa.create_engine(url, future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table in reversed(TABLES):
            conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
    cfg = _config(url, monkeypatch)
    command.stamp(cfg, "0040")

    command.upgrade(cfg, "0041")
    insp = sa.inspect(engine)
    for table in TABLES:
        assert insp.has_table(table), table
        migrated = {c["name"] for c in insp.get_columns(table)}
        modelled = {c.name for c in Base.metadata.tables[table].columns}
        assert migrated == modelled, (table, migrated ^ modelled)
        pk = insp.get_pk_constraint(table)["constrained_columns"]
        assert pk == [c.name for c in Base.metadata.tables[table].primary_key.columns], table

    command.downgrade(cfg, "0040")
    insp = sa.inspect(engine)
    assert not any(insp.has_table(t) for t in TABLES)

    command.upgrade(cfg, "0041")
    assert all(sa.inspect(engine).has_table(t) for t in TABLES)
    engine.dispose()


def test_a_store_built_by_create_all_is_left_as_it_is(tmp_path, monkeypatch) -> None:
    import services.prices.models  # noqa: F401
    from services.db.models import Base

    url = f"sqlite+pysqlite:///{tmp_path / 'c.db'}"
    engine = sa.create_engine(url, future=True)
    Base.metadata.create_all(engine)
    cfg = _config(url, monkeypatch)
    command.stamp(cfg, "0040")
    command.upgrade(cfg, "0041")  # the guards see the tables and do nothing
    assert all(sa.inspect(engine).has_table(t) for t in TABLES)
    engine.dispose()


def test_the_revision_chain() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("m0041", MIGRATIONS_DIR / "versions" / "0041_prices.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("0041", "0040")


def test_there_is_one_head() -> None:
    """`alembic upgrade head` on deploy refuses two heads ("Multiple head revisions are present")."""
    from alembic.script import ScriptDirectory

    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1
