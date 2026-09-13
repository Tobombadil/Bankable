"""AttioCrmAdapter against `httpx.MockTransport` — every request's exact method/path/query/JSON
body, retry behaviour, error mapping, and webhook verification. No live network call anywhere
here (CLAUDE.md tooling rule); every response is built by hand from the shapes verified against
the vendor's OpenAPI document (see `services/crm/README.md` "endpoints used")."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid as _uuid
from collections.abc import Callable

import httpx
import pytest

from services.crm.attio import AttioCrmAdapter
from services.sor.ports import (
    ActivityNote,
    CompanyRef,
    CompanyUpsert,
    DealCreate,
    LeadSignal,
    SorRejected,
    SorUnavailable,
    SubscriptionMirror,
    WebhookRejected,
)

UTC = dt.UTC


class Recorder:
    """`httpx.BaseTransport` that records every request and answers from a handler function, so
    each test asserts the exact request the adapter sent before deciding what to hand back."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _record_id() -> str:
    return str(_uuid.uuid4())


def _record_response(
    record_id: str, values: dict[str, object] | None = None, status: int = 200
) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "data": {
                "id": {
                    "workspace_id": str(_uuid.uuid4()),
                    "object_id": str(_uuid.uuid4()),
                    "record_id": record_id,
                },
                "created_at": "2026-09-13T00:00:00.000000000Z",
                "web_url": "https://app.attio.com/w/company/" + record_id,
                "values": values or {},
            }
        },
    )


def _entry(attribute_type: str, **fields: object) -> dict[str, object]:
    return {
        "active_from": "2026-01-01T00:00:00.000000000Z",
        "active_until": None,
        "attribute_type": attribute_type,
        **fields,
    }


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: object
) -> tuple[AttioCrmAdapter, Recorder]:
    recorder = Recorder(handler)
    kwargs.setdefault("sleep", lambda _s: None)
    adapter = AttioCrmAdapter("test-api-key", transport=recorder, **kwargs)  # type: ignore[arg-type]
    return adapter, recorder


# ------------------------------------------------------------------------------- upsert_company
def test_upsert_company_sends_exact_request_and_never_human_owned_fields() -> None:
    record_id = _record_id()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/v2/objects/companies/records"
        assert request.url.params["matching_attribute"] == "domains"
        body = json.loads(request.content)
        values = body["data"]["values"]
        assert values["domains"] == [{"domain": "acme.example"}]
        assert values["platform_org_id"] == ["org_1"]
        assert values["lead_score"] == [42]
        assert values["lead_band"] == ["hot"]
        assert "name" not in values  # human-owned; never written (README D-2)
        assert "segment" not in values
        assert "do_not_contact" not in values
        return _record_response(record_id)

    adapter, recorder = _adapter(handler)
    ref = adapter.upsert_company(
        CompanyUpsert(
            domain="acme.example",
            name="Should never appear",
            platform_org_id="org_1",
            lead_score=42,
            lead_band="hot",
        )
    )
    assert ref == CompanyRef(sor_kind="attio", sor_ref=record_id)
    assert len(recorder.requests) == 1


def test_upsert_company_only_sends_fields_that_are_set() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert set(body["data"]["values"].keys()) == {"domains"}
        return _record_response(_record_id())

    adapter, _ = _adapter(handler)
    adapter.upsert_company(CompanyUpsert(domain="acme.example"))


