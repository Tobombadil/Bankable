"""Tests for `services/ingest/enrich.py`: the feature merge onto `asset` rows, on SQLite.

Assets and organisations are built through the real loaders (`services.ingest.assets.load_assets`,
`services.ingest.midstream.load_operator_edges`), so the join this module relies on -- operator
string -> organisation -> `asset_owner` edge -- is the one the pipeline actually creates. Feature
parquets are written into a temporary data root in the layout the connectors write.
"""

from __future__ import annotations

import json
import pathlib

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import Asset, Organization
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.assets import load_assets
from services.ingest.enrich import (
    EIA923_SOURCE_ID,
    PHMSA_SOURCE_ID,
    RFS_SOURCE_ID,
    apply_context_features,
    derive_plant_features,
    read_alias_rules,
)
from services.ingest.midstream import load_operator_edges

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


# --------------------------------------------------------------------------------- input frames
def pipelines_frame() -> pd.DataFrame:
    common = {
        "status": "operating",
        "technology": "interstate",
        "country": "US",
        "source_id": "us.eia.atlas.gas_pipelines",
        "source_url": "https://www.eia.gov/maps/map_data/NaturalGas_InterIntrastate_Pipelines_US_EIA.zip",
        "retrieved_at": "2026-09-19T15:34:29Z",
        "attributes": {"miles": 100.0},
    }
    return pd.DataFrame(
        [
            # matches PHMSA on the shared organisation key (norm_org strips ", LLC")
            {"source_asset_id": "tigt", "name": "Tallgrass Interstate Gas Transmission",
             "operator_name": "Tallgrass Interstate Gas Transmission", "owner_name": None, **common},
            # matches only through the curated alias file: PHMSA files as "RUBY LLC"
            {"source_asset_id": "ruby", "name": "Ruby Pipeline LLC",
             "operator_name": "Ruby Pipeline LLC", "owner_name": None, **common},
            # matches only after abbreviation expansion: the Atlas's "Trans Co" against PHMSA's
            # "TRANSMISSION, LLC"
            {"source_asset_id": "columbia-gas", "name": "Columbia Gas Trans Co",
             "operator_name": "Columbia Gas Trans Co", "owner_name": None, **common},
            # no PHMSA operator of that name at all
            {"source_asset_id": "unknown-line", "name": "Nowhere Gathering System",
             "operator_name": "Nowhere Gathering System", "owner_name": None, **common},
        ]
    )  # fmt: skip


def plants_frame() -> pd.DataFrame:
    common = {
        "status": "operating",
        "country": "US",
        "source_id": "us.eia.860m",
        "source_url": "https://www.eia.gov/electricity/data/eia860m/",
        "retrieved_at": "2026-09-13T20:25:39Z",
    }
    return pd.DataFrame(
        [
            {"source_asset_id": "3", "name": "Barry", "capacity_mw": 2500.0, "technology": "gas", **common},
            {"source_asset_id": "2", "name": "Bankhead Dam", "capacity_mw": 60.0,
             "technology": "hydro", **common},
            {"source_asset_id": "999", "name": "No Capacity", "capacity_mw": None,
             "technology": "gas", **common},
        ]
    )  # fmt: skip


def fuels_frame() -> pd.DataFrame:
    common = {
        "status": "operating",
        "country": "US",
        "source_url": "https://www.eia.gov/petroleum/ethanolcapacity/",
        "retrieved_at": "2026-09-19T15:49:01Z",
    }
    return pd.DataFrame(
        [
            {"source_asset_id": "IA-absolute", "name": "Absolute Energy LLC (St Ansgar, IA)",
             "operator_name": "Absolute Energy LLC", "state_code": "US-IA",
             "source_id": "us.eia.ethanol_capacity", **common},
            {"source_asset_id": "NY-wny", "name": "Western New York Energy LLC (Medina, NY)",
             "operator_name": "Western New York Energy LLC", "state_code": "US-NY",
             "source_id": "us.eia.ethanol_capacity", **common},
            {"source_asset_id": "IA-nomatch", "name": "Unregistered Ethanol Co (Ames, IA)",
             "operator_name": "Unregistered Ethanol Co", "state_code": "US-IA",
             "source_id": "us.eia.ethanol_capacity", **common},
        ]
    )  # fmt: skip


