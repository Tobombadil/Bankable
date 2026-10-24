"""`infra/scheduler/bootstrap.py`: which sources a first fetch or load queues, and what it reports;
`first-fetch`, which each deploy runs, and the read-only `sources` report (docs/28 lane F5).
The deferral itself is the scheduler's own (`run_connector` / `load_source` under their locks); here
it is a recording fake, so these need no database. The real queue was exercised on a local Postgres
store on 2026-10-09 (docs/64 §5)."""

from __future__ import annotations

import datetime as dt
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


# ------------------------------------------------------------------ first-fetch (docs/28 lane F5)
UTC = dt.UTC
CLASS_VI = "us.epa.class_vi"
PERMITS = "us.permits_dashboard"
NYISO = "us.iso.nyiso.gen_queue"


def _factory() -> Any:
    from services.db.session import get_engine, get_sessionmaker, init_db

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


def _state(source_id: str, **kw: Any) -> bootstrap.SourceState:
    return bootstrap.SourceState(source_id, has_row=True, **kw)


def test_first_fetch_plans_only_sources_that_never_succeeded_and_says_why_not() -> None:
    ids = ["a.ok", "b.new", "c.paused", "d.blocked", "e.queued", "f.running", "g.failed"]
    sources = [{"id": i} for i in ids]
    states = {
        "a.ok": _state("a.ok", last_success_at=dt.datetime(2026, 10, 23, tzinfo=UTC)),
        "c.paused": _state("c.paused", paused=True),
        "d.blocked": _state("d.blocked", last_run={"status": "blocked"}),
        "g.failed": _state("g.failed", last_run={"status": "failed", "error": "HttpFailed(...)"}),
    }
    jobs_by = {"e.queued": "todo", "f.running": "doing"}
    plan = bootstrap.first_fetch_plan(sources, states, jobs_by)
    assert {r["source_id"]: r["result"] for r in plan} == {
        "b.new": "queue",  # no `source` row: never attempted here
        "c.paused": "paused",
        "d.blocked": "blocked",  # a refusal waits for its schedule (docs/20 §12)
        "e.queued": "already_queued",
        "f.running": "running",
        "g.failed": "queue",
    }
    deferred: list[str] = []

    def defer(source: Any) -> bool:
        deferred.append(source["id"])
        return True

    report = bootstrap.queue_first_fetches(plan, sources, defer)
    assert deferred == ["b.new", "g.failed"]
    assert [r["result"] for r in report if r["source_id"] in deferred] == ["queued", "queued"]


def test_main_first_fetch_queues_the_never_succeeded_under_the_fetch_locks_and_repeats_safely(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through the real app and the real registry, on Procrastinate's in-memory connector: a source
    with a success is left alone, a never-succeeded one is queued once under `fetch`'s locks, and
    a second deploy's run reports it `already_queued` instead of queueing it again."""
    import json

    from procrastinate import testing

    from infra.scheduler import jobs

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")  # never connected
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(tmp_path))
    factory = _factory()
    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)
    jobs.record_source_run(factory, NYISO, {"status": "ok", "finished_at": "2026-10-23T03:08:00Z"})
    jobs.record_source_run(
        factory,
        PERMITS,
        {"status": "failed", "error": "HttpTlsUnverified('...')", "error_class": "HttpTlsUnverified"},
    )
    import infra.scheduler.app as scheduler_app

    connector = testing.InMemoryConnector()
    with scheduler_app.app.replace_connector(connector):
        assert bootstrap.main(["first-fetch"]) == 0
        first = _queued(connector)
        assert bootstrap.main(["first-fetch"]) == 0
        assert _queued(connector) == first  # nothing queued twice
    by_source = {job[1]["source_id"]: job for job in first}
    assert NYISO not in by_source  # it has succeeded
    assert by_source[PERMITS] == (
        "run_connector",
        {"source_id": PERMITS},
        execution_lock_for(PERMITS),
        queueing_lock_for(PERMITS),
    )
    assert CAISO in by_source and "us.iso.pjm.gen_queue" not in by_source  # never fetched; gated
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    summaries = [row for row in out if "sources" in row]
    assert [s["action"] for s in summaries] == ["first-fetch", "first-fetch"]
    assert summaries[0]["queued"] == len(first) and summaries[0]["succeeded_before"] == 1
    assert summaries[1].get("already_queued") == len(first) and "queued" not in summaries[1]


def test_a_tls_failure_is_not_retried_by_the_job_either() -> None:
    from infra.scheduler import jobs

    assert not jobs.is_transient({"status": "failed", "error_class": "HttpTlsUnverified"})
    assert jobs.is_transient({"status": "failed", "error_class": "HttpFailed"})


