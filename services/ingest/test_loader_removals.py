"""A row that leaves its source's file is not a withdrawal (docs/51 §2.7 item 1, 2026-10-10).

Before, `DIFF_EVENT_TYPE_MAP` turned every `removed` diff row into a public `withdrawn` event, so an
EIA-860M unit that entered operation, a grants.gov notice that closed or a NESO row that was re-keyed
reached the feed, alerts, webhooks and social drafts as a withdrawal or a cancellation. Now the
connector class declares what a removal means at its source (`Connector.removal_meaning`), the loader
reads it the way it reads `kind`, and only a declared `withdrawn` is published; everything else is a
`removed_from_source` event that no public or paid surface serves
(`tests/test_removed_from_source_is_never_published.py` walks every surface).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
from collections.abc import Iterator
from typing import Any, ClassVar

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import connector_for, snapshot
from pipeline.connectors.base import REMOVAL_MEANINGS
from pipeline.connectors.registry import RegistrationError, Registry, SourceEntry
from services.db.models import (
    NON_PUBLIC_EVENT_TYPES,
    REMOVED_FROM_SOURCE_EVENT_TYPE,
    Event,
    Opportunity,
    OpportunitySource,
    Proposal,
    ProposalSource,
)
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest import loader
from services.ingest.loader import (
    DIFF_EVENT_TYPE_MAP,
    connector_removal_meaning,
    load_dataframe,
    removal_event_type,
    upsert_licence_and_source,
)
from services.ingest.test_loader import open_source_entry, sample_proposal_row

SRC = "us.test.open_queue"
GONE_AT = "2026-09-13T06:00:00Z"


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _removed(record_id: str, before: str, at: str = GONE_AT, source_id: str = SRC) -> dict[str, Any]:
    return {
        "event_type": "removed",
        "record_id": f"{source_id}:{record_id}",
        "source_id": source_id,
        "field": "lifecycle_state",
        "before": before,
        "after": None,
        "observed_at": at,
    }


def _two_then_one(session: Session, **kw: Any) -> tuple[loader.LoadResult, Proposal, ProposalSource]:
    """Load Q1 and Q2, then a run where Q2 has left the file and the diff says `removed`."""
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    q1, q2 = sample_proposal_row("Q1"), sample_proposal_row("Q2", lifecycle_state="studied")
    load_dataframe(session, src, "proposal", pd.DataFrame([q1, q2]), None)
    result = load_dataframe(
        session, src, "proposal", pd.DataFrame([q1]), pd.DataFrame([_removed("Q2", "studied")]), **kw
    )
    session.flush()
    link = session.scalar(select(ProposalSource).where(ProposalSource.source_record_id == "Q2"))
    assert link is not None
    proposal = session.get(Proposal, link.proposal_id)
    assert proposal is not None
    return result, proposal, link


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


# ------------------------------------------------------------------------------- the event written
def test_a_removal_of_unknown_meaning_is_a_non_public_removed_from_source_event(session: Session) -> None:
    result, proposal, link = _two_then_one(session)

    event = session.scalars(select(Event)).one()
    assert event.event_type == REMOVED_FROM_SOURCE_EVENT_TYPE == "removed_from_source"
    assert event.event_type in NON_PUBLIC_EVENT_TYPES
    assert event.published_at is None and event.public_at is None, "never published"
    assert event.before == {"lifecycle_state": "studied"}
    assert event.after == {"removal_meaning": "unknown"}
    assert event.reason and "does not say this means withdrawal" in event.reason
    # The provenance quartet is still there: admin reads show where and when it was seen.
    assert event.source_id == SRC and event.source_url and event.retrieved_at and event.licence_id
    assert result.events_created == 1 and result.removals_unpublished == 1

    # `gone_at` behaves exactly as before, and the record keeps the state the source last stated.
    assert _aware(link.gone_at) == dt.datetime(2026, 9, 13, 6, tzinfo=dt.UTC)
    assert proposal.lifecycle_state == "studied"


def test_the_default_for_a_direct_caller_is_unknown_not_withdrawn(session: Session) -> None:
    """`load_dataframe` without a meaning (bench, dev-data and resolver callers) fails closed."""
    _two_then_one(session)
    assert not session.scalars(select(Event).where(Event.event_type == "withdrawn")).all()


def test_a_source_declared_withdrawn_still_publishes_withdrawn(session: Session) -> None:
    result, proposal, link = _two_then_one(session, removal_meaning="withdrawn")

    event = session.scalars(select(Event)).one()
    assert event.event_type == "withdrawn"
    assert event.published_at is not None and event.public_at == event.published_at
    assert event.after is None and event.reason is None, "the pre-2026-10-10 payload, unchanged"
    assert result.removals_unpublished == 0
    assert _aware(link.gone_at) == dt.datetime(2026, 9, 13, 6, tzinfo=dt.UTC)
    assert proposal.lifecycle_state == "studied", "a removal never moved the lifecycle; it still does not"


@pytest.mark.parametrize("meaning", ["completed", "closed", "unknown"])
def test_every_other_meaning_is_stored_with_its_meaning(session: Session, meaning: str) -> None:
    _two_then_one(session, removal_meaning=meaning)
    event = session.scalars(select(Event)).one()
    assert event.event_type == REMOVED_FROM_SOURCE_EVENT_TYPE
    assert event.after == {"removal_meaning": meaning}
    assert event.published_at is None and event.public_at is None


def test_a_closed_notice_on_an_opportunity_source(session: Session) -> None:
    """grants.gov's case: the notice leaves the open/forecast search when it closes."""
    entry = SourceEntry.from_yaml(
        {
            "id": "us.test.grants",
            "name": "Test Grants",
            "jurisdiction": "US",
            "category": "funding",
            "operator": "Test",
            "url": "https://example.org/grants",
            "access": "api",
            "reuse": "open",
            "cadence": "daily",
            "tier": 1,
            "license": "US federal public domain",
        }
    )
    src = upsert_licence_and_source(session, entry, "v")

    def notice(key: str) -> dict[str, Any]:
        return {
            "record_id": f"us.test.grants:{key}",
            "source_id": "us.test.grants",
            "source_record_id": key,
            "source_url": f"https://example.org/grants#{key}",
            "retrieved_at": "2026-09-12T05:00:00Z",
            "licence_id": "us.test.grants#irrelevant",
            "kind": "foa",
            "title": f"Notice {key}",
            "jurisdiction": "US",
            "status": "open",
            "due_at": "2027-01-01T00:00:00Z",
            "raw": json.dumps({"id": key}),
        }

    load_dataframe(session, src, "opportunity", pd.DataFrame([notice("N1"), notice("N2")]), None)
    gone = pd.DataFrame([{**_removed("N2", "open", source_id="us.test.grants"), "field": "status"}])
    result = load_dataframe(
        session, src, "opportunity", pd.DataFrame([notice("N1")]), gone, removal_meaning="closed"
    )
    session.flush()
    event = session.scalars(select(Event)).one()
    assert (event.subject_type, event.event_type) == ("opportunity", REMOVED_FROM_SOURCE_EVENT_TYPE)
    assert event.after == {"removal_meaning": "closed"} and event.published_at is None
    assert result.removals_unpublished == 1
    link = session.scalar(select(OpportunitySource).where(OpportunitySource.source_record_id == "N2"))
    assert link is not None and link.gone_at is not None
    opportunity = session.get(Opportunity, link.opportunity_id)
    assert opportunity is not None and opportunity.status == "open", "the status is not inferred"


