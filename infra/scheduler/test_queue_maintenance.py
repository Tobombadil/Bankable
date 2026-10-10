"""Procrastinate upkeep (docs/51 §2.9 item 2, 2026-10-10): stalled jobs are recovered, old jobs are
pruned, both on the tick pattern, and the worker's heartbeat threshold fits the stop grace.

The recovery runs against Procrastinate's own `InMemoryConnector`, which implements the same
heartbeat, prune, retry and delete queries the Postgres connector runs; no database needed."""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
from collections.abc import Iterator
from typing import Any

import procrastinate
import pytest
import yaml
from procrastinate import testing
from procrastinate.jobs import Job, Status
from procrastinate.manager import JobManager

from infra.scheduler import queue_maintenance as qm
from infra.scheduler.cadence import CRON_BY_BUCKET

ROOT = pathlib.Path(__file__).resolve().parents[2]
UTC = dt.UTC


@pytest.fixture()
def scheduler_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")  # never connected
    import infra.scheduler.app as app_module

    return app_module


def _minutes(cron: str) -> set[int]:
    """The minutes a cron's first field fires on (`*`, `*/n`, `a,b`, `n`)."""
    out: set[int] = set()
    for part in cron.split()[0].split(","):
        if part == "*":
            out.update(range(60))
        elif part.startswith("*/"):
            out.update(range(0, 60, int(part[2:])))
        else:
            out.add(int(part))
    return out


# ------------------------------------------------------------------------------ registration
@pytest.mark.parametrize(
    ("tick", "periodic_id", "job", "cron"),
    [
        ("tick_stalled_jobs", "tick:stalled_jobs", "retry_stalled_jobs", qm.STALLED_CRON),
        ("tick_prune_jobs", "tick:prune_jobs", "remove_old_jobs", qm.PRUNE_CRON),
    ],
)
def test_each_tick_defers_its_job_onto_a_consumed_queue_at_its_own_minutes(
    scheduler_app: Any, tick: str, periodic_id: str, job: str, cron: str
) -> None:
    periodic = scheduler_app.app.periodic_registry.periodic_tasks
    registered = periodic[(tick, periodic_id)]
    assert registered.cron == cron
    assert registered.task.queue == scheduler_app.SCHEDULER_ONLY_QUEUE
    others = [p.cron for key, p in periodic.items() if key != (tick, periodic_id)]
    used = set().union(*(_minutes(c) for c in [*others, *CRON_BY_BUCKET.values()]))
    assert not _minutes(cron) & used, sorted(_minutes(cron) & used)
    task = scheduler_app.app.tasks[job]
    assert (task.queue, task.queueing_lock, task.retry_strategy) == ("audit", job, None)
    worker = yaml.safe_load((ROOT / "infra" / "compose" / "docker-compose.yml").read_text())["services"][
        "worker"
    ]
    assert task.queue in worker["command"][worker["command"].index("--queues") + 1].split(",")


def test_stalled_jobs_are_looked_for_every_ten_minutes_and_old_jobs_pruned_daily() -> None:
    assert _minutes(qm.STALLED_CRON) == {4, 14, 24, 34, 44, 54}
    assert qm.STALLED_CRON.split()[1:] == ["*", "*", "*", "*"]
    assert qm.PRUNE_CRON.split()[1:] == ["2", "*", "*", "*"] and len(_minutes(qm.PRUNE_CRON)) == 1


@pytest.mark.parametrize(
    ("tick_name", "job_name", "message"),
    [
        ("_tick_stalled_jobs", "retry_stalled_jobs", "retry_stalled_jobs still queued"),
        ("_tick_prune_jobs", "remove_old_jobs", "remove_old_jobs still queued"),
    ],
)
def test_the_ticks_defer_once_and_tolerate_an_overlap(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scheduler_app: Any,
    tick_name: str,
    job_name: str,
    message: str,
) -> None:
    calls: list[str] = []

    class _Deferrer:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        def defer(self) -> None:
            calls.append("defer")
            if self.fail:
                raise procrastinate.exceptions.AlreadyEnqueued("locked")

    monkeypatch.setattr(scheduler_app, job_name, _Deferrer(fail=False))
    getattr(scheduler_app, tick_name)(0)
    monkeypatch.setattr(scheduler_app, job_name, _Deferrer(fail=True))
    caplog.set_level("INFO", logger="infra.scheduler")
    getattr(scheduler_app, tick_name)(0)
    assert calls == ["defer", "defer"]
    assert any(message in r.getMessage() for r in caplog.records)


def test_the_tasks_run_the_maintenance_bodies_with_the_apps_job_manager(
    monkeypatch: pytest.MonkeyPatch, scheduler_app: Any
) -> None:
    seen: list[tuple[str, object]] = []

    async def _recover(manager: object) -> dict[str, Any]:
        seen.append(("recover", manager))
        return {"retried": [7]}

    async def _remove(manager: object) -> dict[str, Any]:
        seen.append(("remove", manager))
        return {"failed_kept_hours": 1}

    monkeypatch.setattr(qm, "recover_stalled_jobs", _recover)
    monkeypatch.setattr(qm, "remove_old_jobs", _remove)
    assert asyncio.run(scheduler_app.app.tasks["retry_stalled_jobs"]()) == {"retried": [7]}
    assert asyncio.run(scheduler_app.app.tasks["remove_old_jobs"]()) == {"failed_kept_hours": 1}
    assert [name for name, _ in seen] == ["recover", "remove"]
    assert all(manager is scheduler_app.app.job_manager for _, manager in seen)


