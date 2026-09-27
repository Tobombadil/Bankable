"""One `source_run` row per run on the scheduled and the run-now path (2026-09-27).

Before: the admin "run now" route created a `running` row, and the job recorded the run under the
runner's own id, so the first row stayed `running` for ever and the route's 409 guard refused
every later run-now of the source. Now the job carries the row's id (`--run-id`) and
`record_source_run` completes that row; a crash, timeout or refusal is recorded against it too.

No Postgres: SQLite session factory and a fake `subprocess.run`, as in `test_loop.py`. Explicit
`if ...: raise AssertionError` instead of bare `assert` (see `test_jobs.py`'s docstring).
"""

from __future__ import annotations

import datetime as dt
import subprocess
from typing import Any, cast

import pytest
from sqlalchemy import delete, select

from infra.scheduler import jobs
from services.db.models import Source, SourceRun
from services.db.session import get_engine, get_sessionmaker, init_db

SOURCE_ID = "us.iso.ercot.gen_queue"
RUN_ID = "00000000-0000-4000-8000-00000000a001"
QUEUED_AT = dt.datetime(2026, 9, 27, 9, 0, tzinfo=dt.UTC)


class _Factory:
    def __init__(self) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        self.sessionmaker = get_sessionmaker(engine)

    def __call__(self) -> Any:
        return self.sessionmaker()


@pytest.fixture()
def factory() -> _Factory:
    f = _Factory()
    # The source row, as `record_source_run` would mirror it from the registry.
    jobs.record_source_run(
        f, SOURCE_ID, {"id": "00000000-0000-4000-8000-00000000a000", "status": "ok"}, trigger="schedule"
    )
    with f() as session:
        session.execute(delete(SourceRun))
        session.commit()
    return f


def _pending(factory: _Factory, run_id: str = RUN_ID) -> None:
    """The row `QueuedSourceRunner.enqueue` creates before deferring the job."""
    with factory() as session:
        session.add(
            SourceRun(
                id=jobs._run_uuid(run_id),
                source_id=SOURCE_ID,
                trigger="manual",
                started_at=QUEUED_AT,
                status="running",
            )
        )
        session.commit()


def _rows(factory: _Factory) -> list[SourceRun]:
    with factory() as session:
        return list(session.scalars(select(SourceRun)))


def _source(factory: _Factory) -> Source:
    with factory() as session:
        source: Source | None = session.get(Source, SOURCE_ID)
        if source is None:
            raise AssertionError("no source row")
        session.expunge(source)
        return source


def test_the_outcome_completes_the_pre_created_row(factory: _Factory) -> None:
    _pending(factory)
    record = {
        "id": RUN_ID,
        "status": "failed",
        "started_at": "2026-09-27T09:02:00+00:00",
        "finished_at": "2026-09-27T09:02:30+00:00",
        "error": "HttpBlocked('403')",
        "error_class": "HttpBlocked",
        "rows_seen": 0,
    }
    if not jobs.record_source_run(factory, SOURCE_ID, record, trigger="manual"):
        raise AssertionError("not recorded")
    rows = _rows(factory)
    if len(rows) != 1:
        raise AssertionError(f"one row per run, got {len(rows)}")
    row = rows[0]
    if (str(row.id), row.status, row.error_class, row.trigger) != (RUN_ID, "failed", "HttpBlocked", "manual"):
        raise AssertionError((row.id, row.status, row.error_class, row.trigger))
    if jobs._aware(row.started_at) != QUEUED_AT or row.finished_at is None:
        raise AssertionError("the row keeps the time the run was asked for and gets a finish time")
    source = _source(factory)
    if (source.health, source.consecutive_failures) != ("degraded", 1):
        raise AssertionError("completing the row updates health like any other outcome")

    # A second write of the same outcome (a retried recording) changes nothing.
    again = {**record, "status": "ok", "error": None, "error_class": None}
    jobs.record_source_run(factory, SOURCE_ID, again, trigger="manual")
    if [r.status for r in _rows(factory)] != ["failed"] or _source(factory).consecutive_failures != 1:
        raise AssertionError("a recorded run is not rewritten")


