"""`services/match/engine.py`: every branch of the four rules, the score, the rationale in US-402
AC1's shape, and the blocking keys `services/match/run.py` relies on being exact."""

from __future__ import annotations

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
    assert result.rule_set_version == "match-rules@v1"
    rationale = result.rationale
    assert rationale["rules_passed"] == ["technology", "jurisdiction", "size_window", "timing"]
    assert rationale["rules_failed"] == []
    assert rationale["features"]["days_to_due"] == 45
    assert rationale["features"]["capacity_ratio"] == 0.4
    assert rationale["features"]["credits"] == {
        "technology": 1.0,
        "jurisdiction": 1.0,
        "size_window": 1.0,
        "timing": 1.0,
    }


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
    import dataclasses

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
