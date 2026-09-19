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

No draft in this module is ever sent anywhere: every `Post` row lands in state `draft` (or
`approved` with `auto_published=True` when the channel's `ChannelConfig.auto_publish` is on) --
publishing is a later job (`CLAUDE.md`, task brief).
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from services.api.common import ensure_aware
from services.api.visibility import opportunity_visibility_filter, proposal_visibility_filter
from services.db.models import ChannelConfig, Event, Opportunity, Post, Proposal, WorkerWatermark
from services.db.session import get_engine, get_sessionmaker
from services.ids import public_id
from services.social import editorial
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
    """Strip query string and fragment -- used to compare a UTM-decorated `post.link_url`
    (`_utm_link`) against the bare `page_url` embedded in a freshly rendered draft's body."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _utm_link(page_url: str, *, channel: str, event_type: str, event_id: str) -> str:
    """docs/32 §3.2 item 3's UTM contract, applied to `post.link_url` -- not to the text embedded
    in the rendered body, which stays the bare `page_url` `editorial.validate_draft`'s gate 2
    checks appears exactly once (services/social/README.md decision on this)."""
    query = f"utm_source={channel}&utm_medium=social&utm_campaign={event_type}&utm_content={event_id}"
    separator = "&" if "?" in page_url else "?"
    return f"{page_url}{separator}{query}"


def _passes_hard_gates(
    db: Session, event: Event, subject: Proposal | Opportunity, *, now: dt.datetime
) -> bool:
    """docs/32 §4.3, US-801 AC3: the four hard gates this worker owns (everything else -- length,
    banned words, number/organisation traceability, ... -- is `editorial.validate_draft`'s job,
    already run inside `editorial.build_draft`)."""
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
    subject_id: object,
    channel: str,
    body: str,
    bare_link: str,
    since: dt.datetime,
) -> bool:
    """docs/32 §4.8 content-hash guard, scoped per the task brief: same subject, last 7 days,
    same `queue._content_hash(body, link_url)`. Also scoped to the same channel, matching
    `queue.py`'s own precedent (`if existing.channel != draft.channel: continue`) -- without it,
    two channels sharing one event's `render_proposal_new` output (bluesky and x render the exact
    same text, differing only in their character limit) would wrongly flag each other as
    duplicates of the very post this tick is trying to create. Existing rows' `link_url` carries
    the UTM decoration `_utm_link` adds (`_bare_url` strips it back to the plain `page_url` the
    hash was originally computed against, so the two sides compare like for like)."""
    candidate_key = _content_hash(body, bare_link)
    rows = db.execute(
        select(Post.body, Post.link_url).where(
            Post.subject_type == subject_type,
            Post.subject_id == subject_id,
            Post.channel == channel,
            Post.created_at >= since,
        )
    )
    return any(
        _content_hash(existing_body, _bare_url(existing_link)) == candidate_key
        for existing_body, existing_link in rows
    )


def _process_event(db: Session, event: Event, *, now: dt.datetime) -> tuple[int, int, int, bool]:
    """Returns `(posts_created, posts_skipped_duplicate, posts_skipped_gate, eligible)` for one
    event. Raises on an unexpected condition (missing subject row, a draft that fails its own
    validation gates); the caller wraps this in a per-event savepoint so one bad event never loses
    another event's work (task brief)."""
    social_event = social_event_from_db(db, event)
    if social_event is None:
        return 0, 0, 0, False

    subject: Proposal | Opportunity | None
    if social_event.subject_type == "proposal":
        subject = db.get(Proposal, event.subject_id)
    else:
        subject = db.get(Opportunity, event.subject_id)
    if subject is None:  # pragma: no cover -- social_event_from_db already raised for this case
        return 0, 0, 0, False

    if not _passes_hard_gates(db, event, subject, now=now):
        return 0, 0, 1, False

    posts_created = 0
    posts_skipped_duplicate = 0
    since = now - dt.timedelta(days=_DUPLICATE_WINDOW_DAYS)

    for channel in editorial.channels_for_event(social_event):
        already_posted = db.scalar(select(Post.id).where(Post.event_id == event.id, Post.channel == channel))
        if already_posted is not None:
            # Not reachable through this tick's own control flow: every channel for one event
            # commits or rolls back together in one savepoint (`draft_posts_tick`), and
            # `WorkerWatermark` advances past every *examined* event at the end of the tick
            # (`_watermark_row`), so a fully-processed event's seq is never scanned again. Kept as
            # a defensive check for a second worker process or a manually re-run tick against a
            # stale watermark.
            posts_skipped_duplicate += 1  # pragma: no cover
            continue  # pragma: no cover

        draft = editorial.build_draft(social_event, channel)
        # `editorial.channels_for_event` already gates on every field its own templates treat as
        # non-omittable, so a real `SocialEvent` reaching this point should always validate. Kept
        # as a defensive check -- a template change here should fail loudly in this tick's
        # `errors`, never emit an invalid post -- rather than something this test suite can
        # trigger through `db_events.social_event_from_db`'s own output.
        if draft.validation is None or not draft.validation.passed:  # pragma: no cover
            failures = draft.validation.failures if draft.validation else ("no validation result",)
            msg = f"draft failed validation gates for channel {channel}: {failures}"
            raise ValueError(msg)

        if _is_content_duplicate(
            db,
            subject_type=social_event.subject_type,
            subject_id=subject.id,
            channel=channel,
            body=draft.body,
            bare_link=draft.link_url,
            since=since,
        ):
            posts_skipped_duplicate += 1
            continue

        channel_config = db.get(ChannelConfig, channel)
        auto_publish = bool(channel_config and channel_config.auto_publish)
        disclosure_label = (
            channel_config.disclosure_label
            if channel_config and channel_config.disclosure_label
            else draft.disclosure_text
        )
        link_url = _utm_link(
            draft.link_url,
            channel=channel,
            event_type=social_event.event_type,
            event_id=social_event.event_id,
        )

        post = Post(
            public_id="",
            channel=channel,
            event_id=event.id,
            subject_type=social_event.subject_type,
            subject_id=subject.id,
            template_id=draft.template_id,
            template_version=draft.template_version,
            body=draft.body,
            link_url=link_url,
            credit_line=draft.attribution_line,
            disclosure_label=disclosure_label,
            state="approved" if auto_publish else "draft",
            gate_checked_at=now,
            auto_published=auto_publish,
            approved_by_user_id=None,
            metrics={},
            cost_usd=0,
        )
        db.add(post)
        db.flush()
        post.public_id = public_id("post", post.id)
        posts_created += 1

    return posts_created, posts_skipped_duplicate, 0, True


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
    errors: list[str] = []

    with session_factory() as db:
        watermark_row = _watermark_row(db)
        events = list(
            db.scalars(
                select(Event).where(Event.seq > watermark_row.seq).order_by(Event.seq.asc()).limit(limit)
            )
        )
        events_scanned = len(events)

        for event in events:
            try:
                with db.begin_nested():
                    created, skipped_dup, skipped_gate, eligible = _process_event(db, event, now=tick_now)
            except Exception as exc:  # one bad event must never block the rest (task brief)
                errors.append(f"event seq={event.seq}: {type(exc).__name__}: {exc}")
                logger.warning("social draft tick: event seq=%s failed: %s", event.seq, type(exc).__name__)
                continue
            posts_created += created
            posts_skipped_duplicate += skipped_dup
            posts_skipped_gate += skipped_gate
            if eligible:
                events_eligible += 1

        if events:
            # Coordinator fix: every *examined* event, not just posted ones, advances the
            # watermark -- otherwise a run of more than `limit` events that earn nothing (gated,
            # duplicate, ineligible, or erroring) would be rescanned on every tick forever and
            # starve newer events behind the page limit (a livelock, not merely a limitation).
            # Same transaction as this tick's posts, so a failure before `commit()` below leaves
            # both the posts and the watermark advance rolled back together.
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
    )


def main() -> int:
    engine = get_engine()
    factory = get_sessionmaker(engine)
    report = draft_posts_tick(factory)
    summary = (
        f"social draft tick: scanned={report.events_scanned} eligible={report.events_eligible} "
        f"created={report.posts_created} dup={report.posts_skipped_duplicate} "
        f"gated={report.posts_skipped_gate} watermark={report.watermark_seq} "
        f"errors={len(report.errors)}\n"
    )
    sys.stdout.write(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised by running the module, not by import
    raise SystemExit(main())
