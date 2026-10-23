"""The organisation pages (docs/42-backend-review-2026-09-26.md lane L2): `/organizations` and
`/organizations/{ident}`, moved out of `web/app.py` behaviour-preserving, with the helpers the
call graph in that review (§4.1) showed only these two routes reach. Shared page plumbing
(`templates`, `get_api`, JSON-LD, the map/geometry presenters, ...) lives in `web/page.py`, which
this module and `web/app.py` both import -- `web/app.py` includes this router and so cannot be
imported back from here without a cycle.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import QueryParams

from web import formatting
from web.api_client import ApiClient, ApiError, ApiNotFound
from web.labels import REGION_NAMES
from web.list_sort import SortColumn, chosen_sort, column, sort_caption, sort_headers
from web.org_pipeline import ProposalPages, fetch_pipeline, fetch_proposals, pipeline_summary
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
    count_page_view,
    get_api,
    group_nearby_proposals,
    is_htmx,
    item_list_jsonld,
    jsonld_block,
    not_found_response,
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
    ORG_CREDITS_NOTE,
    coverage_facts,
    flatten_opportunity,
    flatten_org_asset_row,
    flatten_organization,
    flatten_proposal,
    page_credits,
    provenance_panel_rows,
    source_label,
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
#: Rows the Proposals section lists; the pipeline summary above it counts all of them.
ORG_PROPOSALS_LISTED = 100
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
    same_as: list[str] | None = None,
) -> str:
    """schema.org `Organization` for a company page. Name, URL, the identifiers this row actually
    holds, its website (and `same_as`, the Wikipedia article a profile links) as `sameAs`, its
    country as a `PostalAddress`, and its parent -- nothing that is not a stored field (task item
    4). `_prune` drops every absent one (through `jsonld_block`)."""
    links = [record["website"]] if record.get("website") else []
    links += [link for link in same_as or [] if link and link not in links]
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
            "sameAs": links or None,
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
            # The register that states this holding (`edge_provenance`), for the scope's share.
            "edge_source_id": (row.get("edge_provenance") or {}).get("source_id")
            if isinstance(row.get("edge_provenance"), Mapping)
            else None,
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


def _asset_key(row: Mapping[str, Any]) -> Any:
    return row.get("public_id") or row.get("slug") or id(row)


def distinct_assets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first row of each asset: the API lists one row per holding (asset, holder, role, register),
    so a plant two companies of a group hold, or one register states twice, is several rows."""
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = _asset_key(row)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _group_share(holdings: list[dict[str, Any]]) -> float | None:
    """One asset's stated share for the scope, from its holdings: per register, each holder's share
    once, summed; the largest register's sum, capped at 100% (decision 2026-10-21: EIA-860 states a
    subsidiary's stake and EPA GHGRP the parent's, the same stake at two levels). None when no
    holding states a share."""
    by_register: dict[Any, dict[Any, float]] = {}
    for row in holdings:
        share = _number(row.get("share_pct"))
        if share is None:
            continue
        holder = (row.get("held_by") or {}).get("public_id")
        shares = by_register.setdefault(row.get("edge_source_id"), {})
        shares[holder] = max(shares.get(holder, 0.0), share)
    if not by_register:
        return None
    return min(100.0, max(sum(shares.values()) for shares in by_register.values()))


