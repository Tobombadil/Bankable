"""Named trend series: the monthly archives and the daily record, read back as lines a chart can draw
(`docs/24-trends.md` §10; `GET /v1/trends`, `services/api/trends.py`).

**One source per series.** Every series is read from one source's documents: EIA-860M's monthly
archive (`us.eia.860m.history`), ERCOT's monthly GIS reports (`us.iso.ercot.gen_queue.history`),
or the daily record of the public figures (`services/trends/snapshot.py`). Series from different
sources are never spliced into one line; a chart that wants EIA's planned MW beside ERCOT's queue
MW draws two series side by side.

**Like for like.** Every point carries the `basis` its document recorded for that figure: what was
counted and how (the sheet, the capacity measure, the geography, a coverage rule the publisher
changed, the activity rule, the platform posture, and for a daily figure the sources that
contributed to it and how many were withheld). A month-on-month (`mom`) change compares a point
with the point exactly one month before, and a year-on-year (`yoy`) change with the point exactly
twelve months before (for a daily series: the same day of the month, one month or one year before);
each is computed only when both points exist, both have a value, both have the same basis and,
for a monthly series, both months were read by the same parser version. Otherwise it is null, and
a change of basis or parser between consecutive points is listed under `breaks`. A gap is never
filled: a month or a day with no document has no point.

**Coverage is part of a daily basis.** A daily figure counts what Inframation's register holds, so
a register that goes live, or one whose terms are recorded and whose rows are published, moves it
without anything changing in the world. The basis names a digest of the contributing source ids
(`sources` of the day's document: those with a link to a counted proposal, or notice) and the
number withheld (`visibility.withheld_sources`), so such a day is a break, never a change; the
break lists the sources added and removed.

**Breakdowns.** A breakdown (`by`) gives one line per key. A document's tally of a dimension is
complete, so a key absent from a month that tallied the dimension is a real zero there; a document
that did not tally the dimension at all (a schema-1 daily document has no `by_state` on the
withdrawn block, an EIA month read before parser 1.1.0 has no `by_state_status`) has no point for
any key, never a zero or a null. A line by a year a stock is
waiting for (EIA's expected year and planned retirement year, ERCOT's projected COD year) empties
as that year's plants come online or retire, so no change is computed on it from the start of its
year: its fall then is completion, not a shrinking pipeline. EIA's operating and planned series also
break down by a state or a technology and the status together (`state_status`, `technology_status`:
one line per pair, keyed `US-TX|U`, `solar|TS`), so a state's or a technology's stage split is a
sum of series lines like every other figure (docs/24-trends.md §11.1).

**What is served.** A monthly series is served only while its source is visible to the caller
(`services/api/visibility.py::source_visible`, decided by the API and passed in), and a month its
data-quality checks held is left out until an operator releases it (`withheld`); a newer reading
of a served month that its checks held is listed under `held_readings` while the month's earlier
document stays served. A daily series serves only the documents computed under a posture the
current one can publish: under `commercial`, documents computed under `noncommercial` are left out
(CLAUDE.md: `noncommercial` rows never flow to a commercial use; docs/00-PLAN.md 2026-10-23,
`genuine-change-v2`). The credit of a daily series is every contributing source's own
(`daily_source_rows`, which the API turns into the licence summary).

Reads files only (the documents under the connector data root), never the database.
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import itertools
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.connectors.history.documents import MonthlyStore, is_held, shift_period
from services.trends.store import TrendStore

logger = logging.getLogger("services.trends.series")

EIA = "us.eia.860m.history"
ERCOT = "us.iso.ercot.gen_queue.history"
DAILY = "inframation.trends.daily"
MONTHLY_SOURCES: tuple[str, ...] = (EIA, ERCOT)
#: Sources whose terms ask for the publication date in the acknowledgment (docs/13 §2.11, EIA:
#: "includes the publication date, such as: Source: U.S. Energy Information Administration (Oct
#: 2008)"), with the months between a data month and its publication: EIA releases a month's
#: workbook in the last week of the next month (September 2026's on 23 October 2026).
DATED_CREDIT: dict[str, int] = {EIA: 1}
#: Breakdowns by the year a stock is waiting for (module docstring, "Breakdowns"): no change is
#: computed on a year's line from the start of that year.
STOCK_YEAR_DIMENSIONS = frozenset({"expected_year", "planned_retirement_year", "cod_year"})


@dataclass(frozen=True)
class SeriesSpec:
    """One named series. For a monthly source, `block` is the document's block and `value` the
    tally field; for the daily record, `block` is the dotted metrics path and `value` the leaf of a
    tally (None where the bucket is a bare count)."""

    metric: str
    label: str
    unit: str
    frequency: str
    source_id: str
    block: str
    value: str | None
    dimensions: Mapping[str, str]
    method: str
    #: Daily only: the dotted path of the basis's rule inside a document (`activity.rule`).
    rule_path: str | None = None
    #: Daily only: the `queries` key whose list total the figure is.
    query: str | None = None
    #: Daily only: the point is dated by the activity window's day, the day before `as_of`.
    window_dated: bool = False

    @property
    def dimension_names(self) -> list[str]:
        return list(self.dimensions)


_EIA_METHOD = (
    "EIA-860M, the Preliminary Monthly Electric Generator Inventory, one workbook a month since July "
    "2015. Generators of 1 MW or more in the 50 states and the District of Columbia (Puerto Rico, "
    "listed from March 2018, is left out of every month); net summer capacity, the measure every "
    "month states. EIA: reported capacities are preliminary estimates, corrected without notice in "
    "later months."
)
_ERCOT_METHOD = (
    "ERCOT's monthly Generation Interconnection Status report since December 2018: the projects it "
    "lists (those for which a full interconnection study has been requested), counted and their MW "
    "summed as listed (a repowering is a net change). Technology is read from ERCOT's fuel code where "
    "the fuel names the plant (coal, biomass, oil, nuclear, ...), else from its fuel and technology "
    "codes. Status is the register's milestone rule: synchronised built, energised under construction, "
    "IA signed contracted, else studied. A corrected re-issue of a month replaces it."
)
_STOCK_NOTE = (
    " Lines by {what} are stocks: a year's line empties as that year's {who}, so no change is computed "
    "on it from the start of its year."
)
_DAILY_METHOD = (
    "Inframation's daily record of its public figures, once per UTC day from 2026-10-23: each figure "
    "is the total of the public list query the document names, as an anonymous visitor was served it "
    "at 05:58 UTC. Counts projects in Inframation's register, not a census of an industry: a day on "
    "which the contributing sources change (a register goes live, or is withheld) is a break, and no "
    "change is computed across it."
)
_ACTIVITY_METHOD = (
    "Change events of one UTC day counted under the rule `genuine-change-v2`: a change a source run "
    "showed after the store already held that source; a source's first load, a released data-quality "
    "hold and edits are left out (docs/24-trends.md section 6). Dated by the day the change was seen. "
    "The day's events list (/v1/events for that day) shows every visible event, the left-out ones "
    "included; the day's document counts them under activity.left_out, so a point names its document "
    "and no list query."
)

_EIA_PAIRS_NOTE = (
    " The state_status and technology_status breakdowns are one line per state, or technology, and EIA "
    "status code together (US-TX|U, solar|TS), so a state's or a technology's lines split by status; a "
    "month read before these tallies existed has no point on them."
)
_EIA_DIMS = {"technology": "technology", "state": "state"}
#: The sheets that state a status are also tallied by each of state and technology with the status:
#: the EIA-860M connector's `PAIRED` (a test holds the two equal; this module reads documents and
#: imports no connector).
EIA_PAIRED: tuple[str, ...] = ("state_status", "technology_status")
_OPERATING_DIMS = {**_EIA_DIMS, "status": "status", **{name: name for name in EIA_PAIRED}}
_PROPOSAL_DIMS = {
    "kind": "by_kind",
    "technology": "by_technology",
    "status": "by_lifecycle_state",
    "state": "by_state",
    "grid_operator": "by_iso",
}

CATALOGUE: tuple[SeriesSpec, ...] = (
    SeriesSpec(
        "us_operating_capacity_mw",
        "US operating capacity",
        "MW",
        "monthly",
        EIA,
        "operating",
        "mw",
        _OPERATING_DIMS,
        _EIA_METHOD + _EIA_PAIRS_NOTE,
    ),
    SeriesSpec(
        "us_operating_generators",
        "US operating generators",
        "generators",
        "monthly",
        EIA,
        "operating",
        "count",
        _OPERATING_DIMS,
        _EIA_METHOD + _EIA_PAIRS_NOTE,
    ),
    SeriesSpec(
        "us_planned_capacity_mw",
        "US planned capacity",
        "MW",
        "monthly",
        EIA,
        "planned",
        "mw",
        {**_OPERATING_DIMS, "expected_year": "expected_year"},
        _EIA_METHOD + _EIA_PAIRS_NOTE + _STOCK_NOTE.format(what="expected year", who="plants come online"),
    ),
    SeriesSpec(
        "us_planned_generators",
        "US planned generators",
        "generators",
        "monthly",
        EIA,
        "planned",
        "count",
        {**_OPERATING_DIMS, "expected_year": "expected_year"},
        _EIA_METHOD + _EIA_PAIRS_NOTE + _STOCK_NOTE.format(what="expected year", who="plants come online"),
    ),
    SeriesSpec(
        "us_retired_capacity_mw",
        "US retired capacity, as listed",
        "MW",
        "monthly",
        EIA,
        "retired",
        "mw",
        {**_EIA_DIMS, "retirement_year": "retirement_year"},
        _EIA_METHOD
        + " The Retired sheet lists every retirement since 2002 only from March 2017; a month whose list"
        " shrinks, or whose settled years move, is flagged by its checks (docs/24-trends.md section 9.4).",
    ),
    SeriesSpec(
        "us_planned_retirements_mw",
        "US planned retirements",
        "MW",
        "monthly",
        EIA,
        "planned_retirements",
        "mw",
        {**_EIA_DIMS, "planned_retirement_year": "planned_retirement_year"},
        _EIA_METHOD
        + " Operating generators with a planned retirement date."
        + _STOCK_NOTE.format(what="planned retirement year", who="generators retire"),
    ),
    SeriesSpec(
        "us_canceled_postponed_mw",
        "US canceled or postponed capacity",
        "MW",
        "monthly",
        EIA,
        "canceled_postponed",
        "mw",
        _EIA_DIMS,
        _EIA_METHOD,
    ),
    SeriesSpec(
        "ercot_queue_mw",
        "ERCOT interconnection queue",
        "MW",
        "monthly",
        ERCOT,
        "projects",
        "mw",
        {
            "technology": "technology",
            "status": "status",
            "phase": "phase",
            "zone": "zone",
            "cod_year": "cod_year",
        },
        _ERCOT_METHOD + _STOCK_NOTE.format(what="projected COD year", who="projects reach operation"),
    ),
    SeriesSpec(
        "ercot_queue_projects",
        "ERCOT interconnection queue",
        "projects",
        "monthly",
        ERCOT,
        "projects",
        "count",
        {
            "technology": "technology",
            "status": "status",
            "phase": "phase",
            "zone": "zone",
            "cod_year": "cod_year",
        },
        _ERCOT_METHOD + _STOCK_NOTE.format(what="projected COD year", who="projects reach operation"),
    ),
    SeriesSpec(
        "inframation_active_projects",
        "Active projects",
        "projects",
        "daily",
        DAILY,
        "proposals.active",
        "records",
        _PROPOSAL_DIMS,
        _DAILY_METHOD,
        rule_path="visibility.active_rule",
        query="proposals.active",
    ),
    SeriesSpec(
        "inframation_active_capacity_mw",
        "Active projects' capacity",
        "MW",
        "daily",
        DAILY,
        "proposals.active",
        "capacity_mw",
        _PROPOSAL_DIMS,
        _DAILY_METHOD,
        rule_path="visibility.active_rule",
        query="proposals.active",
    ),
    SeriesSpec(
        "inframation_withdrawn_projects",
        "Withdrawn or cancelled projects",
        "projects",
        "daily",
        DAILY,
        "proposals.withdrawn",
        "records",
        {"kind": "by_kind", "state": "by_state"},
        _DAILY_METHOD,
        query="proposals.withdrawn",
    ),
    SeriesSpec(
        "inframation_built_projects",
        "Built projects",
        "projects",
        "daily",
        DAILY,
        "proposals.built",
        "records",
        {"kind": "by_kind", "state": "by_state"},
        _DAILY_METHOD,
        query="proposals.built",
    ),
    SeriesSpec(
        "inframation_open_energy_notices",
        "Open energy RFPs and funding calls",
        "notices",
        "daily",
        DAILY,
        "opportunities.open.energy",
        None,
        {"kind": "by_kind", "state": "by_state"},
        _DAILY_METHOD,
        rule_path="visibility.opportunity_relevance",
        query="opportunities.open.energy",
    ),
    SeriesSpec(
        "inframation_changes",
        "Changes at sources",
        "events",
        "daily",
        DAILY,
        "activity.counted",
        None,
        {"event_type": "by_event_type"},
        _ACTIVITY_METHOD,
        rule_path="metrics.activity.rule",
        # No list query totals this figure (`_ACTIVITY_METHOD`): `activity.visible_events` counts the
        # left-out events too, and a point names only what its link shows (docs/00-PLAN.md 2026-10-23).
        query=None,
        window_dated=True,
    ),
)
BY_METRIC: dict[str, SeriesSpec] = {spec.metric: spec for spec in CATALOGUE}


class SeriesError(ValueError):
    """A request names no series, or a breakdown the series does not have."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name


