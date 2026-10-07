"""The one place the site turns a vocabulary token into the words a reader sees (audit 2026-09-30:
designer D-7, D-14; frontend F1).

Every technology, lifecycle state, opportunity status, kind, organisation type, reuse class and
asset attribute key a page prints comes from a table in this module. Before this module the only
technology label was `load`, so readers saw `gas_cc`, `solar_storage` and `wind_offshore` in
filter selects, list columns, detail pages and the map; attribute keys were printed with their
underscores swapped for spaces ("net generation mwh 2025", "match method oris_crosswalk").

Rules, in the order they bite:

* A token in a table renders as that table's words. Option *values* in forms and URLs stay tokens;
  only the visible text changes.
* A token no table names still never reaches a reader as itself: `humanise` turns it into words
  ("new_thing" -> "New thing"). `web/test_labels.py` fails when any token of a controlled
  vocabulary (data/vocabulary/lifecycle_states.yaml, the classifier's technology classes, the API's
  kind and type vocabularies, the asset statuses) lacks an entry, so the fallback is a safety net
  for a token added between a vocabulary change and its label, not a way of labelling.
* The tables live in Python, not in `data/vocabulary/`, because the web image ships `web/` and
  `data/sources.yaml` only (infra/docker/Dockerfile); a YAML file there would be absent in
  production. The vocabulary files stay the definitions; this module is their reader-facing names.
  The technology and lifecycle tables are defined in `services/labels.py` and re-exported here,
  because social posts print the same words from an image that has no `web/`.

`web/viewmodels.py` and `web/retirement.py` re-export the tables they always exported, so existing
imports keep working; the map receives the same tables as JSON (`map_labels`), so `map.js` names a
token exactly as the server does.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# The technology and lifecycle tables live in `services/labels.py` (2026-10-07), so the social worker,
# whose image has no `web/`, prints the same words; they are re-exported here unchanged.
from services.labels import LIFECYCLE_LABELS as LIFECYCLE_LABELS
from services.labels import TECHNOLOGY_LABELS as TECHNOLOGY_LABELS
from services.labels import TECHNOLOGY_LOAD_LABEL as TECHNOLOGY_LOAD_LABEL
from services.labels import _from
from services.labels import humanise as humanise
from services.labels import lifecycle_label as lifecycle_label
from services.labels import technology_label as technology_label

#: Proposal `kind` (docs/21 §3). The map's Kind select prints these.
PROPOSAL_KIND_LABELS: dict[str, str] = {
    "generation": "Generation",
    "storage": "Storage",
    "load": "Load (data centres, large loads)",
    "transmission": "Transmission",
    "pipeline": "Pipeline",
    "lng": "LNG",
    "nuclear": "Nuclear",
    "ccs": "Carbon capture (CCS)",
    "hydrogen": "Hydrogen",
    "other": "Other",
}

#: Opportunity `kind` (docs/21 §3.3).
OPPORTUNITY_KIND_LABELS: dict[str, str] = {
    "rfp": "Request for proposals (RFP)",
    "foa": "Funding opportunity (FOA)",
    "tender": "Tender",
    "auction": "Auction",
    "loan_program": "Loan programme",
    "procurement_notice": "Procurement notice",
    "program": "Programme",
}

#: Organisation `type` (`GET /v1/meta/vocabularies` `organization_type`).
ORGANIZATION_TYPE_LABELS: dict[str, str] = {
    "developer": "Developer",
    "ipp": "Independent power producer",
    "utility": "Utility",
    "coop": "Cooperative",
    "cca": "Community choice aggregator",
    "agency": "Public agency",
    "lender": "Lender",
    "investor": "Investor",
    "epc": "EPC contractor",
    "oem": "Equipment manufacturer",
    "offtaker": "Offtaker",
    "other": "Other",
}

#: `asset.status` (services/db/models.py ASSET_STATUSES); moved here from web/retirement.py.
ASSET_STATUS_LABELS: dict[str, str] = {
    "operating": "Operating",
    "standby": "Standby",
    "retiring": "Retiring",
    "retired": "Retired",
    "unknown": "Status not stated",
}

#: The eleven families the map draws existing power plants in (map.js `PLANT_FAMILY_CLASSES` groups
#: the technology classes into these). The plant-type select, the plants legend, the drawer and the
#: in-view rows all print these words; map.js reads them from `#map-labels`.
PLANT_FAMILY_LABELS: dict[str, str] = {
    "solar": "Solar",
    "wind": "Wind",
    "gas": "Gas",
    "oil": "Oil",
    "coal": "Coal",
    "nuclear": "Nuclear",
    "hydro": "Hydro",
    "storage": "Storage",
    "biomass": "Biomass / waste",
    "geothermal": "Geothermal",
    "other": "Other",
}

#: A licence's reuse class (services/db/models.py REUSE_CLASSES), as the provenance panel and the
#: map drawer print it.
REUSE_CLASS_LABELS: dict[str, str] = {
    "open": "Open licence",
    "attribution": "Open, credit required",
    "noncommercial": "Non-commercial licence",
    "restricted": "Restricted licence",
    "unknown": "Licence terms not yet recorded",
}

#: Report issue types (`POST /v1/reports`, services/api/admin_posts.py REPORT_ISSUE_TYPES).
REPORT_ISSUE_LABELS: dict[str, str] = {
    "wrong_merge": "Two different projects were merged into this record",
    "wrong_status": "The status is wrong",
    "wrong_sponsor": "The sponsor or owner is wrong",
    "other": "Something else",
}

#: Words for the asset `attributes` keys the loaders write (ADR 0008; inventory of the dev store on
#: 2026-10-06), with the unit in the label rather than in the key. A trailing `_YYYY` is a year
#: suffix (`net_generation_mwh_2025`), labelled as "<label>, 2025" (`attribute_label`).
ATTRIBUTE_LABELS: dict[str, str] = {
    # power plants (EIA-923)
    "capacity_factor": "Capacity factor",
    "fuel_mmbtu": "Fuel burned (MMBtu)",
    "heat_rate_btu_kwh": "Heat rate (Btu/kWh)",
    "net_generation_mwh": "Net generation (MWh)",
    # enrichment blocks
    "ghgrp": "EPA greenhouse gas reporting (GHGRP)",
    "rfs": "EPA Renewable Fuel Standard registration",
    "phmsa": "PHMSA operator annual report",
    # ethanol
    "as_of_year": "As of year",
    "atlas_operator_name": "Operator (EIA Energy Atlas)",
    "nameplate_capacity_mmgal_yr": "Nameplate capacity (MMgal/yr)",
    # pipelines
    "miles": "Length (miles)",
    "part_count": "Mapped parts",
    "segment_count": "Mapped segments",
    "pipeline_type": "Pipeline type",
    "states_crossed": "States crossed",
    "status_raw": "Status, as the source states it",
    "source_vintage": "Source release",
    # gas processing plants
    "btu_content": "Heat content (Btu per cubic foot)",
    "capacity_mmcfd": "Capacity (MMcf/d)",
    "plant_flow_mmcfd": "Plant flow (MMcf/d)",
    "dry_stor": "Dry gas storage capacity",
    "ngl_stor": "NGL storage capacity",
    "city": "City",
    "zip_code": "ZIP code",
    "period": "Source period",
    "operator_raw": "Operator, as the source states it",
    "owner_raw": "Owner, as the source states it",
    # gas storage
    "base_gas_mcf": "Base gas (Mcf)",
    "working_gas_capacity_mcf": "Working gas capacity (Mcf)",
    "total_field_capacity_mcf": "Total field capacity (Mcf)",
    "max_deliverability_mcfd": "Maximum deliverability (Mcf/d)",
    "field_type": "Field type",
    "field_code": "EIA field code",
    "reservoir": "Reservoir",
    "reservoir_code": "EIA reservoir code",
    "region": "Region",
    # LNG terminals
    "functions": "Functions",
    "liquefaction_bcfd": "Liquefaction capacity (Bcf/d)",
    "regasification_bcfd": "Regasification capacity (Bcf/d)",
    "storage_bcf": "Storage capacity (Bcf)",
    # RNG projects (LMOP, AgSTAR)
    "actual_mw_generation": "Actual generation (MW)",
    "awarded_usda_funding": "USDA funding awarded",
    "biogas_generation_estimate_cuft_day": "Estimated biogas generation (cu ft/day)",
    "cattle": "Cattle (head)",
    "dairy": "Dairy cows (head)",
    "poultry": "Poultry (head)",
    "swine": "Swine (head)",
    "electricity_generated_kwh_yr": "Electricity generated (kWh/yr)",
    "emission_reductions_avoided_mmtco2e_yr": "Avoided emissions (MMTCO2e/yr)",
    "emission_reductions_direct_mmtco2e_yr": "Direct emission reductions (MMTCO2e/yr)",
    "total_emission_reductions_mtco2e_yr": "Total emission reductions (MTCO2e/yr)",
    "landfill_closure_year": "Landfill closure year",
    "landfill_count": "Landfills",
    "landfill_design_capacity_tons": "Landfill design capacity (tons)",
    "landfill_lfg_collected_mmscfd": "Landfill gas collected (MMscf/d)",
    "landfill_lfg_generated_mmscfd": "Landfill gas generated (MMscf/d)",
    "landfill_waste_in_place_tons": "Waste in place (tons)",
    "landfill_waste_in_place_year": "Waste in place, year",
    "landfill_year_opened": "Landfill opened",
    "lcfs_pathway": "LCFS pathway",
    "lfg_flow_to_project_mmscfd": "Landfill gas flow to project (MMscf/d)",
    "project_start_year": "Project start year",
    "project_shutdown_year": "Project shutdown year",
    "rated_mw": "Rated capacity (MW)",
    "year_operational": "Year operational",
    "year_shutdown": "Year shut down",
    # transmission lines
    "voltage_class": "Voltage class",
    "voltage_kv": "Voltage (kV)",
    "hifld_line_id": "HIFLD line ID",
    "sub_1": "Endpoint substation 1",
    "sub_2": "Endpoint substation 2",
    "owner": "Owner",
    # GHGRP block
    "ghgrp_facility_id": "GHGRP facility ID",
    "frs_id": "EPA FRS ID",
    "reporting_year": "Reporting year",
    "naics_code": "NAICS code",
    "subparts": "Subparts reported",
    "subpart_rr": "Subpart RR (geologic sequestration)",
    "subpart_uu": "Subpart UU (CO2 injection)",
    "subpart_pp": "Subpart PP (CO2 supply)",
    "co2_captured": "CO2 captured",
    "rr_co2_sequestered_t": "CO2 sequestered, Subpart RR (t)",
    "rr_co2_sequestered_confidential": "Subpart RR quantity confidential",
    "rr_mrv_plan_url": "Subpart RR monitoring plan",
    "uu_co2_received_t": "CO2 received, Subpart UU (t)",
    "uu_co2_received_confidential": "Subpart UU quantity confidential",
    # RFS block
    "company_name": "Company",
    "facility_name": "Facility",
    "facility_type": "Facility type",
    "d_codes": "RIN D-codes",
    "pathway_count": "Approved pathways",
    "first_registered_year": "First registered",
    # PHMSA block
    "operator_id": "PHMSA operator ID",
    "operator_name": "Operator, as PHMSA names it",
    "report_year": "Report year",
    "onshore_transmission_miles": "Onshore transmission miles",
    "miles_by_decade": "Miles by decade installed",
    "miles_by_diameter": "Miles by diameter (inches)",
    "incident_years": "Years with an incident",
    "incidents_5y_total": "Incidents, last 5 years",
    "incidents_5y_significant": "Significant incidents, last 5 years",
    "incidents_5y_with_fatality": "Incidents with a fatality, last 5 years",
    "incidents_5y_with_injury": "Incidents with an injury, last 5 years",
    "incidents_5y_with_ignition": "Incidents with ignition, last 5 years",
    "incidents_5y_with_explosion": "Incidents with an explosion, last 5 years",
    "pre_1940": "Before 1940",
    "co_processing": "Co-processing",
    "certifications": "Certifications",
    "scheme": "Scheme",
    "current": "Current",
    "unknown": "Not stated",
}

#: Attribute keys that are pipeline bookkeeping rather than facts about the asset: how an
#: enrichment row was matched, which licence row it carries, duplicate raw spellings of a promoted
#: field, geometry counters. They are dropped from the public Attributes table (designer D-14:
#: "match method oris_crosswalk", "vertex count", "sub 1 raw"); the record's Sources panel carries
#: the provenance. `feature_flags` is not dropped: its sentences become data notes (`data_notes`).
INTERNAL_ATTRIBUTE_KEYS: frozenset[str] = frozenset(
    {
        "source_id",
        "source_url",
        "retrieved_at",
        "licence_id",
        "match_method",
        "match_score",
        "matched_on",
        "share_flag",
        "sources",
        "vertex_count",
        "length_miles_source",
        "lbnl_link_confidence",
        "sub_1_raw",
        "sub_2_raw",
        "feature_flags",
    }
)

#: Keys whose 0/1 values are yes/no flags in the source (AgSTAR), not counts.
BOOLEAN_ATTRIBUTE_KEYS: frozenset[str] = frozenset({"awarded_usda_funding", "lcfs_pathway", "co2_captured"})

#: Keys whose integer values are years, codes or identifiers: printed without thousands separators.
_PLAIN_NUMBER_KEY = re.compile(r"(year|period|code|_id$|^id$|zip|vintage)")
_YEAR_SUFFIX = re.compile(r"^(?P<base>.+)_(?P<year>(19|20)\d{2})$")


def proposal_kind_label(token: Any) -> str | None:
    return _from(PROPOSAL_KIND_LABELS, token)


def opportunity_kind_label(token: Any) -> str | None:
    return _from(OPPORTUNITY_KIND_LABELS, token)


def organization_type_label(token: Any) -> str | None:
    return _from(ORGANIZATION_TYPE_LABELS, token)


def reuse_class_label(token: Any) -> str | None:
    return _from(REUSE_CLASS_LABELS, token)


def asset_status_label(token: Any) -> str | None:
    return _from(ASSET_STATUS_LABELS, token)


def technologies_label(tokens: Any) -> str:
    """A list of technology tokens as one comma-joined phrase; pipe-joined legacy values are split
    (frontend F2: `solar_pv|nuclear` stored as one element)."""
    if not tokens:
        return ""
    items = [tokens] if isinstance(tokens, str) else list(tokens)
    out: list[str] = []
    for item in items:
        for part in str(item).split("|"):
            label = technology_label(part.strip())
            if label and label not in out:
                out.append(label)
    return ", ".join(out)


def attribute_label(key: Any) -> str:
    """The row label for one `attributes` key; `net_generation_mwh_2025` -> "Net generation (MWh),
    2025"."""
    text = str(key)
    if text in ATTRIBUTE_LABELS:
        return ATTRIBUTE_LABELS[text]
    match = _YEAR_SUFFIX.match(text)
    if match and match.group("base") in ATTRIBUTE_LABELS:
        return f"{ATTRIBUTE_LABELS[match.group('base')]}, {match.group('year')}"
    return humanise(text)


