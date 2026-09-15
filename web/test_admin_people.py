"""Tests for `web/admin/people.py`: Users and Customers (with subscriptions) admin screens.
Fixtures mirror `web/test_admin_shell.py` (same `db_sessionmaker`/`web_client`/`_sign_in` shape);
the API app's CRM/billing ports are overridden with fresh in-memory fakes per test, exactly as
`services/crm/test_router.py` and `services/billing/test_router.py` already do for the API-only
suites -- these tests exercise the same routes end to end, through the web layer's HTML forms.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.billing.fake import InMemoryBilling
from services.crm.fake import InMemoryCrm
from services.db.models import Account, User
from services.db.session import get_engine, get_sessionmaker, init_db
from services.sor.ports import CompanyRef, CompanyUpsert, SorUnavailable
from services.sor.wiring import get_billing_port, get_crm_port
from tests.conftest import make_account, make_user
from web.admin.people import router as people_router
from web.api_client import ApiClient
from web.app import app as web_app


class _UnavailableCrm(InMemoryCrm):
    """Decision (task brief): "the fake raises `SorUnavailable` (subclass it in the test)" -- a
    thin `InMemoryCrm` subclass so everything but `upsert_company` still behaves like the real
    fake, matching `services/crm/test_router.py`'s `_UnavailableCrm`/`_RejectingCrm` pattern."""

    def upsert_company(self, company: CompanyUpsert) -> CompanyRef:
        raise SorUnavailable("CRM temporarily unreachable")


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def fake_crm() -> InMemoryCrm:
    return InMemoryCrm()


@pytest.fixture()
def fake_billing() -> InMemoryBilling:
    return InMemoryBilling()


@pytest.fixture()
def web_client(
    db_sessionmaker: sessionmaker[Session], fake_crm: InMemoryCrm, fake_billing: InMemoryBilling
) -> Iterator[TestClient]:
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
    api_app.dependency_overrides[get_crm_port] = lambda: fake_crm
    api_app.dependency_overrides[get_billing_port] = lambda: fake_billing
    if not any(getattr(r, "path", None) == "/admin/users" for r in web_app.routes):
        web_app.include_router(people_router)
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


