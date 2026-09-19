"""Loader tests for `services/ingest/assets.py`, the generalised asset loader (ADR 0008)."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from services.db.models import Asset
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.assets import UnsupportedAssetTypeError, load_assets, load_assets_parquet

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as s:
        yield s


def sample_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "source_asset_id": "1000",
                "name": "Sunrise Solar",
                "operator_name": "Sunrise Power LLC",
                "technology": "solar",
                "technology_raw": "Solar Photovoltaic",
                "technologies": {"Solar Photovoltaic": 150.0},
                "capacity_mw": 150.0,
                "unit_count": 3,
                "commissioned_year": 2010,
                "lon": -90.0,
                "lat": 30.0,
                "state_code": "US-TX",
                "county_name": "Nolan",
                "country": "US",
                "attributes": {"capacity_factor_2025": 0.25},
                "source_url": "https://www.eia.gov/electricity/data/eia860m/",
                "retrieved_at": "2026-09-13T20:25:39Z",
            }
        ]
    )


def test_load_assets_assigns_public_id_and_slug(session):
    result = load_assets(session, sample_frame(), "power_plant")
    assert result.inserted == 1
    row = session.query(Asset).one()
    assert row.asset_type == "power_plant"
    assert row.public_id.startswith("asset_")
    assert row.slug == "sunrise-solar-us-tx"
    assert row.attributes == {"capacity_factor_2025": 0.25}


def test_load_assets_rejects_unwired_asset_type(session):
    with pytest.raises(UnsupportedAssetTypeError):
        load_assets(session, sample_frame(), "transmission_line")


def test_load_assets_rejects_unknown_asset_type(session):
    with pytest.raises(UnsupportedAssetTypeError):
        load_assets(session, sample_frame(), "not_a_real_type")


def test_load_assets_slug_deduplicates_on_collision(session):
    df = pd.concat([sample_frame(), sample_frame()], ignore_index=True)
    df.loc[1, "source_asset_id"] = "1001"
    result = load_assets(session, df, "power_plant")
    assert result.inserted == 2
    slugs = {r.slug for r in session.query(Asset).all()}
    assert slugs == {"sunrise-solar-us-tx", "sunrise-solar-us-tx-2"}


def test_load_assets_parquet_reads_a_file(session, tmp_path):
    from pipeline.connectors.base import to_parquet_safe

    path = tmp_path / "assets.parquet"
    to_parquet_safe(sample_frame()).to_parquet(path, index=False)
    result = load_assets_parquet(session, path, "power_plant")
    assert result.inserted == 1


# ------------------------------------------------------------- midstream types (2026-09-19)
def _line_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "source_asset_id": "trailblazer-pipeline-co-interstate",
                "name": "Trailblazer Pipeline Co",
                "operator_name": "Trailblazer Pipeline Co",
                "status": "operating",
                "technology": "interstate",
                "technology_raw": "Interstate",
                "unit_count": 39,
                "lon": -101.248,
                "lat": 40.844928,
                "geom_line_wkt": (
                    "MULTILINESTRING((-96.916045 40.313107, -96.739595 40.232), (-101.3 40.8, -101.2 40.9))"
                ),
                "state_code": "US-NE",
                "country": "US",
                "attributes": {
                    "pipeline_type": "interstate",
                    "segment_count": 39,
                    "miles": 436.8,
                    "states_crossed": ["US-CO", "US-NE", "US-WY"],
                    "status_raw": ["Operating"],
                    "source_vintage": "202001",
                    "diameter_in": None,
                },
                "source_url": "https://www.eia.gov/maps/map_data/NaturalGas_InterIntrastate_Pipelines_US_EIA.zip",
                "retrieved_at": "2026-09-19T15:34:29Z",
            }
        ]
    )


def test_load_gas_pipeline_round_trips_line_geometry_and_typed_attributes(session):
    result = load_assets(session, _line_frame(), "gas_pipeline")
    assert result.inserted == 1 and result.placed == 1
    row = session.query(Asset).one()
    assert row.asset_type == "gas_pipeline"
    assert row.slug == "trailblazer-pipeline-co-us-ne"
    assert row.geom == (-101.248, 40.844928)  # representative point keeps point code working
    assert row.geom_line == _line_frame().iloc[0]["geom_line_wkt"]  # WKT string on SQLite
    assert row.technology == "interstate" and row.unit_count == 39
    assert row.capacity_value is None
    assert row.attributes["states_crossed"] == ["US-CO", "US-NE", "US-WY"]
    assert row.attributes["status_raw"] == ["Operating"]
    assert row.attributes["source_vintage"] == "202001"
    assert row.attributes["miles"] == 436.8
    assert "diameter_in" in row.attributes and row.attributes["diameter_in"] is None
    assert row.source_id == "us.eia.atlas.gas_pipelines"
    assert row.licence_id.startswith("us.eia.atlas.gas_pipelines#")


def test_load_gas_pipeline_parquet_round_trip_keeps_line_and_attributes(session, tmp_path):
    from pipeline.connectors.base import to_parquet_safe

    path = tmp_path / "pipelines.parquet"
    to_parquet_safe(_line_frame()).to_parquet(path, index=False)
    load_assets_parquet(session, path, "gas_pipeline")
    row = session.query(Asset).one()
    assert row.geom_line.startswith("MULTILINESTRING((-96.916045 40.313107")
    assert row.attributes["states_crossed"] == ["US-CO", "US-NE", "US-WY"]
    again = load_assets_parquet(session, path, "gas_pipeline")
    assert again.inserted == 0 and again.updated == 1
    assert session.query(Asset).count() == 1


def test_load_assets_promotes_linestring_and_drops_empty_lines(session):
    df = pd.concat([_line_frame(), _line_frame()], ignore_index=True)
    df.loc[0, "geom_line_wkt"] = "LINESTRING(-100 40, -99.5 40.5)"
    df.loc[1, "source_asset_id"] = "empty-line"
    df.loc[1, "geom_line_wkt"] = "MULTILINESTRING EMPTY"
    load_assets(session, df, "gas_pipeline")
    rows = {r.source_asset_id: r for r in session.query(Asset).all()}
    assert rows["trailblazer-pipeline-co-interstate"].geom_line == "MULTILINESTRING((-100 40, -99.5 40.5))"
    assert rows["empty-line"].geom_line is None


@pytest.mark.parametrize(
    ("asset_type", "source_id"),
    [
        ("gas_pipeline", "us.eia.atlas.gas_pipelines"),
        ("gas_processing_plant", "us.eia.atlas.gas_processing_plants"),
        ("gas_storage", "us.eia.atlas.gas_storage"),
        ("lng_terminal", "us.eia.atlas.lng_terminals"),
    ],
)
def test_midstream_asset_types_are_wired_to_their_atlas_source(session, asset_type, source_id):
    frame = sample_frame().drop(columns=["technologies", "capacity_mw"])
    frame["capacity_value"] = 12.5
    frame["capacity_unit"] = "MMcf/d"
    result = load_assets(session, frame, asset_type)
    assert result.inserted == 1
    row = session.query(Asset).one()
    assert row.asset_type == asset_type and row.source_id == source_id
    assert float(row.capacity_value) == 12.5 and row.capacity_unit == "MMcf/d"
    assert row.geom_line is None


# ------------------------------------------------ several registries per asset type (2026-09-19)
def _typed_frame(source_id: str, source_asset_id: str = "1") -> pd.DataFrame:
    frame = sample_frame().drop(columns=["technologies", "capacity_mw"])
    frame["source_id"] = source_id
    frame["source_asset_id"] = source_asset_id
    return frame


def test_two_registries_for_one_asset_type_coexist_in_one_store(session):
    from services.ingest.assets import resolve_source_id

    assert resolve_source_id(_typed_frame("us.epa.lmop"), "rng_project") == "us.epa.lmop"
    assert resolve_source_id(_typed_frame("us.epa.agstar"), "rng_project") == "us.epa.agstar"
    assert resolve_source_id(sample_frame(), "rng_project") == "us.epa.lmop"  # no column: first wired
    load_assets(session, _typed_frame("us.epa.lmop"), "rng_project")
    load_assets(session, _typed_frame("us.epa.agstar"), "rng_project")  # same source_asset_id "1"
    load_assets(session, _typed_frame("us.eia.atlas.ethanol_plants"), "ethanol_plant")
    load_assets(session, _typed_frame("us.eia.ethanol_capacity"), "ethanol_plant")
    rows = session.query(Asset).all()
    assert sorted((r.asset_type, r.source_id) for r in rows) == [
        ("ethanol_plant", "us.eia.atlas.ethanol_plants"),
        ("ethanol_plant", "us.eia.ethanol_capacity"),
        ("rng_project", "us.epa.agstar"),
        ("rng_project", "us.epa.lmop"),
    ]
    again = load_assets(session, _typed_frame("us.epa.agstar"), "rng_project")
    assert again.inserted == 0 and again.updated == 1
    assert session.query(Asset).count() == 4


def test_source_id_must_be_wired_for_the_type(session):
    with pytest.raises(UnsupportedAssetTypeError, match="not wired"):
        load_assets(session, _typed_frame("us.epa.lmop"), "ethanol_plant")
    with pytest.raises(UnsupportedAssetTypeError, match="not wired"):
        load_assets(session, sample_frame(), "power_plant", source_id="us.epa.lmop")
    mixed = pd.concat(
        [_typed_frame("us.epa.lmop", "1"), _typed_frame("us.epa.agstar", "2")], ignore_index=True
    )
    with pytest.raises(UnsupportedAssetTypeError, match="mixes"):
        load_assets(session, mixed, "rng_project")


_FUELS_PARQUETS = {
    "rng_project": ("us.epa.lmop", "us.epa.agstar"),
    "ethanol_plant": ("us.eia.atlas.ethanol_plants", "us.eia.ethanol_capacity"),
}


@pytest.mark.parametrize("asset_type", sorted(_FUELS_PARQUETS))
def test_both_real_fuels_parquets_load_into_one_store(session, asset_type):
    """Against the fuels lane's real outputs under data/normalized/context/ (gitignored, so
    skipped where they are absent); the in-memory test above is the one CI always runs."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "data" / "normalized" / "context"
    paths = [root / f"{sid}.parquet" for sid in _FUELS_PARQUETS[asset_type]]
    if not all(p.exists() for p in paths):
        pytest.skip("fuels parquets not present")
    total = 0
    for path in paths:
        result = load_assets_parquet(session, path, asset_type)
        assert result.inserted == result.assets_seen > 0
        total += result.inserted
    assert session.query(Asset).filter(Asset.asset_type == asset_type).count() == total
    assert {r.source_id for r in session.query(Asset).all()} == set(_FUELS_PARQUETS[asset_type])
