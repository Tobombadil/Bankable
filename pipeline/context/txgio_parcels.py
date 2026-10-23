"""Texas land parcels for the site finder: TxGIO StratMap Land Parcels -> per-county GeoParquet of parcels
and same-owner holdings, with no owner names (`us.tx.txgio.parcels`; docs/25-siting-parcels.md; owner
decisions 2026-10-23 "Site finder Pro ... land parcels" and "Inframation serves the owner's own
powered-land deals first").

**Source.** TxGIO (Texas Geographic Information Office, Texas Water Development Board) compiles the
appraisal districts' parcel files into one schema. The catalogue behind data.geographic.texas.gov
(`api.tnris.org/api/v1/collections/?search=parcels`) lists one collection per vintage; the newest
"Land Parcels" record (2026: acquired 2026-09-25, published 2026-10-01, `CC0-1.0`, spatial reference
4326) has one zip per county (253 of 254; Donley is excluded) and a statewide zip, listed by
`api.tnris.org/api/v1/resources/?collection_id=<id>`. Only the county zips are read, one at a time
(the statewide zip is 2.7 GB). TxGIO's map service (`stratmap_land_parcels_48_most_recent/MapServer`)
answers every `query` with "Requested operation is not supported by this service" (measured
2026-10-23), so it is not a route; it is never asked for features any other way.

**Privacy rule (docs/25-siting-parcels.md §4.7 option (b), stricter: no keyed owner hash is kept
either).** The owner name is read into memory, normalised (`owner_key`), used to group contiguous
parcels that share it, and dropped: no owner name, care-of name, mailing address or situs address is
ever written to an output, a summary, a log or an exception. The mailing, care-of, situs and legal
description columns are not even read. The output carries a build-local holding id (`secrets`, random
per build, never derived from the name), each holding's parcel count, acres and owner count (1: a
holding is one owner's contiguous land), and per parcel only the TxGIO record id, county FIPS, the
appraisal district's property id, acres (computed in an equal-area projection, the stated GIS acreage
beside it), the state land-use code and its category, land and market value with the tax year, the
holding's figures and the outline simplified for display (the geographic id is not read). A parcel
whose owner is blank, a placeholder ("NOT ON FILE", "VARIOUS", "ROW", "SEE ACCT ...") or marked
confidential (Tax Code §25.025 elections show as "CONFIDENTIAL", "25.025", "RESTRICTED" and the like;
`owner_screen`) never joins another parcel: it is a holding of one, indistinguishable from any other
single-parcel holding, so the output never marks which parcels belong to a protected individual. The
county summary counts each class, and the kept names that still look like a marker, as counts only.
The owner is read on the appraisal district's own record, through the link the site finder gives
(`services/site_finder/cad_links.py`).

**Holdings.** Parcels whose normalised owner is the same and that touch or lie within 30 m of each
other (`GROUP_DISTANCE_M`, measured in the equal-area plane: a county road or a creek between two
tracts of one owner does not split them) form one holding, transitively. Two holdings of one owner more
than 30 m apart stay two holdings, and nothing in the output says they share an owner.

**Stacked and overlapping outlines.** Texas rolls draw several accounts on one geography (a land account
and the mobile home or improvement account on it; each heir's undivided interest; condominium units),
and TxGIO changes no value. A holding's acres count each outline once (`outline_ids`) and, where its
outlines overlap, are their union's area; its parcel count is its distinct outlines (`accounts` beside
it). Land that another holding's outline also covers (two owners on one outline; a small account drawn
inside a large one) is the holding's `overlap_acres`, which the site finder subtracts before adding the
holding to an assemblage, so one tract is never counted twice. The county summary counts repeated
outlines, overlapping pairs and their acres (`outlines`).

**Kinds.** A holding is `land`, or one of the kinds the site finder leaves out of its acreage figures:
a right-of-way `strip` (20 acres or more, on average under 60 m across), `exempt` land (category X) or
`utility` land (category J, railroads included).

**Pipeline.** Sub-commands (`python -m pipeline.context.txgio_parcels ...`), run by
`.github/workflows/parcels-layer.yml` through `infra/scripts/build_parcels_layer.sh`:

1. ``plan``: the newest vintage and its county zips from the catalogue (`--check` also asks each zip's
   size with a HEAD request: the dry run).
2. ``county``: one county: download the zip (or take `--zip`), read it with pyogrio, group, write
   `parcels/<fips>.parquet`, `holdings/<fips>.parquet` and `counties/<fips>.json` (the county's
   summary, or its failure), delete the zip.
3. ``manifest``: the counties' summaries -> `manifest.json`, carrying forward from the published
   `latest.json` every county this build did not build or failed to build, and refusing (exit 1) when a
   published county would be lost or a county's parcels fell by more than half.
4. ``report``: the manifest as a Markdown table (the workflow's step summary).
5. ``pull``: a server fetches `latest.json` and the files of the counties `PARCELS_COUNTIES` names
   (`all`, or FIPS codes) from the private object store with its own credentials (`R2_*`), checks each
   sha256, and writes them under `<data root>/normalized/context/us.tx.txgio.parcels/<version>/` with
   `current.json` last (`PARCELS_DATA_DIR` in place of that directory when set: the readers'
   `services.context_sample.parcels.parcels_dir`). Run by the server's monthly `context_build`
   (`infra/scheduler/jobs.py` `CONTEXT_BUILDERS`). Before the first build, or when none of those
   counties is published, it answers `status: not_published` (exit 0) and touches nothing.

Outputs are GeoParquet 1.1 (WKB, a `bbox` covering struct, rows in Z-order, zstd), read by
`services/context_sample/parcels.py`. They live in the private object store under `OBJECT_PREFIX`
(`R2_BUCKET`, as the flood build's do), never on the public tiles bucket: the source is `derived_only`
(docs/13: do not mirror the source table), and the per-parcel file is that table less its owner columns.
The public reads them only through the API, behind the register gate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import resource
import secrets
import shutil
import sys
import tempfile
import time
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa  # type: ignore[import-untyped,unused-ignore]
import pyarrow.parquet as pq  # type: ignore[import-untyped,unused-ignore]

SOURCE_ID = "us.tx.txgio.parcels"
COLLECTIONS_URL = "https://api.tnris.org/api/v1/collections/?search=parcels"
RESOURCES_URL = "https://api.tnris.org/api/v1/resources/?collection_id={cid}&limit=400"
COLLECTION_PAGE = "https://data.geographic.texas.gov/collection/?c={cid}"
#: TxGIO's own map service: its `query` is switched off (module docstring), so it is named, never used.
SERVICE_URL = (
    "https://feature.geographic.texas.gov/arcgis/rest/services/Parcels/"
    "stratmap_land_parcels_48_most_recent/MapServer"
)
CATALOGUE_HOST = "api.tnris.org"
DOWNLOAD_HOST = "data.geographic.texas.gov"
#: Requests per second: the catalogue API twice a second at most, the file host once per five seconds.
RATE_LIMITS = {CATALOGUE_HOST: 0.5, DOWNLOAD_HOST: 0.2}
LICENCE = (
    "CC0 1.0 Universal (TxGIO DataHub catalogue record, license_name 'Creative Commons Zero v1.0 "
    "Universal'); TWDB: 'freely grants permission to copy and distribute its materials'"
)
ATTRIBUTION = (
    "Source: Texas Geographic Information Office (TWDB), StratMap Land Parcels, "
    "https://geographic.texas.gov; data from Texas appraisal districts"
)
NOTICE = (
    "Neither Inframation nor this information, as presented on Inframation, is endorsed by the State of "
    "Texas or any state agency. Not survey grade; not for legal purposes."
)
#: Bumped when a published column changes meaning; part of the bucket key.
LAYOUT_VERSION = "v1"
#: Where the build writes, in the private object store (`R2_BUCKET`, the bucket the flood and prospect
#: builds use; never the public tiles bucket): the per-parcel files are a derived-only source's table.
#: The local layout mirrors it under the data root (`services.context_sample.parcels.parcels_dir`, the
#: directory the site finder and the API read).
OBJECT_PREFIX = f"data/normalized/context/{SOURCE_ID}"
LATEST_NAME = "latest.json"
MANIFEST_NAME = "manifest.json"
CURRENT_NAME = "current.json"
#: Parcels of one owner this close (metres, equal-area plane) are one holding.
GROUP_DISTANCE_M = 30.0
#: Display outlines: Douglas-Peucker at about a metre, coordinates on a 1e-6 degree grid (about 0.1 m).
SIMPLIFY_DEG = 0.00001
PRECISION_DEG = 1e-6
ACRE_M2 = 4046.8564224
SQFT_PER_ACRE = 43_560.0
ROW_GROUP_SIZE = 2048
#: A county's parcels may not fall by more than this share between published builds (a truncated file).
MAX_DROP = 0.5
#: Texas is FIPS state 48; a county code is 48 + three digits.
FIPS_RE = re.compile(r"^48\d{3}$")
COUNTY_ZIP_RE = re.compile(r"landparcels_(48\d{3})_lp\.zip$", re.I)
VERSION_RE = re.compile(r"^v\d+/[0-9A-Za-z._-]+$")

# --------------------------------------------------------------------------------------- land use
#: The Comptroller's property categories (Texas Property Tax Assistance, "Property Classification Guide",
#: March 2026, https://comptroller.texas.gov/taxes/property-tax/docs/96-313.pdf, read 2026-10-23), as
#: `STAT_LAND_USE` carries them: category key, then the guide's heading in plain words. Codes are read
#: as letter plus digit where the guide splits a letter (C1/C2, D1/D2, F1/F2, L1/L2), else by letter
#: (appraisal districts write A1, E3, J4 ...). A blank code is "not stated", never "excluded".
LAND_USE_BY_CODE: dict[str, tuple[str, str]] = {
    "A": ("residential", "Single-family residential"),
    "B": ("residential", "Multifamily residential"),
    "C1": ("vacant", "Vacant lots and tracts"),
    "C2": ("vacant", "Colonia lots and land tracts"),
    "D1": ("open_space", "Qualified open-space land (agricultural appraisal)"),
    "D2": ("open_space", "Farm and ranch improvements on open-space land"),
    "E": ("rural", "Rural land, not qualified for open-space appraisal"),
    "F1": ("commercial", "Commercial"),
    "F2": ("industrial", "Industrial"),
    "G": ("minerals", "Oil, gas, minerals and other subsurface interests"),
    "J": ("utilities", "Utilities"),
    "L1": ("business_personal", "Commercial personal property"),
    "L2": ("business_personal", "Industrial personal property"),
    "M": ("mobile_homes", "Mobile homes"),
    "O": ("residential", "Residential inventory"),
    "S": ("inventory", "Special inventory"),
    "X": ("exempt", "Exempt property"),
}
LAND_USE_LABELS: dict[str, str] = {
    "residential": "Residential",
    "vacant": "Vacant lots and tracts",
    "open_space": "Agricultural open-space land",
    "rural": "Rural land, not agricultural",
    "commercial": "Commercial",
    "industrial": "Industrial",
    "minerals": "Minerals and subsurface",
    "utilities": "Utilities",
    "business_personal": "Business personal property",
    "mobile_homes": "Mobile homes",
    "inventory": "Inventory",
    "exempt": "Exempt property",
    "unknown": "Land use not recognised",
    "not_stated": "Land use not stated",
}
_LETTER_FALLBACK = {"C": "vacant", "D": "open_space", "F": "commercial", "L": "business_personal"}


def land_use_category(code: Any) -> str:
    """The category key of a `STAT_LAND_USE` code (`not_stated` for a blank, `unknown` for a code the
    Comptroller's scheme does not have)."""
    text = re.sub(r"[^A-Z0-9]", "", str(code or "").upper())
    if not text:
        return "not_stated"
    if text[:2] in LAND_USE_BY_CODE:
        return LAND_USE_BY_CODE[text[:2]][0]
    if text[0] in LAND_USE_BY_CODE:
        return LAND_USE_BY_CODE[text[0]][0]
    return _LETTER_FALLBACK.get(text[0], "unknown")


# ------------------------------------------------------------------------------------------ owners
#: Confidentiality markers (Tax Code §25.025 elections and the words districts write for them), anywhere
#: in the owner string: "CONFIDENTIAL", its misspellings and abbreviations ("CONFIDENTAL", "CONFID"),
#: "25.025", "RESTRICTED", "PROTECTED", "WITHHELD", "REDACTED". Matching is deliberately wide: a real
#: name caught here only stays a holding of one parcel, which is the safe direction.
_WITHHELD = re.compile(
    r"\b(CONFI?D[A-Z]*|CONFIDENTAL|WITHHELD|WITHHOLD[A-Z]*|REDACTED|SUPPRESSED|NOT DISCLOSED|"
    r"NON ?DISCLOSURE|PROTECTED|RESTRICTED|25 ?025|SEC(?:TION)? 25)\b"
)
#: Whole owner strings that withhold the name ("PRIVATE"), where the word alone would also catch firms.
_WITHHELD_EXACT = frozenset(
    {
        "PRIVATE",
        "PRIVATE OWNER",
        "PRIVATE OWNERSHIP",
        "PRIVATE PROPERTY",
        "PRIVATE INFORMATION",
        "PRIVATE RECORD",
        "OWNER PRIVATE",
        "NAME PRIVATE",
    }
)
#: Whole owner strings that name nobody: blanks, "unknown", "not on file", "various", right of way and
#: roads, "see account". Two unrelated neighbours with one of these must never become one holding.
_NOBODY = frozenset(
    {
        "UNKNOWN",
        "UNKNOWN OWNER",
        "UNKNOWN OWNERS",
        "OWNER UNKNOWN",
        "OWNERS UNKNOWN",
        "UNKNOWN OWNER OF RECORD",
        "NO OWNER",
        "NO OWNER OF RECORD",
        "NONE",
        "NULL",
        "NA",
        "N A",
        "TBD",
        "UNK",
        "NOT ON FILE",
        "NAME NOT ON FILE",
        "OWNER NOT ON FILE",
        "NO NAME",
        "NONAME",
        "NO NAME ON FILE",
        "NO NAME GIVEN",
        "NOT AVAILABLE",
        "NOT APPLICABLE",
        "VARIOUS",
        "VARIOUS OWNERS",
        "MULTIPLE OWNERS",
        "SEVERAL OWNERS",
        "ROW",
        "RIGHT OF WAY",
        "PUBLIC ROW",
        "ROAD RIGHT OF WAY",
        "PUBLIC ROAD",
        "PUBLIC ROADS",
        "ROAD",
        "ROADS",
        "STREET",
        "STREETS",
        "ALLEY",
        "COUNTY ROAD",
        "EASEMENT",
        "PUBLIC",
        "OWNER",
        "OWNERS",
        "UNASSIGNED",
        "UNDETERMINED",
        "UNDEFINED",
        "UNIDENTIFIED",
        "DELETED",
        "VOID",
        "PENDING",
        "NEW OWNER",
        "CURRENT OWNER",
        "OCCUPANT",
        "RESIDENT",
        "TAXPAYER",
        "NO DATA",
        "MISSING",
        "BLANK",
        "TEST",
        "X",
        "XX",
        "XXX",
    }
)
#: Owner strings that point elsewhere or carry no name: "SEE ACCT 12345", "SAME AS R1234", "UNKNOWN HEIRS
#: OF ...", digits only.
_NOBODY_PATTERN = re.compile(r"^(?:SEE|SAME AS|REFER TO|UNKNOWN|NOT ON FILE|NO NAME)\b|^[0-9 ]+$")
#: Kept names that still look like a placeholder or a marker. Counted per county (never written) so the
#: first real build shows whether a district uses a word the lists above lack.
_SUSPECT = re.compile(
    r"\b(UNKNOWN|VARIOUS|FILE|NONE|ROW|RIGHT OF WAY|ROADS?|STREETS?|HIGHWAY|HWY|ALLEY|EASEMENT|PRIVATE|"
    r"ACCT|ACCOUNT|RECORD|TBD|PENDING|DISCLOS[A-Z]*|DELETED|VOID|TEST|DUMMY|PLACEHOLDER|MISC[A-Z]*)\b"
    r"|^[A-Z]{1,3} ?[0-9]{3,}$"
)
#: `owner_screen` classes.
OWNER_CLASSES = ("named", "blank", "withheld", "placeholder")
_SUFFIXES = {
    "INCORPORATED": "INC",
    "CORPORATION": "CORP",
    "COMPANY": "CO",
    "LIMITED": "LTD",
    "PARTNERSHIP": "PARTNERS",
    "TRUSTEES": "TRUSTEE",
    "TRUSTS": "TRUST",
    "TR": "TRUST",
    "ESTATE": "EST",
}


def owner_key(raw: Any) -> str | None:
    """A grouping key for an owner string, or None when it names nobody (blank, "unknown", "see
    account", right of way) or is withheld ("confidential", "25.025"). Case, accents, punctuation and
    spacing do not matter (`L.L.C.`, `LLC` and `L L C` are one), `&` reads as AND, and a few corporate
    words are written one way. Distinct co-owner lists ("A AND B" against "A") stay distinct. The key
    lives in memory only."""
    return owner_screen(raw)[0]


def owner_suspect(key: str) -> bool:
    """Whether a kept grouping key still looks like a placeholder or a marker (`_SUSPECT`; counted only)."""
    return bool(_SUSPECT.search(key)) or len(key.replace(" ", "")) <= 2


def owner_screen(raw: Any) -> tuple[str | None, str]:
    """(`owner_key`, class): `named`, `blank`, `withheld` or `placeholder` (`OWNER_CLASSES`)."""
    if raw is None:
        return None, "blank"
    text = unicodedata.normalize("NFKD", str(raw)).encode("ascii", "ignore").decode("ascii").upper()
    text = text.replace("&", " AND ")
    tokens = re.sub(r"[^A-Z0-9 ]+", " ", text).split()
    if not tokens:
        return None, "blank"
    joined = " ".join(tokens)
    if _WITHHELD.search(joined) or joined in _WITHHELD_EXACT:
        return None, "withheld"
    key = _merge_initialisms(tokens)
    if joined in _NOBODY or key in _NOBODY or _NOBODY_PATTERN.search(joined):
        return None, "placeholder"
    return key, "named"


def _merge_initialisms(tokens: Sequence[str]) -> str:
    """The key of a tokenised owner string: initialisms joined, corporate words written one way."""
    merged: list[str] = []
    run = ""
    for token in tokens:  # "L L C" -> "LLC": a run of single letters is one initialism
        if len(token) == 1 and token.isalpha():
            run += token
            continue
        if run:
            merged.append(run)
            run = ""
        merged.append(token)
    if run:
        merged.append(run)
    return " ".join(_SUFFIXES.get(t, t) for t in merged)


# ------------------------------------------------------------------------------------- projection
#: GRS80, the ellipsoid of NAD83 and (to a millimetre here) WGS84.
_A = 6_378_137.0
_F = 1 / 298.257222101
_E2 = _F * (2 - _F)
_E = math.sqrt(_E2)


def _q(sin_phi: Any) -> Any:
    s = np.asarray(sin_phi, dtype=np.float64)
    return (1 - _E2) * (s / (1 - _E2 * s * s) - (1 / (2 * _E)) * np.log((1 - _E * s) / (1 + _E * s)))


def _m(phi: float) -> float:
    return math.cos(phi) / math.sqrt(1 - _E2 * math.sin(phi) ** 2)


@dataclass(frozen=True)
class Albers:
    """Albers equal-area conic on GRS80 (Snyder 1987, eqs. 14-1 to 14-6). The default parameters are
    Texas Centric Albers Equal Area (EPSG:3083) without its false origin: areas are exact on the
    ellipsoid, and distances are within about 1.5% anywhere in Texas, plenty for a 30 m join."""

    lat1: float = 27.5
    lat2: float = 35.0
    lat0: float = 18.0
    lon0: float = -100.0

    def _constants(self) -> tuple[float, float, float]:
        p1, p2, p0 = (math.radians(v) for v in (self.lat1, self.lat2, self.lat0))
        m1, m2 = _m(p1), _m(p2)
        q1, q2, q0 = (float(_q(math.sin(p))) for p in (p1, p2, p0))
        n = (m1 * m1 - m2 * m2) / (q2 - q1)
        c = m1 * m1 + n * q1
        rho0 = _A * math.sqrt(c - n * q0) / n
        return n, c, rho0

    def forward(self, lon: Any, lat: Any) -> tuple[Any, Any]:
        n, c, rho0 = self._constants()
        q = _q(np.sin(np.radians(np.asarray(lat, dtype=np.float64))))
        rho = _A * np.sqrt(c - n * q) / n
        theta = n * np.radians(np.asarray(lon, dtype=np.float64) - self.lon0)
        return rho * np.sin(theta), rho0 - rho * np.cos(theta)

    def transform(self, coords: Any) -> Any:
        """(n, 2) lon/lat -> (n, 2) metres, for `shapely.transform`."""
        x, y = self.forward(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])


