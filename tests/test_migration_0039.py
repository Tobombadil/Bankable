"""Round trip for migration 0039 (`site_finder_run`, `site_candidate`: the site finder's segments).

0038's shape: the store starts at the schema `Base.metadata` produces (which declares both tables), is
stamped `0039`, walked down (both go) and up again (both return with their keys, indexes and
vocabulary), and the ORM models agree with what the migration created, column for column. The two
foreign keys to `asset` follow a withdrawn asset: its segments go, a withdrawn substation is unset.
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
TABLES = ("site_finder_run", "site_candidate")


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


def _down_revision() -> str:
    """0041 (prices) on this lane's base; whichever revision the coordinator relinks it onto later."""
    from alembic.script import ScriptDirectory

    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    down = ScriptDirectory.from_config(cfg).get_revision("0039").down_revision
    assert isinstance(down, str)
    return down


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0039.db'}"


def _insert_run_and_segment(
    conn: sa.Connection, *, run: str, segment: str, line: str, sub: str | None
) -> None:
    conn.execute(sa.text("INSERT INTO site_finder_run (id, status) VALUES (:id, 'ok')"), {"id": run})
    conn.execute(
        sa.text(
            "INSERT INTO site_candidate (id, run_id, line_asset_id, segment_index, start_mi, length_mi, "
            "min_lon, min_lat, max_lon, max_lat, mid_lon, mid_lat, substation_asset_id, source_id, "
            "source_url, retrieved_at, licence_id) VALUES (:id, :run, :line, 0, 0, 0.5, -98, 30, -97.99, "
            "30.01, -97.995, 30.005, :sub, 'us.osm.power', 'https://www.openstreetmap.org/way/1', "
            "'2026-10-03T15:21:49Z', 'lic')"
        ),
        {"id": segment, "run": run, "line": line, "sub": sub},
    )


def test_0039_round_trip(sqlite_url: str) -> None:
    import services.db.models  # noqa: F401 -- registers the tables on Base.metadata

    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    model_columns = {t: {c["name"] for c in sa.inspect(engine).get_columns(t)} for t in TABLES}
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0039")

    command.downgrade(cfg, _down_revision())
    names = sa.inspect(engine).get_table_names()
    assert not set(TABLES) & set(names)
    assert "asset" in names  # nothing else is touched

    command.upgrade(cfg, "0039")
    inspector = sa.inspect(engine)
    for table in TABLES:
        assert {c["name"] for c in inspector.get_columns(table)} == model_columns[table], table
    indexes = {i["name"] for i in inspector.get_indexes("site_candidate")}
    assert {"ix_site_candidate_filter", "ix_site_candidate_place", "ix_site_candidate_mid"} <= indexes
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
        _insert_run_and_segment(conn, run="r1", segment="s1", line="a1", sub=None)
        row = conn.execute(
            sa.text("SELECT voltage_unknown, flood_cover_100, retiring_count FROM site_candidate")
        ).one()
        assert (bool(row.voltage_unknown), row.flood_cover_100, row.retiring_count) == (False, 0, 0)
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text("INSERT INTO site_finder_run (id, status) VALUES ('r2', 'stale')"))
    engine.dispose()


def test_0039_withdrawn_assets_follow_the_foreign_keys(sqlite_url: str) -> None:
    """A withdrawn line takes its segments; a withdrawn substation leaves the segment without one."""
    import services.db.models  # noqa: F401

    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0039")
    command.downgrade(cfg, _down_revision())
    command.upgrade(cfg, "0039")
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=OFF"))
        for asset_id in ("line", "sub"):
            conn.execute(
                sa.text(
                    "INSERT INTO asset (id, public_id, slug, asset_type, source_asset_id, name, status, "
                    "technologies, attributes, country, source_id, source_url, retrieved_at, licence_id, "
                    "first_seen, last_changed) VALUES (:id, :id, :id, 'substation', :id, :id, 'operating', "
                    "'{}', '{}', 'US', 'us.osm.power', 'u', '2026-10-03', 'lic', '2026-10-03', '2026-10-03')"
                ),
                {"id": asset_id},
            )
        _insert_run_and_segment(conn, run="r1", segment="s1", line="line", sub="sub")
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        conn.execute(sa.text("DELETE FROM asset WHERE id = 'sub'"))
        assert conn.execute(sa.text("SELECT substation_asset_id FROM site_candidate")).scalar_one() is None
        conn.execute(sa.text("DELETE FROM asset WHERE id = 'line'"))
        assert conn.execute(sa.text("SELECT count(*) FROM site_candidate")).scalar_one() == 0
        conn.execute(sa.text("DELETE FROM site_finder_run"))
    engine.dispose()


def test_0039_sits_on_one_chain() -> None:
    """One head, and 0039 on it, whichever revision the coordinator links 0039 onto (0041 on this
    lane's base; a later revision may be relinked above or below it): the chain from the head down
    passes through 0039 and its parent, and every revision is on that one chain."""
    from alembic.script import ScriptDirectory

    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1, heads
    chain = [rev.revision for rev in script.iterate_revisions(heads[0], "base")]
    assert "0039" in chain and _down_revision() in chain
    assert chain.index(_down_revision()) == chain.index("0039") + 1
    assert set(chain) == {rev.revision for rev in script.walk_revisions()}
