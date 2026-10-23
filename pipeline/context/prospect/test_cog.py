# ruff: noqa: S101 - asserts are the point (docs/04 E-6)
"""The GDAL-free COG writer round-trips through the reader and lays the file out cloud-optimised."""

from __future__ import annotations

import pathlib
import struct

import numpy as np
import pytest

from pipeline.context.prospect.cog import Grid, gdal_escape, quantise, write_cog
from services.context_sample.geotiff import GeoTiff


def _grid(h: int = 300, w: int = 520) -> Grid:
    rows, cols = np.mgrid[0:h, 0:w]
    data = ((rows * 7 + cols * 3) % 60000).astype("uint16")
    data[5, 5] = 65535
    return Grid(data=data, west=-106.0, north=36.5, dx=0.01, dy=0.01)


def test_round_trip_values_scale_and_metadata(tmp_path: pathlib.Path) -> None:
    g = _grid()
    out = tmp_path / "x.tif"
    summary = write_cog(
        out, g, nodata=65535, scale=0.001, metadata={"notice": 'a & b <c> "q"', "source_id": "s"}
    )
    assert summary["overviews"] == 2 and summary["width"] == 520
    with GeoTiff(out) as t:
        assert (t.width, t.height, t.block_w, t.compression, t.predictor) == (520, 300, 256, 8, 2)
        assert t.scale == pytest.approx(0.001) and t.nodata == 65535
        assert t.metadata == {"notice": 'a & b <c> "q"', "source_id": "s"}
        back = t.read_window(0, 0, 300, 520)
        assert np.array_equal(back, g.data)
        lon, lat = -106.0 + 10.5 * 0.01, 36.5 - 20.5 * 0.01
        assert t.value_at(lon, lat) == pytest.approx((20 * 7 + 10 * 3) * 0.001)
        assert t.value_at(-106.0 + 5.5 * 0.01, 36.5 - 5.5 * 0.01) is None


def test_layout_is_cloud_optimised(tmp_path: pathlib.Path) -> None:
    """Every IFD before any tile data; overview tiles before full-resolution tiles."""
    out = tmp_path / "x.tif"
    write_cog(out, _grid(), nodata=65535)
    raw = out.read_bytes()
    ifds, offsets = [], []
    pos = struct.unpack("<I", raw[4:8])[0]
    while pos:
        ifds.append(pos)
        (n,) = struct.unpack("<H", raw[pos : pos + 2])
        tile_offsets: list[int] = []
        for i in range(n):
            tag, _typ, count, value = struct.unpack("<HHII", raw[pos + 2 + 12 * i : pos + 14 + 12 * i])
            if tag == 324:
                tile_offsets = (
                    [value]
                    if count == 1
                    else list(struct.unpack(f"<{count}I", raw[value : value + 4 * count]))
                )
        offsets.append(tile_offsets)
        pos = struct.unpack("<I", raw[pos + 2 + 12 * n : pos + 6 + 12 * n])[0]
    assert len(ifds) == 3
    first_tile = min(min(o) for o in offsets)
    assert max(ifds) < first_tile
    assert max(offsets[-1]) < min(offsets[0]), "smallest overview first, full resolution last"


def test_quantise_maps_invalid_and_out_of_range_to_nodata() -> None:
    v = np.array([5.8079996, np.nan, 0.0, 70.0, -1.0])
    q = quantise(v, scale=0.001, nodata=65535, invalid=0.0)
    assert q.tolist() == [5808, 65535, 65535, 65535, 65535]


def test_clip_snaps_outward_to_whole_cells() -> None:
    g = _grid()
    c = g.clip((-105.955, 36.0, -105.0, 36.45))
    assert c.west == pytest.approx(-105.96) and c.north == pytest.approx(36.45)
    assert c.data.shape == (45, 96)
    with pytest.raises(ValueError):
        g.clip((0.0, 0.0, 1.0, 1.0))


def test_refuses_float_data(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError):
        write_cog(tmp_path / "x.tif", Grid(np.zeros((2, 2)), 0, 0, 1, 1), nodata=0)


def test_gdal_escape_is_double() -> None:
    assert gdal_escape("a&b") == "a&amp;amp;b"


def test_band_wise_read_matches_the_whole_window(tmp_path: pathlib.Path) -> None:
    """read_scaled quantises band by band; the stored grid equals quantising the whole clip at once, and
    values outside the valid range become nodata."""
    from pipeline.context.prospect.rasters import read_grid, read_scaled

    rows, cols = np.mgrid[0:300, 0:520]
    phys = (rows * 0.01 + cols * 0.001).astype("float64")
    src = tmp_path / "src.tif"
    stored = np.round(phys / 0.001).astype("uint16")
    write_cog(src, Grid(data=stored, west=-106.0, north=36.5, dx=0.01, dy=0.01), nodata=65535, scale=0.001)
    bbox = (-105.955, 34.0, -101.0, 36.45)
    whole = read_grid(src, bbox)
    grid, stats = read_scaled(src, bbox, scale=0.01, valid_range=(0.0, 2.5), band_rows=7)
    expected = quantise(np.where(whole.data > 2.5, np.nan, whole.data), scale=0.01, nodata=65535)
    assert grid.data.shape == whole.data.shape and np.array_equal(grid.data, expected)
    assert (grid.west, grid.north) == pytest.approx((whole.west, whole.north))
    kept = whole.data[whole.data <= 2.5]
    assert stats["value_max"] == pytest.approx(kept.max(), abs=1e-4)
    assert stats["value_mean"] == pytest.approx(kept.mean(), abs=1e-4)
