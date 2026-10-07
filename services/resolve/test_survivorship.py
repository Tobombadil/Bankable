"""`services/resolve/survivorship.py`: each per-field rule on hand-built members (docs/22 §23.1),
plus the store pass's restatement contract (idempotent, no events, overrides pinned). The end-to-end
merge, unmerge, reload, served-view and page cases are in `tests/test_field_survivorship.py`."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SourceEntry
from services.db.models import Event, Proposal, ProposalSource
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.resolve.survivorship import Member, restate_all, survive

T0 = dt.datetime(2026, 9, 13, 20, 25, tzinfo=dt.UTC)


def _m(
    source_id: str,
    record_id: str,
    category: str,
    *,
    days: int = 0,
    gone: bool = False,
    identifiers: dict[str, Any] | None = None,
    **values: Any,
) -> Member:
    return Member(
        source_id=source_id,
        category=category,
        record_id=record_id,
        retrieved_at=T0 + dt.timedelta(days=days),
        licence_id=f"{source_id}#lic",
        values=values,
        identifiers=identifiers or {},
        gone=gone,
    )


def _req(record_id: str, mw: float | None, state: str = "studied", **values: Any) -> Member:
    return _m(
        "us.iso.caiso.gen_queue",
        record_id,
        "generation_queue",
        capacity_mw=mw,
        lifecycle_state=state,
        **values,
    )


def _gen(record_id: str, mw: float, tech: str = "solar", state: str = "permitted", **values: Any) -> Member:
    plant, gen = record_id.split("-")
    return _m(
        "us.eia.860m",
        record_id,
        "registry",
        days=14,
        identifiers={"eia_plant_id": plant, "eia_generator_id": gen},
        capacity_mw=mw,
        technology=tech,
        lifecycle_state=state,
        **values,
    )


# -------------------------------------------------------------------------------------- capacity
def test_capacity_is_the_live_request_not_a_generator_nor_the_nameplate_sum() -> None:
    picks = survive(
        [
            _req("1949", 1150.0, technology="solar_storage"),
            _gen("69662-IPD2S", 342.7),
            _gen("69662-IPD2B", 303.7, tech="storage"),
        ]
    )
    assert picks["capacity_mw"].value == 1150.0
    assert picks["capacity_mw"].rule == "interconnection_request"
    assert picks["capacity_mw"].source_ids == ["us.iso.caiso.gen_queue"]


def test_without_a_live_request_the_plant_inventory_is_summed() -> None:
    picks = survive(
        [
            _req("1949", 1150.0, state="withdrawn"),
            _gen("69662-IPD2S", 342.7),
            _gen("69662-IPD2B", 303.7, tech="storage"),
        ]
    )
    assert round(picks["capacity_mw"].value, 3) == 646.4
    assert picks["capacity_mw"].rule == "plant_inventory_sum"
    # A request that has left its register is not live either.
    gone = _m("us.iso.caiso.gen_queue", "1949", "generation_queue", gone=True, capacity_mw=1150.0)
    assert survive([gone, _gen("69662-IPD2S", 342.7)])["capacity_mw"].value == 342.7


def test_with_only_withdrawn_requests_their_sum_is_kept() -> None:
    picks = survive([_req("A", 100.0, state="withdrawn"), _req("B", 50.0, state="withdrawn")])
    assert picks["capacity_mw"].value == 150.0
    assert picks["capacity_mw"].rule == "interconnection_request_inactive_sum"


def test_one_projects_two_queue_ids_count_once_but_numbered_phases_both_count() -> None:
    nyiso = "us.iso.nyiso.gen_queue"
    twice = [
        _m(
            nyiso,
            "C24-008",
            "generation_queue",
            capacity_mw=50.0,
            technology="storage",
            name_canonical="KCE NY 30",
        ),
        _m(
            nyiso,
            "1448",
            "generation_queue",
            capacity_mw=50.0,
            technology="storage",
            name_canonical="KCE NY 30",
        ),
    ]
    assert survive(twice)["capacity_mw"].value == 50.0
    ercot = "us.iso.ercot.gen_queue"
    phases = [
        _m(
            ercot,
            "20INR0205",
            "generation_queue",
            capacity_mw=254.0,
            technology="solar",
            name_canonical="Roseland Solar",
        ),
        _m(
            ercot,
            "22INR0506",
            "generation_queue",
            capacity_mw=254.0,
            technology="solar",
            name_canonical="Roseland Solar II",
        ),
    ]
    assert survive(phases)["capacity_mw"].value == 508.0
    assert survive(phases)["capacity_mw"].rule == "interconnection_request_sum"


def test_an_inventory_family_no_request_covers_is_added_to_the_request() -> None:
    """ERCOT holds only the storage half of Albatross; EIA lists both halves."""
    storage = _m(
        "us.iso.ercot.gen_queue", "25INR0071", "generation_queue", capacity_mw=50.4, technology="storage"
    )
    picks = survive([storage, _gen("66894-ALBBA", 50.4, tech="storage"), _gen("66894-ALBPV", 101.0)])
    assert round(picks["capacity_mw"].value, 3) == 151.4
    assert picks["capacity_mw"].rule == "interconnection_request_plus_inventory_sum"
    assert picks["technology"].value == "solar_storage"
    # A hybrid request covers both halves, so nothing is added (Darden).
    hybrid = survive([_req("1949", 1150.0, technology="solar_storage"), _gen("69662-IPD2S", 342.7)])
    assert hybrid["capacity_mw"].value == 1150.0


# ------------------------------------------------------------------------------------ technology
def test_technology_comes_from_the_capacity_suppliers_and_a_hybrid_pair_is_named_as_one() -> None:
    picks = survive(
        [
            _req("24INR0337", 200.94, technology="solar", technology_raw="SOL"),
            _req("24INR0338", 201.31, technology="storage", technology_raw="OTH"),
            _gen("65847-ELDPV", 200.0, tech="solar"),
        ]
    )
    assert picks["technology"].value == "solar_storage"
    assert picks["technology"].rule == "hybrid_of_capacity_suppliers"
    assert picks["technology_raw"].value == "OTH + SOL"  # one source's texts, largest member first


# ------------------------------------------------------------------------------------- lifecycle
def test_the_most_advanced_state_wins_with_its_own_status_text_and_date() -> None:
    picks = survive(
        [
            _req("1949", 1150.0, state="contracted", status_raw="ACTIVE", proposed_online_date="2027-07-19"),
            _gen(
                "69662-IPD2S", 342.7, status_raw="(T) Regulatory approvals", proposed_online_date="2028-03-01"
            ),
        ]
    )
    assert picks["lifecycle_state"].value == "contracted"
    assert picks["lifecycle_state"].rule == "most_advanced_of_conflicting"
    assert picks["status_raw"].value == "ACTIVE"
    assert picks["proposed_online_date"].value == "2027-07-19"
    assert picks["proposed_online_date"].rule == "with_lifecycle"


def test_a_withdrawn_member_does_not_end_a_project_another_member_shows_progressing() -> None:
    picks = survive([_req("1949", 1150.0, state="withdrawn"), _gen("69662-IPD2S", 342.7, state="permitted")])
    assert picks["lifecycle_state"].value == "permitted"
    every = survive([_req("A", 10.0, state="withdrawn"), _req("B", 10.0, state="cancelled")])
    assert every["lifecycle_state"].value in {"withdrawn", "cancelled"}
    assert every["lifecycle_state"].rule == "every_member_terminal"


def test_unknown_never_wins_and_a_gone_member_yields_to_a_present_one() -> None:
    assert (
        survive([_req("A", 10.0, state="unknown"), _gen("1-G", 5.0, state="filed")])["lifecycle_state"].value
        == "filed"
    )
    gone = _m(
        "us.iso.caiso.gen_queue",
        "A",
        "generation_queue",
        gone=True,
        capacity_mw=10.0,
        lifecycle_state="built",
    )
    assert survive([gone, _gen("1-G", 5.0, state="filed")])["lifecycle_state"].value == "filed"


def test_the_date_comes_from_the_next_member_when_the_status_winner_states_none() -> None:
    picks = survive(
        [
            _req("1949", 1150.0, state="contracted"),
            _gen("69662-IPD2S", 342.7, proposed_online_date="2028-03-01"),
        ]
    )
    assert picks["proposed_online_date"].value == "2028-03-01"
    assert picks["proposed_online_date"].rule == "next_most_advanced"


# --------------------------------------------------------------------------- name and identifiers
def test_the_plant_inventory_names_the_record_and_every_members_keys_survive() -> None:
    picks = survive(
        [
            _req("1949", 1150.0, name_canonical="DARDEN", identifiers=None),
            _m(
                "us.iso.caiso.gen_queue",
                "1950",
                "generation_queue",
                capacity_mw=10.0,
                identifiers={"queue_ids": [{"iso": "CAISO", "id": "1950"}]},
            ),
            _gen("69661-IPD1S", 342.7, name_canonical="Darden I Solar"),
            _gen("69662-IPD2S", 342.7, name_canonical="Darden II Solar"),
            _gen("69662-IPD2B", 303.7, tech="storage", name_canonical="Darden II Solar"),
        ],
        {"name_canonical": "Darden I Solar"},
    )
    # Plant 69662 holds more MW than 69661, so it names the record whatever is stored.
    assert picks["name_canonical"].value == "Darden II Solar"
    ids = picks["identifiers"].value
    assert ids["eia_plant_id"] == "69662"
    assert ids["queue_ids"] == [{"iso": "CAISO", "id": "1950"}]
    assert [g["generator_id"] for g in ids["eia_generators"]] == ["IPD1S", "IPD2B", "IPD2S"]
    assert "eia_generator_id" not in ids


def test_a_tied_name_keeps_the_stored_one() -> None:
    members = [
        _gen("69661-IPD1S", 342.7, name_canonical="Darden I Solar"),
        _gen("69662-IPD2S", 342.7, name_canonical="Darden II Solar"),
    ]
    assert (
        survive(members, {"name_canonical": "Darden II Solar"})["name_canonical"].value == "Darden II Solar"
    )
    assert survive(members, {"name_canonical": "Darden I Solar"})["name_canonical"].value == "Darden I Solar"


# --------------------------------------------------------------------------------- the store pass
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


def _row(source_id: str, record_id: str, mw: float, state: str) -> dict[str, Any]:
    return {
        "record_id": f"{source_id}:{record_id}",
        "source_id": source_id,
        "source_record_id": record_id,
        "source_url": f"https://example.org/{record_id}",
        "retrieved_at": "2026-09-13T20:25:21Z",
        "kind": "generation",
        "name_canonical": "Two Source Solar",
        "technology": "solar",
        "capacity_mw": mw,
        "iso": "CAISO",
        "state": "CA",
        "lifecycle_state": state,
        "status_raw": state.upper(),
        "raw": "{}",
    }


def test_the_store_pass_restates_without_events_and_is_idempotent(session: Session) -> None:
    """Two sources' links on one record written as the pre-survivorship store had them (the
    record shows the last-loaded generator's figures): the pass corrects the record, writes no
    event, leaves an overridden field alone, and a second pass changes nothing."""
    queue = upsert_licence_and_source(session, _entry("us.test.queue", "generation_queue"), "v")
    inventory = upsert_licence_and_source(session, _entry("us.test.inventory", "registry"), "v")
    load_dataframe(
        session, queue, "proposal", pd.DataFrame([_row("us.test.queue", "Q1", 400.0, "contracted")]), None
    )
    load_dataframe(
        session,
        inventory,
        "proposal",
        pd.DataFrame([_row("us.test.inventory", "1-G1", 150.0, "filed")]),
        None,
    )
    session.commit()
    links = session.scalars(select(ProposalSource)).all()
    keep = session.get(Proposal, next(lk.proposal_id for lk in links if lk.source_id == "us.test.inventory"))
    other = next(lk for lk in links if lk.source_id == "us.test.queue")
    assert keep is not None
    # Move the queue link by hand, the way a merge before survivorship left a record.
    other.proposal_id = keep.id
    keep.overrides = {"name_canonical": {"value": keep.name_canonical, "event_id": "e", "user_id": "u"}}
    session.commit()
    events = session.scalar(select(func.count()).select_from(Event))

    report = restate_all(session)
    assert report.records_changed == 1
    assert {"capacity_mw", "lifecycle_state", "status_raw"} <= set(report.fields_changed)
    assert (
        "name_canonical" not in keep.field_provenance
        or keep.field_provenance["name_canonical"].get("rule") is None
    )
    assert float(keep.capacity_mw or 0) == 400.0
    assert keep.lifecycle_state == "contracted"
    assert keep.field_provenance["capacity_mw"]["source_id"] == "us.test.queue"
    assert session.scalar(select(func.count()).select_from(Event)) == events

    again = restate_all(session)
    assert again.records_changed == 0
    assert again.provenance_restamped == 0
