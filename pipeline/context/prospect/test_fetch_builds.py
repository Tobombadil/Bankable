# ruff: noqa: S101 - asserts are the point (docs/04 E-6)
"""Each prospect builder's fetch route, offline: the terms re-read, the requests it makes, the run record it
leaves, the raw files it deletes, and what it refuses. The hosts are the fake web in conftest.py; the GDAL
readers (pyogrio, a build-only dependency) are replaced by the recorded GeoJSON they would have yielded."""

from __future__ import annotations

import io
import json
import pathlib
import struct
import zipfile
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pyarrow.parquet as pq  # type: ignore[import-untyped, unused-ignore]
import pytest
import requests

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import HttpBlocked
from pipeline.connectors.store import Store
from pipeline.context.prospect import (
    eia_oil_gas,
    gwa_wind,
    netl_energy_communities,
    nrel_nsrdb,
    usgs_nshm,
    usgs_padus,
    wri_aqueduct,
)
from pipeline.context.prospect.cog import Grid, write_cog
from pipeline.context.prospect.common import REGIONS, TermsChanged
from pipeline.context.prospect.conftest import FakeWeb
from services.context_sample.geotiff import GeoTiff, GeoTiffError

FIX = pathlib.Path(__file__).parent / "fixtures"


def _features(*parts: str) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = json.loads(FIX.joinpath(*parts).read_text())["features"]
    return features


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _runs(store_root: pathlib.Path, source_id: str) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted((store_root / "runs" / source_id).glob("*.json"))]


def _empty(path: pathlib.Path) -> bool:
    return not any(path.iterdir())


def _arcgis_layer(web: FakeWeb, layer_url: str, doc: dict[str, Any]) -> None:
    """An ArcGIS layer whose first page is `doc` and whose later pages are empty."""
    web.json(layer_url, {"maxRecordCount": 1000, "objectIdField": "OBJECTID"})

    def query(req: requests.PreparedRequest) -> tuple[int, bytes, dict[str, str]]:
        offset = int(parse_qs(urlsplit(str(req.url)).query)["resultOffset"][0])
        page = doc if offset == 0 else {"type": "FeatureCollection", "features": []}
        return 200, json.dumps(page).encode(), {"Content-Type": "application/geo+json"}

    web.route(f"{layer_url}/query", query)


# ------------------------------------------------------------------ EIA basins and plays
EIA_TERMS = (
    "<html><body><h1>Copyrights and Reuse</h1><p>U.S. government publications are in the public domain and "
    "are not subject to copyright protection.</p><p>You may use and/or distribute any of our data, files, "
    "databases, reports, graphs, charts, and other information products</p></body></html>"
)


def _eia_web(web: FakeWeb) -> None:
    web.page(eia_oil_gas.TERMS_URL, EIA_TERMS)
    for layer_id, spec in eia_oil_gas.LAYERS.items():
        _arcgis_layer(web, spec["url"], json.loads((FIX / "eia_oil_gas" / f"{layer_id}.geojson").read_text()))


def test_eia_layers_are_fetched_from_the_atlas_for_the_region_box(
    web: FakeWeb, tmp_path: pathlib.Path
) -> None:
    _eia_web(web)
    out = eia_oil_gas.build(
        region="texas", out_dir=tmp_path / "out", session=web.session(), store=Store(tmp_path)
    )
    by_layer = {o["layer_id"]: o for o in out}
    assert by_layer["oil_gas_basins"]["summary"]["features_in"] == 2
    assert by_layer["oil_gas_plays"]["summary"]["rows"] == 3
    assert by_layer["oil_gas_plays"]["coverage"] == {
        "kind": "bbox",
        "bbox": list(REGIONS["texas"]),
        "label": "texas",
    }
    for layer_id, spec in eia_oil_gas.LAYERS.items():
        asked = web.params(f"{spec['url']}/query")
        assert len(asked) == 1 and asked[0]["geometry"] == ",".join(str(v) for v in REGIONS["texas"])
        table = pq.read_table(tmp_path / "out" / f"{layer_id}.parquet")
        assert set(table.column("source_url").to_pylist()) == {spec["url"]}
        (run,) = _runs(tmp_path, spec["source_id"])
        assert run["layer"] == layer_id and run["region"] == "texas"
        assert run["requests"][0]["url"] == eia_oil_gas.TERMS_URL
        assert run["features"] == by_layer[layer_id]["summary"]["features_in"]


