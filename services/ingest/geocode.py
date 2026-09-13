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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_COUNTY_CENTROID_TSV = Path(__file__).resolve().parent / "data" / "us_county_centroids.tsv"

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

    @classmethod
    def load(cls, path: Path = DEFAULT_COUNTY_CENTROID_TSV) -> CountyGazetteer:
        gaz = cls()
        sums: dict[str, tuple[float, float, int]] = {}
        with path.open(encoding="utf-8") as f:
            next(f)  # header
            for line in f:
                state, county_name, lat_s, lon_s = line.rstrip("\n").split("\t")
                lat, lon = float(lat_s), float(lon_s)
                norm = normalize_county_name(county_name)
                if norm:
                    gaz.counties[(state, norm)] = (lat, lon)
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


def geocode(
    state: str | None, county: str | None, *, gaz: CountyGazetteer | None = None
) -> tuple[tuple[float, float] | None, str]:
    """`(geom, precision)` for a parsed state/county pair (docs/04 D-8 placement precedence,
    restricted to this module's two tiers): a resolvable county -> its centroid, `county_centroid`;
    else a resolvable state -> its centroid, `state_centroid`; else `(None, "unknown")` -- counted
    as unplaced (never dropped), not silently defaulted to a guessed point.
    """
    gaz = gaz or default_gazetteer()
    if county:
        point = gaz.county_point(state, county)
        if point is not None:
            lat, lon = point
            return (lon, lat), "county_centroid"
    if state:
        point = gaz.state_point(state)
        if point is not None:
            lat, lon = point
            return (lon, lat), "state_centroid"
    return None, "unknown"
