"""County/state centroid geocoding for the loader (docs/21-data-model.md §3.7; docs/04-standards.md
D-8).

`services/ingest/loader.py` previously left `location.geom` null for every record (open decision
#2 in `services/README.md`): no geocoder existed, so nothing plotted on the map without a
frontend-side workaround (`web/data_loading.py::backfill_locations`, built directly on this same
public-domain table before this module existed). This module is that geocoder, moved into the
loader itself so `services/` no longer depends on `web/` for it: county name + state code ->
county centroid (US Census Gazetteer 2024, public domain); state code alone -> a derived state
centroid (unweighted mean of that state's county centroids). Neither is a substitute for a real
address-level geocoder — both are categorically `county_centroid` / `state_centroid` precision,
never `exact` (docs/21 §3.7's precision vocabulary; `exact` requires real coordinates from the
source itself, e.g. EIA-860M's `Latitude`/`Longitude`, which this module does not touch).

The vendored TSV (`services/ingest/data/us_county_centroids.tsv`) is a copy of
`web/data_ref/us_county_centroids.tsv` (same public-domain source), so a location string a
connector already parsed geocodes the same way whether it is loaded through `web/dev_up.py` or a
real ingestion run — `services/` no longer needs `web/` to plot anything on a map.

2026-09-15: the vendored TSV had dropped the source Gazetteer's `GEOID` (county FIPS) column,
leaving `docs/21` §3.7's `location.county_fips` NULL on every row. `GEOID` was re-derived from the
same 2024 Gazetteer file and appended as a fifth `county_fips` column (`services/ingest/data/
README.md` has the join method and edge cases); `web/data_ref/us_county_centroids.tsv` is
untouched and still has four columns, so it is no longer byte-identical to this copy. `geocode()`
still returns the same `(point, precision)` 2-tuple; a caller wanting FIPS calls
`CountyGazetteer.county_fips(state, county_name)` directly (`loader.py::_get_or_create_location`
does, for both the county-centroid and exact-point paths).

Sprint 3 item 5 adds a second, unrelated tier: `gb.neso.tec_register` rows carry a "Connection
Site" -- a transmission substation name -- and nothing else geographic (docs/02 NESO row). A
substation is not the project's location (the plant can be kilometres away), so this is not, and
must never become, `exact` (docs/21 §3.7); it is a site-level proxy comparable in scale to a
county centroid, so it is stamped `county_centroid` the same as the US tier. `SubstationGazetteer`
resolves it against the vendored `services/ingest/data/gb_substations.tsv` (NESO Open Data
Licence -- see `services/ingest/data/README.md` for the exact source, licence text and retrieval
date). `geocode()` takes this path only when `country` starts with "GB"; `loader.py` does not pass
`country` yet (it hardcodes `country="US"` when building `Location`) -- wiring that argument at the
call site, and the one-line follow-up below, is left to the coordinator.

`geocode()` still returns a 2-tuple, unchanged, because `loader.py` unpacks it positionally today
(`point, precision = geocode(state, county, gaz=gaz)`) and this module does not touch `loader.py`.
There is nowhere in that tuple to carry which geocoder tier produced the point, so
`docs/21` §3.7's `Location.geocoder` field cannot be set from here: when the coordinator wires the
`country` argument at the loader's call site, it should also set `Location.geocoder =
"gb_substation"` there for rows where `country` starts with "GB" and `precision == "county_centroid"`
(mirroring the `precision_reason="licence"` pattern already at that call site) -- `census_tiger`
would be wrong, and `docs/21`'s `geocoder` vocabulary does not yet name this tier so it may need
extending there too.

2026-09-20 adds a fallback *inside* the GB tier: where the Connection Site names no public Grid
Supply Point but does name a real settlement ("Cilfynydd 400kV Substation", "Navenby", "Chirk
GSP"), `SettlementGazetteer` resolves that place name against the vendored
`services/ingest/data/gb_settlements.tsv` (ONS Index of Place Names GB 2024, Open Government
Licence v3). The GSP lookup still runs first and still wins. A name shared by more than one
locality is left `unknown`, never tie-broken. This tier is reported `county_centroid` too -- see
`geocode()`'s docstring for why that is the honest grade and not a new one. The coordinator's
`Location.geocoder` follow-up above now has a value for it: `docs/21` §3.7's vocabulary names both
`gb_substation` and `gb_settlement`, though nothing sets either yet. `geocode()`'s 2-tuple still
cannot say which tier produced a point, so a caller that needs to tell them apart asks
`default_substation_gazetteer().substation_point(site)` first, exactly as this module does.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

DEFAULT_COUNTY_CENTROID_TSV = Path(__file__).resolve().parent / "data" / "us_county_centroids.tsv"
DEFAULT_GB_SUBSTATION_TSV = Path(__file__).resolve().parent / "data" / "gb_substations.tsv"
DEFAULT_GB_SETTLEMENT_TSV = Path(__file__).resolve().parent / "data" / "gb_settlements.tsv"

#: cos(55°N), the middle of GB: scales a longitude difference to the same units as a latitude
#: difference so squared distances can be compared. Only ever used to rank neighbours.
COS_GB = 0.5736

#: The transmission owner each NESO TEC register row names in `HOST TO`, mapped to the
#: transmission area `gb_substations.tsv` records per GSP. `OFTO` is deliberately absent: an
#: offshore transmission owner has no onshore area, so an OFTO row can never satisfy the
#: settlement tier's region precondition and is never placed by it.
GB_TRANSMISSION_OWNER_REGIONS: dict[str, str] = {
    "NGET": "england_wales",
    "SPT": "south_scotland",
    "SHET": "north_scotland",
}

_COUNTY_SUFFIX_RE = re.compile(r"\b(COUNTY|PARISH|BOROUGH|CENSUS AREA|MUNICIPALITY|CITY AND BOROUGH|CITY)\b")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9 ]")

# NYISO's free-text `county` field names NYC boroughs and carries a handful of misspellings rather
# than the Gazetteer's county name; a multi-county span ("Oneida-Dutchess") is left unmatched on
# purpose -- the state-centroid fallback is the honest placement for those, not a guess at one of
# the counties named (same reasoning `web/build_data.py`'s original table vendoring used).
_COUNTY_ALIASES: dict[str, str] = {
    "BROOKLYN": "KINGS",
    "MANHATTAN": "NEW YORK",
    "STATEN ISLAND": "RICHMOND",
    "THOMPKINS": "TOMPKINS",
    "OSTEGO": "OTSEGO",
}


def normalize_county_name(name: str | None) -> str | None:
    """Match `docs/21` §3.7 `county_name` free text against the Census Gazetteer spelling."""
    if not name:
        return None
    upper = name.upper()
    upper = _COUNTY_SUFFIX_RE.sub("", upper)
    upper = _NON_ALNUM_RE.sub("", upper)
    normalized = " ".join(upper.split())
    if not normalized:
        return None
    return _COUNTY_ALIASES.get(normalized, normalized)


@dataclass
class CountyGazetteer:
    """US county centroids (Census Gazetteer 2024, public domain) plus a derived state centroid
    (unweighted mean of that state's county centroids), mirroring `web/build_data.py`'s prototype
    table exactly so the two loaders geocode identically."""

    counties: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)
    state_centroids: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: US county FIPS (docs/21 §3.7 `county_fips`, char(5): 2-digit state + 3-digit county), keyed
    #: identically to `counties` and populated in the same pass over the same TSV line, so a given
    #: `(state, norm)` key's FIPS always names the same row that key's lat/lon came from -- even
    #: for the half-dozen state/independent-city pairs that collide under `normalize_county_name`
    #: (e.g. VA's "Fairfax County" vs "Fairfax city" both normalise to `("VA", "FAIRFAX")`; the
    #: last row in the TSV wins for both dicts together, never a mismatched pairing of one row's
    #: point with another row's FIPS).
    fips: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = DEFAULT_COUNTY_CENTROID_TSV) -> CountyGazetteer:
        gaz = cls()
        sums: dict[str, tuple[float, float, int]] = {}
        with path.open(encoding="utf-8") as f:
            next(f)  # header
            for line in f:
                state, county_name, lat_s, lon_s, fips_s = line.rstrip("\n").split("\t")
                lat, lon = float(lat_s), float(lon_s)
                norm = normalize_county_name(county_name)
                if norm:
                    gaz.counties[(state, norm)] = (lat, lon)
                    gaz.fips[(state, norm)] = fips_s
                slat, slon, n = sums.get(state, (0.0, 0.0, 0))
                sums[state] = (slat + lat, slon + lon, n + 1)
        gaz.state_centroids = {s: (slat / n, slon / n) for s, (slat, slon, n) in sums.items() if n}
        return gaz

    def county_point(self, state: str | None, county_name: str | None) -> tuple[float, float] | None:
        if not state:
            return None
        norm = normalize_county_name(county_name)
        if not norm:
            return None
        return self.counties.get((state.upper(), norm))

    def county_fips(self, state: str | None, county_name: str | None) -> str | None:
        """The county's 5-digit FIPS (Census Gazetteer `GEOID`), or `None` if `state`/`county_name`
        is missing or does not resolve to a vendored county -- never derived from a coordinate
        (docs/21 §3.7: `county_fips` is set only when a source's own county name resolves)."""
        if not state:
            return None
        norm = normalize_county_name(county_name)
        if not norm:
            return None
        return self.fips.get((state.upper(), norm))

    def state_point(self, state: str | None) -> tuple[float, float] | None:
        if not state:
            return None
        return self.state_centroids.get(state.upper())


_CACHED_GAZETTEER: CountyGazetteer | None = None


def default_gazetteer() -> CountyGazetteer:
    """Process-wide cache: the loader calls this once per row, and the TSV (3,222 rows) is cheap
    to parse but not free -- loading ~10,400 proposals should not re-read and re-parse it 10,400
    times."""
    global _CACHED_GAZETTEER
    if _CACHED_GAZETTEER is None:
        _CACHED_GAZETTEER = CountyGazetteer.load()
    return _CACHED_GAZETTEER


# NESO TEC register "Connection Site" strings name a substation with a voltage/role suffix that
# varies row to row ("Berkswell GSP", "Aberthaw 275kV Substation", "Blackhill 132/33kV") while the
# open gazetteer (`gb_substations.tsv`, sourced from NESO's own FES Grid Supply Point list, see
# `services/ingest/data/README.md`) records the bare site name only. The dual-voltage pattern
# ("132/33kV") must be stripped before the single-voltage one, or "132/" is left as a stray token.
# Word-boundary tokens are stripped before punctuation/whitespace so "SUBSTATION" etc. match whole
# words, not substrings. Whitespace is dropped last, and entirely (not collapsed to one space): the
# fixture pairs a one-word Connection Site with a two-word gazetteer name for the same real site
# ("Upperboat" / "Upper Boat"; "Clydesmill" / "Clyde's Mill", apostrophe and all) often enough that
# this is a spelling variant to normalise past, not a coincidence -- verified against the 371-row
# gazetteer to introduce no cross-site collisions (two different sites reduced to the same key).
_GB_KV_PAIR_RE = re.compile(r"\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?\s*KV\b")
_GB_KV_RE = re.compile(r"\d+(?:\.\d+)?\s*KV\b")
_GB_SUBSTATION_WORD_RE = re.compile(r"\bS/?S\b|\bSUBSTATIONS?\b|\bGRID SUPPLY POINT\b|\bGSP\b")
_GB_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")


def normalize_substation_name(name: str | None) -> str | None:
    """Match a NESO TEC register `Connection Site` string against `gb_substations.tsv`'s bare
    site names -- case, the "GSP"/"Substation"/"S/S" role suffix, voltage suffixes (single or
    dual-voltage), and remaining punctuation/whitespace all fold away; `None` (or blank) stays
    `None` rather than becoming an empty-string key that could spuriously match a blank row."""
    if not name:
        return None
    upper = name.upper()
    upper = _GB_KV_PAIR_RE.sub("", upper)
    upper = _GB_KV_RE.sub("", upper)
    upper = _GB_SUBSTATION_WORD_RE.sub("", upper)
    normalized = _GB_NON_ALNUM_RE.sub("", upper)
    return normalized or None


@dataclass
class SubstationGazetteer:
    """GB transmission substation points (NESO Open Data Licence; see
    `services/ingest/data/README.md` for the exact dataset, licence text and retrieval date this
    was vendored under). A site-level proxy, not the project's own location -- `geocode()` never
    reports this tier as `exact` (docs/21 §3.7)."""

    points: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: Every vendored GSP as `(lat, lon, region)`, in file order. The region is the transmission
    #: area the GSP's own NESO "GSP Group" places it in (2026-09-20 column, see
    #: `services/ingest/data/README.md`): `_P` -> `north_scotland`, `_N` -> `south_scotland`,
    #: the other twelve groups -> `england_wales`. This is the only authoritative GB transmission
    #: geography in the repository, and `region_of_point()` below is what the settlement tier's
    #: region precondition is built on.
    sites: list[tuple[float, float, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = DEFAULT_GB_SUBSTATION_TSV) -> SubstationGazetteer:
        gaz = cls()
        with path.open(encoding="utf-8") as f:
            next(f)  # header
            for line in f:
                name, lat_s, lon_s, region = line.rstrip("\n").split("\t")
                lat, lon = float(lat_s), float(lon_s)
                gaz.sites.append((lat, lon, region))
                norm = normalize_substation_name(name)
                if norm and norm not in gaz.points:
                    gaz.points[norm] = (lat, lon)
        return gaz

    def substation_point(self, name: str | None) -> tuple[float, float] | None:
        norm = normalize_substation_name(name)
        if not norm:
            return None
        return self.points.get(norm)

    def region_of_point(self, lat: float, lon: float) -> str | None:
        """Which transmission area a point falls in, by nearest vendored Grid Supply Point.

        A nearest-neighbour classifier over the 371 GSPs, not a boundary polygon: no licence-area
        geometry is published under terms this repository has read, and the GSP network *is* the
        transmission system, so "whose network is this point in the catchment of" is the question
        the register's `HOST TO` answers too. It needs no tuned radius, which a band or a buffer
        would. Validated against the 346 Connection Sites the GSP tier already places, where the
        register's own `HOST TO` and the gazetteer's own region are both known independently --
        `services/ingest/data/README.md` records the agreement measured.
        """
        if not self.sites:
            return None
        best_region = None
        best = float("inf")
        for slat, slon, region in self.sites:
            # Equirectangular approximation: exact enough to rank neighbours at GB latitudes, and
            # this only ever picks a nearest point, never reports a distance.
            dlon = (slon - lon) * COS_GB
            d = dlon * dlon + (slat - lat) * (slat - lat)
            if d < best:
                best, best_region = d, region
        return best_region


# ----------------------------------------------------------------- GB settlement tier (2026-09-20)
# The GSP tier above places 739 of the TEC register's 2,198 rows. Of the 1,459 it leaves, 1,010
# name a real UK settlement rather than a public Grid Supply Point ("Cilfynydd 400kV Substation",
# "Navenby", "Chirk GSP"): GB transmission sites are overwhelmingly named after the place they
# stand next to. `gb_settlements.tsv` (ONS Index of Place Names GB 2024, Open Government Licence
# v3 -- see `services/ingest/data/README.md`) resolves that place name to the locality's point.
#
# Three rules keep this honest:
#   * the GSP lookup always runs first and always wins. A substation's own coordinates beat a
#     settlement centroid; this tier is only ever a fallback for a site the GSP list does not name.
#   * a name borne by more than one place is left unplaced, never tie-broken. UK settlement names
#     repeat: "Thornton" is 6 places up to 5 degrees apart, "Overton" is 16. Nothing in a
#     Connection Site string says which one, so a guess would be a fabricated coordinate and
#     `unknown` is the truthful answer (docs/04 D-8: unplaced, never dropped). The vendored table
#     carries that count per name in its `places` column; this module only ever reads it.
#   * word boundaries are kept, unlike the GSP tier, which squashes them ("Upperboat" matching
#     "Upper Boat"). Squashing them here placed the TEC register's "Whitelee 275/33kV" -- the
#     Whitelee wind farm south of Glasgow -- on "White Lee" in Kirklees, 220 km away. The GSP list
#     is 371 hand-checked names where that tolerance was verified to introduce no collision; a
#     52,110-name settlement index is not, so it does not get the tolerance.
#
# The candidate name is the Connection Site with its voltage and role suffixes removed. On top of
# the GSP stripping above this also drops the site-role words that only ever qualify a substation
# ("Switching Station", "Converter Station", "Collector", "Extension") and up to two trailing unit
# designators -- "Alness 2", "Aberthaw B", "Spittal 2 400kV Substation" name the second or third
# substation at one place, and the place is still Alness, Aberthaw, Spittal. Nothing else is
# stripped: a project-specific name ("17 ACRES BESS", "Alyth Solar and Battery") keeps the words
# that make it project-specific and therefore matches no locality, which is the intended outcome.
#
# Measured failure mode, left open for the coordinator. Cross-checking all 166 settlement-tier
# placements against the transmission owner the register names in `HOST TO` (SHET = north
# Scotland, SPT = south Scotland, NGET = England and Wales) puts 11 of them outside that owner's
# licence area; inspection says ~8 are genuinely wrong and the rest are the crude latitude bands
# used for the check, not the placement (Carradale in Kintyre, Cousland in Midlothian). The same
# check over the GSP tier flags 1 of 346. The wrong ones are all Scottish sites named after a hill
# or wind farm that collides with a small English locality of the same name -- "Torness" (the East
# Lothian nuclear station) landing on a Torness in Highland, "Griffin" (Perthshire) on a Griffin in
# Lancashire, "Greens" (a SHET site for the Moray Firth offshore farms) on a Greens in Rossendale.
# No property of the matched gazetteer record distinguishes these: the constraint that would is the
# `HOST TO` region, which this function never sees (`state` is accepted and unused on the GB path,
# and `loader.py` does not pass `country` at all yet). Wiring a region constraint through belongs
# with wiring `country` through, and both are the coordinator's call -- until then this tier is
# dormant. `services/ingest/data/README.md` records the full measurement.
_GB_SITE_ROLE_RE = re.compile(r"\bSWITCHING STATION\b|\bCONVERTER STATION\b|\bCOLLECTOR\b|\bEXTENSION\b")
_GB_UNIT_SUFFIX_RE = re.compile(r"\s+(?:\d{1,2}|[A-Z])\s*$")
_GB_NON_ALNUM_SPACE_RE = re.compile(r"[^A-Z0-9 ]")


def normalize_settlement_name(name: str | None) -> str | None:
    """Fold a gazetteer place name to its lookup key: case and punctuation only, spaces kept.

    Deliberately *not* the same function as `gb_settlement_key()` below -- the gazetteer side must
    never have substation vocabulary stripped out of it, or a locality genuinely called "Collector"
    or "Grid" would lose its name.
    """
    if not name:
        return None
    return " ".join(_GB_NON_ALNUM_SPACE_RE.sub(" ", name.upper()).split()) or None


def gb_settlement_key(name: str | None) -> str | None:
    """The settlement-name portion of a NESO TEC register `Connection Site`, as a lookup key.

    See the block comment above for what is stripped and why. Substitutions leave a space behind
    (unlike `normalize_substation_name`, which squashes straight to a key) because both the
    trailing unit-designator rule and the key itself need word boundaries to survive.
    """
    if not name:
        return None
    upper = name.upper()
    upper = _GB_KV_PAIR_RE.sub(" ", upper)
    upper = _GB_KV_RE.sub(" ", upper)
    upper = _GB_SUBSTATION_WORD_RE.sub(" ", upper)
    upper = _GB_SITE_ROLE_RE.sub(" ", upper)
    upper = " ".join(upper.split())
    for _ in range(2):
        stripped = _GB_UNIT_SUFFIX_RE.sub("", upper).strip()
        if stripped == upper:
            break
        upper = stripped
    return normalize_settlement_name(upper)


@dataclass
class SettlementGazetteer:
    """GB settlement points (ONS Index of Place Names GB 2024, OGL v3; see
    `services/ingest/data/README.md` for the source, the licence comparison and how the table was
    trimmed).

    `points` holds only the names the vendored table marks as borne by exactly one place.
    `ambiguous` maps the rest to that count, so a caller can tell "this is several real places"
    from "this is not a place at all" -- both resolve to no point, but only the first is a match
    this module refused.
    """

    points: dict[str, tuple[float, float]] = field(default_factory=dict)
    ambiguous: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = DEFAULT_GB_SETTLEMENT_TSV) -> SettlementGazetteer:
        gaz = cls()
        with path.open(encoding="utf-8") as f:
            next(f)  # header
            for line in f:
                name, lat_s, lon_s, _district, places_s = line.rstrip("\n").split("\t")
                key = normalize_settlement_name(name)
                if not key:
                    continue
                places = int(places_s)
                if key in gaz.points or key in gaz.ambiguous:
                    # Two vendored spellings folding to one key are two entries under one name,
                    # which is exactly the case this tier refuses to resolve.
                    gaz.points.pop(key, None)
                    gaz.ambiguous[key] = gaz.ambiguous.get(key, 1) + places
                elif places == 1 and lat_s and lon_s:
                    gaz.points[key] = (float(lat_s), float(lon_s))
                else:
                    gaz.ambiguous[key] = places
        return gaz

    def settlement_point(self, name: str | None) -> tuple[float, float] | None:
        """The locality point for a Connection Site string, or `None` when the stripped name is
        not a settlement *or* names several of them."""
        key = gb_settlement_key(name)
        if not key:
            return None
        return self.points.get(key)

    def is_ambiguous(self, name: str | None) -> bool:
        """True when the stripped name is a real settlement name borne by more than one place --
        the case this module refuses to place. Reporting and tests only; `geocode()` returns
        `unknown` either way."""
        key = gb_settlement_key(name)
        if not key:
            return False
        return key in self.ambiguous


#: Every way the settlement tier can end, for data-quality reporting. `geocode()` collapses all
#: five non-placing outcomes to `unknown` because `docs/21` §3.7's precision vocabulary has no
#: value for "a match was found and refused", and inventing one is a schema change with a CHECK
#: constraint behind it. The distinction is real and worth counting, so it is reported here rather
#: than smuggled into the precision grade.
GB_SETTLEMENT_OUTCOMES = (
    "placed",
    "no_region",  # caller passed no region: the precondition, not a property of the name
    "unknown_region",  # a transmission owner with no onshore area (OFTO) or an unrecognised code
    "not_a_settlement",  # the stripped name is not in the gazetteer at all
    "ambiguous",  # the name is borne by more than one real place
    "outside_region",  # the name resolves, but to a place in another transmission owner's area
)


def gb_settlement_match(
    name: str | None,
    region: str | None,
    *,
    gaz: SubstationGazetteer | None = None,
    settlements: SettlementGazetteer | None = None,
) -> tuple[tuple[float, float] | None, str]:
    """Resolve a Connection Site to a settlement point under the region precondition.

    Returns `((lat, lon), "placed")` or `(None, <outcome>)` from `GB_SETTLEMENT_OUTCOMES`.
    `region` is the transmission owner the register names in its `HOST TO` column -- `NGET`,
    `SPT`, `SHET` or `OFTO`. **Without it nothing is placed**, which is the point: this is a
    precondition, not a filter applied to a result that would otherwise stand. Wiring `country`
    through at `loader.py`'s call site therefore cannot, on its own, switch on unconstrained
    settlement placement -- a caller has to supply the region as well, deliberately.
    """
    region_key = (region or "").strip().upper()
    if not region_key:
        return None, "no_region"
    to_region = GB_TRANSMISSION_OWNER_REGIONS.get(region_key)
    if to_region is None:
        return None, "unknown_region"
    settlement_gaz = settlements if settlements is not None else default_settlement_gazetteer()
    point = settlement_gaz.settlement_point(name)
    if point is None:
        return None, "ambiguous" if settlement_gaz.is_ambiguous(name) else "not_a_settlement"
    sub_gaz = gaz if gaz is not None else default_substation_gazetteer()
    if sub_gaz.region_of_point(*point) != to_region:
        return None, "outside_region"
    return point, "placed"


_CACHED_SETTLEMENT_GAZETTEER: SettlementGazetteer | None = None


def default_settlement_gazetteer() -> SettlementGazetteer:
    """Process-wide cache, mirroring `default_gazetteer()`. This TSV is the largest of the three
    (52,110 rows), so re-parsing it per row would be the expensive mistake."""
    global _CACHED_SETTLEMENT_GAZETTEER
    if _CACHED_SETTLEMENT_GAZETTEER is None:
        _CACHED_SETTLEMENT_GAZETTEER = SettlementGazetteer.load()
    return _CACHED_SETTLEMENT_GAZETTEER


_CACHED_SUBSTATION_GAZETTEER: SubstationGazetteer | None = None


def default_substation_gazetteer() -> SubstationGazetteer:
    """Process-wide cache, mirroring `default_gazetteer()` (the TSV is small -- ~370 rows -- but
    there is no reason to re-parse it per row either)."""
    global _CACHED_SUBSTATION_GAZETTEER
    if _CACHED_SUBSTATION_GAZETTEER is None:
        _CACHED_SUBSTATION_GAZETTEER = SubstationGazetteer.load()
    return _CACHED_SUBSTATION_GAZETTEER


def geocode(
    state: str | None,
    county: str | None,
    *,
    gaz: CountyGazetteer | SubstationGazetteer | None = None,
    country: str | None = None,
    settlements: SettlementGazetteer | None = None,
    region: str | None = None,
) -> tuple[tuple[float, float] | None, str]:
    """`(geom, precision)` for a parsed state/county pair (docs/04 D-8 placement precedence).

    `country` starting with "GB" (case-insensitive) switches tiers entirely: `county` is then read
    as the NESO TEC register's "Connection Site" substation name (the connector keeps that field
    named `county` on purpose -- see `pipeline/connectors/gb_neso_tec_register/connector.py`) and
    looked up in the vendored `SubstationGazetteer`; a hit is `county_centroid` (a substation is
    not the project's location -- never `exact`, docs/21 §3.7), a miss falls through to the
    settlement tier below. `state` is unused on this path -- NESO does not carry one -- and there
    is no state-level GB fallback.

    The GB settlement fallback (2026-09-20) runs only when the GSP lookup missed, because a
    substation's own coordinates always beat a settlement centroid. It strips the voltage and role
    suffixes off the Connection Site and looks the remaining place name up in the vendored
    `SettlementGazetteer`; a name that resolves to exactly one locality is `county_centroid`, a
    name that resolves to several is left `unknown` rather than tie-broken, and so is a name that
    is not a settlement at all.

    **`region` is a precondition of that fallback, not a filter on it.** It is the transmission
    owner the source names (`HOST TO`: NGET, SPT, SHET, OFTO), and without it the settlement tier
    places nothing at all -- so wiring `country` through at a call site cannot, by itself, turn on
    unconstrained settlement placement. A match whose point falls in another owner's transmission
    area is refused; `gb_settlement_match()` says which of the five refusals happened. The GSP
    tier takes no region: those 371 names are NESO's own hand-maintained list of the substations
    the register connects into, a name there is the site rather than a place that shares its name,
    and the same cross-check finds 1 disagreement in 346 against the settlement tier's 11 in 166.

    Both GB tiers report `county_centroid`, which is the honest grade for each. The vocabulary
    (docs/21 §3.7) has no bucket between `exact` and `county_centroid`, and `exact` is out of the
    question for either: what the grade has to describe is how far the project may be from the
    point, and for both tiers that is bounded by the same thing -- the distance a generator can sit
    from the substation it connects into, which is county-scale. The settlement tier adds one more
    term to that error (the locality centroid is not the substation), typically a few kilometres,
    which does not move it into a different bucket. `state_centroid` would be wrong in the other
    direction: it would throw away a locality-scale point we actually have.

    Otherwise (the original, default behaviour, unchanged for `country=None`): a resolvable county
    -> its centroid, `county_centroid`; else a resolvable state -> its centroid, `state_centroid`;
    else `(None, "unknown")` -- counted as unplaced (never dropped), not silently defaulted to a
    guessed point.
    """
    if country and country.upper().startswith("GB"):
        sub_gaz = gaz if isinstance(gaz, SubstationGazetteer) else default_substation_gazetteer()
        point = sub_gaz.substation_point(county)
        if point is not None:
            lat, lon = point
            return (lon, lat), "county_centroid"
        sub_or_none = gaz if isinstance(gaz, SubstationGazetteer) else None
        point, _outcome = gb_settlement_match(county, region, gaz=sub_or_none, settlements=settlements)
        if point is not None:
            lat, lon = point
            return (lon, lat), "county_centroid"
        return None, "unknown"

    county_gaz = gaz if isinstance(gaz, CountyGazetteer) else default_gazetteer()
    if county:
        point = county_gaz.county_point(state, county)
        if point is not None:
            lat, lon = point
            return (lon, lat), "county_centroid"
    if state:
        point = county_gaz.state_point(state)
        if point is not None:
            lat, lon = point
            return (lon, lat), "state_centroid"
    return None, "unknown"


# ============================================================ county_fips backfill CLI (2026-09-15)
# The gazetteer logic above is pure and DB-free; this section is the one place in the module that
# talks to the store, added only because the county_fips defect fix needed a standalone,
# idempotent backfill command and this module already owns the lookup it runs
# (`CountyGazetteer.county_fips`). `services.db` is imported lazily inside `main()`, not at module
# level, so importing `geocode` for its pure gazetteer classes (as `test_geocode.py` and
# `pipeline/` code do) never pulls in SQLAlchemy model wiring just to run.
def backfill_county_fips(session: Session, gaz: CountyGazetteer | None = None) -> dict[str, Any]:
    """Fill `location.county_fips` (docs/21 §3.7) on every existing row where it is currently NULL
    and resolvable, using the exact same lookup `loader.py::_get_or_create_location` runs at load
    time (`CountyGazetteer.county_fips`, keyed on the row's own stored `state_code`/`county_name`
    -- never on `geom`: there is no point-in-polygon here, so a coordinate never fills this column).

    Idempotent: only rows with `county_fips IS NULL` are examined, so a row this call fills is
    never touched again, and a second run over the same data fills 0. A non-US row, or a US row
    whose `county_name` doesn't resolve in the vendored gazetteer (misspelling, multi-county span,
    no county at all), is left NULL and counted under `unresolved` -- not guessed.
    """
    from services.db.models import Location  # local import: see module note above

    county_gaz = gaz if gaz is not None else default_gazetteer()
    by_precision: dict[str, dict[str, int]] = {}
    rows_seen = 0
    filled = 0
    for loc in session.scalars(select(Location).where(Location.county_fips.is_(None))):
        rows_seen += 1
        bucket = by_precision.setdefault(loc.precision, {"seen": 0, "filled": 0})
        bucket["seen"] += 1
        if loc.country != "US" or not loc.county_name:
            continue
        state = loc.state_code[3:] if loc.state_code and loc.state_code.startswith("US-") else None
        fips = county_gaz.county_fips(state, loc.county_name)
        if fips:
            loc.county_fips = fips
            filled += 1
            bucket["filled"] += 1
    session.commit()
    return {
        "rows_seen": rows_seen,
        "filled": filled,
        "unresolved": rows_seen - filled,
        "by_precision": by_precision,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backfill_parser = subparsers.add_parser(
        "backfill-fips",
        help="Fill location.county_fips on existing rows where it is NULL and resolvable.",
    )
    backfill_parser.add_argument("--db", type=Path, default=Path("web/.data/dev.db"))
    args = parser.parse_args(argv)

    if args.command == "backfill-fips":
        from services.db.session import get_engine, get_sessionmaker, init_db  # local: see note above

        db_url = os.environ.get("DATABASE_URL") or f"sqlite+pysqlite:///{args.db}"
        engine = get_engine(db_url)
        init_db(engine)
        session_factory = get_sessionmaker(engine)
        with session_factory() as session:
            result = backfill_county_fips(session)
        print(json.dumps(result))  # noqa: T201 — CLI summary line


if __name__ == "__main__":
    main()
