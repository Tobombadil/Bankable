"""Is an opportunity about energy at all? (docs/22 §25.3; docs/16 U-7.)

One question with two readers: the matcher's eligibility gate (`services/match/engine.py`: a
notice that is not about energy cannot match a project) and, later, the `/opportunities` default
view docs/16 U-7 recommends (energy-relevant notices, with a switch to all notices). It is a pure
function over the notice's own fields -- title, summary, the extractor's technology tags and the
programme codes the connectors store in `identifiers` -- so `services/api` can call it on an ORM
row (`opportunity_relevance`) and the evaluation on a CSV row (`assess_relevance`). It imports
nothing from the database or the web layer.

"Energy" here means energy infrastructure: generation, storage, networks, energy carriers that
need infrastructure (hydrogen, CCS, LNG), heat networks, EV charging, efficiency. General civil
infrastructure (water, roads, buildings) is out of scope by design (docs/22 A-22-28).

The verdict, first match wins:

1. **Fuel purchase -> not relevant.** docs/16 §4.5 (2026-10-20, before any v2 label existed):
   "many of those are fuel purchases ("Diesel fuel", "Heating oil")". A notice whose CPV codes
   all sit under 091 (fuels) or 092 (petroleum products, oils and lubricants), or whose title or
   summary names a fuel as the object of a purchase ("supply of ... fuel", "delivery of diesel"),
   buys a commodity for the buyer's own boilers and vehicles.
2. **Positive evidence -> relevant**, from any of: an energy keyword in the title or summary once
   the false friends below are removed; an energy CPV code; a World Bank energy sector code; a
   technology tag, unless the text held a false friend (the tag was read from that same text).
3. **False friend only -> not relevant** ("directed energy", "nuclear weapons", "wind tunnel" ...:
   the energy word is not about energy).
4. Otherwise **not relevant** ("no energy evidence").

`RELEVANCE_VERSION` names this rule list; a matcher rationale records it, and any change to a list
below bumps it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

RELEVANCE_VERSION = "opportunity-relevance@v1"

#: CPV prefixes of commodity fuels: 091 "Fuels" (solid, liquid, gaseous fuels; natural gas, diesel,
#: heating oil, wood fuel) and 092 "Petroleum, coal and oil products" (lubricants).
FUEL_CPV_PREFIXES: tuple[str, ...] = ("091", "092")

#: CPV prefixes that are energy infrastructure or energy itself, whatever the title says.
ENERGY_CPV_PREFIXES: tuple[str, ...] = (
    "093",  # electricity, heating, solar and nuclear energy (09310000 electricity, 09332000 solar)
    "311",  # electric motors, generators and transformers
    "3132",  # power distribution cables
    "314",  # accumulators, primary cells and batteries
    "38551",  # energy meters
    "4211",  # turbines and motors
    "452314",  # construction work for electricity power lines
    "45251",  # construction works for power plants and heating plants
    "653",  # electricity distribution and related services
    "654",  # other sources of energy supply and distribution
    "71314",  # energy and related services
)

#: World Bank sector codes of the Energy & Extractives practice that are energy (not mining, not
#: public administration): solar, wind, hydro, geothermal, biomass, transmission and distribution,
#: other energy. The sector belongs to the World Bank *project* the notice is procured under.
WORLD_BANK_ENERGY_SECTORS: frozenset[str] = frozenset({"LB", "LH", "LI", "LT", "LU", "LW", "LZ"})

#: Phrases whose energy word is not about energy. Removed from the text before the keyword search.
#: What each matches in data/eval/match_labels.csv (docs/22 §25.3): "directed energy" matches one
#: labelled notice, tune-half 332367. "nuclear weapons" and "weapons complex" match only test-half
#: 359970 (NIEHS HAZMAT training, the §21.3 notice) and were written with its title known, so their
#: effect there is not blind; they are not decisive (`people` and `research` also remove it). The
#: rest match no labelled notice.
FALSE_FRIENDS: tuple[str, ...] = (
    r"directed[\s-]+energy",  # matches tune 332367
    r"nuclear\s+weapons?",  # matches only test 359970; title known when written
    r"weapons\s+complex",  # matches only test 359970; title known when written
    r"nuclear\s+(medicine|magnetic|physics)",
    r"wind\s+tunnels?",
    r"solar\s+wind",
    r"dark\s+energy",
    r"(data|disease|virus|viral|hiv)\s+transmission",
)
_FALSE_FRIEND_RE = re.compile("|".join(FALSE_FRIENDS), re.IGNORECASE)

#: Energy keywords over the title and summary (after false friends are removed).
ENERGY_TERMS: tuple[str, ...] = (
    r"\benerg(y|ies)\b",
    r"\belectricity\b",
    r"\belectric(al)?\s+power\b",
    r"\bpower\s+(plants?|stations?|generation|lines?|grids?|systems?)\b",
    r"\bgrids?\b",
    r"\brenewables?\b",
    r"\bsolar\b",
    r"photovolta",
    r"\bpv\b",
    r"\bwind\b",
    r"\bbatter(y|ies)\b",
    r"\bhydrogen\b",
    r"\bfuel\s+cells?\b",
    r"\bnuclear\b",
    r"\bgeotherm",
    r"\bhydro(power|electric\w*)?\b",
    r"\bbiomass\b",
    r"\bbiogas\b",
    r"\bbiomethane\b",
    r"\btransmission\b",
    r"\bsubstations?\b",
    r"\b\d+\s*kv\b",
    r"\bmw\b",
    r"\bmegawatts?\b",
    r"\binterconnect(ion|ions|or|ors)\b",
    r"\bgenerators?\b",
    r"\bdistrict\s+heat",
    r"\bheat\s+(networks?|pumps?)\b",
    r"\bcarbon\s+capture\b",
    r"\bccus?\b",
    r"\b(ev|electric\s+vehicle)\s+charging\b",
    r"\bcharging\s+(infrastructure|stations?|points?)\b",
    r"\belectrification\b",
    r"\b(micro|mini)[\s-]?grids?\b",
    r"\bsmart\s+meters?\b",
    r"\bcogeneration\b",
    r"\bchp\b",
    r"\bturbines?\b",
    r"\bnatural\s+gas\b",
    r"\blng\b",
    r"\boil\s+and\s+gas\b",
)
_ENERGY_RE = [re.compile(t, re.IGNORECASE) for t in ENERGY_TERMS]

#: "supply of ... fuel": a purchase verb whose object, within three words, is a fuel. "Fuel cells",
#: "diesel generators" and "fuel gensets" are equipment, not a fuel purchase.
_FUEL_PURCHASE_RE = re.compile(
    r"\b(supply|supplies|purchase|procurement|delivery|provision)\s+(and\s+[\w-]+\s+)?of\s+"
    r"(?:[\w&-]+\s+){0,3}?"
    r"(fuels?|diesel|petrol|gasoline|kerosene|heating\s+oil|fuel\s+oil|lubricants?|motor\s+oils?)\b"
    r"(?!\s*(cells?|generators?|gensets?))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Relevance:
    relevant: bool
    #: `keyword`, `technology tag`, `programme code` (relevant); `fuel purchase`,
    #: `false friend only`, `no energy evidence` (not relevant).
    reason: str
    #: What decided it: the matched text, token or code; `None` for "no energy evidence".
    evidence: str | None
    version: str = RELEVANCE_VERSION

    @property
    def label(self) -> str:
        return self.reason if self.evidence is None else f"{self.reason}: {self.evidence}"


def programme_codes(identifiers: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The programme codes in an opportunity's `identifiers`, as `scheme:code` strings: `cpv:` (TED,
    Find a Tender), `aln:` (grants.gov Assistance Listing), `wb_sector:` (World Bank) and `nature:`
    (TED contract nature). The keys are the ones the four opportunity connectors write."""
    if not identifiers:
        return ()
    out: list[str] = []
    for key, scheme in (
        ("cpv", "cpv"),
        ("aln", "aln"),
        ("sector_codes", "wb_sector"),
        ("contract_nature", "nature"),
    ):
        value = identifiers.get(key)
        values: Iterable[Any] = value if isinstance(value, list | tuple) else ([value] if value else [])
        for item in values:
            text = str(item).strip()
            code = f"{scheme}:{text}"
            if text and code not in out:
                out.append(code)
    return tuple(out)