def phmsa_rows() -> pd.DataFrame:
    common = {
        "source_id": PHMSA_SOURCE_ID,
        "source_url": "https://www.phmsa.dot.gov/sites/phmsa.dot.gov/files/data_statistics/pipeline/annual_gas_transmission_gathering_2010_present.zip",
        "retrieved_at": "2026-09-19T21:55:04Z",
        "report_year": 2025,
        "incidents_5y_significant": None,
        "incident_years": [2021, 2022, 2023, 2024, 2025],
        "feature_flags": ["incidents_5y_significant unavailable: the flagged file could not be retrieved"],
    }
    return pd.DataFrame(
        [
            {"operator_id": "1007", "operator_name": "TALLGRASS INTERSTATE GAS TRANSMISSION, LLC",
             "onshore_transmission_miles": 4308.32, "miles_by_decade": {"1950s": 1000.0, "2000s": 3308.32},
             "miles_by_diameter": {"8": 500.0, "24": 3808.32}, "incidents_5y_total": 3,
             "incidents_5y_with_fatality": 0, "incidents_5y_with_injury": 1,
             "incidents_5y_with_ignition": 0, "incidents_5y_with_explosion": 0, **common},
            {"operator_id": "40623", "operator_name": "RUBY LLC", "onshore_transmission_miles": 682.81,
             "miles_by_decade": {"2010s": 682.81}, "miles_by_diameter": {"42": 682.81},
             "incidents_5y_total": 0, "incidents_5y_with_fatality": 0, "incidents_5y_with_injury": 0,
             "incidents_5y_with_ignition": 0, "incidents_5y_with_explosion": 0, **common},
            {"operator_id": "2616", "operator_name": "COLUMBIA GAS TRANSMISSION, LLC",
             "onshore_transmission_miles": 10452.0, "miles_by_decade": {"1960s": 10452.0},
             "miles_by_diameter": {"30": 10452.0}, "incidents_5y_total": 2,
             "incidents_5y_with_fatality": 0, "incidents_5y_with_injury": 0,
             "incidents_5y_with_ignition": 1, "incidents_5y_with_explosion": 0, **common},
        ]
    )  # fmt: skip


def eia923_rows() -> pd.DataFrame:
    common = {
        "source_id": EIA923_SOURCE_ID,
        "source_url": "https://www.eia.gov/electricity/data/eia923/xls/f923_2025.zip",
        "retrieved_at": "2026-09-19T21:52:51Z",
        "data_year": 2025,
    }
    return pd.DataFrame(
        [
            # Barry: 11,459,781 MWh on 2,500 MW -> CF 0.523; 77,980,345 MMBtu -> 6,805 Btu/kWh
            {"plant_id": "3", "plant_name": "Barry", "net_generation_mwh": 11459781.0,
             "total_fuel_mmbtu": 77980345.0, "fuel_types": ["NG"], **common},
            # Bankhead Dam: hydro, EIA's 3,412 Btu/kWh equivalence in the fuel column
            {"plant_id": "2", "plant_name": "Bankhead Dam", "net_generation_mwh": 150777.0,
             "total_fuel_mmbtu": 514451.0, "fuel_types": ["WAT"], **common},
            {"plant_id": "999", "plant_name": "No Capacity", "net_generation_mwh": 1000.0,
             "total_fuel_mmbtu": 0.0, "fuel_types": ["SUN"], **common},
            # a plant id with no asset loaded
            {"plant_id": "123456", "plant_name": "Unloaded", "net_generation_mwh": 5.0,
             "total_fuel_mmbtu": 0.0, "fuel_types": ["SUN"], **common},
        ]
    )  # fmt: skip


