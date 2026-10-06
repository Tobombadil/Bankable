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

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.api_client import ApiError, ApiNotFound
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
from web.viewmodels import iso_label, lifecycle_family, provenance_panel_rows, web_relative_url

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
SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("-active_mw", "Active queued MW, largest first"),
    ("name", "Name, A to Z"),
    ("-voltage_kv", "Voltage, highest first"),
)
PAGE_SIZE = 50


def _mw(value: Any) -> str:
    try:
        return f"{float(value):,.1f}"
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
    params: dict[str, str | None] = {name: qp[name] for name in POINT_INDEX_FILTERS if qp.get(name)}
    params["limit"] = str(PAGE_SIZE)
    params["cursor"] = qp.get("cursor")
    params["include"] = "count"
    try:
        envelope = api.get("/v1/interconnection-points", params=params)
    except ApiError as exc:
        if exc.status_code == 400:
            # A malformed filter from a hand-written link: show the unfiltered index, not a 503.
            envelope = api.get(
                "/v1/interconnection-points", params={"limit": str(PAGE_SIZE), "include": "count"}
            )
        else:
            return unavailable_response(request)
    records = [flatten_point(e) for e in envelope["data"]]
    canonical_path = "/interconnection-points" + canonical_query(qp, (*POINT_INDEX_FILTERS, "cursor"))
    sort = qp.get("sort") or "-active_mw"
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
        "sorts": SORT_OPTIONS,
        "sort": sort,
        "sort_caption": dict(SORT_OPTIONS).get(sort, "sorted as requested").lower(),
        "canonical_path": canonical_path,
        "jsonld": [
            item_list_jsonld(
                request,
                name="Grid interconnection points",
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


@router.get("/interconnection-points/{public_id}", response_class=HTMLResponse)
def interconnection_point_detail(request: Request, public_id: str) -> HTMLResponse:
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
    path = f"/interconnection-points/{public_id}"
    response = templates.TemplateResponse(
        request,
        "interconnection_point_detail.html",
        {
            "record": record,
            "proposals": proposals,
            "proposals_truncated": bool(entity.get("proposals_truncated")),
            "changes": [flatten_change(e) for e in changes],
            "provenance_rows": provenance_panel_rows(api, _panel_provenance(record, changes)),
            "canonical_path": path,
            "jsonld": [
                breadcrumb_jsonld(
                    request,
                    [
                        ("Home", "/"),
                        ("Grid interconnection points", "/interconnection-points"),
                        (record["name"], path),
                    ],
                )
            ],
        },
    )
    return count_page_view(request, response, "point")


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
