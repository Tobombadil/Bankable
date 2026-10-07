"""Transport conventions of docs/23 §1, §7, §8 that the backend audit of 2026-10-07 found unmet:
`ETag` and conditional GET (API-2), `Vary: Accept-Encoding` kept (API-3), the framework's own 404,
405 and validation errors as problem+json (API-4), unknown `include` values and out-of-range `limit`
refused (API-5), an honest `total_is_estimate` (API-6) and `Cache-Control` on feeds (API-9)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from services.api.conditional import entity_tag, none_match
from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal


def _seed(db: Session, n: int = 3) -> None:
    src = make_public_source(db, make_open_licence(db))
    for i in range(n):
        make_visible_proposal(db, src, public_id_suffix=str(i + 1))
    db.commit()


# ------------------------------------------------------------------------------- ETag (API-2)
def test_a_public_get_carries_an_etag_and_answers_304_when_it_still_matches(
    client: TestClient, db: Session
) -> None:
    _seed(db)
    first = client.get("/v1/proposals")
    tag = first.headers["etag"]
    assert tag.startswith('W/"')
    # Another response with its own request id and timestamps is still the same representation.
    again = client.get("/v1/proposals")
    assert again.json()["meta"]["request_id"] != first.json()["meta"]["request_id"]
    assert again.headers["etag"] == tag

    not_modified = client.get("/v1/proposals", headers={"If-None-Match": tag})
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["etag"] == tag
    assert not_modified.headers["cache-control"] == "public, max-age=300"
    assert "x-request-id" in not_modified.headers and "ratelimit-limit" in not_modified.headers
    assert client.get("/v1/proposals", headers={"If-None-Match": '"other", *'}).status_code == 304

    # A change to the data is a new tag, and the old one no longer matches.
    _seed_more = make_visible_proposal(
        db, make_public_source(db, make_open_licence(db, "l2"), id_="s2"), public_id_suffix="99"
    )
    db.commit()
    changed = client.get("/v1/proposals", headers={"If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["etag"] != tag
    assert _seed_more.public_id in changed.text


def test_no_etag_on_credentialed_errors_or_streams(client: TestClient, db: Session) -> None:
    _seed(db, n=1)
    keyed = client.get("/v1/proposals", headers={"Authorization": "Bearer bk_live_nope"})
    assert "etag" not in keyed.headers
    assert keyed.headers["cache-control"] == "private, no-store"
    missing = client.get("/v1/proposals/prop_nope")
    assert missing.status_code == 404 and "etag" not in missing.headers


def test_entity_tags_ignore_only_the_per_request_values() -> None:
    one = b'{"data":[1],"meta":{"request_id":"req_a","generated_at":"x","data_as_of":"y"}}'
    two = b'{"data":[1],"meta":{"request_id":"req_b","generated_at":"z","data_as_of":"w"}}'
    three = b'{"data":[2],"meta":{"request_id":"req_a","generated_at":"x","data_as_of":"y"}}'
    assert entity_tag(one) == entity_tag(two) != entity_tag(three)
    assert none_match('W/"x", "y"', 'W/"y"') and none_match("*", 'W/"q"') and not none_match(None, 'W/"q"')


# ------------------------------------------------------------------------ caching (API-3, API-9)
def test_vary_keeps_accept_encoding_on_a_compressed_public_response(client: TestClient, db: Session) -> None:
    _seed(db, n=20)
    resp = client.get("/v1/proposals", headers={"Accept-Encoding": "gzip"})
    assert resp.headers.get("content-encoding") == "gzip"
    vary = {v.strip().lower() for v in resp.headers["vary"].split(",")}
    assert {"accept-encoding", "authorization", "cookie"} <= vary


def test_feeds_are_edge_cacheable_and_the_private_feed_is_not(client: TestClient, db: Session) -> None:
    _seed(db, n=1)
    feed = client.get("/feeds/proposals.rss")
    assert feed.status_code == 200
    assert feed.headers["cache-control"] == "public, max-age=300, stale-while-revalidate=60"
    assert feed.headers["etag"].startswith('W/"')
    private = client.get("/feeds/saved/rt_doesnotexist000000000000")
    assert private.headers["cache-control"] == "private, no-store"


# ------------------------------------------------------------------- framework errors (API-4)
def test_an_unknown_path_is_a_problem_not_a_bare_detail(client: TestClient) -> None:
    resp = client.get("/v1/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == "not_found" and body["request_id"].startswith("req_")
    assert "detail" in body and body["instance"] == "/v1/nope"


def test_a_wrong_method_is_a_405_problem_with_allow(client: TestClient) -> None:
    resp = client.delete("/v1/proposals")
    assert resp.status_code == 405
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["code"] == "method_not_allowed"
    assert "GET" in resp.headers["allow"]


def test_a_malformed_body_is_a_400_validation_problem(client: TestClient) -> None:
    resp = client.post("/v1/ui-events", content=b"{not json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == "validation_error" and body["errors"]
    assert all({"field", "message"} <= set(e) for e in body["errors"])


# ------------------------------------------------------------------ include and limit (API-5)
@pytest.mark.parametrize(
    "path",
    ["/v1/proposals", "/v1/opportunities", "/v1/organizations", "/v1/events", "/v1/assets", "/v1/sources"],
)
@pytest.mark.parametrize("limit", ["0", "201", "500"])
def test_a_limit_outside_the_documented_range_is_refused(
    path: str, limit: str, client: TestClient, db: Session
) -> None:
    resp = client.get(path, params={"limit": limit})
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "validation_error"
    assert resp.json()["errors"][0]["field"] == "limit"


@pytest.mark.parametrize("path", ["/v1/proposals", "/v1/opportunities", "/v1/organizations", "/v1/events"])
def test_an_include_a_list_does_not_build_is_refused(path: str, client: TestClient, db: Session) -> None:
    for value in ("bogus", "count,sources"):
        resp = client.get(path, params={"include": value})
        assert resp.status_code == 400, (value, resp.text)
        assert resp.json()["code"] == "validation_error"
    assert client.get(path, params={"include": "count", "limit": 200}).status_code == 200


def test_a_detail_refuses_an_undocumented_include(client: TestClient, db: Session) -> None:
    _seed(db, n=1)
    pid = client.get("/v1/proposals").json()["data"][0]["public_id"]
    assert client.get(f"/v1/proposals/{pid}", params={"include": "events"}).status_code == 200
    resp = client.get(f"/v1/proposals/{pid}", params={"include": "bogus"})
    assert resp.status_code == 400 and resp.json()["code"] == "validation_error"


# -------------------------------------------------------------------- include=count (API-6)
def test_include_count_is_exact_says_so_and_works_on_every_list(client: TestClient, db: Session) -> None:
    _seed(db, n=3)
    for path, expected in (("/v1/proposals", 3), ("/v1/organizations", 0), ("/v1/events", 0)):
        meta = client.get(path, params={"include": "count", "limit": 1}).json()["meta"]
        assert meta["total"] == expected, path
        assert meta["total_is_estimate"] is False, path


@pytest.mark.parametrize(
    "query",
    ["proposed_online_date[gte]=2026-10-07", "county=Loudoun", "completion_date[from]=2026-01-01"],
)
def test_filters_the_spec_does_not_define_are_refused_not_dropped(query: str, client: TestClient) -> None:
    """Expert review 2026-10-07 (large-load analyst): these returned the unfiltered set on the site.
    The API refuses them (docs/23 §7 defines `county_fips`, and no completion-date filter); the
    site's own pages are the web lane's to align."""
    resp = client.get(f"/v1/proposals?{query}")
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_parameter"
