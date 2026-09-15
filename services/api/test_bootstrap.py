"""`python -m services.api.bootstrap owner`: create or promote the first operator."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.api.auth import authenticate_user
from services.api.bootstrap import main, make_owner
from services.db.models import Account, User
from services.db.session import get_engine, get_sessionmaker, init_db

_PASSWORD = "correct horse battery staple"  # noqa: S105 - a test credential, not a secret


def _factory() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


def test_creates_an_owner_with_an_admin_account() -> None:
    with _factory()() as db:
        user, created = make_owner(db, email="Owner@Example.com", password=_PASSWORD, name="O", seats=5)
        db.commit()
        assert created and user.role == "owner" and user.email == "owner@example.com"
        account = db.get(Account, user.account_id)
        assert account is not None
        assert (
            account.entitlement == "admin"
            and account.entitlement_source == "manual_grant"
            and account.seats == 5
        )
        assert authenticate_user(db, email="owner@example.com", password=_PASSWORD) is not None


def test_promotes_an_existing_member_without_touching_the_password() -> None:
    with _factory()() as db:
        make_owner(db, email="m@example.com", password=_PASSWORD, name=None, seats=1)
        db.commit()
        db.scalar(select(User)).role = "member"
        db.commit()
        user, created = make_owner(db, email="m@example.com", password=None, name=None, seats=5)
        db.commit()
        assert not created and user.role == "owner"
        assert authenticate_user(db, email="m@example.com", password=_PASSWORD) is not None
        assert db.get(Account, user.account_id).seats == 5


def test_cli_targets_a_sqlite_file(tmp_path) -> None:
    db_path = tmp_path / "dev.db"
    rc = main(
        [
            "owner",
            "--email",
            "cli@example.com",
            "--password",
            "correct horse battery staple",
            "--db",
            str(db_path),
        ]
    )
    assert rc == 0
    engine = get_engine(f"sqlite:///{db_path}")
    with get_sessionmaker(engine)() as db:
        assert db.scalar(select(User)).role == "owner"