def rfs_rows() -> pd.DataFrame:
    common = {
        "source_id": RFS_SOURCE_ID,
        "source_url": "https://cdxoarapps.epa.gov/oar-otaq-reg-III/rest/public/reports/Part80FuelsProgramsList",
        "retrieved_at": "2026-09-19T21:52:09Z",
        "first_registered_year": None,
        "feature_flags": ["first_registered_year unavailable: the list states no registration date"],
    }
    return pd.DataFrame(
        [
            {"facility_key": "a1", "company_id": "5049", "company_name": "ABSOLUTE ENERGY LLC",
             "facility_name": "ABSOLUTE ENERGY LLC", "facility_city": "ST ANSGAR",
             "facility_state": "US-IA", "facility_type": "Gas/Ethanol",
             "d_codes": ["D3", "D6"], "pathway_count": 2, **common},
            # the asset's name does not match, but its operator string matches the company
            {"facility_key": "b2", "company_id": "6100", "company_name": "WESTERN NEW YORK ENERGY LLC",
             "facility_name": "MEDINA BIOREFINERY", "facility_city": "MEDINA",
             "facility_state": "US-NY", "facility_type": "Gas/Ethanol",
             "d_codes": ["D6"], "pathway_count": 1, **common},
        ]
    )  # fmt: skip


def write_context(data_root: pathlib.Path, **frames: pd.DataFrame) -> None:
    from pipeline.connectors.base import to_parquet_safe

    context = data_root / "normalized" / "context"
    context.mkdir(parents=True, exist_ok=True)
    for source_id, frame in frames.items():
        to_parquet_safe(frame).to_parquet(context / f"{source_id}.parquet", index=False)


@pytest.fixture()
def loaded(session, tmp_path) -> pathlib.Path:
    pipelines = pipelines_frame()
    load_assets(session, pipelines, "gas_pipeline")
    load_operator_edges(session, pipelines, "gas_pipeline")
    load_assets(session, plants_frame(), "power_plant")
    load_assets(session, fuels_frame(), "ethanol_plant", source_id="us.eia.ethanol_capacity")
    data_root = tmp_path / "data"
    write_context(
        data_root,
        **{
            PHMSA_SOURCE_ID: phmsa_rows(),
            EIA923_SOURCE_ID: eia923_rows(),
            RFS_SOURCE_ID: rfs_rows(),
        },
    )
    return data_root


def attributes(session: Session, source_asset_id: str) -> dict:
    asset = session.scalar(select(Asset).where(Asset.source_asset_id == source_asset_id))
    assert asset is not None
    return dict(asset.attributes or {})


# ------------------------------------------------------------------------------ derived features
def test_capacity_factor_and_heat_rate_are_the_documented_ratios():
    values, flags = derive_plant_features(
        capacity_mw=2500.0, net_generation_mwh=11459781.0, total_fuel_mmbtu=77980345.0,
        year=2025, fuel_types=["NG"],
    )  # fmt: skip
    assert values["capacity_factor_2025"] == pytest.approx(11459781.0 / (2500.0 * 8760), abs=1e-4)
    assert values["heat_rate_btu_kwh_2025"] == pytest.approx(77980345.0 * 1000 / 11459781.0, abs=0.1)
    assert flags == []


def test_an_impossible_capacity_factor_is_null_with_a_flag_not_clamped():
    values, flags = derive_plant_features(
        capacity_mw=10.0, net_generation_mwh=200_000.0, total_fuel_mmbtu=0.0, year=2025, fuel_types=["SUN"]
    )
    assert values["capacity_factor_2025"] is None
    assert any("exceeds 1.05" in f for f in flags)
    # the inputs are still stored: the reader can see what produced the flag
    assert values["net_generation_mwh_2025"] == 200_000.0


def test_a_heat_rate_outside_the_window_is_null_with_a_flag():
    values, flags = derive_plant_features(
        capacity_mw=100.0, net_generation_mwh=1_000.0, total_fuel_mmbtu=100_000.0,
        year=2025, fuel_types=["NG"],
    )  # fmt: skip
    assert values["heat_rate_btu_kwh_2025"] is None
    assert any("outside 5000-30000" in f for f in flags)


def test_non_combustion_plants_get_no_heat_rate_and_no_flag():
    """EIA fills the fuel column for hydro/wind/solar at 3,412 Btu/kWh; that is an energy
    equivalence, not fuel burned, so there is nothing to report and nothing to flag."""
    values, flags = derive_plant_features(
        capacity_mw=60.0, net_generation_mwh=150_777.0, total_fuel_mmbtu=514_451.0,
        year=2025, fuel_types=["WAT"],
    )  # fmt: skip
    assert "heat_rate_btu_kwh_2025" not in values
    assert flags == []
    assert values["capacity_factor_2025"] == pytest.approx(150777.0 / (60.0 * 8760), abs=1e-4)


