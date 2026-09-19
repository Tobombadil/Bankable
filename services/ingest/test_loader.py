"""Loader tests: idempotent upsert, gate refusal, public_at lag computation."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry, SourceEntry
from services.db.models import (
    Event,
    Licence,
    Location,
    Organization,
    OrganizationAlias,
    Proposal,
    ProposalSource,
    Source,
    SourceRun,
)
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.geocode import CountyGazetteer, geocode
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
    both are kept, each stored under a content-derived key (never a positional `#2`, audit
    2026-09-18 item 1), and a data-quality warning is recorded on both `LoadResult.warnings` and
    `source_run.dq`."""
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

    from pipeline.connectors.dedupe import content_disambiguator

    key_a, key_b = (f"0031#{content_disambiguator(r)}" for r in (row_a, row_b))
    links = {link.source_record_id: link for link in session.scalars(select(ProposalSource)).all()}
    assert set(links) == {key_a, key_b}
    assert links[key_a].proposal_id == proposals["First Distinct Project"].id
    assert links[key_b].proposal_id == proposals["Second Distinct Project"].id

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
    # Sprint 2 fix: a gate-cleared (open/attribution) source loads straight to `public`, not the
    # earlier `api_only` default that needed a separate admin-style flip (services/README.md
    # "Sprint 2 fixes"; see also `test_source_publish_state_defaults_from_registry_class` below).
    assert src.publish_state == "public"


# ------------------------------------------------------------------ licence correctness (Sprint 2)
def test_licence_flags_derived_from_registry_reuse_not_hardcoded(session: Session) -> None:
    """docs/21 §8 / services/README.md open decision #11, fixed: `allows_raw_publication` and the
    licence `quote_text` come from the real `data/sources.yaml` registry entry, not a blanket
    `True` for every source. CAISO and NYISO are the two sources the legal register
    (docs/00-PLAN.md, 2026-09-12) calls derived-only, and both say so in their own `notes` text
    (`_is_derived_only_override`'s module-docstring rationale in services/ingest/loader.py: this is
    a per-source override on top of `attribution`, not a rule that every `attribution` source is
    derived-only -- a plain attribution source with no such note, e.g. GB NESO, stays raw-ok per
    docs/21 §8's general `attribution` row). ERCOT (`open`) is unaffected either way.
    """
    registry = Registry()

    caiso = upsert_licence_and_source(session, registry.get("us.iso.caiso.gen_queue"), registry.version)
    nyiso = upsert_licence_and_source(session, registry.get("us.iso.nyiso.gen_queue"), registry.version)
    ercot = upsert_licence_and_source(session, registry.get("us.iso.ercot.gen_queue"), registry.version)

    assert caiso.licence.reuse_class == "attribution"
    assert caiso.licence.allows_raw_publication is False
    assert caiso.licence.quote_text and caiso.licence.quote_text.startswith("CAISO Terms of Use")

    assert nyiso.licence.reuse_class == "attribution"
    assert nyiso.licence.allows_raw_publication is False
    assert nyiso.licence.quote_text and nyiso.licence.quote_text.startswith("NYISO Legal Notice")

    assert ercot.licence.reuse_class == "open"
    assert ercot.licence.allows_raw_publication is True
    assert ercot.licence.quote_text and ercot.licence.quote_text.startswith("ERCOT Website User Agreement")


def test_plain_attribution_source_without_a_derived_only_note_stays_raw_allowed(session: Session) -> None:
    """The override is textual (module docstring above), not a blanket rule over `reuse ==
    "attribution"` -- an attribution source whose notes/licence text never says "derived-only"
    (e.g. a curated issuer with a mandatory-credit-only clause, matching GB NESO's real registry
    entry in shape) keeps `allows_raw_publication = True`, per docs/21 §8's general attribution
    row ("everything, at lag, with the credit line rendered")."""
    entry = SourceEntry.from_yaml(
        {
            "id": "test.attribution.credit_only",
            "name": "Test Credit-Only Register",
            "category": "generation_queue",
            "access": "api",
            "reuse": "attribution",
            "cadence": "weekly",
            "license": 'Open licence: "free to exploit commercially and non-commercially" with credit.',
            "notes": 'Mandatory exact attribution string "Supported by Test Register"; no reuse restriction.',
        }
    )
    src = upsert_licence_and_source(session, entry, "2026-09-12")
    assert src.licence.allows_raw_publication is True


