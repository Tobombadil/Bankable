"""Data rooms: documents in folders on a tracked item (docs/19 §3; next-wave item C of the decision
row of 2026-10-23; "Data rooms, as built" in docs/19).

Every function takes the `ProjectAccess` that `authz.authorize` returned and re-checks its own action
(`authz.require_action`), as the other private writes do:

- every member may see the folders and files and download (`download`);
- an owner or editor may upload, add a folder and rename one they can see (`upload`), and delete a
  file they uploaded themselves;
- the owner alone may delete anyone's file or a whole folder (`manage_files`) and read the access log
  (`audit`).

A folder, a file or a log line of another item is the same `404` an unknown id gets.

**The folders.** The checklist's six (Contracts, Finance, Security, Permits, Feasibility, Legal &
Corporate) and Other, created the first time anyone opens the item's data room; members may add
more. A default folder is never deleted, so the checklist's folders stay for the gap analysis
(next-wave item D) to read.

**An upload** is checked (`data_room_files.inspect_upload`: the type from the bytes, the size cap),
counted against the uploader's own allowance (`Limits.quota_bytes`, every file they uploaded that is
still stored, in any item), scanned when a scanner is configured (`data_room_scan`), sealed
(`data_room_store.Sealer`) and written under a random key before its row is added. A refused upload
writes nothing but its line in the access log.

**The access log** (`private_project_document_event`) records who viewed a folder's file list
(once per member and folder in `VIEW_COALESCE`), downloaded, uploaded or deleted which file, refused
uploads and folder changes, with when. Its owner reads it; nobody else does, and nothing from it is
written to `event`, the admin audit log, analytics or an application log.

**Deleting** a file, a folder, an item (`forget_project`, before `authz.delete_project`) or an
account (`erase_user_data_rooms`) deletes the rows in the caller's transaction and removes the
objects once it commits (`data_room_store.delete_after_commit`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import unicodedata
import uuid as _uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import ColumnElement, and_, delete, exists, func, not_, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.api.errors import ProblemError
from services.db.models import User
from services.projects import data_room_files
from services.projects.authz import ProjectAccess, RoleForbidden, project_not_found, require_action
from services.projects.data_room_scan import Scanner
from services.projects.data_room_store import (
    Limits,
    ObjectStore,
    Sealer,
    delete_after_commit,
    discard_on_rollback,
    new_object_key,
)
from services.projects.models import (
    CHECKLIST_FOLDERS,
    DATA_ROOM_FOLDER_KEYS,
    DATA_ROOM_FOLDER_LABELS,
    FOLDER_NAME_MAX_CHARS,
    MAX_DOCUMENTS_PER_PROJECT,
    MAX_FOLDERS_PER_PROJECT,
    PrivateProjectDocument,
    PrivateProjectDocumentEvent,
    PrivateProjectFolder,
    PrivateProjectItem,
    PrivateProjectMember,
    folder_name_key,
    has_control_characters,
    utcnow,
)

#: A member reopening the same folder within this window adds no second "viewed" line.
VIEW_COALESCE: Final = dt.timedelta(minutes=10)
ACCESS_LOG_DEFAULT_LIMIT: Final = 100
ACCESS_LOG_MAX_LIMIT: Final = 500
_CUSTOM_SORT_START: Final = 1000


def _invalid(field: str, message: str) -> ProblemError:
    return ProblemError(
        "validation_error", "Invalid request", detail=message, errors=[{"field": field, "message": message}]
    )


# ------------------------------------------------------------------------------------ folders
@dataclass(frozen=True)
class FolderSummary:
    folder: PrivateProjectFolder
    documents: int
    bytes: int
    #: The checklist's "N of M done" for a default checklist folder; `None` for Other and custom ones.
    checklist_done: int | None
    checklist_total: int | None

    @property
    def is_default(self) -> bool:
        return self.folder.key is not None


def ensure_default_folders(db: Session, access: ProjectAccess) -> None:
    """The seven default folders, created the first time the item's data room is opened. Two members
    opening it at once share one set: the second insert hits the unique key and reads what the first
    wrote. An archived item is read-only (§12.2 of docs/19: "nothing in it changes"), so opening it
    writes nothing: it shows the folders it already has."""
    require_action(access, "download")
    if access.project.archived_at is not None:
        return
    have = set(
        db.execute(
            select(PrivateProjectFolder.key).where(
                PrivateProjectFolder.project_id == access.project.id, PrivateProjectFolder.key.is_not(None)
            )
        ).scalars()
    )
    missing = [key for key in DATA_ROOM_FOLDER_KEYS if key not in have]
    if not missing:
        return
    try:
        with db.begin_nested():
            for key in missing:
                db.add(
                    PrivateProjectFolder(
                        project_id=access.project.id,
                        key=key,
                        name=DATA_ROOM_FOLDER_LABELS[key],
                        name_key=folder_name_key(DATA_ROOM_FOLDER_LABELS[key]),
                        sort_order=DATA_ROOM_FOLDER_KEYS.index(key) * 10,
                    )
                )
            db.flush()
    except IntegrityError:
        pass  # another request created them first


def _folders(db: Session, access: ProjectAccess) -> list[PrivateProjectFolder]:
    return list(
        db.execute(
            select(PrivateProjectFolder)
            .where(PrivateProjectFolder.project_id == access.project.id)
            .order_by(
                PrivateProjectFolder.sort_order, PrivateProjectFolder.created_at, PrivateProjectFolder.id
            )
        ).scalars()
    )


def folder_summaries(db: Session, access: ProjectAccess) -> list[FolderSummary]:
    """Every folder with its file count and size, and, for a checklist folder, the item's checklist
    counts there (done over the items not marked not applicable, as the checklist tab counts them)."""
    require_action(access, "download")
    ensure_default_folders(db, access)
    folders = _folders(db, access)
    counts = {
        folder_id: (int(n), int(size or 0))
        for folder_id, n, size in db.execute(
            select(
                PrivateProjectDocument.folder_id,
                func.count(),
                func.sum(PrivateProjectDocument.size_bytes),
            )
            .where(PrivateProjectDocument.project_id == access.project.id)
            .group_by(PrivateProjectDocument.folder_id)
        ).all()
    }
    checklist: dict[str, list[int]] = {}
    for folder_key, status, n in db.execute(
        select(PrivateProjectItem.folder, PrivateProjectItem.status, func.count())
        .where(PrivateProjectItem.project_id == access.project.id)
        .group_by(PrivateProjectItem.folder, PrivateProjectItem.status)
    ).all():
        tally = checklist.setdefault(folder_key, [0, 0])
        if status == "done":
            tally[0] += int(n)
        if status != "not_applicable":
            tally[1] += int(n)
    rows = []
    for folder in folders:
        n, size = counts.get(folder.id, (0, 0))
        done_total: list[int] | None = None
        if folder.key in CHECKLIST_FOLDERS:
            done_total = checklist.get(folder.key, [0, 0])
        rows.append(
            FolderSummary(
                folder=folder,
                documents=n,
                bytes=size,
                checklist_done=done_total[0] if done_total is not None else None,
                checklist_total=done_total[1] if done_total is not None else None,
            )
        )
    return rows


def _folder_id(value: object) -> _uuid.UUID:
    if isinstance(value, _uuid.UUID):
        return value
    try:
        return _uuid.UUID(str(value))
    except ValueError:
        raise project_not_found() from None


def folder_or_404(db: Session, access: ProjectAccess, folder_id: object) -> PrivateProjectFolder:
    """One of this item's folders, or the same `404` as anything not the caller's."""
    require_action(access, "download")
    folder = db.execute(
        select(PrivateProjectFolder).where(
            PrivateProjectFolder.id == _folder_id(folder_id),
            PrivateProjectFolder.project_id == access.project.id,
        )
    ).scalar_one_or_none()
    if folder is None:
        raise project_not_found()
    return folder


def clean_folder_name(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid("name", "name is required")
    if has_control_characters(value):
        raise _invalid("name", "name has characters it cannot keep")
    name = " ".join(unicodedata.normalize("NFC", value).split())
    if not 1 <= len(name) <= FOLDER_NAME_MAX_CHARS:
        raise _invalid("name", f"name is 1 to {FOLDER_NAME_MAX_CHARS} characters")
    return name


def _name_taken(db: Session, access: ProjectAccess, name: str, *, besides: _uuid.UUID | None = None) -> bool:
    """A quick answer for the common case; the unique `(project_id, name_key)` decides a race
    (`_flush_folder`)."""
    taken = db.scalar(
        select(PrivateProjectFolder.id).where(
            PrivateProjectFolder.project_id == access.project.id,
            PrivateProjectFolder.name_key == folder_name_key(name),
        )
    )
    return taken is not None and taken != besides


def _write_folder(db: Session, change: Callable[[], None]) -> None:
    """Makes a folder change and writes it inside a savepoint, so a name another member took in the
    meantime (the unique `(project_id, name_key)`) is the same `400` as one taken before, and the
    request's transaction stays usable. The change is made inside the savepoint: `begin_nested`
    flushes what is pending before it begins, outside the savepoint."""
    try:
        with db.begin_nested():
            change()
            db.flush()
    except IntegrityError:
        raise _invalid("name", "a folder of that name is already here") from None


def _log(
    db: Session,
    access: ProjectAccess,
    action: str,
    *,
    now: dt.datetime,
    document: PrivateProjectDocument | None = None,
    folder: PrivateProjectFolder | None = None,
    file_name: str | None = None,
    reason: str | None = None,
    size_bytes: int | None = None,
) -> None:
    db.add(
        PrivateProjectDocumentEvent(
            project_id=access.project.id,
            document_id=document.id if document is not None else None,
            folder_id=folder.id
            if folder is not None
            else (document.folder_id if document is not None else None),
            file_name=file_name
            if file_name is not None
            else (document.file_name if document is not None else None),
            folder_name=folder.name if folder is not None else None,
            actor_user_id=access.user.id,
            action=action,
            reason=reason,
            size_bytes=size_bytes if size_bytes is not None else (document.size_bytes if document else None),
            occurred_at=now,
        )
    )


def create_folder(
    db: Session, access: ProjectAccess, name: object, *, now: dt.datetime | None = None
) -> PrivateProjectFolder:
    require_action(access, "upload")
    clean = clean_folder_name(name)
    ensure_default_folders(db, access)
    count = db.scalar(
        select(func.count())
        .select_from(PrivateProjectFolder)
        .where(PrivateProjectFolder.project_id == access.project.id)
    )
    if int(count or 0) >= MAX_FOLDERS_PER_PROJECT:
        raise ProblemError(
            "conflict", "Too many folders", detail=f"An item holds at most {MAX_FOLDERS_PER_PROJECT} folders."
        )
    if _name_taken(db, access, clean):
        raise _invalid("name", "a folder of that name is already here")
    highest = db.scalar(
        select(func.max(PrivateProjectFolder.sort_order)).where(
            PrivateProjectFolder.project_id == access.project.id
        )
    )
    folder = PrivateProjectFolder(
        project_id=access.project.id,
        key=None,
        name=clean,
        name_key=folder_name_key(clean),
        sort_order=max(int(highest or 0) + 1, _CUSTOM_SORT_START),
        created_by_user_id=access.user.id,
        created_at=now or utcnow(),
    )
    _write_folder(db, lambda: db.add(folder))
    _log(db, access, "folder_created", now=now or utcnow(), folder=folder)
    return folder


def rename_folder(
    db: Session, access: ProjectAccess, folder_id: object, name: object, *, now: dt.datetime | None = None
) -> PrivateProjectFolder:
    """A folder a member added; the default folders keep the checklist's names."""
    require_action(access, "upload")
    folder = folder_or_404(db, access, folder_id)
    if folder.key is not None:
        raise _invalid("name", "the default folders keep their names")
    clean = clean_folder_name(name)
    if clean != folder.name:
        if _name_taken(db, access, clean, besides=folder.id):
            raise _invalid("name", "a folder of that name is already here")

        def change() -> None:
            folder.name = clean
            folder.name_key = folder_name_key(clean)

        _write_folder(db, change)
        _log(db, access, "folder_renamed", now=now or utcnow(), folder=folder)
    return folder


