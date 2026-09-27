"""`source_run.trigger` on the scheduled and the manual path (2026-09-27 defect: every scheduled run
was recorded `manual`).

Cause, as measured before the fix: `pipeline/connectors/runner.py` defaulted `trigger="manual"` and
wrote it into the run record; `record_source_run` did `record.get("trigger") or trigger`, so the
record's `manual` beat the scheduler's own `scheduled` (itself off the docs/21 §4.2 vocabulary).
Now the scheduler passes `--trigger schedule` to the CLI and its explicit value wins in
`record_source_run`; a run started by hand still says `manual`.

No Postgres: SQLite session factory and a fake `subprocess.run`, as in `test_loop.py`. Explicit
`if ...: raise AssertionError` instead of bare `assert` (see `test_jobs.py`'s docstring).
"""

from __future__ import annotations

import json
import logging
import pathlib
import subprocess
from typing import Any, cast

import pytest
from sqlalchemy import select

from infra.scheduler import jobs
from pipeline.connectors.runner import RUN_TRIGGERS
from services.db.models import SOURCE_RUN_TRIGGERS, SourceRun
from services.db.session import get_engine, get_sessionmaker, init_db

SOURCE_ID = "us.iso.ercot.gen_queue"


class _Factory:
    def __init__(self) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        self.sessionmaker = get_sessionmaker(engine)

    def __call__(self) -> Any:
        return self.sessionmaker()


@pytest.fixture()
def factory() -> _Factory:
    return _Factory()


def _record(run_id: str, trigger: str | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": run_id,
        "status": "ok",
        "started_at": "2026-09-27T03:07:00+00:00",
        "finished_at": "2026-09-27T03:07:09+00:00",
    }
    if trigger is not None:
        record["trigger"] = trigger
    return record


def _triggers(factory: _Factory) -> list[str]:
    with factory() as session:
        return [str(t) for t in session.scalars(select(SourceRun.trigger).order_by(SourceRun.id))]


def test_the_pipeline_and_the_orm_agree_on_the_trigger_vocabulary() -> None:
    if tuple(RUN_TRIGGERS) != tuple(SOURCE_RUN_TRIGGERS):
        raise AssertionError((RUN_TRIGGERS, SOURCE_RUN_TRIGGERS))
    if jobs.SCHEDULED_TRIGGER not in SOURCE_RUN_TRIGGERS:
        raise AssertionError(jobs.SCHEDULED_TRIGGER)


def test_the_callers_explicit_trigger_wins_over_the_records(factory: _Factory) -> None:
    """The defect itself: a record that says `manual` (the runner's default) written by the
    scheduled path is a `schedule` row."""
    jobs.record_source_run(
        factory, SOURCE_ID, _record("00000000-0000-4000-8000-000000000001", "manual"), trigger="schedule"
    )
    if _triggers(factory) != ["schedule"]:
        raise AssertionError(_triggers(factory))


def test_fetch_outcome_records_schedule_by_default_and_manual_when_told(
    tmp_path: pathlib.Path, factory: _Factory
) -> None:
    for i, (given, expected) in enumerate(
        ((None, "schedule"), ("manual", "manual"), ("backfill", "backfill"))
    ):
        run_id = f"00000000-0000-4000-8000-00000000001{i}"
        path = tmp_path / f"run{i}.json"
        path.write_text(json.dumps(_record(run_id, "manual")))
        stdout = json.dumps({"event": "result", "status": "ok", "run_id": run_id, "run_path": str(path)})
        kwargs: dict[str, Any] = {} if given is None else {"trigger": given}
        jobs.fetch_outcome(
            SOURCE_ID, returncode=0, stdout=stdout, stderr="", session_factory=factory, **kwargs
        )
        with factory() as session:
            row = session.scalar(select(SourceRun).where(SourceRun.id == jobs._run_uuid(run_id)))
            if row is None or row.trigger != expected:
                raise AssertionError((given, row and row.trigger))


def test_without_a_caller_trigger_the_records_value_is_kept(factory: _Factory) -> None:
    jobs.record_source_run(factory, SOURCE_ID, _record("00000000-0000-4000-8000-000000000021", "manual"))
    if _triggers(factory) != ["manual"]:
        raise AssertionError(_triggers(factory))


def test_legacy_and_unknown_triggers_are_normalised_not_rejected(
    factory: _Factory, caplog: pytest.LogCaptureFixture
) -> None:
    jobs.record_source_run(factory, SOURCE_ID, _record("00000000-0000-4000-8000-000000000031", "scheduled"))
    with caplog.at_level(logging.WARNING, logger="infra.scheduler.jobs"):
        jobs.record_source_run(factory, SOURCE_ID, _record("00000000-0000-4000-8000-000000000032", "cron"))
    jobs.record_source_run(factory, SOURCE_ID, _record("00000000-0000-4000-8000-000000000033", None))
    if _triggers(factory) != ["schedule", "schedule", "schedule"]:
        raise AssertionError(_triggers(factory))
    if not any("off-vocabulary trigger" in r.getMessage() for r in caplog.records):
        raise AssertionError("an unknown trigger must be logged, not silently rewritten")


class _Ctx:
    class job:  # noqa: N801 - mirrors procrastinate.JobContext.job
        queue = "fetch"


