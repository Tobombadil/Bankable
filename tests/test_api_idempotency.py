"""`Idempotency-Key` on mutating calls (api/openapi.yaml `IdempotencyKey`; docs/23 §1; backend
audit 2026-09-30 F12: two `POST /v1/webhooks` with one key created two endpoints)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.api import idempotency
from services.api.auth import resolve_request_auth
from services.db.models import IdempotencyRecord, WebhookEndpoint
from tests.conftest import make_account, make_api_key, make_user

HOOK = {"url": "https://example.com/hook", "types": ["event.published"]}


def _key_headers(db: Session, name: str = "Acme") -> dict[str, str]:
    account = make_account(db, entitlement="api", name=name)
    user = make_user(db, account, email=f"{name.lower()}@example.com")
    _key, secret = make_api_key(db, account, user, scopes=["read:live", "write:webhooks"])
    db.commit()
    return {"Authorization": f"Bearer {secret}"}


def _endpoints(db: Session) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(WebhookEndpoint)) or 0


def test_a_repeated_key_replays_the_first_response_and_writes_once(client: TestClient, db: Session) -> None:
    headers = {**_key_headers(db), "Idempotency-Key": "key-0001-abcdef"}
    first = client.post("/v1/webhooks", json=HOOK, headers=headers)
    second = client.post("/v1/webhooks", json=HOOK, headers=headers)
    assert first.status_code == second.status_code == 201
    assert second.json()["data"] == first.json()["data"]
    assert second.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert _endpoints(db) == 1


def test_without_a_key_each_call_acts(client: TestClient, db: Session) -> None:
    headers = _key_headers(db)
    client.post("/v1/webhooks", json=HOOK, headers=headers)
    client.post("/v1/webhooks", json=HOOK, headers=headers)
    assert _endpoints(db) == 2


def test_the_same_key_with_another_body_is_409(client: TestClient, db: Session) -> None:
    headers = {**_key_headers(db), "Idempotency-Key": "key-0002-abcdef"}
    assert client.post("/v1/webhooks", json=HOOK, headers=headers).status_code == 201
    other = client.post("/v1/webhooks", json={**HOOK, "url": "https://example.com/other"}, headers=headers)
    assert other.status_code == 409
    assert other.headers["content-type"].startswith("application/problem+json")
    assert other.json()["code"] == "conflict"
    assert _endpoints(db) == 1


def test_a_key_is_scoped_to_its_account(client: TestClient, db: Session) -> None:
    a = {**_key_headers(db, "Acme"), "Idempotency-Key": "shared-key-123"}
    b = {**_key_headers(db, "Beta"), "Idempotency-Key": "shared-key-123"}
    assert client.post("/v1/webhooks", json=HOOK, headers=a).status_code == 201
    replay_b = client.post("/v1/webhooks", json=HOOK, headers=b)
    assert replay_b.status_code == 201 and "Idempotent-Replayed" not in replay_b.headers
    assert _endpoints(db) == 2


def test_a_failed_request_is_not_stored_so_a_corrected_retry_acts(client: TestClient, db: Session) -> None:
    headers = {**_key_headers(db), "Idempotency-Key": "key-0003-abcdef"}
    bad = client.post("/v1/webhooks", json={**HOOK, "url": "http://insecure"}, headers=headers)
    assert bad.status_code == 400
    good = client.post("/v1/webhooks", json=HOOK, headers=headers)
    assert good.status_code == 201 and "Idempotent-Replayed" not in good.headers
    assert _endpoints(db) == 1


def test_a_malformed_key_is_400(client: TestClient, db: Session) -> None:
    headers = {**_key_headers(db), "Idempotency-Key": "short"}
    resp = client.post("/v1/webhooks", json=HOOK, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["errors"][0]["field"] == "Idempotency-Key"
    assert _endpoints(db) == 0


def test_an_expired_record_no_longer_replays(client: TestClient, db: Session) -> None:
    headers = {**_key_headers(db), "Idempotency-Key": "key-0004-abcdef"}
    assert client.post("/v1/webhooks", json=HOOK, headers=headers).status_code == 201
    record = db.scalar(select(IdempotencyRecord))
    assert record is not None
    record.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    db.commit()
    again = client.post("/v1/webhooks", json=HOOK, headers=headers)
    assert again.status_code == 201 and "Idempotent-Replayed" not in again.headers
    assert _endpoints(db) == 2


def test_a_concurrent_duplicate_rolls_back_its_write_and_answers_409(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both requests passed the lookup before either committed (the race the unique constraint
    exists for): the second one's commit fails on `(scope, key)`, so its endpoint is not created."""
    headers = {**_key_headers(db), "Idempotency-Key": "key-0005-abcdef"}
    assert client.post("/v1/webhooks", json=HOOK, headers=headers).status_code == 201

    def racing_lookup(request: Any, session: Session, key: str, digest: str) -> idempotency.PendingRecord:
        # As if the first request had not committed when this one looked: no existing record seen.
        ctx = resolve_request_auth(
            request, session, session_cookie=None, authorization=request.headers.get("authorization")
        )
        assert ctx.account is not None
        return idempotency.PendingRecord(
            session=session,
            scope=f"account:{ctx.account.id}",
            key=key,
            request_hash=digest,
            method=request.method,
            path=request.url.path,
        )

    monkeypatch.setattr(idempotency, "_lookup", racing_lookup)
    second = client.post("/v1/webhooks", json=HOOK, headers=headers)
    monkeypatch.undo()
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "conflict"
    assert second.json()["request_id"]
    assert _endpoints(db) == 1
