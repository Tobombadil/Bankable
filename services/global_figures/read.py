"""The figures `GET /v1/global/electricity` serves, read from the global electricity tables.

Every figure is computed from one dataset of one load (`pick`): a series never splices two datasets or two
routes into one line (owner, 2026-10-23: "Series from different sources are never spliced into one line,
and changes are compared only like for like"). Year-on-year changes compare the same area, source and
metric in consecutive years of the same load, and a total's (or a share's) change compares two years only
when both carry the same generation types: a type present in one year and missing in the other would
compare two different sums, so the change is null and `sources_differ` names the types.

`pick` takes, of a dataset's loads that carry the metric, the one with the newest period; on a tie, the
API's (Ember's own automated answer: a person's download can be an older file copied in later), then the
newest retrieved.

Definitions (docs/26-global-figures.md §5):

* **Clean share**: generation from solar, wind, hydro, nuclear, bioenergy and other renewables, over
  generation from Ember's nine generation types, in percent (`ember.CLEAN_SOURCES`).
* **Latest year** of an area: the newest year in which it has a generation value.
* **Latest complete year**: scanning down from the newest year, the first whose countries with a
  generation value are at least 90 % of the year before's. Ember's latest yearly data covers fewer countries
  than earlier years until national statistics report, and Ember estimates World and regional figures for
  that year from the countries it has (methodology, "Regional and world estimates"). So every block and
  series row of World or a region for a later year carries `estimated: true`.
* **Wind and solar added**: a country's operating solar plus wind capacity at the end of a year minus the
  same at the end of the year before, a **net** change (retirements subtract), ranked in the latest
  complete capacity year, so a ranking never compares a handful of early reporters. Solar is AC or DC as
  each country reports it (Ember's methodology), so two countries' solar figures need not share a basis.
  From Ember's yearly capacity; without it, from Ember's monthly wind and solar capacity (December against
  December), which covers fewer countries, and the response says so.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from pipeline.connectors.global_ember_electricity.areas import ISO3_TO_ISO2, WORLD_ID, region_slug
from pipeline.connectors.global_ember_electricity.ember import (
    CLEAN_SOURCES,
    DATASET_CITATIONS,
    DATASET_PAGES,
    FUEL_SOURCES,
    SOURCE_LABELS,
)
from services.global_figures.models import GlobalElectricityLoad, GlobalElectricityValue

COMPLETE_SHARE = 0.9
TOP_N = 10
WIND_SOLAR = ("solar", "wind")
COVERAGE_YEARS = 6
#: What an `added_gw` is (module docstring, "Wind and solar added").
NET_NOTE = (
    "Added capacity is a net change: operating capacity at the end of the period minus the same a year "
    "earlier, so retirements subtract. Solar is AC or DC as each country reports it."
)

Series = dict[int, dict[str, float]]


@dataclass(frozen=True)
class AreaRef:
    id: str
    name: str
    kind: str
    iso3: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "kind": self.kind, "iso3": self.iso3}


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def current_loads(session: Session) -> list[GlobalElectricityLoad]:
    return list(session.scalars(sa.select(GlobalElectricityLoad).order_by(GlobalElectricityLoad.id)))


def pick(loads: Sequence[GlobalElectricityLoad], dataset: str, metric: str) -> GlobalElectricityLoad | None:
    """The load a figure comes from: of `dataset`'s loads carrying `metric`, the one with the newest
    period; on a tie the API's, then the newest retrieved (module docstring)."""
    found = [load for load in loads if load.dataset == dataset and metric in load.metrics.split(",")]
    if not found:
        return None
    return max(
        found,
        key=lambda load: (str(load.last_period or ""), load.origin == "api", _aware(load.retrieved_at)),
    )


def provenance(load: GlobalElectricityLoad) -> dict[str, Any]:
    return {
        "dataset": load.dataset,
        "route": load.origin,
        "source_id": load.source_id,
        "source_url": load.source_url,
        "retrieved_at": _aware(load.retrieved_at).isoformat().replace("+00:00", "Z"),
        "licence_id": load.licence_id,
        "citation": DATASET_CITATIONS[load.dataset],
        "dataset_url": DATASET_PAGES[load.dataset],
    }


