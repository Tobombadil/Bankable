"""Migration 0046 (`postponed` in the proposal lifecycle vocabulary) on SQLite, through Alembic.

docs/04 E-11, both ways, on an empty and on a seeded store: the store starts at today's `Base.metadata`
(which already admits `postponed`), is stamped `0046` and walked `downgrade -1` -> `upgrade 0046` ->
(a postponed record) -> `downgrade -1` (refused) -> (the record changed deliberately) -> `downgrade -1` ->
`upgrade 0046`. Each step asserts on what the CHECK admits by writing, and that the SQLite table copy kept
every row, every other CHECK and every index. The PostgreSQL half (the NOT VALID and VALIDATE steps, their
locks and timings on a large table) is measured in docs/22 §3.3.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import pathlib
from collections.abc import Iterator
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from services.db.base import Base
from services.db.models import LIFECYCLE_STATES, Proposal

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "services" / "db" / "migrations"
NOW = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.UTC)


def _module(name: str) -> ModuleType:
    path = next((MIGRATIONS_DIR / "versions").glob(f"{name}_*.py"))
    spec = importlib.util.spec_from_file_location(f"m{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    monkeypatch.setenv("DATABASE_URL", url)
    return cfg


def _insert(engine: sa.Engine, public_id: str, state: str) -> None:
    with Session(engine) as s:
        s.add(
            Proposal(
                public_id=public_id,
                slug=public_id,
                kind="generation",
                name_canonical=public_id,
                jurisdiction="US-TX",
                lifecycle_state=state,
                publish_state="public",
                published_at=NOW,
                public_at=NOW,
                min_reuse_class="open",
                source_count=1,
            )
        )
        s.commit()


def _vocab_sql(engine: sa.Engine) -> str:
    for check in sa.inspect(engine).get_check_constraints("proposal"):
        if str(check.get("name") or "").endswith("lifecycle_state_vocab"):
            return str(check["sqltext"])
    raise AssertionError("no lifecycle_state_vocab CHECK on proposal")


def _shape(engine: sa.Engine) -> tuple[set[str], set[tuple[str, str]]]:
    """The proposal table's CHECK names and its indexes as SQLite stores them (`sqlite_master.sql`, so a
    partial index's WHERE clause counts: a table copy that rebuilt one as a full index would show)."""
    checks = {str(c["name"]) for c in sa.inspect(engine).get_check_constraints("proposal")}
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'proposal' AND sql IS NOT NULL"
            )
        ).tuples()
        indexes = {(str(name), " ".join(str(sql).split())) for name, sql in rows}
    return checks, indexes


def _states(engine: sa.Engine) -> dict[str, str]:
    with engine.connect() as conn:
        return dict(conn.execute(sa.text("SELECT public_id, lifecycle_state FROM proposal")).tuples().all())


def _version(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return str(conn.scalar(sa.text("SELECT version_num FROM alembic_version")))


@pytest.mark.parametrize("seeded", [False, True], ids=["empty", "seeded"])
def test_0046_round_trip_and_the_downgrade_refuses_while_a_record_is_postponed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, seeded: bool
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'm0046.db'}"
    engine = sa.create_engine(url, future=True)
    Base.metadata.create_all(engine)
    seed = {f"prop_seed_{i}": s for i, s in enumerate(LIFECYCLE_STATES) if s != "postponed"} if seeded else {}
    for public_id, state in seed.items():
        _insert(engine, public_id, state)
    shape = _shape(engine)
    cfg = _config(url, monkeypatch)
    command.stamp(cfg, "0046")

    # ---- down to 0045: the ten-value vocabulary refuses `postponed`, still admits the rest
    command.downgrade(cfg, "-1")
    assert _version(engine) == "0045"
    assert "'postponed'" not in _vocab_sql(engine)
    with pytest.raises(sa.exc.IntegrityError):
        _insert(engine, "prop_postponed_before", "postponed")
    _insert(engine, "prop_filed_before", "filed")
    assert _shape(engine) == shape  # the table copy kept every CHECK and index
    assert _states(engine) == {**seed, "prop_filed_before": "filed"}

    # ---- up to 0046: `postponed` is admitted; an off-vocabulary value is still refused
    command.upgrade(cfg, "0046")
    assert _version(engine) == "0046"
    _insert(engine, "prop_postponed", "postponed")
    with pytest.raises(sa.exc.IntegrityError):
        _insert(engine, "prop_bogus", "paused")
    assert _shape(engine) == shape

    # ---- the refusal: a postponed record exists, so the schema is not touched
    with pytest.raises(RuntimeError, match=r"0046 downgrade refused: 1 proposal\(s\) are 'postponed'"):
        command.downgrade(cfg, "-1")
    assert _version(engine) == "0046"
    assert "'postponed'" in _vocab_sql(engine)

    # ---- the record is changed deliberately, then the downgrade goes through
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE proposal SET lifecycle_state = 'cancelled' WHERE public_id = 'prop_postponed'")
        )
    command.downgrade(cfg, "-1")
    assert _version(engine) == "0045"
    assert _states(engine) == {**seed, "prop_filed_before": "filed", "prop_postponed": "cancelled"}

    # ---- and back up
    command.upgrade(cfg, "0046")
    _insert(engine, "prop_postponed_again", "postponed")
    assert _shape(engine) == shape
    engine.dispose()


