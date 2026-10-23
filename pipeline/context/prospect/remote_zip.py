"""Read one member of a remote zip with HTTP range requests, without downloading the archive.

USGS publishes the 2023 NSHM hazard grids as one ~1.9 GB zip per site class; the prospect layer needs one
~10 MB member of one of them. A zip's central directory sits at its end, so: range-request the tail,
locate the end-of-central-directory record (ZIP64 when present), read the central directory, find the
member's local header offset, range-request the header to learn the data offset, then range-request the
compressed bytes and inflate them. Every request goes through the polite session and is logged.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from pipeline.connectors.http import HttpFailed, PoliteSession
from pipeline.context.prospect.common import FetchLog, now_iso

TAIL_BYTES = 1 << 20
EOCD_SIG = b"PK\x05\x06"
ZIP64_EOCD_SIG = b"PK\x06\x06"
ZIP64_LOCATOR_SIG = b"PK\x06\x07"
CDIR_SIG = b"PK\x01\x02"
LOCAL_SIG = b"PK\x03\x04"


class ZipFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Member:
    name: str
    method: int
    compressed_size: int
    size: int
    header_offset: int
    crc: int


def parse_central_directory(blob: bytes) -> list[Member]:
    out = []
    i = 0
    while i + 46 <= len(blob) and blob[i : i + 4] == CDIR_SIG:
        (method, _t, _d, crc, csize, usize, nlen, xlen, clen, _disk, _ia, _ea, offset) = struct.unpack_from(
            "<HHHIIIHHHHHII", blob, i + 10
        )
        name = blob[i + 46 : i + 46 + nlen].decode("utf-8", "replace")
        extra = blob[i + 46 + nlen : i + 46 + nlen + xlen]
        if 0xFFFFFFFF in (csize, usize, offset):
            usize, csize, offset = _zip64_extra(extra, usize, csize, offset)
        out.append(Member(name, method, csize, usize, offset, crc))
        i += 46 + nlen + xlen + clen
    return out


def _zip64_extra(extra: bytes, usize: int, csize: int, offset: int) -> tuple[int, int, int]:
    j = 0
    while j + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, j)
        if tag == 0x0001:
            vals = list(struct.unpack_from(f"<{size // 8}Q", extra, j + 4))
            if usize == 0xFFFFFFFF:
                usize = vals.pop(0)
            if csize == 0xFFFFFFFF:
                csize = vals.pop(0)
            if offset == 0xFFFFFFFF:
                offset = vals.pop(0)
            break
        j += 4 + size
    return usize, csize, offset


def locate_directory(tail: bytes, tail_start: int) -> tuple[int, int]:
    """(central directory offset, size) from the archive's last bytes."""
    at = tail.rfind(EOCD_SIG)
    if at < 0:
        raise ZipFormatError("no end-of-central-directory record in the tail")
    (_d, _ds, _n_disk, _n, cd_size, cd_offset) = struct.unpack_from("<HHHHII", tail, at + 4)
    if cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        loc = tail.rfind(ZIP64_LOCATOR_SIG, 0, at)
        if loc < 0:
            raise ZipFormatError("ZIP64 archive without a ZIP64 locator")
        (_disk, eocd64_offset, _total) = struct.unpack_from("<IQI", tail, loc + 4)
        rel = eocd64_offset - tail_start
        if rel < 0 or tail[rel : rel + 4] != ZIP64_EOCD_SIG:
            raise ZipFormatError("ZIP64 end record not in the tail read")
        cd_size, cd_offset = struct.unpack_from("<QQ", tail, rel + 40)
    return cd_offset, cd_size


class RemoteZip:
    def __init__(self, session: PoliteSession, url: str, *, log: FetchLog) -> None:
        self.session, self.url, self.log = session, url, log
        self.size: int | None = None
        self.members: dict[str, Member] = {}
        self.last_modified: str | None = None

    def _range(self, start: int | None, end: int | None) -> bytes:
        header = f"bytes={start}-{end}" if start is not None else f"bytes=-{end}"
        resp = self.session.get(self.url, headers={"Range": header}, honour_robots=False)
        if resp.status_code != 206:
            raise HttpFailed(f"{self.url} answered HTTP {resp.status_code} to a range request (206 expected)")
        crange = resp.headers.get("Content-Range", "")
        if "/" in crange and crange.rsplit("/", 1)[1].isdigit():
            self.size = int(crange.rsplit("/", 1)[1])
        self.last_modified = resp.headers.get("Last-Modified") or self.last_modified
        self.log.add(url=self.url, range=header, status=206, bytes=len(resp.content), retrieved_at=now_iso())
        return bytes(resp.content)

    def open(self) -> RemoteZip:
        tail = self._range(None, TAIL_BYTES)
        if self.size is None:
            raise HttpFailed(f"{self.url}: no total size in Content-Range")
        tail_start = self.size - len(tail)
        cd_offset, cd_size = locate_directory(tail, tail_start)
        if cd_offset >= tail_start:
            blob = tail[cd_offset - tail_start : cd_offset - tail_start + cd_size]
        else:
            blob = self._range(cd_offset, cd_offset + cd_size - 1)
        self.members = {m.name: m for m in parse_central_directory(blob)}
        return self

    def read(self, name: str) -> bytes:
        m = self.members.get(name)
        if m is None:
            raise KeyError(f"{name} not in {self.url}")
        head = self._range(m.header_offset, m.header_offset + 29)
        if head[:4] != LOCAL_SIG:
            raise ZipFormatError(f"no local header at {m.header_offset}")
        nlen, xlen = struct.unpack_from("<HH", head, 26)
        start = m.header_offset + 30 + nlen + xlen
        data = self._range(start, start + m.compressed_size - 1)
        if m.method == 0:
            raw = data
        elif m.method == 8:
            raw = zlib.decompress(data, -15)
        else:
            raise ZipFormatError(f"compression method {m.method} not supported")
        if zlib.crc32(raw) & 0xFFFFFFFF != m.crc:
            raise ZipFormatError(f"CRC mismatch for {name}")
        return raw
