"""The daily metrics snapshot on the scheduler (lane metrics-recorder; docs/24-trends.md): `tick_trends`
runs hourly at a minute no other tick uses and asks for the `daily` run at 05:58 and a `fill` run at
every other hour; the job writes today's document in the current schema, a `fill` only when it is
missing after 05:58, every run sweeps what a killed write left, and an object store that cannot be
reached never fails it. No Postgres: an in-memory SQLite store and fake deferrers and buckets, as in
`test_retention_tick.py`."""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import time
from typing import Any

import pytest
import yaml

from infra.scheduler import jobs
from infra.scheduler.cadence import CRON_BY_BUCKET
from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal
from services.db.session import get_engine, get_sessionmaker, init_db, session_scope
from services.trends.store import Mirror, MirrorConfig, TrendStore

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


# ------------------------------------------------------------------------------ the schedule
def test_the_tick_is_hourly_at_a_minute_no_other_tick_uses(scheduler_app: Any) -> None:
    periodic = scheduler_app.app.periodic_registry.periodic_tasks
    key = ("tick_trends", "tick:trends")
    tick = periodic[key]
    assert tick.cron == scheduler_app.TRENDS_CRON == "58 * * * *"
    assert tick.task.queue == scheduler_app.SCHEDULER_ONLY_QUEUE
    others = [p.cron for k, p in periodic.items() if k != key]
    assert others, "the other ticks are registered"
    used = set().union(*(_minutes(cron) for cron in [*others, *CRON_BY_BUCKET.values()]))
    assert not _minutes(tick.cron) & used


def test_the_job_runs_on_a_consumed_queue_under_its_own_lock(scheduler_app: Any) -> None:
    task = scheduler_app.app.tasks["trends_tick"]
    assert (task.queue, task.queueing_lock, task.retry_strategy) == ("audit", "trends_tick", None)
    worker = yaml.safe_load((ROOT / "infra" / "compose" / "docker-compose.yml").read_text())["services"][
        "worker"
    ]
    consumed = set(worker["command"][worker["command"].index("--queues") + 1].split(","))
    assert task.queue in consumed


def _at(hour: int, minute: int = 58, day: int = 24) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("hour", "mode"), [(5, "daily"), (6, "fill"), (4, "fill"), (0, "fill"), (23, "fill")]
)
def test_the_daily_run_is_the_0558_tick_and_every_other_hour_fills(hour: int, mode: str) -> None:
    assert jobs.trends_mode_for(int(_at(hour).timestamp())) == mode


def test_the_tick_defers_its_mode_and_tolerates_an_overlap(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, scheduler_app: Any
) -> None:
    import procrastinate

    calls: list[str] = []

    class _Deferrer:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        def defer(self, *, mode: str) -> None:
            calls.append(mode)
            if self.fail:
                raise procrastinate.exceptions.AlreadyEnqueued("locked")

    monkeypatch.setattr(scheduler_app, "trends_tick", _Deferrer(fail=False))
    scheduler_app._tick_trends(int(_at(5).timestamp()))
    monkeypatch.setattr(scheduler_app, "trends_tick", _Deferrer(fail=True))
    caplog.set_level("INFO", logger="infra.scheduler")
    scheduler_app._tick_trends(int(_at(9).timestamp()))
    assert calls == ["daily", "fill"]
    assert any("trends_tick still queued" in r.getMessage() for r in caplog.records)


def test_the_task_runs_the_job_under_the_timeout(monkeypatch: pytest.MonkeyPatch, scheduler_app: Any) -> None:
    seen: list[str] = []

    def _job(mode: str) -> dict[str, Any]:
        seen.append(mode)
        return {"mode": mode, "written": True}

    monkeypatch.setattr(jobs, "trends_tick_job", _job)
    assert scheduler_app.app.tasks["trends_tick"](mode="fill") == {"mode": "fill", "written": True}
    assert seen == ["fill"]
    assert scheduler_app.TRENDS_TIMEOUT_S == 900


# --------------------------------------------------------------------------------- the job
def _factory() -> Any:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    factory = get_sessionmaker(engine)
    with session_scope(factory) as db:
        source = make_public_source(db, make_open_licence(db))
        for n in range(3):
            make_visible_proposal(db, source, public_id_suffix=str(n + 1))
    return factory


class _Bucket:
    def __init__(self, down: bool = False) -> None:
        self.down = down
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, *, immutable: bool = False) -> None:
        if self.down:
            raise ConnectionError("endpoint unreachable")
        self.objects[key] = data


def _mirror(bucket: _Bucket) -> MirrorConfig:
    return MirrorConfig(Mirror("beta-object-storage-production", bucket), "configured")


def test_the_daily_run_writes_todays_document_and_reports_counts_only(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    factory = _factory()
    now = dt.datetime.now(UTC)
    caplog.set_level("INFO", logger="infra.scheduler.jobs")
    result = jobs.trends_tick_job("daily", _session_factory=factory, now=now, data_root=tmp_path, env={})
    path = tmp_path / "normalized" / "trends" / "daily" / f"{now.date().isoformat()}.json"
    assert result["written"] is True and result["path"] == str(path)
    assert result["proposals_active"] == 3 and result["sources"] == 1
    assert result["upload"] == {"status": "not_configured", "detail": None, "uploaded": 0}
    document = json.loads(path.read_text())
    assert document["as_of"] == now.date().isoformat()
    assert document["metrics"]["proposals"]["active"]["total"]["records"] == 3
    assert result["bytes"] == path.stat().st_size < 1_000_000
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("trends_tick "))
    assert "proposals_active=3" in line and "upload_status=not_configured" in line
    assert "Test Storage Project" not in line and "Test Storage Project" not in json.dumps(result)


