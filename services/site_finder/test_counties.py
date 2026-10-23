"""TIGER/Line counties (`services/site_finder/counties.py`): reading a county zip, keeping the states
asked for, the lookup, the vendored fallback, and a rebuild that fetches nothing when unchanged. A
tiny shapefile is written in memory with pyshp's own writer; nothing is downloaded."""

from __future__ import annotations

import io
import pathlib
import zipfile
from typing import Any

import pytest

from services.site_finder import counties as c


def _county_zip(rows: list[tuple[str, str, str, list[tuple[float, float]]]]) -> bytes:
    import shapefile  # type: ignore[import-untyped]

    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    for field in ("STATEFP", "GEOID", "NAME", "NAMELSAD"):
        w.field(field, "C", size=40)
    for statefp, geoid, name, ring in rows:
        w.poly([ring])
        w.record(statefp, geoid, name, f"{name} County")
    w.close()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for ext, buf in (("shp", shp), ("shx", shx), ("dbf", dbf)):
            zf.writestr(f"tl_2026_us_county.{ext}", buf.getvalue())
    return out.getvalue()


SQUARE_TX = [(-98.0, 31.0), (-98.0, 32.0), (-97.0, 32.0), (-97.0, 31.0), (-98.0, 31.0)]
SQUARE_OK = [(-98.0, 34.0), (-98.0, 35.0), (-97.0, 35.0), (-97.0, 34.0), (-98.0, 34.0)]
ZIP = _county_zip([("48", "48027", "Bell", SQUARE_TX), ("40", "40019", "Carter", SQUARE_OK)])


def test_counties_from_zip_keeps_the_states_asked_for_with_provenance() -> None:
    table = c.counties_from_zip(
        ZIP, states=["TX"], url=c.URL, retrieved_at="2026-10-23T00:00:00Z", licence="PD", vintage="v"
    )
    assert table.column("geoid").to_pylist() == ["48027"]
    row = table.drop_columns(["geometry"]).to_pylist()[0]
    assert row["state_code"] == "US-TX" and row["namelsad"] == "Bell County"
    assert (row["source_id"], row["source_url"], row["retrieved_at"]) == (
        c.SOURCE_ID,
        c.URL,
        "2026-10-23T00:00:00Z",
    )
    every = c.counties_from_zip(ZIP, states=None, url=c.URL, retrieved_at="t", licence="PD", vintage="v")
    assert every.num_rows == 2


def test_the_index_names_the_county_a_point_is_in(tmp_path: pathlib.Path) -> None:
    import pyarrow.parquet as pq

    path = c.output_path(tmp_path)
    path.parent.mkdir(parents=True)
    pq.write_table(
        c.counties_from_zip(ZIP, states=None, url=c.URL, retrieved_at="t", licence="PD", vintage="v"), path
    )
    index = c.CountyIndex.load(tmp_path)
    found = index.lookup([-97.5, -97.5, -90.0], [31.5, 34.5, 40.0])
    assert [f.fips if f else None for f in found] == ["48027", "40019", None]
    assert index.provenance["source_id"] == c.SOURCE_ID
    assert index.provenance["note"] == c.BOUNDARY_NOTE
    assert "TIGER/Line®" in (index.provenance["attribution_text"] or "")


def test_without_the_file_the_vendored_counties_answer_and_say_so(tmp_path: pathlib.Path) -> None:
    index = c.CountyIndex.load(tmp_path)
    assert index.provenance["source_id"] == c.FALLBACK_SOURCE_ID
    (travis,) = index.lookup([-97.7404], [30.2747])  # the Texas Capitol
    assert travis is not None and (travis.fips, travis.state_code) == ("48453", "US-TX")


class _Resp:
    def __init__(self, headers: dict[str, str], body: bytes = b"") -> None:
        self.headers, self._body = headers, body

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, size: int) -> Any:
        for i in range(0, len(self._body), size):
            yield self._body[i : i + size]


class _Session:
    def __init__(self, body: bytes, last_modified: str) -> None:
        self.body, self.last_modified, self.gets = body, last_modified, 0

    def request(self, method: str, url: str, **_: Any) -> _Resp:
        assert method == "HEAD"
        return _Resp({"Last-Modified": self.last_modified, "Content-Length": str(len(self.body))})

    def get(self, url: str, **_: Any) -> _Resp:
        self.gets += 1
        return _Resp({}, self.body)


def test_build_writes_once_and_fetches_nothing_when_the_file_is_unchanged(tmp_path: pathlib.Path) -> None:
    session = _Session(ZIP, "Wed, 07 Oct 2026 01:45:57 GMT")
    first = c.build(tmp_path, states=["TX"], session=session)  # type: ignore[arg-type]
    assert first["fetched"] is True and first["counties"] == 1 and session.gets == 1
    assert not list(c.output_path(tmp_path).parent.glob("tiger-*")), "the download is deleted"
    again = c.build(tmp_path, states=["TX"], session=session)  # type: ignore[arg-type]
    assert again["fetched"] is False and session.gets == 1
    session.last_modified = "Thu, 08 Oct 2026 00:00:00 GMT"
    assert c.build(tmp_path, states=["TX"], session=session)["fetched"] is True  # type: ignore[arg-type]


def test_a_short_download_is_refused(tmp_path: pathlib.Path) -> None:
    class Short(_Session):
        def request(self, method: str, url: str, **_: Any) -> _Resp:
            return _Resp({"Last-Modified": "x", "Content-Length": str(len(self.body) + 10)})

    with pytest.raises(RuntimeError, match="announced"):
        c.build(tmp_path, states=["TX"], session=Short(ZIP, "x"))  # type: ignore[arg-type]
    assert not c.output_path(tmp_path).exists()
