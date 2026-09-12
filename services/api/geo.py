"""GeoJSON map payloads (docs/04-standards.md §2.2; api/openapi.yaml `GeoResponse`).

Clustering here is a simple lon/lat grid keyed by zoom (grid cell size halves per zoom level) —
enough to prove the contract (cluster vs individual-feature shape, `lifecycle_state_counts`,
`technology_counts`, the restricted-precision rule) at this sprint's data volumes. A production
implementation would use PostGIS `ST_ClusterKMeans`/`ST_SnapToGrid` server-side; that is a
follow-up once the map page (docs/04 D-10) is being built against real traffic, not before.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from services.db.models import Location, Proposal

SPLIT_THRESHOLD = 500  # docs/04 D-10: clusters split into markers at <=500 visible records


def _grid_cell(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    cell_deg = 360.0 / (2 ** max(1, min(zoom, 12)))
    return (math.floor(lon / cell_deg), math.floor(lat / cell_deg))


def _precision_reason(loc: Location) -> str | None:
    if loc.precision == "county_centroid" and not loc.licence.allows_raw_publication:
        return "licence"
    return None


def build_geo_feature_collection(
    proposals: list[Proposal], *, bbox: tuple[float, float, float, float], zoom: int
) -> tuple[dict[str, Any], int]:
    """`proposals` must already be tier/licence filtered by the caller (services/api/visibility.py).

    Returns `(feature_collection, unplaced_count)`.
    """
    with_location: list[tuple[Proposal, Location]] = [
        (p, p.location) for p in proposals if p.location is not None
    ]
    unplaced_count = len(proposals) - len(with_location)

    # county/state-centroid records without a stored point cannot be plotted exactly; treat them
    # as "unplaced" for feature purposes but still counted honestly (docs/04 D-8) rather than
    # silently dropped. A future geocoder (services/ingest/loader.py note) fills `geom` in.
    plottable: list[tuple[Proposal, Location, tuple[float, float]]] = [
        (p, loc, loc.geom) for p, loc in with_location if loc.geom is not None
    ]
    unplaced_count += len(with_location) - len(plottable)

    features: list[dict[str, Any]] = []
    if len(plottable) <= SPLIT_THRESHOLD:
        for p, loc, (lon, lat) in plottable:
            features.append(_record_feature(p, loc, lon, lat))
    else:
        groups: dict[tuple[int, int], list[tuple[Proposal, Location, tuple[float, float]]]] = defaultdict(
            list
        )
        for member in plottable:
            _, _, (lon, lat) = member
            groups[_grid_cell(lon, lat, zoom)].append(member)
        for members in groups.values():
            features.append(_cluster_feature(members))

    lifecycle_counts: dict[str, int] = defaultdict(int)
    technology_counts: dict[str, int] = defaultdict(int)
    for p in proposals:
        lifecycle_counts[p.lifecycle_state] += 1
        if p.technology:
            technology_counts[p.technology] += 1

    fc = {
        "type": "FeatureCollection",
        "bbox": list(bbox),
        "features": features,
        "totals": {
            "records": len(proposals),
            "clustered": len(plottable) > SPLIT_THRESHOLD,
            "lifecycle_state_counts": dict(lifecycle_counts),
            "technology_counts": dict(technology_counts),
        },
    }
    return fc, unplaced_count


def _record_feature(p: Proposal, loc: Location, lon: float, lat: float) -> dict[str, Any]:
    from services.api.common import WEB_HOST
    from services.api.serialize import provenance_row

    reason = _precision_reason(loc)
    return {
        "type": "Feature",
        "id": p.public_id,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "feature_kind": "proposal",
            "public_id": p.public_id,
            "name": p.name_canonical,
            "url": f"{WEB_HOST}/proposals/{p.slug}",
            "kind": p.kind,
            "technology": p.technology,
            "lifecycle_state": p.lifecycle_state,
            "capacity_mw": float(p.capacity_mw) if p.capacity_mw is not None else None,
            "county_name": loc.county_name,
            "state_code": loc.state_code,
            "precision": loc.precision,
            "precision_reason": reason,
            "precision_note": (
                "location shown at county level (source licence)" if reason == "licence" else None
            ),
            "last_changed": p.last_changed.isoformat(),
            "provenance": [provenance_row(s, s.source) for s in p.sources if s.active],
        },
    }


def _cluster_feature(members: list[tuple[Proposal, Location, tuple[float, float]]]) -> dict[str, Any]:
    lons: list[float] = []
    lats: list[float] = []
    lifecycle_counts: dict[str, int] = defaultdict(int)
    technology_counts: dict[str, int] = defaultdict(int)
    capacity_sum = 0.0
    precisions: set[str] = set()
    for p, loc, (lon, lat) in members:
        lons.append(lon)
        lats.append(lat)
        lifecycle_counts[p.lifecycle_state] += 1
        if p.technology:
            technology_counts[p.technology] += 1
        if p.capacity_mw:
            capacity_sum += float(p.capacity_mw)
        precisions.add(loc.precision)

    centroid_lon = sum(lons) / len(lons)
    centroid_lat = sum(lats) / len(lats)
    dominant_lifecycle = max(lifecycle_counts, key=lambda k: lifecycle_counts[k])
    dominant_technology = (
        max(technology_counts, key=lambda k: technology_counts[k]) if technology_counts else None
    )
    precision = precisions.pop() if len(precisions) == 1 else "mixed"

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [centroid_lon, centroid_lat]},
        "properties": {
            "feature_kind": "cluster",
            "count": len(members),
            "lifecycle_state_counts": dict(lifecycle_counts),
            "technology_counts": dict(technology_counts),
            "capacity_mw_sum": capacity_sum,
            "dominant_lifecycle_state": dominant_lifecycle,
            "dominant_technology": dominant_technology,
            "bbox": [min(lons), min(lats), max(lons), max(lats)],
            "precision": precision,
            "expands_to_zoom": 10,
        },
    }
