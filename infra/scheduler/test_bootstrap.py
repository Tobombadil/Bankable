"""`infra/scheduler/bootstrap.py`: which sources a first fetch or load queues, and what it reports.
The deferral itself is the scheduler's own (`run_connector` / `load_source` under their locks); here
it is a recording fake, so these need no database. The real queue was exercised on a local Postgres
store on 2026-10-09 (docs/64 §5)."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from infra.scheduler import bootstrap
from infra.scheduler.cadence import execution_lock_for, queueing_lock_for, safe_id


def expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(str(message))


SCHEDULED = [
    {"id": "us.iso.caiso.gen_queue", "cadence": "weekly"},
    {"id": "us.iso.pjm.gen_queue", "cadence": "daily"},
    {"id": "us.ferc.elibrary", "cadence": "realtime"},
    {"id": "us.eia.api", "cadence": "varies"},
]
STATUS = [
    {"id": "us.iso.caiso.gen_queue", "state": "implemented"},
    {"id": "us.iso.pjm.gen_queue", "state": "gated"},
    {"id": "us.ferc.elibrary", "state": "implemented"},
    {"id": "us.eia.api", "state": "unimplemented"},
    {"id": "us.gridtracker.interconnection_fyi", "state": "excluded"},
]


def _run(status: str, ts: str | None, *, location: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {"status": status, "outputs": {}}
    if ts is not None:
        record["outputs"]["normalized"] = location or f"/data/normalized/x/{ts}.parquet"
    return record


def test_only_implemented_scheduled_sources_are_fetched() -> None:
    picked = [s["id"] for s in bootstrap.fetchable_sources(SCHEDULED, STATUS)]
    expect(picked == ["us.iso.caiso.gen_queue", "us.ferc.elibrary"], picked)


def test_the_latest_promoted_run_is_the_newest_ok_run_with_a_normalised_file() -> None:
    runs = [
        _run("ok", "20261001T000000Z"),
        _run("ok", "20261008T000000Z", location="s3://bucket/data/normalized/x/20261008T000000Z.parquet"),
        _run("unchanged", "20261009T000000Z"),  # an unchanged run promotes nothing new
        _run("failed", "20261009T010000Z"),
        _run("ok", None),  # a run that wrote no normalised output
    ]
    expect(bootstrap.latest_promoted_ts(runs) == "20261008T000000Z", bootstrap.latest_promoted_ts(runs))
    expect(bootstrap.latest_promoted_ts([_run("failed", "20261009T000000Z")]) is None, "nothing to load")
    expect(bootstrap.latest_promoted_ts([]) is None, "no runs")


def test_fetches_report_queued_and_already_queued() -> None:
    queued: list[str] = []

    def defer(source: Any) -> bool:
        queued.append(source["id"])
        return bool(source["id"] != "us.ferc.elibrary")  # its fetch is still queued from the last tick

    report = bootstrap.queue_fetches(bootstrap.fetchable_sources(SCHEDULED, STATUS), defer)
    expect(queued == ["us.iso.caiso.gen_queue", "us.ferc.elibrary"], queued)
    expect([r["result"] for r in report] == ["queued", "already_queued"], report)


def test_loads_skip_sources_with_nothing_promoted_or_no_generic_load() -> None:
    runs = {
        "a.loadable": [_run("ok", "20261009T000000Z")],
        "b.never_ran": [],
        "c.document": [_run("ok", "20261009T000000Z")],
        "d.busy": [_run("ok", "20261009T120000Z")],
    }
    deferred: list[tuple[str, str]] = []

    def defer(source_id: str, ts: str) -> bool:
        deferred.append((source_id, ts))
        return source_id != "d.busy"

    report = bootstrap.queue_loads(
        list(runs),
        lambda source_id: runs[source_id],
        lambda source_id: "kind document" if source_id == "c.document" else None,
        defer,
    )
    expect(deferred == [("a.loadable", "20261009T000000Z"), ("d.busy", "20261009T120000Z")], deferred)
    results = {r["source_id"]: r["result"] for r in report}
    expect(
        results
        == {
            "a.loadable": "queued",
            "b.never_ran": "no_promoted_run",
            "c.document": "no_generic_load",
            "d.busy": "already_queued",
        },
        results,
    )


CAISO = "us.iso.caiso.gen_queue"


def _queued(connector: Any) -> list[tuple[str, dict[str, Any], str | None, str | None]]:
    return [
        (job["task_name"], job["args"], job["lock"], job["queueing_lock"])
        for job in sorted(connector.jobs.values(), key=lambda j: j["id"])
    ]


def test_main_queues_the_schedulers_own_jobs_under_their_locks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`fetch` then `load` through the real app and task registrations, on Procrastinate's
    in-memory connector: one `run_connector` per implemented source and one `load_source` for the
    source with a promoted run, each under the locks the ticks use, and a summary line."""
    import json

    from procrastinate import testing

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")  # never connected
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(tmp_path))
    run_dir = tmp_path / "runs" / "us.iso.caiso.gen_queue"
    run_dir.mkdir(parents=True)
    normalized = tmp_path / "normalized" / "us.iso.caiso.gen_queue" / "20261009T194942Z.parquet"
    (run_dir / "20261009T194942Z.json").write_text(
        json.dumps({"status": "ok", "outputs": {"normalized": str(normalized)}})
    )
    import infra.scheduler.app as scheduler_app

    connector = testing.InMemoryConnector()
    with scheduler_app.app.replace_connector(connector):
        assert bootstrap.main(["fetch"]) == 0
        fetches = _queued(connector)
        assert fetches and all(name == "run_connector" for name, *_ in fetches)
        caiso = [job for job in fetches if job[1] == {"source_id": "us.iso.caiso.gen_queue"}]
        assert caiso == [
            ("run_connector", {"source_id": CAISO}, execution_lock_for(CAISO), queueing_lock_for(CAISO))
        ]
        assert not [job for job in fetches if job[1]["source_id"] == "us.iso.pjm.gen_queue"]  # gated

        assert bootstrap.main(["fetch"]) == 0  # again: every fetch is still queued
        assert len(_queued(connector)) == len(fetches)

        assert bootstrap.main(["load"]) == 0
        loads = [job for job in _queued(connector) if job[0] == "load_source"]
        assert loads == [
            (
                "load_source",
                {"source_id": CAISO, "ts": "20261009T194942Z"},
                execution_lock_for(CAISO),
                f"load:{safe_id(CAISO)}",
            )
        ]
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    summaries = [row for row in out if "sources" in row]
    assert [s["action"] for s in summaries] == ["fetch", "fetch", "load"]
    assert summaries[1].get("already_queued") == summaries[0]["queued"]
    assert summaries[2]["queued"] == 1 and summaries[2]["no_promoted_run"] >= 1


@pytest.mark.parametrize(
    ("argv", "task"), [(["context"], "context_load"), (["context", "--build"], "context_build")]
)
def test_context_queues_the_monthly_ticks_job_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    task: str,
) -> None:
    """`context` queues the tick's own job under its own locks (`context_load` holds the store-wide
    `resolve` lock); a second call while it is queued is `already_queued`, not a second job."""
    import json

    from procrastinate import testing

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")  # never connected
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(tmp_path))
    import infra.scheduler.app as scheduler_app

    connector = testing.InMemoryConnector()
    with scheduler_app.app.replace_connector(connector):
        assert bootstrap.main(argv) == 0
        assert bootstrap.main(argv) == 0
        jobs = _queued(connector)
    lock = "resolve" if task == "context_load" else "context_build"
    assert jobs == [(task, {}, lock, task)]
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["result"] for r in rows if "result" in r] == ["queued", "already_queued"]
