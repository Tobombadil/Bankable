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
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import QueryParams

from web.api_client import ApiClient, ApiError, ApiNotFound, build_client
from web.assets import ASSET_VERSION
from web.auth import router as auth_router
from web.regions import Region, regions_with_data
from web.viewmodels import (
    ACTIVE_PROPOSAL_STATES,
    ALL_OPPORTUNITY_STATUSES,
    WITHDRAWN_PROPOSAL_STATES,
    WORLD_BBOX,
    flatten_opportunity,
    flatten_proposal,
    lifecycle_breakdown,
    opportunity_status_param,
    provenance_panel_rows,
    relativize_geo_feature_urls,
    resolve_proposal_lifecycle_param,
)

ALL_OPPORTUNITY_STATUSES_CSV = ",".join(ALL_OPPORTUNITY_STATUSES)
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


def querystring_without(params: QueryParams, *drop: str) -> str:
    kept = [(k, v) for k, v in params.multi_items() if k not in drop]
    return "&".join(f"{k}={v}" for k, v in kept)


def delayed_notice(request: Request, kind: str) -> dict[str, Any]:
    """docs/04 D-3/D-28: the notice always states the *actual configured* lag for the record's
    class, sourced from the API's own health payload -- never a hard-coded string (product defect
    E, task item 5). `public_at` from the API is what actually governs visibility; this notice is
    just the honest label for that, not a second filter."""
    lag_key = "supply" if kind == "proposal" else "opportunities"
    lag_days = get_lag_days(request)[lag_key]
    now = dt.datetime.now(dt.UTC)
    data_as_of = (now - dt.timedelta(days=lag_days)).strftime("%Y-%m-%d")
    return {"lag_days": lag_days, "data_as_of": data_as_of, "preview_active": is_preview_active(request)}


def not_found_response(request: Request, kind: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "not_found.html", {"kind": kind}, status_code=404)


PROPOSAL_PASSTHROUGH_FILTERS = (
    "technology",
    "jurisdiction",
    "kind",
    "capacity_mw[gte]",
    "capacity_mw[lte]",
    "q",
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

    context = {
        "records": [flatten_proposal(e) for e in envelope["data"]],
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
    return templates.TemplateResponse(
        request,
        "proposal_detail.html",
        {
            "record": record,
            "provenance_rows": provenance_panel_rows(api, record["provenance"]),
            "delayed": delayed_notice(request, "proposal"),
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

    context = {
        "records": [flatten_opportunity(e) for e in envelope["data"]],
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
    return templates.TemplateResponse(
        request,
        "opportunity_detail.html",
        {
            "record": record,
            "provenance_rows": provenance_panel_rows(api, record["provenance"]),
            "delayed": delayed_notice(request, "opportunity"),
        },
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request) -> HTMLResponse:
    api = get_api(request)
    q = request.query_params.get("q", "").strip()
    proposals: list[dict[str, Any]] = []
    opportunities: list[dict[str, Any]] = []
    if q:
        proposals_env = api.get("/v1/proposals", params={"q": q, "limit": 50})
        proposals = [flatten_proposal(e) for e in proposals_env["data"]]
        opportunities_env = api.get(
            "/v1/opportunities", params={"q": q, "limit": 50, "status": ALL_OPPORTUNITY_STATUSES_CSV}
        )
        opportunities = [flatten_opportunity(e) for e in opportunities_env["data"]]
    return templates.TemplateResponse(
        request, "search.html", {"q": q, "proposals": proposals, "opportunities": opportunities}
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
    kind = "opportunity" if "/opportunities" in request.url.path else "proposal"
    return not_found_response(request, kind)
