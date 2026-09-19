"""Dependency-free geometry helpers for the context layers (no shapely/pyproj in this venv).

Everything here works on GeoJSON-style ``[lon, lat]`` coordinate lists in WGS84:

- ``geodesic_length_miles`` — great-circle (haversine) length on the WGS84 mean radius. Not
  Vincenty/Karney: over pipeline-segment distances (metres to hundreds of km) the spherical
  error is below 0.5%, far under the source's own positional accuracy, and the source's
  ``Shape_Leng`` is in degrees and unusable for mileage.
- ``simplify`` — Douglas-Peucker in degrees (planar), kept only to bound parquet size; the
  default in `eia_atlas.py` is 0.0 (off) because the unsimplified layer is already small.
- ``representative_point`` — the vertex nearest the midpoint (by length) of the longest part, so
  the point sits *on* the line, not at a centroid that can fall off it.
- ``wkt_multilinestring`` / ``wkt_point`` — WKT writers with a fixed decimal precision.
- ``StateIndex`` — point-in-polygon against the vendored Census state polygons
  (``data/vendored/regions/us_states.geojson``) with a bounding-box prefilter, for "states crossed"
  and the representative point's state.
"""

from __future__ import annotations

import json
import math
import pathlib
from collections.abc import Iterable, Sequence
from typing import Any

Coord = Sequence[float]
Line = Sequence[Coord]

EARTH_RADIUS_M = 6_371_008.8  # WGS84 mean radius
METRES_PER_MILE = 1_609.344


def haversine_m(a: Coord, b: Coord) -> float:
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def line_length_m(line: Line) -> float:
    return sum(haversine_m(line[i], line[i + 1]) for i in range(len(line) - 1))


def geodesic_length_miles(lines: Iterable[Line]) -> float:
    return sum(line_length_m(ln) for ln in lines) / METRES_PER_MILE


def _perp_distance(p: Coord, a: Coord, b: Coord) -> float:
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]
    px, py = p[0], p[1]
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def simplify(line: Line, tolerance: float) -> list[list[float]]:
    """Douglas-Peucker; ``tolerance`` in degrees. ``0`` returns the line unchanged (copied)."""
    pts = [[float(c[0]), float(c[1])] for c in line]
    if tolerance <= 0 or len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        best_i, best_d = -1, -1.0
        for i in range(lo + 1, hi):
            d = _perp_distance(pts[i], pts[lo], pts[hi])
            if d > best_d:
                best_i, best_d = i, d
        if best_d > tolerance:
            keep[best_i] = True
            stack.append((lo, best_i))
            stack.append((best_i, hi))
    return [p for p, k in zip(pts, keep, strict=True) if k]


def representative_point(lines: Sequence[Line]) -> list[float] | None:
    """The vertex of the longest part nearest that part's half-length. ``None`` for no vertices."""
    best: Line | None = None
    best_len = -1.0
    for ln in lines:
        if len(ln) == 0:
            continue
        length = line_length_m(ln)
        if length > best_len:
            best, best_len = ln, length
    if best is None:
        return None
    if len(best) == 1 or best_len == 0:
        return [float(best[0][0]), float(best[0][1])]
    half = best_len / 2
    acc = 0.0
    for i in range(len(best) - 1):
        seg = haversine_m(best[i], best[i + 1])
        if acc + seg >= half:
            # nearer end of the crossing segment
            chosen = best[i] if (half - acc) < (acc + seg - half) else best[i + 1]
            return [float(chosen[0]), float(chosen[1])]
        acc += seg
    return [float(best[-1][0]), float(best[-1][1])]


def _fmt(v: float, precision: int) -> str:
    s = f"{v:.{precision}f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def wkt_point(coord: Coord, *, precision: int = 6) -> str:
    return f"POINT({_fmt(coord[0], precision)} {_fmt(coord[1], precision)})"


def wkt_multilinestring(lines: Iterable[Line], *, precision: int = 6) -> str:
    parts = []
    for ln in lines:
        if len(ln) < 2:
            continue
        parts.append("(" + ", ".join(f"{_fmt(c[0], precision)} {_fmt(c[1], precision)}" for c in ln) + ")")
    if not parts:
        return "MULTILINESTRING EMPTY"
    return "MULTILINESTRING(" + ", ".join(parts) + ")"


def _point_in_ring(x: float, y: float, ring: Sequence[Coord]) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def _bbox(coords: Iterable[Coord]) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for c in coords:
        xs.append(c[0])
        ys.append(c[1])
    return (min(xs), min(ys), max(xs), max(ys))


class StateIndex:
    """Point -> ``US-XX`` lookup over a GeoJSON of (Multi)Polygons carrying ``region_id``.

    Lookups are cached on a ~0.01 degree (~1 km) grid: the pipeline layer has ~195k vertices
    and many fall within a kilometre of another, so the cache cuts polygon tests by an order of
    magnitude. A cached answer is the answer for the *first* point seen in the cell, which can
    misassign a point within ~1 km of a state border; "states crossed" is a summary attribute,
    not a legal fact, and the loss is recorded in `eia_atlas.py`'s docstring.
    """

    GRID = 100.0  # cells per degree

    def __init__(self, features: Sequence[dict[str, Any]]) -> None:
        self._polys: list[tuple[str, tuple[float, float, float, float], list[list[Sequence[Coord]]]]] = []
        for f in features:
            region_id = str((f.get("properties") or {}).get("region_id") or "")
            geom = f.get("geometry") or {}
            if not region_id or not geom:
                continue
            polys: list[list[Sequence[Coord]]]
            if geom["type"] == "Polygon":
                polys = [geom["coordinates"]]
            elif geom["type"] == "MultiPolygon":
                polys = list(geom["coordinates"])
            else:
                continue
            all_coords = [c for poly in polys for ring in poly for c in ring]
            self._polys.append((region_id, _bbox(all_coords), polys))
        self._cache: dict[tuple[int, int], str | None] = {}

    @classmethod
    def from_file(cls, path: pathlib.Path) -> StateIndex:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data.get("features", []))

    def lookup(self, lon: float, lat: float) -> str | None:
        key = (math.floor(lon * self.GRID), math.floor(lat * self.GRID))
        if key in self._cache:
            return self._cache[key]
        found: str | None = None
        for region_id, (minx, miny, maxx, maxy), polys in self._polys:
            if not (minx <= lon <= maxx and miny <= lat <= maxy):
                continue
            for poly in polys:
                if not poly:
                    continue
                if _point_in_ring(lon, lat, poly[0]) and not any(
                    _point_in_ring(lon, lat, hole) for hole in poly[1:]
                ):
                    found = region_id
                    break
            if found:
                break
        self._cache[key] = found
        return found

    def states_for(self, lines: Iterable[Line]) -> list[str]:
        seen: set[str] = set()
        for ln in lines:
            for c in ln:
                s = self.lookup(float(c[0]), float(c[1]))
                if s:
                    seen.add(s)
        return sorted(seen)
