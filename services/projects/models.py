"""The private-project tables (docs/19 §4.1; migrations `0040_private_projects`, `0042_tracker_kinds`).

Nine tables, all named `private_*` except the checklist template, which is public content:

- `private_project`: one item of the user's tracker: a project, a bid or an investment (`kind`, with
  its own stage vocabulary, `services.projects.kinds`; migration 0042). A bid may name the project it
  is for (`parent_project_id`, set to NULL when that project is deleted). **No owner column**:
  ownership is a `private_project_member` row with role `owner`, so one table answers every access
  question (`services.projects.authz`).
- `private_project_member`: one row per user per project; a partial unique index allows one owner.
  It also holds the member's own view of the item: when they last opened it (`last_viewed_at`, the
  "changed since your last visit" mark) and whether they asked to be emailed its changes
  (`email_changes`, an explicit opt-in per item; stored only, nothing is sent yet).
- `private_project_link`: a reference to a public record by type and public id. **No foreign key
  into a public table**, on purpose: a link is resolved live through the public read path, so a
  merge, takedown or licence change never cascades into private rows (docs/19 §2.2, §4.2).
- `checklist_template_item`: rows of a loaded private content pack (`load_checklist`).
- `private_project_item`, `private_project_evidence`: the project's checklist and the references
  behind suggested public-record evidence. There is no `verified` status.
- `private_project_note`, `private_project_invitation`, `private_project_activity`.

**Ids.** `prj_`, `nte_` and `inv_` public ids are drawn from a random uuid4, not derived from the
row's primary key, so a private URL carries no creation time and no internal id. Every private
table's primary key is a random uuid4 too, not the uuid7 docs/21 §1 uses for public records:
checklist item, link and evidence ids appear in URLs (`…/checklist/{item_id}`, `…/links/{link_id}`)
and in activity detail, and a uuid7 there would date the row. Only `checklist_template_item`,
which is public content, keeps uuid7.

**Never in `event`.** Private work writes `private_project_activity` only (docs/19 §4.1).
Activity `detail` holds field names, ids and vocabulary tokens; `add_activity` checks every value
against its key's own shape (`ACTIVITY_DETAIL_SHAPES`), so a title, note or address cannot be
written there.

**Text a database cannot store is refused before any write.** `has_control_characters` finds NUL
(which Postgres `text` rejects with an error whose message would carry the statement's parameters)
and the other control characters and lone surrogates; every user-typed value is checked with it
and refused with a `400`.

Vocabularies are `text` + `CHECK` (docs/21 §1). The migration freezes its own copies, as 0020-0038
do; `test_models.py` pins them equal.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid as _uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, Session, mapped_column

import services.db.models  # noqa: F401 -- registers `user`, which every private table references
from services.db.base import Base
from services.db.types import GUID, JSONVariant, TextArray, uuid7
from services.ids import public_id
from services.projects.kinds import ITEM_KINDS as ITEM_KINDS  # re-exported: models names every vocabulary
from services.projects.kinds import PROJECT_STAGES as PROJECT_STAGES
from services.projects.kinds import kind_condition, stage_for_kind_condition

# ----------------------------------------------------------------------------------- vocabularies
#: The item fields a user sets (docs/19 §2.1; the tracker's kinds, migration 0042), in form order.
#: `kind` is set at creation only; `parent_project_id` and `submitted_on` belong to bids;
#: `archived` is separate.
PROJECT_FIELDS: Final = (
    "title",
    "kind",
    "technology",
    "capacity_mw",
    "storage_mwh",
    "stage",
    "parent_project_id",
    "submitted_on",
    "lat",
    "lon",
    "point_precision",
)
#: Inframation's technology classes (`pipeline.normalize.TECH_RULES`) plus `other`, so a project and
#: its linked records speak one vocabulary (docs/19 §2.1). Frozen here because the column carries a
#: CHECK; `test_models.py` fails when TECH_RULES gains a class, which then needs a migration.
PROJECT_TECHNOLOGIES: Final = (
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
#: `user`: placed or pasted by the user. The rest are copied from the starting record's placement
#: (ADR 0008), or the finder corridor's centre, under the licence rule of docs/19 §7.
POINT_PRECISIONS: Final = ("user", "exact", "county_centroid", "state_centroid", "corridor_centre")
MEMBER_ROLES: Final = ("owner", "editor", "viewer")
#: An invitation never grants ownership; ownership moves only by transfer (docs/19 §2.6).
INVITATION_ROLES: Final = ("editor", "viewer")
LINK_RECORD_TYPES: Final = (
    "proposal",
    "site",
    "asset",
    "opportunity",
    "organization",
    "interconnection_point",
    "corridor",
)
LINK_RELATIONS: Final = (
    "this_project",
    "nearby",
    "candidate_site",
    "grid_connection",
    "offtake_or_rfp",
    "funding",
    "counterparty",
    "comparable",
)
#: The six folders of docs/19 §2.3 (Contracts; Finance; Security; Permits; Feasibility; Legal & Corporate).
CHECKLIST_FOLDERS: Final = ("contracts", "finance", "security", "permits", "feasibility", "legal_corporate")
#: The pillars an item is tagged with. The fifth pillar, completeness, counts every item, so it is
#: never a tag (`services.projects.checklist.READINESS_PILLARS`).
ITEM_PILLARS: Final = ("fundamentals", "counterparties", "regulatory_site", "sponsor")
#: The user's own statement (docs/19 §2.3). **There is no `verified` value, and no certified label.**
ITEM_STATUSES: Final = ("not_started", "in_progress", "done", "not_applicable")
EVIDENCE_STATES: Final = ("suggested", "accepted", "dismissed")
#: Which docs/19 §2.4 rule fired. The item each rule may attach to is
#: `services.projects.checklist.EVIDENCE_RULE_ITEMS`.
EVIDENCE_RULES: Final = (
    "queue_position",
    "executed_interconnection_agreement",
    "gb_tec_register",
    "air_permit",
    "federal_record",
)
ACTIVITY_ACTIONS: Final = (
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

# ----------------------------------------------------------------------------------------- limits
TITLE_MAX_CHARS: Final = 200
ITEM_NAME_MAX_CHARS: Final = 200
NOTE_MAX_CHARS: Final = 20_000
#: docs/19 §9 question 6: v1 defaults until the owner says otherwise.
MAX_ACTIVE_PROJECTS_PER_USER: Final = 25
MAX_LINKS_PER_PROJECT: Final = 50
MAX_PENDING_INVITATIONS_PER_PROJECT: Final = 10
MAX_INVITATIONS_PER_USER_PER_DAY: Final = 20
INVITATION_TTL: Final = dt.timedelta(days=7)

# -------------------------------------------------------------------------------------------- ids
PROJECT_ID_PREFIX: Final = "prj"
NOTE_ID_PREFIX: Final = "nte"
INVITATION_ID_PREFIX: Final = "inv"
PRIVATE_ID_PREFIXES: Final = (PROJECT_ID_PREFIX, NOTE_ID_PREFIX, INVITATION_ID_PREFIX)
_CROCKFORD_TAIL = "[0-9A-HJKMNP-TV-Z]{10,26}"
#: Any private public id inside a string. `services/api/ui_events.py` refuses property values that
#: match it (docs/19 §5.2 item 4); that module may not import this package, so it carries a copy of
#: this pattern and a test pins the two equal.
PRIVATE_ID_PATTERN: Final = re.compile(rf"\b(?:prj|nte|inv)_{_CROCKFORD_TAIL}\b")


def new_private_public_id(prefix: str) -> str:
    """A fresh `prj_`/`nte_`/`inv_` id from a random uuid4: unguessable (122 random bits) and
    unrelated to the row's primary key or creation time."""
    if prefix not in PRIVATE_ID_PREFIXES:
        raise ValueError(f"not a private id prefix: {prefix!r}")
    return public_id(prefix, _uuid.uuid4())


