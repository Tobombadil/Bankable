"""Loader re-runs after an organisation merge resolve to the survivor (docs/22 §20.7).

Each test merges an organisation A (created by a loader) into B, re-runs the load that created A,
and asserts that nothing new attaches to A and no second copy of A is created. Before
`services/ingest/org_redirects.py`, every one of them failed: the sponsor loader's exact-spelling
index held the redirect (new proposals were sponsored by A) and its punctuation index skipped
redirects (a punctuation variant created a new organisation); `ownership._build_norm_org_index`,
which the ownership, midstream, GHGRP and ethanol loaders share, held the redirect (new edges on A);
`organizations.org_key_multimap` skipped redirects (a GLEIF parent or alias rule naming A's spelling
created a duplicate when no alias carried it).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.db.models import AssetOwner, Organization, OrganizationAlias, Proposal
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.assets import load_assets
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.ingest.midstream import load_operator_edges
from services.ingest.org_redirects import OrgRedirects
from services.ingest.organizations import org_key_multimap
from services.ingest.ownership import _build_norm_org_index, _resolve_organization, load_owner_shares
from services.ingest.test_loader import open_source_entry, sample_proposal_row
from services.ingest.test_midstream import processing_frame
from services.ingest.test_ownership import _seed_plant, owner_frame
from services.resolve.merge import merge_organization

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _org(session: Session, name: str) -> Organization:
    org = Organization(
        public_id="",
        slug=slugify(name),
        name_canonical=name,
        name_normalised=name.lower(),
        type="other",
        country="US",
    )
    session.add(org)
    session.flush()
    org.public_id = public_id("org", org.id)
    session.flush()
    return org


def _named(session: Session, name: str) -> Organization:
    org = session.scalar(select(Organization).where(Organization.name_canonical == name))
    assert org is not None, name
    return org


def _merge(session: Session, absorbed: Organization, survivor: Organization) -> None:
    merge_organization(session, canonical=survivor, absorbed=absorbed, rationale="test merge")
    session.commit()


def _org_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(Organization)) or 0)


# ---------------------------------------------------------------------------------- the helper
def test_terminal_follows_a_chain_and_stops_on_a_cycle(session: Session) -> None:
    a, b, c = _org(session, "A Co"), _org(session, "B Co"), _org(session, "C Co")
    a.merged_into_id, b.merged_into_id = b.id, c.id
    session.flush()
    redirects = OrgRedirects(session)
    assert redirects.terminal(a) is c and redirects.terminal(b) is c and redirects.terminal(c) is c
    c.merged_into_id = a.id  # a cycle no merge writes; the lookup must still return
    session.flush()
    assert OrgRedirects(session).terminal(a) is a


# ------------------------------------------------------------------------------ sponsor loader
def test_sponsor_loader_rerun_attaches_new_proposals_to_the_survivor(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "2026-09-12")
    first = pd.DataFrame(
        [
            {**sample_proposal_row("Q1"), "sponsor_name": "Absorbed Solar LLC"},
            {**sample_proposal_row("Q2"), "sponsor_name": "Survivor Solar Holdings"},
        ]
    )
    load_dataframe(session, src, "proposal", first, None)
    session.commit()
    absorbed, survivor = _named(session, "Absorbed Solar LLC"), _named(session, "Survivor Solar Holdings")
    _merge(session, absorbed, survivor)
    before = _org_count(session)

    rerun = pd.DataFrame(
        [
            *first.to_dict("records"),
            {**sample_proposal_row("Q3"), "sponsor_name": "Absorbed Solar LLC"},  # exact spelling
            {**sample_proposal_row("Q4"), "sponsor_name": "ABSORBED SOLAR, LLC"},  # punctuation variant
        ]
    )
    load_dataframe(session, src, "proposal", rerun, None)
    session.commit()

    assert _org_count(session) == before, "a second copy of the absorbed organisation was created"
    on_absorbed = session.scalars(select(Proposal).where(Proposal.sponsor_org_id == absorbed.id)).all()
    assert on_absorbed == []
    new = session.scalars(select(Proposal).where(Proposal.name_canonical == "Test Storage Project")).all()
    assert {p.sponsor_org_id for p in new} == {survivor.id}


def test_sponsor_loader_follows_a_merge_chain(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "2026-09-12")
    load_dataframe(
        session,
        src,
        "proposal",
        pd.DataFrame([{**sample_proposal_row("Q1"), "sponsor_name": "First Name Co"}]),
        None,
    )
    session.commit()
    first = _named(session, "First Name Co")
    middle, last = _org(session, "Middle Name Co"), _org(session, "Last Name Co")
    session.commit()
    _merge(session, first, middle)
    _merge(session, middle, last)
    load_dataframe(
        session,
        src,
        "proposal",
        pd.DataFrame([{**sample_proposal_row("Q9"), "sponsor_name": "First Name Co"}]),
        None,
    )
    session.commit()
    # Q1 was moved to the end of the chain by the two merges; Q9 is new and must land there too.
    assert set(session.scalars(select(Proposal.sponsor_org_id))) == {last.id}


# ---------------------------------------------------- ownership (shared by midstream/GHGRP/ethanol)
def test_ownership_rerun_writes_no_edge_or_alias_onto_the_absorbed_row(session: Session) -> None:
    _seed_plant(session)
    load_owner_shares(session, owner_frame())
    absorbed = _named(session, "Minority Partner Co")
    survivor = _org(session, "Minority Partner Holdings")
    session.commit()
    _merge(session, absorbed, survivor)
    before = _org_count(session)
    edges_before = int(session.scalar(select(func.count()).select_from(AssetOwner)) or 0)

    load_owner_shares(session, owner_frame())

    assert _org_count(session) == before
    assert session.scalars(select(AssetOwner).where(AssetOwner.organization_id == absorbed.id)).all() == []
    assert (
        session.scalars(
            select(OrganizationAlias).where(OrganizationAlias.organization_id == absorbed.id)
        ).all()
        == []
    )
    assert int(session.scalar(select(func.count()).select_from(AssetOwner)) or 0) == edges_before
    assert {e.organization_id for e in session.scalars(select(AssetOwner))} >= {survivor.id}


def test_the_shared_owner_index_resolves_redirects_and_prefers_live_rows(session: Session) -> None:
    absorbed, survivor = _org(session, "Redirected Gas LLC"), _org(session, "Surviving Gas Inc")
    session.commit()
    _merge(session, absorbed, survivor)
    index = _build_norm_org_index(session)
    assert all(org.merged_into_id is None for org in index.values())
    resolved = _resolve_organization(
        session,
        index,
        "REDIRECTED GAS, L.L.C.",
        source=upsert_licence_and_source(session, open_source_entry(), "2026-09-12"),
        now=dt.datetime(2026, 9, 27, tzinfo=dt.UTC),
        created_counter=[0],
    )
    assert resolved.id == survivor.id


def test_midstream_operator_edges_rerun_lands_on_the_survivor(session: Session) -> None:
    load_assets(session, processing_frame(), "gas_processing_plant")
    load_operator_edges(session, processing_frame(), "gas_processing_plant")
    absorbed = _named(session, "Tallgrass Energy Midstream LLC")
    survivor = _org(session, "Tallgrass Midstream Holdings")
    session.commit()
    _merge(session, absorbed, survivor)
    before = _org_count(session)

    again = load_operator_edges(session, processing_frame(), "gas_processing_plant")

    assert again.organizations_created == 0 and _org_count(session) == before
    assert session.scalars(select(AssetOwner).where(AssetOwner.organization_id == absorbed.id)).all() == []
    assert {e.organization_id for e in session.scalars(select(AssetOwner))} == {survivor.id}


# --------------------------------------------------------- the GLEIF/alias index (organizations.py)
def test_org_key_multimap_resolves_an_absorbed_spelling_to_the_survivor(session: Session) -> None:
    absorbed, survivor = _org(session, "Truncated Pipeline Transmissio"), _org(session, "Unrelated Name Corp")
    session.commit()
    _merge(session, absorbed, survivor)
    # A merge with no edge and no sponsored proposal writes no spelling alias (no provenance for one),
    # so the absorbed spelling is reachable only through the redirect itself.
    assert session.scalars(select(OrganizationAlias)).all() == []
    index = org_key_multimap(session)
    from pipeline.normalize import org_key

    assert [o.id for o in index[org_key("Truncated Pipeline Transmissio")]] == [survivor.id]
    assert all(o.merged_into_id is None for orgs in index.values() for o in orgs)
