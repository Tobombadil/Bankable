"""`GET /v1/global/electricity`: Ember's operating capacity and generation for World, a region or a country
(`docs/26-global-figures.md`; owner, 2026-10-23: "If we have numbers that are credible for Global numbers
then let's lead with those as the top level global statistics you can drill into").

**What the numbers are.** Operating capacity (installed, in GW) and electricity generated (TWh), as Ember
publishes them: not a pipeline of proposed projects. The response says so (`measure: operating`, field
names `operating_capacity_gw` and `generation_twh`), and for `area=US` it links to Inframation's own US
pipeline figure by URL (lane front-api's `/v1/stats/capacity`), labelled `measure: pipeline`, so a page
never sets the two side by side as one measure. The link asks for the active pipeline only: the lifecycle
states announced through under construction (`org_pipeline.ACTIVE_LIFECYCLE_STATES`, the list's default
view) and records a register still lists (`listed=true`). Without that filter the endpoint counts every
state, built (operating) and withdrawn included, and would put operating MW inside a figure labelled
pipeline.

**Visibility.** The figures are served only while the source passes the visibility predicate at the
caller's tier (`provenance_visible`): an operator who takes the source off the surface takes the figures
with it. Before the first load, or with the source hidden, the response is `data.loaded: false` with no
figure, and the credit still names Ember (CC BY 4.0 asks for it wherever the work is used; it costs
nothing to say it where nothing is shown yet).

**Attribution.** `licence_summary` names the source and its licence; `notes.credits` carries Ember's own
citation form for each dataset served ("Yearly electricity data, Ember"), the licence URL and the
statement of changes, and every figure block carries its provenance (route, URL, retrieval time).
"""

from __future__ import annotations

import datetime as dt
import functools
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from pipeline.connectors.global_ember_electricity import SOURCE_ID
from pipeline.connectors.global_ember_electricity.ember import (
    API_DOCS_URL,
    CLEAN_SOURCES,
    DATASET_CITATIONS,
    DATASET_PAGES,
    FUEL_SOURCES,
)
from pipeline.connectors.registry import Registry, SourceEntry
from services.api.auth import AuthContext, get_auth_context
from services.api.common import API_HOST
from services.api.deps import get_db
from services.api.errors import not_found, validation_error
from services.api.org_pipeline import ACTIVE_LIFECYCLE_STATES
from services.api.params import check_allowed
from services.api.serialize import build_envelope, build_licence_summary, build_meta, licence_summary_row
from services.api.visibility import provenance_visible
from services.db.models import Source
from services.global_figures import read
from services.global_figures.models import GlobalElectricityLoad

router = APIRouter()

PARAMS = {"area", "years"}
DEFAULT_AREA = "World"
FIRST_YEAR, LAST_YEAR = 1990, 2100
#: Lane front-api's US pipeline figure, linked by URL, never imported (task, 2026-10-23): the active
#: states and listed records only (module docstring), the shape `organizations.list_queries` serves.
US_PIPELINE_ENDPOINT = "/v1/stats/capacity"
US_PIPELINE_QUERY: dict[str, str] = {
    "country": "US",
    "lifecycle_state": ",".join(ACTIVE_LIFECYCLE_STATES),
    "listed": "true",
}
US_PIPELINE_PATH = f"{US_PIPELINE_ENDPOINT}?" + "&".join(f"{k}={v}" for k, v in US_PIPELINE_QUERY.items())
MEASURE_NOTE = (
    "Operating capacity (installed) and electricity generated, as Ember publishes them: not a pipeline of "
    "proposed projects. Never add or compare these to a pipeline figure as one measure."
)
NO_YEARLY_CAPACITY = (
    "No yearly capacity is loaded: Ember's API serves no yearly capacity, and its yearly file is read only "
    "from a person's download (docs/26-global-figures.md §2)."
)
NO_AREA_CAPACITY = "Ember's yearly capacity has no value for this area."
US_PIPELINE_NOTE = (
    "Inframation's own count of active proposed US projects (announced through under construction, in "
    "interconnection queues and permits, that a register still lists): a pipeline, not operating capacity. "
    "It is a different measure from the figures above."
)
_YEARS = re.compile(r"^(\d{4})?(-)?(\d{4})?$")


@functools.lru_cache(maxsize=1)
def _entry() -> SourceEntry:
    return Registry().get(SOURCE_ID)


def parse_years(text: str | None, instance: str) -> tuple[int | None, int | None]:
    """`2025`, `2010-2025`, `2015-` or `-2010`; the series window, both ends included."""
    if text is None or not text.strip():
        return None, None
    m = _YEARS.match(text.strip())
    if not m or not (m.group(1) or m.group(3)):
        raise validation_error("years", "years must be YYYY, YYYY-YYYY, YYYY- or -YYYY", instance)
    first = int(m.group(1)) if m.group(1) else None
    last = int(m.group(3)) if m.group(3) else None
    if m.group(2) is None:
        last = first
    for value in (first, last):
        if value is not None and not FIRST_YEAR <= value <= LAST_YEAR:
            raise validation_error("years", f"years must lie between {FIRST_YEAR} and {LAST_YEAR}", instance)
    if first is not None and last is not None and first > last:
        raise validation_error("years", "the first year is after the last", instance)
    return first, last


