"""Shared fixtures for the Pro-tier and alerts test suite (`tests/test_api_pro*.py`,
`tests/test_alerts*.py`). Mirrors `services/api/conftest.py`'s DB/TestClient wiring (that file's
fixtures are scoped to `services/api/` by pytest's conftest discovery rules, so this is a small,
deliberate duplication rather than a cross-package import of pytest fixture objects) and adds the
account/user/key factories this sprint's tests need. Entity factories with no auth role
(`make_open_licence`, `make_public_source`, `make_visible_proposal`, ...) are imported directly
from `services.api.conftest` — those are plain functions, not fixtures, so importing them is safe
and avoids duplicating the whole public-tier fixture set.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app
from services.api.auth import create_session
from services.api.deps import get_db
from services.db.models import Account, ApiKey, User
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id

UTC = dt.UTC


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Session:
    with db_sessionmaker() as s:
        yield s


@pytest.fixture()
def client(db_sessionmaker: sessionmaker[Session]) -> TestClient:
    def _override():
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_account(session: Session, *, entitlement: str = "pro", name: str = "Acme Capital") -> Account:
    account = Account(public_id="", name=name, kind="organization", entitlement=entitlement)
    session.add(account)
    session.flush()
    account.public_id = public_id("acc", account.id)
    session.flush()
    return account


def make_user(
    session: Session,
    account: Account,
    *,
    email: str = "ana@example.com",
    role: str = "member",
    password: str = "correct horse battery staple",
) -> User:
    from services.api.auth import hash_password

    user = User(
        public_id="",
        account_id=account.id,
        email=email,
        password_hash=hash_password(password),
        name="Ana Ruiz",
        role=role,
        auth_provider="password",
    )
    session.add(user)
    session.flush()
    user.public_id = public_id("usr", user.id)
    session.flush()
    return user


def make_api_key(
    session: Session, account: Account, user: User, *, scopes: list[str] | None = None
) -> tuple[ApiKey, str]:
    from services.api.auth import generate_api_key

    secret, key_hash, last4 = generate_api_key("bk_live")
    key = ApiKey(
        public_id="",
        account_id=account.id,
        created_by_user_id=user.id,
        name="test-key",
        prefix="bk_live",
        last4=last4,
        key_hash=key_hash,
        scopes=scopes or ["read:live"],
        tier="pro",
        licence_accepted_version="api-licence-1.0",
        licence_accepted_at=dt.datetime.now(UTC),
    )
    session.add(key)
    session.flush()
    key.public_id = public_id("key", key.id)
    session.flush()
    return key, secret


def login(client: TestClient, session_db: Session, user: User) -> None:
    """Signs `client` in as `user` by minting a real session row and setting the cookie exactly as
    `services.api.auth.create_session` would for a web login — not a test-only bypass."""
    _row, cookie_value = create_session(session_db, user)
    client.cookies.set("session", cookie_value)
