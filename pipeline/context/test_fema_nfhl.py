"""FEMA NFHL flood layer (pipeline/context/fema_nfhl.py; docs/25 section 5). No network: FEMA's answers
are recorded fixtures under fixtures/fema_nfhl/, and HTTP range reads are served from memory."""

from __future__ import annotations

import io
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

shapely = pytest.importorskip("shapely")

from pipeline.connectors.http import HttpBlocked, HttpFailed  # noqa: E402
from pipeline.context import fema_nfhl as nfhl  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "fema_nfhl"
CATALOG = FIXTURES / "catalog_nfhl_48_20261013"
COUNTY_ZIP = FIXTURES / "county_48441C_sample.zip"
COUNTY_URL = "https://hazards.fema.gov/nfhlv2/output/County/48441C_20260611.zip"
#: Centre of the fixture's window: an LBNL line crosses Zone A there, 4 m from a FIRM panel seam.
WINDOW_CENTRE = (-99.78286, 32.34379)
WORKFLOW = ROOT / ".github" / "workflows" / "flood-layer.yml"
SCRIPT = ROOT / "infra" / "scripts" / "build_flood_layer.sh"
PROV = nfhl.Provenance(
    "us.fema.nfhl", "https://hazards.fema.gov/x.zip", "2026-10-22T00:00:00Z", "PD", "NFHL_X"
)


# ------------------------------------------------------------------------------------- helpers
class FakeResponse:
    def __init__(self, status: int, content: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    def close(self) -> None:
        pass


class RangeServer:
    """Serves one in-memory file the way hazards.fema.gov does: HEAD with Accept-Ranges and an
    ETag, GET with Range -> 206 and Content-Range. Records every request."""

    def __init__(self, data: bytes, *, etag: str = '"v1"', ranges: bool = True) -> None:
        self.data = data
        self.etag = etag
        self.ranges = ranges
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.requests_made = 0

    def allowed_by_robots(self, url: str) -> bool:
        return True

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, dict(kwargs.get("headers") or {})))
        headers = {"Content-Length": str(len(self.data)), "Accept-Ranges": "bytes" if self.ranges else "none"}
        headers["ETag"] = self.etag
        if method == "HEAD":
            return FakeResponse(200, b"", headers)
        rng = (kwargs.get("headers") or {}).get("Range")
        if_range = (kwargs.get("headers") or {}).get("If-Range")
        if not rng or not self.ranges or (if_range and if_range != self.etag):
            return FakeResponse(200, self.data, headers)
        start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
        body = self.data[start : end + 1]
        return FakeResponse(206, body, {**headers, "Content-Range": f"bytes {start}-{end}/{len(self.data)}"})

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        kwargs.pop("honour_robots", None)
        return self.request("GET", url, **kwargs)


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _box(x0: float, y0: float, x1: float, y1: float) -> Any:
    return shapely.box(x0, y0, x1, y1)


def _panel(
    name: str, geom: Any, typ: str = "Countywide, Panel Printed", eff: Any = "2019-11-15"
) -> nfhl.Feature:
    return (
        {"FIRM_PAN": name, "PANEL_TYP": typ, "EFF_DATE": eff, "DFIRM_ID": "48999C", "VERSION_ID": "1.1.1.0"},
        geom,
    )


def _zone(fid: str, geom: Any, zone: str = "AE", sub: str | None = None, sfha: str = "T") -> nfhl.Feature:
    return (
        {"FLD_AR_ID": fid, "FLD_ZONE": zone, "ZONE_SUBTY": sub, "SFHA_TF": sfha, "DFIRM_ID": "48999C"},
        geom,
    )


def _metres(deg: float) -> float:
    return deg * nfhl.M_PER_DEG


# ------------------------------------------------------------------------------- route policy
@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://hazards.fema.gov/nfhlv2/output/State/NFHL_48_20261013.zip", True),
        ("https://hazards.fema.gov/nfhlv2/output/County/48201C_20260517.zip", True),
        ("https://msc.fema.gov/portal/advanceSearch", True),
        # robots.txt (2026-10-22): `Disallow: /*?*`, `/arcgis`, `/portal/downloadProduct?productID=`
        ("https://msc.fema.gov/portal/downloadProduct?productID=NFHL_48201C", False),
        ("https://msc.fema.gov/portal/advanceSearch?getCounty=48", False),
        ("https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query", False),
        ("https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer?f=json", False),
        ("https://data.openei.org/files/8742/x.csv?y=1", True),  # not a FEMA host: not judged here
    ],
)
def test_only_urls_fema_robots_allow_are_fetched(url: str, allowed: bool) -> None:
    assert nfhl.fema_url_allowed(url) is allowed


def test_a_disallowed_url_is_refused_before_any_request() -> None:
    server = RangeServer(b"x")
    with pytest.raises(HttpBlocked):
        nfhl.open_remote_zip("https://msc.fema.gov/portal/downloadProduct?productID=NFHL_48201C", server)  # type: ignore[arg-type]
    assert server.calls == []


# ------------------------------------------------------------------------- product discovery
def test_the_recorded_msc_answer_names_the_texas_state_file() -> None:
    payload = json.loads((FIXTURES / "msc_search_tx_480298.json").read_text())
    [product] = nfhl.parse_state_products(payload, "TX", "48")
    assert product.name == "NFHL_48_20261013"
    assert product.file == "NFHL_48_20261013.zip"
    assert product.url == "https://hazards.fema.gov/nfhlv2/output/State/NFHL_48_20261013.zip"
    assert product.size_label == "2031MB"
    assert product.latest_study_effective == "2026-08-04"
    assert product.latest_lomr_effective == "2026-10-14"
    assert product.build_date == "2026-10-13"
    assert nfhl.fema_url_allowed(product.url)


def test_a_state_file_for_another_state_or_an_odd_name_is_refused() -> None:
    payload = json.loads((FIXTURES / "msc_search_tx_480298.json").read_text())
    with pytest.raises(ValueError, match="unexpected NFHL state file"):
        nfhl.parse_state_products(payload, "OK", "40")
    payload["EFFECTIVE"]["NFHL_STATE_DATA"][0]["product_FILE_PATH"] = "../../etc/passwd"
    with pytest.raises(ValueError):
        nfhl.parse_state_products(payload, "TX", "48")
    with pytest.raises(ValueError, match="no EFFECTIVE"):
        nfhl.parse_state_products({"error": "x"}, "TX", "48")


def test_the_search_form_is_the_one_the_msc_page_posts() -> None:
    form = nfhl.search_form(nfhl.STATE_PROBES["TX"])
    assert form["method"] == "search" and form["selstate"] == "48"
    assert form["selcommunity"] == form["searchedCid"] == nfhl.STATE_PROBES["TX"].cid
    assert all(p.state_fips == p.county[:2] for p in nfhl.STATE_PROBES.values())


def test_product_names_given_by_hand() -> None:
    state = nfhl.product_from_file("TX", "NFHL_48_20261013.zip")
    assert state.url.endswith("/State/NFHL_48_20261013.zip")
    county = nfhl.product_from_file("TX", "48201C_20260517.zip")
    assert county.url == "https://hazards.fema.gov/nfhlv2/output/County/48201C_20260517.zip"
    with pytest.raises(ValueError):
        nfhl.product_from_file("TX", "NFHL_48201C")


# --------------------------------------------------------------------------- zip by range
def test_only_the_named_members_are_fetched_by_range(tmp_path: pathlib.Path) -> None:
    big = os.urandom(3_000_000)  # incompressible: a member we must not download
    data = _zip({"S_FLD_HAZ_LN.shp": big, "S_FLD_HAZ_AR.shp": b"zones" * 1000, "S_FIRM_PAN.dbf": b"p" * 10})
    server = RangeServer(data)
    remote = nfhl.open_remote_zip("https://hazards.fema.gov/nfhlv2/output/County/1.zip", server)  # type: ignore[arg-type]
    names = nfhl.shapefile_members(
        [i.filename for i in remote.zip.infolist()], ["S_FLD_HAZ_AR", "S_FIRM_PAN"]
    )
    assert names == ["S_FLD_HAZ_AR.shp", "S_FIRM_PAN.dbf"]
    nfhl.extract_members(remote.zip, names, tmp_path)
    assert (tmp_path / "S_FLD_HAZ_AR.shp").read_bytes() == b"zones" * 1000
    assert remote.reader.bytes_fetched < len(big) // 2
    # Every GET carried a Range and the ETag pin.
    gets = [h for m, h in server.calls if m == "GET"]
    assert gets and all(h.get("Range", "").startswith("bytes=") and h.get("If-Range") == '"v1"' for h in gets)


def test_extracted_members_are_byte_identical(tmp_path: pathlib.Path) -> None:
    members = {"a/S_FIRM_PAN.shp": os.urandom(5000), "a/S_FIRM_PAN.dbf": b"dbf" * 100}
    server = RangeServer(_zip(members))
    remote = nfhl.open_remote_zip("https://hazards.fema.gov/nfhlv2/output/County/1.zip", server)  # type: ignore[arg-type]
    written = nfhl.extract_members(remote.zip, list(members), tmp_path)
    assert written == sum(len(v) for v in members.values())
    for name, data in members.items():
        assert (tmp_path / name).read_bytes() == data
    assert not list(tmp_path.rglob("*.part"))


def test_a_file_replaced_mid_read_fails_instead_of_mixing_two_files(tmp_path: pathlib.Path) -> None:
    server = RangeServer(_zip({"S_FLD_HAZ_AR.shp": os.urandom(400_000)}))
    remote = nfhl.open_remote_zip("https://hazards.fema.gov/nfhlv2/output/County/1.zip", server)  # type: ignore[arg-type]
    server.etag = '"v2"'  # FEMA republished the file: If-Range no longer matches -> 200 with the whole body
    remote.reader._block = b""
    with pytest.raises(HttpFailed, match="range request"):
        nfhl.extract_members(remote.zip, ["S_FLD_HAZ_AR.shp"], tmp_path)
    assert not (tmp_path / "S_FLD_HAZ_AR.shp").exists()