def test_source_publish_state_defaults_from_registry_class(session: Session) -> None:
    """Sprint 2 fix (services/README.md, supersedes the sprint-1 open decision #5 default): a
    gate-cleared source (its `reuse` already known `open`/`attribution` by the time this runs --
    `_assert_not_gated` and `upsert_licence_and_source`'s own "both must hold" check both raise
    first) loads straight to `publish_state = "public"`, matching `docs/21` §5.4's
    `source_permits` gate, instead of the old `api_only` default that needed a separate
    admin-style flip (`web/data_loading.py::_flip_publish_state_public`'s workaround, now
    unnecessary)."""
    open_src = upsert_licence_and_source(session, open_source_entry("us.test.pub_state_open"), "2026-09-12")
    assert open_src.publish_state == "public"

    attribution_entry = open_source_entry("us.test.pub_state_attr")
    attribution_entry.reuse = "attribution"
    attr_src = upsert_licence_and_source(session, attribution_entry, "2026-09-12")
    assert attr_src.publish_state == "public"


# --------------------------------------------------------------------------- geocoding (Sprint 2)
def test_geocode_resolves_known_county_to_its_centroid() -> None:
    point, precision = geocode("TX", "Travis")
    assert precision == "county_centroid"
    assert point is not None
    lon, lat = point
    assert -99 < lon < -96  # Travis County, TX is roughly here
    assert 29 < lat < 31


def test_geocode_falls_back_to_state_centroid_when_county_unresolvable() -> None:
    point, precision = geocode("TX", "Not A Real County")
    assert precision == "state_centroid"
    assert point is not None


def test_geocode_is_unknown_with_neither_state_nor_county_resolvable() -> None:
    point, precision = geocode("ZZ", "Nowhere County")
    assert point is None
    assert precision == "unknown"


def test_geocode_nyiso_borough_alias_resolves(session: Session) -> None:
    """`_COUNTY_ALIASES` maps NYISO's free-text borough names to their Gazetteer county (module
    docstring in services/ingest/geocode.py)."""
    point, precision = geocode("NY", "Brooklyn")
    assert precision == "county_centroid"
    assert point is not None


def test_loader_geocodes_proposal_location_from_state_and_county(session: Session) -> None:
    """services/README.md open decision #2, fixed: `services/ingest/loader.py` now geocodes a
    connector-parsed state/county pair through `services/ingest/geocode.py` instead of leaving
    `location.geom` null for every row."""
    entry = open_source_entry("us.test.geocoded_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")

    row = sample_proposal_row("G1")
    row["source_id"] = entry.id
    row["record_id"] = f"{entry.id}:G1"
    row["state"] = "TX"
    row["county"] = "Travis"
    df = pd.DataFrame([row])
    load_dataframe(session, src, "proposal", df, None)

    proposal = session.scalar(select(Proposal))
    assert proposal is not None and proposal.location_id is not None
    loc = session.get(Location, proposal.location_id)
    assert loc is not None
    assert loc.precision == "county_centroid"
    assert loc.geom is not None
    assert loc.precision_reason is None  # ERCOT-style open source: no restricted-precision reason


def test_loader_falls_back_to_state_centroid_for_unresolvable_county(session: Session) -> None:
    entry = open_source_entry("us.test.state_fallback_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")

    row = sample_proposal_row("G2")
    row["source_id"] = entry.id
    row["record_id"] = f"{entry.id}:G2"
    row["state"] = "TX"
    row["county"] = "Not A Real County"
    df = pd.DataFrame([row])
    load_dataframe(session, src, "proposal", df, None)

    proposal = session.scalar(select(Proposal))
    assert proposal is not None
    loc = session.get(Location, proposal.location_id)
    assert loc is not None
    assert loc.precision == "state_centroid"
    assert loc.geom is not None


