"""`services/resolve/operators.py`: a data centre is linked to the operator its own name names (docs/22 §27).

What is pinned: the operator file's evidence rule (every match carries the operator's own name; no row names a
natural person; a project vehicle that does not name its parent stays unlinked), the labelled set, and the
store pass (a reversible, non-public event per link; a sponsor set by anyone else is never touched; reruns
change nothing; a manual unlink sticks; a merged organisation is followed).
"""

from __future__ import annotations

import csv
import datetime as dt
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Event, Organization, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.loader import upsert_licence_and_source
from services.personal_names import classify
from services.resolve import merge as merge_mod
from services.resolve import operators as ops

UTC = dt.UTC
T0 = dt.datetime(2026, 10, 22, 0, 24, 23, tzinfo=UTC)
DEQ = "us.va.deq.data_center_air_sites"
ICIS = "us.epa.echo.icis_air"
LABELS = pathlib.Path(__file__).with_name("fixtures") / "data_centre_operator_labels.csv"


# ----------------------------------------------------------------------------------------- the file
def test_the_operator_file_loads_and_every_row_links_its_own_examples() -> None:
    registry = ops.load_registry()
    assert len(registry.rules) >= 100
    assert len({r.key for r in registry.rules}) == len(registry.rules)
    for rule in registry.rules:
        assert rule.examples, rule.key
        for example in rule.examples:
            assert ops.operator_for([example], registry).rule is rule, (rule.key, example)


def test_no_operator_is_a_natural_person() -> None:
    for rule in ops.load_registry().rules:
        assert not classify(rule.organization).personal, rule.organization


def test_every_match_carries_one_of_the_operators_own_names() -> None:
    """The evidence rule at run time: a pattern that matches text holding none of the row's names links
    nothing, whatever the pattern says."""
    doc = {
        "operators": [
            {
                "key": "acme",
                "organization": "Acme Data Inc.",
                "names": ["Acme"],
                "patterns": [r"DATA\s+CENTER"],
                "examples": ["ACME DATA CENTER"],
            }
        ]
    }
    with pytest.raises(ops.RegistryError, match="does not link"):
        ops.parse_registry(doc)
    rule = ops.OperatorRule(
        "acme", "Acme Data Inc.", "US", ("Acme",), (ops.re.compile(r"DATA\s+CENTER"),), None, ()
    )
    assert rule.match("ACME DATA CENTER") is None


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            {"key": "p", "organization": "John Smith", "names": ["Smith"], "examples": ["SMITH DC"]},
            "natural person",
        ),
        ({"key": "x", "organization": "X Corp", "names": ["X Corp"], "examples": []}, "no examples"),
        ({"key": "x", "organization": "", "names": ["X"], "examples": ["X"]}, "needs key"),
    ],
)
def test_a_bad_row_refuses_the_whole_file(row: dict[str, Any], message: str) -> None:
    with pytest.raises(ops.RegistryError, match=message):
        ops.parse_registry({"operators": [row]})


def test_a_duplicate_key_refuses_the_file() -> None:
    row = {"key": "a", "organization": "Alpha Corp", "names": ["Alpha"], "examples": ["ALPHA DC"]}
    with pytest.raises(ops.RegistryError, match="twice"):
        ops.parse_registry({"operators": [row, dict(row)]})


@pytest.mark.parametrize(
    ("name", "text", "hit"),
    [
        ("Cyrus One", "CYRUSONE - MESA", True),
        ("Wal-Mart", "WALMART EAST DATA CENTER", True),
        ("AT&T", "AT & T DATA CENTER", True),
        ("AT&T", "ATT DATA CENTER", False),
        ("QTS", "QTSX HOLDINGS", False),
        ("Edged", "EDGE CONNEX DATA CENTER", False),
        ("Level 3", "Level 3 Commuications Herndon Gateway", True),
    ],
)
def test_name_patterns_are_whole_words(name: str, text: str, hit: bool) -> None:
    assert bool(ops.re.search(ops.name_pattern(name), text, ops.re.IGNORECASE)) is hit


