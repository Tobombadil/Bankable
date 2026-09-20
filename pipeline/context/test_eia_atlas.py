"""Tests for `pipeline.context.eia_atlas`, `pipeline.context.shapefile` and `pipeline.context.geo`
against recorded fixtures under `pipeline/context/fixtures/` (no network).

The four ``*_sample.zip`` fixtures are byte-for-byte record subsets of the real EIA zips fetched on
2026-09-19 (`data/snapshots/us.eia.atlas.*/20260919T1534*.zip`): the `.shp`/`.dbf` records were
copied verbatim and the two header counts fixed. ``eia_atlas_gas_pipelines_sample.geojson`` is the
same rows in the shape an ArcGIS ``f=geojson`` answer has (``OBJECTID`` property, ``id`` on the
feature) -- derived, not recorded, because the feature service answered ``Token Required`` on
every attempt that day (module docstring)."""

from __future__ import annotations

import json
import pathlib

import pandas as pd
import pytest

from pipeline.connectors.base import ParseError
from pipeline.connectors.store import Store
from pipeline.context import eia_atlas, geo, shapefile

STATES_GEOJSON = eia_atlas.STATES_GEOJSON
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PIPELINES_ZIP = FIXTURES / "eia_atlas_gas_pipelines_sample.zip"
PIPELINES_GEOJSON = FIXTURES / "eia_atlas_gas_pipelines_sample.geojson"
PROCESSING_ZIP = FIXTURES / "eia_atlas_gas_processing_plants_sample.zip"
STORAGE_ZIP = FIXTURES / "eia_atlas_gas_storage_sample.zip"
LNG_ZIP = FIXTURES / "eia_atlas_lng_terminals_sample.zip"

PROV = eia_atlas.Provenance(
    source_id="test",
    source_url="https://example.org/x.zip",
    retrieved_at="2026-09-19T15:34:29Z",
    licence="pd",
)


# ------------------------------------------------------------------------------- shapefile
def test_read_zip_counts_and_geometry_types():
    feats = shapefile.read_zip(PIPELINES_ZIP.read_bytes())
    assert len(feats) == 119
    assert {f["geometry"]["type"] for f in feats} == {"MultiLineString"}
    assert set(feats[0]["properties"]) == {"TYPEPIPE", "Operator", "Status", "Shape_Leng"}
    points = shapefile.read_zip(LNG_ZIP.read_bytes())
    assert len(points) == 8
    assert points[0]["geometry"]["type"] == "Point"
    assert points[0]["properties"]["Facility"] == "Elba Island"
    assert isinstance(points[0]["properties"]["Stora_Bcf"], float)


def test_read_zip_rejects_non_shapefile_zip(tmp_path):
    import zipfile

    p = tmp_path / "x.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("readme.txt", "no shapes here")
    with pytest.raises(shapefile.ShapefileError):
        shapefile.read_zip(p.read_bytes())


def test_read_shp_rejects_bad_magic():
    with pytest.raises(shapefile.ShapefileError):
        shapefile.read_shp(b"\x00" * 120)


def test_dbf_field_decoding():
    rows = shapefile.read_zip(PROCESSING_ZIP.read_bytes())
    row = rows[0]["properties"]
    assert isinstance(row["Period"], int) and row["Period"] == 2017
    assert isinstance(row["Cap_MMcfd"], float)
    assert isinstance(row["Plant_Name"], str)


# ------------------------------------------------------------------------------------ geo
def test_haversine_one_degree_of_latitude():
    miles = geo.geodesic_length_miles([[[-100.0, 40.0], [-100.0, 41.0]]])
    assert 68.5 < miles < 69.5


def test_simplify_drops_collinear_points_and_zero_tolerance_is_identity():
    line = [[0.0, 0.0], [0.5, 0.5], [1.0, 1.0], [2.0, 1.0]]
    assert geo.simplify(line, 0.0) == line
    assert geo.simplify(line, 0.01) == [[0.0, 0.0], [1.0, 1.0], [2.0, 1.0]]


