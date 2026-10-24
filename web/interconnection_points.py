"""Grid interconnection point pages (owner decision 2026-09-28; docs/21 §3.24; docs/25 §1):
`/interconnection-points` and `/interconnection-points/{public_id}`, read from
`GET /v1/interconnection-points[/{id}]`.

Every number on these pages is the API's, computed at the caller's tier over the proposals it may
see; the page adds no arithmetic of its own beyond formatting, so what it prints can never include a
proposal the API withheld. The page says so in one line, because a total that silently omits
unpublished rows reads as the whole queue.

"Recent changes at this point" (lane H2, 2026-09-29) is the detail response's `recent_changes[]`:
the API has already applied the events visibility rule and the point's own proposal set, so the
page only words each event, links its proposal and credits its source.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from web import formatting, labels, record_tabs
from web.api_client import ApiError, ApiNotFound
from web.list_sort import POINT_COLUMNS, chosen_sort, sort_caption, sort_headers, valid_sort
from web.page import (
    breadcrumb_jsonld,
    canonical_query,
    count_page_view,
    get_api,
    is_htmx,
    item_list_jsonld,
    not_found_response,
    querystring_without,
    templates,
    unavailable_response,
)
from web.viewmodels import (
    delisted_phrase,
    iso_label,
    lifecycle_family,
    provenance_panel_rows,
    web_relative_url,
)

router = APIRouter()

#: Query parameters the index forwards to `GET /v1/interconnection-points`, in canonical order.
POINT_INDEX_FILTERS: tuple[str, ...] = ("iso", "jurisdiction", "kind", "q", "min_active_mw", "sort")
#: The registers that name points today (`services/ingest/interconnection.py::POI_FIELDS`): the
#: operator select offers these; `?iso=` still passes any token through for a hand-written link.
ISO_OPTIONS: tuple[str, ...] = ("ERCOT", "CAISO", "NYISO", "NESO")
KIND_LABELS: dict[str, str] = {
    "substation": "Substation",
    "line_tap": "Line tap",
    "unknown": "Unparsed",
}
#: The order: largest active queue first until a heading click chooses another
#: (`web/list_sort.py::POINT_COLUMNS`, lane list-sort, 2026-10-22).
DEFAULT_SORT = "-active_mw"
PAGE_SIZE = 50


def _mw(value: Any) -> str:
    """docs/31 §4 capacity format (`web/formatting.py::mw`): `2,000`, not `2,000.0` (UX-8)."""
    try:
        return formatting.mw(float(value))
    except (TypeError, ValueError):
        return "—"


def flatten_point(entity: Mapping[str, Any]) -> dict[str, Any]:
    """An API point as the templates read it: display strings resolved once, here."""
    totals = entity.get("totals") or {}
    provenance = (entity.get("provenance") or [{}])[0]
    technologies = totals.get("by_technology") or []
    voltage = entity.get("voltage_kv")
    return {
        "public_id": entity.get("public_id"),
        "href": f"/interconnection-points/{entity.get('public_id')}",
        "name": entity.get("name"),
        "iso": entity.get("iso"),
        "iso_label": iso_label(entity.get("iso")),
        "kind": entity.get("kind"),
        "kind_label": KIND_LABELS.get(str(entity.get("kind")), str(entity.get("kind"))),
        "voltage_label": f"{float(voltage):g} kV" if voltage is not None else None,
        "bus_number": entity.get("bus_number"),
        "jurisdiction": entity.get("jurisdiction"),
        "substation_asset": entity.get("substation_asset"),
        "totals": totals,
        "active_mw_label": _mw(totals.get("active_mw")),
        "withdrawn_mw_label": _mw(totals.get("withdrawn_mw")),
        "built_mw_label": _mw(totals.get("built_mw")),
        "top_technologies": [t["technology"] for t in technologies if t.get("active_count")][:3],
        "technologies": [{**t, "active_mw_label": _mw(t.get("active_mw"))} for t in technologies],
        "provenance": entity.get("provenance") or [],
        "source_id": provenance.get("source_id"),
        "source_name": provenance.get("source_name"),
        "source_url": provenance.get("source_url"),
        "retrieved_at": provenance.get("retrieved_at"),
        "reuse_class": provenance.get("reuse_class"),
        "attribution_text": provenance.get("attribution_text"),
    }


def _state_words(state: Any) -> str | None:
    return str(state).replace("_", " ") if state else None


def change_label(event: Mapping[str, Any]) -> str:
    """One event of `recent_changes[]` as a phrase. The before and after states are the event's
    own `lifecycle_state` values; a status change the source recorded without a before state says
    only where it went."""
    event_type = str(event.get("event_type") or "")
    before = _state_words((event.get("before") or {}).get("lifecycle_state"))
    after = _state_words((event.get("after") or {}).get("lifecycle_state"))
    if event_type == "created":
        return "Entered the queue here" + (f" as {after}" if after else "")
    if event_type == "delisted":
        # A project that left its register's report; it frees queue space here (docs/21 §7.3).
        return delisted_phrase(event)
    if event_type == "status_change":
        if before and after:
            return f"Status changed from {before} to {after}"
        return f"Status changed to {after}" if after else "Status changed"
    return (_state_words(event_type) or "Changed").capitalize()


def flatten_change(event: Mapping[str, Any]) -> dict[str, Any]:
    """A `recent_changes[]` event as the template reads it: the date observed, the phrase, the
    proposal it happened to (linked) and the source that recorded it (credited as elsewhere)."""
    subject = event.get("subject") or {}
    provenance = event.get("provenance") or {}
    observed = event.get("observed_at")
    return {
        "id": event.get("id"),
        "date": str(observed)[:10] if observed else None,
        "label": change_label(event),
        "proposal_name": subject.get("name"),
        "proposal_href": web_relative_url(subject.get("url")),
        "source_name": provenance.get("source_name"),
        "source_url": provenance.get("source_url"),
        "attribution_text": provenance.get("attribution_text"),
    }


def _panel_provenance(record: Mapping[str, Any], changes: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The Sources panel's rows: the register that names the point, then any other source a listed
    change event credits (one row per source), so every row on the page is attributed."""
    rows: list[dict[str, Any]] = list(record.get("provenance") or [])
    seen = {row.get("source_id") for row in rows}
    for event in changes:
        provenance = event.get("provenance")
        if provenance and provenance.get("source_id") not in seen:
            rows.append(dict(provenance))
            seen.add(provenance.get("source_id"))
    return rows