# ------------------------------------------------------------------------------------- areas
def areas(session: Session, loads: Sequence[GlobalElectricityLoad]) -> list[AreaRef]:
    """Every area the current loads hold, the yearly load's names first."""
    order = sorted(loads, key=lambda load: (load.dataset != "yearly", load.origin != "api"))
    seen: dict[str, AreaRef] = {}
    for load in order:
        rows = session.execute(
            sa.select(
                GlobalElectricityValue.area_id,
                GlobalElectricityValue.area_name,
                GlobalElectricityValue.area_kind,
                GlobalElectricityValue.iso3,
            )
            .where(GlobalElectricityValue.load_id == load.id)
            .distinct()
        )
        for area_id, name, kind, iso3 in rows:
            seen.setdefault(area_id, AreaRef(area_id, name, kind, iso3))
    return sorted(seen.values(), key=lambda a: (("world", "region", "country").index(a.kind), a.name))


def find_area(known: Iterable[AreaRef], text: str) -> AreaRef | None:
    """`World`, an ISO 3166-1 alpha-2 (or alpha-3) code, or a region's name or slug, in any case."""
    wanted = text.strip()
    if not wanted:
        return None
    by_id = {a.id: a for a in known}
    if wanted.lower() == WORLD_ID:
        return by_id.get(WORLD_ID)
    upper = wanted.upper()
    if len(upper) == 2 and upper.isalpha() and upper in by_id:
        return by_id[upper]
    if len(upper) == 3 and upper.isalpha() and ISO3_TO_ISO2.get(upper) in by_id:
        return by_id[ISO3_TO_ISO2[upper]]
    slug = region_slug(wanted)
    for area in by_id.values():
        if area.kind != "country" and (area.id == slug or region_slug(area.name) == slug):
            return area
    return None


# ------------------------------------------------------------------------------------ values
def yearly(session: Session, load: GlobalElectricityLoad | None, area_id: str, metric: str) -> Series:
    """Year -> source -> value for one area and metric of one load."""
    out: Series = defaultdict(dict)
    if load is None:
        return {}
    rows = session.execute(
        sa.select(
            GlobalElectricityValue.year, GlobalElectricityValue.source, GlobalElectricityValue.value
        ).where(
            GlobalElectricityValue.load_id == load.id,
            GlobalElectricityValue.area_id == area_id,
            GlobalElectricityValue.metric == metric,
        )
    )
    for year, source, value in rows:
        out[int(year)][source] = float(value)
    return dict(out)


def monthly(
    session: Session, load: GlobalElectricityLoad | None, area_id: str, metric: str
) -> dict[tuple[int, int], dict[str, float]]:
    out: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
    if load is None:
        return {}
    rows = session.execute(
        sa.select(
            GlobalElectricityValue.year,
            GlobalElectricityValue.month,
            GlobalElectricityValue.source,
            GlobalElectricityValue.value,
        ).where(
            GlobalElectricityValue.load_id == load.id,
            GlobalElectricityValue.area_id == area_id,
            GlobalElectricityValue.metric == metric,
        )
    )
    for year, month, source, value in rows:
        if month is not None:
            out[(int(year), int(month))][source] = float(value)
    return dict(out)


def _fuel_total(values: dict[str, float]) -> float | None:
    parts = [values[s] for s in FUEL_SOURCES if s in values]
    return sum(parts) if parts else None


def sources_differ(now: dict[str, float], before: dict[str, float]) -> list[str]:
    """The generation types present in one of two periods and missing in the other (none when `before`
    is empty: there is nothing to compare). A total's or a share's change needs this list empty."""
    if not before:
        return []
    return [s for s in FUEL_SOURCES if (s in now) != (s in before)]


def _estimated(year: int, estimated_after: int | None) -> bool:
    return estimated_after is not None and year > estimated_after


def clean_share(values: dict[str, float]) -> float | None:
    total = _fuel_total(values)
    if not total:
        return None
    return sum(values.get(s, 0.0) for s in CLEAN_SOURCES) / total * 100


def _change(now: float | None, before: float | None) -> tuple[float | None, float | None]:
    if now is None or before is None:
        return None, None
    pct = (now - before) / before * 100 if before > 0 else None
    return now - before, pct


def _latest_with_fuels(series: Series) -> int | None:
    years = [y for y, values in series.items() if any(s in values for s in FUEL_SOURCES)]
    return max(years) if years else None


