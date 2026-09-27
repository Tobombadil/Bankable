"""Round-trip for migration 0022 (`organization.publish_state`; docs/40 §6 item 2).

Run here on SQLite through Alembic, not the ORM, in 0020's shape: the store starts at the schema
`Base.metadata` produces (which already carries the column, its CHECK and its index), is stamped
`0022`, and is walked `downgrade -1` -> `upgrade 0022` -> (take one organisation down) ->
`downgrade -1` (refused) -> (republish) -> `downgrade -1` -> `upgrade 0022`. Each step asserts on
what the schema actually admits, by inserting. The PostGIS half (`upgrade head` / `downgrade -1`
/ `upgrade head` against a real instance) is CI's Postgres `migrations` job, not this file.
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
NOW = dt.datetime(2026, 9, 26, 6, 0, tzinfo=dt.UTC)


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    os.environ["DATABASE_URL"] = db_url
    return cfg


@pytest.fixture()
def sqlite_url(tmp_path: pathlib.Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'migration_0022.db'}"


def _columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("organization")}


def _index_names(engine: sa.Engine) -> set[str]:
    return {i["name"] for i in sa.inspect(engine).get_indexes("organization")}


def _check_names(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_check_constraints("organization") if c.get("name")}


def _has_vocab_check(engine: sa.Engine) -> bool:
    return any(n == "publish_state_vocab" or n.endswith("_publish_state_vocab") for n in _check_names(engine))


def _insert_org(engine: sa.Engine, key: str, publish_state: str | None = None) -> None:
    """Raw SQL, so the row shape is the schema's at that revision and the server default is what
    fills `publish_state` when the test does not name one."""
    cols = (
        "id, public_id, slug, name_canonical, name_normalised, type, country, ids, is_curated_issuer, "
        "first_seen, last_changed, created_at, updated_at"
    )
    vals = ":id, :pid, :slug, :name, :norm, 'developer', 'US', '{}', 0, :now, :now, :now, :now"
    params: dict[str, object] = {
        "id": key.rjust(32, "0"),
        "pid": f"org_{key}",
        "slug": f"org-{key}",
        "name": f"Org {key}",
        "norm": f"org {key}",
        "now": NOW,
    }
    if publish_state is not None:
        cols += ", publish_state"
        vals += ", :ps"
        params["ps"] = publish_state
    with engine.begin() as conn:
        conn.execute(sa.text(f"INSERT INTO organization ({cols}) VALUES ({vals})"), params)  # noqa: S608


def _state_of(engine: sa.Engine, key: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.scalar(
                sa.text("SELECT publish_state FROM organization WHERE public_id = :p"), {"p": f"org_{key}"}
            )
        )


def _set_state(engine: sa.Engine, key: str, state: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE organization SET publish_state = :s WHERE public_id = :p"),
            {"s": state, "p": f"org_{key}"},
        )


def _version(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(conn.scalar(sa.text("SELECT version_num FROM alembic_version")))


def test_0022_round_trip_and_the_downgrade_refuses_while_a_row_is_not_public(sqlite_url: str) -> None:
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0022")
    # The ORM-built schema has a Python-side default and no server default (as `proposal` does), so
    # a raw insert names the state here; the migration-built column below fills it in itself.
    _insert_org(engine, "a", "public")

    # ---- down to 0021: no column, no CHECK, no index; the row survives
    command.downgrade(cfg, "-1")
    assert "publish_state" not in _columns(engine)
    assert "ix_organization_publish_state" not in _index_names(engine)
    assert not _has_vocab_check(engine)
    assert _version(engine) == "0021"
    with pytest.raises(sa.exc.OperationalError):
        _insert_org(engine, "b", "public")
    _insert_org(engine, "b")

    # ---- up to 0022: the column arrives with `public` on every existing row, the CHECK and the index
    command.upgrade(cfg, "0022")
    assert "publish_state" in _columns(engine)
    assert "ix_organization_publish_state" in _index_names(engine)
    assert _has_vocab_check(engine)
    assert _state_of(engine, "a") == "public", "an existing organisation stays exactly as visible as it was"
    assert _state_of(engine, "b") == "public"
    _insert_org(engine, "c")  # the server default fills it in
    assert _state_of(engine, "c") == "public"
    _insert_org(engine, "d", "unpublished")
    with pytest.raises(sa.exc.IntegrityError):
        _insert_org(engine, "e", "taken_down")  # not in the vocabulary

    # ---- the refusal: one row is taken down, so the downgrade must not touch the schema
    with pytest.raises(RuntimeError, match=r"refused: 1 organization row"):
        command.downgrade(cfg, "-1")
    assert _version(engine) == "0022"
    assert "publish_state" in _columns(engine)
    assert _state_of(engine, "d") == "unpublished", "the takedown is intact"

    # ---- republish (the audited admin decision), then the downgrade goes through
    _set_state(engine, "d", "public")
    command.downgrade(cfg, "-1")
    assert _version(engine) == "0021"
    assert "publish_state" not in _columns(engine)
    with engine.connect() as conn:
        # The other rows survived SQLite's table recreate.
        got = set(conn.execute(sa.text("SELECT public_id FROM organization")).scalars())
    assert got == {"org_a", "org_b", "org_c", "org_d"}

    # ---- and back up, idempotently
    command.upgrade(cfg, "0022")
    assert _state_of(engine, "d") == "public"
    with pytest.raises(sa.exc.IntegrityError):
        _insert_org(engine, "f", "banana")


def test_0022_is_a_no_op_on_a_store_the_orm_already_built(sqlite_url: str) -> None:
    """`Base.metadata.create_all` already carries the column, CHECK and index; upgrading through
    0022 from a stamp at 0021 must add none of them twice (the chain test's own precondition)."""
    engine = sa.create_engine(sqlite_url, future=True)
    Base.metadata.create_all(engine)
    cfg = _alembic_config(sqlite_url)
    command.stamp(cfg, "0021")
    before = (_columns(engine), _index_names(engine), _check_names(engine))
    command.upgrade(cfg, "0022")
    assert (_columns(engine), _index_names(engine), _check_names(engine)) == before
    assert _version(engine) == "0022"
