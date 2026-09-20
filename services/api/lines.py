"""Line-geometry helpers for the asset line layer (ADR 0008 §2, owner decision 2026-09-19 option (a):
pipelines as a GeoJSON line layer first, simplified by zoom; vector tiles later).

Pure Python on purpose: `shapely` is not in `requirements.txt` (the repo already keeps geometry
dependency-free -- `pyshp` "pure-Python (no geopandas, ADR 0008)"), and everything this layer
needs -- WKT/GeoJSON parsing of a line, Douglas-Peucker simplification, bbox intersection,
point-to-polyline distance and polyline length -- is a few dozen lines each. The SQLite test target
stores `asset.geom_line` as WKT text (`services/db/types.py::GeographyLine`) and Postgres serves
it through `ST_AsGeoJSON`, so `parse_line_parts` accepts both spellings plus the bound-value
forms the ORM hands back (a list of `(lon, lat)` pairs, or a list of such lists).

A "parts" value throughout is `tuple[tuple[tuple[float, float], ...], ...]`: one tuple of
`(lon, lat)` vertices per LineString part, so a LineString is one part and a MultiLineString
is several. GeoJSON is emitted as `LineString` for one part and `MultiLineString` otherwise.
"""

from __future__ import annotations

import json
import math
from itertools import pairwise
from typing import Any

Point = tuple[float, float]
Part = tuple[Point, ...]
Parts = tuple[Part, ...]
Bbox = tuple[float, float, float, float]

EARTH_RADIUS_KM = 6371.0088
KM_PER_MILE = 1.609344

#: Douglas-Peucker tolerance in degrees at `zoom`, chosen so that a removed vertex moves the
#: drawn line by at most about half a CSS pixel on a 256px Web Mercator tile
#: (`360 / (256 * 2**zoom)` degrees of longitude per pixel, times one half). Zoom 4 (national
#: view) -> 0.044 deg (~4.9 km); zoom 8 -> 0.0027 deg (~300 m); at and above
#: `NO_SIMPLIFY_ZOOM` the stored vertices are served as they are.
NO_SIMPLIFY_ZOOM = 14
#: Tolerance used for an asset page's `geometry` (detail responses): the zoom-10 value, ~75 m,
#: which keeps a state-length pipeline at a few KB while staying honest at page-map scale.
DETAIL_ZOOM = 10


def tolerance_for_zoom(zoom: int) -> float:
    if zoom >= NO_SIMPLIFY_ZOOM:
        return 0.0
    effective = max(0, zoom)
    return 0.5 * 360.0 / (256.0 * (2.0**effective))


# ------------------------------------------------------------------------------------------ parse
def _pairs(seq: Any) -> Part:
    out: list[Point] = []
    for pair in seq:
        lon, lat = pair[0], pair[1]
        out.append((float(lon), float(lat)))
    return tuple(out)


def _parse_wkt(text: str) -> Parts:
    body = text.strip()
    upper = body.upper()
    if upper.startswith("SRID="):
        body = body.split(";", 1)[1].strip()
        upper = body.upper()
    if upper.endswith("EMPTY"):
        return ()
    if upper.startswith("MULTILINESTRING"):
        inner = body[body.index("(") + 1 : body.rindex(")")]
        parts: list[Part] = []
        for chunk in inner.split("),"):
            chunk = chunk.strip().lstrip("(").rstrip(")")
            parts.append(_pairs(pair.split() for pair in chunk.split(",") if pair.strip()))
        return tuple(p for p in parts if p)
    if upper.startswith("LINESTRING"):
        inner = body[body.index("(") + 1 : body.rindex(")")]
        return (_pairs(pair.split() for pair in inner.split(",") if pair.strip()),)
    raise ValueError(f"unsupported WKT geometry: {body[:24]!r}")


