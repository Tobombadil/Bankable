"""`services/match/engine.py`: every branch of the four rules and the v2 eligibility gate, the score,
the rationale in US-402 AC1's shape, and the blocking keys `services/match/run.py` relies on being
exact."""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools

import pytest

from services.match.engine import (
    OpportunityFacts,
    ProposalFacts,
    format_mw,
    jurisdiction_country,
    opportunity_families,
    opportunity_tokens,
    proposal_families,
    score_pair,
)
from services.match.rules import load_rules

NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=dt.UTC)
RULES = load_rules()


def _p(**kw: object) -> ProposalFacts:
    base: dict[str, object] = {
        "technology": "storage",
        "jurisdiction": "US-TX",
        "capacity_mw": 200.0,
        "lifecycle_state": "studied",
    }
    base.update(kw)
    return ProposalFacts(**base)  # type: ignore[arg-type]


def _o(**kw: object) -> OpportunityFacts:
    base: dict[str, object] = {
        "technologies": ["bess"],
        "jurisdiction": "US-TX",
        "capacity_sought_mw": 500.0,
        "status": "open",
        "due_at": NOW + dt.timedelta(days=45),
    }
    base.update(kw)
    return OpportunityFacts(**base)  # type: ignore[arg-type]


def test_the_us_402_example_renders_exactly() -> None:
    result = score_pair(_p(), _o(), RULES, now=NOW)
    assert result.is_match
    assert result.rationale_text == "storage, TX, 50–550 MW, due in 45 days"
    assert result.score == 1.0
    assert result.rule_set_version == "match-rules@v2"
    rationale = result.rationale
    # v2: the eligibility gate is a fifth check; it is never in the credits (it has no weight) and
    # its label stays out of the rationale text when it passes.
    assert rationale["rules_passed"] == ["technology", "jurisdiction", "size_window", "timing", "eligibility"]
    assert rationale["rules_failed"] == []
    assert rationale["features"]["days_to_due"] == 45
    assert rationale["features"]["capacity_ratio"] == 0.4
    assert rationale["features"]["credits"] == {
        "technology": 1.0,
        "jurisdiction": 1.0,
        "size_window": 1.0,
        "timing": 1.0,
    }
    assert rationale["features"]["notice_class"] == "unclassified"
    assert rationale["features"]["notice_relevance"] == "technology tag: bess"


def test_defaults_to_the_committed_rules_and_the_clock() -> None:
    far = _o(due_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=400))
    assert score_pair(_p(), far).is_match


# ------------------------------------------------------------------------------------ technology
def test_pipe_joined_tokens_are_split() -> None:
    assert opportunity_tokens(["solar_pv|Nuclear", " solar_pv ", "", "gas"]) == ["solar_pv", "nuclear", "gas"]
    result = score_pair(_p(technology="nuclear"), _o(technologies=["solar_pv|nuclear"]), RULES, now=NOW)
    assert "technology" in result.rules_passed


@pytest.mark.parametrize(
    ("proposal_tech", "opp_techs", "passed", "label"),
    [
        ("solar", ["all_source"], True, "solar"),
        (None, ["any"], True, "any technology"),
        ("unknown", ["bess"], False, "technology unknown"),
        (None, ["bess"], False, "technology unknown"),
        ("storage", [], False, "opportunity technology not stated"),
        ("wind", ["solar_pv"], False, "technology mismatch (wind vs solar_pv)"),
        ("solar_storage", ["bess"], True, "storage"),
        # v2: `ccs` meets the Class VI wells; `microgrid`, `metering` and `load` are excluded by name,
        # and an excluded proposal token does not ride an all-source notice.
        ("co2_geologic_sequestration", ["ccs"], True, "ccs"),
        ("load", ["all_source"], False, "load not matched (excluded)"),
        ("load", ["transmission"], False, "load not matched (excluded)"),
        ("solar_storage", ["microgrid"], False, "microgrid not matched (excluded)"),
        ("storage", ["metering|microgrid"], False, "metering, microgrid not matched (excluded)"),
        ("storage", ["microgrid|bess"], True, "storage"),
    ],
)
def test_technology_rule(proposal_tech: str | None, opp_techs: list[str], passed: bool, label: str) -> None:
    result = score_pair(_p(technology=proposal_tech), _o(technologies=opp_techs), RULES, now=NOW)
    outcome = result.outcomes[0]
    assert outcome.name == "technology"
    assert outcome.passed is passed
    assert outcome.label == label


