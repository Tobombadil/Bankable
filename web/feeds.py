"""Feeds and the API page (audit 2026-10-07 UX-6; docs/30 §1.1, §3: `/feeds/*` US-503, `/docs/api`
US-704).

The API has served `/feeds/{proposals,opportunities,events}.{rss,json}` since Sprint 2, and
`/pricing` lists "RSS feeds of new proposals and opportunities" as a free feature, but no page
linked a feed and no page carried `<link rel="alternate">`; nothing pointed at the API reference
either. This module gives every list its feed twin (the same filters, docs/04 D-17), and a
`/docs/api` page that says what the API and the feeds are and links the live reference.

**`/feeds/*` here is a development relay.** In production Caddy sends `/feeds/*` on the site host
to the API before the site sees it (`infra/compose/Caddyfile`, `@api path ... /feeds/*`), so these
routes are never reached there; under `dev_up` and the in-process client the site has no API
host in front of it, and without the relay every feed link on the page would be a 404. The relay
forwards the path and the query string only, never cookies, and answers with the API's own status,
content type and body.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from services.api.common import API_HOST, DOMAIN
from web.page import OPPORTUNITY_PASSTHROUGH_FILTERS, PROPOSAL_PASSTHROUGH_FILTERS, get_api, templates
from web.viewmodels import opportunity_status_param, resolve_proposal_lifecycle_param

router = APIRouter()

#: The public feeds the API serves (services/api/app.py `feed_*`), in the order the page lists them.
FEEDS: tuple[tuple[str, str], ...] = (
    ("proposals", "New and changed proposals"),
    ("opportunities", "New and changed opportunities"),
    ("events", "Every change event"),
)
FEED_FORMATS = ("rss", "json")


def feed_href(entity: str, qp: Mapping[str, str], fmt: str = "rss") -> str:
    """The feed twin of a list view: the list's own filters, with the lifecycle or status choice
    spelt out, because the feed (like the API) has no "active by default" of its own."""
    if entity == "opportunity":
        kept = [(n, qp[n]) for n in OPPORTUNITY_PASSTHROUGH_FILTERS if qp.get(n)]
        kept.append(("status", opportunity_status_param(qp)))
        return f"/feeds/opportunities.{fmt}?" + urlencode(kept)
    kept = [(n, qp[n]) for n in PROPOSAL_PASSTHROUGH_FILTERS if qp.get(n) and n != "placement"]
    lifecycle_csv, _explicit, _withdrawn = resolve_proposal_lifecycle_param(qp)
    kept.append(("lifecycle_state", lifecycle_csv))
    return f"/feeds/proposals.{fmt}?" + urlencode(kept)


def public_api_base(request: Request) -> str:
    """Where this deployment's API answers: `PUBLIC_API_URL` when set, else the host beside the site
    (`api.` for production, `api-staging.` for `staging.`, as `infra/compose/Caddyfile` names them),
    else the production host (a development checkout has no API host of its own)."""
    configured = (os.environ.get("PUBLIC_API_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    host = (request.url.hostname or "").lower()
    if host == f"staging.{DOMAIN}":
        return f"https://api-staging.{DOMAIN}"
    return API_HOST


@router.get("/feeds/{name}.{fmt}")
def feed_relay(name: str, fmt: str, request: Request) -> Response:
    """Development relay for the API's public feeds (module docstring)."""
    if name not in {n for n, _ in FEEDS} or fmt not in FEED_FORMATS:
        return Response(status_code=404)
    response = get_api(request).get_raw(f"/feeds/{name}.{fmt}", params=dict(request.query_params))
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
    )


@router.get("/docs/api", response_class=HTMLResponse)
def api_docs(request: Request) -> HTMLResponse:
    base = public_api_base(request)
    feeds: list[dict[str, Any]] = [
        {"name": name, "label": label, "rss": f"/feeds/{name}.rss", "json": f"/feeds/{name}.json"}
        for name, label in FEEDS
    ]
    return templates.TemplateResponse(
        request,
        "docs_api.html",
        {
            "api_base": base,
            "reference_url": f"{base}/openapi.json",
            "openapi_url": f"{base}/openapi.json",
            "feeds": feeds,
            "example_feed": "/feeds/proposals.rss?kind=load&jurisdiction=US-VA",
        },
    )
