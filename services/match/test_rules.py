"""`services/match/rules.py`: the committed rule file loads, and every malformed part fails at load
time with the field named, never as a silently neutral rule. Also: every technology token either
side can carry is in a family or deliberately excluded (match-rules@v2, docs/22 §25.2)."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from pipeline.connectors.opportunity import KINDS, OPPORTUNITY_TECHNOLOGIES
from pipeline.normalize import TECH_RULES
from services.match.rules import (
    DEFAULT_RULES_PATH,
    GATE_NAMES,
    REPO_ROOT,
    RULE_NAMES,
    RuleSetError,
    load_rules,
    load_rules_from,
    parse_rules,
)


@pytest.fixture()
def doc() -> dict[str, Any]:
    return yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))


def test_committed_rule_file_loads() -> None:
    rules = load_rules()
    assert rules.version == "match-rules@v3"
    assert 0 < rules.threshold <= 1
    assert set(rules.weights) == set(RULE_NAMES)
    assert not set(GATE_NAMES) & set(rules.weights)  # a gate never carries a weight
    assert rules.weight_total == pytest.approx(sum(rules.weights.values()))
    assert "storage" in rules.technology.families_of["bess"]
    # `solar_storage` sits in two families at once.
    assert rules.technology.families_of["solar_storage"] >= {"solar", "storage"}
    assert "US-NV" in rules.jurisdiction.adjacency["US-AZ"]
    assert rules.path == DEFAULT_RULES_PATH.resolve()
    # v2 additions
    assert rules.eligibility is not None and len(rules.eligibility.classes) == 5
    assert rules.technology.families_of["co2_geologic_sequestration"] == {"ccs"}
    assert rules.technology.families_of["ccs"] == {"ccs"}
    assert rules.technology.excluded_tokens == {"microgrid", "metering", "load"}
    assert rules.jurisdiction.national_kinds == {"foa", "program", "loan_program", "auction"}


#: Placeholders, not technologies: they can never be in a family and need not be excluded.
_PLACEHOLDERS = frozenset({"other", "unknown"})
_CONNECTOR_TECHNOLOGY = re.compile(r'"technology":\s*"([a-z0-9_]+)"')


def _proposal_vocabulary() -> set[str]:
    """Every token a proposal can carry: the classifier's outputs (`pipeline.normalize.TECH_RULES`)
    and the literals connectors write without it (`"technology": "co2_geologic_sequestration"`)."""
    tokens = {tech for _, tech, _ in TECH_RULES}
    for path in (REPO_ROOT / "pipeline" / "connectors").rglob("*.py"):
        if not path.name.startswith("test_"):
            tokens.update(_CONNECTOR_TECHNOLOGY.findall(path.read_text(encoding="utf-8")))
    return tokens


@pytest.mark.parametrize(
    ("side", "vocabulary"),
    [("opportunity", lambda: set(OPPORTUNITY_TECHNOLOGIES)), ("proposal", _proposal_vocabulary)],
)
def test_every_technology_token_is_mapped_or_deliberately_excluded(side: str, vocabulary: Any) -> None:
    """v1 left `ccs`, `microgrid`, `metering` (opportunity) and `load`, `co2_geologic_sequestration`
    (proposal) in no family, so a pair on them failed as an unexplained mismatch."""
    rules = load_rules()
    tokens = vocabulary()
    assert len(tokens) > 10, f"{side}: the vocabulary read too few tokens, so this test proves nothing"
    covered = set(rules.technology.families_of) | rules.technology.excluded_tokens | _PLACEHOLDERS
    assert sorted(tokens - covered) == [], f"{side} tokens in no family and not excluded"


def test_national_kinds_are_opportunity_kinds() -> None:
    national = load_rules().jurisdiction.national_kinds
    assert national is not None and national <= set(KINDS)
    # The procurement kinds are deliberately absent: their country-only jurisdiction is "unknown".
    assert not national & {"tender", "procurement_notice", "rfp"}


def test_load_rules_is_cached_per_path() -> None:
    assert load_rules() is load_rules(DEFAULT_RULES_PATH)


def test_load_rules_from_another_file(tmp_path: Path, doc: dict[str, Any]) -> None:
    doc["rule_set_version"] = "match-rules@test"
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert load_rules_from(path).version == "match-rules@test"
    assert load_rules(path).version == "match-rules@test"


def test_unreadable_and_non_mapping_files(tmp_path: Path) -> None:
    with pytest.raises(RuleSetError, match="cannot read"):
        load_rules_from(tmp_path / "missing.yaml")
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(RuleSetError, match="not a mapping"):
        load_rules_from(path)


def _mutated(doc: dict[str, Any], mutate: Any) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    mutate(out)
    return out


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.pop("rule_set_version"), "rule_set_version"),
        (lambda d: d.update(rule_set_version="  "), "rule_set_version"),
        (lambda d: d.update(threshold=1.5), "threshold must be in"),
        (lambda d: d.update(threshold="high"), "threshold must be a number"),
        (lambda d: d.pop("weights"), "missing section weights"),
        (lambda d: d.update(weights=[1, 2]), "section weights must be a mapping"),
        (lambda d: d["weights"].pop("timing"), "weights.timing"),
        (lambda d: d["weights"].update(timing=0), "weights.timing must be positive"),
        (lambda d: d["rules"]["technology"].update(families={}), "families must be a non-empty"),
        (lambda d: d["rules"]["technology"]["families"].update(solar=[]), "families.solar"),
        (lambda d: d["rules"]["jurisdiction"].update(adjacency=["US-TX"]), "adjacency must be a mapping"),
        (lambda d: d["rules"]["jurisdiction"]["adjacency"].update({"US-TX": ["US-CA"]}), "not symmetric"),
        (lambda d: d["rules"]["jurisdiction"].update(national_credit="x"), "national_credit"),
        (lambda d: d["rules"]["size_window"].update(lower_ratio=2.0), "0 < lower_ratio < upper_ratio"),
        (lambda d: d["rules"]["size_window"].pop("upper_ratio"), "lower_ratio/upper_ratio"),
        (lambda d: d["rules"]["timing"].update(open_statuses=[]), "open_statuses"),
        (lambda d: d["rules"].pop("timing"), "missing section rules.timing"),
        (lambda d: d.update(publish="yes"), "publish must be true or false"),
        # v2 parts
        (lambda d: d["rules"]["technology"].update(excluded_tokens="load"), "excluded_tokens must be a list"),
        (lambda d: d["rules"]["technology"].update(excluded_tokens=["bess"]), "bess is also in a family"),
        (lambda d: d["rules"]["jurisdiction"].update(national_kinds="foa"), "national_kinds must be a list"),
        (lambda d: d["rules"].update(eligibility=[]), "section rules.eligibility must be a mapping"),
        (
            lambda d: d["rules"]["eligibility"].update(require_energy_relevance="y"),
            "require_energy_relevance",
        ),
        (lambda d: d["rules"]["eligibility"].update(classes=[]), "classes must be a non-empty list"),
        (lambda d: d["rules"]["eligibility"]["classes"].append("people"), r"classes\[5\] must be a mapping"),
        (lambda d: d["rules"]["eligibility"]["classes"][1].update(name="people"), r"classes\[1\]\.name"),
        (lambda d: d["rules"]["eligibility"]["classes"][0].update(eligible="no"), r"classes\[0\]\.eligible"),
        (lambda d: d["rules"]["eligibility"]["classes"][0].update(reason=" "), r"classes\[0\]\.reason"),
        (lambda d: d["rules"]["eligibility"]["classes"][0].update(terms="x"), "must be lists"),
        (lambda d: d["rules"]["eligibility"]["classes"][0].update(terms=["(unclosed"]), "not a valid regex"),
        (lambda d: d["rules"]["eligibility"]["classes"][0].update(cpv_all_in=[80]), "digit-string prefixes"),
        (
            lambda d: d["rules"]["eligibility"]["classes"][1].update(terms=[], cpv_all_in=[]),
            r"classes\[1\] needs terms or cpv_all_in",
        ),
    ],
)
def test_malformed_rule_sets_fail_with_the_field_named(
    doc: dict[str, Any], mutate: Any, message: str
) -> None:
    with pytest.raises(RuleSetError, match=message):
        parse_rules(_mutated(doc, mutate))


def test_optional_parts_default_empty(doc: dict[str, Any]) -> None:
    doc["rules"]["technology"].pop("all_source_tokens")
    doc["rules"]["jurisdiction"].pop("adjacency")
    doc["rules"]["timing"].pop("proposal_excluded_states")
    rules = parse_rules(doc)
    assert rules.technology.all_source_tokens == frozenset()
    assert dict(rules.jurisdiction.adjacency) == {}
    assert rules.timing.proposal_excluded_states == frozenset()


def test_v2_parts_are_optional_and_absent_means_v1(doc: dict[str, Any]) -> None:
    """A rule file without the v2 keys parses to v1 behaviour: no gate, no exclusions, every kind
    national. `services/match/test_eval.py` measures such a file and gets docs/22 §21's numbers."""
    doc["rules"].pop("eligibility")
    doc["rules"]["technology"].pop("excluded_tokens")
    doc["rules"]["jurisdiction"].pop("national_kinds")
    rules = parse_rules(doc)
    assert rules.eligibility is None
    assert rules.technology.excluded_tokens == frozenset()
    assert rules.jurisdiction.national_kinds is None
    doc["rules"]["eligibility"] = {
        "classes": [{"name": "people", "eligible": False, "reason": "r", "terms": ["x"]}]
    }
    gate = parse_rules(doc).eligibility
    assert gate is not None and gate.require_energy_relevance is True  # absent means required


def test_publish_gate(doc: dict[str, Any]) -> None:
    # The committed rule set is withheld: v1 missed docs/10 US-401 AC3 (docs/22 §21), and v2 has not
    # met it either (docs/22 §25.5: 82 independent pairs, below 100, and its decisive rules were not
    # blind). The gate opens only on a fresh blind draw of at least 100 pairs.
    assert load_rules().publish is False
    assert parse_rules({**doc, "publish": True}).publish is True
    doc.pop("publish")
    assert parse_rules(doc).publish is False  # absent means withheld, never shown by default
