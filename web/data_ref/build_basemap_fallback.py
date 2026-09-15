"""One-off script that produced `web/static/data/basemap_fallback.geojson`: a lightweight,
public-domain world-countries + US-states outline layer the map page draws underneath the tile
basemap so the map still reads as a map if the tile provider is unreachable (this sandbox's
egress proxy resets Chromium's own TLS handshake to every external host the page references --
`web/README.md` "Playwright smoke path" -- so the map canvas rendered blank behind the data in
every screenshot until this fallback was added).

Sources, both public domain, fetched live (not vendored raw) and trimmed to name + geometry:
  - World country outlines: Natural Earth 1:110m Admin 0 Countries
    (https://github.com/nvkelso/natural-earth-vector, maintained by a Natural Earth Vector team
    member; "Natural Earth is a public domain map dataset" -- no attribution required).
  - US state outlines: US Census Bureau TIGERweb `State_County` MapServer, layer 0 (States),
    generalised server-side (`maxAllowableOffset=0.02` degrees) to keep the file small -- the
    same public-domain federal source as `web/data_ref/us_county_centroids.tsv`.

Re-run to refresh: `python web/data_ref/build_basemap_fallback.py`. Network access required
(these are fetched live, not committed as raw intermediates).
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

WORLD_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_110m_admin_0_countries.geojson"
US_STATES_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/0/query"
    "?where=1%3D1&outFields=NAME&outSR=4326&geometryPrecision=3&maxAllowableOffset=0.02&f=geojson"
)
OUT_PATH = Path(__file__).resolve().parent.parent / "static" / "data" / "basemap_fallback.geojson"


def _fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 -- fixed https urls above
        return dict(json.load(r))


def _round(coords: Any, nd: int) -> Any:
    if isinstance(coords[0], int | float):
        return [round(c, nd) for c in coords]
    return [_round(c, nd) for c in coords]


def main() -> None:
    features: list[dict[str, Any]] = []

    world = _fetch_json(WORLD_URL)
    for f in world["features"]:
        name = f["properties"].get("NAME") or f["properties"].get("ADMIN")
        geometry = f["geometry"]
        geometry["coordinates"] = _round(geometry["coordinates"], 2)
        features.append(
            {"type": "Feature", "properties": {"name": name, "level": "country"}, "geometry": geometry}
        )

    states = _fetch_json(US_STATES_URL)
    for f in states["features"]:
        name = f["properties"].get("NAME")
        geometry = f["geometry"]
        geometry["coordinates"] = _round(geometry["coordinates"], 3)
        features.append(
            {"type": "Feature", "properties": {"name": name, "level": "us_state"}, "geometry": geometry}
        )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"))
    )
    print(f"wrote {len(features)} features to {OUT_PATH} ({OUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
