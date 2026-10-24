"""What an issuer listing item is, and the facts it states (docs/28 §8.2; docs/21 §3.3).

The issuer pages mix generation solicitations with items a developer cannot bid on: AEP's energy
RFP pages list gas-lateral and firm-transportation RFPs beside its capacity RFPs; CT DEEP's news
index carries paddlecraft-rental and food-concession RFPs, award announcements and RFIs; NJ BPU's
public notices are mostly hearings and stakeholder meetings. Only items classified `generation`
are published, as kind `rfp` (or the row's configured kind). `classify` decides, in order:

1. `not_solicitation` - the title names no solicitation (RFP, request for proposals, solicitation,
   procurement, tranche, "seeking proposals"), or names an award or selection, or only an RFI. The
   title decides: a CT DEEP snippet about a workshop may mention a future procurement;
2. `sell_side` - the issuer is selling (capacity, energy, RECs, a plant), not procuring; "purchase
   and sale agreement" is the buy-side acquisition contract and is removed before this rule;
3. `non_generation` - fuel and gas infrastructure (laterals, firm transportation), transmission-
   only solicitations, services and concessions, efficiency, EV charging, broadband, grants; or no
   generation resource named anywhere in the item;
4. `generation` - everything left: a solicitation for capacity, energy, storage or RECs.

Each verdict carries the rule id that decided it, so a run can show why an item was left out.

The fact extractors never guess (task brief; docs/21 §3.3 "where stated"):

- `capacity_sought_mw` returns a figure only when the item states exactly one distinct MW (or GW)
  figure; "up to 800 MW ... ; up to 300 MW ..." or "350 MW in 2028 and 600 MW in 2029" stay null,
  with every stated figure kept in `raw`.
- `technologies` is the shared keyword classifier (`pipeline.connectors.opportunity`) over the title
  and the item's own description, with "associated transmission" (a generation project's own tie-in)
  removed first; an item that invites every resource (`is_all_source`: all-source, or a capacity
  solicitation named as one, with no technology class) is `[ALL_SOURCE]` (docs/28 B-1, B-7; it was
  `[]` until 2026-10-24).
- Dates in an item's text are read from labelled phrases only ("Proposals Due <date>", "Proposal Due
  Date <date>", "accept bids ... through <date>", "Final RFP Issued <date>"); an unlabelled date in
  the text is never used. A label the utility qualifies as its own ("Self-Build Proposal Due Date")
  is not the bidders' deadline. One date is not from the text: a CT DEEP release whose title
  announces, releases or issues the solicitation opens it on the release's own date, as the listing
  dates it (`extract.news_records`; never for a draft or a request for comment).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

from pipeline.connectors.opportunity import (
    ALL_SOURCE,
    ALL_SOURCE_PHRASE,
    ALL_SOURCE_RE,
    CAPACITY_SOLICITATION_RE,
    QUALIFIED_RESOURCE_RE,
    TECHNOLOGY_CLASS_RE,
    classify_technologies,
)

Category = Literal["generation", "sell_side", "non_generation", "not_solicitation"]

_SOLICITATION = re.compile(
    r"\brfps?\b|\brfos?\b|\brequests? for (?:proposals?|offers?|bids?)\b|\bsolicitations?\b"
    r"|\bsoliciting\b|\bprocurements?\b|\btranche\b|\bseek(?:s|ing)? (?:bids|proposals|offers)\b"
    r"|\bcompetitive solar incentive\b"
)
_AWARD = re.compile(
    r"\bselect(?:s|ed|ion|ions)?\b|\baward(?:s|ed)?\b|\brecipients?\b|\bwinning\b"
    r"|\bcontracts? (?:signed|executed|approved)\b"
)
_RFI_ONLY = re.compile(r"\brfi\b|\brequests? for information\b")
#: The buy-side acquisition contract: "purchase and sale agreement(s) (PSAs)".
_PSA = re.compile(r"purchase\s+and\s+sale\s+agreements?(?:\s*\(psas?\))?")
_SELL_SIDE = re.compile(
    r"\b(?:sale|sell|selling|divest\w*)\s+(?:of\s+)?(?:its\s+|the\s+|our\s+)?"
    r"(?:excess|surplus|capacity|energy|renewable energy (?:credits?|certificates?)|recs?\b|"
    r"environmental attributes|generat\w+|plant|units?|assets?|facilit\w+)"
    r"|\boffers? to (?:buy|purchase)\b|\bpurchase\s+(?:\w+\s+){0,4}from (?:the )?(?:company|utility)\b"
)
#: "REC Sale RFP", "Capacity Sales" in a title (title only: "energy sales" is common in the
#: description of a load-serving procurement).
_SALE_NOUN = re.compile(r"\b(?:recs?|capacity|energy|power|plant|units?|assets?|attributes)\s+sales?\b")
_FUEL_INFRA = re.compile(
    r"natural gas (?:pipeline|lateral|interconnect|transportation|supply)|pipeline lateral|\blateral\b|"
    r"firm (?:gas )?transportation|gas transportation|fuel (?:supply|provider|procurement)|\bcoal supply\b|"
    r"(?:construct|build)\w*[^.]{0,60}\bpipeline\b"
)
_TRANSMISSION_ONLY = re.compile(
    r"\btransmission (?:infrastructure|project|line|facilit\w+)\b|\bcompetitive transmission\b|"
    r"solicitation for (?:a )?transmission\b"
)
_NON_GENERATION_TOPIC = re.compile(
    r"\b(?:rental|concession|concessions|food|beverage|janitorial|vegetation|tree trimming|consulting|"
    r"legal services|auditing|software|fleet|paddlecraft|lifeguard|campground|marina|invasive species|"
    r"grants?\b|broadband|energy efficiency|efficiency|energy savings|weatherization|electric vehicles?|"
    r"ev charging|charging program|demand response|revolving loan|esip)\b"
)
#: A generation resource named in the item (the bare word "energy" is not one: it is in every CT
#: DEEP snippet through the department's own name, which `classify` also strips).
_RESOURCE = re.compile(
    r"\bcapacity\b|\bgenerat(?:ion|ing)\b|\benergy storage\b|\bstorage\b|\bbatter(?:y|ies)\b|\bsolar\b"
    r"|\bwind\b|\boffshore wind\b|\borecs?\b|\brenewable|\brecs?\b|\bhydro\w*|\bnuclear\b|\bgeotherm\w*"
    r"|\bcombined cycle\b|\bgas-fired\b|\bthermal\b|\bmegawatts?\b|\bmw\b|\bzero carbon\b|\bclean energy\b"
    r"|\belectric power\b|\bpower purchase\b|\ball[- ]source\b"
)


@dataclass(frozen=True)
class Verdict:
    category: Category
    rule: str

    @property
    def publishable(self) -> bool:
        return self.category == "generation"


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split())


def classify(title: str, description: str = "", *, ignore: tuple[str, ...] = ()) -> Verdict:
    """The item's category and the rule that decided it (module docstring). `ignore` holds phrases
    removed first, such as the issuer's own name ("Department of Energy and Environmental
    Protection"), which would otherwise read as a resource or topic."""
    t = _norm(title)
    d = _norm(description)
    for phrase in ignore:
        p = _norm(phrase)
        t = t.replace(p, " ")
        d = d.replace(p, " ")
    t_buy = _PSA.sub(" ", t)
    d_buy = _PSA.sub(" ", d)
    if _AWARD.search(t):
        return Verdict("not_solicitation", "not_solicitation.award")
    if not _SOLICITATION.search(t):
        return Verdict("not_solicitation", "not_solicitation.no_solicitation")
    if _RFI_ONLY.search(t) and not _SOLICITATION.search(_RFI_ONLY.sub(" ", t)):
        return Verdict("not_solicitation", "not_solicitation.rfi")
    if _SELL_SIDE.search(t_buy) or _SELL_SIDE.search(d_buy) or _SALE_NOUN.search(t_buy):
        return Verdict("sell_side", "sell_side.issuer_sells")
    if _FUEL_INFRA.search(t) or _FUEL_INFRA.search(d):
        return Verdict("non_generation", "non_generation.fuel_infrastructure")
    if _TRANSMISSION_ONLY.search(t):
        return Verdict("non_generation", "non_generation.transmission")
    if _NON_GENERATION_TOPIC.search(t):
        return Verdict("non_generation", "non_generation.topic")
    if not _RESOURCE.search(t) and not _RESOURCE.search(d):
        return Verdict("non_generation", "non_generation.no_resource")
    return Verdict("generation", "generation")


