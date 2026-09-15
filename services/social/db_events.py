"""The DB `event` -> `editorial.SocialEvent` bridge (Sprint 3 item 4, docs/32-social-operating-
playbook.md §3.1/§3.2).

`services.social.editorial` already owns which events earn a post, per-channel templates and
validation; this module owns exactly one thing: turning one `services.db.models.Event` row (and
the `proposal`/`opportunity` row it points at) into the structured `editorial.SocialEvent` those
functions consume, or deciding the event is not a candidate at all (returns `None`).

Two kinds of "not a candidate" are both a plain `None` return, not an error:
  - the event's `event_type` is not one docs/32 §3.1 turns into a post at all (default-deny, same
    stance as `editorial.POSTABLE_EVENT_TYPES` takes for anything it doesn't recognise);
  - the event *would* map to a postable type, but the record is missing a field that type's
    template treats as non-omittable (docs/32 §3.4, and services/social/README.md's own recorded
    assumption: "RFP/award/funding org and title fields are effectively required ... never
    invent a value"). See `_opportunity_social_event` for the two cases this hits today.

What this module deliberately does *not* do: the docs/32 §4.3 hard gates (subject visibility,
licence `allows_derived_publication`, `event.published_at` not null, record not `unpublished`).
Those need the caller's transaction and count toward `DraftTickReport.posts_skipped_gate`
(services/social/worker.py); this module only ever returns a `SocialEvent` or `None`.

A missing subject row (`event.subject_id` has no matching `proposal`/`opportunity`) is a real data
inconsistency, not a "no post" case -- `SubjectNotFoundError` propagates so the caller can log it
and count it in `DraftTickReport.errors` for that one event, per the worker's per-event error
handling (services/social/worker.py: "one bad event never blocks the rest").
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from services.api.common import WEB_HOST, ensure_aware
from services.db.models import Event, Opportunity, Proposal
from services.ingest.lag import LAG_DAYS_BY_KIND
from services.social.editorial import SocialEvent


class SubjectNotFoundError(LookupError):
    """`event.subject_id` has no matching `proposal`/`opportunity` row."""


#: docs/32 §3.1's mapping table, restricted to the docs/21 §7.3 "Proposal lifecycle" event types
#: the platform actually has a store for (`services/db/models.py`'s `Event.event_type`).
#: `created`/`status_change`/`withdrawn`/`cancelled` are exactly the four the task brief names;
#: everything else in §7.3's Proposal lifecycle group (`filed`, `studied`, `permitted`,
#: `contracted`, `built` as *event types* in their own right, rather than values folded into a
#: `status_change` event's `before`/`after`) is not in that list and is default-deny here, matching
#: the brief's "anything else -> None" instruction literally rather than extending it.
_PROPOSAL_EVENT_TYPE_MAP: dict[str, str] = {
    "created": "proposal.new",
    "status_change": "proposal.status_changed",
    "withdrawn": "proposal.withdrawn",
    "cancelled": "proposal.withdrawn",
}

#: docs/21 §7.3's "Opportunity lifecycle" group spells the terminal-deadline state `closed`, not
#: the task brief's word "closing"; both `closed` and `due_date_changed` read as a closing-reminder
#: event once `editorial.channels_for_event`'s own 14/3-day window applies to it. `closing` itself
#: is accepted too in case a future emitter uses that literal spelling -- accepting a synonym here
#: costs nothing and default-denies nothing the brief asked for.
_OPPORTUNITY_EVENT_TYPE_MAP: dict[str, str] = {
    "announced": "opportunity.rfp_opened",
    "opened": "opportunity.rfp_opened",
    "closed": "opportunity.rfp_closing",
    "closing": "opportunity.rfp_closing",
    "due_date_changed": "opportunity.rfp_closing",
    "awarded": "opportunity.awarded",
    "cancelled": "funding.cancelled",
    "reinstated": "funding.reinstated",
}


def _map_event_type(subject_type: str, event_type: str) -> str | None:
    if subject_type == "proposal":
        return _PROPOSAL_EVENT_TYPE_MAP.get(event_type)
    if subject_type == "opportunity":
        return _OPPORTUNITY_EVENT_TYPE_MAP.get(event_type)
    return None  # pragma: no cover -- guarded by social_event_from_db before this is called


def _social_event_id(event: Event) -> str:
    return str(event.id)


def _state_code(state_code: str | None, jurisdiction: str | None) -> str | None:
    """`location.state_code`/`proposal|opportunity.jurisdiction` are stored `US-TX`-style
    (`services/ingest/loader.py` `_jurisdiction`/`_get_or_create_location`); `editorial.fmt_state`
    expects the bare two-letter code, same convention `web/viewmodels.py` already uses."""
    raw = state_code or jurisdiction
    if not raw:
        return None
    return raw.rsplit("-", 1)[-1] or None


def _queue_id(identifiers: dict[str, Any] | None) -> str | None:
    """`proposal.identifiers["queue_ids"]` is `[{"iso": ..., "id": ...}, ...]`
    (`services/ingest/loader.py` `_proposal_fields_from_row`) -- take the first entry's id, the
    same one the record's own primary queue listing uses."""
    entries = (identifiers or {}).get("queue_ids")
    if not isinstance(entries, list) or not entries:
        return None
    first = entries[0]
    return str(first["id"]) if isinstance(first, dict) and first.get("id") else None


