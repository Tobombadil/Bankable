"""Loader tests for `services/ingest/midstream.py`: operator/owner edges from a context parquet's
raw strings (dedupe through `norm_org`, idempotent re-run, unmatched assets reported) and the
curated parent file (`organization.parent_org_id` / `parent_source_id` / `parent_as_of`)."""

from __future__ import annotations

import datetime as dt
import pathlib

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.normalize import org_key
from services.db.models import Asset, AssetOwner, Organization
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.assets import UnsupportedAssetTypeError, load_assets
from services.ingest.midstream import (
    DEFAULT_PARENTS_PATH,
    PARENTS_SOURCE_ID,
    load_operator_edges,
    load_operator_edges_parquet,
    load_parents,
    read_parent_rules,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as s:
        yield s


def pipelines_frame() -> pd.DataFrame:
    common = {
        "status": "operating",
        "technology": "interstate",
        "country": "US",
        "source_url": "https://www.eia.gov/maps/map_data/NaturalGas_InterIntrastate_Pipelines_US_EIA.zip",
        "retrieved_at": "2026-09-19T15:34:29Z",
    }
    return pd.DataFrame(
        [
            {
                "source_asset_id": "rockies-express-pipeline-interstate",
                "name": "Rockies Express Pipeline",
                "operator_name": "Rockies Express Pipeline",
                "owner_name": None,
                "lon": -82.77,
                "lat": 39.93,
                "state_code": "US-OH",
                "geom_line_wkt": "MULTILINESTRING((-104.28 40.99, -104.1 41), (-82.8 39.9, -82.7 39.95))",
                "attributes": {"miles": 1321.2, "segment_count": 49, "states_crossed": ["US-CO", "US-OH"]},
                **common,
            },
            {
                "source_asset_id": "rockies-express-entrega-interstate",
                "name": "Rockies Express (Entrega)",
                # same organisation once corp suffix/punctuation are stripped (norm_org)
                "operator_name": "ROCKIES EXPRESS PIPELINE, LLC",
                "owner_name": None,
                "lon": -108.1,
                "lat": 40.37,
                "state_code": "US-CO",
                "geom_line_wkt": "MULTILINESTRING((-108.2 40.3, -108.0 40.4))",
                "attributes": {"miles": 328.3, "segment_count": 16, "states_crossed": ["US-CO", "US-WY"]},
                **common,
            },
        ]
    )


def processing_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "source_asset_id": "b05602e853e0fddb",
                "name": "Douglas Plant",
                "operator_name": "Tallgrass Energy Midstream LLC",
                "owner_name": "Tallgrass Energy Midstream LLC",
                "capacity_value": 130.0,
                "capacity_unit": "MMcf/d",
                "lon": -105.4,
                "lat": 42.75,
                "state_code": "US-WY",
                "country": "US",
                "attributes": {"period": 2017},
                "source_url": "https://www.eia.gov/maps/map_data/NaturalGas_ProcessingPlants_US_EIA.zip",
                "retrieved_at": "2026-09-19T15:34:36Z",
            },
            {
                "source_asset_id": "aa7d910d229e38bc",
                "name": "Casper Plant",
                "operator_name": None,
                "owner_name": "Tallgrass Energy Midstream LLC",
                "capacity_value": 65.0,
                "capacity_unit": "MMcf/d",
                "lon": -106.3,
                "lat": 42.85,
                "state_code": "US-WY",
                "country": "US",
                "attributes": {"period": 2017},
                "source_url": "https://www.eia.gov/maps/map_data/NaturalGas_ProcessingPlants_US_EIA.zip",
                "retrieved_at": "2026-09-19T15:34:36Z",
            },
        ]
    )


# ------------------------------------------------------------------------------ operator edges
def test_operator_edges_dedupe_organisations_by_norm_org(session):
    load_assets(session, pipelines_frame(), "gas_pipeline")
    result = load_operator_edges(session, pipelines_frame(), "gas_pipeline")
    assert result.assets_matched == 2 and result.unmatched_asset_ids == []
    assert result.organizations_created == 1  # both spellings -> one organisation
    assert result.edges_written == 2 and result.edges_by_role == {"operator": 2}

    orgs = session.scalars(select(Organization)).all()
    assert len(orgs) == 1
    edges = session.scalars(select(AssetOwner)).all()
    assert len(edges) == 2
    assert {e.role for e in edges} == {"operator"}
    assert all(e.share_pct is None and e.as_of is None for e in edges)
    assert {e.owner_name_raw for e in edges} == {"Rockies Express Pipeline", "ROCKIES EXPRESS PIPELINE, LLC"}
    assert {e.source_id for e in edges} == {"us.eia.atlas.gas_pipelines"}
    assert all(e.source_url.endswith("Pipelines_US_EIA.zip") for e in edges)
    assert all(e.licence_id == session.get(Asset, e.asset_id).licence_id for e in edges)


