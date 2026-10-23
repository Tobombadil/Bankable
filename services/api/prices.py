"""`GET /v1/prices/hubs`, `GET /v1/prices/hubs/{id}/series` and `GET /v1/prices/retail`
(docs/29 slice 1: hubs and zones first, and EIA's state retail prices).

Every value goes through the visibility predicate's two clauses (`services/api/visibility.py::
provenance_visible`): the location's source passes `source_permits` at the caller's tier and its
licence `licence_permits` under the platform posture. So SPP (`noncommercial`) appears only while
`PLATFORM_POSTURE=noncommercial`, and a source an operator takes off the surface takes its prices
with it. PJM, MISO and ISO-NE have no rows at all: the response names them as link-outs, with
their own price pages and the reason (`pipeline/connectors/prices/locations.py::LINK_OUTS`).

**Publication levels** (docs/29 §2, `data/sources.yaml` `publication`): the statistics are derived
values and publish for every visible location. The raw interval price, in the latest readout and
the daily series, is served only where the location's licence allows raw publication
(`licence.allows_raw_publication`: ERCOT, SPP, Elexon); for a derived-only source (CAISO, NYISO)
`latest` is null and the series endpoint answers the statistics with the reason instead.

**Credits.** Each location carries its source's credit line verbatim, and Elexon's licence
statement is rendered with the years of the data served ("[year] is the year of the data shown",
docs/29 §2.8). `notes.credits` lists each credit once with its licence's URL, so a page can link
the licence where it shows the statement (Elexon: "Where possible You must also contain a link to
this licence"; review 2026-10-23). Coordinates are served only at `point` grade (SPP's own display
points); a `region` location carries its `region_id` and a label point that the response marks as
a label.

**Sorting** (review 2026-10-23). A statistic's token without a market (`mean`, `basis`, ...) reads
the location's lead market, day-ahead where it has one and real-time otherwise (Great Britain),
which is what the page shows in its shared columns. Numbers in different currencies are never
interleaved: dollar rows sort among themselves, then pound rows; a location without the figure
sorts last either way.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from pipeline.connectors.prices.locations import LINK_OUTS, MARKET_LABELS, POSTURE_LINK_OUTS, label_point
from services.api.auth import AuthContext, get_auth_context
from services.api.common import iso
from services.api.deps import get_db
from services.api.errors import not_found, validation_error
from services.api.params import check_allowed, csv_param
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    licence_summary_row,
    provenance_quartet,
)
from services.api.visibility import provenance_visible
from services.db.models import Source
from services.posture import platform_posture
from services.prices.models import PriceDaily, PriceLatest, PriceLocation, PriceStat, RetailPrice

router = APIRouter()

MARKET_CODES = ("ERCOT", "SPP", "CAISO", "NYISO", "GB")
KINDS = ("hub", "zone")
STAT_FIELDS = ("mean", "mean_30d", "basis", "negative_share", "spread")
#: `sort=` tokens: a statistic of one market (`da_mean`, `rt_spread`, ...), the latest real-time
#: price, or the location's name or market. Numbers sort largest first by default on the page; the
#: API sorts ascending unless the token is prefixed `-`. A location without the figure sorts last
#: in both directions ("missing last").
SORTS: frozenset[str] = frozenset(
    {f"{m}_{f}" for m in ("da", "rt") for f in STAT_FIELDS}
    | set(STAT_FIELDS)
    | {"latest_rt", "latest_da", "name", "market"}
)
#: Numbers sort within a currency, in this order (module docstring).
CURRENCY_ORDER = ("USD", "GBP")
DEFAULT_SORT = "-da_mean"
LIST_PARAMS = {"market", "kind", "sort"}
SERIES_DAYS = 365
#: A day enters the series with at least this share of its intervals (the statistics' 90% rule).
SERIES_MIN_COVERAGE = 0.9
SCREENING_NOTE = (
    "Historical averages for screening, not a forecast: a location's past prices are not its "
    "future, and a delivered price for a new large load is negotiated."
)
_UNIT = {"USD": "$/MWh", "GBP": "£/MWh"}
_YEAR = re.compile(r"\b(19|20)\d{2}\b$")


def elexon_credit(text: str, years: Iterable[int]) -> str:
    """The BMRS statement with `[year]` as the years of the data served: `2025-2026` for a window
    that spans two."""
    span = sorted(set(years))
    if not span or not _YEAR.search(text):
        return text
    label = str(span[0]) if span[0] == span[-1] else f"{span[0]}-{span[-1]}"
    return _YEAR.sub(label, text)


def _credit(loc: PriceLocation, years: Iterable[int] = ()) -> str | None:
    text = loc.source.attribution_text or loc.licence.attribution_text or None
    if text and loc.source_id == "gb.elexon.bmrs_prices":
        return elexon_credit(text, years)
    return text


def _raw_ok(loc: PriceLocation) -> bool:
    return bool(loc.licence.allows_raw_publication)


def _visible_locations(db: Session, entitlement: str) -> list[PriceLocation]:
    rows = db.scalars(
        sa.select(PriceLocation).order_by(PriceLocation.market, PriceLocation.kind, PriceLocation.id)
    )
    return [loc for loc in rows if provenance_visible(loc.source, loc.licence, entitlement)]


def _num(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def serialize_stat(stat: PriceStat | None) -> dict[str, Any] | None:
    if stat is None:
        return None
    return {
        "window": stat.window_kind,
        "window_start": iso(stat.window_start),
        "window_end": iso(stat.window_end),
        "days": stat.days,
        "sample_count": stat.sample_count,
        "expected_count": stat.expected_count,
        "coverage": _num(stat.sample_count / stat.expected_count, 4) if stat.expected_count else None,
        "interval_minutes": stat.interval_minutes,
        "mean": _num(stat.mean),
        "mean_30d": _num(stat.mean_30d),
        "window_30d_start": iso(stat.window_30d_start),
        "samples_30d": stat.samples_30d,
        "basis": _num(stat.basis_mean),
        "basis_reference_id": stat.basis_reference_id,
        "basis_samples": stat.basis_samples,
        "negative_share": _num(stat.negative_share, 4),
        "spread": _num(stat.spread_mean),
        "spread_days": stat.spread_days,
        "withheld": dict(stat.withheld or {}),
        "computed_at": iso(stat.computed_at),
        "provenance": {
            "source_id": stat.source_id,
            "source_url": stat.source_url,
            "retrieved_at": iso(stat.retrieved_at_max),
            "licence_id": stat.licence_id,
            "derived": True,
        },
    }


def _placement(loc: PriceLocation) -> dict[str, Any]:
    point = loc.placement_grade == "point" and loc.lat is not None and _raw_ok(loc)
    return {
        "grade": loc.placement_grade if point or loc.placement_grade == "region" else "region",
        "method": loc.placement_method,
        "basis": loc.placement_basis,
        "region_id": loc.region_id,
        "lat": loc.lat if point else None,
        "lon": loc.lon if point else None,
        "evidence_url": loc.placement_evidence_url,
        "label_point": label_point(loc.region_id) if not point else None,
    }


def lead_market(markets: Iterable[str], stats: dict[str, PriceStat]) -> str:
    """The market a location's shared figures (30-day, basis, negative share, spread) come from:
    day-ahead where it has statistics, else real-time (Great Britain has no day-ahead market; a
    market whose day-ahead history is too short to summarise yet shows its real-time figures)."""
    if stats.get("da") is not None:
        return "da"
    if stats.get("rt") is not None:
        return "rt"
    return "da" if "da" in markets else "rt"


def serialize_location(
    loc: PriceLocation,
    stats: dict[str, PriceStat],
    latest: dict[str, PriceLatest],
    labels: dict[str, str],
) -> dict[str, Any]:
    raw_ok = _raw_ok(loc)
    years: set[int] = set()
    for stat in stats.values():
        years.update({stat.window_start.year, stat.window_end.year})
    latest_out: dict[str, Any] | None = None
    if raw_ok:
        latest_out = {}
        for market, row in latest.items():
            years.add(row.interval_start.year)
            latest_out[market] = {
                "interval_start": iso(row.interval_start),
                "interval_end": iso(row.interval_end),
                "price": _num(row.price),
                "retrieved_at": iso(row.retrieved_at),
                "source_url": row.source_url,
            }
    markets = loc.markets.split(",")
    labels_for = MARKET_LABELS.get(loc.market, {})
    return {
        "id": loc.id,
        "name": loc.name,
        "label": loc.label,
        "market": loc.market,
        "kind": loc.kind,
        "currency": loc.currency,
        "unit": _UNIT.get(loc.currency, f"{loc.currency}/MWh"),
        "markets": markets,
        "market_labels": {m: labels_for[m] for m in markets if m in labels_for},
        "lead_market": lead_market(markets, stats),
        "note": loc.note,
        "publication": "raw_ok" if raw_ok else "derived_only",
        "reference": (
            {"id": loc.reference_id, "label": labels.get(loc.reference_id or "", loc.reference_id)}
            if loc.reference_id
            else None
        ),
        "placement": _placement(loc),
        "statistics": {market: serialize_stat(stats.get(market)) for market in ("da", "rt")},
        "latest": latest_out,
        "series_url": f"/v1/prices/hubs/{loc.id}/series" if raw_ok else None,
        "credit": _credit(loc, years),
        "licence_url": loc.licence.url,
        "provenance": [
            provenance_quartet(
                loc.source, loc.licence, source_url=loc.source_url, retrieved_at=loc.retrieved_at
            )
        ],
    }


def _sort_value(item: dict[str, Any], field: str) -> Any:
    if field == "name":
        return str(item["label"]).lower()
    if field == "market":
        return item["market"]
    if field.startswith("latest_"):
        market = field.split("_", 1)[1]
        latest = (item.get("latest") or {}).get(market)
        return latest["price"] if latest else None
    if field in STAT_FIELDS:  # the lead market's figure (module docstring)
        block = item["statistics"].get(item.get("lead_market") or "da")
        return block.get(field) if block else None
    market, stat = field.split("_", 1)
    block = item["statistics"].get(market)
    return block.get(stat) if block else None


def _currency_rank(item: dict[str, Any]) -> int:
    currency = str(item.get("currency") or "")
    return CURRENCY_ORDER.index(currency) if currency in CURRENCY_ORDER else len(CURRENCY_ORDER)


def sort_items(items: list[dict[str, Any]], token: str) -> list[dict[str, Any]]:
    """Sort by `token` (`-` for descending) with missing values last either way, then by label. A
    number (any token but `name` and `market`) sorts within its currency, dollars first."""
    descending = token.startswith("-")
    field = token.lstrip("-")
    present = [i for i in items if _sort_value(i, field) is not None]
    missing = [i for i in items if _sort_value(i, field) is None]
    present.sort(key=lambda i: str(i["label"]).lower())
    present.sort(key=lambda i: _sort_value(i, field), reverse=descending)
    if field not in ("name", "market"):
        present.sort(key=_currency_rank)  # stable: each currency keeps the order above
    missing.sort(key=lambda i: str(i["label"]).lower())
    return present + missing


def _rows_by_location(db: Session, model: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not ids:
        return out
    for row in db.scalars(sa.select(model).where(model.location_id.in_(ids))):
        out.setdefault(row.location_id, {})[row.market] = row
    return out


def link_outs(visible_markets: Iterable[str] = ()) -> list[dict[str, str]]:
    """The markets never shown, then each posture-dependent market not visible to this caller (SPP
    under the commercial posture), each with its own price page and the reason."""
    visible = set(visible_markets)
    shown = [*LINK_OUTS, *(lo for lo in POSTURE_LINK_OUTS if lo.market not in visible)]
    return [
        {
            "market": lo.market,
            "label": lo.label,
            "url": lo.url,
            "reason": lo.reason,
            "reference": lo.reference,
        }
        for lo in shown
    ]


def credit_entries(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each credit once, with its licence's URL and source: what a page renders, linked."""
    seen: dict[str, dict[str, Any]] = {}
    for item in items:
        text = item.get("credit")
        if text and text not in seen:
            provenance = item.get("provenance") or [{}]
            seen[text] = {
                "text": text,
                "licence_url": item.get("licence_url") or None,
                "source_id": provenance[0].get("source_id"),
            }
    return [seen[text] for text in sorted(seen)]