@pytest.mark.parametrize(
    "name",
    [
        "SI NVA05D LLC",  # STACK's naming, but the record does not say STACK
        "VADATA Inc MEG Four",  # an Amazon vehicle that does not say Amazon
        "Digital Filigree LLC",
        "QUALITY INVESTMENT PROPERTIES SUWANEE, LLC",
        "SICULUS, INC.",
        "ADP Appomattox Data Hub LLC",
        "Intergate - Ashburn, LLC",
        "THE WORLD BANK",
        "Smithsonian Institution",
        "COMPASS ENERGY STORAGE LLC",  # Compass only with data-centre words
        "SWITCH STATION 4",  # a bare "switch" is a word
        "VANTAGE POINT HOLDINGS",
        "IAD-T5 BUILDING",  # T5 and H5 are also building codes
        "BLDG H5 DATA HALL",
        "ANTHEM RANCH DATA CAMPUS",  # Anthem is also a town in Arizona
        "VISA TECHNOLOGY PARK",
        "OATH DEVELOPMENT PARTNERS",
    ],
)
def test_shells_public_bodies_and_common_words_stay_unlinked(name: str) -> None:
    match = ops.operator_for([name])
    assert match.rule is None and not match.conflict


def test_a_vehicle_that_names_its_operator_is_linked() -> None:
    assert (rule := ops.operator_for(["QTS Richmond V LLC"]).rule) is not None and rule.key == "qts"
    assert (rule := ops.operator_for(["STACK Infrastructure - SI NVA06A LLC"]).rule) is not None
    assert rule.key == "stack"


def test_a_name_two_operators_share_links_neither() -> None:
    match = ops.operator_for(["OPTUM C/O UNITED HEALTH GROUP"])
    assert match.rule is None
    assert match.conflict == ("optum", "unitedhealth")


def test_names_from_a_record_and_its_links_must_agree() -> None:
    assert ops.operator_for(["Equinix LLC - DC 13", "EQUINIX LLC"]).rule is not None
    assert ops.operator_for(["Equinix LLC - DC 13", "Microsoft Corp - LVL Data Center"]).conflict


def test_the_labelled_set() -> None:
    """The hand-checked seeded sample of linked records and every unlinked record of the 2026-10-23
    register (docs/22 §27): the file still links each to the operator, or to nothing, it was labelled with."""
    with LABELS.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) >= 60 + 200
    registry = ops.load_registry()
    for row in rows:
        match = ops.operator_for([row["name"]], registry)
        got = match.rule.key if match.rule is not None else ""
        assert got == row["expected_operator"], row["name"]
        if row["label"] == "conflict":
            assert match.conflict, row["name"]
    linked = [r for r in rows if r["label"] in {"correct", "uncertain", "wrong"}]
    assert len(linked) >= 60
    assert sum(r["label"] == "wrong" for r in linked) == 0


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
            "category": "permits",
            "operator": "Test operator",
            "url": f"https://example.org/{id_}",
            "access": "api",
            "reuse": "open",
            "cadence": "daily",
            "tier": 1,
            "license": f"licence for {id_}",
        }
    )
    return upsert_licence_and_source(session, entry, "test-manifest")


def make_org(session: Session, name: str, **kw: Any) -> Organization:
    org = Organization(
        public_id="",
        slug="",
        name_canonical=name,
        name_normalised=name.lower(),
        type="other",
        country="US",
        **kw,
    )
    session.add(org)
    session.flush()
    org.public_id = public_id("org", org.id)
    org.slug = slugify(name) + "-" + org.public_id[-4:].lower()
    session.flush()
    return org


def make_load(
    session: Session,
    source: Source,
    record_id: str,
    name: str,
    *,
    kind: str = "load",
    sponsor: Organization | None = None,
    raw_only: bool = False,
) -> Proposal:
    proposal = Proposal(
        public_id="",
        slug="",
        kind=kind,
        name_canonical=name,
        technology=kind,
        jurisdiction="US-VA",
        lifecycle_state="filed",
        identifiers={},
        publish_state="public",
        min_reuse_class="open",
        source_count=1,
        sponsor_org_id=sponsor.id if sponsor else None,
        last_changed=T0,
        first_seen=T0,
    )
    session.add(proposal)
    session.flush()
    proposal.public_id = public_id("prop", proposal.id)
    proposal.slug = f"{slugify(name)}-{proposal.public_id[-6:].lower()}"
    session.add(
        ProposalSource(
            proposal_id=proposal.id,
            source_id=source.id,
            source_record_id=record_id,
            source_url=f"{source.url}?id={record_id}",
            retrieved_at=T0,
            licence_id=source.licence_id,
            raw={"PLA_NAME": name} if raw_only else {},
            normalised={} if raw_only else {"name_canonical": name},
            first_seen=T0,
            last_seen=T0,
        )
    )
    session.flush()
    return proposal


def _events(session: Session, proposal: Proposal) -> list[Event]:
    return list(
        session.scalars(
            select(Event)
            .where(Event.subject_id == proposal.id, Event.event_type.in_(ops.EVENT_TYPES))
            .order_by(Event.seq)
        )
    )


