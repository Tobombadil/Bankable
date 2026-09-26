"""The organisation pages (docs/42-backend-review-2026-09-26.md lane L2): `/organizations` and
`/organizations/{ident}`, moved out of `web/app.py` behaviour-preserving, with the helpers the
call graph in that review (§4.1) showed only these two routes reach. Shared page plumbing
(`templates`, `get_api`, JSON-LD, the map/geometry presenters, ...) lives in `web/page.py`, which
this module and `web/app.py` both import -- `web/app.py` includes this router and so cannot be
imported back from here without a cycle.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.api_client import ApiClient, ApiError, ApiNotFound
from web.ownership import (
    DEFAULT_SCOPE,
    GROUP_MAP_MAX_ASSETS,
    SCOPE_PARAM,
    GroupView,
    ancestor_claims,
    group_view,
    parent_claim,
    portfolio_rows,
    resolve_scope,
    scope_links,
)
from web.page import (
    ALL_OPPORTUNITY_STATUSES_CSV,
    ASSET_TYPE_LABELS,
    JSONLD_CONTEXT,
    LINE_ASSET_TYPES,
    _asset_feature,
    _attr,
    _basemap_attribution,
    _geometry_of,
    _line_class,
    _mini_map,
    _number,
    _proposal_feature,
    _sentence_label,
    _states_crossed,
    _tile_mode,
    breadcrumb_jsonld,
    canonical_query,
    canonical_url,
    get_api,
    group_nearby_proposals,
    is_htmx,
    item_list_jsonld,
    jsonld_block,
    not_found_response,
    querystring_without,
    templates,
)
from web.relevance import (
    PARAM as NEARBY_TECHNOLOGY_PARAM,
)
from web.relevance import (
    NearbyFilter,
    load_relevance,
    nearby_notice,
    resolve_nearby_filter,
)
from web.viewmodels import (
    coverage_facts,
    flatten_opportunity,
    flatten_org_asset_row,
    flatten_organization,
    flatten_proposal,
    provenance_panel_rows,
)

router = APIRouter()

#: Company-page map: neither `/v1/organizations/{id}/assets` nor `/v1/assets` embeds geometry
#: (`include_geometry=False`, API lane 2026-09-19), so the page reads it from each asset's own
#: detail response, capped -- the map shows the first N assets, the caption says how many.
#: Aliased to `web/ownership.py::GROUP_MAP_MAX_ASSETS` rather than repeated, because that module
#: drops a *group's* map at exactly this number and cites this constant as the reason (2026-09-20);
#: two copies of the same 40 would let the threshold and its justification drift apart.
ORG_MAP_DETAIL_CAP = GROUP_MAP_MAX_ASSETS
ORG_NEARBY_LIMIT = 50
ORG_ROLE_LABELS = {"operator": "Operates", "owner": "Owns"}
ORG_ROLE_ORDER = ("operator", "owner", "other")


def _count_or(value: Any, fallback: int) -> int:
    """A non-negative integer from an API `totals` field, else `fallback`. Guards the company
    page's "N of M" line against a transport that sends no totals at all."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return fallback
    return count if count >= 0 else fallback


#: `organization.ids` keys (`api/openapi.yaml` `Organization.ids`) -> the `propertyID` a
#: schema.org `PropertyValue` carries. Only identifiers actually stored are emitted.
ORG_IDENTIFIER_LABELS = {
    "lei": "LEI",
    "sam_uei": "SAM UEI",
    "eia_utility_id": "EIA utility id",
    "cik": "SEC CIK",
    "duns": "DUNS",
}


def organization_jsonld(
    request: Request,
    record: Mapping[str, Any],
    *,
    ids: Mapping[str, Any] | None,
    path: str,
    description: str | None = None,
) -> str:
    """schema.org `Organization` for a company page. Name, URL, the identifiers this row actually
    holds, its website as `sameAs`, its country as a `PostalAddress`, and its parent -- nothing
    that is not a stored field (task item 4). `_prune` drops every absent one (through
    `jsonld_block`)."""
    identifiers = [
        {
            "@type": "PropertyValue",
            "propertyID": ORG_IDENTIFIER_LABELS.get(str(key), str(key)),
            "value": str(value),
        }
        for key, value in (ids or {}).items()
        if value not in (None, "")
    ]
    parent_name = record.get("parent_name")
    parent_ident = record.get("parent_slug") or record.get("parent_public_id")
    return jsonld_block(
        {
            "@context": JSONLD_CONTEXT,
            "@type": "Organization",
            "name": record.get("name"),
            "url": canonical_url(request, path),
            "description": description,
            "identifier": identifiers,
            "sameAs": [record["website"]] if record.get("website") else None,
            "address": (
                {"@type": "PostalAddress", "addressCountry": record["country"]}
                if record.get("country")
                else None
            ),
            "parentOrganization": (
                {
                    "@type": "Organization",
                    "name": parent_name,
                    "url": canonical_url(request, f"/organizations/{parent_ident}"),
                }
                if parent_name and parent_ident
                else None
            ),
        }
    )


