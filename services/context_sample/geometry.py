"""Corridor geometry for `sample_line`: the buffer around a line, as an even lattice of points.

A polygon layer's "share of the buffer" and a raster's mean over the buffer are both estimated from one
square lattice laid over the buffer: every lattice point within `buffer` of the line counts once, so the
share of points inside a polygon estimates the share of the buffer's area inside it (the error falls
with spacing; the response states the spacing and point count). No shapely.

**Lattice plane.** Each group of nearby parts (parts whose buffered boxes touch; far-apart parts of a
MultiLineString are separate groups) gets one square grid in a sinusoidal plane centred on the group's
mean longitude, x = (lon - lon0) cos(lat) R, y = (lat - lat0) R. The projection is equal-area, so every
lattice point stands for the same ground area wherever it falls in the group.

**Distance test.** The line is cut into pieces about two buffers long (a long segment is split along
its straight lon/lat line, which is what a GeoJSON segment means, RFC 7946 §3.1.1). A grid cell is kept
when it lies within the buffer of a piece, measured in a sinusoidal plane centred on that piece, where
the only distortion is a shear of about (distance from the piece's centre) x tan(lat) / R. Measured
2026-10-23 on 210-mile diagonal lines at 26-28.5 N and 47-49.4 N: the kept points' great-circle distance
from the line stays within 0.05% of the buffer at 2 and 10 miles (0.5% at 0.25 miles, the resolution
of the check itself), and points x cell area matches the buffer's area within 0.2% (0.7% at 10 miles).
The single equirectangular plane this replaced scaled x by cos(mean latitude), about 1.6% off at the
ends of a 250-mile north-south line. Cells kept by several pieces are merged by grid index, so a point
counts once, and the work follows the corridor's area, not its bounding box: a diagonal or bent line, or
two parts 300 miles apart, costs what a straight line of the same length does.

**Spacing.** Chosen to land near `TARGET_POINTS`, but never coarser than a sixth of the band's width
(`MIN_CELLS_ACROSS`), so a long narrow corridor gets more points instead of a band one or two cells wide.
A caller may set `spacing_m`; if that would test more than `MAX_LATTICE_POINTS` cells it is refused with
the smallest spacing that fits, and the automatic spacing coarsens itself to fit instead.

`length_m` is the sum of great-circle distances between consecutive vertices.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

EARTH_RADIUS_M = 6_371_008.8
METRES_PER_MILE = 1_609.344
METRES_PER_DEGREE = EARTH_RADIUS_M * math.pi / 180
#: Lattice points aimed for per corridor; spacing is chosen to land near it.
TARGET_POINTS = 1_500
MIN_SPACING_M = 30.0
#: ...but never fewer than this many lattice columns across the band (2 x buffer): a long, narrow
#: corridor gets more points rather than a band one or two cells wide, whose share depends on how the
#: grid happens to line up with it.
MIN_CELLS_ACROSS = 6
#: Hard ceilings so a request cannot ask for an unbounded computation.
MAX_BUFFER_MI = 10.0
MAX_LINE_MI = 250.0
MAX_VERTICES = 5_000
#: Candidate grid cells (the pieces' buffered boxes at the spacing) one request may test. The automatic
#: spacing coarsens itself to fit; a caller-chosen `spacing_m` that would exceed it is refused with the
#: smallest spacing that fits.
MAX_LATTICE_POINTS = 400_000
SEGMENT_CHUNK = 512
POINT_CHUNK = 2_048
#: A piece of the line is cut once it is this many buffers long.
PIECE_BUFFERS = 2.0
#: A piece's candidate box is its box in the group's plane widened by this many buffers: distances in the
#: group plane differ from the piece plane's by the shear between them (under 2% within 250 miles).
BOX_MARGIN = 1.05


class LineError(ValueError):
    """The geometry is not a usable line (the message says why)."""


@dataclass(frozen=True)
class Lattice:
    lons: FloatArray
    lats: FloatArray
    spacing_m: float
    length_m: float
    buffer_m: float
    #: Groups of parts laid out separately (1 for a single line or for parts near each other).
    groups: int = 1

    @property
    def count(self) -> int:
        return int(self.lons.size)


def parse_lines(geometry: Any) -> list[FloatArray]:
    """GeoJSON LineString / MultiLineString (dict or Feature), or a bare list of [lon, lat] pairs ->
    list of (n, 2) arrays."""
    if isinstance(geometry, dict) and geometry.get("type") == "Feature":
        geometry = geometry.get("geometry")
    if isinstance(geometry, dict):
        gtype, coords = geometry.get("type"), geometry.get("coordinates")
        if gtype == "LineString":
            parts = [coords]
        elif gtype == "MultiLineString":
            parts = list(coords or [])
        else:
            raise LineError(f"expected LineString or MultiLineString, got {gtype}")
    elif isinstance(geometry, Sequence):
        parts = [geometry]
    else:
        raise LineError("geometry must be GeoJSON or a list of [lon, lat] pairs")
    lines: list[FloatArray] = []
    total = 0
    for part in parts:
        try:
            arr = np.asarray(part, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise LineError(f"coordinates are not numeric pairs: {exc}") from exc
        if arr.ndim != 2 or arr.shape[1] < 2 or arr.shape[0] < 2:
            raise LineError("each line needs at least two [lon, lat] points")
        arr = arr[:, :2]
        if not np.isfinite(arr).all() or (np.abs(arr[:, 0]) > 180).any() or (np.abs(arr[:, 1]) > 90).any():
            raise LineError("coordinates out of range")
        total += arr.shape[0]
        lines.append(arr)
    if not lines:
        raise LineError("no line in the geometry")
    if total > MAX_VERTICES:
        raise LineError(f"at most {MAX_VERTICES} vertices per request")
    return lines


def _segment_lengths_m(line: FloatArray) -> FloatArray:
    """Great-circle (haversine) length of each segment of a lon/lat polyline, metres."""
    lon, lat = np.radians(line[:, 0]), np.radians(line[:, 1])
    a = np.sin(np.diff(lat) / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(np.diff(lon) / 2) ** 2
    return np.asarray(2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0))))


def great_circle_m(line: FloatArray) -> float:
    """Length of a lon/lat polyline along great circles (haversine), metres."""
    return float(_segment_lengths_m(line).sum())


def _groups(lines: list[FloatArray], buffer_m: float) -> list[list[int]]:
    """Indices of the parts, grouped so that parts whose buffered lon/lat boxes overlap share a group.
    A point within the buffer of two parts lies in both boxes, so no point is near two groups."""
    pad_lat = buffer_m / METRES_PER_DEGREE * 1.01
    boxes = []
    for ln in lines:
        top = min(89.0, float(np.abs(ln[:, 1]).max()) + pad_lat)
        pad_lon = buffer_m / (METRES_PER_DEGREE * math.cos(math.radians(top))) * 1.01
        lo, hi = ln.min(axis=0), ln.max(axis=0)
        boxes.append((lo[0] - pad_lon, lo[1] - pad_lat, hi[0] + pad_lon, hi[1] + pad_lat))
    b = np.array(boxes)
    touch = (
        (b[:, None, 0] <= b[None, :, 2])
        & (b[:, None, 2] >= b[None, :, 0])
        & (b[:, None, 1] <= b[None, :, 3])
        & (b[:, None, 3] >= b[None, :, 1])
    )
    group = np.full(len(lines), -1)
    out: list[list[int]] = []
    for start in range(len(lines)):
        if group[start] >= 0:
            continue
        group[start] = len(out)
        members, frontier = [start], [start]
        while frontier:
            nxt = np.nonzero(touch[frontier].any(axis=0) & (group < 0))[0]
            group[nxt] = len(out)
            frontier = nxt.tolist()
            members += frontier
        out.append(sorted(members))
    return out


@dataclass(frozen=True)
class _Plane:
    """A sinusoidal (equal-area) plane centred on (lon0, lat0), metres."""

    lon0: float
    lat0: float

    def forward(self, lon: FloatArray, lat: FloatArray) -> tuple[FloatArray, FloatArray]:
        x = (lon - self.lon0) * np.cos(np.radians(lat)) * METRES_PER_DEGREE
        return x, (lat - self.lat0) * METRES_PER_DEGREE

    def inverse(self, x: FloatArray, y: FloatArray) -> tuple[FloatArray, FloatArray]:
        lat = y / METRES_PER_DEGREE + self.lat0
        return x / (METRES_PER_DEGREE * np.cos(np.radians(lat))) + self.lon0, lat


def _pieces(lonlat: FloatArray, piece_m: float) -> list[FloatArray]:
    """Cut a lon/lat polyline into runs of consecutive segments about `piece_m` long (great-circle); a
    segment longer than `piece_m` is split at even points along it in lon/lat, so a segment stays the
    straight lon/lat line GeoJSON means (RFC 7946 §3.1.1) and a piece's box stays near the corridor."""
    out: list[FloatArray] = []
    current = [lonlat[0]]
    run = 0.0
    for a, b, seg in zip(lonlat[:-1], lonlat[1:], _segment_lengths_m(lonlat).tolist(), strict=True):
        if seg > piece_m:
            if len(current) > 1:
                out.append(np.array(current))
            cuts = math.ceil(seg / piece_m)
            pts = a + np.linspace(0.0, 1.0, cuts + 1)[:, None] * (b - a)
            out += [pts[i : i + 2] for i in range(cuts)]
            current, run = [b], 0.0
            continue
        current.append(b)
        run += seg
        if run >= piece_m:
            out.append(np.array(current))
            current, run = [b], 0.0
    if len(current) > 1:
        out.append(np.array(current))
    return out


