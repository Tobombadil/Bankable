"""Tests for `pipeline/context/ethanol_match.py`: the scoring functions on hand-picked cases and
the global-greedy assignment on a small synthetic frame that reproduces the two failure modes the
real data showed (docs/24 §2.3; the module docstring): a same-company multi-plant operator, and an
ownership-change pair with almost no name overlap."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from pipeline.context.ethanol_match import (
    DEFAULT_THRESHOLD,
    capacity_score,
    city_of,
    city_score,
    combined_score,
    match_ethanol,
    pair_score,
)


# ------------------------------------------------------------------------------- city_score
def test_city_score_is_exact_after_light_abbreviation_normalisation():
    assert city_score("Fort Dodge", "Ft Dodge") == 1.0
    assert city_score("Bingham Lake", "Bingham Lake") == 1.0


def test_city_score_does_not_reward_a_shared_generic_suffix_alone():
    """The trap that inflated wrong cross-plant pairs before the fix: two different Iowa towns
    share the bare word "city" and nothing else."""
    score = city_score("Charles City", "Albert City")
    assert score is not None
    assert score < 0.5


def test_city_score_rewards_a_shared_non_generic_token():
    score = city_score("Jefferson Plant", "Jefferson")
    assert score == 1.0


def test_city_score_is_none_when_either_side_has_no_place_name():
    assert city_score(None, "Anywhere") is None
    assert city_score("Anywhere", "") is None


# ------------------------------------------------------------------------------- capacity_score
@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (100.0, 95.0, 1.0),
        (100.0, 80.0, 0.7),
        (100.0, 65.0, 0.4),
        (100.0, 10.0, 0.0),
    ],
)
def test_capacity_score_bins_on_the_ratio(a, b, expected):
    assert capacity_score(a, b) == expected


def test_capacity_score_is_none_without_two_positive_values():
    assert capacity_score(None, 50.0) is None
    assert capacity_score(50.0, 0.0) is None


# ------------------------------------------------------------------------------- pair_score
def test_pair_score_is_zero_with_no_positive_evidence():
    """Same-size, same-state, but nothing links the names or the places -- capacity alone must not
    manufacture a match (module docstring)."""
    score, ns, cts, _caps = pair_score("Acme Ethanol LLC", "Qxzy", 60.0, "Zenith Fuels Inc", "Wbkr", 60.0)
    assert score == 0.0
    assert ns == 0.0
    assert not cts


def test_pair_score_combines_all_three_signals():
    score, ns, cts, caps = pair_score(
        "Green Plains Ord LLC", "Ord", 57.0, "Green America Biofuels Ord LLC", "Ord", 68.0
    )
    assert ns == pytest.approx(0.6667, abs=1e-3)
    assert cts == 1.0
    assert caps == 0.7
    assert score == combined_score(ns, cts, caps)


# ------------------------------------------------------------------------------- city_of
def test_city_of_reads_the_atlas_site_key():
    assert city_of(json.dumps({"site": "Maricopa", "padd": "5"})) == "Maricopa"


def test_city_of_reads_the_capacity_report_city_key():
    assert city_of(json.dumps({"city": "Medina", "padd": "PADD 1"})) == "Medina"


def test_city_of_is_none_for_unparseable_or_missing_input():
    assert city_of(None) is None
    assert city_of("not json") is None
    assert city_of(float("nan")) is None


# ------------------------------------------------------------------------------- match_ethanol
def _row(source_asset_id, name, city, capacity, state="US-IA", **extra):
    row = {
        "source_asset_id": source_asset_id,
        "operator_name": name,
        "state_code": state,
        "capacity_value": capacity,
        "attributes_text": json.dumps({"site": city} if "atlas" in source_asset_id else {"city": city}),
    }
    row.update(extra)
    return row


def test_match_ethanol_resolves_same_name_plants_by_city_not_by_name():
    """The real Iowa case (docs/24 §2.3, this module's docstring): several "Valero Renewable Fuels
    LLC" plants in one state, distinguished only by exact-city matches. A matcher that let a
    shared generic word ("city") carry a cross-plant match would mis-assign at least one of these."""
    atlas = pd.DataFrame(
        [
            _row("atlas-1", "Valero Renewable Fuels LLC", "Charles City", 140.0),
            _row("atlas-2", "Valero Renewable Fuels LLC", "Hartley", 140.0),
            _row("atlas-3", "Valero Renewable Fuels LLC", "Albert City", 135.0),
        ]
    )
    capacity = pd.DataFrame(
        [
            _row("cap-1", "Valero Renewable Fuels LLC", "Hartley", 150.0),
            _row("cap-2", "Valero Renewable Fuels LLC", "Charles City", 165.0),
            _row("cap-3", "Valero Renewable Fuels LLC", "Albert City", 140.0),
        ]
    )
    matches = match_ethanol(atlas, capacity, threshold=DEFAULT_THRESHOLD)
    accepted = {
        (r["atlas_source_asset_id"], r["capacity_source_asset_id"])
        for r in matches.to_dict("records")
        if r["accepted"]
    }
    assert accepted == {("atlas-1", "cap-2"), ("atlas-2", "cap-1"), ("atlas-3", "cap-3")}


def test_match_ethanol_does_not_pair_an_ownership_changed_name_with_the_wrong_sibling_plant():
    """Real NE case (docs/24 §2.3): "Green Plains Ord" truly matches "Green America Biofuels Ord"
    (an ownership change at the same place), not "Green Plains York" (the same owner's *other*,
    unrelated plant) -- global greedy must not let the weaker same-owner pair win just because the
    name overlap looks stronger there than the true, low-name-overlap match."""
    atlas = pd.DataFrame([_row("atlas-ord", "Green Plains Ord LLC", "Ord", 57.0, state="US-NE")])
    capacity = pd.DataFrame(
        [
            _row("cap-ord", "Green America Biofuels Ord LLC", "Ord", 68.0, state="US-NE"),
            _row("cap-york", "Green Plains York LLC", "York", 60.0, state="US-NE"),
        ]
    )
    matches = match_ethanol(atlas, capacity, threshold=DEFAULT_THRESHOLD)
    accepted = matches[matches["accepted"]]
    assert len(accepted) == 1
    row = accepted.iloc[0]
    assert row["atlas_source_asset_id"] == "atlas-ord"
    assert row["capacity_source_asset_id"] == "cap-ord"


def test_match_ethanol_never_drops_an_unmatched_row():
    atlas = pd.DataFrame([_row("atlas-only", "Solo Ethanol LLC", "Nowhere", 40.0)])
    capacity = pd.DataFrame([_row("cap-only", "Different Fuels Inc", "Elsewhere", 90.0)])
    matches = match_ethanol(atlas, capacity, threshold=DEFAULT_THRESHOLD)
    assert not matches["accepted"].any()


def test_match_ethanol_blocks_by_state():
    atlas = pd.DataFrame([_row("atlas-1", "Acme Ethanol LLC", "Anytown", 50.0, state="US-IA")])
    capacity = pd.DataFrame([_row("cap-1", "Acme Ethanol LLC", "Anytown", 50.0, state="US-NE")])
    matches = match_ethanol(atlas, capacity, threshold=DEFAULT_THRESHOLD)
    assert len(matches) == 0


def test_match_ethanol_empty_frames():
    empty = pd.DataFrame(columns=["source_asset_id", "operator_name", "state_code", "capacity_value"])
    matches = match_ethanol(empty, empty, threshold=DEFAULT_THRESHOLD)
    assert list(matches.columns)
    assert len(matches) == 0
