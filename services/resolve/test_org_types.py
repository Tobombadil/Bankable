"""`services/resolve/org_types.py`: organisation types from EIA-860 Schedule 1 (docs/22 §27).

Pinned: the read keeps three columns and never a natural person's row; the code table is docs/28 §8.2's;
the join prefers the EIA utility id an organisation's EIA-860M record carries over the name key; only `other`
(or a type this module set) changes; each change is one non-public event with the snapshot's provenance; a
revert sticks. The workbook is built in the test with the real sheet's layout (title row, header row,
eleven columns), never fetched.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from collections.abc import Iterator
from typing import Any

import openpyxl
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.base import ParseError, PreviousSnapshot
from pipeline.connectors.registry import SourceEntry
from services.db.models import Event, Organization, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.loader import ORGANIZATION_TYPES, upsert_licence_and_source
from services.resolve import merge as merge_mod
from services.resolve import org_types as ot

UTC = dt.UTC
RETRIEVED = dt.datetime(2026, 9, 19, 20, 48, 22, tzinfo=UTC)
URL = "https://www.eia.gov/electricity/data/eia860/xls/eia8602025.zip"
HEADER = [
    "Utility ID",
    "Utility Name",
    "Street Address",
    "City",
    "State",
    "Zip",
    "Owner of Plants Reported on Form?",
    "Operator of Plants Reported on Form?",
    "Asset Manager of Plants Reported on Form?",
    "Other Relationships with Plants Reported on Form?",
    "Entity Type",
]
#: (id, name, entity type). The street addresses are filler the read must never keep.
UTILITIES = [
    (7140, "Georgia Power Co", "I"),
    (13994, "Oglethorpe Power Corporation", "C"),
    (1015, "City of Austin - (TX)", "M"),
    (19876, "Florida Municipal Power Agency", "P"),
    (18642, "Tennessee Valley Authority", "F"),
    (56769, "NextEra Energy Resources, LLC", "Q"),
    (7, "Primary Products Ingredients Americas, LLC", "IND"),
    (61001, "BASF Corp", "IND"),
    (61002, "BASF Corporation", "Q"),
    (62000, "Mary Johnson", "Q"),  # a natural person's row: dropped at parse
    (63000, "Sunny Hill Solar LLC", "Q"),
]


def workbook(rows: list[tuple[int, str, str]] = UTILITIES, *, header: list[str] = HEADER) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = ot.SHEET
    ws.append(["2025 Form EIA-860 Data - Schedule 1, 'Utility Data'"])
    ws.append(header)
    for uid, name, code in rows:
        ws.append(
            [uid, name, "1 Main Street", "Town", "GA", "30301", "Y", "Y", None, None, code][: len(header)]
        )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def archive(content: bytes, member: str = "1___Utility_Y2025.xlsx") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("LayoutY2025.xlsx", b"layout")
        zf.writestr(member, content)
    return buf.getvalue()


def schedule(rows: list[tuple[int, str, str]] = UTILITIES, licence_id: str | None = None) -> ot.Schedule1:
    utilities, year = ot.read_archive(archive(workbook(rows)))
    return ot.Schedule1(utilities, year, URL, RETRIEVED, licence_id)


# ----------------------------------------------------------------------------------------- the read
def test_the_read_keeps_three_columns_and_drops_natural_persons() -> None:
    frame = ot.parse_utility_sheet(workbook())
    assert list(frame.columns) == ["utility_id", "utility_name", "entity_type"]
    assert "Mary Johnson" not in set(frame["utility_name"])
    assert frame.attrs["personal_dropped"] == 1
    assert len(frame) == len(UTILITIES) - 1
    assert frame.loc[frame["utility_name"] == "Georgia Power Co", "utility_id"].item() == "7140"
    text = frame.to_csv()
    assert "Main Street" not in text and "30301" not in text


def test_a_changed_layout_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        ot.parse_utility_sheet(workbook(header=HEADER[:10]))
    with pytest.raises(ParseError):
        ot.read_archive(archive(workbook(), member="4___Owner_Y2025.xlsx"))


def test_the_newest_schedule_in_a_zip_is_read() -> None:
    names = ["1___Utility_Y2024.xlsx", "1___Utility_Y2025.xlsx", "4___Owner_Y2025.xlsx"]
    assert ot.find_utility_member(names) == ("1___Utility_Y2025.xlsx", 2025)
    assert ot.find_utility_member(["4___Owner_Y2025.xlsx"]) is None


def test_the_code_table_is_docs_28s() -> None:
    table = ot.load_mapping()
    assert set(table.types.values()) <= set(ORGANIZATION_TYPES)
    assert table.types == {
        "C": "coop",
        "I": "utility",
        "Q": "ipp",
        "M": "agency",
        "P": "agency",
        "F": "agency",
        "S": "agency",
        "IND": "other",
        "COM": "other",
    }
    with pytest.raises(ValueError, match="not an organisation type"):
        ot.parse_mapping({"codes": {"C": {"type": "cooperative"}}}, allowed=ORGANIZATION_TYPES)


# ------------------------------------------------------------------------------------------ the store
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
            "category": "registry",
            "operator": "EIA",
            "url": f"https://example.org/{id_}",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "annual",
            "tier": 1,
            "license": "public domain",
        }
    )
    return upsert_licence_and_source(session, entry, "test-manifest")


def make_org(session: Session, name: str, type_: str = "other", **kw: Any) -> Organization:
    org = Organization(
        public_id="",
        slug="",
        name_canonical=name,
        name_normalised=name.lower(),
        type=type_,
        country="US",
        **kw,
    )
    session.add(org)
    session.flush()
    org.public_id = public_id("org", org.id)
    org.slug = slugify(name) + "-" + org.public_id[-4:].lower()
    session.flush()
    return org


def sponsor_eia_record(
    session: Session, source: Source, org: Organization, entity_id: str, entity_name: str | None = None
) -> None:
    proposal = Proposal(
        public_id="",
        slug="",
        kind="generation",
        name_canonical=f"Plant of {org.name_canonical}",
        jurisdiction="US-GA",
        lifecycle_state="filed",
        identifiers={},
        publish_state="public",
        min_reuse_class="open",
        source_count=1,
        sponsor_org_id=org.id,
    )
    session.add(proposal)
    session.flush()
    proposal.public_id = public_id("prop", proposal.id)
    proposal.slug = f"plant-{proposal.public_id[-6:].lower()}"
    session.add(
        ProposalSource(
            proposal_id=proposal.id,
            source_id=source.id,
            source_record_id=f"{entity_id}:{org.public_id}",
            source_url=source.url,
            retrieved_at=RETRIEVED,
            licence_id=source.licence_id,
            raw={"Entity ID": int(entity_id), "Entity Name": entity_name or org.name_canonical},
            normalised={},
            first_seen=RETRIEVED,
            last_seen=RETRIEVED,
        )
    )
    session.flush()


def type_events(session: Session) -> list[Event]:
    return list(
        session.scalars(select(Event).where(Event.event_type.in_(ot.EVENT_TYPES)).order_by(Event.seq))
    )


def test_types_follow_the_table_and_each_change_is_one_non_public_event(session: Session) -> None:
    src = make_source(session, ot.SOURCE_ID)
    georgia = make_org(session, "GEORGIA POWER COMPANY")  # the same key as EIA's "Georgia Power Co"
    coop = make_org(session, "Oglethorpe Power Corp")
    austin = make_org(session, "City of Austin - (TX)")
    fmpa = make_org(session, "Florida Municipal Power Agency")
    tva = make_org(session, "Tennessee Valley Authority")
    ipp = make_org(session, "NextEra Energy Resources LLC")
    industrial = make_org(session, "Primary Products Ingredients Americas LLC")
    stranger = make_org(session, "Not In EIA Holdings LLC")
    report = ot.apply_entity_types(session, schedule(licence_id=src.licence_id))
    assert (georgia.type, coop.type, austin.type, fmpa.type, tva.type, ipp.type) == (
        "utility",
        "coop",
        "agency",
        "agency",
        "agency",
        "ipp",
    )
    assert industrial.type == "other" and stranger.type == "other"
    assert (report.typed, report.stays_other, report.unmatched) == (6, 1, 1)
    assert report.by_match == {"name": 7}
    events = type_events(session)
    assert len(events) == 6
    event = next(e for e in events if e.subject_id == georgia.id)
    assert event.subject_type == "organization" and event.public_at is None and event.published_at is None
    assert (event.source_id, event.source_url, event.licence_id) == (ot.SOURCE_ID, URL, src.licence_id)
    assert event.before == {"type": "other"}
    assert event.after is not None and event.after["type"] == "utility"
    assert event.after["eia_entity_type"] == ["I"] and event.after["eia_utility_ids"] == ["7140"]
    assert event.after["match"] == "name" and event.after["schedule_year"] == 2025

    again = ot.apply_entity_types(session, schedule(licence_id=src.licence_id))
    assert (again.typed, again.kept) == (0, 6) and len(type_events(session)) == 6


def test_types_counted_before_and_after(session: Session) -> None:
    make_source(session, ot.SOURCE_ID)
    make_org(session, "Georgia Power Co")
    make_org(session, "Unrelated LLC")
    report = ot.apply_entity_types(session, schedule())
    assert report.types_before == {"other": 2}
    assert report.types_after == {"utility": 1, "other": 1}


def test_the_eia_utility_id_wins_over_the_name(session: Session) -> None:
    make_source(session, ot.SOURCE_ID)
    monthly = make_source(session, ot.MONTHLY_SOURCE_ID)
    # A sponsor whose own name EIA does not hold, and one whose name says IPP but whose id says cooperative.
    renamed = make_org(session, "Old Name Power Partners")
    sponsor_eia_record(session, monthly, renamed, "13994")
    misnamed = make_org(session, "Sunny Hill Solar LLC")
    sponsor_eia_record(session, monthly, misnamed, "13994")
    report = ot.apply_entity_types(session, schedule())
    assert renamed.type == "coop" and misnamed.type == "coop"
    assert report.by_match == {"entity_id": 2}
    assert report.id_name_disagree == 1
    event = next(e for e in type_events(session) if e.subject_id == renamed.id)
    assert event.after is not None and event.after["match"] == "entity_id"


def test_an_eia_id_on_a_record_another_register_sponsors_is_not_used(session: Session) -> None:
    """A merged record's sponsor can be an ERCOT interconnecting entity; the EIA link's `Entity ID` is
    then another company's, and is not applied to the sponsor."""
    make_source(session, ot.SOURCE_ID)
    monthly = make_source(session, ot.MONTHLY_SOURCE_ID)
    ercot_sponsor = make_org(session, "Cazadores Solar LLC")
    sponsor_eia_record(session, monthly, ercot_sponsor, "7140", entity_name="Georgia Power Co")
    report = ot.apply_entity_types(session, schedule())
    assert ercot_sponsor.type == "other" and report.by_match == {}
    assert report.unmatched == 1


