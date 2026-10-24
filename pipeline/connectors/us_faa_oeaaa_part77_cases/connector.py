"""us.faa.oeaaa.part77_cases — FAA obstruction evaluation (OE/AAA) Part 77 cases at data-centre sites.

What the source is (measured 2026-10-23, docs/08 §4.1). 14 CFR Part 77 requires notice to the FAA
(Form 7460-1) for any structure, construction cranes included, taller than 200 ft or inside the
slopes around a public airport (https://www.ecfr.gov/current/title-14/chapter-I/subchapter-E/part-77).
Data-centre builds use tall cranes and often sit near airports (Dulles, Phoenix, Dallas), so many
file. The FAA publishes every case it holds as one CSV per region per year on its OE/AAA site
("Download Archives"): `Part77<REGION><YEAR>List.gzip` from `/oeaaa/oe3a-external-api/
downloadArchives.do`, the URL the site's own download controller builds. The page says the archives
are offered "so that it can easily be taken into the public and private sectors through your own
applications and pages", are "usually generated once a week on Saturday evenings", and end with a
"Data as of" line. Read 2026-10-23 (data as of 2026-10-17), the nine 2026 archives held 116,486 cases
entered 2026-01-01 to 2026-10-16, the nine 2025 archives 124,474. Columns (44): study number (ASN),
status, determination, entered, received, completion and expiration dates, a point with an FAA
survey-accuracy code, structure name, city, county, state, proposal and location descriptions,
notice type, duration and work schedule, date built, structure type, heights, and the sponsor's and
representative's names.

Fetch (politeness). One GET per archive for the current and previous calendar year (18 files, about
260 MB decoded, served gzip-encoded), then one GET of the site's furlough flag (below), at the
manifest's 0.1 rps; `oeaaa.faa.gov/robots.txt` answers 404 (no rule) and is honoured anyway. The
server sends no `ETag` or `Last-Modified` and answers 200 to a conditional request, so every run
downloads the files; the unchanged short-circuit works on the stored snapshot instead (below). An
archive the FAA has not published yet (the new year's file in early January) answers 200 with an
empty body and is recorded, not failed; an archive that answers HTML fails the run. The interactive
search pages are never fetched. The home page's template carries a shutdown banner ("Due to the
government shutdown the Federal Aviation Administration (FAA) is unable to accept any new notices
under 14 CFR Part 77"; "Pending a formal determination, all submittals should be presumed to be a
hazard to air navigation until advised otherwise") that the page shows only while its furlough flag
is on; the page's own script reads the flag from `FURLOUGH_FLAG_URL` (on 2026-10-24 it answered
`{"enableOegFurloughPublicMsg":false}`). So the flag is read, and the home page only when the flag is
on; the banner's text is then kept in the run record as a source-health note
(`RawSnapshot.meta["site_notice"]`), never a failure: a shutdown stops new filings, not the archives.

Schedule. Weekly (`cadence: weekly` in the manifest row) since `pipeline/resolve.py` refuses to merge
two load records whose exact points lie more than 2 km apart (rule L, docs/22 §28). Until then the row
had no cadence: a replay of these records against the held loads accepted fuzzy merges of points up
to 32 km apart (docs/08 §4.1 "Built"), and the scheduler fetches every row with a cadence.

The stored snapshot is the candidate table, not the 260 MB of upstream bytes (the ICIS-Air
connector makes the same choice): `fetch` keeps the rows `is_candidate` accepts (looser than the
selection rule, so `parse` applies the rule and a fixture proves it), and from each kept row only
`KEEP_COLUMNS`. It never keeps `REPRESENTATIVE NAME`, `SIGNATURE CONTROL NUMBER` or `FREQUENCY_JSON`;
`SPONSOR NAME` is read in memory and kept only as `SPONSOR ORGANISATION` when `organisation_name`
proves it is an organisation (below); free text is scrubbed of e-mail addresses, telephone numbers
and anything `services.personal_names` reads as a person's name (`scrub_text`).

Selection (`direct_basis`, `select_cases`). A case is a data-centre case on the first of:
- `structure_name`, `description`, `location_description`: the structure name, proposal
  description or location description says "data center", "data centre", "datacenter",
  "hyperscale" or "DC campus" (`DATA_CENTRE_RE`, the survey's rule, docs/08 §3). A location
  description that names a data centre only as a neighbour ("beside a Data Center") does not count;
- `sponsor`: the sponsor's name says so (read in memory; only the flag is kept);
- `operator`: a data-centre operator's name (`OPERATOR_RE`: QTS, Equinix, NTT, CyrusOne, Vantage
  Data Centers, Aligned, EdgeCore, Yondr, CloudHQ, Digital Realty, ...);
- `campus_code`: a data-centre campus code in an off-airport (OE) case's structure name (`IAD 205`,
  `CMH137`, `PDX154`, `DCA 72`; `CAMPUS_CODE_RE`); a runway designator ("IAD 19R") and an airport's
  own programme ("DCA 10 Year Project", an on-airport NRA case) are not;
- `linked_case`: the case cites (`PRIOR ASN`, or an ASN in its descriptions) a case selected by one
  of the rules above: the cranes filed for a building study are the same project. One hop only.
Precision and recall on hand-checked samples are in docs/08 §4.1 ("Built").

Exclusions (`exclusion`), before linking: the structures that serve a data centre but are not it,
which docs/08 §3 asks to leave out. A transmission tower, utility pole, catenary, wind turbine,
solar array, power plant, stack, drilling rig or sign (`EXCLUDED_TYPE_PREFIXES`,
`EXCLUDED_TYPES_EXACT`); any tower or antenna (`TOWER*`, a monopole, a name that says antenna:
telecom structures); and a case whose structure name or first clause names power infrastructure
(substation, switchyard, transmission or power line, battery storage, a power project or plant)
without naming a data centre (`POWER_SUBJECT_RE`). A campus's own light poles, water tanks, roads
and equipment yards stay: they are the site. So do its generator yards and generator buildings
("Peru Ridge Generator 3", filed as "New data center with 7 buildings") and a data centre's own
energy centre ("South Site Data Center Energy Center"): "generator" is deliberately not a power word.

Sites (`clusters`, `assign_sites`). A campus files many structures and cranes, and a real-world
site is one record. Selected cases in the same state are joined pair by pair within `LINK_KM` (1 km;
`SHARED_LINK_KM`, 1.2 km, when both groups name the same company or carry the same campus code),
nearest pairs first, and a join is refused when (a) the two groups' identities conflict
(`site_identity`, `identities_compatible`): two groups naming different data-centre companies
(`IDENTITY_PATTERNS`), or a group with an AWS-style campus code and no company beside a group naming
a company other than Amazon or Digital Realty, the two that number buildings that way; or (b) the
joined group would be wider than `MAX_SITE_KM` (1.2 km) between its two farthest cases. Nearest
pairs alone chain one campus into the next: on the 2025-2026 archives the first rule (1 km, an
operator guard only) made four Loudoun and Prince William records of several campuses each, 2.5 km
across (Yondr's NOV2 campus inside an AWS-coded record, Iron Mountain's Manassas campus named after
another building's crane, QTS MAN1 published as "NVA05E"); the fixture `loudoun_pwc_campuses.csv`
holds those 77 cases. With the cap and the code identity the same archives give 276 sites, none
wider than 1.18 km. The cap splits a campus wider than it into several records (QTS Cedar Rapids is
about 3 km across); a wider cap for one company was tried and joined Amazon's neighbouring campuses,
which share a name and nothing else. Two AWS-coded campuses closer than 1 km (IAD 12-13 and IAD 170)
stay one record: a code alone does not say which buildings share a campus. Each site is keyed by its
founding case: the
earliest entered, then the lowest ASN. The key must not move when the window moves or when a new
case lands between two sites, so the snapshot carries `site_keys`, the assignment the previous
snapshot produced (`fetch`
recomputes it with this same code): a remembered case keeps its site, an unremembered case joins
the site of the nearest remembered case in its cluster, and only a cluster with no remembered case
gets a new key. Two established sites are therefore never merged, and a site whose cases drift
apart stays one record. Cases from a year no longer fetched are carried forward from the previous
snapshot while they can still matter (`keep_carried`): until `CARRY_GRACE_DAYS` after they stop
being active, at most `CARRY_YEARS` years after entry.

Record model: one `proposal` per site, `kind = technology = load` (as the two air-permit
connectors, docs/25 §3.3). `capacity_mw` is null (the FAA records heights, not MW); `sponsor_name`
is null, because the FAA sponsor is whoever files: a crane contractor or consultant as often as the
owner. Organisation sponsors stay in `raw.sponsor_organisations`.
- source_record_id: the founding case's ASN ("2025-AEA-4484-OE"); source_url: its regional archive.
- name_canonical: the most telling structure name (`site_name`): one naming a data centre, then an
  operator or campus code, then a building's, then any; a name that reads as a person's is never used.
  A name that names no data centre carries its place (`display_name`: "Building B (Marysville data
  centre)"), so a list never shows a bare "Crane 1".
- location: the centroid of the site's permanent structures (else of all its cases), written to raw
  `Latitude`/`Longitude` (an `exact` point for a `raw_ok` source) only when every case's FAA
  accuracy code is 5 (±500 ft) or better and the point falls inside the stated state. County: the
  county that point falls in (vendored Census boundaries, the shared spelling), else the FAA's.
- queue_date: the earliest received date of the site's cases.

Lifecycle (`site_status`, mapped by status_map.yaml). From what the filings prove, with the
determinations evaluated on the fetch date (`evaluated_on`):
- a permanent structure with a `DATE BUILT` (the FAA's notice of actual construction, Form 7460-2)
  under a notice of new construction or alteration: `under_construction`. The structure reached its
  height; operation is not proven. On a notice of an existing structure (`NOTICE OF` "Existing") the
  date built is the original build date, so it proves the structure stands (`built`, below);
- an active case (under FAA study, or a Determination of No Hazard not yet expired): `filed`. A
  Determination of No Hazard clears one structure's height and point for air navigation; it is not
  the project's land-use, air or building permit, so not `permitted`, and it is not a construction
  start, so not `under_construction` (a crane notice included: it says a crane is planned, not up);
- otherwise, a case describing work at an existing data centre (`existing_work`: replacing rooftop
  units, repairing fans, a re-roof, a platform on an existing building, and no new construction
  named; "adjacent to an existing data center" is a neighbour, not this site; or a notice of an
  existing structure with its date built): `built`. The filing itself says the data centre stands.
  "Existing" ticked alone is not enough: filers tick it for new buildings too;
- otherwise the site holds no active FAA case (every determination expired, or a Determination of
  Hazard only) and is left out of the records (`INACTIVE_STATUSES`, counted in
  `raw.meta["sites_inactive"]`). An expired determination proves only that the FAA no longer has an
  active proposal: the project may be built, delayed or abandoned, and no lifecycle state says
  that. A site already loaded therefore becomes a `removed` event that keeps its last state
  (docs/20 §3.3), as ICIS-Air treats a Permanently Closed facility; `unknown` is the harmoniser's
  fall-through and no map points at it.
Expiry (`case_active`): the FAA's own `EXPIRATION DATE`, which carries 14 CFR 77.33(b) ("each
Determination of No Hazard ... expires 18 months after the effective date of the determination")
and any extension; when it is blank, 18 months after the completion date. A Determination of Hazard
has no expiration date (77.33(c)) and keeps nothing active. A crane's own work window is not used:
the determination stays in force until it expires, and a window passing says the lift is done, not
that the project stopped (58 of the 2026-10-24 sites are `filed` only on cranes past their window;
dropping them would drop sites just as building gets under way). Each case reports `work_ended`.

Unchanged runs. The archives change weekly and expiry moves daily, so the snapshot carries its
`evaluated_on` date; `canonical_content` drops that date and adds the set of inactive cases, so a
week with no new filing and no expiry is `unchanged`, and the week a determination expires is not.

Personal data (docs/13 §5.4): 38 of 127 distinct sponsor strings in the survey's sample looked like
a person. `REPRESENTATIVE NAME` and `SIGNATURE CONTROL NUMBER` are never kept; a sponsor is kept
only as `SPONSOR ORGANISATION`, when `services.personal_names.classify` says organisation on
structural evidence (a legal form, digit or industry word: basis `structure`) or the curated list
says so, and nothing in it reads as a person (`organisation_name`): no part split at commas,
slashes, brackets, " - ", "c/o", "attn" or "dba", and no window of any run of capitalised words
(`person_spans`, the test `scrub_text` uses). A legal form makes `classify` answer "structure" for
the whole string, so "<X> ENTERPRISES L.L.C. (<given> <initial>. <surname>)" (2026-AEA-922-OE)
passed the first rule; now it is refused, as is a business named after a person ("<given> <surname>
Consulting"). A bracketed suffix that names no person ("(CE)", "(Alan)") is cut. "AMAZON" alone is
dropped, which costs a name, never a person. A structure name loses any person's name in it, with
the separator before it (`clean_name`: "Crane 1 - <person>" -> "Crane 1"), and `site_name` never
uses a name that still reads as a person's. `parse` puts every stored case through the same rules
again, so a snapshot an older fetch wrote never reaches a record with a name the current rules
refuse. `scrub_text` also removes a title and a name ("Mr. <surname>"), "c/o" or "attn" and a name,
and "<SURNAME>, <GIVEN>"; it leaves places ("Glenn Drive", "Sterling Va", "Santa Clara") and cannot
find lower-case names. The person tests use made-up names.

Terms: US federal government work, public domain (17 U.S.C. §105). The download page's disclaimer
("as is", "as available") is recorded in data/sources.yaml.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import io
import json
import logging
import math
import pathlib
import re
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from functools import lru_cache
from typing import IO, Any, ClassVar

import numpy as np
import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot
from pipeline.connectors.canonical import harmonise_status, norm_county, norm_name, to_date
from pipeline.connectors.us_epa_echo_icis_air.connector import county_for_point
from pipeline.context.geo import StateIndex
from services.personal_names import classify

log = logging.getLogger(__name__)

ARCHIVE_URL = (
    "https://oeaaa.faa.gov/oeaaa/oe3a-external-api/downloadArchives.do?fname=Part77{region}{year}List.gzip"
)
HOME_URL = "https://oeaaa.faa.gov/oeaaa/oe3a/main/"
#: The flag the OE3A page's own script reads to show its shutdown banner (`_site_notice`).
FURLOUGH_FLAG_URL = "https://oeaaa.faa.gov/oeaaa/oe3a-external-api/isFurloughBannerEnabled.do"
FURLOUGH_FLAG = "enableOegFurloughPublicMsg"
#: The nine FAA regions the download page lists (Alaskan, Central, Eastern, Great Lakes, New
#: England, Northwest Mountain, Southern, Southwest, Western-Pacific).
REGIONS = ("AAL", "ACE", "AEA", "AGL", "ANE", "ANM", "ASO", "ASW", "AWP")
#: Calendar years fetched each run: the current one and the one before (module docstring).
YEARS_FETCHED = 2
#: Version of what `fetch` stores. A previous snapshot made by other fetch code is not used for
#: carried cases or site memory; bump it with `is_candidate`, `KEEP_COLUMNS` or the document shape.
FETCH_VERSION = 2
DOWNLOAD_CHUNK = 1 << 20
CSV_FIELD_LIMIT = 16 * 1024 * 1024

# ------------------------------------------------------------------ columns
ASN = "STUDY (ASN)"
PRIOR = "PRIOR ASN"
STATUS = "STATUS"
DETERMINATION = "DETERMINATION"
ENTERED = "ENTERED DATE"
RECEIVED = "RECEIVED DATE"
COMPLETION = "COMPLETION DATE"
EXPIRATION = "EXPIRATION DATE"
LAT = "LATITUDE"
LON = "LONGITUDE"
ACCURACY = "SURVEY_ACCURACY"
NAME = "STRUCTURE NAME"
CITY = "STRUCTURE CITY"
COUNTY = "STRUCTURE COUNTY NAME"
STATE = "STRUCTURE STATE"
DESCRIPTION = "PROPOSAL DESCRIPTION"
LOCATION = "LOCATION DESCRIPTION"
NOTICE = "NOTICE OF"
DURATION = "DURATION"
WORK_BEGIN = "WORK SCHEDULE BEGINNING DATE"
WORK_END = "WORK SCHEDULE ENDING DATE"
DATE_BUILT = "DATE BUILT"
TYPE = "STRUCTURE TYPE"
AGL = "AGL HEIGHT PROPOSED"
AMSL = "AMSL HEIGHT PROPOSED"
SPONSOR = "SPONSOR NAME"
#: Upstream columns that name or identify a person. Never kept, never logged.
PERSON_COLUMNS = ("REPRESENTATIVE NAME", "SPONSOR NAME", "SIGNATURE CONTROL NUMBER")
#: Derived columns `fetch` adds: the sponsor when it is a proven organisation, whether the sponsor's
#: name (read in memory) names a data centre or an operator, and the archive the row came from.
SPONSOR_ORG = "SPONSOR ORGANISATION"
SPONSOR_SIGNAL = "SPONSOR NAMES A DATA CENTRE"
ARCHIVE = "ARCHIVE"
KEEP_COLUMNS = (
    ASN,
    PRIOR,
    STATUS,
    DETERMINATION,
    ENTERED,
    RECEIVED,
    COMPLETION,
    EXPIRATION,
    LAT,
    LON,
    ACCURACY,
    NAME,
    CITY,
    COUNTY,
    STATE,
    DESCRIPTION,
    LOCATION,
    NOTICE,
    DURATION,
    WORK_BEGIN,
    WORK_END,
    DATE_BUILT,
    TYPE,
    AGL,
    AMSL,
)
SNAPSHOT_COLUMNS = (*KEEP_COLUMNS, SPONSOR_ORG, SPONSOR_SIGNAL, ARCHIVE)
#: Columns an archive must carry (`read_archive` refuses one without them).
REQUIRED_COLUMNS = (ASN, STATUS, DETERMINATION, ENTERED, EXPIRATION, LAT, LON, NAME, STATE, DESCRIPTION, TYPE)
_ASN_RE = re.compile(r"^(\d{4})-([A-Z]{3})-(\d+)-(OE|NRA)$")
_AS_OF_RE = re.compile(r"^Data as of\s+(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*([AP]M)?", re.I)

# ------------------------------------------------------------------ selection
DATA_CENTRE_RE = re.compile(r"data\s*cent(?:er|re)s?|datacent|hyperscale|\bdc\s+campus\b", re.I)
OPERATOR_RE = re.compile(
    r"\b(?:QTS|Vantage\s+(?:Data|(?:VA|TX|AZ|OH|WI|NV|GA)\s?\d+)|Edge\s*Core|Cloud\s*HQ|Equinix|NTT|"
    r"Cyrus\s*One|Iron\s+Mountain\s+(?:Data|VA\d)|Yondr|Sabey|STACK\s+Infrastructure|Aligned\s+(?:Data|Energy|PHX)|"
    r"Digital\s+Realty|DataBank|Flexential|T5\s*@\s*\w+|T5\s+(?:Data|CHI|Chicago|DFW|ATL|LA|PDX)|"
    r"Switch\s+NAP|CoreWeave|Crusoe|Corscale|PowerHouse\s+Data|Compass\s+Datacenters|Novva|EdgeConneX|"
    r"Cologix|CoreSite|H5\s+Data|DC\s*Blox|Centersquare|Element\s+Critical)\b",
    re.I,
)
#: AWS campus codes ("IAD 205", "CMH137", "PDX154", "DCA 72"): the airport code then two or three
#: digits, not a runway designator ("IAD 19R") and not an airport's own programme ("DCA 10 Year
#: Project"). Read only on off-airport (OE) cases.
CAMPUS_CODE_RE = re.compile(r"\b(?:IAD|DCA|CMH|PDX)\s?-?\d{2,3}\b(?![LRC]\b)(?!\s*-?\s*(?:[Yy]ear|YEAR))")
#: A data centre named only as a neighbour in a location description ("beside a Data Center").
PROXIMITY_RE = re.compile(
    r"\b(?:beside|adjacent\s+to|next\s+to|near|across\s+from|behind)\s+(?:the\s+|an?\s+|existing\s+)?"
    r"(?:\w+\s+){0,3}$",
    re.I,
)
#: Structure types that serve a data centre but are not it: lines, poles, generation, stacks,
#: towers and antennas, drilling rigs, signs. Prefixes, except the exact pole types.
EXCLUDED_TYPE_PREFIXES = (
    "TRANSMISSION_LINE",
    "CATENARY",
    "WIND",
    "TOWER",
    "ELECTRICAL_SYSTEM",
    "POWER_PLANT",
    "STACK",
    "RIG",
    "SIGN",
)
EXCLUDED_TYPES_EXACT = frozenset({"POLE", "POLE$UTILITY", "POLE$MONO"})
ANTENNA_RE = re.compile(r"\b(?:antennas?|monopole|cell\s+tower|telecom\w*)\b", re.I)
POWER_SUBJECT_RE = re.compile(
    r"\b(?:sub-?stations?|switch\s*yards?|switching\s+stations?|transmission|power\s*lines?|"
    r"distribution\s+lines?|power\s+(?:plants?|projects?|hubs?|stations?|generation)|utility\s+plants?|"
    r"generating\s+(?:station|facility|plant)|turbines?|solar|photovoltaic|battery\s+energy\s+storage|BESS|"
    r"energy\s+storage)\b",
    re.I,
)
ASN_REF_RE = re.compile(
    r"\b(20\d\d)[- ]?([A-Z]{3})[- ]?(\d{1,6})(?:\s*(?:-|:|to|through|thru)\s*(\d{1,6}))?[- ]?(OE|NRA)\b"
)
#: Data-centre companies, for keeping neighbouring campuses apart (`clusters`). Wider than
#: `OPERATOR_RE` (the hyperscalers' own names select warehouses and offices too, so they identify
#: but never select).
IDENTITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (key, re.compile(pattern, re.I))
    for key, pattern in (
        ("amazon", r"\b(?:amazon|aws|vadata)\b"),
        ("microsoft", r"\bmicrosoft\b"),
        ("google", r"\bgoogle\b(?!\s+(?:earth|maps?)\b)"),
        ("meta", r"\b(?:meta|facebook)\b"),
        ("oracle", r"\boracle\b"),
        ("qts", r"\bQTS\b"),
        ("equinix", r"\bequinix\b"),
        ("ntt", r"\bNTT\b"),
        ("cyrusone", r"\bcyrus\s*one\b"),
        ("cloudhq", r"\bcloud\s*hq\b|\bcloud\s*LC\s?\d"),
        ("edgecore", r"\bedge\s*core\b"),
        ("vantage", r"\bvantage\b"),
        ("aligned", r"\baligned\s+(?:data|energy|PHX|DC)|(?-i:\bAligned\s+\d{2}\b)"),
        ("yondr", r"\byondr\b"),
        ("sabey", r"\bsabey\b"),
        ("stack", r"\bstack\s+(?:infrastructure|data|[A-Z]{3}\s?\d)"),
        ("stream", r"\bstream\s+(?:data|fir|PHX|DFW)"),
        ("prime", r"\bprime\s+data"),
        ("digital_realty", r"\bdigital\s+realty\b|\bDLR\b"),
        ("databank", r"\bdatabank\b"),
        ("flexential", r"\bflexential\b"),
        ("t5", r"\bT5\s*@|\bT5\s+(?:data|CHI|Chicago|DFW|ATL|LA|PDX)\b"),
        ("switch", r"\bswitch\s+(?:nap|citadel|prime)\b"),
        ("coreweave", r"\bcoreweave\b"),
        ("crusoe", r"\bcrusoe\b"),
        ("iron_mountain", r"\biron\s+m(?:ou)?n?t"),
        ("compass", r"\bcompass\s+(?:datacenters?|data\s+cent|PHX|[A-Z]{3}\d)"),
        ("coresite", r"\bcoresite\b"),
        ("cologix", r"\bcologix\b"),
        ("edgeconnex", r"\bedge\s*connex\b|\bECX\b"),
        ("dc_blox", r"\bDC\s*Blox\b"),
        ("centersquare", r"\bcentersquare\b"),
    )
)
#: Bases in the order a site reports its strongest one.
BASES = (
    "structure_name",
    "description",
    "location_description",
    "sponsor",
    "operator",
    "campus_code",
    "linked_case",
)

# ------------------------------------------------------------------ evidence and expiry
UNDER_STUDY = frozenset({"Studying", "Evaluating", "Reevaluating", "Pending", "Revision", "Public Comment"})
DETERMINED_NO_HAZARD = frozenset({"Determined - No Hazard", "Reminder"})
HAZARD = "Determined - Hazard"
NOTICE_EXISTING = "Existing"
DNH_MONTHS = 18
EXISTING_RE = re.compile(
    r"\bexisting\b|\breplac\w*|\bswap\w*|\brepair\w*|\bre-?install\w*|\bre-?roof\w*|\bmaintenance\b|"
    r"\bbuilding\s+used\s+as\s+a\s+data\s*cent",
    re.I,
)
#: New construction named in a filing: new work wins over `EXISTING_RE` ("new building on existing
#: campus"). "Construction materials" and "construction site" alone are not new work.
NEW_WORK_RE = re.compile(
    r"\bnew\b|\bconstruct(?:ion)?\s+(?:of|a|an|the|new)\b|\bconstructing\b|"
    r"\bconstruction\s+(?:crane|equipment|activit\w*|project|phase|purposes)\b|\bunder\s+construction\b|"
    r"\bto\s+build\b|\bbuild\s+(?:a|an|the)\b|\bprecast\b|\bstructural\s+steel\b|\bsteel\s+erection\b|"
    r"\btilt[-\s]?(?:up|walls?|panels?)\b|"
    r"\bexpan\w*|\baddition\b|\bdemoli\w*|\bconver\w*|\bproposed\b|\bphase\b|\bdevelopment\b|\bgreenfield\b",
    re.I,
)
#: "Existing" said of a neighbour ("adjacent to an existing data center") is not this filing's site.
NEIGHBOUR_EXISTING_RE = re.compile(r"\b(?:adjacent|next|close)\s+to\s+(?:an?\s+|the\s+)?existing\b", re.I)
#: Site status labels (status_raw); status_map.yaml maps them to the lifecycle.
STATUS_BUILT_REPORTED = "Structure height reached (FAA notice of actual construction)"
STATUS_DETERMINED = "Determined - No Hazard"
STATUS_UNDER_STUDY = "Under FAA aeronautical study"
STATUS_EXISTING = "Work at an existing data centre"
STATUS_HAZARD = "Determined - Hazard"
STATUS_EXPIRED = "FAA determinations expired"
#: Labels of a site with no active FAA case: such a site is not emitted (module docstring), so
#: status_map.yaml deliberately lists neither.
INACTIVE_STATUSES = frozenset({STATUS_HAZARD, STATUS_EXPIRED})

# ------------------------------------------------------------------ sites and placement
LINK_KM = 1.0
#: The widest a site may grow, between its two farthest cases (`clusters`).
MAX_SITE_KM = 1.2
#: How far apart two groups that share an identity (a company, a campus code) may still be joined,
#: within `MAX_SITE_KM`.
SHARED_LINK_KM = 1.2
#: The identity a campus code stands for (`site_identity`), and the companies that number their
#: buildings with such codes: a coded group may join only them.
CAMPUS_CODE = "campus_code"
CODE_NUMBERING_COMPANIES = frozenset({"amazon", "digital_realty"})
CARRY_GRACE_DAYS = 90
CARRY_YEARS = 6
#: FAA accuracy codes: the digit is the horizontal class (1 = ±20 ft ... 5 = ±500 ft, 6 = ±1,000 ft,
#: 7 = ±1/2 NM, 8 = ±1 NM, 9 = unknown). Classes 1-5 are within about 150 m: an exact point.
EXACT_ACCURACY_CLASSES = frozenset("12345")
COORD_DECIMALS = 6
REGIONS_DIR = pathlib.Path(__file__).resolve().parents[3] / "data" / "vendored" / "regions"

# ------------------------------------------------------------------ personal data
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}\b")
#: Runs of two or more capitalised words (initials allowed); `_person_words` asks `classify` about
#: each window of a run.
_NAME_SEQ_RE = re.compile(r"\b[A-Z][a-zA-Z'\-]+(?:\s+(?:[A-Z]\.?|[A-Z][a-zA-Z'\-]+))+\b")
#: Where a sponsor string joins its parts: commas, slashes, semicolons, brackets, " - ", "c/o",
#: "attn", "dba".
_SPLIT_RE = re.compile(r"\s*(?:,|/|;|[()\[\]{}]|\s-\s|\bc/o\b|\battn:?|\bdba\b)\s*", re.I)
#: A bracketed part of a sponsor ("(CE)", "(Alan)", "(ADS)"): the filer's initials, a contact or
#: an abbreviation. Never needed to name the organisation, so never kept.
_BRACKETED_RE = re.compile(r"\s*[(\[{][^)\]}]*(?:[)\]}]|$)")
#: A title before a name ("Mr. Smith", "Ms Jane Doe").
_HONORIFIC_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Mx|Miss)\.?\s+(?:[A-Z]\.?\s+)?[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)?"
)
#: "c/o" or "attn" then a name ("c/o J. Smith"): the words after it, initials included.
_CARE_OF_RE = re.compile(
    r"(?i:\bc/o\b|\battn\b:?|\battention\b:?)\s+((?:[A-Z]\.?|[A-Z][A-Za-z'\-]+)(?:\s+(?:[A-Z]\.?|[A-Z][A-Za-z'\-]+)){0,3})"
)
#: "SMITH, JOHN" / "Smith, John A.": a surname, a comma, a given name.
_LAST_FIRST_RE = re.compile(r"\b[A-Z][A-Za-z'\-]+,\s+[A-Z][A-Za-z'\-]+(?:\s+[A-Z]\.?)?(?![\w'])")
#: Words that make a window of capitalised words a place in this source's text, not a person:
#: a street type after the first word ("Glenn Drive", "Belmont Ridge Rd"), a place prefix as the
#: first word ("Santa Clara", "Fort Worth") and a two-letter state as the last ("Sterling Va").
#: "Lane" is not one: it is a surname as often as a street.
_STREET_WORDS = frozenset(
    "drive dr road rd street avenue ave boulevard blvd parkway pkwy way court ct circle cir place pl pike "
    "highway hwy trail trl terrace ter loop".split()
)
_PLACE_PREFIXES = frozenset("santa san fort ft saint port las los el".split())
_STATE_CODES = frozenset(
    "al ak az ar ca co ct de fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd "
    "oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy dc pr".split()
)
_STATE_NAMES = frozenset(
    "alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho "
    "illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota "
    "mississippi missouri montana nebraska nevada ohio oklahoma oregon pennsylvania tennessee texas utah "
    "vermont virginia washington wisconsin wyoming".split()
)


def _place_window(words: Sequence[str]) -> bool:
    bare = [w.strip(".,'\"").casefold() for w in words]
    return (
        bare[0] in _PLACE_PREFIXES
        or any(b in _STREET_WORDS for b in bare[1:])
        or (len(bare[-1]) == 2 and bare[-1] in _STATE_CODES and words[-1][:1].isupper())
    )


def _person_words(seq: str, offset: int, kept: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Character spans (in the whole text: the run starts at `offset`) of the words in a run of
    capitalised words that `classify` reads as part of a person's name, tested window by window
    (four, three, then two words): "Contact Jane Doe" yields "Jane Doe" though "Contact" is no given
    name. Windows that touch a data-centre word, an operator or a campus code of the whole text
    (`kept`: "Cyrus One" is a company, "T5 Chi IV" a campus) or read as a place are not tested."""
    spans = [(offset + m.start(), offset + m.end()) for m in re.finditer(r"\S+", seq)]
    words = [seq[a - offset : b - offset] for a, b in spans]
    protected = [any(a < e and s < b for s, e in kept) for a, b in spans]
    drop = [False] * len(words)
    for size in (4, 3, 2):
        for i in range(len(words) - size + 1):
            window = words[i : i + size]
            if any(protected[i : i + size]) or _place_window(window):
                continue
            if classify(" ".join(window)).personal:
                drop[i : i + size] = [True] * size
    return [span for span, d in zip(spans, drop, strict=True) if d]


