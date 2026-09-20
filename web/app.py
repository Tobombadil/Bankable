"""Public site (docs/00-PLAN.md Sprint 2 "wire site to API" item): reads `services/api` -- over
HTTP when `API_BASE_URL` is set, or the API app mounted in-process otherwise -- instead of the
static JSON files `web/build_data.py` used to produce. `build_data.py` itself is retained only for
the vendored basemap fallback asset (`web/data_ref/build_basemap_fallback.py`'s output); no route
below reads its JSON output any more.

Run for real: `python -m web.dev_up` (loads `data/normalized/*` into a SQLite file, starts
`services.api.app` and this app together). Run against an in-process API with no separate process
at all: `uvicorn web.app:app --reload` with `DATABASE_URL` pointing at an already-loaded SQLite
file (or the default in-memory database, empty until something loads it).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import time
from collections.abc import Iterable, Mapping
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import QueryParams

from web.api_client import ApiClient, ApiError, ApiNotFound, build_client
from web.assets import ASSET_VERSION
from web.auth import router as auth_router
from web.regions import Region, regions_with_data
from web.relevance import (
    PARAM as NEARBY_TECHNOLOGY_PARAM,
)
from web.relevance import (
    load_relevance,
    nearby_notice,
    resolve_nearby_filter,
)
from web.viewmodels import (
    ACTIVE_PROPOSAL_STATES,
    ALL_OPPORTUNITY_STATUSES,
    ALL_PROPOSAL_LIFECYCLE_STATES,
    WITHDRAWN_PROPOSAL_STATES,
    WORLD_BBOX,
    flatten_asset,
    flatten_opportunity,
    flatten_org_asset_row,
    flatten_organization,
    flatten_proposal,
    lifecycle_breakdown,
    opportunity_status_param,
    provenance_panel_rows,
    relativize_geo_feature_urls,
    resolve_proposal_lifecycle_param,
)
from web.viewmodels import (
    footer_build as vm_footer_build,
)

ALL_OPPORTUNITY_STATUSES_CSV = ",".join(ALL_OPPORTUNITY_STATUSES)
ALL_PROPOSAL_LIFECYCLE_STATES_CSV = ",".join(ALL_PROPOSAL_LIFECYCLE_STATES)
_PROPOSAL_SOURCE_IDS = {
    "us.iso.ercot.gen_queue",
    "us.iso.caiso.gen_queue",
    "us.iso.nyiso.gen_queue",
    "us.eia.860m",
    "gb.neso.tec_register",
}

# docs/40-launch-runbook.md §2.7 / CLAUDE.md task brief "Basemap": three modes, keyed on the shape
# of `MAP_TILE_URL` alone -- never a second env var -- so there is exactly one source of truth for
# which basemap a deploy runs. `.pmtiles` -> Protomaps PMTiles self-hosted on R2 (owner decision,
# docs/00-PLAN.md 2026-09-14/15); a `{z}` template -> a hosted raster provider (MapTiler/Stadia,
# the runbook's other named option); unset (or neither shape) -> today's dev-only OSM raster.
TileMode = str  # "pmtiles" | "raster" | "dev" -- not a real enum, only used inside this module


def _tile_mode(tile_url: str | None) -> TileMode:
    if not tile_url:
        return "dev"
    if tile_url.endswith(".pmtiles"):
        return "pmtiles"
    if "{z}" in tile_url:
        return "raster"
    return "dev"


def _basemap_attribution(tile_mode: TileMode, tile_url: str | None) -> str:
    """The credit line `home_map.html` renders next to the map (D-13's "attribution rendered").
    Named by provider when the mode is known; the raster case names whatever host `MAP_TILE_URL`
    points at, since this app has no registry of hosted tile providers to look the name up in."""
    if tile_mode == "pmtiles":
        return "Basemap: Protomaps, © OpenStreetMap contributors, ODbL."
    if tile_mode == "raster":
        host = urlsplit(tile_url).netloc if tile_url else ""
        provider = host or "a hosted tile provider"
        return f"Basemap: {provider}, © OpenStreetMap contributors, ODbL."
    return "Basemap © OpenStreetMap contributors, ODbL (dev tile source; not for production use)."


def _region_context(region: Region) -> dict[str, Any]:
    return {"code": region.code, "label": region.label, "bbox": ",".join(str(v) for v in region.bbox)}


# ------------------------------------------------------------------ existing assets (ADR 0008;
# midstream slice, docs/00-PLAN.md 2026-09-19 option (a)). Labels for every `asset.asset_type`
# in services/db/models.py ASSET_TYPES: (singular, plural), lower-case; `_type_label` capitalises
# the first letter only, so "LNG terminal" keeps its acronym.
ASSET_TYPE_LABELS: dict[str, tuple[str, str]] = {
    "power_plant": ("power plant", "power plants"),
    "gas_pipeline": ("gas pipeline", "gas pipelines"),
    "gas_processing_plant": ("gas processing plant", "gas processing plants"),
    "gas_storage": ("gas storage site", "gas storage sites"),
    "lng_terminal": ("LNG terminal", "LNG terminals"),
    "compressor_station": ("compressor station", "compressor stations"),
    "ethanol_plant": ("ethanol plant", "ethanol plants"),
    "biodiesel_plant": ("biodiesel plant", "biodiesel plants"),
    "rng_project": ("RNG project", "RNG projects"),
    "transmission_line": ("transmission line", "transmission lines"),
    "substation": ("substation", "substations"),
    "refinery": ("refinery", "refineries"),
}
#: The "Existing assets" control on the map (home_map.html): `(value, label, live)` -- every type
#: with data behind it is enabled. Ethanol and RNG went live with the second midstream slice
#: (2026-09-19 evening: EIA Atlas ethanol plants, EPA LMOP landfill-gas projects, EPA AgSTAR
#: digesters); the `live` flag stays so a future type (biodiesel, refineries) can be listed as
#: coming rather than silently absent.
HOME_MAP_ASSET_TYPES: list[tuple[str, str, bool]] = [
    ("power_plant", "Power plants", True),
    ("gas_pipeline", "Gas pipelines", True),
    ("gas_processing_plant", "Gas processing", True),
    ("gas_storage", "Gas storage", True),
    ("lng_terminal", "LNG terminals", True),
    ("ethanol_plant", "Ethanol", True),
    ("rng_project", "RNG", True),
]
LINE_ASSET_TYPES = {"gas_pipeline", "transmission_line"}
#: Fuel-side asset types whose promoted rows replace the plant-shaped Technology / Capacity /
#: Commissioned rows on the asset page (`_fuel_fields`).
FUEL_ASSET_TYPES = {"ethanol_plant", "rng_project"}
#: `asset.technology` values the RNG loaders emit (us.epa.lmop `lfg_electricity|rng|lfg_direct_use`,
#: us.epa.agstar `farm_digester`) -> the words the page, drawer and search row show.
RNG_TECHNOLOGY_LABELS: dict[str, str] = {
    "lfg_electricity": "Landfill gas to electricity",
    "lfg_direct_use": "Landfill gas direct use",
    "rng": "Renewable natural gas",
    "farm_digester": "Farm digester",
}
#: AgSTAR livestock head counts in `attributes` (one column per animal type); a non-zero count
#: is the digester's feedstock when the source carries no feedstock text.
AGSTAR_HERD_KEYS = ("dairy", "swine", "cattle", "poultry")
#: Words for the capacity units the fuel loaders emit; anything else renders as the source wrote it.
CAPACITY_UNIT_LABELS = {"mmgal/yr": "MMgal/yr", "mmscfd": "MMscf/d", "cu-ft/day": "cu ft/day"}
#: `attributes` keys the asset page promotes to a named field (operator, class, diameter, states,
#: length); the generic Attributes table omits them so a value is never shown twice.
PROMOTED_ATTRIBUTE_KEYS = (
    "operator",
    "line_class",
    "interstate",
    "pipeline_type",
    "type_of_pipeline",
    "system_type",
    "diameter_in",
    "diameter_inches",
    "diameter",
    "diameter_mix",
    "states",
    "states_crossed",
    "state_codes",
    "length_miles",
    "miles",
)
#: Company-page map: neither `/v1/organizations/{id}/assets` nor `/v1/assets` embeds geometry
#: (`include_geometry=False`, API lane 2026-09-19), so the page reads it from each asset's own
#: detail response, capped -- the map shows the first N assets, the caption says how many.
ORG_MAP_DETAIL_CAP = 40
ORG_NEARBY_LIMIT = 50
ORG_ROLE_LABELS = {"operator": "Operates", "owner": "Owns"}
ORG_ROLE_ORDER = ("operator", "owner", "other")


def _type_label(asset_type: str | None, *, plural: bool = False) -> str:
    key = asset_type or "power_plant"
    pair = ASSET_TYPE_LABELS.get(key)
    label = (pair[1] if plural else pair[0]) if pair else key.replace("_", " ")
    return label[:1].upper() + label[1:]


def _sentence_label(asset_type: str | None, *, plural: bool) -> str:
    """Mid-sentence form ("Operates 3 gas pipelines"): lower-case unless the label starts with
    an acronym (LNG, RNG)."""
    label = _type_label(asset_type, plural=plural)
    return label if label[:3].isupper() else label.lower()


def _attr(entity: Mapping[str, Any], *keys: str) -> Any:
    """A midstream field may sit at the top level of the record or inside its `attributes` bag
    (data lane, 2026-09-19); the first present value wins, absent is `None` and renders nothing."""
    attributes = entity.get("attributes")
    bag: Mapping[str, Any] = attributes if isinstance(attributes, Mapping) else {}
    for key in keys:
        for source in (entity, bag):
            value = source.get(key)
            if value is not None and value != "":
                return value
    return None


def _line_class(entity: Mapping[str, Any]) -> str | None:
    raw = _attr(entity, "line_class", "interstate", "pipeline_type", "type_of_pipeline", "system_type")
    if raw is True:
        return "interstate"
    if raw is False:
        return "intrastate"
    if raw is None:
        return None
    text = str(raw).lower()
    if "intra" in text:
        return "intrastate"
    if "inter" in text:
        return "interstate"
    if "gather" in text:
        return "gathering"
    return None


def _states_crossed(entity: Mapping[str, Any]) -> str | None:
    raw = _attr(entity, "states", "states_crossed", "state_codes")
    if raw is None:
        return None
    items = raw if isinstance(raw, list | tuple) else str(raw).replace(";", ",").split(",")
    cleaned = [str(s).strip() for s in items if str(s).strip()]
    return ", ".join(cleaned) if cleaned else None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _count_or(value: Any, fallback: int) -> int:
    """A non-negative integer from an API `totals` field, else `fallback`. Guards the company
    page's "N of M" line against a transport that sends no totals at all."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return fallback
    return count if count >= 0 else fallback


def _diameter_text(entity: Mapping[str, Any]) -> str | None:
    raw = _attr(entity, "diameter_in", "diameter_inches", "diameter", "diameter_mix")
    if raw is None:
        return None
    number = _number(raw)
    if number is not None:
        return f"{number:,.1f} in".replace(".0 in", " in")
    return str(raw)


def _quantity(value: Any, unit: str | None, *, digits: int = 1) -> str | None:
    """`55 MMgal/yr`, `1.725 MMscf/d`, `1,814,400 cu ft/day`: thousands separators, at most
    `digits` decimals, a trailing `.0` dropped -- the page's number style, the source's unit word."""
    number = _number(value)
    if number is None:
        return None
    text = f"{number:,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    label = CAPACITY_UNIT_LABELS.get(str(unit or "").lower(), unit)
    return f"{text} {label}" if label else text


def _year(value: Any) -> str | None:
    number = _number(value)
    return str(int(number)) if number is not None and number > 0 else None


def _padd(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    return text if text.upper().startswith("PADD") else f"PADD {text}"


_LMOP_PROJECT_NAME = re.compile(r"^Project\s+#\d+\s+-\s+(?P<landfill>.+)$")


def _host_landfill(entity: Mapping[str, Any]) -> str | None:
    """The landfill an LMOP project sits on: the source's own column when the record carries it,
    else read back out of the loader's `Project #N - <Landfill name>` naming (services/ingest,
    us.epa.lmop) -- a presentation rule over a name the data lane composed, not new data."""
    named = _attr(entity, "landfill_name", "host_landfill")
    if named:
        return str(named)
    match = _LMOP_PROJECT_NAME.match(str(entity.get("name") or ""))
    return match.group("landfill").strip() if match else None


def _feedstock(entity: Mapping[str, Any]) -> str | None:
    named = _attr(entity, "feedstock", "animal_farm_types", "feedstock_raw")
    if named:
        return str(named)
    herds: list[str] = []
    for key in AGSTAR_HERD_KEYS:
        head = _number(_attr(entity, key))
        if head and head > 0:
            herds.append(f"{key.capitalize()} ({head:,.0f} head)")
    return "; ".join(herds) if herds else None


def _fuel_fields(entity: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """The promoted rows for an ethanol plant or RNG project (task brief, second midstream slice):
    `(rows, consumed_attribute_keys)`. A row exists only where the record carries the value (the
    "None" gate); the consumed keys are dropped from the generic Attributes table so nothing shows
    twice. Ethanol: nameplate capacity with its unit, feedstock, PADD, the capacity's as-of year.
    RNG: project type, technology family, rated MW and/or LFG flow (or a digester's biogas
    estimate), biogas end use, host landfill or digester type, feedstock, start and shutdown year.
    Text attributes the API does not serialise yet (PADD, data period, end use, landfill name) are
    read by key so they render the day the API sends them, and are simply absent until then."""
    asset_type = entity.get("asset_type")
    unit = str(entity.get("capacity_unit") or "").lower()
    rows: list[dict[str, Any]] = []
    consumed: list[str] = []

    def add(label: str, value: str | None, *keys: str, tnum: bool = False) -> None:
        consumed.extend(keys)
        if value:
            rows.append({"label": label, "value": value, "tnum": tnum})

    if asset_type == "ethanol_plant":
        nameplate = _attr(entity, "nameplate_capacity_mmgal_yr")
        if nameplate is None and unit == "mmgal/yr":
            nameplate = entity.get("capacity_value")
        add("Nameplate capacity", _quantity(nameplate, "MMgal/yr"), "nameplate_capacity_mmgal_yr", tnum=True)
        add("Feedstock", _feedstock(entity), "feedstock", "feedstock_raw")
        add("PADD", _padd(_attr(entity, "padd")), "padd")
        as_of = _year(_attr(entity, "as_of_year")) or (
            str(_attr(entity, "data_period")) if _attr(entity, "data_period") else None
        )
        add("Capacity as of", as_of, "as_of_year", "data_period", tnum=True)
        return rows, consumed

    if asset_type == "rng_project":
        technology = str(entity.get("technology") or "")
        digester = technology == "farm_digester"
        project_type = _attr(entity, "lfg_energy_project_type", "project_type") or (
            None if digester else entity.get("technology_raw")
        )
        add(
            "Project type",
            str(project_type) if project_type else None,
            "lfg_energy_project_type",
            "project_type",
        )
        add("Technology", RNG_TECHNOLOGY_LABELS.get(technology) or (technology.replace("_", " ") or None))
        add(
            "Rated capacity", _quantity(_attr(entity, "rated_mw", "capacity_mw"), "MW"), "rated_mw", tnum=True
        )
        lfg_flow = _attr(entity, "lfg_flow_to_project_mmscfd")
        if lfg_flow is None and unit == "mmscfd":
            lfg_flow = entity.get("capacity_value")
        add(
            "LFG flow to project",
            _quantity(lfg_flow, "MMscf/d", digits=3),
            "lfg_flow_to_project_mmscfd",
            tnum=True,
        )
        biogas = _attr(entity, "biogas_generation_estimate_cuft_day")
        if biogas is None and unit == "cu-ft/day":
            biogas = entity.get("capacity_value")
        add(
            "Biogas generation (est.)",
            _quantity(biogas, "cu ft/day", digits=0),
            "biogas_generation_estimate_cuft_day",
            tnum=True,
        )
        end_use = _attr(entity, "biogas_end_uses", "lfg_use_details", "project_type_category")
        add(
            "Biogas end use",
            str(end_use) if end_use else None,
            "biogas_end_uses",
            "lfg_use_details",
            "project_type_category",
        )
        if digester:
            digester_type = _attr(entity, "digester_type") or entity.get("technology_raw")
            add("Digester type", str(digester_type) if digester_type else None, "digester_type")
        else:
            add("Host landfill", _host_landfill(entity), "landfill_name", "host_landfill")
        add("Feedstock", _feedstock(entity), "feedstock", "animal_farm_types", *AGSTAR_HERD_KEYS)
        start = _year(_attr(entity, "project_start_year", "year_operational", "commissioned_year"))
        add("Start year", start, "project_start_year", "year_operational", tnum=True)
        add("Shutdown year", _year(_attr(entity, "year_shutdown")), "year_shutdown", tnum=True)
        return rows, consumed

    return rows, consumed


def _geometry_of(obj: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """GeoJSON geometry from whichever key the API puts it under (`geometry` on asset detail per
    the API lane's contract; `geom` and `location.geom` are the older proposal spellings)."""
    if not obj:
        return None
    for key in ("geometry", "geom"):
        value = obj.get(key)
        if isinstance(value, Mapping) and value.get("type") and value.get("coordinates") is not None:
            return dict(value)
    location = obj.get("location")
    if isinstance(location, Mapping):
        return _geometry_of(location)
    return None


def _normalise_owners(entity: Mapping[str, Any]) -> list[dict[str, Any]]:
    """`owners[]` as `serialize_asset_owner` actually emits it embeds the organisation under
    `organization` (public_id, slug, name_canonical); `flatten_asset` reads the flat spelling
    docs/23's table row describes. Accept both, so the owners table and the operator link never
    render a blank organisation for a real row."""
    out: list[dict[str, Any]] = []
    for raw in entity.get("owners") or []:
        org_raw = raw.get("organization")
        org: Mapping[str, Any] = org_raw if isinstance(org_raw, Mapping) else {}
        source_raw = raw.get("provenance")
        source: Mapping[str, Any] = source_raw if isinstance(source_raw, Mapping) else {}
        out.append(
            {
                "public_id": (
                    org.get("public_id") or raw.get("public_id") or raw.get("organization_public_id")
                ),
                "slug": org.get("slug") or raw.get("slug"),
                "name": (
                    org.get("name_canonical")
                    or org.get("name")
                    or raw.get("name")
                    or raw.get("name_canonical")
                ),
                "role": raw.get("role"),
                "share_pct": raw.get("share_pct"),
                "as_of": raw.get("as_of"),
                "source_name": source.get("source_name") or raw.get("source_name") or raw.get("source"),
            }
        )
    return out


def _asset_extras(entity: Mapping[str, Any]) -> dict[str, Any]:
    """The midstream fields `asset_detail.html` renders beyond `flatten_asset`'s plant shape.
    Every value may be absent; the template drops the row rather than printing "None"."""
    owners = _normalise_owners(entity)
    operator_edge = next((o for o in owners if o.get("role") == "operator" and o.get("name")), None)
    operator_name = entity.get("operator_name") or _attr(entity, "operator")
    if operator_edge is None and operator_name:
        wanted = str(operator_name).strip().lower()
        operator_edge = next((o for o in owners if (o.get("name") or "").strip().lower() == wanted), None)
    operator = {
        "name": (operator_edge or {}).get("name") or operator_name,
        "public_id": (operator_edge or {}).get("public_id"),
        "slug": (operator_edge or {}).get("slug"),
    }
    asset_type = entity.get("asset_type")
    geometry = _geometry_of(entity)
    line_geometry = bool(geometry and str(geometry.get("type", "")).endswith("LineString"))
    is_line = asset_type in LINE_ASSET_TYPES or line_geometry
    attributes = entity.get("attributes")
    bag: Mapping[str, Any] = attributes if isinstance(attributes, Mapping) else {}
    promoted = [
        k for k in PROMOTED_ATTRIBUTE_KEYS if k in bag and (is_line or k in ("length_miles", "operator"))
    ]
    fuel_rows, fuel_keys = _fuel_fields(entity)
    promoted += [k for k in fuel_keys if k in bag and k not in promoted]
    technology = str(entity.get("technology") or "")
    return {
        "promoted_attributes": promoted,
        "type_label": _type_label(asset_type),
        # RNG: the family word ("Landfill gas to electricity") wherever a one-liner names the
        # technology (header badge, search row, mini-map subtitle); other types keep the class.
        "technology_label": RNG_TECHNOLOGY_LABELS.get(technology) if asset_type == "rng_project" else None,
        "fuel_fields": fuel_rows,
        "is_fuel": asset_type in FUEL_ASSET_TYPES,
        "is_line": is_line,
        "line_class": _line_class(entity) if is_line else None,
        "length_miles": _number(_attr(entity, "length_miles", "miles")),
        "diameter": _diameter_text(entity) if is_line else None,
        "states": _states_crossed(entity),
        "operator": operator,
        "owners": owners,
        "geometry": geometry,
    }


# ---- static mini-map (asset and company pages) -------------------------------------------------
MINI_MAP_W = 640
MINI_MAP_H = 320
MINI_MAP_PAD = 18
MINI_MAP_MAX_VERTICES = 400


def _walk_coords(coords: Any) -> Iterable[tuple[float, float]]:
    if isinstance(coords, list | tuple) and coords and isinstance(coords[0], int | float):
        yield float(coords[0]), float(coords[1])
        return
    if isinstance(coords, list | tuple):
        for part in coords:
            yield from _walk_coords(part)


def _line_parts(geometry: Mapping[str, Any]) -> list[list[tuple[float, float]]]:
    coords = geometry.get("coordinates") or []
    if geometry.get("type") == "LineString":
        return [list(_walk_coords(coords))]
    if geometry.get("type") == "MultiLineString":
        return [list(_walk_coords(part)) for part in coords]
    return []


def _thin(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) <= MINI_MAP_MAX_VERTICES:
        return points
    step = math.ceil(len(points) / MINI_MAP_MAX_VERTICES)
    kept = points[::step]
    if kept[-1] != points[-1]:
        kept.append(points[-1])
    return kept


def _svg_xy(point: tuple[float, float]) -> str:
    return f"{point[0]:.1f} {point[1]:.1f}"


def _geometry_svg(features: list[dict[str, Any]], label: str) -> str:
    """An inline SVG of the features' geometry (equirectangular, aspect-corrected at the mid
    latitude), the no-JS rendering of the map on asset and company pages. Lines are pipelines
    (dashed when intrastate), circles are point assets, small circles nearby proposals; every
    shape carries a `<title>` so the figure is readable by assistive technology too."""
    points = [pt for f in features for pt in _walk_coords((f.get("geometry") or {}).get("coordinates"))]
    min_lon, max_lon = min(p[0] for p in points), max(p[0] for p in points)
    min_lat, max_lat = min(p[1] for p in points), max(p[1] for p in points)
    if max_lon - min_lon < 0.3:
        min_lon, max_lon = min_lon - 0.15, max_lon + 0.15
    if max_lat - min_lat < 0.3:
        min_lat, max_lat = min_lat - 0.15, max_lat + 0.15
    kx = math.cos(math.radians((min_lat + max_lat) / 2)) or 1.0
    world_w = (max_lon - min_lon) * kx
    world_h = max_lat - min_lat
    scale = min((MINI_MAP_W - 2 * MINI_MAP_PAD) / world_w, (MINI_MAP_H - 2 * MINI_MAP_PAD) / world_h)
    off_x = (MINI_MAP_W - world_w * scale) / 2
    off_y = (MINI_MAP_H - world_h * scale) / 2

    def project(lon: float, lat: float) -> tuple[float, float]:
        return off_x + (lon - min_lon) * kx * scale, off_y + (max_lat - lat) * scale

    parts: list[str] = []
    for f in features:
        props = f.get("properties") or {}
        geometry = f.get("geometry") or {}
        title = escape(str(props.get("name") or ""))
        if props.get("subtitle"):
            title += " — " + escape(str(props["subtitle"]))
        line_parts = _line_parts(geometry)
        if line_parts:
            cls = "mini-map__line"
            if props.get("line_class") == "intrastate":
                cls += " mini-map__line--intrastate"
            d = " ".join(
                "M" + " L".join(_svg_xy(project(lon, lat)) for lon, lat in _thin(part))
                for part in line_parts
                if part
            )
            parts.append(f'<path class="{cls}" d="{d}"><title>{title}</title></path>')
        elif geometry.get("type") == "Point":
            lon, lat = next(iter(_walk_coords(geometry.get("coordinates"))))
            x, y = project(lon, lat)
            cls, r = (
                ("mini-map__proposal", 3.5) if props.get("kind") == "proposal" else ("mini-map__point", 5)
            )
            parts.append(
                f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{r}"><title>{title}</title></circle>'
            )
    return (
        f'<svg viewBox="0 0 {MINI_MAP_W} {MINI_MAP_H}" preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{escape(label)}">' + "".join(parts) + "</svg>"
    )


def _mini_map(features: list[dict[str, Any]], *, label: str, caption: str) -> dict[str, Any] | None:
    """`None` when nothing has geometry (the templates then render no map at all)."""
    drawable = [f for f in features if _geometry_of(f) is not None]
    if not drawable:
        return None
    collection = {"type": "FeatureCollection", "features": drawable}
    # Inside a `<script type="application/json">`, `</` is the only sequence that can end the
    # element early; JSON never needs it unescaped.
    geojson = json.dumps(collection, separators=(",", ":")).replace("</", "<\\/")
    return {"svg": _geometry_svg(drawable, label), "geojson": geojson, "caption": caption}


def _asset_feature(record: Mapping[str, Any], geometry: Mapping[str, Any]) -> dict[str, Any]:
    subtitle_bits = [record.get("type_label") or _type_label(record.get("asset_type"))]
    if record.get("technology_label"):
        subtitle_bits.append(str(record["technology_label"]))
    operator = record.get("operator") or {}
    if isinstance(operator, Mapping) and operator.get("name"):
        subtitle_bits.append(str(operator["name"]))
    elif record.get("operator_name"):
        subtitle_bits.append(str(record["operator_name"]))
    return {
        "type": "Feature",
        "geometry": dict(geometry),
        "properties": {
            "kind": "asset",
            "name": record.get("name") or "",
            "subtitle": " · ".join(subtitle_bits),
            "asset_type": record.get("asset_type") or "power_plant",
            "line_class": record.get("line_class") or None,
            "plant_family": _plant_family(record.get("technology")),
            "url": f"/assets/{record['slug']}" if record.get("slug") else None,
        },
    }


def _proposal_feature(record: Mapping[str, Any], geometry: Mapping[str, Any]) -> dict[str, Any]:
    bits = [b for b in (record.get("technology"), record.get("state") or record.get("jurisdiction")) if b]
    if record.get("capacity_mw"):
        bits.append(f"{float(record['capacity_mw']):.1f} MW")
    if record.get("distance_km") is not None:
        bits.append(f"{float(record['distance_km']):.1f} km away")
    return {
        "type": "Feature",
        "geometry": dict(geometry),
        "properties": {
            "kind": "proposal",
            "name": record.get("name") or "",
            "subtitle": " · ".join(str(b) for b in bits),
            "family": record.get("lifecycle_family") or "neutral",
            "url": f"/proposals/{record['slug']}" if record.get("slug") else None,
        },
    }


def group_nearby_proposals(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse EIA-860M's one-row-per-generator-unit into one nearby row per project: rows that
    share `(name, sponsor, county)` become a single entry carrying `unit_count`, the summed
    `capacity_mw` (None when no member has one) and the nearest member's `distance_km` and
    nearest-asset fields; every other field is the nearest member's. Order is preserved (the API
    returns nearest first), so the group sits where its nearest unit sat. Shared by the asset
    page, the company page and (in `map.js`, the same key) the map's in-view list."""
    groups: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
    order: list[tuple[Any, Any, Any]] = []
    for raw in rows:
        row = dict(raw)
        key = (row.get("name"), row.get("sponsor"), row.get("county"))
        capacity = _number(row.get("capacity_mw"))
        distance = _number(row.get("distance_km"))
        group = groups.get(key)
        if group is None:
            row["unit_count"] = 1
            row["capacity_mw"] = capacity
            row["distance_km"] = distance
            groups[key] = row
            order.append(key)
            continue
        group["unit_count"] += 1
        if capacity is not None:
            group["capacity_mw"] = (group["capacity_mw"] or 0.0) + capacity
        if distance is not None and (group["distance_km"] is None or distance < group["distance_km"]):
            group["distance_km"] = distance
            for field in ("nearest_asset_name", "nearest_asset_slug", "slug", "public_id"):
                if row.get(field) is not None:
                    group[field] = row[field]
    return [groups[key] for key in order]


#: What an organisation typed `other` is called from the assets it holds (task brief: "OTHER" under
#: the name is meaningless for an ethanol producer). Keyed by `asset_type`; the two largest
#: holdings make a two-part descriptor ("Ethanol producer and RNG developer").
ORG_DESCRIPTORS: dict[str, str] = {
    "gas_pipeline": "Gas pipeline operator",
    "gas_processing_plant": "Gas processing operator",
    "gas_storage": "Gas storage operator",
    "lng_terminal": "LNG terminal operator",
    "compressor_station": "Gas pipeline operator",
    "ethanol_plant": "Ethanol producer",
    "biodiesel_plant": "Biodiesel producer",
    "rng_project": "RNG developer",
    "power_plant": "Power plant owner",
    "transmission_line": "Transmission owner",
    "substation": "Transmission owner",
    "refinery": "Refiner",
}


def org_descriptor(org_type: str | None, type_counts: Mapping[str, Any]) -> str | None:
    """`None` unless the organisation's `type` is `other` (or unset) and it holds assets; else the
    descriptor of what it holds most of, joined with the runner-up when there is one. The raw
    type stays in the page's fields table -- this only replaces the header's badge."""
    if org_type not in (None, "", "other"):
        return None
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    order = list(ORG_DESCRIPTORS)
    for asset_type, count in type_counts.items():
        label = ORG_DESCRIPTORS.get(str(asset_type))
        n = int(count) if isinstance(count, int | float) else 0
        if not label or n <= 0 or label in seen:
            continue
        seen.add(label)
        ranked.append((-n, order.index(str(asset_type)), label))
    if not ranked:
        return None
    labels = [label for _, _, label in sorted(ranked)][:2]
    return " and ".join(labels)


def _org_type_counts(asset_counts: Any, groups: list[dict[str, Any]]) -> dict[str, int]:
    """`{asset_type: n}` from the API's `asset_counts.by_type` (any role), else over the page's
    rows -- the same fallback `_org_summary_parts` uses."""
    if isinstance(asset_counts, Mapping):
        by_type = asset_counts.get("by_type")
        if isinstance(by_type, Mapping):
            return {str(k): int(v) for k, v in by_type.items() if isinstance(v, int | float)}
        by_role_and_type = asset_counts.get("by_role_and_type")
        if isinstance(by_role_and_type, Mapping):
            totals: dict[str, int] = {}
            for per_type in by_role_and_type.values():
                if isinstance(per_type, Mapping):
                    for k, v in per_type.items():
                        if isinstance(v, int | float):
                            totals[str(k)] = totals.get(str(k), 0) + int(v)
            return totals
    totals = {}
    for g in groups:
        totals[g["asset_type"]] = totals.get(g["asset_type"], 0) + int(g["count"])
    return totals


_PLANT_FAMILY_PREFIXES = (
    ("solar", "solar"),
    ("wind", "wind"),
    ("gas", "gas"),
    ("fuel_cell", "gas"),
    ("hydrogen", "gas"),
    ("oil", "oil"),
    ("coal", "coal"),
    ("nuclear", "nuclear"),
    ("pumped", "storage"),
    ("hydro", "hydro"),
    ("storage", "storage"),
    ("battery", "storage"),
    ("biomass", "biomass"),
    ("waste", "biomass"),
    ("geothermal", "geothermal"),
)


def _plant_family(technology: str | None) -> str:
    """The legend family a plant technology class lands in -- the same grouping map.js's
    `PLANT_FAMILY_CLASSES` draws, reduced to a prefix match so the mini-map needs no second copy
    of that table."""
    tech = (technology or "").lower()
    for prefix, family in _PLANT_FAMILY_PREFIXES:
        if tech.startswith(prefix):
            return family
    return "other"


# ---- company page: assets by role and type ------------------------------------------------------
def _org_asset_row(row: Mapping[str, Any]) -> dict[str, Any]:
    asset_raw = row.get("asset")
    asset: Mapping[str, Any] = asset_raw if isinstance(asset_raw, Mapping) else row
    out = flatten_org_asset_row(row)
    out.update(
        {
            "operator_name": asset.get("operator_name"),
            "technology": asset.get("technology"),
            "length_miles": _number(_attr(asset, "length_miles", "miles")),
            "states": _states_crossed(asset),
            "state": asset.get("state_code"),
            "line_class": _line_class(asset) if asset.get("asset_type") in LINE_ASSET_TYPES else None,
            "geometry": _geometry_of(asset),
            "provenance": list(asset.get("provenance") or row.get("provenance") or []),
            "held_by": _held_by(row),
        }
    )
    return out


def _held_by(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The subsidiary that actually holds the edge when the page shows a parent's group
    (`held_by` on `/v1/organizations/{id}/assets?include_subsidiaries=true`)."""
    raw = row.get("held_by")
    if not isinstance(raw, Mapping) or not raw.get("public_id"):
        return None
    return {
        "public_id": raw.get("public_id"),
        "slug": raw.get("slug"),
        "name": raw.get("name_canonical") or raw.get("name"),
    }


def _role_key(role: Any) -> str:
    return role if role in ORG_ROLE_LABELS else "other"


def _role_label(role_key: str) -> str:
    return ORG_ROLE_LABELS.get(role_key, "Owns or operates")


def _org_asset_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows grouped by (role, asset type): operator groups first (the discovery asset for a
    pipeline company is "the pipelines they operate"), then owner, then unstated; types in the
    `ASSET_TYPE_LABELS` order. Column flags say which of the optional columns any row fills, so
    a pipeline group shows length and states, a plant group capacity and share."""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (_role_key(row.get("role")), row.get("asset_type") or "power_plant")
        buckets.setdefault(key, []).append(row)
    type_order = list(ASSET_TYPE_LABELS)

    def sort_key(item: tuple[str, str]) -> tuple[int, int]:
        role, asset_type = item
        type_index = type_order.index(asset_type) if asset_type in type_order else len(type_order)
        return ORG_ROLE_ORDER.index(role), type_index

    groups: list[dict[str, Any]] = []
    for role, asset_type in sorted(buckets, key=sort_key):
        members = buckets[(role, asset_type)]
        groups.append(
            {
                "role": role,
                "role_label": _role_label(role),
                "asset_type": asset_type,
                "type_label": _sentence_label(asset_type, plural=True),
                "type_label_singular": _sentence_label(asset_type, plural=False),
                "count": len(members),
                "rows": members,
                "show_capacity": any(m.get("capacity_mw") is not None for m in members),
                "show_length": any(m.get("length_miles") is not None for m in members),
                "show_states": any(m.get("states") or m.get("state") for m in members),
                "show_share": any(m.get("share_pct") is not None for m in members),
                "show_held_by": any(m.get("held_by") for m in members),
            }
        )
    return groups


def _org_summary_parts(asset_counts: Any, groups: list[dict[str, Any]]) -> list[str]:
    """ "Operates 3 gas pipelines · Owns 12 power plants". From the API's `asset_counts` when it
    carries one -- either `{role: {asset_type: n}}` or a flat `{asset_type: n}` -- since the
    page's rows are capped at 100; else counted over the groups on the page."""
    parts: list[tuple[int, int, str]] = []
    type_order = list(ASSET_TYPE_LABELS)
    if isinstance(asset_counts, Mapping):
        # `organization_asset_totals` (services/api/assets.py): `{assets, by_role, by_type,
        # by_role_and_type}` -- the role x type table is what the sentence needs; a flat
        # `by_type` (or a bare `{asset_type: n}`) gives the role-less form.
        if isinstance(asset_counts.get("by_role_and_type"), Mapping):
            asset_counts = asset_counts["by_role_and_type"]
        elif isinstance(asset_counts.get("by_type"), Mapping):
            asset_counts = asset_counts["by_type"]

    def part(role_key: str, asset_type: str, count: int) -> tuple[int, int, str]:
        count = int(count)
        label = _sentence_label(asset_type, plural=count != 1)
        type_index = type_order.index(asset_type) if asset_type in type_order else len(type_order)
        return ORG_ROLE_ORDER.index(role_key), type_index, f"{_role_label(role_key)} {count} {label}"

    if isinstance(asset_counts, Mapping) and asset_counts:
        for key, value in asset_counts.items():
            if isinstance(value, Mapping):
                for asset_type, count in value.items():
                    if isinstance(count, int | float) and count > 0:
                        parts.append(part(_role_key(key), str(asset_type), int(count)))
            elif isinstance(value, int | float) and value > 0:
                parts.append(part("other", str(key), int(value)))
    if not parts:
        parts = [part(g["role"], g["asset_type"], g["count"]) for g in groups]
    return [text for _, _, text in sorted(parts)]


def _org_subsidiaries(entity: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = entity.get("subsidiaries") or entity.get("children") or []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name_canonical") or item.get("name")
        public_id = item.get("public_id")
        if not (name or public_id):
            continue
        out.append({"public_id": public_id, "slug": item.get("slug"), "name": name, "type": item.get("type")})
    return out


def _derived_org_provenance(
    assets: list[dict[str, Any]], proposals: list[dict[str, Any]], opportunities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The distinct source rows behind an organisation's assets, proposals and opportunities --
    what the company page's Sources panel shows, since an organisation row has none of its own."""
    seen: set[tuple[str | None, str | None]] = set()
    out: list[dict[str, Any]] = []
    for record in [*assets, *proposals, *opportunities]:
        for row in record.get("provenance") or []:
            if not isinstance(row, Mapping):
                continue
            key = (row.get("source_id"), row.get("source_url"))
            if key in seen or not (row.get("source_id") or row.get("source_name")):
                continue
            seen.add(key)
            out.append(dict(row))
    return out


WEB_ROOT = Path(__file__).resolve().parent

app = FastAPI(title="Infraque -- public site")
app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))
# Sprint 3 "login and registration surface": /login, /register, /logout, /verify, /account (own
# router in web/auth.py -- see that module's docstring for why it keeps its own Jinja2Templates
# rather than importing this one).
app.include_router(auth_router)
from web.legal import router as legal_router  # noqa: E402

app.include_router(legal_router)
# Sprint 3 item 6: the public pricing page and its checkout hand-off (own router in
# web/pricing.py, same reason as the two above).
from web.pricing import router as pricing_router  # noqa: E402

app.include_router(pricing_router)

# Sprint 3 item 3: the admin panel shell (operator guard, chrome) — page routers for each nav
# group are mounted below it as they land.
from web.admin.shell import NotAnOperator, not_an_operator_handler  # noqa: E402
from web.admin.shell import router as admin_shell_router  # noqa: E402

app.add_exception_handler(NotAnOperator, not_an_operator_handler)
app.include_router(admin_shell_router)
from web.admin.posts import router as admin_posts_router  # noqa: E402
from web.admin.records import router as admin_records_router  # noqa: E402
from web.admin.sources import router as admin_sources_router  # noqa: E402
from web.admin.tasks import router as admin_tasks_router  # noqa: E402

app.include_router(admin_sources_router)
app.include_router(admin_records_router)
app.include_router(admin_tasks_router)
app.include_router(admin_posts_router)
from web.admin.engagement import router as admin_engagement_router  # noqa: E402
from web.admin.ops import router as admin_ops_router  # noqa: E402
from web.admin.people import router as admin_people_router  # noqa: E402

app.include_router(admin_people_router)
app.include_router(admin_ops_router)
app.include_router(admin_engagement_router)


def get_api(request: Request) -> ApiClient:
    """One `ApiClient` per app process (or per test app instance), cached on `app.state` -- the
    same lifetime `get_store()` gave the old static-file `Store`."""
    client: ApiClient | None = getattr(request.app.state, "api_client", None)
    if client is None:
        client = build_client()
        request.app.state.api_client = client
    return client


def is_preview_active(request: Request) -> bool:
    """docs/00-PLAN.md task item 5: "a dev-only override flag ... clearly labelled in the UI when
    active". `app.state.preview_active` lets a test set this directly without an environment
    variable; `web/dev_up.py --preview` sets `WEB_DEV_PREVIEW=1` for the real subprocess case."""
    override = getattr(request.app.state, "preview_active", None)
    if override is not None:
        return bool(override)
    return os.environ.get("WEB_DEV_PREVIEW", "").strip().lower() in ("1", "true", "yes", "on")


def get_lag_days(request: Request) -> dict[str, int]:
    """`lag_days_default` from `/v1/health` cached for the process lifetime -- it is server
    configuration, not per-request data, and the footer (product defect B) and delayed-tier notice
    on every page need it without a health round trip each time."""
    cached: dict[str, int] | None = getattr(request.app.state, "lag_days_default", None)
    if cached is None:
        health = get_api(request).get("/v1/health")
        cached = dict(health["lag_days_default"])
        request.app.state.lag_days_default = cached
    return cached


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


# Available in every template without every route threading them through by hand (product defect
# B's calm one-line footer, and the dev-preview label task item 5 requires "clearly labelled in
# the UI when active"). Jinja2Templates always injects `request` into the render context, so
# `{{ is_preview_active(request) }}` / `{{ footer_lag_days(request) }}` work from any template.
templates.env.globals["is_preview_active"] = is_preview_active
templates.env.globals["footer_lag_days"] = get_lag_days
templates.env.globals["asset_version"] = ASSET_VERSION

templates.env.globals["footer_build"] = lambda request: vm_footer_build(request, get_api(request))


# ---- SEO surface: canonical URLs, Open Graph / Twitter cards, JSON-LD ---------------------------
# docs/50 §3.2 web bullet ("no Open Graph or structured data") and docs/00-PLAN.md 2026-09-19
# item 4. docs/50 §4.4 is the reason it matters: the asset map and the crawlable asset and company
# pages are the acquisition surface, so every public page has to be findable by a crawler and
# legible when it is pasted into a chat or a social post.
#
# A page's title and description each have exactly one source: the `title` and `meta_description`
# blocks the page template already defines. `base.html` re-reads them through Jinja's
# `self.<block>()`, which returns the block's already-escaped `Markup` (so a name carrying a quote
# is escaped exactly once, never twice -- covered by
# `test_meta.py::test_record_name_with_quote_and_angle_bracket_...`). A description is therefore
# always built from the record the API returned and never from a template constant, and it cannot
# leak a field the visibility gate withheld, because the gate ran before the template saw the row.
SITE_NAME = "Infraque"


def canonical_url(request: Request, path: str | None = None) -> str:
    """The absolute URL for `rel=canonical` and `og:url`. `path` is the canonical path the route
    chose (record pages: the slug URL; index pages: the path plus only the filter parameters the
    page understands, so a URL carrying a stray tracking parameter canonicalises to the clean
    one); absent, the request's own path, which is right for every static page."""
    base = str(request.base_url).rstrip("/")
    return base + (request.url.path if path is None else path)


def canonical_query(params: QueryParams, names: Iterable[str]) -> str:
    """`?a=1&b=2` over `names` in a fixed order, skipping absent ones -- so two requests that
    differ only in parameter order share one canonical URL."""
    kept = [(name, params[name]) for name in names if params.get(name)]
    return ("?" + urlencode(kept)) if kept else ""


JSONLD_CONTEXT = "https://schema.org"
#: Rows an index page's `ItemList` names. The list describes the page, so it is the page's own
#: rows -- not the whole filtered corpus, which `numberOfItems` carries as a number.
JSONLD_LIST_CAP = 50


def _prune(value: Any) -> Any:
    """Drop `None` and empty members recursively, so every field that survives into a JSON-LD
    graph is one this record actually carries (task item 4: no invented fields)."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, raw in value.items():
            pruned = _prune(raw)
            if pruned is None or pruned == "" or pruned == [] or pruned == {}:
                continue
            out[key] = pruned
        return out
    if isinstance(value, list):
        return [_prune(item) for item in value if item is not None]
    return value


def jsonld_block(obj: Mapping[str, Any]) -> str:
    """One `<script type="application/ld+json">` payload, rendered with `| safe`.

    `ensure_ascii=True` plus the three replacements below leave no `<`, `>` or `&` anywhere in the
    output, so a record named `Acme "Big" <Energy> & Co </script>` can neither close the script
    element early nor be re-read as markup. `\\u003c` inside a JSON string is ordinary JSON string
    escaping, so a parser still recovers the original characters -- the graph stays honest while
    the page stays unbreakable.
    """
    text = json.dumps(_prune(obj), ensure_ascii=True, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def breadcrumb_jsonld(request: Request, trail: list[tuple[str, str]]) -> str:
    """schema.org `BreadcrumbList` for a record page (docs/30 §1: "breadcrumbs on every record
    page"). `trail` is `[(name, path), ...]` from the home page to this record; every `item` is
    the absolute URL of a page that exists on this site."""
    return jsonld_block(
        {
            "@context": JSONLD_CONTEXT,
            "@type": "BreadcrumbList",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": position,
                    "name": name,
                    "item": canonical_url(request, path),
                }
                for position, (name, path) in enumerate(trail, start=1)
            ],
        }
    )


def item_list_jsonld(
    request: Request,
    *,
    name: str,
    description: str,
    path: str,
    rows: list[tuple[str, str]],
    total: int | None = None,
) -> str:
    """schema.org `ItemList` for an index page: the rows this page renders, in the order it
    renders them. `numberOfItems` is the size of the whole list the page paginates through, which
    is what schema.org's note on multi-page pagination asks for -- omitted entirely when the count
    is an estimate or unknown rather than guessed at."""
    return jsonld_block(
        {
            "@context": JSONLD_CONTEXT,
            "@type": "ItemList",
            "name": name,
            "description": description,
            "url": canonical_url(request, path),
            "numberOfItems": total,
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": position,
                    "name": row_name,
                    "url": canonical_url(request, row_path),
                }
                for position, (row_name, row_path) in enumerate(rows[:JSONLD_LIST_CAP], start=1)
            ],
        }
    )


#: `organization.ids` keys (`api/openapi.yaml` `Organization.ids`) -> the `propertyID` a
#: schema.org `PropertyValue` carries. Only identifiers actually stored are emitted.
ORG_IDENTIFIER_LABELS = {
    "lei": "LEI",
    "sam_uei": "SAM UEI",
    "eia_utility_id": "EIA utility id",
    "cik": "SEC CIK",
    "duns": "DUNS",
}


def organization_jsonld(
    request: Request,
    record: Mapping[str, Any],
    *,
    ids: Mapping[str, Any] | None,
    path: str,
    description: str | None = None,
) -> str:
    """schema.org `Organization` for a company page. Name, URL, the identifiers this row actually
    holds, its website as `sameAs`, its country as a `PostalAddress`, and its parent -- nothing
    that is not a stored field (task item 4). `_prune` drops every absent one."""
    identifiers = [
        {
            "@type": "PropertyValue",
            "propertyID": ORG_IDENTIFIER_LABELS.get(str(key), str(key)),
            "value": str(value),
        }
        for key, value in (ids or {}).items()
        if value not in (None, "")
    ]
    parent_name = record.get("parent_name")
    parent_ident = record.get("parent_slug") or record.get("parent_public_id")
    return jsonld_block(
        {
            "@context": JSONLD_CONTEXT,
            "@type": "Organization",
            "name": record.get("name"),
            "url": canonical_url(request, path),
            "description": description,
            "identifier": identifiers,
            "sameAs": [record["website"]] if record.get("website") else None,
            "address": (
                {"@type": "PostalAddress", "addressCountry": record["country"]}
                if record.get("country")
                else None
            ),
            "parentOrganization": (
                {
                    "@type": "Organization",
                    "name": parent_name,
                    "url": canonical_url(request, f"/organizations/{parent_ident}"),
                }
                if parent_name and parent_ident
                else None
            ),
        }
    )


def querystring_without(params: QueryParams, *drop: str) -> str:
    kept = [(k, v) for k, v in params.multi_items() if k not in drop]
    return "&".join(f"{k}={v}" for k, v in kept)


def delayed_notice(request: Request, kind: str) -> dict[str, Any]:
    """docs/04 D-3/D-28: the notice always states the *actual configured* lag for the record's
    class, sourced from the API's own health payload -- never a hard-coded string (product defect
    E, task item 5). `public_at` from the API is what actually governs visibility; this notice is
    just the honest label for that, not a second filter.

    Since the paywall became a matter of shape rather than time (owner, 2026-09-19) `lag_days` is
    `0` for both kinds and the banner's job changes with it: it has to stop claiming a delay that
    no longer exists and instead name the one that does -- change events from an ISO queue
    register, `iso_change_event_lag_days`, still withheld from the free tier. Both numbers keep
    coming from `/v1/health`, not from a constant in this file, so the page can never disagree
    with the predicate that actually governs visibility."""
    lag_key = "supply" if kind == "proposal" else "opportunities"
    lag = get_lag_days(request)
    lag_days = lag[lag_key]
    now = dt.datetime.now(dt.UTC)
    data_as_of = (now - dt.timedelta(days=lag_days)).strftime("%Y-%m-%d")
    return {
        "lag_days": lag_days,
        "iso_change_event_lag_days": lag.get("iso_change_events", 0),
        "data_as_of": data_as_of,
        "preview_active": is_preview_active(request),
    }


def not_found_response(request: Request, kind: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "not_found.html", {"kind": kind}, status_code=404)


PROPOSAL_PASSTHROUGH_FILTERS = (
    "technology",
    "jurisdiction",
    "kind",
    "capacity_mw[gte]",
    "capacity_mw[lte]",
    "q",
    # ADR 0008 placement grades (docs/23 §3.1): both on `/proposals/geo` (the map) and on
    # `/proposals` (the list) -- a region-polygon click lands on `/proposals?county_fips=...`.
    "placement",
    "county_fips",
)
OPPORTUNITY_PASSTHROUGH_FILTERS = ("kind", "jurisdiction", "technologies", "q")


def _proposal_params(qp: QueryParams, *, lifecycle_csv: str) -> dict[str, str | None]:
    params: dict[str, str | None] = {name: qp[name] for name in PROPOSAL_PASSTHROUGH_FILTERS if qp.get(name)}
    params["lifecycle_state"] = lifecycle_csv
    return params


@app.get("/", response_class=HTMLResponse)
def home_map(request: Request) -> HTMLResponse:
    api = get_api(request)
    qp = request.query_params
    _lifecycle_csv, explicit, include_withdrawn = resolve_proposal_lifecycle_param(qp)
    vocab = api.get("/v1/meta/vocabularies")["data"]
    breakdown = lifecycle_breakdown(
        api, extra_filters={n: qp[n] for n in ("technology", "jurisdiction", "kind") if qp.get(n)}
    )
    tile_url = (os.environ.get("MAP_TILE_URL") or "").strip() or None
    tile_mode = _tile_mode(tile_url)
    # docs/40 §2.7 / task item 3: only regions a published, non-gated source actually covers get a
    # jump button -- reusing the same `/v1/sources` fetch `about()` already makes, filtered
    # server-side to `publish_state=public` so a gated-but-listed source never counts.
    sources_env = api.get("/v1/sources", params={"limit": 100, "publish_state": "public"})
    regions = [_region_context(r) for r in regions_with_data(sources_env["data"])]
    return templates.TemplateResponse(
        request,
        "home_map.html",
        {
            "delayed": delayed_notice(request, "proposal"),
            "technologies": [v["value"] for v in vocab["technology"]],
            "kinds": [v["value"] for v in vocab["proposal_kind"]],
            "include_withdrawn": include_withdrawn,
            "lifecycle_explicit": explicit,
            "filters": dict(qp),
            "breakdown": breakdown,
            "active_states": ACTIVE_PROPOSAL_STATES,
            "withdrawn_states": WITHDRAWN_PROPOSAL_STATES,
            "tile_url": tile_url,
            "tile_mode": tile_mode,
            "basemap_attribution": _basemap_attribution(tile_mode, tile_url),
            "regions": regions,
            "asset_types": HOME_MAP_ASSET_TYPES,
        },
    )


@app.get("/map")
def map_alias() -> RedirectResponse:
    return RedirectResponse(url="/")


@app.get("/api/proposals/geo")
def proposals_geo_proxy(request: Request) -> JSONResponse:
    """What `web/static/js/map.js` fetches: the API's own clustering, proxied so the browser
    never needs to know the API's host/port (in HTTP mode) or that there is no separate host at
    all (in-process mode). The client renders exactly what comes back; it does not cluster."""
    api = get_api(request)
    qp = request.query_params
    lifecycle_csv, _explicit, _include = resolve_proposal_lifecycle_param(qp)
    params = _proposal_params(qp, lifecycle_csv=lifecycle_csv)
    params["bbox"] = qp.get("bbox") or WORLD_BBOX
    params["zoom"] = qp.get("zoom") or "3"
    try:
        envelope = api.get("/v1/proposals/geo", params=params)
    except ApiError as exc:
        return JSONResponse(exc.body, status_code=exc.status_code)
    envelope["data"] = relativize_geo_feature_urls(envelope["data"])
    return JSONResponse(envelope)


@app.get("/api/context/plants/geo")
def plants_geo_proxy(request: Request) -> JSONResponse:
    """Same-origin proxy for `GET /v1/context/plants/geo` (task contract), mirroring
    `proposals_geo_proxy` above: forwards `bbox`, `zoom`, `technology` only, no cookies. The API
    lane had not landed this route while this was built -- `web/test_map_layers.py` monkeypatches
    `ApiClient` rather than exercising a real API app for these tests."""
    api = get_api(request)
    qp = request.query_params
    params: dict[str, str | None] = {
        "bbox": qp.get("bbox") or WORLD_BBOX,
        "zoom": qp.get("zoom") or "3",
    }
    if qp.get("technology"):
        params["technology"] = qp["technology"]
    try:
        envelope = api.get("/v1/context/plants/geo", params=params)
    except ApiError as exc:
        return JSONResponse(exc.body, status_code=exc.status_code)
    return JSONResponse(envelope)


@app.get("/api/assets/geo")
def assets_geo_proxy(request: Request) -> JSONResponse:
    """Same-origin proxy for `GET /v1/assets/geo` (ADR 0008): forwards `bbox`, `zoom`,
    `asset_type`, `technology` only, no cookies -- `web/static/js/map.js`'s assets layer fetches
    this instead of `/api/context/plants/geo` (which stays, unchanged, as its own proxy for the
    `/v1/context/plants/geo` alias route)."""
    api = get_api(request)
    qp = request.query_params
    params: dict[str, str | None] = {
        "bbox": qp.get("bbox") or WORLD_BBOX,
        "zoom": qp.get("zoom") or "3",
    }
    if qp.get("asset_type"):
        params["asset_type"] = qp["asset_type"]
    if qp.get("technology"):
        params["technology"] = qp["technology"]
    try:
        envelope = api.get("/v1/assets/geo", params=params)
    except ApiError as exc:
        return JSONResponse(exc.body, status_code=exc.status_code)
    return JSONResponse(envelope)


#: `web/static/js/map.js` fetches regions in one batch per level (docs/23 §3.1 `ids` csv, max
#: 500); cached client-side for the page's session, not here -- this proxy is a stateless
#: pass-through, forwarding only `level`/`ids` (never cookies), matching every other geo proxy in
#: this module. `/v1/geo/regions` is "cacheable for a day" per docs/23; no separate server-side
#: cache is added here, since `StaticFiles`/the API's own caching is where that would belong, not
#: a same-origin relay with no storage of its own.
@app.get("/api/geo/regions")
def geo_regions_proxy(request: Request) -> JSONResponse:
    api = get_api(request)
    qp = request.query_params
    params: dict[str, str | None] = {"level": qp.get("level"), "ids": qp.get("ids")}
    try:
        envelope = api.get("/v1/geo/regions", params=params)
    except ApiError as exc:
        return JSONResponse(exc.body, status_code=exc.status_code)
    return JSONResponse(envelope)


@app.post("/api/ui-events")
async def ui_events_proxy(request: Request) -> JSONResponse:
    """Same-origin proxy for `POST /v1/ui-events` (task contract): forwards only `name`/`props`
    (never cookies or headers, never the caller's IP/UA to the API beyond what any HTTP request
    already carries at the transport level) and always answers 202, whether or not the API call
    behind it succeeds -- measurement must never be able to break the map page. `map.js` posts
    here with `navigator.sendBeacon` (a `Blob`, so the request may arrive without a JSON
    content-type; Starlette's `Request.json()` parses the body regardless)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("name") if isinstance(body, dict) else None
    props = body.get("props") if isinstance(body, dict) and isinstance(body.get("props"), dict) else {}
    if isinstance(name, str) and name:
        api = get_api(request)
        try:
            api.post("/v1/ui-events", json={"name": name, "props": props})
        except Exception:  # noqa: S110 -- measurement must never block or surface an error here
            pass
    return JSONResponse({}, status_code=202)


@app.get("/proposals", response_class=HTMLResponse)
def proposals_list(request: Request) -> HTMLResponse:
    api = get_api(request)
    qp = request.query_params
    lifecycle_csv, explicit, include_withdrawn = resolve_proposal_lifecycle_param(qp)
    params = _proposal_params(qp, lifecycle_csv=lifecycle_csv)
    params["sort"] = qp.get("sort") or "-capacity_mw"
    params["cursor"] = qp.get("cursor")
    params["include"] = "count"
    envelope = api.get("/v1/proposals", params=params)
    vocab = api.get("/v1/meta/vocabularies")["data"]
    breakdown = lifecycle_breakdown(
        api, extra_filters={n: qp[n] for n in ("technology", "jurisdiction", "kind") if qp.get(n)}
    )

    records = [flatten_proposal(e) for e in envelope["data"]]
    canonical_path = "/proposals" + canonical_query(
        qp, (*PROPOSAL_PASSTHROUGH_FILTERS, "include_withdrawn", "sort", "cursor")
    )
    context = {
        "records": records,
        "total": envelope["meta"].get("total"),
        "total_is_estimate": envelope["meta"].get("total_is_estimate", False),
        "has_more": envelope["page"]["has_more"],
        "next_cursor": envelope["page"]["next_cursor"],
        "prev_cursor": envelope["page"]["prev_cursor"],
        "querystring": querystring_without(qp, "cursor"),
        "delayed": delayed_notice(request, "proposal"),
        "technologies": [v["value"] for v in vocab["technology"]],
        "kinds": [v["value"] for v in vocab["proposal_kind"]],
        "include_withdrawn": include_withdrawn,
        "lifecycle_explicit": explicit,
        "filters": dict(qp),
        "breakdown": breakdown,
        "canonical_path": canonical_path,
        "jsonld": [
            item_list_jsonld(
                request,
                name="Proposals",
                description="Interconnection queue and generator proposals matching these filters.",
                path=canonical_path,
                rows=[(r["name"], f"/proposals/{r['slug']}") for r in records],
                total=(None if envelope["meta"].get("total_is_estimate") else envelope["meta"].get("total")),
            )
        ],
    }
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/_proposal_rows.html", context)
    return templates.TemplateResponse(request, "proposals_list.html", context)


def _resolve_proposal_by_slug(api: ApiClient, slug: str) -> dict[str, Any] | None:
    """`GET /v1/proposals?slug=...` (`api/openapi.yaml`'s `SlugFilter`, added in
    `services/README.md`'s "Sprint 2 fixes" #5) is an exact match on the record's own slug --
    this replaces the search-and-scan workaround (derive a phrase from the slug, scan up to 200
    `q=` results for it) that used to stand in for a slug-keyed lookup, per web/README.md "Missing
    from the API" item 1, now resolved. No `lifecycle_state` filter is passed, so a detail page
    resolves regardless of the record's lifecycle state, matching the old workaround's behaviour.
    """
    envelope = api.get("/v1/proposals", params={"slug": slug, "limit": 1})
    entities: list[dict[str, Any]] = envelope["data"]
    return entities[0] if entities else None


def _resolve_opportunity_by_slug(api: ApiClient, slug: str) -> dict[str, Any] | None:
    """Same slug-filter lookup as `_resolve_proposal_by_slug`; `status=` is still passed as every
    status (the API defaults `status` to `open` when absent, docs/23), so a detail page resolves
    regardless of the opportunity's status, matching the old workaround's behaviour."""
    envelope = api.get(
        "/v1/opportunities", params={"slug": slug, "limit": 1, "status": ALL_OPPORTUNITY_STATUSES_CSV}
    )
    entities: list[dict[str, Any]] = envelope["data"]
    return entities[0] if entities else None


@app.get("/proposals/{slug}", response_class=HTMLResponse)
def proposal_detail(request: Request, slug: str) -> HTMLResponse:
    api = get_api(request)
    entity = _resolve_proposal_by_slug(api, slug)
    if entity is None:
        return not_found_response(request, "proposal")
    record = flatten_proposal(entity)
    path = f"/proposals/{record['slug']}"
    return templates.TemplateResponse(
        request,
        "proposal_detail.html",
        {
            "record": record,
            "provenance_rows": provenance_panel_rows(api, record["provenance"]),
            "delayed": delayed_notice(request, "proposal"),
            "canonical_path": path,
            "jsonld": [
                breadcrumb_jsonld(
                    request, [("Home", "/"), ("Proposals", "/proposals"), (record["name"], path)]
                )
            ],
        },
    )


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities_list(request: Request) -> HTMLResponse:
    api = get_api(request)
    qp = request.query_params
    status_param = opportunity_status_param(qp)
    params: dict[str, str | None] = {
        name: qp[name] for name in OPPORTUNITY_PASSTHROUGH_FILTERS if qp.get(name)
    }
    params["status"] = status_param
    params["cursor"] = qp.get("cursor")
    params["include"] = "count"
    envelope = api.get("/v1/opportunities", params=params)
    vocab = api.get("/v1/meta/vocabularies")["data"]

    records = [flatten_opportunity(e) for e in envelope["data"]]
    canonical_path = "/opportunities" + canonical_query(
        qp, (*OPPORTUNITY_PASSTHROUGH_FILTERS, "status", "cursor")
    )
    context = {
        "records": records,
        "total": envelope["meta"].get("total"),
        "total_is_estimate": envelope["meta"].get("total_is_estimate", False),
        "has_more": envelope["page"]["has_more"],
        "next_cursor": envelope["page"]["next_cursor"],
        "prev_cursor": envelope["page"]["prev_cursor"],
        "querystring": querystring_without(qp, "cursor"),
        "delayed": delayed_notice(request, "opportunity"),
        "kinds": [v["value"] for v in vocab["opportunity_kind"]],
        "statuses": [v["value"] for v in vocab["opportunity_status"]],
        "technologies": [v["value"] for v in vocab["technology"]],
        "filters": {**dict(qp), "status": qp.get("status", "open")},
        "canonical_path": canonical_path,
        "jsonld": [
            item_list_jsonld(
                request,
                name="Opportunities",
                description="Grants, tenders and procurement notices matching these filters.",
                path=canonical_path,
                rows=[(r["title"], f"/opportunities/{r['slug']}") for r in records],
                total=(None if envelope["meta"].get("total_is_estimate") else envelope["meta"].get("total")),
            )
        ],
    }
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/_opportunity_rows.html", context)
    return templates.TemplateResponse(request, "opportunities_list.html", context)


@app.get("/opportunities/{slug}", response_class=HTMLResponse)
def opportunity_detail(request: Request, slug: str) -> HTMLResponse:
    api = get_api(request)
    entity = _resolve_opportunity_by_slug(api, slug)
    if entity is None:
        return not_found_response(request, "opportunity")
    record = flatten_opportunity(entity)
    path = f"/opportunities/{record['slug']}"
    return templates.TemplateResponse(
        request,
        "opportunity_detail.html",
        {
            "record": record,
            "provenance_rows": provenance_panel_rows(api, record["provenance"]),
            "delayed": delayed_notice(request, "opportunity"),
            "canonical_path": path,
            "jsonld": [
                breadcrumb_jsonld(
                    request,
                    [("Home", "/"), ("Opportunities", "/opportunities"), (record["title"], path)],
                )
            ],
        },
    )


# ---- /assets index (docs/00-PLAN.md 2026-09-19 item 4; docs/30 §1.1) ----------------------------
#: The filters `/assets` forwards, named exactly as `GET /v1/assets` names them (D-17: one filter
#: vocabulary and one URL grammar across list, map and feed). `technology` is deliberately not
#: exposed as a control -- its vocabulary is plant-shaped and means nothing for a pipeline -- but
#: it is forwarded when present so a link from the map keeps working.
ASSET_INDEX_FILTERS = ("asset_type", "state", "q", "technology")
#: `sort=` tokens `services/api/assets.py::ASSET_SORT_ALLOWLIST` accepts, with the words the
#: control shows. Capacity descending is the default: the biggest thing is the most interesting
#: row on an index of infrastructure. Length is *not* an API sort field, so a pipeline's mileage
#: shows in its row but never orders the list; name ascending is the tiebreak a reader can reach.
ASSET_INDEX_SORTS = (
    ("-capacity_mw", "Capacity, largest first"),
    ("name", "Name, A to Z"),
    ("-last_changed", "Recently changed"),
)
ASSET_INDEX_DEFAULT_SORT = "-capacity_mw"
#: A line asset carries no `capacity_mw`, so a capacity sort over a pipeline-only view orders it
#: by nothing at all. `length_miles` is not in the API's sort allowlist, so the honest default for
#: such a view is the one key that does order it: name.
ASSET_INDEX_LINE_DEFAULT_SORT = "name"
ASSET_TYPE_COUNTS_CACHE_SECONDS = 900


def _asset_index_default_sort(selected_types: set[str]) -> str:
    if selected_types and selected_types <= LINE_ASSET_TYPES:
        return ASSET_INDEX_LINE_DEFAULT_SORT
    return ASSET_INDEX_DEFAULT_SORT


#: The subject of `/assets`' description when no type is selected.
ASSET_INDEX_SUBJECT = (
    "Power plants, gas pipelines, gas processing and storage sites, LNG terminals, ethanol "
    "plants and RNG projects"
)


def _asset_index_description(selected_types: set[str], counts: Mapping[str, int], params: QueryParams) -> str:
    """The page's `<meta name="description">`, built from the filters actually in force.

    A filtered index that repeated the unfiltered page's sentence would hand a crawler one
    description for many URLs, and would claim a corpus total beside a list that does not show
    it. The count is quoted only for a view whose size `asset_type_counts()` actually knows: the
    whole corpus, or one type of it, with no other filter narrowing the rows."""
    only = next(iter(selected_types)) if len(selected_types) == 1 else None
    subject = _type_label(only, plural=True) if only else ASSET_INDEX_SUBJECT
    where = f" in {params['state']}" if params.get("state") else ""
    total = counts.get(only) if only else (sum(counts.values()) or None)
    counted = f" -- {total:,} of them" if total and not where and not params.get("q") else ""
    return (
        f"{subject}{where} from US public registers{counted}, each with its owner, operator, "
        "location and the register it came from."
    )


def asset_type_counts(request: Request) -> dict[str, int]:
    """`{asset_type: n}` for the counts above `/assets`, read from `GET /v1/assets/geo`'s
    `totals.asset_type_counts` over the whole world in one call.

    `GET /v1/assets` has no `include=count` (`services/api/assets.py::list_assets`'s allowlist),
    and one counting call per type would be twelve round trips per page render, so the map
    endpoint -- which already counts by type for the legend -- is the cheaper honest source. Its
    denominator is assets with a published location, which is *not* the same set as the list
    below (an asset whose licence forbids raw publication keeps its row and page but is off the
    map, `docs/23` `/v1/assets/geo`), so the template says so rather than implying the two agree.
    The counts change once per data load, so they are cached per app process like the sitemap; a
    failed call returns `{}` and the page simply renders no counts.
    """
    cache: dict[str, tuple[float, dict[str, int]]] = request.app.state.__dict__.setdefault(
        "asset_type_counts_cache", {}
    )
    cached = cache.get("world")
    if cached and time.monotonic() - cached[0] < ASSET_TYPE_COUNTS_CACHE_SECONDS:
        return cached[1]
    counts: dict[str, int] = {}
    try:
        envelope = get_api(request).get("/v1/assets/geo", params={"bbox": WORLD_BBOX, "zoom": "3"})
        data = envelope.get("data")
        totals = data.get("totals") if isinstance(data, Mapping) else None
        raw = totals.get("asset_type_counts") if isinstance(totals, Mapping) else None
        if isinstance(raw, Mapping):
            counts = {str(k): int(v) for k, v in raw.items() if isinstance(v, int | float) and v > 0}
    except (ApiError, httpx.HTTPError):
        return {}
    cache["world"] = (time.monotonic(), counts)
    return counts


@app.get("/assets", response_class=HTMLResponse)
def assets_list(request: Request) -> HTMLResponse:
    """The crawlable index of every registry asset (docs/50 §4.4: the asset pages are the
    acquisition surface, so they need a path in from a page a crawler can reach). Same markup,
    pagination and empty state as `/proposals` -- `partials/_asset_rows.html` mirrors
    `partials/_proposal_rows.html` rather than inventing a second list idiom."""
    api = get_api(request)
    qp = request.query_params
    selected_types = set((qp.get("asset_type") or "").split(",")) - {""}
    sort = qp.get("sort") or _asset_index_default_sort(selected_types)
    if sort not in {token for token, _label in ASSET_INDEX_SORTS}:
        sort = _asset_index_default_sort(selected_types)
    params: dict[str, str | None] = {name: qp[name] for name in ASSET_INDEX_FILTERS if qp.get(name)}
    params["sort"] = sort
    params["cursor"] = qp.get("cursor")
    envelope = api.get("/v1/assets", params=params)
    records: list[dict[str, Any]] = []
    for entity in envelope["data"]:
        record = flatten_asset(entity)
        record.update(_asset_extras(entity))
        records.append(record)
    counts = asset_type_counts(request)
    type_counts = [
        {
            "value": asset_type,
            "label": _type_label(asset_type, plural=True),
            "count": counts[asset_type],
            "selected": asset_type in selected_types,
        }
        for asset_type in sorted(counts, key=lambda k: (-counts[k], _type_label(k, plural=True)))
    ]
    page = envelope["page"]
    canonical_path = "/assets" + canonical_query(qp, (*ASSET_INDEX_FILTERS, "sort", "cursor"))
    # `numberOfItems` only when the page is the unfiltered index: `asset_type_counts()` counts the
    # whole corpus, so quoting it beside a filtered list would be a number the page does not show.
    filtered = any(qp.get(name) for name in ASSET_INDEX_FILTERS)
    corpus_total = (sum(counts.values()) or None) if not filtered else None
    context = {
        "records": records,
        "type_counts": type_counts,
        "counts_total": sum(counts.values()) or None,
        "sorts": ASSET_INDEX_SORTS,
        "sort": sort,
        "sort_caption": next(label.lower() for token, label in ASSET_INDEX_SORTS if token == sort),
        "description": _asset_index_description(selected_types, counts, qp),
        "has_more": page["has_more"],
        "next_cursor": page["next_cursor"],
        "prev_cursor": page.get("prev_cursor"),
        "querystring": querystring_without(qp, "cursor"),
        "filters": dict(qp),
        "canonical_path": canonical_path,
        "jsonld": [
            item_list_jsonld(
                request,
                name="Assets",
                description=_asset_index_description(selected_types, counts, qp),
                path=canonical_path,
                rows=[(r["name"], f"/assets/{r['slug']}") for r in records if r.get("slug")],
                total=corpus_total,
            )
        ],
    }
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/_asset_rows.html", context)
    return templates.TemplateResponse(request, "assets_list.html", context)


def _resolve_asset_by_slug(api: ApiClient, slug: str) -> dict[str, Any] | None:
    """Same slug-filter lookup as `_resolve_proposal_by_slug` (ADR 0008: `asset` carries a
    `slug`, `docs/21` §3.22)."""
    envelope = api.get("/v1/assets", params={"slug": slug, "limit": 1})
    entities: list[dict[str, Any]] = envelope["data"]
    return entities[0] if entities else None


def _resolve_organization(api: ApiClient, ident: str) -> dict[str, Any] | None:
    """Company pages are linked to from two places that may hand this route different kinds of
    identifier: `/search`'s organisations section links by slug (matching every other search
    result on this site), while `asset_detail.html`'s owners table links by the organisation
    `public_id` `docs/23`'s `/v1/assets/{public_id}` owners embed is documented to carry (that
    table row does not promise a `slug` on each owner). Tried as a slug first (the common case,
    one list call); a miss falls back to a direct `public_id` lookup rather than resolving every
    owner row's slug up front for a page that may render dozens of them.
    """
    envelope = api.get("/v1/organizations", params={"slug": ident, "limit": 1})
    entities: list[dict[str, Any]] = envelope["data"]
    if entities:
        return entities[0]
    try:
        result: dict[str, Any] = api.get(f"/v1/organizations/{ident}")["data"]
        return result
    except ApiNotFound:
        return None


@app.get("/assets/{slug}", response_class=HTMLResponse)
def asset_detail(request: Request, slug: str) -> HTMLResponse:
    """ADR 0008 asset page: identity, attributes, owners and nearby exact-grade proposals."""
    api = get_api(request)
    entity = _resolve_asset_by_slug(api, slug)
    if entity is None:
        return not_found_response(request, "asset")
    # The list row resolves the slug but omits `owners` and `geometry` (API lane 2026-09-19: list
    # rows never embed geometry); the detail envelope carries both, so the page reads it and
    # falls back to the list row only when the detail call fails (found on the first Tallgrass
    # screenshots: "No ownership records" beside 1,126 operator edges).
    try:
        entity = api.get(f"/v1/assets/{entity['public_id']}")["data"]
    except ApiError:
        pass
    record = flatten_asset(entity)
    record.update(_asset_extras(entity))
    nearby: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    if record["geometry"] is not None:
        features.append(_asset_feature(record, record["geometry"]))
    try:
        nearby_env = api.get(f"/v1/assets/{record['public_id']}/nearby-proposals")
        for e in nearby_env["data"]:
            flat = flatten_proposal(e)
            # `distance_km` (line-aware for pipelines, API lane 2026-09-19) rides on the row.
            flat["distance_km"] = _number(e.get("distance_km"))
            nearby.append(flat)
            geometry = _geometry_of(e)
            if geometry is not None:
                features.append(_proposal_feature(flat, geometry))
    except ApiError:
        nearby = []
    # One list row per project, not per EIA-860M generator unit; the map keeps every unit's dot.
    nearby = group_nearby_proposals(nearby)
    tile_url = (os.environ.get("MAP_TILE_URL") or "").strip() or None
    tile_mode = _tile_mode(tile_url)
    placed = sum(1 for f in features if f["properties"]["kind"] == "proposal")
    where = "route" if record["is_line"] else "location"
    caption = (
        f"{record['name']}: {where}"
        + (f" and {placed} exact-grade proposal{'s' if placed != 1 else ''} within 25 km" if placed else "")
        + ". "
        + _basemap_attribution(tile_mode, tile_url)
    )
    mini_map = _mini_map(features, label=f"Map of {record['name']}", caption=caption)
    path = f"/assets/{record['slug']}"
    return templates.TemplateResponse(
        request,
        "asset_detail.html",
        {
            "record": record,
            "nearby_proposals": nearby,
            "provenance_rows": provenance_panel_rows(api, record["provenance"]),
            "mini_map": mini_map,
            "tile_url": tile_url,
            "tile_mode": tile_mode,
            "canonical_path": path,
            "jsonld": [
                breadcrumb_jsonld(request, [("Home", "/"), ("Assets", "/assets"), (record["name"], path)])
            ],
        },
    )


@app.get("/assets/by-id/{public_id}")
def asset_by_public_id(request: Request, public_id: str) -> Response:
    """Map features (`/v1/assets/geo`) carry an asset's `public_id` but not its slug; the drawer's
    "Open asset page" link comes here and is redirected to the canonical slug URL."""
    api = get_api(request)
    try:
        entity = api.get(f"/v1/assets/{public_id}")["data"]
    except ApiNotFound:
        return not_found_response(request, "asset")
    slug = entity.get("slug")
    if not slug:
        return not_found_response(request, "asset")
    return RedirectResponse(url=f"/assets/{slug}", status_code=302)


@app.get("/api/assets/{public_id}")
def asset_detail_proxy(request: Request, public_id: str) -> JSONResponse:
    """Same-origin relay for `GET /v1/assets/{public_id}` (no params, no cookies): the map drawer
    fetches it when a point asset is opened, because `/v1/assets/geo` point features carry no
    `capacity_value`/`capacity_unit`/`attributes` (only line features do) and an ethanol plant's
    nameplate or a landfill project's LFG flow lives there. The drawer renders what the feature
    has first and re-renders when this answers, so a failure here costs rows, never the drawer."""
    api = get_api(request)
    try:
        envelope = api.get(f"/v1/assets/{public_id}")
    except ApiError as exc:
        return JSONResponse(exc.body, status_code=exc.status_code)
    return JSONResponse(envelope)


# ---- /organizations index (docs/00-PLAN.md 2026-09-19 item 4) -----------------------------------
#: Forwarded to `GET /v1/organizations` under the API's own names; `q` is a case-insensitive
#: substring of `name_canonical` (`services/api/app.py::list_organizations`), which is exactly the
#: "searchable by name" this index needs.
ORG_INDEX_FILTERS = ("q", "type", "country")
#: Smaller than the 50-row proposals page on purpose: `GET /v1/organizations` list rows carry no
#: `asset_counts` (only `GET /v1/organizations/{id}` does), so each row costs one extra call.
#: Twenty-five keeps the worst case at 26 upstream calls per render. When the API lane adds
#: `asset_counts` to the list row, `_org_holdings()` loses its second call and this can grow.
ORG_INDEX_PAGE_SIZE = 25


def _org_holdings(api: ApiClient, public_id: str | None) -> dict[str, Any]:
    """What a company-index row says the organisation holds: the `asset_counts` the detail
    response carries, turned into the same descriptor and the same "Operates 3 gas pipelines"
    clauses the company page shows, through `org_descriptor()`/`_org_type_counts()`/
    `_org_summary_parts()` -- the helpers the ownership lane landed, reused rather than copied.
    `group_asset_counts` is preferred where present for the same reason the company page passes
    `include_subsidiaries=true`: a holding parent holds no edge itself, its subsidiaries do.
    A failed or absent count leaves the row rendering name and type alone, never a zero."""
    if not public_id:
        return {"type_counts": {}, "summary_parts": [], "asset_total": None}
    try:
        detail = api.get(f"/v1/organizations/{public_id}")["data"]
    except ApiError:
        return {"type_counts": {}, "summary_parts": [], "asset_total": None}
    counts = detail.get("group_asset_counts") or detail.get("asset_counts")
    type_counts = _org_type_counts(counts, [])
    return {
        "type_counts": type_counts,
        "summary_parts": _org_summary_parts(counts, []),
        "asset_total": sum(type_counts.values()) or None,
        "proposal_count": detail.get("proposal_count"),
        "opportunity_count": detail.get("opportunity_count"),
    }


@app.get("/organizations", response_class=HTMLResponse)
def organizations_list(request: Request) -> HTMLResponse:
    """The crawlable index of companies. Mirrors `/proposals`' markup, pagination and empty state;
    the searchable control is a single name box because `GET /v1/organizations`'s `q` is a name
    substring and promising more than that would be a filter the API cannot honour."""
    api = get_api(request)
    qp = request.query_params
    params: dict[str, str | None] = {name: qp[name] for name in ORG_INDEX_FILTERS if qp.get(name)}
    params["limit"] = str(ORG_INDEX_PAGE_SIZE)
    params["cursor"] = qp.get("cursor")
    envelope = api.get("/v1/organizations", params=params)
    records: list[dict[str, Any]] = []
    for entity in envelope["data"]:
        record = flatten_organization(entity)
        holdings = _org_holdings(api, record["public_id"])
        record.update(holdings)
        record["descriptor"] = org_descriptor(record.get("type"), holdings["type_counts"])
        record["href"] = f"/organizations/{record['slug'] or record['public_id']}"
        records.append(record)
    page = envelope["page"]
    canonical_path = "/organizations" + canonical_query(qp, (*ORG_INDEX_FILTERS, "cursor"))
    context = {
        "records": records,
        "has_more": page["has_more"],
        "next_cursor": page["next_cursor"],
        "prev_cursor": page.get("prev_cursor"),
        "querystring": querystring_without(qp, "cursor"),
        "filters": dict(qp),
        "canonical_path": canonical_path,
        "jsonld": [
            item_list_jsonld(
                request,
                name="Companies",
                description=(
                    "Owners and operators of the assets, proposals and opportunities in the "
                    "Infraque register."
                ),
                path=canonical_path,
                rows=[(r["name"], r["href"]) for r in records if r.get("name")],
            )
        ],
    }
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/_organization_rows.html", context)
    return templates.TemplateResponse(request, "organizations_list.html", context)


@app.get("/organizations/{ident}", response_class=HTMLResponse)
def organization_detail(request: Request, ident: str) -> HTMLResponse:
    """ADR 0008 company page: assets (through `asset_owner`, with role/share), proposals,
    opportunities, provenance."""
    api = get_api(request)
    entity = _resolve_organization(api, ident)
    if entity is None:
        return not_found_response(request, "organisation")
    record = flatten_organization(entity)
    parent_raw = entity.get("parent")
    record["parent_slug"] = parent_raw.get("slug") if isinstance(parent_raw, Mapping) else None
    record["subsidiary_count"] = entity.get("subsidiary_count")
    public_id = record["public_id"]
    asset_counts = entity.get("asset_counts")
    try:
        # A parent such as Tallgrass Energy holds no edge itself; the subsidiaries do (curated
        # parents, services/ingest/midstream.py), so the page always asks for the group.
        assets_env = api.get(
            f"/v1/organizations/{public_id}/assets",
            params={"limit": 100, "include_subsidiaries": "true"},
        )
        assets = [_org_asset_row(r) for r in assets_env["data"]]
        meta = assets_env.get("meta")
        if asset_counts is None and isinstance(meta, Mapping):
            asset_counts = meta.get("asset_counts")
    except ApiError:
        assets = []
    try:
        proposals_env = api.get(f"/v1/organizations/{public_id}/proposals", params={"limit": 100})
        proposals = [flatten_proposal(e) for e in proposals_env["data"]]
    except ApiError:
        proposals = []
    try:
        opportunities_env = api.get(
            f"/v1/organizations/{public_id}/opportunities",
            params={"limit": 100, "status": ALL_OPPORTUNITY_STATUSES_CSV},
        )
        opportunities = [flatten_opportunity(e) for e in opportunities_env["data"]]
    except ApiError:
        opportunities = []
    groups = _org_asset_groups(assets)
    # The map of everything the organisation owns or operates: geometry from the asset rows when
    # the API embeds it, else each asset's own detail response (capped, see ORG_MAP_DETAIL_CAP).
    for a in [a for a in assets if not a.get("geometry") and a.get("public_id")][:ORG_MAP_DETAIL_CAP]:
        try:
            a["geometry"] = _geometry_of(api.get(f"/v1/assets/{a['public_id']}")["data"])
        except ApiError:
            continue
    features = [_asset_feature(a, a["geometry"]) for a in assets if a.get("geometry")]
    # "Proposals near those pipelines" (owner, 2026-09-19): exact-grade proposals within 25 km of
    # any of the organisation's assets, each at its distance to the nearest one, which is named.
    # Narrowed by default to the technologies the company's own asset types make relevant
    # (`data/vendored/relevance/asset_technology_relevance.yaml`, owner 2026-09-20: "given
    # tallgrass doesnt do solar, I dont want them seeing solar"). The narrowing is stated in words
    # with both counts above the list and undone by one link, because a silently shortened list
    # reads as thin coverage.
    try:
        technology_vocabulary = [v["value"] for v in api.get("/v1/meta/vocabularies")["data"]["technology"]]
    except (ApiError, KeyError, TypeError):
        technology_vocabulary = []
    relevance_default = load_relevance().default_for(
        _org_type_counts(entity.get("group_asset_counts") or asset_counts, groups)
    )
    nearby_filter = resolve_nearby_filter(
        request.query_params.get(NEARBY_TECHNOLOGY_PARAM),
        default=relevance_default,
        vocabulary=technology_vocabulary or None,
    )
    nearby: list[dict[str, Any]] = []
    nearby_totals: Mapping[str, Any] = {}
    try:
        nearby_params: dict[str, Any] = {"limit": ORG_NEARBY_LIMIT, "include_subsidiaries": "true"}
        if nearby_filter.technologies:
            nearby_params["technology"] = ",".join(nearby_filter.technologies)
        nearby_env = api.get(f"/v1/organizations/{public_id}/nearby-proposals", params=nearby_params)
        raw_totals = nearby_env.get("totals")
        nearby_totals = raw_totals if isinstance(raw_totals, Mapping) else {}
        for e in nearby_env["data"]:
            flat = flatten_proposal(e)
            flat["distance_km"] = _number(e.get("distance_km"))
            nearest_raw = e.get("nearest_asset")
            nearest: Mapping[str, Any] = nearest_raw if isinstance(nearest_raw, Mapping) else {}
            flat["nearest_asset_name"] = nearest.get("name")
            flat["nearest_asset_slug"] = nearest.get("slug")
            nearby.append(flat)
            geometry = _geometry_of(e)
            if geometry is not None:
                features.append(_proposal_feature(flat, geometry))
    except ApiError:
        nearby = []
    # Counts for the "N of M" line come from the API's `totals`, never from the rendered rows:
    # `limit` caps the page and `group_nearby_proposals` collapses a project's generator units, so
    # the row count is neither the matched count nor the total.
    nearby_shown = _count_or(nearby_totals.get("proposals_within_radius"), len(nearby))
    nearby_total = _count_or(nearby_totals.get("proposals_within_radius_unfiltered"), nearby_shown)
    nearby = group_nearby_proposals(nearby)
    tile_url = (os.environ.get("MAP_TILE_URL") or "").strip() or None
    tile_mode = _tile_mode(tile_url)
    mapped = sum(1 for f in features if f["properties"]["kind"] == "asset")
    unmapped = len(assets) - mapped
    plural = "s" if len(nearby) != 1 else ""
    caption = (
        f"{mapped} of {len(assets)} asset{'s' if len(assets) != 1 else ''} with a mapped location"
        + (f"; {unmapped} without one {'is' if unmapped == 1 else 'are'} listed below" if unmapped else "")
        + (f", and {len(nearby)} exact-grade proposal{plural} within 25 km" if nearby else "")
        + ". "
        + _basemap_attribution(tile_mode, tile_url)
    )
    mini_map = _mini_map(features, label=f"Map of assets of {record['name']}", caption=caption)
    # An organisation row carries no provenance of its own (`serialize_organization` emits []);
    # the panel shows the sources of its assets and proposals, or nothing -- never the "withheld
    # under licence" empty state, which would be a false licence claim (owner brief, 2026-09-19).
    provenance = record["provenance"] or _derived_org_provenance(assets, proposals, opportunities)
    provenance_note = (
        None
        if record["provenance"]
        else (
            "The registers behind this organisation's assets, proposals and opportunities; "
            "the organisation record itself is derived from them."
        )
    )
    descriptor = org_descriptor(record.get("type"), _org_type_counts(asset_counts, groups))
    path = f"/organizations/{record['slug'] or record['public_id']}"
    notice = nearby_notice(
        nearby_filter,
        shown=nearby_shown,
        total=nearby_total,
        path=path,
        listed_cap=ORG_NEARBY_LIMIT,
        listed_projects=len(nearby),
    )
    return templates.TemplateResponse(
        request,
        "organization_detail.html",
        {
            "record": record,
            "assets": assets,
            "asset_groups": groups,
            "summary_parts": _org_summary_parts(asset_counts, groups),
            "descriptor": descriptor,
            "canonical_path": path,
            "jsonld": [
                breadcrumb_jsonld(
                    request,
                    [("Home", "/"), ("Companies", "/organizations"), (record["name"], path)],
                ),
                organization_jsonld(
                    request,
                    record,
                    ids=entity.get("ids") if isinstance(entity.get("ids"), Mapping) else None,
                    path=path,
                    description=descriptor,
                ),
            ],
            "subsidiaries": _org_subsidiaries(entity),
            "nearby_proposals": nearby,
            "nearby_filter": nearby_filter,
            "nearby_notice": notice,
            "nearby_technologies": technology_vocabulary,
            "nearby_param": NEARBY_TECHNOLOGY_PARAM,
            "proposals": proposals,
            "opportunities": opportunities,
            "provenance_rows": provenance_panel_rows(api, provenance) if provenance else [],
            "provenance_note": provenance_note,
            "mini_map": mini_map,
            "tile_url": tile_url,
            "tile_mode": tile_mode,
        },
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request) -> HTMLResponse:
    api = get_api(request)
    q = request.query_params.get("q", "").strip()
    proposals: list[dict[str, Any]] = []
    opportunities: list[dict[str, Any]] = []
    organizations: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    if q:
        proposals_env = api.get("/v1/proposals", params={"q": q, "limit": 50})
        proposals = [flatten_proposal(e) for e in proposals_env["data"]]
        opportunities_env = api.get(
            "/v1/opportunities", params={"q": q, "limit": 50, "status": ALL_OPPORTUNITY_STATUSES_CSV}
        )
        opportunities = [flatten_opportunity(e) for e in opportunities_env["data"]]
        # ADR 0008 task item 3: an "Organisations" section on /search.
        organizations_env = api.get("/v1/organizations", params={"q": q, "limit": 50})
        organizations = [flatten_organization(e) for e in organizations_env["data"]]
        # Midstream slice: assets by name, operator or owner (`/v1/assets?q=`), so "Rockies
        # Express" (a pipeline) and "Tallgrass" (its operator) both resolve from the search box.
        try:
            assets_env = api.get("/v1/assets", params={"q": q, "limit": 50})
            for e in assets_env["data"]:
                flat = flatten_asset(e)
                flat.update(_asset_extras(e))
                flat["operator_name"] = flat["operator"].get("name")
                assets.append(flat)
        except ApiError:
            assets = []
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "proposals": proposals,
            "opportunities": opportunities,
            "organizations": organizations,
            "assets": assets,
        },
    )


@app.get("/about", response_class=HTMLResponse)
def about(request: Request) -> HTMLResponse:
    api = get_api(request)
    sources_env = api.get("/v1/sources", params={"limit": 100})
    sources = []
    for source in sources_env["data"]:
        kind = "proposal" if source["source_id"] in _PROPOSAL_SOURCE_IDS else "opportunity"
        try:
            count_env = api.get(
                ("/v1/proposals" if kind == "proposal" else "/v1/opportunities"),
                params={
                    "source_id": source["source_id"],
                    "limit": 1,
                    "include": "count",
                    **({"status": ALL_OPPORTUNITY_STATUSES_CSV} if kind == "opportunity" else {}),
                },
            )
            rows_visible = count_env["meta"].get("total", 0)
        except ApiError:
            rows_visible = None
        sources.append({**source, "kind": kind, "rows_visible": rows_visible})
    return templates.TemplateResponse(
        request,
        "about.html",
        {"sources": sources, "lag_days": get_lag_days(request)},
    )


@app.get("/attribution", response_class=HTMLResponse)
def attribution(request: Request) -> HTMLResponse:
    """Task item 4: every source the API lists, plus a Basemap section (Protomaps/OSM ODbL,
    Natural Earth, Census) and a Context layers section (EIA-860M, public domain). Reuses the same
    `/v1/sources` fetch `about()` makes rather than a second query shape."""
    api = get_api(request)
    sources_env = api.get("/v1/sources", params={"limit": 100})
    return templates.TemplateResponse(
        request,
        "attribution.html",
        {"sources": sources_env["data"]},
    )


#: docs/23 §3.1 "Detail pages visible on the public tier only; regenerated hourly with the
#: delayed view" -- "regenerated hourly" describes a would-be cache in front of this route, not
#: this route's own logic: every call here reads the live API, which already applies the delay
#: and tier gating itself (the same visibility predicate every other page in this module reads
#: through), so the sitemap is honest on every request with no separate cache of its own.
#: "capped at a sensible page count" (task brief): 25 pages of 200 rows is 5,000 URLs per
#: resource, generous for this data set's actual size (docs/adr/0008 ~7,700 active proposals) while
#: bounding one request's worst case to 100 upstream calls total across the four resources.
#: Raised from 25 on 2026-09-19 (task item 6): 25 pages capped a resource at 5,000 URLs, which
#: silently truncated assets (17.4k visible on today's dev load) and organisations (5.5k). 150
#: pages of 200 (`services/api/pagination.py::MAX_LIMIT`) is 30,000 URLs per resource, which
#: covers every resource with headroom and bounds one cold build at 600 upstream calls.
SITEMAP_MAX_PAGES_PER_RESOURCE = 150
SITEMAP_PAGE_SIZE = 200
SITEMAP_CACHE_SECONDS = 3600
#: The sitemaps protocol caps one file at 50,000 URLs and 50 MB uncompressed. 25,000 URLs is half
#: that count and, at roughly 80 bytes per `<url>`, about 2 MB -- so neither limit can be reached
#: even if a URL grows. Above one chunk, `/sitemap.xml` becomes a `<sitemapindex>` pointing at
#: `/sitemaps/{n}.xml`; at or below it, it stays the single `<urlset>` it has always been.
SITEMAP_URLS_PER_FILE = 25_000
#: Static public pages, listed first so they are in the first chunk whatever the record counts do.
SITEMAP_STATIC_PATHS = (
    "/",
    "/proposals",
    "/opportunities",
    "/assets",
    "/organizations",
    "/search",
    "/about",
    "/attribution",
    "/pricing",
)


def _sitemap_paths_for(
    api: ApiClient, path: str, url_prefix: str, *, extra_params: dict[str, str] | None = None
) -> list[str]:
    paths: list[str] = []
    cursor: str | None = None
    for _ in range(SITEMAP_MAX_PAGES_PER_RESOURCE):
        params: dict[str, str | None] = {"limit": str(SITEMAP_PAGE_SIZE), "cursor": cursor}
        params.update(extra_params or {})
        envelope = api.get(path, params=params)
        for row in envelope["data"]:
            slug = row.get("slug")
            if slug:
                paths.append(f"{url_prefix}/{slug}")
        page = envelope.get("page") or {}
        if not page.get("has_more"):
            break
        cursor = page.get("next_cursor")
        if cursor is None:
            break
    return paths


def _render_sitemap_xml(base_url: str, paths: list[str]) -> str:
    urls = "".join(f"<url><loc>{escape(base_url + p)}</loc></url>" for p in paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + urls + "</urlset>"
    )


def _render_sitemap_index_xml(base_url: str, paths: list[str]) -> str:
    entries = "".join(f"<sitemap><loc>{escape(base_url + p)}</loc></sitemap>" for p in paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + entries + "</sitemapindex>"
    )


def _build_sitemap_documents(api: ApiClient, base: str) -> dict[str, str]:
    """Every sitemap document this site serves, keyed by path, from one walk of every resource.

    Proposals, opportunities, assets and organisations, cursor-paginated per resource and capped
    (`SITEMAP_MAX_PAGES_PER_RESOURCE`). A resource whose list call errors is skipped rather than
    blanking the whole sitemap. When the URLs fit one file, `/sitemap.xml` is that `<urlset>`;
    above that it becomes a `<sitemapindex>` over `/sitemaps/{n}.xml` (`docs/23` §3.1's
    split-by-file shape), so the protocol's 50,000-URL and 50 MB limits stay out of reach.
    """
    paths: list[str] = list(SITEMAP_STATIC_PATHS)
    resources: list[tuple[str, str, dict[str, str]]] = [
        ("/v1/proposals", "/proposals", {"lifecycle_state": ALL_PROPOSAL_LIFECYCLE_STATES_CSV}),
        ("/v1/opportunities", "/opportunities", {"status": ALL_OPPORTUNITY_STATUSES_CSV}),
        ("/v1/assets", "/assets", {}),
        ("/v1/organizations", "/organizations", {}),
    ]
    for api_path, prefix, extra in resources:
        try:
            paths += _sitemap_paths_for(api, api_path, prefix, extra_params=extra)
        except (ApiError, httpx.HTTPError):
            continue  # one resource's list call failing must not blank the whole sitemap
    if len(paths) <= SITEMAP_URLS_PER_FILE:
        return {"/sitemap.xml": _render_sitemap_xml(base, paths)}
    documents: dict[str, str] = {}
    children: list[str] = []
    for number, start in enumerate(range(0, len(paths), SITEMAP_URLS_PER_FILE), start=1):
        child = f"/sitemaps/{number}.xml"
        documents[child] = _render_sitemap_xml(base, paths[start : start + SITEMAP_URLS_PER_FILE])
        children.append(child)
    documents["/sitemap.xml"] = _render_sitemap_index_xml(base, children)
    return documents


def _sitemap_documents(request: Request) -> dict[str, str]:
    """The cached `{path: xml}` for this base URL, built on a miss.

    Building costs up to 600 sequential upstream list calls (web audit 2026-09-18: an uncached
    amplifier on a public route, and a connection reset mid-way 500ed the whole response), so the
    rendered documents are cached per base URL for an hour -- the sitemap changes daily at most.
    The cache now holds every document from one walk rather than one file's XML, so a crawler
    fetching the index and then twenty child sitemaps still costs one walk, not twenty-one."""
    base = str(request.base_url).rstrip("/")
    cache: dict[str, tuple[float, dict[str, str]]] = request.app.state.__dict__.setdefault(
        "sitemap_cache", {}
    )
    cached = cache.get(base)
    if cached and time.monotonic() - cached[0] < SITEMAP_CACHE_SECONDS:
        return cached[1]
    documents = _build_sitemap_documents(get_api(request), base)
    cache[base] = (time.monotonic(), documents)
    return documents


@app.get("/sitemap.xml")
def sitemap(request: Request) -> Response:
    return Response(content=_sitemap_documents(request)["/sitemap.xml"], media_type="application/xml")


@app.get("/sitemaps/{number}.xml")
def sitemap_chunk(request: Request, number: str) -> Response:
    """One chunk of a split sitemap. Only reachable from `/sitemap.xml`'s index; a number that is
    not in the current build is a 404, never an empty `<urlset>`, which a crawler would read as
    "these URLs were removed"."""
    xml = _sitemap_documents(request).get(f"/sitemaps/{number}.xml")
    if xml is None:
        return Response(content="No such sitemap.\n", media_type="text/plain", status_code=404)
    return Response(content=xml, media_type="application/xml")


# ---- robots.txt (task item 5; none existed before 2026-09-19) ----------------------------------
#: Everything public is crawlable; these are the paths that are not. `/admin` is operator-only
#: (D-16 puts it on its own host in production, but it is mounted here in dev and a stray link
#: must never be followed), `/api/` is this site's own same-origin relay for its JavaScript rather
#: than a public API (`/v1` on the API host is the documented one), and the rest are session
#: routes: a crawler that follows them gets a form or a 401, never content. `/search?` blocks the
#: query-string form only, so the empty `/search` page in the sitemap stays crawlable while a
#: crawler cannot wander an unbounded space of result pages.
ROBOTS_DISALLOW = (
    "/admin",
    "/api/",
    "/login",
    "/register",
    "/logout",
    "/verify",
    "/account",
    "/privacy/request",
    "/unsubscribe",
    "/health",
    "/search?",
)


@app.get("/robots.txt")
def robots(request: Request) -> Response:
    """`Allow: /` first, then the disallowed prefixes: the sitemaps protocol and every major
    crawler resolve the most specific matching rule, so the order is documentation, not logic.
    The `Sitemap:` line must be absolute, so it is built from this request's own base URL and the
    file is not a static asset."""
    base = str(request.base_url).rstrip("/")
    lines = ["User-agent: *", "Allow: /"]
    lines += [f"Disallow: {path}" for path in ROBOTS_DISALLOW]
    lines += ["", f"Sitemap: {base}/sitemap.xml", ""]
    return Response(content="\n".join(lines), media_type="text/plain")


@app.get("/health")
def health(request: Request) -> dict[str, Any]:
    api = get_api(request)
    upstream = api.get("/v1/health")
    return {
        "status": upstream["status"],
        "data_as_of": upstream["data_as_of"],
        "live_as_of": upstream["live_as_of"],
        "preview_active": is_preview_active(request),
        "checked_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@app.exception_handler(ApiNotFound)
async def api_not_found_handler(request: Request, _exc: ApiNotFound) -> HTMLResponse:
    path = request.url.path
    if "/opportunities" in path:
        kind = "opportunity"
    elif "/assets" in path:
        kind = "asset"
    elif "/organizations" in path:
        kind = "organisation"
    else:
        kind = "proposal"
    return not_found_response(request, kind)