def test_a_store_built_by_create_all_is_left_as_it_is(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'c0046.db'}"
    engine = sa.create_engine(url, future=True)
    Base.metadata.create_all(engine)
    before = _vocab_sql(engine)
    cfg = _config(url, monkeypatch)
    command.stamp(cfg, "0045")
    command.upgrade(cfg, "0046")  # the guard sees `postponed` already admitted and does nothing
    assert _vocab_sql(engine) == before
    _insert(engine, "prop_postponed", "postponed")
    engine.dispose()


class _RecordingOp:
    """Stands in for `alembic.op` on a PostgreSQL bind: records each statement, the count calls and the
    commit `autocommit_block` makes, so a test can read the order the downgrade runs them in."""

    def __init__(self, counts: list[int]) -> None:
        self.log: list[str] = []
        self._counts = iter(counts)

    def execute(self, sql: object) -> None:
        self.log.append(" ".join(str(sql).split()))

    def get_context(self) -> _RecordingOp:
        return self

    @contextlib.contextmanager
    def autocommit_block(self) -> Iterator[None]:
        self.log.append("COMMIT")
        yield
        self.log.append("BEGIN")

    def count(self) -> int:
        self.log.append("COUNT")
        return next(self._counts)


def _postgresql_downgrade(
    monkeypatch: pytest.MonkeyPatch, counts: list[int]
) -> tuple[ModuleType, _RecordingOp]:
    m0046 = _module("0046")
    recorder = _RecordingOp(counts)
    monkeypatch.setattr(m0046, "op", recorder)
    monkeypatch.setattr(m0046, "_has_table", lambda: True)
    monkeypatch.setattr(m0046, "_is_postgresql", lambda: True)
    monkeypatch.setattr(
        m0046, "_check_constraint", lambda: (m0046.CONSTRAINT, m0046._in_list(m0046.STATES_AFTER))
    )
    monkeypatch.setattr(m0046, "rows_in_the_new_state", recorder.count)
    return m0046, recorder


def test_postgresql_downgrade_counts_again_under_the_lock_its_swap_takes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the first version left open: no postponed row at the first count, one written before the swap.
    The second count runs after `ALTER TABLE ... NOT VALID` (which holds ACCESS EXCLUSIVE to the end of the
    transaction) and before the commit, so the downgrade refuses and the transaction rolls back: the narrowed
    CHECK is never committed, and VALIDATE never runs. Before the fix the count ran once, before the swap,
    the narrowed CHECK was committed NOT VALID and VALIDATE failed on the row, leaving the store at 0046
    with a CHECK that refused its own `postponed` rows."""
    m0046, recorder = _postgresql_downgrade(monkeypatch, counts=[0, 1])
    with pytest.raises(RuntimeError, match=r"0046 downgrade refused: 1 proposal\(s\) are 'postponed'"):
        m0046.downgrade()
    alter = next(
        i for i, s in enumerate(recorder.log) if s.startswith("ALTER TABLE proposal DROP CONSTRAINT")
    )
    assert recorder.log[alter].endswith("NOT VALID")
    assert recorder.log[alter + 1 :] == ["COUNT"]  # counted under the lock; nothing committed after it
    assert "COMMIT" not in recorder.log and not any("VALIDATE" in s for s in recorder.log)


def test_postgresql_downgrade_commits_only_after_the_count_under_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m0046, recorder = _postgresql_downgrade(monkeypatch, counts=[0, 0])
    m0046.downgrade()
    assert recorder.log[0] == "COUNT"  # the lock-free count that refuses the usual case
    # Both bounds are LOCAL: they end with the transaction that holds ACCESS EXCLUSIVE.
    assert recorder.log[1:3] == ["SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '5s'"]
    assert recorder.log[3].startswith(
        "ALTER TABLE proposal DROP CONSTRAINT ck_proposal_lifecycle_state_vocab"
    )
    assert "'postponed'" not in recorder.log[3] and recorder.log[3].endswith("NOT VALID")
    assert recorder.log[4:] == [
        "COUNT",
        "COMMIT",
        "SET lock_timeout = '5s'",
        "ALTER TABLE proposal VALIDATE CONSTRAINT ck_proposal_lifecycle_state_vocab",
        "RESET lock_timeout",
        "BEGIN",
    ]


def test_the_revision_chain_and_the_frozen_vocabularies() -> None:
    m0046, m0001 = _module("0046"), _module("0001")
    assert (m0046.revision, m0046.down_revision) == ("0046", "0045")
    assert m0046.STATES_BEFORE == m0001.LIFECYCLE_STATES
    assert m0046.STATES_AFTER == LIFECYCLE_STATES
    assert set(m0046.STATES_AFTER) - set(m0046.STATES_BEFORE) == {"postponed"}


def test_the_widened_check_reads_as_the_model_writes_it() -> None:
    """The migration's CHECK text is the model's (`lifecycle_state IN ('unknown', ...)`), so a store built
    by `create_all` and one migrated to 0046 hold the same constraint."""
    m0046 = _module("0046")
    model_check = next(
        c
        for c in Base.metadata.tables["proposal"].constraints
        if isinstance(c, sa.CheckConstraint) and str(c.name).endswith("lifecycle_state_vocab")
    )
    assert str(model_check.sqltext) == m0046._in_list(m0046.STATES_AFTER)
