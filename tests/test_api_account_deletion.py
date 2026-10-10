"""A member deletes their own account in-app: `DELETE /v1/me` (`services/api/auth_routes.py`
decision 7), which runs the same erasure as an operator's completed deletion task
(`services/api/account_erasure.py`). Covers the success path and what is deleted versus kept per
table, the password re-authentication, the refusals (no session, an API key, a staff role, a body
naming someone else, too many attempts, the CRM down) and that the audit trail holds no personal
value. The CRM and billing ports are the in-memory fakes; nothing leaves the process."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.alerts.suppression import is_suppressed, suppress
from services.api.app import app
from services.api.audit import hash_identifier
from services.api.auth import ResendEmailAdapter, create_session, make_verification_token
from services.api.auth_routes import get_email_port
from services.billing.fake import InMemoryBilling
from services.crm.fake import InMemoryCrm
from services.db.models import (
    Account,
    Alert,
    ApiKey,
    Event,
    SavedSearch,
    Subscription,
    Suppression,
    Task,
    User,
    UserSession,
    WebhookEndpoint,
)
from services.ids import public_id
from services.sor.ports import SorUnavailable
from services.sor.wiring import get_billing_port, get_crm_port
from tests.conftest import make_account, make_api_key, make_user
from tests.test_api_contract import OPENAPI_PATH, assert_valid

PASSWORD = "correct horse battery staple"
UTC = dt.UTC


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    with OPENAPI_PATH.open(encoding="utf-8") as fh:
        loaded: dict[str, Any] = yaml.safe_load(fh)
    return loaded


@pytest.fixture(autouse=True)
def _audit_pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_HASH_PEPPER", "test-pepper-not-a-secret")


class _FlakyCrm(InMemoryCrm):
    def request_personal_data_deletion(self, *, email: str, reason: str) -> str:
        raise SorUnavailable("adapter down")


@pytest.fixture()
def ports(client: TestClient) -> Iterator[dict[str, Any]]:
    """Fresh fakes per test; `client`'s teardown clears every override."""
    crm, billing, email = InMemoryCrm(), InMemoryBilling(), ResendEmailAdapter(api_key="")
    app.dependency_overrides[get_crm_port] = lambda: crm
    app.dependency_overrides[get_billing_port] = lambda: billing
    app.dependency_overrides[get_email_port] = lambda: email
    yield {"crm": crm, "billing": billing}


