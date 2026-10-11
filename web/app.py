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
import functools
import logging
import os
import pathlib
import re
from collections.abc import Mapping
from os import PathLike
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import QueryParams
from starlette.types import Scope

from services.environment import is_dev_environment
from web.api_client import ApiClient, ApiError, ApiNotFound, VisitorIpMiddleware
from web.auth import router as auth_router
from web.empty_state import empty_result_facets, range_error
from web.head_requests import HeadAsGetMiddleware
from web.labels import PLANT_FAMILY_LABELS
from web.list_subject import list_subject, subject_hidden_fields
from web.page import (
    ALL_OPPORTUNITY_STATUSES_CSV,
    ASSET_TYPE_LABELS,
    OPPORTUNITY_PASSTHROUGH_FILTERS,
    PROPOSAL_PASSTHROUGH_FILTERS,
    WEB_ROOT,
    _asset_extras,
    _basemap_attribution,
    _tile_mode,
    _type_label,
    breadcrumb_jsonld,
    canonical_query,
    count_page_view,
    get_api,
    get_free_alerts,
    get_lag_days,
    get_platform_posture,
    is_htmx,
    is_preview_active,
    item_list_jsonld,
    not_found_response,
    paid_tiers_offered,
    querystring_without,
    save_alert_href,
    templates,
    unavailable_response,
)
from web.proposal_location import proposal_location
from web.regions import Region, regions_with_data
from web.viewmodels import (
    ACTIVE_PROPOSAL_STATES,
    ALL_PROPOSAL_LIFECYCLE_STATES,
    ORG_CREDITS_NOTE,
    PROPOSAL_SOURCE_LABELS,
    WITHDRAWN_PROPOSAL_STATES,
    WORLD_BBOX,
    absence_note,
    attach_select_basis,
    coverage_data,
    coverage_facts,
    flatten_asset,
    flatten_opportunity,
    flatten_organization,
    flatten_proposal,
    lifecycle_query_items,
    map_description,
    map_heading,
    map_labels_json,
    opportunity_kind_label,
    opportunity_status_param,
    page_credits,
    proposal_field_rows,
    proposal_fields_json,
    proposal_kind_label,
    proposal_sources_phrase,
    proposal_status_choices,
    provenance_panel_rows,
    relativize_geo_feature_urls,
    resolve_proposal_lifecycle_param,
    source_freshness,
    source_label,
    technology_label,
    with_built_href,
)

#: The sources whose rows are proposals. One list with the names the page copy uses
#: (`web/viewmodels.py::PROPOSAL_SOURCE_LABELS`), so the map header and the list's meta
#: description cannot name a different set from the one `/about` classifies.
_PROPOSAL_SOURCE_IDS = frozenset(PROPOSAL_SOURCE_LABELS)


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
    # Grid lane G2 (2026-09-28): LBNL's FERC x HIFLD transmission lines. `substation` is not listed:
    # DHS restricts the only national layer (docs/13 §2.19), so there is nothing "coming" to promise.
    ("transmission_line", "Transmission lines", True),
]


logger = logging.getLogger("web.app")

# FastAPI's own `/docs`, `/redoc` and `/openapi.json` describe the site's HTML routes, which no one
# reads as an API; nothing links them, and the two pages load scripts and fonts the site's content
# security policy refuses (docs/60 §2). Development only. `app.openapi()` itself still works.
_FRAMEWORK_DOCS = is_dev_environment()
app = FastAPI(
    title="Infraque -- public site",
    docs_url="/docs" if _FRAMEWORK_DOCS else None,
    redoc_url="/redoc" if _FRAMEWORK_DOCS else None,
    openapi_url="/openapi.json" if _FRAMEWORK_DOCS else None,
)
# Every server-side API call carries the visitor's address (web/api_client.py; devops audit F1).
app.add_middleware(VisitorIpMiddleware)
# `HEAD` on every `GET` route, page and static alike, with the `GET`'s status and headers and no
# body (web/head_requests.py). Added last, so it is the outermost of the site's own middleware.
app.add_middleware(HeadAsGetMiddleware)


