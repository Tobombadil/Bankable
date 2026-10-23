# ruff: noqa: S101 - asserts are the point (docs/04 E-6)
"""Every source parser on its recorded fixture (pipeline/context/prospect/fixtures/), offline."""

from __future__ import annotations

import json
import pathlib
import zipfile
from typing import Any

import pyarrow.parquet as pq  # type: ignore[import-untyped, unused-ignore]
import pytest

from pipeline.connectors.base import ParseError
from pipeline.context.prospect import (
    eia_oil_gas,
    eia_reserves,
    gwa_wind,
    netl_energy_communities,
    nrel_nsrdb,
    usgs_nshm,
    usgs_padus,
    wri_aqueduct,
)
from pipeline.context.prospect.arcgis import parse_features
from services.context_sample.geotiff import GeoTiff

FIX = pathlib.Path(__file__).parent / "fixtures"
PROVENANCE_COLUMNS = {"source_id", "source_url", "retrieved_at", "licence", "vintage"}


def _geojson(*parts: str) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = json.loads(FIX.joinpath(*parts).read_text())["features"]
    return features


# ------------------------------------------------------------------ solar
def test_nsrdb_layer_keeps_the_source_value_and_the_notice(tmp_path: pathlib.Path) -> None:
    out = nrel_nsrdb.build(region="texas", out_dir=tmp_path, snapshot_dir=FIX / "nrel_nsrdb")
    ghi = next(o for o in out if o["layer_id"] == "solar_ghi")
    assert ghi["provenance"]["notice"] == nrel_nsrdb.NOTICE
    assert ghi["provenance"]["citation"].startswith("Sengupta, M.")
    with GeoTiff(FIX / "nrel_nsrdb" / "nsrdb3_ghi.tif") as src, GeoTiff(tmp_path / "solar_ghi.tif") as built:
        for lon, lat in ((-103.5, 31.4), (-102.95, 30.95)):
            source = src.value_at(lon, lat)
            assert source is not None
            assert built.value_at(lon, lat) == pytest.approx(round(source, 3))
        assert built.metadata["notice"] == nrel_nsrdb.NOTICE
        assert built.metadata["source_id"] == "us.nrel.nsrdb.psm3_annual"


def test_the_nlr_notice_held_in_code_matches_the_quoted_terms() -> None:
    """The notice lives in code (data/sources.yaml `notice` would append 2.4 KB to every credit line;
    docs/13 §6), so it is pinned here to the terms quoted in the source's row."""
    from pipeline.connectors.registry import Registry

    def norm(text: str) -> str:
        return " ".join(text.split()).lower()

    quote = norm(Registry().get(nrel_nsrdb.SOURCE_ID).license)
    notice = norm(nrel_nsrdb.NOTICE)
    for phrase in nrel_nsrdb.TERMS_PHRASES:
        assert norm(phrase) in notice and norm(phrase) in quote
    quoted_part = nrel_nsrdb.NOTICE[: nrel_nsrdb.NOTICE.index("The user also understands")]
    assert norm(quoted_part) in quote


def test_nsrdb_extracts_only_the_annual_raster(tmp_path: pathlib.Path) -> None:
    z = tmp_path / "nsrdbv3_ghi.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("__MACOSX/nsrdbv3_ghi/Annual GHI/._nsrdb3_ghi.tif", b"junk")
        zf.writestr("nsrdbv3_ghi/Monthly GHI/ghi_jan.tif", b"monthly")
        zf.write(FIX / "nrel_nsrdb" / "nsrdb3_ghi.tif", "nsrdbv3_ghi/Annual GHI/nsrdb3_ghi.tif")
    got = nrel_nsrdb.extract_member(z, "Annual GHI/nsrdb3_ghi.tif", tmp_path / "out.tif")
    assert got.read_bytes() == (FIX / "nrel_nsrdb" / "nsrdb3_ghi.tif").read_bytes()


# ------------------------------------------------------------------ wind
def test_wind_is_manual_only() -> None:
    with pytest.raises(gwa_wind.ManualSourceOnly, match="forbid automated access"):
        gwa_wind.fetch()


def test_wind_builds_from_a_downloaded_file(tmp_path: pathlib.Path) -> None:
    tif = FIX / "global_wind_atlas" / "gwa_format_synthetic_amarillo.tif"
    out = gwa_wind.build(region="texas", out_dir=tmp_path, snapshot=tif, downloaded_at="2026-10-20T10:00:00Z")
    assert out[0]["provenance"]["retrieved_at"] == "2026-10-20T10:00:00Z"
    with GeoTiff(tmp_path / "wind_speed_100m.tif") as t:
        assert t.value_at(-101.85, 35.22) == pytest.approx(6.4)
        assert t.value_at(-101.9 + 4.5 * 0.0025, 35.25 - 3.5 * 0.0025) is None, "source nodata stays nodata"


