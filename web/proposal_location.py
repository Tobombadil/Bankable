"""The "Location" line and locator map on a proposal page (designer audit 2026-09-30 D-8).

The proposal page had no map, no coordinates and no placement grade (only a "county centroid"
badge for one grade), and nothing linked it back to the map. This builds what
`partials/_proposal_location.html` prints:

* one line: where (county, state or country), how precisely in words (the ADR 0008 grade, and the
  licence downgrade when that is why), and "Show on map" -- a link to the home map centred on the
  record, carrying the lifecycle choice that keeps the record drawn and, for an exact point, the
  record's id so the map opens its drawer;
* a locator: the point inside the outline of its US state or country, as inline SVG from the
  basemap fallback the map already ships (`web/static/data/basemap_fallback.geojson`), drawn with
  the mini-map helpers asset and company pages use (`web/page.py`). No MapLibre: the proposal page
  is the Core Web Vitals target page (docs/04 D-31) and stays script-free (frontend F13).

Only what the API served is used: `location.geom` is already the licence-safe placement
(`services/api/serialize.py::serialize_location`), so a downgraded record is drawn at its region
centroid, never at a withheld coordinate.
"""

from __future__ import annotations

import functools
import json
import math
from collections.abc import Mapping
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from web.page import MINI_MAP_H, MINI_MAP_PAD, MINI_MAP_W, _svg_xy, _thin, _walk_coords
from web.viewmodels import ACTIVE_PROPOSAL_STATES, ALL_PROPOSAL_LIFECYCLE_STATES, WITHDRAWN_PROPOSAL_STATES

FALLBACK_PATH = Path(__file__).resolve().parent / "static" / "data" / "basemap_fallback.geojson"

#: How precisely the record is placed, in words (ADR 0008 grades; docs/31 §5.7 D-9).
GRADE_WORDS = {
    "exact": "exact point (source coordinate)",
    "county_centroid": "county centroid, not the site",
    "state_centroid": "state centroid, not the site",
    "country_centroid": "country centroid, not the site",
}
LICENCE_WORDS = "shown at {grade} level (source licence)"
LICENCE_GRADE = {"county_centroid": "county", "state_centroid": "state", "country_centroid": "country"}

#: The map zoom a "Show on map" link opens at, by grade: the site, the county, the state.
GRADE_ZOOM = {"exact": 10.0, "county_centroid": 8.0, "state_centroid": 5.5, "country_centroid": 4.5}


@functools.lru_cache(maxsize=1)
def _outlines() -> tuple[tuple[str, str, tuple[tuple[tuple[float, float], ...], ...]], ...]:
    """`(level, name, rings)` for every US state and country in the fallback basemap, US states
    first so a US point lands on its state rather than on the whole country."""
    data = json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))
    out = []
    for feature in data.get("features") or []:
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates") or []
        polygons = [coords] if geometry.get("type") == "Polygon" else coords
        rings = tuple(tuple(_walk_coords(ring)) for polygon in polygons for ring in polygon)
        out.append((str(props.get("level") or ""), str(props.get("name") or ""), rings))
    out.sort(key=lambda o: 0 if o[0] == "us_state" else 1)
    return tuple(out)


def _inside(point: tuple[float, float], ring: tuple[tuple[float, float], ...]) -> bool:
    x, y = point
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _bbox(rings: tuple[tuple[tuple[float, float], ...], ...]) -> tuple[float, float, float, float]:
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _outline_for(
    point: tuple[float, float],
) -> tuple[str, tuple[tuple[tuple[float, float], ...], ...]] | None:
    """The state or country containing the point; else (an offshore site) the nearest by bounding
    box, so the locator still shows the coast it is off."""
    outlines = _outlines()
    for _level, name, rings in outlines:
        if any(_inside(point, ring) for ring in rings):
            return name, rings
    best: tuple[float, str, Any] | None = None
    for _level, name, rings in outlines:
        x0, y0, x1, y1 = _bbox(rings)
        dx = max(x0 - point[0], 0.0, point[0] - x1)
        dy = max(y0 - point[1], 0.0, point[1] - y1)
        distance = dx * dx + dy * dy
        if distance < 4.0 and (best is None or distance < best[0]):
            best = (distance, name, rings)
    return (best[1], best[2]) if best else None