def person_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of `text` that name a person, merged and in order: windows of capitalised
    runs (`_person_words`), a title and a name, "c/o" or "attn" and a name, and "Surname, Given" (not
    "Ashburn, Virginia")."""
    kept = [m.span() for rx in (DATA_CENTRE_RE, OPERATOR_RE, CAMPUS_CODE_RE) for m in rx.finditer(text)]
    found: list[tuple[int, int]] = []
    for m in _NAME_SEQ_RE.finditer(text):
        found.extend(_person_words(m.group(0), m.start(), kept))
    found.extend(m.span() for m in _HONORIFIC_RE.finditer(text))
    found.extend(m.span(1) for m in _CARE_OF_RE.finditer(text))
    for m in _LAST_FIRST_RE.finditer(text):
        given = m.group(0).split(",", 1)[1].split()[0].casefold()
        if given not in _STATE_NAMES and given not in _STATE_CODES and classify(m.group(0)).personal:
            found.append(m.span())
    merged: list[tuple[int, int]] = []
    for a, b in sorted(found):
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _whole_is_person(text: str) -> bool:
    """`classify` on the whole string, unless it names a data centre, an operator or a campus code
    ("Cyrus One" alone is a company; any person beside it is found by `person_spans`)."""
    named = any(rx.search(text) for rx in (DATA_CENTRE_RE, OPERATOR_RE, CAMPUS_CODE_RE))
    return not named and classify(text).personal


def names_a_person(text: str | None) -> bool:
    """Whether any part of `text` reads as a person's name: the whole string (`_whole_is_person`),
    or any span `person_spans` finds in it."""
    value = " ".join(str(text or "").split())
    return bool(value) and (_whole_is_person(value) or bool(person_spans(value)))


def organisation_name(name: str | None) -> str | None:
    """The organisation `name` names when the repo's personal-name rules prove it is one, else None.

    Proof is `classify` answering not-personal on structural evidence (basis `structure`: a legal
    form, a digit, an industry word) or the curated not-personal list, *and* nothing in it reading
    as a person: no part of it split at commas, slashes, brackets, " - ", "c/o", "attn" or "dba",
    and no window of any run of capitalised words (`person_spans`, the test `scrub_text` uses). So
    "Acme LLC / Jane Doe", "Acme LLC (Jane Doe)", "Acme Inc Jane Doe", "Jane Doe (Acme Inc)" and
    "Mary Jones Consulting" are all refused: a business named after its owner names a person. A
    name with no person shape but no structure either ("AMAZON") is refused too: not proven. A
    bracketed part that names no person ("(CE)", "(Alan)") is cut: initials or a contact, never
    needed to name the organisation; what is left must still be proven. Never logs the name."""
    text = " ".join(str(name or "").split())
    if not text:
        return None
    whole = classify(text)
    if whole.personal or whole.basis not in ("structure", "curated_not_personal"):
        return None
    for part in _SPLIT_RE.split(text):
        if part and classify(part).personal:
            return None
    if person_spans(text):
        return None
    kept = " ".join(_BRACKETED_RE.sub(" ", text).split()).strip(" ,;/-")
    if not kept:
        return None
    if kept != text:
        rest = classify(kept)
        if rest.personal or rest.basis not in ("structure", "curated_not_personal"):
            return None
    return kept


def scrub_text(text: str | None) -> str:
    """Free text without e-mail addresses, telephone numbers or anything that reads as a person's
    name (`person_spans`). Runs that name a data centre, an operator or a campus code are kept, so
    the selection rule reads the same words after scrubbing ("Cyrus One" is a company). Lower-case
    names ("john smith") are not found: the given-name list cannot tell them from ordinary words."""
    out = " ".join(str(text or "").split())
    if not out:
        return ""
    out = _EMAIL_RE.sub("[email removed]", out)
    out = _PHONE_RE.sub("[phone removed]", out)
    for a, b in reversed(person_spans(out)):
        out = f"{out[:a]}[name removed]{out[b:]}"
    return out


_GAP = "\x00"


def clean_name(name: str | None) -> str:
    """A structure name without any person's name in it ("Crane 1 - Glen Williams" -> "Crane 1",
    "Mobile crane (contact Glen Williams)" -> "Mobile crane"), the separator or "contact" that led
    to the name going with it; "" when nothing else is left or the whole name reads as a person."""
    text = " ".join(str(name or "").split())
    if not text or _whole_is_person(text):
        return ""
    text = _EMAIL_RE.sub(_GAP, _PHONE_RE.sub(_GAP, text))
    for a, b in reversed(person_spans(text)):
        text = f"{text[:a]}{_GAP}{text[b:]}"
    if _GAP not in text:
        return text
    text = re.sub(rf"(?i:\b(?:contact|attn|c/o|by|for|call)\b:?\s*)+{_GAP}", _GAP, text)
    text = re.sub(rf"[(\[{{]\s*{_GAP}\s*[)\]}}]?", _GAP, text)
    text = re.sub(rf"\s*(?:\s[-–]\s|[,/;:])?\s*{_GAP}+\s*(?:\s?[-–]\s|[,/;:])?\s*", " ", text)
    out = " ".join(text.replace(_GAP, " ").split()).strip(" -–,/;:")
    return "" if names_a_person(out) else out


# ------------------------------------------------------------------ archives
def archive_url(region: str, year: int) -> str:
    return ARCHIVE_URL.format(region=region, year=year)


def asn_key(asn: str) -> tuple[int, str, int, str]:
    """Sort key for an ASN: year, region, sequence number, case type."""
    m = _ASN_RE.match(asn)
    if not m:
        return (9999, asn, 0, "")
    return (int(m.group(1)), m.group(2), int(m.group(3)), m.group(4))


def data_as_of(line: str) -> str | None:
    """The archive's trailer ("Data as of 10/17/2026 01:15 AM") as an ISO local time ("2026-10-17T01:15";
    the FAA states no zone, so none is invented)."""
    m = _AS_OF_RE.match(line.strip())
    if not m:
        return None
    month, day, year, hour, minute, ampm = m.groups()
    h = int(hour) % 12 + (12 if (ampm or "").upper() == "PM" else 0) if ampm else int(hour)
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}T{h:02d}:{minute}"


def read_archive(fh: IO[str]) -> tuple[list[str], Iterator[dict[str, str]], dict[str, Any]]:
    """(header, rows, info) of one archive. The FAA's CSV escapes quotes inside quoted fields with a
    backslash (the frequency JSON) and keeps CRLF inside quoted descriptions, so it is read with
    `escapechar="\\\\"` from a file opened with `newline=""`. Rows without a well-formed ASN (the
    blank line and the "Data as of" trailer) are skipped; the trailer's timestamp lands in
    `info["data_as_of"]` once the iterator is exhausted. Header names are stripped (the FAA pads
    four of them with a trailing space)."""
    csv.field_size_limit(CSV_FIELD_LIMIT)
    reader = csv.reader(fh, escapechar="\\")
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        return [], iter(()), {"rows": 0, "data_as_of": None}
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ParseError(f"FAA archive lacks {missing}; header starts {header[:8]}")
    info: dict[str, Any] = {"rows": 0, "data_as_of": None}

    def rows() -> Iterator[dict[str, str]]:
        for rec in reader:
            if not rec:
                continue
            first = rec[0].strip()
            if not _ASN_RE.match(first):
                stamp = data_as_of(first)
                if stamp:
                    info["data_as_of"] = stamp
                continue
            info["rows"] += 1
            yield {h: (rec[i].strip() if i < len(rec) else "") for i, h in enumerate(header)}

    return header, rows(), info


def cited_asns(row: Mapping[str, Any]) -> set[str]:
    """ASNs a case cites: its `PRIOR ASN` and any ASN (or "2026-AEA-2624:2626-OE" range of at most
    50) written in its descriptions."""
    out: set[str] = set()
    prior = str(row.get(PRIOR) or "").strip()
    if _ASN_RE.match(prior):
        out.add(prior)
    text = f"{row.get(DESCRIPTION) or ''} {row.get(LOCATION) or ''}"
    for m in ASN_REF_RE.finditer(text):
        year, region, lo_s, hi_s, kind = m.groups()
        lo = int(lo_s)
        hi = int(hi_s) if hi_s else lo
        if hi < lo or hi - lo > 50:
            hi = lo
        out.update(f"{year}-{region}-{n}-{kind}" for n in range(lo, hi + 1))
    out.discard(str(row.get(ASN) or ""))
    return out


def _signal(text: str) -> bool:
    return bool(DATA_CENTRE_RE.search(text) or OPERATOR_RE.search(text))


def is_candidate(row: Mapping[str, Any], sponsor: str = "") -> bool:
    """Fetch-side pre-filter, looser than `direct_basis`: any data-centre word, operator or campus
    code in the name, descriptions or sponsor, with no exclusion applied."""
    text = " ".join(str(row.get(c) or "") for c in (NAME, DESCRIPTION, LOCATION))
    return _signal(text) or _signal(sponsor) or bool(CAMPUS_CODE_RE.search(str(row.get(NAME) or "")))


def cut_row(row: Mapping[str, str], archive: str) -> list[str]:
    """The stored form of one candidate: `KEEP_COLUMNS` (descriptions scrubbed, any person's name
    cut from the structure name), the sponsor reduced to a proven organisation and a flag, and the
    archive."""
    sponsor = str(row.get(SPONSOR) or "")
    out: list[str] = []
    for c in KEEP_COLUMNS:
        v = str(row.get(c) or "")
        if c in (DESCRIPTION, LOCATION):
            v = scrub_text(v)
        elif c == NAME:
            v = clean_name(v)
        out.append(v)
    out.append(organisation_name(sponsor) or "")
    out.append("Y" if _signal(sponsor) else "")
    out.append(archive)
    return out


# ------------------------------------------------------------------ selection
def exclusion(case: Mapping[str, Any]) -> str | None:
    """Why a case is left out although it names a data centre (module docstring); None = kept."""
    stype = str(case.get(TYPE) or "").strip().upper()
    name = str(case.get(NAME) or "")
    if stype in EXCLUDED_TYPES_EXACT or stype.startswith(EXCLUDED_TYPE_PREFIXES):
        return "structure_type"
    if ANTENNA_RE.search(name):
        return "telecom_structure"
    first_clause = re.split(r"[.;]", str(case.get(DESCRIPTION) or ""), maxsplit=1)[0][:200]
    for text in (name, first_clause):
        if (
            POWER_SUBJECT_RE.search(text)
            and not DATA_CENTRE_RE.search(name)
            and not DATA_CENTRE_RE.search(text)
        ):
            return "power_infrastructure"
    return None


def _location_names_site(text: str) -> bool:
    """A data-centre word in the location description that is not only a neighbour's."""
    return any(not PROXIMITY_RE.search(text[: m.start()]) for m in DATA_CENTRE_RE.finditer(text))


