"""Loader tests for `services/ingest/assets.py`, the generalised asset loader (ADR 0008)."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from services.db.models import Asset, AssetSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.assets import (
    UnsupportedAssetTypeError,
    load_assets,
    load_assets_parquet,
    load_ethanol_plants,
)

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
    # The same token, promoted to a first-class fact about the source: this layer is 2020 data,
    # whatever date we fetched it (`services/ingest/vintage.py`, migration 0018).
    source = session.query(Source).filter_by(id="us.eia.atlas.gas_pipelines").one()
    assert (source.vintage, source.vintage_basis) == ("2020-01", "shapefile_member")


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


# --------------------------------------------------- ethanol_plant resolution (docs/24 §5(a))
def _ethanol_row(
    source_asset_id: str, name: str, city: str, capacity: float, *, state="US-IA", lon=None, lat=None
):
    is_atlas = "atlas" in source_asset_id
    attributes = {"nameplate_capacity_mmgal_yr": capacity}
    if not is_atlas:
        # `pipeline/context/ethanol_capacity.py`'s own field: the workbook's "as of January 1,
        # <year>" title row, carried onto every row (`services.ingest.assets._capacity_report_as_of`
        # reads it to date a merged asset's capacity-report operator edge).
        attributes["as_of_year"] = 2025
    row = {
        "source_asset_id": source_asset_id,
        "name": f"{name} ({city}, {state[-2:]})",
        "operator_name": name,
        "status": "operating",
        "technology": "ethanol",
        "capacity_value": capacity,
        "capacity_unit": "MMgal/yr",
        "state_code": state,
        "country": "US",
        "attributes": attributes,
        "attributes_text": {"site": city} if is_atlas else {"city": city},
        "source_url": "https://example.invalid/ethanol",
        "retrieved_at": "2026-09-19T15:34:29Z",
    }
    if lon is not None and lat is not None:
        row["lon"], row["lat"] = lon, lat
    return row


def test_load_ethanol_plants_merges_a_matched_pair_into_one_asset(session):
    atlas = pd.DataFrame(
        [
            _ethanol_row(
                "atlas-fairmont",
                "Flint Hills Resources Fairmont LLC",
                "Fairmont",
                127.0,
                state="US-NE",
                lon=-97.6,
                lat=40.6,
            )
        ]
    )
    capacity = pd.DataFrame(
        [_ethanol_row("cap-fairmont", "Poet Biorefining-Fairmont", "Fairmont", 128.0, state="US-NE")]
    )
    result = load_ethanol_plants(session, atlas, capacity)
    assert (result.matched_pairs, result.atlas_only, result.capacity_only) == (1, 0, 0)
    assert result.assets_total == 1 and result.assets_inserted == 1

    asset = session.query(Asset).one()
    assert asset.asset_type == "ethanol_plant"
    assert asset.source_id == "us.eia.atlas.ethanol_plants"  # identity/location: the primary
    assert asset.source_asset_id == "atlas-fairmont"
    # operator: current, not the primary source's (coordinator correction, docs/24 §7.1) -- the
    # capacity report's name, since it states one; the Atlas string is kept, not discarded.
    assert asset.operator_name == "Poet Biorefining-Fairmont"
    assert asset.attributes["atlas_operator_name"] == "Flint Hills Resources Fairmont LLC"
    assert asset.geom == (-97.6, 40.6)  # location: Atlas
    assert float(asset.capacity_value) == 128.0  # capacity: the capacity report
    assert asset.attributes["sources"] == {
        "capacity_value": "us.eia.ethanol_capacity",
        "location": "us.eia.atlas.ethanol_plants",
        "name": "us.eia.atlas.ethanol_plants",
        "operator_name": "us.eia.ethanol_capacity",
    }

    links = sorted(asset.sources, key=lambda link: link.source_id)
    assert [link.source_id for link in links] == ["us.eia.atlas.ethanol_plants", "us.eia.ethanol_capacity"]
    atlas_link, capacity_link = links
    assert atlas_link.is_primary is True and atlas_link.match_method == "deterministic_key"
    assert atlas_link.match_score is None
    assert capacity_link.is_primary is False and capacity_link.match_method == "rule"
    assert capacity_link.match_score is not None and 0.0 < float(capacity_link.match_score) <= 1.0
    assert capacity_link.source_record_id == "cap-fairmont"


def test_load_ethanol_plants_writes_the_merged_operator_edge_from_the_capacity_report(session):
    """Coordinator correction, 2026-09-26 (docs/24 §7.1): a merged asset's `operator` edge must be
    the capacity report's current name, dated to that report's own stated vintage, attributed to
    that source -- not silently defaulted to the primary (Atlas) source just because that source
    sets the asset's identity."""
    from services.db.models import AssetOwner

    atlas = pd.DataFrame(
        [
            _ethanol_row(
                "atlas-fairmont",
                "Flint Hills Resources Fairmont LLC",
                "Fairmont",
                127.0,
                state="US-NE",
                lon=-97.6,
                lat=40.6,
            )
        ]
    )
    capacity = pd.DataFrame(
        [_ethanol_row("cap-fairmont", "Poet Biorefining-Fairmont", "Fairmont", 128.0, state="US-NE")]
    )
    result = load_ethanol_plants(session, atlas, capacity)
    assert result.operator_edges_written == 1
    assert (result.operator_edges_from_capacity, result.operator_edges_from_atlas_fallback) == (1, 0)
    assert result.merged_capacity_source_asset_ids == ["cap-fairmont"]

    asset = session.query(Asset).one()
    edge = (
        session.query(AssetOwner).filter(AssetOwner.asset_id == asset.id, AssetOwner.role == "operator").one()
    )
    assert edge.source_id == "us.eia.ethanol_capacity"
    assert edge.owner_name_raw == "Poet Biorefining-Fairmont"
    assert edge.as_of == dt.date(2025, 1, 1)  # the capacity table's own "as of January 1, 2025"
    assert edge.organization.name_canonical == "Poet Biorefining-Fairmont"

    # Idempotent: re-running does not duplicate the edge or change its attribution.
    load_ethanol_plants(session, atlas, capacity)
    edges = (
        session.query(AssetOwner).filter(AssetOwner.asset_id == asset.id, AssetOwner.role == "operator").all()
    )
    assert len(edges) == 1


