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
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, cast

import procrastinate
import pytest
from sqlalchemy import select

from infra.scheduler import cadence, jobs
from services.db.models import Source, SourceRun
from services.db.session import get_engine, get_sessionmaker, init_db

ROOT = pathlib.Path(__file__).resolve().parents[2]

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
        # What the runner wrote into every record before 2026-09-27, whoever started the run: the
        # CLI's default. The scheduled path must still record `schedule` (test_trigger.py).
        "trigger": "manual",
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
        if str(run.id) != record["id"] or run.status != "ok" or run.trigger != "schedule":
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
    if strategy.max_attempts != 4:  # five runs, the fifth failure dead-letters (docs/21 D-13)
        raise AssertionError(strategy.max_attempts)
    if not strategy.exponential_wait:
        raise AssertionError("no exponential wait: five attempts with zero backoff (the audit finding)")

    class _Job:
        def __init__(self, attempts: int) -> None:
            self.attempts = attempts

    # `attempts` is the number of attempts already made when this one failed (0 on the first run),
    # so attempts=4 is the fifth failure.
    waits: list[int | None] = []
    for attempts in range(0, 5):
        decision = strategy.get_retry_decision(
            exception=jobs.TransientConnectorFailure("x"), job=cast(Any, _Job(attempts))
        )
        if decision is None or decision.retry_at is None:
            waits.append(None)
        else:
            waits.append(round((decision.retry_at - dt.datetime.now(dt.UTC)).total_seconds()))
    if waits[-1] is not None:
        raise AssertionError(f"the fifth failure must dead-letter, got {waits}")
    real = [w for w in waits if w is not None]
    expected = [5, 25, 125, 625]
    if len(real) != len(expected) or any(abs(w - e) > 1 for w, e in zip(real, expected, strict=True)):
        raise AssertionError(f"waits must be {expected} s, got {waits}")
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
    monkeypatch.delenv("INFRAQUE_DATA_DIR", raising=False)
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


