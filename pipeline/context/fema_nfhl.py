"""FEMA National Flood Hazard Layer (NFHL) -> flood polygons near high-voltage lines, for the
powered-land site finder (owner decision 2026-10-22, `docs/00-PLAN.md`; `docs/25` §5; terms in
`docs/13` §6 and `data/sources.yaml` `us.fema.nfhl`).

**What is produced.** Per state, three GeoParquet files (WKB geometry, OGC:CRS84 lon/lat, a GeoParquet
1.1 `bbox` covering column, rows ordered along a Z-order curve so row-group statistics prune by area):

* ``sfha/<ST>.parquet`` -- the Special Flood Hazard Area: ``S_FLD_HAZ_AR`` polygons whose ``SFHA_TF``
  is true or whose ``FLD_ZONE`` is one of A, AE, AH, AO, AR, A99, V, VE, **clipped to the areas of
  interest** (buffers around 69 kV-and-up lines) and split along the effective FIRM panels, so each
  row names the one panel (``firm_panel``) and that panel's effective date it comes from.
* ``zones_other/<ST>.parquet`` -- the other mapped zones a site report must not call "outside the
  floodplain" without saying so, clipped the same way: shaded X (0.2 % annual chance and
  levee-reduced risk; reported, never excluded), Zone D (hazard undetermined), open water, and
  "AREA NOT INCLUDED" (inside a map's extent, mapped by nobody).
* ``coverage/<ST>.parquet`` -- every effective FIRM panel (``S_FIRM_PAN``), **not** clipped: the
  footprint where FEMA has an effective digital map at all. A point covered by no panel with ``covers``
  true has **no digital flood map**; the finder says "flood data unavailable" there, never "outside".

Unshaded X ("area of minimal flood hazard") is not written. The rule that follows from it needs all
three files *and the areas of interest*, because the zone files hold no row outside them: a site is
outside the SFHA on the effective FIRM only where it lies inside the state's area of interest (the
state's row of that state's version's ``aoi.parquet``) **and** inside a covering panel, and touches no
``sfha``, ``zone_d``, ``not_included`` or ``unrecognised`` polygon. Outside the area of interest the
answer is "flood data unavailable (not computed)", whatever the coverage says. `FloodLayer.assess` is
the reference implementation (`docs/25` §5.3); the manifest repeats the rule for the finder.

A state that was not rebuilt keeps its own version: the manifest names, per state, the key of every
file it reads (``states[ST].files[layer].key``, ``states[ST].aoi.key``), and a partial rebuild
carries the other states forward from the previous manifest rather than dropping them.

**Route (robots.txt read 2026-10-22).** Both FEMA hosts answer ``Disallow: /*?*`` (every URL with a
query string) and ``Disallow: /arcgis``; the Map Service Center adds
``Disallow: /portal/downloadProduct?productID=``. So the NFHL ArcGIS REST services and the MSC
"direct download" links are never used. What is used:

1. one POST to the MSC product search (``/portal/advanceSearch``, a path robots.txt allows; the same
   form the MSC page submits) per state, for a community in that state (``STATE_PROBES``), whose answer
   lists the state's NFHL product (``NFHL_48_20261013.zip``, 2,031 MB on 2026-10-22);
2. the file itself at its static path on ``hazards.fema.gov/nfhlv2/output/State/`` (allowed), read by
   HTTP range requests: the zip's central directory, then only the members of the two layers used
   (``S_FLD_HAZ_AR``, ``S_FIRM_PAN``) and the geodatabase's system tables. Texas: 1.0 GB of 2.0 GB.

A state product is an Esri file geodatabase, read with GDAL through ``pyogrio`` (imported lazily; a
build-only dependency). County products (``hazards.fema.gov/nfhlv2/output/County/48201C_*.zip``) are
shapefile zips and are read with ``pyshp`` when ``pyogrio`` is absent, which is how the recorded
fixtures are tested.

**Areas of interest.** ``aoi`` builds them from a lines file: the LBNL x HIFLD transmission parquet
(``pipeline/context/lbnl_transmission.py``) until the OpenStreetMap lines land, or any GeoParquet of
lines (OGC:CRS84 or EPSG:4326) with a numeric ``voltage_kv`` column in kV. Each line of ``min_kv`` (69)
or more is buffered by ``buffer_miles`` (2, the finder's largest adjacency) plus ``AOI_MARGIN_M``
(200 m: covers the up to 37 m a polygonal buffer at ``quad_segs`` 8 cuts inside the true circle, since GEOS
rounds a join's arc to whole steps, and the simplification below; 43 m worst over every LBNL line),
in pieces at most ``BUFFER_MAX_LAT_SPAN`` degrees of latitude tall so each is scaled at its own latitude,
assigned to every state within ``STATE_SLACK_M`` of its buffer (the state shapes are Census 1:20,000,000
generalisations), and dissolved per state.

**Simplification.** Douglas-Peucker ring by ring (`simplify_polygonal`), tolerance ``tolerance_m``
(10 m) converted at ``M_PER_DEG`` m per degree. A degree of longitude is shorter than a degree of
latitude, so east-west the bound is tighter than needed (cos(latitude) of it), which costs size, not
accuracy; north-south it is ``tolerance_m`` times the true meridional degree over ``M_PER_DEG``: 9.94 m
in Puerto Rico, 9.95-9.99 m in the contiguous US, 10.02 m in northern Alaska. Every kept vertex is
an original one. GEOS's topology-preserving simplifier is not used: it broke the bound on the Texas run.

**Coordinates.** NFHL is in NAD83 geographic (EPSG:4269); coordinates are carried unchanged and declared
OGC:CRS84. NAD83 and WGS84 differ by about 1-2 m in the contiguous US, inside the 10 m tolerance.

CLI (``python -m pipeline.context.fema_nfhl``): ``aoi``, ``product``, ``state``, ``manifest``; see
``main``. ``infra/scripts/build_flood_layer.sh`` runs them in GitHub Actions and uploads to R2.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import io
import itertools
import json
import math
import pathlib
import re
import shutil
import struct
import tempfile
import time
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import numpy as np
import pyarrow as pa  # type: ignore[import-untyped]  # no stubs; pandas reads parquet elsewhere
import pyarrow.parquet as pq  # type: ignore[import-untyped]
import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]
from shapely.strtree import STRtree  # type: ignore[import-untyped,import-not-found,unused-ignore]

from pipeline.connectors.http import HttpBlocked, HttpFailed, PoliteSession
from pipeline.connectors.store import DATA_DIR

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATES_GEOJSON = ROOT / "data" / "vendored" / "regions" / "us_states.geojson"
#: Local layout mirrors the R2 keys under the object store's `data/` prefix
#: (`pipeline/connectors/objectstore.py` DEFAULT_PREFIX), so `aws s3 sync` moves one into the other.
NORMALIZED_DIR = DATA_DIR / "normalized" / "context" / "us.fema.nfhl"
R2_PREFIX = "data/normalized/context/us.fema.nfhl"

SOURCE_ID = "us.fema.nfhl"
LANDING_URL = "https://msc.fema.gov/portal/advanceSearch"
MSC_SEARCH_URL = "https://msc.fema.gov/portal/advanceSearch"
STATE_FILE_URL = "https://hazards.fema.gov/nfhlv2/output/State/{file}"
COUNTY_FILE_URL = "https://hazards.fema.gov/nfhlv2/output/County/{file}"
MSC_HOST = "msc.fema.gov"
HAZARDS_HOST = "hazards.fema.gov"
#: One search POST per 5 s; one range request per 2 s (each carries up to RANGE_MAX bytes).
RATE_LIMITS = {MSC_HOST: 0.2, HAZARDS_HOST: 0.5}
#: TLS handshakes to both hosts failed intermittently from the development egress (2026-10-22);
#: a range request is idempotent, so it is retried more than the session default.
FEMA_MAX_ATTEMPTS = 8
#: robots.txt on msc.fema.gov and hazards.fema.gov (read 2026-10-22). `/*?*` is enforced by
#: refusing any query string at all (the stdlib robots parser does not implement wildcards).
FEMA_DISALLOWED_PREFIXES = (
    "/arcgis",
    "/portal/downloadProduct",
    "/portal/search",
    "/portal/availabilitySearch",
    "/nri/report/viewer",
    "/flood-maps/archived-flood-data",
)
RANGE_MIN = 256 * 1024
RANGE_MAX = 32 * 1024 * 1024
COPY_CHUNK = 16 * 1024 * 1024

#: Zones FEMA defines as the Special Flood Hazard Area (44 CFR 59.1; NFHL `D_Zone` domain).
SFHA_ZONES = frozenset({"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"})
#: `ZONE_SUBTY` values of unshaded X. Any other X subtype is "shaded X" (0.2 % annual chance,
#: levee-reduced, 1 % shallower than a foot or draining under a square mile).
MINIMAL_X_SUBTYPES = frozenset({"", "AREA OF MINIMAL FLOOD HAZARD"})
CATEGORIES = ("sfha", "shaded_x", "zone_d", "open_water", "not_included", "unrecognised")
KNOWN_ZONES = SFHA_ZONES | {"X", "D", "OPEN WATER", "AREA NOT INCLUDED"}
#: Read every zone polygon except unshaded X (classified again in Python; this only saves reading).
#: Each disjunct is NULL-safe: in SQL, ``FLD_ZONE <> 'X'`` is NULL, not true, when FLD_ZONE is NULL, and
#: a NULL filter drops the row. So the rows skipped are exactly zone ``X`` with no subtype or the
#: minimal-hazard one and with ``SFHA_TF`` NULL or ``F``: rows `classify` maps to None anyway.
ZONE_WHERE = (
    "FLD_ZONE IS NULL OR FLD_ZONE <> 'X' OR "
    "(ZONE_SUBTY IS NOT NULL AND ZONE_SUBTY <> 'AREA OF MINIMAL FLOOD HAZARD') OR "
    "(SFHA_TF IS NOT NULL AND SFHA_TF <> 'F')"
)
ZONE_FIELDS = (
    "DFIRM_ID",
    "VERSION_ID",
    "FLD_AR_ID",
    "STUDY_TYP",
    "FLD_ZONE",
    "ZONE_SUBTY",
    "SFHA_TF",
    "STATIC_BFE",
    "V_DATUM",
    "DEPTH",
    "LEN_UNIT",
    "AR_REVERT",
    "AR_SUBTRV",
    "DUAL_ZONE",
    "SOURCE_CIT",
)
PANEL_FIELDS = (
    "DFIRM_ID",
    "VERSION_ID",
    "FIRM_PAN",
    "PANEL_TYP",
    "PRE_DATE",
    "EFF_DATE",
    "SCALE",
    "PNP_REASON",
    "BASE_TYP",
    "SOURCE_CIT",
)
ZONE_LAYER = "S_FLD_HAZ_AR"
PANEL_LAYER = "S_FIRM_PAN"

#: Metres per degree, used for both axes (a degree of longitude as this times cos(latitude)). 111,320 m
#: is the equatorial degree of longitude; the true meridional degree runs from 110,574 m at the equator
#: to 111,694 m at the poles (mean 111,132 m), so a latitude distance converted with this value is off by
#: at most 0.7 %, and by 0.1-0.5 % in the contiguous US. Measurement and buffers only; see the
#: simplification note in the module docstring for what it does to the 10 m bound.
M_PER_DEG = 111_320.0
METRES_PER_MILE = 1_609.344
DEFAULT_TOLERANCE_M = 10.0
DEFAULT_MIN_KV = 69.0
DEFAULT_BUFFER_MILES = 2.0
AOI_MARGIN_M = 200.0
#: A line is buffered in pieces at most this many degrees of latitude tall, each scaled at its own
#: latitude: one cos(latitude) for a 3-degree line left its buffer 98 m short east-west at the north end
#: (LBNL, 2026-10-22); a half-degree piece is at most 1.3 % of the distance short at 71.4 N (44 m of the
#: 3,419 m), 0.5 % at 49 N, inside the 200 m margin.
BUFFER_MAX_LAT_SPAN = 0.5
#: A buffer is assigned to every state whose shape lies within this distance of it. The shapes are the
#: Census 1:20,000,000 cartographic boundaries (median segment 7 km), which can sit kilometres off the
#: true border, and a buffer assigned to no state is a buffer no state's flood files cover.
STATE_SLACK_M = 10_000.0
#: Highest operating voltage anywhere is 1,100 kV; a larger value in a kV column is volts.
MAX_PLAUSIBLE_KV = 1_500.0
#: The 2 x 2 split of a geometry stops below this many vertices (PostGIS ST_Subdivide's idea): the
#: dissolved areas of interest of a state are one network-shaped polygon of 10^5 vertices, and an
#: intersection against it per FIRM panel would be linear in all of them.
SUBDIVIDE_MAX_VERTICES = 512
#: Pieces under a square metre are noding slivers along panel seams, not flood zones.
MIN_PIECE_M2 = 1.0
ROW_GROUP_SIZE = 20_000


# -------------------------------------------------------------------------------------- states
@dataclasses.dataclass(frozen=True)
class StateProbe:
    """A community whose MSC search lists the state's NFHL product (any community does)."""

    state: str
    state_fips: str
    county: str
    cid: str


