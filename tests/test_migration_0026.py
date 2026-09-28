"""Round-trip for migration 0026 (`interconnection_point` and `proposal.interconnection_point_id`).

Run here on SQLite through Alembic, in 0025's shape: the store starts at the schema
`Base.metadata` produces (which already carries the table and the column), is stamped `0026`, and
is walked `downgrade -1` -> `upgrade 0026` -> `downgrade -1` -> `upgrade 0026`. Each step asserts
on what the schema admits by inserting, and that proposal rows survive both directions. The
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
SOURCE_ID = "us.test.migration_0026"


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0026.db'}"


def _tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _proposal_columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("proposal")}


def _version(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(conn.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _insert_proposal(engine: sa.Engine, key: str, point: str | None = None) -> None:
    columns = (
        "id, public_id, slug, kind, name_canonical, jurisdiction, lifecycle_state, identifiers, "
        "first_seen, last_changed, publish_state, min_reuse_class, field_provenance, overrides, "
        "source_count, created_by, created_at, updated_at"
    )
    values = (
        ":id, :pid, :slug, 'generation', 'P', 'US-TX', 'filed', '{}', '2026-09-28 00:00:00', "
        "'2026-09-28 00:00:00', 'public', 'open', '{}', '{}', 1, 'pipeline', '2026-09-28 00:00:00', "
        "'2026-09-28 00:00:00'"
    )
    params = {"id": key.rjust(32, "0"), "pid": f"prop_{key}", "slug": f"p-{key}"}
    if point is not None:
        columns += ", interconnection_point_id"
        values += ", :point"
        params["point"] = point.rjust(32, "0")
    with engine.begin() as conn:
        conn.execute(sa.text(f"INSERT INTO proposal ({columns}) VALUES ({values})"), params)  # noqa: S608


def _insert_point(
    engine: sa.Engine, key: str, *, name_key: str = "sub:bearkat|345", kind: str = "substation"
) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO interconnection_point (id, public_id, operator, name_display, name_key, "
                "key_rule, kind, source_id, source_url, retrieved_at, licence_id, created_at, updated_at) "
                "VALUES (:id, :pid, 'ERCOT', 'Bearkat 345kV', :name_key, '2026-09-28.1', :kind, :sid, "
                "'https://example.org', '2026-09-28 00:00:00', 'lic', '2026-09-28 00:00:00', "
                "'2026-09-28 00:00:00')"
            ),
            {
                "id": key.rjust(32, "0"),
                "pid": f"poi_{key}",
                "name_key": name_key,
                "kind": kind,
                "sid": SOURCE_ID,
            },
        )


def _proposal_ids(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return {str(r[0]).lstrip("0") for r in conn.execute(sa.text("SELECT id FROM proposal")).all()}


def test_0026_round_trip_creates_and_drops_the_point_table_and_keeps_proposals(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0026")
    # A plain SQLite engine does not enforce foreign keys, so no source/licence row is needed.
    _insert_point(engine, "a1")
    _insert_proposal(engine, "a", point="a1")
    _insert_proposal(engine, "b")

    # ---- down to 0025: no table, no column; the proposals survive
    command.downgrade(cfg, "-1")
    assert _version(engine) == "0025"
    assert "interconnection_point" not in _tables(engine)
    assert "interconnection_point_id" not in _proposal_columns(engine)
    assert _proposal_ids(engine) == {"a", "b"}
    _insert_proposal(engine, "c")

    # ---- up to 0026: the table and a NULL column arrive; the CHECK and the unique key bite
    command.upgrade(cfg, "0026")
    assert _version(engine) == "0026"
    assert "interconnection_point" in _tables(engine)
    assert "interconnection_point_id" in _proposal_columns(engine)
    with engine.connect() as conn:
        linked = conn.execute(sa.text("SELECT interconnection_point_id FROM proposal")).all()
    assert all(r[0] is None for r in linked)
    _insert_point(engine, "p1")
    _insert_point(engine, "p2", name_key="line:cortland~fenner|115", kind="line_tap")
    with pytest.raises(sa.exc.IntegrityError):
        _insert_point(engine, "p3")  # same (source_id, name_key) as p1
    with pytest.raises(sa.exc.IntegrityError):
        _insert_point(engine, "p4", name_key="sub:other|138", kind="switchyard")
    _insert_proposal(engine, "d", point="p1")
    indexes = {i["name"] for i in sa.inspect(engine).get_indexes("proposal")}
    assert "ix_proposal_interconnection_point_id" in indexes

    # ---- and down and up again, idempotently, keeping every proposal
    command.downgrade(cfg, "-1")
    assert "interconnection_point" not in _tables(engine)
    assert _proposal_ids(engine) == {"a", "b", "c", "d"}
    command.upgrade(cfg, "0026")
    assert "interconnection_point_id" in _proposal_columns(engine)
    assert _proposal_ids(engine) == {"a", "b", "c", "d"}


def test_0026_is_a_no_op_on_a_store_the_orm_already_built(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0025")
    before = (_tables(engine), _proposal_columns(engine))
    command.upgrade(cfg, "0026")
    assert (_tables(engine), _proposal_columns(engine)) == before
    assert _version(engine) == "0026"
