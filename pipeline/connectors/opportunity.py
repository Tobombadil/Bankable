"""Shared helpers for demand-side (opportunity) connectors — docs/21 §3.3 and §7.2."""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import re
from typing import Any

import pandas as pd

OPPORTUNITY_STATES = (
    "unknown",
    "announced",
    "open",
    "frozen",
    "reinstated",
    "closed",
    "cancelled",
    "awarded",
)
KINDS = ("rfp", "foa", "tender", "auction", "loan_program", "procurement_notice", "program")

# Ordered keyword rules over notice titles/descriptions -> normalised technology tokens.
# Deliberately conservative: an all-source notice keeps an empty list (docs/21 §3.3).
TECH_KEYWORDS: list[tuple[str, str]] = [
    (r"offshore\s*wind", "wind_offshore"),
    (r"\bwind\b|éolien|eolic|wiatrow|windkraft|windenergie", "wind"),
    (r"photovolta|\bsolar\b|\bpv\b|fotowolta|solaire|solare|fotovolta", "solar_pv"),
    (r"battery|\bbess\b|energy storage|stockage|batter[iy]|magazyn", "bess"),
    (r"pumped[\s-]*storage|pumped hydro", "pumped_storage"),
    (r"hydro(?:electric|power)?\b|hydraul|wasserkraft", "hydro"),
    (r"\btidal\b|wave energy|marine energy|hydrokinetic", "marine"),
    (r"nuclear|nucléaire|nucleare|jądrow|kernkraft|\bsmr\b", "nuclear"),
    (r"hydrogen|hydrogène|wasserstoff|wodor|electrolys", "hydrogen"),
    (r"geotherm|géotherm", "geothermal"),
    (r"biomass|biogas|biomethane|biométhane|biogaz|anaerobic", "biomass"),
    (r"carbon capture|\bccs\b|\bccus\b|carbon dioxide removal", "ccs"),
    (r"natural gas|\bgas\b|gaz naturel|erdgas|combined[\s-]cycle|\blng\b|gasoduc|pipeline", "gas"),
    (
        r"transmission|substation|grid|\bhv\b|high[\s-]voltage|électrique|sieci|netz|interconnect"
        r"|distribution line",
        "transmission",
    ),
    (r"district heat|heat network|chaleur|fernwärme|heat pump|ciepł", "heat"),
    (r"electric vehicle|\bev\b|charging|recharge|ładowa", "ev_charging"),
    (r"energy efficien|efficacité énergétique|retrofit|insulation|termomodern", "efficiency"),
    (r"microgrid|mini[\s-]grid|off[\s-]grid|rural electrification", "microgrid"),
    (r"smart meter|metering|\bami\b", "metering"),
]

#: Every technology token an opportunity can carry, in rule order: the vocabulary the opportunity
#: technology filter offers (`GET /v1/meta/vocabularies` `opportunity_technology`). It is not the
#: proposal vocabulary (`pipeline.normalize.TECH_RULES`): opportunities say `solar_pv`, `bess`,
#: `gas`, `heat`, ... and the opportunities page used to offer the proposal tokens, which match no
#: tagged notice (audit 2026-09-30, frontend F2).
OPPORTUNITY_TECHNOLOGIES: tuple[str, ...] = tuple(dict.fromkeys(token for _, token in TECH_KEYWORDS))