class _StaticFiles(StaticFiles):
    """`/static`, with the self-hosted fonts cached for a year (frontend audit F8). The two first-
    paint fonts are `font-display: optional` (styles.css): a copy that has to be revalidated on a
    slow link misses Chrome's short wait and the page stays in the fallback face, so a font must be
    usable from the cache without a round trip. A font file is renamed whenever its content
    changes, so `immutable` is safe."""

    def file_response(
        self,
        full_path: PathLike[str] | str,
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        if str(full_path).endswith(".woff2"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.mount("/static", _StaticFiles(directory=str(WEB_ROOT / "static")), name="static")
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
# Owner decision 2026-09-30: `/alerts` -- save a view as an email alert; list, pause, resume and
# delete alerts (own router in web/alerts.py, over `/v1/saved-searches`).
from web.alerts import router as alerts_router  # noqa: E402

app.include_router(alerts_router)
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

# Grid interconnection points (owner decision 2026-09-28; docs/21 §3.24): `/interconnection-points`
# and `/interconnection-points/{public_id}`, a router of their own like the two above.
from web.interconnection_points import proposal_connection  # noqa: E402
from web.interconnection_points import router as interconnection_points_router  # noqa: E402

app.include_router(interconnection_points_router)

# Sites (owner decision 2026-10-10; docs/21 §3.25): `/sites/{public_id}` and the proposal page's panel.
from web.sites import router as sites_router  # noqa: E402

app.include_router(sites_router)

from web.coverage_statement import uncovered_iso_notes  # noqa: E402

# Audit 2026-10-07 UX-6: `/docs/api` and the development relay for `/feeds/*` (web/feeds.py).
from web.feeds import feed_href  # noqa: E402
from web.feeds import router as feeds_router  # noqa: E402

app.include_router(feeds_router)

# docs/42-backend-review-2026-09-26.md lane L3: the sitemap/robots cluster -- a closed set of
# helpers and routes (§4.1) reached by nothing else in this module, so it moves as a whole with no
# path-overlap risk against any route defined above or below it.
from web.sitemaps import router as sitemaps_router  # noqa: E402

app.include_router(sitemaps_router)

# Designer audit 2026-09-30 D-6: "Report a problem" posts here and is relayed to `/v1/reports`.
from web.reports import report_context  # noqa: E402
from web.reports import router as reports_router  # noqa: E402

app.include_router(reports_router)

# US-1001, docs/30 §4.5: "Submit a project", relayed to `/v1/intake/proposals` (web/submit.py).
from web.submit import router as submit_router  # noqa: E402

app.include_router(submit_router)

# docs/60 §2: Content-Security-Policy violation reports, the policy's `report-uri` and `report-to`.
from web.csp_reports import router as csp_reports_router  # noqa: E402

app.include_router(csp_reports_router)

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
from web.admin.sites import router as admin_sites_router  # noqa: E402

app.include_router(admin_people_router)
app.include_router(admin_ops_router)
app.include_router(admin_engagement_router)
app.include_router(admin_sites_router)


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
        # Owner decision 2026-09-30: under the noncommercial posture the notice's call to action is
        # the free alerts page, not the paid tiers (`get_free_alerts`, read from `/v1/health`).
        "free_alerts": get_free_alerts(request),
        # No "in Pro" call to action while the posture keeps the paid tiers off (owner, 2026-10-10).
        "paid_tiers_offered": paid_tiers_offered(request),
        # Whether "live" is true of the sources right now (`source_freshness`, `/v1/coverage`).
        "freshness": source_freshness(request, get_api(request)),
    }


#: The filters `lifecycle_counts` forwards, so the "Showing N active proposals" line counts the
#: same set the list shows (before 2026-09-27 it forwarded only technology, jurisdiction and kind:
#: `?q=solar&state=US-TX` listed 651 and announced 1,743).
BREAKDOWN_FILTERS = PROPOSAL_PASSTHROUGH_FILTERS

#: Lifecycle states outside both the default view and the withdrawn toggle.
OTHER_PROPOSAL_STATES: tuple[str, ...] = tuple(
    s for s in ALL_PROPOSAL_LIFECYCLE_STATES if s not in ACTIVE_PROPOSAL_STATES + WITHDRAWN_PROPOSAL_STATES
)

#: The map's own default placement (`map.js` DEFAULT_PLACEMENT). It is a drawing choice, not a
#: filter: `/v1/proposals/geo`'s `totals.records` (the map's count line) counts every placement
#: grade, so the map's notice ignores `placement` too, and the map's links to the list drop it
#: when it is this default (an unplaced proposal is listed, which is where the map sends readers).
MAP_DEFAULT_PLACEMENT = "exact,region"


def lifecycle_counts(api: ApiClient, qp: QueryParams, *, surface: str = "list") -> dict[str, int]:
    """Active / withdrawn / built / unknown counts behind the lifecycle notice, for every filter in play
    except the lifecycle one (product defect A: "so the choice is visible").

    Four `GET /v1/proposals?limit=1&include=count` calls, one per bucket. Until 2026-10-06 this
    was one world-wide clustered `GET /v1/proposals/geo` at zoom 1 whose features were thrown away
    for its `lifecycle_state_counts` (frontend audit F9): 1.0-1.4 s of every home, `/proposals`
    and map-filter request against 0.04-0.09 s per count on the same store. A failed count reads
    as zero, as the geo version did, so the page still renders."""
    params: dict[str, str] = {n: qp[n] for n in BREAKDOWN_FILTERS if qp.get(n)}
    if surface == "map":
        params.pop("placement", None)  # the map's count line counts every placement grade
    counts: dict[str, int] = {}
    # `built` and `unknown` are counted apart (UX-1) so the notice can offer the built ones, which
    # the status control shows; `other` stays their sum for the sentences that say "outside this view".
    for bucket, states in (
        ("active", ACTIVE_PROPOSAL_STATES),
        ("withdrawn", WITHDRAWN_PROPOSAL_STATES),
        ("built", ("built",)),
        ("unknown", tuple(s for s in OTHER_PROPOSAL_STATES if s != "built")),
    ):
        try:
            envelope = api.get(
                "/v1/proposals",
                params={**params, "lifecycle_state": ",".join(states), "limit": 1, "include": "count"},
            )
            counts[bucket] = int((envelope.get("meta") or {}).get("total") or 0)
        except ApiError:
            counts[bucket] = 0
    counts["other"] = counts["built"] + counts["unknown"]
    counts["total"] = counts["active"] + counts["withdrawn"] + counts["other"]
    return counts


#: An empty page of a list, for a query the page refuses to send (an impossible range).
NO_ROWS: dict[str, Any] = {
    "data": [],
    "meta": {"total": 0, "total_is_estimate": False},
    "page": {"has_more": False, "next_cursor": None, "prev_cursor": None},
}

#: The map viewport in the URL (frontend audit F7, docs/04 D-17): `center=<lng>,<lat>` and
#: `zoom=<z>`, written by map.js. The list keeps them only to hand them back to the map.
_VIEW_CENTER = re.compile(r"-?\d{1,3}(\.\d+)?,-?\d{1,2}(\.\d+)?")
_VIEW_ZOOM = re.compile(r"\d{1,2}(\.\d+)?")


def map_empty_facets(api: ApiClient, qp: QueryParams, breakdown: Mapping[str, int]) -> list[dict[str, Any]]:
    """When the map's filters match no proposal in any state, the facets that emptied it (docs/31
    §6, designer D-11): each filter in play, what removing it would show, and the map link
    without it. Empty otherwise, so a normal view costs no extra call."""
    if breakdown.get("total"):
        return []
    lifecycle_csv, _explicit, _include = resolve_proposal_lifecycle_param(qp)
    params: dict[str, str | None] = {n: qp[n] for n in BREAKDOWN_FILTERS if qp.get(n) and n != "placement"}
    params["lifecycle_state"] = lifecycle_csv
    return empty_result_facets(
        api,
        entity="proposal",
        endpoint="/v1/proposals",
        path="/",
        qp=qp,
        names=[n for n in PROPOSAL_PASSTHROUGH_FILTERS if n != "placement"],
        base_params=params,
    )


def list_view_href(qp: QueryParams) -> str:
    """`/proposals` showing the map's proposals: the same filters, plus the viewport so the list's
    "View as map" link can return to it."""
    kept = [
        (k, v)
        for k, v in qp.multi_items()
        if v
        and (k in PROPOSAL_PASSTHROUGH_FILTERS or k in ("center", "zoom"))
        and not (k == "placement" and v == MAP_DEFAULT_PLACEMENT)
    ]
    kept += lifecycle_query_items(qp)
    return "/proposals" + ("?" + urlencode(kept) if kept else "")


def map_view_href(qp: QueryParams) -> str:
    """The map (`/`) showing the same proposals as this list: every passthrough filter, the
    lifecycle choice, and the viewport when the list was reached from the map (designer D-8). The
    map honours every one of these (W3/H3: map.js forwards each passthrough filter)."""
    kept = [(k, v) for k, v in qp.multi_items() if v and k in PROPOSAL_PASSTHROUGH_FILTERS]
    kept += lifecycle_query_items(qp)
    if _VIEW_CENTER.fullmatch(qp.get("center") or "") and _VIEW_ZOOM.fullmatch(qp.get("zoom") or ""):
        kept += [("center", qp["center"]), ("zoom", qp["zoom"])]
    return "/" + ("?" + urlencode(kept) if kept else "")


def is_load_view(qp: QueryParams) -> bool:
    """A view of large loads only (`kind=load` or `technology=load`), whose rows carry no MW."""
    return qp.get("kind") == "load" or qp.get("technology") == "load"


def _proposal_params(qp: QueryParams, *, lifecycle_csv: str) -> dict[str, str | None]:
    params: dict[str, str | None] = {name: qp[name] for name in PROPOSAL_PASSTHROUGH_FILTERS if qp.get(name)}
    params["lifecycle_state"] = lifecycle_csv
    return params


def _proposal_kind_options(vocab: dict[str, Any]) -> list[tuple[str, str | None]]:
    """`(value, label)` for every proposal kind: the map and the list name kinds the same way."""
    return [(v["value"], proposal_kind_label(v["value"])) for v in vocab["proposal_kind"]]


def _technology_options(vocab: dict[str, Any]) -> list[tuple[str, str | None]]:
    """`(value, label)` for every technology the store holds; only `load` reads differently."""
    return [(v["value"], technology_label(v["value"])) for v in vocab["technology"]]


@app.get("/", response_class=HTMLResponse)
def home_map(request: Request) -> HTMLResponse:
    api = get_api(request)
    qp = request.query_params
    _lifecycle_csv, explicit, include_withdrawn = resolve_proposal_lifecycle_param(qp)
    vocab = api.get("/v1/meta/vocabularies")["data"]
    breakdown = lifecycle_counts(api, qp, surface="map")
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
            "technology_options": _technology_options(vocab),
            "kind_options": _proposal_kind_options(vocab),
            "plant_families": list(PLANT_FAMILY_LABELS.items()),
            "sources_phrase": proposal_sources_phrase(),
            # The heading and the meta description (so og:/twitter: description) from one table.
            "map_heading": map_heading(),
            "map_description": map_description(),
            # Which field rows each proposal kind cannot carry, so the drawer leaves out the rows
            # the record page leaves out (`#proposal-fields`).
            "proposal_fields_json": proposal_fields_json(),
            # map.js reads, keeps in the URL and forwards to `/api/proposals/geo` exactly these
            # names (2026-09-29: it knew four by hand and `/?kind=load` drew 5,853 proposals under
            # a notice counting 46). One list, rendered from here, so the two cannot drift.
            "passthrough_filters": PROPOSAL_PASSTHROUGH_FILTERS,
            # The words map.js prints for a token (in-view list, drawer): the server's own maps,
            # rendered into `#map-labels`, so the browser never keeps a copy of them.
            "map_labels_json": map_labels_json(),
            "include_withdrawn": include_withdrawn,
            "lifecycle_explicit": explicit,
            "status_choices": proposal_status_choices(qp),
            "built_href": with_built_href("/", qp),
            "feed_href": feed_href("proposal", qp),
            "feed_alternate": {"title": "Proposals (RSS)", "href": feed_href("proposal", qp)},
            "iso_gap_notes": uncovered_iso_notes(qp.get("iso"), coverage_data(request, api)),
            "filters": dict(qp),
            "save_alert_href": None
            if _alert_cannot_watch(qp)
            else save_alert_href("proposal", qp, origin="map"),
            # The view switch's List link before map.js runs (it rewrites it with every change).
            "list_href": list_view_href(qp),
            "breakdown": breakdown,
            "empty_facets": map_empty_facets(api, qp, breakdown),
            "active_states": ACTIVE_PROPOSAL_STATES,
            "withdrawn_states": WITHDRAWN_PROPOSAL_STATES,
            "tile_url": tile_url,
            "tile_mode": tile_mode,
            "basemap_attribution": _basemap_attribution(tile_mode, tile_url),
            "regions": regions,
            "asset_types": HOME_MAP_ASSET_TYPES,
        },
    )


