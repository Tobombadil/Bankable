"""Ember's electricity data: what one run asks for, how each answer is parsed, and the normalised rows.

    plan(today, key_present)  -> list[EmberRequest]   the API requests a run makes (none without a key)
    parse_api(raw)            -> list[Observation]     one API answer, never the network
    parse_csv(raw)            -> list[Observation]     a person's download of Ember's CSV
    classify_file(obs, ...)   -> FileDataset           which dataset a download is, or why it is held
    normalize(obs, raw, ...)  -> DataFrame             `VALUE_COLUMNS`, provenance on every row

**What a row is.** One value: an area (World, a region, a country), a period (a year, or a month), an
electricity source (solar, wind, hydro, nuclear, gas, coal, other renewables, bioenergy, other fossil,
or one of Ember's aggregates), a metric (`capacity_gw`, `generation_twh`, `share_of_generation_pct`)
and the value. Ember's global data has no storage series (its methodology names nine generation
types, https://files.ember-energy.org/public-downloads/ember_electricity_data_methodology.pdf), so
none is invented. Each row carries `source_id`, `source_url`, `retrieved_at`, `licence_id` and `raw`
(the source row it came from), as CLAUDE.md requires.

**Operating, not pipeline.** `capacity_gw` is installed (operating) capacity and `generation_twh` is
electricity generated; the API that serves these rows names them `operating_capacity_gw` and
`generation_twh` so that no page sets them beside a pipeline figure as one measure (docs/26 §4).

**The API** (OpenAPI 1.2.3, https://api.ember-energy.org/v1/openapi.json, read 2026-10-23): each
dataset endpoint answers `{"stats": {...}, "data": [...]}`, a generation item carrying `entity`,
`entity_code`, `is_aggregate_entity`, `date`, `series`, `is_aggregate_series`, `generation_twh` and
`share_of_generation_pct`, a capacity item `capacity_gw` and `capacity_w_per_capita` instead. The key
travels as the `api_key` query parameter, so it is removed from every URL, body and error text this
module records (`redact`); `stats.query_parameters_used` may echo it.

**The CSV** (a person's download, never fetched: the file host's robots.txt is `Disallow: /`). Ember's
page describes the July 2026 layout: "One row per area, month and electricity source", an "Electricity
source" column, "Is aggregated source", and the metrics as columns ("Generation (TWh)", "Share of
generation (%)", "Capacity (GW)", ...). The earlier long layout (one row per area, year and variable,
with `Category`, `Variable`, `Unit` and `Value`) is read too. Columns are found by their normalised
header, so a re-ordered file still parses; a file missing a required column fails with the names it
lacks, and a failed run keeps the figures already loaded. The country code column is required in both
layouts: it is what makes a country a country (and keys it `US`, `DE`), so a file without one would
load every country as a region.

**Which dataset a download is.** Ember's yearly and monthly pages describe their July 2026 files in the
same words ("One row per area, month and electricity source"), and a yearly file may date its periods
`2025-01-01`. So a file's dataset is taken from what the person said (`"dataset"` in its sidecar), else
its name (`release_generation_yearly_*`, `*monthly*`), else its structure: one period per area, year and
source, all in January, is the yearly data with full dates. A file whose name and structure disagree,
or that cannot be told apart, is held with the reason (`classify_file`).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlencode

import pandas as pd

from pipeline.connectors.base import ParseError, RawSnapshot, raw_json
from pipeline.connectors.global_ember_electricity import SOURCE_ID
from pipeline.connectors.global_ember_electricity.areas import Area, area_for

API_BASE = "https://api.ember-energy.org"
API_DOCS_URL = "https://api.ember-energy.org/v1/docs"
YEARLY_PAGE = "https://ember-energy.org/data/yearly-electricity-data/"
MONTHLY_PAGE = "https://ember-energy.org/data/monthly-electricity-data/"
CAPACITY_PAGE = "https://ember-energy.org/data/monthly-wind-and-solar-capacity-data/"
#: Ember's citation form, per dataset ("Ember is cited as the data source. E.g. 'Monthly electricity
#: generation data, Ember'", the API's License and Attribution section, read 2026-10-23).
DATASET_CITATIONS: dict[str, str] = {
    "yearly": "Yearly electricity data, Ember",
    "monthly": "Monthly electricity data, Ember",
    "monthly_capacity": "Monthly wind and solar capacity data, Ember",
}
DATASET_PAGES: dict[str, str] = {
    "yearly": YEARLY_PAGE,
    "monthly": MONTHLY_PAGE,
    "monthly_capacity": CAPACITY_PAGE,
}
DATASETS = ("yearly", "monthly", "monthly_capacity")
ORIGINS = ("api", "file")

#: Bump on a deliberate change of what parsing or normalising produces.
PARSER_VERSION = "1.0.0"

# --------------------------------------------------------------------------------- vocabulary
#: Ember's nine generation types, in the order every response lists them.
FUEL_SOURCES: tuple[str, ...] = (
    "solar",
    "wind",
    "hydro",
    "nuclear",
    "gas",
    "coal",
    "other_renewables",
    "bioenergy",
    "other_fossil",
)
#: Inframation's clean share (docs/26 §5): solar, wind, hydro, nuclear, bioenergy and other renewables
#: (geothermal, tidal and wave, per Ember's definitions). The same set Ember's own `Clean` aggregate
#: sums; the share is computed from the parts, so it is defined here and nowhere else.
CLEAN_SOURCES: frozenset[str] = frozenset(
    {"solar", "wind", "hydro", "nuclear", "bioenergy", "other_renewables"}
)
AGGREGATE_SOURCES: tuple[str, ...] = (
    "total_generation",
    "clean",
    "fossil",
    "renewables",
    "wind_and_solar",
    "hydro_bioenergy_other_renewables",
)
SOURCE_LABELS: dict[str, str] = {
    "solar": "Solar",
    "wind": "Wind",
    "hydro": "Hydro",
    "nuclear": "Nuclear",
    "gas": "Gas",
    "coal": "Coal",
    "other_renewables": "Other renewables",
    "bioenergy": "Bioenergy",
    "other_fossil": "Other fossil",
    "total_generation": "Total generation",
    "clean": "Clean",
    "fossil": "Fossil",
    "renewables": "Renewables",
    "wind_and_solar": "Wind and solar",
    "hydro_bioenergy_other_renewables": "Hydro, bioenergy and other renewables",
}
#: Ember's series names (any case; `&` read as `and`) -> (source, is an aggregate).
SERIES: dict[str, tuple[str, bool]] = {
    "solar": ("solar", False),
    "wind": ("wind", False),
    "hydro": ("hydro", False),
    "nuclear": ("nuclear", False),
    "gas": ("gas", False),
    "coal": ("coal", False),
    "other renewables": ("other_renewables", False),
    "bioenergy": ("bioenergy", False),
    "other fossil": ("other_fossil", False),
    "total generation": ("total_generation", True),
    "total": ("total_generation", True),
    "clean": ("clean", True),
    "fossil": ("fossil", True),
    "renewables": ("renewables", True),
    "wind and solar": ("wind_and_solar", True),
    "hydro, bioenergy and other renewables": ("hydro_bioenergy_other_renewables", True),
}
#: Rows the CSV carries that are not electricity sources ("The 'Electricity source' column covers
#: fuels, aggregates, demand and net imports"): skipped and counted, never vocabulary drift.
NON_SOURCE_PREFIXES = ("demand", "net import", "total emissions", "co2 intensity", "emissions intensity")

METRICS: dict[str, str] = {
    "capacity_gw": "GW",
    "generation_twh": "TWh",
    "share_of_generation_pct": "%",
}

VALUE_COLUMNS: list[str] = [
    "record_id",
    "source_id",
    "source_record_id",
    "source_url",
    "retrieved_at",
    "licence_id",
    "dataset",
    "origin",
    "area_id",
    "area_name",
    "area_kind",
    "iso3",
    "period",
    "year",
    "month",
    "source",
    "source_label",
    "is_aggregate",
    "metric",
    "value",
    "unit",
    "raw",
]


def series_key(label: str) -> str:
    return " ".join(str(label).replace("&", " and ").lower().split())


def source_for(label: str) -> tuple[str, bool] | None:
    """Ember's series name -> (source, aggregate), or None for a name this table does not know."""
    return SERIES.get(series_key(label))