def test_loader_stamps_licence_precision_reason_for_derived_only_sources(session: Session) -> None:
    """docs/04 D-9: a derived-only source (module docstring's `_is_derived_only_override`) never
    gets to imply an exact point, and its locations carry `precision_reason = "licence"` so the API
    can render the restricted-precision note -- CAISO/NYISO in production, a synthetic equivalent
    here so this test does not depend on those two real counties existing in the same shape."""
    entry = SourceEntry.from_yaml(
        {
            "id": "test.derived_only.queue",
            "name": "Test Derived-Only Queue",
            "category": "generation_queue",
            "access": "bulk_file",
            "reuse": "attribution",
            "cadence": "weekly",
            "license": "Test Terms of Use.",
            "notes": "Publish derived-only until counsel resolves the tension between two clauses.",
        }
    )
    src = upsert_licence_and_source(session, entry, "2026-09-12")
    assert src.licence.allows_raw_publication is False

    row = sample_proposal_row("D1")
    row["source_id"] = entry.id
    row["record_id"] = f"{entry.id}:D1"
    row["state"] = "TX"
    row["county"] = "Travis"
    df = pd.DataFrame([row])
    load_dataframe(session, src, "proposal", df, None)

    proposal = session.scalar(select(Proposal))
    assert proposal is not None
    loc = session.get(Location, proposal.location_id)
    assert loc is not None
    assert loc.precision == "county_centroid"
    assert loc.precision_reason == "licence"


def test_county_gazetteer_normalizes_suffixes_and_case() -> None:
    gaz = CountyGazetteer.load()
    assert gaz.county_point("TX", "TRAVIS COUNTY") == gaz.county_point("TX", "travis")
    assert gaz.state_point("TX") is not None


# --------------------------------------------------------------- county_fips (added 2026-09-15)
def test_loader_sets_county_fips_for_a_county_centroid_location(session: Session) -> None:
    entry = open_source_entry("us.test.fips_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")

    row = sample_proposal_row("F1")
    row["source_id"] = entry.id
    row["record_id"] = f"{entry.id}:F1"
    row["state"] = "TX"
    row["county"] = "Travis"
    df = pd.DataFrame([row])
    load_dataframe(session, src, "proposal", df, None)

    proposal = session.scalar(select(Proposal))
    assert proposal is not None
    loc = session.get(Location, proposal.location_id)
    assert loc is not None
    assert loc.precision == "county_centroid"
    assert loc.county_fips == "48453"


def test_loader_leaves_county_fips_null_for_an_unresolvable_county(session: Session) -> None:
    entry = open_source_entry("us.test.fips_unresolved_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")

    row = sample_proposal_row("F2")
    row["source_id"] = entry.id
    row["record_id"] = f"{entry.id}:F2"
    row["state"] = "TX"
    row["county"] = "Not A Real County"
    df = pd.DataFrame([row])
    load_dataframe(session, src, "proposal", df, None)

    proposal = session.scalar(select(Proposal))
    assert proposal is not None
    loc = session.get(Location, proposal.location_id)
    assert loc is not None
    assert loc.precision == "state_centroid"
    assert loc.county_fips is None


# ------------------------------------------------------- EIA exact-point promotion (Sprint 3)
def eia_row(
    record_id: str,
    *,
    lat: object = 39.8536,
    lon: object = -119.0394,
    state: str = "NV",
    county: str = "Churchill",
) -> dict[str, object]:
    """An EIA-860M-shaped row: same normalised columns `sample_proposal_row` produces, but with a
    `raw` payload carrying the connector's real `Latitude`/`Longitude` keys (verified against
    `pipeline.connectors.us_eia_860m.connector.Connector.parse`'s output over
    `tests/fixtures/eia860m_planned.xlsx` -- see `test_eia_fixture_row_promotes_through_the_real_
    connector_output` below for the real thing end to end)."""
    row = sample_proposal_row(record_id)
    row["source_id"] = "us.eia.860m"
    row["record_id"] = f"us.eia.860m:{record_id}"
    row["state"] = state
    row["county"] = county
    row["raw"] = json.dumps({"Plant ID": record_id, "Latitude": lat, "Longitude": lon})
    return row