TEXAS_ALBERS = Albers()


def ellipsoid_band_area_m2(lat_south: float, lat_north: float, dlon_deg: float) -> float:
    """The exact area of a latitude-longitude quadrangle on GRS80 (the test of `Albers`)."""
    qs, qn = (float(_q(math.sin(math.radians(v)))) for v in (lat_south, lat_north))
    return _A * _A * math.radians(dlon_deg) * (qn - qs) / 2


# ------------------------------------------------------------------------------------- one county
class ParcelsError(RuntimeError):
    """A county or a build cannot be completed; the message never carries an owner value."""


@dataclass
class CountyFrame:
    """One county's parcels as read, in memory. `owner` is the only personal field and never leaves
    this object: `build_county` turns it into integer group codes and drops it. It is left out of the
    object's repr, so no debug print or traceback that shows locals can show a name."""

    fips: str
    name: str
    txgio_id: list[int]
    prop_id: list[str | None]
    owner: list[str | None] = field(repr=False)
    gis_area: list[float | None]
    gis_area_unit: list[str | None]
    land_use_code: list[str | None]
    land_value: list[float | None]
    market_value: list[float | None]
    tax_year: list[str | None]
    #: Shapely polygons in lon/lat (EPSG:4326).
    geometry: Any
    source_url: str = ""
    retrieved_at: str = ""

    def __len__(self) -> int:
        return len(self.txgio_id)


@dataclass(frozen=True)
class Provenance:
    source_url: str
    retrieved_at: str
    licence: str = LICENCE

    def columns(self) -> dict[str, str]:
        return {
            "source_id": SOURCE_ID,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "licence": self.licence,
        }


