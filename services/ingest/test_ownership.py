"""Loader tests for `services/ingest/ownership.py` (EIA-860 Schedule 4 -> `asset_owner`, ADR
0008): aggregation (unweighted mean across generators), organisation resolution/creation via the
alias table, and the unmatched-plant-id report."""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import AssetOwner, Organization, OrganizationAlias
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.assets import load_assets
from services.ingest.ownership import load_owner_shares, load_owner_shares_parquet

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as s:
        yield s


def _seed_plant(session: Session, source_asset_id: str = "1000") -> None:
    load_assets(
        session,
        pd.DataFrame(
            [
                {
                    "source_asset_id": source_asset_id,
                    "name": "Sunrise Solar",
                    "lon": -90.0,
                    "lat": 30.0,
                    "state_code": "US-TX",
                    "country": "US",
                    "source_url": "https://www.eia.gov/electricity/data/eia860m/",
                    "retrieved_at": "2026-09-13T20:25:39Z",
                }
            ]
        ),
        "power_plant",
    )


def owner_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "source_plant_id": "1000",
                "generator_id": "GEN1",
                "owner_name": "NextEra Energy Resources, LLC",
                "ownership_pct": 60.0,
                "as_of": "2024-12-31",
                "source_url": "https://www.eia.gov/electricity/data/eia860/",
                "retrieved_at": "2026-09-18T00:00:00Z",
                "licence": "public-domain",
            },
            {
                "source_plant_id": "1000",
                "generator_id": "GEN2",
                "owner_name": "NextEra Energy Resources, LLC",
                "ownership_pct": 40.0,
                "as_of": "2024-12-31",
                "source_url": "https://www.eia.gov/electricity/data/eia860/",
                "retrieved_at": "2026-09-18T00:00:00Z",
                "licence": "public-domain",
            },
            {
                "source_plant_id": "1000",
                "generator_id": "GEN1",
                "owner_name": "Minority Partner Co",
                "ownership_pct": None,
                "as_of": "2024-12-31",
                "source_url": "https://www.eia.gov/electricity/data/eia860/",
                "retrieved_at": "2026-09-18T00:00:00Z",
                "licence": "public-domain",
            },
            {
                "source_plant_id": "9999",  # no matching asset
                "generator_id": "GENX",
                "owner_name": "Nobody Energy LLC",
                "ownership_pct": 100.0,
                "as_of": "2024-12-31",
                "source_url": "https://www.eia.gov/electricity/data/eia860/",
                "retrieved_at": "2026-09-18T00:00:00Z",
                "licence": "public-domain",
            },
        ]
    )


def test_aggregates_generator_shares_to_one_plant_level_edge(session):
    _seed_plant(session)
    result = load_owner_shares(session, owner_frame())

    assert result.rows_seen == 4
    assert result.assets_matched == 1
    assert result.unmatched_plant_ids == ["9999"]
    assert result.organizations_created == 2  # NextEra + Minority Partner Co
    assert result.edges_written == 2  # two distinct owners for plant 1000

    edges = session.scalars(select(AssetOwner)).all()
    by_owner = {e.organization.name_canonical: e for e in edges}
    nextera = by_owner["NextEra Energy Resources, LLC"]
    assert nextera.share_pct == pytest.approx(50.0)  # unweighted mean of 60 and 40
    assert nextera.role == "owner"
    assert str(nextera.as_of) == "2024-12-31"

    minority = by_owner["Minority Partner Co"]
    assert minority.share_pct is None  # only a null ownership_pct row


def test_organization_alias_resolves_existing_org(session):
    _seed_plant(session)
    org = Organization(
        public_id="org_existing1",
        slug="nextera-existing",
        name_canonical="NextEra Energy Resources, LLC",
        name_normalised="nextera energy resources, llc",
        type="developer",
        country="US",
    )
    session.add(org)
    session.commit()

    result = load_owner_shares(session, owner_frame())
    assert result.organizations_created == 1  # only Minority Partner Co is new

    edge = session.scalar(select(AssetOwner).where(AssetOwner.organization_id == org.id))
    assert edge is not None
    assert edge.share_pct == pytest.approx(50.0)


def test_a_second_run_updates_the_same_edge_without_duplicating(session):
    _seed_plant(session)
    load_owner_shares(session, owner_frame())
    load_owner_shares(session, owner_frame())

    edges = session.scalars(select(AssetOwner)).all()
    assert len(edges) == 2  # still one edge per (asset, organization, role)


def test_alias_row_written_for_new_organization(session):
    _seed_plant(session)
    load_owner_shares(session, owner_frame())
    alias = session.scalar(
        select(OrganizationAlias).where(OrganizationAlias.alias_normalised == "nextera energy resources, llc")
    )
    assert alias is not None
    assert alias.created_by == "pipeline"
    assert alias.kind == "filing_spelling"


def test_norm_org_merges_corp_suffix_and_punctuation_variants(session):
    """Coordinator correction, 2026-09-18: resolution must go through `pipeline.normalize.norm_org`
    (corp-suffix/punctuation-stripping), not a raw case-fold, or "NextEra Energy Resources, LLC"
    and "NEXTERA ENERGY RESOURCES LLC" split into two organisations."""
    _seed_plant(session, "1000")
    _seed_plant(session, "2000")
    df = pd.DataFrame(
        [
            {
                "source_plant_id": "1000",
                "generator_id": "GEN1",
                "owner_name": "NextEra Energy Resources, LLC",
                "ownership_pct": 100.0,
                "as_of": "2024-12-31",
                "source_url": "https://www.eia.gov/electricity/data/eia860/",
                "retrieved_at": "2026-09-18T00:00:00Z",
                "licence": "public-domain",
            },
            {
                "source_plant_id": "2000",
                "generator_id": "GEN1",
                "owner_name": "NEXTERA ENERGY RESOURCES LLC",
                "ownership_pct": 100.0,
                "as_of": "2024-12-31",
                "source_url": "https://www.eia.gov/electricity/data/eia860/",
                "retrieved_at": "2026-09-18T00:00:00Z",
                "licence": "public-domain",
            },
        ]
    )
    result = load_owner_shares(session, df)

    assert result.organizations_created == 1
    assert result.edges_written == 2
    orgs = session.scalars(select(Organization)).all()
    assert len(orgs) == 1
    edges = session.scalars(select(AssetOwner)).all()
    assert {e.organization_id for e in edges} == {orgs[0].id}
    aliases = session.scalars(
        select(OrganizationAlias).where(OrganizationAlias.organization_id == orgs[0].id)
    ).all()
    assert {a.alias_normalised for a in aliases} == {
        "nextera energy resources, llc",
        "nextera energy resources llc",
    }
    assert all(float(a.confidence) == pytest.approx(0.9) for a in aliases)


def test_load_owner_shares_parquet_reads_a_file(session, tmp_path):
    _seed_plant(session)
    path = tmp_path / "owners.parquet"
    owner_frame().to_parquet(path, index=False)
    result = load_owner_shares_parquet(session, path)
    assert result.edges_written == 2
