"""Sources, source-runs and licence-gate screens (Sprint 3 item 3; docs/30-design-ia.md §1.3,
§4.6, §5.5; docs/10-prd-mvp.md US-303, US-904, US-905). Server-rendered over `/admin/v1` through
`ctx.api` (`web/admin/shell.py`); every write POST checks `require_same_origin` first, then calls
the API, then redirects 303 with a `?flash=` message on success or re-renders the page with
`problem_notice(result)` and the submitted values on a non-2xx response, at the API's own status
code (the shell's contract, mirrored exactly).

Decisions
---------
D1. **Filters live in the query string as single-valued selects**, not multi-select checkbox
    groups. `GET /admin/v1/sources`'s `health`/`publish_state`/`implemented` parameters accept a
    comma-separated list for OR, but a single admin session narrowing "show me the failing ones"
    almost always wants one value; a single `<select>` keeps the toolbar usable at 400px (no
    JS-free multi-select renders usefully that narrow) while still round-tripping through the URL
    exactly as the task requires. `implemented` is a two-value select (`true`/`false`) since the
    API's own parameter is a plain boolean, not an enum.
D2. **"Add issuer" (`AdminSourceCreate`) takes `licence_id` as a plain text field, not a
    dropdown.** `GET /v1/licences` (`listLicences`) is `x-status: planned` in `api/openapi.yaml`
    (unimplemented), so a dropdown backed by it would silently break the form the moment this
    screen shipped. The admin adding a curated issuer already knows which licence row covers it
    (visible on every existing source's detail page) and enters the id directly, exactly as the
    task's field list describes it ("licence_id" as one field among equals, not "a licence
    picker").
D3. **The gate-clearance form is rendered, disabled, for every non-`legal` role** rather than
    hidden, so an operator can see what evidence a gate needs without being told to go ask legal
    with no context (docs/30 §4.6: "Only legal role can clear gate"). The API route itself still
    refuses a non-legal session with `403` regardless of what this page renders — the disabled
    form is a UX courtesy, not the enforcement point.
D4. **Health icon mapping is 1:1 with `SourceHealth`'s five values**, not the three-icon wireframe
    literally (`docs/30` §5.5 sketches only ✓/⚠/✗ since it predates `blocked`/`paused` joining the
    enum): `ok`->✓ success, `degraded`->⚠ progress, `failing`/`blocked`->✗ danger, `paused`->⏸
    neutral. Icon and text label are both always rendered (D-5 "never colour alone", docs/31 §7 SC
    1.4.1) — the label is the health string itself, never the icon alone.
D5. **The "GATED" chip (`chip--gated`, docs/31 §5.12) reads off the *licence*** (`reuse_class` in
    the posture's gated set, or `gate_flag` still open) rather than a boolean the API returns
    directly — `AdminSource` carries the licence's `reuse_class`/`gate_flag` embedded but no single
    `gated` field, so this module derives it the same way `_licence_gate_missing` on the API side
    does, kept as one small `_is_gated` helper so the list and detail pages agree. Since 2026-09-25
    the gated set is the platform posture's (`services/posture.py`, docs/26): `(noncommercial,
    restricted, unknown)` under `commercial`, `(restricted, unknown)` under `noncommercial`. The web
    container reads the same `PLATFORM_POSTURE` as the api (one `env_file` anchor in
    `infra/compose`), and `tests/test_platform_posture.py` pins this constant to the registry's.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from services.posture import gated_reuse_classes, platform_posture
from web.admin.shell import (
    AdminContext,
    problem_notice,
    render,
    require_operator,
    require_same_origin,
    templates,
)

router = APIRouter()

#: `SourceHealth` (api/openapi.yaml) -> (icon glyph, chip family class). Every value maps
#: somewhere; an unrecognised value (a future enum addition) falls back to a plain neutral chip
#: with its own text rather than raising (docs/31 D-29: never blank).
_HEALTH_CHIP: dict[str, tuple[str, str]] = {
    "ok": ("✓", "chip--success"),
    "degraded": ("⚠", "chip--progress"),
    "failing": ("✗", "chip--danger"),
    "blocked": ("✗", "chip--danger"),
    "paused": ("⏸", "chip--neutral"),
}
_HEALTH_VALUES = ("ok", "degraded", "failing", "blocked", "paused")
_PUBLISH_STATE_VALUES = ("ingest_only", "api_only", "public")
_ACCESS_VALUES = ("html", "rss", "api")
_CADENCE_VALUES = (
    "15-min",
    "daily",
    "weekly",
    "twice_weekly",
    "monthly",
    "quarterly",
    "annual",
    "realtime",
    "continuous",
)


def _health_chip(health: str) -> tuple[str, str]:
    return _HEALTH_CHIP.get(health, (health, "chip--neutral"))


#: D5: the reuse classes the chip calls gated — the posture's complement of the publishable set,
#: read once at import like `services/api/visibility.py::PUBLISHABLE_REUSE_CLASSES`.
GATED_REUSE_CLASSES: tuple[str, ...] = gated_reuse_classes(platform_posture())


def _is_gated(source: dict[str, Any]) -> bool:
    """D5: reads the embedded licence summary, the same two conditions
    `services/api/admin_sources.py::_licence_gate_missing` checks for reuse class and gate flag."""
    licence = source.get("licence") or {}
    return licence.get("reuse_class") in GATED_REUSE_CLASSES or bool(licence.get("gate_flag"))


templates.env.globals["source_health_chip"] = _health_chip
templates.env.globals["source_is_gated"] = _is_gated


def _selected_filters(request: Request) -> dict[str, str]:
    qp = request.query_params
    return {
        "health": qp.get("health", ""),
        "publish_state": qp.get("publish_state", ""),
        "implemented": qp.get("implemented", ""),
    }


# =================================================================================== source list
@router.get("/admin/sources", response_class=HTMLResponse)
def list_sources(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    filters = _selected_filters(request)
    params = {k: v for k, v in filters.items() if v}
    cursor = request.query_params.get("cursor")
    if cursor:
        params["cursor"] = cursor
    result = ctx.api.get("/admin/v1/sources", params=params)
    if result.status_code != 200:
        return render(
            request,
            "admin/sources/list.html",
            {
                "sources": [],
                "filters": filters,
                "next_cursor": None,
                "notice": problem_notice(result),
                "flash": request.query_params.get("flash"),
            },
            ctx=ctx,
            nav_key="sources",
            status_code=result.status_code,
        )
    body = result.body.get("data", [])
    page = result.body.get("page") or {}
    return render(
        request,
        "admin/sources/list.html",
        {
            "sources": body,
            "filters": filters,
            "next_cursor": page.get("next_cursor"),
            "notice": None,
            "flash": request.query_params.get("flash"),
        },
        ctx=ctx,
        nav_key="sources",
    )


# ============================================================================== add curated issuer
@router.get("/admin/sources/new", response_class=HTMLResponse)
def new_source_form(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    return render(
        request,
        "admin/sources/new.html",
        {"values": {}, "notice": None, "access_values": _ACCESS_VALUES, "cadence_values": _CADENCE_VALUES},
        ctx=ctx,
        nav_key="sources",
    )


@router.post("/admin/sources/new", response_class=HTMLResponse)
async def create_source(
    request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    values = {k: str(v) for k, v in form.items()}
    body: dict[str, Any] = {
        "source_id": values.get("source_id", ""),
        "name": values.get("name", ""),
        "jurisdiction": values.get("jurisdiction") or None,
        "category": "procurement",
        "operator": values.get("operator", ""),
        "url": values.get("url", ""),
        "access": values.get("access", ""),
        "cadence": values.get("cadence", ""),
        "licence_id": values.get("licence_id", ""),
        "attribution_text": values.get("attribution_text") or None,
        "reason": values.get("reason", ""),
    }
    result = ctx.api.post("/admin/v1/sources", json=body)
    if result.status_code == 201:
        source_id = result.body.get("data", {}).get("source_id", body["source_id"])
        return RedirectResponse(url=f"/admin/sources/{source_id}?flash=Issuer+added", status_code=303)
    return render(
        request,
        "admin/sources/new.html",
        {
            "values": values,
            "notice": problem_notice(result),
            "access_values": _ACCESS_VALUES,
            "cadence_values": _CADENCE_VALUES,
        },
        ctx=ctx,
        nav_key="sources",
        status_code=result.status_code,
    )


# ======================================================================================= detail
def _fetch_source_detail(ctx: AdminContext, source_id: str) -> tuple[dict[str, Any] | None, Any]:
    result = ctx.api.get(f"/admin/v1/sources/{source_id}")
    if result.status_code != 200:
        return None, result
    return result.body.get("data"), result


@router.get("/admin/sources/{source_id}", response_class=HTMLResponse)
def source_detail(
    source_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    source, result = _fetch_source_detail(ctx, source_id)
    if source is None:
        return render(
            request,
            "admin/sources/detail.html",
            {"source": None, "runs": [], "notice": problem_notice(result), "flash": None},
            ctx=ctx,
            nav_key="sources",
            status_code=result.status_code,
        )
    runs_result = ctx.api.get("/admin/v1/source-runs", params={"source_id": source_id, "limit": 10})
    runs = runs_result.body.get("data", []) if runs_result.status_code == 200 else []
    return render(
        request,
        "admin/sources/detail.html",
        {
            "source": source,
            "runs": runs,
            "notice": None,
            "flash": request.query_params.get("flash"),
            "publish_state_values": _PUBLISH_STATE_VALUES,
            "can_clear_gate": ctx.role == "legal",
        },
        ctx=ctx,
        nav_key="sources",
    )


def _redirect_to_source(source_id: str, message: str) -> RedirectResponse:
    return RedirectResponse(url=f"/admin/sources/{source_id}?flash={message}", status_code=303)


def _rerender_source_detail(request: Request, ctx: AdminContext, source_id: str, result: Any) -> Response:
    source, _ = _fetch_source_detail(ctx, source_id)
    runs_result = ctx.api.get("/admin/v1/source-runs", params={"source_id": source_id, "limit": 10})
    runs = runs_result.body.get("data", []) if runs_result.status_code == 200 else []
    return render(
        request,
        "admin/sources/detail.html",
        {
            "source": source,
            "runs": runs,
            "notice": problem_notice(result),
            "flash": None,
            "publish_state_values": _PUBLISH_STATE_VALUES,
            "can_clear_gate": ctx.role == "legal",
        },
        ctx=ctx,
        nav_key="sources",
        status_code=result.status_code,
    )


# ------------------------------------------------------------------------------------- run now
@router.post("/admin/sources/{source_id}/run", response_class=HTMLResponse)
async def run_source(
    source_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    reason = str(form.get("reason", ""))
    trigger = str(form.get("trigger") or "manual")
    result = ctx.api.post(f"/admin/v1/sources/{source_id}/run", json={"reason": reason, "trigger": trigger})
    if result.status_code == 202:
        return _redirect_to_source(source_id, "Run+queued")
    return _rerender_source_detail(request, ctx, source_id, result)


# --------------------------------------------------------------------------------- pause/resume
@router.post("/admin/sources/{source_id}/pause", response_class=HTMLResponse)
async def pause_source(
    source_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    reason = str(form.get("reason", ""))
    result = ctx.api.patch(f"/admin/v1/sources/{source_id}", json={"paused": True, "reason": reason})
    if result.status_code == 200:
        return _redirect_to_source(source_id, "Source+paused")
    return _rerender_source_detail(request, ctx, source_id, result)


@router.post("/admin/sources/{source_id}/resume", response_class=HTMLResponse)
async def resume_source(
    source_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    reason = str(form.get("reason", ""))
    result = ctx.api.patch(f"/admin/v1/sources/{source_id}", json={"paused": False, "reason": reason})
    if result.status_code == 200:
        return _redirect_to_source(source_id, "Source+resumed")
    return _rerender_source_detail(request, ctx, source_id, result)


# ---------------------------------------------------------------------------------- edit cadence
@router.post("/admin/sources/{source_id}/edit", response_class=HTMLResponse)
async def edit_source(
    source_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    body: dict[str, Any] = {"reason": str(form.get("reason", ""))}
    schedule_cron = str(form.get("schedule_cron", "")).strip()
    if schedule_cron:
        body["schedule_cron"] = schedule_cron
    # No lag field: nothing is time-delayed on any tier and there is no per-source delay to set
    # (owner, 2026-09-21; `services/ingest/lag.py`, migration 0019). The API would reject one.
    result = ctx.api.patch(f"/admin/v1/sources/{source_id}", json=body)
    if result.status_code == 200:
        return _redirect_to_source(source_id, "Cadence+updated")
    return _rerender_source_detail(request, ctx, source_id, result)


# --------------------------------------------------------------------------------- publish state
@router.post("/admin/sources/{source_id}/publish-state", response_class=HTMLResponse)
async def set_source_publish_state(
    source_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    body = {"publish_state": str(form.get("publish_state", "")), "reason": str(form.get("reason", ""))}
    result = ctx.api.put(f"/admin/v1/sources/{source_id}/publish-state", json=body)
    if result.status_code == 200:
        return _redirect_to_source(source_id, "Publish+state+updated")
    return _rerender_source_detail(request, ctx, source_id, result)


# ------------------------------------------------------------------------------------- gate form
@router.post("/admin/sources/{source_id}/gate", response_class=HTMLResponse)
async def clear_licence_gate(
    source_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    same_origin = require_same_origin(request)
    if same_origin is not None:
        return same_origin
    form = await request.form()
    licence_id = str(form.get("licence_id", ""))
    body: dict[str, Any] = {
        "gate_flag": str(form.get("gate_flag", "true")) == "true",
        "reason": str(form.get("reason", "")),
    }
    for field in ("evidence_url", "classified_by", "contract_ref", "notes"):
        value = str(form.get(field, "")).strip()
        if value:
            body[field] = value
    evidence_retrieved_at = str(form.get("evidence_retrieved_at", "")).strip()
    if evidence_retrieved_at:
        body["evidence_retrieved_at"] = f"{evidence_retrieved_at}T00:00:00Z"
    result = ctx.api.put(f"/admin/v1/licences/{licence_id}/gate", json=body)
    if result.status_code == 200:
        return _redirect_to_source(source_id, "Licence+gate+updated")
    return _rerender_source_detail(request, ctx, source_id, result)


# =================================================================================== source runs
@router.get("/admin/source-runs", response_class=HTMLResponse)
def list_source_runs(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    qp = request.query_params
    filters = {"source_id": qp.get("source_id", ""), "status": qp.get("status", "")}
    params = {k: v for k, v in filters.items() if v}
    cursor = qp.get("cursor")
    if cursor:
        params["cursor"] = cursor
    result = ctx.api.get("/admin/v1/source-runs", params=params)
    runs = result.body.get("data", []) if result.status_code == 200 else []
    page = result.body.get("page") or {}
    return render(
        request,
        "admin/sources/runs_list.html",
        {
            "runs": runs,
            "filters": filters,
            "next_cursor": page.get("next_cursor"),
            "notice": None if result.status_code == 200 else problem_notice(result),
        },
        ctx=ctx,
        nav_key="sources",
        status_code=200 if result.status_code == 200 else result.status_code,
    )


@router.get("/admin/source-runs/{run_id}", response_class=HTMLResponse)
def source_run_detail(
    run_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    result = ctx.api.get(f"/admin/v1/source-runs/{run_id}")
    if result.status_code != 200:
        return render(
            request,
            "admin/sources/run_detail.html",
            {"run": None, "notice": problem_notice(result)},
            ctx=ctx,
            nav_key="sources",
            status_code=result.status_code,
        )
    return render(
        request,
        "admin/sources/run_detail.html",
        {"run": result.body.get("data"), "notice": None},
        ctx=ctx,
        nav_key="sources",
    )


# ===================================================================================== snapshot
@router.get("/admin/snapshots/{snapshot_id}", response_class=HTMLResponse)
def snapshot_detail(
    snapshot_id: str, request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]
) -> Response:
    """Linked from a run's detail page (docs/30 §5.5 "link to the snapshot when present")."""
    result = ctx.api.get(f"/admin/v1/snapshots/{snapshot_id}")
    if result.status_code != 200:
        return render(
            request,
            "admin/sources/snapshot_detail.html",
            {"snapshot": None, "notice": problem_notice(result)},
            ctx=ctx,
            nav_key="sources",
            status_code=result.status_code,
        )
    return render(
        request,
        "admin/sources/snapshot_detail.html",
        {"snapshot": result.body.get("data"), "notice": None},
        ctx=ctx,
        nav_key="sources",
    )


__all__ = ["router"]