# ISO 3166-1 alpha-3 -> alpha-2 for the buyer countries TED and MDB notices use.
ISO3_TO_ISO2: dict[str, str] = {
    "AUT": "AT",
    "BEL": "BE",
    "BGR": "BG",
    "HRV": "HR",
    "CYP": "CY",
    "CZE": "CZ",
    "DNK": "DK",
    "EST": "EE",
    "FIN": "FI",
    "FRA": "FR",
    "DEU": "DE",
    "GRC": "GR",
    "HUN": "HU",
    "IRL": "IE",
    "ITA": "IT",
    "LVA": "LV",
    "LTU": "LT",
    "LUX": "LU",
    "MLT": "MT",
    "NLD": "NL",
    "POL": "PL",
    "PRT": "PT",
    "ROU": "RO",
    "SVK": "SK",
    "SVN": "SI",
    "ESP": "ES",
    "SWE": "SE",
    "NOR": "NO",
    "ISL": "IS",
    "LIE": "LI",
    "CHE": "CH",
    "GBR": "GB",
    "UKR": "UA",
    "MDA": "MD",
    "SRB": "RS",
    "MKD": "MK",
    "MNE": "ME",
    "ALB": "AL",
    "BIH": "BA",
    "TUR": "TR",
    "GEO": "GE",
    "ARM": "AM",
    "USA": "US",
    "CAN": "CA",
    "AUS": "AU",
    "NZL": "NZ",
    "JPN": "JP",
    "KOR": "KR",
    "IND": "IN",
    "CHN": "CN",
    "BRA": "BR",
    "ZAF": "ZA",
    "XKX": "XK",
    "1A0": "XK",
}


def classify_technologies(*texts: Any) -> list[str]:
    blob = " ".join(
        str(t) for t in texts if t is not None and not (isinstance(t, float) and pd.isna(t))
    ).lower()
    found: list[str] = []
    for pattern, token in TECH_KEYWORDS:
        if re.search(pattern, blob) and token not in found:
            found.append(token)
    return found


def iso2(country: Any) -> str | None:
    if country is None or (isinstance(country, float) and pd.isna(country)):
        return None
    c = str(country).strip().upper()
    if len(c) == 2:
        return c
    return ISO3_TO_ISO2.get(c, c[:2] if c else None)


def to_utc(v: Any, fmt: str | None = None) -> pd.Timestamp | None:
    """Parse a date/datetime; naive values are taken as UTC (docs/04 E-4)."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return None
    try:
        ts = pd.to_datetime(v, format=fmt, errors="coerce", utc=False)
    except (ValueError, TypeError):
        return None
    if ts is None or pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    utc: pd.Timestamp = ts.tz_convert("UTC")
    return utc


def deadline_passed(due: pd.Timestamp | None, now: dt.datetime) -> str:
    """'yes'/'no'/'' — a string so status_map refine rules can match it."""
    if due is None:
        return ""
    return "yes" if due < pd.Timestamp(now).tz_convert("UTC") else "no"


#: `status_rule` of a row `close_past_deadline` moved from `open` to `closed`.
DEADLINE_PASSED_RULE = "opportunity.deadline_passed"


def close_past_deadline(df: pd.DataFrame, now: dt.datetime) -> tuple[pd.DataFrame, int]:
    """Every `open` row whose `due_at` is before `now` becomes `closed` (docs/21 §7.2: "open -->
    closed: deadline passed"; `closed` is "deadline passed, outcome unknown"), with `status_rule`
    `DEADLINE_PASSED_RULE`. Returns the frame and how many rows moved.

    Each connector already applies this at fetch time through its status map's refine rule (TED
    `ted.competition_deadline_passed`, Find a Tender, World Bank, grants.gov). The runner applies it
    again to the whole frame at the run's `retrieved_at`, because an incremental source carries
    earlier rows forward unchanged: a notice fetched while open and never re-fetched stayed `open`
    after its deadline (audit 2026-09-30: 90 of 405 stored `open` notices had a past `due_at`)."""
    if "status" not in df.columns or "due_at" not in df.columns or not len(df):
        return df, 0
    due = pd.to_datetime(df["due_at"], errors="coerce", utc=True)
    cutoff = pd.Timestamp(now)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    moved = (df["status"].astype("string") == "open").fillna(False).to_numpy() & (due < cutoff).fillna(
        False
    ).to_numpy()
    if not moved.any():
        return df, 0
    out = df.copy()
    out["status"] = out["status"].astype("object")
    out.loc[moved, "status"] = "closed"
    if "status_rule" in out.columns:
        out["status_rule"] = out["status_rule"].astype("object")
        out.loc[moved, "status_rule"] = DEADLINE_PASSED_RULE
    return out, int(moved.sum())


def technologies_str(tokens: list[str]) -> str:
    return "|".join(tokens)


def known_budget(amount: float | int | None) -> float | None:
    """A stated budget as a float, or `None` when it is unknown.

    Zero and negative amounts are placeholders, not budgets: TED states `estimated-value-glo` as 0
    or -1 on notices that give no value (9 of the 279 TED notices with an amount in the 2026-09-13
    dev snapshot: 8 at 0 EUR, 1 at -1 PLN). Stored as numbers they sorted first on an ascending
    budget sort and printed "-1 PLN". As `None` they take the API's null ordering (last, both ways)
    and the page's dash. The notice's stated currency is left alone (docs/00 2026-09-27, lane E16:
    one currency per budget sort), and `raw` keeps the source's own value. Lane I3, 2026-09-29."""
    if amount is None or isinstance(amount, bool):
        return None
    value = float(amount)
    return value if math.isfinite(value) and value > 0 else None


