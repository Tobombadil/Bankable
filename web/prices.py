"""`/prices`: energy prices at the trading hubs and load zones, and EIA's state retail averages
(docs/29 slice 1; owner decision 2026-10-22: "every major price in America", hubs and zones first).

Read from `GET /v1/prices/hubs` and `GET /v1/prices/retail`; the page does no arithmetic of its own
beyond formatting, so it can never show a figure the API withheld. Average user first: a short
plain-language intro (what LMP, basis, negative hours and spread mean for a developer, one line
each), then the hubs-and-zones table with click-to-sort headings (largest first;
`web/list_sort.py`), a latest-price column only where the source's terms allow raw rows, the
link-out markets with the reason for each, the EIA state table, the credits and the screening note.
Two-line compact rows below 720px, like the other lists.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import QueryParams

from web.api_client import ApiError
from web.list_sort import SortColumn, chosen_sort, column, sort_caption, sort_headers, valid_sort
from web.page import canonical_query, get_api, is_htmx, templates, unavailable_response

router = APIRouter()

#: The hubs table's sortable headings -> the API's `sort` fields (`services/api/prices.py::SORTS`).
#: The four shared columns show each location's lead market (day-ahead, else real-time: Great
#: Britain), so they sort on the API's lead-market tokens, the figure shown (review 2026-10-23).
PRICE_COLUMNS: dict[str, SortColumn] = {
    "name": column("name", "name", "name"),
    "da": column("da_mean", "number", "average day-ahead price"),
    "rt": column("rt_mean", "number", "average real-time price"),
    "recent": column("mean_30d", "number", "last 30 days' average"),
    "basis": column("basis", "number", "basis to the reference hub"),
    "negative": column("negative_share", "number", "share of negative-price hours"),
    "spread": column("spread", "number", "daily top-4 minus bottom-4 spread"),
    "latest": column("latest_rt", "number", "latest real-time price"),
}
#: A statistic resting on fewer days than this is marked with its window in its cell (SPP's
#: 30-day history beside other markets' twelve months; review 2026-10-23).
SHORT_WINDOW_DAYS = 300
PRICE_OTHER: dict[str, SortColumn] = {
    "rt_recent": column("rt_mean_30d", "number", "last 30 days' real-time average"),
    "rt_negative": column("rt_negative_share", "number", "share of negative-price intervals"),
    "rt_spread": column("rt_spread", "number", "real-time daily spread"),
    "rt_basis": column("rt_basis", "number", "real-time basis"),
    "da_recent": column("da_mean_30d", "number", "last 30 days' day-ahead average"),
    "da_basis": column("da_basis", "number", "day-ahead basis"),
    "da_negative": column("da_negative_share", "number", "share of negative day-ahead hours"),
    "da_spread": column("da_spread", "number", "day-ahead daily spread"),
}
DEFAULT_SORT = "-da_mean"
FILTERS = ("market", "kind", "sort")
MARKET_OPTIONS = (
    ("ERCOT", "ERCOT (Texas)"),
    ("SPP", "SPP"),
    ("CAISO", "CAISO (California)"),
    ("NYISO", "NYISO (New York)"),
    ("GB", "Great Britain"),
)
#: The state table's headings: `<sector>_<figure>` -> (API `sector`, API `sort` field).
RETAIL_COLUMNS: dict[str, tuple[str, str, str]] = {
    "state": ("industrial", "state", "state"),
    "industrial_latest": ("industrial", "latest", "latest industrial price"),
    "industrial_avg": ("industrial", "avg_12m", "12-month industrial average"),
    "commercial_latest": ("commercial", "latest", "latest commercial price"),
    "commercial_avg": ("commercial", "avg_12m", "12-month commercial average"),
    "residential_latest": ("residential", "latest", "latest residential price"),
    "residential_avg": ("residential", "avg_12m", "12-month residential average"),
}
DEFAULT_RETAIL_SORT = "-industrial_latest"
STATE_NAMES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "US": "United States",
}  # fmt: skip
_SYMBOL = {"USD": "$", "GBP": "£"}


# ------------------------------------------------------------------------------------- formatting
def money(value: Any, currency: str = "USD", *, signed: bool = False) -> str | None:
    """`$31.42`, `−$4.10` (a true minus sign), `+$2.05` when `signed`; None for no value."""
    if value is None:
        return None
    number = float(value)
    symbol = _SYMBOL.get(currency, "")
    text = f"{symbol}{abs(number):,.2f}"
    if number < 0:
        return f"−{text}"
    return f"+{text}" if signed and number > 0 else text


def share(value: Any) -> str | None:
    return None if value is None else f"{float(value) * 100:.1f}%"


def _date(value: Any) -> str | None:
    if not value:
        return None
    day = dt.date.fromisoformat(str(value)[:10])
    return f"{day:%b} {day.day}, {day.year}"


def _time(value: Any) -> str | None:
    if not value:
        return None
    stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp.strftime("%Y-%m-%d %H:%M UTC")


def _stat(block: Mapping[str, Any] | None, key: str) -> Any:
    return block.get(key) if block else None


def _why(block: Mapping[str, Any] | None, key: str, *, collected: bool = True) -> str | None:
    """The API's reason a figure is withheld, for the cell's tooltip. With no statistics at all:
    a market the location does not have says so; one it has is waiting for history."""
    if not block:
        return (
            "No statistics yet: too little history held." if collected else "No such market at this location."
        )
    return (block.get("withheld") or {}).get(key)


def flatten_hub(item: Mapping[str, Any]) -> dict[str, Any]:
    """An API hub as the template reads it: every display string resolved once, here."""
    currency = str(item.get("currency") or "USD")
    stats = item.get("statistics") or {}
    da, rt = stats.get("da"), stats.get("rt")
    # The lead market is the API's (day-ahead where the market has one; Great Britain prices one
    # market, the imbalance price, in real time), the same one its lead-market sorts read.
    lead_key = str(item.get("lead_market") or ("da" if da else "rt"))
    lead = stats.get(lead_key)
    lead_market = "day-ahead" if lead_key == "da" else "real-time"
    latest = (item.get("latest") or {}).get("rt") if item.get("latest") else None
    window = lead or {}
    reference = item.get("reference") or {}
    labels = item.get("market_labels") or {}
    days = window.get("days")
    markets = set(item.get("markets") or ("da", "rt"))
    return {
        "id": item.get("id"),
        "label": item.get("label"),
        "name": item.get("name"),
        "market": item.get("market"),
        "kind": item.get("kind"),
        "kind_label": "Hub" if item.get("kind") == "hub" else "Zone",
        "unit": item.get("unit"),
        "currency": currency,
        "derived_only": item.get("publication") == "derived_only",
        "da": money(_stat(da, "mean"), currency),
        "da_why": _why(da, "mean", collected="da" in markets),
        "rt": money(_stat(rt, "mean"), currency),
        "rt_why": _why(rt, "mean", collected="rt" in markets),
        "recent": money(_stat(lead, "mean_30d"), currency),
        "recent_why": _why(lead, "mean_30d"),
        "basis": money(_stat(lead, "basis"), currency, signed=True),
        "basis_why": _why(lead, "basis"),
        "basis_reference": reference.get("label") if reference.get("id") != item.get("id") else None,
        "is_reference": bool(reference) and reference.get("id") == item.get("id"),
        "negative": share(_stat(lead, "negative_share")),
        "negative_why": _why(lead, "negative_share"),
        "spread": money(_stat(lead, "spread"), currency),
        "spread_why": _why(lead, "spread"),
        "lead_market": lead_market,
        "latest": money(latest.get("price"), currency) if latest else None,
        "latest_at": _time(latest.get("interval_end")) if latest else None,
        "window_from": _date(window.get("window_start")),
        "window_to": _date(window.get("window_end")),
        "window_days": days,
        "short_window": (
            f"{days} day{'s' if days != 1 else ''}"
            if isinstance(days, int) and days < SHORT_WINDOW_DAYS
            else None
        ),
        "da_label": labels.get("da"),
        "rt_label": labels.get("rt"),
        "samples": window.get("sample_count"),
        "credit": item.get("credit"),
        "note": item.get("note"),
        "placement_grade": (item.get("placement") or {}).get("grade"),
        "source_url": ((item.get("provenance") or [{}])[0]).get("source_url"),
    }


def flatten_state(item: Mapping[str, Any]) -> dict[str, Any]:
    sectors = item.get("sectors") or {}

    def cents(sector: str, key: str) -> str | None:
        block = sectors.get(sector) or {}
        value = block.get(key)
        return None if value is None else f"{float(value):.2f}¢"

    def why(sector: str) -> str | None:
        return (sectors.get(sector) or {}).get("avg_12m_withheld")

    industrial = sectors.get("industrial") or {}
    return {
        "state": item.get("state"),
        "name": STATE_NAMES.get(str(item.get("state")), str(item.get("state"))),
        "industrial_latest": cents("industrial", "latest_cents_kwh"),
        "industrial_avg": cents("industrial", "avg_12m_cents_kwh"),
        "industrial_avg_why": why("industrial"),
        "commercial_latest": cents("commercial", "latest_cents_kwh"),
        "commercial_avg": cents("commercial", "avg_12m_cents_kwh"),
        "commercial_avg_why": why("commercial"),
        "residential_latest": cents("residential", "latest_cents_kwh"),
        "residential_avg": cents("residential", "avg_12m_cents_kwh"),
        "residential_avg_why": why("residential"),
        "period": industrial.get("latest_period"),
        "preliminary": (industrial.get("latest_status") or "").lower() == "preliminary",
    }


def _credit(entry: Any) -> dict[str, str | None]:
    """A credit as the page renders it: the statement verbatim and, where its licence has one, a
    link to the licence (Elexon's licence asks for the link "where possible"; a web page is)."""
    if isinstance(entry, Mapping):
        url = entry.get("licence_url")
        return {"text": str(entry.get("text") or ""), "licence_url": str(url) if url else None}
    return {"text": str(entry), "licence_url": None}


def _period_label(period: str | None) -> str | None:
    if not period:
        return None
    year, month = (int(x) for x in period.split("-"))
    return f"{dt.date(year, month, 1):%B %Y}"


# ------------------------------------------------------------------------------------- state sort
def retail_sort_headers(path: str, qp: QueryParams, current: str) -> dict[str, dict[str, Any]]:
    """`web/list_sort.py::sort_headers` for the state table, whose order travels as `rsort` so the
    two tables on the page sort independently (the hubs table keeps `sort`)."""
    descending = current.startswith("-")
    field = current.lstrip("-")
    out: dict[str, dict[str, Any]] = {}
    for key, (_sector, _api, noun) in RETAIL_COLUMNS.items():
        first = "asc" if key == "state" else "desc"
        direction = ("desc" if descending else "asc") if key == field else None
        click = ("asc" if direction == "desc" else "desc") if direction else first
        token = f"-{key}" if click == "desc" else key
        kept = [(k, v) for k, v in qp.multi_items() if k != "rsort" and v != ""]
        kept.append(("rsort", token))
        words = (
            ("Z to A" if click == "desc" else "A to Z")
            if key == "state"
            else ("largest first" if click == "desc" else "smallest first")
        )
        out[key] = {
            "href": f"{path}?{urlencode(kept)}#states",
            "direction": direction,
            "aria_sort": {"asc": "ascending", "desc": "descending"}.get(direction or ""),
            "hint": f"Sort by {noun}, {words}",
        }
    return out


def _retail_params(token: str) -> dict[str, str]:
    field = token.lstrip("-")
    sector, api_sort, _noun = RETAIL_COLUMNS[field]
    return {"sector": sector, "sort": ("-" if token.startswith("-") else "") + api_sort}


# ------------------------------------------------------------------------------------------- page
@router.get("/prices", response_class=HTMLResponse)
def prices_page(request: Request) -> HTMLResponse:
    api = get_api(request)
    qp = request.query_params
    sort = valid_sort(qp.get("sort"), PRICE_COLUMNS, DEFAULT_SORT, PRICE_OTHER)
    params: dict[str, str | None] = {"sort": sort}
    for name in ("market", "kind"):
        if qp.get(name):
            params[name] = qp[name]
    rsort = qp.get("rsort") or DEFAULT_RETAIL_SORT
    if rsort.lstrip("-") not in RETAIL_COLUMNS:
        rsort = DEFAULT_RETAIL_SORT
    try:
        try:
            hubs = api.get("/v1/prices/hubs", params=params)
        except ApiError as exc:
            if exc.status_code != 400:
                raise
            hubs = api.get("/v1/prices/hubs", params={"sort": sort})
        retail = api.get("/v1/prices/retail", params=_retail_params(rsort))
    except ApiError:
        return unavailable_response(request)
    records = [flatten_hub(h) for h in hubs["data"]]
    states = [flatten_state(s) for s in retail["data"]]
    hub_notes = hubs.get("notes") or {}
    retail_notes = retail.get("notes") or {}
    # Offer only the markets this caller can see (SPP is withheld under the commercial posture
    # and stands in the link-outs instead; review 2026-10-23).
    visible = set(hub_notes.get("markets") or [code for code, _label in MARKET_OPTIONS])
    market_options = [(code, label) for code, label in MARKET_OPTIONS if code in visible]
    period = next((s["period"] for s in states if s["state"] == "US" and s["period"]), None) or next(
        (s["period"] for s in states if s["period"]), None
    )
    context = {
        "records": records,
        "states": states,
        "total": len(records),
        "filters": dict(qp),
        "market_options": market_options,
        "sort": sort,
        "sort_caption": sort_caption(sort, PRICE_COLUMNS, PRICE_OTHER),
        "sort_headers": sort_headers("/prices", qp, PRICE_COLUMNS, sort),
        "sort_param": chosen_sort(qp.get("sort"), PRICE_COLUMNS, PRICE_OTHER),
        "rsort": rsort,
        "retail_headers": retail_sort_headers("/prices", qp, rsort),
        "link_outs": hub_notes.get("link_outs") or [],
        "credits": [_credit(c) for c in hub_notes.get("credits") or []],
        "screening_note": hub_notes.get("screening_note"),
        "retail_credit": retail_notes.get("credit"),
        "retail_note": retail_notes.get("note"),
        "retail_period": _period_label(period),
        "any_latest": any(r["latest"] for r in records),
        "canonical_path": "/prices" + canonical_query(qp, ("market", "kind")),
    }
    if is_htmx(request) and request.headers.get("HX-Target") == "price-rows":
        # The credits of the rows swapped in travel with them (`hx-swap-oob`): a Great Britain-only
        # swap must not leave the page without the Elexon statement (review 2026-10-23).
        return templates.TemplateResponse(
            request, "partials/_price_rows.html", {**context, "oob_credits": True}
        )
    return templates.TemplateResponse(request, "prices.html", context)
