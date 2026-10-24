"""The history ticks (`infra/scheduler/history.py`): the generic bucket ticks leave `runner: history`
sources alone, each history source has one weekly tick in a minute no other tick uses, and the job
runs the history CLI and records its outcome like any connector's."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from typing import Any

import pytest

from infra.scheduler import history
from infra.scheduler.cadence import CRON_BY_BUCKET


def _minutes(cron: str) -> set[int]:
    field = cron.split()[0]
    if field.startswith("*/"):
        return set(range(0, 60, int(field[2:])))
    return set(range(60)) if field == "*" else {int(m) for m in field.split(",")}


def test_every_history_source_has_a_tick_and_the_bucket_ticks_skip_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    rows = [s for s in scheduler_app._load_sources() if history.is_history_source(s)]
    assert {s["id"] for s in rows} == {t.source_id for t in history.HISTORY_TICKS}
    timestamp = int(dt.datetime(2026, 11, 1, 5, 21, tzinfo=dt.UTC).timestamp())
    for bucket in CRON_BY_BUCKET:
        assert not [s for s in scheduler_app.due_sources(bucket, timestamp) if history.is_history_source(s)]
    assert "history_fetch" in scheduler_app.app.tasks


def test_history_ticks_use_minutes_no_other_tick_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    ours = {t.periodic_id: t.cron for t in history.HISTORY_TICKS}
    assert len(ours) == len(history.HISTORY_TICKS)
    others = [
        p.cron
        for (_name, pid), p in scheduler_app.app.periodic_registry.periodic_tasks.items()
        if pid not in ours
    ]
    used = set().union(*(_minutes(c) for c in [*others, *CRON_BY_BUCKET.values()]))
    seen: set[int] = set()
    for pid, cron in ours.items():
        assert len(cron.split()) == 5, pid
        assert cron.split()[4] != "*", pid  # weekly, not daily
        mine = _minutes(cron)
        assert not mine & used, (pid, sorted(mine & used))
        assert not mine & seen, (pid, sorted(mine & seen))
        seen |= mine


def test_each_history_run_holds_the_lock_of_the_register_source_it_reads() -> None:
    from pipeline.connectors.history import connector_class_for

    for tick in history.HISTORY_TICKS:
        module = sys.modules[connector_class_for(tick.source_id).__module__]
        assert tick.reads_from == module.SHARED_SOURCE_ID, tick.source_id


def test_the_job_runs_the_cli_in_auto_mode_and_records_the_outcome() -> None:
    calls: list[list[str]] = []
    outcomes: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert kwargs["timeout"] == history.TIMEOUT_S
        return subprocess.CompletedProcess(cmd, 0, stdout='{"event": "result", "status": "ok"}', stderr="")

    def fake_outcome(source_id: str, **kwargs: Any) -> dict[str, Any]:
        outcomes.append({"source_id": source_id, **kwargs})
        return {"status": "ok", "ts": None, "transient": False}

    out = history.history_fetch_job("us.eia.860m.history", _run=fake_run, _outcome=fake_outcome)
    assert out["status"] == "ok"
    assert calls[0][1:5] == ["-m", "pipeline.connectors.history", "run", "us.eia.860m.history"]
    assert "--mode" in calls[0] and calls[0][calls[0].index("--mode") + 1] == "auto"
    assert outcomes[0]["returncode"] == 0 and outcomes[0]["trigger"] == "schedule"


def test_a_timed_out_run_is_recorded_with_its_timeout() -> None:
    def slow(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, history.TIMEOUT_S, output=b"partial", stderr=None)

    seen: dict[str, Any] = {}

    def fake_outcome(source_id: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "failed", "ts": None, "transient": True}

    out = history.history_fetch_job("us.iso.ercot.gen_queue.history", _run=slow, _outcome=fake_outcome)
    assert out["transient"] is True
    assert (
        seen["returncode"] is None and seen["timeout_s"] == history.TIMEOUT_S and seen["stdout"] == "partial"
    )


def test_the_command_carries_a_run_id_when_given() -> None:
    cmd = history.history_command("us.eia.860m.history", "manual", "00000000-0000-0000-0000-000000000001")
    assert cmd[-2:] == ["--run-id", "00000000-0000-0000-0000-000000000001"]
    assert history.is_history_source({"runner": "history"}) and not history.is_history_source(
        {"runner": "prices"}
    )
