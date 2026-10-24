"""Which proposal kinds' `capacity_mw` is summed, and as what (one rule for every surface that adds MW).

A generation, storage, nuclear or other record's `capacity_mw` is generating or storage capacity and
is summed as capacity. A `load` record's (a data centre, a large industrial load) is demand: summed
apart, never added to capacity. A line's is transfer capability, a pipeline's, an LNG terminal's or
a hydrogen plant's is throughput, a CO2 well's is nothing: never summed.

Kept in a module of its own so `services/api/geo.py` can read it without importing
`services/api/org_pipeline.py`, which imports `records.py`, which imports `geo.py`.
`org_pipeline.CAPACITY_NOT_SUMMED_KINDS` re-exports the tuple, and `services/trends/snapshot.py`'s
`DEMAND_KINDS` and `NOT_SUMMED_KINDS` are asserted equal to these (`services/api/test_front.py`).
"""

from __future__ import annotations

#: Kinds whose `capacity_mw` is not generating or storage capacity.
CAPACITY_NOT_SUMMED_KINDS: tuple[str, ...] = ("load", "transmission", "pipeline", "lng", "ccs", "hydrogen")
#: Kinds whose `capacity_mw` is demand, summed apart as `demand_mw`.
DEMAND_KINDS: tuple[str, ...] = ("load",)
#: Kinds whose `capacity_mw` is never summed at all.
NEVER_SUMMED_KINDS: tuple[str, ...] = tuple(k for k in CAPACITY_NOT_SUMMED_KINDS if k not in DEMAND_KINDS)


def summed_as(kind: str | None) -> str | None:
    """`capacity`, `demand` or `None` (never summed) for a record of `kind`. A record with no kind
    is summed as capacity, as every tally here does."""
    if kind in DEMAND_KINDS:
        return "demand"
    if kind in CAPACITY_NOT_SUMMED_KINDS:
        return None
    return "capacity"


__all__ = ["CAPACITY_NOT_SUMMED_KINDS", "DEMAND_KINDS", "NEVER_SUMMED_KINDS", "summed_as"]