def _documents(
    db: Session, access: ProjectAccess, *where: ColumnElement[bool]
) -> list[PrivateProjectDocument]:
    return list(
        db.execute(
            select(PrivateProjectDocument)
            .where(PrivateProjectDocument.project_id == access.project.id, *where)
            .order_by(PrivateProjectDocument.uploaded_at.desc(), PrivateProjectDocument.id)
        ).scalars()
    )


def delete_folder(
    db: Session,
    access: ProjectAccess,
    folder_id: object,
    store: ObjectStore | None,
    *,
    now: dt.datetime | None = None,
) -> int:
    """The owner deletes a folder they or a member added, with every file in it; returns the number
    of files. Default folders stay."""
    require_action(access, "manage_files")
    folder = folder_or_404(db, access, folder_id)
    if folder.key is not None:
        raise _invalid("folder", "the default folders cannot be deleted; delete the files in them instead")
    when = now or utcnow()
    documents = _documents(db, access, PrivateProjectDocument.folder_id == folder.id)
    for document in documents:
        _log(db, access, "deleted", now=when, document=document)
    _log(db, access, "folder_deleted", now=when, folder=folder)
    keys = [document.object_key for document in documents]
    db.execute(delete(PrivateProjectDocument).where(PrivateProjectDocument.folder_id == folder.id))
    db.execute(delete(PrivateProjectFolder).where(PrivateProjectFolder.id == folder.id))
    if store is not None:
        delete_after_commit(db, store, keys)
    db.flush()
    return len(documents)