@app.get("/api/proposals/notice", response_class=HTMLResponse)
def proposals_notice_fragment(request: Request) -> HTMLResponse:
    """The home map's lifecycle notice for the current filters, as the page itself renders it.

    `map.js` changes filters without a reload; it fetches this after each change so the notice
    ("Showing N active proposals ...") and the count line keep describing the same set instead of
    the notice going on describing the filters the page loaded with. Same breakdown, same
    template as `home_map`, so the wording lives in one place. Under `/api/`, which robots.txt
    disallows: a fragment is not a page.
    """
    api = get_api(request)
    qp = request.query_params
    _lifecycle_csv, explicit, include_withdrawn = resolve_proposal_lifecycle_param(qp)
    breakdown = lifecycle_counts(api, qp, surface="map")
    return templates.TemplateResponse(
        request,
        "_map_notice.html",
        {
            "breakdown": breakdown,
            "include_withdrawn": include_withdrawn,
            "lifecycle_explicit": explicit,
            "built_href": with_built_href("/", qp),
            "iso_gap_notes": uncovered_iso_notes(qp.get("iso"), coverage_data(request, api)),
            "empty_facets": map_empty_facets(api, qp, breakdown),
        },
    )


@app.get("/map")
def map_alias(request: Request) -> RedirectResponse:
    """`/map` is a permanent alias of the map at `/` (docs/04 D-16), so it answers 301, not the
    default 307 (audit 2026-09-30 F15), and keeps the view's query string."""
    query = request.url.query
    return RedirectResponse(url="/" + (f"?{query}" if query else ""), status_code=301)


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
    `asset_type`, `technology`, `status` only, no cookies -- `web/static/js/map.js`'s assets layer fetches
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
    # Lane R1: the map's existing-plants layer leaves retired plants out, and its "Retired &
    # retiring plants" layer asks for exactly those two statuses.
    if qp.get("status"):
        params["status"] = qp["status"]
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


