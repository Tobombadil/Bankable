"""Every branch of `signal_from_event` (docs/34 §2.4; docs/33 §2.3)."""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

from services.crm.signals import signal_from_event
from services.db.models import Event
from services.ids import public_id

UTC = dt.UTC
NOW = dt.datetime(2026, 9, 13, tzinfo=UTC)


def _event(subject_type: str, event_type: str, *, observed_at: dt.datetime) -> Event:
    return Event(
        id=_uuid.uuid4(),
        subject_type=subject_type,
        subject_id=_uuid.uuid4(),
        event_type=event_type,
        observed_at=observed_at,
        idempotency_key=f"test:{subject_type}:{event_type}:{observed_at.isoformat()}",
    )


def test_proposal_created_maps_to_proposal_new() -> None:
    ev = _event("proposal", "created", observed_at=NOW - dt.timedelta(days=1))
    signal = signal_from_event(ev, subject_name="Widget Storage", subject_url="https://x/p", now=NOW)
    assert signal is not None
    assert signal.event_type == "proposal.new"
    assert signal.subject_kind == "proposal"
    assert signal.score == 8  # base 8 * 1.0 recency
    assert signal.signal_id == public_id("evt", ev.id)


def test_proposal_status_change_maps_and_scores() -> None:
    ev = _event("proposal", "status_change", observed_at=NOW - dt.timedelta(days=10))
    signal = signal_from_event(ev, subject_name="X", subject_url="https://x", now=NOW)
    assert signal is not None
    assert signal.event_type == "proposal.status_changed"
    assert signal.score == round(18 * 0.7)


def test_proposal_withdrawn_maps() -> None:
    ev = _event("proposal", "withdrawn", observed_at=NOW - dt.timedelta(days=1))
    signal = signal_from_event(ev, subject_name="X", subject_url="https://x", now=NOW)
    assert signal is not None
    assert signal.event_type == "proposal.withdrawn"
    assert signal.score == 12


def test_proposal_cancelled_maps_like_withdrawn() -> None:
    ev = _event("proposal", "cancelled", observed_at=NOW - dt.timedelta(days=1))
    signal = signal_from_event(ev, subject_name="X", subject_url="https://x", now=NOW)
    assert signal is not None
    assert signal.event_type == "proposal.withdrawn"
    assert signal.score == 12


def test_opportunity_announced_and_opened_map_to_rfp_opened() -> None:
    for event_type in ("announced", "opened"):
        ev = _event("opportunity", event_type, observed_at=NOW - dt.timedelta(days=1))
        signal = signal_from_event(ev, subject_name="X", subject_url="https://x", now=NOW)
        assert signal is not None
        assert signal.event_type == "opportunity.rfp_opened"
        assert signal.score == 22


def test_opportunity_closed_and_due_date_changed_map_to_rfp_closing() -> None:
    for event_type in ("closed", "due_date_changed"):
        ev = _event("opportunity", event_type, observed_at=NOW - dt.timedelta(days=1))
        signal = signal_from_event(ev, subject_name="X", subject_url="https://x", now=NOW)
        assert signal is not None
        assert signal.event_type == "opportunity.rfp_closing"
        assert signal.score == 18


def test_opportunity_awarded_maps() -> None:
    ev = _event("opportunity", "awarded", observed_at=NOW - dt.timedelta(days=1))
    signal = signal_from_event(ev, subject_name="X", subject_url="https://x", now=NOW)
    assert signal is not None
    assert signal.event_type == "opportunity.awarded"
    assert signal.score == 15


def test_opportunity_cancelled_maps_to_funding_cancelled() -> None:
    ev = _event("opportunity", "cancelled", observed_at=NOW - dt.timedelta(days=1))
    signal = signal_from_event(ev, subject_name="X", subject_url="https://x", now=NOW)
    assert signal is not None
    assert signal.event_type == "funding.cancelled"
    assert signal.score == 18


def test_opportunity_reinstated_maps_to_funding_reinstated() -> None:
    ev = _event("opportunity", "reinstated", observed_at=NOW - dt.timedelta(days=1))
    signal = signal_from_event(ev, subject_name="X", subject_url="https://x", now=NOW)
    assert signal is not None
    assert signal.event_type == "funding.reinstated"
    assert signal.score == 18


def test_match_added_maps_to_match_new() -> None:
    ev = _event("match", "match_added", observed_at=NOW - dt.timedelta(days=1))
    signal = signal_from_event(ev, subject_name="X", subject_url="https://x", now=NOW)
    assert signal is not None
    assert signal.event_type == "match.new"
    assert signal.subject_kind == "match"
    assert signal.score == 25


def test_unmapped_event_returns_none() -> None:
    ev = _event("organization", "created", observed_at=NOW)
    assert signal_from_event(ev, subject_name="X", subject_url="https://x", now=NOW) is None
    ev2 = _event("proposal", "field_changed", observed_at=NOW)
    assert signal_from_event(ev2, subject_name="X", subject_url="https://x", now=NOW) is None


def test_recency_multiplier_boundaries() -> None:
    cases = [
        (0, 1.0),
        (7, 1.0),
        (8, 0.7),
        (30, 0.7),
        (31, 0.3),
        (90, 0.3),
        (91, 0.0),
        (365, 0.0),
    ]
    for days, multiplier in cases:
        ev = _event("match", "match_added", observed_at=NOW - dt.timedelta(days=days))
        signal = signal_from_event(ev, subject_name="X", subject_url="https://x", now=NOW)
        assert signal is not None
        assert signal.score == round(25 * multiplier), f"days={days}"


def test_rationale_is_one_sentence_and_mentions_subject() -> None:
    ev = _event("match", "match_added", observed_at=NOW - dt.timedelta(days=2))
    signal = signal_from_event(
        ev, subject_name="Widget Storage ↔ Regional RFP", subject_url="https://x", now=NOW
    )
    assert signal is not None
    assert signal.rationale.count(".") == 1  # exactly one terminal sentence
    assert "Widget Storage" in signal.rationale


def test_passthrough_fields_carried_onto_the_signal() -> None:
    ev = _event("proposal", "created", observed_at=NOW)
    signal = signal_from_event(
        ev,
        subject_name="X",
        subject_url="https://x",
        company_domain="acme.example",
        platform_org_id="org_ABC",
        jurisdiction="US-TX",
        technology="bess_li_ion",
        capacity_mw=100.0,
        now=NOW,
    )
    assert signal is not None
    assert signal.company_domain == "acme.example"
    assert signal.platform_org_id == "org_ABC"
    assert signal.jurisdiction == "US-TX"
    assert signal.technology == "bess_li_ion"
    assert signal.capacity_mw == 100.0


def test_defaults_now_to_current_time_when_not_given() -> None:
    ev = _event("match", "match_added", observed_at=dt.datetime.now(UTC))
    signal = signal_from_event(ev, subject_name="X", subject_url="https://x")
    assert signal is not None
    assert signal.score == 25
