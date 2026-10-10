"""DA-10 retention (docs/04 DA-10; docs/20 §3.2, §11; docs/21 §3.16, §4.3): each rule removes exactly
what is past its age and nothing younger, the monthly sample keeps one snapshot per source per month,
what the pipeline still reads is never deleted, a dry run changes nothing, and a real run leaves one
run-log row. SQLite session factory and tmp data roots; no Postgres, no network."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import pathlib
from typing import Any

import pytest
from sqlalchemy import func, select

from pipeline.connectors.objectstore import LocalBackend, S3Backend
from pipeline.connectors.store import QuarantineStore, Store
from services.db.models import (
    Account,
    Alert,
    Event,
    Licence,
    SavedSearch,
    Snapshot,
    Source,
    SourceRun,
    User,
    UserSession,
)
from services.db.session import get_engine, get_sessionmaker, init_db, session_scope
from services.retention import run as retention
from services.retention.snapshots import (
    SnapshotFile,
    artefact_of,
    compact_snapshots,
    compact_source,
    months_before,
    plan,
    row_classes,
    taken_at,
)

UTC = dt.UTC
NOW = dt.datetime(2026, 10, 10, 1, 47, tzinfo=UTC)
CUTOFF = dt.datetime(2024, 10, 10, 1, 47, tzinfo=UTC)  # NOW minus 24 calendar months


def H(label: str) -> str:  # noqa: N802 -- reads as a label's hash in the expectations
    """The SHA-256 of the bytes `write_run` stores for `label` (the store verifies it on read)."""
    return hashlib.sha256(f"bytes {label}".encode()).hexdigest()


def ts(at: dt.datetime) -> str:
    return at.strftime("%Y%m%dT%H%M%SZ")


def d(y: int, m: int, day: int, hour: int = 6) -> dt.datetime:
    return dt.datetime(y, m, day, hour, tzinfo=UTC)


def write_run(
    root: pathlib.Path,
    source_id: str,
    at: dt.datetime,
    *,
    sha: str,
    status: str = "ok",
    ext: str = "csv",
    stored: bool = True,
    promoted: bool = False,
) -> pathlib.Path:
    """One run the way the runner leaves it: the object under the run's own `ts` (unless `stored` is
    false, as for an `unchanged` run), the run record, and for a promoted run its normalised file.
    `sha` is a label; the record carries the real hash of the bytes stored for it."""
    token = ts(at)
    path = root / "snapshots" / source_id / f"{token}.{ext}"
    if stored:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"bytes {sha}".encode())
    record: dict[str, Any] = {
        "id": f"run-{source_id}-{token}",
        "status": status,
        "snapshot": {"sha256": H(sha), "object_key": str(path) if stored else None},
        "outputs": {},
    }
    if promoted:
        normalized = root / "normalized" / source_id / f"{token}.parquet"
        normalized.parent.mkdir(parents=True, exist_ok=True)
        normalized.write_bytes(b"")
        record["outputs"]["normalized"] = str(normalized)
    run_path = root / "runs" / source_id / f"{token}.json"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text(json.dumps(record))
    return path


def names(root: pathlib.Path, source_id: str) -> list[str]:
    directory = root / "snapshots" / source_id
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


def build_daily(root: pathlib.Path) -> dict[str, pathlib.Path]:
    """A daily source: three old months and a straddling one, then a recent promoted run."""
    sid = "us.test.daily"
    files = {
        "aug01": write_run(root, sid, d(2024, 8, 1), sha="aug01", promoted=True),
        "aug02": write_run(root, sid, d(2024, 8, 2), sha="aug02", promoted=True),
        "aug03": write_run(root, sid, d(2024, 8, 3), sha="aug03", status="failed"),
        "sep10": write_run(root, sid, d(2024, 9, 10), sha="sep10", status="failed"),
        "sep20": write_run(root, sid, d(2024, 9, 20), sha="sep20", status="failed"),
        "oct05": write_run(root, sid, d(2024, 10, 5), sha="oct05", promoted=True),
        "oct09": write_run(root, sid, d(2024, 10, 9), sha="oct09", promoted=True),
        "oct10": write_run(root, sid, d(2024, 10, 10, 3), sha="oct10", promoted=True),  # 1 h past the cutoff
        "oct15": write_run(root, sid, d(2024, 10, 15), sha="oct15", promoted=True),
        "now": write_run(root, sid, d(2026, 10, 1), sha="latest", promoted=True),
    }
    write_run(root, sid, d(2026, 10, 2), sha="latest", status="unchanged", stored=False)
    return files


def build_stopped(root: pathlib.Path) -> None:
    """A source that stopped in March 2024: its last promoted bytes (A), the held run the next fetch
    compares against (B) and a failed fetch (C) are all older than 24 months."""
    sid = "us.test.stopped"
    write_run(root, sid, d(2024, 2, 1), sha="D", promoted=True)
    write_run(root, sid, d(2024, 2, 2), sha="E", promoted=True)
    write_run(root, sid, d(2024, 3, 1), sha="A", promoted=True)
    write_run(root, sid, d(2024, 3, 2), sha="B", status="partial")
    write_run(root, sid, d(2024, 3, 3), sha="C", status="failed")


@pytest.fixture(autouse=True)
def _local_store_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """`default_media` adds the bucket when `SNAPSHOT_STORE=s3`; these tests pass media or use local."""
    monkeypatch.delenv("SNAPSHOT_STORE", raising=False)


# ------------------------------------------------------------------------------- pure parts
def test_months_before_counts_calendar_months_and_clamps_the_day() -> None:
    assert months_before(NOW, 24) == CUTOFF
    assert months_before(NOW, 12) == dt.datetime(2025, 10, 10, 1, 47, tzinfo=UTC)
    assert months_before(dt.datetime(2026, 3, 31, tzinfo=UTC), 1) == dt.datetime(2026, 2, 28, tzinfo=UTC)
    assert months_before(dt.datetime(2026, 1, 15, tzinfo=UTC), 13) == dt.datetime(2024, 12, 15, tzinfo=UTC)


def test_taken_at_reads_the_runner_token_and_nothing_else() -> None:
    assert taken_at("20240801T060000Z.csv") == d(2024, 8, 1)
    assert taken_at("20240801T060000Z.csv.gz") == d(2024, 8, 1)
    assert taken_at("golden-copy.zip") is None
    assert taken_at("2024-08-01.csv") is None


def test_plan_keeps_the_newest_promoted_snapshot_of_each_old_month() -> None:
    def f(at: dt.datetime, status: str | None, sha: str | None = None, ext: str = "csv") -> SnapshotFile:
        return SnapshotFile(key=f"k/{ts(at)}.{ext}", name=f"{ts(at)}.{ext}", at=at, sha256=sha, status=status)

    files = [
        f(d(2024, 8, 1), "ok"),
        f(d(2024, 8, 2), "ok"),
        f(d(2024, 8, 3), "failed"),  # newest of August, but not promoted
        f(d(2024, 9, 10), "failed"),
        f(d(2024, 9, 20), None),  # no promoted run in September: the newest file is the sample
        f(d(2024, 7, 1), "failed", sha="baseline"),
        f(d(2024, 7, 2), "ok"),
        f(d(2026, 10, 1), "ok"),
        SnapshotFile(key="k/notes.txt", name="notes.txt", at=None),
    ]
    out = plan(files, cutoff=CUTOFF, in_use={f"k/{ts(d(2024, 7, 1))}.csv"})
    assert out == {
        f"k/{ts(d(2024, 8, 1))}.csv": "delete",
        f"k/{ts(d(2024, 8, 2))}.csv": "sample",
        f"k/{ts(d(2024, 8, 3))}.csv": "delete",
        f"k/{ts(d(2024, 9, 10))}.csv": "delete",
        f"k/{ts(d(2024, 9, 20))}.csv": "sample",
        f"k/{ts(d(2024, 7, 1))}.csv": "in_use",
        f"k/{ts(d(2024, 7, 2))}.csv": "sample",
        f"k/{ts(d(2026, 10, 1))}.csv": "young",
        "k/notes.txt": "undated",
    }


def test_plan_keeps_the_newest_file_of_each_extension_whatever_its_age() -> None:
    """A source that stopped: its newest file of each kind is what a builder's `--latest-snapshot`
    reads, so it stays even when the month's promoted sample is another file."""

    def f(key: str, at: dt.datetime, status: str | None) -> SnapshotFile:
        return SnapshotFile(key=key, name=f"{ts(at)}.{key.rsplit('.', 1)[1]}", at=at, status=status)

    files = [
        f("k/a.zip", d(2023, 1, 1), "ok"),  # January's promoted sample
        f("k/b.zip", d(2023, 1, 2), "failed"),
        f("k/c.zip", d(2023, 1, 3), "failed"),  # the newest zip
        f("k/d.xlsx", d(2022, 12, 5), None),  # the only (so newest) xlsx
    ]
    assert plan(files, cutoff=CUTOFF, in_use=()) == {
        "k/a.zip": "sample",
        "k/b.zip": "delete",
        "k/c.zip": "latest",
        "k/d.xlsx": "latest",
    }


