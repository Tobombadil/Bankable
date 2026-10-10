"""`services/ingest/context_layers.py`: the order the context layers load in, and that a missing
input is one log line rather than a failure of the steps after it. Moved from `web/test_dev_up.py`
with the loaders themselves (docs/64 §7), so the scheduler's `context_load` and `web.dev_up` share
one tested path."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from services.ingest import context_layers


def test_apply_context_features_is_called_once_with_session_and_data_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[Any, Path]] = []

    def fake(session: Any, data_root: Path) -> dict[str, int]:
        calls.append((session, data_root))
        return {"features": 1}

    monkeypatch.setattr(context_layers, "apply_context_features", fake)
    session = object()
    context_layers.apply_features(session, tmp_path)  # type: ignore[arg-type]
    assert calls == [(session, tmp_path)]


def test_apply_context_features_missing_input_is_one_log_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    def fake(session: Any, data_root: Path) -> None:
        raise FileNotFoundError(data_root / "normalized" / "context" / "missing.parquet")

    monkeypatch.setattr(context_layers, "apply_context_features", fake)
    with caplog.at_level(logging.INFO, logger=context_layers.log.name):
        context_layers.apply_features(object(), tmp_path)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("context features:")]
    assert len(lines) == 1
    assert "input not found" in lines[0]


def test_load_ownership_missing_parquet_is_one_log_line(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    with caplog.at_level(logging.INFO, logger=context_layers.log.name):
        context_layers.load_ownership(object(), tmp_path)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("asset ownership:")]
    assert len(lines) == 1
    assert "not found" in lines[0]


def test_load_ownership_calls_the_lane_loader_with_the_parquet_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parquet = tmp_path / "normalized" / "context" / "us.eia.860.owners.parquet"
    parquet.parent.mkdir(parents=True)
    parquet.write_bytes(b"")
    calls: list[Path] = []
    monkeypatch.setattr(
        context_layers, "load_owner_shares_parquet", lambda session, path: calls.append(path) or {}
    )
    context_layers.load_ownership(object(), tmp_path)  # type: ignore[arg-type]
    assert calls == [parquet]


def test_load_ghgrp_missing_parquet_is_one_log_line(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    with caplog.at_level(logging.INFO, logger=context_layers.log.name):
        context_layers.load_ghgrp(object(), tmp_path)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("ghgrp ownership:")]
    assert len(lines) == 1
    assert "not found" in lines[0]


def test_load_ghgrp_calls_the_lane_loader_with_the_parquet_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parquet = tmp_path / "normalized" / "context" / "us.epa.ghgrp.parquet"
    parquet.parent.mkdir(parents=True)
    parquet.write_bytes(b"")
    calls: list[Path] = []
    monkeypatch.setattr(
        context_layers, "load_ghgrp_parquet", lambda session, path: (calls.append(path), None)
    )
    context_layers.load_ghgrp(object(), tmp_path)  # type: ignore[arg-type]
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
    monkeypatch.setattr(context_layers, "load_ownership", lambda session, data_dir: order.append("ownership"))
    monkeypatch.setattr(context_layers, "load_ghgrp", lambda session, data_dir: order.append("ghgrp"))
    monkeypatch.setattr(context_layers, "apply_features", lambda session, data_root: order.append("features"))
    monkeypatch.setattr(
        context_layers, "load_organization_graph", lambda session, data_dir: order.append("org_graph")
    )
    (tmp_path / "normalized" / "context").mkdir(parents=True)
    with caplog.at_level(logging.INFO, logger=context_layers.log.name):
        context_layers.load_asset_layers(object(), tmp_path)  # type: ignore[arg-type]
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
    with get_sessionmaker(engine)() as session, caplog.at_level(logging.INFO, logger=context_layers.log.name):
        context_layers.load_organization_graph(session, tmp_path)
    messages = [r.getMessage() for r in caplog.records]
    assert any("gleif" in m.lower() and "not found" in m.lower() for m in messages), messages
    assert any("alias" in m.lower() for m in messages), messages
    # The curated merge file runs after the aliases; on an empty store every rule is inert.
    merges = [m for m in messages if "curated merges applied" in m]
    assert len(merges) == 1, messages
    assert "'merged': []" in merges[0] and "'missing': [" in merges[0]


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
    report (docs/24 §7.1: the current owner, not the stale Atlas one); `_load_ethanol_plants` must
    not also ask the generic per-file loader to look up that same capacity row, since it was
    consumed as a secondary source, not its own asset -- while the capacity-only single (never
    merged) still gets its edge the ordinary way."""
    from services.db.models import Asset, AssetOwner
    from services.db.session import get_engine, get_sessionmaker, init_db

    _write_ethanol_parquets(tmp_path)
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as session:
        loaded = context_layers._load_ethanol_plants(session, tmp_path)
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


# ------------------------------------------------- EIA-860M retirements onto the plant assets
def test_load_retirements_missing_frame_is_one_log_line(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    with caplog.at_level(logging.INFO, logger=context_layers.log.name):
        context_layers.load_retirements(object(), tmp_path)  # type: ignore[arg-type]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("retirements:")]
    assert len(lines) == 1 and "skipping" in lines[0]


def test_load_retirements_sets_plant_status_and_creates_no_proposal(tmp_path: Path) -> None:
    """2026-10-07: the dev store showed every one of 14,659 plants `operating`, because the
    retirements run was never loaded. Plants loaded the way the pre-R1 context file had them (no
    status column), then the connector's run of the recorded August 2026 trim through
    `load_retirements`: the plants take their retirement state, and nothing becomes a proposal."""
    from sqlalchemy import func, select

    from conftest import fixture_path, snapshot
    from pipeline.connectors.registry import Registry
    from pipeline.connectors.runner import run
    from pipeline.connectors.store import Store
    from pipeline.connectors.us_eia_860m_retirements.test_connector import FIXTURE, MONTH1, URL
    from pipeline.context.eia_plants import aggregate_plants
    from pipeline.context.retirements import parse_generator_sheets
    from services.db.models import Asset, Proposal
    from services.db.session import get_engine, get_sessionmaker, init_db
    from services.ingest.plants import load_plants

    result = run(
        "us.eia.860m.retirements",
        registry=Registry(),
        store=Store(tmp_path),
        raw=snapshot(FIXTURE, URL, retrieved_at=MONTH1),  # type: ignore[arg-type]
    )
    assert result.status == "ok"
    sheets = parse_generator_sheets(fixture_path(FIXTURE).read_bytes())
    plants = aggregate_plants(
        sheets.operating, retrieved_at=MONTH1, retired=sheets.retired, as_of=sheets.as_of
    ).drop(columns=["status", "retirement_year", "attributes"])
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as session:
        load_plants(session, plants)
        assert set(session.scalars(select(Asset.status))) == {"operating"}
        context_layers.load_retirements(session, tmp_path)
        status = dict(session.execute(select(Asset.source_asset_id, Asset.status)).all())
        assert status["6155"] == "retired" and status["3122"] == "retired"  # Rush Island, Homer City
        assert status["6166"] == "retiring" and status["2828"] == "operating"
        assert session.scalar(select(func.count()).select_from(Proposal)) == 0