def test_load_ethanol_plants_falls_back_to_atlas_operator_when_capacity_states_none(session):
    """The rare/theoretical branch: if a matched capacity row somehow stated no operator name, the
    edge and the field both fall back to Atlas's, with no `as_of` (Atlas states no comparable
    dated vintage for this) -- exercised directly since real capacity rows always state one."""
    from services.db.models import AssetOwner

    atlas = pd.DataFrame(
        [
            _ethanol_row(
                "atlas-fairmont",
                "Flint Hills Resources Fairmont LLC",
                "Fairmont",
                127.0,
                state="US-NE",
                lon=-97.6,
                lat=40.6,
            )
        ]
    )
    capacity_row = _ethanol_row("cap-fairmont", "Poet Biorefining-Fairmont", "Fairmont", 128.0, state="US-NE")
    capacity_row["operator_name"] = None
    capacity = pd.DataFrame([capacity_row])

    result = load_ethanol_plants(session, atlas, capacity)
    assert (result.operator_edges_from_capacity, result.operator_edges_from_atlas_fallback) == (0, 1)

    asset = session.query(Asset).one()
    assert asset.operator_name == "Flint Hills Resources Fairmont LLC"
    edge = (
        session.query(AssetOwner).filter(AssetOwner.asset_id == asset.id, AssetOwner.role == "operator").one()
    )
    assert edge.source_id == "us.eia.atlas.ethanol_plants"
    assert edge.as_of is None


