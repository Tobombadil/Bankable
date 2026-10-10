"""The two passes a deployed store had no scheduled path for until 2026-10-09 (docs/64 §7):
proposal-opportunity matches (`match_tick`, chained after every enrich pass) and the context layers
(`tick_context` -> `context_build` -> `context_load`, monthly). No Postgres: the SQLite session
factory, fake builder processes and fake task deferrers, as in `test_loop.py`."""

from __future__ import annotations

import pathlib
import subprocess
from typing import Any

import pytest

from infra.scheduler import jobs
from services.db.session import get_engine, get_sessionmaker, init_db

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _Factory:
    def __init__(self) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        self.sessionmaker = get_sessionmaker(engine)

    def __call__(self) -> Any:
        return self.sessionmaker()


class _FakeTask:
    def __init__(self, name: str, deferred: list[str]) -> None:
        self.name, self.deferred = name, deferred

    def defer(self, **kwargs: Any) -> int:
        self.deferred.append(self.name)
        return len(self.deferred)


@pytest.fixture()
def scheduler_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")  # never connected
    import infra.scheduler.app as app_module

    return app_module


def test_enrich_chains_to_match_and_build_chains_to_load(
    monkeypatch: pytest.MonkeyPatch, scheduler_app: Any
) -> None:
    deferred: list[str] = []
    real_enrich, real_build = scheduler_app.enrich_tick, scheduler_app.context_build
    monkeypatch.setattr(scheduler_app, "match_tick", _FakeTask("match_tick", deferred))
    monkeypatch.setattr(scheduler_app, "context_load", _FakeTask("context_load", deferred))
    monkeypatch.setattr(jobs, "enrich_tick_job", lambda: {"ok": True})
    monkeypatch.setattr(jobs, "context_build_job", lambda: {"ok": 13})
    real_enrich.func()  # the task bodies, without a worker
    real_build.func()
    assert deferred == ["match_tick", "context_load"]


def test_the_new_tasks_run_on_queues_the_worker_consumes_under_the_store_wide_lock(
    scheduler_app: Any,
) -> None:
    import yaml

    tasks = scheduler_app.app.tasks
    worker = yaml.safe_load((ROOT / "infra" / "compose" / "docker-compose.yml").read_text())["services"][
        "worker"
    ]
    consumed = set(worker["command"][worker["command"].index("--queues") + 1].split(","))
    for name, queue, lock in (
        ("match_tick", "resolve", "resolve"),
        ("context_load", "resolve", "resolve"),
        ("context_build", "fetch", "context_build"),
    ):
        task = tasks[name]
        assert (task.queue, task.lock, task.queueing_lock) == (queue, lock, name)
        assert task.queue in consumed, (name, task.queue)
    assert tasks["context_load"].retry_strategy is scheduler_app.LOAD_RETRY
    periodic = scheduler_app.app.periodic_registry.periodic_tasks
    assert ("tick_context", "tick:context") in periodic
    assert periodic[("tick_context", "tick:context")].cron == scheduler_app.CONTEXT_CRON == "43 2 3 * *"


def test_match_tick_runs_the_matcher_and_reports_counts_only() -> None:
    """A first run on a store has no watermark, so the matcher runs in full mode; the report is the
    matcher's counts without the new rows' ids."""
    report = jobs.match_tick_job(_session_factory=_Factory())
    assert report["mode"] == "full" and report["added"] == 0 and report["active_total"] == 0
    assert "added_ids" not in report
    # The run moved the watermark: the next one is incremental.
    factory = _Factory()
    jobs.match_tick_job(_session_factory=factory)
    assert jobs.match_tick_job(_session_factory=factory)["mode"] == "incremental"


def test_context_build_runs_every_builder_and_survives_a_failure_and_a_timeout() -> None:
    calls: list[tuple[list[str], Any, int]] = []

    def fake_run(
        cmd: list[str], *, cwd: Any, timeout: int, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, cwd, timeout))
        module = cmd[2]
        if module.endswith("eia_owners"):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="FileNotFoundError: no snapshot")
        if module.endswith("phmsa"):
            raise subprocess.TimeoutExpired(cmd, timeout, stderr=b"still downloading")
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    report = jobs.context_build_job(_run=fake_run, timeout_s=7)
    assert [c[0][1:] for c in calls] == [["-m", *argv] for argv in jobs.CONTEXT_BUILDERS]
    assert {c[1] for c in calls} == {jobs.ROOT} and {c[2] for c in calls} == {7}
    assert report["builders"] == len(jobs.CONTEXT_BUILDERS)
    assert report["ok"] == len(jobs.CONTEXT_BUILDERS) - 2
    assert report["failed"] == ["eia_owners: exit 1", "phmsa: timeout"]


