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


def test_a_withdrawn_queue_position_wins_over_the_plant_inventory() -> None:
    """docs/22 §23.1 (2026-10-07, lane L10): High Bridge Wind, NYISO 0706 withdrawn 2026-05-31 and
    0784 withdrawn, served "announced" from EIA-860M (P). A withdrawn request decides the record
    unless a live request carries the project on; the inventory does not."""
    nyiso = "us.iso.nyiso.gen_queue"
    picks = survive(
        [
            _m(
                nyiso,
                "0706",
                "generation_queue",
                capacity_mw=100.8,
                technology="wind",
                lifecycle_state="withdrawn",
                status_raw="Withdrawn",
                queue_date="2018-04-25",
                withdrawn_date="2026-05-31",
            ),
            _m(
                nyiso,
                "0784",
                "generation_queue",
                capacity_mw=5.0,
                technology="storage",
                lifecycle_state="withdrawn",
                status_raw="Withdrawn",
                queue_date="2018-12-11",
                withdrawn_date="2024-06-30",
            ),
            _gen("62894-WT", 103.2, tech="wind", state="announced", status_raw="(P) Planned"),
        ]
    )
    assert picks["lifecycle_state"].value == "withdrawn"
    assert picks["lifecycle_state"].rule == "withdrawn_request_wins"
    assert picks["lifecycle_state"].primary.record_id == "0706"  # the largest deciding request
    assert picks["status_raw"].value == "Withdrawn"
    assert picks["milestones.withdrawn_date"].value == "2026-05-31"
    every = survive([_req("A", 10.0, state="withdrawn"), _req("B", 10.0, state="cancelled")])
    assert every["lifecycle_state"].value in {"withdrawn", "cancelled"}
    assert every["lifecycle_state"].rule == "every_member_terminal"


def test_a_later_live_request_carries_the_project_past_a_withdrawal() -> None:
    """Hoffman Falls Wind 2: 1335 (2022) withdrawn, C24-042 (2024 cluster) active."""
    nyiso = "us.iso.nyiso.gen_queue"
    picks = survive(
        [
            _m(
                nyiso,
                "C24-042",
                "generation_queue",
                capacity_mw=29.8,
                lifecycle_state="studied",
                queue_date="2024-08-01",
            ),
            _m(
                nyiso,
                "1335",
                "generation_queue",
                capacity_mw=102.5,
                lifecycle_state="withdrawn",
                queue_date="2022-02-24",
                withdrawn_date="2024-05-31",
            ),
            _gen("67346-Q1335", 103.5, tech="wind", state="announced"),
        ]
    )
    assert picks["lifecycle_state"].value == "studied"
    assert picks["lifecycle_state"].rule == "most_advanced_of_conflicting"
    assert "milestones.withdrawn_date" not in picks  # a live record shows no member's withdrawal
    assert picks["milestones.queue_date"].value == "2024-08-01"


def test_a_withdrawn_smaller_add_on_does_not_end_the_project_its_main_request_carries() -> None:
    """Excelsior Energy Center: 0721 (2018, 280 MW solar) active, its 20 MW storage add-on 1169
    (2021) withdrawn, EIA-860M under construction. "Later" alone would end it (docs/22 §23.1)."""
    nyiso = "us.iso.nyiso.gen_queue"
    members = [
        _m(
            nyiso,
            "0721",
            "generation_queue",
            capacity_mw=280.0,
            lifecycle_state="studied",
            queue_date="2018-06-07",
        ),
        _m(
            nyiso,
            "1169",
            "generation_queue",
            capacity_mw=20.0,
            lifecycle_state="withdrawn",
            queue_date="2021-05-11",
        ),
        _gen("68507-EEC", 280.0, state="under_construction"),
    ]
    assert survive(members)["lifecycle_state"].value == "under_construction"
    # The same pair with the add-on as large as the request: an earlier live request does not
    # carry a later withdrawal, so the withdrawal decides.
    same_size = [
        members[0],
        _m(
            nyiso,
            "1169",
            "generation_queue",
            capacity_mw=280.0,
            lifecycle_state="withdrawn",
            queue_date="2021-05-11",
        ),
        members[2],
    ]
    assert survive(same_size)["lifecycle_state"].value == "withdrawn"


def test_an_operating_plant_outranks_a_stale_queue_status_and_gives_the_actual_cod() -> None:
    """MONTEZUMA II: CAISO contracted with a 2012 COD; the EIA plant Montezuma Wind II operating
    since 2012 (the close-out link, `services/resolve/closeout.py`)."""
    plant = _m(
        "us.eia.860m",
        "plant:57701",
        "registry",
        identifiers={"eia_plant_id": "57701"},
        capacity_mw=78.2,
        technology="wind",
        lifecycle_state="built",
        status_raw="Operating",
        operating_plant=True,
        actual_cod="2012",
        name_canonical="Montezuma Wind II",
    )
    request = _req(
        "1037",
        78.0,
        state="contracted",
        technology="wind_storage",
        proposed_online_date="2012-01-29",
        queue_date="2007-01-31",
        name_canonical="MONTEZUMA II",
    )
    picks = survive([request, plant])
    assert picks["lifecycle_state"].value == "built"
    assert picks["lifecycle_state"].rule == "operating_plant_outranks_queue"
    assert picks["milestones.actual_cod"].value == "2012"
    assert picks["milestones.actual_cod"].source_ids == ["us.eia.860m"]
    assert picks["capacity_mw"].value == 78.0  # the request is still the grid-connection figure
    assert picks["identifiers"].value["eia_plant_id"] == "57701"
    # A later withdrawal of the request is newer evidence than the match and decides.
    withdrawn = _req("1037", 78.0, state="withdrawn", technology="wind_storage", queue_date="2007-01-31")
    assert survive([withdrawn, plant])["lifecycle_state"].value == "withdrawn"


def test_a_planned_record_shows_no_actual_cod_and_a_built_one_shows_its_own() -> None:
    built = _req("188", 198.0, state="built", actual_cod="2015-06-25", queue_date="2006-01-01")
    planned = _gen("1-G", 200.0, state="under_construction")
    picks = survive([built, planned])
    assert picks["milestones.actual_cod"].value == "2015-06-25"
    assert picks["milestones.queue_date"].value == "2006-01-01"
    not_built = survive([_req("188", 198.0, state="studied", actual_cod="2015-06-25"), planned])
    assert "milestones.actual_cod" not in not_built


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
