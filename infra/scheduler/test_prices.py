"""The price sources' own schedule (`infra/scheduler/prices.py`): which ticks exist, that the
generic bucket ticks skip price rows, the first-run backfill, and the fetch job's outcome path."""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
from typing import Any

import pytest

from infra.scheduler import prices
from infra.scheduler.cadence import CRON_BY_BUCKET
from pipeline.connectors.prices import PRICE_CONNECTORS


def test_every_price_source_has_a_daily_tick_and_only_raw_ok_ones_a_latest() -> None:
    by_source: dict[str, set[str]] = {}
    for tick in prices.PRICE_TICKS:
        by_source.setdefault(tick.source_id, set()).add(tick.mode)
    assert set(by_source) == set(PRICE_CONNECTORS)
    assert all("daily" in modes for modes in by_source.values())
    latest = {s for s, modes in by_source.items() if "latest" in modes}
    assert latest == {"us.iso.ercot.prices", "us.iso.spp.prices", "gb.elexon.bmrs_prices"}  # raw-ok only


def _minutes(cron: str) -> set[int]:
    field = cron.split()[0]
    if field.startswith("*/"):
        return set(range(0, 60, int(field[2:])))
    return set(range(60)) if field == "*" else {int(m) for m in field.split(",")}


def test_periodic_ids_are_unique_and_no_minute_is_shared_with_another_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As `test_queue_maintenance.py` asks of its own ticks: a price tick fires in a minute no other
    periodic task (and no bucket tick) uses, nor another price tick."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    ids = [t.periodic_id for t in prices.PRICE_TICKS]
    assert len(ids) == len(set(ids))
    ours = {t.periodic_id: t.cron for t in prices.PRICE_TICKS} | {"tick:price_stats": prices.PRICE_STATS_CRON}
    others = [
        p.cron
        for (_name, pid), p in scheduler_app.app.periodic_registry.periodic_tasks.items()
        if pid not in ours
    ]
    used = set().union(*(_minutes(c) for c in [*others, *CRON_BY_BUCKET.values()]))
    seen: set[int] = set()
    for pid, cron in ours.items():
        assert len(cron.split()) == 5, pid
        mine = _minutes(cron)
        assert not mine & used, (pid, sorted(mine & used))
        assert not mine & seen, (pid, sorted(mine & seen))
        seen |= mine


def test_politeness_of_the_latest_ticks() -> None:
    """Elexon: not more than once a half hour; SPP: hourly; ERCOT: every 15 minutes."""
    crons = {(t.source_id, t.mode): t.cron for t in prices.PRICE_TICKS}
    assert crons[("gb.elexon.bmrs_prices", "latest")].split()[:2] == ["42", "*"]
    assert crons[("us.iso.spp.prices", "latest")].split()[:2] == ["9", "*"]
    assert len(crons[("us.iso.ercot.prices", "latest")].split()[0].split(",")) == 4


def test_the_generic_ticks_skip_price_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    at = int(dt.datetime(2027, 1, 2, 7, 37, tzinfo=dt.UTC).timestamp())
    for bucket in CRON_BY_BUCKET:
        due = {s["id"] for s in scheduler_app.due_sources(bucket, at)}
        assert not due & set(PRICE_CONNECTORS), bucket
    tasks = scheduler_app.app.tasks
    assert {"price_fetch", "price_load", "price_stats"} <= set(tasks)
    assert "tick_price_us-iso-ercot-prices_latest" in tasks and "tick_price_stats" in tasks


def _attempts(runs: list[dict[str, Any]], killed: int = 0) -> set[str]:
    """The attempt markers a store would hold: one per backfill run, plus `killed` backfills that
    died before writing their run record."""
    marks = {f"run{i}" for i, r in enumerate(runs) if r["mode"] == "backfill"}
    return marks | {f"killed{i}" for i in range(killed)}