# ------------------------------------------------------------------ oil and gas
def test_eia_basins_and_plays_normalise() -> None:
    rows, geoms = eia_oil_gas.normalise("oil_gas_basins", _geojson("eia_oil_gas", "oil_gas_basins.geojson"))
    assert sorted(r["name"] for r in rows) == ["PERMIAN", "WESTERN GULF"] and len(geoms) == 2
    rows, _ = eia_oil_gas.normalise("oil_gas_plays", _geojson("eia_oil_gas", "oil_gas_plays.geojson"))
    wolfcamp = next(r for r in rows if r["name"] == "Wolfcamp")
    assert wolfcamp["basin"] == "Permian" and wolfcamp["lithology"]


def test_eia_layers_carry_provenance_columns(tmp_path: pathlib.Path) -> None:
    out = eia_oil_gas.build(region="texas", out_dir=tmp_path, snapshot_dir=FIX / "eia_oil_gas")
    assert {o["layer_id"] for o in out} == {"oil_gas_basins", "oil_gas_plays"}
    table = pq.read_table(tmp_path / "oil_gas_plays.parquet")
    assert PROVENANCE_COLUMNS <= set(table.column_names)
    assert set(table.column("source_id").to_pylist()) == {"us.eia.atlas.shale_plays"}
    assert b"geo" in (table.schema.metadata or {})


def test_arcgis_error_body_raises() -> None:
    with pytest.raises(ParseError, match="ArcGIS error"):
        parse_features(b'{"error": {"code": 499, "message": "Token Required"}}')
    feats, exceeded = parse_features(
        {"type": "FeatureCollection", "features": [], "exceededTransferLimit": True}
    )
    assert feats == [] and exceeded


def test_reserves_parse_states_subdivisions_and_withheld_cells() -> None:
    rows = eia_reserves.table(
        eia_reserves.parse((FIX / "eia_reserves" / "ARR_2024_TABLES_6_8_sample.xlsx").read_bytes())
    )
    texas = next(r for r in rows if r["area"] == "Texas")
    assert texas["crude_oil_lease_condensate_mmbbl"] == 19644.0 and texas["natural_gas_wet_bcf"] == 138670.0
    assert texas["is_state_row"] and texas["report_year"] == 2024
    d8 = next(r for r in rows if r["area"] == "RRC District 8")
    assert d8["parent_state"] == "Texas" and not d8["is_state_row"]
    offshore = next(r for r in rows if r["area"] == "State Offshore" and r["parent_state"] == "Texas")
    assert offshore["crude_oil_lease_condensate_mmbbl"] is None and "no data reported" in offshore["notes"]
    us = next(r for r in rows if r["area"] == "U.S. Total")
    assert us["natural_gas_wet_bcf"] == 583942.0


def test_reserves_keep_rows_found_on_one_sheet_only() -> None:
    """Pennsylvania is on the gas sheet only (95,135 Bcf, about 16% of US proved gas, year-end 2024) and
    Nebraska on the crude sheet only: the table is the union of both sheets, in sheet order."""
    rows = eia_reserves.table(
        eia_reserves.parse((FIX / "eia_reserves" / "ARR_2024_TABLES_6_8_sample.xlsx").read_bytes())
    )
    pa_row = next(r for r in rows if r["area"] == "Pennsylvania")
    assert pa_row["natural_gas_wet_bcf"] == 95135.0 and pa_row["gas_proved_nonproducing_bcf"] == 25650.0
    assert pa_row["crude_oil_lease_condensate_mmbbl"] is None
    assert "no row on the crude oil sheet" in pa_row["notes"]
    assert pa_row["is_state_row"] and pa_row["row_kind"] == "state"
    ne = next(r for r in rows if r["area"] == "Nebraska")
    assert ne["crude_oil_lease_condensate_mmbbl"] == 8.0 and ne["natural_gas_wet_bcf"] is None
    assert "no row on the natural gas sheet" in ne["notes"]
    areas = [r["area"] for r in rows]
    assert areas.index("Oklahoma") < areas.index("Pennsylvania") < areas.index("Texas")
    assert len(areas) == len({(r["area"], r["parent_state"]) for r in rows}) == 14


