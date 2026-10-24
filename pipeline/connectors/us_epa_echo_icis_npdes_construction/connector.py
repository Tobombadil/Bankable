"""us.epa.echo.icis_npdes_construction — EPA ICIS-NPDES construction stormwater coverage of data centres.

What the source is (measured 2026-10-23 and 2026-10-24, docs/08 §4.1). EPA ECHO's weekly ICIS-NPDES bulk
download (`npdes_downloads.zip`, 357,154,247 bytes, Last-Modified 2026-10-18) holds 15 CSVs; this
connector reads three: `ICIS_FACILITIES.csv` (1,256,976 facilities, one per NPDES id; 73.1 MB
compressed), `ICIS_PERMITS.csv` (1,774,058 permit versions; 101.5 MB) and `NPDES_PERM_COMPONENTS.csv`
(782,220 rows; 3.0 MB). Under the NPDES electronic reporting rule the states report general-permit
coverage to EPA, so construction stormwater coverage reaches ICIS: any construction that disturbs an
acre or more needs it (https://www.epa.gov/npdes/stormwater-discharges-construction-activities). A
data-centre campus files a notice of intent (NOI) before grading and a notice of termination (NOT)
once the site is stabilised. Columns: https://echo.epa.gov/tools/data-downloads/icis-npdes-download-summary.

Fetch (politeness). `echo.epa.gov/robots.txt` allows `/files/` with `Crawl-delay: 10`; the manifest's
0.1 rps is that delay. The zip's central directory is read with one ranged GET of its last 64 KiB
(conditional on the previous run's ETag and Last-Modified, which echo.epa.gov answers with 304), then
one ranged GET per member, each streamed to a temporary file and inflated row by row
(`MemberStream`), so the 387 MB permits member never sits in memory. The central-directory reader,
the conditional-request headers and the point-in-county lookup are the ICIS-Air connector's,
imported (`_counties` and `_states` are private names there: this connector imports them rather
than edit that one). A server that ignores `Range` gets the whole zip streamed to a temporary file
and read with `zipfile`. A member whose Last-Modified differs from the tail's fails the run (the file
changed between reads); a CRC-32 or size mismatch is a `ParseError`. The stored snapshot is the
candidate rows of the three members, every master general permit's title and dates, the
construction-coverage filings of each state by month (`reporting`) and the file's own date (179 KB),
never the 178 MB of upstream bytes (the ICIS-Air choice): the fetch-side pre-filter (`is_candidate`)
is looser than the selection rule and `parse` applies the rule, so a fixture proves it. Cost: 5
requests and about 52 s when the file changed, 2 requests (robots.txt and one 304) when it did not.

Selection (`select_basis`, the survey's rule, docs/08 §3). A facility is a candidate data centre when
`FACILITY_NAME` says "data center", "data centre", "datacenter", "hyperscale", "DC campus", "server
farm" or "data hall", or a form of the same words the file carries: an abbreviation ("DATA CENT
SHELL", "DATA CTR"), a name cut inside the words by the field's length ("... BRODERICK DATA CEN") and
the two misspellings found ("DATA CETNER", "DATA CENETER"). A cryptocurrency mine is not one
(`is_crypto_mine`): a name that declares mining (`CRYPTO_RE`: "CIPHER MINING ODESSA DATA CENTER",
"CARTERVILLE - BLOCKCHAIN DATA CENTER", "DIGITAL ASSET DATA CENTER", "BITPOWER DATA CENTER"), or a
mine named in its operator's own filings (`KNOWN_CRYPTO_SITES`: Hut 8's King Mountain, Cipher's
Alborz, Bear and Chief, Argo's Helios); 14 coverages on 2026-10-18. A facility is kept only when its
current permit (the lowest `VERSION_NMBR`, which ICIS numbers 0) is construction stormwater coverage
(`construction_basis`):
- `component`: the permit's own components include `SWC` ("Storm Water Construction");
- `master_component`: it has no component of its own and its master general permit
  (`MASTER_EXTERNAL_PERMIT_NMBR`) carries `SWC`;
- `master_title`: neither carries a component and the master's own title names construction
  (`CONSTRUCTION_TITLE_RE`) without naming dewatering, hydrostatic tests, an exclusion of construction
  or utility operations (`NOT_CONSTRUCTION_TITLE_RE`). Seven masters need it, among them Arizona's
  2020 Construction General Permit (`AZG200001`, "CONSTRUCTION GENERAL PERMIT", 7,236 coverages, none
  with a component) and Kansas's `KSR100000` ("Construction Stormwater").
A permit whose own components exist but exclude `SWC` (industrial stormwater, a POTW) is not
construction. Non-construction coverage of a data centre (non-contact cooling water, a concrete batch
plant permit, an individual discharge permit) is outside this source. A site made only of permits
for works that serve a data centre (`is_infrastructure`: a substation or switching station, a feeder,
a duct package, a pipeline, offsite roads, a batch plant or laydown yard, or any permit a utility
files) is not a data-centre record and is dropped (19 on 2026-10-18, listed in the run's
`snapshot.meta.infrastructure_only_sites`); such a permit inside a data-centre site stays in it.
Precision, hand-checked on a random 50 of the 229 published sites (seed 20261024, crypto counted as
a miss): 45 are a data-centre site, 2 are works for one (a parking lot, an auxiliary site), 3 are
uncertain and may be mines (a bare "DATA CENTER" in Childress County, TX; TeraWulf's Lake Mariner,
a bitcoin mine adding computing halls; "APL DATA CENTER", East Wenatchee). Recall, on a hand-checked
random 160 construction coverages within 300 m of a held data-centre point: 54 were that data
centre's own construction and the name rule finds 23 of them (43 %, unchanged by the 2026-10-24
rules: Northern Virginia files under campus codes and project names, "IAD 550 RGP", "PEANUT MASS
GRADING"). Operator names alone (`OPERATOR_RE` in the ICIS-Air connector) and "technology campus"
names would find more and are not used yet: the file has no industry code to guard them. Details in
docs/08 §4.1.

Record model: one `proposal` per real-world site, `kind = technology = load` (the vocabulary's token
for a data centre: `pipeline/normalize.py` TECH_RULES, as for ICIS-Air and Virginia DEQ),
`technology_raw = "Data Center"`, `capacity_mw` and `sponsor_name` null (the file has no MW, and the
facility name mixes operator, campus code and phase). Permits are grouped into sites
(`group_sites`, `site_link`): two selected permits in one state are one site when their exact points
lie within `SITE_NEAR_M`; or within `SITE_TOKEN_M` and their names share a distinctive token (an
operator or project word: not a generic word, a number, a metro code, a Northern Virginia place word
or a word of the permit's own city or county; a code split by a hyphen is one word, "CCO-06" is
"CCO06"); or within `SITE_CODE_M` and their names carry building codes of one family ("CYS13",
"CYS14"; a state code such as "VA10" is no family); or their names, less the data-centre words, legal
forms and "project", are identical (`name_key`, numbers kept) within `SITE_SAME_NAME_M` or without a
point; or, without a point, they share a distinctive token and the same county code or city. A
permit whose name has no distinctive word joins the one site within `SITE_GENERIC_M` and none when two
are ("LANCASTER DATA CENTER" in Lancaster joins LAN01, 150 m). Phased campuses file one NOI per
phase, contractors file their own, and Arizona re-covered every open site under its 2025 permit with
a new id at the same point, so 51 of the 229 published sites hold several permits. Residual error,
hand-checked on 2026-10-24: 2 pairs share an operator word beyond 1 km (Nexus at Hubbard, 1,425 m;
Stargate Abilene buildings 9 and 10, filed twice, 1,572 m), and 1 pair is one operator under two
brands (NTT TX-4 and RagingWire, 163 m), so about 3 campuses show twice. Union-find chaining means a
new permit filed between two published sites can join them: one site key then becomes a `removed`
event and the other site carries both.
- source_record_id: the NPDES id of the site's first coverage for the data centre itself (works that
  serve it come after; then the earliest date the permit row carries, then the lowest id), stable
  while the site's permits are and the sites around it stay apart. Its facility name is
  `name_canonical`; every permit is in `raw.permits` and in `cross_refs` as `npdes:<id>`, followed by
  every FRS id the permits carry as `frs:<id>` (`cross_refs`). FRS is a facility-registry namespace
  in `pipeline/resolve.py`: the same id on an ICIS-Air record is that facility (a deterministic
  match, 9 of the 11 merges on 2026-10-24), and a different one refuses the fuzzy pair. Without it
  the resolver joined one operator's distinct campuses through a construction site's name (Vantage
  GA1 and GA2, 12.7 km apart; TX 11 and TX2; Aligned ORD03 with Aligned's Northlake campus, 14.5 km).
- held sites (`held.yaml`, `HELD_PATH`): a site that duplicates a record ICIS-Air or Virginia DEQ
  already holds (the same campus within 300 m, or farther under the held record's own building code
  or name) is not emitted, because the resolver compares names, counties and registry ids and not
  points, so the two would be published as two records, often in contradicting states (H5 Ashburn
  under construction 11 m from DEQ's built H5). A site the resolver would join to held records of
  distinct campuses is held too (Vantage VA16: Virginia DEQ rows cite no `frs` id, so it chained four
  Vantage campuses 11.7 km across). 47 sites on 2026-10-24 (46 duplicates, 3 of them uncertain, and 1
  bridge), listed in `snapshot.meta.held_sites`. The list is reviewed by hand and goes stale: a new
  permit beside a held record is published until the next review or until the resolver compares
  points (docs/08 §4.1, PLAN decisions).
- source_url: ECHO's Detailed Facility Report for that permit's FRS id (a page for people; robots.txt
  disallows it for crawlers and this connector never fetches it), else the zip.
- lifecycle_state (status_map.yaml; the file's own date, `file.as_of`, is the date every rule is
  judged at, so the same bytes always give the same states). What each coverage proves:
  a pending NOI (`PND`) is `filed`; coverage in force (`EFF`, administratively continued `ADC`) is
  `under_construction`, because coverage exists only for a site in its construction phase, though an
  NOI precedes ground-breaking by days to months and a stalled project can keep coverage; a
  terminated coverage (`TRM`, or any permit with a `TERMINATION_DATE`) is `built`: the operator
  certified that earth-disturbing work ended and the site is stabilised, which does not prove the
  data centre operates; an expired coverage (`EXP`, no NOT) is `unknown`. Three things the filing
  does not prove are `unknown` too (`_permit_state`): coverage in force whose own expiration date
  passed more than `LAPSED_COVERAGE_DAYS` before the file (`lapsed_coverage`: Virginia's 2019-permit
  coverages, expired 2024-06-30, never closed); coverage in force in a state that no longer reports
  to ICIS (`state_not_reporting`, `Evidence.reports`: California, Wyoming, Oklahoma, Guam on
  2026-10-18), unless the coverage is itself from the last `REPORTING_WINDOW_DAYS`; and a
  termination dated on the coverage's expiration date or the day after (`terminated_at_expiry`: the
  coverage lapsing, not a notice of termination). A site's state is its most recent filing's
  (`site_decision`), except that coverage in force filed within `HANDOVER_DAYS` before the latest
  termination keeps the site under construction: a new operator's NOI on a handover, a renewal or the
  next phase. The status of the permit that decided is `status_raw`; `status_as_of` is the file's
  date. 2026-10-18 file, 229 published sites: under_construction 105, built 77, unknown 47.
- first_covered: the earliest date a data-centre permit of the site carries that is not its master
  general permit's own (`Permit.noi_date`; Georgia, Virginia and California stamp the general
  permit's date on their coverages), None where none is (every Virginia site).
- location: `GEOCODE_LATITUDE`/`GEOCODE_LONGITUDE` are the NOI's site coordinate. They reach `raw` as
  `Latitude`/`Longitude` (which `services/ingest/loader.py` promotes to an `exact` point for a `raw_ok`
  source) only when both carry at least `COORD_MIN_DECIMALS` decimals (Georgia files whole degrees,
  "34,-84", on 4,547 facilities) and the point lies inside the facility's own state (`placement_for`).
  A site's point is its first exact permit point in key order. Otherwise no coordinate is passed on,
  and the loader places the record at its county (from `COUNTY_CODE`, a state-prefixed FIPS code such
  as "TX489") or its state. 213 of the 229 published sites have an exact point.
- county: for an exact point, the county the point falls in (vendored Census boundaries, the ICIS-Air
  helper); otherwise `COUNTY_CODE` read through the same boundaries' names. `raw.permits[*]
  .county_code_matches_point` records where the two disagree.

Personal data (docs/13 §5.4). `ICIS_PERMITS.csv` `PERMIT_NAME` is the permittee, and for general-permit
coverage it is sometimes a natural person (four in a 30-row sample, docs/08 §4.1): it is never
stored. `fetch` keeps an explicit column list per table (`FACILITY_COLUMNS`, `PERMIT_COLUMNS`), so
the column never reaches the snapshot; `redact` and `parse` drop it (and any contact-like column)
again should a stored payload ever carry one. The one exception is a master general permit's own
`PERMIT_NAME`, its title ("CONSTRUCTION GENERAL PERMIT"), read for `PERMIT_TYPE_CODE == "NGP"` rows
only and stored as `MASTER_TITLE`. A selected facility name that `services.personal_names` reads as
a person's name is dropped, not published (one on 2026-10-18). That check reads the whole name: it
catches a name that is a person's ("PAT DOE DATACENTER") but not a person's name inside a project
name ("... JOHN SMITH DATA CENTER" reads as an organisation); facility names are project names, and
the one such case seen, a laydown yard named for a road or a person, is works-only and never emitted.

Terms: US federal government work, public domain (17 U.S.C. §105; docs/13 §2.12 EPA hedge), the
terms of the ICIS-Air row.
"""