def test_a_source_that_stores_several_files_per_run_keeps_a_sample_of_each() -> None:
    """EIA-923's builder stores the current and the previous year's workbooks on every build
    (measured on the operator's data root, 2026-10-10): one sample per source would drop one."""
    files = []
    for build, day in enumerate((1, 9, 20)):
        for year in (2023, 2024):
            at = d(2024, 3, day) + dt.timedelta(seconds=2 * (year - 2023))
            files.append(
                SnapshotFile(
                    key=f"k/{build}-{year}.zip",
                    name=f"{ts(at)}.zip",
                    at=at,
                    status="ok",
                    artefact=f"www.eia.gov/electricity/data/eia923/xls/f923_{year}.zip",
                )
            )
    for year in (2023, 2024):  # a recent build of both: the newest copy of each artefact is young
        url = f"www.eia.gov/electricity/data/eia923/xls/f923_{year}.zip"
        name = f"{ts(d(2026, 10, 1) + dt.timedelta(seconds=year - 2023))}.zip"
        files.append(SnapshotFile(key=f"k/{year}.zip", name=name, at=d(2026, 10, 1), artefact=url))
    out = plan(files, cutoff=CUTOFF, in_use=())
    assert {k for k, reason in out.items() if reason == "sample"} == {"k/2-2023.zip", "k/2-2024.zip"}
    assert sorted(k for k, reason in out.items() if reason == "delete") == [
        "k/0-2023.zip",
        "k/0-2024.zip",
        "k/1-2023.zip",
        "k/1-2024.zip",
    ]


