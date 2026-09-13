"""Loader tests: idempotent upsert, gate refusal, public_at lag computation."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry, SourceEntry
from services.db.models import (
    Event,
    Organization,
    OrganizationAlias,
    Proposal,
    ProposalSource,
    Source,
    SourceRun,
)
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


def make_source_run(session: Session, source: Source) -> SourceRun:
    run = SourceRun(source_id=source.id, trigger="manual", started_at=dt.datetime(2026, 9, 12, tzinfo=UTC))
    session.add(run)
    session.flush()
    return run


def test_intra_run_id_reuse_keeps_both_records_and_warns(session: Session) -> None:
    """Two distinct source records sharing one `source_record_id` inside the *same* run/dataframe
    (the measured ISO-NE/NYISO signature, docs/22 §5/§7.1) must never collapse into one proposal:
    both are kept, the second's `source_record_id` is suffixed deterministically, and a
    data-quality warning is recorded on both `LoadResult.warnings` and `source_run.dq`."""
    entry = open_source_entry("us.test.isone_like_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")
    run = make_source_run(session, src)

    row_a = sample_proposal_row("0031", lifecycle_state="filed")
    row_a["name_canonical"] = "First Distinct Project"
    row_b = sample_proposal_row("0031", lifecycle_state="studied")  # same source_record_id
    row_b["record_id"] = "us.test.open_queue:0031#2"  # connector-style composite-key suffix
    row_b["name_canonical"] = "Second Distinct Project"
    df = pd.DataFrame([row_a, row_b])

    result = load_dataframe(session, src, "proposal", df, None, run=run)

    assert result.proposals_created == 2
    assert result.proposals_updated == 0
    assert len(result.warnings) == 1
    assert "0031" in result.warnings[0]
    assert "reused" in result.warnings[0]

    proposals = {p.name_canonical: p for p in session.scalars(select(Proposal)).all()}
    assert set(proposals) == {"First Distinct Project", "Second Distinct Project"}

    links = {link.source_record_id: link for link in session.scalars(select(ProposalSource)).all()}
    assert set(links) == {"0031", "0031#2"}
    assert links["0031"].proposal_id == proposals["First Distinct Project"].id
    assert links["0031#2"].proposal_id == proposals["Second Distinct Project"].id

    assert run.dq_status == "warn"
    assert run.dq is not None
    checks = run.dq["checks"]
    assert any(c["check"] == "duplicate_source_record_id_same_run" for c in checks)


def test_intra_run_id_reuse_does_not_confuse_a_later_run_update(session: Session) -> None:
    """A source record whose natural key already exists from an *earlier* run is an ordinary
    update through the existing diff path, not intra-run reuse -- the occurrence counter resets
    on every `load_dataframe` call."""
    entry = open_source_entry("us.test.update_after_reuse")
    src = upsert_licence_and_source(session, entry, "2026-09-12")
    run1 = make_source_run(session, src)
    df1 = pd.DataFrame([sample_proposal_row("Q1", lifecycle_state="filed")])
    result1 = load_dataframe(session, src, "proposal", df1, None, run=run1)
    assert result1.proposals_created == 1
    assert result1.warnings == []

    run2 = make_source_run(session, src)
    df2 = pd.DataFrame([sample_proposal_row("Q1", lifecycle_state="studied")])
    result2 = load_dataframe(session, src, "proposal", df2, None, run=run2)
    assert result2.proposals_created == 0
    assert result2.proposals_updated == 1
    assert result2.warnings == []
    assert run2.dq_status != "warn"

    count = session.scalar(select(func.count()).select_from(Proposal))
    assert count == 1


def test_repeated_record_id_in_one_dataframe_is_a_sequential_update_not_reuse(session: Session) -> None:
    """A dataframe that bundles more than one historical observation of the *same* connector
    `record_id` for one natural key (e.g. an evaluation snapshot spanning several pull dates,
    `services/resolve/report.py`) must fold them as ordinary sequential updates, not intra-run id
    reuse -- only a genuinely *different* `record_id` sharing the natural key is reuse."""
    entry = open_source_entry("us.test.sequential_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")

    row_1 = sample_proposal_row("Q1", lifecycle_state="filed")
    row_2 = sample_proposal_row("Q1", lifecycle_state="studied")  # same record_id, later snapshot
    row_3 = sample_proposal_row("Q1", lifecycle_state="permitted")
    df = pd.DataFrame([row_1, row_2, row_3])

    result = load_dataframe(session, src, "proposal", df, None)

    assert result.proposals_created == 1
    assert result.proposals_updated == 2
    assert result.warnings == []

    count = session.scalar(select(func.count()).select_from(Proposal))
    assert count == 1
    prop = session.scalar(select(Proposal))
    assert prop is not None
    assert prop.lifecycle_state == "permitted"  # last row in the dataframe wins


def test_organization_punctuation_only_spellings_merge_with_two_aliases(session: Session) -> None:
    """Two raw sponsor spellings that normalise to the same organisation once case, whitespace and
    punctuation are stripped resolve to one `organization` row with two `organization_alias` rows
    -- not a `UNIQUE constraint failed: organization.slug` crash
    (`services/resolve/README.md`'s observed limitation)."""
    entry = open_source_entry("us.test.org_punct_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")

    row_a = sample_proposal_row("A1")
    row_a["sponsor_name"] = "CED Development, Inc."
    row_b = sample_proposal_row("A2")
    row_b["sponsor_name"] = "Ced Development Inc"
    df = pd.DataFrame([row_a, row_b])

    result = load_dataframe(session, src, "proposal", df, None)
    assert result.proposals_created == 2
    assert result.organizations_created == 1

    orgs = session.scalars(select(Organization)).all()
    assert len(orgs) == 1
    org = orgs[0]
    assert org.name_canonical == "CED Development, Inc."

    aliases = session.scalars(
        select(OrganizationAlias).where(OrganizationAlias.organization_id == org.id)
    ).all()
    assert {a.alias for a in aliases} == {"CED Development, Inc.", "Ced Development Inc"}

    proposals = session.scalars(select(Proposal)).all()
    assert {p.sponsor_org_id for p in proposals} == {org.id}


def test_organization_letter_distinct_names_stay_separate(session: Session) -> None:
    """Two sponsor names that differ in letters, not just punctuation, remain distinct
    organisations."""
    entry = open_source_entry("us.test.org_distinct_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")

    row_a = sample_proposal_row("B1")
    row_a["sponsor_name"] = "Acme Power LLC"
    row_b = sample_proposal_row("B2")
    row_b["sponsor_name"] = "Acme Power II LLC"
    df = pd.DataFrame([row_a, row_b])

    result = load_dataframe(session, src, "proposal", df, None)
    assert result.organizations_created == 2

    org_names = {o.name_canonical for o in session.scalars(select(Organization)).all()}
    assert org_names == {"Acme Power LLC", "Acme Power II LLC"}
    alias_count = session.scalar(select(func.count()).select_from(OrganizationAlias))
    assert alias_count == 2  # one alias per organisation for its own first-seen spelling


def test_organization_repeated_alternate_spelling_does_not_duplicate_alias(session: Session) -> None:
    """Re-ingesting the same alternate spelling for an already-known organisation must not create
    a second identical `organization_alias` row (idempotent across runs)."""
    entry = open_source_entry("us.test.org_repeat_alias_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")

    row_a = sample_proposal_row("C1")
    row_a["sponsor_name"] = "CED Development, Inc."
    row_b = sample_proposal_row("C2")
    row_b["sponsor_name"] = "Ced Development Inc"
    row_c = sample_proposal_row("C3")
    row_c["sponsor_name"] = "Ced Development Inc"  # same alternate spelling again
    df = pd.DataFrame([row_a, row_b, row_c])

    load_dataframe(session, src, "proposal", df, None)

    org = session.scalar(select(Organization))
    assert org is not None
    alias_count = session.scalar(
        select(func.count()).select_from(OrganizationAlias).where(OrganizationAlias.organization_id == org.id)
    )
    assert alias_count == 2


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