def test_reserves_aggregate_rows_are_not_states() -> None:
    rows = eia_reserves.table(
        eia_reserves.parse((FIX / "eia_reserves" / "ARR_2024_TABLES_6_8_sample.xlsx").read_bytes())
    )
    kinds = {r["area"]: r["row_kind"] for r in rows if r["parent_state"] is None}
    assert kinds["Lower 48 States"] == kinds["U.S. Total"] == "total"
    assert kinds["Federal Offshore"] == "federal_offshore"
    assert kinds["Texas"] == kinds["Alaska"] == "state"
    assert not any(r["is_state_row"] for r in rows if r["row_kind"] != "state")
    assert eia_reserves.row_kind("Other States", None) == "other_states"
    assert eia_reserves.row_kind("Gulf of America (Western)", "Federal Offshore") == "federal_offshore"
    assert eia_reserves.row_kind("RRC District 8", "Texas") == "subdivision"


def test_reserves_state_layer_and_table(tmp_path: pathlib.Path) -> None:
    out = eia_reserves.build(
        region="texas", out_dir=tmp_path, snapshot=FIX / "eia_reserves" / "ARR_2024_TABLES_6_8_sample.xlsx"
    )
    states = pq.read_table(tmp_path / "oil_gas_reserves_state.parquet").column("state").to_pylist()
    assert set(states) == {"Texas", "New Mexico", "Oklahoma"}
    assert out[0]["summary"]["table_rows"] == 14
    assert (tmp_path / "oil_gas_reserves_by_state.csv").read_text().count("us.eia.proved_reserves") == 14
    # The polygons are Census's, not EIA's: the layer names both sources.
    prov = out[0]["provenance"]
    assert prov["source_id"] == "us.eia.proved_reserves"
    assert prov["geometry_source_id"] == "us.census.cartographic_boundaries"
    assert prov["geometry_source_url"].endswith("cb_2024_us_state_20m.zip")
    assert prov["geometry_licence"].startswith("Public domain")


def test_reserves_state_layer_for_conus_includes_gas_only_states(tmp_path: pathlib.Path) -> None:
    eia_reserves.build(
        region="conus", out_dir=tmp_path, snapshot=FIX / "eia_reserves" / "ARR_2024_TABLES_6_8_sample.xlsx"
    )
    table = pq.read_table(tmp_path / "oil_gas_reserves_state.parquet")
    states, gas = table.column("state").to_pylist(), table.column("natural_gas_wet_bcf").to_pylist()
    by_state = dict(zip(states, gas, strict=True))
    assert by_state["Pennsylvania"] == 95135.0
    assert "Lower 48 States" not in by_state and "U.S. Total" not in by_state


def test_reserves_refuse_a_changed_layout() -> None:
    with pytest.raises(ParseError):
        eia_reserves.parse(b"not a workbook")
    with pytest.raises(ParseError, match="State and"):
        eia_reserves.parse_sheet([("Table 6",), (None,)])


# ------------------------------------------------------------------ energy communities
def test_coal_closure_categories() -> None:
    rows, _ = netl_energy_communities.normalise_coal(_geojson("netl", "coal.geojson"))
    assert len(rows) == 6
    cats = {r["category"] for r in rows}
    assert "adjoins a closure tract" in cats
    assert all(r["dataset_as_of"] == "2024-06-07" and r["state"] == "Texas" for r in rows)


def test_ffe_areas_say_whether_they_are_energy_communities() -> None:
    rows, _ = netl_energy_communities.normalise_ffe(
        _geojson("netl", "ec.geojson"), _geojson("netl", "ffe_only.geojson")
    )
    by_county = {r["county"]: r for r in rows}
    assert by_county["Hidalgo County"]["is_energy_community"] is True
    assert by_county["Travis County"]["is_energy_community"] is False
    assert all(r["meets_ffe_threshold"] for r in rows)
    assert by_county["Travis County"]["statistical_area"] == "Austin-Round Rock, TX"


def test_energy_community_provenance_carries_the_taxpayer_line(tmp_path: pathlib.Path) -> None:
    out = netl_energy_communities.build(region="texas", out_dir=tmp_path, snapshot_dir=FIX / "netl")
    for o in out:
        assert o["provenance"]["taxpayer_line"] == netl_energy_communities.TAXPAYER_LINE
        assert "2024" in o["provenance"]["vintage"]