def test_artefact_drops_the_query_string() -> None:
    assert artefact_of("https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1") == (
        "www.ercot.com/misdownload/servlets/mirDownload"
    )
    assert artefact_of(None) == artefact_of("") == ""


def test_row_classes_follow_what_is_left() -> None:
    old, young = d(2024, 8, 1), d(2026, 1, 1)
    recorded = {"a": (old, "gone"), "b": (old, "kept_old"), "c": (young, "kept_young"), "e": (young, "lost")}
    survivors = [
        SnapshotFile(key="b", name="b", at=old, sha256="kept_old"),
        SnapshotFile(key="c", name="c", at=young, sha256="kept_young"),
    ]
    assert row_classes(recorded, survivors, cutoff=CUTOFF) == {
        "gone": "expired",
        "kept_old": "sampled",
        "kept_young": "full",
        # "lost": a young copy missing for a reason this job did not cause gets no class
    }


# ------------------------------------------------------------------------------- the medium
def test_each_old_month_keeps_one_sample_and_nothing_younger_is_touched(tmp_path: pathlib.Path) -> None:
    files = build_daily(tmp_path)
    runs_before = sorted(p.name for p in (tmp_path / "runs" / "us.test.daily").iterdir())
    outcome = compact_source(Store(tmp_path), "us.test.daily", medium="local", cutoff=CUTOFF, dry_run=False)
    assert outcome is not None
    kept = names(tmp_path, "us.test.daily")
    assert kept == sorted(
        files[k].name
        for k in ("aug02", "sep20", "oct09", "oct10", "oct15", "now")  # aug02/sep20/oct09: the samples
    )
    assert sorted(pathlib.Path(k).name for k in outcome.deleted) == sorted(
        files[k].name for k in ("aug01", "aug03", "sep10", "oct05")
    )
    assert outcome.reasons == {"sample": 3, "young": 3, "delete": 4}
    # Run records are never touched, nor the normalised output.
    assert sorted(p.name for p in (tmp_path / "runs" / "us.test.daily").iterdir()) == runs_before
    assert (tmp_path / "normalized" / "us.test.daily" / f"{ts(d(2024, 8, 1))}.parquet").exists()
    assert outcome.classes == {
        H("aug01"): "expired",
        H("aug03"): "expired",
        H("sep10"): "expired",
        H("oct05"): "expired",
        H("aug02"): "sampled",
        H("sep20"): "sampled",
        H("oct09"): "sampled",
        H("oct10"): "full",
        H("oct15"): "full",
        H("latest"): "full",
    }
    # A second run finds nothing more to delete: the rule is idempotent.
    again = compact_source(Store(tmp_path), "us.test.daily", medium="local", cutoff=CUTOFF, dry_run=False)
    assert again is not None and again.deleted == [] and again.classes == outcome.classes