# ================================================================================================= users
def test_users_list_renders_rows(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/users")
    assert resp.status_code == 200
    assert "operator@example.com" in resp.text


def test_users_list_empty_state_names_the_filter(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/users?role=legal")
    assert resp.status_code == 200
    assert "No users match role=legal" in resp.text
    assert "Clear all" in resp.text


def test_users_list_filters_by_role(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    with db_sessionmaker() as db:
        account = db.query(Account).first()
        assert account is not None
        make_user(db, account, email="viewer@example.com", role="viewer")
        db.commit()
    resp = web_client.get("/admin/users?role=viewer")
    assert resp.status_code == 200
    assert "viewer@example.com" in resp.text
    # The signed-in operator's own email still appears once, in the header chrome -- just not as
    # a filtered-out table row.
    assert resp.text.count("operator@example.com") == 1


def test_owner_role_option_hidden_from_operator_shown_to_owner(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    with db_sessionmaker() as db:
        account = db.query(Account).first()
        assert account is not None
        target = make_user(db, account, email="target@example.com", role="member")
        db.commit()
        target_id = target.public_id
    resp = web_client.get(f"/admin/users/{target_id}")
    assert resp.status_code == 200
    assert '<option value="owner"' not in resp.text

    _sign_in(web_client, db_sessionmaker, role="owner")
    resp = web_client.get(f"/admin/users/{target_id}")
    assert resp.status_code == 200
    assert '<option value="owner"' in resp.text


def test_user_role_change_redirects_with_flash(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    with db_sessionmaker() as db:
        account = db.query(Account).first()
        assert account is not None
        target = make_user(db, account, email="target2@example.com", role="member")
        db.commit()
        target_id = target.public_id

    resp = web_client.post(
        f"/admin/users/{target_id}/role",
        data={"role": "legal", "reason": "promote to legal review"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/admin/users/{target_id}?flash=")

    with db_sessionmaker() as db:
        refreshed = db.query(User).filter(User.public_id == target_id).one()
        assert refreshed.role == "legal"


def test_user_status_change_invalid_value_rerenders_with_notice(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    with db_sessionmaker() as db:
        account = db.query(Account).first()
        assert account is not None
        target = make_user(db, account, email="target3@example.com", role="member")
        db.commit()
        target_id = target.public_id

    resp = web_client.post(
        f"/admin/users/{target_id}/status",
        data={"status": "not-a-real-status", "reason": "test"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 400
    assert "target3@example.com" in resp.text  # detail page re-rendered with real data


def test_delete_confirm_then_post_redirects_to_task(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    with db_sessionmaker() as db:
        account = db.query(Account).first()
        assert account is not None
        target = make_user(db, account, email="deleteme@example.com", role="member")
        db.commit()
        target_id = target.public_id

    confirm = web_client.get(f"/admin/users/{target_id}/delete")
    assert confirm.status_code == 200
    assert "deletion-request task" in confirm.text

    resp = web_client.post(
        f"/admin/users/{target_id}/delete",
        data={"reason": "user requested deletion"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/tasks/task_")

    with db_sessionmaker() as db:
        refreshed = db.query(User).filter(User.public_id == target_id).one()
        assert refreshed.status == "disabled"


def test_post_without_origin_is_forbidden(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    with db_sessionmaker() as db:
        account = db.query(Account).first()
        assert account is not None
        target = make_user(db, account, email="csrf@example.com", role="member")
        db.commit()
        target_id = target.public_id
    resp = web_client.post(f"/admin/users/{target_id}/role", data={"role": "legal", "reason": "x"})
    assert resp.status_code == 403


def test_anonymous_is_redirected_to_login(web_client: TestClient) -> None:
    resp = web_client.get("/admin/users")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/admin/users"


# ============================================================================================ customers
def test_customer_detail_shows_stale_banner(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    with db_sessionmaker() as db:
        account = make_account(db, entitlement="pro", name="Stale Co")
        account.entitlement_stale = True
        db.commit()
        account_id = account.public_id

    resp = web_client.get(f"/admin/customers/{account_id}")
    assert resp.status_code == 200
    assert "Writes are refused while the adapter is unavailable" in resp.text


def test_customer_list_empty_state(web_client: TestClient, db_sessionmaker: sessionmaker[Session]) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.get("/admin/customers?entitlement=api")
    assert resp.status_code == 200
    assert "No customers match entitlement=api" in resp.text


def test_customer_create_writes_through_fake_crm(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session], fake_crm: InMemoryCrm
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    resp = web_client.post(
        "/admin/customers/new",
        data={
            "name": "Somecorp LLC",
            "kind": "organization",
            "primary_contact_email": "buyer@somecorp.example",
            "reason": "new customer onboarding",
        },
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/customers/acc_")
    assert "somecorp.example" in fake_crm.companies


def test_customer_create_sor_unavailable_renders_notice(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    api_app.dependency_overrides[get_crm_port] = lambda: _UnavailableCrm()
    resp = web_client.post(
        "/admin/customers/new",
        data={
            "name": "Downcorp LLC",
            "kind": "organization",
            "primary_contact_email": "buyer@downcorp.example",
            "reason": "new customer onboarding",
        },
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 503
    assert "CRM adapter unavailable" in resp.text


def test_subscription_create_without_billing_ref_is_409(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    with db_sessionmaker() as db:
        account = make_account(db, entitlement="public", name="No Billing Co")
        db.commit()
        account_id = account.public_id

    resp = web_client.post(
        f"/admin/customers/{account_id}/subscriptions",
        data={"plan_code": "pro", "seats": "2", "reason": "start subscription"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 409
    assert "no billing customer" in resp.text.lower()


def test_subscription_create_with_billing_ref_flips_entitlement(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in(web_client, db_sessionmaker, role="operator")
    with db_sessionmaker() as db:
        account = make_account(db, entitlement="public", name="Billed Co")
        account.billing_ref = "cus_test_123"
        db.commit()
        account_id = account.public_id

    resp = web_client.post(
        f"/admin/customers/{account_id}/subscriptions",
        data={"plan_code": "pro", "seats": "3", "reason": "start subscription"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/admin/customers/{account_id}?flash=")

    detail = web_client.get(f"/admin/customers/{account_id}")
    assert detail.status_code == 200
    assert "pro" in detail.text
    with db_sessionmaker() as db:
        refreshed = db.query(Account).filter(Account.public_id == account_id).one()
        assert refreshed.entitlement == "pro"