def test_eia_build_stops_when_the_reuse_terms_change(web: FakeWeb, tmp_path: pathlib.Path) -> None:
    _eia_web(web)
    web.page(
        eia_oil_gas.TERMS_URL,
        "<p>U.S. government publications are in the public domain and are not "
        "subject to copyright protection.</p><p>Permission is required to reuse our data.</p>",
    )
    with pytest.raises(TermsChanged, match="You may use and/or distribute"):
        eia_oil_gas.build(
            region="texas", out_dir=tmp_path / "out", session=web.session(), store=Store(tmp_path)
        )
    assert _empty(tmp_path / "out") and not any("/query" in u for u in web.urls()), "nothing fetched or built"
    assert not (tmp_path / "runs").exists()


def test_eia_rows_without_geometry_or_numbers_are_handled() -> None:
    rows, geoms = eia_oil_gas.normalise(
        "oil_gas_plays",
        [
            {
                "properties": {"Shale_play": " Barnett ", "Area_sq_mi": "n/a", "Basin": ""},
                "geometry": {"type": "x"},
            },
            {"properties": {"Shale_play": "No outline"}, "geometry": None},
        ],
    )
    assert rows == [{"name": "Barnett", "basin": None, "lithology": None, "age": None, "area_sq_mi": None}]
    assert len(geoms) == 1


# ------------------------------------------------------------------ NETL energy communities
NETL_LICENCE = {
    "licenseInfo": "<p>License: Creative Commons Attribution.</p><p>These spatial data may not be relied "
    "upon by taxpayers to substantiate a tax return position or for determining whether certain penalties "
    "apply and will not be used by the IRS for examination purposes.</p>",
    "accessInformation": "DOE NETL",
}


def _netl_web(web: FakeWeb) -> None:
    for key, service in netl_energy_communities.SERVICES.items():
        web.json(f"{service}/info/iteminfo?f=json", NETL_LICENCE)
        _arcgis_layer(web, f"{service}/0", json.loads((FIX / "netl" / f"{key}.geojson").read_text()))


def test_netl_layers_are_fetched_per_state_and_generalised_by_the_server(
    web: FakeWeb, tmp_path: pathlib.Path
) -> None:
    _netl_web(web)
    out = netl_energy_communities.build(
        region="texas", out_dir=tmp_path / "out", session=web.session(), store=Store(tmp_path)
    )
    assert [o["layer_id"] for o in out] == ["energy_community_coal_closure", "energy_community_ffe"]
    assert out[0]["summary"]["rows"] == 6 and out[1]["summary"]["rows"] == 3
    coal_pages = web.params(f"{netl_energy_communities.SERVICES['coal']}/0/query")
    # The recorded coal page carries exceededTransferLimit, so the pager asked for the next page.
    assert [p["resultOffset"] for p in coal_pages] == ["0", "1000"]
    assert all(
        p["where"] == "state_name IN ('Texas')" and p["maxAllowableOffset"] == "5e-05" for p in coal_pages
    )
    (run,) = _runs(tmp_path, netl_energy_communities.SOURCE_ID)
    assert run["features"] == {"coal": 6, "ec": 2, "ffe_only": 1}
    assert sum(1 for r in run["requests"] if r.get("terms_checked") == 2) == 3, "each item's licence re-read"
    assert not any(u.endswith("/robots.txt") for u in web.urls()), "the REST API is read, not EDX's pages"


def test_netl_build_stops_when_an_item_drops_the_taxpayer_line(web: FakeWeb, tmp_path: pathlib.Path) -> None:
    _netl_web(web)
    web.json(
        f"{netl_energy_communities.SERVICES['ffe_only']}/info/iteminfo?f=json",
        {"licenseInfo": "<p>License: Creative Commons Attribution.</p>"},
    )
    with pytest.raises(TermsChanged, match="relied upon by taxpayers"):
        netl_energy_communities.build(region="texas", out_dir=tmp_path / "out", session=web.session())
    assert _empty(tmp_path / "out")