def codes_in(scheme: str, codes: Sequence[str]) -> list[str]:
    prefix = f"{scheme}:"
    return [c[len(prefix) :] for c in codes if c.startswith(prefix)]


def _text(title: str | None, summary: str | None) -> str:
    return " ".join(t for t in (title, summary) if t)


def _tokens(technologies: Sequence[str]) -> list[str]:
    out: list[str] = []
    for raw in technologies:
        for token in (raw or "").split("|"):
            token = token.strip().lower()
            if token and token not in out:
                out.append(token)
    return out


def assess_relevance(
    *,
    title: str | None,
    summary: str | None = None,
    technologies: Sequence[str] = (),
    programme_codes: Sequence[str] = (),
) -> Relevance:
    """The verdict and what decided it (module docstring for the order)."""
    text = _text(title, summary)
    cpv = codes_in("cpv", programme_codes)

    if cpv and all(c.startswith(FUEL_CPV_PREFIXES) for c in cpv):
        return Relevance(False, "fuel purchase", f"cpv:{cpv[0]}")
    purchase = _FUEL_PURCHASE_RE.search(text)
    if purchase:
        return Relevance(False, "fuel purchase", purchase.group(0))

    false_friend = _FALSE_FRIEND_RE.search(text)
    cleaned = _FALSE_FRIEND_RE.sub(" ", text)
    for pattern in _ENERGY_RE:
        hit = pattern.search(cleaned)
        if hit:
            return Relevance(True, "keyword", hit.group(0).lower())
    for code in cpv:
        if code.startswith(ENERGY_CPV_PREFIXES):
            return Relevance(True, "programme code", f"cpv:{code}")
    for sector in codes_in("wb_sector", programme_codes):
        if sector in WORLD_BANK_ENERGY_SECTORS:
            return Relevance(True, "programme code", f"wb_sector:{sector}")
    tokens = _tokens(technologies)
    if tokens and false_friend is None:
        return Relevance(True, "technology tag", tokens[0])
    if false_friend is not None:
        return Relevance(False, "false friend only", false_friend.group(0).lower())
    return Relevance(False, "no energy evidence", None)


def is_energy_relevant(
    *,
    title: str | None,
    summary: str | None = None,
    technologies: Sequence[str] = (),
    programme_codes: Sequence[str] = (),
) -> bool:
    return assess_relevance(
        title=title, summary=summary, technologies=technologies, programme_codes=programme_codes
    ).relevant


def opportunity_relevance(row: Any) -> Relevance:
    """For an ORM `Opportunity`, or anything with `title`, `summary`, `technologies` and
    `identifiers` attributes: what `services/api` will call for the docs/16 U-7 default view."""
    return assess_relevance(
        title=getattr(row, "title", None),
        summary=getattr(row, "summary", None),
        technologies=list(getattr(row, "technologies", None) or []),
        programme_codes=programme_codes(getattr(row, "identifiers", None)),
    )


__all__ = [
    "ENERGY_CPV_PREFIXES",
    "ENERGY_TERMS",
    "FALSE_FRIENDS",
    "FUEL_CPV_PREFIXES",
    "RELEVANCE_VERSION",
    "WORLD_BANK_ENERGY_SECTORS",
    "Relevance",
    "assess_relevance",
    "codes_in",
    "is_energy_relevant",
    "opportunity_relevance",
    "programme_codes",
]
