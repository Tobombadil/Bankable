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
import os
import re
import time
from collections.abc import Mapping
from html import escape
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import QueryParams

from web.api_client import ApiClient, ApiError, ApiNotFound
from web.auth import router as auth_router
from web.page import (
    ALL_OPPORTUNITY_STATUSES_CSV,
    ASSET_TYPE_LABELS,
    LINE_ASSET_TYPES,
    WEB_ROOT,
    _asset_feature,
    _attr,
    _basemap_attribution,
    _geometry_of,
    _line_class,
    _mini_map,
    _number,
    _proposal_feature,
    _sentence_label,
    _states_crossed,
    _tile_mode,
    _type_label,
    breadcrumb_jsonld,
    canonical_query,
    get_api,
    get_lag_days,
    group_nearby_proposals,
    is_htmx,
    is_preview_active,
    item_list_jsonld,
    not_found_response,
    querystring_without,
    templates,
)
from web.regions import Region, regions_with_data
from web.viewmodels import (
    ACTIVE_PROPOSAL_STATES,
    ALL_PROPOSAL_LIFECYCLE_STATES,
    WITHDRAWN_PROPOSAL_STATES,
    WORLD_BBOX,
    absence_note,
    asset_count_note,
    coverage_facts,
    flatten_asset,
    flatten_opportunity,
    flatten_organization,
    flatten_proposal,
    lifecycle_breakdown,
    opportunity_status_param,
    provenance_panel_rows,
    relativize_geo_feature_urls,
    resolve_proposal_lifecycle_param,
)

ALL_PROPOSAL_LIFECYCLE_STATES_CSV = ",".join(ALL_PROPOSAL_LIFECYCLE_STATES)
_PROPOSAL_SOURCE_IDS = {
    "us.iso.ercot.gen_queue",
    "us.iso.caiso.gen_queue",
    "us.iso.nyiso.gen_queue",
    "us.eia.860m",
    "gb.neso.tec_register",
}


def _region_context(region: Region) -> dict[str, Any]:
    return {"code": region.code, "label": region.label, "bbox": ",".join(str(v) for v in region.bbox)}


# ------------------------------------------------------------------ existing assets (ADR 0008;
# midstream slice, docs/00-PLAN.md 2026-09-19 option (a)).
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


app = FastAPI(title="Infraque -- public site")
app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")
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
# docs/42-backend-review-2026-09-26.md lane L2: `/organizations` and `/organizations/{ident}`,
# moved out of this module into their own router. `web/page.py` holds the `templates` instance (and
# the other page plumbing) both this module and `web/organizations.py` import, since a page router
# cannot import this module back without a cycle.
from web.organizations import router as organizations_router  # noqa: E402

app.include_router(organizations_router)

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


def get_platform_posture(request: Request) -> dict[str, str] | None:
    """The platform posture as `GET /v1/health` reports it (`posture`, `posture_statement`;
    docs/26): the setting lives on the API host, and the sentence the two public pages print is
    the API's, so a page can never claim a posture the gate is not applying. Not cached on
    `app.state` like `lag_days_default`: it is read on exactly two low-traffic pages, and a
    per-process cache is one more thing a posture flip would need restarting. `None` when the
    API predates the field, in which case the pages print nothing rather than a guess."""
    health = get_api(request).get("/v1/health")
    posture = health.get("posture")
    statement = health.get("posture_statement")
    if not isinstance(posture, str) or not isinstance(statement, str):
        return None
    return {"value": posture, "statement": statement}


#: docs/50 §3.2 web bullet ("no Open Graph or structured data") and docs/00-PLAN.md 2026-09-19
#: item 4: every public page needs a title, canonical URL and JSON-LD, which is why `SITE_NAME`
#: and the canonical/JSON-LD helpers moved to `web/page.py` -- shared with `web/organizations.py`.
SITE_NAME = "Infraque"


