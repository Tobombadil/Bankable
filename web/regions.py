"""Regional quick-view table for the map page (docs/00-PLAN.md owner decisions 2026-09-14/15:
Protomaps basemap; "one-click regional views" taken from Open Grid Works / Open Infrastructure
Map). Geography here is static; which of these five actually gets a button on a given deploy is
computed at request time in `web/app.py::home_map` from which jurisdictions have at least one
published, non-gated source (`regions_with_data`) -- an unpublished/never-ingested region never
shows a jump button pointing at an empty map.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Region:
    code: str
    label: str
    # (west, south, east, north) -- same axis order as the `bbox` query parameter used
    # throughout (`docs/23` §7, `web/viewmodels.WORLD_BBOX`).
    bbox: tuple[float, float, float, float]


#: Order is display order (left to right in the button group).
REGIONS: tuple[Region, ...] = (
    Region("us", "United States", (-125.0, 24.0, -66.0, 50.0)),
    Region("gb", "United Kingdom", (-8.65, 49.82, 1.76, 60.85)),
    Region("eu", "Europe", (-25.0, 34.0, 45.0, 71.0)),
    Region("ca", "Canada", (-141.0, 41.7, -52.6, 83.1)),
    Region("au", "Australia", (112.75, -43.7, 153.7, -9.0)),
)


def jurisdiction_matches_region(jurisdiction: str | None, region_code: str) -> bool:
    """A source's free-text `jurisdiction` (`data/sources.yaml`, `docs/21` §4.1) counts toward a
    region if it starts with that region's code, case-insensitively -- e.g. `US-TX`, `US+CA`,
    `US (Duke, Southern, ...)` all count for `us`; `GB (England/Wales)` counts for `gb`; `CA-ON`
    counts for `ca`. The field is free text, not an enum (`data/sources.yaml` has roughly ten
    distinct spellings across these five regions), so a loose prefix match is the honest match
    this data supports -- an exact-set match would silently drop rows like `GB (England/Wales)`.
    """
    if not jurisdiction:
        return False
    return jurisdiction.strip().upper().startswith(region_code.upper())


def regions_with_data(sources: list[dict[str, Any]]) -> list[Region]:
    """A region appears iff at least one non-gated, published source (`publish_state = public`)
    names a jurisdiction under it. Callers may pre-filter `sources` to `publish_state=public`
    (the `/v1/sources` query does) -- the `publish_state` check here is defence in depth, not the
    only gate, so this function is correct even given an unfiltered source list."""
    active_codes: set[str] = set()
    for source in sources:
        if source.get("publish_state") not in (None, "public"):
            continue
        jurisdiction = source.get("jurisdiction")
        for region in REGIONS:
            if jurisdiction_matches_region(jurisdiction, region.code):
                active_codes.add(region.code)
    return [r for r in REGIONS if r.code in active_codes]
