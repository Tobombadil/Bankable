"""The sitemap and robots.txt routes (docs/42-backend-review-2026-09-26.md lane L3), moved out of
`web/app.py` behaviour-preserving: a closed cluster (§4.1) -- five helpers and three routes that
nothing outside it reaches, and that reach nothing but `get_api`, `ApiClient`/`ApiError` and the two
CSV constants for the "every lifecycle state / every status" walk.

Shared page plumbing (`get_api`, `ALL_OPPORTUNITY_STATUSES_CSV`) lives in `web/page.py`, which this
module and `web/app.py` both import -- `web/app.py` includes this router and so cannot be imported
back from here without a cycle.
"""

from __future__ import annotations

import logging
import time
from html import escape

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

from web.api_client import ApiClient, ApiError
from web.page import ALL_OPPORTUNITY_STATUSES_CSV, get_api
from web.viewmodels import ALL_PROPOSAL_LIFECYCLE_STATES

ALL_PROPOSAL_LIFECYCLE_STATES_CSV = ",".join(ALL_PROPOSAL_LIFECYCLE_STATES)

router = APIRouter()
log = logging.getLogger(__name__)


#: docs/23 §3.1 "Detail pages visible on the public tier only; regenerated hourly with the
#: delayed view" -- "regenerated hourly" describes a would-be cache in front of this route, not
#: this route's own logic: every call here reads the live API, which already applies the delay
#: and tier gating itself (the same visibility predicate every other page in this module reads
#: through), so the sitemap is honest on every request with no separate cache of its own.
#:
#: History of the per-resource bound: 25 pages (5,000 URLs) until 2026-09-19, then 150 pages
#: (30,000 URLs), which the 2026-09-30 audit (frontend F12) measured cutting `/assets` off at
#: exactly 30,000 of 30,955 rows -- and every new asset source widens that gap (transmission lines
#: alone added 13,084). The walk now runs every resource to its last page. This number is only a
#: guard against a pagination bug looping for ever: 5,000 pages of 200 is a million URLs per
#: resource, forty sitemap files, far above any register this site holds; reaching it is logged as
#: an error naming the resource, never silently truncated.
SITEMAP_MAX_PAGES_PER_RESOURCE = 5_000
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
    "/interconnection-points",
    "/search",
    "/about",
    "/methodology",
    "/attribution",
    "/pricing",
)


def _sitemap_paths_for(
    api: ApiClient,
    path: str,
    url_prefix: str,
    *,
    extra_params: dict[str, str] | None = None,
    key: str = "slug",
) -> list[str]:
    """Every page of one resource's public list, as `{url_prefix}/{row[key]}`. `key` is the field
    the detail route takes: a slug for records, assets and organisations, the `public_id` for an
    interconnection point (which has no slug, docs/21 §3.24)."""
    paths: list[str] = []
    cursor: str | None = None
    for _ in range(SITEMAP_MAX_PAGES_PER_RESOURCE):
        params: dict[str, str | None] = {"limit": str(SITEMAP_PAGE_SIZE), "cursor": cursor}
        params.update(extra_params or {})
        envelope = api.get(path, params=params)
        for row in envelope["data"]:
            ident = row.get(key)
            if ident:
                paths.append(f"{url_prefix}/{ident}")
        page = envelope.get("page") or {}
        if not page.get("has_more"):
            break
        cursor = page.get("next_cursor")
        if cursor is None:
            break
    else:
        log.error(
            "sitemap_resource_truncated path=%s pages=%d urls=%d: the guard was reached with more pages left",
            path,
            SITEMAP_MAX_PAGES_PER_RESOURCE,
            len(paths),
        )
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

    Proposals, opportunities, assets, organisations and grid interconnection points,
    cursor-paginated per resource to the last page (`SITEMAP_MAX_PAGES_PER_RESOURCE` is a loop
    guard, not a cap). Points are read
    from `GET /v1/interconnection-points` at the anonymous tier like every other resource, so a
    point is listed only when that route lists it: its register is visible and at least one of its
    proposals is (docs/21 D-17). A gated or emptied point is absent, exactly as it is from the
    index page, so the sitemap is no oracle for it (lane H2, 2026-09-29). A resource whose list
    call errors is skipped rather than blanking the whole sitemap. When the URLs fit one file,
    `/sitemap.xml` is that `<urlset>`; above that it becomes a `<sitemapindex>` over
    `/sitemaps/{n}.xml` (`docs/23` §3.1's split-by-file shape), so the protocol's 50,000-URL and
    50 MB limits stay out of reach.
    """
    paths: list[str] = list(SITEMAP_STATIC_PATHS)
    resources: list[tuple[str, str, dict[str, str], str]] = [
        ("/v1/proposals", "/proposals", {"lifecycle_state": ALL_PROPOSAL_LIFECYCLE_STATES_CSV}, "slug"),
        ("/v1/opportunities", "/opportunities", {"status": ALL_OPPORTUNITY_STATUSES_CSV}, "slug"),
        ("/v1/assets", "/assets", {}, "slug"),
        ("/v1/organizations", "/organizations", {}, "slug"),
        ("/v1/interconnection-points", "/interconnection-points", {}, "public_id"),
    ]
    for api_path, prefix, extra, key in resources:
        try:
            paths += _sitemap_paths_for(api, api_path, prefix, extra_params=extra, key=key)
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

    Building costs one sequential upstream list call per 200 URLs (about 280 on the 2026-10-06 dev
    store), an uncached amplifier on a public route where a connection reset mid-way 500ed the
    whole response (web audit 2026-09-18), so the rendered documents are cached per base URL for
    an hour -- the sitemap changes daily at most. The cache holds every document from one walk
    rather than one file's XML, so a crawler fetching the index and then twenty child sitemaps
    still costs one walk, not twenty-one."""
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


@router.get("/sitemap.xml")
def sitemap(request: Request) -> Response:
    return Response(content=_sitemap_documents(request)["/sitemap.xml"], media_type="application/xml")


@router.get("/sitemaps/{number}.xml")
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
    "/report",
)


@router.get("/robots.txt")
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