def test_ffe_rows_name_the_service_they_came_from(tmp_path: pathlib.Path) -> None:
    """The statistical-area layer joins two NETL services; each row's provenance `source_url` is its own."""
    out = netl_energy_communities.build(region="texas", out_dir=tmp_path, snapshot_dir=FIX / "netl")
    table = pq.read_table(tmp_path / "energy_community_ffe.parquet")
    by_county = dict(
        zip(table.column("county").to_pylist(), table.column("source_url").to_pylist(), strict=True)
    )
    assert by_county["Travis County"].endswith(
        "/2024_MSAs_NonMSAs_that_only_meet_the_FFE_Threshold/FeatureServer/0"
    )
    assert by_county["Hidalgo County"].endswith(
        "/2024_MSAs_NonMSAs_that_are_Energy_Communities/FeatureServer/0"
    )
    ffe = next(o for o in out if o["layer_id"] == "energy_community_ffe")
    assert ffe["provenance"]["source_urls"] == [
        f"{netl_energy_communities.SERVICES['ec']}/0",
        f"{netl_energy_communities.SERVICES['ffe_only']}/0",
    ]
    assert ffe["coverage"] == {
        "kind": "states",
        "states": ["Texas"],
        "label": "Texas",
        "region": "texas",
        "outline_source_id": "us.census.cartographic_boundaries",
        "outline_source_url": "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_state_20m.zip",
    }


def test_where_clause_quotes_state_names() -> None:
    assert netl_energy_communities._where("texas") == "state_name IN ('Texas')"
    assert netl_energy_communities._where("conus") == "1=1"


# ------------------------------------------------------------------ protected lands
def test_padus_gap_1_2_is_the_hard_constraint() -> None:
    feats = [
        f for f in _geojson("padus", "padus_tx_sample.geojson") if f["properties"]["feature_class"] == "Fee"
    ]
    rows, _ = usgs_padus.normalise(feats, "Fee")
    by_name = {r["unit_name"]: r for r in rows}
    assert by_name["Big Bend National Park"]["hard_constraint"] is True
    assert by_name["Padre Island National Seashore"]["gap_status"] == "2"
    assert by_name["Big Bend Ranch State Park"]["hard_constraint"] is False
    assert by_name["Big Bend National Park"]["manager"] == "National Park Service"


def test_padus_finds_fee_and_designation_layers_by_name() -> None:
    state = ["PADUS4_1Fee_State_TX", "PADUS4_1Designation_State_TX", "PADUS4_1Easement_State_TX"]
    assert usgs_padus.find_layers(state) == {
        "Fee": "PADUS4_1Fee_State_TX",
        "Designation": "PADUS4_1Designation_State_TX",
    }
    assert usgs_padus.find_layers(["PADUS4_1Fee", "PADUS4_1Designation"])["Fee"] == "PADUS4_1Fee"
    with pytest.raises(LookupError):
        usgs_padus.find_layers(["PADUS4_1Fee"])
    assert usgs_padus.file_url("texas").endswith("name=PADUS4_1_State_TX_GDB_KMZ.zip")
    assert usgs_padus.file_url("conus").endswith("name=PADUS4_1Geodatabase.zip")


# ------------------------------------------------------------------ water
def test_aqueduct_baseline_water_stress() -> None:
    rows, _ = wri_aqueduct.normalise(_geojson("wri_aqueduct", "aqueduct_tx_sample.geojson"))
    labels = {r["label"] for r in rows}
    assert labels == {"Medium - High (20-40%)", "Extremely High (>80%)"}
    assert all(isinstance(r["category"], int) and r["basin_id"] for r in rows)


# ------------------------------------------------------------------ seismic
def test_nshm_map_csv_and_grid() -> None:
    content = (FIX / "usgs_nshm" / "nshm_map_pga_pecos.csv").read_bytes()
    lons, lats, vals = usgs_nshm.parse_map_csv(content)
    assert len(lons) == 99
    grid = usgs_nshm.to_grid(lons, lats, vals)
    first = content.decode().splitlines()[1].split(",")
    lon0, lat0, v2475 = float(first[0]), float(first[1]), float(first[4])
    row = round((grid.north - 0.025 - lat0) / 0.05)
    col = round((lon0 - (grid.west + 0.025)) / 0.05)
    assert grid.data[row, col] == pytest.approx(v2475)
    with pytest.raises(ParseError):
        usgs_nshm.parse_map_csv(b"lon,lat,475.0\n-100,30,0.1\n")