def test_netl_flags_dates_and_missing_geometry() -> None:
    rows, _ = netl_energy_communities.normalise_coal(
        [
            {
                "properties": {
                    "mine_closure": "Yes - 2015",
                    "generator_closure": "Y",
                    "date_last_update": "soon",
                },
                "geometry": {"type": "Polygon"},
            },
            {
                "properties": {"mine_closure": "No", "label": "fallback label"},
                "geometry": {"type": "Polygon"},
            },
            {"properties": {"mine_closure": "Yes"}, "geometry": None},
        ]
    )
    assert rows[0]["category"] == "coal mine closure and coal generator retirement"
    assert rows[0]["record_updated"] is None and rows[0]["tract_geoid"] is None
    assert rows[1]["category"] == "fallback label" and len(rows) == 2
    rows, _ = netl_energy_communities.normalise_ffe(
        [{"properties": {"label_ec": None, "ec_qual_status": "Yes"}, "geometry": None}],
        [{"properties": {"label_fee": "", "ec_qual_status": "No"}, "geometry": {"type": "Polygon"}}],
    )
    assert len(rows) == 1 and rows[0]["status"] == "No" and rows[0]["is_energy_community"] is False


# ------------------------------------------------------------------ NLR solar
NLR_DISCLAIMER = f"<html><main><h2>Data and Software</h2><p>{nrel_nsrdb.NOTICE}</p></main></html>"


def _nsrdb_zip(kind: str) -> bytes:
    tif = (FIX / "nrel_nsrdb" / f"nsrdb3_{kind}.tif").read_bytes()
    return _zip(
        {
            f"__MACOSX/nsrdbv3_{kind}/Annual {kind.upper()}/._nsrdb3_{kind}.tif": b"resource fork",
            f"nsrdbv3_{kind}/Monthly {kind.upper()}/nsrdb3_{kind}_01.tif": b"monthly, never extracted",
            f"nsrdbv3_{kind}/Annual {kind.upper()}/nsrdb3_{kind}.tif": tif,
        }
    )


def _nlr_web(web: FakeWeb) -> dict[str, bytes]:
    web.page(nrel_nsrdb.TERMS_URL, NLR_DISCLAIMER)
    zips = {kind: _nsrdb_zip(kind) for kind in ("ghi", "dni")}
    for kind, data in zips.items():
        web.file(
            f"{nrel_nsrdb.BASE_URL}/nsrdbv3_{kind}.zip", data, last_modified="Tue, 17 Dec 2024 00:00:00 GMT"
        )
    return zips


def test_nsrdb_is_downloaded_extracted_and_the_raw_files_deleted(
    web: FakeWeb, tmp_path: pathlib.Path
) -> None:
    import hashlib

    zips = _nlr_web(web)
    work = tmp_path / "work"
    work.mkdir()
    out = nrel_nsrdb.build(
        region="texas", out_dir=tmp_path / "out", session=web.session(), store=Store(tmp_path), work_dir=work
    )
    assert [o["layer_id"] for o in out] == ["solar_ghi", "solar_dni"]
    assert _empty(work), "the zips and the extracted rasters are working files"
    (run,) = _runs(tmp_path, nrel_nsrdb.SOURCE_ID)
    downloads = [r for r in run["requests"] if "sha256" in r]
    assert [d["sha256"] for d in downloads] == [hashlib.sha256(zips[k]).hexdigest() for k in ("ghi", "dni")]
    assert out[0]["provenance"]["retrieved_at"] == downloads[0]["retrieved_at"]
    assert out[1]["provenance"]["source_url"] == f"{nrel_nsrdb.BASE_URL}/nsrdbv3_dni.zip"
    with (
        GeoTiff(FIX / "nrel_nsrdb" / "nsrdb3_dni.tif") as src,
        GeoTiff(tmp_path / "out" / "solar_dni.tif") as got,
    ):
        value = src.value_at(-103.5, 31.4)
        assert value is not None and got.value_at(-103.5, 31.4) == pytest.approx(round(value, 3))
        assert got.metadata["notice"] == nrel_nsrdb.NOTICE
    assert web.urls()[0] == "https://www.nlr.gov/robots.txt"


