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
    assert body["type"].startswith("https://api.infraque.com/errors/")


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
    # Nothing is delayed on any tier: records since 2026-09-19 and change events since
    # 2026-09-21, when the ISO change-event delay went along with its per-source knob (owner).
    # `/v1/health` is where `web/` reads the number from, and the `iso_change_events` key is
    # gone rather than zeroed -- a key named after a delay that no longer exists would invite a
    # page to print one.
    assert resp2.json()["lag_days_default"] == {"supply": 0, "opportunities": 0}


def test_health_queue_check_is_not_applicable_on_sqlite(client, db):
    """`checks.queue` was hard-coded `true` (docs/60 §11 item 9). On SQLite there is no job queue
    at all, so it says `null` -- neither a pass nor a failure -- and `status` is unaffected."""
    body = client.get("/v1/health").json()
    assert body["checks"]["queue"] is None
    assert body["status"] == "ok"


class _PgStub:
    """Just enough of a `Session` for `queue_schema_present`: a Postgres dialect and a scripted
    answer (or error) for the one catalogue query."""

    def __init__(self, answer):
        self.answer = answer
        self.seen = []

    def get_bind(self):
        class _Bind:
            class dialect:  # noqa: N801 -- mirrors SQLAlchemy's attribute name
                name = "postgresql"

        return _Bind()

    def execute(self, statement, params=None):
        self.seen.append((str(statement), params))
        if isinstance(self.answer, Exception):
            raise self.answer

        class _Result:
            def scalar(inner):  # noqa: N805
                return self.answer

        return _Result()


def test_health_queue_check_on_postgres_is_one_catalogue_lookup():
    from infra.scheduler.queue_schema import SENTINEL_TABLE
    from services.api.app import QUEUE_SENTINEL_TABLE, queue_schema_present

    assert QUEUE_SENTINEL_TABLE == SENTINEL_TABLE  # the API checks the table the deploy step installs
    present = _PgStub(True)
    assert queue_schema_present(present) is True
    assert "to_regclass" in present.seen[0][0]
    assert present.seen[0][1] == {"t": "procrastinate_jobs"}
    assert queue_schema_present(_PgStub(False)) is False
    assert queue_schema_present(_PgStub(None)) is False  # a NULL answer reads as absent, never as present
    assert queue_schema_present(_PgStub(RuntimeError("connection lost"))) is False


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


def test_every_feed_item_carries_its_attribution(client, db):
    """CLAUDE.md: attribution renders automatically on every surface; docs/23 §10 names the fields.
    Before 2026-09-27 an RSS item carried only `dc:creator` and a link, and the JSON Feed's
    `_platform.provenance` was always `[]`. Now each item names `source_id`, `source_url`,
    `retrieved_at` and `licence` in a declared namespace (plus a Dublin Core `dc:source`), and the
    credit line is in the description, on both formats."""
    import xml.etree.ElementTree as ET

    from services.api.feeds import PROVENANCE_NS

    lic = make_attribution_licence(db)
    src = make_public_source(db, lic)
    make_visible_proposal(db, src, public_id_suffix="51")
    db.commit()

    resp = client.get("/feeds/proposals.rss")
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)  # noqa: S314 -- our own output, parsed to prove it is well-formed
    ns = {"infraque": PROVENANCE_NS, "dc": "http://purl.org/dc/elements/1.1/"}
    items = root.findall("./channel/item")
    assert len(items) == 1
    item = items[0]
    rows = item.findall("infraque:provenance", ns)
    assert len(rows) == 1
    fields = {child.tag.split("}")[1]: child.text for child in rows[0]}
    assert fields["source_id"] == "us.test.public_source"
    assert fields["source_url"] == "https://example.org/queue#51"
    assert fields["licence"] == lic.id
    assert fields["reuse_class"] == "attribution"
    assert fields["attribution"] == "Source: Test ISO"
    assert fields["retrieved_at"].endswith("Z")
    assert [e.text for e in item.findall("dc:source", ns)] == ["https://example.org/queue#51"]
    assert item.findtext("description").endswith("— Source: Test ISO")

    feed = client.get("/feeds/proposals.json").json()
    ext = feed["items"][0]["_platform"]
    assert [(p["source_id"], p["source_url"], p["licence_id"]) for p in ext["provenance"]] == [
        ("us.test.public_source", "https://example.org/queue#51", lic.id)
    ]
    assert ext["provenance"][0]["retrieved_at"] == fields["retrieved_at"]
    assert feed["items"][0]["content_text"].endswith("— Source: Test ISO")


