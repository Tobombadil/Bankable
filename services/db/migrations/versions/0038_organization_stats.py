"""`organization_stats`: per-organisation figures for the companies index (2026-10-22, lane org-stats).

The owner asked for a companies page that searches, filters and sorts by size: "sort by companies
with the most assets or GWs", by clicking a column title. `GET /v1/organizations` could not: its
rows carried no figure, and the page fetched each company's detail to print one (25 extra calls a
page; `web/organizations.py::_org_holdings`). Sorting 8,454 companies by MW needs the MW of every
one before the first page, which the portfolio and pipeline endpoints answer one company at a time.

One row per (organisation, scope, tier): `scope` is `self` or `group` (the organisation and every
organisation below it in the ownership tree), `tier` the visibility predicate counted under
(`public`; `pro`, which also serves `api`). The figures are those of
`GET /v1/organizations/{id}/portfolio` (distinct current assets by role, nameplate MW, owned MW
where a share is stated) and `/pipeline` (the active pipeline and its MW, withdrawn projects),
plus visible opportunities issued and the states, grid operators and technologies touched. Derived
data: no provenance quartet of its own (as `site`); `sources` names the registers behind each row.

The table is written by `services/api/org_stats_refresh.py` after each context load and resolve pass;
this migration writes no rows (0037's reason: the counting rules are application code). A store
upgraded to 0038 serves `stats: null` on every list row until the first refresh, which the next
resolve tick runs, or `python -m services.api.org_stats_refresh` by hand.

Guards follow 0037 (the SQLite round trip builds today's `Base.metadata`, which already has the
table, and stamps 0038). Downgrade drops the table; it does not refuse, every row is re-derivable.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID, JSONVariant

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported from `services.db.models` (0020-0037's convention).
SCOPES = ("self", "group")
TIERS = ("public", "pro")
TABLE = "organization_stats"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _count(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer, nullable=False, server_default="0")


def upgrade() -> None:
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("organization_id", GUID(), sa.ForeignKey("organization.id"), nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("tier", sa.Text, nullable=False),
        sa.Column("organizations", sa.Integer, nullable=False, server_default="1"),
        _count("assets"),
        _count("operated_assets"),
        _count("owned_assets"),
        sa.Column("operating_mw", sa.Numeric(14, 3), nullable=True),
        _count("operating_mw_assets"),
        sa.Column("owned_mw", sa.Numeric(14, 3), nullable=True),
        _count("owned_mw_assets"),
        _count("pipeline_count"),
        sa.Column("pipeline_mw", sa.Numeric(14, 3), nullable=True),
        _count("pipeline_mw_records"),
        _count("withdrawn_count"),
        _count("proposals"),
        _count("rfps_issued"),
        _count("rfps_open"),
        sa.Column("rfp_issuers", sa.Text, nullable=False, server_default=""),
        sa.Column("states", sa.Text, nullable=False, server_default=""),
        sa.Column("isos", sa.Text, nullable=False, server_default=""),
        sa.Column("technologies", sa.Text, nullable=False, server_default=""),
        sa.Column("sources", JSONVariant(), nullable=False, server_default="[]"),
        sa.Column("gate_key", sa.Text, nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("organization_id", "scope", "tier", name="pk_organization_stats"),
        sa.CheckConstraint(f"scope IN {SCOPES!r}", name="ck_organization_stats_scope_vocab"),
        sa.CheckConstraint(f"tier IN {TIERS!r}", name="ck_organization_stats_tier_vocab"),
    )


def downgrade() -> None:
    if _has_table(TABLE):
        op.drop_table(TABLE)
