"""The connector store's contract, run against both backends (docs/20 §2, §3.2, §12; docs/60 §5).

`local` is a tmp directory; `s3` is `S3Backend` over `FakeS3`, an in-memory stand-in for the four
boto3 client calls the backend makes (put/get/head/delete + the `list_objects_v2` paginator) with
S3's error shapes, `If-None-Match` semantics and pagination. No network, no credentials, no boto3.

`live` runs the same cases against a real S3-compatible server through a real boto3 client, and is
skipped unless `INFRAQUE_TEST_S3_ENDPOINT` (+ `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`) is set.
Measured once on 2026-09-26 against RustFS 1.0.0 in a local container: all live cases passed
(docs/60 §11 item 9). A real R2 bucket has not been exercised.

The cross-host case is modelled literally: the fetch writes through one `Store` instance and the
load reads through a *fresh* instance over the same medium, sharing nothing in memory.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import pathlib
import subprocess
import sys
import uuid
from collections.abc import Iterator
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import func, select

from conftest import ROOT, snapshot
from pipeline.connectors.objectstore import (
    ImmutableObjectExists,
    LocalBackend,
    S3Backend,
    StoreConfigError,
    StoreReadError,
    StoreWriteError,
    backend_from_env,
    store_mode,
)
from pipeline.connectors.runner import run
from pipeline.connectors.store import QuarantineStore, Store, open_store

SOURCE_ID = "us.iso.ercot.gen_queue"
URL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1269363208"
DAY1 = dt.datetime(2026, 9, 12, 6, 0, tzinfo=dt.UTC)
DAY2 = dt.datetime(2026, 9, 13, 6, 0, tzinfo=dt.UTC)
BUCKET = "infraque-test"


# ------------------------------------------------------------------------------- the fake
class FakeClientError(Exception):
    """Shaped like botocore's `ClientError`: the backend reads only `.response`."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(f"{code} ({status})")
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeS3:
    """In-memory S3. `fail_put`/`fail_get` make a call raise when its key contains the substring
    (an unreachable endpoint looks like this to boto3 after its retries: a plain exception)."""

    def __init__(self, *, honour_if_none_match: bool = True, page_size: int = 2) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.honour_if_none_match = honour_if_none_match
        self.page_size = page_size
        self.fail_put: str | None = None
        self.fail_get: str | None = None
        self.hide_from_head: set[str] = set()  # simulate a writer racing between HEAD and PUT
        self.calls: list[str] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str | None = None) -> None:  # noqa: N803
        self.calls.append(f"put {Key}")
        if self.fail_put is not None and self.fail_put in Key:
            raise ConnectionError(f"could not connect to the endpoint for {Key}")
        if IfNoneMatch == "*" and self.honour_if_none_match and (Bucket, Key) in self.objects:
            raise FakeClientError("PreconditionFailed", 412)
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.calls.append(f"get {Key}")
        if self.fail_get is not None and self.fail_get in Key:
            raise ConnectionError("read timed out")
        if (Bucket, Key) not in self.objects:
            raise FakeClientError("NoSuchKey", 404)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.calls.append(f"head {Key}")
        if (Bucket, Key) not in self.objects or Key in self.hide_from_head:
            raise FakeClientError("404", 404)
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        self.calls.append(f"delete {Key}")
        self.objects.pop((Bucket, Key), None)

    def get_paginator(self, name: str) -> FakeS3:
        assert name == "list_objects_v2"
        return self

    def paginate(
        self,
        *,
        Bucket: str,  # noqa: N803
        Prefix: str,  # noqa: N803
        Delimiter: str | None = None,  # noqa: N803
    ) -> Iterator[dict[str, Any]]:
        self.calls.append(f"list {Prefix}")
        keys = sorted(
            k
            for (b, k) in self.objects
            if b == Bucket and k.startswith(Prefix) and not (Delimiter and Delimiter in k[len(Prefix) :])
        )
        if not keys:
            yield {"KeyCount": 0}
            return
        for i in range(0, len(keys), self.page_size):
            yield {"Contents": [{"Key": k} for k in keys[i : i + self.page_size]]}

    def keys(self) -> list[str]:
        return sorted(k for (_, k) in self.objects)