def _format_number(key: str, value: float) -> str:
    if _PLAIN_NUMBER_KEY.search(key):
        return str(int(value)) if float(value).is_integer() else str(value)
    if float(value).is_integer():
        return f"{int(value):,}"
    magnitude = abs(value)
    places = 1 if magnitude >= 100 else 2 if magnitude >= 1 else 3
    return f"{value:,.{places}f}"


def _display_value(key: str, value: Any) -> Any:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if key in BOOLEAN_ATTRIBUTE_KEYS and isinstance(value, (int, float)) and value in (0, 1):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return _format_number(key, value)
    if isinstance(value, Mapping):
        return labelled_attributes(value)
    if isinstance(value, (list, tuple)):
        return [_display_value(key, v) for v in value if v is not None]
    return value


def labelled_attributes(bag: Mapping[str, Any] | None, omit: Any = ()) -> dict[str, Any]:
    """An `attributes` bag as `{reader label: display value}`, in source order: internal keys and
    `omit` (fields the page already promoted) dropped, empty values skipped, nested blocks labelled
    the same way, numbers formatted. The Attributes table prints the keys as they come."""
    out: dict[str, Any] = {}
    for key, value in (bag or {}).items():
        if key in INTERNAL_ATTRIBUTE_KEYS or key in omit:
            continue
        if value is None or (not isinstance(value, (str, int, float, bool)) and not value and value != 0):
            continue
        if isinstance(value, str) and not value.strip():
            continue
        out[attribute_label(key)] = _display_value(key, value)
    return out


