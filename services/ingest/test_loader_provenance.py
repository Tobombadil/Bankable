"""Provenance on what the loaders write (audit 2026-09-30 data engineer F8): every event the loader
writes carries the quartet and names the record's own page, and an organisation alias carries the
fetch its spelling was read in, not the time of the load."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import Event, OrganizationAlias
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.ingest.ownership import load_owner_shares
from services.ingest.test_loader import open_source_entry, sample_proposal_row
from services.ingest.test_ownership import _seed_plant, owner_frame

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
SRC = "us.test.open_queue"


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _row(srid: str, **over: Any) -> dict[str, Any]:
    r = sample_proposal_row(srid)
    r["source_url"] = f"https://example.org/queue/{srid}"
    r.update(over)
    return r


def _event(record_id: str, etype: str, field: str, before: Any, after: Any, at: str) -> dict[str, Any]:
    return {
        "event_type": etype,
        "record_id": f"{SRC}:{record_id}",
        "source_id": SRC,
        "field": field,
        "before": before,
        "after": after,
        "observed_at": at,
    }


def test_every_event_the_loader_writes_carries_the_quartet(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    load_dataframe(session, src, "proposal", pd.DataFrame([_row("Q1"), _row("Q2"), _row("Q3")]), None)

    at = "2026-09-27T15:15:28Z"
    frame = pd.DataFrame(
        [
            _row("Q1", lifecycle_state="contracted", retrieved_at=at),
            _row("Q2", status_raw="(TS) Construction complete", retrieved_at=at),
        ]
    )
    events = pd.DataFrame(
        [
            _event("Q1", "status_change", "lifecycle_state", "filed", "contracted", at),
            _event("Q2", "status_raw_change", "status_raw", "ACTIVE", "(TS) Construction complete", at),
            _event("Q3", "removed", "lifecycle_state", "filed", None, at),
        ]
    )
    result = load_dataframe(session, src, "proposal", frame, events)
    assert result.events_created == 3

    rows = list(session.scalars(select(Event).where(Event.source_id == SRC)))
    assert len(rows) == 3
    for e in rows:
        assert e.source_id and e.source_url and e.retrieved_at and e.licence_id, e.event_type
        # the record's own page, as on its link row, not the manifest landing URL
        assert e.source_url.startswith("https://example.org/queue/Q"), e.source_url
        stamped = e.retrieved_at if e.retrieved_at.tzinfo else e.retrieved_at.replace(tzinfo=dt.UTC)
        assert stamped == dt.datetime(2026, 9, 27, 15, 15, 28, tzinfo=dt.UTC)
    by_key = {tuple(e.changed_keys): e for e in rows if e.event_type == "field_changed"}
    raw_event = by_key[("status_raw",)]  # within-state status text (audit F7) loads as a field change
    assert raw_event.after == {"status_raw": "(TS) Construction complete"}
    assert raw_event.source_url == "https://example.org/queue/Q2"


def test_an_owner_alias_carries_the_fetch_not_the_load_time(session: Session) -> None:
    _seed_plant(session)
    load_owner_shares(session, owner_frame())
    aliases = list(session.scalars(select(OrganizationAlias)))
    assert aliases, "the owner names create aliases"
    for alias in aliases:
        stamped = (
            alias.retrieved_at if alias.retrieved_at.tzinfo else alias.retrieved_at.replace(tzinfo=dt.UTC)
        )
        assert stamped == dt.datetime(2026, 9, 18, tzinfo=dt.UTC), (alias.alias, alias.retrieved_at)
        assert alias.source_url == "https://www.eia.gov/electricity/data/eia860/"