def empty_opportunity(n: int) -> dict[str, list[Any]]:
    return {c: [None] * n for c in ("summary", "capacity_sought_mw", "budget_amount", "budget_currency")}


# ------------------------------------------------------------------------------- stated values
#: What a notice's stated amount is, in the words the page prints beside it (lane rfp-value,
#: 2026-10-22). A bare "budget" misleads both ways: a TED result notice's total is what was
#: awarded, not what is on offer, and a Grants.gov award ceiling is the most one award can be, not
#: the programme's size. Each connector's `stated_value(row)` names one of these per figure; the
#: amount is always the source's own number, never derived from another figure.
VALUE_KINDS: dict[str, str] = {
    "estimated_value": "Estimated value",
    "estimated_value_incl_vat": "Estimated value including VAT",
    "awarded_value": "Total awarded value",
    "contract_value": "Contract value",
    "total_programme_funding": "Estimated total programme funding",
    "award_ceiling": "Award ceiling",
    "award_floor": "Award floor",
}
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
#: The largest signed and unsigned 32-bit integers: what a form's numeric field holds when someone
#: typed past its maximum. Grants.gov 363912 states `estimatedFunding` 2147483647 (2026-10-22).
_FIELD_MAXIMA = frozenset({2_147_483_647.0, 4_294_967_295.0})
#: Below this a "value" is a unit price or a placeholder, not what a contract or programme is worth:
#: on 2026-10-22 the store's 14 amounts under 100 were electricity and fuel unit prices (EUR 0.01,
#: 0.26, 1.86, 3.28, 9.10), five notices stating 1, and two stating 2.
PLACEHOLDER_FLOOR = 100.0
#: The shortest run of nines read as a placeholder: 999,999,999 (Grants.gov 361080's award ceiling,
#: 2026-10-22, the only run of nines among the store's and the live Grants.gov figures). A shorter
#: run is an ordinary figure set just under a threshold ($999,999, $9,999,999) and is ranked.
NINES_MIN_DIGITS = 9


def placeholder_reason(amount: float | None) -> str | None:
    """Why a stated amount is not ranked as a value, or None when it is (lane rfp-value). The
    amount stays the notice's own and is shown as stated; only the USD equivalent that orders the
    list is withheld (`services/ingest/opportunity_value.py`). Rules: a 32-bit field maximum, a run
    of `NINES_MIN_DIGITS` or more nines, or less than `PLACEHOLDER_FLOOR` in any currency. Nothing
    else is judged: an implausible but ordinary-looking figure is ranked as stated."""
    if amount is None:
        return None
    if amount in _FIELD_MAXIMA:
        return "the largest number the source's field holds, not a figure"
    digits = str(int(amount)) if amount == int(amount) else ""
    if len(digits) >= NINES_MIN_DIGITS and set(digits) == {"9"}:
        return "a run of nines, a placeholder rather than a figure"
    if amount < PLACEHOLDER_FLOOR:
        return f"under {PLACEHOLDER_FLOOR:,.0f}, a unit price or placeholder rather than a total"
    return None