# ------------------------------------------------------------------------------ thresholds
def test_a_draining_worker_is_never_taken_for_dead() -> None:
    """A worker stops beating when its graceful stop starts and Docker kills it after the grace, so
    the stalled threshold must exceed the grace, and the compose files must give that grace."""
    assert qm.STALLED_WORKER_TIMEOUT_S == 2 * qm.STOP_GRACE_S == 600
    base = yaml.safe_load((ROOT / "infra" / "compose" / "docker-compose.yml").read_text())["services"]
    for name in ("worker", "browser-worker", "scheduler"):
        assert base[name]["stop_grace_period"] == f"{qm.STOP_GRACE_S}s", name


def test_every_worker_process_prunes_peers_only_past_the_stalled_threshold(
    monkeypatch: pytest.MonkeyPatch, scheduler_app: Any
) -> None:
    """Procrastinate's default `stalled_worker_timeout` is 30 s: a worker starting during a peer's
    graceful stop would prune it, and the peer's running job would count as stalled at once."""
    from infra.scheduler import worker

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(scheduler_app.app, "run_worker", lambda **kwargs: calls.append(kwargs))
    worker.main(["--queues", "fetch,audit", "--concurrency", "2"])
    scheduler_app.main()
    assert [c["stalled_worker_timeout"] for c in calls] == [qm.STALLED_WORKER_TIMEOUT_S] * 2
    assert calls[0]["queues"] == ["fetch", "audit"] and calls[1]["queues"] == [
        scheduler_app.SCHEDULER_ONLY_QUEUE
    ]


# ------------------------------------------------------------------------------ recovery
def _now() -> dt.datetime:
    return dt.datetime.now(UTC)


class _Queue:
    """A JobManager over the in-memory connector, with helpers to stage workers and jobs."""

    def __init__(self) -> None:
        self.connector = testing.InMemoryConnector()
        self.manager = JobManager(self.connector)

    def run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def worker(self, silent_for_s: float) -> int:
        worker_id = int(self.run(self.manager.register_worker()))
        self.connector.workers[worker_id] = _now() - dt.timedelta(seconds=silent_for_s)
        return worker_id

    def doing(
        self, task: str, worker_id: int, *, lock: str | None = None, queueing_lock: str | None = None
    ) -> int:
        job = Job(id=None, queue="resolve", lock=lock, queueing_lock=queueing_lock, task_name=task)
        job_id = int(self.run(self.manager.defer_job_async(job)).id or 0)
        row = self.connector.jobs[job_id]
        row["status"], row["worker_id"] = "doing", worker_id
        self.connector.events[job_id].append({"type": "started", "at": _now()})
        return job_id

    def status(self, job_id: int) -> str:
        return str(self.connector.jobs[job_id]["status"])


@pytest.fixture()
def queue() -> Iterator[_Queue]:
    yield _Queue()


def test_a_dead_workers_job_is_retried_and_releases_its_lock(queue: _Queue) -> None:
    dead = queue.worker(silent_for_s=qm.STALLED_WORKER_TIMEOUT_S + 60)
    job_id = queue.doing("resolve_tick", dead, lock="resolve", queueing_lock="resolve_tick")
    report = queue.run(qm.recover_stalled_jobs(queue.manager))
    assert report == {"pruned_workers": 1, "retried": [job_id], "failed": []}
    assert queue.status(job_id) == "todo", "back in the queue, so `lock=resolve` is free again"
    assert queue.connector.jobs[job_id]["attempts"] == 1
    assert dead not in queue.connector.workers


def test_a_live_or_draining_workers_job_is_left_alone(queue: _Queue) -> None:
    """A one-hour resolve on a live worker beats every 10 s; a worker in its graceful stop has not
    beaten for up to the grace. Neither is stalled."""
    live = queue.worker(silent_for_s=5)
    draining = queue.worker(silent_for_s=qm.STOP_GRACE_S - 1)
    long_pass = queue.doing("resolve_tick", live, lock="resolve")
    finishing = queue.doing("context_load", draining, lock="context")
    report = queue.run(qm.recover_stalled_jobs(queue.manager))
    assert report == {"pruned_workers": 0, "retried": [], "failed": []}
    assert queue.status(long_pass) == queue.status(finishing) == "doing"


