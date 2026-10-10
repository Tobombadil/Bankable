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
from infra.scheduler.freshness import (
    ALLOWANCE,
    assess,
    held_vintage,
    last_success_from_runs,
    store_report,
)
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


# ---------------------------------------------------------------- stale releases (review §2.7 #4)
def test_an_unchanged_poll_of_an_overdue_release_is_stale() -> None:
    """ERCOT polled an hour ago (`unchanged`) still holding the August report on 8 October: the
    September report (published 1 October) is overdue, so the source is stale, not fresh."""
    polled = dt.datetime(2026, 10, 8, 2, 7, tzinfo=dt.UTC)
    now = dt.datetime(2026, 10, 8, 3, 7, tzinfo=dt.UTC)
    held = assess("daily", polled, now, vintage="2026-08", cadence="monthly")
    assert held.status == "stale" and held.vintage_stale and held.alert
    assert held.to_dict()["vintage_overdue_at"] == "2026-10-07T00:00:00Z"  # 1 Sep + 31 d + 5 d
    current = assess("daily", polled, now, vintage="2026-09", cadence="monthly")
    assert current.status == "fresh" and not current.vintage_stale
    assert current.to_dict()["vintage_overdue_at"] == "2026-11-06T00:00:00Z"


def test_a_twice_weekly_register_is_overdue_after_one_interval_and_the_grace() -> None:
    day = dt.timedelta(days=1)
    register = "2026-10-10"
    on_time = dt.datetime(2026, 10, 18, 11, 0, tzinfo=dt.UTC)
    assert (
        assess("twice weekly", on_time - day / 4, on_time, vintage=register, cadence="twice weekly").status
        == "fresh"
    )
    late = dt.datetime(2026, 10, 18, 13, 0, tzinfo=dt.UTC)
    assert assess(
        "twice weekly", late - day / 4, late, vintage=register, cadence="twice weekly"
    ).vintage_stale


def test_without_a_vintage_or_a_cadence_freshness_is_unchanged() -> None:
    polled = NOW - dt.timedelta(hours=1)
    assert assess("daily", polled, NOW).status == "fresh"
    assert assess("daily", polled, NOW, vintage="2020-01").status == "fresh"  # no cadence: no rule
    assert assess("daily", polled, NOW, vintage=None, cadence="monthly").status == "fresh"
    assert assess("daily", polled, NOW, vintage="not-a-date", cadence="monthly").status == "fresh"
    # paused, unscheduled and never keep their meaning
    assert assess("daily", polled, NOW, paused=True, vintage="2020-01", cadence="monthly").status == "paused"
    assert assess("daily", None, NOW, vintage="2020-01", cadence="monthly").status == "never"


def test_the_held_vintage_is_the_newest_successful_run_that_names_one() -> None:
    def ercot(status: str, at: str, name: str) -> dict[str, Any]:
        return {"status": status, "finished_at": at, "snapshot": {"meta": {"friendly_name": name}}}

    runs = [
        ercot("ok", "2026-09-12T06:00:00Z", "GIS_Report_August2026"),
        ercot("unchanged", "2026-10-01T05:21:00Z", "GIS_Report_August2026"),
        ercot("ok", "2026-10-02T03:07:00Z", "GIS_Report_September2026"),
        ercot("partial", "2026-11-02T03:07:00Z", "GIS_Report_October2026"),  # held: not what we serve
        {"status": "failed", "finished_at": "2026-11-03T03:07:00Z", "snapshot": None},
    ]
    assert held_vintage(runs).value == "2026-09"
    assert held_vintage(runs[:2]).value == "2026-08"
    assert held_vintage([]).value is None


def test_the_store_report_flags_a_release_left_behind_by_unchanged_runs(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "us.iso.ercot.gen_queue"
    run_dir.mkdir(parents=True)
    for ts, status, finished in (
        ("20260912T060000Z", "ok", "2026-09-12T06:00:05+00:00"),
        ("20261007T030700Z", "unchanged", "2026-10-07T03:07:04+00:00"),
    ):
        (run_dir / f"{ts}.json").write_text(
            json.dumps(
                {
                    "id": ts,
                    "status": status,
                    "finished_at": finished,
                    "snapshot": {
                        "fetched_url": "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1269363208",
                        "meta": {"doc_id": "1269363208", "friendly_name": "GIS_Report_August2026"},
                    },
                }
            )
        )
    rows = {r["source_id"]: r for r in store_report(NOW, tmp_path)}
    ercot = rows["us.iso.ercot.gen_queue"]
    assert ercot["poll"] == "daily" and ercot["age_hours"] < 24
    assert ercot["status"] == "stale" and ercot["vintage_stale"] and ercot["vintage"] == "2026-08"
    assert ercot["vintage_label"] == "August 2026"


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


class _Runs:
    """A connector store reduced to the one read the freshness tick makes."""

    def __init__(self, by_source: dict[str, list[dict[str, Any]]], *, fail: bool = False) -> None:
        self.by_source, self.fail = by_source, fail
        self.asked: list[str] = []

    def runs(self, source_id: str) -> list[dict[str, Any]]:
        self.asked.append(source_id)
        if self.fail:
            raise OSError("store unreachable")
        return self.by_source.get(source_id, [])


def _ercot_polled_on_time(factory: _Factory) -> None:
    for source_id in ("us.iso.ercot.gen_queue", "us.eia.860m"):
        jobs.record_source_run(
            factory, source_id, {"status": "unchanged", "finished_at": "2026-10-07T11:00:00Z"}
        )
    with factory() as session:
        for row in session.scalars(select(Source)):
            row.implemented = True
            row.last_success_at = dt.datetime(2026, 10, 7, 11, 0, tzinfo=dt.UTC)
        session.commit()


def _ercot_run(month: str) -> dict[str, Any]:
    return {
        "status": "unchanged",
        "finished_at": "2026-10-07T11:00:00Z",
        "snapshot": {"meta": {"friendly_name": f"GIS_Report_{month}"}},
    }


def test_freshness_tick_flags_a_source_polled_on_time_that_still_serves_an_old_release() -> None:
    """ERCOT polled an hour ago, every run `unchanged`, but the release held is July's: overdue
    since early September (period end + one monthly interval + grace), so the tick alerts. Only
    the sources whose run records name a release are read from the store."""
    factory = _Factory()
    _ercot_polled_on_time(factory)
    store = _Runs({"us.iso.ercot.gen_queue": [_ercot_run("July2026")]})
    report = jobs.freshness_tick_job(factory, now=NOW, raise_on_stale=False, _store=store)
    assert [a["source_id"] for a in report["alerting"]] == ["us.iso.ercot.gen_queue"]
    assert report["alerting"][0]["vintage_stale"] is True
    assert store.asked == ["us.iso.ercot.gen_queue"]

    current = _Runs({"us.iso.ercot.gen_queue": [_ercot_run("September2026")]})
    assert jobs.freshness_tick_job(factory, now=NOW, _store=current)["alerting"] == []


def test_freshness_tick_judges_on_the_poll_alone_when_run_records_cannot_be_read() -> None:
    factory = _Factory()
    _ercot_polled_on_time(factory)
    assert jobs.freshness_tick_job(factory, now=NOW, _store=_Runs({}, fail=True))["alerting"] == []