# --------------------------------------------------------------------------- sources (read-only)
def test_short_error_keeps_one_line_without_queries_credentials_or_secrets() -> None:
    env = {"ERCOT_API_PASSWORD": "hunter2-correct-horse", "DOMAIN": "inframation.io", "SHORT_KEY": "abc"}
    text = (
        "HttpFailed('GET https://u:p@api.example.gov/v2/x?api_key=AAAABBBBCCCC&page=2 failed after 4"
        " attempts: ConnectionError(\\'with url: /v2/x?api_key=AAAABBBBCCCC (Caused by ...)\\')')\n"
        "Authorization: Bearer abcdefghijklmnop; password=hunter2-correct-horse token: zzz123"
    )
    out = bootstrap.short_error(text, env, limit=1000)
    assert out is not None
    for leaked in ("AAAABBBBCCCC", "u:p@", "abcdefghijklmnop", "hunter2", "zzz123", "\n"):
        assert leaked not in out, (leaked, out)
    assert "https://api.example.gov/v2/x?…" in out and "with url: /v2/x?…" in out
    assert bootstrap.short_error("x" * 500, env, limit=40) == "x" * 39 + "…"
    for leaked in ('{"access_token": "eyJabc.def"}', "oauth error client_secret=abc123", "X-Api-Key: k-77"):
        out = bootstrap.short_error(leaked, env)
        assert out is not None and not any(v in out for v in ("eyJabc", "abc123", "k-77")), out
    kept = "HTTP 403; server=CloudFront; x-amz-cf-pop=IAD89-P1; x-amz-cf-id=Zq3x=="
    assert bootstrap.short_error(kept, env) == kept  # the edge's own answer is what an allow-list needs
    assert bootstrap.short_error(None, env) is None and bootstrap.short_error("", env) is None


def test_attempt_outcomes_name_what_happened() -> None:
    held = _state(
        CLASS_VI,
        last_run={
            "status": "partial",
            "dq": {
                "checks": [
                    {
                        "check": "vocabulary_unmapped",
                        "level": "hold",
                        "detail": "19 rows (27.9 %) with unmapped lifecycle_state",
                    },
                    {"check": "row_count_drift", "level": "info", "detail": "no baseline"},
                ]
            },
        },
    )
    assert bootstrap.attempt_outcome(held, {}) == {
        "result": "partial",
        "what": "held by data quality",
        "error": "19 rows (27.9 %) with unmapped lifecycle_state",
    }
    tls = _state(
        PERMITS,
        last_run={
            "status": "failed",
            "error_class": "HttpTlsUnverified",
            "error": "HttpTlsUnverified('GET https://data.permits.performance.gov/x.csv?accessType=DOWNLOAD:"
            " ...')",
        },
    )
    outcome = bootstrap.attempt_outcome(tls, {})
    assert outcome["what"] == "TLS certificate did not verify" and "accessType" not in outcome["error"]
    old_tls = _state(
        PERMITS,
        last_run={
            "status": "failed",
            "error_class": "HttpFailed",
            "error": "HttpFailed('GET https://h.example/a.csv?x=1 failed after 4 attempts: SSLError(... "
            + "x" * 300
            + " [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get loca')",
        },
    )
    assert bootstrap.attempt_outcome(old_tls, {})["error"].startswith("unable to get loca; HttpFailed(")
    refused = _state(
        CAISO, last_run={"status": "blocked", "error": "BlockedError('HTTP 403; server=CloudFront')"}
    )
    assert bootstrap.attempt_outcome(refused, {})["what"] == "refused by the host"
    assert bootstrap.attempt_outcome(bootstrap.SourceState("x"), {})["result"] == "never_attempted"
    older = _state(
        "z", last_error="an older run's 403", last_run={"status": "failed", "error_class": "RunAbandoned"}
    )
    assert bootstrap.attempt_outcome(older, {})["error"] == "RunAbandoned"  # never the older run's error
    assert bootstrap.attempt_outcome(_state("y", last_run={"status": "ok"}), {})["what"] == "succeeded"


def _sources_store() -> Any:
    from infra.scheduler import jobs

    factory = _factory()
    jobs.record_source_run(
        factory,
        CLASS_VI,
        {
            "status": "partial",
            "dq_status": "fail",
            "started_at": "2026-10-22T00:24:30Z",
            "finished_at": "2026-10-22T00:24:40Z",
            "dq": {
                "checks": [{"check": "vocabulary_unmapped", "level": "hold", "detail": "19 rows unmapped"}]
            },
        },
    )
    jobs.record_source_run(
        factory,
        PERMITS,
        {
            "status": "failed",
            "started_at": "2026-10-22T00:24:20Z",
            "finished_at": "2026-10-22T00:24:29Z",
            "error": "HttpTlsUnverified('GET https://data.permits.performance.gov/a.csv?accessType=DOWNLOAD: "
            "the TLS certificate of data.permits.performance.gov did not verify (unable to get local issuer "
            "certificate); not retried')",
            "error_class": "HttpTlsUnverified",
        },
    )
    jobs.record_source_run(factory, NYISO, {"status": "ok", "finished_at": "2026-10-23T03:08:00Z"})
    return factory


