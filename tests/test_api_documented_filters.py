"""The list filters the spec documented and the code refused until 2026-09-27 (lane E15;
services/README.md open decision 7), on the surfaces `tests/test_saved_search_parity.py` does not
drive: the organisation list, the events feed, the map payloads and feeds, and the rules that are
400s rather than result sets (`budget_amount[gte]` without one currency, a date-time for a date).
List/matcher parity for every proposal and opportunity filter is the parity test's job.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_event,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.models import Event, Organization
from services.ids import public_id

UTC = dt.UTC


def _ids(resp: Any, key: str = "public_id") -> set[str]:
    assert resp.status_code == 200, resp.text
    return {row[key] for row in resp.json()["data"]}


def _feed_ids(resp: Any) -> set[str]:
    assert resp.status_code == 200, resp.text
    return {item["id"] for item in resp.json()["items"]}


def _problem(resp: Any, code: str, field: str) -> None:
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == code
    assert body["errors"][0]["field"] == field


# ------------------------------------------------------------------------------- organisations
def test_organisation_list_filters_on_jurisdiction_and_updated_since(client: TestClient, db: Session) -> None:
    texas = make_org(db, "Lone Star Wind")
    texas.jurisdiction = "US-TX"
    ontario = make_org(db, "Maple Hydro")
    ontario.jurisdiction = "CA-ON"
    unstated = make_org(db, "Nowhere In Particular")
    stale = dt.datetime(2026, 1, 1, tzinfo=UTC)
    db.flush()
    # `updated_at` moves on every ORM write, so set the old value with a bulk UPDATE the mixin's
    # `onupdate` does not touch.
    db.execute(update(Organization).where(Organization.id == ontario.id).values(updated_at=stale))
    db.commit()

    assert _ids(client.get("/v1/organizations", params={"jurisdiction": "US-TX"})) == {texas.public_id}
    assert _ids(client.get("/v1/organizations", params={"jurisdiction": "US-TX,CA-ON"})) == {
        texas.public_id,
        ontario.public_id,
    }
    # The field as served: NULL matches nothing, and a country is `country`, not `jurisdiction`.
    assert _ids(client.get("/v1/organizations", params={"jurisdiction": "US"})) == set()
    recent = _ids(client.get("/v1/organizations", params={"updated_since": "2026-06-01T00:00:00Z"}))
    assert recent == {texas.public_id, unstated.public_id}
    # Inclusive: the bound equal to the stored instant selects the row.
    assert ontario.public_id in _ids(
        client.get("/v1/organizations", params={"updated_since": "2026-01-01T00:00:00Z"})
    )
    _problem(
        client.get("/v1/organizations", params={"updated_since": "recently"}),
        "validation_error",
        "updated_since",
    )


def test_organisation_filters_never_list_a_taken_down_organisation(client: TestClient, db: Session) -> None:
    hidden = make_org(db, "Withdrawn Holdings")
    hidden.jurisdiction = "US-TX"
    hidden.publish_state = "unpublished"
    db.commit()
    assert _ids(client.get("/v1/organizations", params={"jurisdiction": "US-TX"})) == set()
    assert hidden.public_id not in _ids(
        client.get("/v1/organizations", params={"updated_since": "2000-01-01"})
    )


# ---------------------------------------------------------------------------------- events feed
def _two_subject_world(db: Session) -> dict[str, Any]:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    other = make_public_source(db, lic, id_="us.test.other_source")
    texas = make_visible_proposal(db, src, public_id_suffix="1", jurisdiction="US-TX")
    california = make_visible_proposal(db, src, public_id_suffix="2", jurisdiction="US-CA")
    opp = make_visible_opportunity(db, src)  # jurisdiction US-AZ
    ev_tx = make_event(db, texas, src)
    ev_ca = make_event(db, california, other)
    now = dt.datetime.now(UTC)
    ev_opp = Event(
        subject_type="opportunity",
        subject_id=opp.id,
        event_type="status_change",
        observed_at=now - dt.timedelta(days=2),
        source_id=src.id,
        source_url=src.url,
        retrieved_at=now - dt.timedelta(days=2),
        licence_id=src.licence_id,
        before={"status": "announced"},
        after={"status": "open"},
        changed_keys=["status"],
        public_at=now - dt.timedelta(days=1),
        published_at=now - dt.timedelta(days=1),
        idempotency_key="opp-event-1",
    )
    db.add(ev_opp)
    db.commit()
    return {
        "tx": public_id("evt", ev_tx.id),
        "ca": public_id("evt", ev_ca.id),
        "opp": public_id("evt", ev_opp.id),
        "texas": texas,
    }


def test_events_feed_jurisdiction_is_the_subjects(client: TestClient, db: Session) -> None:
    w = _two_subject_world(db)
    assert _feed_ids(client.get("/feeds/events.json")) == {w["tx"], w["ca"], w["opp"]}
    assert _feed_ids(client.get("/feeds/events.json", params={"jurisdiction": "US-TX"})) == {w["tx"]}
    assert _feed_ids(client.get("/feeds/events.json", params={"jurisdiction": "US-AZ"})) == {w["opp"]}
    assert _feed_ids(client.get("/feeds/events.json", params={"jurisdiction": "US-TX,US-AZ"})) == {
        w["tx"],
        w["opp"],
    }
    assert _feed_ids(client.get("/feeds/events.json", params={"jurisdiction": "GB"})) == set()
    rss = client.get("/feeds/events.rss", params={"jurisdiction": "US-TX"})
    assert rss.status_code == 200 and w["tx"] in rss.text and w["ca"] not in rss.text


def test_events_feed_jurisdiction_does_not_reach_a_hidden_subject(client: TestClient, db: Session) -> None:
    w = _two_subject_world(db)
    w["texas"].publish_state = "unpublished"
    db.commit()
    assert _feed_ids(client.get("/feeds/events.json", params={"jurisdiction": "US-TX"})) == set()


def test_events_feed_applies_source_id(client: TestClient, db: Session) -> None:
    """Accepted and ignored until 2026-09-27: `?source_id=` returned every event."""
    w = _two_subject_world(db)
    only_other = _feed_ids(client.get("/feeds/events.json", params={"source_id": "us.test.other_source"}))
    assert only_other == {w["ca"]}


# ------------------------------------------------------------------------ proposals: map and feed
@pytest.fixture()
def sponsored(db: Session) -> dict[str, Any]:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    acme = make_org(db, "Acme Power LLC")
    hidden = make_org(db, "Hidden Holdings")
    hidden.publish_state = "unpublished"
    a = make_visible_proposal(db, src, public_id_suffix="1", sponsor=acme)
    b = make_visible_proposal(db, src, public_id_suffix="2", sponsor=hidden)
    c = make_visible_proposal(db, src, public_id_suffix="3")
    a.storage_mwh = 400
    c.storage_mwh = 50
    old = dt.datetime(2026, 1, 1, tzinfo=UTC)
    b.first_seen = old
    b.last_changed = old
    db.commit()
    return {
        "acme": acme.public_id,
        "hidden": hidden.public_id,
        "a": a.public_id,
        "b": b.public_id,
        "c": c.public_id,
    }


def test_proposal_feed_takes_the_lists_new_filters(client: TestClient, sponsored: dict[str, Any]) -> None:
    def feed(**params: str) -> set[str]:
        resp = client.get("/feeds/proposals.json", params=params)
        assert resp.status_code == 200, resp.text
        return {item["_platform"]["subject"]["public_id"] for item in resp.json()["items"]}

    s = sponsored
    assert feed(sponsor_id=s["acme"]) == {s["a"]}
    assert feed(sponsor_id=s["hidden"]) == set()
    assert feed(**{"storage_mwh[gte]": "100"}) == {s["a"]}
    assert feed(**{"first_seen[to]": "2026-06-01"}) == {s["b"]}
    assert feed(**{"last_changed[from]": "2026-06-01"}) == {s["a"], s["c"]}
    assert feed(updated_since="2026-06-01") == {s["a"], s["c"]}


def test_updated_since_is_last_changed_from(client: TestClient, sponsored: dict[str, Any]) -> None:
    """One meaning, the bulk stream's: `last_changed` at or after the instant (inclusive)."""
    for bound in ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.000001Z", "2026-06-01"):
        sync = _ids(client.get("/v1/proposals", params={"updated_since": bound}))
        window = _ids(client.get("/v1/proposals", params={"last_changed[from]": bound}))
        assert sync == window
    assert sponsored["b"] in _ids(
        client.get("/v1/proposals", params={"updated_since": "2026-01-01T00:00:00Z"})
    )
    assert sponsored["b"] not in _ids(
        client.get("/v1/proposals", params={"updated_since": "2026-01-01T00:00:00.000001Z"})
    )


