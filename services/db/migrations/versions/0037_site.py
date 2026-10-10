"""`site` and `site_member`: the parent over proposals that share a place (owner decision
2026-10-10, answering docs/51 §7 Q6; docs/21 §3.25).

"Use unique identifiers: if records share an address, put them under the latest and largest filing
for that address, list the others as subprojects in the same site, and do your best to label the
relationships." A `site` is a parent with a stable public id of its own; its members are existing
proposals, which keep theirs. `site_member` holds one proposal's membership (at most one site per
proposal), the rule that grouped it, its plant group and group head (site > plant group > unit),
its relationship to the site's lead and the basis those labels were read from. `site_audit` records
what each rebuild did to a site (created, members gained or lost, lead changed, split, merged,
retired), for operators only.

The tables are written by `services/sites/build.py` at the end of each resolve tick (and by
`python -m services.sites`); this migration writes no rows, for the reason 0026 gives: the
grouping rules are application code that will keep changing (`RULE_VERSION`), and a migration that
froze one version of them would diverge from the builder.

Guards follow 0024/0026: the SQLite round-trip tests build today's `Base.metadata` (which already
has both tables) and stamp an earlier revision, so each step checks what is present. Downgrade
drops the three tables. It does not refuse: every row is re-derivable by running the builder again;
only the ids of sites would be new (the reason the builder never deletes a site).

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID, JSONVariant

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported from `services.db.models` (0020-0026's convention).
GROUPING_RULES = ("eia_plant", "exact_point_sponsor", "exact_point_stem", "poi_sponsor", "poi_stem")
RELATIONS = (
    "lead",
    "unit_of",
    "phase_of",
    "co_located",
    "refiling_of",
    "superseded_by",
    "expansion_of",
    "expanded_by",
    "same_site",
)
CONFIDENCES = ("high", "medium", "low")
REVIEW_FLAGS = ("oversize",)
AUDIT_KINDS = ("created", "members_gained", "members_lost", "lead_changed", "split", "merged", "retired")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    if not _has_table("site"):
        op.create_table(
            "site",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("public_id", sa.Text, nullable=False),
            sa.Column("slug", sa.Text, nullable=False),
            sa.Column("name_display", sa.Text, nullable=False),
            sa.Column("lead_proposal_id", GUID(), sa.ForeignKey("proposal.id"), nullable=True),
            sa.Column("member_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("rule_version", sa.Text, nullable=False),
            sa.Column("review_flag", sa.Text, nullable=True),
            sa.Column("anchors", JSONVariant(), nullable=False, server_default="{}"),
            sa.Column("built_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("retired_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("successor_site_id", GUID(), sa.ForeignKey("site.id"), nullable=True),
            *_timestamps(),
            sa.CheckConstraint(
                "review_flag IS NULL OR review_flag IN (" + ", ".join(repr(v) for v in REVIEW_FLAGS) + ")",
                name="ck_site_review_flag_vocab",
            ),
            sa.UniqueConstraint("public_id", name="uq_site_public_id"),
            sa.UniqueConstraint("slug", name="uq_site_slug"),
        )
        op.create_index("ix_site_lead_proposal_id", "site", ["lead_proposal_id"])
        op.create_index("ix_site_retired_at", "site", ["retired_at"])
    if not _has_table("site_member"):
        op.create_table(
            "site_member",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("site_id", GUID(), sa.ForeignKey("site.id"), nullable=False),
            sa.Column("proposal_id", GUID(), sa.ForeignKey("proposal.id"), nullable=False),
            sa.Column("is_lead", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("lead_rank", sa.Integer, nullable=False, server_default="0"),
            sa.Column("group_key", sa.Text, nullable=False),
            sa.Column("parent_proposal_id", GUID(), sa.ForeignKey("proposal.id"), nullable=True),
            sa.Column("grouping_rule", sa.Text, nullable=False),
            sa.Column("grouping_evidence", JSONVariant(), nullable=False, server_default="{}"),
            sa.Column("relation", sa.Text, nullable=False),
            sa.Column("relation_rule", sa.Text, nullable=False),
            sa.Column("confidence", sa.Text, nullable=False),
            sa.Column("basis", JSONVariant(), nullable=False, server_default="{}"),
            *_timestamps(),
            sa.CheckConstraint(
                f"grouping_rule IN {GROUPING_RULES!r}", name="ck_site_member_grouping_rule_vocab"
            ),
            sa.CheckConstraint(f"relation IN {RELATIONS!r}", name="ck_site_member_relation_vocab"),
            sa.CheckConstraint(f"confidence IN {CONFIDENCES!r}", name="ck_site_member_confidence_vocab"),
            sa.UniqueConstraint("proposal_id", name="uq_site_member_proposal_id"),
        )
        op.create_index("ix_site_member_site_id", "site_member", ["site_id"])
    if not _has_table("site_audit"):
        op.create_table(
            "site_audit",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("site_id", GUID(), sa.ForeignKey("site.id"), nullable=False),
            sa.Column("kind", sa.Text, nullable=False),
            sa.Column("detail", JSONVariant(), nullable=False, server_default="{}"),
            sa.Column("rule_version", sa.Text, nullable=False),
            sa.Column(
                "recorded_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.CheckConstraint(f"kind IN {AUDIT_KINDS!r}", name="ck_site_audit_kind_vocab"),
        )
        op.create_index("ix_site_audit_site_recorded", "site_audit", ["site_id", "recorded_at"])


def downgrade() -> None:
    if _has_table("site_audit"):
        op.drop_index("ix_site_audit_site_recorded", table_name="site_audit")
        op.drop_table("site_audit")
    if _has_table("site_member"):
        op.drop_index("ix_site_member_site_id", table_name="site_member")
        op.drop_table("site_member")
    if _has_table("site"):
        op.drop_index("ix_site_retired_at", table_name="site")
        op.drop_index("ix_site_lead_proposal_id", table_name="site")
        op.drop_table("site")
