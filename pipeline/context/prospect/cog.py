"""Write a cloud-optimised GeoTIFF without GDAL (numpy + zlib only).

The prospect rasters (solar, wind, seismic) are single-band integer grids on a geographic (EPSG:4326)
lattice, so the writer covers exactly that and nothing else:

- classic little-endian TIFF (the build refuses a file over 4 GB; a CONUS 250 m uint16 grid compresses
  to a few hundred MB);
- one band, `uint16` or `int16`, tiled (default 256 x 256), Deflate with horizontal-differencing
  predictor 2 -- the combination every GDAL since 2.x reads and range-requests well;
- reduced-resolution overviews (nearest neighbour, halving until the image fits one tile), so a map can
  draw the layer later without reading the full grid;
- the COG layout: every IFD at the front of the file, then the tile data, smallest overview first and
  full resolution last, so a reader learns the whole structure from the first few kilobytes;
- georeferencing by `ModelPixelScale` + `ModelTiepoint` and GeoKeys for EPSG:4326, PixelIsArea;
- `GDAL_NODATA`, and `GDAL_METADATA` carrying the scale and offset that turn the stored integer into the
  physical value, plus the provenance items (source id, url, retrieved_at, licence, vintage, notice).

`services/context_sample/geotiff.py` reads what this writes; `test_cog.py` round-trips the two.
"""

from __future__ import annotations

import math
import pathlib
import struct
import zlib
from dataclasses import dataclass
from html import escape
from typing import Any

import numpy as np
import numpy.typing as npt

TILE = 256
DEFLATE_LEVEL = 6
#: Classic TIFF offsets are 32-bit.
MAX_CLASSIC_BYTES = 4_000_000_000

_SHORT, _LONG, _DOUBLE, _ASCII = 3, 4, 12, 2


#: Cell-index tolerance: a bbox edge within a millionth of a cell of a cell edge is on that edge. Without it,
#: (36.5 - 36.45) / 0.01 = 4.9999999 floors to 4 and the clip gains a whole row of cells.
EDGE_EPS = 1e-6


def cell_window(
    bbox: tuple[float, float, float, float], *, west: float, north: float, dx: float, dy: float
) -> tuple[int, int, int, int]:
    """(col0, row0, col1, row1): the cells of a north-up grid that overlap `bbox`, snapped outward to whole
    cells (col0/row0 clamped at 0; the caller clamps col1/row1 to the grid's size)."""
    min_lon, min_lat, max_lon, max_lat = bbox
    c0 = max(0, math.floor((min_lon - west) / dx + EDGE_EPS))
    c1 = math.ceil((max_lon - west) / dx - EDGE_EPS)
    r0 = max(0, math.floor((north - max_lat) / dy + EDGE_EPS))
    r1 = math.ceil((north - min_lat) / dy - EDGE_EPS)
    return c0, r0, c1, r1


@dataclass(frozen=True)
class Grid:
    """A north-up lon/lat grid: `data[row, col]` covers lon [west + col*dx, west + (col+1)*dx) and
    lat (north - (row+1)*dy, north - row*dy]."""

    data: npt.NDArray[Any]
    west: float
    north: float
    dx: float
    dy: float

    def clip(self, bbox: tuple[float, float, float, float]) -> Grid:
        """The cells that overlap (min_lon, min_lat, max_lon, max_lat), snapped outward to whole cells."""
        c0, r0, c1, r1 = cell_window(bbox, west=self.west, north=self.north, dx=self.dx, dy=self.dy)
        c1, r1 = min(self.data.shape[1], c1), min(self.data.shape[0], r1)
        if c1 <= c0 or r1 <= r0:
            raise ValueError(f"bbox {bbox} does not overlap the grid")
        return Grid(
            data=self.data[r0:r1, c0:c1],
            west=self.west + c0 * self.dx,
            north=self.north - r0 * self.dy,
            dx=self.dx,
            dy=self.dy,
        )


