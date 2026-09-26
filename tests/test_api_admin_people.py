"""Tests for `services/api/admin_people.py` — users, the task queue, customers and subscriptions
under `/admin/v1` (Sprint 3 item 3).

Builds a standalone FastAPI app carrying only this module's router plus the `ProblemError` handler
(the same "option B" `services/crm/test_router.py` already uses for this repo's admin/port-backed
routers) rather than importing `services.api.app.app`, so this suite never depends on load order
with the other agents' concurrent work in `services/api/admin_sources.py`, `admin_records.py` and
`admin_posts.py`.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from collections.abc import Iterator

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.api.admin_intake import router as admin_intake_router
from services.api.admin_people import router
from services.api.audit import hash_identifier
from services.api.auth import create_session, hash_password
from services.api.deps import get_db
from services.api.errors import ProblemError, problem_exception_handler
from services.billing.fake import InMemoryBilling
from services.crm.fake import InMemoryCrm
from services.db.models import (
    Account,
    ApiKey,
    Event,
    Organization,
    Proposal,
    SavedSearch,
    Subscription,
    Suppression,
    Task,
    User,
)
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id, slugify
from services.sor.ports import SorUnavailable
from services.sor.wiring import get_billing_port, get_crm_port
from tests.test_api_contract import assert_valid

UTC = dt.UTC
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
OPENAPI_PATH = REPO_ROOT / "api" / "openapi.yaml"


@pytest.fixture(scope="module")
def spec() -> dict:
    with OPENAPI_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(autouse=True)
def _audit_pepper(monkeypatch):
    """`services/api/audit.py` hashes identifiers with a server-side pepper; tests use an
    obviously fake one rather than the dev fallback."""
    monkeypatch.setenv("AUDIT_HASH_PEPPER", "test-pepper-not-a-secret")


class _CancellingBilling(InMemoryBilling):
    """`InMemoryBilling` plus the `cancel_subscription` operation the erasure path calls when the
    adapter offers it (`services.sor.ports.BillingPort` does not declare one yet; see
    `_cancel_billing_for_erased_user`)."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []

    def cancel_subscription(self, *, ref: str) -> str:
        self.cancelled.append(ref)
        return f"cancelled:{ref}"


def _make_saved_search(db: Session, account: Account, user: User) -> SavedSearch:
    search = SavedSearch(
        public_id="",
        user_id=user.id,
        account_id=account.id,
        name="mine",
        entity="proposal",
        query={},
        query_hash="x",
        channels=["email", "rss"],
    )
    db.add(search)
    db.flush()
    search.public_id = public_id("ss", search.id)
    db.flush()
    return search


def _make_subscription(db: Session, account: Account, *, status: str = "active") -> Subscription:
    now = dt.datetime.now(UTC)
    sub = Subscription(
        public_id="",
        account_id=account.id,
        sor_kind="stripe",
        sor_ref=f"sub_fake_{account.public_id[-6:]}",
        plan_code="pro_monthly",
        plan_tier="pro",
        status=status,
        seats=1,
        current_period_start=now - dt.timedelta(days=10),
        current_period_end=now + dt.timedelta(days=20),
        currency="USD",
    )
    db.add(sub)
    db.flush()
    sub.public_id = public_id("sub", sub.id)
    db.flush()
    return sub


class _FlakyCrm(InMemoryCrm):
    """`InMemoryCrm` that raises `SorUnavailable` on demand — used to prove a deletion task stays
    open and a customer is not created when the adapter is down."""

    def __init__(self, *, fail: bool = True) -> None:
        super().__init__()
        self.fail = fail

    def request_personal_data_deletion(self, *, email: str, reason: str) -> str:
        if self.fail:
            raise SorUnavailable("adapter down")
        return super().request_personal_data_deletion(email=email, reason=reason)

    def upsert_company(self, company):  # type: ignore[no-untyped-def, override]
        if self.fail:
            raise SorUnavailable("adapter down")
        return super().upsert_company(company)


def _build_app() -> FastAPI:
    """`router` (`services/api/admin_people.py`) plus `admin_intake_router`
    (`services/api/admin_intake.py`, docs/42-backend-review-2026-09-26.md §7 lane L4): the
    approve-intake tests below exercise a route that moved out of `admin_people.py` into its own
    module, mounted here the same way `services/api/app.py` mounts both."""
    app = FastAPI()
    app.add_exception_handler(ProblemError, problem_exception_handler)
    app.include_router(router)
    app.include_router(admin_intake_router)
    return app


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def db(db_sessionmaker: sessionmaker[Session]) -> Iterator[Session]:
    with db_sessionmaker() as s:
        yield s


@pytest.fixture()
def fake_crm() -> InMemoryCrm:
    return InMemoryCrm()


@pytest.fixture()
def fake_billing() -> InMemoryBilling:
    return InMemoryBilling()


@pytest.fixture()
def client(
    db_sessionmaker: sessionmaker[Session], fake_crm: InMemoryCrm, fake_billing: InMemoryBilling
) -> Iterator[TestClient]:
    app = _build_app()

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_crm_port] = lambda: fake_crm
    app.dependency_overrides[get_billing_port] = lambda: fake_billing
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------------------- fixtures
def _make_account(
    db: Session, *, entitlement: str = "public", name: str = "Acme Capital", kind: str = "organization"
) -> Account:
    account = Account(public_id="", name=name, kind=kind, entitlement=entitlement)
    db.add(account)
    db.flush()
    account.public_id = public_id("acc", account.id)
    db.flush()
    return account