# ---------------------------------------------------------------------------------- coverage
def complete_year(counts: dict[int, int]) -> int | None:
    """Scanning down from the newest year, the first whose count is at least 90 % of the year before's."""
    years = sorted(counts, reverse=True)
    for year in years:
        before = counts.get(year - 1)
        if before is None or counts[year] >= COMPLETE_SHARE * before:
            return year
    return None


def coverage(load: GlobalElectricityLoad | None) -> dict[str, Any] | None:
    if load is None:
        return None
    counts = {int(y): int(n) for y, n in (load.countries_by_year or {}).items()}
    if not counts:
        return None
    latest = max(counts)
    complete = complete_year(counts)
    shown = sorted(counts)[-COVERAGE_YEARS:]
    return {
        "latest_year": latest,
        "latest_complete_year": complete,
        "countries_latest_year": counts[latest],
        "countries_latest_complete_year": counts.get(complete) if complete is not None else None,
        "countries_by_year": [{"year": y, "countries": counts[y]} for y in shown],
        "rule": (
            "A year is complete when the countries with a generation value are at least 90% of the year "
            "before's; the latest complete year is the newest such year."
        ),
        "note": (
            "Ember's latest yearly data covers fewer countries than earlier years until national statistics "
            "report; Ember estimates World and regional figures for that year from the countries it has."
        ),
    }


# ----------------------------------------------------------------------------------- blocks
def generation_block(
    series: Series, load: GlobalElectricityLoad, *, estimated_after: int | None = None
) -> dict[str, Any] | None:
    """The latest year's generation by source with its change on the year before. `estimated_after` is
    the latest complete year for World or a region (None for a country): a later year is Ember's
    estimate, flagged `estimated`."""
    year = _latest_with_fuels(series)
    if year is None:
        return None
    now, before = series.get(year, {}), series.get(year - 1, {})
    differ = sources_differ(now, before)
    total = _fuel_total(now)
    total_before = _fuel_total(before) if not differ else None
    rows = []
    for source in FUEL_SOURCES:
        value = now.get(source)
        prev = before.get(source)
        change, pct = _change(value, prev)
        share = value / total * 100 if value is not None and total else None
        share_before = prev / total_before * 100 if prev is not None and total_before else None
        rows.append(
            {
                "source": source,
                "label": SOURCE_LABELS[source],
                "generation_twh": _r(value),
                "previous_twh": _r(prev),
                "change_twh": _r(change),
                "change_pct": _r(pct, 1),
                "share_pct": _r(share),
                "share_change_pp": _r(
                    share - share_before if share is not None and share_before is not None else None
                ),
            }
        )
    change, pct = _change(total, total_before)
    return {
        "year": year,
        "previous_year": year - 1 if before else None,
        "estimated": _estimated(year, estimated_after),
        "unit": "TWh",
        "total_twh": _r(total),
        "total_change_twh": _r(change),
        "total_change_pct": _r(pct, 1),
        "sources_differ": differ,
        "by_source": rows,
        "provenance": provenance(load),
    }


def clean_block(series: Series, *, estimated_after: int | None = None) -> dict[str, Any] | None:
    year = _latest_with_fuels(series)
    if year is None:
        return None
    values, previous = series.get(year, {}), series.get(year - 1, {})
    differ = sources_differ(values, previous)
    now = clean_share(values)
    before = clean_share(previous)
    return {
        "year": year,
        "estimated": _estimated(year, estimated_after),
        "pct": _r(now),
        "previous_year": year - 1 if before is not None else None,
        "previous_pct": _r(before),
        "change_pp": _r(now - before if now is not None and before is not None and not differ else None),
        "sources_differ": differ,
        "definition": sorted(CLEAN_SOURCES, key=FUEL_SOURCES.index),
        "basis": "share of generation from Ember's nine generation types",
    }


