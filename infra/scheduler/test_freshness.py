"""Source freshness (audit 2026-09-30 data engineer F2): every source row read `health = ok` while
nine implemented sources were 16.7 days stale. Pure assessment, the poll override, the store
report and the `freshness_tick` ops signal."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from infra.scheduler import jobs
from infra.scheduler.cadence import bucket_for_cadence, is_due, poll_cadence
from infra.scheduler.freshness import ALLOWANCE, assess, last_success_from_runs, store_report
from services.db.models import Source
from services.db.session import get_engine, get_sessionmaker, init_db

NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)


def test_assess_against_the_poll_allowance() -> None:
    day = dt.timedelta(days=1)
    assert assess("weekly", NOW - 3 * day, NOW).status == "fresh"
    assert assess("weekly", NOW - 10 * day, NOW).status == "late"
    # the audit's case: a weekly queue last fetched 2026-09-13 is 23.6 days old on 2026-10-07
    stale = assess("weekly", dt.datetime(2026, 9, 13, 20, 25, tzinfo=dt.UTC), NOW)
    assert stale.status == "stale" and stale.alert
    assert stale.allowance_hours == 192.0 and stale.stale_after_hours == 384.0
    assert assess("realtime", NOW - dt.timedelta(hours=3), NOW).status == "stale"
    assert assess("daily", None, NOW).status == "never"
    assert assess("daily", NOW - 30 * day, NOW, paused=True).status == "paused"
    assert not assess("daily", NOW - 30 * day, NOW, scheduled=False).alert
    # annual sources have a year and a run month's slack
    assert assess("annual", NOW - 300 * day, NOW).status == "fresh"


def test_every_bucket_has_an_allowance() -> None:
    from infra.scheduler.cadence import CRON_BY_BUCKET

    assert set(ALLOWANCE) == set(CRON_BY_BUCKET)


def test_the_poll_override_moves_the_bucket() -> None:
    ferc: dict[str, Any] = {"id": "us.ferc.elibrary", "cadence": "realtime", "poll": "daily"}
    assert poll_cadence(ferc) == "daily"
    assert is_due(ferc, "daily", 10) and not is_due(ferc, "15min", 10)
    assert bucket_for_cadence("hourly").bucket == "hourly"
    assert poll_cadence({"cadence": "weekly"}) == "weekly"


def test_the_manifest_polls_the_heavy_realtime_sources_less_often() -> None:
    import yaml

    doc = yaml.safe_load((Path(__file__).resolve().parents[2] / "data" / "sources.yaml").read_text())
    by_id = {s["id"]: s for s in doc["sources"]}
    assert bucket_for_cadence(poll_cadence(by_id["us.ferc.elibrary"])).bucket == "daily"
    assert bucket_for_cadence(poll_cadence(by_id["gb.find_a_tender"])).bucket == "hourly"
    assert bucket_for_cadence(poll_cadence(by_id["us.grants_gov.search2"])).bucket == "hourly"
    for s in doc["sources"]:
        if s.get("poll"):
            assert bucket_for_cadence(str(s["poll"])).matched_keyword, s["id"]


def test_last_success_reads_runner_and_context_records() -> None:
    runs = [
        {
            "status": "ok",
            "started_at": "2026-09-13T20:25:00+00:00",
            "finished_at": "2026-09-13T20:25:30+00:00",
        },
        {"status": "failed", "finished_at": "2026-09-20T03:07:10+00:00"},
        {"status": "ok", "retrieved_at": "2026-09-01T00:00:00Z"},  # a context loader's record
    ]
    last, latest = last_success_from_runs(runs)
    assert last == dt.datetime(2026, 9, 13, 20, 25, 30, tzinfo=dt.UTC) and latest == "failed"


def test_the_store_report_names_stale_and_never_run_sources(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "eu.ted.api"
    run_dir.mkdir(parents=True)
    (run_dir / "20260913T202619Z.json").write_text(
        json.dumps({"id": "r", "status": "ok", "finished_at": "2026-09-13T20:26:24+00:00"})
    )
    rows = {r["source_id"]: r for r in store_report(NOW, tmp_path)}
    assert rows["eu.ted.api"]["status"] == "stale"
    assert rows["us.epa.class_vi"]["status"] == "never"
    assert rows["us.tx.rrc.class_vi"]["status"] == "unscheduled"  # gated under the posture in force


class _Factory:
    def __init__(self) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        self.sessionmaker = get_sessionmaker(engine)

    def __call__(self) -> Any:
        return self.sessionmaker()


def _seed(factory: _Factory) -> None:
    for source_id in ("eu.ted.api", "us.iso.caiso.gen_queue", "us.epa.class_vi", "us.eia.860m"):
        jobs.record_source_run(factory, source_id, {"status": "ok", "finished_at": "2026-10-07T00:00:00Z"})
    with factory() as session:
        rows = {s.id: s for s in session.scalars(select(Source))}
        rows["eu.ted.api"].last_success_at = dt.datetime(2026, 9, 13, 20, 26, tzinfo=dt.UTC)
        rows["us.iso.caiso.gen_queue"].last_success_at = dt.datetime(2026, 9, 13, 20, 25, tzinfo=dt.UTC)
        rows["us.iso.caiso.gen_queue"].paused = True
        rows["us.epa.class_vi"].last_success_at = None
        for row in rows.values():
            row.implemented = True
        session.commit()


def test_freshness_tick_fails_loudly_while_a_source_is_stale() -> None:
    factory = _Factory()
    _seed(factory)
    with pytest.raises(jobs.SourcesStale, match=r"eu\.ted\.api \(stale, 23\.6 d\)") as exc:
        jobs.freshness_tick_job(factory, now=NOW)
    assert "us.epa.class_vi (never)" in str(exc.value)
    assert "caiso" not in str(exc.value)  # paused: not expected to be fresh

    report = jobs.freshness_tick_job(factory, now=NOW, raise_on_stale=False)
    assert report["by_status"] == {"stale": 1, "paused": 1, "never": 1, "fresh": 1}
    assert {a["source_id"] for a in report["alerting"]} == {"eu.ted.api", "us.epa.class_vi"}


def test_freshness_tick_is_quiet_when_everything_is_fresh() -> None:
    factory = _Factory()
    jobs.record_source_run(factory, "us.eia.860m", {"status": "ok", "finished_at": "2026-10-07T00:00:00Z"})
    with factory() as session:
        for row in session.scalars(select(Source)):
            row.implemented = True
        session.commit()
    assert jobs.freshness_tick_job(factory, now=NOW)["alerting"] == []
