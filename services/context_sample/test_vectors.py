"""Polygon reading and point-in-polygon without shapely: WKB decoding, holes, row-group pruning."""

from __future__ import annotations

import itertools
import pathlib
import struct
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pyarrow as pa  # type: ignore[import-untyped, unused-ignore]
import pytest

from pipeline.context.prospect.common import Provenance
from pipeline.context.prospect.geoparquet import polygons_of, wkb_multipolygon, write_geoparquet
from services.context_sample import vectors
from services.context_sample.vectors import PolygonLayer, decode_wkb, points_in_rings

PROV = Provenance("test.source", "https://example.org/x", "2026-10-22T00:00:00Z", "test licence", "v1")
SQUARE_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
    ],
}


def _naive_inside(x: float, y: float, rings: list[list[tuple[float, float]]]) -> bool:
    inside = False
    for ring in rings:
        for (x1, y1), (x2, y2) in itertools.pairwise(ring):
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def test_wkb_round_trip() -> None:
    polys = polygons_of(
        {
            "type": "MultiPolygon",
            "coordinates": [SQUARE_WITH_HOLE["coordinates"], [[[20, 20], [21, 20], [21, 21], [20, 20]]]],
        }
    )
    decoded = decode_wkb(wkb_multipolygon(polys))
    assert len(decoded) == 2
    assert decoded[0][1].tolist() == [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]


def test_hole_is_outside_and_ring_is_inside() -> None:
    rings = [np.asarray(r, dtype=float) for r in SQUARE_WITH_HOLE["coordinates"]]
    px = np.array([1.0, 5.0, 9.9, 11.0, 3.0])
    py = np.array([1.0, 5.0, 9.9, 5.0, 7.0])
    assert points_in_rings(px, py, rings).tolist() == [True, False, True, False, True]


def test_vectorised_test_agrees_with_a_naive_ray_cast() -> None:
    rng = np.random.default_rng(1)
    angles = np.sort(rng.uniform(0, 2 * np.pi, 300))
    radius = rng.uniform(2, 5, 300)
    ring = np.column_stack([np.cos(angles) * radius, np.sin(angles) * radius])
    ring = np.vstack([ring, ring[:1]])
    px, py = rng.uniform(-6, 6, 3000), rng.uniform(-6, 6, 3000)
    got = points_in_rings(px, py, [ring])
    pts = [tuple(p) for p in ring.tolist()]
    want = [_naive_inside(float(x), float(y), [pts]) for x, y in zip(px, py, strict=True)]
    assert got.tolist() == want


def test_row_groups_are_pruned_by_bbox(tmp_path: pathlib.Path) -> None:
    rows, geoms = [], []
    for i in range(40):  # a 40-square strip along the equator, 1 degree each
        rows.append({"name": f"sq{i}"})
        geoms.append({"type": "Polygon", "coordinates": [[[i, 0], [i + 1, 0], [i + 1, 1], [i, 1], [i, 0]]]})
    out = tmp_path / "strip.parquet"
    summary = write_geoparquet(
        out, rows, geoms, schema=[("name", pa.string())], provenance=PROV, row_group_size=4
    )
    assert summary["rows"] == 40 and summary["row_groups"] == 10
    layer = PolygonLayer(out)
    assert layer.geo["columns"]["geometry"]["covering"]["bbox"]["xmin"] == ["bbox", "xmin"]
    hits = layer.containing(12.5, 0.5)
    assert [f.props["name"] for f in hits] == ["sq12"]
    assert hits[0].props["source_id"] == "test.source" and hits[0].props["vintage"] == "v1"
    assert len(layer._cache) == 1, "one row group read for one point"
    members = layer.membership(np.array([0.5, 1.5, 39.5, 50.0]), np.array([0.5, 0.5, 0.5, 0.5]))
    assert sorted(f.props["name"] for f, _ in members) == ["sq0", "sq1", "sq39"]


