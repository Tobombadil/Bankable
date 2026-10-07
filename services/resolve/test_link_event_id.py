"""Every link a proposal merge re-points names that merge, and unmerge moves back exactly those links
(docs/21 §6.3; docs/22 §13.7).

Before 2026-10-07 `merge_proposal` left `proposal_source.link_event_id` null on the links it moved,
and `unmerge_proposal` moved back every id the merge event listed, wherever it was: after C into B,
B into A and C out again, unmerging B also took C's links. Each test below fails on that code.

Fixtures are local rather than imported from `tests/`, per this repo's convention that test modules
do not import one another.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Event, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.loader import upsert_licence_and_source
from services.resolve import merge as merge_mod

T0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


@pytest.fixture()
def session() -> Iterator[Session]:
    import services.resolve.models  # noqa: F401 -- register resolution_decision on Base.metadata

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def make_source(session: Session, id_: str) -> Source:
    entry = SourceEntry.from_yaml(
        {
            "id": id_,
            "name": f"Test {id_}",
            "jurisdiction": "US",
            "category": "generation_queue",
            "operator": "Test operator",
            "url": f"https://example.org/{id_}",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "weekly",
            "tier": 1,
            "license": f"licence for {id_}",
        }
    )
    return upsert_licence_and_source(session, entry, "test-manifest")


def add_link(session: Session, proposal: Proposal, source: Source, record_id: str) -> ProposalSource:
    link = ProposalSource(
        proposal_id=proposal.id,
        source_id=source.id,
        source_record_id=record_id,
        source_url=f"{source.url}/{record_id}",
        retrieved_at=T0,
        licence_id=source.licence_id,
        raw={},
        normalised={},
        first_seen=T0,
        last_seen=T0,
    )
    session.add(link)
    session.flush()
    return link


def make_proposal(session: Session, source: Source, record_id: str) -> tuple[Proposal, ProposalSource]:
    proposal = Proposal(
        public_id="",
        slug="",
        kind="solar",
        name_canonical=f"Project {record_id}",
        jurisdiction="US-TX",
        lifecycle_state="filed",
        identifiers={},
        publish_state="public",
        min_reuse_class="open",
        source_count=1,
    )
    session.add(proposal)
    session.flush()
    proposal.public_id = public_id("prop", proposal.id)
    proposal.slug = f"{slugify(proposal.name_canonical)}-{proposal.public_id[-6:].lower()}"
    return proposal, add_link(session, proposal, source, record_id)


def merge(session: Session, canonical: Proposal, absorbed: Proposal) -> Event:
    return merge_mod.merge_proposal(
        session, canonical=canonical, absorbed=absorbed, score=90.0, rationale="r"
    )


def where(session: Session, *links: ProposalSource) -> list[tuple[_uuid.UUID, _uuid.UUID | None]]:
    """Each link's (proposal, link_event_id), read back from the store."""
    session.flush()
    session.expire_all()
    out = []
    for link in links:
        row = session.get(ProposalSource, link.id)
        assert row is not None
        out.append((row.proposal_id, row.link_event_id))
    return out


@dataclass
class Chain:
    """A, B, C, D on their own sources; B and C hold a second link each. C into B (e1), then B into A
    (e2). D is a separate record for a third merge into A."""

    a: Proposal
    b: Proposal
    c: Proposal
    d: Proposal
    a1: ProposalSource
    b1: ProposalSource
    b2: ProposalSource
    c1: ProposalSource
    c2: ProposalSource
    d1: ProposalSource
    e1: Event
    e2: Event


@pytest.fixture()
def chain(session: Session) -> Chain:
    sa_, sb, sc, sd, sx = (make_source(session, f"src.{n}") for n in ("a", "b", "c", "d", "x"))
    a, a1 = make_proposal(session, sa_, "A1")
    b, b1 = make_proposal(session, sb, "B1")
    b2 = add_link(session, b, sx, "B2")
    c, c1 = make_proposal(session, sc, "C1")
    c2 = add_link(session, c, sx, "C2")
    d, d1 = make_proposal(session, sd, "D1")
    e1 = merge(session, b, c)
    e2 = merge(session, a, b)
    return Chain(a, b, c, d, a1, b1, b2, c1, c2, d1, e1, e2)


# ------------------------------------------------------------------------------------------- merge
def test_a_merge_stamps_every_link_it_moves_and_no_other(session: Session) -> None:
    s1, s2, s3 = make_source(session, "src.1"), make_source(session, "src.2"), make_source(session, "src.3")
    survivor, own = make_proposal(session, s1, "S1")
    absorbed, moved1 = make_proposal(session, s2, "M1")
    moved2 = add_link(session, absorbed, s3, "M2")

    event = merge(session, survivor, absorbed)

    assert where(session, moved1, moved2, own) == [
        (survivor.id, event.id),
        (survivor.id, event.id),
        (survivor.id, None),
    ]
    assert event.before["absorbed"][merge_mod.LINK_EVENT_PRIORS_KEY] == {
        str(moved1.id): None,
        str(moved2.id): None,
    }


