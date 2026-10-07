"""An opportunity past its deadline is served `closed` whether or not its source has run since
(docs/21 §7.2, docs/22 §8.2; audit 2026-10-07 DATA-14 / UX-2): the load closes what it writes, an
hourly sweep closes the rest, neither writes an event, an undated notice keeps its stated status and
an operator's override stands."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.opportunity import DEADLINE_PASSED_RULE
from pipeline.connectors.registry import SourceEntry
from services.db.models import Event, Opportunity, OpportunitySource
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.ingest.opportunity_status import close_past_deadline, served_status

UTC = dt.UTC
SOURCE_ID = "us.test.tenders"
FETCHED = dt.datetime(2026, 9, 13, 5, tzinfo=UTC)


@pytest.fixture()
def factory() -> Any:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def session(factory: Any) -> Iterator[Session]:
    with factory() as s:
        yield s


def _entry() -> SourceEntry:
    return SourceEntry.from_yaml(
        {
            "id": SOURCE_ID,
            "name": "Test Tenders",
            "jurisdiction": "US",
            "category": "procurement",
            "operator": "Test",
            "url": "https://example.org/tenders",
            "access": "api",
            "reuse": "open",
            "cadence": "daily",
            "tier": 1,
            "license": "US federal public domain",
        }
    )


def _notice(key: str, due: str | None, status: str = "open") -> dict[str, Any]:
    return {
        "record_id": f"{SOURCE_ID}:{key}",
        "source_id": SOURCE_ID,
        "source_record_id": key,
        "source_url": f"https://example.org/tenders#{key}",
        "retrieved_at": FETCHED.isoformat(),
        "licence_id": f"{SOURCE_ID}#irrelevant",
        "kind": "tender",
        "title": f"Notice {key}",
        "jurisdiction": "US-AZ",
        "status": status,
        "due_at": due,
        "raw": json.dumps({"id": key}),
    }


def _by_title(session: Session) -> dict[str, Opportunity]:
    session.expire_all()
    return {o.title: o for o in session.scalars(select(Opportunity))}


def test_the_rule_closes_only_an_open_notice_whose_deadline_has_passed() -> None:
    now = dt.datetime(2026, 10, 7, tzinfo=UTC)
    assert served_status("open", dt.datetime(2026, 9, 14, tzinfo=UTC), now) == "closed"
    assert served_status("open", dt.datetime(2026, 10, 8, tzinfo=UTC), now) == "open"
    assert served_status("open", None, now) == "open"  # undated: the source's word stands
    assert served_status("open", dt.datetime(2026, 9, 14, tzinfo=UTC).replace(tzinfo=None), now) == "closed"
    assert served_status("awarded", dt.datetime(2026, 9, 14, tzinfo=UTC), now) == "awarded"
    assert served_status(None, None, now) == "unknown"


def test_a_load_after_the_deadline_serves_the_notice_closed_and_keeps_what_the_source_said(
    session: Session,
) -> None:
    """A frame fetched 2026-09-13 and loaded today: before, `Due 2026-09-14` was served `open`
    (the runner judges deadlines at the fetch time, and the source had not run since)."""
    src = upsert_licence_and_source(session, _entry(), "v")
    frame = pd.DataFrame(
        [
            _notice("PAST", "2026-09-14T12:00:00Z"),
            _notice("FUTURE", "2099-01-01T00:00:00Z"),
            _notice("UNDATED", None),
        ]
    )
    result = load_dataframe(session, src, "opportunity", frame, None)
    session.commit()

    assert result.deadline_closed == 1
    served = _by_title(session)
    assert served["Notice PAST"].status == "closed"
    assert served["Notice PAST"].field_provenance["status"]["rule"] == DEADLINE_PASSED_RULE
    assert served["Notice PAST"].field_provenance["status"]["source_id"] == SOURCE_ID
    assert served["Notice FUTURE"].status == "open"
    assert served["Notice UNDATED"].status == "open" and served["Notice UNDATED"].due_at is None
    link = session.scalar(select(OpportunitySource).where(OpportunitySource.source_record_id == "PAST"))
    assert link is not None and link.normalised["status"] == "open", "the source's own statement"
    assert session.scalar(select(Event)) is None, "the source's next run publishes the change"

    # a reload of the same frame is a no-op on the served record
    stamp = served["Notice PAST"].last_changed
    again = load_dataframe(session, src, "opportunity", frame, None)
    session.commit()
    assert again.deadline_closed == 1  # written back from the frame, closed again in the same load
    assert again.records_changed == 0
    assert _by_title(session)["Notice PAST"].last_changed == stamp


def test_the_sweep_closes_notices_whose_deadline_passed_since_their_last_load(session: Session) -> None:
    src = upsert_licence_and_source(session, _entry(), "v")
    frame = pd.DataFrame(
        [
            _notice("SOON", "2099-01-01T00:00:00Z"),
            _notice("LATER", "2099-06-01T00:00:00Z"),
            _notice("PINNED", "2099-01-01T00:00:00Z"),
            _notice("UNDATED", None),
        ]
    )
    load_dataframe(session, src, "opportunity", frame, None)
    session.commit()
    pinned = _by_title(session)["Notice PINNED"]
    pinned.overrides = {"status": {"value": "open", "by": "usr_OPERATOR"}}
    session.commit()

    at = dt.datetime(2099, 2, 1, tzinfo=UTC)
    report = close_past_deadline(session, at)
    session.commit()
    assert report.to_dict() == {"closed": 1, "skipped_override": 1, "by_source": {SOURCE_ID: 1}}
    served = _by_title(session)
    assert served["Notice SOON"].status == "closed"
    assert served["Notice SOON"].last_changed.replace(tzinfo=UTC) == at
    assert served["Notice LATER"].status == "open"
    assert served["Notice PINNED"].status == "open", "an operator's override stands"
    assert served["Notice UNDATED"].status == "open"
    assert session.scalar(select(Event)) is None

    # idempotent
    assert close_past_deadline(session, at).closed == 0


def test_the_scheduler_tick_runs_the_sweep(factory: Any) -> None:
    from infra.scheduler import jobs

    with factory() as session:
        src = upsert_licence_and_source(session, _entry(), "v")
        load_dataframe(
            session, src, "opportunity", pd.DataFrame([_notice("A", "2099-01-01T00:00:00Z")]), None
        )
        session.commit()
    report = jobs.deadline_tick_job(factory, now=dt.datetime(2099, 1, 2, tzinfo=UTC))
    assert report["closed"] == 1
    with factory() as session:
        assert session.scalar(select(Opportunity.status)) == "closed"
