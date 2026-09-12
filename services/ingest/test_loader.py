"""Loader tests: idempotent upsert, gate refusal, public_at lag computation."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry, SourceEntry
from services.db.models import Event, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import GateRefused, load_dataframe, upsert_licence_and_source

UTC = dt.UTC


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as s:
        yield s


def open_source_entry(id_: str = "us.test.open_queue") -> SourceEntry:
    return SourceEntry.from_yaml(
        {
            "id": id_,
            "name": "Test Open Queue",
            "jurisdiction": "US-TX",
            "category": "generation_queue",
            "operator": "Test ISO",
            "url": "https://example.org/queue",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "weekly",
            "tier": 1,
            "license": "US federal public domain",
        }
    )


def restricted_source_entry(id_: str = "us.test.restricted_queue") -> SourceEntry:
    e = open_source_entry(id_)
    e.reuse = "restricted"
    return e


def sample_proposal_row(record_id: str = "Q1", lifecycle_state: str = "filed") -> dict[str, object]:
    return {
        "record_id": f"us.test.open_queue:{record_id}",
        "source_id": "us.test.open_queue",
        "source_record_id": record_id,
        "source_url": "https://example.org/queue/Q1",
        "retrieved_at": "2026-09-12T05:00:00Z",
        "licence_id": "us.test.open_queue#irrelevant",
        "kind": "storage",
        "name_canonical": "Test Storage Project",
        "name_norm": "test storage project",
        "sponsor_name": "Acme Power LLC",
        "sponsor_norm": "acme power",
        "technology": "bess_li_ion",
        "technology_raw": "Battery Storage",
        "capacity_mw": 100.0,
        "storage_mwh": 200.0,
        "iso": "ERCOT",
        "state": "TX",
        "county": "Travis",
        "county_norm": "travis",
        "lifecycle_state": lifecycle_state,
        "status_raw": "ACTIVE",
        "status_rule": "r1",
        "status_conflict": False,
        "queue_date": "2026-01-01",
        "proposed_cod": "2028-06-30",
        "queue_id": record_id,
        "eia_plant_id": None,
        "eia_generator_id": None,
        "cross_refs": None,
        "raw": f'{{"Queue ID": "{record_id}", "Status": "ACTIVE"}}',
    }


def test_gate_refused_for_restricted_source(session: Session) -> None:
    entry = restricted_source_entry()
    with pytest.raises(GateRefused):
        upsert_licence_and_source(session, entry, "2026-09-12")


def test_upsert_licence_and_source_open(session: Session) -> None:
    entry = open_source_entry()
    src = upsert_licence_and_source(session, entry, "2026-09-12")
    assert src.id == entry.id
    assert src.licence.reuse_class == "open"
    assert src.gated is False


def test_gate_refused_for_reuse_value_outside_the_known_vocabulary(session: Session) -> None:
    """Defence in depth (module docstring): `_assert_not_gated` only screens the two known gated
    values (`restricted`, `unknown`); this proves the second, independent check inside
    `upsert_licence_and_source` also refuses anything that is not affirmatively `open` or
    `attribution`, rather than defaulting open on an unrecognised value."""
    entry = open_source_entry("us.test.mystery_reuse")
    entry.reuse = "not-a-real-reuse-class"
    with pytest.raises(GateRefused):
        upsert_licence_and_source(session, entry, "2026-09-12")


def test_gate_refused_when_stored_licence_is_gated_even_if_the_manifest_now_says_open(
    session: Session,
) -> None:
    """The "both must hold" re-check: a `licence` row already on file as `restricted` (e.g. a
    licence dispute reclassified it, docs/21 §3.19 invariant L2) refuses ingestion even if the
    caller's registry entry claims `open` under the same licence id."""
    from services.db.models import Licence

    entry = open_source_entry("us.test.stale_manifest")
    session.add(
        Licence(
            id=entry.licence_id,
            name="Stale licence row",
            reuse_class="restricted",
            evidence_url="https://example.org/terms",
            evidence_retrieved_at=dt.datetime(2026, 1, 1, tzinfo=UTC),
            classified_by="legal-compliance",
        )
    )
    session.flush()
    with pytest.raises(GateRefused):
        upsert_licence_and_source(session, entry, "2026-09-12")