# ---------------------------------------------------------------------------------- jurisdiction
@pytest.mark.parametrize(
    ("proposal_j", "opp_j", "passed", "credit", "relation", "label"),
    [
        ("US-TX", "US-TX", True, 1.0, "exact", "TX"),
        ("US-TX", "US", True, 0.8, "national", "US-wide"),
        ("US", "US-TX", True, 0.5, "unresolved", "TX (US region unknown)"),
        ("US-NV", "US-AZ", True, 0.5, "adjacent", "NV/AZ adjacent"),
        ("US-NV", "US-TX", False, 0.0, "different_region", "NV outside TX"),
        ("GB", "PL", False, 0.0, "different_country", "GB vs PL"),
        ("", "US", False, 0.0, "different_country", "? vs US"),
    ],
)
def test_jurisdiction_rule(
    proposal_j: str, opp_j: str, passed: bool, credit: float, relation: str, label: str
) -> None:
    outcome = score_pair(_p(jurisdiction=proposal_j), _o(jurisdiction=opp_j), RULES, now=NOW).outcomes[1]
    assert (outcome.passed, outcome.credit, outcome.label) == (passed, credit, label)
    assert outcome.features["jurisdiction_relation"] == relation


@pytest.mark.parametrize(
    ("kind", "credit", "relation", "label"),
    [
        # A national programme is country-wide ...
        ("foa", 0.8, "national", "GB-wide"),
        ("auction", 0.8, "national", "GB-wide"),
        # ... a single buyer's tender that states only its country is not (v2 rule J).
        ("tender", 0.5, "notice_region_unknown", "GB (notice region not stated)"),
        ("procurement_notice", 0.5, "notice_region_unknown", "GB (notice region not stated)"),
        ("rfp", 0.5, "notice_region_unknown", "GB (notice region not stated)"),
        # No kind (a caller with only the four rules' facts): v1's reading.
        (None, 0.8, "national", "GB-wide"),
    ],
)
def test_rule_j_country_only_procurement_is_region_unknown(
    kind: str | None, credit: float, relation: str, label: str
) -> None:
    outcome = score_pair(_p(jurisdiction="GB"), _o(jurisdiction="GB", kind=kind), RULES, now=NOW).outcomes[1]
    assert (outcome.passed, outcome.credit, outcome.label) == (True, credit, label)
    assert outcome.features["jurisdiction_relation"] == relation


def test_rule_j_moves_a_regionless_open_ended_tender_below_the_threshold() -> None:
    """The shape of the 424 wind matches of `gb.find_a_tender` ocds-h6vhtk-066b6b under v1 (docs/22
    §25.5): wind against a GB wind tender, both sides without a region, no size, open with no
    deadline. v1 credited the tender GB-wide (0.765); v2 scores it 0.675 < 0.70. With a stated
    deadline it scores 0.775 and matches again: rule J weakens the evidence, it does not exclude."""
    tender = _o(
        technologies=["wind"],
        jurisdiction="GB",
        capacity_sought_mw=None,
        due_at=None,
        kind="tender",
        title="Onshore Wind Energy Development – Northumberland",
    )
    wind = _p(technology="wind", jurisdiction="GB", capacity_mw=None, lifecycle_state="filed")
    result = score_pair(wind, tender, RULES, now=NOW)
    assert result.all_rules_pass and result.score == 0.675 and not result.is_match
    with_deadline = dataclasses.replace(tender, due_at=NOW + dt.timedelta(days=30))
    assert score_pair(wind, with_deadline, RULES, now=NOW).score == 0.775
    assert score_pair(wind, with_deadline, RULES, now=NOW).is_match
    v1_reading = dataclasses.replace(tender, kind=None)
    assert score_pair(wind, v1_reading, RULES, now=NOW).score == 0.765