def is_private_public_id(prefix: str, value: object) -> bool:
    """Shape check only, done before any query, so a malformed id costs no database work."""
    return isinstance(value, str) and re.fullmatch(rf"{prefix}_{_CROCKFORD_TAIL}", value) is not None


#: The public id prefix of each linkable record type (`services.ids.public_id`; `api/openapi.yaml`
#: pins `^prop_[0-9A-HJKMNP-TV-Z]{10,26}$` and the rest). A corridor has none yet: the site finder
#: lane settles its id (docs/19 §4.2), so `is_record_public_id` takes a plain token for it.
RECORD_ID_PREFIXES: Final[Mapping[str, str]] = {
    "proposal": "prop",
    "site": "site",
    "asset": "asset",
    "opportunity": "opp",
    "organization": "org",
    "interconnection_point": "poi",
}
_CORRIDOR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_ANY_RECORD_ID = re.compile(rf"(?:{'|'.join(RECORD_ID_PREFIXES.values())})_{_CROCKFORD_TAIL}")


def is_record_public_id(record_type: object, value: object) -> bool:
    """Whether `value` has the shape of a public id of `record_type` (shape only: whether the record
    exists and the viewer may see it is the read path's question). Checked before a reference is
    stored, so nothing but an id reaches a private row's id column."""
    if not isinstance(record_type, str) or not isinstance(value, str):
        return False
    if record_type == "corridor":
        return _CORRIDOR_ID.fullmatch(value) is not None
    prefix = RECORD_ID_PREFIXES.get(record_type)
    return prefix is not None and re.fullmatch(rf"{prefix}_{_CROCKFORD_TAIL}", value) is not None