def parse_line_parts(value: Any) -> Parts | None:
    """Normalise every representation of `asset.geom_line` this codebase can hand over into
    parts: `None` -> `None`; a GeoJSON dict or JSON text (`ST_AsGeoJSON`); WKT text (the SQLite
    column); a list of `(lon, lat)` pairs (`GeographyLine.process_result_value`); a list of such
    lists (a MultiLineString bound value). Empty geometries return `None`, never an empty tuple."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("{"):
            return parse_line_parts(json.loads(stripped))
        parts = _parse_wkt(stripped)
        return parts or None
    if isinstance(value, dict):
        kind = str(value.get("type", "")).lower()
        coords = value.get("coordinates") or []
        if kind == "linestring":
            part = _pairs(coords)
            return (part,) if part else None
        if kind == "multilinestring":
            parts = tuple(_pairs(c) for c in coords)
            parts = tuple(p for p in parts if p)
            return parts or None
        raise ValueError(f"unsupported GeoJSON geometry type: {value.get('type')!r}")
    seq = list(value)
    if not seq:
        return None
    first = seq[0]
    if isinstance(first, (list, tuple)) and first and isinstance(first[0], (list, tuple)):
        parts = tuple(_pairs(p) for p in seq)
        parts = tuple(p for p in parts if p)
        return parts or None
    part = _pairs(seq)
    return (part,) if part else None


def parts_to_geojson(parts: Parts) -> dict[str, Any]:
    if len(parts) == 1:
        return {"type": "LineString", "coordinates": [[lon, lat] for lon, lat in parts[0]]}
    return {"type": "MultiLineString", "coordinates": [[[lon, lat] for lon, lat in p] for p in parts]}


# ------------------------------------------------------------------------------------- simplify
def _perp_distance_sq(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    seg_sq = dx * dx + dy * dy
    if seg_sq == 0.0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / seg_sq
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def simplify_part(part: Part, tolerance: float) -> Part:
    """Douglas-Peucker in degree space (iterative, so a 10,000-vertex line cannot hit the
    recursion limit). Endpoints are always kept; a two-vertex line is returned as is."""
    n = len(part)
    if tolerance <= 0.0 or n <= 2:
        return part
    tol_sq = tolerance * tolerance
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        start, end = stack.pop()
        if end - start < 2:
            continue
        a, b = part[start], part[end]
        best_i = -1
        best_d = tol_sq
        for i in range(start + 1, end):
            d = _perp_distance_sq(part[i], a, b)
            if d > best_d:
                best_d = d
                best_i = i
        if best_i >= 0:
            keep[best_i] = True
            stack.append((start, best_i))
            stack.append((best_i, end))
    return tuple(p for p, k in zip(part, keep, strict=True) if k)


def _part_extent(part: Part) -> float:
    lons = [p[0] for p in part]
    lats = [p[1] for p in part]
    return max(max(lons) - min(lons), max(lats) - min(lats))


def simplify_parts(parts: Parts, tolerance: float) -> Parts:
    """Simplify every part and drop the parts whose whole extent is below `tolerance` -- they
    would draw as a dot smaller than half a pixel. The longest part is always kept so no asset
    disappears from the map.

    **The floor is two vertices per part, and it binds.** Douglas-Peucker keeps both endpoints
    of every part, so a dissolved EIA Atlas pipeline row (`pipeline/context/eia_atlas.py`: one
    row per operator x type) cannot go below `2 * len(parts)` however coarse the zoom. Measured
    2026-09-20 on the 259 real rows (the figures in an earlier version of this docstring, 67,710
    stored and "a few thousand" at zoom 4, were both wrong):

    - stored before ingest-time chaining: 33,184 parts / 194,887 vertices (128 parts per row,
      2,027 on the largest); at zoom 4, 17,613 parts / 36,571 vertices, of which 35,226 (96%)
      were the two-per-part floor.
    - stored since `geo.merge_touching_lines` chains touching parts at ingest (2026-09-20):
      17,997 parts / 179,700 vertices (69 per row); at zoom 4, 10,511 parts / 24,309 vertices,
      floor 21,022 (86%).

    Because the national view is set by part count rather than detail, coarsening the tolerance
    here cannot help; the fix is merging parts at ingest, not simplifying harder at request
    time, and this function's behaviour is deliberately unchanged."""
    if tolerance <= 0.0:
        return parts
    if len(parts) == 1:
        return (simplify_part(parts[0], tolerance),)
    longest = max(range(len(parts)), key=lambda i: _part_extent(parts[i]))
    kept = tuple(
        simplify_part(p, tolerance)
        for i, p in enumerate(parts)
        if i == longest or _part_extent(p) >= tolerance
    )
    return kept