def test_representative_point_is_a_vertex_of_the_longest_part():
    short = [[0.0, 0.0], [0.0, 0.1]]
    long_ = [[10.0, 10.0], [10.0, 11.0], [10.0, 12.0], [10.0, 13.0]]
    rep = geo.representative_point([short, long_])
    assert rep in long_
    assert rep == [10.0, 11.0] or rep == [10.0, 12.0]


def test_wkt_writers():
    assert geo.wkt_point([-104.280599, 40.988289]) == "POINT(-104.280599 40.988289)"
    wkt = geo.wkt_multilinestring([[[-1.0, 2.0], [-1.5, 2.5]], [[3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]])
    assert wkt == "MULTILINESTRING((-1 2, -1.5 2.5), (5 6, 7 8))"
    assert geo.wkt_multilinestring([]) == "MULTILINESTRING EMPTY"


def test_state_index_point_in_polygon_with_hole():
    square = {
        "type": "Feature",
        "properties": {"region_id": "US-SQ"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
                [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
            ],
        },
    }
    idx = geo.StateIndex([square])
    assert idx.lookup(1.0, 1.0) == "US-SQ"
    assert idx.lookup(5.0, 5.0) is None  # inside the hole
    assert idx.lookup(11.0, 1.0) is None
    assert idx.states_for([[[1, 1], [2, 2]], [[20, 20]]]) == ["US-SQ"]


# ------------------------------------------------------------------- geo.merge_touching_lines
def test_merge_chains_touching_parts_and_reverses_where_needed():
    # b is stored back-to-front; joining it needs the reversal, and the joint appears once.
    a = [[0.0, 0.0], [1.0, 0.0]]
    b = [[2.0, 0.0], [1.0, 0.0]]
    assert geo.merge_touching_lines([a, b]) == [[[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]]]
    # and the same chain however the two parts are ordered on input
    assert geo.merge_touching_lines([b, a]) == [[[2.0, 0.0], [1.0, 0.0], [0.0, 0.0]]]


def test_merge_joins_only_at_the_stored_precision():
    # 1e-7 apart: below the 6-decimal precision the WKT writer stores, so they are one node
    near = geo.merge_touching_lines([[[0.0, 0.0], [1.0, 0.0]], [[1.0000001, 0.0], [2.0, 0.0]]])
    assert len(near) == 1
    # 1e-5 apart: a real gap at 6 decimals, so the source did not join them and neither do we
    far = geo.merge_touching_lines([[[0.0, 0.0], [1.0, 0.0]], [[1.00001, 0.0], [2.0, 0.0]]])
    assert len(far) == 2


def test_merge_stops_at_a_three_way_junction_rather_than_inventing_topology():
    # Three parts meet at (1, 0). Welding two of them would assert a continuous run the source
    # does not describe, so every chain stops at the junction and all three parts survive.
    parts = [
        [[0.0, 0.0], [1.0, 0.0]],
        [[1.0, 0.0], [2.0, 0.0]],
        [[1.0, 0.0], [1.0, 1.0]],
    ]
    merged = geo.merge_touching_lines(parts)
    assert len(merged) == 3
    assert merged == [[[0.0, 0.0], [1.0, 0.0]], [[1.0, 0.0], [2.0, 0.0]], [[1.0, 0.0], [1.0, 1.0]]]
    # a tail hanging off one branch still chains up to the junction and no further
    tailed = geo.merge_touching_lines([*parts, [[2.0, 0.0], [3.0, 0.0]]])
    assert len(tailed) == 3
    assert [[1.0, 0.0], [2.0, 0.0], [3.0, 0.0]] in tailed


def test_merge_handles_rings_and_parts_too_short_to_chain():
    ring = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
    stub = [[5.0, 5.0]]
    empty: list[list[float]] = []
    merged = geo.merge_touching_lines([ring, stub, empty])
    assert merged == [ring, stub, empty]  # unchanged, and in input order
    # a ring keeps both of its endpoint slots, so a third part touching it is a junction
    with_spur = geo.merge_touching_lines([ring, [[0.0, 0.0], [-1.0, 0.0]]])
    assert len(with_spur) == 2
    # four parts that close into a loop become one closed chain, not four
    loop = geo.merge_touching_lines(
        [
            [[0.0, 0.0], [1.0, 0.0]],
            [[1.0, 0.0], [1.0, 1.0]],
            [[1.0, 1.0], [0.0, 1.0]],
            [[0.0, 1.0], [0.0, 0.0]],
        ]
    )
    assert len(loop) == 1
    assert loop[0][0] == loop[0][-1] and len(loop[0]) == 5


def _fixture_pipeline_lines() -> list[list[list[float]]]:
    """Every real line part in the recorded EIA shapefile fixture, ungrouped."""
    return [
        [[float(c[0]), float(c[1])] for c in ln]
        for f in shapefile.read_zip(PIPELINES_ZIP.read_bytes())
        for ln in eia_atlas._lines(f)
        if len(ln) >= 2
    ]


def test_merge_is_idempotent_and_length_preserving_on_the_recorded_layer():
    lines = _fixture_pipeline_lines()
    merged = geo.merge_touching_lines(lines)
    assert len(merged) < len(lines)  # the fixture really does contain touching segments

    # idempotent: merging the merged set changes nothing at all
    assert geo.merge_touching_lines(merged) == merged
    # deterministic: same input, same output, so the parquet is reproducible
    assert geo.merge_touching_lines(lines) == merged

    # length-preserving: the only vertices removed are the duplicated joints
    before = geo.geodesic_length_miles(lines)
    after = geo.geodesic_length_miles(merged)
    assert abs(before - after) < 1e-6, f"{before} != {after}"

    # and no vertex is invented or lost, only de-duplicated
    seen_before = {(round(c[0], 6), round(c[1], 6)) for ln in lines for c in ln}
    seen_after = {(round(c[0], 6), round(c[1], 6)) for ln in merged for c in ln}
    assert seen_before == seen_after


#: The full 259-row layer is only present after a local connector run (`data/normalized/` is
#: gitignored), so the whole-layer assertion is opt-in; the fixture test above covers CI.
FULL_PIPELINES_PARQUET = (
    pathlib.Path(__file__).resolve().parents[2]
    / "data"
    / "normalized"
    / "context"
    / "us.eia.atlas.gas_pipelines.parquet"
)


def _read_multilinestring(wkt: str) -> list[list[list[float]]]:
    """Read back what `geo.wkt_multilinestring` writes. Local on purpose: `pipeline` may not
    import `services.api` (`infra/importlinter.ini`), and the format here is fixed by the writer
    in the module under test."""
    if wkt.endswith("EMPTY"):
        return []
    inner = wkt[wkt.index("(") + 1 : wkt.rindex(")")]
    parts = []
    for chunk in inner.split("),"):
        pairs = chunk.strip().lstrip("(").rstrip(")").split(",")
        parts.append([[float(v) for v in pair.split()] for pair in pairs if pair.strip()])
    return parts


@pytest.mark.skipif(not FULL_PIPELINES_PARQUET.exists(), reason="full layer not normalised locally")
def test_merge_is_idempotent_and_length_preserving_on_the_full_layer():
    df = pd.read_parquet(FULL_PIPELINES_PARQUET)
    assert len(df) == 259
    worst = 0.0
    for wkt in df["geom_line_wkt"]:
        parts = _read_multilinestring(wkt)
        merged = geo.merge_touching_lines(parts)
        assert geo.merge_touching_lines(merged) == merged
        worst = max(worst, abs(geo.geodesic_length_miles(parts) - geo.geodesic_length_miles(merged)))
    assert worst < 1e-6, worst


# ----------------------------------------------------------------------------------- parse
def test_parse_accepts_zip_and_geojson_and_rejects_garbage():
    assert len(eia_atlas.parse(PIPELINES_ZIP.read_bytes())) == 119
    assert len(eia_atlas.parse(PIPELINES_GEOJSON.read_bytes())) == 48
    with pytest.raises(ParseError):
        eia_atlas.parse(b"<html>not data</html>")
    with pytest.raises(ParseError):
        eia_atlas.parse(json.dumps({"type": "Feature"}).encode())


def test_source_vintage_from_zip_member_names():
    assert eia_atlas.source_vintage(PIPELINES_ZIP.read_bytes()) == "202001"
    assert eia_atlas.source_vintage(PROCESSING_ZIP.read_bytes()) == "2017_v2"
    assert eia_atlas.source_vintage(STORAGE_ZIP.read_bytes()) == "202012"
    assert eia_atlas.source_vintage(PIPELINES_GEOJSON.read_bytes()) is None


# ------------------------------------------------------------------------------- pipelines
def _pipelines() -> pd.DataFrame:
    prov = eia_atlas.Provenance(**{**PROV.__dict__, "vintage": "202001"})
    return eia_atlas.normalise_pipelines(eia_atlas.parse(PIPELINES_ZIP.read_bytes()), prov)


def test_pipelines_dissolve_by_operator_and_type():
    df = _pipelines()
    assert list(df.columns) == eia_atlas.ASSET_COLUMNS
    assert df["source_asset_id"].is_unique
    rex = df[df["name"] == "Rockies Express Pipeline"].iloc[0]
    assert rex["source_asset_id"] == "rockies-express-pipeline-interstate"
    assert rex["technology"] == "interstate" and rex["technology_raw"] == "Interstate"
    assert rex["status"] == "operating"
    assert rex["attributes"]["segment_count"] == 49
    assert rex["unit_count"] == 49
    assert 1300 < rex["attributes"]["miles"] < 1340
    assert rex["attributes"]["status_raw"] == ["Operating"]
    assert rex["attributes"]["source_vintage"] == "202001"
    assert rex["geom_line_wkt"].startswith("MULTILINESTRING((")
    # 49 source segments chain into 18 stored parts (test_pipelines_merges_touching_parts)
    assert rex["geom_line_wkt"].count("(") == 18 + 1
    assert rex["attributes"]["part_count"] == 18
    assert rex["capacity_value"] is None and rex["capacity_unit"] is None
    assert rex["operator_name"] == "Rockies Express Pipeline" and rex["owner_name"] is None
    assert rex["source_url"] == PROV.source_url and rex["retrieved_at"] == PROV.retrieved_at
    # the operator string is the only label the layer has, so it is the name
    assert set(df.loc[df["technology"] == "interstate", "name"]) >= {
        "Tallgrass Interstate Gas Transmission",
        "Trailblazer Pipeline Co",
        "Ruby Pipeline LLC",
        "Rockies Express (Entrega)",
        "Rockies Express (Echo Springs Lateral)",
    }
    assert set(df["technology"]) == {"interstate", "intrastate", "gathering"}


def test_pipelines_representative_point_lies_on_the_line():
    df = _pipelines()
    row = df[df["name"] == "Trailblazer Pipeline Co"].iloc[0]
    assert f"{row['lon']:g} {row['lat']:g}".split()[0][:6] in row["geom_line_wkt"]
    assert row["lon"] is not None and row["lat"] is not None


def test_pipelines_degenerate_segments_are_dropped_and_counted():
    df = _pipelines()
    assert df.attrs["dropped_no_geometry"] == 3  # the three single-vertex records kept in the fixture


def test_pipelines_states_crossed_with_a_state_index():
    states = geo.StateIndex(
        [
            {
                "type": "Feature",
                "properties": {"region_id": "US-WEST"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-125, 30], [-100, 30], [-100, 50], [-125, 50], [-125, 30]]],
                },
            }
        ]
    )
    prov = eia_atlas.Provenance(**PROV.__dict__)
    df = eia_atlas.normalise_pipelines(eia_atlas.parse(PIPELINES_ZIP.read_bytes()), prov, states=states)
    entrega = df[df["name"] == "Rockies Express (Entrega)"].iloc[0]
    assert entrega["attributes"]["states_crossed"] == ["US-WEST"]
    assert entrega["state_code"] == "US-WEST"
    rex = df[df["name"] == "Rockies Express Pipeline"].iloc[0]
    assert rex["state_code"] is None  # REX's longest part is east of -100


