"""GeoJSON payload for the built-infrastructure context layer (docs/00-PLAN.md decision
2026-09-14 "Built-infrastructure context layer"; docs/21 §3.20).

`BuiltPlant` rows are context drawn *beneath* the proposals map, not proposals: no lifecycle
state, no matches, no restricted-precision rule (every source in scope this sprint — EIA-860M —
is public domain, `docs/21` §3.20). Clustering reuses `services/api/geo.py`'s pure-Python lon/lat
grid (`_grid_cell`, `SPLIT_THRESHOLD`, `_in_bbox`) rather than a second implementation of the same
tuned formula — the grid does not know or care what kind of point it is bucketing.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from services.api.common import iso
from services.api.geo import SPLIT_THRESHOLD, _grid_cell, _in_bbox
from services.db.models import BuiltPlant

__all__ = ["build_plant_feature_collection"]


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


def _plant_cluster_feature(members: list[tuple[BuiltPlant, tuple[float, float]]]) -> dict[str, Any]:
    lons: list[float] = []
    lats: list[float] = []
    technology_counts: dict[str, int] = defaultdict(int)
    capacity_sum = 0.0
    for plant, (lon, lat) in members:
        lons.append(lon)
        lats.append(lat)
        if plant.technology:
            technology_counts[plant.technology] += 1
        if plant.capacity_mw:
            capacity_sum += float(plant.capacity_mw)

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
    plants: list[BuiltPlant],
    *,
    bbox: tuple[float, float, float, float],
    zoom: int,
    records_total: int,
    technology_counts: dict[str, int],
) -> dict[str, Any]:
    """`plants` must already be filtered by the caller (`services/api/context_routes.py`) and
    pre-restricted to rows with a placed point (`plant.geom is not None`) — the caller computes
    that restriction in SQL rather than this function filtering a wider set, per the same reasoning
    as `services/api/geo.py::build_geo_feature_collection`.

    `bbox` scopes which of those become map *features*; `records_total` and `technology_counts` are
    the caller's own aggregate over the *full* filter match (not just the in-view subset), computed
    with the same restriction to placed rows (docs/21 §3.20 carries no "unplaced" concept for
    context plants — every row in scope this sprint has a source-supplied coordinate).
    """
    placed: list[tuple[BuiltPlant, tuple[float, float]]] = [(p, p.geom) for p in plants if p.geom is not None]
    in_view = [member for member in placed if _in_bbox(member[1][0], member[1][1], bbox)]

    features: list[dict[str, Any]] = []
    if len(in_view) <= SPLIT_THRESHOLD:
        for plant, (lon, lat) in in_view:
            features.append(_plant_feature(plant, lon, lat))
    else:
        groups: dict[tuple[int, int], list[tuple[BuiltPlant, tuple[float, float]]]] = defaultdict(list)
        for member in in_view:
            _, (lon, lat) = member
            groups[_grid_cell(lon, lat, zoom)].append(member)
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