def test_the_bbox_covering_is_the_geoparquet_1_1_struct(tmp_path: pathlib.Path) -> None:
    """GeoParquet 1.1 defines the bbox covering as one struct column whose xmin/ymin/xmax/ymax fields the
    covering paths name (["bbox", "xmin"], ...); top-level columns are not that form, so a reader that
    implements the specification would not prune with them."""
    import json

    import pyarrow.parquet as pq  # type: ignore[import-untyped, unused-ignore]

    out = tmp_path / "one.parquet"
    write_geoparquet(
        out, [{"name": "a"}], [SQUARE_WITH_HOLE], schema=[("name", pa.string())], provenance=PROV
    )
    pf = pq.ParquetFile(out)
    geo = json.loads(pf.schema_arrow.metadata[b"geo"])
    assert geo["version"] == "1.1.0"
    covering = geo["columns"]["geometry"]["covering"]["bbox"]
    assert covering == {k: ["bbox", k] for k in ("xmin", "ymin", "xmax", "ymax")}
    bbox_type = pf.schema_arrow.field("bbox").type
    assert pa.types.is_struct(bbox_type)
    assert [bbox_type.field(i).name for i in range(bbox_type.num_fields)] == ["xmin", "ymin", "xmax", "ymax"]
    assert all(pa.types.is_float64(bbox_type.field(i).type) for i in range(4))
    assert not {"xmin", "ymin", "xmax", "ymax"} & set(pf.schema_arrow.names)
    stats = {
        pf.metadata.row_group(0).column(i).path_in_schema: pf.metadata.row_group(0).column(i).statistics
        for i in range(pf.metadata.num_columns)
    }
    assert stats["bbox.xmin"].min == 0.0 and stats["bbox.xmax"].max == 10.0
    assert pq.read_table(out).column("bbox").to_pylist() == [
        {"xmin": 0.0, "ymin": 0.0, "xmax": 10.0, "ymax": 10.0}
    ]


def test_a_row_may_name_its_own_source_url(tmp_path: pathlib.Path) -> None:
    import pyarrow.parquet as pq  # type: ignore[import-untyped, unused-ignore]

    out = tmp_path / "two.parquet"
    rows = [{"name": "a"}, {"name": "b", "source_url": "https://example.org/second"}]
    other = {"type": "Polygon", "coordinates": [[[20, 0], [21, 0], [21, 1], [20, 1], [20, 0]]]}
    write_geoparquet(out, rows, [SQUARE_WITH_HOLE, other], schema=[("name", pa.string())], provenance=PROV)
    got = dict(zip(*(pq.read_table(out).column(c).to_pylist() for c in ("name", "source_url")), strict=True))
    assert got == {"a": "https://example.org/x", "b": "https://example.org/second"}


def test_coverage_says_where_the_layer_answers(tmp_path: pathlib.Path) -> None:
    """A layer built for Texas only is covered in Texas, not in New Mexico inside the Texas box."""
    from pipeline.context.prospect.coverage import region_box, states

    out = tmp_path / "tx.parquet"
    write_geoparquet(
        out,
        [{"name": "a"}],
        [SQUARE_WITH_HOLE],
        schema=[("name", pa.string())],
        provenance=PROV,
        extra_metadata={"coverage": states("texas", ("Texas",))},
    )
    layer = PolygonLayer(out)
    assert layer.coverage is not None and layer.coverage["states"] == ["Texas"]
    assert "wkb_hex" not in layer.coverage
    # Austin, then Hobbs NM (inside the Texas bounding box), then Fort Worth.
    lons, lats = np.array([-97.74, -103.14, -97.33]), np.array([30.27, 32.70, 32.75])
    assert np.asarray(layer.covered(lons, lats)).tolist() == [True, False, True]
    box = tmp_path / "box.parquet"
    write_geoparquet(
        box,
        [{"name": "a"}],
        [SQUARE_WITH_HOLE],
        schema=[("name", pa.string())],
        provenance=PROV,
        extra_metadata={"coverage": region_box("texas")},
    )
    assert np.asarray(PolygonLayer(box).covered(lons, lats)).tolist() == [True, True, True]
    assert PolygonLayer(tmp_path / "tx.parquet").covered(np.array([-120.0]), np.array([45.0])).tolist() == [
        False
    ]
    plain = tmp_path / "plain.parquet"
    write_geoparquet(
        plain, [{"name": "a"}], [SQUARE_WITH_HOLE], schema=[("name", pa.string())], provenance=PROV
    )
    assert PolygonLayer(plain).covered(lons, lats) is None


def test_non_polygonal_rows_are_dropped_and_counted(tmp_path: pathlib.Path) -> None:
    summary = write_geoparquet(
        tmp_path / "x.parquet",
        [{"name": "a"}, {"name": "b"}],
        [SQUARE_WITH_HOLE, {"type": "Point", "coordinates": [0, 0]}],
        schema=[("name", pa.string())],
        provenance=PROV,
    )
    assert summary["rows"] == 1 and summary["dropped_geometry"] == 1


