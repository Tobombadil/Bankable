"""Click-to-sort column headers on the record lists (lane list-sort, 2026-10-22; docs/16 §4.3).

One rule on every list: a click on a sortable heading sorts by that column -- numbers largest first,
names A to Z, dates newest first (deadlines soonest first, the RFPs & funding page's own order) --
and a second click on the heading already sorted reverses it. The heading shows an arrow for the
current order. Each heading is a plain link carrying the view's own query with `sort=` replaced and
the page cursor dropped, so a sorted view is a URL that can be shared, the filters are kept, and the
lists need no script. `sort=` carries the API's own token (`-capacity_mw`), never a second
vocabulary (D-17: one URL grammar for list, map, feed and API).

A token the page does not offer falls back to the page's default instead of reaching the API,
which answers an unknown sort field with `400` (`services/api/params.py::sort_spec`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from starlette.datastructures import QueryParams


@dataclass(frozen=True)
class SortColumn:
    """One sortable heading: the API field it orders by, the order a first click gives, and the
    words the caption and the heading's tooltip use for each order."""

    field: str
    first: str  # "desc" or "asc"
    noun: str
    desc_words: str
    asc_words: str

    def words(self, direction: str) -> str:
        return self.desc_words if direction == "desc" else self.asc_words


def column(field: str, kind: str, noun: str) -> SortColumn:
    """`kind`: `number` (largest first), `name` (A to Z), `date` (newest first) or `deadline`
    (soonest first)."""
    if kind == "number":
        return SortColumn(field, "desc", noun, "largest first", "smallest first")
    if kind == "name":
        return SortColumn(field, "asc", noun, "Z to A", "A to Z")
    if kind == "date":
        return SortColumn(field, "desc", noun, "latest first", "earliest first")
    if kind == "deadline":
        return SortColumn(field, "asc", noun, "latest first", "soonest first")
    raise ValueError(kind)


# ---- the four lists' sortable headings, keyed by the heading's own name in the template ----------
#: `/proposals` (API `services/api/records.py::PROPOSAL_SORT_ALLOWLIST`).
PROPOSAL_COLUMNS: dict[str, SortColumn] = {
    "name": column("name_canonical", "name", "name"),
    "capacity": column("capacity_mw", "number", "capacity"),
    "online": column("proposed_online_date", "date", "proposed online date"),
}
#: Tokens the API sorts proposals by that no heading offers; kept so an older link still sorts.
PROPOSAL_OTHER: dict[str, SortColumn] = {
    "last_changed": column("last_changed", "date", "last change"),
    "first_seen": column("first_seen", "date", "first seen"),
}
#: `/opportunities` (API `OPPORTUNITY_SORT_ALLOWLIST`). The Value column sorts by `value`, each
#: notice's approximate US-dollar equivalent, which ranks across currencies where `budget_amount`
#: cannot; notices with no equivalent go last either way (lane pages, 2026-10-23).
OPPORTUNITY_COLUMNS: dict[str, SortColumn] = {
    "value": column("value", "number", "value in US dollars"),
    "due": column("due_at", "deadline", "deadline"),
}
OPPORTUNITY_OTHER: dict[str, SortColumn] = {
    "open_at": column("open_at", "date", "opening date"),
    "last_changed": column("last_changed", "date", "last change"),
}
#: `/assets` (API `services/api/assets.py::ASSET_SORT_ALLOWLIST`). The operator column does not
#: sort: a withheld operator name would still order the rows (`services/api/withheld_names.py`).
ASSET_COLUMNS: dict[str, SortColumn] = {
    "name": column("name", "name", "name"),
    "capacity": column("capacity_mw", "number", "capacity"),
}
ASSET_OTHER: dict[str, SortColumn] = {
    "last_changed": column("last_changed", "date", "last change"),
    "first_seen": column("first_seen", "date", "first seen"),
    "retirement_year": column("retirement_year", "date", "retirement year"),
}
#: `/interconnection-points` (API `services/api/interconnection_points.py::SORTS`).
POINT_COLUMNS: dict[str, SortColumn] = {
    "name": column("name", "name", "name"),
    "voltage": column("voltage_kv", "number", "voltage"),
    "active_mw": column("active_mw", "number", "active queued MW"),
    "active_count": column("active_count", "number", "active projects"),
    "withdrawn_count": column("withdrawn_count", "number", "withdrawn projects"),
    "built_count": column("built_count", "number", "built projects"),
}


