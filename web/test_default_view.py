"""docs/00-PLAN.md task item 6 (product defect A): the default map/list view excludes withdrawn
and cancelled proposals, the "include withdrawn" toggle includes them, an explicit
`lifecycle_state=` wins over both, and every rendered proposal carries provenance straight from
the API envelope.

Every test runs against two stores, built through the real `services/ingest/loader.py` path and
served by an in-process API mounted into `web.app`:

- `committed` -- committed data only, so it runs in CI's `test-web` job (`pytest web -v`) and
  never skips. It is the same entity-resolution fixture CI's other web suites fall back to
  (`data/eval/normalized.parquet` through `web.data_loading.load_test_database`, sampled per
  lifecycle state) plus the recorded Permitting Dashboard connector fixture
  (`tests/fixtures/permits_dashboard_projects.csv`), run through the real connector runner (parse,
  normalise, DQ gates) into a temporary store and loaded from there by
  `web.data_loading.load_real_normalized_sources`, exactly as a scheduled run would be. The eval
  fixture holds no `cancelled` row from a publishable source (its only ones are SPP's, which
  `EVAL_SHORT_ID_MAP` drops as restricted), and the Permitting Dashboard fixture carries a real
  `Cancelled` project (Project ID 71031, pinned in that connector's own tests). Both are public
  registers with `source_id`/`source_url`/`retrieved_at`/licence on every row; no row is invented.
- `real` -- the full git-ignored `data/normalized/*` connector output through
  `web.data_loading.load_dev_database`, as before. Skips, with the reason, when no source under
  that tree loads (a fresh checkout or CI). `DEFAULT_VIEW_DATA_ROOT` points it at another data
  root (the directory that holds `normalized/`).

Until 2026-10-07 this file was `tests/test_web_default_view.py` and loaded only the real tree, so
CI ran it nowhere (`test-core` ignores that path, `test-web` collects `web/` only) and it went stale
unnoticed. A store that lacks any of active, withdrawn or cancelled rows fails
`test_store_holds_every_bucket_the_view_filters` instead of letting the other tests pass vacuously.
"""

from __future__ import annotations

import html
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from conftest import snapshot
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import run as run_connector
from pipeline.connectors.store import Store
from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.session import get_engine, get_sessionmaker, init_db
from web.api_client import ApiClient
from web.app import app as web_app
from web.data_loading import (
    DEFAULT_DATA_ROOT,
    load_dev_database,
    load_real_normalized_sources,
    load_test_database,
)

# The spec's own words (docs/00-PLAN.md task item 6: active is "announced through
# under_construction"; withdrawn and cancelled sit behind the toggle), spelled out here rather than
# imported from `web.viewmodels`, so a change to the code's constants cannot quietly change what
# these tests demand.
ACTIVE_PROPOSAL_STATES = ("announced", "filed", "studied", "permitted", "contracted", "under_construction")
WITHDRAWN_PROPOSAL_STATES = ("withdrawn", "cancelled")
ACTIVE_STATES_CSV = ",".join(ACTIVE_PROPOSAL_STATES)
WORLD = {"bbox": "-179,-85,179,85", "zoom": "1"}

#: Rows per lifecycle state per source taken from the eval fixture: enough for every bucket and
#: more than one list page's worth of rows, small enough to load in about a second.
EVAL_SAMPLE_PER_STATE = 8
PERMITS_SOURCE_ID = "us.permits_dashboard"
PERMITS_FIXTURE = "permits_dashboard_projects.csv"


def _load_committed(session: Session, data_root: Path) -> None:
    load_test_database(session, sample_per_state=EVAL_SAMPLE_PER_STATE, include_opportunities=False)
    registry = Registry()
    raw = snapshot(PERMITS_FIXTURE, registry.get(PERMITS_SOURCE_ID).url, "text/csv")
    result = run_connector(
        PERMITS_SOURCE_ID, registry=registry, store=Store(data_root), trigger="manual", raw=raw
    )
    assert result.status == "ok", (
        f"recorded {PERMITS_SOURCE_ID} fixture did not pass its own run: {result.status}"
    )
    status = load_real_normalized_sources(session, data_root=data_root, source_ids=[PERMITS_SOURCE_ID])
    assert status == {PERMITS_SOURCE_ID: "loaded"}, status