from __future__ import annotations

import collections
import contextlib
import csv
import datetime as dt
import email.utils
import io
import itertools
import json
import logging
import math
import pathlib
import re
import struct
import tempfile
import time
import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import IO, Any, ClassVar

import pandas as pd
import yaml

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot, utcnow
from pipeline.connectors.canonical import harmonise_status, norm_county, norm_name
from pipeline.connectors.us_epa_echo_icis_air.connector import (
    LOCAL_HEADER_SLACK,
    REGIONS,
    TAIL_BYTES,
    ZipMember,
    _counties,
    _states,
    central_directory,
    conditional_headers,
    county_for_point,
)
from pipeline.context.geo import county_label
from services.personal_names import is_personal_name

log = logging.getLogger(__name__)

ZIP_URL = "https://echo.epa.gov/files/echodownloads/npdes_downloads.zip"
FACILITIES_MEMBER = "ICIS_FACILITIES.csv"
PERMITS_MEMBER = "ICIS_PERMITS.csv"
COMPONENTS_MEMBER = "NPDES_PERM_COMPONENTS.csv"
DFR_URL = "https://echo.epa.gov/detailed-facility-report?fid={registry_id}"

#: The columns each table keeps. Everything else, `PERMIT_NAME` above all, is never stored.
FACILITY_COLUMNS = (
    "NPDES_ID",
    "ICIS_FACILITY_INTEREST_ID",
    "FACILITY_UIN",
    "FACILITY_TYPE_CODE",
    "FACILITY_NAME",
    "LOCATION_ADDRESS",
    "SUPPLEMENTAL_ADDRESS_TEXT",
    "CITY",
    "COUNTY_CODE",
    "STATE_CODE",
    "ZIP",
    "GEOCODE_LATITUDE",
    "GEOCODE_LONGITUDE",
)
PERMIT_COLUMNS = (
    "EXTERNAL_PERMIT_NMBR",
    "VERSION_NMBR",
    "PERMIT_TYPE_CODE",
    "PERMIT_STATUS_CODE",
    "AGENCY_TYPE_CODE",
    "ISSUING_AGENCY",
    "ORIGINAL_ISSUE_DATE",
    "ISSUE_DATE",
    "EFFECTIVE_DATE",
    "EXPIRATION_DATE",
    "RETIREMENT_DATE",
    "TERMINATION_DATE",
    "MASTER_EXTERNAL_PERMIT_NMBR",
)
#: `MASTER_DATES`: every date any version of the master carries ("|"-joined), so a coverage date
#: that is the general permit's own can be told from the coverage's (`Permit.noi_date`).
MASTER_COLUMNS = ("MASTER_EXTERNAL_PERMIT_NMBR", "MASTER_TITLE", "MASTER_STATUS_CODE", "MASTER_DATES")
#: Construction-coverage filings by state (the permit id's two-letter prefix) and month (`YYYYMM`).
REPORTING_COLUMNS = ("STATE_CODE", "MONTH", "FILINGS")
COMPONENT_COLUMNS = ("EXTERNAL_PERMIT_NMBR", "COMPONENT_TYPE_CODE")
REQUIRED_FACILITY = ("NPDES_ID", "FACILITY_NAME", "STATE_CODE", "GEOCODE_LATITUDE", "GEOCODE_LONGITUDE")
REQUIRED_PERMIT = (
    "EXTERNAL_PERMIT_NMBR",
    "VERSION_NMBR",
    "PERMIT_TYPE_CODE",
    "PERMIT_STATUS_CODE",
    "TERMINATION_DATE",
    "MASTER_EXTERNAL_PERMIT_NMBR",
)
REQUIRED_COMPONENT = COMPONENT_COLUMNS
#: The permittee. Read upstream only for master general permits (their title); never stored.
PERMITTEE_COLUMN = "PERMIT_NAME"
PERSONAL_COLUMNS = frozenset({PERMITTEE_COLUMN})
CONTACT_COLUMN = re.compile(r"CONTACT|PHONE|EMAIL|E_MAIL|PERSON|PERMITTEE|OWNER|OPERATOR_NAME", re.I)
MASTER_PERMIT_TYPE = "NGP"

