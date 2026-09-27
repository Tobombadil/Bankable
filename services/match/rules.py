"""The versioned rule set (docs/10 US-401 AC1: "the rule set is a versioned config file").

`data/match_rules.yaml` is the only place a weight, a threshold, a technology family or a
jurisdiction adjacency lives; this module parses it once into a frozen `RuleSet` and validates the
parts the engine relies on, so a typo in the file fails at load time with a named field rather
than as a silently-neutral rule. Nothing here reads a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_PATH = REPO_ROOT / "data" / "match_rules.yaml"

RULE_NAMES = ("technology", "jurisdiction", "size_window", "timing")


class RuleSetError(ValueError):
    """The rules file is missing or malformed; the field name is in the message."""


@dataclass(frozen=True)
class TechnologyRule:
    #: token -> family name, built from the `families` table (a token may sit in several families:
    #: `solar_storage` is both `solar` and `storage`, so the value is a frozenset).
    families_of: MappingProxyType[str, frozenset[str]]
    all_source_tokens: frozenset[str]


@dataclass(frozen=True)
class JurisdictionRule:
    national_credit: float
    unresolved_credit: float
    adjacent_credit: float
    adjacency: MappingProxyType[str, frozenset[str]]


@dataclass(frozen=True)
class SizeWindowRule:
    lower_ratio: float
    upper_ratio: float
    unstated_credit: float


@dataclass(frozen=True)
class TimingRule:
    open_statuses: frozenset[str]
    unknown_status_credit: float
    no_deadline_credit: float
    proposal_excluded_states: frozenset[str]


@dataclass(frozen=True)
class RuleSet:
    version: str
    threshold: float
    weights: MappingProxyType[str, float]
    technology: TechnologyRule
    jurisdiction: JurisdictionRule
    size_window: SizeWindowRule
    timing: TimingRule
    path: Path
    #: Whether matches produced by this rule set may be shown to anyone but an operator
    #: (`publish` in the file; absent means `False`, so a rule set is withheld until someone says
    #: otherwise). Read by `services/api/matches.py` and `services/match/run.py`'s event stamps.
    publish: bool = False

    @property
    def weight_total(self) -> float:
        return sum(self.weights.values())


def _credit(section: dict[str, Any], key: str, where: str) -> float:
    try:
        value = float(section[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuleSetError(f"{where}.{key} must be a number in [0, 1]") from exc
    if not 0.0 <= value <= 1.0:
        raise RuleSetError(f"{where}.{key} must be in [0, 1], got {value}")
    return value


def _section(doc: dict[str, Any], *keys: str) -> dict[str, Any]:
    node: Any = doc
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            raise RuleSetError(f"missing section {'.'.join(keys)}")
        node = node[key]
    if not isinstance(node, dict):
        raise RuleSetError(f"section {'.'.join(keys)} must be a mapping")
    return node


def parse_rules(doc: dict[str, Any], *, path: Path = DEFAULT_RULES_PATH) -> RuleSet:
    version = doc.get("rule_set_version")
    if not isinstance(version, str) or not version.strip():
        raise RuleSetError("rule_set_version must be a non-empty string")
    threshold = _credit(doc, "threshold", "rules")
    publish = doc.get("publish", False)
    if not isinstance(publish, bool):
        raise RuleSetError("publish must be true or false")

    weights_raw = _section(doc, "weights")
    weights: dict[str, float] = {}
    for name in RULE_NAMES:
        try:
            weights[name] = float(weights_raw[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuleSetError(f"weights.{name} must be a number") from exc
        if weights[name] <= 0:
            raise RuleSetError(f"weights.{name} must be positive")

    tech = _section(doc, "rules", "technology")
    families_raw = tech.get("families")
    if not isinstance(families_raw, dict) or not families_raw:
        raise RuleSetError("rules.technology.families must be a non-empty mapping")
    families_of: dict[str, set[str]] = {}
    for family, tokens in families_raw.items():
        if not isinstance(tokens, list) or not tokens:
            raise RuleSetError(f"rules.technology.families.{family} must be a non-empty list")
        for token in tokens:
            families_of.setdefault(str(token), set()).add(str(family))
    all_source = tech.get("all_source_tokens") or []
    technology = TechnologyRule(
        families_of=MappingProxyType({k: frozenset(v) for k, v in families_of.items()}),
        all_source_tokens=frozenset(str(t) for t in all_source),
    )

    jur = _section(doc, "rules", "jurisdiction")
    adjacency_raw = jur.get("adjacency") or {}
    if not isinstance(adjacency_raw, dict):
        raise RuleSetError("rules.jurisdiction.adjacency must be a mapping")
    adjacency = {str(k): frozenset(str(x) for x in (v or [])) for k, v in adjacency_raw.items()}
    for code, neighbours in adjacency.items():
        for other in neighbours:
            if code not in adjacency.get(other, frozenset()):
                raise RuleSetError(f"rules.jurisdiction.adjacency is not symmetric: {code} -> {other}")
    jurisdiction = JurisdictionRule(
        national_credit=_credit(jur, "national_credit", "rules.jurisdiction"),
        unresolved_credit=_credit(jur, "unresolved_credit", "rules.jurisdiction"),
        adjacent_credit=_credit(jur, "adjacent_credit", "rules.jurisdiction"),
        adjacency=MappingProxyType(adjacency),
    )

    size = _section(doc, "rules", "size_window")
    try:
        lower, upper = float(size["lower_ratio"]), float(size["upper_ratio"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuleSetError("rules.size_window.lower_ratio/upper_ratio must be numbers") from exc
    if not 0 < lower < upper:
        raise RuleSetError("rules.size_window needs 0 < lower_ratio < upper_ratio")
    size_window = SizeWindowRule(
        lower_ratio=lower,
        upper_ratio=upper,
        unstated_credit=_credit(size, "unstated_credit", "rules.size_window"),
    )

    timing_raw = _section(doc, "rules", "timing")
    open_statuses = timing_raw.get("open_statuses")
    if not isinstance(open_statuses, list) or not open_statuses:
        raise RuleSetError("rules.timing.open_statuses must be a non-empty list")
    timing = TimingRule(
        open_statuses=frozenset(str(s) for s in open_statuses),
        unknown_status_credit=_credit(timing_raw, "unknown_status_credit", "rules.timing"),
        no_deadline_credit=_credit(timing_raw, "no_deadline_credit", "rules.timing"),
        proposal_excluded_states=frozenset(
            str(s) for s in (timing_raw.get("proposal_excluded_states") or [])
        ),
    )

    return RuleSet(
        version=version.strip(),
        threshold=threshold,
        publish=publish,
        weights=MappingProxyType(weights),
        technology=technology,
        jurisdiction=jurisdiction,
        size_window=size_window,
        timing=timing,
        path=path,
    )


def load_rules_from(path: Path) -> RuleSet:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuleSetError(f"cannot read rule set at {path}: {exc}") from exc
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise RuleSetError(f"rule set at {path} is not a mapping")
    return parse_rules(doc, path=path)


@lru_cache(maxsize=4)
def _cached(path: str) -> RuleSet:
    return load_rules_from(Path(path))


def load_rules(path: Path | None = None) -> RuleSet:
    """The committed rule set, parsed once per process (`lru_cache` on the resolved path). Pass a
    path to load another file, as `services/match/eval.py --rules` and the tests do."""
    return _cached(str((path or DEFAULT_RULES_PATH).resolve()))


__all__ = [
    "DEFAULT_RULES_PATH",
    "RULE_NAMES",
    "JurisdictionRule",
    "RuleSet",
    "RuleSetError",
    "SizeWindowRule",
    "TechnologyRule",
    "TimingRule",
    "load_rules",
    "load_rules_from",
    "parse_rules",
]
