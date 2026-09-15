"""Fixtures for `services/billing/test_router.py`: a standalone FastAPI app carrying
`services.billing.router.router` plus (read-only import, never mounted by the coordinator's
`services.api.app.app` from here) `services.api.pro.router` — just enough to let one test exercise
`GET /v1/me` after a webhook, per the task brief, without this package ever writing to
`services/api/app.py` (CLAUDE.md "one agent per file area", `services/billing/README.md`
"Deferred"). Fresh SQLite database per test; dependency overrides for `get_db`, `get_billing_port`
and `get_crm_port` mirror `tests/conftest.py`'s pattern for the shared app. `make_account`,
`make_user` and `login` are imported directly from `tests.conftest` (plain functions there, not
pytest fixtures — the same reuse `tests/conftest.py`'s own docstring describes for
`services.api.conftest`'s entity factories).
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.deps import get_db
from services.api.errors import ProblemError, problem_exception_handler
from services.api.pro import router as pro_router
from services.api.ratelimit import default_limiter
from services.billing.fake import InMemoryBilling
from services.billing.router import router as billing_router
from services.crm.fake import InMemoryCrm
from services.db.session import get_engine, get_sessionmaker, init_db
from services.sor.wiring import get_billing_port, get_crm_port

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPO_ROOT / "api" / "openapi.yaml"


@pytest.fixture(scope="session")
def spec() -> dict[str, Any]:
    """The committed `api/openapi.yaml`, for `services/billing/test_router.py`'s admin-list test
    (same jsonschema-against-the-committed-spec approach as `tests/test_api_contract.py`, whose
    `assert_valid` helper this package imports directly rather than duplicating)."""
    with OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """`services.api.ratelimit.default_limiter` is process-wide (its module docstring); this
    package's `/v1/me` test goes through `services.api.pro`'s routes, which meter it — reset for
    the same reason `tests/conftest.py` and `services/api/conftest.py` both do."""
    default_limiter.reset()


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Iterator[Session]:
    with db_sessionmaker() as s:
        yield s


@pytest.fixture()
def billing_port() -> InMemoryBilling:
    return InMemoryBilling()


@pytest.fixture()
def crm_port() -> InMemoryCrm:
    return InMemoryCrm()


@pytest.fixture()
def client(
    db_sessionmaker: sessionmaker[Session], billing_port: InMemoryBilling, crm_port: InMemoryCrm
) -> Iterator[TestClient]:
    app = FastAPI()
    app.add_exception_handler(ProblemError, problem_exception_handler)
    app.include_router(billing_router)
    app.include_router(pro_router)

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_billing_port] = lambda: billing_port
    app.dependency_overrides[get_crm_port] = lambda: crm_port
    with TestClient(app) as c:
        yield c