def test_upsert_company_sends_top_event_at_as_iso_timestamp() -> None:
    when = dt.datetime(2026, 9, 1, 12, 30, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["data"]["values"]["top_event_at"] == ["2026-09-01T12:30:00.000000Z"]
        return _record_response(_record_id())

    adapter, _ = _adapter(handler)
    adapter.upsert_company(CompanyUpsert(domain="acme.example", top_event_at=when))


# ----------------------------------------------------------------------------------- get_company
def test_get_company_parses_values_and_prefers_active_entries() -> None:
    record_id = _record_id()
    values = {
        "domains": [_entry("domain", domain="acme.example", root_domain="acme.example")],
        "name": [_entry("text", value="Acme Power")],
        "do_not_contact": [
            {**_entry("checkbox", value=True), "active_until": "2026-01-02T00:00:00Z"},
            _entry("checkbox", value=False),
        ],
        "consent_basis": [_entry("select", option={"title": "consent"})],
        "stage": [_entry("status", status={"title": "2 Contacted"})],
        "platform_org_id": [_entry("text", value="org_1")],
        "platform_account_id": [_entry("text", value="acc_1")],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/v2/objects/companies/records/{record_id}"
        return _record_response(record_id, values)

    adapter, _ = _adapter(handler)
    company = adapter.get_company(CompanyRef(sor_kind="attio", sor_ref=record_id))
    assert company is not None
    assert company.domain == "acme.example"
    assert company.name == "Acme Power"
    assert company.do_not_contact is False  # the active (non-superseded) entry wins
    assert company.consent_basis == "consent"
    assert company.stage == "2 Contacted"
    assert company.platform_org_id == "org_1"
    assert company.platform_account_id == "acc_1"


def test_get_company_404_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status_code": 404, "type": "not_found", "code": "not_found"})

    adapter, _ = _adapter(handler)
    assert adapter.get_company(CompanyRef(sor_kind="attio", sor_ref="missing")) is None


# ---------------------------------------------------------------------------------- find_company
def test_find_company_queries_by_domain() -> None:
    record_id = _record_id()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v2/objects/companies/records/query"
        body = json.loads(request.content)
        assert body["filter"] == {"domains": {"domain": {"$eq": "acme.example"}}}
        assert body["limit"] == 1
        return httpx.Response(200, json={"data": [_record_response(record_id).json()["data"]]})

    adapter, _ = _adapter(handler)
    company = adapter.find_company(domain="acme.example")
    assert company is not None
    assert company.ref.sor_ref == record_id


def test_find_company_queries_by_platform_org_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["filter"] == {"platform_org_id": {"$eq": "org_1"}}
        return httpx.Response(200, json={"data": []})

    adapter, _ = _adapter(handler)
    assert adapter.find_company(platform_org_id="org_1") is None


def test_contact_policy_unknown_company() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    adapter, _ = _adapter(handler)
    policy = adapter.contact_policy(domain="unknown.example")
    assert policy.do_not_contact is False
    assert policy.consent_basis is None
    assert policy.allowed is False


def test_contact_policy_found_company_reads_do_not_contact_and_consent() -> None:
    company_id = _record_id()
    values = {
        "do_not_contact": [_entry("checkbox", value=True)],
        "consent_basis": [_entry("select", option={"title": "customer"})],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [_record_response(company_id, values).json()["data"]]})

    adapter, _ = _adapter(handler)
    policy = adapter.contact_policy(domain="acme.example")
    assert policy.do_not_contact is True
    assert policy.consent_basis == "customer"
    assert policy.allowed is False  # do_not_contact wins regardless of consent


# ------------------------------------------------------------------------------ create_lead_signal
def test_create_lead_signal_matched_company() -> None:
    company_id = _record_id()
    signal_id_record = _record_id()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v2/objects/companies/records/query":
            return httpx.Response(200, json={"data": [_record_response(company_id).json()["data"]]})
        if request.url.path == "/v2/objects/lead_signals/records":
            assert request.method == "PUT"
            assert request.url.params["matching_attribute"] == "signal_id"
            body = json.loads(request.content)
            values = body["data"]["values"]
            assert values["signal_id"] == ["evt_1"]
            assert values["company"] == [{"target_object": "companies", "target_record_id": company_id}]
            return _record_response(signal_id_record)
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    adapter, _ = _adapter(handler)
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
    ref = adapter.create_lead_signal(signal)
    assert ref.unmatched is False
    assert ref.company == CompanyRef(sor_kind="attio", sor_ref=company_id)
    assert "PUT /v2/lists/unmatched_signals/entries" not in calls


def test_create_lead_signal_includes_optional_context_fields() -> None:
    company_id = _record_id()
    signal_id_record = _record_id()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/objects/companies/records/query":
            return httpx.Response(200, json={"data": [_record_response(company_id).json()["data"]]})
        body = json.loads(request.content)
        values = body["data"]["values"]
        assert values["jurisdiction"] == ["US-TX"]
        assert values["technology"] == ["bess_li_ion"]
        assert values["capacity_mw"] == [100.0]
        return _record_response(signal_id_record)

    adapter, _ = _adapter(handler)
    signal = LeadSignal(
        signal_id="evt_1b",
        event_type="proposal.new",
        subject_kind="proposal",
        subject_name="X",
        subject_url="https://x",
        observed_at=dt.datetime.now(UTC),
        score=8,
        rationale="Because.",
        company_domain="acme.example",
        jurisdiction="US-TX",
        technology="bess_li_ion",
        capacity_mw=100.0,
    )
    adapter.create_lead_signal(signal)


def test_create_lead_signal_unmatched_lands_on_unmatched_list() -> None:
    signal_record_id = _record_id()
    list_calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/objects/companies/records/query":
            return httpx.Response(200, json={"data": []})
        if request.url.path == "/v2/objects/lead_signals/records":
            return _record_response(signal_record_id)
        if request.url.path == "/v2/lists/unmatched_signals/entries":
            body = json.loads(request.content)
            list_calls.append(body)
            assert body["data"]["parent_object"] == "lead_signals"
            assert body["data"]["parent_record_id"] == signal_record_id
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": {
                            "workspace_id": str(_uuid.uuid4()),
                            "list_id": str(_uuid.uuid4()),
                            "entry_id": str(_uuid.uuid4()),
                        }
                    }
                },
            )
        raise AssertionError(f"unexpected request {request.url.path}")

    adapter, _ = _adapter(handler)
    signal = LeadSignal(
        signal_id="evt_2",
        event_type="proposal.new",
        subject_kind="proposal",
        subject_name="X",
        subject_url="https://x",
        observed_at=dt.datetime.now(UTC),
        score=8,
        rationale="Because.",
    )
    ref = adapter.create_lead_signal(signal)
    assert ref.unmatched is True
    assert ref.company is None
    assert len(list_calls) == 1