def test_load_ethanol_plants_keeps_every_unmatched_row_as_its_own_asset(session):
    """The known case (docs/24 §7.3, this task's brief): a plant present in only one registry --
    here, modelled on Green Plains York, NE, which has no Atlas coordinate or twin -- still loads,
    with one link, not zero."""
    atlas = pd.DataFrame(
        [_ethanol_row("atlas-ord", "Green Plains Ord LLC", "Ord", 57.0, state="US-NE", lon=-98.9, lat=41.6)]
    )
    capacity = pd.DataFrame(
        [
            _ethanol_row("cap-ord", "Green America Biofuels Ord LLC", "Ord", 68.0, state="US-NE"),
            _ethanol_row("cap-york", "Green Plains York LLC", "York", 60.0, state="US-NE"),
        ]
    )
    result = load_ethanol_plants(session, atlas, capacity)
    assert (result.matched_pairs, result.atlas_only, result.capacity_only) == (1, 0, 1)
    assert result.assets_total == 2

    york = session.query(Asset).filter(Asset.source_asset_id == "cap-york").one()
    assert york.source_id == "us.eia.ethanol_capacity"
    assert york.geom is None  # no coordinate in this registry (docs/24)
    assert len(york.sources) == 1
    assert york.sources[0].is_primary is True
    assert york.sources[0].match_method == "deterministic_key"
    assert york.sources[0].match_score is None

    ord_asset = session.query(Asset).filter(Asset.source_asset_id == "atlas-ord").one()
    assert len(ord_asset.sources) == 2  # matched to Green America Biofuels Ord, not to York


def test_load_ethanol_plants_never_drops_a_row(session):
    atlas_towns = ["Prairieview", "Larkspur", "Windham"]
    capacity_towns = ["Millbrook", "Ashfield"]
    atlas = pd.DataFrame(
        [
            _ethanol_row(f"atlas-{i}", f"Atlas Only {t} LLC", t, 40.0, state="US-KS")
            for i, t in enumerate(atlas_towns)
        ]
    )
    capacity = pd.DataFrame(
        [
            _ethanol_row(f"cap-{i}", f"Capacity Only {t} Inc", t, 90.0, state="US-KS")
            for i, t in enumerate(capacity_towns)
        ]
    )
    result = load_ethanol_plants(session, atlas, capacity)
    assert result.atlas_rows == 3 and result.capacity_rows == 2
    assert result.matched_pairs == 0
    assert result.assets_total == 5
    assert session.query(Asset).count() == 5
    assert session.query(AssetSource).count() == 5


def test_load_ethanol_plants_is_idempotent(session):
    atlas = pd.DataFrame(
        [
            _ethanol_row(
                "atlas-fairmont",
                "Flint Hills Resources Fairmont LLC",
                "Fairmont",
                127.0,
                state="US-NE",
                lon=-97.6,
                lat=40.6,
            )
        ]
    )
    capacity = pd.DataFrame(
        [_ethanol_row("cap-fairmont", "Poet Biorefining-Fairmont", "Fairmont", 128.0, state="US-NE")]
    )
    first = load_ethanol_plants(session, atlas, capacity)
    second = load_ethanol_plants(session, atlas, capacity)
    assert first.assets_inserted == 1 and second.assets_inserted == 0
    assert second.assets_updated == 1
    assert session.query(Asset).count() == 1
    assert session.query(AssetSource).count() == 2


def test_load_ethanol_plants_against_real_parquets(session):
    """Measured counts against the real fuels-lane outputs (docs/24 §6.3's "rebuild counts");
    skipped where the gitignored parquets are absent, same rule as
    `test_both_real_fuels_parquets_load_into_one_store` above."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "data" / "normalized" / "context"
    atlas_path = root / "us.eia.atlas.ethanol_plants.parquet"
    capacity_path = root / "us.eia.ethanol_capacity.parquet"
    if not atlas_path.exists() or not capacity_path.exists():
        pytest.skip("ethanol parquets not present")
    atlas_df = pd.read_parquet(atlas_path)
    capacity_df = pd.read_parquet(capacity_path)
    result = load_ethanol_plants(session, atlas_df, capacity_df)
    assert (result.atlas_rows, result.capacity_rows) == (197, 191)
    assert result.assets_total == session.query(Asset).count()
    assert result.assets_total < result.atlas_rows + result.capacity_rows  # duplication resolved
    assert session.query(AssetSource).count() == result.atlas_rows + result.capacity_rows