def _merge_holdings(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One table row per asset within a (role, type) group, with every company of the scope that
    holds it in that role (`holders`) and the scope's share (`_group_share`)."""
    by_asset: dict[Any, list[dict[str, Any]]] = {}
    for row in members:
        by_asset.setdefault(_asset_key(row), []).append(row)
    merged: list[dict[str, Any]] = []
    for holdings in by_asset.values():
        row = dict(holdings[0])
        holders: dict[Any, dict[str, Any]] = {}
        for h in holdings:
            held_by = h.get("held_by")
            if held_by and held_by.get("public_id") not in holders:
                holders[held_by.get("public_id")] = held_by
        row["holders"] = list(holders.values())
        row["share_pct"] = _group_share(holdings)
        row["holdings"] = len(holdings)
        merged.append(row)
    return merged


def _org_asset_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows grouped by (role, asset type): operator groups first (the discovery asset for a
    pipeline company is "the pipelines they operate"), then owner, then unstated; types in the
    `ASSET_TYPE_LABELS` order. Within a group one row per asset (`_merge_holdings`), so `count` is
    distinct assets, the figure the companies index and the API's portfolio count; an asset held in
    two roles is in both groups. Column flags say which of the optional columns any row fills, so
    a pipeline group shows length and states, a plant group capacity and share."""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (_role_key(row.get("role")), row.get("asset_type") or "power_plant")
        buckets.setdefault(key, []).append(row)
    buckets = {key: _merge_holdings(members) for key, members in buckets.items()}
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
                # The scope's distinct assets of this role and type when the page reads fewer rows
                # than the API holds (`_with_totals`); `count` otherwise.
                "total": len(members),
                "rows": members,
                "show_capacity": any(m.get("capacity_mw") is not None for m in members),
                "show_length": any(m.get("length_miles") is not None for m in members),
                "show_states": any(m.get("states") or m.get("state") for m in members),
                "show_share": any(m.get("share_pct") is not None for m in members),
                "show_held_by": any(m.get("holders") for m in members),
            }
        )
    return groups


def _distinct_role_type_counts(asset_counts: Any) -> dict[tuple[str, str], int]:
    """`{(role, asset_type): distinct assets}` from the API's assets `totals`. `by_role_and_type`
    counts holdings (one per company of the group that holds the asset, so Vistra's group reads 56
    for 51 plants on the 2026-10-20 store), while `by_role` and `by_type` count distinct assets; a
    cell can be no larger than either, and with one role or one type that bound is the exact count."""
    if not isinstance(asset_counts, Mapping):
        return {}
    table = asset_counts.get("by_role_and_type")
    if not isinstance(table, Mapping):
        return {}
    by_role = asset_counts.get("by_role")
    by_type = asset_counts.get("by_type")
    by_role = by_role if isinstance(by_role, Mapping) else {}
    by_type = by_type if isinstance(by_type, Mapping) else {}
    out: dict[tuple[str, str], int] = {}
    for role, per_type in table.items():
        if not isinstance(per_type, Mapping):
            continue
        for asset_type, n in per_type.items():
            if not isinstance(n, int | float) or n <= 0:
                continue
            bounds = [int(n)]
            for bound in (by_role.get(role), by_type.get(asset_type)):
                if isinstance(bound, int | float):
                    bounds.append(int(bound))
            key = (_role_key(role), str(asset_type))
            out[key] = out.get(key, 0) + min(bounds)
    return out


def _with_totals(groups: list[dict[str, Any]], asset_counts: Any, *, complete: bool) -> list[dict[str, Any]]:
    """Each group's `total`: the rows' own count when the page read every row; else the API's
    distinct count for that role and type (`_distinct_role_type_counts`), so the heading of a
    121-asset group says 57 RNG projects, not the 36 of them on the first page."""
    if complete:
        return groups
    totals = _distinct_role_type_counts(asset_counts)
    for g in groups:
        g["total"] = max(g["count"], totals.get((g["role"], g["asset_type"]), g["count"]))
    return groups


def _org_summary_parts(
    asset_counts: Any, groups: list[dict[str, Any]], *, complete: bool = False
) -> list[str]:
    """ "Operates 3 gas pipelines · Owns 12 power plants", in distinct assets: counted over the
    page's groups (one row per asset) when the page read every row (`complete`) or `asset_counts`
    is None (a narrowed section); else from the API's `asset_counts`, since the rows are capped at
    100 -- the role x type table bounded to distinct assets (`_distinct_role_type_counts`), or a flat
    `{asset_type: n}` for the role-less form."""
    parts: list[tuple[int, int, str]] = []
    type_order = list(ASSET_TYPE_LABELS)

    def part(role_key: str, asset_type: str, count: int) -> tuple[int, int, str]:
        count = int(count)
        label = _sentence_label(asset_type, plural=count != 1)
        type_index = type_order.index(asset_type) if asset_type in type_order else len(type_order)
        return ORG_ROLE_ORDER.index(role_key), type_index, f"{_role_label(role_key)} {count} {label}"

    if isinstance(asset_counts, Mapping) and not (complete and groups):
        # `organization_asset_totals` (services/api/assets.py): `{assets, by_role, by_type,
        # by_role_and_type}` -- the role x type table is what the sentence needs; a flat
        # `by_type` (or a bare `{asset_type: n}`) gives the role-less form.
        if isinstance(asset_counts.get("by_role_and_type"), Mapping):
            cells = _distinct_role_type_counts(asset_counts)
            parts = [part(role, asset_type, n) for (role, asset_type), n in cells.items() if n > 0]
        else:
            by_type = asset_counts.get("by_type")
            flat: Mapping[str, Any] = by_type if isinstance(by_type, Mapping) else asset_counts
            for key, value in flat.items():
                if isinstance(value, Mapping):
                    for asset_type, count in value.items():
                        if isinstance(count, int | float) and count > 0:
                            parts.append(part(_role_key(key), str(asset_type), int(count)))
                elif isinstance(value, int | float) and value > 0:
                    parts.append(part("other", str(key), int(value)))
    if not parts:
        parts = [part(g["role"], g["asset_type"], g.get("total", g["count"])) for g in groups]
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


# ---- company page header: the public company facts (lane org-enrich's `profile`; lane pages) ------
#: Country names for the headquarters line (ISO 3166-1 alpha-2); a code not listed prints as itself.
COUNTRY_NAMES: dict[str, str] = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AT": "Austria", "AU": "Australia", "BE": "Belgium",
    "BR": "Brazil", "CA": "Canada", "CH": "Switzerland", "CL": "Chile", "CN": "China", "CZ": "Czechia",
    "DE": "Germany", "DK": "Denmark", "ES": "Spain", "FI": "Finland", "FR": "France", "GB": "United Kingdom",
    "GR": "Greece", "HK": "Hong Kong", "IE": "Ireland", "IL": "Israel", "IN": "India", "IT": "Italy",
    "JP": "Japan", "KR": "South Korea", "LU": "Luxembourg", "MX": "Mexico", "NL": "Netherlands",
    "NO": "Norway", "NZ": "New Zealand", "PL": "Poland", "PT": "Portugal", "QA": "Qatar",
    "SA": "Saudi Arabia", "SE": "Sweden", "SG": "Singapore", "TW": "Taiwan", "US": "United States",
    "ZA": "South Africa",
}  # fmt: skip


def _fact_source(fact: Mapping[str, Any]) -> dict[str, Any] | None:
    """The source line of one profile fact: the register's short name, linked to the statement the
    value was read from, with its credit and the date it was read as the tooltip."""
    prov = fact.get("provenance")
    if not isinstance(prov, Mapping):
        return None
    source_id = prov.get("source_id")
    credit = str(prov.get("attribution_text") or "").rstrip(".")
    read = formatting.display_date(prov.get("retrieved_at"))
    title = ". ".join(part for part in (credit, f"Read {read}" if read else "") if part)
    return {
        "name": source_label(source_id) or prov.get("source_name") or source_id,
        "url": prov.get("source_url"),
        "title": (title + ".") if title else None,
    }


#: The profile facts the page prints (`organization_profile_view`), in the order it prints them.
PROFILE_FACTS = ("description", "website", "headquarters", "ticker", "lei", "wikipedia")


def profile_provenance(profile: Any) -> list[dict[str, Any]]:
    """One Sources-panel row per register behind the facts printed under the company's name
    (Wikidata, SEC EDGAR), linked to the record read (a Wikidata item, without the property
    anchor), so their credits are on the page as text, not only in a source link's tooltip."""
    if not isinstance(profile, Mapping):
        return []
    rows: dict[Any, dict[str, Any]] = {}
    for name in PROFILE_FACTS:
        fact = profile.get(name)
        if not isinstance(fact, Mapping) or fact.get("value") in (None, "", {}):
            continue
        prov = fact.get("provenance")
        if not isinstance(prov, Mapping) or not prov.get("source_id") or prov["source_id"] in rows:
            continue
        row = dict(prov)
        if isinstance(row.get("source_url"), str):
            row["source_url"] = row["source_url"].split("#", 1)[0]
        rows[prov["source_id"]] = row
    return list(rows.values())


def _website_text(url: str) -> str:
    """`https://www.duke-energy.com/` -> `duke-energy.com`: the address a reader recognises."""
    text = url.split("://", 1)[-1]
    text = text[4:] if text.startswith("www.") else text
    return text.rstrip("/")


def organization_profile_view(profile: Any) -> dict[str, Any] | None:
    """The company page header's public facts from `GET /v1/organizations/{id}` `profile`: a one-line
    description, then website, headquarters and stock listing, each with its source line; the LEI and
    the Wikipedia link go to the fields table under it. A fact the API sends as null (not stated, or
    its source not public) is left out; None when no fact is left."""
    if not isinstance(profile, Mapping):
        return None

    def fact(name: str) -> Mapping[str, Any] | None:
        raw = profile.get(name)
        return raw if isinstance(raw, Mapping) and raw.get("value") not in (None, "", {}) else None

    header: list[dict[str, Any]] = []
    if website := fact("website"):
        url = str(website["value"])
        header.append(
            {
                "key": "website",
                "label": "Website",
                "text": _website_text(url),
                "href": url,
                "source": _fact_source(website),
            }
        )
    if (hq := fact("headquarters")) and isinstance(hq["value"], Mapping):
        city, country = hq["value"].get("city"), hq["value"].get("country")
        place = ", ".join(str(p) for p in (city, COUNTRY_NAMES.get(str(country or ""), country)) if p)
        if place:
            header.append(
                {"key": "headquarters", "label": "Headquarters", "text": place, "source": _fact_source(hq)}
            )
    if (ticker := fact("ticker")) and isinstance(ticker["value"], Mapping) and ticker["value"].get("symbol"):
        exchange, symbol = ticker["value"].get("exchange"), ticker["value"]["symbol"]
        header.append(
            {
                "key": "ticker",
                "label": "Listed",
                "text": f"{exchange}: {symbol}" if exchange else str(symbol),
                "source": _fact_source(ticker),
            }
        )
    description = fact("description")
    lei, wikipedia = fact("lei"), fact("wikipedia")
    view = {
        "description": {"text": str(description["value"]), "source": _fact_source(description)}
        if description
        else None,
        "header": header,
        "lei": {"text": str(lei["value"]), "source": _fact_source(lei)} if lei else None,
        "wikipedia": {"href": str(wikipedia["value"]), "source": _fact_source(wikipedia)}
        if wikipedia
        else None,
        "website": header[0]["href"] if header and header[0]["key"] == "website" else None,
    }
    if not (view["description"] or header or lei or wikipedia):
        return None
    return view


#: The company page's Assets section can be narrowed to the statuses and role a companies-index
#: figure counts (`figure_href`), so the number a reader clicked is the number the section shows.
#: Values are the API's (`services/api/assets.py::ORGANIZATION_ASSET_FILTERS`); anything else is
#: ignored rather than forwarded into a 400.
ASSET_FILTER_ROLES = ("operator", "owner")
ASSET_FILTER_STATUSES = ("operating", "standby", "retiring", "retired", "unknown")  # models.ASSET_STATUSES


def _asset_filter(request: Request) -> dict[str, str]:
    qp = request.query_params
    out: dict[str, str] = {}
    statuses = [s.strip() for s in (qp.get("status") or "").split(",") if s.strip() in ASSET_FILTER_STATUSES]
    if statuses:
        out["status"] = ",".join(dict.fromkeys(statuses))
    if (role := (qp.get("role") or "").strip()) in ASSET_FILTER_ROLES:
        out["role"] = role
    return out


def asset_filter_notice(
    asset_filter: Mapping[str, str],
    assets: list[dict[str, Any]],
    *,
    more: bool,
    clear_href: str,
    listed: int | None = None,
) -> dict[str, str] | None:
    """ "Showing the 38 operating, standby or retiring assets it owns": the line over a narrowed Assets
    section, with the link back to every asset. The number is the distinct assets on the rows (the
    figure the companies index counts, one asset once whatever its roles); "the first N" when the API
    has more rows than the page reads. `listed` is how many table rows the groups print (one per asset
    and role): more than the distinct count means an asset is under two role headings, and the line
    says so."""
    if not asset_filter:
        return None
    statuses = [s.replace("_", " ") for s in asset_filter.get("status", "").split(",") if s]
    status_words = (
        ", ".join(statuses[:-1]) + " or " + statuses[-1]
        if len(statuses) > 1
        else (statuses[0] if statuses else "")
    )
    role = {"operator": " it operates", "owner": " it owns"}.get(asset_filter.get("role", ""), "")
    distinct = len({a.get("public_id") or id(a) for a in assets})
    if more:
        words = f"{status_words} assets" if status_words else "assets"
        text = f"Showing only {words}{role}: the first {distinct:,} of them."
    else:
        noun = "asset" if distinct == 1 else "assets"
        words = f"{status_words} {noun}" if status_words else noun
        text = f"Showing only the {distinct:,} {words}{role}."
        if (listed if listed is not None else len(assets)) > distinct:
            # One row per asset and role (`_merge_holdings`): an asset the scope both operates and
            # owns is under both headings, so they can add up to more than the count.
            text += " An asset held in two roles is listed under each."
    return {"text": text, "clear_href": clear_href}


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


# ---- /organizations index (docs/00-PLAN.md 2026-09-19 item 4; docs/16 U-9) ----------------------
# Lane pages (2026-10-23): the index reads each company's stored figures from the list response's
# `stats` (decision 2026-10-22, `organization_stats`), so a row needs no detail call; it sorts by
# operating capacity, largest first (docs/16 §4.7: a first visit shows the large owners, not
# "1000 Mile Solar, LLC"), and every figure links to the list whose total it is.
#: Forwarded to `GET /v1/organizations` under the API's own names. `q` matches the name or an alias,
#: ignoring case, spaces and punctuation; the rest filter on the stored figures of the chosen scope.
#: No form control sets `country`, `jurisdiction`, `iso`, `has_assets`, `min_operating_mw` or
#: `is_curated_issuer`; they pass through for links.
ORG_INDEX_FILTERS = (
    "q",
    "type",
    "state",
    "technology",
    "role",
    "has_pipeline",
    "has_assets",
    "iso",
    "min_operating_mw",
    "country",
    "jurisdiction",
    "is_curated_issuer",
)
#: Filters on the stored figures: while the figures are refreshing they match nothing (API
#: `meta.organization_stats.state`), and the page says so instead of "no companies match".
ORG_FIGURE_FILTERS = ("state", "technology", "role", "has_pipeline", "has_assets", "iso", "min_operating_mw")
#: One API call per page now (the rows carry their figures), so the page is as long as the other lists.
ORG_INDEX_PAGE_SIZE = 50
#: Whose figures a row shows: the whole group (the company and every subsidiary listed under it,
#: each asset counted once) or the company alone. The page defaults to the group, as the company
#: page does (`web/ownership.py::DEFAULT_SCOPE`): a holding company's own row holds little, so
#: "NextEra Energy" would otherwise read as a company with no projects. The API's default is `self`.
ORG_SCOPE_PARAM = "scope"
ORG_SCOPES = ("group", "self")
ORG_DEFAULT_SCOPE = "group"
#: The sortable headings, keyed as the template names them (API `SortOrganizations`). A first click
#: sorts a figure largest first and the name A to Z; a missing figure sorts last either way.
ORG_COLUMNS: dict[str, SortColumn] = {
    "name": column("name", "name", "name"),
    "assets": column("assets", "number", "operating assets"),
    "operating_mw": column("operating_mw", "number", "operating capacity"),
    "owned_mw": column("owned_mw", "number", "owned capacity"),
    "pipeline_count": column("pipeline_count", "number", "projects in development"),
    "pipeline_mw": column("pipeline_mw", "number", "capacity in development"),
}
#: Tokens the API sorts companies by that no heading offers; kept so a linked view still sorts.
ORG_OTHER: dict[str, SortColumn] = {
    "rfps_issued": column("rfps_issued", "number", "RFPs issued"),
    "name_canonical": column("name_canonical", "name", "name"),
}
ORG_DEFAULT_SORT = "-operating_mw"
#: The figures a row prints, in column order (`org_index_row`).
ORG_FIGURE_KEYS = ("assets", "operating_mw", "owned_mw", "pipeline_count", "pipeline_mw")
#: The Role filter's choices (API `role`): what a company does in the register, in plain words.
ORG_ROLE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("operator", "Operates plants or pipelines"),
    ("owner", "Owns plants or pipelines"),
    ("sponsor", "Develops projects"),
)
#: How long the Type filter's list of types with at least one company is reused (one `limit=1` read
#: per type, as the RFPs & funding Kind filter does, but cached: on the 2026-10-20 store every
#: company is typed `other`, and a Type menu whose every choice empties the list helps nobody).
ORG_TYPE_PROBE_TTL_S = 900.0
#: What `GET /v1/organizations` accepts for each filter (`services/api/organizations.py`
#: `_stats_filters`, `_organization_filters`). A value it would refuse with a 400 is dropped here
#: (one token of a list, or the whole parameter), so a hand-typed or stale link shows the list
#: without that filter instead of an error page: the `web/ownership.py::resolve_scope` rule, and what
#: `/proposals` and `/opportunities` already do with a value they do not know.
ORG_TOKEN_FILTERS = ("state", "iso", "technology")
ORG_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")  # the API's `_TOKEN_RE`
ORG_ROLES = tuple(value for value, _ in ORG_ROLE_OPTIONS)  # the API's `ROLES`
ORG_BOOLEAN_FILTERS = ("has_pipeline", "has_assets", "is_curated_issuer")


