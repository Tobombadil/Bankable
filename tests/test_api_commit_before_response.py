"""A write is committed before its response is sent (backend audit 2026-09-30 F3, F13).

Before, `services/api/deps.py::get_db` committed in the teardown of a yield dependency, which
FastAPI runs after the response has gone out: a commit that failed (a serialization failure, a
dropped connection, a deferred constraint) still answered 2xx with the id of a row that was never
stored. These tests force the request's commit to fail and require a 5xx problem+json carrying the
request's one id, with nothing persisted. They run through the real `get_db`, not a test override.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from services.api import deps
from services.api.app import app
from services.db.models import SavedSearch, WebhookEndpoint
from services.db.session import get_engine, get_sessionmaker, init_db
from tests.conftest import make_account, make_user

_SIMULATED = "simulated: could not serialize access due to concurrent update"


@pytest.fixture()
def file_sessionmaker(tmp_path: Any) -> sessionmaker[Session]:
    """A file database, so a second connection sees only what has really been committed (the
    shared in-memory fixture pins every session to one connection and would see uncommitted rows)."""
    engine = get_engine(f"sqlite+pysqlite:///{tmp_path / 'commit.db'}")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def db(file_sessionmaker: sessionmaker[Session]) -> Iterator[Session]:
    with file_sessionmaker() as s:
        yield s


class _AtResponseStart:
    """An ASGI wrapper that runs `hook` when the app sends `http.response.start`, i.e. before the
    client has any byte of the response."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.hook: Callable[[], None] | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def wrapped(message: Any) -> None:
            if message["type"] == "http.response.start" and self.hook is not None:
                self.hook()
            await send(message)

        await self.inner(scope, receive, wrapped)


@pytest.fixture()
def wrapper() -> _AtResponseStart:
    return _AtResponseStart(app)


@pytest.fixture()
def real_db_client(
    file_sessionmaker: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, wrapper: _AtResponseStart
) -> Iterator[TestClient]:
    """The app's own `get_db` (no `dependency_overrides`), bound to this test's database."""
    monkeypatch.setattr(deps, "_sessionmaker", file_sessionmaker)
    saved = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    with TestClient(wrapper, raise_server_exceptions=False) as c:  # type: ignore[arg-type]
        yield c
    app.dependency_overrides.update(saved)