#: What an organisation typed `other` is called from the assets it holds (task brief: "OTHER" under
#: the name is meaningless for an ethanol producer). Keyed by `asset_type`; the two largest
#: holdings make a two-part descriptor ("Ethanol producer and RNG developer").
ORG_DESCRIPTORS: dict[str, str] = {
    "gas_pipeline": "Gas pipeline operator",
    "gas_processing_plant": "Gas processing operator",
    "gas_storage": "Gas storage operator",
    "lng_terminal": "LNG terminal operator",
    "compressor_station": "Gas pipeline operator",
    "ethanol_plant": "Ethanol producer",
    "biodiesel_plant": "Biodiesel producer",
    "rng_project": "RNG developer",
    "power_plant": "Power plant owner",
    "transmission_line": "Transmission owner",
    "substation": "Transmission owner",
    "refinery": "Refiner",
}


def org_descriptor(org_type: str | None, type_counts: Mapping[str, Any]) -> str | None:
    """`None` unless the organisation's `type` is `other` (or unset) and it holds assets; else the
    descriptor of what it holds most of, joined with the runner-up when there is one. The raw
    type stays in the page's fields table -- this only replaces the header's badge."""
    if org_type not in (None, "", "other"):
        return None
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    order = list(ORG_DESCRIPTORS)
    for asset_type, count in type_counts.items():
        label = ORG_DESCRIPTORS.get(str(asset_type))
        n = int(count) if isinstance(count, int | float) else 0
        if not label or n <= 0 or label in seen:
            continue
        seen.add(label)
        ranked.append((-n, order.index(str(asset_type)), label))
    if not ranked:
        return None
    labels = [label for _, _, label in sorted(ranked)][:2]
    return " and ".join(labels)


def _org_type_counts(asset_counts: Any, groups: list[dict[str, Any]]) -> dict[str, int]:
    """`{asset_type: n}` from the API's `asset_counts.by_type` (any role), else over the page's
    rows -- the same fallback `_org_summary_parts` uses."""
    if isinstance(asset_counts, Mapping):
        by_type = asset_counts.get("by_type")
        if isinstance(by_type, Mapping):
            return {str(k): int(v) for k, v in by_type.items() if isinstance(v, int | float)}
        by_role_and_type = asset_counts.get("by_role_and_type")
        if isinstance(by_role_and_type, Mapping):
            totals: dict[str, int] = {}
            for per_type in by_role_and_type.values():
                if isinstance(per_type, Mapping):
                    for k, v in per_type.items():
                        if isinstance(v, int | float):
                            totals[str(k)] = totals.get(str(k), 0) + int(v)
            return totals
    totals = {}
    for g in groups:
        totals[g["asset_type"]] = totals.get(g["asset_type"], 0) + int(g["count"])
    return totals


# ---- company page: assets by role and type ------------------------------------------------------
def _org_asset_row(row: Mapping[str, Any]) -> dict[str, Any]:
    asset_raw = row.get("asset")
    asset: Mapping[str, Any] = asset_raw if isinstance(asset_raw, Mapping) else row
    out = flatten_org_asset_row(row)
    out.update(
        {
            "operator_name": asset.get("operator_name"),
            "technology": asset.get("technology"),
            "length_miles": _number(_attr(asset, "length_miles", "miles")),
            "states": _states_crossed(asset),
            "state": asset.get("state_code"),
            "line_class": _line_class(asset) if asset.get("asset_type") in LINE_ASSET_TYPES else None,
            "geometry": _geometry_of(asset),
            "provenance": list(asset.get("provenance") or row.get("provenance") or []),
            "held_by": _held_by(row),
        }
    )
    return out


