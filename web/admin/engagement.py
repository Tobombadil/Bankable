"""Admin panel: Engagement screen — weekly counts of the identifier-free `ui_event` rows
(docs/21 §3.21; docs/00-PLAN.md decision 2026-09-15 "Context layer and map"). The context layer
exists to raise engagement, and the owner asked for that to be measured rather than assumed. This
page is the measurement: per ISO week, how often the plants layer was switched on and off, how
often a regional quick view was used, how many times the basemap failed to load (the runbook §2.7
done-check says this must stay at zero after the deploy), how many registrations arrived from a
page with the layer on, and how many alerts were created.

Reads only `GET /admin/v1/ui-events/summary` through `ctx.api`; renders through the shell. No
totals are invented here: the API returns one row per (week, name) and this module pivots them
into a week × name grid so a reader can compare columns. Missing cells are shown as 0 because a
week with no rows really did have zero events (the API window is the last N weeks regardless of
whether anything happened).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from web.admin.shell import AdminContext, problem_notice, render, require_operator

router = APIRouter()

#: Column order and human labels; the vocabulary is `services/db/models.py::UI_EVENT_NAMES`.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("map.layer_toggled", "Plants layer toggled"),
    ("map.region_jumped", "Region jumps"),
    ("map.basemap_failed", "Basemap failures"),
    ("auth.registered", "Registrations"),
    ("alert.created", "Alerts created"),
)
DEFAULT_WEEKS = 8
MAX_WEEKS = 52


def pivot_weeks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows of `{week, name, count, count_on?, count_off?}` → one dict per week (newest first)
    with a `cells` map name → `{count, count_on, count_off}`; absent pairs are zero."""
    weeks: dict[str, dict[str, Any]] = {}
    for r in rows:
        week = str(r.get("week") or "")
        if not week:
            continue
        cells = weeks.setdefault(week, {"week": week, "cells": {}})["cells"]
        cells[str(r.get("name"))] = {
            "count": int(r.get("count") or 0),
            "count_on": r.get("count_on"),
            "count_off": r.get("count_off"),
        }
    out: list[dict[str, Any]] = []
    for week in sorted(weeks, reverse=True):
        cells = weeks[week]["cells"]
        for name, _label in COLUMNS:
            cells.setdefault(name, {"count": 0, "count_on": None, "count_off": None})
        out.append(weeks[week])
    return out


def _weeks_param(raw: str | None) -> int:
    try:
        n = int(raw) if raw else DEFAULT_WEEKS
    except ValueError:
        n = DEFAULT_WEEKS
    return max(1, min(n, MAX_WEEKS))


@router.get("/admin/engagement")
def get_engagement(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    weeks = _weeks_param(request.query_params.get("weeks"))
    result = ctx.api.get("/admin/v1/ui-events/summary", params={"weeks": str(weeks)})
    if result.status_code != 200:
        return render(
            request,
            "admin/engagement/index.html",
            {"notice": problem_notice(result), "weeks": weeks, "grid": [], "columns": COLUMNS},
            ctx=ctx,
            nav_key="engagement",
            status_code=result.status_code,
        )
    rows = result.body.get("data", [])
    grid = pivot_weeks(rows if isinstance(rows, list) else [])
    return render(
        request,
        "admin/engagement/index.html",
        {"weeks": weeks, "grid": grid, "columns": COLUMNS},
        ctx=ctx,
        nav_key="engagement",
    )
