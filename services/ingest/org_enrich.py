"""Company facts a company page shows -- official website, headquarters, a one-line description,
stock ticker and exchange, LEI and a Wikipedia link -- from Wikidata (`global.wikidata`, CC0) and
SEC EDGAR's company-ticker file (`us.sec.company_tickers`, a US federal work), written to
`organization.ids` and `organization.website` (no migration; lane org-enrich, 2026-10-22).

Two phases, so the network work never holds a database transaction or the store-wide lock:

    collect_targets(session) -> [Target]                 one read
    fetch_profiles(None, transport, targets=...) -> FetchResult    network only
    apply_profiles(session, result) -> ApplyReport       writes; no network

`run(session, transport)` does all three; the scheduler runs them as two jobs (`infra/scheduler`
`org_enrich_fetch` -> `org_enrich_apply`, monthly) with the decisions file between them.

**Who is enriched.** Live (`merged_into_id IS NULL`), public organisations that hold an asset (an
`asset_owner` edge as owner or operator), sponsor a proposal or issue an opportunity. Natural
persons never: `personal_data` rows are not looked up, and a profile left on a row later classified
as a person is removed by the next apply. A Wikidata item that is an instance of human (Q5) is never
attached, whatever rule found it.

**Matching must not attach the wrong company.** Rules run in this order and the first that decides
wins; every decision records the rule, a confidence and the reasons behind it:

1. `lei` -- the organisation's `ids["lei"]` (GLEIF) equals a Wikidata item's LEI (P1278). An
   identifier match: confidence 1.0, one item or nothing (two items with one LEI is `ambiguous`),
   and a `conflict` when the item's country contradicts the evidence: Wikidata has put a
   subsidiary's LEI on its parent's item (`Johnson Controls, Inc.`, US, on Johnson Controls
   International plc, IE). The same happens within one country, so an item that carries more than
   one LEI must also carry the organisation's name key, else it is a `conflict` too: Public Service
   Enterprise Group's item carries PSE&G's LEI, and Phillips 66's carries Phillips 66 Partners'
   (both read at api.gleif.org, 2026-10-22).
2. `sec_cik` -- the organisation's `ids["cik"]`, or else its `org_key` (`pipeline.normalize`, the one
   organisation key the resolver uses) equals the key of exactly one SEC registrant in the ticker
   file. The CIK then joins Wikidata through P5531, an identifier join. A key match is scored
   (`SEC_*` weights): it requires US among the organisation's evidence countries (the countries of
   its own proposals and assets, `services.ingest.organizations.evidence_countries`; the
   `organization.country` column is a hard-coded `US`), it is marked down when the two names carry
   different legal forms (`Marathon Petroleum Company LP` is not `Marathon Petroleum Corp`) or only
   the organisation's carries one (`Phillips 66 Company`, the operating subsidiary, is not the
   registrant `Phillips 66`; GLEIF has the two as separate legal entities), for a
   one-word key and for a registrant listed only over the counter, and it is lifted to
   `SEC_CORROBORATED` when the CIK's Wikidata item carries the same name key. A CIK whose Wikidata
   item names a country outside the evidence is a `conflict` and nothing is attached (`National
   Grid`, a New York queue sponsor, is not the London-listed `NATIONAL GRID PLC`). "Held by the
   row" means a register's or an operator's: this module never writes `ids` outside the profile, so
   last month's scored match never becomes this month's certainty.
3. `name_country` -- an exact normalised name match against Wikidata. Candidates are the items
   whose English or multilingual label, alias or official name (P1448) is one of the name's
   spellings (`name_variants`: as written, without legal form, and re-cased when the register
   writes in capitals), looked up on the Query Service; for an organisation with no such item, a
   `wbsearchentities` search on the name, within `SEARCH_BUDGET`. Either way the candidate's name
   must have the organisation's `org_key` (or one of its aliases' keys). Gates: the item must be an
   organisation (P31/P279* reaches Q43229), its country (P17, else the country of its headquarters
   P159) must not contradict the evidence, and it must be the only such candidate (two that both
   agree is `ambiguous`). Two more gates, each measured on the 2026-10-20 store (lane org-enrich):
   a name that ends in a legal form (`Calipatria, LLC`, `Corn LP`, `Bailey Farm, LLC`) is a legal
   entity, so its item needs a P31 class that is an organisation and not a place or a physical thing
   (`PHYSICAL_CLASSES`), and none that is physical and not a business -- project companies are
   named after towns, counties, farms and malls, and Wikidata files cities under "municipal
   corporation"; and a place (a settlement or a county: a city's or a county's government is
   recorded as the place) must lie in a state or region the evidence names (P131 up to P300, ISO
   3166-2, against the assets' `state_code` and the proposals' `jurisdiction`), so `CITY OF
   ARLINGTON` with a landfill in Texas is not Arlington, Washington. Any other organisation that is
   not a business (a county government, an authority, a hospital), and any item for a locally scoped
   name (`local_scope`: county, city, school, hospital, authority, board, district, university and
   the like), is held to the same where Wikidata locates it (`_Aux.located`: P131, or its
   headquarters P159 up P131): `MONTGOMERY COUNTY GOVERNMENT` with a Tennessee asset is not
   Montgomery County, Maryland's government, nor `St. Elizabeth Medical Center` (New York) St.
   Elizabeth Healthcare (Kentucky). A locally scoped name also needs an item Wikidata locates at
   all, by statement or by naming the state in its label or description (`named_subdivisions`):
   `Sheridan School Corporation`, an Indiana school district, is not Sheridan School, "independent
   school", with no place. A Wikimedia disambiguation page among the exact-name candidates competes
   for a local body's name unless Wikidata places every remaining candidate in the evidence's state;
   for a business of an ordinary name it says little (Calpine, Cargill and Wells Fargo each have
   one). Measured on the 2026-10-20 store's replayed recording (lane org-enrich, 2026-10-22): these
   refuse 3 matches the earlier gates made, all wrong (the three above), and 2 that were right
   (Orlando Utilities Commission, Holland Board of Public Works: Wikidata neither locates them nor
   names their state), and settle one name that was ambiguous (`THE MOUNT SINAI MEDICAL CENTER`, New
   York: the Miami Beach and Chicago hospitals of that name are refused). Scored by `W_*`: label 1.0 /
   alias `W_ALIAS`; country agrees 1.0 / unknown `W_COUNTRY_UNKNOWN` / the item also names a country
   outside the evidence `W_COUNTRY_PARTIAL`; business 1.0 / other organisation `W_OTHER_ORG`;
   one-word key `W_SINGLE_TOKEN` (three letters or fewer `W_SHORT_TOKEN`); dissolved (P576)
   `W_DISSOLVED`; an item with no English description, no English Wikipedia article and no website
   `W_THIN` (nothing a reader could check the match against).

Below `MATCH_THRESHOLD` nothing is written and the decision is reported as `below_threshold` with
its reasons. An identifier that disagrees with one the row already holds (a Wikidata LEI or CIK
against the row's own) is a `conflict`. A CIK read from the matched item (P5531, rules 1 and 3) is
kept only when SEC names that registrant with the organisation's key (`_item_cik`): an item can
merge a utility and its holding company. After an SEC key match with no CIK item, a Wikidata item
found by name is attached only if it is a business and, for a one-word key, by its label: SEC has
decided which company it is, and a same-named nonprofit (`Mdu Resources Group Inc`, an IRS import)
or a retailer whose alias is the key (`Anderson's`) is not it. The ticker needs two sources: an
SEC symbol on a main board (`MAIN_SEC_EXCHANGES`, no `-` suffix) for the verified CIK, and the
matched Wikidata item listed on a stock exchange (P414) under that same symbol (P249), because
utility subsidiaries and federal corporations carry exchange-traded preferreds and notes (`ELC`,
`GPJA`, `TVE`) under their CIK. When SEC and Wikidata name different symbols nothing is shown
(Georgia Power: SEC `GPJA`, a junior subordinated note; Wikidata `GAR`); only when the item's
listing names no symbol at all is SEC's primary main-board symbol shown (Waste Connections `WCN`).
Without a CIK, a US organisation's Wikidata listing is shown only when SEC lists that symbol under
a registrant of its name (Wikidata gives Georgia Power `GAR`, PG&E's utility its parent's `PCG`,
Southern Company Gas a `GAS` delisted in 2016); abroad, where SEC is silent, Wikidata's listing
stands alone. The LEI shown is Wikidata's (P1278), and only when the item states one (one truthy
value: a preferred-rank value wins over normal ones) or one of them is the row's own GLEIF LEI:
an item that merges a parent and its subsidiary states both (Air Products & Chemicals' item also
carries Air Products Manufacturing Corporation's LEI, PPL's a lapsed French "PPL"'s), and which is
which is not on the item. The row's own GLEIF LEI stays in `ids["lei"]` and is never re-labelled
as a Wikidata fact. The website is the item's one official website (P856); of several, the one
homepage that is not a sub-host of another (`my.xcelenergy.com` is Xcel Energy's customer login
beside `www.xcelenergy.com`) or whose domain is the name (`ameren.com`, not `amereninvestors.com`),
else none (`official_website`). A headquarters outside the evidence's countries on an item whose
own country (P17) is inside them is withheld (`ALON USA LP`, Texas, given Hof HaSharon, Israel):
one of the two statements is wrong. Headquarters is P159 climbed up P131 (`HQ_CLIMB`) to the first
settlement, so a building or a square reads as its city; a P31 class counts as a settlement only if it reaches
neither neighbourhood nor county (Wikidata files Las Vegas under "college town", a neighbourhood,
and Nevada's counties under municipality); a street is not climbed (Wilshire Boulevard is "in"
Beverly Hills, and Macerich is in Santa Monica), nor a place located in more than one place. When
an item states several current headquarters, a city is shown only if they agree (NRG: Princeton
and Houston, neither marked current), else the country if they agree on that. A dissolved item's
website is withheld (it is the defunct company's), and a one-word description says nothing.

**Storage, provenance, reversibility.** `ids["profile"]` holds every field as `{value, source_id,
source_url, retrieved_at, licence_id}`, the match (`rule`, `confidence`, `wikidata_id`, `cik`,
`reasons`) and the website this module wrote (`website_written`). Nothing else in `ids` is written:
the matched Wikidata item and CIK are the profile's, served with their confidence as
`profile.match`, never as identifiers the row holds (`ids` is the registers' and operators', and
the web page's JSON-LD prints it as identifiers). `organization.website` is written only when empty
or when this module wrote the value it holds, so a curated website is never overwritten; one an
operator cleared after this module wrote it is not written back, nor shown in the profile, while
the match is the same item (`website_refused`). A later run that decides `no_match`,
`below_threshold`, `ambiguous` or `conflict` removes exactly what was written; a `lookup_failed`
keeps the previous profile. A row that is no longer a target (merged, not public, a natural person,
or without assets, proposals and opportunities) loses its profile at the next apply, decision or
not. `last_changed` moves only when a shown value moves. The public organisation routes of
`services/api` strip `profile` from `ids` and serve it, gated per field by the source's
visibility, as `GET /v1/organizations/{id}` `profile`; a `website` this module wrote is gated the
same way on every organisation surface (`serialize.organization_website`). An admin edit that
echoes `ids` keeps the profile (`admin_records.admin_update_organization`).

**Measured, and the known limit.** `data/eval/org_enrich_metrics.json` holds the counts by rule and
status on the 2026-10-20 store, and the hand checks: every match below 0.95 (103: 96 correct, 6
related, 1 uncertain, none wrong; `org_enrich_below95.csv`), a seeded sample of 40 matches (40
correct), the top 50 organisations by assets, and the decisions each review round changed
(`org_enrich_audit.csv`). The 6 related are the known limit: Wikidata files a subsidiary's or a
former name as an alias of its parent's item (`Avangrid Renewables`, `Westar Energy`, `Regions
Bank`), and nothing on the item says which legal entity the alias was. Refusing an alias match
whose item another organisation holds by label or identifier would remove 3 of the 6, and with
them 13 correct ones (former names and abbreviations: `Iberdrola USA`, `Dominion Resources`, `NY
Power Authority`), so it is not done.

**Routes and politeness.** The Wikidata Query Service (`https://query.wikidata.org/sparql`) does the
bulk work in batched queries: identifier joins (P1278, P5531), exact-name candidates, each item's
statements, terms and English Wikipedia link, and class reachability -- a few hundred queries for
eight thousand organisations, where the Action API would need one search per organisation. The
manual documents the endpoint for programs ("SPARQL queries can be submitted directly to the SPARQL
endpoint with a GET or POST request") and limits each client to "60 seconds of processing time each
60 seconds"; queries go one at a time, and after each the client waits at least as long as it took
(`SPARQL_RPS` floor), so it never uses more than half of that. The Action API (`wbsearchentities`)
is the fallback only: at most `SEARCH_BUDGET` requests and `SEARCH_MINUTES` a run, half of the
budget on the largest organisations every month and the other half moving on through the rest month
by month (`search_order`), one at a time at `WIKIDATA_RPS` (the registry row's `rate_limit_rps`),
`maxlag=5`, `Retry-After` honoured, five
seconds' pause after any request slower than one second (Robot policy), and abandoned for the run
after `SEARCH_FAILURE_LIMIT` consecutive failures. An organisation the fallback does not reach is
`lookup_failed`, which keeps its previous profile. Both Wikimedia hosts get one User-Agent with a URL
and an email (Wikimedia Foundation User-Agent Policy; a browser-like one is "assumed malicious").
robots.txt disallows `/sparql` on query.wikidata.org and `/w/` on www.wikidata.org to every agent;
both are API endpoints that Wikimedia's own policies address to programs, and the platform does not
treat API endpoints as robots-relevant (`pipeline/connectors/http.py`); the registry row
(`data/sources.yaml` `global.wikidata`) records the clauses and the open question. SEC: one request
for the ticker file per run, a declared `Company email` User-Agent, at most `SEC_RPS` (fair access:
10 per second).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import pathlib
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.connectors.http import DOMAIN_PLACEHOLDER, HttpBlocked, HttpFailed, PoliteSession
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import DATA_DIR
from pipeline.normalize import org_key
from services.db.models import Asset, AssetOwner, Opportunity, Organization, OrganizationAlias, Proposal
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import upsert_licence_and_source
from services.ingest.organizations import evidence_countries

log = logging.getLogger(__name__)

WIKIDATA_SOURCE_ID = "global.wikidata"
SEC_TICKERS_SOURCE_ID = "us.sec.company_tickers"
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
WIKIDATA_ITEM_URL = "https://www.wikidata.org/wiki/{qid}"
WIKIDATA_ENTITY_PREFIX = "http://www.wikidata.org/entity/"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_COMPANY_URL = "https://www.sec.gov/edgar/browse/?CIK={cik}"
#: The decisions file the scheduler's fetch job leaves for the apply job, under the connector data
#: root's `normalized/context/` (beside the GLEIF parents parquet).
DECISIONS_FILENAME = "org_enrich.decisions.json"
DEFAULT_DECISIONS_PATH = DATA_DIR / "normalized" / "context" / DECISIONS_FILENAME
DEFAULT_DB_PATH = pathlib.Path("web/.data/dev.db")

PROFILE_KEY = "profile"
PROFILE_VERSION = 1
#: The fields the profile carries, in the order the API serves them.
PROFILE_FIELDS: tuple[str, ...] = ("website", "headquarters", "description", "ticker", "lei", "wikipedia")
RULES: tuple[str, ...] = ("lei", "sec_cik", "name_country")
#: A decision below this confidence writes nothing.
MATCH_THRESHOLD = 0.85

# Rule 3 (`name_country`) weights; the confidence is their product (module docstring).
W_ALIAS = 0.92
W_COUNTRY_UNKNOWN = 0.75
W_OTHER_ORG = 0.95
W_SINGLE_TOKEN = 0.95
W_SHORT_TOKEN = 0.80
W_DISSOLVED = 0.90
#: The item's countries meet the evidence but also name one outside it (Verso Books: GB and US).
W_COUNTRY_PARTIAL = 0.92
#: No English description, no English Wikipedia article and no website: nothing to check against.
W_THIN = 0.90
#: Two organisation candidates share the key but only one has a country that agrees.
W_COUNTRY_TIEBREAK = 0.90
# Rule 2 (`sec_cik`) weights for an SEC name-key match.
SEC_BASE = 0.97
SEC_CORROBORATED = 0.99
W_SEC_OTC_ONLY = 0.85
W_LEGAL_FORM_MISMATCH = 0.80

#: SEC exchange values that are a stock exchange's main board (`company_tickers_exchange.json`
#: carries `NYSE`, `Nasdaq`, `CBOE`, `TXSE`, `OTC` and null, measured 2026-10-22).
MAIN_SEC_EXCHANGES = frozenset({"NYSE", "Nasdaq", "CBOE", "TXSE"})
#: Wikidata classes. Organisation is the gate; business/company/enterprise decide `W_OTHER_ORG`.
ORGANIZATION_CLASS = "Q43229"
BUSINESS_CLASSES = frozenset({"Q4830453", "Q783794", "Q6881511"})
HUMAN_CLASS = "Q5"
#: A headquarters "city" must be one of these (human settlement, municipality, city, town); a
#: building, a square or a neighbourhood is climbed up its P131 (located in) to the first that is.
SETTLEMENT_CLASSES = frozenset({"Q486972", "Q15284", "Q515", "Q3957"})
#: ...but a neighbourhood (Q123705) is part of a city, not the city (Phillips 66: Westchase, Houston),
#: and a county (Q28575) is not a city although Wikidata files US counties under municipality. Both
#: are judged per P31 class: Las Vegas is a "college town", which reaches neighbourhood, and a city.
NEIGHBOURHOOD_CLASS = "Q123705"
COUNTY_CLASS = "Q28575"
#: A street (thoroughfare) is "located in" one of the places it runs through; never climbed.
THOROUGHFARE_CLASS = "Q83620"
#: An item of a settlement or county class is a place: a city's or county's government recorded as
#: the place itself. It must lie in a subdivision the evidence names (module docstring).
PLACE_CLASSES = frozenset({"Q486972", COUNTY_CLASS})
#: Places and physical things: human settlement, county, administrative territorial entity,
#: geographic location, facility, architectural structure, building. A name with a legal form is a
#: legal entity and is never matched to an item whose P31 is only one of these.
PHYSICAL_CLASSES = frozenset({*PLACE_CLASSES, "Q56061", "Q2221906", "Q13226383", "Q811979", "Q41176"})
#: Wikimedia disambiguation page (and its "human name" subclass): an exact-label candidate that is
#: one says the name is shared (`St. Elizabeth Medical Center`), so it competes for the name.
DISAMBIGUATION_CLASSES = frozenset({"Q4167410", "Q22808320"})
#: Words that make a name a local body's: a county, a city, a school district, a hospital, a utility
#: board or an authority acts where it is, and the same name recurs state by state (`Sheridan School
#: Corporation`, an Indiana school district, is not Sheridan School in Washington, DC). Such a name
#: is matched only to an item Wikidata locates (`_Aux.located`) in a state the evidence names.
LOCAL_SCOPE_WORDS = frozenset(
    {
        "authority",
        "board",
        "borough",
        "city",
        "college",
        "commission",
        "counties",
        "county",
        "department",
        "district",
        "government",
        "hospital",
        "hospitals",
        "medical",
        "municipal",
        "municipality",
        "parish",
        "school",
        "schools",
        "town",
        "township",
        "university",
        "village",
    }
)
#: English names of the subdivisions the store's evidence names (assets' `state_code`, proposals'
#: `jurisdiction`: US codes only on the 2026-10-20 store), for a local body Wikidata does not
#: locate by statement but whose label or description names its state (`New York Power Authority`;
#: the Lower Colorado River Authority, "public utility in Texas").
US_SUBDIVISION_NAMES = {
    "US-AK": "Alaska",
    "US-AL": "Alabama",
    "US-AR": "Arkansas",
    "US-AZ": "Arizona",
    "US-CA": "California",
    "US-CO": "Colorado",
    "US-CT": "Connecticut",
    "US-DC": "District of Columbia",
    "US-DE": "Delaware",
    "US-FL": "Florida",
    "US-GA": "Georgia",
    "US-HI": "Hawaii",
    "US-IA": "Iowa",
    "US-ID": "Idaho",
    "US-IL": "Illinois",
    "US-IN": "Indiana",
    "US-KS": "Kansas",
    "US-KY": "Kentucky",
    "US-LA": "Louisiana",
    "US-MA": "Massachusetts",
    "US-MD": "Maryland",
    "US-ME": "Maine",
    "US-MI": "Michigan",
    "US-MN": "Minnesota",
    "US-MO": "Missouri",
    "US-MS": "Mississippi",
    "US-MT": "Montana",
    "US-NC": "North Carolina",
    "US-ND": "North Dakota",
    "US-NE": "Nebraska",
    "US-NH": "New Hampshire",
    "US-NJ": "New Jersey",
    "US-NM": "New Mexico",
    "US-NV": "Nevada",
    "US-NY": "New York",
    "US-OH": "Ohio",
    "US-OK": "Oklahoma",
    "US-OR": "Oregon",
    "US-PA": "Pennsylvania",
    "US-PR": "Puerto Rico",
    "US-RI": "Rhode Island",
    "US-SC": "South Carolina",
    "US-SD": "South Dakota",
    "US-TN": "Tennessee",
    "US-TX": "Texas",
    "US-UT": "Utah",
    "US-VA": "Virginia",
    "US-VT": "Vermont",
    "US-WA": "Washington",
    "US-WI": "Wisconsin",
    "US-WV": "West Virginia",
    "US-WY": "Wyoming",
}
#: Descriptions that tell a reader nothing (Wikidata gives Dominion Energy and Kinder Morgan `company`);
#: any one-word description is dropped too (`article`, `organization`).
GENERIC_DESCRIPTIONS = frozenset(
    {"company", "business", "corporation", "enterprise", "organization", "organisation", "public company"}
)
HQ_CLIMB = 2
#: P131 levels read above a place, so a building reaches its city, county and state (P300).
PLACE_CLIMB = 3
#: Exchange items whose short name is what a reader expects to see.
EXCHANGE_SHORT_NAMES = {"Q13677": "NYSE", "Q82059": "Nasdaq", "Q1072876": "NYSE American"}

#: Query Service pacing: at most one query a second, and after each a pause at least as long as it
#: ran, so at most half of the "60 seconds of processing time each 60 seconds" a client is allowed.
#: Both Wikimedia rates are the `global.wikidata` registry row's `rate_limit_rps` and SEC's its row's
#: (`test_the_request_rates_are_the_registry_rows`).
SPARQL_RPS = 1.0
WIKIDATA_RPS = 1.0
SEC_RPS = 5.0
#: Robot policy, Action API rules: "if your request takes more than 1 second to serve, please wait
#: 5 seconds before making another request".
SLOW_REQUEST_S = 1.0
SLOW_REQUEST_PAUSE_S = 5.0
MAXLAG_S = "5"
#: Name searches a run may make (about 50 minutes at `WIKIDATA_RPS`; `search_order` says which), and
#: the wall-clock minutes the fallback may take: a host answering 429 with long `Retry-After`s
#: is waited out politely, but the monthly job still ends (`infra/scheduler` `org_enrich_fetch`).
SEARCH_BUDGET = 3000
SEARCH_MINUTES = 90.0
#: Consecutive failed searches after which the fallback stops for the run.
SEARCH_FAILURE_LIMIT = 3
SEARCH_LIMIT = 50
#: Items per statements/terms query; literals per name query; values per identifier query.
ENTITY_BATCH = 100
NAME_BATCH = 400
VALUE_BATCH = 200
CLASS_BATCH = 100

#: Statements kept for a matched (or candidate) item, and for the places, exchanges and countries
#: it points at.
ITEM_PROPS: tuple[str, ...] = (
    "P31",
    "P17",
    "P159",
    "P856",
    "P1278",
    "P5531",
    "P414",
    "P249",
    "P576",
    "P1448",
    "P131",
    "P300",
)
AUX_PROPS: tuple[str, ...] = ("P17", "P297", "P31", "P131", "P300")
QUALIFIER_PROPS: tuple[str, ...] = ("P249", "P580", "P582")
_NAME_LANGUAGES = ("en", "mul")
_RANKS = {
    "http://wikiba.se/ontology#PreferredRank": "preferred",
    "http://wikiba.se/ontology#NormalRank": "normal",
    "http://wikiba.se/ontology#DeprecatedRank": "deprecated",
}
_LEI = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
_QID = re.compile(r"^Q[1-9][0-9]*$")
#: An ISO 3166-2 subdivision code (`US-TX`, `GB-SCT`); a bare country code is not one.
_SUBDIVISION = re.compile(r"^[A-Z]{2}-[A-Z0-9]{1,3}$")
#: Legal forms at the end of a name, by family (`legal_form`): a corporation and an LP of one name
#: are two legal entities. `co`/`company` are corporations, as `org_key` already treats them.
_FORM_FAMILY = {
    "inc": "corporation",
    "incorporated": "corporation",
    "corp": "corporation",
    "corporation": "corporation",
    "co": "corporation",
    "company": "corporation",
    "companies": "corporation",
    "llc": "llc",
    "lp": "lp",
    "lllp": "lp",
    "llp": "llp",
    "ltd": "limited",
    "limited": "limited",
    "plc": "plc",
    "pllc": "llc",
}
_TRAILING_FORM = re.compile(
    r"(?:[\s,.]+(?:l\.?\s?l\.?\s?c|inc|incorporated|corp|corporation|co|company|companies|l\.?\s?p|lllp|llp|ltd|"
    r"limited|plc|pllc)\.?)+\s*$",
    re.IGNORECASE,
)
#: Words a title-cased name keeps in lower case after its first word ("Bank of America").
_TITLE_LOWER = frozenset({"of", "and", "the", "for", "de", "du", "des", "la", "le", "on", "at", "in"})


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def _batches(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


# ------------------------------------------------------------------------------------- names
def legal_form(name: str) -> str | None:
    """The family of the last legal form a name ends with (`Marathon Petroleum Company LP` -> `lp`),
    or None when it ends with none."""
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    if not tokens:
        return None
    # Trailing initials spell a form: "L.L.C." -> "llc", "Holdings A, L.P." -> "...alp" -> "lp".
    singles = ""
    for token in reversed(tokens):
        if len(token) != 1 or not token.isalpha():
            break
        singles = token + singles
    if len(singles) >= 2:
        for form in sorted(_FORM_FAMILY, key=len, reverse=True):
            if singles.endswith(form):
                return _FORM_FAMILY[form]
        return None
    return _FORM_FAMILY.get(tokens[-1])


def search_text(name: str) -> str:
    """The name without its trailing legal forms or a leading article, case kept (`NRG Energy, Inc.`
    -> `NRG Energy`): Wikidata labels usually omit both."""
    text = re.sub(r"^\s*the\s+", "", name.strip(), flags=re.IGNORECASE)
    stripped = _TRAILING_FORM.sub("", text).strip(" ,.;")
    return stripped or name.strip()


def _title(text: str, *, keep_short_upper: bool) -> str:
    words = []
    for n, word in enumerate(text.split()):
        lower = word.lower()
        if n and lower in _TITLE_LOWER:
            words.append(lower)
        elif keep_short_upper and word.isalpha() and len(word) <= 3:
            words.append(word.upper())
        else:
            words.append(word[:1].upper() + word[1:].lower())
    return " ".join(words)


def name_variants(name: str) -> list[str]:
    """The spellings a Wikidata label of this name could have, for an exact lookup: the name as
    written, without its legal form, and -- when the register writes it in one case, as EIA and
    GHGRP do -- re-cased two ways (`DUKE ENERGY CORP` -> `Duke Energy`; `CPN MANAGEMENT LP` -> `CPN
    Management`, short words kept as initials)."""
    base = re.sub(r"\s+", " ", name.strip())
    core = search_text(base)
    out = [base, core]
    for text in (base, core):
        letters = [c for c in text if c.isalpha()]
        if letters and (text.upper() == text or text.lower() == text):
            out.append(_title(text, keep_short_upper=False))
            out.append(_title(text, keep_short_upper=True))
    return [v for v in dict.fromkeys(out) if v and len(v) <= 200]


def key_tokens(key: str) -> list[str]:
    return [t for t in key.split() if t]


def local_scope(name: str) -> bool:
    """Whether the name is a local body's (`LOCAL_SCOPE_WORDS`): `MONTGOMERY COUNTY GOVERNMENT`,
    `Sheridan School Corporation`, `St. Elizabeth Medical Center`."""
    return bool(set(re.findall(r"[a-z]+", name.lower())) & LOCAL_SCOPE_WORDS)


def named_subdivisions(entity: Mapping[str, Any], codes: Iterable[str]) -> set[str]:
    """The `codes` whose English name (`US_SUBDIVISION_NAMES`) the item's English or multilingual
    label or English description names as a word: "public power utility in New York". Washington,
    D.C. does not name Washington state, nor West Virginia Virginia."""
    texts = [*(entity.get("labels") or {}).values(), str(entity.get("description") or "")]
    text = " ".join(str(t) for t in texts if t)
    out: set[str] = set()
    for code in codes:
        name = US_SUBDIVISION_NAMES.get(code)
        if not name:
            continue
        here = re.sub(r"Washington,?\s+D\.?\s?C\.?", " ", text) if code == "US-WA" else text
        guard = r"(?<!West )" if code == "US-VA" else ""
        if re.search(rf"{guard}\b{re.escape(name)}\b", here):
            out.add(code)
    return out


# --------------------------------------------------------------------------------- transport
class Transport(Protocol):
    """One JSON request. The live implementation is `LiveTransport`; tests pass
    `RecordedTransport`. A Query Service request carries `query` in `params`."""

    def get_json(self, url: str, params: Mapping[str, str]) -> Any: ...


def request_key(url: str, params: Mapping[str, str]) -> str:
    """A stable key for one request: the URL and its parameters, sorted, without `maxlag` (which
    says how patient we are, not what we asked)."""
    query = urlencode(sorted((k, v) for k, v in params.items() if k != "maxlag"))
    return f"{url}?{query}" if query else url


class RecordedTransport:
    """Answers from recorded exchanges: a list of `{"url", "params", "response"}` (the fixture
    format `RecordingTransport` writes) or a `{request_key: response}` mapping. An unrecorded
    request raises, so a test can never fall through to the network."""

    def __init__(self, recordings: Mapping[str, Any] | Sequence[Mapping[str, Any]] | pathlib.Path) -> None:
        loaded: Any = (
            json.loads(recordings.read_text(encoding="utf-8"))
            if isinstance(recordings, pathlib.Path)
            else recordings
        )
        if isinstance(loaded, Mapping) and "exchanges" in loaded:
            loaded = loaded["exchanges"]
        if isinstance(loaded, Mapping):
            self.recordings: dict[str, Any] = dict(loaded)
        else:
            self.recordings = {request_key(e["url"], e.get("params") or {}): e["response"] for e in loaded}
        self.requests: list[str] = []

    def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        key = request_key(url, params)
        self.requests.append(key)
        if key not in self.recordings:
            raise HttpFailed(f"not recorded: {key[:300]}")
        return self.recordings[key]


class RecordingTransport:
    """Wraps a live transport and keeps every exchange, for writing test fixtures."""

    def __init__(self, inner: Transport) -> None:
        self.inner = inner
        self.exchanges: list[dict[str, Any]] = []

    def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        payload = self.inner.get_json(url, params)
        self.exchanges.append({"url": url, "params": dict(params), "response": payload})
        return payload


def contact(domain: str | None = None, email: str | None = None) -> tuple[str, str]:
    """`(domain, email)` the declared User-Agents name: `BANKABLE_DOMAIN` (the deployment's domain,
    as `pipeline/connectors/http.py`) and `PRODUCT_CONTACT_EMAIL`, else `hello@<domain>` (the
    product address `services/social/editorial.py` already uses). Raises when no real domain is
    configured: both hosts require a contact that reaches someone."""
    domain = domain or os.environ.get("BANKABLE_DOMAIN") or ""
    if not domain or domain == DOMAIN_PLACEHOLDER:
        raise ValueError("set BANKABLE_DOMAIN so the declared User-Agent names a real contact")
    email = email or os.environ.get("PRODUCT_CONTACT_EMAIL", "").strip() or f"hello@{domain}"
    return domain, email


def wikimedia_user_agent(domain: str, email: str) -> str:
    """Wikimedia Foundation User-Agent Policy: `<client>/<version> (<contact>) <library>/<version>`,
    "bot" in the name, a URL and an email, never a browser's string."""
    from importlib.metadata import version

    return (
        f"InframationBot/0.2 (https://{domain}/bot; {email}) org-enrich python-requests/{version('requests')}"
    )