def eia_source(session: Session, id_: str = "us.eia.860m") -> Source:
    entry = open_source_entry(id_)
    entry.category = "generator_inventory"
    return upsert_licence_and_source(session, entry, "2026-09-12")


def test_row_with_valid_coordinates_is_promoted_to_exact(session: Session) -> None:
    src = eia_source(session)
    df = pd.DataFrame([eia_row("E1")])
    result = load_dataframe(session, src, "proposal", df, None)

    assert result.locations_created == 1
    assert result.locations_exact_promoted == 1
    proposal = session.scalar(select(Proposal))
    assert proposal is not None
    loc = session.get(Location, proposal.location_id)
    assert loc is not None
    assert loc.precision == "exact"
    assert loc.kind == "point"
    assert loc.geocoder == "source_provided"
    assert loc.geom == (-119.0394, 39.8536)  # (lon, lat), matching services/ingest/geocode.py
    assert loc.precision_reason is None
    # county_fips (added 2026-09-15): an exact-point row still names a real county (NV/Churchill,
    # `eia_row`'s default) alongside its coordinate, so it gets a FIPS too -- looked up from that
    # county name, never from the coordinate itself (docs/21 §3.7).
    assert loc.county_fips == "32001"


@pytest.mark.parametrize(
    "lat,lon",
    [
        (0.0, 0.0),  # the common unset-field placeholder, not a real point
        (95.0, -119.0394),  # out of world bounds
        (39.8536, -190.0),  # out of world bounds
        ("not-a-number", -119.0394),  # non-numeric
        (None, -119.0394),  # missing half of the pair
    ],
)
def test_row_with_invalid_coordinates_falls_back_and_is_not_promoted(
    session: Session, lat: object, lon: object
) -> None:
    src = eia_source(session)
    df = pd.DataFrame([eia_row("E2", lat=lat, lon=lon)])
    result = load_dataframe(session, src, "proposal", df, None)

    assert result.locations_created == 1
    assert result.locations_exact_promoted == 0
    proposal = session.scalar(select(Proposal))
    assert proposal is not None
    loc = session.get(Location, proposal.location_id)
    assert loc is not None
    assert loc.precision == "county_centroid"  # NV/Churchill resolves -- module docstring above
    assert loc.geom is not None


def test_row_without_raw_coordinates_falls_back_to_county_geocoding_unchanged(session: Session) -> None:
    """A non-EIA row with no `Latitude`/`Longitude` on its raw payload is entirely unaffected by
    this promotion -- the pre-existing county/state geocoding path, byte-for-byte."""
    entry = open_source_entry("us.test.no_coords_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")
    row = sample_proposal_row("NC1")
    row["state"] = "TX"
    row["county"] = "Travis"
    df = pd.DataFrame([row])
    result = load_dataframe(session, src, "proposal", df, None)

    assert result.locations_exact_promoted == 0
    proposal = session.scalar(select(Proposal))
    assert proposal is not None
    loc = session.get(Location, proposal.location_id)
    assert loc is not None
    assert loc.precision == "county_centroid"
    assert loc.geocoder is None


def test_derived_only_source_is_never_promoted_even_with_valid_raw_coordinates(session: Session) -> None:
    """docs/04 D-9: an exact coordinate is a raw field, withheld exactly like any other for a
    derived-only source -- valid `Latitude`/`Longitude` on the raw payload must not promote it."""
    entry = SourceEntry.from_yaml(
        {
            "id": "test.derived_only.eia_like_queue",
            "name": "Test Derived-Only EIA-Like Queue",
            "category": "generation_queue",
            "access": "bulk_file",
            "reuse": "attribution",
            "cadence": "weekly",
            "license": "Test Terms of Use.",
            "notes": "Publish derived-only until counsel resolves the tension between two clauses.",
        }
    )
    src = upsert_licence_and_source(session, entry, "2026-09-12")
    row = eia_row("E3")
    row["source_id"] = entry.id
    row["record_id"] = f"{entry.id}:E3"
    df = pd.DataFrame([row])
    result = load_dataframe(session, src, "proposal", df, None)

    assert result.locations_exact_promoted == 0
    proposal = session.scalar(select(Proposal))
    assert proposal is not None
    loc = session.get(Location, proposal.location_id)
    assert loc is not None
    assert loc.precision == "county_centroid"
    assert loc.precision_reason == "licence"


