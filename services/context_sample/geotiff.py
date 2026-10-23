"""A dependency-light GeoTIFF reader: the prospect rasters at a point, without GDAL.

The API containers carry numpy and pyarrow but no GDAL, rasterio or shapely (ADR 0008 kept the stack
pure-Python), so the sampler reads the cloud-optimised GeoTIFFs the build writes with this module. It
also reads the source rasters the build ingests (NSRDB's uncompressed float64 strips; a Global Wind
Atlas GeoTIFF), so one reader serves both sides.

Supported, and refused loudly otherwise (`GeoTiffError`):

- classic TIFF and BigTIFF, either byte order;
- one band (`SamplesPerPixel` 1), chunky or planar (identical for one band);
- strips or tiles;
- compression none (1), LZW (5), Deflate (8 and the legacy 32946), PackBits (32773);
- predictor none (1), horizontal differencing (2), floating point (3);
- unsigned, signed and floating sample formats of 8/16/32/64 bits;
- georeferencing by `ModelPixelScale` + `ModelTiepoint` (no rotation) in a geographic CRS
  (`GTModelTypeGeoKey` 2), with `PixelIsArea` or `PixelIsPoint`;
- `GDAL_NODATA` (tag 42113) and the `scale`/`offset` roles in `GDAL_METADATA` (tag 42112), plus every
  other `<Item name=...>` as plain metadata (the build stores provenance there).

Only the first IFD (full resolution) is read; overviews written after it are ignored.
"""

from __future__ import annotations

import math
import pathlib
import re
import struct
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from html import unescape
from typing import Any, BinaryIO

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]

_TYPE_SIZES: dict[int, tuple[str, int]] = {
    1: ("B", 1),
    2: ("s", 1),
    3: ("H", 2),
    4: ("I", 4),
    5: ("II", 8),
    6: ("b", 1),
    7: ("B", 1),
    8: ("h", 2),
    9: ("i", 4),
    10: ("ii", 8),
    11: ("f", 4),
    12: ("d", 8),
    16: ("Q", 8),
    17: ("q", 8),
    18: ("Q", 8),
}

TAG_WIDTH = 256
TAG_HEIGHT = 257
TAG_BITS = 258
TAG_COMPRESSION = 259
TAG_STRIP_OFFSETS = 273
TAG_SAMPLES = 277
TAG_ROWS_PER_STRIP = 278
TAG_STRIP_BYTES = 279
TAG_PREDICTOR = 317
TAG_TILE_WIDTH = 322
TAG_TILE_LENGTH = 323
TAG_TILE_OFFSETS = 324
TAG_TILE_BYTES = 325
TAG_SAMPLE_FORMAT = 339
TAG_PIXEL_SCALE = 33550
TAG_TIEPOINT = 33922
TAG_TRANSFORMATION = 34264
TAG_GEOKEYS = 34735
TAG_GDAL_METADATA = 42112
TAG_GDAL_NODATA = 42113

GEOKEY_MODEL_TYPE = 1024
GEOKEY_RASTER_TYPE = 1025
GEOKEY_GEOGRAPHIC_TYPE = 2048
MODEL_TYPE_GEOGRAPHIC = 2
RASTER_PIXEL_IS_POINT = 2

#: Decoded blocks kept per open file. A 256x256 uint16 tile is 128 KB, so 256 tiles is 32 MB at most.
BLOCK_CACHE_SIZE = 256


class GeoTiffError(ValueError):
    """The file is not a GeoTIFF this reader supports (the message says which part)."""


@dataclass(frozen=True)
class GeoTransform:
    """North-up geographic grid: pixel (row, col) covers lon [west + col*dx, west + (col+1)*dx)."""

    west: float
    north: float
    dx: float
    dy: float

    def cell(self, lon: float, lat: float) -> tuple[int, int]:
        return math.floor((self.north - lat) / self.dy), math.floor((lon - self.west) / self.dx)