# ------------------------------------------------------------------------------------ eligibility
def test_the_gate_fails_people_notices_whatever_the_score() -> None:
    """docs/22 §21.3: the scholarship notice scored exactly what the real match scored (0.865)."""
    nuclear = _p(technology="nuclear", jurisdiction="US-TX", capacity_mw=1117.0, lifecycle_state="announced")
    common = {"technologies": ["nuclear"], "jurisdiction": "US", "capacity_sought_mw": None, "kind": "foa"}
    scholarship = _o(
        title="University Nuclear Leadership Program– Scholarship and Fellowship Support", **common
    )
    licensing = _o(title="Advanced Nuclear Energy Licensing Cost-Share Grant Program", **common)
    wrong = score_pair(nuclear, scholarship, RULES, now=NOW)
    right = score_pair(nuclear, licensing, RULES, now=NOW)
    assert wrong.score == right.score == 0.865
    assert right.is_match and right.rules_failed == ()
    assert not wrong.is_match and wrong.rules_failed == ("eligibility",)
    assert wrong.rationale_text == (
        "not project-eligible: funds or hires people, not projects (Scholarship); "
        "nuclear, US-wide, size not stated, due in 45 days"
    )
    assert wrong.rationale["features"]["notice_class"] == "people"
    assert "eligibility" not in wrong.rationale["features"]["credits"]


@pytest.mark.parametrize(
    ("title", "codes", "label"),
    [
        (
            "Nova Education Trust Contract for Supply of Biomass Boiler Fuel Pellets",
            ["cpv:09110000"],
            "not energy-relevant (fuel purchase: cpv:09110000)",
        ),
        (
            "NIEHS Worker Training Program’s HAZMAT Training at DOE Nuclear Weapons Complex",
            [],
            "not energy-relevant (false friend only: nuclear weapons)",
        ),
        (
            "FW Supply & Deliver – LV & MV Cables – Multi Site 1232",
            ["cpv:31321200"],
            "not project-eligible: buys goods or services for the buyer, which suppliers bid, not projects "
            "(Supply &)",
        ),
    ],
)
def test_the_gate_names_its_reason(title: str, codes: list[str], label: str) -> None:
    gate = score_pair(_p(), _o(title=title, programme_codes=codes), RULES, now=NOW).outcomes[4]
    assert (gate.name, gate.passed, gate.credit, gate.label) == ("eligibility", False, 0.0, label)


def test_relevance_can_be_switched_off_in_the_rule_file() -> None:
    gate_cfg = RULES.eligibility
    assert gate_cfg is not None
    lenient = dataclasses.replace(
        RULES, eligibility=dataclasses.replace(gate_cfg, require_energy_relevance=False)
    )
    fuel = _o(title="Contract for Supply of Biomass Boiler Fuel Pellets", programme_codes=["cpv:09110000"])
    gate = score_pair(_p(), fuel, lenient, now=NOW).outcomes[4]
    assert "notice_relevant" not in gate.features
    assert gate.label.startswith("not project-eligible: buys goods or services")


def test_a_rule_file_without_the_gate_scores_as_v1() -> None:
    v1_like = dataclasses.replace(RULES, eligibility=None)
    result = score_pair(_p(), _o(title="Scholarship programme"), v1_like, now=NOW)
    assert [o.name for o in result.outcomes] == ["technology", "jurisdiction", "size_window", "timing"]
    assert result.is_match and result.rationale_text == "storage, TX, 50–550 MW, due in 45 days"


# ----------------------------------------------------------------------------------- size window
@pytest.mark.parametrize(
    ("capacity", "sought", "passed", "credit", "label"),
    [
        (200.0, None, True, 0.5, "size not stated"),
        (200.0, 0.0, True, 0.5, "size not stated"),
        (None, 500.0, True, 0.5, "50–550 MW (capacity unknown)"),
        (20.0, 500.0, False, 0.0, "20 MW outside 50–550 MW"),
        (1000.0, 1000.0, True, 1.0, "100–1,100 MW"),
        (1200.0, 1000.0, False, 0.0, "1,200 MW outside 100–1,100 MW"),
        (12.5, 25.0, True, 1.0, "2.5–27.5 MW"),
    ],
)
def test_size_window_rule(
    capacity: float | None, sought: float | None, passed: bool, credit: float, label: str
) -> None:
    outcome = score_pair(_p(capacity_mw=capacity), _o(capacity_sought_mw=sought), RULES, now=NOW).outcomes[2]
    assert (outcome.passed, outcome.credit, outcome.label) == (passed, credit, label)