def folder_documents(
    db: Session, access: ProjectAccess, folder_id: object, *, now: dt.datetime | None = None
) -> tuple[PrivateProjectFolder, list[PrivateProjectDocument]]:
    """A folder's files, newest first. Opening the list is a view: logged once per member and folder
    in `VIEW_COALESCE`."""
    require_action(access, "download")
    folder = folder_or_404(db, access, folder_id)
    when = now or utcnow()
    recent = db.scalar(
        select(func.count())
        .select_from(PrivateProjectDocumentEvent)
        .where(
            PrivateProjectDocumentEvent.project_id == access.project.id,
            PrivateProjectDocumentEvent.folder_id == folder.id,
            PrivateProjectDocumentEvent.actor_user_id == access.user.id,
            PrivateProjectDocumentEvent.action == "viewed",
            PrivateProjectDocumentEvent.occurred_at >= when - VIEW_COALESCE,
        )
    )
    if not recent:
        _log(db, access, "viewed", now=when, folder=folder)
    return folder, _documents(db, access, PrivateProjectDocument.folder_id == folder.id)


# ------------------------------------------------------------------------------------ uploads
def quota_used(db: Session, user_id: _uuid.UUID) -> int:
    """Bytes the user's allowance carries, across every item: the stored files they uploaded to items
    they are still a member of, and, in the items they own, the files whose uploader no longer is
    (removed, left, or erased and shown as "Former member"). A departed collaborator's files stay as
    the item's content and count against its owner, who can delete them, rather than against
    someone who can no longer reach them (review of 2026-10-23)."""
    document, member = PrivateProjectDocument, PrivateProjectMember
    uploader_is_member = exists().where(
        member.project_id == document.project_id, member.user_id == document.uploaded_by_user_id
    )
    owned = select(member.project_id).where(member.user_id == user_id, member.role == "owner")
    total = db.scalar(
        select(func.sum(document.size_bytes)).where(
            or_(
                and_(document.uploaded_by_user_id == user_id, uploader_is_member),
                and_(document.project_id.in_(owned), not_(uploader_is_member)),
            )
        )
    )
    return int(total or 0)