def test_pipelines_geojson_and_shapefile_paths_agree():
    from_zip = _pipelines().set_index("source_asset_id")
    from_geojson = eia_atlas.normalise_pipelines(eia_atlas.parse(PIPELINES_GEOJSON.read_bytes()), PROV)
    tb = from_geojson.set_index("source_asset_id").loc["trailblazer-pipeline-co-interstate"]
    assert (
        tb["attributes"]["segment_count"]
        == from_zip.loc["trailblazer-pipeline-co-interstate", "attributes"]["segment_count"]
    )
    assert tb["geom_line_wkt"] == from_zip.loc["trailblazer-pipeline-co-interstate", "geom_line_wkt"]


def test_pipelines_merges_touching_parts():
    """Regression: the segments in the recorded layer touch, so `normalise_pipelines` must chain
    them before writing WKT. Removing the `merge_touching_lines` call fails every assert here."""
    df = _pipelines().set_index("source_asset_id")
    assert df.attrs["parts_before_merge"] == 302
    assert df.attrs["parts_after_merge"] == 231
    # per row: the stored part count is the chained one, not the segment count
    rex = df.loc["rockies-express-pipeline-interstate"]
    assert rex["attributes"]["segment_count"] == 49  # what the source shipped
    assert rex["attributes"]["part_count"] == 18  # what we store
    assert rex["geom_line_wkt"].count("(") == rex["attributes"]["part_count"] + 1
    trailblazer = df.loc["trailblazer-pipeline-co-interstate"]
    assert trailblazer["attributes"]["segment_count"] == 39
    assert trailblazer["attributes"]["part_count"] == 7
    # every row is at or below its segment count, and the layer as a whole is well below
    counts = df["attributes"].map(lambda a: (a["part_count"], a["segment_count"]))
    assert all(parts <= segs or segs == 1 for parts, segs in counts)
    assert df.attrs["parts_after_merge"] < df.attrs["parts_before_merge"]