# ---------------------------------------------------------------------------- facts
_NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
_MW_FIGURE = re.compile(_NUM + r"\s*(?:\(\s*)?(mw|megawatts?|gw|gigawatts?)\b(?:\s*\))?", re.I)


def stated_mw(text: str) -> list[float]:
    """Every MW figure the text states, in order (GW converted)."""
    out: list[float] = []
    for m in _MW_FIGURE.finditer(text or ""):
        value = float(m.group(1).replace(",", ""))
        unit = m.group(2).lower()
        out.append(value * 1000.0 if unit.startswith("g") else value)
    return out


def capacity_sought_mw(text: str) -> float | None:
    """The one MW figure the item states, or None when it states none or several (never summed,
    never picked: module docstring)."""
    figures = {round(v, 3) for v in stated_mw(text)}
    return figures.pop() if len(figures) == 1 else None


_TARGET_MW = re.compile(
    r"\b(?:aims?|seeks?|seeking|intends?|plans?) to (?:award|procure|acquire|contract for)\s+"
    r"(?:an additional\s+|up to\s+|approximately\s+|at least\s+)?" + _NUM + r"\s*(mw|megawatts?|gw)\b",
    re.I,
)


def target_mw(text: str) -> float | None:
    """A procurement target stated with a verb of procurement ("Tranche 2 aims to award an
    additional 645 MW"), for documents that mention many unrelated MW figures. One distinct
    figure or None."""
    found = set()
    for m in _TARGET_MW.finditer(text or ""):
        value = float(m.group(1).replace(",", ""))
        found.add(value * 1000.0 if m.group(2).lower().startswith("g") else value)
    return found.pop() if len(found) == 1 else None


