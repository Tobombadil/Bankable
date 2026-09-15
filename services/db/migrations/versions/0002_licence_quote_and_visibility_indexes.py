"""Add `licence.quote_text`, and the indexes the visibility predicate was missing.

Two independent fixes bundled into one migration (services/README.md "Sprint 2 fixes" has the
full writeup of both):

1. **`licence.quote_text`** (services/db/models.py `Licence.quote_text`). The public
   `source`/`licence` resources had no field for `data/sources.yaml`'s free-text `license` clause
   (the actual quoted terms) — only `notes` (data-engineer commentary about them, already a
   column). `web/README.md` "Missing from the API" item 3 asked for one so the provenance panel
   can render a real quote instead of a composed paraphrase; `services/ingest/loader.py` now
   populates it from `SourceEntry.license` at ingest time.

2. **`ix_proposal_source_proposal_id`, `ix_opportunity_source_opportunity_id`,
   `ix_source_publish_state`**. `services/api/visibility.py`'s `_has_public_source` is a
   correlated `EXISTS` keyed on `proposal_id`/`opportunity_id` for every candidate row; neither
   column had an index, so both Postgres and SQLite plan a full `proposal_source`/
   `opportunity_source` scan per outer row rather than an index seek. Measured at ~10,400 real
   proposals in SQLite: 14-75s per list/geo page before these indexes, ~0.1s after (`EXPLAIN QUERY
   PLAN` before/after is in services/README.md) — this was the entire performance gap;
   `public_at` was already materialised (docs/21 §5.4) and needed no change.

Not run against a live database in this environment (Postgres is not installed here —
services/README.md's caveat on `0001_initial_schema.py` applies here too); checked for internal
consistency (`alembic history` resolves the revision graph) only. The SQLite-side timing above was
measured directly (services/README.md), which is why this migration is trusted despite that.

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
    op.create_index("ix_proposal_source_proposal_id", "proposal_source", ["proposal_id"])
    op.create_index("ix_opportunity_source_opportunity_id", "opportunity_source", ["opportunity_id"])
    op.create_index("ix_source_publish_state", "source", ["publish_state"])


def downgrade() -> None:
    op.drop_index("ix_source_publish_state", table_name="source")
    op.drop_index("ix_opportunity_source_opportunity_id", table_name="opportunity_source")
    op.drop_index("ix_proposal_source_proposal_id", table_name="proposal_source")
    op.drop_column("licence", "quote_text")
