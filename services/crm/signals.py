"""Pure mapping from a platform `Event` to a CRM `LeadSignal` (docs/34-crm-system-of-record.md
§2.4; docs/33-gtm-and-sales-playbook.md §2.3 "Timing: strongest event").

`signal_from_event` is a table lookup plus one arithmetic step — no I/O, no port call — so it is
cheap to call from anywhere (a pipeline step, an admin action, a later scheduled worker) and cheap
to test exhaustively. The worker that would call this for every qualifying pipeline event and feed
the result into `CrmPort.create_lead_signal` is a later wave (services/crm/README.md "deferred");
this module ships only the function.

Only the "Timing: strongest event" component of the full lead-scoring rubric is computed here
(docs/33 §2.3's `fit` and `engagement` components need account-level data this function does not
have — CRM `segment`/`tools_in_use`/`owner_relationship` — so they are out of scope for a per-event
mapping and are the scheduled worker's job, not this function's). The score is therefore this
event's base "timing" points times the recency multiplier, as an int — a component of the full
score, not the full score itself.
"""

from __future__ import annotations

import datetime as dt

from services.db.models import Event
from services.ids import public_id
from services.sor.ports import LeadSignal

# --------------------------------------------------------------------------------------- mapping
#: (event.subject_type, event.event_type) -> (docs/34 §2.4 event_type, docs/33 §2.3 base points).
#: Only platform event types with a clear correspondence to a docs/34 signal type are mapped;
#: everything else (identity events, field-level events, publication events, organisation events —
#: `org` is not a valid `subject_kind` for a Lead Signal, docs/34 §2.4) returns `None`.
#:
#: Decisions recorded in services/crm/README.md:
#: - D-3: platform `cancelled` on a proposal is treated the same as `withdrawn` (docs/34 §2.4 has
#:   no separate `proposal.cancelled` signal type) — same base points as `withdrawn`.
#: - D-4: platform `closed` and `due_date_changed` on an opportunity both map to
#:   `opportunity.rfp_closing` (docs/33 §2.3's table has no explicit points for "closing"; this
#:   reuses the `opportunity.cancelled/reinstated` row's 18 points as the closest urgency signal).
#: - D-5: `load.request_filed` and `org.first_seen` from docs/33 §2.3's table have no corresponding
#:   platform `Event` today (no `load` subject, and `organization` is not a valid Lead Signal
#:   `subject_kind`) — omitted, not approximated.
_MAPPING: dict[tuple[str, str], tuple[str, int]] = {
    ("proposal", "created"): ("proposal.new", 8),
    ("proposal", "status_change"): ("proposal.status_changed", 18),
    ("proposal", "withdrawn"): ("proposal.withdrawn", 12),
    ("proposal", "cancelled"): ("proposal.withdrawn", 12),  # D-3
    ("opportunity", "announced"): ("opportunity.rfp_opened", 22),
    ("opportunity", "opened"): ("opportunity.rfp_opened", 22),
    ("opportunity", "closed"): ("opportunity.rfp_closing", 18),  # D-4
    ("opportunity", "due_date_changed"): ("opportunity.rfp_closing", 18),  # D-4
    ("opportunity", "awarded"): ("opportunity.awarded", 15),
    ("opportunity", "cancelled"): ("funding.cancelled", 18),
    ("opportunity", "reinstated"): ("funding.reinstated", 18),
    ("match", "match_added"): ("match.new", 25),
}

_LABELS: dict[str, str] = {
    "proposal.new": "New proposal activity",
    "proposal.status_changed": "Proposal status change",
    "proposal.withdrawn": "Proposal withdrawn",
    "opportunity.rfp_opened": "RFP opened",
    "opportunity.rfp_closing": "RFP closing soon",
    "opportunity.awarded": "Opportunity awarded",
    "funding.cancelled": "Funding cancelled",
    "funding.reinstated": "Funding reinstated",
    "match.new": "New match",
}


def _recency_multiplier(age_days: float) -> float:
    """docs/33 §2.3 "Timing: recency decay": ×1.0 ≤7 days; ×0.7 8-30 days; ×0.3 31-90 days; ×0
    after."""
    if age_days <= 7:
        return 1.0
    if age_days <= 30:
        return 0.7
    if age_days <= 90:
        return 0.3
    return 0.0


def signal_from_event(
    event: Event,
    *,
    subject_name: str,
    subject_url: str,
    company_domain: str | None = None,
    platform_org_id: str | None = None,
    jurisdiction: str | None = None,
    technology: str | None = None,
    capacity_mw: float | None = None,
    now: dt.datetime | None = None,
) -> LeadSignal | None:
    """`None` when `event.event_type` (given `event.subject_type`) has no docs/34 §2.4
    correspondence — the caller (a future worker) should simply skip the event, not treat it as an
    error."""
    mapped = _MAPPING.get((event.subject_type, event.event_type))
    if mapped is None:
        return None
    attio_event_type, base_points = mapped

    reference_now = now if now is not None else dt.datetime.now(dt.UTC)
    observed_at = event.observed_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=dt.UTC)
    age_days = max(0.0, (reference_now - observed_at).total_seconds() / 86400.0)
    multiplier = _recency_multiplier(age_days)
    score = round(base_points * multiplier)

    label = _LABELS[attio_event_type]
    rationale = (
        f"{label} on {subject_name}, observed {observed_at.date().isoformat()} "
        f"({int(age_days)} day{'s' if int(age_days) != 1 else ''} ago)."
    )

    return LeadSignal(
        signal_id=public_id("evt", event.id),
        event_type=attio_event_type,
        subject_kind=event.subject_type,
        subject_name=subject_name,
        subject_url=subject_url,
        observed_at=observed_at,
        score=score,
        rationale=rationale,
        company_domain=company_domain,
        platform_org_id=platform_org_id,
        jurisdiction=jurisdiction,
        technology=technology,
        capacity_mw=capacity_mw,
    )


__all__ = ["signal_from_event"]