def _dtype(bits: int, sample_format: int, byteorder: str) -> np.dtype[Any]:
    kind = {1: "u", 2: "i", 3: "f"}.get(sample_format)
    if kind is None or bits not in (8, 16, 32, 64) or (kind == "f" and bits < 32):
        raise GeoTiffError(f"unsupported sample format {sample_format} with {bits} bits")
    return np.dtype(f"{byteorder}{kind}{bits // 8}")


def lzw_decode(data: bytes) -> bytes:
    """TIFF LZW (MSB-first codes, 'early change', ClearCode 256, EOI 257)."""
    out = bytearray()
    table: list[bytes] = [bytes([i]) for i in range(256)] + [b"", b""]
    bit_pos = 0
    n_bits = 9
    total_bits = len(data) * 8
    prev: bytes | None = None
    while bit_pos + n_bits <= total_bits:
        byte_index = bit_pos >> 3
        chunk = int.from_bytes(data[byte_index : byte_index + 4].ljust(4, b"\0"), "big")
        code = (chunk >> (32 - (bit_pos & 7) - n_bits)) & ((1 << n_bits) - 1)
        bit_pos += n_bits
        if code == 256:
            table = table[:258]
            n_bits = 9
            prev = None
            continue
        if code == 257:
            break
        if prev is None:
            entry = table[code]
            out += entry
            prev = entry
            continue
        if code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        elif code == len(table):
            entry = prev + prev[:1]
            table.append(entry)
        else:
            raise GeoTiffError(f"corrupt LZW stream (code {code} beyond table {len(table)})")
        out += entry
        prev = entry
        # Early change: the code width grows one code before the table fills.
        if len(table) + 1 >= (1 << n_bits) and n_bits < 12:
            n_bits += 1
    return bytes(out)


def packbits_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        n = data[i]
        i += 1
        if n < 128:
            out += data[i : i + n + 1]
            i += n + 1
        elif n > 128:
            out += bytes([data[i]]) * (257 - n)
            i += 1
    return bytes(out)


def _xml_items(xml: str) -> tuple[dict[str, str], float, float]:
    """`GDAL_METADATA` -> (plain items, scale, offset)."""
    items: dict[str, str] = {}
    scale, offset = 1.0, 0.0
    for m in re.finditer(r"<Item\s+([^>]*)>(.*?)</Item>", xml, re.S):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        # GDAL escapes item values twice (CPLEscapeString, then the XML serialiser).
        value = unescape(unescape(m.group(2)))
        role = attrs.get("role")
        if role == "scale":
            scale = float(value)
        elif role == "offset":
            offset = float(value)
        elif "name" in attrs:
            items[attrs["name"]] = value
    return items, scale, offset