def is_non_source(label: str) -> bool:
    return series_key(label).startswith(NON_SOURCE_PREFIXES)


# --------------------------------------------------------------------------------- requests
@dataclass(frozen=True)
class EmberRequest:
    """One API request: `key` names it in the snapshot store and the run record."""

    key: str
    dataset: str
    path: str
    params: Mapping[str, str]
    metrics: tuple[str, ...]

    @property
    def url(self) -> str:
        """The request URL as recorded: never with the key."""
        return (
            f"{API_BASE}{self.path}?{urlencode(dict(self.params))}"
            if self.params
            else f"{API_BASE}{self.path}"
        )


#: The yearly series starts in 2000, the earliest year Ember offers ("We provide data for 215 countries
#: from 2000", methodology p.10). The monthly generation series is kept to a rolling 36 months ending
#: with the current one: it serves the latest month (Ember reports a few months behind) and its change
#: on a year earlier, and the whole monthly history for 88 areas would be about half a million rows a
#: run. Rolling, not calendar years: a window that starts each January would drop a whole year on
#: 1 January, and the next run's row count would fall by a third and hold (`dq.row_count`).
YEARLY_START = "2000"
MONTHLY_MONTHS = 36
CAPACITY_START = "2016-01"


def monthly_start(today: dt.date) -> str:
    """The first month of the rolling monthly window: 35 months before `today`'s month."""
    index = today.year * 12 + (today.month - 1) - (MONTHLY_MONTHS - 1)
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def plan(today: dt.date, *, key_present: bool) -> list[EmberRequest]:
    """The three API requests of a run (yearly generation, monthly generation, monthly wind and solar
    capacity), every area in one answer each; none without a key (the run is then `blocked`)."""
    if not key_present:
        return []
    return [
        EmberRequest(
            "yearly_generation",
            "yearly",
            "/v1/electricity-generation/yearly",
            {"start_date": YEARLY_START},
            ("generation_twh", "share_of_generation_pct"),
        ),
        EmberRequest(
            "monthly_generation",
            "monthly",
            "/v1/electricity-generation/monthly",
            {"start_date": monthly_start(today)},
            ("generation_twh", "share_of_generation_pct"),
        ),
        EmberRequest(
            "monthly_capacity",
            "monthly_capacity",
            "/v1/installed-capacity/monthly",
            {"start_date": CAPACITY_START},
            ("capacity_gw",),
        ),
    ]