def test_write_refuses_mismatched_lengths(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError):
        write_geoparquet(tmp_path / "x.parquet", [{}], [], schema=[], provenance=PROV)


def test_decode_wkb_reads_z_and_m_coordinates_as_lon_lat() -> None:
    """ISO WKB (type 1003, PolygonZ) and EWKB (Z flag in the high bit) both decode to the 2D ring."""
    ring = [(0.0, 0.0, 9.0), (1.0, 0.0, 9.0), (1.0, 1.0, 9.0), (0.0, 0.0, 9.0)]
    flat = [c for pt in ring for c in pt]
    for gtype in (1003, 0x80000003):
        buf = struct.pack("<BII", 1, gtype, 1) + struct.pack("<I", 4) + struct.pack(f"<{len(flat)}d", *flat)
        (poly,) = decode_wkb(buf)
        assert poly[0].tolist() == [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
    zm = [(0.0, 0.0, 9.0, 7.0), (2.0, 0.0, 9.0, 7.0), (2.0, 2.0, 9.0, 7.0), (0.0, 0.0, 9.0, 7.0)]
    flat = [c for pt in zm for c in pt]
    buf = struct.pack("<BII", 1, 3003, 1) + struct.pack("<I", 4) + struct.pack(f"<{len(flat)}d", *flat)
    assert decode_wkb(buf)[0][0][2].tolist() == [2.0, 2.0]


def test_streamed_features_write_the_same_file_as_lists(tmp_path: pathlib.Path) -> None:
    from pipeline.context.prospect.geoparquet import write_geoparquet_features

    rows = [{"name": f"p{i}"} for i in range(10)]
    geoms = [
        {"type": "Polygon", "coordinates": [[[i, 0], [i + 1, 0], [i + 1, 1], [i, 0]]]} for i in range(10)
    ]
    geoms.append({"type": "Point", "coordinates": [0, 0]})
    rows.append({"name": "not polygonal"})
    a = write_geoparquet(tmp_path / "a.parquet", rows, geoms, schema=[("name", pa.string())], provenance=PROV)
    streamed = ((r, g) for r, g in zip(rows, geoms, strict=True))
    b = write_geoparquet_features(
        tmp_path / "b.parquet", streamed, schema=[("name", pa.string())], provenance=PROV
    )
    assert a["rows"] == b["rows"] == 10 and a["dropped_geometry"] == b["dropped_geometry"] == 1
    assert (tmp_path / "a.parquet").read_bytes() == (tmp_path / "b.parquet").read_bytes()


def test_threads_sharing_one_layer_get_the_serial_answers(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API serves the sync route from a threadpool and the site finder samples from threads, so one
    PolygonLayer is read by many threads at once. A pyarrow ParquetFile is not safe to read from several
    threads (concurrent `read_row_group` calls raised `ReadRangeCache did not find matching cache entry`,
    returned wrong rows, or aborted the interpreter): every read goes through the layer's lock."""
    rows, geoms = [], []
    for i in range(20):
        for j in range(20):
            rows.append({"name": f"c{i}_{j}"})
            geoms.append(
                {"type": "Polygon", "coordinates": [[[i, j], [i + 1, j], [i + 1, j + 1], [i, j + 1], [i, j]]]}
            )
    out = tmp_path / "grid.parquet"
    write_geoparquet(out, rows, geoms, schema=[("name", pa.string())], provenance=PROV, row_group_size=4)
    # A one-group cache makes nearly every query read from the file, as cold traffic does.
    monkeypatch.setattr(vectors, "ROW_GROUP_CACHE", 1)
    rng = np.random.default_rng(7)
    pts = rng.uniform(0.01, 19.99, size=(1600, 2))
    serial = PolygonLayer(out)
    want = [[f.props["name"] for f in serial.containing(float(x), float(y))] for x, y in pts]
    assert all(len(w) == 1 for w in want)
    threads = 16
    for _trial in range(3):
        shared = PolygonLayer(out)
        barrier = threading.Barrier(threads)

        def work(k: int, layer: PolygonLayer = shared, gate: threading.Barrier = barrier) -> list[list[str]]:
            gate.wait()
            return [
                [f.props["name"] for f in layer.containing(float(x), float(y))] for x, y in pts[k::threads]
            ]

        with ThreadPoolExecutor(threads) as pool:
            parts = list(pool.map(work, range(threads)))
        got: list[list[str]] = [[] for _ in pts]
        for k, part in enumerate(parts):
            for n, names in enumerate(part):
                got[k + n * threads] = names
        assert got == want