def _clean_filter(name: str, raw: str) -> str | None:
    """One filter value as the API documents it, or None when it would be refused."""
    if name in ORG_BOOLEAN_FILTERS:
        value = raw.lower()
        return value if value in ("true", "false") else None
    if name == "min_operating_mw":
        try:
            bound = float(raw)
        except ValueError:
            return None
        return raw if math.isfinite(bound) and bound >= 0 else None
    if name == "role" or name in ORG_TOKEN_FILTERS:
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if name == "role":
            tokens = [t.lower() for t in tokens if t.lower() in ORG_ROLES]
        else:
            tokens = [t for t in tokens if ORG_TOKEN_RE.match(t)]
        return ",".join(dict.fromkeys(tokens)) or None
    return raw  # q, type, country, jurisdiction: any text, matched as it is


def org_index_filters(qp: QueryParams) -> dict[str, str]:
    """The index's filters from the URL, in `ORG_INDEX_FILTERS` order, each trimmed and checked
    (`_clean_filter`); an empty or refused value is left out, so `q=%20` is no search at all."""
    out: dict[str, str] = {}
    for name in ORG_INDEX_FILTERS:
        raw = (qp.get(name) or "").strip()
        value = _clean_filter(name, raw) if raw else None
        if value:
            out[name] = value
    return out