#: The counters `map.js` sends (`services/db/models.py::UI_EVENT_NAMES`, the `map.*` names).
BROWSER_UI_EVENTS = frozenset({"map.layer_toggled", "map.region_jumped", "map.basemap_failed"})


@app.post("/api/ui-events")
async def ui_events_proxy(request: Request) -> JSONResponse:
    """Same-origin proxy for `POST /v1/ui-events` (task contract): forwards only `name`/`props`
    (never cookies or headers, never the caller's IP/UA to the API beyond what any HTTP request
    already carries at the transport level) and always answers 202, whether or not the API call
    behind it succeeds -- measurement must never be able to break the map page. `map.js` posts
    here with `navigator.sendBeacon` (a `Blob`, so the request may arrive without a JSON
    content-type; Starlette's `Request.json()` parses the body regardless).

    Only the map's own events are relayed (`BROWSER_UI_EVENTS`). This relay calls the API with the
    site's service identity, so relaying `page.viewed` would let any visitor inflate the page-view
    count the API accepts only from the site; `auth.registered` and `alert.created` are recorded by
    the servers that see the registration and the saved search, never by a browser (2026-09-30)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("name") if isinstance(body, dict) else None
    props = body.get("props") if isinstance(body, dict) and isinstance(body.get("props"), dict) else {}
    if isinstance(name, str) and name in BROWSER_UI_EVENTS:
        api = get_api(request)
        try:
            api.post("/v1/ui-events", json={"name": name, "props": props})
        except Exception:  # noqa: S110 -- measurement must never block or surface an error here
            pass
    return JSONResponse({}, status_code=202)


def _alert_cannot_watch(qp: QueryParams) -> bool:
    """Whether the view uses a list filter a saved search cannot carry yet: `listed`, or a
    `sponsor_scope` wider than the default `self` (`services/api/records.py::PROPOSAL_VIEW_FILTERS`;
    the alert matcher does not evaluate them)."""
    return bool(qp.get("listed")) or (qp.get("sponsor_scope") or "self").strip().lower() != "self"


@app.get("/proposals", response_class=HTMLResponse)
def proposals_list(request: Request) -> HTMLResponse:
    api = get_api(request)
    qp = request.query_params
    lifecycle_csv, explicit, include_withdrawn = resolve_proposal_lifecycle_param(qp)
    params = _proposal_params(qp, lifecycle_csv=lifecycle_csv)
    # A large-load view has no capacity to sort on or print: no load record carries MW (0 of 578,
    # lane L11), and "sorted by capacity" over a blank column read as a defect. It sorts by name
    # and drops the column (docs/30 §5.3 note); any other view keeps capacity, largest first.
    load_view = is_load_view(qp)
    params["sort"] = qp.get("sort") or ("name_canonical" if load_view else "-capacity_mw")
    params["cursor"] = qp.get("cursor")
    params["include"] = "count"
    # docs/31 §5.9: an impossible capacity range is said inline, not sent to the API and answered
    # with an unexplained "no proposals" (designer audit D-11).
    capacity_error = range_error(qp, "capacity_mw[gte]", "capacity_mw[lte]", noun="capacity")
    envelope = NO_ROWS if capacity_error else api.get("/v1/proposals", params=params)
    vocab = api.get("/v1/meta/vocabularies")["data"]
    # No lifecycle counts for an impossible range either: "Showing 0 active proposals" would read
    # as a fact about the register rather than about the query.
    breakdown = None if capacity_error else lifecycle_counts(api, qp)

    records = [flatten_proposal(e) for e in envelope["data"]]
    empty_facets = (
        empty_result_facets(
            api,
            entity="proposal",
            endpoint="/v1/proposals",
            path="/proposals",
            qp=qp,
            names=PROPOSAL_PASSTHROUGH_FILTERS,
            base_params=params,
        )
        if not records and not capacity_error and not qp.get("cursor")
        else []
    )
    canonical_path = "/proposals" + canonical_query(
        qp, (*PROPOSAL_PASSTHROUGH_FILTERS, "include_withdrawn", "sort", "cursor")
    )
    context = {
        "records": records,
        # UX-4: the registers behind the rows on this page, from the API's own licence summary.
        "credits": page_credits(envelope),
        "load_view": load_view,
        "sort_caption": "sorted by name, A to Z" if load_view else "sorted by capacity, largest first",
        "total": envelope["meta"].get("total"),
        "total_is_estimate": envelope["meta"].get("total_is_estimate", False),
        "has_more": envelope["page"]["has_more"],
        "next_cursor": envelope["page"]["next_cursor"],
        "prev_cursor": envelope["page"]["prev_cursor"],
        "querystring": querystring_without(qp, "cursor"),
        # No "save as alert" for a view an alert cannot watch yet (records.PROPOSAL_VIEW_FILTERS):
        # an alert without the filter would watch more, or other, proposals than the list shows.
        "save_alert_href": None if _alert_cannot_watch(qp) else save_alert_href("proposal", qp),
        "delayed": delayed_notice(request, "proposal"),
        # What the list is about (one company, its group, or one connection point), named in the
        # heading with a link back to its page; the filter form keeps it (web/list_subject.py). Not
        # read for an htmx swap, which replaces the rows and leaves the heading as it is.
        "subject": None if is_htmx(request) else list_subject(api, qp, envelope["data"]),
        "subject_fields": subject_hidden_fields(qp),
        "technology_options": _technology_options(vocab),
        "kind_options": _proposal_kind_options(vocab),
        "sources_phrase": proposal_sources_phrase(),
        # `.get`, not `[...]`: the schedule filter is a control the page can do without, and an
        # API deployed before this vocabulary landed must render the rest of the bar, not 500.
        "slip_buckets": vocab.get("slip_bucket", []),
        "include_withdrawn": include_withdrawn,
        "lifecycle_explicit": explicit,
        "status_choices": proposal_status_choices(qp),
        "built_href": with_built_href("/proposals", qp),
        "feed_href": feed_href("proposal", qp),
        "feed_alternate": {"title": "These proposals (RSS)", "href": feed_href("proposal", qp)},
        # `?iso=PJM` and the like: say the rows are EIA-860M units, not that ISO's queue.
        "iso_gap_notes": uncovered_iso_notes(qp.get("iso"), coverage_data(request, api)),
        "filters": dict(qp),
        "breakdown": breakdown,
        # Only computed when the filter matched nothing: see `absence_note`'s docstring for why
        # this is not rendered beside a result that has rows.
        "absence": (
            absence_note(coverage_facts(request, api), qp) if not records and not capacity_error else None
        ),
        "empty_facets": empty_facets,
        "capacity_error": capacity_error,
        # Designer audit D-8: "View as: Map · List" beside the results, each carrying the query.
        "map_href": map_view_href(qp),
        "list_href": "/proposals"
        + ("?" + urlencode([(k, v) for k, v in qp.multi_items() if k != "cursor"]) if qp else ""),
        "view_center": qp.get("center") if _VIEW_CENTER.fullmatch(qp.get("center") or "") else None,
        "view_zoom": qp.get("zoom") if _VIEW_ZOOM.fullmatch(qp.get("zoom") or "") else None,
        "canonical_path": canonical_path,
        "jsonld": [
            item_list_jsonld(
                request,
                name="Proposals",
                description=(
                    "Interconnection queue, generator and data-centre proposals matching these filters."
                ),
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


def _survivor_slug(api: ApiClient, collection: str, segment: str) -> str | None:
    """Where a detail URL that names no current record now lives (US-201 AC3; QA audit 2026-09-30
    QA-8). The API answers a merged record's old id or old slug with `301` to its survivor, and
    `404` when the survivor is not visible at this tier (so its id never reaches this page). Both
    transports follow that redirect (`web/api_client.py::build_client`), so a 200 here is the
    survivor's envelope; its slug differs from `segment` exactly when the visitor should be sent on.
    A live record's own public id resolves here too and lands on its slug URL."""
    try:
        envelope = api.get(f"/v1/{collection}/{quote(segment, safe='')}")
    except ApiNotFound:
        return None
    slug = (envelope.get("data") or {}).get("slug")
    return slug if isinstance(slug, str) and slug and slug != segment else None