def test_a_link_is_one_non_public_event_with_the_links_provenance(session: Session) -> None:
    deq = make_source(session, DEQ)
    p = make_load(session, deq, "74362", "Amazon Data Services Inc IAD-175 Stafford Technology Campus")
    report = ops.link_data_centre_operators(session)
    assert (report.linked, report.organizations_created, report.by_operator["amazon"]) == (1, 1, 1)
    org = session.get(Organization, p.sponsor_org_id)
    assert org is not None and org.name_canonical == "Amazon.com, Inc." and org.type == "other"
    assert not org.personal_data
    (event,) = _events(session, p)
    assert event.event_type == ops.LINK_EVENT and event.subject_type == "proposal"
    assert event.published_at is None and event.public_at is None  # never news
    assert (event.source_id, event.licence_id) == (DEQ, deq.licence_id)
    assert event.source_url == f"{deq.url}?id=74362" and event.retrieved_at is not None
    assert event.before == {"sponsor_org_id": None}
    assert event.after is not None and event.after["operator_key"] == "amazon"
    assert event.after["evidence"][0] == {
        "source_id": DEQ,
        "source_record_id": "74362",
        "name": p.name_canonical,
        "matched": "Amazon",
    }
    last_changed = p.last_changed if p.last_changed.tzinfo else p.last_changed.replace(tzinfo=UTC)
    assert last_changed > T0

    again = ops.link_data_centre_operators(session)
    assert (again.linked, again.kept, len(again.events)) == (0, 1, 0)
    assert len(_events(session, p)) == 1


def test_an_existing_organisation_with_the_key_is_reused(session: Session) -> None:
    icis = make_source(session, ICIS)
    existing = make_org(session, "MICROSOFT CORP")
    p = make_load(session, icis, "WY0001", "MICROSOFT CHEYENNE DATA CENTER (MICROSOFT CORPORATION)")
    report = ops.link_data_centre_operators(session)
    assert (report.linked, report.organizations_created) == (1, 0)
    assert p.sponsor_org_id == existing.id


def test_a_sponsor_someone_else_set_is_never_touched(session: Session) -> None:
    deq = make_source(session, DEQ)
    other = make_org(session, "Loudoun Campus Owner LLC")
    p = make_load(session, deq, "1", "Equinix LLC - DC 13", sponsor=other)
    report = ops.link_data_centre_operators(session)
    assert (report.linked, report.sponsored_elsewhere) == (0, 1)
    assert p.sponsor_org_id == other.id and not _events(session, p)


def test_only_live_large_load_records_are_read(session: Session) -> None:
    deq = make_source(session, DEQ)
    solar = make_load(session, deq, "2", "Microsoft Solar Farm LLC", kind="generation")
    merged = make_load(session, deq, "3", "Equinix LLC")
    survivor = make_load(session, deq, "4", "Equinix LLC - DC 14")
    merged.merged_into_id = survivor.id
    session.flush()
    report = ops.link_data_centre_operators(session)
    assert report.considered == 1 and report.linked == 1
    assert solar.sponsor_org_id is None and merged.sponsor_org_id is None


def test_a_link_written_before_normalised_names_reads_the_raw_name(session: Session) -> None:
    deq = make_source(session, DEQ)
    p = make_load(session, deq, "5", "Untitled", raw_only=True)
    p.name_canonical = "Untitled"
    session.flush()
    ops.link_data_centre_operators(session)
    assert p.sponsor_org_id is None  # "Untitled" plus a raw PLA_NAME of "Untitled"
    q = make_load(session, deq, "6", "CyrusOne Sterling IX/X/XI", raw_only=True)
    q.name_canonical = "Sterling campus"
    session.flush()
    ops.link_data_centre_operators(session)
    org = session.get(Organization, q.sponsor_org_id)
    assert org is not None and org.name_canonical == "CyrusOne"


def test_unmatched_and_conflicting_records_stay_unlinked(session: Session) -> None:
    icis = make_source(session, ICIS)
    a = make_load(session, icis, "7", "SI NVA05D LLC")
    b = make_load(session, icis, "8", "OPTUM C/O UNITED HEALTH GROUP")
    report = ops.link_data_centre_operators(session)
    assert (report.unmatched, report.conflicts, report.linked) == (1, 1, 0)
    assert a.sponsor_org_id is None and b.sponsor_org_id is None
    assert not session.scalars(select(Event)).all()


