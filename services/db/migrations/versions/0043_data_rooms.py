"""Data rooms: folders, documents and the access log on a tracked item (docs/19 §3 and "Data rooms, as
built"; next-wave item C of the decision row of 2026-10-23).

Three tables, all reachable only through `services.projects.authz.authorize`:

- `private_project_folder`: an item's folders. `key` names one of the seven default folders (the
  checklist's six and `other`) and is NULL for a folder a member added; one of each default per item.
  `name_key` is the name case-folded, unique per item, so two members naming folders at once
  cannot make two of one name.
- `private_project_document`: what a stored file is (name, type, size, SHA-256, uploader, scan
  status) and where its sealed bytes are: a random object key under `data-rooms/` in the private
  bucket, and the file's own key sealed under the deployment's `DATA_ROOM_KEY`. Never the bytes.
- `private_project_document_event`: the item's access log (viewed, downloaded, uploaded, deleted,
  refused uploads, folder changes), read by the item's owner. No foreign key to the document or the
  folder, which may be gone; the file's name as it was is kept on the line.

Every row cascades from `private_project`, so deleting an item deletes its data room's rows (the
objects are removed by the application once the deletion commits, and swept by the retention job if
not). Users are referenced without cascades, as in 0040: erasure clears or deletes those rows in
code.

Guards follow 0040/0042: a SQLite store built by `create_all` from today's models already has the
tables and is stamped, so each create checks what is present.

**Downgrade** drops the three tables. It refuses while any document row exists, naming the count:
dropping them would strand the objects in the bucket with nothing to find or delete them by, and a
schema rollback must not delete a user's files (0042's precedent). Before launch (`DATA_ROOMS` off)
there are none.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported from `services.projects.models` (0020-0042's convention);
#: `services/projects/test_migration.py` pins them equal.
DATA_ROOM_FOLDER_KEYS = (
    "contracts",
    "finance",
    "security",
    "permits",
    "feasibility",
    "legal_corporate",
    "other",
)
DOCUMENT_TYPES = ("pdf", "docx", "xlsx", "pptx", "png", "jpeg", "tiff", "csv", "txt")
SCAN_STATUSES = ("not_scanned", "clean")
SCANNERS = ("clamd",)
DOCUMENT_EVENT_ACTIONS = (
    "viewed",
    "downloaded",
    "uploaded",
    "deleted",
    "upload_refused",
    "folder_created",
    "folder_renamed",
    "folder_deleted",
)
REFUSAL_REASONS = (
    "empty",
    "too_large",
    "type_not_accepted",
    "executable",
    "script",
    "archive",
    "macros",
    "external_content",
    "legacy_office",
    "encrypted",
    "malware",
    "quota",
)
FOLDER_NAME_MAX_CHARS = 80
FILE_NAME_MAX_CHARS = 200

#: Children first: the order `downgrade` drops them in.
TABLES = ("private_project_document_event", "private_project_document", "private_project_folder")


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


def _project_id(table: str) -> sa.Column:
    return sa.Column(
        "project_id",
        GUID(),
        sa.ForeignKey(
            "private_project.id", ondelete="CASCADE", name=f"fk_{table}_project_id_private_project"
        ),
        nullable=False,
    )


def _user(table: str, column: str) -> sa.Column:
    return sa.Column(
        column, GUID(), sa.ForeignKey("user.id", name=f"fk_{table}_{column}_user"), nullable=True
    )


def _create_folder() -> None:
    t = "private_project_folder"
    op.create_table(
        t,
        sa.Column("id", GUID(), nullable=False),
        _project_id(t),
        sa.Column("key", sa.Text, nullable=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("name_key", sa.Text, nullable=False),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        _user(t, "created_by_user_id"),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("project_id", "key", name=f"uq_{t}_project_id_key"),
        sa.UniqueConstraint("project_id", "name_key", name=f"uq_{t}_project_id_name_key"),
        _check(t, "key_vocab", _in("key", DATA_ROOM_FOLDER_KEYS)),
        _check(t, "name_length", f"length(name) BETWEEN 1 AND {FOLDER_NAME_MAX_CHARS}"),
    )
    op.create_index(f"ix_{t}_project_id", t, ["project_id"])


def _create_document() -> None:
    t = "private_project_document"
    op.create_table(
        t,
        sa.Column("id", GUID(), nullable=False),
        _project_id(t),
        sa.Column(
            "folder_id",
            GUID(),
            sa.ForeignKey(
                "private_project_folder.id",
                ondelete="CASCADE",
                name=f"fk_{t}_folder_id_private_project_folder",
            ),
            nullable=False,
        ),
        sa.Column("file_name", sa.Text, nullable=False),
        sa.Column("doc_type", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.CHAR(64), nullable=False),
        sa.Column("object_key", sa.Text, nullable=False),
        sa.Column("key_fingerprint", sa.Text, nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary, nullable=False),
        sa.Column("scan_status", sa.Text, nullable=False),
        sa.Column("scanner", sa.Text, nullable=True),
        _now("scanned_at", nullable=True),
        _user(t, "uploaded_by_user_id"),
        _now("uploaded_at"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        sa.UniqueConstraint("object_key", name=f"uq_{t}_object_key"),
        _check(t, "doc_type_vocab", _in("doc_type", DOCUMENT_TYPES)),
        _check(t, "scan_status_vocab", _in("scan_status", SCAN_STATUSES)),
        _check(t, "scanner_vocab", _in("scanner", SCANNERS)),
        _check(t, "scanner_pair", "(scan_status = 'clean') = (scanner IS NOT NULL)"),
        _check(t, "size_positive", "size_bytes > 0"),
        _check(t, "file_name_length", f"length(file_name) BETWEEN 1 AND {FILE_NAME_MAX_CHARS}"),
    )
    op.create_index(f"ix_{t}_project_id_folder_id", t, ["project_id", "folder_id"])
    op.create_index(f"ix_{t}_uploaded_by_user_id", t, ["uploaded_by_user_id"])


def _create_event() -> None:
    t = "private_project_document_event"
    op.create_table(
        t,
        sa.Column("id", GUID(), nullable=False),
        _project_id(t),
        sa.Column("document_id", GUID(), nullable=True),
        sa.Column("folder_id", GUID(), nullable=True),
        sa.Column("file_name", sa.Text, nullable=True),
        sa.Column("folder_name", sa.Text, nullable=True),
        _user(t, "actor_user_id"),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("size_bytes", sa.BigInteger, nullable=True),
        _now("occurred_at"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{t}"),
        _check(t, "action_vocab", _in("action", DOCUMENT_EVENT_ACTIONS)),
        _check(t, "reason_vocab", _in("reason", REFUSAL_REASONS)),
        _check(t, "reason_refusals_only", "(reason IS NULL) = (action <> 'upload_refused')"),
    )
    op.create_index(f"ix_{t}_project_id_occurred_at", t, ["project_id", "occurred_at"])
    op.create_index(f"ix_{t}_actor_user_id", t, ["actor_user_id"])


def upgrade() -> None:
    if not _has_table("private_project_folder"):
        _create_folder()
    if not _has_table("private_project_document"):
        _create_document()
    if not _has_table("private_project_document_event"):
        _create_event()


def downgrade() -> None:
    if _has_table("private_project_document"):
        stored = op.get_bind().execute(sa.text("SELECT count(*) FROM private_project_document")).scalar()
        if stored:
            raise RuntimeError(
                f"0043 downgrade refused: {stored} stored documents; their objects would be stranded in "
                "the bucket. Delete the documents through the application first."
            )
    for table in TABLES:
        if _has_table(table):
            op.drop_table(table)
