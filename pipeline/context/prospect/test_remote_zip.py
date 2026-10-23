# ruff: noqa: S101 - asserts are the point (docs/04 E-6)
"""remote_zip.RemoteZip over a fake range-serving host: one member read without the archive, ZIP64 included,
and every malformed answer refused with a reason rather than read as data."""

from __future__ import annotations

import io
import struct
import zipfile
import zlib

import pytest

from pipeline.connectors.http import HttpFailed
from pipeline.context.prospect import remote_zip
from pipeline.context.prospect.common import FetchLog
from pipeline.context.prospect.conftest import FakeWeb
from pipeline.context.prospect.remote_zip import RemoteZip, ZipFormatError

URL = "https://files.example.test/hazard/conus.zip"
CSV = b"lon,lat,475,975,2475\n" + b"".join(b"-103.%02d,31.40,0.01,0.02,0.03\n" % i for i in range(60))


def _zip(members: dict[str, tuple[bytes, int]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, (data, method) in members.items():
            zf.writestr(name, data, compress_type=method)
    return buf.getvalue()


def _zip64(members: dict[str, bytes]) -> bytes:
    """A stored-member ZIP64 archive as large-archive tools write it: every size and offset in the central
    directory is 0xFFFFFFFF with the real values in the 0x0001 extra field, and the end record points at a
    ZIP64 end record through the locator. (zipfile writes these only past 4 GB, so they are built here.)"""
    out = bytearray()
    central = bytearray()
    for name, data in members.items():
        raw_name, crc, offset = name.encode(), zlib.crc32(data), len(out)
        local_extra = struct.pack("<HHQQ", 1, 16, len(data), len(data))
        out += struct.pack(
            "<4sHHHHHIIIHH", b"PK\x03\x04", 45, 0, 0, 0, 0x21, crc, 0xFFFFFFFF, 0xFFFFFFFF, len(raw_name), 20
        )
        out += raw_name + local_extra + data
        cd_extra = struct.pack("<HHQQQ", 1, 24, len(data), len(data), offset)
        central += struct.pack(
            "<4sHHHHHHIIIHHHHHII",
            b"PK\x01\x02",
            45,
            45,
            0,
            0,
            0,
            0x21,
            crc,
            0xFFFFFFFF,
            0xFFFFFFFF,
            len(raw_name),
            len(cd_extra),
            0,
            0,
            0,
            0,
            0xFFFFFFFF,
        )
        central += raw_name + cd_extra
    cd_offset = len(out)
    out += central
    eocd64_offset = len(out)
    n = len(members)
    out += struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, n, n, len(central), cd_offset)
    out += struct.pack("<4sIQI", b"PK\x06\x07", 0, eocd64_offset, 1)
    out += struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    return bytes(out)


def _open(web: FakeWeb, data: bytes, *, last_modified: str | None = None) -> tuple[RemoteZip, FetchLog]:
    web.file(URL, data, last_modified=last_modified)
    log = FetchLog("test.remote_zip")
    return RemoteZip(web.session(), URL, log=log).open(), log


def test_one_member_is_read_by_range_requests_without_the_archive(web: FakeWeb) -> None:
    filler = bytes(range(256)) * 400  # ~100 KB the build never needs
    data = _zip(
        {
            "conus/vs30-760/PGA/map.csv": (CSV, zipfile.ZIP_DEFLATED),
            "conus/vs30-760/SA1P0/map.csv": (filler, zipfile.ZIP_STORED),
            "conus/README.txt": (b"stored as is", zipfile.ZIP_STORED),
        }
    )
    rz, log = _open(web, data, last_modified="Mon, 10 Aug 2026 12:00:00 GMT")
    assert rz.size == len(data)
    assert set(rz.members) == {
        "conus/vs30-760/PGA/map.csv",
        "conus/vs30-760/SA1P0/map.csv",
        "conus/README.txt",
    }
    assert rz.read("conus/vs30-760/PGA/map.csv") == CSV
    assert rz.read("conus/README.txt") == b"stored as is"
    assert rz.last_modified == "Mon, 10 Aug 2026 12:00:00 GMT"
    # The whole archive fits in the tail read, so: the tail, then a header and a body per member read.
    ranges = [r["range"] for r in log.requests]
    assert ranges[0] == f"bytes=-{remote_zip.TAIL_BYTES}" and len(ranges) == 5
    assert all(r["status"] == 206 for r in log.requests)
    pga = rz.members["conus/vs30-760/PGA/map.csv"]
    assert pga.method == zipfile.ZIP_DEFLATED and pga.compressed_size < pga.size == len(CSV)
    assert [r["bytes"] for r in log.requests[1:3]] == [30, pga.compressed_size], "header, then the member"
    assert all("Inframation/" in str(r.headers["User-Agent"]) for r in web.requests)
    assert not any(str(r.url).endswith("/robots.txt") for r in web.requests), "range reads are API reads"


def test_a_central_directory_before_the_tail_is_fetched_with_its_own_range(
    web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _zip({f"member_{i:02d}_with_a_long_name.csv": (CSV, zipfile.ZIP_DEFLATED) for i in range(5)})
    monkeypatch.setattr(remote_zip, "TAIL_BYTES", 30)  # the end record only
    rz, log = _open(web, data)
    cd_offset, cd_size = remote_zip.locate_directory(data[-30:], len(data) - 30)
    assert [r["range"] for r in log.requests] == ["bytes=-30", f"bytes={cd_offset}-{cd_offset + cd_size - 1}"]
    assert len(rz.members) == 5 and rz.read("member_03_with_a_long_name.csv") == CSV


def test_a_zip64_archive_is_read_through_its_zip64_records(web: FakeWeb) -> None:
    data = _zip64({"conus/PGA/map.csv": CSV, "conus/PGV/map.csv": b"lon,lat,2475\n-100,30,1\n"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:  # the standard library agrees the archive is valid
        assert zf.testzip() is None and zf.read("conus/PGA/map.csv") == CSV
    rz, _log = _open(web, data)
    member = rz.members["conus/PGV/map.csv"]
    assert member.size == member.compressed_size == 23 and member.header_offset == 30 + 17 + 20 + len(CSV)
    assert rz.read("conus/PGA/map.csv") == CSV
    assert rz.read("conus/PGV/map.csv") == b"lon,lat,2475\n-100,30,1\n"


def test_zip64_records_missing_from_the_tail_are_refused(
    web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _zip64({"a.csv": CSV})
    eocd = data[-22:]
    without_locator = data[:-42] + eocd
    with pytest.raises(ZipFormatError, match="without a ZIP64 locator"):
        remote_zip.locate_directory(without_locator, 0)
    monkeypatch.setattr(remote_zip, "TAIL_BYTES", 50)  # the locator and end record, not the ZIP64 record
    with pytest.raises(ZipFormatError, match="ZIP64 end record not in the tail"):
        _open(web, data)


def test_a_tail_without_an_end_record_is_not_a_zip(web: FakeWeb) -> None:
    with pytest.raises(ZipFormatError, match="no end-of-central-directory"):
        _open(web, b"<html>this is an error page, not an archive</html>" * 10)


def test_a_host_that_ignores_range_requests_is_refused(web: FakeWeb) -> None:
    web.file(URL, _zip({"a.csv": (CSV, zipfile.ZIP_STORED)}), ranges=False)
    with pytest.raises(HttpFailed, match="answered HTTP 200 to a range request"):
        RemoteZip(web.session(), URL, log=FetchLog("t")).open()


def test_a_range_answer_without_a_total_size_is_refused(web: FakeWeb) -> None:
    web.route(URL, lambda _req: (206, b"PK\x05\x06" + bytes(18), {"Content-Range": "bytes 0-21/*"}))
    with pytest.raises(HttpFailed, match="no total size in Content-Range"):
        RemoteZip(web.session(), URL, log=FetchLog("t")).open()


def test_a_member_not_in_the_archive_names_the_archive(web: FakeWeb) -> None:
    rz, _ = _open(web, _zip({"a.csv": (CSV, zipfile.ZIP_STORED)}))
    with pytest.raises(KeyError, match=r"b\.csv not in https://files\.example\.test/hazard/conus\.zip"):
        rz.read("b.csv")


def test_a_corrupt_local_header_is_refused(web: FakeWeb) -> None:
    data = bytearray(_zip({"a.csv": (CSV, zipfile.ZIP_STORED)}))
    data[0:4] = b"XX\x03\x04"
    rz, _ = _open(web, bytes(data))
    with pytest.raises(ZipFormatError, match="no local header at 0"):
        rz.read("a.csv")


def test_a_member_whose_bytes_changed_fails_its_crc(web: FakeWeb) -> None:
    data = bytearray(_zip({"a.csv": (CSV, zipfile.ZIP_STORED)}))
    at = bytes(data).index(b"-103.07")
    data[at : at + 7] = b"-109.07"
    rz, _ = _open(web, bytes(data))
    with pytest.raises(ZipFormatError, match=r"CRC mismatch for a\.csv"):
        rz.read("a.csv")


def test_an_unsupported_compression_method_is_refused(web: FakeWeb) -> None:
    rz, _ = _open(web, _zip({"a.csv": (CSV, zipfile.ZIP_BZIP2)}))
    assert rz.members["a.csv"].method == zipfile.ZIP_BZIP2
    with pytest.raises(ZipFormatError, match="compression method 12 not supported"):
        rz.read("a.csv")