@pytest.fixture()
def failing_commit(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """While `state["fail"]` is true, every `Session.commit` raises as a lost serialization would."""
    state = {"fail": False}
    original = Session.commit

    def commit(self: Session) -> None:
        if state["fail"]:
            raise OperationalError("COMMIT", {}, Exception(_SIMULATED))
        original(self)

    monkeypatch.setattr(Session, "commit", commit)
    return state


def _signed_in(client: TestClient, db: Session, entitlement: str = "pro") -> None:
    from services.api.auth import create_session

    account = make_account(db, entitlement=entitlement)
    user = make_user(db, account)
    _row, cookie = create_session(db, user)
    db.commit()
    client.cookies.set("session", cookie)


def _post_saved_search(client: TestClient, name: str) -> Any:
    return client.post("/v1/saved-searches", json={"name": name, "entity": "proposal", "query": {}})


def test_a_failed_commit_answers_5xx_problem_and_persists_nothing(
    real_db_client: TestClient, db: Session, failing_commit: dict[str, bool]
) -> None:
    _signed_in(real_db_client, db)
    failing_commit["fail"] = True
    resp = _post_saved_search(real_db_client, "lost-write")
    failing_commit["fail"] = False

    assert resp.status_code >= 500, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == resp.status_code
    assert body["code"] == "internal_error"
    # F13: the error body and the header carry the same, single request id.
    assert body["request_id"] == resp.headers["X-Request-Id"]
    # Nothing in the body hints at the failure's internals (api/openapi.yaml `Problem`).
    assert _SIMULATED not in resp.text
    db.expire_all()
    assert (
        db.scalar(select(func.count()).select_from(SavedSearch).where(SavedSearch.name == "lost-write")) == 0
    )


def test_a_successful_write_is_visible_to_the_next_request(real_db_client: TestClient, db: Session) -> None:
    _signed_in(real_db_client, db)
    created = _post_saved_search(real_db_client, "kept-write")
    assert created.status_code == 201, created.text
    sid = created.json()["data"]["saved_search_id"]
    assert real_db_client.get(f"/v1/saved-searches/{sid}").status_code == 200


def test_the_commit_happens_before_the_response_starts(
    real_db_client: TestClient,
    db: Session,
    file_sessionmaker: sessionmaker[Session],
    wrapper: _AtResponseStart,
) -> None:
    """The row is durable before the client receives the status line: an independent connection
    counts it at the moment the response starts."""
    _signed_in(real_db_client, db, entitlement="api")
    seen: list[int] = []

    def count_committed() -> None:
        with file_sessionmaker() as other:
            seen.append(other.scalar(select(func.count()).select_from(WebhookEndpoint)) or 0)

    wrapper.hook = count_committed
    resp = real_db_client.post(
        "/v1/webhooks", json={"url": "https://example.com/hook", "types": ["event.published"]}
    )
    wrapper.hook = None
    assert resp.status_code == 201, resp.text
    assert seen == [1]


def test_a_keyed_read_releases_its_write_lock_before_the_body(
    real_db_client: TestClient, db: Session, tmp_path: Any, wrapper: _AtResponseStart
) -> None:
    """Every API-key request writes `api_key.last_used_at` (audit F14). That row lock used to be
    held until after the body had streamed; now it is released when the response starts, so
    another writer is not blocked behind the transfer. SQLite's database lock stands in for the
    Postgres row lock here: a second connection with no busy wait must be able to write."""
    from tests.conftest import make_api_key

    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user)
    db.commit()
    outcome: list[str] = []

    def write_from_another_connection() -> None:
        conn = sqlite3.connect(tmp_path / "commit.db", timeout=0)
        try:
            conn.execute("UPDATE account SET name = 'other writer'")
            conn.commit()
            outcome.append("written")
        except sqlite3.OperationalError as exc:
            outcome.append(str(exc))
        finally:
            conn.close()

    wrapper.hook = write_from_another_connection
    resp = real_db_client.get("/v1/sources", headers={"Authorization": f"Bearer {secret}"})
    wrapper.hook = None
    assert resp.status_code == 200, resp.text
    assert outcome == ["written"]


def test_an_unhandled_error_is_problem_json_with_the_request_id(real_db_client: TestClient) -> None:
    """F13: a 500 is RFC 9457 problem+json, never `text/plain`, and its id matches the header."""

    @app.get("/v1/__test_boom")
    def boom() -> None:
        raise RuntimeError("internal detail that must not leak")

    try:
        resp = real_db_client.get("/v1/__test_boom")
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/v1/__test_boom"]
    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == "internal_error"
    assert body["request_id"] == resp.headers["X-Request-Id"]
    assert "internal detail" not in resp.text


def test_one_request_id_in_header_and_meta(real_db_client: TestClient) -> None:
    """F13: `meta.request_id` and `X-Request-Id` are the same id, and differ between requests."""
    first = real_db_client.get("/v1/sources")
    second = real_db_client.get("/v1/sources")
    assert first.status_code == 200
    assert first.json()["meta"]["request_id"] == first.headers["X-Request-Id"]
    assert second.json()["meta"]["request_id"] == second.headers["X-Request-Id"]
    assert first.headers["X-Request-Id"] != second.headers["X-Request-Id"]


def test_a_problem_body_carries_the_header_request_id(real_db_client: TestClient) -> None:
    resp = real_db_client.get("/v1/proposals/prop_doesnotexist")
    assert resp.status_code == 404
    assert resp.json()["request_id"] == resp.headers["X-Request-Id"]
