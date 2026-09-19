"""Dialect-aware column types.

`docs/21-data-model.md` §1 fixes the canonical Postgres types (`uuid`, `jsonb`, `text[]`,
`geography(Point,4326)`). The Alembic migration under `services/db/migrations/versions` writes
those types literally against Postgres 16 + PostGIS — it is the canonical, executable schema
(ADR 0003). This module exists only so the *same ORM models* also create working tables on
SQLite, which is this sprint's test target because Postgres is not installed in this environment
(see services/README.md for the caveat). Every type here compiles to the real Postgres type on a
`postgresql` dialect and to the closest SQLite equivalent otherwise; application code never
imports `postgresql.*` types directly outside this file and the migration.
"""

from __future__ import annotations

import json
import os
import time
import uuid as _uuid
from typing import Any

from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import Dialect
from sqlalchemy.types import JSON, String, TypeDecorator, TypeEngine


def uuid7() -> _uuid.UUID:
    """Time-ordered UUID (draft RFC 9562 "UUIDv7"), per docs/21 §1 ("uuid v7 for domain entities").

    Minimal, dependency-free implementation: a 48-bit millisecond Unix timestamp followed by
    version/variant bits and 74 random bits. Good enough for time-ordered primary keys; it is not
    a substitute for a vetted library if strict spec conformance is ever required.
    """
    ms = int(time.time() * 1000).to_bytes(6, "big")
    rand = os.urandom(10)
    b = bytearray(ms + rand)
    b[6] = (b[6] & 0x0F) | 0x70  # version 7
    b[8] = (b[8] & 0x3F) | 0x80  # variant 10 (RFC 4122)
    return _uuid.UUID(bytes=bytes(b))


class GUID(TypeDecorator[_uuid.UUID]):
    """`uuid` on Postgres; `CHAR(36)` text on SQLite."""

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        as_uuid = value if isinstance(value, _uuid.UUID) else _uuid.UUID(str(value))
        return as_uuid if dialect.name == "postgresql" else str(as_uuid)

    def process_result_value(self, value: Any, dialect: Dialect) -> _uuid.UUID | None:
        if value is None:
            return None
        return value if isinstance(value, _uuid.UUID) else _uuid.UUID(str(value))


class JSONVariant(TypeDecorator[Any]):
    """`jsonb` on Postgres (docs/21 §1: "raw payloads: jsonb"); `JSON` text on SQLite."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_JSONB())
        return dialect.type_descriptor(JSON())


class TextArray(TypeDecorator[Any]):
    """`text[]` on Postgres; a JSON-encoded list stored as `TEXT` on SQLite."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_ARRAY(Text()))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return value
        return json.dumps(list(value) if value is not None else [])

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return value if value is not None else []
        if value is None:
            return []
        result: list[Any] = json.loads(value)
        return result