_ALLOWANCE_LOCK_LABEL: Final = b"inframation:data-room:allowance:"


def lock_allowance(db: Session, user_id: _uuid.UUID) -> bool:
    """Holds the user's allowance until `db` commits or rolls back, so two uploads by one user in
    two API processes cannot both pass the check and together overshoot it: PostgreSQL's
    transaction-level advisory lock on a key derived from the user's id. SQLite (the tests and
    development) runs one writer at a time, so there is nothing to take. Returns whether a lock was
    taken."""
    if db.get_bind().dialect.name != "postgresql":
        return False
    digest = hashlib.sha256(_ALLOWANCE_LOCK_LABEL + user_id.bytes).digest()
    db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(digest[:8], "big", signed=True)}
    )
    return True


def _refuse(
    db: Session,
    access: ProjectAccess,
    folder: PrivateProjectFolder,
    raw_name: str,
    reason: str,
    size: int,
    now: dt.datetime,
) -> data_room_files.Refusal:
    _log(
        db,
        access,
        "upload_refused",
        now=now,
        folder=folder,
        file_name=data_room_files.display_name(raw_name),
        reason=reason,
        size_bytes=size or None,
    )
    db.flush()
    return data_room_files.Refusal(reason)


def store_upload(
    db: Session,
    access: ProjectAccess,
    *,
    folder_id: object,
    raw_name: str,
    data: bytes,
    store: ObjectStore,
    sealer: Sealer,
    scanner: Scanner,
    limits: Limits,
    now: dt.datetime | None = None,
) -> PrivateProjectDocument | data_room_files.Refusal:
    """Check, count, scan, seal, store (module docstring). A `Refusal` has written its log line and
    nothing else; the caller commits it. `ScannerUnavailable` and `DataRoomUnavailable` propagate
    with nothing written, so the caller answers `503`."""
    require_action(access, "upload")
    when = now or utcnow()
    folder = folder_or_404(db, access, folder_id)
    stored = db.scalar(
        select(func.count())
        .select_from(PrivateProjectDocument)
        .where(PrivateProjectDocument.project_id == access.project.id)
    )
    if int(stored or 0) >= MAX_DOCUMENTS_PER_PROJECT:
        raise ProblemError(
            "conflict", "Too many files", detail=f"An item holds at most {MAX_DOCUMENTS_PER_PROJECT} files."
        )
    verdict = data_room_files.inspect_upload(data, raw_name, max_bytes=limits.max_file_bytes)
    if isinstance(verdict, data_room_files.Refusal):
        return _refuse(db, access, folder, raw_name, verdict.reason, len(data), when)
    lock_allowance(db, access.user.id)
    if quota_used(db, access.user.id) + len(data) > limits.quota_bytes:
        return _refuse(db, access, folder, raw_name, "quota", len(data), when)
    scan = scanner.scan(data)
    if scan.verdict == "infected":
        return _refuse(db, access, folder, raw_name, "malware", len(data), when)
    object_key = new_object_key()
    sealed, wrapped = sealer.seal(object_key, data)
    store.put(object_key, sealed)
    discard_on_rollback(db, store, object_key)
    document = PrivateProjectDocument(
        project_id=access.project.id,
        folder_id=folder.id,
        file_name=verdict.file_name,
        doc_type=verdict.doc_type.key,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        object_key=object_key,
        key_fingerprint=sealer.fingerprint,
        wrapped_key=wrapped,
        scan_status="clean" if scan.verdict == "clean" else "not_scanned",
        scanner=scan.scanner if scan.verdict == "clean" else None,
        scanned_at=when if scan.verdict == "clean" else None,
        uploaded_by_user_id=access.user.id,
        uploaded_at=when,
    )
    db.add(document)
    db.flush()
    _log(db, access, "uploaded", now=when, document=document)
    db.flush()
    return document