def test_load_dataframe_creates_proposal_with_public_at_lag(session: Session) -> None:
    entry = open_source_entry()
    src = upsert_licence_and_source(session, entry, "2026-09-12")
    df = pd.DataFrame([sample_proposal_row()])
    result = load_dataframe(session, src, "proposal", df, None)
    assert result.proposals_created == 1
    prop = session.scalar(select(Proposal))
    assert prop is not None
    assert prop.name_canonical == "Test Storage Project"
    assert prop.jurisdiction == "US-TX"
    assert prop.publish_state == "public"
    assert prop.public_at is not None
    assert prop.published_at is not None
    lag = prop.public_at - prop.published_at
    assert lag == dt.timedelta(days=14)  # supply default (services/ingest/lag.py)
    assert prop.sponsor is not None
    assert prop.sponsor.name_canonical == "Acme Power LLC"
    assert prop.location is not None
    assert prop.location.state_code == "US-TX"
    assert prop.min_reuse_class == "open"
    link = session.scalar(select(ProposalSource))
    assert link is not None
    assert link.source_id == src.id
    assert link.source_record_id == "Q1"
    assert link.retrieved_at is not None


def test_load_dataframe_is_idempotent(session: Session) -> None:
    entry = open_source_entry()
    src = upsert_licence_and_source(session, entry, "2026-09-12")
    df = pd.DataFrame([sample_proposal_row()])
    load_dataframe(session, src, "proposal", df, None)
    result2 = load_dataframe(session, src, "proposal", df, None)
    assert result2.proposals_created == 0
    assert result2.proposals_updated == 1
    count = session.scalar(select(func.count()).select_from(Proposal))
    assert count == 1
    link_count = session.scalar(select(func.count()).select_from(ProposalSource))
    assert link_count == 1


def test_load_dataframe_with_events_idempotent(session: Session) -> None:
    entry = open_source_entry()
    src = upsert_licence_and_source(session, entry, "2026-09-12")
    df = pd.DataFrame([sample_proposal_row()])
    load_dataframe(session, src, "proposal", df, None)

    events_df = pd.DataFrame(
        [
            {
                "event_type": "status_change",
                "record_id": "us.test.open_queue:Q1",
                "source_id": src.id,
                "field": "lifecycle_state",
                "before": "filed",
                "after": "studied",
                "observed_at": "2026-09-12T06:00:00Z",
            }
        ]
    )
    result = load_dataframe(session, src, "proposal", df, events_df)
    assert result.events_created == 1
    result2 = load_dataframe(session, src, "proposal", df, events_df)
    assert result2.events_created == 0
    assert result2.events_skipped_idempotent == 1
    ev_count = session.scalar(select(func.count()).select_from(Event))
    assert ev_count == 1
    ev = session.scalar(select(Event))
    assert ev is not None
    assert ev.event_type == "status_change"
    assert ev.after == {"lifecycle_state": "studied"}
    assert ev.public_at is not None
    assert ev.published_at is not None


def test_load_from_files_gate_refused_before_any_file_read(tmp_path, session: Session) -> None:
    """The registry gate is checked before the loader even looks for the parquet files."""
    from services.ingest.loader import load_from_files

    registry = Registry.__new__(Registry)  # bypass YAML load
    registry.path = None  # type: ignore[assignment]
    registry.version = "test"
    registry.sources = {"us.test.restricted_queue": restricted_source_entry()}
    with pytest.raises(GateRefused):
        load_from_files(
            session, "us.test.restricted_queue", "20260101T000000Z", data_root=tmp_path, registry=registry
        )
    # No directories were even created under tmp_path for a gated source.
    assert not (tmp_path / "normalized").exists()


def test_load_from_files_reads_parquet_and_run_json(tmp_path, session: Session) -> None:
    from services.ingest.loader import load_from_files

    entry = open_source_entry("us.test.files_queue")
    registry = Registry.__new__(Registry)
    registry.path = None  # type: ignore[assignment]
    registry.version = "2026-09-12"
    registry.sources = {entry.id: entry}

    row = sample_proposal_row("Q9")
    row["record_id"] = f"{entry.id}:Q9"
    row["source_id"] = entry.id
    df = pd.DataFrame([row])
    norm_dir = tmp_path / "normalized" / entry.id
    norm_dir.mkdir(parents=True)
    df.to_parquet(norm_dir / "20260912T050000Z.parquet", index=False)

    result = load_from_files(session, entry.id, "20260912T050000Z", data_root=tmp_path, registry=registry)
    assert result.proposals_created == 1
    src = session.get(Source, entry.id)
    assert src is not None
    assert src.publish_state == "api_only"
