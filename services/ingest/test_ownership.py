"""Loader tests for `services/ingest/ownership.py` (EIA-860 Schedule 4 -> `asset_owner`, ADR
0008): aggregation (unweighted mean across generators), organisation resolution/creation via the
alias table, and the unmatched-plant-id report; and the resolver's cost when a load creates thousands
of organisations (the slug and alias lookups, docs/16 §5)."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from services.db.models import AssetOwner, Organization, OrganizationAlias, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.assets import load_assets
from services.ingest.loader import upsert_licence_and_source
from services.ingest.ownership import (
    OWNERSHIP_SOURCE_ID,
    StoredOrganizationSlugs,
    _alias_keys,
    _build_norm_org_index,
    _resolve_organization,
    load_owner_shares,
    load_owner_shares_parquet,
)

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


# ------------------------------------------------------ plant-level share arithmetic (2026-09-19)
def _row(plant: str, gen: str, owner: str, pct: float | None, gen_mw: float | None, plant_mw: float | None):
    return {
        "source_plant_id": plant,
        "generator_id": gen,
        "owner_name": owner,
        "ownership_pct": pct,
        "generator_status": "OP" if gen_mw is not None else "RE",
        "generator_capacity_mw": gen_mw,
        "plant_capacity_mw": plant_mw,
        "as_of": "2025-12-31",
        "source_url": "https://www.eia.gov/electricity/data/eia860/xls/eia8602025.zip",
        "retrieved_at": "2026-09-19T20:48:22Z",
        "licence": "public-domain",
    }


def test_share_is_owned_nameplate_over_plant_nameplate_including_unlisted_units(session):
    """Plant 1000: GEN1 100 MW (A 60 %, B 40 %), GEN2 300 MW (A 100 %), plus 100 MW of generators
    Schedule 4 does not list (wholly operator-owned) -> plant total 500 MW.
    A = (60*100 + 100*300) / 500 = 72 %; B = 40*100 / 500 = 8 %. Owner C only appears on a retired
    generator with no operable nameplate: the edge exists, its share is NULL, nothing is invented."""
    _seed_plant(session)
    df = pd.DataFrame(
        [
            _row("1000", "GEN1", "Owner A LLC", 60.0, 100.0, 500.0),
            _row("1000", "GEN1", "Owner B LLC", 40.0, 100.0, 500.0),
            _row("1000", "GEN2", "Owner A LLC", 100.0, 300.0, 500.0),
            _row("1000", "GEN9", "Owner C LLC", 100.0, None, 500.0),
        ]
    )
    result = load_owner_shares(session, df)

    assert result.edges_written == 3
    assert result.edges_nameplate_weighted == 2
    assert result.edges_without_share == 1
    assert result.edges_unweighted_mean == 0
    assert result.generators_over_100 == {}
    by_owner = {e.owner_name_raw: e for e in session.scalars(select(AssetOwner)).all()}
    assert float(by_owner["Owner A LLC"].share_pct) == pytest.approx(72.0)
    assert float(by_owner["Owner B LLC"].share_pct) == pytest.approx(8.0)
    assert by_owner["Owner C LLC"].share_pct is None
    assert str(by_owner["Owner A LLC"].as_of) == "2025-12-31"


def test_unweighted_mean_when_the_parquet_has_no_capacity_columns(session):
    _seed_plant(session)
    result = load_owner_shares(session, owner_frame())
    assert result.edges_unweighted_mean == 1  # NextEra: mean of 60 and 40
    assert result.edges_without_share == 1  # Minority Partner Co: only a null pct row
    assert result.edges_nameplate_weighted == 0


def test_generator_shares_over_100_are_flagged_and_left_as_stated(session):
    _seed_plant(session)
    df = pd.DataFrame(
        [
            _row("1000", "GEN1", "Owner A LLC", 60.0, 100.0, 100.0),
            _row("1000", "GEN1", "Owner B LLC", 60.0, 100.0, 100.0),
            _row("1000", "GEN2", "Owner A LLC", 99.99, 100.0, 100.0),  # under 100: not flagged
        ]
    )
    result = load_owner_shares(session, df)

    assert result.generators_over_100 == {"1000/GEN1": 120.0}
    by_owner = {e.owner_name_raw: float(e.share_pct) for e in session.scalars(select(AssetOwner)).all()}
    # Nothing is rescaled: A = (60*100 + 99.99*100) / 100 = 159.99 as the registry states it.
    assert by_owner["Owner A LLC"] == pytest.approx(159.99)
    assert by_owner["Owner B LLC"] == pytest.approx(60.0)


def test_two_spellings_of_one_owner_at_one_plant_aggregate_into_one_edge(session):
    """The group key is `norm_org`, so the corp-suffix/punctuation variants of one owner at the
    same plant are one group (one edge, mean over both generators), not two groups where the
    second overwrote the first (the 2026-09-18 known limitation)."""
    _seed_plant(session)
    df = pd.DataFrame(
        [
            _row("1000", "GEN1", "NextEra Energy Resources, LLC", 60.0, None, None),
            _row("1000", "GEN2", "NEXTERA ENERGY RESOURCES LLC", 40.0, None, None),
        ]
    )
    result = load_owner_shares(session, df)
    assert result.edges_written == 1
    edges = session.scalars(select(AssetOwner)).all()
    assert len(edges) == 1
    assert float(edges[0].share_pct) == pytest.approx(50.0)
    assert edges[0].owner_name_raw == "NEXTERA ENERGY RESOURCES LLC"  # alphabetically first, for audit


def test_placeholder_owner_other_is_not_attributed(session):
    _seed_plant(session)
    df = pd.DataFrame(
        [
            _row("1000", "GEN1", "Other", 25.96, 100.0, 100.0),
            _row("1000", "GEN1", "Real Owner LLC", 74.04, 100.0, 100.0),
        ]
    )
    result = load_owner_shares(session, df)
    assert result.rows_skipped_placeholder_owner == 1
    assert result.edges_written == 1
    assert session.scalar(select(Organization).where(Organization.name_canonical == "Other")) is None


def test_rerun_is_idempotent_for_a_name_norm_org_strips_entirely(session):
    """The index, the lookup and the group key must be one function, or every re-run re-creates
    the organisation and its edges (measured 2026-09-19 on the real file: 1 organisation, 20
    edges, for "US Solar" -- `norm_org` then stripped both tokens as sector suffixes and returned
    None, so the row was indexed under a key nothing looked up). Since the key strips legal forms
    only, `norm_org("US Solar")` is "US SOLAR" and only a string that is *nothing but* legal forms
    still needs `org_key`'s fallback; both spellings are exercised here."""
    _seed_plant(session, "1000")
    _seed_plant(session, "2000")
    df = pd.DataFrame(
        [
            _row("1000", "GEN1", "US Solar", 100.0, None, None),
            _row("2000", "GEN1", "US Solar", 100.0, None, None),
        ]
    )
    first = load_owner_shares(session, df)
    second = load_owner_shares(session, df)
    assert first.organizations_created == 1
    assert second.organizations_created == 0
    assert second.organizations_matched == 1
    assert len(session.scalars(select(Organization)).all()) == 1
    assert len(session.scalars(select(AssetOwner)).all()) == 2


