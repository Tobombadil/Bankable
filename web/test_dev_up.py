"""Unit tests for `web/dev_up.py`: the order `build_store` loads in, `--context-only`, the stale-schema
check, the fixture fallback, matches and `ANALYZE`. The context-layer loaders it calls are tested in
`services/ingest/test_context_layers.py`."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from web import dev_up


# ------------------------------------------------- a stale dev database is rebuilt, not reused
def test_columns_missing_from_reports_a_column_the_database_lacks(tmp_path: Path) -> None:
    """`init_db` is `metadata.create_all`, which creates missing *tables* and never adds a column
    to one that already exists. So a `dev.db` written before a migration keeps its old schema for
    ever, and the first page to read the new column dies with "no such column" -- which is what a
    database written before 0015 added `organization.parent_org_id` actually did."""
    import sqlalchemy as sa

    from services.db.session import get_engine

    db = tmp_path / "stale.db"
    engine = get_engine(f"sqlite+pysqlite:///{db}")
    with engine.begin() as conn:
        # One real table, one column short of the model.
        conn.execute(sa.text("CREATE TABLE organization (id TEXT PRIMARY KEY)"))

    missing = dev_up._columns_missing_from(engine)

    assert any(m == "organization.parent_org_id" for m in missing), missing
    # A table the database does not have at all is `create_all`'s job, so it is not reported here.
    assert not any(m.startswith("proposal.") for m in missing), missing


def test_columns_missing_from_is_empty_for_a_current_database(tmp_path: Path) -> None:
    """The other direction, so the check cannot start reporting phantom drift on a good database
    and send someone rebuilding for no reason."""
    from services.db.session import get_engine, init_db

    db = tmp_path / "fresh.db"
    engine = get_engine(f"sqlite+pysqlite:///{db}")
    init_db(engine)

    assert dev_up._columns_missing_from(engine) == []


def test_fixture_fallback_loads_only_when_no_connector_output_loaded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A fresh checkout (CI) has no `data/normalized/*`: dev_up must serve the committed fixture rather
    than empty pages, and say so; with real output loaded it must not touch the fixture."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(dev_up, "load_test_database", lambda session, **kw: calls.append(kw))

    real = {"sources": {"us.caiso.queue": "loaded", "us.pjm.queue": "skipped: gated"}}
    assert dev_up.load_fixture_if_empty(object(), real, sample_per_state=None) is False  # type: ignore[arg-type]
    assert calls == []

    empty = {"sources": {"us.caiso.queue": "missing", "us.ercot.gis": "missing"}}
    with caplog.at_level(logging.WARNING, logger="web.dev_up"):
        assert dev_up.load_fixture_if_empty(object(), empty, sample_per_state=None) is True  # type: ignore[arg-type]
    assert calls == [{"sample_per_state": dev_up.FIXTURE_SAMPLE_PER_STATE, "include_opportunities": False}]
    assert "fixture" in caplog.text

    calls.clear()
    dev_up.load_fixture_if_empty(object(), {"sources": {}}, sample_per_state=5)  # type: ignore[arg-type]
    assert calls == [{"sample_per_state": 5, "include_opportunities": False}]


def test_run_matches_runs_the_matcher_in_full_mode_and_logs_its_summary(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """docs/10 US-401: `dev_up` computes matches after every load, through the matcher's own entry
    point, in full mode (a fresh store has no watermark to be incremental against)."""
    calls: list[dict[str, Any]] = []

    class _Report:
        def summary(self) -> str:
            return "mode=full added=3"

    def fake(session: Any, **kwargs: Any) -> _Report:
        calls.append({"session": session, **kwargs})
        return _Report()

    monkeypatch.setattr(dev_up, "run_matches", fake)
    session = object()
    with caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._run_matches(session)  # type: ignore[arg-type]
    assert calls == [{"session": session, "full": True}]
    assert "matches: mode=full added=3" in [r.getMessage() for r in caplog.records]


def test_link_interconnection_points_runs_the_ingest_pass_and_logs_each_source(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Result:
        def summary(self) -> str:
            return "us.iso.ercot.gen_queue: 3/3 proposals linked to 2 points"

    calls: list[object] = []
    monkeypatch.setattr(dev_up, "link_all_points", lambda session: calls.append(session) or [_Result()])
    session = object()
    with caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._link_interconnection_points(session)  # type: ignore[arg-type]
    assert calls == [session]
    assert "interconnection points: us.iso.ercot.gen_queue: 3/3 proposals linked to 2 points" in caplog.text


def test_refresh_planner_statistics_writes_sqlite_statistics(tmp_path: Path) -> None:
    """After a bulk load the planner needs row counts; without them SQLite chose the wrong index for
    the interconnection-point predicate (M-11 audit 3.2 s -> 35 s on the dev store, 2026-09-29)."""
    from services.db.session import get_engine, get_sessionmaker, init_db
    from web.dev_up import refresh_planner_statistics

    engine = get_engine(f"sqlite+pysqlite:///{tmp_path / 'stats.db'}")
    init_db(engine)
    with get_sessionmaker(engine)() as session:
        refresh_planner_statistics(session)
        query = text("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in session.execute(query)}
    assert "sqlite_stat1" in tables


# ------------------------------------------------------------ cross-source resolution (docs/25 §3.8)
_VA = "us.va.deq.data_center_air_sites"
_ICIS = "us.epa.echo.icis_air"
#: One facility both recorded fixtures carry: DEQ's PLA_ICIS_ID 74349 is ICIS-Air's PGM_SYS_ID
#: VA0000005115374349, so both rows cite `icis_air:VA0000005115374349`.
_SHARED = {_VA: "74349", _ICIS: "VA0000005115374349"}


def _two_source_data_root(root: Path) -> Path:
    """A data root holding one normalised row per source for the shared facility, each with the run
    record the connector runner writes, parsed from the committed fixtures (no network)."""
    import json

    from conftest import connector_for, snapshot
    from pipeline.connectors.us_epa_echo_icis_air.connector import ICIS_URL
    from pipeline.connectors.us_va_deq_data_center_air_sites.connector import LAYER_URL

    fixtures = {
        _VA: ("va_deq_air_sites_data_centers.json", LAYER_URL),
        _ICIS: ("epa_icis_air_data_centers.json", ICIS_URL),
    }
    ts = "20260929T120000Z"
    for index, (source_id, (fixture, url)) in enumerate(fixtures.items()):
        connector = connector_for(source_id)
        raw = snapshot(fixture, url, "application/json")
        frame = connector.normalize(connector.parse(raw), raw)
        frame = frame[frame["source_record_id"] == _SHARED[source_id]].reset_index(drop=True)
        assert len(frame) == 1 and f"icis_air:{_SHARED[_ICIS]}" in str(frame.at[0, "cross_refs"])
        parquet = root / "normalized" / source_id / f"{ts}.parquet"
        parquet.parent.mkdir(parents=True)
        frame.to_parquet(parquet, index=False)
        run = root / "runs" / source_id / f"{ts}.json"
        run.parent.mkdir(parents=True)
        run_record = {
            "id": f"00000000-0000-4000-8000-00000000000{index}",
            "status": "ok",
            "outputs": {"normalized": str(parquet)},
        }
        run.write_text(json.dumps(run_record))
    return root


def _live_proposals(db: Path) -> list[tuple[str, ...]]:
    """Per live proposal (not merged into another), the sorted source ids of its active links."""
    from sqlalchemy import select

    from services.db.models import Proposal, ProposalSource
    from services.db.session import get_engine, get_sessionmaker

    with get_sessionmaker(get_engine(f"sqlite+pysqlite:///{db}"))() as session:
        rows = session.execute(
            select(Proposal.id, ProposalSource.source_id)
            .join(ProposalSource, ProposalSource.proposal_id == Proposal.id)
            .where(Proposal.merged_into_id.is_(None), ProposalSource.active.is_(True))
        ).all()
    by_proposal: dict[object, list[str]] = {}
    for proposal_id, source_id in rows:
        by_proposal.setdefault(proposal_id, []).append(source_id)
    return sorted(tuple(sorted(sources)) for sources in by_proposal.values())


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], [(_ICIS, _VA)]),  # one proposal carrying both sources
        (["--no-resolve"], [(_ICIS,), (_VA,)]),  # one proposal per source
    ],
)
def test_build_store_resolves_a_shared_registry_id_to_one_proposal(
    tmp_path: Path, argv: list[str], expected: list[tuple[str, ...]]
) -> None:
    from sqlalchemy import inspect as sa_inspect

    from services.db.session import get_engine

    """The dev store runs the scheduler's resolution after the load (docs/25 §3.8): a facility that
    Virginia DEQ and EPA ICIS-Air both cite by one `icis_air:` id is one proposal with both source
    links, and `--no-resolve` leaves it as two."""
    data_root = _two_source_data_root(tmp_path / "data")
    args = dev_up._parse_args(["--data-dir", str(data_root), "--db", str(tmp_path / "dev.db"), *argv])
    report = dev_up.build_store(
        f"sqlite+pysqlite:///{args.db}",
        data_dir=args.data_dir,
        sources_yaml=args.sources_yaml,
        resolve=args.resolve,
    )
    assert report["sources"][_VA] == "loaded" and report["sources"][_ICIS] == "loaded"
    assert _live_proposals(args.db) == expected
    # The gate files a below-threshold cluster into `resolution_decision`, so the store must have it
    # before the API server's own startup would create it.
    engine = get_engine(f"sqlite+pysqlite:///{args.db}")
    assert "resolution_decision" in sa_inspect(engine).get_table_names()


def test_build_store_resolves_after_the_loads_and_before_points_matches_and_analyze(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolution runs once every loader has written its rows, and before the interconnection-point
    and match passes, so both see only surviving proposals; `ANALYZE` stays last."""
    calls: list[str] = []
    monkeypatch.setattr(
        dev_up, "load_dev_database", lambda session, **kw: calls.append("load") or {"sources": {}}
    )
    monkeypatch.setattr(dev_up, "load_fixture_if_empty", lambda *a, **kw: calls.append("fixture"))
    monkeypatch.setattr(dev_up, "load_context_files", lambda *a, **kw: calls.append("context"))
    monkeypatch.setattr(dev_up, "_resolve_clusters", lambda *a: calls.append("resolve"))
    monkeypatch.setattr(dev_up, "_link_interconnection_points", lambda *a: calls.append("points"))
    monkeypatch.setattr(dev_up, "_run_matches", lambda *a: calls.append("matches"))
    monkeypatch.setattr(dev_up, "refresh_planner_statistics", lambda *a: calls.append("analyze"))
    url = f"sqlite+pysqlite:///{tmp_path / 'order.db'}"
    dev_up.build_store(url, data_dir=tmp_path, sources_yaml=dev_up.DEFAULT_SOURCES_YAML)
    assert calls == ["load", "fixture", "context", "resolve", "points", "matches", "analyze"]
    calls.clear()
    dev_up.build_store(url, data_dir=tmp_path, sources_yaml=dev_up.DEFAULT_SOURCES_YAML, resolve=False)
    assert calls == ["load", "fixture", "context", "points", "matches", "analyze"]