def _not_listed(entity: Mapping[str, Any]) -> dict[str, Any] | None:
    """`{"since": <delisted_at or None>}` when the API says no register lists the record any more
    (`listed: false`), else `None`; a response without the field is read as listed."""
    if entity.get("listed") is not False:
        return None
    return {"since": entity.get("delisted_at")}


@app.get("/proposals/{slug}", response_class=HTMLResponse)
def proposal_detail(request: Request, slug: str) -> Response:
    api = get_api(request)
    entity = _resolve_proposal_by_slug(api, slug)
    if entity is None:
        if survivor := _survivor_slug(api, "proposals", slug):
            return RedirectResponse(f"/proposals/{quote(survivor, safe='')}", status_code=301)
        return not_found_response(request, "proposal")
    from web.viewmodels import proposal_history, with_composition  # local: off the shared import block

    record = flatten_proposal(with_composition(api, entity))
    path = f"/proposals/{record['slug']}"
    connection = proposal_connection(api, record.get("public_id"))
    response = templates.TemplateResponse(
        request,
        "proposal_detail.html",
        {
            "record": record,
            # No register lists it any more (API `listed: false`): the status is the last one stated.
            "not_listed": _not_listed(entity),
            "history": proposal_history(api, record),
            "provenance_rows": attach_select_basis(record, provenance_panel_rows(api, record["provenance"])),
            "connection": connection,
            # The field-grid rows this record's kind can carry (web/viewmodels.py
            # `PROPOSAL_FIELDS_NOT_APPLICABLE`): a Class VI well shows no MW, ISO or grid rows.
            "fields": proposal_field_rows(record, connected=bool(connection)),
            "location": proposal_location(entity),
            "delayed": delayed_notice(request, "proposal"),
            "canonical_path": path,
            "report": report_context(record["public_id"], record["name"], path),
            "jsonld": [
                breadcrumb_jsonld(
                    request, [("Home", "/"), ("Proposals", "/proposals"), (record["name"], path)]
                )
            ],
        },
    )
    return count_page_view(request, response, "proposal")


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
    empty_facets = (
        empty_result_facets(
            api,
            entity="opportunity",
            endpoint="/v1/opportunities",
            path="/opportunities",
            qp=qp,
            names=OPPORTUNITY_PASSTHROUGH_FILTERS,
            base_params=params,
        )
        if not records and not qp.get("cursor")
        else []
    )
    canonical_path = "/opportunities" + canonical_query(
        qp, (*OPPORTUNITY_PASSTHROUGH_FILTERS, "status", "cursor")
    )
    context = {
        "records": records,
        "credits": page_credits(envelope),  # UX-4
        "feed_href": feed_href("opportunity", qp),  # UX-6
        "feed_alternate": {"title": "These opportunities (RSS)", "href": feed_href("opportunity", qp)},
        "total": envelope["meta"].get("total"),
        "total_is_estimate": envelope["meta"].get("total_is_estimate", False),
        "has_more": envelope["page"]["has_more"],
        "next_cursor": envelope["page"]["next_cursor"],
        "prev_cursor": envelope["page"]["prev_cursor"],
        "querystring": querystring_without(qp, "cursor"),
        "save_alert_href": save_alert_href("opportunity", qp),
        "delayed": delayed_notice(request, "opportunity"),
        "kind_options": [(v["value"], opportunity_kind_label(v["value"])) for v in vocab["opportunity_kind"]],
        "statuses": [v["value"] for v in vocab["opportunity_status"]],
        # The opportunity vocabulary, not the proposal one: `solar` matches no tagged notice.
        "technologies": [v["value"] for v in vocab.get("opportunity_technology", [])],
        "filters": {**dict(qp), "status": qp.get("status", "open")},
        "empty_facets": empty_facets,
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
def opportunity_detail(request: Request, slug: str) -> Response:
    api = get_api(request)
    entity = _resolve_opportunity_by_slug(api, slug)
    if entity is None:
        if survivor := _survivor_slug(api, "opportunities", slug):
            return RedirectResponse(f"/opportunities/{quote(survivor, safe='')}", status_code=301)
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
            "report": report_context(record["public_id"], record["title"], path),
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
    credits: dict[str, list[dict[str, Any]]] = {}
    if q:
        proposals_env = api.get("/v1/proposals", params={"q": q, "limit": 50})
        credits["proposals"] = page_credits(proposals_env)
        proposals = [flatten_proposal(e) for e in proposals_env["data"]]
        opportunities_env = api.get(
            "/v1/opportunities", params={"q": q, "limit": 50, "status": ALL_OPPORTUNITY_STATUSES_CSV}
        )
        opportunities = [flatten_opportunity(e) for e in opportunities_env["data"]]
        credits["opportunities"] = page_credits(opportunities_env)
        # ADR 0008 task item 3: an "Organisations" section on /search.
        organizations_env = api.get("/v1/organizations", params={"q": q, "limit": 50})
        organizations = [flatten_organization(e) for e in organizations_env["data"]]
        # Midstream slice: assets by name, operator or owner (`/v1/assets?q=`), so "Rockies
        # Express" (a pipeline) and "Tallgrass" (its operator) both resolve from the search box.
        try:
            assets_env = api.get("/v1/assets", params={"q": q, "limit": 50})
            # `/v1/assets` sends an empty licence summary; each row carries its provenance.
            credits["assets"] = page_credits(assets_env, assets_env["data"])
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
            "credits": credits,
            "org_credits_note": ORG_CREDITS_NOTE,
        },
    )