def redact(text: str, secrets: Iterable[str]) -> str:
    """`text` with every secret (the API key) replaced; secrets shorter than 6 characters are left,
    since replacing them would mangle ordinary text and no real key is that short."""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "[redacted]")
    return out


def redact_bytes(content: bytes, secrets: Iterable[str]) -> bytes:
    out = content
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret.encode("utf-8"), b"[redacted]")
    return out


def data_digest(content: bytes) -> str:
    """A digest of an API answer's `data` alone: `stats` carries the response time and a timestamp,
    which differ on every call, so the whole body's SHA-256 would never repeat."""
    import hashlib

    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return hashlib.sha256(content).hexdigest()
    data = payload.get("data") if isinstance(payload, dict) else payload
    body = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


# ------------------------------------------------------------------------------- observations
@dataclass
class Observation:
    """One source row, read: the area, the period, Ember's series name and the metric values."""

    area: Area
    year: int
    month: int | None
    series: str
    is_aggregate: bool | None
    values: dict[str, float]
    raw: dict[str, Any]


@dataclass
class ParseReport:
    """What a parse skipped: series this vocabulary does not know (vocabulary drift), rows that are not
    electricity sources (demand, net imports), and rows without a usable area or period. Also the
    areas Ember flags as countries that carry no usable code (`uncoded_countries`, name -> rows): they
    are kept as regions, so they drop out of coverage and the rankings, and the run warns."""

    unmapped: dict[str, int] = field(default_factory=dict)
    non_source_rows: int = 0
    bad_rows: int = 0
    layout: str = ""
    columns: list[str] = field(default_factory=list)
    uncoded_countries: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unmapped": dict(sorted(self.unmapped.items())),
            "non_source_rows": self.non_source_rows,
            "bad_rows": self.bad_rows,
            "layout": self.layout,
            "columns": self.columns,
            "uncoded_countries": dict(sorted(self.uncoded_countries.items())),
        }

    def note_area(self, area: Area, flagged_country: bool) -> None:
        """Count a row Ember flags as a country that `area_for` could not key as one."""
        if flagged_country and area.kind != "country" and area.id != "world":
            self.uncoded_countries[area.name] = self.uncoded_countries.get(area.name, 0) + 1


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if not text or text.lower() in ("nan", "null", "none", "n/a", "-"):
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in ("true", "1", "yes", "y"):
        return True
    if text in ("false", "0", "no", "n"):
        return False
    return None