def delayed_notice(request: Request, kind: str) -> dict[str, Any]:
    """docs/04 D-3/D-28: the notice always states the *actual configured* lag for the record's
    class, sourced from the API's own health payload -- never a hard-coded string (product defect
    E, task item 5). `public_at` from the API is what actually governs visibility; this notice is
    just the honest label for that, not a second filter.

    Since the paywall became a matter of shape rather than time (owner, 2026-09-19) `lag_days` is
    `0` for both kinds, and since 2026-09-21 -- when the ISO change-event delay was dropped along
    with its per-source knob -- there is no delay left for the banner to name at all. It still
    reads the number from `/v1/health` rather than from a constant here, so if a delay were ever
    reintroduced the page could not disagree with the predicate that governs visibility."""
    lag_key = "supply" if kind == "proposal" else "opportunities"
    lag = get_lag_days(request)
    lag_days = lag[lag_key]
    now = dt.datetime.now(dt.UTC)
    data_as_of = (now - dt.timedelta(days=lag_days)).strftime("%Y-%m-%d")
    return {
        "lag_days": lag_days,
        "data_as_of": data_as_of,
        "preview_active": is_preview_active(request),
    }


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
    # Schedule slippage (services/api/slippage.py, docs/22 §18). Passed straight through, so an
    # unknown `slip_bucket` token surfaces the API's own 400 rather than a second, divergent
    # allowlist here.
    "slipped",
    "slip_bucket",
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
        # `.get`, not `[...]`: the schedule filter is a control the page can do without, and an
        # API deployed before this vocabulary landed must render the rest of the bar, not 500.
        "slip_buckets": vocab.get("slip_bucket", []),
        "include_withdrawn": include_withdrawn,
        "lifecycle_explicit": explicit,
        "filters": dict(qp),
        "breakdown": breakdown,
        # Only computed when the filter matched nothing: see `absence_note`'s docstring for why
        # this is not rendered beside a result that has rows.
        "absence": absence_note(coverage_facts(request, api), qp) if not records else None,
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
    count_note = asset_count_note(coverage_facts(request, api), selected_types)
    if count_note:
        count_note["label"] = _sentence_label(next(iter(selected_types)), plural=True)
    context = {
        "records": records,
        "type_counts": type_counts,
        "counts_total": sum(counts.values()) or None,
        # The corrected count for a type whose rows out-number its assets (docs/24); `None` for
        # every other view, argued in `web/viewmodels.py::asset_count_note` and the template.
        "count_note": count_note,
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
        {
            "sources": sources,
            "lag_days": get_lag_days(request),
            "posture": get_platform_posture(request),
        },
    )


#: How `source.vintage_basis` reads to someone who is not going to read the code. The four
#: values are deliberately distinct: two ways of knowing, plus "the source states none" and "we
#: have not resolved one", which are different answers and are never merged.
VINTAGE_BASIS_LABELS = {
    "artefact_filename": "the release is in the name of the file we fetch",
    "shapefile_member": "the release is in the shapefile's member names",
    "not_stated": "the source publishes no release label",
    "undetermined": "no load has resolved a release for this source",
}


@app.get("/methodology", response_class=HTMLResponse)
def methodology(request: Request) -> HTMLResponse:
    """Coverage, vintage and the status definitions, on one page.

    Everything here is served from `/v1/coverage` and `/v1/lifecycle-states`, which derive their
    numbers from the store and the pipeline's own status maps at request time. Nothing on this
    page is a figure typed into a template, because a typed figure is exactly what goes quietly
    wrong: the page this one replaces printed a single fetch date and let readers take it for the
    data's age.
    """
    api = get_api(request)
    coverage_env = api.get("/v1/coverage")
    vocabulary_env = api.get("/v1/lifecycle-states")
    data = coverage_env["data"]
    return templates.TemplateResponse(
        request,
        "methodology.html",
        {
            "coverage": data,
            "vintage": data["vintage"],
            "withheld_supply": [s for s in data["sources"]["withheld"] if s.get("supply")],
            # Sources per asset type, rows sorted descending, with the note (if any) that measured
            # what the second source does to the count -- looked up by the fact it hangs off.
            "asset_sources": sorted(
                (data.get("assets") or {}).get("by_type", {}).items(), key=lambda kv: -int(kv[1]["rows"])
            ),
            "asset_notes": {
                n["applies_to"]["unresolved_asset_type"]: n
                for n in data.get("notes") or []
                if (n.get("applies_to") or {}).get("unresolved_asset_type")
            },
            "asset_type_labels": {t: _type_label(t, plural=True) for t in ASSET_TYPE_LABELS},
            "vocabulary": vocabulary_env["data"],
            "basis_labels": VINTAGE_BASIS_LABELS,
            "posture": get_platform_posture(request),
        },
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
    "/methodology",
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