#: `<namespace>:<key>_null: why` and `<key> unavailable: why`, the two shapes of a loader's
#: feature flag (pipeline/context/*): the key is replaced by its label, the explanation kept.
_FLAG = re.compile(r"^\s*(?:[a-z0-9]+:)?(?P<key>[a-z0-9_]+?)(?:_null| unavailable)\s*:\s*(?P<why>.+)$", re.S)


def _flag_sentences(value: Any) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        if not item:
            continue
        text = str(item)
        match = _FLAG.match(text)
        if match:
            why = match.group("why").strip()
            # "Net generation ..." -> "net generation ..."; an acronym ("EPA's") keeps its case.
            if why[1:2].islower():
                why = why[:1].lower() + why[1:]
            out.append(f"{attribute_label(match.group('key'))} not shown: {why}")
        else:
            out.append(text)
    return out


def data_notes(bag: Mapping[str, Any] | None) -> list[str]:
    """Every `feature_flags` sentence in an attributes bag (top level and inside enrichment
    blocks), with its machine key turned into words: "Capacity factor, 2025 not shown: net
    generation -3402.0 MWh is not positive ..."."""
    notes: list[str] = []
    for key, value in (bag or {}).items():
        if key == "feature_flags":
            notes += _flag_sentences(value)
        elif isinstance(value, Mapping):
            notes += data_notes(value)
    return notes


