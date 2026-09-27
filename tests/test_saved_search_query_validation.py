"""Stored filter validation (2026-09-27): `POST`/`PATCH /v1/saved-searches` and `POST /v1/webhooks`
answer a `query` key the entity's list endpoint does not filter on with the list's own
`400 unknown_parameter`, and a value the list would refuse with its `400 validation_error`; rows
stored before the check keep working. Also the list-side defects the parity work found: `state`
accepted and ignored, a malformed range bound answering 500, `?placement=,` dropping unlocated rows.
"""

from __future__ import annotations

import datetime as dt

import pytest

from services.alerts.matching import (
    event_matches_query,
    opportunity_matches_query,
    proposal_matches_query,
    query_params,
)
from services.api.conftest import (
    make_event,
    make_location,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.models import SavedSearch
from services.ids import public_id
from tests.conftest import login, make_account, make_user


def _login(client, db, entitlement: str = "pro"):
    account = make_account(db, entitlement=entitlement)
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    return account, user


def _problem(resp, status: int, code: str, field: str | None = None) -> None:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["code"] == code
    if field is not None:
        assert body["errors"][0]["field"] == field


# ------------------------------------------------------------------------------- saved searches
@pytest.mark.parametrize(
    ("entity", "key"),
    [
        ("proposal", "technolgy"),
        ("proposal", "sort"),
        ("proposal", "limit"),
        ("proposal", "due_at[from]"),
        ("opportunity", "state"),
        ("opportunity", "capacity_mw[gte]"),
        ("event", "q"),
        ("event", "technology"),
        ("match", "cursor"),
    ],
)
def test_create_refuses_a_key_the_list_does_not_filter_on(client, db, entity, key):
    _login(client, db)
    resp = client.post("/v1/saved-searches", json={"name": "n", "entity": entity, "query": {key: "x"}})
    _problem(resp, 400, "unknown_parameter", key)
    assert db.query(SavedSearch).count() == 0


@pytest.mark.parametrize(
    ("entity", "query", "field"),
    [
        ("proposal", {"capacity_mw[gte]": "big"}, "capacity_mw[gte]"),
        ("proposal", {"capacity_mw[lte]": "nan"}, "capacity_mw[lte]"),
        ("proposal", {"placement": "somewhere"}, "placement"),
        ("proposal", {"slipped": "maybe"}, "slipped"),
        ("proposal", {"slip_bucket": "decade"}, "slip_bucket"),
        ("proposal", {"slipped": False, "slip_bucket": "under_1y"}, "slip_bucket"),
        ("opportunity", {"due_at[from]": "next tuesday"}, "due_at[from]"),
        ("event", {"since": "yesterday"}, "since"),
        ("event", {"changed_key": "lifecycle state"}, "changed_key"),
        ("event", {"changed_key": 'a","b'}, "changed_key"),
        ("event", {"observed_at[from]": "last week"}, "observed_at[from]"),
        ("event", {"observed_at[to]": "soon"}, "observed_at[to]"),
        ("proposal", {"state": {"nested": "object"}}, "query.state"),
        # Lane E15 (2026-09-27): the filters implemented that day parse through the list's functions.
        ("proposal", {"storage_mwh[gte]": "lots"}, "storage_mwh[gte]"),
        ("proposal", {"first_seen[from]": "last spring"}, "first_seen[from]"),
        ("proposal", {"last_changed[to]": "2026-13-01"}, "last_changed[to]"),
        ("opportunity", {"open_at[from]": "2026-01-01T00:00:00Z"}, "open_at[from]"),
        ("opportunity", {"open_at[to]": "June"}, "open_at[to]"),
        ("opportunity", {"capacity_sought_mw[gte]": "inf"}, "capacity_sought_mw[gte]"),
        ("opportunity", {"budget_currency": "eur"}, "budget_currency"),
        ("opportunity", {"budget_amount[gte]": 1000000}, "budget_amount[gte]"),
        (
            "opportunity",
            {"budget_amount[gte]": 1000000, "budget_currency": ["EUR", "USD"]},
            "budget_amount[gte]",
        ),
        ("opportunity", {"budget_amount[gte]": "a million", "budget_currency": "EUR"}, "budget_amount[gte]"),
    ],
)
def test_create_refuses_a_value_the_list_would_refuse(client, db, entity, query, field):
    _login(client, db)
    resp = client.post("/v1/saved-searches", json={"name": "n", "entity": entity, "query": query})
    _problem(resp, 400, "validation_error", field)


def test_create_accepts_every_list_filter(client, db):
    _login(client, db)
    query = {
        "kind": ["storage"],
        "technology": "bess_li_ion",
        "lifecycle_state": "filed",
        "jurisdiction": "US-TX",
        "iso": "ERCOT",
        "state": ["US-TX", "US-NM"],
        "source_id": "us.test.public_source",
        "capacity_mw[gte]": 500,
        "capacity_mw[lte]": "900.5",
        "slug": "x",
        "county_fips": "48453",
        "placement": "exact,region",
        "slipped": True,
        "slip_bucket": "under_1y",
        "q": "solar",
        "sponsor_id": "org_01JBQ8C4X1",
        "storage_mwh[gte]": 100,
        "first_seen[from]": "2026-09-01T00:00:00Z",
        "first_seen[to]": "2026-09-30",
        "last_changed[from]": "2026-09-01T00:00:00+02:00",
        "last_changed[to]": "2026-09-30T00:00:00Z",
    }
    resp = client.post("/v1/saved-searches", json={"name": "all", "entity": "proposal", "query": query})
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["query"] == query


def test_create_accepts_every_opportunity_filter(client, db):
    _login(client, db)
    query = {
        "status": "open,closed",
        "kind": "rfp",
        "technologies": "wind",
        "jurisdiction": "GB",
        "source_id": "us.test.public_source",
        "due_at[from]": "2026-10-01",
        "due_at[to]": "2026-12-31T23:59:59Z",
        "slug": "x",
        "q": "wind",
        "issuer_id": ["org_01JBQ8C4X1"],
        "open_at[from]": "2026-01-01",
        "open_at[to]": "2026-06-30",
        "capacity_sought_mw[gte]": 50,
        "budget_currency": "EUR",
        "budget_amount[gte]": 1000000,
        "first_seen[from]": "2026-09-01T00:00:00Z",
        "first_seen[to]": "2026-09-30",
        "last_changed[from]": "2026-09-01",
        "last_changed[to]": "2026-09-30",
    }
    resp = client.post("/v1/saved-searches", json={"name": "opps", "entity": "opportunity", "query": query})
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["query"] == query


@pytest.mark.parametrize("entity", ["proposal", "opportunity"])
def test_create_refuses_updated_since_and_says_what_to_use(client, db, entity):
    """`updated_since` is the list's sync cursor, not an alert filter (records.SYNC_FILTERS): refused
    with the key named and a detail that points at `last_changed[from]`."""
    _login(client, db)
    resp = client.post(
        "/v1/saved-searches",
        json={"name": "n", "entity": entity, "query": {"updated_since": "2026-09-01T00:00:00Z"}},
    )
    _problem(resp, 400, "unknown_parameter", "updated_since")
    assert "last_changed[from]" in resp.json()["detail"]
    assert db.query(SavedSearch).count() == 0


def test_webhook_create_refuses_updated_since(client, db):
    _login(client, db, "api")
    resp = client.post(
        "/v1/webhooks",
        json={
            "url": "https://example.com/hook",
            "types": ["event.published"],
            "entity": "opportunity",
            "query": {"updated_since": "2026-09-01"},
        },
    )
    _problem(resp, 400, "unknown_parameter", "updated_since")
    assert "last_changed[from]" in resp.json()["detail"]


def test_create_accepts_the_event_filters_added_on_2026_09_27(client, db):
    _login(client, db)
    query = {
        "changed_key": ["lifecycle_state", "capacity_mw"],
        "observed_at[from]": "2026-09-01T00:00:00Z",
        "observed_at[to]": "2026-09-30",
        "event_type": "status_change",
    }
    resp = client.post("/v1/saved-searches", json={"name": "events", "entity": "event", "query": query})
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["query"] == query


def test_events_list_refuses_q_rather_than_ignoring_it(client, db):
    """`GET /v1/events?q=` was accepted and filtered nothing until 2026-09-27 (lane E14): the whole
    feed came back under a search that looked applied."""
    resp = client.get("/v1/events", params={"q": "solar"})
    _problem(resp, 400, "unknown_parameter", "q")


def test_patch_validates_a_new_query_against_the_stored_entity(client, db):
    _login(client, db)
    created = client.post(
        "/v1/saved-searches", json={"name": "n", "entity": "opportunity", "query": {"status": "open"}}
    ).json()["data"]
    sid = created["saved_search_id"]
    _problem(
        client.patch(f"/v1/saved-searches/{sid}", json={"query": {"state": "US-TX"}}),
        400,
        "unknown_parameter",
        "state",
    )
    _problem(client.patch(f"/v1/saved-searches/{sid}", json={"query": ["status"]}), 400, "validation_error")
    ok = client.patch(f"/v1/saved-searches/{sid}", json={"query": {"technologies": "wind"}})
    assert ok.status_code == 200 and ok.json()["data"]["query"] == {"technologies": "wind"}
    # A patch that does not touch the query is not validated against it.
    assert client.patch(f"/v1/saved-searches/{sid}", json={"name": "renamed"}).status_code == 200


def test_a_stored_search_is_not_revalidated_on_read(client, db):
    account, user = _login(client, db)
    legacy = SavedSearch(
        public_id="",
        user_id=user.id,
        account_id=account.id,
        name="legacy",
        entity="proposal",
        query={"technolgy": "wind", "sort": "-capacity_mw"},
        query_hash="x",
        channels=["email"],
    )
    db.add(legacy)
    db.flush()
    legacy.public_id = public_id("ss", legacy.id)
    db.commit()
    assert client.get(f"/v1/saved-searches/{legacy.public_id}").status_code == 200
    assert client.post(f"/v1/saved-searches/{legacy.public_id}/preview").status_code == 200
    assert (
        client.patch(f"/v1/saved-searches/{legacy.public_id}", json={"status": "paused"}).status_code == 200
    )


# ------------------------------------------------------------------------------------ webhooks
def test_webhook_create_refuses_an_unknown_key_and_an_unknown_entity(client, db):
    _login(client, db, "api")
    base = {"url": "https://example.com/hook", "types": ["event.published"]}
    _problem(
        client.post("/v1/webhooks", json={**base, "entity": "proposal", "query": {"capacity_mw[gt]": 5}}),
        400,
        "unknown_parameter",
        "capacity_mw[gt]",
    )
    _problem(
        client.post("/v1/webhooks", json={**base, "query": {"kind": "storage"}}), 400, "unknown_parameter"
    )
    _problem(client.post("/v1/webhooks", json={**base, "entity": "asset"}), 400, "validation_error", "entity")
    _problem(client.post("/v1/webhooks", json={**base, "query": "kind=storage"}), 400, "validation_error")
    ok = client.post("/v1/webhooks", json={**base, "entity": "proposal", "query": {"state": "US-TX"}})
    assert ok.status_code == 201, ok.text


# ------------------------------------------------------------------------------ list endpoints
def test_state_filter_narrows_the_list(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    tx = make_location(db, src, lic, state_code="US-TX")
    ca = make_location(db, src, lic, state_code="US-CA", county_fips="06037")
    make_visible_proposal(db, src, public_id_suffix="1", location=tx)
    make_visible_proposal(db, src, public_id_suffix="2", location=ca)
    make_visible_proposal(db, src, public_id_suffix="3")
    db.commit()

    def names(params: str) -> set[str]:
        resp = client.get(f"/v1/proposals?{params}")
        assert resp.status_code == 200, resp.text
        return {p["slug"].rsplit("-", 1)[1] for p in resp.json()["data"]}

    assert names("state=US-TX") == {"1"}
    assert names("state=US-TX,US-CA") == {"1", "2"}
    assert names("state=US-ZZ") == set()
    assert names("") == {"1", "2", "3"}
    # An empty grade list is no filter, not "every located row".
    assert names("placement=,") == {"1", "2", "3"}


@pytest.mark.parametrize(
    "url",
    [
        "/v1/proposals?capacity_mw[gte]=abc",
        "/v1/proposals?capacity_mw[lte]=inf",
        "/v1/opportunities?due_at[from]=abc",
        "/v1/opportunities?due_at[to]=2026-13-01",
    ],
)
def test_a_malformed_range_bound_is_a_400_not_a_500(client, url):
    _problem(client.get(url), 400, "validation_error")


# ------------------------------------------------------------------------------------- matcher
def test_matcher_fails_closed_on_a_stored_value_the_list_would_refuse(db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src)
    opp = make_visible_opportunity(db, src)
    event = make_event(db, prop, src)
    assert proposal_matches_query(prop, {}) is True
    assert proposal_matches_query(prop, {"capacity_mw[gte]": "big"}) is False
    assert proposal_matches_query(prop, {"placement": "somewhere"}) is False
    assert proposal_matches_query(prop, {"slipped": "maybe"}) is False
    assert opportunity_matches_query(opp, {"due_at[to]": "soon"}) is False
    assert event_matches_query(event, {"since": "yesterday"}) is False
    # An unknown key in a stored row is ignored, as it always was.
    assert proposal_matches_query(prop, {"technolgy": "wind"}) is True


def test_matcher_event_subject_and_since(db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src)
    other = make_visible_proposal(db, src, public_id_suffix="2")
    event = make_event(db, prop, src)
    assert event_matches_query(event, {"subject_id": prop.public_id}) is True
    assert event_matches_query(event, {"subject_id": other.public_id}) is False
    assert event_matches_query(event, {"subject_id": "prop_0" + prop.public_id[5:]}) is False
    assert event_matches_query(event, {"subject_id": prop.public_id.replace("prop_", "opp_")}) is False
    assert event_matches_query(event, {"subject_id": "org_123"}) is False
    assert event_matches_query(event, {"since": str(event.seq - 1)}) is True
    assert event_matches_query(event, {"since": str(event.seq)}) is False
    before = (event.observed_at - dt.timedelta(hours=1)).isoformat()
    assert event_matches_query(event, {"since": before}) is True


def test_query_params_renders_like_a_query_string():
    assert query_params({"a": ["x", "", None, 5], "b": True, "c": False, "d": None, "e": [], "f": 2.5}) == {
        "a": "x,5",
        "b": "true",
        "c": "false",
        "f": "2.5",
    }
