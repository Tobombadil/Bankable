"""us.epa.echo.icis_air — EPA ICIS-Air facilities that are data centres, placed through FRS (national).

What the source is (measured 2026-09-29, docs/25 §3.7). EPA ECHO's weekly ICIS-Air bulk download
(`ICIS-AIR_downloads.zip`, 70,252,181 bytes, Last-Modified 2026-09-27) holds ten CSVs; this connector
needs one, `ICIS-AIR_FACILITIES.csv` (19 columns, 280,208 facilities, one row per ICIS-Air programme
id `PGM_SYS_ID`). It is the national register of facilities in the Clean Air Act stationary-source
programme; a data centre registers because its backup generators need an air permit. The file has
no coordinates, only `REGISTRY_ID`, the EPA Facility Registry Service (FRS) id.

Placement comes from ECHO's own FRS-derived facility table, the ECHO Exporter (`echo_exporter.zip`,
443,452,854 bytes, one member `ECHO_EXPORTER.csv`, 3,221,273 facilities). The FRS national download
on the same path (`frs_downloads.zip`, 368,601,232 bytes; `FRS_FACILITIES.csv` carries only
`LATITUDE_MEASURE`/`LONGITUDE_MEASURE`, per ECHO's FRS data dictionary) was rejected because it has
no collection method or accuracy, so it cannot say which points are real. The Exporter's
`FAC_LAT`/`FAC_LONG` are, in ECHO's column dictionary (echo_exporter_columns_7-16-2025_0.xlsx),
"from the FRS EPA Locational Reference Tables (LRT) file which represents the most accurate value
for the facility", with `FAC_COLLECTION_METHOD`, `FAC_REFERENCE_POINT` and `FAC_ACCURACY_METERS`
("the estimate of accuracy based on provided spatial metadata") from the same FRS record.

Fetch (politeness). Both files are on `echo.epa.gov/files/`, which robots.txt allows (`Crawl-delay:
10`; the manifest's 0.1 rps is that delay). `echodata.epa.gov` (the ECHO REST host) answers
`Disallow: *` and is never used. ICIS-Air: the zip's central directory is read with one ranged GET
of its last 64 KiB, then one ranged GET fetches only the facilities member (10.5 MB compressed of
70 MB), checked against the directory's CRC-32; a server that ignores `Range` gets the whole zip
read normally. Exporter: one GET of the whole zip (its single member is one deflate stream, so no
range helps), streamed to a temporary file in 1 MiB pieces and read row by row, filtered to the
candidates' registry ids. The stored snapshot is that filtered pair of tables, not the 513 MB of
upstream bytes (the same choice the Virginia connector makes with its server-side `where`): the
fetch-side pre-filter (`is_candidate`) is looser than the selection rule, and `parse` applies the
rule, so a fixture proves it. Upstream sizes, Last-Modified, ETag and row totals are kept in the run
record (`RawSnapshot.meta`).

Refresh cost (docs/25 §3.9). Both GETs are conditional on the previous run's validators
(`If-None-Match` + `If-Modified-Since`, which echo.epa.gov answers with 304), and the ICIS tail GET
keeps its `Range`, so a changed file costs no extra request. Both 304: the stored snapshot's bytes
come back verbatim and the runner records the run `unchanged` (meta `upstream: unchanged`), for two
requests and no body. ICIS changed, Exporter 304: the stored FRS rows are reused, filtered to the
new candidates, but only when they already cover every registry id now wanted; otherwise the
Exporter is downloaded unconditionally. ICIS 304, Exporter changed: the stored candidate table is
reused and the Exporter downloaded. A snapshot made by other fetch code (`FETCH_VERSION`), or one
without both files' validators, is never reused.

Selection (`select_basis`, measured 2026-09-29, docs/25 §3.7). A facility is kept when it is not
`Permanently Closed` and one of:
- `name`: `FACILITY_NAME` names a data centre (`DATA CENTER`/`CENTRE`, `DATACENTER`, `DATA CTR`, or a
  name ICIS truncated to end in `DATA CENT`);
- `naics_518210`: `NAICS_CODES` carries 518210 (computing infrastructure / data processing and
  hosting), unless it is co-coded with mining or oil-and-gas extraction or support (211xxx,
  212xxx, 213xxx: flare-gas crypto-mining generator sets at well pads) or the name declares a
  non-data-centre function (`NON_DC_NAME_RE`: headquarters, BPO office, paper mill, a flare-gas
  crypto-mining pad);
- `naics_541513_operator`: NAICS 541513 or 541519 (computer facilities management, other computer
  services) and the name carries a colocation or hyperscale operator (`OPERATOR_RE`). 541513 on its
  own also selects offices (L.L. Bean, Deere & Co, an IBM environmental-affairs office).
Precision on a hand-checked random sample and recall against Virginia DEQ's own data-centre flag
are in docs/25 §3.7; neither rule is perfect and the basis travels on every row.

Record model: identical to `us.va.deq.data_center_air_sites` (docs/25 §3.3). One `proposal` per
ICIS-Air facility, `kind = technology = load`, `capacity_mw` null (ICIS-Air publishes no capacity),
`sponsor_name` null (the facility name mixes owner, SPV and campus code).
- source_record_id: `PGM_SYS_ID` (unique over all 280,208 rows). source_url: ECHO's Detailed
  Facility Report for the FRS id (a page for people; never fetched by this connector), else the zip.
- lifecycle_state: `AIR_OPERATING_STATUS_DESC` through `status_map.yaml` (Planned Facility -> filed,
  Under Construction -> under_construction, Operating / Temporarily Closed / Seasonal -> built;
  blank -> unknown).
- location: `Latitude`/`Longitude` are written to `raw` (which `services/ingest/loader.py` promotes
  to an `exact` point for a `raw_ok` source) only when FRS says the point is the site
  (`placement_for`): a site-specific collection method (`EXACT_METHODS`, or a GPS method), a
  stated accuracy of at most `EXACT_MAX_ACCURACY_M` metres, and a point inside the facility's own
  state. Every other row carries no coordinate at all, only FRS's method and accuracy, and is
  placed by the loader from its county name (county centroid) or state (state centroid); a ZIP or
  county centroid from FRS is never passed off as a point.
- county: for an exact point, the county the point falls in (vendored Census boundaries), so the
  county and the point never disagree; otherwise ICIS-Air's `COUNTY_NAME` ("Undetermined" and blank
  read as missing). The two agree on 365 of the 376 exact rows that carry an ICIS county; the 11
  others are mostly independent-city lines (Manassas) and a few ICIS entries that name the wrong
  county (Microsoft MKE 3B, Mount Pleasant WI: ICIS "Richland", point in Racine). `COUNTY_NAME` stays
  in `raw`.
- cross_refs: `icis_air:<PGM_SYS_ID>|frs:<REGISTRY_ID>`. Virginia DEQ emits the same
  `icis_air:<PLA_ICIS_ID>` token, and `pipeline/resolve.py`'s D3 pass pairs records of two sources
  that cite the same identifier, so a Virginia facility present in both becomes one proposal.

Personal data (docs/13 §5.4): ICIS-AIR_FACILITIES.csv has no contact, person or phone column; the
Exporter columns kept (`EXPORTER_COLUMNS`) are facility identity and location only (its
demographic and compliance columns are never read). `CONTACT_COLUMN` strips any contact-like
column should either file gain one. Some facility names are a sole proprietor's name; none is
among the selected rows.

Terms: US federal government work, public domain (17 U.S.C. §105; docs/13 §2.12 EPA hedge).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import pathlib
import re
import struct
import tempfile
import time
import zipfile
import zlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import IO, Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot, utcnow
from pipeline.connectors.canonical import harmonise_status, norm_county, norm_name
from pipeline.context.geo import StateIndex

log = logging.getLogger(__name__)

ICIS_URL = "https://echo.epa.gov/files/echodownloads/ICIS-AIR_downloads.zip"
ICIS_MEMBER = "ICIS-AIR_FACILITIES.csv"
EXPORTER_URL = "https://echo.epa.gov/files/echodownloads/echo_exporter.zip"
EXPORTER_MEMBER = "ECHO_EXPORTER.csv"
DFR_URL = "https://echo.epa.gov/detailed-facility-report?fid={registry_id}"

ICIS_COLUMNS = (
    "PGM_SYS_ID",
    "REGISTRY_ID",
    "FACILITY_NAME",
    "STREET_ADDRESS",
    "CITY",
    "COUNTY_NAME",
    "STATE",
    "ZIP_CODE",
    "EPA_REGION",
    "SIC_CODES",
    "NAICS_CODES",
    "FACILITY_TYPE_CODE",
    "AIR_POLLUTANT_CLASS_CODE",
    "AIR_POLLUTANT_CLASS_DESC",
    "AIR_OPERATING_STATUS_CODE",
    "AIR_OPERATING_STATUS_DESC",
    "CURRENT_HPV",
    "LOCAL_CONTROL_REGION_CODE",
    "LOCAL_CONTROL_REGION_NAME",
)
REQUIRED_ICIS = (
    "PGM_SYS_ID",
    "REGISTRY_ID",
    "FACILITY_NAME",
    "STATE",
    "NAICS_CODES",
    "AIR_OPERATING_STATUS_DESC",
)
#: The Exporter columns kept (identity + FRS location metadata). Everything else is never read.
EXPORTER_COLUMNS = (
    "REGISTRY_ID",
    "FAC_STATE",
    "FAC_COUNTY",
    "FAC_LAT",
    "FAC_LONG",
    "FAC_COLLECTION_METHOD",
    "FAC_REFERENCE_POINT",
    "FAC_ACCURACY_METERS",
    "FAC_DERIVED_STCTY_FIPS",
    "AIR_IDS",
)
CONTACT_COLUMN = re.compile(r"CONTACT|PHONE|EMAIL|E_MAIL|PERSON", re.I)

# ------------------------------------------------------------------ selection
#: "DATA CENT" at the very end catches a name ICIS cut at 40 characters
#: ("CENTRA HEALTH ADMINISTRATION - DATA CENT").
NAME_RE = re.compile(r"DATA\s*CENT(?:ER|RE)|DATACENT|\bDATA\s*CENT\s*$|\bDATA\s*CTR\b", re.I)
PREFILTER_NAICS = frozenset({"518210", "541513", "541519"})
EXTRACTION_NAICS = re.compile(r"^21[123]\d{3}$")
#: Names that declare a non-data-centre function: an office (headquarters, BPO), a paper mill, or a
#: flare-gas crypto-mining generator set at an oil and gas well site ("NYDIG DFM - VENETA PAD",
#: "GRMR OIL AND GAS - DEAL GULCH PRODUCTION"; DFM = digital flare mitigation).
NON_DC_NAME_RE = re.compile(
    r"\bHEADQUARTERS\b|\bBPO\b|\bPAPER\s+MILL\b|\bOIL\s*(?:AND|&)\s*GAS\b|\bPAD\b|\bDFM\b", re.I
)
OPERATOR_RE = re.compile(
    r"\b(?:EQUINIX|DIGITAL\s+REALTY|QTS|QUALITY\s+INVESTMENTS?\s+PROPERTIES|CYRUS\s*ONE|VANTAGE\s+DATA|"
    r"COLOGIX|ALIGNED\s+(?:ENERGY|DATA)|CORESITE|FLEXENTIAL|DATABANK|EDGE\s*CONNEX|STACK\s+INFRASTRUCTURE|"
    r"COMPASS\s+DATACENTERS|SABEY|NTT\s+GLOBAL|H5\s+DATA|T5\s*@|IRON\s+MOUNTAIN\s+DATA|CYXTERA|PEAK\s+10|"
    r"TELX|SWITCH\s+LTD|NOVVA|AMAZON\s+DATA\s+SERVICES|VADATA)\b",
    re.I,
)
CLOSED_STATUS = "Permanently Closed"


def naics_codes(value: Any) -> list[str]:
    """`NAICS_CODES` is space-separated ("211130 518210"); tolerate commas/semicolons too."""
    return [c for c in re.split(r"[\s,;]+", str(value or "").strip()) if c]


def is_candidate(row: dict[str, Any]) -> bool:
    """Fetch-side pre-filter, deliberately looser than `select_basis` (no status or exclusion test)."""
    if NAME_RE.search(str(row.get("FACILITY_NAME") or "")):
        return True
    return bool(PREFILTER_NAICS.intersection(naics_codes(row.get("NAICS_CODES"))))


def select_basis(row: dict[str, Any]) -> str | None:
    """Why an ICIS-Air facility is a data centre (module docstring); None = not selected."""
    if str(row.get("AIR_OPERATING_STATUS_DESC") or "").strip() == CLOSED_STATUS:
        return None
    name = str(row.get("FACILITY_NAME") or "")
    if NAME_RE.search(name):
        return "name"
    codes = naics_codes(row.get("NAICS_CODES"))
    if "518210" in codes:
        if any(EXTRACTION_NAICS.match(c) for c in codes) or NON_DC_NAME_RE.search(name):
            return None
        return "naics_518210"
    if {"541513", "541519"}.intersection(codes) and OPERATOR_RE.search(name):
        return "naics_541513_operator"
    return None


# ------------------------------------------------------------------ placement
#: FRS collection methods that locate the site itself (address point, imagery, map, survey).
#: Centroids (ZIP, county, state, place, census block), block-face / intersection / street-centre
#: address matches, public-land-survey sections, "UNKNOWN" and blank are not exact.
EXACT_METHODS = frozenset(
    {
        "ADDRESS MATCHING-HOUSE NUMBER",
        "ADDRESS MATCHING (GEOCODING)",
        "GDT-ADDRESS MATCHING (GEOCODING)",
        "ADDRESS MATCHING-DIGITIZED",
        "THE GEOGRAPHIC COORDINATE DETERMINATION METHOD BASED ON ADDRESS MATCHING",
        "INTERPOLATION-PHOTO",
        "INTERPOLATION-SATELLITE",
        "INTERPOLATION-MAP",
        "CLASSICAL SURVEYING TECHNIQUES",
        "THE GEOGRAPHIC COORDINATE DETERMINATION METHOD BASED ON GPS",
    }
)
EXACT_MAX_ACCURACY_M = 200.0
COORD_DECIMALS = 6
REGIONS = pathlib.Path(__file__).resolve().parents[3] / "data" / "vendored" / "regions"


class _PreciseIndex(StateIndex):
    """`StateIndex` with its lookup cache at ~1 cm, so a point near a border is never answered from
    an earlier point's cache cell."""

    GRID = 10_000_000.0


