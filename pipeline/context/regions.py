"""Vendor region polygons for `docs/adr/0008-assets-first-class-and-placement-grades.md` §3:
region-grade proposals (county/state/country precision) are drawn as highlighted polygons, never
as points, and the map's regions endpoint serves them from a vendored file rather than inlining
one per proposal.

Sources (both recorded in `data/sources.yaml`):
- US counties and states: Census Bureau cartographic boundary files, 1:20,000,000 generalisation
  (id `us.census.cartographic_boundaries`), read with `pyshp` -- no `geopandas` dependency, per
  this task's own constraint; the tile/geo stack elsewhere in the repo is pure-Python too.
- Countries: Natural Earth 1:110m Admin 0 Countries (public domain), taken as GeoJSON from its
  GitHub mirror rather than its own shapefile zip -- 177 simple polygons, one parse either way,
  and this avoids a second shapefile code path for a single extra file.

Politeness: fetched through `pipeline.connectors.http.PoliteSession`, the same browser-like
User-Agent, retry/backoff and `robots.txt` check every connector uses. Checked live 2026-09-18:
`www2.census.gov/robots.txt` has no `Disallow` under `User-agent: *` (only a `Crawl-delay: 30`
for a few named crawlers, which `PoliteSession` does not need to honour to be polite -- it already
rate-limits to 1 rps per host); `raw.githubusercontent.com/robots.txt` answers HTTP 404, which
`PoliteSession.allowed_by_robots` treats as permissive, its own documented convention.

Output, under `data/vendored/regions/` (committed; not excluded by `.gitignore`):
- `us_counties.geojson`: `region_id` = 5-digit GEOID, `name`, `state_code` = "US-" + the state's
  USPS postal abbreviation (looked up from the *state* shapefile's own STATEFP -> STUSPS records,
  not a hard-coded table -- the state file is downloaded anyway and is the authoritative source
  for that mapping), `level` = "county".
- `us_states.geojson`: `region_id` = "US-" + STUSPS, `name`, `level` = "state".
- `countries.geojson`: `region_id` = ISO 3166-1 alpha-2 (`ISO_A2`), falling back to `ISO_A2_EH`
  where `ISO_A2` is the Natural Earth placeholder `"-99"` (five countries: Norway, France,
  N. Cyprus, Somaliland, Kosovo; the first two and Kosovo resolve to a real code via `ISO_A2_EH`,
  N. Cyprus and Somaliland have no code in either field and keep `"-99"` -- a known gap, not a bug,
  recorded here and in the CHANGELOG), `name`, `level` = "country".

Every geometry is normalised to GeoJSON MultiPolygon (Census/Natural Earth mix single- and
multi-ring polygons within the same file) with coordinates rounded to `DEFAULT_COORD_DECIMALS`
places. `us_counties.geojson` is the one file large enough to matter for a committed, tile-adjacent
asset; `main()` reports its size and re-rounds to 3 decimals if it exceeds `SIZE_BUDGET_BYTES`.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import time
import zipfile
from typing import Any

import shapefile  # type: ignore[import-untyped] # pyshp ships no type stubs

from pipeline.connectors.http import PoliteSession

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "vendored" / "regions"

COUNTY_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_county_20m.zip"
STATE_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_state_20m.zip"
COUNTRIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
    "ne_110m_admin_0_countries.geojson"
)

DEFAULT_COORD_DECIMALS = 4
FALLBACK_COORD_DECIMALS = 3
#: Above this, `us_counties.geojson` is re-rounded to `FALLBACK_COORD_DECIMALS` (task budget).
SIZE_BUDGET_BYTES = 4 * 1024 * 1024

NE_PLACEHOLDER_ISO2 = "-99"


def fetch(session: PoliteSession, url: str) -> bytes:
    """GET `url` through the shared polite session; robots.txt and rate limits apply."""
    r = session.get(url)
    content: bytes = r.content
    return content


def read_shapefile_zip(content: bytes) -> Any:
    """A `shapefile.Reader` over an in-memory Census cartographic-boundary zip's shp/dbf/shx."""
    zf = zipfile.ZipFile(io.BytesIO(content))
    members = {name.rsplit(".", 1)[-1].lower(): name for name in zf.namelist()}
    return shapefile.Reader(
        shp=io.BytesIO(zf.read(members["shp"])),
        dbf=io.BytesIO(zf.read(members["dbf"])),
        shx=io.BytesIO(zf.read(members["shx"])),
    )


def _round_coords(coords: Any, ndigits: int) -> Any:
    if isinstance(coords, (float, int)):
        return round(float(coords), ndigits)
    return [_round_coords(c, ndigits) for c in coords]