def _delete(client: TestClient, body: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    return client.request("DELETE", "/v1/me", json={"password": PASSWORD} if body is None else body, **kwargs)


def _me_status(client: TestClient, cookie: str) -> int:
    client.cookies.clear()
    client.cookies.set("session", cookie)
    return client.get("/v1/me").status_code


def _register(client: TestClient, email: str) -> str:
    """A real registration: a `personal` account whose name is the address, signed in."""
    resp = client.post("/v1/auth/register", json={"email": email, "password": PASSWORD, "name": "Ana Ruiz"})
    assert resp.status_code == 201, resp.text
    cookie = resp.cookies.get("session")
    assert cookie
    return cookie


def _saved_search(db: Session, user: User, *, name: str = "TX storage") -> SavedSearch:
    search = SavedSearch(
        public_id="",
        user_id=user.id,
        account_id=user.account_id,
        name=name,
        entity="proposal",
        query={"technology": ["storage"]},
        query_hash=f"h-{name}",
        channels=["email", "rss"],
        rss_token=f"rss-{user.public_id}-{name}",
    )
    db.add(search)
    db.flush()
    search.public_id = public_id("ss", search.id)
    db.flush()
    return search


def _alert(db: Session, search: SavedSearch, user: User) -> Alert:
    now = dt.datetime.now(UTC)
    alert = Alert(
        public_id="",
        saved_search_id=search.id,
        user_id=user.id,
        window_start=now - dt.timedelta(days=1),
        window_end=now,
        event_seqs=[1, 2],
        recipient=user.email,
        subject="2 new storage proposals",
        status="sent",
        sent_at=now,
        unsubscribe_token=f"ut-{search.public_id}",
    )
    db.add(alert)
    db.flush()
    alert.public_id = public_id("alr", alert.id)
    db.flush()
    return alert


def _webhook(db: Session, user: User) -> WebhookEndpoint:
    endpoint = WebhookEndpoint(
        public_id="",
        account_id=user.account_id,
        created_by_user_id=user.id,
        url="https://hooks.example.com/infraque",
        types=["event.published"],
        secret="s" * 43,
    )
    db.add(endpoint)
    db.flush()
    endpoint.public_id = public_id("wh", endpoint.id)
    db.flush()
    return endpoint


def _user_by_public_id(db: Session, user_public_id: str) -> User:
    db.expire_all()
    user = db.scalar(select(User).where(User.public_id == user_public_id))
    assert user is not None
    return user


# ---------------------------------------------------------------------------------------- success
def test_a_member_deletes_their_own_account(
    client: TestClient, db: Session, ports: dict[str, Any], spec: dict[str, Any]
) -> None:
    cookie = _register(client, "leaving@example.com")
    user = db.scalar(select(User).where(User.email == "leaving@example.com"))
    assert user is not None
    user_public_id, user_id = user.public_id, user.id
    key, key_secret = make_api_key(db, user.account, user, scopes=["read:public"])
    key.last_used_ip = "198.51.100.0/24"
    search = _saved_search(db, user)
    alert = _alert(db, search, user)
    endpoint = _webhook(db, user)
    db.commit()
    _row, other_device = create_session(db, user)
    db.commit()

    client.cookies.clear()
    client.cookies.set("session", cookie)
    resp = _delete(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_valid(spec, "AccountDeletionResponse", body)
    data = body["data"]
    assert data["deleted"] is True and data["user_id"] == user_public_id
    assert data["sessions_deleted"] == 2
    assert data["api_keys_revoked"] == 1
    assert data["saved_searches_paused"] == 1
    assert data["alerts_redacted"] == 1
    assert data["webhooks_disabled"] == 1
    assert data["account_closed"] is True
    assert data["billing"] == "no_active_subscription"
    assert "leaving@example.com" not in resp.text and "Ana Ruiz" not in resp.text
    # The response itself signs the browser out.
    set_cookie = resp.headers.get("set-cookie", "")
    assert set_cookie.startswith('session=""') or "session=;" in set_cookie
    assert "Max-Age=0" in set_cookie or "max-age=0" in set_cookie.lower()

    # Signed out everywhere, and the old credentials no longer work.
    assert _me_status(client, cookie) == 401
    assert _me_status(client, other_device) == 401
    client.cookies.clear()
    login = client.post("/v1/auth/login", json={"email": "leaving@example.com", "password": PASSWORD})
    assert login.status_code == 401
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {key_secret}"}).status_code == 401

    # user: anonymised, not deleted.
    erased = _user_by_public_id(db, user_public_id)
    assert erased.email == f"erased-{hash_identifier('leaving@example.com')[:16]}@erased.invalid"
    assert erased.name is None and erased.password_hash is None
    assert erased.last_login_at is None and erased.sor_ref is None
    assert erased.status == "anonymised" and erased.anonymised_at is not None
    assert erased.marketing_consent is False
    # session: every row deleted.
    assert db.scalar(select(func.count()).select_from(UserSession).where(UserSession.user_id == user_id)) == 0
    # api_key: revoked, address cleared, row kept.
    db.refresh(key)
    assert key.revoked_at is not None and key.last_used_ip is None
    # saved_search: kept, paused, no email, no private feed.
    db.refresh(search)
    assert search.status == "paused" and "email" not in search.channels and search.rss_token is None
    # alert: the send log is kept without the address.
    db.refresh(alert)
    assert alert.recipient is None and alert.status == "sent" and alert.sent_at is not None
    # webhook: disabled.
    db.refresh(endpoint)
    assert endpoint.status == "disabled"
    # account: the personal account held the address as its name; renamed and closed.
    account = db.get(Account, erased.account_id)
    assert account is not None
    assert account.name == "Deleted account" and account.status == "closed"
    # suppression: the hash, never the address.
    suppression = db.scalar(select(Suppression).where(Suppression.reason == "erasure"))
    assert suppression is not None
    assert suppression.email_hash == hash_identifier("leaving@example.com")

    # The audit trail: hashes and counts, written by the member, nothing personal in the clear.
    event = db.scalar(select(Event).where(Event.event_type == "personal_data_redacted"))
    assert event is not None
    assert event.subject_id == user_id and event.actor_user_id == user_id
    assert event.reason == "Account holder deleted their own account in-app"
    assert event.before is not None and event.after is not None
    assert event.before["email_hash"] == hash_identifier("leaving@example.com")
    assert event.before["name_hash"] == hash_identifier("Ana Ruiz")
    assert event.after["initiated_by"] == "account_holder"
    text = f"{event.before}{event.after}{event.reason}"
    assert "leaving@example.com" not in text and "Ana Ruiz" not in text

    # The record of the request: a deletion task, already done, pointing at the event.
    task = db.scalar(select(Task).where(Task.type == "deletion_request"))
    assert task is not None
    assert task.public_id == data["deletion_task_id"]
    assert task.status == "done" and task.completed_at is not None
    assert task.subject_type == "user" and task.subject_id == user_id
    assert task.audit_event_ids == [public_id("evt", event.id)]
    assert task.contact is None
    assert "leaving@example.com" not in (task.notes or "")

    # The CRM was asked, with the real address, through the dry-run fake.
    assert [t.email for t in ports["crm"].tasks.values()] == ["leaving@example.com"]


def test_the_address_can_register_again_after_deletion(client: TestClient, ports: dict[str, Any]) -> None:
    """The tombstone frees the address: the person can come back with a fresh account."""
    _register(client, "returning@example.com")
    assert _delete(client).status_code == 200
    client.cookies.clear()
    again = client.post("/v1/auth/register", json={"email": "returning@example.com", "password": PASSWORD})
    assert again.status_code == 201, again.text


# ---------------------------------------------- coming back: a verified sign-up lifts the erasure row
def _suppression_reasons(db: Session, email: str) -> list[str]:
    db.expire_all()
    digest = hash_identifier(email)
    return sorted(db.scalars(select(Suppression.reason).where(Suppression.email_hash == digest)).all())


def _user_by_email(db: Session, email: str) -> User:
    db.expire_all()
    user = db.scalar(select(User).where(User.email == email))
    assert user is not None
    return user


def _verify(client: TestClient, token: str) -> Any:
    return client.get("/v1/auth/verify", params={"token": token})


def test_a_verified_fresh_sign_up_lifts_the_erasure_suppression(
    client: TestClient, db: Session, ports: dict[str, Any]
) -> None:
    """Owner decision 2026-10-10: a person who deleted their account and signs up again with the same
    address gets alerts again once the new account's verification link proves the address.
    Registering alone lifts nothing, since anyone can type someone else's address."""
    _register(client, "returning@example.com")
    assert _delete(client).status_code == 200
    assert _suppression_reasons(db, "returning@example.com") == ["erasure"]

    client.cookies.clear()
    again = client.post("/v1/auth/register", json={"email": "returning@example.com", "password": PASSWORD})
    assert again.status_code == 201, again.text
    assert _suppression_reasons(db, "returning@example.com") == ["erasure"]  # not yet proven

    user = _user_by_email(db, "returning@example.com")
    assert _verify(client, make_verification_token(user)).json() == {"verified": True}
    assert _suppression_reasons(db, "returning@example.com") == []
    assert not is_suppressed(db, "returning@example.com")

    events = db.scalars(
        select(Event).where(Event.subject_id == user.id, Event.event_type == "suppression_lifted")
    ).all()
    assert len(events) == 1
    assert events[0].before == {"suppression": ["erasure"]} and events[0].after == {"suppression": []}
    assert "returning@example.com" not in repr((events[0].before, events[0].after, events[0].reason))

    # A second click on the link changes nothing and writes nothing.
    assert _verify(client, make_verification_token(user)).status_code == 200
    db.expire_all()
    assert (
        db.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.subject_id == user.id, Event.event_type == "suppression_lifted")
        )
        == 1
    )