def _make_user(
    db: Session,
    account: Account,
    *,
    email: str = "ana@example.com",
    role: str = "member",
    status: str = "active",
    password: str = "correct horse battery staple",
) -> User:
    user = User(
        public_id="",
        account_id=account.id,
        email=email,
        password_hash=hash_password(password),
        name="Ana Ruiz",
        role=role,
        status=status,
        auth_provider="password",
    )
    db.add(user)
    db.flush()
    user.public_id = public_id("usr", user.id)
    db.flush()
    return user


def _login(client: TestClient, db: Session, user: User) -> None:
    _row, cookie_value = create_session(db, user)
    db.commit()
    client.cookies.set("session", cookie_value)


def _operator(db: Session, *, role: str = "operator") -> User:
    ops_account = _make_account(db, entitlement="admin", name="Ops")
    return _make_user(db, ops_account, email=f"{role}@example.com", role=role)


def _make_bare_proposal(
    db: Session, *, name: str = "Existing Solar", jurisdiction: str = "US-TX"
) -> Proposal:
    p = Proposal(
        public_id="",
        slug="",
        kind="generation",
        name_canonical=name,
        jurisdiction=jurisdiction,
        identifiers={"a": 1},
    )
    db.add(p)
    db.flush()
    p.public_id = public_id("prop", p.id)
    p.slug = slugify(name)
    db.flush()
    return p


def _make_intake_task(
    db: Session,
    *,
    type_: str = "intake_proposal",
    pending: dict | None = None,
    public_opt_in: bool = False,
    status: str = "open",
) -> Task:
    task = Task(
        public_id="",
        type=type_,
        status=status,
        pending_record=pending,
        public_opt_in=public_opt_in,
    )
    db.add(task)
    db.flush()
    task.public_id = public_id("task", task.id)
    db.flush()
    return task


def _now_iso() -> str:
    return dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _pending_proposal(**overrides: object) -> dict:
    """`Task.pending_record` validates as `AdminProposal | AdminOpportunity | null` (the schema
    types it as a preview of the record-to-be, not the raw submission form) — so every fixture
    used in an `assert_valid`-checked response carries the schema's required scaffolding
    (placeholder `public_id`/`slug`/`url`/`provenance`/...) plus the extra, schema-permitted
    convenience keys `admin_people.py` actually reads (`sponsor_name`, decision 1)."""
    now = _now_iso()
    base: dict[str, object] = {
        "public_id": "prop_0000000000",
        "slug": "pending-project",
        "url": "https://example.test/proposals/pending-project",
        "kind": "storage",
        "name_canonical": "Gemini Solar + Storage",
        "sponsor": None,
        "sponsor_name": "Acme Developer LLC",
        "technology": "bess_li_ion",
        "capacity_mw": 150.0,
        "jurisdiction": "US-TX",
        "lifecycle_state": "announced",
        "identifiers": {"queue_id": "Q-100"},
        "first_seen": now,
        "last_changed": now,
        "min_reuse_class": "open",
        "source_count": 0,
        "created_by": "user",
        "provenance": [],
        "publish_state": "pending_review",
        "overrides": {},
    }
    base.update(overrides)
    return base


def _pending_opportunity(**overrides: object) -> dict:
    now = _now_iso()
    base: dict[str, object] = {
        "public_id": "opp_0000000000",
        "slug": "pending-rfp",
        "url": "https://example.test/opportunities/pending-rfp",
        "kind": "rfp",
        "title": "Statewide battery storage RFP",
        "issuer": None,
        "issuer_name": "Arizona Public Service",
        "jurisdiction": "US-AZ",
        "technologies": ["storage"],
        "capacity_sought_mw": 200.0,
        "status": "open",
        "identifiers": {},
        "first_seen": now,
        "last_changed": now,
        "min_reuse_class": "open",
        "source_count": 0,
        "created_by": "user",
        "provenance": [],
        "publish_state": "pending_review",
        "overrides": {},
    }
    base.update(overrides)
    return base


# ============================================================================================ users
def test_list_users_requires_authentication(client: TestClient) -> None:
    resp = client.get("/admin/v1/users")
    assert resp.status_code == 401


def test_list_users_requires_operator_role(client: TestClient, db: Session) -> None:
    viewer_account = _make_account(db, name="Viewer Co")
    viewer = _make_user(db, viewer_account, email="viewer@example.com", role="viewer")
    db.commit()
    _login(client, db, viewer)

    resp = client.get("/admin/v1/users")
    assert resp.status_code == 403


def test_list_users_happy_path(client: TestClient, db: Session, spec: dict) -> None:
    operator = _operator(db)
    account = _make_account(db, entitlement="pro", name="Customer Co")
    _make_user(db, account, email="cust@example.com")
    db.commit()
    _login(client, db, operator)

    resp = client.get("/admin/v1/users")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "UserListResponse", body)
    emails = {row["email"] for row in body["data"]}
    assert "cust@example.com" in emails
    row = next(r for r in body["data"] if r["email"] == "cust@example.com")
    assert row["entitlement"] == "pro"
    assert row["account_name"] == "Customer Co"


