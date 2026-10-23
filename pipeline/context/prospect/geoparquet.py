"""Write polygon layers as GeoParquet 1.1 without GDAL or shapely (pyarrow only).

Layout, chosen for point and corridor queries without a spatial database:

- `geometry`: WKB `MultiPolygon` (every polygon is written as a one-part multipolygon, so a reader
  handles one type), coordinates rounded to 6 decimals (~0.1 m, below every source's accuracy);
- `bbox`: the feature's bounding box as a struct of `xmin`, `ymin`, `xmax`, `ymax` (float64), declared
  as the GeoParquet 1.1 bbox `covering` (`"xmin": ["bbox", "xmin"]`, ...), the form the specification
  defines (https://geoparquet.org/releases/v1.1.0/, "Bounding Box Columns"), so readers that implement
  it (GDAL 3.9+, DuckDB) can prune row groups with the struct fields' statistics;
- rows sorted by the Morton (Z-order) key of their bounding-box centre and written in small row groups
  (`ROW_GROUP_SIZE`), so a reader that filters on those statistics touches only the few groups near the
  query (`services/context_sample/vectors.py`);
- the layer's attribute columns, then the provenance columns every stored record carries
  (`source_id`, `source_url`, `retrieved_at`, `licence`, `vintage`; CLAUDE.md). A row may carry its own
  value for a provenance column (an energy-community area from NETL's second service names that
  service's URL); otherwise the layer's provenance fills it.

Input geometries are GeoJSON-style dicts (`Polygon` / `MultiPolygon`, lon/lat, EPSG:4326).
"""

from __future__ import annotations

import json
import pathlib
import struct
from collections.abc import Iterable, Sequence
from typing import Any

import pyarrow as pa  # type: ignore[import-untyped, unused-ignore]
import pyarrow.parquet as pq  # type: ignore[import-untyped, unused-ignore]

from pipeline.context.prospect.common import Provenance

ROW_GROUP_SIZE = 256
PRECISION = 6
#: The GeoParquet 1.1 bbox covering: one struct column with these four float64 fields.
BBOX_COLUMN = "bbox"
BBOX_FIELDS = ("xmin", "ymin", "xmax", "ymax")

Ring = list[tuple[float, float]]
PolygonCoords = list[Ring]


class GeometryError(ValueError):
    """A geometry the writer cannot store (not polygonal, or empty)."""


def polygons_of(geometry: dict[str, Any]) -> list[PolygonCoords]:
    """GeoJSON Polygon/MultiPolygon -> list of polygons (rings of (lon, lat)), rounded; empty rings and
    degenerate rings (fewer than 4 points) dropped."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Polygon":
        raw_polys = [coords]
    elif gtype == "MultiPolygon":
        raw_polys = list(coords)
    else:
        raise GeometryError(f"not polygonal: {gtype}")
    out: list[PolygonCoords] = []
    for poly in raw_polys:
        rings: PolygonCoords = []
        for ring in poly:
            pts = [(round(float(p[0]), PRECISION), round(float(p[1]), PRECISION)) for p in ring]
            if len(pts) >= 3 and pts[0] != pts[-1]:
                pts.append(pts[0])
            if len(pts) >= 4:
                rings.append(pts)
        if rings:
            out.append(rings)
    if not out:
        raise GeometryError("empty geometry")
    return out


def wkb_multipolygon(polygons: Sequence[PolygonCoords]) -> bytes:
    parts = [struct.pack("<BII", 1, 6, len(polygons))]
    for rings in polygons:
        parts.append(struct.pack("<BII", 1, 3, len(rings)))
        for ring in rings:
            parts.append(struct.pack("<I", len(ring)))
            parts.append(struct.pack(f"<{2 * len(ring)}d", *(c for pt in ring for c in pt)))
    return b"".join(parts)


def bounds(polygons: Sequence[PolygonCoords]) -> tuple[float, float, float, float]:
    xs = [p[0] for poly in polygons for p in poly[0]]
    ys = [p[1] for poly in polygons for p in poly[0]]
    return min(xs), min(ys), max(xs), max(ys)


def _morton(x: int, y: int) -> int:
    key = 0
    for bit in range(16):
        key |= ((x >> bit) & 1) << (2 * bit) | ((y >> bit) & 1) << (2 * bit + 1)
    return key


def write_geoparquet(
    path: str | pathlib.Path,
    rows: Sequence[dict[str, Any]],
    geometries: Sequence[dict[str, Any]],
    *,
    schema: Sequence[tuple[str, pa.DataType]],
    provenance: Provenance,
    row_group_size: int = ROW_GROUP_SIZE,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write `rows` (attribute dicts) with their `geometries`. Returns a summary (rows, bytes, bbox,
    dropped). A row whose geometry is not polygonal or is empty is dropped and counted."""
    if len(rows) != len(geometries):
        raise ValueError("rows and geometries differ in length")
    return write_geoparquet_features(
        path,
        zip(rows, geometries, strict=True),
        schema=schema,
        provenance=provenance,
        row_group_size=row_group_size,
        extra_metadata=extra_metadata,
    )


