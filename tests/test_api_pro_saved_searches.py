"""Saved searches: CRUD with the list-endpoint filter grammar, quota, preview, and the private
RSS/JSON feed token (task brief item 3; docs/21-data-model.md §3.15; docs/23-api-spec-outline.md
§3.2, §9.2)."""

from __future__ import annotations

import datetime as dt

from services.alerts.feed import RSS_TOKEN_ALPHABET, generate_rss_token
from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal
from services.api.pro import SAVED_SEARCH_QUOTA
from tests.conftest import login, make_account, make_user

UTC = dt.UTC


def _login_pro(client, db):
    account = make_account(db, entitlement="pro")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    return account, user


def test_create_and_list_saved_search(client, db):
    _login_pro(client, db)
    resp = client.post(
        "/v1/saved-searches",
        json={
            "name": "TX storage 50-500 MW",
            "entity": "proposal",
            "query": {"kind": "storage", "jurisdiction": "US-TX"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["name"] == "TX storage 50-500 MW"
    assert data["delivery_mode"] == "daily"
    assert data["channels"] == ["email"]
    assert data["watermark_seq"] == 0
    assert data["rss_url"] is None

    listing = client.get("/v1/saved-searches").json()["data"]
    assert len(listing) == 1
    assert listing[0]["saved_search_id"] == data["saved_search_id"]


def test_saved_search_requires_pro_entitlement(client, db):
    account = make_account(db, entitlement="public")
    user = make_user(db, account)
    db.commit()
    login(client, db, user)
    resp = client.get("/v1/saved-searches")
    assert resp.status_code == 403


def test_saved_search_channel_rss_mints_a_token(client, db):
    _login_pro(client, db)
    resp = client.post(
        "/v1/saved-searches",
        json={"name": "RSS one", "entity": "proposal", "query": {}, "channels": ["email", "rss"]},
    )
    data = resp.json()["data"]
    assert data["rss_url"] is not None
    assert "/feeds/saved/rt_" in data["rss_url"]


def test_update_saved_search_toggles_rss_token(client, db):
    _login_pro(client, db)
    created = client.post(
        "/v1/saved-searches", json={"name": "n", "entity": "proposal", "query": {}, "channels": ["email"]}
    ).json()["data"]
    sid = created["saved_search_id"]

    added = client.patch(f"/v1/saved-searches/{sid}", json={"channels": ["email", "rss"]}).json()["data"]
    assert added["rss_url"] is not None

    removed = client.patch(f"/v1/saved-searches/{sid}", json={"channels": ["email"]}).json()["data"]
    assert removed["rss_url"] is None


def test_delete_saved_search(client, db):
    _login_pro(client, db)
    created = client.post("/v1/saved-searches", json={"name": "n", "entity": "proposal", "query": {}}).json()[
        "data"
    ]
    sid = created["saved_search_id"]
    assert client.delete(f"/v1/saved-searches/{sid}").status_code == 204
    assert client.get(f"/v1/saved-searches/{sid}").status_code == 404


def test_saved_search_quota_enforced(client, db):
    _login_pro(client, db)
    for i in range(SAVED_SEARCH_QUOTA):
        resp = client.post(
            "/v1/saved-searches", json={"name": f"search {i}", "entity": "proposal", "query": {}}
        )
        assert resp.status_code == 201, resp.text
    over = client.post("/v1/saved-searches", json={"name": "one too many", "entity": "proposal", "query": {}})
    assert over.status_code == 403
    assert over.json()["code"] == "forbidden_tier"


def test_saved_searches_are_isolated_per_account(client, db):
    _account_a, _user_a = _login_pro(client, db)
    created = client.post(
        "/v1/saved-searches", json={"name": "a-search", "entity": "proposal", "query": {}}
    ).json()["data"]

    client.cookies.clear()
    account_b = make_account(db, entitlement="pro", name="Other Account")
    user_b = make_user(db, account_b, email="other@example.com")
    db.commit()
    login(client, db, user_b)
    assert client.get(f"/v1/saved-searches/{created['saved_search_id']}").status_code == 404
    assert client.get("/v1/saved-searches").json()["data"] == []


def test_preview_saved_search_uses_the_stored_query(client, db):
    _account, _user = _login_pro(client, db)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    now = dt.datetime.now(UTC)
    matching = make_visible_proposal(db, src, public_id_suffix="1")
    matching.published_at = now - dt.timedelta(minutes=1)
    matching.kind = "storage"
    non_matching = make_visible_proposal(db, src, public_id_suffix="2")
    non_matching.published_at = now - dt.timedelta(minutes=1)
    non_matching.kind = "generation"
    db.commit()

    created = client.post(
        "/v1/saved-searches",
        json={"name": "storage only", "entity": "proposal", "query": {"kind": "storage"}},
    ).json()["data"]
    sid = created["saved_search_id"]

    resp = client.post(f"/v1/saved-searches/{sid}/preview")
    assert resp.status_code == 200
    guids = [item["guid"] for item in resp.json()["data"]]
    assert matching.public_id in guids
    assert non_matching.public_id not in guids


# ------------------------------------------------------------------------------------- rss token
def test_rss_token_shape():
    token = generate_rss_token()
    assert token.startswith("rt_")
    body = token[3:]
    assert 22 <= len(body) <= 64
    assert all(c in RSS_TOKEN_ALPHABET for c in body)


def test_rss_token_is_unguessable():
    """No formal entropy proof is possible in a unit test; this asserts the property that would
    fail fast if the generator regressed to something guessable: many independent draws, no
    collisions, and every character drawn from the full declared alphabet (not a narrowed
    subset — e.g. a broken RNG that only emits digits)."""
    tokens = {generate_rss_token() for _ in range(2000)}
    assert len(tokens) == 2000, "collision in 2000 draws would indicate far too little entropy"
    seen_chars = set()
    for t in tokens:
        seen_chars.update(t[3:])
    # 62-symbol alphabet, 32-character token, 2000 draws: expect to see the large majority of
    # symbols appear at least once; a suspiciously small observed alphabet would suggest a biased
    # or narrowed generator.
    assert len(seen_chars) >= 50


def test_feed_saved_search_requires_a_valid_token(client, db):
    resp = client.get("/feeds/saved/rt_doesnotexist000000000000")
    assert resp.status_code == 404


def test_feed_saved_search_serves_live_rss_for_a_valid_token(client, db):
    _account, _user = _login_pro(client, db)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    now = dt.datetime.now(UTC)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    prop.published_at = now - dt.timedelta(minutes=1)
    db.commit()

    created = client.post(
        "/v1/saved-searches",
        json={"name": "feed test", "entity": "proposal", "query": {}, "channels": ["email", "rss"]},
    ).json()["data"]
    token = created["rss_url"].rsplit("/", 1)[-1]

    resp = client.get(f"/feeds/saved/{token}")
    assert resp.status_code == 200
    assert "application/rss+xml" in resp.headers["content-type"]
    assert prop.name_canonical in resp.text
    assert "Live feed" in resp.text

    json_resp = client.get(f"/feeds/saved/{token}?format=json")
    assert json_resp.status_code == 200
    body = json_resp.json()
    assert body["items"][0]["_platform"]["data_as_of"] == "live"
