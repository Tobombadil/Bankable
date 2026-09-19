"""Minimal ESRI shapefile reader (``.shp`` + ``.dbf`` inside a zip) -> GeoJSON-shaped features.

Written for `pipeline/context/eia_atlas.py`: EIA's own downloadable copies of the Atlas natural
gas layers are shapefile zips on ``www.eia.gov/maps/map_data/``, and this environment has no
``pyshp``/``fiona``/``shapely`` (``requirements.txt`` is another lane's file). The format is small
and stable (ESRI Shapefile Technical Description, 1998), so a reader for exactly the shape types
those layers use — ``Point`` (1), ``PolyLine`` (3) and their ``Z``/``M`` variants (11, 13, 21, 23),
plus ``Null`` (0) — is ~100 lines and fully testable, which beats a dependency this sprint
cannot add.

Every feature comes back as ``{"type": "Feature", "properties": {...}, "geometry": {...}}`` with
``geometry`` in GeoJSON form (``Point`` / ``MultiLineString``), so the normalisers in
`eia_atlas.py` see the same shape whether the bytes came from the ArcGIS feature service
(GeoJSON) or from the zip. ``Z``/``M`` values are dropped: the Atlas layers are 2-D and the
downstream columns are 2-D.

Not handled, by design: polygons (5, 15, 25), multipoint (8, 18, 28), multipatch (31) — raise
``ShapefileError`` rather than guess.
"""

from __future__ import annotations

import datetime as dt
import io
import struct
import zipfile
from typing import Any

SHAPE_NULL = 0
SHAPE_POINT = 1
SHAPE_POLYLINE = 3
SHAPE_POINT_Z = 11
SHAPE_POLYLINE_Z = 13
SHAPE_POINT_M = 21
SHAPE_POLYLINE_M = 23

_POINT_TYPES = frozenset({SHAPE_POINT, SHAPE_POINT_Z, SHAPE_POINT_M})
_POLYLINE_TYPES = frozenset({SHAPE_POLYLINE, SHAPE_POLYLINE_Z, SHAPE_POLYLINE_M})


class ShapefileError(ValueError):
    """The bytes are not a shapefile this reader handles (missing member, unsupported type)."""


def _member(zf: zipfile.ZipFile, suffix: str) -> str | None:
    names = [n for n in zf.namelist() if n.lower().endswith(suffix) and not n.startswith("__MACOSX")]
    return sorted(names)[0] if names else None


def read_dbf(content: bytes, *, encoding: str = "latin-1") -> list[dict[str, Any]]:
    """dBASE III/IV attribute table -> one dict per (non-deleted) record.

    ``C`` -> stripped ``str`` (``""`` kept as ``""``), ``N``/``F`` -> ``int``/``float`` or ``None``
    when blank or unparsable, ``D`` -> ``datetime.date`` or ``None``, ``L`` -> ``bool`` or ``None``.
    """
    if len(content) < 32:
        raise ShapefileError("dbf too short")
    n_records = struct.unpack("<I", content[4:8])[0]
    header_len = struct.unpack("<H", content[8:10])[0]
    record_len = struct.unpack("<H", content[10:12])[0]

    fields: list[tuple[str, str, int]] = []
    pos = 32
    while pos < header_len and content[pos] != 0x0D:
        name = content[pos : pos + 11].split(b"\x00")[0].decode("ascii", "replace")
        ftype = chr(content[pos + 11])
        length = content[pos + 16]
        fields.append((name, ftype, length))
        pos += 32

    rows: list[dict[str, Any]] = []
    for i in range(n_records):
        start = header_len + i * record_len
        rec = content[start : start + record_len]
        if len(rec) < record_len:
            break  # truncated trailer
        if rec[0:1] == b"*":
            continue  # deleted
        row: dict[str, Any] = {}
        p = 1
        for name, ftype, length in fields:
            raw = rec[p : p + length]
            p += length
            row[name] = _decode_field(raw, ftype, encoding)
        rows.append(row)
    return rows