def test_a_server_without_ranges_is_refused() -> None:
    server = RangeServer(_zip({"x": b"y"}), ranges=False)
    with pytest.raises(HttpFailed, match="byte ranges"):
        nfhl.open_remote_zip("https://hazards.fema.gov/nfhlv2/output/County/1.zip", server)  # type: ignore[arg-type]


def test_a_member_escaping_the_work_directory_is_refused(tmp_path: pathlib.Path) -> None:
    server = RangeServer(_zip({"../evil.txt": b"x"}))
    remote = nfhl.open_remote_zip("https://hazards.fema.gov/nfhlv2/output/County/1.zip", server)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="escapes"):
        nfhl.extract_members(remote.zip, ["../evil.txt"], tmp_path / "work")
    assert not (tmp_path / "evil.txt").exists()


# ------------------------------------------------------------------- file geodatabase catalog
def test_the_recorded_texas_catalog_maps_layers_to_table_files() -> None:
    catalog = nfhl.read_gdb_catalog(
        (CATALOG / "a00000001.gdbtable").read_bytes(), (CATALOG / "a00000001.gdbtablx").read_bytes()
    )
    assert len(catalog) == 57  # GDAL 3.10 lists the same 57 rows (2026-10-22)
    assert catalog["GDB_SystemCatalog"] == 1
    assert catalog["S_FLD_HAZ_AR"] == 0x15
    assert catalog["S_FIRM_PAN"] == 0x25
    assert catalog["S_LIMWA"] == 0x38


def test_a_corrupt_catalog_index_is_refused() -> None:
    table = (CATALOG / "a00000001.gdbtable").read_bytes()
    tablx = bytearray((CATALOG / "a00000001.gdbtablx").read_bytes())
    tablx[4] = 7  # seven 1024-row blocks: not a catalog
    with pytest.raises(ValueError, match="unsupported"):
        nfhl.read_gdb_catalog(table, bytes(tablx))


def test_a_state_geodatabase_fetches_system_tables_and_the_two_layers_only(tmp_path: pathlib.Path) -> None:
    gdb = "NFHL_48_20261013.gdb"
    members = {
        f"{gdb}/a00000001.gdbtable": (CATALOG / "a00000001.gdbtable").read_bytes(),
        f"{gdb}/a00000001.gdbtablx": (CATALOG / "a00000001.gdbtablx").read_bytes(),
        f"{gdb}/a00000004.gdbtable": b"items",
        f"{gdb}/gdb": b"\x05\x00\x00\x00",
        f"{gdb}/timestamps": b"t",
        f"{gdb}/a00000015.gdbtable": b"zones",  # S_FLD_HAZ_AR
        f"{gdb}/a00000015.gdbtablx": b"zx",
        f"{gdb}/a00000015.spx": b"zs",
        f"{gdb}/a00000025.gdbtable": b"panels",  # S_FIRM_PAN
        f"{gdb}/a00000017.gdbtable": os.urandom(500_000),  # S_FLD_HAZ_LN: not fetched
        f"{gdb}/a0000000f.gdbtable": os.urandom(500_000),  # S_TRNSPORT_LN: not fetched
    }
    server = RangeServer(_zip(members))
    download = nfhl.download_layers(
        "https://hazards.fema.gov/nfhlv2/output/State/NFHL_48_20261013.zip", tmp_path, server
    )  # type: ignore[arg-type]
    assert download.kind == "gdb"
    assert download.source == tmp_path / gdb
    got = sorted(p.name for p in (tmp_path / gdb).iterdir())
    assert got == sorted(
        [
            "a00000001.gdbtable",
            "a00000001.gdbtablx",
            "a00000004.gdbtable",
            "gdb",
            "timestamps",
            "a00000015.gdbtable",
            "a00000015.gdbtablx",
            "a00000015.spx",
            "a00000025.gdbtable",
        ]
    )
    assert download.bytes_fetched < 400_000  # neither 500 kB table was read


# ---------------------------------------------------------------------------- classification
@pytest.mark.parametrize(
    ("zone", "subtype", "sfha_tf", "expected"),
    [
        ("A", None, "T", "sfha"),
        ("AE", "FLOODWAY", "T", "sfha"),
        ("AH", None, "T", "sfha"),
        ("AO", None, "T", "sfha"),
        ("AR", None, "T", "sfha"),
        ("A99", None, "T", "sfha"),
        ("V", None, "T", "sfha"),
        ("VE", None, "T", "sfha"),
        ("AE", None, "F", "sfha"),  # flag and zone disagree: floodplain wins
        ("X", None, "T", "sfha"),
        ("X", "0.2 PCT ANNUAL CHANCE FLOOD HAZARD", "F", "shaded_x"),
        ("X", "AREA WITH REDUCED FLOOD RISK DUE TO LEVEE", "F", "shaded_x"),
        ("X", "AREA OF MINIMAL FLOOD HAZARD", "F", None),
        ("X", None, "F", None),
        ("X", "  ", "F", None),
        ("D", None, "F", "zone_d"),
        ("OPEN WATER", None, "F", "open_water"),
        ("AREA NOT INCLUDED", None, "F", "not_included"),
        ("B", None, "F", "unrecognised"),
    ],
)
def test_zone_classification(zone: str, subtype: str | None, sfha_tf: str, expected: str | None) -> None:
    assert nfhl.classify(zone, subtype, sfha_tf) == expected


def test_dates_and_panel_coverage() -> None:
    import datetime as dt

    assert nfhl.iso_date(dt.datetime(2019, 11, 15, 0, 0)) == "2019-11-15"  # noqa: DTZ001 - GDAL gives naive
    assert nfhl.iso_date(dt.date(2019, 11, 15)) == "2019-11-15"
    assert nfhl.iso_date("20191115") == "2019-11-15"
    assert nfhl.iso_date("2019-11-15T00:00:00") == "2019-11-15"
    assert nfhl.iso_date("2019-02-30") is None
    assert nfhl.iso_date(None) is None
    assert nfhl.panel_covers("Countywide, Panel Printed")
    assert nfhl.panel_covers("Countywide, Not Printed - (No Special Flood Hazard Areas - All Zone X)")
    assert not nfhl.panel_covers("Countywide, Not Printed - (Area Not Included)")


# ---------------------------------------------------------------------------------- geometry
def test_subdivide_tiles_the_input_exactly() -> None:
    ring = shapely.Point(-97.0, 31.0).buffer(0.5, quad_segs=200)  # 801 vertices
    pieces = nfhl.subdivide(ring, max_vertices=64)
    assert len(pieces) > 4
    assert all(shapely.get_num_coordinates(p) <= 64 for p in pieces)
    assert abs(shapely.union_all(pieces).area - ring.area) < 1e-9


def test_buffer_m_is_metric_east_west_too() -> None:
    line = shapely.LineString([(-100.0, 35.0), (-100.0, 35.1)])
    buf = nfhl.buffer_m(line, 3_218.688)  # 2 miles
    minx, _, maxx, _ = buf.bounds
    half_width_m = (maxx - minx) / 2 * nfhl.M_PER_DEG * 0.8191520  # cos 35 degrees
    assert abs(half_width_m - 3_218.688) < 5


def test_aoi_buffers_lines_of_69_kv_and_up_into_every_state_they_reach() -> None:
    # A line in Texas 1 km south of the Red River (the Oklahoma line), one at 34.5 kV, one unknown.
    lines = [
        nfhl.LineRecord(
            shapely.LineString([(-97.20, 33.82), (-97.10, 33.82)]), 138.0, "s", "u", "2026-10-09", "L"
        ),
        nfhl.LineRecord(
            shapely.LineString([(-97.5, 31.0), (-97.4, 31.0)]), 34.5, "s", "u", "2026-10-09", "L"
        ),
        nfhl.LineRecord(
            shapely.LineString([(-97.5, 31.5), (-97.4, 31.5)]), None, "s", "u", "2026-10-09", "L"
        ),
    ]
    rows, geoms, summary = nfhl.build_aoi(lines)
    assert summary["lines_kept"] == 1
    assert summary["lines_below_min_kv"] == 1 and summary["lines_without_voltage"] == 1
    assert [r["state"] for r in rows] == ["OK", "TX"]
    tx = geoms[1]
    # North-south reach of the buffer: 2 miles + 200 m margin, less at most the 10 m simplification.
    reach_m = _metres(tx.bounds[3] - 33.82)
    assert 3_218.688 + 200 - 25 <= reach_m <= 3_218.688 + 200 + 5
    assert rows[1]["line_count"] == 1 and rows[1]["licence"] == "L"


# ---------------------------------------------------------------------------------- clipping
def _two_panel_case() -> tuple[nfhl.PanelIndex, list[nfhl.Feature], Any]:
    # Two panels side by side; the AOI is a band across both; one AE polygon straddles the seam,
    # one A polygon is outside the AOI, one minimal-X polygon is never written.
    aoi = _box(-97.10, 31.00, -96.90, 31.02)
    panels = [
        _panel("48999C0100F", _box(-97.10, 30.95, -97.00, 31.05)),
        _panel("48999C0200F", _box(-97.00, 30.95, -96.90, 31.05)),
    ]
    zones = [
        _zone("1", _box(-97.02, 30.99, -96.98, 31.03)),
        _zone("2", _box(-97.08, 30.96, -97.06, 30.98), zone="A"),
        _zone(
            "3", _box(-97.05, 31.00, -97.04, 31.01), zone="X", sub="AREA OF MINIMAL FLOOD HAZARD", sfha="F"
        ),
        _zone(
            "4",
            _box(-96.95, 31.005, -96.94, 31.015),
            zone="X",
            sub="0.2 PCT ANNUAL CHANCE FLOOD HAZARD",
            sfha="F",
        ),
    ]
    return nfhl.PanelIndex.build(panels, aoi), zones, aoi