def test_operator_edges_rerun_writes_no_duplicates(session):
    load_assets(session, pipelines_frame(), "gas_pipeline")
    load_operator_edges(session, pipelines_frame(), "gas_pipeline")
    again = load_operator_edges(session, pipelines_frame(), "gas_pipeline")
    assert again.organizations_created == 0 and again.edges_written == 2
    assert len(session.scalars(select(AssetOwner)).all()) == 2
    assert len(session.scalars(select(Organization)).all()) == 1


def test_owner_and_operator_roles_from_one_string_share_one_organisation(session):
    load_assets(session, processing_frame(), "gas_processing_plant")
    result = load_operator_edges(session, processing_frame(), "gas_processing_plant")
    assert result.edges_by_role == {"operator": 1, "owner": 2}
    assert result.organizations_created == 1
    org = session.scalars(select(Organization)).one()
    edges = session.scalars(select(AssetOwner).where(AssetOwner.organization_id == org.id)).all()
    assert sorted((session.get(Asset, e.asset_id).name, e.role) for e in edges) == [
        ("Casper Plant", "owner"),
        ("Douglas Plant", "operator"),
        ("Douglas Plant", "owner"),
    ]


def test_operator_edges_reuse_an_existing_organisation_and_alias(session):
    """A string already known to the store (here as a sponsor created by another loader) is
    matched, not duplicated."""
    load_assets(session, pipelines_frame(), "gas_pipeline")
    load_operator_edges(session, pipelines_frame(), "gas_pipeline")
    existing = session.scalars(select(Organization)).one()
    load_assets(session, processing_frame(), "gas_processing_plant")
    frame = processing_frame()
    frame.loc[0, "operator_name"] = "Rockies Express Pipeline LLC"
    result = load_operator_edges(session, frame, "gas_processing_plant")
    assert result.organizations_created == 1  # only Tallgrass Energy Midstream is new
    douglas = session.scalars(select(Asset).where(Asset.name == "Douglas Plant")).one()
    op_edge = session.scalars(
        select(AssetOwner).where(AssetOwner.asset_id == douglas.id, AssetOwner.role == "operator")
    ).one()
    assert op_edge.organization_id == existing.id


def test_operator_edges_report_unmatched_assets_and_reject_unwired_types(session):
    load_assets(session, pipelines_frame().iloc[:1], "gas_pipeline")
    result = load_operator_edges(session, pipelines_frame(), "gas_pipeline")
    assert result.assets_matched == 1
    assert result.unmatched_asset_ids == ["rockies-express-entrega-interstate"]
    assert result.edges_written == 1
    with pytest.raises(UnsupportedAssetTypeError):
        load_operator_edges(session, pipelines_frame(), "transmission_line")


def test_operator_edges_parquet_reads_a_file(session, tmp_path):
    from pipeline.connectors.base import to_parquet_safe

    path = tmp_path / "pipelines.parquet"
    to_parquet_safe(pipelines_frame()).to_parquet(path, index=False)
    load_assets(session, pd.read_parquet(path), "gas_pipeline")
    result = load_operator_edges_parquet(session, path, "gas_pipeline")
    assert result.edges_written == 2


# -------------------------------------------------------------------------------- parents
PARENTS_YAML = """
parents:
  - child_pattern: '^rockies express'
    parent: Tallgrass Energy
    source_url: https://www.tallgrass.com/energy-solutions/natural-gas
    retrieved_at: '2026-09-19T15:25:00Z'
    note: test
  - child_pattern: '^tallgrass energy midstream'
    parent: Tallgrass Energy
    source_url: https://www.tallgrass.com/energy-solutions/natural-gas
    retrieved_at: '2026-09-19T15:25:00Z'
  - child_pattern: '^nobody matches this'
    parent: Tallgrass Energy
    source_url: https://www.tallgrass.com/energy-solutions/natural-gas
    retrieved_at: '2026-09-19T15:25:00Z'
"""