def _refused_parameter(exc: ApiError, params: Mapping[str, Any]) -> str | None:
    """The parameter a 400 from the list names, when the page sent it: an expired or mangled
    `cursor`, or the field of a `validation_error` (a rule the API added after `_clean_filter`)."""
    if exc.status_code != 400 or not isinstance(exc.body, Mapping):
        return None
    if exc.body.get("code") == "invalid_cursor" and params.get("cursor"):
        return "cursor"
    errors = exc.body.get("errors")
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], Mapping) else {}
    field_name = first.get("field")
    return str(field_name) if field_name and params.get(str(field_name)) is not None else None


def _read_index(
    api: ApiClient, params: dict[str, str | None]
) -> tuple[dict[str, Any], dict[str, str | None]]:
    """`GET /v1/organizations`, read again without whatever parameter a 400 names (at most once per
    parameter), so a link the checks above let through still shows the list: page one for a stale
    cursor, the list without the filter for a refused value. Returns the envelope and the
    parameters it was read with."""
    for _ in range(len(params) + 1):
        try:
            return api.get("/v1/organizations", params=params), params
        except ApiError as exc:
            refused = _refused_parameter(exc, params)
            if refused is None:
                raise
            params = {k: v for k, v in params.items() if k != refused}
    return api.get("/v1/organizations", params=params), params