#: The source-side fields the status maps' combined rules test (`services/api/lifecycle.py`
#: `maps_from[].raw` for a rule: "status_raw = ACTIVE, ia_status = Executed"), in words.
SOURCE_FIELD_LABELS: dict[str, str] = {
    "status_raw": "status",
    "status_original": "original status",
    "project_status": "project status",
    "ia_status": "interconnection agreement status",
    "ia_signed": "interconnection agreement signed",
    "approved_for_energization": "approved for energisation",
    "approved_for_synchronization": "approved for synchronisation",
    "form_type": "notice type",
    "deadline_passed": "deadline passed",
    "is_open_type": "open-type notice",
    "tag": "release tag",
}


def condition_words(raw: Any) -> str:
    """A combined status rule's condition in words: `status_raw = ACTIVE, ia_status = Executed` ->
    `status is "ACTIVE" and interconnection agreement status is "Executed"`. Text that is not in
    that shape is returned unchanged."""
    text = str(raw or "")
    if " = " not in text:
        return text
    parts = []
    for clause in text.split(", "):
        field, _, value = clause.partition(" = ")
        words = SOURCE_FIELD_LABELS.get(field.strip()) or humanise(field.strip()).lower()
        parts.append(f"{words} is \u201c{value.strip()}\u201d")
    return " and ".join(parts)