#: C0 and C1 controls (Unicode category Cc, NUL included) and lone surrogates (Cs).
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
#: The same, less tab, line feed and carriage return: for multi-line text such as a note.
_CONTROL_BUT_LINE_BREAKS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")


def has_control_characters(value: str, *, allow_line_breaks: bool = False) -> bool:
    """Whether `value` holds a character no private text column should store: NUL, which Postgres
    `text` cannot hold (the driver's error then carries the whole statement's parameters into
    logs and error reports), any other control character, or a lone surrogate, which UTF-8 cannot
    encode. With `allow_line_breaks`, tab, line feed and carriage return pass (notes)."""
    pattern = _CONTROL_BUT_LINE_BREAKS if allow_line_breaks else _CONTROL
    return pattern.search(value) is not None


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def as_utc(value: dt.datetime) -> dt.datetime:
    """SQLite returns `timestamptz` columns naive; every write here stores UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _private_pk() -> Mapped[_uuid.UUID]:
    """A random uuid4 primary key: item, link and evidence ids reach URLs and activity detail, and a
    time-ordered uuid7 would date the row (module docstring)."""
    return mapped_column(GUID(), primary_key=True, default=_uuid.uuid4)


def _fk_user(*, nullable: bool) -> Mapped[Any]:
    return mapped_column(GUID(), sa.ForeignKey("user.id"), nullable=nullable)


def _project_fk() -> Mapped[_uuid.UUID]:
    return mapped_column(GUID(), sa.ForeignKey("private_project.id", ondelete="CASCADE"), nullable=False)


def _timestamp(*, nullable: bool = False) -> Mapped[Any]:
    if nullable:
        return mapped_column(sa.DateTime(timezone=True), nullable=True)
    return mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, server_default=sa.func.now()
    )


# ================================================================================ private_project
class PrivateProject(Base):
    __tablename__ = "private_project"

    id: Mapped[_uuid.UUID] = _private_pk()
    public_id: Mapped[str] = mapped_column(
        sa.Text, nullable=False, unique=True, default=lambda: new_private_public_id(PROJECT_ID_PREFIX)
    )
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    technology: Mapped[str | None] = mapped_column(sa.Text)
    capacity_mw: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 3))
    storage_mwh: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 3))
    #: `project`, `bid` or `investment` (`services.projects.kinds`); every row before 0042 is a project.
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False, default="project", server_default="project")
    #: The kind's first stage unless given (`kinds.DEFAULT_STAGES`); `stage_for_kind` ties the two.
    stage: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="prospecting", server_default="prospecting"
    )
    #: A bid's project: one of the user's own projects they can edit when it was set (checked in
    #: `services.projects.authz`, not here). NULL for a bid that stands alone, and set to NULL when
    #: that project is deleted.
    parent_project_id: Mapped[_uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("private_project.id", ondelete="SET NULL")
    )
    #: The day the user says the bid went in. Bids only.
    submitted_on: Mapped[dt.date | None] = mapped_column(sa.Date)
    lat: Mapped[Decimal | None] = mapped_column(sa.Numeric(9, 6))
    lon: Mapped[Decimal | None] = mapped_column(sa.Numeric(9, 6))
    point_precision: Mapped[str | None] = mapped_column(sa.Text)
    origin_type: Mapped[str | None] = mapped_column(sa.Text)
    origin_public_id: Mapped[str | None] = mapped_column(sa.Text)
    archived_at: Mapped[dt.datetime | None] = _timestamp(nullable=True)
    created_at: Mapped[dt.datetime] = _timestamp()
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=sa.func.now(),
    )

    __table_args__ = (
        sa.CheckConstraint(f"length(title) BETWEEN 1 AND {TITLE_MAX_CHARS}", name="title_length"),
        sa.CheckConstraint(_in("technology", PROJECT_TECHNOLOGIES), name="technology_vocab"),
        sa.CheckConstraint("capacity_mw >= 0", name="capacity_mw_non_negative"),
        sa.CheckConstraint("storage_mwh >= 0", name="storage_mwh_non_negative"),
        sa.CheckConstraint(kind_condition(), name="kind_vocab"),
        sa.CheckConstraint(stage_for_kind_condition(), name="stage_for_kind"),
        sa.CheckConstraint("parent_project_id IS NULL OR kind = 'bid'", name="parent_bid_only"),
        sa.CheckConstraint("parent_project_id IS NULL OR parent_project_id <> id", name="parent_not_self"),
        sa.CheckConstraint("submitted_on IS NULL OR kind = 'bid'", name="submitted_bid_only"),
        sa.CheckConstraint("(lat IS NULL) = (lon IS NULL)", name="point_pair"),
        sa.CheckConstraint("lat BETWEEN -90 AND 90", name="lat_range"),
        sa.CheckConstraint("lon BETWEEN -180 AND 180", name="lon_range"),
        sa.CheckConstraint(_in("point_precision", POINT_PRECISIONS), name="point_precision_vocab"),
        sa.CheckConstraint("(lat IS NULL) = (point_precision IS NULL)", name="point_precision_pair"),
        sa.CheckConstraint(_in("origin_type", LINK_RECORD_TYPES), name="origin_type_vocab"),
        sa.CheckConstraint("(origin_type IS NULL) = (origin_public_id IS NULL)", name="origin_pair"),
        sa.Index("ix_private_project_parent_project_id", "parent_project_id"),
    )


# ========================================================================= private_project_member
class PrivateProjectMember(Base):
    __tablename__ = "private_project_member"

    id: Mapped[_uuid.UUID] = _private_pk()
    project_id: Mapped[_uuid.UUID] = _project_fk()
    #: No cascade: users are anonymised, never deleted (docs/19 B3); erasure removes these rows in code.
    user_id: Mapped[_uuid.UUID] = _fk_user(nullable=False)
    role: Mapped[str] = mapped_column(sa.Text, nullable=False)
    added_by_user_id: Mapped[_uuid.UUID | None] = _fk_user(nullable=True)
    added_at: Mapped[dt.datetime] = _timestamp()
    #: When this member last opened the item's page: what "changed since your last visit" counts
    #: from (`added_at` before the first visit). This member's own; never shown to anyone else,
    #: never in analytics, and erased with the membership.
    last_viewed_at: Mapped[dt.datetime | None] = _timestamp(nullable=True)
    #: "Email me changes": this member's explicit opt-in for this one item (the `CLAUDE.md` consent
    #: guardrail). Stored only: no email is sent until a named automated channel is enabled.
    email_changes: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )

    __table_args__ = (
        sa.UniqueConstraint("project_id", "user_id"),
        sa.CheckConstraint(_in("role", MEMBER_ROLES), name="role_vocab"),
        sa.Index(
            "uq_private_project_member_one_owner",
            "project_id",
            unique=True,
            postgresql_where=sa.text("role = 'owner'"),
            sqlite_where=sa.text("role = 'owner'"),
        ),
        sa.Index("ix_private_project_member_user_id", "user_id"),
    )


#: A JSON column given Python `None` stores JSON `null`, not SQL NULL (`JSONVariant` keeps
#: SQLAlchemy's default), so "no params" is either. Text casts compare alike on both dialects.
PARAMS_ABSENT: Final = "(params IS NULL OR CAST(params AS TEXT) = 'null')"


# =========================================================================== private_project_link
class PrivateProjectLink(Base):
    """A reference, resolved live (`services.api.my_projects.resolve_link`). No foreign key to any
    public table, by design."""

    __tablename__ = "private_project_link"

    id: Mapped[_uuid.UUID] = _private_pk()
    project_id: Mapped[_uuid.UUID] = _project_fk()
    record_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    record_public_id: Mapped[str | None] = mapped_column(sa.Text)
    #: The site finder's permalink parameters, for a corridor without a stable id (docs/19 §4.2).
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant())
    relation: Mapped[str] = mapped_column(sa.Text, nullable=False)
    added_by_user_id: Mapped[_uuid.UUID | None] = _fk_user(nullable=True)
    created_at: Mapped[dt.datetime] = _timestamp()

    __table_args__ = (
        # Named by hand: the convention's name is 72 characters, past Postgres's 63.
        sa.UniqueConstraint(
            "project_id",
            "record_type",
            "record_public_id",
            "relation",
            name="uq_private_project_link_reference",
        ),
        sa.CheckConstraint(_in("record_type", LINK_RECORD_TYPES), name="record_type_vocab"),
        sa.CheckConstraint(_in("relation", LINK_RELATIONS), name="relation_vocab"),
        sa.CheckConstraint(
            f"record_public_id IS NOT NULL OR (record_type = 'corridor' AND NOT {PARAMS_ABSENT})",
            name="reference_present",
        ),
        sa.CheckConstraint(f"{PARAMS_ABSENT} OR record_type = 'corridor'", name="params_corridor_only"),
    )


# ======================================================================== checklist_template_item
class ChecklistTemplateItem(Base):
    """A loaded content pack's items (`services.projects.load_checklist`). Public content, not user
    data, but the guidance text of a private pack stays out of the public repository and is shown
    only on members' checklist pages. The generic pack (`data/checklist/generic-v1.yaml`) is read
    from the file and never stored here."""

    __tablename__ = "checklist_template_item"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid7)
    pack_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    folder: Mapped[str] = mapped_column(sa.Text, nullable=False)
    pillar: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    guidance: Mapped[str | None] = mapped_column(sa.Text)
    sort_order: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    #: Empty means every technology.
    technologies: Mapped[list[str] | None] = mapped_column(TextArray())
    #: Which pack is current: the most recently loaded one.
    loaded_at: Mapped[dt.datetime] = _timestamp()

    __table_args__ = (
        sa.UniqueConstraint("pack_version", "key"),
        sa.CheckConstraint(_in("folder", CHECKLIST_FOLDERS), name="folder_vocab"),
        sa.CheckConstraint(_in("pillar", ITEM_PILLARS), name="pillar_vocab"),
    )


# =========================================================================== private_project_item
class PrivateProjectItem(Base):
    __tablename__ = "private_project_item"

    id: Mapped[_uuid.UUID] = _private_pk()
    project_id: Mapped[_uuid.UUID] = _project_fk()
    #: NULL for a custom item.
    template_key: Mapped[str | None] = mapped_column(sa.Text)
    pack_version: Mapped[str | None] = mapped_column(sa.Text)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    folder: Mapped[str] = mapped_column(sa.Text, nullable=False)
    pillar: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="not_started", server_default="not_started"
    )
    sort_order: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0, server_default="0")
    updated_by_user_id: Mapped[_uuid.UUID | None] = _fk_user(nullable=True)
    updated_at: Mapped[dt.datetime] = _timestamp()

    __table_args__ = (
        sa.UniqueConstraint("project_id", "template_key"),
        sa.CheckConstraint(_in("status", ITEM_STATUSES), name="status_vocab"),
        sa.CheckConstraint(_in("folder", CHECKLIST_FOLDERS), name="folder_vocab"),
        sa.CheckConstraint(_in("pillar", ITEM_PILLARS), name="pillar_vocab"),
        sa.CheckConstraint(f"length(name) BETWEEN 1 AND {ITEM_NAME_MAX_CHARS}", name="name_length"),
    )


# ======================================================================= private_project_evidence
class PrivateProjectEvidence(Base):
    """A reference and the rule that fired; the suggestion text is rebuilt live from the record."""

    __tablename__ = "private_project_evidence"

    id: Mapped[_uuid.UUID] = _private_pk()
    project_id: Mapped[_uuid.UUID] = _project_fk()
    item_id: Mapped[_uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("private_project_item.id", ondelete="CASCADE"), nullable=False
    )
    record_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    record_public_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rule: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[str] = mapped_column(
        sa.Text, nullable=False, default="suggested", server_default="suggested"
    )
    decided_by_user_id: Mapped[_uuid.UUID | None] = _fk_user(nullable=True)
    decided_at: Mapped[dt.datetime | None] = _timestamp(nullable=True)
    created_at: Mapped[dt.datetime] = _timestamp()

    __table_args__ = (
        sa.UniqueConstraint(
            "item_id", "record_type", "record_public_id", "rule", name="uq_private_project_evidence_reference"
        ),
        sa.CheckConstraint(_in("record_type", LINK_RECORD_TYPES), name="record_type_vocab"),
        sa.CheckConstraint(_in("rule", EVIDENCE_RULES), name="rule_vocab"),
        sa.CheckConstraint(_in("state", EVIDENCE_STATES), name="state_vocab"),
        sa.Index("ix_private_project_evidence_project_id", "project_id"),
    )


# =========================================================================== private_project_note
class PrivateProjectNote(Base):
    """Plain text. `author_user_id` is NULL after the author's erasure; pages show "Former member"
    (owner, 2026-10-22)."""

    __tablename__ = "private_project_note"

    id: Mapped[_uuid.UUID] = _private_pk()
    public_id: Mapped[str] = mapped_column(
        sa.Text, nullable=False, unique=True, default=lambda: new_private_public_id(NOTE_ID_PREFIX)
    )
    project_id: Mapped[_uuid.UUID] = _project_fk()
    author_user_id: Mapped[_uuid.UUID | None] = _fk_user(nullable=True)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[dt.datetime] = _timestamp()
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=sa.func.now(),
    )

    __table_args__ = (
        sa.CheckConstraint(f"length(body) BETWEEN 1 AND {NOTE_MAX_CHARS}", name="body_length"),
        sa.Index("ix_private_project_note_project_id_created_at", "project_id", "created_at"),
    )


# ===================================================================== private_project_invitation
class PrivateProjectInvitation(Base):
    """docs/19 §2.6. The token is never stored: only its SHA-256 (`token_hash`), as session tokens
    are. `email` is cleared on accept, revoke or expiry; `email_hash` (peppered,
    `services.api.audit.hash_identifier`) stays for the uniqueness of open invitations and for
    erasure."""

    __tablename__ = "private_project_invitation"

    id: Mapped[_uuid.UUID] = _private_pk()
    public_id: Mapped[str] = mapped_column(
        sa.Text, nullable=False, unique=True, default=lambda: new_private_public_id(INVITATION_ID_PREFIX)
    )
    project_id: Mapped[_uuid.UUID] = _project_fk()
    email: Mapped[str | None] = mapped_column(sa.Text)
    email_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(sa.Text, nullable=False)
    invited_by_user_id: Mapped[_uuid.UUID] = _fk_user(nullable=False)
    created_at: Mapped[dt.datetime] = _timestamp()
    expires_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[dt.datetime | None] = _timestamp(nullable=True)
    accepted_by_user_id: Mapped[_uuid.UUID | None] = _fk_user(nullable=True)
    revoked_at: Mapped[dt.datetime | None] = _timestamp(nullable=True)

    __table_args__ = (
        sa.CheckConstraint(_in("role", INVITATION_ROLES), name="role_vocab"),
        sa.CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        sa.Index(
            "uq_private_project_invitation_open_email",
            "project_id",
            "email_hash",
            unique=True,
            postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL AND email IS NOT NULL"),
            sqlite_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL AND email IS NOT NULL"),
        ),
        sa.Index("ix_private_project_invitation_project_id", "project_id"),
        sa.Index(
            "ix_private_project_invitation_invited_by_user_id_created_at", "invited_by_user_id", "created_at"
        ),
    )


# ======================================================================= private_project_activity
class PrivateProjectActivity(Base):
    """The project's own history, shown to its members. Never written to `event`."""

    __tablename__ = "private_project_activity"

    id: Mapped[_uuid.UUID] = _private_pk()
    project_id: Mapped[_uuid.UUID] = _project_fk()
    actor_user_id: Mapped[_uuid.UUID | None] = _fk_user(nullable=True)
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant(), nullable=False, default=dict, server_default="{}"
    )
    occurred_at: Mapped[dt.datetime] = _timestamp()

    __table_args__ = (
        sa.CheckConstraint(_in("action", ACTIVITY_ACTIONS), name="action_vocab"),
        sa.Index("ix_private_project_activity_project_id_occurred_at", "project_id", "occurred_at"),
    )