# ------------------------------------------------------------------ selection
#: Fetch-side pre-filter, deliberately looser than `NAME_RE` ("DATA CHEM, INC.", "DATA COURT ...").
CANDIDATE_RE = re.compile(r"\bDATA\s*C|DATACENT|HYPERSCALE|\bDC\s+CAMPUS\b|SERVER\s*FARM|\bDATA\s+HALL", re.I)
NAME_RE = re.compile(
    r"DATA\s*CENT(?:ER|RE)|DATACENT"  # the survey's words: data center, data centre, datacenter
    r"|\bDATA\s*CENTE?\b|\bDATA\s*CTRS?\b"  # abbreviated: "SAT14 DATA CENT SHELL", "DATA CTR"
    r"|\bDATA\s*C(?:E(?:N(?:T(?:E)?)?)?)?\s*$"  # cut by the field length: "... DATA CEN"
    r"|\bDATA\s*(?:CETNER|CENETER)\b"  # the two misspellings in the 2026-10-18 file
    r"|HYPERSCALE|\bDC\s+CAMPUS\b"
    r"|\bSERVER\s*FARMS?\b|\bDATA\s+HALLS?\b",  # "SERVERFARM ATL2", "CPT-01 CHIRISA DATA HALL"
    re.I,
)
#: A name that declares cryptocurrency mining: a bitcoin mine calls itself a data center in these
#: filings ("CIPHER MINING ODESSA DATA CENTER", "CARTERVILLE - BLOCKCHAIN DATA CENTER"). ICIS-Air
#: excludes the same class (`NON_DC_NAME_RE` there: flare-gas mining at well pads).
CRYPTO_RE = re.compile(r"CRYPTO|BLOCKCHAIN|BITCOIN|\bMINING\b|\bDIGITAL\s+ASSETS?\b|\bBITPOWER\b", re.I)
#: Bitcoin mines whose filed name does not say so, by state, from the operators' own filings:
#: Cipher Mining's 10-K for 2024 (https://www.sec.gov/Archives/edgar/data/1819989/000181998925000005/
#: cifr-20241231.htm) names the Alborz (near Happy), Bear and Chief (near Andrews) joint-venture mining
#: sites; Hut 8's 6-K (https://www.sec.gov/Archives/edgar/data/1731805/000110465923091050/
#: tm2322932-2_exh99x2.htm) the King Mountain joint venture's mining site in Upton County; Argo
#: Blockchain built its Helios mine near Afton, Dickens County, in 2021
#: (https://www.galaxy.com/newsroom/galaxy-to-acquire-helios-bitcoin-mining-facility-from-argo-blockchain).
KNOWN_CRYPTO_SITES: dict[str, re.Pattern[str]] = {
    "TX": re.compile(r"^\s*(?:KING\s+MOUNTAIN|ALBORZ|BEAR|CHIEF|ARGO)\s+DATA\s+CENTER\b", re.I),
}
CONSTRUCTION_COMPONENT = "SWC"
CONSTRUCTION_TITLE_RE = re.compile(r"\bCONSTRUCTION\b", re.I)
NOT_CONSTRUCTION_TITLE_RE = re.compile(r"DEWATER|EXCLUD|NON-?\s*CONSTRUCTION|HYDROSTATIC|OPERATIONS", re.I)
#: A permit named for works that serve a data centre rather than the data centre itself: a substation
#: or switching station, a feeder or line, a pipeline, offsite roads and utilities, a contractor's
#: batch plant or laydown yard.
INFRASTRUCTURE_RE = re.compile(
    r"SUBSTATION|\bSUBSTAT\b|SWITCHYARD|\b(?:SWITCH|SWITCHING|PROPOSED|ELECTRIC|POWER|LIFT|PUMP)\s+STATION\b|"
    r"FEEDER|TRANSMISSION|PIPELINE|DUCT\s*(?:BANK|PACKAGE)|\bTUG\s+LINES?\b|"
    r"\bTO\s+TSS\b|\bOFFSITES?\b|ROADWAY|\bROAD\s+IMPROVEMENT|WATER\s*(?:LINE|MAIN)|WATERLINE|SEWER|"
    r"FORCE\s*MAIN|GAS\s+LINE|BATCH\s+PLANT|LAYDOWN|DATA\s*CENT\w*\s+(?:INFRASTRUCTURE|CONNECT)\b",
    re.I,
)
#: A permit a utility files for a data centre is the utility's works for it ("COMED KEHOE DATA CENTER
#: DUCT PACKAGE PROJECT", "ENTERGY LOUISIANA, LLC: RIVER BEND DATA CENTER-PROPOSED STATION").
UTILITY_FILER_RE = re.compile(
    r"^\s*(?:COMED|COMMONWEALTH\s+EDISON|ENTERGY|ONCOR|CENTERPOINT|AEP\s+TEXAS|DOMINION\s+ENERGY|"
    r"DUKE\s+ENERGY|GEORGIA\s+POWER|XCEL\s+ENERGY|AMEREN|NV\s+ENERGY|SALT\s+RIVER\s+PROJECT|"
    r"ARIZONA\s+PUBLIC\s+SERVICE)\b",
    re.I,
)
_JOIN = re.compile(r"\s+AND\s+|&", re.I)


def is_candidate(row: Mapping[str, Any]) -> bool:
    """Fetch-side pre-filter on the facility name alone (module docstring)."""
    return bool(CANDIDATE_RE.search(str(row.get("FACILITY_NAME") or "")))


def is_infrastructure(name: Any) -> bool:
    """A permit for works that serve a data centre, not the data centre (`INFRASTRUCTURE_RE`,
    `UTILITY_FILER_RE`). A name that joins two things is the data centre's own permit when one of
    its parts names the data centre and no works ("DATA CENTER AND SUBSTAT", "ORD04 & ELECTRICAL
    DUCT BANK"), and works when none does ("LAYDOWN YARD ... PHASE 1 & PHASE 2")."""
    text = str(name or "")
    if UTILITY_FILER_RE.search(text):
        return True
    if not INFRASTRUCTURE_RE.search(text):
        return False
    return not any(NAME_RE.search(part) and not INFRASTRUCTURE_RE.search(part) for part in _JOIN.split(text))


def is_crypto_mine(name: Any, state: Any = "") -> bool:
    """A cryptocurrency mine filed as a data center (`CRYPTO_RE`, `KNOWN_CRYPTO_SITES`)."""
    text = str(name or "")
    known = KNOWN_CRYPTO_SITES.get(str(state or "").strip().upper())
    return bool(CRYPTO_RE.search(text)) or bool(known is not None and known.search(text))


def select_basis(name: Any) -> str | None:
    """Why a facility name counts as a data centre; None = not selected."""
    return "name" if NAME_RE.search(str(name or "")) else None


def is_construction_title(title: Any) -> bool:
    text = str(title or "")
    return bool(CONSTRUCTION_TITLE_RE.search(text)) and not NOT_CONSTRUCTION_TITLE_RE.search(text)


def construction_basis(
    permit_id: str,
    master_id: str,
    components: Mapping[str, frozenset[str]],
    master_titles: Mapping[str, str],
) -> str | None:
    """Why a permit is construction stormwater coverage (module docstring); None = it is not."""
    own = components.get(permit_id)
    if own:
        return "component" if CONSTRUCTION_COMPONENT in own else None
    if not master_id:
        return None
    if CONSTRUCTION_COMPONENT in components.get(master_id, frozenset()):
        return "master_component"
    if is_construction_title(master_titles.get(master_id)):
        return "master_title"
    return None


# ------------------------------------------------------------------ placement
COORD_MIN_DECIMALS = 3
COORD_DECIMALS = 6
_NUMBER = re.compile(r"\s*[-+]?\d+(?:\.(\d*))?\s*")


def _decimals(text: str) -> int | None:
    m = _NUMBER.fullmatch(text)
    return None if m is None else len(m.group(1) or "")


