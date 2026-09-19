"""Canonical store models and session helpers (docs/21-data-model.md; ADR 0003).

Canonical DDL targets Postgres 16 + PostGIS (services/db/migrations). This sprint's test suite
runs on SQLite because Postgres is not installed in this environment; services/db/types.py
supplies a dialect-aware fallback for the Postgres-only types (UUID, JSONB, text[], geography)
so the same ORM models create equivalent tables on both engines. See services/README.md.
"""

from services.db.base import Base
from services.db.session import get_engine, get_sessionmaker, init_db

__all__ = ["Base", "get_engine", "get_sessionmaker", "init_db"]