#: Every private table, children first (the order a by-hand delete would take).
PRIVATE_TABLES: Final = (
    "private_project_activity",
    "private_project_invitation",
    "private_project_note",
    "private_project_evidence",
    "private_project_item",
    "private_project_link",
    "private_project_member",
    "private_project",
)

# --------------------------------------------------------------------------------------- activity
_UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_USER_ID = re.compile(rf"usr_{_CROCKFORD_TAIL}")
_TEMPLATE_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
#: What `fields` may name: the project's own fields, and `checklist` for a custom item.
ACTIVITY_FIELD_NAMES: Final = frozenset({*PROJECT_FIELDS, "checklist"})
DetailShape = Callable[[object], bool]


def _one_of(values: Collection[str]) -> DetailShape:
    return lambda value: isinstance(value, str) and value in values


def _shaped(pattern: re.Pattern[str]) -> DetailShape:
    return lambda value: isinstance(value, str) and pattern.fullmatch(value) is not None


def _record_id(value: object) -> bool:
    return isinstance(value, str) and (
        _ANY_RECORD_ID.fullmatch(value) is not None or is_record_public_id("corridor", value)
    )


def _field_names(value: object) -> bool:
    return (
        isinstance(value, (list, tuple))
        and bool(value)
        and all(isinstance(v, str) and v in ACTIVITY_FIELD_NAMES for v in value)
    )