#: Transmission named only as the generation's own tie-in ("zero carbon electric power from nuclear,
#: hydropower, solar, onshore wind and associated transmission", CT DEEP 2026). A published item is
#: already a generation solicitation (transmission-only ones are left out, rule 3), so this phrase
#: names no technology of its own.
_INCIDENTAL_TRANSMISSION = re.compile(r"\b(?:and\s+|with\s+)?associated\s+transmission\b", re.I)


#: The description names the solicitation itself all-source ("this all-source RFP"), not some
#: other one in passing.
_ALL_SOURCE_SOLICITATION = re.compile(
    ALL_SOURCE_PHRASE + r"\s+(?:rfps?|requests?\s+for\s+proposals?|solicitations?|procurements?)\b"
)


def is_all_source(title: str, description: str = "") -> bool:
    """Whether the item invites every resource technology (docs/28 B-1, B-7; review of 2026-10-24,
    finding 1). Yes when the title says all-source and names no technology class ("2026 All-Source
    RFP", not "All-Source Renewable RFP"); or, when the title names no technology and no class,
    when the title is a capacity solicitation ("2025 SPP Capacity RFP", "2023 Accredited
    Deliverable Capacity RFP") or the description calls this solicitation all-source, and the
    description does not qualify what is sought ("renewable capacity", "demand response
    capacity"). The bare word "capacity" anywhere is never enough: I&M's "2024 RFPs" carried it
    only in "by the 2029/30 capacity planning year"."""
    t, d = _norm(title), _norm(description)
    if TECHNOLOGY_CLASS_RE.search(t):
        return False
    if ALL_SOURCE_RE.search(t):
        return True
    if classify_technologies(_INCIDENTAL_TRANSMISSION.sub(" ", title)) or QUALIFIED_RESOURCE_RE.search(d):
        return False
    return bool(CAPACITY_SOLICITATION_RE.search(t) or _ALL_SOURCE_SOLICITATION.search(d))