def test_negative_net_generation_and_missing_capacity_are_flagged():
    values, flags = derive_plant_features(
        capacity_mw=50.0, net_generation_mwh=-1200.0, total_fuel_mmbtu=0.0, year=2025, fuel_types=["MWH"]
    )
    assert values["capacity_factor_2025"] is None
    assert any("not positive" in f for f in flags)

    values, flags = derive_plant_features(
        capacity_mw=None, net_generation_mwh=1000.0, total_fuel_mmbtu=0.0, year=2025, fuel_types=["SUN"]
    )
    assert values["capacity_factor_2025"] is None
    assert any("no nameplate capacity" in f for f in flags)


# ---------------------------------------------------------------------------------- the merge
def test_features_land_on_the_right_assets(session, loaded, tmp_path, monkeypatch):
    # A test-local cross-dataset alias file holding only the Ruby rule, so all three matching steps are
    # exercised; the committed file (which also names Tallgrass Interstate and Columbia Gulf, where
    # a curated rule would win) is checked separately below.
    aliases = tmp_path / "aliases.yaml"
    aliases.write_text(
        "aliases:\n"
        "  - alias: 'RUBY LLC'\n"
        "    organization: 'Ruby Pipeline LLC'\n"
        "    source_id: us.phmsa.pipeline_operator_reports\n"
        "    external_id: '40623'\n"
    )
    monkeypatch.setattr("services.ingest.enrich.ALIASES_PATH", aliases)
    report = apply_context_features(session, loaded)

    assert report["phmsa"]["assets_matched"] == 3
    assert report["phmsa"]["match_rate"] == pytest.approx(0.75)
    assert report["phmsa"]["matched_by"] == {"org_key": 1, "alias_file": 1, "expanded_key": 1}

    tigt = attributes(session, "tigt")["phmsa"]
    assert tigt["operator_id"] == "1007"
    assert tigt["onshore_transmission_miles"] == pytest.approx(4308.32)
    assert tigt["miles_by_decade"]["2000s"] == pytest.approx(3308.32)
    assert tigt["miles_by_diameter"]["24"] == pytest.approx(3808.32)
    assert tigt["incidents_5y_total"] == 3
    assert tigt["incidents_5y_significant"] is None
    assert tigt["matched_on"] == "org_key"
    # the Atlas attributes the loader wrote are untouched
    assert attributes(session, "tigt")["miles"] == 100.0

    assert attributes(session, "ruby")["phmsa"]["matched_on"] == "alias_file"
    assert attributes(session, "columbia-gas")["phmsa"]["matched_on"] == "expanded_key"
    assert "phmsa" not in attributes(session, "unknown-line")


def test_plant_features_use_the_assets_capacity(session, loaded):
    apply_context_features(session, loaded)
    barry = attributes(session, "3")
    assert barry["capacity_factor_2025"] == pytest.approx(0.523, abs=0.002)
    assert barry["heat_rate_btu_kwh_2025"] == pytest.approx(6804.7, abs=1.0)
    assert "feature_flags" not in barry

    bankhead = attributes(session, "2")
    assert "heat_rate_btu_kwh_2025" not in bankhead
    assert bankhead["capacity_factor_2025"] == pytest.approx(0.287, abs=0.002)

    no_capacity = attributes(session, "999")
    assert no_capacity["capacity_factor_2025"] is None
    assert any("no nameplate capacity" in f for f in no_capacity["feature_flags"])
    assert all(f.startswith("eia923:") for f in no_capacity["feature_flags"])


def test_rfs_matches_on_facility_then_company(session, loaded):
    report = apply_context_features(session, loaded)
    assert report["rfs"]["assets_matched"] == 2
    assert report["rfs"]["matched_by"] == {"facility_name": 1, "company_name": 1}
    assert report["rfs"]["by_asset_type"]["ethanol_plant"]["assets"] == 3

    absolute = attributes(session, "IA-absolute")["rfs"]
    assert absolute["d_codes"] == ["D3", "D6"]
    assert absolute["pathway_count"] == 2
    assert absolute["first_registered_year"] is None
    assert absolute["matched_on"] == "facility_name"
    assert attributes(session, "NY-wny")["rfs"]["matched_on"] == "company_name"
    assert "rfs" not in attributes(session, "IA-nomatch")