def _min_distance(px: FloatArray, py: FloatArray, segs: FloatArray) -> FloatArray:
    """Distance from each point to the nearest of `segs` (m, 4: x1 y1 x2 y2), in point and segment
    chunks so a temporary never exceeds POINT_CHUNK x SEGMENT_CHUNK floats."""
    best = np.full(px.shape, np.inf)
    for p0 in range(0, px.size, POINT_CHUNK):
        qx, qy = px[p0 : p0 + POINT_CHUNK, None], py[p0 : p0 + POINT_CHUNK, None]
        for start in range(0, segs.shape[0], SEGMENT_CHUNK):
            s = segs[start : start + SEGMENT_CHUNK]
            x1, y1, x2, y2 = s[:, 0], s[:, 1], s[:, 2], s[:, 3]
            dx, dy = x2 - x1, y2 - y1
            len2 = dx * dx + dy * dy
            with np.errstate(divide="ignore", invalid="ignore"):
                t = ((qx - x1) * dx + (qy - y1) * dy) / len2
            t = np.clip(np.nan_to_num(t, nan=0.0), 0.0, 1.0)
            d = np.hypot(qx - (x1 + t * dx), qy - (y1 + t * dy)).min(axis=1)
            best[p0 : p0 + POINT_CHUNK] = np.minimum(best[p0 : p0 + POINT_CHUNK], d)
    return best