def test_get_user_not_found(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    db.commit()
    _login(client, db, operator)

    resp = client.get("/admin/v1/users/usr_doesnotexist000000000")
    assert resp.status_code == 404


def test_get_user_happy_path(client: TestClient, db: Session, spec: dict) -> None:
    operator = _operator(db)
    account = _make_account(db)
    user = _make_user(db, account)
    db.commit()
    _login(client, db, operator)

    resp = client.get(f"/admin/v1/users/{user.public_id}")
    assert resp.status_code == 200
    assert_valid(spec, "UserDetailResponse", resp.json())


def test_update_user_requires_reason(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    account = _make_account(db)
    user = _make_user(db, account)
    db.commit()
    _login(client, db, operator)

    resp = client.patch(f"/admin/v1/users/{user.public_id}", json={"role": "member"})
    assert resp.status_code == 400


def test_update_user_requires_role_or_status(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    account = _make_account(db)
    user = _make_user(db, account)
    db.commit()
    _login(client, db, operator)

    resp = client.patch(f"/admin/v1/users/{user.public_id}", json={"reason": "just because"})
    assert resp.status_code == 400


def test_update_user_non_owner_cannot_grant_owner(client: TestClient, db: Session) -> None:
    operator = _operator(db, role="operator")
    account = _make_account(db)
    user = _make_user(db, account)
    db.commit()
    _login(client, db, operator)

    resp = client.patch(f"/admin/v1/users/{user.public_id}", json={"role": "owner", "reason": "promote"})
    assert resp.status_code == 403


def test_update_user_owner_can_grant_owner_and_sets_mfa(client: TestClient, db: Session, spec: dict) -> None:
    owner = _operator(db, role="owner")
    account = _make_account(db)
    user = _make_user(db, account)
    db.commit()
    _login(client, db, owner)

    resp = client.patch(
        f"/admin/v1/users/{user.public_id}", json={"role": "owner", "reason": "promote to owner"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "UserDetailResponse", body)
    assert body["data"]["role"] == "owner"
    assert body["data"]["mfa_enforced"] is True

    from services.db.models import Event

    events = db.query(Event).filter_by(subject_type="user", event_type="admin_edit").all()
    matching = [e for e in events if e.after.get("role") == "owner"]
    assert len(matching) == 1
    assert matching[0].before == {"role": "member"}
    assert matching[0].reason == "promote to owner"


def test_update_user_disable_revokes_sessions(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    account = _make_account(db)
    user = _make_user(db, account)
    db.commit()
    session_row, _cookie = create_session(db, user)
    db.commit()
    _login(client, db, operator)

    resp = client.patch(f"/admin/v1/users/{user.public_id}", json={"status": "disabled", "reason": "misuse"})
    assert resp.status_code == 200
    db.refresh(session_row)
    assert session_row.revoked_at is not None


def test_delete_user_creates_deletion_task_and_disables(client: TestClient, db: Session, spec: dict) -> None:
    operator = _operator(db)
    account = _make_account(db)
    user = _make_user(db, account)
    db.commit()
    session_row, _cookie = create_session(db, user)
    key = ApiKey(
        public_id="",
        account_id=account.id,
        created_by_user_id=user.id,
        name="k",
        prefix="bk_live",
        last4="abcd",
        key_hash="x" * 64,
        licence_accepted_version="api-licence-1.0",
    )
    db.add(key)
    db.flush()
    key.public_id = public_id("key", key.id)
    search = _make_saved_search(db, account, user)
    db.commit()
    _login(client, db, operator)

    resp = client.request(
        "DELETE", f"/admin/v1/users/{user.public_id}", json={"reason": "user requested deletion"}
    )
    assert resp.status_code == 202
    body = resp.json()
    assert_valid(spec, "TaskDetailResponse", body)
    assert body["data"]["type"] == "deletion_request"
    assert body["data"]["status"] == "open"

    db.refresh(user)
    assert user.status == "disabled"
    db.refresh(session_row)
    assert session_row.revoked_at is not None
    db.refresh(key)
    assert key.revoked_at is not None
    # Item 2/3 (docs/50 §3.1): the request pauses the user's alerts and suppresses the address
    # immediately, and the audit row never carries the address or the name.
    db.refresh(search)
    assert search.status == "paused"
    suppression = db.scalar(select(Suppression))
    assert suppression is not None
    assert suppression.reason == "erasure"
    assert suppression.email_hash == hash_identifier("ana@example.com")
    audit = db.scalar(select(Event).where(Event.subject_type == "user"))
    assert "ana@example.com" not in str(audit.before) + str(audit.after)
    assert "Ana Ruiz" not in str(audit.before) + str(audit.after)


def test_delete_user_requires_a_reason_with_min_length(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    account = _make_account(db)
    user = _make_user(db, account)
    db.commit()
    _login(client, db, operator)

    resp = client.request("DELETE", f"/admin/v1/users/{user.public_id}", json={"reason": "hi"})
    assert resp.status_code == 400


# ============================================================================================ tasks
def test_list_tasks_happy_path_oldest_first(client: TestClient, db: Session, spec: dict) -> None:
    operator = _operator(db)
    older = _make_intake_task(db, pending=_pending_proposal())
    db.flush()
    newer = _make_intake_task(db, type_="intake_opportunity", pending=_pending_opportunity())
    db.commit()
    _login(client, db, operator)

    resp = client.get("/admin/v1/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "TaskListResponse", body)
    ids = [row["task_id"] for row in body["data"]]
    assert ids.index(older.public_id) < ids.index(newer.public_id)


def test_get_task_not_found(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    db.commit()
    _login(client, db, operator)

    resp = client.get("/admin/v1/tasks/task_doesnotexist000000")
    assert resp.status_code == 404


def test_update_task_requires_reason(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    task = _make_intake_task(db)
    db.commit()
    _login(client, db, operator)

    resp = client.patch(f"/admin/v1/tasks/{task.public_id}", json={"notes": "looking into it"})
    assert resp.status_code == 400


def test_update_task_generic_status_change_audits(client: TestClient, db: Session, spec: dict) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="report")
    db.commit()
    _login(client, db, operator)

    resp = client.patch(
        f"/admin/v1/tasks/{task.public_id}",
        json={"status": "in_progress", "notes": "triaging", "reason": "picked up"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "TaskDetailResponse", body)
    assert body["data"]["status"] == "in_progress"
    assert body["data"]["notes"] == "triaging"
    assert len(body["data"]["audit_event_ids"]) == 1

    from services.db.models import Event

    events = db.query(Event).filter_by(subject_type="task", event_type="admin_edit").all()
    assert len(events) == 1
    assert events[0].before == {"status": "open", "notes": None}
    assert events[0].after == {"status": "in_progress", "notes": "triaging"}


def test_complete_deletion_task_redacts_and_requests_crm_deletion(
    client: TestClient, db: Session, fake_crm: InMemoryCrm, spec: dict
) -> None:
    operator = _operator(db)
    account = _make_account(db)
    subject = _make_user(db, account, email="delete-me@example.com")
    search = _make_saved_search(db, account, subject)
    task = _make_intake_task(db, type_="deletion_request")
    task.subject_type = "user"
    task.subject_id = subject.id
    task.contact = {"email": "delete-me@example.com", "name": "Ana Ruiz"}
    db.commit()
    session_row, _cookie = create_session(db, subject)
    db.commit()
    _login(client, db, operator)

    resp = client.patch(
        f"/admin/v1/tasks/{task.public_id}",
        json={"status": "done", "reason": "verified request"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "TaskDetailResponse", body)
    assert body["data"]["status"] == "done"
    assert body["data"]["contact"] is None

    db.refresh(subject)
    # Item 2 (docs/50 §3.1): anonymised, not merely flagged — tombstone email, null name/password.
    assert subject.email == f"erased-{hash_identifier('delete-me@example.com')[:16]}@erased.invalid"
    assert "delete-me" not in subject.email
    assert subject.name is None
    assert subject.password_hash is None
    assert subject.status == "anonymised"
    assert subject.anonymised_at is not None
    assert subject.marketing_consent is False
    db.refresh(session_row)
    assert session_row.revoked_at is not None
    db.refresh(search)
    assert search.status == "paused" and "email" not in search.channels
    db.refresh(task)
    assert task.contact is None

    # The append-only audit row carries hashes of the identifiers, never the values.
    audit = db.scalar(select(Event).where(Event.event_type == "personal_data_redacted"))
    assert audit is not None
    assert audit.before["email_hash"] == hash_identifier("delete-me@example.com")
    assert audit.before["name_hash"] == hash_identifier("Ana Ruiz")
    text = str(audit.before) + str(audit.after)
    assert "delete-me@example.com" not in text and "Ana Ruiz" not in text
    assert audit.after["suppressed"] is True
    assert audit.after["billing"] == "not_applicable", (
        "an organization account's subscription is not the user's"
    )

    suppression = db.scalar(select(Suppression).where(Suppression.reason == "erasure"))
    assert suppression is not None
    assert suppression.email_hash == hash_identifier("delete-me@example.com")

    assert len(fake_crm.tasks) == 1
    recorded = next(iter(fake_crm.tasks.values()))
    assert recorded.email == "delete-me@example.com", "the CRM deletion needs the real address"


def test_complete_deletion_task_cancels_a_personal_accounts_subscription_through_the_port(
    db_sessionmaker: sessionmaker[Session], fake_crm: InMemoryCrm
) -> None:
    billing = _CancellingBilling()
    app = _build_app()

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_crm_port] = lambda: fake_crm
    app.dependency_overrides[get_billing_port] = lambda: billing
    with TestClient(app) as client, db_sessionmaker() as db:
        operator = _operator(db)
        account = _make_account(db, kind="personal", entitlement="pro")
        subject = _make_user(db, account, email="solo@example.com")
        sub = _make_subscription(db, account)
        _make_subscription(db, _make_account(db, name="Other"), status="active")  # someone else's
        task = _make_intake_task(db, type_="deletion_request")
        task.subject_type = "user"
        task.subject_id = subject.id
        db.commit()
        _login(client, db, operator)

        resp = client.patch(
            f"/admin/v1/tasks/{task.public_id}", json={"status": "done", "reason": "verified"}
        )
        assert resp.status_code == 200
        assert billing.cancelled == [sub.sor_ref]
        audit = db.scalar(select(Event).where(Event.event_type == "personal_data_redacted"))
        assert audit.after["billing"] == "cancelled"
        assert audit.after["subscription_refs"] == [f"cancelled:{sub.sor_ref}"]


def test_complete_deletion_task_records_pending_cancellation_when_the_port_cannot_cancel(
    client: TestClient, db: Session
) -> None:
    """`InMemoryBilling` (like `BillingPort` today) has no cancel operation: the outcome is
    recorded as pending, never silently skipped."""
    operator = _operator(db)
    account = _make_account(db, kind="personal", entitlement="pro")
    subject = _make_user(db, account, email="solo2@example.com")
    sub = _make_subscription(db, account)
    task = _make_intake_task(db, type_="deletion_request")
    task.subject_type = "user"
    task.subject_id = subject.id
    db.commit()
    _login(client, db, operator)

    resp = client.patch(f"/admin/v1/tasks/{task.public_id}", json={"status": "done", "reason": "verified"})
    assert resp.status_code == 200
    audit = db.scalar(select(Event).where(Event.event_type == "personal_data_redacted"))
    assert audit.after["billing"] == "cancellation_pending"
    assert audit.after["subscription_refs"] == [sub.sor_ref]


def test_complete_deletion_task_sor_unavailable_leaves_user_and_task_untouched(
    db_sessionmaker: sessionmaker[Session],
) -> None:
    flaky = _FlakyCrm(fail=True)
    app = _build_app()

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_crm_port] = lambda: flaky
    with TestClient(app) as client, db_sessionmaker() as db:
        operator = _operator(db)
        account = _make_account(db)
        subject = _make_user(db, account, email="keep-me@example.com")
        task = _make_intake_task(db, type_="deletion_request")
        task.subject_type = "user"
        task.subject_id = subject.id
        db.commit()
        _login(client, db, operator)

        resp = client.patch(
            f"/admin/v1/tasks/{task.public_id}",
            json={"status": "done", "reason": "verified request"},
        )
        assert resp.status_code == 503

        db.refresh(subject)
        assert subject.email == "keep-me@example.com"
        assert subject.status == "active"
        db.refresh(task)
        assert task.status == "open"


def test_complete_deletion_task_twice_conflicts(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    account = _make_account(db)
    subject = _make_user(db, account, email="already-gone@example.com")
    task = _make_intake_task(db, type_="deletion_request")
    task.subject_type = "user"
    task.subject_id = subject.id
    task.status = "done"
    task.completed_at = dt.datetime.now(UTC)
    db.commit()
    _login(client, db, operator)

    resp = client.patch(f"/admin/v1/tasks/{task.public_id}", json={"status": "done", "reason": "retry"})
    assert resp.status_code == 409


# --------------------------------------------------------------------------------- approve-intake
def test_approve_intake_creates_proposal_and_links_task(client: TestClient, db: Session, spec: dict) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="intake_proposal", pending=_pending_proposal())
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "approve", "create_lead": False, "reason": "looks legitimate"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "IntakeDecisionResponse", body)
    data = body["data"]
    assert data["decision"] == "approve"
    assert data["record"]["name_canonical"] == "Gemini Solar + Storage"
    assert data["record"]["publish_state"] == "pending_review"
    assert data["task"]["status"] == "done"
    assert data["task"]["subject_type"] == "proposal"
    assert data["task"]["subject_id"] == data["record"]["public_id"]

    db.refresh(task)
    assert len(task.audit_event_ids) == 2
    created = db.scalar(select(Proposal).where(Proposal.public_id == data["record"]["public_id"]))
    assert created is not None
    assert created.sponsor is not None
    assert created.sponsor.name_canonical == "Acme Developer LLC"


def test_approve_intake_public_requires_opt_in(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="intake_proposal", pending=_pending_proposal(), public_opt_in=False)
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "approve", "publish_state": "public", "reason": "trying to publish"},
    )
    assert resp.status_code == 400


def test_approve_intake_reject_creates_no_record(client: TestClient, db: Session, spec: dict) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="intake_proposal", pending=_pending_proposal())
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "reject", "reason": "duplicate submission"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "IntakeDecisionResponse", body)
    assert body["data"]["record"] is None
    assert body["data"]["task"]["status"] == "rejected"


def test_approve_intake_create_lead_lands_company_and_signal(
    client: TestClient, db: Session, fake_crm: InMemoryCrm
) -> None:
    operator = _operator(db)
    pending = _pending_opportunity()
    task = _make_intake_task(db, type_="intake_opportunity", pending=pending)
    db.commit()
    _login(client, db, operator)

    # Give the issuer a resolvable domain so `create_lead` reaches `upsert_company` too.
    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "approve", "create_lead": True, "add_curated_issuer": True, "reason": "vetted RFP"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["record"]["kind"] == "rfp"

    assert len(fake_crm.signals) == 1
    signal_row = next(iter(fake_crm.signals.values()))
    assert signal_row.signal.event_type == "opportunity.rfp_opened"

    issuer = db.scalar(select(Organization).where(Organization.name_normalised == "arizona public service"))
    assert issuer is not None
    assert issuer.is_curated_issuer is True


def test_approve_intake_link_merges_identifiers_without_new_record(
    client: TestClient, db: Session, spec: dict
) -> None:
    operator = _operator(db)
    existing = _make_bare_proposal(db)
    task = _make_intake_task(
        db, type_="intake_proposal", pending=_pending_proposal(identifiers={"eia_id": "999"})
    )
    db.commit()
    _login(client, db, operator)

    before_count = db.query(Proposal).count()
    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "link", "link_to_public_id": existing.public_id, "reason": "same project"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "IntakeDecisionResponse", body)
    assert db.query(Proposal).count() == before_count  # no new record
    assert body["data"]["record"]["identifiers"] == {"a": 1, "eia_id": "999"}
    assert body["data"]["task"]["subject_id"] == existing.public_id


def test_approve_intake_conflict_when_task_not_open(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="intake_proposal", pending=_pending_proposal(), status="done")
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "approve", "reason": "too late"},
    )
    assert resp.status_code == 409