# --------------------------------------------------------------------------- fixtures
#: Set these to also run the contract against a real S3-compatible server (MinIO, R2): the
#: `live` cases then build a real boto3 client and write under a fresh per-test prefix.
LIVE_ENDPOINT = os.environ.get("INFRAQUE_TEST_S3_ENDPOINT", "")
LIVE_BUCKET = os.environ.get("INFRAQUE_TEST_S3_BUCKET", "infraque-store-test")
LIVE_SKIP = pytest.mark.skipif(
    not LIVE_ENDPOINT, reason="set INFRAQUE_TEST_S3_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY"
)


class Medium:
    """One shared medium (a directory, a fake bucket, a live bucket prefix). `new_store()` opens
    a *fresh* Store over it, as a process on another host would."""

    def __init__(self, kind: str, tmp_path: pathlib.Path) -> None:
        self.kind = kind
        self.root = tmp_path / ("data" if kind == "local" else "s3-mode-root")  # stays empty for s3
        self.fake = FakeS3()
        self.bucket, self.prefix, self.client = BUCKET, "data/", self.fake
        if kind == "live":
            from pipeline.connectors.objectstore import build_s3_client

            self.client = build_s3_client(
                LIVE_ENDPOINT, os.environ["R2_ACCESS_KEY_ID"], os.environ["R2_SECRET_ACCESS_KEY"]
            )
            self.bucket, self.prefix = LIVE_BUCKET, f"test-{uuid.uuid4().hex[:12]}/data/"
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except Exception as exc:  # already there from an earlier test
                if "BucketAlready" not in type(exc).__name__ + str(exc):
                    raise

    def backend(self) -> S3Backend:
        return S3Backend(self.bucket, client=self.client, prefix=self.prefix)

    def new_store(self) -> Store:
        return Store(self.root) if self.kind == "local" else Store(self.root, backend=self.backend())

    def keys(self) -> list[str]:
        if self.kind == "local":
            return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        found: list[str] = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=self.prefix
        ):
            found.extend(str(o["Key"])[len(self.prefix) :] for o in page.get("Contents") or [])
        return sorted(found)

    def raw_put(self, key: str, data: bytes) -> None:
        """Write behind the store's back (a corrupt record, a stray file)."""
        if self.kind == "local":
            (self.root / key).parent.mkdir(parents=True, exist_ok=True)
            (self.root / key).write_bytes(data)
        else:
            self.client.put_object(Bucket=self.bucket, Key=self.prefix + key, Body=data)


@pytest.fixture(params=["local", "s3", pytest.param("live", marks=LIVE_SKIP)])
def medium(request: pytest.FixtureRequest, tmp_path: pathlib.Path) -> Medium:
    return Medium(request.param, tmp_path)


@pytest.fixture(params=["s3", pytest.param("live", marks=LIVE_SKIP)])
def bucket_medium(request: pytest.FixtureRequest, tmp_path: pathlib.Path) -> Medium:
    return Medium(request.param, tmp_path)


