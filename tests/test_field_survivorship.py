"""Field survivorship on a merged proposal, end to end through the loader, the merge, the served
view and the page (docs/22 §23; 2026-09-30 audit data scientist F2, designer D-5).

The fixture is the audit's own example in miniature: `darden-ii-solar-vdtde2` held one CAISO
request for a 1,150 MW solar-plus-storage hybrid and EIA-860M's two generators of one of its plants
(342.7 MW solar, 303.7 MW storage). The merge kept the EIA generator's record as survivor and
recomputed nothing, so the page showed 342.7 MW and credited CAISO with EIA's status.

These tests import only what existed before the fix (loader, merge, served view, API, page), so on
the base commit they fail on their assertions rather than on an import.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from pipeline.connectors.registry import SourceEntry
from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.visibility import gated_proposal, hidden_provenance_clause
from services.db.models import Event, Proposal, ProposalSource, Source
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.resolve.merge import merge_proposal, unmerge_proposal

QUEUE = "us.test.caiso"
INVENTORY = "us.test.eia860m"
EIA_STATUS = "(T) Regulatory approvals received. Not under construction"


def _entry(source_id: str, category: str, name: str) -> SourceEntry:
    return SourceEntry.from_yaml(
        {
            "id": source_id,
            "name": name,
            "jurisdiction": "US-CA",
            "category": category,
            "operator": "Test operator",
            "url": f"https://example.org/{source_id}",
            "access": "bulk_file",
            "reuse": "open",
            "publication": "raw_ok",
            "cadence": "weekly",
            "tier": 1,
            "license": "US federal public domain",
        }
    )


def _row(source_id: str, record_id: str, **values: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "record_id": f"{source_id}:{record_id}",
        "source_id": source_id,
        "source_record_id": record_id,
        "source_url": f"https://example.org/{source_id}/{record_id}",
        "retrieved_at": "2026-09-13T20:25:21Z",
        "kind": "generation",
        "name_canonical": None,
        "sponsor_name": None,
        "technology": None,
        "technology_raw": None,
        "capacity_mw": None,
        "storage_mwh": None,
        "iso": "CAISO",
        "state": "CA",
        "county": None,
        "lifecycle_state": "unknown",
        "status_raw": None,
        "proposed_cod": None,
        "queue_id": None,
        "eia_plant_id": None,
        "eia_generator_id": None,
        "raw": "{}",
    }
    row.update(values)
    return row


def _request(mw: float = 1150.0) -> dict[str, Any]:
    return _row(
        QUEUE,
        "1949",
        name_canonical="DARDEN",
        technology="solar_storage",
        technology_raw="Solar + Storage",
        capacity_mw=mw,
        lifecycle_state="contracted",
        status_raw="ACTIVE",
        proposed_cod="2027-07-19",
        queue_id="1949",
    )


def _generator(gen: str, tech: str, mw: float) -> dict[str, Any]:
    return _row(
        INVENTORY,
        f"69662-{gen}",
        retrieved_at="2026-09-27T15:15:28Z",
        name_canonical="Darden II Solar",
        technology=tech,
        technology_raw="Solar Photovoltaic" if tech == "solar" else "Batteries",
        capacity_mw=mw,
        lifecycle_state="permitted",
        status_raw=EIA_STATUS,
        proposed_cod="2028-03-01",
        eia_plant_id="69662",
        eia_generator_id=gen,
    )


def _sources(db: Session) -> tuple[Source, Source]:
    queue = upsert_licence_and_source(db, _entry(QUEUE, "generation_queue", "Test CAISO Queue"), "2026-10-07")
    inventory = upsert_licence_and_source(db, _entry(INVENTORY, "registry", "Test EIA-860M"), "2026-10-07")
    return queue, inventory


def _load(db: Session, source: Source, rows: list[dict[str, Any]]) -> None:
    load_dataframe(db, source, "proposal", pd.DataFrame(rows), None)
    db.commit()


def _record(db: Session, source_id: str, record_id: str) -> Proposal:
    link = db.scalar(
        select(ProposalSource).where(
            ProposalSource.source_id == source_id, ProposalSource.source_record_id == record_id
        )
    )
    assert link is not None
    prop = db.get(Proposal, link.proposal_id)
    assert prop is not None
    return prop


def _darden(db: Session) -> tuple[Proposal, Event, Event]:
    """Load the three rows as three records, then merge them the way the resolver did: the
    EIA-keyed generator survives (`choose_canonical`)."""
    queue, inventory = _sources(db)
    _load(db, queue, [_request()])
    _load(db, inventory, [_generator("IPD2S", "solar", 342.7), _generator("IPD2B", "storage", 303.7)])
    survivor = _record(db, INVENTORY, "69662-IPD2S")
    storage = _record(db, INVENTORY, "69662-IPD2B")
    request = _record(db, QUEUE, "1949")
    first = merge_proposal(db, canonical=survivor, absorbed=storage, score=100.0, rationale="D2 eia plant")
    second = merge_proposal(db, canonical=survivor, absorbed=request, score=92.0, rationale="P name+capacity")
    db.commit()
    db.expire_all()
    return _record(db, INVENTORY, "69662-IPD2S"), first, second


def _mw(value: Any) -> float | None:
    return round(float(value), 3) if value is not None else None


# ------------------------------------------------------------------------------------- the store
def test_a_merge_recomputes_every_field_from_all_members(db: Session) -> None:
    prop, _first, _second = _darden(db)

    # Capacity is the interconnection request, not one generator (342.7) nor the nameplate sum.
    assert _mw(prop.capacity_mw) == 1150.0
    assert prop.technology == "solar_storage"
    # The most advanced state any member reports, with that member's own status text and date.
    assert prop.lifecycle_state == "contracted"
    assert prop.status_raw == "ACTIVE"
    assert prop.proposed_online_date == dt.date(2027, 7, 19)
    # The plant inventory names the project; every member's keys survive.
    assert prop.name_canonical == "Darden II Solar"
    assert prop.identifiers["queue_ids"] == [{"iso": "CAISO", "id": "1949"}]
    assert prop.identifiers["eia_plant_id"] == "69662"
    assert {g["generator_id"] for g in prop.identifiers["eia_generators"]} == {"IPD2S", "IPD2B"}
    # Field-level provenance names the register that supplied each value.
    prov = prop.field_provenance
    assert prov["capacity_mw"]["source_id"] == QUEUE
    assert prov["lifecycle_state"]["source_id"] == QUEUE
    assert prov["status_raw"]["source_id"] == QUEUE
    assert prov["name_canonical"]["source_id"] == INVENTORY


def test_unmerge_restores_each_survivor_state_exactly(db: Session) -> None:
    prop, first, second = _darden(db)

    unmerge_proposal(db, second.id, reason="test")
    db.commit()
    db.expire_all()
    prop = _record(db, INVENTORY, "69662-IPD2S")
    # Back to the plant alone: the inventory's two generators summed, EIA's own status.
    assert _mw(prop.capacity_mw) == 646.4
    assert prop.lifecycle_state == "permitted"
    assert prop.status_raw == EIA_STATUS
    assert prop.proposed_online_date == dt.date(2028, 3, 1)
    assert "queue_ids" not in prop.identifiers
    request = _record(db, QUEUE, "1949")
    assert _mw(request.capacity_mw) == 1150.0
    assert request.lifecycle_state == "contracted"

    unmerge_proposal(db, first.id, reason="test")
    db.commit()
    db.expire_all()
    prop = _record(db, INVENTORY, "69662-IPD2S")
    assert _mw(prop.capacity_mw) == 342.7
    assert prop.technology == "solar"
    assert prop.identifiers.get("eia_generator_id") == "IPD2S"
    assert "eia_generators" not in prop.identifiers
    assert prop.field_provenance["capacity_mw"]["source_id"] == INVENTORY


def test_a_reload_of_one_member_does_not_write_its_row_over_the_merged_record(db: Session) -> None:
    prop, _first, _second = _darden(db)
    queue, inventory = _sources(db)

    # The EIA register reloads with a revised generator: the record keeps the request's MW and
    # status (before the fix, the last-loaded source's row was written over the record).
    _load(db, inventory, [_generator("IPD2S", "solar", 350.0), _generator("IPD2B", "storage", 303.7)])
    db.expire_all()
    prop = _record(db, INVENTORY, "69662-IPD2S")
    assert _mw(prop.capacity_mw) == 1150.0
    assert prop.lifecycle_state == "contracted"
    assert prop.status_raw == "ACTIVE"

    # The request itself is revised: the record follows it.
    _load(db, queue, [_request(mw=1200.0)])
    db.expire_all()
    prop = _record(db, INVENTORY, "69662-IPD2S")
    assert _mw(prop.capacity_mw) == 1200.0


def test_recomputing_fields_writes_no_change_event(db: Session) -> None:
    """A recomputed canonical value is a restatement (docs/22 §8.1, §8.2): news about a member is
    its own source's diff event, so a merge and a member reload add no `capacity_changed`."""
    _darden(db)
    queue, _inventory = _sources(db)
    before = db.scalar(select(func.count()).select_from(Event).where(Event.event_type != "merged"))
    _load(db, queue, [_request()])  # an unchanged reload
    after = db.scalar(select(func.count()).select_from(Event).where(Event.event_type != "merged"))
    assert after == before


