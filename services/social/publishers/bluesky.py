"""Bluesky publisher (docs/32-social-operating-playbook.md §1.3).

Dry-run only by default. A real publish path may be written behind `feature_flag_live` using the
AT Protocol app-password flow (`com.atproto.server.createSession` then
`com.atproto.repo.createRecord`, collection `app.bsky.feed.post`), per the task brief -- but it
must refuse to run without `BSKY_HANDLE`/`BSKY_APP_PASSWORD` in the environment, and this class
is never constructed with `feature_flag_live=True` by anything else in this package. No account
exists yet (docs/32 §2), so the live path itself is not implemented here: turning the flag on with
valid credentials still raises `NotImplementedError`, not a network call.
"""

from __future__ import annotations

import datetime as dt
import os

from services.social.editorial import CHANNEL_LIMITS
from services.social.models import PostDraft
from services.social.publishers.base import CredentialsMissing, Publisher, PublishResult


class BlueskyPublisher(Publisher):
    channel = "bluesky"

    def __init__(self, *, feature_flag_live: bool = False) -> None:
        self.feature_flag_live = feature_flag_live

    def _payload(self, draft: PostDraft) -> dict[str, object]:
        return {
            "text": draft.body,
            "collection": "app.bsky.feed.post",
            "langs": ["en"],
            "embed": {
                "$type": "app.bsky.embed.external",
                "external": {
                    "uri": draft.link_url,
                    "title": draft.event_type,
                    "description": draft.attribution_line,
                },
            },
            "createdAt": dt.datetime.now(dt.UTC).isoformat(),
        }

    def publish(self, draft: PostDraft, *, dry_run: bool = True) -> PublishResult:
        validation = self.validate(draft)
        payload_and_checks = {
            **self._payload(draft),
            "validation": validation.to_dict(),
            "channel_limit": CHANNEL_LIMITS[self.channel],
        }

        if dry_run or not self.feature_flag_live:
            return PublishResult(
                platform_id=None, url=None, published_at=None, dry_run=True, would_send=payload_and_checks
            )

        handle = os.environ.get("BSKY_HANDLE")
        app_password = os.environ.get("BSKY_APP_PASSWORD")
        if not handle or not app_password:
            raise CredentialsMissing(
                "BSKY_HANDLE and BSKY_APP_PASSWORD must both be set in the environment "
                "(docs/32 §2.5) before a live Bluesky publish is attempted"
            )
        raise NotImplementedError(
            "live Bluesky publish (AT Protocol app-password session + createRecord) is out of "
            "Sprint 2 scope: no account exists yet (docs/32 §2). Wire this once the owner hands "
            "back BSKY_HANDLE/BSKY_APP_PASSWORD and the account has been created."
        )