def _licence_rows(locations: list[PriceLocation]) -> list[dict[str, Any]]:
    return [licence_summary_row(loc.source, loc.licence, loc.retrieved_at) for loc in locations]


@router.get("/v1/prices/hubs")
def list_price_hubs(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, LIST_PARAMS)
    qp = request.query_params
    locations = _visible_locations(db, ctx.entitlement)
    visible_markets = sorted({loc.market for loc in locations}, key=MARKET_CODES.index)
    if v := qp.get("market"):
        wanted = {m.upper() for m in csv_param(v)}
        unknown = sorted(wanted - set(MARKET_CODES))
        if unknown:
            raise validation_error(
                "market", f"market must be one of {', '.join(MARKET_CODES)}", request.url.path
            )
        locations = [loc for loc in locations if loc.market in wanted]
    if v := qp.get("kind"):
        if v not in KINDS:
            raise validation_error("kind", "kind must be hub or zone", request.url.path)
        locations = [loc for loc in locations if loc.kind == v]
    token = qp.get("sort") or DEFAULT_SORT
    if token.lstrip("-") not in SORTS:
        raise validation_error(
            "sort", f"sort field {token.lstrip('-')!r} is not allowlisted for this resource", request.url.path
        )
    ids = [loc.id for loc in locations]
    stats = _rows_by_location(db, PriceStat, ids)
    latest = _rows_by_location(db, PriceLatest, ids)
    labels = {loc.id: loc.label for loc in locations}
    items = [
        serialize_location(loc, stats.get(loc.id, {}), latest.get(loc.id, {}), labels) for loc in locations
    ]
    envelope = build_list_envelope(
        sort_items(items, token),
        meta=build_meta(tier=ctx.entitlement, extra={"total": len(items)}),
        licence_summary=build_licence_summary(_licence_rows(locations)),
        page=build_page(None, None, False),
    )
    envelope["notes"] = {
        "sort": token,
        "posture": platform_posture(),
        "screening_note": SCREENING_NOTE,
        "credits": credit_entries(items),
        "markets": visible_markets,
        "link_outs": link_outs(visible_markets),
    }
    return envelope


