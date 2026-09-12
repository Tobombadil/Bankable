"""Shared helpers for demand-side (opportunity) connectors — docs/21 §3.3 and §7.2."""

from __future__ import annotations

import datetime as dt
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


def technologies_str(tokens: list[str]) -> str:
    return "|".join(tokens)


def empty_opportunity(n: int) -> dict[str, list[Any]]:
    return {c: [None] * n for c in ("summary", "capacity_sought_mw", "budget_amount", "budget_currency")}
