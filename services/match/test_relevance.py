"""`services/match/relevance.py`: every branch of the verdict, its order, the programme-code reader
for each opportunity connector's `identifiers`, and the ORM-row entry point `services/api` will use
for the docs/16 U-7 default view."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.match.relevance import (
    RELEVANCE_VERSION,
    assess_relevance,
    is_energy_relevant,
    opportunity_relevance,
    programme_codes,
)


@pytest.mark.parametrize(
    ("title", "technologies", "codes", "relevant", "reason", "evidence"),
    [
        # 1. Fuel purchases (docs/16 §4.5): by CPV (every code a fuel) or by the purchase's object.
        ("Heating oil", [], ["cpv:09135100"], False, "fuel purchase", "cpv:09135100"),
        ("Diesel fuel", ["gas"], ["cpv:09134100", "cpv:09211000"], False, "fuel purchase", "cpv:09134100"),
        ("Supply of diesel fuel to depots", [], [], False, "fuel purchase", "Supply of diesel"),
        (
            "Supply and delivery of heating oil",
            [],
            [],
            False,
            "fuel purchase",
            "Supply and delivery of heating oil",
        ),
        # ... but a fuel CPV beside an energy one is not a pure fuel purchase,
        ("Fuel and power", [], ["cpv:09134100", "cpv:09310000"], True, "programme code", "cpv:09310000"),
        # ... and fuel cells, diesel generators and gensets are equipment.
        ("Supply of fuel cells for buses", [], [], True, "keyword", "fuel cells"),
        ("Supply and installation of diesel generators", [], [], True, "keyword", "generators"),
        # 2. Positive evidence.
        (
            "Advanced Nuclear Energy Licensing Cost-Share Grant Program",
            ["nuclear"],
            ["aln:81.121"],
            True,
            "keyword",
            "energy",
        ),
        ("HVPS Cabling", [], ["cpv:31320000"], True, "programme code", "cpv:31320000"),
        ("Lot 1", [], ["wb_sector:LT", "wb_sector:LP"], True, "programme code", "wb_sector:LT"),
        ("Test RFP 1", ["bess"], [], True, "technology tag", "bess"),
        ("Test RFP 1", ["solar_pv|nuclear"], [], True, "technology tag", "solar_pv"),
        ("Rural electrification and mini-grids", [], [], True, "keyword", "grids"),
        # 3. False friends: the energy word is not about energy, and a tag read from it is no evidence.
        ("DIRECTED ENERGY TECHNOLOGY EXPERIMENTATION", [], [], False, "false friend only", "directed energy"),
        (
            "NIEHS Worker Training Program’s HAZMAT Training at DOE Nuclear Weapons Complex",
            ["nuclear"],
            [],
            False,
            "false friend only",
            "nuclear weapons",
        ),
        ("Subsonic wind tunnel upgrade", ["wind"], [], False, "false friend only", "wind tunnel"),
        # 4. Nothing about energy (docs/16: the "DoW Military Burn" grants that opened the list).
        ("DoW Military Burn, Discovery Award", [], ["aln:12.420"], False, "no energy evidence", None),
        ("", [], [], False, "no energy evidence", None),
    ],
)
def test_verdicts(
    title: str,
    technologies: list[str],
    codes: list[str],
    relevant: bool,
    reason: str,
    evidence: str | None,
) -> None:
    verdict = assess_relevance(title=title, technologies=technologies, programme_codes=codes)
    assert (verdict.relevant, verdict.reason, verdict.evidence) == (relevant, reason, evidence)
    assert verdict.version == RELEVANCE_VERSION
    assert is_energy_relevant(title=title, technologies=technologies, programme_codes=codes) is relevant


def test_summary_is_read_too() -> None:
    assert not is_energy_relevant(title="Contract 1234")
    assert is_energy_relevant(title="Contract 1234", summary="A 50 MW battery storage system")


def test_label() -> None:
    assert assess_relevance(title="Solar farm").label == "keyword: solar"
    assert assess_relevance(title="Catering").label == "no energy evidence"


@pytest.mark.parametrize(
    ("identifiers", "codes"),
    [
        (None, ()),
        ({}, ()),
        # gb.find_a_tender
        (
            {"ocid": "ocds-x", "cpv": ["09310000", "31121320"], "tender_id": "T1"},
            ("cpv:09310000", "cpv:31121320"),
        ),
        # us.grants_gov.search2
        (
            {"grants_gov_number": "DE-FOA-0003339", "aln": ["81.121"], "agency_code": "DOE-ID"},
            ("aln:81.121",),
        ),
        # mdb.worldbank.procnotices
        ({"wb_notice_id": "OP1", "sector_codes": ["LT", "LZ"]}, ("wb_sector:LT", "wb_sector:LZ")),
        # eu.ted.api: contract nature may repeat per lot; codes are de-duplicated in order
        (
            {"cpv": ["45223000", "45223000"], "contract_nature": ["works", "works"]},
            ("cpv:45223000", "nature:works"),
        ),
        ({"contract_nature": "supplies", "cpv": None}, ("nature:supplies",)),
    ],
)
def test_programme_codes(identifiers: dict[str, object] | None, codes: tuple[str, ...]) -> None:
    assert programme_codes(identifiers) == codes


def test_opportunity_relevance_reads_an_orm_shaped_row() -> None:
    fuel = SimpleNamespace(
        title="Nova Education Trust Contract for Supply of Biomass Boiler Fuel Pellets",
        summary=None,
        technologies=["biomass"],
        identifiers={"cpv": ["09110000"]},
    )
    verdict = opportunity_relevance(fuel)
    assert (verdict.relevant, verdict.reason, verdict.evidence) == (False, "fuel purchase", "cpv:09110000")
    bare = SimpleNamespace(title="Wind farm grid connection")
    assert opportunity_relevance(bare).relevant
