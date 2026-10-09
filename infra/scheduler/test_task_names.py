"""Every task the scheduler queues must be importable by a worker under the name it was queued with.

Procrastinate names an unnamed task after its module. Run as `python -m infra.scheduler.app` (the
module docstring's own command), that module is `__main__`, so an unnamed `run_connector` was queued
as `__main__.run_connector` and every fetch failed in the worker with `TaskNotFound` (2026-10-09,
seen on a local scheduler; the container path, `python -m infra.entrypoint scheduler`, imports the
module under its real name and was not affected)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]

_PROBE = """
import json, runpy, procrastinate
def report(self, **kwargs):
    print(json.dumps(sorted(self.tasks)))
procrastinate.App.run_worker = report
runpy.run_module("infra.scheduler.app", run_name="__main__")
"""


def test_running_the_module_directly_registers_no_task_under_main() -> None:
    env = dict(os.environ, DATABASE_URL="postgresql://probe@127.0.0.1:1/probe")  # never connected
    out = subprocess.run(  # noqa: S603 -- fixed argv
        [sys.executable, "-c", _PROBE], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    ).stdout
    names = json.loads(out.strip().splitlines()[-1])
    assert "run_connector" in names and "load_source" in names
    assert not [name for name in names if name.startswith("__main__")]


def test_a_load_retries_only_a_conflict_with_a_concurrent_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sources' loads run side by side and may create the same organisation; on Postgres the
    loser fails with a unique violation or a deadlock (the 2026-10-09 single-host rehearsal: 2 of
    15 seed loads). Those, and nothing else, are retried with backoff."""
    import sqlalchemy.exc

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")  # never connected
    import procrastinate

    import infra.scheduler.app as scheduler_app

    strategy = scheduler_app.app.tasks["load_source"].retry_strategy
    assert isinstance(strategy, procrastinate.RetryStrategy) and strategy.max_attempts == 2
    assert set(strategy.retry_exceptions or ()) == {
        sqlalchemy.exc.OperationalError,
        sqlalchemy.exc.IntegrityError,
    }
    assert strategy.get_retry_decision(exception=ValueError("a refused frame"), job=_job(0)) is None
    deadlock = sqlalchemy.exc.OperationalError("INSERT ...", {}, Exception("deadlock detected"))
    assert strategy.get_retry_decision(exception=deadlock, job=_job(0)) is not None
    assert strategy.get_retry_decision(exception=deadlock, job=_job(2)) is None  # the third failure stands


def _job(attempts: int) -> Any:
    import procrastinate

    return procrastinate.jobs.Job(
        id=1, queue="normalise", lock=None, queueing_lock=None, task_name="load_source", attempts=attempts
    )
