"""The paywall is by shape, not by time — what is still delayed, and what is not.

Owner decision, 2026-09-19 (`docs/00-PLAN.md`, 2026-09-18 row item 2): "alerts, exports, API and
watchlists are paid; free users see every record; the delay is kept only on ISO change events."

This file pins the predicate that implements it, at all three levels it exists on:

* the **manifest** — which sources declare a change-event lag (`data/sources.yaml`
  `change_event_lag_days`), and that it is exactly the `us.iso.*` interconnection-queue registers,
  so a ninth ISO source added without the field fails here rather than publishing live unnoticed;
* the **policy function** — `services/ingest/lag.py`;
* the **loader** — a record gets `public_at == published_at`, an ISO change event gets
  `published_at + 14 days`, and an event from a source with no declared lag gets neither.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SOURCES_YAML, Registry, SourceEntry
from services.db.models import Event, Proposal
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.lag import (
    ISO_CHANGE_EVENT_LAG_DAYS,
    RECORD_LAG_DAYS,
    change_event_lag_days,
    change_event_public_at,
    record_public_at,
)
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.ingest.test_loader import sample_proposal_row

#: The set the owner's decision names. Written out here, not derived from the manifest, so that
#: this test is a statement about the decision rather than a tautology over whatever the file says.
ISO_QUEUE_SOURCE_IDS = {
    "us.iso.caiso.gen_queue",
    "us.iso.ercot.gen_queue",
    "us.iso.ercot.large_load_queue",
    "us.iso.spp.gen_queue",
    "us.iso.nyiso.gen_queue",
    "us.iso.isone.gen_queue",
    "us.iso.pjm.gen_queue",
    "us.iso.miso.gen_queue",
}


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def iso_source_entry(id_: str = "us.iso.test.gen_queue") -> SourceEntry:
    return SourceEntry.from_yaml(
        {
            "id": id_,
            "name": "Test ISO Queue",
            "jurisdiction": "US-TX",
            "category": "generation_queue",
            "operator": "Test ISO",
            "url": "https://example.org/queue",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "weekly",
            "publication": "raw_ok",
            "change_event_lag_days": ISO_CHANGE_EVENT_LAG_DAYS,
            "tier": 1,
            "license": "US federal public domain",
        }
    )


def live_source_entry(id_: str = "us.test.live_queue") -> SourceEntry:
    e = iso_source_entry(id_)
    e.change_event_lag_days = None
    return e


def _row(source_id: str, record_id: str, lifecycle_state: str) -> dict[str, Any]:
    r = dict(sample_proposal_row(record_id, lifecycle_state=lifecycle_state))
    r["record_id"] = f"{source_id}:{record_id}"
    r["source_id"] = source_id
    return r


def _event_row(source_id: str, record_id: str, before: str, after: str) -> dict[str, Any]:
    return {
        "event_type": "status_change",
        "record_id": f"{source_id}:{record_id}",
        "source_id": source_id,
        "field": "lifecycle_state",
        "before": before,
        "after": after,
        "observed_at": "2026-09-12T06:00:00Z",
    }


# ------------------------------------------------------------------ the manifest is the registry
def test_exactly_the_iso_queue_registers_declare_a_change_event_lag() -> None:
    """The delayed set lives in `data/sources.yaml`, not in code — so this is what pins it."""
    doc = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    declared = {s["id"] for s in doc["sources"] if s.get("change_event_lag_days")}
    assert declared == ISO_QUEUE_SOURCE_IDS


def test_every_declared_lag_is_the_one_documented_figure() -> None:
    reg = Registry()
    lags = {i: reg.get(i).change_event_lag_days for i in ISO_QUEUE_SOURCE_IDS}
    assert set(lags.values()) == {ISO_CHANGE_EVENT_LAG_DAYS}


def test_non_iso_connection_registers_are_not_delayed() -> None:
    """`source.category` would have been the tempting handle and is the wrong one: the
    `generation_queue` vocabulary also covers six connection registers that are not ISO queues,
    and the owner's decision says ISO."""
    reg = Registry()
    queue_like = [i for i in reg.ids() if reg.get(i).category in ("generation_queue", "load_queue")]
    non_iso = [i for i in queue_like if i not in ISO_QUEUE_SOURCE_IDS]
    assert non_iso, "the point of the test is that some queue sources are not ISO queues"
    assert all(reg.get(i).change_event_lag_days is None for i in non_iso)


