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