# ------------------------------------------------------------------------------------ create_deal
def test_create_deal_links_company_signal_and_writes_note() -> None:
    company_id = _record_id()
    signal_id = _record_id()
    deal_id = _record_id()
    note_calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/objects/companies/records/query":
            return httpx.Response(200, json={"data": [_record_response(company_id).json()["data"]]})
        if path == "/v2/objects/lead_signals/records/query":
            body = json.loads(request.content)
            assert body["filter"] == {"signal_id": {"$eq": "evt_9"}}
            return httpx.Response(200, json={"data": [_record_response(signal_id).json()["data"]]})
        if path == "/v2/objects/deals/records":
            assert request.method == "POST"
            body = json.loads(request.content)
            values = body["data"]["values"]
            assert values["name"] == ["Acme x Regional RFP"]
            assert values["associated_company"] == [
                {"target_object": "companies", "target_record_id": company_id}
            ]
            assert values["originating_signal"] == [
                {"target_object": "lead_signals", "target_record_id": signal_id}
            ]
            assert values["tier"] == ["pro"]
            return _record_response(deal_id)
        if path == "/v2/notes":
            body = json.loads(request.content)
            note_calls.append(body)
            assert body["data"]["parent_object"] == "deals"
            assert body["data"]["parent_record_id"] == deal_id
            assert body["data"]["title"] == "Lead hand-off from platform"
            assert "prop_1" in body["data"]["content"]
            return httpx.Response(
                200,
                json={"data": {"id": {"workspace_id": str(_uuid.uuid4()), "note_id": str(_uuid.uuid4())}}},
            )
        raise AssertionError(f"unexpected request {path}")

    adapter, _ = _adapter(handler)
    deal_ref = adapter.create_deal(
        DealCreate(
            name="Acme x Regional RFP",
            proposal_public_id="prop_1",
            opportunity_public_id="opp_1",
            score=0.85,
            rationale="Because.",
            link_url="https://platform/proposals/acme",
            company_domain="acme.example",
            originating_signal_id="evt_9",
            tier="pro",
        )
    )
    assert deal_ref.sor_ref == deal_id
    assert len(note_calls) == 1


