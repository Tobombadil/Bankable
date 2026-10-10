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


#: Words of four letters or fewer that are words, not acronyms, in register names ("WIND FARM",
#: "BLUE SKY", "LA CASA", "SAN JUAN"). Any other token that short is kept in capitals, because in
#: an all-capitals register name it is usually an acronym (BESS, SLF, LLC, KCE, TX, NRG). A short
#: word missing here stays in capitals, which is the name as filed, never a wrong word.
_SHORT_WORDS = frozenset(
    """
    a able ace acre aero age air all alta alto ana and ant apex arc arch area ark arm art ash at ave axe bank
    bar bay beam bear bee bell belt bend best big bio bird bit blue boat bold bolt bond bone book boot bow box
    boy brad bull burn bush by cal camp cane cap cape car card care casa cash cat cave cell cid city clay club
    coal cobb cold cole cone cook cool cop cord core corn cost cove cow crab crow cub cup cut dam dark data
    dawn day de deal deep deer del den dew dial dome dos dove down draw drew dry duck dune dust each east echo
    eco edge el elk elm end ever eye fair fall farm fast fawn fern fig fir fire fish five flat flex flow fly
    foam fog for ford fork form fort four fox free frog fry full fund gain gale game gap gas gate gem gen gila
    glen glow goal goat gold golf good gray grid gulf gum gun hall hand hare hart hat hawk hay head heat helm
    hen high hill hive hog hole holt home hood hook hope horn host hub hull hut ice in inc inca iron isle ivy
    jack jade java jay jean jet joy just keel keen keep kent key keys king kit kite knob la lab labs lake lamb
    land lane lark las lava law lead leaf lee left leo lily lime line link lion live lock loco log loma lone
    long loop lord los lost lot luna lux lynx main mall man mann many map mar mark mast max may mead mesa mid
    mile mill mine mink mint mira mist mod mole moon moor moss most moth mule nest net new nido nine noon nova
    oak oaks oat of off oil old on one onyx opal oro oso out owl own ox pace pack palm palo park pass paw
    paz peak pear peel pen pico pier pike pine pink pit plum plus pole pond pool port post pot puff puma pump
    pure quay rail rain ram ray rays red reed reef rey rice rich rim ring rio rise road rock roe rojo roof
    rook room root rosa rose rosy ruby run rush rye safe sage sal salt san sand sea seal seco seed set shoe
    shop side silo six sky snow soda sol son spa span spur star stem step sun sur swan swap tail tall tank tea
    teal team tech ten tern the tide tie tin to toad top tor tow town toy tree tres trim troy true tule twin
    two una unit up vale van vega vest via view vine wade wall ward warm wash watt wave way weir well west
    wet whey wild will wind wing wire wish wolf wood wool yard yew yolo zeta zeus
    """.split()
)
#: Short words set in lower case after the first word ("Bank of the West").
_LOWER_AFTER_FIRST = frozenset({"and", "at", "by", "for", "in", "of", "on", "the", "to"})
#: Abbreviations a reader expects in mixed case rather than capitals.
_ABBREVIATIONS = {"INC": "Inc", "LTD": "Ltd", "CORP": "Corp", "BROS": "Bros"}
#: Longer acronyms that must not become a word ("Ercot").
_LONG_ACRONYMS = frozenset({"CAISO", "ERCOT", "ISONE", "LADWP", "NYISO", "NYSEG", "NYSERDA", "USACE"})
_ROMAN = re.compile(r"^(?=[IVX]+$)X{0,3}(?:IX|IV|V?I{0,3})$")
_LETTER_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]+(?![A-Za-z0-9])")


def _readable_word(word: str, *, first: bool, after_apostrophe: bool) -> str:
    upper = word.upper()
    if len(word) == 1:
        # An initial ("R.E.", "UNIT A") stays a capital; the "s" of a possessive does not.
        return word.lower() if after_apostrophe and upper in ("S", "T", "D") else word
    if upper in _ABBREVIATIONS:
        return _ABBREVIATIONS[upper]
    if _ROMAN.match(upper) or upper in _LONG_ACRONYMS:
        return word
    lower = word.lower()
    if len(word) <= 4 and lower not in _SHORT_WORDS:
        return word  # an acronym of four letters or fewer is kept as filed
    if not first and lower in _LOWER_AFTER_FIRST:
        return lower
    return word[0].upper() + word[1:].lower()


def readable_name(value: Any) -> str:
    """A register name filed in capitals, in a case a reader can scan: `TRENT WIND FARM LLC` ->
    `Trent Wind Farm LLC`, `BRP OCTANS BESS LLC` -> `BRP Octans BESS LLC` (review 2026-10-10
    §2.6 item 7). Only a name with no lower-case letter is changed, so a name its register already
    sets in mixed case is printed exactly as filed. Acronyms of four letters or fewer stay in
    capitals unless they are an ordinary word (`_SHORT_WORDS`), and so do Roman numerals and
    letters joined to digits (`TX16`, `CO2`). For display only: search, URLs and the stored name
    are unchanged, and the page shows the name as filed once beside it."""
    if value is None:
        return ""
    text = str(value)
    if not text or any(ch.islower() for ch in text):
        return text
    out: list[str] = []
    last = 0
    for index, match in enumerate(_LETTER_RUN.finditer(text)):
        start = match.start()
        out.append(text[last:start])
        out.append(
            _readable_word(
                match.group(0),
                first=index == 0,
                after_apostrophe=start > 0 and text[start - 1] in "'’",
            )
        )
        last = match.end()
    out.append(text[last:])
    return "".join(out)


def install(env: Any) -> None:
    env.filters["thousands"] = thousands
    env.filters["mw"] = mw
    env.filters["display_date"] = display_date
    env.filters["rfc3339"] = rfc3339
    env.filters["readable_name"] = readable_name
