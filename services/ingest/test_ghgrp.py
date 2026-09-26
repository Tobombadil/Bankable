"""Loader tests for `services/ingest/ghgrp.py`: owner edges with shares and `as_of`, attributes on
the existing row with inline provenance, unmatched facilities held, and an idempotent re-run."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import Asset, AssetOwner, Organization
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.assets import load_assets
from services.ingest.ghgrp import ATTRIBUTE_KEY, load_ghgrp

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _seed(session: Session) -> None:
    load_assets(
        session,
        pd.DataFrame(
            [
                {
                    "source_asset_id": "54537",
                    "name": "Ferndale Generating Station",
                    "lon": -122.68,
                    "lat": 48.83,
                    "state_code": "US-WA",
                    "country": "US",
                    "technology": "gas_cc",
                    "attributes": {"net_generation_mwh_2025": 1.0},
                    "source_url": "https://www.eia.gov/electricity/data/eia860m/",
                    "retrieved_at": "2026-09-13T20:25:39Z",
                },
                {
                    "source_asset_id": "6060",
                    "name": "Randolph",
                    "lon": -71.03,
                    "lat": 42.162,
                    "state_code": "US-MA",
                    "country": "US",
                    "technology": "gas_ct",
                    "source_url": "https://www.eia.gov/electricity/data/eia860m/",
                    "retrieved_at": "2026-09-13T20:25:39Z",
                },
                # a solar farm 100 m from the landfill emitter with the same town name: never a candidate
                {
                    "source_asset_id": "61732",
                    "name": "Randolph",
                    "lon": -71.04,
                    "lat": 42.16,
                    "state_code": "US-MA",
                    "country": "US",
                    "technology": "solar",
                    "source_url": "https://www.eia.gov/electricity/data/eia860m/",
                    "retrieved_at": "2026-09-13T20:25:39Z",
                },
            ]
        ),
        "power_plant",
    )


def _facilities() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ghgrp_facility_id": "1000001",
                "frs_id": "110000455203",
                "name": "PSE Ferndale Generating Station",
                "state_code": "US-WA",
                "lon": -122.685533,
                "lat": 48.828707,
                "naics_code": "221112",
                "reporting_year": 2023,
                "co2_captured": None,
                "rr_mrv_plan_url": None,
                "subparts": ["C", "D"],
                "subpart_rr": False,
                "subpart_uu": False,
                "subpart_pp": False,
                "share_flag": "not_100",
                "share_sum_pct": 99.0,
                "parents": [
                    {"name": "PUGET HOLDINGS LLC", "share_pct": 74.0},
                    {"name": "Puget Holdings, LLC", "share_pct": 25.0},
                ],
                "source_url": "https://data.epa.gov/efservice/pub_dim_facility/facility_id/1000001/year/2023/JSON",
                "retrieved_at": "2026-09-25T17:54:00Z",
                "licence": "public-domain",
            },
            {
                "ghgrp_facility_id": "1007947",
                "frs_id": None,
                "name": "RANDOLPH LANDFILL",
                "state_code": "US-MA",
                "lon": -71.041,
                "lat": 42.1605,
                "naics_code": "562212",
                "reporting_year": 2023,
                "co2_captured": None,
                "rr_mrv_plan_url": None,
                "subparts": ["HH"],
                "subpart_rr": False,
                "subpart_uu": False,
                "subpart_pp": False,
                "share_flag": "ok",
                "share_sum_pct": 100.0,
                "parents": [{"name": "TOWN OF RANDOLPH", "share_pct": 100.0}],
                "source_url": "u3",
                "retrieved_at": "2026-09-25T17:54:00Z",
                "licence": "public-domain",
            },
            {
                "ghgrp_facility_id": "1999999",
                "frs_id": None,
                "name": "Nowhere Compressor Station",
                "state_code": "US-NE",
                "lon": -99.0,
                "lat": 41.0,
                "naics_code": "486210",
                "reporting_year": 2023,
                "co2_captured": "Y",
                "rr_mrv_plan_url": None,
                "subparts": ["W"],
                "subpart_rr": False,
                "subpart_uu": False,
                "subpart_pp": False,
                "share_flag": "ok",
                "share_sum_pct": 100.0,
                "parents": [
                    {"name": "TALLGRASS DEVELOPMENT LP", "share_pct": 75.0},
                    {"name": "PHILLIPS 66", "share_pct": 25.0},
                ],
                "source_url": "u4",
                "retrieved_at": "2026-09-25T17:54:00Z",
                "licence": "public-domain",
            },
        ]
    )


def _crosswalk() -> pd.DataFrame:
    return pd.DataFrame([{"ghgrp_facility_id": "1000001", "oris_code": "54537"}])


def test_edges_and_attributes_on_the_existing_asset_only(session: Session) -> None:
    _seed(session)
    before = session.scalar(select(Asset.id).where(Asset.source_asset_id == "54537"))
    result, matches = load_ghgrp(session, _facilities(), crosswalk=_crosswalk())

    assert result.facilities_seen == 3
    assert result.matched_crosswalk == 1
    assert result.matched_geo_name == 0
    assert result.held_below_threshold == 1  # Randolph landfill vs the peaker: 0.53 < 0.55
    assert result.held_no_candidate == 1  # nowhere near any asset
    assert result.matched_by_asset_type == {"power_plant": 1}
    assert session.scalar(select(Asset).where(Asset.source_asset_id == "54537")).id == before
    assert len(list(session.scalars(select(Asset)))) == 3  # nothing inserted
    assert set(matches["source_asset_id"]) == {"54537", "6060"}  # the solar farm was never a candidate

    edges = list(session.scalars(select(AssetOwner).where(AssetOwner.source_id == "us.epa.ghgrp")))
    assert len(edges) == 1  # two spellings of one parent -> one organisation, shares summed
    e = edges[0]
    assert e.role == "owner"
    assert float(e.share_pct) == 99.0
    assert e.as_of == dt.date(2023, 12, 31)
    assert e.owner_name_raw == "PUGET HOLDINGS LLC"
    assert e.source_url.endswith("/facility_id/1000001/year/2023/JSON")
    assert e.licence_id.startswith("us.epa.ghgrp#")
    assert result.edges_written == 1 and result.facilities_shares_not_100 == 1
    assert result.facilities_parents_merged == 1
    assert session.scalar(select(Organization).where(Organization.id == e.organization_id)) is not None

    asset = session.scalar(select(Asset).where(Asset.source_asset_id == "54537"))
    attrs = asset.attributes
    assert attrs["net_generation_mwh_2025"] == 1.0  # untouched
    g = attrs[ATTRIBUTE_KEY]
    assert g["ghgrp_facility_id"] == "1000001" and g["frs_id"] == "110000455203"
    assert g["reporting_year"] == 2023 and g["subparts"] == ["C", "D"] and g["subpart_rr"] is False
    assert g["match_method"] == "oris_crosswalk" and g["match_score"] == 1.0
    assert g["source_id"] == "us.epa.ghgrp" and g["source_url"] == e.source_url
    assert g["retrieved_at"] == "2026-09-25T17:54:00Z" and g["licence_id"] == e.licence_id
    for other_id in ("6060", "61732"):
        other = session.scalar(select(Asset).where(Asset.source_asset_id == other_id))
        assert ATTRIBUTE_KEY not in (other.attributes or {})
    assert len(matches) == 2 and matches["accepted"].sum() == 1


def test_rerun_is_idempotent(session: Session) -> None:
    _seed(session)
    load_ghgrp(session, _facilities(), crosswalk=_crosswalk())
    result, _ = load_ghgrp(session, _facilities(), crosswalk=_crosswalk())
    edges = list(session.scalars(select(AssetOwner).where(AssetOwner.source_id == "us.epa.ghgrp")))
    assert len(edges) == 1 and result.edges_written == 1
    assert result.organizations_created == 0 and result.organizations_matched == 1


def test_lower_threshold_accepts_geo_name_and_writes_share_as_stated(session: Session) -> None:
    _seed(session)
    result, _ = load_ghgrp(session, _facilities(), crosswalk=_crosswalk(), threshold=0.5)
    assert result.matched_geo_name == 1
    town = session.scalar(select(AssetOwner).where(AssetOwner.owner_name_raw == "TOWN OF RANDOLPH"))
    assert town is not None and float(town.share_pct) == 100.0 and town.as_of == dt.date(2023, 12, 31)
    asset = session.get(Asset, town.asset_id)
    assert asset.attributes[ATTRIBUTE_KEY]["match_method"] == "geo_name"


def test_empty_frame_writes_nothing(session: Session) -> None:
    _seed(session)
    result, matches = load_ghgrp(session, pd.DataFrame(columns=["ghgrp_facility_id", "lon"]))
    assert result.facilities_seen == 0 and result.edges_written == 0 and matches.empty