def test_pipelines_merge_changes_no_measurement(monkeypatch):
    """Chaining removes duplicated joints only, so mileage, segment counts and the states the
    line crosses are identical to what normalising without the merge produces."""
    feats = eia_atlas.parse(PIPELINES_ZIP.read_bytes())
    prov = eia_atlas.Provenance(**PROV.__dict__)
    states = geo.StateIndex.from_file(STATES_GEOJSON) if STATES_GEOJSON.exists() else None
    merged = eia_atlas.normalise_pipelines(feats, prov, states=states).set_index("source_asset_id")

    def _identity(lines, **_kwargs):
        return [[[float(c[0]), float(c[1])] for c in ln] for ln in lines]

    monkeypatch.setattr(eia_atlas, "merge_touching_lines", _identity)
    plain = eia_atlas.normalise_pipelines(feats, prov, states=states).set_index("source_asset_id")

    assert plain.attrs["parts_after_merge"] == plain.attrs["parts_before_merge"] == 302
    assert list(merged.index) == list(plain.index)
    for key in merged.index:
        a, b = merged.loc[key, "attributes"], plain.loc[key, "attributes"]
        assert a["miles"] == b["miles"]
        assert a["segment_count"] == b["segment_count"]
        assert a["states_crossed"] == b["states_crossed"]
        assert a["part_count"] <= b["part_count"]


