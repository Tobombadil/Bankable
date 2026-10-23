"""Upload and download latency of a 10 MB file through the data-room API, against a local object-store
stand-in that speaks the S3 API (`conftest.S3StandIn`), so the real path runs: the multipart parse,
the type check, the seal (AES-256-GCM), the S3 client's PUT and GET, the open and the hash check.

The budgets are generous (a shared test machine); the measured figures are written to stdout and
recorded in docs/19 "Data rooms, as built". Run alone with `-s` to read them.
"""

from __future__ import annotations

import pathlib
import random
import statistics
import sys
import time
import tracemalloc
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app
from services.api.auth import create_session
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.projects.conftest import TEST_DATA_ROOM_KEY, S3StandIn, UserFactory

MIB = 1024 * 1024
SIZE = 10 * MIB
RUNS = 5
#: Seconds, end to end in process. Measured well under one second each (docs/19).
UPLOAD_BUDGET = 8.0
DOWNLOAD_BUDGET = 8.0


@pytest.fixture()
def api(db_sessionmaker: sessionmaker[Session]) -> Iterator[None]:
    def _override() -> Iterator[Session]:
        session = db_sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    default_limiter.reset()
    app.dependency_overrides[get_db] = _override
    yield
    app.dependency_overrides.clear()


def test_a_10_mb_file_uploads_and_downloads_within_budget(
    api: None,
    db: Session,
    make_user: UserFactory,
    s3_standin: S3StandIn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        **s3_standin.env(),
        "DATA_ROOMS": "on",
        "DATA_ROOM_STORE": "s3",
        "DATA_ROOM_KEY": TEST_DATA_ROOM_KEY,
        "MY_PIPELINE": "on",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("DATA_ROOM_SCANNER", raising=False)
    user = make_user()
    _row, cookie = create_session(db, user)
    db.commit()
    # Seeded, so every run uploads the same bytes; not a secret.
    data = b"%PDF-1.7\n" + random.Random(20261023).randbytes(SIZE - 20) + b"\n%%EOF\n"  # noqa: S311
    with TestClient(app, base_url="http://testserver") as tab:
        tab.headers["Origin"] = "http://testserver"
        tab.cookies.set("session", cookie)
        project = tab.post("/v1/my/projects", json={"title": "Latency"}).json()["data"]["public_id"]
        room = tab.get(f"/v1/my/projects/{project}/documents").json()["data"]
        folder = room["folders"][0]["folder_id"]
        uploads, downloads = [], []
        for n in range(RUNS):
            started = time.perf_counter()
            resp = tab.post(
                f"/v1/my/projects/{project}/documents",
                data={"folder_id": folder},
                files={"file": (f"model-{n}.pdf", data, "application/pdf")},
            )
            uploads.append(time.perf_counter() - started)
            assert resp.status_code == 201, resp.text
            download = resp.json()["data"]["download"]
            started = time.perf_counter()
            got = tab.get(download, headers={"Accept-Encoding": "identity"})
            downloads.append(time.perf_counter() - started)
            assert got.status_code == 200 and got.content == data
    # Every object in the stand-in is sealed: never the plaintext, always a little longer.
    assert all(len(body) == len(data) + 33 for body, _at in s3_standin.objects.values())
    sys.stdout.write(
        f"\ndata-room latency, {SIZE // MIB} MiB x {RUNS}: "
        f"upload median {statistics.median(uploads):.3f}s max {max(uploads):.3f}s; "
        f"download median {statistics.median(downloads):.3f}s max {max(downloads):.3f}s\n"
    )
    assert max(uploads) < UPLOAD_BUDGET and max(downloads) < DOWNLOAD_BUDGET


#: One accepted upload just under the default 25 MiB cap, the review's case (2026-10-23), which
#: measured a 96 MiB peak (four copies: the test client's body, the form, the ciphertext and the
#: ciphertext again with its header). The form reader no longer copies the file and the seal writes
#: header, ciphertext and tag into one buffer, so three copies remain, one of them the test client's:
#: 72 MiB measured. The budget is three and a quarter copies.
MEMORY_FILE = 24 * MIB
MEMORY_BUDGET = MEMORY_FILE * 13 // 4


def test_one_upload_holds_a_bounded_number_of_copies_of_the_file(
    api: None,
    db: Session,
    make_user: UserFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    for name, value in {
        "DATA_ROOMS": "on",
        "DATA_ROOM_STORE": "local",
        "DATA_ROOM_LOCAL_DIR": str(tmp_path / "store"),
        "DATA_ROOM_KEY": TEST_DATA_ROOM_KEY,
        "MY_PIPELINE": "on",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("DATA_ROOM_SCANNER", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    user = make_user()
    _row, cookie = create_session(db, user)
    db.commit()
    data = b"%PDF-1.7\n" + random.Random(20261024).randbytes(MEMORY_FILE - 20) + b"\n%%EOF\n"  # noqa: S311
    with TestClient(app, base_url="http://testserver") as tab:
        tab.headers["Origin"] = "http://testserver"
        tab.cookies.set("session", cookie)
        project = tab.post("/v1/my/projects", json={"title": "Memory"}).json()["data"]["public_id"]
        folder = tab.get(f"/v1/my/projects/{project}/documents").json()["data"]["folders"][0]["folder_id"]
        tracemalloc.start()
        try:
            resp = tab.post(
                f"/v1/my/projects/{project}/documents",
                data={"folder_id": folder},
                files={"file": ("model.pdf", data, "application/pdf")},
            )
            _now, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert resp.status_code == 201, resp.text
    sys.stdout.write(f"\ndata-room upload memory, {MEMORY_FILE // MIB} MiB file: peak {peak / MIB:.0f} MiB\n")
    assert peak <= MEMORY_BUDGET