def sec_user_agent(domain: str, email: str) -> str:
    """SEC "Accessing EDGAR Data": "User-Agent: Sample Company Name AdminContact@<sample company
    domain>.com"."""
    return f"Inframation {email}"


class LiveTransport:
    """The three endpoints this module reads, each through its own `PoliteSession` (token bucket,
    retry with backoff, `Retry-After`) with the User-Agent that host's policy asks for. API
    endpoints: `honour_robots=False`, the platform convention (module docstring)."""

    def __init__(
        self,
        *,
        domain: str | None = None,
        email: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        domain, email = contact(domain, email)
        self._sleep = sleep
        self._clock = clock
        wikimedia_ua = wikimedia_user_agent(domain, email)
        self.sparql = PoliteSession(
            rate_limits={"query.wikidata.org": SPARQL_RPS}, max_attempts=6, timeout=90.0, sleep=sleep
        )
        self.sparql.session.headers.update(
            {"User-Agent": wikimedia_ua, "Accept": "application/sparql-results+json"}
        )
        # Few attempts: a search that cannot get through is cheaper to skip than to wait out.
        self.wikidata = PoliteSession(
            rate_limits={"www.wikidata.org": WIKIDATA_RPS}, max_attempts=3, timeout=60.0, sleep=sleep
        )
        self.wikidata.session.headers.update({"User-Agent": wikimedia_ua})
        self.sec = PoliteSession(
            rate_limits={"www.sec.gov": SEC_RPS}, max_attempts=4, timeout=60.0, sleep=sleep
        )
        self.sec.session.headers.update({"User-Agent": sec_user_agent(domain, email)})
        self.slow_pauses = 0
        self.sparql_seconds = 0.0

    def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        host = urlsplit(url).netloc
        if host == "query.wikidata.org":
            return self._sparql(url, params)
        if host.endswith("wikidata.org"):
            return self._wikidata(url, params)
        if host.endswith("sec.gov"):
            return self._json(self.sec.get(url, honour_robots=False, params=dict(params)), host)
        raise ValueError(f"no transport for {host}")

    @staticmethod
    def _json(resp: Any, host: str) -> Any:
        if resp.status_code >= 400:
            raise HttpFailed(f"{host} answered HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpFailed(f"{host} answered a body that is not JSON") from exc

    def _sparql(self, url: str, params: Mapping[str, str]) -> Any:
        """POST (the manual's form for long queries), then wait at least as long as it ran."""
        started = self._clock()
        resp = self.sparql.post(url, data=dict(params))
        elapsed = self._clock() - started
        self.sparql_seconds += elapsed
        payload = self._json(resp, "query.wikidata.org")
        self._sleep(elapsed)
        return payload

    def _wikidata(self, url: str, params: Mapping[str, str]) -> Any:
        query = {"format": "json", "formatversion": "2", "maxlag": MAXLAG_S, **params}
        for _attempt in range(3):
            started = self._clock()
            resp = self.wikidata.get(url, honour_robots=False, params=query)
            if self._clock() - started > SLOW_REQUEST_S:
                self.slow_pauses += 1
                self._sleep(SLOW_REQUEST_PAUSE_S)
            payload = self._json(resp, "www.wikidata.org")
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict) and error.get("code") == "maxlag":
                retry_after = str(resp.headers.get("Retry-After") or "5")
                self._sleep(float(retry_after) if retry_after.isdigit() else 5.0)
                continue
            if isinstance(error, dict):
                raise HttpFailed(f"wikidata error {error.get('code')}: {error.get('info')}")
            return payload
        raise HttpFailed("wikidata: maxlag did not clear after 3 attempts")


# ----------------------------------------------------------------------------------- the SEC file
def _norm_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", symbol.upper())


def _common_symbol(ticker: str) -> bool:
    """A common share's symbol: no `-` suffix, or a one-letter share class other than `P`."""
    if not ticker or "-" not in ticker:
        return bool(ticker)
    suffix = ticker.split("-", 1)[1]
    return len(suffix) == 1 and suffix.isalpha() and suffix.upper() != "P"


#: EDGAR's conformed names end with tags that are not part of the name: the state of incorporation
#: (`ENTERGY CORP /DE/`), `/NEW/`, `/ADR` (measured 2026-10-22: 600 of the file's names, `/ADR` 334,
#: `/DE/` 60, `/MD/` 16, `/DE` 14, then two- to four-letter state and country codes).
_SEC_TAG = re.compile(r"\s*/[A-Z]{2,5}/?\s*$")


def sec_core_name(name: str) -> str:
    """`ENTERGY CORP /DE/` -> `ENTERGY CORP`; every trailing tag, so `X INC /NEW/ /DE/` sheds both."""
    text = name.strip()
    while True:
        stripped = _SEC_TAG.sub("", text).strip()
        if stripped == text or not stripped:
            return text
        text = stripped


@dataclass(frozen=True)
class SecListing:
    cik: str  # ten digits, zero-padded, as EDGAR and Wikidata P5531 write it
    name: str
    ticker: str
    exchange: str | None


@dataclass
class SecTickers:
    """`company_tickers_exchange.json`: `{"fields": ["cik","name","ticker","exchange"], "data": [...]}`,
    one row per ticker, several per CIK, in the file's own order (its first ticker for a CIK is the
    company's primary listing)."""

    by_cik: dict[str, list[SecListing]]
    by_key: dict[str, set[str]]
    retrieved_at: dt.datetime
    by_symbol: dict[str, list[SecListing]] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], retrieved_at: dt.datetime) -> SecTickers:
        fields_ = list(payload.get("fields") or [])
        index = {
            name: fields_.index(name) for name in ("cik", "name", "ticker", "exchange") if name in fields_
        }
        if len(index) != 4:
            raise ValueError(f"unexpected SEC ticker file fields: {fields_}")
        by_cik: dict[str, list[SecListing]] = defaultdict(list)
        by_key: dict[str, set[str]] = defaultdict(set)
        by_symbol: dict[str, list[SecListing]] = defaultdict(list)
        for row in payload.get("data") or []:
            cik = f"{int(row[index['cik']]):010d}"
            listing = SecListing(
                cik=cik,
                name=str(row[index["name"]] or ""),
                ticker=str(row[index["ticker"]] or ""),
                exchange=row[index["exchange"]] or None,
            )
            by_cik[cik].append(listing)
            by_key[org_key(sec_core_name(listing.name))].add(cik)
            by_symbol[_norm_symbol(listing.ticker)].append(listing)
        return cls(
            by_cik=dict(by_cik), by_key=dict(by_key), retrieved_at=retrieved_at, by_symbol=dict(by_symbol)
        )

    def name(self, cik: str) -> str:
        """The registrant's name without EDGAR's trailing tags (`sec_core_name`)."""
        rows = self.by_cik.get(cik) or []
        return sec_core_name(rows[0].name) if rows else ""

    def names(self, cik: str, keys: Iterable[str]) -> bool:
        """Whether the registrant `cik` is, by name key, one of `keys`."""
        return cik in self.by_cik and org_key(self.name(cik)) in set(keys)

    def confirm_symbol(self, symbol: str, keys: Iterable[str]) -> SecListing | None:
        """The main-board listing of `symbol` whose registrant has one of `keys`, if SEC has one."""
        wanted = set(keys)
        rows = self.by_symbol.get(_norm_symbol(symbol)) or []
        return next((r for r in rows if r in self.main_board(r.cik) and self.names(r.cik, wanted)), None)

    def main_board(self, cik: str) -> list[SecListing]:
        """The CIK's listings on a main board, common shares only: SEC writes preferreds, warrants,
        units and rights with a `-` suffix (`DUK-PA`, `OXY-WT`, `ETI-P`), a share class with one letter
        (`BRK-B`, `MOG-A`; measured 2026-10-22: 373 `-P?`, 61 `-W?`, 50 `-U?`, 19 `-R?`, 24 classes)."""
        return [
            row
            for row in self.by_cik.get(cik) or []
            if row.exchange in MAIN_SEC_EXCHANGES and _common_symbol(row.ticker)
        ]