#: The only keys an activity's `detail` may carry, each with its own shape: a vocabulary key takes
#: only its vocabulary, an id key only its id's shape, `fields` only project field names, and a
#: record reference only an id of the record type given beside it (`_TYPED_REFERENCES`). A title, a
#: note or an address fits none of them. The loosest are `template_key` (one lower-case snake_case
#: token) and a corridor reference (one id-like token, until the finder settles its ids).
ACTIVITY_DETAIL_SHAPES: Final[Mapping[str, DetailShape]] = {
    "fields": _field_names,
    "link_id": _shaped(_UUID_TEXT),
    "item_id": _shaped(_UUID_TEXT),
    "evidence_id": _shaped(_UUID_TEXT),
    "note_id": lambda value: is_private_public_id(NOTE_ID_PREFIX, value),
    "invitation_id": lambda value: is_private_public_id(INVITATION_ID_PREFIX, value),
    "user_id": _shaped(_USER_ID),
    "from_user_id": _shaped(_USER_ID),
    "to_user_id": _shaped(_USER_ID),
    "kind": _one_of(ITEM_KINDS),
    "record_type": _one_of(LINK_RECORD_TYPES),
    "origin_type": _one_of(LINK_RECORD_TYPES),
    "record_public_id": _record_id,
    "origin_public_id": _record_id,
    "relation": _one_of(LINK_RELATIONS),
    "template_key": _shaped(_TEMPLATE_KEY),
    "status": _one_of(ITEM_STATUSES),
    "role": _one_of(MEMBER_ROLES),
    "from_role": _one_of(MEMBER_ROLES),
    "to_role": _one_of(MEMBER_ROLES),
    "rotated": lambda value: isinstance(value, bool),
}
ACTIVITY_DETAIL_KEYS: Final = frozenset(ACTIVITY_DETAIL_SHAPES)
#: A reference is checked against its own record type, which must be given with it.
_TYPED_REFERENCES: Final = (("record_type", "record_public_id"), ("origin_type", "origin_public_id"))