def test_what_the_next_run_and_a_reparse_read_is_never_deleted(tmp_path: pathlib.Path) -> None:
    build_stopped(tmp_path)
    store = Store(tmp_path)
    assert store.last_snapshot_sha("us.test.stopped") == H("B")
    outcome = compact_source(store, "us.test.stopped", medium="local", cutoff=CUTOFF, dry_run=False)
    assert outcome is not None
    # D goes; E is February's sample; A (March's sample) and B are what the pipeline reads; C is
    # the newest file, which a builder's `--latest-snapshot` would read.
    assert outcome.reasons == {"delete": 1, "sample": 1, "in_use": 2, "latest": 1}
    assert names(tmp_path, "us.test.stopped") == sorted(
        f"{ts(at)}.csv" for at in (d(2024, 2, 2), d(2024, 3, 1), d(2024, 3, 2), d(2024, 3, 3))
    )
    # The pipeline's own readers still find their bytes: the comparison baseline and the bytes under
    # the last promoted output (restated by a parser change).
    previous = store.last_snapshot("us.test.stopped")
    assert previous is not None and previous.load() == b"bytes B"
    promoted = store.last_promoted("us.test.stopped")
    assert promoted is not None
    assert store.snapshot_bytes("us.test.stopped", promoted[0], promoted[1]) == b"bytes A"


def test_only_the_copy_the_pipeline_reads_is_held_back(tmp_path: pathlib.Path) -> None:
    """A-B-A: the runner stores the same bytes again after a change. `Store.snapshot_bytes` reads the
    newest copy, so the older identical one follows the monthly rule."""
    sid = "us.test.aba"
    write_run(tmp_path, sid, d(2024, 2, 1), sha="A", promoted=True)
    write_run(tmp_path, sid, d(2024, 2, 2), sha="B", promoted=True)
    write_run(tmp_path, sid, d(2024, 2, 3), sha="A", promoted=True)
    store = Store(tmp_path)
    outcome = compact_source(store, sid, medium="local", cutoff=CUTOFF, dry_run=False)
    assert outcome is not None and outcome.reasons == {"delete": 2, "latest": 1}
    assert names(tmp_path, sid) == [f"{ts(d(2024, 2, 3))}.csv"]
    previous = store.last_snapshot(sid)
    assert previous is not None and previous.load() == b"bytes A"
    assert outcome.classes == {H("A"): "sampled", H("B"): "expired"}


def test_builder_records_are_matched_by_their_time(tmp_path: pathlib.Path) -> None:
    """Context builders write run records with no `object_key` (EIA-923), a `snapshot.path`
    (the EIA atlas layers) or no `status`; the record under a file's own `ts` describes it unless it
    names another file. Three builds in one old month store identical bytes; one sample stays."""
    sid = "us.test.builder"
    url = "https://www.eia.gov/maps/map_data/Layer.zip"
    for day in (1, 2, 3):
        token = ts(d(2024, 3, day))
        path = tmp_path / "snapshots" / sid / f"{token}.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"bytes same")
        snap: dict[str, Any] = {"fetched_url": url, "sha256": H("same")}
        if day == 2:
            snap["path"] = f"snapshots/{sid}/{token}.zip"
        record = {"source_id": sid, "snapshot": snap}  # no status, no object_key
        (tmp_path / "runs" / sid).mkdir(parents=True, exist_ok=True)
        (tmp_path / "runs" / sid / f"{token}.json").write_text(json.dumps(record))
    # A record that names another object does not describe the file under its own time.
    stray = tmp_path / "snapshots" / sid / f"{ts(d(2024, 3, 4))}.zip"
    stray.write_bytes(b"bytes stray")
    (tmp_path / "runs" / sid / f"{ts(d(2024, 3, 4))}.json").write_text(
        json.dumps(
            {"snapshot": {"sha256": H("other"), "path": "snapshots/x/elsewhere.zip", "fetched_url": url}}
        )
    )
    outcome = compact_source(Store(tmp_path), sid, medium="local", cutoff=CUTOFF, dry_run=True)
    assert outcome is not None
    # Kept: the newest copy of the artefact (also March's sample) and the newest zip (the stray one,
    # which no record describes and which a builder's `--latest-snapshot` reads). The two older
    # identical copies go; the bytes survive in the kept one.
    assert outcome.reasons == {"delete": 2, "latest": 2}
    assert outcome.deleted == []  # a dry run
    assert outcome.classes == {H("same"): "sampled"}