def test_zones_are_clipped_to_the_aoi_and_split_by_panel() -> None:
    panels, zones, aoi = _two_panel_case()
    rows, geoms, stats = nfhl.clip_zones([zones], panels, state="TX", prov=PROV)
    assert [(r["fld_ar_id"], r["firm_panel"], r["category"]) for r in rows] == [
        ("1", "48999C0100F", "sfha"),
        ("1", "48999C0200F", "sfha"),
        ("4", "48999C0200F", "shaded_x"),
    ]
    assert [r["piece_id"] for r in rows] == ["TX:1:48999C0100F:1", "TX:1:48999C0200F:1", "TX:4:48999C0200F:1"]
    assert stats.features_outside_aoi == 1 and stats.unshaded_x_read == 1
    for row, g in zip(rows, geoms, strict=True):
        assert g.difference(aoi.buffer(1e-9)).is_empty
        panel = next(
            p for p, pg in zip(panels.props, panels.geoms, strict=True) if p["FIRM_PAN"] == row["firm_panel"]
        )
        assert row["panel_effective_date"] == "2019-11-15" and panel
        assert (
            row["source_id"] == "us.fema.nfhl"
            and row["retrieved_at"]
            and row["licence"]
            and row["source_url"]
        )
        assert json.loads(row["raw"])["FLD_AR_ID"] == row["fld_ar_id"]
    # The two pieces of polygon 1 cover exactly its part inside the AOI.
    both = shapely.union_all(geoms[:2])
    assert abs(both.area - _box(-97.02, 31.00, -96.98, 31.02).area) < 1e-12
    # Each piece is 0.02 x 0.02 degrees at 31 N: 4.25 km2.
    assert rows[0]["area_m2"] == pytest.approx(0.02 * 0.02 * nfhl.M_PER_DEG**2 * 0.857, rel=0.01)


def test_simplification_moves_no_vertex_more_than_the_tolerance() -> None:
    # A wiggly floodplain edge: 0.5 m to 40 m wiggles along 2 km.
    import math

    xs = [-97.0 + i * 0.0002 for i in range(101)]
    top = [(x, 31.01 + (0.00035 if i % 7 == 0 else 0.00001) * math.sin(i)) for i, x in enumerate(xs)]
    poly = shapely.Polygon([(-97.0, 31.0), *top, (xs[-1], 31.0)])
    panels = nfhl.PanelIndex.build(
        [_panel("P", _box(-97.1, 30.9, -96.9, 31.1))], _box(-97.1, 30.9, -96.9, 31.1)
    )
    _, exact_geoms, _ = nfhl.clip_zones([[_zone("1", poly)]], panels, state="TX", prov=PROV, tolerance_m=0.0)
    _, geoms, stats = nfhl.clip_zones([[_zone("1", poly)]], panels, state="TX", prov=PROV, tolerance_m=10.0)
    assert stats.vertices_after < stats.vertices_before
    assert _metres(shapely.hausdorff_distance(exact_geoms[0], geoms[0])) <= 10.0 + 1e-6


def test_invalid_source_polygons_are_repaired_not_dropped() -> None:
    bowtie = shapely.Polygon([(-97.05, 31.0), (-96.95, 31.02), (-96.95, 31.0), (-97.05, 31.02)])
    panels = nfhl.PanelIndex.build(
        [_panel("P", _box(-97.1, 30.9, -96.9, 31.1))], _box(-97.1, 30.9, -96.9, 31.1)
    )
    rows, geoms, stats = nfhl.clip_zones([[_zone("1", bowtie)]], panels, state="TX", prov=PROV)
    assert stats.invalid_repaired == 1 and len(rows) == 1 and geoms[0].is_valid


def test_coverage_is_every_panel_and_area_not_included_does_not_cover() -> None:
    panels = nfhl.PanelIndex.build(
        [
            _panel("A1", _box(0, 0, 1, 1)),
            _panel("A2", _box(1, 0, 2, 1), typ="Countywide, Not Printed - (Area Not Included)", eff=None),
        ],
        None,
    )
    rows, geoms = nfhl.coverage_rows(panels, state="TX", prov=PROV)
    assert [(r["firm_panel"], r["covers"]) for r in rows] == [("A1", True), ("A2", False)]
    assert rows[1]["panel_effective_date"] is None and len(geoms) == 2


def test_the_simplifier_keeps_every_original_vertex_within_the_tolerance() -> None:
    """Real Texas pieces on which GEOS 3.13's topology-preserving simplifier moved vertices by up to
    1.57 times the tolerance (recorded 2026-10-22); the ring-wise Douglas-Peucker keeps the bound."""
    tol = nfhl.tolerance_degrees(10.0)
    pieces = json.loads((FIXTURES / "tps_violations_nfhl_48.json").read_text())["pieces"]
    assert len(pieces) == 4
    for piece in pieces:
        original = shapely.from_wkt(piece["wkt"])
        simplified = nfhl.simplify_polygonal(original, tol)
        assert simplified.is_valid and not simplified.is_empty
        assert shapely.get_num_coordinates(simplified) < shapely.get_num_coordinates(original)
        vertices = shapely.points(shapely.get_coordinates(original))
        assert float(shapely.distance(vertices, simplified.boundary).max()) <= tol * (1 + 1e-9), piece[
            "fld_ar_id"
        ]
        kept = shapely.points(shapely.get_coordinates(simplified))
        assert float(shapely.distance(kept, original.boundary).max()) < 1e-9  # only original vertices


def test_a_ring_too_small_to_simplify_is_kept_whole() -> None:
    tiny = shapely.Point(-97.0, 31.0).buffer(0.00003)  # ~3 m radius: collapses under 10 m
    holed = shapely.Polygon(_box(-97.01, 30.99, -96.99, 31.01).exterior, [tiny.exterior.coords])
    out = nfhl.simplify_polygonal(holed, nfhl.tolerance_degrees(10.0))
    assert len(out.interiors) == 1 and out.interiors[0].equals(tiny.exterior)


# ------------------------------------------------------------- the recorded county product
def _county_aoi() -> Any:
    line = nfhl.LineRecord(
        shapely.LineString([(WINDOW_CENTRE[0], 32.30), (WINDOW_CENTRE[0], 32.40)]),
        138.0,
        "t",
        "u",
        "2026-10-22",
        "L",
    )
    _, geoms, _ = nfhl.build_aoi([line], buffer_miles=0.5, margin_m=0.0, states=["TX"])
    return geoms[0]


def _extracted_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    with zipfile.ZipFile(COUNTY_ZIP) as zf:
        zf.extractall(tmp_path / "fixture")
    return tmp_path / "fixture"


def _rows(source: pathlib.Path, layer: str, engine: str) -> list[nfhl.Feature]:
    fields = nfhl.ZONE_FIELDS if layer == nfhl.ZONE_LAYER else nfhl.PANEL_FIELDS
    return [f for batch in nfhl.read_layer(source, layer, columns=fields, engine=engine) for f in batch]


def test_the_recorded_county_product_parses(tmp_path: pathlib.Path) -> None:
    source = _extracted_fixture(tmp_path)
    zones = _rows(source, nfhl.ZONE_LAYER, "pyshp")
    panels = _rows(source, nfhl.PANEL_LAYER, "pyshp")
    assert len(zones) == 32 and len(panels) == 3
    assert sorted(p["FIRM_PAN"] for p, _ in panels) == ["48441C0331F", "48441C0332F", "48441C0335F"]
    assert {nfhl.iso_date(p["EFF_DATE"]) for p, _ in panels} == {"2012-01-06"}
    first = zones[0][0]
    assert first["FLD_AR_ID"] == "48441C_10447" and first["FLD_ZONE"] == "A" and first["SFHA_TF"] == "T"
    assert first["ZONE_SUBTY"] is None  # a blank DBF text field reads as None, as GDAL reads it
    cats = [nfhl.classify(p["FLD_ZONE"], p["ZONE_SUBTY"], p["SFHA_TF"]) for p, _ in zones]
    assert (cats.count("sfha"), cats.count("shaded_x"), cats.count(None)) == (17, 13, 2)