def _write_parents(tmp_path: pathlib.Path, text: str = PARENTS_YAML) -> pathlib.Path:
    p = tmp_path / "parents.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_read_parent_rules_validates_required_fields(tmp_path):
    rules = read_parent_rules(_write_parents(tmp_path))
    assert [r.parent for r in rules] == ["Tallgrass Energy"] * 3
    assert rules[0].regex.search("ROCKIES EXPRESS PIPELINE, LLC")
    bad = _write_parents(tmp_path, "parents:\n  - child_pattern: x\n    parent: y\n")
    with pytest.raises(ValueError, match="lacks"):
        read_parent_rules(bad)


def test_load_parents_creates_parent_and_links_children(session, tmp_path):
    load_assets(session, pipelines_frame(), "gas_pipeline")
    load_operator_edges(session, pipelines_frame(), "gas_pipeline")
    load_assets(session, processing_frame(), "gas_processing_plant")
    load_operator_edges(session, processing_frame(), "gas_processing_plant")

    result = load_parents(session, _write_parents(tmp_path))
    assert result.rules == 3 and result.parents_created == 1
    assert result.children_linked == 2 and result.children_unchanged == 0
    assert result.rules_without_match == ["^nobody matches this"]
    assert result.linked == {
        "Tallgrass Energy": ["Rockies Express Pipeline", "Tallgrass Energy Midstream LLC"]
    }

    parent = session.scalars(
        select(Organization).where(Organization.name_canonical == "Tallgrass Energy")
    ).one()
    assert parent.parent_org_id is None  # never its own parent
    children = session.scalars(select(Organization).where(Organization.parent_org_id == parent.id)).all()
    assert len(children) == 2
    assert {c.parent_source_id for c in children} == {PARENTS_SOURCE_ID}
    assert {c.name_canonical for c in children} == {
        "Rockies Express Pipeline",
        "Tallgrass Energy Midstream LLC",
    }

    again = load_parents(session, _write_parents(tmp_path))
    assert again.parents_created == 0 and again.children_linked == 0 and again.children_unchanged == 2
    assert len(session.scalars(select(Organization)).all()) == 3


def test_load_parents_reuses_an_existing_parent_organisation(session, tmp_path):
    load_assets(session, pipelines_frame(), "gas_pipeline")
    load_operator_edges(session, pipelines_frame(), "gas_pipeline")
    # a differently-spelled organisation that norm_org equates with the parent name
    frame = processing_frame()
    frame["owner_name"] = "Tallgrass Energy, LP"
    load_assets(session, frame, "gas_processing_plant")
    load_operator_edges(session, frame, "gas_processing_plant")
    before = {o.name_canonical: o.id for o in session.scalars(select(Organization))}

    result = load_parents(session, _write_parents(tmp_path))
    assert result.parents_created == 0
    parent = session.scalars(
        select(Organization).where(Organization.name_canonical == "Tallgrass Energy, LP")
    ).one()
    assert parent.id == before["Tallgrass Energy, LP"]
    rex = session.scalars(
        select(Organization).where(Organization.name_canonical == "Rockies Express Pipeline")
    ).one()
    assert rex.parent_org_id == parent.id


def test_load_parents_matches_on_alias_spellings(session, tmp_path):
    load_assets(session, pipelines_frame().iloc[1:], "gas_pipeline")  # only the ", LLC" spelling
    load_operator_edges(session, pipelines_frame().iloc[1:], "gas_pipeline")
    org = session.scalars(select(Organization)).one()
    assert org.name_canonical == "ROCKIES EXPRESS PIPELINE, LLC"
    result = load_parents(session, _write_parents(tmp_path))
    assert result.children_linked == 1
    session.refresh(org)
    assert org.parent_org_id is not None


# ---------------------------------------------------------------- the dated edge above Tallgrass
#: The shape of the real file's Blackstone rule: a parent over the top of the existing chain, with
#: the one thing the nine operating-company rules cannot state -- the date the ownership began.
DATED_PARENTS_YAML = """
parents:
  - child_pattern: '^rockies express'
    parent: Tallgrass Energy
    source_url: https://www.tallgrass.com/energy-solutions/natural-gas
    retrieved_at: '2026-09-19T15:25:00Z'
  - child_pattern: '^tallgrass energy(,? ?l\\.?p\\.?)?$'
    parent: Blackstone Infrastructure Partners
    as_of: 2019-03-11
    source_url: https://www.sec.gov/Archives/edgar/data/1633651/000119312519070906/d706091d8k.htm
    retrieved_at: '2026-09-21T01:12:00Z'
    note: test
"""