def fetch_sec_tickers(transport: Transport, now: dt.datetime | None = None) -> SecTickers:
    payload = transport.get_json(SEC_TICKERS_URL, {})
    return SecTickers.from_payload(payload, now or utcnow())


# ------------------------------------------------------------------------------------- Wikidata
def _qid(uri: str) -> str | None:
    if uri.startswith(WIKIDATA_ENTITY_PREFIX):
        tail = uri[len(WIKIDATA_ENTITY_PREFIX) :]
        return tail if _QID.match(tail) else None
    return None


def _binding_value(binding: Mapping[str, Any] | None) -> Any:
    """A SPARQL JSON binding as the value this module stores: an item's QID, a URL (P856), or a
    literal's text. An unknown value ("somevalue", a `.well-known/genid/` node) and any other
    entity IRI (a property, a lexeme) are None."""
    if not binding:
        return None
    value = str(binding.get("value") or "")
    if binding.get("type") == "uri":
        if value.startswith(WIKIDATA_ENTITY_PREFIX):
            return _qid(value)
        if "/.well-known/genid/" in value:
            return None
        return value
    if binding.get("type") == "bnode":
        return None
    return value


def _literal(text: str, lang: str | None = None) -> str:
    """A SPARQL string literal (JSON's escapes are a subset of SPARQL's), optionally language-tagged."""
    quoted = json.dumps(text, ensure_ascii=False)
    return f"{quoted}@{lang}" if lang else quoted


