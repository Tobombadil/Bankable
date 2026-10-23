"""Line geometry for the site finder: geodesic lengths, cutting a line into segments of about half a
mile, a local metric plane for buffers and areas, and point-to-line distances.

Pure numpy except `LocalPlane.to_xy` / `to_deg`, which take and return shapely geometries. Distances
and areas are computed in a plane centred on the piece being measured (an equirectangular projection
at the piece's own latitude), which over the few miles a chunk spans is within 0.1% of the
ellipsoid; lengths are haversine on the WGS84 mean radius.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

#: WGS84 mean radius (IUGG), metres.
EARTH_RADIUS_M = 6_371_008.8
METRES_PER_MILE = 1609.344
#: Metres per degree of latitude on the mean sphere (and of longitude at the equator).
M_PER_DEG = math.pi * EARTH_RADIUS_M / 180.0

FloatArray = np.ndarray[Any, np.dtype[np.float64]]


def haversine_m(lon1: Any, lat1: Any, lon2: Any, lat2: Any) -> Any:
    """Great-circle distance in metres; numpy-broadcasting."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def cumulative_m(coords: FloatArray) -> FloatArray:
    """Distance along `coords` ((n, 2) lon/lat) to each vertex, from 0."""
    if len(coords) < 2:
        return np.zeros(len(coords))
    steps = haversine_m(coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1])
    return np.concatenate([[0.0], np.cumsum(steps)])


def length_mi(coords: FloatArray) -> float:
    return float(cumulative_m(coords)[-1]) / METRES_PER_MILE if len(coords) > 1 else 0.0


@dataclass(frozen=True)
class Segment:
    """One piece of a line: its index along the line, where it starts (miles from the line's first
    vertex), its length and its vertices (the cut points plus every vertex between them)."""

    index: int
    start_mi: float
    length_mi: float
    coords: FloatArray

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (
            float(self.coords[:, 0].min()),
            float(self.coords[:, 1].min()),
            float(self.coords[:, 0].max()),
            float(self.coords[:, 1].max()),
        )

    @property
    def midpoint(self) -> tuple[float, float]:
        """The point halfway along the segment (not the bbox centre: a bent segment's centre can lie
        off it)."""
        point = point_along(self.coords, self.length_mi * METRES_PER_MILE / 2)
        return float(point[0]), float(point[1])


def point_along(coords: FloatArray, distance_m: float) -> FloatArray:
    """The point `distance_m` along `coords`, interpolated linearly within its edge."""
    cum = cumulative_m(coords)
    if distance_m <= 0 or len(coords) == 1:
        return np.array(coords[0], dtype=np.float64)
    if distance_m >= cum[-1]:
        return np.array(coords[-1], dtype=np.float64)
    i = int(np.searchsorted(cum, distance_m, side="right")) - 1
    span = cum[i + 1] - cum[i]
    t = 0.0 if span <= 0 else (distance_m - cum[i]) / span
    return np.asarray(coords[i] + t * (coords[i + 1] - coords[i]))


def split_line(coords: Sequence[Sequence[float]] | FloatArray, target_mi: float = 0.5) -> list[Segment]:
    """Cut a line into `max(1, round(length / target_mi))` pieces of equal length (so each is within
    a third of `target_mi` of it, and a line shorter than the target is one piece). Zero-length
    edges (repeated vertices) are dropped first; a line with fewer than two distinct vertices has no
    segment."""
    xy = np.asarray(coords, dtype=np.float64)
    if xy.ndim != 2 or len(xy) < 2:
        return []
    keep = np.concatenate([[True], np.any(np.diff(xy, axis=0) != 0, axis=1)])
    xy = xy[keep]
    if len(xy) < 2:
        return []
    cum = cumulative_m(xy)
    total = float(cum[-1])
    if total <= 0:
        return []
    n = segment_count(xy, target_mi)
    cuts = np.linspace(0.0, total, n + 1)
    out: list[Segment] = []
    for k in range(n):
        a, b = float(cuts[k]), float(cuts[k + 1])
        inner = xy[(cum > a) & (cum < b)]
        piece = (
            np.vstack([point_along(xy, a), inner, point_along(xy, b)])
            if len(inner)
            else np.vstack([point_along(xy, a), point_along(xy, b)])
        )
        out.append(
            Segment(index=k, start_mi=a / METRES_PER_MILE, length_mi=(b - a) / METRES_PER_MILE, coords=piece)
        )
    return out


def segment_count(coords: Sequence[Sequence[float]] | FloatArray, target_mi: float = 0.5) -> int:
    """How many pieces `split_line` cuts `coords` into: 0 for a line with no length."""
    xy = np.asarray(coords, dtype=np.float64)
    if xy.ndim != 2 or len(xy) < 2:
        return 0
    total = float(cumulative_m(xy)[-1])
    if total <= 0:
        return 0
    return max(1, round(total / (target_mi * METRES_PER_MILE)))


def join_segments(pieces: Sequence[Sequence[Sequence[float]] | FloatArray]) -> FloatArray:
    """One polyline from consecutive segments' vertices (each segment starts where the one before it
    ends, so the shared vertex is kept once): a band's centreline from the vertices its run stored,
    never from a line geometry a later load may have changed."""
    out: list[FloatArray] = []
    last: tuple[float, float] | None = None
    for piece in pieces:
        xy = np.asarray(piece, dtype=np.float64).reshape(-1, 2)
        if not len(xy):
            continue
        # Plain float compares: the map joins tens of thousands of segments a request.
        x0, y0 = float(xy[0, 0]), float(xy[0, 1])
        if last is not None and abs(last[0] - x0) <= 1e-9 and abs(last[1] - y0) <= 1e-9:
            xy = xy[1:]
        if len(xy):
            out.append(xy)
            last = (float(xy[-1, 0]), float(xy[-1, 1]))
    return np.vstack(out) if out else np.zeros((0, 2))


