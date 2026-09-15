"""`apply_entitlement_change` tests: entitlement resolution per plan/status, idempotency on
`event_ref`, the `admin` guard, account matching by `billing_ref` vs `account_public_id`, the
CRM mirror's best-effort semantics, and the unknown-account case.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from services.billing.entitlement import apply_entitlement_change
from services.crm.fake import InMemoryCrm
from services.db.models import Account, Event, Subscription
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id
from services.sor.ports import EntitlementChange, SorUnavailable, SubscriptionState, entitlement_for

UTC = dt.UTC


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def make_account(
    session: Session, *, entitlement: str = "public", billing_ref: str | None = None, name: str = "Acme"
) -> Account:
    account = Account(
        public_id="", name=name, kind="organization", entitlement=entitlement, billing_ref=billing_ref
    )
    session.add(account)
    session.flush()
    account.public_id = public_id("acc", account.id)
    session.flush()
    return account


def make_state(
    *,
    plan_tier: str = "pro",
    status: str = "active",
    seats: int = 3,
    sor_ref: str = "sub_test_1",
    billing_ref: str = "cus_test_1",
    plan_code: str = "pro_monthly",
) -> SubscriptionState:
    now = dt.datetime.now(UTC)
    return SubscriptionState(
        sor_kind="stripe",
        sor_ref=sor_ref,
        billing_ref=billing_ref,
        plan_code=plan_code,
        plan_tier=plan_tier,
        status=status,
        seats=seats,
        current_period_start=now,
        current_period_end=now + dt.timedelta(days=30),
        currency="USD",
        cancel_at=None,
        mrr_amount=147.0,
    )


def make_change(
    state: SubscriptionState, *, event_ref: str = "evt_1", account_public_id: str | None = None
) -> EntitlementChange:
    return EntitlementChange(
        event_ref=event_ref,
        occurred_at=dt.datetime.now(UTC),
        billing_ref=state.billing_ref,
        subscription=state,
        entitlement=entitlement_for(state.plan_tier, state.status),
        account_public_id=account_public_id,
    )


def test_pro_plan_grants_pro(session: Session) -> None:
    account = make_account(session, billing_ref="cus_test_1")
    session.commit()
    state = make_state(plan_tier="pro", status="active")
    result = apply_entitlement_change(session, make_change(state))
    assert result is account
    assert account.entitlement == "pro"
    assert account.entitlement_source == "sor"
    assert account.seats == 3
    sub = session.query(Subscription).filter_by(sor_ref=state.sor_ref).one()
    assert sub.plan_tier == "pro"
    assert sub.status == "active"
    assert sub.account_id == account.id


def test_api_plan_grants_api(session: Session) -> None:
    account = make_account(session, billing_ref="cus_test_2")
    session.commit()
    state = make_state(plan_tier="api", status="active", sor_ref="sub_test_2", billing_ref="cus_test_2")
    apply_entitlement_change(session, make_change(state))
    assert account.entitlement == "api"


def test_team_plan_grants_pro(session: Session) -> None:
    account = make_account(session, billing_ref="cus_test_3")
    session.commit()
    state = make_state(plan_tier="team", status="active", sor_ref="sub_test_3", billing_ref="cus_test_3")
    apply_entitlement_change(session, make_change(state))
    assert account.entitlement == "pro"


def test_enterprise_plan_stored_as_api_plan_tier(session: Session) -> None:
    """docs/21's `plan_tier` vocabulary has no `enterprise` member (services/db/models.py
    `Subscription` docstring, decision #3): the mirror row stores `api` (the entitlement
    `enterprise` grants) while `plan_code` keeps the vendor's exact code."""
    account = make_account(session, billing_ref="cus_test_ent")
    session.commit()
    state = make_state(
        plan_tier="enterprise",
        status="active",
        sor_ref="sub_test_ent",
        billing_ref="cus_test_ent",
        plan_code="enterprise_monthly",
    )
    apply_entitlement_change(session, make_change(state))
    assert account.entitlement == "api"
    sub = session.query(Subscription).filter_by(sor_ref="sub_test_ent").one()
    assert sub.plan_tier == "api"
    assert sub.plan_code == "enterprise_monthly"


def test_canceled_status_drops_to_public(session: Session) -> None:
    account = make_account(session, entitlement="pro", billing_ref="cus_test_4")
    session.commit()
    state = make_state(plan_tier="pro", status="canceled", sor_ref="sub_test_4", billing_ref="cus_test_4")
    apply_entitlement_change(session, make_change(state))
    assert account.entitlement == "public"


def test_past_due_keeps_entitlement(session: Session) -> None:
    account = make_account(session, entitlement="pro", billing_ref="cus_test_5")
    session.commit()
    state = make_state(plan_tier="pro", status="past_due", sor_ref="sub_test_5", billing_ref="cus_test_5")
    apply_entitlement_change(session, make_change(state))
    assert account.entitlement == "pro"


def test_paused_drops_to_public(session: Session) -> None:
    account = make_account(session, entitlement="pro", billing_ref="cus_test_6")
    session.commit()
    state = make_state(plan_tier="pro", status="paused", sor_ref="sub_test_6", billing_ref="cus_test_6")
    apply_entitlement_change(session, make_change(state))
    assert account.entitlement == "public"