def gis_acres(value: Any, unit: Any) -> float | None:
    """The file's own GIS area in acres, when its unit is one this can read."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    u = re.sub(r"[^A-Z]", "", str(unit or "").upper())
    if u in ("AC", "ACRE", "ACRES", "ACR"):
        return number
    if u in ("SQFT", "SF", "SQUAREFEET", "SQUAREFOOT", "FT", "SQFEET", "FT2"):
        return number / SQFT_PER_ACRE
    if u in ("SQM", "M", "SQUAREMETERS", "SQUAREMETRES", "M2", "SQMETERS"):
        return number / ACRE_M2
    if u in ("HA", "HECTARE", "HECTARES"):
        return number * 10_000 / ACRE_M2
    return None


def _clean_geometry(geoms: Any) -> tuple[Any, Any]:
    """Valid polygonal geometries (invalid ones repaired, collections reduced to their polygons) and a
    mask of the ones kept."""
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    geoms = np.asarray(geoms, dtype=object)
    keep = ~(shapely.is_missing(geoms) | shapely.is_empty(geoms))
    invalid = keep & ~shapely.is_valid(geoms)
    if invalid.any():
        geoms[invalid] = shapely.make_valid(geoms[invalid])
    types = shapely.get_type_id(geoms)
    # 3 Polygon, 6 MultiPolygon, 7 GeometryCollection (from make_valid).
    collections = keep & (types == 7)
    for i in np.nonzero(collections)[0]:
        parts = [p for p in shapely.get_parts(geoms[i]) if shapely.get_type_id(p) in (3, 6)]
        geoms[i] = shapely.union_all(parts) if parts else None
    types = shapely.get_type_id(geoms)
    keep &= np.isin(types, (3, 6)) & ~shapely.is_empty(geoms)
    return geoms, keep


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = np.arange(n, dtype=np.int64)

    def find(self, i: int) -> int:
        parent = self.parent
        root = i
        while parent[root] != root:
            root = int(parent[root])
        while parent[i] != root:  # path compression
            nxt = int(parent[i])
            parent[i] = root
            i = nxt
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if ra < rb:
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb


def group_holdings(owner_codes: Any, projected: Any, distance_m: float = GROUP_DISTANCE_M) -> Any:
    """Holding index per parcel (0..k-1): parcels with one owner code (>= 0) that touch or lie within
    `distance_m` of each other, transitively; a parcel with code -1 (no owner on record, or withheld) is
    a holding of its own."""
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    codes = np.asarray(owner_codes, dtype=np.int64)
    n = len(codes)
    uf = _UnionFind(n)
    counts = np.bincount(codes[codes >= 0]) if (codes >= 0).any() else np.zeros(0, dtype=np.int64)
    shared = np.zeros(n, dtype=bool)
    valid = codes >= 0
    shared[valid] = counts[codes[valid]] >= 2
    candidates = np.nonzero(shared)[0]
    if len(candidates) > 1:
        geoms = np.asarray(projected, dtype=object)[candidates]
        tree = shapely.STRtree(geoms)
        left, right = tree.query(geoms, predicate="dwithin", distance=distance_m)
        same = (left < right) & (codes[candidates[left]] == codes[candidates[right]])
        for a, b in zip(candidates[left[same]].tolist(), candidates[right[same]].tolist(), strict=True):
            uf.union(a, b)
    roots = np.fromiter((uf.find(i) for i in range(n)), dtype=np.int64, count=n)
    _, holding = np.unique(roots, return_inverse=True)
    return holding.astype(np.int64)


def _holding_ids(count: int, rng: Callable[[int], str] | None = None) -> list[str]:
    """`count` distinct random ids (`h` + 12 hex digits), drawn fresh for every build."""
    draw = rng or (lambda nbytes: secrets.token_hex(nbytes))
    seen: set[str] = set()
    out: list[str] = []
    while len(out) < count:
        candidate = "h" + draw(6)
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _morton(x: Any, y: Any) -> Any:
    """Interleave the bits of two 16-bit integer arrays (a Z-order key)."""
    key = np.zeros(len(x), dtype=np.int64)
    xi, yi = np.asarray(x, dtype=np.int64), np.asarray(y, dtype=np.int64)
    for bit in range(16):
        key |= ((xi >> bit) & 1) << (2 * bit)
        key |= ((yi >> bit) & 1) << (2 * bit + 1)
    return key


def _zorder(bounds: Any) -> Any:
    cx = (bounds[:, 0] + bounds[:, 2]) / 2
    cy = (bounds[:, 1] + bounds[:, 3]) / 2
    x0, x1, y0, y1 = cx.min(), cx.max(), cy.min(), cy.max()
    sx = 65535 / (x1 - x0) if x1 > x0 else 0.0
    sy = 65535 / (y1 - y0) if y1 > y0 else 0.0
    return np.argsort(
        _morton(((cx - x0) * sx).astype(np.int64), ((cy - y0) * sy).astype(np.int64)), kind="stable"
    )


#: Two accounts whose outlines agree after rounding to this (degrees, about 1 cm) are on one outline.
OUTLINE_KEY_DEG = 1e-7
#: The county summary counts apart the overlaps that cover at least this share of the smaller outline.
OVERLAP_REPORT_SHARE = 0.1
#: Overlaps under this many square metres (about 2 m by 2 m), or under this share of the smaller outline
#: (a 1 m sliver along a section line), are drawing noise, not shared land: two edges that meet in a
#: straight line in degrees do not quite meet in the equal-area plane, and adjacent tracts digitised apart
#: overlap along their common line. The holding's acres stay within that share of the union.
OVERLAP_MIN_M2 = 4.0
OVERLAP_MIN_SHARE = 0.001
#: A holding of `STRIP_MIN_ACRES` or more whose outline is on average narrower than `STRIP_WIDTH_M`
#: (twice its area over its perimeter: a 100 ft railroad right of way reads about 30 m, a square 20-acre
#: tract 142 m) is a right-of-way strip.
STRIP_MIN_ACRES = 20.0
STRIP_WIDTH_M = 60.0
#: What a holding is for the site finder's acreage figures: `land`, or one of the kinds left out of them:
#: a right-of-way `strip`, `exempt` land (category X: public, religious, school) or `utility` land
#: (category J, railroads included).
HOLDING_KINDS = ("land", "strip", "exempt", "utility")
#: One outline drawn under several accounts (a land account and a mobile home or other improvement on
#: it; undivided interests) takes the first of its accounts' categories in this order, land before what
#: stands on or lies under it. Equal acreages within a holding are decided the same way.
USE_PRIORITY: tuple[str, ...] = (
    "open_space",
    "rural",
    "vacant",
    "residential",
    "commercial",
    "industrial",
    "inventory",
    "utilities",
    "exempt",
    "mobile_homes",
    "business_personal",
    "minerals",
    "unknown",
    "not_stated",
)

#: One overlapping partner of a holding: its id (same county) and the acres the two share.
OVERLAP_STRUCT = pa.struct([pa.field("holding_id", pa.string()), pa.field("acres", pa.float64())])
BBOX_FIELDS = ("xmin", "ymin", "xmax", "ymax")
PARCEL_COLUMNS: tuple[tuple[str, Any], ...] = (
    ("parcel_key", pa.string()),
    ("txgio_id", pa.int64()),
    ("fips", pa.string()),
    ("prop_id", pa.string()),
    ("acres", pa.float64()),
    ("gis_acres", pa.float64()),
    ("land_use_code", pa.string()),
    ("land_use", pa.string()),
    ("land_value", pa.int64()),
    ("market_value", pa.int64()),
    ("tax_year", pa.string()),
    ("holding_id", pa.string()),
    ("holding_acres", pa.float64()),
    ("holding_parcels", pa.int32()),
    ("holding_land_use", pa.string()),
    ("holding_value_per_acre", pa.int64()),
    ("holding_link_prop_id", pa.string()),
    ("holding_kind", pa.string()),
    ("holding_overlaps", pa.list_(OVERLAP_STRUCT)),
    ("holding_overlap_rest_acres", pa.float64()),
)
HOLDING_COLUMNS: tuple[tuple[str, Any], ...] = (
    ("holding_id", pa.string()),
    ("fips", pa.string()),
    ("parcels", pa.int32()),
    ("accounts", pa.int32()),
    ("owner_count", pa.int32()),
    ("acres", pa.float64()),
    ("gis_acres", pa.float64()),
    ("overlap_acres", pa.float64()),
    ("overlap_partners", pa.int32()),
    ("kind", pa.string()),
    ("mean_width_m", pa.float64()),
    ("land_use", pa.string()),
    ("land_value", pa.int64()),
    ("market_value", pa.int64()),
    ("value_per_acre", pa.int64()),
    ("tax_year", pa.string()),
    ("link_prop_id", pa.string()),
    ("lon", pa.float64()),
    ("lat", pa.float64()),
    ("xmin", pa.float64()),
    ("ymin", pa.float64()),
    ("xmax", pa.float64()),
    ("ymax", pa.float64()),
)
PROVENANCE_COLUMNS = ("source_id", "source_url", "retrieved_at", "licence")


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _int_or_none(values: Iterable[float | None]) -> list[int | None]:
    out: list[int | None] = []
    for v in values:
        out.append(round(float(v)) if v is not None and math.isfinite(v) else None)
    return out


def _write(path: pathlib.Path, table: Any) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    pq.write_table(table, tmp, row_group_size=ROW_GROUP_SIZE, compression="zstd", write_statistics=True)
    tmp.replace(path)
    return {"rows": table.num_rows, "bytes": path.stat().st_size, "sha256": _sha256(path)}


def _box(bounds: Any) -> list[float] | None:
    """The box around every row's box, rounded to the stored precision."""
    if not len(bounds):
        return None
    return [
        round(float(bounds[:, 0].min()), 6),
        round(float(bounds[:, 1].min()), 6),
        round(float(bounds[:, 2].max()), 6),
        round(float(bounds[:, 3].max()), 6),
    ]


def _geo_metadata(bounds: Any, extra: Mapping[str, Any]) -> dict[bytes, bytes]:
    box = _box(bounds) or [0.0, 0.0, 0.0, 0.0]
    geo = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                "encoding": "WKB",
                "geometry_types": ["Polygon", "MultiPolygon"],
                "crs": None,
                "bbox": box,
                "covering": {"bbox": {n: ["bbox", n] for n in BBOX_FIELDS}},
            }
        },
    }
    return {
        b"geo": json.dumps(geo).encode(),
        b"inframation": json.dumps(dict(extra), sort_keys=True).encode(),
    }


# ---------------------------------------------------------------------------- stacked and overlapping
def outline_ids(geoms: Any) -> Any:
    """Each outline's index among the distinct ones (0..k-1, in order of first appearance). Accounts
    drawn on one outline share it whatever the start vertex or the ring's direction: Texas appraisal
    rolls draw a land account and the mobile home or improvement account on it, or each heir's
    undivided interest, on the same geography, and TxGIO changes no value."""
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    if not len(geoms):
        return np.zeros(0, dtype=np.int64)
    rounded = shapely.set_precision(np.asarray(geoms, dtype=object), OUTLINE_KEY_DEG, mode="pointwise")
    keys = shapely.to_wkb(shapely.normalize(rounded), output_dimension=2)
    seen: dict[bytes, int] = {}
    return np.fromiter(
        (seen.setdefault(k, len(seen)) for k in keys.tolist()), dtype=np.int64, count=len(keys)
    )


def _intersection_areas(a: Any, b: Any) -> Any:
    """The area of each pair's intersection; a pair GEOS cannot intersect as drawn is repaired first."""
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    try:
        return shapely.area(shapely.intersection(a, b))
    except shapely.errors.GEOSException:
        out = np.zeros(len(a))
        for k, (x, y) in enumerate(zip(a, b, strict=True)):
            try:
                out[k] = shapely.area(shapely.intersection(x, y))
            except shapely.errors.GEOSException:
                out[k] = shapely.area(shapely.intersection(shapely.make_valid(x), shapely.make_valid(y)))
        return out


def _union(geoms: Any) -> Any:
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    try:
        return shapely.union_all(geoms)
    except shapely.errors.GEOSException:
        return shapely.union_all(shapely.make_valid(np.asarray(geoms, dtype=object)))


def overlapping_outlines(projected: Any, reps: Any) -> tuple[Any, Any, Any]:
    """(outline i, outline j, overlap in square metres), i < j, for the distinct outlines whose
    interiors overlap by `OVERLAP_MIN_M2` and `OVERLAP_MIN_SHARE` of the smaller or more; `reps[o]` is the
    row of outline o's first account.
    Outlines that only touch along an edge do not overlap."""
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    none = (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), np.zeros(0))
    sub = np.asarray(projected, dtype=object)[np.asarray(reps, dtype=np.int64)]
    if len(sub) < 2:
        return none
    left, right = shapely.STRtree(sub).query(sub, predicate="intersects")
    keep = left < right
    left, right = left[keep], right[keep]
    if len(left):
        inner = shapely.relate_pattern(sub[left], sub[right], "T********")
        left, right = left[inner], right[inner]
    if not len(left):
        return none
    area = _intersection_areas(sub[left], sub[right])
    smaller = np.minimum(shapely.area(sub[left]), shapely.area(sub[right]))
    keep = (area >= OVERLAP_MIN_M2) & (area >= OVERLAP_MIN_SHARE * smaller)
    return left[keep].astype(np.int64), right[keep].astype(np.int64), area[keep]


#: Pairwise overlaps are kept only between holdings of at least this many acres: a smaller holding never
#: joins an assemblage (`services/site_finder/parcels.py` ASSEMBLY_MIN_ACRES).
OVERLAP_MIN_ACRES = 1.0
#: At most this many overlapping partners are listed per holding, the largest overlaps first; the rest
#: of its overlap is one figure (`overlap_rest_acres`) the site finder subtracts whatever joins, which
#: keeps an assemblage a lower bound (a condominium outline can carry hundreds of owners).
MAX_OVERLAP_PARTNERS = 32


@dataclass
class Overlaps:
    """Land two holdings both cover, holding by holding (indexes into the county's holdings)."""

    #: Acres of each holding that another holding's outline also covers (outlines counted once each).
    covered: Any
    #: Per holding with any: its partners and the acres each pair shares.
    partners: dict[int, dict[int, float]]
    #: Holdings whose own outlines overlap one another (their acres are measured as a union).
    union_needed: set[int]