def placement_for(lat_text: Any, lon_text: Any, state: str) -> tuple[str, float | None, float | None]:
    """(placement, lat, lon). `exact` only for a coordinate with at least `COORD_MIN_DECIMALS`
    decimals on both axes that lies inside the stated state; otherwise no coordinate is returned and
    the reason is the placement label."""
    lat_s, lon_s = str(lat_text or "").strip(), str(lon_text or "").strip()
    dl, dn = _decimals(lat_s), _decimals(lon_s)
    if dl is None or dn is None:
        return "no_coordinate", None, None
    lat, lon = float(lat_s), float(lon_s)
    if (lat == 0.0 and lon == 0.0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "no_coordinate", None, None
    if min(dl, dn) < COORD_MIN_DECIMALS:
        return "coarse_coordinate", None, None
    if state and _states().lookup(lon, lat) != f"US-{state}":
        return "point_outside_state", None, None
    return "exact", round(lat, COORD_DECIMALS), round(lon, COORD_DECIMALS)


@lru_cache(maxsize=1)
def _state_fips() -> dict[str, str]:
    """Two-letter state code -> two-digit state FIPS, from the vendored county boundaries."""
    doc = json.loads((REGIONS / "us_counties.geojson").read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for f in doc.get("features", []):
        props = f.get("properties") or {}
        state, geoid = str(props.get("state_code") or ""), str(props.get("region_id") or "")
        if state.startswith("US-") and len(geoid) == 5:
            out.setdefault(state[3:], geoid[:2])
    return out


def county_geoid(code: Any) -> str | None:
    """`COUNTY_CODE` ("TX489": state + county FIPS) as a five-digit county GEOID, or None."""
    text = str(code or "").strip().upper()
    if len(text) != 5 or not text[2:].isdigit():
        return None
    fips = _state_fips().get(text[:2])
    return f"{fips}{text[2:]}" if fips else None


def county_from_code(code: Any) -> str | None:
    """The county `COUNTY_CODE` names, in the shared spelling (`county_label`)."""
    geoid = county_geoid(code)
    if geoid is None:
        return None
    _, names = _counties()
    return county_label(geoid, names.get(geoid)) if geoid in names else None


# ------------------------------------------------------------------ sites
SITE_NEAR_M = 100.0
SITE_TOKEN_M = 1000.0
SITE_SAME_NAME_M = 5000.0
#: A permit whose name has no distinctive word ("LANCASTER DATA CENTER" in Lancaster, "MILAM DATA
#: CENTER BUILDING 1" in Milam County) joins the one site within this distance, and none when two or
#: more are (`group_sites`), so it can never bridge two sites.
SITE_GENERIC_M = 500.0
#: Building codes of one family ("CYS13" and "CYS14", 176 m; "CH3" and "CH4", 395 m) within this
#: distance are one campus filed building by building. A state code is no family ("VA10" is NTT's,
#: "VA11" Vantage's).
SITE_CODE_M = 400.0
_CODE_TOKEN = re.compile(r"^([A-Z]{2,4})(\d{1,3}[A-Z]?)$")
_STATE_CODES = frozenset(
    """
    AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC
    ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR GU VI AS MP
    """.split()
)


def code_families(tokens: frozenset[str]) -> frozenset[str]:
    """The letter prefixes of a name's building codes ("CYS13" -> "CYS"), state codes excepted."""
    out = set()
    for t in tokens:
        m = _CODE_TOKEN.match(t)
        if m and m.group(1) not in _STATE_CODES:
            out.add(m.group(1))
    return frozenset(out)


#: Words that say nothing about which campus a permit belongs to: the data-centre words, phase and
#: plan vocabulary, legal forms, compass points, and the metro codes several operators use.
GENERIC_SITE_TOKENS = frozenset(
    """
    DATA CENTER CENTERS CENTRE CENTRES CENT CENTE DATACENTER DATACENTERS DATACENTRE CTR CTRS HYPERSCALE
    CAMPUS PHASE BUILDING BUILDINGS BLDG BLDGS SITE SITES PLAN PLANS GRADING EARLY MASS ROUGH FINAL
    PROJECT PROJECTS PROPOSED NEW EXPANSION ADDITION DEVELOPMENT SUBDIVISION FILING APPLICATION
    SUBSTATION SWITCHYARD UTILITY UTILITIES INFRASTRUCTURE IMPROVEMENT IMPROVEMENTS OFFSITE ONSITE
    ROAD ROADWAY ROADS STREET PARKING STAGING AREA MAIN SHELL ENGINEERING DEFERRED
    PARK TECHNOLOGY TECH BUSINESS INDUSTRIAL COMMERCE CORPORATE CENTRAL COUNTY CITY
    THE AND FOR LLC INC CORP CORPORATION COMPANY LTD HOLDINGS OWNER PROPCO DBA
    NORTH SOUTH EAST WEST NORTHEAST NORTHWEST SOUTHEAST SOUTHWEST
    PHX ORD IAD DFW ATL CHI SJC DEN SAT AUS HOU CMH SLC LAS PDX SEA CLT RIC DCA NVA ASH LAX NYC
    ASHBURN STERLING LOUDOUN MANASSAS DULLES
    """.split()
)
#: For `name_key` only the data-centre words, legal forms and the words that only announce a project
#: are dropped: phase numbers and building codes stay, so "MICROSOFT PHX 16" and "PHX 70 - MICROSOFT"
#: are different names, and "PROJECT BORDEN COUNTY DATA CENTER" is "BORDEN COUNTY DATA CENTER".
NAME_KEY_GENERIC = frozenset(
    "DATA CENTER CENTERS CENTRE CENTRES DATACENTER DATACENTERS DATACENTRE CTR CTRS THE AND LLC INC "
    "PROJECT PROPOSED".split()
)
_TOKEN = re.compile(r"[A-Z0-9]+")
#: A campus or building code split by a hyphen ("CCO-06", "ORD-1") is one word: "CCO-06 DATA CENTER"
#: and "CCO06 DATA CENTER" are one campus, and "CCO" alone would say nothing.
_CODE_HYPHEN = re.compile(r"\b([A-Z]+)-(\d+[A-Z]?)\b")


def _words(text: Any) -> list[str]:
    return _TOKEN.findall(_CODE_HYPHEN.sub(r"\1\2", str(text or "").upper()))


def site_tokens(name: Any, city: Any = "", county: Any = "") -> frozenset[str]:
    """Distinctive name tokens: three characters or more, not all digits, not generic, not a word of
    the permit's own city or county ("ELK GROVE", "LANCASTER", "MILAM", "BORDEN" name places, not
    campuses)."""
    place_words = set(_words(city)) | set(_words(county))
    return frozenset(
        t
        for t in _words(name)
        if len(t) >= 3 and not t.isdigit() and t not in GENERIC_SITE_TOKENS and t not in place_words
    )


def name_key(name: Any) -> str:
    """A name without its data-centre words and legal forms, every other word and number kept in
    order; "" for a generic name ("DATA CENTER"), which never makes two permits one site."""
    words = [t for t in _words(name) if t not in NAME_KEY_GENERIC]
    return " ".join(words) if any(not t.isdigit() for t in words) else ""


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points, metres."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(h)))


@dataclass
class Permit:
    """One selected permit: the facility, its current permit version and how it was judged."""

    npdes_id: str
    facility: dict[str, str]
    permit: dict[str, str]
    construction_basis: str
    placement: str
    point: tuple[float, float] | None
    tokens: frozenset[str] = field(default_factory=frozenset)
    key: str = ""
    infrastructure: bool = False

    @property
    def state(self) -> str:
        return str(self.facility.get("STATE_CODE") or "").strip().upper()

    @property
    def master(self) -> str:
        return str(self.permit.get("MASTER_EXTERNAL_PERMIT_NMBR") or "").strip()

    def noi_date(self, master_dates: Mapping[str, frozenset[pd.Timestamp]]) -> pd.Timestamp | None:
        """The coverage's own date, the nearest the file comes to its notice of intent: the first of
        `ORIGINAL_ISSUE_DATE`, `ISSUE_DATE`, `EFFECTIVE_DATE` that is not one of its master general
        permit's own dates (Georgia stamps the permit's 08/01/2018 or 08/01/2023 on every coverage's
        original issue date and dates its effective date by the NOI); None when every one is the
        master's (Virginia's 07/01/2019 and 07/01/2024 coverages)."""
        own = master_dates.get(self.master, frozenset())
        for col in ("ORIGINAL_ISSUE_DATE", "ISSUE_DATE", "EFFECTIVE_DATE"):
            d = _date(self.permit.get(col))
            if d is not None and d not in own:
                return d
        return None

    def filing_date(self, master_dates: Mapping[str, frozenset[pd.Timestamp]]) -> pd.Timestamp | None:
        """The date of the coverage's latest filing: its termination, else its own date, else the
        effective or issue date it carries."""
        return (
            _date(self.permit.get("TERMINATION_DATE"))
            or self.noi_date(master_dates)
            or _date(self.permit.get("EFFECTIVE_DATE"))
            or _date(self.permit.get("ISSUE_DATE"))
        )


def _date(value: Any) -> pd.Timestamp | None:
    text = str(value or "").strip()
    if not text:
        return None
    ts = pd.to_datetime(text, format="%m/%d/%Y", errors="coerce")
    return None if pd.isna(ts) else ts


def site_link(a: Permit, b: Permit) -> str | None:
    """Why two selected permits of one state are one site (module docstring); None = they are not."""
    d = distance_m(a.point, b.point) if a.point is not None and b.point is not None else None
    if d is not None and d <= SITE_NEAR_M:
        return "near"
    if d is not None and d <= SITE_TOKEN_M and a.tokens & b.tokens:
        return "shared_token"
    if d is not None and d <= SITE_CODE_M and code_families(a.tokens) & code_families(b.tokens):
        return "code_family"
    if a.key and a.key == b.key and (d is None or d <= SITE_SAME_NAME_M):
        return "same_name"
    if d is None and a.tokens & b.tokens and _same_place(a, b):
        return "shared_token_place"
    return None


def _same_place(a: Permit, b: Permit) -> bool:
    """The same stated county (`COUNTY_CODE`) or city, for permits that cannot be compared by point."""
    for col in ("COUNTY_CODE", "CITY"):
        va = str(a.facility.get(col) or "").strip().upper()
        if va and va != "UNKNOWN" and va == str(b.facility.get(col) or "").strip().upper():
            return True
    return False


def _key_order(p: Permit) -> tuple[bool, int, str, str]:
    """A site's permits in key order: the data centre's own permits before works that serve it,
    then by the date the permit row carries first (`ORIGINAL_ISSUE_DATE`, else `ISSUE_DATE`, the
    master's date where the state stamps it), then by id. The first is the site's key and gives it
    its name."""
    issued = _date(p.permit.get("ORIGINAL_ISSUE_DATE")) or _date(p.permit.get("ISSUE_DATE"))
    if issued is None:
        return (p.infrastructure, 1, "", p.npdes_id)
    return (p.infrastructure, 0, issued.strftime("%Y%m%d"), p.npdes_id)


def group_sites(permits: Sequence[Permit]) -> list[tuple[list[Permit], list[str]]]:
    """Permits grouped into sites (union of `site_link` pairs within each state), each site's
    permits in key order (`_key_order`) with the link reasons that joined them."""
    parent = {p.npdes_id: p.npdes_id for p in permits}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    reasons: dict[str, list[str]] = {}
    by_state: dict[str, list[Permit]] = {}
    for p in sorted(permits, key=lambda q: q.npdes_id):
        by_state.setdefault(p.state, []).append(p)
    links: list[tuple[str, str, str]] = []
    for group in by_state.values():
        for a, b in itertools.combinations(group, 2):
            why = site_link(a, b)
            if why is not None:
                links.append((a.npdes_id, b.npdes_id, why))
                union(a.npdes_id, b.npdes_id)
    # A generic name joins the one site around it (`SITE_GENERIC_M`), never one of two.
    for group in by_state.values():
        for g in group:
            if g.tokens or g.point is None:
                continue
            gp = g.point
            around = {
                find(p.npdes_id)
                for p in group
                if p is not g and p.point is not None and distance_m(gp, p.point) <= SITE_GENERIC_M
            }
            if len(around) == 1 and find(g.npdes_id) not in around:
                other = around.pop()
                links.append((g.npdes_id, other, "generic_near"))
                union(g.npdes_id, other)
    for a_id, _b_id, why in links:
        reasons.setdefault(find(a_id), []).append(why)
    sites: dict[str, list[Permit]] = {}
    for p in permits:
        sites.setdefault(find(p.npdes_id), []).append(p)
    out = []
    for root, members in sites.items():
        members.sort(key=_key_order)
        out.append((members, sorted(set(reasons.get(root, [])))))
    out.sort(key=lambda s: s[0][0].npdes_id)
    return out


# ------------------------------------------------------------------ lifecycle
#: A site's state is its most recent filing's (`_site_row`); filings of one date are ordered by this.
STATE_RANK = {"built": 4, "under_construction": 3, "filed": 2, "unknown": 1}
#: Statuses that say nothing about construction: expired with no notice of termination (EXP), not
#: needed (NON), retired (RET). They are `unknown` by this rule, not by a status-map entry, because
#: `unknown` is the fallback no map may name (tests/test_lifecycle_definitions.py), and a fall-through
#: would read as an unmapped value (EXP alone is 12 % of the sites, past the DQ hold).
NOT_STATED_STATUSES = frozenset({"EXP", "NON", "RET"})
IN_FORCE_STATUSES = frozenset({"EFF", "ADC"})
#: Coverage in force (EFF, or ADC: carried past expiry while the general permit is reissued) whose
#: own expiration date passed more than this long before the file is not current evidence: states
#: reissue and re-cover open sites within months (Arizona re-covered its open sites under AZG2025),
#: so such a coverage is a record no one closed (Virginia's 2019-permit coverages, expired
#: 2024-06-30; a Hawaii school's, expired 2018-12-05). 35 of the 37 ADC coverages of 2026-10-18.
LAPSED_COVERAGE_DAYS = 365
#: A termination dated within this many days of the coverage's own expiration date is the coverage
#: lapsing (9 of the 133 terminations of 2026-10-18 fall on it or the day after), not a notice of
#: termination, so it does not prove the work ended.
EXPIRY_TERMINATION_DAYS = 1
#: A state reports construction coverage to ICIS when the file holds at least `REPORTING_MIN`
#: construction-coverage filings (an original issue, issue, effective or termination date) of that
#: state in the `REPORTING_WINDOW_DAYS` before the file, and at least `REPORTING_KEEP_SHARE` of its
#: filings in the year before that (`reporting`, counted by `fetch` over the whole permits member,
#: by the master permit's construction status). Where it does not, a coverage's status is frozen at
#: the last report, so coverage in force there is current evidence only when it is itself that
#: recent. On the 2026-10-18 file: California 22 filings against 12,183 the year before (it stopped
#: in October 2025), Vermont 23 against 170, Wyoming 33 against 163, Guam 31; the lowest share among
#: states with volume is Pennsylvania's 30 %. Georgia (6,196) and Virginia (944) report: docs/08
#: §4.1's "none since 2025" counted only coverages that carry their own SWC component, and theirs
#: carry it on the master permit.
REPORTING_WINDOW_DAYS = 365
REPORTING_MIN = 50
REPORTING_KEEP_SHARE = 0.25
#: Months of filing counts `fetch` keeps (the window is taken in `parse`, from the file's date).
REPORTING_KEEP_DAYS = 730
#: Date columns of a permit version that are the master general permit's own when a coverage carries
#: them (Georgia, Virginia and California stamp the permit's date on every coverage).
DATE_COLUMNS = ("ORIGINAL_ISSUE_DATE", "ISSUE_DATE", "EFFECTIVE_DATE", "EXPIRATION_DATE")


#: Reviewed permits whose sites this connector holds back (`held_permits`): a site that duplicates a
#: record another source already holds (the same campus, within 300 m) or that the resolver would join
#: to held records of distinct campuses. `pipeline/resolve.py` compares names and counties, not
#: points, so until it does, publishing these would show one campus twice or make two campuses one.
HELD_PATH = pathlib.Path(__file__).with_name("held.yaml")
HELD_REASONS = frozenset({"duplicate", "bridge"})


@lru_cache(maxsize=1)
def held_permits(path: pathlib.Path = HELD_PATH) -> dict[str, str]:
    """NPDES id -> why its site is held (`HELD_PATH`); a malformed entry is an error, never skipped."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for entry in doc.get("held") or []:
        npdes_id, reason = str(entry.get("npdes_id") or "").strip(), str(entry.get("reason") or "")
        if not npdes_id or reason not in HELD_REASONS or not entry.get("held_record"):
            raise ValueError(
                f"{path.name}: every entry needs an npdes_id, a reason in {sorted(HELD_REASONS)} and the "
                f"held record it concerns ({entry!r})"
            )
        out[npdes_id] = reason
    return out


@dataclass(frozen=True)
class Evidence:
    """What the file says about construction, as of its own date: the reference date, the filing
    counts by state (None: the snapshot predates them) and every master general permit's own dates."""

    as_of: pd.Timestamp
    reporting: Mapping[str, int] | None
    master_dates: Mapping[str, frozenset[pd.Timestamp]]
    #: The same counts for the year before `reporting`'s.
    reporting_before: Mapping[str, int] = field(default_factory=dict)

    def reports(self, state: str) -> bool:
        if self.reporting is None:
            return True
        last, before = self.reporting.get(state, 0), self.reporting_before.get(state, 0)
        return last >= REPORTING_MIN and last >= REPORTING_KEEP_SHARE * before


#: Coverage in force filed within this long before a site's latest termination is the site's
#: continuing construction: a new operator's NOI on a handover, a renewal, the next phase.
HANDOVER_DAYS = 365


def site_decision(states: Sequence[str], filed: Sequence[pd.Timestamp | None], ids: Sequence[str]) -> int:
    """Which permit decides a site's state (index into the site's permits). The most recent filing
    decides (a termination by its date, any other coverage by its own date), one date's filings
    ordered by `STATE_RANK`; except that coverage in force (an `under_construction` permit, which
    `_permit_state` grants only to coverage that has not lapsed in a state that reports) filed no
    more than `HANDOVER_DAYS` before the latest termination, or after it, keeps the site under
    construction. Measured on the 2026-10-18 file: of the 10 sites the two rules split, the
    exception is right on 7 (Freebird and Eagle Mountain: a termination and a new NOI on one day;
    Lancaster LAN01: an NOI 11 days before the older coverage ended; Core Scientific Denton's
    expansion; EdgeCore's North Campus), and the most recent filing on 2 (Westpointe, Aligned
    Memory Tech: an NOI years older than the termination)."""
    ended = [d for s, d in zip(states, filed, strict=True) if s == "built" and d is not None]
    latest_end = max(ended) if ended else None
    order = range(len(states))
    live = [
        i
        for i in order
        if states[i] == "under_construction"
        and (
            latest_end is None
            or (filed[i] is not None and filed[i] >= latest_end - pd.Timedelta(days=HANDOVER_DAYS))
        )
    ]
    pool = live or list(order)
    return max(
        pool,
        key=lambda i: (
            filed[i] if filed[i] is not None else pd.Timestamp.min,
            STATE_RANK.get(states[i], 0),
            ids[i],
        ),
    )


# ------------------------------------------------------------------ zip members as streams
FETCH_VERSION = 2  # 2: master dates, filing counts by state and the file's own date stored
DOWNLOAD_CHUNK = 1 << 20
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)")


class MemberStream(io.RawIOBase):
    """One zip member inflated from a file positioned at the member's local header, as a readable
    byte stream; the CRC-32 and size are checked when the stream ends (`ParseError` on a mismatch)."""

    def __init__(self, fh: IO[bytes], member: ZipMember) -> None:
        super().__init__()
        if member.method not in (0, 8):
            raise ParseError(f"{member.name}: unsupported zip compression method {member.method}")
        head = fh.read(30)
        if head[:4] != b"PK\x03\x04":
            raise ParseError(f"{member.name}: local file header signature mismatch")
        name_len, extra_len = struct.unpack("<HH", head[26:30])
        fh.read(name_len + extra_len)
        self._fh = fh
        self._member = member
        self._left = member.compressed_size
        self._inflate = zlib.decompressobj(-15) if member.method == 8 else None
        self._buf = b""
        self._pos = 0
        self._done = False
        self.crc = 0
        self.size = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        while self._pos >= len(self._buf):
            if self._done:
                return 0
            self._fill()
        n = min(len(b), len(self._buf) - self._pos)
        b[:n] = self._buf[self._pos : self._pos + n]
        self._pos += n
        return n

    def _fill(self) -> None:
        chunk = self._fh.read(min(DOWNLOAD_CHUNK, self._left)) if self._left > 0 else b""
        if self._left > 0 and not chunk:
            raise ParseError(f"{self._member.name}: ranged read truncated ({self._left} bytes short)")
        self._left -= len(chunk)
        if chunk:
            out = self._inflate.decompress(chunk) if self._inflate is not None else chunk
        else:
            out = self._inflate.flush() if self._inflate is not None else b""
            self._done = True
        self.crc = zlib.crc32(out, self.crc)
        self.size += len(out)
        self._buf, self._pos = out, 0
        if self._done and (self.crc != self._member.crc or self.size != self._member.size):
            raise ParseError(f"{self._member.name}: CRC or size mismatch after ranged read")


def csv_rows(stream: IO[bytes] | io.RawIOBase) -> Iterator[list[str]]:
    """CSV records of a member stream (a raw `MemberStream` is buffered first)."""
    buffered: IO[bytes] = (
        io.BufferedReader(stream, buffer_size=DOWNLOAD_CHUNK) if isinstance(stream, io.RawIOBase) else stream
    )
    return csv.reader(io.TextIOWrapper(buffered, encoding="utf-8", errors="replace", newline=""))


def _keep_index(header: list[str], wanted: Sequence[str], required: Sequence[str], member: str) -> list[int]:
    missing = [c for c in required if c not in header]
    if missing:
        raise ParseError(f"{member} lacks {missing}: {header}")
    return [header.index(c) if c in header else -1 for c in wanted]


def _pick(rec: list[str], idx: list[int]) -> list[str]:
    return [rec[i] if 0 <= i < len(rec) else "" for i in idx]


def _version(value: str) -> int:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else 1_000_000


def _month(value: str) -> str:
    """ "MM/DD/YYYY" as "YYYYMM"; "" for anything else."""
    text = str(value or "").strip()
    return f"{text[6:10]}{text[0:2]}" if len(text) == 10 and text[2] == "/" and text[5] == "/" else ""


def file_date(last_modified: Any) -> dt.date:
    """The zip's own date (its `Last-Modified`), the date every lifecycle rule is judged at; today
    when the server sent none."""
    try:
        return email.utils.parsedate_to_datetime(str(last_modified)).astimezone(dt.UTC).date()
    except (TypeError, ValueError, IndexError):
        return utcnow().date()


def _strip_columns(columns: list[str]) -> list[int]:
    return [i for i, c in enumerate(columns) if c not in PERSONAL_COLUMNS and not CONTACT_COLUMN.search(c)]


def cross_refs(row: Mapping[str, Any]) -> str:
    """`npdes:<id>` for every permit of the site, then `frs:<id>` for every FRS id they carry. FRS is
    a facility-registry namespace (`pipeline.resolve.SHARED_ID_NAMESPACES`): the same id on an
    ICIS-Air record is that facility (D3), and a different one refuses the fuzzy pair, which is what
    keeps one operator's other campuses in the state (Vantage GA1 and GA2, 12.7 km apart) from
    joining through a construction site's name."""
    permits = list(row.get("permits") or [])
    frs = sorted({str(p.get("FACILITY_UIN") or "").strip() for p in permits} - {""})
    return "|".join([*(f"npdes:{p['NPDES_ID']}" for p in permits), *(f"frs:{u}" for u in frs)])


# ------------------------------------------------------------------ connector
class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.epa.echo.icis_npdes_construction"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = True  # echo.epa.gov robots.txt allows /files/ (Crawl-delay 10)
    status_key: ClassVar[str] = "epa_icis_npdes_construction"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    dq_required_fields: ClassVar[tuple[str, ...]] = ("name_canonical", "state")
    personal_data_columns: ClassVar[tuple[str, ...]] = (PERMITTEE_COLUMN,)
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "NPDES_ID",
        "FACILITY_NAME",
        "STATE_CODE",
        "COUNTY_CODE",
        "GEOCODE_LATITUDE",
        "GEOCODE_LONGITUDE",
        "EXTERNAL_PERMIT_NMBR",
        "PERMIT_STATUS_CODE",
        "TERMINATION_DATE",
        "MASTER_EXTERNAL_PERMIT_NMBR",
        "COMPONENT_TYPE_CODE",
    )

    # ------------------------------------------------------------------ fetch
    def _get(self, url: str, headers: dict[str, str], allowed: tuple[int, ...]) -> Any:
        r = self.http.get(url, honour_robots=self.honour_robots, headers=headers, timeout=900, stream=True)
        if r.status_code not in allowed:
            close = getattr(r, "close", None)
            if close is not None:
                close()
            raise ConnectorError(f"GET {url} -> HTTP {r.status_code}")
        return r

    @staticmethod
    def _spool(resp: Any) -> tuple[IO[bytes], int]:
        """The response body streamed to a temporary file in `DOWNLOAD_CHUNK` pieces; the caller closes it."""
        fh = tempfile.TemporaryFile()
        try:
            size = 0
            for chunk in resp.iter_content(DOWNLOAD_CHUNK):
                fh.write(chunk)
                size += len(chunk)
            expected = str(resp.headers.get("Content-Length") or "")
            if expected.isdigit() and not resp.headers.get("Content-Encoding") and int(expected) != size:
                raise ConnectorError(f"GET {ZIP_URL} truncated: {size} of {expected} bytes")
            fh.seek(0)
        except BaseException:
            fh.close()
            raise
        finally:
            close = getattr(resp, "close", None)
            if close is not None:
                close()
        return fh, size

    def _reusable_previous(self) -> tuple[bytes, dict[str, Any]] | None:
        """(bytes, meta) of the stored snapshot when this fetch code made it and the zip's validators
        are recorded; None means fetch unconditionally."""
        prev = self.previous
        if prev is None:
            return None
        meta = prev.meta
        if meta.get("fetch_version") != FETCH_VERSION or not conditional_headers(meta.get("zip")):
            return None
        try:
            content = prev.content()
            doc = json.loads(content)
            if not all(isinstance(doc.get(k), dict) for k in ("facilities", "permits", "components")):
                return None
        except Exception as e:  # a stored object we cannot use costs one full download, no more
            log.warning("previous snapshot not reusable: %r", e, extra={"source_id": self.source_id})
            return None
        return content, meta

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        before = self.http.requests_made
        prev = self._reusable_previous()
        cond = conditional_headers(prev[1].get("zip")) if prev else {}
        tail = self._get(
            ZIP_URL, {"Range": f"bytes=-{TAIL_BYTES}", **cond}, (200, 206, 304) if cond else (200, 206)
        )
        if tail.status_code == 304 and prev is not None:
            tail.close()
            return self._unchanged(prev, tail, t0, before)
        zmeta: dict[str, Any] = {
            "url": ZIP_URL,
            "last_modified": tail.headers.get("Last-Modified"),
            "etag": tail.headers.get("ETag"),
            "not_modified": False,
        }
        members_meta: dict[str, dict[str, Any]] = {}
        with contextlib.ExitStack() as stack:
            if tail.status_code == 200:  # the server ignored Range: the whole zip, read by zipfile
                fh, size = self._spool(tail)
                stack.callback(fh.close)
                whole = stack.enter_context(zipfile.ZipFile(fh))
                zmeta.update(ranged=False, zip_bytes=size, bytes_fetched=size)

                def open_member(name: str) -> IO[bytes] | io.RawIOBase:
                    members_meta[name] = {"size": whole.getinfo(name).file_size}
                    return whole.open(name)

            else:
                content = tail.content
                tail.close()
                m = _CONTENT_RANGE.search(str(tail.headers.get("Content-Range") or ""))
                if not m:
                    raise ConnectorError(f"GET {ZIP_URL} answered 206 without a Content-Range header")
                total = int(m.group(3))
                directory = {z.name: z for z in central_directory(content, total)}
                zmeta.update(ranged=True, zip_bytes=total, bytes_fetched=len(content))

                def open_member(name: str) -> IO[bytes] | io.RawIOBase:
                    if name not in directory:
                        raise ParseError(f"{ZIP_URL} has no member {name}; members: {sorted(directory)}")
                    mem = directory[name]
                    end = min(
                        total - 1,
                        mem.header_offset + 30 + len(name) + LOCAL_HEADER_SLACK + mem.compressed_size,
                    )
                    body = self._get(ZIP_URL, {"Range": f"bytes={mem.header_offset}-{end}"}, (206,))
                    if body.headers.get("Last-Modified") != zmeta["last_modified"]:
                        body.close()
                        raise ConnectorError(f"{ZIP_URL} changed between ranged reads; retry on the next run")
                    part, size = self._spool(body)
                    stack.callback(part.close)
                    zmeta["bytes_fetched"] += size
                    members_meta[name] = {"compressed": mem.compressed_size, "size": mem.size}
                    return MemberStream(part, mem)

            doc, totals = self._read_members(open_member, zmeta.get("last_modified"))
        for name, n in totals.items():
            members_meta.setdefault(name, {})["rows_total"] = n
        return RawSnapshot(
            content=json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            content_type="application/json",
            url=ZIP_URL,
            retrieved_at=utcnow(),
            http_status=200,
            ext="json",
            headers={"Last-Modified": str(zmeta.get("last_modified"))},
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=self.http.requests_made - before,
            meta={
                "fetch_version": FETCH_VERSION,
                "upstream": "changed",
                "zip": zmeta,
                "members": members_meta,
                "candidates": len(doc["facilities"]["rows"]),
            },
        )

    def _read_members(
        self, open_member: Callable[[str], IO[bytes] | io.RawIOBase], last_modified: Any = None
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """The three tables cut to the candidates: facilities first (the name pre-filter), then their
        current permit versions and every master's title and dates, then the components of both;
        and, over the whole permits member, construction-coverage filings by state and month for
        the `REPORTING_KEEP_DAYS` before the file (`REPORTING_COLUMNS`). A coverage counts toward
        its state's month when its master is construction stormwater (the master's `SWC`
        component, its title, or a coverage under it carrying `SWC`), by the latest of its
        original issue, issue, effective and termination dates."""
        as_of = file_date(last_modified)
        cutoff = (as_of - dt.timedelta(days=REPORTING_KEEP_DAYS)).strftime("%Y%m")
        totals: dict[str, int] = {}
        rows = csv_rows(open_member(FACILITIES_MEMBER))
        idx = _keep_index(next(rows), FACILITY_COLUMNS, REQUIRED_FACILITY, FACILITIES_MEMBER)
        facilities: dict[str, list[str]] = {}
        n = 0
        for rec in rows:
            n += 1
            row = _pick(rec, idx)
            if is_candidate({"FACILITY_NAME": row[FACILITY_COLUMNS.index("FACILITY_NAME")]}):
                facilities.setdefault(row[0].strip(), row)
        totals[FACILITIES_MEMBER] = n

        rows = csv_rows(open_member(PERMITS_MEMBER))
        header = next(rows)
        idx = _keep_index(header, PERMIT_COLUMNS, REQUIRED_PERMIT, PERMITS_MEMBER)
        title_i = header.index(PERMITTEE_COLUMN) if PERMITTEE_COLUMN in header else -1
        type_i, ver_i = PERMIT_COLUMNS.index("PERMIT_TYPE_CODE"), PERMIT_COLUMNS.index("VERSION_NMBR")
        master_i = PERMIT_COLUMNS.index("MASTER_EXTERNAL_PERMIT_NMBR")
        status_i = PERMIT_COLUMNS.index("PERMIT_STATUS_CODE")
        filing_is = [
            PERMIT_COLUMNS.index(c)
            for c in ("ORIGINAL_ISSUE_DATE", "ISSUE_DATE", "EFFECTIVE_DATE", "TERMINATION_DATE")
        ]
        master_date_is = [PERMIT_COLUMNS.index(c) for c in DATE_COLUMNS]
        permits: dict[str, list[str]] = {}
        masters: dict[str, tuple[int, list[str]]] = {}
        master_dates: dict[str, set[str]] = {}
        filings: collections.Counter[tuple[str, str, str]] = collections.Counter()
        recent_master: dict[str, str] = {}
        n = 0
        for rec in rows:
            n += 1
            row = _pick(rec, idx)
            pid = row[0].strip()
            if pid in facilities and (
                pid not in permits or _version(row[ver_i]) < _version(permits[pid][ver_i])
            ):
                permits[pid] = row
            master = row[master_i].strip()
            if master:
                month = max(_month(row[i]) for i in filing_is)
                if month >= cutoff:
                    filings[(pid[:2].upper(), master, month)] += 1
                    recent_master[pid] = master
            if row[type_i].strip() == MASTER_PERMIT_TYPE:
                # A master general permit's PERMIT_NAME is its title, not a permittee.
                title = rec[title_i].strip() if 0 <= title_i < len(rec) else ""
                dates = (row[i].strip() for i in master_date_is)
                master_dates.setdefault(pid, set()).update(d for d in dates if d)
                if pid not in masters or _version(row[ver_i]) < masters[pid][0]:
                    masters[pid] = (_version(row[ver_i]), [pid, title, row[status_i]])
        totals[PERMITS_MEMBER] = n
        wanted_masters = {r[master_i].strip() for r in permits.values()} - {""}
        filing_masters = {m for _, m, _ in filings}

        rows = csv_rows(open_member(COMPONENTS_MEMBER))
        idx = _keep_index(next(rows), COMPONENT_COLUMNS, REQUIRED_COMPONENT, COMPONENTS_MEMBER)
        wanted = set(facilities) | wanted_masters
        components: set[tuple[str, str]] = set()
        construction_masters: set[str] = set()
        n = 0
        for rec in rows:
            n += 1
            row = _pick(rec, idx)
            pid, code = row[0].strip(), row[1].strip()
            if pid in wanted:
                components.add((pid, code))
            if code == CONSTRUCTION_COMPONENT:
                if pid in filing_masters:
                    construction_masters.add(pid)
                elif pid in recent_master:
                    construction_masters.add(recent_master[pid])
        totals[COMPONENTS_MEMBER] = n
        construction_masters |= {
            m for m in filing_masters if m in masters and is_construction_title(masters[m][1][1])
        }
        by_state: collections.Counter[tuple[str, str]] = collections.Counter()
        for (state, master, month), count in filings.items():
            if master in construction_masters:
                by_state[(state, month)] += count

        def master_row(k: str, v: list[str]) -> list[str]:
            return [*v, "|".join(sorted(master_dates.get(k, ())))]

        doc = {
            "file": {"last_modified": str(last_modified or ""), "as_of": as_of.isoformat()},
            "facilities": {"columns": list(FACILITY_COLUMNS), "rows": sorted(facilities.values())},
            "permits": {"columns": list(PERMIT_COLUMNS), "rows": sorted(permits.values())},
            "masters": {
                "columns": list(MASTER_COLUMNS),
                "rows": sorted(master_row(k, v) for k, (_, v) in masters.items() if k in wanted_masters),
            },
            "components": {"columns": list(COMPONENT_COLUMNS), "rows": sorted(list(c) for c in components)},
            "reporting": {
                "columns": list(REPORTING_COLUMNS),
                "rows": [[s, m, str(c)] for (s, m), c in sorted(by_state.items())],
            },
        }
        return doc, totals

    def _unchanged(
        self, prev: tuple[bytes, dict[str, Any]], resp: Any, t0: float, before: int
    ) -> RawSnapshot:
        """The zip answered 304: the stored bytes, verbatim, so the runner's SHA comparison records
        the run `unchanged` (docs/20 §3.2). Apache's 304 repeats the ETag but not Last-Modified, so
        the previous value is carried for the next run's `If-Modified-Since`."""
        content, meta = prev
        zmeta = dict(meta.get("zip") or {})
        zmeta.update(
            last_modified=resp.headers.get("Last-Modified") or zmeta.get("last_modified"),
            etag=resp.headers.get("ETag") or zmeta.get("etag"),
            not_modified=True,
            bytes_fetched=0,
        )
        return RawSnapshot(
            content=content,
            content_type="application/json",
            url=ZIP_URL,
            retrieved_at=utcnow(),
            http_status=304,
            ext="json",
            headers={"Last-Modified": str(zmeta.get("last_modified"))},
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=self.http.requests_made - before,
            meta={
                "fetch_version": FETCH_VERSION,
                "upstream": "unchanged",
                "zip": zmeta,
                "members": meta.get("members") or {},
                "candidates": meta.get("candidates"),
            },
        )

    def redact(self, content: bytes) -> bytes:
        """Strip the permittee and any contact-like column from every table; bytes untouched when
        there is none (the fetch never stores one)."""
        try:
            doc = json.loads(content)
        except json.JSONDecodeError:
            return content
        if not isinstance(doc, dict):
            return content
        stripped = False
        for table in doc.values():
            if not isinstance(table, dict):
                continue
            cols = list(table.get("columns") or [])
            keep = _strip_columns(cols)
            if len(keep) != len(cols):
                table["columns"] = [cols[i] for i in keep]
                table["rows"] = [[r[i] for i in keep] for r in table.get("rows") or []]
                stripped = True
        if not stripped:
            return content
        return json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")

    # ------------------------------------------------------------------ parse
    @staticmethod
    def _table(doc: dict[str, Any], key: str, required: Sequence[str]) -> list[dict[str, str]]:
        table = doc.get(key)
        if not isinstance(table, dict):
            raise ParseError(f"ICIS-NPDES snapshot lacks its {key} table")
        cols = [str(c) for c in table.get("columns") or []]
        missing = [c for c in required if c not in cols]
        if missing:
            raise ParseError(f"ICIS-NPDES {key} table lacks {missing}")
        keep = _strip_columns(cols)  # the permittee never reaches a parsed row, whatever was stored
        return [{cols[i]: str(rec[i] or "") for i in keep if i < len(rec)} for rec in table.get("rows") or []]

    @staticmethod
    def _evidence(doc: dict[str, Any], raw: RawSnapshot, masters: Sequence[Mapping[str, str]]) -> Evidence:
        """The file's own date (stored with the tables, so the same bytes always parse alike; the
        retrieval date for a snapshot that predates it), the construction-coverage filings of each
        state in the `REPORTING_WINDOW_DAYS` before it, and every master's own dates."""
        file_meta = doc.get("file") if isinstance(doc.get("file"), dict) else {}
        as_of_text = str((file_meta or {}).get("as_of") or "")
        try:
            as_of = pd.Timestamp(dt.date.fromisoformat(as_of_text))
        except ValueError:
            as_of = pd.Timestamp(raw.retrieved_at.astimezone(dt.UTC).date())
        reporting: dict[str, int] | None = None
        before: dict[str, int] = {}
        if isinstance(doc.get("reporting"), dict):
            # Months after the file's own month do not count, nor its month a year back twice.
            last_month = as_of.strftime("%Y%m")
            since = (as_of - pd.Timedelta(days=REPORTING_WINDOW_DAYS)).strftime("%Y%m")
            first = (as_of - pd.Timedelta(days=2 * REPORTING_WINDOW_DAYS)).strftime("%Y%m")
            reporting = {}
            for r in Connector._table(doc, "reporting", REPORTING_COLUMNS):
                count = int(r["FILINGS"]) if r["FILINGS"].strip().isdigit() else 0
                state, month = r["STATE_CODE"], r["MONTH"]
                if since < month <= last_month:
                    reporting[state] = reporting.get(state, 0) + count
                elif first < month <= since:
                    before[state] = before.get(state, 0) + count
        master_dates: dict[str, frozenset[pd.Timestamp]] = {}
        for m in masters:
            parsed = (_date(d) for d in str(m.get("MASTER_DATES") or "").split("|"))
            master_dates[m["MASTER_EXTERNAL_PERMIT_NMBR"].strip()] = frozenset(
                d for d in parsed if d is not None
            )
        return Evidence(as_of=as_of, reporting=reporting, master_dates=master_dates, reporting_before=before)

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        try:
            doc = json.loads(raw.content)
        except json.JSONDecodeError as e:
            raise ParseError(f"ICIS-NPDES snapshot is not JSON: {e}") from e
        if not isinstance(doc, dict):
            raise ParseError("ICIS-NPDES snapshot is not a table document")
        facilities = self._table(doc, "facilities", REQUIRED_FACILITY)
        permits = {r["EXTERNAL_PERMIT_NMBR"].strip(): r for r in self._table(doc, "permits", REQUIRED_PERMIT)}
        master_rows = (
            self._table(doc, "masters", ("MASTER_EXTERNAL_PERMIT_NMBR",)) if "masters" in doc else []
        )
        masters = {r["MASTER_EXTERNAL_PERMIT_NMBR"].strip(): r.get("MASTER_TITLE", "") for r in master_rows}
        comps: dict[str, set[str]] = {}
        for r in self._table(doc, "components", REQUIRED_COMPONENT):
            comps.setdefault(r["EXTERNAL_PERMIT_NMBR"].strip(), set()).add(r["COMPONENT_TYPE_CODE"].strip())
        components = {k: frozenset(v) for k, v in comps.items()}
        evidence = self._evidence(doc, raw, master_rows)

        excluded: dict[str, int] = {}
        bases: dict[str, int] = {}
        placements: dict[str, int] = {}
        selected: list[Permit] = []
        for fac in facilities:
            pid = fac["NPDES_ID"].strip()
            name = fac.get("FACILITY_NAME", "")
            state = fac.get("STATE_CODE", "").strip().upper()
            permit = permits.get(pid)
            basis = None
            if permit is not None:
                master = permit.get("MASTER_EXTERNAL_PERMIT_NMBR", "").strip()
                basis = construction_basis(pid, master, components, masters)
            reason = (
                "name_not_selected"
                if select_basis(name) is None
                else "crypto_mining"
                if is_crypto_mine(name, state)
                else "personal_name"
                if is_personal_name(name)
                else "no_permit"
                if permit is None
                else "not_construction"
                if basis is None
                else None
            )
            if reason is not None or permit is None or basis is None:
                excluded[reason or "no_permit"] = excluded.get(reason or "no_permit", 0) + 1
                continue
            placement, lat, lon = placement_for(
                fac.get("GEOCODE_LATITUDE"), fac.get("GEOCODE_LONGITUDE"), state
            )
            bases[basis] = bases.get(basis, 0) + 1
            placements[placement] = placements.get(placement, 0) + 1
            selected.append(
                Permit(
                    npdes_id=pid,
                    facility=fac,
                    permit=permit,
                    construction_basis=basis,
                    placement=placement,
                    point=(lat, lon) if lat is not None and lon is not None else None,
                    tokens=site_tokens(name, fac.get("CITY"), county_from_code(fac.get("COUNTY_CODE"))),
                    key=name_key(name),
                    infrastructure=is_infrastructure(name),
                )
            )
        rows: list[dict[str, Any]] = []
        infrastructure_only: list[str] = []
        held_out: list[dict[str, Any]] = []
        held = held_permits()
        for members, links in group_sites(selected):
            if all(p.infrastructure for p in members):
                # Works that serve a data centre (a substation, a feeder, offsite roads), not one.
                infrastructure_only.append(members[0].npdes_id)
                continue
            hits = sorted(p.npdes_id for p in members if p.npdes_id in held)
            if hits:
                # A held record's duplicate or a bridge between held records (`HELD_PATH`).
                held_out.append({"site_key": members[0].npdes_id, "permits": hits, "reason": held[hits[0]]})
                continue
            rows.append(self._site_row(members, links, evidence))
        raw.meta["infrastructure_only_sites"] = infrastructure_only
        raw.meta["held_sites"] = held_out
        raw.meta["permits_selected"] = len(selected)
        raw.meta["rows_selected"] = len(rows)
        raw.meta["excluded"] = dict(sorted(excluded.items()))
        raw.meta["construction_basis"] = dict(sorted(bases.items()))
        raw.meta["placement"] = dict(sorted(placements.items()))
        raw.meta["sites_with_several_permits"] = sum(1 for r in rows if len(r["permits"]) > 1)
        raw.meta["as_of"] = evidence.as_of.date().isoformat()
        raw.meta["states_not_reporting"] = (
            None
            if evidence.reporting is None
            else sorted({r["STATE_CODE"] for r in rows if not evidence.reports(r["STATE_CODE"])})
        )
        raw.meta["source_header"] = sorted(
            {str(c) for t in doc.values() if isinstance(t, dict) for c in t.get("columns") or []}
            - PERSONAL_COLUMNS
        )
        return rows

    def _permit_state(self, p: Permit, evidence: Evidence) -> tuple[str, str]:
        """One coverage's lifecycle state and the rule that set it, from what the filing proves as of
        the file's date (module docstring, status_map.yaml)."""
        code = p.permit.get("PERMIT_STATUS_CODE", "").strip()
        ended = _date(p.permit.get("TERMINATION_DATE"))
        expires = _date(p.permit.get("EXPIRATION_DATE"))
        at_expiry = (
            ended is not None
            and expires is not None
            and abs((ended - expires).days) <= EXPIRY_TERMINATION_DAYS
        )
        if at_expiry:
            return "unknown", f"{self.status_key}.terminated_at_expiry"
        if code in NOT_STATED_STATUSES and ended is None:
            return "unknown", f"{self.status_key}.not_stated"
        if code in IN_FORCE_STATUSES and ended is None:
            if expires is not None and (evidence.as_of - expires).days > LAPSED_COVERAGE_DAYS:
                return "unknown", f"{self.status_key}.lapsed_coverage"
            own = p.noi_date(evidence.master_dates) or _date(p.permit.get("EFFECTIVE_DATE"))
            recent = own is not None and (evidence.as_of - own).days <= REPORTING_WINDOW_DAYS
            if not evidence.reports(p.state) and not recent:
                return "unknown", f"{self.status_key}.state_not_reporting"
        return harmonise_status(
            self.status_key,
            {"status_raw": code, "terminated": "yes" if ended is not None else "no"},
            self.status_map,
        )

    def _site_row(self, members: list[Permit], links: list[str], evidence: Evidence) -> dict[str, Any]:
        key = members[0]
        states = [self._permit_state(p, evidence) for p in members]
        filed = [p.filing_date(evidence.master_dates) for p in members]
        decide = site_decision([s for s, _ in states], filed, [p.npdes_id for p in members])
        own_dates = [p.noi_date(evidence.master_dates) for p in members if not p.infrastructure]
        stated = [d for d in own_dates if d is not None]
        point = next((p.point for p in members if p.point is not None), None)
        placed = next((p for p in members if p.point is not None), None)
        permits_out = []
        for p, (state, rule), when in zip(members, states, filed, strict=True):
            point_county = county_for_point(p.point[1], p.point[0]) if p.point is not None else None
            code_county = county_from_code(p.facility.get("COUNTY_CODE"))
            noi = p.noi_date(evidence.master_dates)
            entry: dict[str, Any] = {
                **{
                    c: p.facility.get(c, "")
                    for c in FACILITY_COLUMNS
                    if c not in ("GEOCODE_LATITUDE", "GEOCODE_LONGITUDE")
                },
                **{c: p.permit.get(c, "") for c in PERMIT_COLUMNS if c != "EXTERNAL_PERMIT_NMBR"},
                "construction_basis": p.construction_basis,
                "infrastructure": p.infrastructure,
                "placement": p.placement,
                "lifecycle_state": state,
                "status_rule": rule,
                "coverage_date": noi.date().isoformat() if noi is not None else None,
                "filing_date": when.date().isoformat() if when is not None else None,
                "county_code_matches_point": (
                    None if point_county is None or code_county is None else point_county == code_county
                ),
            }
            if p.point is not None:  # a coarse or misplaced coordinate never reaches `raw`
                entry["Latitude"], entry["Longitude"] = p.point
            permits_out.append(entry)
        row: dict[str, Any] = {
            "site_key": key.npdes_id,
            "FACILITY_NAME": key.facility.get("FACILITY_NAME", ""),
            "FACILITY_UIN": next(
                (p.facility.get("FACILITY_UIN", "") for p in members if p.facility.get("FACILITY_UIN")), ""
            ),
            "STATE_CODE": key.state,
            "CITY": key.facility.get("CITY", ""),
            "COUNTY_CODE": next(
                (p.facility.get("COUNTY_CODE", "") for p in members if p.facility.get("COUNTY_CODE")), ""
            ),
            "select_basis": "name",
            "status_permit": members[decide].npdes_id,
            "PERMIT_STATUS_CODE": members[decide].permit.get("PERMIT_STATUS_CODE", "").strip(),
            "lifecycle_state": states[decide][0],
            "status_rule": states[decide][1],
            "status_as_of": evidence.as_of.date().isoformat(),
            "first_covered": min(stated).date().isoformat() if stated else None,
            "site_links": links,
            "placement": placed.placement if placed is not None else members[0].placement,
            "permits": permits_out,
        }
        if point is not None:
            row["Latitude"], row["Longitude"] = point
        return row

    # ------------------------------------------------------------------ normalize
    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        counties: list[str | None] = []
        for r in rows:
            c = None
            if r.get("Latitude") is not None:
                c = county_for_point(float(r["Longitude"]), float(r["Latitude"]))
            counties.append(c or county_from_code(r.get("COUNTY_CODE")))
        ids = [str(r["site_key"]) for r in rows]
        regs = [str(r.get("FACILITY_UIN") or "").strip() for r in rows]
        df = pd.DataFrame(
            {
                "source_record_id": ids,
                "source_url": [DFR_URL.format(registry_id=g) if g else ZIP_URL for g in regs],
                "kind": "load",
                "name_canonical": [r.get("FACILITY_NAME") or None for r in rows],
                "name_norm": [norm_name(r.get("FACILITY_NAME")) for r in rows],
                "sponsor_name": None,
                "sponsor_norm": None,
                "technology": "load",
                "technology_raw": "Data Center",
                "capacity_mw": pd.array([None] * len(rows), dtype="Float64"),
                "storage_mwh": pd.array([None] * len(rows), dtype="Float64"),
                "iso": None,
                "state": [str(r.get("STATE_CODE") or "").strip().upper() or None for r in rows],
                "county": counties,
                "county_norm": [norm_county(c) for c in counties],
                "lifecycle_state": [r["lifecycle_state"] for r in rows],
                "status_raw": [r.get("PERMIT_STATUS_CODE") or None for r in rows],
                "status_rule": [r["status_rule"] for r in rows],
                "status_conflict": False,
                "queue_date": pd.NaT,
                "proposed_cod": pd.NaT,
                "queue_id": None,
                "eia_plant_id": None,
                "eia_generator_id": None,
                "cross_refs": [cross_refs(r) for r in rows],
            },
            index=range(len(rows)),
        )
        return self.finalize(df, rows, raw)
