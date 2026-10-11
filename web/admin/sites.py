"""Admin panel: Sites screen, read-only (docs/21 §3.25; lane S2, 2026-10-10). What recent site
rebuilds did (`site_audit`: created, members gained or lost, lead changed, split, merged, retired;
the members and sites involved; when) and every site flagged `oversize`, which readers are not
served until it is reviewed. Each site links to its public page, so an operator sees what a
reader sees; a flagged site's page answers 404 by design.

Reads only `GET /admin/v1/sites/review` through `ctx.api`, renders through the shell. No write path:
clearing the `oversize` flag would need an override the builder honours (it sets the flag again on
its next pass while the site still groups that many things), which does not exist yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from web.admin.shell import AdminContext, problem_notice, render, require_operator

router = APIRouter()

#: Proposals named in one audit row's summary before "and N more".
MEMBERS_SHOWN = 6
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
#: `site_audit.kind` as an operator reads it (`services/db/models.py::SITE_AUDIT_KINDS`).
KIND_LABELS: dict[str, str] = {
    "created": "Created",
    "members_gained": "Members gained",
    "members_lost": "Members lost",
    "lead_changed": "Lead changed",
    "split": "Split",
    "merged": "Merged",
    "retired": "Retired",
}


def _names(ids: Any) -> str:
    items = [str(i) for i in ids or []]
    shown = ", ".join(items[:MEMBERS_SHOWN])
    more = len(items) - MEMBERS_SHOWN
    return f"{shown} and {more} more" if more > 0 else shown


def involved(kind: str, detail: Mapping[str, Any]) -> str:
    """The proposals and sites one audit row names, as one line (`services/sites/build.py::_audit`
    writes `members`, `lead`, `from`/`to`, `into` or `successor` per kind)."""
    if kind == "lead_changed":
        return f"{detail.get('from') or 'none'} to {detail.get('to') or 'none'}"
    if kind == "split":
        into = detail.get("into") or {}
        return "; ".join(f"{site}: {_names(members)}" for site, members in sorted(into.items()))
    if kind == "merged":
        return f"from {_names(detail.get('from'))}"
    text = _names(detail.get("members"))
    if kind == "created" and detail.get("lead"):
        text += f" (lead {detail['lead']})"
    if kind == "retired":
        text += f"; successor {detail.get('successor') or 'none'}"
    return text


def _limit(raw: str | None) -> int:
    try:
        n = int(raw) if raw else DEFAULT_LIMIT
    except ValueError:
        n = DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


@router.get("/admin/sites")
def get_sites(request: Request, ctx: Annotated[AdminContext, Depends(require_operator)]) -> Response:
    limit = _limit(request.query_params.get("limit"))
    result = ctx.api.get("/admin/v1/sites/review", params={"limit": str(limit)})
    if result.status_code != 200:
        return render(
            request,
            "admin/sites/review.html",
            {"notice": problem_notice(result), "limit": limit, "audit": [], "flagged": []},
            ctx=ctx,
            nav_key="sites",
            status_code=result.status_code,
        )
    data = result.body.get("data") or {}
    audit = [
        {
            **row,
            "kind_label": KIND_LABELS.get(str(row.get("kind")), str(row.get("kind"))),
            "involved": involved(str(row.get("kind")), row.get("detail") or {}),
        }
        for row in data.get("audit") or []
    ]
    return render(
        request,
        "admin/sites/review.html",
        {"limit": limit, "audit": audit, "flagged": data.get("flagged") or []},
        ctx=ctx,
        nav_key="sites",
    )