#: Found 2026-10-22 by MSC product-ID searches of countywide products (``NFHL_<county fips>C``, the
#: first county in FIPS order that has one), whose answers name a community; NFIP community ids do not
#: change. Connecticut uses its pre-2022 county code (FEMA kept them), DC and Puerto Rico their
#: community-wide ids. All 52 answered with one state product (37,120 MB of zips in all).
STATE_PROBES: dict[str, StateProbe] = {
    "AK": StateProbe("AK", "02", "02090", "02090C"),
    "AL": StateProbe("AL", "01", "01001", "010314"),
    "AR": StateProbe("AR", "05", "05001", "05001C"),
    "AZ": StateProbe("AZ", "04", "04001", "040010"),
    "CA": StateProbe("CA", "06", "06001", "060003"),
    "CO": StateProbe("CO", "08", "08001", "080007"),
    "CT": StateProbe("CT", "09", "09001", "090016"),
    "DC": StateProbe("DC", "11", "11001", "110001"),
    "DE": StateProbe("DE", "10", "10001", "100002"),
    "FL": StateProbe("FL", "12", "12001", "120001"),
    "GA": StateProbe("GA", "13", "13001", "13001C"),
    "HI": StateProbe("HI", "15", "15003", "15003C"),
    "IA": StateProbe("IA", "19", "19001", "190792"),
    "ID": StateProbe("ID", "16", "16001", "16001C"),
    "IL": StateProbe("IL", "17", "17001", "17001C"),
    "IN": StateProbe("IN", "18", "18001", "180001"),
    "KS": StateProbe("KS", "20", "20001", "200299"),
    "KY": StateProbe("KY", "21", "21001", "210333"),
    "LA": StateProbe("LA", "22", "22001", "220104"),
    "MA": StateProbe("MA", "25", "25001", "250012"),
    "MD": StateProbe("MD", "24", "24001", "240001"),
    "ME": StateProbe("ME", "23", "23001", "230011"),
    "MI": StateProbe("MI", "26", "26001", "261119"),
    "MN": StateProbe("MN", "27", "27003", "270031"),
    "MO": StateProbe("MO", "29", "29001", "29001C"),
    "MS": StateProbe("MS", "28", "28001", "280001"),
    "MT": StateProbe("MT", "30", "30001", "30001C"),
    "NC": StateProbe("NC", "37", "37001", "370001"),
    "ND": StateProbe("ND", "38", "38003", "380202"),
    "NE": StateProbe("NE", "31", "31001", "31001C"),
    "NH": StateProbe("NH", "33", "33003", "330220"),
    "NJ": StateProbe("NJ", "34", "34001", "340525"),
    "NM": StateProbe("NM", "35", "35001", "350018"),
    "NV": StateProbe("NV", "32", "32001", "32001C"),
    "NY": StateProbe("NY", "36", "36001", "360006"),
    "OH": StateProbe("OH", "39", "39001", "390908"),
    "OK": StateProbe("OK", "40", "40001", "400002"),
    "OR": StateProbe("OR", "41", "41003", "41003C"),
    "PA": StateProbe("PA", "42", "42001", "421247"),
    "PR": StateProbe("PR", "72", "72000", "720000"),
    "RI": StateProbe("RI", "44", "44001", "44001C"),
    "SC": StateProbe("SC", "45", "45001", "450001"),
    "SD": StateProbe("SD", "46", "46003", "460301"),
    "TN": StateProbe("TN", "47", "47001", "470001"),
    "TX": StateProbe("TX", "48", "48001", "48001C"),
    "UT": StateProbe("UT", "49", "49003", "490005"),
    "VA": StateProbe("VA", "51", "51001", "510050"),
    "VT": StateProbe("VT", "50", "50003", "500014"),
    "WA": StateProbe("WA", "53", "53001", "530002"),
    "WI": StateProbe("WI", "55", "55001", "550003"),
    "WV": StateProbe("WV", "54", "54001", "54001C"),
    "WY": StateProbe("WY", "56", "56001", "560003"),
}

STATE_FIPS = {p.state: p.state_fips for p in STATE_PROBES.values()}