def org_scope(raw: str | None) -> str:
    """`group` or `self` from the URL; anything else is the default."""
    value = (raw or "").strip().lower()
    return value if value in ORG_SCOPES else ORG_DEFAULT_SCOPE


def gw_text(mw: Any) -> str | None:
    """MW as the companies index prints it, in GW: one decimal from 10 GW, two below, and "<0.01"
    for a figure under 5 MW that would otherwise round to nothing. None for no figure. The switch
    reads the rounded figure, so 9,999.9 MW is "10.0", not "10.00"."""
    value = _number(mw)
    if value is None:
        return None
    gw = value / 1000
    if gw == 0:
        return "0"
    if gw < 0.005:
        return "<0.01"
    return f"{gw:,.1f}" if round(gw, 2) >= 10 else f"{gw:,.2f}"


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def figure_href(record: Mapping[str, Any], list_query: Any, *, scope: str) -> str | None:
    """The page whose total a figure is, from the API's `list_queries` entry (`{path, query}`):
    proposals open on `/proposals` and RFPs on `/opportunities` with the same query (all notices, not
    the energy-related default, since the figure counts every one); asset figures open the company
    page at the same scope with the Assets section narrowed to the same statuses and role
    (`_asset_filter`), because no other page lists one company's assets. None when the API sent no
    query for the figure."""
    if not isinstance(list_query, Mapping):
        return None
    path = str(list_query.get("path") or "")
    query = list_query.get("query")
    pairs = [(str(k), str(v)) for k, v in query.items()] if isinstance(query, Mapping) else []
    if path == "/v1/proposals":
        return "/proposals?" + urlencode(pairs)
    if path == "/v1/opportunities":
        return "/opportunities?" + urlencode([*pairs, ("relevance", "all")])
    if path.startswith("/v1/organizations/") and path.endswith("/assets"):
        kept = [(k, v) for k, v in pairs if k in ("status", "role")]
        if scope == "self":
            kept.insert(0, (SCOPE_PARAM, "self"))
        base = f"/organizations/{record.get('slug') or record.get('public_id')}"
        return f"{base}?{urlencode(kept)}#assets" if kept else f"{base}#assets"
    return None


def org_index_row(entity: Mapping[str, Any], *, scope: str) -> dict[str, Any]:
    """One companies-index row: the organisation, its figures in the words and units the page prints
    (each with the link that reproduces it and a tooltip with the exact value), and a short line of
    where it works. Each figure is None when the API sent no `stats` (figures withheld or
    refreshing): the row then prints dashes, never zeros."""
    record = flatten_organization(entity)
    record["href"] = f"/organizations/{record['slug'] or record['public_id']}"
    stats = entity.get("stats")
    if not isinstance(stats, Mapping):
        record.update({"figures": dict.fromkeys(ORG_FIGURE_KEYS), "group_size": None, "places": None})
        return record
    raw_queries = stats.get("list_queries")
    queries: Mapping[str, Any] = raw_queries if isinstance(raw_queries, Mapping) else {}

    def href(key: str) -> str | None:
        return figure_href(record, queries.get(key), scope=scope)

    def figure(
        key: str, value: float | None, text: str | None, title: str, note: str | None = None
    ) -> dict[str, Any]:
        return {
            "value": value,
            "text": text,
            "href": href(key),
            "title": title if value is not None else None,
            # Printed under a capacity that fewer than half of the records behind it state, so a
            # pipeline operator's "0.01 GW" reads as one plant's figure, not as a small fleet.
            "note": note if value else None,
        }

    def partial(stated: int, of: int | None, noun: str) -> str | None:
        return f"from {stated:,} of {of:,} {noun}" if of and stated * 2 < of else None

    def across(mw: float | None, stated: int, of: int | None, noun: str) -> str:
        if of and stated < of:
            return f"{formatting.mw(mw)} MW across {stated:,} of {of:,} {noun}; the others state no capacity."
        return f"{formatting.mw(mw)} MW across {stated:,} {noun} with a stated capacity."

    assets = _int_or_none(stats.get("assets"))
    owned_assets = _int_or_none(stats.get("owned_assets"))
    pipeline = _int_or_none(stats.get("pipeline_count"))
    operating, owned, pipeline_mw = (
        _number(stats.get(k)) for k in ("operating_mw", "owned_mw", "pipeline_mw")
    )
    mw_assets = _int_or_none(stats.get("operating_mw_assets")) or 0
    owned_mw_assets = _int_or_none(stats.get("owned_mw_assets")) or 0
    mw_projects = _int_or_none(stats.get("pipeline_mw_records")) or 0
    whose = (
        "the group's" if scope == "group" and (_int_or_none(stats.get("organizations")) or 1) > 1 else "its"
    )
    record["figures"] = {
        "assets": figure(
            "assets",
            assets,
            f"{assets:,}" if assets is not None else None,
            f"{assets or 0:,} operating, standby or retiring asset{'' if assets == 1 else 's'}, each counted "
            f"once. Open {whose} assets.",
        ),
        "operating_mw": figure(
            "operating_mw",
            operating,
            gw_text(operating),
            across(operating, mw_assets, assets, "assets"),
            partial(mw_assets, assets, "assets"),
        ),
        "owned_mw": figure(
            "owned_mw",
            owned,
            gw_text(owned),
            f"{formatting.mw(owned)} MW: each owned asset's capacity times the stated share.",
            partial(owned_mw_assets, owned_assets, "owned assets"),
        ),
        "pipeline_count": figure(
            "pipeline_count",
            pipeline,
            f"{pipeline:,}" if pipeline is not None else None,
            f"{pipeline or 0:,} active project{'' if pipeline == 1 else 's'} (announced to under "
            "construction) a register still lists. Open them.",
        ),
        "pipeline_mw": figure(
            "pipeline_mw",
            pipeline_mw,
            gw_text(pipeline_mw),
            f"{formatting.mw(pipeline_mw)} MW across {mw_projects:,} projects with a stated capacity; "
            "data-centre load and transmission are counted, not summed.",
            partial(mw_projects, pipeline, "projects"),
        ),
    }
    record["group_size"] = _int_or_none(stats.get("organizations")) if scope == "group" else None
    states = [str(s) for s in stats.get("states") or [] if s]
    short = [s[3:] if s.startswith("US-") else s for s in states]
    record["places"] = (
        ", ".join(short[:4]) + (f" +{len(short) - 4}" if len(short) > 4 else "") if short else None
    )
    return record