def capacity_block(
    series: Series, load: GlobalElectricityLoad, *, estimated_after: int | None = None
) -> dict[str, Any] | None:
    year = _latest_with_fuels(series)
    if year is None:
        return None
    now, before = series.get(year, {}), series.get(year - 1, {})
    differ = sources_differ(now, before)
    rows = []
    for source in FUEL_SOURCES:
        change, pct = _change(now.get(source), before.get(source))
        rows.append(
            {
                "source": source,
                "label": SOURCE_LABELS[source],
                "operating_capacity_gw": _r(now.get(source)),
                "previous_gw": _r(before.get(source)),
                "change_gw": _r(change),
                "change_pct": _r(pct, 1),
            }
        )
    total, total_before = _fuel_total(now), (_fuel_total(before) if not differ else None)
    change, pct = _change(total, total_before)
    return {
        "year": year,
        "previous_year": year - 1 if before else None,
        "estimated": _estimated(year, estimated_after),
        "unit": "GW",
        "total_gw": _r(total),
        "total_change_gw": _r(change),
        "total_change_pct": _r(pct, 1),
        "sources_differ": differ,
        "by_source": rows,
        "provenance": provenance(load),
    }


def series_rows(
    gen: Series,
    cap: Series,
    years: tuple[int | None, int | None],
    *,
    estimated_after: int | None = None,
) -> list[dict[str, Any]]:
    first, last = years
    out = []
    for year in sorted(set(gen) | set(cap)):
        if (first is not None and year < first) or (last is not None and year > last):
            continue
        g, c = gen.get(year, {}), cap.get(year, {})
        out.append(
            {
                "year": year,
                "estimated": _estimated(year, estimated_after),
                "generation_twh": {s: _r(g[s]) for s in FUEL_SOURCES if s in g} or None,
                "total_generation_twh": _r(_fuel_total(g)),
                "clean_share_pct": _r(clean_share(g)),
                "operating_capacity_gw": {s: _r(c[s]) for s in FUEL_SOURCES if s in c} or None,
                "total_operating_capacity_gw": _r(_fuel_total(c)),
            }
        )
    return out


def latest_month(
    session: Session,
    loads: Sequence[GlobalElectricityLoad],
    area_id: str,
) -> dict[str, Any] | None:
    """The newest month of Ember's monthly generation (and of its monthly wind and solar capacity), each
    against the same month a year earlier."""
    out: dict[str, Any] = {}
    gen_load = pick(loads, "monthly", "generation_twh")
    gen = monthly(session, gen_load, area_id, "generation_twh")
    months = [k for k, v in gen.items() if any(s in v for s in FUEL_SOURCES)]
    if gen_load is not None and months:
        year, month = max(months)
        now, before = gen[(year, month)], gen.get((year - 1, month), {})
        rows = []
        for source in FUEL_SOURCES:
            change, pct = _change(now.get(source), before.get(source))
            rows.append(
                {
                    "source": source,
                    "label": SOURCE_LABELS[source],
                    "generation_twh": _r(now.get(source), 3),
                    "same_month_last_year_twh": _r(before.get(source), 3),
                    "change_pct": _r(pct, 1),
                }
            )
        differ = sources_differ(now, before)
        share_now, share_before = clean_share(now), (clean_share(before) if not differ else None)
        out["generation"] = {
            "month": f"{year:04d}-{month:02d}",
            "unit": "TWh",
            "total_twh": _r(_fuel_total(now), 3),
            "by_source": rows,
            "clean_share_pct": _r(share_now),
            "clean_share_change_pp": _r(
                share_now - share_before if share_now is not None and share_before is not None else None
            ),
            "sources_differ": differ,
            "provenance": provenance(gen_load),
        }
    cap_load = pick(loads, "monthly_capacity", "capacity_gw")
    cap = monthly(session, cap_load, area_id, "capacity_gw")
    cap_months = [k for k, v in cap.items() if any(s in v for s in WIND_SOLAR)]
    if cap_load is not None and cap_months:
        year, month = max(cap_months)
        now, before = cap[(year, month)], cap.get((year - 1, month), {})
        rows = []
        for source in WIND_SOLAR:
            change, _pct = _change(now.get(source), before.get(source))
            rows.append(
                {
                    "source": source,
                    "label": SOURCE_LABELS[source],
                    "operating_capacity_gw": _r(now.get(source)),
                    "same_month_last_year_gw": _r(before.get(source)),
                    "added_gw": _r(change),
                }
            )
        out["wind_solar_capacity"] = {
            "month": f"{year:04d}-{month:02d}",
            "unit": "GW",
            "by_source": rows,
            "note": NET_NOTE,
            "provenance": provenance(cap_load),
        }
    return out or None


