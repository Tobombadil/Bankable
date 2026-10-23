"""Read the prospect polygon layers (GeoParquet 1.1 with a bbox covering) and test points against them.

No spatial database and no shapely: a query reads only the row groups whose bbox statistics (the
columns the file's `covering` names: the `bbox` struct's `xmin`/`ymin`/`xmax`/`ymax` fields as the
writer makes them) overlap the query box (the writer sorts rows along a Z-order curve, so a point
touches one or two groups), decodes those rows' WKB into numpy rings once per row group (LRU-cached), and
runs an even-odd ray-casting test vectorised over the query points. Even-odd across every ring of a
feature handles holes, and a valid multipolygon's parts never overlap, so the test is exact for valid
input.

A layer may also say what area it covers (`coverage` in the file's `inframation` metadata, written by
`pipeline/context/prospect/coverage.py`): the build's region box, or the states a state-filtered source
was fetched for, with their outline. Outside it the sampler answers "no data", not "outside every
polygon".
"""

from __future__ import annotations

import json
import pathlib
import struct
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow.parquet as pq  # type: ignore[import-untyped, unused-ignore]

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

#: Edges and points per numpy pass; a temporary is at most POINT_CHUNK x EDGE_CHUNK x 8 bytes (8 MB).
EDGE_CHUNK = 1024
POINT_CHUNK = 1024
ROW_GROUP_CACHE = 64
#: The keys of a GeoParquet 1.1 bbox covering, in (x0, y0, x1, y1) order.
BBOX_KEYS = ("xmin", "ymin", "xmax", "ymax")


class WkbError(ValueError):
    pass


def decode_wkb(buf: bytes) -> list[list[FloatArray]]:
    """WKB Polygon/MultiPolygon -> list of polygons, each a list of (n, 2) float rings."""
    polys: list[list[FloatArray]] = []

    def read(offset: int) -> int:
        order = buf[offset]
        bo = "<" if order == 1 else ">"
        (flags,) = struct.unpack_from(bo + "I", buf, offset + 1)
        # ISO WKB encodes Z/M as +1000/+2000/+3000 on the type; EWKB as the two high bits.
        iso = flags & 0xFFFF
        has_z = bool(flags & 0x80000000) or iso // 1000 in (1, 3)
        has_m = bool(flags & 0x40000000) or iso // 1000 in (2, 3)
        gtype = iso % 1000
        dims = 2 + int(has_z) + int(has_m)
        offset += 5
        if gtype == 3:
            (nrings,) = struct.unpack_from(bo + "I", buf, offset)
            offset += 4
            rings: list[FloatArray] = []
            for _ in range(nrings):
                (npts,) = struct.unpack_from(bo + "I", buf, offset)
                offset += 4
                arr = np.frombuffer(buf, dtype=np.dtype(bo + "f8"), count=dims * npts, offset=offset)
                rings.append(arr.reshape(npts, dims)[:, :2].astype(np.float64))
                offset += 8 * dims * npts
            polys.append(rings)
            return offset
        if gtype == 6:
            (nparts,) = struct.unpack_from(bo + "I", buf, offset)
            offset += 4
            for _ in range(nparts):
                offset = read(offset)
            return offset
        raise WkbError(f"unsupported WKB geometry type {gtype}")

    read(0)
    return polys


