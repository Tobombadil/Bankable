"""Score one proposal <-> opportunity pair against the versioned rule set (docs/10 US-401 AC1,
US-402 AC1). Pure functions over two small fact records: no session, no clock of its own (`now`
is a parameter), no I/O -- `services/match/run.py` feeds it ORM rows, `services/match/eval.py`
feeds it CSV rows, the tests feed it literals.

The four rules -- technology, jurisdiction, size window, timing -- each yield a `RuleOutcome`
(pass/fail, a credit in [0, 1], a short label, the values it looked at). A pair is a match when
every rule passes AND the weighted score reaches `RuleSet.threshold`; the score is a confidence,
never a substitute for a failed rule. `rationale` is the stored JSON (`{rules_passed, rules_failed,
features}`, docs/21 §3.11) and `rationale_text` is the plain-language line in the shape US-402 AC1
gives ("storage, TX, 50–500 MW, due in 45 days"), one label per rule in rule order.

From match-rules@v2 a rule file may also carry the **eligibility gate** (`rules.eligibility`,
docs/22 §25): the notice must be about energy (`services/match/relevance.py`) and must fund or buy
something a project can apply to or bid with itself (`services/match/eligibility.py`). The gate
passes or fails and has no weight, so it never moves a score; a failed gate is a failed rule, and
its label leads the rationale text ("not project-eligible: funds or hires people, not projects
(Scholarship); nuclear, US-wide, ...").
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from services.match.eligibility import classify_notice
from services.match.relevance import assess_relevance
from services.match.rules import RULE_NAMES, RuleSet, load_rules

EN_DASH = "–"


@dataclass(frozen=True)
class ProposalFacts:
    technology: str | None
    jurisdiction: str
    capacity_mw: float | None
    lifecycle_state: str


@dataclass(frozen=True)
class OpportunityFacts:
    technologies: Sequence[str]
    jurisdiction: str
    capacity_sought_mw: float | None
    status: str
    due_at: dt.datetime | None
    # What the eligibility gate reads (match-rules@v2). Defaulted so a caller that only has the
    # four rules' facts still builds one; with no title, summary or codes the gate sees no
    # exclusion and decides relevance from the technology tags alone.
    title: str | None = None
    summary: str | None = None
    #: `opportunity.kind` (`foa`, `tender`, ...): the jurisdiction rule's `national_kinds`.
    kind: str | None = None
    #: `services.match.relevance.programme_codes(identifiers)`: `cpv:...`, `aln:...`, ...
    programme_codes: Sequence[str] = ()


@dataclass(frozen=True)
class RuleOutcome:
    name: str
    passed: bool
    credit: float
    label: str
    features: dict[str, Any]


@dataclass(frozen=True)
class MatchResult:
    rule_set_version: str
    score: float
    rules_passed: tuple[str, ...]
    rules_failed: tuple[str, ...]
    outcomes: tuple[RuleOutcome, ...]
    rationale_text: str
    threshold: float

    @property
    def all_rules_pass(self) -> bool:
        return not self.rules_failed

    @property
    def is_match(self) -> bool:
        return self.all_rules_pass and self.score >= self.threshold

    @property
    def rationale(self) -> dict[str, Any]:
        features: dict[str, Any] = {}
        for outcome in self.outcomes:
            features.update(outcome.features)
        # Credits are the score's inputs: the weighted rules only, never a gate.
        features["credits"] = {o.name: o.credit for o in self.outcomes if o.name in RULE_NAMES}
        features["threshold"] = self.threshold
        return {
            "rules_passed": list(self.rules_passed),
            "rules_failed": list(self.rules_failed),
            "features": features,
        }


# ------------------------------------------------------------------------------------ formatting
def format_mw(value: float) -> str:
    """`50`, `1,200`, `12.5` -- whole megawatts without a decimal, otherwise one decimal."""
    rounded = round(value, 1)
    if rounded == int(rounded):
        return f"{int(rounded):,}"
    return f"{rounded:,.1f}"


def _split_jurisdiction(code: str) -> tuple[str, str | None]:
    code = (code or "").strip().upper()
    if "-" in code:
        country, region = code.split("-", 1)
        return country, region or None
    return code, None


# ----------------------------------------------------------------------------------------- rules
def opportunity_tokens(technologies: Sequence[str]) -> list[str]:
    """The opportunity's technology tokens, lower-cased, de-duplicated in order. The extractors
    store a multi-technology notice as one pipe-joined element (`["solar_pv|nuclear"]`, 12 TED
    rows and 22 World Bank rows in data/normalized on 2026-09-13), so each element is split on
    `|` before it is looked up; a clean list passes through unchanged."""
    out: list[str] = []
    for raw in technologies:
        for token in (raw or "").split("|"):
            token = token.strip().lower()
            if token and token not in out:
                out.append(token)
    return out


# ------------------------------------------------------------------------------------- blocking keys
# `services/match/run.py` scores only pairs that share a country and a technology family (or an
# all-source opportunity): every other pair fails the jurisdiction or the technology rule by
# construction, so blocking on these keys is exact, not an approximation. The keys are computed
# here, next to the rules they mirror, and `test_engine.py` pins blocked == brute force.
def jurisdiction_country(code: str | None) -> str:
    return _split_jurisdiction(code or "")[0]


def proposal_families(proposal: ProposalFacts, rules: RuleSet) -> frozenset[str]:
    token = (proposal.technology or "").strip().lower()
    return rules.technology.families_of.get(token, frozenset()) if token else frozenset()


def opportunity_families(opportunity: OpportunityFacts, rules: RuleSet) -> frozenset[str] | None:
    """The opportunity's technology families, or `None` for an all-source opportunity (which
    passes the technology rule against any proposal in its country)."""
    tokens = opportunity_tokens(opportunity.technologies)
    if any(t in rules.technology.all_source_tokens for t in tokens):
        return None
    families: set[str] = set()
    for token in tokens:
        families |= rules.technology.families_of.get(token, frozenset())
    return frozenset(families)


def technology_rule(proposal: ProposalFacts, opportunity: OpportunityFacts, rules: RuleSet) -> RuleOutcome:
    families_of = rules.technology.families_of
    excluded = rules.technology.excluded_tokens
    prop_token = (proposal.technology or "").strip().lower() or None
    opp_tokens = opportunity_tokens(opportunity.technologies)
    features: dict[str, Any] = {
        "proposal_technology": prop_token,
        "opportunity_technologies": opp_tokens,
    }
    prop_families = families_of.get(prop_token, frozenset()) if prop_token else frozenset()
    # Excluded tokens (match-rules@v2 `excluded_tokens`) fail with their own reason, and before
    # the all-source pass: a data-centre load does not answer an all-source generation call.
    if prop_token is not None and prop_token in excluded:
        features["technology_family"] = None
        return RuleOutcome("technology", False, 0.0, f"{prop_token} not matched (excluded)", features)
    if opp_tokens and all(t in excluded for t in opp_tokens):
        features["technology_family"] = None
        label = f"{', '.join(opp_tokens)} not matched (excluded)"
        return RuleOutcome("technology", False, 0.0, label, features)
    if any(t in rules.technology.all_source_tokens for t in opp_tokens):
        label = prop_token or "any technology"
        features["technology_family"] = "all_source"
        return RuleOutcome("technology", True, 1.0, label, features)
    if not prop_families:
        features["technology_family"] = None
        return RuleOutcome("technology", False, 0.0, "technology unknown", features)
    if not opp_tokens:
        features["technology_family"] = None
        return RuleOutcome("technology", False, 0.0, "opportunity technology not stated", features)
    opp_families: set[str] = set()
    for token in opp_tokens:
        opp_families |= families_of.get(token, frozenset())
    shared = sorted(prop_families & opp_families)
    if shared:
        features["technology_family"] = shared[0]
        return RuleOutcome("technology", True, 1.0, shared[0], features)
    features["technology_family"] = None
    label = f"technology mismatch ({prop_token} vs {', '.join(opp_tokens)})"
    return RuleOutcome("technology", False, 0.0, label, features)


def jurisdiction_rule(proposal: ProposalFacts, opportunity: OpportunityFacts, rules: RuleSet) -> RuleOutcome:
    p_country, p_region = _split_jurisdiction(proposal.jurisdiction)
    o_country, o_region = _split_jurisdiction(opportunity.jurisdiction)
    features: dict[str, Any] = {
        "proposal_jurisdiction": proposal.jurisdiction,
        "opportunity_jurisdiction": opportunity.jurisdiction,
    }
    cfg = rules.jurisdiction
    if not p_country or not o_country or p_country != o_country:
        features["jurisdiction_relation"] = "different_country"
        return RuleOutcome("jurisdiction", False, 0.0, f"{p_country or '?'} vs {o_country or '?'}", features)
    if o_region is None:
        national_kinds = cfg.national_kinds
        if national_kinds is None or opportunity.kind is None or opportunity.kind in national_kinds:
            features["jurisdiction_relation"] = "national"
            return RuleOutcome("jurisdiction", True, cfg.national_credit, f"{o_country}-wide", features)
        # A single buyer's notice (a tender) that states only its country: the region was not
        # extracted, it is not a country-wide programme (match-rules@v2, docs/22 §25.2 rule J).
        features["jurisdiction_relation"] = "notice_region_unknown"
        label = f"{o_country} (notice region not stated)"
        return RuleOutcome("jurisdiction", True, cfg.unresolved_credit, label, features)
    if p_region is None:
        features["jurisdiction_relation"] = "unresolved"
        label = f"{o_region} ({p_country} region unknown)"
        return RuleOutcome("jurisdiction", True, cfg.unresolved_credit, label, features)
    if p_region == o_region:
        features["jurisdiction_relation"] = "exact"
        return RuleOutcome("jurisdiction", True, 1.0, p_region, features)
    if opportunity.jurisdiction.strip().upper() in cfg.adjacency.get(
        proposal.jurisdiction.strip().upper(), ()
    ):
        features["jurisdiction_relation"] = "adjacent"
        return RuleOutcome(
            "jurisdiction", True, cfg.adjacent_credit, f"{p_region}/{o_region} adjacent", features
        )
    features["jurisdiction_relation"] = "different_region"
    return RuleOutcome("jurisdiction", False, 0.0, f"{p_region} outside {o_region}", features)


def size_window_rule(proposal: ProposalFacts, opportunity: OpportunityFacts, rules: RuleSet) -> RuleOutcome:
    cfg = rules.size_window
    sought = opportunity.capacity_sought_mw
    capacity = proposal.capacity_mw
    features: dict[str, Any] = {"capacity_mw": capacity, "capacity_sought_mw": sought}
    if sought is None or sought <= 0:
        features.update({"window_min_mw": None, "window_max_mw": None, "capacity_ratio": None})
        return RuleOutcome("size_window", True, cfg.unstated_credit, "size not stated", features)
    low, high = sought * cfg.lower_ratio, sought * cfg.upper_ratio
    window = f"{format_mw(low)}{EN_DASH}{format_mw(high)} MW"
    features.update({"window_min_mw": round(low, 3), "window_max_mw": round(high, 3)})
    if capacity is None or capacity <= 0:
        features["capacity_ratio"] = None
        return RuleOutcome("size_window", True, cfg.unstated_credit, f"{window} (capacity unknown)", features)
    features["capacity_ratio"] = round(capacity / sought, 3)
    if low <= capacity <= high:
        return RuleOutcome("size_window", True, 1.0, window, features)
    return RuleOutcome("size_window", False, 0.0, f"{format_mw(capacity)} MW outside {window}", features)


def timing_rule(
    proposal: ProposalFacts, opportunity: OpportunityFacts, rules: RuleSet, *, now: dt.datetime
) -> RuleOutcome:
    cfg = rules.timing
    status = (opportunity.status or "unknown").strip().lower()
    features: dict[str, Any] = {
        "opportunity_status": status,
        "proposal_lifecycle_state": proposal.lifecycle_state,
        "days_to_due": None,
    }
    if proposal.lifecycle_state in cfg.proposal_excluded_states:
        return RuleOutcome("timing", False, 0.0, f"proposal {proposal.lifecycle_state}", features)
    if status != "unknown" and status not in cfg.open_statuses:
        return RuleOutcome("timing", False, 0.0, status, features)
    due = opportunity.due_at
    if due is not None:
        due_aware = due if due.tzinfo is not None else due.replace(tzinfo=dt.UTC)
        days = (due_aware.astimezone(dt.UTC).date() - now.astimezone(dt.UTC).date()).days
        features["days_to_due"] = days
        if days < 0:
            return RuleOutcome("timing", False, 0.0, f"past due ({-days} days ago)", features)
        label = "due today" if days == 0 else f"due in {days} days"
        credit = cfg.unknown_status_credit if status == "unknown" else 1.0
        return RuleOutcome("timing", True, credit, label, features)
    if status == "unknown":
        return RuleOutcome("timing", True, cfg.unknown_status_credit, "status unknown", features)
    return RuleOutcome("timing", True, cfg.no_deadline_credit, "open, no deadline", features)


def eligibility_rule(opportunity: OpportunityFacts, rules: RuleSet) -> RuleOutcome | None:
    """The gate (`rules.eligibility`), or `None` when the rule file has none (v1)."""
    cfg = rules.eligibility
    if cfg is None:
        return None
    features: dict[str, Any] = {}
    if cfg.require_energy_relevance:
        relevance = assess_relevance(
            title=opportunity.title,
            summary=opportunity.summary,
            technologies=opportunity.technologies,
            programme_codes=opportunity.programme_codes,
        )
        features.update(
            {
                "notice_relevant": relevance.relevant,
                "notice_relevance": relevance.label,
                "relevance_version": relevance.version,
            }
        )
        if not relevance.relevant:
            label = f"not energy-relevant ({relevance.label})"
            return RuleOutcome("eligibility", False, 0.0, label, features)
    verdict = classify_notice(
        title=opportunity.title,
        summary=opportunity.summary,
        programme_codes=opportunity.programme_codes,
        rule=cfg,
    )
    features.update({"notice_class": verdict.notice_class, "notice_class_evidence": verdict.evidence})
    if not verdict.eligible:
        label = f"not project-eligible: {verdict.reason} ({verdict.evidence})"
        return RuleOutcome("eligibility", False, 0.0, label, features)
    return RuleOutcome("eligibility", True, 1.0, "project-eligible", features)


# ---------------------------------------------------------------------------------------- scoring
def score_pair(
    proposal: ProposalFacts,
    opportunity: OpportunityFacts,
    rules: RuleSet | None = None,
    *,
    now: dt.datetime | None = None,
) -> MatchResult:
    """Every rule's outcome, the weighted score and the rendered rationale for one pair."""
    rules = rules or load_rules()
    now = now or dt.datetime.now(dt.UTC)
    weighted_outcomes = (
        technology_rule(proposal, opportunity, rules),
        jurisdiction_rule(proposal, opportunity, rules),
        size_window_rule(proposal, opportunity, rules),
        timing_rule(proposal, opportunity, rules, now=now),
    )
    gate = eligibility_rule(opportunity, rules)
    outcomes = (*weighted_outcomes, gate) if gate is not None else weighted_outcomes
    weighted = sum(rules.weights[o.name] * o.credit for o in weighted_outcomes)
    score = round(weighted / rules.weight_total, 3)
    rationale_text = ", ".join(o.label for o in weighted_outcomes)
    if gate is not None and not gate.passed:
        rationale_text = f"{gate.label}; {rationale_text}"
    return MatchResult(
        rule_set_version=rules.version,
        score=score,
        rules_passed=tuple(o.name for o in outcomes if o.passed),
        rules_failed=tuple(o.name for o in outcomes if not o.passed),
        outcomes=outcomes,
        rationale_text=rationale_text,
        threshold=rules.threshold,
    )


__all__ = [
    "EN_DASH",
    "MatchResult",
    "OpportunityFacts",
    "ProposalFacts",
    "RuleOutcome",
    "eligibility_rule",
    "format_mw",
    "jurisdiction_country",
    "jurisdiction_rule",
    "opportunity_families",
    "opportunity_tokens",
    "proposal_families",
    "score_pair",
    "size_window_rule",
    "technology_rule",
    "timing_rule",
]
