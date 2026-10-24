"""The technology family set and the proposal kinds (docs/28 §8.1, lane F4, 2026-10-24).

Every raw technology value the public register held is pinned to a fine token, a kind and a family
key, so a filter by family (`family=solar`) is the same filter on every page and nothing falls
through to a catch-all unseen. The table below is the audit's `tag_sets.proposal_technology.
raw_mapping` (filter-audit.json, public register pulled 2026-10-23 23:16-23:21 UTC, 6,633
proposals), one row per distinct raw value with the sources that sent it and how many records
carried it. Where this table differs from the audit's `proposed` column it is on purpose, and the
reason is in `AUDIT_DEVIATIONS`.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from pipeline.connectors.opportunity import OPPORTUNITY_TECHNOLOGIES
from pipeline.normalize import (
    TECH_RULES,
    classify_tech,
    proposal_kinds,
    technology_families,
    technology_family,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONNECTORS = ROOT / "pipeline" / "connectors"

#: (raw technology, fine token, kind, family, sources, records). `None` is a record whose source
#: states no technology; the Texas RRC row is the connector's own literal, not the classifier's.
AUDIT_RAW_VALUES: tuple[tuple[str | None, str, str, str, str, int], ...] = (
    ("Biomass", "biomass", "generation", "bioenergy", "gb.neso.tec_register", 10),
    ("Biomass;Demand", "biomass", "generation", "bioenergy", "gb.neso.tec_register", 1),
    ("Landfill Gas", "biomass", "generation", "bioenergy", "us.eia.860m", 11),
    ("Other Waste Biomass", "biomass", "generation", "bioenergy", "us.eia.860m", 17),
    ("CHP (Combined Heat and Power)", "chp", "generation", "other", "gb.neso.tec_register", 12),
    (None, "co2_geologic_sequestration", "ccs", "carbon_storage", "us.tx.rrc.class_vi", 20),
    ("CCGT (Combined Cycle Gas Turbine)", "gas_cc", "generation", "gas", "gb.neso.tec_register", 51),
    ("CCGT (Combined Cycle Gas Turbine);Demand", "gas_cc", "generation", "gas", "gb.neso.tec_register", 4),
    (
        "CCGT (Combined Cycle Gas Turbine);Demand;Energy Storage System;Hydrogen;OCGT (Open Cycle Gas Turbine)",  # noqa: E501
        "gas_cc",
        "generation",
        "gas",
        "gb.neso.tec_register",
        2,
    ),
    (
        "CCGT (Combined Cycle Gas Turbine);Energy Storage System",
        "gas_cc",
        "generation",
        "gas",
        "gb.neso.tec_register",
        6,
    ),
    (
        "CCGT (Combined Cycle Gas Turbine);Energy Storage System;OCGT (Open Cycle Gas Turbine)",
        "gas_cc",
        "generation",
        "gas",
        "gb.neso.tec_register",
        3,
    ),
    (
        "CCGT (Combined Cycle Gas Turbine);Reactive Compensation",
        "gas_cc",
        "generation",
        "gas",
        "gb.neso.tec_register",
        1,
    ),
    ("Gas - Combined-Cycle", "gas_cc", "generation", "gas", "us.eia.860m;us.iso.ercot.gen_queue", 45),
    ("Natural Gas Fired Combined Cycle", "gas_cc", "generation", "gas", "us.eia.860m", 231),
    (
        "Energy Storage System;OCGT (Open Cycle Gas Turbine)",
        "gas_ct",
        "generation",
        "gas",
        "gb.neso.tec_register",
        3,
    ),
    (
        "Gas - Combustion (gas) Turbine, but not part of a Combined-Cycle",
        "gas_ct",
        "generation",
        "gas",
        "us.eia.860m;us.iso.ercot.gen_queue",
        53,
    ),
    ("Natural Gas Fired Combustion Turbine", "gas_ct", "generation", "gas", "us.eia.860m", 168),
    ("OCGT (Open Cycle Gas Turbine)", "gas_ct", "generation", "gas", "gb.neso.tec_register", 15),
    ("Oil & AGT (Advanced Gas Turbine)", "gas_ct", "generation", "gas", "gb.neso.tec_register", 7),
    ("Energy Storage System;Gas Reciprocating", "gas_ice", "generation", "gas", "gb.neso.tec_register", 6),
    (
        "Gas - Internal Combustion Engine, eg. Reciprocating",
        "gas_ice",
        "generation",
        "gas",
        "us.eia.860m;us.iso.ercot.gen_queue",
        32,
    ),
    ("Gas Reciprocating", "gas_ice", "generation", "gas", "gb.neso.tec_register", 7),
    ("Natural Gas Internal Combustion Engine", "gas_ice", "generation", "gas", "us.eia.860m", 60),
    (
        "Gas - Steam Turbine other than Combined-Cycle",
        "gas_steam",
        "generation",
        "gas",
        "us.iso.ercot.gen_queue",
        3,
    ),
    ("Natural Gas Steam Turbine", "gas_steam", "generation", "gas", "us.eia.860m", 1),
    ("Geothermal", "geothermal", "generation", "geothermal", "us.eia.860m", 9),
    ("Reactive Compensation", "grid_equipment", "transmission", "grid", "gb.neso.tec_register", 17),
    ("Conventional Hydroelectric", "hydro", "generation", "hydro", "us.eia.860m", 117),
    ("Hydro", "hydro", "generation", "hydro", "gb.neso.tec_register", 32),
    ("Water - Other", "hydro", "generation", "hydro", "us.iso.ercot.gen_queue", 1),
    ("Demand;Hydrogen", "hydrogen", "generation", "hydrogen", "gb.neso.tec_register", 1),
    ("Hydrogen", "hydrogen", "generation", "hydrogen", "gb.neso.tec_register", 1),
    ("Hydrogen - Other", "hydrogen", "generation", "hydrogen", "us.iso.ercot.gen_queue", 1),
    (
        "Data Center",
        "load",
        "load",
        "large_load",
        "us.epa.echo.icis_air;us.va.deq.data_center_air_sites",
        582,
    ),
    ("Energy Storage System;Tidal", "marine", "generation", "hydro", "gb.neso.tec_register", 5),
    ("Tidal", "marine", "generation", "hydro", "gb.neso.tec_register", 7),
    ("Nuclear", "nuclear", "generation", "nuclear", "gb.neso.tec_register;us.eia.860m", 16),
    (
        "Nuclear - Steam Turbine other than Combined-Cycle",
        "nuclear",
        "generation",
        "nuclear",
        "us.iso.ercot.gen_queue",
        2,
    ),
    ("Fuel Oil - Other", "oil", "generation", "oil", "us.iso.ercot.gen_queue", 1),
    ("Petroleum Liquids", "oil", "generation", "oil", "us.eia.860m", 23),
    ("All Other", "other", "other", "other", "us.eia.860m", 5),
    ("Other - Other", "other", "other", "other", "us.iso.ercot.gen_queue", 4),
    ("Thermal", "other", "other", "other", "gb.neso.tec_register", 7),
    (
        "Energy Storage System;Pump Storage;Thermal",
        "pumped_storage",
        "storage",
        "storage",
        "gb.neso.tec_register",
        2,
    ),
    ("Hydroelectric Pumped Storage", "pumped_storage", "storage", "storage", "us.eia.860m", 11),
    ("Pump Storage", "pumped_storage", "storage", "storage", "gb.neso.tec_register", 18),
    ("Pump Storage;Wind Onshore", "pumped_storage", "storage", "storage", "gb.neso.tec_register", 1),
    ("Demand;PV Array (Photo Voltaic/solar)", "solar", "generation", "solar", "gb.neso.tec_register", 6),
    ("PV Array (Photo Voltaic/solar)", "solar", "generation", "solar", "gb.neso.tec_register", 37),
    ("Solar - Photovoltaic Solar", "solar", "generation", "solar", "us.eia.860m;us.iso.ercot.gen_queue", 580),
    ("Solar Photovoltaic", "solar", "generation", "solar", "us.eia.860m;us.iso.ercot.gen_queue", 973),
    (
        "CCGT (Combined Cycle Gas Turbine);Energy Storage System;PV Array (Photo Voltaic/solar);Wind Onshore",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        1,
    ),
    (
        "CHP (Combined Heat and Power);Energy Storage System;PV Array (Photo Voltaic/solar);Wind Onshore",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        1,
    ),
    (
        "Demand;Energy Storage System;Nuclear;PV Array (Photo Voltaic/solar);Wind Onshore",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        2,
    ),
    (
        "Demand;Energy Storage System;PV Array (Photo Voltaic/solar)",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        37,
    ),
    (
        "Demand;Energy Storage System;PV Array (Photo Voltaic/solar);Reactive Compensation",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        10,
    ),
    (
        "Demand;Energy Storage System;PV Array (Photo Voltaic/solar);Wind Onshore",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        6,
    ),
    (
        "Energy Storage System;Hydro;Nuclear;PV Array (Photo Voltaic/solar);Wind Onshore",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        1,
    ),
    (
        "Energy Storage System;Hydrogen;PV Array (Photo Voltaic/solar);Wind Onshore",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        1,
    ),
    (
        "Energy Storage System;Nuclear;PV Array (Photo Voltaic/solar);Reactive Compensation;Wind Onshore",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        3,
    ),
    (
        "Energy Storage System;Nuclear;PV Array (Photo Voltaic/solar);Wind Onshore",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        11,
    ),
    (
        "Energy Storage System;PV Array (Photo Voltaic/solar)",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        445,
    ),
    (
        "Energy Storage System;PV Array (Photo Voltaic/solar);Reactive Compensation",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        2,
    ),
    (
        "Energy Storage System;PV Array (Photo Voltaic/solar);Reactive Compensation;Wind Onshore",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        4,
    ),
    (
        "Energy Storage System;PV Array (Photo Voltaic/solar);Wind Offshore",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        1,
    ),
    (
        "Energy Storage System;PV Array (Photo Voltaic/solar);Wind Onshore",
        "solar_storage",
        "generation",
        "solar",
        "gb.neso.tec_register",
        51,
    ),
    (
        "Other - Battery Energy Storage + Solar - Photovoltaic Solar",
        "solar_storage",
        "generation",
        "solar",
        "us.eia.860m;us.iso.ercot.gen_queue",
        11,
    ),
    (
        "Solar - Photovoltaic Solar + Other - Battery Energy Storage",
        "solar_storage",
        "generation",
        "solar",
        "us.eia.860m;us.iso.ercot.gen_queue",
        15,
    ),
    ("Solar Thermal without Energy Storage", "solar_thermal", "generation", "solar", "us.eia.860m", 1),
    ("Batteries", "storage", "storage", "storage", "us.eia.860m;us.iso.ercot.gen_queue", 385),
    ("Demand;Energy Storage System", "storage", "storage", "storage", "gb.neso.tec_register", 25),
    (
        "Demand;Energy Storage System;Reactive Compensation",
        "storage",
        "storage",
        "storage",
        "gb.neso.tec_register",
        2,
    ),
    ("Demand;LAES (Liquid Air Energy Storage)", "storage", "storage", "storage", "gb.neso.tec_register", 1),
    ("Energy Storage System", "storage", "storage", "storage", "gb.neso.tec_register", 745),
    (
        "Energy Storage System;Reactive Compensation",
        "storage",
        "storage",
        "storage",
        "gb.neso.tec_register",
        21,
    ),
    (
        "Other - Battery Energy Storage",
        "storage",
        "storage",
        "storage",
        "us.eia.860m;us.iso.ercot.gen_queue",
        804,
    ),
    (None, "unknown", "other", "other", "us.iso.ercot.gen_queue", 1),
    ("Municipal Solid Waste", "waste", "generation", "bioenergy", "us.eia.860m", 1),
    ("Waste", "waste", "generation", "bioenergy", "gb.neso.tec_register", 4),
    ("Demand;Wind Onshore", "wind", "generation", "wind", "gb.neso.tec_register", 2),
    ("Onshore Wind Turbine", "wind", "generation", "wind", "us.eia.860m", 70),
    ("PV Array (Photo Voltaic/solar);Wind Onshore", "wind", "generation", "wind", "gb.neso.tec_register", 3),
    ("Substation;Wind Onshore", "wind", "generation", "wind", "gb.neso.tec_register", 1),
    ("Wind - Wind Turbine", "wind", "generation", "wind", "us.eia.860m;us.iso.ercot.gen_queue", 159),
    ("Wind Onshore", "wind", "generation", "wind", "gb.neso.tec_register", 318),
    ("Demand;Wind Offshore", "wind_offshore", "generation", "wind", "gb.neso.tec_register", 4),
    ("Interconnector;Wind Offshore", "wind_offshore", "generation", "wind", "gb.neso.tec_register", 1),
    ("Offshore Wind Turbine", "wind_offshore", "generation", "wind", "us.eia.860m", 4),
    ("Wind Offshore", "wind_offshore", "generation", "wind", "gb.neso.tec_register", 141),
    (
        "Demand;Energy Storage System;Wind Onshore",
        "wind_storage",
        "generation",
        "wind",
        "gb.neso.tec_register",
        2,
    ),
    ("Energy Storage System;Wind Offshore", "wind_storage", "generation", "wind", "gb.neso.tec_register", 4),
    ("Energy Storage System;Wind Onshore", "wind_storage", "generation", "wind", "gb.neso.tec_register", 104),
)

#: Rows where this table and the audit's `proposed` column differ, and why. The audit asked for the
#: three small tags to be merged "into their families"; a family merge keeps the fine token as data.
AUDIT_DEVIATIONS: dict[str | None, str] = {
    "Gas - Steam Turbine other than Combined-Cycle": "gas_steam kept as data, merged into the gas family",
    "Natural Gas Steam Turbine": "gas_steam kept as data, merged into the gas family",
    None: "unknown ('not stated') kept as data, merged into the other family",
    "CHP (Combined Heat and Power)": "kind generation (a CHP plant generates electricity), not other",
    "Solar Thermal without Energy Storage": "solar_thermal: the audit kept the hybrid rule's misreading",
}

#: Opportunity markers that are not technologies (`data/match_rules.yaml` `all_source_tokens`).
NOT_TECHNOLOGIES = frozenset({"all_source", "any"})
#: The classifier's two fall-through tokens (no rule emits them).
FALLBACK_TOKENS = ("unknown", "other")


def _proposal_connector_sources() -> list[str]:
    out = []
    for path in sorted(CONNECTORS.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        if 'kind: ClassVar[Kind] = "proposal"' in text:
            out.append(text)
    return out


def _connector_literals(field: str) -> set[str]:
    pattern = re.compile(rf'"{field}":\s*"([a-z0-9_]+)"')
    return {m for text in _proposal_connector_sources() for m in pattern.findall(text)}


def _proposal_tokens() -> set[str]:
    return {tech for _, tech, _ in TECH_RULES} | set(FALLBACK_TOKENS) | _connector_literals("technology")


@pytest.mark.parametrize(
    ("raw", "tech", "kind", "family", "sources", "records"),
    AUDIT_RAW_VALUES,
    ids=[f"{r[4]}:{r[0]}" for r in AUDIT_RAW_VALUES],
)
def test_every_raw_value_the_register_held_maps_to_a_token_a_kind_and_a_family(
    raw: str | None, tech: str, kind: str, family: str, sources: str, records: int
) -> None:
    if sources == "us.tx.rrc.class_vi":
        assert tech in _connector_literals("technology")
        assert kind in _connector_literals("kind")
    else:
        assert classify_tech(raw) == (tech, kind)
    assert technology_family(tech) == family
    assert records > 0


def test_the_table_covers_every_record_the_register_held_and_names_its_deviations() -> None:
    """92 distinct raw values over the 6,633 proposals of the pulled register: every record."""
    assert len(AUDIT_RAW_VALUES) == 92
    assert sum(r[5] for r in AUDIT_RAW_VALUES) == 6633
    raws = {r[0] for r in AUDIT_RAW_VALUES}
    assert set(AUDIT_DEVIATIONS) <= raws


def test_every_token_anything_can_emit_is_in_exactly_one_family() -> None:
    families = technology_families()  # raises if a token sits in two families
    tokens = _proposal_tokens() | (set(OPPORTUNITY_TECHNOLOGIES) - NOT_TECHNOLOGIES)
    assert len(tokens) > 30, "the vocabulary read too few tokens, so this test proves nothing"
    assert sorted(t for t in tokens if technology_family(t) is None) == []
    members = {t for m in families.values() for t in m}
    # No family names a token nothing emits (a stale member would hide a vocabulary gap). `coal`
    # is stored on opportunities and emitted by the classifier; `gas` is an opportunity token.
    assert sorted(members - tokens) == []


def test_the_merged_small_tags_stay_data_inside_their_families() -> None:
    """docs/28 §6, §8.2: "Not stated", "Gas, steam turbine" and "Waste to energy" stop being options
    of their own; the records keep their fine token and the family carries them."""
    assert technology_family("gas_steam") == "gas"
    assert technology_family("waste") == "bioenergy"
    assert technology_family("unknown") == "other"
    assert technology_family(None) == "other"
    assert classify_tech("Natural Gas Steam Turbine") == ("gas_steam", "generation")
    assert classify_tech("Municipal Solid Waste") == ("waste", "generation")
    assert classify_tech(None) == ("unknown", "other")


def test_a_family_holds_every_token_of_the_audit_family_set() -> None:
    """docs/28 §8.1, member for member; `lng_terminal` (no record yet) joins gas."""
    families = technology_families()
    assert list(families) == [
        "solar",
        "wind",
        "storage",
        "gas",
        "nuclear",
        "hydro",
        "bioenergy",
        "geothermal",
        "oil",
        "coal",
        "hydrogen",
        "large_load",
        "carbon_storage",
        "grid",
        "demand_side",
        "other",
    ]
    assert families["grid"] == ("transmission", "grid_equipment")
    assert families["other"] == ("other", "unknown", "chp", "pipeline")
    assert "gas_pipeline" in families["gas"]
    assert "lng_terminal" in families["gas"]


# ------------------------------------------------------------------ kinds
def test_every_kind_the_rules_or_a_connector_emit_is_a_vocabulary_kind() -> None:
    kinds, retired = proposal_kinds()
    emitted = {kind for _, _, kind in TECH_RULES} | {"other"} | _connector_literals("kind")
    assert emitted <= set(kinds), sorted(emitted - set(kinds))
    assert not emitted & set(retired)


def test_every_vocabulary_kind_is_written_by_a_rule_or_a_connector() -> None:
    """The other direction (review of lane F4): a kind in `kinds` that nothing writes is a filter
    that answers zero for good. `pipeline` was such a kind until the pipeline rules: the Permitting
    Dashboard's pipelines landed under transmission and generation instead."""
    kinds, _ = proposal_kinds()
    emitted = {kind for _, _, kind in TECH_RULES} | {"other"} | _connector_literals("kind")
    assert sorted(set(kinds) - emitted) == []


