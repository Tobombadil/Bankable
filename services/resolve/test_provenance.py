"""Every event the resolver writes carries a complete provenance quartet (docs/22 §13.7; CLAUDE.md).

Before 2026-10-07 the proposal `merged`/`unmerged`, organisation `merged`/`unmerged` and
suppression `unpublished` events carried none, and a key-based organisation merge carried a
`licence_id` alone (the spelling alias's, with no source to go with it). Each per-type test below
fails on that code. The rule tested is `services/resolve/provenance.py`'s: one member record's
whole quartet, the most restrictive licence first, the triggering side on a tie.

Fixtures are local rather than imported from `tests/`, per this repo's convention that test
modules do not import one another.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from pipeline.normalize import org_key
from services.db.models import Event, Licence, Organization, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.posture import platform_posture, publishable_reuse_classes
from services.resolve import merge as merge_mod
from services.resolve import provenance
from services.resolve.suppress import Suppression, apply_suppressions

UTC = dt.UTC
T_OLD = dt.datetime(2026, 9, 1, tzinfo=UTC)
T_NEW = dt.datetime(2026, 9, 20, tzinfo=UTC)
ICIS = "us.epa.echo.icis_air"


@pytest.fixture()
def session() -> Iterator[Session]:
    import services.resolve.models  # noqa: F401 -- register resolution_decision on Base.metadata

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def make_source(session: Session, id_: str, *, category: str = "generation_queue") -> Source:
    entry = SourceEntry.from_yaml(
        {
            "id": id_,
            "name": f"Test {id_}",
            "jurisdiction": "US",
            "category": category,
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


def make_restricted(session: Session, id_: str) -> Source:
    """A source whose licence was reclassified `restricted` after its rows loaded (the loader refuses
    a restricted source outright, so this is the only way such a link reaches the store)."""
    src = make_source(session, id_)
    lic = session.get(Licence, src.licence_id)
    assert lic is not None
    lic.reuse_class = "restricted"
    lic.allows_derived_publication = lic.allows_raw_publication = False
    lic.allows_api_redistribution = lic.allows_bulk_export = False
    session.flush()
    return src


def make_proposal(
    session: Session,
    source: Source,
    record_id: str,
    *,
    retrieved_at: dt.datetime = T_OLD,
    sponsor: Organization | None = None,
) -> tuple[Proposal, ProposalSource]:
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
        sponsor_org_id=sponsor.id if sponsor else None,
    )
    session.add(proposal)
    session.flush()
    proposal.public_id = public_id("prop", proposal.id)
    proposal.slug = f"{slugify(proposal.name_canonical)}-{proposal.public_id[-6:].lower()}"
    link = ProposalSource(
        proposal_id=proposal.id,
        source_id=source.id,
        source_record_id=record_id,
        source_url=f"{source.url}/{record_id}",
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


def make_org(session: Session, name: str) -> Organization:
    org = Organization(
        public_id="", slug="", name_canonical=name, name_normalised=name.lower(), type="other", country="US"
    )
    session.add(org)
    session.flush()
    org.public_id = public_id("org", org.id)
    org.slug = f"{slugify(name)}-{org.public_id[-6:].lower()}"
    session.flush()
    return org


def quartet_of(row: Any) -> tuple[Any, ...]:
    return (row.source_id, row.source_url, _aware(row.retrieved_at), row.licence_id)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def assert_complete(event: Event) -> None:
    assert provenance.is_complete(event.source_id, event.source_url, event.retrieved_at, event.licence_id), (
        event.event_type,
        quartet_of(event),
    )


# ------------------------------------------------------------------------------- per event type
def test_a_proposal_merge_carries_one_members_whole_quartet(session: Session) -> None:
    eia, caiso = make_source(session, "src.eia", category="registry"), make_source(session, "src.caiso")
    survivor, _ = make_proposal(session, eia, "P1-1", retrieved_at=T_NEW)
    absorbed, absorbed_link = make_proposal(session, caiso, "Q42", retrieved_at=T_OLD)

    event = merge_mod.merge_proposal(
        session, canonical=survivor, absorbed=absorbed, score=90.0, rationale="r"
    )

    assert_complete(event)
    # Same licence class on both sides: the absorbed record, whose evidence the merge adds, is cited
    # even though the survivor's link is more recent.
    assert quartet_of(event) == quartet_of(absorbed_link)


def test_a_proposal_unmerge_carries_the_quartet_of_the_merge_it_reverses(session: Session) -> None:
    a, b = make_source(session, "src.a"), make_source(session, "src.b")
    survivor, _ = make_proposal(session, a, "A1")
    absorbed, _ = make_proposal(session, b, "B1")
    merged = merge_mod.merge_proposal(
        session, canonical=survivor, absorbed=absorbed, score=90.0, rationale="r"
    )

    undone = merge_mod.unmerge_proposal(session, merged.id)

    assert_complete(undone)
    assert quartet_of(undone) == quartet_of(merged)


def test_an_unmerge_of_a_merge_written_before_the_rule_derives_its_quartet(session: Session) -> None:
    a, b = make_source(session, "src.a"), make_source(session, "src.b")
    survivor, _ = make_proposal(session, a, "A1")
    absorbed, absorbed_link = make_proposal(session, b, "B1")
    merged = merge_mod.merge_proposal(
        session, canonical=survivor, absorbed=absorbed, score=90.0, rationale="r"
    )
    # The shape of every merge event stored before 2026-10-07 (a test fixture may write the row;
    # application code never updates `event`).
    merged.source_id = merged.source_url = merged.retrieved_at = merged.licence_id = None
    session.flush()

    undone = merge_mod.unmerge_proposal(session, merged.id)

    assert_complete(undone)
    assert quartet_of(undone) == quartet_of(absorbed_link)


def test_a_key_based_organization_merge_and_its_unmerge_carry_a_full_quartet(session: Session) -> None:
    src = make_source(session, "src.q")
    org_a, org_b = make_org(session, "Acme Power LLC"), make_org(session, "ACME POWER, LLC")
    make_proposal(session, src, "Q1", sponsor=org_a)
    make_proposal(session, src, "Q2", sponsor=org_a)
    _, b_link = make_proposal(session, src, "Q3", sponsor=org_b)

    report = merge_mod.resolve_organizations(session, org_key)

    assert len(report.merge_events) == 1
    merged = report.merge_events[0]
    assert_complete(merged)
    # The absorbed spelling's own record, not the alias licence alone (the defect this replaces).
    assert quartet_of(merged) == quartet_of(b_link)
    undone = merge_mod.unmerge_organization(session, merged.id)
    assert_complete(undone)
    assert quartet_of(undone) == quartet_of(merged)


def test_an_organization_whose_proposal_was_merged_away_still_has_its_evidence(session: Session) -> None:
    eia, ercot = make_source(session, "src.eia", category="registry"), make_source(session, "src.ercot")
    org_a, org_b = make_org(session, "Bravo Solar LLC"), make_org(session, "BRAVO SOLAR, LLC")
    make_proposal(session, eia, "P1", sponsor=org_a)
    survivor, _ = make_proposal(session, eia, "P9", sponsor=org_a)
    absorbed, absorbed_link = make_proposal(session, ercot, "25INR0001", sponsor=org_b)
    merge_mod.merge_proposal(session, canonical=survivor, absorbed=absorbed, score=90.0, rationale="r")

    merged = merge_mod.merge_organization(session, canonical=org_a, absorbed=org_b, rationale="key")

    # org_b's only record now sits on another proposal; its merge event still names it.
    assert quartet_of(merged) == quartet_of(absorbed_link)


def test_a_suppression_carries_the_suppressed_records_quartet(session: Session) -> None:
    entry = SourceEntry.from_yaml(
        {
            "id": ICIS,
            "name": "ICIS-Air",
            "jurisdiction": "US",
            "category": "permit",
            "operator": "US EPA",
            "url": "https://echo.epa.gov/",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "weekly",
            "tier": 1,
            "license": "US federal public domain",
        }
    )
    src = upsert_licence_and_source(session, entry, "test")
    row = {
        "record_id": f"{ICIS}:VA1",
        "source_id": ICIS,
        "source_record_id": "VA1",
        "source_url": "https://echo.epa.gov/facility/VA1",
        "retrieved_at": "2026-09-29T05:00:00Z",
        "licence_id": "x",
        "kind": "load",
        "name_canonical": "OFFICE CAMPUS",
        "name_norm": "office campus",
        "sponsor_name": None,
        "sponsor_norm": None,
        "technology": "load",
        "technology_raw": "Data Center",
        "capacity_mw": None,
        "storage_mwh": None,
        "iso": None,
        "state": "VA",
        "county": "Fairfax",
        "county_norm": "fairfax",
        "lifecycle_state": "built",
        "status_raw": "Operating",
        "queue_date": None,
        "proposed_cod": None,
        "queue_id": None,
        "eia_plant_id": None,
        "eia_generator_id": None,
        "raw": json.dumps({"REGISTRY_ID": "110000000001"}),
    }
    load_dataframe(session, src, "proposal", pd.DataFrame([row]), None)
    session.flush()

    apply_suppressions(session, [Suppression(ICIS, "110000000001", "OFFICE CAMPUS", "office")])

    event = session.scalars(select(Event).where(Event.event_type == "unpublished")).one()
    link = session.scalars(select(ProposalSource).where(ProposalSource.source_record_id == "VA1")).one()
    assert_complete(event)
    assert quartet_of(event) == quartet_of(link)


# ------------------------------------------------------------------- restricted members never leak
def test_a_restricted_member_makes_the_event_restricted_and_off_every_public_surface(
    session: Session,
) -> None:
    open_src, gated = make_source(session, "src.open"), make_restricted(session, "src.gated")
    survivor, _ = make_proposal(session, open_src, "O1", retrieved_at=T_NEW)
    absorbed, gated_link = make_proposal(session, gated, "G1", retrieved_at=T_OLD)

    event = merge_mod.merge_proposal(
        session, canonical=survivor, absorbed=absorbed, score=90.0, rationale="r"
    )

    assert quartet_of(event) == quartet_of(gated_link)
    # The licence clause `services/api/visibility.py::event_visibility_filter` applies on every
    # non-admin surface (feeds, RSS, webhooks, alerts; the social worker checks the same licence):
    # the restricted URL is on an event no public reader is served.
    assert event.licence is not None
    assert event.licence.reuse_class not in publishable_reuse_classes(platform_posture())


def test_the_survivors_restricted_link_wins_over_an_open_absorbed_one(session: Session) -> None:
    open_src, gated = make_source(session, "src.open"), make_restricted(session, "src.gated")
    survivor, gated_link = make_proposal(session, gated, "G1")
    absorbed, _ = make_proposal(session, open_src, "O1")

    event = merge_mod.merge_proposal(
        session, canonical=survivor, absorbed=absorbed, score=90.0, rationale="r"
    )

    assert quartet_of(event) == quartet_of(gated_link)


def test_a_raw_withheld_licence_outranks_an_open_one_within_the_publishable_classes(session: Session) -> None:
    ercot, caiso = make_source(session, "src.ercot"), make_source(session, "src.caiso")
    caiso_licence = session.get(Licence, caiso.licence_id)
    assert caiso_licence is not None
    caiso_licence.reuse_class, caiso_licence.allows_raw_publication = "attribution", False
    survivor, caiso_link = make_proposal(session, caiso, "C1")
    absorbed, _ = make_proposal(session, ercot, "E1")

    event = merge_mod.merge_proposal(
        session, canonical=survivor, absorbed=absorbed, score=90.0, rationale="r"
    )

    assert quartet_of(event) == quartet_of(caiso_link)
    # Publishable, so its source_url is a source the public tier may link out to.
    assert caiso_licence.reuse_class in publishable_reuse_classes(platform_posture())


def test_an_explicit_quartet_is_used_whole_and_a_partial_one_is_refused(session: Session) -> None:
    curated, src = make_source(session, "curated.merges", category="registry"), make_source(session, "src.q")
    org_a, org_b = make_org(session, "Charlie Gas LLC"), make_org(session, "CHARLIE GAS")
    make_proposal(session, src, "Q1", sponsor=org_b)

    with pytest.raises(ValueError, match="all four"):
        merge_mod.merge_organization(
            session, canonical=org_a, absorbed=org_b, rationale="curated", source_url="https://sec.gov/x"
        )
    event = merge_mod.merge_organization(
        session,
        canonical=org_a,
        absorbed=org_b,
        rationale="curated",
        actor_type="user",
        source_id=curated.id,
        source_url="https://www.sec.gov/x",
        retrieved_at=T_NEW,
        licence_id=curated.licence_id,
    )
    assert quartet_of(event) == (curated.id, "https://www.sec.gov/x", T_NEW, curated.licence_id)