def test_rows_whose_asset_is_not_loaded_are_counted_not_invented(session, loaded):
    report = apply_context_features(session, loaded)
    assert report["eia923"]["rows_without_an_asset"] == 1
    assert session.scalar(select(Asset).where(Asset.source_asset_id == "123456")) is None


def test_second_run_changes_nothing(session, loaded):
    first = apply_context_features(session, loaded)
    before = {a.source_asset_id: (dict(a.attributes), a.last_changed) for a in session.scalars(select(Asset))}

    second = apply_context_features(session, loaded)
    for key in ("phmsa", "eia923", "rfs"):
        assert second[key]["updated"] == 0, key
        assert second[key]["unchanged"] == first[key]["assets_matched"], key
    after = {a.source_asset_id: (dict(a.attributes), a.last_changed) for a in session.scalars(select(Asset))}
    assert after == before


def test_nothing_is_created(session, loaded):
    organisations = session.scalar(select(Organization).where(Organization.name_canonical == "RUBY LLC"))
    assert organisations is None  # the alias file must not mint the PHMSA spelling as an org
    before = len(list(session.scalars(select(Organization))))
    assets_before = len(list(session.scalars(select(Asset))))
    apply_context_features(session, loaded)
    assert len(list(session.scalars(select(Organization)))) == before
    assert len(list(session.scalars(select(Asset)))) == assets_before


def test_a_missing_parquet_is_reported_not_raised(session, tmp_path):
    report = apply_context_features(session, tmp_path / "empty-root")
    for key in ("phmsa", "eia923", "rfs"):
        assert report[key]["status"] == "missing"
        assert report[key]["assets_matched"] == 0


def test_features_survive_a_json_round_trip(session, loaded):
    """`asset.attributes` is JSON on both dialects; every value written here must serialise."""
    apply_context_features(session, loaded)
    for asset in session.scalars(select(Asset)):
        json.dumps(asset.attributes)


# ---------------------------------------------------------------------------------- alias file
def test_the_committed_cross_dataset_alias_file_covers_the_tallgrass_group():
    rules = read_alias_rules()
    phmsa_rules = rules[PHMSA_SOURCE_ID]
    aliases = {r["alias"] for r in phmsa_rules}
    assert "RUBY LLC" in aliases
    assert "ROCKIES EXPRESS PIPELINE LLC" in aliases
    assert "TALLGRASS INTERSTATE GAS TRANSMISSION, LLC" in aliases
    assert "TRAILBLAZER PIPELINE CO" in aliases
    for rule in phmsa_rules:
        assert rule["organization"] and rule["external_id"]
        assert rule["evidence"]["shared_states"]


def test_alias_file_rows_must_carry_their_provenance(tmp_path):
    bad = tmp_path / "aliases.yaml"
    bad.write_text("aliases:\n  - alias: 'X'\n    organization: 'Y'\n")
    with pytest.raises(ValueError, match="source_id"):
        read_alias_rules(bad)


def test_a_missing_alias_file_is_not_an_error(tmp_path):
    assert read_alias_rules(tmp_path / "nope.yaml") == {}


def test_the_two_alias_files_stay_separate():
    """`aliases.yaml` (ownership lane) records renames of one legal entity and has no `source_id`;
    this lane's file records cross-dataset operator spellings and is the only one loaded here.
    Merging them would silently feed rename rules into the feature merge, or drop them."""
    import yaml

    from services.ingest.enrich import ALIASES_PATH

    assert ALIASES_PATH.name == "external_operator_aliases.yaml"
    renames = yaml.safe_load((ALIASES_PATH.parent / "aliases.yaml").read_text(encoding="utf-8"))
    assert {"alias", "canonical"} <= set(renames["aliases"][0])
    assert "source_id" not in renames["aliases"][0]
    cross = yaml.safe_load(ALIASES_PATH.read_text(encoding="utf-8"))
    assert {"alias", "organization", "source_id", "external_id"} <= set(cross["aliases"][0])