def test_sources_reports_each_last_attempt_and_the_never_succeeded(tmp_path: pathlib.Path) -> None:
    factory = _sources_store()
    registry = [
        {"id": CLASS_VI, "state": "implemented"},
        {"id": PERMITS, "state": "implemented"},
        {"id": NYISO, "state": "implemented"},
        {"id": CAISO, "state": "implemented"},
        {"id": "us.iso.pjm.gen_queue", "state": "gated"},
    ]
    inputs = bootstrap.StatusInputs(
        registry_status=registry,
        scheduled=[{"id": i} for i in (CLASS_VI, PERMITS, NYISO, CAISO)],
        session_factory=factory,
        list_jobs=lambda *, task, status: (
            [type("J", (), {"task_kwargs": {"source_id": CAISO}})()] if status == "todo" else []
        ),
        data_root=tmp_path,
        env={"DOMAIN": "inframation.io"},
        now=dt.datetime(2026, 10, 24, 3, 0, tzinfo=UTC),
    )
    report = bootstrap.sources_report(inputs)
    assert report["never_succeeded"] == [CLASS_VI, CAISO, PERMITS]  # by id
    rows = {r["source_id"]: r for r in report["sources"]}
    assert "us.iso.pjm.gen_queue" not in rows  # gated: not this action's business
    assert rows[CLASS_VI]["result"] == "partial" and rows[CLASS_VI]["error"] == "19 rows unmapped"
    assert rows[PERMITS]["what"] == "TLS certificate did not verify"
    assert "unable to get local issuer certificate" in rows[PERMITS]["error"]
    assert "accessType" not in rows[PERMITS]["error"]
    assert rows[PERMITS]["last_attempt_at"] == "2026-10-22T00:24Z" and rows[PERMITS]["succeeded"] is False
    assert rows[NYISO]["succeeded"] is True and rows[NYISO]["last_success_at"] == "2026-10-23T03:08Z"
    assert rows[CAISO]["result"] == "never_attempted" and rows[CAISO]["job"] == "todo"
    finder = report["site_finder"]
    assert finder["computed"] is False and finder["osm_lines_loaded"] == 0
    assert "https://tiles.inframation.io/grid/osm-power/latest.json" in finder["next"]
    assert "grid-layers.yml" in finder["next"] and "seed" in finder["next"]
    text = bootstrap.render_sources(report)
    assert f"Implemented sources that never succeeded (3): {CLASS_VI}, {CAISO}, {PERMITS}" in text
    assert "held by data quality: 19 rows unmapped" in text
    assert (
        "Site finder: not computed (no run yet; 0 OpenStreetMap lines loaded; grid layer on disk: no)" in text
    )


def test_the_site_finder_reads_computed_once_a_run_with_segments_is_served(tmp_path: pathlib.Path) -> None:
    from services.db.models import SiteFinderRun

    factory = _factory()
    with factory() as session:
        session.add(
            SiteFinderRun(
                status="ok",
                started_at=dt.datetime(2026, 10, 24, 1, tzinfo=UTC),
                finished_at=dt.datetime(2026, 10, 24, 2, tzinfo=UTC),
                segments=120_699,
            )
        )
        session.commit()
    with bootstrap.read_only(factory) as session:
        finder = bootstrap.read_site_finder(session, tmp_path, {"DOMAIN": "inframation.io"})
    assert finder["computed"] is True and finder["segments"] == 120_699 and finder["next"] is None
    text = bootstrap.render_sources(
        {
            "generated_at": "x",
            "implemented": 0,
            "succeeded_once": 0,
            "never_succeeded": [],
            "sources": [],
            "site_finder": finder,
        }
    )
    assert "Site finder: computed, 120,699 segments (run of 2026-10-24T02:00Z)" in text


def test_main_sources_reads_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`sources` imports no Procrastinate app, and every read is rolled back: the store's rows are
    the same afterwards."""
    import json

    from sqlalchemy import func, select

    from infra.scheduler import jobs
    from services.db.models import Source, SourceRun

    factory = _sources_store()
    monkeypatch.setattr(jobs, "build_session_factory", lambda: factory)
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(tmp_path))
    with factory() as session:
        before = (
            session.scalar(select(func.count(Source.id))),
            session.scalar(select(func.count(SourceRun.id))),
        )
    assert bootstrap.main(["sources", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert {CLASS_VI, PERMITS} <= set(report["never_succeeded"])
    assert NYISO not in report["never_succeeded"]
    assert report["implemented"] == len(report["sources"]) >= 20  # every implemented source in the registry
    with factory() as session:
        after = (
            session.scalar(select(func.count(Source.id))),
            session.scalar(select(func.count(SourceRun.id))),
        )
    assert after == before
    assert bootstrap.main(["sources"]) == 0
    assert "Implemented sources that never succeeded" in capsys.readouterr().out
