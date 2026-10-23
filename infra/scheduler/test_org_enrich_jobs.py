"""The monthly company-facts pass (lane org-enrich, 2026-10-22): `tick_org_enrich` ->
`org_enrich_fetch` (network only, no store-wide lock) -> `org_enrich_apply` (store-wide lock),
with the decisions file between them under the connector data root. No Postgres and no network:
a SQLite file store and the recorded Wikidata/SEC answers of `services/ingest/test_org_enrich.py`."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from infra.scheduler import jobs
from services.db.models import Organization
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest import org_enrich as oe
from services.ingest.test_org_enrich import FIXTURE, build_store

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _Factory:
    def __init__(self, path: pathlib.Path) -> None:
        engine = get_engine(f"sqlite+pysqlite:///{path}")
        init_db(engine)
        self.sessionmaker = get_sessionmaker(engine)

    def __call__(self) -> Any:
        return self.sessionmaker()


@pytest.fixture()
def store(tmp_path: pathlib.Path) -> _Factory:
    factory = _Factory(tmp_path / "store.db")
    with factory() as session:
        build_store(session)
    return factory


def test_fetch_writes_the_decisions_file_and_reports_counts_only(
    tmp_path: pathlib.Path, store: _Factory
) -> None:
    report = jobs.org_enrich_fetch_job(
        oe.RecordedTransport(FIXTURE), _session_factory=store, _data_root=tmp_path
    )
    path = tmp_path / "normalized" / "context" / oe.DECISIONS_FILENAME
    assert jobs.org_enrich_decisions_path(tmp_path) == path and path.exists()
    assert report["targets"] == 43
    assert report["matched_by_rule"] == {"lei": 5, "sec_cik": 12, "name_country": 11}
    # The job result and its log line carry counts, never an organisation's name.
    assert "NRG" not in json.dumps(report) and "below_threshold_top" not in report
    assert len(oe.read_decisions(path).decisions) == 43
    with store() as session:  # the fetch wrote nothing to the store
        assert all("profile" not in (o.ids or {}) for o in session.query(Organization))


def test_apply_writes_the_decisions_and_a_second_apply_changes_nothing(
    tmp_path: pathlib.Path, store: _Factory
) -> None:
    jobs.org_enrich_fetch_job(oe.RecordedTransport(FIXTURE), _session_factory=store, _data_root=tmp_path)
    first = jobs.org_enrich_apply_job(_session_factory=store, _data_root=tmp_path)
    assert first["written"] == 28 and first["changed"] == 28
    second = jobs.org_enrich_apply_job(_session_factory=store, _data_root=tmp_path)
    assert second["written"] == 28 and second["changed"] == 0 and second["websites_written"] == 0
    with store() as session:
        profiled = {o.name_canonical for o in session.query(Organization) if "profile" in (o.ids or {})}
    assert "NRG Energy, Inc." in profiled and "Warren Buffett" not in profiled


def test_apply_without_a_fetch_fails_loudly(tmp_path: pathlib.Path, store: _Factory) -> None:
    with pytest.raises(FileNotFoundError):
        jobs.org_enrich_apply_job(_session_factory=store, _data_root=tmp_path)


@pytest.fixture()
def scheduler_app(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> Any:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")  # never connected
    monkeypatch.setattr(jobs, "build_session_factory", lambda: _Factory(tmp_path / "lock.db"))
    import infra.scheduler.app as app_module

    return app_module


def test_the_tasks_run_monthly_on_queues_the_worker_consumes(scheduler_app: Any) -> None:
    import yaml

    tasks = scheduler_app.app.tasks
    worker = yaml.safe_load((ROOT / "infra" / "compose" / "docker-compose.yml").read_text())["services"][
        "worker"
    ]
    consumed = set(worker["command"][worker["command"].index("--queues") + 1].split(","))
    fetch, apply = tasks["org_enrich_fetch"], tasks["org_enrich_apply"]
    assert (fetch.queue, fetch.lock, fetch.queueing_lock) == ("fetch", "org_enrich_fetch", "org_enrich_fetch")
    assert (apply.queue, apply.lock, apply.queueing_lock) == ("resolve", "resolve", "org_enrich_apply")
    assert {fetch.queue, apply.queue} <= consumed
    assert apply.retry_strategy is scheduler_app.LOAD_RETRY
    periodic = scheduler_app.app.periodic_registry.periodic_tasks
    assert (
        periodic[("tick_org_enrich", "tick:org_enrich")].cron == scheduler_app.ORG_ENRICH_CRON == "17 6 4 * *"
    )


class _FakeTask:
    def __init__(self, name: str, deferred: list[str], *, queued: bool = False) -> None:
        self.name, self.deferred, self.queued = name, deferred, queued

    def defer(self, **kwargs: Any) -> int:
        import procrastinate

        if self.queued:
            raise procrastinate.exceptions.AlreadyEnqueued("queued")
        self.deferred.append(self.name)
        return len(self.deferred)


def test_the_tick_defers_the_fetch_and_the_fetch_chains_to_the_apply(
    monkeypatch: pytest.MonkeyPatch, scheduler_app: Any
) -> None:
    deferred: list[str] = []
    real_fetch, real_apply = scheduler_app.org_enrich_fetch, scheduler_app.org_enrich_apply
    monkeypatch.setattr(jobs, "org_enrich_fetch_job", lambda: {"targets": 3})
    monkeypatch.setattr(jobs, "org_enrich_apply_job", lambda: {"written": 2})
    monkeypatch.setattr(scheduler_app, "org_enrich_fetch", _FakeTask("org_enrich_fetch", deferred))
    scheduler_app.app.tasks["tick_org_enrich"].func(timestamp=0)
    monkeypatch.setattr(scheduler_app, "org_enrich_apply", _FakeTask("org_enrich_apply", deferred))
    assert real_fetch.func() == {"targets": 3}
    assert deferred == ["org_enrich_fetch", "org_enrich_apply"]
    assert real_apply.func() == {"written": 2}
    # A successor still queued from an earlier run is logged and dropped, not an error.
    monkeypatch.setattr(
        scheduler_app, "org_enrich_apply", _FakeTask("org_enrich_apply", deferred, queued=True)
    )
    monkeypatch.setattr(
        scheduler_app, "org_enrich_fetch", _FakeTask("org_enrich_fetch", deferred, queued=True)
    )
    assert real_fetch.func() == {"targets": 3}
    scheduler_app.app.tasks["tick_org_enrich"].func(timestamp=0)
    assert deferred == ["org_enrich_fetch", "org_enrich_apply"]
