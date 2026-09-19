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
        load_assets(session, sample_frame(), "gas_pipeline")


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