def test_pipelines_simplify_reduces_vertices():
    prov = eia_atlas.Provenance(**PROV.__dict__)
    feats = eia_atlas.parse(PIPELINES_ZIP.read_bytes())
    full = eia_atlas.normalise_pipelines(feats, prov)
    thin = eia_atlas.normalise_pipelines(feats, prov, simplify_deg=0.01)
    assert thin["geom_line_wkt"].str.len().sum() < full["geom_line_wkt"].str.len().sum()


# --------------------------------------------------------------------------- point layers
def test_processing_plants():
    df = eia_atlas.normalise_processing_plants(eia_atlas.parse(PROCESSING_ZIP.read_bytes()), PROV)
    assert len(df) == 10
    assert df["source_asset_id"].is_unique  # includes the two real "Wheeler / TX / Wheeler" rows
    assert (df["name"] == "Wheeler").sum() == 2
    casper = df[df["name"] == "Casper Plant"].iloc[0]
    assert casper["operator_name"] is None and casper["owner_name"] == "Tallgrass Energy Midstream LLC"
    assert casper["capacity_value"] == 65.0 and casper["capacity_unit"] == "MMcf/d"
    assert casper["state_code"] == "US-WY" and casper["county_name"] == "Natrona"
    assert casper["attributes"]["period"] == 2017
    assert casper["attributes"]["btu_content"] == 1145.0
    assert casper["status"] == "operating" and casper["geom_line_wkt"] is None
    assert -111 < casper["lon"] < -104 and 41 < casper["lat"] < 45