def frame(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame(
        {"record_id": [f"r{i}" for i in range(n)], "capacity_mw": [float(i) for i in range(n)]}
    )


# ------------------------------------------------------------------------ the contract
def test_every_object_kind_round_trips_under_keys_that_mirror_the_local_tree(medium) -> None:
    new_store = medium.new_store
    st = new_store()
    st.write_snapshot("a.b", "20260912T060000Z", "xlsx", b"raw-bytes")
    st.write_parquet(st.normalized_path("a.b", "20260912T060000Z"), frame())
    st.write_parquet(st.events_path("a.b", "20260912T060000Z"), frame(1))
    st.write_parquet(st.held_path("a.b", "20260912T060000Z"), frame(2))
    st.write_run("a.b", "20260912T060000Z", {"id": "r1", "status": "ok"})

    assert medium.keys() == [
        "events/a.b/20260912T060000Z.parquet",
        "held/a.b/20260912T060000Z.parquet",
        "normalized/a.b/20260912T060000Z.parquet",
        "runs/a.b/20260912T060000Z.json",
        "snapshots/a.b/20260912T060000Z.xlsx",
    ]
    fresh = new_store()
    assert fresh.read_bytes(fresh.snapshot_path("a.b", "20260912T060000Z", "xlsx")) == b"raw-bytes"
    pd.testing.assert_frame_equal(
        fresh.read_parquet(fresh.normalized_path("a.b", "20260912T060000Z")), frame(), check_dtype=False
    )
    assert len(fresh.read_parquet(fresh.held_path("a.b", "20260912T060000Z"))) == 2
    assert fresh.read_json(fresh.run_path("a.b", "20260912T060000Z")) == {"id": "r1", "status": "ok"}
    assert fresh.exists(fresh.events_path("a.b", "20260912T060000Z"))
    assert not fresh.exists(fresh.events_path("a.b", "20260101T000000Z"))
    with pytest.raises(FileNotFoundError):
        fresh.read_bytes(fresh.events_path("a.b", "20260101T000000Z"))


def test_locate_names_the_file_or_the_bucket_object(medium) -> None:
    kind, new_store = medium.kind, medium.new_store
    st = new_store()
    where = st.locate(st.run_path("a.b", "t"))
    if kind == "local":
        assert where == str(st.run_path("a.b", "t"))
    else:
        assert where == f"s3://{medium.bucket}/{medium.prefix}runs/a.b/t.json"


def test_s3_mode_writes_nothing_to_the_local_filesystem(tmp_path: pathlib.Path) -> None:
    fake = FakeS3()
    root = tmp_path / "unwritable-in-a-container"
    st = Store(root, backend=S3Backend(BUCKET, client=fake))
    st.write_snapshot("a.b", "t", "csv", b"x")
    st.write_parquet(st.normalized_path("a.b", "t"), frame())
    st.write_run("a.b", "t", {"status": "ok"})
    assert not root.exists()
    assert len(fake.keys()) == 3


def test_snapshots_are_immutable_but_an_identical_replay_is_a_no_op(medium) -> None:
    new_store = medium.new_store
    st = new_store()
    first = st.write_snapshot("a.b", "t", "csv", b"one")
    assert st.write_snapshot("a.b", "t", "csv", b"one") == first  # identical bytes: accepted
    with pytest.raises(ImmutableObjectExists):
        new_store().write_snapshot("a.b", "t", "csv", b"two")
    assert new_store().read_bytes(first) == b"one"


def test_non_snapshot_objects_may_be_rewritten(medium) -> None:
    new_store = medium.new_store
    st = new_store()
    path = st.normalized_path("a.b", "t")
    st.write_parquet(path, frame(3))
    st.write_parquet(path, frame(1))
    assert len(new_store().read_parquet(path)) == 1
    st.write_run("a.b", "t", {"status": "running"})
    st.write_run("a.b", "t", {"status": "ok"})
    assert [r["status"] for r in st.runs("a.b")] == ["ok"]  # the cache was evicted on rewrite


def test_runs_are_listed_oldest_first_and_bad_records_are_skipped(medium) -> None:
    new_store = medium.new_store
    st = new_store()
    for ts in ("20260914T000000Z", "20260912T000000Z", "20260913T000000Z", "20260911T000000Z"):
        st.write_run("a.b", ts, {"id": ts, "status": "ok"})
    st.write_run("other.source", "20260910T000000Z", {"id": "x", "status": "ok"})
    # A corrupt record and a non-JSON neighbour, written behind the store's back.
    medium.raw_put("runs/a.b/20260915T000000Z.json", b"{not json")
    medium.raw_put("runs/a.b/notes.txt", b"ignore me")
    ids = [r["id"] for r in new_store().runs("a.b")]
    assert ids == ["20260911T000000Z", "20260912T000000Z", "20260913T000000Z", "20260914T000000Z"]
    assert new_store().runs("missing.source") == []


def test_previous_normalized_is_the_latest_ok_run_and_resolves_on_a_fresh_instance(medium) -> None:
    new_store = medium.new_store
    st = new_store()
    for ts, status, n in (
        ("20260911T000000Z", "ok", 1),
        ("20260912T000000Z", "ok", 2),
        ("20260913T000000Z", "partial", 3),
    ):
        path = st.normalized_path("a.b", ts) if status == "ok" else st.held_path("a.b", ts)
        st.write_parquet(path, frame(n))
        st.write_run(
            "a.b", ts, {"id": ts, "status": status, "outputs": {"normalized": "/elsewhere/host-a/x.parquet"}}
        )
    # An ok run whose output never landed (a crash between the two writes) is skipped.
    st.write_run(
        "a.b", "20260914T000000Z", {"id": "orphan", "status": "ok", "outputs": {"normalized": "gone"}}
    )
    df, run_id = new_store().previous_normalized("a.b")
    assert run_id == "20260912T000000Z" and df is not None and len(df) == 2
    assert new_store().previous_normalized("nothing.here") == (None, None)


def test_the_quarantine_tree_keeps_its_prefix_and_cannot_write_publishable_keys(medium) -> None:
    new_store = medium.new_store
    base = new_store()
    q = QuarantineStore(base.base_root, backend=base.backend)
    q.write_parquet(q.normalized_path("x.y", "t"), frame())
    assert medium.keys() == ["quarantine/normalized/x.y/t.parquet"]
    with pytest.raises(RuntimeError):
        q.write_parquet(base.normalized_path("x.y", "t"), frame())
    assert not base.exists(base.normalized_path("x.y", "t"))


def test_delete_removes_outputs_but_never_a_snapshot(medium) -> None:
    new_store = medium.new_store
    st = new_store()
    snap = st.write_snapshot("a.b", "t", "csv", b"x")
    out = st.write_parquet(st.events_path("a.b", "t"), frame())
    st.delete(out)
    st.delete(out)  # idempotent
    assert not st.exists(out)
    with pytest.raises(RuntimeError, match="immutable"):
        st.delete(snap)
    assert st.exists(snap)


# ------------------------------------------------------------- runner: fail closed, cross host
def _run(st: Store, registry, when: dt.datetime = DAY1):
    return run(
        SOURCE_ID, registry=registry, store=st, raw=snapshot("ercot_gis_report.xlsx", URL, retrieved_at=when)
    )


def test_a_run_writes_its_record_last(registry, tmp_path: pathlib.Path) -> None:
    medium = Medium("s3", tmp_path)  # write order is observed through the fake client's call log
    result = _run(medium.new_store(), registry)
    assert result.status == "ok"
    puts = [c.split(" ", 1)[1] for c in medium.fake.calls if c.startswith("put ")]
    assert puts[-1].startswith("data/runs/") and len(puts) == 3
    assert (
        result.run["snapshot"]["object_key"]
        == f"s3://{BUCKET}/data/snapshots/{SOURCE_ID}/20260912T060000Z.xlsx"
    )
    assert result.run["outputs"]["normalized"].startswith(f"s3://{BUCKET}/data/normalized/")


@pytest.mark.parametrize("failing", ["normalized/", "events/"])
def test_an_output_write_failure_fails_the_run_and_leaves_no_partial_result(registry, failing) -> None:
    fake = FakeS3()
    st = Store(pathlib.Path("/nonexistent/data"), backend=S3Backend(BUCKET, client=fake))
    assert _run(st, registry, DAY1).status == "ok"
    # Day 2 re-serves the same payload; make it count as new (forget day 1's hash) and make it
    # emit events for the `events/` case (trim day 1's frame so 20 rows diff as `new`).
    day1_run = st.read_json(st.run_path(SOURCE_ID, "20260912T060000Z"))
    day1_run["snapshot"]["sha256"] = "0" * 64
    st.write_run(SOURCE_ID, "20260912T060000Z", day1_run)
    day1_norm = st.normalized_path(SOURCE_ID, "20260912T060000Z")
    st.write_parquet(day1_norm, st.read_parquet(day1_norm).iloc[:5])
    fake.fail_put = failing
    result = _run(st, registry, DAY2)
    assert result.status == "failed" and result.run["error_class"] == "StoreWriteError"
    assert result.run["outputs"] == {}
    day2 = [k for k in fake.keys() if "20260913T060000Z" in k]
    # The snapshot (evidence) and the failed run record stay; no normalised or events object does.
    assert day2 == [
        f"data/runs/{SOURCE_ID}/20260913T060000Z.json",
        f"data/snapshots/{SOURCE_ID}/20260913T060000Z.xlsx",
    ]
    # The next run still diffs against day 1, the last committed result.
    df, run_id = Store(
        pathlib.Path("/nonexistent/data"), backend=S3Backend(BUCKET, client=fake)
    ).previous_normalized(SOURCE_ID)
    assert run_id == _first_run_id(fake) and df is not None


def _first_run_id(fake: FakeS3) -> str:
    return json.loads(fake.objects[(BUCKET, f"data/runs/{SOURCE_ID}/20260912T060000Z.json")])["id"]


def test_a_snapshot_write_failure_fails_the_run_before_any_output(registry) -> None:
    fake = FakeS3()
    fake.fail_put = "snapshots/"
    st = Store(pathlib.Path("/nonexistent/data"), backend=S3Backend(BUCKET, client=fake))
    result = _run(st, registry)
    assert result.status == "failed" and result.run["error_class"] == "StoreWriteError"
    assert fake.keys() == [f"data/runs/{SOURCE_ID}/20260912T060000Z.json"]
    assert result.run["rows_seen"] == 0  # nothing was parsed past the failed snapshot


def test_when_even_the_run_record_cannot_be_written_the_run_raises(registry) -> None:
    fake = FakeS3()
    fake.fail_put = "data/"  # the whole bucket is unreachable
    st = Store(pathlib.Path("/nonexistent/data"), backend=S3Backend(BUCKET, client=fake))
    with pytest.raises(StoreWriteError):
        _run(st, registry)
    assert fake.keys() == []  # no run record -> the scheduler sees a crash, no load is deferred


def test_a_local_write_failure_is_a_store_write_error_with_no_temp_files(
    tmp_path: pathlib.Path, registry
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / "normalized").write_text("a file where a directory must go")
    result = _run(Store(root), registry)
    assert result.status == "failed" and result.run["error_class"] == "StoreWriteError"
    leftovers = [p.name for p in root.rglob("*") if p.name.endswith(".tmp")]
    assert leftovers == []


def test_fetch_on_one_host_and_load_on_another_share_only_the_bucket(
    registry, bucket_medium: Medium, tmp_path: pathlib.Path
) -> None:
    """The production shape (docs/60 §2): `run_connector` on worker A writes, `load_source` on
    worker B reads. Two Store instances, two backend instances, two different local roots; the
    only thing they share is the bucket."""
    from services.db.models import Proposal, SourceRun
    from services.db.session import get_engine, get_sessionmaker, init_db
    from services.ingest.loader import load_from_files

    fetch_store = Store(tmp_path / "host-a", backend=bucket_medium.backend())
    fetched = _run(fetch_store, registry)
    assert fetched.status == "ok"
    ts = fetched.paths["run"].stem

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    load_store = Store(tmp_path / "host-b", backend=bucket_medium.backend())
    with get_sessionmaker(engine)() as session:
        loaded = load_from_files(
            session, SOURCE_ID, ts, data_root=tmp_path / "host-b", registry=registry, store=load_store
        )
        session.commit()
        assert loaded.proposals_created == fetched.run["rows_seen"] == 25
        assert session.scalar(select(func.count()).select_from(Proposal)) == 25
        run_row = session.scalar(select(SourceRun))
        assert run_row is not None and run_row.status == "ok" and run_row.rows_new == 25
    assert not (tmp_path / "host-a").exists() and not (tmp_path / "host-b").exists()

    # A third host re-fetching the same payload sees the first run's record: `unchanged`.
    again = _run(Store(tmp_path / "host-c", backend=bucket_medium.backend()), registry, DAY2)
    assert again.status == "unchanged"
    assert [k.split("/")[0] for k in bucket_medium.keys()] == ["normalized", "runs", "runs", "snapshots"]


@LIVE_SKIP
def test_the_live_server_refuses_a_conditional_put_over_an_existing_key(tmp_path: pathlib.Path) -> None:
    """The atomic half of snapshot immutability (`If-None-Match: *`) is the server's to honour;
    this asks the real server directly, bypassing the backend's HEAD pre-check."""
    medium = Medium("live", tmp_path)
    medium.backend().put("snapshots/a/t.csv", b"one", immutable=True)
    with pytest.raises(Exception) as caught:
        medium.client.put_object(
            Bucket=medium.bucket, Key=medium.prefix + "snapshots/a/t.csv", Body=b"two", IfNoneMatch="*"
        )
    assert caught.value.response["ResponseMetadata"]["HTTPStatusCode"] == 412  # type: ignore[attr-defined]
    assert medium.backend().get("snapshots/a/t.csv") == b"one"


def test_the_loader_raises_file_not_found_with_the_object_location(registry, tmp_path: pathlib.Path) -> None:
    from services.db.session import get_engine, get_sessionmaker, init_db
    from services.ingest.loader import load_from_files

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    st = Store(tmp_path, backend=S3Backend(BUCKET, client=FakeS3()))
    with (
        get_sessionmaker(engine)() as session,
        pytest.raises(FileNotFoundError, match=f"s3://{BUCKET}/data/normalized/"),
    ):
        load_from_files(session, SOURCE_ID, "20260101T000000Z", registry=registry, store=st)


# --------------------------------------------------------------------- S3 backend specifics
def test_immutability_holds_when_the_endpoint_ignores_if_none_match() -> None:
    fake = FakeS3(honour_if_none_match=False)
    backend = S3Backend(BUCKET, client=fake)
    backend.put("snapshots/a/t.csv", b"one", immutable=True)
    with pytest.raises(ImmutableObjectExists):
        backend.put("snapshots/a/t.csv", b"two", immutable=True)  # refused by the HEAD, before a PUT
    assert fake.objects[(BUCKET, "data/snapshots/a/t.csv")] == b"one"


def test_a_writer_racing_between_head_and_put_is_caught_by_the_conditional_put() -> None:
    fake = FakeS3()
    backend = S3Backend(BUCKET, client=fake)
    backend.put("snapshots/a/t.csv", b"one", immutable=True)
    fake.hide_from_head.add("data/snapshots/a/t.csv")
    with pytest.raises(ImmutableObjectExists):
        backend.put("snapshots/a/t.csv", b"two", immutable=True)
    backend.put("snapshots/a/t.csv", b"one", immutable=True)  # same bytes after a 412: accepted
    assert fake.objects[(BUCKET, "data/snapshots/a/t.csv")] == b"one"


def test_listing_follows_pagination_and_does_not_recurse() -> None:
    fake = FakeS3(page_size=2)
    backend = S3Backend(BUCKET, client=fake)
    for name in ("e", "a", "d", "c", "b"):
        backend.put(f"runs/src/{name}.json", b"{}")
    backend.put("runs/src/nested/deeper.json", b"{}")
    assert backend.list("runs/src/") == [f"runs/src/{n}.json" for n in "abcde"]


def test_read_failures_other_than_missing_are_store_read_errors() -> None:
    fake = FakeS3()
    backend = S3Backend(BUCKET, client=fake)
    backend.put("runs/a/t.json", b"{}")
    fake.fail_get = "runs/"
    with pytest.raises(StoreReadError):
        backend.get("runs/a/t.json")

    class Broken(FakeS3):
        def head_object(self, **kw: Any) -> dict[str, Any]:
            raise FakeClientError("AccessDenied", 403)

        def paginate(self, **kw: Any) -> Iterator[dict[str, Any]]:
            raise FakeClientError("AccessDenied", 403)

        def delete_object(self, **kw: Any) -> None:
            raise ConnectionError("down")

    broken = S3Backend(BUCKET, client=Broken())
    with pytest.raises(StoreReadError):
        broken.exists("runs/a/t.json")
    with pytest.raises(StoreReadError):
        broken.list("runs/a/")
    with pytest.raises(StoreWriteError):
        broken.delete("runs/a/t.json")


def test_the_prefix_is_normalised_and_may_be_empty() -> None:
    fake = FakeS3()
    S3Backend(BUCKET, client=fake, prefix="/pipeline/data/").put("runs/a/t.json", b"{}")
    S3Backend(BUCKET, client=fake, prefix="").put("runs/a/u.json", b"{}")
    assert fake.keys() == ["pipeline/data/runs/a/t.json", "runs/a/u.json"]
    assert S3Backend(BUCKET, client=fake, prefix="").uri("k") == f"s3://{BUCKET}/k"


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "a//b", "./a"])
def test_invalid_keys_are_rejected(key: str, tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError):
        LocalBackend(tmp_path).put(key, b"x")
    with pytest.raises(ValueError):
        S3Backend(BUCKET, client=FakeS3()).put(key, b"x")


