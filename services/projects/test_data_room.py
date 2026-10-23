"""Data rooms (`services/projects/data_room.py`): folders, uploads, downloads, deletes, the access log,
the per-user allowance, scanning, and what deleting an item or erasing an account does to them. The
store is a directory under the test's own temporary path; the seals use the test key."""

from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
import uuid
from collections.abc import Callable

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from services.api.errors import ProblemError
from services.db.models import Event, User
from services.projects import authz, data_room, data_room_files
from services.projects import data_room_store as rooms
from services.projects.conftest import TEST_DATA_ROOM_KEY, UserFactory, session_ctx
from services.projects.data_room_scan import NoScanner, ScannerUnavailable, ScanResult
from services.projects.models import (
    DATA_ROOM_FOLDER_KEYS,
    DATA_ROOM_TABLES,
    MAX_FOLDERS_PER_PROJECT,
    PrivateProject,
    PrivateProjectDocument,
    PrivateProjectDocumentEvent,
    PrivateProjectFolder,
    PrivateProjectMember,
)

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.UTC)
PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
SENTINEL = "SENTINEL-room-7Q2"
MIB = 1024 * 1024
LIMITS = rooms.Limits(max_file_bytes=MIB, quota_bytes=2 * MIB)
SEALER = rooms.Sealer(rooms._decode_key(TEST_DATA_ROOM_KEY))


class Infected:
    name = "clamd"

    def scan(self, data: bytes) -> ScanResult:
        return ScanResult(verdict="infected", scanner="clamd")


class Clean:
    name = "clamd"

    def scan(self, data: bytes) -> ScanResult:
        return ScanResult(verdict="clean", scanner="clamd")


class Down:
    name = "clamd"

    def scan(self, data: bytes) -> ScanResult:
        raise ScannerUnavailable("clamd did not answer")


class Room:
    """One item with an owner, an editor and a viewer, and a store under `tmp_path`."""

    def __init__(self, db: Session, make_user: UserFactory, store: rooms.LocalObjectStore) -> None:
        self.db = db
        self.store = store
        self.owner, self.editor, self.viewer = make_user(), make_user(), make_user()
        created = authz.create_project(db, session_ctx(self.owner), fields={"title": f"Item {SENTINEL}"})
        self.project: PrivateProject = created.project
        for user, role in ((self.editor, "editor"), (self.viewer, "viewer")):
            db.add(PrivateProjectMember(project_id=self.project.id, user_id=user.id, role=role))
        db.flush()

    def access(self, user: User, action: authz.Action = "download") -> authz.ProjectAccess:
        return authz.authorize(self.db, session_ctx(user), self.project.public_id, action)

    def folder(self, key: str = "permits") -> PrivateProjectFolder:
        data_room.ensure_default_folders(self.db, self.access(self.owner))
        return self.db.execute(
            sa.select(PrivateProjectFolder).where(
                PrivateProjectFolder.project_id == self.project.id, PrivateProjectFolder.key == key
            )
        ).scalar_one()

    def upload(
        self,
        user: User | None = None,
        data: bytes = PDF,
        name: str = f"{SENTINEL} lease.pdf",
        *,
        folder: PrivateProjectFolder | None = None,
        scanner: object = None,
        limits: rooms.Limits = LIMITS,
    ) -> PrivateProjectDocument | data_room_files.Refusal:
        return data_room.store_upload(
            self.db,
            self.access(user or self.owner, "upload"),
            folder_id=(folder or self.folder()).id,
            raw_name=name,
            data=data,
            store=self.store,
            sealer=SEALER,
            scanner=scanner or NoScanner(),  # type: ignore[arg-type]
            limits=limits,
            now=NOW,
        )


@pytest.fixture()
def room(db: Session, make_user: UserFactory, tmp_path: pathlib.Path) -> Room:
    return Room(db, make_user, rooms.LocalObjectStore(tmp_path / "store"))


def _log(db: Session, room: Room) -> list[PrivateProjectDocumentEvent]:
    return list(
        db.execute(
            sa.select(PrivateProjectDocumentEvent)
            .where(PrivateProjectDocumentEvent.project_id == room.project.id)
            .order_by(PrivateProjectDocumentEvent.occurred_at, PrivateProjectDocumentEvent.id)
        ).scalars()
    )


def _objects(room: Room) -> list[str]:
    return [key for key, _ in room.store.list_objects()]


def _not_found(call: Callable[[], object]) -> None:
    with pytest.raises(ProblemError) as caught:
        call()
    assert (caught.value.status, caught.value.code) == (404, "not_found")


