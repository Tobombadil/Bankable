"""Round-trip for migration 0030 (`asset.status` gains `retiring`; `asset.retirement_year`).
Lane R1 wrote it as 0027; renumbered after lanes P2 (0027, 0028) and A1 (0029) merged first.

0026's shape: the store starts at the schema `Base.metadata` produces (which already has the
five-value CHECK and the column), is stamped `0030`, and is walked down and up again. Each step
asserts on what the schema admits by inserting, and that asset rows survive both directions. The
PostGIS half is CI's Postgres `migrations` job.
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
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0030.db'}"


def _columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("asset")}


def _insert_asset(engine: sa.Engine, key: str, status: str, year: int | None = None) -> None:
    columns = (
        "id, public_id, slug, asset_type, source_asset_id, name, status, technologies, attributes, "
        "country, source_id, source_url, retrieved_at, licence_id, first_seen, last_changed"
    )
    values = (
        ":id, :pid, :slug, 'power_plant', :key, 'Plant', :status, '{}', '{}', 'US', 'us.eia.860m', "
        "'https://example.org', '2026-10-06 00:00:00', 'lic', '2026-10-06 00:00:00', '2026-10-06 00:00:00'"
    )
    params: dict[str, object] = {
        "id": key.rjust(32, "0"),
        "pid": f"asset_{key}",
        "slug": f"plant-{key}",
        "key": key,
        "status": status,
    }
    if year is not None:
        columns += ", retirement_year"
        values += ", :year"
        params["year"] = year
    with engine.begin() as conn:
        conn.execute(sa.text(f"INSERT INTO asset ({columns}) VALUES ({values})"), params)  # noqa: S608


def _keys(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return {str(r[0]) for r in conn.execute(sa.text("SELECT source_asset_id FROM asset")).all()}


def test_0030_round_trip(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0030")
    _insert_asset(engine, "1", "retired", 2024)
    _insert_asset(engine, "2", "operating")

    # ---- down to 0029: no column, `retiring` refused, rows kept
    command.downgrade(cfg, "-1")
    assert "retirement_year" not in _columns(engine)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_asset(engine, "3", "retiring")
    assert _keys(engine) == {"1", "2"}

    # ---- up to 0030: the column (NULL) and the fifth status arrive; nonsense is still refused
    command.upgrade(cfg, "0030")
    assert "retirement_year" in _columns(engine)
    assert "ix_asset_retirement_year" in {i["name"] for i in sa.inspect(engine).get_indexes("asset")}
    _insert_asset(engine, "4", "retiring", 2028)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_asset(engine, "5", "mothballed")
    assert _keys(engine) == {"1", "2", "4"}


def test_0030_downgrade_refuses_while_an_asset_is_retiring(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0030")
    _insert_asset(engine, "1", "retiring", 2030)
    with pytest.raises(RuntimeError, match="retiring"):
        command.downgrade(cfg, "-1")
    assert "retirement_year" in _columns(engine)


def test_0030_is_a_no_op_on_a_store_the_orm_already_built(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0029")
    before = _columns(engine)
    command.upgrade(cfg, "0030")
    assert _columns(engine) == before
    _insert_asset(engine, "1", "retiring", 2031)
