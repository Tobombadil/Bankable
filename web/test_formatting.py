"""One number and date format on every page (docs/31 §4; audit 2026-10-07 UX-8).

Before: one capacity printed as "4800.0" (`/proposals`), "6809.0 MW" (`/assets`), "2,000.0"
(grid-point page) and "1150.0" beside "1,150.0 MW active" (one proposal page); every public date
was ISO against docs/31 §4's "11 Sep 2026"; admin timestamps carried microseconds.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from web.formatting import display_date, mw, rfc3339, thousands

WEB = Path(__file__).resolve().parent


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (4800.0, "4,800"),
        (1150, "1,150"),
        (13365.94, "13,365.9"),
        (0.04, "0"),
        (2.25, "2.2"),
        ("12.5", "12.5"),
    ],
)
def test_mw_has_separators_one_decimal_and_no_trailing_zero(value: object, expected: str) -> None:
    assert mw(value) == expected


def test_absent_or_odd_values_never_become_a_number() -> None:
    assert mw(None) == "" and thousands(None) == ""
    assert mw("n/a") == "n/a"
    assert thousands(1844) == "1,844"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-11", "11 Sep 2026"),
        ("2026-09-13T18:23:12Z", "13 Sep 2026"),
        ("2026-12", "Dec 2026"),
        ("2030", "2030"),
        (dt.date(2026, 1, 5), "5 Jan 2026"),
        ("unknown", "unknown"),
        ("2026-13-01", "2026-13-01"),
    ],
)
def test_display_date_is_the_docs31_form(value: object, expected: str) -> None:
    assert display_date(value) == expected


def test_rfc3339_drops_microseconds_and_keeps_utc() -> None:
    assert rfc3339("2026-10-07T18:23:12.876312Z") == "2026-10-07T18:23:12Z"
    assert rfc3339("2026-10-07 18:23:12.876312") == "2026-10-07T18:23:12Z"
    assert rfc3339("2026-10-07T20:23:12+02:00") == "2026-10-07T18:23:12Z"
    assert rfc3339("2026-11-06") == "2026-11-06"  # a date alone gains no invented midnight
    assert rfc3339(None) == ""


def test_public_templates_no_longer_hand_format_capacity() -> None:
    """The list, search, asset, company and opportunity templates print capacity through `mw`.
    (The proposal page's field grid is the one exception left until PR #64 merges; see docs/31 §4.)"""
    offenders = []
    for path in (WEB / "templates").rglob("*.html"):
        if "admin" in path.parts or path.name == "proposal_detail.html":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"'%\.1f'\|format\((?:r|record|a|mbr)\.capacity", line):
                offenders.append(f"{path.name}:{number}")
    assert offenders == []