@router.get("/v1/prices/hubs/{location_id}/series")
def price_hub_series(
    request: Request,
    location_id: str,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, set())
    loc = db.get(PriceLocation, location_id)
    if loc is None or not provenance_visible(loc.source, loc.licence, ctx.entitlement):
        raise not_found(request.url.path)
    stats = {s.market: s for s in db.scalars(sa.select(PriceStat).where(PriceStat.location_id == loc.id))}
    latest = {
        r.market: r for r in db.scalars(sa.select(PriceLatest).where(PriceLatest.location_id == loc.id))
    }
    item = serialize_location(loc, stats, latest, {loc.id: loc.label})
    series: dict[str, list[dict[str, Any]]] | None = None
    reason: str | None = None
    if _raw_ok(loc):
        newest = db.scalar(sa.select(sa.func.max(PriceDaily.day)).where(PriceDaily.location_id == loc.id))
        series = {"da": [], "rt": []}
        if newest is not None:
            first = newest - dt.timedelta(days=SERIES_DAYS - 1)
            rows = list(
                db.scalars(
                    sa.select(PriceDaily)
                    .where(PriceDaily.location_id == loc.id, PriceDaily.day >= first)
                    .order_by(PriceDaily.market, PriceDaily.day)
                )
            )
            # A market's newest day that is not complete is a day still in progress (Great Britain's
            # 47 of 48 periods at the last fetch): the 90% rule below admits it, so it goes here.
            newest_by_market: dict[str, PriceDaily] = {r.market: r for r in rows}
            in_progress = {
                (r.market, r.day) for r in newest_by_market.values() if r.intervals < r.expected_intervals
            }
            years: set[int] = set()
            for r in rows:
                if (r.market, r.day) in in_progress:
                    continue
                if r.intervals < SERIES_MIN_COVERAGE * r.expected_intervals:
                    continue  # a partial day (SPP's hourly real-time readouts) is no daily mean
                years.add(r.day.year)
                series.setdefault(r.market, []).append(
                    {
                        "day": iso(r.day),
                        "mean": _num(r.mean),
                        "min": _num(r.min),
                        "max": _num(r.max),
                        "intervals": r.intervals,
                        "expected_intervals": r.expected_intervals,
                        "negative_intervals": r.negative_intervals,
                        "spread": _num(r.top4_mean - r.bottom4_mean)
                        if r.top4_mean is not None and r.bottom4_mean is not None
                        else None,
                        "retrieved_at": iso(r.retrieved_at),
                    }
                )
            item["credit"] = _credit(loc, years | {s.window_end.year for s in stats.values()})
    else:
        reason = (
            "This source's terms allow derived values only (data/sources.yaml `publication: derived_only`): "
            "the statistics are published; the daily series and the latest price are not."
        )
    data = {**item, "series": series, "series_withheld_reason": reason}
    envelope = build_envelope(
        data,
        meta=build_meta(tier=ctx.entitlement),
        licence_summary=build_licence_summary(_licence_rows([loc])),
    )
    envelope["notes"] = {"screening_note": SCREENING_NOTE, "posture": platform_posture()}
    return envelope


