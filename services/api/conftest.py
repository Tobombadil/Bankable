"""Shared fixtures: a seeded SQLite database wired into the FastAPI app via dependency override
(services/api/deps.get_db), per E-9's "seeded database holds >= 1 record per publish_state and
tier boundary"."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app
from services.api.deps import get_db
from services.db.models import (
    Event,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    Source,
)
from services.db.session import get_engine, get_sessionmaker, init_db

UTC = dt.UTC


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Session:
    with db_sessionmaker() as s:
        yield s


@pytest.fixture()
def client(db_sessionmaker: sessionmaker[Session]):
    def _override():
        s = db_sessionmaker()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_open_licence(session: Session, id_: str = "open-lic") -> Licence:
    lic = Licence(
        id=id_,
        name="Open Licence",
        reuse_class="open",
        allows_derived_publication=True,
        allows_raw_publication=True,
        allows_api_redistribution=True,
        allows_bulk_export=True,
        allows_commercial_use=True,
        gate_flag=False,
        evidence_url="https://example.org/terms",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    session.add(lic)
    session.flush()
    return lic


def make_attribution_licence(session: Session, id_: str = "attr-lic") -> Licence:
    lic = Licence(
        id=id_,
        name="Attribution Licence",
        reuse_class="attribution",
        attribution_required=True,
        attribution_text="Source: Test ISO",
        requires_link_back=True,
        allows_derived_publication=True,
        allows_raw_publication=True,
        allows_api_redistribution=True,
        allows_bulk_export=True,
        allows_commercial_use=False,
        gate_flag=False,
        evidence_url="https://example.org/terms2",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    session.add(lic)
    session.flush()
    return lic


def make_public_source(session: Session, licence: Licence, id_: str = "us.test.public_source") -> Source:
    src = Source(
        id=id_,
        name="Test Public Source",
        category="generation_queue",
        jurisdiction="US-TX",
        operator="Test Operator",
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


def make_org(session: Session, name: str = "Acme Power LLC") -> Organization:
    from services.ids import public_id, slugify

    org = Organization(
        public_id="",
        slug="",
        name_canonical=name,
        name_normalised=name.lower(),
        type="developer",
        country="US",
    )
    session.add(org)
    session.flush()
    org.public_id = public_id("org", org.id)
    org.slug = slugify(name)
    session.flush()
    return org


def make_location(session: Session, source: Source, licence: Licence) -> Location:
    loc = Location(
        kind="county",
        precision="county_centroid",
        county_name="Travis",
        state_code="US-TX",
        country="US",
        source_id=source.id,
        source_url=source.url,
        retrieved_at=dt.datetime(2026, 9, 10, tzinfo=UTC),
        licence_id=licence.id,
    )
    session.add(loc)
    session.flush()
    return loc


def make_visible_proposal(
    session: Session,
    source: Source,
    *,
    public_id_suffix: str = "1",
    public_at: dt.datetime | None = None,
    sponsor: Organization | None = None,
    location: Location | None = None,
    lifecycle_state: str = "filed",
) -> Proposal:
    from services.ids import public_id, slugify

    now = dt.datetime.now(UTC)
    prop = Proposal(
        public_id="",
        slug="",
        kind="storage",
        name_canonical=f"Test Storage Project {public_id_suffix}",
        jurisdiction="US-TX",
        lifecycle_state=lifecycle_state,
        capacity_mw=100.0 + int(public_id_suffix),
        technology="bess_li_ion",
        publish_state="public",
        published_at=now - dt.timedelta(days=20),
        public_at=public_at or (now - dt.timedelta(days=1)),
        min_reuse_class=source.licence.reuse_class,
        source_count=1,
        sponsor_org_id=sponsor.id if sponsor else None,
        location_id=location.id if location else None,
    )
    session.add(prop)
    session.flush()
    prop.public_id = public_id("prop", prop.id)
    prop.slug = slugify(prop.name_canonical) + f"-{public_id_suffix}"
    session.flush()
    link = ProposalSource(
        proposal_id=prop.id,
        source_id=source.id,
        source_record_id=f"Q{public_id_suffix}",
        source_url=f"{source.url}#{public_id_suffix}",
        retrieved_at=now - dt.timedelta(days=1),
        licence_id=source.licence_id,
        raw={"Queue ID": f"Q{public_id_suffix}"},
        first_seen=now - dt.timedelta(days=30),
        last_seen=now - dt.timedelta(days=1),
    )
    session.add(link)
    session.flush()
    return prop


def make_visible_opportunity(
    session: Session, source: Source, *, public_id_suffix: str = "1", status: str = "open"
) -> Opportunity:
    from services.ids import public_id, slugify

    now = dt.datetime.now(UTC)
    opp = Opportunity(
        public_id="",
        slug="",
        kind="rfp",
        title=f"Test RFP {public_id_suffix}",
        jurisdiction="US-AZ",
        technologies=["solar_pv"],
        status=status,
        due_at=now + dt.timedelta(days=60),
        publish_state="public",
        published_at=now - dt.timedelta(days=10),
        public_at=now - dt.timedelta(hours=1),
        min_reuse_class=source.licence.reuse_class,
        source_count=1,
    )
    session.add(opp)
    session.flush()
    opp.public_id = public_id("opp", opp.id)
    opp.slug = slugify(opp.title) + f"-{public_id_suffix}"
    session.flush()
    link = OpportunitySource(
        opportunity_id=opp.id,
        source_id=source.id,
        source_record_id=f"N{public_id_suffix}",
        source_url=f"{source.url}#N{public_id_suffix}",
        retrieved_at=now - dt.timedelta(hours=1),
        licence_id=source.licence_id,
        raw={"Notice ID": f"N{public_id_suffix}"},
        first_seen=now - dt.timedelta(days=5),
        last_seen=now - dt.timedelta(hours=1),
    )
    session.add(link)
    session.flush()
    return opp


def make_event(
    session: Session,
    proposal: Proposal,
    source: Source,
    *,
    event_type: str = "status_change",
    public_at: dt.datetime | None = None,
) -> Event:
    now = dt.datetime.now(UTC)
    ev = Event(
        subject_type="proposal",
        subject_id=proposal.id,
        event_type=event_type,
        observed_at=now - dt.timedelta(days=2),
        source_id=source.id,
        source_url=source.url,
        retrieved_at=now - dt.timedelta(days=2),
        licence_id=source.licence_id,
        before={"lifecycle_state": "announced"},
        after={"lifecycle_state": "filed"},
        changed_keys=["lifecycle_state"],
        public_at=public_at or (now - dt.timedelta(days=1)),
        published_at=now - dt.timedelta(days=1),
        idempotency_key=f"{source.id}:{proposal.public_id}:{event_type}:{(public_at or now).isoformat()}",
    )
    session.add(ev)
    session.flush()
    return ev