def holding_overlaps(
    n_holdings: int,
    pair_holding: Any,
    pair_outline: Any,
    outline_acres: Any,
    overlap_i: Any,
    overlap_j: Any,
    overlap_acres: Any,
) -> tuple[Overlaps, Any]:
    """Which holdings share land: every pair of holdings drawn on one outline shares that outline's acres
    (two owners on one tract: undivided interests, a mobile home's owner and the landowner), and two
    distinct overlapping outlines share their intersection. `pair_holding` and `pair_outline` list each
    distinct (holding, outline) pair once. Returns the overlaps and how many holdings hold each outline."""
    covered = np.zeros(n_holdings)
    partners: dict[int, dict[int, float]] = {}
    union_needed: set[int] = set()
    n_out = len(outline_acres)
    holders_of_outline = np.bincount(pair_outline, minlength=n_out) if n_out else np.zeros(0, dtype=np.int64)
    holders: dict[int, list[int]] = {}
    involved = holders_of_outline >= 2
    if len(overlap_i):
        involved[overlap_i] = True
        involved[overlap_j] = True
    sel = np.nonzero(involved[pair_outline])[0] if n_out else np.zeros(0, dtype=np.int64)
    for o, h in zip(pair_outline[sel].tolist(), pair_holding[sel].tolist(), strict=True):
        holders.setdefault(o, []).append(h)
    for o, hs in holders.items():
        if len(hs) < 2:
            continue
        area = float(outline_acres[o])
        for h in hs:
            covered[h] += area
            listed = partners.setdefault(h, {})
            for other in hs[: MAX_OVERLAP_PARTNERS + 1]:
                if other != h:
                    listed[other] = listed.get(other, 0.0) + area
    for a, b, area in zip(
        overlap_i.tolist(), overlap_j.tolist(), np.asarray(overlap_acres).tolist(), strict=True
    ):
        for ha in holders.get(a, ()):
            for hb in holders.get(b, ()):
                if ha == hb:
                    union_needed.add(ha)
                    continue
                covered[ha] += area
                covered[hb] += area
                for one, other in ((ha, hb), (hb, ha)):
                    listed = partners.setdefault(one, {})
                    listed[other] = listed.get(other, 0.0) + area
    return Overlaps(covered, partners, union_needed), holders_of_outline


def _overlap_lists(
    overlaps: Overlaps,
    holders_of_outline: Any,
    pair_holding: Any,
    pair_outline: Any,
    outline_acres: Any,
    holding_acres: Any,
    ids: Sequence[str],
) -> tuple[list[list[dict[str, Any]] | None], Any]:
    """Each holding's listed partners (`MAX_OVERLAP_PARTNERS`, largest first) and the rest of its shared
    acres, between holdings of `OVERLAP_MIN_ACRES` or more."""
    n_holdings = len(holding_acres)
    rest = np.zeros(n_holdings)
    lists: list[list[dict[str, Any]] | None] = [None] * n_holdings
    # Holders past the listing cap of a crowded outline (a condominium) are not in `partners`: their
    # share of that outline goes to `rest` whole.
    crowded = holders_of_outline[pair_outline] > MAX_OVERLAP_PARTNERS + 1
    for h, o in zip(pair_holding[crowded].tolist(), pair_outline[crowded].tolist(), strict=True):
        rest[h] += float(outline_acres[o]) * (int(holders_of_outline[o]) - 1 - MAX_OVERLAP_PARTNERS)
    for h, found in overlaps.partners.items():
        if holding_acres[h] < OVERLAP_MIN_ACRES:
            rest[h] = 0.0
            continue
        items = sorted(
            ((o, a) for o, a in found.items() if holding_acres[o] >= OVERLAP_MIN_ACRES),
            key=lambda t: (-t[1], ids[t[0]]),
        )
        rest[h] += sum(a for _, a in items[MAX_OVERLAP_PARTNERS:])
        kept = items[:MAX_OVERLAP_PARTNERS]
        if kept:
            lists[h] = [{"holding_id": ids[o], "acres": round(float(a), 3)} for o, a in kept]
    return lists, np.minimum(rest, holding_acres)