def test_nsrdb_honours_robots_txt_for_the_files(web: FakeWeb, tmp_path: pathlib.Path) -> None:
    _nlr_web(web)
    web.page(
        "https://www.nlr.gov/robots.txt",
        "User-agent: Inframation\nDisallow: /docs/\n",
        content_type="text/plain",
    )
    with pytest.raises(HttpBlocked, match=r"robots\.txt disallows"):
        nrel_nsrdb.build(region="texas", out_dir=tmp_path / "out", session=web.session(), work_dir=tmp_path)
    assert not any(u.endswith(".zip") for u in web.urls()) and _empty(tmp_path / "out")


def test_nsrdb_refuses_to_build_when_the_notice_clauses_are_gone(
    web: FakeWeb, tmp_path: pathlib.Path
) -> None:
    _nlr_web(web)
    web.page(nrel_nsrdb.TERMS_URL, "<p>All rights reserved. Contact us to license the Data.</p>")
    with pytest.raises(TermsChanged, match="entire notice appears in all copies"):
        nrel_nsrdb.build(region="texas", out_dir=tmp_path / "out", session=web.session())
    assert not any(u.endswith(".zip") for u in web.urls())


def test_nsrdb_builds_from_recorded_zips_without_a_terms_request(
    web: FakeWeb, tmp_path: pathlib.Path
) -> None:
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    for kind in ("ghi", "dni"):
        (snaps / f"nsrdbv3_{kind}.zip").write_bytes(_nsrdb_zip(kind))
    out = nrel_nsrdb.build(
        region="texas", out_dir=tmp_path / "out", snapshot_dir=snaps, session=web.session()
    )
    assert {o["layer_id"] for o in out} == {"solar_ghi", "solar_dni"} and web.requests == []
    assert sorted(p.name for p in snaps.iterdir()) == ["nsrdbv3_dni.zip", "nsrdbv3_ghi.zip"], "inputs kept"
    with pytest.raises(FileNotFoundError, match=r"Annual GHI/nsrdb3_ghi\.tif not in empty\.zip"):
        nrel_nsrdb.extract_member(
            _write(tmp_path / "empty.zip", _zip({"x.txt": b""})),
            "Annual GHI/nsrdb3_ghi.tif",
            tmp_path / "x.tif",
        )


def _write(path: pathlib.Path, data: bytes) -> pathlib.Path:
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ USGS NSHM seismic
NSHM_CSV = (FIX / "usgs_nshm" / "nshm_map_pga_pecos.csv").read_bytes()
SCIENCEBASE_CC0 = {
    "title": "2023 NSHM CONUS hazard curves",
    "rights": "Creative Commons Zero v1.0 Universal (CC0-1.0) public domain dedication",
}


def test_nshm_reads_one_member_of_the_remote_zip_and_records_it(web: FakeWeb, tmp_path: pathlib.Path) -> None:
    web.json(usgs_nshm.TERMS_URL, SCIENCEBASE_CC0)
    archive = _zip(
        {
            usgs_nshm.MEMBER.replace("/PGA/", "/SA0P2/"): b"lon,lat,2475\n" + b"0,0,0\n" * 4000,
            usgs_nshm.MEMBER: NSHM_CSV,
        }
    )
    web.file(usgs_nshm.ZIP_URL, archive, last_modified="Mon, 10 Aug 2026 00:00:00 GMT")
    out = usgs_nshm.build(
        region="texas", out_dir=tmp_path / "out", session=web.session(), store=Store(tmp_path)
    )
    assert out[0]["summary"]["grid_nodes_in_file"] == 99
    assert out[0]["provenance"]["source_url"] == f"{usgs_nshm.ZIP_URL}#{usgs_nshm.MEMBER}"
    (snapshot,) = (tmp_path / "snapshots" / usgs_nshm.SOURCE_ID).glob("*.csv")
    assert snapshot.read_bytes() == NSHM_CSV, "the member read is kept as the immutable snapshot"
    (run,) = _runs(tmp_path, usgs_nshm.SOURCE_ID)
    assert run["member_bytes"] == len(NSHM_CSV) and run["zip_bytes"] == len(archive)
    assert run["last_modified"] == "Mon, 10 Aug 2026 00:00:00 GMT"
    assert all(r.get("status") == 206 for r in run["requests"][1:]) and len(run["requests"]) == 4
    first = NSHM_CSV.decode().splitlines()[1].split(",")
    with GeoTiff(tmp_path / "out" / "seismic_pga_2pct50.tif") as tif:
        assert tif.value_at(float(first[0]), float(first[1])) == pytest.approx(float(first[4]), abs=1e-4)


