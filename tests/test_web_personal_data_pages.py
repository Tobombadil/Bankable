"""Public pages and the admin privacy queue after the 2026-09-30 legal audit (L-5, L-6).

L-5: an organisation that is a natural person stays readable but is left out of the sitemap, its
page is `noindex` and shows no ownership share. L-6: a takedown made from the admin site leaves
the cached sitemap at once (the API promises `effective_within_seconds: 60`; the cache lived an
hour), and privacy requests have their own queue with a deadline, an overdue view and the
checklist of records that name the subject. Every name is invented.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.api import admin_records as api_admin_records
from services.api.app import app as api_app
from services.api.conftest import (
    make_asset,
    make_asset_owner,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_proposal,
)
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.models import PrivacyRequest
from services.db.session import get_engine, get_sessionmaker, init_db
from tests.conftest import make_account, make_user
from web.admin import records as web_records
from web.admin.tasks import router as tasks_router
from web.api_client import ApiClient
from web.app import app as web_app
from web.formatting import rfc3339

UTC = dt.UTC
ORIGIN = {"origin": "http://testserver"}


def _mount_once(app: Any, path: str, router: Any) -> None:
    if not any(getattr(r, "path", None) == path for r in app.routes):
        app.include_router(router)


_mount_once(api_app, "/admin/v1/proposals/{public_id}", api_admin_records.router)
_mount_once(web_app, "/admin/records", web_records.router)
_mount_once(web_app, "/admin/tasks", tasks_router)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    default_limiter.reset()
    web_app.state.__dict__.pop("sitemap_cache", None)
    yield
    web_app.state.__dict__.pop("sitemap_cache", None)


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def web_client(db_sessionmaker: sessionmaker[Session]) -> Iterator[TestClient]:
    def _override_get_db() -> Iterator[Session]:
        session = db_sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = _override_get_db
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app, follow_redirects=False) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def _sign_in(client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    with db_sessionmaker() as db:
        account = make_account(db, entitlement="admin", name="Ops")
        make_user(
            db,
            account,
            email="operator@example.com",
            role="operator",
            password="correct horse battery staple",
        )
        db.commit()
    resp = client.post(
        "/login",
        data={"email": "operator@example.com", "password": "correct horse battery staple"},
        headers=ORIGIN,
    )
    assert resp.status_code == 303, resp.text


@pytest.fixture()
def world(db_sessionmaker: sessionmaker[Session]) -> dict[str, str]:
    with db_sessionmaker() as db:
        lic = make_open_licence(db)
        src = make_public_source(db, lic)
        person = make_org(db, "Dorothy Quillfeather")
        company = make_org(db, "Quillfeather Gas Partners LLC")
        asset = make_asset(db, src, lic, name="Quillfeather Landfill")
        make_asset_owner(db, asset, person, src, lic, share_pct=6.25)
        make_asset_owner(db, asset, company, src, lic, share_pct=93.75)
        proposal = make_visible_proposal(db, src, sponsor=company)
        db.commit()
        return {
            "person_slug": person.slug,
            "person_id": person.public_id,
            "company_slug": company.slug,
            "company_id": company.public_id,
            "asset_slug": asset.slug,
            "proposal_slug": proposal.slug,
        }


def test_a_person_is_left_out_of_the_sitemap_and_their_page_is_noindex(
    web_client: TestClient, world: dict[str, str]
) -> None:
    sitemap = web_client.get("/sitemap.xml").text
    assert f"/organizations/{world['company_slug']}" in sitemap
    assert f"/organizations/{world['person_slug']}" not in sitemap

    person = web_client.get(f"/organizations/{world['person_slug']}")
    assert person.status_code == 200, "the page stays readable; dropping the row is an owner decision"
    assert '<meta name="robots" content="noindex, follow">' in person.text
    assert "6.2%" not in person.text and "6.3%" not in person.text
    company = web_client.get(f"/organizations/{world['company_slug']}")
    assert '<meta name="robots" content="index, follow">' in company.text

    asset_page = web_client.get(f"/assets/{world['asset_slug']}").text
    assert "93.8%" in asset_page, "a company's share is still shown"
    assert "6.2%" not in asset_page and "6.3%" not in asset_page, "a person's share is not"


def test_an_admin_takedown_leaves_the_cached_sitemap_at_once(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session], world: dict[str, str]
) -> None:
    """L-6 (c): no manual cache reset between the takedown and the second read."""
    assert f"/organizations/{world['company_slug']}" in web_client.get("/sitemap.xml").text
    _sign_in(web_client, db_sessionmaker)
    resp = web_client.post(
        f"/admin/records/organizations/{world['company_id']}/publish-state",
        data={"publish_state": "unpublished", "takedown": "on", "reason": "rights-holder request"},
        headers=ORIGIN,
    )
    assert resp.status_code == 303
    assert f"/organizations/{world['company_slug']}" not in web_client.get("/sitemap.xml").text
    assert "/proposals/" in web_client.get("/sitemap.xml").text, "the rest of the cached sitemap stands"


def test_the_privacy_queue_shows_deadlines_overdue_requests_and_the_checklist(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session], world: dict[str, str]
) -> None:
    api = TestClient(api_app)
    on_time = api.post(
        "/v1/privacy/requests",
        json={"kind": "erasure", "record_public_id": world["person_id"], "contact_email": "dq@example.com"},
    ).json()["data"]
    late = api.post(
        "/v1/privacy/requests",
        json={
            "kind": "correction",
            "record_public_id": world["company_id"],
            "contact_email": "x@example.com",
        },
    ).json()["data"]
    with db_sessionmaker() as db:
        row = db.scalar(select(PrivacyRequest).where(PrivacyRequest.public_id == late["public_id"]))
        assert row is not None
        row.due_at = dt.datetime.now(UTC) - dt.timedelta(days=2)
        db.commit()

    _sign_in(web_client, db_sessionmaker)
    tasks = web_client.get("/admin/tasks").text
    assert 'id="privacy-summary"' in tasks and "2 open, 1 overdue" in tasks

    overdue = web_client.get("/admin/tasks/privacy", params={"overdue": "true"}).text
    assert late["public_id"] in overdue and on_time["public_id"] not in overdue

    detail = web_client.get(f"/admin/tasks/privacy/{on_time['public_id']}")
    assert detail.status_code == 200
    assert 'id="privacy-checklist"' in detail.text
    assert world["person_id"] in detail.text and "Quillfeather Landfill" in detail.text
    assert rfc3339(on_time["due_at"]) in detail.text  # to the second, UTC (web/formatting.py)

    closed = web_client.post(
        f"/admin/tasks/privacy/{on_time['public_id']}/update",
        data={"status": "done", "reason": "organisation taken down"},
        headers=ORIGIN,
    )
    assert closed.status_code == 303
    after = web_client.get(f"/admin/tasks/privacy/{on_time['public_id']}").text
    assert "dq@example.com" not in after
