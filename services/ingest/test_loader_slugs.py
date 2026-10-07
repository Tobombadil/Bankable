"""The loader never assigns a slug another row already holds, and never reassigns one on reload.

Reproduces the 2026-10-07 web e2e setup failure (`sqlite3.IntegrityError: UNIQUE constraint
failed: proposal.slug`, two "Untitled" proposals both slugged `untitled-657kte`): the slug suffix is
the uuid's low 30 random bits (`services/test_ids.py`), so the collision is forced here by giving
every minted uuid the same low 30 bits instead of waiting for a 1-in-2**30 draw.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Opportunity, Organization, Proposal, ProposalSource, new_uuid
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id
from services.ingest import loader
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.ingest.test_loader import open_source_entry, sample_proposal_row

TAIL_BITS = (1 << 30) - 1
TAIL = 0x0657_4B7E & TAIL_BITS


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


@pytest.fixture()
def colliding_uuids(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Any]:
    """Every uuid the loader mints keeps its real timestamp and middle random bits (so ids stay
    unique) but shares one low-30-bit tail, i.e. one six-digit public-id suffix."""

    def mint() -> Any:
        u = new_uuid()
        return type(u)(int=(u.int & ~TAIL_BITS) | TAIL)

    monkeypatch.setattr(loader, "new_uuid", mint)
    return mint


def untitled_row(srid: str) -> dict[str, Any]:
    """A queue row with no project name, no sponsor and no queue id: the loader names it
    "Untitled" (`_proposal_fields`), as it did 1,352 eval-fixture rows."""
    r = sample_proposal_row(srid)
    r.update(name_canonical=None, name_norm=None, sponsor_name=None, sponsor_norm=None, queue_id=None)
    r["raw"] = json.dumps({"Queue ID": srid})
    return r


def slugs_by_record(session: Session) -> dict[str, str]:
    rows = session.execute(
        select(ProposalSource.source_record_id, Proposal.slug).join(
            Proposal, Proposal.id == ProposalSource.proposal_id
        )
    )
    return dict(rows.tuples().all())


def test_two_untitled_proposals_with_the_same_suffix_both_load(
    session: Session, colliding_uuids: Any
) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    result = load_dataframe(
        session, src, "proposal", pd.DataFrame([untitled_row("A"), untitled_row("B")]), None
    )
    session.commit()

    assert result.proposals_created == 2
    proposals = session.scalars(select(Proposal).order_by(Proposal.public_id)).all()
    assert [p.name_canonical for p in proposals] == ["Untitled", "Untitled"]
    assert len({p.public_id[-6:] for p in proposals}) == 1, "the forced collision is in place"
    slugs = sorted((p.slug for p in proposals), key=len)
    assert len(set(slugs)) == 2
    first = next(p for p in proposals if p.slug == slugs[0])
    second = next(p for p in proposals if p.slug == slugs[1])
    assert first.slug == f"untitled-{first.public_id[-6:].lower()}", "a free slug keeps the usual shape"
    assert second.slug == f"untitled-{second.public_id[-7:].lower()}", "a clash takes one more digit"


def test_a_new_record_does_not_take_a_slug_an_earlier_load_stored(
    session: Session, colliding_uuids: Any
) -> None:
    """The check covers the table, not just this frame: the second load sees the first's slug."""
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    load_dataframe(session, src, "proposal", pd.DataFrame([untitled_row("A")]), None)
    session.commit()
    load_dataframe(session, src, "proposal", pd.DataFrame([untitled_row("A"), untitled_row("B")]), None)
    session.commit()

    slugs = slugs_by_record(session)
    assert set(slugs) == {"A", "B"} and len(set(slugs.values())) == 2