@lru_cache(maxsize=1)
def _states() -> _PreciseIndex:
    doc = json.loads((REGIONS / "us_states.geojson").read_text(encoding="utf-8"))
    return _PreciseIndex(doc.get("features", []))


@lru_cache(maxsize=1)
def _counties() -> tuple[_PreciseIndex, dict[str, str]]:
    doc = json.loads((REGIONS / "us_counties.geojson").read_text(encoding="utf-8"))
    feats = doc.get("features", [])
    names = {str(f["properties"]["region_id"]): str(f["properties"]["name"]) for f in feats}
    return _PreciseIndex(feats), names


def county_for_point(lon: float, lat: float) -> str | None:
    """County name for a WGS84 point (independent cities, FIPS xx510+, keep a "city" suffix)."""
    index, names = _counties()
    geoid = index.lookup(lon, lat)
    if not geoid:
        return None
    name = names.get(geoid)
    if name and int(geoid[-3:]) >= 510 and geoid[:2] in {"24", "29", "32", "51"}:
        return f"{name} city"
    return name


def _float(value: Any) -> float | None:
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def placement_for(frs: dict[str, Any] | None, state: str) -> tuple[str, float | None, float | None]:
    """(placement, lat, lon). `exact` only when FRS says the point is the site (module docstring);
    otherwise no coordinate is returned and the reason is the placement label."""
    if not frs:
        return "no_frs_record", None, None
    lat, lon = _float(frs.get("FAC_LAT")), _float(frs.get("FAC_LONG"))
    if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
        return "no_coordinate", None, None
    method = str(frs.get("FAC_COLLECTION_METHOD") or "").strip().upper()
    if not (method in EXACT_METHODS or method.startswith("GPS")):
        return "method_not_site_specific", None, None
    accuracy = _float(frs.get("FAC_ACCURACY_METERS"))
    if accuracy is None or accuracy > EXACT_MAX_ACCURACY_M:
        return "accuracy_not_stated_or_coarse", None, None
    if state and _states().lookup(lon, lat) != f"US-{state}":
        return "point_outside_state", None, None
    return "exact", round(lat, COORD_DECIMALS), round(lon, COORD_DECIMALS)