def test_rerunning_the_same_eia_frame_is_idempotent(session: Session) -> None:
    src = eia_source(session)
    df = pd.DataFrame([eia_row("E4")])
    result1 = load_dataframe(session, src, "proposal", df, None)
    result2 = load_dataframe(session, src, "proposal", df, None)

    assert result1.locations_exact_promoted == 1
    assert result2.proposals_created == 0
    assert result2.proposals_updated == 1
    assert result2.locations_exact_promoted == 0  # nothing new created on the update path
    assert session.scalar(select(func.count()).select_from(Location)) == 1
    loc = session.scalar(select(Location))
    assert loc is not None and loc.precision == "exact"


def test_exact_promotion_batch_size_does_not_change_what_gets_written() -> None:
    """The bulk-insert pass's invariant (`test_batch_size_does_not_change_what_gets_written` above)
    extended to exact-point promotion: `batch_size=1` and `batch_size=500` must produce the same
    locations for a frame mixing promoted and non-promoted EIA rows."""
    rows = [
        eia_row("BSE1"),
        eia_row("BSE2", lat=0.0, lon=0.0),  # invalid -- falls back
        eia_row("BSE3", lat=31.19371, lon=-102.3159, state="TX", county="Crane"),
    ]
    df = pd.DataFrame(rows)

    digests: dict[int, str] = {}
    for batch_size in (1, 500):
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        session_factory = get_sessionmaker(engine)
        with session_factory() as s:
            src = eia_source(s)
            load_dataframe(s, src, "proposal", df, None, batch_size=batch_size)
            digests[batch_size] = _store_digest(s)

    assert digests[1] == digests[500]


def test_eia_fixture_row_promotes_through_the_real_connector_output(session: Session) -> None:
    """End to end through the real connector parse (not a hand-built `raw` payload): the fixture's
    first "Planned" row (Sierra Solar Hybrid, NV) carries real `Latitude`/`Longitude` and must come
    out `exact`."""
    from conftest import connector_for, snapshot

    entry = open_source_entry("us.eia.860m")
    src = upsert_licence_and_source(session, entry, "2026-09-12")

    connector = connector_for("us.eia.860m")
    raw = snapshot(
        "eia860m_planned.xlsx",
        "https://www.eia.gov/electricity/data/eia860m/xls/july_generator2026.xlsx",
    )
    rows = connector.parse(raw)
    df = connector.normalize(rows, raw)

    result = load_dataframe(session, src, "proposal", df, None)

    assert result.locations_exact_promoted >= 1
    exact_locations = session.scalars(select(Location).where(Location.precision == "exact")).all()
    assert len(exact_locations) == result.locations_exact_promoted
    for loc in exact_locations:
        assert loc.kind == "point"
        assert loc.geocoder == "source_provided"
        lon, lat = loc.geom
        assert -180.0 <= lon <= 180.0
        assert -90.0 <= lat <= 90.0


