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
import time
from collections.abc import Mapping, Sequence
from os import PathLike
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import QueryParams
from starlette.types import Scope

from services.environment import is_dev_environment
from web import record_tabs
from web.api_client import ApiClient, ApiError, ApiNotFound, VisitorIpMiddleware
from web.auth import router as auth_router
from web.empty_state import empty_result_facets, range_error
from web.formatting import readable_name
from web.head_requests import HeadAsGetMiddleware
from web.headline import LANDINGS, Landing, figures_sentence, landing_for, opening_figures, shows_figures
from web.labels import PLANT_FAMILY_LABELS, SECTION_LABELS
from web.legal import controller_identity
from web.list_sort import (
    OPPORTUNITY_COLUMNS,
    OPPORTUNITY_OTHER,
    PROPOSAL_COLUMNS,
    PROPOSAL_OTHER,
    chosen_sort,
    sort_caption,
    sort_headers,
    valid_sort,
)
from web.list_subject import list_subject, subject_hidden_fields
from web.organizations import ORG_DEFAULT_SCOPE, ORG_SCOPE_PARAM, org_index_row
from web.page import (
    ALL_OPPORTUNITY_STATUSES_CSV,
    ASSET_TYPE_LABELS,
    MY_PIPELINE_ENV,
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
from web.unlisted import listed_param, unlisted_notice, unlisted_query_items, unlisted_toggle
from web.viewmodels import (
    ACTIVE_PROPOSAL_STATES,
    ALL_PROPOSAL_LIFECYCLE_STATES,
    OPPORTUNITY_PAGE_DEFAULT_STATUS,
    ORG_CREDITS_NOTE,
    PROPOSAL_SOURCE_LABELS,
    RECENTLY_CLOSED_LIMIT,
    WITHDRAWN_PROPOSAL_STATES,
    WORLD_BBOX,
    absence_note,
    attach_select_basis,
    coverage_data,
    coverage_facts,
    flatten_asset,
    flatten_opportunity,
    flatten_proposal,
    lifecycle_query_items,
    map_description,
    map_heading,
    map_labels_json,
    no_status_chosen,
    opportunity_kind_label,
    opportunity_status_chosen,
    opportunity_status_form_value,
    opportunity_status_param,
    opportunity_status_phrase,
    page_credits,
    proposal_field_rows,
    proposal_fields_json,
    proposal_kind_label,
    proposal_sources_phrase,
    proposal_status_choices,
    provenance_panel_rows,
    recently_closed_window,
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
    title="Inframation -- public site",
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


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    """Pages declare their icons in base.html, but feed readers, crawlers and the admin pages ask for
    /favicon.ico directly, which answered 404 (review of lane w1-nav, docs/16 U-24)."""
    return FileResponse(
        WEB_ROOT / "static" / "img" / "favicon.ico",
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=604800"},
    )


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
# Owner decision 2026-10-22: the home map's search field, `/api/map/search`, which finds a project,
# a company or a place for map.js to move the map to (own router in web/map_search.py), and the
# panel's busiest areas (`busiest_areas`, served by `map_areas` below beside the geo proxy).
from web.map_search import busiest_areas  # noqa: E402
from web.map_search import router as map_search_router  # noqa: E402

app.include_router(map_search_router)
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

# Energy prices, slice 1 (docs/29; owner decision 2026-10-22): `/prices`, hubs and zones and EIA's
# state retail averages, read from `/v1/prices/*` (own router in web/prices.py).
from web.prices import router as prices_router  # noqa: E402

app.include_router(prices_router)

# The site finder (docs/25): `/site-finder`, its two fragments and its two map proxies.
from web.site_finder import router as site_finder_router  # noqa: E402

app.include_router(site_finder_router)

# My pipeline, the Tracker (decision rows 2026-10-23; docs/19 §6.2; docs/30 §11.4-§11.5): `/my`,
# `/my/items/...` and the Track block's global (`track_offer`), all off until `MY_PIPELINE=on`.
from web.my_projects import router as my_projects_router  # noqa: E402

app.include_router(my_projects_router)

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

# Private beta (docs/65): "Tell us what you were trying to do", relayed to `/v1/reports` as
# `beta_feedback` (web/feedback.py). Both routes answer 404 unless SITE_ACCESS=basic (web/beta.py).
from web.feedback import router as feedback_router  # noqa: E402

app.include_router(feedback_router)

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
SITE_NAME = "Inframation"


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


def lifecycle_counts(
    api: ApiClient, qp: QueryParams, *, surface: str = "list", active_total: int | None = None
) -> dict[str, int]:
    """Active / withdrawn / built / unknown counts behind the lifecycle notice, for every filter in play
    except the lifecycle one (product defect A: "so the choice is visible").

    Four `GET /v1/proposals?limit=1&include=count` calls, one per bucket. Until 2026-10-06 this
    was one world-wide clustered `GET /v1/proposals/geo` at zoom 1 whose features were thrown away
    for its `lifecycle_state_counts` (frontend audit F9): 1.0-1.4 s of every home, `/proposals`
    and map-filter request against 0.04-0.09 s per count on the same store. A failed count reads
    as zero, as the geo version did, so the page still renders.

    `active_total`: the list's own total when its view is the default (active states, the same
    filters and the same `listed`), which is the active bucket's answer, so it is not asked twice.
    The call that saves pays for the hidden-records count (`web/unlisted.py`), so the default list
    makes as many calls as it did before records no register lists were hidden."""
    params: dict[str, str] = {n: qp[n] for n in BREAKDOWN_FILTERS if qp.get(n)}
    if surface == "map":
        params.pop("placement", None)  # the map's count line counts every placement grade
    counts: dict[str, int] = {}
    # A view that hides the records no register lists any more (`web/unlisted.py`) counts its active
    # bucket the same way, so "Showing N active proposals" is the number of rows under it. The other
    # buckets count states outside the view, and a view showing any of them hides nothing.
    hidden = listed_param(qp)
    # `built` and `unknown` are counted apart (UX-1) so the notice can offer the built ones, which
    # the status control shows; `other` stays their sum for the sentences that say "outside this view".
    for bucket, states in (
        ("active", ACTIVE_PROPOSAL_STATES),
        ("withdrawn", WITHDRAWN_PROPOSAL_STATES),
        ("built", ("built",)),
        ("unknown", tuple(s for s in OTHER_PROPOSAL_STATES if s != "built")),
    ):
        if bucket == "active" and active_total is not None:
            counts[bucket] = active_total
            continue
        try:
            envelope = api.get(
                "/v1/proposals",
                params={
                    **params,
                    **(hidden if bucket == "active" else {}),
                    "lifecycle_state": ",".join(states),
                    "limit": 1,
                    "include": "count",
                },
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
    params.update(listed_param(qp))
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
    kept += lifecycle_query_items(qp) + unlisted_query_items(qp)
    return "/proposals" + ("?" + urlencode(kept) if kept else "")


def map_view_href(qp: QueryParams) -> str:
    """The map (`/`) showing the same proposals as this list: every passthrough filter, the
    lifecycle choice, and the viewport when the list was reached from the map (designer D-8). The
    map honours every one of these (W3/H3: map.js forwards each passthrough filter)."""
    kept = [(k, v) for k, v in qp.multi_items() if v and k in PROPOSAL_PASSTHROUGH_FILTERS]
    kept += lifecycle_query_items(qp) + unlisted_query_items(qp)
    if _VIEW_CENTER.fullmatch(qp.get("center") or "") and _VIEW_ZOOM.fullmatch(qp.get("zoom") or ""):
        kept += [("center", qp["center"]), ("zoom", qp["zoom"])]
    return "/" + ("?" + urlencode(kept) if kept else "")


def is_load_view(qp: QueryParams) -> bool:
    """A view of large loads only (`kind=load` or `technology=load`), whose rows carry no MW."""
    return qp.get("kind") == "load" or qp.get("technology") == "load"


def _proposal_params(qp: QueryParams, *, lifecycle_csv: str) -> dict[str, str | None]:
    """The API query for the list's rows and the map's geo and cluster reads: the passthrough
    filters, the lifecycle choice and, on a view of active states only, `listed=true`
    (`web/unlisted.py`). The web-only `unlisted` toggle is never among them."""
    params: dict[str, str | None] = {name: qp[name] for name in PROPOSAL_PASSTHROUGH_FILTERS if qp.get(name)}
    params["lifecycle_state"] = lifecycle_csv
    params.update(listed_param(qp))
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
    no_status = no_status_chosen(qp)
    vocab = api.get("/v1/meta/vocabularies")["data"]
    breakdown = lifecycle_counts(api, qp, surface="map")
    tile_url = (os.environ.get("MAP_TILE_URL") or "").strip() or None
    tile_mode = _tile_mode(tile_url)
    # docs/40 §2.7 / task item 3: only regions a published, non-gated source actually covers get a
    # jump button -- reusing the same `/v1/sources` fetch `about()` already makes, filtered
    # server-side to `publish_state=public` so a gated-but-listed source never counts.
    sources_env = api.get("/v1/sources", params={"limit": 100, "publish_state": "public"})
    regions = [_region_context(r) for r in regions_with_data(sources_env["data"])]
    # The opening figures and the preview card's words (web/headline.py): on the plain map and on a
    # landing such as `/texas`, never on a view someone filtered.
    landing = landing_for(qp)
    figures = opening_figures(api, landing, breakdown["active"]) if shows_figures(qp) else []
    lead = figures_sentence(figures, landing)
    return templates.TemplateResponse(
        request,
        "home_map.html",
        {
            "delayed": delayed_notice(request, "proposal"),
            "landing": landing,
            "headline_figures": figures,
            "page_title": f"{landing.name} energy projects, large loads and RFPs | Inframation"
            if landing
            else "Inframation: planned energy projects, large loads and RFPs",
            "technology_options": _technology_options(vocab),
            "kind_options": _proposal_kind_options(vocab),
            "plant_families": list(PLANT_FAMILY_LABELS.items()),
            "sources_phrase": proposal_sources_phrase(),
            # The heading and the meta description (so og:/twitter: description) from one table.
            "map_heading": f"{map_heading()} in {landing.name}" if landing else map_heading(),
            "map_description": f"{lead} {map_description()}" if lead else map_description(),
            # Which field rows each proposal kind cannot carry, so the drawer leaves out the rows
            # the record page leaves out (`#proposal-fields`).
            "proposal_fields_json": proposal_fields_json(),
            # map.js reads, keeps in the URL and forwards to `/api/proposals/geo` exactly these
            # names (2026-09-29: it knew four by hand and `/?kind=load` drew 5,853 proposals under
            # a notice counting 46). One list, rendered from here, so the two cannot drift.
            "passthrough_filters": PROPOSAL_PASSTHROUGH_FILTERS,
            # The words map.js prints for a token (in-view panel, drawer): the server's own maps,
            # rendered into `#map-labels`, so the browser never keeps a copy of them.
            "map_labels_json": map_labels_json(),
            "include_withdrawn": include_withdrawn,
            "lifecycle_explicit": explicit,
            "status_choices": proposal_status_choices(qp),
            # Owner, 2026-10-11: records no register lists any more are hidden from a view of active
            # states; the box and the line that says how many (`web/unlisted.py`).
            "unlisted_toggle": unlisted_toggle(qp),
            "unlisted_notice": unlisted_notice(api, qp, BREAKDOWN_FILTERS, path="/", surface="map"),
            "built_href": with_built_href("/", qp),
            # Owner, 2026-10-22: the map has no view switch. Its feed is offered on the page "Save
            # this search as an alert" opens (web/alerts.py) and stays discoverable here.
            "feed_alternate": {"title": "Proposals (RSS)", "href": feed_href("proposal", qp)},
            "iso_gap_notes": uncovered_iso_notes(qp.get("iso"), coverage_data(request, api)),
            "filters": dict(qp),
            # Every Status box unticked (`lifecycle_state=none`): no proposal is drawn, the count, the
            # notice and the panel say so from first paint, and the list and save links wait for a
            # status (map.js).
            "no_status": no_status,
            "save_alert_href": None
            if _alert_cannot_watch(qp)
            else save_alert_href("proposal", qp, origin="map"),
            # "Open these in the list" before map.js runs (it rewrites it with every change).
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


def _landing_redirect(landing: Landing) -> Any:
    def redirect(request: Request) -> RedirectResponse:
        """A short link for an email: the map filtered to and framed on one place. Tracking tags on
        the short link (`utm_*`) travel with it; nothing else does."""
        tags = [(k, v) for k, v in request.query_params.multi_items() if k.startswith("utm_") and v]
        return RedirectResponse(landing.href + ("&" + urlencode(tags) if tags else ""), status_code=302)

    return redirect


for _landing in LANDINGS:
    app.add_api_route(
        f"/{_landing.slug}", _landing_redirect(_landing), methods=["GET"], include_in_schema=False
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
            "unlisted_notice": unlisted_notice(api, qp, BREAKDOWN_FILTERS, path="/", surface="map"),
            "iso_gap_notes": uncovered_iso_notes(qp.get("iso"), coverage_data(request, api)),
            "empty_facets": map_empty_facets(api, qp, breakdown),
            "no_status": no_status_chosen(qp),
        },
    )


#: `/api/proposals/geo`'s answer to `lifecycle_state=none` (`proposals_geo_proxy`).
NO_STATUS_GEO: dict[str, Any] = {
    "data": {
        "type": "FeatureCollection",
        "features": [],
        "totals": {"records": 0, "clustered": False, "lifecycle_state_counts": {}},
    },
    "meta": {},
}


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
    if no_status_chosen(qp):
        # Every Status box unticked: nothing to draw and nothing to ask the API (owner report
        # 2026-10-22). The empty answer keeps the API's shape, so map.js draws and counts it as any.
        return JSONResponse(NO_STATUS_GEO)
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


@app.get("/api/map/areas")
def map_areas(request: Request) -> JSONResponse:
    """The busiest states (or countries) in a view, for the "In view" panel's overview at national and
    multi-state zoom (owner, 2026-10-22; `web/map_search.py::busiest_areas`). The same query as
    `proposals_geo_proxy`, so the projects counted are the ones the map draws."""
    qp = request.query_params
    if no_status_chosen(qp):
        return JSONResponse({"areas": [], "total": 0})
    lifecycle_csv, _explicit, _include = resolve_proposal_lifecycle_param(qp)
    params = _proposal_params(qp, lifecycle_csv=lifecycle_csv)
    params["bbox"] = qp.get("bbox") or WORLD_BBOX
    return JSONResponse(busiest_areas(get_api(request), params))


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
    the alert matcher does not evaluate them). Only a `listed` the link itself names counts: the
    default view's hiding of records no register lists (`web/unlisted.py`) is not in the query
    string, so the default view keeps its alert, which watches every record (owner, 2026-10-11)."""
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
    # Lane list-sort (2026-10-22): the headings sort on a click (`web/list_sort.py`); a token no heading or
    # API field sorts by falls back to the default rather than reaching the API as a 400.
    sort = valid_sort(
        qp.get("sort"), PROPOSAL_COLUMNS, "name_canonical" if load_view else "-capacity_mw", PROPOSAL_OTHER
    )
    params["sort"] = sort
    params["cursor"] = qp.get("cursor")
    params["include"] = "count"
    # docs/31 §5.9: an impossible capacity range is said inline, not sent to the API and answered
    # with an unexplained "no proposals" (designer audit D-11).
    capacity_error = range_error(qp, "capacity_mw[gte]", "capacity_mw[lte]", noun="capacity")
    envelope = NO_ROWS if capacity_error else api.get("/v1/proposals", params=params)
    vocab = api.get("/v1/meta/vocabularies")["data"]
    # No lifecycle counts for an impossible range either: "Showing 0 active proposals" would read
    # as a fact about the register rather than about the query. The default view's own total is
    # the notice's active count (`lifecycle_counts`' `active_total`).
    total = envelope["meta"].get("total")
    default_view = not explicit and not include_withdrawn and isinstance(total, int)
    breakdown = (
        None if capacity_error else lifecycle_counts(api, qp, active_total=total if default_view else None)
    )

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
        "sort_caption": sort_caption(sort, PROPOSAL_COLUMNS, PROPOSAL_OTHER),
        "sort_headers": sort_headers("/proposals", qp, PROPOSAL_COLUMNS, sort),
        # Kept by the filter form, so a filter change keeps the order the reader chose.
        "sort_param": chosen_sort(qp.get("sort"), PROPOSAL_COLUMNS, PROPOSAL_OTHER),
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
        # Owner, 2026-10-11: the box that shows records no register lists any more, and the line
        # saying how many this view hides (`web/unlisted.py`); no count for an impossible range.
        "unlisted_toggle": unlisted_toggle(qp),
        "unlisted_notice": None
        if capacity_error
        else unlisted_notice(api, qp, BREAKDOWN_FILTERS, path="/proposals"),
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
    """A project's Overview, the record's own URL (docs/30 §11.5)."""
    return _proposal_page(request, slug, "overview")


@app.get("/proposals/{slug}/{tab}", response_class=HTMLResponse)
def proposal_tab(request: Request, slug: str, tab: str) -> Response:
    """A project's Timeline, Sources or (once matches publish) Matches tab (docs/30 §11.5, §11.10)."""
    if tab == "overview":
        return record_tabs.overview_redirect(request, f"/proposals/{quote(slug, safe='')}")
    return _proposal_page(request, slug, tab)


#: The project's list in its breadcrumb (docs/30 §11.3: "Market / Projects / {name}").
_PROPOSAL_TRAIL: tuple[tuple[str, str], ...] = ((SECTION_LABELS["projects"], "/proposals"),)


def _proposal_page(request: Request, slug: str, tab: str) -> Response:
    """One tab of a project (web/record_tabs.py): `?tab=` answers 301 to its segment, an unknown segment
    is the not-found page, a merged project's old slug answers 301 to the survivor on the same tab, and
    each tab reads only what it shows beside the header every tab shares (T-10)."""
    moved = record_tabs.query_tab_redirect(
        request, f"/proposals/{quote(slug, safe='')}", record_tabs.PROPOSAL_TABS, tab
    )
    if moved is not None:
        return moved
    if tab != "overview" and not record_tabs.is_tab(record_tabs.PROPOSAL_TABS, tab):
        return not_found_response(request, "page")
    api = get_api(request)
    entity = _resolve_proposal_by_slug(api, slug)
    if entity is None:
        if survivor := _survivor_slug(api, "proposals", slug):
            return record_tabs.moved_redirect(request, "proposals", survivor, tab)
        return not_found_response(request, "proposal")
    from web.viewmodels import proposal_history, with_composition  # local: off the shared import block

    record = flatten_proposal(with_composition(api, entity))
    path = f"/proposals/{quote(record['slug'], safe='')}"
    tabs = record_tabs.Tabs(
        "proposal",
        path,
        record_tabs.PROPOSAL_TABS,
        # Matches stay hidden while matches are withheld from readers (IA-9; US-401 AC3).
        present={"timeline": True, "sources": True, "matches": False},
        current=tab,
        counts={"sources": record_tabs.source_count(record["provenance"])},
    )
    location = proposal_location(entity)
    name = readable_name(record["name"])
    context: dict[str, Any] = {
        "record": record,
        "head": {
            "type": "proposal",
            "crumbs": record_tabs.crumbs(tabs, _PROPOSAL_TRAIL, name),
            "name": name,
            "chip": {
                "state": record.get("lifecycle_state"),
                "family": record.get("lifecycle_family"),
                "raw": record.get("status_raw"),
            },
            "facts": record_tabs.proposal_facts(record, proposal_field_rows(record)),
            "action": record_tabs.primary_action(
                request,
                tabs,
                "proposal",
                record,
                ("Show on map", location.get("map_href") if location else None),
                ("View at source", record.get("source_url")),
            ),
        },
        # No register lists it any more (API `listed: false`): the status is the last one stated.
        "not_listed": _not_listed(entity),
        "delayed": delayed_notice(request, "proposal"),
        "credits": record_tabs.credits(record["provenance"]),
        "canonical_path": tabs.href(tab),
    }
    if tab == "overview":
        connection = proposal_connection(api, record.get("public_id"))
        context.update(
            connection=connection,
            # The field-grid rows this record's kind can carry (web/viewmodels.py
            # `PROPOSAL_FIELDS_NOT_APPLICABLE`): a Class VI well shows no MW, ISO or grid rows.
            fields=proposal_field_rows(record, connected=bool(connection)),
            location=location,
            # Why each register counts a large load as a data centre: the claim a reader checks first,
            # so it stays on the Overview as well as in its Sources row.
            select_basis=[
                {"source": row.get("source_name"), "line": row["select_basis_line"]}
                for row in attach_select_basis(record, [dict(p) for p in record["provenance"]])
                if row.get("select_basis_line")
            ],
            timeline=record_tabs.proposal_timeline(record, proposal_history(api, record)),
            jsonld=[
                breadcrumb_jsonld(request, record_tabs.jsonld_trail(_PROPOSAL_TRAIL, record["name"], path))
            ],
        )
    elif tab == "timeline":
        context["timeline"] = record_tabs.proposal_timeline(record, proposal_history(api, record))
    elif tab == "sources":
        context.update(
            provenance_rows=attach_select_basis(record, provenance_panel_rows(api, record["provenance"])),
            report=report_context(record["public_id"], record["name"], tabs.href("sources")),
        )
    response = record_tabs.render(
        request, f"proposal_{'detail' if tab == 'overview' else tab}.html", context, tabs
    )
    # One view of the record per visit (PM-5): the Overview counts, its tabs do not.
    return count_page_view(request, response, "proposal") if tabs.is_overview else response


#: docs/16 U-7 (decision 2026-10-22): RFPs & funding opens on the notices
#: `services/match/relevance.py` judges energy-relevant (API `relevance=energy`); `?relevance=all`
#: is the "All notices" switch. Any other value reads as the default, never as a 400.
OPPORTUNITY_RELEVANCE_ALL = "all"


def opportunity_relevance(qp: QueryParams) -> str:
    return "all" if (qp.get("relevance") or "").strip().lower() == OPPORTUNITY_RELEVANCE_ALL else "energy"


def _opportunity_total(api: ApiClient, params: Mapping[str, str | None]) -> int | None:
    """`meta.total` for `params` (one `limit=1&include=count` call), or `None` if the API cannot say."""
    query = {k: v for k, v in params.items() if k not in ("cursor", "sort", "include") and v is not None}
    try:
        envelope = api.get("/v1/opportunities", params={**query, "limit": 1, "include": "count"})
        return int(envelope["meta"].get("total") or 0)
    except (ApiError, httpx.HTTPError, KeyError, TypeError, ValueError):
        return None


def _opportunity_exists(api: ApiClient, params: Mapping[str, str | None]) -> bool | None:
    """Whether `params` select at least one notice (one `limit=1` call, no count query), or `None`
    if the API cannot say."""
    query = {k: v for k, v in params.items() if k not in ("cursor", "sort", "include") and v is not None}
    try:
        return bool(api.get("/v1/opportunities", params={**query, "limit": 1})["data"])
    except (ApiError, httpx.HTTPError, KeyError, TypeError):
        return None


def opportunity_kind_options(
    api: ApiClient, params: Mapping[str, str | None], kinds: Sequence[str], selected: str | None
) -> list[tuple[str, str | None]]:
    """The Kind filter's options, `(value, label)`: each kind with at least one notice in this view,
    every other filter kept (2026-10-22: the RFP kind was offered with no open notice behind it, so
    picking it emptied the list). The kind in force stays offered either way. One `limit=1` call per
    kind; if any cannot answer, every kind is offered, as before. No count in the label: the select
    sits outside the rows an htmx filter change swaps, so a number there would go stale."""
    base = {k: v for k, v in params.items() if k != "kind"}
    present = {kind: _opportunity_exists(api, {**base, "kind": kind}) for kind in kinds}
    if any(found is None for found in present.values()):
        return [(kind, opportunity_kind_label(kind)) for kind in kinds]
    return [(kind, opportunity_kind_label(kind)) for kind in kinds if present[kind] or kind == selected]


def _relevance_switch(qp: QueryParams, relevance: str, totals: Mapping[str, int | None]) -> dict[str, Any]:
    """The "Energy-related · All notices" links over the rows: each the current view in that scope,
    without the page cursor, with its count."""
    kept = [(k, v) for k, v in qp.multi_items() if k not in ("relevance", "cursor") and v != ""]
    return {
        "current": relevance,
        "energy": {
            "href": "/opportunities" + ("?" + urlencode(kept) if kept else ""),
            "count": totals["energy"],
        },
        "all": {"href": "/opportunities?" + urlencode([*kept, ("relevance", "all")]), "count": totals["all"]},
    }


def _closed_list_href(qp: QueryParams) -> str:
    """The full closed list for this view (lane rfp-view): the reader's filters and scope with
    `status=closed`, newest deadline first, without the page cursor."""
    kept = [(k, v) for k, v in qp.multi_items() if k not in ("status", "sort", "cursor") and v != ""]
    return "/opportunities?" + urlencode([*kept, ("status", "closed"), ("sort", "-due_at")])


#: The reader's filters the "Recently closed" section leaves out: the deadline window is its own.
_RECENTLY_CLOSED_OWN = ("status", "sort", "cursor", "include", "due_at[from]", "due_at[to]")


def _recently_closed(
    api: ApiClient, params: Mapping[str, str | None], qp: QueryParams
) -> dict[str, Any] | None:
    """The section "Recently closed (last 12 months)" under the list (lane rfp-view): closed notices
    in this view's scope and filters whose deadline fell in the last 365 days, newest first, at most
    `RECENTLY_CLOSED_LIMIT`, with the count and a link to the full closed list. One more list call.
    `None` when there are none, or when the API cannot say: the section is extra, so a failure leaves
    the page as it was rather than failing it. A row the API answers with any other status is
    dropped, so the section can never show an open notice as closed or the reverse."""
    start, end = recently_closed_window()
    query: dict[str, Any] = {
        k: v for k, v in params.items() if k not in _RECENTLY_CLOSED_OWN and v is not None
    }
    query.update(
        {
            "status": "closed",
            "due_at[from]": start,
            "due_at[to]": end,
            "sort": "-due_at",
            "limit": RECENTLY_CLOSED_LIMIT,
            "include": "count",
        }
    )
    try:
        envelope = api.get("/v1/opportunities", params=query)
    except (ApiError, httpx.HTTPError):
        return None
    rows = [flatten_opportunity(e) for e in envelope.get("data") or [] if e.get("status") == "closed"]
    if not rows:
        return None
    total = (envelope.get("meta") or {}).get("total")
    return {
        "records": rows,
        "total": total if isinstance(total, int) and total >= len(rows) else len(rows),
        "href": _closed_list_href(qp),
        # The registers behind these rows, from the rows themselves: the envelope's summary would
        # also credit the ECB for a converted value the compact rows do not print.
        "credits": page_credits(rows),
    }


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities_list(request: Request) -> HTMLResponse:
    api = get_api(request)
    qp = request.query_params
    status_param = opportunity_status_param(qp)
    status_chosen = opportunity_status_chosen(qp)
    relevance = opportunity_relevance(qp)
    sort = valid_sort(qp.get("sort"), OPPORTUNITY_COLUMNS, "due_at", OPPORTUNITY_OTHER)
    params: dict[str, str | None] = {
        name: qp[name] for name in OPPORTUNITY_PASSTHROUGH_FILTERS if qp.get(name)
    }
    params["status"] = status_param
    if relevance == "energy":
        params["relevance"] = "energy"
    params["sort"] = sort
    params["cursor"] = qp.get("cursor")
    params["include"] = "count"
    envelope = api.get("/v1/opportunities", params=params)
    vocab = api.get("/v1/meta/vocabularies")["data"]
    # The other scope's count, for the switch: "Energy-related 307 · All notices 624".
    other = {k: v for k, v in params.items() if k != "relevance"}
    if relevance == "all":
        other["relevance"] = "energy"
    total = envelope["meta"].get("total")
    other_total = _opportunity_total(api, other)
    relevance_totals = {relevance: total, ("all" if relevance == "energy" else "energy"): other_total}

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
    # Lane rfp-view: while the reader has not chosen a status, the first page ends with the closed
    # notices of the last 12 months, in a section of their own (`_recently_closed`).
    recently_closed = (
        _recently_closed(api, params, qp) if not status_chosen and not qp.get("cursor") else None
    )
    # The all-notices view is not a duplicate of the energy view, so it is its own canonical URL; an
    # explicit `relevance=energy` (or any other value) is the default page and names no scope, and so
    # is the default status named in the URL (`status=open,announced`, what the Status select
    # submits untouched). `sort` is kept as on /proposals and /assets: a cursor only means something
    # under its order.
    canonical_path = "/opportunities" + canonical_query(
        qp,
        (
            *OPPORTUNITY_PASSTHROUGH_FILTERS,
            *(("status",) if status_chosen else ()),
            *(("relevance",) if relevance == OPPORTUNITY_RELEVANCE_ALL else ()),
            "sort",
            "cursor",
        ),
    )
    context = {
        "records": records,
        "credits": page_credits(envelope),  # UX-4
        "feed_href": feed_href("opportunity", qp),  # UX-6
        "feed_alternate": {"title": "These opportunities (RSS)", "href": feed_href("opportunity", qp)},
        "total": total,
        "total_is_estimate": envelope["meta"].get("total_is_estimate", False),
        "has_more": envelope["page"]["has_more"],
        "next_cursor": envelope["page"]["next_cursor"],
        "prev_cursor": envelope["page"]["prev_cursor"],
        "querystring": querystring_without(qp, "cursor"),
        "save_alert_href": save_alert_href("opportunity", qp),
        "delayed": delayed_notice(request, "opportunity"),
        "relevance": relevance,
        "relevance_switch": _relevance_switch(qp, relevance, relevance_totals),
        "sort_caption": sort_caption(sort, OPPORTUNITY_COLUMNS, OPPORTUNITY_OTHER),
        "sort_headers": sort_headers("/opportunities", qp, OPPORTUNITY_COLUMNS, sort),
        "sort_param": chosen_sort(qp.get("sort"), OPPORTUNITY_COLUMNS, OPPORTUNITY_OTHER),
        # The filter form is not part of an htmx swap, so its options are built for a full page only.
        "kind_options": []
        if is_htmx(request)
        else opportunity_kind_options(
            api, params, [v["value"] for v in vocab["opportunity_kind"]], qp.get("kind")
        ),
        "statuses": [v["value"] for v in vocab["opportunity_status"]],
        # The opportunity vocabulary, not the proposal one: `solar` matches no tagged notice.
        "technologies": [v["value"] for v in vocab.get("opportunity_technology", [])],
        "filters": {**dict(qp), "status": opportunity_status_form_value(qp)},
        # Lane rfp-view: the page's default status, whether the reader chose another, the words the
        # standfirst uses for it, the recently closed section and the empty state's way on.
        "status_default": OPPORTUNITY_PAGE_DEFAULT_STATUS,
        "status_chosen": status_chosen,
        "status_phrase": opportunity_status_phrase(qp),
        "recently_closed": recently_closed,
        "closed_href": _closed_list_href(qp),
        "reader_filters": any(qp.get(name) for name in OPPORTUNITY_PASSTHROUGH_FILTERS),
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
                    [("Home", "/"), (SECTION_LABELS["rfps"], "/opportunities"), (record["title"], path)],
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


# ---- Search (Navigation v2 lane V7; docs/30 §11.4, docs/16 §4.4 U-8) ---------------------------------
#: The type tabs after "All", in order: companies first (the owner's "companies on top"), then the
#: Market lists in their bar's order, then public money. RFPs are buyers asking for projects and Funding
#: is public money offered (docs/30 §11.2); a notice goes where its kind says, the split `/funding` makes
#: in lane V4. "RFPs" is typed here because the section label still reads "RFPs & funding" until then.
SEARCH_TABS: tuple[tuple[str, str], ...] = (
    ("companies", SECTION_LABELS["companies"]),
    ("projects", SECTION_LABELS["projects"]),
    ("infrastructure", SECTION_LABELS["infrastructure"]),
    ("rfps", "RFPs"),
    ("funding", SECTION_LABELS["funding"]),
)
SEARCH_TAB_KEYS = frozenset(key for key, _ in SEARCH_TABS)
SEARCH_RFP_KINDS = "rfp,tender,auction,procurement_notice"
SEARCH_FUNDING_KINDS = "foa,loan_program,program"
#: Rows per bucket on "All" (docs/16 §4.4: "five rows per bucket ... each with All N"), and on a type tab.
SEARCH_ALL_ROWS = 5
SEARCH_TAB_ROWS = 50
#: `GET /v1/assets` takes no `include=count`, so Infrastructure always reads one page of 50 and says "50+"
#: when there is more, on every tab: a count of "5+" would say nothing.
SEARCH_ASSET_ROWS = 50
#: A top hit needs at least this many letters or digits of query: "so" would make every name a match.
SEARCH_TOP_HIT_MIN = 3


def _search_key(text: Any) -> str:
    """A name or query compared as the API compares names: case, spaces and punctuation ignored."""
    return re.sub(r"[^0-9a-z]+", "", str(text or "").lower())


def _search_bucket(
    key: str,
    label: str,
    rows: list[dict[str, Any]],
    envelope: Mapping[str, Any] | None,
    *,
    list_href: str,
    q: str,
    credits: list[dict[str, Any]] | None = None,
    read: int | None = None,
) -> dict[str, Any]:
    """One tab's results: its rows, its total and how the tab prints it, and the list that holds them all.

    The total is the API's count where the route counts (`include=count`); otherwise the rows read, with
    a "+" when the page says there are more (`read`: how many were read, when more were read than shown).
    Each tab and "All N" link carries the same query, and the full list link reproduces exactly the set
    counted, so a figure never names more than its link shows.
    """
    meta = (envelope or {}).get("meta") or {}
    page = (envelope or {}).get("page") or {}
    total = meta.get("total") if isinstance(meta.get("total"), int) else None
    more = total is None and bool(page.get("has_more"))
    count = total if total is not None else (len(rows) if read is None else read)
    return {
        "key": key,
        "label": label,
        "rows": rows,
        "total": count,
        "count_text": f"{count:,}+" if more else f"{count:,}",
        "more": more or count > len(rows),
        "href": "/search?" + urlencode({"q": q, "type": key}),
        "list_href": list_href,
        "credits": credits or [],
        "error": envelope is None,
    }


def _search_read(api: ApiClient, path: str, params: dict[str, Any]) -> Mapping[str, Any] | None:
    """One bucket's read. A refusal for capacity, an upstream failure or a list route this API does not
    serve costs that bucket only (it says it could not be searched), as the assets bucket always did; any
    other client error is a bug in this page and stays loud."""
    try:
        return api.get(path, params=params)
    except ApiError as exc:
        if exc.status_code not in (404, 429) and exc.status_code < 500:
            raise
        logger.warning("search bucket unavailable", extra={"path": path, "api_status": exc.status_code})
        return None


def search_top_hit(
    q: str, organizations: Sequence[Mapping[str, Any]], assets: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """The one result a name search most likely meant (docs/16 §4.4): the first company, else the first
    asset, whose name starts with the query, ignoring case, spaces and punctuation. Companies come first
    because a name search is most often for one (the owner's "companies on top"). None for a query too
    short to mean a name, or when no name starts with it."""
    key = _search_key(q)
    if len(key) < SEARCH_TOP_HIT_MIN:
        return None
    for entity in organizations:
        if _search_key(entity.get("name_canonical") or entity.get("name")).startswith(key):
            return {"kind": "company", "record": org_index_row(entity, scope=ORG_DEFAULT_SCOPE)}
    for asset in assets:
        if _search_key(asset.get("name")).startswith(key):
            return {"kind": "asset", "record": asset}
    return None


@app.get("/search", response_class=HTMLResponse)
def search(request: Request) -> HTMLResponse:
    """One field, five kinds of record (docs/30 §11.4): tabs with counts, a top hit, five rows per kind on
    "All" and fifty on a kind's own tab, each with its sources credited. `type` picks the tab; an unknown
    one is "All". Five reads per search, whichever tab: the tabs print every kind's count."""
    api = get_api(request)
    qp = request.query_params
    q = qp.get("q", "").strip()
    view = qp.get("type", "").strip().lower()
    view = view if view in SEARCH_TAB_KEYS else "all"
    if not q:
        return templates.TemplateResponse(request, "search.html", {"q": "", "view": "all", "buckets": []})

    def limit(key: str) -> int:
        return SEARCH_TAB_ROWS if view == key else SEARCH_ALL_ROWS

    # Companies: the group's figures, as the companies index shows them by default.
    org_env = _search_read(
        api,
        "/v1/organizations",
        {"q": q, "limit": limit("companies"), "include": "count", ORG_SCOPE_PARAM: ORG_DEFAULT_SCOPE},
    )
    org_entities = list((org_env or {}).get("data") or [])
    companies = [org_index_row(e, scope=ORG_DEFAULT_SCOPE) for e in org_entities]
    # Projects in every lifecycle state: the API's default, and the full list's link names all of them so
    # it is not narrowed to the list's own default (active states, listed records).
    projects_env = _search_read(
        api, "/v1/proposals", {"q": q, "limit": limit("projects"), "include": "count"}
    )
    projects = [flatten_proposal(e) for e in (projects_env or {}).get("data") or []]
    # Midstream slice: assets by name, operator or owner (`/v1/assets?q=`), so "Rockies Express" (a
    # pipeline) and "Tallgrass" (its operator) both resolve from the search box.
    assets_env = _search_read(api, "/v1/assets", {"q": q, "limit": SEARCH_ASSET_ROWS})
    assets: list[dict[str, Any]] = []
    for e in (assets_env or {}).get("data") or []:
        flat = flatten_asset(e)
        flat.update(_asset_extras(e))
        flat["operator_name"] = flat["operator"].get("name")
        assets.append(flat)

    def notices(key: str, kinds: str) -> tuple[Mapping[str, Any] | None, list[dict[str, Any]]]:
        params = {"q": q, "limit": limit(key), "include": "count", "status": ALL_OPPORTUNITY_STATUSES_CSV}
        env = _search_read(api, "/v1/opportunities", {**params, "kind": kinds})
        return env, [flatten_opportunity(e) for e in (env or {}).get("data") or []]

    rfps_env, rfps = notices("rfps", SEARCH_RFP_KINDS)
    funding_env, funding = notices("funding", SEARCH_FUNDING_KINDS)

    every_state = ",".join(ALL_PROPOSAL_LIFECYCLE_STATES)
    every_notice = {"q": q, "status": "all", "relevance": OPPORTUNITY_RELEVANCE_ALL}
    tab_labels = dict(SEARCH_TABS)
    shown = SEARCH_TAB_ROWS if view != "all" else SEARCH_ALL_ROWS
    buckets = [
        _search_bucket(
            "companies",
            tab_labels["companies"],
            companies,
            org_env,
            list_href="/organizations?" + urlencode({"q": q}),
            q=q,
        ),
        _search_bucket(
            "projects",
            tab_labels["projects"],
            projects,
            projects_env,
            list_href="/proposals?" + urlencode({"q": q, "lifecycle_state": every_state}),
            q=q,
            credits=page_credits(projects_env) if projects_env else [],
        ),
        _search_bucket(
            "infrastructure",
            tab_labels["infrastructure"],
            assets[:shown],
            assets_env,
            list_href="/assets?" + urlencode({"q": q}),
            q=q,
            # `/v1/assets` sends an empty licence summary; each row carries its provenance.
            credits=page_credits(assets_env, assets_env["data"]) if assets_env else [],
            read=len(assets),
        ),
        _search_bucket(
            "rfps",
            tab_labels["rfps"],
            rfps,
            rfps_env,
            list_href="/opportunities?" + urlencode({**every_notice, "kind": SEARCH_RFP_KINDS}),
            q=q,
            credits=page_credits(rfps_env) if rfps_env else [],
        ),
        _search_bucket(
            "funding",
            tab_labels["funding"],
            funding,
            funding_env,
            list_href="/opportunities?" + urlencode({**every_notice, "kind": SEARCH_FUNDING_KINDS}),
            q=q,
            credits=page_credits(funding_env) if funding_env else [],
        ),
    ]
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "view": view,
            "buckets": buckets,
            "current": next((b for b in buckets if b["key"] == view), None),
            "top_hit": search_top_hit(q, org_entities, assets) if view == "all" else None,
            "org_credits_note": ORG_CREDITS_NOTE,
        },
    )


# ---- About, About & sources (Navigation v2 lane V7; docs/30 §11.4, §11.10) -------------------------------
#: Where a register reaches, US first (owner, 2026-10-23: "our primary focus should be the USA"; Great
#: Britain, EU and World Bank coverage stays and stays reachable). The id is the region's anchor on
#: `/about/sources`, so a figure on `/about` links to exactly the table it counts.
SOURCE_REGIONS: tuple[tuple[str, str], ...] = (
    ("united-states", "United States"),
    ("great-britain", "Great Britain"),
    ("europe", "Europe"),
    ("international", "International"),
)
_EUROPE_CODES = frozenset(
    {"EU", "DE", "IE", "FR", "ES", "IT", "NL", "BE", "DK", "SE", "NO", "FI", "PL", "PT", "AT", "CH", "CZ"}
)
#: A register's `category` in words, for the line under each region on `/about`.
SOURCE_CATEGORY_WORDS: dict[str, str] = {
    "generation_queue": "interconnection queues",
    "load_queue": "large-load queues",
    "permit": "permits",
    "regulatory_docket": "regulatory dockets",
    "funding": "funding notices",
    "procurement": "RFPs and tenders",
    "registry": "asset registers",
    "planning": "grid plans",
    "market_price": "prices",
    "land": "land records",
    "aggregator": "compilations",
}
#: Registers whose rows are RFPs, tenders or funding notices (counted on `/opportunities`).
_NOTICE_CATEGORIES = frozenset({"procurement", "funding"})
#: Sources that are not registers of projects, notices, assets or prices: news feeds and the site's own
#: social channels. `/about` never counts them as a region's registers. `/about/sources` lists the news
#: feeds in a section of their own after the regions (`NEWS_SECTION`) and leaves the channels out: they
#: are where the site posts, not where a fact comes from (`/attribution` still lists every source).
NON_REGISTER_CATEGORIES = frozenset({"news", "social_channel"})
NEWS_SECTION: tuple[str, str] = ("news", "News")
#: A global layer loaded only for the United States ("Global (US clipped)", data/sources.yaml) reaches the US.
_US_CLIPPED = re.compile(r"\bUS[ -]clipped\b", re.IGNORECASE)
#: How long `/about/sources` keeps a register's record count: the counts move once per load, and the page
#: makes one count read per published register (`_source_record_counts`), so it keeps them per process as
#: the sitemap and the Infrastructure type counts are kept.
SOURCE_RECORDS_CACHE_SECONDS = 900


def source_region(source: Mapping[str, Any]) -> str:
    """The region a register reaches, from its `jurisdiction` ("US-TX", "US (conterminous)", "GB",
    "EU/EEA", "global"): the first two letters name the country or the union, and a global layer the US
    clips is the United States. A register with no jurisdiction (YAML reads Norway's `NO` as false, so
    `no.sodir.factpages_co2_storage` arrives blank) is placed by its id's country segment when that is a
    two-letter code. Anything else is International, World Bank and global registers included."""
    raw = source.get("jurisdiction")
    text = raw.strip() if isinstance(raw, str) else ""
    if _US_CLIPPED.search(text):
        return "united-states"
    if not text:
        prefix = str(source.get("source_id") or "").split(".", 1)[0]
        text = prefix if len(prefix) == 2 else ""
    code = text[:2].upper()
    if code == "US":
        return "united-states"
    if code == "GB":
        return "great-britain"
    if code in _EUROPE_CODES:
        return "europe"
    return "international"


def _all_sources(api: ApiClient) -> list[dict[str, Any]]:
    """Every register `/v1/sources` lists, following the cursor (180 in `data/sources.yaml` on
    2026-10-23; the page used to read the first 100 only)."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(20):  # a guard against a paging bug, not a limit: 4,000 registers
        envelope = api.get("/v1/sources", params={"limit": 200, "cursor": cursor})
        out.extend(envelope.get("data") or [])
        page = envelope.get("page") or {}
        cursor = page.get("next_cursor")
        if not page.get("has_more") or not cursor:
            break
    return out


def _published(source: Mapping[str, Any]) -> bool:
    return source.get("publish_state") == "public"


def _is_register(source: Mapping[str, Any]) -> bool:
    """Whether a source is a register a region counts: not a news feed or one of the site's channels."""
    return source.get("category") not in NON_REGISTER_CATEGORIES


def _posture_response(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
    """A reference page that prints the posture sentence: never cached while the status is unavailable."""
    posture = context.get("posture")
    response = templates.TemplateResponse(request, template, context)
    if isinstance(posture, Mapping) and posture.get("available") is False:
        response.headers["Cache-Control"] = "no-store"  # a cache must not keep the degraded version
    return response


@app.get("/about", response_class=HTMLResponse)
def about(request: Request) -> HTMLResponse:
    """What Inframation is, where it reaches (US first), what a reader can do here, the tiers and the
    posture sentence, and who runs it. The register and its licences are their own page
    (`/about/sources`); this one reads the register once, to count what each region publishes. If the
    register cannot be read the whole page is the 503 `api_unavailable_handler` renders; if only
    `/v1/health` fails, the page renders and the posture line says the status is unavailable."""
    api = get_api(request)
    sources = _all_sources(api)
    regions = []
    for region_id, name in SOURCE_REGIONS:
        published = [
            s for s in sources if _published(s) and _is_register(s) and source_region(s) == region_id
        ]
        if not published:
            continue  # no empty figure (owner, 2026-10-21)
        kinds: dict[str, int] = {}
        for s in published:
            category = str(s.get("category") or "other")
            words = SOURCE_CATEGORY_WORDS.get(category, category.replace("_", " "))
            kinds[words] = kinds.get(words, 0) + 1
        regions.append(
            {
                "id": region_id,
                "name": name,
                "count": len(published),
                "href": f"/about/sources#{region_id}",
                "kinds": [k for k, _ in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))][:4],
            }
        )
    return _posture_response(
        request,
        "about.html",
        {
            "regions": regions,
            "posture": get_platform_posture(request),
            "free_alerts": get_free_alerts(request),
            "pipeline_on": os.environ.get(MY_PIPELINE_ENV, "").strip().lower() == "on",
            "controller": controller_identity(),
        },
    )


def _source_records(api: ApiClient, source: Mapping[str, Any]) -> tuple[int | None, str | None]:
    """How many records a published register backs on the public tier, and the list that shows exactly
    those: projects in every lifecycle state, or notices of every status and relevance. Registers of
    other kinds (asset registers, prices, context layers) are counted on their own pages: `(None, None)`."""
    source_id = str(source["source_id"])
    if source_id in _PROPOSAL_SOURCE_IDS:
        path, params = "/v1/proposals", {"source_id": source_id}
        href = "/proposals?" + urlencode(
            {"source_id": source_id, "lifecycle_state": ",".join(ALL_PROPOSAL_LIFECYCLE_STATES)}
        )
    elif source.get("category") in _NOTICE_CATEGORIES:
        path, params = "/v1/opportunities", {"source_id": source_id, "status": ALL_OPPORTUNITY_STATUSES_CSV}
        href = "/opportunities?" + urlencode(
            {"source_id": source_id, "status": "all", "relevance": OPPORTUNITY_RELEVANCE_ALL}
        )
    else:
        return None, None
    try:
        envelope = api.get(path, params={**params, "limit": 1, "include": "count"})
    except ApiError:
        return None, None
    total = (envelope.get("meta") or {}).get("total")
    return (total, href) if isinstance(total, int) else (None, None)


def _source_record_counts(
    request: Request, api: ApiClient, sources: Sequence[Mapping[str, Any]]
) -> dict[str, tuple[int | None, str | None]]:
    """`_source_records` for each source, kept per process for `SOURCE_RECORDS_CACHE_SECONDS`: the API
    has no grouped count, so a cold page makes one read per published register. A failed read is not
    kept, so the next load asks again."""
    cache: dict[str, tuple[float, tuple[int | None, str | None]]] = request.app.state.__dict__.setdefault(
        "source_records_cache", {}
    )
    now = time.monotonic()
    out: dict[str, tuple[int | None, str | None]] = {}
    for source in sources:
        source_id = str(source["source_id"])
        hit = cache.get(source_id)
        if hit is not None and now - hit[0] < SOURCE_RECORDS_CACHE_SECONDS:
            out[source_id] = hit[1]
            continue
        counted = _source_records(api, source)
        if counted[0] is not None:
            cache[source_id] = (now, counted)
        out[source_id] = counted
    return out


@app.get("/about/sources", response_class=HTMLResponse)
def about_sources(request: Request) -> HTMLResponse:
    """Sources & licences (docs/30 §11.4, §11.10): which register says what, under which licence, by
    region, US first. Published registers lead each region, most records first (docs/16 §4.9: the
    curated rows that led with "0" sort last); registers listed but not published follow behind a
    disclosure. One count per published register of a kind a list counts, each a link to that list.
    News feeds follow the regions in a section of their own, which `/about` does not count; the site's
    own social channels are not sources of facts and are left out (`NON_REGISTER_CATEGORIES`)."""
    api = get_api(request)
    sources = _all_sources(api)
    counts = _source_record_counts(request, api, [s for s in sources if _published(s)])
    groups = [
        (region_id, name, [s for s in sources if _is_register(s) and source_region(s) == region_id])
        for region_id, name in SOURCE_REGIONS
    ]
    groups.append((*NEWS_SECTION, [s for s in sources if s.get("category") == "news"]))
    regions = []
    for region_id, name, in_region in groups:
        if not in_region:
            continue
        published = []
        for source in in_region:
            if _published(source):
                records, href = counts[str(source["source_id"])]
                published.append({**source, "records": records, "records_href": href})
        published.sort(key=lambda s: (-(s["records"] or 0), str(s.get("name") or "").lower()))
        withheld = sorted(
            (s for s in in_region if not _published(s)), key=lambda s: str(s.get("name") or "").lower()
        )
        regions.append({"id": region_id, "name": name, "published": published, "withheld": withheld})
    return templates.TemplateResponse(request, "about_sources.html", {"regions": regions})


#: How `source.vintage_basis` reads to someone who is not going to read the code. The four
#: values are deliberately distinct: two ways of knowing, plus "the source states none" and "we
#: have not resolved one", which are different answers and are never merged.
VINTAGE_BASIS_LABELS = {
    "artefact_filename": "the release is in the name of the file we fetch",
    "shapefile_member": "the release is in the shapefile's member names",
    "not_stated": "the source publishes no release label",
    "undetermined": "no load has resolved a release for this source",
}


#: Coverage & methodology's three URL tabs (docs/30 §11.3 third level, §11.10), in order: the key, the
#: path and the tab's words. `/methodology` keeps Release dates, the tab the freshness line links to
#: (`/methodology#vintage`).
METHODOLOGY_TABS: tuple[tuple[str, str, str], ...] = (
    ("dates", "/methodology", "Release dates"),
    ("gaps", "/methodology/gaps", "What is not here"),
    ("words", "/methodology/status-words", "Status words"),
)


def _methodology_page(request: Request, tab: str) -> HTMLResponse:
    """One tab of Coverage & methodology. Everything on it is served from `/v1/coverage` (and, for the
    status words, `/v1/lifecycle-states`), which derive their numbers from the store and the pipeline's
    own status maps at request time. Nothing is a figure typed into a template, because a typed figure is
    exactly what goes quietly wrong: the page these replace began by printing a single fetch date that
    readers took for the data's age. Each tab reads only what it prints."""
    api = get_api(request)
    data = api.get("/v1/coverage")["data"]
    context: dict[str, Any] = {
        "tab": tab,
        "tabs": METHODOLOGY_TABS,
        "coverage": data,
        "vintage": data["vintage"],
        "withheld_supply": [s for s in data["sources"]["withheld"] if s.get("supply")],
        # Source ids name a register in the status maps and the asset table; a reader sees its short
        # name (or the manifest's name), never the id (audit 2026-09-30, F1).
        "source_names": methodology_source_names(),
        "posture": get_platform_posture(request),
    }
    if tab == "dates":
        context["basis_labels"] = VINTAGE_BASIS_LABELS
    if tab == "gaps":
        # Sources per asset type, rows sorted descending, with the note (if any) that measured what the
        # second source does to the count -- looked up by the fact it hangs off.
        context["asset_sources"] = sorted(
            (data.get("assets") or {}).get("by_type", {}).items(), key=lambda kv: -int(kv[1]["rows"])
        )
        context["asset_notes"] = {
            n["applies_to"]["unresolved_asset_type"]: n
            for n in data.get("notes") or []
            if (n.get("applies_to") or {}).get("unresolved_asset_type")
        }
        context["asset_type_labels"] = {t: _type_label(t, plural=True) for t in ASSET_TYPE_LABELS}
    if tab == "words":
        context["vocabulary"] = api.get("/v1/lifecycle-states")["data"]
    template = {
        "dates": "methodology.html",
        "gaps": "methodology_gaps.html",
        "words": "methodology_status_words.html",
    }
    return _posture_response(request, template[tab], context)


@app.get("/methodology", response_class=HTMLResponse)
def methodology(request: Request) -> HTMLResponse:
    """Release dates: each source's own release against the date we fetched it, and how records are
    placed and shown. Summaries of the other two tabs keep the old anchors (`#absences`, `#states`,
    `#withheld`, `#ownership`, `#lifecycle`, `#note-…`) answering, each linking on (docs/30 §11.10)."""
    return _methodology_page(request, "dates")


@app.get("/methodology/gaps", response_class=HTMLResponse)
def methodology_gaps(request: Request) -> HTMLResponse:
    """What is not here: technologies and states no source produces, registers held but withheld, the
    depth of ownership records, sources per asset type and the dated coverage notes."""
    return _methodology_page(request, "gaps")


@app.get("/methodology/status-words", response_class=HTMLResponse)
def methodology_status_words(request: Request) -> HTMLResponse:
    """What each status word asserts, what it does not, and which source values map into it, with how
    many records carry it on this load. Plain-language versions live in the glossary."""
    return _methodology_page(request, "words")


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


# Navigation v2 lane V7: the glossary (`/glossary`), plain words for every term on the site, its entries
# and anchors in web/glossary.py.
from web.glossary import router as glossary_router  # noqa: E402

app.include_router(glossary_router)


@app.get("/attribution", response_class=HTMLResponse)
def attribution(request: Request) -> HTMLResponse:
    """Task item 4: every source the API lists, plus a Basemap section (Protomaps/OSM ODbL,
    Natural Earth, Census), a Context layers section (EIA-860M, public domain) and the geocoding
    gazetteers (`geocoding_gazetteer_credits`). Reads every page of `/v1/sources` with the fetch
    `/about` and `/about/sources` make (`_all_sources`): one page of 100, sorted by id, stopped at
    `us.ferc.elibrary` and left out every ISO queue, LBNL and OpenStreetMap's credit line."""
    api = get_api(request)
    return templates.TemplateResponse(
        request,
        "attribution.html",
        {"sources": _all_sources(api), "gazetteers": geocoding_gazetteer_credits()},
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