def _docket_id(identifiers: dict[str, Any] | None) -> str | None:
    """docs/21 §3.2's `identifiers` example is `{"docket_id": "CP24-12"}`; no connector populates
    it yet (`services/ingest/loader.py` only ever writes `queue_ids`/`eia_*`), so this is
    forward-compatible best-effort, not a load-bearing field for any postable event type today."""
    value = (identifiers or {}).get("docket_id")
    return str(value) if value else None


def _lifecycle_value(payload: dict[str, Any] | None, key: str = "lifecycle_state") -> str | None:
    return (payload or {}).get(key) if payload else None


def _proposal_social_event(event: Event, event_type: str, proposal: Proposal) -> SocialEvent:
    location = proposal.location
    retrieved_at = ensure_aware(event.retrieved_at or event.recorded_at)
    withdrawal_reason = (
        _lifecycle_value(event.after, "status_raw") if event_type == "proposal.withdrawn" else None
    )
    return SocialEvent(
        event_id=_social_event_id(event),
        event_type=event_type,
        event_date=ensure_aware(event.observed_at).date(),
        subject_type="proposal",
        subject_id=str(proposal.id),
        source_id=event.source_id or "",
        source_name=event.source.name if event.source else "",
        source_url=event.source_url or (event.source.url if event.source else ""),
        retrieved_at=retrieved_at,
        reuse_class=event.licence.reuse_class if event.licence else "unknown",
        page_url=f"{WEB_HOST}/proposals/{proposal.slug}",
        lag_days=LAG_DAYS_BY_KIND["proposal"],
        proposal_name=proposal.name_canonical,
        technology=proposal.technology,
        capacity_mw=float(proposal.capacity_mw) if proposal.capacity_mw is not None else None,
        county=location.county_name if location else None,
        state=_state_code(location.state_code if location else None, proposal.jurisdiction),
        iso_rto=proposal.iso,
        queue_id=_queue_id(proposal.identifiers),
        docket_id=_docket_id(proposal.identifiers),
        status_from=_lifecycle_value(event.before),
        status_to=_lifecycle_value(event.after),
        developer_org=proposal.sponsor.name_canonical if proposal.sponsor else None,
        withdrawal_reason_code=withdrawal_reason,
    )