_PERIOD = re.compile(r"^\s*(\d{4})(?:[-/](\d{1,2}))?")


def period_of(value: Any) -> tuple[int, int | None] | None:
    """`2025` -> (2025, None); `2025-08`, `2025-08-01` -> (2025, 8)."""
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(float(value)):
        return int(value), None
    m = _PERIOD.match(str(value or ""))
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else None
    if month is not None and not 1 <= month <= 12:
        return None
    return year, month


def parse_api(raw: RawSnapshot, request: EmberRequest) -> tuple[list[Observation], ParseReport]:
    """One API answer as observations. Raises `ParseError` on a body that is not the documented
    `{"stats": ..., "data": [...]}` shape."""
    try:
        payload = json.loads(raw.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f"{request.key}: not JSON: {exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ParseError(f"{request.key}: no `data` list in the answer")
    stated = (payload.get("stats") or {}).get("number_of_records") if isinstance(payload, dict) else None
    if isinstance(stated, int) and not isinstance(stated, bool) and stated != len(data):
        # A cut-off answer would load as a shorter series: fail, and keep the last good figures.
        raise ParseError(f"{request.key}: the answer states {stated} records and holds {len(data)}")
    report = ParseReport(
        layout="api", columns=sorted({k for item in data[:50] if isinstance(item, dict) for k in item})
    )
    out: list[Observation] = []
    for item in data:
        if not isinstance(item, dict):
            report.bad_rows += 1
            continue
        period = period_of(item.get("date"))
        name = str(item.get("entity") or "").strip()
        if period is None or not name:
            report.bad_rows += 1
            continue
        values = {m: v for m in request.metrics if (v := _number(item.get(m))) is not None}
        aggregate_entity = _flag(item.get("is_aggregate_entity"))
        area = area_for(name, item.get("entity_code"), is_aggregate=aggregate_entity)
        report.note_area(area, aggregate_entity is False)
        out.append(
            Observation(
                area=area,
                year=period[0],
                month=period[1],
                series=str(item.get("series") or ""),
                is_aggregate=_flag(item.get("is_aggregate_series")),
                values=values,
                raw=item,
            )
        )
    return out, report


# ------------------------------------------------------------------------------------- CSV
def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(header).lower())