def best_values(entity: Mapping[str, Any], prop: str) -> list[Any]:
    """Wikidata's "truthy" values: deprecated dropped, preferred ones if any, else normal; and a
    statement with an end time (P582) dropped, since it is no longer true."""
    return [s["value"] for s in best_statements(entity, prop)]


def best_statements(entity: Mapping[str, Any], prop: str) -> list[dict[str, Any]]:
    statements = [
        s
        for s in (entity.get("claims") or {}).get(prop, [])
        if s.get("rank") != "deprecated" and not s["q"].get("P582")
    ]
    preferred = [s for s in statements if s.get("rank") == "preferred"]
    return preferred or statements


def name_keys(entity: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """`(label keys, alias keys)`: `org_key` of the English and multilingual labels, and of the
    aliases and official names (P1448) in any language this module keeps."""
    labels = {org_key(v) for v in (entity.get("labels") or {}).values() if v}
    aliases = {org_key(a) for values in (entity.get("aliases") or {}).values() for a in values if a}
    aliases |= {org_key(v) for v in best_values(entity, "P1448") if isinstance(v, str)}
    labels.discard("")
    aliases.discard("")
    return labels, aliases - labels


def empty_entity(qid: str) -> dict[str, Any]:
    """The shape an item takes here: labels and aliases in `_NAME_LANGUAGES`, the English
    description, the English Wikipedia URL, and the statements of the props asked for, each
    `{rank, value, q: {qualifier: [values]}}`, sorted by value so a run is deterministic."""
    return {"id": qid, "labels": {}, "aliases": {}, "description": None, "enwiki": None, "claims": {}}


class WikidataClient:
    """Query Service lookups (identifiers, exact names, statements, terms, classes) and the
    `wbsearchentities` fallback over a transport, with an entity cache so no item is read twice in
    a run."""

    def __init__(self, transport: Transport, clock: Callable[[], dt.datetime] = utcnow) -> None:
        self.transport = transport
        self.clock = clock
        self.entities: dict[str, dict[str, Any]] = {}
        self.retrieved: dict[str, dt.datetime] = {}
        self.requests: dict[str, int] = {"sparql": 0, "search": 0}
        self._terms_done: set[str] = set()

    # -- transport
    def sparql(self, query: str) -> list[dict[str, Any]]:
        self.requests["sparql"] += 1
        payload = self.transport.get_json(WIKIDATA_SPARQL_URL, {"query": query, "format": "json"})
        return list(((payload or {}).get("results") or {}).get("bindings") or [])

    def search(self, text: str) -> list[dict[str, Any]]:
        self.requests["search"] += 1
        payload = self.transport.get_json(
            WIKIDATA_API_URL,
            {
                "action": "wbsearchentities",
                "search": text,
                "language": "en",
                "uselang": "en",
                "type": "item",
                "limit": str(SEARCH_LIMIT),
            },
        )
        return list(payload.get("search") or [])

    # -- lookups
    def by_statement(self, prop: str, values: Iterable[str]) -> dict[str, list[str]]:
        """Item ids whose truthy `prop` is each value, read back from the item's own statements
        (`fetch`), so the join never decides an identifier on a value the item no longer states."""
        wanted = sorted({v for v in values if v})
        hits: set[str] = set()
        for chunk in _batches(wanted, VALUE_BATCH):
            rows = self.sparql(
                f"SELECT ?value ?item WHERE {{ VALUES ?value {{ {' '.join(_literal(v) for v in chunk)} }} "
                f"?item wdt:{prop} ?value . }}"
            )
            hits |= {q for q in (_binding_value(r.get("item")) for r in rows) if isinstance(q, str)}
        self.fetch(sorted(hits))
        out: dict[str, list[str]] = defaultdict(list)
        wanted_set = set(wanted)
        for qid in sorted(hits):
            for st in (self.entities.get(qid) or {}).get("claims", {}).get(prop, []):
                value = str(st["value"])
                if st.get("rank") != "deprecated" and value in wanted_set and qid not in out[value]:
                    out[value].append(qid)
        return dict(out)

    def by_names(self, names: Iterable[str]) -> dict[str, list[str]]:
        """`{spelling: [item ids]}` for every item with that exact English or multilingual label or
        alias, or official name (P1448) tagged `en`/`mul`."""
        spellings = sorted({n for n in names if n})
        literals = [(s, _literal(s, lang)) for s in spellings for lang in _NAME_LANGUAGES]
        out: dict[str, set[str]] = defaultdict(set)
        for chunk in _batches(literals, NAME_BATCH):
            rows = self.sparql(
                f"SELECT ?name ?item WHERE {{ VALUES ?name {{ {' '.join(lit for _s, lit in chunk)} }} "
                "?item rdfs:label|skos:altLabel|wdt:P1448 ?name . }"
            )
            for row in rows:
                qid = _binding_value(row.get("item"))
                name = str((row.get("name") or {}).get("value") or "")
                if isinstance(qid, str) and name:
                    out[name].add(qid)
        return {name: sorted(qids) for name, qids in out.items()}

    def fetch(self, qids: Sequence[str], *, props: Sequence[str] = ITEM_PROPS, terms: str = "all") -> None:
        """Read the items not yet held: their statements for `props` (with the `QUALIFIER_PROPS`
        qualifiers and the rank) and their terms (`all`: labels, aliases, English description and
        English Wikipedia link; `labels`: labels only), `ENTITY_BATCH` items per query."""
        todo = [q for q in dict.fromkeys(qids) if _QID.match(q) and q not in self.entities]
        prop_rows = " ".join(f'("{p}" p:{p} ps:{p})' for p in props)
        qual_rows = " ".join(f'("{p}" pq:{p})' for p in QUALIFIER_PROPS)
        for chunk in _batches(todo, ENTITY_BATCH):
            items = " ".join(f"wd:{q}" for q in chunk)
            entities = {q: empty_entity(q) for q in chunk}
            statements: dict[str, dict[str, Any]] = {}
            rows = self.sparql(
                f"SELECT ?item ?prop ?st ?rank ?value ?qprop ?qvalue WHERE {{ VALUES ?item {{ {items} }} "
                f"VALUES (?prop ?p ?ps) {{ {prop_rows} }} ?item ?p ?st . "
                "?st ?ps ?value ; wikibase:rank ?rank . "
                f"OPTIONAL {{ VALUES (?qprop ?pq) {{ {qual_rows} }} ?st ?pq ?qvalue . }} }}"
            )
            for row in rows:
                qid = _binding_value(row.get("item"))
                value = _binding_value(row.get("value"))
                st_id = str((row.get("st") or {}).get("value") or "")
                prop = str((row.get("prop") or {}).get("value") or "")
                if not isinstance(qid, str) or qid not in entities or value is None or not st_id:
                    continue
                st = statements.get(st_id)
                if st is None:
                    rank = _RANKS.get(str((row.get("rank") or {}).get("value")), "normal")
                    st = statements[st_id] = {
                        "rank": rank,
                        "value": value,
                        "q": {},
                        "_item": qid,
                        "_prop": prop,
                    }
                qprop = str((row.get("qprop") or {}).get("value") or "")
                qvalue = _binding_value(row.get("qvalue"))
                if qprop and qvalue is not None and qvalue not in st["q"].setdefault(qprop, []):
                    st["q"][qprop].append(qvalue)
            for _st_id, st in sorted(statements.items(), key=lambda kv: (str(kv[1]["value"]), kv[0])):
                item, prop = st.pop("_item"), st.pop("_prop")
                for values in st["q"].values():
                    values.sort(key=str)
                entities[item]["claims"].setdefault(prop, []).append(st)
            if terms != "none":
                self._terms(chunk, entities, labels_only=terms == "labels")
            now = self.clock()
            for qid, entity in entities.items():
                self.entities[qid] = entity
                self.retrieved[qid] = now

    def _terms(self, chunk: Sequence[str], entities: dict[str, dict[str, Any]], *, labels_only: bool) -> None:
        items = " ".join(f"wd:{q}" for q in chunk)
        if labels_only:
            query = (
                f"SELECT ?item ?kind ?lang ?text WHERE {{ VALUES ?item {{ {items} }} "
                "?item rdfs:label ?text . "
                f'BIND("label" AS ?kind) BIND(LANG(?text) AS ?lang) FILTER(?lang IN ("en", "mul")) }}'
            )
        else:
            query = (
                f"SELECT ?item ?kind ?lang ?text WHERE {{ VALUES ?item {{ {items} }} "
                '{ ?item rdfs:label ?text . BIND("label" AS ?kind) } '
                'UNION { ?item skos:altLabel ?text . BIND("alias" AS ?kind) } '
                'UNION { ?item schema:description ?text . BIND("description" AS ?kind) } '
                "UNION { ?text schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . "
                'BIND("enwiki" AS ?kind) } '
                "BIND(LANG(?text) AS ?lang) "
                'FILTER(?kind = "enwiki" || ?lang = "en" || (?lang = "mul" && ?kind != "description")) }'
            )
        for row in self.sparql(query):
            qid = _binding_value(row.get("item"))
            if not isinstance(qid, str) or qid not in entities:
                continue
            kind = str((row.get("kind") or {}).get("value") or "")
            lang = str((row.get("lang") or {}).get("value") or "")
            text = str((row.get("text") or {}).get("value") or "")
            entity = entities[qid]
            if not text:
                continue
            if kind == "label":
                entity["labels"][lang] = text
            elif kind == "alias":
                entity["aliases"].setdefault(lang, []).append(text)
            elif kind == "description" and lang == "en":
                entity["description"] = text
            elif kind == "enwiki":
                entity["enwiki"] = text
        for entity in entities.values():
            for values in entity["aliases"].values():
                values.sort()

    def class_reach(self, classes: Iterable[str]) -> dict[str, set[str]]:
        """For each class, the classes of interest (organisation, the business, settlement, place
        and physical classes, neighbourhood, street) it is, or is a subclass of at any depth (P279*)."""
        wanted_tops = {
            ORGANIZATION_CLASS,
            *BUSINESS_CLASSES,
            *SETTLEMENT_CLASSES,
            *PHYSICAL_CLASSES,
            NEIGHBOURHOOD_CLASS,
            THOROUGHFARE_CLASS,
        }
        tops = " ".join(f"wd:{q}" for q in sorted(wanted_tops))
        wanted = sorted({c for c in classes if _QID.match(c)})
        out: dict[str, set[str]] = {c: set() for c in wanted}
        for chunk in _batches(wanted, CLASS_BATCH):
            rows = self.sparql(
                f"SELECT ?cls ?top WHERE {{ VALUES ?cls {{ {' '.join(f'wd:{c}' for c in chunk)} }} "
                f"VALUES ?top {{ {tops} }} ?cls wdt:P279* ?top . }}"
            )
            for row in rows:
                cls, top = _binding_value(row.get("cls")), _binding_value(row.get("top"))
                if isinstance(cls, str) and isinstance(top, str) and cls in out:
                    out[cls].add(top)
        return out


# ---------------------------------------------------------------------------------- targets
@dataclass
class Target:
    organization_id: str
    public_id: str
    name: str
    keys: tuple[str, ...]  # the name's org_key first, then its aliases' keys
    evidence: frozenset[str]
    names: tuple[str, ...] = ()  # the name, then its aliases as written
    #: ISO 3166-2 codes of its assets' states and its proposals' jurisdictions (`US-TX`).
    subdivisions: frozenset[str] = frozenset()
    lei: str | None = None
    cik: str | None = None
    assets: int = 0
    proposals: int = 0
    opportunities: int = 0

    @property
    def key(self) -> str:
        return self.keys[0]

    @property
    def footprint(self) -> int:
        return self.assets + self.proposals + self.opportunities

    def variants(self) -> list[str]:
        return list(dict.fromkeys(v for n in (self.names or (self.name,)) for v in name_variants(n)))


def _held_identifier(ids: Mapping[str, Any], key: str) -> str:
    """`ids[key]`, the row's own evidence. This module never writes `ids` outside the profile, so a
    value there came from a register or an operator, never from last month's scored match."""
    return str(ids.get(key) or "").strip()


def collect_targets(session: Session) -> list[Target]:
    """Live, public, non-personal organisations with an asset, a sponsored proposal or an issued
    opportunity, largest footprint first (so a run cut short has done the companies that matter)."""

    def counts(stmt: Any) -> dict[Any, int]:
        return {row[0]: int(row[1]) for row in session.execute(stmt).all() if row[0] is not None}

    assets = counts(
        select(AssetOwner.organization_id, func.count(func.distinct(AssetOwner.asset_id))).group_by(
            AssetOwner.organization_id
        )
    )
    proposals = counts(select(Proposal.sponsor_org_id, func.count()).group_by(Proposal.sponsor_org_id))
    opportunities = counts(
        select(Opportunity.issuer_org_id, func.count()).group_by(Opportunity.issuer_org_id)
    )
    aliases: dict[Any, dict[str, str]] = defaultdict(dict)
    for org_id, alias in session.execute(
        select(OrganizationAlias.organization_id, OrganizationAlias.alias)
    ).all():
        if alias and alias.strip():
            aliases[org_id].setdefault(org_key(alias), alias.strip())
    countries = evidence_countries(session)
    subdivisions: dict[Any, set[str]] = defaultdict(set)
    for org_id, jurisdiction in session.execute(
        select(Opportunity.issuer_org_id, Opportunity.jurisdiction).where(
            Opportunity.issuer_org_id.is_not(None)
        )
    ).all():
        if jurisdiction:
            countries.setdefault(org_id, set()).add(str(jurisdiction)[:2].upper())
            subdivisions[org_id].add(str(jurisdiction).upper())
    for org_id, jurisdiction in session.execute(
        select(Proposal.sponsor_org_id, Proposal.jurisdiction).where(Proposal.sponsor_org_id.is_not(None))
    ).all():
        if jurisdiction:
            subdivisions[org_id].add(str(jurisdiction).upper())
    for org_id, state_code in session.execute(
        select(AssetOwner.organization_id, Asset.state_code).join(Asset, Asset.id == AssetOwner.asset_id)
    ).all():
        if state_code:
            subdivisions[org_id].add(str(state_code).upper())
    targets: list[Target] = []
    for org in session.scalars(
        select(Organization).where(
            Organization.merged_into_id.is_(None),
            Organization.personal_data.is_(False),
            Organization.publish_state == "public",
        )
    ):
        n_assets = int(assets.get(org.id, 0))
        n_props = int(proposals.get(org.id, 0))
        n_opps = int(opportunities.get(org.id, 0))
        if not (n_assets or n_props or n_opps):
            continue
        key = org_key(org.name_canonical)
        if not key:
            continue
        ids = org.ids or {}
        lei = _held_identifier(ids, "lei").upper() or None
        cik_raw = _held_identifier(ids, "cik")
        cik = f"{int(cik_raw):010d}" if cik_raw.isdigit() else None
        evidence = frozenset(countries.get(org.id) or {org.country})
        others = {k: a for k, a in aliases.get(org.id, {}).items() if k != key}
        targets.append(
            Target(
                organization_id=str(org.id),
                public_id=org.public_id,
                name=org.name_canonical,
                keys=(key, *sorted(others)),
                evidence=evidence,
                names=(org.name_canonical, *(others[k] for k in sorted(others))),
                subdivisions=frozenset(c for c in subdivisions.get(org.id, ()) if _SUBDIVISION.match(c)),
                lei=lei if lei and _LEI.match(lei) else None,
                cik=cik,
                assets=n_assets,
                proposals=n_props,
                opportunities=n_opps,
            )
        )
    targets.sort(key=lambda t: (-t.footprint, t.name))
    return targets


# --------------------------------------------------------------------------------- decisions
@dataclass
class Decision:
    organization_id: str
    public_id: str
    name: str
    status: str  # matched | no_match | below_threshold | ambiguous | conflict | lookup_failed
    rule: str | None = None
    confidence: float | None = None
    reasons: list[str] = field(default_factory=list)
    qid: str | None = None
    cik: str | None = None
    wikidata_rule: str | None = None
    candidates: list[str] = field(default_factory=list)
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    footprint: int = 0

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Decision:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


DEFINITIVE_NEGATIVE = frozenset({"no_match", "below_threshold", "ambiguous", "conflict"})


def _own_subdivisions(entity: Mapping[str, Any]) -> set[str]:
    """The item's own ISO 3166-2 codes (P300)."""
    codes = {str(v).upper() for v in best_values(entity, "P300")}
    return {c for c in codes if _SUBDIVISION.match(c)}


@dataclass
class _Aux:
    """Labels, countries and classes for the items a decision reads."""

    client: WikidataClient
    ancestors: dict[str, set[str]] = field(default_factory=dict)

    def iso2(self, qid: str) -> str | None:
        entity = self.client.entities.get(qid)
        if not entity:
            return None
        codes = [str(v).upper() for v in best_values(entity, "P297")]
        return codes[0] if codes else None

    def label(self, qid: str) -> str | None:
        entity = self.client.entities.get(qid) or {}
        labels = entity.get("labels") or {}
        return labels.get("en") or labels.get("mul")

    def countries(self, entity: Mapping[str, Any]) -> set[str]:
        """ISO codes of the item's country (P17) and of its headquarters' country."""
        out = {c for c in (self.iso2(str(q)) for q in best_values(entity, "P17")) if c}
        for hq in best_values(entity, "P159"):
            hq_entity = self.client.entities.get(str(hq)) or {}
            own = self.iso2(str(hq))
            if own:
                out.add(own)
            out |= {c for c in (self.iso2(str(q)) for q in best_values(hq_entity, "P17")) if c}
        return out

    def is_human(self, entity: Mapping[str, Any]) -> bool:
        return HUMAN_CLASS in {str(v) for v in best_values(entity, "P31")}

    def reach(self, entity: Mapping[str, Any]) -> set[str]:
        out: set[str] = set()
        for cls in best_values(entity, "P31"):
            out |= self.ancestors.get(str(cls), {str(cls)})
        return out

    def _class_reach(self, cls: Any) -> set[str]:
        return self.ancestors.get(str(cls), {str(cls)})

    def is_city(self, entity: Mapping[str, Any]) -> bool:
        """A P31 class that reaches a settlement class and neither neighbourhood nor county: judged
        per class, because Las Vegas is a "college town" (which reaches neighbourhood) and a city."""
        for cls in best_values(entity, "P31"):
            reach = self._class_reach(cls)
            if reach & SETTLEMENT_CLASSES and not reach & {NEIGHBOURHOOD_CLASS, COUNTY_CLASS}:
                return True
        return False

    def is_street(self, entity: Mapping[str, Any]) -> bool:
        return THOROUGHFARE_CLASS in self.reach(entity)

    def is_place(self, entity: Mapping[str, Any]) -> bool:
        """A settlement or a county (`PLACE_CLASSES`): a government recorded as its place."""
        return bool(self.reach(entity) & PLACE_CLASSES)

    def legal_entity_fit(self, entity: Mapping[str, Any]) -> str | None:
        """Why the item cannot be a legal entity (a name with a legal form), or None when it can: it
        needs a P31 class that is an organisation and not physical, and none that is physical and
        not a business (`PHYSICAL_CLASSES`). Chevron's oil-company class reaches building and
        business; Bailey Farm's "building" reaches building only."""
        organisation = physical_only = None
        for cls in best_values(entity, "P31"):
            reach = self._class_reach(cls)
            physical = reach & PHYSICAL_CLASSES
            if ORGANIZATION_CLASS in reach and not physical:
                organisation = str(cls)
            if physical and not reach & BUSINESS_CLASSES:
                physical_only = str(cls)
        if physical_only:
            return f"P31 {physical_only} is a place or a physical thing, not a business"
        if organisation is None:
            return "no P31 class is an organisation that is not a place or a physical thing"
        return None

    def subdivisions(self, qids: Iterable[str]) -> set[str]:
        """ISO 3166-2 codes (P300) on the places and on every place they are located in (P131, all
        branches, `PLACE_CLIMB` levels): a county gives its state, a city its county's state."""
        out: set[str] = set()
        frontier, seen = [str(q) for q in qids], set()
        for _level in range(PLACE_CLIMB + 1):
            parents: list[str] = []
            for q in frontier:
                if q in seen:
                    continue
                seen.add(q)
                entity = self.client.entities.get(q) or {}
                out |= _own_subdivisions(entity)
                parents += [str(v) for v in best_values(entity, "P131")]
            frontier = parents
        return out

    def item_subdivisions(self, entity: Mapping[str, Any]) -> set[str]:
        """Where the item itself is: its own P300, and the subdivisions of the places it is in."""
        return _own_subdivisions(entity) | self.subdivisions(str(v) for v in best_values(entity, "P131"))

    def located(self, entity: Mapping[str, Any]) -> set[str]:
        """Where Wikidata locates an organisation: `item_subdivisions`, and the subdivisions its
        headquarters (P159) lies in. Empty when Wikidata says nothing (Sheridan School, DC)."""
        return self.item_subdivisions(entity) | self.subdivisions(str(v) for v in best_values(entity, "P159"))

    def place(self, hq: str) -> tuple[str | None, str | None]:
        """`(city, country)` of one P159 value: the first settlement among the place and the places
        it is located in (P131, `HQ_CLIMB` levels, while each is in exactly one), and the first
        country any of them states. A country as headquarters has no city; a street is not climbed;
        a place nothing classifies keeps its own label."""
        own = self.iso2(hq)
        if own:
            return None, own
        entities = self.client.entities
        chain = [hq]
        if not self.is_street(entities.get(hq) or {}):
            for _ in range(HQ_CLIMB):
                up = list(dict.fromkeys(str(v) for v in best_values(entities.get(chain[-1]) or {}, "P131")))
                if len(up) != 1 or up[0] in chain:
                    break
                chain.append(up[0])
        city = next((q for q in chain if self.is_city(entities.get(q) or {})), None)
        if city is None and not best_values(entities.get(hq) or {}, "P31"):
            city = hq
        countries = (self.iso2(str(x)) for q in chain for x in best_values(entities.get(q) or {}, "P17"))
        country = next((c for c in countries if c), None)
        return (self.label(city) if city else None), country

    def headquarters(self, hqs: Sequence[str], reasons: list[str]) -> tuple[str | None, str | None]:
        """`(city, country)` over every current P159 value: the city when exactly one is named and
        no value names another country; else the country when they agree on one; else nothing."""
        places = [self.place(h) for h in dict.fromkeys(hqs)]
        cities = {(city, country) for city, country in places if city}
        countries = {country for _city, country in places if country}
        if len(cities) == 1:
            city, country = next(iter(cities))
            if not countries - {country}:
                return city, country
        if len(cities) > 1:
            reasons.append(f"headquarters: Wikidata states {len(cities)} places; city withheld")
        if len(countries) == 1:
            return None, next(iter(countries))
        return None, None

    def org_kind(self, entity: Mapping[str, Any]) -> str | None:
        """`business`, `organisation`, or None when no P31 class reaches organisation (Q43229)."""
        reach = self.reach(entity)
        if ORGANIZATION_CLASS not in reach:
            return None
        return "business" if reach & BUSINESS_CLASSES else "organisation"


def _token_weight(key: str) -> tuple[float, str | None]:
    tokens = key_tokens(key)
    if len(tokens) > 1:
        return 1.0, None
    if len(key) <= 3:
        return W_SHORT_TOKEN, f"key={key!r} is one token of {len(key)} characters"
    return W_SINGLE_TOKEN, f"key={key!r} is one token"


def score_sec(target: Target, cik: str, sec: SecTickers) -> tuple[float, list[str]]:
    """Confidence that the SEC registrant `cik`, found by an exact key match, is `target`."""
    reasons = [f"sec_name={sec.name(cik)!r}", f"key={target.key!r}"]
    score = SEC_BASE
    weight, why = _token_weight(target.key)
    score *= weight
    if why:
        reasons.append(why)
    ours, theirs = legal_form(target.name), legal_form(sec.name(cik))
    if ours and theirs and ours != theirs:
        score *= W_LEGAL_FORM_MISMATCH
        reasons.append(f"legal form {ours} != {theirs}")
    elif ours and not theirs:
        # `Phillips 66 Company` (the operating subsidiary) against the registrant `Phillips 66`: the
        # key drops the form, so only the form tells them apart (1 of 164 key matches, 2026-10-22).
        score *= W_LEGAL_FORM_MISMATCH
        reasons.append(f"legal form {ours} != none (SEC's name has no legal form)")
    if not sec.main_board(cik):
        score *= W_SEC_OTC_ONLY
        reasons.append("no main-board listing (OTC or none)")
    return round(score, 4), reasons


#: The reason a legal entity's name is refused an item that is a place or a physical thing. Such an
#: item still competes for the name (`_rule3`): a project company named after a town is not the
#: same-named bank or university, so the key is ambiguous rather than the other item's.
NAMED_AFTER = "the name has a legal form"
#: The reason a place is refused for lying outside the evidence's subdivisions.
PLACE_ELSEWHERE = "a place in"
#: The reason a public body (any organisation that is not a business, or any item for a locally
#: scoped name, `local_scope`) is refused for the same: a county government, an authority or a
#: hospital acts where it is. It does not compete for the name as a place does (`_rule3`): another
#: state's body of the same name is simply another body.
BODY_ELSEWHERE = "a local body in"
#: The reason a locally scoped name is refused an item Wikidata does not locate at all.
BODY_UNLOCATED = "a local body's name, but Wikidata does not say where the item is"
#: The reason a disambiguation page is refused; it still competes for the name (`_rule3`).
DISAMBIGUATION = "a Wikimedia disambiguation page"
#: The reason a scored candidate carries when Wikidata locates it in a state the evidence names.
LOCATED_HERE = "located="


def score_wikidata(target: Target, entity: Mapping[str, Any], aux: _Aux) -> tuple[float | None, list[str]]:
    """Confidence that the Wikidata item is `target` by name and country, or None when a gate
    fails (not an organisation, a person, a country that contradicts the evidence, no key, a legal
    entity's name on a place or a physical thing, a place outside the evidence's subdivisions, a
    local body located elsewhere or, for a locally scoped name, nowhere)."""
    if aux.is_human(entity):
        return None, ["instance of human (Q5): never attached"]
    if {str(v) for v in best_values(entity, "P31")} & DISAMBIGUATION_CLASSES:
        return None, [f"{DISAMBIGUATION}: the name is shared"]
    labels, aliases = name_keys(entity)
    keys = set(target.keys)
    if labels & keys:
        name_w, how = 1.0, "label"
    elif aliases & keys:
        name_w, how = W_ALIAS, "alias"
    else:
        return None, ["no label, alias or official name with the key"]
    kind = aux.org_kind(entity)
    if kind is None:
        return None, ["not an organisation (P31/P279* does not reach Q43229)"]
    countries = aux.countries(entity)
    if countries and not countries & target.evidence:
        return None, [f"country {sorted(countries)} not in evidence {sorted(target.evidence)}"]
    form = legal_form(target.name)
    if form:
        unfit = aux.legal_entity_fit(entity)
        if unfit:
            return None, [f"{NAMED_AFTER} ({form}) but {unfit}"]
    here: set[str] = set()
    if aux.is_place(entity):
        where = aux.item_subdivisions(entity)
        if not where & target.subdivisions:
            return None, [
                f"{PLACE_ELSEWHERE} {sorted(where) or 'no known subdivision'}, evidence in "
                f"{sorted(target.subdivisions) or 'no subdivision'}"
            ]
        here = where & target.subdivisions
    elif target.subdivisions and (kind != "business" or local_scope(target.name)):
        # `MONTGOMERY COUNTY GOVERNMENT` (a Tennessee asset) is not the government of Montgomery
        # County, Maryland; `St. Elizabeth Medical Center` (New York) is not St. Elizabeth Healthcare
        # (Kentucky). Where Wikidata locates the body (its P131, its headquarters' P131 chain), that
        # must meet the evidence; a locally scoped name needs an item Wikidata locates at all, by a
        # statement or by naming the state in its label or description (`Sheridan School
        # Corporation`, Indiana, is not Sheridan School, "independent school", which states no
        # place). A business with an ordinary name is not held to it: it owns plants far from its seat.
        where = aux.located(entity)
        if where and not where & target.subdivisions:
            return None, [f"{BODY_ELSEWHERE} {sorted(where)}, evidence in {sorted(target.subdivisions)}"]
        here = where & target.subdivisions
        if not where and local_scope(target.name):
            here = named_subdivisions(entity, target.subdivisions)
            if not here:
                return None, [f"{BODY_UNLOCATED} (evidence in {sorted(target.subdivisions)})"]
    reasons = [f"name={how}"]
    if here:
        reasons.append(f"{LOCATED_HERE}{','.join(sorted(here))}")
    score = name_w
    if countries:
        reasons.append(f"country=agree:{','.join(sorted(countries & target.evidence))}")
        if countries - target.evidence:
            score *= W_COUNTRY_PARTIAL
            reasons.append(f"country=also:{','.join(sorted(countries - target.evidence))}")
    else:
        score *= W_COUNTRY_UNKNOWN
        reasons.append("country=unknown")
    if kind != "business":
        score *= W_OTHER_ORG
        reasons.append("class=organisation (not a business)")
    else:
        reasons.append("class=business")
    weight, why = _token_weight(target.key)
    score *= weight
    if why:
        reasons.append(why)
    # Wikidata labels usually drop the legal form; an alias or official name that keeps one can
    # still say the item is the corporation and the row its same-named LP.
    ours = legal_form(target.name)
    names = [
        *(entity.get("labels") or {}).values(),
        *[a for vs in (entity.get("aliases") or {}).values() for a in vs],
    ]
    names += [str(v) for v in best_values(entity, "P1448")]
    theirs = {f for f in (legal_form(str(n)) for n in names if n and org_key(str(n)) in keys) if f}
    if ours and theirs and ours not in theirs:
        score *= W_LEGAL_FORM_MISMATCH
        reasons.append(f"legal form {ours} not in {sorted(theirs)}")
    if best_values(entity, "P576"):
        score *= W_DISSOLVED
        reasons.append("dissolved (P576)")
    if not entity.get("description") and not entity.get("enwiki") and not best_values(entity, "P856"):
        score *= W_THIN
        reasons.append("thin item: no description, Wikipedia article or website")
    return round(score, 4), reasons


# ------------------------------------------------------------------------------------- fields
def _field(
    value: Any, source_id: str, source_url: str, retrieved_at: dt.datetime, licence_id: str
) -> dict[str, Any]:
    return {
        "value": value,
        "source_id": source_id,
        "source_url": source_url,
        "retrieved_at": iso(retrieved_at),
        "licence_id": licence_id,
    }


def _site(url: str) -> str:
    """The host a URL names, without a leading `www.` (`https://www.ameren.com/` -> `ameren.com`)."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def official_website(values: Iterable[Any], keys: Sequence[str], reasons: list[str]) -> str | None:
    """The one official website (P856) to show, or None. Of several truthy values (a preferred-rank
    value already wins over normal ones, `best_values`): the homepages (no path, no query); then not
    a sub-host of another homepage's site (`my.xcelenergy.com` and `manage.gassouth.com` are customer
    logins beside `www.xcelenergy.com` and `www.gassouth.com`); then, if several sites remain, the
    one whose domain is the organisation's name key (`ameren.com`, not `amereninvestors.com`). Two
    or more sites left are withheld: which is the company's is not on the item (measured 2026-10-22:
    11 matched items state several websites; 9 resolve, 2 are withheld)."""
    urls = sorted({str(v) for v in values if re.match(r"^https?://", str(v)) and len(str(v)) <= 300})
    if len(urls) <= 1:
        return urls[0] if urls else None
    homes = [u for u in urls if urlsplit(u).path in ("", "/") and not urlsplit(u).query]
    sites = {_site(u) for u in homes}
    homes = [u for u in homes if not any(_site(u).endswith(f".{s}") for s in sites)]
    by_site: dict[str, list[str]] = defaultdict(list)
    for url in homes:
        by_site[_site(url)].append(url)
    if len(by_site) > 1:
        names = {re.sub(r"[^a-z0-9]", "", k.lower()) for k in keys}
        names |= {"".join(t for t in k.lower().split() if t != "and") for k in keys}
        named = {s: us for s, us in by_site.items() if re.sub(r"[^a-z0-9]", "", s.split(".")[0]) in names}
        if len(named) == 1:
            by_site = named
    if len(by_site) == 1:
        # One site written several ways (`http://gevo.com`, `http://www.gevo.com`): https, then www.
        (choices,) = by_site.values()
        return min(choices, key=lambda u: (not u.startswith("https://"), "://www." not in u.lower(), u))
    reasons.append(f"website withheld: Wikidata states {len(urls)} websites and no one homepage among them")
    return None


def _wikidata_listings(entity: Mapping[str, Any]) -> list[tuple[str | None, str]]:
    """`(symbol, exchange QID)` for each current P414 statement, the symbol from its P249 qualifier;
    the item's own P249 fills a missing symbol, or stands alone with no exchange (`""`)."""
    out: list[tuple[str | None, str]] = []
    for st in best_statements(entity, "P414"):
        symbols = [str(s) for s in st["q"].get("P249", []) if s]
        out.append((symbols[0] if symbols else None, str(st["value"])))
    loose = [str(v) for v in best_values(entity, "P249")]
    if loose and not any(sym for sym, _ in out):
        out = [(loose[0], ex) for _sym, ex in out] or [(loose[0], "")]
    return out


