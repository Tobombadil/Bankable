"""The pure-Python GeoTIFF reader against GDAL-written files (recorded fixtures) and its own codecs."""

from __future__ import annotations

import pathlib
import struct

import numpy as np
import pytest

from services.context_sample.geotiff import GeoTiff, GeoTiffError, lzw_decode, packbits_decode

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
#: Each fixture is the same 37 x 23 grid from GDAL (value = 5 + col*0.01 + row*0.1, nodata at row 3 col 4)
#: written with a different layout; mk_tiff_fixtures.py in docs/25 "Prospecting layers" names the options.
GDAL_FILES = [
    "gdal_lzw_float32_strips.tif",
    "gdal_deflate_pred3_tiled.tif",
    "gdal_cog_lzw_pred2_int16.tif",
    "gdal_packbits_bigtiff.tif",
]


def _centre(t: GeoTiff, row: int, col: int) -> tuple[float, float]:
    return t.transform.west + (col + 0.5) * t.transform.dx, t.transform.north - (row + 0.5) * t.transform.dy


@pytest.mark.parametrize("name", GDAL_FILES)
def test_reads_gdal_layouts_to_the_same_values(name: str) -> None:
    with GeoTiff(FIXTURES / name) as t:
        assert (t.width, t.height) == (37, 23)
        assert t.epsg == 4326
        assert t.transform.west == pytest.approx(-101.9)
        assert t.transform.north == pytest.approx(35.25)
        for row, col in ((0, 0), (2, 10), (22, 36), (17, 5)):
            assert t.value_at(*_centre(t, row, col)) == pytest.approx(5 + col * 0.01 + row * 0.1, abs=1e-5)
        assert t.value_at(*_centre(t, 3, 4)) is None, "nodata cell"
        assert t.value_at(-50.0, 10.0) is None, "outside the grid"
        assert t.metadata.get("source_id") == "fixture"


def test_vectorised_values_match_single_reads() -> None:
    with GeoTiff(FIXTURES / "gdal_deflate_pred3_tiled.tif") as t:
        rng = np.random.default_rng(7)
        lons = rng.uniform(-101.9, -101.9 + 37 * 0.0025, 200)
        lats = rng.uniform(35.25 - 23 * 0.0025, 35.25, 200)
        many = t.values_at(lons, lats)
        one = [t.value_at(float(x), float(y)) for x, y in zip(lons, lats, strict=True)]
        assert [None if np.isnan(v) else pytest.approx(v) for v in many] == one


def test_scale_from_gdal_metadata_is_applied() -> None:
    with GeoTiff(FIXTURES / "gdal_cog_lzw_pred2_int16.tif") as t:
        assert t.scale == pytest.approx(0.01)
        assert t.read_raw(2, 10) == 530.0


def test_packbits_and_lzw_codecs() -> None:
    # The TIFF 6.0 specification's PackBits example (section 9).
    packed = bytes([0xFE, 0xAA, 0x02, 0x80, 0x00, 0x2A, 0xFD, 0xAA, 0x03, 0x80, 0x00, 0x2A, 0x22, 0xF7, 0xAA])
    assert packbits_decode(packed) == bytes.fromhex("AAAAAA80002AAAAAAAAA80002A22AAAAAAAAAAAAAAAAAAAA")
    # LZW: ClearCode, 'A', 'B', code 258 ('AB'), EOI with 9-bit codes.
    codes = [256, 65, 66, 258, 257]
    bits = "".join(format(c, "09b") for c in codes)
    bits += "0" * (-len(bits) % 8)
    stream = int(bits, 2).to_bytes(len(bits) // 8, "big")
    assert lzw_decode(stream) == b"ABAB"


def test_refuses_non_tiff_and_projected(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "x.tif"
    bad.write_bytes(b"not a tiff at all")
    with pytest.raises(GeoTiffError):
        GeoTiff(bad)
    # A minimal TIFF whose GeoKeys declare a projected model (GTModelTypeGeoKey = 1).
    entries = [
        (256, 3, 1, 1),
        (257, 3, 1, 1),
        (258, 3, 1, 8),
        (273, 4, 1, 200),
        (279, 4, 1, 1),
        (33550, 12, 3, 0),
        (33922, 12, 6, 0),
        (34735, 3, 8, 0),
    ]
    head = b"II" + struct.pack("<HI", 42, 8)
    ifd = struct.pack("<H", len(entries))
    blobs: list[bytes] = []
    data_at = 8 + 2 + 12 * len(entries) + 4
    for tag, typ, count, value in entries:
        if tag in (33550, 33922, 34735):
            blob = {
                33550: struct.pack("<3d", 1.0, 1.0, 0.0),
                33922: struct.pack("<6d", 0, 0, 0, 0.0, 1.0, 0),
                34735: struct.pack("<8H", 1, 1, 0, 1, 1024, 0, 1, 1),
            }[tag]
            ifd += struct.pack("<HHII", tag, typ, count, data_at + sum(len(b) for b in blobs))
            blobs.append(blob)
        else:
            ifd += struct.pack("<HHII", tag, typ, count, value)
    ifd += struct.pack("<I", 0)
    proj = tmp_path / "projected.tif"
    proj.write_bytes(head + ifd + b"".join(blobs) + b"\0" * 64)
    with pytest.raises(GeoTiffError, match="not a geographic"):
        GeoTiff(proj)
