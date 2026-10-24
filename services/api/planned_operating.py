"""`GET /v1/stats/planned-vs-operating`: what is planned against what runs today, so the scale of the
build-out reads at a glance, and honestly (owner, 2026-10-23: "could we show planned projects against
active project? ... Plus it helps put the datacenter build out in comparison"; docs/00-PLAN.md row
"Planned is shown against operating"; docs/24-trends.md §11).

**Power plants: one survey, one unit, one month.** Both sides are EIA-860M's, from the newest month
the trends lane's monthly documents serve (`services.trends.series.latest_month`): the Operating
sheet (every status on it: operating, standby, out of service; EIA's "Inventory of Operating
Generators") and the Planned sheet, generators counted and net summer MW summed, the 50 states and
DC. Nothing from the queue register is mixed in. Every figure is the point at that month of a
`GET /v1/trends` series, named under `series` with the `lines` whose values add up to it
(`us_operating_capacity_mw`, `us_planned_capacity_mw` and their generator counts), so it reproduces
exactly. A line the series does not have is zero only because every key a figure reads is one the
month tallies: a state is one of the 50 and DC, which the month's state tally covers completely, and
a technology must be a key of the month's technology tally on one side or the other (any other
token is refused, since EIA-860M's classifier never produces it and a zero would be false).

**Planned is split by stage**, mapped from EIA's planned status codes (`STAGES`, served under
`stage_mapping`): construction complete, not yet in commercial operation (TS), under construction
(U, V), regulatory approvals received (T), approvals pending (L), and early or other (P, OT). A code
outside the vocabulary is its own `unmapped` row, so the stages always add up to the total. The
monthly document tallies the status for the 50 states and DC together, and the state and the
technology each on their own, so under a `state` or `technology` filter the planned total has no
stage split (`by_stage` null, `by_stage_note` says why) and the operating status split is null too:
a deviation from the PLAN row's "always split by stage" until the two-level tally lands (docs/24 §11.1).

**The completion rate is one fixed, cited line** (`COMPLETION_RATE`), never a computation: Berkeley
Lab, Queued Up 2026: of the capacity that requested interconnection in 2000-2020, 13% had reached
commercial operation by the end of 2025 and 75% had been withdrawn. It describes queue requests,
not EIA's planned generators, and says so.

**Data centres compare by power, never by site counts.** Planned is the demand MW of the register's
`load` records (data centres and other large loads, as the site's own figure counts them) in an
active lifecycle state that a register still lists, in the 50 states and DC, with how many state MW
and how many do not, against today's average US data-centre demand and its 2028 range
(`DATA_CENTRE_DEMAND`, the DOE's release of Berkeley Lab's 2024 report: 176 TWh in 2023, about 20 GW
on average; 325-580 TWh in 2028). These are the capacity endpoint's own figures
(`services/api/front.py`: `served_rows`, `capacity_summary`, `CapacitySelection`), so each carries
the list query whose `include=count` total it is, the licence gate is that endpoint's exactly (a
restricted or unknown-terms register, a register not cleared for the tier, a record not yet public
is never counted, and a value a withheld register supplied is counted as served), and records no
register lists any more are never counted in an active state. A sum over no stated MW is null,
never zero. The DOE and Berkeley Lab figures are national only, so under a `state` filter
`average_demand` is null and `average_demand_note` says why: a state's planned MW is never set
against all-US demand. Site counts appear only as "sites we track", every lifecycle state, with
their coverage stated: no complete public inventory of US data centres exists.

**Filters.** `state` (`US-TX`, comma-separated for several, the 50 states and DC) narrows both
halves (the data-centre lists by `jurisdiction`); `technology` (the tokens the served EIA-860M month
tallies) narrows the power plants and leaves data centres out, since a load is not a power-plant
technology. The two together are refused: the document tallies neither side by state and
technology at once.

**Who sees what.** The power plants are served only while `us.eia.860m.history` is visible at the
caller's tier (`visibility.source_visible`, the rule `GET /v1/trends` applies); otherwise
`power_plants` is null and `power_plants_note` says which of the three reasons holds (not served at
the tier, no month read, or the month lacks a tally the figures read). The licence summary has one
row for the EIA month read and one per source of the data-centre records read.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from pipeline.connectors.history.eia860m.connector import US_STATES_DC
from services.api.auth import AuthContext, get_auth_context
from services.api.deps import get_db
from services.api.errors import validation_error
from services.api.front import CapacitySelection, capacity_summary, served_rows
from services.api.org_pipeline import ACTIVE_LIFECYCLE_STATES
from services.api.params import check_allowed, csv_param
from services.api.records import _source_licence_aggregate
from services.api.serialize import (
    _finalize_licence_summary,
    build_envelope,
    build_licence_summary,
    build_meta,
    licence_summary_row,
    source_credit,
)
from services.api.visibility import source_visible
from services.db.models import ProposalSource, Source
from services.labels import TECHNOLOGY_LABELS
from services.trends import series as trends

router = APIRouter()

PATH = "/v1/stats/planned-vs-operating"
PARAMS = frozenset({"state", "technology"})
TRENDS_PATH = "/v1/trends"
EIA = trends.EIA

#: The `GET /v1/trends` series each side's figures are points of, MW and generators.
SERIES: dict[str, dict[str, str]] = {
    "operating": {"mw": "us_operating_capacity_mw", "generators": "us_operating_generators"},
    "planned": {"mw": "us_planned_capacity_mw", "generators": "us_planned_generators"},
}

#: EIA-860M's status labels, as the September 2026 workbook writes them ("(U) Under construction,
#: ..."); OT is on the connector's planned vocabulary (`KNOWN_STATUS`) though no recent month has it.
STATUS_LABELS: dict[str, str] = {
    "OP": "Operating",
    "SB": "Standby/Backup: available for service but not normally used",
    "OS": "Out of service and NOT expected to return to service in next calendar year",
    "OA": "Out of service but expected to return to service in next calendar year",
    "P": "Planned for installation, but regulatory approvals not initiated",
    "L": "Regulatory approvals pending. Not under construction",
    "T": "Regulatory approvals received. Not under construction",
    "U": "Under construction, less than or equal to 50 percent complete",
    "V": "Under construction, more than 50 percent complete",
    "TS": "Construction complete, but not yet in commercial operation",
    "OT": "Other",
}


@dataclass(frozen=True)
class Stage:
    stage: str
    label: str
    codes: tuple[str, ...]


#: Planned stages, most advanced first, from EIA's planned status codes (module docstring). TS is
#: its own stage: the register's lifecycle reads it as `under_construction` (`pipeline/status_map.yaml`),
#: but EIA's label says construction is complete, so counting it as under construction would say
#: something EIA does not (review, 2026-10-24: 17,155.2 MW of September 2026's 96,236.0 MW).
STAGES: tuple[Stage, ...] = (
    Stage("construction_complete", "Construction complete, not yet in commercial operation", ("TS",)),
    Stage("under_construction", "Under construction", ("U", "V")),
    Stage("approvals_received", "Regulatory approvals received, not under construction", ("T",)),
    Stage("approvals_pending", "Regulatory approvals pending, not under construction", ("L",)),
    Stage("early_or_other", "Planned, regulatory approvals not initiated, or other", ("P", "OT")),
)
UNMAPPED = Stage("unmapped", "A status outside EIA's planned codes, or none stated", ())
_STAGE_OF = {code: stage for stage in STAGES for code in stage.codes}

BY_STAGE_NOTE = (
    "EIA-860M's monthly document tallies planned capacity by status for the 50 states and DC together, and "
    "by state and by technology each on its own, so a state's or a technology's planned MW has no stage "
    "split here; the request without a filter carries the national split."
)
#: Why `power_plants` is null: one note per reason (`_eia_side`).
POWER_PLANTS_NOT_SERVED = (
    "EIA-860M's monthly archive is not served at this tier (GET /v1/trends lists the series it serves)."
)
POWER_PLANTS_NO_MONTH = "No month of EIA-860M's monthly archive has been read on this store."
POWER_PLANTS_INCOMPLETE = (
    "EIA-860M's newest served month ({period}) lacks a sheet or a tally these figures read (operating and "
    "planned, each by technology, state and status), so no figure is served from it."
)
DATA_CENTRES_WITH_TECHNOLOGY = (
    "A technology filter selects power plants; data centres and other large loads are demand, not a "
    "power-plant technology, so they are left out."
)

#: Berkeley Lab, Queued Up 2026: the one fixed line on how much queued capacity is built (module
#: docstring). Read 2026-10-24 at Berkeley Lab's publications host (emp.lbl.gov answers non-browser
#: clients with a challenge, docs/13 §2.1); the newest edition replaces the 2025 one the PLAN row named.
COMPLETION_RATE: dict[str, Any] = {
    "text": (
        "Of the capacity that requested interconnection in 2000-2020, 13% had reached commercial operation "
        "by the end of 2025; 75% had been withdrawn and 10% was still active."
    ),
    "reached_operation_pct": 13,
    "withdrawn_pct": 75,
    "still_active_pct": 10,
    "cohort": "2000-2020",
    "as_of": "2025-12",
    "applies_to": (
        "Interconnection requests in US grid operators' queues, as Berkeley Lab compiled them, not "
        "EIA-860M's planned generators, which EIA's survey lists by stage: context for how much proposed "
        "capacity has historically been built, not a forecast of these figures."
    ),
    "source": {
        "name": (
            "Queued Up: 2026 Edition, Characteristics of Power Plants Seeking Transmission Interconnection "
            "As of the End of 2025"
        ),
        "publisher": "Lawrence Berkeley National Laboratory",
        "url": "https://emp.lbl.gov/publications/queued-2026-edition-characteristics",
        "read_at": "https://eta-publications.lbl.gov/publications/queued-2026-edition-characteristics",
        "as_of": "2025-12",
        "licence": {
            "basis": "citation",
            "note": (
                "A figure quoted with credit from Berkeley Lab's report; the page states no licence for the "
                "report itself (its Queued Up data file is CC BY 4.0, docs/13 §2.1)."
            ),
        },
        "credit": "Berkeley Lab, Queued Up: 2026 Edition",
    },
}

#: Hours in each reference year: average GW is the year's TWh over them (2028 is a leap year).
HOURS = {2023: 8760, 2028: 8784}


def _average_gw(twh: float, year: int) -> float:
    return round(twh * 1000 / HOURS[year], 1)


_DOE_SOURCE: dict[str, Any] = {
    "name": "DOE Releases New Report Evaluating Increase in Electricity Demand from Data Centers",
    "publisher": "U.S. Department of Energy",
    "report": "2024 Report on U.S. Data Center Energy Use, prepared by Lawrence Berkeley National Laboratory",
    "url": "https://www.energy.gov/articles/doe-releases-new-report-evaluating-increase-electricity-demand-data-centers",
    "as_of": "2024-12",
    "licence": {
        "basis": "citation",
        "note": (
            "Figures quoted with credit from the Department of Energy's release of 20 December 2024, a US "
            "federal work (17 U.S.C. §105)."
        ),
    },
    "credit": "U.S. Department of Energy and Berkeley Lab, 2024 Report on U.S. Data Center Energy Use",
}

#: Today's average US data-centre demand and its 2028 range, each cited (module docstring).
DATA_CENTRE_DEMAND: dict[str, Any] = {
    "conversion": "average GW = TWh x 1,000 / hours in the year (8,760 in 2023, 8,784 in 2028)",
    "geography": "US",
    "measured_2023": {
        "year": 2023,
        "twh": 176,
        "share_of_us_electricity_pct": 4.4,
        "average_gw": _average_gw(176, 2023),
        "source": _DOE_SOURCE,
    },
    "projected_2028": {
        "year": 2028,
        "twh_low": 325,
        "twh_high": 580,
        "share_of_us_electricity_pct_low": 6.7,
        "share_of_us_electricity_pct_high": 12,
        "average_gw_low": _average_gw(325, 2028),
        "average_gw_high": _average_gw(580, 2028),
        "source": _DOE_SOURCE,
    },
    "comparison_note": (
        "Planned MW is the load each record states (often a requested or permitted figure, not a measured "
        "draw); average demand is a year's energy over its hours. Both are power, but a site adds its stated "
        "MW to average demand only if it draws it around the clock."
    ),
}
DATA_CENTRE_RULE = (
    "Records of kind load (data centres and other large loads, as the site's own figure counts them) in the "
    "50 states and DC; planned is the active lifecycle states, announced to under construction, of records a "
    "register still lists; demand MW summed where a record states it, null when none does."
)
#: Under a state filter (module docstring): the references have no state split.
AVERAGE_DEMAND_NATIONAL_ONLY = (
    "The Department of Energy's and Berkeley Lab's data-centre demand figures are for the US as a whole, "
    "with no split by state, so a state's planned MW is not set against them; the request without a state "
    "filter carries them."
)
#: The data-centre side's geography without a state filter: the EIA side's (`US_STATES_DC`).
NATIONAL_JURISDICTIONS: tuple[str, ...] = tuple(f"US-{code}" for code in sorted(US_STATES_DC))
GEOGRAPHY = "the 50 states and the District of Columbia"
SITES_COVERAGE = (
    "No complete public inventory of US data centres exists. These are the sites the registers Inframation "
    "reads list (the sources below), every lifecycle state: a count of this register's records, not a census."
)

_STATE_CODE = re.compile(r"^US-[A-Z]{2}$")


# ----------------------------------------------------------------------------------- parameters
def _states(raw: str | None) -> list[str] | None:
    values = [v.upper() for v in csv_param(raw)] if raw else []
    bad = [v for v in values if not _STATE_CODE.fullmatch(v) or v[3:] not in US_STATES_DC]
    if bad:
        raise validation_error(
            "state",
            f"state takes US-XX codes of the 50 states and DC, which EIA-860M covers (got {', '.join(bad)})",
            PATH,
        )
    return sorted(set(values)) or None


def _technologies(raw: str | None) -> list[str] | None:
    """The caller's technology tokens, each one of the register's (`GET /v1/meta/vocabularies`). That
    is only the first check: `_check_technologies` refuses a token the served month does not tally."""
    values = csv_param(raw) if raw else []
    bad = [v for v in values if v not in TECHNOLOGY_LABELS]
    if bad:
        raise validation_error(
            "technology",
            f"technology takes the register's tokens (GET /v1/meta/vocabularies; got {', '.join(bad)})",
            PATH,
        )
    return sorted(set(values)) or None


def _check_technologies(
    technologies: Sequence[str], operating: Mapping[str, Any], planned: Mapping[str, Any], period: str
) -> None:
    """Refuse a token the month's technology tally holds on neither side. The register's vocabulary
    also holds the opportunity and asset tokens (`solar_pv`, `bess`, `gas`), `load` and
    `transmission`, none of which EIA-860M's classifier produces, so the complete tally would read
    them as a false zero (review, 2026-10-24: `technology=gas` answered 0 MW against 295,138.4 MW of
    operating `gas_cc`). A token held on one side only is a true zero on the other."""
    held = set(operating["dims"]["technology"]) | set(planned["dims"]["technology"])
    bad = [t for t in technologies if t not in held]
    if bad:
        raise validation_error(
            "technology",
            f"technology takes the tokens EIA-860M's {period} document tallies, operating or planned "
            f"({', '.join(sorted(held))}); got {', '.join(bad)}, which no generator of that month is "
            "classified as, so its figure would be a false zero",
            PATH,
        )


# ---------------------------------------------------------------------------------- power plants
def _mw(value: float) -> float:
    return round(value, 3)


def _pct(part: float | None, whole: float | None) -> float | None:
    return round(part / whole * 100, 1) if part is not None and whole else None


def _series_ref(metric: str, by: str | None, period: str) -> dict[str, Any]:
    query = {"metric": metric, **({"by": by} if by else {}), "from": period, "to": period}
    return {"path": TRENDS_PATH, "query": query}


def _series(side: str, by: str | None, period: str) -> dict[str, Any]:
    return {unit: _series_ref(metric, by, period) for unit, metric in SERIES[side].items()}


def _figure(block: Mapping[str, Any], dim: str | None, keys: Sequence[str]) -> dict[str, Any]:
    """Generators and net summer MW over `keys` of `block`'s tally by `dim`, or its total when `dim`
    is None: the sum of the series lines `keys` at the month. A key the complete tally does not hold
    has none of either."""
    if dim is None:
        count, mw, _alt = block["total"]
        return {"generators": int(count or 0), "mw": _mw(mw or 0.0), "lines": ["total"]}
    tallied = block["dims"][dim]
    generators, mw = 0, 0.0
    for key in keys:
        count, value, _alt = tallied.get(key, (0, 0.0, None))
        generators += int(count or 0)
        mw += round(value or 0.0, 3)
    return {"generators": generators, "mw": _mw(mw), "lines": list(keys)}


def _stages(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The planned total by stage, most advanced first; `unmapped` only when a code needs it."""
    codes = sorted(block["dims"]["status"])
    total = block["total"][1] or 0.0
    out = []
    for stage in (*STAGES, UNMAPPED):
        present = [c for c in codes if _STAGE_OF.get(c, UNMAPPED) is stage]
        if stage is UNMAPPED and not present:
            continue
        figure = _figure(block, "status", present)
        out.append(
            {
                "stage": stage.stage,
                "label": stage.label,
                "codes": list(stage.codes) if stage is not UNMAPPED else present,
                **figure,
                "share_of_planned_pct": _pct(figure["mw"], total),
            }
        )
    return out


