"""Shared fixtures for the social review gates (docs/32 §4.6 graduation), used by the API, worker and
admin-page tests. Plain functions over a session, the `services/api/conftest.py` factory pattern."""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from services.db.models import Event, Post
from services.ids import public_id
from services.social.queue import GRADUATION_MIN_POSTS, GRADUATION_MIN_WINDOW_DAYS


def seed_graduated_pair(
    db: Session,
    event: Event,
    *,
    channel: str,
    event_type: str,
    count: int = GRADUATION_MIN_POSTS,
    now: dt.datetime | None = None,
    reject_wrong_fact: bool = False,
) -> list[Post]:
    """`count` posts of the (channel, event type) pair published after review, the first
    `GRADUATION_MIN_WINDOW_DAYS + 1` days ago: enough for `graduation.graduation_status` to pass
    when `count` is the minimum. `reject_wrong_fact` adds one `wrong_fact` rejection, which must
    revoke it."""
    now = now or dt.datetime.now(dt.UTC)
    start = now - dt.timedelta(days=GRADUATION_MIN_WINDOW_DAYS + 1)
    step = (now - start) / max(count, 1)
    posts = []
    for i in range(count):
        at = start + step * i
        posts.append(
            Post(
                public_id=f"post_seed{channel}{event_type}{i}",
                channel=channel,
                event_id=event.id,
                subject_type="proposal",
                subject_id=event.subject_id,
                template_id=f"{event_type}.{channel}",
                template_version="v2",
                body=f"seed {i}",
                link_url="https://example.org/p",
                credit_line="Source: seed",
                state="published",
                gate_checked_at=at,
                auto_published=False,
                published_at=at,
                created_at=at,
                metrics={},
                cost_usd=0,
            )
        )
    if reject_wrong_fact:
        posts.append(
            Post(
                public_id=f"post_seed{channel}{event_type}wrong",
                channel=channel,
                event_id=event.id,
                subject_type="proposal",
                subject_id=event.subject_id,
                template_id=f"{event_type}.{channel}",
                template_version="v2",
                body="seed wrong",
                link_url="https://example.org/p",
                credit_line="Source: seed",
                state="rejected",
                reject_reason="wrong_fact",
                gate_checked_at=now,
                auto_published=False,
                created_at=now,
                metrics={},
                cost_usd=0,
            )
        )
    db.add_all(posts)
    db.flush()
    for post in posts:
        post.public_id = public_id("post", post.id)
    db.flush()
    return posts
