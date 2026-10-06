"""The scheduler queues a generic load only for a source the loader loads (lane FX2, 2026-09-30).

`run_connector` deferred `load_source` after every `ok` fetch, whatever the connector's kind, and the
loader then published a document frame as proposals. FERC eLibrary is on the 15-minute bucket, so
that was one junk load per tick. Now `_defer_load` asks `jobs.load_kind_refusal` first (the loader's
own `kind_refusal`), and a document source is fetched, recorded and diffed with no load queued.

No Postgres: the SQLite session factory, a fake `subprocess.run` and fake deferrers, as in
`test_loop.py`.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from typing import Any, cast

import pytest
from sqlalchemy import select

from infra.scheduler import jobs
from services.db.models import SourceRun
from services.db.session import get_engine, get_sessionmaker, init_db

TS = "20260930T131500Z"
DOCUMENT_SOURCES = ["us.ferc.elibrary", "us.eia.860", "us.epa.ghgrp"]


class _Factory:
    def __init__(self) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        self.sessionmaker = get_sessionmaker(engine)

    def __call__(self) -> Any:
        return self.sessionmaker()


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


class _Ctx:
    class job:  # noqa: N801 - mirrors procrastinate.JobContext.job
        queue = "fetch"
        attempts = 0


def _ok_run(tmp_path: pathlib.Path, source_id: str) -> str:
    """An `ok` run record where the CLI's `result` line points, and that line."""
    record = {
        "id": "8d3b7f1e-4c2a-4b1e-9c3d-00000000f0a2",
        "source_id": source_id,
        "trigger": "schedule",
        "started_at": "2026-09-30T13:15:00+00:00",
        "finished_at": "2026-09-30T13:15:04+00:00",
        "status": "ok",
        "egress_class": "plain",
        "http_status": 200,
        "bytes": 4321,
        "rows_seen": 101,
        "rows_new": 101,
        "rows_changed": 0,
        "rows_gone": 0,
        "events_emitted": 101,
        "worker_seconds": 4.0,
        "dq_status": "pass",
        "dq": {"status": "pass", "checks": []},
        "error": None,
        "error_class": None,
        "attempt": 1,
        "outputs": {},
    }
    path = tmp_path / "runs" / source_id / f"{TS}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return "\n".join(
        json.dumps({"event": e, "source_id": source_id, "status": "ok", "run_path": str(path)})
        for e in ("run finished", "result")
    )


@pytest.mark.parametrize("source_id", DOCUMENT_SOURCES)
def test_a_successful_fetch_of_a_document_source_queues_no_load(
    source_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    factory = _Factory()
    stdout = _ok_run(tmp_path, source_id)
    deferred: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    )
    monkeypatch.setattr(scheduler_app, "load_source", _FakeTask("load_source", deferred))
    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)

    scheduler_app.run_connector.func(cast(Any, _Ctx()), source_id)

    assert deferred == [], deferred
    with factory() as session:  # the fetch itself is still recorded
        run = session.scalar(select(SourceRun))
        assert run is not None and run.status == "ok"


def test_a_successful_fetch_of_a_proposal_source_still_queues_its_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    factory = _Factory()
    stdout = _ok_run(tmp_path, "us.iso.ercot.gen_queue")
    deferred: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    )
    monkeypatch.setattr(scheduler_app, "load_source", _FakeTask("load_source", deferred))
    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)

    scheduler_app.run_connector.func(cast(Any, _Ctx()), "us.iso.ercot.gen_queue")

    assert [(d[0], d[2]) for d in deferred] == [
        ("load_source", {"source_id": "us.iso.ercot.gen_queue", "ts": TS})
    ], deferred


def test_a_released_hold_of_a_document_source_queues_no_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    deferred: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(scheduler_app, "load_source", _FakeTask("load_source", deferred))
    monkeypatch.setattr(jobs, "release_held_job", lambda source_id, run_id, released_by: {"ts": TS})
    scheduler_app.release_held_run.func("us.ferc.elibrary", "run-1", "usr_OPERATOR")
    assert deferred == [], deferred


@pytest.mark.parametrize(
    ("source_id", "refused"),
    [
        ("us.ferc.elibrary", True),
        ("us.eia.860", True),
        ("us.epa.ghgrp", True),
        # A document source with its own loader (lane R1): queued, loaded by services.ingest.retirements.
        ("us.eia.860m.retirements", False),
        ("us.iso.ercot.gen_queue", False),
        ("eu.ted.api", False),
    ],
)
def test_load_kind_refusal_follows_the_connectors_declared_kind(source_id: str, refused: bool) -> None:
    assert bool(jobs.load_kind_refusal(source_id)) is refused


def test_a_kind_refusal_inside_the_load_job_is_logged_and_returned_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt and braces: a load queued before this change (or by hand) for a document source."""
    from services.ingest.loader import KindRefused

    def refusing_load(session: Any, source_id: str, ts: str, **kw: Any) -> Any:
        raise KindRefused(f"{source_id}: connector kind='document'")

    monkeypatch.setattr(jobs, "build_session_factory", lambda: _Factory())
    report = jobs.load_source_job("us.ferc.elibrary", TS, _load=refusing_load)
    assert report == {"source_id": "us.ferc.elibrary", "ts": TS, "skipped": "kind refused"}