# ---------------------------------------------------------------------------------- downloads
def document_or_404(db: Session, access: ProjectAccess, document_id: object) -> PrivateProjectDocument:
    require_action(access, "download")
    document = db.execute(
        select(PrivateProjectDocument).where(
            PrivateProjectDocument.id == _folder_id(document_id),
            PrivateProjectDocument.project_id == access.project.id,
        )
    ).scalar_one_or_none()
    if document is None:
        raise project_not_found()
    return document


class DocumentDamaged(RuntimeError):
    """The stored object is missing, or no longer the bytes that were uploaded."""


def open_document(
    db: Session,
    access: ProjectAccess,
    document_id: object,
    *,
    store: ObjectStore,
    sealer: Sealer,
    now: dt.datetime | None = None,
) -> tuple[PrivateProjectDocument, bytes]:
    """The file and its bytes, checked against the hash taken at upload; the download is logged."""
    document = document_or_404(db, access, document_id)
    try:
        sealed = store.get(document.object_key)
    except FileNotFoundError:
        raise DocumentDamaged("the stored object is missing") from None
    data = sealer.open(document.object_key, sealed, document.wrapped_key, document.key_fingerprint)
    if hashlib.sha256(data).hexdigest() != document.sha256:
        raise DocumentDamaged("the stored object does not match its upload")
    _log(db, access, "downloaded", now=now or utcnow(), document=document)
    db.flush()
    return document, data


def may_delete(access: ProjectAccess, document: PrivateProjectDocument) -> bool:
    """The owner deletes any file; an editor the files they uploaded; a viewer none."""
    if access.project.archived_at is not None:
        return False
    if access.allows("manage_files"):
        return True
    return access.allows("upload") and document.uploaded_by_user_id == access.user.id


def delete_document(
    db: Session,
    access: ProjectAccess,
    document_id: object,
    store: ObjectStore | None,
    *,
    now: dt.datetime | None = None,
) -> None:
    require_action(access, "upload")
    document = document_or_404(db, access, document_id)
    if not may_delete(access, document):
        raise RoleForbidden(action="manage_files", role=access.role)
    _log(db, access, "deleted", now=now or utcnow(), document=document)
    key = document.object_key
    db.execute(delete(PrivateProjectDocument).where(PrivateProjectDocument.id == document.id))
    if store is not None:
        delete_after_commit(db, store, [key])
    db.flush()