def _clean_county(value: Any) -> str | None:
    v = str(value or "").strip()
    return None if not v or v.upper() in {"UNDETERMINED", "UNKNOWN", "N/A"} else v


# ------------------------------------------------------------------ zip members over HTTP ranges
TAIL_BYTES = 65_536
LOCAL_HEADER_SLACK = 1_024
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)")


@dataclass(frozen=True)
class ZipMember:
    name: str
    method: int
    crc: int
    compressed_size: int
    size: int
    header_offset: int


def central_directory(tail: bytes, total_size: int) -> list[ZipMember]:
    """Members listed in a zip's central directory, read from the file's last `len(tail)` bytes."""
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0 or eocd + 22 > len(tail):
        raise ParseError("zip end-of-central-directory record not in the fetched tail")
    count, _cd_size, cd_offset = struct.unpack("<HII", tail[eocd + 10 : eocd + 20])
    z64 = tail.rfind(b"PK\x06\x06", 0, eocd)
    if z64 >= 0:
        (count,) = struct.unpack("<Q", tail[z64 + 32 : z64 + 40])
        _cd_size, cd_offset = struct.unpack("<QQ", tail[z64 + 40 : z64 + 56])
    p = cd_offset - (total_size - len(tail))
    if p < 0:
        raise ParseError("zip central directory starts before the fetched tail")
    members: list[ZipMember] = []
    for _ in range(count):
        if tail[p : p + 4] != b"PK\x01\x02":
            raise ParseError("zip central directory entry signature mismatch")
        (method,) = struct.unpack("<H", tail[p + 10 : p + 12])
        crc, csize, usize = struct.unpack("<III", tail[p + 16 : p + 28])
        name_len, extra_len, comment_len = struct.unpack("<HHH", tail[p + 28 : p + 34])
        (offset,) = struct.unpack("<I", tail[p + 42 : p + 46])
        name = tail[p + 46 : p + 46 + name_len].decode("utf-8", "replace")
        extra = tail[p + 46 + name_len : p + 46 + name_len + extra_len]
        if 0xFFFFFFFF in (csize, usize, offset):
            q = 0
            while q + 4 <= len(extra):
                header_id, size = struct.unpack("<HH", extra[q : q + 4])
                if header_id == 1:
                    n = size // 8
                    vals = list(struct.unpack("<" + "Q" * n, extra[q + 4 : q + 4 + 8 * n]))
                    if usize == 0xFFFFFFFF and vals:
                        usize = vals.pop(0)
                    if csize == 0xFFFFFFFF and vals:
                        csize = vals.pop(0)
                    if offset == 0xFFFFFFFF and vals:
                        offset = vals.pop(0)
                q += 4 + size
        members.append(ZipMember(name, method, crc, csize, usize, offset))
        p += 46 + name_len + extra_len + comment_len
    return members


