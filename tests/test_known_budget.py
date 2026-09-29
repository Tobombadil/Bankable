"""pipeline/connectors/opportunity.py: shared opportunity helpers (lane I3, 2026-09-29)."""

from __future__ import annotations

import math

import pytest

from pipeline.connectors.opportunity import known_budget


@pytest.mark.parametrize("value", [None, 0, 0.0, -1, -1.0, -0.01, math.nan, math.inf, True])
def test_placeholders_and_non_numbers_are_unknown(value: object) -> None:
    assert known_budget(value) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(("value", "expected"), [(1, 1.0), (0.5, 0.5), (1_266_866_061.69, 1_266_866_061.69)])
def test_positive_amounts_are_kept(value: float, expected: float) -> None:
    assert known_budget(value) == expected