def test_proposal_map_counts_follow_the_new_filters(client: TestClient, sponsored: dict[str, Any]) -> None:
    resp = client.get(
        "/v1/proposals/geo", params={"bbox": "-180,-90,180,90", "zoom": "3", "sponsor_id": sponsored["acme"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["totals"]["records"] == 1


@pytest.mark.parametrize(
    ("path", "params", "field"),
    [
        ("/v1/proposals", {"storage_mwh[gte]": "lots"}, "storage_mwh[gte]"),
        ("/v1/proposals", {"first_seen[from]": "last week"}, "first_seen[from]"),
        ("/v1/proposals", {"updated_since": "yesterday"}, "updated_since"),
        ("/feeds/proposals.json", {"last_changed[to]": "soon"}, "last_changed[to]"),
        ("/v1/opportunities", {"open_at[from]": "2026-01-01T00:00:00Z"}, "open_at[from]"),
        ("/v1/opportunities", {"budget_amount[gte]": "1000000"}, "budget_amount[gte]"),
        (
            "/v1/opportunities",
            {"budget_amount[gte]": "1000000", "budget_currency": "EUR,USD"},
            "budget_amount[gte]",
        ),
        ("/v1/opportunities", {"budget_currency": "euro"}, "budget_currency"),
        ("/feeds/opportunities.json", {"budget_amount[gte]": "5"}, "budget_amount[gte]"),
        (
            "/v1/opportunities/geo",
            {"bbox": "-180,-90,180,90", "zoom": "3", "capacity_sought_mw[gte]": "x"},
            "capacity_sought_mw[gte]",
        ),
    ],
)
def test_malformed_or_undefined_bounds_are_a_400(
    client: TestClient, db: Session, path: str, params: dict[str, str], field: str
) -> None:
    _problem(client.get(path, params=params), "validation_error", field)


# ------------------------------------------------------------------------------- opportunities
def test_budget_bound_never_compares_across_currencies(client: TestClient, db: Session) -> None:
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    eur = make_visible_opportunity(db, src, public_id_suffix="1")
    czk = make_visible_opportunity(db, src, public_id_suffix="2")
    none = make_visible_opportunity(db, src, public_id_suffix="3")
    eur.budget_amount, eur.budget_currency = 1_000_000, "EUR"
    czk.budget_amount, czk.budget_currency = 25_000_000, "CZK"
    none.budget_amount, none.budget_currency = None, "EUR"
    db.commit()
    got = _ids(
        client.get("/v1/opportunities", params={"budget_currency": "EUR", "budget_amount[gte]": "1000000"})
    )
    assert got == {eur.public_id}
    assert _ids(client.get("/v1/opportunities", params={"budget_currency": "EUR"})) == {
        eur.public_id,
        none.public_id,
    }
    assert _ids(
        client.get("/v1/opportunities", params={"budget_currency": "CZK", "budget_amount[gte]": "1000000"})
    ) == {czk.public_id}