def sub_line(coords: FloatArray, start_mi: float, end_mi: float) -> FloatArray:
    """The part of `coords` between two distances along it (miles): the API finds a band's centre
    point this way."""
    cum = cumulative_m(coords)
    a, b = start_mi * METRES_PER_MILE, end_mi * METRES_PER_MILE
    inner = coords[(cum > a) & (cum < b)]
    parts = [point_along(coords, a)[None, :], inner, point_along(coords, b)[None, :]]
    return np.vstack([p for p in parts if len(p)])


@dataclass(frozen=True)
class LocalPlane:
    """An equirectangular plane in metres centred on (`lon0`, `lat0`)."""

    lon0: float
    lat0: float

    @property
    def kx(self) -> float:
        return M_PER_DEG * math.cos(math.radians(self.lat0))

    def forward(self, lon: Any, lat: Any) -> tuple[Any, Any]:
        return (np.asarray(lon) - self.lon0) * self.kx, (np.asarray(lat) - self.lat0) * M_PER_DEG

    def inverse(self, x: Any, y: Any) -> tuple[Any, Any]:
        return np.asarray(x) / self.kx + self.lon0, np.asarray(y) / M_PER_DEG + self.lat0

    def xy(self, coords: FloatArray) -> FloatArray:
        x, y = self.forward(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    def to_xy(self, geom: Any) -> Any:
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        kx, lon0, lat0 = self.kx, self.lon0, self.lat0
        return shapely.transform(
            geom, lambda c: np.column_stack([(c[:, 0] - lon0) * kx, (c[:, 1] - lat0) * M_PER_DEG])
        )

    def to_deg(self, geom: Any) -> Any:
        import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

        kx, lon0, lat0 = self.kx, self.lon0, self.lat0
        return shapely.transform(
            geom, lambda c: np.column_stack([c[:, 0] / kx + lon0, c[:, 1] / M_PER_DEG + lat0])
        )

    @classmethod
    def around(cls, coords: FloatArray) -> LocalPlane:
        return cls(
            float((coords[:, 0].min() + coords[:, 0].max()) / 2),
            float((coords[:, 1].min() + coords[:, 1].max()) / 2),
        )


def points_to_polyline_m(px: FloatArray, py: FloatArray, line_xy: FloatArray) -> FloatArray:
    """The distance (metres, in the plane) from each point to the nearest point of a polyline."""
    if len(px) == 0:
        return np.zeros(0)
    if len(line_xy) == 1:
        return np.asarray(np.hypot(px - line_xy[0, 0], py - line_xy[0, 1]))
    ax, ay = line_xy[:-1, 0], line_xy[:-1, 1]
    dx, dy = line_xy[1:, 0] - ax, line_xy[1:, 1] - ay
    seg2 = dx * dx + dy * dy
    best = np.full(len(px), np.inf)
    # Points in chunks so a long line and many points never build one huge matrix.
    for start in range(0, len(px), 512):
        qx, qy = px[start : start + 512, None], py[start : start + 512, None]
        with np.errstate(invalid="ignore", divide="ignore"):
            t = np.where(seg2 > 0, ((qx - ax) * dx + (qy - ay) * dy) / seg2, 0.0)
        t = np.clip(t, 0.0, 1.0)
        d = np.hypot(qx - (ax + t * dx), qy - (ay + t * dy))
        best[start : start + 512] = d.min(axis=1)
    return best


class PointIndex:
    """Points bucketed on a grid of `cell_deg` degrees, for "which points lie near this box"."""

    def __init__(self, lons: Sequence[float], lats: Sequence[float], cell_deg: float = 0.25) -> None:
        self.lons = np.asarray(lons, dtype=np.float64)
        self.lats = np.asarray(lats, dtype=np.float64)
        self.cell = cell_deg
        self._cells: dict[tuple[int, int], list[int]] = {}
        for i, (x, y) in enumerate(zip(self.lons, self.lats, strict=True)):
            self._cells.setdefault((math.floor(x / cell_deg), math.floor(y / cell_deg)), []).append(i)

    def __len__(self) -> int:
        return len(self.lons)

    def near(
        self, bbox: tuple[float, float, float, float], radius_m: float
    ) -> np.ndarray[Any, np.dtype[np.int64]]:
        """Indices of the points within `radius_m` of the box (a superset of those within that
        distance of anything inside it)."""
        x0, y0, x1, y1 = bbox
        pad_lat = radius_m / M_PER_DEG
        lat_far = min(89.0, max(abs(y0), abs(y1)) + pad_lat)
        pad_lon = radius_m / (M_PER_DEG * math.cos(math.radians(lat_far)))
        x0, x1, y0, y1 = x0 - pad_lon, x1 + pad_lon, y0 - pad_lat, y1 + pad_lat
        found: list[int] = []
        for cx in range(math.floor(x0 / self.cell), math.floor(x1 / self.cell) + 1):
            for cy in range(math.floor(y0 / self.cell), math.floor(y1 / self.cell) + 1):
                found.extend(self._cells.get((cx, cy), ()))
        if not found:
            return np.zeros(0, dtype=np.int64)
        idx = np.asarray(found, dtype=np.int64)
        keep = (
            (self.lons[idx] >= x0) & (self.lons[idx] <= x1) & (self.lats[idx] >= y0) & (self.lats[idx] <= y1)
        )
        return idx[keep]