# ------------------------------------------------------------------------------------------ retail
RETAIL_SECTORS_SHOWN = ("industrial", "commercial", "residential")
RETAIL_PARAMS = {"sector", "sort"}
RETAIL_SORTS = frozenset({"state", "latest", "avg_12m"})
#: Rows read for the summaries: a state whose newest month lags the newest by up to a year still
#: finds its own twelve months (the window is its own, `_retail_summary`).
RETAIL_LOOKBACK_MONTHS = 24


def _months_back(period: str, months: int) -> str:
    """`2026-07`, 14 -> `2025-06`: the `YYYY-MM` period `months - 1` months before `period`."""
    year, month = (int(x) for x in period.split("-"))
    index = year * 12 + (month - 1) - (months - 1)
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _retail_summary(rows: list[RetailPrice]) -> dict[str, Any] | None:
    """Latest month and the trailing 12-month average for one state and sector. The average is
    revenue over sales across the twelve months (a sales-weighted average price, as EIA computes
    an annual average), not the mean of twelve monthly prices.

    The twelve months are the twelve calendar months ending with the latest priced month, each
    with its revenue and sales (review 2026-10-23): a month missing, or one without revenue or
    sales, withholds the average (`avg_12m_withheld` says why) rather than widening the window to
    an older month or counting the gap as zero."""
    priced = sorted((r for r in rows if r.price_cents_kwh is not None), key=lambda r: r.period)
    if not priced:
        return None
    latest = priced[-1]
    by_period = {r.period: r for r in rows}
    periods = [_months_back(latest.period, n) for n in range(12, 0, -1)]
    window = [by_period[p] for p in periods if p in by_period]
    complete = [r for r in window if r.revenue_thousand_usd is not None and r.sales_mwh]
    avg: float | None = None
    withheld: str | None = None
    if len(window) < 12:
        missing = [p for p in periods if p not in by_period]
        withheld = f"{len(missing)} of the twelve months are not published ({', '.join(missing[:3])})"
    elif len(complete) < 12:
        withheld = f"{12 - len(complete)} of the twelve months have no revenue or sales figure"
    else:
        revenue = sum(float(r.revenue_thousand_usd or 0.0) for r in complete)
        sales = sum(float(r.sales_mwh or 0.0) for r in complete)
        avg = revenue / sales * 100.0 if sales else None
    return {
        "latest_period": latest.period,
        "latest_cents_kwh": _num(latest.price_cents_kwh),
        "latest_status": latest.data_status,
        "avg_12m_cents_kwh": _num(avg),
        "avg_12m_from": periods[0] if avg is not None else None,
        "avg_12m_to": periods[-1] if avg is not None else None,
        "avg_12m_withheld": withheld,
        "months": len(complete),
        "preliminary_months": sum(1 for r in window if (r.data_status or "").lower() == "preliminary"),
    }