def stage_mapping() -> list[dict[str, Any]]:
    return [
        {
            "stage": stage.stage,
            "label": stage.label,
            "codes": [{"code": c, "label": STATUS_LABELS[c]} for c in stage.codes],
        }
        for stage in STAGES
    ]


def _status_rows(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    tallied = block["dims"]["status"]
    order = {code: i for i, code in enumerate(STATUS_LABELS)}
    return [
        {"status": code, "label": STATUS_LABELS.get(code), **_figure(block, "status", [code])}
        for code in sorted(tallied, key=lambda c: (order.get(c, len(order)), c))
    ]


def _rows(
    operating: Mapping[str, Any], planned: Mapping[str, Any], dim: str, keys: Iterable[str] | None
) -> list[dict[str, Any]]:
    """One row per key of `dim`, both sides: the caller's keys, else every key either side tallies;
    most planned MW first."""
    wanted = (
        list(keys) if keys is not None else sorted(set(operating["dims"][dim]) | set(planned["dims"][dim]))
    )
    rows = []
    for key in wanted:
        op, pl = _figure(operating, dim, [key]), _figure(planned, dim, [key])
        row: dict[str, Any] = {dim: key}
        if dim == "technology":
            row["label"] = TECHNOLOGY_LABELS.get(key, key)
        row.update(operating=op, planned=pl, planned_share_of_operating_pct=_pct(pl["mw"], op["mw"]))
        rows.append(row)
    return sorted(rows, key=lambda r: (-r["planned"]["mw"], -r["operating"]["mw"], r[dim]))


def _breakdown(
    operating: Mapping[str, Any],
    planned: Mapping[str, Any],
    dim: str,
    keys: Iterable[str] | None,
    period: str,
) -> dict[str, Any]:
    return {
        "series": {
            "operating": _series("operating", dim, period),
            "planned": _series("planned", dim, period),
        },
        "rows": _rows(operating, planned, dim, keys),
    }


def power_plants(
    month: trends.LatestMonth, source: Source, states: list[str] | None, technologies: list[str] | None
) -> dict[str, Any] | None:
    """The EIA-860M side for the newest served month (module docstring), or None when the month
    lacks a block or a tally the figures read. A technology the month does not tally is a 400
    (`_check_technologies`)."""
    point = month.point
    period = point.period
    operating, planned = point.blocks.get("operating"), point.blocks.get("planned")
    needed = ("technology", "state", "status")
    if (
        operating is None
        or planned is None
        or any(d not in b["dims"] for b in (operating, planned) for d in needed)
    ):
        return None
    if technologies:
        _check_technologies(technologies, operating, planned, period)
    dim, keys = (
        ("state", states) if states else ("technology", technologies) if technologies else (None, None)
    )
    filtered = dim is not None

    def side(name: str, block: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **_figure(block, dim, keys or []),
            "basis": block["basis"],
            "series": _series(name, dim, period),
            "dq_warnings": list(block.get("warnings") or ()),
        }

    op, pl = side("operating", operating), side("planned", planned)
    op["by_status"] = (
        None
        if filtered
        else {"series": _series("operating", "status", period), "rows": _status_rows(operating)}
    )
    pl["by_stage"] = (
        None if filtered else {"series": _series("planned", "status", period), "rows": _stages(planned)}
    )
    pl["by_stage_note"] = BY_STAGE_NOTE if filtered else None
    licence = source.licence
    return {
        "as_of": period,
        "unit": "MW",
        "measure": "net summer capacity",
        "geography": GEOGRAPHY,
        "source": {
            "source_id": source.id,
            "name": source.name,
            "operator": source.operator,
            "url": point.source_url,
            "retrieved_at": point.retrieved_at,
            "licence": {
                "licence_id": licence.id,
                "name": licence.name,
                "url": licence.url,
                "reuse_class": licence.reuse_class,
            },
            "credit": trends.dated_credit(EIA, source_credit(source), period),
        },
        "parser_version": point.parser_version,
        "operating": op,
        "planned": pl,
        "planned_share_of_operating_pct": _pct(pl["mw"], op["mw"]),
        "stage_mapping": stage_mapping(),
        "by_technology": None
        if states
        else _breakdown(operating, planned, "technology", technologies, period),
        "by_state": None if technologies else _breakdown(operating, planned, "state", states, period),
        "newer_months_withheld": [dict(w) for w in month.newer_withheld],
        "held_reading": dict(month.held_reading) if month.held_reading else None,
    }


# ---------------------------------------------------------------------------------- data centres
def data_centres(db: Session, entitlement: str, states: list[str] | None) -> dict[str, Any]:
    """The data-centre side (module docstring); `sources` is the licence summary of every record it
    read, as the capacity endpoint credits them. Without a state filter the lists name the 50 states
    and DC, the EIA side's geography, so Puerto Rico, the territories and records placed only to
    the US are not counted (review, 2026-10-24: `country=US` held them)."""
    query = {"kind": "load", "jurisdiction": ",".join(states or NATIONAL_JURISDICTIONS)}
    rows, ids, differs = served_rows(db, query, entitlement, PATH)
    sites = capacity_summary(rows, CapacitySelection(query=query, states=None, listed=None))
    active = frozenset(ACTIVE_LIFECYCLE_STATES)
    planned_query = {**query, "lifecycle_state": ",".join(ACTIVE_LIFECYCLE_STATES)}
    planned = capacity_summary(
        [r for r in rows if r.lifecycle_state in active],
        CapacitySelection(query=planned_query, states=ACTIVE_LIFECYCLE_STATES, listed=None),
    )
    summary = _source_licence_aggregate(db, ProposalSource, ProposalSource.proposal_id, ids, entitlement)
    newest = max((s["retrieved_at_max"] for s in summary["sources"] if s["retrieved_at_max"]), default=None)
    return {
        "geography": states or GEOGRAPHY,
        "rule": DATA_CENTRE_RULE,
        "planned": planned["total"],
        "planned_not_listed": planned["not_listed"],
        "sites_we_track": {
            "coverage": SITES_COVERAGE,
            "total": sites["total"],
            "by_lifecycle_state": sites["by_lifecycle_state"],
            "not_listed": sites["not_listed"],
        },
        "served_not_stored": differs.as_dict(),
        "as_of": newest[:7] if newest else None,
        "retrieved_at_max": newest,
        "sources": summary["sources"],
        # National references only: never set against one state's planned MW (module docstring).
        "average_demand": None if states else DATA_CENTRE_DEMAND,
        "average_demand_note": AVERAGE_DEMAND_NATIONAL_ONLY if states else None,
    }


def _eia_side(
    db: Session, entitlement: str, states: list[str] | None, technologies: list[str] | None
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    """The power plants, or None with the note that says why, and the licence summary row of the
    month they read."""
    source = db.get(Source, EIA)
    if source is None:
        return None, POWER_PLANTS_NO_MONTH, []
    if not source_visible(source, entitlement):
        return None, POWER_PLANTS_NOT_SERVED, []
    root = trends.data_root()
    month = trends.latest_month(root, EIA)
    if month is None:
        return None, POWER_PLANTS_NO_MONTH, []
    plants = power_plants(month, source, states, technologies)
    if plants is None:
        return None, POWER_PLANTS_INCOMPLETE.format(period=month.point.period), []
    rows = [
        licence_summary_row(source, source.licence, retrieved)
        for _period, retrieved in trends.monthly_retrievals(root, EIA, [plants["as_of"]])
    ]
    return plants, None, build_licence_summary(rows)["sources"]


# ----------------------------------------------------------------------------------------- route
@router.get(PATH)
def get_planned_vs_operating(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, set(PARAMS))
    qp = request.query_params
    states, technologies = _states(qp.get("state")), _technologies(qp.get("technology"))
    if states and technologies:
        raise validation_error(
            "technology",
            "state and technology together: EIA-860M's monthly document tallies capacity by state and by "
            "technology, not by both at once",
            PATH,
        )
    plants, plants_note, sources = _eia_side(db, ctx.entitlement, states, technologies)
    centres = None if technologies else data_centres(db, ctx.entitlement, states)
    if centres is not None:
        sources += centres["sources"]
    data = {
        "geography": {"country": "US", "states": states, "technologies": technologies},
        "power_plants": plants,
        "power_plants_note": plants_note,
        "completion_rate": COMPLETION_RATE,
        "data_centres": centres,
        "data_centres_note": DATA_CENTRES_WITH_TECHNOLOGY if technologies else None,
    }
    return build_envelope(
        data, meta=build_meta(tier=ctx.entitlement), licence_summary=_finalize_licence_summary(sources)
    )