def direct_basis(case: Mapping[str, Any]) -> str | None:
    """The first rule that selects a case on its own words (module docstring), or None."""
    name = str(case.get(NAME) or "")
    if DATA_CENTRE_RE.search(name):
        return "structure_name"
    if DATA_CENTRE_RE.search(str(case.get(DESCRIPTION) or "")):
        return "description"
    if _location_names_site(str(case.get(LOCATION) or "")):
        return "location_description"
    if str(case.get(SPONSOR_SIGNAL) or "") == "Y":
        return "sponsor"
    text = " ".join(str(case.get(c) or "") for c in (NAME, DESCRIPTION, LOCATION))
    if OPERATOR_RE.search(text):
        return "operator"
    if CAMPUS_CODE_RE.search(name) and str(case.get(ASN) or "").endswith("-OE"):
        return "campus_code"
    return None


def select_cases(cases: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str], Counter[str]]:
    """({asn: basis} for the selected cases, Counter of exclusion reasons among cases a rule chose)."""
    excluded: Counter[str] = Counter()
    basis: dict[str, str] = {}
    for c in cases:
        b = direct_basis(c)
        if b is None:
            continue
        why = exclusion(c)
        if why:
            excluded[why] += 1
            continue
        basis[str(c[ASN])] = b
    direct = set(basis)
    for c in cases:
        asn = str(c[ASN])
        if asn in basis or not (cited_asns(c) & direct):
            continue
        why = exclusion(c)
        if why:
            excluded[why] += 1
            continue
        basis[asn] = "linked_case"
    return basis, excluded


