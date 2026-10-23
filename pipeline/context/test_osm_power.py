"""Tests for `pipeline.context.osm_power` (lane grid-osm, 2026-10-22).

Most tests need no network and no pyosmium: tag rules, WKB, GeoParquet, the GeoParquet -> asset
normalisers, the build's de-duplication across regions, and the server's `pull` against a fake
session. ``fixtures/osm_power_texas_sample.osm`` holds sixteen real OpenStreetMap elements from the
lane's Texas development run, one per rule (docs/25 §2.7; the file's own header says which and
why); ``fixtures/osm_power_texas_sample/`` is the build's recorded output for it, which the
normaliser tests read everywhere. The tests that read the OSM file itself run only where pyosmium is
installed (the build's own job installs it, `.github/workflows/grid-layers.yml`), and check that the
build still reproduces the recorded output."""

from __future__ import annotations

import hashlib
import json
import pathlib
import struct
from typing import Any

import pandas as pd
import pyarrow as pa  # type: ignore[import-untyped,unused-ignore]
import pyarrow.parquet as pq  # type: ignore[import-untyped,unused-ignore]
import pytest

from pipeline.context import osm_power as op
from pipeline.context.geo import StateIndex

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "osm_power_texas_sample.osm"
#: The build's recorded output for `FIXTURE` (GeoParquet and manifest).
RECORDED = pathlib.Path(__file__).parent / "fixtures" / "osm_power_texas_sample"
#: The newest element edit in the fixture; its header states no replication timestamp.
FIXTURE_TIMESTAMP = "2026-10-03T15:21:49Z"
LICENCE = "Open Database License 1.0 (test)"
TS = "2026-10-21T20:21:02Z"


# ------------------------------------------------------------------------------------- voltage
@pytest.mark.parametrize(
    ("raw", "values", "assumed"),
    [
        ("138000;69000", (138.0, 69.0), False),
        ("345000", (345.0,), False),
        ("345 kV", (345.0,), False),
        ("138kV", (138.0,), False),
        ("138000/69000", (138.0, 69.0), False),
        # Every stated value is kept, a repeated one too (review 2026-10-22): two 69 kV circuits.
        ("69000;69000", (69.0, 69.0), False),
        ("69000;138000;138000", (138.0, 138.0, 69.0), False),
        ("13800", (13.8,), False),
        ("12,47 kV", (12.47,), False),
        # Commas (review 2026-10-22): a list, a thousands mark or a decimal mark by what surrounds it.
        ("12470,69000", (69.0, 12.47), False),
        ("138000,69000", (138.0, 69.0), False),
        ("138000, 69000", (138.0, 69.0), False),
        ("138,000", (138.0,), False),
        ("115,000;69,000", (115.0, 69.0), False),
        ("115,000,69,000", (115.0, 69.0), False),
        ("138 kV,69 kV", (138.0, 69.0), False),
        ("13,8", (13.8,), True),
        ("138", (138.0,), True),
        ("765", (765.0,), True),
        ("34.5", (34.5,), True),
        ("400 V", (0.4,), False),
        # Service drops mis-tagged power=line in the Texas build (2026-10-22): volts, not kilovolts.
        ("240", (0.24,), False),
        ("208", (0.208,), False),
        ("480", (0.48,), False),
    ],
)
def test_parse_voltage_reads_volts_kilovolts_and_multi_values(raw, values, assumed):
    v = op.parse_voltage(raw)
    assert v.values_kv == values
    assert v.max_kv == values[0]
    assert v.assumed_kv is assumed
    assert v.raw == raw and not v.unknown


@pytest.mark.parametrize("raw", [None, "", "  ", "high", "unknown", float("nan")])
def test_parse_voltage_without_a_number_is_unknown(raw):
    v = op.parse_voltage(raw)
    assert v.unknown and v.max_kv is None and v.values_kv == ()


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"power": "line", "voltage": "138000"}, (True, "kept")),
        ({"power": "line", "voltage": "69000;12470"}, (True, "kept")),
        ({"power": "line", "voltage": "12470,69000"}, (True, "kept")),
        ({"power": "cable", "voltage": "345000"}, (True, "kept")),
        ({"power": "line"}, (True, "voltage_unknown")),
        ({"power": "line", "voltage": "medium"}, (True, "voltage_unknown")),
        ({"power": "line", "voltage": "34500"}, (False, "below_69kv")),
        ({"power": "line", "voltage": "240", "cables": "1"}, (False, "below_69kv")),
        ({"power": "line", "voltage": "480"}, (False, "below_69kv")),
        ({"power": "line", "voltage": "138000", "line": "busbar"}, (False, "busbar_or_bay")),
        ({"power": "line", "voltage": "138000", "line": "bay"}, (False, "busbar_or_bay")),
        ({"power": "minor_line", "voltage": "138000"}, (False, "not_a_line")),
    ],
)
def test_line_rules(tags, expected):
    assert op.line_kept(tags) == expected


@pytest.mark.parametrize(
    ("subtype", "kept"),
    [
        (None, True),
        ("transmission", True),
        ("distribution", True),
        ("converter", True),
        ("generation", True),
        ("switching", True),
        ("traction", False),
        ("minor_distribution", False),
    ],
)
def test_substation_rules(subtype, kept):
    tags = {"power": "substation"} | ({"substation": subtype} if subtype else {})
    assert op.substation_kept(tags)[0] is kept
    assert op.substation_kept({"power": "plant"}) == (False, "not_a_substation")


def test_cables_and_underground_lines_are_flagged():
    assert op.is_underground({"power": "cable"})
    assert op.is_underground({"power": "line", "location": "underground"})
    assert not op.is_underground({"power": "line"})


def test_an_explicit_location_wins_over_the_power_type():
    """An aerial cable (``power=cable`` + ``location=overhead``) is not underground (review 2026-10-22)."""
    assert not op.is_underground({"power": "cable", "location": "overhead"})
    assert not op.is_underground({"power": "cable", "location": "Overground"})
    assert op.is_underground({"power": "cable", "location": "underwater"})
    assert op.is_underground({"power": "cable", "location": "indoor"})  # not a statement either way