def test_as_of_is_optional_parsed_as_a_date_and_never_taken_from_retrieved_at(tmp_path):
    rules = read_parent_rules(_write_parents(tmp_path, DATED_PARENTS_YAML))
    undated, dated = rules
    assert undated.as_of is None  # a systems list states no date; retrieved_at is not borrowed
    assert dated.as_of == dt.date(2019, 3, 11)
    assert dated.retrieved_at == "2026-09-21T01:12:00Z"


def test_a_malformed_as_of_raises_rather_than_half_loading(session, tmp_path):
    """The whole file is parsed before the session is touched, so a typo in the last row cannot
    leave the first rows applied."""
    load_assets(session, pipelines_frame(), "gas_pipeline")
    load_operator_edges(session, pipelines_frame(), "gas_pipeline")
    bad = DATED_PARENTS_YAML.replace("as_of: 2019-03-11", "as_of: 'March 11 2019'")
    with pytest.raises(ValueError, match="unparseable `as_of`"):
        load_parents(session, _write_parents(tmp_path, bad))
    assert not [o for o in session.scalars(select(Organization)) if o.parent_org_id is not None]
    assert not session.scalars(
        select(Organization).where(Organization.name_canonical == "Blackstone Infrastructure Partners")
    ).all()


def test_the_dated_edge_deepens_the_chain_and_carries_its_date(session, tmp_path):
    load_assets(session, pipelines_frame(), "gas_pipeline")
    load_operator_edges(session, pipelines_frame(), "gas_pipeline")

    result = load_parents(session, _write_parents(tmp_path, DATED_PARENTS_YAML))
    assert result.children_linked == 2 and result.children_dated == 1

    rex = session.scalars(
        select(Organization).where(Organization.name_canonical == "Rockies Express Pipeline")
    ).one()
    # the chain is now two levels: Rockies Express -> Tallgrass Energy -> Blackstone Infrastructure
    chain = []
    node = rex
    while node.parent_org_id is not None:
        node = session.get(Organization, node.parent_org_id)
        chain.append(node.name_canonical)
    assert chain == ["Tallgrass Energy", "Blackstone Infrastructure Partners"]

    tallgrass = session.scalars(
        select(Organization).where(Organization.name_canonical == "Tallgrass Energy")
    ).one()
    assert tallgrass.parent_as_of == dt.date(2019, 3, 11)
    assert tallgrass.parent_source_id == PARENTS_SOURCE_ID
    assert tallgrass.parent_share_pct is None  # no source states BIP's stake; never inferred
    assert rex.parent_as_of is None  # the undated rule stays undated rather than borrowing one

    again = load_parents(session, _write_parents(tmp_path, DATED_PARENTS_YAML))
    assert again.children_linked == 0 and again.children_unchanged == 2 and again.children_dated == 0


def test_the_repo_parent_file_carries_the_dated_blackstone_edge():
    """The real file, not a fixture: the one curated edge whose sources date the transaction."""
    rules = read_parent_rules(DEFAULT_PARENTS_PATH)
    top = [r for r in rules if r.parent != "Tallgrass Energy"]
    assert len(top) == 1, "this lane adds one curated edge, not a campaign"
    edge = top[0]
    assert edge.parent == "Blackstone Infrastructure Partners"
    assert edge.as_of == dt.date(2019, 3, 11)
    assert edge.source_url.startswith("https://www.sec.gov/Archives/edgar/data/1633651/")
    # the fund, never the listed parent: "Blackstone" alone keys to BLACKSTONE, which is GLEIF's
    # BLACKSTONE INC. node (docs/22 §17.3, and the note on the rule)
    assert org_key(edge.parent) == "BLACKSTONE INFRASTRUCTURE PARTNER"
    # and not the asset manager Blackstone spun out in 1994, whose name differs by two letters
    assert org_key(edge.parent).startswith("BLACKSTONE ")
    # narrow: the holding company only, never one of its operating subsidiaries
    assert edge.regex.search("Tallgrass Energy") and edge.regex.search("Tallgrass Energy, LP")
    for child in ("Tallgrass Energy Midstream LLC", "Tallgrass Energy Partners, LP", "Rockies Express"):
        assert not edge.regex.search(child), child
