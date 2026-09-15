"""Tests for `web/admin/ops.py`: Keys, Costs and Audit admin screens. Fixtures mirror
`web/test_admin_shell.py`/`web/test_admin_people.py`."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.db.models import ApiKey, ModelCall
from services.db.session import get_engine, get_sessionmaker, init_db
from tests.conftest import make_account, make_user
from web.admin.ops import router as ops_router
from web.api_client import ApiClient
from web.app import app as web_app

UTC = dt.UTC


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


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
    if not any(getattr(r, "path", None) == "/admin/keys" for r in web_app.routes):
        web_app.include_router(ops_router)
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app, follow_redirects=False) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def _sign_in(client: TestClient, db_sessionmaker: sessionmaker[Session], *, role: str = "operator") -> None:
    with db_sessionmaker() as db:
        account = make_account(db, entitlement="admin", name="Ops")
        make_user(
            db, account, email=f"{role}@example.com", role=role, password="correct horse battery staple"
        )
        db.commit()
    resp = client.post(
        "/login",
        data={"email": f"{role}@example.com", "password": "correct horse battery staple"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303, resp.text


def _make_customer_account(db_sessionmaker: sessionmaker[Session], *, name: str = "Key Customer") -> str:
    with db_sessionmaker() as db:
        account = make_account(db, entitlement="pro", name=name)
        db.commit()
        return account.public_id


# ================================================================================================= keys
def test_keys_list_empty_state(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    _sign_in(web_client, db_sessionmaker)
    resp = web_client.get("/admin/keys")
    assert resp.status_code == 200
    assert "No API keys issued yet." in resp.text


def test_issue_key_shows_secret_once_and_list_shows_new_key(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker)
    account_id = _make_customer_account(db_sessionmaker)

    resp = web_client.post(
        "/admin/keys",
        data={
            "name": "Integration key",
            "account_id": account_id,
            "licence_acceptance_ref": "evt_1234",
            "reason": "issue for integration testing",
        },
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 201
    assert "Copy this secret now" in resp.text
    secret_match = re.search(r'value="(bk_live_[^"]+)"', resp.text)
    assert secret_match, resp.text
    secret = secret_match.group(1)

    with db_sessionmaker() as db:
        key = db.query(ApiKey).filter(ApiKey.name == "Integration key").one()
        assert key.revoked_at is None

    listing = web_client.get("/admin/keys")
    assert listing.status_code == 200
    assert "Integration key" not in listing.text  # name isn't rendered in the list, prefix/last4 is
    assert key.prefix in listing.text
    assert secret not in listing.text  # never shown a second time


def test_keys_list_filters_by_account(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    _sign_in(web_client, db_sessionmaker)
    account_a = _make_customer_account(db_sessionmaker, name="Account A")
    account_b = _make_customer_account(db_sessionmaker, name="Account B")
    for account_id in (account_a, account_b):
        resp = web_client.post(
            "/admin/keys",
            data={
                "name": f"key for {account_id}",
                "account_id": account_id,
                "licence_acceptance_ref": "evt_x",
                "reason": "setup",
            },
            headers={"origin": "http://testserver"},
        )
        assert resp.status_code == 201

    filtered = web_client.get(f"/admin/keys?account_id={account_a}")
    assert filtered.status_code == 200
    assert account_a in filtered.text
    assert account_b not in filtered.text


def test_revoke_key_works(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    _sign_in(web_client, db_sessionmaker)
    account_id = _make_customer_account(db_sessionmaker)
    issue = web_client.post(
        "/admin/keys",
        data={
            "name": "Revoke me",
            "account_id": account_id,
            "licence_acceptance_ref": "evt_r",
            "reason": "setup",
        },
        headers={"origin": "http://testserver"},
    )
    assert issue.status_code == 201
    with db_sessionmaker() as db:
        key = db.query(ApiKey).filter(ApiKey.name == "Revoke me").one()
        key_id = key.public_id

    resp = web_client.post(
        f"/admin/keys/{key_id}/revoke",
        data={"reason": "no longer needed"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/keys?flash=")

    with db_sessionmaker() as db:
        refreshed = db.query(ApiKey).filter(ApiKey.public_id == key_id).one()
        assert refreshed.revoked_at is not None

    listing = web_client.get("/admin/keys?status=revoked")
    assert listing.status_code == 200
    assert "revoked" in listing.text


def test_key_post_without_origin_is_forbidden(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker)
    account_id = _make_customer_account(db_sessionmaker)
    resp = web_client.post(
        "/admin/keys",
        data={
            "name": "x",
            "account_id": account_id,
            "licence_acceptance_ref": "evt_x",
            "reason": "x",
        },
    )
    assert resp.status_code == 403


def test_anonymous_is_redirected_to_login(web_client: TestClient) -> None:
    resp = web_client.get("/admin/keys")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/admin/keys"


# ================================================================================================ costs
def test_costs_empty_state_says_expected(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker)
    resp = web_client.get("/admin/costs")
    assert resp.status_code == 200
    assert "expected state today" in resp.text


def test_costs_seeded_row_renders_with_totals(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker)
    with db_sessionmaker() as db:
        call = ModelCall(
            purpose="extract",
            alias="fast",
            source_id=None,
            input_tokens=1000,
            output_tokens=500,
            cost_usd=1.5,
            cache_hit=True,
            created_at=dt.datetime.now(UTC),
        )
        db.add(call)
        db.commit()

    resp = web_client.get("/admin/costs")
    assert resp.status_code == 200
    assert "extract" in resp.text
    assert "1000" in resp.text
    assert "500" in resp.text
    assert "$1.5000" in resp.text
    assert "<th>Total</th>" in resp.text


# ================================================================================================ audit
def test_audit_list_filters_and_detail_renders_before_after(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker)
    account_id = _make_customer_account(db_sessionmaker)
    issue = web_client.post(
        "/admin/keys",
        data={
            "name": "Audited key",
            "account_id": account_id,
            "licence_acceptance_ref": "evt_a",
            "reason": "issue for audit test",
        },
        headers={"origin": "http://testserver"},
    )
    assert issue.status_code == 201

    listing = web_client.get("/admin/audit?subject_type=api_key")
    assert listing.status_code == 200
    assert "key_issued" in listing.text
    assert "issue for audit test" in listing.text

    match = re.search(r"/admin/audit/(evt_[A-Za-z0-9]+)\?subject_type=api_key", listing.text)
    assert match, listing.text
    event_id = match.group(1)

    detail = web_client.get(f"/admin/audit/{event_id}?subject_type=api_key")
    assert detail.status_code == 200
    assert "key_issued" in detail.text
    assert "issue for audit test" in detail.text
    assert "scopes" in detail.text  # an `after` field from the key-issue audit event


def test_audit_list_empty_state(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    _sign_in(web_client, db_sessionmaker)
    resp = web_client.get("/admin/audit?subject_type=source")
    assert resp.status_code == 200
    assert "No audit events match" in resp.text


def test_audit_detail_not_found(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    _sign_in(web_client, db_sessionmaker)
    resp = web_client.get("/admin/audit/evt_00000000000000000000000000")
    assert resp.status_code == 404