@pytest.mark.parametrize(
    ("runs", "killed", "expected"),
    [
        ([], 0, "backfill"),
        ([{"mode": "backfill", "status": "failed"}], 0, "backfill"),
        ([{"mode": "backfill", "status": "failed"}] * 2, 1, "daily"),  # three tries used
        ([{"mode": "backfill", "status": "ok"}], 0, "daily"),
        ([{"mode": "backfill", "status": "unchanged"}, {"mode": "daily", "status": "failed"}], 0, "daily"),
    ],
)
def test_a_daily_tick_backfills_until_a_backfill_succeeds_or_its_tries_are_used(
    runs: list[dict[str, Any]], killed: int, expected: str
) -> None:
    got = prices.effective_mode(
        "us.iso.nyiso.prices",
        "daily",
        runs_of=lambda _s: runs,
        attempts_of=lambda _s: _attempts(runs, killed),
    )
    assert got == expected
    assert prices.effective_mode("us.iso.ercot.prices", "latest", runs_of=lambda _s: runs) == "latest"


@pytest.mark.parametrize(
    ("runs", "killed"),
    [
        ([{"mode": "backfill", "status": "failed"}], 0),  # a backfill that ended failed
        ([{"mode": "backfill", "status": "partial"}], 0),  # a DQ hold
        ([], 1),  # a backfill killed before its run record (OOM, timeout): only its marker
    ],
)
def test_ercot_is_never_backfilled_twice_on_its_own(runs: list[dict[str, Any]], killed: int) -> None:
    """Review 2026-10-23: each ERCOT backfill may spend one of a workbook's three downloads a year,
    so after one unsuccessful try the daily tick runs `daily` and an operator decides."""
    assert prices.AUTO_BACKFILL_ATTEMPTS["us.iso.ercot.prices"] == 1
    got = prices.effective_mode(
        "us.iso.ercot.prices",
        "daily",
        runs_of=lambda _s: runs,
        attempts_of=lambda _s: _attempts(runs, killed),
    )
    assert got == "daily"
    assert (
        prices.effective_mode(
            "us.iso.ercot.prices", "daily", runs_of=lambda _s: [], attempts_of=lambda _s: set()
        )
        == "backfill"
    )


def test_a_retried_crashed_backfill_runs_daily() -> None:
    """Procrastinate retries a crashed or timed-out fetch (FETCH_RETRY); the retry of an ERCOT
    backfill whose marker is on the store must not start the backfill again."""
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    out = prices.price_fetch_job(
        "us.iso.ercot.prices",
        "daily",
        trigger="retry",
        attempt=2,
        _run=fake_run,
        _runs_of=lambda _s: [],
        _attempts_of=lambda _s: {"20261023T191700Z"},
        _outcome=lambda _s, **_kw: {"status": "ok", "ts": None, "transient": False},
    )
    assert out["mode"] == "daily" and seen["cmd"][-4:] == ["--mode", "daily", "--trigger", "retry"]


def test_attempts_are_read_from_the_markers_and_old_run_records(tmp_path: pathlib.Path) -> None:
    from pipeline.connectors.prices import store as pstore
    from pipeline.connectors.store import Store

    store = Store(tmp_path)
    assert pstore.backfill_attempts(store, "us.iso.ercot.prices") == set()
    pstore.write_attempt(store, "us.iso.ercot.prices", "20261023T191700Z", {"mode": "backfill"})
    store.write_run("us.iso.ercot.prices", "20261020T191700Z", {"mode": "backfill", "status": "failed"})
    store.write_run("us.iso.ercot.prices", "20261021T191700Z", {"mode": "daily", "status": "ok"})
    assert pstore.backfill_attempts(store, "us.iso.ercot.prices") == {"20261023T191700Z", "20261020T191700Z"}


def test_an_unreadable_store_runs_the_daily_fetch() -> None:
    def broken(_source: str) -> list[dict[str, Any]]:
        raise OSError("store down")

    assert prices.effective_mode("us.iso.nyiso.prices", "daily", runs_of=broken) == "daily"
    assert (
        prices.effective_mode("us.iso.nyiso.prices", "daily", runs_of=lambda _s: [], attempts_of=broken)  # type: ignore[arg-type]
        == "daily"
    )