def test_an_unsubscribe_survives_a_verified_sign_up(
    client: TestClient, db: Session, ports: dict[str, Any]
) -> None:
    """Only the erasure row is lifted: an unsubscribe (or a bounce, or a complaint) is the person's own
    instruction, which a new sign-up does not withdraw."""
    _register(client, "careful-return@example.com")
    assert _delete(client).status_code == 200
    suppress(db, "careful-return@example.com", "unsubscribe")
    db.commit()
    client.cookies.clear()
    assert (
        client.post(
            "/v1/auth/register", json={"email": "careful-return@example.com", "password": PASSWORD}
        ).status_code
        == 201
    )
    user = _user_by_email(db, "careful-return@example.com")
    assert _verify(client, make_verification_token(user)).status_code == 200
    assert _suppression_reasons(db, "careful-return@example.com") == ["unsubscribe"]
    assert is_suppressed(db, "careful-return@example.com")


def test_a_link_for_an_address_the_account_no_longer_has_lifts_nothing(
    client: TestClient, db: Session, ports: dict[str, Any]
) -> None:
    """The link proves the address it was sent to, not whatever the account holds now."""
    _register(client, "first@example.com")
    assert _delete(client).status_code == 200
    client.cookies.clear()
    assert (
        client.post(
            "/v1/auth/register", json={"email": "first@example.com", "password": PASSWORD}
        ).status_code
        == 201
    )
    user = _user_by_email(db, "first@example.com")
    token = make_verification_token(user)
    user.email = "second@example.com"
    db.commit()
    assert _verify(client, token).status_code == 200
    assert _suppression_reasons(db, "first@example.com") == ["erasure"]


