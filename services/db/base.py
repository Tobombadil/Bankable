"""Declarative base and naming convention shared by every model (docs/21 §1)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase

# Stable constraint names make Alembic autogenerate diffs (and downgrade()) predictable —
# docs/04 E-11 requires migrations to be tested both ways.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