def test_a_dry_run_deletes_nothing_and_reports_the_same_plan(tmp_path: pathlib.Path) -> None:
    build_daily(tmp_path)
    before = names(tmp_path, "us.test.daily")
    dry = compact_source(Store(tmp_path), "us.test.daily", medium="local", cutoff=CUTOFF, dry_run=True)
    assert dry is not None and dry.deleted == [] and dry.to_delete == 4
    assert names(tmp_path, "us.test.daily") == before
    real = compact_source(Store(tmp_path), "us.test.daily", medium="local", cutoff=CUTOFF, dry_run=False)
    assert real is not None and len(real.deleted) == dry.to_delete and real.classes == dry.classes


def test_the_quarantine_tree_and_undated_files_on_a_local_root(tmp_path: pathlib.Path) -> None:
    build_daily(tmp_path)
    quarantine = tmp_path / "quarantine"
    write_run(quarantine, "us.test.gated", d(2024, 1, 5), sha="q1", promoted=True)
    write_run(quarantine, "us.test.gated", d(2024, 1, 6), sha="q2", promoted=True)
    write_run(quarantine, "us.test.gated", d(2026, 9, 1), sha="q3", promoted=True)
    gleif = tmp_path / "snapshots" / "global.gleif.lei"
    gleif.mkdir(parents=True)
    (gleif / "golden-copy-rr.csv.zip").write_bytes(b"undated")
    outcomes = compact_snapshots(
        tmp_path, cutoff=CUTOFF, dry_run=False, media=[("local", LocalBackend(tmp_path))], source_ids=()
    )
    found = {(o.tree, o.source_id): o for o in outcomes}
    assert set(found) == {
        ("snapshots", "global.gleif.lei"),
        ("snapshots", "us.test.daily"),
        ("quarantine", "us.test.gated"),
    }
    assert found[("snapshots", "global.gleif.lei")].reasons == {"undated": 1}
    assert found[("quarantine", "us.test.gated")].reasons == {"delete": 1, "sample": 1, "young": 1}
    assert names(quarantine, "us.test.gated") == [f"{ts(d(2024, 1, 6))}.csv", f"{ts(d(2026, 9, 1))}.csv"]
    assert QuarantineStore(tmp_path).last_snapshot_sha("us.test.gated") == H("q3")


class _NoSuchKey(Exception):
    """Shaped like botocore's `ClientError` for a missing key: the backend reads only `.response`."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.response = {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}}


class _FakeS3Client:
    """The five client calls `S3Backend` makes, in memory (objects only; no directories, as in S3)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail_list = False
        self.fail_delete = False

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str | None = None) -> None:  # noqa: N803
        self.objects[Key] = bytes(Body)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise _NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise _NoSuchKey(Key)
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        if self.fail_delete:
            raise ConnectionError("endpoint unreachable")
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def get_paginator(self, name: str) -> _FakeS3Client:
        return self

    def paginate(self, *, Bucket: str, Prefix: str, Delimiter: str | None = None) -> Any:  # noqa: N803
        if self.fail_list:
            raise ConnectionError("endpoint unreachable")
        keys = sorted(k for k in self.objects if k.startswith(Prefix) and "/" not in k[len(Prefix) :])
        yield {"Contents": [{"Key": k} for k in keys]}