# ------------------------------------------------------------------------------------- refusals
def _assert_untouched(db: Session, user_public_id: str, email: str, ports: dict[str, Any]) -> None:
    user = _user_by_public_id(db, user_public_id)
    assert user.email == email and user.status == "active" and user.password_hash is not None
    assert db.scalar(select(Event).where(Event.event_type == "personal_data_redacted")) is None
    assert db.scalar(select(Suppression)) is None
    assert db.scalar(select(Task)) is None
    assert ports["crm"].tasks == {}


def test_a_wrong_password_deletes_nothing(client: TestClient, db: Session, ports: dict[str, Any]) -> None:
    cookie = _register(client, "careful@example.com")
    user = db.scalar(select(User).where(User.email == "careful@example.com"))
    assert user is not None

    wrong = _delete(client, {"password": "not my password at all"})
    assert wrong.status_code == 400
    assert wrong.json()["code"] == "validation_error"
    assert wrong.json()["errors"] == [{"field": "password", "message": "does not match this account"}]
    missing = _delete(client, {})
    assert missing.status_code == 400 and missing.json()["errors"][0]["field"] == "password"
    blank = _delete(client, {"password": ""})
    assert blank.status_code == 400 and blank.json()["errors"][0]["field"] == "password"

    _assert_untouched(db, user.public_id, "careful@example.com", ports)
    assert _me_status(client, cookie) == 200, "a refused deletion leaves the session signed in"


def test_password_attempts_are_rate_limited_per_user(
    client: TestClient, db: Session, ports: dict[str, Any]
) -> None:
    _register(client, "guessed@example.com")
    for _ in range(5):
        assert _delete(client, {"password": "a wrong guess here"}).status_code == 400
    limited = _delete(client)
    assert limited.status_code == 429 and limited.json()["code"] == "rate_limited"
    assert "retry-after" in limited.headers
    user = db.scalar(select(User).where(User.email == "guessed@example.com"))
    assert user is not None and user.status == "active", "even the right password waits out the window"


def test_a_session_is_required(client: TestClient, db: Session, ports: dict[str, Any]) -> None:
    account = make_account(db, entitlement="api")
    user = make_user(db, account, email="keyholder@example.com")
    _key, secret = make_api_key(db, account, user, scopes=["read:public", "read:live"])
    db.commit()

    client.cookies.clear()
    assert _delete(client).status_code == 401
    keyed = _delete(client, headers={"Authorization": f"Bearer {secret}"})
    assert keyed.status_code == 401, "an API key cannot delete the account that issued it"
    assert keyed.json()["code"] == "unauthenticated"
    _assert_untouched(db, user.public_id, "keyholder@example.com", ports)


def test_a_member_cannot_name_another_user(client: TestClient, db: Session, ports: dict[str, Any]) -> None:
    """The body takes `password` only; the operation acts on the session's own user. And deleting
    one member of an organisation account leaves the others, and the account, as they were."""
    account = make_account(db, entitlement="pro")
    account.seats = 5
    me = make_user(db, account, email="me@example.com")
    colleague = make_user(db, account, email="colleague@example.com")
    _colleague_key, _ = make_api_key(db, account, colleague)
    colleague_search = _saved_search(db, colleague, name="colleague's")
    db.commit()
    _row, colleague_cookie = create_session(db, colleague)
    db.commit()
    login = client.post("/v1/auth/login", json={"email": "me@example.com", "password": PASSWORD})
    assert login.status_code == 200, login.text
    my_cookie = login.cookies.get("session")
    assert my_cookie

    named = _delete(client, {"password": PASSWORD, "user_id": colleague.public_id})
    assert named.status_code == 400 and named.json()["errors"][0]["field"] == "user_id"
    _assert_untouched(db, colleague.public_id, "colleague@example.com", ports)
    _assert_untouched(db, me.public_id, "me@example.com", ports)

    client.cookies.clear()
    client.cookies.set("session", my_cookie)
    assert _delete(client).status_code == 200
    assert _user_by_public_id(db, me.public_id).status == "anonymised"
    survivor = _user_by_public_id(db, colleague.public_id)
    assert survivor.status == "active" and survivor.email == "colleague@example.com"
    assert _me_status(client, colleague_cookie) == 200
    colleague_key = db.scalar(select(ApiKey).where(ApiKey.created_by_user_id == colleague.id))
    assert colleague_key is not None and colleague_key.revoked_at is None
    db.refresh(colleague_search)
    assert colleague_search.status == "active" and "email" in colleague_search.channels
    db.refresh(account)
    assert account.name == "Acme Capital" and account.status == "active", "an organisation account stays"


