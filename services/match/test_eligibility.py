"""`services/match/eligibility.py` over the committed `rules.eligibility` classes: each class claims
what it says, the classes are tried in file order, CPV codes claim a notice only when every code
falls in the class, and the notices v2 must leave alone (the true matches) stay eligible."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml

from services.match.eligibility import UNCLASSIFIED, classify_notice
from services.match.rules import DEFAULT_RULES_PATH, EligibilityRule, load_rules, parse_rules


def _rule() -> EligibilityRule:
    rule = load_rules().eligibility
    assert rule is not None, "the committed rule set has no eligibility gate"
    return rule


def test_committed_class_order() -> None:
    assert [c.name for c in _rule().classes] == [
        "people",
        "project_offtake",
        "research",
        "studies_planning",
        "goods_services",
    ]
    assert [c.eligible for c in _rule().classes] == [False, True, False, False, False]
    assert _rule().require_energy_relevance is True


@pytest.mark.parametrize(
    ("title", "codes", "notice_class", "evidence"),
    [
        # people -- the coordinator's a-priori categories, and the tune-half "Delegates" notice
        (
            "University Nuclear Leadership Program– Scholarship and Fellowship Support",
            [],
            "people",
            "Scholarship",
        ),
        (
            "NIEHS Worker Training Program’s HAZMAT Training at DOE Nuclear Weapons Complex",
            [],
            "people",
            "Training",
        ),
        ("Staff Support to Tribal Radioactive Materials Transportation Committee", [], "people", "Staff"),
        (
            "CSP26751 - Delegates for International Energy Agency Technology Collaboration Programme",
            [],
            "people",
            "Delegates",
        ),
        ("Lot 3", ["cpv:80530000"], "people", "cpv:80530000"),
        # project offtake is tried before the exclusions it would otherwise fall into
        (
            "Supply of electricity under a 15-year power purchase agreement",
            [],
            "project_offtake",
            "power purchase",
        ),
        ("All-source request for proposals", [], "project_offtake", "All-source"),
        # ... but after people: a course about PPAs still funds people
        ("Training on PPA negotiation for municipal staff", [], "people", "Training"),
        # research
        ("Research Interests of the Air Force Office of Scientific Research", [], "research", "Research"),
        (
            "NRL Long Range Broad Agency Announcement (BAA) for Basic and Applied Research",
            [],
            "research",
            "Research",
        ),
        ("Lot 2", ["cpv:73110000", "cpv:73210000"], "research", "cpv:73110000"),
        # studies and planning
        ("Feasibility study for a 50 MW solar plant", [], "studies_planning", "study"),
        (
            "Provision of Energy Performance Certificate (EPC) Assessments",
            [],
            "studies_planning",
            "Assessments",
        ),
        # goods and services the buyer consumes -- the two tune-half framework notices
        (
            "FW Supply & Deliver – LV & MV Cables – Multi Site 1232",
            ["cpv:31321200"],
            "goods_services",
            "Supply &",
        ),
        (
            "Provision of Motor Control Centres, Systems Integration and ICA Services",
            [],
            "goods_services",
            "Provision of",
        ),
        ("Civils Contractors (Minor Works) Framework Agreement", [], "goods_services", "Framework"),
        (
            "Fire Safety and Security Systems Servicing and Reactive Maintenance",
            [],
            "goods_services",
            "Maintenance",
        ),
    ],
)
def test_classes_claim_what_they_name(title: str, codes: list[str], notice_class: str, evidence: str) -> None:
    verdict = classify_notice(title=title, programme_codes=codes, rule=_rule())
    assert (verdict.notice_class, verdict.evidence) == (notice_class, evidence)
    assert verdict.eligible is (notice_class == "project_offtake")


@pytest.mark.parametrize(
    ("title", "codes"),
    [
        # The true matches of both labelled samples: v2 must not touch them.
        ("Advanced Nuclear Energy Licensing Cost-Share Grant Program", ["aln:81.121"]),
        # Deployment programmes with nothing to exclude.
        ("Renewable Energy Systems and Energy Efficiency Improvements Program", ["aln:10.868"]),
        # A CPV claim needs every code in the class: R&D next to power-plant works is not research.
        ("Lot 4", ["cpv:73110000", "cpv:45251100"]),
        # What v2 does not catch (docs/22 §25.6): a landowner's call for a development partner.
        ("Onshore Wind Energy Development – Northumberland", ["cpv:09310000", "cpv:45200000"]),
        (None, []),
    ],
)
def test_unclassified_notices_are_eligible(title: str | None, codes: list[str]) -> None:
    verdict = classify_notice(title=title, programme_codes=codes, rule=_rule())
    assert (verdict.notice_class, verdict.eligible, verdict.evidence) == (UNCLASSIFIED, True, None)


def test_summary_is_read_when_present() -> None:
    verdict = classify_notice(title="Lot 1", summary="Supply of LV cables to depots", rule=_rule())
    assert verdict.notice_class == "goods_services"


def test_first_class_in_file_order_wins() -> None:
    doc: dict[str, Any] = copy.deepcopy(yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8")))
    classes = doc["rules"]["eligibility"]["classes"]
    title = "Research and training framework"  # research, people and goods_services all apply
    assert classify_notice(title=title, rule=_rule()).notice_class == "people"
    doc["rules"]["eligibility"]["classes"] = [c for c in classes if c["name"] != "people"]
    rule = parse_rules(doc).eligibility
    assert rule is not None
    assert classify_notice(title=title, rule=rule).notice_class == "research"
    doc["rules"]["eligibility"]["classes"] = list(reversed(doc["rules"]["eligibility"]["classes"]))
    rule = parse_rules(doc).eligibility
    assert rule is not None
    assert classify_notice(title=title, rule=rule).notice_class == "goods_services"
