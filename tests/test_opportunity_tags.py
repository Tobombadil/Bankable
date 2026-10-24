"""`pipeline/connectors/opportunity.py`: the technology rules every opportunity connector shares
(docs/28 B-4 to B-6) and the two type-of-work layers (owner, 2026-10-24: "filter RFPs and the like
by what type of opportunity it is for"). Titles and codes are the 2026-10-23 public register's
unless a case says otherwise."""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import fixture_path
from pipeline.connectors.opportunity import (
    ALL_SOURCE,
    APPLICANT_AUDIENCES,
    APPLICANT_TYPES,
    AUDIENCES,
    OPPORTUNITY_TECHNOLOGIES,
    TECH_KEYWORDS,
    WORK_TYPE_AUDIENCES,
    WORK_TYPES,
    Tag,
    audiences_for,
    classify_technologies,
    eligible_applicants,
    energy_supply_phrase,
    grid_evidence,
    not_grid_context,
    ted_buyer_title,
    work_profile,
    work_profile_columns,
)

# --------------------------------------------------------------------------------- technologies


def test_coal_is_a_technology_a_notice_can_carry_and_all_source_is_not_an_option() -> None:
    """docs/28 B-5: 28 stored notices carried `coal` and no reader could choose it."""
    assert "coal" in OPPORTUNITY_TECHNOLOGIES
    assert ALL_SOURCE not in OPPORTUNITY_TECHNOLOGIES
    assert set(OPPORTUNITY_TECHNOLOGIES) == {token for _, token in TECH_KEYWORDS}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Dostawa węgla kamiennego do kotłowni", ["coal"]),
        ("Lieferung von Steinkohle", ["coal"]),
        ("Fourniture de charbon actif pour la station d'épuration", []),  # activated carbon
        ("Low-carbon heating study", []),
        ("Fornitura di sistemi per medicina nucleare", []),
        ("Nuclear power plant decommissioning support", ["nuclear"]),
        ("Kurināmās malkas iegāde", ["biomass"]),
        ("Dodávka štěpky frakce 30 - 150", ["biomass"]),
        ("Suministro de pellet para las calderas de biomasa", ["biomass"]),
        ("Off-grid solar for rural clinics", ["solar_pv", "microgrid"]),
        ("Rahmenvertrag für die Lieferung von AC-Ladestationen mit 44 kW", ["ev_charging"]),
    ],
)
def test_keyword_rules(text: str, expected: list[str]) -> None:
    assert classify_technologies(text) == expected


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        ("Ernen nouvelle sous-station, Transformateurs 65/16.5 kV", "sous-station"),
        ("Dostawa odłączników i uziemników do sieci 110 kV, 220 kV i 400 kV", "odłączników"),
        ("Keskpinge maakaabli ristlõikega 240 mm² ostmine", "keskpinge"),
        ("Distribuční olejové třífázové transformátory vn/nn II", "transformátory"),
        ("EDF SEI - TRAVAUX GROUPES AERO-SOUTERRAINS DE RESEAUX HTA/BT", "hta"),
        # the old rule's words that name any network or anything electrical
        ("Budowa sieci kanalizacji sanitarnej i wodociągowej", None),
        ("Fourniture de matériels électriques", None),
        ("Bau eines Gigabit-Netzes (FTTB) in der Stadt Hayingen", None),
        ("Rekonstrukce SCZT pára x HV", None),  # Czech HV: hot water
        ("Off-grid solar home systems", None),
        # a voltage in an electricity purchase is the supply's
        ("Suministro de energía eléctrica, en baja y alta tensión", None),
        ("Доставка на електрическа енергия - ниско и средно напрежение", None),
        # never grid work: rail traction, street lighting, charging
        ("Modernizacja podstacji trakcyjnej 15 kV", None),
        ("Vidsprieguma transformatoru piegāde vilces apakšstacijai Nr. 6", None),
        ("Modernizacja oświetlenia ulicznego wraz z przebudową linii kablowej SN", None),
    ],
)
def test_grid_evidence(text: str, evidence: str | None) -> None:
    hit = grid_evidence(text)
    assert (hit is None) if evidence is None else (hit is not None and hit.startswith(evidence[:6]))