def _decode_field(raw: bytes, ftype: str, encoding: str) -> Any:
    text = raw.decode(encoding, "replace").strip("\x00 ")
    if ftype in ("N", "F"):
        if not text or text == "*" * len(text):
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None
    if ftype == "D":
        if len(text) != 8 or not text.isdigit():
            return None
        try:
            return dt.date(int(text[:4]), int(text[4:6]), int(text[6:]))
        except ValueError:
            return None
    if ftype == "L":
        if text in ("T", "t", "Y", "y"):
            return True
        if text in ("F", "f", "N", "n"):
            return False
        return None
    return text


def read_shp(content: bytes) -> list[dict[str, Any] | None]:
    """``.shp`` main file -> one GeoJSON geometry (or ``None`` for a Null shape) per record, in
    file order (the order ``.dbf`` records are in)."""
    if len(content) < 100 or struct.unpack(">i", content[0:4])[0] != 9994:
        raise ShapefileError("not a .shp file (bad magic)")
    file_type = struct.unpack("<i", content[32:36])[0]
    if file_type not in _POINT_TYPES | _POLYLINE_TYPES | {SHAPE_NULL}:
        raise ShapefileError(f"unsupported shape type {file_type} (only Point and PolyLine are read)")

    geometries: list[dict[str, Any] | None] = []
    pos = 100
    total = len(content)
    while pos + 8 <= total:
        _recno, content_words = struct.unpack(">ii", content[pos : pos + 8])
        pos += 8
        rec = content[pos : pos + content_words * 2]
        pos += content_words * 2
        if len(rec) < 4:
            break
        shape_type = struct.unpack("<i", rec[0:4])[0]
        if shape_type == SHAPE_NULL:
            geometries.append(None)
        elif shape_type in _POINT_TYPES:
            x, y = struct.unpack("<dd", rec[4:20])
            geometries.append({"type": "Point", "coordinates": [x, y]})
        elif shape_type in _POLYLINE_TYPES:
            num_parts, num_points = struct.unpack("<ii", rec[36:44])
            parts = list(struct.unpack(f"<{num_parts}i", rec[44 : 44 + 4 * num_parts]))
            base = 44 + 4 * num_parts
            xy = struct.unpack(f"<{2 * num_points}d", rec[base : base + 16 * num_points])
            points = [[xy[2 * i], xy[2 * i + 1]] for i in range(num_points)]
            bounds = [*parts, num_points]
            lines = [points[bounds[i] : bounds[i + 1]] for i in range(num_parts)]
            geometries.append({"type": "MultiLineString", "coordinates": [ln for ln in lines if ln]})
        else:
            raise ShapefileError(f"unsupported shape type {shape_type} in record")
    return geometries


def read_zip(content: bytes) -> list[dict[str, Any]]:
    """A zipped shapefile (``.shp`` + ``.dbf``, optional ``.cpg`` for the attribute encoding) ->
    GeoJSON-shaped features. Raises ``ShapefileError`` when a member is missing or the record
    counts of the two files disagree (a corrupt or mismatched pair, never silently zipped)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as e:
        raise ShapefileError(f"not a zip: {e}") from e
    shp_name = _member(zf, ".shp")
    dbf_name = _member(zf, ".dbf")
    if shp_name is None or dbf_name is None:
        raise ShapefileError(f"zip lacks .shp/.dbf: {zf.namelist()}")
    cpg_name = _member(zf, ".cpg")
    encoding = "latin-1"
    if cpg_name is not None:
        declared = zf.read(cpg_name).decode("ascii", "replace").strip().lower()
        if declared in ("utf-8", "utf8", "65001"):
            encoding = "utf-8"

    geometries = read_shp(zf.read(shp_name))
    rows = read_dbf(zf.read(dbf_name), encoding=encoding)
    if len(geometries) != len(rows):
        raise ShapefileError(f".shp has {len(geometries)} records but .dbf has {len(rows)}")
    return [
        {"type": "Feature", "properties": props, "geometry": geom}
        for props, geom in zip(rows, geometries, strict=True)
    ]


def is_zip(content: bytes) -> bool:
    return content[:2] == b"PK"
