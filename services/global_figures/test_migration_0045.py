"""Migration 0045 (the global electricity tables) on SQLite: upgrade from 0043, the columns and keys match
the models, downgrade drops them, upgrade again; a store built by `create_all` is left as it is."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
TABLES = ("global_electricity_load", "global_electricity_value")


def _config(url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    monkeypatch.setenv("DATABASE_URL", url)
    return cfg


def test_upgrade_matches_the_models_and_downgrade_drops_the_tables(tmp_path, monkeypatch) -> None:
    import services.global_figures.models  # noqa: F401 - registers the tables
    from services.db.models import Base

    url = f"sqlite+pysqlite:///{tmp_path / 'm.db'}"
    engine = sa.create_engine(url, future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table in TABLES[::-1]:  # the values first: they reference the loads
            conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
    cfg = _config(url, monkeypatch)
    command.stamp(cfg, "0043")

    command.upgrade(cfg, "0045")
    insp = sa.inspect(engine)
    for table in TABLES:
        assert insp.has_table(table), table
        migrated = {c["name"] for c in insp.get_columns(table)}
        modelled = {c.name for c in Base.metadata.tables[table].columns}
        assert migrated == modelled, (table, migrated ^ modelled)
        pk = insp.get_pk_constraint(table)["constrained_columns"]
        assert pk == [c.name for c in Base.metadata.tables[table].primary_key.columns], table
    unique = {tuple(u["column_names"]) for u in insp.get_unique_constraints("global_electricity_load")}
    assert ("dataset", "origin") in unique

    command.downgrade(cfg, "0043")
    insp = sa.inspect(engine)
    assert not any(insp.has_table(t) for t in TABLES)

    command.upgrade(cfg, "0045")
    assert all(sa.inspect(engine).has_table(t) for t in TABLES)
    engine.dispose()


def test_a_store_built_by_create_all_is_left_as_it_is(tmp_path, monkeypatch) -> None:
    import services.global_figures.models  # noqa: F401
    from services.db.models import Base

    url = f"sqlite+pysqlite:///{tmp_path / 'c.db'}"
    engine = sa.create_engine(url, future=True)
    Base.metadata.create_all(engine)
    cfg = _config(url, monkeypatch)
    command.stamp(cfg, "0043")
    command.upgrade(cfg, "0045")  # the guards see the tables and do nothing
    assert all(sa.inspect(engine).has_table(t) for t in TABLES)
    engine.dispose()


def test_the_revision_chain() -> None:
    spec = importlib.util.spec_from_file_location(
        "m0045", MIGRATIONS_DIR / "versions" / "0045_global_electricity.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("0045", "0043")


def test_the_frozen_vocabularies_match_the_code() -> None:
    from pipeline.connectors.global_ember_electricity.areas import AREA_KINDS
    from pipeline.connectors.global_ember_electricity.ember import DATASETS, METRICS, ORIGINS

    spec = importlib.util.spec_from_file_location(
        "m0045v", MIGRATIONS_DIR / "versions" / "0045_global_electricity.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DATASETS == DATASETS and module.ORIGINS == ORIGINS and module.AREA_KINDS == AREA_KINDS
    assert set(module.METRICS) == set(METRICS)