def test_road_words_veto_codes_and_cues_but_not_explicit_grid_words() -> None:
    title = (
        "Wooden Poles with Lattice Towers for 11kV and 33kV Overhead Lines in Swampy Areas, River and "
        "Road Crossings"
    )
    assert grid_evidence(title) is not None
    assert not_grid_context(title) == "road"
    assert not_grid_context(title, weak=False) is None


def test_energy_supply_phrases() -> None:
    assert energy_supply_phrase("Kompleksowa dostawa energii elektrycznej wraz z usługą dystrybucji")
    assert energy_supply_phrase("Lieferung von Ökostrom") is None
    assert energy_supply_phrase("Fourniture et acheminement d'énergie électrique")
    assert energy_supply_phrase("Dostawa transformatorów SN/nn") is None


def test_ted_buyer_title() -> None:
    assert (
        ted_buyer_title("Slovenia – Electricity – Dobava električne energije") == "Dobava električne energije"
    )


# --------------------------------------------------------------------------------- type of work


def _ted(title: str, cpv: list[str], nature: list[str]) -> dict[str, Any]:
    return {
        "source_id": "eu.ted.api",
        "kind": "tender",
        "title": title,
        "identifiers": {"cpv": cpv, "contract_nature": nature},
    }


@pytest.mark.parametrize(
    ("notice", "raw", "work", "audiences"),
    [
        # An electricity purchase: energy supply, for energy suppliers (docs/28 §11 item 1).
        (
            _ted(
                "Poland – Electricity – Dostawa energii elektrycznej na 2027 rok", ["09310000"], ["supplies"]
            ),
            None,
            ["energy_supply"],
            ["energy_suppliers"],
        ),
        # Transformers bought by a distribution operator: equipment.
        (
            _ted(
                "Lithuania – Transformers – 110/10/10 kV 40 MVA 2 vnt galios transformatorių pirkimas",
                ["31170000"],
                ["supplies"],
            ),
            None,
            ["equipment"],
            ["equipment_suppliers"],
        ),
        # A solar EPC with grid connection and O&M: works and operations.
        (
            {
                "source_id": "gb.find_a_tender",
                "kind": "tender",
                "title": "GBE EPC, Grid Connection (ICO) and Operations and Maintenance Services",
                "identifiers": {"cpv": ["09330000", "09331200", "45251100", "50710000", "71323100"]},
            },
            {"tender": {"mainProcurementCategory": "works"}},
            ["works", "operations"],
            ["contractors"],
        ),
        # Design-build: the design codes are the contractor's own, not a services contract.
        (
            _ted(
                "Poland – Construction work for power plant – Wykonanie robót budowlanych w "
                "formule zaprojektuj i "
                "wybuduj: Projektowanie, dostawa i budowa instalacji fotowoltaicznej",
                ["09332000", "45100000", "45251100", "71323100"],
                ["works"],
            ),
            None,
            ["works"],
            ["contractors"],
        ),
        # Engineering services outside a works contract: services, for consultants.
        (
            _ted(
                "Sweden – Architectural, construction, engineering and inspection services "
                "– Tekniska konsulttjänster El",
                ["71000000", "71314000"],
                ["services"],
            ),
            None,
            ["services"],
            ["consultants"],
        ),
        # A fuel purchase is read and is none of the types: other.
        (
            _ted(
                "Greece – Petroleum products – Προμήθεια υγρών καυσίμων",
                ["09000000", "09132100"],
                ["supplies"],
            ),
            None,
            ["other"],
            [],
        ),
        # A waste-collection services contract is not engineering: other.
        (
            _ted(
                "France – Refuse and waste related services – Prestation de collecte des pneus",
                ["19521000", "90500000"],
                ["services"],
            ),
            None,
            ["other"],
            [],
        ),
        # Maintenance of transformer stations: operations.
        (
            _ted(
                "Spain – Repair and maintenance services of transformers – Mantenimiento "
                "Preventivo de los Centros de Transformación",
                ["31100000", "50532200"],
                ["services"],
            ),
            None,
            ["operations"],
            ["contractors"],
        ),
        # Supply and installation: works and equipment.
        (
            _ted(
                "Slovenia – Medium-voltage switchboards – Dobava in montaža 20 kV celic za "
                "RTP 110/20 kV Lendava",
                ["31214520", "51112000"],
                ["services", "supplies"],
            ),
            None,
            ["works", "equipment"],
            ["contractors", "equipment_suppliers"],
        ),
        # A short-term PPA bought by a public buyer is its electricity supply, not a project
        # (review of 2026-10-24: it read as a project for developers) ...
        (
            _ted("Germany – Electricity – Kurzfrist-PPA", ["09310000"], ["supplies"]),
            None,
            ["energy_supply"],
            ["energy_suppliers"],
        ),
        # ... while a PPA from a named new plant is a project, and the supply it buys.
        (
            {
                **_ted(
                    "Germany – Electricity – PPA für Strom aus einem neuen Solarpark",
                    ["09310000"],
                    ["supplies"],
                ),
                "technologies": ["solar_pv"],
            },
            None,
            ["project", "energy_supply"],
            ["developers", "energy_suppliers"],
        ),
        # World Bank: the procurement group, then the bid reference when the row is not at hand.
        (
            {
                "source_id": "mdb.worldbank.procnotices",
                "kind": "tender",
                "title": "Social Specialist",
                "identifiers": {},
            },
            {"procurement_group": "CS", "procurement_method_name": "Individual Consultant Selection"},
            ["services"],
            ["consultants"],
        ),
        (
            {
                "source_id": "mdb.worldbank.procnotices",
                "kind": "tender",
                "title": "Procurement of Line Installation Contractors - (Batch 2) -Lots 9-11",
                "identifiers": {"bid_reference_no": "UG-MEMD-550339-CW-RFB"},
            },
            None,
            ["works"],
            ["contractors"],
        ),
        (
            {
                "source_id": "mdb.worldbank.procnotices",
                "kind": "tender",
                "title": "Supply of Aerial Bundled Cable (ABC)",
                "identifiers": {},
            },
            {"procurement_group": "GO"},
            ["equipment"],
            ["equipment_suppliers"],
        ),
        # A utility's capacity RFP: a project, for developers and IPPs.
        (
            {"source_id": "us.rfp.pso", "kind": "rfp", "title": "2025 SPP Capacity RFP", "identifiers": {}},
            {
                "contract_types": [],
                "description": "to obtain up to 350 megawatts (MW) of accredited capacity",
            },
            ["project"],
            ["developers"],
        ),
        # A funding opportunity whose eligibility the connector did not keep: funding, no audience.
        (
            {
                "source_id": "us.grants_gov.search2",
                "kind": "foa",
                "title": "Grid Resilience Utility and Industry Grants",
                "identifiers": {},
            },
            None,
            ["funding"],
            [],
        ),
        # The labelled sample (data/eval/opportunity_technology_tags.csv, 2026-10-24):
        # a request for information on a funding register awards nothing: other.
        (
            {
                "source_id": "us.grants_gov.search2",
                "kind": "foa",
                "title": "Request for Information - Gauging Investor Interest in the Development and "
                "Commercialization of Subsurface Energy Technologies",
                "identifiers": {},
            },
            None,
            ["other"],
            [],
        ),
        # a design-build is works, whatever nature the buyer ticked;
        (
            _ted(
                "Czechia – Solar photovoltaic modules – Realizace fotovoltaických elektráren metodou "
                "Design & Build na sedmi objektech AOPK ČR",
                ["09331200", "09332000", "71320000"],
                ["services", "supplies"],
            ),
            None,
            ["works"],
            ["contractors"],
        ),
        # supply-and-install in a World Bank works contract buys the plant too;
        (
            {
                "source_id": "mdb.worldbank.procnotices",
                "kind": "tender",
                "title": "Procurement of Plant Design, Supply, Installation, test and Commissioning of "
                "transmission lines associated with substation of Injibara and Debark",
                "identifiers": {"bid_reference_no": "ET-EEP-533125-CW-RFB"},
            },
            None,
            ["works", "equipment"],
            ["contractors", "equipment_suppliers"],
        ),
        # the other goods in a fuel framework are its sundries, not equipment;
        (
            _ted(
                "Spain – Diesel oil – Acuerdo Marco para el Suministro de Combustibles en Estaciones "
                "de Servicio",
                ["09132000", "09134100", "24322220"],
                ["supplies"],
            ),
            None,
            ["other"],
            [],
        ),
        # a maintenance code in a works contract is the build's own scope ...
        (
            _ted(
                "Poland – Wastewater treatment plant construction work – Budowa centrum produkcji zielonej "
                "energii z wykorzystaniem gospodarki obiegu zamkniętego",
                ["45252127", "45300000", "50500000"],
                ["works"],
            ),
            None,
            ["works"],
            ["contractors"],
        ),
        # ... unless the title names operation or maintenance;
        (
            _ted(
                "France – Restructuring work – Marché public global de performance énergétique de conception "
                "réalisation exploitation maintenance relatif aux bâtiments du Centre Hospitalier Valvert",
                ["45259900", "45454000", "50700000"],
                ["works"],
            ),
            None,
            ["works", "operations"],
            ["contractors"],
        ),
        # and a supply phrase in a works contract is its purpose, not an energy purchase.
        (
            _ted(
                "Poland – Generator units – Zasilanie rezerwowe dla zapewnienia ciągłości dostaw energii "
                "elektrycznej w sytuacjach kryzysowych",
                ["31122000", "45000000", "45310000"],
                ["supplies", "works"],
            ),
            None,
            ["works", "equipment"],
            ["contractors", "equipment_suppliers"],
        ),
    ],
)
def test_work_profile(
    notice: dict[str, Any], raw: dict[str, Any] | None, work: list[str], audiences: list[str]
) -> None:
    profile = work_profile(notice, raw)
    assert profile.work_type_keys == work
    assert profile.audience_keys == audiences
    assert all(tag.evidence for tag in profile.work_types + profile.audiences)
    assert work_profile(notice, raw) == profile  # pure