def _state_shapes(path: pathlib.Path = STATES_GEOJSON) -> tuple[list[str], list[Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    codes: list[str] = []
    shapes: list[Any] = []
    for feature in data.get("features", []):
        region = str((feature.get("properties") or {}).get("region_id") or "")
        if not region.startswith("US-") or not feature.get("geometry"):
            continue
        codes.append(region[3:])
        shapes.append(shapely.geometry.shape(feature["geometry"]))
    return codes, shapes


# ---------------------------------------------------------------------------------- the route
def fema_url_allowed(url: str) -> bool:
    """True when ``url`` is one robots.txt on the FEMA hosts allows: no query string at all
    (``Disallow: /*?*``), and no disallowed path prefix. URLs on other hosts are not judged here."""
    parts = urlsplit(url)
    if parts.netloc not in {MSC_HOST, HAZARDS_HOST}:
        return True
    if parts.query or "?" in url:
        return False
    return not any(parts.path.startswith(prefix) for prefix in FEMA_DISALLOWED_PREFIXES)


def _require_allowed(url: str) -> None:
    if not fema_url_allowed(url):
        raise HttpBlocked(f"FEMA robots.txt disallows {url}")


def fema_session() -> PoliteSession:
    return PoliteSession(rate_limits=dict(RATE_LIMITS), max_attempts=FEMA_MAX_ATTEMPTS, timeout=180.0)


@dataclasses.dataclass(frozen=True)
class NfhlProduct:
    """One NFHL download as the MSC search lists it."""

    state: str
    name: str
    file: str
    url: str
    size_label: str | None = None
    latest_study_effective: str | None = None
    latest_lomr_effective: str | None = None

    @property
    def build_date(self) -> str | None:
        m = re.search(r"_(\d{4})(\d{2})(\d{2})\.zip$", self.file)
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


_STATE_FILE_RE = re.compile(r"^NFHL_(\d{2})_(\d{8})\.zip$")


def _us_date(value: Any) -> str | None:
    """MSC's ``MM/DD/YYYY`` -> ISO date, else None."""
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*", str(value or ""))
    if not m:
        return None
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"


def search_form(probe: StateProbe) -> dict[str, str]:
    """The fields the MSC "Search All Products" page posts for a state/county/community search."""
    return {
        "selstate": probe.state_fips,
        "selcounty": probe.county,
        "selcommunity": probe.cid,
        "txtjurisdictionname": "",
        "jurisdictionkey": "",
        "jurisdictionvalue": "",
        "searchedCid": probe.cid,
        "searchedDateStart": "",
        "searchedDateEnd": "",
        "txtproductid": "",
        "txtstartdate": "",
        "txtenddate": "",
        "method": "search",
    }


def parse_state_products(payload: Mapping[str, Any], state: str, state_fips: str) -> list[NfhlProduct]:
    """The ``EFFECTIVE.NFHL_STATE_DATA`` entries of an MSC search answer, newest first. A file name
    that is not ``NFHL_<state fips>_<YYYYMMDD>.zip`` is refused rather than fetched."""
    effective = payload.get("EFFECTIVE")
    if not isinstance(effective, Mapping):
        raise ValueError("MSC search answer has no EFFECTIVE object")
    products: list[NfhlProduct] = []
    for item in effective.get("NFHL_STATE_DATA") or []:
        file = str(item.get("product_FILE_PATH") or "").strip()
        m = _STATE_FILE_RE.match(file)
        if not m or m.group(1) != state_fips:
            raise ValueError(f"unexpected NFHL state file name {file!r} for {state}")
        products.append(
            NfhlProduct(
                state=state,
                name=str(item.get("product_NAME") or file.removesuffix(".zip")),
                file=file,
                url=STATE_FILE_URL.format(file=file),
                size_label=item.get("product_FILE_SIZE"),
                latest_study_effective=_us_date(item.get("product_EFFECTIVE_DATE_STRING")),
                latest_lomr_effective=_us_date(item.get("product_DESCRIPTION")),
            )
        )
    return sorted(products, key=lambda p: p.file, reverse=True)


def find_state_product(state: str, session: PoliteSession | None = None) -> NfhlProduct:
    probe = STATE_PROBES.get(state)
    if probe is None:
        raise KeyError(f"no MSC search community recorded for {state}; pass --product-file")
    session = session or fema_session()
    _require_allowed(MSC_SEARCH_URL)
    resp = session.post(
        MSC_SEARCH_URL,
        data=search_form(probe),
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": LANDING_URL,
        },
    )
    if resp.status_code != 200 or "json" not in str(resp.headers.get("Content-Type", "")):
        raise HttpFailed(
            f"MSC search for {state} answered HTTP {resp.status_code} {resp.headers.get('Content-Type')}"
        )
    products = parse_state_products(resp.json(), state, probe.state_fips)
    if not products:
        raise HttpFailed(f"MSC search for {state} (community {probe.cid}) lists no NFHL state product")
    return products[0]


def product_from_file(state: str, file: str) -> NfhlProduct:
    """A product named by hand (``--product-file NFHL_48_20261013.zip``) or a county product."""
    if _STATE_FILE_RE.match(file):
        return NfhlProduct(
            state=state, name=file.removesuffix(".zip"), file=file, url=STATE_FILE_URL.format(file=file)
        )
    if re.fullmatch(r"\d{5}C_\d{8}\.zip", file):
        return NfhlProduct(
            state=state, name=f"NFHL_{file[:6]}", file=file, url=COUNTY_FILE_URL.format(file=file)
        )
    raise ValueError(f"not an NFHL state or county file name: {file!r}")


# ------------------------------------------------------------------- zip members over HTTP range
class RangeReader(io.RawIOBase):
    """A read-only, seekable view of a remote file through HTTP range requests, so `zipfile` can
    read the central directory and chosen members without downloading the archive. ``If-Range``
    pins every request to the ETag seen first: a file replaced mid-read answers 200, not 206, and
    the read fails instead of mixing two files."""

    def __init__(self, url: str, session: PoliteSession, *, size: int, etag: str | None) -> None:
        super().__init__()
        self.url = url
        self.session = session
        self.size = size
        self.etag = etag
        self.pos = 0
        self.bytes_fetched = 0
        self._block_start = 0
        self._block = b""

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        else:
            self.pos = self.size + offset
        self.pos = max(0, self.pos)
        return self.pos

    def _fetch(self, start: int, length: int) -> bytes:
        end = min(self.size, start + length) - 1
        headers = {"Range": f"bytes={start}-{end}"}
        if self.etag:
            headers["If-Range"] = self.etag
        # Not streamed: requests reads the body inside PoliteSession.request, so a connection reset
        # partway through a 32 MB range is retried there (FEMA_MAX_ATTEMPTS) like a failed connect.
        resp = self.session.get(self.url, honour_robots=True, headers=headers)
        try:
            if resp.status_code != 206:
                raise HttpFailed(
                    f"{self.url} answered HTTP {resp.status_code} to a range request (file changed?)"
                )
            content_range = str(resp.headers.get("Content-Range", ""))
            if not content_range.startswith(f"bytes {start}-"):
                raise HttpFailed(f"{self.url}: Content-Range {content_range!r} for bytes {start}-{end}")
            data = resp.content
        finally:
            resp.close()
        if len(data) != end - start + 1:
            raise HttpFailed(f"{self.url}: {len(data)} bytes for range {start}-{end}")
        self.bytes_fetched += len(data)
        return data

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self.size - self.pos
        if size == 0 or self.pos >= self.size:
            return b""
        size = min(size, self.size - self.pos)
        offset = self.pos - self._block_start
        if not (0 <= offset and offset + size <= len(self._block)):
            want = min(max(size, RANGE_MIN), RANGE_MAX) if size <= RANGE_MAX else size
            self._block_start = self.pos
            self._block = self._fetch(self.pos, want)
            offset = 0
        data = self._block[offset : offset + size]
        self.pos += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


@dataclasses.dataclass
class RemoteZip:
    url: str
    size: int
    etag: str | None
    last_modified: str | None
    reader: RangeReader
    zip: zipfile.ZipFile


def open_remote_zip(url: str, session: PoliteSession) -> RemoteZip:
    _require_allowed(url)
    if not session.allowed_by_robots(url):
        raise HttpBlocked(f"robots.txt disallows {url}")
    head = session.request("HEAD", url, honour_robots=False, allow_redirects=False)
    if head.status_code != 200:
        raise HttpFailed(f"HEAD {url} answered HTTP {head.status_code}")
    if str(head.headers.get("Accept-Ranges", "")).lower() != "bytes":
        raise HttpFailed(f"{url} does not serve byte ranges")
    size = int(head.headers["Content-Length"])
    etag = head.headers.get("ETag")
    reader = RangeReader(url, session, size=size, etag=etag)
    return RemoteZip(url, size, etag, head.headers.get("Last-Modified"), reader, zipfile.ZipFile(reader))


def extract_members(zf: zipfile.ZipFile, names: Iterable[str], dest: pathlib.Path) -> int:
    """Stream the named members to ``dest`` (paths kept, written whole or not at all); returns the
    uncompressed bytes written. Member names are checked against escaping ``dest``."""
    written = 0
    root = dest.resolve()
    for name in names:
        target = (dest / name).resolve()
        if root not in target.parents:
            raise ValueError(f"zip member escapes the work directory: {name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        with zf.open(name) as src, partial.open("wb") as out:
            shutil.copyfileobj(src, out, COPY_CHUNK)
        partial.replace(target)
        written += target.stat().st_size
    return written


_GDB_TABLE_RE = re.compile(r"^(?P<dir>.+\.gdb)/a(?P<id>[0-9a-f]{8})\.", re.IGNORECASE)
#: File geodatabase system tables (GDB_SystemCatalog ... GDB_ReplicaLog) are ids 1-8.
GDB_SYSTEM_IDS = frozenset(range(1, 9))


def gdb_members(names: Sequence[str]) -> tuple[str, list[str]]:
    """(the .gdb directory, its system-table and loose members) of a state product."""
    dirs = {m.group("dir") for n in names if (m := _GDB_TABLE_RE.match(n))}
    if len(dirs) != 1:
        raise ValueError(f"expected one file geodatabase in the zip, found {sorted(dirs)}")
    gdb = dirs.pop()
    system = []
    for n in names:
        if not n.startswith(gdb + "/") or n.endswith("/"):
            continue
        m = _GDB_TABLE_RE.match(n)
        if m is None or int(m.group("id"), 16) in GDB_SYSTEM_IDS:
            system.append(n)
    return gdb, system


def gdb_table_members(names: Sequence[str], gdb: str, table_ids: Iterable[int]) -> list[str]:
    wanted = {f"{gdb}/a{i:08x}.".lower() for i in table_ids}
    return [n for n in names if any(n.lower().startswith(w) for w in wanted)]


def shapefile_members(names: Sequence[str], layers: Iterable[str]) -> list[str]:
    stems = {layer.upper() for layer in layers}
    exts = {".shp", ".shx", ".dbf", ".prj", ".cpg"}
    out = []
    for n in names:
        p = pathlib.PurePosixPath(n)
        if p.stem.upper() in stems and p.suffix.lower() in exts:
            out.append(n)
    return out


def _pyogrio() -> Any:
    try:
        import pyogrio  # type: ignore[import-untyped,import-not-found,unused-ignore]
    except ImportError as e:  # pragma: no cover - exercised only where the build installs it
        raise RuntimeError(
            "reading an NFHL file geodatabase needs pyogrio (GDAL); "
            "infra/scripts/build_flood_layer.sh installs it"
        ) from e
    return pyogrio


def _varuint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7


def read_gdb_catalog(table: bytes, tablx: bytes) -> dict[str, int]:
    """GDB_SystemCatalog (``a00000001.gdbtable`` + ``.gdbtablx``) -> {table name: number}; the
    number is the row's object id and names the files ``a<number as 8 hex digits>.*``.

    Read in Python, not GDAL: GDAL will not open a geodatabase directory holding only its system
    tables, and the point is to learn which data tables to fetch before fetching any. Layout per
    GDAL's reverse-engineered FileGDB spec: the catalog has three fields (ObjectID, Name string,
    FileFormat int32) and fewer than 1,024 rows, so one block of 4-6 byte row offsets."""
    magic, blocks, rows, offset_size = struct.unpack_from("<4i", tablx, 0)
    if magic != 3 or blocks != 1 or not 4 <= offset_size <= 6:
        raise ValueError(
            f"unsupported GDB_SystemCatalog index (magic {magic}, blocks {blocks}, offsets {offset_size})"
        )
    pos = struct.unpack_from("<q", table, 32)[0]
    _header_size, _version, _flags = struct.unpack_from("<iii", table, pos)
    pos += 12
    (n_fields,) = struct.unpack_from("<h", table, pos)
    pos += 2
    fields: list[tuple[str, int, bool]] = []
    for _ in range(n_fields):
        n = table[pos]
        name = table[pos + 1 : pos + 1 + 2 * n].decode("utf-16-le")
        pos += 1 + 2 * n
        pos += 1 + 2 * table[pos]  # alias
        ftype = table[pos]
        pos += 1
        if ftype == 6:  # ObjectID: width, flag
            pos += 2
            fields.append((name, ftype, False))
            continue
        if ftype == 4:  # string: max length, flag, [default]
            pos += 4
        elif ftype == 1:  # int32: width, flag, [default]
            pos += 1
        else:
            raise ValueError(f"unexpected field type {ftype} ({name}) in GDB_SystemCatalog")
        flag = table[pos]
        pos += 1
        if flag & 4:
            length, pos = _varuint(table, pos)
            pos += length
        fields.append((name, ftype, bool(flag & 1)))
    nullable = [f for f in fields if f[2]]
    out: dict[str, int] = {}
    for i in range(rows):
        start = 16 + i * offset_size
        offset = int.from_bytes(tablx[start : start + offset_size], "little")
        if offset == 0:
            continue  # deleted row
        pos = offset + 4
        nulls = table[pos : pos + (len(nullable) + 7) // 8]
        pos += len(nulls)
        values: dict[str, Any] = {}
        k = 0
        for name, ftype, is_nullable in fields:
            if ftype == 6:
                continue
            if is_nullable:
                is_null = bool(nulls[k // 8] & (1 << (k % 8)))
                k += 1
                if is_null:
                    values[name] = None
                    continue
            if ftype == 4:
                length, pos = _varuint(table, pos)
                values[name] = table[pos : pos + length].decode("utf-8")
                pos += length
            else:
                (values[name],) = struct.unpack_from("<i", table, pos)
                pos += 4
        if values.get("Name"):
            out[str(values["Name"])] = i + 1
    return out


def gdb_table_ids(gdb_path: pathlib.Path, layers: Iterable[str]) -> dict[str, int]:
    """Layer name -> table number, from the extracted GDB_SystemCatalog."""
    catalog = read_gdb_catalog(
        (gdb_path / "a00000001.gdbtable").read_bytes(), (gdb_path / "a00000001.gdbtablx").read_bytes()
    )
    out = {}
    for layer in layers:
        if layer not in catalog:
            raise ValueError(f"{gdb_path.name} has no {layer} table")
        out[layer] = catalog[layer]
    return out


@dataclasses.dataclass
class Download:
    """What a state build fetched: where the layers are, and the record of the request."""

    source: pathlib.Path
    kind: str  # "gdb" | "shapefile"
    url: str
    size: int
    etag: str | None
    last_modified: str | None
    members: list[str]
    bytes_fetched: int
    bytes_written: int
    retrieved_at: str


def download_layers(url: str, work_dir: pathlib.Path, session: PoliteSession | None = None) -> Download:
    """Fetch only the members the build reads: the zone and panel layers (and, for a geodatabase,
    the system tables that name them). Everything lands under ``work_dir``."""
    session = session or fema_session()
    remote = open_remote_zip(url, session)
    retrieved_at = _now_iso()
    names = [i.filename for i in remote.zip.infolist()]
    written = 0
    if any(_GDB_TABLE_RE.match(n) for n in names):
        gdb, system = gdb_members(names)
        written += extract_members(remote.zip, system, work_dir)
        ids = gdb_table_ids(work_dir / gdb, (ZONE_LAYER, PANEL_LAYER))
        tables = gdb_table_members(names, gdb, ids.values())
        written += extract_members(remote.zip, tables, work_dir)
        members, source, kind = system + tables, work_dir / gdb, "gdb"
    else:
        members = shapefile_members(names, (ZONE_LAYER, PANEL_LAYER))
        if not members:
            raise ValueError(f"{url} holds neither a file geodatabase nor {ZONE_LAYER} shapefiles")
        written += extract_members(remote.zip, members, work_dir)
        source = work_dir / pathlib.PurePosixPath(members[0]).parent
        kind = "shapefile"
    return Download(
        source=source,
        kind=kind,
        url=url,
        size=remote.size,
        etag=remote.etag,
        last_modified=remote.last_modified,
        members=members,
        bytes_fetched=remote.reader.bytes_fetched,
        bytes_written=written,
        retrieved_at=retrieved_at,
    )


# ------------------------------------------------------------------------------- reading layers
Feature = tuple[dict[str, Any], Any]


def _plain(value: Any) -> Any:
    """One attribute value as plain Python. A blank text field is None, as GDAL reads it, so the
    pyshp and pyogrio readers give identical rows (a DBF stores a null text field as spaces)."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, str) and not value.strip():
        return None
    return value


def read_layer(
    source: pathlib.Path,
    layer: str,
    *,
    columns: Sequence[str],
    where: str | None = None,
    mask: Any = None,
    batch_size: int = 5_000,
    engine: str | None = None,
) -> Iterator[list[Feature]]:
    """Batches of (attributes, shapely geometry) from a geodatabase or a shapefile directory.
    ``engine``: ``"pyogrio"`` (GDAL; geodatabases, and shapefiles when installed) or ``"pyshp"``
    (shapefiles, pure Python: the recorded fixtures in CI). ``where`` is an OGR SQL filter (pyogrio
    only; pyshp callers classify every row anyway); ``mask`` keeps features that intersect it."""
    is_gdb = source.suffix.lower() == ".gdb"
    if engine is None:
        try:
            _pyogrio()
            engine = "pyogrio"
        except RuntimeError:
            engine = "pyshp"
    if engine == "pyshp":
        if is_gdb:
            raise RuntimeError("a file geodatabase needs pyogrio")
        yield from _read_pyshp(source / f"{layer}.shp", columns=columns, mask=mask, batch_size=batch_size)
        return
    path = str(source) if is_gdb else str(source / f"{layer}.shp")
    from pyogrio.raw import open_arrow  # type: ignore[import-untyped,import-not-found,unused-ignore]

    with open_arrow(
        path,
        layer=layer if is_gdb else None,
        columns=list(columns),
        where=where,
        mask=mask,
        batch_size=batch_size,
        use_pyarrow=True,
    ) as (meta, reader):
        # A geodatabase names its geometry column (SHAPE); for a shapefile GDAL's Arrow stream calls it
        # wkb_geometry and the metadata leaves the name empty.
        names = reader.schema.names
        geometry_name = meta.get("geometry_name") or next(
            (n for n in ("wkb_geometry", "geometry", "wkb") if n in names), names[-1]
        )
        for batch in reader:
            data = batch.to_pydict()
            geoms = shapely.from_wkb(data.pop(geometry_name))
            names = [c for c in data if c in columns]
            rows = [{c: _plain(data[c][i]) for c in names} for i in range(len(geoms))]
            yield list(zip(rows, geoms, strict=True))


def _read_pyshp(
    path: pathlib.Path, *, columns: Sequence[str], mask: Any, batch_size: int
) -> Iterator[list[Feature]]:
    import shapefile  # type: ignore[import-untyped]  # pyshp ships no type stubs

    prepared = None
    if mask is not None:
        prepared = mask
        shapely.prepare(prepared)
    with shapefile.Reader(str(path), encoding="utf-8", encodingErrors="replace") as reader:
        fields = [f[0] for f in reader.fields[1:]]
        wanted = [c for c in columns if c in fields]
        batch: list[Feature] = []
        for shape_record in reader.iterShapeRecords():
            if shape_record.shape.shapeType == shapefile.NULL:
                continue
            geom = shapely.geometry.shape(shape_record.shape.__geo_interface__)
            if prepared is not None and not shapely.intersects(prepared, geom):
                continue
            record = shape_record.record.as_dict()
            batch.append(({c: _plain(record.get(c)) for c in wanted}, geom))
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch


# ---------------------------------------------------------------------------- classification
def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    s = str(value).strip()
    return s or None


def classify(zone: Any, subtype: Any, sfha_tf: Any) -> str | None:
    """One `S_FLD_HAZ_AR` row -> a category of `CATEGORIES`, or None for unshaded X (not written).
    The SFHA is the union of what ``SFHA_TF`` says and what the zone says: where the two disagree
    the row counts as floodplain (the safe reading for a "not in the floodplain" claim), and the
    disagreement is counted in the run summary."""
    z = (_text(zone) or "").upper()
    if (_text(sfha_tf) or "").upper() in {"T", "TRUE", "Y", "1"} or z in SFHA_ZONES:
        return "sfha"
    if z == "X":
        return None if (_text(subtype) or "").upper() in MINIMAL_X_SUBTYPES else "shaded_x"
    if z == "D":
        return "zone_d"
    if z == "OPEN WATER":
        return "open_water"
    if z == "AREA NOT INCLUDED":
        return "not_included"
    return "unrecognised"


def iso_date(value: Any) -> str | None:
    """A geodatabase timestamp, a pyshp date, ``YYYYMMDD`` or ``YYYY-MM-DD...`` -> ``YYYY-MM-DD``."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    s = str(value).strip()
    m = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", s)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def panel_covers(panel_type: Any) -> bool:
    """A panel is a digital map of its area unless FEMA marks it "Area Not Included"."""
    return "AREA NOT INCLUDED" not in (_text(panel_type) or "").upper()


def _json_raw(props: Mapping[str, Any], fields: Sequence[str]) -> str:
    out = {}
    for f in fields:
        v = props.get(f)
        if isinstance(v, (dt.date, dt.datetime)):
            v = v.isoformat()
        elif isinstance(v, float) and math.isnan(v):
            v = None
        out[f] = v
    return json.dumps(out, sort_keys=True, default=str)


# ------------------------------------------------------------------------------ geometry
def tolerance_degrees(tolerance_m: float) -> float:
    return tolerance_m / M_PER_DEG


def polygonal(geom: Any) -> Any:
    """The polygonal part of an overlay result (lines and points from touching edges dropped)."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in {"Polygon", "MultiPolygon"}:
        return geom
    parts = [
        p for p in shapely.get_parts(geom) if p.geom_type in {"Polygon", "MultiPolygon"} and not p.is_empty
    ]
    if not parts:
        return None
    return shapely.union_all(parts) if len(parts) > 1 else parts[0]


def repair(geom: Any) -> Any:
    """``make_valid`` by structure: overlapping parts are unioned and holes subtracted, so a repaired
    flood zone covers at least the area its parts claimed. The default ``linework`` method rebuilds
    areas by the even-odd rule and drops the area two parts share (two overlapping 2 x 2 boxes: union 7,
    repaired 6), which would turn floodplain into "outside"."""
    return shapely.make_valid(geom, method="structure", keep_collapsed=False)


def valid(geom: Any) -> Any:
    if geom is None or geom.is_empty:
        return None
    if not shapely.is_valid(geom):
        geom = repair(geom)
    return polygonal(geom)


def simplify_polygonal(geom: Any, tolerance_deg: float) -> Any:
    """Douglas-Peucker on every ring, run as a plain line so each ring keeps its start point and no
    original vertex ends farther than ``tolerance_deg`` from the simplified ring. A ring that would
    collapse (fewer than 4 points) is kept as it was; a self-intersection the simplification
    creates is repaired with `repair`, which keeps every edge and unions what overlaps.

    Not GEOS's topology-preserving simplifier: on the Texas run (GEOS 3.13.1, 2026-10-22) it moved
    29 of 708 sampled pieces' vertices by more than the tolerance, up to 1.57 times it (fixture
    ``tps_violations_nfhl_48.json``). Not GEOS's polygon Douglas-Peucker either: it repairs with
    ``buffer(0)``, which can drop a lobe of a self-intersecting ring."""
    if geom is None or tolerance_deg <= 0:
        return geom
    polys = []
    for poly in shapely.get_parts(geom):
        rings = []
        for ring in shapely.get_rings(poly):
            coords = shapely.get_coordinates(ring)
            simplified = shapely.get_coordinates(
                shapely.simplify(shapely.linestrings(coords), tolerance_deg, preserve_topology=False)
            )
            rings.append(simplified if len(simplified) >= 4 else coords)
        polys.append(shapely.geometry.Polygon(rings[0], rings[1:]))
    out = polys[0] if len(polys) == 1 else shapely.geometry.MultiPolygon(polys)
    return out if shapely.is_valid(out) else polygonal(repair(out))


def areas_m2(geoms: Any) -> Any:
    """Areas in square metres: degree-squared area times the local scale (cos of the centroid
    latitude). Within 0.5 % for polygons a few kilometres across; used for measurement only."""
    arr = np.asarray(geoms, dtype=object)
    if arr.size == 0:
        return np.zeros(0)
    lat = shapely.get_y(shapely.centroid(arr))
    return shapely.area(arr) * (M_PER_DEG**2) * np.cos(np.radians(lat))


def _buffer_scaled(geom: Any, distance_m: float, quad_segs: int) -> Any:
    lat0 = (geom.bounds[1] + geom.bounds[3]) / 2.0
    k = math.cos(math.radians(lat0))
    scale = np.array([k, 1.0])
    scaled = shapely.transform(geom, lambda xy: xy * scale)
    buffered = shapely.buffer(scaled, distance_m / M_PER_DEG, quad_segs=quad_segs)
    return shapely.transform(buffered, lambda xy: xy / scale)


def buffer_m(
    geom: Any, distance_m: float, quad_segs: int = 8, max_lat_span: float = BUFFER_MAX_LAT_SPAN
) -> Any:
    """Buffer by metres: longitude is scaled by cos(latitude), buffered in that locally isotropic
    plane, scaled back. A geometry taller than ``max_lat_span`` degrees is cut into latitude bands
    first and each band buffered at its own latitude (the pieces' buffers overlap where they meet, so
    the union is the buffer of the whole); one scale for a tall line leaves the buffer short
    east-west at the end farthest from the equator."""
    miny, maxy = geom.bounds[1], geom.bounds[3]
    if maxy - miny <= max_lat_span:
        return _buffer_scaled(geom, distance_m, quad_segs)
    n = math.ceil((maxy - miny) / max_lat_span)
    edges = np.linspace(miny, maxy, n + 1)
    # GEOS's rectangle clip drops a segment lying exactly on the rectangle's edge (a horizontal
    # segment at the line's lowest latitude, 52 m of an LBNL line, vanished). The bands overlap, so
    # every band edge lies strictly inside the next band, and the outer edges lie beyond the line.
    overlap = (maxy - miny) / n * 0.05
    minx, maxx = geom.bounds[0] - 1.0, geom.bounds[2] + 1.0
    parts = []
    for y0, y1 in itertools.pairwise(edges.tolist()):
        piece = shapely.clip_by_rect(geom, minx, y0 - overlap, maxx, y1 + overlap)
        if piece is not None and not piece.is_empty:
            parts.append(_buffer_scaled(piece, distance_m, quad_segs))
    return shapely.union_all(parts)


def subdivide(geom: Any, max_vertices: int = SUBDIVIDE_MAX_VERTICES) -> list[Any]:
    """Split a (multi)polygon into pieces of at most ``max_vertices`` by recursive halving of the
    bounding box (alternating axes on the longer side). The pieces tile the input exactly."""
    out: list[Any] = []
    stack = [g for g in shapely.get_parts(geom) if not g.is_empty]
    while stack:
        g = stack.pop()
        if shapely.get_num_coordinates(g) <= max_vertices:
            out.append(g)
            continue
        minx, miny, maxx, maxy = g.bounds
        if maxx - minx >= maxy - miny:
            mid = (minx + maxx) / 2.0
            boxes = ((minx, miny, mid, maxy), (mid, miny, maxx, maxy))
        else:
            mid = (miny + maxy) / 2.0
            boxes = ((minx, miny, maxx, mid), (minx, mid, maxx, maxy))
        for box in boxes:
            part = polygonal(shapely.clip_by_rect(g, *box))
            if part is not None:
                stack.extend(p for p in shapely.get_parts(part) if not p.is_empty)
    return out


# --------------------------------------------------------------------------- areas of interest
@dataclasses.dataclass
class LineRecord:
    geometry: Any
    voltage_kv: float | None
    source_id: str
    source_url: str
    retrieved_at: str
    licence: str


#: CRS identifiers a lines GeoParquet may declare: lon/lat on WGS84 (GeoParquet's default when absent).
LONLAT_CRS_IDS = frozenset({("OGC", "CRS84"), ("EPSG", "4326")})
LINE_TYPES = frozenset({"LineString", "MultiLineString"})


def _crs_id(crs: Any) -> tuple[str, str] | None:
    """(authority, code) of a GeoParquet ``crs`` value: PROJJSON with an ``id``, or a string."""
    if isinstance(crs, Mapping):
        ident = crs.get("id") or {}
        if isinstance(ident, Mapping) and ident.get("authority") and ident.get("code") is not None:
            return str(ident["authority"]).upper(), str(ident["code"]).upper()
        return None
    if isinstance(crs, str) and ":" in crs:
        authority, _, code = crs.partition(":")
        return authority.strip().upper(), code.strip().upper()
    return None


def _require_lonlat(path: pathlib.Path, geo: Mapping[str, Any], column: str) -> None:
    meta = (geo.get("columns") or {}).get(column) or {}
    if "crs" not in meta:
        return  # GeoParquet: an absent crs is OGC:CRS84
    ident = _crs_id(meta["crs"])
    if ident not in LONLAT_CRS_IDS:
        raise ValueError(
            f"{path}: geometry column {column!r} declares crs {ident or meta['crs']!r}; the areas of "
            "interest are built in lon/lat degrees, so reproject the lines to OGC:CRS84 or EPSG:4326 first"
        )


def _voltage_kv(value: Any, path: pathlib.Path, column: str) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{path}: {column} holds {value!r}; it must be a number in kV (parse OSM voltage tags such as "
            "'138000;69000' into one kV value per line first)"
        )
    kv = float(value)
    if kv > MAX_PLAUSIBLE_KV:
        raise ValueError(f"{path}: {column} = {kv:g} is not a kV value (no line runs above 1,100 kV); volts?")
    return kv


def lines_from_parquet(path: pathlib.Path, voltage_column: str = "voltage_kv") -> Iterator[LineRecord]:
    """Lines from the LBNL asset parquet (``geom_line_wkt`` + ``attributes.voltage_kv``) or from a
    GeoParquet of lines carrying a numeric ``voltage_column`` in kV and the provenance columns.
    Refused with a clear message, before any line is yielded: a GeoParquet crs other than lon/lat
    WGS84, a voltage column that is not numeric (or a kV value that is volts), geometries that are not
    lines, and coordinates outside lon/lat range (a projected file that declares no crs)."""
    table = pq.read_table(path)
    cols = set(table.column_names)
    data = table.to_pydict()
    n = table.num_rows
    if "geom_line_wkt" in cols:
        geoms = shapely.from_wkt([w or "GEOMETRYCOLLECTION EMPTY" for w in data["geom_line_wkt"]])
        attrs = [
            json.loads(a) if isinstance(a, str) else (a or {}) for a in data.get("attributes", [None] * n)
        ]
        column_name = f"attributes.{voltage_column}"
        volts = [_voltage_kv(a.get(voltage_column), path, column_name) for a in attrs]
    else:
        geo = json.loads((table.schema.metadata or {}).get(b"geo", b"{}"))
        column = geo.get("primary_column", "geometry")
        if column not in cols:
            raise ValueError(f"{path} has neither geom_line_wkt nor a GeoParquet geometry column")
        _require_lonlat(path, geo, column)
        if voltage_column not in cols:
            raise ValueError(f"{path} has no {voltage_column} column")
        vtype = table.schema.field(voltage_column).type
        if not (pa.types.is_integer(vtype) or pa.types.is_floating(vtype) or pa.types.is_decimal(vtype)):
            raise ValueError(
                f"{path}: {voltage_column} is {vtype}, not numeric; it must be one kV value per line "
                "(parse OSM voltage tags such as '138000;69000' or '115 kV' first)"
            )
        geoms = shapely.from_wkb(data[column])
        volts = [
            _voltage_kv(None if v is None else float(v), path, voltage_column) for v in data[voltage_column]
        ]
    present = [g for g in geoms if g is not None and not g.is_empty]
    kinds = Counter(str(g.geom_type) for g in present)
    if set(kinds) - LINE_TYPES:
        raise ValueError(f"{path}: the areas of interest are built from lines, found {dict(kinds)}")
    if present:
        minx, miny, maxx, maxy = shapely.total_bounds(np.asarray(present, dtype=object))
        if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
            raise ValueError(
                f"{path}: coordinates span ({minx:.0f}, {miny:.0f}) to ({maxx:.0f}, {maxy:.0f}), not lon/lat "
                "degrees; reproject the lines to OGC:CRS84 first"
            )
    for i in range(n):
        g = geoms[i]
        if g is None or g.is_empty:
            continue
        yield LineRecord(
            geometry=g,
            voltage_kv=volts[i],
            source_id=str((data.get("source_id") or [""] * n)[i] or ""),
            source_url=str((data.get("source_url") or [""] * n)[i] or ""),
            retrieved_at=str((data.get("retrieved_at") or [""] * n)[i] or ""),
            licence=str((data.get("licence") or [""] * n)[i] or ""),
        )


def build_aoi(
    lines: Iterable[LineRecord],
    *,
    min_kv: float = DEFAULT_MIN_KV,
    buffer_miles: float = DEFAULT_BUFFER_MILES,
    margin_m: float = AOI_MARGIN_M,
    states: Sequence[str] | None = None,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
    state_shapes: tuple[list[str], list[Any]] | None = None,
    state_slack_m: float = STATE_SLACK_M,
) -> tuple[list[dict[str, Any]], list[Any], dict[str, Any]]:
    """One dissolved area of interest per state: (rows, geometries, summary). A row is a record
    derived from the lines: its provenance (``source_id``, ``source_url``, ``retrieved_at``,
    ``licence``) is that of the lines buffered into *that* state, not of every line read."""
    codes, shapes = state_shapes or _state_shapes()
    tree = STRtree(shapes)
    wanted = {s.upper() for s in states} if states else None
    distance = buffer_miles * METRES_PER_MILE + margin_m
    per_state: dict[str, list[Any]] = {}
    lines_per_state: Counter[str] = Counter()
    sources: dict[str, set[tuple[str, str, str, str]]] = {}
    kept = below = unknown = 0
    for line in lines:
        if line.voltage_kv is None:
            unknown += 1
            continue
        if line.voltage_kv < min_kv:
            below += 1
            continue
        buf = buffer_m(line.geometry, distance)
        # The slack in degrees of longitude at the buffer's highest latitude: at least state_slack_m
        # in every direction.
        lat = max(abs(buf.bounds[1]), abs(buf.bounds[3]))
        slack = state_slack_m / (M_PER_DEG * math.cos(math.radians(min(lat, 89.0))))
        hit = [codes[i] for i in tree.query(buf, predicate="dwithin", distance=slack)]
        hit = [s for s in hit if wanted is None or s in wanted]
        if not hit:
            continue
        kept += 1
        key = (line.source_id, line.source_url, line.retrieved_at, line.licence)
        for s in hit:
            per_state.setdefault(s, []).append(buf)
            lines_per_state[s] += 1
            sources.setdefault(s, set()).add(key)
    rows: list[dict[str, Any]] = []
    geoms: list[Any] = []
    for state in sorted(per_state):
        union = shapely.union_all(per_state[state])
        union = valid(simplify_polygonal(union, tolerance_degrees(tolerance_m)))
        mine = sources[state]
        rows.append(
            {
                "state": state,
                "buffer_miles": buffer_miles,
                "margin_m": margin_m,
                "min_kv": min_kv,
                "line_count": lines_per_state[state],
                "area_m2": float(areas_m2([union])[0]),
                "source_id": ";".join(sorted({k[0] for k in mine})),
                "source_url": ";".join(sorted({k[1] for k in mine})),
                "retrieved_at": max((k[2] for k in mine), default=""),
                "licence": " | ".join(sorted({k[3] for k in mine})),
            }
        )
        geoms.append(union)
    summary = {
        "lines_kept": kept,
        "lines_below_min_kv": below,
        "lines_without_voltage": unknown,
        "params": {
            "min_kv": min_kv,
            "buffer_miles": buffer_miles,
            "margin_m": margin_m,
            "tolerance_m": tolerance_m,
            "state_slack_m": state_slack_m,
        },
        "states": {
            r["state"]: {
                "lines": r["line_count"],
                "area_km2": round(r["area_m2"] / 1e6, 1),
                "source_id": r["source_id"],
            }
            for r in rows
        },
    }
    return rows, geoms, summary


# ----------------------------------------------------------------------------------- clipping
@dataclasses.dataclass
class Provenance:
    source_id: str
    source_url: str
    retrieved_at: str
    licence: str
    nfhl_product: str

    def columns(self) -> dict[str, str]:
        return {
            "nfhl_product": self.nfhl_product,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "licence": self.licence,
        }


@dataclasses.dataclass
class PanelIndex:
    """FIRM panels and, for each that touches the areas of interest, the AOI inside it."""

    props: list[dict[str, Any]]
    geoms: list[Any]
    aoi_parts: dict[int, Any]
    keys: list[int]
    tree: Any

    @classmethod
    def build(cls, panels: Sequence[Feature], aoi: Any) -> PanelIndex:
        props = [p for p, _ in panels]
        geoms = [valid(g) for _, g in panels]
        pieces = subdivide(aoi) if aoi is not None else []
        piece_tree = STRtree(pieces) if pieces else None
        aoi_parts: dict[int, Any] = {}
        for i, g in enumerate(geoms):
            if g is None or piece_tree is None:
                continue
            idx = piece_tree.query(g, predicate="intersects")
            if len(idx) == 0:
                continue
            local = shapely.union_all([pieces[j] for j in idx])
            part = polygonal(shapely.intersection(g, local))
            if part is not None:
                aoi_parts[i] = part
        keys = sorted(aoi_parts)
        tree = STRtree([aoi_parts[k] for k in keys]) if keys else None
        return cls(props=props, geoms=geoms, aoi_parts=aoi_parts, keys=keys, tree=tree)

    def query(self, geoms: Any) -> tuple[Any, Any]:
        """(feature index, panel index) pairs whose AOI-inside-panel intersects the feature."""
        if self.tree is None or len(geoms) == 0:
            return np.zeros(0, dtype=int), np.zeros(0, dtype=int)
        pairs = self.tree.query(np.asarray(geoms, dtype=object), predicate="intersects")
        return pairs[0], np.asarray(self.keys, dtype=int)[pairs[1]]


@dataclasses.dataclass
class ClipStats:
    features_read: int = 0
    features_kept: int = 0
    features_outside_aoi: int = 0
    unshaded_x_read: int = 0
    invalid_repaired: int = 0
    pieces: int = 0
    slivers_dropped: int = 0
    simplify_kept_original: int = 0
    vertices_before: int = 0
    vertices_after: int = 0
    by_category: Counter[str] = dataclasses.field(default_factory=Counter)
    zones: Counter[str] = dataclasses.field(default_factory=Counter)
    subtypes: Counter[str] = dataclasses.field(default_factory=Counter)
    sfha_flag_disagreements: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        for k in ("by_category", "zones", "subtypes"):
            d[k] = dict(sorted(getattr(self, k).items()))
        return d


def clip_zones(
    batches: Iterable[Sequence[Feature]],
    panels: PanelIndex,
    *,
    state: str,
    prov: Provenance,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
) -> tuple[list[dict[str, Any]], list[Any], ClipStats]:
    """Classify, clip to the AOI inside each FIRM panel, simplify. One row per (zone polygon,
    panel) piece; the panel names the FIRM the piece comes from. Holds every piece: `build_state`
    consumes `iter_clip_zones` batch by batch instead."""
    stats = ClipStats()
    rows: list[dict[str, Any]] = []
    out: list[Any] = []
    for batch_rows, batch_geoms in iter_clip_zones(
        batches, panels, state=state, prov=prov, tolerance_m=tolerance_m, stats=stats
    ):
        rows.extend(batch_rows)
        out.extend(batch_geoms)
    return rows, out, stats


def iter_clip_zones(
    batches: Iterable[Sequence[Feature]],
    panels: PanelIndex,
    *,
    state: str,
    prov: Provenance,
    stats: ClipStats,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
) -> Iterator[tuple[list[dict[str, Any]], list[Any]]]:
    """`clip_zones` one input batch at a time: yields that batch's (rows, pieces), ``area_m2`` set,
    and updates ``stats`` as it goes. Piece ids stay unique across batches."""
    seen: Counter[tuple[str, str]] = Counter()
    tol = tolerance_degrees(tolerance_m)
    for batch in batches:
        rows: list[dict[str, Any]] = []
        out: list[Any] = []
        keep_props: list[tuple[dict[str, Any], str]] = []
        keep_geoms: list[Any] = []
        for props, geom in batch:
            stats.features_read += 1
            zone = _text(props.get("FLD_ZONE"))
            category = classify(zone, props.get("ZONE_SUBTY"), props.get("SFHA_TF"))
            if category is None:
                stats.unshaded_x_read += 1
                continue
            sfha_flag = (_text(props.get("SFHA_TF")) or "").upper() == "T"
            if (category == "sfha") != sfha_flag:
                stats.sfha_flag_disagreements += 1
            if geom is None or geom.is_empty:
                continue
            if not shapely.is_valid(geom):
                stats.invalid_repaired += 1
            g = valid(geom)
            if g is None:
                continue
            keep_props.append((props, category))
            keep_geoms.append(g)
        if not keep_geoms:
            continue
        feat_idx, panel_idx = panels.query(keep_geoms)
        touched = set(feat_idx.tolist())
        stats.features_outside_aoi += len(keep_geoms) - len(touched)
        stats.features_kept += len(touched)
        if len(feat_idx) == 0:
            continue
        left = np.asarray(keep_geoms, dtype=object)[feat_idx]
        right = np.asarray([panels.aoi_parts[int(p)] for p in panel_idx], dtype=object)
        pieces = shapely.intersection(left, right)
        for fi, pi, piece in zip(feat_idx.tolist(), panel_idx.tolist(), pieces, strict=True):
            piece = polygonal(piece)
            if piece is None:
                continue
            if float(areas_m2([piece])[0]) < MIN_PIECE_M2:
                stats.slivers_dropped += 1
                continue
            simplified = simplify_polygonal(piece, tol)
            if simplified is None:
                stats.simplify_kept_original += 1
                simplified = piece
            stats.vertices_before += int(shapely.get_num_coordinates(piece))
            stats.vertices_after += int(shapely.get_num_coordinates(simplified))
            props, category = keep_props[fi]
            panel = panels.props[pi]
            zone = _text(props.get("FLD_ZONE"))
            subtype = _text(props.get("ZONE_SUBTY"))
            stats.pieces += 1
            stats.by_category[category] += 1
            # FEMA reuses an FLD_AR_ID for several polygons of one panel (26 times in Texas), so the
            # piece id adds the occurrence; stable for one product, read in FID order.
            key = (_text(props.get("FLD_AR_ID")) or "", _text(panel.get("FIRM_PAN")) or "")
            seen[key] += 1
            stats.zones[zone or ""] += 1
            stats.subtypes[subtype or ""] += 1
            rows.append(
                {
                    "piece_id": f"{state}:{key[0]}:{key[1]}:{seen[key]}",
                    "category": category,
                    "sfha": category == "sfha",
                    "zone": zone,
                    "zone_subtype": subtype,
                    "sfha_tf": _text(props.get("SFHA_TF")),
                    "fld_ar_id": _text(props.get("FLD_AR_ID")),
                    "dfirm_id": _text(props.get("DFIRM_ID")),
                    "version_id": _text(props.get("VERSION_ID")),
                    "firm_panel": _text(panel.get("FIRM_PAN")),
                    "panel_effective_date": iso_date(panel.get("EFF_DATE")),
                    "state": state,
                    "area_m2": None,
                    **prov.columns(),
                    "raw": _json_raw(props, ZONE_FIELDS),
                }
            )
            out.append(simplified)
        if out:
            for row, area in zip(rows, areas_m2(out).tolist(), strict=True):
                row["area_m2"] = round(float(area), 1)
            yield rows, out


def coverage_rows(
    panels: PanelIndex, *, state: str, prov: Provenance, tolerance_m: float = DEFAULT_TOLERANCE_M
) -> tuple[list[dict[str, Any]], list[Any]]:
    rows: list[dict[str, Any]] = []
    geoms: list[Any] = []
    tol = tolerance_degrees(tolerance_m)
    for props, geom in zip(panels.props, panels.geoms, strict=True):
        if geom is None:
            continue
        simplified = simplify_polygonal(geom, tol) or geom
        rows.append(
            {
                "firm_panel": _text(props.get("FIRM_PAN")),
                "panel_type": _text(props.get("PANEL_TYP")),
                "covers": panel_covers(props.get("PANEL_TYP")),
                "panel_effective_date": iso_date(props.get("EFF_DATE")),
                "dfirm_id": _text(props.get("DFIRM_ID")),
                "version_id": _text(props.get("VERSION_ID")),
                "state": state,
                "area_m2": None,
                **prov.columns(),
                "raw": _json_raw(props, PANEL_FIELDS),
            }
        )
        geoms.append(simplified)
    if geoms:
        for row, area in zip(rows, areas_m2(geoms).tolist(), strict=True):
            row["area_m2"] = round(float(area), 1)
    return rows, geoms


# --------------------------------------------------------------------------------- GeoParquet
def _zorder(xs: Any, ys: Any) -> Any:
    """Morton key of lon/lat quantised to 2^16 x 2^16: nearby rows land in the same row group."""
    qx = np.clip(((np.asarray(xs) + 180.0) / 360.0 * 65535).astype(np.uint64), 0, 65535)
    qy = np.clip(((np.asarray(ys) + 90.0) / 180.0 * 65535).astype(np.uint64), 0, 65535)

    def spread(v: Any) -> Any:
        v = (v | (v << np.uint64(8))) & np.uint64(0x00FF00FF)
        v = (v | (v << np.uint64(4))) & np.uint64(0x0F0F0F0F)
        v = (v | (v << np.uint64(2))) & np.uint64(0x33333333)
        return (v | (v << np.uint64(1))) & np.uint64(0x55555555)

    return spread(qx) | (spread(qy) << np.uint64(1))


_BBOX_KEYS = ("xmin", "ymin", "xmax", "ymax")
_BBOX_TYPE = pa.struct([(k, pa.float64()) for k in _BBOX_KEYS])
_ZORDER = "__zorder"
#: String columns that differ on every row; every other string column repeats (the licence text alone
#: is 1.5 kB on every row) and is held dictionary-encoded until it is written.
ROW_UNIQUE_COLUMNS = frozenset({"piece_id", "fld_ar_id", "raw"})


class GeoParquetBuilder:
    """Collects rows and geometries batch by batch as Arrow tables (attributes, WKB, bbox and a Z-order
    key), then writes one GeoParquet file in Z-order, a row group at a time. A state's pieces are held as
    Arrow buffers (WKB, repeated strings dictionary-encoded) rather than Python dicts and shapely objects:
    on a synthetic Texas-shaped run each further 135,000 pieces cost 425 MB instead of 756 MB
    (`docs/25` §5.8 item 6)."""

    def __init__(self, schema: pa.Schema) -> None:
        self.schema = schema
        self._tables: list[pa.Table] = []
        self._types: set[str] = set()
        self._total: list[float] | None = None
        self.rows = 0

    def add(self, rows: Sequence[Mapping[str, Any]], geoms: Sequence[Any]) -> None:
        if len(rows) != len(geoms):
            raise ValueError(f"{len(rows)} rows for {len(geoms)} geometries")
        if not len(geoms):
            return
        arr = np.asarray(geoms, dtype=object)
        bounds = shapely.bounds(arr)
        centre = shapely.centroid(arr)
        columns: dict[str, Any] = {}
        for field in self.schema:
            values = pa.array([r.get(field.name) for r in rows], type=field.type)
            if pa.types.is_string(field.type) and field.name not in ROW_UNIQUE_COLUMNS:
                values = values.dictionary_encode()
            columns[field.name] = values
        columns["bbox"] = pa.StructArray.from_arrays(
            [pa.array(bounds[:, i], type=pa.float64()) for i in range(4)], fields=list(_BBOX_TYPE)
        )
        columns["geometry"] = pa.array(shapely.to_wkb(arr, output_dimension=2).tolist(), type=pa.binary())
        columns[_ZORDER] = pa.array(_zorder(shapely.get_x(centre), shapely.get_y(centre)), type=pa.uint64())
        self._tables.append(pa.table(columns))
        self._types.update(str(t) for t in {g.geom_type for g in arr})
        box = [
            float(bounds[:, 0].min()),
            float(bounds[:, 1].min()),
            float(bounds[:, 2].max()),
            float(bounds[:, 3].max()),
        ]
        t = self._total
        self._total = (
            box if t is None else [min(t[0], box[0]), min(t[1], box[1]), max(t[2], box[2]), max(t[3], box[3])]
        )
        self.rows += len(geoms)

    def write(self, path: pathlib.Path) -> int:
        """GeoParquet 1.1: ``geometry`` (WKB) plus a ``bbox`` covering struct; CRS omitted = OGC:CRS84.
        The attribute columns are ``schema``'s, so an empty file has the same columns as a full one.
        Rows are in Z-order of their centroids (stable: ties keep insertion order). Returns the size."""
        empty: dict[str, Any] = {f.name: pa.array([], type=f.type) for f in self.schema}
        empty["bbox"] = pa.array([], type=_BBOX_TYPE)
        empty["geometry"] = pa.array([], type=pa.binary())
        table = pa.concat_tables(self._tables) if self._tables else None
        self._tables = []
        order = (
            np.argsort(table.column(_ZORDER).to_numpy(), kind="stable")
            if table is not None
            else np.zeros(0, dtype=np.int64)
        )
        geo = {
            "version": "1.1.0",
            "primary_column": "geometry",
            "columns": {
                "geometry": {
                    "encoding": "WKB",
                    "geometry_types": sorted(self._types),
                    "bbox": self._total or [],
                    "covering": {"bbox": {k: ["bbox", k] for k in _BBOX_KEYS}},
                }
            },
        }
        schema = pa.table(empty).schema.with_metadata({b"geo": json.dumps(geo).encode("utf-8")})
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".part")
        # One row group at a time in Z-order: the sorted copy is a row group, not the whole table.
        with pq.ParquetWriter(partial, schema, compression="zstd") as writer:
            if table is None:
                writer.write_table(pa.table(empty, schema=schema))
            else:
                for start in range(0, len(order), ROW_GROUP_SIZE):
                    chunk = table.take(pa.array(order[start : start + ROW_GROUP_SIZE]))
                    # Back to the declared plain types (the dictionaries were for memory only).
                    chunk = chunk.drop_columns([_ZORDER]).cast(schema)
                    writer.write_table(chunk, row_group_size=ROW_GROUP_SIZE)
        partial.replace(path)
        return path.stat().st_size


def write_geoparquet(
    path: pathlib.Path, rows: Sequence[Mapping[str, Any]], geoms: Sequence[Any], schema: pa.Schema
) -> int:
    """One-shot `GeoParquetBuilder`: ``schema`` lists the attribute columns (without geometry and
    bbox). Returns the file size."""
    builder = GeoParquetBuilder(schema)
    builder.add(rows, geoms)
    return builder.write(path)


def read_geoparquet(path: pathlib.Path) -> tuple[pa.Table, Any]:
    table = pq.read_table(path)
    geo = json.loads((table.schema.metadata or {}).get(b"geo", b"{}"))
    column = geo.get("primary_column", "geometry")
    return table, shapely.from_wkb(table.column(column).to_pylist())


_PROV_FIELDS = [
    ("nfhl_product", pa.string()),
    ("source_id", pa.string()),
    ("source_url", pa.string()),
    ("retrieved_at", pa.string()),
    ("licence", pa.string()),
    ("raw", pa.string()),
]
ZONE_SCHEMA = pa.schema(
    [
        ("piece_id", pa.string()),
        ("category", pa.string()),
        ("sfha", pa.bool_()),
        ("zone", pa.string()),
        ("zone_subtype", pa.string()),
        ("sfha_tf", pa.string()),
        ("fld_ar_id", pa.string()),
        ("dfirm_id", pa.string()),
        ("version_id", pa.string()),
        ("firm_panel", pa.string()),
        ("panel_effective_date", pa.string()),
        ("state", pa.string()),
        ("area_m2", pa.float64()),
        *_PROV_FIELDS,
    ]
)
COVERAGE_SCHEMA = pa.schema(
    [
        ("firm_panel", pa.string()),
        ("panel_type", pa.string()),
        ("covers", pa.bool_()),
        ("panel_effective_date", pa.string()),
        ("dfirm_id", pa.string()),
        ("version_id", pa.string()),
        ("state", pa.string()),
        ("area_m2", pa.float64()),
        *_PROV_FIELDS,
    ]
)
AOI_SCHEMA = pa.schema(
    [
        ("state", pa.string()),
        ("buffer_miles", pa.float64()),
        ("margin_m", pa.float64()),
        ("min_kv", pa.float64()),
        ("line_count", pa.int64()),
        ("area_m2", pa.float64()),
        # Provenance of the lines buffered into this state (a derived record's sources).
        ("source_id", pa.string()),
        ("source_url", pa.string()),
        ("retrieved_at", pa.string()),
        ("licence", pa.string()),
    ]
)


def aoi_for_state(aoi_path: pathlib.Path, state: str) -> Any:
    table, geoms = read_geoparquet(aoi_path)
    states = table.column("state").to_pylist()
    hits = [g for s, g in zip(states, geoms, strict=True) if s == state]
    return shapely.union_all(hits) if hits else None


# ----------------------------------------------------------------------------- data quality
def dup_keys(rows: Sequence[Mapping[str, Any]], key: Sequence[str]) -> int:
    seen: Counter[tuple[Any, ...]] = Counter(tuple(r.get(k) for k in key) for r in rows)
    return sum(n - 1 for n in seen.values() if n > 1)


def quality(
    zone_rows: Sequence[Mapping[str, Any]], coverage: Sequence[Mapping[str, Any]], stats: ClipStats
) -> dict[str, Any]:
    """Vocabulary drift, null spikes and duplicate keys for one state's run."""
    unknown_zones = sorted(z for z in stats.zones if z and z not in KNOWN_ZONES)
    panel_types = Counter(str(r.get("panel_type") or "") for r in coverage)
    no_date = sum(1 for r in coverage if not r.get("panel_effective_date"))
    zone_no_panel = sum(1 for r in zone_rows if not r.get("firm_panel"))
    return {
        "unknown_zones": unknown_zones,
        "unrecognised_rows": stats.by_category.get("unrecognised", 0),
        "sfha_flag_disagreements": stats.sfha_flag_disagreements,
        "panel_types": dict(sorted(panel_types.items())),
        "panels_without_effective_date": no_date,
        "zone_rows_without_panel": zone_no_panel,
        "duplicate_piece_ids": dup_keys(zone_rows, ("piece_id",)),
        "source_ids_reused_in_a_panel": dup_keys(zone_rows, ("fld_ar_id", "firm_panel")),
        "duplicate_panels": dup_keys(coverage, ("firm_panel",)),
    }


# ------------------------------------------------------------------------------ state build
def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _registry_licence() -> str:
    from pipeline.connectors.registry import Registry

    return Registry().get(SOURCE_ID).license


def build_state(
    *,
    state: str,
    aoi: Any,
    out_dir: pathlib.Path,
    product: NfhlProduct | None = None,
    source: pathlib.Path | None = None,
    work_dir: pathlib.Path | None = None,
    keep_download: bool = False,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
    session: PoliteSession | None = None,
    licence: str | None = None,
    engine: str | None = None,
) -> dict[str, Any]:
    """Fetch one state's layers (or read ``source``, a local geodatabase or shapefile directory),
    clip, write ``sfha/``, ``zones_other/`` and ``coverage/`` under ``out_dir``, delete the
    download, and return the summary that `manifest` collects."""
    t0 = time.monotonic()
    licence = licence if licence is not None else _registry_licence()
    work: pathlib.Path | None = None
    download: Download | None = None
    try:
        if source is None:
            if product is None:
                product = find_state_product(state, session)
            if work_dir is not None:
                work_dir.mkdir(parents=True, exist_ok=True)
            work = pathlib.Path(tempfile.mkdtemp(prefix=f"nfhl-{state}-", dir=work_dir))
            download = download_layers(product.url, work, session)
            source = download.source
            prov = Provenance(SOURCE_ID, product.url, download.retrieved_at, licence, product.name)
        else:
            name = product.name if product else source.stem
            url = product.url if product else LANDING_URL
            prov = Provenance(SOURCE_ID, url, _now_iso(), licence, name)
        t_fetch = time.monotonic()
        panel_features = [
            f for batch in read_layer(source, PANEL_LAYER, columns=PANEL_FIELDS, engine=engine) for f in batch
        ]
        panels = PanelIndex.build(panel_features, aoi)
        mask = None
        if aoi is not None and panels.aoi_parts:
            mask = shapely.union_all(list(panels.aoi_parts.values()))
        batches = (
            read_layer(source, ZONE_LAYER, columns=ZONE_FIELDS, where=ZONE_WHERE, mask=mask, engine=engine)
            if mask is not None
            else iter(())
        )
        stats = ClipStats()
        builders = {"sfha": GeoParquetBuilder(ZONE_SCHEMA), "zones_other": GeoParquetBuilder(ZONE_SCHEMA)}
        area_by_category: dict[str, float] = dict.fromkeys(CATEGORIES, 0.0)
        # Only the three keys `quality` checks are kept per piece; the pieces go to Arrow per batch.
        keys: list[dict[str, Any]] = []
        for rows, geoms in iter_clip_zones(
            batches, panels, state=state, prov=prov, tolerance_m=tolerance_m, stats=stats
        ):
            for layer, builder in builders.items():
                idx = [i for i, r in enumerate(rows) if (r["category"] == "sfha") == (layer == "sfha")]
                builder.add([rows[i] for i in idx], [geoms[i] for i in idx])
            for r in rows:
                area_by_category[r["category"]] += r["area_m2"] or 0.0
                keys.append({k: r[k] for k in ("piece_id", "fld_ar_id", "firm_panel")})
        cov_rows, cov_geoms = coverage_rows(panels, state=state, prov=prov, tolerance_m=tolerance_m)
        t_clip = time.monotonic()
        files: dict[str, dict[str, Any]] = {}
        for layer, builder in builders.items():
            path = out_dir / layer / f"{state}.parquet"
            n_rows = builder.rows
            size = builder.write(path)
            files[layer] = {
                "path": str(path.relative_to(out_dir)),
                "rows": n_rows,
                "bytes": size,
                "sha256": _sha256(path),
            }
        path = out_dir / "coverage" / f"{state}.parquet"
        size = write_geoparquet(path, cov_rows, cov_geoms, COVERAGE_SCHEMA)
        files["coverage"] = {
            "path": str(path.relative_to(out_dir)),
            "rows": len(cov_rows),
            "bytes": size,
            "sha256": _sha256(path),
        }
        sfha_area = area_by_category["sfha"]
        summary: dict[str, Any] = {
            "state": state,
            "nfhl_product": prov.nfhl_product,
            "source_url": prov.source_url,
            "retrieved_at": prov.retrieved_at,
            "product": dataclasses.asdict(product) if product else None,
            "download": (
                {
                    k: getattr(download, k)
                    for k in ("kind", "size", "etag", "last_modified", "bytes_fetched", "bytes_written")
                }
                | {"members": len(download.members)}
                if download
                else None
            ),
            "aoi_area_km2": round(float(areas_m2([aoi])[0]) / 1e6, 1) if aoi is not None else 0.0,
            "panels": len(panels.props),
            "panels_touching_aoi": len(panels.aoi_parts),
            "panels_covering": sum(1 for r in cov_rows if r["covers"]),
            "sfha_area_km2": round(sfha_area / 1e6, 2),
            "other_area_km2": {c: round(area_by_category[c] / 1e6, 2) for c in CATEGORIES if c != "sfha"},
            "tolerance_m": tolerance_m,
            "clip": stats.as_dict(),
            "quality": quality(keys, cov_rows, stats),
            "files": files,
            "seconds": {
                "fetch": round(t_fetch - t0, 1),
                "clip": round(t_clip - t_fetch, 1),
                "total": round(time.monotonic() - t0, 1),
            },
        }
        summary_path = out_dir / "states" / f"{state}.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
        )
        return summary
    finally:
        # The raw layers are deleted whether the build succeeded or not (disk is the constraint;
        # the run record keeps the URL, ETag and sizes). A --source directory is never touched.
        if work is not None and not keep_download:
            shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------------------- the manifest
#: What each output layer holds, as the manifest states it for the finder.
LAYER_NOTES = {
    "aoi": "areas of interest, one row per state (the state's row of states[ST].aoi.key): the only area "
    "where the zone layers were computed. Outside it no zone row exists, so a site there is 'flood data "
    "unavailable (not computed)', never 'outside', whatever the coverage says",
    "sfha": "Special Flood Hazard Area (zones A, AE, AH, AO, AR, A99, V, VE), clipped to the state's "
    "areas of interest",
    "zones_other": "shaded X (0.2% annual chance, levee-reduced, future conditions), Zone D, open water, "
    "area not included, unrecognised zones; clipped to the state's areas of interest",
    "coverage": "effective FIRM panels, statewide and NOT clipped: where FEMA has a digital flood map "
    "(covers = false: area not included). A covering panel outside the areas of interest does not make "
    "a site 'outside'",
}

#: The finder's rule, in order; `FloodLayer.assess` implements it (docs/25 section 5.3).
FINDER_RULE = (
    "Load every state in `states`, each from its own keys (states[ST].aoi.key, states[ST].files[layer].key): "
    "a state carried forward from an earlier build lives under that build's prefix.",
    "1 SFHA: the site comes within tolerance_m of an `sfha` piece of any state -> in the floodplain.",
    "2 Not computed: some part of the site lies outside every state's area of interest -> flood data "
    "unavailable (not computed).",
    "3 No digital map: some part of the site lies outside the union, over states, of (that state's area of "
    "interest intersected with its covering panels, grown by tolerance_m) -> flood data unavailable "
    "(no digital map).",
    "4 Undetermined: the site comes within tolerance_m of a zone_d, not_included or unrecognised piece -> "
    "flood data unavailable (that category).",
    "5 Otherwise outside the SFHA on the effective FIRM. Report shaded_x and open_water within tolerance_m "
    "beside the answer, never as a reason to exclude. Never an official flood determination.",
)
REFERENCE_IMPLEMENTATION = "pipeline/context/fema_nfhl.py FloodLayer.assess"


class QualityGateFailed(RuntimeError):
    """A state's SFHA rows fell by more than the allowed share since the previous build, or a state
    of the previous build is missing from a full one."""


def _state_entry(
    summary: Mapping[str, Any], *, prefix: str, version: str, aoi: Mapping[str, Any] | None
) -> dict[str, Any]:
    """A state summary as the manifest lists it: where each of its files lives (full object keys under
    ``prefix``) and the area of interest its zone files were clipped to."""
    entry = dict(summary)
    entry["version"] = version
    entry["prefix"] = prefix
    entry["files"] = {k: {**f, "key": f"{prefix}/{f['path']}"} for k, f in summary["files"].items()}
    state_aoi = ((aoi or {}).get("states") or {}).get(summary["state"]) or {}
    entry["aoi"] = {
        "key": f"{prefix}/aoi.parquet",
        "state_row": summary["state"],
        **((aoi or {}).get("params") or {}),
        **state_aoi,
    }
    return entry


def _carried_entry(entry: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    """A previous manifest's state, unchanged, with its own prefix (derived from that manifest's
    ``r2_prefix`` for a manifest written before states carried one)."""
    if (
        entry.get("prefix")
        and all("key" in f for f in entry["files"].values())
        and "key" in (entry.get("aoi") or {})
    ):
        return dict(entry)
    prefix = str(entry.get("prefix") or previous["r2_prefix"])
    version = str(entry.get("version") or previous["version"])
    return _state_entry(entry, prefix=prefix, version=version, aoi=previous.get("aoi"))


def build_manifest(
    out_dir: pathlib.Path,
    *,
    version: str,
    previous: Mapping[str, Any] | None = None,
    max_drop_pct: float = 50.0,
    aoi: Mapping[str, Any] | None = None,
    partial: bool = False,
    allow_dropped: Iterable[str] = (),
) -> dict[str, Any]:
    """Collect ``states/*.json`` into ``manifest.json``.

    * Each state names the object keys of its own files and of its area of interest.
    * ``partial`` (a rebuild of chosen states): every state of ``previous`` not rebuilt is carried
      forward unchanged, under its own version's prefix, so the manifest still covers every state.
    * A full build refuses (QualityGateFailed) a state of ``previous`` that it did not build, unless
      named in ``allow_dropped``: a plan that lost a state (lines input changed, a state shape missed)
      would otherwise remove that state's flood data without a word.
    * A state whose SFHA rows fell by more than ``max_drop_pct`` is refused: the signature of a
      truncated download, not of FEMA remapping a state.

    The manifest is written either way (for the run log); the caller moves ``latest.json`` only when
    this returns."""
    prefix = f"{R2_PREFIX}/{version}"
    built: dict[str, Any] = {}
    for path in sorted((out_dir / "states").glob("*.json")):
        s = json.loads(path.read_text(encoding="utf-8"))
        built[s["state"]] = _state_entry(s, prefix=prefix, version=version, aoi=aoi)
    allowed = {s.strip().upper() for s in allow_dropped if s.strip()}
    prev_states: Mapping[str, Any] = (previous or {}).get("states") or {}
    prev_version = (previous or {}).get("version")
    states: dict[str, Any] = dict(built)
    carried: list[str] = []
    dropped: list[str] = []
    failures: list[str] = []
    for state in sorted(set(prev_states) - set(built)):
        if partial:
            states[state] = _carried_entry(prev_states[state], previous or {})
            carried.append(state)
        elif state in allowed:
            dropped.append(state)
        else:
            failures.append(
                f"{state}: in the previous manifest ({prev_version}) but not built now; a full build "
                f"must rebuild every state or name it in --allow-dropped"
            )
    changes: dict[str, Any] = {}
    for state, s in built.items():
        now = s["files"]["sfha"]["rows"]
        before = ((prev_states.get(state) or {}).get("files") or {}).get("sfha", {}).get("rows")
        if before:
            pct = round(100.0 * (now - before) / before, 1)
            changes[state] = {"sfha_rows_before": before, "sfha_rows_now": now, "change_pct": pct}
            if pct < -max_drop_pct:
                failures.append(f"{state}: SFHA rows {before} -> {now} ({pct}%)")
    states = dict(sorted(states.items()))
    manifest = {
        "source_id": SOURCE_ID,
        "version": version,
        "built_at": _now_iso(),
        # Where this build's own files and this manifest live; a carried state keeps its own prefix.
        "r2_prefix": prefix,
        "partial": partial,
        "layers": LAYER_NOTES,
        "finder_rule": list(FINDER_RULE),
        "reference_implementation": REFERENCE_IMPLEMENTATION,
        "aoi": dict(aoi) if aoi else None,
        "states": states,
        "states_built": sorted(built),
        "states_carried": carried,
        "states_dropped": dropped,
        "previous_version": prev_version,
        "change_vs_previous": changes,
        "totals": {
            "states": len(states),
            "sfha_rows": sum(s["files"]["sfha"]["rows"] for s in states.values()),
            "zones_other_rows": sum(s["files"]["zones_other"]["rows"] for s in states.values()),
            "coverage_rows": sum(s["files"]["coverage"]["rows"] for s in states.values()),
            "bytes": sum(f["bytes"] for s in states.values() for f in s["files"].values()),
            "bytes_fetched": sum((s.get("download") or {}).get("bytes_fetched", 0) for s in built.values()),
        },
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    if failures:
        raise QualityGateFailed("; ".join(failures))
    return manifest


# ---------------------------------------------------------------- the finder's rule (reference)
UNAVAILABLE_CATEGORIES = ("zone_d", "not_included", "unrecognised")
REPORTED_CATEGORIES = ("shaded_x", "open_water")


def local_path(key: str, data_root: pathlib.Path = DATA_DIR) -> pathlib.Path:
    """An object key under the store's ``data/`` prefix -> the same file under a local data root
    (the layout `aws s3 sync` of that prefix produces)."""
    if not key.startswith("data/") or ".." in pathlib.PurePosixPath(key).parts:
        raise ValueError(f"not an object key under data/: {key!r}")
    return data_root / key.removeprefix("data/")


@dataclasses.dataclass(frozen=True)
class FloodAnswer:
    """The finder's answer for one site (`docs/25` §5.3). ``answer`` is "sfha" (in the floodplain),
    "outside" (outside the SFHA on the effective FIRM) or "unavailable", with ``reason``
    "not_computed", "no_digital_map", "zone_d", "not_included" or "unrecognised". ``reported`` holds
    the shaded X and open water the site comes near: shown beside the answer, never a reason to
    exclude. ``firm_panels`` are the covering panels under the site, for a link to each FIRM."""

    answer: str
    reason: str | None = None
    reported: tuple[str, ...] = ()
    firm_panels: tuple[str, ...] = ()


class _Pieces:
    def __init__(self, rows: Sequence[Mapping[str, Any]], geoms: Sequence[Any]) -> None:
        self.rows = list(rows)
        self.geoms = list(geoms)
        self.tree = STRtree(self.geoms) if self.geoms else None

    def near(self, geom: Any, distance: float) -> list[int]:
        if self.tree is None:
            return []
        return sorted(int(i) for i in self.tree.query(geom, predicate="dwithin", distance=distance))


class FloodLayer:
    """One build's flood files for a set of states, and the finder's rule over them (`FINDER_RULE`):
    the reference implementation the finder can call or port. ``aoi`` maps a state to its area of
    interest; each layer is (rows, geometries) as `read_geoparquet` returns them, all states together."""

    def __init__(
        self,
        *,
        aoi: Mapping[str, Any],
        sfha: tuple[Sequence[Mapping[str, Any]], Sequence[Any]],
        zones_other: tuple[Sequence[Mapping[str, Any]], Sequence[Any]],
        coverage: tuple[Sequence[Mapping[str, Any]], Sequence[Any]],
        tolerance_m: float = DEFAULT_TOLERANCE_M,
    ) -> None:
        self.tolerance_m = tolerance_m
        self.sfha = _Pieces(*sfha)
        self.other = _Pieces(*zones_other)
        cov_rows, cov_geoms = coverage
        covering = [
            (r, g) for r, g in zip(cov_rows, cov_geoms, strict=True) if r.get("covers") and g is not None
        ]
        self.panels = _Pieces([r for r, _ in covering], [g for _, g in covering])
        aois = {s: g for s, g in aoi.items() if g is not None and not g.is_empty}
        self.computed = shapely.union_all(list(aois.values())) if aois else shapely.Polygon()
        # Mapped and computed: per state, its area of interest within its own covering panels. The
        # panels are grown by the simplification tolerance (degree space), which closes the seams
        # independently simplified neighbours leave and no more.
        grow = tolerance_degrees(tolerance_m)
        mapped = []
        for state, area in aois.items():
            mine = [g for r, g in covering if r.get("state") == state]
            if mine:
                mapped.append(shapely.intersection(area, shapely.buffer(shapely.union_all(mine), grow)))
        self.mapped = shapely.union_all(mapped) if mapped else shapely.Polygon()
        shapely.prepare(self.computed)
        shapely.prepare(self.mapped)

    @classmethod
    def load(
        cls,
        manifest: Mapping[str, Any],
        *,
        data_root: pathlib.Path = DATA_DIR,
        states: Iterable[str] | None = None,
    ) -> FloodLayer:
        """The states of a manifest (default: all), each read from its own keys under ``data_root``."""
        wanted = sorted(manifest["states"]) if states is None else sorted({s.upper() for s in states})
        aoi: dict[str, Any] = {}
        layers: dict[str, tuple[list[dict[str, Any]], list[Any]]] = {
            k: ([], []) for k in ("sfha", "zones_other", "coverage")
        }
        for state in wanted:
            entry = manifest["states"][state]
            aoi[state] = aoi_for_state(local_path(entry["aoi"]["key"], data_root), state)
            for layer, (rows, geoms) in layers.items():
                table, g = read_geoparquet(local_path(entry["files"][layer]["key"], data_root))
                rows.extend(table.drop_columns(["geometry", "bbox"]).to_pylist())
                geoms.extend(g)
        tolerance = max(
            (float(manifest["states"][s].get("tolerance_m") or DEFAULT_TOLERANCE_M) for s in wanted),
            default=DEFAULT_TOLERANCE_M,
        )
        return cls(
            aoi=aoi,
            sfha=layers["sfha"],
            zones_other=layers["zones_other"],
            coverage=layers["coverage"],
            tolerance_m=tolerance,
        )

    def _near_distance(self, geom: Any) -> float:
        """``tolerance_m`` as a degree distance that is at least that many metres in every direction
        at the site's latitude farthest from the equator."""
        lat = min(89.0, max(abs(geom.bounds[1]), abs(geom.bounds[3])))
        return self.tolerance_m / (M_PER_DEG * math.cos(math.radians(lat)))

    def assess(self, geom: Any) -> FloodAnswer:
        """`FINDER_RULE` for one site: a point, or an area such as a candidate band."""
        near = self._near_distance(geom)
        other = [self.other.rows[i]["category"] for i in self.other.near(geom, near)]
        reported = tuple(c for c in REPORTED_CATEGORIES if c in other)
        panels: tuple[str, ...] = ()
        if self.panels.tree is not None:
            hits = self.panels.tree.query(geom, predicate="intersects")
            panels = tuple(sorted({str(self.panels.rows[int(i)].get("firm_panel")) for i in hits}))
        if self.sfha.near(geom, near):
            return FloodAnswer("sfha", None, reported, panels)
        if not self.computed.covers(geom):
            return FloodAnswer("unavailable", "not_computed", reported, panels)
        if not self.mapped.covers(geom):
            return FloodAnswer("unavailable", "no_digital_map", reported, panels)
        for category in UNAVAILABLE_CATEGORIES:
            if category in other:
                return FloodAnswer("unavailable", category, reported, panels)
        return FloodAnswer("outside", None, reported, panels)


# ------------------------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.context.fema_nfhl",
        description="FEMA NFHL flood polygons near high-voltage lines (docs/25 section 5)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_aoi = sub.add_parser("aoi", help="areas of interest: buffers around lines of min-kv and up, per state")
    p_aoi.add_argument("--lines", type=pathlib.Path, required=True)
    p_aoi.add_argument("--voltage-column", default="voltage_kv")
    p_aoi.add_argument("--min-kv", type=float, default=DEFAULT_MIN_KV)
    p_aoi.add_argument("--buffer-miles", type=float, default=DEFAULT_BUFFER_MILES)
    p_aoi.add_argument("--margin-m", type=float, default=AOI_MARGIN_M)
    p_aoi.add_argument(
        "--states", default="", help="comma-separated postal codes; default every state touched"
    )
    p_aoi.add_argument("--out", type=pathlib.Path, required=True)
    p_aoi.add_argument("--list-states", action="store_true", help="print the states as a JSON list only")

    p_prod = sub.add_parser("product", help="the state's current NFHL product, from the MSC search")
    p_prod.add_argument("--state", required=True)

    p_state = sub.add_parser("state", help="fetch, clip and write one state")
    p_state.add_argument("--state", required=True)
    p_state.add_argument("--aoi", type=pathlib.Path, required=True)
    p_state.add_argument("--out-dir", type=pathlib.Path, required=True)
    p_state.add_argument(
        "--product-file", help="NFHL_48_20261013.zip or a county 48201C_20260517.zip; skips the search"
    )
    p_state.add_argument("--source", type=pathlib.Path, help="a local .gdb or shapefile directory; no fetch")
    p_state.add_argument("--work-dir", type=pathlib.Path)
    p_state.add_argument("--keep-download", action="store_true")
    p_state.add_argument("--tolerance-m", type=float, default=DEFAULT_TOLERANCE_M)

    p_man = sub.add_parser("manifest", help="collect the state summaries; refuse a large SFHA drop")
    p_man.add_argument("--out-dir", type=pathlib.Path, required=True)
    p_man.add_argument("--version", required=True)
    p_man.add_argument("--previous", type=pathlib.Path)
    p_man.add_argument("--aoi-summary", type=pathlib.Path)
    p_man.add_argument("--max-drop-pct", type=float, default=50.0)
    p_man.add_argument(
        "--partial",
        action="store_true",
        help="a rebuild of chosen states: carry every other state of --previous forward unchanged",
    )
    p_man.add_argument(
        "--allow-dropped",
        default="",
        help="states of --previous a full build may leave out (comma or space separated); default none",
    )

    args = parser.parse_args(argv)
    result: Any
    if args.cmd == "aoi":
        t0 = time.monotonic()
        states = [s.strip().upper() for s in args.states.split(",") if s.strip()] or None
        rows, geoms, summary = build_aoi(
            lines_from_parquet(args.lines, args.voltage_column),
            min_kv=args.min_kv,
            buffer_miles=args.buffer_miles,
            margin_m=args.margin_m,
            states=states,
        )
        size = write_geoparquet(args.out, rows, geoms, AOI_SCHEMA)
        if args.list_states:
            result = [r["state"] for r in rows]
        else:
            result = {
                **summary,
                "out": str(args.out),
                "bytes": size,
                "seconds": round(time.monotonic() - t0, 1),
            }
    elif args.cmd == "product":
        result = dataclasses.asdict(find_state_product(args.state.upper()))
    elif args.cmd == "state":
        state = args.state.upper()
        product = product_from_file(state, args.product_file) if args.product_file else None
        result = build_state(
            state=state,
            aoi=aoi_for_state(args.aoi, state),
            out_dir=args.out_dir,
            product=product,
            source=args.source,
            work_dir=args.work_dir,
            keep_download=args.keep_download,
            tolerance_m=args.tolerance_m,
        )
    else:
        previous = (
            json.loads(args.previous.read_text(encoding="utf-8"))
            if args.previous and args.previous.exists()
            else None
        )
        aoi = json.loads(args.aoi_summary.read_text(encoding="utf-8")) if args.aoi_summary else None
        manifest = build_manifest(
            args.out_dir,
            version=args.version,
            previous=previous,
            max_drop_pct=args.max_drop_pct,
            aoi=aoi,
            partial=args.partial,
            allow_dropped=re.split(r"[\s,]+", args.allow_dropped),
        )
        result = {
            "version": manifest["version"],
            "states_built": manifest["states_built"],
            "states_carried": manifest["states_carried"],
            **manifest["totals"],
        }
    print(json.dumps(result, sort_keys=True, default=str))  # noqa: T201 - CLI summary line


if __name__ == "__main__":
    main()