def test_a_name_match_on_an_organisation_with_only_foreign_records_is_refused(session: Session) -> None:
    make_source(session, ot.SOURCE_ID)
    neso = make_source(session, "gb.neso.tec_register")
    gb = make_org(session, "Sunny Hill Solar Limited")  # the key of EIA's US "Sunny Hill Solar LLC"
    sponsor_eia_record(session, neso, gb, "1")
    gb_record = session.scalars(select(Proposal).where(Proposal.sponsor_org_id == gb.id)).one()
    gb_record.jurisdiction = "GB"
    session.flush()
    report = ot.apply_entity_types(session, schedule())
    assert gb.type == "other" and report.foreign_skipped == 1


def test_conflicting_codes_leave_the_type(session: Session) -> None:
    make_source(session, ot.SOURCE_ID)
    basf = make_org(session, "BASF")  # BASF Corp (IND, other) and BASF Corporation (Q, ipp) share a key
    report = ot.apply_entity_types(session, schedule())
    assert basf.type == "other" and report.conflicts == 1 and not type_events(session)


def test_a_type_set_elsewhere_and_a_person_are_never_touched(session: Session) -> None:
    make_source(session, ot.SOURCE_ID)
    issuer = make_org(session, "Georgia Power Co", type_="agency", is_curated_issuer=True)
    person = make_org(session, "City of Austin - (TX)")
    person.personal_data = True
    session.flush()
    report = ot.apply_entity_types(session, schedule())
    assert issuer.type == "agency" and person.type == "other"
    assert (report.set_elsewhere, report.personal_skipped) == (1, 1)
    assert not type_events(session)