# ======================================================================================== customers
def test_create_customer_writes_company_and_stores_sor_ref(
    client: TestClient, db: Session, fake_crm: InMemoryCrm, spec: dict
) -> None:
    operator = _operator(db)
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        "/admin/v1/customers",
        json={
            "name": "New Customer LLC",
            "kind": "organization",
            "primary_contact_email": "contact@newcustomer.example",
            "reason": "pre-sell signed",
        },
    )
    assert resp.status_code == 200 or resp.status_code == 201
    body = resp.json()
    assert_valid(spec, "CustomerDetailResponse", body)
    account_view = body["data"]["account"]
    assert account_view["sor_kind"] == "attio_fake"
    assert len(body["data"]["users"]) == 1
    assert body["data"]["users"][0]["email"] == "contact@newcustomer.example"

    assert "newcustomer.example" in fake_crm.companies


def test_create_customer_sor_unavailable_refuses_write(db_sessionmaker: sessionmaker[Session]) -> None:
    flaky = _FlakyCrm(fail=True)
    app = _build_app()

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_crm_port] = lambda: flaky
    with TestClient(app) as client, db_sessionmaker() as db:
        operator = _operator(db)
        db.commit()
        _login(client, db, operator)

        before = db.query(Account).count()
        resp = client.post(
            "/admin/v1/customers",
            json={
                "name": "Doomed Customer",
                "kind": "organization",
                "primary_contact_email": "contact@doomed.example",
                "reason": "attempt during outage",
            },
        )
        assert resp.status_code == 503
        assert db.query(Account).count() == before