def _flatten_point_proposal(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "href": web_relative_url(row.get("url")) or f"/proposals/{row.get('slug')}",
        "lifecycle_family": lifecycle_family(row.get("lifecycle_state")),
        "capacity_label": _mw(row.get("capacity_mw")) if row.get("capacity_mw") is not None else "—",
    }


@router.get("/interconnection-points", response_class=HTMLResponse)
def interconnection_points_list(request: Request) -> HTMLResponse:
    """The index of grid interconnection points, largest active queue first. Same list idiom as
    `/assets`: a filter bar, one `.record-table`, the shared pager, an empty state."""
    api = get_api(request)
    qp = request.query_params
    # A token no heading sorts by falls back to the default here, so it never 400s the API and
    # drops the reader's filters with it (the fallback below).
    sort = valid_sort(qp.get("sort"), POINT_COLUMNS, DEFAULT_SORT)
    params: dict[str, str | None] = {name: qp[name] for name in POINT_INDEX_FILTERS if qp.get(name)}
    params["sort"] = sort
    params["limit"] = str(PAGE_SIZE)
    params["cursor"] = qp.get("cursor")
    params["include"] = "count"
    try:
        envelope = api.get("/v1/interconnection-points", params=params)
    except ApiError as exc:
        if exc.status_code == 400:
            # A malformed filter from a hand-written link: show the unfiltered index, not a 503. The
            # order is kept: it is always one the API sorts by, and the headings say it.
            envelope = api.get(
                "/v1/interconnection-points",
                params={"sort": sort, "limit": str(PAGE_SIZE), "include": "count"},
            )
        else:
            return unavailable_response(request)
    records = [flatten_point(e) for e in envelope["data"]]
    canonical_path = "/interconnection-points" + canonical_query(qp, (*POINT_INDEX_FILTERS, "cursor"))
    context = {
        "records": records,
        "total": envelope["meta"].get("total"),
        "has_more": envelope["page"]["has_more"],
        "next_cursor": envelope["page"]["next_cursor"],
        "prev_cursor": envelope["page"].get("prev_cursor"),
        "querystring": querystring_without(qp, "cursor"),
        "filters": dict(qp),
        "iso_options": ISO_OPTIONS,
        "kind_labels": KIND_LABELS,
        "sort": sort,
        "sort_caption": sort_caption(sort, POINT_COLUMNS),
        "sort_headers": sort_headers("/interconnection-points", qp, POINT_COLUMNS, sort),
        # Kept by the filter form, so a filter change keeps the order a heading click chose.
        "sort_param": chosen_sort(qp.get("sort"), POINT_COLUMNS),
        "canonical_path": canonical_path,
        "jsonld": [
            item_list_jsonld(
                request,
                name="Grid connection points",
                description=(
                    "Substations and line taps where proposed projects connect to the grid, with the "
                    "queue waiting at each."
                ),
                path=canonical_path,
                rows=[(r["name"], r["href"]) for r in records if r.get("name")],
                total=envelope["meta"].get("total"),
            )
        ],
    }
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/_interconnection_point_rows.html", context)
    return templates.TemplateResponse(request, "interconnection_points_list.html", context)


#: The point's lists in its breadcrumb (docs/30 §11.3): Infrastructure, then its Grid connection points tab.
_POINT_TRAIL: tuple[tuple[str, str], ...] = (
    (labels.SECTION_LABELS["infrastructure"], "/assets"),
    (labels.SECTION_LABELS["points"], "/interconnection-points"),
)


@router.get("/interconnection-points/{public_id}", response_class=HTMLResponse)
def interconnection_point_detail(request: Request, public_id: str) -> Response:
    """A grid connection point's Overview (docs/30 §11.5: Overview · Projects · Changes · Sources)."""
    return _point_page(request, public_id, "overview")


