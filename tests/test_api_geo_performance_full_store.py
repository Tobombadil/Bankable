"""`GET /v1/proposals/geo` within docs/04 D-13's per-call budget on a store the size and shape of the
full dev store, in CI.

`tests/test_api_geo_performance.py` asserts the same budget, but only on EIA-860M (~2,300 rows) and
only where the git-ignored `data/normalized` exists, so CI never ran it, and the full dev store
(~10,600 live proposals over every `PROPOSAL_SOURCE_IDS` source) measured 1.1-1.3 s for a warm
national call (2026-10-07). This module builds a synthetic store of that size and mix from
committed data (`tests/geo_full_store.py`: 10,652 public proposals, eight sources, 404 merges and
532 merged-away rows, every placement grade, two derived-only licences, GB rows) in about 3 s, and
asserts the budget on the map's real requests with the existing test's convention: one warm-up
call, then the timed call, against 500 ms plus the existing test's margin (1.0 s), here the median
of three timed calls so one scheduler hiccup does not fail CI.

Measured on the dev VM (2026-10-07, median of 9 warm calls; docs/CHANGELOG.md): national zoom 3
on this store 1,377 ms before the change, about 340 ms after; on main, 6 of the 9 budget cases
below fail. The whole module runs in about 15-20 s.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

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
#: 500 ms (D-13 as this sprint set it) plus the margin `tests/test_api_geo_performance.py` uses.
BUDGET_S = 1.0

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


@pytest.mark.parametrize("name", list(REQUESTS))
def test_geo_call_meets_the_d13_budget_on_a_full_scale_store(client: TestClient, name: str) -> None:
    params = REQUESTS[name]
    _get(client, params)  # warm-up, as tests/test_api_geo_performance.py
    timings = [_get(client, params)[0] for _ in range(3)]
    median = statistics.median(timings)
    assert median < BUDGET_S, (
        f"{name}: median {median * 1000:.0f} ms over {[round(t * 1000) for t in timings]} ms "
        f"(budget 500 ms + margin, {BUDGET_S * 1000:.0f} ms)"
    )
