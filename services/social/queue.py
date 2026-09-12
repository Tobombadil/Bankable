"""The review queue (docs/32-social-operating-playbook.md §4.4-§4.9).

`ReviewQueue` is a JSON-file-backed store (task brief: "JSON or SQLite store" -- JSON chosen so
the queue is human-diffable and needs no new runtime dependency). It owns everything the drafting
step in `services.social.editorial` cannot decide on its own because it needs the store's history:
cross-event duplicate suppression, the SLA expiry clock, rate-limit-aware scheduling stubs, cost
roll-ups, and the graduation check.

No network call and no real publish happens here. `schedule()` only assigns a slot; actually
sending is `services.social.publishers`' job, and every publisher in this package is dry-run only.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import re
from collections.abc import Callable, Iterable
from typing import Any
from zoneinfo import ZoneInfo

from services.social import editorial
from services.social.models import CHANNELS, REJECT_REASONS, PostDraft

_ET = ZoneInfo("America/New_York")

#: docs/32 §4.4 queue SLA: stale drafts expire rather than post as stale news.
SLA_HOURS: dict[str, int] = {"linkedin": 24, "x": 24, "bluesky": 48}

#: docs/32 §4.7 "our ceiling" column (not the platform limit -- our own configured daily cap).
DAILY_CEILING: dict[str, int] = {"bluesky": 100, "linkedin": 3, "x": 30}

#: docs/32 §4.5 per-channel calendar slots, in America/New_York.
BLUESKY_X_WINDOW = (dt.time(6, 0), dt.time(20, 0))
BLUESKY_X_MIN_GAP_MINUTES = 10
LINKEDIN_SLOTS = (dt.time(7, 30), dt.time(12, 0), dt.time(16, 30))

#: docs/32 §4.6 graduation gate.
GRADUATION_MIN_POSTS = 200
GRADUATION_MIN_WINDOW_DAYS = 30
GRADUATION_MAX_EDIT_RATE = 0.05
GRADUATION_MAX_ADAPTER_ERROR_RATE = 0.01


@dataclasses.dataclass
class GraduationResult:
    channel: str
    event_type: str
    posts_in_window: int
    window_days: int
    edit_rate: float | None
    wrong_fact_count: int
    policy_incidents: int
    adapter_error_rate: float | None
    eligible: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class DuplicateDraft(Exception):
    """Raised by `add_draft` when the event/channel pair is suppressed (docs/32 §4.8)."""

    def __init__(self, reason: str, existing_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.existing_id = existing_id


class ReviewQueue:
    def __init__(
        self, path: str | pathlib.Path, *, now: Callable[[], dt.datetime] | None = None
    ) -> None:
        self.path = pathlib.Path(path)
        self._now: Callable[[], dt.datetime] = now or (lambda: dt.datetime.now(dt.UTC))
        self._drafts: dict[str, PostDraft] = {}
        if self.path.exists():
            self._load()

    # ------------------------------------------------------------------------- persistence

    def _load(self) -> None:
        raw = json.loads(self.path.read_text())
        self._drafts = {d["id"]: PostDraft.from_dict(d) for d in raw.get("drafts", [])}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"drafts": [d.to_dict() for d in self._drafts.values()]}
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    # ------------------------------------------------------------------------- duplicate suppression (§4.8)

    def _find_duplicate(self, draft: PostDraft) -> DuplicateDraft | None:
        for existing in self._drafts.values():
            if existing.idempotency_key == draft.idempotency_key:
                return DuplicateDraft("idempotency_key already queued or published", existing.id)

        window = dt.timedelta(days=editorial.DEFAULT_CONFIG.dedupe_window_days)
        now = draft.created_at
        for existing in self._drafts.values():
            if existing.dedupe_key != draft.dedupe_key:
                continue
            if existing.status not in ("approved", "scheduled", "published", "draft"):
                continue
            if now - existing.created_at > window:
                continue
            if editorial.outranks(draft.event_type, existing.event_type):
                continue
            return DuplicateDraft(
                f"subject already posted to {draft.channel} within "
                f"{editorial.DEFAULT_CONFIG.dedupe_window_days} days ({existing.event_type})",
                existing.id,
            )

        content_key = _content_hash(draft.body, draft.link_url)
        thirty_days = dt.timedelta(days=30)
        for existing in self._drafts.values():
            if existing.channel != draft.channel:
                continue
            if now - existing.created_at > thirty_days:
                continue
            if _content_hash(existing.body, existing.link_url) == content_key:
                return DuplicateDraft("content hash matches a post from the last 30 days", existing.id)
        return None

    def add_draft(self, draft: PostDraft) -> PostDraft:
        """Persist a drafted post, re-running validation with the store's duplicate check
        (`editorial.build_draft` cannot see queue history) and raising `DuplicateDraft` instead
        of silently storing a suppressed post."""
        duplicate = self._find_duplicate(draft)
        if duplicate is not None:
            raise duplicate

        draft.review.sla_expires_at = draft.created_at + dt.timedelta(hours=SLA_HOURS[draft.channel])
        self._drafts[draft.id] = draft
        self._save()
        return draft

    # ------------------------------------------------------------------------- list/approve/reject/edit

    def list_drafts(
        self, *, channel: str | None = None, status: str | None = None
    ) -> list[PostDraft]:
        drafts = list(self._drafts.values())
        if channel is not None:
            drafts = [d for d in drafts if d.channel == channel]
        if status is not None:
            drafts = [d for d in drafts if d.status == status]
        return sorted(drafts, key=lambda d: d.created_at)

    def get(self, draft_id: str) -> PostDraft:
        return self._drafts[draft_id]

    def approve(self, draft_id: str, reviewer: str) -> PostDraft:
        draft = self._drafts[draft_id]
        if draft.status not in ("draft",):
            raise ValueError(f"cannot approve a draft in status {draft.status!r}")
        if draft.validation is not None and not draft.validation.passed:
            raise ValueError(f"cannot approve a draft that failed validation: {draft.validation.failures}")
        draft.status = "approved"
        draft.review.reviewed_at = self._now()
        draft.review.reviewer = reviewer
        draft.review.decision = "approved"
        draft.updated_at = self._now()
        self._save()
        return draft

    def reject(self, draft_id: str, reviewer: str, reason_code: str) -> PostDraft:
        if reason_code not in REJECT_REASONS:
            raise ValueError(f"reason_code must be one of {REJECT_REASONS}")
        draft = self._drafts[draft_id]
        draft.status = "rejected"
        draft.review.reviewed_at = self._now()
        draft.review.reviewer = reviewer
        draft.review.decision = "rejected"
        draft.review.reject_reason = reason_code
        draft.updated_at = self._now()
        self._save()
        return draft

    def edit(self, draft_id: str, new_body: str, reviewer: str) -> PostDraft:
        """Store an edit as a diff (docs/32 §4.4: "Edits are stored as diffs (used for
        graduation metrics)") and re-validate; the draft returns to `draft` status for approval
        (an edited post is not itself an approval)."""
        draft = self._drafts[draft_id]
        old_body = draft.body
        draft.body = new_body
        draft.validation = editorial.validate_draft(
            new_body,
            _event_from_snapshot(draft),
            draft.channel,
            attribution_line=draft.attribution_line,
            disclosure_text=draft.disclosure_text,
            delayed_tier_notice=draft.delayed_tier_notice,
        )
        draft.review.edit_count += 1
        draft.review.last_edit_diff = f"- {old_body}\n+ {new_body}"
        draft.review.reviewer = reviewer
        draft.review.decision = "edited"
        draft.status = "draft"
        draft.updated_at = self._now()
        self._save()
        return draft

    # ------------------------------------------------------------------------- SLA expiry (§4.4)

    def expire_stale(self, at: dt.datetime | None = None) -> list[PostDraft]:
        at = at or self._now()
        expired = []
        for draft in self._drafts.values():
            if draft.status not in ("draft", "approved"):
                continue
            deadline = draft.review.sla_expires_at
            if draft.event_type == "opportunity.rfp_closing":
                deadline_date = draft.fields_snapshot.get("deadline_date")
                if deadline_date:
                    deadline = dt.datetime.fromisoformat(deadline_date).replace(tzinfo=dt.UTC)
            if deadline is not None and at >= deadline:
                draft.status = "withdrawn"
                draft.review.decision = "expired"
                draft.updated_at = at
                expired.append(draft)
        if expired:
            self._save()
        return expired

    # ------------------------------------------------------------------------- scheduling stubs (§4.5)

    def daily_count(self, channel: str, day: dt.date) -> int:
        return sum(
            1
            for d in self._drafts.values()
            if d.channel == channel
            and d.status in ("scheduled", "published")
            and d.scheduled_for is not None
            and d.scheduled_for.astimezone(_ET).date() == day
        )

    def check_rate_limit(self, channel: str, day: dt.date) -> bool:
        """True if another post may still be scheduled for `channel` on `day` under our own
        configured ceiling (docs/32 §4.7 "our ceiling" column, not the platform's raw limit)."""
        return self.daily_count(channel, day) < DAILY_CEILING[channel]

    def next_available_slot(self, channel: str, after: dt.datetime) -> dt.datetime:
        """A scheduling stub: the next calendar slot for `channel` at or after `after`, honouring
        the daily ceiling. Does not touch a real calendar or a publisher -- docs/32 §4.5."""
        after_et = after.astimezone(_ET)
        if channel == "linkedin":
            day = after_et.date()
            for _ in range(14):
                if day.weekday() < 5 and self.check_rate_limit(channel, day):
                    for slot_time in LINKEDIN_SLOTS:
                        candidate = dt.datetime.combine(day, slot_time, tzinfo=_ET)
                        if candidate >= after_et and not self._slot_taken(channel, candidate):
                            return candidate.astimezone(dt.UTC)
                day = day + dt.timedelta(days=1)
                after_et = dt.datetime.combine(day, dt.time(0, 0), tzinfo=_ET)
            raise RuntimeError("no LinkedIn slot found in the next 14 days")

        # bluesky / x: any 10-minute-gapped slot inside the daily window, subject to the ceiling
        day = after_et.date()
        for _ in range(14):
            if self.check_rate_limit(channel, day):
                window_start = dt.datetime.combine(day, BLUESKY_X_WINDOW[0], tzinfo=_ET)
                window_end = dt.datetime.combine(day, BLUESKY_X_WINDOW[1], tzinfo=_ET)
                candidate = max(after_et, window_start)
                while candidate <= window_end:
                    if not self._slot_taken(channel, candidate):
                        return candidate.astimezone(dt.UTC)
                    candidate += dt.timedelta(minutes=BLUESKY_X_MIN_GAP_MINUTES)
            day = day + dt.timedelta(days=1)
            after_et = dt.datetime.combine(day, dt.time(0, 0), tzinfo=_ET)
        raise RuntimeError(f"no {channel} slot found in the next 14 days")

    def _slot_taken(self, channel: str, candidate: dt.datetime) -> bool:
        gap = dt.timedelta(
            minutes=BLUESKY_X_MIN_GAP_MINUTES if channel != "linkedin" else 0
        )
        for d in self._drafts.values():
            if d.channel != channel or d.scheduled_for is None:
                continue
            if abs(d.scheduled_for - candidate) <= gap:
                return True
        return False

    def schedule(self, draft_id: str, after: dt.datetime | None = None) -> PostDraft:
        draft = self._drafts[draft_id]
        if draft.status != "approved":
            raise ValueError(f"cannot schedule a draft in status {draft.status!r}")
        slot = self.next_available_slot(draft.channel, after or self._now())
        draft.scheduled_for = slot
        draft.status = "scheduled"
        draft.updated_at = self._now()
        self._save()
        return draft

    # ------------------------------------------------------------------------- graduation (§4.6)

    def graduation_status(self, channel: str, event_type: str) -> GraduationResult:
        if channel == "linkedin":
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
                reasons=("LinkedIn never auto-publishes (docs/32 §1.4, §4.6): stays manual by decision.",),
            )

        window_start = self._now() - dt.timedelta(days=GRADUATION_MIN_WINDOW_DAYS)
        cohort = [
            d
            for d in self._drafts.values()
            if d.channel == channel and d.event_type == event_type and d.status == "published"
            and d.created_at >= window_start
        ]
        trailing_100 = sorted(cohort, key=lambda d: d.created_at)[-100:]
        edit_rate = (
            sum(1 for d in trailing_100 if d.review.edit_count > 0) / len(trailing_100)
            if trailing_100
            else None
        )
        wrong_fact = sum(1 for d in trailing_100 if d.review.reject_reason == "wrong_fact")

        reasons = []
        if len(cohort) < GRADUATION_MIN_POSTS:
            reasons.append(f"only {len(cohort)} of {GRADUATION_MIN_POSTS} required posts published in window")
        if edit_rate is not None and edit_rate > GRADUATION_MAX_EDIT_RATE:
            reasons.append(f"edit rate {edit_rate:.1%} exceeds {GRADUATION_MAX_EDIT_RATE:.0%}")
        if wrong_fact > 0:
            reasons.append(f"{wrong_fact} wrong_fact rejection(s) in the trailing 100 drafts")
        if not reasons:
            reasons.append(
                "thresholds met, but auto_publish still requires the owner's explicit "
                "config flag with a date and name (docs/32 §4.6)"
            )

        return GraduationResult(
            channel=channel,
            event_type=event_type,
            posts_in_window=len(cohort),
            window_days=GRADUATION_MIN_WINDOW_DAYS,
            edit_rate=edit_rate,
            wrong_fact_count=wrong_fact,
            policy_incidents=0,
            adapter_error_rate=None,
            eligible=False,  # never true from this store alone -- see docs/32 §4.6 and reasons
            reasons=tuple(reasons),
        )

    # ------------------------------------------------------------------------- reporting

    def cost_summary(self) -> dict[str, float]:
        totals: dict[str, float] = dict.fromkeys(CHANNELS, 0.0)
        for draft in self._drafts.values():
            if draft.status in ("scheduled", "published"):
                totals[draft.channel] += draft.cost_estimate_usd
        return totals

    def review_stats(self) -> dict[str, int]:
        drafts = list(self._drafts.values())
        return {
            "drafted": len(drafts),
            "approved": sum(1 for d in drafts if d.status in ("approved", "scheduled", "published")),
            "edited": sum(1 for d in drafts if d.review.edit_count > 0),
            "rejected": sum(1 for d in drafts if d.status == "rejected"),
            "expired": sum(1 for d in drafts if d.status == "withdrawn"),
            "wrong_fact": sum(1 for d in drafts if d.review.reject_reason == "wrong_fact"),
        }