def test_storage_fields():
    df = eia_atlas.normalise_storage(eia_atlas.parse(STORAGE_ZIP.read_bytes()), PROV)
    assert len(df) == 9
    assert df["source_asset_id"].is_unique
    huntsman = df[df["name"] == "Huntsman"].iloc[0]
    assert huntsman["source_asset_id"] == "340291_3"
    assert huntsman["operator_name"] == "TALLGRASS INTERSTATE GAS TRANSMISSIO"
    assert huntsman["technology"] == "depleted_field" and huntsman["technology_raw"] == "Depleted Field"
    assert huntsman["capacity_value"] == 12653122.0 and huntsman["capacity_unit"] == "Mcf"
    assert huntsman["attributes"]["max_deliverability_mcfd"] > 0
    assert huntsman["state_code"] == "US-NE" and huntsman["status"] == "operating"
    assert set(df["status"]) == {"operating", "standby"}
    assert (df["status"] == "standby").sum() == 2
    assert (
        df.loc[df["status"] == "standby", "attributes"].map(lambda a: a["status_raw"]) == "Inactive"
    ).all()


def test_lng_terminals():
    df = eia_atlas.normalise_lng_terminals(eia_atlas.parse(LNG_ZIP.read_bytes()), PROV)
    assert len(df) == 8 and df["source_asset_id"].is_unique
    by = df.set_index("name")
    assert by.loc["Sabine Pass", "capacity_value"] == 3.0  # liquefaction, an export terminal
    assert by.loc["Everett", "capacity_value"] == 0.7  # regasification, an import terminal
    assert by.loc["Sabine Pass", "capacity_unit"] == "Bcf/d"
    assert by.loc["Elba Island", "technology"] == "import_export"
    assert by.loc["Elba Island", "owner_name"] == "Kinder Morgan"
    assert by.loc["Elba Island", "operator_name"] == "Southern LNG, Inc."
    assert by.loc["Elba Island", "state_code"] == "US-GA"  # full state name mapped
    assert by.loc["Northeast Gateway", "state_code"] is None  # offshore, blank in the source
    assert by.loc["Cameron LNG", "attributes"]["regasification_bcfd"] is None  # "-" in the source