@app.get("/about", response_class=HTMLResponse)
def about(request: Request) -> HTMLResponse:
    """The source register, the tiers and the posture sentence. If the register itself cannot be
    read the whole page is the 503 `api_unavailable_handler` renders; if only `/v1/health` fails,
    the page renders and the posture line says the status is unavailable (`get_platform_posture`)."""
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
    posture = get_platform_posture(request)
    response = templates.TemplateResponse(
        request,
        "about.html",
        {
            "sources": sources,
            "lag_days": get_lag_days(request),
            "posture": posture,
            "free_alerts": get_free_alerts(request),
        },
    )
    if posture is not None and posture.get("available") is False:
        response.headers["Cache-Control"] = "no-store"  # a cache must not keep the degraded version
    return response


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
            # Source ids name a register in the status maps and the asset table; a reader sees
            # its short name (or the manifest's name), never the id (audit 2026-09-30, F1).
            "source_names": methodology_source_names(),
            "basis_labels": VINTAGE_BASIS_LABELS,
            "posture": get_platform_posture(request),
        },
    )


#: The registers the loader geocodes against (`services/ingest/geocode.py`): their names place
#: records on the map, so their credit is owed wherever that placement appears, although they are
#: not `source` rows of their own (L-2 of the 2026-09-30 legal audit: NESO's gazetteer carries the
#: same mandatory statement as its TEC register).
GEOCODING_GAZETTEER_IDS = (
    "gb.neso.fes_gsp_gazetteer",
    "gb.ons.ipn_gazetteer",
    "us.census.cartographic_boundaries",
)
_MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "data" / "sources.yaml"


