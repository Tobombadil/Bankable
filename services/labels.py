"""Vocabulary tokens as reader-facing words, shared by the site and the services (2026-10-07,
social lane; audit content F14).

The technology and lifecycle tables moved here from `web/labels.py` unchanged, because the social
drafting worker runs in the `worker` image, which ships `services/` but not `web/`
(`infra/docker/Dockerfile`), and its posts printed raw tokens (`150 MW bess_li_ion`, `status
permitted → under_construction`). `web/labels.py` imports these names, so the site and the posts
read one table; every other table (kinds, organisation types, attribute keys, ...) stays in
`web/labels.py`, which owns the rules for using them (its module docstring). `web/test_labels.py`
still fails when a vocabulary token lacks an entry here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The proposal `technology` classes (`pipeline.normalize.TECH_RULES` plus its `unknown`/`other`
#: fallbacks), the opportunity classes (`pipeline.connectors.opportunity.TECH_KEYWORDS`) and the
#: asset `technology` values the context loaders write (pipeline type, storage field type, LNG
#: function, RNG family). One table, because one token (`hydro`, `wind`) means the same thing on
#: every surface.
TECHNOLOGY_LOAD_LABEL = "Large load"
TECHNOLOGY_LABELS: dict[str, str] = {
    # proposals and power plants
    "solar": "Solar",
    "solar_storage": "Solar + storage",
    "solar_thermal": "Solar thermal",
    "wind": "Wind",
    "wind_storage": "Wind + storage",
    "wind_offshore": "Offshore wind",
    "storage": "Storage",
    "pumped_storage": "Pumped hydro storage",
    "hydro": "Hydro",
    "marine": "Tidal and wave",
    "nuclear": "Nuclear",
    "gas_cc": "Gas, combined cycle",
    "gas_ct": "Gas, combustion turbine",
    "gas_ice": "Gas, reciprocating engine",
    "gas_steam": "Gas, steam turbine",
    "gas_other": "Gas, other",
    "fuel_cell": "Fuel cell",
    "hydrogen": "Hydrogen",
    "geothermal": "Geothermal",
    "biomass": "Biomass",
    "waste": "Waste to energy",
    "coal": "Coal",
    "oil": "Oil",
    "transmission": "Transmission",
    # `ccs`: EPA Class VI wells (us.epa.class_vi). "CO2", not "CO₂": the self-hosted IBM Plex subset has
    # no U+2082, so a subscript would print in the fallback face (docs/31 §4).
    "co2_geologic_sequestration": "CO2 geologic sequestration",
    # `load`: the data-centre and large-load connectors set technology to the kind. A short label,
    # because the kind label ("Load (data centres, large loads)") wraps every list row.
    "load": TECHNOLOGY_LOAD_LABEL,
    "other": "Other",
    "unknown": "Not stated",
    # opportunities (their own vocabulary, frontend F2)
    "solar_pv": "Solar PV",
    "bess": "Battery storage",
    "ccs": "Carbon capture (CCS)",
    "gas": "Natural gas",
    "heat": "Heat networks and heat pumps",
    "ev_charging": "EV charging",
    "efficiency": "Energy efficiency",
    "microgrid": "Microgrids and off-grid",
    "metering": "Smart metering",
    # existing assets other than power plants
    "ethanol": "Fuel ethanol",
    "gathering": "Gathering",
    "interstate": "Interstate",
    "intrastate": "Intrastate",
    "aquifer": "Aquifer",
    "depleted_field": "Depleted field",
    "salt_dome": "Salt dome",
    "import": "Import",
    "export": "Export",
    "import_export": "Import and export",
    # RNG families (us.epa.lmop, us.epa.agstar)
    "lfg_electricity": "Landfill gas to electricity",
    "lfg_direct_use": "Landfill gas direct use",
    "rng": "Renewable natural gas",
    "farm_digester": "Farm digester",
}

#: Proposal lifecycle states and opportunity statuses: the keys of
#: data/vocabulary/lifecycle_states.yaml (`lifecycle_states`, `opportunity_statuses`). The two
#: vocabularies share `unknown`, `announced` and `cancelled`; the words are the same in both.
LIFECYCLE_LABELS: dict[str, str] = {
    "unknown": "Unknown",
    "announced": "Announced",
    "filed": "Filed",
    "studied": "Studied",
    "permitted": "Permitted",
    "contracted": "Contracted",
    "under_construction": "Under construction",
    "built": "Built",
    "withdrawn": "Withdrawn",
    "cancelled": "Cancelled",
    "open": "Open",
    "frozen": "Frozen",
    "reinstated": "Reinstated",
    "closed": "Closed",
    "awarded": "Awarded",
}


def humanise(token: str) -> str:
    """Words for a token no table names: underscores to spaces, first letter capitalised. The
    safety net described in the module docstring; never the intended path for a vocabulary token."""
    text = str(token).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _from(table: Mapping[str, str], token: Any) -> str | None:
    if token is None or token == "":
        return None
    key = str(token)
    return table.get(key) or humanise(key)


def technology_label(token: Any) -> str | None:
    return _from(TECHNOLOGY_LABELS, token)


def lifecycle_label(token: Any) -> str | None:
    return _from(LIFECYCLE_LABELS, token)
