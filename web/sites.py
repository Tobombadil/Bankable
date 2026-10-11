"""Site pages (owner decision 2026-10-10, docs/51 §7 Q6; docs/21 §3.25): `/sites/{public_id}` and the
"At this site" panel on a proposal page, both read from `GET /v1/sites/{public_id}`.

The API has already applied the proposal predicate to every member, neighbour, count and total, and
re-run the lead and labels when the stored lead is hidden from the caller; the page adds wording and
formatting only, so it can never print a member the API withheld. A record with no site, or whose
site has fewer than two members this viewer may see, gets no panel (the embed is `null`). When the
members a viewer may see are not all linked to each other without a hidden one (lane S2), the API
serves one linked group: the panel asks for its own record's group (`?member=`), the site page gets
the largest, and both say that other records of the site keep their own pages (`partial`).

A site page view is counted like the other detail pages (`page.viewed`, `page_type = site`).

The panel is a template global (`site_panel`), the way `source_freshness` is (`web/auth.py`), so the
proposal page needs one `{% include "_site_panel.html" %}` line and no change to its route. It costs
one extra detail call on every proposal page and a second only when the record has a site; any
failure drops the panel, never the page.

Neighbours at the same interconnection point are never members: they are listed apart, collapsed,
as "Also at this interconnection point".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from services.sites.switch import sites_enabled
from web import formatting
from web.api_client import ApiError, ApiNotFound
from web.page import (
    breadcrumb_jsonld,
    count_page_view,
    get_api,
    not_found_response,
    templates,
    unavailable_response,
)
from web.viewmodels import lifecycle_family, page_credits, provenance_panel_rows, web_relative_url

router = APIRouter()

#: How a member relates to the site's lead, as a reader-facing phrase ("member <relation> lead").
RELATION_LABELS: dict[str, str] = {
    "lead": "Lead filing",
    "unit_of": "Unit of the same plant",
    "phase_of": "Another phase",
    "co_located": "Co-located, other technology",
    "refiling_of": "Later re-filing, withdrawn",
    "superseded_by": "Earlier filing, superseded",
    "expansion_of": "Expansion",
    "expanded_by": "Operating plant being expanded",
    "same_site": "Same site, relation unclear",
}
#: Why the records were grouped, one phrase per grouping rule.
GROUPING_LABELS: dict[str, str] = {
    "eia_plant": "the same EIA plant id",
    "exact_point_sponsor": "the same exact location and developer",
    "exact_point_stem": "the same exact location and project name",
    "poi_sponsor": "the same grid connection point and developer",
    "poi_stem": "the same grid connection point and project name",
}
_API_FAILURES = (ApiError, httpx.HTTPError, KeyError, TypeError, ValueError)
#: The proposal page's panel lists every unit of a plant group up to this many records in the site;
#: above it (Project Matador has 161) each group shows its head and a unit count, and the site page
#: lists them all.
PANEL_UNIT_LIMIT = 20


def _mw(value: Any) -> str:
    """docs/31 §4 capacity format (`web/formatting.py::mw`)."""
    try:
        return formatting.mw(float(value))
    except (TypeError, ValueError):
        return "—"


def _relation_label(row: Mapping[str, Any]) -> str:
    relation = str(row.get("relation") or "")
    plants = (row.get("group") or {}).get("eia_plant_ids") or []
    if relation == "unit_of" and row.get("parent_public_id") and plants:
        return f"Unit of EIA plant {', '.join(plants)}"
    return RELATION_LABELS.get(relation, relation.replace("_", " ").capitalize())


def flatten_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A member or neighbour row as the templates read it."""
    relation = str(row.get("relation") or "")
    return {
        "public_id": row.get("public_id"),
        "href": web_relative_url(row.get("url")) or f"/proposals/{row.get('slug')}",
        "name": row.get("name_canonical"),
        "technology": row.get("technology"),
        "capacity_mw": row.get("capacity_mw"),
        "mw_label": _mw(row.get("capacity_mw")) if row.get("capacity_mw") is not None else "—",
        "lifecycle_state": row.get("lifecycle_state"),
        "lifecycle_family": lifecycle_family(row.get("lifecycle_state")),
        "is_lead": bool(row.get("is_lead")),
        "parent_public_id": row.get("parent_public_id"),
        "is_unit": bool(row.get("parent_public_id")),
        "eia_plant_ids": list((row.get("group") or {}).get("eia_plant_ids") or []),
        "sponsor": row.get("sponsor"),
        "sponsor_href": web_relative_url((row.get("sponsor") or {}).get("url")),
        "relation": relation,
        "relation_label": _relation_label(row),
        "confidence": row.get("confidence"),
        "grouping_label": GROUPING_LABELS.get(str(row.get("grouping_rule") or "")),
        "point": row.get("interconnection_point"),
        "point_href": web_relative_url((row.get("interconnection_point") or {}).get("url")),
        "provenance": row.get("provenance") or [],
    }