def write_geoparquet_features(
    path: str | pathlib.Path,
    features: Iterable[tuple[dict[str, Any], dict[str, Any]]],
    *,
    schema: Sequence[tuple[str, pa.DataType]],
    provenance: Provenance,
    row_group_size: int = ROW_GROUP_SIZE,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`write_geoparquet` over an iterable of (row, geometry) pairs, consumed once: each geometry is
    turned into WKB as it arrives and its coordinate lists are dropped, so a national layer never holds
    every feature's coordinates as Python objects at once (only the compact WKB, for the Z-order sort)."""
    kept: list[tuple[dict[str, Any], bytes, tuple[float, float, float, float]]] = []
    dropped = 0
    for row, geom in features:
        try:
            polys = polygons_of(geom)
        except GeometryError:
            dropped += 1
            continue
        kept.append((row, wkb_multipolygon(polys), bounds(polys)))

    if kept:
        gx0 = min(b[0] for _, _, b in kept)
        gy0 = min(b[1] for _, _, b in kept)
        gx1 = max(b[2] for _, _, b in kept)
        gy1 = max(b[3] for _, _, b in kept)
    else:
        gx0 = gy0 = gx1 = gy1 = 0.0
    sx = 65535 / (gx1 - gx0) if gx1 > gx0 else 0.0
    sy = 65535 / (gy1 - gy0) if gy1 > gy0 else 0.0

    def key(item: tuple[dict[str, Any], bytes, tuple[float, float, float, float]]) -> int:
        b = item[2]
        return _morton(int(((b[0] + b[2]) / 2 - gx0) * sx), int(((b[1] + b[3]) / 2 - gy0) * sy))

    kept.sort(key=key)
    prov = provenance.columns()
    columns: dict[str, list[Any]] = {name: [] for name, _ in schema}
    columns.update({"geometry": [], BBOX_COLUMN: []})
    columns.update({name: [] for name in prov})
    for row, wkb, b in kept:
        for name, _ in schema:
            columns[name].append(row.get(name))
        columns["geometry"].append(wkb)
        columns[BBOX_COLUMN].append(dict(zip(BBOX_FIELDS, b, strict=True)))
        for name, v in prov.items():
            # A row's own provenance value (a second service's URL) wins over the layer's.
            columns[name].append(row.get(name) or v)

    fields = [pa.field(name, typ) for name, typ in schema]
    fields += [pa.field("geometry", pa.binary())]
    fields += [pa.field(BBOX_COLUMN, pa.struct([pa.field(n, pa.float64()) for n in BBOX_FIELDS]))]
    fields += [pa.field(n, pa.string()) for n in prov]
    geo = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": ["MultiPolygon"],
                "bbox": [gx0, gy0, gx1, gy1],
                "covering": {"bbox": {n: [BBOX_COLUMN, n] for n in BBOX_FIELDS}},
            }
        },
    }
    meta = {b"geo": json.dumps(geo).encode()}
    if extra_metadata:
        meta[b"inframation"] = json.dumps(extra_metadata, sort_keys=True).encode()
    schema_pa = pa.schema(fields, metadata=meta)
    table = pa.Table.from_pydict({f.name: columns[f.name] for f in fields}, schema=schema_pa)
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    pq.write_table(table, tmp, row_group_size=row_group_size, compression="zstd", write_statistics=True)
    tmp.replace(path)
    return {
        "rows": len(kept),
        "dropped_geometry": dropped,
        "bytes": path.stat().st_size,
        "bbox": [gx0, gy0, gx1, gy1],
        "row_groups": pq.ParquetFile(path).num_row_groups,
    }