# ------------------------------------------------------- bulk-insert pass (Sprint 3, batch_size)
def _store_digest(session: Session) -> str:
    """A deterministic content digest of everything `load_dataframe` wrote: proposals (with their
    links, sponsor and location), organisations (with their aliases) and events. Excludes every
    wall-clock-derived column (`published_at`, `public_at`, `last_changed`, `created_at`,
    `updated_at`, `recorded_at`) and every randomly/time-generated primary key (`id`, `public_id`,
    `slug`'s collision-suffix case), so two independent loads of the same rows produce the same
    digest regardless of `batch_size` or of the wall-clock moment each ran at -- proving
    `batch_size` changes only flush cadence, never what gets written (docs/00-PLAN.md's "measured
    numbers only" rule)."""
    proposals: list[dict[str, Any]] = []
    for p in session.scalars(select(Proposal).order_by(Proposal.name_canonical)):
        links = sorted(
            (
                {
                    "source_record_id": link.source_record_id,
                    "source_url": link.source_url,
                    "raw": link.raw,
                    "normalised": link.normalised,
                    "status_raw": link.status_raw,
                    "link_method": link.link_method,
                    "link_confidence": float(link.link_confidence),
                    "active": link.active,
                    "gone_at": link.gone_at.isoformat() if link.gone_at else None,
                }
                for link in session.scalars(select(ProposalSource).where(ProposalSource.proposal_id == p.id))
            ),
            key=lambda d: str(d["source_record_id"]),
        )
        proposals.append(
            {
                "name_canonical": p.name_canonical,
                "kind": p.kind,
                "technology": p.technology,
                "technology_raw": p.technology_raw,
                "capacity_mw": float(p.capacity_mw) if p.capacity_mw is not None else None,
                "storage_mwh": float(p.storage_mwh) if p.storage_mwh is not None else None,
                "jurisdiction": p.jurisdiction,
                "iso": p.iso,
                "lifecycle_state": p.lifecycle_state,
                "status_raw": p.status_raw,
                "identifiers": p.identifiers,
                "proposed_online_date": (
                    p.proposed_online_date.isoformat() if p.proposed_online_date else None
                ),
                "publish_state": p.publish_state,
                "min_reuse_class": p.min_reuse_class,
                "source_count": p.source_count,
                "sponsor": p.sponsor.name_canonical if p.sponsor else None,
                "location": (
                    {
                        "state_code": p.location.state_code,
                        "county_name": p.location.county_name,
                        "precision": p.location.precision,
                        "precision_reason": p.location.precision_reason,
                    }
                    if p.location
                    else None
                ),
                "links": links,
            }
        )

    organizations: list[dict[str, Any]] = []
    for org in session.scalars(select(Organization).order_by(Organization.name_normalised)):
        aliases = sorted(
            (a.alias, a.alias_normalised, a.kind)
            for a in session.scalars(
                select(OrganizationAlias).where(OrganizationAlias.organization_id == org.id)
            )
        )
        organizations.append(
            {
                "name_canonical": org.name_canonical,
                "name_normalised": org.name_normalised,
                "type": org.type,
                "country": org.country,
                "aliases": aliases,
            }
        )

    events = sorted(
        (
            {
                "event_type": e.event_type,
                "idempotency_key": e.idempotency_key,
                "before": e.before,
                "after": e.after,
                "changed_keys": e.changed_keys,
            }
            for e in session.scalars(select(Event))
        ),
        key=lambda d: str(d["idempotency_key"]),
    )

    snapshot = {"proposals": proposals, "organizations": organizations, "events": events}
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()


def test_batch_size_does_not_change_what_gets_written() -> None:
    """The bulk-insert pass's whole point: `batch_size` only changes how often pending writes are
    flushed, never which rows, ids' content, links, organisations or events end up written.
    Proven by loading the same rows into two independent, fresh stores -- one with `batch_size=1`
    (flush every row, the pre-optimisation cadence) and one with `batch_size=500` (the new
    default) -- and comparing `_store_digest` between them."""
    rows = [sample_proposal_row("BS1"), sample_proposal_row("BS2"), sample_proposal_row("BS3")]
    rows[0]["sponsor_name"] = "CED Development, Inc."
    rows[1]["sponsor_name"] = "Ced Development Inc"  # punctuation-only alias of row 0's sponsor
    rows[2]["sponsor_name"] = "Acme Power LLC"
    rows[2]["state"] = "TX"
    rows[2]["county"] = "Travis"
    df = pd.DataFrame(rows)
    events_df = pd.DataFrame(
        [
            {
                "event_type": "status_change",
                "record_id": "us.test.open_queue:BS1",
                "source_id": "us.test.open_queue",
                "field": "lifecycle_state",
                "before": "filed",
                "after": "studied",
                "observed_at": "2026-09-12T06:00:00Z",
            }
        ]
    )

    digests: dict[int, str] = {}
    for batch_size in (1, 500):
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        session_factory = get_sessionmaker(engine)
        with session_factory() as s:
            src = upsert_licence_and_source(s, open_source_entry(), "2026-09-12")
            load_dataframe(s, src, "proposal", df, events_df, batch_size=batch_size)
            digests[batch_size] = _store_digest(s)

    assert digests[1] == digests[500]


