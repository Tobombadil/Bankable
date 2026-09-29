"""The loader carries a selecting connector's `select_basis` to `proposal.identifiers` (lane H6,
2026-09-29; docs/25 §3.3, §3.7), keyed by source, so the public page can say why a site counts
as a data centre without reading the admin-only `raw` payload.

Fixtures are duplicated from `services/ingest/test_loader.py` rather than imported, per this
repo's convention that test modules do not import one another.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Proposal, ProposalSource
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import SELECT_BASIS_KEY, load_dataframe, upsert_licence_and_source

ICIS = "us.test.icis_air"
DEQ = "us.test.va_deq"


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _entry(id_: str) -> SourceEntry:
    return SourceEntry.from_yaml(
        {
            "id": id_,
            "name": f"Test {id_}",
            "jurisdiction": "US",
            "category": "permit",
            "operator": "Test agency",
            "url": "https://example.org/air",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "weekly",
            "tier": 1,
            "license": "US federal public domain",
        }
    )


def _row(source_id: str, record_id: str, raw: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
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
        "raw": json.dumps(raw),
    }
    row.update(overrides)
    return row


def _load(session: Session, source_id: str, rows: list[dict[str, Any]]) -> None:
    source = upsert_licence_and_source(session, _entry(source_id), "test")
    load_dataframe(session, source, "proposal", pd.DataFrame(rows), None)
    session.commit()


def _only_proposal(session: Session) -> Proposal:
    proposals = session.scalars(select(Proposal)).all()
    assert len(proposals) == 1
    return proposals[0]


def test_a_row_with_a_basis_stores_it_under_its_source(session: Session) -> None:
    _load(session, ICIS, [_row(ICIS, "A1", {"FACILITY_NAME": "X", "select_basis": "naics_518210"})])
    assert _only_proposal(session).identifiers == {SELECT_BASIS_KEY: {ICIS: "naics_518210"}}


def test_a_row_without_a_basis_stores_no_key(session: Session) -> None:
    _load(session, ICIS, [_row(ICIS, "A1", {"FACILITY_NAME": "X"})])
    assert SELECT_BASIS_KEY not in _only_proposal(session).identifiers


def test_the_basis_sits_beside_the_existing_identifiers(session: Session) -> None:
    _load(session, ICIS, [_row(ICIS, "A1", {"select_basis": "name"}, queue_id="Q9", iso="ERCOT")])
    assert _only_proposal(session).identifiers == {
        "queue_ids": [{"iso": "ERCOT", "id": "Q9"}],
        SELECT_BASIS_KEY: {ICIS: "name"},
    }


def test_a_changed_basis_at_its_own_source_is_followed(session: Session) -> None:
    _load(session, ICIS, [_row(ICIS, "A1", {"select_basis": "naics_518210"})])
    _load(session, ICIS, [_row(ICIS, "A1", {"select_basis": "name"})])
    assert _only_proposal(session).identifiers[SELECT_BASIS_KEY] == {ICIS: "name"}


def test_a_proposal_fed_by_two_sources_keeps_both_reasons_across_reloads(session: Session) -> None:
    """The DEQ/ICIS-Air overlap: after a merge both links point at one proposal, and each load
    replaces `identifiers` with its own row's. Neither source's reason may erase the other's."""
    _load(session, DEQ, [_row(DEQ, "V1", {"select_basis": "deq_flag"})])
    _load(session, ICIS, [_row(ICIS, "I1", {"select_basis": "name"})])
    survivor, absorbed = session.scalars(select(Proposal).order_by(Proposal.first_seen)).all()
    # What `services/resolve/merge.py::merge_proposal` does to the links, reduced to the move.
    for link in session.scalars(select(ProposalSource).where(ProposalSource.proposal_id == absorbed.id)):
        link.proposal_id = survivor.id
    absorbed.merged_into_id = survivor.id
    session.commit()

    _load(session, ICIS, [_row(ICIS, "I1", {"select_basis": "name"})])
    _load(session, DEQ, [_row(DEQ, "V1", {"select_basis": "deq_flag"})])
    session.refresh(survivor)
    assert survivor.identifiers[SELECT_BASIS_KEY] == {DEQ: "deq_flag", ICIS: "name"}

    _load(session, ICIS, [_row(ICIS, "I1", {"select_basis": "naics_518210"})])
    session.refresh(survivor)
    assert survivor.identifiers[SELECT_BASIS_KEY] == {DEQ: "deq_flag", ICIS: "naics_518210"}


def test_reloading_the_same_frame_is_idempotent(session: Session) -> None:
    rows = [_row(ICIS, "A1", {"select_basis": "name"})]
    _load(session, ICIS, rows)
    before = dict(_only_proposal(session).identifiers)
    _load(session, ICIS, rows)
    assert _only_proposal(session).identifiers == before
