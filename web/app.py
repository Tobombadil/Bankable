"""Sprint 2 public-site prototype (docs/20 §15 web-app row: FastAPI + Jinja2 + htmx; MapLibre for
the map). Public delayed tier only -- no auth, no Pro, no admin (this task's scope).

Run: `uvicorn web.app:app --reload` from the repo root, after `python -m web.build_data --no-lag`
has written `web/static/data/*.json` (see `web/README.md`).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import QueryParams

from web.store import (
    OPPORTUNITY_SORT_ALLOWLIST,
    PROPOSAL_SORT_ALLOWLIST,
    Store,
    filter_opportunities,
    filter_proposals,
    paginate,
    sort_records,
)

WEB_ROOT = Path(__file__).resolve().parent
DATA_DIR = WEB_ROOT / "static" / "data"
PAGE_SIZE = 50

app = FastAPI(title="Infraqueue (placeholder) -- public site prototype")
app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))


def get_store() -> Store:
    # Loaded once and cached on the app object (docs/20 §15: this sprint has no database; the
    # store is the three files `build_data.py` writes). A restart picks up a re-run build.
    store: Store | None = getattr(app.state, "store", None)
    if store is None:
        store = Store.load(DATA_DIR)
        app.state.store = store
    return store


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def querystring_without(params: QueryParams, *drop: str) -> str:
    kept = [(k, v) for k, v in params.multi_items() if k not in drop]
    return "&".join(f"{k}={v}" for k, v in kept)


def delayed_notice(kind: str, stats: dict[str, Any]) -> dict[str, Any]:
    """docs/04 D-3/D-28: fixed wording, rendered from data. The prototype's `--no-lag` build
    still carries the configured `lag_days` here so the notice reads as it will in production
    (the task's own instruction: "render the delayed-tier notice regardless").
    """
    lag_days = stats["lag_days"][kind]
    data_as_of = stats["data_as_of"].get(kind)
    return {"lag_days": lag_days, "data_as_of": data_as_of, "no_lag_demo": stats.get("no_lag", False)}


def facet_options(records: list[dict[str, Any]], field: str) -> list[str]:
    return sorted({str(r[field]) for r in records if r.get(field)})


def facet_options_multi(records: list[dict[str, Any]], field: str) -> list[str]:
    values: set[str] = set()
    for r in records:
        for v in r.get(field) or []:
            values.add(str(v))
    return sorted(values)


@app.get("/", response_class=HTMLResponse)
def home_map(request: Request) -> HTMLResponse:
    store = get_store()
    return templates.TemplateResponse(
        request,
        "home_map.html",
        {
            "stats": store.stats,
            "delayed": delayed_notice("proposal", store.stats),
            "technologies": facet_options(store.proposals, "technology"),
            "lifecycle_states": facet_options(store.proposals, "lifecycle_state"),
            "jurisdictions": facet_options(store.proposals, "state"),
            "generated_at": store.stats.get("generated_at"),
        },
    )


@app.get("/map")
def map_alias() -> RedirectResponse:
    return RedirectResponse(url="/")


@app.get("/proposals", response_class=HTMLResponse)
def proposals_list(request: Request) -> HTMLResponse:
    store = get_store()
    qp = request.query_params
    filtered = filter_proposals(
        store.proposals,
        technology=qp.get("technology"),
        lifecycle_state=qp.get("lifecycle_state"),
        kind=qp.get("kind"),
        jurisdiction=qp.get("jurisdiction"),
        capacity_gte=_to_float(qp.get("capacity_mw[gte]")),
        capacity_lte=_to_float(qp.get("capacity_mw[lte]")),
        q=qp.get("q"),
    )
    filtered = sort_records(filtered, qp.get("sort"), PROPOSAL_SORT_ALLOWLIST, "-capacity_mw")
    offset = _to_int(qp.get("cursor")) or 0
    page, has_more, total = paginate(filtered, offset=offset, limit=PAGE_SIZE)

    context = {
        "records": page,
        "total": total,
        "has_more": has_more,
        "next_cursor": offset + PAGE_SIZE if has_more else None,
        "prev_cursor": max(offset - PAGE_SIZE, 0) if offset > 0 else None,
        "querystring": querystring_without(qp, "cursor"),
        "delayed": delayed_notice("proposal", store.stats),
        "technologies": facet_options(store.proposals, "technology"),
        "lifecycle_states": facet_options(store.proposals, "lifecycle_state"),
        "jurisdictions": facet_options(store.proposals, "state"),
        "kinds": facet_options(store.proposals, "kind"),
        "filters": dict(qp),
        "stats": store.stats,
    }
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/_proposal_rows.html", context)
    return templates.TemplateResponse(request, "proposals_list.html", context)


@app.get("/proposals/{slug}", response_class=HTMLResponse)
def proposal_detail(request: Request, slug: str) -> HTMLResponse:
    store = get_store()
    record = store.proposals_by_slug.get(slug)
    if record is None:
        return templates.TemplateResponse(request, "not_found.html", {"kind": "proposal"}, status_code=404)
    return templates.TemplateResponse(
        request,
        "proposal_detail.html",
        {
            "record": record,
            "delayed": delayed_notice("proposal", store.stats),
            "stats": store.stats,
        },
    )


@app.get("/opportunities", response_class=HTMLResponse)
def opportunities_list(request: Request) -> HTMLResponse:
    store = get_store()
    qp = request.query_params
    # US-301 AC2: default view is open opportunities; `status=all` (or any explicit value)
    # overrides the default rather than being ANDed with it.
    status_param = qp.get("status", "open")
    filtered = filter_opportunities(
        store.opportunities,
        kind=qp.get("kind"),
        status=None if status_param == "all" else status_param,
        jurisdiction=qp.get("jurisdiction"),
        technology=qp.get("technology"),
        q=qp.get("q"),
    )
    filtered = sort_records(filtered, qp.get("sort"), OPPORTUNITY_SORT_ALLOWLIST, "due_at")
    offset = _to_int(qp.get("cursor")) or 0
    page, has_more, total = paginate(filtered, offset=offset, limit=PAGE_SIZE)

    context = {
        "records": page,
        "total": total,
        "has_more": has_more,
        "next_cursor": offset + PAGE_SIZE if has_more else None,
        "prev_cursor": max(offset - PAGE_SIZE, 0) if offset > 0 else None,
        "querystring": querystring_without(qp, "cursor"),
        "delayed": delayed_notice("opportunity", store.stats),
        "kinds": facet_options(store.opportunities, "kind"),
        "statuses": facet_options(store.opportunities, "status"),
        "jurisdictions": facet_options(store.opportunities, "jurisdiction"),
        "technologies": facet_options_multi(store.opportunities, "technologies"),
        "filters": {**dict(qp), "status": status_param},
        "stats": store.stats,
    }
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/_opportunity_rows.html", context)
    return templates.TemplateResponse(request, "opportunities_list.html", context)


@app.get("/opportunities/{slug}", response_class=HTMLResponse)
def opportunity_detail(request: Request, slug: str) -> HTMLResponse:
    store = get_store()
    record = store.opportunities_by_slug.get(slug)
    if record is None:
        return templates.TemplateResponse(request, "not_found.html", {"kind": "opportunity"}, status_code=404)
    return templates.TemplateResponse(
        request,
        "opportunity_detail.html",
        {
            "record": record,
            "delayed": delayed_notice("opportunity", store.stats),
            "stats": store.stats,
        },
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request) -> HTMLResponse:
    store = get_store()
    q = request.query_params.get("q", "").strip()
    proposals = filter_proposals(store.proposals, q=q)[:50] if q else []
    opportunities = filter_opportunities(store.opportunities, q=q)[:50] if q else []
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "proposals": proposals,
            "opportunities": opportunities,
            "stats": store.stats,
        },
    )


@app.get("/about", response_class=HTMLResponse)
def about(request: Request) -> HTMLResponse:
    store = get_store()
    return templates.TemplateResponse(
        request,
        "about.html",
        {"sources": store.stats["sources"], "stats": store.stats},
    )


@app.get("/health")
def health() -> dict[str, Any]:
    store = get_store()
    return {
        "status": "ok",
        "data_as_of": store.stats["data_as_of"],
        "generated_at": store.stats["generated_at"],
        "checked_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _to_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