def test_a_merged_rows_slug_stays_reserved(session: Session, colliding_uuids: Any) -> None:
    """A merged-away row keeps its slug for the 301 (`services/api/merged_redirect.py`), so it is
    still taken."""
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    load_dataframe(session, src, "proposal", pd.DataFrame([untitled_row("A"), untitled_row("B")]), None)
    session.commit()
    a, b = session.scalars(select(Proposal).order_by(Proposal.slug)).all()
    b.merged_into_id = a.id
    session.commit()
    load_dataframe(session, src, "proposal", pd.DataFrame([untitled_row("C")]), None)
    session.commit()
    assert len(set(session.scalars(select(Proposal.slug)).all())) == 3


def test_a_stored_slug_is_unchanged_by_a_reload(session: Session, colliding_uuids: Any) -> None:
    """Slugs are public URLs: a reload matches each stored row by its source key and leaves the
    slug alone, including a slug that took the longer tail and a record whose fields changed."""
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    frame = [untitled_row("A"), untitled_row("B"), sample_proposal_row("Q1")]
    load_dataframe(session, src, "proposal", pd.DataFrame(frame), None)
    session.commit()
    before = slugs_by_record(session)
    assert sorted(len(s) for s in before.values() if s.startswith("untitled-")) == [15, 16]

    changed = [dict(r) for r in frame]
    changed[1]["capacity_mw"] = 250.0
    changed[2]["capacity_mw"] = 120.0
    again = load_dataframe(session, src, "proposal", pd.DataFrame(changed), None)
    session.commit()
    assert again.proposals_created == 0 and again.proposals_updated == 3
    assert slugs_by_record(session) == before


def test_two_untitled_opportunities_with_the_same_suffix_both_load(
    session: Session, colliding_uuids: Any
) -> None:
    entry = SourceEntry.from_yaml(
        {
            "id": "us.test.tenders",
            "name": "Test tenders",
            "jurisdiction": "US",
            "category": "procurement",
            "operator": "Test agency",
            "url": "https://example.org/tenders",
            "access": "api",
            "reuse": "open",
            "cadence": "daily",
            "tier": 1,
            "license": "US federal public domain",
        }
    )
    src = upsert_licence_and_source(session, entry, "v")

    def opp(n: str) -> dict[str, Any]:
        return {
            "record_id": f"us.test.tenders:N{n}",
            "source_id": "us.test.tenders",
            "source_record_id": f"N{n}",
            "source_url": f"https://example.org/tenders#N{n}",
            "retrieved_at": "2026-09-29T05:00:00Z",
            "licence_id": "us.test.tenders#irrelevant",
            "kind": "tender",
            "title": None,
            "jurisdiction": "US-AZ",
            "status": "open",
            "raw": json.dumps({"n": n}),
        }

    result = load_dataframe(session, src, "opportunity", pd.DataFrame([opp("1"), opp("2")]), None)
    session.commit()
    assert result.opportunities_created == 2
    opportunities = session.scalars(select(Opportunity)).all()
    assert {o.title for o in opportunities} == {"Untitled"}
    assert len({o.slug for o in opportunities}) == 2


def test_a_new_sponsor_whose_bare_and_six_digit_slugs_are_taken_gets_a_longer_tail(
    session: Session, colliding_uuids: Any
) -> None:
    """The organisation path's fallback was the same unchecked six-digit tail."""
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    next_tail = public_id("org", colliding_uuids())[-6:].lower()
    for slug in ("acme-power-llc", f"acme-power-llc-{next_tail}"):
        session.add(
            Organization(
                public_id=public_id("org", new_uuid()),
                slug=slug,
                name_canonical=f"Unrelated {slug}",
                name_normalised=f"unrelated {slug}",
                type="other",
                country="US",
            )
        )
    session.commit()

    load_dataframe(session, src, "proposal", pd.DataFrame([sample_proposal_row("Q1")]), None)
    session.commit()
    sponsor = session.scalar(select(Organization).where(Organization.name_canonical == "Acme Power LLC"))
    assert sponsor is not None
    assert sponsor.slug == f"acme-power-llc-{sponsor.public_id[-7:].lower()}"
