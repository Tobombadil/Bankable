"""The global electricity refresh (`infra/scheduler/global_figures.py`): its schedule, that the generic bucket
ticks skip the row, and the job's path from fetch to load, offline (subprocess and outcome are fakes)."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from typing import Any

import pytest

from infra.scheduler import global_figures, prices
from infra.scheduler.cadence import CRON_BY_BUCKET
from pipeline.connectors.global_ember_electricity import SOURCE_ID


def _minutes(cron: str) -> set[int]:
    field = cron.split()[0]
    if field.startswith("*/"):
        return set(range(0, 60, int(field[2:])))
    return set(range(60)) if field == "*" else {int(m) for m in field.split(",")}


@pytest.fixture()
def scheduler_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")  # never connected
    import infra.scheduler.app as app_module

    return app_module


def test_twice_a_month_after_each_of_embers_update_weeks() -> None:
    minute, hour, day, month, weekday = global_figures.GLOBAL_ELECTRICITY_CRON.split()
    assert (minute, hour, day, month, weekday) == ("38", "9", "8,22", "*", "*")


def test_its_minute_is_used_by_no_other_tick(scheduler_app: Any) -> None:
    periodic = scheduler_app.app.periodic_registry.periodic_tasks
    ours = [p for (_name, pid), p in periodic.items() if pid == global_figures.PERIODIC_ID]
    assert len(ours) == 1 and ours[0].cron == global_figures.GLOBAL_ELECTRICITY_CRON
    others = [p.cron for (_name, pid), p in periodic.items() if pid != global_figures.PERIODIC_ID]
    others += list(CRON_BY_BUCKET.values()) + [t.cron for t in prices.PRICE_TICKS] + [prices.PRICE_STATS_CRON]
    used = set().union(*(_minutes(c) for c in others))
    assert not _minutes(global_figures.GLOBAL_ELECTRICITY_CRON) & used
    assert {"global_electricity_refresh", "tick_global_electricity"} <= set(scheduler_app.app.tasks)


def test_the_generic_ticks_never_queue_the_row(scheduler_app: Any) -> None:
    for bucket in CRON_BY_BUCKET:
        for month in (1, 6, 10):
            at = int(dt.datetime(2026, month, 2, 7, 37, tzinfo=dt.UTC).timestamp())
            assert SOURCE_ID not in {s["id"] for s in scheduler_app.due_sources(bucket, at)}, bucket
    assert global_figures.is_global_source({"id": SOURCE_ID, "runner": "global"})
    assert not global_figures.is_global_source({"id": "x"})


class _Runs:
    """Answers the fetch and the load commands; records what was run."""

    def __init__(
        self, *, fetch_code: int = 0, load_code: int = 0, load_report: dict[str, Any] | None = None
    ) -> None:
        self.fetch_code, self.load_code = fetch_code, load_code
        self.load_report = load_report if load_report is not None else {"source_id": SOURCE_ID, "runs": []}
        self.commands: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(cmd)
        self.envs.append(kwargs.get("env"))
        if "services.global_figures.load" in cmd:
            return subprocess.CompletedProcess(cmd, self.load_code, json.dumps(self.load_report) + "\n", "")
        return subprocess.CompletedProcess(cmd, self.fetch_code, '{"event": "result"}\n', "")


def _outcome(status: str, *, transient: bool = False, run: dict[str, Any] | None = None) -> Any:
    seen: list[dict[str, Any]] = []

    def record(source_id: str, **kwargs: Any) -> dict[str, Any]:
        seen.append({"source_id": source_id, **kwargs})
        return {"status": status, "ts": "20261008T094900Z", "transient": transient, "record": run}

    record.seen = seen  # type: ignore[attr-defined]
    return record


@pytest.mark.parametrize("status", ["ok", "partial", "unchanged"])
def test_a_run_that_may_be_served_is_loaded(status: str) -> None:
    runs, outcome = _Runs(), _outcome(status)
    out = global_figures.global_electricity_job(trigger="schedule", _run=runs, _outcome=outcome)
    assert runs.commands[0][-4:] == [
        "pipeline.connectors.global_ember_electricity",
        "run",
        "--trigger",
        "schedule",
    ]
    assert (
        runs.commands[1][-2:] == ["services.global_figures.load", "--latest"]
        and out["load"]["source_id"] == SOURCE_ID
    )
    assert outcome.seen[0]["source_id"] == SOURCE_ID and outcome.seen[0]["returncode"] == 0
    env = runs.envs[1]
    assert env is not None and "INFRAQUE_DATA_DIR" in env


@pytest.mark.parametrize("status", ["failed", "blocked", "refused"])
def test_a_failed_download_loads_nothing_and_keeps_the_figures(status: str) -> None:
    runs = _Runs(fetch_code=1)
    out = global_figures.global_electricity_job(_run=runs, _outcome=_outcome(status))
    assert len(runs.commands) == 1 and "load" not in out


@pytest.mark.parametrize("status, transient", [("blocked", False), ("failed", True)])
def test_a_download_read_while_the_api_failed_is_loaded(status: str, transient: bool) -> None:
    # The connector reads the inbox whatever the API answered; the run's status still says the API failed.
    run = {"status": status, "outputs": {"datasets": ["yearly/file"]}}
    runs = _Runs(fetch_code=1)
    out = global_figures.global_electricity_job(
        _run=runs, _outcome=_outcome(status, transient=transient, run=run)
    )
    assert runs.commands[1][-2:] == ["services.global_figures.load", "--latest"] and "load" in out
    assert out["status"] == status and out["transient"] is transient  # still retried when transient
    assert global_figures.wrote_datasets(run) and not global_figures.wrote_datasets({"outputs": {}})


def test_a_failed_load_fails_the_job_visibly() -> None:
    runs = _Runs(load_code=1, load_report={"error": "OperationalError: gone"})
    with pytest.raises(global_figures.GlobalLoadFailed, match="OperationalError"):
        global_figures.global_electricity_job(_run=runs, _outcome=_outcome("ok"))


def test_a_fetch_that_times_out_is_recorded_as_a_crash() -> None:
    def times_out(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0), output=b"partial", stderr=b"slow")

    outcome = _outcome("failed", transient=True)
    out = global_figures.global_electricity_job(_run=times_out, _outcome=outcome)
    assert out["transient"] is True and outcome.seen[0]["returncode"] is None
    assert outcome.seen[0]["stderr"] == "slow"


def test_the_task_retries_transient_failures_only(
    scheduler_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from infra.scheduler.jobs import ConnectorRunFailed, TransientConnectorFailure

    task = scheduler_app.app.tasks["global_electricity_refresh"]
    assert task.retry_strategy.retry_exceptions == [TransientConnectorFailure]

    class _Job:
        attempts = 0

    class _Context:
        job = _Job()

    for status, transient, raised in (
        ("ok", False, None),
        ("blocked", False, ConnectorRunFailed),
        ("failed", True, TransientConnectorFailure),
    ):
        monkeypatch.setattr(
            global_figures,
            "global_electricity_job",
            lambda _s=status, _t=transient, **kw: {"status": _s, "transient": _t},
        )
        if raised is None:
            scheduler_app.global_electricity_refresh(_Context())
        else:
            with pytest.raises(raised):
                scheduler_app.global_electricity_refresh(_Context())