def test_the_client_is_built_lazily_and_needs_settings() -> None:
    backend = S3Backend(BUCKET)
    with pytest.raises(StoreConfigError):
        _ = backend.client
    with pytest.raises(StoreConfigError):
        S3Backend("")


# ------------------------------------------------------------------------ env selection
R2_ENV = {
    "SNAPSHOT_STORE": "s3",
    "R2_BUCKET": "infraque-staging",
    "R2_ACCOUNT_ID": "acct123",
    "R2_ACCESS_KEY_ID": "key-id",
    "R2_SECRET_ACCESS_KEY": "not-a-real-secret",
}


def test_local_is_the_default_mode(tmp_path: pathlib.Path) -> None:
    assert store_mode({}) == "local" and store_mode({"SNAPSHOT_STORE": " "}) == "local"
    assert isinstance(backend_from_env(tmp_path, {}), LocalBackend)
    assert isinstance(backend_from_env(tmp_path, {"SNAPSHOT_STORE": "LOCAL"}), LocalBackend)


def test_s3_mode_reuses_the_backup_scripts_r2_settings(tmp_path: pathlib.Path) -> None:
    backend = backend_from_env(tmp_path, R2_ENV)
    assert isinstance(backend, S3Backend)
    assert backend.bucket == "infraque-staging"
    assert backend.endpoint_url == "https://acct123.r2.cloudflarestorage.com"  # as backup.sh builds it
    assert backend.prefix == "data/" and backend._client is None  # nothing imported or dialled yet
    minio = backend_from_env(
        tmp_path, {**R2_ENV, "R2_ENDPOINT": "http://127.0.0.1:9000", "SNAPSHOT_STORE_PREFIX": "p"}
    )
    assert (
        isinstance(minio, S3Backend)
        and minio.endpoint_url == "http://127.0.0.1:9000"
        and minio.prefix == "p/"
    )


