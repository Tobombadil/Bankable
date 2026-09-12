"""Post and PostDraft models (docs/21-data-model.md §3.18 `post`, adapted for a review-queue
workflow that has no live publisher yet).

Two record shapes, matching the task brief:

- `PostDraft` is the mutable pre-publish record: everything from `drafted` through `approved`,
  `rejected`, `scheduled`, or `withdrawn` (expired). It carries the rendered body, the structured
  fields it was rendered from (for audit -- docs/32 §4.2), the attribution and disclosure text,
  the delayed-tier notice, a cost estimate, and review metadata.
- `Post` is the terminal record created when a draft is (dry-run) published or fails to publish:
  it freezes the draft's content and adds the publish outcome (`external_post_id`, `published_at`,
  `metrics`, actual `cost_usd`). In Sprint 2 every `Post` is a dry-run outcome (`dry_run=True`);
  nothing here calls a real network.

Both are plain dataclasses with `to_dict`/`from_dict` so `services.social.queue.ReviewQueue` can
persist them as JSON without a database dependency (task brief: "JSON or SQLite store").

No model identifiers appear anywhere in this module or its output (CLAUDE.md guardrail).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from typing import Any

#: docs/21 §3.18 `channel` vocabulary. Email/RSS are the owned channel (docs/32 §1.2) and are
#: out of scope for this package, which covers the three "social" adapters named in the task
#: brief (item 4): bluesky, linkedin, x.
CHANNELS: tuple[str, ...] = ("bluesky", "linkedin", "x")

#: Task brief item 1's status vocabulary, plus `withdrawn` for the queue SLA expiry rule
#: (docs/32 §4.4: "LinkedIn/X drafts older than 24 hours expire to `withdrawn`").
POST_STATUSES: tuple[str, ...] = (
    "draft",
    "approved",
    "rejected",
    "scheduled",
    "published",
    "failed",
    "withdrawn",
)

#: Reject reason codes, docs/32 §4.4.
REJECT_REASONS: tuple[str, ...] = (
    "wrong_fact",
    "not_newsworthy",
    "source_doubt",
    "style",
    "duplicate",
    "other",
)


def _iso(value: dt.datetime | dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value else None


def _parse_date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


@dataclasses.dataclass
class ValidationResult:
    """Outcome of the docs/32 §4.3 hard gates against one rendered draft."""

    passed: bool
    failures: tuple[str, ...] = ()
    checked_at: dt.datetime = dataclasses.field(
        default_factory=lambda: dt.datetime.now(dt.UTC)
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": list(self.failures),
            "checked_at": _iso(self.checked_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ValidationResult:
        return cls(
            passed=bool(data["passed"]),
            failures=tuple(data.get("failures", [])),
            checked_at=_parse_dt(data.get("checked_at")) or dt.datetime.now(dt.UTC),
        )


@dataclasses.dataclass
class ReviewMetadata:
    """Human review trail for one draft (docs/32 §4.4)."""

    drafted_at: dt.datetime = dataclasses.field(
        default_factory=lambda: dt.datetime.now(dt.UTC)
    )
    reviewed_at: dt.datetime | None = None
    reviewer: str | None = None
    decision: str | None = None  # "approved" | "rejected" | "edited" | "expired"
    reject_reason: str | None = None
    edit_count: int = 0
    last_edit_diff: str | None = None
    sla_expires_at: dt.datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "drafted_at": _iso(self.drafted_at),
            "reviewed_at": _iso(self.reviewed_at),
            "reviewer": self.reviewer,
            "decision": self.decision,
            "reject_reason": self.reject_reason,
            "edit_count": self.edit_count,
            "last_edit_diff": self.last_edit_diff,
            "sla_expires_at": _iso(self.sla_expires_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewMetadata:
        return cls(
            drafted_at=_parse_dt(data.get("drafted_at")) or dt.datetime.now(dt.UTC),
            reviewed_at=_parse_dt(data.get("reviewed_at")),
            reviewer=data.get("reviewer"),
            decision=data.get("decision"),
            reject_reason=data.get("reject_reason"),
            edit_count=int(data.get("edit_count", 0)),
            last_edit_diff=data.get("last_edit_diff"),
            sla_expires_at=_parse_dt(data.get("sla_expires_at")),
        )


@dataclasses.dataclass
class PostDraft:
    """A drafted post awaiting review (docs/21 §3.18, pre-publish states).

    `fields_snapshot` is the exact structured-field JSON the template was rendered from
    (docs/32 §4.2: "the drafting prompt receives a JSON object of those fields ... nothing
    else"), kept for audit even though no model is ever called in this package.
    """

    channel: str
    event_id: str
    event_type: str
    subject_type: str
    subject_id: str
    template_id: str
    template_version: str
    body: str
    link_url: str
    attribution_line: str
    disclosure_text: str
    fields_snapshot: dict[str, Any]
    idempotency_key: str
    dedupe_key: str
    delayed_tier_notice: str | None = None
    cost_estimate_usd: float = 0.0
    status: str = "draft"
    validation: ValidationResult | None = None
    review: ReviewMetadata = dataclasses.field(default_factory=ReviewMetadata)
    gate_checked_at: dt.datetime = dataclasses.field(
        default_factory=lambda: dt.datetime.now(dt.UTC)
    )
    scheduled_for: dt.datetime | None = None
    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)
    created_at: dt.datetime = dataclasses.field(
        default_factory=lambda: dt.datetime.now(dt.UTC)
    )
    updated_at: dt.datetime = dataclasses.field(
        default_factory=lambda: dt.datetime.now(dt.UTC)
    )

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel {self.channel!r}, must be one of {CHANNELS}")
        if self.status not in POST_STATUSES:
            raise ValueError(f"unknown status {self.status!r}, must be one of {POST_STATUSES}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "channel": self.channel,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "body": self.body,
            "link_url": self.link_url,
            "attribution_line": self.attribution_line,
            "disclosure_text": self.disclosure_text,
            "delayed_tier_notice": self.delayed_tier_notice,
            "fields_snapshot": self.fields_snapshot,
            "idempotency_key": self.idempotency_key,
            "dedupe_key": self.dedupe_key,
            "cost_estimate_usd": self.cost_estimate_usd,
            "status": self.status,
            "validation": self.validation.to_dict() if self.validation else None,
            "review": self.review.to_dict(),
            "gate_checked_at": _iso(self.gate_checked_at),
            "scheduled_for": _iso(self.scheduled_for),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PostDraft:
        return cls(
            id=data["id"],
            channel=data["channel"],
            event_id=data["event_id"],
            event_type=data["event_type"],
            subject_type=data["subject_type"],
            subject_id=data["subject_id"],
            template_id=data["template_id"],
            template_version=data["template_version"],
            body=data["body"],
            link_url=data["link_url"],
            attribution_line=data["attribution_line"],
            disclosure_text=data["disclosure_text"],
            delayed_tier_notice=data.get("delayed_tier_notice"),
            fields_snapshot=data.get("fields_snapshot", {}),
            idempotency_key=data["idempotency_key"],
            dedupe_key=data["dedupe_key"],
            cost_estimate_usd=float(data.get("cost_estimate_usd", 0.0)),
            status=data.get("status", "draft"),
            validation=ValidationResult.from_dict(data["validation"])
            if data.get("validation")
            else None,
            review=ReviewMetadata.from_dict(data["review"])
            if data.get("review")
            else ReviewMetadata(),
            gate_checked_at=_parse_dt(data.get("gate_checked_at")) or dt.datetime.now(dt.UTC),
            scheduled_for=_parse_dt(data.get("scheduled_for")),
            created_at=_parse_dt(data.get("created_at")) or dt.datetime.now(dt.UTC),
            updated_at=_parse_dt(data.get("updated_at")) or dt.datetime.now(dt.UTC),
        )

    def to_post(
        self,
        *,
        external_post_id: str | None = None,
        published_at: dt.datetime | None = None,
        cost_usd: float | None = None,
        dry_run: bool = True,
    ) -> Post:
        """Freeze this draft into a terminal `Post` record (publish outcome)."""
        return Post(
            id=uuid.uuid4().hex,
            draft_id=self.id,
            channel=self.channel,
            event_id=self.event_id,
            subject_type=self.subject_type,
            subject_id=self.subject_id,
            template_id=self.template_id,
            template_version=self.template_version,
            body=self.body,
            link_url=self.link_url,
            attribution_line=self.attribution_line,
            disclosure_text=self.disclosure_text,
            state="published" if external_post_id or not dry_run else "draft",
            auto_published=self.review.reviewer is None,
            approved_by=self.review.reviewer,
            external_post_id=external_post_id,
            published_at=published_at,
            dry_run=dry_run,
            cost_usd=cost_usd if cost_usd is not None else self.cost_estimate_usd,
        )


@dataclasses.dataclass
class Post:
    """A publish attempt's outcome (docs/21 §3.18 post-publish fields).

    In Sprint 2 every `Post` has `dry_run=True`: no publisher in this package ever sends
    anything to a real network (task brief item 4).
    """

    draft_id: str
    channel: str
    event_id: str
    subject_type: str
    subject_id: str
    template_id: str
    template_version: str
    body: str
    link_url: str
    attribution_line: str
    disclosure_text: str
    state: str = "draft"  # "published" | "failed" | "draft" (dry-run preview)
    auto_published: bool = False
    approved_by: str | None = None
    external_post_id: str | None = None
    published_at: dt.datetime | None = None
    dry_run: bool = True
    cost_usd: float = 0.0
    metrics: dict[str, Any] = dataclasses.field(
        default_factory=lambda: {"impressions": 0, "clicks": 0, "likes": 0, "reposts": 0, "fetched_at": None}
    )
    id: str = dataclasses.field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "draft_id": self.draft_id,
            "channel": self.channel,
            "event_id": self.event_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "body": self.body,
            "link_url": self.link_url,
            "attribution_line": self.attribution_line,
            "disclosure_text": self.disclosure_text,
            "state": self.state,
            "auto_published": self.auto_published,
            "approved_by": self.approved_by,
            "external_post_id": self.external_post_id,
            "published_at": _iso(self.published_at),
            "dry_run": self.dry_run,
            "cost_usd": self.cost_usd,
            "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Post:
        return cls(
            id=data.get("id", uuid.uuid4().hex),
            draft_id=data["draft_id"],
            channel=data["channel"],
            event_id=data["event_id"],
            subject_type=data["subject_type"],
            subject_id=data["subject_id"],
            template_id=data["template_id"],
            template_version=data["template_version"],
            body=data["body"],
            link_url=data["link_url"],
            attribution_line=data["attribution_line"],
            disclosure_text=data["disclosure_text"],
            state=data.get("state", "draft"),
            auto_published=bool(data.get("auto_published", False)),
            approved_by=data.get("approved_by"),
            external_post_id=data.get("external_post_id"),
            published_at=_parse_dt(data.get("published_at")),
            dry_run=bool(data.get("dry_run", True)),
            cost_usd=float(data.get("cost_usd", 0.0)),
            metrics=data.get(
                "metrics",
                {"impressions": 0, "clicks": 0, "likes": 0, "reposts": 0, "fetched_at": None},
            ),
        )
