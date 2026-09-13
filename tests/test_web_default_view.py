"""docs/00-PLAN.md task item 6: the default map/list view excludes withdrawn and cancelled
proposals (product defect A), the "include withdrawn" toggle includes them, and every rendered
proposal carries provenance straight from the API envelope -- all against the real
`data/eval/normalized.parquet` fixture loaded through `services/ingest/loader.py` into an
in-process API mounted into `web.app`, per the task's instruction to test against that fixture.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.deps import get_db
from services.db.session import get_engine, get_sessionmaker, init_db
from web.api_client import ApiClient
from web.app import app as web_app
from web.data_loading import load_test_database
from web.viewmodels import ACTIVE_PROPOSAL_STATES

ACTIVE_STATES_CSV = ",".join(ACTIVE_PROPOSAL_STATES)


@pytest.fixture(scope="module")
def loaded_db() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        report = load_test_database(session, include_opportunities=False)
    assert sum(report["proposals"].values()) > 0, "eval fixture produced no proposal rows"
    return session_factory


@pytest.fixture()
def web_client(loaded_db: sessionmaker[Session]):
    def _override_get_db():
        session = loaded_db()
        try:
            yield session
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = _override_get_db
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def test_default_geo_excludes_withdrawn_and_cancelled(web_client: TestClient) -> None:
    resp = web_client.get("/api/proposals/geo", params={"bbox": "-179,-85,179,85", "zoom": "1"})
    assert resp.status_code == 200
    counts = resp.json()["data"]["totals"]["lifecycle_state_counts"]
    assert "withdrawn" not in counts
    assert "cancelled" not in counts
    assert sum(counts.values()) > 0, "default view should still show the active proposals"


def test_include_withdrawn_toggle_adds_them_back(web_client: TestClient) -> None:
    resp = web_client.get(
        "/api/proposals/geo",
        params={"bbox": "-179,-85,179,85", "zoom": "1", "include_withdrawn": "1"},
    )
    assert resp.status_code == 200
    counts = resp.json()["data"]["totals"]["lifecycle_state_counts"]
    assert counts.get("withdrawn", 0) > 0


def test_explicit_lifecycle_state_overrides_the_default(web_client: TestClient) -> None:
    """Same "explicit wins" rule the opportunities list already used for `status=all`."""
    resp = web_client.get(
        "/api/proposals/geo",
        params={"bbox": "-179,-85,179,85", "zoom": "1", "lifecycle_state": "withdrawn"},
    )
    counts = resp.json()["data"]["totals"]["lifecycle_state_counts"]
    assert set(counts) <= {"withdrawn"}
    assert counts.get("withdrawn", 0) > 0


def test_home_page_states_the_active_vs_withdrawn_counts(web_client: TestClient) -> None:
    resp = web_client.get("/")
    assert resp.status_code == 200
    assert "active proposals" in resp.text
    assert "hidden by default" in resp.text


def test_proposals_list_default_excludes_withdrawn_rows(web_client: TestClient) -> None:
    resp = web_client.get("/proposals", params={"sort": "-capacity_mw"})
    assert resp.status_code == 200
    for state in ("withdrawn", "cancelled"):
        assert f">{state}<" not in resp.text


def test_proposals_list_toggle_includes_withdrawn_rows(web_client: TestClient) -> None:
    resp = web_client.get("/proposals", params={"include_withdrawn": "1", "sort": "-capacity_mw"})
    assert resp.status_code == 200
    assert ">withdrawn<" in resp.text


def test_proposal_detail_provenance_matches_the_api_envelope(web_client: TestClient) -> None:
    api = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    listing = api.get("/v1/proposals", params={"limit": 1, "lifecycle_state": ACTIVE_STATES_CSV})
    assert listing["data"], "expected at least one active proposal from the eval fixture"
    entity = listing["data"][0]
    provenance = entity["provenance"]
    assert provenance, "API returned a record with no provenance -- should be impossible (DA-2)"

    resp = web_client.get(f"/proposals/{entity['slug']}")
    assert resp.status_code == 200
    assert provenance[0]["source_name"] in resp.text
    assert provenance[0]["source_url"] in resp.text
    assert provenance[0]["retrieved_at"][:10] in resp.text