# ----------------------------------------------------------------------------------------- WKB
def test_wkb_round_trips_every_geometry_the_layers_write():
    assert op.wkb_to_geojson(op.wkb_point(-97.5, 30.25)) == {"type": "Point", "coordinates": [-97.5, 30.25]}
    line = [(-97.0, 30.0), (-97.1, 30.1), (-97.2, 30.3)]
    assert op.wkb_to_geojson(op.wkb_linestring(line)) == {
        "type": "LineString",
        "coordinates": [list(c) for c in line],
    }
    ring = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]
    hole = [(0.2, 0.2), (0.4, 0.2), (0.4, 0.4), (0.2, 0.2)]
    assert op.wkb_to_geojson(op.wkb_polygon([ring, hole]))["coordinates"] == [
        [list(c) for c in ring],
        [list(c) for c in hole],
    ]
    multi = op.wkb_to_geojson(op.wkb_multipolygon([[ring], [hole]]))
    assert multi is not None and multi["type"] == "MultiPolygon" and len(multi["coordinates"]) == 2
    assert op.wkb_to_geojson(None) is None


def test_wkb_reader_takes_big_endian_too():
    big = struct.pack(">BIdd", 0, 1, 1.5, -2.5)
    assert op.wkb_to_geojson(big) == {"type": "Point", "coordinates": [1.5, -2.5]}
    with pytest.raises(ValueError, match="unsupported"):
        op.wkb_to_geojson(struct.pack("<BI", 1, 7) + b"\x00" * 8)


def test_polygon_centroid_is_area_weighted_and_subtracts_holes():
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0)]
    assert op.polygon_centroid([[square]]) == pytest.approx((1.0, 1.0))
    hole = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)]
    cx, cy = op.polygon_centroid([[square, hole]]) or (0.0, 0.0)
    assert cx > 1.0 and cy > 1.0  # the hole pulls the centroid away from its corner
    assert op.polygon_centroid([[[(5.0, 5.0), (5.0, 5.0), (5.0, 5.0), (5.0, 5.0)]]]) == (5.0, 5.0)
    assert op.polygon_centroid([]) is None


# ---------------------------------------------------------------------------- records -> rows
def _line(osm_id: int, tags: dict[str, str], coords: list[tuple[float, float]]) -> op.LineRecord:
    return op.LineRecord("way", osm_id, {"power": "line", **tags}, coords)


def _sub(osm_type: str, osm_id: int, tags: dict[str, str], point: tuple[float, float], outline=None):
    return op.SubstationRecord(osm_type, osm_id, {"power": "substation", **tags}, point, outline)


SQUARE = [[(-97.001, 30.001), (-97.0, 30.001), (-97.0, 30.0), (-97.001, 30.0), (-97.001, 30.001)]]


def _built_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    straight = [(-97.0 + i * 0.001, 30.0) for i in range(11)]  # ten towers on a straight run
    lines = [
        op.line_row(
            _line(101, {"voltage": "138000;69000", "operator": "Oncor", "circuits": "2"}, straight),
            "US-TX",
            TS,
        ),
        op.line_row(_line(102, {"name": "Limestone - Gibbons Creek 345kV"}, straight[:3]), "US-TX", TS),
        op.line_row(
            op.LineRecord("way", 103, {"power": "cable", "voltage": "138 kV"}, straight[:2]), "US-TX", TS
        ),
    ]
    subs = [
        op.substation_row(
            _sub("node", 201, {"voltage": "138000;12470", "name": "Gibbons Creek"}, (-96.1, 30.6)),
            "US-TX",
            TS,
        ),
        op.substation_row(
            _sub(
                "way", 202, {"substation": "transmission", "operator": "Oncor"}, (-97.0005, 30.0005), [SQUARE]
            ),
            "US-TX",
            TS,
        ),
    ]
    return lines, subs


def test_line_rows_carry_parsed_voltage_raw_tags_and_osm_coordinates():
    lines, _ = _built_rows()
    first = lines[0]
    assert (first["voltage_kv"], first["voltages_kv"], first["voltage_raw"]) == (
        138.0,
        [138.0, 69.0],
        "138000;69000",
    )
    assert first["operator"] == "Oncor" and first["circuits"] == "2" and first["underground"] is False
    assert (first["region"], first["states_crossed"], first["extract_regions"]) == (
        "US-TX",
        ["US-TX"],
        ["US-TX"],
    )
    assert (first["scope_regions"], first["extract_timestamp"]) == (["US-TX"], TS)
    assert lines[1]["voltage_unknown"] is True and lines[1]["voltage_kv"] is None
    assert lines[2]["power"] == "cable" and lines[2]["underground"] is True