def _zip(path: pathlib.Path, members: dict[str, bytes]) -> pathlib.Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_extract_gdb_writes_only_the_geodatabase_and_refuses_traversal(tmp_path: pathlib.Path) -> None:
    from pipeline.context.prospect.common import extract_gdb

    good = _zip(
        tmp_path / "good.zip",
        {
            "PADUS4_1_StateTX.gdb/a0000001.gdbtable": b"table",
            "PADUS4_1_StateTX.gdb/gdb": b"x",
            "PADUS4_1_StateTX.kmz": b"not extracted",
        },
    )
    out = tmp_path / "out"
    out.mkdir()
    gdb = extract_gdb(good, out)
    assert gdb == out / "PADUS4_1_StateTX.gdb"
    assert sorted(p.name for p in gdb.iterdir()) == ["a0000001.gdbtable", "gdb"]
    assert not (out / "PADUS4_1_StateTX.kmz").exists()

    evil = _zip(tmp_path / "evil.zip", {"x.gdb/ok": b"1", "x.gdb/../../escaped.txt": b"2"})
    with pytest.raises(ValueError, match="outside"):
        extract_gdb(evil, out)
    assert not (tmp_path / "escaped.txt").exists()
    with pytest.raises(FileNotFoundError):
        extract_gdb(_zip(tmp_path / "empty.zip", {"readme.txt": b"no geodatabase"}), out)


def test_padus_hands_the_region_box_to_gdal_in_the_layer_crs(tmp_path: pathlib.Path) -> None:
    """A CONUS build reads the national geodatabase: Alaska, Hawaii and the territories are filtered by
    GDAL in the layer's Albers CRS, so they are never decoded, reprojected or simplified in Python.
    Needs pyogrio and pyproj (installed in the Actions build only); skipped elsewhere."""
    pytest.importorskip("pyogrio")
    pytest.importorskip("pyproj")
    import numpy as np
    import pyogrio.raw  # type: ignore[import-not-found, import-untyped, unused-ignore]
    from pyproj import Transformer  # type: ignore[import-not-found, import-untyped, unused-ignore]

    from pipeline.context.prospect.common import REGIONS
    from pipeline.context.prospect.geoparquet import wkb_multipolygon

    albers = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)

    def square(lon: float, lat: float) -> bytes:
        ring = [(lon, lat), (lon + 0.1, lat), (lon + 0.1, lat + 0.1), (lon, lat + 0.1), (lon, lat)]
        xs, ys = albers.transform([p[0] for p in ring], [p[1] for p in ring])
        return wkb_multipolygon([[list(zip(xs, ys, strict=True))]])

    places = {"Austin": (-97.8, 30.2), "Anchorage": (-149.9, 61.2), "Honolulu": (-157.9, 21.3)}
    gpkg = tmp_path / "padus.gpkg"
    pyogrio.raw.write(
        str(gpkg),
        np.array([square(*p) for p in places.values()], dtype=object),
        [np.array(list(places), dtype=object), np.array(["1", "2", "3"], dtype=object)],
        ["Unit_Nm", "GAP_Sts"],
        layer="PADUS4_1Fee",
        driver="GPKG",
        geometry_type="MultiPolygon",
        crs="EPSG:5070",
    )
    everything: dict[str, int] = {}
    assert len(list(usgs_padus.read_gdb(gpkg, "PADUS4_1Fee", counts=everything))) == 3
    clipped: dict[str, int] = {}
    got = list(usgs_padus.read_gdb(gpkg, "PADUS4_1Fee", counts=clipped, bbox_lonlat=REGIONS["conus"]))
    assert [f["properties"]["Unit_Nm"] for f in got] == ["Austin"]
    assert clipped["features"] == 1, "the others were never decoded"
    lon, lat = got[0]["geometry"]["coordinates"][0][0][0]
    assert lon == pytest.approx(-97.8, abs=1e-6) and lat == pytest.approx(30.2, abs=1e-6)


def test_padus_recognises_curved_geometries_to_re_read() -> None:
    """GDAL's Arrow stream hands curved polygons through as MultiSurface/CurvePolygon (ISO or EWKB
    flagged); those are the features read_gdb re-reads linearised instead of dropping."""
    import struct

    def head(gtype: int, order: str = "<") -> bytes:
        return struct.pack(f"{order}BI", 1 if order == "<" else 0, gtype)

    curved = [head(12), head(10), head(1012), head(0x8000000C), head(12, ">")]
    assert all(usgs_padus._wkb_base_type(w) in usgs_padus.CURVED_WKB_TYPES for w in curved)
    assert usgs_padus._wkb_base_type(head(6)) not in usgs_padus.CURVED_WKB_TYPES
    assert usgs_padus._wkb_base_type(head(1006)) == 6