def test_format_mw() -> None:
    assert format_mw(50) == "50"
    assert format_mw(1200) == "1,200"
    assert format_mw(12.5) == "12.5"


# ---------------------------------------------------------------------------------------- timing
@pytest.mark.parametrize(
    ("state", "status", "due_days", "passed", "credit", "label"),
    [
        ("withdrawn", "open", 10, False, 0.0, "proposal withdrawn"),
        ("studied", "closed", 10, False, 0.0, "closed"),
        ("studied", "awarded", None, False, 0.0, "awarded"),
        ("studied", "open", -3, False, 0.0, "past due (3 days ago)"),
        ("studied", "open", 0, True, 1.0, "due today"),
        ("studied", "unknown", 30, True, 0.5, "due in 30 days"),
        ("studied", "unknown", None, True, 0.5, "status unknown"),
        ("studied", "open", None, True, 0.5, "open, no deadline"),
        ("studied", "announced", 5, True, 1.0, "due in 5 days"),
    ],
)
def test_timing_rule(
    state: str, status: str, due_days: int | None, passed: bool, credit: float, label: str
) -> None:
    due = NOW + dt.timedelta(days=due_days) if due_days is not None else None
    outcome = score_pair(_p(lifecycle_state=state), _o(status=status, due_at=due), RULES, now=NOW).outcomes[3]
    assert (outcome.passed, outcome.credit, outcome.label) == (passed, credit, label)


def test_naive_due_date_is_read_as_utc() -> None:
    due = (NOW + dt.timedelta(days=2)).replace(tzinfo=None)
    assert score_pair(_p(), _o(due_at=due), RULES, now=NOW).outcomes[3].label == "due in 2 days"


# ----------------------------------------------------------------------------------------- score
def test_every_rule_must_pass_whatever_the_score() -> None:
    result = score_pair(_p(jurisdiction="US-CA"), _o(), RULES, now=NOW)
    assert result.rules_failed == ("jurisdiction",)
    assert not result.all_rules_pass
    assert not result.is_match
    assert "CA outside TX" in result.rationale_text


def test_a_pass_below_threshold_is_not_a_match() -> None:
    strict = dataclasses.replace(RULES, threshold=0.99)
    weak = score_pair(_p(), _o(capacity_sought_mw=None, due_at=None), strict, now=NOW)
    assert weak.all_rules_pass
    assert weak.score == pytest.approx((0.35 + 0.30 + 0.15 * 0.5 + 0.20 * 0.5) / 1.0, abs=1e-3)
    assert not weak.is_match


# -------------------------------------------------------------------------------------- blocking
def test_blocking_keys_are_exact_against_brute_force() -> None:
    """A pair outside the blocking keys never passes both the technology and jurisdiction rules,
    over a grid of every kind of value the rules distinguish."""
    proposals = [
        _p(technology=t, jurisdiction=j)
        for t, j in itertools.product(
            ["storage", "solar", "solar_storage", "gas_cc", "unknown", None], ["US-TX", "US", "GB", ""]
        )
    ]
    opportunities = [
        _o(technologies=t, jurisdiction=j)
        for t, j in itertools.product(
            [["bess"], ["solar_pv|gas"], [], ["all_source"], ["nuclear"]], ["US-TX", "US", "PL", "GB"]
        )
    ]
    for p, o in itertools.product(proposals, opportunities):
        result = score_pair(p, o, RULES, now=NOW)
        both = "technology" in result.rules_passed and "jurisdiction" in result.rules_passed
        families = opportunity_families(o, RULES)
        blocked_in = jurisdiction_country(p.jurisdiction) == jurisdiction_country(o.jurisdiction) and (
            families is None or bool(proposal_families(p, RULES) & families)
        )
        if both:
            assert blocked_in, (p, o)