def locator_svg(point: tuple[float, float], *, exact: bool, label: str) -> tuple[str, str] | None:
    """`(svg, area name)`: the point in its state's or country's outline, or `None` when the point
    is nowhere near either. Projection and sizing as `web/page.py::_geometry_svg` (equirectangular,
    aspect-corrected at the mid latitude)."""
    found = _outline_for(point)
    if found is None:
        return None
    area, rings = found
    x0, y0, x1, y1 = _bbox(rings)
    x0, x1 = min(x0, point[0]) - 0.2, max(x1, point[0]) + 0.2
    y0, y1 = min(y0, point[1]) - 0.2, max(y1, point[1]) + 0.2
    kx = math.cos(math.radians((y0 + y1) / 2)) or 1.0
    world_w, world_h = (x1 - x0) * kx, y1 - y0
    scale = min((MINI_MAP_W - 2 * MINI_MAP_PAD) / world_w, (MINI_MAP_H - 2 * MINI_MAP_PAD) / world_h)
    off_x = (MINI_MAP_W - world_w * scale) / 2
    off_y = (MINI_MAP_H - world_h * scale) / 2

    def project(lon: float, lat: float) -> tuple[float, float]:
        return off_x + (lon - x0) * kx * scale, off_y + (y1 - lat) * scale

    # Rings too small to see at this scale (islands a few pixels across) are left out.
    paths = [
        "M" + " L".join(_svg_xy(project(lon, lat)) for lon, lat in _thin(list(ring))) + " Z"
        for ring in rings
        if len(ring) > 2 and (max(p[0] for p in ring) - min(p[0] for p in ring)) * kx * scale > 2
    ]
    px, py = project(*point)
    mark = (
        f'<circle class="mini-map__site" cx="{px:.1f}" cy="{py:.1f}" r="6"/>'
        if exact
        else f'<circle class="mini-map__centroid" cx="{px:.1f}" cy="{py:.1f}" r="9"/>'
    )
    svg = (
        f'<svg viewBox="0 0 {MINI_MAP_W} {MINI_MAP_H}" preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{escape(label)}"><path class="mini-map__outline" d="{" ".join(paths)}"/>{mark}</svg>'
    )
    return svg, area


def _place_words(location: Mapping[str, Any], jurisdiction: str | None) -> str | None:
    state = str(location.get("state_code") or jurisdiction or "")
    state = state.rsplit("-", 1)[-1] if state.startswith("US-") else state
    parts = [p for p in (location.get("county_name"), state or None) if p]
    return ", ".join(str(p) for p in parts) or None


def map_href(entity: Mapping[str, Any], point: tuple[float, float], precision: str) -> str:
    """The home map centred on the record, drawing it: withdrawn and cancelled proposals are off
    the map by default, built and unknown ones are outside the default states, so the link asks
    for what keeps this one on. An exact point also names the record, for the map to open."""
    state = entity.get("lifecycle_state")
    params: list[tuple[str, str]] = []
    if state in WITHDRAWN_PROPOSAL_STATES:
        params.append(("include_withdrawn", "1"))
    elif state not in ACTIVE_PROPOSAL_STATES:
        params.append(("lifecycle_state", ",".join(ALL_PROPOSAL_LIFECYCLE_STATES)))
    params.append(("center", f"{point[0]:.4f},{point[1]:.4f}"))
    params.append(("zoom", f"{GRADE_ZOOM.get(precision, 6.0):.2f}"))
    if precision == "exact" and entity.get("public_id"):
        params.append(("focus", str(entity["public_id"])))
    return "/?" + urlencode(params)


def proposal_location(entity: Mapping[str, Any]) -> dict[str, Any] | None:
    """What the Location section prints, or `None` when the record has no location row at all."""
    location = entity.get("location") or {}
    if not location:
        return None
    precision = str(location.get("precision") or "unknown")
    licence = location.get("precision_reason") == "licence"
    if licence and precision in LICENCE_GRADE:
        grade = LICENCE_WORDS.format(grade=LICENCE_GRADE[precision])
    else:
        grade = GRADE_WORDS.get(precision, "no usable location in the source")
    place = _place_words(location, entity.get("jurisdiction"))
    geom = location.get("geom") or {}
    coords = list(_walk_coords(geom.get("coordinates"))) if geom.get("type") == "Point" else []
    out: dict[str, Any] = {"place": place, "grade": grade, "map_href": None, "svg": None, "caption": None}
    if not coords:
        return out
    point = coords[0]
    out["map_href"] = map_href(entity, point, precision)
    name = str(entity.get("name_canonical") or "this proposal")
    drawn = locator_svg(point, exact=precision == "exact", label=f"Where {name} is")
    if drawn is not None:
        svg, area = drawn
        mark = (
            "the dot is the source's coordinate"
            if precision == "exact"
            else "the ring is the region's centre, not the site"
        )
        out["svg"] = svg
        out["caption"] = (
            f"{name} in {area}: {mark}. Outline: Natural Earth and US Census Bureau, both public domain."
        )
    return out