def _parse(token: str | None) -> tuple[str, str] | None:
    if not token:
        return None
    token = token.strip().split(",")[0]
    if token.startswith("-"):
        return token[1:], "desc"
    return token, "asc"


def _token(field: str, direction: str) -> str:
    return f"-{field}" if direction == "desc" else field


def chosen_sort(
    raw: str | None, columns: Mapping[str, SortColumn], other: Mapping[str, SortColumn] | None = None
) -> str | None:
    """The order the reader chose, as one API token, when `raw` names a field a heading (or
    `other`) sorts by; `None` for no choice or one the page does not offer. The filter form
    carries this, never a fallback, so a mistyped link does not pin the default into the URL."""
    parsed = _parse(raw)
    fields = {c.field for c in columns.values()} | {c.field for c in (other or {}).values()}
    if parsed is None or parsed[0] not in fields:
        return None
    return _token(*parsed)


def valid_sort(
    raw: str | None,
    columns: Mapping[str, SortColumn],
    default: str,
    other: Mapping[str, SortColumn] | None = None,
) -> str:
    """`raw` when it names a field a heading (or `other`) sorts by, else `default`."""
    return chosen_sort(raw, columns, other) or default


def sort_caption(
    current: str, columns: Mapping[str, SortColumn], other: Mapping[str, SortColumn] | None = None
) -> str:
    """`sorted by capacity, largest first` for the table caption and the count line."""
    parsed = _parse(current)
    if parsed is not None:
        for col in (*columns.values(), *(other or {}).values()):
            if col.field == parsed[0]:
                return f"sorted by {col.noun}, {col.words(parsed[1])}"
    return "sorted as requested"


def _href(path: str, qp: QueryParams, token: str) -> str:
    kept = [(k, v) for k, v in qp.multi_items() if k not in ("sort", "cursor") and v != ""]
    kept.append(("sort", token))
    return f"{path}?{urlencode(kept)}"


def sort_headers(
    path: str, qp: QueryParams, columns: Mapping[str, SortColumn], current: str
) -> dict[str, dict[str, Any]]:
    """For each heading: `href` (the view sorted by it: its first order, or the reverse when it is
    the current sort), `direction` (`asc`, `desc`, or `None` when another column sorts the view),
    `aria_sort` and `hint` (the order a click gives, in words, for the tooltip)."""
    parsed = _parse(current)
    out: dict[str, dict[str, Any]] = {}
    for key, col in columns.items():
        direction = parsed[1] if parsed is not None and parsed[0] == col.field else None
        click = ("asc" if direction == "desc" else "desc") if direction else col.first
        out[key] = {
            "href": _href(path, qp, _token(col.field, click)),
            "direction": direction,
            "aria_sort": {"asc": "ascending", "desc": "descending"}.get(direction or ""),
            "hint": f"Sort by {col.noun}, {col.words(click)}",
        }
    return out


__all__ = [
    "ASSET_COLUMNS",
    "ASSET_OTHER",
    "OPPORTUNITY_COLUMNS",
    "OPPORTUNITY_OTHER",
    "POINT_COLUMNS",
    "PROPOSAL_COLUMNS",
    "PROPOSAL_OTHER",
    "SortColumn",
    "chosen_sort",
    "column",
    "sort_caption",
    "sort_headers",
    "valid_sort",
]
