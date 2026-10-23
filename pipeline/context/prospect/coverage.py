"""The area a prospect polygon layer covers, written into its GeoParquet so the sampler can say "no data".

A polygon layer only answers "outside every polygon" truthfully inside the area its source was fetched
for. The development build fetches NETL's energy communities and PAD-US for Texas only, so without this
a corridor running into New Mexico would read 0% protected land there instead of "no data". Each
builder records one of:

- `{"kind": "bbox", "bbox": [w, s, e, n], "label": <region>}`: the build's region box (sources clipped
  to it: EIA basins and plays, the reserves states that touch it, Aqueduct, the CONUS builds);
- `{"kind": "states", "states": [...], "label": ..., "wkb_hex": ...}`: the states a state-filtered
  source was fetched for, with their outline from the vendored Census cartographic boundary file
  (`us.census.cartographic_boundaries`, public domain; 1:20,000,000, so within about a kilometre of a
  state line a point the source does cover can read "no data" -- never the reverse, since a point inside
  any of the layer's own polygons always counts as covered).

`public()` is the same record without the outline, for the manifest and the API.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from pipeline.context.prospect.common import REGION_STATES, REGIONS
from pipeline.context.prospect.geoparquet import polygons_of, wkb_multipolygon

ROOT = pathlib.Path(__file__).resolve().parents[3]
STATES_GEOJSON = ROOT / "data" / "vendored" / "regions" / "us_states.geojson"
#: Where the vendored state outlines came from (pipeline/context/regions.py STATE_URL).
CENSUS_STATE_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_state_20m.zip"
CENSUS_SOURCE_ID = "us.census.cartographic_boundaries"


def region_box(region: str) -> dict[str, Any]:
    return {"kind": "bbox", "bbox": list(REGIONS[region]), "label": region}


def states(region: str, names: tuple[str, ...], states_path: pathlib.Path = STATES_GEOJSON) -> dict[str, Any]:
    doc = json.loads(states_path.read_text())
    found = {f["properties"]["name"]: f for f in doc["features"] if f["properties"].get("name") in names}
    missing = sorted(set(names) - set(found))
    if missing:
        raise LookupError(f"no outline for {missing} in {states_path.name}")
    polys = [p for name in names for p in polygons_of(found[name]["geometry"])]
    return {
        "kind": "states",
        "states": list(names),
        "label": ", ".join(names),
        "region": region,
        "outline_source_id": CENSUS_SOURCE_ID,
        "outline_source_url": CENSUS_STATE_URL,
        "wkb_hex": wkb_multipolygon(polys).hex(),
    }


def for_state_filtered(region: str) -> dict[str, Any]:
    """Coverage of a source fetched per state for this region (NETL, PAD-US): the states when the region
    names them, else the region box (CONUS fetches the national data)."""
    names = REGION_STATES.get(region)
    return states(region, tuple(names)) if names else region_box(region)


def public(coverage: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in coverage.items() if k != "wkb_hex"}
