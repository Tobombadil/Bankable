"""Where data-room files live and how they are sealed (`services/projects/data_room_store.py`): the
seals, the stores (a directory for development, any S3-compatible bucket, here a local stand-in),
the settings, deletes that follow the commit, and the transfer slots."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import os
import pathlib
import stat
import threading

import pytest
from sqlalchemy.orm import Session, sessionmaker

from services.projects import data_room_store as rooms
from services.projects.conftest import TEST_DATA_ROOM_KEY, S3StandIn

KEY = base64.urlsafe_b64decode(TEST_DATA_ROOM_KEY)
OTHER_KEY = b"another-32-byte-test-key-0123456"


# ------------------------------------------------------------------------------------- seals
def test_a_sealed_file_opens_with_its_key_and_its_object_key_only() -> None:
    sealer = rooms.Sealer(KEY)
    object_key = rooms.new_object_key()
    sealed, wrapped = sealer.seal(object_key, b"site lease, page 1")
    assert b"site lease" not in sealed and b"site lease" not in wrapped
    assert sealer.open(object_key, sealed, wrapped, sealer.fingerprint) == b"site lease, page 1"
    # Another object's key: the associated data does not match.
    with pytest.raises(rooms.DataRoomUnavailable):
        sealer.open(rooms.new_object_key(), sealed, wrapped, sealer.fingerprint)
    # One flipped bit in the object or in the wrapped key.
    tampered = bytearray(sealed)
    tampered[-1] ^= 1
    with pytest.raises(rooms.DataRoomUnavailable):
        sealer.open(object_key, bytes(tampered), wrapped, sealer.fingerprint)
    bad_wrap = bytearray(wrapped)
    bad_wrap[-1] ^= 1
    with pytest.raises(rooms.DataRoomUnavailable):
        sealer.open(object_key, sealed, bytes(bad_wrap), sealer.fingerprint)


def test_each_file_has_its_own_key_and_nonce() -> None:
    sealer = rooms.Sealer(KEY)
    key = rooms.new_object_key()
    first, wrapped_first = sealer.seal(key, b"same bytes")
    second, wrapped_second = sealer.seal(key, b"same bytes")
    assert first != second and wrapped_first != wrapped_second


def test_another_deployment_key_cannot_open_a_file() -> None:
    sealer, other = rooms.Sealer(KEY), rooms.Sealer(OTHER_KEY)
    assert sealer.fingerprint != other.fingerprint
    key = rooms.new_object_key()
    sealed, wrapped = sealer.seal(key, b"x")
    with pytest.raises(rooms.DataRoomUnavailable, match="another DATA_ROOM_KEY"):
        other.open(key, sealed, wrapped, sealer.fingerprint)
    with pytest.raises(rooms.DataRoomUnavailable, match="integrity"):
        other.open(key, sealed, wrapped, other.fingerprint)


@pytest.mark.parametrize("raw", ["", "   ", "not base64!", base64.b64encode(b"short").decode()])
def test_a_missing_or_malformed_key_makes_the_room_unavailable(raw: str) -> None:
    with pytest.raises(rooms.DataRoomUnavailable):
        rooms.sealer_from_env({"DATA_ROOM_KEY": raw})


def test_the_key_is_read_in_either_base64_alphabet() -> None:
    standard = base64.b64encode(KEY).decode()
    assert rooms.sealer_from_env({"DATA_ROOM_KEY": standard}).fingerprint == rooms.Sealer(KEY).fingerprint
    assert (
        rooms.sealer_from_env({"DATA_ROOM_KEY": TEST_DATA_ROOM_KEY}).fingerprint
        == rooms.Sealer(KEY).fingerprint
    )


def test_new_key_prints_a_usable_key() -> None:
    written: list[str] = []
    assert rooms.main(["new-key"], write=written.append) == 0
    rooms.sealer_from_env({"DATA_ROOM_KEY": written[0].strip()})
    assert rooms.main([], write=written.append) == 2


# -------------------------------------------------------------------------------- object keys
def test_object_keys_are_random_and_carry_nothing() -> None:
    keys = {rooms.new_object_key() for _ in range(200)}
    assert len(keys) == 200
    assert all(rooms.is_object_key(key) and key.startswith("data-rooms/") for key in keys)
    for bad in ("data-rooms/../etc/passwd", "data/x", "data-rooms/ABC", "", None, 5):
        assert not rooms.is_object_key(bad)


# ------------------------------------------------------------------------------------- stores
def test_the_local_store_writes_owner_only_and_lists_its_objects(tmp_path: pathlib.Path) -> None:
    store = rooms.LocalObjectStore(tmp_path)
    key = rooms.new_object_key()
    store.put(key, b"sealed bytes")
    path = tmp_path / key
    assert path.read_bytes() == b"sealed bytes"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert store.get(key) == b"sealed bytes"
    assert [listed for listed, _ in store.list_objects()] == [key]
    store.delete(key)
    store.delete(key)  # absent is not an error
    with pytest.raises(FileNotFoundError):
        store.get(key)
    with pytest.raises(ValueError):
        store.put("../outside", b"x")


def test_the_s3_store_round_trips_against_a_local_stand_in(s3_standin: S3StandIn) -> None:
    store = rooms.store_from_env(s3_standin.env())
    assert isinstance(store, rooms.S3ObjectStore) and store.bucket == "private-bucket"
    key = rooms.new_object_key()
    store.put(key, b"\x00sealed\xff" * 1000)
    assert store.get(key) == b"\x00sealed\xff" * 1000
    listed = list(store.list_objects())
    assert [k for k, _ in listed] == [key] and listed[0][1].tzinfo is not None
    store.delete(key)
    with pytest.raises(FileNotFoundError):
        store.get(key)
    assert ("PUT", key) in s3_standin.requests and ("DELETE", key) in s3_standin.requests


def test_an_unreachable_bucket_is_unavailable_and_names_no_object(s3_standin: S3StandIn) -> None:
    env = s3_standin.env() | {"R2_ENDPOINT": "http://127.0.0.1:9"}
    store = rooms.store_from_env(env)
    key = rooms.new_object_key()
    with pytest.raises(rooms.DataRoomUnavailable) as caught:
        store.put(key, b"x")
    assert key not in str(caught.value)


def test_store_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    local = rooms.store_from_env({"DATA_ROOM_STORE": "local", "DATA_ROOM_LOCAL_DIR": str(tmp_path)})
    assert isinstance(local, rooms.LocalObjectStore)
    with pytest.raises(rooms.DataRoomUnavailable, match="DATA_ROOM_LOCAL_DIR"):
        rooms.store_from_env({"DATA_ROOM_STORE": "local"})
    with pytest.raises(rooms.DataRoomUnavailable, match="one of s3, local"):
        rooms.store_from_env({"DATA_ROOM_STORE": "disk"})
    with pytest.raises(rooms.DataRoomUnavailable, match=r"R2_BUCKET.*R2_ENDPOINT or R2_ACCOUNT_ID"):
        rooms.store_from_env({})
    # Never a container's disk in a deployment.
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(rooms.DataRoomUnavailable, match="development only"):
        rooms.store_from_env({"DATA_ROOM_STORE": "local", "DATA_ROOM_LOCAL_DIR": str(tmp_path)})


def test_the_switch_reads_on_and_nothing_else() -> None:
    assert rooms.data_rooms_enabled({"DATA_ROOMS": " On "})
    for value in ("", "off", "true", "1", "yes"):
        assert not rooms.data_rooms_enabled({"DATA_ROOMS": value})
    assert not rooms.data_rooms_enabled({})


def test_limits() -> None:
    default = rooms.limits_from_env({})
    assert (default.max_file_bytes, default.quota_bytes) == (25 * rooms.MIB, 1024 * rooms.MIB)
    custom = rooms.limits_from_env({"DATA_ROOM_MAX_FILE_MB": "10", "DATA_ROOM_QUOTA_MB": "200"})
    assert (custom.max_file_bytes, custom.quota_bytes) == (10 * rooms.MIB, 200 * rooms.MIB)
    for bad in ("0", "51", "ten", "-1"):
        with pytest.raises(rooms.DataRoomUnavailable):
            rooms.limits_from_env({"DATA_ROOM_MAX_FILE_MB": bad})
    assert rooms.UPLOAD_BODY_CEILING > rooms.MAX_FILE_MB_CEILING * rooms.MIB


# ------------------------------------------------------------------------ after the commit
def test_deletes_follow_the_commit_and_rollback_discards_new_objects(
    db: Session, tmp_path: pathlib.Path
) -> None:
    store = rooms.LocalObjectStore(tmp_path)
    kept, gone, written = rooms.new_object_key(), rooms.new_object_key(), rooms.new_object_key()
    for key in (kept, gone):
        store.put(key, b"x")
    # Scheduled deletes are forgotten on rollback; a write made in that transaction is removed.
    rooms.delete_after_commit(db, store, [kept])
    store.put(written, b"y")
    rooms.discard_on_rollback(db, store, written)
    db.rollback()
    assert store.get(kept) == b"x"
    with pytest.raises(FileNotFoundError):
        store.get(written)
    # A commit removes what was scheduled and keeps what was written.
    rooms.delete_after_commit(db, store, [gone])
    store.put(written, b"y")
    rooms.discard_on_rollback(db, store, written)
    db.commit()
    with pytest.raises(FileNotFoundError):
        store.get(gone)
    assert store.get(written) == b"y" and store.get(kept) == b"x"


def test_a_released_savepoint_decides_nothing(db: Session, tmp_path: pathlib.Path) -> None:
    """Review of 2026-10-23: SQLAlchemy fires `after_commit` when a savepoint is released, and the
    objects went then; the outer rollback that followed restored their rows, so the files were
    destroyed. Now only the root transaction's commit removes them."""
    store = rooms.LocalObjectStore(tmp_path)
    scheduled, written = rooms.new_object_key(), rooms.new_object_key()
    store.put(scheduled, b"x")
    rooms.delete_after_commit(db, store, [scheduled])
    store.put(written, b"y")
    rooms.discard_on_rollback(db, store, written)
    with db.begin_nested():
        pass
    assert store.get(scheduled) == b"x", "a savepoint's release removed an object"
    db.rollback()
    assert store.get(scheduled) == b"x", "its rows are back, so its object stays"
    with pytest.raises(FileNotFoundError):
        store.get(written)  # the outer rollback still discards what was written in it


