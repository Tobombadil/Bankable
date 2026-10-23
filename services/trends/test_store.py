"""The daily documents' file store, reader and private-bucket mirror (`services/trends/store.py`).

Held here: a day is written atomically (the file and its directory flushed) and a second write
replaces it; a killed write's temporary file is swept once a day old; the reader lists and loads
the series in day order, reads every schema version ever written (a recorded version-1 document
included) and never invents a day; the mirror is configured only from the private bucket's
settings, refuses the public tiles bucket by its setting and by its name, uploads each document
once per content, and an unreachable bucket never raises and is retried by the next run.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import stat
import time
from typing import Any

import pytest

from pipeline.connectors.objectstore import S3Backend
from services.trends import store as trends_store
from services.trends.snapshot import DOCUMENT_KIND, SCHEMA_VERSION, figure
from services.trends.store import (
    DocumentError,
    Mirror,
    MirrorConfig,
    TrendStore,
    document_bytes,
    mirror_from_env,
    object_key,
    sync_uploads,
)

#: A version-1 document as the first recorder wrote it (the test store of `test_snapshot.py`).
V1_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "daily-v1-2026-10-23.json"

R2 = {
    "R2_BUCKET": "beta-object-storage-production",
    "R2_ACCESS_KEY_ID": "key-id",
    "R2_SECRET_ACCESS_KEY": "not-a-real-secret",
    "R2_ACCOUNT_ID": "acct",
}


def _doc(day: str, active: int = 1) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DOCUMENT_KIND,
        "as_of": day,
        "generated_at": f"{day}T05:58:00Z",
        "metrics": {"proposals": {"active": {"total": {"records": active}}}},
    }


class FakeBackend:
    """An object backend that records puts, or fails every one while `down`."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.down = False

    def put(self, key: str, data: bytes, *, immutable: bool = False) -> None:
        if self.down:
            raise ConnectionError("endpoint unreachable")
        self.puts.append(key)
        self.objects[key] = data


def _mirror(backend: FakeBackend, bucket: str = R2["R2_BUCKET"]) -> MirrorConfig:
    return MirrorConfig(Mirror(bucket, backend), "configured")