def test_the_fetch_job_runs_the_cli_and_records_the_outcome() -> None:
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"], seen["timeout"] = cmd, kw["timeout"]
        line = json.dumps({"msg": "result", "status": "ok", "run_path": "/x/runs/s/20261022T190000Z.json"})
        return subprocess.CompletedProcess(cmd, 0, stdout=line + "\n", stderr="")

    def fake_outcome(source_id: str, **kw: Any) -> dict[str, Any]:
        seen["outcome"] = (source_id, kw["returncode"], kw["trigger"])
        return {"status": "ok", "ts": "20261022T190000Z", "transient": False}

    out = prices.price_fetch_job(
        "us.iso.nyiso.prices",
        "daily",
        _run=fake_run,
        _runs_of=lambda _s: [],
        _attempts_of=lambda _s: set(),
        _outcome=fake_outcome,
    )
    assert out == {"status": "ok", "ts": "20261022T190000Z", "transient": False, "mode": "backfill"}
    assert seen["cmd"][-6:] == ["run", "us.iso.nyiso.prices", "--mode", "backfill", "--trigger", "backfill"]
    assert seen["timeout"] == prices.TIMEOUT_S["backfill"]
    assert seen["outcome"] == ("us.iso.nyiso.prices", 0, "backfill")


def test_a_timeout_is_recorded_as_one() -> None:
    def slow(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, kw["timeout"], output=b"partial", stderr=b"")

    def outcome(source_id: str, **kw: Any) -> dict[str, Any]:
        assert (
            kw["returncode"] is None
            and kw["timeout_s"] == prices.TIMEOUT_S["latest"]
            and kw["stdout"] == "partial"
        )
        return {"status": "failed", "ts": None, "transient": True}

    out = prices.price_fetch_job(
        "us.iso.ercot.prices", "latest", _run=slow, _runs_of=lambda _s: [], _outcome=outcome
    )
    assert out["status"] == "failed" and out["mode"] == "latest"


def test_a_price_load_runs_as_its_own_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review 2026-10-23: a load in the worker process kept its memory there; as a process, an
    out-of-memory kill takes the load, not the worker. The worker's session limits travel as
    `PGOPTIONS`."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@db:5432/app")
    monkeypatch.delenv("PGOPTIONS", raising=False)
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"], seen["env"], seen["timeout"] = cmd, kw["env"], kw["timeout"]
        line = json.dumps({"source_id": "us.iso.ercot.prices", "daily_rows": 30, "seconds": 1.2})
        return subprocess.CompletedProcess(cmd, 0, stdout="log line\n" + line + "\n", stderr="")

    report = prices.price_load_job("us.iso.ercot.prices", "20261023T191700Z", _run=fake_run)
    assert report["daily_rows"] == 30
    assert seen["cmd"][1:] == [
        "-m",
        "services.prices.load",
        "us.iso.ercot.prices",
        "--run",
        "20261023T191700Z",
    ]
    assert "statement_timeout" in seen["env"]["PGOPTIONS"] and "lock_timeout" in seen["env"]["PGOPTIONS"]
    assert seen["timeout"] == prices.LOAD_TIMEOUT_S and seen["env"]["INFRAQUE_DATA_DIR"]


def test_a_load_conflict_is_retried_and_a_failure_is_not() -> None:
    import sqlalchemy.exc

    def answer(code: int, error: str) -> Any:
        def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                cmd, code, stdout=json.dumps({"error": error}) + "\n", stderr=""
            )

        return run

    with pytest.raises(sqlalchemy.exc.OperationalError, match="ercot"):  # LOAD_RETRY retries these
        prices.price_load_job("us.iso.ercot.prices", "t", _run=answer(prices.EXIT_RETRYABLE, "deadlock"))
    with pytest.raises(prices.PriceLoadFailed, match="KeyError"):
        prices.price_load_job("us.iso.ercot.prices", "t", _run=answer(1, "KeyError: x"))

    def killed(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, -9, stdout="", stderr="")

    with pytest.raises(prices.PriceLoadFailed, match="-9"):
        prices.price_load_job("us.iso.ercot.prices", "t", _run=killed)
    from services.prices import load

    assert load.EXIT_RETRYABLE == prices.EXIT_RETRYABLE


def test_is_price_source() -> None:
    assert prices.is_price_source({"id": "x", "runner": "prices"})
    assert not prices.is_price_source({"id": "x"})