def build_county(
    frame: CountyFrame,
    out_dir: pathlib.Path,
    *,
    rng: Callable[[int], str] | None = None,
    projection: Albers = TEXAS_ALBERS,
) -> dict[str, Any]:
    """Group, measure and write one county (module docstring). Returns its summary, which names no owner.

    A holding's acres count each outline once (accounts stacked on one outline are one tract) and, where
    its outlines overlap, are the area of their union. A holding whose land another holding's outline also
    covers (two owners on one outline; a smaller account drawn inside a larger one) lists that land pair by
    pair (`holding_overlaps`, the rest as `holding_overlap_rest_acres`), which the site finder subtracts
    when the second of the two joins an assemblage; the holdings file's `overlap_acres` is its total."""
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    t0 = time.monotonic()
    fips = frame.fips
    if not FIPS_RE.match(fips):
        raise ParcelsError(f"not a Texas county FIPS code: {fips!r}")
    read = len(frame)
    geoms, keep = _clean_geometry(frame.geometry)
    idx = np.nonzero(keep)[0]
    dropped = int(read - len(idx))
    lonlat = geoms[idx]
    n = len(idx)

    # Owners -> integer codes, then the strings go: nothing below this block sees an owner value. The
    # screen's classes and the suspect flag are kept as counts only.
    keys: dict[str, int] = {}
    codes = np.full(n, -1, dtype=np.int64)
    screen: Counter[str] = Counter()
    suspect = np.zeros(n, dtype=bool)
    suspect_codes: set[int] = set()
    for out_i, src_i in enumerate(idx.tolist()):
        key, cls = owner_screen(frame.owner[src_i])
        screen[cls] += 1
        if key is None:
            continue
        code = keys.get(key)
        if code is None:
            code = keys[key] = len(keys)
            if owner_suspect(key):
                suspect_codes.add(code)
        codes[out_i] = code
        suspect[out_i] = code in suspect_codes
    distinct_owners = len(keys)
    keys.clear()
    frame.owner = []

    projected = shapely.transform(lonlat, projection.transform)
    acres = shapely.area(projected) / ACRE_M2
    outline = outline_ids(lonlat)
    n_out = int(outline.max()) + 1 if n else 0
    reps = np.unique(outline, return_index=True)[1] if n else np.zeros(0, dtype=np.int64)
    t_read = time.monotonic()
    holding = group_holdings(codes, projected)
    t_group = time.monotonic()
    o_i, o_j, o_m2 = overlapping_outlines(projected, reps)
    t_overlap = time.monotonic()
    n_holdings = int(holding.max()) + 1 if n else 0
    ids = _holding_ids(n_holdings, rng)

    def pick(values: Sequence[Any]) -> list[Any]:
        return [values[i] for i in idx.tolist()]

    prop_id = [None if v in (None, "") else str(v).strip() for v in pick(frame.prop_id)]
    stated = [gis_acres(v, u) for v, u in zip(pick(frame.gis_area), pick(frame.gis_area_unit), strict=True)]
    codes_lu = [None if not v else str(v).strip()[:5] for v in pick(frame.land_use_code)]
    categories = [land_use_category(c) for c in codes_lu]
    priority_of = {c: i for i, c in enumerate(USE_PRIORITY)}
    prio = np.fromiter((priority_of[c] for c in categories), dtype=np.int64, count=n)
    land_value = _int_or_none(pick(frame.land_value))
    market_value = _int_or_none(pick(frame.market_value))
    tax_year = [None if not v else str(v).strip()[:4] for v in pick(frame.tax_year)]
    txgio_id = [int(v) for v in pick(frame.txgio_id)]

    # Distinct (holding, outline) pairs: each outline counts once in its holding whatever its accounts,
    # represented by its best-placed account (`USE_PRIORITY`: the land account before the mobile home).
    pair = holding * max(n_out, 1) + outline
    by_pair = np.lexsort((prio, pair))
    starts_pair = np.ones(n, dtype=bool)
    if n:
        starts_pair[1:] = pair[by_pair][1:] != pair[by_pair][:-1]
    rep_rows = by_pair[starts_pair]
    pair_index = np.empty(n, dtype=np.int64)
    pair_index[by_pair] = np.cumsum(starts_pair) - 1
    n_pairs = len(rep_rows)
    p_hold, p_out, p_acres, p_prio = holding[rep_rows], outline[rep_rows], acres[rep_rows], prio[rep_rows]
    parcels_per = np.bincount(p_hold, minlength=n_holdings).astype(np.int64)
    accounts_per = np.bincount(holding, minlength=n_holdings).astype(np.int64)
    sum_once = np.bincount(p_hold, weights=p_acres, minlength=n_holdings)

    # Land another holding also covers: an outline two holdings share (two owners on one outline), and
    # distinct outlines of two holdings that overlap. Overlapping outlines within one holding are
    # measured as their union below.
    o_acres = o_m2 / ACRE_M2
    outline_acres = acres[reps] if n else np.zeros(0)
    overlaps, holders_of_outline = holding_overlaps(
        n_holdings, p_hold, p_out, outline_acres, o_i, o_j, o_acres
    )
    union_needed = overlaps.union_needed

    # Acres: the distinct outlines' sum, or their union where they overlap; and each large holding's mean
    # width (twice its area over its perimeter), which finds right-of-way strips.
    holding_acres = sum_once.copy()
    mean_width = np.full(n_holdings, np.nan)
    order_p = np.argsort(p_hold, kind="stable")
    sorted_hold = p_hold[order_p]
    first_p = np.searchsorted(sorted_hold, np.arange(n_holdings))
    end_p = np.searchsorted(sorted_hold, np.arange(n_holdings), side="right")
    large = np.nonzero(sum_once >= STRIP_MIN_ACRES)[0]
    for h in sorted(union_needed | set(large[parcels_per[large] >= 2].tolist())):
        shape = _union(projected[rep_rows[order_p[first_p[h] : end_p[h]]]])
        area_m2 = float(shapely.area(shape))
        if h in union_needed:
            holding_acres[h] = area_m2 / ACRE_M2
        perimeter = float(shapely.length(shape))
        if perimeter > 0:
            mean_width[h] = 2 * area_m2 / perimeter
    singles = large[parcels_per[large] == 1]
    if len(singles):
        perimeter_1 = shapely.length(projected[rep_rows[order_p[first_p[singles]]]])
        mean_width[singles] = np.where(
            perimeter_1 > 0,
            2 * sum_once[singles] * ACRE_M2 / np.where(perimeter_1 > 0, perimeter_1, 1),
            np.nan,
        )
    overlap = np.minimum(overlaps.covered, holding_acres)
    partner_lists, overlap_rest = _overlap_lists(
        overlaps, holders_of_outline, p_hold, p_out, outline_acres, holding_acres, ids
    )

    # Land use by acreage over the distinct outlines, equal acreages by `USE_PRIORITY`.
    holding_use = ["not_stated"] * n_holdings
    named_use = p_prio != priority_of["not_stated"]
    width = len(USE_PRIORITY)
    combined = p_hold[named_use] * width + p_prio[named_use]
    if len(combined):
        uniq, inverse = np.unique(combined, return_inverse=True)
        sums = np.bincount(inverse, weights=p_acres[named_use])
        u_hold, u_use = uniq // width, uniq % width
        best = np.lexsort((u_use, -sums, u_hold))
        lead = np.ones(len(best), dtype=bool)
        lead[1:] = u_hold[best][1:] != u_hold[best][:-1]
        for h, c in zip(u_hold[best][lead].tolist(), u_use[best][lead].tolist(), strict=True):
            holding_use[h] = USE_PRIORITY[c]
    # The appraisal-record link: the holding's largest outline, by its best-placed account.
    order = np.lexsort((p_prio, -p_acres, p_hold))
    lead = np.ones(len(order), dtype=bool)
    lead[1:] = p_hold[order][1:] != p_hold[order][:-1]
    largest = np.empty(n_holdings, dtype=np.int64)
    largest[p_hold[order][lead]] = rep_rows[order][lead]

    # Values: every account's (land and improvements are separate accounts); value per acre over the
    # outlines that carry one, each once, scaled to the union where outlines overlap.
    mv = np.array([v if v is not None else np.nan for v in market_value], dtype=np.float64)
    lv = np.array([v if v is not None else np.nan for v in land_value], dtype=np.float64)
    has_mv = np.isfinite(mv) & (mv > 0)
    mv_sum = np.bincount(holding, weights=np.where(has_mv, mv, 0.0), minlength=n_holdings)
    mv_any = np.bincount(holding, weights=has_mv.astype(np.float64), minlength=n_holdings) > 0
    pair_has_mv = np.bincount(pair_index, weights=has_mv.astype(np.float64), minlength=n_pairs) > 0
    ratio = np.divide(holding_acres, sum_once, out=np.ones(n_holdings), where=sum_once > 0)
    mv_acres = np.bincount(p_hold, weights=np.where(pair_has_mv, p_acres, 0.0), minlength=n_holdings) * ratio
    has_lv = np.isfinite(lv)
    lv_sum = np.bincount(holding, weights=np.where(has_lv, lv, 0.0), minlength=n_holdings)
    lv_any = np.bincount(holding, weights=has_lv.astype(np.float64), minlength=n_holdings) > 0
    st = np.array([v if v is not None else np.nan for v in stated], dtype=np.float64)[rep_rows]
    st_all = (
        np.bincount(p_hold, weights=np.isfinite(st).astype(np.float64), minlength=n_holdings) == parcels_per
    )
    st_sum = np.bincount(p_hold, weights=np.where(np.isfinite(st), st, 0.0), minlength=n_holdings)
    years: list[Counter[str]] = [Counter() for _ in range(n_holdings)]
    for h, y in zip(holding.tolist(), tax_year, strict=True):
        if y:
            years[h][y] += 1
    vpa = [
        round(float(mv_sum[h] / mv_acres[h])) if mv_any[h] and mv_acres[h] > 0 else None
        for h in range(n_holdings)
    ]
    kind = [
        "exempt"
        if holding_use[h] == "exempt"
        else "utility"
        if holding_use[h] == "utilities"
        else "strip"
        if holding_acres[h] >= STRIP_MIN_ACRES and mean_width[h] < STRIP_WIDTH_M
        else "land"
        for h in range(n_holdings)
    ]

    # Coordinates rounded point by point (GEOS's topology-preserving mode took most of a county's build),
    # then the few outlines the rounding made invalid are repaired.
    display = shapely.set_precision(
        shapely.simplify(lonlat, SIMPLIFY_DEG, preserve_topology=True), PRECISION_DEG, mode="pointwise"
    )
    invalid = ~shapely.is_valid(display)
    if invalid.any():
        display[invalid] = shapely.make_valid(display[invalid])
    bounds = shapely.bounds(display)
    sort = _zorder(bounds) if len(bounds) else np.zeros(0, dtype=np.int64)
    prov = Provenance(frame.source_url, frame.retrieved_at).columns()
    # Column by column into Arrow, then one `take` into Z-order: no second copy of any Python list.
    per_holding = {
        "holding_id": pa.array(ids, pa.string()),
        "holding_acres": pa.array(np.round(holding_acres, 2), pa.float64()),
        "holding_parcels": pa.array(parcels_per, pa.int32()),
        "holding_land_use": pa.array(holding_use, pa.string()),
        "holding_value_per_acre": pa.array(vpa, pa.int64()),
        "holding_link_prop_id": pa.array([prop_id[int(i)] for i in largest.tolist()], pa.string()),
        "holding_kind": pa.array(kind, pa.string()),
        "holding_overlaps": pa.array(partner_lists, pa.list_(OVERLAP_STRUCT)),
        "holding_overlap_rest_acres": pa.array(np.round(overlap_rest, 3), pa.float64()),
    }
    holding_index = pa.array(holding, pa.int64())
    arrays: dict[str, Any] = {
        "parcel_key": pa.array([f"{fips}-{t}" for t in txgio_id], pa.string()),
        "txgio_id": pa.array(txgio_id, pa.int64()),
        "fips": pa.array([fips] * n, pa.string()),
        "prop_id": pa.array(prop_id, pa.string()),
        "acres": pa.array(np.round(acres, 3), pa.float64()),
        "gis_acres": pa.array([round(v, 3) if v is not None else None for v in stated], pa.float64()),
        "land_use_code": pa.array(codes_lu, pa.string()),
        "land_use": pa.array(categories, pa.string()),
        "land_value": pa.array(land_value, pa.int64()),
        "market_value": pa.array(market_value, pa.int64()),
        "tax_year": pa.array(tax_year, pa.string()),
        **{name: values.take(holding_index) for name, values in per_holding.items()},
    }
    fields = [pa.field(name, typ) for name, typ in PARCEL_COLUMNS]
    fields += [pa.field("geometry", pa.binary())]
    fields += [pa.field("bbox", pa.struct([pa.field(col, pa.float64()) for col in BBOX_FIELDS]))]
    fields += [pa.field(col, pa.string()) for col in PROVENANCE_COLUMNS]
    extra = {"source_id": SOURCE_ID, "fips": fips, "layout": LAYOUT_VERSION, "privacy": PRIVACY_STATEMENT}
    bbox_array = pa.StructArray.from_arrays(
        [pa.array(bounds[:, k] if n else [], pa.float64()) for k in range(4)], names=list(BBOX_FIELDS)
    )
    columns = [arrays[name] for name, _ in PARCEL_COLUMNS]
    columns += [pa.array(shapely.to_wkb(display, output_dimension=2).tolist(), pa.binary()), bbox_array]
    columns += [pa.array([prov[col]] * n, pa.string()) for col in PROVENANCE_COLUMNS]
    arrays.clear()
    parcels_table = pa.Table.from_arrays(
        columns, schema=pa.schema(fields, metadata=_geo_metadata(bounds, extra))
    ).take(pa.array(sort, pa.int64()))
    del columns
    parcels_file = _write(out_dir / "parcels" / f"{fips}.parquet", parcels_table)
    del parcels_table

    # Holdings: one row each, largest first; its box and the largest parcel's representative point.
    h_bounds = np.full((n_holdings, 4), np.nan)
    if len(bounds):
        h_bounds[:, 0] = np.full(n_holdings, np.inf)
        h_bounds[:, 1] = np.full(n_holdings, np.inf)
        h_bounds[:, 2] = np.full(n_holdings, -np.inf)
        h_bounds[:, 3] = np.full(n_holdings, -np.inf)
        np.minimum.at(h_bounds[:, 0], holding, bounds[:, 0])
        np.minimum.at(h_bounds[:, 1], holding, bounds[:, 1])
        np.maximum.at(h_bounds[:, 2], holding, bounds[:, 2])
        np.maximum.at(h_bounds[:, 3], holding, bounds[:, 3])
    points = shapely.point_on_surface(lonlat[largest]) if n_holdings else np.zeros(0, dtype=object)
    px = shapely.get_x(points) if n_holdings else np.zeros(0)
    py = shapely.get_y(points) if n_holdings else np.zeros(0)
    h_order = np.argsort(-holding_acres, kind="stable")
    hdata: dict[str, list[Any]] = {
        "holding_id": [ids[h] for h in h_order],
        "fips": [fips] * n_holdings,
        "parcels": [int(parcels_per[h]) for h in h_order],
        "accounts": [int(accounts_per[h]) for h in h_order],
        "owner_count": [1] * n_holdings,
        "acres": [round(float(holding_acres[h]), 2) for h in h_order],
        "gis_acres": [round(float(st_sum[h]), 2) if st_all[h] else None for h in h_order],
        "overlap_acres": [round(float(overlap[h]), 2) for h in h_order],
        "overlap_partners": [len(overlaps.partners.get(int(h), {})) for h in h_order],
        "kind": [kind[h] for h in h_order],
        "mean_width_m": [
            round(float(mean_width[h]), 1) if np.isfinite(mean_width[h]) else None for h in h_order
        ],
        "land_use": [holding_use[h] for h in h_order],
        "land_value": [round(float(lv_sum[h])) if lv_any[h] else None for h in h_order],
        "market_value": [round(float(mv_sum[h])) if mv_any[h] else None for h in h_order],
        "value_per_acre": [vpa[h] for h in h_order],
        "tax_year": [years[h].most_common(1)[0][0] if years[h] else None for h in h_order],
        "link_prop_id": [prop_id[int(largest[h])] for h in h_order],
        "lon": [round(float(px[h]), 6) for h in h_order],
        "lat": [round(float(py[h]), 6) for h in h_order],
        **{
            name: [round(float(h_bounds[h, k]), 6) for h in h_order]
            for k, name in enumerate(("xmin", "ymin", "xmax", "ymax"))
        },
        **{col: [prov[col]] * n_holdings for col in PROVENANCE_COLUMNS},
    }
    h_fields = [pa.field(name, typ) for name, typ in HOLDING_COLUMNS] + [
        pa.field(col, pa.string()) for col in PROVENANCE_COLUMNS
    ]
    holdings_table = pa.Table.from_pydict(
        hdata,
        schema=pa.schema(h_fields, metadata={b"inframation": json.dumps(extra, sort_keys=True).encode()}),
    )
    holdings_file = _write(out_dir / "holdings" / f"{fips}.parquet", holdings_table)
    t_write = time.monotonic()

    multi = parcels_per >= 2
    land = np.array([k == "land" for k in kind], dtype=bool)

    def share(count: int) -> float | None:
        return round(count / n, 4) if n else None

    smaller = np.minimum(acres[reps[o_i]], acres[reps[o_j]]) if len(o_i) else np.zeros(0)
    suspect_holdings = np.bincount(holding, weights=suspect.astype(np.float64), minlength=n_holdings) > 0
    summary: dict[str, Any] = {
        "fips": fips,
        "name": frame.name,
        "status": "ok",
        "parcels_read": read,
        "parcels": n,
        "dropped_geometry": dropped,
        "holdings": n_holdings,
        "multi_parcel_holdings": int(multi.sum()),
        "parcels_in_multi_parcel_holdings": int(parcels_per[multi].sum()),
        "largest_holding_acres": round(float(holding_acres.max()), 1) if n_holdings else None,
        "largest_holding_parcels": int(parcels_per[int(np.argmax(holding_acres))]) if n_holdings else None,
        "largest_land_holding_acres": round(float(holding_acres[land].max()), 1) if land.any() else None,
        "holdings_at_least_acres": {
            str(t): int((holding_acres >= t).sum()) for t in (20, 50, 100, 200, 500, 1000)
        },
        "holding_kinds": {k: int(sum(1 for x in kind if x == k)) for k in HOLDING_KINDS},
        # Each outline once: accounts stacked on one outline are one tract.
        "acres": round(float(acres[reps].sum()), 1) if n else 0.0,
        # Stacked and overlapping outlines (counts and acres only), and how the holdings were measured.
        "outlines": {
            "distinct": n_out,
            "accounts_on_a_repeated_outline": int(n - n_out),
            "repeats_within_a_holding": int(n - n_pairs),
            "outlines_shared_by_holdings": int((holders_of_outline >= 2).sum()),
            "overlapping_pairs": len(o_i),
            "overlapping_pairs_over_10_percent": int(
                (o_m2 / ACRE_M2 >= OVERLAP_REPORT_SHARE * smaller).sum()
            ),
            "overlap_acres": round(float(o_acres.sum()), 1),
            "holdings_measured_as_union": len(union_needed),
            "holdings_overlapping_another": int((overlap > 0).sum()),
        },
        # The owner screen (counts only; no string): kept names, blanks, withheld markers, placeholders,
        # and kept names that still look like a placeholder or a marker (`_SUSPECT`).
        "owners": {
            **{c: int(screen[c]) for c in OWNER_CLASSES},
            "suspect_kept": int(suspect.sum()),
            "suspect_in_multi_parcel_holdings": int((suspect_holdings & multi).sum()),
        },
        # Aggregate shares only: which parcels lack an owner is never written.
        "fill": {
            "owner": share(int(screen["named"])),
            "prop_id": share(sum(1 for v in prop_id if v)),
            "land_use": share(sum(1 for c in categories if c != "not_stated")),
            "market_value": share(int(has_mv.sum())),
            "land_value": share(int(has_lv.sum())),
            "gis_acres": share(sum(1 for v in stated if v is not None)),
            "tax_year": share(sum(1 for v in tax_year if v)),
        },
        "distinct_owner_keys": distinct_owners,
        "land_use_parcels": dict(Counter(categories).most_common()),
        "bbox": _box(bounds),
        "files": {
            "parcels": {"path": f"parcels/{fips}.parquet", **parcels_file},
            "holdings": {"path": f"holdings/{fips}.parquet", **holdings_file},
        },
        "source_url": frame.source_url,
        "retrieved_at": frame.retrieved_at,
        "seconds": {
            "measure": round(t_read - t0, 2),
            "group": round(t_group - t_read, 2),
            "overlaps": round(t_overlap - t_group, 2),
            "write": round(t_write - t_overlap, 2),
        },
        "group_distance_m": GROUP_DISTANCE_M,
    }
    return summary