def test_nshm_stops_before_any_range_request_when_the_dedication_is_gone(
    web: FakeWeb, tmp_path: pathlib.Path
) -> None:
    web.json(usgs_nshm.TERMS_URL, {"rights": "All rights reserved"})
    with pytest.raises(TermsChanged):
        usgs_nshm.build(
            region="texas", out_dir=tmp_path / "out", session=web.session(), store=Store(tmp_path)
        )
    assert web.urls() == [usgs_nshm.TERMS_URL] and not (tmp_path / "snapshots").exists()


def test_nshm_csv_header_variants_and_bad_rows() -> None:
    lons, lats, vals = usgs_nshm.parse_map_csv(
        b"LON, LAT ,475yr,2475-year\n"
        b"-100.0,30.0,0.1,0.2\n"
        b"-100.05,30.0,0.1\n"  # too short: skipped
        b"bad,30.0,0.1,0.3\n"  # not a number: skipped
        b"-100.05,30.05,0.1,0.4\n"
    )
    assert (
        lons.tolist() == [-100.0, -100.05] and lats.tolist() == [30.0, 30.05] and vals.tolist() == [0.2, 0.4]
    )
    grid = usgs_nshm.to_grid(lons, lats, vals)
    assert grid.data.shape == (2, 2) and np.isnan(grid.data[0, 1]) and grid.data[0, 0] == 0.4
    with pytest.raises(ParseError, match="no lon/lat"):
        usgs_nshm.parse_map_csv(b"x,y,2475\n1,2,3\n")
    with pytest.raises(ParseError, match="no rows"):
        usgs_nshm.parse_map_csv(b"lon,lat,2475\n")


# ------------------------------------------------------------------ PAD-US protected lands
PADUS_ITEM = {"title": "PAD-US 4.1", "rights": "<p>Public Domain.</p>"}
PADUS_LAYERS = ["PADUS4_1Fee_State_TX", "PADUS4_1Designation_State_TX", "PADUS4_1Easement_State_TX"]
#: A feature in Alaska, which a CONUS-wide reader could yield: the lon/lat box test drops it.
ALASKA = {
    "properties": {"Unit_Nm": "Denali National Park", "GAP_Sts": "1", "feature_class": "Fee"},
    "geometry": {
        "type": "MultiPolygon",
        "coordinates": [[[[-151.0, 63.0], [-150.0, 63.0], [-150.0, 63.5], [-151.0, 63.0]]]],
    },
}


class FakeGdal:
    """Stands in for pyogrio: the layer names of the extracted geodatabase and the recorded features."""

    def __init__(self) -> None:
        self.opened: list[pathlib.Path] = []
        self.files: list[str] = []
        self.boxes: list[Any] = []

    def list_gdb_layers(self, gdb: pathlib.Path) -> list[str]:
        self.opened.append(gdb)
        self.files = sorted(p.name for p in gdb.iterdir())
        return PADUS_LAYERS

    def read_gdb(
        self, gdb: pathlib.Path, layer: str, *, counts: dict[str, int], bbox_lonlat: Any
    ) -> Iterator[dict[str, Any]]:
        self.boxes.append(bbox_lonlat)
        fc = "Fee" if "Fee" in layer else "Designation"
        feats = [
            f for f in _features("padus", "padus_tx_sample.geojson") if f["properties"]["feature_class"] == fc
        ]
        if fc == "Fee":
            feats.append(ALASKA)
        counts["features"] = len(feats)
        yield from feats