def test_feed_renderer_escapes_and_skips_absent_provenance():
    """An item with no provenance rows (a user-actor event) renders exactly as before, credited to
    its `creator`; hostile text in a provenance field is escaped, never markup."""
    import xml.etree.ElementTree as ET

    from services.api.feeds import render_rss

    base = {
        "title": "t",
        "url": "https://example.test/x",
        "guid": "g",
        "pub_date": dt.datetime(2026, 9, 27, tzinfo=dt.UTC),
        "creator": "the platform",
        "description": "d",
    }
    hostile = {
        "source_id": "s",
        "source_name": "n",
        "source_url": "https://example.test/?a=1&b=<2>",
        "retrieved_at": "2026-09-27T00:00:00Z",
        "licence_id": "l",
        "reuse_class": "open",
        "attribution_text": "</infraque:attribution><script>",
    }
    xml = render_rss(
        resource="Events",
        kind="proposal",
        self_url="https://example.test/feed",
        items=[
            {**base, "platform_ext": {"provenance": []}},
            {**base, "guid": "h", "platform_ext": {"provenance": [hostile]}},
        ],
    )
    root = ET.fromstring(xml)  # noqa: S314 -- our own output
    first, second = root.findall("./channel/item")
    assert first.findtext("description") == "d — the platform"
    assert "provenance" not in ET.tostring(first, encoding="unicode")
    assert "<script>" not in xml
    ns = {"infraque": "https://infraque.com/ns/feed-provenance/1"}
    assert (
        second.find("infraque:provenance/infraque:attribution", ns).text == "</infraque:attribution><script>"
    )


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


def test_geo_bbox_is_enforced(client, db):
    """web/README.md "Missing from the API" item 4, fixed: `bbox` used to be accepted and echoed
    but never applied, so panning the map re-fetched the same full result set every request. One
    proposal placed in Texas, one in New York; a Texas-only bbox must return only the Texas one."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    # `precision="exact"` (ADR 0008): this test is about bbox scoping of point/cluster features,
    # not about region grouping (`services/api/test_placement.py` covers that) -- the default
    # `county_centroid` precision would render both as `region` features instead.
    tx = make_location(
        db, src, lic, geom=(-97.7, 30.3), precision="exact", county_name="Travis", state_code="US-TX"
    )
    ny = make_location(
        db, src, lic, geom=(-73.9, 42.9), precision="exact", county_name="Albany", state_code="US-NY"
    )
    make_visible_proposal(db, src, public_id_suffix="701", location=tx)
    make_visible_proposal(db, src, public_id_suffix="702", location=ny)
    db.commit()

    tx_bbox = "-107,25,-93,37"
    resp = client.get(f"/v1/proposals/geo?bbox={tx_bbox}&zoom=8")
    assert resp.status_code == 200
    body = resp.json()
    features = body["data"]["features"]
    assert len(features) == 1
    assert features[0]["properties"]["feature_kind"] == "proposal"
    assert features[0]["geometry"]["coordinates"][0] < -93  # the Texas point, not the NY one

    # totals stay scoped to the whole filter match, not the viewport (docs/04 D-8: a record isn't
    # dropped just because a narrower bbox was asked for)
    assert body["data"]["totals"]["records"] == 2

    world_resp = client.get("/v1/proposals/geo?bbox=-179,-85,179,85&zoom=8")
    assert len(world_resp.json()["data"]["features"]) == 2


def test_geo_unplaced_records_are_counted_not_dropped(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    placed = make_location(db, src, lic, geom=(-97.7, 30.3))
    unplaced = make_location(db, src, lic, geom=None, precision="unknown", county_name=None, state_code=None)
    make_visible_proposal(db, src, public_id_suffix="710", location=placed)
    make_visible_proposal(db, src, public_id_suffix="711", location=unplaced)
    make_visible_proposal(db, src, public_id_suffix="712", location=None)
    db.commit()

    resp = client.get("/v1/proposals/geo?bbox=-179,-85,179,85&zoom=5")
    body = resp.json()
    assert len(body["data"]["features"]) == 1
    assert body["data"]["totals"]["records"] == 3
    assert body["meta"]["unplaced_count"] == 2


def test_geo_restricted_precision_reason_renders_for_derived_only_sources(client, db):
    """ADR 0008 (2026-09-18): a `county_centroid` location — whether the source only ever gave a
    county, or an exact one was downgraded by a derived-only licence (docs/04 D-9) — is now the
    `region` placement grade and renders as a `region` feature, grouped with every other proposal
    in that county, not as an individual point carrying its own `precision_reason`. The
    restricted-precision *rule itself* (never publish the exact point) still holds — it now holds
    by construction, since a `region` feature never carries a point geometry finer than the
    region's own representative centroid."""
    lic = make_attribution_licence(db)
    lic.allows_raw_publication = False
    src = make_public_source(db, lic, id_="us.test.derived_only_source")
    loc = make_location(db, src, lic, geom=(-97.7, 30.3), precision_reason="licence")
    make_visible_proposal(db, src, public_id_suffix="720", location=loc)
    db.commit()

    resp = client.get("/v1/proposals/geo?bbox=-179,-85,179,85&zoom=10")
    feature = resp.json()["data"]["features"][0]
    assert feature["properties"]["feature_kind"] == "region"
    assert feature["properties"]["region_level"] == "county"
    assert feature["properties"]["count"] == 1
    assert "precision_reason" not in feature["properties"]