def test_an_admin_override_wins_over_survivorship_and_unmerge_gives_it_back(db: Session) -> None:
    queue, inventory = _sources(db)
    _load(db, queue, [_request()])
    _load(db, inventory, [_generator("IPD2S", "solar", 342.7)])
    survivor = _record(db, INVENTORY, "69662-IPD2S")
    request = _record(db, QUEUE, "1949")
    # A human decision on the absorbed record (W3: carried onto the survivor by the merge).
    request.capacity_mw = 1000.0
    request.overrides = {"capacity_mw": {"value": 1000.0, "event_id": "evt_test", "user_id": "usr_test"}}
    # And one on the survivor itself.
    survivor.name_canonical = "Darden (admin)"
    survivor.overrides = {"name_canonical": {"value": "Darden (admin)", "event_id": "e", "user_id": "u"}}
    db.commit()

    event = merge_proposal(db, canonical=survivor, absorbed=request, score=92.0, rationale="test")
    db.commit()
    db.expire_all()
    prop = _record(db, INVENTORY, "69662-IPD2S")
    assert _mw(prop.capacity_mw) == 1000.0  # the carried override, not the request's 1,150
    assert prop.name_canonical == "Darden (admin)"
    assert prop.lifecycle_state == "contracted"  # an un-pinned field still follows the rule

    _load(db, queue, [_request(mw=1300.0)])
    db.expire_all()
    assert _mw(_record(db, INVENTORY, "69662-IPD2S").capacity_mw) == 1000.0

    unmerge_proposal(db, event.id, reason="test")
    db.commit()
    db.expire_all()
    prop = _record(db, INVENTORY, "69662-IPD2S")
    assert _mw(prop.capacity_mw) == 342.7
    assert prop.name_canonical == "Darden (admin)"
    request = _record(db, QUEUE, "1949")
    assert "capacity_mw" in (request.overrides or {})
    assert _mw(request.capacity_mw) == 1000.0