PRIVACY_STATEMENT = (
    "No owner name, care-of name, mailing address or situs address is stored. Holdings group contiguous "
    "parcels of one owner at build time; holding ids are random per build."
)


# ----------------------------------------------------------------------------------- reading a zip
#: Logical field -> the names it has in the file geodatabase and in the shapefile (10-character names).
FIELD_NAMES: dict[str, tuple[str, ...]] = {
    "prop_id": ("PROP_ID",),
    "owner": ("OWNER_NAME", "OWNER_NAM", "OWNERNAME"),
    "gis_area": ("GIS_AREA",),
    "gis_area_unit": ("GIS_AREA_UNIT", "GIS_AREA_U", "GIS_AREA_UN", "GIS_AR_UNI"),
    "land_use_code": ("STAT_LAND_USE", "STAT_LAND_", "STAT_LAND", "STAT_LU"),
    "land_value": ("LAND_VALUE", "LAND_VAL"),
    "market_value": ("MKT_VALUE", "MKT_VAL"),
    "tax_year": ("TAX_YEAR",),
    "fips": ("FIPS",),
    "objectid": ("OBJECTID", "OBJECTID_1", "FID"),
}


def _pick_layer(zip_path: pathlib.Path) -> tuple[str, str | None]:
    """The dataset inside a county zip: the file geodatabase (full field names) where there is one,
    else the shapefile. Returns a GDAL path and a layer name."""
    import pyogrio  # type: ignore[import-untyped,import-not-found,unused-ignore]

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    gdbs = sorted({n.split(".gdb/")[0] + ".gdb" for n in names if ".gdb/" in n})
    shps = sorted(n for n in names if n.lower().endswith(".shp"))
    candidates: list[tuple[str, str | None]] = []
    for gdb in gdbs:
        path = f"/vsizip/{zip_path}/{gdb}"
        for layer, gtype in pyogrio.list_layers(path):
            if gtype and "polygon" in str(gtype).lower():
                candidates.append((path, str(layer)))
    for shp in shps:
        candidates.append((f"/vsizip/{zip_path}/{shp}", None))
    if not candidates:
        raise ParcelsError(f"{zip_path.name}: no file geodatabase polygon layer or shapefile inside")
    preferred = [c for c in candidates if "parcel" in (c[1] or c[0]).lower()]
    return (preferred or candidates)[0]


GEOGRAPHIC_CRS = frozenset({"EPSG:4326", "EPSG:4269", "OGC:CRS84", "CRS84", "EPSG:4152"})
WEB_MERCATOR_CRS = frozenset({"EPSG:3857", "EPSG:102100", "EPSG:900913", "ESRI:102100"})


def _to_lonlat(geoms: Any, crs: Any) -> Any:
    """Geometries in EPSG:4326. The 2026 catalogue states spatial reference 4326; any other CRS is
    reprojected with pyproj (installed by the Actions build), and Web Mercator without it."""
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    text = str(crs or "").upper().strip()
    projected = "PROJCS" in text or "PROJCRS" in text
    if not text or text in GEOGRAPHIC_CRS or (not projected and ("GEOGCS" in text or "GEOGCRS" in text)):
        return geoms
    if text in WEB_MERCATOR_CRS:
        r = 6_378_137.0

        def merc(c: Any) -> Any:
            lon = np.degrees(c[:, 0] / r)
            lat = np.degrees(2 * np.arctan(np.exp(c[:, 1] / r)) - math.pi / 2)
            return np.column_stack([lon, lat])

        return shapely.transform(geoms, merc)
    try:
        from pyproj import Transformer  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:
        raise ParcelsError(f"the county file is in {text[:60]}; reprojecting it needs pyproj") from exc
    tf = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    def project(c: Any) -> Any:
        x, y = tf.transform(c[:, 0], c[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geoms, project)


def read_county_zip(zip_path: pathlib.Path, fips: str, name: str = "") -> CountyFrame:
    """One TxGIO county zip -> a `CountyFrame`. Reads only the columns `FIELD_NAMES` lists: never the
    care-of, mailing, situs or legal-description columns."""
    import pyogrio  # type: ignore[import-untyped,import-not-found,unused-ignore]
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    path, layer = _pick_layer(zip_path)
    info = pyogrio.read_info(path, layer=layer)
    available = {str(f).upper(): str(f) for f in info["fields"]}
    resolved: dict[str, str] = {}
    for logical, names in FIELD_NAMES.items():
        for candidate in names:
            if candidate in available:
                resolved[logical] = available[candidate]
                break
    missing = [f for f in ("prop_id", "owner") if f not in resolved]
    if missing:
        raise ParcelsError(f"{zip_path.name}: the TxGIO columns {', '.join(missing)} are missing")
    columns = sorted(set(resolved.values()))
    meta, table = pyogrio.read_arrow(path, layer=layer, columns=columns, return_fids=True)
    geom_col = meta.get("geometry_name") or "wkb_geometry"
    if geom_col not in table.column_names:
        geom_col = next(n for n in table.column_names if n.lower() in ("wkb_geometry", "geometry", "shape"))
    geoms = shapely.from_wkb(np.asarray(table.column(geom_col).to_pylist(), dtype=object))
    geoms = _to_lonlat(geoms, meta.get("crs"))

    def col(logical: str) -> list[Any]:
        name_ = resolved.get(logical)
        if name_ is None or name_ not in table.column_names:
            return [None] * table.num_rows
        values: list[Any] = table.column(name_).to_pylist()
        return values

    def text(values: list[Any]) -> list[str | None]:
        return [None if v is None or str(v).strip() == "" else str(v).strip() for v in values]

    def number(values: list[Any]) -> list[float | None]:
        out: list[float | None] = []
        for v in values:
            try:
                f = float(v) if v is not None and str(v).strip() != "" else None
            except (TypeError, ValueError):
                f = None
            out.append(f if f is not None and math.isfinite(f) else None)
        return out

    # A geodatabase's OBJECTID is its feature id, not a field: GDAL returns it as the FID.
    fid_name = next((n for n in table.column_names if n.lower() in ("ogc_fid", "fid", "__fid__")), None)
    if "objectid" in resolved:
        ids = [int(v) if v is not None else i for i, v in enumerate(col("objectid"))]
    elif fid_name:
        ids = [int(v) for v in table.column(fid_name).to_pylist()]
    else:
        ids = list(range(table.num_rows))
    fips_values = {str(v).strip() for v in col("fips") if v}
    if fips_values and fips_values != {fips}:
        others = sorted(fips_values - {fips})
        if others:
            raise ParcelsError(f"{zip_path.name}: rows from other counties ({', '.join(others[:5])})")
    return CountyFrame(
        fips=fips,
        name=name,
        txgio_id=ids,
        prop_id=text(col("prop_id")),
        owner=col("owner"),
        gis_area=number(col("gis_area")),
        gis_area_unit=text(col("gis_area_unit")),
        land_use_code=text(col("land_use_code")),
        land_value=number(col("land_value")),
        market_value=number(col("market_value")),
        tax_year=text(col("tax_year")),
        geometry=geoms,
    )


def frame_from_features(features: Sequence[Mapping[str, Any]], fips: str, name: str = "") -> CountyFrame:
    """GeoJSON features in TxGIO's schema (field names as the service and the geodatabase spell them)
    -> a `CountyFrame`, without GDAL: the tests' synthetic county and a developer's GeoJSON export."""
    import shapely  # type: ignore[import-untyped,import-not-found,unused-ignore]

    def get(props: Mapping[str, Any], logical: str) -> Any:
        upper = {str(k).upper(): v for k, v in props.items()}
        for candidate in FIELD_NAMES[logical]:
            if candidate in upper:
                return upper[candidate]
        return None

    def num(v: Any) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    def txt(v: Any) -> str | None:
        return None if v is None or str(v).strip() == "" else str(v).strip()

    props = [dict(f.get("properties") or {}) for f in features]
    geoms = np.asarray(
        [shapely.geometry.shape(f["geometry"]) if f.get("geometry") else None for f in features], dtype=object
    )
    return CountyFrame(
        fips=fips,
        name=name,
        txgio_id=[
            int(get(p, "objectid") if get(p, "objectid") is not None else i) for i, p in enumerate(props)
        ],
        prop_id=[txt(get(p, "prop_id")) for p in props],
        owner=[get(p, "owner") for p in props],
        gis_area=[num(get(p, "gis_area")) for p in props],
        gis_area_unit=[txt(get(p, "gis_area_unit")) for p in props],
        land_use_code=[txt(get(p, "land_use_code")) for p in props],
        land_value=[num(get(p, "land_value")) for p in props],
        market_value=[num(get(p, "market_value")) for p in props],
        tax_year=[txt(get(p, "tax_year")) for p in props],
        geometry=geoms,
    )


# ----------------------------------------------------------------------------------- the catalogue
@dataclass(frozen=True)
class CountyFile:
    fips: str
    name: str
    url: str
    bytes: int | None


@dataclass
class Plan:
    collection_id: str
    name: str
    acquisition_date: str | None
    publication_date: str | None
    license_name: str | None
    license_abbreviation: str | None
    spatial_reference: str | None
    excluded_note: str | None
    counties: list[CountyFile] = field(default_factory=list)
    statewide: CountyFile | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "collection_id": self.collection_id,
            "collection_url": COLLECTION_PAGE.format(cid=self.collection_id),
            "name": self.name,
            "acquisition_date": self.acquisition_date,
            "publication_date": self.publication_date,
            "license_name": self.license_name,
            "license_abbreviation": self.license_abbreviation,
            "spatial_reference": self.spatial_reference,
            "excluded_note": self.excluded_note,
            "counties": [c.__dict__ for c in self.counties],
            "statewide": self.statewide.__dict__ if self.statewide else None,
        }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Plan:
        plan = cls(
            collection_id=str(doc["collection_id"]),
            name=str(doc.get("name") or ""),
            acquisition_date=doc.get("acquisition_date"),
            publication_date=doc.get("publication_date"),
            license_name=doc.get("license_name"),
            license_abbreviation=doc.get("license_abbreviation"),
            spatial_reference=doc.get("spatial_reference"),
            excluded_note=doc.get("excluded_note"),
        )
        plan.counties = [CountyFile(**c) for c in doc.get("counties") or []]
        if doc.get("statewide"):
            plan.statewide = CountyFile(**doc["statewide"])
        return plan

    def county(self, fips: str) -> CountyFile:
        for c in self.counties:
            if c.fips == fips:
                return c
        raise ParcelsError(f"county {fips} is not in the {self.publication_date} catalogue")


def latest_collection(collections: Mapping[str, Any]) -> dict[str, Any]:
    """The newest "Land Parcels" collection of a catalogue search (by publication, then acquisition date)."""
    rows = [
        r
        for r in collections.get("results") or []
        if str(r.get("name") or "").strip().lower() == "land parcels" and r.get("public", True)
    ]
    if not rows:
        raise ParcelsError("the TxGIO catalogue lists no public Land Parcels collection")
    return max(
        rows, key=lambda r: (str(r.get("publication_date") or ""), str(r.get("acquisition_date") or ""))
    )


def parse_plan(collections: Mapping[str, Any], resources: Mapping[str, Any]) -> Plan:
    """The newest vintage and its county zips, from the catalogue's two answers."""
    coll = latest_collection(collections)
    cid = str(coll["collection_id"])
    desc = str(coll.get("description") or "")
    excluded = re.search(r"excludes the following count(?:y|ies):\s*([^\n<.]+)", desc, re.I)
    plan = Plan(
        collection_id=cid,
        name=str(coll.get("name") or "Land Parcels"),
        acquisition_date=coll.get("acquisition_date"),
        publication_date=coll.get("publication_date"),
        license_name=coll.get("license_name"),
        license_abbreviation=coll.get("license_abbreviation"),
        spatial_reference=str(coll.get("spatial_reference")) if coll.get("spatial_reference") else None,
        excluded_note=excluded.group(1).strip() if excluded else None,
    )
    for r in resources.get("results") or []:
        if str(r.get("collection_id") or cid) != cid:
            continue
        url = str(r.get("resource") or "")
        size = r.get("filesize")
        m = COUNTY_ZIP_RE.search(url)
        entry_bytes = int(size) if isinstance(size, (int, float)) else None
        if m and str(r.get("area_type") or "county") == "county":
            plan.counties.append(CountyFile(m.group(1), str(r.get("area_type_name") or ""), url, entry_bytes))
        elif url.lower().endswith(".zip") and "_48_" in url:
            plan.statewide = CountyFile("48", "Texas", url, entry_bytes)
    plan.counties.sort(key=lambda c: c.fips)
    if not plan.counties:
        raise ParcelsError(f"collection {cid} lists no county zips")
    return plan