def test_rerun_with_default_batch_size_is_idempotent(session: Session) -> None:
    """The Sprint 3 default `batch_size` (500) doesn't change idempotency: re-loading the same
    frame must not create duplicate proposals, links or events, exactly as the pre-optimisation
    per-row cadence already guaranteed (`test_load_dataframe_is_idempotent`,
    `test_load_dataframe_with_events_idempotent` above)."""
    entry = open_source_entry("us.test.batch_idempotent_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")
    df = pd.DataFrame([sample_proposal_row("BI1"), sample_proposal_row("BI2")])
    events_df = pd.DataFrame(
        [
            {
                "event_type": "status_change",
                "record_id": "us.test.open_queue:BI1",
                "source_id": entry.id,
                "field": "lifecycle_state",
                "before": "filed",
                "after": "studied",
                "observed_at": "2026-09-12T06:00:00Z",
            }
        ]
    )

    load_dataframe(session, src, "proposal", df, events_df)  # batch_size defaults to 500
    load_dataframe(session, src, "proposal", df, events_df)

    assert session.scalar(select(func.count()).select_from(Proposal)) == 2
    assert session.scalar(select(func.count()).select_from(ProposalSource)) == 2
    assert session.scalar(select(func.count()).select_from(Event)) == 1


def test_gate_refusal_writes_nothing_to_the_store(session: Session) -> None:
    """The licence gate raises before touching the store at all (module docstring's "both must
    hold" invariant) -- not merely that `GateRefused` is raised, but that no `source`/`licence`
    row exists afterward either, matching CLAUDE.md's "restricted source cannot enter the store"
    guardrail literally."""
    entry = restricted_source_entry()
    with pytest.raises(GateRefused):
        upsert_licence_and_source(session, entry, "2026-09-12")
    assert session.scalar(select(func.count()).select_from(Source)) == 0
    assert session.scalar(select(func.count()).select_from(Licence)) == 0
    assert session.scalar(select(func.count()).select_from(Proposal)) == 0


def gb_source_entry(id_: str = "gb.test.tec_register") -> SourceEntry:
    return SourceEntry.from_yaml(
        {
            "id": id_,
            "name": "Test TEC Register",
            "jurisdiction": "GB",
            "category": "generation_queue",
            "operator": "Test SO",
            "url": "https://example.org/tec",
            "access": "bulk_file",
            "reuse": "attribution",
            "cadence": "weekly",
            "tier": 1,
            "license": "Test open licence with attribution",
        }
    )


def test_loader_places_gb_rows_at_their_connection_substation(session: Session) -> None:
    """Sprint 3 item 5: a NESO TEC row carries no state and a transmission "Connection Site" in
    `county`; the loader passes the source's GB jurisdiction to `geocode`, which resolves the site
    against the vendored NESO gazetteer. A hit is a substation-level proxy (`county_centroid`,
    geocoder `gb_substation`), never `exact`; a miss stays unplaced, never guessed."""
    entry = gb_source_entry()
    src = upsert_licence_and_source(session, entry, "2026-09-13")

    placed_row = sample_proposal_row("T1")
    placed_row.update(
        source_id=entry.id,
        record_id=f"{entry.id}:T1",
        name_canonical="Aberthaw Battery",
        name_norm="aberthaw battery",
        state=None,
        county="Aberthaw",
    )
    missed_row = sample_proposal_row("T2")
    missed_row.update(
        source_id=entry.id,
        record_id=f"{entry.id}:T2",
        name_canonical="Node Battery",
        name_norm="node battery",
        state=None,
        county="Connection Node 4711",
    )
    load_dataframe(session, src, "proposal", pd.DataFrame([placed_row, missed_row]), None)

    placed = session.scalar(select(Proposal).where(Proposal.name_canonical == "Aberthaw Battery"))
    assert placed is not None and placed.location_id is not None
    loc = session.get(Location, placed.location_id)
    assert loc is not None
    assert loc.country == "GB" and loc.state_code is None
    assert loc.precision == "county_centroid" and loc.geocoder == "gb_substation"
    assert loc.geom is not None
    lon, lat = loc.geom
    assert 51 < lat < 52 and -4 < lon < -3  # Aberthaw, south Wales

    missed = session.scalar(select(Proposal).where(Proposal.name_canonical == "Node Battery"))
    assert missed is not None and missed.location_id is not None
    loc2 = session.get(Location, missed.location_id)
    assert loc2 is not None
    assert loc2.precision == "unknown" and loc2.geom is None and loc2.geocoder is None