# ------------------------------------------------------------------ dates and evidence
def _date(value: Any) -> dt.date | None:
    text = str(value or "").strip()[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _add_months(day: dt.date, months: int) -> dt.date:
    year, month = divmod(day.month - 1 + months, 12)
    year += day.year
    month += 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def expires_on(case: Mapping[str, Any]) -> dt.date | None:
    """When a determined case stops being active: the FAA's `EXPIRATION DATE`, else 18 months after
    completion (14 CFR 77.33(b)); None when neither date is given."""
    exp = _date(case.get(EXPIRATION))
    if exp is not None:
        return exp
    done = _date(case.get(COMPLETION))
    return _add_months(done, DNH_MONTHS) if done else None


def _dated_permanent(case: Mapping[str, Any]) -> bool:
    return bool(_date(case.get(DATE_BUILT))) and str(case.get(DURATION) or "").strip() != "Temporary"


def built_reported(case: Mapping[str, Any]) -> bool:
    """A permanent structure the sponsor reported built under a notice of new construction or
    alteration (FAA Form 7460-2: `DATE BUILT`). On a notice of an existing structure (`NOTICE OF`
    "Existing") the date built is when the structure was first built, not a 7460-2, so it proves
    the structure stands (`existing_work`), not that it just reached its height."""
    return _dated_permanent(case) and str(case.get(NOTICE) or "").strip() != NOTICE_EXISTING


def case_active(case: Mapping[str, Any], on: dt.date) -> bool:
    """Under FAA study, or a Determination of No Hazard not yet expired on `on`."""
    status = str(case.get(STATUS) or "").strip()
    if status in UNDER_STUDY:
        return True
    if status in DETERMINED_NO_HAZARD:
        end = expires_on(case)
        return end is None or end >= on
    return False


def existing_work(case: Mapping[str, Any]) -> bool:
    """The filing describes work at an existing data centre and names no new construction, or it
    is a notice of an existing permanent structure with the date it was built. "Existing" alone is
    not enough: filers tick it for new buildings too ("PDX139 ... four single-story buildings")."""
    if _dated_permanent(case) and str(case.get(NOTICE) or "").strip() == NOTICE_EXISTING:
        return True
    text = NEIGHBOUR_EXISTING_RE.sub(" ", f"{case.get(DESCRIPTION) or ''} {case.get(LOCATION) or ''}")
    return bool(EXISTING_RE.search(text)) and not NEW_WORK_RE.search(text)


def work_ended(case: Mapping[str, Any], on: dt.date) -> bool:
    """A temporary structure whose own work schedule ended before `on`. Reported, not used for
    activity: the FAA's expiration date is when its determination stops being in force, and a
    crane's window passing says the lift is done, not that the project stopped (module docstring)."""
    end = _date(case.get(WORK_END))
    return str(case.get(DURATION) or "").strip() == "Temporary" and end is not None and end < on


def site_status(cases: Sequence[Mapping[str, Any]], on: dt.date) -> str:
    """The site's status label from what its filings prove on `on` (module docstring)."""
    new_work = [c for c in cases if not existing_work(c)]
    if any(built_reported(c) for c in new_work):
        return STATUS_BUILT_REPORTED
    active = [c for c in new_work if case_active(c, on)]
    if any(str(c.get(STATUS) or "").strip() in DETERMINED_NO_HAZARD for c in active):
        return STATUS_DETERMINED
    if active:
        return STATUS_UNDER_STUDY
    if any(existing_work(c) for c in cases):
        return STATUS_EXISTING
    if any(str(c.get(STATUS) or "").strip() == HAZARD for c in cases):
        return STATUS_HAZARD
    return STATUS_EXPIRED


def keep_carried(case: Mapping[str, Any], on: dt.date) -> bool:
    """Whether a case from a year no longer fetched is carried into this snapshot (module docstring)."""
    entered = _date(case.get(ENTERED))
    if entered is None or entered < dt.date(on.year - CARRY_YEARS, 1, 1):
        return False
    if built_reported(case) or existing_work(case):
        return True
    return case_active(case, on - dt.timedelta(days=CARRY_GRACE_DAYS))


# ------------------------------------------------------------------ sites
def _float(value: Any) -> float | None:
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in km (equirectangular; exact enough at a few kilometres)."""
    p = math.pi / 180.0
    x = (lon2 - lon1) * p * math.cos((lat1 + lat2) / 2.0 * p)
    return 6371.0 * math.hypot(x, (lat2 - lat1) * p)


def _order(case: Mapping[str, Any]) -> tuple[str, tuple[int, str, int, str]]:
    return (str(case.get(ENTERED) or "9999"), asn_key(str(case[ASN])))


def operators_named(case: Mapping[str, Any]) -> frozenset[str]:
    """The data-centre companies a case names (`IDENTITY_PATTERNS`), in its structure name,
    descriptions or organisation sponsor."""
    text = " ".join(str(case.get(c) or "") for c in (NAME, DESCRIPTION, LOCATION, SPONSOR_ORG))
    return frozenset(key for key, pattern in IDENTITY_PATTERNS if pattern.search(text))


def site_identity(case: Mapping[str, Any]) -> frozenset[str]:
    """The identity two neighbouring campuses must not share a record across: the companies a case
    names (`operators_named`), else `CAMPUS_CODE` when its structure name carries a campus code
    ("IAD 12-13 Datacenters", "Project IAD 170"). A code names no company by itself (Digital Realty
    numbers its Dulles buildings "IAD56" as AWS numbers its own), so it stands for those two only
    (`identities_compatible`)."""
    named = operators_named(case)
    if named:
        return named
    return frozenset({CAMPUS_CODE}) if CAMPUS_CODE_RE.search(str(case.get(NAME) or "")) else frozenset()


def identities_compatible(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two groups (their `site_identity` unions) may be one site. Once both name a company,
    only a shared company joins them (a campus code no longer counts: an Amazon group and a Digital
    Realty group are two sites, whatever codes they carry). A group with a campus code and no
    company joins a coded group, an unnamed group, or a group naming a company that numbers its
    buildings with codes (`CODE_NUMBERING_COMPANIES`), never a Yondr or QTS group. A group with
    no identity joins any."""
    ca, cb = a - {CAMPUS_CODE}, b - {CAMPUS_CODE}
    if ca and cb:
        return bool(ca & cb)
    if not a or not b or not (ca or cb):
        return True
    return bool((ca or cb) & CODE_NUMBERING_COMPANIES)


def campus_codes(case: Mapping[str, Any]) -> frozenset[str]:
    """The campus codes a case's structure name carries, spaces and hyphens dropped ("IAD 457
    Crawler Gens" -> {"IAD457"}): two groups sharing one are one campus (`clusters`)."""
    name = str(case.get(NAME) or "")
    return frozenset(re.sub(r"[\s-]", "", m.group(0)).upper() for m in CAMPUS_CODE_RE.finditer(name))


def clusters(
    cases: Sequence[Mapping[str, Any]],
    link_km: float = LINK_KM,
    max_km: float = MAX_SITE_KM,
    shared_link_km: float = SHARED_LINK_KM,
) -> list[list[int]]:
    """Groups of case indices, one per site. Cases in one state are joined pair by pair, nearest
    pairs first (ties by index): within `link_km`, or within `shared_link_km` when the two groups
    share an identity (a company both name, or a campus code: two "Aligned Data Center Campus"
    filings 1.1 km apart). A join is refused when (a) the groups' identities conflict
    (`identities_compatible`: an Equinix campus and an NTT campus across the road, or a Yondr campus
    and an AWS-coded one, stay two records even when an unnamed crane stands between them), or (b)
    the joined group would be wider than `max_km` between its two farthest cases: nearest pairs
    alone chain one campus into the next (single linkage; in Loudoun County a 1 km link made 2.5 km
    records of four campuses each). The cap splits a campus wider than it (QTS Cedar Rapids, about
    3 km) into several records; a wider cap for one company would join Amazon's or Digital Realty's
    neighbouring campuses, which share a name and nothing else. A case without a point is a group of
    its own. Deterministic: groups in input order of their first member."""
    parent = list(range(len(cases)))
    owners = [site_identity(c) for c in cases]
    codes = [campus_codes(c) for c in cases]
    members: dict[int, list[int]] = {i: [i] for i in range(len(cases))}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    reach = max(link_km, shared_link_km)
    cell_deg = reach / 111.0
    grid: dict[tuple[str, int, int], list[int]] = {}
    lat = np.full(len(cases), np.nan)
    lon = np.full(len(cases), np.nan)
    for i, c in enumerate(cases):
        y, x = _float(c.get(LAT)), _float(c.get(LON))
        if y is not None and x is not None:
            lat[i], lon[i] = y, x
    pairs: list[tuple[float, int, int]] = []
    for i, c in enumerate(cases):
        if math.isnan(lat[i]):
            continue
        state = str(c.get(STATE) or "").strip().upper()
        cy, cx = math.floor(lat[i] / cell_deg), math.floor(lon[i] / cell_deg)
        for dy in (-1, 0, 1):
            for dx in range(-3, 4):  # longitude cells shrink with latitude; 3 covers |lat| < 70
                for j in grid.get((state, cy + dy, cx + dx), []):
                    d = km(lat[i], lon[i], lat[j], lon[j])
                    if d <= reach:
                        pairs.append((d, j, i))
        grid.setdefault((state, cy, cx), []).append(i)

    def width(a: list[int], b: list[int]) -> float:
        """km between the farthest case of group `a` and of group `b` (`km`, vectorised)."""
        p = math.pi / 180.0
        la, lb = lat[a][:, None], lat[b][None, :]
        x = (lon[b][None, :] - lon[a][:, None]) * p * np.cos((la + lb) / 2.0 * p)
        return float(6371.0 * np.hypot(x, (lb - la) * p).max())

    for d, j, i in sorted(pairs):
        ri, rj = find(i), find(j)
        if ri == rj or not identities_compatible(owners[ri], owners[rj]):
            continue
        shared = bool((owners[ri] - {CAMPUS_CODE}) & (owners[rj] - {CAMPUS_CODE}) or codes[ri] & codes[rj])
        if d > (shared_link_km if shared else link_km):
            continue
        if width(members[ri], members[rj]) > max_km:
            continue
        keep, gone = min(ri, rj), max(ri, rj)
        parent[gone] = keep
        owners[keep] = owners[keep] | owners[gone]
        codes[keep] = codes[keep] | codes[gone]
        members[keep].extend(members.pop(gone))
    groups: dict[int, list[int]] = {}
    for i in range(len(cases)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def assign_sites(
    cases: Sequence[Mapping[str, Any]], memory: Mapping[str, str], link_km: float = LINK_KM
) -> dict[str, str]:
    """{asn: site key} for the selected cases (module docstring, "Sites")."""
    out: dict[str, str] = {}
    for group in clusters(cases, link_km):
        remembered = [i for i in group if memory.get(str(cases[i][ASN]))]
        if not remembered:
            founder = min(group, key=lambda i: _order(cases[i]))
            key = str(cases[founder][ASN])
            out.update({str(cases[i][ASN]): key for i in group})
            continue
        for i in group:
            asn = str(cases[i][ASN])
            if asn in memory:
                out[asn] = memory[asn]
                continue
            out[asn] = memory[str(cases[_nearest(cases, i, remembered, memory)][ASN])]
    return out


def _nearest(
    cases: Sequence[Mapping[str, Any]], i: int, among: Sequence[int], memory: Mapping[str, str]
) -> int:
    """The case in `among` nearest to case `i` (ties: the smaller remembered key)."""

    def key(j: int) -> tuple[float, str]:
        return (_distance(cases[i], cases[j]), memory[str(cases[j][ASN])])

    return min(among, key=key)


def _distance(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    """km between two cases' points; infinite when either has none."""
    la, lo = _float(a.get(LAT)), _float(a.get(LON))
    lb, mb = _float(b.get(LAT)), _float(b.get(LON))
    if la is None or lo is None or lb is None or mb is None:
        return math.inf
    return km(la, lo, lb, mb)


class _PreciseIndex(StateIndex):
    """`StateIndex` with its lookup cache at ~1 cm, so a site near a state line is never answered
    from an earlier point's cache cell."""

    GRID = 10_000_000.0


@lru_cache(maxsize=1)
def _states() -> _PreciseIndex:
    doc = json.loads((REGIONS_DIR / "us_states.geojson").read_text(encoding="utf-8"))
    return _PreciseIndex(doc.get("features", []))


def placement(cases: Sequence[Mapping[str, Any]], state: str) -> tuple[str, float | None, float | None]:
    """(placement, lat, lon) of a site: the centroid of its permanent structures (else of all its
    cases), `exact` only when every case's accuracy class is 1-5 and the point is inside `state`."""
    permanent = [c for c in cases if str(c.get(DURATION) or "").strip() == "Permanent"]
    pts = [
        (lat, lon)
        for c in (permanent or cases)
        for lat, lon in [(_float(c.get(LAT)), _float(c.get(LON)))]
        if lat is not None and lon is not None and not (lat == 0.0 and lon == 0.0)
    ]
    if not pts:
        return "no_coordinate", None, None
    if any(str(c.get(ACCURACY) or "")[:1] not in EXACT_ACCURACY_CLASSES for c in cases):
        return "accuracy_not_stated_or_coarse", None, None
    lat = round(sum(p[0] for p in pts) / len(pts), COORD_DECIMALS)
    lon = round(sum(p[1] for p in pts) / len(pts), COORD_DECIMALS)
    if state and _states().lookup(lon, lat) != f"US-{state}":
        return "point_outside_state", None, None
    return "exact", lat, lon


def site_name(cases: Sequence[Mapping[str, Any]]) -> str:
    """The most telling structure name of a site (module docstring); "" when none is usable."""

    def rank(c: Mapping[str, Any]) -> int:
        name = str(c.get(NAME) or "")
        if DATA_CENTRE_RE.search(name):
            return 0
        if OPERATOR_RE.search(name) or CAMPUS_CODE_RE.search(name):
            return 1
        if str(c.get(TYPE) or "").upper().startswith("BUILDING"):
            return 2
        return 3

    named = [c for c in cases if str(c.get(NAME) or "").strip() and not names_a_person(str(c.get(NAME)))]
    if not named:
        return ""
    best = min(named, key=lambda c: (rank(c), _order(c)))
    return " ".join(str(best[NAME]).split())


def display_name(name: str, city: str, state: str) -> str:
    """The record's name: the chosen structure name when it names a data centre, an operator or a
    campus code; otherwise that name with its place ("Building B (Marysville data centre)"), so a
    list never shows a bare "Crane 1"; "Data centre site, <City>" when no structure name is usable."""
    words = city.split(",")[0].strip(" .").split()
    place = " ".join(w if w.isupper() and len(w) <= 3 else w.capitalize() for w in words) or state
    if not name:
        return f"Data centre site, {place}"
    if DATA_CENTRE_RE.search(name) or OPERATOR_RE.search(name) or CAMPUS_CODE_RE.search(name):
        return name
    return f"{name} ({place} data centre)"


def _mode(values: Iterable[str], fallback: str = "") -> str:
    counts = Counter(v for v in values if v)
    if not counts:
        return fallback
    top = max(counts.values())
    return sorted(v for v, n in counts.items() if n == top)[0]


def _archive_of(asn: str) -> tuple[str, int] | None:
    m = _ASN_RE.match(asn)
    return (m.group(2), int(m.group(1))) if m else None


#: The per-case fields a site's raw payload carries: identity, dates, status, structure and point.
#: No description and no sponsor: those were read for selection, not kept.
CASE_FIELDS = (
    ASN,
    STATUS,
    DETERMINATION,
    ENTERED,
    COMPLETION,
    EXPIRATION,
    DATE_BUILT,
    TYPE,
    DURATION,
    WORK_BEGIN,
    WORK_END,
    NAME,
    LAT,
    LON,
    ACCURACY,
    AGL,
)


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.faa.oeaaa.part77_cases"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "json"
    honour_robots: ClassVar[bool] = True  # oeaaa.faa.gov/robots.txt answers 404 (no rule); checked anyway
    status_key: ClassVar[str] = "faa_oeaaa_part77"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    dq_required_fields: ClassVar[tuple[str, ...]] = ("name_canonical", "state", "county")
    key_source_columns: ClassVar[tuple[str, ...]] = REQUIRED_COLUMNS
    personal_data_columns: ClassVar[tuple[str, ...]] = PERSON_COLUMNS

    # ------------------------------------------------------------------ fetch
    def _years(self) -> list[int]:
        year = self.now().astimezone(dt.UTC).year
        return list(range(year - YEARS_FETCHED + 1, year + 1))

    def _download(self, url: str) -> tuple[IO[bytes], int, dict[str, str]]:
        r = self.http.get(url, honour_robots=self.honour_robots, timeout=600, stream=True)
        try:
            if r.status_code != 200:
                raise ConnectorError(f"GET {url} -> HTTP {r.status_code}")
            ctype = str(r.headers.get("Content-Type") or "").lower()
            if "html" in ctype:
                raise ParseError(f"GET {url} answered HTML, not an archive")
            fh = tempfile.TemporaryFile()
            size = 0
            try:
                for chunk in r.iter_content(DOWNLOAD_CHUNK):
                    fh.write(chunk)
                    size += len(chunk)
                fh.seek(0)
            except BaseException:
                fh.close()
                raise
            return (
                fh,
                size,
                {k: str(v) for k, v in dict(r.headers).items() if k.lower() in ("content-type", "date")},
            )
        finally:
            close = getattr(r, "close", None)
            if close is not None:
                close()

    def _site_notice(self) -> str | None:
        """The OE3A site's shutdown banner, when the site shows it. The banner sits in the home
        page's template whatever the day; the page shows it only while the furlough flag is on, and
        the page's own script reads that flag from `FURLOUGH_FLAG_URL` (a JSON answer,
        `{"enableOegFurloughPublicMsg": false}` on 2026-10-24). So the flag is read first and the
        home page only when it is on. A failure here is logged and ignored: the notice is a health
        note, the archives are the data."""
        try:
            r = self.http.get(FURLOUGH_FLAG_URL, honour_robots=self.honour_robots, timeout=60)
            flag = json.loads(r.text).get(FURLOUGH_FLAG) if r.status_code == 200 else None
        except Exception as e:  # health note only; never fails the run
            log.warning("OE3A furlough flag not read: %r", e, extra={"source_id": self.source_id})
            return None
        if flag is not True:
            return None
        try:
            r = self.http.get(HOME_URL, honour_robots=self.honour_robots, timeout=60)
            text = r.text if r.status_code == 200 else ""
        except Exception as e:  # health note only; never fails the run
            log.warning("OE3A home page not read: %r", e, extra={"source_id": self.source_id})
            text = ""
        banner = re.search(r'id="furloughMessage".*?</div>\s*</div>', text, flags=re.S | re.I)
        notes = [
            " ".join(re.sub(r"<[^>]+>", " ", m).split())
            for m in re.findall(r"<b>(.*?)</b>", banner.group(0) if banner else "", flags=re.S | re.I)
            if re.search(r"shutdown|unable to accept|not accepting|suspend|presumed", m, re.I)
        ]
        return (" | ".join(notes)[:500] or None) or "OE3A furlough banner on (its text was not read)"

    def _previous_doc(self) -> dict[str, Any] | None:
        prev = self.previous
        if prev is None or prev.meta.get("fetch_version") != FETCH_VERSION:
            return None
        try:
            doc = json.loads(prev.content())
        except Exception as e:  # an unusable stored object costs carried cases and memory, no more
            log.warning("previous snapshot not reusable: %r", e, extra={"source_id": self.source_id})
            return None
        if not isinstance(doc, dict) or list(doc.get("columns") or []) != list(SNAPSHOT_COLUMNS):
            return None
        return doc

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        before = self.http.requests_made
        today = self.now().astimezone(dt.UTC).date()
        years = self._years()
        prev_doc = self._previous_doc()
        archives: list[dict[str, Any]] = []
        header: list[str] = []
        files: list[tuple[str, IO[bytes]]] = []
        candidates: set[str] = set()
        empty: set[str] = set()
        try:
            # Pass 1: download each archive once, note its candidates' ASNs.
            for year in years:
                for region in REGIONS:
                    url = archive_url(region, year)
                    fh, size, headers = self._download(url)
                    name = f"{region}{year}"
                    meta: dict[str, Any] = {"archive": name, "url": url, "bytes": size, **headers}
                    archives.append(meta)
                    if size == 0:
                        meta["published"] = False
                        empty.add(name)
                        fh.close()
                        continue
                    files.append((name, fh))
                    text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
                    head, rows, info = read_archive(text)
                    header = header or head
                    for row in rows:
                        if is_candidate(row, str(row.get(SPONSOR) or "")):
                            candidates.add(row[ASN])
                    meta.update(rows_total=info["rows"], data_as_of=info["data_as_of"])
                    text.detach()
            # Carried cases (years no longer fetched, or an archive that came back empty).
            carried: list[list[str]] = []
            fetched = {f"{r}{y}" for y in years for r in REGIONS} - empty
            if prev_doc is not None:
                cols = list(prev_doc["columns"])
                for rec in prev_doc.get("cases") or []:
                    case = dict(zip(cols, rec, strict=False))
                    if str(case.get(ARCHIVE)) in fetched:
                        continue
                    if keep_carried(case, today) or str(case.get(ARCHIVE)) in empty:
                        carried.append([str(case.get(c) or "") for c in SNAPSHOT_COLUMNS])
                        candidates.add(str(case[ASN]))
            # Pass 2: keep the candidates and the cases that cite one.
            kept: list[list[str]] = []
            for name, fh in files:
                fh.seek(0)
                text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
                _, rows, _ = read_archive(text)
                for row in rows:
                    if row[ASN] in candidates or cited_asns(row) & candidates:
                        kept.append(cut_row(row, name))
                text.detach()
        finally:
            for _, fh in files:
                fh.close()
        cases = sorted(kept + carried, key=lambda r: asn_key(r[0]))
        memory = self._memory(prev_doc)
        notice = self._site_notice()
        doc = {
            "fetch_version": FETCH_VERSION,
            "evaluated_on": today.isoformat(),
            "archive_header": header,
            "archives": archives,
            "columns": list(SNAPSHOT_COLUMNS),
            "cases": cases,
            "site_keys": dict(sorted(memory.items())),
        }
        as_of = sorted(str(a["data_as_of"]) for a in archives if a.get("data_as_of"))
        return RawSnapshot(
            content=json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            content_type="application/json",
            url=archive_url(REGIONS[0], years[-1]),
            retrieved_at=self.now().astimezone(dt.UTC),
            http_status=200,
            ext="json",
            headers={},
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=self.http.requests_made - before,
            meta={
                "fetch_version": FETCH_VERSION,
                "years": years,
                "archives_published": len(archives) - len(empty),
                "archives_unpublished": sorted(empty),
                "rows_total": sum(int(a.get("rows_total") or 0) for a in archives),
                "candidates_kept": len(kept),
                "cases_carried": len(carried),
                "data_as_of": [as_of[0], as_of[-1]] if as_of else None,
                "site_notice": notice,
            },
        )

    def _memory(self, prev_doc: Mapping[str, Any] | None) -> dict[str, str]:
        """The site assignment the previous snapshot produced, recomputed with this code."""
        if prev_doc is None:
            return {}
        cases = _cases(prev_doc)
        basis, _ = select_cases(cases)
        selected = [c for c in cases if str(c[ASN]) in basis]
        return assign_sites(selected, dict(prev_doc.get("site_keys") or {}))

    def redact(self, content: bytes) -> bytes:
        """Strip any person column (`PERSON_COLUMNS`) should a snapshot ever carry one; bytes are
        returned untouched when there is none (the fetch never stores them)."""
        try:
            doc = json.loads(content)
        except json.JSONDecodeError:
            return content
        cols = list(doc.get("columns") or []) if isinstance(doc, dict) else []
        drop = [i for i, c in enumerate(cols) if c in PERSON_COLUMNS]
        if not drop:
            return content
        keep = [i for i in range(len(cols)) if i not in drop]
        doc["columns"] = [cols[i] for i in keep]
        doc["cases"] = [[r[i] for i in keep if i < len(r)] for r in doc.get("cases") or []]
        return json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")

    def canonical_content(self, content: bytes) -> bytes:
        """The snapshot without its `evaluated_on` date, plus the cases inactive on that date: a
        later fetch of the same archives is unchanged until a determination expires."""
        try:
            doc = json.loads(content)
            on = dt.date.fromisoformat(str(doc.pop("evaluated_on")))
            cases = _cases(doc)
        except Exception:
            return content
        doc["inactive"] = sorted(str(c[ASN]) for c in cases if not case_active(c, on))
        doc.pop("archives", None)  # per-request sizes and dates
        return json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")

    # ------------------------------------------------------------------ parse
    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        try:
            doc = json.loads(raw.content)
        except json.JSONDecodeError as e:
            raise ParseError(f"FAA Part 77 snapshot is not JSON: {e}") from e
        if not isinstance(doc, dict) or "cases" not in doc or "columns" not in doc:
            raise ParseError("FAA Part 77 snapshot lacks its case table")
        missing = [c for c in SNAPSHOT_COLUMNS if c not in doc["columns"]]
        if missing:
            raise ParseError(f"FAA Part 77 snapshot lacks columns {missing}")
        if any(c in doc["columns"] for c in PERSON_COLUMNS):
            raise ParseError("FAA Part 77 snapshot carries a person column; it must be redacted first")
        on = _date(doc.get("evaluated_on")) or raw.retrieved_at.date()
        cases = _cases(doc)
        cases = [_resanitised(c) for c in cases]
        basis, excluded = select_cases(cases)
        selected = [c for c in cases if str(c[ASN]) in basis]
        keys = assign_sites(selected, dict(doc.get("site_keys") or {}))
        by_site: dict[str, list[dict[str, Any]]] = {}
        for c in selected:
            by_site.setdefault(keys[str(c[ASN])], []).append(c)
        sites = [self._site_row(key, members, basis, on) for key, members in sorted(by_site.items())]
        rows = [r for r in sites if r["status"] not in INACTIVE_STATUSES]
        raw.meta["cases_selected"] = len(selected)
        raw.meta["sites"] = len(rows)
        raw.meta["sites_inactive"] = len(sites) - len(rows)
        raw.meta["selected_by_basis"] = dict(sorted(Counter(basis.values()).items()))
        raw.meta["excluded"] = dict(sorted(excluded.items()))
        raw.meta["source_header"] = list(doc.get("archive_header") or []) or None
        raw.meta["evaluated_on"] = on.isoformat()
        return rows

    def _site_row(
        self, key: str, members: list[dict[str, Any]], basis: Mapping[str, str], on: dt.date
    ) -> dict[str, Any]:
        members = sorted(members, key=_order)
        founder = next((c for c in members if str(c[ASN]) == key), members[0])
        state = str(founder.get(STATE) or "").strip().upper()
        where, lat, lon = placement(members, state)
        bases = Counter(basis[str(c[ASN])] for c in members)
        status = site_status(members, on)
        active = [c for c in members if case_active(c, on)]
        expiries = [e for c in active if (e := expires_on(c)) is not None]
        built = sorted(str(c[DATE_BUILT]) for c in members if built_reported(c))
        received = sorted(
            str(c.get(RECEIVED) or c.get(ENTERED) or "") for c in members if c.get(RECEIVED) or c.get(ENTERED)
        )
        archive = _archive_of(key) or _archive_of(str(founder[ASN])) or (REGIONS[0], on.year)
        row: dict[str, Any] = {
            "site_key": key,
            "select_basis": min(bases, key=BASES.index),
            "bases": dict(sorted(bases.items())),
            "case_count": len(members),
            "active_cases": len(active),
            "asns": [str(c[ASN]) for c in sorted(members, key=lambda c: asn_key(str(c[ASN])))],
            "name": site_name(members),
            "state": state,
            "city": _mode((str(c.get(CITY) or "").strip() for c in members), str(founder.get(CITY) or "")),
            "county_faa": str(founder.get(COUNTY) or "").strip() or None,
            "placement": where,
            "status": status,
            "evaluated_on": on.isoformat(),
            "first_received": received[0] if received else None,
            "last_entered": max(str(c.get(ENTERED) or "") for c in members) or None,
            "active_until": max(expiries).isoformat() if expiries else None,
            "date_built": built[-1] if built else None,
            "structure_types": dict(sorted(Counter(str(c.get(TYPE) or "") for c in members).items())),
            "max_agl_ft": max((f for c in members if (f := _float(c.get(AGL))) is not None), default=None),
            "sponsor_organisations": sorted({str(c[SPONSOR_ORG]) for c in members if c.get(SPONSOR_ORG)}),
            "source_archive": archive_url(*archive),
            "cases": [
                {
                    **{f: (c.get(f) or None) for f in CASE_FIELDS},
                    "basis": basis[str(c[ASN])],
                    "active": case_active(c, on),
                    "existing_work": existing_work(c),
                    "work_ended": work_ended(c, on),
                    "archive": c.get(ARCHIVE) or None,
                }
                for c in members
            ],
        }
        if where == "exact":
            row["Latitude"], row["Longitude"] = lat, lon
        return row

    # ------------------------------------------------------------------ normalize
    def _harmonise(self, labels: Iterable[Any]) -> list[tuple[str, str]]:
        return [harmonise_status(self.status_key, {"status_raw": s}, self.status_map) for s in labels]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        harmonised = self._harmonise(r["status"] for r in rows)
        counties: list[str | None] = []
        for r in rows:
            c = (
                county_for_point(float(r["Longitude"]), float(r["Latitude"]))
                if r.get("Latitude") is not None
                else None
            )
            counties.append(c or r.get("county_faa"))
        names = [display_name(r["name"], r["city"], r["state"]) for r in rows]
        df = pd.DataFrame(
            {
                "source_record_id": [r["site_key"] for r in rows],
                "source_url": [r["source_archive"] for r in rows],
                "kind": "load",
                "name_canonical": names,
                "name_norm": [norm_name(n) for n in names],
                "sponsor_name": None,
                "sponsor_norm": None,
                "technology": "load",
                "technology_raw": "Data Center",
                "capacity_mw": pd.array([None] * len(rows), dtype="Float64"),
                "storage_mwh": pd.array([None] * len(rows), dtype="Float64"),
                "iso": None,
                "state": [r["state"] or None for r in rows],
                "county": counties,
                "county_norm": [norm_county(c) for c in counties],
                "lifecycle_state": [s for s, _ in harmonised],
                "status_raw": [r["status"] for r in rows],
                "status_rule": [rule for _, rule in harmonised],
                "status_conflict": False,
                "queue_date": [
                    to_date(r["first_received"]) if r.get("first_received") else pd.NaT for r in rows
                ],
                "proposed_cod": pd.NaT,
                "queue_id": None,
                "eia_plant_id": None,
                "eia_generator_id": None,
                "cross_refs": ["|".join(f"faa_asn:{a}" for a in r["asns"]) for r in rows],
            },
            index=range(len(rows)),
        )
        return self.finalize(df, rows, raw)

    def restate_status(self, df: pd.DataFrame) -> pd.DataFrame | None:
        """`lifecycle_state`/`status_rule` from each stored row's `status_raw` under the current map."""
        if "status_raw" not in df.columns:
            return None
        harmonised = self._harmonise(df["status_raw"].tolist())
        return pd.DataFrame(
            {"lifecycle_state": [s for s, _ in harmonised], "status_rule": [r for _, r in harmonised]},
            index=df.index,
        )


def _resanitised(case: dict[str, Any]) -> dict[str, Any]:
    """A stored case put through the personal-data rules again, so a snapshot an older fetch wrote
    (a re-parse) never reaches a record with a name the current rules refuse."""
    return {
        **case,
        NAME: clean_name(case.get(NAME)),
        DESCRIPTION: scrub_text(case.get(DESCRIPTION)),
        LOCATION: scrub_text(case.get(LOCATION)),
        SPONSOR_ORG: organisation_name(case.get(SPONSOR_ORG)) or "",
    }


def _cases(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    cols = list(doc.get("columns") or [])
    out = []
    for rec in doc.get("cases") or []:
        case = dict(zip(cols, rec, strict=False))
        if _ASN_RE.match(str(case.get(ASN) or "")):
            out.append(case)
    return out