@router.get("/v1/prices/retail")
def list_retail_prices(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, RETAIL_PARAMS)
    qp = request.query_params
    sector = qp.get("sector") or "industrial"
    if sector not in RETAIL_SECTORS_SHOWN:
        raise validation_error(
            "sector", f"sector must be one of {', '.join(RETAIL_SECTORS_SHOWN)}", request.url.path
        )
    token = qp.get("sort") or "-latest"
    if token.lstrip("-") not in RETAIL_SORTS:
        raise validation_error(
            "sort", f"sort field {token.lstrip('-')!r} is not allowlisted", request.url.path
        )
    # Only what a summary reads: the three sectors shown, and the newest period's trailing two
    # years (`RETAIL_LOOKBACK_MONTHS`).
    newest_period = db.scalar(sa.select(sa.func.max(RetailPrice.period)))
    cutoff = _months_back(newest_period, RETAIL_LOOKBACK_MONTHS) if newest_period else None
    query = sa.select(RetailPrice).where(RetailPrice.sector.in_(RETAIL_SECTORS_SHOWN))
    if cutoff:
        query = query.where(RetailPrice.period >= cutoff)
    rows = list(db.scalars(query.order_by(RetailPrice.state, RetailPrice.period)))
    visible_sources: dict[str, bool] = {}
    by_state: dict[str, dict[str, list[RetailPrice]]] = {}
    provenance_rows: list[dict[str, Any]] = []
    released: str | None = None
    for row in rows:
        if row.source_id not in visible_sources:
            source = db.get(Source, row.source_id)
            ok = source is not None and provenance_visible(source, source.licence, ctx.entitlement)
            visible_sources[row.source_id] = ok
            if ok and source is not None:
                provenance_rows.append(licence_summary_row(source, source.licence, row.retrieved_at))
        if not visible_sources[row.source_id]:
            continue
        released = row.released or released
        by_state.setdefault(row.state, {}).setdefault(row.sector, []).append(row)
    items: list[dict[str, Any]] = []
    for state, sectors in by_state.items():
        summary = {s: _retail_summary(sectors.get(s, [])) for s in RETAIL_SECTORS_SHOWN}
        chosen = summary.get(sector)
        newest = max((r for rs in sectors.values() for r in rs), key=lambda r: r.retrieved_at)
        items.append(
            {
                "state": state,
                "sectors": summary,
                "latest_cents_kwh": chosen["latest_cents_kwh"] if chosen else None,
                "avg_12m_cents_kwh": chosen["avg_12m_cents_kwh"] if chosen else None,
                "provenance": {
                    "source_id": newest.source_id,
                    "source_url": newest.source_url,
                    "retrieved_at": iso(newest.retrieved_at),
                    "licence_id": newest.licence_id,
                },
            }
        )
    field = token.lstrip("-")
    key = {"state": "state", "latest": "latest_cents_kwh", "avg_12m": "avg_12m_cents_kwh"}[field]
    present = [i for i in items if i[key] is not None]
    missing = [i for i in items if i[key] is None]
    present.sort(key=lambda i: str(i["state"]))
    present.sort(key=lambda i: i[key], reverse=token.startswith("-"))
    credit = None
    if provenance_rows:
        base = provenance_rows[0].get("attribution_text") or "Source: U.S. Energy Information Administration"
        credit = f"{base}, released {released}" if released else base
    envelope = build_list_envelope(
        present + sorted(missing, key=lambda i: str(i["state"])),
        meta=build_meta(tier=ctx.entitlement, extra={"total": len(items)}),
        licence_summary=build_licence_summary(provenance_rows[:1]),
        page=build_page(None, None, False),
    )
    envelope["notes"] = {
        "sector": sector,
        "sort": token,
        "credit": credit,
        "released": released,
        "note": (
            "Average revenue per kilowatt-hour sold to each sector: an average, not a tariff, and "
            "not what a new large load would pay."
        ),
    }
    return envelope
