"""FastAPI dependencies: one DB session per request.

Configuration comes from `DATABASE_URL` (CLAUDE.md: secrets/config from environment only) via
`services.db.session`. Tests override `get_db` with `app.dependency_overrides` rather than reading
the environment, so they never depend on process-global state (services/api/test_routes.py).
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session, sessionmaker

from services.db.session import get_engine, get_sessionmaker, init_db

_sessionmaker: sessionmaker[Session] | None = None


def configure(database_url: str | None = None) -> sessionmaker[Session]:
    global _sessionmaker
    engine = get_engine(database_url)
    init_db(engine)
    _sessionmaker = get_sessionmaker(engine)
    return _sessionmaker


def get_db() -> Iterator[Session]:
    active = _sessionmaker or configure()
    db = active()
    try:
        yield db
    finally:
        db.close()