def technologies(title: str, description: str = "") -> list[str]:
    """Technology tokens the item names, or `[ALL_SOURCE]` for an item that invites every resource
    (`is_all_source`), which every technology filter matches (docs/28 B-1, B-7). Before 2026-10-24
    an all-source item was `[]`, which since B-1 matches no technology, as does an item naming no
    technology and no all-source solicitation (I&M's "2024 RFPs")."""
    if is_all_source(title, description):
        return [ALL_SOURCE]
    return classify_technologies(
        _INCIDENTAL_TRANSMISSION.sub(" ", title), _INCIDENTAL_TRANSMISSION.sub(" ", description)
    )


_CONTRACT = re.compile(r"\b(PSAs?|PPAs?|CPAs?|RECs?|ORECs?|tolling agreements?|BTAs?)\b")


def contract_types(text: str) -> list[str]:
    """Contract forms the item names (PSA, PPA, CPA, REC, OREC, BTA, tolling), in first-seen order."""
    seen: list[str] = []
    for m in _CONTRACT.finditer(text or ""):
        token = m.group(1).upper().rstrip("S") if not m.group(1).lower().startswith("tolling") else "TOLLING"
        if token not in seen:
            seen.append(token)
    return seen


# ---------------------------------------------------------------------------- dates
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
DATE = (
    r"(?:(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})"
)
_DATE_PARTS = re.compile(r"([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", re.I)
_NUMERIC_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_TIME = re.compile(
    r"^\s*,?\s*(?:at|by)?\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?\s*\(?\s*(e[sdp]?t|c[sdp]?t|m[sdp]?t|p[sdp]?t)?\b",
    re.I,
)
_ZONES = {"e": "America/New_York", "c": "America/Chicago", "m": "America/Denver", "p": "America/Los_Angeles"}


def parse_date(text: str) -> dt.date | None:
    """A calendar date from "September 17, 2026", "Sept. 17 2026" or "9/17/2026"."""
    m = _DATE_PARTS.search(text or "")
    if m:
        month = _MONTHS.get(m.group(1).lower()[:3])
        if month:
            try:
                return dt.date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                return None
    m = _NUMERIC_DATE.search(text or "")
    if m:
        try:
            return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def deadline(date_text: str, after: str = "") -> dt.datetime | None:
    """A deadline as an aware UTC datetime: the stated time and zone when the words right after the
    date give them ("at 12:00 PM EPT"; prevailing/standard/daylight all read as the zone's local
    time), else midnight UTC of the date, which is what every other opportunity connector stores
    for a date-only deadline (`pipeline.connectors.opportunity.to_utc`)."""
    day = parse_date(date_text)
    if day is None:
        return None
    m = _TIME.match(after or "")
    if m and m.group(4):
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        minute = int(m.group(2) or 0)
        zone = ZoneInfo(_ZONES[m.group(4).lower()[0]])
        local = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        return local.astimezone(dt.UTC)
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)


def labelled_dates(text: str, labels: re.Pattern[str]) -> list[tuple[str, str]]:
    """(date text, the words after it) for every date that directly follows one of `labels`."""
    out: list[tuple[str, str]] = []
    pattern = re.compile(r"(?:" + labels.pattern + r")[\s:,*-]*(?:on\s+|by\s+|is\s+)?(" + DATE + r")", re.I)
    for m in pattern.finditer(text or ""):
        out.append((m.group(1), (text or "")[m.end() : m.end() + 40]))
    return out
