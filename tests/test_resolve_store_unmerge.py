"""Round-trip proof for services/resolve/merge.py's unmerge (docs/21-data-model.md §6.3
invariant M1: "every merged event must contain enough state to execute this without reading any
other row"). See tests/test_resolve_store.py for canonical-choice, gate-refusal and merge-shape
tests, and for the shared `make_source`/`make_proposal`/`make_organization` helpers duplicated
here at unit-test scale (no import between the two test modules, on purpose).
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Event, Organization, OrganizationAlias, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.loader import upsert_licence_and_source
from services.resolve import merge as merge_mod

UTC = dt.UTC


@pytest.fixture()
def session() -> Session:
    import services.resolve.models  # noqa: F401 -- register resolution_decision on Base.metadata

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def make_source(session: Session, id_: str = "src.unmerge") -> Source:
    entry = SourceEntry.from_yaml(
        {
            "id": id_,
            "name": f"Test {id_}",
            "jurisdiction": "US-TX",
            "category": "generation_queue",
            "operator": "Test ISO",
            "url": f"https://example.org/{id_}",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "weekly",
            "tier": 1,
            "license": f"licence for {id_}",
        }
    )
    return upsert_licence_and_source(session, entry, "test-manifest")


def make_proposal(
    session: Session,
    source: Source,
    *,
    source_record_id: str,
    name: str = "Test Project",
    sponsor: Organization | None = None,
) -> tuple[Proposal, ProposalSource]:
    proposal = Proposal(
        public_id="",
        slug="",
        kind="storage",
        name_canonical=name,
        capacity_mw=100.0,
        jurisdiction="US-TX",
        lifecycle_state="filed",
        identifiers={},
        publish_state="public",
        min_reuse_class=source.licence.reuse_class,
        source_count=1,
        sponsor_org_id=sponsor.id if sponsor else None,
    )
    session.add(proposal)
    session.flush()
    proposal.public_id = public_id("prop", proposal.id)
    proposal.slug = f"{slugify(name)}-{proposal.public_id[-6:].lower()}"
    session.flush()

    retrieved_at = dt.datetime(2026, 1, 1, tzinfo=UTC)
    link = ProposalSource(
        proposal_id=proposal.id,
        source_id=source.id,
        source_record_id=source_record_id,
        source_url=f"{source.url}/{source_record_id}",
        retrieved_at=retrieved_at,
        licence_id=source.licence_id,
        raw={},
        normalised={},
        first_seen=retrieved_at,
        last_seen=retrieved_at,
    )
    session.add(link)
    session.flush()
    return proposal, link


def make_organization(session: Session, name: str) -> Organization:
    org = Organization(
        public_id="", slug="", name_canonical=name, name_normalised=name.lower(), type="other", country="US"
    )
    session.add(org)
    session.flush()
    org.public_id = public_id("org", org.id)
    org.slug = f"{slugify(name)}-{org.public_id[-6:].lower()}"
    session.flush()
    return org


# --------------------------------------------------------------------------------- proposal unmerge
def test_unmerge_proposal_round_trip(session: Session) -> None:
    src = make_source(session)
    canonical, _canonical_link = make_proposal(session, src, source_record_id="Q1", name="Canonical")
    absorbed, absorbed_link = make_proposal(session, src, source_record_id="Q2", name="Absorbed")

    # snapshot everything about `absorbed` before the merge so the round trip can be checked
    # field-by-field, not just "some row exists again"
    before_snapshot = merge_mod.serialize_row(absorbed)
    absorbed_id = absorbed.id
    absorbed_link_id = absorbed_link.id

    merge_event = merge_mod.merge_proposal(
        session, canonical=canonical, absorbed=absorbed, score=90.0, rationale="round trip test"
    )
    session.flush()

    # sanity: the merge actually changed things
    assert absorbed.merged_into_id == canonical.id
    assert absorbed.publish_state == "unpublished"
    assert canonical.source_count == 2
    session.refresh(absorbed_link)
    assert absorbed_link.proposal_id == canonical.id

    unmerge_event = merge_mod.unmerge_proposal(session, merge_event.id)

    assert unmerge_event.event_type == "unmerged"
    assert unmerge_event.reverses_event_id == merge_event.id
    assert unmerge_event.subject_id == canonical.id

    # exact restoration -- every field of the absorbed row, not just merged_into_id
    restored = session.get(Proposal, absorbed_id)
    assert restored is not None
    after_snapshot = merge_mod.serialize_row(restored)
    assert after_snapshot == before_snapshot

    # proposal_source moved back
    restored_link = session.get(ProposalSource, absorbed_link_id)
    assert restored_link is not None
    assert restored_link.proposal_id == absorbed_id

    # canonical's changed fields restored too
    assert canonical.source_count == 1
    assert canonical.resolution_confidence is None

    # nothing was deleted at any point
    assert session.get(Proposal, absorbed_id) is not None
    assert session.get(Event, merge_event.id) is not None  # merge event itself survives


def test_unmerge_proposal_is_idempotent(session: Session) -> None:
    src = make_source(session)
    canonical, _ = make_proposal(session, src, source_record_id="Q1")
    absorbed, _ = make_proposal(session, src, source_record_id="Q2")
    merge_event = merge_mod.merge_proposal(
        session, canonical=canonical, absorbed=absorbed, score=90.0, rationale="x"
    )

    first = merge_mod.unmerge_proposal(session, merge_event.id)
    second = merge_mod.unmerge_proposal(session, merge_event.id)
    assert first.id == second.id

    all_unmerges = session.scalars(
        select(Event).where(Event.event_type == "unmerged", Event.reverses_event_id == merge_event.id)
    ).all()
    assert len(all_unmerges) == 1


def test_unmerge_proposal_rejects_wrong_event_type(session: Session) -> None:
    src = make_source(session)
    proposal, _ = make_proposal(session, src, source_record_id="Q1")
    not_a_merge = Event(
        subject_type="proposal",
        subject_id=proposal.id,
        event_type="status_change",
        observed_at=dt.datetime.now(UTC),
        idempotency_key="not-a-merge-event",
    )
    session.add(not_a_merge)
    session.flush()

    with pytest.raises(ValueError, match="not a proposal"):
        merge_mod.unmerge_proposal(session, not_a_merge.id)


def test_unmerge_after_two_merges_restores_only_the_targeted_absorbed_record(session: Session) -> None:
    """Two separate proposals merged into the same canonical; unmerging one leaves the other
    (and the canonical's now-combined state from that other merge) alone."""
    src = make_source(session)
    canonical, _ = make_proposal(session, src, source_record_id="Q1")
    absorbed_a, link_a = make_proposal(session, src, source_record_id="Q2")
    absorbed_b, link_b = make_proposal(session, src, source_record_id="Q3")

    ev_a = merge_mod.merge_proposal(
        session, canonical=canonical, absorbed=absorbed_a, score=90.0, rationale="a"
    )
    merge_mod.merge_proposal(session, canonical=canonical, absorbed=absorbed_b, score=85.0, rationale="b")
    assert canonical.source_count == 3

    merge_mod.unmerge_proposal(session, ev_a.id)

    session.refresh(absorbed_a)
    session.refresh(absorbed_b)
    assert absorbed_a.merged_into_id is None
    assert absorbed_b.merged_into_id == canonical.id  # untouched by the first unmerge
    session.refresh(link_a)
    session.refresh(link_b)
    assert link_a.proposal_id == absorbed_a.id
    assert link_b.proposal_id == canonical.id


# ----------------------------------------------------------------------------- organization unmerge
def test_unmerge_organization_round_trip(session: Session) -> None:
    src = make_source(session)
    canonical = make_organization(session, "Acme Power LLC")
    absorbed = make_organization(session, "ACME POWER, LLC")
    proposal, _ = make_proposal(session, src, source_record_id="Q1", sponsor=absorbed)

    before_snapshot = merge_mod.serialize_row(absorbed)
    absorbed_id = absorbed.id

    merge_event = merge_mod.merge_organization(
        session, canonical=canonical, absorbed=absorbed, rationale="round trip"
    )
    session.refresh(proposal)
    assert proposal.sponsor_org_id == canonical.id

    unmerge_event = merge_mod.unmerge_organization(session, merge_event.id)
    assert unmerge_event.reverses_event_id == merge_event.id

    restored = session.get(Organization, absorbed_id)
    assert restored is not None
    assert merge_mod.serialize_row(restored) == before_snapshot

    session.refresh(proposal)
    assert proposal.sponsor_org_id == absorbed_id  # sponsor re-pointed back

    # the alias row written at merge time is not deleted by unmerge (docs/21 §6.1: nothing is
    # deleted; an alias recorded once stays as a spelling this organization is known to use)
    alias = session.scalar(select(OrganizationAlias).where(OrganizationAlias.organization_id == canonical.id))
    assert alias is not None
