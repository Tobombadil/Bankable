"""Round-trip for migration 0020 (`noncommercial` in the reuse-class vocabulary; the class and
`allows_commercial_use` pinned to agree; a downgrade that refuses while any row carries the class).

Run here on SQLite through Alembic, not the ORM: the store starts at the schema `Base.metadata`
produces (which already carries 0020's constraints), is stamped `0020`, and is then walked
`downgrade -1` -> `upgrade 0020` -> (seed) -> `downgrade -1` (refused) -> (purge) -> `downgrade -1`
-> `upgrade 0020`. Each step asserts on what the CHECKs actually admit, by inserting. The PostGIS
half (`upgrade head` / `downgrade -1` / `upgrade head` against a real instance) is recorded in
`docs/CHANGELOG.md` for this revision rather than asserted here.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from services.db.base import Base

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
UTC = dt.UTC
NOW = dt.datetime(2026, 9, 25, 6, 0, tzinfo=UTC)


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0020.db'}"


def _check_names(engine: sa.Engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_check_constraints(table) if c.get("name")}


def _insert_licence(engine: sa.Engine, id_: str, reuse_class: str, *, commercial: bool) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO licence (id, name, reuse_class, attribution_required, requires_link_back, "
                "allows_derived_publication, allows_raw_publication, allows_api_redistribution, "
                "allows_bulk_export, allows_commercial_use, share_alike, gate_flag, created_at, updated_at) "
                "VALUES (:id, :name, :rc, 0, 0, 1, 1, 0, 0, :com, 0, 0, :now, :now)"
            ),
            {"id": id_, "name": id_, "rc": reuse_class, "com": int(commercial), "now": NOW},
        )


def _insert_proposal(engine: sa.Engine, public_id: str, min_reuse_class: str) -> None:
    """Through the ORM: the row shape is the model's, and only the CHECK is under test."""
    from sqlalchemy.orm import Session

    from services.db.models import Proposal

    with Session(engine) as s:
        s.add(
            Proposal(
                public_id=public_id,
                slug=public_id,
                kind="storage",
                name_canonical="P",
                jurisdiction="US-TX",
                lifecycle_state="filed",
                publish_state="public",
                published_at=NOW,
                public_at=NOW,
                min_reuse_class=min_reuse_class,
                source_count=1,
            )
        )
        s.commit()


def _delete_rows(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM proposal WHERE min_reuse_class = 'noncommercial'"))
        conn.execute(sa.text("DELETE FROM licence WHERE reuse_class = 'noncommercial'"))


def test_0020_round_trip_and_the_downgrade_refuses_while_rows_carry_the_class(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0020")

    # ---- down to 0019: the four-value vocabulary, no consistency CHECK
    command.downgrade(cfg, "-1")
    assert not any(n.endswith("noncommercial_no_commercial_use") for n in _check_names(engine, "licence"))
    with pytest.raises(sa.exc.IntegrityError):
        _insert_licence(engine, "nc-before", "noncommercial", commercial=False)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_proposal(engine, "prop_nc_before", "noncommercial")
    _insert_licence(engine, "attr-before", "attribution", commercial=False)  # the old vocabulary still works

    # ---- up to 0020: the class is admitted, and only with allows_commercial_use = false
    command.upgrade(cfg, "0020")
    assert any(n.endswith("noncommercial_no_commercial_use") for n in _check_names(engine, "licence"))
    with pytest.raises(sa.exc.IntegrityError):
        _insert_licence(engine, "nc-contradiction", "noncommercial", commercial=True)
    _insert_licence(engine, "nc-lic", "noncommercial", commercial=False)
    _insert_proposal(engine, "prop_nc", "noncommercial")
    # An `attribution` licence with the flag false is still fine: the converse is not constrained.
    _insert_licence(engine, "attr-after", "attribution", commercial=False)
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0020"

    # ---- the refusal: rows carry the class, so the downgrade must not touch the schema
    with pytest.raises(RuntimeError, match=r"refused.*licence=1.*proposal=1"):
        command.downgrade(cfg, "-1")
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0020"
    assert any(n.endswith("noncommercial_no_commercial_use") for n in _check_names(engine, "licence"))
    _insert_proposal(engine, "prop_nc_2", "noncommercial")  # the five-value CHECK is intact

    # ---- purge (the per-source decision docs/26 §5 describes), then the downgrade goes through
    _delete_rows(engine)
    command.downgrade(cfg, "-1")
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0019"
    with pytest.raises(sa.exc.IntegrityError):
        _insert_licence(engine, "nc-after-down", "noncommercial", commercial=False)
    with engine.connect() as conn:
        # The other vocabulary CHECKs survived the SQLite table recreate.
        assert {"attr-before", "attr-after"} <= set(conn.execute(sa.text("SELECT id FROM licence")).scalars())
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as c2:
            c2.execute(sa.text("UPDATE licence SET reuse_class = 'not-a-class' WHERE id = 'attr-before'"))

    # ---- and back up, idempotently
    command.upgrade(cfg, "0020")
    _insert_licence(engine, "nc-final", "noncommercial", commercial=False)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_licence(engine, "nc-final-bad", "noncommercial", commercial=True)
