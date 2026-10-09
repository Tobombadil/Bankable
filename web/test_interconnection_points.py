"""Grid interconnection point pages (owner decision 2026-09-28; docs/25 §1): `/interconnection-points`,
`/interconnection-points/{public_id}` and the "Connects at" row on a proposal page.

Driven against the real API in-process over a SQLite store (the `web/test_auth.py` pattern), so what
the page prints is what the API's tier predicate let through: a point whose only project is
unpublished must not appear, and no total may include that project.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.conftest import make_attribution_licence, make_event, make_open_licence, make_public_source
from services.api.deps import get_db
from services.api.interconnection_points import ACTIVE_LIFECYCLE_STATES
from services.api.ratelimit import default_limiter
from services.api.visibility import interconnection_point_visibility_filter
from services.db.models import InterconnectionPoint, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.ingest.interconnection import link_all_points
from web.api_client import ApiClient, ApiError
from web.app import app as web_app
from web.interconnection_points import change_label, flatten_change, flatten_point, proposal_connection
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
    # The unpublished 900 MW is in no total; MW print in the docs/31 §4 format (UX-8: no ".0").
    assert "1,100" not in body and 'class="num tnum">200</td>' in body
    assert f'href="/interconnection-points/{store["points"]["b1"]}"' in body
    assert "2 points match these filters." in body
    assert '<a href="/interconnection-points">Grid connection points</a>' in body
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
    assert '<dd class="tnum">200 <span' in body and "345 kV" in body and "59903" in body
    assert "Test Public Source" in body  # the provenance panel names the register
    assert "View at source" in body


def _add_changes(db_sessionmaker: sessionmaker[Session], store: dict[str, Any]) -> None:
    db = db_sessionmaker()
    by_key = {
        k: db.scalar(select(Proposal).where(Proposal.public_id == pid))
        for k, (pid, _) in store["props"].items()
    }
    ercot = db.get(Source, "us.test.ercot")
    assert ercot is not None
    for key, event_type, days, after in (
        ("b1", "created", 6, {"lifecycle_state": "filed"}),
        ("b2", "withdrawn", 2, {"lifecycle_state": "withdrawn"}),
        ("b3", "status_change", 1, {"lifecycle_state": "studied"}),  # unpublished project: never shown
    ):
        prop = by_key[key]
        assert prop is not None
        event = make_event(db, prop, ercot, event_type=event_type)
        event.observed_at = NOW - dt.timedelta(days=days)
        event.after = after
        event.idempotency_key = f"{event.idempotency_key}:{key}"
    db.commit()
    db.close()


def test_detail_page_lists_recent_changes_at_the_point(
    web_client: TestClient, store: dict[str, Any], db_sessionmaker: sessionmaker[Session]
) -> None:
    quiet = web_client.get(f"/interconnection-points/{store['points']['b1']}").text
    assert "Recent changes at this point" in quiet
    assert "No change events are recorded for the projects listed here yet." in quiet
    _add_changes(db_sessionmaker, store)

    body = web_client.get(f"/interconnection-points/{store['points']['b1']}").text

    section = body.split("Recent changes at this point", 1)[1].split('aria-label="Sources"', 1)[0]
    _b1, b1_slug = store["props"]["b1"]
    _b2, b2_slug = store["props"]["b2"]
    assert section.index("Withdrawn") < section.index("Entered the queue here as filed")  # newest first
    assert f'href="/proposals/{b2_slug}">Project b2</a>' in section
    assert f'href="/proposals/{b1_slug}">Project b1</a>' in section
    assert "Project b3" not in body and "studied" not in section  # the unpublished project's event
    assert "Test Public Source" in section  # each row credits its source


def test_change_labels_word_each_event() -> None:
    assert change_label({"event_type": "created", "after": {"lifecycle_state": "under_construction"}}) == (
        "Entered the queue here as under construction"
    )
    assert change_label({"event_type": "created"}) == "Entered the queue here"
    assert change_label(
        {
            "event_type": "status_change",
            "before": {"lifecycle_state": "filed"},
            "after": {"lifecycle_state": "studied"},
        }
    ) == ("Status changed from filed to studied")
    assert change_label({"event_type": "status_change", "after": {"lifecycle_state": "built"}}) == (
        "Status changed to built"
    )
    assert change_label({"event_type": "status_change", "before": None, "after": None}) == "Status changed"
    assert change_label({"event_type": "withdrawn"}) == "Withdrawn"
    assert change_label({"event_type": "under_construction"}) == "Under construction"
    row = flatten_change({"id": "evt_1", "event_type": "cancelled", "subject": {}, "provenance": None})
    assert row["label"] == "Cancelled" and row["date"] is None and row["proposal_href"] is None


def test_hidden_and_unknown_points_are_the_same_404(web_client: TestClient, store: dict[str, Any]) -> None:
    hidden = web_client.get(f"/interconnection-points/{store['points']['h1']}")
    unknown = web_client.get("/interconnection-points/poi_0000000000")
    assert hidden.status_code == unknown.status_code == 404


def test_proposal_page_says_where_the_project_connects(web_client: TestClient, store: dict[str, Any]) -> None:
    _b1_id, b1_slug = store["props"]["b1"]
    body = web_client.get(f"/proposals/{b1_slug}").text
    assert "<dt>Connects at</dt>" in body
    assert f'<a href="/interconnection-points/{store["points"]["b1"]}">59903 Bearkat 345kV</a>' in body
    assert "200 MW active across 1 project queued there (2 in all)" in body
    _n1_id, n1_slug = store["props"]["n1"]
    assert "<dt>Connects at</dt><dd>—</dd>" in web_client.get(f"/proposals/{n1_slug}").text


def test_the_proposal_list_narrows_to_one_point(web_client: TestClient, store: dict[str, Any]) -> None:
    body = web_client.get(
        "/proposals", params={"interconnection_point_id": store["points"]["b1"], "include_withdrawn": "1"}
    ).text
    assert "Project b1" in body and "Project b2" in body and "Project c1" not in body


def test_the_sitemap_lists_exactly_the_points_the_public_tier_can_see(
    web_client: TestClient, store: dict[str, Any], db_sessionmaker: sessionmaker[Session]
) -> None:
    """The sitemap reads the points list, so it lists a point only when
    `interconnection_point_visibility_filter` admits it: never a point whose only project is
    unpublished, and never one named by a gated register even beside a visible project -- either
    would be an oracle. Pinned against the predicate itself, not a hand-written list."""
    db = db_sessionmaker()
    gated_lic = make_open_licence(db, id_="gated-lic")
    gated_lic.reuse_class = "restricted"
    pjm = make_public_source(db, gated_lic, id_="us.test.pjm")
    gated = InterconnectionPoint(
        public_id="",
        operator="PJM",
        name_display="Gated Sub 500kV",
        name_key="sub:gated|500",
        key_rule="test",
        kind="substation",
        source_id=pjm.id,
        source_url=pjm.url,
        retrieved_at=NOW,
        licence_id=gated_lic.id,
    )
    db.add(gated)
    db.flush()
    gated.public_id = public_id("poi", gated.id)
    visible_beside = db.scalar(select(Proposal).where(Proposal.public_id == store["props"]["c1"][0]))
    assert visible_beside is not None
    visible_beside.interconnection_point_id = gated.id  # a visible project at a gated register's point
    db.commit()
    expected = set(
        db.scalars(
            select(InterconnectionPoint.public_id).where(*interconnection_point_visibility_filter("public"))
        )
    )
    everything = set(db.scalars(select(InterconnectionPoint.public_id)))
    gated_id = gated.public_id
    db.close()
    web_app.state.__dict__.pop("sitemap_cache", None)

    body = web_client.get("/sitemap.xml").text
    web_app.state.__dict__.pop("sitemap_cache", None)

    listed = set(re.findall(r"/interconnection-points/(poi_[0-9A-Za-z]+)</loc>", body))
    assert expected == {store["points"]["b1"]}  # Joslin (unpublished only), Whirlwind (emptied), PJM: dark
    assert listed == expected
    assert gated_id in everything and gated_id not in body
    assert store["points"]["h1"] not in body and store["points"]["c1"] not in body
    assert "<loc>http://testserver/interconnection-points</loc>" in body


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
