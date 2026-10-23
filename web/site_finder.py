"""The site finder (`/site-finder`; docs/25 "Site finder"): corridor bands of land beside high-voltage
lines, near a substation and out of the floodplain, as a first screen for powered land.

One page, read from `GET /v1/site-finder` (the bands, their facts and where each came from) and
`GET /v1/site-finder/geo` (the bands in view, for the map). The page adds wording and formatting only:
every value it prints is the API's, at the reader's tier, so it can never print a record the API
withheld.

The criteria are the URL. The form is a plain GET form, so the page works without scripts; with them
(`web/static/js/site_finder.js`) a change redraws the list and the map in place and rewrites the URL
with `history.replaceState`, keeping only what differs from the defaults. The server reads the same
names back (`read_criteria`), snapping each value to a choice the API accepts, so a hand-edited or old
link still answers: `adjacent_mi=0.8` reads as 1 mile, `min_kv=120` as 115 kV.

A band's details are its own URL too (`band=`): without scripts the page opens with that band's drawer
drawn; with them a click on a row or on the map fetches the same drawer as a fragment
(`/api/site-finder/band`). The list's sort, page and filter changes fetch `/api/site-finder/rows`, the
same template the page renders, so the wording lives in one place. Both fragments are under `/api/`,
which robots.txt disallows: a fragment is not a page.

The map draws the grid around the bands from OpenStreetMap only (`/api/site-finder/grid`, over
`GET /v1/assets/geo` with `source=us.osm.power`): lines of 69 kV and up and those that state no
voltage, coloured by voltage class, and substations from zoom 8.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import QueryParams

from web import formatting
from web.api_client import ApiError
from web.labels import REGION_NAMES
from web.list_sort import SortColumn, column, sort_caption, sort_headers
from web.page import _basemap_attribution, _tile_mode, get_api, templates, unavailable_response
from web.viewmodels import page_credits, source_label, web_relative_url

router = APIRouter()

PATH = "/site-finder"
#: The voltage floors the criteria offer (kV); the API takes any value from 69 up.
KV_CHOICES: tuple[int, ...] = (69, 115, 138, 230, 345, 500)
#: The band half-widths the candidates are stored at (miles; `services/db/models.py::SITE_FINDER_DISTANCES`).
ADJACENT_CHOICES: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0)
SUBSTATION_MIN, SUBSTATION_MAX, SUBSTATION_STEP = 1.0, 10.0, 0.5
PAGE_SIZE = 25
DEFAULT_SORT = "-voltage_kv"
#: The sortable headings, keyed by the template's name for each (`web/list_sort.py`). Distance to a
#: substation sorts nearest first on the first click; the rest largest first.
SORT_COLUMNS: dict[str, SortColumn] = {
    "voltage": column("voltage_kv", "number", "voltage"),
    "length": column("length_mi", "number", "band length"),
    "substation": SortColumn(
        "substation_mi", "asc", "distance to a substation", "farthest first", "nearest first"
    ),
    "retiring": column("retiring_mw", "number", "retiring and retired plant MW within 5 miles"),
}
_SORT_FIELDS = {c.field for c in SORT_COLUMNS.values()}
_STATE = re.compile(r"^(?:US-)?([A-Z]{2})$")
_COUNTY = re.compile(r"^[0-9]{5}$")
_CURSOR = re.compile(r"^[A-Za-z0-9_=-]{1,512}$")
_BAND = re.compile(r"^[A-Za-z0-9_-]{1,64}\.[0-9]{1,6}-[0-9]{1,6}$")
_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}
_API_FAILURES = (ApiError, httpx.HTTPError, KeyError, TypeError, ValueError)
#: A row's floodplain cell, short; the drawer says it in full (`flood.label`).
FLOOD_SHORT = {"outside": "Outside", "partly_in": "Partly in, cut out", "unavailable": "No flood data"}
#: The OpenStreetMap grid the map draws behind the bands (`/api/site-finder/grid`).
GRID_LAYERS = {"lines": "transmission_line", "substations": "substation"}
GRID_SOURCE = "us.osm.power"
ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"
#: Short names for a fact's source line where the register's own name is a sentence (the full
#: credit stays on the credit line).
SHORT_SOURCE_NAMES = {
    "us.osm.power": "OpenStreetMap",
    "us.fema.nfhl": "FEMA National Flood Hazard Layer",
    "us.census.tiger_counties": "Census TIGER/Line counties",
    "us.census.cartographic_boundaries": "Census cartographic boundaries",
    "us.fcc.bdc.fixed_summary": "FCC Broadband Data Collection",
}


# ------------------------------------------------------------------------------------- criteria
@dataclass(frozen=True)
class Criteria:
    min_kv: int = 69
    adjacent_mi: float = 1.0
    max_substation_mi: float = 5.0
    exclude_flood: bool = True
    include_unknown_voltage: bool = False
    exclude_protected: bool = False
    state: str | None = None  # two letters, "TX"
    county: str | None = None  # five-digit FIPS
    sort: str = DEFAULT_SORT

    def api_params(self) -> dict[str, str]:
        """What `GET /v1/site-finder` and its map endpoint are asked, every criterion explicit."""
        params = {
            "min_kv": str(self.min_kv),
            "adjacent_mi": f"{self.adjacent_mi:g}",
            "max_substation_mi": f"{self.max_substation_mi:g}",
            "exclude_flood": _bool(self.exclude_flood),
            "include_unknown_voltage": _bool(self.include_unknown_voltage),
            "exclude_protected": _bool(self.exclude_protected),
        }
        if self.state:
            params["state"] = self.state
        if self.county:
            params["county"] = self.county
        return params

    def url_items(self, *, sort: bool = True) -> list[tuple[str, str]]:
        """The page URL's query: only what differs from the defaults, in a fixed order, so one view
        has one URL (the same rule `site_finder.js` writes with)."""
        default = DEFAULTS
        items: list[tuple[str, str]] = []
        if self.min_kv != default.min_kv:
            items.append(("min_kv", str(self.min_kv)))
        if self.adjacent_mi != default.adjacent_mi:
            items.append(("adjacent_mi", f"{self.adjacent_mi:g}"))
        if self.max_substation_mi != default.max_substation_mi:
            items.append(("max_substation_mi", f"{self.max_substation_mi:g}"))
        for name in ("exclude_flood", "include_unknown_voltage", "exclude_protected"):
            if getattr(self, name) != getattr(default, name):
                items.append((name, _bool(getattr(self, name))))
        if self.state:
            items.append(("state", self.state))
        if self.county:
            items.append(("county", self.county))
        if sort and self.sort != DEFAULT_SORT:
            items.append(("sort", self.sort))
        return items

    def href(self, **extra: str | None) -> str:
        items = self.url_items() + [(k, v) for k, v in extra.items() if v]
        return PATH + ("?" + urlencode(items) if items else "")


DEFAULTS = Criteria()


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _last(qp: QueryParams, name: str) -> str | None:
    """The last value given for `name`: the form sends a hidden `false` before a ticked box's `true`."""
    values = [v for v in qp.getlist(name) if v is not None]
    return values[-1].strip() if values else None