def test_each_hop_of_a_chain_records_its_own_merge(session: Session, chain: Chain) -> None:
    k = chain
    # After C into B both C links named e1; B into A carried them on, so they name e2 and e2 records e1.
    assert where(session, k.c1, k.c2, k.b1, k.b2, k.a1) == [
        (k.a.id, k.e2.id),
        (k.a.id, k.e2.id),
        (k.a.id, k.e2.id),
        (k.a.id, k.e2.id),
        (k.a.id, None),
    ]
    assert k.e1.before["absorbed"][merge_mod.LINK_EVENT_PRIORS_KEY] == {
        str(k.c1.id): None,
        str(k.c2.id): None,
    }
    assert k.e2.before["absorbed"][merge_mod.LINK_EVENT_PRIORS_KEY] == {
        str(k.c1.id): str(k.e1.id),
        str(k.c2.id): str(k.e1.id),
        str(k.b1.id): None,
        str(k.b2.id): None,
    }


# ----------------------------------------------------------------------------------------- unmerge
def test_unmerging_the_later_merge_moves_back_only_its_links(session: Session, chain: Chain) -> None:
    k = chain
    e3 = merge(session, k.a, k.d)

    merge_mod.unmerge_proposal(session, k.e2.id)

    assert where(session, k.b1, k.b2, k.c1, k.c2, k.d1, k.a1) == [
        (k.b.id, None),
        (k.b.id, None),
        (k.b.id, k.e1.id),  # back with B, still naming C into B
        (k.b.id, k.e1.id),
        (k.a.id, e3.id),  # another merge's link stays
        (k.a.id, None),
    ]

    merge_mod.unmerge_proposal(session, k.e1.id)

    assert where(session, k.c1, k.c2, k.b1) == [(k.c.id, None), (k.c.id, None), (k.b.id, None)]


def test_unmerging_out_of_order_leaves_each_link_with_the_merge_that_still_holds_it(
    session: Session, chain: Chain
) -> None:
    k = chain
    # C out first, while B is still inside A: C's links come back from A, unstamped.
    merge_mod.unmerge_proposal(session, k.e1.id)
    assert where(session, k.c1, k.c2) == [(k.c.id, None), (k.c.id, None)]

    # Then B out: B's own links only. e2 still lists C's links, but they are no longer e2's to move.
    merge_mod.unmerge_proposal(session, k.e2.id)
    assert where(session, k.b1, k.b2, k.c1, k.c2, k.a1) == [
        (k.b.id, None),
        (k.b.id, None),
        (k.c.id, None),
        (k.c.id, None),
        (k.a.id, None),
    ]


def _as_written_before_stamping(session: Session, event: Event) -> None:
    """The shape of a merge stored before 2026-10-07: no priors in the payload, no stamp on the links
    (a test may write the event row; application code never updates `event`)."""
    before = dict(event.before)
    before["absorbed"] = {k: v for k, v in before["absorbed"].items() if k != merge_mod.LINK_EVENT_PRIORS_KEY}
    event.before = before
    for raw in before["absorbed"]["proposal_source_ids"]:
        link = session.get(ProposalSource, _uuid.UUID(raw))
        assert link is not None
        link.link_event_id = None
    session.flush()


def test_a_merge_written_before_stamping_still_unmerges_under_a_later_stamped_merge(session: Session) -> None:
    sa_, sb, sc = make_source(session, "src.a"), make_source(session, "src.b"), make_source(session, "src.c")
    a, a1 = make_proposal(session, sa_, "A1")
    b, b1 = make_proposal(session, sb, "B1")
    c, c1 = make_proposal(session, sc, "C1")
    e1 = merge(session, b, c)
    _as_written_before_stamping(session, e1)  # C into B, as stored before the change

    e2 = merge(session, a, b)  # B into A after the change: carries C's link on, stamped
    assert where(session, c1, b1, a1) == [(a.id, e2.id), (a.id, e2.id), (a.id, None)]
    assert e2.before["absorbed"][merge_mod.LINK_EVENT_PRIORS_KEY] == {str(b1.id): None, str(c1.id): None}

    # The pre-change merge is identified by its listed ids, wherever a later merge carried them, and
    # its link goes back with the value it had when that merge ran.
    merge_mod.unmerge_proposal(session, e1.id)
    assert where(session, c1) == [(c.id, None)]
    # The later stamped merge then moves back only B's own link; A keeps its own.
    merge_mod.unmerge_proposal(session, e2.id)
    assert where(session, b1, c1, a1) == [(b.id, None), (c.id, None), (a.id, None)]


def test_a_merge_written_before_stamping_unmerges_by_its_listed_ids(session: Session) -> None:
    s1, s2 = make_source(session, "src.1"), make_source(session, "src.2")
    survivor, own = make_proposal(session, s1, "S1")
    absorbed, moved = make_proposal(session, s2, "M1")
    event = merge(session, survivor, absorbed)
    _as_written_before_stamping(session, event)

    merge_mod.unmerge_proposal(session, event.id)

    assert where(session, moved, own) == [(absorbed.id, None), (survivor.id, None)]