def _org_types_present(request: Request, api: ApiClient, vocabulary: list[str]) -> list[str]:
    """The organisation types at least one company has, cached for `ORG_TYPE_PROBE_TTL_S` on
    `app.state` for this API client. A type the API cannot answer for is kept (offered), as the
    Kind filter does, and an answer with such a gap is not cached. One read first asks whether any
    company has a type other than `other` (`type` is any-of): when none does, as on every store so
    far, the menu is not shown and that one read is the whole probe."""
    cached = getattr(request.app.state, "org_types_present", None)
    if (
        isinstance(cached, tuple)
        and len(cached) == 3
        and cached[0] is api
        and time.monotonic() - cached[1] < ORG_TYPE_PROBE_TTL_S
    ):
        return list(cached[2])
    present: list[str] = []
    complete = True
    named = [value for value in vocabulary if value != "other"]
    try:
        typed = bool(named) and bool(
            api.get("/v1/organizations", params={"type": ",".join(named), "limit": "1"})["data"]
        )
    except (ApiError, KeyError, TypeError):
        typed, complete = True, False
    for value in vocabulary if typed else []:
        try:
            if api.get("/v1/organizations", params={"type": value, "limit": "1"})["data"]:
                present.append(value)
        except (ApiError, KeyError, TypeError):
            present.append(value)
            complete = False
    if complete:
        request.app.state.org_types_present = (api, time.monotonic(), tuple(present))
    return present


def _org_filter_options(request: Request, api: ApiClient) -> dict[str, Any]:
    """The filter form's choices: types with a company (shown only when one is not `other`), US
    states by name, the technology vocabulary, and the roles. Built for a full page only (an htmx
    swap replaces the rows, not the form)."""
    try:
        vocab = api.get("/v1/meta/vocabularies")["data"]
    except (ApiError, KeyError, TypeError):
        vocab = {}
    type_vocab = [v["value"] for v in vocab.get("organization_type") or [] if isinstance(v, Mapping)]
    types = _org_types_present(request, api, type_vocab) if type_vocab else []
    return {
        "types": types if any(t != "other" for t in types) else [],
        "states": sorted(REGION_NAMES.items(), key=lambda kv: kv[1]),
        "technologies": [v["value"] for v in vocab.get("technology") or [] if isinstance(v, Mapping)],
        "roles": ORG_ROLE_OPTIONS,
    }


def _scope_switch(qp: QueryParams, scope: str) -> dict[str, Any]:
    """ "Count: Whole group · This company only": each link the current view at that scope, without the
    page cursor (a cursor belongs to one scope's order)."""
    kept = [(k, v) for k, v in qp.multi_items() if k not in (ORG_SCOPE_PARAM, "cursor") and v != ""]
    return {
        "current": scope,
        "group": "/organizations" + ("?" + urlencode(kept) if kept else ""),
        "self": "/organizations?" + urlencode([*kept, (ORG_SCOPE_PARAM, "self")]),
    }