_PROSE_TOKEN = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b(?!\.[a-z]{2,4}\b)")


def words_in_prose(text: Any) -> str:
    """Prose written beside the code (a status-map rule's note) that names a flag or field by its
    key ("all are flagged status_conflict") reads it as words ("status conflict"). A file name the
    source itself publishes (`ks_wells.zip`) is left as written."""
    return _PROSE_TOKEN.sub(lambda m: m.group(0).replace("_", " "), str(text or ""))


def map_labels() -> dict[str, dict[str, str]]:
    """The tables `map.js` reads from the map page's `#map-labels` JSON (web/viewmodels.py
    `map_labels_json`)."""
    return {
        "technology": TECHNOLOGY_LABELS,
        "asset_status": ASSET_STATUS_LABELS,
        "lifecycle": LIFECYCLE_LABELS,
        "reuse_class": REUSE_CLASS_LABELS,
        "plant_family": PLANT_FAMILY_LABELS,
    }


def install(env: Any) -> None:
    """Registers the label filters on a Jinja environment. Every `Jinja2Templates` the site builds
    calls this (web/page.py, auth, legal, pricing, admin), because `_macros.html` uses them and is
    imported from templates each of those renders."""
    env.filters["technology_label"] = technology_label
    env.filters["technologies_label"] = technologies_label
    env.filters["lifecycle_label"] = lifecycle_label
    env.filters["proposal_kind_label"] = proposal_kind_label
    env.filters["opportunity_kind_label"] = opportunity_kind_label
    env.filters["organization_type_label"] = organization_type_label
    env.filters["reuse_class_label"] = reuse_class_label
    env.filters["attribute_label"] = attribute_label
    env.filters["condition_words"] = condition_words
    env.filters["humanise"] = humanise
    env.filters["words_in_prose"] = words_in_prose