def quantise(
    values: npt.NDArray[Any], *, scale: float, nodata: int, dtype: str = "uint16", invalid: Any = None
) -> npt.NDArray[Any]:
    """Physical values -> stored integers (round(value / scale)); NaN, `invalid` and out-of-range values
    become `nodata`. The caller picks `scale` at or below the source's own precision, so nothing the
    source stated is lost (e.g. NSRDB's 3-decimal kWh/m²/day at scale 0.001)."""
    info = np.iinfo(np.dtype(dtype))
    v = np.asarray(values, dtype=np.float64)
    bad = ~np.isfinite(v)
    if invalid is not None:
        bad |= v == invalid
    q = np.round(v / scale)
    bad |= (q < info.min) | (q > info.max) | (q == nodata)
    out = np.where(bad, nodata, q).astype(dtype)
    return out


def gdal_escape(value: str) -> str:
    """GDAL escapes a metadata value twice (CPLEscapeString, then its XML serialiser) and unescapes it
    twice on read; matching that keeps `&`, `<`, `>` and `"` intact for GDAL and for our reader."""
    return escape(escape(value, quote=False).replace('"', "&quot;"), quote=False)


def _metadata_xml(scale: float, offset: float, items: dict[str, str]) -> str:
    parts = [
        "<GDALMetadata>",
        f'<Item name="SCALE" sample="0" role="scale">{scale!r}</Item>',
        f'<Item name="OFFSET" sample="0" role="offset">{offset!r}</Item>',
    ]
    for key, value in items.items():
        parts.append(f'<Item name="{escape(key)}">{gdal_escape(value)}</Item>')
    parts.append("</GDALMetadata>")
    return "".join(parts)


def _tiles(data: npt.NDArray[Any], nodata: int, tile: int) -> list[bytes]:
    h, w = data.shape
    out: list[bytes] = []
    for r in range(0, h, tile):
        for c in range(0, w, tile):
            block = np.full((tile, tile), nodata, dtype=data.dtype)
            part = data[r : r + tile, c : c + tile]
            block[: part.shape[0], : part.shape[1]] = part
            diff = block.copy()
            diff[:, 1:] = block[:, 1:] - block[:, :-1]  # predictor 2; integer wrap-around is intended
            out.append(zlib.compress(diff.astype(diff.dtype.newbyteorder("<")).tobytes(), DEFLATE_LEVEL))
    return out


def _overviews(data: npt.NDArray[Any], tile: int) -> list[npt.NDArray[Any]]:
    levels: list[npt.NDArray[Any]] = []
    cur = data
    while max(cur.shape) > tile:
        cur = cur[::2, ::2]
        levels.append(np.ascontiguousarray(cur))
    return levels


class _Ifd:
    def __init__(self) -> None:
        self.entries: list[tuple[int, int, list[Any] | str]] = []

    def add(self, tag: int, typ: int, values: list[Any] | str) -> None:
        self.entries.append((tag, typ, values))

    def size(self) -> int:
        return 2 + 12 * len(self.entries) + 4


def _encode_values(typ: int, values: list[Any] | str) -> tuple[bytes, int]:
    if isinstance(values, str):
        raw = values.encode("utf-8") + b"\0"
        return raw, len(raw)
    fmt = {_SHORT: "H", _LONG: "I", _DOUBLE: "d"}[typ]
    return struct.pack("<" + fmt * len(values), *values), len(values)


