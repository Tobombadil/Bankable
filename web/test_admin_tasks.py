"""Tests for the admin tasks screens (`web/admin/tasks.py`, Sprint 3 item 3): the queue, task
detail (pending record, contact, audit ids), the update form and the approve-intake flow, over the
real `/admin/v1/tasks*` API mounted in-process (`web.api_client.build_client`'s TestClient path).

Fixtures copied from `web/test_admin_shell.py` (its own docstring: "coordinator-owned contract for
the admin page modules"); this module additionally mounts `web.admin.tasks.router` if it is not
already on `web_app` (the coordinator's job once every Sprint 3 agent's module lands) and gives
tests a raw `TestClient(api_app)` to hit the public `/v1/intake/*` and `/v1/reports` endpoints
directly, exactly the way a real submitter would.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.crm.fake import InMemoryCrm
from services.db.models import Task, User
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id
from services.sor.ports import SorUnavailable
from services.sor.wiring import get_crm_port
from tests.conftest import make_account, make_user
from web.admin.tasks import router as tasks_router
from web.api_client import ApiClient
from web.app import app as web_app

UTC = dt.UTC

if not any(getattr(r, "path", None) == "/admin/tasks" for r in web_app.routes):
    web_app.include_router(tasks_router)


class _FlakyCrm(InMemoryCrm):
    """Raises `SorUnavailable` on `request_personal_data_deletion` -- proves the deletion task's
    503 notice (screen brief: "the 503 notice when the CRM is unreachable")."""

    def request_personal_data_deletion(self, *, email: str, reason: str) -> str:
        raise SorUnavailable("adapter down")


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
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    with TestClient(web_app, follow_redirects=False) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


@pytest.fixture()
def api_client(db_sessionmaker: sessionmaker[Session], web_client: TestClient) -> TestClient:
    """A second `TestClient` over the same mounted `api_app` (same dependency override, same
    database) for hitting the public `/v1/intake/*` endpoints directly, the way
    `tests/test_api_admin_intake_flow.py` does."""
    return TestClient(api_app, base_url="http://api-internal")


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


_PROPOSAL_SUBMISSION = {
    "project_name": "Sunrise Storage",
    "kind": "storage",
    "jurisdiction": "US-TX",
    "lifecycle_state": "announced",
    "sponsor_name": "Sunrise Power LLC",
    "capacity_mw": 100,
    "contact": {"name": "Jamie Rivera", "email": "jamie@example.com"},
    "consent": True,
    "captcha_token": "test-token",
}


def _submit_intake_proposal(api_client: TestClient) -> str:
    resp = api_client.post("/v1/intake/proposals", json=_PROPOSAL_SUBMISSION)
    assert resp.status_code == 202, resp.text
    task_id: str = resp.json()["task_id"]
    return task_id


def _make_deletion_task(db_sessionmaker: sessionmaker[Session]) -> tuple[str, str]:
    """A `deletion_request` task against a real (non-admin) user, the shape
    `services/api/admin_people.py`'s `admin_delete_user` produces."""
    with db_sessionmaker() as db:
        account = make_account(db, entitlement="public", name="Member Co")
        user = make_user(db, account, email="member@example.com", role="member")
        task = Task(
            public_id="",
            type="deletion_request",
            subject_type="user",
            subject_id=user.id,
            status="open",
            due_at=dt.datetime.now(UTC) + dt.timedelta(days=30),
        )
        db.add(task)
        db.flush()
        task.public_id = public_id("task", task.id)
        db.commit()
        return task.public_id, user.public_id


# =================================================================================================== auth
def test_tasks_list_redirects_anonymous_to_login(web_client: TestClient) -> None:
    resp = web_client.get("/admin/tasks")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=/admin/tasks"


def test_task_update_without_origin_is_forbidden(web_client: TestClient, db_sessionmaker) -> None:
    _sign_in(web_client, db_sessionmaker)
    task_id, _ = _make_deletion_task(db_sessionmaker)
    resp = web_client.post(f"/admin/tasks/{task_id}/update", data={"status": "in_progress", "reason": "x"})
    assert resp.status_code == 403


# =================================================================================================== list
def test_tasks_list_empty_state_names_the_filter(web_client: TestClient, db_sessionmaker) -> None:
    _sign_in(web_client, db_sessionmaker)
    resp = web_client.get("/admin/tasks?status=rejected")
    assert resp.status_code == 200
    assert "No tasks match" in resp.text
    assert "status=rejected" in resp.text
    assert "Clear all filters" in resp.text


def test_tasks_list_renders_rows_and_filters(
    web_client: TestClient, db_sessionmaker, api_client: TestClient
) -> None:
    _submit_intake_proposal(api_client)
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.get("/admin/tasks")
    assert resp.status_code == 200
    assert "intake proposal" in resp.text
    assert "open" in resp.text

    filtered = web_client.get("/admin/tasks?type=intake_opportunity")
    assert filtered.status_code == 200
    assert "No tasks match" in filtered.text
    assert "type=intake_opportunity" in filtered.text


