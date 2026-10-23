"""Private projects: "Build a deal" (docs/19 §4.1; ADR 0009; owner decisions of 2026-10-22).

Nine tables. Eight hold one user's private work and are reachable only through
`services.projects.authz.authorize`: `private_project`, `private_project_member`,
`private_project_link`, `private_project_item`, `private_project_evidence`, `private_project_note`,
`private_project_invitation`, `private_project_activity`. The ninth, `checklist_template_item`,
holds a loaded checklist content pack (public structure; private guidance text).

- **Ownership is a member row.** `private_project` has no owner column; a partial unique index on
  `private_project_member (project_id) WHERE role = 'owner'` allows one owner per project.
- **No foreign key into a public table.** A link is a record type and a public id, resolved live,
  so merges, takedowns and licence changes never cascade into private rows.
- **Users are referenced without cascades.** Users are anonymised, never deleted (docs/19 B3);
  erasure deletes or anonymises private rows in code (docs/19 §5.4).
- **No `verified` status** in `private_project_item.status`, and no score column anywhere.
- **Invitation tokens are stored only as SHA-256** (`token_hash char(64)`), and one invitation may
  be open per (project, address hash).
- Every time column is `timestamptz`; coordinates are `numeric(9,6)`, not PostGIS (v1 needs no
  spatial query, and the tests run on SQLite).

This migration writes no rows. The generic checklist is read from `data/checklist/generic-v1.yaml`;
a private pack is written by `python -m services.projects.load_checklist`.

Guards follow 0037/0038: the SQLite round trips build today's `Base.metadata` (which may already
hold these tables) and stamp an earlier revision, so each create checks what is present. Downgrade
drops the nine tables. That deletes users' private projects, so it is for a rollback before launch;
after launch, take a backup first.

**`down_revision`.** 0038 is the newest revision on this lane's base; 0039 (site finder) is held by
a parallel lane. The coordinator relinks this to whichever has landed (docs/19 §4.1).

Revision ID: 0040
Revises: 0038
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID, JSONVariant, TextArray

revision: str = "0040"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported from `services.projects.models` (0020-0038's convention);
#: `services/projects/test_migration.py` pins them equal.
PROJECT_STAGES = ("prospecting", "site_control", "development", "ready_to_build", "construction", "operating")
PROJECT_TECHNOLOGIES = (
    "biomass",
    "coal",
    "fuel_cell",
    "gas_cc",
    "gas_ct",
    "gas_ice",
    "gas_other",
    "gas_steam",
    "geothermal",
    "hydro",
    "hydrogen",
    "load",
    "marine",
    "nuclear",
    "oil",
    "pumped_storage",
    "solar",
    "solar_storage",
    "solar_thermal",
    "storage",
    "transmission",
    "waste",
    "wind",
    "wind_offshore",
    "wind_storage",
    "other",
)
POINT_PRECISIONS = ("user", "exact", "county_centroid", "state_centroid", "corridor_centre")
MEMBER_ROLES = ("owner", "editor", "viewer")
INVITATION_ROLES = ("editor", "viewer")
LINK_RECORD_TYPES = (
    "proposal",
    "site",
    "asset",
    "opportunity",
    "organization",
    "interconnection_point",
    "corridor",
)
LINK_RELATIONS = (
    "this_project",
    "nearby",
    "candidate_site",
    "grid_connection",
    "offtake_or_rfp",
    "funding",
    "counterparty",
    "comparable",
)
CHECKLIST_FOLDERS = ("contracts", "finance", "security", "permits", "feasibility", "legal_corporate")
ITEM_PILLARS = ("fundamentals", "counterparties", "regulatory_site", "sponsor")
ITEM_STATUSES = ("not_started", "in_progress", "done", "not_applicable")
EVIDENCE_STATES = ("suggested", "accepted", "dismissed")
EVIDENCE_RULES = (
    "queue_position",
    "executed_interconnection_agreement",
    "gb_tec_register",
    "air_permit",
    "federal_record",
)
ACTIVITY_ACTIONS = (
    "created",
    "field_changed",
    "link_added",
    "link_removed",
    "item_status_changed",
    "evidence_accepted",
    "evidence_dismissed",
    "note_added",
    "note_edited",
    "note_deleted",
    "member_invited",
    "member_joined",
    "member_removed",
    "role_changed",
    "ownership_transferred",
    "invitation_revoked",
    "archived",
    "unarchived",
    "exported",
)
#: JSON `null` and SQL NULL both mean "no params" (`services.projects.models.PARAMS_ABSENT`).
PARAMS_ABSENT = "(params IS NULL OR CAST(params AS TEXT) = 'null')"
TITLE_MAX_CHARS = 200
ITEM_NAME_MAX_CHARS = 200
NOTE_MAX_CHARS = 20_000

#: Children first: the order `downgrade` drops them in.
TABLES = (
    "private_project_activity",
    "private_project_invitation",
    "private_project_note",
    "private_project_evidence",
    "private_project_item",
    "private_project_link",
    "private_project_member",
    "checklist_template_item",
    "private_project",
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _check(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=f"ck_{table}_{name}")


def _now(name: str, *, nullable: bool = False) -> sa.Column:
    if nullable:
        return sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=True)
    return sa.Column(name, sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())


def _id() -> sa.Column:
    return sa.Column("id", GUID(), nullable=False)


def _project_id(table: str) -> sa.Column:
    return sa.Column(
        "project_id",
        GUID(),
        sa.ForeignKey(
            "private_project.id", ondelete="CASCADE", name=f"fk_{table}_project_id_private_project"
        ),
        nullable=False,
    )


def _user(table: str, column: str, *, nullable: bool) -> sa.Column:
    return sa.Column(
        column, GUID(), sa.ForeignKey("user.id", name=f"fk_{table}_{column}_user"), nullable=nullable
    )


def _pk(table: str) -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint("id", name=f"pk_{table}")


def _create_private_project() -> None:
    t = "private_project"
    op.create_table(
        t,
        _id(),
        sa.Column("public_id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("technology", sa.Text, nullable=True),
        sa.Column("capacity_mw", sa.Numeric(12, 3), nullable=True),
        sa.Column("storage_mwh", sa.Numeric(12, 3), nullable=True),
        sa.Column("stage", sa.Text, nullable=False, server_default="prospecting"),
        sa.Column("lat", sa.Numeric(9, 6), nullable=True),
        sa.Column("lon", sa.Numeric(9, 6), nullable=True),
        sa.Column("point_precision", sa.Text, nullable=True),
        sa.Column("origin_type", sa.Text, nullable=True),
        sa.Column("origin_public_id", sa.Text, nullable=True),
        _now("archived_at", nullable=True),
        _now("created_at"),
        _now("updated_at"),
        _pk(t),
        sa.UniqueConstraint("public_id", name=f"uq_{t}_public_id"),
        _check(t, "title_length", f"length(title) BETWEEN 1 AND {TITLE_MAX_CHARS}"),
        _check(t, "technology_vocab", _in("technology", PROJECT_TECHNOLOGIES)),
        _check(t, "capacity_mw_non_negative", "capacity_mw >= 0"),
        _check(t, "storage_mwh_non_negative", "storage_mwh >= 0"),
        _check(t, "stage_vocab", _in("stage", PROJECT_STAGES)),
        _check(t, "point_pair", "(lat IS NULL) = (lon IS NULL)"),
        _check(t, "lat_range", "lat BETWEEN -90 AND 90"),
        _check(t, "lon_range", "lon BETWEEN -180 AND 180"),
        _check(t, "point_precision_vocab", _in("point_precision", POINT_PRECISIONS)),
        _check(t, "point_precision_pair", "(lat IS NULL) = (point_precision IS NULL)"),
        _check(t, "origin_type_vocab", _in("origin_type", LINK_RECORD_TYPES)),
        _check(t, "origin_pair", "(origin_type IS NULL) = (origin_public_id IS NULL)"),
    )


def _create_member() -> None:
    t = "private_project_member"
    op.create_table(
        t,
        _id(),
        _project_id(t),
        _user(t, "user_id", nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        _user(t, "added_by_user_id", nullable=True),
        _now("added_at"),
        _pk(t),
        sa.UniqueConstraint("project_id", "user_id", name=f"uq_{t}_project_id_user_id"),
        _check(t, "role_vocab", _in("role", MEMBER_ROLES)),
    )
    op.create_index(
        "uq_private_project_member_one_owner",
        t,
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("role = 'owner'"),
        sqlite_where=sa.text("role = 'owner'"),
    )
    op.create_index("ix_private_project_member_user_id", t, ["user_id"])


def _create_link() -> None:
    t = "private_project_link"
    op.create_table(
        t,
        _id(),
        _project_id(t),
        sa.Column("record_type", sa.Text, nullable=False),
        sa.Column("record_public_id", sa.Text, nullable=True),
        sa.Column("params", JSONVariant(), nullable=True),
        sa.Column("relation", sa.Text, nullable=False),
        _user(t, "added_by_user_id", nullable=True),
        _now("created_at"),
        _pk(t),
        sa.UniqueConstraint(
            "project_id",
            "record_type",
            "record_public_id",
            "relation",
            name=f"uq_{t}_reference",
        ),
        _check(t, "record_type_vocab", _in("record_type", LINK_RECORD_TYPES)),
        _check(t, "relation_vocab", _in("relation", LINK_RELATIONS)),
        _check(
            t,
            "reference_present",
            f"record_public_id IS NOT NULL OR (record_type = 'corridor' AND NOT {PARAMS_ABSENT})",
        ),
        _check(t, "params_corridor_only", f"{PARAMS_ABSENT} OR record_type = 'corridor'"),
    )


def _create_template() -> None:
    t = "checklist_template_item"
    op.create_table(
        t,
        _id(),
        sa.Column("pack_version", sa.Text, nullable=False),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("folder", sa.Text, nullable=False),
        sa.Column("pillar", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("guidance", sa.Text, nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("technologies", TextArray(), nullable=True),
        _now("loaded_at"),
        _pk(t),
        sa.UniqueConstraint("pack_version", "key", name=f"uq_{t}_pack_version_key"),
        _check(t, "folder_vocab", _in("folder", CHECKLIST_FOLDERS)),
        _check(t, "pillar_vocab", _in("pillar", ITEM_PILLARS)),
    )


def _create_item() -> None:
    t = "private_project_item"
    op.create_table(
        t,
        _id(),
        _project_id(t),
        sa.Column("template_key", sa.Text, nullable=True),
        sa.Column("pack_version", sa.Text, nullable=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("folder", sa.Text, nullable=False),
        sa.Column("pillar", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="not_started"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        _user(t, "updated_by_user_id", nullable=True),
        _now("updated_at"),
        _pk(t),
        sa.UniqueConstraint("project_id", "template_key", name=f"uq_{t}_project_id_template_key"),
        _check(t, "status_vocab", _in("status", ITEM_STATUSES)),
        _check(t, "folder_vocab", _in("folder", CHECKLIST_FOLDERS)),
        _check(t, "pillar_vocab", _in("pillar", ITEM_PILLARS)),
        _check(t, "name_length", f"length(name) BETWEEN 1 AND {ITEM_NAME_MAX_CHARS}"),
    )


def _create_evidence() -> None:
    t = "private_project_evidence"
    op.create_table(
        t,
        _id(),
        _project_id(t),
        sa.Column(
            "item_id",
            GUID(),
            sa.ForeignKey(
                "private_project_item.id", ondelete="CASCADE", name=f"fk_{t}_item_id_private_project_item"
            ),
            nullable=False,
        ),
        sa.Column("record_type", sa.Text, nullable=False),
        sa.Column("record_public_id", sa.Text, nullable=False),
        sa.Column("rule", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="suggested"),
        _user(t, "decided_by_user_id", nullable=True),
        _now("decided_at", nullable=True),
        _now("created_at"),
        _pk(t),
        sa.UniqueConstraint("item_id", "record_type", "record_public_id", "rule", name=f"uq_{t}_reference"),
        _check(t, "record_type_vocab", _in("record_type", LINK_RECORD_TYPES)),
        _check(t, "rule_vocab", _in("rule", EVIDENCE_RULES)),
        _check(t, "state_vocab", _in("state", EVIDENCE_STATES)),
    )
    op.create_index("ix_private_project_evidence_project_id", t, ["project_id"])


def _create_note() -> None:
    t = "private_project_note"
    op.create_table(
        t,
        _id(),
        sa.Column("public_id", sa.Text, nullable=False),
        _project_id(t),
        _user(t, "author_user_id", nullable=True),
        sa.Column("body", sa.Text, nullable=False),
        _now("created_at"),
        _now("updated_at"),
        _pk(t),
        sa.UniqueConstraint("public_id", name=f"uq_{t}_public_id"),
        _check(t, "body_length", f"length(body) BETWEEN 1 AND {NOTE_MAX_CHARS}"),
    )
    op.create_index("ix_private_project_note_project_id_created_at", t, ["project_id", "created_at"])


def _create_invitation() -> None:
    t = "private_project_invitation"
    op.create_table(
        t,
        _id(),
        sa.Column("public_id", sa.Text, nullable=False),
        _project_id(t),
        sa.Column("email", sa.Text, nullable=True),
        sa.Column("email_hash", sa.Text, nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        _user(t, "invited_by_user_id", nullable=False),
        _now("created_at"),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        _now("accepted_at", nullable=True),
        _user(t, "accepted_by_user_id", nullable=True),
        _now("revoked_at", nullable=True),
        _pk(t),
        sa.UniqueConstraint("public_id", name=f"uq_{t}_public_id"),
        sa.UniqueConstraint("token_hash", name=f"uq_{t}_token_hash"),
        _check(t, "role_vocab", _in("role", INVITATION_ROLES)),
        _check(t, "expiry_after_creation", "expires_at > created_at"),
    )
    open_invitation = "accepted_at IS NULL AND revoked_at IS NULL AND email IS NOT NULL"
    op.create_index(
        "uq_private_project_invitation_open_email",
        t,
        ["project_id", "email_hash"],
        unique=True,
        postgresql_where=sa.text(open_invitation),
        sqlite_where=sa.text(open_invitation),
    )
    op.create_index("ix_private_project_invitation_project_id", t, ["project_id"])
    op.create_index(
        "ix_private_project_invitation_invited_by_user_id_created_at", t, ["invited_by_user_id", "created_at"]
    )


def _create_activity() -> None:
    t = "private_project_activity"
    op.create_table(
        t,
        _id(),
        _project_id(t),
        _user(t, "actor_user_id", nullable=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("detail", JSONVariant(), nullable=False, server_default="{}"),
        _now("occurred_at"),
        _pk(t),
        _check(t, "action_vocab", _in("action", ACTIVITY_ACTIONS)),
    )
    op.create_index("ix_private_project_activity_project_id_occurred_at", t, ["project_id", "occurred_at"])


_CREATORS = (
    ("private_project", _create_private_project),
    ("private_project_member", _create_member),
    ("private_project_link", _create_link),
    ("checklist_template_item", _create_template),
    ("private_project_item", _create_item),
    ("private_project_evidence", _create_evidence),
    ("private_project_note", _create_note),
    ("private_project_invitation", _create_invitation),
    ("private_project_activity", _create_activity),
)


def upgrade() -> None:
    for table, create in _CREATORS:
        if not _has_table(table):
            create()


def downgrade() -> None:
    for table in TABLES:
        if _has_table(table):
            op.drop_table(table)