def test_evidence_names_the_field_and_value() -> None:
    profile = work_profile(
        _ted("Poland – Electricity – Dostawa energii elektrycznej", ["09310000"], ["supplies"])
    )
    (tag,) = profile.work_types
    assert tag.evidence == ("CPV 09310000", 'title: "dostawa energii elektrycznej"')
    (audience,) = profile.audiences
    assert audience.evidence == ("work type: energy_supply",)
    assert profile.as_json() == {
        "work_types": [
            {"key": "energy_supply", "evidence": ["CPV 09310000", 'title: "dostawa energii elektrycznej"']}
        ],
        "audiences": [{"key": "energy_suppliers", "evidence": ["work type: energy_supply"]}],
    }


def test_a_notice_nothing_can_read_is_unknown() -> None:
    notice = {
        "source_id": "mdb.worldbank.procnotices",
        "kind": "tender",
        "title": "LRP - Livelihood Restoration Plan - AQI",
        "identifiers": {"bid_reference_no": "159B/TREP/2025"},
    }
    profile = work_profile(notice)
    assert profile.unknown and profile.work_types == () and profile.audiences == ()


def test_identifiers_may_be_the_stored_json_text() -> None:
    notice = {
        "source_id": "eu.ted.api",
        "kind": "tender",
        "title": "x – y – Lieferung",
        "identifiers": json.dumps({"cpv": ["31170000"], "contract_nature": ["supplies"]}),
    }
    assert work_profile(notice, "{}").work_type_keys == ["equipment"]