def derive_fields(
    *,
    entity: Mapping[str, Any] | None,
    qid: str | None,
    cik: str | None,
    target: Target,
    sec: SecTickers,
    aux: _Aux,
    licences: Mapping[str, str],
    retrieved_at: dt.datetime,
    reasons: list[str],
) -> dict[str, dict[str, Any]]:
    """The profile fields one decision supports, each with its provenance. `cik` is one the match
    verified (rule 2, or a Wikidata CIK whose SEC registrant has the organisation's name)."""
    out: dict[str, dict[str, Any]] = {}
    wd_lic = licences[WIKIDATA_SOURCE_ID]
    sec_lic = licences[SEC_TICKERS_SOURCE_ID]
    if entity is not None and qid:
        page = WIKIDATA_ITEM_URL.format(qid=qid)
        websites = best_values(entity, "P856")
        if websites and best_values(entity, "P576"):
            reasons.append("website withheld: dissolved (P576)")
        else:
            website = official_website(websites, target.keys, reasons)
            if website:
                out["website"] = _field(website, WIKIDATA_SOURCE_ID, f"{page}#P856", retrieved_at, wd_lic)
        hqs = [str(v) for v in best_values(entity, "P159")]
        own_countries = {c for c in (aux.iso2(str(q)) for q in best_values(entity, "P17")) if c}
        if hqs:
            city, country = aux.headquarters(hqs, reasons)
            if country and country not in target.evidence and own_countries & target.evidence:
                # The item's own country is the evidence's and its headquarters another's: one of
                # the two is wrong, or the item merges a company and its foreign parent (`ALON USA
                # LP`, Texas, given Hof HaSharon, Israel). Which is not on the item.
                reasons.append(
                    f"headquarters withheld: {city or country} ({country}) is outside the evidence "
                    f"{sorted(target.evidence)} though the item's country is {sorted(own_countries)}"
                )
                city = country = None
            if city or country:
                out["headquarters"] = _field(
                    {"city": city, "country": country},
                    WIKIDATA_SOURCE_ID,
                    f"{page}#P159",
                    retrieved_at,
                    wd_lic,
                )
        description = str(entity.get("description") or "").strip()
        if (
            len(description.split()) > 1
            and len(description) <= 250
            and description.lower() not in GENERIC_DESCRIPTIONS
        ):
            out["description"] = _field(description, WIKIDATA_SOURCE_ID, page, retrieved_at, wd_lic)
        # One truthy LEI (a preferred-rank one wins over normal ones), or the row's own among several:
        # an item that merges a parent and a subsidiary states both, and nothing on it says which is
        # which (Air Products & Chemicals' item: its own and Air Products Manufacturing Corporation's).
        leis = sorted({str(v).upper() for v in best_values(entity, "P1278") if _LEI.match(str(v).upper())})
        lei = leis[0] if len(leis) == 1 else target.lei if target.lei in leis else None
        if lei:
            out["lei"] = _field(lei, WIKIDATA_SOURCE_ID, f"{page}#P1278", retrieved_at, wd_lic)
        elif leis:
            reasons.append(
                f"lei withheld: Wikidata states {len(leis)} LEIs ({', '.join(leis)}), none preferred, "
                "none the row's own"
            )
        if entity.get("enwiki"):
            out["wikipedia"] = _field(str(entity["enwiki"]), WIKIDATA_SOURCE_ID, page, retrieved_at, wd_lic)
    # Ticker: an SEC main-board symbol with a second source saying the company is listed. For a
    # US organisation that means SEC's own file; Wikidata's listing alone is shown only abroad.
    listings = _wikidata_listings(entity) if entity is not None else []
    main = sec.main_board(cik) if cik else []
    wd_symbol, wd_exchange = next(((sym, ex) for sym, ex in listings if sym), (None, ""))

    def sec_ticker(row: SecListing) -> dict[str, Any]:
        return _field(
            {"symbol": row.ticker, "exchange": row.exchange},
            SEC_TICKERS_SOURCE_ID,
            SEC_COMPANY_URL.format(cik=int(row.cik)),
            sec.retrieved_at,
            sec_lic,
        )

    if main and listings:
        wd_symbols = sorted({_norm_symbol(sym) for sym, _ in listings if sym})
        agreed = next((row for row in main if _norm_symbol(row.ticker) in wd_symbols), None)
        if agreed is not None:
            out["ticker"] = sec_ticker(agreed)
        elif not wd_symbols:
            # Listed on an exchange (P414) with no symbol stated (Waste Connections): SEC's primary
            # main-board symbol for the verified CIK.
            out["ticker"] = sec_ticker(main[0])
        else:
            # Georgia Power: SEC's main-board row for its CIK is GPJA, a junior subordinated note;
            # Wikidata's GAR is not SEC's either. Two sources that disagree are not two sources.
            reasons.append(
                f"ticker withheld: SEC lists {', '.join(r.ticker for r in main)} for {cik}; "
                f"Wikidata lists {', '.join(wd_symbols)}"
            )
    elif main:
        reasons.append(
            f"ticker withheld: SEC lists {main[0].ticker} but no source says the company is listed"
        )
    elif wd_symbol and qid:
        if cik:
            reasons.append(
                f"ticker withheld: Wikidata lists {wd_symbol}; SEC lists no main-board symbol for {cik}"
            )
        elif "US" in target.evidence:
            confirmed = sec.confirm_symbol(wd_symbol, target.keys)
            if confirmed is not None:
                out["ticker"] = sec_ticker(confirmed)
            else:
                reasons.append(
                    f"ticker withheld: Wikidata lists {wd_symbol}; no SEC registrant of this name lists it"
                )
        else:
            exchange = EXCHANGE_SHORT_NAMES.get(wd_exchange) or (
                aux.label(wd_exchange) if wd_exchange else None
            )
            out["ticker"] = _field(
                {"symbol": wd_symbol, "exchange": exchange},
                WIKIDATA_SOURCE_ID,
                f"{WIKIDATA_ITEM_URL.format(qid=qid)}#P414",
                retrieved_at,
                wd_lic,
            )
    return out