def test_a_changed_code_retypes_and_a_dropped_one_returns_to_other(session: Session) -> None:
    make_source(session, ot.SOURCE_ID)
    org = make_org(session, "Georgia Power Co")
    ot.apply_entity_types(session, schedule())
    assert org.type == "utility"
    recoded = [(7140, "Georgia Power Co", "Q")]
    report = ot.apply_entity_types(session, schedule(recoded))
    assert (org.type, report.retyped) == ("ipp", 1)
    report = ot.apply_entity_types(session, schedule([(7140, "Georgia Power Co", "COM")]))
    assert (org.type, report.reverted_to_other) == ("other", 1)
    assert [e.after["type"] for e in type_events(session) if e.after] == ["utility", "ipp", "other"]


def test_a_type_someone_else_set_back_to_other_is_left_alone(session: Session) -> None:
    """An editor's reset to `other` without `revert` stands: the pass never retypes over it."""
    make_source(session, ot.SOURCE_ID)
    org = make_org(session, "Oglethorpe Power Corporation")
    ot.apply_entity_types(session, schedule())
    assert org.type == "coop"
    org.type = "other"
    session.flush()
    report = ot.apply_entity_types(session, schedule())
    assert org.type == "other" and report.cleared_elsewhere == 1 and report.typed == 0
    assert len(type_events(session)) == 1