# ---------------------------------------------------------------------------------- log_activity
def test_log_activity_posts_a_plaintext_note_on_the_company() -> None:
    note_id = str(_uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v2/notes"
        body = json.loads(request.content)
        assert body["data"]["parent_object"] == "companies"
        assert body["data"]["parent_record_id"] == "cmp_123"
        assert body["data"]["format"] == "plaintext"
        assert body["data"]["title"] == "alert_delivered"
        return httpx.Response(
            200, json={"data": {"id": {"workspace_id": str(_uuid.uuid4()), "note_id": note_id}}}
        )

    adapter, _ = _adapter(handler)
    returned = adapter.log_activity(
        CompanyRef(sor_kind="attio", sor_ref="cmp_123"),
        ActivityNote(
            kind="alert_delivered",
            title="x",
            body="An alert was sent.",
            occurred_at=dt.datetime.now(UTC),
        ),
    )
    assert returned == note_id


# ------------------------------------------------------------------------------ upsert_subscription
def test_upsert_subscription_resolves_company_by_platform_account_id() -> None:
    company_id = _record_id()
    sub_id = _record_id()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/objects/companies/records/query":
            body = json.loads(request.content)
            assert body["filter"] == {"platform_account_id": {"$eq": "acc_1"}}
            return httpx.Response(200, json={"data": [_record_response(company_id).json()["data"]]})
        if request.url.path == "/v2/objects/subscriptions/records":
            assert request.url.params["matching_attribute"] == "stripe_subscription_id"
            body = json.loads(request.content)
            values = body["data"]["values"]
            assert values["stripe_subscription_id"] == ["sub_1"]
            assert values["company"] == [{"target_object": "companies", "target_record_id": company_id}]
            return _record_response(sub_id)
        raise AssertionError(request.url.path)

    adapter, _ = _adapter(handler)
    record_id = adapter.upsert_subscription(
        SubscriptionMirror(
            stripe_subscription_id="sub_1",
            stripe_customer_id="cus_1",
            platform_account_id="acc_1",
            plan="pro",
            seats=2,
            status="active",
            current_period_end=None,
            mrr=300.0,
        )
    )
    assert record_id == sub_id


def test_upsert_subscription_falls_back_to_domain_and_writes_period_end() -> None:
    company_id = _record_id()
    sub_id = _record_id()
    end = dt.datetime(2026, 10, 1, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/objects/companies/records/query":
            body = json.loads(request.content)
            assert body["filter"] == {"domains": {"domain": {"$eq": "acme.example"}}}
            return httpx.Response(200, json={"data": [_record_response(company_id).json()["data"]]})
        if request.url.path == "/v2/objects/subscriptions/records":
            body = json.loads(request.content)
            values = body["data"]["values"]
            assert values["current_period_end"] == ["2026-10-01T00:00:00.000000Z"]
            return _record_response(sub_id)
        raise AssertionError(request.url.path)

    adapter, _ = _adapter(handler)
    record_id = adapter.upsert_subscription(
        SubscriptionMirror(
            stripe_subscription_id="sub_2",
            stripe_customer_id="cus_2",
            platform_account_id="",
            plan="pro",
            seats=1,
            status="trialing",
            current_period_end=end,
            mrr=None,
            company_domain="acme.example",
        )
    )
    assert record_id == sub_id


# ------------------------------------------------------------------ request_personal_data_deletion
def test_request_personal_data_deletion_opens_a_task_with_a_30_day_deadline() -> None:
    now = dt.datetime(2026, 9, 13, tzinfo=UTC)
    task_id = str(_uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v2/tasks"
        body = json.loads(request.content)
        data = body["data"]
        assert "person@example.com" in data["content"]
        assert "GDPR" in data["content"]
        assert data["is_completed"] is False
        assert data["linked_records"] == ["person@example.com"]
        assert data["deadline_at"] == "2026-10-13T00:00:00.000000Z"
        return httpx.Response(
            200, json={"data": {"id": {"workspace_id": str(_uuid.uuid4()), "task_id": task_id}}}
        )

    adapter, _ = _adapter(handler, clock=lambda: now)
    returned = adapter.request_personal_data_deletion(email="person@example.com", reason="GDPR request")
    assert returned == task_id


# ------------------------------------------------------------------------------------ retry logic
def test_429_with_http_date_retry_after_is_retried_then_succeeds() -> None:
    now = dt.datetime(2026, 9, 13, 0, 0, 0, tzinfo=UTC)
    record_id = _record_id()
    attempts: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(
                429,
                json={"status_code": 429, "type": "rate_limit_error", "code": "rate_limit_exceeded"},
                headers={"Retry-After": "Sun, 13 Sep 2026 00:00:02 GMT"},
            )
        return _record_response(record_id)

    recorder = Recorder(handler)
    adapter = AttioCrmAdapter("k", transport=recorder, sleep=lambda s: sleeps.append(s), clock=lambda: now)
    ref = adapter.upsert_company(CompanyUpsert(domain="acme.example"))
    assert ref.sor_ref == record_id
    assert len(attempts) == 2
    assert sleeps == [2.0]


def test_429_exhausted_raises_sor_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"status_code": 429}, headers={"Retry-After": "1"})

    adapter, _ = _adapter(handler)
    with pytest.raises(SorUnavailable):
        adapter.upsert_company(CompanyUpsert(domain="acme.example"))


def test_429_retry_wait_is_capped_at_five_seconds() -> None:
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={}, headers={"Retry-After": "600"})

    adapter, _ = _adapter(handler, sleep=lambda s: sleeps.append(s))
    with pytest.raises(SorUnavailable):
        adapter.upsert_company(CompanyUpsert(domain="acme.example"))
    assert len(sleeps) == 3  # _MAX_429_RETRIES
    assert all(s <= 5.0 for s in sleeps)