def test_idempotent_on_event_ref(session: Session) -> None:
    account = make_account(session, billing_ref="cus_test_7")
    session.commit()
    state = make_state(sor_ref="sub_test_7", billing_ref="cus_test_7")
    change = make_change(state, event_ref="evt_dup")
    apply_entitlement_change(session, change)
    assert account.entitlement == "pro"
    # Simulate a human having since overridden the source; a replayed webhook (same event_ref)
    # must not touch it again.
    account.entitlement_source = "manual_grant"
    session.flush()

    second = apply_entitlement_change(session, change)
    assert second is account
    assert account.entitlement_source == "manual_grant"
    events = session.query(Event).filter_by(idempotency_key="billing:evt_dup").all()
    assert len(events) == 1


def test_admin_account_never_overwritten(session: Session) -> None:
    account = make_account(session, entitlement="admin", billing_ref="cus_test_8")
    session.commit()
    state = make_state(plan_tier="pro", status="canceled", sor_ref="sub_test_8", billing_ref="cus_test_8")
    apply_entitlement_change(session, make_change(state))
    assert account.entitlement == "admin"
    assert account.entitlement_source == "manual_grant"
    # The mirror row is still written (docs/21 §3.14 is a read model of the commercial record
    # regardless of what the platform does with it) — only `account.entitlement` is protected.
    sub = session.query(Subscription).filter_by(sor_ref="sub_test_8").one()
    assert sub.status == "canceled"


def test_match_by_account_public_id_sets_billing_ref(session: Session) -> None:
    account = make_account(session, billing_ref=None)
    session.commit()
    state = make_state(sor_ref="sub_test_9", billing_ref="cus_test_9")
    change = make_change(state, account_public_id=account.public_id)
    apply_entitlement_change(session, change)
    assert account.billing_ref == "cus_test_9"
    assert account.entitlement == "pro"


def test_crm_mirror_called_with_right_subscription_mirror(session: Session) -> None:
    account = make_account(session, billing_ref="cus_test_10")
    session.commit()
    state = make_state(sor_ref="sub_test_10", billing_ref="cus_test_10", plan_tier="pro", seats=5)
    crm = InMemoryCrm()
    apply_entitlement_change(session, make_change(state), crm=crm)
    row = crm.subscriptions["sub_test_10"]
    assert row.subscription.stripe_subscription_id == "sub_test_10"
    assert row.subscription.stripe_customer_id == "cus_test_10"
    assert row.subscription.platform_account_id == account.public_id
    assert row.subscription.plan == "pro"
    assert row.subscription.seats == 5
    assert row.subscription.status == "active"


class _FailingCrm:
    """Minimal `CrmPort`-shaped stand-in whose only implemented method always fails — this test
    only needs `upsert_subscription` to raise; mypy strict does not check test modules
    (`pyproject.toml` `[tool.mypy] exclude`)."""

    sor_kind = "attio_fake"

    def upsert_subscription(self, subscription: object) -> str:
        raise SorUnavailable("attio unavailable")


def test_crm_sor_unavailable_does_not_fail_the_apply(session: Session) -> None:
    account = make_account(session, billing_ref="cus_test_11")
    session.commit()
    state = make_state(sor_ref="sub_test_11", billing_ref="cus_test_11")
    result = apply_entitlement_change(session, make_change(state), crm=_FailingCrm())  # type: ignore[arg-type]
    assert result is account
    assert account.entitlement == "pro"


def test_second_change_updates_existing_subscription_row_in_place(session: Session) -> None:
    account = make_account(session, billing_ref="cus_test_13")
    session.commit()
    state = make_state(sor_ref="sub_test_13", billing_ref="cus_test_13", status="active", seats=2)
    apply_entitlement_change(session, make_change(state, event_ref="evt_13a"))
    first_row_id = session.query(Subscription).filter_by(sor_ref="sub_test_13").one().id

    updated_state = make_state(sor_ref="sub_test_13", billing_ref="cus_test_13", status="past_due", seats=5)
    apply_entitlement_change(session, make_change(updated_state, event_ref="evt_13b"))

    rows = session.query(Subscription).filter_by(sor_ref="sub_test_13").all()
    assert len(rows) == 1
    assert rows[0].id == first_row_id
    assert rows[0].status == "past_due"
    assert rows[0].seats == 5
    assert account.seats == 5


def test_unrecognised_plan_tier_falls_back_to_api(session: Session) -> None:
    make_account(session, billing_ref="cus_test_14")
    session.commit()
    state = make_state(
        plan_tier="not-a-real-plan", status="active", sor_ref="sub_test_14", billing_ref="cus_test_14"
    )
    # `entitlement_for` treats an unrecognised plan_tier as ungranted (falls to "public"); the
    # mirror row's own `plan_tier` still needs a value the DB's CHECK constraint accepts.
    change = EntitlementChange(
        event_ref="evt_14",
        occurred_at=dt.datetime.now(UTC),
        billing_ref=state.billing_ref,
        subscription=state,
        entitlement="public",
    )
    apply_entitlement_change(session, change)
    sub = session.query(Subscription).filter_by(sor_ref="sub_test_14").one()
    assert sub.plan_tier == "api"


def test_unknown_account_returns_none(session: Session) -> None:
    state = make_state(sor_ref="sub_test_12", billing_ref="cus_does_not_exist")
    change = make_change(state, event_ref="evt_unknown")
    result = apply_entitlement_change(session, change)
    assert result is None
    assert session.query(Subscription).count() == 0
