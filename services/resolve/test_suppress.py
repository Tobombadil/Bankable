"""Reviewed suppressions unpublish verified non-data-centres from EPA ICIS-Air, once, with a reason
(audit 2026-09-30, data scientist F8; `data/vendored/data_centres/icis_air_not_data_centres.yaml`).

Fixtures are duplicated from `services/ingest/test_loader_select_basis.py` rather than imported,
per this repo's convention that test modules do not import one another.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Event, Proposal, ProposalSource
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.resolve.merge import merge_proposal
from services.resolve.suppress import Suppression, apply_suppressions, load_suppressions

ICIS = "us.epa.echo.icis_air"
DEQ = "us.test.va_deq"


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _entry(id_: str) -> SourceEntry:
    return SourceEntry.from_yaml(
        {
            "id": id_,
            "name": f"Test {id_}",
            "jurisdiction": "US",
            "category": "permit",
            "operator": "Test agency",
            "url": "https://example.org/air",
            "access": "bulk_file",
            "reuse": "open",
            "cadence": "weekly",
            "tier": 1,
            "license": "US federal public domain",
        }
    )


def _row(source_id: str, record_id: str, name: str, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": f"{source_id}:{record_id}",
        "source_id": source_id,
        "source_record_id": record_id,
        "source_url": f"https://example.org/air/{record_id}",
        "retrieved_at": "2026-09-29T05:00:00Z",
        "licence_id": f"{source_id}#irrelevant",
        "kind": "load",
        "name_canonical": name,
        "name_norm": name.lower(),
        "sponsor_name": None,
        "sponsor_norm": None,
        "technology": "load",
        "technology_raw": "Data Center",
        "capacity_mw": None,
        "storage_mwh": None,
        "iso": None,
        "state": "VA",
        "county": "Fairfax",
        "county_norm": "fairfax",
        "lifecycle_state": "built",
        "status_raw": "Operating",
        "queue_date": None,
        "proposed_cod": None,
        "queue_id": None,
        "eia_plant_id": None,
        "eia_generator_id": None,
        "raw": json.dumps(raw),
    }


def _load(session: Session, source_id: str, rows: list[dict[str, Any]]) -> None:
    source = upsert_licence_and_source(session, _entry(source_id), "test")
    load_dataframe(session, source, "proposal", pd.DataFrame(rows), None)
    session.commit()


def _by_name(session: Session) -> dict[str, Proposal]:
    return {p.name_canonical: p for p in session.scalars(select(Proposal)).all()}


@pytest.fixture()
def world(session: Session) -> dict[str, Proposal]:
    _load(
        session,
        ICIS,
        [
            _row(
                ICIS, "VA1", "OFFICE CAMPUS", {"REGISTRY_ID": "110000000001", "select_basis": "naics_518210"}
            ),
            _row(ICIS, "VA2", "SHARED SITE", {"REGISTRY_ID": "110000000002", "select_basis": "naics_518210"}),
            _row(ICIS, "VA3", "REAL DATA CENTER", {"REGISTRY_ID": "110000000003", "select_basis": "name"}),
        ],
    )
    _load(session, DEQ, [_row(DEQ, "D2", "SHARED SITE (DEQ)", {"PLA_ICIS_ID": "VA2"})])
    names = _by_name(session)
    merge_proposal(
        session,
        canonical=names["SHARED SITE"],
        absorbed=names["SHARED SITE (DEQ)"],
        score=100.0,
        rationale="D3",
    )
    session.commit()
    return _by_name(session)


LISTED = [
    Suppression(ICIS, "110000000001", "OFFICE CAMPUS", "Corporate office (hand-checked)."),
    Suppression(ICIS, "110000000002", "SHARED SITE", "Corporate office (hand-checked)."),
    Suppression(ICIS, "110000009999", "GONE", "Corporate office (hand-checked)."),
]


def test_an_icis_only_office_is_unpublished_with_a_reasoned_event(
    session: Session, world: dict[str, Proposal]
) -> None:
    report = apply_suppressions(session, LISTED)
    session.commit()
    office, shared, real = world["OFFICE CAMPUS"], world["SHARED SITE"], world["REAL DATA CENTER"]
    assert report.unpublished == [office.public_id]
    assert (office.publish_state, shared.publish_state, real.publish_state) == (
        "unpublished",
        "public",
        "public",
    )
    events = session.scalars(select(Event).where(Event.event_type == "unpublished")).all()
    assert len(events) == 1
    ev = events[0]
    assert ev.subject_id == office.id and ev.actor_type == "pipeline"
    assert ev.reason == "suppressed: Corporate office (hand-checked)."
    assert ev.before == {"publish_state": "public"}
    assert ev.after == {
        "publish_state": "unpublished",
        "suppression": {"source_id": ICIS, "registry_id": "110000000001"},
    }
    assert ev.published_at is None  # an editorial correction, not a change-feed item


def test_another_sources_support_keeps_the_record_and_a_stale_id_is_reported(
    session: Session, world: dict[str, Proposal]
) -> None:
    report = apply_suppressions(session, LISTED)
    assert report.kept_other_sources == [world["SHARED SITE"].public_id]
    assert report.not_found == ["110000009999"]


def test_it_runs_once_and_an_admin_republish_stands(session: Session, world: dict[str, Proposal]) -> None:
    apply_suppressions(session, LISTED)
    session.commit()
    office = world["OFFICE CAMPUS"]
    office.publish_state = "public"  # an admin re-publish
    session.commit()
    again = apply_suppressions(session, LISTED)
    session.commit()
    assert again.unpublished == [] and again.already_recorded == 1
    assert office.publish_state == "public"
    assert len(session.scalars(select(Event).where(Event.event_type == "unpublished")).all()) == 1


def test_the_source_link_and_its_raw_payload_are_kept(session: Session, world: dict[str, Proposal]) -> None:
    apply_suppressions(session, LISTED)
    link = session.scalars(select(ProposalSource).where(ProposalSource.source_record_id == "VA1")).one()
    assert link.active and link.raw["REGISTRY_ID"] == "110000000001"


def test_the_reviewed_file_lists_only_the_audited_offices_each_with_a_reason() -> None:
    listed = load_suppressions()
    assert {s.source_id for s in listed} == {ICIS}
    assert {s.registry_id: s.name for s in listed} == {
        "110040513496": "IBM DULLES STATION WEST",
        "110043433219": "NORTHROP GRUMMAN SYSTEMS CORPORATION",
        "110059984744": "US LIABILITY INS GROUP/WAYNE",
        "110035370504": "CONCORDANCE HEALTHCARE SOLUTIONS",
        "110054888298": "FCA US LLC",
        "110055591328": "HEALTH AND HOSPITAL CORPORATION",
    }
    assert all("docs/25" in s.reason for s in listed)