def test_a_revert_restores_and_sticks(session: Session) -> None:
    make_source(session, ot.SOURCE_ID)
    org = make_org(session, "Oglethorpe Power Corporation")
    (event,) = ot.apply_entity_types(session, schedule()).events
    reversal = ot.revert(session, event.id, reason="checked by hand")
    assert org.type == "other"
    assert reversal.event_type == ot.REVERT_EVENT and reversal.reverses_event_id == event.id
    assert reversal.source_id == ot.SOURCE_ID and reversal.public_at is None
    assert ot.revert(session, event.id).id == reversal.id
    report = ot.apply_entity_types(session, schedule())
    assert report.suppressed == 1 and org.type == "other"


def test_without_the_source_row_nothing_is_written(session: Session) -> None:
    org = make_org(session, "Georgia Power Co")
    report = ot.apply_entity_types(session, schedule())
    assert report.skipped == "no us.eia.860 source row" and org.type == "other"
    assert not type_events(session)


def test_a_dry_run_writes_nothing(session: Session) -> None:
    make_source(session, ot.SOURCE_ID)
    org = make_org(session, "Georgia Power Co")
    report = ot.apply_entity_types(session, schedule(), dry_run=True)
    assert report.typed == 1 and org.type == "other" and not type_events(session)


class FakeStore:
    def __init__(self, content: bytes | None) -> None:
        self.content = content

    def last_snapshot(self, source_id: str) -> PreviousSnapshot | None:
        assert source_id == ot.SOURCE_ID
        if self.content is None:
            return None
        body = self.content
        import hashlib

        record = {
            "status": "ok",
            "snapshot": {
                "sha256": hashlib.sha256(body).hexdigest(),
                "fetched_url": URL,
                "retrieved_at": "2026-09-19T20:48:22Z",
                "licence_id": None,
            },
        }
        return PreviousSnapshot(record=record, load=lambda: body)


def test_the_stored_snapshot_is_read_and_its_provenance_kept() -> None:
    sched = ot.latest_schedule1(FakeStore(archive(workbook())))
    assert sched is not None
    assert (sched.year, sched.source_url, sched.retrieved_at) == (2025, URL, RETRIEVED)
    assert ot.latest_schedule1(FakeStore(None)) is None


def test_the_same_snapshot_is_parsed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore(archive(workbook([(7140, "Georgia Power Co", "I")])))
    first = ot.latest_schedule1(store)
    calls: list[bytes] = []
    monkeypatch.setattr(ot, "read_archive", lambda body: calls.append(body) or (None, 0))
    again = ot.latest_schedule1(store)
    assert first is not None and again is not None and not calls
    assert again.utilities is first.utilities


def test_the_resolve_pass_types_organisations_from_the_store(session: Session) -> None:
    src = make_source(session, ot.SOURCE_ID)
    org = make_org(session, "Georgia Power Co")
    report = merge_mod.resolve_organizations(
        session, link_operators=False, entity_types_store=FakeStore(archive(workbook()))
    )
    assert report.entity_types is not None and report.entity_types["typed"] == 1
    assert org.type == "utility"
    (event,) = type_events(session)
    assert event.licence_id == src.licence_id
    empty = merge_mod.resolve_organizations(session, link_operators=False, entity_types_store=FakeStore(None))
    assert empty.entity_types is None
