"""A fresh SQLite store per test, and the app wired to it, so each figure can be checked against the
public route that answers it (the same shape as `services/api/conftest.py`'s fixtures)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.session import get_sessionmaker
from tests.db_template import disposing, fresh_engine


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def db_sessionmaker() -> Iterator[sessionmaker[Session]]:
    with disposing(fresh_engine()) as engine:
        yield get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Iterator[Session]:
    with db_sessionmaker() as session:
        yield session


@pytest.fixture()
def client(db_sessionmaker: sessionmaker[Session]) -> Iterator[TestClient]:
    factory = [db_sessionmaker]

    def _override() -> Iterator[Session]:
        session = factory[0]()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    factory.clear()
