"""Milestones (queue date, study phase, IA date, withdrawn date, actual COD) reach each link's
`normalised` row and the record's `field_provenance` (docs/22 §3.2; interconnection analyst review
2026-10-07, §5 item 3)."""

from __future__ import annotations

import json

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Proposal, ProposalSource
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.ingest.test_loader import sample_proposal_row

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
CAISO = "us.iso.caiso.gen_queue"


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _caiso(session: Session):
    entry = SourceEntry.from_yaml(
        {
            "id": CAISO,
            "name": "CAISO",
            "jurisdiction": "US-CA",
            "category": "generation_queue",
            "operator": "CAISO",
            "url": "https://example.org/caiso",
            "access": "bulk_file",
            "reuse": "open",
            "publication": "raw_ok",
            "cadence": "weekly",
            "tier": 1,
            "license": "test",
        }
    )
    return upsert_licence_and_source(session, entry, "v")


def _row(srid: str, **over: object) -> dict[str, object]:
    row = sample_proposal_row(srid)
    row.update(source_id=CAISO, record_id=f"{CAISO}:{srid}", **over)
    return row


def _link(session: Session, srid: str) -> ProposalSource:
    link = session.scalar(select(ProposalSource).where(ProposalSource.source_record_id == srid))
    assert link is not None
    return link


def test_milestone_columns_reach_the_link_and_the_records_provenance(session: Session) -> None:
    src = _caiso(session)
    frame = pd.DataFrame(
        [
            _row(
                "751",
                lifecycle_state="withdrawn",
                queue_date=pd.Timestamp("2009-01-15"),
                study_phase="Serial LGIP",
                withdrawn_date=pd.Timestamp("2013-03-20"),
                ia_date=pd.NaT,
                actual_cod=pd.NaT,
            )
        ]
    )
    load_dataframe(session, src, "proposal", frame, None)
    link = _link(session, "751")
    assert link.normalised["withdrawn_date"] == "2013-03-20"
    assert link.normalised["study_phase"] == "Serial LGIP"
    assert "ia_date" not in link.normalised and "actual_cod" not in link.normalised
    record = session.get(Proposal, link.proposal_id)
    assert record is not None
    entry = record.field_provenance["withdrawn_date"]
    assert entry["value"] == "2013-03-20" and entry["source_id"] == CAISO and entry["licence_id"]


def test_a_frame_written_before_the_columns_existed_is_read_from_raw(session: Session) -> None:
    src = _caiso(session)
    raw = {
        "Queue Date": "2006-02-01T08:00:00",
        "Study Process": "Serial LGIP",
        "Actual Completion Date": "2015-06-25T00:00:00",
    }
    load_dataframe(
        session,
        src,
        "proposal",
        pd.DataFrame([_row("188", lifecycle_state="built", raw=json.dumps(raw))]),
        None,
    )
    link = _link(session, "188")
    assert link.normalised["actual_cod"] == "2015-06-25"
    assert link.normalised["study_phase"] == "Serial LGIP"
    record = session.get(Proposal, link.proposal_id)
    assert record is not None and record.field_provenance["actual_cod"]["value"] == "2015-06-25"
