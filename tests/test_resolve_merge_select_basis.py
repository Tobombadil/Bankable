"""services/resolve/merge.py carries the absorbed record's `select_basis` onto the survivor, and
unmerge takes back exactly what the merge added (lane I3, 2026-09-29).

`proposal.identifiers.select_basis` is `{source_id: basis}`, written by the loader (lane H6,
docs/25 §3.3). Before this, a merge left the survivor with its own entry only, so a Virginia DEQ
site merged with its ICIS-Air record said why it was a data centre for one source until the other
source loaded again.

Helpers are duplicated from `tests/test_resolve_store_unmerge.py` rather than imported, per this
repo's convention that test modules do not import one another.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.loader import SELECT_BASIS_KEY, load_dataframe, upsert_licence_and_source
from services.resolve import merge as merge_mod

UTC = dt.UTC
DEQ = "us.test.va_deq"
ICIS = "us.test.icis_air"


@pytest.fixture()
def session() -> Session:
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
            "category": "permit",
            "operator": "Test agency",
            "url": f"https://example.org/{id_}",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "weekly",
            "tier": 1,
            "license": "US federal public domain",
        }
    )
    return upsert_licence_and_source(session, entry, "test-manifest")


def make_proposal(
    session: Session, source: Source, *, source_record_id: str, identifiers: dict[str, Any]
) -> tuple[Proposal, ProposalSource]:
    proposal = Proposal(
        public_id="",
        slug="",
        kind="load",
        name_canonical=f"Data Center {source_record_id}",
        jurisdiction="US-VA",
        lifecycle_state="built",
        identifiers=identifiers,
        publish_state="public",
        min_reuse_class=source.licence.reuse_class,
        source_count=1,
    )
    session.add(proposal)
    session.flush()
    proposal.public_id = public_id("prop", proposal.id)
    proposal.slug = f"{slugify(proposal.name_canonical)}-{proposal.public_id[-6:].lower()}"
    retrieved_at = dt.datetime(2026, 9, 29, tzinfo=UTC)
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


def basis(proposal: Proposal) -> dict[str, str]:
    return dict((proposal.identifiers or {}).get(SELECT_BASIS_KEY) or {})


def test_merge_carries_the_absorbed_basis_onto_the_survivor(session: Session) -> None:
    deq, icis = make_source(session, DEQ), make_source(session, ICIS)
    survivor, _ = make_proposal(
        session, deq, source_record_id="V1", identifiers={SELECT_BASIS_KEY: {DEQ: "deq_flag"}}
    )
    absorbed, _ = make_proposal(
        session,
        icis,
        source_record_id="I1",
        identifiers={"eia_plant_id": "999", SELECT_BASIS_KEY: {ICIS: "naics_518210"}},
    )
    merge_mod.merge_proposal(session, canonical=survivor, absorbed=absorbed, score=100.0, rationale="d3")
    session.flush()
    session.refresh(survivor)
    assert basis(survivor) == {DEQ: "deq_flag", ICIS: "naics_518210"}
    # Only the basis is carried; the absorbed row's other identifiers are its own.
    assert "eia_plant_id" not in survivor.identifiers
    # The absorbed row itself is not edited by the merge.
    assert basis(absorbed) == {ICIS: "naics_518210"}


def test_the_survivors_own_entry_wins_on_conflict(session: Session) -> None:
    icis = make_source(session, ICIS)
    survivor, _ = make_proposal(
        session, icis, source_record_id="I1", identifiers={SELECT_BASIS_KEY: {ICIS: "name"}}
    )
    absorbed, _ = make_proposal(
        session, icis, source_record_id="I2", identifiers={SELECT_BASIS_KEY: {ICIS: "naics_518210"}}
    )
    event = merge_mod.merge_proposal(
        session, canonical=survivor, absorbed=absorbed, score=90.0, rationale="x"
    )
    assert basis(survivor) == {ICIS: "name"}
    merge_mod.unmerge_proposal(session, event.id)
    assert basis(survivor) == {ICIS: "name"}  # nothing was added, so nothing is taken away


def test_a_survivor_without_identifiers_gains_the_basis(session: Session) -> None:
    deq, icis = make_source(session, DEQ), make_source(session, ICIS)
    survivor, _ = make_proposal(session, deq, source_record_id="V1", identifiers={})
    absorbed, _ = make_proposal(
        session, icis, source_record_id="I1", identifiers={SELECT_BASIS_KEY: {ICIS: "name"}}
    )
    merge_mod.merge_proposal(session, canonical=survivor, absorbed=absorbed, score=100.0, rationale="d3")
    assert survivor.identifiers == {SELECT_BASIS_KEY: {ICIS: "name"}}


def test_unmerge_restores_both_rows_exactly(session: Session) -> None:
    deq, icis = make_source(session, DEQ), make_source(session, ICIS)
    survivor, _ = make_proposal(
        session,
        deq,
        source_record_id="V1",
        identifiers={"queue_ids": [{"iso": "", "id": "V1"}], SELECT_BASIS_KEY: {DEQ: "deq_flag"}},
    )
    absorbed, _ = make_proposal(
        session, icis, source_record_id="I1", identifiers={SELECT_BASIS_KEY: {ICIS: "name"}}
    )
    survivor_before = json.loads(json.dumps(survivor.identifiers))
    absorbed_before = merge_mod.serialize_row(absorbed)

    event = merge_mod.merge_proposal(
        session, canonical=survivor, absorbed=absorbed, score=100.0, rationale="d3"
    )
    session.flush()
    assert basis(survivor) == {DEQ: "deq_flag", ICIS: "name"}
    merge_mod.unmerge_proposal(session, event.id)
    session.flush()

    # No refresh: SQLite drops timezones on read-back, so the existing round-trip test in
    # tests/test_resolve_store_unmerge.py compares the in-session rows too.
    assert survivor.identifiers == survivor_before
    assert merge_mod.serialize_row(absorbed) == absorbed_before


def test_unmerging_one_absorbed_record_keeps_the_others_basis(session: Session) -> None:
    deq, icis = make_source(session, DEQ), make_source(session, ICIS)
    other = make_source(session, "us.test.other")
    survivor, _ = make_proposal(
        session, deq, source_record_id="V1", identifiers={SELECT_BASIS_KEY: {DEQ: "deq_flag"}}
    )
    a, _ = make_proposal(session, icis, source_record_id="I1", identifiers={SELECT_BASIS_KEY: {ICIS: "name"}})
    b, _ = make_proposal(
        session, other, source_record_id="O1", identifiers={SELECT_BASIS_KEY: {other.id: "x"}}
    )
    ev_a = merge_mod.merge_proposal(session, canonical=survivor, absorbed=a, score=90.0, rationale="a")
    merge_mod.merge_proposal(session, canonical=survivor, absorbed=b, score=90.0, rationale="b")
    assert basis(survivor) == {DEQ: "deq_flag", ICIS: "name", other.id: "x"}

    merge_mod.unmerge_proposal(session, ev_a.id)
    assert basis(survivor) == {DEQ: "deq_flag", other.id: "x"}


def test_unmerge_removes_the_entry_even_after_its_source_reloaded(session: Session) -> None:
    """Between merge and unmerge the absorbed record's source loads again and changes its basis on
    the survivor (the link now points there). The entry still belongs to the absorbed record's
    link, which unmerge moves back, so it leaves the survivor whatever its value now is."""
    deq, icis = make_source(session, DEQ), make_source(session, ICIS)
    survivor, _ = make_proposal(
        session, deq, source_record_id="V1", identifiers={SELECT_BASIS_KEY: {DEQ: "deq_flag"}}
    )
    absorbed, _ = make_proposal(
        session, icis, source_record_id="I1", identifiers={SELECT_BASIS_KEY: {ICIS: "name"}}
    )
    event = merge_mod.merge_proposal(
        session, canonical=survivor, absorbed=absorbed, score=100.0, rationale="d3"
    )
    survivor.identifiers = {SELECT_BASIS_KEY: {DEQ: "deq_flag", ICIS: "naics_518210"}}
    session.flush()

    merge_mod.unmerge_proposal(session, event.id)
    assert survivor.identifiers == {SELECT_BASIS_KEY: {DEQ: "deq_flag"}}


def test_merge_event_records_what_it_added(session: Session) -> None:
    deq, icis = make_source(session, DEQ), make_source(session, ICIS)
    survivor, _ = make_proposal(
        session, deq, source_record_id="V1", identifiers={SELECT_BASIS_KEY: {DEQ: "deq_flag"}}
    )
    absorbed, _ = make_proposal(
        session, icis, source_record_id="I1", identifiers={SELECT_BASIS_KEY: {ICIS: "name"}}
    )
    event = merge_mod.merge_proposal(
        session, canonical=survivor, absorbed=absorbed, score=100.0, rationale="d3"
    )
    assert event.after["surviving"][SELECT_BASIS_KEY + "_added"] == {ICIS: "name"}
    assert "identifiers" in event.changed_keys


def test_a_merge_with_no_basis_changes_no_identifiers(session: Session) -> None:
    deq, icis = make_source(session, DEQ), make_source(session, ICIS)
    survivor, _ = make_proposal(session, deq, source_record_id="V1", identifiers={"eia_plant_id": "1"})
    absorbed, _ = make_proposal(session, icis, source_record_id="I1", identifiers={"eia_plant_id": "2"})
    event = merge_mod.merge_proposal(
        session, canonical=survivor, absorbed=absorbed, score=100.0, rationale="d3"
    )
    assert survivor.identifiers == {"eia_plant_id": "1"}
    assert "identifiers" not in event.changed_keys
    assert SELECT_BASIS_KEY + "_added" not in event.after["surviving"]


# ------------------------------------------------------------------ with the loader (lane H6)
def _row(source_id: str, record_id: str, basis_token: str) -> dict[str, Any]:
    return {
        "record_id": f"{source_id}:{record_id}",
        "source_id": source_id,
        "source_record_id": record_id,
        "source_url": f"https://example.org/air/{record_id}",
        "retrieved_at": "2026-09-29T05:00:00Z",
        "licence_id": f"{source_id}#irrelevant",
        "kind": "load",
        "name_canonical": "Example Data Center",
        "name_norm": "example data center",
        "sponsor_name": None,
        "sponsor_norm": None,
        "technology": "load",
        "technology_raw": "Data Center",
        "capacity_mw": None,
        "storage_mwh": None,
        "iso": None,
        "state": "VA",
        "county": "Loudoun",
        "county_norm": "loudoun",
        "lifecycle_state": "built",
        "status_raw": "Operating",
        "queue_date": None,
        "proposed_cod": None,
        "queue_id": None,
        "eia_plant_id": None,
        "eia_generator_id": None,
        "raw": json.dumps({"select_basis": basis_token}),
    }


def _load(session: Session, source: Source, rows: list[dict[str, Any]]) -> None:
    load_dataframe(session, source, "proposal", pd.DataFrame(rows), None)
    session.commit()


def test_loader_reloads_after_a_real_merge_keep_both_reasons(session: Session) -> None:
    """The end-to-end path: both sources load, the resolver's merge runs, and then either source
    loads again. The survivor carries both reasons at every step, not only after the second load."""
    deq, icis = make_source(session, DEQ), make_source(session, ICIS)
    _load(session, deq, [_row(DEQ, "V1", "deq_flag")])
    _load(session, icis, [_row(ICIS, "I1", "name")])
    survivor, absorbed = session.query(Proposal).order_by(Proposal.first_seen).all()

    merge_mod.merge_proposal(session, canonical=survivor, absorbed=absorbed, score=100.0, rationale="d3")
    session.commit()
    session.refresh(survivor)
    assert basis(survivor) == {DEQ: "deq_flag", ICIS: "name"}

    _load(session, deq, [_row(DEQ, "V1", "deq_flag")])
    session.refresh(survivor)
    assert basis(survivor) == {DEQ: "deq_flag", ICIS: "name"}

    _load(session, icis, [_row(ICIS, "I1", "naics_518210")])  # a changed basis at its own source
    session.refresh(survivor)
    assert basis(survivor) == {DEQ: "deq_flag", ICIS: "naics_518210"}