def inflate_member(buf: bytes, member: ZipMember) -> bytes:
    """Decompress one member from bytes that start at its local file header; CRC and size checked."""
    if buf[:4] != b"PK\x03\x04":
        raise ParseError(f"{member.name}: local file header signature mismatch")
    name_len, extra_len = struct.unpack("<HH", buf[26:30])
    start = 30 + name_len + extra_len
    data = buf[start : start + member.compressed_size]
    if len(data) != member.compressed_size:
        raise ParseError(f"{member.name}: ranged read truncated ({len(data)} of {member.compressed_size})")
    if member.method == 0:
        out = data
    elif member.method == 8:
        out = zlib.decompress(data, -15)
    else:
        raise ParseError(f"{member.name}: unsupported zip compression method {member.method}")
    if len(out) != member.size or zlib.crc32(out) != member.crc:
        raise ParseError(f"{member.name}: CRC or size mismatch after ranged read")
    return out


# ------------------------------------------------------------------ conditional requests
#: Version of what `fetch` stores: the pre-filter, the columns kept and the document layout. A stored
#: snapshot stands in for a file that answered 304 only when this same version made it, so bump
#: it whenever `is_candidate`, `_drop_contact`, `EXPORTER_COLUMNS` or the document shape changes.
FETCH_VERSION = 1
DOWNLOAD_CHUNK = 1 << 20


