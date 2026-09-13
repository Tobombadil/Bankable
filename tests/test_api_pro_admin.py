"""The interim admin entitlement-grant endpoint (task rule: "billing is Sprint 3 so entitlements
are set by an admin-only endpoint for now, documented as such")."""

from __future__ import annotations

from services.db.models import Event
from tests.conftest import login, make_account, make_user


def test_admin_set_entitlement_requires_operator_role(client, db):
    account = make_account(db, entitlement="public")
    operator_account = make_account(db, entitlement="admin", name="Ops")
    non_operator = make_user(db, operator_account, email="viewer@example.com", role="viewer")
    db.commit()
    login(client, db, non_operator)

    resp = client.put(
        f"/admin/v1/accounts/{account.public_id}/entitlement",
        json={"entitlement": "pro", "reason": "trial upgrade"},
    )
    assert resp.status_code == 403


def test_admin_set_entitlement_updates_the_account_and_audits(client, db):
    account = make_account(db, entitlement="public", name="Customer")
    operator_account = make_account(db, entitlement="admin", name="Ops")
    operator = make_user(db, operator_account, email="ops@example.com", role="operator")
    db.commit()
    login(client, db, operator)

    resp = client.put(
        f"/admin/v1/accounts/{account.public_id}/entitlement",
        json={"entitlement": "pro", "reason": "manual trial grant, ticket #123"},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]["account"]
    assert body["entitlement"] == "pro"
    assert body["entitlement_source"] == "manual_grant"

    events = db.query(Event).filter_by(subject_type="account", event_type="admin_edit").all()
    assert len(events) == 1
    assert events[0].reason == "manual trial grant, ticket #123"
    assert events[0].before == {"entitlement": "public"}
    assert events[0].after == {"entitlement": "pro"}


def test_admin_set_entitlement_requires_a_reason(client, db):
    account = make_account(db, entitlement="public")
    operator_account = make_account(db, entitlement="admin", name="Ops")
    operator = make_user(db, operator_account, email="ops2@example.com", role="owner")
    db.commit()
    login(client, db, operator)

    resp = client.put(f"/admin/v1/accounts/{account.public_id}/entitlement", json={"entitlement": "pro"})
    assert resp.status_code == 400


def test_admin_set_entitlement_rejects_unknown_value(client, db):
    account = make_account(db, entitlement="public")
    operator_account = make_account(db, entitlement="admin", name="Ops")
    operator = make_user(db, operator_account, email="ops3@example.com", role="owner")
    db.commit()
    login(client, db, operator)

    resp = client.put(
        f"/admin/v1/accounts/{account.public_id}/entitlement",
        json={"entitlement": "gold", "reason": "x"},
    )
    assert resp.status_code == 400


def test_admin_set_entitlement_unknown_account_is_404(client, db):
    operator_account = make_account(db, entitlement="admin", name="Ops")
    operator = make_user(db, operator_account, email="ops4@example.com", role="owner")
    db.commit()
    login(client, db, operator)

    resp = client.put(
        "/admin/v1/accounts/acc_doesnotexist0000000000/entitlement",
        json={"entitlement": "pro", "reason": "x"},
    )
    assert resp.status_code == 404