def test_geoparquet_files_carry_geo_metadata_and_round_trip(tmp_path):
    lines, subs = _built_rows()
    meta = op.write_geoparquet(lines, tmp_path / "lines.parquet", kind="lines")
    op.write_geoparquet(subs, tmp_path / "substations.parquet", kind="substations")
    data = (tmp_path / "lines.parquet").read_bytes()
    assert meta == {"rows": 3, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    geo = json.loads(pq.read_schema(tmp_path / "lines.parquet").metadata[b"geo"])
    assert geo["primary_column"] == "geometry" and geo["columns"]["geometry"]["encoding"] == "WKB"
    assert geo["columns"]["geometry"]["geometry_types"] == ["LineString"]
    assert geo["columns"]["geometry"]["bbox"] == pytest.approx([-97.0, 30.0, -96.99, 30.0])
    sub_geo = json.loads(pq.read_schema(tmp_path / "substations.parquet").metadata[b"geo"])
    assert sub_geo["columns"]["outline"]["geometry_types"] == ["Polygon", "MultiPolygon"]
    back = op.read_geoparquet(tmp_path / "lines.parquet")
    assert [r["osm_id"] for r in back] == [101, 102, 103] and back[0]["voltages_kv"] == [138.0, 69.0]
    pq.write_table(pa.table({"a": [1]}), tmp_path / "plain.parquet")
    with pytest.raises(op.BuildError, match="GeoParquet"):
        op.read_geoparquet(tmp_path / "plain.parquet")


def test_display_names_read_cleanly_with_and_without_a_voltage_or_operator():
    """Review 2026-10-22: an unnamed line with no voltage was 'Power line, voltage not stated, (OSM
    way 2)', a stray comma on a public page and in its slug."""
    base = {"osm_type": "way", "osm_id": 2, "power": "line", "voltage_kv": None, "operator": None}
    assert op.line_display_name(base) == "Power line, voltage not stated (OSM way 2)"
    assert (
        op.line_display_name({**base, "operator": "Oncor"})
        == "Oncor power line, voltage not stated (OSM way 2)"
    )
    assert op.line_display_name({**base, "power": "cable"}) == "Power cable, voltage not stated (OSM way 2)"
    assert (
        op.line_display_name({**base, "voltage_kv": 138.0, "operator": "Oncor"})
        == "Oncor 138 kV line (OSM way 2)"
    )
    assert op.line_display_name({**base, "voltage_kv": 34.5}) == "34.5 kV line (OSM way 2)"
    assert op.line_display_name({**base, "name": "Limestone - Gibbons Creek"}) == "Limestone - Gibbons Creek"


def test_normalised_lines_carry_provenance_per_record_and_a_simplified_line():
    lines, _ = _built_rows()
    df = op.normalise_lines(lines, licence=LICENCE)
    assert list(df.columns) == op.ASSET_COLUMNS
    row = df.iloc[0]
    assert row["source_id"] == "us.osm.power" and row["source_asset_id"] == "way/101"
    assert row["source_url"] == "https://www.openstreetmap.org/way/101"
    assert row["retrieved_at"] == TS and row["licence"] == LICENCE
    assert row["name"] == "Oncor 138 kV line (OSM way 101)"
    assert row["operator_name"] is None and row["status"] == "operating" and row["country"] == "US"
    attrs = row["attributes"]
    assert attrs["voltage_kv"] == 138.0 and attrs["voltages_kv"] == [138.0, 69.0]
    assert attrs["voltage_class"] == "100-161 kV" and attrs["circuits"] == 2
    assert attrs["vertex_count_source"] == 11 and attrs["vertex_count"] == 2  # collinear towers dropped
    assert row["geom_line_wkt"] == "MULTILINESTRING((-97 30, -96.99 30))"
    assert attrs["miles"] == pytest.approx(0.6, abs=0.05)
    assert df.iloc[1]["name"] == "Limestone - Gibbons Creek 345kV"
    assert df.iloc[1]["attributes"]["voltage_unknown"] is True
    assert attrs["region"] == "US-TX" and attrs["coverage_regions"] == attrs["scope_regions"] == ["US-TX"]
    assert (
        df.iloc[2]["name"] == "138 kV cable (OSM way 103)" and df.iloc[2]["attributes"]["underground"] is True
    )


def test_normalised_substations_keep_point_outline_subtype_and_voltage():
    _, subs = _built_rows()
    df = op.normalise_substations(subs, licence=LICENCE)
    node, area = df.iloc[0], df.iloc[1]
    assert (
        node["source_asset_id"] == "node/201"
        and node["source_url"] == "https://www.openstreetmap.org/node/201"
    )
    assert node["name"] == "Gibbons Creek substation" and (node["lon"], node["lat"]) == (-96.1, 30.6)
    assert node["attributes"]["voltages_kv"] == [138.0, 12.47] and node["attributes"]["outline"] is None
    assert node["attributes"]["location_method"] == "node" and node["geom_line_wkt"] is None
    assert area["name"] == "Oncor substation (OSM way 202)"
    assert area["attributes"]["substation_type"] == "transmission"
    assert area["attributes"]["outline"]["type"] == "Polygon"
    assert area["attributes"]["location_method"] == "area_centroid"
    assert area["attributes"]["voltage_unknown"] is True and area["retrieved_at"] == TS


def _states() -> StateIndex:
    if not op.STATES_GEOJSON.exists():
        pytest.skip("vendored state polygons not in this checkout")
    return StateIndex.from_file(op.STATES_GEOJSON)


def test_rows_are_labelled_by_the_state_polygon_not_by_the_extract():
    states = _states()
    # Read from the Texas extract (Geofabrik's polygon runs past the line), but in Oklahoma City.
    row = op.line_row(_line(1, {}, [(-97.5, 35.0), (-97.4, 35.1)]), "US-TX", TS, states=states)
    assert (row["region"], row["states_crossed"]) == ("US-OK", ["US-OK"])
    assert (row["extract_regions"], row["scope_regions"]) == (["US-TX"], ["US-OK", "US-TX"])
    sub = op.substation_row(_sub("node", 2, {}, (-92.0, 30.5)), "US-TX", TS, states=states)
    assert (sub["region"], sub["scope_regions"]) == ("US-LA", ["US-LA", "US-TX"])
    offshore = op.substation_row(_sub("node", 3, {}, (-94.5, 28.5)), "US-TX", TS, states=states)
    assert offshore["region"] == "US-TX"  # in no polygon (the Gulf): the extract's region
    df = op.normalise_lines([row], licence=LICENCE, covered=["US-TX"])
    assert df.iloc[0]["state_code"] == "US-OK"
    attrs = df.iloc[0]["attributes"]
    assert (attrs["region"], attrs["scope_regions"], attrs["coverage_regions"]) == (
        "US-OK",
        ["US-OK", "US-TX"],
        ["US-TX"],
    )
    # A row without labels (not this build's) is labelled from its geometry when an index is given.
    unlabelled = {**row, "region": None, "states_crossed": [], "scope_regions": []}
    again = op.normalise_lines([unlabelled], licence=LICENCE, states=states).iloc[0]
    assert again["state_code"] == "US-OK" and again["attributes"]["scope_regions"] == ["US-OK", "US-TX"]
    bare = {**unlabelled, "extract_regions": []}  # nothing to label it with: no region, never ""
    blank = op.normalise_lines([bare], licence=LICENCE).iloc[0]
    assert blank["state_code"] is None and blank["attributes"]["states_crossed"] == []


def test_a_border_way_whose_first_extract_is_the_neighbour_stays_in_a_texas_load(tmp_path):
    """Review 2026-10-22, blocking: a way across the Red River is in the Oklahoma and Texas extracts,
    and Oklahoma comes first in table order. It used to be labelled US-OK and dropped from a US-TX
    load. Now it is held once, labelled by its geometry, and in Texas's scope, so the US-TX load,
    its count in the manifest and the map's coverage all hold it."""
    states = _states()
    crossing = _line(9, {"voltage": "345000"}, [(-97.5, 33.6), (-97.5, 34.2), (-97.5, 34.4)])
    cut = _line(9, {"voltage": "345000"}, [(-97.5, 34.2), (-97.5, 34.4)])  # the OK extract's copy, cut
    ok_only = _line(10, {"voltage": "138000"}, [(-97.5, 35.0), (-97.4, 35.1)])  # Oklahoma City
    tx_only = _line(11, {"voltage": "138000"}, [(-97.0, 31.0), (-97.1, 31.1)])  # central Texas
    result = op.BuildResult()
    ok = op.ExtractStats(region="US-OK", timestamp=TS)
    tx = op.ExtractStats(region="US-TX", timestamp="2026-10-21T21:00:00Z")
    op.add_region(result, [cut, ok_only], [_sub("node", 5, {}, (-97.5, 35.0))], ok, states=states)
    op.add_region(result, [crossing, tx_only], [], tx, states=states)
    op.count_scope(result)
    by_id = {r["osm_id"]: r for r in result.lines}
    assert sorted(by_id) == [9, 10, 11]
    border = by_id[9]
    assert border["region"] == "US-OK" and border["states_crossed"] == ["US-OK", "US-TX"]
    assert border["extract_regions"] == ["US-OK", "US-TX"] and border["scope_regions"] == ["US-OK", "US-TX"]
    # The Texas copy is whole, so it replaced the cut one (and its timestamp came with it).
    assert len(op.wkb_to_geojson(border["geometry"])["coordinates"]) == 3
    assert border["extract_timestamp"] == "2026-10-21T21:00:00Z"
    assert (ok.lines_read, tx.lines_read, tx.duplicates_skipped) == (2, 2, 1)
    assert (ok.lines_kept, tx.lines_kept, ok.substations_kept, tx.substations_kept) == (2, 2, 1, 0)
    geo = tmp_path / "geo"
    files = {
        op.GEO_LINES_NAME: op.write_geoparquet(result.lines, geo / op.GEO_LINES_NAME, kind="lines"),
        op.GEO_SUBSTATIONS_NAME: op.write_geoparquet(
            result.substations, geo / op.GEO_SUBSTATIONS_NAME, kind="substations"
        ),
    }
    manifest = {"version": "v1/test", "regions": [ok.as_dict(), tx.as_dict()], "files": files}
    (geo / op.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    summary = op.write_asset_frames(geo, tmp_path / "out", licence=LICENCE, regions=["US-TX"])
    assert (summary["lines"], summary["substations"], summary["regions"]) == (tx.lines_kept, 0, ["US-TX"])
    frame = pd.read_parquet(tmp_path / "out" / op.LINES_FILE).set_index("source_asset_id")
    assert sorted(frame.index) == ["way/11", "way/9"]
    attrs = json.loads(frame.loc["way/9", "attributes"])
    assert frame.loc["way/9", "state_code"] == "US-OK" and attrs["coverage_regions"] == ["US-TX"]
    held = json.loads((tmp_path / "out" / f"{op.SOURCE_ID}.manifest.json").read_text())
    assert [r["region"] for r in held["regions"]] == ["US-TX"]
    # Asking for a region the build does not hold loads nothing and says so.
    none = op.write_asset_frames(geo, tmp_path / "none", licence=LICENCE, regions=["US-NM"])
    assert (none["lines"], none["regions"], none["regions_missing"]) == (0, [], ["US-NM"])


def test_a_region_load_filters_the_published_file_in_pyarrow_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "READ_BATCH_ROWS", 2)
    regions = ["US-TX", "US-OK", "US-TX", "US-LA", "US-OK", "US-TX", "US-AR"]
    rows = [
        op.line_row(_line(i, {"voltage": "138000"}, [(-97.0, 30.0 + i / 100), (-97.1, 30.0)]), r, TS)
        for i, r in enumerate(regions)
    ]
    rows[4]["scope_regions"] = ["US-OK", "US-TX"]  # a border way labelled Oklahoma
    op.write_geoparquet(rows, tmp_path / "lines.parquet", kind="lines")
    got = op.read_geoparquet(tmp_path / "lines.parquet", regions={"US-TX"})
    assert [r["osm_id"] for r in got] == [0, 2, 4, 5]
    assert got[2]["scope_regions"] == ["US-OK", "US-TX"] and isinstance(got[0]["geometry"], bytes)
    assert len(op.read_geoparquet(tmp_path / "lines.parquet")) == 7
    assert op.read_geoparquet(tmp_path / "lines.parquet", regions=set()) == []


def test_clip_keeps_border_lines_whole_and_drops_what_lies_outside():
    if not op.STATES_GEOJSON.exists():
        pytest.skip("vendored state polygons not in this checkout")
    states = StateIndex.from_file(op.STATES_GEOJSON)
    stats = op.ExtractStats(region="US-TX")
    crossing = _line(1, {}, [(-97.5, 33.5), (-97.5, 34.5)])  # Texas into Oklahoma across the Red River
    outside = _line(2, {}, [(-97.5, 35.0), (-97.4, 35.1)])  # Oklahoma City
    inside_sub = _sub("node", 3, {}, (-97.74, 30.27))  # Austin
    outside_sub = _sub("node", 4, {}, (-92.0, 30.5))  # Louisiana
    lines, subs = op.clip_to_region([crossing, outside], [inside_sub, outside_sub], "US-TX", states, stats)
    assert [ln.osm_id for ln in lines] == [1] and [s.osm_id for s in subs] == [3]
    assert stats.dropped == {"line_outside_region": 1, "substation_outside_region": 1}


# ------------------------------------------------------------------------------- build helpers
def test_states_select_by_iso_code_or_geofabrik_slug_in_table_order():
    assert len(op.select_states("all")) == len(op.US_STATE_EXTRACTS) == 52
    assert op.select_states("texas,US-OK") == [("US-OK", "oklahoma"), ("US-TX", "texas")]
    assert op.geofabrik_url("texas") == "https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf"
    with pytest.raises(op.BuildError, match="atlantis"):
        op.select_states("texas,atlantis")


def _gate_manifest(counts: dict[str, tuple[int, int]]) -> dict[str, Any]:
    return {
        "files": {op.GEO_LINES_NAME: {"rows": 1}, op.GEO_SUBSTATIONS_NAME: {"rows": 1}},
        "regions": [
            {"region": r, "lines_kept": lines, "substations_kept": subs}
            for r, (lines, subs) in counts.items()
        ],
    }


def test_the_promotion_gate_has_an_absolute_floor_and_still_blocks_a_state_emptied():
    """Review 2026-10-22: a handful of edits in DC is over 10% and must not block the national
    promotion; 50 rows and more than 10% still does, and a state falling to nothing always does."""
    published = _gate_manifest({"US-DC": (40, 30), "US-TX": (21000, 6200)})
    small = _gate_manifest({"US-DC": (34, 26), "US-TX": (20900, 6190)})
    assert op.compare_manifests(small, published) == []
    emptied = _gate_manifest({"US-DC": (0, 30), "US-TX": (21000, 6200)})
    assert op.compare_manifests(emptied, published) == ["US-DC: lines_kept fell from 40 to none"]
    large = _gate_manifest({"US-DC": (40, 30), "US-TX": (18000, 6200)})
    assert op.compare_manifests(large, published) == [
        "US-TX: lines_kept fell from 21000 to 18000 (more than 10% and 50 rows)"
    ]
    assert op.compare_manifests(small, published, min_rows=1) == [
        "US-DC: lines_kept fell from 40 to 34 (more than 10% and 1 rows)",
        "US-DC: substations_kept fell from 30 to 26 (more than 10% and 1 rows)",
    ]


def test_version_key_names_data_and_build_time():
    key = op.version_key({"data_timestamp": "2026-10-21T20:21:02Z", "built_at": "2026-10-22T06:15:00Z"})
    assert key == "v1/20261021T202102Z-20261022T061500Z"


def test_timestamps_normalise_to_utc_seconds():
    assert op.normalise_timestamp("2026-10-21T20:21:02Z") == "2026-10-21T20:21:02Z"
    assert op.normalise_timestamp("2026-10-21T15:21:02-05:00") == "2026-10-21T20:21:02Z"


def test_default_base_url_prefers_the_explicit_setting(monkeypatch):
    monkeypatch.delenv("GRID_LAYERS_URL", raising=False)
    monkeypatch.delenv("BANKABLE_DOMAIN", raising=False)
    assert op.default_base_url() is None
    monkeypatch.setenv("BANKABLE_DOMAIN", "inframation.io")
    assert op.default_base_url() == "https://tiles.inframation.io/grid/osm-power"
    monkeypatch.setenv("GRID_LAYERS_URL", "https://example.test/grid/osm-power/")
    assert op.default_base_url() == "https://example.test/grid/osm-power"


def test_measure_counts_miles_by_voltage_class_and_substations_by_type():
    lines, subs = _built_rows()
    m = op.measure(
        op.normalise_lines(lines, licence=LICENCE), op.normalise_substations(subs, licence=LICENCE)
    )
    assert m["lines"] == 3 and set(m["by_voltage_class"]) == {"100-161 kV", "voltage unknown"}
    assert m["by_voltage_class"]["100-161 kV"]["lines"] == 2
    assert m["by_voltage_class"]["100-161 kV"]["underground_miles"] > 0
    assert m["substations_by_type"] == {"(untagged)": 1, "transmission": 1}


# ----------------------------------------------------------------------------------------- pull
class _Resp:
    def __init__(self, status: int, content: bytes) -> None:
        self.status_code = status
        self.content = content
        self.closed = False

    def iter_content(self, chunk_size: int = 1) -> Any:
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requested: list[str] = []
        self.streamed: list[str] = []
        self.responses: list[_Resp] = []

    def get(self, url: str, **kwargs: Any) -> _Resp:
        self.requested.append(url)
        if kwargs.get("stream"):
            self.streamed.append(url)
        resp = _Resp(200, self.files[url]) if url in self.files else _Resp(404, b"")
        self.responses.append(resp)
        return resp


def _published(tmp_path: pathlib.Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    lines, subs = _built_rows()
    geo = tmp_path / "geo"
    files_meta = {
        op.GEO_LINES_NAME: op.write_geoparquet(lines, geo / op.GEO_LINES_NAME, kind="lines"),
        op.GEO_SUBSTATIONS_NAME: op.write_geoparquet(subs, geo / op.GEO_SUBSTATIONS_NAME, kind="substations"),
    }
    manifest = {
        "version": "v1/20261021T202102Z-20261022T061500Z",
        "regions": [{"region": "US-TX", "timestamp": TS}],
        "files": files_meta,
    }
    base = "https://tiles.example.test/grid/osm-power"
    served = {f"{base}/{op.LATEST_NAME}": json.dumps(manifest).encode()}
    for name in files_meta:
        served[f"{base}/{manifest['version']}/{name}"] = (geo / name).read_bytes()
    return served, manifest


def test_pull_checks_checksums_writes_frames_and_skips_a_version_it_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "_licence_text", lambda: LICENCE)
    served, manifest = _published(tmp_path)
    session = _Session(served)
    out = tmp_path / "normalized"
    base = "https://tiles.example.test/grid/osm-power"
    summary = op.pull(base, work_dir=tmp_path / "work", out_dir=out, session=session, with_states=False)
    assert summary["status"] == "pulled" and (summary["lines"], summary["substations"]) == (3, 2)
    assert summary["regions"] == ["US-TX"]
    frame = pd.read_parquet(out / op.LINES_FILE)
    assert frame["source_url"].iloc[0] == "https://www.openstreetmap.org/way/101"
    assert json.loads((out / f"{op.SOURCE_ID}.manifest.json").read_text())["version"] == manifest["version"]
    assert not any((tmp_path / "work" / "osm-power-pull").iterdir())  # downloads cleaned up
    # The two GeoParquet files are streamed to disk, never read whole (review 2026-10-23: the pull
    # runs in the worker's container, and the national lines file is about 90-100 MB), and every
    # response is closed.
    assert session.streamed == [f"{base}/{manifest['version']}/{name}" for name in manifest["files"]]
    streamed = [
        r for url, r in zip(session.requested, session.responses, strict=True) if url in session.streamed
    ]
    assert len(streamed) == 2 and all(r.closed for r in streamed)
    again = op.pull(
        base, work_dir=tmp_path / "work", out_dir=out, session=_Session(served), with_states=False
    )
    assert again["status"] == "unchanged"


def test_pull_can_load_a_subset_of_regions_and_narrows_the_manifest_to_it(tmp_path, monkeypatch):
    """`GRID_LAYERS_REGIONS` (docs/25 §2.7, "The national map payload"): a server that cannot hold the
    national line index loads the states it names; the manifest copied beside the frames lists only
    those, so the loader withdraws nothing elsewhere, and a changed filter re-pulls the same version."""
    monkeypatch.setattr(op, "_licence_text", lambda: LICENCE)
    lines, subs = _built_rows()
    ok_line = op.line_row(_line(301, {"voltage": "345000"}, [(-97.5, 35.0), (-97.4, 35.1)]), "US-OK", TS)
    ok_sub = op.substation_row(_sub("node", 302, {}, (-97.5, 35.0)), "US-OK", TS)
    geo = tmp_path / "geo"
    files_meta = {
        op.GEO_LINES_NAME: op.write_geoparquet([*lines, ok_line], geo / op.GEO_LINES_NAME, kind="lines"),
        op.GEO_SUBSTATIONS_NAME: op.write_geoparquet(
            [*subs, ok_sub], geo / op.GEO_SUBSTATIONS_NAME, kind="substations"
        ),
    }
    version = "v1/20261021T202102Z-20261022T061500Z"
    manifest = {
        "version": version,
        "regions": [{"region": "US-OK", "timestamp": TS}, {"region": "US-TX", "timestamp": TS}],
        "files": files_meta,
    }
    base = "https://tiles.example.test/grid/osm-power"
    served = {f"{base}/{op.LATEST_NAME}": json.dumps(manifest).encode()}
    for name in files_meta:
        served[f"{base}/{version}/{name}"] = (geo / name).read_bytes()
    out = tmp_path / "normalized"
    summary = op.pull(
        base,
        work_dir=tmp_path / "w",
        out_dir=out,
        session=_Session(served),
        with_states=False,
        regions=["US-OK"],
    )
    assert summary["status"] == "pulled" and (summary["lines"], summary["substations"]) == (1, 1)
    assert summary["regions"] == ["US-OK"]
    assert list(pd.read_parquet(out / op.LINES_FILE)["source_asset_id"]) == ["way/301"]
    held = json.loads((out / f"{op.SOURCE_ID}.manifest.json").read_text())
    assert [r["region"] for r in held["regions"]] == ["US-OK"] and held["regions_loaded"] == ["US-OK"]
    again = op.pull(base, work_dir=tmp_path / "w", out_dir=out, session=_Session(served), with_states=False)
    assert again["status"] == "pulled" and again["regions"] == ["US-OK", "US-TX"]  # the filter changed
    with pytest.raises(op.BuildError, match="US-ZZ"):
        op.pull(base, work_dir=tmp_path / "w", out_dir=out, session=_Session(served), regions=["US-ZZ"])


def test_regions_setting_must_be_set_and_reads_iso_codes_or_all(monkeypatch):
    """Fail closed (review 2026-10-22): unset is refused, never read as the national layer the beta
    server cannot hold; national is `all`, written down."""
    monkeypatch.delenv("GRID_LAYERS_REGIONS", raising=False)
    with pytest.raises(op.BuildError, match="GRID_LAYERS_REGIONS is not set"):
        op.default_regions()
    monkeypatch.setenv("GRID_LAYERS_REGIONS", "  ")
    with pytest.raises(op.BuildError, match="not set"):
        op.default_regions()
    monkeypatch.setenv("GRID_LAYERS_REGIONS", " us-tx, US-OK ,")
    assert op.default_regions() == ["US-OK", "US-TX"]
    monkeypatch.setenv("GRID_LAYERS_REGIONS", "all")
    assert op.default_regions() is None
    monkeypatch.setenv("GRID_LAYERS_REGIONS", ",")
    with pytest.raises(op.BuildError, match="no region"):
        op.default_regions()


def test_pull_cli_refuses_to_run_without_a_regions_setting(monkeypatch):
    monkeypatch.delenv("GRID_LAYERS_REGIONS", raising=False)

    def no_network(*_: object, **__: object) -> None:
        raise AssertionError("pull must not run")

    monkeypatch.setattr(op, "pull", no_network)
    with pytest.raises(SystemExit, match="GRID_LAYERS_REGIONS is not set"):
        op.main(["pull", "--base-url", "https://tiles.example.test/grid/osm-power"])


def test_pull_before_the_first_national_build_is_not_published_and_keeps_the_frames(tmp_path, monkeypatch):
    """Lane sf-golive: until grid-layers.yml promotes a build, `latest.json` answers 404. The server's
    `context_build` runs this pull every month, so that is `not_published` (exit 0), never an error,
    and the frames the server already holds are left as they are."""
    out = tmp_path / "normalized"
    out.mkdir()
    (out / op.LINES_FILE).write_bytes(b"held")
    base = "https://tiles.example.test/grid/osm-power"
    session = _Session({})
    summary = op.pull(base, work_dir=tmp_path / "work", out_dir=out, session=session, regions=["US-TX"])
    assert summary == {"source_id": op.SOURCE_ID, "status": op.NOT_PUBLISHED, "url": f"{base}/latest.json"}
    assert session.requested == [f"{base}/latest.json"]
    assert (out / op.LINES_FILE).read_bytes() == b"held" and not (tmp_path / "work").exists()
    down = _Session({})
    down.get = lambda url, **_: _Resp(503, b"")
    with pytest.raises(op.BuildError, match="HTTP 503"):  # any other answer is still an error
        op.pull(base, work_dir=tmp_path / "work", out_dir=out, session=down, regions=["US-TX"])


def test_pull_refuses_a_file_whose_checksum_does_not_match(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "_licence_text", lambda: LICENCE)
    served, manifest = _published(tmp_path)
    base = "https://tiles.example.test/grid/osm-power"
    served[f"{base}/{manifest['version']}/{op.GEO_LINES_NAME}"] += b"tampered"
    with pytest.raises(op.BuildError, match="sha256"):
        op.pull(base, work_dir=tmp_path / "work", out_dir=tmp_path / "out", session=_Session(served))
    assert not (tmp_path / "out" / op.LINES_FILE).exists()
    served[f"{base}/{op.LATEST_NAME}"] = json.dumps({"version": "../../etc"}).encode()
    with pytest.raises(op.BuildError, match="no usable version"):
        op.pull(base, work_dir=tmp_path / "work", out_dir=tmp_path / "out", session=_Session(served))


def test_the_frames_and_their_manifest_are_one_set_renamed_into_place(tmp_path, monkeypatch):
    """Review 2026-10-22: the lines frame, the substations frame and the manifest were written one
    after another, so a crash between them paired new frames with the previous manifest's covered
    regions. Now all three carry one `frame_set` token and are renamed into place, the manifest last;
    a write that fails part-way leaves the previous set as it was and no temporary file behind."""
    monkeypatch.setattr(op, "_licence_text", lambda: LICENCE)
    served, _ = _published(tmp_path)
    geo = tmp_path / "geo"
    (geo / op.MANIFEST_NAME).write_text(
        served["https://tiles.example.test/grid/osm-power/latest.json"].decode(), encoding="utf-8"
    )
    out = tmp_path / "out"
    op.write_asset_frames(geo, out, with_states=False)
    manifest = json.loads((out / op.FRAMES_MANIFEST_FILE).read_text())
    token = manifest[op.FRAME_SET_KEY]
    for name in (op.LINES_FILE, op.SUBSTATIONS_FILE):
        assert pq.read_schema(out / name).metadata[op.FRAME_SET_KEY.encode()].decode() == token
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [op.LINES_FILE, op.SUBSTATIONS_FILE, op.FRAMES_MANIFEST_FILE]
    )
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    real_write = pq.write_table
    calls: list[str] = []

    def fail_on_the_second_frame(table: Any, where: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(str(where))
        if len(calls) == 2:
            pathlib.Path(where).write_bytes(b"PAR1 half a file")  # the disk fills part-way through
            raise OSError("disk full")
        real_write(table, where, *args, **kwargs)

    monkeypatch.setattr(op.pq, "write_table", fail_on_the_second_frame)
    with pytest.raises(OSError, match="disk full"):
        op.write_asset_frames(geo, out, with_states=False)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


class _Stream:
    """A streamed download: `chunks`, then `error` raised mid-stream if given."""

    def __init__(
        self, status: int, chunks: list[bytes], *, length: int | None = None, error: Exception | None = None
    ) -> None:
        self.status_code = status
        self.headers = {"Content-Length": str(length)} if length is not None else {}
        self._chunks, self._error = chunks, error
        self.text = b"".join(chunks).decode("latin-1")
        self.closed = False

    def iter_content(self, chunk_size: int = 1) -> Any:
        yield from self._chunks
        if self._error is not None:
            raise self._error

    def close(self) -> None:
        self.closed = True


class _ScriptedSession:
    """Answers each URL with the next response scripted for it."""

    def __init__(self, script: dict[str, list[_Stream]]) -> None:
        self.script = {url: list(answers) for url, answers in script.items()}
        self.requested: list[str] = []

    def get(self, url: str, **_: Any) -> _Stream:
        self.requested.append(url)
        return self.script[url].pop(0)


PBF_URL = "https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf"
PBF = b"\x00\x00\x00\x0dOSMHeader" + bytes(range(256)) * 8


def _md5_answer(body: bytes = PBF) -> _Stream:
    return _Stream(200, [f"{hashlib.md5(body).hexdigest()}  texas-latest.osm.pbf\n".encode()])  # noqa: S324


def test_a_state_cut_mid_stream_is_downloaded_again_and_checked_against_its_md5(tmp_path):
    """Review 2026-10-22: one reset part-way through a state aborted the whole national run, and a
    clean but short transfer was caught only when pyosmium failed to read it. Now each state is
    checked against Geofabrik's `.md5` and the Content-Length, and retried alone."""
    import requests

    reset = requests.exceptions.ChunkedEncodingError("Connection broken: reset by peer")
    session = _ScriptedSession(
        {
            f"{PBF_URL}.md5": [_md5_answer(), _md5_answer()],
            PBF_URL: [
                _Stream(200, [PBF[:100]], length=len(PBF), error=reset),
                _Stream(200, [PBF[:1000], PBF[1000:]], length=len(PBF)),
            ],
        }
    )
    slept: list[float] = []
    dest = tmp_path / "osm" / "texas-latest.osm.pbf"
    got = op.download(PBF_URL, dest, session=session, sleep=slept.append)
    assert dest.read_bytes() == PBF
    assert (got["attempts"], got["bytes"], got["md5"]) == (2, len(PBF), hashlib.md5(PBF).hexdigest())  # noqa: S324
    assert slept == [op.DOWNLOAD_RETRY_DELAYS_S[0]]
    assert session.requested == [f"{PBF_URL}.md5", PBF_URL, f"{PBF_URL}.md5", PBF_URL]


@pytest.mark.parametrize(
    "bad",
    [
        _Stream(200, [PBF[:500]], length=len(PBF)),  # a clean close, short of the announced length
        _Stream(200, [PBF[:-1] + bytes([PBF[-1] ^ 1])], length=len(PBF)),  # right length, wrong bytes
    ],
    ids=["short", "md5"],
)
def test_a_state_that_never_arrives_whole_fails_the_build_and_leaves_no_file(tmp_path, bad):
    session = _ScriptedSession({f"{PBF_URL}.md5": [_md5_answer()] * 3, PBF_URL: [bad, bad, bad]})
    slept: list[float] = []
    dest = tmp_path / "texas-latest.osm.pbf"
    with pytest.raises(op.BuildError, match="3 attempts failed"):
        op.download(PBF_URL, dest, session=session, sleep=slept.append)
    assert not dest.exists() and slept == list(op.DOWNLOAD_RETRY_DELAYS_S)


def test_a_refused_download_is_not_retried(tmp_path):
    session = _ScriptedSession({f"{PBF_URL}.md5": [_md5_answer()], PBF_URL: [_Stream(404, [])]})
    slept: list[float] = []
    with pytest.raises(op.BuildError, match="HTTP 404"):
        op.download(PBF_URL, tmp_path / "x.pbf", session=session, sleep=slept.append)
    assert slept == [] and session.requested == [f"{PBF_URL}.md5", PBF_URL]
    no_md5 = _ScriptedSession({f"{PBF_URL}.md5": [_Stream(200, [b"<html>not a checksum</html>"])]})
    with pytest.raises(op.BuildError, match="no md5 checksum"):
        op.download(PBF_URL, tmp_path / "x.pbf", session=no_md5, sleep=slept.append)
    assert slept == []


def test_recorded_texas_build_normalises_with_provenance_on_every_record(tmp_path):
    """The recorded output of the fixture's build (real elements) -> asset frames: every row carries
    the provenance quartet, the element's own page, and the extract's timestamp as `retrieved_at`."""
    summary = op.write_asset_frames(RECORDED, tmp_path, licence=LICENCE)
    assert (summary["lines"], summary["substations"], summary["regions"]) == (5, 6, ["US-TX"])
    lines = pd.read_parquet(tmp_path / op.LINES_FILE)
    subs = pd.read_parquet(tmp_path / op.SUBSTATIONS_FILE)
    for frame in (lines, subs):
        assert set(frame["source_id"]) == {"us.osm.power"} and set(frame["licence"]) == {LICENCE}
        assert set(frame["retrieved_at"]) == {FIXTURE_TIMESTAMP}
        for sid, url in zip(frame["source_asset_id"], frame["source_url"], strict=True):
            assert url == f"https://www.openstreetmap.org/{sid}"
    by_id = {r["source_asset_id"]: r for r in lines.to_dict("records")}
    multi = by_id["way/1535302130"]
    assert multi["name"] == "CenterPoint Energy 138 kV line (OSM way 1535302130)"
    assert json.loads(multi["attributes"])["voltages_kv"] == [138.0, 69.0]
    assert json.loads(by_id["way/1343869767"]["attributes"])["voltage_unknown"] is True
    subs_by_id = {r["source_asset_id"]: r for r in subs.to_dict("records")}
    durazno = json.loads(subs_by_id["relation/21433644"]["attributes"])
    assert subs_by_id["relation/21433644"]["name"] == "Durazno Substation"
    assert durazno["outline"]["type"] == "Polygon" and durazno["location_method"] == "area_centroid"
    if op.STATES_GEOJSON.exists():
        # In the Texas extract (bounding box) but in Arkansas: the state comes from the polygon.
        assert subs_by_id["way/915732550"]["state_code"] == "US-AR"
        assert json.loads(subs_by_id["way/915732550"]["attributes"])["location_method"] == "vertex_mean"


def test_normalised_dir_follows_the_connector_data_root():
    from pipeline.connectors.store import DATA_DIR

    assert op.NORMALIZED_DIR == DATA_DIR / "normalized" / "context"


# ------------------------------------------------------------------------- OSM file (pyosmium)
def test_fixture_builds_the_expected_lines_and_substations(tmp_path):
    pytest.importorskip("osmium")
    manifest = op.build(
        [("US-TX", FIXTURE)],
        work_dir=tmp_path / "work",
        out_dir=tmp_path / "out",
        timestamp=FIXTURE_TIMESTAMP,
    )
    region = manifest["regions"][0]
    assert region["timestamp"] == FIXTURE_TIMESTAMP
    lines = {
        (r["osm_type"], r["osm_id"]): r for r in op.read_geoparquet(tmp_path / "out" / op.GEO_LINES_NAME)
    }
    subs = {
        (r["osm_type"], r["osm_id"]): r
        for r in op.read_geoparquet(tmp_path / "out" / op.GEO_SUBSTATIONS_NAME)
    }
    expected = json.loads((FIXTURE.parent / "osm_power_texas_sample.expected.json").read_text())
    assert sorted(f"{t}/{i}" for t, i in lines) == expected["lines"]
    assert sorted(f"{t}/{i}" for t, i in subs) == expected["substations"]
    assert region["dropped"] == expected["dropped"]
    for key, want in expected["line_checks"].items():
        osm_type, osm_id = key.split("/")
        row = lines[(osm_type, int(osm_id))]
        for field, value in want.items():
            assert row[field] == value, (key, field)
    for key, want in expected["substation_checks"].items():
        osm_type, osm_id = key.split("/")
        row = subs[(osm_type, int(osm_id))]
        for field, value in want.items():
            if field == "outline_type":
                outline = op.wkb_to_geojson(row["outline"])
                assert (outline or {}).get("type") == value, key
            else:
                assert row[field] == value, (key, field)
    assert not any((tmp_path / "work").iterdir())  # the filtered file is cleaned up
    # The recorded output the normaliser tests read is what this build still produces.
    for name in (op.GEO_LINES_NAME, op.GEO_SUBSTATIONS_NAME):
        assert op.read_geoparquet(tmp_path / "out" / name) == op.read_geoparquet(RECORDED / name), name


def test_a_file_without_a_timestamp_is_refused(tmp_path):
    pytest.importorskip("osmium")
    with pytest.raises(op.BuildError, match="timestamp"):
        op.extract_region(FIXTURE, "US-TX", work_dir=tmp_path)


def _osm_file(path: pathlib.Path, nodes: dict[int, tuple[float, float]], body: str) -> pathlib.Path:
    """A synthetic OSM XML file: `nodes` (id -> lon, lat) and the ways/relations in `body`."""
    node_xml = "\n".join(
        f'  <node id="{i}" version="1" lat="{lat}" lon="{lon}"/>' for i, (lon, lat) in sorted(nodes.items())
    )
    head = '<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="test">'
    path.write_text(f"{head}\n{node_xml}\n{body}\n</osm>\n", encoding="utf-8")
    return path


def test_a_two_extract_build_holds_a_border_way_once_and_a_double_mapped_substation_once(tmp_path):
    """Review 2026-10-22, through pyosmium: Oklahoma's extract (first in table order) and Texas's both
    hold way 9 across the Red River, and Oklahoma's holds a substation tagged on a closed way and on
    the multipolygon relation whose only outer is that way. One line, labelled by its geometry and in
    Texas's scope; one substation, the relation, with the name only the way states."""
    pytest.importorskip("osmium")
    _states()
    crossing = {1: (-97.5, 33.6), 2: (-97.5, 34.2), 3: (-97.5, 34.4)}
    way9 = (
        '  <way id="9" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/>'
        '<tag k="power" v="line"/><tag k="voltage" v="345000"/></way>'
    )
    square = {20: (-97.50, 35.00), 21: (-97.49, 35.00), 22: (-97.49, 35.01), 23: (-97.50, 35.01)}
    twin = (
        '  <way id="40" version="1"><nd ref="20"/><nd ref="21"/><nd ref="22"/><nd ref="23"/><nd ref="20"/>'
        '<tag k="power" v="substation"/><tag k="name" v="Twin"/></way>\n'
        '  <relation id="50" version="1"><member type="way" ref="40" role="outer"/>'
        '<tag k="type" v="multipolygon"/><tag k="power" v="substation"/>'
        '<tag k="substation" v="transmission"/><tag k="voltage" v="138000"/></relation>'
    )
    ok = _osm_file(tmp_path / "ok.osm", {**crossing, **square}, f"{way9}\n{twin}")
    tx = _osm_file(
        tmp_path / "tx.osm",
        {**crossing, 30: (-97.0, 31.0), 31: (-97.1, 31.1)},
        f'{way9}\n  <way id="11" version="1"><nd ref="30"/><nd ref="31"/>'
        '<tag k="power" v="line"/><tag k="voltage" v="138000"/></way>',
    )
    manifest = op.build(
        [("US-OK", ok), ("US-TX", tx)], work_dir=tmp_path / "work", out_dir=tmp_path / "out", timestamp=TS
    )
    lines = {r["osm_id"]: r for r in op.read_geoparquet(tmp_path / "out" / op.GEO_LINES_NAME)}
    subs = op.read_geoparquet(tmp_path / "out" / op.GEO_SUBSTATIONS_NAME)
    assert sorted(lines) == [9, 11]
    assert (lines[9]["region"], lines[9]["extract_regions"], lines[9]["scope_regions"]) == (
        "US-OK",
        ["US-OK", "US-TX"],
        ["US-OK", "US-TX"],
    )
    assert [(s["osm_type"], s["osm_id"], s["name"], s["substation"]) for s in subs] == [
        ("relation", 50, "Twin", "transmission")
    ]
    regions = {r["region"]: r for r in manifest["regions"]}
    assert regions["US-OK"]["notes"] == {"substation_way_folded_into_its_relation": 1}
    assert (regions["US-TX"]["lines_kept"], regions["US-TX"]["lines_read"]) == (2, 2)
    assert (regions["US-OK"]["lines_kept"], regions["US-TX"]["duplicates_skipped"]) == (1, 1)
    summary = op.write_asset_frames(tmp_path / "out", tmp_path / "frames", licence=LICENCE, regions=["US-TX"])
    assert (summary["lines"], summary["substations"]) == (2, 0)
    frame = pd.read_parquet(tmp_path / "frames" / op.LINES_FILE)
    assert sorted(frame["source_asset_id"]) == ["way/11", "way/9"]