#: Normalised header -> role, for both layouts. The first header of a role found wins.
_ROLES: dict[str, tuple[str, ...]] = {
    "area": ("area", "countryorregion", "entity", "country"),
    "code": (
        "iso3code",
        "iso3",
        "countrycode",
        "entitycode",
        "areacode",
        "isocode",
        "iso3166alpha3",
        "iso3166alpha3code",
        "code",
    ),
    "area_type": ("areatype",),
    "is_aggregate_area": ("isaggregatearea", "isaggregateentity", "isaggregatedarea"),
    "date": ("year", "date", "month"),
    "series": ("electricitysource", "variable", "series", "source"),
    "is_aggregate_series": ("isaggregatedsource", "isaggregatesource", "isaggregateseries"),
    "category": ("category",),
    "subcategory": ("subcategory",),
    "unit": ("unit",),
    "value": ("value",),
    "generation_twh": ("generationtwh",),
    "share_of_generation_pct": ("shareofgeneration", "shareofgenerationpct"),
    "capacity_gw": ("capacitygw",),
}
#: `code` is required in both layouts: without a country code column every country would load as a
#: region (`areas.area_for`), so `area=US` would not exist and coverage and the ranking would be empty.
WIDE_REQUIRED = ("area", "code", "date", "series", "generation_twh")
LONG_REQUIRED = ("area", "code", "date", "series", "category", "unit", "value")
#: How a missing role is named in the error, where its role name alone would not say which column.
_ROLE_HINTS: dict[str, str] = {
    "code": "code (a country code column such as 'ISO 3 code' or 'Country code')",
    "date": "date (a 'Year' or 'Date' column)",
    "series": "series (an 'Electricity source' or 'Variable' column)",
}


def _columns(headers: Sequence[str]) -> dict[str, str]:
    by_norm: dict[str, str] = {}
    for h in headers:
        by_norm.setdefault(_norm(h), h)
    found: dict[str, str] = {}
    for role, names in _ROLES.items():
        for name in names:
            if name in by_norm:
                found[role] = by_norm[name]
                break
    return found


def _long_metric(category: str, unit: str) -> str | None:
    """The old long layout's (Category, Unit) -> metric: generation in TWh, its share in %, capacity
    in GW. Emissions, demand and imports are not read."""
    cat = category.strip().lower()
    u = unit.strip().lower()
    if cat == "electricity generation" and u == "twh":
        return "generation_twh"
    if cat == "electricity generation" and u == "%":
        return "share_of_generation_pct"
    if cat == "capacity" and u == "gw":
        return "capacity_gw"
    return None


def _csv_area(row: Mapping[str, Any], cols: Mapping[str, str], report: ParseReport) -> Area:
    aggregate: bool | None = None
    if "is_aggregate_area" in cols:
        aggregate = _flag(row.get(cols["is_aggregate_area"]))
    elif "area_type" in cols:
        kind = str(row.get(cols["area_type"]) or "").strip().lower()
        aggregate = None if not kind else not kind.startswith("country")
    code = row.get(cols["code"]) if "code" in cols else None
    area = area_for(str(row.get(cols["area"]) or ""), code, is_aggregate=aggregate)
    report.note_area(area, aggregate is False)
    return area