@router.get("/interconnection-points/{public_id}/{tab}", response_class=HTMLResponse)
def interconnection_point_tab(request: Request, public_id: str, tab: str) -> Response:
    """A grid connection point's Projects, Changes or Sources tab (docs/30 §11.10)."""
    if tab == "overview":
        return record_tabs.overview_redirect(request, f"/interconnection-points/{quote(public_id, safe='')}")
    return _point_page(request, public_id, tab)


def _point_page(request: Request, public_id: str, tab: str) -> Response:
    """One tab of a grid connection point. One API call carries every tab's rows (the detail response's
    `proposals[]` and `recent_changes[]`), so each tab's presence and count are known on every tab."""
    base = f"/interconnection-points/{quote(public_id, safe='')}"
    moved = record_tabs.query_tab_redirect(request, base, record_tabs.POINT_TABS, tab)
    if moved is not None:
        return moved
    if tab != "overview" and not record_tabs.is_tab(record_tabs.POINT_TABS, tab):
        return not_found_response(request, "page")
    api = get_api(request)
    try:
        envelope = api.get(f"/v1/interconnection-points/{public_id}")
    except ApiNotFound:
        return not_found_response(request, "interconnection point")
    except ApiError:
        return unavailable_response(request)
    entity = envelope["data"]
    record = flatten_point(entity)
    proposals = [_flatten_point_proposal(p) for p in entity.get("proposals") or []]
    changes = list(entity.get("recent_changes") or [])
    path = f"/interconnection-points/{quote(str(record['public_id'] or public_id), safe='')}"
    totals = record.get("totals") or {}
    tabs = record_tabs.Tabs(
        "interconnection_point",
        path,
        record_tabs.POINT_TABS,
        present={"projects": bool(proposals), "changes": bool(changes), "sources": True},
        current=tab,
        # No count on Changes: `recent_changes` is the newest few (the API's RECENT_CHANGES_LIMIT), not
        # a total, and a bare number beside the tab would read as one (T-2).
        counts={
            "projects": int(totals.get("proposal_count") or len(proposals)),
            "sources": record_tabs.source_count(_panel_provenance(record, changes)),
        },
    )
    # The point's register names it as filed: a bus name is not re-cased.
    name = str(record.get("name") or public_id)
    context: dict[str, Any] = {
        "record": record,
        "head": {
            "type": "interconnection_point",
            "crumbs": record_tabs.crumbs(tabs, _POINT_TRAIL, name),
            "name": name,
            "badge": record.get("kind_label"),
            "facts": record_tabs.point_facts(record),
            "action": record_tabs.primary_action(
                request,
                tabs,
                "interconnection_point",
                record,
                (
                    "Open in Projects",
                    f"/proposals?{urlencode({'interconnection_point_id': record['public_id']})}",
                ),
                ("View at source", record.get("source_url")),
            ),
        },
        "delayed": record_tabs.tier_notice(request),
        "proposals": proposals,
        "proposals_truncated": bool(entity.get("proposals_truncated")),
        "changes": [flatten_change(e) for e in changes],
        "credits": record_tabs.credits(record["provenance"]),
        "canonical_path": tabs.href(tab),
    }
    if tab == "overview":
        context["jsonld"] = [breadcrumb_jsonld(request, record_tabs.jsonld_trail(_POINT_TRAIL, name, path))]
        # The Overview lists the first change rows, so it credits the registers that recorded them too.
        context["credits"] = record_tabs.credits(
            _panel_provenance(record, changes[: record_tabs.OVERVIEW_ROWS])
        )
    elif tab == "changes":
        # Each change credits the register that recorded it.
        context["credits"] = record_tabs.credits(_panel_provenance(record, changes))
    elif tab == "sources":
        context["provenance_rows"] = provenance_panel_rows(api, _panel_provenance(record, changes))
    template = (
        "interconnection_point_detail.html" if tab == "overview" else f"interconnection_point_{tab}.html"
    )
    response = record_tabs.render(request, template, context, tabs)
    # One view of the record per visit (PM-5): the Overview counts, its tabs do not.
    return count_page_view(request, response, "point") if tabs.is_overview else response


def proposal_connection(api: Any, proposal_public_id: str | None) -> dict[str, Any] | None:
    """The "Connects at" row on a proposal page: the `interconnection_point` embed from the
    proposal's own detail response (the page itself resolves the record through the slug-filtered
    list, which does not carry it). Any failure drops the row, never the page."""
    if not proposal_public_id:
        return None
    try:
        embed = api.get(f"/v1/proposals/{proposal_public_id}")["data"].get("interconnection_point")
    except (ApiError, KeyError, TypeError):
        return None
    if not embed:
        return None
    return {
        "href": f"/interconnection-points/{embed['public_id']}",
        "name": embed.get("name"),
        "active_mw_label": _mw(embed.get("active_mw")),
        "active_count": embed.get("active_count", 0),
        "proposal_count": embed.get("proposal_count", 0),
    }