#: The Permitting Dashboard's sector types for its pipeline and carbon capture sectors, as its
#: 2026-10-06 sandbox run sent them (`data/eval/technology_classes.csv`). The source has never
#: loaded in production (docs/28 §1), so none of these is on the public register yet.
PERMITS_DASHBOARD_NON_POWER: tuple[tuple[str, str, str, str], ...] = (
    ("Pipelines", "pipeline", "pipeline", "other"),
    ("Interstate Natural Gas Pipelines", "gas_pipeline", "pipeline", "gas"),
    (
        "Liquefied Natural Gas Terminal Facilities (Onshore or in State Water), and associated Natural Gas "
        "Pipelines",
        "lng_terminal",
        "lng",
        "gas",
    ),
    ("Carbon Capture and Sequestration", "ccs", "ccs", "carbon_storage"),
)


@pytest.mark.parametrize(("raw", "tech", "kind", "family"), PERMITS_DASHBOARD_NON_POWER)
def test_the_permits_dashboard_pipelines_and_carbon_capture_reach_their_own_kinds(
    raw: str, tech: str, kind: str, family: str
) -> None:
    """Were `(transmission, transmission)` ("pipe-line-s" matched the transmission rule's `line`),
    `(gas_other, generation)` and `(other, other)`."""
    assert classify_tech(raw) == (tech, kind)
    assert technology_family(tech) == family