def parse_csv(raw: RawSnapshot) -> tuple[list[Observation], ParseReport]:
    """A person's download of Ember's yearly (or monthly) CSV as observations."""
    try:
        text = raw.content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError(f"not UTF-8 text: {exc}") from exc
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    cols = _columns(headers)
    long_layout = all(r in cols for r in ("category", "unit", "value"))
    required = LONG_REQUIRED if long_layout else WIDE_REQUIRED
    missing = [r for r in required if r not in cols]
    if missing:
        lacking, found = ", ".join(_ROLE_HINTS.get(r, r) for r in missing), ", ".join(headers)[:500]
        raise ParseError(f"the file lacks the columns this parser reads ({lacking}); headers: {found}")
    report = ParseReport(layout="long" if long_layout else "wide", columns=headers)
    out: list[Observation] = []
    for row in reader:
        series = str(row.get(cols["series"]) or "").strip()
        period = period_of(row.get(cols["date"]))
        if period is None or not str(row.get(cols["area"]) or "").strip():
            report.bad_rows += 1
            continue
        aggregate = _flag(row.get(cols["is_aggregate_series"])) if "is_aggregate_series" in cols else None
        if long_layout:
            metric = _long_metric(str(row.get(cols["category"]) or ""), str(row.get(cols["unit"]) or ""))
            if metric is None:
                continue  # emissions, demand, imports, intensity: not read
            value = _number(row.get(cols["value"]))
            values = {metric: value} if value is not None else {}
            sub = str(row.get(cols["subcategory"]) or "").strip().lower() if "subcategory" in cols else ""
            if sub:
                aggregate = sub != "fuel"
        else:
            values = {m: v for m in METRICS if m in cols and (v := _number(row.get(cols[m]))) is not None}
        out.append(
            Observation(
                area=_csv_area(row, cols, report),
                year=period[0],
                month=period[1],
                series=series,
                is_aggregate=aggregate,
                values=values,
                raw=dict(row),
            )
        )
    return out, report


FILE_DATASETS = ("yearly", "monthly")


@dataclass(frozen=True)
class FileDataset:
    """Which of Ember's datasets a person's download is. `dataset` is None when the file is held, and
    `hold` says why; `basis` is what decided (`sidecar`, `name` or `structure`); `dated_years` is a
    yearly file whose periods are full dates (`2025-01-01`), read as years (`as_yearly`)."""

    dataset: str | None
    basis: str
    hold: str | None = None
    dated_years: bool = False


def dataset_hint(name: str, declared: object = None) -> tuple[str | None, str]:
    """What the person said the file is (`"dataset"` in its sidecar), else what its name says
    (Ember's `release_generation_yearly_global.csv`, `..._monthly_...`), else nothing."""
    if isinstance(declared, str) and declared.strip().lower() in FILE_DATASETS:
        return declared.strip().lower(), "sidecar"
    base = name.rsplit("/", 1)[-1].lower()
    yearly, monthly = "yearly" in base, "monthly" in base
    if yearly != monthly:
        return ("yearly" if yearly else "monthly"), "name"
    return None, "structure"


def classify_file(observations: Sequence[Observation], *, name: str, declared: object = None) -> FileDataset:
    """The dataset of a person's download (module docstring, "Which dataset a download is"):

    * one period per (area, year, source), with no month or every month January, is yearly data;
    * more than one period a year for one area and source is monthly data;
    * held, with the reason: a file named or declared yearly that holds several periods a year; one
      named or declared monthly whose periods are bare years, or one a year and all January, or that
      carries Capacity (GW) values (only Ember's yearly file has capacity by source); and one with a
      single period a year in another month and no name or sidecar to say which it is.
    """
    hint, basis = dataset_hint(name, declared)
    months = [o.month for o in observations]
    periods: dict[tuple[str, int, str], set[int | None]] = {}
    for o in observations:
        periods.setdefault((o.area.id, o.year, series_key(o.series)), set()).add(o.month)
    one_a_year = all(len(found) == 1 for found in periods.values())
    bare_years = all(m is None for m in months)
    january = bool(months) and all(m == 1 for m in months)
    other_months = sorted({m for m in months if m is not None and m != 1})
    capacity = any("capacity_gw" in o.values for o in observations)
    most = max((len(found) for found in periods.values()), default=0)
    said = f"named {hint} ({'its sidecar' if basis == 'sidecar' else 'its file name'})"
    if hint == "yearly":
        if bare_years or one_a_year:
            return FileDataset("yearly", basis, dated_years=not bare_years)
        return FileDataset(
            None, basis, f"{said} but holds up to {most} periods a year for one area and source"
        )
    if hint == "monthly":
        if bare_years:
            return FileDataset(None, basis, f"{said} but its periods are years, not months")
        if one_a_year and january:
            return FileDataset(
                None,
                basis,
                f"{said} but every period is January, one a year: Ember's yearly data with full dates?",
            )
        if capacity:
            return FileDataset(
                None, basis, f"{said} but carries Capacity (GW) values, which only Ember's yearly file has"
            )
        return FileDataset("monthly", basis)
    if bare_years:
        return FileDataset("yearly", basis)
    if one_a_year and january:
        return FileDataset("yearly", basis, dated_years=True)
    if one_a_year:
        found = ", ".join(str(m) for m in other_months)
        return FileDataset(
            None,
            basis,
            f"one period a year (month {found}): yearly or monthly cannot be told; name the file as Ember "
            'does or set "dataset" in its sidecar',
        )
    if capacity:
        return FileDataset(
            None, basis, "monthly periods but Capacity (GW) values, which only Ember's yearly file has"
        )
    return FileDataset("monthly", basis)