def test_the_builders_are_the_ones_whose_files_the_load_reads() -> None:
    """Every file `context_load` reads directly has a builder in the monthly run, except GLEIF's
    parent links (an operator run, `jobs.CONTEXT_BUILDERS` comment); and every builder module
    exists, so a rename cannot leave the tick running a module that is gone."""
    import importlib.util

    from services.ingest import context_layers

    modules = {argv[0] for argv in jobs.CONTEXT_BUILDERS}
    for module in modules:
        assert importlib.util.find_spec(module) is not None, module
    builder_for = {
        "us.eia.860m.plants.parquet": "eia_plants",
        "us.eia.atlas.gas_pipelines.parquet": "eia_atlas",
        "us.eia.atlas.gas_processing_plants.parquet": "eia_atlas",
        "us.eia.atlas.gas_storage.parquet": "eia_atlas",
        "us.eia.atlas.lng_terminals.parquet": "eia_atlas",
        "us.epa.lmop.parquet": "lmop",
        "us.epa.agstar.parquet": "agstar",
        "us.lbnl.ferc_hifld_transmission_lines.parquet": "lbnl_transmission",
        "us.eia.atlas.ethanol_plants.parquet": "ethanol_plants",
        "us.eia.ethanol_capacity.parquet": "ethanol_capacity",
        "us.eia.860.owners.parquet": "eia_owners",
        "us.epa.ghgrp.parquet": "ghgrp",
        "global.gleif.lei.parents.parquet": None,
    }
    assert set(builder_for) == set(context_layers.expected_files())
    for name, builder in builder_for.items():
        if builder is not None:
            assert f"pipeline.context.{builder}" in modules, name


def test_context_load_reads_the_connector_data_root_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen: list[pathlib.Path] = []

    def fake_load(session: Any, data_root: pathlib.Path) -> dict[str, Any]:
        seen.append(data_root)
        return {"files_loaded": 0}

    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(tmp_path))
    assert jobs.context_load_job(fake_load, _session_factory=_Factory()) == {"files_loaded": 0}
    assert seen == [tmp_path]


def test_context_load_on_an_empty_data_root_reports_every_file_missing(tmp_path: pathlib.Path) -> None:
    """The real loader over a data root with no context file: nothing fails, every expected file is
    reported missing, and the curated organisation files (which ship with the code) still apply."""
    from services.ingest import context_layers

    report = jobs.context_load_job(_session_factory=_Factory(), _data_root=tmp_path)
    assert report["files_loaded"] == 0
    assert report["files_missing"] == context_layers.expected_files()


def test_the_task_bodies_call_their_jobs_and_an_already_queued_successor_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, scheduler_app: Any
) -> None:
    import procrastinate

    real_build, real_enrich = scheduler_app.context_build, scheduler_app.enrich_tick
    tick = scheduler_app.app.tasks["tick_context"]
    monkeypatch.setattr(jobs, "match_tick_job", lambda: {"mode": "incremental"})
    monkeypatch.setattr(jobs, "context_load_job", lambda: {"files_loaded": 12})
    monkeypatch.setattr(jobs, "enrich_tick_job", lambda: {"ok": True})
    monkeypatch.setattr(jobs, "context_build_job", lambda: {"ok": 13})
    assert scheduler_app.match_tick.func() == {"mode": "incremental"}
    assert scheduler_app.context_load.func() == {"files_loaded": 12}

    deferred: list[str] = []
    monkeypatch.setattr(scheduler_app, "context_build", _FakeTask("context_build", deferred))
    tick.func(timestamp=0)
    assert deferred == ["context_build"]

    class _Queued(_FakeTask):
        def defer(self, **kwargs: Any) -> int:
            raise procrastinate.exceptions.AlreadyEnqueued("queued")

    # The successor still queued from an earlier run: logged and dropped, the job still succeeds.
    for name in ("context_build", "context_load", "match_tick"):
        monkeypatch.setattr(scheduler_app, name, _Queued(name, deferred))
    tick.func(timestamp=0)
    assert real_build.func() == {"ok": 13}
    assert real_enrich.func() == {"ok": True}
    assert deferred == ["context_build"]