def test_a_savepoint_rolled_back_forgets_its_deletes_and_orphans_its_writes(
    db: Session, tmp_path: pathlib.Path
) -> None:
    store = rooms.LocalObjectStore(tmp_path)
    outside, inside, written_inside = (rooms.new_object_key() for _ in range(3))
    for key in (outside, inside):
        store.put(key, b"x")
    rooms.delete_after_commit(db, store, [outside])
    savepoint = db.begin_nested()
    rooms.delete_after_commit(db, store, [inside])
    store.put(written_inside, b"y")
    rooms.discard_on_rollback(db, store, written_inside)
    savepoint.rollback()
    db.commit()
    # The root committed: the delete scheduled outside the savepoint happens; the one inside it was
    # undone with its rows; the object written inside it lost its row, so it goes too.
    with pytest.raises(FileNotFoundError):
        store.get(outside)
    assert store.get(inside) == b"x"
    with pytest.raises(FileNotFoundError):
        store.get(written_inside)


def test_a_session_closed_without_a_commit_discards_what_it_wrote(
    db_sessionmaker: sessionmaker[Session], tmp_path: pathlib.Path
) -> None:
    """Closed with neither a commit nor a rollback (a request that failed before either): before the
    fix the object stayed until the retention job's sweep."""
    store = rooms.LocalObjectStore(tmp_path)
    key = rooms.new_object_key()
    store.put(key, b"y")
    with db_sessionmaker() as session:
        rooms.discard_on_rollback(session, store, key)
    with pytest.raises(FileNotFoundError):
        store.get(key)