def flatten_site(entity: Mapping[str, Any]) -> dict[str, Any]:
    totals = entity.get("totals") or {}
    anchors = entity.get("anchors") or {}
    members = [flatten_row(m) for m in entity.get("members") or []]
    neighbours = [flatten_row(n) for n in entity.get("shares_interconnection_point") or []]
    groupings = sorted({m["grouping_label"] for m in members if m["grouping_label"]})
    groups: list[dict[str, Any]] = []
    for m in members:
        if not m["is_unit"] or not groups:
            groups.append({"head": m, "units": []})
        else:
            groups[-1]["units"].append(m)
    return {
        "public_id": entity.get("public_id"),
        "href": f"/sites/{entity.get('public_id')}",
        "name": entity.get("name"),
        "member_count": entity.get("member_count") or len(members),
        "partial": bool(entity.get("partial")),
        "members": members,
        "members_truncated": bool(entity.get("members_truncated")),
        "lead": members[0] if members else None,
        "groupings": groupings,
        "groups": groups,
        "sponsors": [
            {**o, "href": web_relative_url(o.get("url")) or f"/organizations/{o.get('slug')}"}
            for o in entity.get("sponsors") or []
        ],
        "totals": totals,
        "total_mw_label": _mw(totals.get("total_mw")),
        "active_mw_label": _mw(totals.get("active_mw")),
        "withdrawn_mw_label": _mw(totals.get("withdrawn_mw")),
        "built_mw_label": _mw(totals.get("built_mw")),
        "eia_plant_ids": list(anchors.get("eia_plant_ids") or []),
        "points": [
            {**p, "href": web_relative_url(p.get("url")) or f"/interconnection-points/{p.get('public_id')}"}
            for p in anchors.get("interconnection_points") or []
        ],
        "assets": [
            {
                **a,
                "href": web_relative_url(a.get("url")) or f"/assets/{a.get('slug')}",
                "owners": [
                    {
                        "name": (o.get("organization") or {}).get("name_canonical"),
                        "href": web_relative_url((o.get("organization") or {}).get("url")),
                        "role": o.get("role"),
                    }
                    for o in a.get("owners") or []
                ],
            }
            for a in anchors.get("assets") or []
        ],
        "neighbours": neighbours,
        "neighbour_count": entity.get("shares_interconnection_point_count") or len(neighbours),
    }


def site_panel(request: Request, record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The "At this site" panel for a proposal page, or None (no site on this tier, or any failure)."""
    public_id = record.get("public_id") if isinstance(record, Mapping) else None
    if not public_id or not sites_enabled():
        return None
    api = get_api(request)
    try:
        embed = api.get(f"/v1/proposals/{public_id}")["data"].get("site")
        if not embed:
            return None
        # This record's own linked group, which is not always the site page's largest one.
        envelope = api.get(f"/v1/sites/{embed['public_id']}", params={"member": public_id})
        site = flatten_site(envelope["data"])
    except _API_FAILURES:
        return None
    site["current"] = public_id
    site["show_units"] = (site["member_count"] or 0) <= PANEL_UNIT_LIMIT
    site["relation_label"] = RELATION_LABELS.get(str(embed.get("relation")), None)
    site["credits"] = page_credits(envelope)
    return site


templates.env.globals["site_panel"] = site_panel


@router.get("/sites/{public_id}", response_class=HTMLResponse)
def site_detail(request: Request, public_id: str) -> Response:
    if not sites_enabled():
        return not_found_response(request, "site")
    api = get_api(request)
    try:
        envelope = api.get(f"/v1/sites/{public_id}")
    except ApiNotFound:
        return not_found_response(request, "site")
    except ApiError:
        return unavailable_response(request)
    site = flatten_site(envelope["data"])
    path = f"/sites/{site['public_id']}"
    if site["public_id"] != public_id:
        # A retired site's id: the API answered with its successor (the client follows the 301).
        return RedirectResponse(path, status_code=301)
    provenance: list[dict[str, Any]] = []
    for row in [*site["members"], *site["neighbours"]]:
        provenance.extend(row["provenance"])
    response = templates.TemplateResponse(
        request,
        "site_detail.html",
        {
            "site": site,
            "provenance_rows": provenance_panel_rows(api, provenance),
            "credits": page_credits(envelope),
            "canonical_path": path,
            "jsonld": [
                breadcrumb_jsonld(request, [("Home", "/"), ("Proposals", "/proposals"), (site["name"], path)])
            ],
        },
    )
    return count_page_view(request, response, "site")