def _load_real(session: Session) -> None:
    data_root = Path(os.environ.get("DEFAULT_VIEW_DATA_ROOT") or DEFAULT_DATA_ROOT)
    report = load_dev_database(session, data_root=data_root, preview=True)
    if "loaded" not in report["sources"].values():
        pytest.skip(
            f"no loadable connector output under {data_root / 'normalized'}; the committed store covers CI"
        )


@pytest.fixture(scope="module", params=["committed", "real"])
def loaded_db(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    with session_factory() as session:
        if request.param == "committed":
            _load_committed(session, tmp_path_factory.mktemp("default-view-store"))
        else:
            _load_real(session)
    return session_factory


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """One process-wide limiter (`services/api/ratelimit.py`); every page here makes several API
    calls (the lifecycle notice alone is three), so without a reset the module trips 429s."""
    default_limiter.reset()


@pytest.fixture()
def api(loaded_db: sessionmaker[Session]) -> Iterator[ApiClient]:
    def _override_get_db() -> Iterator[Session]:
        session = loaded_db()
        try:
            yield session
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = _override_get_db
    try:
        yield ApiClient(TestClient(api_app, base_url="http://api-internal"))
    finally:
        api_app.dependency_overrides.clear()


@pytest.fixture()
def web_client(api: ApiClient) -> Iterator[TestClient]:
    web_app.state.api_client = api
    web_app.state.lag_days_default = None
    try:
        with TestClient(web_app) as client:
            yield client
    finally:
        del web_app.state.api_client


def _count(api: ApiClient, states: str) -> int:
    envelope = api.get("/v1/proposals", params={"lifecycle_state": states, "limit": 1, "include": "count"})
    return int(envelope["meta"]["total"])


def _one(api: ApiClient, state: str) -> dict[str, Any]:
    """The first proposal in `state`, or a skip for the `real` store only: the committed store
    must hold every state these tests name (`test_store_holds_every_bucket_the_view_filters`)."""
    listing = api.get("/v1/proposals", params={"lifecycle_state": state, "limit": 1, "sort": "-capacity_mw"})
    if not listing["data"]:
        pytest.skip(f"this store holds no {state} proposal")
    return listing["data"][0]


def _geo_counts(web_client: TestClient, **params: str) -> dict[str, int]:
    resp = web_client.get("/api/proposals/geo", params={**WORLD, **params})
    assert resp.status_code == 200
    counts: dict[str, int] = resp.json()["data"]["totals"]["lifecycle_state_counts"]
    return counts


def test_store_holds_every_bucket_the_view_filters(api: ApiClient, request: pytest.FixtureRequest) -> None:
    """Without active, withdrawn and cancelled rows every exclusion test below passes vacuously.
    On the committed store the withdrawn and cancelled rows `_one` picks are also placed on the map,
    so the geo tests' "only when placed" guards never apply there."""
    assert _count(api, ACTIVE_STATES_CSV) > 0
    assert _count(api, "withdrawn") > 0
    if request.node.callspec.params["loaded_db"] == "committed":
        for state in WITHDRAWN_PROPOSAL_STATES:
            assert _count(api, state) > 0, f"committed store holds no {state} proposal"
            assert _one(api, state).get("location"), f"committed store's {state} proposal is not placed"


def test_default_geo_excludes_withdrawn_and_cancelled(web_client: TestClient) -> None:
    counts = _geo_counts(web_client)
    assert "withdrawn" not in counts
    assert "cancelled" not in counts
    assert sum(counts.values()) > 0, "default view should still show the active proposals"


@pytest.mark.parametrize("state", WITHDRAWN_PROPOSAL_STATES)
def test_include_withdrawn_toggle_adds_them_back(web_client: TestClient, api: ApiClient, state: str) -> None:
    entity = _one(api, state)
    counts = _geo_counts(web_client, include_withdrawn="1")
    if entity.get("location"):
        assert counts.get(state, 0) > 0
    assert set(counts) & set(ACTIVE_PROPOSAL_STATES), (
        "the toggle adds rows; it does not replace the active ones"
    )


@pytest.mark.parametrize("state", WITHDRAWN_PROPOSAL_STATES)
def test_explicit_lifecycle_state_overrides_the_default(
    web_client: TestClient, api: ApiClient, state: str
) -> None:
    """Same "explicit wins" rule the opportunities list already used for `status=all`."""
    entity = _one(api, state)
    counts = _geo_counts(web_client, lifecycle_state=state)
    assert set(counts) <= {state}
    if entity.get("location"):
        assert counts.get(state, 0) > 0


def test_home_page_states_the_active_vs_withdrawn_counts(web_client: TestClient, api: ApiClient) -> None:
    resp = web_client.get("/")
    assert resp.status_code == 200
    active = _count(api, ACTIVE_STATES_CSV)
    hidden = _count(api, ",".join(WITHDRAWN_PROPOSAL_STATES))
    assert f"Showing {active} active proposals" in resp.text
    assert f"{hidden} withdrawn or cancelled" in resp.text
    assert "hidden by default" in resp.text


def test_proposals_list_default_excludes_withdrawn_rows(web_client: TestClient) -> None:
    resp = web_client.get("/proposals", params={"sort": "-capacity_mw"})
    assert resp.status_code == 200
    # Status chips are sentence case since the 2026-10-06 label pass (`web/labels.py`).
    for state in ("Withdrawn", "Cancelled"):
        assert f">{state}<" not in resp.text


def test_proposals_list_toggle_includes_withdrawn_rows(web_client: TestClient) -> None:
    resp = web_client.get("/proposals", params={"include_withdrawn": "1", "sort": "-capacity_mw"})
    assert resp.status_code == 200
    assert ">Withdrawn<" in resp.text


@pytest.mark.parametrize("state", WITHDRAWN_PROPOSAL_STATES)
def test_named_withdrawn_or_cancelled_row_is_hidden_then_shown(
    web_client: TestClient, api: ApiClient, state: str
) -> None:
    """One specific row, searched for by name so it is on the first page whatever the store's size:
    absent from the default list, present once the toggle is on."""
    entity = _one(api, state)
    href = f'href="/proposals/{entity["slug"]}"'
    default = web_client.get("/proposals", params={"q": entity["name_canonical"]})
    toggled = web_client.get("/proposals", params={"q": entity["name_canonical"], "include_withdrawn": "1"})
    assert default.status_code == 200
    assert toggled.status_code == 200
    assert href not in default.text
    assert href in toggled.text


def test_every_listed_proposal_carries_provenance(api: ApiClient) -> None:
    """DA-2 over the first pages of the toggled-on list (every row of the committed store)."""
    params: dict[str, Any] = {
        "lifecycle_state": ",".join((*ACTIVE_PROPOSAL_STATES, *WITHDRAWN_PROPOSAL_STATES)),
        "limit": 100,
    }
    seen = 0
    for _page in range(5):
        envelope = api.get("/v1/proposals", params=params)
        for entity in envelope["data"]:
            assert entity["provenance"], f"{entity['slug']} has no provenance"
            for row in entity["provenance"]:
                for field in ("source_id", "source_name", "source_url", "retrieved_at", "licence_id"):
                    assert row.get(field), f"{entity['slug']} provenance lacks {field}"
            seen += 1
        if not envelope["page"]["has_more"]:
            break
        params["cursor"] = envelope["page"]["next_cursor"]
    assert seen > 0


@pytest.mark.parametrize(
    "state", [ACTIVE_STATES_CSV, *WITHDRAWN_PROPOSAL_STATES], ids=["active", "withdrawn", "cancelled"]
)
def test_proposal_detail_provenance_matches_the_api_envelope(
    web_client: TestClient, api: ApiClient, state: str
) -> None:
    entity = _one(api, state)
    provenance = entity["provenance"]
    assert provenance, "API returned a record with no provenance -- should be impossible (DA-2)"

    resp = web_client.get(f"/proposals/{entity['slug']}")
    assert resp.status_code == 200
    # Inside the Sources panel, not anywhere on the page: the source name, URL and date also appear
    # in other blocks (attribution footer, field notes), so a page-wide check survives the panel
    # being dropped altogether.
    start = resp.text.find('<section class="provenance-panel"')
    assert start != -1, "detail page renders no Sources panel"
    panel = resp.text[start : resp.text.index("</section>", start)]
    for row in provenance:
        assert html.escape(row["source_name"]) in panel
        # Rendered inside an href, so `&` in a query string appears as `&amp;` (correct HTML escaping).
        assert f'href="{html.escape(row["source_url"])}"' in panel
        assert f"retrieved {row['retrieved_at'][:10]}" in panel
