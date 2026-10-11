"""One number and date format on every page (docs/31 §4; audit 2026-10-07 UX-8).

Before: one capacity printed as "4800.0" (`/proposals`), "6809.0 MW" (`/assets`), "2,000.0"
(grid-point page) and "1150.0" beside "1,150.0 MW active" (one proposal page); every public date
was ISO against docs/31 §4's "11 Sep 2026"; admin timestamps carried microseconds.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import re
import shutil
import subprocess
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


# ---- the map's capacity formatter is the server's `mw` (docs/31 §4) ----
#
# map.js printed capacity four ways: `toLocaleString` (rounds the shortest decimal half up: 2.25 ->
# "2.3", 0.35 -> "0.4"; -0.04 -> "-0"), the retired-plant rows' `toFixed(1) + " MW"` ("4800.0 MW"),
# and two tooltips' `Math.round(...).toLocaleString()` (whole MW, in the browser's own locale). It
# now has one formatter, `fmtMWNumber`, between two marker comments; node runs exactly that block.

MAP_JS = (WEB / "static" / "js" / "map.js").read_text(encoding="utf-8")
_JS_BLOCK = re.search(r"// ---- fmtMWNumber: begin.*?\n(.*?)// ---- fmtMWNumber: end ----", MAP_JS, re.S)

_FIXED_CASES: list[object] = [
    0, 1, 4800.0, 1150, 1150.5, 13365.94, 999.95, 999999.95, 1234567.89, 0.04, -0.04, -0.0, 0.05, 0.15,
    0.25, 0.35, 0.45, 0.75, 0.95, 0.96, 1.25, 2.25, 2.675, 2.75, 9.95, 99.95, -1234.56, -2.25, -0.06,
    5e-324, 2.0**53, 2.0**53 + 2, 1e16 + 0.5, 1e20, 1e21, 2.0**70, 1e300, math.inf, -math.inf, math.nan,
    "12.5", " 12.5 ", "1e3", "1E3", "12.", ".5", "+5", "-1234.56", "4800.0", "abc", "n/a", "", "-",
    None, True, False,
]  # fmt: skip


def _sample() -> list[object]:
    """Seeded: values with up to four decimals (decimal ties such as x.x5 that are not binary
    ties), exact binary ties (k/4), decimal halves of tenths (k/20) and raw doubles over 15
    orders of magnitude, each also negated."""
    rng = random.Random(20261010)  # noqa: S311 -- seeded test values, not a secret
    values: list[float] = []
    values += [round(rng.uniform(0, 10_000), rng.choice((1, 2, 3, 4))) for _ in range(600)]
    values += [rng.randrange(0, 40_000_000) / 4 for _ in range(300)]
    values += [rng.randrange(0, 2_000_000) / 20 for _ in range(300)]
    values += [rng.random() * 10 ** rng.randint(-3, 12) for _ in range(300)]
    return [*values, *(-v for v in values[::7])]


def _js_literal(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return repr(value)  # the shortest round-trip form: JS reads back the same double
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def test_the_map_formats_capacity_exactly_as_the_server_does() -> None:
    """Every value in the table and the seeded sample prints the same in map.js and in
    `web.formatting.mw`. The domain is what reaches map.js: JSON numbers and numeric strings,
    null, and the non-finite numbers arithmetic can make. Python-only spellings (`float("1_000")`,
    `float("inf")` from text) are outside it."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; test_map_prints_capacity_through_one_formatter holds the rule")
    assert _JS_BLOCK is not None, "the fmtMWNumber marker comments are missing from map.js"
    values = _FIXED_CASES + _sample()
    script = (
        _JS_BLOCK.group(1)
        + "\nprocess.stdout.write(JSON.stringify(["
        + ",".join(_js_literal(v) for v in values)
        + "].map(fmtMWNumber)));\n"
    )
    done = subprocess.run(  # noqa: S603 -- node from PATH, our own script on stdin
        [node, "-"], input=script, capture_output=True, text=True, timeout=60, check=True
    )
    printed = json.loads(done.stdout)
    mismatches = [(v, js, mw(v)) for v, js in zip(values, printed, strict=True) if js != mw(v)]
    assert mismatches == [], mismatches[:10]
    assert len(values) > 1700


def test_map_prints_capacity_through_one_formatter() -> None:
    """Without node: the only " MW" in map.js is `fmtMW`'s, and it is `fmtMWNumber`'s text, so no
    capacity can be printed another way (the retired rows' `toFixed(1) + " MW"`, the tooltips'
    `Math.round(...).toLocaleString() + " MW"`, the RNG drawer row's locale-dependent number)."""
    assert MAP_JS.count('" MW"') == 1
    assert 'function fmtMW(value) { return fmtMWNumber(value) + " MW"; }' in MAP_JS
    assert '"MW")' not in MAP_JS  # no capacity through the unit-word helper `quantity`
    old_ways = (
        '.toFixed(1) + " MW"',
        'toLocaleString() + " MW"',
        'toLocaleString("en-US", { maximumFractionDigits: 1',
    )
    for old in old_ways:
        assert old not in MAP_JS, old


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