# ---------------------------------------------------------------------------------- fetch
@dataclass
class FetchResult:
    started_at: dt.datetime
    finished_at: dt.datetime
    decisions: list[Decision]
    report: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "version": PROFILE_VERSION,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "report": self.report,
            "decisions": [d.to_json() for d in self.decisions],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> FetchResult:
        def parse(text: str) -> dt.datetime:
            return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))

        return cls(
            started_at=parse(str(data["started_at"])),
            finished_at=parse(str(data["finished_at"])),
            decisions=[Decision.from_json(d) for d in data.get("decisions") or []],
            report=dict(data.get("report") or {}),
        )


def source_licences(registry: Registry | None = None) -> dict[str, str]:
    """Licence id per source as `upsert_licence_and_source` will key it (the quoted terms' hash)."""
    registry = registry or Registry()
    return {sid: registry.get(sid).licence_id for sid in (WIKIDATA_SOURCE_ID, SEC_TICKERS_SOURCE_ID)}


@dataclass
class _Lookups:
    """What the network phase learned, for `_decide`."""

    by_lei: dict[str, list[str]]
    sec_pick: dict[str, tuple[str, float, list[str]]]
    sec_rejected: dict[str, list[str]]
    by_cik: dict[str, list[str]]
    candidates: dict[str, list[str]]
    unsearched: set[str]
    searched: int = 0
    search_hits: int = 0
    search_failed: int = 0
    search_stopped: str | None = None  # budget | time | failures, when the fallback ended early


