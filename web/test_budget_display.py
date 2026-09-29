"""The opportunity page shows a placeholder budget (0 or less) as a dash, like no budget (lane I3,
2026-09-29). The loader already stores TED's 0 / -1 placeholders as null; this covers a value that
reaches the page another way (an admin edit, or a store loaded before the fix)."""

from __future__ import annotations

from typing import Any

import pytest

from web.viewmodels import flatten_opportunity


def _entity(amount: Any, currency: str | None = "EUR") -> dict[str, Any]:
    return {
        "public_id": "opp_1",
        "slug": "opp-1",
        "title": "Test tender",
        "budget_amount": amount,
        "budget_currency": currency,
        "provenance": [],
    }


@pytest.mark.parametrize("amount", [0, 0.0, -1, -1.0, None])
def test_a_placeholder_or_missing_budget_is_not_an_amount(amount: Any) -> None:
    record = flatten_opportunity(_entity(amount))
    assert record["budget_amount"] is None
    assert record["budget_currency"] == "EUR"  # the stated currency is not the page's to drop


def test_a_positive_budget_is_shown_as_stated() -> None:
    assert flatten_opportunity(_entity(1_250_000.0))["budget_amount"] == 1_250_000.0
