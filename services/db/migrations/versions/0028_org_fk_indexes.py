"""Leading indexes on `proposal.sponsor_org_id` and `opportunity.issuer_org_id` (audit A13).

The audit listed 58 single-column foreign keys with no leading index. These two are the ones a
hot route joins on and a measurement showed matter: the organisation detail
(`GET /v1/organizations/{id}`) counts the proposals the organisation sponsors and the
opportunities it issues. The company index calls that route once per row (25 per render).

Measured 2026-09-30 on a Postgres 16 copy of the audit store (11,098 proposals, 707
opportunities). Median of 9 warm requests, two rounds each:

| Route | Without | With |
|---|---|---|
| detail, 157 sponsored proposals | 26.9 / 26.7 ms | 15.5 / 15.4 ms |
| detail, 41 sponsored proposals | 19.5 / 22.6 ms | 15.3 / 14.8 ms |

Routes that did not move with them, and so gain no index here: `/v1/organizations/{id}/proposals`,
`/v1/proposals?sponsor_id=`, the organisation list, organisation nearby-proposals. Indexes on
`proposal.location_id`, `opportunity.location_id`, `match.opportunity_id`, `post.event_id` and
`webhook_delivery.event_id` were measured with them and moved nothing on this data (no posts or
webhook deliveries exist in it), so they are left out.

Guards follow 0026: the SQLite round-trip tests build today's `Base.metadata`, which already has
both indexes, and stamp an earlier revision, so each step checks what is present.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `(index name, table, column)`.
INDEXES = (
    ("ix_proposal_sponsor_org_id", "proposal", "sponsor_org_id"),
    ("ix_opportunity_issuer_org_id", "opportunity", "issuer_org_id"),
)


def _indexes(table: str) -> set[str]:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(table):
        return set()
    return {str(i["name"]) for i in sa.inspect(bind).get_indexes(table) if i.get("name")}


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    for name, table, column in INDEXES:
        if _has_table(table) and name not in _indexes(table):
            op.create_index(name, table, [column])


def downgrade() -> None:
    for name, table, _column in INDEXES:
        if name in _indexes(table):
            op.drop_index(name, table_name=table)