#: Coordinates are served at a precision matched to the simplification tolerance: one decimal
#: finer than the tolerance's order of magnitude, never fewer than 3 (~110 m) and never more than
#: `FULL_DECIMALS` (the 6 decimals, ~11 cm, the loaders store). At zoom 4 (tolerance 0.044 deg)
#: that is 3 decimals; at zoom 8 (0.0027 deg) 4. Measured on the 3,000-line synthetic fixture the
#: rounding alone takes ~40% off the geometry bytes of a national payload.
FULL_DECIMALS = 6


def decimals_for_tolerance(tolerance: float) -> int:
    if tolerance <= 0.0:
        return FULL_DECIMALS
    return max(3, min(FULL_DECIMALS, math.ceil(-math.log10(tolerance)) + 1))


def round_parts(parts: Parts, decimals: int) -> Parts:
    if decimals >= FULL_DECIMALS:
        return parts
    return tuple(tuple((round(lon, decimals), round(lat, decimals)) for lon, lat in part) for part in parts)


# ----------------------------------------------------------------------------------- bbox tests
def parts_bbox(parts: Parts) -> Bbox:
    lons = [lon for part in parts for lon, _ in part]
    lats = [lat for part in parts for _, lat in part]
    return (min(lons), min(lats), max(lons), max(lats))


def bbox_overlaps(a: Bbox, b: Bbox) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _point_in_bbox(p: Point, bbox: Bbox) -> bool:
    return bbox[0] <= p[0] <= bbox[2] and bbox[1] <= p[1] <= bbox[3]


def _segment_intersects_bbox(a: Point, b: Point, bbox: Bbox) -> bool:
    """Liang-Barsky clip test: does segment a-b have any point inside the axis-aligned box."""
    x0, y0 = a
    x1, y1 = b
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - bbox[0]), (dx, bbox[2] - x0), (-dy, y0 - bbox[1]), (dy, bbox[3] - y0)):
        if p == 0.0:
            if q < 0.0:
                return False
            continue
        r = q / p
        if p < 0.0:
            if r > t1:
                return False
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return False
            if r < t1:
                t1 = r
    return t0 <= t1


def parts_intersect_bbox(parts: Parts, bbox: Bbox) -> bool:
    """True when the line itself (not just its bounding box) touches the viewport: a vertex
    inside it, or a segment crossing it. Callers reject on `bbox_overlaps` first; this is the
    exact test that stops a diagonal line whose envelope covers the viewport from being sent
    when its geometry passes wide of it."""
    for part in parts:
        if any(_point_in_bbox(p, bbox) for p in part):
            return True
        for a, b in pairwise(part):
            if _segment_intersects_bbox(a, b, bbox):
                return True
    return False


# ----------------------------------------------------------------------------------- distances
def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def point_to_parts_km(lon: float, lat: float, parts: Parts) -> float:
    """Great-circle-scale distance from a point to the nearest point on the polyline. A
    single-vertex part is the haversine distance (so a point asset expressed as one vertex gives
    exactly the pre-line-layer result); a segment is measured in a local equirectangular plane
    centred on the query point, which is accurate to well under 1% at the <= 100 km radii the
    nearby-proposals endpoints allow."""
    best = math.inf
    cos_lat = math.cos(math.radians(lat))
    kx = 111.32 * cos_lat  # km per degree of longitude at this latitude
    ky = 110.574  # km per degree of latitude
    for part in parts:
        if len(part) == 1:
            d = haversine_km(lon, lat, part[0][0], part[0][1])
            if d < best:
                best = d
            continue
        for (alon, alat), (blon, blat) in pairwise(part):
            ax, ay = (alon - lon) * kx, (alat - lat) * ky
            bx, by = (blon - lon) * kx, (blat - lat) * ky
            dx, dy = bx - ax, by - ay
            seg_sq = dx * dx + dy * dy
            if seg_sq == 0.0:
                d_sq = ax * ax + ay * ay
            else:
                t = -(ax * dx + ay * dy) / seg_sq
                t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
                cx, cy = ax + t * dx, ay + t * dy
                d_sq = cx * cx + cy * cy
            if d_sq < best * best:
                best = math.sqrt(d_sq)
    return best


def parts_length_km(parts: Parts) -> float:
    total = 0.0
    for part in parts:
        for (alon, alat), (blon, blat) in pairwise(part):
            total += haversine_km(alon, alat, blon, blat)
    return total


def km_to_miles(km: float) -> float:
    return km / KM_PER_MILE
