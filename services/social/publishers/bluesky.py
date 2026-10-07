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

from services.social.editorial import CHANNEL_LIMITS, bare_url
from services.social.models import PostDraft
from services.social.publishers.base import CredentialsMissing, Publisher, PublishResult


class BlueskyPublisher(Publisher):
    channel = "bluesky"

    def __init__(self, *, feature_flag_live: bool = False) -> None:
        self.feature_flag_live = feature_flag_live

    def _payload(self, draft: PostDraft) -> dict[str, object]:
        """The `app.bsky.feed.post` record (content audit F14). The body shows the bare record
        address; a link facet over those bytes carries the tagged link (`draft.link_url`), so the
        link is clickable and its clicks are counted (F15). The card is titled with the record's
        name and described with the post's fact line, never the event-type token. No thumbnail:
        that needs an uploaded blob and the pages have no `og:image` yet (open item)."""
        text = draft.body
        shown = bare_url(draft.link_url)
        encoded = text.encode("utf-8")
        start = encoded.find(shown.encode("utf-8"))
        facets: list[dict[str, object]] = []
        if start >= 0:
            facets.append(
                {
                    "index": {"byteStart": start, "byteEnd": start + len(shown.encode("utf-8"))},
                    "features": [{"$type": "app.bsky.richtext.facet#link", "uri": draft.link_url}],
                }
            )
        snapshot = draft.fields_snapshot or {}
        title = snapshot.get("proposal_name") or snapshot.get("solicitation_title") or text.split(":", 1)[0]
        fact_line = text.split(shown, 1)[0].strip() if shown in text else text
        return {
            "text": text,
            "collection": "app.bsky.feed.post",
            "langs": ["en"],
            "facets": facets,
            "embed": {
                "$type": "app.bsky.embed.external",
                "external": {
                    "uri": draft.link_url,
                    "title": str(title),
                    "description": fact_line,
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
