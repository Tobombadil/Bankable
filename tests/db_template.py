"""A fresh, fully migrated in-memory SQLite engine per test, copied from a per-process template
(audit QA-2 step 2) and disposed of at teardown (audit QA-3).

`init_db` (`Base.metadata.create_all`, ~165 schema objects) costs 50 ms warm and 275 ms cold per
call; about 1,070 test definitions take a fresh database through the `db_sessionmaker` fixtures in
`tests/conftest.py`, `services/api/conftest.py` and `services/billing/conftest.py`. Here the schema is
created once per process and every test gets a byte-for-byte copy through SQLite's online backup API
(~0.5 ms). The engine is the same shape `services.db.session.get_engine` builds for an in-memory URL
(one `StaticPool` connection shared by every session, `PRAGMA foreign_keys=ON` on connect), so a
test cannot tell the difference. The template is rebuilt if a test registers another table on
`Base.metadata` after it was made.

Test support only: production and `make dev` still build their schema with `init_db`/Alembic.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy import event as sa_event
from sqlalchemy.pool import StaticPool

from services.db.base import Base
from services.db.session import get_engine, init_db

_lock = threading.Lock()
_template: sqlite3.Connection | None = None
_template_tables: frozenset[str] = frozenset()


def _template_connection() -> sqlite3.Connection:
    global _template, _template_tables
    with _lock:
        import services.db.models
        import services.resolve.models  # noqa: F401 -- the same registrations init_db makes

        tables = frozenset(Base.metadata.tables)
        if _template is None or tables != _template_tables:
            engine = get_engine("sqlite+pysqlite:///:memory:")
            try:
                init_db(engine)
                pooled = engine.raw_connection()
                try:
                    source = pooled.driver_connection
                    assert isinstance(source, sqlite3.Connection)
                    copy = sqlite3.connect(":memory:", check_same_thread=False)
                    source.backup(copy)
                finally:
                    pooled.close()
            finally:
                engine.dispose()
            if _template is not None:
                _template.close()
            _template, _template_tables = copy, tables
        return _template


def fresh_engine() -> Engine:
    """A new in-memory engine holding the full current schema and no rows."""
    template = _template_connection()

    def creator() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        with _lock:
            template.backup(conn)
        return conn

    engine = create_engine("sqlite+pysqlite://", creator=creator, poolclass=StaticPool, future=True)

    @sa_event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


@contextmanager
def disposing(engine: Engine) -> Iterator[Engine]:
    """Yield `engine` and dispose of it afterwards. Without this every test leaves an engine and
    its `StaticPool` connection for the cyclic collector, which is how a full run reached ~3.57 M
    tracked objects and multi-second generation-2 collections (audit QA-3)."""
    try:
        yield engine
    finally:
        engine.dispose()