def test_5xx_is_retried_once_then_raises_sor_unavailable() -> None:
    statuses: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        statuses.append(502)
        return httpx.Response(502, text="bad gateway")

    adapter, _ = _adapter(handler)
    with pytest.raises(SorUnavailable):
        adapter.upsert_company(CompanyUpsert(domain="acme.example"))
    assert len(statuses) == 2  # one retry, then give up


def test_5xx_retry_then_success() -> None:
    record_id = _record_id()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="unavailable")
        return _record_response(record_id)

    adapter, _ = _adapter(handler)
    ref = adapter.upsert_company(CompanyUpsert(domain="acme.example"))
    assert ref.sor_ref == record_id
    assert len(calls) == 2


def test_400_raises_sor_rejected_with_vendor_message_not_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"status_code": 400, "code": "value_invalid", "message": "bad value for lead_score"},
        )

    adapter, _ = _adapter(handler)
    with pytest.raises(SorRejected) as excinfo:
        adapter.upsert_company(CompanyUpsert(domain="acme.example"))
    assert "bad value for lead_score" in str(excinfo.value)


def test_transport_error_is_retried_once_then_raises_sor_unavailable() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("boom", request=request)

    adapter, _ = _adapter(handler)
    with pytest.raises(SorUnavailable):
        adapter.upsert_company(CompanyUpsert(domain="acme.example"))
    assert len(attempts) == 2


# ------------------------------------------------------------------------------------- webhooks
def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


_SECRET = "test-webhook-secret"  # noqa: S105 - low-entropy fake per CLAUDE.md, not a real secret


def test_parse_webhook_valid_signature_maps_company_updated() -> None:
    object_ids = {"companies": "obj-companies-uuid", "lead_signals": "obj-signals-uuid"}
    adapter, _ = _adapter(
        lambda r: httpx.Response(200, json={}), webhook_secret=_SECRET, object_ids=object_ids
    )
    payload = {
        "event_type": "record.updated",
        "id": {"object_id": "obj-companies-uuid", "record_id": "cmp_1"},
        "actor": {"type": "workspace-member"},
    }
    body = json.dumps(payload).encode()
    sig = _sign(_SECRET, body)
    changes = adapter.parse_webhook(body=body, headers={"Attio-Signature": sig})
    assert len(changes) == 1
    assert changes[0].kind == "company.updated"
    assert changes[0].sor_ref == "cmp_1"


def test_parse_webhook_events_array_and_lead_signal_created() -> None:
    object_ids = {"companies": "obj-c", "lead_signals": "obj-s"}
    adapter, _ = _adapter(
        lambda r: httpx.Response(200, json={}), webhook_secret=_SECRET, object_ids=object_ids
    )
    payload = {
        "events": [
            {"event_type": "record.created", "id": {"object_id": "obj-s", "record_id": "sig_1"}},
        ]
    }
    body = json.dumps(payload).encode()
    sig = _sign(_SECRET, body)
    changes = adapter.parse_webhook(body=body, headers={"Attio-Signature": sig})
    assert len(changes) == 1
    assert changes[0].kind == "lead_signal.updated"


