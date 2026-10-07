"""`services/ingest/personal_data.py`: classify existing rows, and cut a street address from the
name of a proposal a natural person sponsors as a reversible override (legal audit L-5).
Names and addresses are invented."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from services import personal_names
from services.db.models import Event, Organization, Proposal
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.personal_data import (
    REDACTION,
    classify_organizations,
    redact_person_addresses,
    redact_street_address,
)


def make_org(db: Session, name: str) -> Organization:
    """Local fixtures: `services.ingest` may not import `services.api` (infra/importlinter.ini),
    test modules included, so the API conftest's builders are not reused here."""
    org = Organization(
        public_id=f"org_{len(name)}{name[:3].upper()}",
        slug=name.lower().replace(" ", "-"),
        name_canonical=name,
        name_normalised=name.lower(),
        type="other",
        country="US",
    )
    db.add(org)
    db.flush()
    return org


def make_proposal(db: Session, suffix: str, sponsor: Organization, name: str) -> Proposal:
    prop = Proposal(
        public_id=f"prop_{suffix}",
        slug=f"p-{suffix}",
        kind="generation",
        name_canonical=name,
        jurisdiction="US-NY",
        lifecycle_state="filed",
        publish_state="public",
        min_reuse_class="open",
        sponsor_org_id=sponsor.id,
    )
    db.add(prop)
    db.flush()
    return prop


@pytest.fixture()
def db() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as session:
        yield session


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("NY - 12 Quillfeather Hill Rd - 2", f"NY - {REDACTION} - 2"),
        ("1200 N. County Road 5 Solar", f"{REDACTION} Solar"),
        ("8 Old Mill Lane", REDACTION),
        ("Quillfeather 74 MW Solar", None),
        ("Phase 2 Battery", None),
    ],
)
def test_street_addresses_are_found_and_capacities_are_not(name: str, expected: str | None) -> None:
    assert redact_street_address(name) == expected


def test_a_persons_proposal_loses_the_address_as_a_reversible_override(db: Session) -> None:
    person = make_org(db, "Dorothy Quillfeather")
    company = make_org(db, "Quillfeather Solar LLC")
    mine = make_proposal(db, "1", person, "NY - 12 Quillfeather Hill Rd - 2")
    theirs = make_proposal(db, "2", company, "NY - 40 Quillfeather Hill Rd - 1")
    pinned = make_proposal(db, "3", person, "9 Operator Chosen Rd")
    pinned.overrides = {"name_canonical": {"value": "9 Operator Chosen Rd", "user_id": "usr_x"}}
    db.flush()

    report = redact_person_addresses(db)
    assert report.redacted == [mine.public_id]
    assert report.already == 1
    assert mine.name_canonical == f"NY - {REDACTION} - 2"
    override = mine.overrides["name_canonical"]
    assert override["value"] == mine.name_canonical and override["basis"] == "personal_data_street_address"
    assert theirs.name_canonical == "NY - 40 Quillfeather Hill Rd - 1", (
        "a company's address is not personal data"
    )
    assert pinned.name_canonical == "9 Operator Chosen Rd", "an operator's override is never overwritten"

    event = db.scalar(select(Event).where(Event.subject_id == mine.id))
    assert event is not None and event.published_at is None and event.public_at is None
    assert "Quillfeather Hill" not in str(event.before) + str(event.after)
    assert override["event_id"].startswith("evt_")

    again = redact_person_addresses(db)
    assert again.redacted == [] and again.already == 2


def test_classify_reapplies_the_rule_and_the_curated_file(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    person = make_org(db, "Dorothy Quillfeather")
    company = make_org(db, "Margaret Quillfeather")  # say, a company named after its founder
    db.flush()
    assert person.personal_data and company.personal_data
    curated = personal_names.Overrides(
        not_personal=frozenset({personal_names.normalise("Margaret Quillfeather")}),
        personal_sha256=frozenset(),
    )
    monkeypatch.setattr(personal_names, "overrides", lambda: curated)
    report = classify_organizations(db)
    assert report.changed == 1 and report.flagged == 1
    assert company.personal_data is False and company.personal_data_basis == "curated_not_personal"
    assert classify_organizations(db).changed == 0