def _flag(qp: QueryParams, name: str, default: bool) -> bool:
    raw = (_last(qp, name) or "").lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def _number(qp: QueryParams, name: str) -> float | None:
    raw = _last(qp, name)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def snap_kv(value: float | None) -> int:
    """The highest offered floor at or under `value` (69 kV under all of them)."""
    if value is None:
        return DEFAULTS.min_kv
    below = [k for k in KV_CHOICES if k <= value]
    return below[-1] if below else KV_CHOICES[0]


def snap_adjacent(value: float | None) -> float:
    """The nearest stored band width; a tie takes the narrower."""
    if value is None:
        return DEFAULTS.adjacent_mi
    return min(ADJACENT_CHOICES, key=lambda d: (abs(d - value), d))


def snap_substation(value: float | None) -> float:
    if value is None:
        return DEFAULTS.max_substation_mi
    clamped = min(SUBSTATION_MAX, max(SUBSTATION_MIN, value))
    return round(clamped / SUBSTATION_STEP) * SUBSTATION_STEP


def read_criteria(qp: QueryParams) -> Criteria:
    """The page's criteria from its URL (or the form), every value snapped to one the API accepts."""
    state_match = _STATE.match((_last(qp, "state") or "").upper())
    county = _last(qp, "county") or ""
    sort = _last(qp, "sort") or ""
    return Criteria(
        min_kv=snap_kv(_number(qp, "min_kv")),
        adjacent_mi=snap_adjacent(_number(qp, "adjacent_mi")),
        max_substation_mi=snap_substation(_number(qp, "max_substation_mi")),
        exclude_flood=_flag(qp, "exclude_flood", DEFAULTS.exclude_flood),
        include_unknown_voltage=_flag(qp, "include_unknown_voltage", DEFAULTS.include_unknown_voltage),
        exclude_protected=_flag(qp, "exclude_protected", DEFAULTS.exclude_protected),
        state=state_match.group(1) if state_match else None,
        county=county if _COUNTY.match(county) else None,
        sort=sort if sort.lstrip("-") in _SORT_FIELDS else DEFAULT_SORT,
    )