# ------------------------------------------------------------------------------- rankings
def _ranking(
    by_country: dict[str, dict[int, dict[str, float]]],
    names: dict[str, AreaRef],
    year: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Countries by solar plus wind added between the end of `year - 1` and of `year`: a source a
    country reports in neither year counts zero; one it reports in only one makes the country
    incomplete, and it is left out (counted)."""
    ranked: list[dict[str, Any]] = []
    incomplete = 0
    for area_id, years in by_country.items():
        now, before = years.get(year), years.get(year - 1)
        if now is None or before is None:
            continue
        added: dict[str, float] = {}
        ok = True
        for source in WIND_SOLAR:
            if source in now and source in before:
                added[source] = now[source] - before[source]
            elif source in now or source in before:
                ok = False
            else:
                added[source] = 0.0
        if not ok:
            incomplete += 1
            continue
        ranked.append(
            {
                "area": names[area_id].to_dict(),
                "added_gw": sum(added.values()),
                "solar_added_gw": added["solar"],
                "wind_added_gw": added["wind"],
                "operating_wind_solar_gw": now.get("solar", 0.0) + now.get("wind", 0.0),
            }
        )
    compared = len(ranked)
    ranked.sort(key=lambda r: (-r["added_gw"], r["area"]["name"]))
    top = []
    for rank, row in enumerate(ranked[:TOP_N], start=1):
        top.append(
            {
                "rank": rank,
                "area": row["area"],
                "added_gw": _r(row["added_gw"]),
                "solar_added_gw": _r(row["solar_added_gw"]),
                "wind_added_gw": _r(row["wind_added_gw"]),
                "operating_wind_solar_gw": _r(row["operating_wind_solar_gw"]),
            }
        )
    return top, compared, incomplete


def top_wind_solar_additions(
    session: Session, loads: Sequence[GlobalElectricityLoad]
) -> dict[str, Any] | None:
    load = pick(loads, "yearly", "capacity_gw")
    basis = "yearly"
    if load is None:
        load = pick(loads, "monthly_capacity", "capacity_gw")
        basis = "monthly_december"
    if load is None:
        return None
    query = sa.select(
        GlobalElectricityValue.area_id,
        GlobalElectricityValue.area_name,
        GlobalElectricityValue.iso3,
        GlobalElectricityValue.year,
        GlobalElectricityValue.month,
        GlobalElectricityValue.source,
        GlobalElectricityValue.value,
    ).where(
        GlobalElectricityValue.load_id == load.id,
        GlobalElectricityValue.area_kind == "country",
        GlobalElectricityValue.metric == "capacity_gw",
        GlobalElectricityValue.source.in_(WIND_SOLAR),
    )
    if basis == "monthly_december":
        query = query.where(GlobalElectricityValue.month == 12)
    by_country: dict[str, dict[int, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    names: dict[str, AreaRef] = {}
    for area_id, name, iso3, year, _month, source, value in session.execute(query):
        by_country[area_id][int(year)][source] = float(value)
        names.setdefault(area_id, AreaRef(area_id, name, "country", iso3))
    counts: dict[int, int] = defaultdict(int)
    for years in by_country.values():
        for year in years:
            counts[year] += 1
    year = complete_year(dict(counts))
    if year is None or year - 1 not in counts:
        return None
    top, compared, incomplete = _ranking(by_country, names, year)
    latest = max(counts)
    note = (
        "Net operating capacity added in the year (end of year against end of the year before, so "
        "retirements subtract), ranked in the latest complete capacity year. Solar is AC or DC as each "
        "country reports it, so two countries' solar figures need not share a basis."
    )
    if basis == "monthly_december":
        note += (
            " From Ember's monthly wind and solar capacity (December against December), which covers fewer "
            "countries than its yearly data; no yearly capacity is loaded."
        )
    return {
        "year": year,
        "latest_capacity_year": latest,
        "basis": basis,
        "unit": "GW",
        "countries": top,
        "countries_compared": compared,
        "countries_incomplete": incomplete,
        "countries_with_capacity_latest_year": counts[latest],
        "countries_with_capacity_previous_year": counts.get(latest - 1),
        "note": note,
        "provenance": provenance(load),
    }