def test_a_fill_writes_only_a_missing_day_after_the_daily_run(tmp_path: pathlib.Path) -> None:
    factory = _factory()
    store = TrendStore(tmp_path)
    early = jobs.trends_tick_job("fill", _session_factory=factory, now=_at(4), data_root=tmp_path, env={})
    assert early["written"] is False and "before the daily run" in early["skipped"]
    assert not store.exists(_at(4).date())
    late = jobs.trends_tick_job("fill", _session_factory=factory, now=_at(14, 3), data_root=tmp_path, env={})
    assert late["written"] is True and store.exists(_at(14).date())
    first = store.read_bytes(_at(14).date())
    again = jobs.trends_tick_job("fill", _session_factory=factory, now=_at(15), data_root=tmp_path, env={})
    assert again["written"] is False and again["skipped"] == "today's document exists"
    assert store.read_bytes(_at(15).date()) == first
    # The next day's daily run writes the next day and replaces nothing else.
    nxt = jobs.trends_tick_job(
        "daily", _session_factory=factory, now=_at(5, day=25), data_root=tmp_path, env={}
    )
    assert nxt["written"] is True and store.days() == [_at(5).date(), _at(5, day=25).date()]


def test_a_daily_run_replaces_the_days_document(tmp_path: pathlib.Path) -> None:
    """A fill after a deploy wrote the day; a daily run the same day (a re-run, or the tick a
    restart delayed) measures again and replaces it."""
    factory = _factory()
    store = TrendStore(tmp_path)
    jobs.trends_tick_job("fill", _session_factory=factory, now=_at(14, 3), data_root=tmp_path, env={})
    assert store.load(_at(14).date())["generated_at"] == "2026-10-24T14:03:00Z"
    jobs.trends_tick_job("daily", _session_factory=factory, now=_at(15, 0), data_root=tmp_path, env={})
    assert store.load(_at(15).date())["generated_at"] == "2026-10-24T15:00:00Z"
    assert [p.name for p in store.daily.iterdir()] == ["2026-10-24.json"]


def test_an_unreachable_object_store_never_fails_the_job_and_the_next_run_uploads(
    tmp_path: pathlib.Path,
) -> None:
    factory = _factory()
    bucket = _Bucket(down=True)
    result = jobs.trends_tick_job(
        "daily", _session_factory=factory, now=_at(5), data_root=tmp_path, _mirror=_mirror(bucket)
    )
    assert result["written"] is True
    assert result["upload"]["status"] == "failed" and result["upload"]["pending"] == 1
    assert TrendStore(tmp_path).exists(_at(5).date()) and bucket.objects == {}
    bucket.down = False
    retry = jobs.trends_tick_job(
        "fill", _session_factory=factory, now=_at(6), data_root=tmp_path, _mirror=_mirror(bucket)
    )
    assert retry["written"] is False and retry["upload"] == {"status": "ok", "uploaded": 1, "pending": 0}
    assert set(bucket.objects) == {"trends/daily/2026-10-24.json"}


def test_a_misconfigured_object_store_is_reported_and_writes_locally(tmp_path: pathlib.Path) -> None:
    factory = _factory()
    env = {"R2_BUCKET": "beta-object-storage-production"}
    result = jobs.trends_tick_job("daily", _session_factory=factory, now=_at(5), data_root=tmp_path, env=env)
    assert result["written"] is True and result["upload"]["status"] == "incomplete"
    tiles = {**env, "R2_TILES_BUCKET": env["R2_BUCKET"]}
    refused = jobs.trends_tick_job(
        "fill", _session_factory=factory, now=_at(7), data_root=tmp_path, env=tiles
    )
    assert refused["upload"]["status"] == "refused"


def test_a_failed_computation_fails_the_job_after_uploading_what_is_held(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.trends.snapshot as snapshot

    factory = _factory()
    jobs.trends_tick_job("daily", _session_factory=factory, now=_at(5, day=23), data_root=tmp_path, env={})

    def _broken(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(snapshot, "compute_snapshot", _broken)
    bucket = _Bucket()
    with pytest.raises(RuntimeError, match="store unavailable"):
        jobs.trends_tick_job(
            "daily", _session_factory=factory, now=_at(5), data_root=tmp_path, _mirror=_mirror(bucket)
        )
    assert set(bucket.objects) == {"trends/daily/2026-10-23.json"}
    assert TrendStore(tmp_path).days() == [_at(5, day=23).date()]


def test_every_run_sweeps_what_a_killed_write_left(tmp_path: pathlib.Path) -> None:
    """A write killed between its temporary file and the rename leaves the file behind; the next
    run removes it once it is a day old, even a `fill` that writes nothing."""
    store = TrendStore(tmp_path)
    store.daily.mkdir(parents=True)
    leftover = store.daily / f".2026-10-23.json.{'e' * 32}.tmp"
    leftover.write_text("{")
    then = time.time() - 2 * 24 * 3600
    os.utime(leftover, (then, then))
    result = jobs.trends_tick_job("fill", _session_factory=_factory(), now=_at(4), data_root=tmp_path, env={})
    assert result["written"] is False and not leftover.exists()


def test_the_document_is_the_current_schema(tmp_path: pathlib.Path) -> None:
    from services.trends.snapshot import SCHEMA_VERSION

    jobs.trends_tick_job("daily", _session_factory=_factory(), now=_at(5), data_root=tmp_path, env={})
    document = TrendStore(tmp_path).load(_at(5).date())
    assert document["schema_version"] == SCHEMA_VERSION == 2
    assert document["metrics"]["proposals"]["withdrawn"]["by_state"] == {}
    assert document["visibility"]["served_not_stored"]["proposals"]["records"] == 0


def test_an_unknown_mode_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="trends_tick mode"):
        jobs.trends_tick_job("weekly", data_root=tmp_path, env={})
