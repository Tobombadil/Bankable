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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import QueryParams

from web.api_client import ApiClient, ApiError, ApiNotFound, build_client
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

WEB_ROOT = Path(__file__).resolve().parent

app = FastAPI(title="Infraqueue (placeholder) -- public site")
app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")
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