def _bucket_run(backend: S3Backend, source_id: str, at: dt.datetime, sha: str, status: str = "ok") -> None:
    key = f"snapshots/{source_id}/{ts(at)}.csv"
    backend.put(key, f"bytes {sha}".encode())
    record = {
        "id": f"run-{ts(at)}",
        "status": status,
        "snapshot": {"sha256": sha, "object_key": backend.uri(key)},
    }
    backend.put(f"runs/{source_id}/{ts(at)}.json", json.dumps(record).encode())


def test_a_bucket_is_compacted_through_the_same_backend_calls(tmp_path: pathlib.Path) -> None:
    client = _FakeS3Client()
    backend = S3Backend("infraque-test", client=client)
    for day in (1, 2, 3):
        _bucket_run(backend, "us.test.s3", d(2024, 5, day), f"may{day}")
    _bucket_run(backend, "us.test.s3", d(2026, 10, 1), "now")
    _bucket_run(backend, "us.test.unlisted", d(2024, 5, 1), "u1")
    _bucket_run(backend, "us.test.unlisted", d(2024, 5, 2), "u2")
    backend.put("runs/us.test.s3/20240503T000000Z.json", b"{ truncated")  # unreadable: skipped
    backend.put("runs/us.test.s3/README.txt", b"not a run record")
    outcomes = compact_snapshots(
        tmp_path, cutoff=CUTOFF, dry_run=False, media=[("s3", backend)], source_ids=["us.test.s3"]
    )
    assert [(o.medium, o.source_id, o.reasons) for o in outcomes] == [
        ("s3", "us.test.s3", {"delete": 2, "sample": 1, "young": 1})
    ]
    assert sorted(client.deleted) == [
        f"data/snapshots/us.test.s3/{ts(d(2024, 5, 1))}.csv",
        f"data/snapshots/us.test.s3/{ts(d(2024, 5, 2))}.csv",
    ]
    # A prefix under an id the job was not given is not visited: kept, not guessed at.
    assert f"data/snapshots/us.test.unlisted/{ts(d(2024, 5, 1))}.csv" in client.objects


def test_an_unreadable_bucket_deletes_nothing_and_reports_the_source(tmp_path: pathlib.Path) -> None:
    client = _FakeS3Client()
    backend = S3Backend("infraque-test", client=client)
    for day in (1, 2):
        _bucket_run(backend, "us.test.s3", d(2024, 5, day), f"may{day}")
    client.fail_list = True
    outcomes = compact_snapshots(
        tmp_path, cutoff=CUTOFF, dry_run=False, media=[("s3", backend)], source_ids=["us.test.s3"]
    )
    assert len(outcomes) == 2  # one per tree, each refused before any delete
    assert all(o.errors and not o.deleted for o in outcomes)
    assert client.deleted == []


# ------------------------------------------------------------------------------- the store
@pytest.fixture()
def factory() -> Any:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


def _seed(factory: Any) -> None:
    with session_scope(factory) as db:
        lic = Licence(id="test-open", name="Test", reuse_class="open")
        db.add(lic)
        db.flush()
        src = Source(
            id="us.test.daily",
            name="Test",
            category="generation_queue",
            url="https://example.org",
            access="bulk_file",
            cadence="daily",
            licence_id=lic.id,
            manifest_version="2026-09-12",
            manifest_hash="0" * 64,
        )
        db.add(src)
        db.flush()
        run = SourceRun(source_id=src.id, started_at=d(2024, 8, 1), status="ok")
        db.add(run)
        db.flush()
        for label in ("aug01", "aug02", "oct15", "never-stored"):
            db.add(
                Snapshot(
                    source_id=src.id,
                    source_run_id=run.id,
                    object_key=f"snapshots/{src.id}/{label}.csv",
                    sha256=H(label),
                    byte_size=1,
                    content_type="text/csv",
                    fetched_url="https://example.org",
                    http_status=200,
                    retrieved_at=d(2024, 8, 1),
                    licence_id=lic.id,
                )
            )
        account = Account(public_id="acct_1", name="A")
        db.add(account)
        db.flush()
        user = User(public_id="usr_1", account_id=account.id, email="ana@example.com")
        db.add(user)
        db.flush()
        search = SavedSearch(
            public_id="ss_1", user_id=user.id, account_id=account.id, name="S", query_hash="h"
        )
        db.add(search)
        db.flush()
        sessions = {
            "31d": NOW - dt.timedelta(days=31),
            "30d+1m": NOW - dt.timedelta(days=30, minutes=1),
            "29d": NOW - dt.timedelta(days=29),
        }
        for label, created in sessions.items():
            db.add(
                UserSession(
                    user_id=user.id,
                    token_hash=label.ljust(64, "x"),
                    created_at=created,
                    expires_at=created + dt.timedelta(days=30),
                )
            )
        # An old row still valid (a lifetime this job must not shorten): kept and counted.
        db.add(
            UserSession(
                user_id=user.id,
                token_hash="live".ljust(64, "x"),
                created_at=NOW - dt.timedelta(days=40),
                expires_at=NOW + dt.timedelta(days=1),
            )
        )
        alerts = {"13m": d(2025, 9, 9), "12m+1d": NOW - dt.timedelta(days=366), "11m": d(2025, 11, 10)}
        for label, created in alerts.items():
            db.add(
                Alert(
                    public_id=f"alr_{label}",
                    saved_search_id=search.id,
                    user_id=user.id,
                    window_start=created,
                    window_end=created,
                    recipient="ana@example.com",
                    status="sent",
                    unsubscribe_token=f"ut_{label}",
                    created_at=created,
                )
            )