# ----------------------------------------------------------------------------- the policy itself
def test_records_carry_no_lag_at_all() -> None:
    now = dt.datetime(2026, 9, 20, 12, tzinfo=dt.UTC)
    assert RECORD_LAG_DAYS == 0
    assert record_public_at(now) == now


@pytest.mark.parametrize(
    ("source_lag", "overrides", "event_type", "expected"),
    [
        (None, None, "status_change", 0),
        (0, None, "status_change", 0),
        (14, {}, "status_change", 14),
        (14, {"withdrawn": 0}, "withdrawn", 0),
        (14, {"withdrawn": 0}, "status_change", 14),
        (None, {"status_change": 3}, "status_change", 3),
        (14, None, None, 14),
    ],
)
def test_change_event_lag_resolution(
    source_lag: int | None, overrides: dict[str, int] | None, event_type: str | None, expected: int
) -> None:
    assert (
        change_event_lag_days(
            source_change_event_lag_days=source_lag, lag_overrides=overrides, event_type=event_type
        )
        == expected
    )


def test_change_event_public_at_adds_the_resolved_lag() -> None:
    now = dt.datetime(2026, 9, 20, 12, tzinfo=dt.UTC)
    assert change_event_public_at(now, source_change_event_lag_days=14) == now + dt.timedelta(days=14)
    assert change_event_public_at(now) == now


# ------------------------------------------------------------------------- the loader end to end
def test_loader_seeds_source_lag_days_from_the_manifest(session: Session) -> None:
    iso = upsert_licence_and_source(session, iso_source_entry(), "2026-09-18")
    live = upsert_licence_and_source(session, live_source_entry(), "2026-09-18")
    assert iso.lag_days == ISO_CHANGE_EVENT_LAG_DAYS
    assert live.lag_days is None


def test_records_are_public_immediately_and_iso_change_events_are_not(session: Session) -> None:
    entry = iso_source_entry()
    src = upsert_licence_and_source(session, entry, "2026-09-18")
    load_dataframe(session, src, "proposal", pd.DataFrame([_row(entry.id, "Q1", "filed")]), None)
    load_dataframe(
        session,
        src,
        "proposal",
        pd.DataFrame([_row(entry.id, "Q1", "studied")]),
        pd.DataFrame([_event_row(entry.id, "Q1", "filed", "studied")]),
    )
    session.flush()

    proposal = session.scalars(select(Proposal)).one()
    assert proposal.public_at == proposal.published_at, "a record is public the moment it publishes"

    event = session.scalars(select(Event)).one()
    assert event.public_at is not None and event.published_at is not None
    assert event.public_at - event.published_at == dt.timedelta(days=ISO_CHANGE_EVENT_LAG_DAYS)


def test_a_source_without_a_declared_lag_publishes_its_change_events_live(session: Session) -> None:
    entry = live_source_entry()
    src = upsert_licence_and_source(session, entry, "2026-09-18")
    load_dataframe(session, src, "proposal", pd.DataFrame([_row(entry.id, "Q1", "filed")]), None)
    load_dataframe(
        session,
        src,
        "proposal",
        pd.DataFrame([_row(entry.id, "Q1", "studied")]),
        pd.DataFrame([_event_row(entry.id, "Q1", "filed", "studied")]),
    )
    session.flush()

    event = session.scalars(select(Event)).one()
    assert event.public_at == event.published_at
