# ruff: noqa: S101 - asserts are the point (docs/04 E-6)
"""The shared fetch plumbing (common.py) and the ArcGIS pager (arcgis.py), over the fake web in conftest.py:
provenance from data/sources.yaml, streamed downloads, robots.txt, the terms re-read, and paging."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import zipfile
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import HttpBlocked, HttpFailed
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store
from pipeline.context.prospect import arcgis, common
from pipeline.context.prospect.common import FetchLog, TermsChanged
from pipeline.context.prospect.conftest import FakeWeb

HOST = "https://data.example.test"
ROBOTS_NO_PRIVATE = "User-agent: *\nDisallow: /private/\n"


def _set_mtime(path: pathlib.Path, when: str) -> None:
    ts = dt.datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
    os.utime(path, (ts, ts))


# ------------------------------------------------------------------ snapshot times and provenance
def test_a_downloaded_files_sidecar_time_is_its_retrieved_at(tmp_path: pathlib.Path) -> None:
    tif = tmp_path / "wind.tif"
    tif.write_bytes(b"raster")
    _set_mtime(tif, "2026-01-02T03:04:05Z")
    assert common.snapshot_retrieved_at(tif) == "2026-01-02T03:04:05Z", "no sidecar: the file's own time"
    sidecar = tmp_path / "wind.json"
    sidecar.write_text(json.dumps({"downloaded_at": "  2026-10-20T10:00:00Z "}))
    assert common.snapshot_retrieved_at(tif) == "2026-10-20T10:00:00Z"
    for unusable in ("{not json", "[1, 2]", json.dumps({"downloaded_at": "   "}), json.dumps({"other": 1})):
        sidecar.write_text(unusable)
        assert common.snapshot_retrieved_at(tif) == "2026-01-02T03:04:05Z", unusable
    _set_mtime(sidecar, "2026-03-03T00:00:00Z")
    assert common.snapshot_retrieved_at(sidecar) == "2026-03-03T00:00:00Z", "a .json is not its own sidecar"


def test_output_dir_defaults_to_the_prospect_directory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROSPECT_DATA_DIR", str(tmp_path / "prospect"))
    assert common.output_dir() == tmp_path / "prospect" and (tmp_path / "prospect").is_dir()
    assert common.output_dir(tmp_path / "x" / "y") == tmp_path / "x" / "y" and (tmp_path / "x" / "y").is_dir()


def _registry(tmp_path: pathlib.Path, **row: Any) -> Registry:
    path = tmp_path / "sources.yaml"
    path.write_text(json.dumps({"version": 1, "sources": [{"id": "test.source", "name": "Test", **row}]}))
    return Registry(path)


def test_provenance_comes_from_the_quoted_terms_and_refuses_unquoted_ones(tmp_path: pathlib.Path) -> None:
    kwargs: dict[str, Any] = {
        "source_url": f"{HOST}/layer",
        "retrieved_at": "2026-10-22T00:00:00Z",
        "vintage": "2024",
        "licence": "CC BY 4.0",
    }
    with pytest.raises(ValueError, match=r"test\.source: no licence quoted in data/sources\.yaml"):
        common.provenance_for("test.source", registry=_registry(tmp_path), **kwargs)
    reg = _registry(
        tmp_path,
        license="Creative Commons Attribution 4.0.",
        attribution="Data: Test Agency",
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    )
    prov = common.provenance_for("test.source", registry=reg, notice="Keep this notice.", **kwargs)
    assert prov.licence_id == reg.get("test.source").licence_id and prov.licence_id
    assert prov.attribution == "Data: Test Agency" and prov.notice == "Keep this notice."
    assert prov.columns() == {
        "source_id": "test.source",
        "source_url": f"{HOST}/layer",
        "retrieved_at": "2026-10-22T00:00:00Z",
        "licence": "CC BY 4.0",
        "vintage": "2024",
    }
    bare = common.provenance_for(
        "test.source", registry=_registry(tmp_path, license="Public domain."), **kwargs
    )
    assert "licence_url" not in bare.record() and "notice" not in bare.record(), "None fields are left out"
    assert bare.cog_items() == {k: str(v) for k, v in bare.record().items()}
    assert all(isinstance(v, str) for v in prov.cog_items().values())


def test_the_run_record_lists_every_request(tmp_path: pathlib.Path) -> None:
    log = FetchLog("test.source")
    log.add(url=f"{HOST}/a", status=200)
    assert log.write(None, region="texas") is None, "no store: nothing written"
    path = log.write(Store(tmp_path / "data"), region="texas", rows=3)
    assert path is not None and path.parent == tmp_path / "data" / "runs" / "test.source"
    record = json.loads(path.read_text())
    assert record["requests"] == [{"url": f"{HOST}/a", "status": 200}]
    assert record["region"] == "texas" and record["rows"] == 3 and record["retrieved_at"].endswith("Z")


# ------------------------------------------------------------------ downloads
def test_a_download_is_streamed_to_disk_with_its_digest_and_validators(
    web: FakeWeb, tmp_path: pathlib.Path
) -> None:
    payload = os.urandom(5000)
    web.file(f"{HOST}/files/a.zip", payload, last_modified="Tue, 17 Dec 2024 00:00:00 GMT")
    log = FetchLog("test.source")
    info = common.download(
        web.session(), f"{HOST}/files/a.zip", tmp_path / "raw" / "a.zip", log=log, chunk=1024
    )
    assert (tmp_path / "raw" / "a.zip").read_bytes() == payload
    assert (
        info["sha256"]
        == hashlib.sha256(payload).hexdigest()
        == common.sha256_file(tmp_path / "raw" / "a.zip")
    )
    assert info["bytes"] == 5000 and info["status"] == 200
    assert info["last_modified"] == "Tue, 17 Dec 2024 00:00:00 GMT" and info["etag"] == '"v1"'
    assert info["content_type"] == "application/zip" and log.requests == [info]
    assert web.urls()[0] == f"{HOST}/robots.txt", "a file download honours robots.txt"


def test_a_download_that_fails_writes_nothing(web: FakeWeb, tmp_path: pathlib.Path) -> None:
    log = FetchLog("test.source")
    with pytest.raises(HttpFailed, match="answered HTTP 404"):
        common.download(web.session(), f"{HOST}/files/gone.zip", tmp_path / "gone.zip", log=log)
    assert not (tmp_path / "gone.zip").exists() and log.requests == []


def test_robots_txt_stops_a_download_unless_the_caller_waives_it(
    web: FakeWeb, tmp_path: pathlib.Path
) -> None:
    web.page(f"{HOST}/robots.txt", ROBOTS_NO_PRIVATE, content_type="text/plain")
    web.file(f"{HOST}/private/a.zip", b"zip bytes")
    session = web.session()
    with pytest.raises(HttpBlocked, match=r"robots\.txt disallows"):
        common.download(session, f"{HOST}/private/a.zip", tmp_path / "a.zip", log=FetchLog("t"))
    assert not (tmp_path / "a.zip").exists()
    assert f"{HOST}/private/a.zip" not in web.urls(), "the disallowed file was never requested"
    common.download(
        session, f"{HOST}/private/a.zip", tmp_path / "a.zip", log=FetchLog("t"), honour_robots=False
    )
    assert (tmp_path / "a.zip").read_bytes() == b"zip bytes"


def test_get_json_logs_the_request_and_refuses_an_error_status(web: FakeWeb) -> None:
    web.json(f"{HOST}/api/items", {"items": [1, 2]})
    log = FetchLog("t")
    assert common.get_json(web.session(), f"{HOST}/api/items", log=log, params={"f": "json"}) == {
        "items": [1, 2]
    }
    assert log.requests[0]["url"] == f"{HOST}/api/items?f=json"
    assert log.requests[0]["bytes"] == len(json.dumps({"items": [1, 2]}))
    assert f"{HOST}/robots.txt" not in web.urls(), "an API read does not consult robots.txt"
    web.json(f"{HOST}/api/missing", {"error": "nope"}, status=404)
    with pytest.raises(HttpFailed, match="answered HTTP 404"):
        common.get_json(web.session(), f"{HOST}/api/missing", log=log)
    web.json(f"{HOST}/api/down", {}, status=503)
    with pytest.raises(HttpFailed, match="failed after 2 attempts: HTTP 503"):
        common.get_json(web.session(), f"{HOST}/api/down", log=log)


# ------------------------------------------------------------------ terms re-read
PHRASES = (
    "You may use and/or distribute any of our data",
    "U.S. government publications are in the public domain",
)


def test_terms_are_matched_through_markup_whitespace_and_case(web: FakeWeb) -> None:
    web.page(
        f"{HOST}/terms",
        "<html><p>U.S. Government publications\n   are in the <b>public domain</b>.</p>"
        "<p>You may use and/or distribute any of our&nbsp;data, files.</p></html>",
    )
    log = FetchLog("t")
    common.verify_terms(web.session(), f"{HOST}/terms", PHRASES, log=log)
    assert log.requests[-1]["terms_checked"] == 2 and log.requests[-1]["status"] == 200


def test_changed_terms_stop_the_build_and_name_the_missing_clause(web: FakeWeb) -> None:
    web.page(f"{HOST}/terms", "<p>U.S. government publications are in the public domain.</p>")
    log = FetchLog("t")
    with pytest.raises(TermsChanged, match=r"no longer carries: \['You may use and/or distribute"):
        common.verify_terms(web.session(), f"{HOST}/terms", PHRASES, log=log)
    assert log.requests == [], "a failed check is not logged as a checked page"
    with pytest.raises(TermsChanged, match=r"terms page .*/gone answered HTTP 404"):
        common.verify_terms(web.session(), f"{HOST}/gone", PHRASES, log=log)


def test_terms_inside_an_arcgis_item_json_are_read_from_its_strings(web: FakeWeb) -> None:
    """ArcGIS `licenseInfo` is HTML inside JSON: escaped newlines are decoded first, and only
    string values count (a clause that appears only as a key is not a quoted term)."""
    item = {
        "id": 7,
        "tags": ["energy", None, 3.5],
        "licenseInfo": "<p>U.S. government publications\nare in the public domain</p>",
        "more": {"terms": ["<div>You may use and/or\ndistribute any of our data</div>"]},
    }
    web.json(f"{HOST}/item?f=json", item)
    common.verify_terms(web.session(), f"{HOST}/item?f=json", PHRASES, log=FetchLog("t"), honour_robots=False)
    web.json(f"{HOST}/keys?f=json", {PHRASES[0]: True, PHRASES[1]: True})
    with pytest.raises(TermsChanged):
        common.verify_terms(
            web.session(), f"{HOST}/keys?f=json", PHRASES, log=FetchLog("t"), honour_robots=False
        )


def test_a_terms_page_robots_disallows_is_not_read(web: FakeWeb) -> None:
    web.page(f"{HOST}/robots.txt", ROBOTS_NO_PRIVATE, content_type="text/plain")
    web.page(f"{HOST}/private/terms", " ".join(PHRASES))
    with pytest.raises(HttpBlocked):
        common.verify_terms(web.session(), f"{HOST}/private/terms", PHRASES, log=FetchLog("t"))


def test_extract_gdb_takes_the_first_geodatabase_only(tmp_path: pathlib.Path) -> None:
    z = tmp_path / "two.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("__MACOSX/First.gdb/._a", b"resource fork")
        zf.writestr("First.gdb/", b"")
        zf.writestr("First.gdb/a.gdbtable", b"a")
        zf.writestr("Second.gdb/b.gdbtable", b"b")
    gdb = common.extract_gdb(z, tmp_path / "out")
    assert gdb == tmp_path / "out" / "First.gdb" and [p.name for p in gdb.iterdir()] == ["a.gdbtable"]
    assert not (tmp_path / "out" / "Second.gdb").exists() and not (tmp_path / "out" / "__MACOSX").exists()


# ------------------------------------------------------------------ ArcGIS paging
LAYER = f"{HOST}/arcgis/rest/services/Plays/FeatureServer/0"


def _feature(i: int) -> dict[str, Any]:
    return {"type": "Feature", "properties": {"FID": i}, "geometry": {"type": "Point", "coordinates": [i, i]}}


def _pager(pages: list[dict[str, Any]]) -> Any:
    def answer(req: requests.PreparedRequest) -> tuple[int, bytes, dict[str, str]]:
        query = parse_qs(urlsplit(str(req.url)).query)
        page = int(query["resultOffset"][0]) // int(query["resultRecordCount"][0])
        return 200, json.dumps(pages[page]).encode(), {"Content-Type": "application/geo+json"}

    return answer


def test_arcgis_pages_until_a_short_page_without_the_transfer_limit_flag(web: FakeWeb) -> None:
    web.json(LAYER, {"maxRecordCount": 2, "objectIdField": "FID"})
    web.route(
        f"{LAYER}/query",
        _pager(
            [
                {"features": [_feature(0), _feature(1)], "exceededTransferLimit": True},
                {"features": [_feature(2), _feature(3)]},  # full page, no flag: there may be more
                {"features": [_feature(4)], "properties": {"exceededTransferLimit": False}},
            ]
        ),
    )
    log = FetchLog("t")
    bbox = (-106.65, 25.83, -93.5, 36.51)
    got = arcgis.query_geojson(
        web.session(), LAYER, log=log, where="state='TX'", bbox=bbox, max_allowable_offset=0.00005
    )
    assert [f["properties"]["FID"] for f in got] == [0, 1, 2, 3, 4]
    asked = web.params(f"{LAYER}/query")
    assert [p["resultOffset"] for p in asked] == ["0", "2", "4"]
    first = asked[0]
    assert (
        first["resultRecordCount"] == "2"
        and first["orderByFields"] == "FID"
        and first["where"] == "state='TX'"
    )
    assert first["outSR"] == "4326" and first["f"] == "geojson" and first["maxAllowableOffset"] == "5e-05"
    assert first["geometry"] == "-106.65,25.83,-93.5,36.51" and first["inSR"] == "4326"
    assert (
        first["geometryType"] == "esriGeometryEnvelope" and first["spatialRel"] == "esriSpatialRelIntersects"
    )
    assert [r.get("features") for r in log.requests] == [None, 2, 2, 1]


def test_arcgis_page_size_and_defaults(web: FakeWeb) -> None:
    web.json(LAYER, {"name": "no maxRecordCount, no objectIdField"})
    web.route(f"{LAYER}/query", _pager([{"features": [_feature(0)]}, {"features": []}]))
    got = arcgis.query_geojson(web.session(), LAYER, log=FetchLog("t"), page_size=1)
    assert len(got) == 1
    first = web.params(f"{LAYER}/query")[0]
    assert (
        first["resultRecordCount"] == "1" and first["orderByFields"] == "OBJECTID" and first["where"] == "1=1"
    )
    assert "geometry" not in first and "maxAllowableOffset" not in first


def test_arcgis_refuses_error_bodies_failed_pages_and_endless_paging(
    web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    web.json(LAYER, {"error": {"code": 400, "message": "Invalid URL"}})
    with pytest.raises(ParseError, match="Invalid URL"):
        arcgis.query_geojson(web.session(), LAYER, log=FetchLog("t"))
    with pytest.raises(HttpFailed, match="Missing/FeatureServer/0 answered HTTP 404"):
        arcgis.layer_info(web.session(), f"{HOST}/Missing/FeatureServer/0", log=FetchLog("t"))
    web.json(LAYER, {"maxRecordCount": 1})
    with pytest.raises(HttpFailed, match="/query answered HTTP 404"):
        arcgis.query_geojson(web.session(), LAYER, log=FetchLog("t"))
    monkeypatch.setattr(arcgis, "MAX_PAGES", 3)
    web.route(
        f"{LAYER}/query",
        lambda _req: (
            200,
            json.dumps({"features": [_feature(9)], "exceededTransferLimit": True}).encode(),
            {},
        ),
    )
    with pytest.raises(HttpFailed, match="more than 3 pages"):
        arcgis.query_geojson(web.session(), LAYER, log=FetchLog("t"))
    assert len(web.params(f"{LAYER}/query")) == 1 + 3


def test_arcgis_pages_that_are_not_feature_collections_are_refused() -> None:
    with pytest.raises(ParseError, match="not a JSON object"):
        arcgis.parse_features("[1, 2]")
    with pytest.raises(ParseError, match="no features array"):
        arcgis.parse_features({"type": "FeatureCollection", "features": {"0": {}}})
