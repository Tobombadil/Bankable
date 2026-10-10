"""DA-10 retention on the scheduler (docs/04 DA-10, "retention runs as a scheduled job with its own run
log"): `tick_retention` defers `retention_tick` daily at a minute no other tick uses, the job runs on a
queue the compose `worker` consumes under its own queueing lock, and the body leaves one run-log row.
No Postgres: the SQLite session factory and fake deferrers, as in `test_loop.py`."""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from infra.scheduler import jobs
from infra.scheduler.cadence import CRON_BY_BUCKET
from services.db.models import Event, UserSession
from services.db.session import get_engine, get_sessionmaker, init_db, session_scope

ROOT = pathlib.Path(__file__).resolve().parents[2]
NOW = dt.datetime(2026, 10, 10, 1, 47, tzinfo=dt.UTC)


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


def test_the_tick_is_daily_at_a_minute_no_other_tick_uses(scheduler_app: Any) -> None:
    periodic = scheduler_app.app.periodic_registry.periodic_tasks
    tick = periodic[("tick_retention", "tick:retention")]
    assert tick.cron == scheduler_app.RETENTION_CRON == "47 1 * * *"
    assert tick.task.queue == scheduler_app.SCHEDULER_ONLY_QUEUE
    others = {key: p.cron for key, p in periodic.items() if key != ("tick_retention", "tick:retention")}
    assert others, "the other ticks are registered"
    used = set().union(*(_minutes(cron) for cron in [*others.values(), *CRON_BY_BUCKET.values()]))
    assert not _minutes(tick.cron) & used


def test_the_job_runs_on_a_consumed_queue_under_its_own_lock(scheduler_app: Any) -> None:
    task = scheduler_app.app.tasks["retention_tick"]
    assert (task.queue, task.queueing_lock, task.retry_strategy) == ("audit", "retention_tick", None)
    worker = yaml.safe_load((ROOT / "infra" / "compose" / "docker-compose.yml").read_text())["services"][
        "worker"
    ]
    consumed = set(worker["command"][worker["command"].index("--queues") + 1].split(","))
    assert task.queue in consumed
    assert scheduler_app.RETENTION_TIMEOUT_S == 1800


def test_the_tick_defers_once_and_tolerates_an_overlap(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, scheduler_app: Any
) -> None:
    import procrastinate

    calls: list[str] = []

    class _Deferrer:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        def defer(self) -> None:
            calls.append("defer")
            if self.fail:
                raise procrastinate.exceptions.AlreadyEnqueued("locked")

    monkeypatch.setattr(scheduler_app, "retention_tick", _Deferrer(fail=False))
    scheduler_app._tick_retention(0)
    monkeypatch.setattr(scheduler_app, "retention_tick", _Deferrer(fail=True))
    caplog.set_level("INFO", logger="infra.scheduler")
    scheduler_app._tick_retention(0)
    assert calls == ["defer", "defer"]
    assert any("retention_tick still queued" in r.getMessage() for r in caplog.records)


def test_the_task_runs_the_job_under_the_timeout(monkeypatch: pytest.MonkeyPatch, scheduler_app: Any) -> None:
    monkeypatch.setattr(scheduler_app, "retention_tick_job", lambda: {"run_id": "r", "errors": 0})
    assert scheduler_app.app.tasks["retention_tick"]() == {"run_id": "r", "errors": 0}


def _factory() -> Any:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


def test_the_job_applies_the_rules_and_leaves_one_run_log_row(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SNAPSHOT_STORE", raising=False)
    factory = _factory()
    from services.db.models import Account, User

    with session_scope(factory) as db:
        account = Account(public_id="acct_1", name="A")
        db.add(account)
        db.flush()
        user = User(public_id="usr_1", account_id=account.id, email="a@example.com")
        db.add(user)
        db.flush()
        created = NOW - dt.timedelta(days=45)
        db.add(UserSession(user_id=user.id, token_hash="a" * 64, created_at=created, expires_at=created))
    caplog.set_level("INFO", logger="infra.scheduler.jobs")
    report = jobs.retention_tick_job(factory, now=NOW, data_root=tmp_path)
    assert report["rules"]["sessions"]["changed"] == 1 and report["errors"] == 0
    with session_scope(factory) as db:
        assert db.scalars(select(UserSession)).all() == []
        rows = db.scalars(select(Event).where(Event.event_type == "retention_run")).all()
        assert len(rows) == 1 and rows[0].after is not None and rows[0].after["run_id"] == report["run_id"]
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("retention_tick "))
    assert "sessions_changed=1" in line and "error_count=0" in line


def test_the_job_fails_loudly_after_the_run_log_when_a_snapshot_could_not_be_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.retention.run as retention

    def fake_run(factory: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "run_id": "r1",
            "dry_run": False,
            "errors": 1,
            "rules": {
                "raw_snapshots": {
                    "status": "applied",
                    "matched": 2,
                    "changed": 1,
                    "errors": ["local:k: OSError"],
                }
            },
        }

    monkeypatch.setattr(retention, "run_retention", fake_run)
    with pytest.raises(jobs.RetentionIncomplete, match="run r1"):
        jobs.retention_tick_job(_factory(), now=NOW, data_root=pathlib.Path("unused"))
