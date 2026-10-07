"""Every vocabulary token a reader can meet has words in `web/labels.py` (audit 2026-09-30: designer
D-7, D-14; frontend F1).

The tables are pinned to the vocabularies themselves, read where they are defined, so adding a
token anywhere without a label fails here rather than printing `gas_cc` or `under_construction` to a
reader:

* `data/vocabulary/lifecycle_states.yaml`: every proposal lifecycle state and opportunity status;
* `pipeline.normalize.TECH_RULES` (+ its `unknown`/`other` fallbacks): every proposal and plant
  technology class; `pipeline.connectors.opportunity.OPPORTUNITY_TECHNOLOGIES`: every opportunity
  class; the LMOP/AgSTAR RNG families; the asset technology values the context loaders write;
* `services.api.app`'s kind, organisation-type and reuse-class vocabularies, and the report issue
  types; `services/db/models.py` `ASSET_STATUSES` (read as text: web tests do not import the ORM,
  infra/importlinter.ini).

It also pins the rules every table obeys (words, not tokens) and the attribute-table shaping.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from pipeline.connectors.opportunity import OPPORTUNITY_TECHNOLOGIES
from pipeline.context.lmop import CATEGORY_TECH
from pipeline.normalize import TECH_RULES
from services.api.admin_posts import REPORT_ISSUE_TYPES
from services.api.app import (
    LIFECYCLE_STATE_VALUES,
    OPPORTUNITY_KIND_VALUES,
    OPPORTUNITY_STATUS_VALUES,
    ORGANIZATION_TYPE_VALUES,
    PROPOSAL_KIND_VALUES,
    REUSE_CLASS_VALUES,
)
from services.api.assets import TECHNOLOGY_VOCAB
from web import labels

REPO_ROOT = Path(__file__).resolve().parent.parent
VOCABULARY_FILE = REPO_ROOT / "data" / "vocabulary" / "lifecycle_states.yaml"
MODELS_FILE = REPO_ROOT / "services" / "db" / "models.py"

#: Asset `technology` values the context loaders derive from source text (pipe type, storage field
#: type, LNG function, ethanol), measured on the dev store on 2026-10-06. Open-ended by
#: construction (`pipeline/context/eia_atlas.py` lower-cases the source's word), so pinned as
#: measured; a new value still renders as words through `labels.humanise`.
ASSET_TECHNOLOGIES_MEASURED = (
    "ethanol",
    "gathering",
    "interstate",
    "intrastate",
    "aquifer",
    "depleted_field",
    "salt_dome",
    "import",
    "export",
    "import_export",
    "farm_digester",
)


def _tuple_from_models(name: str) -> tuple[str, ...]:
    match = re.search(rf"^{name}\s*=\s*\(([^)]*)\)", MODELS_FILE.read_text(encoding="utf-8"), flags=re.M)
    assert match, f"{name} not found in services/db/models.py"
    return tuple(re.findall(r'"([^"]+)"', match.group(1)))


def _lifecycle_vocabulary() -> list[str]:
    data = yaml.safe_load(VOCABULARY_FILE.read_text(encoding="utf-8"))
    return [*data["lifecycle_states"], *data["opportunity_statuses"]]


VOCABULARIES: list[tuple[str, dict[str, str], list[str]]] = [
    ("lifecycle (data/vocabulary)", labels.LIFECYCLE_LABELS, _lifecycle_vocabulary()),
    ("lifecycle (API)", labels.LIFECYCLE_LABELS, [*LIFECYCLE_STATE_VALUES, *OPPORTUNITY_STATUS_VALUES]),
    ("proposal technology", labels.TECHNOLOGY_LABELS, [t for _, t, _ in TECH_RULES] + ["unknown", "other"]),
    ("plant technology (API)", labels.TECHNOLOGY_LABELS, sorted(TECHNOLOGY_VOCAB)),
    ("opportunity technology", labels.TECHNOLOGY_LABELS, list(OPPORTUNITY_TECHNOLOGIES)),
    ("RNG family", labels.TECHNOLOGY_LABELS, sorted(set(CATEGORY_TECH.values()))),
    ("asset technology", labels.TECHNOLOGY_LABELS, list(ASSET_TECHNOLOGIES_MEASURED)),
    ("proposal kind", labels.PROPOSAL_KIND_LABELS, list(PROPOSAL_KIND_VALUES)),
    ("opportunity kind", labels.OPPORTUNITY_KIND_LABELS, list(OPPORTUNITY_KIND_VALUES)),
    ("organisation type", labels.ORGANIZATION_TYPE_LABELS, list(ORGANIZATION_TYPE_VALUES)),
    ("reuse class", labels.REUSE_CLASS_LABELS, list(REUSE_CLASS_VALUES)),
    ("asset status", labels.ASSET_STATUS_LABELS, list(_tuple_from_models("ASSET_STATUSES"))),
    ("report issue", labels.REPORT_ISSUE_LABELS, list(REPORT_ISSUE_TYPES)),
]


@pytest.mark.parametrize(("name", "table", "tokens"), VOCABULARIES, ids=[v[0] for v in VOCABULARIES])
def test_every_vocabulary_token_has_a_label(name: str, table: dict[str, str], tokens: list[str]) -> None:
    assert tokens, f"{name}: the vocabulary read empty, so this test would pass vacuously"
    missing = sorted({t for t in tokens if t not in table})
    assert not missing, f"{name}: no label for {missing} -- add them to web/labels.py"


#: Technology tokens connectors write as a literal (`"technology": "co2_geologic_sequestration"`)
#: rather than through the classifier, read from the connector sources so a new constant cannot
#: reach a page through the `humanise` fallback (2026-10-07: "Co2 geologic sequestration").
_CONNECTOR_TECHNOLOGY = re.compile(r'"technology":\s*"([a-z0-9_]+)"')


def _connector_technology_constants() -> list[str]:
    found: set[str] = set()
    for path in (REPO_ROOT / "pipeline" / "connectors").rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        found.update(_CONNECTOR_TECHNOLOGY.findall(path.read_text(encoding="utf-8")))
    return sorted(found)


def test_every_connector_technology_constant_has_a_label() -> None:
    constants = _connector_technology_constants()
    assert constants, "no constants found, so this test would pass vacuously"
    missing = [t for t in constants if t not in labels.TECHNOLOGY_LABELS]
    assert not missing, f"no label for {missing} -- add them to web/labels.py"


def test_class_vi_technology_reads_co2_in_capitals_and_without_a_subscript() -> None:
    """U+2082 is outside every self-hosted Plex `unicode-range`, so "CO2" rather than "CO₂"; the
    fallback's "Co2" was the defect."""
    assert "co2_geologic_sequestration" in _connector_technology_constants()
    assert labels.technology_label("co2_geologic_sequestration") == "CO2 geologic sequestration"
    css = (REPO_ROOT / "web" / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert "U+2080" not in css and "2082" not in css  # if a subscript face is ever added, revisit
    assert labels.map_labels()["technology"]["co2_geologic_sequestration"] == "CO2 geologic sequestration"


ALL_TABLES = {
    "TECHNOLOGY_LABELS": labels.TECHNOLOGY_LABELS,
    "LIFECYCLE_LABELS": labels.LIFECYCLE_LABELS,
    "PROPOSAL_KIND_LABELS": labels.PROPOSAL_KIND_LABELS,
    "OPPORTUNITY_KIND_LABELS": labels.OPPORTUNITY_KIND_LABELS,
    "ORGANIZATION_TYPE_LABELS": labels.ORGANIZATION_TYPE_LABELS,
    "ASSET_STATUS_LABELS": labels.ASSET_STATUS_LABELS,
    "REUSE_CLASS_LABELS": labels.REUSE_CLASS_LABELS,
    "PLANT_FAMILY_LABELS": labels.PLANT_FAMILY_LABELS,
    "ATTRIBUTE_LABELS": labels.ATTRIBUTE_LABELS,
    "REPORT_ISSUE_LABELS": labels.REPORT_ISSUE_LABELS,
}


@pytest.mark.parametrize("name", sorted(ALL_TABLES))
def test_labels_are_words_not_tokens(name: str) -> None:
    for token, label in ALL_TABLES[name].items():
        assert label and label.strip() == label, (name, token)
        assert "_" not in label, f"{name}[{token!r}] = {label!r} still reads as a token"
        assert label[0].isupper() or label[0].isdigit(), f"{name}[{token!r}] = {label!r} is not sentence case"


def test_the_existing_names_are_the_same_tables() -> None:
    """One source: the names other modules always imported are these tables, not copies."""
    from web import retirement, viewmodels

    assert viewmodels.TECHNOLOGY_LABELS is labels.TECHNOLOGY_LABELS
    assert viewmodels.PROPOSAL_KIND_LABELS is labels.PROPOSAL_KIND_LABELS
    assert viewmodels.OPPORTUNITY_KIND_LABELS is labels.OPPORTUNITY_KIND_LABELS
    assert retirement.ASSET_STATUS_LABELS is labels.ASSET_STATUS_LABELS


def test_an_unlabelled_token_still_reads_as_words() -> None:
    assert labels.technology_label("tidal_lagoon") == "Tidal lagoon"
    assert labels.lifecycle_label("under_construction") == "Under construction"
    assert labels.technology_label(None) is None and labels.technology_label("") is None


def test_technologies_label_joins_and_splits_pipe_joined_values() -> None:
    assert labels.technologies_label(["solar_pv|nuclear", "bess"]) == "Solar PV, Nuclear, Battery storage"
    assert labels.technologies_label(["gas_cc", "gas_cc"]) == "Gas, combined cycle"
    assert labels.technologies_label([]) == ""


def test_attribute_labels_name_units_and_years() -> None:
    assert labels.attribute_label("net_generation_mwh_2025") == "Net generation (MWh), 2025"
    assert labels.attribute_label("working_gas_capacity_mcf") == "Working gas capacity (Mcf)"
    assert labels.attribute_label("brand_new_key") == "Brand new key"


def test_labelled_attributes_drop_internal_keys_and_format_values() -> None:
    bag = {
        "voltage_class": "345 kV",
        "sub_1_raw": "scriba",
        "vertex_count": 12,
        "length_miles_source": 1.101209821,
        "lbnl_link_confidence": "medium",
        "base_gas_mcf": 6961981.0,
        "reporting_year": 2023,
        "awarded_usda_funding": 1.0,
        "capacity_factor_2025": 0.41234,
        "miles": 14.5,
        "ghgrp": {
            "ghgrp_facility_id": "1005267",
            "match_method": "oris_crosswalk",
            "match_score": 1.0,
            "share_flag": "ok",
            "source_id": "us.epa.ghgrp",
            "subpart_rr": "No",
        },
        "feature_flags": ["eia923:capacity_factor_2025_null: Net generation is not positive"],
        "empty": "",
        "nothing": None,
    }
    shown = labels.labelled_attributes(bag, omit=("miles",))
    assert list(shown) == [
        "Voltage class",
        "Base gas (Mcf)",
        "Reporting year",
        "USDA funding awarded",
        "Capacity factor, 2025",
        "EPA greenhouse gas reporting (GHGRP)",
    ]
    assert shown["Base gas (Mcf)"] == "6,961,981"
    assert shown["Reporting year"] == "2023"  # a year is never "2,023"
    assert shown["USDA funding awarded"] == "Yes"
    assert shown["Capacity factor, 2025"] == "0.412"
    assert shown["EPA greenhouse gas reporting (GHGRP)"] == {
        "GHGRP facility ID": "1005267",
        "Subpart RR (geologic sequestration)": "No",
    }
    assert labels.data_notes(bag) == ["Capacity factor, 2025 not shown: net generation is not positive"]


def test_data_notes_read_both_flag_shapes_and_nested_blocks() -> None:
    bag = {
        "rfs": {"feature_flags": ["first_registered_year unavailable: EPA's list states no date"]},
        "phmsa": {"feature_flags": "incidents_5y_significant unavailable: the flag file was not retrieved"},
    }
    assert labels.data_notes(bag) == [
        "First registered not shown: EPA's list states no date",
        "Significant incidents, last 5 years not shown: the flag file was not retrieved",
    ]


def test_condition_words_reads_a_combined_rule() -> None:
    raw = "status_raw = ACTIVE, ia_status = Filed Unexecuted"
    assert labels.condition_words(raw) == (
        "status is “ACTIVE” and interconnection agreement status is “Filed Unexecuted”"
    )
    assert labels.condition_words("Operational") == "Operational"


def test_prose_written_beside_the_code_reads_keys_as_words() -> None:
    note = "Project Status is stale; 220 rows conflict and all are flagged status_conflict."
    assert labels.words_in_prose(note).endswith("all are flagged status conflict.")
    # A file name the source itself publishes under is the source's word, left as written.
    assert labels.words_in_prose("master well list (ks_wells.zip)") == "master well list (ks_wells.zip)"