def test_reloading_the_same_removal_is_idempotent(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    q1, q2 = sample_proposal_row("Q1"), sample_proposal_row("Q2")
    load_dataframe(session, src, "proposal", pd.DataFrame([q1, q2]), None)
    gone = pd.DataFrame([_removed("Q2", "filed")])
    first = load_dataframe(session, src, "proposal", pd.DataFrame([q1]), gone)
    again = load_dataframe(session, src, "proposal", pd.DataFrame([q1]), gone)
    assert first.events_created == 1 and again.events_created == 0
    assert again.events_skipped_idempotent == 1


def test_an_off_vocabulary_meaning_is_refused(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    with pytest.raises(ValueError, match="removal_meaning"):
        load_dataframe(
            session,
            src,
            "proposal",
            pd.DataFrame([sample_proposal_row("Q1")]),
            None,
            removal_meaning="gone",  # type: ignore[arg-type]
        )


def test_the_type_map_no_longer_reads_removed_as_withdrawn() -> None:
    assert DIFF_EVENT_TYPE_MAP["removed"] == REMOVED_FROM_SOURCE_EVENT_TYPE
    assert DIFF_EVENT_TYPE_MAP["withdrawn"] == "withdrawn", "a lifecycle move to withdrawn is still news"
    assert {m: removal_event_type(m) for m in REMOVAL_MEANINGS} == {
        "withdrawn": "withdrawn",
        "completed": REMOVED_FROM_SOURCE_EVENT_TYPE,
        "closed": REMOVED_FROM_SOURCE_EVENT_TYPE,
        "unknown": REMOVED_FROM_SOURCE_EVENT_TYPE,
    }


# ---------------------------------------------------------------- the declaration and how it is read
#: What each implemented connector declares, written out so a change is a deliberate edit here.
#: `withdrawn` publishes; none of these sources says a disappearance means that (evidence on each
#: declaration: ERCOT's report also drops inactive and re-numbered projects; an EIA-860M unit leaves
#: the Planned sheet on operation or on cancellation; grants.gov is searched for open notices only).
DECLARED_REMOVAL_MEANINGS = {"us.grants_gov.search2": "closed"}


def test_the_declared_removal_meanings_are_pinned() -> None:
    registry = Registry()
    declared: dict[str, str] = {}
    for source_id in registry.ids():
        try:
            cls = registry.connector_class(source_id)
        except RegistrationError:
            continue
        assert cls.removal_meaning in REMOVAL_MEANINGS, source_id
        if cls.removal_meaning != "unknown":
            declared[source_id] = cls.removal_meaning
    assert declared == DECLARED_REMOVAL_MEANINGS
    assert "withdrawn" not in declared.values()


def test_connector_removal_meaning_reads_the_connector_class() -> None:
    registry = Registry()
    assert connector_removal_meaning(registry, "us.grants_gov.search2") == "closed"
    assert connector_removal_meaning(registry, "us.iso.ercot.gen_queue") == "unknown"
    assert connector_removal_meaning(registry, "us.eia.860m") == "unknown"
    assert connector_removal_meaning(registry, "gb.neso.tec_register") == "unknown"
    # no connector class (a fixture source, a manifest-only entry): unknown
    assert connector_removal_meaning(registry, "us.test.not_in_the_manifest") == "unknown"


class _Stub:
    removal_meaning: ClassVar[str] = "withdrawn"


class _StubRegistry:
    def __init__(self, cls: type) -> None:
        self.cls = cls

    def connector_class(self, source_id: str) -> type:
        return self.cls


def test_a_declared_withdrawn_is_read_and_an_off_vocabulary_value_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert connector_removal_meaning(_StubRegistry(_Stub), "s") == "withdrawn"  # type: ignore[arg-type]

    class Typo(_Stub):
        removal_meaning: ClassVar[str] = "withdrew"

    with caplog.at_level(logging.WARNING, logger="services.ingest.loader"):
        assert connector_removal_meaning(_StubRegistry(Typo), "s") == "unknown"  # type: ignore[arg-type]
    assert "withdrew" in caplog.text


# ------------------------------------------------------------------- through load_from_files
TS1, TS2 = "20260912T180000Z", "20260913T180000Z"
ERCOT = "us.iso.ercot.gen_queue"


def _write_ercot_runs(root: pathlib.Path) -> str:
    """Two ERCOT runs where the second has lost one row, as `python -m pipeline.connectors run`
    writes them (normalised frame plus events parquet). Returns the removed record id."""
    connector = connector_for(ERCOT)
    raw = snapshot(
        "ercot_gis_report.xlsx",
        "https://www.ercot.com/misapp/GetReports.do?reportTypeId=15933",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    frame = connector.normalize(connector.parse(raw), raw)
    gone = frame.iloc[-1]
    out = root / "normalized" / ERCOT
    out.mkdir(parents=True)
    frame.to_parquet(out / f"{TS1}.parquet", index=False)
    frame.iloc[:-1].to_parquet(out / f"{TS2}.parquet", index=False)
    events = root / "events" / ERCOT
    events.mkdir(parents=True)
    row = {
        "event_type": "removed",
        "record_id": str(gone["record_id"]),
        "source_id": ERCOT,
        "field": "lifecycle_state",
        "before": str(gone["lifecycle_state"]),
        "after": None,
        "observed_at": "2026-09-13T18:00:00Z",
    }
    pd.DataFrame([row]).to_parquet(events / f"{TS2}.parquet", index=False)
    return str(gone["record_id"])


def test_load_from_files_uses_the_connector_declaration(
    tmp_path: pathlib.Path, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_ercot_runs(tmp_path)
    registry = Registry()
    loader.load_from_files(session, ERCOT, TS1, data_root=tmp_path, registry=registry)
    loader.load_from_files(session, ERCOT, TS2, data_root=tmp_path, registry=registry)
    session.flush()
    event = session.scalars(select(Event)).one()
    assert event.event_type == REMOVED_FROM_SOURCE_EVENT_TYPE and event.published_at is None
    assert event.after == {"removal_meaning": "unknown"}


def test_load_from_files_publishes_withdrawn_only_for_a_declared_withdrawal(
    tmp_path: pathlib.Path, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_ercot_runs(tmp_path)
    registry = Registry()
    monkeypatch.setattr(registry.connector_class(ERCOT), "removal_meaning", "withdrawn")
    loader.load_from_files(session, ERCOT, TS1, data_root=tmp_path, registry=registry)
    loader.load_from_files(session, ERCOT, TS2, data_root=tmp_path, registry=registry)
    session.flush()
    event = session.scalars(select(Event)).one()
    assert event.event_type == "withdrawn" and event.published_at is not None