def _run_connector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, factory: _Factory, **kwargs: Any
) -> list[list[str]]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        # What the CLI writes: the record carries the trigger it was given on the command line.
        trigger = cmd[cmd.index("--trigger") + 1] if "--trigger" in cmd else "manual"
        run_id = f"00000000-0000-4000-8000-0000000000{len(commands):02d}"
        path = tmp_path / f"{run_id}.json"
        path.write_text(json.dumps({**_record(run_id, trigger), "status": "unchanged"}))
        line = {"event": "result", "status": "unchanged", "run_id": run_id, "run_path": str(path)}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(line), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)
    scheduler_app.run_connector.func(cast(Any, _Ctx()), SOURCE_ID, **kwargs)
    return commands


def test_a_scheduled_run_passes_trigger_schedule_to_the_cli_and_is_recorded_as_schedule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, factory: _Factory
) -> None:
    commands = _run_connector(monkeypatch, tmp_path, factory)  # how the bucket tick defers it
    if commands[0][-2:] != ["--trigger", "schedule"]:
        raise AssertionError(commands)
    if _triggers(factory) != ["schedule"]:
        raise AssertionError(_triggers(factory))


def test_a_run_now_job_passes_its_manual_trigger_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, factory: _Factory
) -> None:
    commands = _run_connector(monkeypatch, tmp_path, factory, trigger="manual")
    if commands[0][-2:] != ["--trigger", "manual"]:
        raise AssertionError(commands)
    if _triggers(factory) != ["manual"]:
        raise AssertionError(_triggers(factory))


# ---------------------------------------------------------------- retries say `retry` (2026-09-27)
def _ctx(attempts: int) -> Any:
    class _Job:
        queue = "fetch"

    job = _Job()
    job.attempts = attempts  # type: ignore[attr-defined]  # procrastinate.Job.attempts: prior attempts

    class _Context:
        pass

    context = _Context()
    context.job = job  # type: ignore[attr-defined]
    return context


def _run_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    factory: _Factory,
    attempts: int,
    *,
    status: str = "unchanged",
    **kwargs: Any,
) -> list[list[str]]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        trigger = cmd[cmd.index("--trigger") + 1]
        run_id = (
            cmd[cmd.index("--run-id") + 1]
            if "--run-id" in cmd
            else f"00000000-0000-4000-8000-0000000001{attempts:02d}"
        )
        record = {**_record(run_id, trigger), "status": status}
        if status == "failed":
            record.update(error="HttpFailed('503')", error_class="HttpFailed")
        path = tmp_path / f"{run_id}.json"
        path.write_text(json.dumps(record))
        line = {"event": "result", "status": status, "run_id": run_id, "run_path": str(path)}
        return subprocess.CompletedProcess(cmd, 1 if status == "failed" else 0, json.dumps(line), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)
    scheduler_app.run_connector.func(_ctx(attempts), SOURCE_ID, **kwargs)
    return commands


def _rows(factory: _Factory) -> list[SourceRun]:
    with factory() as session:
        return list(session.scalars(select(SourceRun).order_by(SourceRun.started_at, SourceRun.id)))


def test_a_first_attempt_is_recorded_as_its_trigger_with_attempt_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, factory: _Factory
) -> None:
    commands = _run_attempt(monkeypatch, tmp_path, factory, 0)
    if commands[0][-2:] != ["--trigger", "schedule"]:
        raise AssertionError(commands)
    rows = _rows(factory)
    if [(r.trigger, r.attempt, r.dead_lettered) for r in rows] != [("schedule", 1, False)]:
        raise AssertionError([(r.trigger, r.attempt, r.dead_lettered) for r in rows])


@pytest.mark.parametrize("original", [None, "manual", "backfill"])
def test_a_procrastinate_retry_is_recorded_as_retry_with_its_attempt_number(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, factory: _Factory, original: str | None
) -> None:
    """Procrastinate re-runs the same job with `job.attempts` = the attempts already made; the
    retry is a new run (its own row), recorded `trigger = retry`, `attempt = attempts + 1`, whatever
    started the original."""
    kwargs: dict[str, Any] = {} if original is None else {"trigger": original}
    commands = _run_attempt(monkeypatch, tmp_path, factory, 2, **kwargs)
    if commands[0][-2:] != ["--trigger", "retry"]:
        raise AssertionError(commands)
    rows = _rows(factory)
    if [(r.trigger, r.attempt) for r in rows] != [("retry", 3)]:
        raise AssertionError([(r.trigger, r.attempt) for r in rows])


def test_the_last_transient_failure_is_recorded_dead_lettered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, factory: _Factory
) -> None:
    """`FETCH_RETRY` decides whether a transient failure is retried; the attempt it refuses to
    retry is the dead letter, and its row says so. Earlier transient failures do not."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    last = scheduler_app.FETCH_RETRY.max_attempts
    if last is None:
        raise AssertionError("FETCH_RETRY must bound its attempts")
    for attempts in (last - 1, last):
        with pytest.raises(jobs.TransientConnectorFailure):
            _run_attempt(monkeypatch, tmp_path, factory, attempts, status="failed")
    rows = _rows(factory)
    got = sorted((r.attempt, r.dead_lettered, r.trigger) for r in rows)
    if got != [(last, False, "retry"), (last + 1, True, "retry")]:
        raise AssertionError(got)