def test_the_recorded_permits_dashboard_rows_land_under_kind_pipeline_and_ccs() -> None:
    """End to end through the connector on its recorded CSV (10 energy projects, 2026-09-12): the
    rows the review named, not only the strings."""
    from conftest import connector_for, snapshot

    c = connector_for("us.permits_dashboard")
    raw = snapshot(
        "permits_dashboard_projects.csv",
        "https://data.permits.performance.gov/api/views/mcm3-xbid/rows.csv?accessType=DOWNLOAD",
        "text/csv",
    )
    df = c.normalize(c.parse(raw), raw)
    by_raw = {r: (t, k) for r, t, k in zip(df["technology_raw"], df["technology"], df["kind"], strict=True)}
    assert by_raw["Interstate Natural Gas Pipelines"] == ("gas_pipeline", "pipeline")
    assert by_raw["Carbon Capture and Sequestration"] == ("ccs", "ccs")
    assert by_raw["Electricity Transmission"] == ("transmission", "transmission")
    assert all(technology_family(t) is not None for t in df["technology"])


@pytest.mark.parametrize(
    ("raw", "tech", "kind"),
    [
        ("Carbon Capture and Storage", "ccs", "ccs"),  # not a battery
        ("CO2 Storage", "ccs", "ccs"),
        ("Hydrogen Pipeline", "pipeline", "pipeline"),  # a pipeline, not a hydrogen plant
        ("Crude Oil Pipeline", "pipeline", "pipeline"),  # a pipeline, not an oil plant
        ("Natural Gas Pipeline", "gas_pipeline", "pipeline"),
        ("Pipeline (natural gas)", "gas_pipeline", "pipeline"),
        ("Transmission Line", "transmission", "transmission"),  # the pipeline rules need "pipe"
        ("Energy Storage System;Reactive Compensation", "storage", "storage"),
    ],
)
def test_non_power_rules_win_over_the_words_they_share_with_power_rules(
    raw: str, tech: str, kind: str
) -> None:
    assert classify_tech(raw) == (tech, kind)


