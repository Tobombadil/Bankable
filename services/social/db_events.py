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
from services.api.serialize import source_credit
from services.api.visibility import gated_opportunity, gated_proposal, organization_visible
from services.db.models import NON_PUBLIC_EVENT_TYPES, Event, Opportunity, Proposal, Source
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


def _event_lag_days(event: Event) -> int | None:
    """How far behind the public feed is *for this event*, or `None` for "no delay to disclose".

    `services/social/editorial.py` prints "this public feed runs N days behind our live tier" only
    when this is truthy. Since the paywall became a matter of shape rather than time (owner,
    2026-09-19) that clause is true only of a change event from a source that declares a
    change-event lag — the eight ISO queue registers today — so it is read back from the event's
    own two stored columns rather than from a per-kind constant: a record is never delayed, and a
    post that claimed otherwise would be a false statement on a public channel.
    """
    if event.public_at is None or event.published_at is None:
        return None
    days = (ensure_aware(event.public_at) - ensure_aware(event.published_at)).days
    return days if days > 0 else None


def _lifecycle_value(payload: dict[str, Any] | None, key: str = "lifecycle_state") -> str | None:
    return (payload or {}).get(key) if payload else None


def credit_line_for(source: Source | None) -> str:
    """The credit the record page prints for `source` (`web/templates/_macros.html`): the licence's
    credit verbatim (`services.api.serialize.source_credit`: an operator override, else the
    manifest's `attribution` plus its statement of changes), else `Source: {name}`. `source_credit`
    falls back to the bare name, which the page prefixes, so the prefix is added only then."""
    if source is None:
        return ""
    credit = source_credit(source)
    return f"Source: {credit}" if credit == source.name else credit


def _source_fields(event: Event) -> dict[str, Any]:
    source = event.source
    licence = event.licence
    return {
        "source_id": event.source_id or "",
        "source_name": source.name if source else "",
        "source_url": event.source_url or (source.url if source else ""),
        "reuse_class": licence.reuse_class if licence else "unknown",
        "credit_line": credit_line_for(source),
        "source_category": source.category if source else None,
        "licence_name": licence.name if licence else None,
    }


def live_survivor(db: Session, proposal: Proposal) -> Proposal:
    """The record a merged-away proposal now lives in (`merged_into_id`, followed to its end; a
    cycle or a missing row stops at the last good one). A post is about the project as the site
    shows it now, with the survivor's field-survivorship values and page (content audit F9;
    docs/22 §23), never about a record whose page redirects."""
    seen = {proposal.id}
    current = proposal
    while current.merged_into_id is not None and current.merged_into_id not in seen:
        nxt = db.get(Proposal, current.merged_into_id)
        if nxt is None:
            break
        seen.add(nxt.id)
        current = nxt
    return current


def _proposal_social_event(event: Event, event_type: str, proposal: Proposal) -> SocialEvent | None:
    # A post is a public surface: every field is the record's public served view
    # (`services/api/visibility.py::GatedRecord`), never a value from a hidden source.
    proposal = gated_proposal(proposal, "public")
    if event_type == "proposal.status_changed" and proposal.lifecycle_state != _lifecycle_value(event.after):
        # The post states the record's status now. A record that has since moved on (a later
        # event, or a merged survivor whose own survivorship status differs) is not drafted from
        # this event: the later event, if any, is the news.
        return None
    location = proposal.location
    retrieved_at = ensure_aware(event.retrieved_at or event.recorded_at)
    withdrawal_reason = (
        _lifecycle_value(event.after, "status_raw") if event_type == "proposal.withdrawn" else None
    )
    identifiers = proposal.identifiers or {}
    plant = identifiers.get("eia_plant_id")
    return SocialEvent(
        event_id=_social_event_id(event),
        event_type=event_type,
        event_date=ensure_aware(event.observed_at).date(),
        subject_type="proposal",
        subject_id=str(proposal.id),
        retrieved_at=retrieved_at,
        page_url=f"{WEB_HOST}/proposals/{proposal.slug}",
        lag_days=_event_lag_days(event),
        proposal_name=proposal.name_canonical,
        technology=proposal.technology,
        capacity_mw=float(proposal.capacity_mw) if proposal.capacity_mw is not None else None,
        county=location.county_name if location else None,
        state=_state_code(location.state_code if location else None, proposal.jurisdiction),
        country=(location.country if location and location.country else "US"),
        iso_rto=proposal.iso,
        queue_id=_queue_id(proposal.identifiers),
        docket_id=_docket_id(proposal.identifiers),
        status_from=_lifecycle_value(event.before),
        status_to=_lifecycle_value(event.after),
        # A sponsor the public tier may not see is not named in a post either (docs/21 §8 item 5's
        # rule for a gated source, applied to a taken-down organisation; migration 0022).
        developer_org=(
            proposal.sponsor.name_canonical
            if proposal.sponsor is not None and organization_visible(proposal.sponsor)
            else None
        ),
        withdrawal_reason_code=withdrawal_reason,
        kind=proposal.kind,
        eia_plant_id=str(plant) if plant else None,
        **_source_fields(event),
    )


def _opportunity_social_event(event: Event, event_type: str, opportunity: Opportunity) -> SocialEvent | None:
    opportunity = gated_opportunity(opportunity, "public")
    issuer_org = (
        opportunity.issuer.name_canonical
        if opportunity.issuer is not None and organization_visible(opportunity.issuer)
        else None
    )

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
        retrieved_at=retrieved_at,
        page_url=f"{WEB_HOST}/opportunities/{opportunity.slug}",
        lag_days=_event_lag_days(event),
        technology=technology,
        capacity_mw=(
            float(opportunity.capacity_sought_mw) if opportunity.capacity_sought_mw is not None else None
        ),
        county=location.county_name if location else None,
        state=_state_code(location.state_code if location else None, opportunity.jurisdiction),
        solicitation_title=opportunity.title,
        issuer_org=issuer_org,
        deadline_date=deadline_date,
        **_source_fields(event),
    )


def social_event_from_db(db: Session, event: Event) -> SocialEvent | None:
    """Map one `event` row to a `SocialEvent`, or `None` if it is not a postable candidate at all.

    Raises `SubjectNotFoundError` if `event.subject_id` points at a proposal/opportunity that no
    longer exists -- a data inconsistency the caller (services/social/worker.py) records as a
    per-event error rather than treating as an ordinary "no post" outcome.
    """
    if event.subject_type not in ("proposal", "opportunity"):
        return None
    if event.event_type in NON_PUBLIC_EVENT_TYPES:
        # `removed_from_source` (a row that left its source's file, not a withdrawal; docs/51 §2.7
        # item 1) is never a post. The maps below already default-deny it and the worker's
        # `published_at` gate refuses it; this says so by name, so a later map entry cannot.
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
        return _proposal_social_event(event, event_type, live_survivor(db, proposal))

    opportunity = db.get(Opportunity, event.subject_id)
    if opportunity is None:
        raise SubjectNotFoundError(
            f"opportunity {event.subject_id} referenced by event seq={event.seq} not found"
        )
    return _opportunity_social_event(event, event_type, opportunity)


__all__ = ["SubjectNotFoundError", "credit_line_for", "live_survivor", "social_event_from_db"]