def _cursor(qp: QueryParams) -> str | None:
    raw = _last(qp, "cursor") or ""
    return raw if _CURSOR.match(raw) else None


def _band_id(qp: QueryParams) -> str | None:
    raw = _last(qp, "band") or ""
    return raw if _BAND.match(raw) else None


# ------------------------------------------------------------------------------------ formatting
def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def miles(value: Any, digits: int = 1) -> str | None:
    number = _num(value)
    if number is None:
        return None
    if digits == 1 and 0 <= number < 0.05:
        return "under 0.1 mi"
    return f"{number:,.{digits}f} mi"


def kv(value: Any) -> str | None:
    number = _num(value)
    if number is None:
        return None
    return f"{int(number):,} kV" if float(number).is_integer() else f"{number:,.1f} kV"


def mw(value: Any) -> str | None:
    number = _num(value)
    return None if number is None else f"{formatting.mw(number)} MW"


def share(value: Any) -> str | None:
    """A share of the band: `12%`, `under 1%`, `0%`."""
    number = _num(value)
    if number is None:
        return None
    if number <= 0:
        return "0%"
    if number < 0.01:
        return "under 1%"
    return f"{round(number * 100):.0f}%"


def state_name(code: str | None) -> str | None:
    if not code:
        return None
    full = code if code.startswith("US-") else f"US-{code}"
    return REGION_NAMES.get(full) or full.removeprefix("US-")


def place_label(place: Mapping[str, Any] | None) -> str | None:
    place = place or {}
    state = (place.get("state_code") or "").removeprefix("US-") or None
    county = place.get("county_name")
    if county and state:
        return f"{county}, {state}"
    return county or state_name(state)


