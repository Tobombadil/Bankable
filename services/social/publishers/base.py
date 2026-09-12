"""Abstract publisher contract (docs/32-social-operating-playbook.md §4.5: "Adapter contract:
`publish(post) -> {platform_id, url, published_at}`; idempotent on `post.idempotency_key`;
raises typed errors `RateLimited(retry_after)`, `AuthExpired`, `Rejected(reason)`, `Transient`.").
"""

from __future__ import annotations

import abc
import dataclasses
import datetime as dt
from typing import Any, ClassVar

from services.social.editorial import CHANNEL_LIMITS
from services.social.models import PostDraft, ValidationResult


class PublishError(Exception):
    """Base for the docs/32 §4.5 typed adapter errors."""


class RateLimited(PublishError):
    def __init__(self, retry_after: dt.timedelta) -> None:
        super().__init__(f"rate limited, retry after {retry_after}")
        self.retry_after = retry_after


class AuthExpired(PublishError):
    pass


class Rejected(PublishError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Transient(PublishError):
    pass


class CredentialsMissing(PublishError):
    """Raised instead of attempting a live call when the channel's secrets are not in the
    environment (docs/32 §2.5) -- distinct from `AuthExpired`, which implies credentials exist
    but a token needs refreshing."""


@dataclasses.dataclass(frozen=True)
class PublishResult:
    platform_id: str | None
    url: str | None
    published_at: dt.datetime | None
    dry_run: bool
    would_send: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_id": self.platform_id,
            "url": self.url,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "dry_run": self.dry_run,
            "would_send": self.would_send,
        }


class Publisher(abc.ABC):
    """One channel adapter. `validate()` re-checks the docs/32 §4.3 gates plus channel-specific
    concerns (length, link count, labels, disclosure) at the moment of send, since a draft may
    have been approved some time before it is actually posted. `publish()` never sends live
    unless a subclass is explicitly told to and the required credentials are present."""

    channel: ClassVar[str]

    def validate(self, draft: PostDraft) -> ValidationResult:
        failures: list[str] = []
        if draft.channel != self.channel:
            failures.append(f"draft is for channel {draft.channel!r}, not {self.channel!r}")
        limit = CHANNEL_LIMITS[self.channel]
        if len(draft.body) > limit:
            failures.append(f"length {len(draft.body)} exceeds {self.channel} limit {limit}")
        if draft.body.count(draft.link_url) != 1:
            failures.append("link_url must appear exactly once")
        if not draft.disclosure_text:
            failures.append("disclosure text missing")
        if draft.attribution_line not in draft.body:
            failures.append("attribution line missing from body")
        if draft.status not in ("approved", "scheduled"):
            failures.append(f"draft status {draft.status!r} is not ready to publish")
        return ValidationResult(passed=not failures, failures=tuple(failures))

    @abc.abstractmethod
    def publish(self, draft: PostDraft, *, dry_run: bool = True) -> PublishResult:
        """Publish (or, always in Sprint 2, preview) `draft`. Raises one of the typed errors
        above on a live failure; a dry run never raises for validation reasons -- it reports
        them in `would_send["validation"]` instead, so a reviewer can see why a send would fail."""
        raise NotImplementedError
