"""`GET /v1/proposals/geo` against the one geo latency budget, docs/04 E-16 (`/geo` p95 <= 400 ms; D-13's
API share), on a store the size and shape of the full dev store, in CI.

`tests/test_api_geo_performance.py` covers the same route, but only on EIA-860M (~2,300 rows) and only
where the git-ignored `data/normalized` exists, so CI never ran it. This module builds a synthetic store
of the full dev store's size and mix from committed data (`tests/geo_full_store.py`: 10,652 public
proposals, eight sources, 404 merges and 532 merged-away rows, every placement grade, two derived-only
licences, GB rows) in about 4 s and times the map's real requests.

**Two paths.** Since 2026-10-07 the whole-set part of a map answer is cached per filter set on a data
version (`services/api/geo_cache.py`): a pan is a cache hit, and only the first request per filter set
after a data change does the national work. The *cold* call is timed with the cache cleared before
each call; the *warm* pan must be one SQL statement and well under half the cold call.

**Why a ratio, not 400 ms.** Until 2026-10-07 this asserted the median of three calls under 1.0 s
against a measured ~0.34 s, so a 2x regression passed (backend audit API-1); an absolute number tight
enough to catch one fails on any runner slower than the machine it was set on. The guard here is
relative: each cold call's best of five, divided by the best of five runs of a fixed CPU workload
interleaved with it (`_reference_s`), must stay under `GUARD` times the ratio recorded in
`RECORDED_RATIO`. A slower runner slows both; a change that doubles the call's cost doubles the ratio
and fails. Bests, not medians, because the measuring VM is shared and a burst of load only ever adds
time. The 400 ms itself is checked by the measurement procedure in services/README.md "Geo latency"
on the dev VM, where the cold calls below measured 0.03-0.45 s under load (2026-10-07).
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api import geo_cache
from services.api.app import app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.models import Location, Proposal, ProposalSource
from services.db.session import get_engine, get_sessionmaker, init_db
from tests import geo_full_store
from tests.latency import settle_heap

CONUS_BBOX = "-125,24,-66,50"
TEXAS_BBOX = "-106.65,25.84,-93.51,36.5"
ACTIVE = "announced,filed,studied,permitted,contracted,under_construction"
#: docs/04 E-16: `/geo` p95 <= 400 ms, the one geo latency number (D-13's API share).
BUDGET_S = 0.4
#: Best-of-five cold call / best-of-five `_reference_s()`, per request, on the dev VM (2026-10-07; the
#: middle of three runs, load average 2-4 on 4 cores; services/README.md "Geo latency" has the method).
RECORDED_RATIO = {
    "conus_z3": 6.4,
    "conus_z2": 5.2,
    "conus_z4": 5.2,
    "conus_z6": 5.9,
    "texas_z7": 7.0,
    "default_view_z3": 3.1,
    "withdrawn_included_z3": 3.8,
    "kind_load_z3": 2.3,
    "kind_ccs_z3": 0.5,
}
#: A ratio this many times the recorded one fails: a 2x regression fails unless the run's noise
#: hides more than a fifth of it; measured run-to-run spread of the ratio was about +-25%.
GUARD = 1.6
#: Below this ratio (about 60 ms of call) noise dominates, so a tiny call is held to it instead.
RATIO_FLOOR = 1.0
CALLS = 5

#: The map's own requests (`web/static/js/map.js`): national at zooms 2-6, a state at zoom 7, the
#: default active-only view, withdrawn included, and two kind filters.
REQUESTS = {
    "conus_z3": {"bbox": CONUS_BBOX, "zoom": "3"},
    "conus_z2": {"bbox": CONUS_BBOX, "zoom": "2"},
    "conus_z4": {"bbox": CONUS_BBOX, "zoom": "4"},
    "conus_z6": {"bbox": CONUS_BBOX, "zoom": "6"},
    "texas_z7": {"bbox": TEXAS_BBOX, "zoom": "7"},
    "default_view_z3": {
        "bbox": CONUS_BBOX,
        "zoom": "3",
        "lifecycle_state": ACTIVE,
        "placement": "exact,region",
    },
    "withdrawn_included_z3": {
        "bbox": CONUS_BBOX,
        "zoom": "3",
        "lifecycle_state": f"{ACTIVE},withdrawn,cancelled",
        "placement": "exact,region",
    },
    "kind_load_z3": {"bbox": CONUS_BBOX, "zoom": "3", "kind": "load"},
    "kind_ccs_z3": {"bbox": CONUS_BBOX, "zoom": "3", "kind": "ccs"},
}


@pytest.fixture(scope="module")
def store() -> Iterator[tuple[sessionmaker[Session], dict[str, int]]]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    factory = get_sessionmaker(engine)
    with factory() as session:
        counts = geo_full_store.build(session)
    yield factory, counts
    engine.dispose()
    geo_cache.reset()  # drop this store's cached answers so later modules do not carry them


@pytest.fixture(scope="module")
def client(store: tuple[sessionmaker[Session], dict[str, int]]) -> Iterator[TestClient]:
    factory, _ = store

    def _override() -> Iterator[Session]:
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _get(client: TestClient, params: dict[str, str]) -> tuple[float, dict[str, object]]:
    default_limiter.reset()  # 13+ anonymous calls from one address would pass the hourly bucket
    settle_heap()
    t0 = time.perf_counter()
    resp = client.get("/v1/proposals/geo", params=params)
    elapsed = time.perf_counter() - t0
    assert resp.status_code == 200, resp.text[:500]
    return elapsed, resp.json()


def test_the_store_has_the_full_dev_stores_size_and_mix(
    store: tuple[sessionmaker[Session], dict[str, int]],
) -> None:
    """Guard against the store being shrunk until the budget passes trivially."""
    factory, counts = store
    assert counts["public"] >= 10_000
    assert counts["sources"] >= 8
    assert counts["merged_survivors"] >= 400 and counts["merged_away"] >= 500
    with factory() as s:
        grades = dict(
            s.execute(sa.select(Location.precision, sa.func.count()).group_by(Location.precision))
            .tuples()
            .all()
        )
        assert set(grades) == {"exact", "county_centroid", "state_centroid", "unknown"}
        assert grades["exact"] >= 2_500 and grades["unknown"] >= 1_000
        lifecycles = s.scalars(sa.select(sa.distinct(Proposal.lifecycle_state))).all()
        assert {"withdrawn", "built", "filed", "under_construction"} <= set(lifecycles)
        multi_link = s.scalar(
            sa.select(sa.func.count()).select_from(
                sa.select(ProposalSource.proposal_id)
                .group_by(ProposalSource.proposal_id)
                .having(sa.func.count() > 1)
                .subquery()
            )
        )
        assert multi_link == counts["merged_survivors"]


def test_a_national_call_draws_the_full_store_within_the_feature_budget(
    client: TestClient, store: tuple[sessionmaker[Session], dict[str, int]]
) -> None:
    _, counts = store
    _, body = _get(client, REQUESTS["conus_z3"])
    data = body["data"]
    assert isinstance(data, dict)
    assert data["totals"]["records"] == counts["public"]
    assert data["totals"]["clustered"] is True
    assert 0 < len(data["features"]) <= 2_000  # D-13: a viewport returns at most 2,000 features


def _reference_s() -> float:
    """A fixed CPU workload shaped like the map's Python half (tuples, a sort, grouping, JSON), so
    its time tracks the runner's speed and load, not this code. Deterministic; ~60-100 ms."""
    t0 = time.perf_counter()
    rows = [(i, f"prop_{i:08d}", ("filed", "studied", "built")[i % 3], i * 0.37) for i in range(40_000)]
    rows.sort(key=lambda r: (r[2], -r[3]))
    cells: dict[tuple[int, str], list[float]] = {}
    for i, _name, state, mw in rows:
        cells.setdefault((i % 97, state), []).append(mw)
    json.dumps([{"id": n, "s": s, "mw": m} for _, n, s, m in rows[:15_000]])
    return time.perf_counter() - t0


