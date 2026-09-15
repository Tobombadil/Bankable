"""X publisher (docs/32-social-operating-playbook.md §1.5).

Dry-run only. X posting is pay-per-use ($0.20/post with a URL); this adapter reports the cost
estimate in `would_send` but never spends real credit. No `feature_flag_live` exists for this
channel in Sprint 2 -- OAuth 2.0 PKCE and the automated-account authorisation are owner setup
steps (docs/32 §2.4) that have not happened, so the live path always refuses.
"""

from __future__ import annotations

from services.social.editorial import cost_estimate_usd
from services.social.models import PostDraft
from services.social.publishers.base import Publisher, PublishResult


class XPublisher(Publisher):
    channel = "x"

    def _payload(self, draft: PostDraft) -> dict[str, object]:
        return {"text": draft.body, "estimated_cost_usd": cost_estimate_usd(self.channel)}

    def publish(self, draft: PostDraft, *, dry_run: bool = True) -> PublishResult:
        validation = self.validate(draft)
        payload = {**self._payload(draft), "validation": validation.to_dict()}
        if dry_run:
            return PublishResult(
                platform_id=None, url=None, published_at=None, dry_run=True, would_send=payload
            )
        raise NotImplementedError(
            "live X publish (OAuth 2.0 PKCE, POST /2/tweets) is out of Sprint 2 scope: no "
            "developer app or automated-account authorisation exists yet (docs/32 §2.4), and "
            "the $250/month budget cap (docs/32 §1.5) is not wired to a real credit balance."
        )