def _held_by(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The subsidiary that actually holds the edge when the page shows a parent's group
    (`held_by` on `/v1/organizations/{id}/assets?include_subsidiaries=true`)."""
    raw = row.get("held_by")
    if not isinstance(raw, Mapping) or not raw.get("public_id"):
        return None
    return {
        "public_id": raw.get("public_id"),
        "slug": raw.get("slug"),
        "name": raw.get("name_canonical") or raw.get("name"),
    }


def _role_key(role: Any) -> str:
    return role if role in ORG_ROLE_LABELS else "other"


def _role_label(role_key: str) -> str:
    return ORG_ROLE_LABELS.get(role_key, "Owns or operates")


def _org_asset_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows grouped by (role, asset type): operator groups first (the discovery asset for a
    pipeline company is "the pipelines they operate"), then owner, then unstated; types in the
    `ASSET_TYPE_LABELS` order. Column flags say which of the optional columns any row fills, so
    a pipeline group shows length and states, a plant group capacity and share."""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (_role_key(row.get("role")), row.get("asset_type") or "power_plant")
        buckets.setdefault(key, []).append(row)
    type_order = list(ASSET_TYPE_LABELS)

    def sort_key(item: tuple[str, str]) -> tuple[int, int]:
        role, asset_type = item
        type_index = type_order.index(asset_type) if asset_type in type_order else len(type_order)
        return ORG_ROLE_ORDER.index(role), type_index

    groups: list[dict[str, Any]] = []
    for role, asset_type in sorted(buckets, key=sort_key):
        members = buckets[(role, asset_type)]
        groups.append(
            {
                "role": role,
                "role_label": _role_label(role),
                "asset_type": asset_type,
                "type_label": _sentence_label(asset_type, plural=True),
                "type_label_singular": _sentence_label(asset_type, plural=False),
                "count": len(members),
                "rows": members,
                "show_capacity": any(m.get("capacity_mw") is not None for m in members),
                "show_length": any(m.get("length_miles") is not None for m in members),
                "show_states": any(m.get("states") or m.get("state") for m in members),
                "show_share": any(m.get("share_pct") is not None for m in members),
                "show_held_by": any(m.get("held_by") for m in members),
            }
        )
    return groups


def _org_summary_parts(asset_counts: Any, groups: list[dict[str, Any]]) -> list[str]:
    """ "Operates 3 gas pipelines · Owns 12 power plants". From the API's `asset_counts` when it
    carries one -- either `{role: {asset_type: n}}` or a flat `{asset_type: n}` -- since the
    page's rows are capped at 100; else counted over the groups on the page."""
    parts: list[tuple[int, int, str]] = []
    type_order = list(ASSET_TYPE_LABELS)
    if isinstance(asset_counts, Mapping):
        # `organization_asset_totals` (services/api/assets.py): `{assets, by_role, by_type,
        # by_role_and_type}` -- the role x type table is what the sentence needs; a flat
        # `by_type` (or a bare `{asset_type: n}`) gives the role-less form.
        if isinstance(asset_counts.get("by_role_and_type"), Mapping):
            asset_counts = asset_counts["by_role_and_type"]
        elif isinstance(asset_counts.get("by_type"), Mapping):
            asset_counts = asset_counts["by_type"]

    def part(role_key: str, asset_type: str, count: int) -> tuple[int, int, str]:
        count = int(count)
        label = _sentence_label(asset_type, plural=count != 1)
        type_index = type_order.index(asset_type) if asset_type in type_order else len(type_order)
        return ORG_ROLE_ORDER.index(role_key), type_index, f"{_role_label(role_key)} {count} {label}"

    if isinstance(asset_counts, Mapping) and asset_counts:
        for key, value in asset_counts.items():
            if isinstance(value, Mapping):
                for asset_type, count in value.items():
                    if isinstance(count, int | float) and count > 0:
                        parts.append(part(_role_key(key), str(asset_type), int(count)))
            elif isinstance(value, int | float) and value > 0:
                parts.append(part("other", str(key), int(value)))
    if not parts:
        parts = [part(g["role"], g["asset_type"], g["count"]) for g in groups]
    return [text for _, _, text in sorted(parts)]


def _org_subsidiaries(entity: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = entity.get("subsidiaries") or entity.get("children") or []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name_canonical") or item.get("name")
        public_id = item.get("public_id")
        if not (name or public_id):
            continue
        out.append({"public_id": public_id, "slug": item.get("slug"), "name": name, "type": item.get("type")})
    return out


def _derived_org_provenance(
    assets: list[dict[str, Any]], proposals: list[dict[str, Any]], opportunities: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The distinct source rows behind an organisation's assets, proposals and opportunities --
    what the company page's Sources panel shows, since an organisation row has none of its own."""
    seen: set[tuple[str | None, str | None]] = set()
    out: list[dict[str, Any]] = []
    for record in [*assets, *proposals, *opportunities]:
        for row in record.get("provenance") or []:
            if not isinstance(row, Mapping):
                continue
            key = (row.get("source_id"), row.get("source_url"))
            if key in seen or not (row.get("source_id") or row.get("source_name")):
                continue
            seen.add(key)
            out.append(dict(row))
    return out


def _resolve_organization(api: ApiClient, ident: str) -> dict[str, Any] | None:
    """Company pages are linked to from two places that may hand this route different kinds of
    identifier: `/search`'s organisations section links by slug (matching every other search
    result on this site), while `asset_detail.html`'s owners table links by the organisation
    `public_id` `docs/23`'s `/v1/assets/{public_id}` owners embed is documented to carry (that
    table row does not promise a `slug` on each owner). Tried as a slug first (the common case,
    one list call); a miss falls back to a direct `public_id` lookup rather than resolving every
    owner row's slug up front for a page that may render dozens of them.
    """
    envelope = api.get("/v1/organizations", params={"slug": ident, "limit": 1})
    entities: list[dict[str, Any]] = envelope["data"]
    public_id = entities[0]["public_id"] if entities else ident
    try:
        # The list row resolves the slug and stops there: `serialize_organization` emits the
        # hierarchy fields only on the detail response, so a page built from the list row has no
        # `parent_edge`, no `ancestors` and no `descendant_count` and silently renders without
        # breadcrumbs, without the ownership provenance and without the scope links. Found
        # 2026-09-20 rendering Trailblazer against the real load, and the same class of bug as the
        # asset page's "No ownership records" on the first Tallgrass screenshots (2026-09-19). One
        # extra call, on a page that already makes four.
        result: dict[str, Any] = api.get(f"/v1/organizations/{public_id}")["data"]
        return result
    except ApiNotFound:
        return entities[0] if entities else None


# ---- /organizations index (docs/00-PLAN.md 2026-09-19 item 4) -----------------------------------
#: Forwarded to `GET /v1/organizations` under the API's own names; `q` is a case-insensitive
#: substring of `name_canonical` (`services/api/app.py::list_organizations`), which is exactly the
#: "searchable by name" this index needs.
ORG_INDEX_FILTERS = ("q", "type", "country")
#: Smaller than the 50-row proposals page on purpose: `GET /v1/organizations` list rows carry no
#: `asset_counts` (only `GET /v1/organizations/{id}` does), so each row costs one extra call.
#: Twenty-five keeps the worst case at 26 upstream calls per render. When the API lane adds
#: `asset_counts` to the list row, `_org_holdings()` loses its second call and this can grow.
ORG_INDEX_PAGE_SIZE = 25


def _org_holdings(api: ApiClient, public_id: str | None) -> dict[str, Any]:
    """What a company-index row says the organisation holds: the `asset_counts` the detail
    response carries, turned into the same descriptor and the same "Operates 3 gas pipelines"
    clauses the company page shows, through `org_descriptor()`/`_org_type_counts()`/
    `_org_summary_parts()` -- the helpers the ownership lane landed, reused rather than copied.
    `group_asset_counts` is preferred where present for the same reason the company page passes
    `include_subsidiaries=true`: a holding parent holds no edge itself, its subsidiaries do.
    A failed or absent count leaves the row rendering name and type alone, never a zero."""
    if not public_id:
        return {"type_counts": {}, "summary_parts": [], "asset_total": None}
    try:
        detail = api.get(f"/v1/organizations/{public_id}")["data"]
    except ApiError:
        return {"type_counts": {}, "summary_parts": [], "asset_total": None}
    counts = detail.get("group_asset_counts") or detail.get("asset_counts")
    type_counts = _org_type_counts(counts, [])
    return {
        "type_counts": type_counts,
        "summary_parts": _org_summary_parts(counts, []),
        "asset_total": sum(type_counts.values()) or None,
        "proposal_count": detail.get("proposal_count"),
        "opportunity_count": detail.get("opportunity_count"),
    }


@router.get("/organizations", response_class=HTMLResponse)
def organizations_list(request: Request) -> HTMLResponse:
    """The crawlable index of companies. Mirrors `/proposals`' markup, pagination and empty state;
    the searchable control is a single name box because `GET /v1/organizations`'s `q` is a name
    substring and promising more than that would be a filter the API cannot honour."""
    api = get_api(request)
    qp = request.query_params
    params: dict[str, str | None] = {name: qp[name] for name in ORG_INDEX_FILTERS if qp.get(name)}
    params["limit"] = str(ORG_INDEX_PAGE_SIZE)
    params["cursor"] = qp.get("cursor")
    envelope = api.get("/v1/organizations", params=params)
    records: list[dict[str, Any]] = []
    for entity in envelope["data"]:
        record = flatten_organization(entity)
        holdings = _org_holdings(api, record["public_id"])
        record.update(holdings)
        record["descriptor"] = org_descriptor(record.get("type"), holdings["type_counts"])
        record["href"] = f"/organizations/{record['slug'] or record['public_id']}"
        records.append(record)
    page = envelope["page"]
    canonical_path = "/organizations" + canonical_query(qp, (*ORG_INDEX_FILTERS, "cursor"))
    context = {
        "records": records,
        "has_more": page["has_more"],
        "next_cursor": page["next_cursor"],
        "prev_cursor": page.get("prev_cursor"),
        "querystring": querystring_without(qp, "cursor"),
        "filters": dict(qp),
        "canonical_path": canonical_path,
        "jsonld": [
            item_list_jsonld(
                request,
                name="Companies",
                description=(
                    "Owners and operators of the assets, proposals and opportunities in the "
                    "Infraque register."
                ),
                path=canonical_path,
                rows=[(r["name"], r["href"]) for r in records if r.get("name")],
            )
        ],
    }
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/_organization_rows.html", context)
    return templates.TemplateResponse(request, "organizations_list.html", context)


@dataclass
class _OrgFetch:
    """The organisation-detail route's upstream reads: everything an `ApiError` on one call
    leaves the others unaffected by (each try/except in the original function, kept separate)."""

    record: dict[str, Any]
    public_id: str | None
    asset_counts: Any
    scope: str
    api_params: dict[str, Any]
    url_params: dict[str, Any]
    scope_meta: Mapping[str, Any]
    assets: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    opportunities: list[dict[str, Any]] = field(default_factory=list)
    technology_vocabulary: list[str] = field(default_factory=list)


def _organization_fetch(api: ApiClient, request: Request, entity: dict[str, Any]) -> _OrgFetch:
    """Everything the company page reads from the API before it can compute anything: the
    organisation's own assets/proposals/opportunities at the requested scope, and the technology
    vocabulary the nearby-proposals filter offers. A failed call empties that one section rather
    than failing the page (docs/04 D-3-style graceful degradation, matching the original)."""
    record = flatten_organization(entity)
    parent_raw = entity.get("parent")
    record["parent_slug"] = parent_raw.get("slug") if isinstance(parent_raw, Mapping) else None
    record["subsidiary_count"] = entity.get("subsidiary_count")
    record["descendant_count"] = _count_or(entity.get("descendant_count"), 0)
    public_id = record["public_id"]
    asset_counts = entity.get("asset_counts")
    # How far down the ownership tree this page reads, from the URL (`?scope=self|children|all`,
    # default `all`). Every level is a plain link: this site's browser tests run with scripts off
    # (docs/04 D-30), so the drill-down is URLs and breadcrumbs, never a control.
    scope = resolve_scope(request.query_params.get(SCOPE_PARAM))
    # Two different "omit the default" rules, and they do not coincide: the API defaults to `self`
    # (an unparameterised integration call must not change meaning), the page defaults to `all`
    # (a holding company's page must not render empty). So `self` is the token the API needs
    # spelled out only when it is not the default, and `all` is the token the URL can drop.
    api_params: dict[str, Any] = {} if scope == "self" else {SCOPE_PARAM: scope}
    url_params: dict[str, Any] = {} if scope == DEFAULT_SCOPE else {SCOPE_PARAM: scope}
    fetch = _OrgFetch(
        record=record,
        public_id=public_id,
        asset_counts=asset_counts,
        scope=scope,
        api_params=api_params,
        url_params=url_params,
        scope_meta={},
    )
    try:
        # A parent such as Tallgrass Energy holds no `asset_owner` edge itself; the subsidiaries
        # do (curated parents, services/ingest/midstream.py), and above it a holding company holds
        # nothing either, so the page's default scope is the whole descent rather than one level.
        assets_env = api.get(
            f"/v1/organizations/{public_id}/assets",
            params={"limit": 100, **api_params},
        )
        fetch.assets = [_org_asset_row(r) for r in assets_env["data"]]
        raw_totals = assets_env.get("totals")
        if isinstance(raw_totals, Mapping):
            fetch.asset_counts = raw_totals
        raw_scope = assets_env.get("scope")
        if isinstance(raw_scope, Mapping):
            fetch.scope_meta = raw_scope
    except ApiError:
        fetch.assets = []
    try:
        proposals_env = api.get(
            f"/v1/organizations/{public_id}/proposals", params={"limit": 100, **api_params}
        )
        fetch.proposals = [flatten_proposal(e) for e in proposals_env["data"]]
    except ApiError:
        fetch.proposals = []
    try:
        opportunities_env = api.get(
            f"/v1/organizations/{public_id}/opportunities",
            params={"limit": 100, "status": ALL_OPPORTUNITY_STATUSES_CSV},
        )
        fetch.opportunities = [flatten_opportunity(e) for e in opportunities_env["data"]]
    except ApiError:
        fetch.opportunities = []
    try:
        fetch.technology_vocabulary = [
            v["value"] for v in api.get("/v1/meta/vocabularies")["data"]["technology"]
        ]
    except (ApiError, KeyError, TypeError):
        fetch.technology_vocabulary = []
    return fetch


@dataclass
class _OrgCompose:
    """Everything derived from `_OrgFetch`, ready for the template context: grouped assets, the
    ownership-tree view, the map, the nearby-proposals list and its notice, and the provenance
    panel. One dataclass per phase (matching `_OrgFetch`) rather than a growing tuple, since the
    render step reads these by name."""

    groups: list[dict[str, Any]]
    view: GroupView
    portfolio: Any
    mini_map: dict[str, Any] | None
    tile_url: str | None
    tile_mode: str
    nearby: list[dict[str, Any]]
    nearby_filter: NearbyFilter
    nearby_notice: dict[str, Any] | None
    descriptor: str | None
    provenance: list[dict[str, Any]]
    provenance_note: str | None
    base_path: str
    path: str


@dataclass
class _OrgMap:
    """The map and the nearby-proposals list that go with it -- split out of `_organization_compose`
    only because building the map (filling in missing asset geometry, then the nearby-proposals
    call that appends to the same `features` list) is one contiguous sub-step of it."""

    nearby: list[dict[str, Any]]
    nearby_filter: NearbyFilter
    nearby_shown: int
    nearby_total: int
    mini_map: dict[str, Any] | None
    tile_url: str | None
    tile_mode: str


def _organization_map(
    request: Request, api: ApiClient, fetch: _OrgFetch, view: GroupView, groups: list[dict[str, Any]]
) -> _OrgMap:
    """The map of everything the organisation owns or operates: geometry from the asset rows when
    the API embeds it, else each asset's own detail response (capped, see ORG_MAP_DETAIL_CAP);
    plus the nearby-proposals features the caption and the mini-map both need."""
    if view.show_map:
        capped = [a for a in fetch.assets if not a.get("geometry") and a.get("public_id")]
        for a in capped[:ORG_MAP_DETAIL_CAP]:
            try:
                a["geometry"] = _geometry_of(api.get(f"/v1/assets/{a['public_id']}")["data"])
            except ApiError:
                continue
    features = (
        [_asset_feature(a, a["geometry"]) for a in fetch.assets if a.get("geometry")] if view.show_map else []
    )
    nearby, nearby_shown, nearby_total, nearby_filter = _organization_nearby(
        request, api, fetch, features, groups
    )
    tile_url = (os.environ.get("MAP_TILE_URL") or "").strip() or None
    tile_mode = _tile_mode(tile_url)
    mapped = sum(1 for f in features if f["properties"]["kind"] == "asset")
    unmapped = len(fetch.assets) - mapped
    plural = "s" if len(nearby) != 1 else ""
    caption = (
        f"{mapped} of {len(fetch.assets)} asset{'s' if len(fetch.assets) != 1 else ''} with a mapped location"
        + (f"; {unmapped} without one {'is' if unmapped == 1 else 'are'} listed below" if unmapped else "")
        + (f", and {len(nearby)} exact-grade proposal{plural} within 25 km" if nearby else "")
        + ". "
        + _basemap_attribution(tile_mode, tile_url)
    )
    mini_map = (
        _mini_map(features, label=f"Map of assets of {fetch.record['name']}", caption=caption)
        if view.show_map
        else None
    )
    return _OrgMap(
        nearby=nearby,
        nearby_filter=nearby_filter,
        nearby_shown=nearby_shown,
        nearby_total=nearby_total,
        mini_map=mini_map,
        tile_url=tile_url,
        tile_mode=tile_mode,
    )


def _organization_compose(
    request: Request, api: ApiClient, entity: dict[str, Any], fetch: _OrgFetch
) -> _OrgCompose:
    """The company page's own logic over `_organization_fetch`'s rows: how this node renders
    (company vs. portfolio, `web/ownership.py::group_view`), the map (`_organization_map`, which
    also makes the nearby-proposals call once the asset groups it depends on exist) and the
    provenance panel."""
    groups = _org_asset_groups(fetch.assets)
    # A fund-level page is not a company page (owner brief, 2026-09-20): `web/ownership.py`
    # decides from the API's own counts whether this node renders as a company (map plus a flat
    # asset table) or as a portfolio of companies, and says in words why anything is absent.
    view = group_view(
        scope=fetch.scope, totals=fetch.asset_counts, scope_meta=fetch.scope_meta, asset_rows=fetch.assets
    )
    portfolio = portfolio_rows(fetch.asset_counts, subject_public_id=fetch.public_id)
    m = _organization_map(request, api, fetch, view, groups)
    # An organisation row carries no provenance of its own (`serialize_organization` emits []);
    # the panel shows the sources of its assets and proposals, or nothing -- never the "withheld
    # under licence" empty state, which would be a false licence claim (owner brief, 2026-09-19).
    provenance = fetch.record["provenance"] or _derived_org_provenance(
        fetch.assets, fetch.proposals, fetch.opportunities
    )
    provenance_note = (
        None
        if fetch.record["provenance"]
        else (
            "The registers behind this organisation's assets, proposals and opportunities; "
            "the organisation record itself is derived from them."
        )
    )
    descriptor = org_descriptor(fetch.record.get("type"), _org_type_counts(fetch.asset_counts, groups))
    base_path = f"/organizations/{fetch.record['slug'] or fetch.record['public_id']}"
    # The scope belongs in every link the page emits: the technology filter's "show all" undo, the
    # form action and the canonical URL. Dropping it would silently walk the reader back up to the
    # default scope when they changed something else.
    path = base_path if not fetch.url_params else f"{base_path}?{SCOPE_PARAM}={fetch.scope}"
    notice = nearby_notice(
        m.nearby_filter,
        shown=m.nearby_shown,
        total=m.nearby_total,
        path=base_path,
        listed_cap=ORG_NEARBY_LIMIT,
        listed_projects=len(m.nearby),
        keep=fetch.url_params,
    )
    return _OrgCompose(
        groups=groups,
        view=view,
        portfolio=portfolio,
        mini_map=m.mini_map,
        tile_url=m.tile_url,
        tile_mode=m.tile_mode,
        nearby=m.nearby,
        nearby_filter=m.nearby_filter,
        nearby_notice=notice,
        descriptor=descriptor,
        provenance=provenance,
        provenance_note=provenance_note,
        base_path=base_path,
        path=path,
    )


def _organization_nearby(
    request: Request,
    api: ApiClient,
    fetch: _OrgFetch,
    features: list[dict[str, Any]],
    groups: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int, NearbyFilter]:
    """ "Proposals near those pipelines" (owner, 2026-09-19): exact-grade proposals within 25 km of
    any of the organisation's assets, each at its distance to the nearest one, which is named.
    Narrowed by default to the technologies the company's own asset types make relevant
    (`data/vendored/relevance/asset_technology_relevance.yaml`, owner 2026-09-20: "given
    tallgrass doesnt do solar, I dont want them seeing solar"). Mutates `features` in place (the
    map the caller is building) exactly as the original inline block did. `groups` is the caller's
    already-computed `_org_asset_groups(fetch.assets)`, passed in rather than recomputed."""
    # The PR #8 relevance filter keys off the asset types held *in this scope*, so it stays
    # correct at every level of the tree: the whole group's types at `scope=all`, one company's at
    # `scope=self`. `asset_counts` is the scoped totals block the assets call just returned.
    relevance_default = load_relevance().default_for(_org_type_counts(fetch.asset_counts, groups))
    nearby_filter = resolve_nearby_filter(
        request.query_params.get(NEARBY_TECHNOLOGY_PARAM),
        default=relevance_default,
        vocabulary=fetch.technology_vocabulary or None,
    )
    nearby: list[dict[str, Any]] = []
    nearby_totals: Mapping[str, Any] = {}
    try:
        nearby_params: dict[str, Any] = {"limit": ORG_NEARBY_LIMIT, **fetch.api_params}
        if nearby_filter.technologies:
            nearby_params["technology"] = ",".join(nearby_filter.technologies)
        nearby_env = api.get(f"/v1/organizations/{fetch.public_id}/nearby-proposals", params=nearby_params)
        raw_totals = nearby_env.get("totals")
        nearby_totals = raw_totals if isinstance(raw_totals, Mapping) else {}
        for e in nearby_env["data"]:
            flat = flatten_proposal(e)
            flat["distance_km"] = _number(e.get("distance_km"))
            nearest_raw = e.get("nearest_asset")
            nearest: Mapping[str, Any] = nearest_raw if isinstance(nearest_raw, Mapping) else {}
            flat["nearest_asset_name"] = nearest.get("name")
            flat["nearest_asset_slug"] = nearest.get("slug")
            nearby.append(flat)
            geometry = _geometry_of(e)
            if geometry is not None:
                features.append(_proposal_feature(flat, geometry))
    except ApiError:
        nearby = []
    # Counts for the "N of M" line come from the API's `totals`, never from the rendered rows:
    # `limit` caps the page and `group_nearby_proposals` collapses a project's generator units, so
    # the row count is neither the matched count nor the total.
    nearby_shown = _count_or(nearby_totals.get("proposals_within_radius"), len(nearby))
    nearby_total = _count_or(nearby_totals.get("proposals_within_radius_unfiltered"), nearby_shown)
    return group_nearby_proposals(nearby), nearby_shown, nearby_total, nearby_filter


def _organization_render(
    request: Request, api: ApiClient, entity: dict[str, Any], fetch: _OrgFetch, compose: _OrgCompose
) -> HTMLResponse:
    """The template context, unchanged from the original function's final `TemplateResponse` --
    split out only so the fetch and compose steps above are not entangled with it."""
    record = fetch.record
    return templates.TemplateResponse(
        request,
        "organization_detail.html",
        {
            "record": record,
            "assets": fetch.assets,
            "asset_groups": compose.groups,
            "summary_parts": _org_summary_parts(fetch.asset_counts, compose.groups),
            "descriptor": compose.descriptor,
            "canonical_path": compose.path,
            "jsonld": [
                breadcrumb_jsonld(
                    request,
                    [("Home", "/"), ("Companies", "/organizations"), (record["name"], compose.path)],
                ),
                organization_jsonld(
                    request,
                    record,
                    ids=entity.get("ids") if isinstance(entity.get("ids"), Mapping) else None,
                    path=compose.path,
                    description=compose.descriptor,
                ),
            ],
            "subsidiaries": _org_subsidiaries(entity),
            "ancestors": ancestor_claims(entity),
            "parent_claim": parent_claim(entity),
            # Rendered only when this company has no parent edge: an empty parent field reads as
            # "independent", and 96% of the time it means "we have no parent record". The
            # numbers come from the measured coverage statement, never a hard-coded figure.
            "ownership_coverage": (
                None if parent_claim(entity) else coverage_facts(request, api).get("ownership")
            ),
            "portfolio": compose.portfolio,
            "group_view": compose.view,
            "scope_links": scope_links(
                compose.base_path, fetch.scope, descendant_count=record["descendant_count"]
            ),
            "scope": fetch.scope,
            "nearby_proposals": compose.nearby,
            "nearby_filter": compose.nearby_filter,
            "nearby_notice": compose.nearby_notice,
            "nearby_technologies": fetch.technology_vocabulary,
            "nearby_param": NEARBY_TECHNOLOGY_PARAM,
            "proposals": fetch.proposals,
            "opportunities": fetch.opportunities,
            "provenance_rows": provenance_panel_rows(api, compose.provenance) if compose.provenance else [],
            "provenance_note": compose.provenance_note,
            "mini_map": compose.mini_map,
            "tile_url": compose.tile_url,
            "tile_mode": compose.tile_mode,
        },
    )


@router.get("/organizations/{ident}", response_class=HTMLResponse)
def organization_detail(request: Request, ident: str) -> HTMLResponse:
    """ADR 0008 company page: where this organisation sits in the ownership tree, and at whatever
    level of it the URL asks for, the scope's assets, the proposals its companies sponsor, the
    nearby proposals and the provenance behind all of them.

    `?scope=self|children|all` is the whole drill-down: breadcrumbs up the ancestor chain, links
    down into the portfolio, three scope links across. No JavaScript is involved in any of it
    (docs/04 D-30; `web/test_e2e.py` runs with scripts off), which is why the scope also rides as
    a hidden field on the technology filter's GET form -- a browser replaces the query string on
    submit and would otherwise walk the reader back to the default level.

    Split into fetch (`_organization_fetch`) / compose (`_organization_compose`, which also makes
    the nearby-proposals call once the asset groups it depends on exist) / render
    (`_organization_render`) -- docs/42-backend-review-2026-09-26.md §4.1's phase map.
    """
    api = get_api(request)
    entity = _resolve_organization(api, ident)
    if entity is None:
        return not_found_response(request, "organisation")
    fetch = _organization_fetch(api, request, entity)
    compose = _organization_compose(request, api, entity, fetch)
    return _organization_render(request, api, entity, fetch, compose)
