"""The multi-plant and completed-against-planned cluster checks (docs/22 §22.13; resolution audit
2026-10-07 RES-3 and RES-4)."""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

from services.resolve.merge import (
    ClusterMember,
    completed_planned_conflict,
    gate_cluster,
    multi_plant_conflict,
)

T0 = dt.datetime(2026, 9, 13, tzinfo=dt.UTC)


def _m(
    source: str, name: str, mw: float, tech: str, *, plant: str | None = None, **kw: object
) -> ClusterMember:
    return ClusterMember(
        proposal_id=_uuid.uuid4(),
        source_id=source,
        queue_id=None if plant else name,
        has_eia_id=plant is not None,
        retrieved_at=T0,
        capacity_mw=mw,
        technology=tech,
        eia_plant_id=plant,
        name=name,
        **kw,  # type: ignore[arg-type]
    )


ERCOT, CAISO, EIA = "us.iso.ercot.gen_queue", "us.iso.caiso.gen_queue", "us.eia.860m"


def test_two_same_technology_plants_each_with_their_own_request_go_to_review() -> None:
    barton_bee = [
        _m(ERCOT, "Barton Branch IA", 203.62, "storage"),
        _m(ERCOT, "Bee Branch IA", 203.75, "storage"),
        _m(EIA, "Barton Branch IA", 200.0, "storage", plant="69392"),
        _m(EIA, "Bee Branch IA", 200.0, "storage", plant="70400"),
    ]
    conflict, detail = multi_plant_conflict(barton_bee)
    assert conflict and "69392" in (detail or "") and "70400" in (detail or "")
    allowed, reason = gate_cluster(barton_bee, 77.9)
    assert not allowed and reason.startswith("multi-plant check")


def test_one_request_over_several_plants_and_a_hybrid_split_across_plants_still_merge() -> None:
    darden = [
        _m(CAISO, "DARDEN", 1150.0, "solar_storage"),
        _m(EIA, "Darden I Solar", 342.7, "solar", plant="69661"),
        _m(EIA, "Darden II Solar", 342.7, "solar", plant="69662"),
    ]
    assert multi_plant_conflict(darden) == (False, None)
    hybrid = [
        _m(ERCOT, "Hermes Solar", 200.0, "solar"),
        _m(ERCOT, "Hermes Storage", 100.0, "storage"),
        _m(EIA, "Hermes Solar", 200.0, "solar", plant="1"),
        _m(EIA, "Hermes Storage", 100.0, "storage", plant="2"),
    ]
    assert multi_plant_conflict(hybrid) == (False, None)


def test_a_completed_request_against_a_planned_unit_goes_to_review_unless_the_dates_agree() -> None:
    rosamond = [
        _m(CAISO, "ROSAMOND SOUTH EAST", 101.7, "solar_storage", lifecycle_state="built", proposed_cod=None),
        _m(
            EIA,
            "Rosamond South II",
            70.0,
            "solar",
            plant="69648",
            lifecycle_state="under_construction",
            proposed_cod="2026-11-01",
        ),
    ]
    conflict, detail = completed_planned_conflict(rosamond)
    assert conflict and "69648" in (detail or "")
    assert not gate_cluster(rosamond, 92.0)[0]
    # The same pair with dates eight months apart is one project finishing its paperwork.
    close = [
        _m(CAISO, "X SOLAR", 100.0, "solar", lifecycle_state="built", proposed_cod="2026-03-01"),
        _m(
            EIA,
            "X Solar",
            100.0,
            "solar",
            plant="1",
            lifecycle_state="under_construction",
            proposed_cod="2026-11-01",
        ),
    ]
    assert completed_planned_conflict(close) == (False, None)
    # ERCOT's built is its synchronisation milestone, which precedes EIA's commercial operation.
    ercot = [
        _m(ERCOT, "Y Solar", 100.0, "solar", lifecycle_state="built", status_rule="ercot.synchronized"),
        _m(
            EIA,
            "Y Solar",
            100.0,
            "solar",
            plant="2",
            lifecycle_state="under_construction",
            proposed_cod="2027-11-01",
        ),
    ]
    assert completed_planned_conflict(ercot) == (False, None)