def _content_hash(body: str, link_url: str) -> str:
    """docs/32 §4.8 content-hash guard: text minus URL and dates."""
    text = body.replace(link_url, "")
    text = re.sub(r"\d{1,2} [A-Z][a-z]{2} \d{4}", "", text)  # "12 Sep 2026"
    text = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(text.encode()).hexdigest()


def _event_from_snapshot(draft: PostDraft) -> editorial.SocialEvent:
    """Rebuild the `SocialEvent` a stored draft was rendered from, for re-validation after an
    edit. `fields_snapshot` is the JSON-safe dict `editorial.event_to_json_safe` produced."""
    data = dict(draft.fields_snapshot)
    for key in ("event_date", "deadline_date"):
        if data.get(key):
            data[key] = dt.date.fromisoformat(data[key])
    if data.get("retrieved_at"):
        data["retrieved_at"] = dt.datetime.fromisoformat(data["retrieved_at"])
    if data.get("digest_items") is not None:
        data["digest_items"] = tuple(data["digest_items"])
    return editorial.SocialEvent(**data)


def load_events_from_diff_frame(
    rows: Iterable[dict[str, Any]],
    records: dict[str, dict[str, Any]],
    *,
    source_meta: dict[str, dict[str, Any]],
    page_url_for: Callable[[dict[str, Any]], str],
    lag_days_for: Callable[[dict[str, Any]], int | None],
) -> list[editorial.SocialEvent]:
    """Adapter for `pipeline/diff.py` output rows (see `editorial.from_diff_row` for why this is
    a lossy adapter, not the target contract). `source_meta` maps `source_id` -> {name, url,
    reuse_class} from `data/sources.yaml`; `records` maps `record_id` -> the canonical row from
    `pipeline/normalize.py`'s output; `page_url_for`/`lag_days_for` are callables over one row."""
    events = []
    for row in rows:
        meta = source_meta.get(row["source_id"], {})
        event = editorial.from_diff_row(
            row,
            record=records.get(row["record_id"]),
            source_name=meta.get("name", row["source_id"]),
            source_url=meta.get("url", ""),
            reuse_class=meta.get("reuse_class", "unknown"),
            page_url=page_url_for(row),
            lag_days=lag_days_for(row),
        )
        if event is not None:
            events.append(event)
    return events
