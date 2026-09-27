"""`services/match/rules.py`: the committed rule file loads, and every malformed part fails at load
time with the field named, never as a silently neutral rule."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from services.match.rules import (
    DEFAULT_RULES_PATH,
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
    assert rules.version == "match-rules@v1"
    assert 0 < rules.threshold <= 1
    assert set(rules.weights) == set(RULE_NAMES)
    assert rules.weight_total == pytest.approx(sum(rules.weights.values()))
    assert "storage" in rules.technology.families_of["bess"]
    # `solar_storage` sits in two families at once.
    assert rules.technology.families_of["solar_storage"] >= {"solar", "storage"}
    assert "US-NV" in rules.jurisdiction.adjacency["US-AZ"]
    assert rules.path == DEFAULT_RULES_PATH.resolve()


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


def test_publish_gate(doc: dict[str, Any]) -> None:
    # The committed rule set is withheld: it misses docs/10 US-401 AC3 (docs/22 §21).
    assert load_rules().publish is False
    assert parse_rules({**doc, "publish": True}).publish is True
    doc.pop("publish")
    assert parse_rules(doc).publish is False  # absent means withheld, never shown by default
