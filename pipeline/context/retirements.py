"""Retired and retiring power plants from one EIA-860M workbook (lane R1, owner request 2026-10-06).

A powered-land reader looks for sites where a grid connection already exists: a plant that has
retired, or will, leaves a substation, an interconnection agreement and transmission behind it.
EIA-860M carries both halves of that signal in the workbook the platform already fetches:

* the **"Retired"** sheet, one row per retired generator (EIA keeps 2002 onwards, plus a short
  appendix of earlier nuclear retirements), with the retirement month and year;
* the **"Operating"** sheet's `Planned Retirement Month` / `Planned Retirement Year`, set on the
  generators an owner has told EIA it plans to retire.

Pure functions only: no network, no database. Two callers share them, so the plant-level answer
is computed one way:

* `pipeline/context/eia_plants.py` builds the `power_plant` asset frame (one row per EIA plant id,
  now including plants whose every generator has retired);
* `services/ingest/retirements.py` refreshes the same assets from the generator-level run of
  `us.eia.860m.retirements` (`pipeline/connectors/us_eia_860m_retirements/connector.py`), whose
  diff is what turns a planned-retirement change into an event.

Generator state comes from the connector's status map (`asset_status_map.yaml`, read through the
same `harmonise_status` every connector uses): `operating`, `standby`, `retiring` (in service with a
planned retirement date) or `retired` (on the Retired sheet).

Plant status rule (recorded in docs/27 §R1.2):

* `retired`  -- the plant has retired generators and none left in service;
* `retiring` -- generators with a planned retirement hold at least `RETIRING_SHARE` (half) of the
  plant's in-service nameplate MW: most of the plant is scheduled to close;
* `operating` -- otherwise. A plant retiring a minority of its capacity stays `operating`; its
  planned units are still in `attributes.retirement` and still match a `retirement_year` filter.

`retirement_year` is the year the plant finished retiring (`retired`), else the year its next unit
is scheduled to retire, else null.
"""

from __future__ import annotations

import io
import pathlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from pipeline.connectors.base import ParseError
from pipeline.connectors.canonical import harmonise_status, load_status_map

STATUS_MAP_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "connectors"
    / "us_eia_860m_retirements"
    / "asset_status_map.yaml"
)
STATUS_KEY = "eia860m_generators"
OPERATING_SHEET = "Operating"
RETIRED_SHEET = "Retired"
#: The Retired sheet has no Status column; its rows carry EIA's own Form 860 code for the state.
RETIRED_STATUS_RAW = "(RE) Retired"
#: Share of in-service nameplate MW with a planned retirement at or above which the plant is `retiring`.
RETIRING_SHARE = 0.5
#: Generator states that are still in service.
IN_SERVICE = frozenset({"operating", "standby"})
GENERATOR_STATES = ("operating", "standby", "retiring", "retired")

_AS_OF_RE = re.compile(r"as of\s+([A-Za-z]+)\s+(\d{4})", re.I)
_MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}