def _state(factory: Any) -> dict[str, Any]:
    with session_scope(factory) as db:
        return {
            "sessions": sorted(s.token_hash.rstrip("x") for s in db.scalars(select(UserSession))),
            "alerts": {a.public_id: a.recipient for a in db.scalars(select(Alert))},
            "snapshots": {
                s.object_key.rsplit("/", 1)[-1]: s.retention_class for s in db.scalars(select(Snapshot))
            },
            "events": db.scalar(select(func.count()).select_from(Event)),
        }


def test_each_store_rule_changes_exactly_what_is_past_its_age(factory: Any, tmp_path: pathlib.Path) -> None:
    _seed(factory)
    build_daily(tmp_path)
    report = retention.run_retention(factory, now=NOW, data_root=tmp_path, source_ids=())
    state = _state(factory)
    # 30 days and one minute is past the age; 29 days is not; the still-valid 40-day row is kept.
    assert state["sessions"] == ["29d", "live"]
    assert report["rules"]["sessions"] | {"rule": None, "cutoff": None} == {
        "status": "applied",
        "rule": None,
        "cutoff": None,
        "action": "delete the row",
        "matched": 2,
        "changed": 2,
        "kept_unexpired": 1,
    }
    # Alerts older than 12 calendar months lose the address; the rows stay.
    assert state["alerts"] == {"alr_13m": None, "alr_12m+1d": None, "alr_11m": "ana@example.com"}
    assert (report["rules"]["alerts"]["matched"], report["rules"]["alerts"]["changed"]) == (2, 2)
    # Snapshot rows are never deleted; their class follows the object.
    assert state["snapshots"] == {
        "aug01.csv": "expired",
        "aug02.csv": "sampled",
        "oct15.csv": "full",
        "never-stored.csv": "full",  # no run record names its bytes: left as it is
    }
    assert report["rules"]["snapshot_rows"]["by_class"] == {"expired": 1, "sampled": 1}
    raw = report["rules"]["raw_snapshots"]
    assert (raw["matched"], raw["changed"], raw["errors"]) == (4, 4, [])
    assert raw["kept"] == {"sample": 3, "young": 3}
    assert {name for name, rule in report["rules"].items() if rule["status"] == "skipped"} == {
        "model_call_prompts",
        "documents",
        "withdrawn_personal_fields",
    }
    assert report["errors"] == 0 and report["event_id"].startswith("evt_")


def test_a_dry_run_changes_nothing_and_writes_no_run_log(factory: Any, tmp_path: pathlib.Path) -> None:
    _seed(factory)
    build_daily(tmp_path)
    before, files_before = _state(factory), names(tmp_path, "us.test.daily")
    report = retention.run_retention(factory, now=NOW, data_root=tmp_path, dry_run=True, source_ids=())
    assert _state(factory) == before and names(tmp_path, "us.test.daily") == files_before
    assert "event_id" not in report and report["dry_run"] is True
    matched = {
        name: rule.get("matched") for name, rule in report["rules"].items() if rule["status"] == "applied"
    }
    changed = {
        name: rule.get("changed") for name, rule in report["rules"].items() if rule["status"] == "applied"
    }
    assert matched == {"sessions": 2, "alerts": 2, "raw_snapshots": 4, "snapshot_rows": 2}
    assert set(changed.values()) == {0}
    # The real run then does exactly what the dry run said.
    real = retention.run_retention(factory, now=NOW, data_root=tmp_path, source_ids=())
    assert {name: real["rules"][name]["changed"] for name in matched} == matched