@pytest.mark.parametrize("drop", ["R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ACCOUNT_ID"])
def test_s3_mode_with_a_missing_setting_fails_closed_instead_of_writing_locally(
    tmp_path: pathlib.Path, drop: str
) -> None:
    env = {k: v for k, v in R2_ENV.items() if k != drop}
    with pytest.raises(StoreConfigError, match=drop):
        backend_from_env(tmp_path, env)


def test_an_unknown_mode_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(StoreConfigError, match="gcs"):
        backend_from_env(tmp_path, {"SNAPSHOT_STORE": "gcs"})


def test_open_store_reads_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    for name in R2_ENV:
        monkeypatch.delenv(name, raising=False)
    assert isinstance(open_store(tmp_path).backend, LocalBackend)
    for name, value in R2_ENV.items():
        monkeypatch.setenv(name, value)
    st = open_store(tmp_path)
    assert isinstance(st.backend, S3Backend) and st.base_root == tmp_path


# ---------------------------------------------------------------- CLI and scheduler wiring
def test_the_cli_result_line_carries_the_run_key_and_the_scheduler_reads_it_from_the_bucket(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: pathlib.Path
) -> None:
    from infra.scheduler import jobs
    from pipeline.connectors import __main__ as cli
    from pipeline.connectors import store as store_module
    from pipeline.connectors.registry import Registry

    bucket = FakeS3()
    monkeypatch.setattr(store_module, "backend_from_env", lambda root: S3Backend(BUCKET, client=bucket))
    original = Registry.instantiate

    def instantiate_with_fixture(self: Registry, source_id: str, **kwargs: Any) -> Any:
        connector = original(self, source_id, **kwargs)
        connector.fetch = lambda: snapshot("ercot_gis_report.xlsx", URL, retrieved_at=DAY1)  # no network
        return connector

    monkeypatch.setattr(Registry, "instantiate", instantiate_with_fixture)
    root_logger = logging.getLogger()
    saved = (root_logger.handlers[:], root_logger.level)
    try:
        assert cli.main(["run", SOURCE_ID, "--data-dir", str(tmp_path / "worker-a")]) == 0
    finally:
        root_logger.handlers[:], _ = saved[0], root_logger.setLevel(saved[1])
    line = jobs.parse_result_line(capsys.readouterr().out)
    assert line is not None and line["store"] == "s3"
    assert line["run_key"] == f"runs/{SOURCE_ID}/20260912T060000Z.json"
    assert not (tmp_path / "worker-a").exists()

    record = jobs._read_run_record(line)
    assert record is not None and record["status"] == "ok" and record["rows_seen"] == 25
    assert jobs._read_run_record({**line, "run_key": "runs/nope/missing.json"}) is None