# ------------------------------------------------------------------------------------- parsing
def _sheet(content: bytes, name: str) -> tuple[pd.DataFrame, str | None]:
    """One inventory sheet, header on the third row (rows 1-2 are a title and a blank), with the
    title's "as of <Month> <Year>" returned as `YYYY-MM`."""
    try:
        title = pd.read_excel(io.BytesIO(content), sheet_name=name, header=None, nrows=1, engine="openpyxl")
    except ValueError as exc:  # no such sheet
        raise ParseError(f"EIA-860M workbook has no {name!r} sheet") from exc
    as_of = workbook_as_of(str(title.iat[0, 0]) if title.size else "")
    df = pd.read_excel(io.BytesIO(content), sheet_name=name, header=2, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    if "Plant ID" not in df.columns or "Generator ID" not in df.columns:
        raise ParseError(f"{name} sheet layout changed: {list(df.columns)[:8]}")
    return df, as_of


def workbook_as_of(title: str) -> str | None:
    """`"Inventory of Retired Generators as of August 2026"` -> `"2026-08"`; None when absent."""
    m = _AS_OF_RE.search(title or "")
    if not m or m.group(1).lower() not in _MONTHS:
        return None
    return f"{int(m.group(2)):04d}-{_MONTHS[m.group(1).lower()]:02d}"


def _numeric_plant_ids(df: pd.DataFrame) -> pd.Series:
    return pd.to_numeric(df["Plant ID"], errors="coerce")


def generator_key(value: Any) -> str:
    """A Generator ID as text, the same however the cell was typed: EIA's ids are free text
    (`GT1`, `WT2`, `5.1`), but a numeric cell reads back as `1.0` when a whole column happens to be
    numbers, and `1` must match `1` across the two sheets."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


@dataclass
class GeneratorSheets:
    operating: pd.DataFrame
    retired: pd.DataFrame
    as_of: str | None
    #: Rows dropped because `Plant ID` is not a number: the trailing note rows, and the appendix of
    #: pre-2002 nuclear retirements EIA lists with a blank plant id (12 rows in August 2026).
    dropped_without_plant_id: int = 0


def parse_generator_sheets(content: bytes) -> GeneratorSheets:
    """The Operating and Retired sheets, rows without a numeric Plant ID dropped and counted."""
    operating, as_of = _sheet(content, OPERATING_SHEET)
    retired, retired_as_of = _sheet(content, RETIRED_SHEET)
    dropped = 0
    out = []
    for df in (operating, retired):
        keep = _numeric_plant_ids(df).notna()
        # Note rows have neither a plant id nor a plant name; only rows that name something count.
        dropped += int((~keep & df["Plant Name"].notna()).sum()) if "Plant Name" in df.columns else 0
        kept = df[keep].reset_index(drop=True)
        kept["Generator ID"] = kept["Generator ID"].astype("object").map(generator_key)
        out.append(kept)
    return GeneratorSheets(out[0], out[1], as_of or retired_as_of, dropped)


# ------------------------------------------------------------------------------ generator state
def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _int(value: Any) -> int | None:
    if _blank(value):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def year_month(year: Any, month: Any) -> str | None:
    """`(2028, 6)` -> `"2028-06"`; `(2028, None)` -> `"2028"`; no year -> None. EIA states month
    and year as separate columns and leaves the month blank on some planned retirements (7 of 531
    in August 2026), so the year-only form keeps exactly what the source says."""
    y = _int(year)
    if y is None:
        return None
    m = _int(month)
    return f"{y:04d}-{m:02d}" if m and 1 <= m <= 12 else f"{y:04d}"


def date_of(value: str | None) -> str | None:
    """`"2028-06"` -> `"2028-06-01"`, `"2028"` -> `"2028-01-01"`: the comparable date the runner
    diff reads from `proposed_cod`. The stated precision lives in the year-month string."""
    if not value:
        return None
    return f"{value}-01" if len(value) == 7 else f"{value}-01-01"


def status_context(sheet: str, row: dict[str, Any]) -> dict[str, Any]:
    """What `harmonise_status` reads for one generator row of either sheet."""
    if sheet == RETIRED_SHEET:
        return {"status_raw": RETIRED_STATUS_RAW, "sheet": sheet, "planned_retirement": "no"}
    planned = not _blank(row.get("Planned Retirement Year"))
    status = row.get("Status")
    return {
        "status_raw": None if _blank(status) else str(status).strip(),
        "sheet": sheet,
        "planned_retirement": "yes" if planned else "no",
    }


def generator_state(sheet: str, row: dict[str, Any], status_map: dict[str, Any]) -> tuple[str, str]:
    """`(state, rule id)` for one generator row through the connector's status map."""
    state, rule = harmonise_status(STATUS_KEY, status_context(sheet, row), status_map)
    return state, rule


def load_generator_status_map() -> dict[str, Any]:
    return load_status_map(STATUS_MAP_PATH)


# --------------------------------------------------------------------------------- plant summary
@dataclass(frozen=True)
class GeneratorRecord:
    """One generator, in the shape the plant summary needs (built from either caller's frame)."""

    plant_id: str
    generator_id: str
    state: str
    capacity_mw: float | None
    technology: str | None
    #: `YYYY-MM` or `YYYY`: the retirement date (retired) or the planned one (retiring), else None.
    date: str | None


@dataclass
class PlantRetirement:
    status: str
    retirement_year: int | None
    #: `attributes["retirement"]`, or None when the plant has neither retired nor planned units.
    block: dict[str, Any] | None = field(default=None)


def _sum(values: Iterable[float | None]) -> float:
    return round(sum(v for v in values if v is not None), 3)


def _by_technology(units: list[GeneratorRecord]) -> list[dict[str, Any]]:
    techs: dict[str, dict[str, float]] = {}
    for u in units:
        if u.state not in ("retired", "retiring"):
            continue
        label = u.technology or "Unknown"
        bucket = techs.setdefault(label, {"retired_mw": 0.0, "retiring_mw": 0.0})
        bucket["retired_mw" if u.state == "retired" else "retiring_mw"] += u.capacity_mw or 0.0
    rows = [
        {
            "technology": label,
            "retired_mw": round(v["retired_mw"], 3),
            "retiring_mw": round(v["retiring_mw"], 3),
        }
        for label, v in techs.items()
    ]
    return sorted(rows, key=lambda r: -(r["retired_mw"] + r["retiring_mw"]))


def summarise_plant(units: list[GeneratorRecord], *, as_of: str | None = None) -> PlantRetirement:
    """The plant-level status, `retirement_year` and `attributes["retirement"]` block (module
    docstring for the rule)."""
    # Anything not on the Retired sheet is still there: operating, standby, retiring, or a status
    # the map does not know (`unknown`, which the DQ gate already reports).
    in_service = [u for u in units if u.state != "retired"]
    retiring = [u for u in units if u.state == "retiring"]
    retired = [u for u in units if u.state == "retired"]
    in_service_mw = _sum(u.capacity_mw for u in in_service)
    retiring_mw = _sum(u.capacity_mw for u in retiring)
    retired_mw = _sum(u.capacity_mw for u in retired)
    retired_dates = sorted(d for d in (u.date for u in retired) if d)
    planned_dates = sorted(d for d in (u.date for u in retiring) if d)
    share = (retiring_mw / in_service_mw) if in_service_mw > 0 else (1.0 if retiring else 0.0)

    if retired and not in_service:
        status, rule = "retired", "all_units_retired"
        year = int(retired_dates[-1][:4]) if retired_dates else None
    elif retiring and share >= RETIRING_SHARE:
        status, rule = "retiring", "majority_mw_retiring"
        year = int(planned_dates[0][:4]) if planned_dates else None
    else:
        status = "operating"
        rule = "some_units_retiring" if retiring else ("some_units_retired" if retired else "in_service")
        year = int(planned_dates[0][:4]) if planned_dates else None

    if not retired and not retiring:
        return PlantRetirement(status=status, retirement_year=None, block=None)
    unit_rows = [
        {
            "generator_id": u.generator_id,
            "technology": u.technology,
            "capacity_mw": u.capacity_mw,
            "state": u.state,
            "date": u.date,
        }
        for u in sorted(
            (u for u in units if u.state in ("retired", "retiring")),
            key=lambda u: (u.date or "9999", u.generator_id),
        )
    ]
    block: dict[str, Any] = {
        "status_rule": rule,
        "in_service_mw": in_service_mw,
        "retiring_mw": retiring_mw,
        "retiring_units": len(retiring),
        "retiring_share": round(share, 4) if in_service_mw > 0 else None,
        "next_planned": planned_dates[0] if planned_dates else None,
        "last_planned": planned_dates[-1] if planned_dates else None,
        "retired_mw": retired_mw,
        "retired_units": len(retired),
        "first_retired": retired_dates[0] if retired_dates else None,
        "last_retired": retired_dates[-1] if retired_dates else None,
        "by_technology": _by_technology(units),
        "units": unit_rows,
        "as_of": as_of,
    }
    return PlantRetirement(status=status, retirement_year=year, block=block)


def records_from_sheets(sheets: GeneratorSheets, status_map: dict[str, Any]) -> list[GeneratorRecord]:
    """Every generator of both sheets as a `GeneratorRecord`, state from the status map."""
    out: list[GeneratorRecord] = []
    for sheet, df in ((OPERATING_SHEET, sheets.operating), (RETIRED_SHEET, sheets.retired)):
        for row in df.to_dict("records"):
            state, _rule = generator_state(sheet, row, status_map)
            if sheet == RETIRED_SHEET:
                date = year_month(row.get("Retirement Year"), row.get("Retirement Month"))
            elif state == "retiring":
                date = year_month(row.get("Planned Retirement Year"), row.get("Planned Retirement Month"))
            else:
                date = None
            mw = pd.to_numeric(row.get("Nameplate Capacity (MW)"), errors="coerce")
            tech = row.get("Technology")
            out.append(
                GeneratorRecord(
                    plant_id=str(int(float(row["Plant ID"]))),
                    generator_id=generator_key(row.get("Generator ID")),
                    state=state,
                    capacity_mw=None if pd.isna(mw) or float(mw) <= 0 else float(mw),
                    technology=None if _blank(tech) else str(tech).strip(),
                    date=date,
                )
            )
    return out


def summarise_plants(
    records: Iterable[GeneratorRecord], *, as_of: str | None = None
) -> dict[str, PlantRetirement]:
    """`{plant_id: PlantRetirement}` over every plant the records name."""
    by_plant: dict[str, list[GeneratorRecord]] = {}
    for r in records:
        by_plant.setdefault(r.plant_id, []).append(r)
    return {pid: summarise_plant(units, as_of=as_of) for pid, units in by_plant.items()}


__all__ = [
    "GENERATOR_STATES",
    "IN_SERVICE",
    "OPERATING_SHEET",
    "RETIRED_SHEET",
    "RETIRED_STATUS_RAW",
    "RETIRING_SHARE",
    "STATUS_KEY",
    "STATUS_MAP_PATH",
    "GeneratorRecord",
    "GeneratorSheets",
    "PlantRetirement",
    "date_of",
    "generator_key",
    "generator_state",
    "load_generator_status_map",
    "parse_generator_sheets",
    "records_from_sheets",
    "status_context",
    "summarise_plant",
    "summarise_plants",
    "workbook_as_of",
    "year_month",
]
