"""Draft generation from the event log (Sprint 3 item 4).

`draft_posts_tick` is the function `infra/scheduler` wires up (contract fixed by the task brief;
do not change its name or signature). One tick: read `event` rows past the watermark, map each to
an `editorial.SocialEvent` (`services.social.db_events.social_event_from_db`), run the docs/32
§4.3 hard gates this module owns (`_passes_hard_gates`), then `editorial.channels_for_event` +
`editorial.build_draft` per channel, writing one `services.db.models.Post` row per channel that
clears every gate and is not a duplicate.

Everything else about "which events earn a post" and "what the post says" already lives in
`services.social.editorial` (templates, thresholds, validation) and is only ever *called* here,
never re-implemented (task brief: "you call these, you do not reimplement templates").

No draft in this module is ever sent anywhere: every `Post` row lands in state `draft`, or
`approved` with `auto_published=True` only when `graduation.may_auto_publish` allows it (the owner
enabled that named channel for that event type, the pair passes docs/32 §4.6 graduation, never
LinkedIn, under the daily cap) -- publishing is a later job (`CLAUDE.md`, task brief).

Since 2026-10-07 (content audit F1, F9): a draft that fails a validation gate is written and held
for review (`post.gate_failures`) instead of raising and being lost, and a tick's events are drafted
as stories -- one post per record and one per EIA plant (`_stories`).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import sys
import uuid as _uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from services.api.common import ensure_aware
from services.api.visibility import opportunity_visibility_filter, proposal_visibility_filter
from services.db.event_horizon import stable_event_seq
from services.db.models import Event, Opportunity, Post, Proposal, WorkerWatermark
from services.db.session import get_engine, get_sessionmaker
from services.ids import public_id
from services.social import editorial, graduation
from services.social.db_events import social_event_from_db
from services.social.queue import _content_hash

logger = logging.getLogger(__name__)

#: docs/32 §4.8's window, reused from editorial.py's own config rather than a second constant
#: (task brief: "the same subject in the last 7 days").
_DUPLICATE_WINDOW_DAYS = editorial.DEFAULT_CONFIG.dedupe_window_days

#: `services.db.models.WorkerWatermark.name` for this worker. One row per scheduled worker that
#: scans the event log (that model's own docstring); this is the only one that exists today.
_WATERMARK_NAME = "social.draft_posts"


@dataclass(frozen=True)
class DraftTickReport:
    started_at: dt.datetime
    finished_at: dt.datetime
    events_scanned: int
    events_eligible: int
    posts_created: int
    posts_skipped_duplicate: int
    posts_skipped_gate: int
    watermark_seq: int
    errors: tuple[str, ...]
    #: Of `posts_created`, the drafts kept for review although they fail a docs/32 §4.3 gate
    #: (`post.gate_failures`); before 2026-10-07 such an event raised, was counted in `errors` and
    #: was consumed by the watermark, so it was lost (content audit F1).
    posts_held: int = 0
    #: Of `posts_created`, the posts that went out without review (`graduation.may_auto_publish`).
    posts_auto_published: int = 0


def _post_derived_seq(db: Session) -> int:
    """The highest `event.seq` any existing `post` row references, via a join, or 0 if `post` is
    empty -- this was the *only* watermark before `WorkerWatermark` existed, and is now only the
    bootstrap value for a deployment upgrading from that scheme (coordinator decision: "an
    existing deployment keeps its position")."""
    value = db.scalar(select(func.max(Event.seq)).select_from(Post).join(Event, Event.id == Post.event_id))
    return int(value) if value is not None else 0


def _watermark_row(db: Session) -> WorkerWatermark:
    """The `worker_watermark` row this worker owns (`_WATERMARK_NAME`), created on first use.

    A brand-new deployment starts at seq 0 (nothing scanned yet); one upgrading from the
    post-derived-only watermark (no `worker_watermark` row, but `post` rows already exist) starts
    from `_post_derived_seq` instead, so it does not redraft everything already drafted."""
    row = db.get(WorkerWatermark, _WATERMARK_NAME)
    if row is None:
        row = WorkerWatermark(name=_WATERMARK_NAME, seq=_post_derived_seq(db))
        db.add(row)
        db.flush()
    return row


def _bare_url(url: str) -> str:
    """Strip query string and fragment (`editorial.bare_url`)."""
    return editorial.bare_url(url)


def _utm_link(page_url: str, *, channel: str, event_type: str, event_id: str) -> str:
    """docs/32 §3.2 item 3's UTM contract (`editorial.utm_url`). Since 2026-10-07 the tagged link is
    also what the body carries on X and LinkedIn, and the link facet's target on Bluesky (content
    audit F15)."""
    return editorial.utm_url(page_url, channel=channel, event_type=event_type, event_id=event_id)


def _passes_hard_gates(
    db: Session, event: Event, subject: Proposal | Opportunity, *, now: dt.datetime
) -> bool:
    """docs/32 §4.3, US-801 AC3: the four hard gates this worker owns (everything else -- length,
    banned words, number/organisation traceability, ... -- is `editorial.validate_draft`'s job,
    already run inside `editorial.build_draft`). `subject` is the record the post is about: the
    event's own subject, or the record it was merged into (`db_events.live_survivor`)."""
    if event.published_at is None:
        return False
    if subject.publish_state == "unpublished":
        return False
    if event.licence is None or not event.licence.allows_derived_publication:
        return False
    if isinstance(subject, Proposal):
        visible = db.scalar(
            select(Proposal.id).where(Proposal.id == subject.id, *proposal_visibility_filter("api", now))
        )
    else:
        visible = db.scalar(
            select(Opportunity.id).where(
                Opportunity.id == subject.id, *opportunity_visibility_filter("api", now)
            )
        )
    return visible is not None


def _is_content_duplicate(
    db: Session,
    *,
    subject_type: str,
    subject_ids: list[_uuid.UUID],
    channel: str,
    body: str,
    bare_link: str,
    since: dt.datetime,
) -> bool:
    """docs/32 §4.8 content-hash guard, scoped per the task brief: same subject, last 7 days, same
    `queue._content_hash(body, link_url)` (every URL removed, so the per-event UTM tag does not hide
    a repeat). Scoped to the same channel, matching `queue.py`'s own precedent, and to every record
    a plant post covers, so a generator posted on its own is not posted again inside its plant."""
    candidate_key = _content_hash(body, bare_link)
    rows = db.execute(
        select(Post.body, Post.link_url).where(
            Post.subject_type == subject_type,
            Post.subject_id.in_(subject_ids),
            Post.channel == channel,
            Post.created_at >= since,
        )
    )
    return any(
        _content_hash(existing_body, _bare_url(existing_link)) == candidate_key
        for existing_body, existing_link in rows
    )


@dataclass
class _Candidate:
    """One scanned event that clears the hard gates: the row, its `SocialEvent`, and the record
    the post is about."""

    event: Event
    social: editorial.SocialEvent
    subject: Proposal | Opportunity


def _candidate(db: Session, event: Event, *, now: dt.datetime) -> tuple[_Candidate | None, bool]:
    """`(candidate, gated)` for one event. Raises on a data inconsistency (missing subject row);
    the caller wraps this in a per-event savepoint so one bad event never loses another's work."""
    social_event = social_event_from_db(db, event)
    if social_event is None:
        return None, False
    subject: Proposal | Opportunity | None
    if social_event.subject_type == "proposal":
        subject = db.get(Proposal, _uuid.UUID(social_event.subject_id))
    else:
        subject = db.get(Opportunity, _uuid.UUID(social_event.subject_id))
    if subject is None:  # pragma: no cover -- social_event_from_db already raised for this case
        return None, False
    if not _passes_hard_gates(db, event, subject, now=now):
        return None, True
    if not editorial.channels_for_event(social_event) and social_event.eia_plant_id is None:
        return None, False
    return _Candidate(event=event, social=social_event, subject=subject), False


def _combined_technology(tokens: list[str | None]) -> str | None:
    """docs/22 §23's rule for a plant's parts: one technology when they agree; solar + storage ->
    `solar_storage`, wind + storage -> `wind_storage`; else the largest member's (the caller's
    first)."""
    distinct = {t for t in tokens if t}
    if len(distinct) <= 1:
        return next(iter(distinct), None)
    if distinct == {"solar", "storage"}:
        return "solar_storage"
    if distinct == {"wind", "storage"}:
        return "wind_storage"
    return tokens[0]


def _stories(candidates: list[_Candidate]) -> list[list[_Candidate]]:
    """Group a tick's candidates into one post each (content audit F9: 50 candidate events were 37
    plants; Project Matador was six 50 MW generator records with six identical status changes).

    1. One record, one story: of several events on the same record, the highest-priority one
       (`editorial.EVENT_PRIORITY`: withdrawn > status_changed > new), the latest on a tie.
    2. One EIA plant, one story: records of the same EIA plant, from the same source, with the same
       event type and new status, become one post about the plant, sized by summing the records
       (docs/22 §23's inventory rule, "the inventory's generators summed (the plant rollup)").
       A merged record already carries that sum through survivorship and is one record here.
    """
    by_subject: dict[tuple[str, str], _Candidate] = {}
    for c in candidates:
        key = (c.social.subject_type, c.social.subject_id)
        held = by_subject.get(key)
        if held is None or editorial.EVENT_PRIORITY.get(
            c.social.event_type, 0
        ) >= editorial.EVENT_PRIORITY.get(held.social.event_type, 0):
            by_subject[key] = c
    groups: dict[tuple[str, ...], list[_Candidate]] = {}
    for c in by_subject.values():
        s = c.social
        story: tuple[str, ...]
        if s.subject_type == "proposal" and s.eia_plant_id:
            story = ("plant", s.source_id, s.eia_plant_id, s.event_type, s.status_to or "")
        else:
            story = ("record", s.subject_type, s.subject_id)
        groups.setdefault(story, []).append(c)
    return list(groups.values())


def _story_event(members: list[_Candidate]) -> editorial.SocialEvent:
    """The `SocialEvent` one story is drafted from: the largest record's, sized for the plant."""
    ordered = sorted(members, key=lambda c: (-(c.social.capacity_mw or 0.0), c.event.seq))
    lead = ordered[0].social
    if len(ordered) == 1:
        return lead
    sizes = [c.social.capacity_mw for c in ordered]
    total = sum(v for v in sizes if v is not None) if any(v is not None for v in sizes) else None
    return dataclasses.replace(
        lead,
        capacity_mw=round(total, 3) if total is not None else None,
        technology=_combined_technology([c.social.technology for c in ordered]),
        member_count=len(ordered),
    )


def _draft_story(db: Session, members: list[_Candidate], *, now: dt.datetime) -> tuple[int, int, int, int]:
    """Write one post per channel the story earns. Returns `(created, duplicate, held, auto)`."""
    story = _story_event(members)
    lead = min(members, key=lambda c: (-(c.social.capacity_mw or 0.0), c.event.seq))
    subject_ids = [c.subject.id for c in members]
    since = now - dt.timedelta(days=_DUPLICATE_WINDOW_DAYS)
    created = duplicate = held = auto = 0

    for channel in editorial.channels_for_event(story):
        already_posted = db.scalar(
            select(Post.id).where(Post.event_id == lead.event.id, Post.channel == channel)
        )
        if already_posted is not None:  # pragma: no cover -- a second worker or a stale watermark
            duplicate += 1
            continue

        automated = graduation.may_auto_publish(db, channel, story.event_type, now=now)
        draft = editorial.build_draft(story, channel, automated=automated)
        failures = draft.validation.failures if draft.validation else ("no validation result",)
        passed = bool(draft.validation and draft.validation.passed)
        if not passed and automated:
            # Never auto-publish a draft that fails a gate; render it for review instead.
            automated = False
            draft = editorial.build_draft(story, channel)
            failures = draft.validation.failures if draft.validation else ("no validation result",)
            passed = bool(draft.validation and draft.validation.passed)

        if _is_content_duplicate(
            db,
            subject_type=story.subject_type,
            subject_ids=subject_ids,
            channel=channel,
            body=draft.body,
            bare_link=draft.link_url,
            since=since,
        ):
            duplicate += 1
            continue

        post = Post(
            public_id="",
            channel=channel,
            event_id=lead.event.id,
            subject_type=story.subject_type,
            subject_id=lead.subject.id,
            template_id=draft.template_id,
            template_version=draft.template_version,
            body=draft.body,
            link_url=draft.link_url,
            credit_line=draft.attribution_line,
            disclosure_label=draft.disclosure_text,
            state="approved" if automated else "draft",
            gate_checked_at=now,
            auto_published=automated,
            approved_by_user_id=None,
            metrics={},
            cost_usd=0,
            fields_snapshot=draft.fields_snapshot,
            # A draft that fails a gate is kept for a reviewer to correct (content audit F1), never
            # dropped and never approvable while a failure stands (`admin_approve_post`).
            gate_failures=None if passed else list(failures),
        )
        db.add(post)
        db.flush()
        post.public_id = public_id("post", post.id)
        created += 1
        held += 0 if passed else 1
        auto += 1 if automated else 0
        if not passed:
            logger.info(
                "social draft held for review: template_id=%s event seq=%s failures=%d",
                draft.template_id,
                lead.event.seq,
                len(failures),
            )
    return created, duplicate, held, auto


def draft_posts_tick(
    session_factory: sessionmaker[Session],
    *,
    limit: int = 500,
    now: dt.datetime | None = None,
) -> DraftTickReport:
    started_at = ensure_aware(now) if now is not None else dt.datetime.now(dt.UTC)
    tick_now = started_at

    events_scanned = 0
    events_eligible = 0
    posts_created = 0
    posts_skipped_duplicate = 0
    posts_skipped_gate = 0
    posts_held = 0
    posts_auto = 0
    errors: list[str] = []

    with session_factory() as db:
        watermark_row = _watermark_row(db)
        events = list(
            db.scalars(
                select(Event)
                # Bounded below every seq an open transaction may still commit, so the watermark
                # never passes a late-committing event (backend audit 2026-09-30 F5;
                # `services/db/event_horizon.py`).
                .where(Event.seq > watermark_row.seq, Event.seq <= stable_event_seq(db))
                .order_by(Event.seq.asc())
                .limit(limit)
            )
        )
        events_scanned = len(events)

        candidates: list[_Candidate] = []
        for event in events:
            try:
                with db.begin_nested():
                    candidate, gated = _candidate(db, event, now=tick_now)
            except Exception as exc:  # one bad event must never block the rest (task brief)
                errors.append(f"event seq={event.seq}: {type(exc).__name__}: {exc}")
                logger.warning("social draft tick: event seq=%s failed: %s", event.seq, type(exc).__name__)
                continue
            if gated:
                posts_skipped_gate += 1
            if candidate is not None:
                candidates.append(candidate)

        for members in _stories(candidates):
            try:
                with db.begin_nested():
                    created, duplicate, held, auto = _draft_story(db, members, now=tick_now)
            except Exception as exc:
                seqs = ",".join(str(c.event.seq) for c in members)
                errors.append(f"event seq={seqs}: {type(exc).__name__}: {exc}")
                logger.warning("social draft tick: story seq=%s failed: %s", seqs, type(exc).__name__)
                continue
            posts_created += created
            posts_skipped_duplicate += duplicate
            posts_held += held
            posts_auto += auto
            if created or duplicate:
                events_eligible += len(members)

        if events:
            # Coordinator fix: every *examined* event, not just posted ones, advances the
            # watermark -- otherwise a run of more than `limit` events that earn nothing (gated,
            # duplicate, ineligible, or erroring) would be rescanned on every tick forever and
            # starve newer events behind the page limit (a livelock, not merely a limitation).
            # A draft that fails a validation gate is no longer an error: it is written, held for
            # review (`post.gate_failures`), so advancing past its event loses nothing.
            watermark_row.seq = events[-1].seq
        db.commit()
        watermark_seq = watermark_row.seq

    finished_at = dt.datetime.now(dt.UTC)
    return DraftTickReport(
        started_at=started_at,
        finished_at=finished_at,
        events_scanned=events_scanned,
        events_eligible=events_eligible,
        posts_created=posts_created,
        posts_skipped_duplicate=posts_skipped_duplicate,
        posts_skipped_gate=posts_skipped_gate,
        watermark_seq=watermark_seq,
        errors=tuple(errors),
        posts_held=posts_held,
        posts_auto_published=posts_auto,
    )


def main() -> int:
    engine = get_engine()
    factory = get_sessionmaker(engine)
    report = draft_posts_tick(factory)
    summary = (
        f"social draft tick: scanned={report.events_scanned} eligible={report.events_eligible} "
        f"created={report.posts_created} dup={report.posts_skipped_duplicate} "
        f"gated={report.posts_skipped_gate} held={report.posts_held} auto={report.posts_auto_published} "
        f"watermark={report.watermark_seq} errors={len(report.errors)}\n"
    )
    sys.stdout.write(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised by running the module, not by import
    raise SystemExit(main())
