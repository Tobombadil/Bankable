"""Unit tests for the optional-lane helpers in `web/dev_up.py`: the EIA-860 owner-share load and the
single `services.ingest.enrich.apply_context_features` call run inside `_load_context_asset_layers`
after every asset and edge load and before the curated parents, and each tolerates a missing module
or a missing input with one log line rather than failing the rest of `dev_up`."""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from web import dev_up


@pytest.fixture()
def no_enrich_module(monkeypatch: pytest.MonkeyPatch) -> None:
    # `None` in sys.modules makes `import services.ingest.enrich` raise ImportError deterministically,
    # whether or not another lane has landed the module by the time this runs.
    monkeypatch.setitem(sys.modules, "services.ingest.enrich", None)


def _fake_enrich(monkeypatch: pytest.MonkeyPatch, fn: Any) -> None:
    module = types.ModuleType("services.ingest.enrich")
    module.apply_context_features = fn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.ingest.enrich", module)


def test_apply_context_features_missing_module_is_one_log_line(
    no_enrich_module: None, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    with caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._apply_context_features(object(), tmp_path)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("context features:")]
    assert len(lines) == 1
    assert "not available yet" in lines[0]


def test_apply_context_features_is_called_once_with_session_and_data_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[Any, Path]] = []

    def fake(session: Any, data_root: Path) -> dict[str, int]:
        calls.append((session, data_root))
        return {"features": 1}

    _fake_enrich(monkeypatch, fake)
    session = object()
    dev_up._apply_context_features(session, tmp_path)  # type: ignore[arg-type]
    assert calls == [(session, tmp_path)]


def test_apply_context_features_missing_input_is_one_log_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    def fake(session: Any, data_root: Path) -> None:
        raise FileNotFoundError(data_root / "normalized" / "context" / "missing.parquet")

    _fake_enrich(monkeypatch, fake)
    with caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._apply_context_features(object(), tmp_path)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("context features:")]
    assert len(lines) == 1
    assert "input not found" in lines[0]


def test_load_ownership_missing_parquet_is_one_log_line(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    with caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._load_ownership(object(), tmp_path)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("asset ownership:")]
    assert len(lines) == 1
    assert "not found" in lines[0]


def test_load_ownership_calls_the_lane_loader_with_the_parquet_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import services.ingest.ownership as ownership

    parquet = tmp_path / "normalized" / "context" / "us.eia.860.owners.parquet"
    parquet.parent.mkdir(parents=True)
    parquet.write_bytes(b"")
    calls: list[Path] = []
    monkeypatch.setattr(
        ownership, "load_owner_shares_parquet", lambda session, path: calls.append(path) or {}
    )
    dev_up._load_ownership(object(), tmp_path)  # type: ignore[arg-type]
    assert calls == [parquet]


