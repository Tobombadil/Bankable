"""The asset pages (docs/42-backend-review-2026-09-26.md lane L3): `/assets`, `/assets/{slug}` and
`/assets/by-id/{public_id}`, moved out of `web/app.py` behaviour-preserving, with the helpers the
call graph showed reached only by these routes -- `asset_type_counts`, `_asset_index_description`,
`_resolve_asset_by_slug`, `_asset_index_default_sort` and the index's own constants.

`_asset_extras` and the fuel-field presenter cluster it calls (`_fuel_fields`, `_normalise_owners`,
`_diameter_text`, ...) stayed in `web/page.py` instead: measured (not assumed from the review's
§4.1 estimate), `_asset_extras` is also called by `web/app.py`'s `search` route, so it is shared
page plumbing, not asset-page-only, and everything it calls in turn has to live where it lives.

`/api/assets/{public_id}` (`asset_detail_proxy`) stayed in `web/app.py` too, beside the other
same-origin geo/data proxies (`/api/assets/geo`, `/api/context/plants/geo`, `/api/geo/regions`): by
call graph it reaches nothing this module's routes reach (no template, no `flatten_asset`, no
presenter -- just `get_api` and a JSON relay, exactly the shape of those other three), and moving it
into a router included near the top of `web/app.py` (the `organizations_router` slot) would
register `/api/assets/{public_id}` *before* `/api/assets/geo` is defined further down that module,
which would make the parametrised route swallow `/api/assets/geo` (`public_id="geo"`) -- the
overlap `web/test_asset_pages.py::test_asset_detail_proxy_relays_the_detail_envelope_without_cookies`
already exercises under the original, safe order.

Shared page plumbing (`templates`, `get_api`, JSON-LD, the map/geometry presenters, `_asset_extras`,
...) lives in `web/page.py`, which this module and `web/app.py` both import -- `web/app.py` includes
this router and so cannot be imported back from here without a cycle.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import QueryParams

from web import labels, record_tabs
from web.api_client import ApiClient, ApiError, ApiNotFound
from web.formatting import readable_name
from web.labels import data_notes, labelled_attributes
from web.list_sort import ASSET_COLUMNS, ASSET_OTHER, chosen_sort, sort_caption, sort_headers, valid_sort
from web.page import (
    LINE_ASSET_TYPES,
    _asset_extras,
    _asset_feature,
    _basemap_attribution,
    _geometry_of,
    _mini_map,
    _number,
    _proposal_feature,
    _sentence_label,
    _tile_mode,
    _type_label,
    breadcrumb_jsonld,
    canonical_query,
    count_page_view,
    get_api,
    group_nearby_proposals,
    is_htmx,
    item_list_jsonld,
    not_found_response,
    querystring_without,
    templates,
)
from web.retirement import retirement_view
from web.viewmodels import (
    WORLD_BBOX,
    asset_count_note,
    coverage_facts,
    flatten_asset,
    flatten_proposal,
    provenance_panel_rows,
)

router = APIRouter()


# ---- /assets index (docs/00-PLAN.md 2026-09-19 item 4; docs/30 §1.1) ----------------------------
#: The filters `/assets` forwards, named exactly as `GET /v1/assets` names them (D-17: one filter
#: vocabulary and one URL grammar across list, map and feed). `technology` is deliberately not
#: exposed as a control -- its vocabulary is plant-shaped and means nothing for a pipeline -- but
#: it is forwarded when present so a link from the map keeps working.
ASSET_INDEX_FILTERS = (
    "asset_type",
    "state",
    "q",
    "technology",
    # Lane R1: `/assets?asset_type=power_plant&status=retired,retiring` is the retired-plant index.
    "status",
    "retirement_year[gte]",
    "retirement_year[lte]",
)
#: The `sort=` order is chosen by clicking the Name and Capacity headings (lane list-sort, 2026-10-22;
#: `web/list_sort.py::ASSET_COLUMNS`); any other token `services/api/assets.py::ASSET_SORT_ALLOWLIST`
#: accepts (`-last_changed`, the old "Recently changed" option) still sorts a linked view. Capacity
#: descending is the default: the biggest thing is the most interesting row on an index of
#: infrastructure. Length is *not* an API sort field, so a pipeline's mileage shows in its row but
#: never orders the list.
ASSET_INDEX_DEFAULT_SORT = "-capacity_mw"
#: A line asset carries no `capacity_mw`, so a capacity sort over a pipeline-only view orders it
#: by nothing at all. `length_miles` is not in the API's sort allowlist, so the honest default for
#: such a view is the one key that does order it: name.
ASSET_INDEX_LINE_DEFAULT_SORT = "name"
ASSET_TYPE_COUNTS_CACHE_SECONDS = 900


def _asset_index_default_sort(selected_types: set[str]) -> str:
    if selected_types and selected_types <= LINE_ASSET_TYPES:
        return ASSET_INDEX_LINE_DEFAULT_SORT
    return ASSET_INDEX_DEFAULT_SORT


#: The subject of `/assets`' description when no type is selected.
ASSET_INDEX_SUBJECT = (
    "Power plants, gas pipelines, gas processing and storage sites, LNG terminals, ethanol "
    "plants and RNG projects"
)


def _asset_index_description(selected_types: set[str], counts: Mapping[str, int], params: QueryParams) -> str:
    """The page's `<meta name="description">`, built from the filters actually in force.

    A filtered index that repeated the unfiltered page's sentence would hand a crawler one
    description for many URLs, and would claim a corpus total beside a list that does not show
    it. The count is quoted only for a view whose size `asset_type_counts()` actually knows: the
    whole corpus, or one type of it, with no other filter narrowing the rows."""
    only = next(iter(selected_types)) if len(selected_types) == 1 else None
    subject = _type_label(only, plural=True) if only else ASSET_INDEX_SUBJECT
    where = f" in {params['state']}" if params.get("state") else ""
    total = counts.get(only) if only else (sum(counts.values()) or None)
    counted = f" -- {total:,} of them" if total and not where and not params.get("q") else ""
    return (
        f"{subject}{where} from US public registers{counted}, each with its owner, operator, "
        "location and the register it came from."
    )


def asset_type_counts(request: Request) -> dict[str, int]:
    """`{asset_type: n}` for the counts above `/assets`, read from `GET /v1/assets/geo`'s
    `totals.asset_type_counts` over the whole world in one call.

    `GET /v1/assets` has no `include=count` (`services/api/assets.py::list_assets`'s allowlist),
    and one counting call per type would be twelve round trips per page render, so the map
    endpoint -- which already counts by type for the legend -- is the cheaper honest source. Its
    denominator is assets with a published location, which is *not* the same set as the list
    below (an asset whose licence forbids raw publication keeps its row and page but is off the
    map, `docs/23` `/v1/assets/geo`), so the template says so rather than implying the two agree.
    The counts change once per data load, so they are cached per app process like the sitemap; a
    failed call returns `{}` and the page simply renders no counts.
    """
    cache: dict[str, tuple[float, dict[str, int]]] = request.app.state.__dict__.setdefault(
        "asset_type_counts_cache", {}
    )
    cached = cache.get("world")
    if cached and time.monotonic() - cached[0] < ASSET_TYPE_COUNTS_CACHE_SECONDS:
        return cached[1]
    counts: dict[str, int] = {}
    try:
        envelope = get_api(request).get("/v1/assets/geo", params={"bbox": WORLD_BBOX, "zoom": "3"})
        data = envelope.get("data")
        totals = data.get("totals") if isinstance(data, Mapping) else None
        raw = totals.get("asset_type_counts") if isinstance(totals, Mapping) else None
        if isinstance(raw, Mapping):
            counts = {str(k): int(v) for k, v in raw.items() if isinstance(v, int | float) and v > 0}
    except (ApiError, httpx.HTTPError):
        return {}
    cache["world"] = (time.monotonic(), counts)
    return counts


@router.get("/assets", response_class=HTMLResponse)
def assets_list(request: Request) -> HTMLResponse:
    """The crawlable index of every registry asset (docs/50 §4.4: the asset pages are the
    acquisition surface, so they need a path in from a page a crawler can reach). Same markup,
    pagination and empty state as `/proposals` -- `partials/_asset_rows.html` mirrors
    `partials/_proposal_rows.html` rather than inventing a second list idiom."""
    api = get_api(request)
    qp = request.query_params
    selected_types = set((qp.get("asset_type") or "").split(",")) - {""}
    sort = valid_sort(qp.get("sort"), ASSET_COLUMNS, _asset_index_default_sort(selected_types), ASSET_OTHER)
    params: dict[str, str | None] = {name: qp[name] for name in ASSET_INDEX_FILTERS if qp.get(name)}
    params["sort"] = sort
    params["cursor"] = qp.get("cursor")
    envelope = api.get("/v1/assets", params=params)
    records: list[dict[str, Any]] = []
    for entity in envelope["data"]:
        record = flatten_asset(entity)
        record.update(_asset_extras(entity))
        records.append(record)
    counts = asset_type_counts(request)
    type_counts = [
        {
            "value": asset_type,
            "label": _type_label(asset_type, plural=True),
            "count": counts[asset_type],
            "selected": asset_type in selected_types,
        }
        for asset_type in sorted(counts, key=lambda k: (-counts[k], _type_label(k, plural=True)))
    ]
    page = envelope["page"]
    canonical_path = "/assets" + canonical_query(qp, (*ASSET_INDEX_FILTERS, "sort", "cursor"))
    # `numberOfItems` only when the page is the unfiltered index: `asset_type_counts()` counts the
    # whole corpus, so quoting it beside a filtered list would be a number the page does not show.
    filtered = any(qp.get(name) for name in ASSET_INDEX_FILTERS)
    corpus_total = (sum(counts.values()) or None) if not filtered else None
    count_note = asset_count_note(coverage_facts(request, api), selected_types)
    if count_note:
        count_note["label"] = _sentence_label(next(iter(selected_types)), plural=True)
    context = {
        "records": records,
        "type_counts": type_counts,
        "counts_total": sum(counts.values()) or None,
        # The corrected count for a type whose rows out-number its assets (docs/24); `None` for
        # every other view, argued in `web/viewmodels.py::asset_count_note` and the template.
        "count_note": count_note,
        "sort": sort,
        "sort_caption": sort_caption(sort, ASSET_COLUMNS, ASSET_OTHER),
        "sort_headers": sort_headers("/assets", qp, ASSET_COLUMNS, sort),
        # Kept by the filter form, so a filter change keeps the order a heading click chose.
        "sort_param": chosen_sort(qp.get("sort"), ASSET_COLUMNS, ASSET_OTHER),
        "description": _asset_index_description(selected_types, counts, qp),
        "has_more": page["has_more"],
        "next_cursor": page["next_cursor"],
        "prev_cursor": page.get("prev_cursor"),
        "querystring": querystring_without(qp, "cursor"),
        "filters": dict(qp),
        "canonical_path": canonical_path,
        "jsonld": [
            item_list_jsonld(
                request,
                name="Assets",
                description=_asset_index_description(selected_types, counts, qp),
                path=canonical_path,
                rows=[(r["name"], f"/assets/{r['slug']}") for r in records if r.get("slug")],
                total=corpus_total,
            )
        ],
    }
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/_asset_rows.html", context)
    return templates.TemplateResponse(request, "assets_list.html", context)


def _resolve_asset_by_slug(api: ApiClient, slug: str) -> dict[str, Any] | None:
    """Same slug-filter lookup as `_resolve_proposal_by_slug` (ADR 0008: `asset` carries a
    `slug`, `docs/21` §3.22)."""
    envelope = api.get("/v1/assets", params={"slug": slug, "limit": 1})
    entities: list[dict[str, Any]] = envelope["data"]
    return entities[0] if entities else None


def _nearby_grid(api: ApiClient, record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Lane R1: the grid around a retired or retiring plant (`GET /v1/assets/{id}/nearby-grid`), the
    whole envelope so its licence summary can credit the lines and points it lists. A failed call drops
    only the grid lists; the dates, capacities and grid fields come from the asset itself."""
    if record.get("geometry") is None:
        return None
    try:
        envelope: Mapping[str, Any] = api.get(f"/v1/assets/{record['public_id']}/nearby-grid")
    except ApiError:
        return None
    return envelope


def _grid_data(envelope: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    data = (envelope or {}).get("data")
    return data if isinstance(data, Mapping) else None


def _grid_lists_anything(envelope: Mapping[str, Any] | None) -> bool:
    """Whether the grid around the plant lists a transmission line or a connection point."""
    data = _grid_data(envelope) or {}
    return bool(data.get("transmission_lines") or data.get("interconnection_points"))


def _ownership_tab(owners: list[dict[str, Any]]) -> bool:
    """Whether the asset has an Ownership tab (docs/30 §11.5): two or more ownership records, except one
    company's owner and operator records, which stay the header line ("Owned and operated by"), each
    with its source and date."""
    if len(owners) < 2:
        return False
    if len(owners) == 2:
        roles = sorted(str(o.get("role")) for o in owners)
        holders = {o.get("public_id") or o.get("name") for o in owners}
        if roles == ["operator", "owner"] and len(holders) == 1:
            return False
    return True


#: The Retirement facts the Overview leads with (docs/30 §11.5: "Retiring: N MW by {year}"), in this
#: order: when the plant retired, or how much is scheduled to retire and by when, then what has gone.
#: The status itself is the header's badge, so it is not repeated.
_RETIREMENT_LEAD = (
    "Retired",
    "Capacity scheduled to retire",
    "Next scheduled retirement",
    "Capacity retired",
)


def _retirement_lead(view: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The Overview's three Retirement facts: `_RETIREMENT_LEAD` first, then the Retirement tab's order."""
    facts = [(label, value) for label, value in view.get("facts") or [] if label != "Status"]
    ranked = sorted(
        facts,
        key=lambda f: _RETIREMENT_LEAD.index(f[0]) if f[0] in _RETIREMENT_LEAD else len(_RETIREMENT_LEAD),
    )
    return ranked[:3]


def _nearby_proposals(
    api: ApiClient, record: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The exact-grade projects within 25 km (ADR 0008), one row per project, not per EIA-860M generator
    unit, and the map's features: the asset itself and every unit's dot. Every tab reads the list, since
    whether the Nearby tab exists, and its count, depend on it (T-3)."""
    nearby: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    if record["geometry"] is not None:
        features.append(_asset_feature(record, record["geometry"]))
    try:
        nearby_env = api.get(f"/v1/assets/{record['public_id']}/nearby-proposals")
        for e in nearby_env["data"]:
            flat = flatten_proposal(e)
            # `distance_km` (line-aware for pipelines, API lane 2026-09-19) rides on the row.
            flat["distance_km"] = _number(e.get("distance_km"))
            nearby.append(flat)
            geometry = _geometry_of(e)
            if geometry is not None:
                features.append(_proposal_feature(flat, geometry))
    except ApiError:
        nearby = []
    return group_nearby_proposals(nearby), features


#: The asset's list in its breadcrumb (docs/30 §11.3: "Market / Infrastructure / {name}").
_ASSET_TRAIL: tuple[tuple[str, str], ...] = ((labels.SECTION_LABELS["infrastructure"], "/assets"),)


@router.get("/assets/{slug}", response_class=HTMLResponse)
def asset_detail(request: Request, slug: str) -> Response:
    """ADR 0008 asset page, its Overview (docs/30 §11.5): identity, the map, the first rows of the other
    tabs and the attributes behind a disclosure."""
    return _asset_page(request, slug, "overview")


def _asset_page(request: Request, slug: str, tab: str) -> Response:
    """One tab of a plant, pipeline or line: Overview · Retirement · Grid · Ownership · Nearby · Sources,
    each after Overview only when it has content (docs/30 §11.5; web/record_tabs.py)."""
    moved = record_tabs.query_tab_redirect(
        request, f"/assets/{quote(slug, safe='')}", record_tabs.ASSET_TABS, tab
    )
    if moved is not None:
        return moved
    if tab != "overview" and not record_tabs.is_tab(record_tabs.ASSET_TABS, tab):
        return not_found_response(request, "page")
    api = get_api(request)
    entity = _resolve_asset_by_slug(api, slug)
    if entity is None:
        return not_found_response(request, "asset")
    # The list row resolves the slug but omits `owners` and `geometry` (API lane 2026-09-19: list
    # rows never embed geometry); the detail envelope carries both, so the page reads it and
    # falls back to the list row only when the detail call fails (found on the first Tallgrass
    # screenshots: "No ownership records" beside 1,126 operator edges).
    try:
        entity = api.get(f"/v1/assets/{entity['public_id']}")["data"]
    except ApiError:
        pass
    record = flatten_asset(entity)
    record.update(_asset_extras(entity))
    nearby, features = _nearby_proposals(api, record)
    # Retirement and Grid show for a plant with retired or scheduled units; Grid widens to every plant
    # once the API serves a grid block per plant (docs/30 §11.11 ask B-3).
    view = retirement_view(entity)
    retiring = view is not None
    # Grid has content only when the plant reported grid fields to EIA-860 or the grid around it lists a
    # line or a connection point (T-3). EIA-860's annual file leaves out a plant that retired before
    # its report year, so a retired plant often has neither; nor does one without a mapped location, or
    # when the call fails. The grid around the plant is asked for only when its own fields are empty (or
    # on the Grid tab itself), so a plant that reports them keeps that call to its Grid tab (T-10).
    grid_envelope = (
        _nearby_grid(api, record) if view is not None and (tab == "grid" or not view["grid_rows"]) else None
    )
    has_grid = view is not None and (bool(view["grid_rows"]) or _grid_lists_anything(grid_envelope))
    path = f"/assets/{quote(record['slug'], safe='')}"
    tabs = record_tabs.Tabs(
        "asset",
        path,
        record_tabs.ASSET_TABS,
        present={
            "retirement": retiring,
            "grid": has_grid,
            "ownership": _ownership_tab(record["owners"]),
            "nearby": bool(nearby),
            "sources": True,
        },
        current=tab,
        counts={
            "ownership": len(record["owners"]),
            "nearby": len(nearby),
            "sources": record_tabs.source_count(record["provenance"]),
        },
        absent_lines={} if record["owners"] else {"ownership": "No ownership records for this asset."},
    )
    name = readable_name(record["name"])
    badge = " · ".join(
        str(part)
        for part in (record.get("type_label"), record.get("technology_label"), record.get("line_class"))
        if part
    )
    context: dict[str, Any] = {
        "record": record,
        "head": {
            "type": "asset",
            "crumbs": record_tabs.crumbs(tabs, _ASSET_TRAIL, name),
            "name": name,
            "badge": " · ".join(p for p in (badge, record.get("status_label")) if p),
            "facts": record_tabs.asset_facts(record),
            "action": record_tabs.primary_action(
                request,
                tabs,
                "asset",
                record,
                ("Show on map", record_tabs.map_href(record.get("geometry"))),
                ("View at source", record.get("source_url")),
            ),
        },
        "delayed": record_tabs.tier_notice(request),
        "credits": record_tabs.credits(record["provenance"]),
        "canonical_path": tabs.href(tab),
    }
    if tab == "overview":
        _asset_overview(record, has_grid, nearby, features, context)
        context["jsonld"] = [
            breadcrumb_jsonld(request, record_tabs.jsonld_trail(_ASSET_TRAIL, record["name"], path))
        ]
        if view is not None:
            # The Grid block counts the lines and points around the plant when its own fields are empty.
            context["retirement"] = retirement_view(entity, _grid_data(grid_envelope))
            context["retirement_lead"] = _retirement_lead(view)
            if has_grid and not view["grid_rows"]:
                context["credits"] = record_tabs.credits(
                    record["provenance"],
                    record_tabs.row_provenance(nearby),
                    record_tabs.summary_rows(grid_envelope),
                )
    elif tab == "retirement":
        context["retirement"] = view
    elif tab == "grid":
        context["retirement"] = retirement_view(entity, _grid_data(grid_envelope))
        context["credits"] = record_tabs.credits(
            record["provenance"], record_tabs.summary_rows(grid_envelope)
        )
    elif tab == "ownership":
        # Each ownership record names the register it came from (`_normalise_owners`).
        named = [{"source_name": o["source_name"]} for o in record["owners"] if o.get("source_name")]
        context["credits"] = record_tabs.credits(record["provenance"], named)
    elif tab == "nearby":
        context.update(_map_context(record, features), nearby_proposals=nearby)
        # The map draws the asset too, so its own register is credited beside the projects'.
        context["credits"] = record_tabs.credits(record["provenance"], record_tabs.row_provenance(nearby))
    elif tab == "sources":
        context["provenance_rows"] = provenance_panel_rows(api, record["provenance"])
    response = record_tabs.render(
        request, f"asset_{'detail' if tab == 'overview' else tab}.html", context, tabs
    )
    # One view of the record per visit (PM-5): the Overview counts, its tabs do not.
    return count_page_view(request, response, "asset") if tabs.is_overview else response


def _asset_overview(
    record: dict[str, Any],
    grid_tab: bool,
    nearby: list[dict[str, Any]],
    features: list[dict[str, Any]],
    context: dict[str, Any],
) -> None:
    """The Overview's own data: the map of the asset and the projects near it, and the attributes in
    reader words, less what the Retirement and Grid tabs print themselves."""
    # The Retirement and Grid tabs print these two blocks in words; the Attributes table must not print
    # them again as raw keys and tokens (`majority_mw_retiring`, `2028-12`).
    hidden = ("retirement", "grid") if grid_tab else ("retirement",)
    record["promoted_attributes"] = [*record.get("promoted_attributes", []), *hidden]
    # The Attributes table in reader words (web/labels.py `labelled_attributes`, audit 2026-09-30
    # F1/D-14): internal matching keys dropped, promoted fields omitted, every key a label, and the
    # loaders' caveats as sentences under the table rather than as `feature flags` rows.
    attributes = record["attributes"]
    record["attributes_display"] = labelled_attributes(attributes, omit=record["promoted_attributes"])
    record["attribute_notes"] = data_notes(attributes)
    context.update(
        _map_context(record, features),
        nearby_proposals=nearby,
        # The projects drawn on the map and listed under it are credited with the asset's own source.
        credits=record_tabs.credits(record["provenance"], record_tabs.row_provenance(nearby)),
    )


def _map_context(record: Mapping[str, Any], features: list[dict[str, Any]]) -> dict[str, Any]:
    """The mini-map of the asset and the exact-grade projects near it: the Overview's, and the Nearby
    tab's beside its list (a map with the list that carries the magnitudes)."""
    tile_url = (os.environ.get("MAP_TILE_URL") or "").strip() or None
    tile_mode = _tile_mode(tile_url)
    placed = sum(1 for f in features if f["properties"]["kind"] == "proposal")
    where = "route" if record["is_line"] else "location"
    caption = (
        f"{record['name']}: {where}"
        + (f" and {placed} exact-grade proposal{'s' if placed != 1 else ''} within 25 km" if placed else "")
        + ". "
        + _basemap_attribution(tile_mode, tile_url)
    )
    return {
        "mini_map": _mini_map(features, label=f"Map of {record['name']}", caption=caption),
        "tile_url": tile_url,
        "tile_mode": tile_mode,
    }


@router.get("/assets/by-id/{public_id}")
def asset_by_public_id(request: Request, public_id: str) -> Response:
    """Map features (`/v1/assets/geo`) carry an asset's `public_id` but not its slug; the drawer's
    "Open asset page" link comes here and is redirected to the canonical slug URL."""
    api = get_api(request)
    try:
        entity = api.get(f"/v1/assets/{public_id}")["data"]
    except ApiNotFound:
        return not_found_response(request, "asset")
    slug = entity.get("slug")
    if not slug:
        return not_found_response(request, "asset")
    return RedirectResponse(url=f"/assets/{slug}", status_code=302)


@router.get("/assets/{slug}/{tab}", response_class=HTMLResponse)
def asset_tab(request: Request, slug: str, tab: str) -> Response:
    """A plant's, pipeline's or line's other tabs (docs/30 §11.10). Registered after
    `/assets/by-id/{public_id}`, so that route keeps its two segments."""
    if tab == "overview":
        return record_tabs.overview_redirect(request, f"/assets/{quote(slug, safe='')}")
    return _asset_page(request, slug, tab)
