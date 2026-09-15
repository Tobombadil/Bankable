"""GeoJSON payload for the built-infrastructure context layer (docs/00-PLAN.md decision
2026-09-14 "Built-infrastructure context layer"; docs/21 §3.20).

`BuiltPlant` rows are context drawn *beneath* the proposals map, not proposals: no lifecycle
state, no matches, no restricted-precision rule (every source in scope this sprint — EIA-860M —
is public domain, `docs/21` §3.20). Clustering reuses `services/api/geo.py`'s pure-Python lon/lat
grid (`_grid_cell`, `SPLIT_THRESHOLD`, `_in_bbox`) rather than a second implementation of the same
tuned formula — the grid does not know or care what kind of point it is bucketing.

Coordinator follow-up (2026-09-15, "pan/zoom performance"): this module used to take a
`list[BuiltPlant]` — every visible, placed plant, fully ORM-hydrated with its `Source`/`Licence`
joined, on *every* request. Measured against the real 14,659-row load that cost 860-960 ms per
pan/zoom (~60 µs/row of ORM hydration), regardless of how few plants were actually in view or
turned into individual markers. This module now builds the collection from `PlantIndexRow` — a
tiny, cache-friendly per-plant tuple (`id, lon, lat, technology, capacity_mw, country`) that
`services/api/context_routes.py` keeps in a process-local cache and never needs a `BuiltPlant` ORM
object, or a join, to cluster or total. A full `BuiltPlant` (`plant_details`) is fetched by id, and
only for the rows that actually render as individual markers (`<= SPLIT_THRESHOLD` in view) — see
`services/README.md` "Context layer endpoints — pan/zoom performance" for the measured before/after
and the caching design.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from services.api.common import iso
from services.api.geo import SPLIT_THRESHOLD, _grid_cell, _in_bbox
from services.db.models import BuiltPlant

__all__ = ["PlantIndexRow", "build_plant_feature_collection"]


@dataclass(frozen=True, slots=True)
class PlantIndexRow:
    """The lightweight per-plant fields clustering and totals actually need — deliberately not a
    `BuiltPlant` slice: this is the shape `services/api/context_routes.py`'s process-local index
    cache stores (one row per placed plant, table-wide, independent of any request's filters), so
    it must stay small and hashable-cheap rather than mirror the ORM model."""

    id: str
    lon: float
    lat: float
    technology: str | None
    capacity_mw: float | None
    country: str


def _plant_source(plant: BuiltPlant) -> dict[str, Any]:
    return {
        "source_id": plant.source_id,
        "source_name": plant.source.name,
        "source_url": plant.source_url,
        "retrieved_at": iso(plant.retrieved_at),
        "licence_id": plant.licence_id,
        "licence_name": plant.licence.name,
    }


def _plant_feature(plant: BuiltPlant, lon: float, lat: float) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": str(plant.id),
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "feature_kind": "plant",
            "name": plant.name,
            "operator_name": plant.operator_name,
            "technology": plant.technology,
            "technology_raw": plant.technology_raw,
            "technologies": dict(plant.technologies or {}),
            "capacity_mw": float(plant.capacity_mw) if plant.capacity_mw is not None else None,
            "generator_count": plant.generator_count,
            "earliest_operating_year": plant.earliest_operating_year,
            "state_code": plant.state_code,
            "county_name": plant.county_name,
            "source": _plant_source(plant),
        },
    }


def _plant_cluster_feature(members: list[PlantIndexRow]) -> dict[str, Any]:
    lons = [m.lon for m in members]
    lats = [m.lat for m in members]
    technology_counts: dict[str, int] = defaultdict(int)
    capacity_sum = 0.0
    for m in members:
        if m.technology:
            technology_counts[m.technology] += 1
        if m.capacity_mw:
            capacity_sum += m.capacity_mw

    centroid_lon = sum(lons) / len(lons)
    centroid_lat = sum(lats) / len(lats)
    dominant_technology = (
        max(technology_counts, key=lambda k: technology_counts[k]) if technology_counts else None
    )
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [centroid_lon, centroid_lat]},
        "properties": {
            "feature_kind": "plant_cluster",
            "count": len(members),
            "technology_counts": dict(technology_counts),
            "capacity_mw_sum": capacity_sum,
            "dominant_technology": dominant_technology,
            "bbox": [min(lons), min(lats), max(lons), max(lats)],
            "expands_to_zoom": 10,
        },
    }


def build_plant_feature_collection(
    index: list[PlantIndexRow],
    *,
    bbox: tuple[float, float, float, float],
    zoom: int,
    records_total: int,
    technology_counts: dict[str, int],
    plant_details: dict[str, BuiltPlant] | None = None,
) -> dict[str, Any]:
    """`index` is every placed plant matching the request's `technology`/`country` filters,
    already restricted to `geom IS NOT NULL` — as `PlantIndexRow`, not `BuiltPlant` (see the
    module docstring). `bbox` scopes which of those become map *features*; `records_total` and
    `technology_counts` are the caller's own aggregate over the *full* filtered index (not just the
    in-view subset), matching `services/api/geo.py::build_geo_feature_collection`'s convention.

    `plant_details` — full `BuiltPlant` rows keyed by `str(id)` — is required, and must cover
    every id this function's own in-view/bbox computation selects, whenever that in-view count is
    `<= SPLIT_THRESHOLD` (individual markers need the full row; a cluster does not). The caller
    decides whether to fetch it *before* calling, using this same `_in_bbox`/`SPLIT_THRESHOLD`
    logic, so the two decisions can never disagree (`services/api/context_routes.py`). A missing id
    is a caller bug and raises `KeyError` rather than silently dropping a plant.
    """
    in_view = [row for row in index if _in_bbox(row.lon, row.lat, bbox)]

    features: list[dict[str, Any]] = []
    if len(in_view) <= SPLIT_THRESHOLD:
        if plant_details is None:
            raise ValueError(
                "plant_details is required when the in-view plant count is at or below SPLIT_THRESHOLD"
            )
        for row in in_view:
            features.append(_plant_feature(plant_details[row.id], row.lon, row.lat))
    else:
        groups: dict[tuple[int, int], list[PlantIndexRow]] = defaultdict(list)
        for row in in_view:
            groups[_grid_cell(row.lon, row.lat, zoom)].append(row)
        for members in groups.values():
            features.append(_plant_cluster_feature(members))

    return {
        "type": "FeatureCollection",
        "bbox": list(bbox),
        "features": features,
        "totals": {
            "records": records_total,
            "clustered": len(in_view) > SPLIT_THRESHOLD,
            "technology_counts": technology_counts,
        },
    }