def _grants_raw(opportunity_id: str) -> dict[str, Any]:
    """A Grants.gov row with the `fetchOpportunity` detail the recorded fixture holds. The
    connector keeps only the funding figures of the detail today (`detail_subset`), so this is
    what the reader would see if it also kept `applicantTypes` and `fundingInstruments`."""
    doc = json.loads(fixture_path("grants_gov_fetch_opportunity.json").read_text(encoding="utf-8"))
    data = doc[opportunity_id]["data"]
    return {"detail": {k: data[k] for k in ("synopsis", "forecast") if k in data}}


def test_funding_audiences_come_from_the_eligible_applicant_types() -> None:
    notice = {
        "source_id": "us.grants_gov.search2",
        "kind": "foa",
        "title": "Rural Energy for America Program",
        "identifiers": {},
    }
    # Small businesses (23), others (25: unread, see the text) and tribal organisations (11); a grant.
    profile = work_profile(notice, _grants_raw("279638"))
    assert profile.work_type_keys == ["funding"]
    assert profile.audience_keys == ["developers", "for_profit", "tribal"]
    assert any(e.startswith("funding instrument: G") for e in profile.work_types[0].evidence)
    unrestricted = work_profile(notice, _grants_raw("356623"))  # 99 unrestricted, a cooperative agreement
    assert unrestricted.audience_keys == ["developers", "for_profit", "public_body", "non_profit", "tribal"]
    utilities = eligible_applicants(_grants_raw("363396"))  # "RUS will accept ... from Operating Utilities"
    assert [t.key for t in utilities] == ["utility"]