def test_a_failed_delete_after_commit_is_counted_not_raised(
    db: Session, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        name = "broken"

        def delete(self, key: str) -> None:
            raise rooms.DataRoomUnavailable("down")

    rooms.delete_after_commit(db, Broken(), [rooms.new_object_key()])  # type: ignore[arg-type]
    with caplog.at_level("WARNING"):
        db.commit()
    assert any(getattr(record, "failed_count", None) == 1 for record in caplog.records)


# --------------------------------------------------------------------------------- transfers
def test_transfer_slots_bound_concurrent_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_ROOM_TRANSFERS", "1")
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with rooms.transfer_slot():
            entered.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(5)
    with pytest.raises(rooms.DataRoomBusy):
        with rooms.transfer_slot(wait_seconds=0.05):
            pass
    release.set()
    worker.join(5)
    with rooms.transfer_slot(wait_seconds=1):
        pass
    assert rooms.transfer_count({"DATA_ROOM_TRANSFERS": "x"}) == rooms.DEFAULT_TRANSFERS
    assert rooms.transfer_count({"DATA_ROOM_TRANSFERS": "99"}) == 16


def test_the_async_slot_shares_the_slots_and_gives_up_after_its_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_ROOM_TRANSFERS", "1")

    async def scenario() -> None:
        async with rooms.transfer_slot_async(wait_seconds=1):
            with pytest.raises(rooms.DataRoomBusy):
                async with rooms.transfer_slot_async(wait_seconds=0.05, poll_seconds=0.01):
                    pass
            with pytest.raises(rooms.DataRoomBusy), rooms.transfer_slot(wait_seconds=0.01):
                pass
        async with rooms.transfer_slot_async(wait_seconds=0.05):
            pass

    asyncio.run(scenario())


def test_one_upload_in_progress_per_user() -> None:
    with rooms.user_upload("user-a"):
        with pytest.raises(rooms.UploadInProgress), rooms.user_upload("user-a"):
            pass
        with rooms.user_upload("user-b"):
            pass
    with rooms.user_upload("user-a"):
        pass
    assert "user-a" not in rooms._uploads_in_flight and "user-b" not in rooms._uploads_in_flight


def test_the_upload_body_limit_is_the_cap_and_the_form() -> None:
    limits = rooms.limits_from_env({"DATA_ROOM_MAX_FILE_MB": "10"})
    assert rooms.upload_body_limit(limits) == 10 * rooms.MIB + rooms.UPLOAD_FORM_OVERHEAD
    assert rooms.upload_body_limit(limits) < rooms.UPLOAD_BODY_CEILING


def test_listing_skips_what_is_not_a_data_room_object(tmp_path: pathlib.Path) -> None:
    store = rooms.LocalObjectStore(tmp_path)
    (tmp_path / "data-rooms").mkdir()
    (tmp_path / "data-rooms" / "notes.txt").write_text("x")
    key = rooms.new_object_key()
    store.put(key, b"x")
    listed = list(store.list_objects())
    assert [k for k, _ in listed] == [key]
    assert listed[0][1] <= dt.datetime.now(dt.UTC)