# --------------------------------------------------------------------------------- access log
def access_log(
    db: Session, access: ProjectAccess, *, limit: int = ACCESS_LOG_DEFAULT_LIMIT
) -> list[PrivateProjectDocumentEvent]:
    """Newest first. The owner's alone (`audit`)."""
    require_action(access, "audit")
    return list(
        db.execute(
            select(PrivateProjectDocumentEvent)
            .where(PrivateProjectDocumentEvent.project_id == access.project.id)
            .order_by(PrivateProjectDocumentEvent.occurred_at.desc(), PrivateProjectDocumentEvent.id)
            .limit(max(1, min(limit, ACCESS_LOG_MAX_LIMIT)))
        ).scalars()
    )


# ------------------------------------------------------------------------- items and accounts
def project_object_keys(db: Session, project_ids: Iterable[_uuid.UUID]) -> list[str]:
    ids = list(project_ids)
    if not ids:
        return []
    return list(
        db.execute(
            select(PrivateProjectDocument.object_key).where(PrivateProjectDocument.project_id.in_(ids))
        ).scalars()
    )


def forget_project(db: Session, access: ProjectAccess, store: ObjectStore | None) -> int:
    """Before the owner deletes an item: its files' objects go once the deletion commits (the rows
    go by the item's cascade). Returns the number of files."""
    require_action(access, "delete")
    keys = project_object_keys(db, [access.project.id])
    if store is not None:
        delete_after_commit(db, store, keys)
    return len(keys)


@dataclass(frozen=True)
class DataRoomErasure:
    """What `erase_user_data_rooms` did, as counts."""

    owned_items: int
    documents_deleted: int
    folders_deleted: int
    log_lines_deleted: int
    documents_kept_as_former_member: int
    log_lines_anonymised: int


def erase_user_data_rooms(db: Session, user: User, store: ObjectStore | None) -> DataRoomErasure:
    """docs/19 §5.4 for the data room. In the items the user owns, every file, folder and log line is
    deleted, the objects once the erasure commits. In other people's items, the files the user
    uploaded stay as that item's content, shown as uploaded by "Former member", and the log lines
    they caused lose their author (the rule the owner set for notes, 2026-10-22)."""
    owned = list(
        db.execute(
            select(PrivateProjectMember.project_id).where(
                PrivateProjectMember.user_id == user.id, PrivateProjectMember.role == "owner"
            )
        ).scalars()
    )
    keys = project_object_keys(db, owned)
    documents_deleted = folders_deleted = log_deleted = 0
    if owned:
        log_deleted = _count_delete(db, PrivateProjectDocumentEvent, owned)
        documents_deleted = _count_delete(db, PrivateProjectDocument, owned)
        folders_deleted = _count_delete(db, PrivateProjectFolder, owned)
    kept = _rowcount(
        db.execute(
            update(PrivateProjectDocument)
            .where(PrivateProjectDocument.uploaded_by_user_id == user.id)
            .values(uploaded_by_user_id=None)
        )
    )
    anonymised = _rowcount(
        db.execute(
            update(PrivateProjectDocumentEvent)
            .where(PrivateProjectDocumentEvent.actor_user_id == user.id)
            .values(actor_user_id=None)
        )
    )
    db.execute(
        update(PrivateProjectFolder)
        .where(PrivateProjectFolder.created_by_user_id == user.id)
        .values(created_by_user_id=None)
    )
    if store is not None:
        delete_after_commit(db, store, keys)
    db.flush()
    return DataRoomErasure(
        owned_items=len(owned),
        documents_deleted=documents_deleted,
        folders_deleted=folders_deleted,
        log_lines_deleted=log_deleted,
        documents_kept_as_former_member=kept,
        log_lines_anonymised=anonymised,
    )


def _count_delete(
    db: Session,
    model: type[PrivateProjectDocumentEvent] | type[PrivateProjectDocument] | type[PrivateProjectFolder],
    project_ids: Sequence[_uuid.UUID],
) -> int:
    return _rowcount(db.execute(delete(model).where(model.project_id.in_(list(project_ids)))))


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def stored_object_keys(db: Session) -> set[str]:
    """Every object key a document row names (the retention job's orphan sweep)."""
    return set(db.execute(select(PrivateProjectDocument.object_key)).scalars())