# ================================================================================================= detail
def test_task_detail_shows_pending_record_and_contact(
    web_client: TestClient, db_sessionmaker, api_client: TestClient
) -> None:
    task_id = _submit_intake_proposal(api_client)
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.get(f"/admin/tasks/{task_id}")
    assert resp.status_code == 200
    assert "Sunrise Storage" in resp.text
    assert "Sunrise Power LLC" in resp.text
    assert "Jamie Rivera" in resp.text
    assert "jamie@example.com" in resp.text
    assert "Review intake submission" in resp.text


def test_task_detail_not_found(web_client: TestClient, db_sessionmaker) -> None:
    _sign_in(web_client, db_sessionmaker)
    resp = web_client.get("/admin/tasks/task_doesnotexist000000000")
    assert resp.status_code == 404


# ================================================================================================= update
def test_task_update_changes_status(web_client: TestClient, db_sessionmaker, api_client: TestClient) -> None:
    task_id = _submit_intake_proposal(api_client)
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.post(
        f"/admin/tasks/{task_id}/update",
        data={"status": "in_progress", "notes": "looking into it", "reason": "triage"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    assert "flash=" in resp.headers["location"]

    detail = web_client.get(f"/admin/tasks/{task_id}")
    assert "in progress" in detail.text
    assert "looking into it" in detail.text


def test_task_update_missing_reason_renders_notice(
    web_client: TestClient, db_sessionmaker, api_client: TestClient
) -> None:
    task_id = _submit_intake_proposal(api_client)
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.post(
        f"/admin/tasks/{task_id}/update",
        data={"status": "in_progress"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 400
    assert "reason" in resp.text.lower()


# =========================================================================================== approve-intake
def test_approve_intake_creates_record_and_shows_link(
    web_client: TestClient, db_sessionmaker, api_client: TestClient
) -> None:
    task_id = _submit_intake_proposal(api_client)
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.post(
        f"/admin/tasks/{task_id}/approve-intake",
        data={"decision": "approve", "reason": "verified with the sponsor"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    assert "record_id=prop_" in resp.headers["location"]

    detail = web_client.get(resp.headers["location"])
    assert detail.status_code == 200
    assert "prop_" in detail.text
    assert "Record created" in detail.text


def test_reject_intake_records_reason_and_closes(
    web_client: TestClient, db_sessionmaker, api_client: TestClient
) -> None:
    task_id = _submit_intake_proposal(api_client)
    _sign_in(web_client, db_sessionmaker)

    resp = web_client.post(
        f"/admin/tasks/{task_id}/approve-intake",
        data={"decision": "reject", "reason": "duplicate of an existing queue entry"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303
    detail = web_client.get(resp.headers["location"])
    assert "rejected" in detail.text


# ============================================================================================= deletion
def test_deletion_task_shows_redaction_warning_and_completes(web_client: TestClient, db_sessionmaker) -> None:
    task_id, user_public_id = _make_deletion_task(db_sessionmaker)
    _sign_in(web_client, db_sessionmaker)

    detail = web_client.get(f"/admin/tasks/{task_id}")
    assert detail.status_code == 200
    assert "redacts the user" in detail.text

    resp = web_client.post(
        f"/admin/tasks/{task_id}/update",
        data={"status": "done", "reason": "user requested deletion"},
        headers={"origin": "http://testserver"},
    )
    assert resp.status_code == 303, resp.text

    with db_sessionmaker() as db:
        user = db.query(User).filter_by(public_id=user_public_id).one()
        assert user.status == "anonymised"
        # Erasure tombstones the address (`erased-<hash>@erased.invalid`) so the row keeps a unique,
        # non-personal key; the real address survives only as a peppered hash in the audit log.
        assert user.email is not None and user.email.endswith("@erased.invalid")


def test_deletion_task_completion_503_when_crm_unreachable(web_client: TestClient, db_sessionmaker) -> None:
    task_id, _ = _make_deletion_task(db_sessionmaker)
    _sign_in(web_client, db_sessionmaker)
    api_app.dependency_overrides[get_crm_port] = lambda: _FlakyCrm()
    try:
        resp = web_client.post(
            f"/admin/tasks/{task_id}/update",
            data={"status": "done", "reason": "user requested deletion"},
            headers={"origin": "http://testserver"},
        )
        assert resp.status_code == 503
        assert "CRM" in resp.text
    finally:
        del api_app.dependency_overrides[get_crm_port]

    with db_sessionmaker() as db:
        task = db.query(Task).filter_by(public_id=task_id).one()
        assert task.status == "open"
