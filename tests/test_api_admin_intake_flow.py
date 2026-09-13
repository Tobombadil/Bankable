"""Cross-module flow (coordinator verification, Sprint 3 item 3): a public intake submission
(`services/api/admin_posts.py`) becomes a task an operator approves
(`services/api/admin_people.py`), and the task's pending record validates against the spec at
every step (US-1001, US-1002, US-204). Each module's own suite tests its half; this proves the
two halves agree on the pending-record shape through the real, mounted app.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from sqlalchemy import select

from services.db.models import Opportunity, Proposal, Task
from tests.conftest import login, make_account, make_user
from tests.test_api_contract import OPENAPI_PATH, assert_valid


@pytest.fixture(scope="module")
def spec() -> dict:
    with pathlib.Path(OPENAPI_PATH).open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


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

_OPPORTUNITY_SUBMISSION = {
    "title": "Solar RFP 2027",
    "kind": "rfp",
    "issuer_name": "Test Utility",
    "jurisdiction": "US-AZ",
    "technologies": ["solar_pv"],
    "url": "https://example.org/rfp/2027",
    "contact": {"name": "Sam Lee", "email": "sam@example.com"},
    "consent": True,
    "captcha_token": "test-token",
}


def _operator(db):
    ops = make_account(db, entitlement="admin", name="Ops")
    return make_user(db, ops, email="ops@example.com", role="operator")


def test_proposal_intake_is_reviewable_and_approvable_end_to_end(client, db, spec) -> None:
    accepted = client.post("/v1/intake/proposals", json=_PROPOSAL_SUBMISSION)
    assert accepted.status_code == 202, accepted.text
    assert_valid(spec, "IntakeAcceptedResponse", accepted.json())
    task_public_id = accepted.json()["task_id"]

    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    detail = client.get(f"/admin/v1/tasks/{task_public_id}")
    assert detail.status_code == 200, detail.text
    assert_valid(spec, "TaskDetailResponse", detail.json())
    assert detail.json()["data"]["type"] == "intake_proposal"
    assert detail.json()["data"]["pending_record"]["project_name"] == "Sunrise Storage"

    listing = client.get("/admin/v1/tasks", params={"type": "intake_proposal"})
    assert listing.status_code == 200, listing.text
    assert_valid(spec, "TaskListResponse", listing.json())

    decided = client.post(
        f"/admin/v1/tasks/{task_public_id}/approve-intake",
        json={"decision": "approve", "create_lead": False, "reason": "verified with the sponsor"},
    )
    assert decided.status_code == 200, decided.text
    assert_valid(spec, "IntakeDecisionResponse", decided.json())
    record = decided.json()["data"]["record"]
    assert record["name_canonical"] == "Sunrise Storage"
    assert record["publish_state"] == "pending_review"

    created = db.scalar(select(Proposal).where(Proposal.public_id == record["public_id"]))
    assert created is not None
    assert created.sponsor is not None and created.sponsor.name_canonical == "Sunrise Power LLC"
    task = db.scalar(select(Task).where(Task.public_id == task_public_id))
    assert task is not None and task.status == "done" and task.subject_id == created.id


def test_opportunity_intake_is_approvable_end_to_end(client, db, spec) -> None:
    accepted = client.post("/v1/intake/opportunities", json=_OPPORTUNITY_SUBMISSION)
    assert accepted.status_code == 202, accepted.text
    task_public_id = accepted.json()["task_id"]

    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    detail = client.get(f"/admin/v1/tasks/{task_public_id}")
    assert detail.status_code == 200, detail.text
    assert_valid(spec, "TaskDetailResponse", detail.json())

    decided = client.post(
        f"/admin/v1/tasks/{task_public_id}/approve-intake",
        json={"decision": "approve", "reason": "issuer confirmed by phone"},
    )
    assert decided.status_code == 200, decided.text
    assert_valid(spec, "IntakeDecisionResponse", decided.json())
    record = decided.json()["data"]["record"]
    assert record["title"] == "Solar RFP 2027"
    created = db.scalar(select(Opportunity).where(Opportunity.public_id == record["public_id"]))
    assert created is not None


def test_rejected_intake_creates_no_record(client, db, spec) -> None:
    accepted = client.post("/v1/intake/proposals", json=_PROPOSAL_SUBMISSION)
    task_public_id = accepted.json()["task_id"]
    operator = _operator(db)
    db.commit()
    login(client, db, operator)

    decided = client.post(
        f"/admin/v1/tasks/{task_public_id}/approve-intake",
        json={"decision": "reject", "reason": "duplicate of an existing queue entry"},
    )
    assert decided.status_code == 200, decided.text
    assert_valid(spec, "IntakeDecisionResponse", decided.json())
    assert db.scalar(select(Proposal).where(Proposal.name_canonical == "Sunrise Storage")) is None
    task = db.scalar(select(Task).where(Task.public_id == task_public_id))
    assert task is not None and task.status == "rejected"
