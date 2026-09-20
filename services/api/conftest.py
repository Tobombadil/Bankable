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
from services.api.ratelimit import default_limiter
from services.db.models import (
    Asset,
    AssetOwner,
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


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """`services.api.ratelimit.default_limiter` is one process-wide instance standing in for a
    shared store (its module docstring); reset it so this file's tests never inherit a
    part-filled bucket from `tests/test_api_pro_ratelimit.py` (or each other) when the whole
    suite runs in one pytest process (`tests/conftest.py` carries the identical fixture for the
    same reason — the two directories do not share fixtures, so both need it)."""
    default_limiter.reset()


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
        # Mirrors services/api/deps.py's `get_db` (commit on a clean return, rollback on
        # exception) now that services/api/pro.py adds write endpoints — see that module's
        # docstring for why a plain `finally: s.close()` would silently discard writes.
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
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


def make_asset(
    session: Session,
    source: Source,
    licence: Licence,
    *,
    source_asset_id: str = "1",
    asset_type: str = "power_plant",
    name: str = "Test Plant",
    status: str = "operating",
    technology: str | None = "wind",
    capacity_mw: float | None = 100.0,
    geom: tuple[float, float] | None = (-100.0, 32.0),
    state_code: str | None = "US-TX",
    county_name: str | None = "Nolan",
    country: str = "US",
) -> Asset:
    from services.ids import public_id, slugify

    asset = Asset(
        public_id="",
        slug="",
        asset_type=asset_type,
        source_asset_id=source_asset_id,
        name=name,
        status=status,
        technology=technology,
        technologies={technology: capacity_mw} if technology and capacity_mw else {},
        capacity_mw=capacity_mw,
        unit_count=1,
        geom=geom,
        attributes={},
        state_code=state_code,
        county_name=county_name,
        country=country,
        source_id=source.id,
        source_url=source.url,
        retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        licence_id=licence.id,
    )
    session.add(asset)
    session.flush()
    asset.public_id = public_id("asset", asset.id)
    asset.slug = slugify(f"{name} {state_code}" if state_code else name) + f"-{source_asset_id}"
    session.flush()
    return asset


def make_asset_owner(
    session: Session,
    asset: Asset,
    organization: Organization,
    source: Source,
    licence: Licence,
    *,
    role: str = "owner",
    share_pct: float | None = 100.0,
) -> AssetOwner:
    edge = AssetOwner(
        asset_id=asset.id,
        organization_id=organization.id,
        role=role,
        share_pct=share_pct,
        owner_name_raw=organization.name_canonical,
        source_id=source.id,
        source_url=source.url,
        retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        licence_id=licence.id,
    )
    session.add(edge)
    session.flush()
    return edge


def make_location(
    session: Session,
    source: Source,
    licence: Licence,
    *,
    geom: tuple[float, float] | None = None,
    precision: str = "county_centroid",
    precision_reason: str | None = None,
    county_name: str | None = "Travis",
    county_fips: str | None = "48453",
    state_code: str | None = "US-TX",
    country: str = "US",
) -> Location:
    """`county_fips` (ADR 0008, docs/21 §3.7) defaults to Travis County, TX's real FIPS (48453),
    matching the default `county_name`/`state_code` -- a caller passing a different `county_name`
    without also passing a matching `county_fips` gets a syntactically valid but semantically
    mismatched code, which is harmless for tests exercising region *grouping*/*counting* (they
    never assert the fips-to-name correspondence itself) but wrong for tests of
    `GET /v1/geo/regions` or of `region_id` values specifically, which must pass both explicitly.
    `GET /v1/proposals/geo`'s region-grade grouping (`services/api/geo.py::_region_id_for`) drops
    a `county_centroid` location with no `county_fips` entirely (it cannot key a region group),
    which is why this default exists rather than leaving the field null as before ADR 0008."""
    loc = Location(
        kind="county" if county_name else "state",
        geom=geom,
        precision=precision,
        precision_reason=precision_reason,
        county_name=county_name,
        county_fips=county_fips if precision == "county_centroid" else None,
        state_code=state_code,
        country=country,
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
    technology: str = "bess_li_ion",
    jurisdiction: str = "US-TX",
) -> Proposal:
    from services.ids import public_id, slugify

    now = dt.datetime.now(UTC)
    prop = Proposal(
        public_id="",
        slug="",
        kind="storage",
        name_canonical=f"Test Storage Project {public_id_suffix}",
        jurisdiction=jurisdiction,
        lifecycle_state=lifecycle_state,
        capacity_mw=100.0 + int(public_id_suffix),
        technology=technology,
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
