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

from web.formatting import display_date, mw, readable_name, rfc3339, thousands

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
    """Every public template prints capacity and storage through `mw`, the proposal page's field
    grid included since 2026-10-10 (review §2.6 item 7: "3200.0"), and no meta description joins
    the raw float to " MW" ("120.0 MW")."""
    offenders = []
    for path in (WEB / "templates").rglob("*.html"):
        if "admin" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"'%\.1f'\|format\((?:r|record|a|mbr)\.(?:capacity|storage)", line):
                offenders.append(f"{path.name}:{number}")
            if re.search(r"record\.capacity_mw ~", line):
                offenders.append(f"{path.name}:{number} (raw capacity in text)")
    assert offenders == []


# ---- register names filed in capitals (review 2026-10-10 §2.6 item 7) ----


@pytest.mark.parametrize(
    ("filed", "readable"),
    [
        ("TRENT WIND FARM LLC", "Trent Wind Farm LLC"),
        ("BRP OCTANS BESS LLC", "BRP Octans BESS LLC"),  # acronyms of four letters or fewer kept
        ("IP QUANTUM II, LLC", "IP Quantum II, LLC"),  # Roman numerals kept
        ("EDP RENEWABLES NORTH AMERICA LLC", "EDP Renewables North America LLC"),
        ("BAYWA R.E. SOLAR PROJECTS", "Baywa R.E. Solar Projects"),  # initials kept
        ("BANK OF THE WEST", "Bank of the West"),  # connecting words lower case after the first
        ("O'BRIEN SOLAR", "O'Brien Solar"),
        ("SMITH'S CREEK WIND", "Smith's Creek Wind"),
        ("CO2 STORAGE WELL 1", "CO2 Storage Well 1"),  # letters joined to digits untouched
        ("PG&E SOLAR", "PG&E Solar"),
        ("XYZ CORP INC", "XYZ Corp Inc"),
        ("ERCOT ROSCOE", "ERCOT Roscoe"),  # a known longer acronym is not made a word
        ("KCE TX 16, LLC", "KCE TX 16, LLC"),  # nothing to change
    ],
)
def test_a_name_filed_in_capitals_reads_in_a_readable_case(filed: str, readable: str) -> None:
    assert readable_name(filed) == readable


@pytest.mark.parametrize("name", ["Acme Solar", "NextEra Energy Resources", "eBay Data Center", "", "PJM"])
def test_a_name_with_lower_case_or_nothing_to_change_is_left_exactly_as_filed(name: str) -> None:
    assert readable_name(name) == name


def test_readable_name_of_nothing_is_empty() -> None:
    assert readable_name(None) == ""
