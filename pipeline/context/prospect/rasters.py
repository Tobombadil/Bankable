"""Raster steps shared by the solar, wind and seismic builders: clip a source GeoTIFF to a region and
write the region as a scaled-integer COG carrying its provenance.

Two paths, one result:

- `read_scaled` + `write_quantised` (solar, wind): the source is read in bands of `BAND_ROWS` rows and
  each band is quantised at once, so the only full-size array is the stored integer grid (2 bytes a
  cell). A CONUS clip of the Global Wind Atlas (0.0025 degree, about 23,000 x 10,000 cells) is then
  about 470 MB in memory instead of 1.9 GB per float64 copy; the GitHub Actions runner has 7 GB.
- `write_scaled` (seismic): a small float grid already in memory (the NSHM CSV) is quantised and written.

The value statistics in the summary (min, max, mean) are of the physical values that were stored.
"""

from __future__ import annotations

import math
import pathlib
from typing import Any

import numpy as np

from pipeline.context.prospect.cog import Grid, cell_window, quantise, write_cog
from pipeline.context.prospect.common import Provenance
from services.context_sample.geotiff import GeoTiff

#: Source rows read and quantised per pass.
BAND_ROWS = 512


class _Stats:
    """Running min / max / mean of the valid physical values."""

    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.low = math.inf
        self.high = -math.inf

    def add(self, valid: np.ndarray[Any, Any]) -> None:
        if valid.size:
            self.count += int(valid.size)
            self.total += float(valid.sum())
            self.low = min(self.low, float(valid.min()))
            self.high = max(self.high, float(valid.max()))

    def summary(self) -> dict[str, float]:
        if not self.count:
            return {}
        return {
            "value_min": round(self.low, 4),
            "value_max": round(self.high, 4),
            "value_mean": round(self.total / self.count, 4),
        }


def _window(tif: GeoTiff, bbox: tuple[float, float, float, float], name: str) -> tuple[int, int, int, int]:
    t = tif.transform
    c0, r0, c1, r1 = cell_window(bbox, west=t.west, north=t.north, dx=t.dx, dy=t.dy)
    c1, r1 = min(tif.width, c1), min(tif.height, r1)
    if c1 <= c0 or r1 <= r0:
        raise ValueError(f"{name} does not overlap {bbox}")
    return c0, r0, c1, r1


def _physical(tif: GeoTiff, raw: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    values = raw.astype(np.float64)
    if tif.nodata is not None:
        values[values == tif.nodata] = np.nan
    return values * tif.scale + tif.offset


def read_grid(path: pathlib.Path, bbox: tuple[float, float, float, float]) -> Grid:
    """The cells of a lon/lat GeoTIFF that overlap `bbox`, as physical values (float64, NaN = no data).
    For small inputs and tests; the builders use `read_scaled`."""
    with GeoTiff(path) as tif:
        t = tif.transform
        c0, r0, c1, r1 = _window(tif, bbox, path.name)
        values = _physical(tif, tif.read_window(r0, c0, r1 - r0, c1 - c0))
        return Grid(data=values, west=t.west + c0 * t.dx, north=t.north - r0 * t.dy, dx=t.dx, dy=t.dy)


def read_scaled(
    path: pathlib.Path,
    bbox: tuple[float, float, float, float],
    *,
    scale: float,
    nodata: int = 65535,
    dtype: str = "uint16",
    invalid: Any = None,
    valid_range: tuple[float, float] | None = None,
    band_rows: int = BAND_ROWS,
) -> tuple[Grid, dict[str, float]]:
    """The cells of `path` that overlap `bbox`, quantised band by band (`cog.quantise`): physical values
    outside `valid_range` or equal to `invalid` are stored as `nodata`. Returns the integer grid and the
    statistics of the physical values kept."""
    stats = _Stats()
    with GeoTiff(path) as tif:
        t = tif.transform
        c0, r0, c1, r1 = _window(tif, bbox, path.name)
        out = np.empty((r1 - r0, c1 - c0), dtype=np.dtype(dtype))
        for b0 in range(r0, r1, band_rows):
            rows = min(band_rows, r1 - b0)
            values = _physical(tif, tif.read_window(b0, c0, rows, c1 - c0))
            if valid_range is not None:
                lo, hi = valid_range
                with np.errstate(invalid="ignore"):
                    values[(values < lo) | (values > hi)] = np.nan
            stored = quantise(values, scale=scale, nodata=nodata, dtype=dtype, invalid=invalid)
            out[b0 - r0 : b0 - r0 + rows] = stored
            stats.add(values[stored != nodata])
        grid = Grid(data=out, west=t.west + c0 * t.dx, north=t.north - r0 * t.dy, dx=t.dx, dy=t.dy)
    return grid, stats.summary()


def write_quantised(
    grid: Grid,
    out: pathlib.Path,
    *,
    scale: float,
    provenance: Provenance,
    stats: dict[str, float],
    nodata: int = 65535,
    extra_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Write an already-quantised grid as a COG with the provenance items; the summary carries `stats`."""
    meta = {**provenance.cog_items(), **(extra_metadata or {})}
    summary = write_cog(out, grid, nodata=nodata, scale=scale, metadata=meta)
    summary.update(stats)
    return summary


def write_scaled(
    grid: Grid,
    out: pathlib.Path,
    *,
    scale: float,
    provenance: Provenance,
    nodata: int = 65535,
    dtype: str = "uint16",
    invalid: Any = None,
    extra_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Quantise a float grid held in memory and write it (`write_quantised`)."""
    stored = quantise(grid.data, scale=scale, nodata=nodata, dtype=dtype, invalid=invalid)
    stats = _Stats()
    stats.add(np.asarray(grid.data, dtype=np.float64)[stored != nodata])
    return write_quantised(
        Grid(data=stored, west=grid.west, north=grid.north, dx=grid.dx, dy=grid.dy),
        out,
        scale=scale,
        provenance=provenance,
        stats=stats.summary(),
        nodata=nodata,
        extra_metadata=extra_metadata,
    )