def fetch_profiles(
    session: Session | None,
    transport: Transport,
    *,
    targets: list[Target] | None = None,
    limit: int | None = None,
    threshold: float = MATCH_THRESHOLD,
    search_budget: int = SEARCH_BUDGET,
    search_minutes: float = SEARCH_MINUTES,
    search_rotation: int | None = None,
    clock: Callable[[], dt.datetime] = utcnow,
    monotonic: Callable[[], float] = time.monotonic,
    progress: Callable[[str], None] | None = None,
) -> FetchResult:
    """Decide, for every target, which company it is (or that it cannot tell) and what the profile
    says. Reads the store only to collect the targets (pass `targets` to skip even that); writes
    nothing. `search_rotation` picks the searches' order (`search_order`); by default the month of
    the run, and 0 replays a recording in the order it was made."""
    started = clock()
    if targets is None:
        if session is None:
            raise ValueError("fetch_profiles needs a session or the targets")
        targets = collect_targets(session)
    if limit is not None:
        targets = targets[:limit]
    licences = source_licences()
    client = WikidataClient(transport, clock=clock)
    aux = _Aux(client)
    sec = fetch_sec_tickers(transport, clock())
    say = progress or (lambda _msg: None)

    # Rule 1 and the deterministic half of rule 2: identifiers the rows already hold.
    by_lei = client.by_statement("P1278", [t.lei for t in targets if t.lei])
    # Rule 2: the row's own CIK, else one SEC registrant with the same key.
    sec_pick: dict[str, tuple[str, float, list[str]]] = {}
    sec_rejected: dict[str, list[str]] = {}
    for t in targets:
        if t.lei and by_lei.get(t.lei):
            continue
        if t.cik:
            sec_pick[t.organization_id] = (t.cik, 1.0, ["cik held by the row"])
            continue
        ciks = sec.by_key.get(t.key) or set()
        if not ciks:
            continue
        if len(ciks) > 1:
            sec_rejected[t.organization_id] = [f"{len(ciks)} SEC registrants share key {t.key!r}"]
            continue
        if "US" not in t.evidence:
            sec_rejected[t.organization_id] = [f"SEC key match but evidence {sorted(t.evidence)} has no US"]
            continue
        cik = next(iter(ciks))
        score, reasons = score_sec(t, cik, sec)
        sec_pick[t.organization_id] = (cik, score, reasons)
    by_cik = client.by_statement("P5531", [c for c, _s, _r in sec_pick.values()])
    say(
        f"identifiers: {len(by_lei)} LEI items, {len(sec_pick)} SEC picks, {len(by_cik)} CIK items "
        f"({client.requests['sparql']} queries)"
    )

    # Rule 3 candidates, for every target with no Wikidata item yet: exact names, then search.
    def has_item(t: Target) -> bool:
        pick = sec_pick.get(t.organization_id)
        return bool((t.lei and by_lei.get(t.lei)) or (pick and by_cik.get(pick[0])))

    pending = [t for t in targets if not has_item(t)]
    exact = client.by_names(v for t in pending for v in t.variants())
    candidates: dict[str, list[str]] = {}
    for t in pending:
        candidates[t.organization_id] = list(dict.fromkeys(q for v in t.variants() for q in exact.get(v, [])))
    with_exact = sum(1 for t in pending if candidates[t.organization_id])
    say(f"names: {with_exact} of {len(pending)} have an exact candidate")
    lookups = _Lookups(by_lei, sec_pick, sec_rejected, by_cik, candidates, unsearched=set())
    now = clock()
    _search_fallback(
        client,
        search_order(
            [t for t in pending if not candidates[t.organization_id]],
            budget=search_budget,
            rotation=now.year * 12 + now.month - 1 if search_rotation is None else search_rotation,
        ),
        lookups,
        budget=search_budget,
        deadline=monotonic() + search_minutes * 60.0,
        monotonic=monotonic,
        say=say,
    )

    # Statements and terms of every item, then what they point at, then the classes above them.
    items = {q for qs in by_lei.values() for q in qs} | {q for qs in by_cik.values() for q in qs}
    items |= {q for qs in candidates.values() for q in qs}
    client.fetch(sorted(items))
    pointed: set[str] = set()
    for q in items:
        entity = client.entities.get(q) or {}
        for prop in ("P17", "P159", "P414", "P131"):
            pointed |= {str(v) for v in best_values(entity, prop)}
    client.fetch(sorted(pointed), props=AUX_PROPS, terms="labels")
    # The places a headquarters (a building, a square, a neighbourhood) or an item that is itself a
    # place is located in, up to its state (P300): `PLACE_CLIMB` levels of P131.
    places = set(pointed)
    frontier = {
        str(v) for q in items for p in ("P159", "P131") for v in best_values(client.entities.get(q) or {}, p)
    }
    seen = set(frontier)
    for _level in range(PLACE_CLIMB):
        frontier = {
            str(v) for q in frontier for v in best_values(client.entities.get(q) or {}, "P131")
        } - seen
        if not frontier:
            break
        seen |= frontier
        client.fetch(sorted(frontier), props=AUX_PROPS, terms="labels")
        places |= frontier
    countries = {
        str(v)
        for q in places
        for v in best_values(client.entities.get(q) or {}, "P17")
        if str(v) not in client.entities
    }
    client.fetch(sorted(countries), props=AUX_PROPS, terms="none")
    classes = {str(v) for q in items | places for v in best_values(client.entities.get(q) or {}, "P31")}
    aux.ancestors = client.class_reach(classes)
    say(f"entities: {len(client.entities)} read in {client.requests['sparql']} queries")

    decisions = [_decide(t, lookups, sec, aux, licences, threshold) for t in targets]
    report = summarise(decisions, client=client, sec=sec, lookups=lookups, threshold=threshold)
    return FetchResult(started_at=started, finished_at=clock(), decisions=decisions, report=report)


def search_order(queue: Sequence[Target], *, budget: int, rotation: int) -> list[Target]:
    """The order of the name searches over `queue` (largest footprint first): the first half of the
    budget on the largest organisations every run, then the rest of the queue from a start that
    moves on by the other half each rotation (a month), wrapping round. On the 2026-10-20 store
    7,247 organisations had no exact-name candidate; with the whole budget on the largest first, the
    ~4,200 smallest were never searched, month after month. Now each is reached within
    `ceil(rest / (budget - budget // 2))` months, and a profile found earlier is kept meanwhile
    (`lookup_failed` keeps it)."""
    head_n = max(budget, 0) // 2
    head, tail = list(queue[:head_n]), list(queue[head_n:])
    step = max(budget, 0) - head_n
    if not tail or not step:
        return head + tail
    start = (rotation * step) % len(tail)
    return head + tail[start:] + tail[:start]


def _search_fallback(
    client: WikidataClient,
    targets: Sequence[Target],
    lookups: _Lookups,
    *,
    budget: int,
    deadline: float,
    monotonic: Callable[[], float],
    say: Callable[[str], None],
) -> None:
    """`wbsearchentities` for organisations with no exact-name candidate, largest first, within
    `budget` requests and until `deadline`; stops after `SEARCH_FAILURE_LIMIT` consecutive failures.
    The organisations it does not reach are recorded in `lookups.unsearched` (their decision is
    `lookup_failed`, which keeps a previous profile)."""
    cache: dict[str, list[dict[str, Any]]] = {}
    failures = 0
    for n, t in enumerate(targets):
        text = search_text(t.name)
        if text not in cache:
            stop = (
                "budget"
                if lookups.searched >= budget
                else "failures"
                if failures >= SEARCH_FAILURE_LIMIT
                else "time"
                if monotonic() >= deadline
                else None
            )
            if stop:
                lookups.search_stopped = stop
                lookups.unsearched.update(x.organization_id for x in targets[n:])
                break
            try:
                cache[text] = client.search(text)
                lookups.searched += 1
                failures = 0
            except (HttpFailed, HttpBlocked) as exc:
                log.warning("org_enrich: search failed for %r: %s", text, exc)
                lookups.search_failed += 1
                lookups.unsearched.add(t.organization_id)
                failures += 1
                if failures >= SEARCH_FAILURE_LIMIT:
                    say(f"search: {failures} consecutive failures; stopping for this run")
                continue
        keys = set(t.keys)
        hits = [
            str(h["id"])
            for h in cache[text]
            if org_key(str((h.get("match") or {}).get("text") or "")) in keys
            or org_key(str(h.get("label") or "")) in keys
        ]
        if hits:
            lookups.search_hits += 1
        lookups.candidates[t.organization_id] = list(dict.fromkeys(hits))
        if lookups.searched and lookups.searched % 50 == 0:
            say(f"searched {lookups.searched}/{len(targets)}")


def _decide(
    t: Target,
    lk: _Lookups,
    sec: SecTickers,
    aux: _Aux,
    licences: Mapping[str, str],
    threshold: float,
) -> Decision:
    d = Decision(organization_id=t.organization_id, public_id=t.public_id, name=t.name, status="no_match")
    d.footprint = t.footprint
    client = aux.client

    def finish(entity: Mapping[str, Any] | None, qid: str | None, cik: str | None) -> Decision:
        if entity is not None and aux.is_human(entity):
            d.status, d.qid = "conflict", None
            d.reasons.append(f"{qid} is an instance of human (Q5): never attached")
            return d
        retrieved = client.retrieved.get(qid or "", sec.retrieved_at)
        d.status, d.qid, d.cik = "matched", qid, cik
        d.fields = derive_fields(
            entity=entity,
            qid=qid,
            cik=cik,
            target=t,
            sec=sec,
            aux=aux,
            licences=licences,
            retrieved_at=retrieved,
            reasons=d.reasons,
        )
        return d

    # Rule 1: LEI.
    if t.lei and lk.by_lei.get(t.lei):
        items = lk.by_lei[t.lei]
        d.rule, d.candidates = "lei", list(items)
        if len(items) > 1:
            d.status = "ambiguous"
            d.reasons.append(f"{len(items)} Wikidata items carry LEI {t.lei}")
            return d
        lei_qid = items[0]
        lei_entity = client.entities[lei_qid]
        countries = aux.countries(lei_entity)
        if countries and not countries & t.evidence:
            # Wikidata put the row's LEI on another legal entity's item (Johnson Controls, Inc., US:
            # its LEI is on the item of Johnson Controls International plc, IE).
            d.status = "conflict"
            d.reasons.append(
                f"LEI item {lei_qid} country {sorted(countries)} not in evidence {sorted(t.evidence)}"
            )
            return d
        # Within one country too: PSEG's item carries PSE&G's LEI, Phillips 66's carries Phillips 66
        # Partners'. An item with several LEIs (any rank but deprecated) must also have the name.
        stated = {
            str(st["value"]).upper()
            for st in (lei_entity.get("claims") or {}).get("P1278", [])
            if st.get("rank") != "deprecated"
        }
        labels, aliases = name_keys(lei_entity)
        if len(stated) > 1 and not (labels | aliases) & set(t.keys):
            d.status = "conflict"
            d.reasons.append(
                f"LEI item {lei_qid} states {len(stated)} LEIs and none of its names has key {t.key!r}: "
                "the row's LEI may be another legal entity's on this item"
            )
            return d
        d.confidence, d.wikidata_rule = 1.0, "lei"
        d.reasons.append(f"LEI {t.lei} = {lei_qid} P1278")
        return finish(lei_entity, lei_qid, _item_cik(lei_entity, t, sec, d.reasons))

    # Rule 2: SEC CIK.
    pick = lk.sec_pick.get(t.organization_id)
    if pick:
        cik, score, reasons = pick
        d.rule, d.cik = "sec_cik", cik
        d.reasons.extend(reasons)
        cik_items = list(lk.by_cik.get(cik) or [])
        cik_entity: Mapping[str, Any] | None = None
        cik_qid: str | None = None
        if len(cik_items) > 1:
            d.reasons.append(f"{len(cik_items)} Wikidata items carry CIK {cik}; none attached")
        elif cik_items:
            cik_qid = cik_items[0]
            cik_entity = client.entities[cik_qid]
            labels, aliases = name_keys(cik_entity)
            countries = aux.countries(cik_entity)
            if countries and not countries & t.evidence:
                d.status, d.confidence = "conflict", score
                d.reasons.append(
                    f"CIK item {cik_qid} country {sorted(countries)} not in evidence {sorted(t.evidence)}"
                )
                return d
            leis = {str(v).upper() for v in best_values(cik_entity, "P1278")}
            if t.lei and leis and t.lei not in leis:
                d.status, d.confidence = "conflict", score
                d.reasons.append(f"CIK item {cik_qid} LEI {sorted(leis)} != row LEI {t.lei}")
                return d
            if (labels | aliases) & set(t.keys):
                # The item has the name, not the legal form: `Prologis, L.P.` (the operating
                # partnership) stays marked down against Prologis, Inc.'s CIK and listing.
                form_mismatch = any(r.startswith("legal form ") for r in reasons)
                score = max(score, SEC_CORROBORATED * (W_LEGAL_FORM_MISMATCH if form_mismatch else 1.0))
                d.reasons.append(f"corroborated: CIK item {cik_qid} has the key")
            d.wikidata_rule = "sec_cik"
        d.confidence = round(score, 4)
        if score < threshold:
            d.status = "below_threshold"
            return d
        if cik_entity is None:
            # The SEC match stands; look for a Wikidata item by name, refusing one with another CIK.
            cik_qid, cik_entity = _rule3(t, lk.candidates, aux, threshold, d, required_cik=cik)
            if cik_qid:
                d.wikidata_rule = "name_country"
        return finish(cik_entity, cik_qid, cik)

    if t.organization_id in lk.unsearched:
        d.status = "lookup_failed"
        d.reasons.append("no exact-name candidate and no name search ran; the previous profile is kept")
        return d
    if t.organization_id in lk.sec_rejected:
        d.reasons.extend(lk.sec_rejected[t.organization_id])

    # Rule 3: name and country.
    r3_qid, r3_entity = _rule3(t, lk.candidates, aux, threshold, d, required_cik=None)
    if r3_qid is None:
        return d
    d.rule = d.wikidata_rule = "name_country"
    return finish(r3_entity, r3_qid, _item_cik(r3_entity or {}, t, sec, d.reasons))


def _item_cik(entity: Mapping[str, Any], t: Target, sec: SecTickers, reasons: list[str]) -> str | None:
    """The item's CIK (P5531) when SEC names that registrant with the organisation's key; else None.
    An item can merge a holding company and its subsidiary (Southwest Gas Corp's item carries
    Southwest Gas Holdings' CIK), and a CIK is only worth recording if it is this company's."""
    ciks = [f"{int(v):010d}" for v in best_values(entity, "P5531") if str(v).isdigit()]
    for cik in ciks:
        if sec.names(cik, t.keys):
            return cik
    if ciks:
        cik = ciks[0]
        why = f"SEC names it {sec.name(cik)!r}" if cik in sec.by_cik else "it is not in the SEC ticker file"
        reasons.append(f"CIK {cik} not recorded: {why}")
    return None