def test_an_organisation_flagged_as_a_person_is_never_linked(session: Session) -> None:
    deq = make_source(session, DEQ)
    flagged = make_org(session, "Amazon.com Inc")
    flagged.personal_data = True
    session.flush()
    p = make_load(session, deq, "9", "Amazon DCA-50")
    report = ops.link_data_centre_operators(session)
    assert (report.refused_personal, report.linked) == (1, 0)
    assert p.sponsor_org_id is None


def test_unlink_restores_the_record_and_is_never_undone_by_the_pass(session: Session) -> None:
    deq = make_source(session, DEQ)
    p = make_load(session, deq, "10", "QTS Richmond V LLC")
    (link,) = ops.link_data_centre_operators(session).events
    reversal = ops.unlink(session, link.id, reason="operator not confirmed")
    assert p.sponsor_org_id is None
    assert reversal.event_type == ops.UNLINK_EVENT and reversal.reverses_event_id == link.id
    assert reversal.after == {"sponsor_org_id": None, "suppress": True}
    assert reversal.public_at is None and reversal.source_id == DEQ
    assert ops.unlink(session, link.id).id == reversal.id  # idempotent
    report = ops.link_data_centre_operators(session)
    assert (report.suppressed, report.linked) == (1, 0) and p.sponsor_org_id is None


def test_a_record_the_file_no_longer_links_is_unlinked_and_relinked_when_it_does_again(
    session: Session,
) -> None:
    deq = make_source(session, DEQ)
    p = make_load(session, deq, "11", "Vantage Data Centers VA41 LLC")
    full = ops.load_registry()
    ops.link_data_centre_operators(session, registry=full)
    vantage_org = p.sponsor_org_id
    without = ops.Registry(version="test", rules=tuple(r for r in full.rules if r.key != "vantage"))
    report = ops.link_data_centre_operators(session, registry=without)
    assert (report.unlinked, report.unmatched) == (1, 1) and p.sponsor_org_id is None
    assert _events(session, p)[-1].after == {"sponsor_org_id": None, "suppress": False}
    back = ops.link_data_centre_operators(session, registry=full)
    assert back.linked == 1 and p.sponsor_org_id == vantage_org
    assert [e.event_type for e in _events(session, p)] == [ops.LINK_EVENT, ops.UNLINK_EVENT, ops.LINK_EVENT]


def test_a_merged_operator_is_followed_to_its_survivor(session: Session) -> None:
    deq = make_source(session, DEQ)
    p = make_load(session, deq, "12", "Digital Realty - Ashburn Campus")
    ops.link_data_centre_operators(session)
    linked = session.get(Organization, p.sponsor_org_id)
    assert linked is not None
    survivor = make_org(session, "Digital Realty Trust Inc")
    merge_mod.merge_organization(session, canonical=survivor, absorbed=linked, rationale="test")
    assert p.sponsor_org_id == survivor.id
    report = ops.link_data_centre_operators(session)
    assert (report.kept, report.linked, report.relinked) == (1, 0, 0)


def test_a_dry_run_writes_nothing(session: Session) -> None:
    deq = make_source(session, DEQ)
    p = make_load(session, deq, "13", "Microsoft Corporation - LYH03 Datacenter")
    report = ops.link_data_centre_operators(session, dry_run=True)
    assert (report.linked, report.organizations_created) == (1, 1)
    assert p.sponsor_org_id is None
    assert not session.scalars(select(Event)).all()
    assert not session.scalars(select(Organization)).all()


def test_the_resolve_pass_runs_it_and_survives_its_failure(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    deq = make_source(session, DEQ)
    p = make_load(session, deq, "14", "EDGECONNEX ATL11, LLC")
    make_org(session, "Twin Power LLC")
    make_org(session, "TWIN POWER, L.L.C.")
    report = merge_mod.resolve_organizations(session, entity_types=False)
    assert report.organizations_absorbed == 1
    assert report.operator_links is not None and report.operator_links["linked"] == 1
    assert p.sponsor_org_id is not None

    q = make_load(session, deq, "15", "Equinix LLC - DC 97")
    make_org(session, "Another Power LLC")
    make_org(session, "ANOTHER POWER L.L.C.")

    def boom(session: Session, **_: Any) -> Any:
        session.add(make_org(session, "Half Written Org"))
        raise RuntimeError("pass failed")

    monkeypatch.setattr(ops, "link_data_centre_operators", boom)
    failed = merge_mod.resolve_organizations(session, entity_types=False)
    assert failed.operator_links == {"error": "RuntimeError"}
    assert failed.organizations_absorbed == 1  # the merges stand
    assert q.sponsor_org_id is None
    names = set(session.scalars(select(Organization.name_canonical)))
    assert "Half Written Org" not in names  # the pass's own writes rolled back