# ------------------------------------------------------------------------------------ folders
def test_the_seven_default_folders_are_made_once(db: Session, room: Room) -> None:
    access = room.access(room.viewer)
    summaries = data_room.folder_summaries(db, access)
    assert [s.folder.key for s in summaries] == list(DATA_ROOM_FOLDER_KEYS)
    assert [s.folder.name for s in summaries][-2:] == ["Legal & Corporate", "Other"]
    data_room.folder_summaries(db, access)
    count = db.scalar(sa.select(sa.func.count()).select_from(PrivateProjectFolder))
    assert count == len(DATA_ROOM_FOLDER_KEYS)
    # The checklist's counts sit beside its six folders; Other has none.
    by_key = {s.folder.key: s for s in summaries}
    assert by_key["contracts"].checklist_total == 5 and by_key["contracts"].checklist_done == 0
    assert by_key["other"].checklist_total is None


def test_folders_are_added_renamed_and_deleted_by_role(db: Session, room: Room) -> None:
    created = data_room.create_folder(db, room.access(room.editor, "upload"), "  Site  photos ")
    assert created.name == "Site photos" and created.key is None
    with pytest.raises(ProblemError, match="Invalid request"):
        data_room.create_folder(db, room.access(room.owner, "upload"), "site PHOTOS")
    with pytest.raises(ProblemError):
        data_room.create_folder(db, room.access(room.owner, "upload"), "x" * 81)
    with pytest.raises(ProblemError):
        data_room.create_folder(db, room.access(room.owner, "upload"), "bad\x00name")
    renamed = data_room.rename_folder(db, room.access(room.editor, "upload"), created.id, "Drone survey")
    assert renamed.name == "Drone survey"
    with pytest.raises(ProblemError, match="Invalid request"):
        data_room.rename_folder(db, room.access(room.owner, "upload"), room.folder().id, "Mine")
    with pytest.raises(authz.RoleForbidden):
        data_room.create_folder(db, room.access(room.viewer), "Viewer folder")
    with pytest.raises(authz.RoleForbidden):
        data_room.delete_folder(db, room.access(room.editor, "upload"), created.id, room.store)
    with pytest.raises(ProblemError):
        data_room.delete_folder(db, room.access(room.owner, "manage_files"), room.folder().id, room.store)
    room.upload(folder=created)
    assert len(_objects(room)) == 1
    files = data_room.delete_folder(db, room.access(room.owner, "manage_files"), created.id, room.store)
    db.commit()
    assert files == 1 and _objects(room) == []
    actions = [line.action for line in _log(db, room)]
    # The two lines share one moment, so their order is not fixed.
    assert set(actions[-2:]) == {"deleted", "folder_deleted"} and "folder_renamed" in actions