def test_one_run_log_row_per_run_never_public(factory: Any, tmp_path: pathlib.Path) -> None:
    _seed(factory)
    first = retention.run_retention(factory, now=NOW, data_root=tmp_path, source_ids=())
    second = retention.run_retention(
        factory, now=NOW + dt.timedelta(days=1), data_root=tmp_path, source_ids=()
    )
    with session_scope(factory) as db:
        rows = retention.latest_runs(db, limit=5)
        assert [r.after["run_id"] for r in rows if r.after] == [second["run_id"], first["run_id"]]
        latest = rows[0]
        assert (latest.subject_type, latest.event_type, latest.actor_type) == (
            "source",
            "retention_run",
            "system",
        )
        assert latest.published_at is None and latest.public_at is None
        # A day later the 40-day row kept back yesterday has reached its own expiry and goes; the
        # two the first run deleted are not counted again.
        assert latest.after is not None and latest.after["rules"]["sessions"]["changed"] == 1
    assert (first["rules"]["sessions"]["changed"], first["rules"]["sessions"]["kept_unexpired"]) == (2, 1)


def test_the_command_line_dry_run(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "store.db"
    engine = get_engine(f"sqlite+pysqlite:///{db_path}")
    init_db(engine)
    data_root = tmp_path / "data"
    build_daily(data_root)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.delenv("SNAPSHOT_STORE", raising=False)
    code = retention.main(["--dry-run", "--now", "2026-10-10T01:47:00Z", "--data-root", str(data_root)])
    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert summary["raw_snapshots_matched"] == 4 and summary["raw_snapshots_changed"] == 0
    assert summary["model_call_prompts_skipped"] is True
    assert len(names(data_root, "us.test.daily")) == 10
    assert retention.main(["--latest", "3"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_a_bucket_is_enumerated_from_the_registry_and_the_store(factory: Any, tmp_path: pathlib.Path) -> None:
    """A bucket has no directory listing: with no ids given, the job visits every registry id and
    every `source` row."""
    _seed(factory)  # a `source` row `us.test.daily`, which is in no registry
    client = _FakeS3Client()
    backend = S3Backend("infraque-test", client=client)
    for day in (1, 2):
        _bucket_run(backend, "us.test.daily", d(2024, 5, day), f"db{day}")
        _bucket_run(backend, "us.iso.caiso.gen_queue", d(2024, 5, day), f"reg{day}")
    with session_scope(factory) as db:
        report = retention.apply_retention(db, now=NOW, data_root=tmp_path, media=[("s3", backend)])
    raw = report["rules"]["raw_snapshots"]
    assert raw["media"] == ["s3"] and (raw["matched"], raw["changed"]) == (2, 2)
    assert set(raw["by_source"]) == {"s3:snapshots/us.test.daily", "s3:snapshots/us.iso.caiso.gen_queue"}


def test_a_failed_delete_is_reported_and_the_run_log_still_written(
    factory: Any, tmp_path: pathlib.Path
) -> None:
    client = _FakeS3Client()
    backend = S3Backend("infraque-test", client=client)
    for day in (1, 2, 3):
        _bucket_run(backend, "us.test.s3", d(2024, 5, day), f"may{day}")
    client.fail_delete = True
    report = retention.run_retention(
        factory, now=NOW, data_root=tmp_path, media=[("s3", backend)], source_ids=["us.test.s3"]
    )
    raw = report["rules"]["raw_snapshots"]
    assert (raw["matched"], raw["changed"], report["errors"]) == (2, 0, 2)
    assert all(e.endswith("StoreWriteError") for e in raw["errors"])
    assert len(client.objects) == 6  # three snapshots and three run records, all still there
    with session_scope(factory) as db:
        assert [e.after["errors"] for e in retention.latest_runs(db) if e.after] == [2]