def _opportunity_social_event(event: Event, event_type: str, opportunity: Opportunity) -> SocialEvent | None:
    issuer_org = opportunity.issuer.name_canonical if opportunity.issuer else None

    if event_type in ("opportunity.rfp_opened", "opportunity.rfp_closing", "opportunity.awarded"):
        # docs/32 §3.3's RFP/award templates print `issuer_org` unconditionally -- never an
        # omittable clause -- and services/social/README.md already records that these fields are
        # "effectively required, not omittable". `opportunity.issuer_org_id` is nullable
        # (services/db/models.py); a record with no linked issuer is not draftable for these types.
        if issuer_org is None:
            return None

    if event_type == "opportunity.awarded":
        # No `awardee`/`award_amount` column exists on `opportunity` yet, and no connector wired
        # so far writes one into `event.after` either (services/social/README.md "Known gap:
        # award/funding fields"). `editorial.channels_for_event` already refuses `awardee_org`-less
        # `opportunity.awarded` events on its own, but returning `None` here documents *why* at
        # the one place that knows it's a schema gap, not an editorial threshold.
        return None

    if event_type in ("funding.cancelled", "funding.reinstated"):
        # Same schema gap: no `funding_program`/`awardee`/`award_amount` field is populated for an
        # opportunity anywhere yet. `editorial.render_funding_change` has no omittable clause for
        # any of the three (docs/32 §3.3's template embeds them unconditionally), so drafting one
        # from an all-`None` record would print the literal word "None" into a real post -- never
        # invent a value; treat as not draftable rather than degrade the template's own contract.
        return None

    location = opportunity.location
    deadline_date = ensure_aware(opportunity.due_at).date() if opportunity.due_at else None
    technology = opportunity.technologies[0] if opportunity.technologies else None
    retrieved_at = ensure_aware(event.retrieved_at or event.recorded_at)
    return SocialEvent(
        event_id=_social_event_id(event),
        event_type=event_type,
        event_date=ensure_aware(event.observed_at).date(),
        subject_type="opportunity",
        subject_id=str(opportunity.id),
        source_id=event.source_id or "",
        source_name=event.source.name if event.source else "",
        source_url=event.source_url or (event.source.url if event.source else ""),
        retrieved_at=retrieved_at,
        reuse_class=event.licence.reuse_class if event.licence else "unknown",
        page_url=f"{WEB_HOST}/opportunities/{opportunity.slug}",
        lag_days=LAG_DAYS_BY_KIND["opportunity"],
        technology=technology,
        capacity_mw=(
            float(opportunity.capacity_sought_mw) if opportunity.capacity_sought_mw is not None else None
        ),
        county=location.county_name if location else None,
        state=_state_code(location.state_code if location else None, opportunity.jurisdiction),
        solicitation_title=opportunity.title,
        issuer_org=issuer_org,
        deadline_date=deadline_date,
    )


def social_event_from_db(db: Session, event: Event) -> SocialEvent | None:
    """Map one `event` row to a `SocialEvent`, or `None` if it is not a postable candidate at all.

    Raises `SubjectNotFoundError` if `event.subject_id` points at a proposal/opportunity that no
    longer exists -- a data inconsistency the caller (services/social/worker.py) records as a
    per-event error rather than treating as an ordinary "no post" outcome.
    """
    if event.subject_type not in ("proposal", "opportunity"):
        return None
    event_type = _map_event_type(event.subject_type, event.event_type)
    if event_type is None:
        return None

    if event.subject_type == "proposal":
        proposal = db.get(Proposal, event.subject_id)
        if proposal is None:
            raise SubjectNotFoundError(
                f"proposal {event.subject_id} referenced by event seq={event.seq} not found"
            )
        return _proposal_social_event(event, event_type, proposal)

    opportunity = db.get(Opportunity, event.subject_id)
    if opportunity is None:
        raise SubjectNotFoundError(
            f"opportunity {event.subject_id} referenced by event seq={event.seq} not found"
        )
    return _opportunity_social_event(event, event_type, opportunity)


__all__ = ["SubjectNotFoundError", "social_event_from_db"]