# ------------------------------------------------------------------------------- the served view
def test_a_hidden_supplier_falls_back_to_the_rule_over_readable_members(db: Session) -> None:
    _darden(db)
    queue = db.get(Source, QUEUE)
    assert queue is not None
    queue.publish_state = "ingest_only"  # the request's register is unpublished
    db.commit()
    db.expire_all()

    view = gated_proposal(_record(db, INVENTORY, "69662-IPD2S"), "public")
    # Not the stored 1,150 MW (a hidden source's value) and not one generator: the same rule over
    # the members the public may read, i.e. the plant inventory summed.
    assert _mw(view.capacity_mw) == 646.4
    assert view.lifecycle_state == "permitted"
    assert view.status_raw == EIA_STATUS
    assert view.proposed_online_date == dt.date(2028, 3, 1)
    assert "queue_ids" not in (view.identifiers or {})


def test_a_whole_set_surface_regates_a_row_whose_secondary_supplier_is_hidden(db: Session) -> None:
    """`hidden_provenance_clause` picks the rows a map or a grid-point total re-reads through the
    served view; a summed value names every supplier under `source_ids`."""
    prop, _first, _second = _darden(db)
    prop.field_provenance = {
        **prop.field_provenance,
        "capacity_mw": {
            "source_id": "us.test.first",
            "licence_id": "x",
            "retrieved_at": "2026-10-07",
            "source_ids": ["us.test.first", "us.test.second"],
        },
    }
    db.commit()
    hit = db.scalar(
        select(func.count())
        .select_from(Proposal)
        .where(hidden_provenance_clause(Proposal, ("capacity_mw",), {"us.test.second"}))
    )
    miss = db.scalar(
        select(func.count())
        .select_from(Proposal)
        .where(hidden_provenance_clause(Proposal, ("capacity_mw",), {"us.test.sec"}))
    )
    assert hit == 1
    assert miss == 0