def test_the_load_reads_where_the_connector_wrote_when_the_data_dir_is_a_volume(
    monkeypatch: pytest.MonkeyPatch, factory: _Factory, tmp_path: pathlib.Path
) -> None:
    """docs/60 §11 item 9: in a container the connector output lives on the `connector_data`
    volume named by INFRAQUE_DATA_DIR, not under the root-owned /app/data. The fetch (the
    connector CLI's `Store` default) and the load must agree on it, or every load misses its file."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(tmp_path))
    seen: list[Any] = []

    def fake_load(session: Any, source_id: str, ts: str, **kw: Any) -> dict[str, Any]:
        seen.append(kw.get("data_root"))
        return {}

    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)
    jobs.load_source_job(SOURCE_ID, "20260918T030700Z", _load=fake_load)
    if seen != [tmp_path]:
        raise AssertionError(seen)
    # The connector side reads the variable at import; check it in a fresh interpreter.
    probe = "from pipeline.connectors.store import DATA_DIR, Store; print(DATA_DIR, Store().root)"
    out = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.split()
    if out != [str(tmp_path), str(tmp_path)]:
        raise AssertionError(out)
    unset = {k: v for k, v in os.environ.items() if k != "INFRAQUE_DATA_DIR"}
    out = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT, env=unset, check=True
    ).stdout.split()
    if out != [str(ROOT / "data")] * 2:
        raise AssertionError(f"unset must keep today's default: {out}")


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
    # The natural-person pass runs on every tick (legal audit L-5), so a newly loaded
    # person-sponsored record does not keep a street address until someone runs the CLI.
    if set(report.get("personal_data", {})) != {"classify", "redact"}:
        raise AssertionError(report)


def test_resolver_frames_are_only_sources_the_loader_loads(tmp_path: pathlib.Path) -> None:
    """docs/25 §3.9: `_latest_proposal_frames` applies the loader's own refusal rule. A gated reuse
    class (`registry.status()` already said `gated`) and a `publication: none` source (which only
    the loader refused before) contribute no frame; a loadable one does."""
    import pandas as pd
    import yaml

    from pipeline.connectors.registry import Registry
    from pipeline.connectors.store import Store
    from services.ingest.loader import load_refusal

    doc = yaml.safe_load((ROOT / "data" / "sources.yaml").read_text(encoding="utf-8"))
    loadable, gated, unpublished = (
        "us.iso.ercot.gen_queue",
        "us.va.deq.data_center_air_sites",
        "us.epa.echo.icis_air",
    )
    for entry in doc["sources"]:
        if entry["id"] == gated:
            entry["reuse"] = "restricted"
        elif entry["id"] == unpublished:
            entry["publication"] = "none"
    manifest = tmp_path / "sources.yaml"
    manifest.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    registry = Registry(manifest)

    store = Store(tmp_path / "data")
    for source_id in (loadable, gated, unpublished):
        frame = pd.DataFrame({"source_id": [source_id], "source_record_id": ["1"]})
        path = store.write_parquet(store.normalized_path(source_id, "20260929T060000Z"), frame)
        store.write_run(source_id, "20260929T060000Z", {"status": "ok", "outputs": {"normalized": str(path)}})

    frames = jobs._latest_proposal_frames(tmp_path / "data", registry=registry)
    seen = sorted(str(s) for f in frames for s in f["source_id"].unique())
    if seen != [loadable]:
        raise AssertionError(seen)
    if load_refusal(registry.get(loadable)) is not None:
        raise AssertionError(load_refusal(registry.get(loadable)))
    for source_id in (gated, unpublished):
        if not load_refusal(registry.get(source_id)):
            raise AssertionError(f"{source_id} should be refused by the loader")


def test_execution_lock_is_per_source_and_shared_by_fetch_and_load() -> None:
    if cadence.execution_lock_for("us.iso.ercot.gen_queue") != "source:us-iso-ercot-gen-queue":
        raise AssertionError(cadence.execution_lock_for("us.iso.ercot.gen_queue"))


# ---------------------------------------------------------------- DQ hold release (2026-09-27)
@dataclass
class _Released:  # shaped like pipeline.connectors.runner.ReleaseResult
    run: dict[str, Any]
    ts: str
    already_released: bool


def _held_row(factory: _Factory, run_id: str) -> None:
    jobs.record_source_run(
        factory,
        SOURCE_ID,
        {
            "id": run_id,
            "status": "partial",
            "dq_status": "fail",
            "started_at": "2026-09-18T03:07:00+00:00",
            "finished_at": "2026-09-18T03:07:09+00:00",
        },
    )


def test_release_held_job_promotes_the_run_and_marks_its_row_ok(
    tmp_path: pathlib.Path, factory: _Factory
) -> None:
    run_id = "00000000-0000-4000-8000-0000000000d1"
    _held_row(factory, run_id)
    calls: list[tuple[str, str, str]] = []

    def fake_release(source_id: str, rid: str, *, released_by: str, store: Any) -> _Released:
        calls.append((source_id, rid, released_by))
        record = {
            "id": rid,
            "status": "ok",
            "rows_new": 0,
            "rows_changed": 3,
            "rows_gone": 50,
            "events_emitted": 53,
        }
        return _Released(run=record, ts="20260918T030700Z", already_released=False)

    report = jobs.release_held_job(
        SOURCE_ID,
        run_id,
        "usr_OPERATOR",
        _release=fake_release,
        _session_factory=factory,
        _data_root=tmp_path,
    )
    if calls != [(SOURCE_ID, run_id, "usr_OPERATOR")]:
        raise AssertionError(calls)
    if report["ts"] != "20260918T030700Z" or report["rows_gone"] != 50 or not report["row_marked"]:
        raise AssertionError(report)
    with factory() as session:
        row = session.scalar(select(SourceRun))
        if row is None or row.status != "ok" or (row.rows_gone, row.events_emitted) != (50, 53):
            raise AssertionError(row and (row.status, row.rows_gone, row.events_emitted))


def test_a_refused_release_fails_the_job_and_leaves_the_row_held(
    tmp_path: pathlib.Path, factory: _Factory
) -> None:
    from pipeline.connectors.runner import ReleaseRefused

    run_id = "00000000-0000-4000-8000-0000000000d2"
    _held_row(factory, run_id)

    def refuse(source_id: str, rid: str, **kw: Any) -> _Released:
        raise ReleaseRefused("superseded", "a later run has output")

    with pytest.raises(jobs.HoldReleaseRefused, match="superseded"):
        jobs.release_held_job(
            SOURCE_ID, run_id, "usr_OPERATOR", _release=refuse, _session_factory=factory, _data_root=tmp_path
        )
    with factory() as session:
        row = session.scalar(select(SourceRun))
        if row is None or row.status != "partial":
            raise AssertionError(row and row.status)


def test_the_release_task_enqueues_the_load_the_hold_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    deferred: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(scheduler_app, "load_source", _FakeTask("load_source", deferred))
    monkeypatch.setattr(
        jobs, "release_held_job", lambda source_id, run_id, released_by: {"ts": "20260918T030700Z"}
    )
    scheduler_app.release_held_run.func(SOURCE_ID, "run-1", "usr_OPERATOR")
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
    task = scheduler_app.app.tasks["release_held_run"]
    if task.queue != "normalise":
        raise AssertionError(task.queue)
    if scheduler_app.release_queueing_lock_for("a.b c") != "release:a-b-c":
        raise AssertionError(scheduler_app.release_queueing_lock_for("a.b c"))


# ---------------------------------------------------------------- release-aware annual tick
def test_the_annual_tick_fetches_only_the_sources_whose_run_month_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    def at(month: int) -> int:
        return int(dt.datetime(2027, month, 2, 7, 37, tzinfo=dt.UTC).timestamp())

    january = {s["id"] for s in scheduler_app.due_sources("annual", at(1))}
    october = {s["id"] for s in scheduler_app.due_sources("annual", at(10))}
    november = {s["id"] for s in scheduler_app.due_sources("annual", at(11))}
    if "us.epa.ghgrp" in january or "us.eia.860" in january:
        raise AssertionError("GHGRP and EIA-860 must not run in January, before their data exists")
    if "us.eia.860" not in october or "us.epa.ghgrp" not in november:
        raise AssertionError((sorted(october), sorted(november)))
    if "us.census.cartographic_boundaries" not in january:
        raise AssertionError("an annual source without release_month keeps the January default")
    weekly = {s["id"] for s in scheduler_app.due_sources("weekly", at(10))}
    if SOURCE_ID in weekly or "us.iso.caiso.gen_queue" not in weekly:
        raise AssertionError("non-annual buckets are unaffected by the month")


def test_a_malformed_release_month_falls_back_to_january_without_stopping_the_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    sources = [
        {"id": "a", "cadence": "annual", "release_month": "Oct"},
        {"id": "b", "cadence": "annual", "release_month": 9},
    ]
    monkeypatch.setattr(scheduler_app, "_load_sources", lambda: sources)
    january = int(dt.datetime(2027, 1, 2, tzinfo=dt.UTC).timestamp())
    october = int(dt.datetime(2027, 10, 2, tzinfo=dt.UTC).timestamp())
    if [s["id"] for s in scheduler_app.due_sources("annual", january)] != ["a"]:
        raise AssertionError("malformed release_month -> default month")
    if [s["id"] for s in scheduler_app.due_sources("annual", october)] != ["b"]:
        raise AssertionError("the valid source still runs in its month")