def test_a_procurement_contract_instrument_is_not_funding() -> None:
    raw = {
        "detail": {"synopsis": {"fundingInstruments": [{"id": "PC", "description": "Procurement Contract"}]}}
    }
    notice = {
        "source_id": "us.grants_gov.search2",
        "kind": "foa",
        "title": "Engineering support",
        "identifiers": {},
    }
    assert work_profile(notice, raw).work_type_keys == ["services"]


def test_the_vocabularies_and_the_one_mapping_table_agree() -> None:
    assert set(WORK_TYPE_AUDIENCES) == set(WORK_TYPES)
    mapped = {a for audiences in WORK_TYPE_AUDIENCES.values() for a in audiences}
    assert mapped <= set(AUDIENCES)
    assert set(APPLICANT_TYPES) <= set(AUDIENCES)
    assert WORK_TYPE_AUDIENCES["works"] == WORK_TYPE_AUDIENCES["operations"] == ("contractors",)
    # Who funding is for lives in the same place: the applicant-type table, not in work_profile.
    assert set(APPLICANT_AUDIENCES) == set(APPLICANT_TYPES)
    assert {a for keys in APPLICANT_AUDIENCES.values() for a in keys} <= set(AUDIENCES)
    assert APPLICANT_AUDIENCES["for_profit"] == ("developers", "for_profit")


# ------------------------------------------------------------------- review of 2026-10-24
# Each case below failed on the lane's first round.


def _wb(title: str, reference: str = "") -> dict[str, Any]:
    return {
        "source_id": "mdb.worldbank.procnotices",
        "kind": "tender",
        "title": title,
        "identifiers": {"bid_reference_no": reference} if reference else {},
    }


@pytest.mark.parametrize(
    ("notice", "work"),
    [
        # Finding 3: design-build-operate is works and operations.
        (
            _ted(
                "Germany – Solar energy – Design, build and operate a 20 MW solar park",
                ["45000000"],
                ["works"],
            ),
            ["works", "operations"],
        ),
        (
            _ted("Germany – Construction work – DBO contract for a biomass heating plant", [], ["works"]),
            ["works", "operations"],
        ),
        (
            _ted(
                "Germany – Construction work – Planung, Errichtung und Betrieb einer PV-Anlage", [], ["works"]
            ),
            ["works", "operations"],
        ),
        (
            _wb("Design, Build, Operate and Maintain (DBOM) of solar mini-grids", "MW-ESCOM-1-CW-RFB"),
            ["works", "operations"],
        ),
        # ... and design-build alone, or works "during operation", is not operations.
        (_ted("Germany – Construction work – Design and build of a PV plant", [], ["works"]), ["works"]),
        (
            _ted("Germany – Construction work – Sanierung der Schule im laufenden Betrieb", [], ["works"]),
            ["works"],
        ),
    ],
)
def test_design_build_operate_is_works_and_operations(notice: dict[str, Any], work: list[str]) -> None:
    assert work_profile(notice).work_type_keys == work