#: Registers the status maps name by key alone, because no connector for them is loaded yet
#: (`services/api/lifecycle.py` gives these a `status_key` and no `source_id`).
STATUS_KEY_NAMES = {"isone": "ISO-NE", "spp": "SPP", "miso": "MISO", "pjm": "PJM"}


@functools.lru_cache(maxsize=1)
def methodology_source_names() -> dict[str, str]:
    """`{source_id or status_key: name}` for the methodology page's status-map lists and asset
    table: the short name a list prints (`source_label`) where there is one, else the manifest's
    `name`. A reader sees "CAISO" or "Texas RRC Class VI permits", never `us.tx.rrc.class_vi`
    (audit 2026-09-30, F1)."""
    import yaml

    names: dict[str, str] = dict(STATUS_KEY_NAMES)
    for entry in (yaml.safe_load(_MANIFEST.read_text(encoding="utf-8")) or {}).get("sources") or []:
        source_id = str(entry.get("id") or "")
        if source_id:
            names[source_id] = source_label(source_id) or str(entry.get("name") or source_id)
    return names


@functools.lru_cache(maxsize=1)
def geocoding_gazetteer_credits() -> tuple[dict[str, str], ...]:
    """Name, operator, credit and licence link of each gazetteer, read from `data/sources.yaml`
    (the manifest's `attribution` verbatim, else "Source: <operator>"). A file, not the store:
    nothing about a gazetteer is loaded into a table the API could serve."""
    import yaml

    entries = {
        str(e.get("id")): e for e in (yaml.safe_load(_MANIFEST.read_text(encoding="utf-8")) or {})["sources"]
    }
    out: list[dict[str, str]] = []
    for source_id in GEOCODING_GAZETTEER_IDS:
        entry = entries.get(source_id)
        if entry is None:
            continue
        operator = str(entry.get("operator") or entry.get("name") or source_id)
        out.append(
            {
                "source_id": source_id,
                "name": str(entry.get("name") or source_id),
                "url": str(entry.get("url") or ""),
                "credit": str(entry.get("attribution") or f"Source: {operator}"),
                "licence_url": str(entry.get("licence_url") or ""),
            }
        )
    return tuple(out)


