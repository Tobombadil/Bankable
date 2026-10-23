"""Data-quality checks on a price run's frame, before it is merged (docs/04 DA-6, as applied to prices).

Each check returns `{check, level, detail, data}` (the record connectors' `pipeline.connectors.dq.
Check` shape, so the admin view reads one structure). `hold` keeps the run's rows out of the
interval store (the snapshots stay as evidence and the run ends `partial`); `warn` is recorded.

* `row_count` — rows against the previous run of the same mode; a fall to under half holds.
* `duplicate_keys` — one interval twice in one run with different prices; holds above 1%.
* `null_spike` — prices that are not numbers; hold above 5% of the rows.
* `location_vocabulary` — a location the source added (warn) or a served location absent
  (warn; hold when every location is absent from a run that returned rows, including a run whose
  every source row named a location this connector does not serve: a renamed page).
* `no_rows` — requests answered with content that gave no served-location row at all (warn: an
  empty page for a day ERCOT no longer fills is legitimate; a renamed file is not).
* `price_range` — a price beyond ±$20,000/MWh (no market here clears beyond its cap, ERCOT's
  $5,000 plus adders being the highest); warns, never holds: a scarcity event is news.
* `placement_drift` — SPP's map draws a hub more than 1 km from the point recorded for it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd

PRICE_LIMIT = 20_000.0
DRIFT_KM = 1.0


def _check(check: str, level: str, detail: str, **data: Any) -> dict[str, Any]:
    return {"check": check, "level": level, "detail": detail, "data": data}


def row_count(rows: int, previous: int | None) -> dict[str, Any]:
    if previous is None or previous == 0:
        return _check("row_count", "info", f"{rows} rows; no previous run of this mode", rows=rows)
    ratio = rows / previous
    level = "hold" if ratio < 0.5 else "pass"
    return _check(
        "row_count",
        level,
        f"{rows} rows against {previous} in the previous run",
        rows=rows,
        previous=previous,
    )


def duplicate_keys(df: pd.DataFrame) -> dict[str, Any]:
    """`df` carries `record_id`, or the light key columns it is built from (`location`, `market`,
    `interval_start`: one source's `record_id` is exactly those three)."""
    if df.empty:
        return _check("duplicate_keys", "pass", "no rows", conflicting=0)
    keys = ["record_id"] if "record_id" in df.columns else ["location", "market", "interval_start"]
    repeated = df.duplicated(keys, keep=False)
    conflicting = 0
    if repeated.any():  # only the repeated keys are grouped (a backfill has none, and 660,000 rows)
        conflicting = int((df[repeated].groupby(keys, observed=True)["price"].nunique() > 1).sum())
    distinct = len(df) - int(df.duplicated(keys, keep="first").sum()) if repeated.any() else len(df)
    share = conflicting / max(distinct, 1)
    level = "hold" if share > 0.01 else ("warn" if conflicting else "pass")
    return _check(
        "duplicate_keys",
        level,
        f"{conflicting} intervals carry two prices in one run",
        conflicting=conflicting,
    )


def null_spike(rows: int, dropped: int) -> dict[str, Any]:
    total = rows + dropped
    share = dropped / total if total else 0.0
    level = "hold" if share > 0.05 else ("warn" if dropped else "pass")
    return _check("null_spike", level, f"{dropped} of {total} prices were not numbers", dropped=dropped)


def location_vocabulary(
    df: pd.DataFrame, served: Iterable[str], unknown: Iterable[str], markets: Iterable[str]
) -> dict[str, Any]:
    served_set = set(served)
    seen = set(df["location"].unique()) if not df.empty else set()
    absent = sorted(served_set - seen) if not df.empty else []
    added = sorted(set(unknown))
    level = "pass"
    if added or absent:
        level = "warn"
    if not (seen & served_set) and (not df.empty or added):
        level = "hold"
    return _check(
        "location_vocabulary",
        level,
        f"{len(added)} unserved location(s) in the source, {len(absent)} served location(s) absent",
        added=added[:50],
        absent=absent,
        markets=sorted(set(markets)),
    )


def no_rows(rows: int, parsed: int) -> dict[str, Any]:
    level = "warn" if parsed and not rows else "pass"
    return _check(
        "no_rows", level, f"{parsed} answer(s) with content gave {rows} served-location rows", parsed=parsed
    )


def price_range(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return _check("price_range", "pass", "no rows", outside=0)
    outside = df[(df["price"] > PRICE_LIMIT) | (df["price"] < -PRICE_LIMIT)]
    return _check(
        "price_range",
        "warn" if len(outside) else "pass",
        f"{len(outside)} price(s) beyond ±{PRICE_LIMIT:,.0f}/MWh",
        outside=len(outside),
        examples=outside[["location", "market", "price"]].head(5).to_dict("records") if len(outside) else [],
    )


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def placement_drift(
    points: Mapping[str, tuple[float, float]], recorded: Mapping[str, tuple[float, float]]
) -> dict[str, Any]:
    moved = {
        name: round(_km(points[name], recorded[name]), 3)
        for name in points
        if name in recorded and _km(points[name], recorded[name]) > DRIFT_KM
    }
    return placement_drift_result(moved)


def placement_drift_result(moved: Mapping[str, float]) -> dict[str, Any]:
    """The `placement_drift` check for hubs already found moved (name -> km); the runner merges a
    run's pieces through this."""
    moved = dict(moved)
    return _check(
        "placement_drift",
        "warn" if moved else "pass",
        f"{len(moved)} hub(s) drawn more than {DRIFT_KM} km from the recorded point",
        moved_km=moved,
    )


def status(checks: list[dict[str, Any]]) -> str:
    levels = {c["level"] for c in checks}
    if "hold" in levels:
        return "hold"
    return "warn" if "warn" in levels else "pass"