def test_list_customers_happy_path(client: TestClient, db: Session, spec: dict) -> None:
    operator = _operator(db)
    _make_account(db, name="Listed Co", entitlement="pro")
    db.commit()
    _login(client, db, operator)

    resp = client.get("/admin/v1/customers")
    assert resp.status_code == 200
    assert_valid(spec, "CustomerListResponse", resp.json())


def test_get_customer_not_found(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    db.commit()
    _login(client, db, operator)

    resp = client.get("/admin/v1/customers/acc_doesnotexist0000000000")
    assert resp.status_code == 404


# ===================================================================================== subscriptions
def test_create_subscription_requires_billing_ref(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    account = _make_account(db)
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        "/admin/v1/subscriptions",
        json={"account_id": account.public_id, "plan_code": "pro", "seats": 3, "reason": "signed"},
    )
    assert resp.status_code == 409


def test_create_subscription_unknown_plan_code(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    account = _make_account(db)
    account.billing_ref = "cus_123"
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        "/admin/v1/subscriptions",
        json={"account_id": account.public_id, "plan_code": "gold", "seats": 3, "reason": "signed"},
    )
    assert resp.status_code == 400


def test_create_subscription_flips_entitlement_and_validates(
    client: TestClient, db: Session, fake_billing: InMemoryBilling, spec: dict
) -> None:
    operator = _operator(db)
    account = _make_account(db, entitlement="public")
    account.billing_ref = "cus_456"
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        "/admin/v1/subscriptions",
        json={"account_id": account.public_id, "plan_code": "pro", "seats": 5, "reason": "annual contract"},
    )
    assert resp.status_code == 200 or resp.status_code == 201
    body = resp.json()
    assert_valid(spec, "SubscriptionDetailResponse", body)
    assert body["data"]["plan_tier"] == "pro"
    assert body["data"]["seats"] == 5

    db.refresh(account)
    assert account.entitlement == "pro"
    assert account.entitlement_source == "sor"
    assert len(fake_billing.subscriptions) == 1


