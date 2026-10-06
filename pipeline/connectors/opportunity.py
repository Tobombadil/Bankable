"""Shared helpers for demand-side (opportunity) connectors — docs/21 §3.3 and §7.2."""

from __future__ import annotations

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