def test_slug_filter_looks_up_a_proposal_and_an_opportunity(client, db):
    """web/README.md "Missing from the API" item 1: the site had no `slug=` filter and no
    slug-keyed alias, so it scanned up to 200 search results to map a slug back to a record."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="740")
    opp = make_visible_opportunity(db, src, public_id_suffix="slug1")
    db.commit()

    prop_resp = client.get(f"/v1/proposals?slug={prop.slug}")
    assert prop_resp.status_code == 200
    assert [p["public_id"] for p in prop_resp.json()["data"]] == [prop.public_id]

    opp_resp = client.get(f"/v1/opportunities?slug={opp.slug}")
    assert opp_resp.status_code == 200
    assert [o["public_id"] for o in opp_resp.json()["data"]] == [opp.public_id]

    miss_resp = client.get("/v1/proposals?slug=not-a-real-slug")
    assert miss_resp.json()["data"] == []


def test_opportunities_technologies_filter_is_enforced(client, db):
    """web/README.md "Missing from the API" item 5: `technologies` was allowlisted and accepted
    but silently ignored. Any-of match; an all-source opportunity (empty `technologies[]`) matches
    every value (api/openapi.yaml `Technologies` parameter)."""
    from services.db.models import Opportunity

    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    solar = make_visible_opportunity(db, src, public_id_suffix="solar1")  # default technologies=[solar_pv]
    wind = make_visible_opportunity(db, src, public_id_suffix="wind1")
    db.get(Opportunity, wind.id).technologies = ["wind"]
    all_source = make_visible_opportunity(db, src, public_id_suffix="allsrc1")
    db.get(Opportunity, all_source.id).technologies = []
    db.commit()

    resp = client.get("/v1/opportunities?status=open&technologies=wind")
    ids = {o["public_id"] for o in resp.json()["data"]}
    assert ids == {wind.public_id, all_source.public_id}
    assert solar.public_id not in ids

    resp2 = client.get("/v1/opportunities?status=open&technologies=solar_pv")
    ids2 = {o["public_id"] for o in resp2.json()["data"]}
    assert ids2 == {solar.public_id, all_source.public_id}


def test_organization_detail_route(client, db):
    """`GET /v1/organizations/{public_id}` (api/openapi.yaml `getOrganization`) -- already
    implemented before this task; this asserts the counts and 404 boundary explicitly."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db, name="Acme Power LLC")
    make_visible_proposal(db, src, public_id_suffix="731", sponsor=org)
    db.commit()

    resp = client.get(f"/v1/organizations/{org.public_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["public_id"] == org.public_id
    assert body["data"]["proposal_count"] == 1
    assert body["data"]["opportunity_count"] == 0

    assert client.get("/v1/organizations/org_doesnotexist").status_code == 404


def test_source_and_licence_expose_a_quote_text_field(client, db):
    """web/README.md "Missing from the API" item 3: no field carried `data/sources.yaml`'s
    free-text `license` clause; `web/viewmodels.py` had to compose a paraphrase instead of quoting
    anything. `Licence.quote_text` (services/db/models.py) now carries it; `notes` (data-engineer
    commentary, sometimes about in-progress legal review) stays out of the public shape."""
    lic = make_open_licence(db)
    lic.quote_text = 'Terms of Use: "raw data ... may be used, reproduced, and redistributed."'
    lic.notes = "Internal-only commentary that must never appear on the public API."
    src = make_public_source(db, lic)
    db.commit()

    resp = client.get(f"/v1/sources/{src.id}")
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["licence"]["quote_text"] == lic.quote_text
    assert "notes" not in body["licence"]

    lic_resp = client.get(f"/v1/licences/{lic.id}")
    assert lic_resp.json()["data"]["quote_text"] == lic.quote_text


# ---- `q` matches name, sponsor/issuer organisation, or an active source record id -------------
# (web/templates/base.html promises "Search by name, sponsor, queue ID"; until 2026-09-15 the API
# matched the canonical name only, so a developer's name found nothing.)


def test_proposal_q_matches_sponsor_name_and_source_record_id(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    sponsor = make_org(db, "Tallgrass Energy Partners")
    by_sponsor = make_visible_proposal(db, src, public_id_suffix="41", sponsor=sponsor)
    other = make_visible_proposal(db, src, public_id_suffix="42")
    db.commit()

    ids = lambda resp: [p["public_id"] for p in resp.json()["data"]]  # noqa: E731
    hit = client.get("/v1/proposals", params={"q": "tallgrass"})
    assert ids(hit) == [by_sponsor.public_id]
    by_record = client.get("/v1/proposals", params={"q": "Q42"})
    assert ids(by_record) == [other.public_id]
    by_name = client.get("/v1/proposals", params={"q": "storage project 41"})
    assert ids(by_name) == [by_sponsor.public_id]
    miss = client.get("/v1/proposals", params={"q": "no such developer"})
    assert ids(miss) == []


def test_opportunity_q_matches_issuer_name_and_source_record_id(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    issuer = make_org(db, "Salt River Project")
    by_issuer = make_visible_opportunity(db, src, public_id_suffix="51")
    by_issuer.issuer_org_id = issuer.id
    other = make_visible_opportunity(db, src, public_id_suffix="52")
    db.commit()

    ids = lambda resp: [o["public_id"] for o in resp.json()["data"]]  # noqa: E731
    assert ids(client.get("/v1/opportunities", params={"q": "salt river"})) == [by_issuer.public_id]
    assert ids(client.get("/v1/opportunities", params={"q": "N52"})) == [other.public_id]
    assert ids(client.get("/v1/opportunities", params={"q": "rfp 51"})) == [by_issuer.public_id]
    assert ids(client.get("/v1/opportunities", params={"q": "nothing here"})) == []


def test_organizations_list_filters_by_slug(client, db):
    """The company page resolves a slug through the list endpoint, as proposal pages do."""
    org = make_org(db, "Slug Filter Power LLC")
    make_org(db, "Another Power LLC")
    db.commit()
    resp = client.get("/v1/organizations", params={"slug": org.slug})
    assert [o["public_id"] for o in resp.json()["data"]] == [org.public_id]
    assert client.get("/v1/organizations", params={"slug": "no-such-slug"}).json()["data"] == []


def test_cursor_pagination_survives_a_null_sort_value(client, db):
    """API audit 2026-09-18 (S5): `due_at` is nullable and the default opportunity sort; a page
    ending on a NULL value produced a cursor that 500ed on the next request and, on Postgres,
    silently dropped NULL rows. Every row must be seen exactly once, in either direction."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    made = [make_visible_opportunity(db, src, public_id_suffix=str(60 + i)) for i in range(7)]
    for opp in made[::2]:
        opp.due_at = None
    db.commit()
    expected = {o.public_id for o in made}

    for sort in ("due_at", "-due_at"):
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            params = {"limit": 2, "sort": sort, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            resp = client.get("/v1/opportunities", params=params)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            seen += [o["public_id"] for o in body["data"]]
            if not body["page"]["has_more"]:
                break
            cursor = body["page"]["next_cursor"]
        assert sorted(seen) == sorted(expected), sort
        assert len(seen) == len(set(seen)), sort


def test_organization_proposals_apply_technology_and_jurisdiction(client, db):
    """Regression: `check_allowed` accepted `technology` and `jurisdiction` from the day this
    endpoint landed, but the handler never applied either, so `?technology=solar` returned the
    whole list and no error (found 2026-09-20). Both now filter the way `kind` already did."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    org = make_org(db, "Mixed Fleet Power LLC")
    solar = make_visible_proposal(
        db, src, public_id_suffix="31", sponsor=org, technology="solar", jurisdiction="US-TX"
    )
    gas = make_visible_proposal(
        db, src, public_id_suffix="32", sponsor=org, technology="gas_ct", jurisdiction="US-CO"
    )
    db.commit()

    path = f"/v1/organizations/{org.public_id}/proposals"
    assert {p["public_id"] for p in client.get(path).json()["data"]} == {solar.public_id, gas.public_id}

    only_gas = client.get(path, params={"technology": "gas_ct"}).json()["data"]
    assert [p["public_id"] for p in only_gas] == [gas.public_id]

    # CSV is an OR, the same grammar `GET /v1/proposals` uses.
    both = client.get(path, params={"technology": "gas_ct,solar"}).json()["data"]
    assert {p["public_id"] for p in both} == {solar.public_id, gas.public_id}

    only_co = client.get(path, params={"jurisdiction": "US-CO"}).json()["data"]
    assert [p["public_id"] for p in only_co] == [gas.public_id]

    # The two compose, and a combination nothing matches is empty rather than unfiltered.
    assert client.get(path, params={"technology": "solar", "jurisdiction": "US-CO"}).json()["data"] == []
