"""Shared page plumbing for `web/app.py` and `web/organizations.py` (docs/42-backend-review-
2026-09-26.md lane L2). `web/app.py` cannot be imported from a page router without a cycle -- it
is the module that includes that router -- so the request/template infrastructure, SEO/JSON-LD
helpers and the map/geometry presenters that more than one page module calls all live here
instead, unchanged from where `web/app.py` used to define them. `web/legal.py` and
`web/pricing.py` predate this module and each still construct their own `Jinja2Templates` and
re-register the same globals rather than import a shared one (they cannot import `web/app.py`
either, for the same reason); they are out of this lane's scope but could import this module
instead of duplicating that setup.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterable, Mapping
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import QueryParams

from web.api_client import ApiClient, build_client
from web.assets import ASSET_VERSION
from web.viewmodels import ALL_OPPORTUNITY_STATUSES
from web.viewmodels import footer_build as vm_footer_build

ALL_OPPORTUNITY_STATUSES_CSV = ",".join(ALL_OPPORTUNITY_STATUSES)

WEB_ROOT = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))


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


def querystring_without(params: QueryParams, *drop: str) -> str:
    kept = [(k, v) for k, v in params.multi_items() if k not in drop]
    return "&".join(f"{k}={v}" for k, v in kept)


def not_found_response(request: Request, kind: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "not_found.html", {"kind": kind}, status_code=404)


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
LINE_ASSET_TYPES = {"gas_pipeline", "transmission_line"}


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
