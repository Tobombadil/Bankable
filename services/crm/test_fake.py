"""InMemoryCrm: real read-back semantics, idempotency, and no delete method anywhere on the port
surface (docs/34 §5 "Never ... deletes anything")."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import inspect
import json

from services.crm.fake import InMemoryCrm
from services.sor.ports import (
    ActivityNote,
    CompanyUpsert,
    DealCreate,
    LeadSignal,
    SubscriptionMirror,
    WebhookRejected,
)

UTC = dt.UTC


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_upsert_and_find_and_get_company_round_trip() -> None:
    crm = InMemoryCrm()
    ref = crm.upsert_company(CompanyUpsert(domain="acme.example", platform_org_id="org_1", lead_score=42))
    assert ref.sor_kind == "attio_fake"

    found = crm.find_company(domain="acme.example")
    assert found is not None
    assert found.ref == ref
    assert found.domain == "acme.example"

    found_by_org = crm.find_company(platform_org_id="org_1")
    assert found_by_org is not None
    assert found_by_org.ref == ref

    fetched = crm.get_company(ref)
    assert fetched is not None
    assert fetched.domain == "acme.example"


def test_upsert_company_never_writes_name() -> None:
    crm = InMemoryCrm()
    ref = crm.upsert_company(CompanyUpsert(domain="acme.example", name="Should Not Appear"))
    company = crm.get_company(ref)
    assert company is not None
    assert company.name is None


def test_upsert_company_patches_only_given_fields() -> None:
    crm = InMemoryCrm()
    ref = crm.upsert_company(CompanyUpsert(domain="acme.example", lead_score=10, lead_band="warm"))
    crm.upsert_company(CompanyUpsert(domain="acme.example", lead_score=20))
    row = crm.companies["acme.example"]
    assert row.lead_score == 20
    assert row.lead_band == "warm"  # untouched by the second call
    assert crm.get_company(ref) is not None


def test_find_company_unknown_returns_none() -> None:
    crm = InMemoryCrm()
    assert crm.find_company(domain="nope.example") is None
    assert crm.find_company(platform_org_id="org_nope") is None


def test_contact_policy_unknown_company_is_not_allowed() -> None:
    crm = InMemoryCrm()
    policy = crm.contact_policy(domain="unknown.example")
    assert policy.do_not_contact is False
    assert policy.consent_basis is None
    assert policy.allowed is False  # no consent recorded => no draft


def test_contact_policy_reads_back_do_not_contact() -> None:
    crm = InMemoryCrm()
    crm.upsert_company(CompanyUpsert(domain="acme.example"))
    crm.companies["acme.example"].do_not_contact = True
    crm.companies["acme.example"].consent_basis = "consent"
    policy = crm.contact_policy(domain="acme.example")
    assert policy.do_not_contact is True
    assert policy.allowed is False  # do_not_contact wins


def test_create_lead_signal_resolves_by_domain() -> None:
    crm = InMemoryCrm()
    company_ref = crm.upsert_company(CompanyUpsert(domain="acme.example"))
    signal = LeadSignal(
        signal_id="evt_1",
        event_type="match.new",
        subject_kind="match",
        subject_name="X",
        subject_url="https://x",
        observed_at=dt.datetime.now(UTC),
        score=25,
        rationale="Because.",
        company_domain="acme.example",
    )
    ref = crm.create_lead_signal(signal)
    assert ref.unmatched is False
    assert ref.company == company_ref


def test_create_lead_signal_resolves_by_platform_org_id_when_domain_unset() -> None:
    crm = InMemoryCrm()
    company_ref = crm.upsert_company(CompanyUpsert(domain="acme.example", platform_org_id="org_9"))
    signal = LeadSignal(
        signal_id="evt_2",
        event_type="match.new",
        subject_kind="match",
        subject_name="X",
        subject_url="https://x",
        observed_at=dt.datetime.now(UTC),
        score=25,
        rationale="Because.",
        platform_org_id="org_9",
    )
    ref = crm.create_lead_signal(signal)
    assert ref.company == company_ref


def test_create_lead_signal_unresolved_lands_on_unmatched_list() -> None:
    crm = InMemoryCrm()
    signal = LeadSignal(
        signal_id="evt_3",
        event_type="proposal.new",
        subject_kind="proposal",
        subject_name="X",
        subject_url="https://x",
        observed_at=dt.datetime.now(UTC),
        score=8,
        rationale="Because.",
    )
    ref = crm.create_lead_signal(signal)
    assert ref.unmatched is True
    assert ref.company is None
    assert len(crm.unmatched) == 1
    assert crm.unmatched[0].signal_record_id == ref.sor_ref


def test_create_lead_signal_is_idempotent_on_signal_id() -> None:
    crm = InMemoryCrm()
    signal = LeadSignal(
        signal_id="evt_4",
        event_type="proposal.new",
        subject_kind="proposal",
        subject_name="X",
        subject_url="https://x",
        observed_at=dt.datetime.now(UTC),
        score=8,
        rationale="Because.",
    )
    ref1 = crm.create_lead_signal(signal)
    ref2 = crm.create_lead_signal(signal)
    assert ref1.sor_ref == ref2.sor_ref
    assert len(crm.signals) == 1
    # calling twice while unresolved must not double-enter the Unmatched list
    assert len(crm.unmatched) == 1


def test_create_deal_links_company_and_signal_and_writes_a_note() -> None:
    crm = InMemoryCrm()
    crm.upsert_company(CompanyUpsert(domain="acme.example"))
    signal = LeadSignal(
        signal_id="evt_5",
        event_type="match.new",
        subject_kind="match",
        subject_name="X",
        subject_url="https://x",
        observed_at=dt.datetime.now(UTC),
        score=25,
        rationale="Because.",
        company_domain="acme.example",
    )
    crm.create_lead_signal(signal)
    deal_ref = crm.create_deal(
        DealCreate(
            name="Acme × Regional RFP",
            proposal_public_id="prop_1",
            opportunity_public_id="opp_1",
            score=0.85,
            rationale="Because.",
            link_url="https://platform/proposals/acme",
            company_domain="acme.example",
            originating_signal_id="evt_5",
        )
    )
    deal_row = crm.deals[deal_ref.sor_ref]
    assert deal_row.company_ref is not None
    assert deal_row.signal_record_id == crm.signals["evt_5"].record_id
    notes = [n for n in crm.notes.values() if n.parent_ref == deal_ref.sor_ref]
    assert len(notes) == 1
    assert notes[0].title == "Lead hand-off from platform"
    assert "prop_1" in notes[0].body


def test_log_activity_writes_a_note_on_the_company() -> None:
    crm = InMemoryCrm()
    ref = crm.upsert_company(CompanyUpsert(domain="acme.example"))
    note = ActivityNote(
        kind="alert_delivered", title="x", body="An alert was sent.", occurred_at=dt.datetime.now(UTC)
    )
    note_id = crm.log_activity(ref, note)
    assert crm.notes[note_id].parent_kind == "company"
    assert crm.notes[note_id].parent_ref == ref.sor_ref


def test_upsert_subscription_resolves_company_by_platform_account_id_then_domain() -> None:
    crm = InMemoryCrm()
    crm.upsert_company(CompanyUpsert(domain="acme.example", platform_account_id="acc_1"))
    record_id = crm.upsert_subscription(
        SubscriptionMirror(
            stripe_subscription_id="sub_1",
            stripe_customer_id="cus_1",
            platform_account_id="acc_1",
            plan="pro",
            seats=1,
            status="active",
            current_period_end=None,
            mrr=150.0,
        )
    )
    row = crm.subscriptions["sub_1"]
    assert row.record_id == record_id
    assert row.company_ref is not None


def test_upsert_subscription_is_idempotent_on_stripe_subscription_id() -> None:
    crm = InMemoryCrm()
    sub = SubscriptionMirror(
        stripe_subscription_id="sub_2",
        stripe_customer_id="cus_2",
        platform_account_id="acc_2",
        plan="pro",
        seats=1,
        status="trialing",
        current_period_end=None,
        mrr=None,
    )
    id1 = crm.upsert_subscription(sub)
    id2 = crm.upsert_subscription(sub)
    assert id1 == id2
    assert len(crm.subscriptions) == 1


def test_request_personal_data_deletion_opens_a_task_and_never_deletes() -> None:
    crm = InMemoryCrm()
    task_id = crm.request_personal_data_deletion(email="person@example.com", reason="GDPR request")
    task = crm.tasks[task_id]
    assert task.email == "person@example.com"
    assert task.reason == "GDPR request"


def test_port_surface_has_no_delete_method() -> None:
    members = [name for name, _ in inspect.getmembers(InMemoryCrm, predicate=inspect.isfunction)]
    assert not any("delete" in name.lower() for name in members)


_SECRET = "test-webhook-secret"  # noqa: S105 - low-entropy fake per CLAUDE.md, not a real secret


def test_parse_webhook_accepts_valid_signature() -> None:
    crm = InMemoryCrm(webhook_secret=_SECRET)
    payload = {"event_type": "record.updated", "id": {"object_id": "companies", "record_id": "cmp_1"}}
    body = json.dumps(payload).encode()
    sig = _sign(_SECRET, body)
    changes = crm.parse_webhook(body=body, headers={"Attio-Signature": sig})
    assert len(changes) == 1
    assert changes[0].kind == "company.updated"
    assert changes[0].sor_ref == "cmp_1"


def test_parse_webhook_accepts_events_array_shape() -> None:
    crm = InMemoryCrm()
    payload = {
        "events": [
            {"event_type": "record.updated", "id": {"object_id": "companies", "record_id": "cmp_1"}},
            {"event_type": "record.created", "id": {"object_id": "lead_signals", "record_id": "sig_1"}},
            {"event_type": "record.deleted", "id": {"object_id": "companies", "record_id": "cmp_2"}},
        ]
    }
    body = json.dumps(payload).encode()
    sig = _sign("test-webhook-secret", body)
    changes = crm.parse_webhook(body=body, headers={"Attio-Signature": sig})
    kinds = {c.kind for c in changes}
    assert kinds == {"company.updated", "lead_signal.updated"}
    assert len(changes) == 2  # the record.deleted event is not mapped and is skipped


def test_parse_webhook_rejects_bad_signature() -> None:
    crm = InMemoryCrm()
    body = json.dumps(
        {"event_type": "record.updated", "id": {"object_id": "companies", "record_id": "c1"}}
    ).encode()
    try:
        crm.parse_webhook(body=body, headers={"Attio-Signature": "0" * 64})
    except WebhookRejected:
        pass
    else:
        raise AssertionError("expected WebhookRejected")


def test_parse_webhook_rejects_missing_signature() -> None:
    crm = InMemoryCrm()
    body = b"{}"
    try:
        crm.parse_webhook(body=body, headers={})
    except WebhookRejected:
        pass
    else:
        raise AssertionError("expected WebhookRejected")