@pytest.mark.parametrize(
    ("notice", "work"),
    [
        # Finding 4: World Bank goods that are not plant and equipment are `other`.
        (
            _wb("FOURNITURE DE 09 VEHICULES 4X4 PICK-UP POUR LE SUIVI", "MG-DECIM-P178701-562718-GO-RFB"),
            ["other"],
        ),
        (_wb("FOURNITURE DE 60 MOTOCROSS", "MG-DECIM-P178701-562707-GO-RFB"), ["other"]),
        (_wb("FOURNITURE DES MATERIELS INFORMATIQUES", "MG-DECIM-P178701-562724-GO-RFB"), ["other"]),
        (_wb("Acquisition des sachets biodégradables pour pépinières", "CD-PDL-1-GO-RFQ"), ["other"]),
        (_wb("MHCPP Camp Furniture, Kitchen Utensils, Gym Equipment", "LS-MHCPP-1-GO-RFQ"), ["other"]),
        # a title-only purchase of anything but plant ("refreshments")
        (_wb("Adquisición de Refrigerio"), ["other"]),
        # supply and installation of software is neither construction nor plant
        (_wb("Supply and Installation of PSSE Software Power System Modeling", "ET-EEP-1-GO-RFB"), ["other"]),
        # Plant named in a goods notice is equipment.
        (
            _wb(
                "Fourniture de câbles électriques pour 50 900 branchements", "MG-DECIM-P178701-558062-GO-RFB"
            ),
            ["equipment"],
        ),
        (_wb("Supply of distribution transformers", "UG-UETCL-1-GO-RFB"), ["equipment"]),
        (_wb("Supply of solar home systems", "NG-REA-1-GO-RFB"), ["equipment"]),
        # TED and Find a Tender supply contracts: equipment codes only, never other goods.
        (
            _ted(
                "France – Stationery – Marché papeterie et fournitures de bureau",
                ["30192000", "30192700"],
                ["supplies"],
            ),
            ["other"],
        ),
        (
            _ted(
                "Poland – Pharmaceutical products – Dostawa produktów leczniczych",
                ["33600000", "33140000"],
                ["supplies"],
            ),
            ["other"],
        ),
        (_ted("Poland – Motor vehicles – Dostawa samochodów", ["34100000"], ["supplies"]), ["other"]),
        (
            _ted("Germany – Poles – Supply of wooden poles for overhead lines", ["44212200"], ["supplies"]),
            ["equipment"],
        ),
    ],
)
def test_goods_are_equipment_only_when_they_are_plant(notice: dict[str, Any], work: list[str]) -> None:
    profile = work_profile(notice)
    assert profile.work_type_keys == work
    if work == ["other"]:
        assert "equipment_suppliers" not in profile.audience_keys


def test_another_sources_rfp_kind_is_not_by_itself_a_project() -> None:
    """Nit: `kind in ('rfp', 'auction')` made any future source's RFP a project for developers,
    with only "kind: rfp" as evidence. Only the curated utility RFP registers are projects by
    source; another source's notice is read like any other, and unknown when nothing reads it."""
    notice = {
        "source_id": "us.example.rfps",
        "kind": "rfp",
        "title": "Janitorial services",
        "identifiers": {},
    }
    assert work_profile(notice).work_type_keys == []
    assert work_profile({**notice, "title": "Power purchase agreement for a wind farm"}).work_type_keys == [
        "project"
    ]
    utility = {"source_id": "us.rfp.pso", "kind": "rfp", "title": "2026 RFPs", "identifiers": {}}
    assert work_profile(utility, {"contract_types": []}).work_types[0].evidence == (
        "source: utility RFP register",
    )