def fetch_plan(session: Any) -> Plan:
    collections = session.get(COLLECTIONS_URL, timeout=60).json()
    cid = latest_collection(collections)["collection_id"]
    resources = session.get(RESOURCES_URL.format(cid=cid), timeout=60).json()
    if resources.get("next"):
        raise ParcelsError("the resource list has a second page; raise the limit")
    return parse_plan(collections, resources)


def select_counties(plan: Plan, wanted: str) -> list[CountyFile]:
    """`all`, or a comma list of FIPS codes or county names."""
    text = (wanted or "all").strip()
    if text.lower() == "all":
        return list(plan.counties)
    by_name = {c.name.lower(): c for c in plan.counties}
    out: list[CountyFile] = []
    for item in (t.strip() for t in text.split(",")):
        if not item:
            continue
        if FIPS_RE.match(item):
            out.append(plan.county(item))
        elif item.lower() in by_name:
            out.append(by_name[item.lower()])
        else:
            raise ParcelsError(f"not a county of this vintage: {item!r}")
    return out


def check_files(session: Any, counties: Sequence[CountyFile]) -> list[dict[str, Any]]:
    """The dry run: one HEAD per county zip (its status and size against the catalogue's); nothing is
    downloaded."""
    out = []
    for c in counties:
        row: dict[str, Any] = {"fips": c.fips, "name": c.name, "url": c.url, "catalogue_bytes": c.bytes}
        try:
            resp = session.request("HEAD", c.url, timeout=60, allow_redirects=True)
            row["status"] = resp.status_code
            length = resp.headers.get("Content-Length")
            row["bytes"] = int(length) if length and length.isdigit() else None
            row["ok"] = resp.status_code == 200 and (c.bytes is None or row["bytes"] in (None, c.bytes))
            if resp.status_code != 200:
                row["reason"] = (
                    f"HTTP {resp.status_code} ({resp.headers.get('Server') or 'no server header'})"
                )
        except Exception as exc:
            row.update(ok=False, reason=type(exc).__name__ + ": " + str(exc)[:200])
        out.append(row)
    return out


