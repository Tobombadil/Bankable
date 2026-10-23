"""Fixtures for the private-project tests: a fresh SQLite store with the private tables, users in
every state the authorisation rules distinguish, and the `AuthContext` each would arrive with."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

import services.projects.models  # noqa: F401 -- registers the private tables before the template is built
from services.api.auth import AuthContext, generate_api_key, register_user
from services.db.models import ApiKey, User
from services.db.session import get_sessionmaker
from services.ids import public_id
from tests.db_template import disposing, fresh_engine

VERIFIED_AT = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
TEST_PASSWORD = "correct horse battery"  # noqa: S105 -- a fixture value, not a credential

UserFactory = Callable[..., User]


@pytest.fixture(autouse=True)
def _test_pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Address hashes are peppered (`services.api.audit.hash_identifier`); tests use their own
    obviously fake pepper rather than the development fallback."""
    monkeypatch.setenv("AUDIT_HASH_PEPPER", "test-pepper-private-projects")


@pytest.fixture()
def db_sessionmaker() -> Iterator[sessionmaker[Session]]:
    with disposing(fresh_engine()) as engine:
        yield get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Iterator[Session]:
    with db_sessionmaker() as session:
        yield session


@pytest.fixture()
def make_user(db: Session) -> UserFactory:
    counter = iter(range(1, 10_000))

    def _make(
        email: str | None = None,
        *,
        verified: bool = True,
        status: str = "active",
        role: str = "member",
        name: str | None = None,
    ) -> User:
        n = next(counter)
        user = register_user(
            db,
            email=email or f"user{n}@example.org",
            password=TEST_PASSWORD,
            name=name or f"User {n}",
        )
        user.email_verified_at = VERIFIED_AT if verified else None
        user.role = role
        if status != "active":
            user.status = status
            if status == "anonymised":
                user.anonymised_at = VERIFIED_AT
        db.flush()
        return user

    return _make


def session_ctx(user: User) -> AuthContext:
    """What `services.api.auth.build_auth_context` builds for a valid session cookie."""
    return AuthContext(
        user=user,
        account=user.account,
        api_key=None,
        entitlement=user.account.entitlement,
        scopes=frozenset(),
    )


def api_key_ctx(
    db: Session, user: User, *, scopes: tuple[str, ...] = ("admin:*", "read:live")
) -> AuthContext:
    """What a bearer key resolves to: no user, a key, its account."""
    _, key_hash, last4 = generate_api_key()
    key = ApiKey(
        public_id="",
        account_id=user.account_id,
        created_by_user_id=user.id,
        name="test key",
        last4=last4,
        key_hash=key_hash,
        scopes=list(scopes),
        tier="api",
    )
    db.add(key)
    db.flush()
    key.public_id = public_id("key", key.id)
    db.flush()
    return AuthContext(
        user=None, account=user.account, api_key=key, entitlement="api", scopes=frozenset(scopes)
    )