def points_in_rings(px: FloatArray, py: FloatArray, rings: list[FloatArray]) -> BoolArray:
    """Even-odd point-in-polygon for many points against every ring of one feature."""
    inside = np.zeros(px.shape, dtype=bool)
    if px.size == 0:
        return inside
    # Points sorted by latitude, so each chunk spans a narrow band and only the edges crossing that band
    # are tested (an edge can only be crossed by a ray at y with min(y1, y2) <= y < max(y1, y2)).
    order = np.argsort(py, kind="stable")
    sx, sy = px[order], py[order]
    result = np.zeros(sx.shape, dtype=bool)
    for ring in rings:
        x1, y1 = ring[:-1, 0], ring[:-1, 1]
        x2, y2 = ring[1:, 0], ring[1:, 1]
        lo, hi = np.minimum(y1, y2), np.maximum(y1, y2)
        for p0 in range(0, sx.size, POINT_CHUNK):
            xx, yy = sx[p0 : p0 + POINT_CHUNK], sy[p0 : p0 + POINT_CHUNK]
            sel = np.nonzero((hi > yy[0]) & (lo <= yy[-1]))[0]
            if sel.size == 0:
                continue
            for e0 in range(0, sel.size, EDGE_CHUNK):
                idx = sel[e0 : e0 + EDGE_CHUNK]
                a1, b1, a2, b2 = x1[idx], y1[idx], x2[idx], y2[idx]
                spans = (b1 > yy[:, None]) != (b2 > yy[:, None])
                with np.errstate(divide="ignore", invalid="ignore"):
                    xint = a1 + (yy[:, None] - b1) * (a2 - a1) / (b2 - b1)
                crossings = np.count_nonzero(spans & (xx[:, None] < xint), axis=1)
                result[p0 : p0 + POINT_CHUNK] ^= (crossings % 2).astype(bool)
    inside[order] = result
    return inside


@dataclass
class Feature:
    props: dict[str, Any]
    polygons: list[list[FloatArray]]
    bbox: tuple[float, float, float, float]

    def contains(self, px: FloatArray, py: FloatArray) -> BoolArray:
        x0, y0, x1, y1 = self.bbox
        result = np.zeros(px.shape, dtype=bool)
        cand = (px >= x0) & (px <= x1) & (py >= y0) & (py <= y1)
        if not cand.any():
            return result
        rings = [ring for poly in self.polygons for ring in poly]
        result[cand] = points_in_rings(px[cand], py[cand], rings)
        return result


