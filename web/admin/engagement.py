"""Admin panel: Engagement screen — weekly counts of the identifier-free `ui_event` rows
(docs/21 §3.21; docs/00-PLAN.md decision 2026-09-15 "Context layer and map"). The context layer
exists to raise engagement, and the owner asked for that to be measured rather than assumed. This
page is the measurement: per ISO week, how often the plants layer was switched on and off, how
often a regional quick view was used, how many times the basemap failed to load (the runbook §2.7
done-check says this must stay at zero after the deploy), how many registrations arrived from a
page with the layer on, and how many alerts were created.

**Alert activation** (owner decisions 2026-09-18 (8) and 2026-09-30; PM-5, sales F8): the day-30
posture decision reads alerts created per view of a proposal, company, asset or grid-point page.
`page.viewed` rows carry only `page_type` (`services/api/ui_events.py`), and `alert.created` carries
nothing, so an alert cannot be attributed to the page type it came from without an identifier the
design refuses. The table therefore divides the week's alerts by the week's views, overall and per
page type, and says so in its caption; views are page loads, not unique readers.

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
    ("page.viewed", "Detail page views"),
)
#: `page.viewed`'s page types, in display order (`services/db/models.py::PAGE_VIEW_TYPES`).
PAGE_TYPES: tuple[tuple[str, str], ...] = (
    ("proposal", "Proposal"),
    ("company", "Company"),
    ("asset", "Asset"),
    ("point", "Grid point"),
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
        name = str(r.get("name"))
        cells[name] = {
            "count": int(r.get("count") or 0),
            "count_on": r.get("count_on"),
            "count_off": r.get("count_off"),
        }
        if name == "page.viewed":
            raw = r.get("by_page_type")
            by_type: dict[str, Any] = raw if isinstance(raw, dict) else {}
            cells[name]["by_page_type"] = {t: int(by_type.get(t) or 0) for t, _label in PAGE_TYPES}
    out: list[dict[str, Any]] = []
    for week in sorted(weeks, reverse=True):
        cells = weeks[week]["cells"]
        for name, _label in COLUMNS:
            cells.setdefault(name, {"count": 0, "count_on": None, "count_off": None})
        cells["page.viewed"].setdefault("by_page_type", dict.fromkeys(dict(PAGE_TYPES), 0))
        out.append(weeks[week])
    return out


def per_hundred(alerts: int, views: int) -> str | None:
    """Alerts per 100 views to one decimal, or `None` when there were no views (never a fabricated 0)."""
    return f"{100 * alerts / views:.1f}" if views else None


def activation_rows(grid: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per week: alerts created, views by page type and in all, and alerts per 100 views of each."""
    rows: list[dict[str, Any]] = []
    for week in grid:
        alerts = int(week["cells"]["alert.created"]["count"])
        views = week["cells"]["page.viewed"]["by_page_type"]
        total = int(week["cells"]["page.viewed"]["count"])
        rows.append(
            {
                "week": week["week"],
                "alerts": alerts,
                "total": {"views": total, "rate": per_hundred(alerts, total)},
                "types": [
                    {"label": label, "views": views.get(t, 0), "rate": per_hundred(alerts, views.get(t, 0))}
                    for t, label in PAGE_TYPES
                ],
            }
        )
    return rows


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
            {
                "notice": problem_notice(result),
                "weeks": weeks,
                "grid": [],
                "columns": COLUMNS,
                "activation": [],
                "page_types": PAGE_TYPES,
            },
            ctx=ctx,
            nav_key="engagement",
            status_code=result.status_code,
        )
    rows = result.body.get("data", [])
    grid = pivot_weeks(rows if isinstance(rows, list) else [])
    return render(
        request,
        "admin/engagement/index.html",
        {
            "weeks": weeks,
            "grid": grid,
            "columns": COLUMNS,
            "activation": activation_rows(grid),
            "page_types": PAGE_TYPES,
        },
        ctx=ctx,
        nav_key="engagement",
    )
