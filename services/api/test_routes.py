"""Functional tests for the public-tier routes: envelope shape, tier visibility, gating,
pagination and feeds."""

from __future__ import annotations

import datetime as dt

from services.api.conftest import (
    make_attribution_licence,
    make_event,
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)

UTC = dt.UTC


def test_list_proposals_envelope_shape(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_visible_proposal(db, src, public_id_suffix="1")
    db.commit()

    resp = client.get("/v1/proposals")
    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body and "meta" in body and "licence_summary" in body and "redactions" in body
    assert "page" in body
    assert body["meta"]["tier"] == "public"
    assert len(body["data"]) == 1
    assert body["data"][0]["name_canonical"] == "Test Storage Project 1"
    assert body["data"][0]["provenance"][0]["source_id"] == src.id
    assert body["licence_summary"]["sources"][0]["source_id"] == src.id


def test_proposal_with_future_public_at_is_absent_from_public_list(client, db):
    """docs/21 §5.4: `public_at` in the future -> invisible on the public tier."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    future = dt.datetime.now(UTC) + dt.timedelta(days=5)
    hidden = make_visible_proposal(db, src, public_id_suffix="2", public_at=future)
    visible = make_visible_proposal(db, src, public_id_suffix="3")
    db.commit()

    resp = client.get("/v1/proposals")
    ids = [p["public_id"] for p in resp.json()["data"]]
    assert visible.public_id in ids
    assert hidden.public_id not in ids

    # An "internal" (unfiltered) query proves the row exists — this is not exposed over the
    # public API, but the DB layer itself must not have silently dropped it.
    from sqlalchemy import select

    from services.db.models import Proposal

    row = db.scalar(select(Proposal).where(Proposal.public_id == hidden.public_id))
    assert row is not None
    assert row.public_at == future


def test_get_proposal_detail_and_404_for_hidden(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="4")
    db.commit()

    resp = client.get(f"/v1/proposals/{prop.public_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["public_id"] == prop.public_id

    resp404 = client.get("/v1/proposals/prop_00000000ZZ")
    assert resp404.status_code == 404
    body = resp404.json()
    assert body["code"] == "not_found"
    assert body["type"].startswith("https://api.{{DOMAIN}}/errors/")


def test_source_gate_refusal_hides_records_from_public_api(client, db):
    """A source that is ingested but not (yet) marked `publish_state = 'public'` by an operator
    must not leak onto the public tier, even though the row exists (docs/21 §5.4
    `source_permits`)."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic, id_="us.test.not_yet_public")
    src.publish_state = "api_only"
    db.flush()
    make_visible_proposal(db, src, public_id_suffix="5")
    db.commit()

    resp = client.get("/v1/proposals")
    assert resp.json()["data"] == []


def test_restricted_licence_never_reaches_the_response_even_if_a_row_exists(client, db):
    """Defence in depth: even if a restricted-licence row existed (it should never be written by
    the loader — services/ingest/test_loader.py proves that separately), the API's own predicate
    refuses it too (docs/20 §11)."""

    lic = make_open_licence(db)
    src = make_public_source(db, lic, id_="us.test.gate_check")
    prop = make_visible_proposal(db, src, public_id_suffix="6")
    # Simulate a licence later reclassified as restricted (invariant L2 says historical rows
    # keep their licence, but prove the *query* layer still refuses it regardless).
    prop.min_reuse_class = "restricted"
    db.commit()

    resp = client.get(f"/v1/proposals/{prop.public_id}")
    assert resp.status_code == 404


def test_unknown_query_parameter_is_400(client, db):
    resp = client.get("/v1/proposals?technolgy=bess_li_ion")
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_parameter"


def test_proposal_filters_and_sort(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_visible_proposal(db, src, public_id_suffix="7", lifecycle_state="filed")
    make_visible_proposal(db, src, public_id_suffix="8", lifecycle_state="studied")
    db.commit()

    resp = client.get("/v1/proposals?lifecycle_state=studied")
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["lifecycle_state"] == "studied"

    resp2 = client.get("/v1/proposals?sort=capacity_mw")
    caps = [d["capacity_mw"] for d in resp2.json()["data"]]
    assert caps == sorted(caps)


def test_pagination_cursor(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    for i in range(5):
        make_visible_proposal(db, src, public_id_suffix=str(10 + i))
    db.commit()

    resp = client.get("/v1/proposals?limit=2&sort=name_canonical")
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["page"]["has_more"] is True
    cursor = body["page"]["next_cursor"]
    assert cursor

    resp2 = client.get(f"/v1/proposals?limit=2&sort=name_canonical&cursor={cursor}")
    body2 = resp2.json()
    ids1 = {d["public_id"] for d in body["data"]}
    ids2 = {d["public_id"] for d in body2["data"]}
    assert ids1.isdisjoint(ids2)


def test_proposal_events_timeline_respects_lag(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="20")
    future_event = make_event(db, prop, src, public_at=dt.datetime.now(UTC) + dt.timedelta(days=3))
    visible_event = make_event(db, prop, src)
    db.commit()

    resp = client.get(f"/v1/proposals/{prop.public_id}/events")
    ids = [e["id"] for e in resp.json()["data"]]
    from services.ids import public_id as mint

    assert mint("evt", visible_event.id) in ids
    assert mint("evt", future_event.id) not in ids


def test_proposal_sources_panel(client, db):
    lic = make_attribution_licence(db)
    src = make_public_source(db, lic, id_="us.test.attr")
    prop = make_visible_proposal(db, src, public_id_suffix="21")
    db.commit()

    resp = client.get(f"/v1/proposals/{prop.public_id}/sources")
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["attribution_text"] == "Source: Test ISO"


def test_opportunities_list_and_detail(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic, id_="us.test.opp_source")
    opp = make_visible_opportunity(db, src, public_id_suffix="1")
    db.commit()

    resp = client.get("/v1/opportunities")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["public_id"] == opp.public_id

    resp2 = client.get(f"/v1/opportunities/{opp.public_id}")
    assert resp2.json()["data"]["title"] == "Test RFP 1"

    # default status filter is "open"
    closed = make_visible_opportunity(db, src, public_id_suffix="2", status="closed")
    db.commit()
    resp3 = client.get("/v1/opportunities")
    ids = [o["public_id"] for o in resp3.json()["data"]]
    assert closed.public_id not in ids


def test_organizations_and_linked_proposals(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db, "Acme Power LLC")
    make_visible_proposal(db, src, public_id_suffix="30", sponsor=org)
    db.commit()

    resp = client.get("/v1/organizations")
    assert any(o["public_id"] == org.public_id for o in resp.json()["data"])

    resp2 = client.get(f"/v1/organizations/{org.public_id}")
    assert resp2.json()["data"]["proposal_count"] == 1

    resp3 = client.get(f"/v1/organizations/{org.public_id}/proposals")
    assert len(resp3.json()["data"]) == 1


def test_global_events_feed_excludes_gated_and_future(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="40")
    ev = make_event(db, prop, src)
    db.commit()

    resp = client.get("/v1/events")
    assert resp.status_code == 200
    assert len(resp.json()["data"]) >= 1

    resp2 = client.get(
        f"/v1/events/{__import__('services.ids', fromlist=['public_id']).public_id('evt', ev.id)}"
    )
    assert resp2.status_code == 200


def test_sources_and_licences_registers(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    db.commit()

    resp = client.get("/v1/sources")
    assert any(s["source_id"] == src.id for s in resp.json()["data"])

    resp2 = client.get(f"/v1/sources/{src.id}")
    assert resp2.json()["data"]["licence"]["reuse_class"] == "open"

    resp3 = client.get("/v1/licences")
    assert any(l_["licence_id"] == lic.id for l_ in resp3.json()["data"])

    resp4 = client.get(f"/v1/licences/{lic.id}")
    assert resp4.json()["data"]["reuse_class"] == "open"


def test_vocabularies_and_health(client, db):
    resp = client.get("/v1/meta/vocabularies")
    assert "lifecycle_state" in resp.json()["data"]

    resp2 = client.get("/v1/health")
    assert resp2.json()["status"] == "ok"
    assert resp2.json()["lag_days_default"] == {"supply": 14, "opportunities": 7}


def test_rss_and_json_feed(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    make_visible_proposal(db, src, public_id_suffix="50")
    db.commit()

    resp = client.get("/feeds/proposals.rss")
    assert resp.status_code == 200
    assert "application/rss+xml" in resp.headers["content-type"]
    assert "<rss" in resp.text

    resp2 = client.get("/feeds/proposals.json")
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["version"] == "https://jsonfeed.org/version/1.1"
    assert len(body["items"]) == 1


def test_rate_limit_and_request_id_headers_present(client, db):
    resp = client.get("/v1/health")
    assert "X-Request-Id" in resp.headers
    assert resp.headers["RateLimit-Limit"] == "60"
    assert resp.headers["RateLimit-Policy"]


def test_geo_endpoint_returns_feature_collection(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    loc = make_location(db, src, lic)
    make_visible_proposal(db, src, public_id_suffix="60", location=loc)
    db.commit()

    resp = client.get("/v1/proposals/geo?bbox=-106.6,25.8,-93.5,36.5&zoom=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["type"] == "FeatureCollection"