class GeoTiff:
    """One open GeoTIFF. Thread-safe; blocks are decoded on demand and cached (LRU)."""

    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        self._fh: BinaryIO = self.path.open("rb")
        self._lock = threading.Lock()
        self._cache: OrderedDict[int, npt.NDArray[Any]] = OrderedDict()
        try:
            self._read_header()
        except (struct.error, IndexError) as exc:
            self._fh.close()
            raise GeoTiffError(f"{self.path.name}: truncated or not a TIFF ({exc})") from exc
        except GeoTiffError:
            self._fh.close()
            raise

    # ------------------------------------------------------------------ header
    def _read_header(self) -> None:
        head = self._fh.read(16)
        if head[:2] == b"II":
            self._bo = "<"
        elif head[:2] == b"MM":
            self._bo = ">"
        else:
            raise GeoTiffError(f"{self.path.name}: not a TIFF (byte-order mark {head[:2]!r})")
        magic = struct.unpack(self._bo + "H", head[2:4])[0]
        if magic == 42:
            self._big = False
            ifd = struct.unpack(self._bo + "I", head[4:8])[0]
        elif magic == 43:
            self._big = True
            ifd = struct.unpack(self._bo + "Q", head[8:16])[0]
        else:
            raise GeoTiffError(f"{self.path.name}: not a TIFF (magic {magic})")
        tags = self._read_ifd(ifd)

        def one(tag: int, default: Any = None) -> Any:
            v = tags.get(tag)
            if v is None:
                return default
            return v[0] if isinstance(v, tuple) else v

        self.width = int(one(TAG_WIDTH))
        self.height = int(one(TAG_HEIGHT))
        if int(one(TAG_SAMPLES, 1)) != 1:
            raise GeoTiffError(f"{self.path.name}: more than one band")
        bits = int(one(TAG_BITS, 1))
        self.dtype = _dtype(bits, int(one(TAG_SAMPLE_FORMAT, 1)), self._bo)
        self.compression = int(one(TAG_COMPRESSION, 1))
        if self.compression not in (1, 5, 8, 32946, 32773):
            raise GeoTiffError(f"{self.path.name}: unsupported compression {self.compression}")
        self.predictor = int(one(TAG_PREDICTOR, 1))
        if self.predictor not in (1, 2, 3):
            raise GeoTiffError(f"{self.path.name}: unsupported predictor {self.predictor}")
        if TAG_TILE_WIDTH in tags:
            self.block_w = int(one(TAG_TILE_WIDTH))
            self.block_h = int(one(TAG_TILE_LENGTH))
            offsets, counts = tags[TAG_TILE_OFFSETS], tags[TAG_TILE_BYTES]
        else:
            self.block_w = self.width
            self.block_h = min(int(one(TAG_ROWS_PER_STRIP, self.height)), self.height)
            offsets, counts = tags[TAG_STRIP_OFFSETS], tags[TAG_STRIP_BYTES]
        self._offsets = tuple(int(x) for x in (offsets if isinstance(offsets, tuple) else (offsets,)))
        self._counts = tuple(int(x) for x in (counts if isinstance(counts, tuple) else (counts,)))
        self.blocks_across = -(-self.width // self.block_w)
        self.blocks_down = -(-self.height // self.block_h)
        if len(self._offsets) < self.blocks_across * self.blocks_down:
            raise GeoTiffError(f"{self.path.name}: fewer block offsets than blocks")

        nodata_raw = tags.get(TAG_GDAL_NODATA)
        self.nodata: float | None = None
        if isinstance(nodata_raw, str) and nodata_raw.strip():
            try:
                self.nodata = float(nodata_raw.strip())
            except ValueError:
                self.nodata = None
        xml = tags.get(TAG_GDAL_METADATA)
        self.metadata, self.scale, self.offset = _xml_items(xml) if isinstance(xml, str) else ({}, 1.0, 0.0)
        self.transform = self._georef(tags)

    def _read_ifd(self, offset: int) -> dict[int, Any]:
        fh, bo = self._fh, self._bo
        fh.seek(offset)
        if self._big:
            (n,) = struct.unpack(bo + "Q", fh.read(8))
            size, count_fmt, ptr_fmt, inline = 20, "Q", "Q", 8
        else:
            (n,) = struct.unpack(bo + "H", fh.read(2))
            size, count_fmt, ptr_fmt, inline = 12, "I", "I", 4
        raw = fh.read(n * size)
        tags: dict[int, Any] = {}
        for i in range(n):
            entry = raw[i * size : (i + 1) * size]
            tag, typ = struct.unpack(bo + "HH", entry[:4])
            csize = 8 if self._big else 4
            (count,) = struct.unpack(bo + count_fmt, entry[4 : 4 + csize])
            fmt, width = _TYPE_SIZES.get(typ, ("B", 1))
            nbytes = count * width
            field = entry[4 + csize :]
            if nbytes <= inline:
                data = field[:nbytes]
            else:
                (ptr,) = struct.unpack(bo + ptr_fmt, field[:inline])
                here = fh.tell()
                fh.seek(ptr)
                data = fh.read(nbytes)
                fh.seek(here)
            if typ == 2:
                tags[tag] = data.split(b"\0", 1)[0].decode("utf-8", "replace")
                continue
            per = len(fmt)
            values = struct.unpack(bo + fmt[0] * (count * per), data)
            if typ in (5, 10):
                values = tuple(
                    values[j] / values[j + 1] if values[j + 1] else 0.0 for j in range(0, len(values), 2)
                )
            tags[tag] = values
        return tags

    def _georef(self, tags: dict[int, Any]) -> GeoTransform:
        keys = tags.get(TAG_GEOKEYS)
        geokeys: dict[int, int] = {}
        if isinstance(keys, tuple) and len(keys) >= 4:
            for j in range(4, 4 + 4 * int(keys[3]), 4):
                key_id, location, _count, value = keys[j : j + 4]
                if location == 0:
                    geokeys[int(key_id)] = int(value)
        model = geokeys.get(GEOKEY_MODEL_TYPE)
        if model is not None and model != MODEL_TYPE_GEOGRAPHIC:
            raise GeoTiffError(f"{self.path.name}: not a geographic (lon/lat) raster (model type {model})")
        if TAG_TRANSFORMATION in tags and TAG_PIXEL_SCALE not in tags:
            raise GeoTiffError(f"{self.path.name}: rotated/affine ModelTransformation is not supported")
        scale, tie = tags.get(TAG_PIXEL_SCALE), tags.get(TAG_TIEPOINT)
        if not (isinstance(scale, tuple) and isinstance(tie, tuple) and len(tie) >= 6):
            raise GeoTiffError(f"{self.path.name}: no ModelPixelScale/ModelTiepoint georeferencing")
        dx, dy = float(scale[0]), float(scale[1])
        tie_col, tie_row, x, y = float(tie[0]), float(tie[1]), float(tie[3]), float(tie[4])
        west, north = x - tie_col * dx, y + tie_row * dy
        if geokeys.get(GEOKEY_RASTER_TYPE) == RASTER_PIXEL_IS_POINT:
            west, north = west - dx / 2, north + dy / 2
        self.epsg = geokeys.get(GEOKEY_GEOGRAPHIC_TYPE)
        return GeoTransform(west=west, north=north, dx=dx, dy=dy)

    # ------------------------------------------------------------------ blocks
    def _decode(self, raw: bytes) -> bytes:
        if self.compression == 1:
            return raw
        if self.compression in (8, 32946):
            return zlib.decompress(raw)
        if self.compression == 5:
            return lzw_decode(raw)
        return packbits_decode(raw)

    def block(self, index: int) -> npt.NDArray[Any]:
        """Block `index` (row-major) as a (block_h, block_w) array of the stored type, predictor undone."""
        with self._lock:
            cached = self._cache.get(index)
            if cached is not None:
                self._cache.move_to_end(index)
                return cached
            offset, count = self._offsets[index], self._counts[index]
            if count == 0:
                arr = np.full((self.block_h, self.block_w), self.nodata if self.nodata is not None else 0)
                arr = arr.astype(self.dtype.newbyteorder("="))
            else:
                self._fh.seek(offset)
                raw = self._decode(self._fh.read(count))
                arr = self._unpredict(raw)
            self._cache[index] = arr
            if len(self._cache) > BLOCK_CACHE_SIZE:
                self._cache.popitem(last=False)
            return arr

    def _unpredict(self, raw: bytes) -> npt.NDArray[Any]:
        rows = len(raw) // (self.block_w * self.dtype.itemsize)
        if self.predictor == 3:
            # Floating-point predictor (Adobe TIFF tech note 3): each row is stored as byte planes, most
            # significant byte first whatever the file's byte order, then byte-wise differenced.
            nbytes = self.dtype.itemsize
            b = np.frombuffer(raw, dtype=np.uint8)[: rows * self.block_w * nbytes]
            b = np.cumsum(b.reshape(rows, self.block_w * nbytes), axis=1, dtype=np.uint8)
            samples = np.ascontiguousarray(b.reshape(rows, nbytes, self.block_w).transpose(0, 2, 1))
            arr = samples.view(self.dtype.newbyteorder(">")).reshape(rows, self.block_w)
            arr = arr.astype(self.dtype.newbyteorder("="))
        else:
            arr = np.frombuffer(raw, dtype=self.dtype)[: rows * self.block_w].reshape(rows, self.block_w)
            arr = arr.astype(self.dtype.newbyteorder("="))
            if self.predictor == 2:
                arr = np.cumsum(arr, axis=1, dtype=arr.dtype)
        if rows < self.block_h:
            fill = self.nodata if self.nodata is not None else 0
            pad = np.full((self.block_h - rows, self.block_w), fill, dtype=arr.dtype)
            arr = np.vstack([arr, pad])
        return arr

    # ------------------------------------------------------------------ values
    def read_raw(self, row: int, col: int) -> float | None:
        """The stored value at pixel (row, col), or None outside the grid."""
        if not (0 <= row < self.height and 0 <= col < self.width):
            return None
        index = (row // self.block_h) * self.blocks_across + (col // self.block_w)
        return float(self.block(index)[row % self.block_h, col % self.block_w])

    def values_at(self, lons: npt.ArrayLike, lats: npt.ArrayLike) -> FloatArray:
        """Scaled values at each (lon, lat); NaN outside the grid or where the cell is nodata."""
        lon = np.asarray(lons, dtype=np.float64).ravel()
        lat = np.asarray(lats, dtype=np.float64).ravel()
        t = self.transform
        rows = np.floor((t.north - lat) / t.dy).astype(np.int64)
        cols = np.floor((lon - t.west) / t.dx).astype(np.int64)
        out = np.full(lon.shape, np.nan)
        ok = (rows >= 0) & (rows < self.height) & (cols >= 0) & (cols < self.width)
        if not ok.any():
            return out
        block_ids = (rows // self.block_h) * self.blocks_across + (cols // self.block_w)
        for bid in np.unique(block_ids[ok]):
            sel = ok & (block_ids == bid)
            data = self.block(int(bid))
            vals = data[rows[sel] % self.block_h, cols[sel] % self.block_w].astype(np.float64)
            if self.nodata is not None:
                nod = np.isclose(vals, self.nodata) if math.isfinite(self.nodata) else ~np.isfinite(vals)
                vals[nod] = np.nan
            out[sel] = vals * self.scale + self.offset
        return out

    def value_at(self, lon: float, lat: float) -> float | None:
        v = float(self.values_at([lon], [lat])[0])
        return None if math.isnan(v) else v

    def read_window(self, row0: int, col0: int, rows: int, cols: int) -> npt.NDArray[Any]:
        """A window of stored values (no scale, nodata kept), clipped to the grid."""
        row1, col1 = min(self.height, row0 + rows), min(self.width, col0 + cols)
        row0, col0 = max(0, row0), max(0, col0)
        out = np.empty((row1 - row0, col1 - col0), dtype=self.dtype.newbyteorder("="))
        for br in range(row0 // self.block_h, (row1 - 1) // self.block_h + 1):
            for bc in range(col0 // self.block_w, (col1 - 1) // self.block_w + 1):
                data = self.block(br * self.blocks_across + bc)
                r0, c0 = br * self.block_h, bc * self.block_w
                rs, re_ = max(row0, r0), min(row1, r0 + self.block_h)
                cs, ce = max(col0, c0), min(col1, c0 + self.block_w)
                out[rs - row0 : re_ - row0, cs - col0 : ce - col0] = data[
                    rs - r0 : re_ - r0, cs - c0 : ce - c0
                ]
        return out

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> GeoTiff:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