def test_load_ghgrp_missing_parquet_is_one_log_line(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    with caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._load_ghgrp(object(), tmp_path)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ghgrp ownership:")]
    assert len(lines) == 1
    assert "not found" in lines[0]


def test_load_ghgrp_calls_the_lane_loader_with_the_parquet_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import services.ingest.ghgrp as ghgrp

    parquet = tmp_path / "normalized" / "context" / "us.epa.ghgrp.parquet"
    parquet.parent.mkdir(parents=True)
    parquet.write_bytes(b"")
    calls: list[Path] = []
    monkeypatch.setattr(ghgrp, "load_ghgrp_parquet", lambda session, path: (calls.append(path), None))
    dev_up._load_ghgrp(object(), tmp_path)  # type: ignore[arg-type]
    assert calls == [parquet]


def test_context_layers_run_owner_shares_then_features_after_the_asset_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With no midstream parquet present the file loop does nothing and `load_parents` is not
    reached, but the four post-loop steps still run, in this order: the owner-share load, the
    GHGRP share load, the single features call, then the organisation graph (GLEIF parents and
    curated aliases, docs/22 §17). Ownership edges must exist before features derived from them
    are computed, and the organisation graph reads what all of them created."""
    order: list[str] = []
    monkeypatch.setattr(dev_up, "_load_ownership", lambda session, data_dir: order.append("ownership"))
    monkeypatch.setattr(dev_up, "_load_ghgrp", lambda session, data_dir: order.append("ghgrp"))
    monkeypatch.setattr(
        dev_up, "_apply_context_features", lambda session, data_root: order.append("features")
    )
    monkeypatch.setattr(
        dev_up, "_load_organization_graph", lambda session, data_dir: order.append("org_graph")
    )
    (tmp_path / "normalized" / "context").mkdir(parents=True)
    with caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._load_context_asset_layers(object(), tmp_path)  # type: ignore[arg-type]
    assert order == ["ownership", "ghgrp", "features", "org_graph"]
    assert any("no midstream/fuels parquet" in r.getMessage() for r in caplog.records)


def test_load_organization_graph_without_a_gleif_parquet_still_applies_aliases(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Unlike the two steps above, this one always has work to do: the curated alias file ships in
    the repository, and applying it registers its source, so it needs a real session rather than
    short-circuiting on a missing file. A missing GLEIF parquet is still only a log line, and the
    aliases land regardless (docs/22 §17)."""
    from services.db.session import get_engine, get_sessionmaker, init_db

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as session, caplog.at_level(logging.INFO, logger=dev_up.log.name):
        dev_up._load_organization_graph(session, tmp_path)
    messages = [r.getMessage() for r in caplog.records]
    assert any("gleif" in m.lower() and "not found" in m.lower() for m in messages), messages
    assert any("alias" in m.lower() for m in messages), messages
    # The curated merge file runs after the aliases; on an empty store every rule is inert.
    merges = [m for m in messages if "curated merges applied" in m]
    assert len(merges) == 1, messages
    assert "'merged': []" in merges[0] and "'missing': [" in merges[0]


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


# ------------------------------------------------- ethanol_plant operator edges (docs/24 §7)
def _write_ethanol_parquets(tmp_path: Path) -> None:
    import json

    import pandas as pd

    context_dir = tmp_path / "normalized" / "context"
    context_dir.mkdir(parents=True)

    def row(source_asset_id: str, name: str, city: str, capacity: float, *, is_atlas: bool) -> dict:
        return {
            "source_id": "us.eia.atlas.ethanol_plants" if is_atlas else "us.eia.ethanol_capacity",
            "source_asset_id": source_asset_id,
            "name": f"{name} ({city}, NE)",
            "operator_name": name,
            "status": "operating",
            "technology": "ethanol",
            "capacity_value": capacity,
            "capacity_unit": "MMgal/yr",
            "state_code": "US-NE",
            "country": "US",
            "attributes": json.dumps({"as_of_year": 2025} if not is_atlas else {}),
            "attributes_text": json.dumps({"site": city} if is_atlas else {"city": city}),
            "source_url": "https://example.invalid/ethanol",
            "retrieved_at": "2026-09-19T15:34:29Z",
            **({"lon": -97.6, "lat": 40.6} if is_atlas else {}),
        }

    atlas = pd.DataFrame(
        [row("atlas-fairmont", "Flint Hills Resources Fairmont LLC", "Fairmont", 127.0, is_atlas=True)]
    )
    capacity = pd.DataFrame(
        [
            row("cap-fairmont", "Poet Biorefining-Fairmont", "Fairmont", 128.0, is_atlas=False),
            row("cap-solo", "Standalone Ethanol Co", "Elsewhere", 40.0, is_atlas=False),
        ]
    )
    atlas.to_parquet(context_dir / "us.eia.atlas.ethanol_plants.parquet", index=False)
    capacity.to_parquet(context_dir / "us.eia.ethanol_capacity.parquet", index=False)


def test_capacity_operator_edges_are_not_requested_for_merged_rows_after_the_fix(tmp_path: Path) -> None:
    """`load_ethanol_plants` itself now writes the merged asset's operator edge from the capacity
    report (docs/24 §7.1: the current owner, not the stale Atlas one); `_load_ethanol_plants`
    (this module) must not also ask the generic per-file loader to look up that same capacity row,
    since it was consumed as a secondary source, not its own asset -- while the capacity-only
    single (never merged) still gets its edge the ordinary way."""
    from services.db.models import Asset, AssetOwner
    from services.db.session import get_engine, get_sessionmaker, init_db

    _write_ethanol_parquets(tmp_path)
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as session:
        loaded = dev_up._load_ethanol_plants(session, tmp_path)
        assert loaded is True

        merged = session.query(Asset).filter(Asset.source_asset_id == "atlas-fairmont").one()
        merged_edges = session.query(AssetOwner).filter(AssetOwner.asset_id == merged.id).all()
        by_source = {e.source_id: e for e in merged_edges}
        assert set(by_source) == {"us.eia.atlas.ethanol_plants", "us.eia.ethanol_capacity"}
        assert by_source["us.eia.ethanol_capacity"].owner_name_raw == "Poet Biorefining-Fairmont"
        assert by_source["us.eia.atlas.ethanol_plants"].owner_name_raw == "Flint Hills Resources Fairmont LLC"

        single = session.query(Asset).filter(Asset.source_asset_id == "cap-solo").one()
        single_edges = session.query(AssetOwner).filter(AssetOwner.asset_id == single.id).all()
        assert len(single_edges) == 1
        assert single_edges[0].source_id == "us.eia.ethanol_capacity"
        assert single_edges[0].owner_name_raw == "Standalone Ethanol Co"

        # Exactly two operator edges on the merged asset, not three: re-requesting the merged
        # capacity row from the generic loader would not have corrupted this (measured separately,
        # `services/ingest/midstream.py`'s own `Asset.source_id == source.id` filter already can't
        # find it), but it must not be asked for at all.
        assert len(merged_edges) == 2


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
    monkeypatch.setattr(dev_up, "_load_plants_context_layer", lambda *a: calls.append("plants"))
    monkeypatch.setattr(dev_up, "_load_context_asset_layers", lambda *a: calls.append("context"))
    monkeypatch.setattr(dev_up, "_resolve_clusters", lambda *a: calls.append("resolve"))
    monkeypatch.setattr(dev_up, "_link_interconnection_points", lambda *a: calls.append("points"))
    monkeypatch.setattr(dev_up, "_run_matches", lambda *a: calls.append("matches"))
    monkeypatch.setattr(dev_up, "refresh_planner_statistics", lambda *a: calls.append("analyze"))
    url = f"sqlite+pysqlite:///{tmp_path / 'order.db'}"
    dev_up.build_store(url, data_dir=tmp_path, sources_yaml=dev_up.DEFAULT_SOURCES_YAML)
    assert calls == ["load", "fixture", "plants", "context", "resolve", "points", "matches", "analyze"]
    calls.clear()
    dev_up.build_store(url, data_dir=tmp_path, sources_yaml=dev_up.DEFAULT_SOURCES_YAML, resolve=False)
    assert calls == ["load", "fixture", "plants", "context", "points", "matches", "analyze"]


def test_no_resolve_flag_defaults_to_resolving() -> None:
    assert dev_up._parse_args([]).resolve is True
    assert dev_up._parse_args(["--no-resolve"]).resolve is False
