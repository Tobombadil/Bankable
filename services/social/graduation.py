"""docs/32 §4.6 graduation, read from the store, and the one rule for whether a post may publish
without review (content audit F11, legal L-8; 2026-10-07).

Before this module the owner's channel switch (`PUT /admin/v1/channels/{channel}/auto-publish`)
turned on a whole channel, LinkedIn included, and nothing consulted graduation:
`queue.ReviewQueue.graduation_status` exists only for the file-backed CLI queue and always refuses.
Now a post auto-publishes only when every one of these holds (`may_auto_publish`):

1. the channel is not LinkedIn (docs/32 §1.4, §4.6: "never; it stays manual by decision"; the
   register's `social.linkedin` row: API automation of posting is prohibited);
2. the owner enabled that named channel, with the disclosure confirmed, for this event type
   (`channel_config.auto_publish` and `auto_publish_event_types`; CLAUDE.md "the owner has
   explicitly enabled a named automated channel with disclosure"; docs/13 §7.4);
3. the (channel, event type) pair passes graduation now, not only when it was switched on, so a
   `wrong_fact` rejection revokes auto-publish at the next draft (§4.6 "revoked automatically");
4. the channel's `daily_cap` of auto-published posts is not reached (a capped post goes to review).

Graduation over the `post` table, per (channel, event type), the event type read from
`post.template_id` (`{event_type}.{channel}`):

* at least `GRADUATION_MIN_POSTS` posts published after human review, the first at least
  `GRADUATION_MIN_WINDOW_DAYS` days ago;
* over the trailing 100 drafts, an edit rate (posts whose body a reviewer changed, from the
  `admin_edit` audit events) of at most `GRADUATION_MAX_EDIT_RATE`, and no `wrong_fact` rejection;
* an adapter error rate (`failed` against `published` + `failed`, trailing 30 days) of at most
  `GRADUATION_MAX_ADAPTER_ERROR_RATE`.

Platform policy incidents have no store yet; the result says so, and the owner's explicit enabling
is the attestation (docs/13 §7.4 asks for a dated decisions-log row as well).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from services.db.models import ChannelConfig, Event, Post
from services.social.queue import (
    GRADUATION_MAX_ADAPTER_ERROR_RATE,
    GRADUATION_MAX_EDIT_RATE,
    GRADUATION_MIN_POSTS,
    GRADUATION_MIN_WINDOW_DAYS,
    GraduationResult,
)

#: Channels that never auto-publish, whatever the switch says.
NEVER_AUTO_PUBLISH: frozenset[str] = frozenset({"linkedin"})

_TRAILING_DRAFTS = 100


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _edited_post_ids(db: Session, post_ids: list[Any]) -> set[Any]:
    """Posts whose body a reviewer changed: `admin_edit` audit events on the post carrying `body`."""
    if not post_ids:
        return set()
    rows = db.execute(
        select(Event.subject_id, Event.after).where(
            Event.subject_type == "post",
            Event.event_type == "admin_edit",
            Event.subject_id.in_(post_ids),
        )
    )
    return {subject_id for subject_id, after in rows if isinstance(after, dict) and "body" in after}


def graduation_status(db: Session, channel: str, event_type: str, *, now: dt.datetime) -> GraduationResult:
    if channel in NEVER_AUTO_PUBLISH:
        return GraduationResult(
            channel=channel,
            event_type=event_type,
            posts_in_window=0,
            window_days=0,
            edit_rate=None,
            wrong_fact_count=0,
            policy_incidents=0,
            adapter_error_rate=None,
            eligible=False,
            reasons=("LinkedIn never auto-publishes (docs/32 §1.4, §4.6): it stays manual by decision.",),
        )

    template_id = f"{event_type}.{channel}"
    pair = (Post.channel == channel, Post.template_id == template_id)
    reviewed_published = list(
        db.execute(
            select(Post.published_at).where(
                *pair,
                Post.state == "published",
                Post.auto_published.is_(False),
                Post.published_at.is_not(None),
            )
        ).scalars()
    )
    first_published = min((_aware(p) for p in reviewed_published if p is not None), default=None)
    window_start = now - dt.timedelta(days=GRADUATION_MIN_WINDOW_DAYS)

    trailing = list(
        db.execute(
            select(Post.id, Post.reject_reason)
            .where(*pair, Post.state != "draft")
            .order_by(Post.created_at.desc())
            .limit(_TRAILING_DRAFTS)
        )
    )
    edited = _edited_post_ids(db, [post_id for post_id, _ in trailing])
    edit_rate = len(edited) / len(trailing) if trailing else None
    wrong_fact = sum(1 for _, reason in trailing if reason == "wrong_fact")

    published_30d = db.scalar(
        select(func.count()).where(*pair, Post.state == "published", Post.published_at >= window_start)
    )
    failed_30d = db.scalar(
        select(func.count()).where(*pair, Post.state == "failed", Post.updated_at >= window_start)
    )
    attempts = (published_30d or 0) + (failed_30d or 0)
    adapter_error_rate = (failed_30d or 0) / attempts if attempts else None

    reasons: list[str] = []
    if len(reviewed_published) < GRADUATION_MIN_POSTS:
        reasons.append(
            f"only {len(reviewed_published)} of {GRADUATION_MIN_POSTS} required reviewed posts published"
        )
    if first_published is None or first_published > window_start:
        reasons.append(f"reviewed posts do not yet span {GRADUATION_MIN_WINDOW_DAYS} days")
    if edit_rate is not None and edit_rate > GRADUATION_MAX_EDIT_RATE:
        reasons.append(f"edit rate {edit_rate:.1%} exceeds {GRADUATION_MAX_EDIT_RATE:.0%}")
    if wrong_fact:
        reasons.append(f"{wrong_fact} wrong_fact rejection(s) in the trailing {_TRAILING_DRAFTS} drafts")
    if adapter_error_rate is not None and adapter_error_rate > GRADUATION_MAX_ADAPTER_ERROR_RATE:
        reasons.append(
            f"adapter error rate {adapter_error_rate:.1%} exceeds {GRADUATION_MAX_ADAPTER_ERROR_RATE:.0%}"
        )
    eligible = not reasons
    if eligible:
        reasons.append(
            "thresholds met; platform policy incidents are not recorded in the store, so the owner's "
            "explicit enabling attests to none (docs/32 §4.6, docs/13 §7.4)"
        )
    return GraduationResult(
        channel=channel,
        event_type=event_type,
        posts_in_window=len(reviewed_published),
        window_days=GRADUATION_MIN_WINDOW_DAYS,
        edit_rate=edit_rate,
        wrong_fact_count=wrong_fact,
        policy_incidents=0,
        adapter_error_rate=adapter_error_rate,
        eligible=eligible,
        reasons=tuple(reasons),
    )


def auto_published_today(db: Session, channel: str, *, now: dt.datetime) -> int:
    start = now.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        db.scalar(
            select(func.count()).where(
                Post.channel == channel, Post.auto_published.is_(True), Post.created_at >= start
            )
        )
        or 0
    )


def may_auto_publish(db: Session, channel: str, event_type: str, *, now: dt.datetime) -> bool:
    """Every condition in the module docstring. False is the default answer."""
    if channel in NEVER_AUTO_PUBLISH:
        return False
    config = db.get(ChannelConfig, channel)
    if config is None or not config.auto_publish or not config.disclosure_label:
        return False
    if event_type not in (config.auto_publish_event_types or []):
        return False
    if not graduation_status(db, channel, event_type, now=now).eligible:
        return False
    return not (
        config.daily_cap is not None and auto_published_today(db, channel, now=now) >= config.daily_cap
    )


__all__ = ["NEVER_AUTO_PUBLISH", "auto_published_today", "graduation_status", "may_auto_publish"]