# ------------------------------------------------------------------------------- data root
def data_root() -> Path:
    """The connector data root the documents live under (`INFRAQUE_DATA_DIR`, else this checkout's
    `data/`); the API mounts it read-only on the single host (`infra/compose/compose.single.yml`)."""
    env = os.environ.get("INFRAQUE_DATA_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "data"


# ---------------------------------------------------------------------------- compact readers
#: A tally as (count, mw, nameplate_mw), the fields a monthly series reads.
_Tally = tuple[float | None, float | None, float | None]


def _tally(raw: Any) -> _Tally:
    if not isinstance(raw, Mapping):
        return (None, None, None)
    return (raw.get("count"), raw.get("mw"), raw.get("nameplate_mw"))


@dataclass(frozen=True)
class MonthPoint:
    """One month of one monthly source, reduced to what the series read."""

    period: str
    source_url: str
    retrieved_at: str
    blocks: Mapping[str, Mapping[str, Any]]
    #: The parser that read the month: two months compare only when one parser read both.
    parser_version: str = ""


#: The licence fields of a contributing source as a daily document records them
#: (`services/trends/snapshot.py::contributing_sources`).
_SOURCE_FIELDS = (
    "source_id",
    "name",
    "operator",
    "licence_id",
    "licence_name",
    "licence_url",
    "reuse_class",
    "attribution_text",
    "requires_link_back",
)


@dataclass(frozen=True)
class DayPoint:
    """One day of the daily record, reduced to the figures the catalogue reads."""

    as_of: str
    window_day: str | None
    posture: str
    rules: Mapping[str, str]
    figures: Mapping[str, Any]
    queries: Mapping[str, str]
    #: The contributing sources by what they contribute to (`proposals`, `opportunities`): the
    #: day's coverage, part of every daily basis (module docstring).
    coverage: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: `visibility.withheld_sources`: registered sources the tier did not read; None if unrecorded.
    withheld: int | None = None
    #: The contributing sources' licence fields and latest retrieval, for the credit.
    sources: tuple[Mapping[str, Any], ...] = ()


def _warnings(document: Mapping[str, Any], block: str) -> tuple[str, ...]:
    """The checks that warned on `block` of a served month, or on the whole month: served with
    the point, so a chart can flag a month whose list shrank or whose file named the wrong month."""
    checks = (document.get("dq") or {}).get("checks") or []
    return tuple(
        sorted(
            {
                str(c.get("check"))
                for c in checks
                if isinstance(c, Mapping) and c.get("level") == "warn" and c.get("block") in (block, None)
            }
        )
    )


def _compact_month(document: Mapping[str, Any], latest: str | None) -> MonthPoint:
    blocks: dict[str, dict[str, Any]] = {}
    for name, block in (document.get("blocks") or {}).items():
        if not isinstance(block, Mapping):
            continue
        dims = {
            key[3:]: {k: _tally(v) for k, v in values.items()}
            for key, values in block.items()
            if key.startswith("by_") and isinstance(values, Mapping)
        }
        blocks[name] = {
            "basis": str(block.get("basis") or ""),
            "total": _tally(block.get("total")),
            "dims": dims,
            "warnings": _warnings(document, name),
        }
    url = str(document.get("source_url") or "")
    archive = document.get("archive_url")
    if archive and latest is not None and str(document.get("period")) < latest:
        url = str(archive)  # a month EIA has since moved under archive/ is read there
    return MonthPoint(
        period=str(document["period"]),
        source_url=url,
        retrieved_at=str(document.get("retrieved_at") or ""),
        blocks=blocks,
        parser_version=str(document.get("parser_version") or ""),
    )


def _figure(document: Mapping[str, Any], path: str) -> Any:
    node: Any = document.get("metrics")
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _path(document: Mapping[str, Any], path: str) -> Any:
    node: Any = document
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _compact_day(document: Mapping[str, Any]) -> DayPoint:
    figures: dict[str, Any] = {}
    rules: dict[str, str] = {}
    for spec in CATALOGUE:
        if spec.frequency != "daily":
            continue
        block = _figure(document, spec.block)
        figures[spec.block] = _reduce_daily_block(block, spec)
        if spec.rule_path:
            rules[spec.rule_path] = str(_path(document, spec.rule_path) or "")
    window = _path(document, "metrics.activity.window.from")
    contributing = [
        s for s in (document.get("sources") or []) if isinstance(s, Mapping) and s.get("source_id")
    ]

    def linked(field_name: str) -> tuple[str, ...]:
        return tuple(sorted(str(s["source_id"]) for s in contributing if (s.get(field_name) or 0) > 0))

    withheld = _path(document, "visibility.withheld_sources")
    return DayPoint(
        as_of=str(document["as_of"]),
        window_day=str(window)[:10] if window else None,
        posture=str(_path(document, "visibility.platform_posture") or "noncommercial"),
        rules=rules,
        figures=figures,
        queries={k: str(v) for k, v in (document.get("queries") or {}).items()},
        coverage={"proposals": linked("proposal_links"), "opportunities": linked("opportunity_links")},
        withheld=withheld if isinstance(withheld, int) and not isinstance(withheld, bool) else None,
        sources=tuple(
            {**{k: s.get(k) for k in _SOURCE_FIELDS}, "retrieved_at": s.get("retrieved_at_max")}
            for s in contributing
        ),
    )


def _leaf(value: Any, leaf: str | None) -> Any:
    if leaf is None:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return value.get(leaf) if isinstance(value, Mapping) else None


#: Where a daily block keeps its total, and the leaves of a bucket: a proposal block's total is a
#: tally (`records`, `capacity_mw`, ...), a notice block's a bare `records` count, the activity's a
#: bare `events` count; their buckets are tallies and bare counts alike.
_DAILY_SHAPES: dict[str, tuple[str, tuple[str | None, ...]]] = {
    "proposals": ("total", ("records", "capacity_mw")),
    "opportunities": ("records", (None,)),
    "activity": ("events", (None,)),
}


def _reduce_daily_block(block: Any, spec: SeriesSpec) -> dict[str, Any] | None:
    """The total and each breakdown of one block, keeping only the numbers the catalogue reads."""
    if not isinstance(block, Mapping):
        return None
    total_key, leaves = _DAILY_SHAPES[spec.block.split(".", 1)[0]]
    total = block.get(total_key)
    out: dict[str, Any] = {"total": {(leaf or "value"): _leaf(total, leaf) for leaf in leaves}, "dims": {}}
    for dim_key in {v for s in CATALOGUE if s.block == spec.block for v in s.dimensions.values()}:
        values = block.get(dim_key)
        if isinstance(values, Mapping):
            out["dims"][dim_key] = {
                k: {(leaf or "value"): _leaf(v, leaf) for leaf in leaves} for k, v in values.items()
            }
    return out


# ------------------------------------------------------------------------------------- cache
class _Cache:
    """A small LRU of reduced documents per source, keyed by a fingerprint of the files (name,
    mtime, size): a new or replaced document invalidates it, an unchanged directory is never read
    twice. Thread-safe: the API serves requests on a pool."""

    def __init__(self, size: int = 8) -> None:
        self.size = size
        self.entries: OrderedDict[tuple[str, str], tuple[tuple[Any, ...], Any]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key: tuple[str, str], fingerprint: tuple[Any, ...]) -> Any:
        with self.lock:
            hit = self.entries.get(key)
            if hit is None or hit[0] != fingerprint:
                return None
            self.entries.move_to_end(key)
            return hit[1]

    def put(self, key: tuple[str, str], fingerprint: tuple[Any, ...], value: Any) -> None:
        with self.lock:
            self.entries[key] = (fingerprint, value)
            self.entries.move_to_end(key)
            while len(self.entries) > self.size:
                self.entries.popitem(last=False)

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()


_CACHE = _Cache()


def _fingerprint(directory: Path) -> tuple[Any, ...]:
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return ()
    out = []
    for entry in entries:
        if not entry.name.endswith(".json") or entry.name.startswith("."):
            continue
        try:
            info = entry.stat()
        except OSError:
            continue
        out.append((entry.name, info.st_mtime_ns, info.st_size))
    return tuple(out)


@dataclass
class MonthlyRead:
    points: list[MonthPoint] = field(default_factory=list)
    withheld: list[dict[str, Any]] = field(default_factory=list)
    #: Served months whose newer reading its checks hold beside them (`MonthlyStore.write_pending`).
    held_readings: list[dict[str, Any]] = field(default_factory=list)


def _held_checks(document: Mapping[str, Any]) -> list[str]:
    checks = (document.get("dq") or {}).get("checks") or []
    return sorted(
        {str(c.get("check")) for c in checks if isinstance(c, Mapping) and c.get("level") == "hold"}
    )


def monthly_points(root: Path, source_id: str) -> MonthlyRead:
    """Every month of `source_id` a reader may see, oldest first, the months held back, and the
    newer readings held beside served months."""
    store = MonthlyStore(root)
    directory = store.dir_for(source_id)
    fingerprint = _fingerprint(directory)
    cached = _CACHE.get(("monthly", f"{root}|{source_id}"), fingerprint)
    if cached is not None:
        result: MonthlyRead = cached
        return result
    out = MonthlyRead()
    documents = list(store.documents(source_id))
    visible = [d for d in documents if not is_held(d)]
    latest = str(visible[-1]["period"]) if visible else None
    for document in documents:
        if is_held(document):
            out.withheld.append(
                {
                    "period": str(document["period"]),
                    "reason": "held by its data-quality checks",
                    "checks": _held_checks(document),
                }
            )
            continue
        out.points.append(_compact_month(document, latest))
    served = {p.period for p in out.points}
    for pending in store.pending(source_id):
        period = str(pending["period"])
        if period not in served:
            continue
        out.held_readings.append(
            {
                "period": period,
                "reason": (
                    "a newer reading of this month (a re-issue, a correction or a restatement) is held "
                    "by its data-quality checks; the month's earlier document is served until it is released"
                ),
                "checks": _held_checks(pending),
                "file_id": pending.get("file_id"),
                "parser_version": pending.get("parser_version"),
            }
        )
    _CACHE.put(("monthly", f"{root}|{source_id}"), fingerprint, out)
    return out


def daily_points(root: Path) -> list[DayPoint]:
    store = TrendStore(root)
    fingerprint = _fingerprint(store.daily)
    cached = _CACHE.get(("daily", str(root)), fingerprint)
    if cached is not None:
        result: list[DayPoint] = cached
        return result
    out = [_compact_day(document) for document in store.series()]
    _CACHE.put(("daily", str(root)), fingerprint, out)
    return out


def clear_cache() -> None:
    _CACHE.clear()


# ----------------------------------------------------------------------------------- periods
def _period_key(value: str, frequency: str, *, end: bool) -> str:
    """A `from`/`to` bound at the series' grain: `2026-09-15` is `2026-09` for a monthly series; a
    bare month is its first (or, for `to`, last) day for a daily series."""
    if frequency == "monthly":
        return value[:7]
    if len(value) == 7:
        year, month = (int(x) for x in value.split("-"))
        day = calendar.monthrange(year, month)[1] if end else 1
        return f"{value}-{day:02d}"
    return value


def _valid_bound(value: str) -> bool:
    for fmt in ("%Y-%m", "%Y-%m-%d"):
        try:
            dt.datetime.strptime(value, fmt)  # noqa: DTZ007 -- a calendar bound, no time zone
            return len(value) in (7, 10)
        except ValueError:
            continue
    return False


def _prior(period: str, frequency: str, months: int) -> str | None:
    if frequency == "monthly":
        return shift_period(period, -months)
    day = dt.date.fromisoformat(period)
    index = day.year * 12 + day.month - 1 - months
    year, month = index // 12, index % 12 + 1
    if day.day > calendar.monthrange(year, month)[1]:
        return None  # 31 March has no 31 February: no like-for-like day
    return dt.date(year, month, day.day).isoformat()


def _change(
    value: Any, prior: Mapping[str, Any] | None, like: Any, period: str | None
) -> dict[str, Any] | None:
    if prior is None or period is None or prior["_like"] != like:
        return None
    before = prior["value"]
    if value is None or before is None:
        return None
    change = round(float(value) - float(before), 3)
    pct = round(change / float(before) * 100.0, 2) if before else None
    return {"from": period, "value": before, "change": change, "pct": pct}


def with_changes(
    points: list[dict[str, Any]], frequency: str, *, stock_year: str | None = None
) -> list[dict[str, Any]]:
    """`mom` and `yoy` on each point, like for like only (module docstring). `stock_year` is the
    key of a line by the year a stock is waiting for: from that year on, no change is computed."""
    by_period = {p["period"]: p for p in points}
    for point in points:
        settling = stock_year is not None and point["period"][:4] >= stock_year
        for name, months in (("mom", 1), ("yoy", 12)):
            prior_period = _prior(point["period"], frequency, months)
            prior = by_period.get(prior_period) if prior_period else None
            point[name] = None if settling else _change(point["value"], prior, point["_like"], prior_period)
    return points


def breaks(points: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every change of what is compared between consecutive points: the basis, and with it the
    parser that read a month or the sources that contributed to a day."""
    out = []
    for before, after in itertools.pairwise(points):
        if before["_like"] == after["_like"]:
            continue
        entry: dict[str, Any] = {
            "period": after["period"],
            "basis_before": before["basis"],
            "basis_after": after["basis"],
        }
        if before.get("_parser") != after.get("_parser"):
            entry.update(parser_before=before.get("_parser"), parser_after=after.get("_parser"))
        if "_sources" in before and before["_sources"] != after["_sources"]:
            entry.update(
                sources_added=sorted(set(after["_sources"]) - set(before["_sources"])),
                sources_removed=sorted(set(before["_sources"]) - set(after["_sources"])),
            )
        if "_withheld" in before and before["_withheld"] != after["_withheld"]:
            entry.update(withheld_before=before["_withheld"], withheld_after=after["_withheld"])
        out.append(entry)
    return out


def _public(point: Mapping[str, Any]) -> dict[str, Any]:
    """A point as served: the comparison keys (`_like` and what it is made of) stay inside."""
    return {k: v for k, v in point.items() if not k.startswith("_")}


# ------------------------------------------------------------------------------------ series
_VALUE_INDEX = {"count": 0, "mw": 1, "nameplate_mw": 2}


def _number(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        return round(value, 3)
    return value if isinstance(value, int) else None


def _monthly_lines(
    spec: SeriesSpec, read: MonthlyRead, by: str | None, start: str | None, end: str | None
) -> dict[str, list[dict[str, Any]]]:
    """The lines of a monthly series, with a point for each month from `start` to `end`. Every key
    any month tallied is a line, with no point outside the bounds, so a bounded read lists the
    same lines as the whole series."""
    index = _VALUE_INDEX[spec.value or "mw"]
    dim = spec.dimensions[by] if by else None
    keys: set[str] = set()
    if dim:
        for month in read.points:
            block = month.blocks.get(spec.block)
            if block:
                keys.update((block["dims"].get(dim) or {}).keys())
        lines: dict[str, list[dict[str, Any]]] = {key: [] for key in keys}
    else:
        lines = {"total": []} if any(spec.block in m.blocks for m in read.points) else {}
    for month in read.points:
        if (start and month.period < start) or (end and month.period > end):
            continue
        block = month.blocks.get(spec.block)
        if block is None:
            continue
        base = {
            **_compared(block, month),
            "source_url": month.source_url,
            "retrieved_at": month.retrieved_at,
        }
        if block.get("warnings"):
            base["dq_warnings"] = list(block["warnings"])
        if dim is None:
            lines["total"].append({"period": month.period, "value": _number(block["total"][index]), **base})
            continue
        tallied = block["dims"].get(dim)
        if tallied is None:
            continue  # the month did not tally the dimension: no point, never a zero (module docstring)
        for key in keys:
            # A complete tally without this key holds none of it (module docstring).
            value = _number(tallied[key][index]) if key in tallied else 0
            lines[key].append({"period": month.period, "value": value, **base})
    return lines


def _compared(block: Mapping[str, Any], month: MonthPoint) -> dict[str, Any]:
    """What a monthly point's change compares, and its break reports: the block's basis and the
    parser that read the month (module docstring, "Like for like")."""
    return {
        "basis": block["basis"],
        "_parser": month.parser_version,
        "_like": (block["basis"], month.parser_version),
    }


def _monthly_skeleton(spec: SeriesSpec, read: MonthlyRead, by: str | None) -> list[dict[str, Any]]:
    """The months a monthly series' lines have a point in, each with what a change compares
    (`_compared`): one sequence for every line of a breakdown, since a complete tally gives every
    key a point (a zero where it holds none) in every month that tallied the dimension. Its `breaks`
    are therefore every line's breaks, over the whole archive, whatever the window."""
    dim = spec.dimensions[by] if by else None
    out = []
    for month in read.points:
        block = month.blocks.get(spec.block)
        if block is None or (dim is not None and block["dims"].get(dim) is None):
            continue
        out.append({"period": month.period, **_compared(block, month)})
    return out


def _coverage(spec: SeriesSpec, day: DayPoint) -> tuple[str, ...]:
    """The sources that contribute to `spec`'s figure on `day`: those linked to a counted proposal
    for a proposal figure, to a counted notice for a notice figure, to either for the activity."""
    subject = spec.block.split(".", 1)[0]
    if subject in ("proposals", "opportunities"):
        return tuple(day.coverage.get(subject) or ())
    return tuple(
        sorted(set(day.coverage.get("proposals") or ()) | set(day.coverage.get("opportunities") or ()))
    )


def _daily_basis(spec: SeriesSpec, day: DayPoint, sources: Sequence[str]) -> str:
    """The posture, the rule, and the coverage: how many sources contributed (and a digest of which)
    and how many were withheld (module docstring, "Coverage is part of a daily basis")."""
    parts = ["inframation-daily", day.posture]
    if spec.rule_path:
        parts.append(day.rules.get(spec.rule_path) or "")
    digest = hashlib.sha256("\n".join(sources).encode("utf-8")).hexdigest()[:10]
    parts.append(f"sources-{len(sources)}-{digest}")
    parts.append(f"withheld-{day.withheld}" if day.withheld is not None else "withheld-unrecorded")
    return "/".join(parts)


def _daily_lines(
    spec: SeriesSpec, days: Sequence[DayPoint], by: str | None, start: str | None, end: str | None
) -> dict[str, list[dict[str, Any]]]:
    leaf = spec.value or "value"
    dim = spec.dimensions[by] if by else None
    lines: dict[str, list[dict[str, Any]]] = {}
    keys: set[str] = set()
    if dim:
        for day in days:
            block = day.figures.get(spec.block)
            if block:
                keys.update((block["dims"].get(dim) or {}).keys())
    for day in days:
        period = day.window_day if spec.window_dated else day.as_of
        if period is None or (start and period < start) or (end and period > end):
            continue
        block = day.figures.get(spec.block)
        sources = _coverage(spec, day)
        basis = _daily_basis(spec, day, sources)
        base = {
            "basis": basis,
            "document": day.as_of,
            "query": day.queries.get(spec.query) if spec.query else None,
            "_like": basis,
            "_sources": sources,
            "_withheld": day.withheld,
        }
        if dim is None:
            value = block["total"].get(leaf) if block else None
            lines.setdefault("total", []).append({"period": period, "value": _number(value), **base})
            continue
        tallied = block["dims"].get(dim) if block else None
        if tallied is None:
            continue  # the day's document did not tally the dimension: no point (module docstring)
        for key in keys:
            value = tallied[key].get(leaf) if key in tallied else 0
            lines.setdefault(key, []).append({"period": period, "value": _number(value), **base})
    return lines


@dataclass(frozen=True)
class SourceView:
    """What the API decided about a monthly source for this caller: visible, and its credit."""

    source_id: str
    name: str
    operator: str | None
    url: str | None
    licence: Mapping[str, Any]
    attribution_text: str | None


def publishable_postures(current: str) -> frozenset[str]:
    """The daily documents' postures a reader under `current` may see (module docstring)."""
    return (
        frozenset({"commercial"}) if current == "commercial" else frozenset({"commercial", "noncommercial"})
    )


def _credit(spec: SeriesSpec, source: SourceView | None, last: str | None) -> str | None:
    """The source's credit; for EIA with the publication month of the latest month read (its data
    month plus `DATED_CREDIT`'s lag), as EIA's acknowledgment asks."""
    if source is None:
        return None
    return dated_credit(spec.source_id, source.attribution_text or source.name, last)


def dated_credit(source_id: str, text: str, period: str | None) -> str:
    """`text` with the publication month of the data month `period` for a source whose terms ask
    for it (`DATED_CREDIT`: "Source: U.S. Energy Information Administration (Oct 2026)" for EIA's
    September 2026), else `text` unchanged."""
    lag = DATED_CREDIT.get(source_id)
    if lag is None or not period:
        return text
    year, month = (int(x) for x in shift_period(period[:7], lag).split("-"))
    return f"{text} ({calendar.month_abbr[month]} {year})"


def _daily_credit(days: Sequence[DayPoint]) -> str | None:
    """Every contributing source's own credit over `days` (its attribution text, else its name),
    in source order: what the licence summary lists one by one."""
    credits: dict[str, str] = {}
    for day in days:
        for source in day.sources:
            sid = str(source.get("source_id") or "")
            text = source.get("attribution_text") or source.get("name")
            if sid and text:
                credits[sid] = str(text)
    if not credits:
        return None
    return "; ".join(credits[sid] for sid in sorted(credits))


def _daily_source(credit: str | None) -> dict[str, Any]:
    return {
        "source_id": DAILY,
        "name": "Inframation daily record of its public figures",
        "operator": "Inframation",
        "url": None,
        "licence": {
            "reuse_class": "mixed",
            "summary": "Counts derived from every source the public tier serves, each under its own licence; "
            "the licence summary lists each source the days read, with its credit.",
        },
        "credit": credit,
    }


@dataclass
class Catalogue:
    root: Path
    sources: Mapping[str, SourceView]
    posture: str
    _monthly: dict[str, MonthlyRead] = field(default_factory=dict)
    _daily: list[DayPoint] | None = None

    def monthly(self, source_id: str) -> MonthlyRead:
        if source_id not in self._monthly:
            self._monthly[source_id] = (
                monthly_points(self.root, source_id) if source_id in self.sources else MonthlyRead()
            )
        return self._monthly[source_id]

    def daily(self) -> list[DayPoint]:
        if self._daily is None:
            allowed = publishable_postures(self.posture)
            self._daily = [d for d in daily_points(self.root) if d.posture in allowed]
        return self._daily

    def available(self, spec: SeriesSpec) -> bool:
        if spec.frequency == "monthly":
            return spec.source_id in self.sources
        return True

    def describe(self, spec: SeriesSpec) -> dict[str, Any]:
        lines = self.lines(spec, None, None, None)
        total = lines.get("total", [])
        first = total[0]["period"] if total else None
        last = total[-1]["period"] if total else None
        if spec.frequency == "monthly":
            view = self.sources.get(spec.source_id)
            source = {
                "source_id": spec.source_id,
                "name": view.name if view else None,
                "operator": view.operator if view else None,
                "url": view.url if view else None,
                "licence": dict(view.licence) if view else None,
                "credit": _credit(spec, view, last),
            }
        else:
            source = _daily_source(_daily_credit(self.daily()))
        return {
            "metric": spec.metric,
            "label": spec.label,
            "unit": spec.unit,
            "frequency": spec.frequency,
            "source": source,
            "method": spec.method,
            "dimensions": spec.dimension_names,
            "first": first,
            "last": last,
            "points": len(total),
        }

    def lines(
        self, spec: SeriesSpec, by: str | None, start: str | None, end: str | None
    ) -> dict[str, list[dict[str, Any]]]:
        if spec.frequency == "monthly":
            return _monthly_lines(spec, self.monthly(spec.source_id), by, start, end)
        return _daily_lines(spec, self.daily(), by, start, end)


def catalogue(root: Path, sources: Mapping[str, SourceView], posture: str) -> list[dict[str, Any]]:
    """Every series the caller may read, described: source, licence, method, frequency, first and
    last dates."""
    cat = Catalogue(root, sources, posture)
    return [cat.describe(spec) for spec in CATALOGUE if cat.available(spec)]


CHANGES_RULE = (
    "mom and yoy compare a point with the point one month and twelve months before (a daily series: the "
    "same day of the month), only when both exist, both have a value, both have the same basis (for a "
    "daily series this includes the contributing sources and how many were withheld) and both months "
    "were read by the same parser; never on a line by a year a stock waits for, from the start of that "
    "year."
)


def series(
    root: Path,
    sources: Mapping[str, SourceView],
    posture: str,
    *,
    metric: str,
    by: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """One series, optionally broken down, between `start` and `end` (inclusive)."""
    spec = BY_METRIC.get(metric)
    cat = Catalogue(root, sources, posture)
    if spec is None or not cat.available(spec):
        raise SeriesError("metric", f"no series {metric!r}; GET /v1/trends lists them")
    if by is not None and by not in spec.dimensions:
        allowed = ", ".join(spec.dimension_names) or "none"
        raise SeriesError("by", f"{metric} has no breakdown {by!r} (one of: {allowed})")
    for name, value in (("from", start), ("to", end)):
        if value is not None and not _valid_bound(value):
            raise SeriesError(name, f"{name} must be YYYY-MM or YYYY-MM-DD")
    lo = _period_key(start, spec.frequency, end=False) if start else None
    hi = _period_key(end, spec.frequency, end=True) if end else None
    if lo and hi and lo > hi:
        raise SeriesError("from", "from is after to")
    description = cat.describe(spec)
    stocks = by is not None and spec.dimensions[by] in STOCK_YEAR_DIMENSIONS
    # Changes need the point a month and a year before the window, so they are computed with those
    # months and the window is cut after. A bounded monthly read reads no month earlier than that
    # (a one-month query of a paired breakdown, 331 lines, costs its window, not 135 months); its
    # lines' breaks are the archive's own (`_monthly_skeleton`), as an unbounded read lists them.
    shared_breaks: list[dict[str, Any]] | None = None
    if spec.frequency == "monthly" and (lo or hi):
        lines = cat.lines(spec, by, shift_period(lo, -12) if lo else None, hi)
        shared_breaks = breaks(_monthly_skeleton(spec, cat.monthly(spec.source_id), by))
    else:
        lines = cat.lines(spec, by, None, None)
    out_lines = []
    for key in sorted(lines, key=_line_order):
        stock_year = key if stocks and key.isdigit() else None
        points = with_changes(lines[key], spec.frequency, stock_year=stock_year)
        windowed = [
            _public(p)
            for p in points
            if (lo is None or p["period"] >= lo) and (hi is None or p["period"] <= hi)
        ]
        line_breaks = [dict(b) for b in shared_breaks] if shared_breaks is not None else breaks(points)
        out_lines.append({"key": key, "breaks": line_breaks, "points": windowed})
    read = cat.monthly(spec.source_id) if spec.frequency == "monthly" else MonthlyRead()
    return {
        **description,
        "by": by,
        "from": lo,
        "to": hi,
        "changes_rule": CHANGES_RULE,
        "lines": out_lines,
        "withheld": list(read.withheld),
        "held_readings": list(read.held_readings),
    }


def _line_order(key: str) -> tuple[int, str]:
    return (0 if key == "total" else 2 if key.startswith("_") else 1, key)


def source_ids() -> tuple[str, ...]:
    return MONTHLY_SOURCES


def _instant(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        when = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


def monthly_retrievals(
    root: Path, source_id: str, periods: Iterable[str] | None = None
) -> list[tuple[str, dt.datetime | None]]:
    """`(period, retrieved_at)` of each month of `source_id` a reader may see, or of those among
    `periods`: one licence summary row per month a response read."""
    wanted = None if periods is None else set(periods)
    return [
        (point.period, _instant(point.retrieved_at))
        for point in monthly_points(root, source_id).points
        if wanted is None or point.period in wanted
    ]


@dataclass(frozen=True)
class LatestMonth:
    """The newest month of a monthly source a reader may see (`latest_month`)."""

    point: MonthPoint
    #: Months after `point` that their data-quality checks hold back: the source has a newer
    #: month, and it is not served until an operator releases it.
    newer_withheld: tuple[Mapping[str, Any], ...]
    #: A newer reading of `point`'s own month held beside it (`MonthlyRead.held_readings`), or None.
    held_reading: Mapping[str, Any] | None


def latest_month(root: Path, source_id: str) -> LatestMonth | None:
    """The newest served month of `source_id`, as the series read it (`monthly_points`: held months
    left out, the same cache), with what is held back after it; None when no month is served. A
    figure read from it is the point at `point.period` of the series that read the same block
    (`GET /v1/stats/planned-vs-operating`, docs/24-trends.md §11)."""
    read = monthly_points(root, source_id)
    if not read.points:
        return None
    point = read.points[-1]
    return LatestMonth(
        point=point,
        newer_withheld=tuple(w for w in read.withheld if w["period"] > point.period),
        held_reading=next((h for h in read.held_readings if h["period"] == point.period), None),
    )


def daily_source_rows(
    root: Path, posture: str, documents: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """One row per contributing source of each daily document a reader under `posture` may see (or
    of those among `documents`, by `as_of`), with the licence and credit the document recorded and
    the source's latest retrieval that day: the rows `services/api/serialize.py::build_licence_summary`
    takes, so every daily series carries each source's own credit."""
    wanted = None if documents is None else set(documents)
    allowed = publishable_postures(posture)
    out = []
    for day in daily_points(root):
        if day.posture not in allowed or (wanted is not None and day.as_of not in wanted):
            continue
        for source in day.sources:
            out.append(
                {
                    "source_id": str(source.get("source_id")),
                    "name": str(source.get("name") or source.get("source_id")),
                    "operator": source.get("operator"),
                    "licence_id": str(source.get("licence_id") or ""),
                    "licence_name": str(source.get("licence_name") or source.get("licence_id") or ""),
                    "licence_url": source.get("licence_url"),
                    "reuse_class": str(source.get("reuse_class") or ""),
                    "attribution_text": source.get("attribution_text"),
                    "requires_link_back": bool(source.get("requires_link_back")),
                    "retrieved_at": _instant(source.get("retrieved_at")),
                    "document": day.as_of,
                }
            )
    return out


def specs(frequency: str | None = None) -> Iterable[SeriesSpec]:
    return (s for s in CATALOGUE if frequency is None or s.frequency == frequency)


__all__ = [
    "BY_METRIC",
    "CATALOGUE",
    "CHANGES_RULE",
    "DAILY",
    "EIA",
    "EIA_PAIRED",
    "ERCOT",
    "MONTHLY_SOURCES",
    "STOCK_YEAR_DIMENSIONS",
    "Catalogue",
    "LatestMonth",
    "MonthPoint",
    "SeriesError",
    "SeriesSpec",
    "SourceView",
    "breaks",
    "catalogue",
    "clear_cache",
    "daily_points",
    "daily_source_rows",
    "data_root",
    "dated_credit",
    "latest_month",
    "monthly_points",
    "monthly_retrievals",
    "publishable_postures",
    "series",
    "source_ids",
    "specs",
    "with_changes",
]
