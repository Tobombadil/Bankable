"""Dry-run-only publisher adapters (docs/32-social-operating-playbook.md §4.1, §4.5).

No account exists yet for any channel (the owner creates them, `docs/32` §2). Every publisher in
this package validates a draft and reports what *would* be sent; none of them makes a network
call by default, and the one publisher with a real (unimplemented) live path -- Bluesky, behind
`feature_flag_live` -- refuses to run without `BSKY_HANDLE`/`BSKY_APP_PASSWORD` in the
environment even when that flag is set.
"""

from __future__ import annotations

from services.social.publishers.base import (
    AuthExpired,
    CredentialsMissing,
    Publisher,
    PublishError,
    PublishResult,
    RateLimited,
    Rejected,
    Transient,
)
from services.social.publishers.bluesky import BlueskyPublisher
from services.social.publishers.linkedin import LinkedInPublisher
from services.social.publishers.x import XPublisher

__all__ = [
    "AuthExpired",
    "BlueskyPublisher",
    "CredentialsMissing",
    "LinkedInPublisher",
    "PublishError",
    "PublishResult",
    "Publisher",
    "RateLimited",
    "Rejected",
    "Transient",
    "XPublisher",
]

PUBLISHERS: dict[str, type[Publisher]] = {
    "bluesky": BlueskyPublisher,
    "linkedin": LinkedInPublisher,
    "x": XPublisher,
}
