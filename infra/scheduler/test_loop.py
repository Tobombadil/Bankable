"""The scheduled path after the 2026-09-18 audit (§3.1 "always-on loop is not a loop", §4 item 2):

  6. a failing fetch backs off between attempts (Procrastinate `RetryStrategy`, not `retry=5`,
     which is five attempts with zero wait);
  7. `run_connector` writes a `source_run` row and updates `source.health` / last-success fields
     (before: no writer at all on the scheduled path — the CLI only wrote a JSON file);
  8. a successful fetch enqueues `load_source`, which enqueues `resolve_tick`, which enqueues
     `enrich_tick`; each guarded by a lock so a source is never fetched and loaded at once.

No Postgres: the SQLite session factory from `services.db.session`, a fake `subprocess.run`, and
fake task objects standing in for the Procrastinate deferrers (the pattern `test_jobs.py` uses).
Explicit `if ...: raise AssertionError` instead of bare `assert` (see `test_jobs.py`'s docstring).
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
from dataclasses import dataclass, field
from typing import Any, cast

import procrastinate
import pytest
from sqlalchemy import select

from infra.scheduler import cadence, jobs
from services.db.models import Source, SourceRun
from services.db.session import get_engine, get_sessionmaker, init_db

SOURCE_ID = "us.iso.ercot.gen_queue"  # open, implemented, plain egress


class _Factory:
    """A sessionmaker-like callable around one in-memory SQLite engine."""

    def __init__(self) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        self.sessionmaker = get_sessionmaker(engine)

    def __call__(self) -> Any:
        return self.sessionmaker()


@pytest.fixture()
def factory() -> _Factory:
    return _Factory()


def _record(tmp_path: pathlib.Path, status: str, **over: Any) -> tuple[dict[str, Any], str]:
    """A run record as `pipeline/connectors/runner.py` writes it, saved where the CLI's `result`
    log line would point."""
    ts = "20260918T030700Z"
    record: dict[str, Any] = {
        "id": "8d3b7f1e-4c2a-4b1e-9c3d-1f2e3d4c5b6a",
        "source_id": SOURCE_ID,
        "trigger": "scheduled",
        "started_at": "2026-09-18T03:07:00+00:00",
        "finished_at": "2026-09-18T03:07:09+00:00",
        "status": status,
        "egress_class": "plain",
        "http_status": 200,
        "bytes": 1234,
        "rows_seen": 25,
        "rows_new": 1,
        "rows_changed": 2,
        "rows_gone": 0,
        "events_emitted": 3,
        "worker_seconds": 9.1,
        "dq_status": "pass",
        "dq": {"status": "pass", "checks": []},
        "error": None,
        "error_class": None,
        "attempt": 1,
        "outputs": {},
    }
    record.update(over)
    path = tmp_path / "runs" / SOURCE_ID / f"{ts}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return record, str(path)


def _stdout(source_id: str, run_path: str, status: str) -> str:
    return "\n".join(
        [
            json.dumps({"event": "run finished", "source_id": source_id, "status": status}),
            json.dumps({"event": "result", "source_id": source_id, "status": status, "run_path": run_path}),
        ]
    )


# ---------------------------------------------------------------- item 7: run and health writers
def test_fetch_outcome_writes_a_source_run_row_and_marks_the_source_healthy(
    tmp_path: pathlib.Path, factory: _Factory
) -> None:
    record, run_path = _record(tmp_path, "ok")
    outcome = jobs.fetch_outcome(
        SOURCE_ID, returncode=0, stdout=_stdout(SOURCE_ID, run_path, "ok"), stderr="", session_factory=factory
    )
    if outcome["status"] != "ok" or outcome["ts"] != "20260918T030700Z":
        raise AssertionError(outcome)
    with factory() as session:
        run = session.scalar(select(SourceRun))
        if run is None:
            raise AssertionError("no source_run row written")
        if str(run.id) != record["id"] or run.status != "ok" or run.trigger != "scheduled":
            raise AssertionError((run.id, run.status, run.trigger))
        if (run.rows_seen, run.rows_new, run.rows_changed, run.events_emitted) != (25, 1, 2, 3):
            raise AssertionError((run.rows_seen, run.rows_new, run.rows_changed, run.events_emitted))
        if run.finished_at is None or run.started_at is None:
            raise AssertionError("started_at/finished_at must be set")
        source = session.get(Source, SOURCE_ID)
        if source is None:
            raise AssertionError("source row must be upserted from the registry when missing")
        if source.health != "ok" or source.consecutive_failures != 0 or source.last_success_at is None:
            raise AssertionError((source.health, source.consecutive_failures, source.last_success_at))


def test_fetch_outcome_is_idempotent_per_run_id(tmp_path: pathlib.Path, factory: _Factory) -> None:
    _, run_path = _record(tmp_path, "ok")
    for _ in range(2):
        jobs.fetch_outcome(
            SOURCE_ID,
            returncode=0,
            stdout=_stdout(SOURCE_ID, run_path, "ok"),
            stderr="",
            session_factory=factory,
        )
    with factory() as session:
        if len(session.scalars(select(SourceRun)).all()) != 1:
            raise AssertionError("the same run id must not produce two rows")


def test_repeated_failures_degrade_then_fail_the_source(tmp_path: pathlib.Path, factory: _Factory) -> None:
    for i in range(3):
        _, run_path = _record(
            tmp_path,
            "failed",
            id=f"00000000-0000-4000-8000-00000000000{i}",
            error="BadZipFile('x')",
            error_class="BadZipFile",
        )
        outcome = jobs.fetch_outcome(
            SOURCE_ID,
            returncode=1,
            stdout=_stdout(SOURCE_ID, run_path, "failed"),
            stderr="",
            session_factory=factory,
        )
        with factory() as session:
            source = session.get(Source, SOURCE_ID)
            if source is None:
                raise AssertionError("missing source")
            expected = "degraded" if i < 2 else "failing"  # US-904 AC3: failing at 3 consecutive failures
            if source.health != expected or source.consecutive_failures != i + 1:
                raise AssertionError((i, source.health, source.consecutive_failures))
            if source.last_error != "BadZipFile('x')" or source.last_error_at is None:
                raise AssertionError((source.last_error, source.last_error_at))
    if outcome["status"] != "failed":
        raise AssertionError(outcome)
    with factory() as session:
        runs = session.scalars(select(SourceRun)).all()
        if len(runs) != 3 or {r.error_class for r in runs} != {"BadZipFile"}:
            raise AssertionError([(r.status, r.error_class) for r in runs])


def test_a_blocked_run_marks_the_source_blocked(tmp_path: pathlib.Path, factory: _Factory) -> None:
    _, run_path = _record(tmp_path, "blocked", error="HttpBlocked('challenge')", error_class="HttpBlocked")
    jobs.fetch_outcome(
        SOURCE_ID,
        returncode=1,
        stdout=_stdout(SOURCE_ID, run_path, "blocked"),
        stderr="",
        session_factory=factory,
    )
    with factory() as session:
        source = session.get(Source, SOURCE_ID)
        if source is None or source.health != "blocked":
            raise AssertionError(source and source.health)


def test_a_success_resets_the_failure_counter(tmp_path: pathlib.Path, factory: _Factory) -> None:
    _, failed_path = _record(
        tmp_path, "failed", id="00000000-0000-4000-8000-0000000000aa", error="x", error_class="E"
    )
    jobs.fetch_outcome(
        SOURCE_ID,
        returncode=1,
        stdout=_stdout(SOURCE_ID, failed_path, "failed"),
        stderr="",
        session_factory=factory,
    )
    _, ok_path = _record(tmp_path, "unchanged", id="00000000-0000-4000-8000-0000000000bb")
    jobs.fetch_outcome(
        SOURCE_ID,
        returncode=0,
        stdout=_stdout(SOURCE_ID, ok_path, "unchanged"),
        stderr="",
        session_factory=factory,
    )
    with factory() as session:
        source = session.get(Source, SOURCE_ID)
        if source is None or source.health != "ok" or source.consecutive_failures != 0:
            raise AssertionError(source and (source.health, source.consecutive_failures))


def test_a_crash_without_a_result_line_is_a_failed_run_with_the_stderr_tail(factory: _Factory) -> None:
    outcome = jobs.fetch_outcome(
        SOURCE_ID, returncode=-9, stdout="", stderr="Traceback ...\nMemoryError", session_factory=factory
    )
    if outcome["status"] != "failed" or not outcome["transient"]:
        raise AssertionError(outcome)
    with factory() as session:
        run = session.scalar(select(SourceRun))
        if run is None or run.status != "failed" or "MemoryError" not in str(run.error):
            raise AssertionError(run and (run.status, run.error))
        if run.finished_at is None:
            raise AssertionError("a crashed run still gets finished_at")


def test_a_gated_source_writes_no_run_row_and_does_not_raise(factory: _Factory) -> None:
    outcome = jobs.fetch_outcome(
        "us.iso.pjm.gen_queue",
        returncode=2,
        stdout=json.dumps({"event": "gate refused"}),
        stderr="",
        session_factory=factory,
    )
    if outcome["status"] != "refused":
        raise AssertionError(outcome)
    with factory() as session:
        if session.scalar(select(SourceRun)) is not None:
            raise AssertionError("a gated source has no source row to hang a run on")


def test_transient_classification() -> None:
    if not jobs.is_transient({"status": "failed", "error_class": "HttpFailed"}):
        raise AssertionError("HTTP failures are transient")
    if jobs.is_transient({"status": "failed", "error_class": "BadZipFile"}):
        raise AssertionError("a corrupt payload is not fixed by retrying")
    if jobs.is_transient({"status": "blocked", "error_class": "HttpBlocked"}):
        raise AssertionError("retrying a block is impolite")


# ---------------------------------------------------------------- item 6: backoff on the scheduled path
def test_run_connector_retries_with_exponential_backoff_only_on_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    strategy = scheduler_app.app.tasks["infra.scheduler.app.run_connector"].retry_strategy
    if not isinstance(strategy, procrastinate.RetryStrategy):
        raise AssertionError(f"run_connector must carry a RetryStrategy, got {strategy!r}")
    if strategy.max_attempts != 5:
        raise AssertionError(strategy.max_attempts)
    if not strategy.exponential_wait:
        raise AssertionError("no exponential wait: five attempts with zero backoff (the audit finding)")

    class _Job:
        def __init__(self, attempts: int) -> None:
            self.attempts = attempts

    waits: list[int | None] = []
    for attempts in range(1, 6):
        decision = strategy.get_retry_decision(
            exception=jobs.TransientConnectorFailure("x"), job=cast(Any, _Job(attempts))
        )
        if decision is None or decision.retry_at is None:
            waits.append(None)
        else:
            waits.append(round((decision.retry_at - dt.datetime.now(dt.UTC)).total_seconds()))
    if waits[-1] is not None:
        raise AssertionError(f"fifth failure must dead-letter, got {waits}")
    real = [w for w in waits if w is not None]
    if real != sorted(real) or len(set(real)) != len(real) or real[0] < 10:
        raise AssertionError(f"waits must grow: {waits}")
    if (
        strategy.get_retry_decision(exception=jobs.ConnectorRunFailed("parse"), job=cast(Any, _Job(1)))
        is not None
    ):
        raise AssertionError("a non-transient failure (corrupt payload, block, gate) is never retried")


# ---------------------------------------------------------------- item 8: the loop closes
class _FakeTask:
    def __init__(self, name: str, log: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> None:
        self.name = name
        self.log = log
        self._configure: dict[str, Any] = {}

    def configure(self, **kw: Any) -> _FakeTask:
        clone = _FakeTask(self.name, self.log)
        clone._configure = kw
        return clone

    def defer(self, **kw: Any) -> None:
        self.log.append((self.name, self._configure, kw))


def test_a_successful_fetch_enqueues_the_load_job_under_the_source_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, factory: _Factory
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    _, run_path = _record(tmp_path, "ok")

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=_stdout(SOURCE_ID, run_path, "ok"), stderr="")

    deferred: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(scheduler_app, "load_source", _FakeTask("load_source", deferred))
    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)

    class _Ctx:
        class job:  # noqa: N801 - mirrors procrastinate.JobContext.job
            queue = "fetch"

    scheduler_app.run_connector.func(cast(Any, _Ctx()), SOURCE_ID)  # the task body, without a worker

    if deferred != [
        (
            "load_source",
            {
                "lock": cadence.execution_lock_for(SOURCE_ID),
                "queueing_lock": f"load:{cadence.safe_id(SOURCE_ID)}",
            },
            {"source_id": SOURCE_ID, "ts": "20260918T030700Z"},
        )
    ]:
        raise AssertionError(deferred)
    with factory() as session:
        if session.scalar(select(SourceRun)) is None:
            raise AssertionError("the run row is written before the load job is enqueued")


def test_an_unchanged_fetch_enqueues_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, factory: _Factory
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    _, run_path = _record(tmp_path, "unchanged")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=_stdout(SOURCE_ID, run_path, "unchanged"), stderr=""
        ),
    )
    deferred: list[Any] = []
    monkeypatch.setattr(scheduler_app, "load_source", _FakeTask("load_source", deferred))
    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)

    class _Ctx:
        class job:  # noqa: N801
            queue = "fetch"

    scheduler_app.run_connector.func(cast(Any, _Ctx()), SOURCE_ID)
    if deferred:
        raise AssertionError(deferred)


def test_a_transient_failure_raises_for_the_retry_strategy_and_a_block_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, factory: _Factory
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)
    monkeypatch.setattr(scheduler_app, "load_source", _FakeTask("load_source", []))

    class _Ctx:
        class job:  # noqa: N801
            queue = "fetch"

    _, failed = _record(
        tmp_path,
        "failed",
        id="00000000-0000-4000-8000-0000000000c1",
        error="HttpFailed('503')",
        error_class="HttpFailed",
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, _stdout(SOURCE_ID, failed, "failed"), ""),
    )
    with pytest.raises(jobs.TransientConnectorFailure):
        scheduler_app.run_connector.func(cast(Any, _Ctx()), SOURCE_ID)

    _, blocked = _record(
        tmp_path,
        "blocked",
        id="00000000-0000-4000-8000-0000000000c2",
        error="HttpBlocked",
        error_class="HttpBlocked",
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, _stdout(SOURCE_ID, blocked, "blocked"), ""),
    )
    with pytest.raises(jobs.ConnectorRunFailed):
        scheduler_app.run_connector.func(cast(Any, _Ctx()), SOURCE_ID)


def test_load_source_job_loads_the_run_and_the_task_chains_to_resolve_then_enrich(
    monkeypatch: pytest.MonkeyPatch, factory: _Factory
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    calls: list[tuple[Any, ...]] = []

    @dataclass
    class _Result:  # shaped like services.ingest.loader.LoadResult
        source_run_id: str = "run"
        proposals_created: int = 2
        proposals_updated: int = 1
        events_created: int = 3
        warnings: list[str] = field(default_factory=list)

    def fake_load(session: Any, source_id: str, ts: str, **kw: Any) -> _Result:
        calls.append((source_id, ts, kw.get("data_root")))
        return _Result()

    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)
    report = jobs.load_source_job(SOURCE_ID, "20260918T030700Z", _load=fake_load)
    if calls != [(SOURCE_ID, "20260918T030700Z", scheduler_app.ROOT / "data")]:
        raise AssertionError(calls)
    if report["proposals_created"] != 2 or report["events_created"] != 3:
        raise AssertionError(report)

    deferred: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    real_load, real_resolve = scheduler_app.load_source, scheduler_app.resolve_tick
    monkeypatch.setattr(scheduler_app, "resolve_tick", _FakeTask("resolve_tick", deferred))
    monkeypatch.setattr(scheduler_app, "enrich_tick", _FakeTask("enrich_tick", deferred))
    monkeypatch.setattr(jobs, "load_source_job", lambda source_id, ts: {"ok": True})
    monkeypatch.setattr(jobs, "resolve_tick_job", lambda: {"ok": True})
    real_load.func(SOURCE_ID, "20260918T030700Z")  # the task bodies, without a worker
    real_resolve.func()
    if [d[0] for d in deferred] != ["resolve_tick", "enrich_tick"]:
        raise AssertionError(deferred)


def test_load_resolve_and_enrich_tasks_are_registered_on_queues_the_worker_consumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    tasks = scheduler_app.app.tasks
    load = tasks["load_source"]
    if load.queue != "normalise":
        raise AssertionError(load.queue)
    resolve = tasks["resolve_tick"]
    if resolve.queue != "resolve" or resolve.queueing_lock != "resolve_tick" or resolve.lock != "resolve":
        raise AssertionError((resolve.queue, resolve.queueing_lock, resolve.lock))
    enrich = tasks["enrich_tick"]
    if enrich.queue != "resolve" or enrich.queueing_lock != "enrich_tick" or enrich.lock != "resolve":
        raise AssertionError((enrich.queue, enrich.queueing_lock, enrich.lock))
    periodic = scheduler_app.app.periodic_registry.periodic_tasks
    if ("tick_resolve", "tick:resolve") not in periodic:
        raise AssertionError(sorted(periodic))


def test_default_enrich_runs_the_county_fips_backfill(factory: _Factory) -> None:
    report = jobs.enrich_tick_job(_run=None, _session_factory=factory)
    if report.get("rows_seen") != 0 or report.get("filled") != 0:
        raise AssertionError(report)


def test_default_resolve_runs_over_the_store_without_a_normalised_frame(
    factory: _Factory, tmp_path: pathlib.Path
) -> None:
    report = jobs.resolve_tick_job(_run=None, _session_factory=factory, _data_root=tmp_path)
    if report.get("organizations_merged") != 0 or report.get("proposal_clusters") != 0:
        raise AssertionError(report)


def test_execution_lock_is_per_source_and_shared_by_fetch_and_load() -> None:
    if cadence.execution_lock_for("us.iso.ercot.gen_queue") != "source:us-iso-ercot-gen-queue":
        raise AssertionError(cadence.execution_lock_for("us.iso.ercot.gen_queue"))
