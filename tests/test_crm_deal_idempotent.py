"""A retried lead hand-off creates one Attio deal, not two (backend audit 2026-09-30 F12).

Before, `create_deal` POSTed the deal and then a note, and `_request` retried any POST after a
transport error or a 5xx. A lost response, or a failing note, led to a second deal for the same
match. Now a create is never retried automatically, the deal linked to the match's lead signal is
reused on a retry, and a failed note no longer fails a hand-off whose deal exists.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from services.crm.attio import AttioCrmAdapter
from services.crm.fake import InMemoryCrm
from services.sor.ports import DealCreate, LeadSignal, SorUnavailable

SIGNAL_RECORD = "sig-rec-1"


class FakeAttio:
    """Just enough of Attio's records API, with server-side state, to see duplicates."""

    def __init__(self) -> None:
        self.deals: list[dict[str, Any]] = []
        self.notes = 0
        self.lose_next_deal_response = False
        self.fail_notes = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path == "/v2/objects/lead_signals/records/query":
            return httpx.Response(200, json={"data": [{"id": {"record_id": SIGNAL_RECORD}}]})
        if path == "/v2/objects/deals/records/query":
            wanted = body["filter"]["originating_signal"]["target_record_id"]
            rows = [d for d in self.deals if d["signal"] == wanted]
            return httpx.Response(200, json={"data": [{"id": {"record_id": d["id"]}} for d in rows]})
        if path == "/v2/objects/deals/records" and request.method == "POST":
            ref = body["data"]["values"].get("originating_signal", [{}])[0].get("target_record_id")
            deal = {"id": f"deal_{len(self.deals) + 1}", "signal": ref}
            self.deals.append(deal)
            if self.lose_next_deal_response:
                self.lose_next_deal_response = False
                raise httpx.ReadTimeout("response lost after the deal was created")
            return httpx.Response(200, json={"data": {"id": {"record_id": deal["id"]}}})
        if path == "/v2/notes":
            if self.fail_notes:
                return httpx.Response(503, json={"error": "unavailable"})
            self.notes += 1
            return httpx.Response(200, json={"data": {"id": {"note_id": f"note_{self.notes}"}}})
        if path.startswith("/v2/objects/companies/records/query"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": {}})


DEAL = DealCreate(
    name="Acme × RFP",
    proposal_public_id="prop_1",
    opportunity_public_id="opp_1",
    score=0.8,
    rationale="r",
    link_url="https://example.invalid/p",
    originating_signal_id="mat_1",
)


@pytest.fixture()
def attio() -> tuple[FakeAttio, AttioCrmAdapter]:
    fake = FakeAttio()
    adapter = AttioCrmAdapter(
        "test-key-not-a-secret", transport=httpx.MockTransport(fake.handler), sleep=lambda _s: None
    )
    return fake, adapter


def test_a_lost_create_response_is_not_retried_and_the_retry_reuses_the_deal(
    attio: tuple[FakeAttio, AttioCrmAdapter],
) -> None:
    fake, adapter = attio
    fake.lose_next_deal_response = True
    with pytest.raises(SorUnavailable):
        adapter.create_deal(DEAL)
    assert len(fake.deals) == 1  # not posted twice by an automatic retry

    ref = adapter.create_deal(DEAL)  # the operator's retry
    assert ref.sor_ref == "deal_1"
    assert len(fake.deals) == 1


def test_a_failed_note_does_not_fail_a_hand_off_whose_deal_exists(
    attio: tuple[FakeAttio, AttioCrmAdapter],
) -> None:
    fake, adapter = attio
    fake.fail_notes = True
    ref = adapter.create_deal(DEAL)
    assert ref.sor_ref == "deal_1"
    assert len(fake.deals) == 1 and fake.notes == 0


def test_the_in_memory_fake_reuses_the_deal_for_a_signal_too() -> None:
    import datetime as dt

    crm = InMemoryCrm()
    crm.create_lead_signal(
        LeadSignal(
            signal_id="mat_1",
            event_type="match.new",
            subject_kind="match",
            subject_name="x",
            subject_url="https://example.invalid/p",
            observed_at=dt.datetime.now(dt.UTC),
            score=80,
            rationale="r",
        )
    )
    first = crm.create_deal(DEAL)
    second = crm.create_deal(DEAL)
    assert first == second and len(crm.deals) == 1
