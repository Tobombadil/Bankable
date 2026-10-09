"""`services/resolve/closeout.py`: a queue request closed out against the operating EIA plant it
became (docs/22 §23.8; interconnection analyst review 2026-10-07: MONTEZUMA II "Contracted,
overdue by 14.7 years" beside the operating Montezuma Wind II)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Asset, Event, Proposal, ProposalSource, new_uuid
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.resolve import closeout

QUEUE = "us.iso.caiso.gen_queue"
PLANTS = "us.eia.860m"


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    s = get_sessionmaker(engine)()
    try:
        yield s
    finally:
        s.close()


def _entry(source_id: str, category: str) -> SourceEntry:
    return SourceEntry.from_yaml(
        {
            "id": source_id,
            "name": source_id,
            "jurisdiction": "US-CA",
            "category": category,
            "operator": "Test",
            "url": f"https://example.org/{source_id}",
            "access": "bulk_file",
            "reuse": "open",
            "publication": "raw_ok",
            "cadence": "weekly",
            "tier": 1,
            "license": "US federal public domain",
        }
    )


def _request(record_id: str, name: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "record_id": f"{QUEUE}:{record_id}",
        "source_id": QUEUE,
        "source_record_id": record_id,
        "source_url": "https://example.org/queue.xlsx",
        "retrieved_at": "2026-09-13T20:25:21Z",
        "kind": "generation",
        "name_canonical": name,
        "technology": "wind_storage",
        "capacity_mw": 78.0,
        "iso": "CAISO",
        "state": "CA",
        "county": "Solano",
        "lifecycle_state": "contracted",
        "status_raw": "ACTIVE",
        "queue_id": record_id,
        "queue_date": "2007-01-31",
        "proposed_cod": "2012-01-29",
        "raw": "{}",
    }
    row.update(overrides)
    return row


def _plant(session: Session, plant_id: str, name: str, **overrides: Any) -> Asset:
    fields: dict[str, Any] = {
        "asset_type": "power_plant",
        "source_asset_id": plant_id,
        "name": name,
        "status": "operating",
        "technology": "wind",
        "technology_raw": "Onshore Wind Turbine",
        "technologies": {"Onshore Wind Turbine": 78.2},
        "capacity_mw": 78.2,
        "commissioned_year": 2012,
        "state_code": "US-CA",
        "county_name": "Solano",
        "country": "US",
        "source_id": PLANTS,
        "source_url": "https://www.eia.gov/electricity/data/eia860m/",
        "retrieved_at": dt.datetime(2026, 10, 7, 10, 36, tzinfo=dt.UTC),
        "licence_id": _licence(session),
    }
    fields.update(overrides)
    asset_id = new_uuid()
    asset = Asset(id=asset_id, public_id=public_id("ast", asset_id), slug=f"plant-{plant_id}", **fields)
    session.add(asset)
    session.flush()
    return asset


def _licence(session: Session) -> str:
    return upsert_licence_and_source(session, _entry(PLANTS, "registry"), "v").licence_id


def _load(session: Session, *rows: dict[str, Any]) -> None:
    source = upsert_licence_and_source(session, _entry(QUEUE, "generation_queue"), "v")
    load_dataframe(session, source, "proposal", pd.DataFrame(list(rows)), None)
    session.commit()


def _record(session: Session, record_id: str) -> Proposal:
    link = session.scalar(
        select(ProposalSource).where(
            ProposalSource.source_id == QUEUE, ProposalSource.source_record_id == record_id
        )
    )
    assert link is not None
    proposal = session.get(Proposal, link.proposal_id)
    assert proposal is not None
    return proposal


def test_a_stale_request_closes_out_against_its_operating_plant(session: Session) -> None:
    _load(session, _request("1037", "MONTEZUMA II"))
    _plant(session, "57701", "Montezuma Wind II")
    _plant(session, "57201", "FPL Energy Montezuma Winds LLC", capacity_mw=36.8, commissioned_year=2010)
    record = _record(session, "1037")
    assert record.lifecycle_state == "contracted"

    report = closeout.close_out(session)
    assert report.linked == 1 and report.records_built == 1
    assert record.lifecycle_state == "built"
    assert record.field_provenance["lifecycle_state"]["rule"] == "operating_plant_outranks_queue"
    assert record.field_provenance["actual_cod"]["value"] == "2012"
    assert record.field_provenance["actual_cod"]["source_id"] == PLANTS
    assert record.identifiers["eia_plant_id"] == "57701"
    assert float(record.capacity_mw or 0) == 78.0  # the request stays the grid-connection figure
    assert record.source_count == 2

    link = session.scalar(select(ProposalSource).where(ProposalSource.source_record_id == "plant:57701"))
    assert link is not None and link.link_method == "rule" and link.active
    event = session.get(Event, link.link_event_id)
    assert event is not None and event.event_type == "source_linked"
    assert event.published_at is None  # resolver events are never published
    assert event.source_id and event.licence_id and event.source_url and event.retrieved_at

    again = closeout.close_out(session)
    assert again.linked == 0 and again.candidates == 0


def test_an_unlink_restores_the_record_and_the_pair_is_never_relinked(session: Session) -> None:
    _load(session, _request("1037", "MONTEZUMA II"))
    _plant(session, "57701", "Montezuma Wind II")
    closeout.close_out(session)
    record = _record(session, "1037")
    link = closeout.operating_links(session, [record.id])[0]

    event = closeout.unlink_operating_plant(session, link, reason="reviewed: not the same project")
    assert event.event_type == "source_unlinked" and event.reverses_event_id == link.link_event_id
    assert record.lifecycle_state == "contracted"
    assert "actual_cod" not in record.field_provenance
    assert record.source_count == 1
    assert closeout.close_out(session).linked == 0


@pytest.mark.parametrize(
    ("request_overrides", "plant_overrides", "why"),
    [
        # A storage request beside an operating solar plant of the same name is its add-on.
        (
            {"name_canonical": "Long Point Storage", "technology": "storage"},
            {
                "name": "Long Point Solar",
                "technology": "solar",
                "technologies": {"Solar Photovoltaic": 120.0},
            },
            "technology",
        ),
        # A repower request at an old plant: its proposed date is years after the plant's first year.
        ({"proposed_cod": "2030-06-01", "queue_date": "2009-01-01"}, {}, "cod"),
        # A different phase.
        ({"name_canonical": "MONTEZUMA III"}, {}, "phase"),
        # No agreement and not overdue.
        (
            {"lifecycle_state": "studied", "proposed_cod": "2028-01-01", "queue_date": "2001-01-01"},
            {"commissioned_year": 2026},
            "agreement",
        ),
    ],
)
def test_the_rule_refuses_add_ons_repowers_phases_and_early_requests(
    session: Session, request_overrides: dict[str, Any], plant_overrides: dict[str, Any], why: str
) -> None:
    _load(session, _request("1037", "MONTEZUMA II", **request_overrides))
    _plant(session, "57701", plant_overrides.pop("name", "Montezuma Wind II"), **plant_overrides)
    report = closeout.close_out(session)
    assert report.linked == 0, why
    assert _record(session, "1037").lifecycle_state != "built"


def test_one_request_against_two_matching_plants_links_neither(session: Session) -> None:
    _load(
        session,
        _request(
            "2001",
            "ARATINA SOLAR CENTER 1",
            technology="solar_storage",
            capacity_mw=200.0,
            queue_date="2015-01-01",
            proposed_cod="2026-06-01",
        ),
    )
    techs = {"Solar Photovoltaic": 100.0, "Batteries": 30.0}
    _plant(
        session,
        "1",
        "Aratina Solar Center 1A",
        technology="solar",
        technologies=techs,
        capacity_mw=195.0,
        commissioned_year=2026,
    )
    _plant(
        session,
        "2",
        "Aratina Solar Center 1B",
        technology="solar",
        technologies=techs,
        capacity_mw=130.0,
        commissioned_year=2026,
    )
    report = closeout.close_out(session)
    assert report.linked == 0 and report.ambiguous == 2