def test_parse_webhook_unmapped_object_id_is_skipped() -> None:
    adapter, _ = _adapter(lambda r: httpx.Response(200, json={}), webhook_secret=_SECRET, object_ids={})
    payload = {"event_type": "record.updated", "id": {"object_id": "some-other-uuid", "record_id": "x_1"}}
    body = json.dumps(payload).encode()
    sig = _sign(_SECRET, body)
    assert adapter.parse_webhook(body=body, headers={"Attio-Signature": sig}) == []


def test_parse_webhook_accepts_x_attio_signature_fallback_header() -> None:
    adapter, _ = _adapter(
        lambda r: httpx.Response(200, json={}), webhook_secret=_SECRET, object_ids={"companies": "c"}
    )
    payload = {"event_type": "record.updated", "id": {"object_id": "c", "record_id": "cmp_1"}}
    body = json.dumps(payload).encode()
    sig = _sign(_SECRET, body)
    changes = adapter.parse_webhook(body=body, headers={"X-Attio-Signature": sig})
    assert len(changes) == 1


def test_parse_webhook_bad_signature_is_rejected() -> None:
    adapter, _ = _adapter(lambda r: httpx.Response(200, json={}), webhook_secret=_SECRET)
    body = b'{"event_type": "record.updated", "id": {"object_id": "c", "record_id": "x"}}'
    with pytest.raises(WebhookRejected):
        adapter.parse_webhook(body=body, headers={"Attio-Signature": "0" * 64})


def test_parse_webhook_missing_signature_is_rejected() -> None:
    adapter, _ = _adapter(lambda r: httpx.Response(200, json={}), webhook_secret=_SECRET)
    with pytest.raises(WebhookRejected):
        adapter.parse_webhook(body=b"{}", headers={})


def test_parse_webhook_with_no_secret_configured_is_always_rejected() -> None:
    adapter, _ = _adapter(lambda r: httpx.Response(200, json={}), webhook_secret=None)
    body = b'{"event_type": "record.updated", "id": {"object_id": "c", "record_id": "x"}}'
    sig = _sign("anything", body)
    with pytest.raises(WebhookRejected):
        adapter.parse_webhook(body=body, headers={"Attio-Signature": sig})


def test_parse_webhook_rejects_invalid_json_body() -> None:
    adapter, _ = _adapter(lambda r: httpx.Response(200, json={}), webhook_secret=_SECRET)
    body = b"not json"
    sig = _sign(_SECRET, body)
    with pytest.raises(WebhookRejected):
        adapter.parse_webhook(body=body, headers={"Attio-Signature": sig})


def test_parse_webhook_events_array_with_a_non_dict_entry_is_skipped() -> None:
    adapter, _ = _adapter(
        lambda r: httpx.Response(200, json={}), webhook_secret=_SECRET, object_ids={"companies": "c"}
    )
    payload = {
        "events": [
            "not-an-event-object",
            {"event_type": "record.updated", "id": {"object_id": "c", "record_id": "cmp_1"}},
        ]
    }
    body = json.dumps(payload).encode()
    sig = _sign(_SECRET, body)
    changes = adapter.parse_webhook(body=body, headers={"Attio-Signature": sig})
    assert len(changes) == 1


def test_build_crm_port_returns_in_memory_fake_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.crm import attio as attio_module
    from services.crm.fake import InMemoryCrm

    monkeypatch.delenv("ATTIO_API_KEY", raising=False)
    attio_module._dry_run_singleton = None
    port = attio_module.build_crm_port()
    assert isinstance(port, InMemoryCrm)
    # process-wide singleton
    assert attio_module.build_crm_port() is port


def test_build_crm_port_returns_live_adapter_when_api_key_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.crm import attio as attio_module

    monkeypatch.setenv("ATTIO_API_KEY", "test-key")
    monkeypatch.setenv("ATTIO_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.delenv("ATTIO_OBJECT_IDS", raising=False)
    port = attio_module.build_crm_port()
    assert isinstance(port, AttioCrmAdapter)
    port.close()
