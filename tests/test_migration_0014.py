"""Round-trip for migration 0014 (drop the stale `idx_built_plant_geom` GiST index) on SQLite.

0014 is Postgres-only work: the index it drops is the one geoalchemy2 created implicitly for
`built_plant.geom` in 0007 and that survived 0009's rename to `asset`. On SQLite `geom` is TEXT
(`services.db.types.GeographyPoint`), geoalchemy2 never ran, and no such index has ever existed —
so what this file proves is the dialect guard: `upgrade`, `downgrade`, `upgrade` all run, leave
the `asset` table's index set untouched, and reach no DDL at all on SQLite. The Postgres half
(the duplicate really is there at 0013, is gone after 0014 and comes back on `downgrade -1`) is
what the `migrations` CI job's `upgrade head / downgrade -1 / upgrade head` against PostGIS
exercises on every run, and is quoted index-by-index in the migration's own docstring.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import types

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from services.db.base import Base

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
MIGRATION_0014 = MIGRATIONS_DIR / "versions" / "0014_drop_stale_asset_geom_index.py"


def _load_0014() -> types.ModuleType:
    """`0014_...` is not an importable identifier and `versions/` is not a package, so the
    revision module is loaded by path the way Alembic itself loads it."""
    spec = importlib.util.spec_from_file_location("migration_0014_under_test", MIGRATION_0014)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0014.db'}"


def _asset_indexes(engine: sa.Engine) -> set[str]:
    return {i["name"] or "" for i in inspect(engine).get_indexes("asset")}


def test_0014_upgrade_downgrade_upgrade_is_a_no_op_on_sqlite(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    before = _asset_indexes(engine)
    assert "ix_asset_geom" in before
    assert "idx_built_plant_geom" not in before

    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0013")

    command.upgrade(cfg, "head")
    assert _asset_indexes(engine) == before
    command.downgrade(cfg, "-1")
    assert _asset_indexes(engine) == before
    command.upgrade(cfg, "head")
    assert _asset_indexes(engine) == before

    with engine.connect() as conn:
        version = conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert version == "0014", "the no-op still records the revision"


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_0014_reaches_no_ddl_on_sqlite(
    sqlite_url: str, direction: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both directions return at the `dialect.name != "postgresql"` guard. Asserted on the calls
    rather than on the schema because a statement naming a GiST index would raise on SQLite, which
    a plain round trip could not tell apart from a guard that fired."""
    module = _load_0014()
    engine = sa.create_engine(sqlite_url, future=True)
    calls: list[object] = []
    monkeypatch.setattr(module.op, "execute", lambda *a, **k: calls.append(a))
    with engine.connect() as conn:
        ctx = MigrationContext.configure(connection=conn)
        with Operations.context(ctx):
            getattr(module, direction)()
    assert calls == []