def _rule3(
    t: Target,
    candidates: Mapping[str, list[str]],
    aux: _Aux,
    threshold: float,
    d: Decision,
    *,
    required_cik: str | None,
) -> tuple[str | None, Mapping[str, Any] | None]:
    """The one Wikidata item rule 3 accepts for `t`, else `(None, None)` with `d` saying why. With
    `required_cik` (rule 2 already chose the registrant) only an item without a CIK or with that
    one may be attached, and `d`'s status is left alone when none is."""
    scored: list[tuple[float, str, list[str]]] = []
    rejected: list[str] = []
    named_after: list[str] = []
    elsewhere: list[str] = []
    shared: list[str] = []
    for qid in candidates.get(t.organization_id, []):
        entity = aux.client.entities.get(qid)
        if entity is None:
            continue
        score, reasons = score_wikidata(t, entity, aux)
        if score is None:
            rejected.append(f"{qid}: {reasons[0]}")
            if reasons[0].startswith(NAMED_AFTER):
                named_after.append(qid)
            elif reasons[0].startswith(PLACE_ELSEWHERE):
                elsewhere.append(qid)
            elif reasons[0].startswith(DISAMBIGUATION):
                shared.append(qid)
            continue
        if t.lei:
            leis = {str(v).upper() for v in best_values(entity, "P1278")}
            if leis and t.lei not in leis:
                rejected.append(f"{qid}: LEI {sorted(leis)} != row LEI {t.lei}")
                continue
        ciks = {f"{int(v):010d}" for v in best_values(entity, "P5531") if str(v).isdigit()}
        if required_cik and ciks and required_cik not in ciks:
            rejected.append(f"{qid}: CIK {sorted(ciks)} != SEC CIK {required_cik}")
            continue
        if required_cik and "class=business" not in reasons:
            rejected.append(f"{qid}: not a business, and SEC lists registrant {required_cik}")
            continue
        if required_cik and "name=label" not in reasons and len(key_tokens(t.key)) == 1:
            rejected.append(f"{qid}: a one-word key matched only an alias")
            continue
        scored.append((score, qid, reasons))
    d.candidates = [q for _s, q, _r in scored] + [r.split(":", 1)[0] for r in rejected]
    if not scored:
        if required_cik is None:
            d.status = "no_match"
            d.reasons.extend(rejected[:5] or ["no Wikidata item with the key"])
        else:
            d.reasons.append(
                "no Wikidata item by name" + (f" ({'; '.join(rejected[:3])})" if rejected else "")
            )
        return None, None
    scored.sort(key=lambda s: (-s[0], s[1]))
    # A place refused for its state differs from the place this name is (the counties of one name
    # in several states), but not from a company: a town of the name anywhere in the country says
    # the name is not distinctive (`ARCO`, a Maine hydro owner, and Arco, Idaho).
    choosing_a_place = all(aux.is_place(aux.client.entities.get(q) or {}) for _s, q, _r in scored)
    # A disambiguation page says several things share the name. For a business of an ordinary name
    # that is common and says little (`Calpine`, `Cargill`, `Wells Fargo` each have one; the
    # business class and the country decide). For a local body, Wikidata placing every remaining
    # candidate in a state the evidence names settles which (`Monroe County`, New York, beside the
    # "Monroe County" page); otherwise the name is ambiguous (`St. Elizabeth Medical Center`).
    ordinary = not local_scope(t.name) and any("class=business" in rs for _s, _q, rs in scored)
    located = all(any(r.startswith(LOCATED_HERE) for r in rs) for _s, _q, rs in scored)
    competing = named_after + ([] if choosing_a_place else elsewhere)
    competing += [] if ordinary or located else shared
    if competing:
        # "Syracuse, LLC" (an energy-storage project in New York) is named after the city, not
        # Syracuse University; the item refused as a place still says the name is not distinctive.
        if required_cik is None:
            d.status = "ambiguous"
            d.confidence = scored[0][0]
        d.reasons.append(
            f"the key also names {len(competing)} place(s), physical thing(s) or disambiguation "
            f"page(s) {competing[:3]}: the name may be theirs"
        )
        return None, None
    if len(scored) > 1:
        agree = [s for s in scored if any(r.startswith("country=agree") for r in s[2])]
        if len(agree) == 1:
            best = (
                round(agree[0][0] * W_COUNTRY_TIEBREAK, 4),
                agree[0][1],
                [*agree[0][2], "tie broken by country"],
            )
        else:
            if required_cik is None:
                d.status = "ambiguous"
                d.confidence = scored[0][0]
            d.reasons.append(
                f"{len(scored)} Wikidata organisations share the key: {[q for _s, q, _r in scored][:5]}"
            )
            return None, None
    else:
        best = scored[0]
    score, qid, reasons = best
    if score < threshold:
        if required_cik is None:
            d.status, d.confidence = "below_threshold", score
            d.qid = qid
            d.reasons.extend([f"{qid}:", *reasons])
        else:
            d.reasons.append(f"Wikidata {qid} by name scored {score} < {threshold}")
        return None, None
    if required_cik is None:
        d.confidence = score
    d.reasons.extend([f"{qid}:", *reasons])
    return qid, aux.client.entities[qid]


def summarise(
    decisions: Sequence[Decision],
    *,
    client: WikidataClient | None = None,
    sec: SecTickers | None = None,
    lookups: _Lookups | None = None,
    threshold: float = MATCH_THRESHOLD,
) -> dict[str, Any]:
    by_status: dict[str, int] = defaultdict(int)
    by_rule: dict[str, int] = defaultdict(int)
    filled: dict[str, int] = defaultdict(int)
    for d in decisions:
        by_status[d.status] += 1
        if d.status == "matched" and d.rule:
            by_rule[d.rule] += 1
            for name in d.fields:
                filled[name] += 1
    below = sorted((d for d in decisions if d.status == "below_threshold"), key=lambda d: -d.footprint)
    return {
        "targets": len(decisions),
        "threshold": threshold,
        "by_status": dict(sorted(by_status.items())),
        "matched_by_rule": {rule: by_rule.get(rule, 0) for rule in RULES},
        "wikidata_attached": sum(1 for d in decisions if d.status == "matched" and d.qid),
        "with_fields": sum(1 for d in decisions if d.status == "matched" and d.fields),
        "fields_filled": {name: filled.get(name, 0) for name in PROFILE_FIELDS},
        "ticker_withheld": sum(
            1 for d in decisions if any(r.startswith("ticker withheld") for r in d.reasons)
        ),
        "sec_rejected": len(lookups.sec_rejected) if lookups else 0,
        "search": {
            "searched": lookups.searched if lookups else 0,
            "with_candidate": lookups.search_hits if lookups else 0,
            "failed": lookups.search_failed if lookups else 0,
            "not_run": len(lookups.unsearched) if lookups else 0,
            "stopped": lookups.search_stopped if lookups else None,
        },
        "below_threshold_top": [
            {"name": d.name, "confidence": d.confidence, "rule": d.rule, "reasons": d.reasons[:6]}
            for d in below[:25]
        ],
        "requests": {
            "wikidata_sparql": client.requests["sparql"] if client else 0,
            "wikidata_search": client.requests["search"] if client else 0,
            "sec": 1 if sec else 0,
        },
    }


# ---------------------------------------------------------------------------------- apply
@dataclass
class ApplyReport:
    decisions: int = 0
    written: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    removed_personal: int = 0
    #: Profiles removed from rows that are no longer targets (merged, not public, or no assets,
    #: proposals or opportunities), whether or not the fetch decided them.
    removed_not_target: int = 0
    kept_on_failure: int = 0
    skipped_not_eligible: int = 0
    skipped_renamed: int = 0
    #: Websites written or changed (an unchanged rewrite is not counted).
    websites_written: int = 0
    websites_kept_curated: int = 0
    #: Websites not written back because an operator cleared the one this module wrote.
    websites_refused: int = 0

    def as_report(self) -> dict[str, int]:
        return dataclasses.asdict(self)


def _shown(profile: Mapping[str, Any] | None) -> Any:
    """What a reader sees of a profile: the match identity and each field's value."""
    if not profile:
        return None
    match = profile.get("match") or {}
    values = {name: (f or {}).get("value") for name, f in (profile.get("fields") or {}).items()}
    return (match.get("rule"), match.get("wikidata_id"), match.get("cik"), json.dumps(values, sort_keys=True))


def _strip(org: Organization) -> bool:
    """Remove what this module wrote (the profile, and the website while it still holds the value
    this module wrote); True when anything was removed."""
    ids = dict(org.ids or {})
    old = ids.pop(PROFILE_KEY, None)
    if not isinstance(old, dict):
        return False
    written_site = old.get("website_written")
    if written_site and org.website == written_site:
        org.website = None
    org.ids = ids
    return True


def _write(org: Organization, d: Decision, now: dt.datetime, report: ApplyReport) -> None:
    ids = dict(org.ids or {})
    old = ids.get(PROFILE_KEY) if isinstance(ids.get(PROFILE_KEY), dict) else {}
    old_qid = ((old or {}).get("match") or {}).get("wikidata_id")
    fields = dict(d.fields)
    site = (fields.get("website") or {}).get("value")
    prev_site = (old or {}).get("website_written")
    refused = (old or {}).get("website_refused")
    if prev_site and org.website is None:
        # An operator cleared the website this module wrote: it is not written back, nor shown in
        # the profile, while the match is the same item.
        refused = {"value": prev_site, "wikidata_id": old_qid}
    if not isinstance(refused, dict) or refused.get("wikidata_id") != d.qid:
        refused = None
    if refused is not None and site:
        fields.pop("website", None)
        site = None
        report.websites_refused += 1
    website_written: str | None = None
    if org.website is None or (prev_site and org.website == prev_site):
        if site and org.website != site:
            report.websites_written += 1
        org.website = site or None
        website_written = site or None
    elif site:
        report.websites_kept_curated += 1
    match: dict[str, Any] = {
        "rule": d.rule,
        "confidence": d.confidence,
        "wikidata_id": d.qid,
        "wikidata_rule": d.wikidata_rule,
        "cik": d.cik,
        "reasons": d.reasons[:12],
        "matched_at": iso(now),
    }
    profile: dict[str, Any] = {
        "version": PROFILE_VERSION,
        "match": match,
        "fields": fields,
        "website_written": website_written,
    }
    if refused is not None:
        profile["website_refused"] = refused
    if _shown(old) != _shown(profile):
        org.last_changed = now
        report.changed += 1
    else:
        report.unchanged += 1
        match["matched_at"] = ((old or {}).get("match") or {}).get("matched_at") or iso(now)
    ids[PROFILE_KEY] = profile
    org.ids = ids
    report.written += 1


def apply_profiles(session: Session, result: FetchResult, *, now: dt.datetime | None = None) -> ApplyReport:
    """Write the fetch's decisions, and remove the profile of every row that is no longer a target
    (`collect_targets`, read now: the store may have moved since the fetch). Idempotent; never
    touches the network. A target the fetch did not decide (a `--limit` run) keeps its profile."""
    now = now or utcnow()
    registry = Registry()
    for sid in (WIKIDATA_SOURCE_ID, SEC_TICKERS_SOURCE_ID):
        upsert_licence_and_source(session, registry.get(sid), registry.version)
    report = ApplyReport(decisions=len(result.decisions))
    by_id = {d.organization_id: d for d in result.decisions}
    targets = {t.organization_id for t in collect_targets(session)}
    for org in session.scalars(select(Organization)):
        d = by_id.get(str(org.id))
        has_profile = isinstance((org.ids or {}).get(PROFILE_KEY), dict)
        if org.personal_data:
            if has_profile and _strip(org):
                report.removed_personal += 1
            continue
        if str(org.id) not in targets:
            if d is not None:
                report.skipped_not_eligible += 1
            if has_profile and _strip(org):
                org.last_changed = now
                report.removed_not_target += 1
            continue
        if d is None:
            continue
        if org.name_canonical != d.name:
            report.skipped_renamed += 1
            continue
        if d.status == "matched":
            _write(org, d, now, report)
        elif d.status in DEFINITIVE_NEGATIVE:
            if has_profile and _strip(org):
                org.last_changed = now
                report.removed += 1
        elif has_profile:
            report.kept_on_failure += 1
    session.commit()
    return report


def run(
    session: Session,
    transport: Transport,
    *,
    limit: int | None = None,
    search_budget: int = SEARCH_BUDGET,
    search_minutes: float = SEARCH_MINUTES,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    targets = collect_targets(session)
    session.rollback()  # end the read before the network phase, so no snapshot is held open
    result = fetch_profiles(
        None,
        transport,
        targets=targets,
        limit=limit,
        search_budget=search_budget,
        search_minutes=search_minutes,
        progress=progress,
    )
    applied = apply_profiles(session, result)
    return {"fetch": result.report, "apply": applied.as_report()}


def write_decisions(result: FetchResult, path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(result.to_json(), indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_decisions(path: pathlib.Path) -> FetchResult:
    return FetchResult.from_json(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------------- CLI
def _session(db: str) -> Session:
    url = db if "://" in db else f"sqlite+pysqlite:///{db}"
    engine = get_engine(url)
    init_db(engine)
    return get_sessionmaker(engine)()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m services.ingest.org_enrich", description=(__doc__ or "").split("\n\n")[0]
    )
    parser.add_argument("command", choices=("fetch", "apply", "run"))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path or SQLAlchemy URL")
    parser.add_argument("--decisions", default=str(DEFAULT_DECISIONS_PATH))
    parser.add_argument("--limit", type=int, default=None, help="only the N largest targets")
    parser.add_argument("--search-budget", type=int, default=SEARCH_BUDGET, help="name searches allowed")
    parser.add_argument(
        "--search-minutes", type=float, default=SEARCH_MINUTES, help="time the searches may take"
    )
    parser.add_argument("--record", default=None, help="write every exchange to this JSON file (fixtures)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    decisions_path = pathlib.Path(args.decisions)
    with _session(args.db) as session:
        if args.command == "apply":
            out: dict[str, Any] = apply_profiles(session, read_decisions(decisions_path)).as_report()
        else:
            live: Transport = LiveTransport()
            transport: Transport = RecordingTransport(live) if args.record else live
            targets = collect_targets(session)
            session.rollback()
            result = fetch_profiles(
                None,
                transport,
                targets=targets,
                limit=args.limit,
                search_budget=args.search_budget,
                search_minutes=args.search_minutes,
                progress=log.info,
            )
            write_decisions(result, decisions_path)
            if isinstance(transport, RecordingTransport) and args.record:
                pathlib.Path(args.record).write_text(
                    json.dumps({"exchanges": transport.exchanges}, sort_keys=True), encoding="utf-8"
                )
            out = {"fetch": result.report}
            if args.command == "run":
                out["apply"] = apply_profiles(session, result).as_report()
    sys.stdout.write(json.dumps(out, indent=2, default=str) + "\n")


if __name__ == "__main__":
    main()
