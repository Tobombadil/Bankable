"""One way to print a number or a date on every page (docs/31 §4; audit 2026-10-07 UX-8).

Before this module the same capacity printed four ways ("4800.0" on `/proposals`, "6809.0 MW" on
`/assets`, "2,000.0" on a grid-point page, "1150.0" beside "1,150.0 MW active" on one proposal
page) and every public date was ISO while docs/31 §4 asks for "11 Sep 2026". The filters here are
registered on every Jinja environment by `web.labels.install`, so a template has one spelling:

- `thousands`: an integer count with thousands separators (`1,844`).
- `mw`: a capacity value with thousands separators and at most one decimal, the decimal dropped
  when it is zero (`4,800`, `1,150.5`); the unit belongs in the column header, never here.
- `display_date`: an absolute date in the docs/31 §4 form (`11 Sep 2026`; `Sep 2026` for a
  year-month, the year alone for a year). Text that is not a date passes through unchanged.
- `rfc3339`: an admin timestamp to the second in UTC (`2026-10-07T18:23:12Z`, docs/23 §1),
  never with microseconds.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ISO_DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?(?:[T ].*)?$")


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def thousands(value: Any) -> str:
    """`1844` -> `1,844`. Anything that is not a number prints as itself."""
    number = _number(value)
    if number is None:
        return "" if value is None else str(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.1f}"


def mw(value: Any) -> str:
    """A capacity: thousands separators, one decimal at most, no trailing `.0` (docs/31 §4)."""
    number = _number(value)
    if number is None:
        return "" if value is None else str(value)
    rounded = round(number, 1)
    if rounded.is_integer():
        return f"{int(rounded):,}"
    return f"{rounded:,.1f}"


def display_date(value: Any) -> str:
    """`2026-09-11` (or a timestamp on that day) -> `11 Sep 2026` (docs/31 §4)."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"
    text = str(value).strip()
    match = _ISO_DATE.match(text)
    if not match:
        return text
    year, month, day = match.groups()
    if month and not 1 <= int(month) <= 12:
        return text
    if day:
        return f"{int(day)} {_MONTHS[int(month) - 1]} {year}"
    if month:
        return f"{_MONTHS[int(month) - 1]} {year}"
    return year


def rfc3339(value: Any) -> str:
    """An admin timestamp in RFC 3339 UTC to the second: `2026-10-07T18:23:12Z`."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    else:
        text = str(value).strip().replace(" ", "T", 1)
        if "T" not in text:  # a date alone stays a date; midnight would be invented precision
            return text
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return str(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def install(env: Any) -> None:
    env.filters["thousands"] = thousands
    env.filters["mw"] = mw
    env.filters["display_date"] = display_date
    env.filters["rfc3339"] = rfc3339
