"""Loader tests for `services/ingest/organizations.py` (docs/22 §17): GLEIF Level 2 parent links
behind the country gate, and the curated alias file.

The frames here are cut from the real 2026-09-20 golden copy, so the names, LEIs and dates are the
ones the measured run used. The cases that matter are the two the labelled census
(`data/eval/gleif_matches.csv`) says cost precision: a US operator against its same-named foreign
subsidiary (`Ameresco, Inc.` / `AMERESCO LIMITED`, GB) and one platform name that matches three
GLEIF entities in three countries (`Air Products`).
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import Organization, OrganizationAlias, Proposal
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.assets import load_assets
from services.ingest.midstream import load_operator_edges, load_parents
from services.ingest.organizations import (
    ALIASES_SOURCE_ID,
    GLEIF_SOURCE_ID,
    load_aliases,
    load_gleif_parents,
    org_key_multimap,
    read_alias_rules,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

_PROVENANCE = {
    "source_id": GLEIF_SOURCE_ID,
    "source_url": "https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy",
    "retrieved_at": "2026-09-20T02:19:00Z",
    "licence": "cc0",
}


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _relationship(**kwargs: object) -> dict[str, object]:
    row: dict[str, object] = {
        "child_lei": "X" * 20,
        "child_legal_name": "",
        "child_country": "US",
        "child_region": "US-DE",
        "child_city": "WILMINGTON",
        "child_jurisdiction": "US-DE",
        "child_entity_status": "ACTIVE",
        "parent_lei": "Y" * 20,
        "parent_legal_name": "",
        "parent_country": "US",
        "relationship_type": "IS_DIRECTLY_CONSOLIDATED_BY",
        "relationship_status": "ACTIVE",
        "registration_status": "PUBLISHED",
        "period_start": dt.date(2019, 2, 8),
        "period_end": None,
        "accounting_period_end": dt.date(2024, 12, 31),
        "last_update_date": dt.date(2026, 3, 11),
        **_PROVENANCE,
    }
    row.update(kwargs)
    return row


def gleif_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            _relationship(
                child_lei="D981X4Z4RWS7PDMJUZ03",
                child_legal_name="GEORGIA POWER COMPANY",
                child_region="US-GA",
                parent_lei="549300FC3G3YU2FBZD92",
                parent_legal_name="THE SOUTHERN COMPANY",
            ),
            # the ultimate parent of the same child: direct must win
            _relationship(
                child_lei="D981X4Z4RWS7PDMJUZ03",
                child_legal_name="GEORGIA POWER COMPANY",
                parent_lei="ZZZZ0000000000000000",
                parent_legal_name="SOUTHERN HOLDCO",
                relationship_type="IS_ULTIMATELY_CONSOLIDATED_BY",
                period_start=dt.date(1901, 1, 1),
            ),
            # the GB subsidiary of a US organisation we carry: the country gate's whole job
            _relationship(
                child_lei="98450002A105CB1BD385",
                child_legal_name="AMERESCO LIMITED",
                child_country="GB",
                child_region="GB-LDS",
                child_city="Leeds",
                parent_lei="529900NZXZGBCBXYY327",
                parent_legal_name="AMERESCO, INC.",
            ),
            # a lapsed registration: a record GLEIF no longer stands behind
            _relationship(
                child_lei="AAAA0000000000000001",
                child_legal_name="Lapsed Power Co",
                parent_legal_name="LAPSED HOLDINGS LLC",
                registration_status="LAPSED",
            ),
            # an ended relationship
            _relationship(
                child_lei="AAAA0000000000000002",
                child_legal_name="Former Power Co",
                parent_legal_name="FORMER HOLDINGS LLC",
                relationship_status="INACTIVE",
            ),
        ]
    )


def _seed_asset(session: Session, source_asset_id: str, operator: str, country: str = "US") -> None:
    load_assets(
        session,
        pd.DataFrame(
            [
                {
                    "source_asset_id": source_asset_id,
                    "name": f"{operator} site",
                    "operator_name": operator,
                    "lon": -84.4,
                    "lat": 33.7,
                    "state_code": "US-GA",
                    "country": country,
                    "source_url": "https://www.eia.gov/electricity/data/eia860m/",
                    "retrieved_at": "2026-09-13T20:25:39Z",
                }
            ]
        ),
        "power_plant",
    )


def _seed_org(session: Session, name: str, *, jurisdiction: str | None = None) -> Organization:
    """An organisation plus, when `jurisdiction` is given, a sponsored proposal — which is what
    `evidence_countries` reads, since `organization.country` is hard-coded `US` everywhere."""
    from services.db.models import new_uuid
    from services.ids import public_id as make_public_id
    from services.ids import slugify

    org_id = new_uuid()
    org = Organization(
        id=org_id,
        public_id=make_public_id("org", org_id),
        slug=slugify(name),
        name_canonical=name,
        name_normalised=name.lower(),
        type="other",
        country="US",
    )
    session.add(org)
    session.flush()
    if jurisdiction:
        proposal_id = new_uuid()
        session.add(
            Proposal(
                id=proposal_id,
                public_id=make_public_id("prop", proposal_id),
                slug=f"{slugify(name)}-proposal",
                kind="generation",
                name_canonical=f"{name} project",
                sponsor_org_id=org.id,
                technology="solar",
                jurisdiction=jurisdiction,
                lifecycle_state="filed",
                publish_state="public",
            )
        )
    session.commit()
    return org


def test_direct_parent_wins_and_carries_source_as_of_and_lei(session: Session):
    org = _seed_org(session, "Georgia Power Co", jurisdiction="US-GA")
    result = load_gleif_parents(session, gleif_frame())

    session.refresh(org)
    parent = session.get(Organization, org.parent_org_id)
    assert parent is not None and parent.name_canonical == "THE SOUTHERN COMPANY"
    assert org.parent_source_id == GLEIF_SOURCE_ID
    assert org.parent_as_of == dt.date(2019, 2, 8)
    assert org.ids["lei"] == "D981X4Z4RWS7PDMJUZ03"
    assert parent.ids["lei"] == "549300FC3G3YU2FBZD92"
    assert result.children_linked == 1
    assert result.by_relationship_type == {"IS_DIRECTLY_CONSOLIDATED_BY": 1}


def test_lapsed_and_inactive_relationships_never_reach_the_graph(session: Session):
    _seed_org(session, "Lapsed Power Co", jurisdiction="US-TX")
    _seed_org(session, "Former Power Co", jurisdiction="US-TX")
    result = load_gleif_parents(session, gleif_frame())
    assert result.children_linked == 0
    assert result.rows_after_status_filter == 3  # of 5 rows
    lapsed_parent = select(Organization).where(Organization.name_canonical == "LAPSED HOLDINGS LLC")
    assert session.scalar(lapsed_parent) is None


def test_country_gate_refuses_a_same_named_foreign_subsidiary(session: Session):
    """`Ameresco, Inc.` is a US operator; GLEIF's `AMERESCO LIMITED` is its GB subsidiary, and
    `org_key` cannot tell `Inc.` from `Limited`. Without the gate the parent of Ameresco, Inc.
    would be set to Ameresco, Inc. (labelled `family` in data/eval/gleif_matches.csv)."""
    _seed_asset(session, "5000", "Ameresco, Inc.")
    load_operator_edges(
        session,
        pd.DataFrame(
            [
                {
                    "source_asset_id": "5000",
                    "operator_name": "Ameresco, Inc.",
                    "owner_name": None,
                    "source_url": "https://www.eia.gov/electricity/data/eia860m/",
                    "retrieved_at": "2026-09-13T20:25:39Z",
                }
            ]
        ),
        "power_plant",
        source_id="us.eia.860m",
    )
    result = load_gleif_parents(session, gleif_frame())
    org = session.scalar(select(Organization).where(Organization.name_canonical == "Ameresco, Inc."))
    assert org is not None and org.parent_org_id is None
    assert result.rejected_country_gate == 1
    assert result.children_linked == 0


def test_two_gleif_entities_on_one_key_link_neither(session: Session):
    """`Air Products` keys to a US, a Belgian and a French entity. The country gate leaves the US
    one; where it would leave two, the loader links neither rather than guessing."""
    _seed_org(session, "Air Products", jurisdiction="US-TX")
    frame = pd.DataFrame(
        [
            _relationship(
                child_lei="549300TWUESJXW0P4Q61",
                child_legal_name="AIR PRODUCTS LLC",
                parent_legal_name="AIR PRODUCTS HELIUM, INC.",
            ),
            _relationship(
                child_lei="AAAA0000000000000009",
                child_legal_name="Air Products Inc",
                parent_legal_name="SOMEONE ELSE LLC",
            ),
        ]
    )
    result = load_gleif_parents(session, frame)
    org = session.scalar(select(Organization).where(Organization.name_canonical == "Air Products"))
    assert org is not None and org.parent_org_id is None
    assert result.rejected_ambiguous_gleif == 1


def test_rerun_changes_nothing(session: Session):
    _seed_org(session, "Georgia Power Co", jurisdiction="US-GA")
    first = load_gleif_parents(session, gleif_frame())
    orgs_after_first = len(list(session.scalars(select(Organization))))
    second = load_gleif_parents(session, gleif_frame())
    assert first.children_linked == 1
    assert second.children_linked == 0
    assert second.children_unchanged == 1
    assert second.parents_created == 0
    assert len(list(session.scalars(select(Organization)))) == orgs_after_first


def test_curated_parents_defer_to_a_gleif_link(session: Session, tmp_path):
    """The curated file is the interim source: it never overwrites a registry link, so the two
    loaders are order-independent (docs/22 §17.3)."""
    org = _seed_org(session, "Georgia Power Co", jurisdiction="US-GA")
    load_gleif_parents(session, gleif_frame())
    rules = tmp_path / "parents.yaml"
    rules.write_text(
        "parents:\n"
        "  - child_pattern: '^georgia power'\n"
        "    parent: Some Other Holdco\n"
        "    source_url: https://example.invalid/statement\n"
        "    retrieved_at: '2026-09-20T00:00:00Z'\n",
        encoding="utf-8",
    )
    result = load_parents(session, rules)
    session.refresh(org)
    assert result.children_deferred_to_gleif == 1
    assert result.children_linked == 0
    assert session.get(Organization, org.parent_org_id).name_canonical == "THE SOUTHERN COMPANY"


# ------------------------------------------------------------------------------ alias loader
def test_the_repo_alias_file_parses_and_carries_the_three_sec_rules():
    rules = read_alias_rules()
    pairs = {(r.alias, r.canonical) for r in rules}
    assert ("Vistra Energy", "Vistra Corp") in pairs
    assert ("Enable Midstream", "Enable Midstream Partners") in pairs
    assert ("Noble Environmental", "Noble Environmental Power, LLC") in pairs
    assert all(r.source_url.startswith("https://data.sec.gov/") for r in rules)


def test_alias_rule_attaches_to_the_canonical_organisation(session: Session, tmp_path):
    org = _seed_org(session, "Vistra Corp", jurisdiction="US-TX")
    rules = tmp_path / "aliases.yaml"
    rules.write_text(
        "aliases:\n"
        "  - alias: Vistra Energy\n"
        "    canonical: Vistra Corp\n"
        "    source_url: https://data.sec.gov/submissions/CIK0001692819.json\n"
        "    retrieved_at: '2026-09-19T21:10:00Z'\n",
        encoding="utf-8",
    )
    result = load_aliases(session, rules)
    assert result.aliases_written == 1
    alias = session.scalar(
        select(OrganizationAlias).where(OrganizationAlias.alias_normalised == "vistra energy")
    )
    assert alias is not None
    assert alias.organization_id == org.id
    assert alias.kind == "filing_spelling"
    assert float(alias.confidence) == 1.0
    assert alias.source_id == ALIASES_SOURCE_ID
    assert alias.source_url.endswith("CIK0001692819.json")
    # and the alias is now a resolution key for that organisation
    assert org_key_multimap(session)["VISTRA ENERGY"][0].id == org.id
    assert load_aliases(session, rules).aliases_written == 0


def test_a_rule_whose_organisation_is_not_loaded_is_inert_not_an_error(session: Session, tmp_path):
    rules = tmp_path / "aliases.yaml"
    rules.write_text(
        "aliases:\n"
        "  - alias: Kansas City Power & Light Co\n"
        "    canonical: Evergy Metro\n"
        "    source_url: https://data.sec.gov/submissions/CIK0000054476.json\n"
        "    retrieved_at: '2026-09-19T21:10:00Z'\n",
        encoding="utf-8",
    )
    result = load_aliases(session, rules)
    assert result.aliases_written == 0
    assert result.rules_without_organization == ["Kansas City Power & Light Co -> Evergy Metro"]
    assert list(session.scalars(select(Organization))) == []


def test_an_alias_that_already_names_another_organisation_is_reported_not_merged(session: Session, tmp_path):
    _seed_org(session, "Evergy Kansas Central, Inc", jurisdiction="US-KS")
    _seed_org(session, "Westar Energy Inc", jurisdiction="US-KS")
    rules = tmp_path / "aliases.yaml"
    rules.write_text(
        "aliases:\n"
        "  - alias: Westar Energy Inc\n"
        "    canonical: Evergy Kansas Central, Inc\n"
        "    source_url: https://data.sec.gov/submissions/CIK0000054507.json\n"
        "    retrieved_at: '2026-09-19T21:10:00Z'\n",
        encoding="utf-8",
    )
    result = load_aliases(session, rules)
    assert result.aliases_written == 0
    assert len(result.conflicts) == 1
    assert "services/resolve" in result.conflicts[0]