@app.get("/attribution", response_class=HTMLResponse)
def attribution(request: Request) -> HTMLResponse:
    """Task item 4: every source the API lists, plus a Basemap section (Protomaps/OSM ODbL,
    Natural Earth, Census), a Context layers section (EIA-860M, public domain) and the geocoding
    gazetteers (`geocoding_gazetteer_credits`). Reuses the same `/v1/sources` fetch `about()`
    makes rather than a second query shape."""
    api = get_api(request)
    sources_env = api.get("/v1/sources", params={"limit": 100})
    return templates.TemplateResponse(
        request,
        "attribution.html",
        {"sources": sources_env["data"], "gazetteers": geocoding_gazetteer_credits()},
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


@app.exception_handler(ApiError)
async def api_unavailable_handler(request: Request, exc: ApiError) -> Response:
    """Any page whose data call is refused for capacity (429: the shared anonymous bucket when
    `API_INTERNAL_TOKEN` is unset or wrong) or fails upstream (5xx) renders the 503 "temporarily
    unavailable" page instead of a 500 (docs/60 §11 item 9, measured 2026-09-26 on /about's
    `/v1/sources` call). Routes that can do without one call keep catching `ApiError` themselves;
    this is the floor under the rest. Other 4xx statuses are a bug in the page, not an outage, and
    stay a 500. `ApiNotFound` keeps its own handler below (Starlette picks the closest class)."""
    if exc.status_code != 429 and exc.status_code < 500:
        raise exc
    logger.warning("api unavailable", extra={"path": request.url.path, "api_status": exc.status_code})
    return unavailable_response(request)


@app.exception_handler(httpx.HTTPError)
async def api_unreachable_handler(request: Request, exc: httpx.HTTPError) -> Response:
    """The API did not answer at all (refused, timed out): the same 503 page."""
    logger.warning("api unreachable", extra={"path": request.url.path, "error": type(exc).__name__})
    return unavailable_response(request)


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