def test_rerun_is_idempotent_for_a_name_that_is_only_a_legal_form(session):
    """The `org_key` fallback itself: `norm_org("L.L.C.")` is None, so index, lookup and group
    key all have to agree on the bare upper-cased string."""
    _seed_plant(session, "1000")
    df = pd.DataFrame([_row("1000", "GEN1", "L.L.C.", 100.0, None, None)])
    first = load_owner_shares(session, df)
    second = load_owner_shares(session, df)
    assert first.organizations_created == 1
    assert second.organizations_created == 0
    assert second.organizations_matched == 1
    assert len(session.scalars(select(Organization)).all()) == 1


def test_organizations_matched_counts_distinct_pre_existing_organizations(session):
    _seed_plant(session)
    session.add(
        Organization(
            public_id="org_existing2",
            slug="nextera-existing-2",
            name_canonical="NextEra Energy Resources LLC",
            name_normalised="nextera energy resources llc",
            type="developer",
            country="US",
        )
    )
    session.commit()
    result = load_owner_shares(session, owner_frame())
    assert result.organizations_matched == 1  # NextEra, once, although it owns two generators
    assert result.organizations_created == 1  # Minority Partner Co


def test_report_samples_the_unmatched_plant_ids_and_counts_them_all(session):
    """`web/dev_up.py` logs this report as one line, so the id list in it is capped; the full list
    stays on the dataclass."""
    _seed_plant(session)
    df = pd.concat(
        [owner_frame()] + [owner_frame().assign(source_plant_id=str(90000 + i)) for i in range(12)]
    )
    result = load_owner_shares(session, df)
    report = result.as_report()
    assert report["unmatched_plant_count"] == 13  # "9999" plus the twelve synthetic plants
    assert len(report["unmatched_plant_ids_sample"]) == 10
    assert len(set(result.unmatched_plant_ids)) == 13


