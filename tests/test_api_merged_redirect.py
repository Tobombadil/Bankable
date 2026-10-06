"""A merged record's old id and slug answer `301` to the visible survivor (US-201 AC3;
api/openapi.yaml `getProposal`, `ProposalPublicId`; QA audit 2026-09-30 QA-8).

Before, every absorbed record answered 404 on the API, the web slug and the web id, so each merge
broke shared links, bookmarks, social-post links and search-engine URLs. A survivor that is not
visible at the caller's tier still answers 404, so its id does not leak.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal
from services.api.deps import get_db
from services.db.models import Opportunity, Proposal
from services.resolve.merge import merge_proposal

UTC = dt.UTC


@pytest.fixture()
def merged(db: Session) -> dict[str, Any]:
    source = make_public_source(db, make_open_licence(db))
    survivor = make_visible_proposal(db, source, public_id_suffix="1")
    absorbed = make_visible_proposal(db, source, public_id_suffix="2")
    merge_proposal(db, canonical=survivor, absorbed=absorbed, score=95.0, rationale="test")
    db.commit()
    return {
        "survivor": survivor.public_id,
        "survivor_slug": survivor.slug,
        "old_id": absorbed.public_id,
        "old_slug": absorbed.slug,
        "survivor_row": survivor.id,
    }


def test_the_old_id_answers_301_to_the_survivor(client: TestClient, merged: dict[str, Any]) -> None:
    resp = client.get(f"/v1/proposals/{merged['old_id']}?include=events", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"].endswith(f"/v1/proposals/{merged['survivor']}?include=events")
    followed = client.get(f"/v1/proposals/{merged['old_id']}")
    assert followed.status_code == 200
    assert followed.json()["data"]["public_id"] == merged["survivor"]


def test_the_old_slug_answers_301_to_the_survivor(client: TestClient, merged: dict[str, Any]) -> None:
    resp = client.get(f"/v1/proposals/{merged['old_slug']}", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"].endswith(f"/v1/proposals/{merged['survivor']}")


def test_sub_resources_redirect_on_the_same_path(client: TestClient, merged: dict[str, Any]) -> None:
    for suffix in ("sources", "events"):
        resp = client.get(f"/v1/proposals/{merged['old_id']}/{suffix}", follow_redirects=False)
        assert resp.status_code == 301, suffix
        assert resp.headers["location"].endswith(f"/v1/proposals/{merged['survivor']}/{suffix}")


def test_a_chain_of_merges_lands_on_the_last_survivor(
    client: TestClient, db: Session, merged: dict[str, Any]
) -> None:
    survivor = db.get(Proposal, merged["survivor_row"])
    assert survivor is not None
    source = survivor.sources[0].source
    final = make_visible_proposal(db, source, public_id_suffix="3")
    merge_proposal(db, canonical=final, absorbed=survivor, score=95.0, rationale="test")
    db.commit()
    resp = client.get(f"/v1/proposals/{merged['old_id']}", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"].endswith(f"/v1/proposals/{final.public_id}")


def test_a_hidden_survivor_is_a_plain_404_and_its_id_does_not_leak(
    client: TestClient, db: Session, merged: dict[str, Any]
) -> None:
    survivor = db.get(Proposal, merged["survivor_row"])
    assert survivor is not None
    survivor.publish_state = "unpublished"
    db.commit()
    resp = client.get(f"/v1/proposals/{merged['old_id']}", follow_redirects=False)
    assert resp.status_code == 404
    assert merged["survivor"] not in resp.text
    unknown = client.get("/v1/proposals/prop_doesnotexist", follow_redirects=False)
    assert {k: v for k, v in resp.json().items() if k not in ("instance", "request_id")} == {
        k: v for k, v in unknown.json().items() if k not in ("instance", "request_id")
    }


def test_an_unmerged_hidden_record_is_still_404(client: TestClient, db: Session) -> None:
    source = make_public_source(db, make_open_licence(db))
    hidden = make_visible_proposal(db, source, public_id_suffix="9")
    hidden.publish_state = "unpublished"
    db.commit()
    assert client.get(f"/v1/proposals/{hidden.public_id}", follow_redirects=False).status_code == 404


def test_a_merged_opportunity_redirects_too(client: TestClient, db: Session) -> None:
    from services.api.conftest import make_visible_opportunity

    source = make_public_source(db, make_open_licence(db))
    survivor = make_visible_opportunity(db, source, public_id_suffix="1")
    absorbed: Opportunity = make_visible_opportunity(db, source, public_id_suffix="2")
    absorbed.merged_into_id = survivor.id
    absorbed.publish_state = "unpublished"
    db.commit()
    resp = client.get(f"/v1/opportunities/{absorbed.public_id}", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"].endswith(f"/v1/opportunities/{survivor.public_id}")


# ----------------------------------------------------------------------------------------- web
@pytest.fixture()
def web_client(db_sessionmaker: sessionmaker[Session]) -> Iterator[TestClient]:
    from services.api.app import app as api_app
    from web.api_client import ApiClient
    from web.app import app as web_app

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
    with TestClient(web_app) as c:
        yield c
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def test_the_web_old_slug_and_old_id_answer_301_to_the_survivor_page(
    web_client: TestClient, merged: dict[str, Any]
) -> None:
    for segment in (merged["old_slug"], merged["old_id"]):
        resp = web_client.get(f"/proposals/{segment}", follow_redirects=False)
        assert resp.status_code == 301, segment
        assert resp.headers["location"] == f"/proposals/{merged['survivor_slug']}"
    assert web_client.get(f"/proposals/{merged['old_slug']}").status_code == 200


def test_the_web_page_of_a_hidden_survivor_is_404(
    web_client: TestClient, db: Session, merged: dict[str, Any]
) -> None:
    survivor = db.get(Proposal, merged["survivor_row"])
    assert survivor is not None
    survivor.publish_state = "unpublished"
    db.commit()
    resp = web_client.get(f"/proposals/{merged['old_slug']}", follow_redirects=False)
    assert resp.status_code == 404
    assert merged["survivor_slug"] not in resp.text
