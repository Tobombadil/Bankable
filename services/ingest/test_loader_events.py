"""Loader-side change-event defects from the 2026-09-18 audit (§3.1 "Change events"):

1. duplicated source ids keyed by position (`#2`) — identities swap on a row reorder;
2. the event idempotency key omitted the before-state and the observation time — a status
   that returns to a previous value, or a second removal, was dropped as a duplicate; and
   `removed` events were skipped outright because the record is no longer in the frame;
5. `field_provenance` was written once at creation and never updated.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.dedupe import content_disambiguator
from services.db.models import Event, Proposal, ProposalSource
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.ingest.test_loader import open_source_entry, sample_proposal_row

SRC = "us.test.open_queue"


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def row(srid: str, name: str, capacity: float, county: str, **over: Any) -> dict[str, Any]:
    r = sample_proposal_row(srid)
    r.update(
        name_canonical=name,
        name_norm=name.lower(),
        capacity_mw=capacity,
        county=county,
        county_norm=county.lower(),
    )
    r.update(over)
    return r


def content_keyed(r: dict[str, Any]) -> dict[str, Any]:
    """What `Connector.finalize` emits for a member of a duplicated id group."""
    return {**r, "record_id": f"{SRC}:{r['source_record_id']}#{content_disambiguator(r)}"}


def event(
    record_id: str, etype: str, before: Any, after: Any, at: str, field: str = "lifecycle_state"
) -> dict:
    return {
        "event_type": etype,
        "record_id": record_id,
        "source_id": SRC,
        "field": field,
        "before": before,
        "after": after,
        "observed_at": at,
    }


def proposals_by_name(session: Session) -> dict[str, Proposal]:
    return {p.name_canonical: p for p in session.scalars(select(Proposal))}


def seed_legacy_rows(session: Session, src: Any) -> None:
    """Rows as the pre-2026-09-18 loader stored them: the two `0031` records keyed `0031` (first
    in the file) and `0031#2` (second). Loaded through the current loader, then re-keyed to the
    old spelling in place, since the current loader never writes positional keys any more."""
    alpha = content_keyed(row("0031", "Alpha Solar", 100.0, "Erie"))
    beta = content_keyed(row("0031", "Beta Wind", 50.0, "Ulster"))
    load_dataframe(session, src, "proposal", pd.DataFrame([alpha, beta]), None)
    by_id = {link.proposal_id: link for link in session.scalars(select(ProposalSource))}
    names = proposals_by_name(session)
    by_id[names["Alpha Solar"].id].source_record_id = "0031"
    by_id[names["Beta Wind"].id].source_record_id = "0031#2"
    session.flush()


# ---------------------------------------------------------------- item 1 (loader side)
def test_reordered_duplicate_ids_keep_their_identities(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    alpha = content_keyed(row("0031", "Alpha Solar", 100.0, "Erie"))
    beta = content_keyed(row("0031", "Beta Wind", 50.0, "Ulster"))
    gamma = row("0040", "Gamma BESS", 20.0, "Kings")

    first = load_dataframe(session, src, "proposal", pd.DataFrame([alpha, beta, gamma]), None)
    assert first.proposals_created == 3
    ids_before = {name: p.id for name, p in proposals_by_name(session).items()}

    second = load_dataframe(session, src, "proposal", pd.DataFrame([gamma, beta, alpha]), None)
    assert second.proposals_created == 0 and second.proposals_updated == 3
    assert {name: p.id for name, p in proposals_by_name(session).items()} == ids_before

    links = {link.source_record_id: link for link in session.scalars(select(ProposalSource))}
    assert set(links) == {
        "0040",
        f"0031#{content_disambiguator(alpha)}",
        f"0031#{content_disambiguator(beta)}",
    }
    assert links[f"0031#{content_disambiguator(alpha)}"].proposal_id == ids_before["Alpha Solar"]


def test_stored_positional_suffixes_are_matched_by_content_not_rewritten(session: Session) -> None:
    """Rows already in the store as `0031` / `0031#2` (the pre-2026-09-18 spelling) are matched
    to the new content-keyed rows by their stable fields; their stored keys are left alone."""
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    seed_legacy_rows(session, src)
    links = {link.source_record_id: link for link in session.scalars(select(ProposalSource))}
    assert set(links) == {"0031", "0031#2"}
    ids_before = {name: p.id for name, p in proposals_by_name(session).items()}

    # the new connector output, reordered, with Beta's status changed
    beta = content_keyed(row("0031", "Beta Wind", 50.0, "Ulster", lifecycle_state="withdrawn"))
    alpha = content_keyed(row("0031", "Alpha Solar", 100.0, "Erie"))
    result = load_dataframe(session, src, "proposal", pd.DataFrame([beta, alpha]), None)

    assert result.proposals_created == 0 and result.proposals_updated == 2
    assert {name: p.id for name, p in proposals_by_name(session).items()} == ids_before
    links = {link.source_record_id: link for link in session.scalars(select(ProposalSource))}
    assert set(links) == {"0031", "0031#2"}, "legacy keys are accepted on read, never migrated"
    assert links["0031#2"].proposal_id == ids_before["Beta Wind"]
    assert proposals_by_name(session)["Beta Wind"].lifecycle_state == "withdrawn"
    assert proposals_by_name(session)["Alpha Solar"].lifecycle_state == "filed"


def test_a_legacy_bare_key_is_not_swapped_when_only_the_other_row_survives(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    seed_legacy_rows(session, src)
    beta_id = proposals_by_name(session)["Beta Wind"].id

    only_beta = row("0031", "Beta Wind", 50.0, "Ulster")  # unique now: bare record_id
    result = load_dataframe(session, src, "proposal", pd.DataFrame([only_beta]), None)
    assert result.proposals_created == 0 and result.proposals_updated == 1
    assert proposals_by_name(session)["Beta Wind"].id == beta_id
    assert proposals_by_name(session)["Alpha Solar"].name_canonical == "Alpha Solar"  # untouched


# ---------------------------------------------------------------- item 2: idempotency key
def test_same_snapshot_twice_adds_no_events(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    df = pd.DataFrame([sample_proposal_row("Q1", "studied")])
    ev = pd.DataFrame([event(f"{SRC}:Q1", "status_change", "filed", "studied", "2026-09-12T06:00:00Z")])
    load_dataframe(session, src, "proposal", df, ev)
    again = load_dataframe(session, src, "proposal", df, ev)
    assert again.events_created == 0 and again.events_skipped_idempotent == 1
    assert session.scalar(select(Event).where(Event.event_type == "status_change")) is not None
    assert len(session.scalars(select(Event)).all()) == 1


def test_a_status_that_returns_to_a_previous_value_is_two_events(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    # A -> B
    load_dataframe(
        session,
        src,
        "proposal",
        pd.DataFrame([sample_proposal_row("Q1", "studied")]),
        pd.DataFrame([event(f"{SRC}:Q1", "status_change", "filed", "studied", "2026-09-12T06:00:00Z")]),
    )
    # B -> A
    r2 = load_dataframe(
        session,
        src,
        "proposal",
        pd.DataFrame([sample_proposal_row("Q1", "filed")]),
        pd.DataFrame([event(f"{SRC}:Q1", "status_change", "studied", "filed", "2026-09-13T06:00:00Z")]),
    )
    # A -> B again, a day later
    r3 = load_dataframe(
        session,
        src,
        "proposal",
        pd.DataFrame([sample_proposal_row("Q1", "studied")]),
        pd.DataFrame([event(f"{SRC}:Q1", "status_change", "filed", "studied", "2026-09-14T06:00:00Z")]),
    )
    assert r2.events_created == 1 and r3.events_created == 1
    events = session.scalars(select(Event).order_by(Event.observed_at)).all()
    assert [(e.before, e.after) for e in events] == [
        ({"lifecycle_state": "filed"}, {"lifecycle_state": "studied"}),
        ({"lifecycle_state": "studied"}, {"lifecycle_state": "filed"}),
        ({"lifecycle_state": "filed"}, {"lifecycle_state": "studied"}),
    ]
    assert len({e.idempotency_key for e in events}) == 3


def test_removed_readded_removed_is_two_removal_events(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    q1, q2 = sample_proposal_row("Q1"), sample_proposal_row("Q2")
    load_dataframe(session, src, "proposal", pd.DataFrame([q1, q2]), None)

    gone1 = pd.DataFrame([event(f"{SRC}:Q2", "removed", "filed", None, "2026-09-13T06:00:00Z")])
    r1 = load_dataframe(session, src, "proposal", pd.DataFrame([q1]), gone1)
    assert r1.events_created == 1 and r1.warnings == []
    link = session.scalar(select(ProposalSource).where(ProposalSource.source_record_id == "Q2"))
    assert link is not None and link.gone_at.replace(tzinfo=dt.UTC) == dt.datetime(
        2026, 9, 13, 6, tzinfo=dt.UTC
    )

    back = pd.DataFrame([event(f"{SRC}:Q2", "new", None, "filed", "2026-09-14T06:00:00Z")])
    r2 = load_dataframe(session, src, "proposal", pd.DataFrame([q1, q2]), back)
    assert r2.events_created == 1 and r2.proposals_created == 0
    session.refresh(link)
    assert link.gone_at is None, "a re-sighted record is no longer gone"

    gone2 = pd.DataFrame([event(f"{SRC}:Q2", "removed", "filed", None, "2026-09-15T06:00:00Z")])
    r3 = load_dataframe(session, src, "proposal", pd.DataFrame([q1]), gone2)
    assert r3.events_created == 1
    removals = session.scalars(select(Event).where(Event.event_type == "withdrawn")).all()
    assert len(removals) == 2
    assert {e.observed_at.replace(tzinfo=dt.UTC) for e in removals} == {
        dt.datetime(2026, 9, 13, 6, tzinfo=dt.UTC),
        dt.datetime(2026, 9, 15, 6, tzinfo=dt.UTC),
    }
    # and the first removal is still idempotent on a re-run of its own snapshot
    r4 = load_dataframe(session, src, "proposal", pd.DataFrame([q1]), gone1)
    assert r4.events_created == 0 and r4.events_skipped_idempotent == 1


# ---------------------------------------------------------------- item 5: field provenance
def test_field_provenance_follows_the_run_that_last_changed_each_field(session: Session) -> None:
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    t1, t2 = "2026-09-12T05:00:00Z", "2026-09-13T05:00:00Z"
    load_dataframe(session, src, "proposal", pd.DataFrame([sample_proposal_row("Q1")]), None)
    prop = proposals_by_name(session)["Test Storage Project"]
    assert prop.field_provenance["capacity_mw"]["retrieved_at"] == "2026-09-12T05:00:00+00:00"

    changed = sample_proposal_row("Q1")
    changed.update(capacity_mw=150.0, retrieved_at=t2)
    load_dataframe(session, src, "proposal", pd.DataFrame([changed]), None)
    session.refresh(prop)
    prov = prop.field_provenance
    assert prov["capacity_mw"]["retrieved_at"] == "2026-09-13T05:00:00+00:00"
    assert prov["capacity_mw"]["source_id"] == SRC
    assert prov["name_canonical"]["retrieved_at"] == "2026-09-12T05:00:00+00:00", (
        "unchanged field keeps its date"
    )
    assert t1.replace("Z", "+00:00") == prov["lifecycle_state"]["retrieved_at"]
    assert all(entry["licence_id"] == src.licence_id for entry in prov.values())


# ------------------------------------------------ item 1 follow-up: byte-identical duplicates
def test_byte_identical_rows_sharing_a_natural_key_both_load_and_reload_idempotently(
    session: Session,
) -> None:
    """CI 2026-09-19: the 2026-09-12 eval parquet carries several NYISO rows that are identical
    in every stable field ("Untitled", unknown technology, no capacity) under one natural key
    with legacy positional suffixes. The content key is the same for all of them, so the second
    row violated `proposal_source`'s unique key. The loader now falls back the way
    `suffix_duplicates` does (raw-row hash, then an order suffix), so every row loads, and a
    second load of the same frame matches the same stored rows and adds nothing."""
    src = upsert_licence_and_source(session, open_source_entry(), "v")
    base = row("0099", "Untitled", 0.0, "Unknown", technology="unknown")
    twins = [
        {**base, "record_id": f"{SRC}:0099#2"},
        {**base, "record_id": f"{SRC}:0099#3"},
        {**base, "record_id": f"{SRC}:0099#4"},
    ]
    first = load_dataframe(session, src, "proposal", pd.DataFrame(twins), None)
    assert first.proposals_created == 3
    keys = sorted(session.scalars(select(ProposalSource.source_record_id)).all())
    assert len(keys) == 3 and len(set(keys)) == 3
    assert all(k.startswith("0099#h") for k in keys), keys
    assert not any(k.endswith(("#2", "#3", "#4")) for k in keys), "positional suffixes are never trusted"

    again = load_dataframe(session, src, "proposal", pd.DataFrame(twins), None)
    assert again.proposals_created == 0 and again.proposals_updated == 3
    assert sorted(session.scalars(select(ProposalSource.source_record_id)).all()) == keys
    assert session.scalars(select(Event)).all() == []
