"""`InMemoryBilling.create_subscription` round-trip: what it stores must be visible to a later
`get_subscription` call (coordinator follow-up, 2026-09-13) — the same `subscriptions` dict
`handle_webhook` also writes into, so both code paths share one source of truth.
"""

from __future__ import annotations

from services.billing.fake import InMemoryBilling
from services.sor.ports import SubscriptionCreate


def test_create_subscription_round_trips_through_get_subscription() -> None:
    billing = InMemoryBilling()
    state = billing.create_subscription(
        SubscriptionCreate(billing_ref="cus_1", plan="pro", seats=2, account_public_id="acc_1")
    )
    assert state.status == "active"
    assert state.seats == 2
    assert state.plan_tier == "pro"
    assert state.mrr_amount == 49.0 * 2

    assert billing.get_subscription(state.sor_ref) is state


def test_create_subscription_with_trial_days_is_trialing() -> None:
    billing = InMemoryBilling()
    state = billing.create_subscription(
        SubscriptionCreate(billing_ref="cus_2", plan="team", seats=1, trial_days=14)
    )
    assert state.status == "trialing"
    assert billing.get_subscription(state.sor_ref) is state


def test_create_subscription_unknown_plan_gets_zero_mrr_not_an_error() -> None:
    billing = InMemoryBilling()
    state = billing.create_subscription(SubscriptionCreate(billing_ref="cus_3", plan="mystery", seats=5))
    assert state.mrr_amount == 0.0
    assert billing.get_subscription(state.sor_ref) is state


def test_create_subscription_sor_refs_are_unique_across_calls() -> None:
    billing = InMemoryBilling()
    first = billing.create_subscription(SubscriptionCreate(billing_ref="cus_4", plan="pro", seats=1))
    second = billing.create_subscription(SubscriptionCreate(billing_ref="cus_4", plan="pro", seats=1))
    assert first.sor_ref != second.sor_ref
    assert len(billing.subscriptions) == 2