def test_a_missing_token_of_any_null_kind_is_not_stated() -> None:
    """`pd.NA` (a nullable string column's missing value) is "not stated", like None and NaN."""
    import pandas as pd

    assert technology_family(pd.NA) == "other"
    assert technology_family(float("nan")) == "other"
    assert technology_family("  ") == "other"
    assert technology_family("no_such_token") is None


def test_nuclear_and_hydrogen_are_technologies_of_generation_not_kinds() -> None:
    """docs/28 B-2: a kind nothing can carry is a filter that always answers zero."""
    kinds, retired = proposal_kinds()
    assert set(retired) == {"nuclear", "hydrogen"}
    assert not set(retired) & set(kinds)
    for raw in (
        "Nuclear",
        "Nuclear - Steam Turbine other than Combined-Cycle",
        "Hydrogen",
        "Hydrogen - Other",
    ):
        assert classify_tech(raw)[1] == "generation", raw


def test_lng_is_its_own_kind_and_grid_equipment_is_transmission() -> None:
    assert classify_tech("Liquefied Natural Gas Terminal Facilities (Onshore or in State Water)") == (
        "lng_terminal",
        "lng",
    )
    assert classify_tech("Reactive Compensation") == ("grid_equipment", "transmission")


def test_the_kind_vocabulary_is_a_subset_of_what_the_api_accepts() -> None:
    """The API's fixed list (`services/api/app.py` `PROPOSAL_KIND_VALUES`) still carries the retired
    kinds until the API lane drops them; every live kind must already be accepted there."""
    from services.api.app import PROPOSAL_KIND_VALUES

    kinds, _ = proposal_kinds()
    assert set(kinds) <= set(PROPOSAL_KIND_VALUES), sorted(set(kinds) - set(PROPOSAL_KIND_VALUES))
