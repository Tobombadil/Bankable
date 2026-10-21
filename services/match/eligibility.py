"""What does a notice fund or buy, and can a project apply to it? (docs/22 §25.2.)

The classification behind match-rules@v2's eligibility gate. docs/22 §21.3 found that every false
positive of v1 had the right technology and the wrong object: a scholarship programme, a worker
training programme, a staff-support grant. They fund people, not projects, and no threshold
separates them, because they score exactly as high as the real matches. The independent labels of
2026-10-20 added a second kind on their tune half: a utility's framework to buy cables, and a water
utility's framework for control panels and integration services -- suppliers bid those, not
projects.

`classify_notice` reads the notice's title and summary and its programme codes and returns the
first class of `rules.eligibility.classes` (`data/match_rules.yaml`) that claims it. A class claims
a notice when one of its `terms` occurs in the title or summary, or when the notice states CPV codes
and every one of them falls under the class's `cpv_all_in` prefixes. A notice no class claims is
eligible (`unclassified`): the gate removes what it can name and nothing else.

Pure functions, no I/O. The term lists live in the rule file, so a change to them is a change of
`rule_set_version` like any other rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from services.match.relevance import codes_in
from services.match.rules import EligibilityRule

UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Eligibility:
    notice_class: str
    eligible: bool
    reason: str
    #: The text or code that put the notice in its class; `None` when unclassified.
    evidence: str | None


def classify_notice(
    *,
    title: str | None,
    summary: str | None = None,
    programme_codes: Sequence[str] = (),
    rule: EligibilityRule,
) -> Eligibility:
    text = " ".join(t for t in (title, summary) if t)
    cpv = codes_in("cpv", programme_codes)
    for notice_class in rule.classes:
        for pattern in notice_class.terms:
            hit = pattern.search(text)
            if hit:
                return Eligibility(
                    notice_class.name, notice_class.eligible, notice_class.reason, hit.group(0)
                )
        if cpv and notice_class.cpv_all_in and all(c.startswith(notice_class.cpv_all_in) for c in cpv):
            return Eligibility(notice_class.name, notice_class.eligible, notice_class.reason, f"cpv:{cpv[0]}")
    return Eligibility(UNCLASSIFIED, True, "no exclusion applies", None)


__all__ = ["UNCLASSIFIED", "Eligibility", "classify_notice"]