class PolygonLayer:
    """One GeoParquet polygon layer, opened once per process."""

    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self._pf = pq.ParquetFile(self.path)
        meta = self._pf.schema_arrow.metadata or {}
        self.geo = json.loads(meta.get(b"geo", b"{}"))
        self.extra = json.loads(meta.get(b"inframation", b"{}"))
        primary = self.geo.get("primary_column", "geometry")
        covering = ((self.geo.get("columns") or {}).get(primary) or {}).get("covering", {}).get("bbox")
        if not covering or set(covering) != set(BBOX_KEYS):
            raise ValueError(f"{self.path.name}: no GeoParquet 1.1 bbox covering on {primary!r}")
        #: Each bbox key's column path, e.g. ("bbox", "xmin") for the struct the writer makes.
        self._bbox_paths = {k: tuple(covering[k]) for k in BBOX_KEYS}
        self._geometry_column = primary
        names = self._pf.schema_arrow.names
        skip = {primary} | {p[0] for p in self._bbox_paths.values()}
        self.attribute_columns = [n for n in names if n not in skip]
        md = self._pf.metadata
        leaf = (
            {md.row_group(0).column(i).path_in_schema: i for i in range(md.num_columns)}
            if md.num_row_groups
            else {}
        )
        bounds = []
        for g in range(md.num_row_groups):
            rg = md.row_group(g)
            stats = [rg.column(leaf[".".join(self._bbox_paths[k])]).statistics for k in BBOX_KEYS]
            if any(s is None or not s.has_min_max for s in stats):
                bounds.append((-np.inf, -np.inf, np.inf, np.inf))
            else:
                # The group's features span [min xmin, max xmax] x [min ymin, max ymax].
                bounds.append((stats[0].min, stats[1].min, stats[2].max, stats[3].max))
        self.row_group_bounds = np.array(bounds, dtype=np.float64).reshape(-1, 4)
        self.num_rows = md.num_rows
        #: Guards the row-group cache.
        self._lock = threading.Lock()
        #: Serialises every read of `self._pf`. One pyarrow ParquetFile is not safe to read from several
        #: threads at once: concurrent `read_row_group` calls on a shared file raised "ReadRangeCache did
        #: not find matching cache entry", returned rows from the wrong group, or aborted the process
        #: (measured 2026-10-23, 16 threads; services/context_sample/test_vectors.py). The API serves
        #: GET /v1/context/sample from a threadpool and the site finder samples from threads, so every
        #: read takes this lock; decoding the rows happens outside it.
        self._read_lock = threading.Lock()
        self._cache: OrderedDict[int, list[Feature]] = OrderedDict()
        cov = self.extra.get("coverage")
        #: The covered area as the file records it (without the outline), or None when it does not say.
        self.coverage: dict[str, Any] | None = None
        self._coverage_outline: Feature | None = None
        if isinstance(cov, dict) and cov.get("kind") in ("bbox", "states"):
            self.coverage = {k: v for k, v in cov.items() if k != "wkb_hex"}
            if cov["kind"] == "states":
                polys = decode_wkb(bytes.fromhex(str(cov["wkb_hex"])))
                xs = np.concatenate([ring[:, 0] for poly in polys for ring in poly])
                ys = np.concatenate([ring[:, 1] for poly in polys for ring in poly])
                outline_box = (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
                self._coverage_outline = Feature(props={}, polygons=polys, bbox=outline_box)

    def covered(self, lons: FloatArray, lats: FloatArray) -> BoolArray | None:
        """Which points lie in the area this layer was built for; None when the file does not say."""
        if self.coverage is None:
            return None
        if self._coverage_outline is not None:
            return self._coverage_outline.contains(lons, lats)
        x0, y0, x1, y1 = (float(v) for v in self.coverage["bbox"])
        return np.asarray((lons >= x0) & (lons <= x1) & (lats >= y0) & (lats <= y1))

    def _cached(self, g: int) -> list[Feature] | None:
        with self._lock:
            hit = self._cache.get(g)
            if hit is not None:
                self._cache.move_to_end(g)
            return hit

    def _group(self, g: int) -> list[Feature]:
        hit = self._cached(g)
        if hit is not None:
            return hit
        with self._read_lock:
            # Another thread may have read and cached this group while this one waited.
            hit = self._cached(g)
            if hit is not None:
                return hit
            table = self._pf.read_row_group(g)
        cols = table.to_pydict()

        def bbox_value(key: str, i: int) -> float:
            path = self._bbox_paths[key]
            value = cols[path[0]][i]
            for part in path[1:]:
                value = value[part]
            return float(value)

        feats: list[Feature] = []
        for i in range(table.num_rows):
            props = {n: cols[n][i] for n in self.attribute_columns}
            x0, y0, x1, y1 = (bbox_value(k, i) for k in BBOX_KEYS)
            polys = decode_wkb(cols[self._geometry_column][i])
            feats.append(Feature(props=props, polygons=polys, bbox=(x0, y0, x1, y1)))
        with self._lock:
            self._cache[g] = feats
            if len(self._cache) > ROW_GROUP_CACHE:
                self._cache.popitem(last=False)
        return feats

    def features_in(self, bbox: tuple[float, float, float, float]) -> list[Feature]:
        x0, y0, x1, y1 = bbox
        b = self.row_group_bounds
        groups = np.nonzero((b[:, 0] <= x1) & (b[:, 2] >= x0) & (b[:, 1] <= y1) & (b[:, 3] >= y0))[0]
        out: list[Feature] = []
        for g in groups:
            for f in self._group(int(g)):
                fx0, fy0, fx1, fy1 = f.bbox
                if fx0 <= x1 and fx1 >= x0 and fy0 <= y1 and fy1 >= y0:
                    out.append(f)
        return out

    def containing(self, lon: float, lat: float) -> list[Feature]:
        px, py = np.array([lon]), np.array([lat])
        return [f for f in self.features_in((lon, lat, lon, lat)) if bool(f.contains(px, py)[0])]

    def membership(self, lons: FloatArray, lats: FloatArray) -> list[tuple[Feature, BoolArray]]:
        """Every feature that contains at least one of the points, with its per-point mask."""
        if lons.size == 0:
            return []
        bbox = (float(lons.min()), float(lats.min()), float(lons.max()), float(lats.max()))
        out: list[tuple[Feature, BoolArray]] = []
        for f in self.features_in(bbox):
            mask = f.contains(lons, lats)
            if mask.any():
                out.append((f, mask))
        return out
