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
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import QueryParams

from web.api_client import ApiClient, ApiError, ApiNotFound
from web.auth import router as auth_router
from web.page import (
    ALL_OPPORTUNITY_STATUSES_CSV,
    ASSET_TYPE_LABELS,
    WEB_ROOT,
    _asset_extras,
    _basemap_attribution,
    _tile_mode,
    _type_label,
    breadcrumb_jsonld,
    canonical_query,
    get_api,
    get_lag_days,
    get_platform_posture,
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
    WITHDRAWN_PROPOSAL_STATES,
    WORLD_BBOX,
    absence_note,
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

# docs/42-backend-review-2026-09-26.md lane L3: `/assets`, `/assets/{slug}` and
# `/assets/by-id/{public_id}`, moved out of this module the same way. `/api/assets/{public_id}`
# (`asset_detail_proxy`) stayed below with the other same-origin geo proxies rather than moving
# here: it shares their call shape, not this router's, and including it in this early slot would
# register it *before* `/api/assets/geo` is defined further down this module, which would make the
# parametrised route swallow that literal one (measured; see `web/assets_pages.py`'s docstring).
from web.assets_pages import router as assets_pages_router  # noqa: E402

app.include_router(assets_pages_router)

# docs/42-backend-review-2026-09-26.md lane L3: the sitemap/robots cluster -- a closed set of
# helpers and routes (§4.1) reached by nothing else in this module, so it moves as a whole with no
# path-overlap risk against any route defined above or below it.
from web.sitemaps import router as sitemaps_router  # noqa: E402

app.include_router(sitemaps_router)

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