def parse_amount(value: Any) -> float | None:
    """A stated amount from a JSON number, a numeric string (`"35400000"`, `"3,290,325.87"`) or a
    one-element list of either (TED's `estimated-value-glo` is `["3290325.87"]`), through
    `known_budget`: 0 and negatives are the sources' placeholders for "not stated"."""
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else None
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if not isinstance(value, int | float):
        return None
    return known_budget(value)


def currency_code(value: Any) -> str | None:
    """An ISO 4217 code as the store keeps it (three upper-case letters), else None. A list takes
    its first element (TED's `total-value-cur` is `["RON"]`)."""
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if _CURRENCY_RE.fullmatch(code) else None


@dataclasses.dataclass(frozen=True)
class StatedFigure:
    """One amount a notice states: what it is (`VALUE_KINDS`), the number, its currency and the
    source field it was read from. `may_lead` is False for a figure that is listed but never the
    notice's value: an award floor (the least one award can be), or one of several awards (a lot's
    value is not the procurement's)."""

    kind: str
    amount: float
    currency: str | None
    source_field: str
    may_lead: bool = True

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "amount": self.amount,
            "currency": self.currency,
            "source_field": self.source_field,
        }


@dataclasses.dataclass(frozen=True)
class StatedValue:
    """A notice's value: the `primary` figure (it becomes `budget_amount`/`budget_currency`, and its
    USD equivalent is what the list sorts on), the other figures the same notice states, and, for
    figures read from a different response than the record's own row (Grants.gov's opportunity
    detail), when and from where they were read."""

    primary: StatedFigure
    also_stated: tuple[StatedFigure, ...] = ()
    expected_awards: int | None = None
    retrieved_at: str | None = None
    endpoint: str | None = None

    @property
    def amount(self) -> float:
        return self.primary.amount

    @property
    def currency(self) -> str | None:
        return self.primary.currency

    def identifiers(self) -> dict[str, Any]:
        """`identifiers["value"]` as a connector writes it; the loader adds the USD equivalent
        (`services/ingest/opportunity_value.py`)."""
        out: dict[str, Any] = {"kind": self.primary.kind, "source_field": self.primary.source_field}
        if self.also_stated:
            out["also_stated"] = [f.as_json() for f in self.also_stated]
        if self.expected_awards is not None:
            out["expected_awards"] = self.expected_awards
        if self.retrieved_at:
            out["retrieved_at"] = self.retrieved_at
        if self.endpoint:
            out["endpoint"] = self.endpoint
        return out


def _repeat_key(figure: StatedFigure) -> tuple[Any, ...]:
    """Two fields stating the same figure are one figure (a TED estimate repeated as its total);
    two items of a list (Find a Tender's `awards[0]` and `awards[1]`) are two figures, even at the
    same amount."""
    item = figure.source_field if "[" in figure.source_field else None
    return (figure.kind, figure.amount, figure.currency, item)


def stated_value_of(figures: list[StatedFigure], **extra: Any) -> StatedValue | None:
    """The notice's value from its figures, in the connector's order of precedence, without exact
    repeats; None for no figures. The primary is the first figure that may lead and is not a
    placeholder (`placeholder_reason`), so a notice stating a real ceiling beside a placeholder
    total (Grants.gov 363912: 2,147,483,647 and 250,000,000) is ranked on the ceiling, labelled as
    one. When every figure that may lead is a placeholder, the first of them stays primary, shown
    as stated with its note. The others, in order, are `also_stated`."""
    unique: list[StatedFigure] = []
    seen: set[tuple[Any, ...]] = set()
    for f in figures:
        key = _repeat_key(f)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    if not unique:
        return None
    leads = [f for f in unique if f.may_lead] or unique
    primary = next((f for f in leads if placeholder_reason(f.amount) is None), leads[0])
    return StatedValue(primary=primary, also_stated=tuple(f for f in unique if f is not primary), **extra)
