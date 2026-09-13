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
    """One session per request, committed on a clean return and rolled back on an exception —
    "one commit gives a changed entity, its events and the outbound job inserts" (docs/20 §3.7).
    Sprint 2's routes were all read-only, so this sprint's write endpoints (`services/api/pro.py`:
    keys, saved searches, webhooks, the admin entitlement grant) are the first callers for whom
    the distinction matters — without it, a session `.close()` with no explicit commit silently
    discards every write via SQLAlchemy's implicit rollback-on-close."""
    active = _sessionmaker or configure()
    db = active()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