def test_a_capacity_word_alone_is_not_a_capacity_solicitation() -> None:
    """Finding 1, the type-of-work side: "solicitation: capacity" was any "capacity" word."""
    notice = {
        "source_id": "us.rfp.indiana_michigan_power",
        "kind": "rfp",
        "title": "2024 RFPs",
        "identifiers": {},
    }
    raw = {"contract_types": [], "description": "by the 2029/30 capacity planning year"}
    (tag,) = work_profile(notice, raw).work_types
    assert tag.evidence == ("source: utility RFP register",)
    capacity = {**notice, "title": "2025 SPP Capacity RFP", "technologies": [ALL_SOURCE]}
    (tag,) = work_profile(capacity, raw).work_types
    assert tag.evidence == ("technology: all_source", 'title: "Capacity RFP"')


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Nit: explicit grid words survive lighting and a rail word that names no railway ...
        ("Replacement of 132kV substation at Railway Road", ["transmission"]),
        ("Electricity distribution network reinforcement and street lighting", ["transmission"]),
        ("33kV overhead line refurbishment including guard rail replacement", ["transmission"]),
        ("Modernizacja sieci elektroenergetycznej SN i oświetlenia ulicznego", ["transmission"]),
        # ... while rail and traction still veto them, and lighting still vetoes a voltage alone.
        ("Substation works for the West Midlands Metro extension", []),
        ("Railway electrification 25 kV overhead line", []),
        ("Opravy elektronických ochran rozvaděčů 22 kV v el. stanicích metra", []),
        ("Modernizacja oświetlenia ulicznego wraz z przebudową linii kablowej SN 15 kV", []),
        # Nit: heat in the buyer's own words, and solar heat is not PV.
        ("Lieferung und Montage von GFK-Rohrleitungen - Projekt Flusswärmepumpe 3 Mannheim", ["heat"]),
        ("Fernkältezentrale HKW Süd, 2. Bauabschnitt", ["heat"]),
        ("Доставка на топлинна енергия за ТП на НОИ", ["heat"]),
        ("Impianto solare termico per il teleriscaldamento di Dogliani", ["solar_thermal", "heat"]),
        ("Installation of solar thermal collectors and PV panels", ["solar_thermal", "solar_pv"]),
        # a building's heat supply installations are not a heat network
        ("Abwasser-, Wasser, Wärmeversorgungs-, Kälte- und Feuerlöschanlagen", []),
    ],
)
def test_review_keyword_cases(text: str, expected: list[str]) -> None:
    assert classify_technologies(text) == expected


def test_solar_thermal_is_an_opportunity_technology() -> None:
    assert "solar_thermal" in OPPORTUNITY_TECHNOLOGIES


def test_the_funding_audience_needs_eligibility_the_grants_connector_does_not_keep() -> None:
    """Finding 5, pinned: `us.grants_gov.search2` keeps only `DETAIL_BLOCK_KEYS` of the
    opportunity detail, so a stored Grants.gov notice has no applicant types and its funding names
    no audience. When the connector keeps `applicantTypes` and `fundingInstruments` (handed to the
    coordinator), this test changes and the funding audience exists."""
    from pipeline.connectors.us_grants_gov_search2.connector import DETAIL_BLOCK_KEYS, detail_subset

    assert not {"applicantTypes", "fundingInstruments", "applicantEligibilityDesc"} & set(DETAIL_BLOCK_KEYS)
    doc = json.loads(fixture_path("grants_gov_fetch_opportunity.json").read_text(encoding="utf-8"))
    kept = detail_subset(doc["279638"], "2026-10-24T00:00:00Z", {})
    notice = {
        "source_id": "us.grants_gov.search2",
        "kind": "foa",
        "title": "Rural Energy for America Program",
    }
    profile = work_profile(notice, {"detail": kept})
    assert profile.work_type_keys == ["funding"]
    assert profile.audiences == ()
    # The same notice with the eligibility Grants.gov states reaches developers through the table.
    assert work_profile(notice, _grants_raw("279638")).audience_keys == ["developers", "for_profit", "tribal"]


def test_audiences_for_reads_the_two_tables_only() -> None:
    works = (Tag("works", ("CPV 45000000",)),)
    assert [t.key for t in audiences_for(works)] == ["contractors"]
    funding = (Tag("funding", ("kind: foa",)),)
    applicants = [Tag("for_profit", ("applicant type 22: For profit organizations",))]
    out = audiences_for(funding, applicants)
    assert [t.key for t in out] == ["developers", "for_profit"]
    assert out[0].evidence == ("applicant type 22: For profit organizations",)


def test_work_profile_columns_are_the_proposed_stored_form() -> None:
    columns = work_profile_columns(
        _ted("Poland – Electricity – Dostawa energii elektrycznej", ["09310000"], ["supplies"])
    )
    assert columns == {
        "work_types": ["energy_supply"],
        "audiences": ["energy_suppliers"],
        "work_evidence": {
            "work_types": {"energy_supply": ["CPV 09310000", 'title: "dostawa energii elektrycznej"']},
            "audiences": {"energy_suppliers": ["work type: energy_supply"]},
        },
    }
