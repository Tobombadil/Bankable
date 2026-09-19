"""Model tests: table creation, mandatory provenance/licence columns, CHECK vocabularies,
uniqueness constraints (docs/04 E-7: 100% branch coverage on the gate modules is the API layer's
job — services/api/test_visibility.py — this file proves the schema itself holds the line).
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.db.models import (
    Event,
    Licence,
    Location,
    Match,
    Opportunity,
    OpportunitySource,
    Organization,
    OrganizationAlias,
    Proposal,
    ProposalSource,
    Snapshot,
    Source,
    SourceRun,
)
from services.db.session import get_engine, get_sessionmaker, init_db

UTC = dt.UTC


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as s:
        yield s


def make_licence(
    session: Session, id_: str = "test-open", reuse_class: str = "open", **kw: object
) -> Licence:
    lic = Licence(
        id=id_,
        name="Test Licence",
        reuse_class=reuse_class,
        attribution_required=False,
        requires_link_back=False,
        allows_derived_publication=True,
        allows_raw_publication=True,
        allows_api_redistribution=True,
        allows_bulk_export=True,
        allows_commercial_use=True,
        share_alike=False,
        gate_flag=False,
        evidence_url="https://example.org/terms",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
        **kw,
    )
    session.add(lic)
    session.flush()
    return lic


def make_source(session: Session, licence: Licence, id_: str = "us.test.source") -> Source:
    src = Source(
        id=id_,
        name="Test Source",
        category="generation_queue",
        url="https://example.org/queue",
        access="bulk_file",
        cadence="weekly",
        licence_id=licence.id,
        publish_state="public",
        manifest_version="2026-09-12",
        manifest_hash="0" * 64,
    )
    session.add(src)
    session.flush()
    return src


def test_licence_gate_clear_invariant(session: Session) -> None:
    lic = make_licence(session)
    assert lic.gate_clear is True
    lic.gate_flag = True
    assert lic.gate_clear is False


def test_licence_reuse_class_check_constraint(session: Session) -> None:
    bad = Licence(id="bad", name="x", reuse_class="not-a-class")
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()


def test_source_requires_licence_fk(session: Session) -> None:
    src = Source(
        id="us.test.orphan",
        name="Orphan",
        category="generation_queue",
        url="https://example.org",
        access="bulk_file",
        cadence="weekly",
        licence_id="does-not-exist",
    )
    session.add(src)
    with pytest.raises(IntegrityError):
        session.flush()


def test_source_gated_mirrors_licence_reuse_class(session: Session) -> None:
    open_lic = make_licence(session, id_="open-lic", reuse_class="open")
    restricted_lic = make_licence(session, id_="restricted-lic", reuse_class="restricted")
    open_src = make_source(session, open_lic, "us.test.open")
    restricted_src = make_source(session, restricted_lic, "us.test.restricted")
    assert open_src.gated is False
    assert restricted_src.gated is True


def test_proposal_requires_provenance_quartet_on_source_row(session: Session) -> None:
    lic = make_licence(session)
    src = make_source(session, lic)
    prop = Proposal(
        public_id="prop_01TESTPROPOSAL",
        slug="test-proposal",
        kind="storage",
        name_canonical="Test Storage Project",
        jurisdiction="US-TX",
        lifecycle_state="filed",
        publish_state="public",
        min_reuse_class="open",
    )
    session.add(prop)
    session.flush()
    # A proposal_source row missing any of the provenance quartet must fail — retrieved_at
    # omitted here.
    row = ProposalSource(
        proposal_id=prop.id,
        source_id=src.id,
        source_record_id="Q1",
        source_url="https://example.org/q1",
        licence_id=lic.id,
    )
    session.add(row)
    with pytest.raises(IntegrityError):
        session.flush()


def test_proposal_source_active_uniqueness(session: Session) -> None:
    lic = make_licence(session)
    src = make_source(session, lic)
    prop = Proposal(
        public_id="prop_01TESTPROPOSAL2",
        slug="test-proposal-2",
        kind="storage",
        name_canonical="Test Storage Project 2",
        jurisdiction="US-TX",
        lifecycle_state="filed",
        publish_state="public",
        min_reuse_class="open",
    )
    session.add(prop)
    session.flush()
    now = dt.datetime.now(UTC)
    row_kwargs = {
        "proposal_id": prop.id,
        "source_id": src.id,
        "source_record_id": "Q1",
        "source_url": "https://example.org/q1",
        "retrieved_at": now,
        "licence_id": lic.id,
        "first_seen": now,
        "last_seen": now,
    }
    session.add(ProposalSource(**row_kwargs))
    session.flush()
    # A second *active* row for the same (source_id, source_record_id) violates the partial
    # unique index (docs/21 §5.1); an *inactive* duplicate (superseded link) is fine. A SAVEPOINT
    # scopes the expected failure so the outer transaction (proposal, source, first row) survives
    # to be reused below.
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(ProposalSource(**row_kwargs))
        session.flush()
    session.add(ProposalSource(**{**row_kwargs, "active": False}))
    session.flush()  # no error: only one row is active


def test_event_idempotency_key_unique(session: Session) -> None:
    lic = make_licence(session)
    src = make_source(session, lic)
    now = dt.datetime.now(UTC)
    prop = Proposal(
        public_id="prop_01TESTEVT",
        slug="test-event-proposal",
        kind="storage",
        name_canonical="Event Test",
        jurisdiction="US-TX",
        lifecycle_state="filed",
        publish_state="public",
        min_reuse_class="open",
    )
    session.add(prop)
    session.flush()
    kwargs = {
        "subject_type": "proposal",
        "subject_id": prop.id,
        "event_type": "status_change",
        "observed_at": now,
        "source_id": src.id,
        "source_url": "https://example.org",
        "retrieved_at": now,
        "licence_id": lic.id,
        "idempotency_key": "us.test.source:Q1:status_change:abc123",
    }
    session.add(Event(**kwargs))
    session.flush()
    session.add(Event(**kwargs))
    with pytest.raises(IntegrityError):
        session.flush()


def test_event_seq_autoincrements(session: Session) -> None:
    lic = make_licence(session)
    src = make_source(session, lic)
    now = dt.datetime.now(UTC)
    prop = Proposal(
        public_id="prop_01TESTSEQ",
        slug="test-seq-proposal",
        kind="storage",
        name_canonical="Seq Test",
        jurisdiction="US-TX",
        lifecycle_state="filed",
        publish_state="public",
        min_reuse_class="open",
    )
    session.add(prop)
    session.flush()
    e1 = Event(
        subject_type="proposal",
        subject_id=prop.id,
        event_type="created",
        observed_at=now,
        source_id=src.id,
        source_url="https://example.org",
        retrieved_at=now,
        licence_id=lic.id,
        idempotency_key="k1",
    )
    e2 = Event(
        subject_type="proposal",
        subject_id=prop.id,
        event_type="status_change",
        observed_at=now,
        source_id=src.id,
        source_url="https://example.org",
        retrieved_at=now,
        licence_id=lic.id,
        idempotency_key="k2",
    )
    # Flushed one at a time: the `seq` listener (services/db/models.py) reads MAX(seq) from the
    # transaction so far, which only reflects previously *inserted* rows, not merely pending
    # ones batched into the same statement.
    session.add(e1)
    session.flush()
    session.add(e2)
    session.flush()
    assert e2.seq > e1.seq


def test_opportunity_and_opportunity_source(session: Session) -> None:
    lic = make_licence(session)
    src = make_source(session, lic, "us.test.opp")
    now = dt.datetime.now(UTC)
    opp = Opportunity(
        public_id="opp_01TESTOPP",
        slug="test-opportunity",
        kind="rfp",
        title="Test RFP",
        jurisdiction="US-AZ",
        technologies=["solar_pv", "bess"],
        status="open",
        publish_state="public",
        min_reuse_class="open",
    )
    session.add(opp)
    session.flush()
    session.add(
        OpportunitySource(
            opportunity_id=opp.id,
            source_id=src.id,
            source_record_id="N1",
            source_url="https://example.org/n1",
            retrieved_at=now,
            licence_id=lic.id,
            first_seen=now,
            last_seen=now,
        )
    )
    session.flush()
    fetched = session.scalar(sa.select(Opportunity).where(Opportunity.public_id == "opp_01TESTOPP"))
    assert fetched is not None
    assert fetched.technologies == ["solar_pv", "bess"]


def test_organization_alias_and_location(session: Session) -> None:
    lic = make_licence(session)
    src = make_source(session, lic, "us.test.org")
    now = dt.datetime.now(UTC)
    org = Organization(
        public_id="org_01TESTORG",
        slug="test-org",
        name_canonical="Test Developer LLC",
        name_normalised="test developer",
        type="developer",
        country="US",
    )
    session.add(org)
    session.flush()
    session.add(
        OrganizationAlias(
            organization_id=org.id,
            alias="Test Developer, LLC",
            alias_normalised="test developer llc",
            kind="filing_spelling",
            source_id=src.id,
            source_url="https://example.org",
            retrieved_at=now,
            licence_id=lic.id,
        )
    )
    loc = Location(
        kind="county",
        precision="county_centroid",
        county_fips="48201",
        county_name="Harris",
        state_code="US-TX",
        country="US",
        source_id=src.id,
        source_url="https://example.org",
        retrieved_at=now,
        licence_id=lic.id,
    )
    session.add(loc)
    session.flush()
    assert loc.geom is None


def test_match_active_pair_uniqueness(session: Session) -> None:
    prop = Proposal(
        public_id="prop_01TESTMATCH",
        slug="test-match-proposal",
        kind="storage",
        name_canonical="Match Test",
        jurisdiction="US-TX",
        lifecycle_state="filed",
        publish_state="public",
        min_reuse_class="open",
    )
    opp = Opportunity(
        public_id="opp_01TESTMATCH",
        slug="test-match-opportunity",
        kind="rfp",
        title="Match RFP",
        jurisdiction="US-TX",
        technologies=[],
        status="open",
        publish_state="public",
        min_reuse_class="open",
    )
    session.add_all([prop, opp])
    session.flush()
    kwargs = {
        "proposal_id": prop.id,
        "opportunity_id": opp.id,
        "score": 0.8,
        "rationale": {"rules_passed": ["technology"], "rules_failed": []},
        "rationale_text": "storage, TX",
        "rule_set_version": "match-rules@v1",
    }
    session.add(Match(**kwargs))
    session.flush()
    session.add(Match(**kwargs))
    with pytest.raises(IntegrityError):
        session.flush()


def test_source_run_and_snapshot(session: Session) -> None:
    lic = make_licence(session)
    src = make_source(session, lic, "us.test.run")
    now = dt.datetime.now(UTC)
    run = SourceRun(source_id=src.id, started_at=now, status="ok")
    session.add(run)
    session.flush()
    snap = Snapshot(
        source_id=src.id,
        source_run_id=run.id,
        object_key="raw/us.test.run/2026/09/12/deadbeef.xlsx",
        sha256="a" * 64,
        byte_size=100,
        content_type="application/vnd.ms-excel",
        fetched_url="https://example.org",
        http_status=200,
        retrieved_at=now,
        licence_id=lic.id,
    )
    session.add(snap)
    session.flush()
    assert snap.id is not None


def test_asset_unique_per_source_record(session: Session) -> None:
    from services.db.models import Asset

    lic = make_licence(session)
    src = make_source(session, lic)
    now = dt.datetime.now(UTC)

    def plant(source_asset_id: str) -> Asset:
        return Asset(
            public_id=f"asset_{source_asset_id}",
            slug=f"test-plant-{source_asset_id}",
            asset_type="power_plant",
            source_asset_id=source_asset_id,
            name="Test Plant",
            status="operating",
            technology="solar",
            technologies={"Solar Photovoltaic": 12.5},
            capacity_mw=12.5,
            unit_count=1,
            geom=(-101.5, 33.2),
            attributes={},
            state_code="US-TX",
            country="US",
            source_id=src.id,
            source_url="https://example.org/860m",
            retrieved_at=now,
            licence_id=lic.id,
        )

    session.add(plant("1001"))
    session.commit()
    row = session.scalar(sa.select(Asset))
    assert row is not None
    assert row.geom == (-101.5, 33.2)
    assert row.technologies == {"Solar Photovoltaic": 12.5}
    session.add(plant("1001"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_asset_type_and_status_vocab_enforced(session: Session) -> None:
    from services.db.models import Asset

    lic = make_licence(session)
    src = make_source(session, lic)
    now = dt.datetime.now(UTC)
    session.add(
        Asset(
            public_id="asset_bad",
            slug="asset-bad",
            asset_type="not_a_real_type",
            source_asset_id="X1",
            name="Bad Asset",
            status="operating",
            country="US",
            source_id=src.id,
            source_url="https://example.org",
            retrieved_at=now,
            licence_id=lic.id,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_asset_owner_edge_and_role_vocab(session: Session) -> None:
    from services.db.models import Asset, AssetOwner

    lic = make_licence(session)
    src = make_source(session, lic)
    now = dt.datetime.now(UTC)
    asset = Asset(
        public_id="asset_owned",
        slug="asset-owned",
        asset_type="power_plant",
        source_asset_id="X2",
        name="Owned Plant",
        status="operating",
        country="US",
        source_id=src.id,
        source_url="https://example.org",
        retrieved_at=now,
        licence_id=lic.id,
    )
    session.add(asset)
    session.flush()
    org = Organization(
        public_id="org_owner1",
        slug="owner-one",
        name_canonical="Owner One LLC",
        name_normalised="owner one llc",
        type="developer",
        country="US",
    )
    session.add(org)
    session.flush()
    session.add(
        AssetOwner(
            asset_id=asset.id,
            organization_id=org.id,
            role="owner",
            share_pct=50.0,
            owner_name_raw="Owner One LLC",
            source_id=src.id,
            source_url="https://example.org",
            retrieved_at=now,
            licence_id=lic.id,
        )
    )
    session.commit()
    edge = session.scalar(sa.select(AssetOwner))
    assert edge is not None
    assert edge.asset.name == "Owned Plant"
    assert edge.organization.name_canonical == "Owner One LLC"

    session.add(
        AssetOwner(
            asset_id=asset.id,
            organization_id=org.id,
            role="not_a_role",
            owner_name_raw="Owner One LLC",
            source_id=src.id,
            source_url="https://example.org",
            retrieved_at=now,
            licence_id=lic.id,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_organization_parent_org_id(session: Session) -> None:
    parent = Organization(
        public_id="org_parent1",
        slug="parent-one",
        name_canonical="Parent Holdco",
        name_normalised="parent holdco",
        type="developer",
        country="US",
    )
    session.add(parent)
    session.flush()
    child = Organization(
        public_id="org_child1",
        slug="child-one",
        name_canonical="Child Sub LLC",
        name_normalised="child sub llc",
        type="developer",
        country="US",
        parent_org_id=parent.id,
        ids={"lei": "5493001KJTIIGC8Y1R12"},
    )
    session.add(child)
    session.commit()
    row = session.scalar(sa.select(Organization).where(Organization.public_id == "org_child1"))
    assert row is not None
    assert row.parent_org_id == parent.id
    assert row.ids["lei"] == "5493001KJTIIGC8Y1R12"


def test_ui_event_name_vocab_and_no_identifier_columns(session: Session) -> None:
    from services.db.models import UI_EVENT_NAMES, UiEvent

    session.add(UiEvent(name="map.layer_toggled", props={"layer": "plants", "on": True}))
    session.commit()
    row = session.scalar(sa.select(UiEvent))
    assert row is not None
    assert row.id == 1
    assert row.props == {"layer": "plants", "on": True}
    # The table has no column that could carry an identifier: this is the privacy invariant
    # docs/21 §3.21 relies on, so a future column named like one fails loudly here.
    columns = {c.name for c in UiEvent.__table__.columns}
    assert columns == {"id", "name", "props", "occurred_at"}
    assert "auth.registered" in UI_EVENT_NAMES
    session.add(UiEvent(name="not.allowed", props={}))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
