"""Engine and session factory.

`DATABASE_URL` from the environment selects the backend (CLAUDE.md: secrets/config from
environment only). Falls back to an in-memory SQLite database, which is this sprint's test
target because Postgres is not installed in this environment (services/README.md).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from services.db.base import Base

DEFAULT_SQLITE_URL = "sqlite+pysqlite:///:memory:"


def get_engine(url: str | None = None) -> Engine:
    url = url or os.environ.get("DATABASE_URL", DEFAULT_SQLITE_URL)
    connect_args: dict[str, object] = {}
    kwargs: dict[str, object] = {}
    made = make_url(url)
    if made.get_backend_name() == "sqlite":
        connect_args["check_same_thread"] = False
        if made.database in (None, "", ":memory:"):
            # A bare in-memory SQLite database is per-connection; pin the engine to one
            # connection so every session in a process (and this sprint's tests) sees the
            # same schema and data.
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, future=True, connect_args=connect_args, **kwargs)
    if made.get_backend_name() == "sqlite":
        from sqlalchemy import event as sa_event

        @sa_event.listens_for(engine, "connect")
        def _fk_pragma(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine: Engine) -> None:
    """Create every table from the ORM metadata. Test/dev convenience only — the canonical,
    reviewable schema change process for Postgres is the Alembic migration under
    services/db/migrations/versions (docs/04 E-11)."""
    import services.db.models  # noqa: F401 — registers tables on Base.metadata

    Base.metadata.create_all(engine)


@contextmanager
def session_scope(sessionmaker_: sessionmaker[Session]) -> Iterator[Session]:
    session = sessionmaker_()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