def to_multipolygon(geo: dict[str, Any], ndigits: int) -> dict[str, Any]:
    """A GeoJSON `Polygon` or `MultiPolygon` -> `MultiPolygon`, coordinates rounded."""
    if geo["type"] == "Polygon":
        rings: list[Any] = [geo["coordinates"]]
    elif geo["type"] == "MultiPolygon":
        rings = geo["coordinates"]
    else:
        raise ValueError(f"unexpected geometry type {geo['type']!r} for a region polygon")
    return {"type": "MultiPolygon", "coordinates": _round_coords(rings, ndigits)}


def state_postal_map(state_reader: Any) -> dict[str, str]:
    """`{STATEFP: STUSPS}` read from the state shapefile's own records (no hard-coded table)."""
    out: dict[str, str] = {}
    for sr in state_reader.shapeRecords():
        rec = sr.record.as_dict()
        out[str(rec["STATEFP"])] = str(rec["STUSPS"])
    return out


def county_features(
    county_reader: Any, fips_to_postal: dict[str, str], ndigits: int = DEFAULT_COORD_DECIMALS
) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for sr in county_reader.shapeRecords():
        rec = sr.record.as_dict()
        postal = fips_to_postal.get(str(rec["STATEFP"]))
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "region_id": str(rec["GEOID"]),
                    "name": str(rec["NAME"]),
                    "state_code": f"US-{postal}" if postal else None,
                    "level": "county",
                },
                "geometry": to_multipolygon(sr.shape.__geo_interface__, ndigits),
            }
        )
    return features


def state_features(state_reader: Any, ndigits: int = DEFAULT_COORD_DECIMALS) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for sr in state_reader.shapeRecords():
        rec = sr.record.as_dict()
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "region_id": f"US-{rec['STUSPS']}",
                    "name": str(rec["NAME"]),
                    "level": "state",
                },
                "geometry": to_multipolygon(sr.shape.__geo_interface__, ndigits),
            }
        )
    return features


def country_region_id(properties: dict[str, Any]) -> str:
    iso2 = str(properties.get("ISO_A2", NE_PLACEHOLDER_ISO2))
    if iso2 != NE_PLACEHOLDER_ISO2:
        return iso2
    return str(properties.get("ISO_A2_EH", NE_PLACEHOLDER_ISO2))


def country_features(
    ne_geojson: dict[str, Any], ndigits: int = DEFAULT_COORD_DECIMALS
) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for f in ne_geojson["features"]:
        props = f["properties"]
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "region_id": country_region_id(props),
                    "name": str(props.get("NAME")),
                    "level": "country",
                },
                "geometry": to_multipolygon(f["geometry"], ndigits),
            }
        )
    return features


def write_geojson(path: pathlib.Path, features: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"))
    path.write_text(body, encoding="utf-8")
    return len(body.encode("utf-8"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=pathlib.Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    t0 = time.monotonic()
    session = PoliteSession()

    county_reader = read_shapefile_zip(fetch(session, COUNTY_URL))
    state_reader = read_shapefile_zip(fetch(session, STATE_URL))
    countries_geojson = json.loads(fetch(session, COUNTRIES_URL))

    fips_to_postal = state_postal_map(state_reader)
    counties = county_features(county_reader, fips_to_postal)
    states = state_features(state_reader)
    countries = country_features(countries_geojson)

    out_dir = args.out_dir
    counties_path = out_dir / "us_counties.geojson"
    county_bytes = write_geojson(counties_path, counties)
    coord_decimals = DEFAULT_COORD_DECIMALS
    if county_bytes > SIZE_BUDGET_BYTES:
        coord_decimals = FALLBACK_COORD_DECIMALS
        counties = county_features(county_reader, fips_to_postal, ndigits=coord_decimals)
        county_bytes = write_geojson(counties_path, counties)

    states_path = out_dir / "us_states.geojson"
    state_bytes = write_geojson(states_path, states)
    countries_path = out_dir / "countries.geojson"
    countries_bytes = write_geojson(countries_path, countries)

    summary = {
        "counties": {"count": len(counties), "bytes": county_bytes, "coord_decimals": coord_decimals},
        "states": {"count": len(states), "bytes": state_bytes},
        "countries": {"count": len(countries), "bytes": countries_bytes},
        "elapsed_s": round(time.monotonic() - t0, 2),
    }
    print(json.dumps(summary))  # noqa: T201 -- CLI summary line, same convention as eia_plants.py


if __name__ == "__main__":
    main()