# -------------------------------------------------------------------- small helpers, unit-tested
def test_decode_public_id_round_trips_and_rejects_junk() -> None:
    from services.api.admin_people import _decode_public_id

    account_uuid = _decode_public_id("acc_01JBQ7Z8KD")
    assert account_uuid is not None
    assert _decode_public_id("no-underscore") is None
    assert _decode_public_id("acc_not-crockford!!") is None


def test_parse_iso_datetime_and_date_reject_non_strings_and_bad_values() -> None:
    from services.api.admin_intake import _parse_iso_date, _parse_iso_datetime

    assert _parse_iso_datetime(None) is None
    assert _parse_iso_datetime(12345) is None
    assert _parse_iso_datetime("not-a-date") is None
    assert _parse_iso_datetime("2026-09-13T00:00:00Z") is not None
    assert _parse_iso_date(None) is None
    assert _parse_iso_date("not-a-date") is None
    assert _parse_iso_date("2026-09-13") is not None


def test_normalise_domain_and_domain_from_email_edge_cases() -> None:
    from services.api.admin_people import _domain_from_email
    from services.api.common import normalise_domain

    assert normalise_domain(None) is None
    assert normalise_domain("   ") is None
    assert normalise_domain("https://www.Example.com/path") == "example.com"
    assert normalise_domain("example.org:8443") == "example.org"
    assert _domain_from_email("not-an-email") is None
    assert _domain_from_email("person@gmail.com") is None  # free-mail, decision in module docstring
    assert _domain_from_email("person@realcompany.example") == "realcompany.example"