def source_line(prov: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """One fact's source line: the register's short name, its link and the date. Each register's
    full credit, verbatim, is on the page's credit line under the list (`m.credits_line`)."""
    if not prov:
        return None
    sid = str(prov.get("source_id") or "")
    return {
        "name": source_label(sid)
        or SHORT_SOURCE_NAMES.get(sid)
        or prov.get("source_name")
        or prov.get("attribution_text")
        or sid,
        "url": prov.get("source_url"),
        "retrieved": formatting.display_date(prov.get("retrieved_at")) or None,
        "vintage": prov.get("vintage"),
    }


# ------------------------------------------------------------------------------------ view models
def row_view(band: Mapping[str, Any], c: Criteria) -> dict[str, Any]:
    """One row of the results list."""
    line = band.get("line") or {}
    sub = band.get("substation")
    flood = band.get("flood") or {}
    context = band.get("context") or {}
    stated = None if line.get("voltage_unknown") else kv(line.get("voltage_kv"))
    return {
        "id": band["id"],
        "name": line.get("name") or "Unnamed line",
        "operator": line.get("operator"),
        "voltage_label": stated or "Not stated",
        "voltage_class": line.get("voltage_class") or "unknown",
        "length_label": miles(band.get("length_mi")),
        "substation_name": ((sub or {}).get("name") or "Unnamed substation") if sub else None,
        "substation_label": miles((sub or {}).get("distance_mi")) if sub else None,
        "flood_status": flood.get("status") or "unavailable",
        "flood_label": FLOOD_SHORT.get(str(flood.get("status")), "No flood data"),
        "place_label": place_label(band.get("place")),
        "retiring_label": mw(context.get("retiring_mw")) if context.get("retiring_mw") else None,
        "href": c.href(band=band["id"]),
        "bbox": band.get("bbox"),
    }


def _plant_view(p: Mapping[str, Any]) -> dict[str, Any]:
    status = p.get("status")
    year = p.get("retirement_year")
    when = None
    if status == "retired":
        when = f"retired {year}" if year else "retired"
    elif year:
        when = f"retiring {year}"
    elif status:
        when = str(status).replace("_", " ")
    return {
        "name": p.get("name") or "Unnamed plant",
        "href": web_relative_url(p.get("url")),
        "mw": mw(p.get("capacity_mw")),
        "when": when,
        "owners": ", ".join(str(o) for o in (p.get("owners") or [])[:3]) or None,
        "distance": miles(p.get("distance_mi")),
        "source": source_line(p.get("provenance")),
    }


def _load_view(p: Mapping[str, Any]) -> dict[str, Any]:
    provs = p.get("provenance") or []
    return {
        "name": p.get("name") or "Unnamed request",
        "href": web_relative_url(p.get("url")),
        "mw": mw(p.get("capacity_mw")),
        "state": str(p.get("lifecycle_state") or "").replace("_", " ") or None,
        "distance": miles(p.get("distance_mi")),
        "source": source_line(provs[0]) if provs else None,
    }


def _layer_value(entry: Mapping[str, Any]) -> tuple[str, list[str]]:
    """A prospecting layer's value in words, and its largest features."""
    status = entry.get("status")
    if status == "withheld":
        return "Not shown: this source's licence keeps it off public pages", []
    if status == "unavailable":
        return "Not available here", []
    unit = entry.get("unit") or ""
    summary = entry.get("summary")
    lower = " (at least; part of the band has no data)" if status in ("partial", "no_data") else ""
    if isinstance(summary, Mapping):
        mean, lo, hi = (_num(summary.get(k)) for k in ("mean", "min", "max"))
        if mean is None:
            return "No value over this band", []
        spread = (
            f" (from {lo:.2f} to {hi:.2f})" if lo is not None and hi is not None and hi - lo > 0.005 else ""
        )
        return f"{mean:.2f} {unit}{spread}{lower}", []
    value = share(entry.get("share"))
    words = f"{value} of the band" if value is not None else "No value over this band"
    hard = share(entry.get("hard_constraint_share"))
    if hard is not None and entry.get("layer") == "protected_lands":
        words += f"; {hard} strictly protected (GAP 1-2)"
    qualifying = share(entry.get("qualifying_share"))
    if qualifying is not None and entry.get("layer") == "energy_community_ffe":
        words += f"; {qualifying} in a qualifying area"
    features = []
    for f in entry.get("features") or []:
        fs = share(f.get("share"))
        gap = f" (GAP {f['gap_status']})" if f.get("gap_status") else ""
        features.append(f"{f.get('name')}{gap}{': ' + fs if fs else ''}")
    return words + lower, features


def band_view(band: Mapping[str, Any]) -> dict[str, Any]:
    """The drawer: every fact of one band with its source line, the links, the screening notes."""
    line = band.get("line") or {}
    sub = band.get("substation")
    flood = band.get("flood") or {}
    place = band.get("place") or {}
    context = band.get("context") or {}
    links = band.get("links") or {}
    centre = band.get("centre") or {}
    stated = None if line.get("voltage_unknown") else kv(line.get("voltage_kv"))
    voltages = [kv(v) for v in (line.get("voltages_kv") or []) if kv(v)]

    flood_detail = None
    if flood.get("status") == "unavailable":
        flood_detail = flood.get("reason_label") or "FEMA's flood map was not read here"
    elif flood.get("share") is not None:
        flood_detail = f"{share(flood.get('share'))} of the band is in FEMA's 1% annual-chance floodplain"
    county = place.get("county_name")
    state = state_name(place.get("state_code"))
    grid = context.get("grid_connection")
    fiber = context.get("fiber") or {}
    prospect = band.get("prospect") or {}
    layers = []
    for entry in prospect.get("layers") or []:
        value, features = _layer_value(entry)
        layers.append(
            {
                "title": entry.get("title") or entry.get("layer"),
                "value": value,
                "features": features,
                "not": entry.get("not"),
                "source": source_line(entry.get("provenance")),
            }
        )
    ec = prospect.get("energy_community") or {}
    seen: set[str] = set()
    sources = []
    for prov in band.get("provenance") or []:
        sid = str(prov.get("source_id") or "")
        if sid and sid not in seen:
            seen.add(sid)
            sources.append(source_line(prov))
    return {
        "id": band["id"],
        "name": line.get("name") or "Unnamed line",
        "line_href": web_relative_url(line.get("url")),
        "voltage": stated or "Voltage not stated",
        "voltages": voltages if len(voltages) > 1 else [],
        "voltage_unknown": bool(line.get("voltage_unknown")),
        "voltage_class": line.get("voltage_class") or "unknown",
        "operator": line.get("operator"),
        "underground": bool(line.get("underground")),
        "line_source": source_line(line.get("provenance")),
        "length": miles(band.get("length_mi"), 2) or "A stretch",
        "adjacent": f"{_num(band.get('adjacent_mi')) or 1:g} mi",
        "geometry_note": band.get("geometry_note"),
        "substation": None
        if not sub
        else {
            "name": sub.get("name") or "Unnamed substation",
            "href": web_relative_url(sub.get("url")),
            "voltage": kv(sub.get("voltage_kv")),
            "distance": miles(sub.get("distance_mi"), 2),
            "source": source_line(sub.get("provenance")),
        },
        "flood": {
            "status": flood.get("status") or "unavailable",
            "label": flood.get("label") or "Flood data unavailable",
            "detail": flood_detail,
            "panels": list(flood.get("firm_panels") or [])[:6],
            "reported": [str(r).replace("_", " ") for r in flood.get("reported") or []],
            "not": flood.get("not"),
            "source": source_line(flood.get("provenance")),
        },
        "place": {
            "county": county,
            "state": state,
            "label": ", ".join(x for x in (county, state) if x) or None,
            "others": [n for n in place.get("counties") or [] if n != county],
            "note": place.get("note"),
            "source": source_line(place.get("provenance")),
        },
        "radius": f"{_num(context.get('radius_mi')) or 5:g} mi",
        "plants": [_plant_view(p) for p in context.get("retiring_plants") or []],
        "plants_mw": mw(context.get("retiring_mw")) if context.get("retiring_mw") else None,
        "loads": [_load_view(p) for p in context.get("data_centres") or []],
        "grid": None
        if not grid
        else {
            "name": grid.get("name") or "Unnamed connection point",
            "href": web_relative_url(grid.get("url")),
            "iso": grid.get("iso"),
            "voltage": kv(grid.get("voltage_kv")),
            "queued": mw(grid.get("queued_mw")),
            "count": grid.get("queued_count"),
            "distance": miles(grid.get("nearest_request_mi"), 2),
            "note": grid.get("note"),
            "source": source_line(grid.get("provenance")),
        },
        "fiber": {
            "ok": fiber.get("status") == "ok",
            "share": share(fiber.get("fiber_share")),
            "gigabit": share(fiber.get("fiber_gigabit_share")),
            "reason": fiber.get("reason"),
            "note": fiber.get("note"),
            "href": fiber.get("fcc_area_url"),
            "source": source_line(fiber.get("provenance")),
        },
        "layers": layers,
        "energy_community": {
            "share": share(ec.get("share")),
            "status": ec.get("status"),
            "not": ec.get("not"),
        },
        "links": [
            (label, links.get(key))
            for key, label in (
                ("fema_flood_map", "FEMA flood map at the band's centre"),
                ("google_maps", "Google Maps at the band's centre"),
                ("osm_way", "The line on OpenStreetMap"),
                ("osm_substation", "The substation on OpenStreetMap"),
                (
                    "county",
                    f"{county}, {state} on data.census.gov"
                    if county and state
                    else "The county on data.census.gov",
                ),
            )
            if links.get(key)
        ],
        "centre": f"{centre['lat']:.5f}, {centre['lon']:.5f}" if centre.get("lat") is not None else None,
        "sources": sources,
        # What the map draws for the open band: its polygon, its centreline and its box.
        "map_json": json.dumps(
            {
                "id": band["id"],
                "geometry": band.get("geometry"),
                "centreline": band.get("centreline"),
                "bbox": band.get("bbox"),
            },
            separators=(",", ":"),
        ).replace("</", "<\\/"),
    }


def _summary_view(meta: Mapping[str, Any], c: Criteria) -> dict[str, Any]:
    finder = meta.get("site_finder") or {}
    summary = finder.get("summary") or {}
    run = finder.get("run") or {}
    facets = summary.get("facets") or {}
    states = [
        {
            "code": s["state_code"].removeprefix("US-"),
            "name": state_name(s["state_code"]),
            "miles": s.get("miles"),
        }
        for s in facets.get("states") or []
        if s.get("state_code")
    ]
    if c.state and not any(s["code"] == c.state for s in states):
        states.append({"code": c.state, "name": state_name(c.state), "miles": 0})
    states.sort(key=lambda s: str(s["name"]))
    counties = [
        {
            "fips": row["county_fips"],
            "label": row.get("county_name") or row["county_fips"],
            "state": (row.get("state_code") or "").removeprefix("US-"),
            "miles": row.get("miles"),
        }
        for row in facets.get("counties") or []
        if row.get("county_fips")
    ]
    if c.county and not any(r["fips"] == c.county for r in counties):
        counties.append({"fips": c.county, "label": f"County {c.county}", "state": c.state or "", "miles": 0})
    return {
        "computed": bool(run.get("computed")),
        "computed_at": formatting.display_date(run.get("computed_at")) or None,
        "flood_states": run.get("flood_states") or [],
        "bands": int(summary.get("bands") or 0),
        "miles": _num(summary.get("miles")) or 0.0,
        "flood_unavailable_mi": _num(summary.get("flood_unavailable_mi")),
        "screening": finder.get("screening"),
        "flood_note": finder.get("flood_note"),
        "states": states,
        "counties": counties,
        "count_by_state": len({r["state"] for r in counties}) > 1,
        # Across several states with none chosen the API lists no counties: the picker asks for a state.
        "counties_need_state": bool(facets.get("counties_need_state")) and not c.state,
    }


def _criteria_view(c: Criteria) -> dict[str, Any]:
    return {
        "c": c,
        "kv_choices": KV_CHOICES,
        "adjacent_choices": ADJACENT_CHOICES,
        "adjacent_index": ADJACENT_CHOICES.index(c.adjacent_mi),
        "substation_range": (SUBSTATION_MIN, SUBSTATION_MAX, SUBSTATION_STEP),
        "defaults": DEFAULTS,
    }


def _rows_context(request: Request, c: Criteria, cursor: str | None, selected: str | None) -> dict[str, Any]:
    """Everything the results fragment renders, from one list call."""
    api = get_api(request)
    # The list shows no outline; the drawer asks for its band's (`_band_context`).
    params = {**c.api_params(), "sort": c.sort, "limit": str(PAGE_SIZE), "geometry": "false"}
    if cursor:
        params["cursor"] = cursor
    try:
        envelope = api.get("/v1/site-finder", params=params)
    except ApiError as exc:
        # A cursor from another sort or an older run: start again at the first page.
        if cursor and exc.status_code == 400:
            params.pop("cursor", None)
            envelope = api.get("/v1/site-finder", params=params)
            cursor = None
        else:
            raise
    bands = envelope.get("data") or []
    links_qp = QueryParams(urlencode([*c.url_items(sort=False), ("sort", c.sort)]))
    page = envelope.get("page") or {}
    next_cursor = page.get("next_cursor") if page.get("has_more") else None
    return {
        "criteria": c,
        "summary": _summary_view(envelope.get("meta") or {}, c),
        "rows": [row_view(b, c) for b in bands],
        "selected": selected,
        "sort_headers": sort_headers(PATH, links_qp, SORT_COLUMNS, c.sort),
        "sort_caption": sort_caption(c.sort, SORT_COLUMNS),
        "next_href": c.href(cursor=next_cursor) if next_cursor else None,
        "first_href": c.href() if cursor else None,
        "credits": page_credits(envelope),
        "bounds": _bounds(bands),
    }


def _bounds(bands: Sequence[Mapping[str, Any]]) -> list[float] | None:
    boxes: list[list[float]] = [
        b["bbox"] for b in bands if isinstance(b.get("bbox"), list) and len(b["bbox"]) == 4
    ]
    if not boxes:
        return None
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def _band_context(request: Request, c: Criteria, band_id: str) -> dict[str, Any] | None:
    """One band's drawer under the criteria, or `None` when it no longer matches them."""
    api = get_api(request)
    envelope = api.get("/v1/site-finder", params={**c.api_params(), "band": band_id, "limit": "1"})
    bands = envelope.get("data") or []
    if not bands:
        return None
    view = band_view(bands[0])
    view["credits"] = page_credits(envelope)
    return view


def _config_json(c: Criteria, tile_mode: str, tile_url: str | None) -> str:
    """What site_finder.js needs to start: the defaults it keeps out of the URL and the choices."""
    return json.dumps(
        {
            "path": PATH,
            "defaults": {
                "min_kv": DEFAULTS.min_kv,
                "adjacent_mi": DEFAULTS.adjacent_mi,
                "max_substation_mi": DEFAULTS.max_substation_mi,
                "exclude_flood": DEFAULTS.exclude_flood,
                "include_unknown_voltage": DEFAULTS.include_unknown_voltage,
                "exclude_protected": DEFAULTS.exclude_protected,
                "sort": DEFAULT_SORT,
            },
            "adjacent_choices": list(ADJACENT_CHOICES),
            "criteria": {**c.api_params(), "sort": c.sort},
            "tile_mode": tile_mode,
            "tile_url": tile_url or "",
        },
        separators=(",", ":"),
    ).replace("</", "<\\/")


# ------------------------------------------------------------------------------------------ routes
@router.get(PATH, response_class=HTMLResponse)
def site_finder_page(request: Request) -> HTMLResponse:
    qp = request.query_params
    c = read_criteria(qp)
    band_id = _band_id(qp)
    try:
        rows = _rows_context(request, c, _cursor(qp), band_id)
        band = _band_context(request, c, band_id) if band_id else None
    except _API_FAILURES:
        return unavailable_response(request)
    tile_url = (os.environ.get("MAP_TILE_URL") or "").strip() or None
    tile_mode = _tile_mode(tile_url)
    return templates.TemplateResponse(
        request,
        "site_finder.html",
        {
            **rows,
            **_criteria_view(c),
            "band": band,
            "band_missing": bool(band_id) and band is None,
            "tile_url": tile_url,
            "tile_mode": tile_mode,
            "basemap_attribution": _basemap_attribution(tile_mode, tile_url),
            "config_json": _config_json(c, tile_mode, tile_url),
            "odbl_url": ODBL_URL,
        },
    )


@router.get("/api/site-finder/rows", response_class=HTMLResponse)
def site_finder_rows(request: Request) -> HTMLResponse:
    """The summary, the results list and its pager for the criteria, as the page renders them."""
    qp = request.query_params
    c = read_criteria(qp)
    try:
        rows = _rows_context(request, c, _cursor(qp), _band_id(qp))
    except _API_FAILURES:
        return HTMLResponse(
            '<p class="map-error" role="alert">The site finder is unavailable just now. '
            "Try again in a minute.</p>",
            status_code=503,
        )
    return templates.TemplateResponse(request, "partials/_site_finder_update.html", rows)


@router.get("/api/site-finder/band", response_class=HTMLResponse)
def site_finder_band(request: Request) -> HTMLResponse:
    """One band's drawer, for a click on a row or on the map."""
    qp = request.query_params
    c = read_criteria(qp)
    band_id = _band_id(qp)
    if band_id is None:
        return HTMLResponse("", status_code=400)
    try:
        band = _band_context(request, c, band_id)
    except _API_FAILURES:
        return HTMLResponse(
            '<p class="map-error" role="alert">This band could not be loaded just now.</p>', status_code=503
        )
    return templates.TemplateResponse(
        request,
        "partials/_site_finder_band.html",
        {"band": band, "band_missing": band is None, "criteria": c},
    )


@router.get("/api/site-finder/geo")
def site_finder_geo(request: Request) -> JSONResponse:
    """Same-origin proxy for `GET /v1/site-finder/geo`: the criteria, `bbox` and `zoom` only, no cookies."""
    qp = request.query_params
    c = read_criteria(qp)
    params: dict[str, str] = {**c.api_params()}
    for name in ("bbox", "zoom"):
        if qp.get(name):
            params[name] = qp[name]
    try:
        envelope = get_api(request).get("/v1/site-finder/geo", params=params)
    except ApiError as exc:
        return JSONResponse(exc.body, status_code=exc.status_code)
    return JSONResponse(envelope)


@router.get("/api/site-finder/grid")
def site_finder_grid(request: Request) -> JSONResponse:
    """The OpenStreetMap grid behind the bands (`GET /v1/assets/geo`): `layer=lines` or `substations`,
    at 69 kV and up or with no stated voltage, in `bbox` at `zoom`."""
    qp = request.query_params
    asset_type = GRID_LAYERS.get(qp.get("layer") or "")
    if asset_type is None or not qp.get("bbox") or not qp.get("zoom"):
        return JSONResponse({"title": "layer, bbox and zoom are required"}, status_code=400)
    params = {
        "bbox": qp["bbox"],
        "zoom": qp["zoom"],
        "asset_type": asset_type,
        "source": GRID_SOURCE,
        "voltage_kv[gte]": "69",
        "include_voltage_unknown": "true",
    }
    try:
        envelope = get_api(request).get("/v1/assets/geo", params=params)
    except ApiError as exc:
        return JSONResponse(exc.body, status_code=exc.status_code)
    return JSONResponse(envelope)


__all__ = [
    "ADJACENT_CHOICES",
    "DEFAULTS",
    "KV_CHOICES",
    "Criteria",
    "band_view",
    "read_criteria",
    "router",
    "row_view",
]