# --------------------------------------------- creating organisations at scale (docs/16 U-3, §5)
NOW = dt.datetime(2026, 10, 21, tzinfo=dt.UTC)


def _statements(session: Session) -> list[str]:
    """Every SQL statement the session's engine runs from here on, whitespace-collapsed."""
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(" ".join(statement.split()))

    event.listen(session.get_bind(), "before_cursor_execute", record)
    return seen


def _ownership_source(session: Session) -> Source:
    registry = Registry()
    return upsert_licence_and_source(session, registry.get(OWNERSHIP_SOURCE_ID), registry.version)


def _new_org(name: str, slug: str) -> Organization:
    return Organization(
        public_id=f"org_{slug}",
        slug=slug,
        name_canonical=name,
        name_normalised=name.lower(),
        type="other",
        country="US",
    )


def test_stored_slugs_answer_from_the_store_including_an_organisation_not_yet_flushed(session):
    taken = StoredOrganizationSlugs(session)
    session.add(_new_org("Delta Solar", "delta-solar"))
    session.commit()
    session.add(_new_org("Epsilon Wind", "epsilon-wind"))  # pending: autoflush makes it count
    assert "delta-solar" in taken and "epsilon-wind" in taken
    assert "zeta-storage" not in taken


def test_new_organisations_get_unique_slugs_without_reading_every_slug(session):
    """`_resolve_organization` read every slug in the table for each organisation it created: 264 s
    of the 16,472-plant EIA-860M operator load, which creates about 6,100 (docs/16 §5). Each
    candidate is now one indexed lookup. Slugs stay unique against the table and within the run:
    `org_key` keeps `&` as AND, so these are four organisations, and `acme-power` is everyone's
    first choice."""
    session.add(_new_org("Acme Power", "acme-power"))
    session.commit()
    index = _build_norm_org_index(session)
    source = _ownership_source(session)
    statements = _statements(session)
    created = [0]
    names = ("Acme & Power", "ACME & POWER INC", "Beta Gas", "Beta & Gas")
    orgs = [
        _resolve_organization(session, index, name, source=source, now=NOW, created_counter=created)
        for name in names
    ]
    session.commit()
    issued = list(statements)  # before the assertions' own reads
    assert created == [3]  # the second spelling is the first organisation
    assert orgs[0] is orgs[1]
    slugs = list(session.scalars(select(Organization.slug)))
    assert len(slugs) == len(set(slugs)) == 4
    assert orgs[0].slug.startswith("acme-power-") and orgs[2].slug == "beta-gas"
    assert orgs[3].slug.startswith("beta-gas-")  # beta-gas was taken earlier in this run
    whole_column = [s for s in issued if re.search(r"FROM organization\b(?!_)", s) and " WHERE " not in s]
    assert whole_column == []


def test_alias_keys_answer_the_alias_check_without_a_query_per_name(session):
    index = _build_norm_org_index(session)
    keys = _alias_keys(session)
    source = _ownership_source(session)
    statements = _statements(session)
    for name in ["Gamma Wind LLC"] * 20 + ["GAMMA WIND, L.L.C."]:
        _resolve_organization(
            session, index, name, source=source, now=NOW, created_counter=[0], alias_keys=keys
        )
    session.commit()
    issued = list(statements)  # before the assertions' own reads
    assert sorted(session.scalars(select(OrganizationAlias.alias))) == [
        "GAMMA WIND, L.L.C.",
        "Gamma Wind LLC",
    ]
    assert [s for s in issued if s.startswith("SELECT") and "FROM organization_alias" in s] == []
    # Written through, so a second pass over the same names adds nothing.
    for name in ("Gamma Wind LLC", "GAMMA WIND, L.L.C."):
        _resolve_organization(
            session, index, name, source=source, now=NOW, created_counter=[0], alias_keys=keys
        )
    session.commit()
    assert len(session.scalars(select(OrganizationAlias)).all()) == 2


def test_owner_shares_read_the_alias_table_once_per_load(session):
    _seed_plant(session)
    statements = _statements(session)
    load_owner_shares(session, owner_frame())
    assert len([s for s in statements if s.startswith("SELECT") and "FROM organization_alias" in s]) == 2
