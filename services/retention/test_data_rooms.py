"""The retention job's data-room rule (`services/retention/data_rooms.py`): objects no document row
names are swept once older than the grace period; recent ones and named ones stay; a dry run changes
nothing; rows whose object is missing are counted; the rule reaches no store where data rooms are off."""

from __future__ import annotations

import datetime as dt
import os
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from services.api.auth import AuthContext, register_user
from services.db.session import get_sessionmaker
from services.projects import authz, data_room
from services.projects import data_room_store as rooms
from services.projects.data_room_scan import NoScanner
from services.retention import data_rooms as rule
from services.retention import run as retention
from tests.db_template import disposing, fresh_engine

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.UTC)
KEY = b"retention-test-key-32-bytes-long"
PASSWORD = "correct horse battery"  # noqa: S105 -- a fixture value, not a credential


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    with disposing(fresh_engine()) as engine:
        yield get_sessionmaker(engine)


def _age(store: rooms.LocalObjectStore, key: str, hours: float) -> None:
    moment = (NOW - dt.timedelta(hours=hours)).timestamp()
    os.utime(store.root / key, (moment, moment))


def _stored_document(db: Session, store: rooms.LocalObjectStore) -> str:
    user = register_user(db, email="owner@example.org", password=PASSWORD, name="Owner")
    user.email_verified_at = NOW
    db.flush()
    ctx = AuthContext(user=user, account=user.account, api_key=None, entitlement="public", scopes=frozenset())
    access = authz.create_project(db, ctx, fields={"title": "Item"})
    data_room.ensure_default_folders(db, access)
    folder = data_room.folder_summaries(db, access)[0].folder
    document = data_room.store_upload(
        db,
        authz.authorize(db, ctx, access.project.public_id, "upload"),
        folder_id=folder.id,
        raw_name="lease.pdf",
        data=b"%PDF-1.7\n%%EOF\n",
        store=store,
        sealer=rooms.Sealer(KEY),
        scanner=NoScanner(),
        limits=rooms.Limits(max_file_bytes=1024, quota_bytes=4096),
    )
    assert isinstance(document, data_room.PrivateProjectDocument)
    db.commit()
    return document.object_key


def test_orphans_past_the_grace_period_are_swept(factory: Any, tmp_path: pathlib.Path) -> None:
    store = rooms.LocalObjectStore(tmp_path)
    with factory() as db:
        named = _stored_document(db, store)
    old_orphan, young_orphan = rooms.new_object_key(), rooms.new_object_key()
    for key in (old_orphan, young_orphan):
        store.put(key, b"sealed")
    _age(store, named, 48)
    _age(store, old_orphan, 7)
    _age(store, young_orphan, 1)

    with factory() as db:
        dry = rule.sweep_orphans(db, NOW, dry_run=True, store=store)
    assert (dry["matched"], dry["changed"], dry["kept_recent"]) == (1, 0, 1)
    assert sorted(k for k, _ in store.list_objects()) == sorted([named, old_orphan, young_orphan])

    with factory() as db:
        report = rule.sweep_orphans(db, NOW, dry_run=False, store=store)
    assert (report["matched"], report["changed"], report["errors"], report["rows_without_object"]) == (
        1,
        1,
        0,
        0,
    )
    assert sorted(k for k, _ in store.list_objects()) == sorted([named, young_orphan])
    # The report names no key and no file.
    assert named not in str(report) and "lease" not in str(report)

    store.delete(named)
    with factory() as db:
        assert rule.sweep_orphans(db, NOW, dry_run=True, store=store)["rows_without_object"] == 1


def test_the_rule_runs_only_where_data_rooms_are_in_use(factory: Any, tmp_path: pathlib.Path) -> None:
    with factory() as db:
        off = rule.sweep_orphans(db, NOW, dry_run=True, env={})
        assert off["status"] == "skipped" and "off" in off["reason"]
        unconfigured = rule.sweep_orphans(db, NOW, dry_run=True, env={"DATA_ROOMS": "on"})
        assert unconfigured["status"] == "skipped" and "no data-room store" in unconfigured["reason"]
        local = rule.sweep_orphans(
            db,
            NOW,
            dry_run=True,
            env={"DATA_ROOM_STORE": "local", "DATA_ROOM_LOCAL_DIR": str(tmp_path)},
        )
        assert local["status"] == "applied" and local["matched"] == 0


def test_a_store_that_fails_to_list_is_an_error_in_the_run(factory: Any) -> None:
    class Down:
        name = "down"

        def list_objects(self) -> Iterator[tuple[str, dt.datetime]]:
            raise rooms.DataRoomUnavailable("down")
            yield ("", NOW)  # pragma: no cover - makes this a generator

    with factory() as db:
        report = rule.sweep_orphans(db, NOW, dry_run=False, store=Down())  # type: ignore[arg-type]
    assert report["errors"] == 1 and report["changed"] == 0


def test_the_retention_run_carries_the_rule(factory: Any, tmp_path: pathlib.Path) -> None:
    store = rooms.LocalObjectStore(tmp_path / "rooms")
    orphan = rooms.new_object_key()
    store.put(orphan, b"sealed")
    _age(store, orphan, 30)
    report = retention.run_retention(
        factory, now=NOW, data_root=tmp_path / "data", source_ids=(), data_room_store=store
    )
    assert report["rules"]["data_room_orphans"]["changed"] == 1 and report["errors"] == 0
    assert retention.RULES["data_room_orphans"] == rule.RULE
    assert list(store.list_objects()) == []