def credits(datasets: set[str]) -> list[dict[str, Any]]:
    """Ember's citation per dataset served (all three when none is), with the licence and the changes."""
    entry = _entry()
    wanted = sorted(datasets) if datasets else sorted(DATASET_CITATIONS)
    return [
        {
            "text": DATASET_CITATIONS[d],
            "dataset": d,
            "dataset_url": DATASET_PAGES[d],
            "licence": "CC BY 4.0",
            "licence_url": entry.licence_url,
            "changes": entry.changes_statement,
            "source_id": SOURCE_ID,
        }
        for d in wanted
    ]


def _notes(datasets: set[str]) -> dict[str, Any]:
    entry = _entry()
    return {
        "credit": entry.credit_text,
        "credits": credits(datasets),
        "about": API_DOCS_URL,
        "clean_sources": sorted(CLEAN_SOURCES, key=FUEL_SOURCES.index),
        "sources": list(FUEL_SOURCES),
        "storage": "Ember's global data has no storage series; none is shown.",
    }


def _not_loaded(ctx: AuthContext, reason: str) -> dict[str, Any]:
    env = build_envelope(
        {"loaded": False, "reason": reason, "measure": "operating", "measure_note": MEASURE_NOTE},
        meta=build_meta(tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
    )
    env["notes"] = _notes(set())
    return env


def _licence_rows(source: Source, loads: list[GlobalElectricityLoad]) -> list[dict[str, Any]]:
    newest: dt.datetime | None = None
    for load in loads:
        when = read._aware(load.retrieved_at)
        newest = when if newest is None or when > newest else newest
    return [licence_summary_row(source, source.licence, newest)]


@router.get("/v1/global/electricity")
def global_electricity(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, PARAMS)
    path = request.url.path
    years = parse_years(request.query_params.get("years"), path)
    area_text = request.query_params.get("area") or DEFAULT_AREA
    source = db.get(Source, SOURCE_ID)
    if source is None or not provenance_visible(source, source.licence, ctx.entitlement):
        return _not_loaded(ctx, "No Ember figures are loaded on this server yet.")
    loads = read.current_loads(db)
    if not loads:
        return _not_loaded(ctx, "No Ember figures are loaded on this server yet.")
    area = read.find_area(read.areas(db, loads), area_text)
    if area is None:
        raise not_found(
            path,
            detail="No such area in Ember's data: use World, an ISO 3166-1 alpha-2 code or a region name.",
        )
    gen_load = read.pick(loads, "yearly", "generation_twh")
    cap_load = read.pick(loads, "yearly", "capacity_gw")
    gen = read.yearly(db, gen_load, area.id, "generation_twh")
    cap = read.yearly(db, cap_load, area.id, "capacity_gw")
    coverage = read.coverage(gen_load)
    complete = coverage["latest_complete_year"] if coverage else None
    # Ember estimates World and regional figures for a year later than the latest complete one.
    estimated_after = complete if area.kind != "country" else None
    generation = (
        read.generation_block(gen, gen_load, estimated_after=estimated_after)
        if gen_load is not None
        else None
    )
    capacity = (
        read.capacity_block(cap, cap_load, estimated_after=estimated_after) if cap_load is not None else None
    )
    latest_year = generation["year"] if generation else None
    month = read.latest_month(db, loads, area.id)
    additions = read.top_wind_solar_additions(db, loads)
    data: dict[str, Any] = {
        "loaded": True,
        "measure": "operating",
        "measure_note": MEASURE_NOTE,
        "area": area.to_dict(),
        "latest_year": latest_year,
        "latest_complete_year": complete,
        "latest_year_estimated": bool(
            area.kind != "country"
            and latest_year is not None
            and complete is not None
            and latest_year > complete
        ),
        "coverage": coverage,
        "generation": generation,
        "clean_share": read.clean_block(gen, estimated_after=estimated_after),
        "operating_capacity": capacity,
        "operating_capacity_withheld_reason": None
        if capacity is not None
        else (NO_YEARLY_CAPACITY if cap_load is None else NO_AREA_CAPACITY),
        "latest_month": month,
        "series": read.series_rows(gen, cap, years, estimated_after=estimated_after),
        "series_window": {"first_year": years[0], "last_year": years[1]},
        "top_wind_solar_additions": additions,
        "us_pipeline": {
            "measure": "pipeline",
            "path": US_PIPELINE_PATH,
            "url": f"{API_HOST}{US_PIPELINE_PATH}",
            "list_query": {"path": US_PIPELINE_ENDPOINT, "query": dict(US_PIPELINE_QUERY)},
            "note": US_PIPELINE_NOTE,
        }
        if area.id == "US"
        else None,
    }
    served = {load.dataset for load in loads}
    envelope = build_envelope(
        data,
        meta=build_meta(tier=ctx.entitlement),
        licence_summary=build_licence_summary(_licence_rows(source, loads)),
    )
    envelope["notes"] = _notes(served)
    return envelope