@pytest.mark.parametrize("name", list(REQUESTS))
def test_a_cold_geo_call_has_not_regressed_against_the_reference_workload(
    client: TestClient, name: str
) -> None:
    params = REQUESTS[name]
    _get(client, params)  # imports, page cache and the startup heap freeze, not the call itself
    cold: list[float] = []
    references: list[float] = []
    for _ in range(CALLS):
        references.append(_reference_s())
        geo_cache.reset()  # the first call per filter set after a data change: the national work
        cold.append(_get(client, params)[0])
    ratio = min(cold) / min(references)
    limit = max(GUARD * RECORDED_RATIO[name], RATIO_FLOOR)
    assert ratio < limit, (
        f"{name}: best cold call {min(cold) * 1000:.0f} ms is {ratio:.2f}x the reference workload "
        f"({min(references) * 1000:.0f} ms); recorded {RECORDED_RATIO[name]}x, limit {limit:.2f}x. "
        f"Budget: E-16 {BUDGET_S * 1000:.0f} ms p95 (docs/04)."
    )


@pytest.mark.parametrize("name", ["conus_z3", "conus_z4", "default_view_z3"])
def test_a_pan_is_a_cache_hit_well_under_the_cold_call(client: TestClient, name: str) -> None:
    """The same filters with another viewport reuse the whole-set work (`services/api/geo_cache.py`).
    Clustered views only: a viewport of at most 500 markers also loads those records, whatever the
    cache holds, and is timed by the budget test above."""
    params = REQUESTS[name]
    pan = {**params, "bbox": "-125,24,-85,50"}
    geo_cache.reset()
    cold = [_get(client, params)[0]]
    warm: list[float] = []
    for _ in range(CALLS):
        warm.append(_get(client, pan)[0])
        geo_cache.reset()
        cold.append(_get(client, params)[0])
    warm_median, cold_median = statistics.median(warm), statistics.median(cold)
    assert warm_median < 0.5 * cold_median, (name, warm_median, cold_median)


@pytest.mark.parametrize("name", ["conus_z3", "conus_z4", "default_view_z3"])
def test_a_pan_runs_one_statement(
    client: TestClient, store: tuple[sessionmaker[Session], dict[str, int]], name: str
) -> None:
    """Deterministic twin of the timing above: a cache hit reads only the data version."""
    factory, _ = store
    params = REQUESTS[name]
    _get(client, params)
    statements: list[str] = []
    engine = factory.kw["bind"]

    def grab(_c: object, _cur: object, statement: str, *_a: object) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", grab)
    try:
        _get(client, {**params, "bbox": "-125,24,-85,50"})
    finally:
        sa.event.remove(engine, "before_cursor_execute", grab)
    assert len(statements) == 1, statements