def _activity_value(key: str, value: object) -> object:
    if not ACTIVITY_DETAIL_SHAPES[key](value):
        raise ValueError(f"activity detail {key!r} does not have the shape of a {key}")
    return list(value) if isinstance(value, tuple) else value


def add_activity(
    db: Session,
    *,
    project_id: _uuid.UUID,
    actor_user_id: _uuid.UUID | None,
    action: str,
    detail: Mapping[str, object] | None = None,
    now: dt.datetime | None = None,
) -> PrivateProjectActivity:
    """Append one row to the project's own history (docs/19 §4.1). Refuses a key outside
    `ACTIVITY_DETAIL_KEYS` and any value without its key's shape (`ACTIVITY_DETAIL_SHAPES`), so a
    value typed by a user can never be written here."""
    if action not in ACTIVITY_ACTIONS:
        raise ValueError(f"unknown activity action {action!r}")
    clean: dict[str, object] = {}
    for key, value in (detail or {}).items():
        if key not in ACTIVITY_DETAIL_KEYS:
            raise ValueError(f"activity detail key {key!r} is not allowed")
        clean[key] = _activity_value(key, value)
    for type_key, id_key in _TYPED_REFERENCES:
        if id_key in clean and not is_record_public_id(clean.get(type_key), clean[id_key]):
            raise ValueError(f"activity detail {id_key!r} is not an id of the {type_key!r} beside it")
    row = PrivateProjectActivity(
        project_id=project_id,
        actor_user_id=actor_user_id,
        action=action,
        detail=clean,
        occurred_at=now or utcnow(),
    )
    db.add(row)
    return row