def test_a_crash_with_no_record_is_recorded_against_the_named_run(factory: _Factory) -> None:
    _pending(factory)
    outcome = jobs.fetch_outcome(
        SOURCE_ID,
        returncode=1,
        stdout="",
        stderr="Traceback ... MemoryError",
        trigger="manual",
        session_factory=factory,
        run_id=RUN_ID,
    )
    if outcome["status"] != "failed" or not outcome["transient"]:
        raise AssertionError(outcome)
    rows = _rows(factory)
    if [(str(r.id), r.status, r.error_class) for r in rows] != [(RUN_ID, "failed", "ProcessCrashed")]:
        raise AssertionError([(r.id, r.status, r.error_class) for r in rows])


def test_a_timed_out_run_now_job_records_against_its_row(
    monkeypatch: pytest.MonkeyPatch, factory: _Factory
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    _pending(factory)

    def hang(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 600))

    monkeypatch.setattr(subprocess, "run", hang)
    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)

    class _Ctx:
        class job:  # noqa: N801 - mirrors procrastinate.JobContext.job
            queue = "fetch"
            attempts = 0

    with pytest.raises(jobs.TransientConnectorFailure):
        scheduler_app.run_connector.func(cast(Any, _Ctx()), SOURCE_ID, trigger="manual", run_id=RUN_ID)
    rows = _rows(factory)
    if [(str(r.id), r.status, r.error_class, r.attempt) for r in rows] != [
        (RUN_ID, "failed", "TimeoutExpired", 1)
    ]:
        raise AssertionError([(r.id, r.status, r.error_class, r.attempt) for r in rows])


def test_a_retry_of_a_run_now_job_is_its_own_row_and_closes_an_unrecorded_first_attempt(
    monkeypatch: pytest.MonkeyPatch, factory: _Factory, tmp_path: Any
) -> None:
    """The first attempt's worker died before recording anything (the row is still `running`);
    Procrastinate runs the job again with `attempts = 1`. The retry is a new run; the orphaned
    row is closed so it does not block run-now."""
    import json

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    _pending(factory)
    commands: list[list[str]] = []

    def cli(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        run_id = "00000000-0000-4000-8000-00000000a002"
        path = tmp_path / "run.json"
        path.write_text(json.dumps({"id": run_id, "status": "unchanged", "trigger": "retry"}))
        line = {"event": "result", "status": "unchanged", "run_id": run_id, "run_path": str(path)}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(line), "")

    monkeypatch.setattr(subprocess, "run", cli)
    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)

    class _Ctx:
        class job:  # noqa: N801
            queue = "fetch"
            attempts = 1

    scheduler_app.run_connector.func(cast(Any, _Ctx()), SOURCE_ID, trigger="manual", run_id=RUN_ID)
    if "--run-id" in commands[0] or commands[0][-2:] != ["--trigger", "retry"]:
        raise AssertionError(commands)
    got = sorted((r.trigger, r.status, r.error_class, r.attempt) for r in _rows(factory))
    if got != [("manual", "failed", jobs.ABANDONED_ERROR_CLASS, 1), ("retry", "unchanged", None, 2)]:
        raise AssertionError(got)


def test_close_pending_run_touches_only_running_rows_and_never_raises(factory: _Factory) -> None:
    _pending(factory)
    if not jobs.close_pending_run(factory, RUN_ID, error="gone"):
        raise AssertionError("a running row is closed")
    if jobs.close_pending_run(factory, RUN_ID, error="again"):
        raise AssertionError("a closed row is not closed twice")
    if jobs.close_pending_run(factory, None, error="x") or jobs.close_pending_run(factory, "nope", error="x"):
        raise AssertionError("no id, nothing to close")

    def broken() -> Any:
        raise RuntimeError("database unreachable")

    if jobs.close_pending_run(broken, RUN_ID, error="x"):
        raise AssertionError("a failure to close is reported as False, not raised")
    source = _source(factory)
    if source.consecutive_failures != 0:
        raise AssertionError("closing an abandoned row is not a fetch failure")