@dataclass(frozen=True)
class _Group:
    plane: _Plane
    #: Each piece as (n, 2) lon/lat points.
    pieces: list[FloatArray]
    origin: tuple[float, float]


def _cell_range(lo: float, hi: float, origin: float, step: float) -> tuple[int, int]:
    """Grid indices i (cell centre origin + (i + 0.5) step) whose centres fall in [lo, hi]."""
    return math.ceil((lo - origin) / step - 0.5), math.floor((hi - origin) / step - 0.5)


def _piece_cells(g: _Group, piece: FloatArray, buffer_m: float, step: float) -> tuple[int, int, int, int]:
    margin = BOX_MARGIN * buffer_m
    x, y = g.plane.forward(piece[:, 0], piece[:, 1])
    i0, i1 = _cell_range(float(x.min()) - margin, float(x.max()) + margin, g.origin[0], step)
    j0, j1 = _cell_range(float(y.min()) - margin, float(y.max()) + margin, g.origin[1], step)
    return i0, i1, j0, j1


def _candidates(groups: list[_Group], buffer_m: float, step: float) -> int:
    total = 0
    for g in groups:
        for piece in g.pieces:
            i0, i1, j0, j1 = _piece_cells(g, piece, buffer_m, step)
            total += max(0, i1 - i0 + 1) * max(0, j1 - j0 + 1)
    return total