def write_cog(
    path: str | pathlib.Path,
    grid: Grid,
    *,
    nodata: int,
    scale: float = 1.0,
    offset: float = 0.0,
    metadata: dict[str, str] | None = None,
    tile: int = TILE,
) -> dict[str, Any]:
    """Write `grid` (integer data, already quantised) as a COG. Returns a small summary dict."""
    data = np.ascontiguousarray(grid.data)
    if data.dtype not in (np.dtype("uint16"), np.dtype("int16")):
        raise ValueError(f"write_cog takes uint16/int16 data, got {data.dtype}")
    sample_format = 1 if data.dtype == np.dtype("uint16") else 2
    levels = [data, *_overviews(data, tile)]
    encoded = [_tiles(level, nodata, tile) for level in levels]
    xml = _metadata_xml(scale, offset, metadata or {})

    ifds: list[_Ifd] = []
    for i, level in enumerate(levels):
        h, w = level.shape
        n_tiles = len(encoded[i])
        ifd = _Ifd()
        ifd.add(254, _LONG, [0 if i == 0 else 1])  # NewSubfileType: 1 = reduced-resolution image
        ifd.add(256, _LONG, [w])
        ifd.add(257, _LONG, [h])
        ifd.add(258, _SHORT, [16])
        ifd.add(259, _SHORT, [8])  # Deflate
        ifd.add(262, _SHORT, [1])  # BlackIsZero
        ifd.add(277, _SHORT, [1])
        ifd.add(284, _SHORT, [1])
        ifd.add(317, _SHORT, [2])  # horizontal differencing
        ifd.add(322, _LONG, [tile])
        ifd.add(323, _LONG, [tile])
        ifd.add(324, _LONG, [0] * n_tiles)  # patched below
        ifd.add(325, _LONG, [len(t) for t in encoded[i]])
        ifd.add(339, _SHORT, [sample_format])
        if i == 0:
            ifd.add(33550, _DOUBLE, [grid.dx, grid.dy, 0.0])
            ifd.add(33922, _DOUBLE, [0.0, 0.0, 0.0, grid.west, grid.north, 0.0])
            # GeoKeys: version 1.1.0, 3 keys: geographic model, PixelIsArea, EPSG:4326.
            ifd.add(34735, _SHORT, [1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326])
            ifd.add(42112, _ASCII, xml)
        ifd.add(42113, _ASCII, str(nodata))
        ifd.entries.sort(key=lambda e: e[0])
        ifds.append(ifd)

    # Lay out: header | IFDs (each followed by its out-of-line values) | tile data (overviews first).
    pos = 8
    ifd_offsets: list[int] = []
    value_blobs: list[list[tuple[int, bytes]]] = []
    for ifd in ifds:
        ifd_offsets.append(pos)
        pos += ifd.size()
        blobs: list[tuple[int, bytes]] = []
        for _tag, typ, values in ifd.entries:
            raw, _count = _encode_values(typ, values)
            if len(raw) > 4:
                pos += pos & 1  # word alignment
                blobs.append((pos, raw))
                pos += len(raw)
            else:
                blobs.append((-1, raw))
        value_blobs.append(blobs)
    tile_offsets: list[list[int]] = [[] for _ in levels]
    for i in reversed(range(len(levels))):
        for t in encoded[i]:
            tile_offsets[i].append(pos)
            pos += len(t)
    if pos > MAX_CLASSIC_BYTES:
        raise ValueError(f"COG would be {pos} bytes; classic TIFF is limited to 4 GB")

    # Patch tile offsets now that they are known, and re-encode those value blobs.
    for i, ifd in enumerate(ifds):
        for k, (tag, typ, _values) in enumerate(ifd.entries):
            if tag == 324:
                ifd.entries[k] = (tag, typ, tile_offsets[i])
                raw, _ = _encode_values(typ, tile_offsets[i])
                at = value_blobs[i][k][0]
                value_blobs[i][k] = (at, raw)

    out = bytearray(pos)
    out[0:8] = b"II" + struct.pack("<HI", 42, ifd_offsets[0])
    for i, ifd in enumerate(ifds):
        at = ifd_offsets[i]
        buf = bytearray(struct.pack("<H", len(ifd.entries)))
        for (tag, typ, values), (blob_at, raw) in zip(ifd.entries, value_blobs[i], strict=True):
            _, count = _encode_values(typ, values)
            if blob_at < 0:
                buf += struct.pack("<HHI", tag, typ, count) + raw.ljust(4, b"\0")
            else:
                buf += struct.pack("<HHII", tag, typ, count, blob_at)
                out[blob_at : blob_at + len(raw)] = raw
        next_ifd = ifd_offsets[i + 1] if i + 1 < len(ifds) else 0
        buf += struct.pack("<I", next_ifd)
        out[at : at + len(buf)] = buf
    for i in range(len(levels)):
        for t, off in zip(encoded[i], tile_offsets[i], strict=True):
            out[off : off + len(t)] = t

    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(bytes(out))
    tmp.replace(path)
    valid = int(np.count_nonzero(data != nodata))
    return {
        "width": int(data.shape[1]),
        "height": int(data.shape[0]),
        "overviews": len(levels) - 1,
        "bytes": len(out),
        "valid_cells": valid,
        "bbox": [
            grid.west,
            grid.north - data.shape[0] * grid.dy,
            grid.west + data.shape[1] * grid.dx,
            grid.north,
        ],
        "cell_deg": [grid.dx, grid.dy],
    }
