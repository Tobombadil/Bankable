"""`GET /v1/coverage` within the list budget (docs/04 E-16: 300 ms p95) on a full-store-sized store.

The site calls it on every page (cached ten minutes there), so a slow cold call is a slow page. It
took 0.4-1.3 s (backend audit 2026-10-07, PERF-9): `data/sources.yaml` was parsed on every call
(~0.3 s) and 24 whole-store aggregates ran every time. The registry is now parsed once per file
version and the statement is cached on the proposal map's data version and write generation
(`services/api/geo_cache.py`). Measured on the dev store copy: first call in a process 1.2 s
(imports and the one parse), cold after a data change 0.24 s median, warm 13 ms."""

from __future__ import annotations

import gc
import statistics
import time
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api import geo_cache
from services.api.app import app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.session import get_engine, get_sessionmaker, init_db
from tests import geo_full_store

try:  # the frontend lane's shared helper; a plain collection where it has not landed yet
    from tests.latency import settle_heap  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on merge order

    def settle_heap() -> None:
        gc.collect()


#: E-16's list budget. Asserted with a margin for an unknown runner, as the geo test's absolute
#: number was; the deterministic checks below are what pin the fix.
BUDGET_S = 0.3
MARGIN = 3.0


@pytest.fixture(scope="module")
def store() -> Iterator[sessionmaker[Session]]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    factory = get_sessionmaker(engine)
    with factory() as session:
        geo_full_store.build(session)
    yield factory
    engine.dispose()
    geo_cache.reset()  # drop this store's cached answers so later modules do not carry them


@pytest.fixture(scope="module")
def client(store: sessionmaker[Session]) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        s = store()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _get(client: TestClient) -> float:
    default_limiter.reset()
    t0 = time.perf_counter()
    resp = client.get("/v1/coverage")
    elapsed = time.perf_counter() - t0
    assert resp.status_code == 200, resp.text[:300]
    return elapsed


def test_a_cold_coverage_call_is_within_the_list_budget(client: TestClient) -> None:
    _get(client)  # imports and the one registry parse per process
    cold = []
    for _ in range(5):
        geo_cache.reset()
        settle_heap()
        cold.append(_get(client))
    median = statistics.median(cold)
    assert median < BUDGET_S * MARGIN, f"cold coverage median {median * 1000:.0f} ms over {cold}"


def test_the_registry_is_not_reparsed_and_a_warm_call_is_one_statement(
    client: TestClient, store: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.connectors import registry as registry_module

    _get(client)
    parses: list[Any] = []
    real_init = registry_module.Registry.__init__

    def counting_init(self: Any, *args: Any, **kwargs: Any) -> None:
        parses.append(1)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(registry_module.Registry, "__init__", counting_init)
    geo_cache.reset()
    _get(client)  # cold: aggregates rerun, registry reused
    assert parses == []

    statements: list[str] = []
    engine = store.kw["bind"]

    def grab(_c: object, _cur: object, statement: str, *_a: object) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", grab)
    try:
        warm = _get(client)
    finally:
        sa.event.remove(engine, "before_cursor_execute", grab)
    assert len(statements) == 1, statements  # the data version only
    assert warm < BUDGET_S


def test_a_write_the_statement_reads_refreshes_it(client: TestClient, store: sessionmaker[Session]) -> None:
    from services.db.models import Organization

    before = client.get("/v1/coverage").json()["data"]["ownership"]["organizations"]
    with store() as s:
        s.add(
            Organization(
                public_id="org_latency",
                slug="latency-co",
                name_canonical="Latency Co",
                name_normalised="latency co",
                type="other",
                country="US",
            )
        )
        s.commit()
    default_limiter.reset()
    after = client.get("/v1/coverage").json()["data"]["ownership"]["organizations"]
    assert after == before + 1
