"""`Subscription` model round-trip on SQLite, its CHECK vocabularies, and the migration chain
(docs/04 E-7's model-layer test pattern, `services/db/test_models.py`)."""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.db.models import Account, Subscription
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id

UTC = dt.UTC


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def make_account(session: Session) -> Account:
    account = Account(public_id="", name="Acme", kind="organization")
    session.add(account)
    session.flush()
    account.public_id = public_id("acc", account.id)
    session.flush()
    return account


def test_subscription_round_trips(session: Session) -> None:
    account = make_account(session)
    now = dt.datetime.now(UTC)
    sub = Subscription(
        public_id="",
        account_id=account.id,
        sor_kind="stripe",
        sor_ref="sub_test_1",
        plan_code="pro_monthly",
        plan_tier="pro",
        status="active",
        seats=3,
        current_period_start=now,
        current_period_end=now + dt.timedelta(days=30),
        cancel_at=None,
        mrr_amount=147.0,
        currency="USD",
        mirrored_at=now,
        drift_flag=False,
    )
    session.add(sub)
    session.flush()
    sub.public_id = public_id("subn", sub.id)
    session.commit()

    fetched = session.get(Subscription, sub.id)
    assert fetched is not None
    assert fetched.sor_kind == "stripe"
    assert fetched.sor_ref == "sub_test_1"
    assert fetched.plan_tier == "pro"
    assert fetched.status == "active"
    assert fetched.seats == 3
    assert float(fetched.mrr_amount) == pytest.approx(147.0)
    assert fetched.currency == "USD"
    assert fetched.drift_flag is False
    assert fetched.account_id == account.id


def test_subscription_sor_kind_check_constraint(session: Session) -> None:
    account = make_account(session)
    now = dt.datetime.now(UTC)
    bad = Subscription(
        public_id="x",
        account_id=account.id,
        sor_kind="not-a-real-sor",
        sor_ref="sub_bad_1",
        plan_code="pro_monthly",
        plan_tier="pro",
        status="active",
        current_period_start=now,
        current_period_end=now,
        currency="USD",
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()


def test_subscription_plan_tier_check_constraint(session: Session) -> None:
    session.rollback()
    account = make_account(session)
    now = dt.datetime.now(UTC)
    bad = Subscription(
        public_id="x2",
        account_id=account.id,
        sor_kind="stripe",
        sor_ref="sub_bad_2",
        plan_code="enterprise_monthly",
        plan_tier="enterprise",  # not in docs/21's vocab (services/billing/README.md decision #3)
        status="active",
        current_period_start=now,
        current_period_end=now,
        currency="USD",
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()


def test_subscription_status_check_constraint(session: Session) -> None:
    session.rollback()
    account = make_account(session)
    now = dt.datetime.now(UTC)
    bad = Subscription(
        public_id="x3",
        account_id=account.id,
        sor_kind="stripe",
        sor_ref="sub_bad_3",
        plan_code="pro_monthly",
        plan_tier="pro",
        status="not-a-real-status",
        current_period_start=now,
        current_period_end=now,
        currency="USD",
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()


def test_subscription_one_row_per_sor_kind_and_sor_ref(session: Session) -> None:
    session.rollback()
    account = make_account(session)
    now = dt.datetime.now(UTC)

    def make(sor_ref: str) -> Subscription:
        return Subscription(
            public_id=f"sub_dup_{sor_ref}",
            account_id=account.id,
            sor_kind="stripe",
            sor_ref=sor_ref,
            plan_code="pro_monthly",
            plan_tier="pro",
            status="active",
            current_period_start=now,
            current_period_end=now,
            currency="USD",
        )

    session.add(make("sub_dup_1"))
    session.flush()
    session.add(make("sub_dup_1"))
    with pytest.raises(IntegrityError):
        session.flush()


# --------------------------------------------------------------------------------- migration chain
def test_migration_0004_imports_and_revision_chain_resolves() -> None:
    import importlib

    module = importlib.import_module("services.db.migrations.versions.0004_subscription_mirror")
    assert module.revision == "0004"
    assert module.down_revision == "0003"

    versions_dir = pathlib.Path(__file__).resolve().parents[2] / "services" / "db" / "migrations" / "versions"
    assert versions_dir.is_dir()
    revisions: dict[str, str | None] = {}
    for path in versions_dir.glob("*.py"):
        if path.name == "__init__.py":
            continue
        mod = importlib.import_module(f"services.db.migrations.versions.{path.stem}")
        revisions[mod.revision] = mod.down_revision

    assert revisions["0004"] == "0003"
    # The whole chain resolves to one root (no fork, no dangling `down_revision`).
    roots = [rev for rev, down in revisions.items() if down is None]
    assert len(roots) == 1
    for rev, down in revisions.items():
        assert down is None or down in revisions, f"{rev} points at a missing revision {down!r}"
