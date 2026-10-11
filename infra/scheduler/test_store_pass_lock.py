"""The store-wide passes hold a lock that outlives a timed-out job (docs/51 §2.4 item 3; docs/60 §6.4).

`_run_with_timeout` abandons a pass's thread when its timeout fires: the job fails, Procrastinate
releases the job's `lock="resolve"`, and the thread runs on. Since 2026-10-10 the work itself holds a
session-level advisory lock on a connection of its own (`jobs.run_store_pass`), so the next pass
ends as "skipped: previous pass still running" until the abandoned thread finishes.

The lock tests need a real Postgres (`POSTGIS_TEST_URL`, as `infra/scheduler/test_queue_schema.py`;
skipped otherwise); they take only advisory locks and write no table. The SQLite test always runs.
Explicit `if ...: raise AssertionError` instead of bare `assert` (see `test_jobs.py`'s docstring).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from infra.scheduler import jobs
from services.db.session import get_engine, get_sessionmaker

POSTGRES_URL = os.environ.get("POSTGIS_TEST_URL")
needs_postgres = pytest.mark.skipif(not POSTGRES_URL, reason="POSTGIS_TEST_URL not set")

_HELD = sa.text(
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 1 "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND classid = CAST(:hi AS oid) AND objid = CAST(:lo AS oid)"
)


def _expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(str(message))


def _held(engine: sa.Engine) -> bool:
    key = jobs.STORE_WIDE_LOCK_KEY
    with engine.connect() as connection:
        count = connection.execute(_HELD, {"hi": key >> 32, "lo": key & 0xFFFF_FFFF}).scalar()
    return bool(count)


def _wait_for(condition: Callable[[], bool], what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        _expect(time.monotonic() < deadline, f"timed out waiting for {what}")
        time.sleep(0.05)


def _recording(log: list[str], name: str) -> Callable[[], dict[str, Any]]:
    """A pass that only records that it ran."""

    def work() -> dict[str, Any]:
        log.append(name)
        return {"ran": True}

    return work


class _Blocking:
    """A pass that runs until released, like a resolve pass that outlives its timeout."""

    def __init__(self) -> None:
        self.started, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()

    def __call__(self) -> dict[str, Any]:
        self.started.set()
        try:
            self.release.wait(30)
            return {"ran": "first"}
        finally:
            self.finished.set()


@pytest.fixture()
def postgres() -> Iterator[Any]:
    if not POSTGRES_URL:
        pytest.skip("POSTGIS_TEST_URL not set")
    engine = get_engine(POSTGRES_URL)
    _expect(not _held(engine), "the store-pass lock is already held in this database")
    try:
        yield get_sessionmaker(engine)
    finally:
        _wait_for(lambda: not _held(engine), "the store-pass lock to be released after the test")
        engine.dispose()


def test_on_sqlite_the_pass_runs_unguarded() -> None:
    factory = get_sessionmaker(get_engine("sqlite+pysqlite:///:memory:"))
    report = jobs.run_store_pass("resolve_tick", lambda: {"ran": True}, _session_factory=factory)
    _expect(report == {"ran": True}, report)


@needs_postgres
def test_a_second_pass_is_skipped_while_the_first_holds_the_lock(postgres: Any) -> None:
    first = _Blocking()
    ran: list[str] = []
    results: list[dict[str, Any]] = []
    holder = threading.Thread(
        target=lambda: results.append(jobs.run_store_pass("resolve_tick", first, _session_factory=postgres))
    )
    holder.start()
    _expect(first.started.wait(10), "the first pass did not start")
    engine = postgres.kw["bind"]
    _expect(_held(engine), "the first pass must hold the lock while it runs")
    second = jobs.run_store_pass("match_tick", _recording(ran, "second"), _session_factory=postgres)
    _expect(second == {"skipped": jobs.STORE_WIDE_BUSY}, second)
    _expect(ran == [], "a skipped pass must not run its work")
    first.release.set()
    holder.join(10)
    _expect(results == [{"ran": "first"}], results)
    _expect(not _held(engine), "the lock must be free once the first pass returns")
    third = jobs.run_store_pass("enrich_tick", lambda: {"ran": "third"}, _session_factory=postgres)
    _expect(third == {"ran": "third"}, third)


@needs_postgres
def test_a_pass_that_raises_releases_the_lock(postgres: Any) -> None:
    def broken() -> dict[str, Any]:
        raise RuntimeError("resolve failed")

    with pytest.raises(RuntimeError, match="resolve failed"):
        jobs.run_store_pass("resolve_tick", broken, _session_factory=postgres)
    _expect(not _held(postgres.kw["bind"]), "a failed pass must not keep the lock")


@needs_postgres
def test_a_killed_holder_frees_the_lock(postgres: Any) -> None:
    """A worker that dies mid-pass closes its connection, and Postgres frees a session lock with it."""
    first = _Blocking()
    holder = threading.Thread(target=lambda: _swallow(jobs.run_store_pass, "resolve_tick", first, postgres))
    holder.start()
    _expect(first.started.wait(10), "the first pass did not start")
    engine = postgres.kw["bind"]
    with engine.connect() as connection:
        key = jobs.STORE_WIDE_LOCK_KEY
        pid = connection.execute(
            sa.text(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 1 "
                "AND classid = CAST(:hi AS oid) AND objid = CAST(:lo AS oid)"
            ),
            {"hi": key >> 32, "lo": key & 0xFFFF_FFFF},
        ).scalar_one()
        connection.execute(sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    _wait_for(lambda: not _held(engine), "the terminated holder's lock to go")
    report = jobs.run_store_pass("resolve_tick", lambda: {"ran": "after"}, _session_factory=postgres)
    _expect(report == {"ran": "after"}, report)
    first.release.set()
    holder.join(10)


def _swallow(fn: Callable[..., Any], name: str, work: Callable[[], dict[str, Any]], factory: Any) -> None:
    try:
        fn(name, work, _session_factory=factory)
    except Exception:  # noqa: S110 -- its connection was terminated on purpose
        pass


class _FakeTask:
    def __init__(self, name: str, deferred: list[str]) -> None:
        self.name, self.deferred = name, deferred

    def defer(self, **kwargs: Any) -> int:
        self.deferred.append(self.name)
        return len(self.deferred)


@needs_postgres
def test_a_timed_out_resolve_keeps_the_store_until_its_thread_ends(
    monkeypatch: pytest.MonkeyPatch, postgres: Any
) -> None:
    """The defect itself, through the real task bodies: a resolve pass outlives its timeout, its job
    fails, and the next resolve (and anything else under `lock="resolve"`) is skipped, not run beside
    it, until the abandoned thread finishes; then the next pass runs and chains as usual."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")  # never connected
    import infra.scheduler.app as scheduler_app

    monkeypatch.setattr(jobs, "build_session_factory", lambda: postgres)
    monkeypatch.setattr(scheduler_app, "RESOLVE_TIMEOUT_S", 1)
    deferred: list[str] = []
    monkeypatch.setattr(scheduler_app, "enrich_tick", _FakeTask("enrich_tick", deferred))
    first = _Blocking()
    monkeypatch.setattr(jobs, "resolve_tick_job", first)
    with pytest.raises(TimeoutError):
        scheduler_app.resolve_tick.func()
    engine = postgres.kw["bind"]
    _expect(first.started.is_set() and not first.finished.is_set(), "the abandoned pass must still run")
    _expect(_held(engine), "the abandoned pass keeps the lock after its job failed")

    second_ran: list[str] = []
    monkeypatch.setattr(jobs, "resolve_tick_job", _recording(second_ran, "resolve"))
    monkeypatch.setattr(jobs, "match_tick_job", _recording(second_ran, "match"))
    skipped = scheduler_app.resolve_tick.func()
    _expect(skipped == {"skipped": jobs.STORE_WIDE_BUSY}, skipped)
    _expect(scheduler_app.match_tick.func() == {"skipped": jobs.STORE_WIDE_BUSY}, "match must wait too")
    _expect(second_ran == [] and deferred == [], f"ran {second_ran}, chained {deferred}")

    first.release.set()
    _expect(first.finished.wait(10), "the abandoned pass did not finish")
    _wait_for(lambda: not _held(engine), "the abandoned pass to release the lock")
    report = scheduler_app.resolve_tick.func()
    _expect(report == {"ran": True} and second_ran == ["resolve"], report)
    _expect(deferred == ["enrich_tick"], f"a pass that ran chains as before: {deferred}")