def download(session: Any, county: CountyFile, work_dir: pathlib.Path) -> pathlib.Path:
    """Stream one county zip to `work_dir`, checked against the catalogue's size and as a zip."""
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / f"{county.fips}.zip"
    resp = session.get(county.url, stream=True, timeout=600)
    try:
        if resp.status_code != 200:
            raise ParcelsError(f"{county.url} answered HTTP {resp.status_code}")
        with dest.open("wb") as fh:
            for block in resp.iter_content(chunk_size=1 << 20):
                fh.write(block)
    finally:
        resp.close()
    size = dest.stat().st_size
    if county.bytes is not None and size != county.bytes:
        dest.unlink(missing_ok=True)
        raise ParcelsError(f"{county.fips}: downloaded {size} bytes, the catalogue says {county.bytes}")
    if not zipfile.is_zipfile(dest):
        dest.unlink(missing_ok=True)
        raise ParcelsError(f"{county.fips}: the download is not a zip")
    return dest


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _peak_rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def run_county(
    plan: Plan,
    fips: str,
    out_dir: pathlib.Path,
    work_dir: pathlib.Path,
    *,
    session: Any = None,
    zip_path: pathlib.Path | None = None,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    """One county end to end; writes `counties/<fips>.json` whether it succeeds or fails, and deletes the
    downloaded zip either way. A failure's reason is the exception's type and message, which never
    carry an owner value (only the counts and the field names above appear in messages)."""
    t0 = time.monotonic()
    county = (
        plan.county(fips) if any(c.fips == fips for c in plan.counties) else CountyFile(fips, "", "", None)
    )
    summary: dict[str, Any]
    downloaded: pathlib.Path | None = None
    try:
        if zip_path is None:
            if session is None:
                from pipeline.connectors.http import PoliteSession

                session = PoliteSession(rate_limits=RATE_LIMITS, timeout=600)
            downloaded = download(session, county, work_dir)
            source = downloaded
            stamp = retrieved_at or _now()
        else:
            source = zip_path
            stamp = retrieved_at or dt.datetime.fromtimestamp(zip_path.stat().st_mtime, dt.UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        t_dl = time.monotonic()
        frame = read_county_zip(source, fips, county.name)
        frame.source_url = county.url or COLLECTION_PAGE.format(cid=plan.collection_id)
        frame.retrieved_at = stamp
        t_read = time.monotonic()
        summary = build_county(frame, out_dir)
        del frame
        summary["seconds"] = {
            "download": round(t_dl - t0, 2),
            "read": round(t_read - t_dl, 2),
            **summary["seconds"],
            "total": round(time.monotonic() - t0, 2),
        }
        summary["zip_bytes"] = source.stat().st_size
    except Exception as exc:
        summary = {
            "fips": fips,
            "name": county.name,
            "status": "failed",
            "reason": f"{type(exc).__name__}: {str(exc)[:300]}",
            "source_url": county.url,
            "seconds": {"total": round(time.monotonic() - t0, 2)},
        }
    finally:
        if downloaded is not None:
            downloaded.unlink(missing_ok=True)
    summary["peak_rss_mb"] = _peak_rss_mb()
    summary["vintage"] = {
        "collection_id": plan.collection_id,
        "acquisition_date": plan.acquisition_date,
        "publication_date": plan.publication_date,
    }
    path = out_dir / "counties" / f"{fips}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=1, sort_keys=True), encoding="utf-8")
    return summary


# ------------------------------------------------------------------------------------- the manifest
def version_key(plan: Plan, built_at: dt.datetime | None = None) -> str:
    stamp = (built_at or dt.datetime.now(dt.UTC)).strftime("%Y%m%dT%H%MZ")
    vintage = str(plan.publication_date or "unknown").replace("-", "")
    return f"{LAYOUT_VERSION}/{vintage}-{stamp}"


def build_manifest(
    out_dir: pathlib.Path,
    plan: Plan,
    version: str,
    *,
    previous: Mapping[str, Any] | None = None,
    partial: bool = False,
    expected: Sequence[str] = (),
    allow_dropped: Sequence[str] = (),
    max_drop: float = MAX_DROP,
) -> tuple[dict[str, Any], list[str]]:
    """The counties built under `out_dir/counties/` -> the manifest, and the problems that refuse its
    promotion to `latest.json`. A county this build did not build (a partial build) or failed to build
    keeps its published entry, under that entry's own version; a failure is still listed."""
    if not VERSION_RE.match(version):
        raise ParcelsError(f"bad version key {version!r}")
    built: dict[str, dict[str, Any]] = {}
    for path in sorted((out_dir / "counties").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        built[str(doc["fips"])] = doc
    prev_counties: dict[str, Any] = dict((previous or {}).get("counties") or {})
    counties: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    problems: list[str] = []
    for fips, doc in sorted(built.items()):
        if doc.get("status") == "ok":
            counties[fips] = {**doc, "version": version}
        else:
            failures.append({"fips": fips, "name": doc.get("name"), "reason": doc.get("reason")})
            if fips in prev_counties:
                counties[fips] = {**prev_counties[fips], "carried_forward": True}
    for fips in expected:
        if fips not in built:
            failures.append(
                {"fips": fips, "name": None, "reason": "no summary: the county job did not finish"}
            )
            if fips in prev_counties:
                counties[fips] = {**prev_counties[fips], "carried_forward": True}
    for fips, entry in prev_counties.items():
        if fips in counties:
            continue
        if partial or fips in allow_dropped:
            if partial:
                counties[fips] = {**entry, "carried_forward": True}
            continue
        problems.append(
            f"{fips} ({entry.get('name')}) is published but was not built; allow it to drop explicitly"
        )
    for fips, entry in counties.items():
        old = prev_counties.get(fips)
        if not old or entry.get("carried_forward"):
            continue
        before, after = int(old.get("parcels") or 0), int(entry.get("parcels") or 0)
        if before and after < before * (1 - max_drop):
            problems.append(f"{fips} ({entry.get('name')}): parcels fell from {before} to {after}")
    if not counties:
        problems.append("no county was built")
    manifest = {
        "source_id": SOURCE_ID,
        "layout": LAYOUT_VERSION,
        "version": version,
        "built_at": _now(),
        "vintage": plan.as_dict() | {"counties": len(plan.counties)},
        "counties": counties,
        "failures": failures,
        "totals": {
            "counties": len(counties),
            "parcels": sum(int(c.get("parcels") or 0) for c in counties.values()),
            "holdings": sum(int(c.get("holdings") or 0) for c in counties.values()),
        },
        "group_distance_m": GROUP_DISTANCE_M,
        "privacy": PRIVACY_STATEMENT,
        "attribution": ATTRIBUTION,
        "notice": NOTICE,
        "licence": LICENCE,
        "service_note": f"{SERVICE_URL}: query not supported (2026-10-23); not used",
    }
    for entry in counties.values():
        entry.pop("vintage", None)
    return manifest, problems


def report_markdown(manifest: Mapping[str, Any], checks: Sequence[Mapping[str, Any]] = ()) -> str:
    """A per-county table for the workflow's step summary: what was built, and each failure's reason."""
    lines = [f"### TxGIO land parcels {manifest.get('version') or '(dry run)'}", ""]
    totals = manifest.get("totals") or {}
    if totals:
        lines.append(
            f"{totals.get('counties', 0)} counties, {totals.get('parcels', 0):,} parcels, "
            f"{totals.get('holdings', 0):,} holdings."
        )
        lines.append("")
    failures = list(manifest.get("failures") or [])
    if checks:
        bad = [c for c in checks if not c.get("ok")]
        lines += [
            f"Dry run: {len(checks) - len(bad)} of {len(checks)} county files answer as catalogued.",
            "",
        ]
        failures += [
            {"fips": c["fips"], "name": c.get("name"), "reason": c.get("reason") or "size differs"}
            for c in bad
        ]
    if failures:
        lines += ["| County | FIPS | Failure |", "|---|---|---|"]
        lines += [f"| {f.get('name') or ''} | {f['fips']} | {f.get('reason') or ''} |" for f in failures]
        lines.append("")
    counties = manifest.get("counties") or {}
    if counties:
        lines += [
            "| County | FIPS | Parcels | Holdings | Largest holding (acres) | Seconds | Peak MB |",
            "|---|---|---|---|---|---|---|",
        ]
        for fips, c in sorted(counties.items()):
            secs = (c.get("seconds") or {}).get("total")
            note = " (carried forward)" if c.get("carried_forward") else ""
            cells = [
                f"{c.get('name') or ''}{note}",
                fips,
                f"{c.get('parcels', 0):,}",
                f"{c.get('holdings', 0):,}",
                str(c.get("largest_holding_acres") or ""),
                str(secs or ""),
                str(c.get("peak_rss_mb") or ""),
            ]
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------------------- pulling
def object_store_client(env: Mapping[str, str] | None = None) -> tuple[Any, str]:
    """(S3 client, bucket) for the private object store the build writes to, from the variables the beta
    deploy already renders (`R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_ENDPOINT` or
    `R2_ACCOUNT_ID`; the same as `services/context_sample/sync.py`). Raises naming any that are missing."""
    from pipeline.connectors.objectstore import build_s3_client

    env = os.environ if env is None else env
    bucket = (env.get("R2_BUCKET") or "").strip()
    endpoint = (env.get("R2_ENDPOINT") or "").strip()
    account = (env.get("R2_ACCOUNT_ID") or "").strip()
    if not endpoint and account:
        endpoint = f"https://{account}.r2.cloudflarestorage.com"
    key_id = (env.get("R2_ACCESS_KEY_ID") or "").strip()
    secret = (env.get("R2_SECRET_ACCESS_KEY") or "").strip()
    missing = [
        name
        for name, value in (
            ("R2_BUCKET", bucket),
            ("R2_ENDPOINT or R2_ACCOUNT_ID", endpoint),
            ("R2_ACCESS_KEY_ID", key_id),
            ("R2_SECRET_ACCESS_KEY", secret),
        )
        if not value
    ]
    if missing:
        raise ParcelsError(f"missing {', '.join(missing)}")
    return build_s3_client(endpoint, key_id, secret), bucket


def wanted_counties(value: str | None) -> set[str] | None:
    """`PARCELS_COUNTIES`: `all` (None), or FIPS codes. Unset is refused: a server says what it serves."""
    text = (value or "").strip()
    if not text:
        raise ParcelsError("set PARCELS_COUNTIES to 'all' or a list of county FIPS codes (48351,48241)")
    if text.lower() == "all":
        return None
    codes = {t.strip() for t in text.split(",") if t.strip()}
    bad = sorted(c for c in codes if not FIPS_RE.match(c))
    if bad:
        raise ParcelsError(f"not Texas county FIPS codes: {', '.join(bad)}")
    return codes


#: The summary `status` when the object store has nothing for this server yet (no `latest.json`, or
#: none of the counties it serves in it); `infra/scheduler/jobs.py` (`NOT_PUBLISHED`) reports it as
#: "not published", never as a failed builder.
NOT_PUBLISHED = "not_published"
#: What a "not published" answer tells the operator to run (`.github/workflows/parcels-layer.yml`).
DISPATCH_HINT = "run parcels-layer.yml (docs/66 'Site finder go-live')"


def _get_bytes(client: Any, bucket: str, key: str) -> bytes:
    """The object at `key`. FileNotFoundError when the store has no such key (`S3Backend.get` reads
    botocore's 404 structurally, as the flood and prospect pulls do); any other failure is a
    ParcelsError naming the key."""
    from pipeline.connectors.objectstore import S3Backend, StoreError

    try:
        return S3Backend(bucket, prefix="", client=client).get(key)
    except (StoreError, ValueError) as exc:
        raise ParcelsError(f"{key}: {type(exc).__name__}: {str(exc)[:200]}") from exc


def pull(
    *,
    client: Any,
    bucket: str,
    counties: set[str] | None,
    out_dir: pathlib.Path | None = None,
    prefix: str = OBJECT_PREFIX,
    keep_versions: int = 2,
) -> dict[str, Any]:
    """`<prefix>/latest.json` in the private object store -> the files of the wanted counties, each checked
    against its sha256, under `<out_dir>/<version>/`; `current.json` (the manifest narrowed to those
    counties, with local paths) is written last, so a reader never sees a half-pulled version. The
    per-parcel files are a derived-only source's table and are never on a public host: the server reads
    them with the object store's credentials, as it reads the prospect and flood builds.

    `out_dir` defaults to `parcels_dir()`, where `services/site_finder/compute.py::load_parcels` and the
    API read. No `latest.json` (parcels-layer.yml has not published yet), or none of the wanted counties
    in it -> `status: not_published` and nothing written: what the server held before stays."""
    if out_dir is None:
        from services.context_sample.parcels import parcels_dir

        out_dir = parcels_dir()
    latest_key = f"{prefix}/{LATEST_NAME}"
    try:
        raw = _get_bytes(client, bucket, latest_key)
    except FileNotFoundError:
        return {
            "source_id": SOURCE_ID,
            "status": NOT_PUBLISHED,
            "key": latest_key,
            "to_publish": DISPATCH_HINT,
        }
    latest = json.loads(raw)
    version = str(latest.get("version") or "")
    if not VERSION_RE.match(version):
        raise ParcelsError(f"{latest_key} names no usable version: {version!r}")
    current_path = out_dir / CURRENT_NAME
    held = json.loads(current_path.read_text(encoding="utf-8")) if current_path.exists() else {}
    selected = {
        fips: entry
        for fips, entry in (latest.get("counties") or {}).items()
        if counties is None or fips in counties
    }
    missing = sorted((counties or set()) - set(selected))
    if not selected:
        return {
            "source_id": SOURCE_ID,
            "status": NOT_PUBLISHED,
            "version": version,
            "not_published": missing,
            "to_publish": DISPATCH_HINT,
        }
    if held.get("version") == version and set(held.get("counties") or {}) == set(selected):
        return {"source_id": SOURCE_ID, "version": version, "status": "unchanged", "counties": len(selected)}
    local_root = out_dir / version
    local: dict[str, Any] = {}
    fetched = 0
    for fips, entry in sorted(selected.items()):
        entry_version = str(entry.get("version") or version)
        if not VERSION_RE.match(entry_version):
            raise ParcelsError(f"{fips}: bad version {entry_version!r}")
        files = {}
        for kind in ("parcels", "holdings"):
            spec = (entry.get("files") or {}).get(kind) or {}
            rel = str(spec.get("path") or "")
            if not re.match(r"^(parcels|holdings)/48\d{3}\.parquet$", rel):
                raise ParcelsError(f"{fips}: bad file path {rel!r}")
            dest = local_root / rel
            if not (dest.exists() and _sha256(dest) == spec.get("sha256")):
                key = f"{prefix}/{entry_version}/{rel}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".part")
                try:
                    client.download_file(bucket, key, str(tmp))
                except Exception as exc:
                    tmp.unlink(missing_ok=True)
                    raise ParcelsError(f"{key}: {type(exc).__name__}: {str(exc)[:200]}") from exc
                digest = _sha256(tmp)
                if digest != spec.get("sha256"):
                    tmp.unlink(missing_ok=True)
                    raise ParcelsError(f"{fips}: {rel} sha256 {digest} is not the manifest's")
                tmp.replace(dest)
                fetched += 1
            files[kind] = {**spec, "local": str(dest.relative_to(out_dir))}
        local[fips] = {**entry, "files": files}
    current = {
        **{k: v for k, v in latest.items() if k != "counties"},
        "counties": local,
        "pulled_at": _now(),
        "object_prefix": prefix,
        "requested": sorted(counties) if counties is not None else "all",
        "not_published": missing,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = current_path.with_suffix(".json.part")
    tmp.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
    tmp.replace(current_path)
    # Keep this version and the one before it; delete older version directories.
    versions = sorted(
        (p for p in (out_dir / LAYOUT_VERSION).glob("*") if p.is_dir()), key=lambda p: p.stat().st_mtime
    )
    keep = {local_root.resolve()}
    if held.get("version"):
        keep.add((out_dir / str(held["version"])).resolve())
    for p in versions[: max(0, len(versions) - keep_versions)]:
        if p.resolve() not in keep:
            shutil.rmtree(p, ignore_errors=True)
    return {
        "source_id": SOURCE_ID,
        "version": version,
        "status": "pulled",
        "counties": len(local),
        "files_fetched": fetched,
        "not_published": missing,
    }


# ---------------------------------------------------------------------------------------------- CLI
def _session() -> Any:
    from pipeline.connectors.http import PoliteSession

    return PoliteSession(rate_limits=RATE_LIMITS, timeout=600)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_plan = sub.add_parser("plan", help="The newest vintage and its county zips (the dry run with --check)")
    p_plan.add_argument("--counties", default="all")
    p_plan.add_argument("--out", type=pathlib.Path, required=True)
    p_plan.add_argument("--check", action="store_true", help="HEAD every county zip; download nothing")
    p_plan.add_argument(
        "--catalogue", type=pathlib.Path, help="Recorded catalogue answers (collections.json)"
    )
    p_plan.add_argument("--resources", type=pathlib.Path, help="Recorded resource list (resources.json)")
    p_plan.add_argument(
        "--batches", type=int, default=0, help="Print the counties as this many batches (JSON)"
    )

    p_county = sub.add_parser("county", help="Build one county")
    p_county.add_argument("--plan", type=pathlib.Path, required=True)
    p_county.add_argument("--fips", required=True)
    p_county.add_argument("--out-dir", type=pathlib.Path, required=True)
    p_county.add_argument(
        "--work-dir", type=pathlib.Path, default=pathlib.Path(tempfile.gettempdir()) / "txgio"
    )
    p_county.add_argument("--zip", type=pathlib.Path, help="A county zip a person downloaded (no network)")
    p_county.add_argument("--retrieved-at", help="When that zip was downloaded (ISO time)")

    p_man = sub.add_parser(
        "manifest", help="County summaries -> manifest.json; exit 1 when it may not be promoted"
    )
    p_man.add_argument("--plan", type=pathlib.Path, required=True)
    p_man.add_argument("--out-dir", type=pathlib.Path, required=True)
    p_man.add_argument("--version", required=True)
    p_man.add_argument("--previous", type=pathlib.Path)
    p_man.add_argument("--partial", action="store_true")
    p_man.add_argument("--expected", default="", help="Space-separated FIPS codes the build was asked for")
    p_man.add_argument("--allow-dropped", default="")

    p_rep = sub.add_parser("report", help="Markdown summary of a manifest or a dry run")
    p_rep.add_argument("--manifest", type=pathlib.Path)
    p_rep.add_argument("--checks", type=pathlib.Path)

    p_pull = sub.add_parser(
        "pull", help="Fetch the published counties a server serves, from the private object store (R2_*)"
    )
    p_pull.add_argument("--prefix", default=OBJECT_PREFIX)
    p_pull.add_argument("--counties", help="Default: $PARCELS_COUNTIES ('all' or FIPS codes)")
    p_pull.add_argument(
        "--out-dir",
        type=pathlib.Path,
        help="Default: PARCELS_DATA_DIR or <data>/normalized/context/us.tx.txgio.parcels",
    )

    args = parser.parse_args(argv)
    out: Any
    if args.command == "plan":
        if args.catalogue and args.resources:
            plan = parse_plan(
                json.loads(args.catalogue.read_text(encoding="utf-8")),
                json.loads(args.resources.read_text(encoding="utf-8")),
            )
            session = None
        else:
            session = _session()
            plan = fetch_plan(session)
        chosen = select_counties(plan, args.counties)
        plan.counties = chosen
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(plan.as_dict(), indent=1), encoding="utf-8")
        out = {
            "collection_id": plan.collection_id,
            "publication_date": plan.publication_date,
            "counties": len(chosen),
        }
        if args.check:
            checks = check_files(session or _session(), chosen)
            (args.out.parent / "checks.json").write_text(json.dumps(checks, indent=1), encoding="utf-8")
            out["checks_ok"] = sum(1 for c in checks if c.get("ok"))
            out["checks_failed"] = [c["fips"] for c in checks if not c.get("ok")]
        if args.batches:
            size = max(1, math.ceil(len(chosen) / args.batches))
            batches = [",".join(c.fips for c in chosen[i : i + size]) for i in range(0, len(chosen), size)]
            print(json.dumps(batches))  # noqa: T201 - the workflow's matrix
            return
    elif args.command == "county":
        plan = Plan.from_dict(json.loads(args.plan.read_text(encoding="utf-8")))
        out = run_county(
            plan, args.fips, args.out_dir, args.work_dir, zip_path=args.zip, retrieved_at=args.retrieved_at
        )
        out = {
            k: out.get(k)
            for k in (
                "fips",
                "status",
                "reason",
                "parcels",
                "holdings",
                "largest_holding_acres",
                "seconds",
                "peak_rss_mb",
            )
        }
    elif args.command == "manifest":
        plan = Plan.from_dict(json.loads(args.plan.read_text(encoding="utf-8")))
        previous = (
            json.loads(args.previous.read_text(encoding="utf-8"))
            if args.previous and args.previous.exists()
            else None
        )
        manifest, problems = build_manifest(
            args.out_dir,
            plan,
            args.version,
            previous=previous,
            partial=args.partial,
            expected=args.expected.split(),
            allow_dropped=args.allow_dropped.split(),
        )
        (args.out_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8"
        )
        gate = {
            "version": args.version,
            "promote": not problems,
            "problems": problems,
            "failures": manifest["failures"],
        }
        print(json.dumps(gate))  # noqa: T201 - the gate's verdict for the build script
        if problems:
            raise SystemExit(1)
        return
    elif args.command == "report":
        manifest_doc = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else {}
        checks_doc = json.loads(args.checks.read_text(encoding="utf-8")) if args.checks else []
        sys.stdout.write(report_markdown(manifest_doc, checks_doc))
        return
    else:
        try:
            wanted = wanted_counties(
                args.counties if args.counties is not None else os.environ.get("PARCELS_COUNTIES")
            )
            client, bucket = object_store_client()
            out = pull(
                client=client, bucket=bucket, counties=wanted, out_dir=args.out_dir, prefix=args.prefix
            )
        except ParcelsError as exc:
            raise SystemExit(f"txgio_parcels pull: {exc}") from exc
    print(json.dumps(out, default=str))  # noqa: T201 - CLI summary line


if __name__ == "__main__":
    main()
