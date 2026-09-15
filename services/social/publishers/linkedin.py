"""LinkedIn publisher (docs/32-social-operating-playbook.md §1.4).

Dry-run only. LinkedIn "stays manual by decision" (docs/32 §1.4, §4.6) even after every other
graduation criterion is met, and the launch route is a scheduler bridge (Buffer/Hootsuite) or the
Community Management API once approved -- neither is wired here. There is no `feature_flag_live`
for this channel: the live path always refuses, because programmatic posting without the vetted
Marketing Developer Platform approval breaches the User Agreement (docs/13-legal-outreach-and-
social.md §5.1), and no approval or scheduler credential exists yet.
"""

from __future__ import annotations

from services.social.models import PostDraft
from services.social.publishers.base import Publisher, PublishResult


class LinkedInPublisher(Publisher):
    channel = "linkedin"

    def _payload(self, draft: PostDraft) -> dict[str, object]:
        return {
            "author": "urn:li:organization:{LINKEDIN_ORG_URN}",
            "commentary": draft.body,
            "content": {
                "article": {
                    "source": draft.link_url,
                    "title": draft.event_type,
                    "description": draft.attribution_line,
                }
            },
            "visibility": "PUBLIC",
            "lifecycleState": "PUBLISHED",
        }

    def publish(self, draft: PostDraft, *, dry_run: bool = True) -> PublishResult:
        validation = self.validate(draft)
        payload = {**self._payload(draft), "validation": validation.to_dict()}
        if dry_run:
            return PublishResult(
                platform_id=None, url=None, published_at=None, dry_run=True, would_send=payload
            )
        raise NotImplementedError(
            "LinkedIn never auto-publishes from this pipeline (docs/32 §1.4, §4.6): the owner "
            "posts via the scheduler bridge or, once MDP-approved, the Community Management API "
            "-- neither is wired here, and no account/app approval exists yet (docs/32 §2)."
        )