def as_yearly(observations: Sequence[Observation]) -> list[Observation]:
    """A yearly file's observations with full-date periods (`2025-01-01`) read as years."""
    return [replace(o, month=None) for o in observations]


# ------------------------------------------------------------------------------- normalise
def normalize(
    observations: Sequence[Observation],
    *,
    dataset: str,
    origin: str,
    source_url: str,
    retrieved_at: str,
    licence_id: str,
    report: ParseReport,
) -> pd.DataFrame:
    """`VALUE_COLUMNS`, one row per (area, period, source, metric) value, provenance on every row.
    Series this vocabulary does not know are left out and counted on `report.unmapped`; rows that are
    not electricity sources (demand, net imports) on `report.non_source_rows`."""
    if dataset not in DATASETS or origin not in ORIGINS:
        raise ValueError(f"dataset {dataset!r} / origin {origin!r} not in {DATASETS} / {ORIGINS}")
    rows: list[dict[str, Any]] = []
    for obs in observations:
        mapped = source_for(obs.series)
        if mapped is None:
            if is_non_source(obs.series) or not obs.series:
                report.non_source_rows += 1
            else:
                report.unmapped[obs.series] = report.unmapped.get(obs.series, 0) + 1
            continue
        source, aggregate = mapped
        if dataset == "yearly" and obs.month is not None:
            continue  # a monthly row in a yearly answer is not a yearly value
        if dataset != "yearly" and obs.month is None:
            report.bad_rows += 1
            continue
        period = f"{obs.year:04d}" if obs.month is None else f"{obs.year:04d}-{obs.month:02d}"
        raw = raw_json(obs.raw)
        for metric, value in obs.values.items():
            source_record_id = f"{origin}|{dataset}|{obs.area.id}|{period}|{source}|{metric}"
            rows.append(
                {
                    "record_id": f"{SOURCE_ID}:{source_record_id}",
                    "source_id": SOURCE_ID,
                    "source_record_id": source_record_id,
                    "source_url": source_url,
                    "retrieved_at": retrieved_at,
                    "licence_id": licence_id,
                    "dataset": dataset,
                    "origin": origin,
                    "area_id": obs.area.id,
                    "area_name": obs.area.name,
                    "area_kind": obs.area.kind,
                    "iso3": obs.area.iso3,
                    "period": period,
                    "year": obs.year,
                    "month": obs.month,
                    "source": source,
                    "source_label": obs.series,
                    "is_aggregate": aggregate,
                    "metric": metric,
                    "value": float(value),
                    "unit": METRICS[metric],
                    "raw": raw,
                }
            )
    df = pd.DataFrame(rows, columns=VALUE_COLUMNS)
    if not df.empty:
        df["year"] = df["year"].astype(int)
        df["month"] = df["month"].astype("Int64")
        df["value"] = df["value"].astype(float)
        df["is_aggregate"] = df["is_aggregate"].astype(bool)
    return df
