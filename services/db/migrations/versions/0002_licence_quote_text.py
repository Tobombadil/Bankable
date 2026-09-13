"""Add `licence.quote_text` (services/db/models.py `Licence.quote_text`).

The public `source`/`licence` resources had no field for `data/sources.yaml`'s free-text
`license` clause (the actual quoted terms) — only `notes` (data-engineer commentary about them,
already a column). `web/README.md` "Missing from the API" item 3 asked for one so the provenance
panel can render a real quote instead of a composed paraphrase; `services/ingest/loader.py` now
populates it from `SourceEntry.license` at ingest time.

Not run against a live database in this environment (Postgres is not installed here —
services/README.md's caveat on `0001_initial_schema.py` applies here too); checked for internal
consistency (`alembic history` resolves the revision graph) only.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("licence", sa.Column("quote_text", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("licence", "quote_text")