# ----------------------------------------------------------------------------- the API and page
@pytest.fixture()
def web_client(db_sessionmaker: sessionmaker[Session]) -> Iterator[TestClient]:
    from web.api_client import ApiClient
    from web.app import app as web_app

    def _override_get_db() -> Iterator[Session]:
        session = db_sessionmaker()
        try:
            yield session
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = _override_get_db
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def test_the_detail_route_names_each_fields_supplier_and_lists_the_members(
    db: Session, client: TestClient
) -> None:
    prop, _first, _second = _darden(db)
    data = client.get(f"/v1/proposals/{prop.public_id}").json()["data"]
    assert data["capacity_mw"] == 1150.0
    assert data["field_sources"]["lifecycle_state"]["source_ids"] == [QUEUE]
    assert data["field_sources"]["name_canonical"]["source_ids"] == [INVENTORY]
    assert data["field_sources"]["capacity_mw"]["rule"] == "interconnection_request"
    members = {(m["source_id"], m["capacity_mw"]) for m in data["members"]}
    assert members == {(QUEUE, 1150.0), (INVENTORY, 342.7), (INVENTORY, 303.7)}
    assert [m["role"] for m in data["members"]] == ["request", "inventory", "inventory"]
    assert len(data["merge_history"]) == 2


def test_the_page_credits_the_status_to_the_register_that_reported_it(
    db: Session, web_client: TestClient
) -> None:
    prop, _first, _second = _darden(db)
    # Make the inventory the status supplier and the request the first provenance row: the page
    # used to credit `provenance[0]` (audit F2: CAISO named for EIA's "(T) Regulatory approvals").
    request_link = db.scalar(select(ProposalSource).where(ProposalSource.source_id == QUEUE))
    assert request_link is not None
    request_link.normalised = {**request_link.normalised, "lifecycle_state": "filed", "status_raw": "Filed"}
    db.commit()
    _queue, inventory = _sources(db)
    _load(db, inventory, [_generator("IPD2S", "solar", 342.7), _generator("IPD2B", "storage", 303.7)])
    db.expire_all()
    prop = _record(db, INVENTORY, "69662-IPD2S")
    assert prop.status_raw == EIA_STATUS

    html = web_client.get(f"/proposals/{prop.slug}").text
    assert f"in Test EIA-860M (source status: &ldquo;{EIA_STATUS}&rdquo;)" in html
    assert "in Test CAISO Queue (source status" not in html
    # D-5: what the record combines, each member with its own figures.
    assert "What this record combines" in html
    assert "342.7" in html and "303.7" in html and "1150.0" in html
    assert "1949" in html
