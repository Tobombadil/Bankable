"""Beta feedback through `POST /v1/reports` (`issue_type: beta_feedback`; services/api/admin_posts.py,
beta feedback note; api/openapi.yaml `ReportRequest`, `BetaFeedbackDetail`, `ReportContact`).

The private beta's "Tell us what you were trying to do" form (web/feedback.py) reuses the report
pipeline: a `report` task with no subject, in the same admin queue, behind the same rate limit,
honeypot and Turnstile rule. These tests hold the API half: what is accepted and refused, what is
stored (and that no IP address is), and that requests and the admin's view of the task validate
against the committed spec.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from services.api.admin_posts import _PAGE_PATH_RE, BETA_FEEDBACK_ISSUE, PAGE_PATH_MAX
from services.api.conftest import make_open_licence, make_public_source, make_visible_proposal
from services.db.models import Task
from tests.conftest import login, make_account, make_user

SPEC = yaml.safe_load(
    (pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml").read_text(encoding="utf-8")
)
SITE_TOKEN = "beta-feedback-test-token"  # noqa: S105 -- a test value, not a credential
VISITOR_IP = "198.51.100.23"  # TEST-NET-2 (RFC 5737)


def _errors(schema_name: str, instance: object) -> list[str]:
    schema = SPEC["components"]["schemas"][schema_name]
    resolver = jsonschema.validators.RefResolver.from_schema(SPEC)
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    return sorted(f"{e.message} at {list(e.absolute_path)}" for e in validator.iter_errors(instance))


def _feedback(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "issue_type": BETA_FEEDBACK_ISSUE,
        "description": "Compare two developers' pipelines in SPP.",
        "what_happened": "SPP is not covered yet, so both pipelines were empty.",
        "page_path": "/organizations/acme-power",
        "email": "tester@example.com",
    }
    body.update(overrides)
    return {key: value for key, value in body.items() if value is not None}


def _field(resp: Any) -> str:
    return str(resp.json()["errors"][0]["field"])


def test_feedback_becomes_a_report_task_with_no_subject(client: TestClient, db: Session) -> None:
    body = _feedback()
    assert _errors("ReportRequest", body) == []

    resp = client.post("/v1/reports", json=body)

    assert resp.status_code == 202, resp.text
    assert _errors("ReportAcceptedResponse", resp.json()) == []
    task = db.scalars(select(Task).where(Task.public_id == resp.json()["task_id"])).one()
    assert (task.type, task.status, task.issue_type) == ("report", "open", "beta_feedback")
    assert task.subject_type is None and task.subject_id is None
    assert task.description == "Compare two developers' pipelines in SPP."
    assert task.pending_record == {
        "page_path": "/organizations/acme-power",
        "what_happened": "SPP is not covered yet, so both pipelines were empty.",
    }
    assert task.contact == {"email": "tester@example.com"}


def test_the_operator_reads_it_in_the_task_queue_and_it_matches_the_spec(
    client: TestClient, db: Session
) -> None:
    task_id = client.post("/v1/reports", json=_feedback()).json()["task_id"]
    operator = make_user(
        db, make_account(db, entitlement="admin", name="Ops"), email="ops@example.com", role="operator"
    )
    login(client, db, operator)

    listed = client.get("/admin/v1/tasks", params={"type": "report", "status": "open"})
    detail = client.get(f"/admin/v1/tasks/{task_id}")

    assert listed.status_code == 200 and detail.status_code == 200
    [row] = [t for t in listed.json()["data"] if t["task_id"] == task_id]
    assert row["issue_type"] == "beta_feedback" and row["subject_id"] is None
    assert _errors("Task", detail.json()["data"]) == []


def test_only_what_was_given_is_stored(client: TestClient, db: Session) -> None:
    resp = client.post("/v1/reports", json=_feedback(what_happened=None, page_path=None, email=None))

    assert resp.status_code == 202
    task = db.scalars(select(Task)).one()
    assert task.contact is None and task.pending_record is None
    assert (
        client.post("/v1/reports", json=_feedback(email="", what_happened="", page_path="")).status_code
        == 202
    )


def test_no_ip_address_is_stored(client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """The site forwards the visitor's address for the rate limit (services/api/client_ip.py);
    nothing in the store may hold it afterwards."""
    monkeypatch.setenv("API_INTERNAL_TOKEN", SITE_TOKEN)
    headers = {"X-Internal-Token": SITE_TOKEN, "X-Visitor-IP": VISITOR_IP}

    assert client.post("/v1/reports", json=_feedback(), headers=headers).status_code == 202

    bind = db.get_bind()
    stored = "\n".join(
        str(tuple(row))
        for table in inspect(bind).get_table_names()
        for row in db.execute(text(f'SELECT * FROM "{table}"'))  # noqa: S608 -- table names from the schema
    )
    assert "Compare two developers" in stored  # the scan reads the task table
    assert VISITOR_IP not in stored and "testclient" not in stored


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (_feedback(public_id="prop_0123456789"), "public_id"),
        (_feedback(description=""), "description"),
        (_feedback(description="   "), "description"),
        (_feedback(description="x" * 2001), "description"),
        (_feedback(what_happened="x" * 2001), "what_happened"),
        (_feedback(what_happened=["a list"]), "what_happened"),
        (_feedback(email="not-an-address"), "email"),
        (_feedback(page_path="/proposals?q=wind"), "page_path"),
        (_feedback(page_path="/proposals#map"), "page_path"),
        (_feedback(page_path="//evil.example/phish"), "page_path"),
        (_feedback(page_path="/\\evil.example"), "page_path"),
        (_feedback(page_path="https://evil.example/"), "page_path"),
        (_feedback(page_path="/a b"), "page_path"),
        (_feedback(page_path="/" + "a" * PAGE_PATH_MAX), "page_path"),
        (_feedback(website="http://spam.example"), "website"),
    ],
)
def test_what_is_refused(client: TestClient, db: Session, body: dict[str, Any], field: str) -> None:
    resp = client.post("/v1/reports", json=body)

    assert resp.status_code == 400, resp.text
    assert _field(resp) == field
    assert db.scalars(select(Task)).all() == []
    # The spec refuses the same bodies, except three it cannot: the honeypot is not a documented
    # field, a description of spaces alone passes `minLength`, and `format: email` is an annotation
    # a validator without a format checker does not apply.
    if field not in ("website", "email") and body.get("description") != "   ":
        assert _errors("ReportRequest", body) != [], body


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/proposals",
        "/organizations/acme-power",
        "/a?b",
        "/a#b",
        "//a",
        "/\\a",
        "/a\tb",
        "a",
        "/a/b/c-d_e.f",
    ],
)
def test_the_code_and_the_spec_agree_on_page_paths(path: str) -> None:
    pattern = SPEC["components"]["schemas"]["ReportRequest"]["properties"]["page_path"]["pattern"]
    assert bool(_PAGE_PATH_RE.match(path)) is bool(re.match(pattern, path)), path


def test_a_record_report_still_needs_its_record(client: TestClient, db: Session) -> None:
    resp = client.post("/v1/reports", json={"issue_type": "other", "description": "no record named"})

    assert resp.status_code == 400 and _field(resp) == "public_id"
    assert _errors("ReportRequest", {"issue_type": "other", "description": "x"}) != []


def test_feedback_shares_the_report_and_intake_rate_limit(client: TestClient, db: Session) -> None:
    proposal = make_visible_proposal(db, make_public_source(db, make_open_licence(db)))
    db.commit()
    for _ in range(5):
        assert client.post("/v1/reports", json=_feedback()).status_code == 202

    record_report = {"public_id": proposal.public_id, "issue_type": "other", "description": "stale"}
    assert client.post("/v1/reports", json=record_report).status_code == 429
    assert client.post("/v1/reports", json=_feedback()).status_code == 429


def test_turnstile_is_required_when_verification_is_on(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "test-secret")

    resp = client.post("/v1/reports", json=_feedback())

    assert resp.status_code == 400 and _field(resp) == "captcha_token"


def test_an_idempotent_replay_is_the_same_task_and_a_changed_body_conflicts(client: TestClient) -> None:
    key = {"Idempotency-Key": "feedback-1"}
    first = client.post("/v1/reports", json=_feedback(), headers=key)
    again = client.post("/v1/reports", json=_feedback(), headers=key)
    changed = client.post("/v1/reports", json=_feedback(what_happened="Something else."), headers=key)

    assert first.status_code == again.status_code == 202
    assert first.json()["task_id"] == again.json()["task_id"]
    assert changed.status_code == 409