def conditional_headers(validators: Mapping[str, Any] | None) -> dict[str, str]:
    """`If-None-Match` / `If-Modified-Since` from one file's validators as a previous run recorded
    them. echo.epa.gov (Apache) answers 304 to both, ranged or not (measured 2026-09-29, docs/25
    §3.9); with both sent, the ETag decides (RFC 9110 §13.2.2)."""
    out: dict[str, str] = {}
    if isinstance(validators, Mapping):
        if validators.get("etag"):
            out["If-None-Match"] = str(validators["etag"])
        if validators.get("last_modified"):
            out["If-Modified-Since"] = str(validators["last_modified"])
    return out


def _not_modified_meta(url: str, resp: Any, validators: Mapping[str, Any]) -> dict[str, Any]:
    """Meta for a file that answered 304. Apache's 304 repeats the ETag but not Last-Modified, so
    the previous value is carried forward for the next run's `If-Modified-Since`."""
    return {
        "url": url,
        "last_modified": resp.headers.get("Last-Modified") or validators.get("last_modified"),
        "etag": resp.headers.get("ETag") or validators.get("etag"),
        "not_modified": True,
        "bytes_fetched": 0,
    }


def _carried(previous: Mapping[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """A 304 file's meta: the size and row totals the previous run measured, then this request's."""
    keep = (
        "zip_bytes",
        "ranged",
        "member_compressed",
        "member_size",
        "rows_total",
        "candidates",
        "rows_kept",
    )
    return {**{k: previous[k] for k in keep if k in previous}, **current}


def _csv_rows(text_lines: Iterable[str]) -> Iterator[list[str]]:
    return csv.reader(text_lines)


def _drop_contact(columns: list[str]) -> list[int]:
    return [i for i, c in enumerate(columns) if not CONTACT_COLUMN.search(c)]


# ------------------------------------------------------------------ connector
class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.epa.echo.icis_air"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = True  # echo.epa.gov robots.txt allows /files/ (Crawl-delay 10)
    status_key: ClassVar[str] = "epa_icis_air"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    dq_required_fields: ClassVar[tuple[str, ...]] = ("name_canonical", "state")
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "PGM_SYS_ID",
        "REGISTRY_ID",
        "FACILITY_NAME",
        "STATE",
        "COUNTY_NAME",
        "NAICS_CODES",
        "AIR_OPERATING_STATUS_DESC",
    )

    # ------------------------------------------------------------------ fetch
    def _get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 300,
        not_modified: bool = False,
    ) -> Any:
        r = self.http.get(url, honour_robots=self.honour_robots, headers=headers or {}, timeout=timeout)
        if r.status_code not in ((200, 206, 304) if not_modified else (200, 206)):
            raise ConnectorError(f"GET {url} -> HTTP {r.status_code}")
        return r

    def _fetch_member(
        self, url: str, member_name: str, validators: dict[str, Any] | None = None
    ) -> tuple[bytes | None, dict[str, Any]]:
        """One zip member through two ranged GETs (tail, then the member), or the whole zip when the
        server ignores `Range`. With `validators` the tail GET is conditional: a 304 returns
        `(None, meta)` and nothing else is fetched."""
        cond = conditional_headers(validators)
        tail = self._get(url, {"Range": f"bytes=-{TAIL_BYTES}", **cond}, timeout=120, not_modified=bool(cond))
        if tail.status_code == 304:
            return None, _not_modified_meta(url, tail, validators or {})
        meta: dict[str, Any] = {
            "url": url,
            "last_modified": tail.headers.get("Last-Modified"),
            "etag": tail.headers.get("ETag"),
            "not_modified": False,
        }
        if tail.status_code == 200:
            meta.update(bytes_fetched=len(tail.content), ranged=False, zip_bytes=len(tail.content))
            with zipfile.ZipFile(io.BytesIO(tail.content)) as z:
                return z.read(member_name), meta
        m = _CONTENT_RANGE.search(str(tail.headers.get("Content-Range") or ""))
        if not m:
            raise ConnectorError(f"GET {url} answered 206 without a Content-Range header")
        total = int(m.group(3))
        members = {z.name: z for z in central_directory(tail.content, total)}
        if member_name not in members:
            raise ParseError(f"{url} has no member {member_name}; members: {sorted(members)}")
        mem = members[member_name]
        end = min(
            total - 1, mem.header_offset + 30 + len(mem.name) + LOCAL_HEADER_SLACK + mem.compressed_size
        )
        body = self._get(url, {"Range": f"bytes={mem.header_offset}-{end}"}, timeout=300)
        if body.headers.get("Last-Modified") != meta["last_modified"]:
            raise ConnectorError(f"{url} changed between ranged reads; retry on the next run")
        if body.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(body.content)) as z:
                data = z.read(member_name)
        else:
            data = inflate_member(body.content, mem)
        meta.update(
            zip_bytes=total,
            ranged=True,
            bytes_fetched=len(tail.content) + len(body.content),
            member_compressed=mem.compressed_size,
            member_size=mem.size,
        )
        return data, meta

    def _download(
        self, url: str, validators: dict[str, Any] | None = None
    ) -> tuple[IO[bytes] | None, dict[str, Any]]:
        """The whole file, streamed to a temporary file in `DOWNLOAD_CHUNK` pieces, so the 443 MB
        Exporter never sits in memory. With `validators` the GET is conditional: a 304 returns
        `(None, meta)`. The caller closes the file."""
        cond = conditional_headers(validators)
        r = self.http.get(url, honour_robots=self.honour_robots, headers=cond, timeout=600, stream=True)
        try:
            if r.status_code == 304 and cond:
                return None, _not_modified_meta(url, r, validators or {})
            if r.status_code != 200:
                raise ConnectorError(f"GET {url} -> HTTP {r.status_code}")
            fh = tempfile.TemporaryFile()
            try:
                size = 0
                for chunk in r.iter_content(DOWNLOAD_CHUNK):
                    fh.write(chunk)
                    size += len(chunk)
                expected = str(r.headers.get("Content-Length") or "")
                if expected.isdigit() and not r.headers.get("Content-Encoding") and int(expected) != size:
                    raise ConnectorError(f"GET {url} truncated: {size} of {expected} bytes")
                fh.seek(0)
            except BaseException:
                fh.close()
                raise
        finally:
            close = getattr(r, "close", None)
            if close is not None:
                close()
        meta = {
            "url": url,
            "last_modified": r.headers.get("Last-Modified"),
            "etag": r.headers.get("ETag"),
            "not_modified": False,
            "zip_bytes": size,
            "bytes_fetched": size,
        }
        return fh, meta

    def _reusable_previous(self) -> tuple[bytes, dict[str, Any], dict[str, Any]] | None:
        """(bytes, document, meta) of the stored snapshot when this fetch code made it and both
        files' validators are recorded; None means fetch everything unconditionally."""
        prev = self.previous
        if prev is None:
            return None
        meta = prev.meta
        if meta.get("fetch_version") != FETCH_VERSION:
            return None
        if not all(conditional_headers(meta.get(k)) for k in ("icis", "exporter")):
            return None
        try:
            content = prev.content()
            doc = json.loads(content)
            icis, frs = doc["icis"], doc["frs"]
            if list(frs["columns"]) != list(EXPORTER_COLUMNS) or "REGISTRY_ID" not in icis["columns"]:
                return None
        except Exception as e:  # a stored object we cannot use costs one full download, no more
            log.warning("previous snapshot not reusable: %r", e, extra={"source_id": self.source_id})
            return None
        return content, doc, meta

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        before = self.http.requests_made
        prev = self._reusable_previous()
        prev_doc = prev[1] if prev else {}
        prev_meta = prev[2] if prev else {}

        icis_bytes, icis_meta = self._fetch_member(ICIS_URL, ICIS_MEMBER, prev_meta.get("icis"))
        if icis_bytes is None:
            # 304: the stored candidate table is what this code would cut from the same file.
            icis_cols = list(prev_doc["icis"]["columns"])
            icis_rows = [list(r) for r in prev_doc["icis"]["rows"]]
            icis_total = prev_meta["icis"].get("rows_total")
            icis_meta = _carried(prev_meta["icis"], icis_meta)
        else:
            reader = _csv_rows(io.StringIO(icis_bytes.decode("utf-8", "replace"), newline=""))
            header = next(reader)
            missing = [c for c in REQUIRED_ICIS if c not in header]
            if missing:
                raise ParseError(f"{ICIS_MEMBER} lacks {missing}: {header}")
            keep = _drop_contact(header)
            icis_cols = [header[i] for i in keep]
            icis_total = 0
            icis_rows = []
            for rec in reader:
                icis_total += 1
                row = dict(zip(header, rec, strict=False))
                if is_candidate(row):
                    icis_rows.append([rec[i] if i < len(rec) else "" for i in keep])
            pk = icis_cols.index("PGM_SYS_ID")
            icis_rows.sort(key=lambda r: r[pk])
        wanted = {r[icis_cols.index("REGISTRY_ID")] for r in icis_rows} - {""}

        # The Exporter is asked "changed since?" only when the stored FRS rows cover every id now
        # wanted: they were cut for the previous candidates, so a new id needs the file itself.
        prev_wanted: set[str] = set()
        if prev:
            rid = list(prev_doc["icis"]["columns"]).index("REGISTRY_ID")
            prev_wanted = {r[rid] for r in prev_doc["icis"]["rows"]} - {""}
        covered = bool(prev) and wanted <= prev_wanted
        fh, exp_meta = self._download(EXPORTER_URL, prev_meta.get("exporter") if covered else None)
        if fh is None:
            if icis_bytes is None and prev is not None:
                return self._unchanged(prev, icis_meta, exp_meta, t0, before)
            exp_rows = [list(r) for r in prev_doc["frs"]["rows"] if r[0] in wanted]
            exp_total = prev_meta["exporter"].get("rows_total")
            exp_meta = _carried(prev_meta["exporter"], exp_meta)
        else:
            exp_total = 0
            exp_rows = []
            with fh, zipfile.ZipFile(fh) as z, z.open(EXPORTER_MEMBER) as member:
                ereader = _csv_rows(io.TextIOWrapper(member, encoding="utf-8", errors="replace", newline=""))
                eheader = next(ereader)
                absent = [c for c in EXPORTER_COLUMNS if c not in eheader]
                if absent:
                    raise ParseError(f"{EXPORTER_MEMBER} lacks {absent}")
                idx = [eheader.index(c) for c in EXPORTER_COLUMNS]
                rid = eheader.index("REGISTRY_ID")
                for rec in ereader:
                    exp_total += 1
                    if len(rec) > rid and rec[rid] in wanted:
                        exp_rows.append([rec[i] if i < len(rec) else "" for i in idx])
            exp_rows.sort(key=lambda r: r[0])
        doc = {
            "icis": {"url": ICIS_URL, "member": ICIS_MEMBER, "columns": icis_cols, "rows": icis_rows},
            "frs": {
                "url": EXPORTER_URL,
                "member": EXPORTER_MEMBER,
                "columns": list(EXPORTER_COLUMNS),
                "rows": exp_rows,
            },
        }
        return RawSnapshot(
            content=json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            content_type="application/json",
            url=ICIS_URL,
            retrieved_at=utcnow(),
            http_status=200,
            ext="json",
            headers={"Last-Modified": str(icis_meta.get("last_modified"))},
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=self.http.requests_made - before,
            meta={
                "fetch_version": FETCH_VERSION,
                "upstream": "changed",
                "icis": {**icis_meta, "rows_total": icis_total, "candidates": len(icis_rows)},
                "exporter": {**exp_meta, "rows_total": exp_total, "rows_kept": len(exp_rows)},
            },
        )

    def _unchanged(
        self,
        prev: tuple[bytes, dict[str, Any], dict[str, Any]],
        icis_meta: dict[str, Any],
        exp_meta: dict[str, Any],
        t0: float,
        before: int,
    ) -> RawSnapshot:
        """Both files answered 304: the stored bytes, verbatim, so the runner's SHA comparison
        records the run `unchanged` (docs/20 §3.2). The meta says why and keeps the validators and
        row totals for the next run."""
        content, _, prev_meta = prev
        carried = {
            "icis": _carried(prev_meta["icis"], icis_meta),
            "exporter": _carried(prev_meta["exporter"], exp_meta),
        }
        return RawSnapshot(
            content=content,
            content_type="application/json",
            url=ICIS_URL,
            retrieved_at=utcnow(),
            http_status=304,
            ext="json",
            headers={"Last-Modified": str(icis_meta.get("last_modified"))},
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=self.http.requests_made - before,
            meta={"fetch_version": FETCH_VERSION, "upstream": "unchanged", **carried},
        )

    def redact(self, content: bytes) -> bytes:
        """Strip any contact-like column from either table; bytes untouched when there is none."""
        try:
            doc = json.loads(content)
        except json.JSONDecodeError:
            return content
        stripped = False
        for key in ("icis", "frs"):
            table = doc.get(key) if isinstance(doc, dict) else None
            if not isinstance(table, dict):
                continue
            cols = list(table.get("columns") or [])
            keep = _drop_contact(cols)
            if len(keep) != len(cols):
                table["columns"] = [cols[i] for i in keep]
                table["rows"] = [[r[i] for i in keep] for r in table.get("rows") or []]
                stripped = True
        if not stripped:
            return content
        return json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")

    # ------------------------------------------------------------------ parse
    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        try:
            doc = json.loads(raw.content)
        except json.JSONDecodeError as e:
            raise ParseError(f"ICIS-Air snapshot is not JSON: {e}") from e
        if (
            not isinstance(doc, dict)
            or not isinstance(doc.get("icis"), dict)
            or not isinstance(doc.get("frs"), dict)
        ):
            raise ParseError("ICIS-Air snapshot lacks its icis/frs tables")
        icis_cols = list(doc["icis"].get("columns") or [])
        missing = [c for c in REQUIRED_ICIS if c not in icis_cols]
        if missing:
            raise ParseError(f"ICIS-Air facilities table lacks {missing}")
        frs_cols = list(doc["frs"].get("columns") or [])
        if "REGISTRY_ID" not in frs_cols:
            raise ParseError("FRS table lacks REGISTRY_ID")
        frs: dict[str, dict[str, Any]] = {}
        for rec in doc["frs"].get("rows") or []:
            r = dict(zip(frs_cols, rec, strict=False))
            frs.setdefault(str(r["REGISTRY_ID"]), r)
        rows: list[dict[str, Any]] = []
        placements: dict[str, int] = {}
        for rec in doc["icis"].get("rows") or []:
            icis = dict(zip(icis_cols, rec, strict=False))
            basis = select_basis(icis)
            if basis is None:
                continue
            state = str(icis.get("STATE") or "").strip().upper()
            fac = frs.get(str(icis.get("REGISTRY_ID") or "")) if icis.get("REGISTRY_ID") else None
            placement, lat, lon = placement_for(fac, state)
            row: dict[str, Any] = dict(icis)
            row["select_basis"] = basis
            row["frs_joined"] = fac is not None
            row["frs_collection_method"] = (fac or {}).get("FAC_COLLECTION_METHOD") or None
            row["frs_reference_point"] = (fac or {}).get("FAC_REFERENCE_POINT") or None
            row["frs_accuracy_m"] = _float((fac or {}).get("FAC_ACCURACY_METERS"))
            row["placement"] = placement
            if placement == "exact":
                row["Latitude"], row["Longitude"] = lat, lon
            placements[placement] = placements.get(placement, 0) + 1
            rows.append(row)
        raw.meta["rows_selected"] = len(rows)
        raw.meta["placement"] = dict(sorted(placements.items()))
        return rows

    # ------------------------------------------------------------------ normalize
    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        harmonised = [
            harmonise_status(
                self.status_key, {"status_raw": r.get("AIR_OPERATING_STATUS_DESC")}, self.status_map
            )
            for r in rows
        ]
        counties: list[str | None] = []
        for r in rows:
            c = None
            if r.get("Latitude") is not None:
                c = county_for_point(float(r["Longitude"]), float(r["Latitude"]))
            counties.append(c or _clean_county(r.get("COUNTY_NAME")))
        ids = [str(r.get("PGM_SYS_ID") or "").strip() for r in rows]
        regs = [str(r.get("REGISTRY_ID") or "").strip() for r in rows]
        df = pd.DataFrame(
            {
                "source_record_id": ids,
                "source_url": [DFR_URL.format(registry_id=g) if g else ICIS_URL for g in regs],
                "kind": "load",
                "name_canonical": [r.get("FACILITY_NAME") for r in rows],
                "name_norm": [norm_name(r.get("FACILITY_NAME")) for r in rows],
                "sponsor_name": None,
                "sponsor_norm": None,
                "technology": "load",
                "technology_raw": "Data Center",
                "capacity_mw": pd.array([None] * len(rows), dtype="Float64"),
                "storage_mwh": pd.array([None] * len(rows), dtype="Float64"),
                "iso": None,
                "state": [str(r.get("STATE") or "").strip().upper() or None for r in rows],
                "county": counties,
                "county_norm": [norm_county(c) for c in counties],
                "lifecycle_state": [s for s, _ in harmonised],
                "status_raw": [r.get("AIR_OPERATING_STATUS_DESC") or None for r in rows],
                "status_rule": [rule for _, rule in harmonised],
                "status_conflict": False,
                "queue_date": pd.NaT,
                "proposed_cod": pd.NaT,
                "queue_id": None,
                "eia_plant_id": None,
                "eia_generator_id": None,
                "cross_refs": [
                    "|".join([f"icis_air:{i}"] + ([f"frs:{g}"] if g else []))
                    for i, g in zip(ids, regs, strict=True)
                ],
            },
            index=range(len(rows)),
        )
        return self.finalize(df, rows, raw)