def _centres(g: _Group, ii: IntArray, jj: IntArray, step: float) -> tuple[FloatArray, FloatArray]:
    """Lon/lat of the grid cells (ii, jj) of a group."""
    x = np.asarray(g.origin[0] + (ii + 0.5) * step, dtype=np.float64)
    y = np.asarray(g.origin[1] + (jj + 0.5) * step, dtype=np.float64)
    return g.plane.inverse(x, y)


def _lay_out(g: _Group, buffer_m: float, step: float) -> tuple[FloatArray, FloatArray]:
    keys: list[IntArray] = []
    for piece in g.pieces:
        i0, i1, j0, j1 = _piece_cells(g, piece, buffer_m, step)
        if i1 < i0 or j1 < j0:
            continue
        grid_i, grid_j = np.meshgrid(
            np.arange(i0, i1 + 1, dtype=np.int64), np.arange(j0, j1 + 1, dtype=np.int64)
        )
        ii, jj = grid_i.ravel(), grid_j.ravel()
        # Cell centres and the piece, both moved to the piece's own plane for the distance test.
        lon, lat = _centres(g, ii, jj, step)
        local = _Plane(float(piece[:, 0].mean()), float(piece[:, 1].mean()))
        px, py = local.forward(lon, lat)
        vx, vy = local.forward(piece[:, 0], piece[:, 1])
        segs = np.column_stack([vx[:-1], vy[:-1], vx[1:], vy[1:]])
        keep = _min_distance(px, py, segs) <= buffer_m
        keys.append(np.column_stack([ii[keep], jj[keep]]).astype(np.int64))
    if not keys:
        return np.empty(0), np.empty(0)
    cells = np.unique(np.vstack(keys), axis=0)
    return _centres(g, cells[:, 0], cells[:, 1], step)


def corridor_lattice(
    lines: list[FloatArray],
    buffer_mi: float,
    *,
    target_points: int = TARGET_POINTS,
    spacing_m: float | None = None,
) -> Lattice:
    if not (0 < buffer_mi <= MAX_BUFFER_MI):
        raise LineError(f"buffer_mi must be > 0 and at most {MAX_BUFFER_MI}")
    if spacing_m is not None and (not math.isfinite(spacing_m) or spacing_m <= 0):
        raise LineError("spacing_m must be a positive number")
    length = sum(great_circle_m(ln) for ln in lines)
    if length / METRES_PER_MILE > MAX_LINE_MI:
        raise LineError(f"line longer than {MAX_LINE_MI} miles; split it into segments")
    buffer_m = buffer_mi * METRES_PER_MILE
    groups: list[_Group] = []
    for members in _groups(lines, buffer_m):
        allpts = np.vstack([lines[i] for i in members])
        plane = _Plane(float(allpts[:, 0].mean()), float(allpts[:, 1].mean()))
        pieces = [pc for i in members for pc in _pieces(lines[i], PIECE_BUFFERS * buffer_m)]
        gx, gy = plane.forward(allpts[:, 0], allpts[:, 1])
        origin = (float(gx.min()) - 2 * buffer_m, float(gy.min()) - 2 * buffer_m)
        groups.append(_Group(plane, pieces, origin))
    # Area of the buffer if the parts did not overlap: the sides plus a disc of end caps per part.
    area = 2 * buffer_m * length + math.pi * buffer_m**2 * len(lines)
    if spacing_m is not None:
        step = float(spacing_m)
    else:
        auto = min(math.sqrt(area / target_points), 2 * buffer_m / MIN_CELLS_ACROSS)
        step = max(MIN_SPACING_M, auto)
    candidates = _candidates(groups, buffer_m, step)
    if candidates > MAX_LATTICE_POINTS:
        # Candidates scale with 1 / step^2; the 2% keeps the rounding of cell ranges under the cap.
        fit = step * math.sqrt(candidates / MAX_LATTICE_POINTS) * 1.02
        if spacing_m is not None:
            raise LineError(
                f"spacing_m {spacing_m:g} is too fine for this corridor; use at least {math.ceil(fit)} m"
            )
        step = fit
    lons, lats = [], []
    for g in groups:
        gx, gy = _lay_out(g, buffer_m, step)
        lons.append(gx)
        lats.append(gy)
    return Lattice(
        lons=np.concatenate(lons),
        lats=np.concatenate(lats),
        spacing_m=step,
        length_m=length,
        buffer_m=buffer_m,
        groups=len(groups),
    )