# ------------------------------------------------ county_fips backfill CLI (added 2026-09-15)
def test_backfill_county_fips_fills_resolvable_rows_and_is_idempotent(session: Session) -> None:
    """`services.ingest.geocode.backfill_county_fips`: existing rows are get-or-create (module
    docstring), so a defect fixed after rows already exist needs a standalone backfill -- this
    fills every row where `county_fips` is NULL and resolvable, leaves an unresolvable one NULL,
    and a second run over the same data fills nothing more."""
    from services.ingest.geocode import backfill_county_fips

    entry = open_source_entry("us.test.backfill_queue")
    src = upsert_licence_and_source(session, entry, "2026-09-12")

    resolvable = sample_proposal_row("B1")
    resolvable["source_id"] = entry.id
    resolvable["record_id"] = f"{entry.id}:B1"
    resolvable["state"] = "TX"
    resolvable["county"] = "Travis"

    unresolvable = sample_proposal_row("B2")
    unresolvable["source_id"] = entry.id
    unresolvable["record_id"] = f"{entry.id}:B2"
    unresolvable["state"] = "TX"
    unresolvable["county"] = "Not A Real County"

    load_dataframe(session, src, "proposal", pd.DataFrame([resolvable, unresolvable]), None)

    # Simulate the pre-fix state: every row loaded before this defect was fixed has
    # `county_fips IS NULL` regardless of whether it would have resolved.
    for loc in session.scalars(select(Location)):
        loc.county_fips = None
    session.commit()

    result1 = backfill_county_fips(session)
    assert result1["rows_seen"] == 2
    assert result1["filled"] == 1
    assert result1["unresolved"] == 1
    assert result1["by_precision"]["county_centroid"] == {"seen": 1, "filled": 1}
    assert result1["by_precision"]["state_centroid"] == {"seen": 1, "filled": 0}

    locs = {loc.precision: loc for loc in session.scalars(select(Location))}
    assert locs["county_centroid"].county_fips == "48453"
    assert locs["state_centroid"].county_fips is None

    # Idempotent: nothing left to fill, so a second run touches 0 rows.
    result2 = backfill_county_fips(session)
    assert result2["rows_seen"] == 1  # only the still-unresolved row remains NULL
    assert result2["filled"] == 0
    assert result2["unresolved"] == 1


def test_publication_field_is_explicit_and_none_is_refused(session: Session) -> None:
    """Blockers sprint 2026-09-19: the derived-only rule is the manifest's explicit `publication`
    field, not a regex over notes. `derived_only` withholds raw regardless of notes text; `raw_ok`
    allows raw even when the notes mention the phrase; `none` refuses the load outright; an
    unknown value refuses; an entry without the field falls back to the regex with a warning."""
    import dataclasses

    import pytest

    from services.ingest.loader import GateRefused, _is_derived_only_override

    registry = Registry()
    neso = registry.get("gb.neso.tec_register")

    assert _is_derived_only_override(dataclasses.replace(neso, publication="derived_only")) is True
    assert (
        _is_derived_only_override(
            dataclasses.replace(neso, publication="raw_ok", notes="terms say derived-only but the field wins")
        )
        is False
    )
    with pytest.raises(GateRefused):
        _is_derived_only_override(dataclasses.replace(neso, publication="sometimes"))
    with pytest.raises(GateRefused):
        upsert_licence_and_source(session, dataclasses.replace(neso, publication="none"), registry.version)
    legacy = dataclasses.replace(neso, publication=None, notes="Publish derived-only until counsel resolves")
    assert _is_derived_only_override(legacy) is True