# ============================================================================ list filters, more
def test_list_users_filters_by_q_role_status_and_account_id(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    account_a = _make_account(db, name="Alpha Co")
    _make_user(db, account_a, email="alice@example.com", role="member", status="active")
    account_b = _make_account(db, name="Beta Co")
    _make_user(db, account_b, email="bob@example.com", role="viewer", status="disabled")
    db.commit()
    _login(client, db, operator)

    by_q = client.get("/admin/v1/users", params={"q": "alice"})
    assert {r["email"] for r in by_q.json()["data"]} == {"alice@example.com"}

    by_role = client.get("/admin/v1/users", params={"role": "viewer"})
    assert {r["email"] for r in by_role.json()["data"]} == {"bob@example.com"}

    by_status = client.get("/admin/v1/users", params={"status": "disabled"})
    assert {r["email"] for r in by_status.json()["data"]} == {"bob@example.com"}

    by_account = client.get("/admin/v1/users", params={"account_id": account_a.public_id})
    assert {r["email"] for r in by_account.json()["data"]} == {"alice@example.com"}

    missing_account = client.get("/admin/v1/users", params={"account_id": "acc_doesnotexist0000000000"})
    assert missing_account.json()["data"] == []


def test_list_users_unknown_query_param_is_400(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    db.commit()
    _login(client, db, operator)

    resp = client.get("/admin/v1/users", params={"bogus": "x"})
    assert resp.status_code == 400


def test_list_tasks_filters(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    report_task = _make_intake_task(db, type_="report", status="open")
    intake_task = _make_intake_task(
        db, type_="intake_proposal", pending=_pending_proposal(), status="in_progress"
    )
    assignee_account = _make_account(db, name="Assignee Co")
    assignee = _make_user(db, assignee_account, email="assignee@example.com")
    intake_task.assignee_user_id = assignee.id
    db.commit()
    _login(client, db, operator)

    by_type = client.get("/admin/v1/tasks", params={"type": "report"})
    assert {r["task_id"] for r in by_type.json()["data"]} == {report_task.public_id}

    by_status = client.get("/admin/v1/tasks", params={"status": "in_progress"})
    assert {r["task_id"] for r in by_status.json()["data"]} == {intake_task.public_id}

    by_assignee = client.get("/admin/v1/tasks", params={"assignee_user_id": assignee.public_id})
    assert {r["task_id"] for r in by_assignee.json()["data"]} == {intake_task.public_id}

    missing_assignee = client.get(
        "/admin/v1/tasks", params={"assignee_user_id": "usr_doesnotexist0000000000"}
    )
    assert missing_assignee.json()["data"] == []

    by_subject_type = client.get("/admin/v1/tasks", params={"subject_type": "proposal"})
    assert by_subject_type.status_code == 200

    bad_subject_id = client.get("/admin/v1/tasks", params={"subject_id": "not-a-public-id"})
    assert bad_subject_id.json()["data"] == []


def test_list_customers_filters_by_q_entitlement_and_status(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    _make_account(db, name="Findable Co", entitlement="pro")
    _make_account(db, name="Other Co", entitlement="public")
    db.commit()
    _login(client, db, operator)

    by_q = client.get("/admin/v1/customers", params={"q": "Findable"})
    assert [r["account"]["name"] for r in by_q.json()["data"]] == ["Findable Co"]

    by_entitlement = client.get("/admin/v1/customers", params={"entitlement": "pro"})
    assert [r["account"]["name"] for r in by_entitlement.json()["data"]] == ["Findable Co"]

    by_status = client.get("/admin/v1/customers", params={"status": "active"})
    # 3, not 2: `_operator` creates its own "Ops" account too, also `status="active"`.
    assert len(by_status.json()["data"]) == 3


# --------------------------------------------------------------------------- more validation paths
def test_update_task_rejects_unknown_status(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="report")
    db.commit()
    _login(client, db, operator)

    resp = client.patch(f"/admin/v1/tasks/{task.public_id}", json={"status": "not-a-status", "reason": "x"})
    assert resp.status_code == 400


def test_update_task_rejects_unknown_assignee(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="report")
    db.commit()
    _login(client, db, operator)

    resp = client.patch(
        f"/admin/v1/tasks/{task.public_id}",
        json={"assignee_user_id": "usr_doesnotexist0000000000", "reason": "x"},
    )
    assert resp.status_code == 400


def test_update_task_assigns_a_real_user(client: TestClient, db: Session, spec: dict) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="report")
    account = _make_account(db)
    assignee = _make_user(db, account, email="assign-me@example.com")
    db.commit()
    _login(client, db, operator)

    resp = client.patch(
        f"/admin/v1/tasks/{task.public_id}",
        json={"assignee_user_id": assignee.public_id, "reason": "assigning"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_valid(spec, "TaskDetailResponse", body)
    assert body["data"]["assignee_user_id"] == assignee.public_id


def test_update_user_rejects_unknown_role(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    account = _make_account(db)
    user = _make_user(db, account)
    db.commit()
    _login(client, db, operator)

    resp = client.patch(f"/admin/v1/users/{user.public_id}", json={"role": "superuser", "reason": "x"})
    assert resp.status_code == 400


def test_update_user_rejects_unknown_status(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    account = _make_account(db)
    user = _make_user(db, account)
    db.commit()
    _login(client, db, operator)

    resp = client.patch(f"/admin/v1/users/{user.public_id}", json={"status": "anonymised", "reason": "x"})
    assert resp.status_code == 400


def test_approve_intake_rejects_unknown_decision(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="intake_proposal", pending=_pending_proposal())
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "maybe", "reason": "x"},
    )
    assert resp.status_code == 400


def test_approve_intake_link_requires_link_to_public_id(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="intake_proposal", pending=_pending_proposal())
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "link", "reason": "x"},
    )
    assert resp.status_code == 400


def test_approve_intake_link_wrong_prefix_is_not_found(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="intake_proposal", pending=_pending_proposal())
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "link", "link_to_public_id": "opp_01JBQ8A2M0", "reason": "x"},
    )
    assert resp.status_code == 404


def test_approve_intake_link_missing_target_is_not_found(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    task = _make_intake_task(db, type_="intake_proposal", pending=_pending_proposal())
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "link", "link_to_public_id": "prop_01JBQ7Z8KD", "reason": "x"},
    )
    assert resp.status_code == 404


def test_approve_intake_unknown_sponsor_org_id_is_400(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    pending = _pending_proposal(sponsor_org_id="org_doesnotexist0000000000", sponsor_name=None)
    task = _make_intake_task(db, type_="intake_proposal", pending=pending)
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "approve", "create_lead": False, "reason": "x"},
    )
    assert resp.status_code == 400


