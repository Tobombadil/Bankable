"""Loader tests for the `built_plant` context table: insert, idempotent re-run, geom round-trip,
unique constraint (mirrors `services/ingest/test_loader.py`'s fixture/session conventions)."""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pipeline.connectors.base import to_parquet_safe
from services.db.models import BuiltPlant, Licence, Source, new_uuid
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.plants import load_plants, load_plants_parquet

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
                "source_id": "us.eia.860m",
                "source_plant_id": "1000",
                "name": "Sunrise Solar",
                "operator_name": "Sunrise Power LLC",
                "technology": "solar",
                "technology_raw": "Solar Photovoltaic",
                "technologies": {"Solar Photovoltaic": 150.0, "Onshore Wind Turbine": 0.0},
                "capacity_mw": 150.0,
                "generator_count": 3,
                "earliest_operating_year": 2010,
                "lon": -90.0,
                "lat": 30.0,
                "state_code": "US-TX",
                "county_name": "Nolan",
                "country": "US",
                "source_url": "https://www.eia.gov/electricity/data/eia860m/xls/july_generator2026.xlsx",
                "retrieved_at": "2026-09-13T20:25:39Z",
                "licence": "public-domain",
            },
            {
                "source_id": "us.eia.860m",
                "source_plant_id": "2000",
                "name": "Bay Peaker",
                "operator_name": "Bay Gas Co",
                "technology": "gas_ct",
                "technology_raw": "Natural Gas Fired Combustion Turbine",
                "technologies": {"Natural Gas Fired Combustion Turbine": 25.0},
                "capacity_mw": 25.0,
                "generator_count": 1,
                "earliest_operating_year": 2005,
                "lon": None,
                "lat": None,
                "state_code": "US-LA",
                "county_name": "Orleans",
                "country": "US",
                "source_url": "https://www.eia.gov/electricity/data/eia860m/xls/july_generator2026.xlsx",
                "retrieved_at": "2026-09-13T20:25:39Z",
                "licence": "public-domain",
            },
        ]
    )


def test_load_plants_inserts_one_row_per_source_plant_id(session):
    result = load_plants(session, sample_frame())
    assert result.plants_seen == 2
    assert result.inserted == 2
    assert result.updated == 0
    assert result.placed == 1  # the plant with a valid coordinate
    assert result.unplaced == 1

    rows = session.query(BuiltPlant).order_by(BuiltPlant.source_plant_id).all()
    assert [r.source_plant_id for r in rows] == ["1000", "2000"]
    assert rows[0].generator_count == 3
    assert rows[0].technologies == {"Solar Photovoltaic": 150.0, "Onshore Wind Turbine": 0.0}


def test_load_plants_upserts_licence_and_source(session):
    load_plants(session, sample_frame())
    source = session.get(Source, "us.eia.860m")
    assert source is not None
    assert source.publish_state == "public"
    licence = session.get(Licence, source.licence_id)
    assert licence is not None
    assert licence.reuse_class == "open"


def test_geom_round_trips_as_lon_lat_tuple(session):
    load_plants(session, sample_frame())
    plant = session.query(BuiltPlant).filter_by(source_plant_id="1000").one()
    assert plant.geom == pytest.approx((-90.0, 30.0))
    unplaced = session.query(BuiltPlant).filter_by(source_plant_id="2000").one()
    assert unplaced.geom is None


def test_a_second_run_with_the_same_frame_updates_in_place_without_duplicating(session):
    first = load_plants(session, sample_frame())
    second = load_plants(session, sample_frame())

    assert first.inserted == 2
    assert second.inserted == 0
    assert second.updated == 2  # this loader always overwrites on re-run (see module docstring)

    count = session.query(BuiltPlant).count()
    assert count == 2  # no duplicates: idempotent on (source_id, source_plant_id)


def test_unique_constraint_on_source_id_and_source_plant_id_is_enforced(session):
    load_plants(session, sample_frame())
    dup = BuiltPlant(
        id=new_uuid(),
        source_id="us.eia.860m",
        source_plant_id="1000",  # already used above
        name="Duplicate",
        technologies={},
        generator_count=0,
        country="US",
        source_url="https://example.org",
        retrieved_at=pd.Timestamp("2026-09-15", tz="UTC").to_pydatetime(),
        licence_id=session.get(Source, "us.eia.860m").licence_id,
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_load_plants_parquet_reads_a_file(session, tmp_path: pathlib.Path):
    path = tmp_path / "plants.parquet"
    # `to_parquet_safe` is what `pipeline.context.eia_plants`'s own CLI writes through (JSON-
    # encodes the `technologies` dict column) -- matching that real path here, not a bare
    # `DataFrame.to_parquet`, which lets pyarrow infer a shared struct schema across rows with
    # different technology keys and null-fill the gaps.
    to_parquet_safe(sample_frame()).to_parquet(path, index=False)
    result = load_plants_parquet(session, path)
    assert result.inserted == 2


def test_empty_frame_is_a_no_op(session):
    result = load_plants(session, sample_frame().iloc[0:0])
    assert result.plants_seen == 0
    assert result.inserted == 0
    assert session.query(BuiltPlant).count() == 0
