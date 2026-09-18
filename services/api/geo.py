"""GeoJSON map payloads (docs/04-standards.md §2.2, D-10; api/openapi.yaml `GeoResponse`).

Clustering here is a pure-Python lon/lat grid keyed by zoom (grid cell size halves per zoom level
off an empirically-tuned base, `_grid_cell` below) — enough to prove the contract (cluster vs
individual-feature shape, `lifecycle_state_counts`, `technology_counts`, the restricted-precision
rule) at this sprint's data volumes, real ones included (tuned and measured against the full
`data/normalized/*` load — services/README.md "Sprint 2 fixes"). A production implementation would
use PostGIS `ST_ClusterKMeans`/`ST_SnapToGrid` server-side; that is a follow-up once the map page
is being built against real traffic at a scale this grid stops serving well, not before.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from services.db.models import Location, Proposal

SPLIT_THRESHOLD = 500  # docs/04 D-10: clusters split into markers at <=500 visible records

#: ADR 0008 (2026-09-18): the three region-grade precisions, alongside `exact`; `unknown` is the
#: `none` grade and is never drawn here (`meta.unplaced_count` only, computed by the caller).
REGION_PRECISIONS = ("county_centroid", "state_centroid", "country_centroid")
#: `location.precision` -> the level `GET /v1/geo/regions` serves that region's polygon under.
_REGION_LEVEL_BY_PRECISION = {
    "county_centroid": "county",
    "state_centroid": "state",
    "country_centroid": "country",
}


def placement_grade(precision: str) -> str:
    """Derived placement grade (ADR 0008, docs/21 §3.7), never stored: `exact` -> `exact`; the
    three `*_centroid` precisions -> `region`; `unknown` -> `none`."""
    if precision == "exact":
        return "exact"
    if precision in REGION_PRECISIONS:
        return "region"
    return "none"


def _region_id_for(loc: Location) -> str | None:
    if loc.precision == "county_centroid":
        return loc.county_fips
    if loc.precision == "state_centroid":
        return loc.state_code
    if loc.precision == "country_centroid":
        return loc.country
    return None


#: Grid-cell size in degrees at `zoom = 1`; halves each zoom level thereafter (`_grid_cell`).
#: Chosen empirically against the full proposal set placed by `services/ingest/geocode.py`
#: (~8,150 continental-US points): 36° at zoom 1 yields on the order of 20-60 grid cells with any
#: members over the continental US at zoom 3-4 (docs/04 D-10's "a cluster that hides state and
#: technology hides the product" — the prior fixed `360 / 2**zoom` formula produced only 2-4 at
#: that range, `web/README.md`'s reported "three clusters" defect). Never below `MIN_CELL_DEG`
#: (the old formula's zoom-12 floor), so high zoom still converges to near-individual markers
#: rather than an ever-shrinking cell that never stops splitting.
BASE_CELL_DEG = 36.0
MIN_CELL_DEG = 360.0 / (2**12)


def _grid_cell(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    effective_zoom = max(1, min(zoom, 20))
    cell_deg = max(BASE_CELL_DEG / (2 ** (effective_zoom - 1)), MIN_CELL_DEG)
    return (math.floor(lon / cell_deg), math.floor(lat / cell_deg))


def _precision_reason(loc: Location) -> str | None:
    if loc.precision == "county_centroid" and not loc.licence.allows_raw_publication:
        return "licence"
    return None


def _in_bbox(lon: float, lat: float, bbox: tuple[float, float, float, float]) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def build_geo_feature_collection(
    plottable_proposals: list[Proposal],
    *,
    bbox: tuple[float, float, float, float],
    zoom: int,
    records_total: int,
    lifecycle_state_counts: dict[str, int],
    technology_counts: dict[str, int],
) -> dict[str, Any]:
    """`plottable_proposals` must already be tier/licence filtered by the caller
    (services/api/visibility.py) *and* pre-restricted to records with a placeable point
    (`p.location is not None and p.location.geom is not None`) — the caller computes that
    restriction in SQL (`services/api/app.py::_proposal_geo_query`) rather than this function
    filtering it out of a wider, more expensive-to-load set; over the real ~10,400-row proposal
    set, loading every column of every row (including the ~2,200 that can never be plotted) was
    most of this endpoint's latency (services/README.md "Sprint 2 fixes").

    `bbox` scopes which of those become map *features* (points/clusters) — a pan or zoom now
    actually changes what's returned (`web/README.md` "Missing from the API" item 4: previously
    accepted and echoed but never applied, so every request re-clustered the whole dataset
    regardless of viewport). `records_total`, `lifecycle_state_counts` and `technology_counts` are
    the caller's own aggregate over the *full* filter match (not just the plottable/in-view
    subset) — a record with no geometry is not "in" any bbox, and D-8 requires it stays counted
    rather than silently dropping out the moment a caller narrows the map; `web/app.py`'s sitewide
    active/withdrawn notice deliberately calls this with a whole-world bbox, which already
    includes every placed record, so that caller's numbers are unaffected by this scoping.

    Returns the feature collection dict (`meta.unplaced_count` is the caller's concern, computed
    alongside `records_total` from the same aggregate query — not derived here).

    ADR 0008 (2026-09-18): `plottable_proposals` may now carry region-grade rows too (`precision`
    `county_centroid`/`state_centroid`/`country_centroid`, not just `exact`) — the caller's SQL
    query already restricts to placeable rows regardless of grade (`Location.geom.is_not(None)`
    covers all four). This function splits them by `placement_grade`: `exact` rows keep the
    point/cluster behaviour above unchanged; region-grade rows are grouped by `region_id`
    (`_region_id_for`) into one `region` feature per group, geometry = the group's representative
    point (identical for every member — the vendored gazetteer centroid — so the first member's
    point is used rather than an average). Both kinds of feature can appear in the same response
    (a mixed-precision viewport is normal); `SPLIT_THRESHOLD` clustering applies only to the
    `exact` grade, per docs/23 §3.1's "keep proposal and cluster features for exact precision only".
    """
    exact: list[tuple[Proposal, Location, tuple[float, float]]] = []
    region: list[tuple[Proposal, Location, tuple[float, float]]] = []
    for p in plottable_proposals:
        if p.location is None or p.location.geom is None:
            continue
        member = (p, p.location, p.location.geom)
        grade = placement_grade(p.location.precision)
        if grade == "exact":
            exact.append(member)
        elif grade == "region":
            region.append(member)
        # grade == "none" never reaches here (no geom), defensive only.

    in_view = [member for member in exact if _in_bbox(member[2][0], member[2][1], bbox)]

    features: list[dict[str, Any]] = []
    if len(in_view) <= SPLIT_THRESHOLD:
        for p, loc, (lon, lat) in in_view:
            features.append(_record_feature(p, loc, lon, lat))
    else:
        groups: dict[tuple[int, int], list[tuple[Proposal, Location, tuple[float, float]]]] = defaultdict(
            list
        )
        for member in in_view:
            _, _, (lon, lat) = member
            groups[_grid_cell(lon, lat, zoom)].append(member)
        for members in groups.values():
            features.append(_cluster_feature(members))

    region_in_view = [m for m in region if _in_bbox(m[2][0], m[2][1], bbox)]
    region_groups: dict[tuple[str, str], list[tuple[Proposal, Location, tuple[float, float]]]] = defaultdict(
        list
    )
    for member in region_in_view:
        _, loc, _ = member
        region_id = _region_id_for(loc)
        if region_id is None:  # pragma: no cover - defensive; the loader always sets this field
            continue
        region_groups[(loc.precision, region_id)].append(member)
    for (precision, region_id), members in region_groups.items():
        features.append(_region_feature(precision, region_id, members))

    return {
        "type": "FeatureCollection",
        "bbox": list(bbox),
        "features": features,
        "totals": {
            "records": records_total,
            "clustered": len(in_view) > SPLIT_THRESHOLD,
            "lifecycle_state_counts": lifecycle_state_counts,
            "technology_counts": technology_counts,
        },
    }


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


def _region_feature(
    precision: str, region_id: str, members: list[tuple[Proposal, Location, tuple[float, float]]]
) -> dict[str, Any]:
    """One `feature_kind: region` feature per `(precision, region_id)` group (ADR 0008, docs/23
    §3.1): geometry is the group's representative point (every member shares the same vendored
    centroid for that region, so the first member's point is exact, not an approximation)."""
    lifecycle_counts: dict[str, int] = defaultdict(int)
    technology_counts: dict[str, int] = defaultdict(int)
    capacity_sum = 0.0
    name: str | None = None
    for p, loc, _ in members:
        lifecycle_counts[p.lifecycle_state] += 1
        if p.technology:
            technology_counts[p.technology] += 1
        if p.capacity_mw:
            capacity_sum += float(p.capacity_mw)
        if name is None:
            if precision == "county_centroid":
                name = loc.county_name
            elif precision == "state_centroid":
                name = loc.state_code
            elif precision == "country_centroid":
                name = loc.country

    _, _, (lon, lat) = members[0]
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "feature_kind": "region",
            "region_level": _REGION_LEVEL_BY_PRECISION[precision],
            "region_id": region_id,
            "name": name,
            "count": len(members),
            "lifecycle_state_counts": dict(lifecycle_counts),
            "technology_counts": dict(technology_counts),
            "capacity_mw_sum": capacity_sum,
        },
    }