def test_padus_is_downloaded_read_filtered_and_cleaned_up(
    web: FakeWeb, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gdal = FakeGdal()
    monkeypatch.setattr(usgs_padus, "list_gdb_layers", gdal.list_gdb_layers)
    monkeypatch.setattr(usgs_padus, "read_gdb", gdal.read_gdb)
    web.json(usgs_padus.TERMS_URL, PADUS_ITEM)
    web.file(
        usgs_padus.file_url("texas"),
        _zip(
            {
                "PADUS4_1_State_TX.gdb/a00000001.gdbtable": b"t",
                "PADUS4_1_State_TX.gdb/gdb": b"g",
                "PADUS4_1_State_TX.kmz": b"not needed",
            }
        ),
    )
    work = tmp_path / "work"
    work.mkdir()
    (out,) = usgs_padus.build(
        region="texas", out_dir=tmp_path / "out", session=web.session(), store=Store(tmp_path), work_dir=work
    )
    assert gdal.files == ["a00000001.gdbtable", "gdb"] and gdal.opened[0].name == "PADUS4_1_State_TX.gdb"
    assert gdal.boxes == [REGIONS["texas"], REGIONS["texas"]], "the region box is handed to the reader"
    assert _empty(work), "the zip and the extracted geodatabase are deleted"
    summary = out["summary"]
    assert summary["by_feature_class"]["Fee"] == 5, "Alaska is outside the Texas box"
    # The recorded Lonesome Ridge designation has an empty outline: the writer drops it.
    assert summary["rows"] == 7 and summary["dropped_geometry"] == 1
    assert summary["hard_constraint_rows"] == 3, "GAP 1-2: two national parks and a national seashore"
    assert summary["read"] == {"Fee": {"features": 6}, "Designation": {"features": 3}}
    assert out["coverage"]["kind"] == "states" and out["coverage"]["states"] == ["Texas"]
    assert out["provenance"]["source_url"] == usgs_padus.file_url("texas")
    names = pq.read_table(tmp_path / "out" / "protected_lands.parquet").column("unit_name").to_pylist()
    assert "Denali National Park" not in names and "Big Bend National Park" in names
    (run,) = _runs(tmp_path, usgs_padus.SOURCE_ID)
    assert run["rows"] == 7 and [r["url"] for r in run["requests"]] == [
        usgs_padus.TERMS_URL,
        usgs_padus.file_url("texas"),
    ]
    assert out["provenance"]["retrieved_at"] == run["requests"][1]["retrieved_at"]


def test_padus_never_deletes_a_snapshot_it_was_given(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gdal = FakeGdal()
    monkeypatch.setattr(usgs_padus, "list_gdb_layers", gdal.list_gdb_layers)
    monkeypatch.setattr(usgs_padus, "read_gdb", gdal.read_gdb)
    snapshot = _write(tmp_path / "PADUS4_1_State_TX_GDB_KMZ.zip", _zip({"PADUS4_1_State_TX.gdb/gdb": b"g"}))
    (tmp_path / "PADUS4_1_State_TX_GDB_KMZ.json").write_text(
        json.dumps({"downloaded_at": "2026-10-21T09:00:00Z"})
    )
    (out,) = usgs_padus.build(
        region="texas", out_dir=tmp_path / "out", snapshot=snapshot, store=Store(tmp_path)
    )
    assert snapshot.exists() and out["provenance"]["retrieved_at"] == "2026-10-21T09:00:00Z"
    gdb = tmp_path / "given.gdb"
    gdb.mkdir()
    (gdb / "gdb").write_bytes(b"g")
    usgs_padus.build(region="texas", out_dir=tmp_path / "out", snapshot=gdb)
    assert gdal.opened[-1] == gdb and (gdb / "gdb").exists(), "an extracted geodatabase is read in place"
    (run,) = _runs(tmp_path, usgs_padus.SOURCE_ID)
    assert run["requests"] == [] and run["rows"] == 7


def test_padus_normalise_falls_back_and_skips() -> None:
    rows, geoms = usgs_padus.normalise(
        [
            {
                "properties": {
                    "Loc_Nm": "Local name",
                    "Category": "Easement",
                    "GAP_Sts": "x",
                    "GIS_Acres": "?",
                },
                "geometry": {"type": "MultiPolygon"},
            },
            {"properties": {"Unit_Nm": "No outline"}, "geometry": None},
        ],
        "Fee",
    )
    assert len(rows) == len(geoms) == 1
    assert rows[0]["unit_name"] == "Local name" and rows[0]["category"] == "Easement"
    assert (
        rows[0]["gap_status"] is None and rows[0]["gis_acres"] is None and rows[0]["hard_constraint"] is False
    )


# ------------------------------------------------------------------ WRI Aqueduct water stress
WRI_TERMS = (
    "<p>We have adopted the Creative Commons Attribution 4.0 International License as our standard license "
    "for publications and data.</p>"
)


def test_aqueduct_is_downloaded_read_for_the_region_and_cleaned_up(
    web: FakeWeb, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[pathlib.Path, Any, list[str]]] = []

    def read_gdb(gdb: pathlib.Path, bbox: Any) -> Iterator[dict[str, Any]]:
        seen.append((gdb, bbox, sorted(p.name for p in gdb.iterdir())))
        yield from _features("wri_aqueduct", "aqueduct_tx_sample.geojson")

    monkeypatch.setattr(wri_aqueduct, "read_gdb", read_gdb)
    web.page(wri_aqueduct.TERMS_URL, WRI_TERMS)
    web.file(wri_aqueduct.URL, _zip({"Aq40_Y2023D07M05.gdb/a1.gdbtable": b"t", "Aq40_Y2023D07M05.csv": b"c"}))
    work = tmp_path / "work"
    work.mkdir()
    (out,) = wri_aqueduct.build(
        region="texas", out_dir=tmp_path / "out", session=web.session(), store=Store(tmp_path), work_dir=work
    )
    assert seen[0][0].name == "Aq40_Y2023D07M05.gdb" and seen[0][1] == REGIONS["texas"]
    assert seen[0][2] == ["a1.gdbtable"] and _empty(work)
    assert out["summary"]["rows"] == 2 and out["provenance"]["cautions"] == wri_aqueduct.CAUTIONS
    assert out["coverage"]["bbox"] == list(REGIONS["texas"])
    (run,) = _runs(tmp_path, wri_aqueduct.SOURCE_ID)
    assert run["rows"] == 2 and run["requests"][-1]["url"] == wri_aqueduct.URL
    assert {"https://www.wri.org/robots.txt", "https://files.wri.org/robots.txt"} <= set(web.urls())


def test_aqueduct_values_outside_the_data_range_are_no_data() -> None:
    rows, _ = wri_aqueduct.normalise(
        [
            {
                "properties": {
                    "pfaf_id": 751810.0,
                    "bws_raw": float("nan"),
                    "bws_score": -9999,
                    "bws_cat": "x",
                    "name_0": "",
                    "bws_label": None,
                },
                "geometry": {"type": "MultiPolygon"},
            },
            {
                "properties": {
                    "pfaf_id": None,
                    "bws_raw": float("inf"),
                    "bws_score": 4.12345,
                    "bws_cat": -1.0,
                },
                "geometry": {"type": "MultiPolygon"},
            },
            {"properties": {"pfaf_id": 1}, "geometry": None},
        ]
    )
    assert rows[0] == {
        "basin_id": "751810",
        "country": None,
        "admin1": None,
        "raw_ratio": None,
        "score": None,
        "category": None,
        "label": None,
    }
    assert rows[1]["basin_id"] is None and rows[1]["raw_ratio"] is None
    assert rows[1]["score"] == 4.123 and rows[1]["category"] == -1 and len(rows) == 2


# ------------------------------------------------------------------ Global Wind Atlas (manual only)
def _tif_with_epsg(path: pathlib.Path, epsg: int) -> pathlib.Path:
    grid = Grid(data=np.full((4, 4), 640, dtype=np.uint16), west=-102.0, north=35.5, dx=0.0025, dy=0.0025)
    write_cog(path, grid, nodata=65535, scale=0.01)
    raw = path.read_bytes()
    geographic = struct.pack("<4H", 2048, 0, 1, 4326)
    assert raw.count(geographic) == 1
    return _write(path, raw.replace(geographic, struct.pack("<4H", 2048, 0, 1, epsg)))


def test_wind_never_fetches_and_refuses_a_grid_in_another_crs(tmp_path: pathlib.Path) -> None:
    with pytest.raises(gwa_wind.ManualSourceOnly, match="Download the 100 m wind speed GeoTIFF in a browser"):
        gwa_wind.build(region="texas", out_dir=tmp_path)
    nad83 = _tif_with_epsg(tmp_path / "nad83.tif", 4269)
    with pytest.raises(GeoTiffError, match=r"nad83\.tif: expected EPSG:4326, found EPSG:4269"):
        gwa_wind.build(region="texas", out_dir=tmp_path / "out", snapshot=nad83)
    assert not (tmp_path / "out" / "wind_speed_100m.tif").exists()


def test_wind_without_a_download_date_says_its_time_is_the_files(tmp_path: pathlib.Path) -> None:
    import os

    tif = _write(
        tmp_path / "us_wind.tif",
        (FIX / "global_wind_atlas" / "gwa_format_synthetic_amarillo.tif").read_bytes(),
    )
    os.utime(tif, (1_760_000_000, 1_760_000_000))  # 2025-10-09T08:53:20Z
    (out,) = gwa_wind.build(region="texas", out_dir=tmp_path / "out", snapshot=tif, store=Store(tmp_path))
    assert out["provenance"]["retrieved_at"] == "2025-10-09T08:53:20Z"
    assert out["provenance"]["retrieved_at_basis"] == "file modification time (download date not given)"
    (run,) = _runs(tmp_path, gwa_wind.SOURCE_ID)
    assert run["retrieved_at"] == "2025-10-09T08:53:20Z"
    assert run["manual_snapshot"]["snapshot_bytes"] == tif.stat().st_size
    with GeoTiff(tmp_path / "out" / "wind_speed_100m.tif") as built:
        assert built.metadata["downloaded_by_hand"] == "true"


# ------------------------------------------------------------------ the command lines
def _main_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    printed: dict[str, Any] = json.loads(capsys.readouterr().out)
    return printed


@pytest.mark.parametrize(
    ("module", "argv", "layers"),
    [
        (eia_oil_gas, ["--snapshot-dir", str(FIX / "eia_oil_gas")], ["oil_gas_basins", "oil_gas_plays"]),
        (
            netl_energy_communities,
            ["--snapshot-dir", str(FIX / "netl")],
            ["energy_community_coal_closure", "energy_community_ffe"],
        ),
        (nrel_nsrdb, ["--snapshot-dir", str(FIX / "nrel_nsrdb")], ["solar_ghi", "solar_dni"]),
        (
            usgs_nshm,
            ["--snapshot", str(FIX / "usgs_nshm" / "nshm_map_pga_pecos.csv")],
            ["seismic_pga_2pct50"],
        ),
        (usgs_padus, ["--snapshot", str(FIX / "padus" / "padus_tx_sample.geojson")], ["protected_lands"]),
        (
            wri_aqueduct,
            ["--snapshot", str(FIX / "wri_aqueduct" / "aqueduct_tx_sample.geojson")],
            ["water_stress"],
        ),
        (
            gwa_wind,
            [
                "--snapshot",
                str(FIX / "global_wind_atlas" / "gwa_format_synthetic_amarillo.tif"),
                "--downloaded-at",
                "2026-10-20T10:00:00Z",
                "--version",
                "3.3",
            ],
            ["wind_speed_100m"],
        ),
    ],
)
def test_each_builder_runs_from_its_command_line_on_a_snapshot(
    module: Any,
    argv: list[str],
    layers: list[str],
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(module, "Store", lambda: Store(tmp_path / "data"))
    module.main(["--region", "texas", "--out-dir", str(tmp_path / "out"), *argv])
    printed = _main_json(capsys)
    assert [o["layer_id"] for o in printed["layers"]] == layers and printed["elapsed_s"] >= 0
    for o in printed["layers"]:
        assert (tmp_path / "out" / o["file"]).is_file()
        assert {"source_id", "source_url", "retrieved_at", "licence"} <= set(o["provenance"])
    if module is gwa_wind:
        assert printed["layers"][0]["provenance"]["vintage"].startswith("Global Wind Atlas version 3.3")
        assert _runs(tmp_path / "data", gwa_wind.SOURCE_ID)[0]["retrieved_at"] == "2026-10-20T10:00:00Z"
    else:
        assert not (tmp_path / "data" / "runs").exists(), "a snapshot build makes no requests: no run record"