def test_a_misconfigured_store_makes_the_cli_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.connectors import __main__ as cli

    monkeypatch.setenv("SNAPSHOT_STORE", "s3")
    for name in ("R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ACCOUNT_ID", "R2_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["run", SOURCE_ID]) == 1


def test_load_source_job_hands_the_loader_the_environment_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    from infra.scheduler import jobs
    from services.db.session import get_engine, get_sessionmaker, init_db

    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    monkeypatch.setattr(jobs, "build_session_factory", lambda: get_sessionmaker(engine))
    seen: dict[str, Any] = {}

    def fake_load(session: Any, source_id: str, ts: str, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"proposals_created": 0}

    for name, value in R2_ENV.items():
        monkeypatch.setenv(name, value)
    jobs.load_source_job(SOURCE_ID, "t", _load=fake_load, _data_root=tmp_path)
    assert isinstance(seen["store"].backend, S3Backend) and seen["data_root"] == tmp_path


def test_a_store_write_error_is_retried_by_the_scheduler() -> None:
    from infra.scheduler import jobs

    assert jobs.is_transient({"status": "failed", "error_class": "StoreWriteError"})
    assert not jobs.is_transient({"status": "failed", "error_class": "ImmutableObjectExists"})


def test_local_mode_and_an_unused_s3_backend_never_import_boto3() -> None:
    """boto3 is imported inside `build_s3_client` only, so the default path needs no package,
    credentials or network. Checked in a clean interpreter, since another test may import it."""
    code = (
        "import sys, pathlib\n"
        "from pipeline.connectors.store import open_store\n"
        "from pipeline.connectors.objectstore import S3Backend\n"
        "st = open_store(pathlib.Path('/nonexistent'))\n"
        "S3Backend('b', endpoint_url='http://127.0.0.1:1', access_key_id='k', secret_access_key='s')\n"
        "print(type(st.backend).__name__, 'boto3' in sys.modules, 'botocore' in sys.modules)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "SNAPSHOT_STORE"}
    out = subprocess.run(  # noqa: S603 -- fixed argv
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT, env=env
    ).stdout.split()
    assert out == ["LocalBackend", "False", "False"]