def test_a_job_that_keeps_killing_its_worker_is_failed_not_retried(
    queue: _Queue, caplog: pytest.LogCaptureFixture
) -> None:
    dead = queue.worker(silent_for_s=qm.STALLED_WORKER_TIMEOUT_S + 60)
    job_id = queue.doing("context_load", dead, lock="resolve")
    queue.connector.jobs[job_id]["attempts"] = qm.STALLED_RETRY_MAX_ATTEMPTS
    caplog.set_level("WARNING", logger="infra.scheduler.queue_maintenance")
    report = queue.run(qm.recover_stalled_jobs(queue.manager))
    assert report["failed"] == [job_id] and report["retried"] == []
    assert queue.status(job_id) == "failed", "failed releases the lock too"
    assert any("attempted too often" in r.getMessage() for r in caplog.records)


def test_a_stalled_job_whose_task_is_already_queued_is_failed_instead() -> None:
    """Postgres refuses a second `todo` row with one queueing lock; the queued one does the work."""
    stalled = Job(id=41, queue="resolve", lock="resolve", queueing_lock="match_tick", task_name="match_tick")
    actions: list[tuple[str, int | None, object]] = []

    class _Manager:
        async def prune_stalled_workers(self, seconds: float) -> list[int]:
            return []

        async def get_stalled_jobs(self, seconds_since_heartbeat: float) -> list[Job]:
            actions.append(("look", None, seconds_since_heartbeat))
            return [stalled]

        async def retry_job(self, job: Job) -> None:
            raise procrastinate.exceptions.UniqueViolation(
                constraint_name="procrastinate_jobs_queueing_lock_idx_v1", queueing_lock="match_tick"
            )

        async def finish_job(self, job: Job, status: Status, delete_job: bool) -> None:
            actions.append(("finish", job.id, (status, delete_job)))

    report = asyncio.run(qm.recover_stalled_jobs(_Manager()))  # type: ignore[arg-type]
    assert report == {"pruned_workers": 0, "retried": [], "failed": [41]}
    assert actions == [("look", None, qm.STALLED_WORKER_TIMEOUT_S), ("finish", 41, (Status.FAILED, False))]


# ------------------------------------------------------------------------------ pruning
def test_old_jobs_go_after_their_keep_period_and_failures_stay_longer(queue: _Queue) -> None:
    def finished(status: str, age_days: float) -> int:
        job = Job(id=None, queue="fetch", lock=None, queueing_lock=None, task_name="run_connector")
        job_id = int(queue.run(queue.manager.defer_job_async(job)).id or 0)
        queue.connector.jobs[job_id]["status"] = status
        queue.connector.events[job_id] = [{"type": status, "at": _now() - dt.timedelta(days=age_days)}]
        return job_id

    kept = {
        finished("succeeded", 13),
        finished("failed", 30),
        finished("failed", 89),
        finished("todo", 400),  # never finished: never pruned
    }
    gone = {
        finished("succeeded", 15),
        finished("cancelled", 15),
        finished("aborted", 15),
        finished("failed", 91),
    }
    report = queue.run(qm.remove_old_jobs(queue.manager))
    assert set(queue.connector.jobs) == kept, sorted(set(queue.connector.jobs) & gone)
    assert report == {"succeeded_kept_hours": 14 * 24, "failed_kept_hours": 90 * 24}
    assert qm.FAILED_JOBS_KEEP_HOURS > qm.SUCCEEDED_JOBS_KEEP_HOURS


# ------------------------------------------------------------------------------ worker engine limits
def test_the_worker_engine_bounds_statements_and_lock_waits_on_postgres_only() -> None:
    from infra.scheduler import jobs

    args = jobs.worker_connect_args("postgresql+psycopg://u:p@db:5432/infraque")
    assert args == {"options": "-c statement_timeout=600000 -c lock_timeout=300000"}
    assert jobs.WORKER_LOCK_TIMEOUT_MS < jobs.WORKER_STATEMENT_TIMEOUT_MS
    # Above the longest measured database job (context_load, 190.5 s in one transaction).
    assert jobs.WORKER_LOCK_TIMEOUT_MS > 190_500 * 1.5
    for url in (None, "", "sqlite+pysqlite:///:memory:"):
        assert jobs.worker_connect_args(url) == {}


def test_only_the_worker_session_factory_passes_the_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.db.session as session_module
    from infra.scheduler import jobs

    seen: list[tuple[str | None, dict[str, object] | None]] = []
    real = session_module.get_engine

    def _spy(url: str | None = None, *, connect_args: dict[str, object] | None = None) -> Any:
        seen.append((url, connect_args))
        return real("sqlite+pysqlite:///:memory:")

    monkeypatch.setattr(session_module, "get_engine", _spy)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/infraque")
    jobs.build_session_factory.cache_clear()
    try:
        jobs.build_session_factory()
    finally:
        jobs.build_session_factory.cache_clear()
    assert seen == [("postgresql+psycopg://u:p@db:5432/infraque", jobs.worker_connect_args(seen[0][0]))]
    deps = (ROOT / "services" / "api" / "deps.py").read_text()
    assert "get_engine(database_url)" in deps and "connect_args" not in deps, "the API keeps server defaults"


def test_get_engine_passes_connect_args_through_beside_its_sqlite_defaults() -> None:
    from services.db.session import get_engine

    engine = get_engine("sqlite+pysqlite:///:memory:", connect_args={"timeout": 7})
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("select 1").scalar() == 1
    finally:
        engine.dispose()
