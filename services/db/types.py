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
    """`geography(LineString,4326)` on Postgres (docs/21 §3.22 `asset.geom_line`); WKT text on
    SQLite. ADR 0008 accepts this as a known limitation ("the test target cannot exercise spatial
    operations on lines... lines are drawn from tiles, not queried") — `geom_line` exists on
    `asset` for pages and joins, never for map rendering, so a lossless-enough SQLite
    representation (a plain `LINESTRING(lon lat, ...)` WKT string, parsed back into a list of
    `(lon, lat)` tuples) is sufficient for this sprint's tests. Bind and read as a list of
    `(lon, lat)` pairs, mirroring `GeographyPoint`'s `(lon, lat)` tuple convention.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            from geoalchemy2 import Geography

            return dialect.type_descriptor(Geography(geometry_type="LINESTRING", srid=4326))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None or dialect.name == "postgresql":
            return value
        points = ", ".join(f"{lon} {lat}" for lon, lat in value)
        return f"LINESTRING({points})"

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None or dialect.name == "postgresql":
            return value
        inner = value.strip()
        if inner.upper().startswith("LINESTRING("):
            inner = inner[len("LINESTRING(") : -1]
        pairs: list[tuple[float, float]] = []
        for pair in inner.split(","):
            lon_s, lat_s = pair.strip().split(" ")
            pairs.append((float(lon_s), float(lat_s)))
        return pairs


class GeographyPoint(TypeDecorator[Any]):
    """`geography(Point,4326)` on Postgres (docs/21 §1, §3.7 `location.geom`).

    On SQLite (this sprint's test target — Postgres/PostGIS is not installed here, see
    services/README.md) the point is stored as JSON text `{"lon": ..., "lat": ...}`. Bind and
    read values as a plain `(lon, lat)` tuple; the canonical Postgres migration uses a real
    `geography` column with a GiST index (docs/21 §5.2), which this fallback cannot provide —
    map bounding-box queries are exercised against Postgres only, not in this sprint's SQLite
    test target.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            from geoalchemy2 import Geography

            return dialect.type_descriptor(Geography(geometry_type="POINT", srid=4326))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None or dialect.name == "postgresql":
            return value
        lon, lat = value
        return json.dumps({"lon": lon, "lat": lat})

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None or dialect.name == "postgresql":
            return value
        d = json.loads(value)
        return (d["lon"], d["lat"])