@pytest.mark.parametrize("role", ["operator", "legal", "owner"])
def test_staff_roles_cannot_delete_themselves_in_app(
    role: str, client: TestClient, db: Session, ports: dict[str, Any]
) -> None:
    account = make_account(db, entitlement="admin", name="Ops")
    user = make_user(db, account, email=f"{role}@example.com", role=role)
    db.commit()
    login = client.post("/v1/auth/login", json={"email": f"{role}@example.com", "password": PASSWORD})
    assert login.status_code == 200

    resp = _delete(client)
    assert resp.status_code == 409 and resp.json()["code"] == "conflict"
    assert role in resp.json()["detail"]
    _assert_untouched(db, user.public_id, f"{role}@example.com", ports)


def test_the_crm_being_down_deletes_nothing(client: TestClient, db: Session, ports: dict[str, Any]) -> None:
    app.dependency_overrides[get_crm_port] = lambda: _FlakyCrm()
    cookie = _register(client, "patient@example.com")
    user = db.scalar(select(User).where(User.email == "patient@example.com"))
    assert user is not None

    resp = _delete(client)
    assert resp.status_code == 503 and resp.json()["code"] == "sor_unavailable"
    assert "Nothing was deleted" in resp.json()["detail"]
    _assert_untouched(db, user.public_id, "patient@example.com", ports)
    assert _me_status(client, cookie) == 200


# --------------------------------------------------------------------------------------- billing
def test_a_subscription_the_port_cannot_cancel_leaves_the_task_open_for_an_operator(
    client: TestClient, db: Session, ports: dict[str, Any], spec: dict[str, Any]
) -> None:
    """`BillingPort` has no cancel operation yet, so a personal account's live subscription is
    recorded as pending: the task stays open, naming the subscription, and the erasure is done."""
    account = Account(public_id="", name="solo@example.com", kind="personal", entitlement="pro")
    db.add(account)
    db.flush()
    account.public_id = public_id("acc", account.id)
    user = make_user(db, account, email="solo@example.com")
    now = dt.datetime.now(UTC)
    sub = Subscription(
        public_id="",
        account_id=account.id,
        sor_kind="stripe",
        sor_ref="sub_fake_solo",
        plan_code="pro_monthly",
        plan_tier="pro",
        status="active",
        seats=1,
        current_period_start=now - dt.timedelta(days=3),
        current_period_end=now + dt.timedelta(days=27),
        currency="USD",
    )
    db.add(sub)
    db.flush()
    sub.public_id = public_id("subn", sub.id)
    db.commit()
    login = client.post("/v1/auth/login", json={"email": "solo@example.com", "password": PASSWORD})
    assert login.status_code == 200

    resp = _delete(client)
    assert resp.status_code == 200, resp.text
    assert_valid(spec, "AccountDeletionResponse", resp.json())
    assert resp.json()["data"]["billing"] == "cancellation_pending"
    assert _user_by_public_id(db, user.public_id).status == "anonymised"
    task = db.scalar(select(Task).where(Task.type == "deletion_request"))
    assert task is not None and task.status == "open" and task.completed_at is None
    assert "sub_fake_solo" in (task.notes or "") and "solo@example.com" not in (task.notes or "")
    event = db.scalar(select(Event).where(Event.event_type == "personal_data_redacted"))
    assert event is not None and event.after is not None
    assert event.after["subscription_refs"] == ["sub_fake_solo"]
    db.refresh(sub)
    assert sub.status == "active", "the mirror is written only by the provider's webhook"
