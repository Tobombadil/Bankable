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
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app
from services.api.auth import create_session
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.models import Account, ApiKey, User
from services.db.session import get_sessionmaker
from services.ids import public_id
from tests.db_template import disposing, fresh_engine

UTC = dt.UTC


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """`services.api.ratelimit.default_limiter` is one process-wide instance (by design — it
    stands in for a shared Redis store in production, `services/api/ratelimit.py`'s module
    docstring), so without a reset here, tests sharing this pytest process would exhaust each
    other's buckets by running after `tests/test_api_pro_ratelimit.py`'s deliberately
    limit-filling tests. Autouse rather than opt-in: any test hitting `client` goes through the
    same middleware and the same limiter, so leaving one test file to remember this would be a
    silent ordering hazard."""
    default_limiter.reset()


#: A public address (`address_refusal` accepts it) every name resolves to in this suite.
PUBLIC_TEST_ADDRESS = "93.184.215.14"


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Webhook URLs are resolved at registration and delivery (architect audit A10,
    `services/alerts/webhook_url.py`). No test may resolve a real name (docs/04 E-6), so every
    name answers one public address unless a test installs its own resolver."""
    from services.alerts import webhook_url

    monkeypatch.setattr(webhook_url, "default_resolver", lambda host, port: [PUBLIC_TEST_ADDRESS])


@pytest.fixture()
def db_sessionmaker() -> Iterator[sessionmaker[Session]]:
    """A fresh schema copied from a per-process template, disposed of at teardown
    (`tests/db_template.py`; audit QA-2, QA-3)."""
    with disposing(fresh_engine()) as engine:
        yield get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Session:
    with db_sessionmaker() as s:
        yield s


@pytest.fixture()
def client(db_sessionmaker: sessionmaker[Session]) -> TestClient:
    # FastAPI keeps every dependency callable it has classified in a process-wide
    # `lru_cache(maxsize=4096)` (fastapi/dependencies/models.py), so this closure outlives the test;
    # it reaches the sessionmaker through `factory`, which teardown empties, so the engine does not
    # (audit 2026-10-07 QA-3).
    factory = [db_sessionmaker]

    def _override():
        s = factory[0]()
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
    factory.clear()


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
    `services.api.auth.create_session` would for a web login — not a test-only bypass.

    Commits immediately: this sandbox's SQLite test target shares one physical connection across
    every `Session` in a test (`services/db/session.py`'s `StaticPool`, needed because a bare
    in-memory database is otherwise per-connection) so that fixture writes on the `db` fixture and
    request-scoped writes through `client` can see each other without an explicit commit in every
    test. That convenience has one sharp edge: a later request that raises a handled error (a 404,
    say) rolls back its own request-scoped session (`services/api/deps.py` `get_db`), and because
    the connection is shared, that rollback would also discard this session row if it were left
    uncommitted — an artefact of the shared-connection test setup, not of the app (two real
    connections cannot roll each other back). Committing here up front avoids depending on the
    ordering of later requests to keep a login alive.
    """
    _row, cookie_value = create_session(session_db, user)
    session_db.commit()
    client.cookies.set("session", cookie_value)