class GeographyLine(TypeDecorator[Any]):
    """`geography(MultiLineString,4326)` on Postgres (docs/21 §3.22 `asset.geom_line`, migration
    0013); WKT text on SQLite. ADR 0008 accepts the SQLite side as a known limitation ("the test
    target cannot exercise spatial operations on lines... lines are drawn from tiles, not
    queried") — `geom_line` exists on `asset` for pages and joins, never for map rendering.

    **Python-side contract (2026-09-19, midstream lane): a WKT string on both dialects.** A
    dissolved pipeline (`pipeline/context/eia_atlas.py`, one row per operator and pipeline type)
    is a `MULTILINESTRING`, which the original `geography(LineString)` column could not hold, so
    the column is widened and the value is carried as WKT rather than the earlier list of
    `(lon, lat)` pairs (still accepted on bind for compatibility — a list of pairs binds as a
    one-part multilinestring, a list of lists as one part each; a `LINESTRING(...)` string is
    promoted to a one-part `MULTILINESTRING((...))` because PostGIS rejects a LineString in a
    MultiLineString-typed column). Reads return the WKT string: on SQLite the stored text, on
    Postgres the EWKB PostGIS returns, decoded here (`_ewkb_to_wkt`, LineString and
    MultiLineString only, 2-D) so callers never see a dialect-specific object.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            from geoalchemy2 import Geography

            return dialect.type_descriptor(Geography(geometry_type="MULTILINESTRING", srid=4326))
        return dialect.type_descriptor(Text())

    def column_expression(self, column: Any) -> Any:
        """geoalchemy2's `Geography.column_expression` wraps a read in `ST_AsBinary(...)` typed
        as the bare `Geography`, which would route the result past `process_result_value`
        (measured 2026-09-19: the ORM handed back hex EWKB). Re-typing that wrapper as this
        decorator keeps the WKT contract; on SQLite the column is read as-is."""
        inner = self.impl_instance
        if getattr(inner, "column_expression", None) is None or not inner._has_column_expression:
            return column
        from sqlalchemy import func

        return func.ST_AsBinary(column, type_=self)

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        wkt = _line_value_to_wkt(value)
        if dialect.name == "postgresql":
            return f"SRID=4326;{wkt}"
        return wkt

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return _ewkb_to_wkt(value)
        return str(value)


def _fmt_coord(v: float) -> str:
    s = f"{float(v):.7f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _line_value_to_wkt(value: Any) -> str:
    """Normalise every accepted `geom_line` bind shape to a `MULTILINESTRING(...)` WKT string."""
    if isinstance(value, str):
        text = value.strip()
        upper = text.upper()
        if upper.startswith("SRID="):
            text = text.split(";", 1)[1].strip()
            upper = text.upper()
        if upper.startswith("MULTILINESTRING"):
            return text
        if upper.startswith("LINESTRING"):
            inner = text[text.index("(") :]
            return f"MULTILINESTRING({inner})"
        raise ValueError(f"geom_line WKT must be a LINESTRING or MULTILINESTRING, got {text[:40]!r}")
    seq = list(value)
    if not seq:
        return "MULTILINESTRING EMPTY"
    first = seq[0]
    parts: list[list[Any]]
    if isinstance(first, (list, tuple)) and first and isinstance(first[0], (list, tuple)):
        parts = [list(p) for p in seq]  # list of lines
    else:
        parts = [seq]  # one line, list of (lon, lat)
    rendered = []
    for part in parts:
        rendered.append("(" + ", ".join(f"{_fmt_coord(lon)} {_fmt_coord(lat)}" for lon, lat in part) + ")")
    return "MULTILINESTRING(" + ", ".join(rendered) + ")"


def _ewkb_to_wkt(value: Any) -> str:
    """Decode the (E)WKB PostGIS hands back for a geography column into WKT. Accepts a
    `geoalchemy2.WKBElement`, a hex string or raw bytes; handles 2-D LineString (2) and
    MultiLineString (5), any byte order, with or without the EWKB SRID flag."""
    import struct

    data = _ewkb_bytes(value)
    pos = 0

    def read_geom() -> tuple[int, list[list[tuple[float, float]]]]:
        nonlocal pos
        order = "<" if data[pos] == 1 else ">"
        pos += 1
        (gtype,) = struct.unpack_from(f"{order}I", data, pos)
        pos += 4
        if gtype & 0x20000000:  # EWKB SRID present
            pos += 4
        base = gtype & 0xFF
        if base == 2:  # LineString
            (n,) = struct.unpack_from(f"{order}I", data, pos)
            pos += 4
            coords = list(struct.unpack_from(f"{order}{2 * n}d", data, pos))
            pos += 16 * n
            return base, [[(coords[2 * i], coords[2 * i + 1]) for i in range(n)]]
        if base == 5:  # MultiLineString
            (n,) = struct.unpack_from(f"{order}I", data, pos)
            pos += 4
            lines: list[list[tuple[float, float]]] = []
            for _ in range(n):
                _sub, sub_lines = read_geom()
                lines.extend(sub_lines)
            return base, lines
        raise ValueError(f"unsupported geometry type {gtype:#x} in geom_line")

    _kind, lines = read_geom()
    if not lines:
        return "MULTILINESTRING EMPTY"
    return (
        "MULTILINESTRING("
        + ", ".join("(" + ", ".join(f"{_fmt_coord(x)} {_fmt_coord(y)}" for x, y in ln) + ")" for ln in lines)
        + ")"
    )


class GeographyPoint(TypeDecorator[Any]):
    """`geography(Point,4326)` on Postgres (docs/21 §1, §3.7 `location.geom`).

    On SQLite (the day-to-day test target, see services/README.md) the point is stored as JSON
    text `{"lon": ..., "lat": ...}`. Bind and read values as a plain `(lon, lat)` tuple on **both**
    dialects: on Postgres the tuple binds as EWKT `SRID=4326;POINT(lon lat)` (a `POINT(...)` WKT
    string is accepted too) and the EWKB PostGIS returns is decoded back to the tuple here
    (`_ewkb_to_point`), with reads routed through this decorator by `column_expression` for the
    same reason `GeographyLine` documents. Before 2026-09-19 the Postgres path handed the raw
    tuple to PostGIS, which rejected it ("parse error - invalid geometry"); the canonical
    migration's GiST index and bounding-box queries are still exercised against Postgres only.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            from geoalchemy2 import Geography

            return dialect.type_descriptor(Geography(geometry_type="POINT", srid=4326))
        return dialect.type_descriptor(Text())

    def column_expression(self, column: Any) -> Any:
        inner = self.impl_instance
        if getattr(inner, "column_expression", None) is None or not inner._has_column_expression:
            return column
        from sqlalchemy import func

        return func.ST_AsBinary(column, type_=self)

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        lon, lat = _point_value(value)
        if dialect.name == "postgresql":
            return f"SRID=4326;POINT({_fmt_coord(lon)} {_fmt_coord(lat)})"
        return json.dumps({"lon": lon, "lat": lat})

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return _ewkb_to_point(value)
        d = json.loads(value)
        return (d["lon"], d["lat"])


def _point_value(value: Any) -> tuple[float, float]:
    """`(lon, lat)` from a tuple/list or a `POINT(lon lat)` / `SRID=4326;POINT(...)` string."""
    if isinstance(value, str):
        text = value.strip()
        if text.upper().startswith("SRID="):
            text = text.split(";", 1)[1].strip()
        if not text.upper().startswith("POINT"):
            raise ValueError(f"geom WKT must be a POINT, got {text[:40]!r}")
        inner = text[text.index("(") + 1 : text.rindex(")")]
        lon_s, lat_s = inner.split()
        return float(lon_s), float(lat_s)
    lon, lat = value
    return float(lon), float(lat)


def _ewkb_bytes(value: Any) -> bytes:
    data = getattr(value, "data", value)
    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, str):
        data = bytes.fromhex(data)
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"cannot decode geography value of type {type(value).__name__}")
    return bytes(data)


def _ewkb_to_point(value: Any) -> tuple[float, float]:
    """(E)WKB Point (type 1, any byte order, optional SRID flag) -> `(lon, lat)`."""
    import struct

    data = _ewkb_bytes(value)
    order = "<" if data[0] == 1 else ">"
    (gtype,) = struct.unpack_from(f"{order}I", data, 1)
    pos = 5 + (4 if gtype & 0x20000000 else 0)
    if gtype & 0xFF != 1:
        raise ValueError(f"unsupported geometry type {gtype:#x} in a point column")
    x, y = struct.unpack_from(f"{order}dd", data, pos)
    return (x, y)
