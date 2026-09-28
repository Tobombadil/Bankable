"""Grid interconnection point pages (owner decision 2026-09-28; docs/25 §1): `/interconnection-points`,
`/interconnection-points/{public_id}` and the "Connects at" row on a proposal page.

Driven against the real API in-process over a SQLite store (the `web/test_auth.py` pattern), so what
the page prints is what the API's tier predicate let through: a point whose only project is
unpublished must not appear, and no total may include that project.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.conftest import make_attribution_licence, make_open_licence, make_public_source
from services.api.deps import get_db
from services.api.interconnection_points import ACTIVE_LIFECYCLE_STATES
from services.api.ratelimit import default_limiter
from services.db.models import InterconnectionPoint, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.interconnection import link_all_points
from web.api_client import ApiClient, ApiError
from web.app import app as web_app
from web.interconnection_points import flatten_point, proposal_connection
from web.viewmodels import ACTIVE_PROPOSAL_STATES

NOW = dt.datetime.now(dt.UTC)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


def _proposal(
    db: Session,
    src: Source,
    key: str,
    poi: str | None,
    *,
    mw: float,
    lifecycle: str = "filed",
    publish: str = "public",
) -> Proposal:
    prop = Proposal(
        public_id="",
        slug="",
        kind="generation",
        name_canonical=f"Project {key}",
        jurisdiction="US-TX",
        iso="ERCOT" if src.id.endswith("ercot") else "CAISO",
        lifecycle_state=lifecycle,
        capacity_mw=mw,
        technology="solar_pv",
        publish_state=publish,
        published_at=NOW - dt.timedelta(days=5),
        public_at=NOW - dt.timedelta(days=1),
        min_reuse_class=src.licence.reuse_class,
        source_count=1,
    )
    db.add(prop)
    db.flush()
    prop.public_id = public_id("prop", prop.id)
    prop.slug = f"{slugify(prop.name_canonical)}-{key}"
    db.add(
        ProposalSource(
            proposal_id=prop.id,
            source_id=src.id,
            source_record_id=key,
            source_url=f"{src.url}#{key}",
            retrieved_at=NOW - dt.timedelta(days=1),
            licence_id=src.licence_id,
            raw={"Interconnection Location": poi} if poi else {},
            first_seen=NOW - dt.timedelta(days=9),
            last_seen=NOW - dt.timedelta(days=1),
        )
    )
    db.flush()
    return prop


@pytest.fixture()
def store(db_sessionmaker: sessionmaker[Session]) -> dict[str, Any]:
    db = db_sessionmaker()
    ercot = make_public_source(db, make_open_licence(db), id_="us.test.ercot")
    caiso = make_public_source(db, make_attribution_licence(db), id_="us.test.caiso")
    props = {
        "b1": _proposal(db, ercot, "b1", "59903 Bearkat 345kV", mw=200.0),
        "b2": _proposal(db, ercot, "b2", "59903 BEARKAT 345 kV", mw=100.0, lifecycle="withdrawn"),
        "b3": _proposal(db, ercot, "b3", "59903 Bearkat 345kV", mw=900.0, publish="unpublished"),
        "h1": _proposal(db, ercot, "h1", "8140 Joslin 138kV", mw=400.0, publish="unpublished"),
        "c1": _proposal(db, caiso, "c1", "Whirlwind Substation 230kV", mw=50.0),
        "n1": _proposal(db, ercot, "n1", None, mw=10.0),
    }
    link_all_points(db)
    db.commit()
    points = {
        k: db.get(InterconnectionPoint, p.interconnection_point_id) for k, p in props.items() if k != "n1"
    }
    out = {
        "props": {k: (p.public_id, p.slug) for k, p in props.items()},
        "points": {k: pt.public_id for k, pt in points.items() if pt is not None},
    }
    db.close()
    return out


@pytest.fixture()
def web_client(db_sessionmaker: sessionmaker[Session]) -> Iterator[TestClient]:
    def _override_get_db() -> Iterator[Session]:
        session = db_sessionmaker()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = _override_get_db
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def test_active_means_what_the_proposal_list_means() -> None:
    assert ACTIVE_LIFECYCLE_STATES == ACTIVE_PROPOSAL_STATES


def test_index_lists_visible_points_with_their_totals(web_client: TestClient, store: dict[str, Any]) -> None:
    resp = web_client.get("/interconnection-points")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "59903 Bearkat 345kV" in body and "Whirlwind Substation 230kV" in body
    assert "8140 Joslin 138kV" not in body  # its only project is unpublished
    assert "1,100.0" not in body and "200.0" in body  # the unpublished 900 MW is in no total
    assert f'href="/interconnection-points/{store["points"]["b1"]}"' in body
    assert "2 points match these filters." in body
    assert '<a href="/interconnection-points">Grid points</a>' in body
    # Largest active queue first: Bearkat (200 MW) above Whirlwind (50 MW).
    assert body.index("59903 Bearkat 345kV") < body.index("Whirlwind Substation 230kV")


def test_index_filters_pass_through_and_htmx_gets_the_rows(
    web_client: TestClient, store: dict[str, Any]
) -> None:
    only_caiso = web_client.get("/interconnection-points", params={"iso": "CAISO"}).text
    assert "Whirlwind Substation 230kV" in only_caiso and "59903 Bearkat 345kV" not in only_caiso
    assert '<option value="CAISO" selected>' in only_caiso
    partial = web_client.get(
        "/interconnection-points", params={"q": "bearkat"}, headers={"HX-Request": "true"}
    )
    assert partial.status_code == 200
    assert "<html" not in partial.text and "59903 Bearkat 345kV" in partial.text
    # A malformed filter from a hand-written link shows the unfiltered index rather than an error.
    bad = web_client.get("/interconnection-points", params={"kind": "switchyard"})
    assert bad.status_code == 200 and "59903 Bearkat 345kV" in bad.text
    empty = web_client.get("/interconnection-points", params={"q": "no such point"})
    assert "No interconnection points match these filters." in empty.text


def test_detail_page_shows_the_queue_and_its_source(web_client: TestClient, store: dict[str, Any]) -> None:
    resp = web_client.get(f"/interconnection-points/{store['points']['b1']}")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "<h1>59903 Bearkat 345kV</h1>" in body
    _b1_id, b1_slug = store["props"]["b1"]
    assert f'href="/proposals/{b1_slug}"' in body
    assert "Project b3" not in body  # unpublished
    assert "200.0" in body and "345 kV" in body and "59903" in body
    assert "Test Public Source" in body  # the provenance panel names the register
    assert "View at source" in body


def test_hidden_and_unknown_points_are_the_same_404(web_client: TestClient, store: dict[str, Any]) -> None:
    hidden = web_client.get(f"/interconnection-points/{store['points']['h1']}")
    unknown = web_client.get("/interconnection-points/poi_0000000000")
    assert hidden.status_code == unknown.status_code == 404


def test_proposal_page_says_where_the_project_connects(web_client: TestClient, store: dict[str, Any]) -> None:
    _b1_id, b1_slug = store["props"]["b1"]
    body = web_client.get(f"/proposals/{b1_slug}").text
    assert "<dt>Connects at</dt>" in body
    assert f'<a href="/interconnection-points/{store["points"]["b1"]}">59903 Bearkat 345kV</a>' in body
    assert "200.0 MW active across 1 project queued there (2 in all)" in body
    _n1_id, n1_slug = store["props"]["n1"]
    assert "<dt>Connects at</dt><dd>—</dd>" in web_client.get(f"/proposals/{n1_slug}").text


def test_the_proposal_list_narrows_to_one_point(web_client: TestClient, store: dict[str, Any]) -> None:
    body = web_client.get(
        "/proposals", params={"interconnection_point_id": store["points"]["b1"], "include_withdrawn": "1"}
    ).text
    assert "Project b1" in body and "Project b2" in body and "Project c1" not in body


class _FailingApi:
    def get(self, path: str, params: Any = None) -> Any:
        raise ApiError(503, {"title": "unavailable"})


def test_an_api_failure_is_a_503_page_not_a_500(web_client: TestClient) -> None:
    web_app.state.api_client = _FailingApi()
    assert web_client.get("/interconnection-points").status_code == 503
    assert web_client.get("/interconnection-points/poi_0000000000").status_code == 503


def test_proposal_connection_drops_the_row_on_any_failure() -> None:
    assert proposal_connection(_FailingApi(), "prop_1") is None
    assert proposal_connection(_FailingApi(), None) is None


def test_flatten_point_never_prints_none() -> None:
    record = flatten_point({"public_id": "poi_1", "name": "X", "kind": "line_tap", "totals": {}})
    assert record["voltage_label"] is None
    assert record["active_mw_label"] == "—"
    assert record["kind_label"] == "Line tap"
    assert record["top_technologies"] == []