# ------------------------------------------------------------------------------ the file store
def test_a_day_is_written_where_the_contract_says_and_read_back(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    path = store.write(_doc("2026-10-24"))
    assert path == tmp_path / "normalized" / "trends" / "daily" / "2026-10-24.json"
    assert store.exists(dt.date(2026, 10, 24))
    assert store.load(dt.date(2026, 10, 24)) == _doc("2026-10-24")
    # Sorted keys, one per line: two days diff line by line.
    text = path.read_text()
    assert text.endswith("\n") and text.splitlines()[1].startswith(' "as_of"')
    assert path.read_bytes() == document_bytes(_doc("2026-10-24"))


def test_writing_a_day_twice_replaces_it_and_leaves_no_temporary_file(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_doc("2026-10-24", active=1))
    store.write(_doc("2026-10-24", active=2))
    assert store.load(dt.date(2026, 10, 24))["metrics"]["proposals"]["active"]["total"]["records"] == 2
    assert [p.name for p in store.daily.iterdir()] == ["2026-10-24.json"]


def test_a_failed_write_keeps_the_previous_document(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TrendStore(tmp_path)
    store.write(_doc("2026-10-24", active=1))

    def _fail(*_args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(trends_store.os, "replace", _fail)
    with pytest.raises(OSError, match="disk full"):
        store.write(_doc("2026-10-24", active=2))
    assert store.load(dt.date(2026, 10, 24))["metrics"]["proposals"]["active"]["total"]["records"] == 1
    assert [p.name for p in store.daily.iterdir()] == ["2026-10-24.json"]


def test_the_directory_is_flushed_after_the_rename(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rename(2)` is atomic but not durable until the directory entry is on disk: after the file's
    own fsync and the rename, the directory that holds it is fsynced too."""
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def _fsync(fd: int) -> None:
        order.append("fsync dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync file")
        real_fsync(fd)

    def _replace(src: Any, dst: Any) -> None:
        order.append("rename")
        real_replace(src, dst)

    monkeypatch.setattr(trends_store.os, "fsync", _fsync)
    monkeypatch.setattr(trends_store.os, "replace", _replace)
    store = TrendStore(tmp_path)
    store.write(_doc("2026-10-24"))
    assert order == ["fsync file", "rename", "fsync dir"]
    store.record_upload_state("private", {"2026-10-24": "0" * 64})
    assert order[-3:] == ["fsync file", "rename", "fsync dir"]


def test_a_filesystem_that_cannot_flush_a_directory_still_writes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync

    def _fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(22, "Invalid argument")  # EINVAL: some filesystems refuse a directory fsync
        real_fsync(fd)

    monkeypatch.setattr(trends_store.os, "fsync", _fsync)
    store = TrendStore(tmp_path)
    store.write(_doc("2026-10-24"))
    assert store.load(dt.date(2026, 10, 24)) == _doc("2026-10-24")


def _leftover(directory: pathlib.Path, name: str, age_hours: float) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("{")
    then = time.time() - age_hours * 3600
    os.utime(path, (then, then))
    return path


def test_a_killed_writes_temporary_file_is_swept_once_a_day_old(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    old = _leftover(store.daily, f".2026-10-22.json.{'a' * 32}.tmp", age_hours=25)
    old_state = _leftover(store.state_path.parent, f".uploaded.json.{'b' * 32}.tmp", age_hours=30)
    in_flight = _leftover(store.daily, f".2026-10-24.json.{'c' * 32}.tmp", age_hours=0.1)
    # Not ours: a name the writer never makes is left alone, however old.
    foreign = _leftover(store.daily, "notes.tmp", age_hours=100)
    store.write(_doc("2026-10-24"))
    assert not old.exists() and not old_state.exists()
    assert in_flight.exists() and foreign.exists()
    assert store.days() == [dt.date(2026, 10, 24)]


def test_a_run_that_writes_nothing_still_sweeps(tmp_path: pathlib.Path) -> None:
    """A `fill` run on a day already written writes nothing; it still calls `sync_uploads`."""
    store = TrendStore(tmp_path)
    old = _leftover(store.daily, f".2026-10-22.json.{'d' * 32}.tmp", age_hours=48)
    assert sync_uploads(store, mirror_from_env({}))["status"] == "not_configured"
    assert not old.exists()
    assert store.sweep_temporaries() == 0  # nothing left; an absent directory is not an error
    assert TrendStore(tmp_path / "empty").sweep_temporaries() == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "something.else", "schema_version": 1, "as_of": "2026-10-24"},
        {"kind": DOCUMENT_KIND, "schema_version": "1", "as_of": "2026-10-24"},
        {"kind": DOCUMENT_KIND, "schema_version": True, "as_of": "2026-10-24"},
        {"kind": DOCUMENT_KIND, "schema_version": SCHEMA_VERSION + 1, "as_of": "2026-10-24"},
        {"kind": DOCUMENT_KIND, "schema_version": 0, "as_of": "2026-10-24"},
        {"kind": DOCUMENT_KIND, "schema_version": 1, "as_of": "yesterday"},
    ],
)
def test_only_a_daily_document_is_written(tmp_path: pathlib.Path, bad: dict[str, Any]) -> None:
    with pytest.raises(DocumentError):
        TrendStore(tmp_path).write(bad)


def test_a_version_1_document_is_still_read(tmp_path: pathlib.Path) -> None:
    """The series began at version 1: the reader loads those days beside the newer ones, and a
    figure version 1 did not record reads as absent, never as zero."""
    assert SCHEMA_VERSION == 2
    store = TrendStore(tmp_path)
    store.daily.mkdir(parents=True)
    shutil.copyfile(V1_FIXTURE, store.path_for(dt.date(2026, 10, 23)))
    store.write(_doc("2026-10-24"))
    v1 = store.load(dt.date(2026, 10, 23))
    assert v1["schema_version"] == 1 and v1["metrics"]["activity"]["rule"] == "genuine-change-v1"
    assert [d["schema_version"] for d in store.series()] == [1, 2]
    assert figure(v1, "proposals.active.by_state.US-TX.records") == 5
    assert figure(v1, "proposals.withdrawn.by_state", None) is None
    assert figure(v1, "proposals.active.by_state_kind.US-TX.load.records", None) is None
    with pytest.raises(KeyError):
        figure(v1, "opportunities.open.energy.by_state")
    # A recorded day is uploaded as it is, whatever its version.
    backend = FakeBackend()
    assert sync_uploads(store, _mirror(backend))["uploaded"] == 2
    assert backend.objects["trends/daily/2026-10-23.json"] == V1_FIXTURE.read_bytes()


def test_a_document_from_a_newer_version_is_skipped_not_misread(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_doc("2026-10-23"))
    newer = {**_doc("2026-10-24"), "schema_version": SCHEMA_VERSION + 1}
    store.path_for(dt.date(2026, 10, 24)).write_text(json.dumps(newer))
    assert [d["as_of"] for d in store.series()] == ["2026-10-23"]
    with pytest.raises(DocumentError, match="schema_version"):
        store.load(dt.date(2026, 10, 24))


def test_the_reader_lists_the_series_in_day_order_and_never_invents_a_day(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    assert store.days() == [] and list(store.series()) == []
    for day in ("2026-10-26", "2026-10-24", "2026-10-23"):
        store.write(_doc(day))
    (store.daily / "notes.txt").write_text("not a document")
    (store.daily / ".2026-10-27.json.abc.tmp").write_text("{}")
    (store.daily / "2026-13-01.json").write_text("{}")
    assert store.days() == [dt.date(2026, 10, 23), dt.date(2026, 10, 24), dt.date(2026, 10, 26)]
    assert [d["as_of"] for d in store.series()] == ["2026-10-23", "2026-10-24", "2026-10-26"]
    window = store.series(start=dt.date(2026, 10, 24), end=dt.date(2026, 10, 25))
    assert [d["as_of"] for d in window] == ["2026-10-24"]
    with pytest.raises(FileNotFoundError):
        store.load(dt.date(2026, 10, 25))


def test_an_unreadable_file_is_skipped_by_the_series_and_refused_by_load(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_doc("2026-10-23"))
    store.path_for(dt.date(2026, 10, 24)).write_text("{not json")
    store.path_for(dt.date(2026, 10, 25)).write_text(json.dumps(_doc("2026-10-20")))
    assert [d["as_of"] for d in store.series()] == ["2026-10-23"]
    for day in (dt.date(2026, 10, 24), dt.date(2026, 10, 25)):
        with pytest.raises(DocumentError):
            store.load(day)


# ---------------------------------------------------------------------------------- the mirror
def test_no_bucket_is_not_configured_and_a_missing_credential_is_named_not_shown() -> None:
    assert mirror_from_env({}).status == "not_configured"
    config = mirror_from_env({"R2_BUCKET": "b", "R2_SECRET_ACCESS_KEY": "s3cr3t-value"})
    assert config.mirror is None and config.status == "incomplete"
    assert config.detail is not None
    assert "R2_ACCESS_KEY_ID" in config.detail and "R2_ENDPOINT or R2_ACCOUNT_ID" in config.detail
    assert "s3cr3t-value" not in config.detail


def test_the_public_tiles_bucket_is_refused() -> None:
    config = mirror_from_env({**R2, "R2_TILES_BUCKET": R2["R2_BUCKET"]})
    assert config.mirror is None and config.status == "refused"
    assert mirror_from_env({**R2, "R2_TILES_BUCKET": "beta-object-storage-tiles-production"}).mirror


@pytest.mark.parametrize(
    "bucket", ["beta-object-storage-tiles-production", "TILES", "my-Tiles-copy", "basemaptiles"]
)
def test_a_bucket_named_like_the_tiles_bucket_is_refused_without_the_setting(bucket: str) -> None:
    """The worker's environment need not carry `R2_TILES_BUCKET` (only the deploy's basemap job is
    given it), so the name alone refuses: `<prefix>-tiles-production` is the public bucket."""
    config = mirror_from_env({**R2, "R2_BUCKET": bucket})
    assert config.mirror is None and config.status == "refused"
    assert config.detail is not None and "tiles" in config.detail
    assert mirror_from_env({**R2, "R2_BUCKET": bucket, "R2_TILES_BUCKET": "other"}).status == "refused"


def test_the_private_bucket_is_written_at_the_trends_key_with_no_data_prefix() -> None:
    config = mirror_from_env(R2)
    assert config.status == "configured" and config.mirror is not None
    backend = config.mirror.backend
    assert isinstance(backend, S3Backend)
    assert backend.bucket == R2["R2_BUCKET"] and backend.prefix == ""
    assert backend.endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert object_key(dt.date(2026, 10, 24)) == "trends/daily/2026-10-24.json"
    assert backend._key(object_key(dt.date(2026, 10, 24))) == "trends/daily/2026-10-24.json"


def test_each_document_is_uploaded_once_per_content(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_doc("2026-10-23"))
    store.write(_doc("2026-10-24"))
    backend = FakeBackend()
    assert sync_uploads(store, _mirror(backend)) == {"status": "ok", "uploaded": 2, "pending": 0}
    assert backend.puts == ["trends/daily/2026-10-23.json", "trends/daily/2026-10-24.json"]
    assert backend.objects["trends/daily/2026-10-24.json"] == store.read_bytes(dt.date(2026, 10, 24))
    assert sync_uploads(store, _mirror(backend))["uploaded"] == 0
    store.write(_doc("2026-10-24", active=9))  # the day's daily run replaced an earlier fill
    assert sync_uploads(store, _mirror(backend))["uploaded"] == 1
    assert backend.puts[-1] == "trends/daily/2026-10-24.json"
    state = json.loads(store.state_path.read_text())
    digest = hashlib.sha256(store.read_bytes(dt.date(2026, 10, 24))).hexdigest()
    assert state == {
        "bucket": R2["R2_BUCKET"],
        "objects": {
            "2026-10-23": hashlib.sha256(store.read_bytes(dt.date(2026, 10, 23))).hexdigest(),
            "2026-10-24": digest,
        },
    }
    # Another bucket holds none of them yet.
    other = FakeBackend()
    assert sync_uploads(store, _mirror(other, bucket="another-private-bucket"))["uploaded"] == 2


def test_an_unreachable_bucket_never_raises_and_the_next_run_uploads(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_doc("2026-10-23"))
    store.write(_doc("2026-10-24"))
    backend = FakeBackend()
    backend.down = True
    result = sync_uploads(store, _mirror(backend))
    assert result == {"status": "failed", "detail": "ConnectionError", "uploaded": 0, "pending": 2}
    assert store.exists(dt.date(2026, 10, 24))
    backend.down = False
    assert sync_uploads(store, _mirror(backend)) == {"status": "ok", "uploaded": 2, "pending": 0}


def test_a_real_backend_whose_endpoint_fails_is_reported_not_raised(tmp_path: pathlib.Path) -> None:
    class DeadClient:
        def put_object(self, **_kwargs: Any) -> None:
            raise ConnectionError("could not connect to the endpoint URL")

    store = TrendStore(tmp_path)
    store.write(_doc("2026-10-24"))
    backend = S3Backend("private", prefix="", client=DeadClient())
    result = sync_uploads(store, MirrorConfig(Mirror("private", backend), "configured"))
    assert result["status"] == "failed" and result["detail"] == "StoreWriteError"


def test_a_mirror_that_is_not_configured_uploads_nothing(tmp_path: pathlib.Path) -> None:
    store = TrendStore(tmp_path)
    store.write(_doc("2026-10-24"))
    assert sync_uploads(store, mirror_from_env({})) == {
        "status": "not_configured",
        "detail": None,
        "uploaded": 0,
    }
    refused = mirror_from_env({**R2, "R2_TILES_BUCKET": R2["R2_BUCKET"]})
    assert sync_uploads(store, refused)["status"] == "refused"
    assert not store.state_path.exists()


def test_a_run_uploads_at_most_its_cap(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trends_store, "MAX_UPLOADS_PER_RUN", 2)
    store = TrendStore(tmp_path)
    for day in ("2026-10-21", "2026-10-22", "2026-10-23"):
        store.write(_doc(day))
    backend = FakeBackend()
    assert sync_uploads(store, _mirror(backend)) == {"status": "ok", "uploaded": 2, "pending": 1}
    assert sync_uploads(store, _mirror(backend)) == {"status": "ok", "uploaded": 1, "pending": 0}