def test_both_readers_give_the_same_rows(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("pyogrio")  # the GDAL reader is a build-time dependency
    source = _extracted_fixture(tmp_path)
    for layer in (nfhl.ZONE_LAYER, nfhl.PANEL_LAYER):
        a, b = _rows(source, layer, "pyshp"), _rows(source, layer, "pyogrio")
        assert [p for p, _ in a] == [p for p, _ in b]
        assert all(ga.equals(gb) for (_, ga), (_, gb) in zip(a, b, strict=True))


def test_a_county_product_builds_end_to_end_through_range_reads(tmp_path: pathlib.Path) -> None:
    server = RangeServer(COUNTY_ZIP.read_bytes(), etag='"34bc5dfe1dfadc1:0"')
    product = nfhl.product_from_file("TX", "48441C_20260611.zip")
    assert product.url == COUNTY_URL
    work = tmp_path / "raw"
    summary = nfhl.build_state(
        state="TX",
        aoi=_county_aoi(),
        out_dir=tmp_path / "out",
        product=product,
        work_dir=work,
        session=server,  # type: ignore[arg-type]
        engine="pyshp",
    )
    assert summary["download"]["kind"] == "shapefile" and summary["download"]["members"] == 10
    assert list(work.iterdir()) == []  # the raw layers are gone
    assert summary["panels"] == summary["panels_covering"] == 3
    table, geoms = nfhl.read_geoparquet(tmp_path / "out" / "sfha" / "TX.parquet")
    rows = table.to_pylist()
    assert rows and {r["category"] for r in rows} == {"sfha"} and all(r["sfha"] for r in rows)
    assert {r["zone"] for r in rows} <= nfhl.SFHA_ZONES
    assert {r["firm_panel"] for r in rows} <= {"48441C0331F", "48441C0332F", "48441C0335F"}
    assert {r["panel_effective_date"] for r in rows} == {"2012-01-06"}
    for r in rows:
        assert (
            r["source_id"] == "us.fema.nfhl"
            and r["source_url"] == COUNTY_URL
            and r["nfhl_product"] == "NFHL_48441C"
        )
        assert "government-works" in r["licence"] and r["retrieved_at"].endswith("Z")
        assert json.loads(r["raw"])["SFHA_TF"] == "T"
    aoi = shapely.buffer(_county_aoi(), nfhl.tolerance_degrees(10.0))
    assert all(aoi.covers(g) for g in geoms)
    other = pq.read_table(tmp_path / "out" / "zones_other" / "TX.parquet")
    assert set(other.column("category").to_pylist()) <= {"shaded_x"}
    coverage = pq.read_table(tmp_path / "out" / "coverage" / "TX.parquet").to_pylist()
    assert sorted(r["firm_panel"] for r in coverage) == ["48441C0331F", "48441C0332F", "48441C0335F"]
    assert json.loads((tmp_path / "out" / "states" / "TX.json").read_text())["files"]["sfha"]["rows"] == len(
        rows
    )
    assert summary["quality"]["unknown_zones"] == []
    assert summary["clip"]["unshaded_x_read"] == 1  # of 2: the other lies outside the AOI mask
    assert summary["sfha_area_km2"] > 0 and summary["files"]["sfha"]["rows"] == len(rows)


# ------------------------------------------------------------------------------- GeoParquet
def test_geoparquet_metadata_bbox_covering_and_round_trip(tmp_path: pathlib.Path) -> None:
    panels, zones, _ = _two_panel_case()
    rows, geoms, _ = nfhl.clip_zones([zones], panels, state="TX", prov=PROV)
    path = tmp_path / "sfha" / "TX.parquet"
    nfhl.write_geoparquet(path, rows, geoms, nfhl.ZONE_SCHEMA)
    table = pq.read_table(path)
    geo = json.loads(table.schema.metadata[b"geo"])
    assert geo["version"] == "1.1.0" and geo["primary_column"] == "geometry"
    col = geo["columns"]["geometry"]
    assert col["encoding"] == "WKB" and "crs" not in col  # absent = OGC:CRS84
    assert col["covering"]["bbox"]["xmin"] == ["bbox", "xmin"]
    assert set(col["geometry_types"]) <= {"Polygon", "MultiPolygon"}
    assert table.column_names[-2:] == ["bbox", "geometry"]
    _, back = nfhl.read_geoparquet(path)
    assert sorted(round(g.area, 12) for g in back) == sorted(round(g.area, 12) for g in geoms)
    for b, g in zip(table.column("bbox").to_pylist(), back, strict=True):
        assert (b["xmin"], b["ymin"], b["xmax"], b["ymax"]) == pytest.approx(g.bounds)


def test_an_empty_state_writes_the_same_columns(tmp_path: pathlib.Path) -> None:
    nfhl.write_geoparquet(tmp_path / "e.parquet", [], [], nfhl.ZONE_SCHEMA)
    table = pq.read_table(tmp_path / "e.parquet")
    assert table.num_rows == 0
    assert table.column_names == [f.name for f in nfhl.ZONE_SCHEMA] + ["bbox", "geometry"]


# ----------------------------------------------------------------------------- the manifest
def _summary(out: pathlib.Path, state: str, sfha_rows: int) -> None:
    files = {
        k: {"rows": n, "bytes": 10, "path": f"{k}/{state}.parquet", "sha256": "x"}
        for k, n in (("sfha", sfha_rows), ("zones_other", 1), ("coverage", 2))
    }
    (out / "states").mkdir(parents=True, exist_ok=True)
    (out / "states" / f"{state}.json").write_text(
        json.dumps({"state": state, "files": files, "download": {"bytes_fetched": 5}})
    )


def test_manifest_refuses_a_state_whose_sfha_rows_halved(tmp_path: pathlib.Path) -> None:
    _summary(tmp_path, "TX", 400)
    _summary(tmp_path, "OK", 100)
    previous = {
        "states": {"TX": {"files": {"sfha": {"rows": 1000}}}, "OK": {"files": {"sfha": {"rows": 110}}}}
    }
    with pytest.raises(nfhl.QualityGateFailed, match="TX"):
        nfhl.build_manifest(tmp_path, version="20261022T0441Z", previous=previous)
    manifest = nfhl.build_manifest(tmp_path, version="20261022T0441Z", previous=previous, max_drop_pct=70)
    assert manifest["change_vs_previous"]["TX"]["change_pct"] == -60.0
    assert manifest["totals"]["sfha_rows"] == 500 and manifest["totals"]["states"] == 2
    assert manifest["r2_prefix"] == "data/normalized/context/us.fema.nfhl/20261022T0441Z"
    assert json.loads((tmp_path / "manifest.json").read_text())["version"] == "20261022T0441Z"


def test_quality_reports_vocabulary_drift_and_duplicates() -> None:
    stats = nfhl.ClipStats()
    stats.zones.update({"AE": 3, "B": 1})
    rows = [
        {"piece_id": "TX:1:P:1", "fld_ar_id": "1", "firm_panel": "P"},
        {"piece_id": "TX:1:P:2", "fld_ar_id": "1", "firm_panel": "P"},
    ]
    q = nfhl.quality(
        rows,
        [{"firm_panel": "P", "panel_type": "Countywide, Panel Printed", "panel_effective_date": None}],
        stats,
    )
    assert q["unknown_zones"] == ["B"] and q["panels_without_effective_date"] == 1
    assert (
        q["duplicate_piece_ids"] == 0
        and q["source_ids_reused_in_a_panel"] == 1
        and q["duplicate_panels"] == 0
    )


# ----------------------------------------------------------------------------- the manifest row
def test_the_source_is_registered_public_domain_with_its_credit_and_no_connector() -> None:
    from pipeline.connectors.registry import Registry

    entry = Registry().get("us.fema.nfhl")
    assert entry.reuse == "open" and entry.publication == "raw_ok"
    assert not entry.implemented  # refreshed by the flood-layer workflow, not the scheduler
    raw = next(
        e
        for e in yaml.safe_load((ROOT / "data" / "sources.yaml").read_text())["sources"]
        if e["id"] == "us.fema.nfhl"
    )
    assert raw["attribution"] == "Source: FEMA National Flood Hazard Layer"
    for quoted in ("government-works", "available for free download", "general reference only"):
        assert quoted in raw["license"]


# ------------------------------------------------------------------- build script and workflow
def test_the_build_script_refuses_a_malformed_version_before_any_work(tmp_path: pathlib.Path) -> None:
    env = {
        "PATH": os.environ["PATH"],
        "R2_ACCOUNT_ID": "a",
        "R2_ACCESS_KEY_ID": "k",
        "R2_SECRET_ACCESS_KEY": "s",
        "R2_BUCKET": "b",
        "FLOOD_VERSION": "latest",
        "PYTHON": "/bin/false",
    }
    result = _run_script("plan", env)
    assert result.returncode == 2 and "FLOOD_VERSION" in result.stderr


#: A stand-in for the aws CLI: `aws s3 cp SRC DST [--recursive] [flags...]` against a directory
#: playing the bucket (s3://bucket/key -> $FAKE_R2/bucket/key). Anything else fails the test.
AWS_SHIM = """#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == s3 && "$2" == cp ]] || { echo "unexpected aws call: $*" >&2; exit 9; }
src="$3"; dst="$4"; shift 4; recursive=0
for a in "$@"; do [[ "$a" == --recursive ]] && recursive=1; done
echo "$src $dst" >> "$FAKE_R2/calls.log"
map() { case "$1" in s3://*) echo "$FAKE_R2/${1#s3://}";; *) echo "$1";; esac; }
s="$(map "$src")"; d="$(map "$dst")"
if [[ $recursive == 1 ]]; then mkdir -p "$d"; cp -r "$s"/. "$d"/; exit 0; fi
[[ -e "$s" ]] || { echo "NoSuchKey $src" >&2; exit 1; }
mkdir -p "$(dirname "$d")"; cp "$s" "$d"
"""


BASH = shutil.which("bash") or "/bin/bash"


def _run_script(mode: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command = [BASH, str(SCRIPT), mode]
    return subprocess.run(command, env=env, capture_output=True, text=True, check=False, cwd=ROOT)  # noqa: S603


def _script_env(tmp_path: pathlib.Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(AWS_SHIM)
    (bin_dir / "aws").chmod(0o755)
    (tmp_path / "r2" / "bucket").mkdir(parents=True)
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": os.pathsep.join(p for p in (str(ROOT), os.environ.get("PYTHONPATH", "")) if p),
        "PYTHON": sys.executable,
        "FAKE_R2": str(tmp_path / "r2"),
        "R2_ACCOUNT_ID": "a",
        "R2_ACCESS_KEY_ID": "k",
        "R2_SECRET_ACCESS_KEY": "s",
        "R2_BUCKET": "bucket",
        "FLOOD_VERSION": "20261022T0441Z",
        "HOME": str(tmp_path),
    }


def test_plan_prints_only_the_state_list_and_uploads_the_aoi(tmp_path: pathlib.Path) -> None:
    env = _script_env(tmp_path)
    lines = tmp_path / "lines.parquet"
    rows = [
        {
            "voltage_kv": 138.0,
            "source_id": "osm",
            "source_url": "u",
            "retrieved_at": "2026-10-22",
            "licence": "ODbL",
        }
    ]
    schema = pa.schema(
        [("voltage_kv", pa.float64())]
        + [(k, pa.string()) for k in ("source_id", "source_url", "retrieved_at", "licence")]
    )
    nfhl.write_geoparquet(lines, rows, [shapely.LineString([(-97.5, 31.0), (-97.4, 31.0)])], schema)
    env["LINES_PARQUET"] = str(lines)
    result = _run_script("plan", env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["TX"]
    key = tmp_path / "r2" / "bucket" / "data/normalized/context/us.fema.nfhl/20261022T0441Z"
    assert (key / "aoi.parquet").stat().st_size > 0
    assert json.loads((key / "aoi.json").read_text())["lines_kept"] == 1


def _staged_states(tmp_path: pathlib.Path, rows: dict[str, int]) -> pathlib.Path:
    key = tmp_path / "r2" / "bucket" / "data/normalized/context/us.fema.nfhl/20261022T0441Z"
    for state, n in rows.items():
        _summary(key, state, n)
    (key / "aoi.json").write_text(json.dumps({"lines_kept": 1, "states": {s: {} for s in rows}}))
    return key


def test_publish_writes_the_manifest_then_moves_latest(tmp_path: pathlib.Path) -> None:
    env = _script_env(tmp_path)
    key = _staged_states(tmp_path, {"TX": 100, "OK": 10})
    env["EXPECTED_STATES"] = "OK TX"
    result = _run_script("publish", env)
    assert result.returncode == 0, result.stderr
    latest = tmp_path / "r2" / "bucket" / "data/normalized/context/us.fema.nfhl/latest.json"
    assert latest.read_text() == (key / "manifest.json").read_text()
    assert json.loads(latest.read_text())["totals"]["sfha_rows"] == 110
    calls = (tmp_path / "r2" / "calls.log").read_text().splitlines()
    assert calls[-1].endswith("us.fema.nfhl/latest.json")  # promotion is the last write


def test_publish_refuses_a_missing_state_or_a_collapsed_one_and_keeps_latest(tmp_path: pathlib.Path) -> None:
    env = _script_env(tmp_path)
    _staged_states(tmp_path, {"TX": 100})
    latest = tmp_path / "r2" / "bucket" / "data/normalized/context/us.fema.nfhl/latest.json"
    latest.write_text(json.dumps({"version": "old", "states": {"TX": {"files": {"sfha": {"rows": 1000}}}}}))
    missing = _run_script("publish", {**env, "EXPECTED_STATES": "TX OK"})
    assert missing.returncode != 0 and "OK has no summary" in missing.stderr
    collapsed = _run_script("publish", {**env, "EXPECTED_STATES": "TX"})
    assert collapsed.returncode != 0 and "QualityGateFailed" in collapsed.stderr
    assert json.loads(latest.read_text())["version"] == "old"


def test_the_build_script_deletes_raw_downloads_and_promotes_last() -> None:
    text = SCRIPT.read_text()
    assert "--work-dir" in text and "the raw download was not deleted" in text
    publish = text[text.index("  publish)") :]
    upload = publish.index('r2 cp "${out}/manifest.json"')
    promote = publish.index('"s3://${R2_BUCKET}/${PREFIX}/latest.json" \\')
    assert upload < promote
    assert "R2_TILES_BUCKET" not in text  # private object store, not the public tiles bucket


BUILD_JOBS = ("plan", "state", "publish")


def test_the_workflow_is_quarterly_polite_and_publishes_only_after_every_state() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text())
    on = doc["on"] if "on" in doc else doc[True]
    assert set(on) == {"workflow_dispatch", "schedule", "pull_request"}
    assert on["schedule"][0]["cron"].split()[3] == "1,4,7,10"  # quarterly
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"]["cancel-in-progress"] is False
    jobs = doc["jobs"]
    assert jobs["state"]["strategy"]["max-parallel"] <= 3
    assert jobs["state"]["strategy"]["fail-fast"] is False
    assert jobs["publish"]["needs"] == ["plan", "state"]
    steps = [s for j in jobs.values() for s in j["steps"]]
    assert all(
        "@" in s["uses"] and len(s["uses"].split("@")[1].split()[0]) == 40 for s in steps if "uses" in s
    )
    runs = " ".join(str(s.get("run", "")) for s in steps)
    for mode in BUILD_JOBS:
        assert f"build_flood_layer.sh {mode}" in runs
    for name in BUILD_JOBS:
        assert jobs[name]["environment"] == "production"


def test_the_workflow_runs_these_tests_with_shapely_on_pull_requests_and_before_every_build() -> None:
    """requirements.txt does not carry shapely, so the core suite skips this module; the flood
    workflow is where it runs, with nothing to skip for, and a pull request runs nothing else."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    on = doc["on"] if "on" in doc else doc[True]
    paths = on["pull_request"]["paths"]
    for owned in (
        "pipeline/context/fema_nfhl.py",
        "pipeline/context/test_fema_nfhl.py",
        "infra/scripts/build_flood_layer.sh",
        ".github/workflows/flood-layer.yml",
        # Files the module or these tests read: a change to any of them runs the tests nowhere else.
        "data/sources.yaml",
        "pipeline/connectors/registry.py",
        "pipeline/connectors/store.py",
        "pipeline/connectors/http.py",
        "pipeline/context/lbnl_transmission.py",
        "data/vendored/regions/us_states.geojson",
        "requirements.txt",
    ):
        assert owned in paths
    jobs = doc["jobs"]
    test = jobs["test"]
    # No credentials on a PR: none in the test job, and none in the workflow-level env it inherits.
    assert "environment" not in test and "secrets." not in json.dumps(test)
    assert "secrets." not in json.dumps(doc.get("env") or {})
    for name in BUILD_JOBS:  # the R2 account and bucket live on the build jobs instead
        assert {"R2_ACCOUNT_ID", "R2_BUCKET"} <= set(jobs[name]["env"])
    runs = [str(s.get("run", "")) for s in test["steps"]]
    install = next(r for r in runs if "pip install" in r)
    script = SCRIPT.read_text()
    for package in re.findall(r"(?:shapely|pyogrio)==[\d.]+", install):
        assert package in script  # the versions the build itself installs
    assert {p.split("==")[0] for p in re.findall(r"(?:shapely|pyogrio)==[\d.]+", install)} == {
        "shapely",
        "pyogrio",
    }
    assert any("import shapely, pyogrio" in r for r in runs)
    assert any("pytest pipeline/context/test_fema_nfhl.py" in r for r in runs)
    assert jobs["plan"]["needs"] == "test"
    assert jobs["plan"]["if"] == "github.event_name != 'pull_request'"
    for name in ("state", "publish"):  # skipped with plan: they need its `ready` output
        assert "needs.plan.outputs.ready == 'true'" in jobs[name]["if"]
    assert "pull_request" in doc["concurrency"]["group"]  # a PR never queues behind a build


# ------------------------------------------- the finder's rule: computed only inside the AOI
VERSION = "20261022T0441Z"
#: Inside a source Zone A polygon and a covering FIRM panel of the county fixture, but outside the
#: 0.5-mile area of interest (review, 2026-10-23): no `sfha` row covers it.
OUTSIDE_AOI_IN_ZONE_A = (-99.76908, 32.34034)


def _version_dir(data_root: pathlib.Path, version: str = VERSION) -> pathlib.Path:
    return data_root / "normalized" / "context" / "us.fema.nfhl" / version


def _county_version(tmp_path: pathlib.Path) -> tuple[dict[str, Any], pathlib.Path, Any]:
    """The recorded county product built as one version under a local data root, laid out as the
    workflow lays it out on R2: (manifest, data root, the area of interest)."""
    data_root = tmp_path / "data-root"
    out = _version_dir(data_root)
    line = nfhl.LineRecord(
        shapely.LineString([(WINDOW_CENTRE[0], 32.30), (WINDOW_CENTRE[0], 32.40)]),
        138.0,
        "t",
        "u",
        "2026-10-22",
        "L",
    )
    rows, geoms, aoi_summary = nfhl.build_aoi([line], buffer_miles=0.5, margin_m=0.0, states=["TX"])
    nfhl.write_geoparquet(out / "aoi.parquet", rows, geoms, nfhl.AOI_SCHEMA)
    nfhl.build_state(
        state="TX",
        aoi=geoms[0],
        out_dir=out,
        source=_extracted_fixture(tmp_path),
        licence="PD",
        engine="pyshp",
    )
    manifest = nfhl.build_manifest(out, version=VERSION, aoi=aoi_summary)
    return manifest, data_root, geoms[0]


def _source_sfha(tmp_path: pathlib.Path) -> Any:
    zones = _rows(tmp_path / "fixture", nfhl.ZONE_LAYER, "pyshp")
    return shapely.union_all(
        [g for p, g in zones if nfhl.classify(p["FLD_ZONE"], p["ZONE_SUBTY"], p["SFHA_TF"]) == "sfha"]
    )


def test_a_site_outside_the_areas_of_interest_is_not_computed_never_outside(tmp_path: pathlib.Path) -> None:
    manifest, data_root, aoi = _county_version(tmp_path)
    layer = nfhl.FloodLayer.load(manifest, data_root=data_root)
    point = shapely.Point(*OUTSIDE_AOI_IN_ZONE_A)
    source_sfha = _source_sfha(tmp_path)
    assert source_sfha.covers(point) and not aoi.covers(point)  # the review's case, as recorded
    answer = layer.assess(point)
    assert (answer.answer, answer.reason) == ("unavailable", "not_computed")
    assert answer.firm_panels  # the coverage alone would have called it mapped
    # Every point of a grid over the fixture's panels that lies in the source SFHA reads "sfha" or
    # "unavailable", never "outside"; inside the AOI (20 m in from its edge), always "sfha".
    _, cov_geoms = nfhl.read_geoparquet(_version_dir(data_root) / "coverage" / "TX.parquet")
    x0, y0, x1, y1 = shapely.union_all(cov_geoms).bounds
    inner = shapely.buffer(aoi, -nfhl.tolerance_degrees(20.0))
    answers: Counter[str] = Counter()
    for i in range(41):
        for j in range(41):
            p = shapely.Point(x0 + (x1 - x0) * i / 40, y0 + (y1 - y0) * j / 40)
            a = layer.assess(p)
            answers[a.answer] += 1
            if source_sfha.covers(p):
                assert a.answer != "outside", (p.wkt, a)
                if inner.covers(p):
                    assert a.answer == "sfha", (p.wkt, a)
            if not aoi.covers(p) and a.answer != "sfha":
                assert a.reason == "not_computed", (p.wkt, a)
    assert answers["outside"] > 50 and answers["sfha"] > 20 and answers["unavailable"] > 500


def test_the_rule_reports_shaded_x_and_names_a_missing_map(tmp_path: pathlib.Path) -> None:
    manifest, data_root, aoi = _county_version(tmp_path)
    layer = nfhl.FloodLayer.load(manifest, data_root=data_root)
    version = _version_dir(data_root)
    _, panels = nfhl.read_geoparquet(version / "coverage" / "TX.parquet")
    # Inside the AOI beyond the three recorded panels: computed, but no digital map is loaded there.
    beyond = shapely.Point(WINDOW_CENTRE[0], 32.395)
    assert aoi.covers(beyond) and not any(g.covers(beyond) for g in panels)
    assert layer.assess(beyond) == nfhl.FloodAnswer("unavailable", "no_digital_map")
    # Shaded X is reported beside "outside", never a reason to exclude.
    _, other = nfhl.read_geoparquet(version / "zones_other" / "TX.parquet")
    _, sfha = nfhl.read_geoparquet(version / "sfha" / "TX.parquet")
    # Shaded X borders the SFHA: take a point of a shaded piece more than 30 m from any SFHA piece.
    clear = shapely.buffer(shapely.union_all(sfha), nfhl.tolerance_degrees(30.0))
    rests = [r for r in (shapely.difference(g, clear) for g in other) if not r.is_empty]
    shaded = [shapely.point_on_surface(max(rests, key=lambda r: r.area))]
    answer = layer.assess(shaded[0])
    assert answer.answer == "outside" and answer.reported == ("shaded_x",) and answer.firm_panels
    # 5 m outside an SFHA piece's edge counts as touching it (the files are exact to 10 m).
    piece = max(sfha, key=lambda g: g.area)
    x, y = piece.exterior.coords[0]
    step = 5.0 / nfhl.M_PER_DEG
    probes = [
        shapely.Point(x + step * math.cos(math.radians(k)), y + step * math.sin(math.radians(k)))
        for k in range(0, 360, 5)
    ]
    probe = next(q for q in probes if not piece.covers(q) and aoi.covers(q))
    assert layer.assess(probe).answer == "sfha"


def test_the_rule_never_mixes_one_states_coverage_with_another_states_aoi() -> None:
    # TX's AOI reaches into OK; OK's panels cover the point, but OK's AOI does not reach it, so OK's
    # zone files hold nothing there: not computed by OK, so not "outside".
    layer = nfhl.FloodLayer(
        aoi={"TX": _box(-97.2, 33.7, -96.8, 34.1), "OK": _box(-96.0, 34.0, -95.8, 34.2)},
        sfha=([], []),
        zones_other=([], []),
        coverage=(
            [
                {"state": "TX", "covers": True, "firm_panel": "T1"},
                {"state": "OK", "covers": True, "firm_panel": "O1"},
            ],
            [_box(-97.2, 33.7, -96.8, 33.9), _box(-97.2, 33.9, -95.7, 34.3)],
        ),
    )
    assert layer.assess(shapely.Point(-97.0, 34.05)).answer == "unavailable"
    assert layer.assess(shapely.Point(-97.0, 33.8)).answer == "outside"
    # Zone D and "area not included" are unavailable too; a panel that does not cover is no map.
    d_layer = nfhl.FloodLayer(
        aoi={"TX": _box(0, 0, 1, 1)},
        sfha=([], []),
        zones_other=(
            [{"category": "zone_d"}, {"category": "not_included"}],
            [_box(0.1, 0.1, 0.2, 0.2), _box(0.3, 0.3, 0.4, 0.4)],
        ),
        coverage=(
            [
                {"state": "TX", "covers": True, "firm_panel": "A"},
                {"state": "TX", "covers": False, "firm_panel": "B"},
            ],
            [_box(0, 0, 0.5, 1), _box(0.5, 0, 1, 1)],
        ),
    )
    assert d_layer.assess(shapely.Point(0.15, 0.15)) == nfhl.FloodAnswer("unavailable", "zone_d", (), ("A",))
    assert d_layer.assess(shapely.Point(0.35, 0.35)).reason == "not_included"
    assert d_layer.assess(shapely.Point(0.75, 0.5)).reason == "no_digital_map"
    assert d_layer.assess(_box(0.40, 0.6, 0.45, 0.65)).answer == "outside"
    assert d_layer.assess(_box(0.40, 0.6, 0.60, 0.65)).reason == "no_digital_map"  # a band half off the map


def test_the_manifest_states_the_aoi_rule_and_where_each_file_is(tmp_path: pathlib.Path) -> None:
    manifest, data_root, _ = _county_version(tmp_path)
    assert "not computed" in manifest["layers"]["aoi"] and "NOT clipped" in manifest["layers"]["coverage"]
    assert any("Not computed" in step and "area of interest" in step for step in manifest["finder_rule"])
    assert manifest["reference_implementation"].endswith("FloodLayer.assess")
    tx = manifest["states"]["TX"]
    prefix = f"data/normalized/context/us.fema.nfhl/{VERSION}"
    assert tx["prefix"] == prefix and tx["version"] == VERSION
    assert tx["aoi"]["key"] == f"{prefix}/aoi.parquet" and tx["aoi"]["buffer_miles"] == 0.5
    for layer in ("sfha", "zones_other", "coverage"):
        key = tx["files"][layer]["key"]
        assert key == f"{prefix}/{layer}/TX.parquet" and nfhl.local_path(key, data_root).exists()
    with pytest.raises(ValueError):
        nfhl.local_path("data/../etc/passwd", data_root)


# ------------------------------------------------ partial rebuilds keep every other state
OLD = "20260801T0441Z"


def _previous_manifest(states: tuple[str, ...] = ("FL", "LA", "OK", "TX")) -> dict[str, Any]:
    prefix = f"data/normalized/context/us.fema.nfhl/{OLD}"
    entries = {
        st: {
            "state": st,
            "version": OLD,
            "prefix": prefix,
            "files": {
                k: {
                    "rows": 100,
                    "bytes": 10,
                    "path": f"{k}/{st}.parquet",
                    "key": f"{prefix}/{k}/{st}.parquet",
                }
                for k in ("sfha", "zones_other", "coverage")
            },
            "aoi": {"key": f"{prefix}/aoi.parquet", "state_row": st},
            "download": {"bytes_fetched": 5},
        }
        for st in states
    }
    return {"version": OLD, "r2_prefix": prefix, "states": entries}


def test_a_partial_rebuild_carries_every_other_state_forward_under_its_own_version(
    tmp_path: pathlib.Path,
) -> None:
    _summary(tmp_path, "TX", 100)
    manifest = nfhl.build_manifest(tmp_path, version=VERSION, previous=_previous_manifest(), partial=True)
    assert sorted(manifest["states"]) == ["FL", "LA", "OK", "TX"]
    assert manifest["states_built"] == ["TX"] and manifest["states_carried"] == ["FL", "LA", "OK"]
    new, old = (f"data/normalized/context/us.fema.nfhl/{v}" for v in (VERSION, OLD))
    assert manifest["states"]["TX"]["files"]["sfha"]["key"] == f"{new}/sfha/TX.parquet"
    fl = manifest["states"]["FL"]
    assert fl["version"] == OLD and fl["files"]["sfha"]["key"] == f"{old}/sfha/FL.parquet"
    assert fl["aoi"]["key"] == f"{old}/aoi.parquet"
    assert manifest["totals"]["states"] == 4 and manifest["totals"]["bytes_fetched"] == 5  # only TX now
    # A manifest written before states carried their own keys: the keys come from its r2_prefix.
    legacy = _previous_manifest()
    for entry in legacy["states"].values():
        del entry["prefix"], entry["version"], entry["aoi"]
        for f in entry["files"].values():
            del f["key"]
    carried = nfhl.build_manifest(tmp_path, version=VERSION, previous=legacy, partial=True)["states"]["OK"]
    assert carried["files"]["coverage"]["key"] == f"{old}/coverage/OK.parquet" and carried["version"] == OLD


def test_a_full_build_that_loses_a_state_is_refused_unless_allowed(tmp_path: pathlib.Path) -> None:
    _summary(tmp_path, "TX", 100)
    with pytest.raises(nfhl.QualityGateFailed, match="FL: in the previous manifest") as failed:
        nfhl.build_manifest(tmp_path, version=VERSION, previous=_previous_manifest())
    assert "LA" in str(failed.value) and "OK" in str(failed.value)
    manifest = nfhl.build_manifest(
        tmp_path, version=VERSION, previous=_previous_manifest(), allow_dropped=["FL", "LA", " OK"]
    )
    assert sorted(manifest["states"]) == ["TX"] and manifest["states_dropped"] == ["FL", "LA", "OK"]


def test_publish_of_a_partial_build_keeps_every_state_in_latest(tmp_path: pathlib.Path) -> None:
    env = _script_env(tmp_path)
    _staged_states(tmp_path, {"TX": 100})
    latest = tmp_path / "r2" / "bucket" / "data/normalized/context/us.fema.nfhl/latest.json"
    latest.write_text(json.dumps(_previous_manifest()))
    # A full build that rebuilt only TX would drop three states: refused, latest.json unchanged.
    full = _run_script("publish", {**env, "EXPECTED_STATES": "TX"})
    assert full.returncode != 0 and "FL: in the previous manifest" in full.stderr
    assert json.loads(latest.read_text())["version"] == OLD
    # The same run as a partial rebuild (workflow_dispatch states=TX): all four states stay.
    partial = _run_script("publish", {**env, "EXPECTED_STATES": "TX", "STATES": "TX"})
    assert partial.returncode == 0, partial.stderr
    now = json.loads(latest.read_text())
    assert now["version"] == VERSION and sorted(now["states"]) == ["FL", "LA", "OK", "TX"]
    assert now["states"]["LA"]["prefix"].endswith(OLD) and now["states"]["TX"]["prefix"].endswith(VERSION)
    # A full build drops states only when told to.
    latest.write_text(json.dumps(_previous_manifest()))
    allowed = _run_script("publish", {**env, "EXPECTED_STATES": "TX", "ALLOW_DROPPED_STATES": "FL LA OK"})
    assert allowed.returncode == 0, allowed.stderr
    assert sorted(json.loads(latest.read_text())["states"]) == ["TX"]


# ------------------------------------------------------------------- review nits, tested
def test_the_zone_filter_keeps_null_zones_and_any_sfha_flag_on_a_geodatabase(tmp_path: pathlib.Path) -> None:
    """The production read path (GDAL's OpenFileGDB driver): a row the filter skips must be one
    `classify` drops anyway. In SQL `FLD_ZONE <> 'X'` is NULL for a NULL zone, which skipped the row."""
    pyogrio = pytest.importorskip("pyogrio")
    import numpy as np

    rows = [  # FLD_AR_ID, FLD_ZONE, ZONE_SUBTY, SFHA_TF
        ("1", "AE", None, "T"),
        ("2", "X", "AREA OF MINIMAL FLOOD HAZARD", "F"),
        ("3", None, None, None),  # no zone: unrecognised -> flood data unavailable
        ("4", "X", None, "Y"),  # flagged SFHA with another truthy value
        ("5", "X", None, None),
        ("6", "X", "0.2 PCT ANNUAL CHANCE FLOOD HAZARD", None),
    ]
    gdb = tmp_path / "t.gdb"
    fields = ["FLD_AR_ID", "FLD_ZONE", "ZONE_SUBTY", "SFHA_TF"]
    pyogrio.raw.write(
        gdb,
        shapely.to_wkb([_box(i, 0, i + 0.5, 0.5) for i in range(len(rows))]),
        [np.array([r[i] for r in rows], dtype=object) for i in range(4)],
        fields,
        layer=nfhl.ZONE_LAYER,
        driver="OpenFileGDB",
        geometry_type="Polygon",
        crs="EPSG:4269",
    )

    def read(where: str | None) -> list[dict[str, Any]]:
        batches = nfhl.read_layer(gdb, nfhl.ZONE_LAYER, columns=fields, where=where, engine="pyogrio")
        return [p for b in batches for p, _ in b]

    every = read(None)
    assert every[2]["FLD_ZONE"] is None  # a true NULL, as FEMA's tables can hold
    kept = {p["FLD_AR_ID"] for p in read(nfhl.ZONE_WHERE)}
    assert kept == {"1", "3", "4", "6"}
    skipped = [p for p in every if p["FLD_AR_ID"] not in kept]
    assert all(nfhl.classify(p["FLD_ZONE"], p["ZONE_SUBTY"], p["SFHA_TF"]) is None for p in skipped)


def test_a_repair_unions_overlapping_parts_instead_of_dropping_their_overlap() -> None:
    two = shapely.MultiPolygon([_box(0, 0, 2, 2), _box(1, 1, 3, 3)])  # invalid: the parts overlap
    assert not two.is_valid
    fixed = nfhl.valid(two)
    assert fixed.is_valid and fixed.area == pytest.approx(7.0) and fixed.covers(shapely.Point(1.5, 1.5))
    panels = nfhl.PanelIndex.build([_panel("P", _box(-1, -1, 4, 4))], _box(-1, -1, 4, 4))
    _, geoms, stats = nfhl.clip_zones([[_zone("1", two)]], panels, state="TX", prov=PROV, tolerance_m=0.0)
    assert stats.invalid_repaired == 1 and shapely.union_all(geoms).covers(shapely.Point(1.5, 1.5))


class _BrokenOnce:
    """A urllib3-like body that breaks halfway through when ``broken``."""

    def __init__(self, data: bytes, broken: bool) -> None:
        self.data = data
        self.broken = broken

    def stream(self, chunk_size: int, decode_content: bool = True) -> Any:
        from urllib3.exceptions import ProtocolError

        yield self.data[: len(self.data) // 2]
        if self.broken:
            raise ProtocolError("Connection broken: IncompleteRead")
        yield self.data[len(self.data) // 2 :]

    def close(self) -> None:
        pass


def test_a_connection_reset_inside_a_range_body_is_retried(tmp_path: pathlib.Path) -> None:
    """Through the real PoliteSession: the range body is read inside its retry loop, so a reset
    partway through a range costs one more request, not the state job."""
    import requests
    from requests.adapters import BaseAdapter

    from pipeline.connectors.http import PoliteSession

    data = _zip({"S_FLD_HAZ_AR.shp": os.urandom(300_000)})
    gets: list[str] = []

    class Adapter(BaseAdapter):
        def send(self, request: Any, **kwargs: Any) -> requests.Response:
            resp = requests.Response()
            resp.request, resp.url = request, request.url
            resp.raw = _BrokenOnce(b"", broken=False)
            if request.url.endswith("/robots.txt"):
                resp.status_code = 404
                return resp
            resp.headers["Accept-Ranges"], resp.headers["ETag"] = "bytes", '"v1"'
            if request.method == "HEAD":
                resp.status_code = 200
                resp.headers["Content-Length"] = str(len(data))
                return resp
            start, end = (int(v) for v in request.headers["Range"].removeprefix("bytes=").split("-"))
            gets.append(request.headers["Range"])
            resp.status_code = 206
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            resp.raw = _BrokenOnce(data[start : end + 1], broken=len(gets) == 1)
            return resp

        def close(self) -> None:
            pass

    session = requests.Session()
    session.mount("https://hazards.fema.gov", Adapter())
    polite = PoliteSession(
        rate_limits={"hazards.fema.gov": 1000.0}, max_attempts=3, sleep=lambda s: None, session=session
    )
    remote = nfhl.open_remote_zip("https://hazards.fema.gov/nfhlv2/output/County/1.zip", polite)
    nfhl.extract_members(remote.zip, ["S_FLD_HAZ_AR.shp"], tmp_path)
    assert (tmp_path / "S_FLD_HAZ_AR.shp").stat().st_size == 300_000
    assert len(gets) >= 2 and gets[0] == gets[1]  # the broken range was asked for again


LBNL_SAMPLE = FIXTURES / "lbnl_lines_sample.parquet"


def test_the_lbnl_asset_parquet_is_read_as_lines() -> None:
    """Three rows recorded from the real LBNL x HIFLD asset parquet (2026-10-09 snapshot): the
    `geom_line_wkt` + `attributes.voltage_kv` branch, each line with its own row's provenance."""
    lines = list(nfhl.lines_from_parquet(LBNL_SAMPLE))
    assert sorted(line.voltage_kv or 0.0 for line in lines) == [138.0, 138.0, 345.0]
    assert {line.source_id for line in lines} == {"us.lbnl.ferc_hifld_transmission_lines"}
    assert all(line.geometry.geom_type == "MultiLineString" for line in lines)
    assert all(line.licence.startswith("CC BY 4.0") and line.retrieved_at for line in lines)
    rows, _, summary = nfhl.build_aoi(lines)
    assert {"TX", "OK", "ND", "SD"} <= {r["state"] for r in rows}  # Taylor Co., Tulsa, the 345 kV line
    assert all(r["source_id"] == "us.lbnl.ferc_hifld_transmission_lines" for r in rows)
    assert summary["params"]["state_slack_m"] == nfhl.STATE_SLACK_M


def _lines_file(path: pathlib.Path, voltage: Any, geoms: list[Any], crs: Any = "absent") -> pathlib.Path:
    wkb = pa.array(shapely.to_wkb(geoms).tolist(), type=pa.binary())
    table = pa.table({"voltage_kv": voltage, "geometry": wkb})
    column: dict[str, Any] = {"encoding": "WKB", "geometry_types": []}
    if crs != "absent":
        column["crs"] = crs
    meta = {"version": "1.1.0", "primary_column": "geometry", "columns": {"geometry": column}}
    pq.write_table(table.replace_schema_metadata({b"geo": json.dumps(meta).encode()}), path)
    return path


def test_a_lines_file_in_the_wrong_crs_units_or_shape_is_refused(tmp_path: pathlib.Path) -> None:
    line = [shapely.LineString([(-97.5, 31.0), (-97.4, 31.0)])]
    kv = pa.array([138.0])

    def read(name: str, voltage: Any, geoms: list[Any], crs: Any = "absent") -> list[nfhl.LineRecord]:
        return list(nfhl.lines_from_parquet(_lines_file(tmp_path / name, voltage, geoms, crs)))

    assert read("a.parquet", kv, line, {"id": {"authority": "OGC", "code": "CRS84"}})[0].voltage_kv == 138.0
    assert read("b.parquet", pa.array([138]), line, "EPSG:4326")[0].voltage_kv == 138.0
    with pytest.raises(ValueError, match="crs"):
        read("c.parquet", kv, line, {"id": {"authority": "EPSG", "code": 3857}})
    mercator = [shapely.LineString([(-10_853_000.0, 3_632_000.0), (-10_842_000.0, 3_632_000.0)])]
    with pytest.raises(ValueError, match="not lon/lat"):
        read("d.parquet", kv, mercator)  # projected, and no crs declared
    with pytest.raises(ValueError, match="not numeric"):
        read("e.parquet", pa.array(["138000;69000"]), line)
    with pytest.raises(ValueError, match="volts"):
        read("f.parquet", pa.array([138_000.0]), line)
    with pytest.raises(ValueError, match="from lines"):
        read("g.parquet", kv, [_box(-97.5, 31.0, -97.4, 31.1)])


def _east_west_shortfall_m(line: Any, buf: Any, distance_m: float) -> float:
    """How far short of ``distance_m`` the buffer reaches due east or west of each vertex."""
    worst = 0.0
    for x, y in shapely.get_coordinates(line):
        k = nfhl.M_PER_DEG * math.cos(math.radians(y))
        for sign in (1, -1):
            ray = shapely.LineString([(x, y), (x + sign * 2 * distance_m / k, y)])
            hits = shapely.get_coordinates(ray.intersection(buf.boundary))
            if len(hits):  # none: the buffer reaches past the ray (the line runs east-west there)
                worst = max(worst, distance_m - float(abs(hits[:, 0] - x).min()) * k)
    return worst


def test_a_tall_line_is_buffered_at_its_own_latitudes() -> None:
    """Leland Olds - Fort Thompson 345 kV spans 3.15 degrees of latitude: one cos(latitude) left its
    buffer 104 m short east-west at the north end; half-degree bands leave only the arc approximation."""
    tall = max(
        (line.geometry for line in nfhl.lines_from_parquet(LBNL_SAMPLE)),
        key=lambda g: g.bounds[3] - g.bounds[1],
    )
    assert tall.bounds[3] - tall.bounds[1] > 3.0
    distance = nfhl.DEFAULT_BUFFER_MILES * nfhl.METRES_PER_MILE + nfhl.AOI_MARGIN_M
    assert _east_west_shortfall_m(tall, nfhl.buffer_m(tall, distance), distance) < 25.0
    # A segment lying on a band's edge (here at the line's lowest latitude) survives the clip.
    hooked = shapely.LineString([(-100.0, 47.0), (-100.0, 45.0), (-100.05, 45.0)])
    west = shapely.Point(-100.05 - 2_900 / (nfhl.M_PER_DEG * math.cos(math.radians(45.0))), 45.0)
    assert nfhl.buffer_m(hooked, 3_000.0).covers(west)


def test_aoi_rows_carry_only_their_own_states_provenance_and_reach_nearby_states() -> None:
    codes = ["AA", "BB"]
    shapes = [_box(-100.0, 30.0, -99.0, 31.0), _box(-98.86, 30.0, -98.0, 31.0)]  # a 13 km gap
    lines = [
        nfhl.LineRecord(
            shapely.LineString([(-99.5, 30.2), (-99.5, 30.4)]), 138.0, "osm", "u1", "2026-10-20", "ODbL"
        ),
        nfhl.LineRecord(
            shapely.LineString([(-98.5, 30.6), (-98.5, 30.8)]), 345.0, "lbnl", "u2", "2026-10-09", "CC"
        ),
    ]
    rows, _, _ = nfhl.build_aoi(lines, state_shapes=(codes, shapes), state_slack_m=0.0)
    assert {r["state"]: (r["source_id"], r["licence"]) for r in rows} == {
        "AA": ("osm", "ODbL"),
        "BB": ("lbnl", "CC"),
    }
    assert "lines_source_id" not in nfhl.AOI_SCHEMA.names and "source_id" in nfhl.AOI_SCHEMA.names
    # A line 3.6 km from AA's generalised edge and 9.8 km from BB's: its 3.4 km buffer touches neither
    # shape, so without the slack no state's flood files would cover it.
    near = nfhl.LineRecord(
        shapely.LineString([(-98.9625, 30.5), (-98.9625, 30.55)]), 138.0, "o", "u", "t", "L"
    )
    no_slack, _, _ = nfhl.build_aoi([near], state_shapes=(codes, shapes), state_slack_m=0.0)
    with_slack, _, _ = nfhl.build_aoi([near], state_shapes=(codes, shapes))
    assert no_slack == [] and [r["state"] for r in with_slack] == ["AA", "BB"]


def test_pieces_written_batch_by_batch_equal_one_write(tmp_path: pathlib.Path) -> None:
    panels, zones, _ = _two_panel_case()
    rows, geoms, _ = nfhl.clip_zones([zones[:2], zones[2:]], panels, state="TX", prov=PROV)
    stats = nfhl.ClipStats()
    batches = list(nfhl.iter_clip_zones([zones[:2], zones[2:]], panels, state="TX", prov=PROV, stats=stats))
    assert len(batches) == 2 and [r for b, _ in batches for r in b] == rows
    builder = nfhl.GeoParquetBuilder(nfhl.ZONE_SCHEMA)
    for b_rows, b_geoms in batches:
        builder.add(b_rows, b_geoms)
    builder.write(tmp_path / "batched.parquet")
    nfhl.write_geoparquet(tmp_path / "once.parquet", rows, geoms, nfhl.ZONE_SCHEMA)
    a, b = pq.read_table(tmp_path / "batched.parquet"), pq.read_table(tmp_path / "once.parquet")
    assert a.equals(b) and a.schema.metadata == b.schema.metadata


def test_the_plan_step_fails_when_the_plan_fails() -> None:
    """`echo "states=$(plan)"` returns echo's status, so a failed plan left the step green."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    run = next(s for s in doc["jobs"]["plan"]["steps"] if s.get("id") == "plan")["run"]
    assert not re.search(r"echo [^\n]*\$\(infra/scripts/build_flood_layer\.sh", run)
    assert run.index('states="$(infra/scripts/build_flood_layer.sh plan)"') < run.index("GITHUB_OUTPUT")
    publish = doc["jobs"]["publish"]["steps"][-1]["env"]
    assert publish["STATES"] == "${{ inputs.states }}" and "ALLOW_DROPPED_STATES" in publish


# ------------------------------------------------------------------- the server's pull (context_build)
class FakeStore:
    """`S3Backend.get` over a dict of full keys: a missing key is FileNotFoundError."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.gets: list[str] = []

    def get(self, key: str) -> bytes:
        self.gets.append(key)
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]


def _published_flood(states: dict[str, str]) -> dict[str, bytes]:
    """A published build as build_flood_layer.sh leaves it: per state (state -> its version) three
    layer files with their sha256 and the version's aoi.parquet, and latest.json naming every key."""
    import hashlib

    objects: dict[str, bytes] = {}
    entries: dict[str, Any] = {}
    for st, version in states.items():
        prefix = f"{nfhl.R2_PREFIX}/{version}"
        files = {}
        for layer in ("sfha", "zones_other", "coverage"):
            body = f"{st}-{layer}-{version}".encode()
            key = f"{prefix}/{layer}/{st}.parquet"
            objects[key] = body
            files[layer] = {"key": key, "sha256": hashlib.sha256(body).hexdigest(), "rows": 1}
        objects[f"{prefix}/aoi.parquet"] = f"aoi-{version}".encode()
        entries[st] = {
            "version": version,
            "prefix": prefix,
            "files": files,
            "aoi": {"key": f"{prefix}/aoi.parquet"},
        }
    manifest = {"source_id": nfhl.SOURCE_ID, "version": max(states.values()), "states": entries}
    objects[nfhl.LATEST_KEY] = json.dumps(manifest).encode()
    return objects


def test_pull_before_any_build_is_not_published_and_writes_nothing(tmp_path: pathlib.Path) -> None:
    result = nfhl.pull(FakeStore({}), states=["TX"], data_root=tmp_path)
    assert result["status"] == nfhl.NOT_PUBLISHED and result["key"] == nfhl.LATEST_KEY
    assert not any(tmp_path.iterdir())
    # Published, but not for the states this server loads: also "not published" here.
    objects = _published_flood({"OK": "20261022T0441Z"})
    result = nfhl.pull(FakeStore(objects), states=["TX"], data_root=tmp_path)
    assert result["status"] == nfhl.NOT_PUBLISHED and result["states_not_published"] == ["TX"]
    assert not any(tmp_path.iterdir())


def test_pull_fetches_only_the_servers_states_at_their_own_keys_and_writes_the_manifest_last(
    tmp_path: pathlib.Path,
) -> None:
    """A partial rebuild carries a state forward under its older version (docs/25 §5.6): the pull reads
    each state's files where the manifest says, checks each sha256, and narrows the local manifest to
    the states it holds, which is all the site finder lists (`services/site_finder/flood.py`)."""
    objects = _published_flood({"TX": "20261023T0441Z", "LA": "20260703T0441Z", "OK": "20261023T0441Z"})
    store = FakeStore(objects)
    result = nfhl.pull(store, states=["TX", "LA", "NM"], data_root=tmp_path)
    assert result["status"] == "pulled" and result["states"] == ["LA", "TX"]
    assert result["states_not_published"] == ["NM"] and result["files_fetched"] == 8
    assert not any("/OK." in key for key in store.gets), "a state the server does not load is not fetched"
    assert store.gets[0] == nfhl.LATEST_KEY
    local = json.loads(nfhl.local_path(nfhl.LATEST_KEY, tmp_path).read_text())
    assert sorted(local["states"]) == ["LA", "TX"] and local["states_loaded"] == ["LA", "TX"]
    for entry in local["states"].values():
        for key, _digest in nfhl._state_objects(entry):
            assert nfhl.local_path(key, tmp_path).read_bytes() == objects[key]
    assert not list(tmp_path.rglob("*.part"))

    again = nfhl.pull(FakeStore(objects), states=["TX", "LA", "NM"], data_root=tmp_path)
    assert again["status"] == "unchanged" and again["files_fetched"] == 0

    # Texas rebuilt: its new version arrives and the previous manifest's directories stay (a site
    # finder run may still be reading them); a version neither manifest names is then removed.
    newer = _published_flood({"TX": "20261101T0441Z", "LA": "20260703T0441Z"})
    result = nfhl.pull(FakeStore(newer), states=["TX", "LA"], data_root=tmp_path)
    assert result["status"] == "pulled" and result["files_fetched"] == 4 and result["versions_removed"] == []
    final = _published_flood({"TX": "20261201T0441Z", "LA": "20260703T0441Z"})
    result = nfhl.pull(FakeStore(final), states=["TX", "LA"], data_root=tmp_path)
    assert result["versions_removed"] == ["20261023T0441Z"]
    base = nfhl.local_path(nfhl.LATEST_KEY, tmp_path).parent
    kept = sorted(p.name for p in base.iterdir() if p.is_dir())
    assert kept == ["20260703T0441Z", "20261101T0441Z", "20261201T0441Z"]


def test_pull_refuses_a_file_that_fails_its_sha256_and_keeps_the_local_manifest(
    tmp_path: pathlib.Path,
) -> None:
    nfhl.pull(FakeStore(_published_flood({"TX": "20261023T0441Z"})), states=["TX"], data_root=tmp_path)
    before = nfhl.local_path(nfhl.LATEST_KEY, tmp_path).read_bytes()
    newer = _published_flood({"TX": "20261101T0441Z"})
    newer[f"{nfhl.R2_PREFIX}/20261101T0441Z/sfha/TX.parquet"] += b"tampered"
    with pytest.raises(nfhl.PullError, match="sha256"):
        nfhl.pull(FakeStore(newer), states=["TX"], data_root=tmp_path)
    assert nfhl.local_path(nfhl.LATEST_KEY, tmp_path).read_bytes() == before


def test_pull_cli_reads_grid_layers_regions_and_refuses_without_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    objects = _published_flood({"TX": "20261023T0441Z", "OK": "20261023T0441Z"})
    monkeypatch.setattr(nfhl, "store_from_env", lambda: FakeStore(objects))
    monkeypatch.delenv("GRID_LAYERS_REGIONS", raising=False)
    with pytest.raises(SystemExit, match="GRID_LAYERS_REGIONS is not set"):
        nfhl.main(["pull", "--data-root", str(tmp_path)])
    monkeypatch.setenv("GRID_LAYERS_REGIONS", "US-TX")
    nfhl.main(["pull", "--data-root", str(tmp_path)])
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["status"] == "pulled" and summary["states"] == ["TX"]
    monkeypatch.setenv("GRID_LAYERS_REGIONS", "all")
    nfhl.main(["pull", "--data-root", str(tmp_path)])
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["states"] == ["OK", "TX"]


def test_store_from_env_names_every_missing_r2_setting() -> None:
    with pytest.raises(nfhl.PullError, match="R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY"):
        nfhl.store_from_env({})
    env = {"R2_BUCKET": "b", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s", "R2_ACCOUNT_ID": "acct"}
    store = nfhl.store_from_env(env)
    assert (store.bucket, store.prefix) == ("b", "")
    assert store.endpoint_url == "https://acct.r2.cloudflarestorage.com"