@router.get("/organizations", response_class=HTMLResponse)
def organizations_list(request: Request) -> HTMLResponse:
    """The companies index: a name search, filters on where and what each company works in, and the
    stored figures of the chosen scope in sortable columns (operating capacity, largest first, by
    default; best match first while a search has no chosen order). One API call per render, plus
    the filter form's vocabulary on a full page."""
    api = get_api(request)
    qp = request.query_params
    scope = org_scope(qp.get(ORG_SCOPE_PARAM))
    chosen = chosen_sort(qp.get("sort"), ORG_COLUMNS, ORG_OTHER)
    filters = org_index_filters(qp)
    searching = "q" in filters
    # A search with no chosen order keeps the API's best-match order (names that start with the
    # words first); otherwise the largest operating fleet leads.
    sort = chosen or (None if searching else ORG_DEFAULT_SORT)
    params: dict[str, str | None] = dict(filters)
    params[ORG_SCOPE_PARAM] = scope
    params["sort"] = sort
    params["limit"] = str(ORG_INDEX_PAGE_SIZE)
    params["cursor"] = (qp.get("cursor") or "").strip() or None
    params["include"] = "count"
    envelope, params = _read_index(api, params)
    # What the page was read with: a value the API refused anyway is not echoed into the form,
    # the links or the canonical URL.
    filters = {k: v for k, v in filters.items() if params.get(k) is not None}
    cursor = params.get("cursor")
    stats_meta = envelope.get("meta", {}).get("organization_stats")
    stats_state = stats_meta.get("state") if isinstance(stats_meta, Mapping) else None
    refreshing = stats_state in ("stale", "missing")
    figure_filtered = any(name in filters for name in ORG_FIGURE_FILTERS)
    # While the figures refresh every row's figures are null, so a figure order is no order at all:
    # read the page again in name order rather than show an arbitrary one.
    reread = bool(refreshing and sort and sort.lstrip("-") not in ("name", "name_canonical") and not cursor)
    if reread:
        envelope, _ = _read_index(api, {**params, "sort": "name"})
        sort = "name"
    records = [org_index_row(entity, scope=scope) for entity in envelope["data"]]
    page = envelope["page"]
    meta = envelope.get("meta", {})
    # The view as links carry it: the checked filters, then the scope and the order only where
    # they are not the page's own defaults, so `?scope=group&sort=-operating_mw` and
    # `?scope=bogus&sort=bogus` are the plain index (one canonical URL, no duplicate crawl paths).
    view_pairs = [(name, filters[name]) for name in ORG_INDEX_FILTERS if name in filters]
    if scope != ORG_DEFAULT_SCOPE:
        view_pairs.append((ORG_SCOPE_PARAM, scope))
    if chosen and not (chosen == ORG_DEFAULT_SORT and not searching):
        view_pairs.append(("sort", chosen))
    view = QueryParams(view_pairs)
    canonical_path = "/organizations" + canonical_query(
        QueryParams([*view_pairs, *([("cursor", cursor)] if cursor else [])]),
        (*ORG_INDEX_FILTERS, ORG_SCOPE_PARAM, "sort", "cursor"),
    )
    credits = page_credits(envelope)
    context = {
        "records": records,
        "total": meta.get("total"),
        "total_is_estimate": meta.get("total_is_estimate", False),
        # The registers behind the figures on this page (`licence_summary`); where none is behind
        # them, the line says where the names come from (UX-4).
        "credits": credits,
        "credits_note": ORG_CREDITS_NOTE,
        "has_more": page["has_more"],
        "next_cursor": page["next_cursor"],
        "prev_cursor": page.get("prev_cursor"),
        # The pager's links carry the order the page was read in, so a next-page cursor from the
        # name-order re-read is used with that order.
        "querystring": urlencode(
            [*(p for p in view_pairs if p[0] != "sort"), ("sort", "name")] if reread else view_pairs
        ),
        "filters": filters,
        "scope": scope,
        "scope_switch": _scope_switch(view, scope),
        "refreshing": refreshing,
        # What the "figures refreshing" note says of the order the rows are in.
        "refreshing_order": (
            "name" if sort in ("name", "name_canonical") else "best match" if sort is None else None
        ),
        "figure_filtered": figure_filtered,
        "searching": searching,
        "sort_caption": (sort_caption(sort, ORG_COLUMNS, ORG_OTHER) if sort else "best matches first"),
        "sort_headers": sort_headers("/organizations", view, ORG_COLUMNS, sort or ""),
        "sort_param": chosen,
        "filter_options": None if is_htmx(request) else _org_filter_options(request, api),
        "canonical_path": canonical_path,
        "jsonld": [
            item_list_jsonld(
                request,
                name="Companies",
                description=(
                    "Owners, operators and developers of the plants, pipelines and projects in the "
                    "Inframation register, with each one's operating and planned capacity."
                ),
                path=canonical_path,
                rows=[(r["name"], r["href"]) for r in records if r.get("name")],
                total=None if meta.get("total_is_estimate") else meta.get("total"),
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
    #: The most recently changed proposals at this scope (`web/org_pipeline.py::fetch_proposals`);
    #: `proposals` above is the first `ORG_PROPOSALS_LISTED` of them, flattened for the list section.
    proposal_pages: ProposalPages = field(default_factory=ProposalPages)
    #: `GET /v1/organizations/{id}/pipeline` at this scope: the pipeline summary's counts, over every
    #: record (`None` when it could not be read).
    pipeline: dict[str, Any] | None = None
    opportunities: list[dict[str, Any]] = field(default_factory=list)
    technology_vocabulary: list[str] = field(default_factory=list)
    #: `status` and `role` narrowing the Assets section (`_asset_filter`), from a companies-index figure.
    asset_filter: dict[str, str] = field(default_factory=dict)
    #: Whether the API holds more asset rows than the page read (`page.has_more`).
    assets_more: bool = False
    #: Whether the page read every asset row: the API sent its `page` and said no more. A response
    #: without `page` says neither, and the summary line then keeps the API's own counts.
    assets_complete: bool = False


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
        asset_filter=_asset_filter(request),
    )
    try:
        # A parent such as Tallgrass Energy holds no `asset_owner` edge itself; the subsidiaries
        # do (curated parents, services/ingest/midstream.py), and above it a holding company holds
        # nothing either, so the page's default scope is the whole descent rather than one level.
        assets_env = api.get(
            f"/v1/organizations/{public_id}/assets",
            params={"limit": 100, **api_params, **fetch.asset_filter},
        )
        fetch.assets = [_org_asset_row(r) for r in assets_env["data"]]
        page = assets_env.get("page")
        fetch.assets_more = bool(isinstance(page, Mapping) and page.get("has_more"))
        fetch.assets_complete = isinstance(page, Mapping) and not page.get("has_more")
        raw_totals = assets_env.get("totals")
        if isinstance(raw_totals, Mapping):
            fetch.asset_counts = raw_totals
        raw_scope = assets_env.get("scope")
        if isinstance(raw_scope, Mapping):
            fetch.scope_meta = raw_scope
    except ApiError:
        fetch.assets = []
    # The pipeline summary is the API's aggregate over every record at this scope (one read); the
    # list section prints the first `ORG_PROPOSALS_LISTED` of the most recently changed.
    fetch.pipeline = fetch_pipeline(api, public_id, api_params) if public_id else None
    fetch.proposal_pages = fetch_proposals(api, public_id, api_params) if public_id else ProposalPages()
    fetch.proposals = [_org_proposal_row(e) for e in fetch.proposal_pages.rows[:ORG_PROPOSALS_LISTED]]
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


def _org_proposal_row(entity: Mapping[str, Any]) -> dict[str, Any]:
    """One row of the Proposals section: the list's flattened proposal plus the sponsor's own page,
    so a group's list can name, and link, the company in it that filed each record."""
    row = flatten_proposal(dict(entity))
    sponsor = entity.get("sponsor")
    sponsor = sponsor if isinstance(sponsor, Mapping) else {}
    row["sponsor_public_id"] = sponsor.get("public_id")
    row["sponsor_slug"] = sponsor.get("slug")
    return row


def _proposals_span_group(fetch: _OrgFetch) -> bool:
    """Whether the Proposals section reads more than this one organisation: the scope is wider than
    `self` and the walk reached a subsidiary. Read from the pipeline aggregate's own `scope` block
    (the proposals and the aggregate are read at one scope), else from the assets call's.

    Not `GroupView.is_group`, which counts the companies that *hold assets*: a developer's parent
    usually holds none and its subsidiaries hold none either -- the queue positions are all they
    have -- so that test called NextEra's or RWE's group a single company and the rows did not say
    which subsidiary filed them."""
    if fetch.scope == "self":
        return False
    scope = (fetch.pipeline or {}).get("scope")
    meta = scope if isinstance(scope, Mapping) else fetch.scope_meta
    return _count_or(meta.get("organizations") if isinstance(meta, Mapping) else None, 1) > 1


def _pipeline_total(fetch: _OrgFetch) -> int:
    totals = (fetch.pipeline or {}).get("totals")
    if isinstance(totals, Mapping):
        return _count_or(totals.get("records"), len(fetch.proposal_pages.rows))
    return len(fetch.proposal_pages.rows)


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
    # One point per asset, however many holdings name it (`distinct_assets`).
    assets = distinct_assets(fetch.assets)
    if view.show_map:
        capped = [a for a in assets if not a.get("geometry") and a.get("public_id")]
        for a in capped[:ORG_MAP_DETAIL_CAP]:
            try:
                a["geometry"] = _geometry_of(api.get(f"/v1/assets/{a['public_id']}")["data"])
            except ApiError:
                continue
    features = (
        [_asset_feature(a, a["geometry"]) for a in assets if a.get("geometry")] if view.show_map else []
    )
    nearby, nearby_shown, nearby_total, nearby_filter = _organization_nearby(
        request, api, fetch, features, groups
    )
    tile_url = (os.environ.get("MAP_TILE_URL") or "").strip() or None
    tile_mode = _tile_mode(tile_url)
    mapped = sum(1 for f in features if f["properties"]["kind"] == "asset")
    unmapped = len(assets) - mapped
    plural = "s" if len(nearby) != 1 else ""
    caption = (
        f"{mapped} of {len(assets)} asset{'s' if len(assets) != 1 else ''} with a mapped location"
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
    # The page read every row unless the API has more and the section is not narrowed (a narrowed
    # section's own count is in its notice; the API's totals are the whole scope's).
    groups = _with_totals(
        _org_asset_groups(fetch.assets),
        fetch.asset_counts,
        complete=not fetch.assets_more or bool(fetch.asset_filter),
    )
    # A fund-level page is not a company page (owner brief, 2026-09-20): `web/ownership.py`
    # decides from the API's own counts whether this node renders as a company (map plus a flat
    # asset table) or as a portfolio of companies, and says in words why anything is absent.
    view = group_view(
        scope=fetch.scope,
        totals=fetch.asset_counts,
        scope_meta=fetch.scope_meta,
        asset_rows=distinct_assets(fetch.assets),
    )
    portfolio = portfolio_rows(fetch.asset_counts, subject_public_id=fetch.public_id)
    m = _organization_map(request, api, fetch, view, groups)
    # An organisation row carries no provenance of its own (`serialize_organization` emits []);
    # the panel shows the sources of its assets and proposals, or nothing -- never the "withheld
    # under licence" empty state, which would be a false licence claim (owner brief, 2026-09-19).
    provenance = fetch.record["provenance"] or _derived_org_provenance(
        fetch.assets, fetch.proposals, fetch.opportunities
    )
    # The registers behind the facts under the company's name (website, headquarters, listing,
    # description) are credited here too, as the API's `licence_summary` does.
    facts = [
        row
        for row in profile_provenance(entity.get("profile"))
        if row["source_id"] not in {p.get("source_id") for p in provenance}
    ]
    if fetch.record["provenance"]:
        provenance_note = None
    elif provenance:
        provenance_note = (
            "The registers behind this company's assets, proposals and opportunities"
            + (" and the facts under its name" if facts else "")
            + "; the company record itself is derived from them."
        )
    else:
        provenance_note = "The registers behind the facts under this company's name." if facts else None
    provenance = [*provenance, *facts]
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
    # Website, headquarters, listing and a one-line description, each with its source (lane
    # org-enrich's `profile`; never for a natural person, whom the API sends no profile for).
    profile = organization_profile_view(entity.get("profile"))
    return templates.TemplateResponse(
        request,
        "organization_detail.html",
        {
            "record": record,
            "profile": profile,
            "assets": fetch.assets,
            "asset_groups": compose.groups,
            # A companies-index figure opened here: the Assets section shows what it counted.
            "asset_filter_notice": asset_filter_notice(
                fetch.asset_filter,
                fetch.assets,
                more=fetch.assets_more,
                clear_href=compose.path + "#assets",
                listed=sum(g["count"] for g in compose.groups),
            ),
            # The API's `totals` count the whole scope whatever the filter, so a narrowed section's
            # sentence is counted over its own rows; so is a section that holds every row.
            "summary_parts": _org_summary_parts(
                None if fetch.asset_filter else fetch.asset_counts,
                compose.groups,
                complete=fetch.assets_complete,
            ),
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
                    description=(
                        profile["description"]["text"]
                        if profile and profile.get("description")
                        else compose.descriptor
                    ),
                    same_as=[profile["wikipedia"]["href"]] if profile and profile.get("wikipedia") else None,
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
            # The list covers the subsidiaries too: say so, and name the filer on every row.
            "proposals_group": _proposals_span_group(fetch),
            # The company's pipeline by status, technology and ISO, each count a link to the list
            # (owner, 2026-10-10: "show me their pipeline"); `None` when it sponsors nothing here.
            "pipeline": pipeline_summary(fetch.pipeline),
            # How many the list section is a part of: the aggregate's total when it was read.
            "proposals_total": _pipeline_total(fetch),
            "proposals_complete": fetch.pipeline is not None or fetch.proposal_pages.complete,
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
        return not_found_response(request, "company")
    fetch = _organization_fetch(api, request, entity)
    compose = _organization_compose(request, api, entity, fetch)
    return count_page_view(request, _organization_render(request, api, entity, fetch, compose), "company")