def test_no_resolve_flag_defaults_to_resolving() -> None:
    assert dev_up._parse_args([]).resolve is True
    assert dev_up._parse_args(["--no-resolve"]).resolve is False


def test_context_only_refuses_without_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """docs/64: `--context-only` loads into the store a deploy migrated; it never invents one."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(dev_up, "load_context_layers", lambda *a, **k: pytest.fail("loaded without a store"))
    assert dev_up.main(["--context-only"]) == 2


def test_context_only_loads_the_layers_into_database_url_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The context layers (services/ingest/context_layers.py), then matches and `ANALYZE`; no
    schema, no proposal load, no servers (a deployed store's proposals come from the scheduler)."""
    order: list[str] = []
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'store.db'}")
    monkeypatch.setattr(dev_up, "init_db", lambda engine: pytest.fail("--context-only creates no schema"))
    monkeypatch.setattr(
        dev_up, "build_store", lambda *a, **k: pytest.fail("--context-only loads no proposals")
    )
    monkeypatch.setattr(
        dev_up.subprocess, "Popen", lambda *a, **k: pytest.fail("--context-only starts no server")
    )
    monkeypatch.setattr(
        dev_up, "load_context_files", lambda s, d, sources_yaml: order.append(f"context:{d.name}")
    )
    monkeypatch.setattr(dev_up, "_run_matches", lambda s: order.append("matches"))
    monkeypatch.setattr(dev_up, "refresh_planner_statistics", lambda s: order.append("analyze"))
    root = tmp_path / "root"
    assert dev_up.main(["--context-only", "--data-dir", str(root)]) == 0
    assert order == ["context:root", "matches", "analyze"]
