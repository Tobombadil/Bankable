"""Nothing this platform publishes is delayed by time — on any tier, for any shape, from any source.

Two owner decisions put the whole time dimension out of the product:

* 2026-09-19 (`docs/00-PLAN.md`, 2026-09-18 row item 2): "alerts, exports, API and watchlists are
  paid; free users see every record; the delay is kept only on ISO change events."
* 2026-09-21 (same log): drop that surviving ISO change-event delay too, and drop its knob with
  it. The measurement that settled it is in the 2026-09-20 row and in
  `tests/test_free_tier_change_reconstruction.py`: the withheld `(field, before, after)` was
  recoverable from two reads of a record page that was never delayed, and a full daily sweep of
  the register cost 53 requests against the public tier's own allowance of 1,440.

This file is the statement of that decision, pinned at all three levels it used to exist on:

* the **manifest** — no source in `data/sources.yaml` declares a change-event lag, and in
  particular none of the eight `us.iso.*` interconnection-queue registers that carried one does.
  The ids are written out below rather than derived from the file, so this stays a statement about
  the decision rather than a tautology over whatever the file happens to say. Adding
  `change_event_lag_days` back to any entry fails here;
* the **store** — `source` has no `lag_days`/`lag_overrides` column to hold a delay (migration
  `0019`), so there is no runtime knob an admin `PATCH` could turn;
* the **policy function and the loader** — `services/ingest/lag.py` offers nothing but the
  identity, and a record *and* an ISO change event both come out of the real loader with
  `public_at == published_at`.

What this file deliberately does **not** touch is the licence gate: `restricted`/`unknown` sources
stay invisible on every non-admin surface whatever their timing, which is
`tests/test_licence_gate_survives_removing_the_delay.py`'s subject.
"""

from __future__ import annotations

import datetime as dt
import inspect
from typing import Any

import pandas as pd
import pytest
import sqlalchemy as sa
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

import services.ingest.lag as lag_module
from pipeline.connectors.registry import SOURCES_YAML, Registry, SourceEntry
from services.db.models import Event, Proposal, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.lag import RECORD_LAG_DAYS, record_public_at
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.ingest.test_loader import sample_proposal_row

#: The eight registers that carried the ISO change-event delay until 2026-09-21. Written out here,
#: not read from the manifest, so that "none of these is delayed any more" is a claim about the
#: owner's decision rather than a restatement of the file under test.
FORMERLY_DELAYED_SOURCE_IDS = {
    "us.iso.caiso.gen_queue",
    "us.iso.ercot.gen_queue",
    "us.iso.ercot.large_load_queue",
    "us.iso.spp.gen_queue",
    "us.iso.nyiso.gen_queue",
    "us.iso.isone.gen_queue",
    "us.iso.pjm.gen_queue",
    "us.iso.miso.gen_queue",
}

#: The manifest key that used to carry it, and the two columns it was mirrored into.
LAG_MANIFEST_KEY = "change_event_lag_days"
LAG_COLUMNS = ("lag_days", "lag_overrides")


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def iso_source_entry(id_: str = "us.iso.test.gen_queue") -> SourceEntry:
    """An ISO queue register exactly as the manifest now describes one: no lag field anywhere."""
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
            "tier": 1,
            "license": "US federal public domain",
        }
    )


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


# ------------------------------------------------------------------------------ 1. the manifest
def test_no_source_declares_a_change_event_lag() -> None:
    """Add `change_event_lag_days: 14` to any entry in `data/sources.yaml` and this fails."""
    doc = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    declaring = sorted(s["id"] for s in doc["sources"] if LAG_MANIFEST_KEY in s)
    assert declaring == [], (
        f"{LAG_MANIFEST_KEY} is not a field any more: nothing is time-delayed on any tier "
        f"(owner, 2026-09-21). Declared by: {declaring}"
    )


def test_the_eight_registers_that_used_to_be_delayed_are_present_and_undelayed() -> None:
    """The delay went; the sources did not. An empty manifest would pass the test above."""
    doc = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    entries = {s["id"]: s for s in doc["sources"]}
    missing = sorted(FORMERLY_DELAYED_SOURCE_IDS - set(entries))
    assert missing == [], f"these registers should still be in the manifest: {missing}"
    for source_id in sorted(FORMERLY_DELAYED_SOURCE_IDS):
        assert LAG_MANIFEST_KEY not in entries[source_id], source_id


def test_no_manifest_entry_carries_any_lag_shaped_key() -> None:
    """A lag re-introduced under a different spelling is the same promise. Nothing named `lag`."""
    doc = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    offenders = sorted((s["id"], key) for s in doc["sources"] for key in s if "lag" in key.lower())
    assert offenders == []


def test_the_registry_exposes_no_lag_at_all() -> None:
    """`SourceEntry` has no lag attribute, so a yaml key could not reach the loader even if added."""
    reg = Registry()
    entry = reg.get("us.iso.caiso.gen_queue")
    assert not [f for f in vars(entry) if "lag" in f.lower()]
    assert not [f for f in SourceEntry.__dataclass_fields__ if "lag" in f.lower()]


# --------------------------------------------------------------------------------- 2. the store
def test_the_source_table_has_no_lag_columns() -> None:
    """Migration `0019` drops them; the ORM is where an admin `PATCH` would have reached them."""
    columns = {c.name for c in Source.__table__.columns}
    assert columns.isdisjoint(LAG_COLUMNS), sorted(columns & set(LAG_COLUMNS))


# ------------------------------------------------------------- 3. the policy and the loader
def test_the_policy_module_offers_only_the_identity() -> None:
    """There is no knob, and no function that could apply one (`services/ingest/lag.py`)."""
    assert RECORD_LAG_DAYS == 0
    now = dt.datetime(2026, 9, 21, 12, tzinfo=dt.UTC)
    assert record_public_at(now) == now
    public_callables = sorted(
        name
        for name, obj in vars(lag_module).items()
        if not name.startswith("_") and inspect.isfunction(obj) and obj.__module__ == lag_module.__name__
    )
    assert public_callables == ["record_public_at"]


def test_a_record_and_an_iso_change_event_are_both_public_at_publication(session: Session) -> None:
    """The whole decision, through the real loader, on the shape that used to be withheld."""
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
    assert event.public_at == event.published_at, (
        "an ISO queue change event is public the moment it publishes (owner, 2026-09-21)"
    )


def test_every_event_the_loader_writes_is_immediately_public(session: Session) -> None:
    """Not a property of ISO sources: of every source. A non-ISO register behaves identically."""
    entry = iso_source_entry("us.test.live_queue")
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

    gaps = session.execute(sa.select(Event.public_at, Event.published_at)).all()
    assert gaps, "the fixture must produce at least one event"
    assert all(public == published for public, published in gaps)