# ----------------------------------------------------------------------------------- fetch
class _Resp:
    def __init__(self, status: int, content: bytes, content_type: str) -> None:
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": content_type, "Last-Modified": "Mon, 27 Apr 2020 19:12:02 GMT"}
        self.text = content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)


class _FakeSession:
    """Answers the ArcGIS query with the real 2026-09-19 error body, then the zip."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.requests_made = 0

    def get(self, url: str, *, honour_robots: bool = True, **_: object) -> _Resp:
        self.calls.append((url, honour_robots))
        self.requests_made += 1
        if "/FeatureServer/0/query" in url:
            body = (
                b'{"error":{"code":499,"message":"Token Required","messageCode":"GWM_0003",'
                b'"details":["Token Required"]}}'
            )
            return _Resp(200, body, "text/plain;charset=utf-8")
        return _Resp(200, PIPELINES_ZIP.read_bytes(), "application/x-zip-compressed")


def test_fetch_falls_back_to_eia_zip_and_records_both_urls(tmp_path):
    layer = eia_atlas.LAYERS["gas_pipelines"]
    session = _FakeSession()
    result = eia_atlas.fetch(layer, session=session, store=Store(tmp_path), write=True)  # type: ignore[arg-type]
    assert result.format == "shapefile_zip"
    assert result.fetched_url == layer.download_url
    assert "Token Required" in result.arcgis_result
    assert result.arcgis_query_url.startswith(layer.feature_layer_url + "/query?")
    assert session.calls[0][1] is False  # API endpoint: robots not consulted
    assert session.calls[1][1] is True  # eia.gov file: robots honoured
    assert result.snapshot_path is not None and result.snapshot_path.suffix == ".zip"
    record = json.loads(result.run_path.read_text())
    assert record["arcgis"]["result"] == result.arcgis_result
    assert record["snapshot"]["fetched_url"] == layer.download_url
    assert record["snapshot"]["bytes"] == len(PIPELINES_ZIP.read_bytes())


def test_fetch_uses_geojson_when_the_service_answers(tmp_path):
    layer = eia_atlas.LAYERS["lng_terminals"]
    feats = shapefile.read_zip(LNG_ZIP.read_bytes())

    class _GeoSession(_FakeSession):
        def get(self, url: str, *, honour_robots: bool = True, **_: object) -> _Resp:
            self.calls.append((url, honour_robots))
            body = json.dumps({"type": "FeatureCollection", "features": feats}).encode()
            return _Resp(200, body, "application/geo+json")

    result = eia_atlas.fetch(layer, session=_GeoSession(), store=Store(tmp_path), write=False)  # type: ignore[arg-type]
    assert result.format == "geojson" and result.arcgis_result == "ok"
    assert len(eia_atlas.parse(result.content)) == 8


# ------------------------------------------------------------------------------------- CLI
def test_run_layer_from_snapshot_writes_parquet(tmp_path):
    out = tmp_path / "pipelines.parquet"
    summary = eia_atlas.run_layer(
        eia_atlas.LAYERS["gas_pipelines"],
        snapshot=PIPELINES_ZIP,
        out=out,
        with_states=False,
        store=Store(tmp_path),
    )
    assert summary["features"] == 119 and summary["rows"] == 9
    assert summary["dropped_no_geometry"] == 3
    assert summary["source_vintage"] == "202001"
    df = pd.read_parquet(out)
    assert len(df) == 9
    assert json.loads(df.iloc[0]["attributes"])["segment_count"] >= 1
    assert df["source_id"].unique().tolist() == ["us.eia.atlas.gas_pipelines"]
    assert df["licence"].iloc[0].startswith("US federal work")


def test_main_rejects_out_with_all_layers(capsys):
    with pytest.raises(SystemExit):
        eia_atlas.main(["--layer", "all", "--out", "x.parquet"])