def test_a_folder_name_taken_in_a_race_is_refused_by_the_schema(
    db: Session, room: Room, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quick check misses a name another member added a moment ago (simulated here): the unique
    case-folded `name_key` refuses it with the same 400, and the transaction stays usable."""
    access = room.access(room.owner, "upload")
    data_room.create_folder(db, access, "Site photos")
    monkeypatch.setattr(data_room, "_name_taken", lambda *args, **kwargs: False)
    with pytest.raises(ProblemError, match="Invalid request") as caught:
        data_room.create_folder(db, access, "SITE PHOTOS")
    assert caught.value.status == 400
    other = data_room.create_folder(db, access, "Drone survey")
    with pytest.raises(ProblemError, match="Invalid request"):
        data_room.rename_folder(db, access, other.id, "site photos")
    db.commit()
    names = sorted(
        db.execute(sa.select(PrivateProjectFolder.name).where(PrivateProjectFolder.key.is_(None))).scalars()
    )
    assert names == ["Drone survey", "Site photos"]


def test_an_item_holds_at_most_thirty_folders(db: Session, room: Room) -> None:
    access = room.access(room.owner, "upload")
    for n in range(MAX_FOLDERS_PER_PROJECT - len(DATA_ROOM_FOLDER_KEYS)):
        data_room.create_folder(db, access, f"Folder {n}")
    with pytest.raises(ProblemError) as caught:
        data_room.create_folder(db, access, "One too many")
    assert caught.value.code == "conflict"


# ------------------------------------------------------------------------------------ uploads
def test_an_upload_is_sealed_stored_and_logged(db: Session, room: Room) -> None:
    document = room.upload(room.editor)
    assert isinstance(document, PrivateProjectDocument)
    assert document.file_name == f"{SENTINEL} lease.pdf" and document.doc_type == "pdf"
    assert document.size_bytes == len(PDF) and document.sha256 == hashlib.sha256(PDF).hexdigest()
    assert document.scan_status == "not_scanned" and document.scanner is None
    assert rooms.is_object_key(document.object_key) and SENTINEL not in document.object_key
    stored = room.store.get(document.object_key)
    assert PDF not in stored and SENTINEL.encode() not in stored
    assert [(line.action, line.file_name) for line in _log(db, room)] == [("uploaded", document.file_name)]
    db.commit()
    assert _objects(room) == [document.object_key]


def test_a_refused_upload_writes_only_its_log_line(db: Session, room: Room) -> None:
    refusal = room.upload(data=b"MZ" + b"\x00" * 64, name=f"{SENTINEL}.exe")
    assert refusal == data_room_files.Refusal("executable")
    assert db.scalar(sa.select(sa.func.count()).select_from(PrivateProjectDocument)) == 0
    assert _objects(room) == []
    (line,) = _log(db, room)
    assert (line.action, line.reason, line.file_name) == ("upload_refused", "executable", f"{SENTINEL}.exe")


def test_the_allowance_counts_each_uploaders_own_files(db: Session, room: Room) -> None:
    half = b"%PDF-1.7\n" + b"0" * (MIB - 100)
    assert isinstance(room.upload(room.owner, half), PrivateProjectDocument)
    assert isinstance(room.upload(room.owner, half), PrivateProjectDocument)
    assert room.upload(room.owner, half) == data_room_files.Refusal("quota")
    # The editor's own allowance is untouched by the owner's files.
    assert isinstance(room.upload(room.editor, half), PrivateProjectDocument)
    assert data_room.quota_used(db, room.owner.id) == 2 * len(half)
    assert room.upload(room.owner, b"%PDF-1.7\n" + b"0" * MIB) == data_room_files.Refusal("too_large")


def test_a_departed_collaborators_files_count_against_the_owner(db: Session, room: Room) -> None:
    """Review of 2026-10-23: a removed editor's files stayed on their allowance, though they could no
    longer reach the item to delete them. They now count against the item's owner, who can."""
    owners = room.upload(room.owner)
    editors = room.upload(room.editor)
    assert isinstance(owners, PrivateProjectDocument) and isinstance(editors, PrivateProjectDocument)
    assert data_room.quota_used(db, room.editor.id) == editors.size_bytes
    assert data_room.quota_used(db, room.owner.id) == owners.size_bytes
    db.execute(
        sa.delete(PrivateProjectMember).where(
            PrivateProjectMember.project_id == room.project.id, PrivateProjectMember.user_id == room.editor.id
        )
    )
    assert data_room.quota_used(db, room.editor.id) == 0
    assert data_room.quota_used(db, room.owner.id) == owners.size_bytes + editors.size_bytes
    # An erased uploader's files ("Former member") count against the owner too.
    db.execute(sa.update(PrivateProjectDocument).values(uploaded_by_user_id=None))
    assert data_room.quota_used(db, room.owner.id) == owners.size_bytes + editors.size_bytes


def test_the_allowance_is_locked_per_user_on_postgres() -> None:
    """Two uploads by one user in two API processes cannot both pass the allowance check: on
    PostgreSQL the check runs under a transaction-level advisory lock on the user (SQLite, here,
    runs one writer at a time)."""

    class Recorder:
        def __init__(self, dialect: str) -> None:
            self.dialect = dialect
            self.statements: list[tuple[str, dict[str, object]]] = []

        def get_bind(self) -> object:
            return type("Bind", (), {"dialect": type("Dialect", (), {"name": self.dialect})()})()

        def execute(self, statement: object, params: dict[str, object]) -> None:
            self.statements.append((str(statement), params))

    user_id, other_id = uuid.uuid4(), uuid.uuid4()
    postgres = Recorder("postgresql")
    assert data_room.lock_allowance(postgres, user_id)  # type: ignore[arg-type]
    assert data_room.lock_allowance(postgres, user_id)  # type: ignore[arg-type]
    data_room.lock_allowance(postgres, other_id)  # type: ignore[arg-type]
    (sql, first), (_, again), (_, other) = postgres.statements
    assert sql == "SELECT pg_advisory_xact_lock(:key)"
    assert first == again != other and isinstance(first["key"], int) and -(2**63) <= first["key"] < 2**63
    assert not data_room.lock_allowance(Recorder("sqlite"), user_id)  # type: ignore[arg-type]


def test_scanning(db: Session, room: Room) -> None:
    assert room.upload(scanner=Infected()) == data_room_files.Refusal("malware")
    assert _objects(room) == []
    clean = room.upload(scanner=Clean())
    assert isinstance(clean, PrivateProjectDocument)
    assert (clean.scan_status, clean.scanner, clean.scanned_at) == ("clean", "clamd", NOW)
    before = db.scalar(sa.select(sa.func.count()).select_from(PrivateProjectDocumentEvent))
    with pytest.raises(ScannerUnavailable):
        room.upload(scanner=Down())
    after = db.scalar(sa.select(sa.func.count()).select_from(PrivateProjectDocumentEvent))
    assert before == after and len(_objects(room)) == 1


def test_a_viewer_cannot_upload(room: Room) -> None:
    with pytest.raises(authz.RoleForbidden):
        data_room.store_upload(
            room.db,
            room.access(room.viewer),
            folder_id=room.folder().id,
            raw_name="x.pdf",
            data=PDF,
            store=room.store,
            sealer=SEALER,
            scanner=NoScanner(),
            limits=LIMITS,
        )


# ---------------------------------------------------------------------------------- downloads
def test_a_download_returns_the_bytes_and_is_logged(db: Session, room: Room) -> None:
    document = room.upload()
    assert isinstance(document, PrivateProjectDocument)
    got, data = data_room.open_document(
        db, room.access(room.viewer), document.id, store=room.store, sealer=SEALER
    )
    assert got.id == document.id and data == PDF
    assert [line.action for line in _log(db, room)] == ["uploaded", "downloaded"]


def test_a_damaged_or_missing_object_never_downloads(db: Session, room: Room) -> None:
    document = room.upload()
    assert isinstance(document, PrivateProjectDocument)
    sealed = bytearray(room.store.get(document.object_key))
    sealed[-1] ^= 1
    room.store.put(document.object_key, bytes(sealed))
    with pytest.raises(rooms.DataRoomUnavailable):
        data_room.open_document(db, room.access(room.owner), document.id, store=room.store, sealer=SEALER)
    room.store.delete(document.object_key)
    with pytest.raises(data_room.DocumentDamaged):
        data_room.open_document(db, room.access(room.owner), document.id, store=room.store, sealer=SEALER)


def test_ids_from_another_item_are_the_same_404(db: Session, room: Room, make_user: UserFactory) -> None:
    document = room.upload()
    assert isinstance(document, PrivateProjectDocument)
    stranger = make_user()
    theirs = authz.create_project(db, session_ctx(stranger), fields={"title": "Theirs"})
    data_room.ensure_default_folders(db, theirs)
    _not_found(lambda: data_room.document_or_404(db, theirs, document.id))
    _not_found(lambda: data_room.folder_or_404(db, theirs, room.folder().id))
    _not_found(lambda: data_room.document_or_404(db, theirs, "not-a-uuid"))
    _not_found(lambda: authz.authorize(db, session_ctx(stranger), room.project.public_id, "download"))


# -------------------------------------------------------------------------------- deletions
def test_who_may_delete_a_file(db: Session, room: Room) -> None:
    owners = room.upload(room.owner)
    editors = room.upload(room.editor)
    assert isinstance(owners, PrivateProjectDocument) and isinstance(editors, PrivateProjectDocument)
    with pytest.raises(authz.RoleForbidden):
        data_room.delete_document(db, room.access(room.editor, "upload"), owners.id, room.store)
    with pytest.raises(authz.RoleForbidden):
        data_room.delete_document(db, room.access(room.viewer), editors.id, room.store)
    data_room.delete_document(db, room.access(room.editor, "upload"), editors.id, room.store)
    data_room.delete_document(db, room.access(room.owner, "upload"), owners.id, room.store)
    assert len(_objects(room)) == 2  # the objects go only once the deletion commits
    db.commit()
    assert _objects(room) == []
    assert [line.action for line in _log(db, room)][-2:] == ["deleted", "deleted"]


def test_deleting_an_item_removes_its_objects_after_the_commit(db: Session, room: Room) -> None:
    room.upload()
    room.upload(room.editor)
    db.commit()
    access = room.access(room.owner, "delete")
    assert data_room.forget_project(db, access, room.store) == 2
    authz.delete_project(db, access)
    assert len(_objects(room)) == 2
    db.commit()
    assert _objects(room) == []
    for table in DATA_ROOM_TABLES:
        assert db.scalar(sa.text(f"SELECT count(*) FROM {table}")) == 0  # noqa: S608 -- fixed names


def test_an_archived_item_reads_but_does_not_change(db: Session, room: Room) -> None:
    document = room.upload()
    assert isinstance(document, PrivateProjectDocument)
    authz.set_archived(db, room.access(room.owner, "archive"), True)
    data_room.open_document(db, room.access(room.owner), document.id, store=room.store, sealer=SEALER)
    data_room.access_log(db, room.access(room.owner, "audit"))
    _not_found(lambda: room.access(room.owner, "upload"))
    assert not data_room.may_delete(room.access(room.owner), document)


def test_opening_an_archived_items_data_room_writes_nothing(db: Session, room: Room) -> None:
    """Review of 2026-10-23: the first opening created the default folders even on an archived item,
    which is read-only. Now it lists what is there and writes nothing."""
    authz.set_archived(db, room.access(room.owner, "archive"), True)
    db.commit()
    assert data_room.folder_summaries(db, room.access(room.viewer)) == []
    assert db.scalar(sa.select(sa.func.count()).select_from(PrivateProjectFolder)) == 0
    assert not db.new and not db.dirty


# -------------------------------------------------------------------------------- access log
def test_the_access_log_is_the_owners_and_views_coalesce(db: Session, room: Room) -> None:
    folder = room.folder()
    for minutes in (0, 5, 11):
        data_room.folder_documents(
            db, room.access(room.viewer), folder.id, now=NOW + dt.timedelta(minutes=minutes)
        )
    views = [line for line in _log(db, room) if line.action == "viewed"]
    assert len(views) == 2 and views[0].folder_name == "Permits"
    assert data_room.access_log(db, room.access(room.owner, "audit"), limit=1)
    with pytest.raises(authz.RoleForbidden):
        data_room.access_log(db, room.access(room.editor))


# ----------------------------------------------------------------------------- the account
def test_erasure_deletes_owned_rooms_and_anonymises_the_rest(
    db: Session, room: Room, make_user: UserFactory, tmp_path: pathlib.Path
) -> None:
    # The owner's own item, with a file the editor uploaded.
    room.upload(room.owner)
    room.upload(room.editor)
    # The editor's own item elsewhere, with a file the owner uploaded into it as an editor.
    other = authz.create_project(db, session_ctx(room.editor), fields={"title": "Editor's item"})
    db.add(PrivateProjectMember(project_id=other.project.id, user_id=room.owner.id, role="editor"))
    db.flush()
    data_room.ensure_default_folders(db, other)
    other_folder = (
        db.execute(sa.select(PrivateProjectFolder).where(PrivateProjectFolder.project_id == other.project.id))
        .scalars()
        .first()
    )
    assert other_folder is not None
    as_editor = authz.authorize(db, session_ctx(room.owner), other.project.public_id, "upload")
    kept = data_room.store_upload(
        db,
        as_editor,
        folder_id=other_folder.id,
        raw_name="kept.pdf",
        data=PDF,
        store=room.store,
        sealer=SEALER,
        scanner=NoScanner(),
        limits=LIMITS,
    )
    assert isinstance(kept, PrivateProjectDocument)
    db.commit()
    assert len(_objects(room)) == 3

    result = data_room.erase_user_data_rooms(db, room.owner, room.store)
    db.commit()
    assert (result.owned_items, result.documents_deleted, result.folders_deleted) == (1, 2, 7)
    assert result.documents_kept_as_former_member == 1 and result.log_lines_anonymised >= 1
    assert _objects(room) == [kept.object_key]
    db.refresh(kept)
    assert kept.uploaded_by_user_id is None
    assert (
        db.scalar(
            sa.select(sa.func.count())
            .select_from(PrivateProjectDocumentEvent)
            .where(PrivateProjectDocumentEvent.actor_user_id == room.owner.id)
        )
        == 0
    )


def test_no_data_room_operation_writes_an_event(db: Session, room: Room) -> None:
    document = room.upload()
    assert isinstance(document, PrivateProjectDocument)
    data_room.open_document(db, room.access(room.owner), document.id, store=room.store, sealer=SEALER)
    data_room.folder_documents(db, room.access(room.owner), room.folder().id)
    data_room.delete_document(db, room.access(room.owner, "upload"), document.id, room.store)
    db.commit()
    assert db.scalar(sa.select(sa.func.count()).select_from(Event)) == 0
    assert data_room.stored_object_keys(db) == set()
