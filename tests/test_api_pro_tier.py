"""Tier visibility: public vs pro vs api on the same record (task brief item 2 and its named
test). `services/api/visibility.py`'s `entitlement` parameter reads `public_at` for `public` and
`published_at` for `pro`/`api`; a `restricted`/`unknown` source stays invisible on every
non-admin tier, Pro and API included (the standing, stricter reading — docs/21 D-2, `CLAUDE.md`).
"""

from __future__ import annotations

import datetime as dt

from services.api.conftest import (
    make_attribution_licence,
    make_event,
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.db.models import Licence, Source
from tests.conftest import make_account, make_api_key, make_user

UTC = dt.UTC


def _make_pending_proposal(db, source, *, published_at, public_at):
    """A proposal whose `published_at` has already passed but whose `public_at` (lag applied)
    has not — visible live (Pro/API), not yet visible delayed (public). Mirrors
    `make_visible_proposal` but with independent control of both timestamps."""
    prop = make_visible_proposal(db, source, public_id_suffix="1", public_at=public_at)
    prop.published_at = published_at
    db.flush()
    return prop


def test_pro_and_api_see_a_record_at_published_at_before_public_at(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    now = dt.datetime.now(UTC)
    # published five minutes ago (live tiers should see it); public_at (lag applied) is still
    # ten days in the future (the public tier must not see it yet).
    prop = _make_pending_proposal(
        db, src, published_at=now - dt.timedelta(minutes=5), public_at=now + dt.timedelta(days=10)
    )
    db.commit()

    public_resp = client.get("/v1/proposals")
    assert prop.public_id not in [p["public_id"] for p in public_resp.json()["data"]]
    assert public_resp.json()["meta"]["tier"] == "public"

    pro_account = make_account(db, entitlement="pro")
    pro_user = make_user(db, pro_account, email="pro@example.com")
    _key, secret = make_api_key(db, pro_account, pro_user, scopes=["read:live"])
    db.commit()

    pro_resp = client.get("/v1/proposals", headers={"Authorization": f"Bearer {secret}"})
    assert pro_resp.status_code == 200
    assert pro_resp.json()["meta"]["tier"] == "pro"
    assert pro_resp.json()["meta"]["lag_days"] == 0
    assert prop.public_id in [p["public_id"] for p in pro_resp.json()["data"]]

    api_account = make_account(db, entitlement="api")
    api_user = make_user(db, api_account, email="api@example.com")
    _key2, secret2 = make_api_key(db, api_account, api_user, scopes=["read:live", "read:bulk"])
    db.commit()

    api_resp = client.get("/v1/proposals", headers={"Authorization": f"Bearer {secret2}"})
    assert api_resp.status_code == 200
    assert api_resp.json()["meta"]["tier"] == "api"
    assert prop.public_id in [p["public_id"] for p in api_resp.json()["data"]]


def test_public_sees_the_same_record_once_public_at_arrives(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    now = dt.datetime.now(UTC)
    prop = _make_pending_proposal(
        db, src, published_at=now - dt.timedelta(days=20), public_at=now - dt.timedelta(seconds=1)
    )
    db.commit()
    resp = client.get("/v1/proposals")
    assert prop.public_id in [p["public_id"] for p in resp.json()["data"]]


def test_proposal_detail_and_events_respect_the_same_tier_split(client, db):
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    now = dt.datetime.now(UTC)
    prop = _make_pending_proposal(
        db, src, published_at=now - dt.timedelta(minutes=1), public_at=now + dt.timedelta(days=5)
    )
    ev = make_event(db, prop, src, public_at=now + dt.timedelta(days=5))
    ev.published_at = now - dt.timedelta(minutes=1)
    db.commit()

    assert client.get(f"/v1/proposals/{prop.public_id}").status_code == 404

    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live"])
    db.commit()
    headers = {"Authorization": f"Bearer {secret}"}

    detail = client.get(f"/v1/proposals/{prop.public_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["meta"]["tier"] == "pro"

    events = client.get(f"/v1/proposals/{prop.public_id}/events", headers=headers)
    assert events.status_code == 200
    assert len(events.json()["data"]) == 1


def test_restricted_source_is_invisible_on_public_pro_and_api(client, db):
    restricted = Licence(
        id="restricted-lic",
        name="Restricted Licence",
        reuse_class="restricted",
        gate_flag=True,
    )
    db.add(restricted)
    db.flush()
    src = Source(
        id="us.test.restricted_source",
        name="Restricted Source",
        category="generation_queue",
        url="https://example.org/restricted",
        access="html",
        cadence="weekly",
        licence_id=restricted.id,
        publish_state="public",
    )
    db.add(src)
    db.flush()
    prop = make_visible_proposal(db, src, public_id_suffix="2")
    prop.min_reuse_class = "restricted"
    db.commit()

    public_ids = [p["public_id"] for p in client.get("/v1/proposals").json()["data"]]
    assert prop.public_id not in public_ids

    account = make_account(db, entitlement="api")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live", "read:bulk"])
    db.commit()
    resp = client.get("/v1/proposals", headers={"Authorization": f"Bearer {secret}"})
    ids = [p["public_id"] for p in resp.json()["data"]]
    assert prop.public_id not in ids, "restricted sources must stay invisible on API too (docs/21 D-2)"

    assert client.get(f"/v1/proposals/{prop.public_id}", headers={"Authorization": f"Bearer {secret}"}).status_code == 404


def test_api_only_source_is_visible_to_pro_but_not_public(client, db):
    """`source.publish_state = 'api_only'` (cleared by licence/ingest but not yet flipped to the
    public surface): invisible to `public`, visible to `pro`/`api` (services/api/visibility.py
    `_PERMITTED_SOURCE_STATES`)."""
    lic = make_attribution_licence(db, "attr-api-only")
    src = make_public_source(db, lic, id_="us.test.api_only_source")
    src.publish_state = "api_only"
    prop = make_visible_proposal(db, src, public_id_suffix="3")
    db.commit()

    assert prop.public_id not in [p["public_id"] for p in client.get("/v1/proposals").json()["data"]]

    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    _key, secret = make_api_key(db, account, user, scopes=["read:live"])
    db.commit()
    resp = client.get("/v1/proposals", headers={"Authorization": f"Bearer {secret}"})
    assert prop.public_id in [p["public_id"] for p in resp.json()["data"]]
