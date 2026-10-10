"""`GET /v1/health` tells the truth (docs/51 §2.9 item 1, 2026-10-10).

- The database down is HTTP 503 with the same body, so the Compose healthcheck (`urlopen` raises on
  5xx) and `deploy.sh`'s `curl -sf` fail instead of passing on `status: degraded`.
- `queue_age_seconds` is measured from Procrastinate's `todo` rows; the constants that could not be
  known (`rate_limiter_active`, `edge_cache`, `backup_age_hours`) are gone.
- The answer is never cached, so a probe always reaches the origin.
"""

from __future__ import annotations

import contextlib
import pathlib
from collections.abc import Iterator
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from services.api.app import QUEUE_AGE_SQL, app, queue_age_seconds
from services.api.deps import get_db
from services.db.session import get_sessionmaker

ROOT = pathlib.Path(__file__).resolve().parents[2]
REMOVED = ("rate_limiter_active", "edge_cache", "backup_age_hours")


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    return doc


def _valid(spec: dict[str, Any], body: object) -> None:
    schema = spec["components"]["schemas"]["HealthResponse"]
    resolver = jsonschema.validators.RefResolver.from_schema(spec)
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(body), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


@pytest.fixture()
def unreachable_db(tmp_path: pathlib.Path) -> Iterator[TestClient]:
    """A real SQLAlchemy session whose every connection fails, the way a stopped database does."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path}/missing-dir/store.sqlite")
    factory = get_sessionmaker(engine)

    def _override() -> Iterator[Any]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_database_down_is_503_with_the_body(unreachable_db: TestClient, spec: dict[str, Any]) -> None:
    resp = unreachable_db.get("/v1/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] is False
    assert body["checks"]["queue"] is False
    assert body["checks"]["queue_age_seconds"] is None
    # Nothing read from the store is reported as if it had been.
    assert body["source_vintage"] is None and body["source_data_as_of"] is None
    assert body["data_as_of"] is None
    # The parts that need no database still answer, so the web host keeps its configuration.
    assert body["lag_days_default"] == {"supply": 0, "opportunities": 0}
    assert isinstance(body["posture_statement"], str) and body["free_alerts"]["cap"] >= 0
    assert resp.headers["Cache-Control"] == "no-store"
    _valid(spec, body)


def test_a_healthy_store_is_200_and_reports_only_what_it_measured(
    client: TestClient, db: Any, spec: dict[str, Any]
) -> None:
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["checks"]["database"] is True
    for name in REMOVED:
        assert name not in body["checks"], f"{name} is a constant, not a check"
    # SQLite has no job queue: not applicable, so null rather than 0 or a pass.
    assert body["checks"]["queue"] is None and body["checks"]["queue_age_seconds"] is None
    assert resp.headers["Cache-Control"] == "no-store"
    _valid(spec, body)


def test_the_compose_healthcheck_fails_on_the_503(unreachable_db: TestClient) -> None:
    """`infra/compose/docker-compose.yml`'s api healthcheck is `urllib.request.urlopen(...)`, which
    raises `HTTPError` for any 5xx; `deploy.sh` uses `curl -sf`, which exits 22 on it. Both read
    the status code, so the code is what has to change, not the body."""
    compose = (ROOT / "infra" / "compose" / "docker-compose.yml").read_text()
    assert "urllib.request.urlopen('http://localhost:8000/v1/health'" in compose
    assert unreachable_db.get("/v1/health").status_code >= 500


def test_the_spec_documents_the_503_with_the_health_body(spec: dict[str, Any]) -> None:
    responses = spec["paths"]["/v1/health"]["get"]["responses"]
    for code in ("200", "503"):
        schema = responses[code]["content"]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/HealthResponse"}, code
    checks = spec["components"]["schemas"]["HealthResponse"]["properties"]["checks"]["properties"]
    assert not set(REMOVED) & set(checks), sorted(checks)
    assert "queue_age_seconds" in checks


class _Nested:
    def __init__(self, owner: _PgSession) -> None:
        self.owner = owner

    def __enter__(self) -> _Nested:
        self.owner.savepoints += 1
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _PgSession:
    """Just enough of a Postgres `Session` for `queue_age_seconds`: a scripted scalar or error."""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.savepoints = 0
        self.statements: list[str] = []

    def begin_nested(self) -> contextlib.AbstractContextManager[_Nested]:
        return _Nested(self)

    def execute(self, statement: Any) -> Any:
        self.statements.append(str(statement))
        if isinstance(self.answer, Exception):
            raise self.answer
        answer = self.answer

        class _Result:
            def scalar(self) -> object:
                return answer

        return _Result()


def test_queue_age_reads_the_oldest_runnable_todo_job_in_a_savepoint() -> None:
    session = _PgSession(412.7)
    assert queue_age_seconds(session, True) == 412  # type: ignore[arg-type]
    assert session.savepoints == 1, "a failed read must not poison the probe's transaction"
    sql = session.statements[0]
    assert "procrastinate_jobs" in sql and "status = 'todo'" in sql and "ready_at <= now()" in sql
    assert queue_age_seconds(_PgSession(0), True) == 0  # type: ignore[arg-type]  # nothing waiting


@pytest.mark.parametrize(
    ("present", "answer"),
    [(None, 5), (False, 5), (True, RuntimeError("permission denied for table procrastinate_jobs"))],
)
def test_queue_age_is_null_when_it_cannot_be_known(present: bool | None, answer: object) -> None:
    session = _PgSession(answer)
    assert queue_age_seconds(session, present) is None  # type: ignore[arg-type]
    if present is not True:
        assert session.statements == [], "no query without the queue schema"


def test_the_queue_age_query_counts_jobs_waiting_on_a_lock_but_not_ones_scheduled_later() -> None:
    sql = str(QUEUE_AGE_SQL)
    assert "GREATEST(" in sql and "j.scheduled_at" in sql
    assert "'deferred', 'deferred_for_retry', 'retried'" in sql
    assert "lock" not in sql, "a job blocked behind a lock is still waiting"