def test_approve_intake_reuses_an_existing_organization_by_name(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    existing_org = Organization(
        public_id="",
        slug="acme-developer-llc",
        name_canonical="Acme Developer LLC",
        name_normalised="acme developer llc",
        type="developer",
        country="US",
    )
    db.add(existing_org)
    db.flush()
    existing_org.public_id = public_id("org", existing_org.id)
    db.commit()

    task = _make_intake_task(db, type_="intake_proposal", pending=_pending_proposal())
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "approve", "create_lead": False, "reason": "x"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["record"]["sponsor"]["public_id"] == existing_org.public_id
    assert db.query(Organization).filter_by(name_normalised="acme developer llc").count() == 1


def test_approve_intake_opportunity_missing_title_is_400(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    pending = _pending_opportunity(title=None, name_canonical=None)
    task = _make_intake_task(db, type_="intake_opportunity", pending=pending)
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        f"/admin/v1/tasks/{task.public_id}/approve-intake",
        json={"decision": "approve", "create_lead": False, "reason": "x"},
    )
    assert resp.status_code == 400


def test_approve_intake_second_call_reuses_the_intake_source(client: TestClient, db: Session) -> None:
    """Exercises `_get_or_create_intake_source`'s early-return branch: a second approval finds the
    `intake.platform` source (and its licence) already on disk instead of recreating either."""
    operator = _operator(db)
    task_one = _make_intake_task(db, type_="intake_proposal", pending=_pending_proposal())
    db.commit()
    _login(client, db, operator)
    first = client.post(
        f"/admin/v1/tasks/{task_one.public_id}/approve-intake",
        json={"decision": "approve", "create_lead": False, "reason": "first"},
    )
    assert first.status_code == 200

    task_two = _make_intake_task(
        db, type_="intake_proposal", pending=_pending_proposal(name_canonical="Second Project")
    )
    db.commit()
    second = client.post(
        f"/admin/v1/tasks/{task_two.public_id}/approve-intake",
        json={"decision": "approve", "create_lead": False, "reason": "second"},
    )
    assert second.status_code == 200
    assert (
        first.json()["data"]["record"]["provenance"][0]["source_id"]
        == second.json()["data"]["record"]["provenance"][0]["source_id"]
    )

    from services.db.models import Licence, Source

    assert db.query(Source).filter_by(id="intake.platform").count() == 1
    assert db.query(Licence).filter_by(id="platform-open").count() == 1


def test_create_customer_requires_name_and_valid_kind(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    db.commit()
    _login(client, db, operator)

    missing_name = client.post("/admin/v1/customers", json={"kind": "organization", "reason": "x"})
    assert missing_name.status_code == 400

    bad_kind = client.post("/admin/v1/customers", json={"name": "X", "kind": "nonsense", "reason": "x"})
    assert bad_kind.status_code == 400


def test_create_customer_unknown_organization_id_is_400(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        "/admin/v1/customers",
        json={
            "name": "X",
            "kind": "organization",
            "organization_id": "org_doesnotexist0000000000",
            "reason": "x",
        },
    )
    assert resp.status_code == 400


def test_create_customer_skips_crm_for_free_mail_domain(
    client: TestClient, db: Session, fake_crm: InMemoryCrm
) -> None:
    operator = _operator(db)
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        "/admin/v1/customers",
        json={
            "name": "No Website Co",
            "kind": "organization",
            "primary_contact_email": "person@gmail.com",
            "reason": "x",
        },
    )
    assert resp.status_code in (200, 201)
    body = resp.json()["data"]
    assert body["account"]["sor_kind"] is None
    assert fake_crm.companies == {}


def test_create_customer_uses_organization_website_domain(
    client: TestClient, db: Session, fake_crm: InMemoryCrm
) -> None:
    operator = _operator(db)
    org = Organization(
        public_id="",
        slug="orgwithsite",
        name_canonical="Org With Site",
        name_normalised="org with site",
        type="developer",
        country="US",
        website="https://www.orgwithsite.example/about",
    )
    db.add(org)
    db.flush()
    org.public_id = public_id("org", org.id)
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        "/admin/v1/customers",
        json={
            "name": "Org With Site",
            "kind": "organization",
            "organization_id": org.public_id,
            "reason": "x",
        },
    )
    assert resp.status_code in (200, 201)
    assert "orgwithsite.example" in fake_crm.companies


def test_create_customer_sor_rejected_is_conflict(db_sessionmaker: sessionmaker[Session]) -> None:
    class _RejectingCrm(InMemoryCrm):
        def upsert_company(self, company):  # type: ignore[no-untyped-def, override]
            from services.sor.ports import SorRejected

            raise SorRejected("nope")

    app = _build_app()

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_crm_port] = lambda: _RejectingCrm()
    with TestClient(app) as client, db_sessionmaker() as db:
        operator = _operator(db)
        db.commit()
        _login(client, db, operator)

        resp = client.post(
            "/admin/v1/customers",
            json={
                "name": "Rejected Co",
                "kind": "organization",
                "primary_contact_email": "person@rejected.example",
                "reason": "x",
            },
        )
        assert resp.status_code == 409


def test_create_subscription_rejects_bad_seats_and_trial_days(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    account = _make_account(db)
    account.billing_ref = "cus_789"
    db.commit()
    _login(client, db, operator)

    bad_seats = client.post(
        "/admin/v1/subscriptions",
        json={"account_id": account.public_id, "plan_code": "pro", "seats": 0, "reason": "x"},
    )
    assert bad_seats.status_code == 400

    bad_trial = client.post(
        "/admin/v1/subscriptions",
        json={
            "account_id": account.public_id,
            "plan_code": "pro",
            "seats": 1,
            "trial_days": "soon",
            "reason": "x",
        },
    )
    assert bad_trial.status_code == 400


def test_create_subscription_unknown_account_is_404(client: TestClient, db: Session) -> None:
    operator = _operator(db)
    db.commit()
    _login(client, db, operator)

    resp = client.post(
        "/admin/v1/subscriptions",
        json={
            "account_id": "acc_doesnotexist0000000000",
            "plan_code": "pro",
            "seats": 1,
            "reason": "x",
        },
    )
    assert resp.status_code == 404


def test_create_subscription_billing_rejected_is_conflict(db_sessionmaker: sessionmaker[Session]) -> None:
    class _RejectingBilling(InMemoryBilling):
        def create_subscription(self, request):  # type: ignore[no-untyped-def, override]
            from services.sor.ports import SorRejected

            raise SorRejected("nope")

    app = _build_app()

    def _override_db() -> Iterator[Session]:
        s = db_sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_billing_port] = lambda: _RejectingBilling()
    with TestClient(app) as client, db_sessionmaker() as db:
        operator = _operator(db)
        account = _make_account(db)
        account.billing_ref = "cus_rejected"
        db.commit()
        _login(client, db, operator)

        resp = client.post(
            "/admin/v1/subscriptions",
            json={"account_id": account.public_id, "plan_code": "pro", "seats": 1, "reason": "x"},
        )
        assert resp.status_code == 409
