"""Admin surface for the social review queue and channel switches (US-801..804, `docs/20` §8 item
5), admin-issued API keys (US-701, `docs/23` §5), and the three public write endpoints that feed
the task queue: project/opportunity intake (US-1001, US-1003) and "report a problem" (US-204).

Sprint 3 item 3 (coordinator-owned "admin panel tables" block, `services/db/models.py`). Mounted
by the coordinator (`router = APIRouter()`); this module never registers itself on `app.py`.

Decisions (numbered; see `services/api/admin_posts.md` for the full rationale and deferred items):

1. Vocabulary: `POST_CHANNELS`/`POST_STATES` come from `services.db.models` (the CHECK-constraint
   source of truth); `REJECT_REASONS` is the one constant imported from `services.social.models`
   — that module's dataclasses/file-queue code (`PostDraft`, `ReviewQueue`, ...) are never pulled
   in, only the tuple.
2. `POST /admin/v1/posts/{post_id}/reject`'s body is `ReasonRequest` (spec) — one `reason` string,
   not a separate `reject_reason` field. Its value is required to be one of `REJECT_REASONS` and is
   stored on `Post.reject_reason` and the audit event's `reason` verbatim (US-802 AC3's weekly
   rejection report reads that vocabulary either way).
3. `POST /admin/v1/posts/{post_id}/approve` has no request body in the spec, so there is no
   caller-supplied `reason` to require; the audit event records a fixed system reason
   (`_APPROVE_REASON`). Every other write in this module that has a `reason` field in its schema
   enforces it as non-empty (`400 validation_error`), per the task brief's audit rule.
4. `ChannelAutoPublishRequest` carries no disclosure-label *text*, only the
   `disclosure_label_confirmed` boolean gate (CLAUDE.md: automated accounts are labelled).
   Enabling a channel stores a fixed platform label (`_DEFAULT_DISCLOSURE_LABEL`); disabling clears
   it. The actual per-platform label copy is a follow-up (see the .md).
5. `channel_config` has no surrogate UUID key (its PK is the channel name); audit events for it use
   a `uuid5` derived deterministically from the channel name as `Event.subject_id`, the same
   escape hatch `docs/21` already allows for id-less rows.
6. Public write endpoints (`/v1/intake/*`, `/v1/reports`) create a `Task` only — never a `Proposal`
   /`Opportunity` row directly. `Task.pending_record` holds the validated request body verbatim;
   turning it into a real record is `adminApproveIntake`'s job (another agent's file, out of
   scope here). This matches `services/db/models.py`'s `Task` docstring ("`pending_record` holds
   the submission until approved") even though it is not yet shaped like `AdminProposal`
   /`AdminOpportunity` — the intake-review step normalises it.
7. Captcha is accepted as an opaque required string (`captcha_token`) and never verified — no
   captcha provider is wired up this sprint (see the .md "deferred"). A `website` honeypot field
   (any non-empty value rejects the submission) and a shared `intake:<ip>` bucket on
   `services.api.ratelimit.default_limiter` (5/hour) stand in for the abuse controls the spec
   otherwise assigns to captcha.
8. Channel body-length checks (US-32 §4.3 item 1) count Python string length (Unicode code
   points), not extended grapheme clusters — no grapheme-clustering library is in
   `requirements.txt`. Documented as a follow-up, not a silent gap.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid as _uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.audit import record_audit_event
from services.api.auth import AuthContext, generate_api_key, require_admin
from services.api.common import WEB_HOST, ensure_aware, iso, new_request_id, utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, validation_error
from services.api.pagination import clamp_limit, paginate
from services.api.params import check_allowed, csv_param
from services.api.pro import API_LICENCE_VERSION
from services.api.ratelimit import default_limiter
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    serialize_api_key,
    serialize_event,
)
from services.db.models import (
    POST_CHANNELS,
    POST_STATES,
    Account,
    ApiKey,
    ChannelConfig,
    Event,
    Opportunity,
    Organization,
    Post,
    Proposal,
    Task,
    User,
)
from services.ids import public_id
from services.social.models import REJECT_REASONS

router = APIRouter()

# ------------------------------------------------------------------------------------- constants
PROPOSAL_KINDS = (
    "generation",
    "storage",
    "load",
    "transmission",
    "pipeline",
    "lng",
    "nuclear",
    "ccs",
    "hydrogen",
    "other",
)
OPPORTUNITY_KINDS = (
    "rfp",
    "foa",
    "tender",
    "auction",
    "loan_program",
    "procurement_notice",
    "program",
)
LIFECYCLE_STATES = (
    "unknown",
    "announced",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "under_construction",
    "built",
    "withdrawn",
    "cancelled",
)
REPORT_ISSUE_TYPES = ("wrong_merge", "wrong_status", "wrong_sponsor", "other")

#: docs/32 §4.3 item 1. True grapheme clustering is a follow-up (decision 8 above); Python string
#: length is the interim measure.
CHANNEL_BODY_LIMITS = {"bluesky": 300, "x": 280, "linkedin": 3000}

_GATE_MAX_AGE = dt.timedelta(hours=24)
_APPROVE_REASON = "Approved via the social review queue (US-802)."
_CHANNEL_CAPABILITIES = ["create_post", "read_metrics"]  # US-804 AC1: nothing else is exposed.
_DEFAULT_DISCLOSURE_LABEL = "Automated post — see disclosure"

_JURISDICTION_RE = re.compile(r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

PRIVACY_NOTICE_URL = f"{WEB_HOST}/legal/privacy"
INTAKE_RATE_LIMIT = 5  # per hour, per IP (docs/23 §7 P-5; shared across intake + reports)


# ----------------------------------------------------------------------------------- small utils
def _require_user(ctx: AuthContext) -> User:
    """`require_admin()` already guarantees `ctx.user is not None`; this turns that into a typed
    non-`None` return for mypy strict instead of an `assert` (ruff S101 bans `assert` outside
    tests)."""
    if ctx.user is None:  # pragma: no cover — unreachable once require_admin has run
        raise ProblemError("unauthenticated", "An operator session is required")
    return ctx.user


def _parse_dt(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce_intake_rate_limit(request: Request, instance: str) -> None:
    result = default_limiter.check(f"intake:{_client_ip(request)}", limit=INTAKE_RATE_LIMIT)
    if not result.allowed:
        raise ProblemError(
            "rate_limited",
            "Rate limit exceeded",
            detail=f"More than {INTAKE_RATE_LIMIT} submissions in the current hour from this IP.",
            instance=instance,
            headers={"Retry-After": str(result.reset_seconds)},
        )


def _reject_honeypot(body: dict[str, Any], instance: str) -> None:
    if body.get("website"):
        raise validation_error("website", "spam check failed", instance)


def _validate_contact(contact: Any, instance: str) -> dict[str, str]:
    if not isinstance(contact, dict) or not contact.get("name") or not contact.get("email"):
        raise validation_error("contact", "contact.name and contact.email are required", instance)
    email = contact["email"]
    if not isinstance(email, str) or not _EMAIL_RE.match(email):
        raise validation_error("contact.email", "must be a valid email address", instance)
    return {"name": str(contact["name"]), "email": email}


def _require_field(body: dict[str, Any], field: str, instance: str) -> Any:
    value = body.get(field)
    if value in (None, ""):
        raise validation_error(field, f"{field} is required", instance)
    return value


# =============================================================================== post review queue
def _find_post(db: Session, post_id: str) -> Post | None:
    if not post_id.startswith("post_"):
        return None
    return db.scalar(select(Post).where(Post.public_id == post_id))


def _get_subject(db: Session, subject_type: str, subject_id: _uuid.UUID) -> Proposal | Opportunity | None:
    if subject_type == "proposal":
        return db.get(Proposal, subject_id)
    if subject_type == "opportunity":
        return db.get(Opportunity, subject_id)
    return None


def _subject_summary(db: Session, post: Post) -> dict[str, str]:
    subject = _get_subject(db, post.subject_type, post.subject_id)
    if isinstance(subject, Proposal):
        return {
            "public_id": subject.public_id,
            "name": subject.name_canonical,
            "url": f"{WEB_HOST}/proposals/{subject.slug}",
        }
    if isinstance(subject, Opportunity):
        return {
            "public_id": subject.public_id,
            "name": subject.title,
            "url": f"{WEB_HOST}/opportunities/{subject.slug}",
        }
    return {"public_id": str(post.subject_id), "name": "Unknown", "url": WEB_HOST}


def serialize_post(db: Session, post: Post) -> dict[str, Any]:
    subject = _subject_summary(db, post)
    event = db.get(Event, post.event_id)
    event_public_id = public_id("evt", event.id) if event else ""
    approved_by_public_id = None
    if post.approved_by_user_id is not None:
        approver = db.get(User, post.approved_by_user_id)
        approved_by_public_id = approver.public_id if approver else None
    event_data = None
    if event is not None:
        event_data = serialize_event(
            event,
            subject_public_id=subject["public_id"],
            subject_name=subject["name"],
            subject_url=subject["url"],
        )
    return {
        "post_id": post.public_id,
        "channel": post.channel,
        "event_id": event_public_id,
        "event": event_data,
        "subject_type": post.subject_type,
        "subject_id": subject["public_id"],
        "subject": subject,
        "template_id": post.template_id,
        "template_version": post.template_version,
        "body": post.body,
        "link_url": post.link_url,
        "credit_line": post.credit_line,
        "disclosure_label": post.disclosure_label,
        "state": post.state,
        "gate_checked_at": iso(post.gate_checked_at),
        "approved_by_user_id": approved_by_public_id,
        "auto_published": post.auto_published,
        "scheduled_for": iso(post.scheduled_for),
        "published_at": iso(post.published_at),
        "external_post_id": post.external_post_id,
        "reject_reason": post.reject_reason,
        "metrics": post.metrics or {},
        "cost_usd": float(post.cost_usd),
        "created_at": iso(post.created_at),
    }


def _check_gate(db: Session, post: Post, *, instance: str) -> None:
    """US-801 AC3: re-checked at approval, not trusted from draft time alone."""
    subject = _get_subject(db, post.subject_type, post.subject_id)
    if subject is None or subject.publish_state not in ("public", "api_only"):
        raise ProblemError(
            "gate_unmet",
            "Publish gate not met",
            detail="The subject record must be public or api_only to publish a post about it.",
            instance=instance,
        )
    if utcnow() - ensure_aware(post.gate_checked_at) > _GATE_MAX_AGE:
        raise ProblemError(
            "gate_unmet",
            "Gate check has expired",
            detail="gate_checked_at is older than 24 hours; the draft must be regenerated.",
            instance=instance,
        )
    post.gate_checked_at = utcnow()


def _post_envelope(data: Any) -> dict[str, Any]:
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier="admin"), licence_summary=build_licence_summary([])
    )


@router.get("/admin/v1/posts")
def admin_list_posts(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"limit", "cursor", "channel", "state", "event_type", "subject_type"})
    qp = request.query_params
    instance = request.url.path
    limit = clamp_limit(int(qp["limit"]) if "limit" in qp else None)
    stmt = select(Post)
    if channels := csv_param(qp.get("channel")):
        bad = [c for c in channels if c not in POST_CHANNELS]
        if bad:
            raise validation_error("channel", f"unknown channel(s): {bad}", instance)
        stmt = stmt.where(Post.channel.in_(channels))
    if states := csv_param(qp.get("state")):
        bad_states = [s for s in states if s not in POST_STATES]
        if bad_states:
            raise validation_error("state", f"unknown state(s): {bad_states}", instance)
        stmt = stmt.where(Post.state.in_(states))
    if subject_types := csv_param(qp.get("subject_type")):
        stmt = stmt.where(Post.subject_type.in_(subject_types))
    if event_types := csv_param(qp.get("event_type")):
        stmt = stmt.join(Event, Post.event_id == Event.id).where(Event.event_type.in_(event_types))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=Post.created_at,
        id_column=Post.id,
        ascending=True,  # "oldest draft first" (spec description)
        cursor=qp.get("cursor"),
        limit=limit,
        instance=instance,
    )
    data = [serialize_post(db, p) for p in rows]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/admin/v1/posts/{post_id}")
def admin_get_post(
    post_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    post = _find_post(db, post_id)
    if post is None:
        raise not_found(request.url.path)
    return _post_envelope(serialize_post(db, post))


@router.patch("/admin/v1/posts/{post_id}")
def admin_update_post(
    post_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    user = _require_user(ctx)
    instance = request.url.path
    post = _find_post(db, post_id)
    if post is None:
        raise not_found(instance)
    if post.state not in ("draft", "approved"):
        raise ProblemError(
            "conflict",
            "Post not editable",
            detail=f"state is {post.state!r}; only a draft or approved post can be edited.",
            instance=instance,
        )
    reason = body.get("reason")
    if not reason:
        raise validation_error("reason", "reason is required", instance)
    if "body" not in body and "scheduled_for" not in body:
        raise validation_error("body", "at least one of body or scheduled_for must be provided", instance)

    before = {"body": post.body, "scheduled_for": iso(post.scheduled_for)}
    after: dict[str, Any] = {}

    if "body" in body:
        new_body = body["body"]
        if not isinstance(new_body, str) or not new_body.strip():
            raise validation_error("body", "body must be non-empty text", instance)
        limit = CHANNEL_BODY_LIMITS.get(post.channel)
        if limit is not None and len(new_body) > limit:
            detail = f"exceeds the {post.channel} limit of {limit} characters"
            raise validation_error("body", detail, instance)
        if post.link_url not in new_body or post.credit_line not in new_body:
            raise validation_error(
                "body", "an edit cannot remove the detail-page link or the source credit line", instance
            )
        if post.disclosure_label and post.disclosure_label not in new_body:
            raise validation_error("body", "an edit cannot remove the disclosure label", instance)
        post.body = new_body
        after["body"] = new_body

    if "scheduled_for" in body:
        raw = body["scheduled_for"]
        when = _parse_dt(raw) if raw else None
        if raw and when is None:
            raise validation_error("scheduled_for", "must be an ISO 8601 datetime", instance)
        post.scheduled_for = ensure_aware(when) if when else None
        after["scheduled_for"] = iso(post.scheduled_for)

    db.flush()
    record_audit_event(
        db,
        subject_type="post",
        subject_id=post.id,
        event_type="admin_edit",
        actor=user,
        reason=reason,
        before=before,
        after=after,
    )
    return _post_envelope(serialize_post(db, post))


@router.post("/admin/v1/posts/{post_id}/approve")
def admin_approve_post(
    post_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    user = _require_user(ctx)
    instance = request.url.path
    post = _find_post(db, post_id)
    if post is None:
        raise not_found(instance)
    if post.state != "draft":
        raise ProblemError(
            "conflict",
            "Post not in draft",
            detail=f"state is {post.state!r}; only a draft can be approved.",
            instance=instance,
        )
    _check_gate(db, post, instance=instance)  # US-801 AC3, re-checked here
    before = {"state": post.state, "approved_by_user_id": None}
    post.state = "approved"
    post.approved_by_user_id = user.id
    db.flush()
    record_audit_event(
        db,
        subject_type="post",
        subject_id=post.id,
        event_type="admin_edit",
        actor=user,
        reason=_APPROVE_REASON,  # decision 3: no request body to carry a caller reason
        before=before,
        after={"state": "approved", "approved_by_user_id": user.public_id},
    )
    return _post_envelope(serialize_post(db, post))


@router.post("/admin/v1/posts/{post_id}/reject")
def admin_reject_post(
    post_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    user = _require_user(ctx)
    instance = request.url.path
    post = _find_post(db, post_id)
    if post is None:
        raise not_found(instance)
    if post.state not in ("draft", "approved", "scheduled"):
        raise ProblemError(
            "conflict",
            "Post not rejectable",
            detail=f"state is {post.state!r}.",
            instance=instance,
        )
    reason = body.get("reason")  # decision 2: ReasonRequest.reason doubles as reject_reason
    if reason not in REJECT_REASONS:
        raise validation_error("reason", f"must be one of {REJECT_REASONS}", instance)
    before = {"state": post.state, "reject_reason": post.reject_reason}
    post.state = "rejected"
    post.reject_reason = reason
    db.flush()
    record_audit_event(
        db,
        subject_type="post",
        subject_id=post.id,
        event_type="admin_edit",
        actor=user,
        reason=reason,
        before=before,
        after={"state": "rejected", "reject_reason": reason},
    )
    return _post_envelope(serialize_post(db, post))


@router.post("/admin/v1/posts/{post_id}/schedule")
def admin_schedule_post(
    post_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    user = _require_user(ctx)
    instance = request.url.path
    post = _find_post(db, post_id)
    if post is None:
        raise not_found(instance)
    if post.state != "approved":
        raise ProblemError(
            "conflict",
            "Post not approved",
            detail=f"state is {post.state!r}; only an approved post can be scheduled.",
            instance=instance,
        )
    raw = body.get("scheduled_for")
    if not raw:
        raise validation_error("scheduled_for", "scheduled_for is required", instance)
    when = _parse_dt(raw)
    if when is None:
        raise validation_error("scheduled_for", "must be an ISO 8601 datetime", instance)
    when = ensure_aware(when)
    if when <= utcnow():
        raise validation_error("scheduled_for", "must be in the future", instance)
    reason = body.get("reason")
    if not reason:
        raise validation_error("reason", "reason is required", instance)

    before = {"state": post.state, "scheduled_for": iso(post.scheduled_for)}
    post.state = "scheduled"
    post.scheduled_for = when
    db.flush()
    record_audit_event(
        db,
        subject_type="post",
        subject_id=post.id,
        event_type="admin_edit",
        actor=user,
        reason=reason,
        before=before,
        after={"state": "scheduled", "scheduled_for": iso(when)},
    )
    return _post_envelope(serialize_post(db, post))


# ============================================================================== channel switches
def _channel_subject_id(channel: str) -> _uuid.UUID:
    """`channel_config` has no surrogate UUID key (decision 5) — a deterministic uuid5 keyed on
    the channel name gives every audit event for the same channel the same `subject_id`."""
    return _uuid.uuid5(_uuid.NAMESPACE_DNS, f"channel_config:{channel}")


def _latest_channel_event_public_id(db: Session, channel: str) -> str | None:
    """The most recent `admin_edit` audit event for this channel's synthetic `subject_id`
    (`_channel_subject_id`), or `None` for a channel that has never been changed — used by both
    the PUT response (always has one, just written) and the GET listing (may have none)."""
    event = db.scalar(
        select(Event)
        .where(Event.subject_type == "channel_config", Event.subject_id == _channel_subject_id(channel))
        .order_by(Event.seq.desc())
        .limit(1)
    )
    return public_id("evt", event.id) if event else None


def serialize_channel_config(
    channel: str,
    config: ChannelConfig | None,
    *,
    changed_by_user_id: str | None,
    event_id: str | None,
) -> dict[str, Any]:
    """Shared by `PUT .../auto-publish` and `GET /admin/v1/channels` (coordinator follow-up: the
    admin UI needs to read state before toggling it) so the two never drift apart — see
    `api/fragments/channels.yaml`'s `ChannelConfig` schema. `config is None` is a channel with no
    `channel_config` row yet: the all-defaults state (`auto_publish: false`, everything else
    `null`, `review_required: true`)."""
    return {
        "channel": channel,
        "auto_publish": config.auto_publish if config else False,
        "review_required": not (config.auto_publish if config else False),
        "daily_cap": config.daily_cap if config else None,
        "budget_usd_monthly": None,
        "disclosure_label": config.disclosure_label if config else None,
        "capabilities": list(_CHANNEL_CAPABILITIES),
        "changed_by_user_id": changed_by_user_id,
        "event_id": event_id,
        "updated_at": iso(config.updated_at) if config else None,
    }


@router.get("/admin/v1/channels")
def admin_list_channels(
    db: Annotated[Session, Depends(get_db)],
    _ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    """`GET /admin/v1/channels` (coordinator follow-up, `api/fragments/channels.yaml`): one row per
    `POST_CHANNELS` entry so the admin UI can read auto-publish state before an owner toggles it —
    the spec previously had only the `PUT`. Any operator/owner may read; only an owner may write
    (`admin_set_channel_auto_publish`'s own `roles=("owner",)`)."""
    data = []
    for channel in POST_CHANNELS:
        config = db.get(ChannelConfig, channel)
        changed_by_user_id = None
        if config is not None and config.updated_by_user_id is not None:
            changed_by_user_id = _user_public_id(db, config.updated_by_user_id)
        event_id = _latest_channel_event_public_id(db, channel) if config is not None else None
        view = serialize_channel_config(
            channel, config, changed_by_user_id=changed_by_user_id, event_id=event_id
        )
        data.append(view)
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(None, None, False),
    )


@router.put("/admin/v1/channels/{channel}/auto-publish")
def admin_set_channel_auto_publish(
    channel: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin(roles=("owner",)))],
) -> Any:
    user = _require_user(ctx)
    instance = request.url.path
    if channel not in POST_CHANNELS:
        raise not_found(instance)
    auto_publish = body.get("auto_publish")
    if not isinstance(auto_publish, bool):
        raise validation_error("auto_publish", "auto_publish is required and must be a boolean", instance)
    reason = body.get("reason")
    if not reason:
        raise validation_error("reason", "reason is required", instance)
    disclosure_confirmed = bool(body.get("disclosure_label_confirmed", False))
    if auto_publish and not disclosure_confirmed:
        raise ProblemError(
            "gate_unmet",
            "Disclosure not confirmed",
            detail=(
                "Enabling auto-publish requires disclosure_label_confirmed=true "
                "(CLAUDE.md: automated accounts are labelled)."
            ),
            instance=instance,
        )

    config = db.get(ChannelConfig, channel)
    before: dict[str, Any]
    if config is None:
        before = {"auto_publish": False, "daily_cap": None, "disclosure_label": None}
        config = ChannelConfig(channel=channel, auto_publish=False)
        db.add(config)
        db.flush()
    else:
        before = {
            "auto_publish": config.auto_publish,
            "daily_cap": config.daily_cap,
            "disclosure_label": config.disclosure_label,
        }
    config.auto_publish = auto_publish
    if "daily_cap" in body:
        config.daily_cap = body["daily_cap"]
    config.disclosure_label = _DEFAULT_DISCLOSURE_LABEL if auto_publish else None  # decision 4
    config.updated_by_user_id = user.id
    db.flush()

    event = record_audit_event(
        db,
        subject_type="channel_config",
        subject_id=_channel_subject_id(channel),
        event_type="admin_edit",
        actor=user,
        reason=reason,
        before=before,
        after={
            "auto_publish": config.auto_publish,
            "daily_cap": config.daily_cap,
            "disclosure_label": config.disclosure_label,
        },
    )
    data = serialize_channel_config(
        channel, config, changed_by_user_id=user.public_id, event_id=public_id("evt", event.id)
    )
    return _post_envelope(data)


# ===================================================================================== admin keys
def _user_public_id(db: Session, user_id: _uuid.UUID) -> str:
    """`ApiKey.created_by_user_id` is `NOT NULL`, so the only caller always has a real id."""
    user = db.get(User, user_id)
    return user.public_id if user else ""


@router.get("/admin/v1/keys")
def admin_list_keys(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    _ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"limit", "cursor", "account_id", "revoked"})
    qp = request.query_params
    instance = request.url.path
    limit = clamp_limit(int(qp["limit"]) if "limit" in qp else None)
    stmt = select(ApiKey)
    revoked = qp.get("revoked", "false").strip().lower() in ("1", "true", "yes")
    stmt = stmt.where(ApiKey.revoked_at.is_not(None) if revoked else ApiKey.revoked_at.is_(None))
    if account_public_id := qp.get("account_id"):
        account = db.scalar(select(Account).where(Account.public_id == account_public_id))
        if account is None:
            raise not_found(instance)
        stmt = stmt.where(ApiKey.account_id == account.id)
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=ApiKey.created_at,
        id_column=ApiKey.id,
        ascending=False,
        cursor=qp.get("cursor"),
        limit=limit,
        instance=instance,
    )
    data = [
        serialize_api_key(
            k,
            account_public_id=k.account.public_id,
            created_by_public_id=_user_public_id(db, k.created_by_user_id),
        )
        for k in rows
    ]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@router.post("/admin/v1/keys", status_code=201)
def admin_create_key(
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    user = _require_user(ctx)
    instance = request.url.path
    name = _require_field(body, "name", instance)
    licence_version = body.get("licence_accepted_version")
    if licence_version != API_LICENCE_VERSION:
        raise validation_error(
            "licence_accepted_version",
            f"Must equal the current API licence version ({API_LICENCE_VERSION!r}).",
            instance,
        )
    account_public_id = _require_field(body, "account_id", instance)
    account = db.scalar(select(Account).where(Account.public_id == account_public_id))
    if account is None:
        raise not_found(instance)
    licence_ref = _require_field(body, "licence_acceptance_ref", instance)
    reason = _require_field(body, "reason", instance)

    requested_scopes = body.get("scopes") or ["read:public"]
    if "admin:*" in requested_scopes:
        raise validation_error("scopes", "admin:* may never be requested by an issued key", instance)
    prefix = body.get("prefix", "bk_live")
    secret, key_hash, last4 = generate_api_key(prefix)
    tier = (
        "api"
        if ("read:bulk" in requested_scopes or "write:webhooks" in requested_scopes)
        else ("pro" if "read:live" in requested_scopes else "public")
    )
    key_kwargs: dict[str, Any] = {"daily_quota": body.get("daily_quota")}
    if body.get("rate_limit_per_hour") is not None:
        key_kwargs["rate_limit_per_hour"] = body["rate_limit_per_hour"]

    key = ApiKey(
        public_id="",
        account_id=account.id,
        created_by_user_id=user.id,
        name=name,
        prefix=prefix,
        last4=last4,
        key_hash=key_hash,
        scopes=requested_scopes,
        tier=tier,
        licence_accepted_version=licence_version,
        licence_accepted_at=utcnow(),
        **key_kwargs,
    )
    db.add(key)
    db.flush()
    key.public_id = public_id("key", key.id)
    db.flush()
    record_audit_event(
        db,
        subject_type="api_key",
        subject_id=key.id,
        event_type="key_issued",
        actor=user,
        reason=reason,
        after={
            "account_id": account.public_id,
            "scopes": requested_scopes,
            "tier": tier,
            "licence_acceptance_ref": licence_ref,
        },
    )
    data = serialize_api_key(key, account_public_id=account.public_id, created_by_public_id=user.public_id)
    data["secret"] = secret
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier="admin"), licence_summary=build_licence_summary([])
    )


@router.delete("/admin/v1/keys/{key_id}", status_code=204)
def admin_revoke_key(
    key_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Response:
    user = _require_user(ctx)
    instance = request.url.path
    key = db.scalar(select(ApiKey).where(ApiKey.public_id == key_id))
    if key is None:
        raise not_found(instance)
    reason = body.get("reason")
    if not reason:
        raise validation_error("reason", "reason is required", instance)
    before = {"revoked_at": iso(key.revoked_at)}
    key.revoked_at = utcnow()
    db.flush()
    record_audit_event(
        db,
        subject_type="api_key",
        subject_id=key.id,
        event_type="key_revoked",
        actor=user,
        reason=reason,
        before=before,
        after={"revoked_at": iso(key.revoked_at)},
    )
    return Response(status_code=204)


# ============================================================================ public intake/report
def _intake_accepted_response(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.public_id,
        "status": "pending_review",
        "status_url": f"{WEB_HOST}/status/{task.public_id}",
        "privacy_notice_url": PRIVACY_NOTICE_URL,
        "request_id": new_request_id(),
    }


def _report_accepted_response(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.public_id,
        "privacy_notice_url": PRIVACY_NOTICE_URL,
        "request_id": new_request_id(),
    }


def _find_idempotent_task(db: Session, idempotency_key: str | None) -> Task | None:
    if not idempotency_key:
        return None
    return db.scalar(select(Task).where(Task.idempotency_key == idempotency_key))


def _validate_intake_proposal(body: dict[str, Any], instance: str) -> None:
    for field in ("project_name", "kind", "jurisdiction", "lifecycle_state", "sponsor_name"):
        _require_field(body, field, instance)
    if body["kind"] not in PROPOSAL_KINDS:
        raise validation_error("kind", f"must be one of {PROPOSAL_KINDS}", instance)
    if body["lifecycle_state"] not in LIFECYCLE_STATES:
        raise validation_error("lifecycle_state", f"must be one of {LIFECYCLE_STATES}", instance)
    if not _JURISDICTION_RE.match(body["jurisdiction"]):
        raise validation_error("jurisdiction", "must look like 'US' or 'US-TX'", instance)
    if body.get("consent") is not True:
        raise validation_error("consent", "consent must be accepted", instance)
    if not body.get("captcha_token"):
        raise validation_error("captcha_token", "captcha_token is required", instance)
    description = body.get("description")
    if description is not None and len(description) > 2000:
        raise validation_error("description", "must be at most 2000 characters", instance)


def _validate_intake_opportunity(body: dict[str, Any], instance: str) -> None:
    for field in ("title", "kind", "issuer_name", "jurisdiction", "url"):
        _require_field(body, field, instance)
    if body["kind"] not in OPPORTUNITY_KINDS:
        raise validation_error("kind", f"must be one of {OPPORTUNITY_KINDS}", instance)
    technologies = body.get("technologies")
    if not isinstance(technologies, list) or not technologies:
        raise validation_error("technologies", "at least one technology is required", instance)
    currency = body.get("budget_currency")
    if currency is not None and not _CURRENCY_RE.match(currency):
        raise validation_error("budget_currency", "must be a 3-letter ISO currency code", instance)
    if not str(body["url"]).startswith(("http://", "https://")):
        raise validation_error("url", "must be an http(s) URL", instance)
    if body.get("consent") is not True:
        raise validation_error("consent", "consent must be accepted", instance)
    if not body.get("captcha_token"):
        raise validation_error("captcha_token", "captcha_token is required", instance)
    summary = body.get("summary")
    if summary is not None and len(summary) > 2000:
        raise validation_error("summary", "must be at most 2000 characters", instance)


@router.post("/v1/intake/proposals", status_code=202)
def submit_intake_proposal(
    request: Request, body: dict[str, Any], db: Annotated[Session, Depends(get_db)]
) -> Any:
    instance = request.url.path
    _enforce_intake_rate_limit(request, instance)
    _reject_honeypot(body, instance)
    _validate_intake_proposal(body, instance)
    contact = _validate_contact(body.get("contact"), instance)

    idempotency_key = request.headers.get("Idempotency-Key")
    existing = _find_idempotent_task(db, idempotency_key)
    if existing is not None:
        if existing.type != "intake_proposal" or existing.pending_record != body:
            raise ProblemError(
                "conflict", "Idempotency key reused with a different request", instance=instance
            )
        return _intake_accepted_response(existing)

    task = Task(
        public_id="",
        type="intake_proposal",
        status="open",
        contact=contact,
        pending_record=body,
        public_opt_in=bool(body.get("public_opt_in", False)),
        idempotency_key=idempotency_key,
    )
    db.add(task)
    db.flush()
    task.public_id = public_id("task", task.id)
    db.flush()
    return _intake_accepted_response(task)


@router.post("/v1/intake/opportunities", status_code=202)
def submit_intake_opportunity(
    request: Request, body: dict[str, Any], db: Annotated[Session, Depends(get_db)]
) -> Any:
    instance = request.url.path
    _enforce_intake_rate_limit(request, instance)
    _reject_honeypot(body, instance)
    _validate_intake_opportunity(body, instance)
    contact = _validate_contact(body.get("contact"), instance)

    idempotency_key = request.headers.get("Idempotency-Key")
    existing = _find_idempotent_task(db, idempotency_key)
    if existing is not None:
        if existing.type != "intake_opportunity" or existing.pending_record != body:
            raise ProblemError(
                "conflict", "Idempotency key reused with a different request", instance=instance
            )
        return _intake_accepted_response(existing)

    task = Task(
        public_id="",
        type="intake_opportunity",
        status="open",
        contact=contact,
        pending_record=body,
        public_opt_in=bool(body.get("public_opt_in", False)),
        idempotency_key=idempotency_key,
    )
    db.add(task)
    db.flush()
    task.public_id = public_id("task", task.id)
    db.flush()
    return _intake_accepted_response(task)


def _resolve_reportable_subject(db: Session, value: str) -> tuple[str, _uuid.UUID] | tuple[None, None]:
    if value.startswith("prop_"):
        row = db.scalar(select(Proposal).where(Proposal.public_id == value))
        return ("proposal", row.id) if row else (None, None)
    if value.startswith("opp_"):
        opp_row = db.scalar(select(Opportunity).where(Opportunity.public_id == value))
        return ("opportunity", opp_row.id) if opp_row else (None, None)
    if value.startswith("org_"):
        org_row = db.scalar(select(Organization).where(Organization.public_id == value))
        return ("organization", org_row.id) if org_row else (None, None)
    return (None, None)


@router.post("/v1/reports", status_code=202)
def create_report(request: Request, body: dict[str, Any], db: Annotated[Session, Depends(get_db)]) -> Any:
    instance = request.url.path
    _enforce_intake_rate_limit(request, instance)
    _reject_honeypot(body, instance)

    public_id_value = _require_field(body, "public_id", instance)
    issue_type = body.get("issue_type")
    if issue_type not in REPORT_ISSUE_TYPES:
        raise validation_error("issue_type", f"must be one of {REPORT_ISSUE_TYPES}", instance)
    description = body.get("description")
    if not description or len(description) > 2000:
        raise validation_error("description", "description is required (max 2000 characters)", instance)
    email = body.get("email")
    if email and not _EMAIL_RE.match(email):
        raise validation_error("email", "must be a valid email address", instance)

    subject_type, subject_id = _resolve_reportable_subject(db, public_id_value)
    if subject_type is None or subject_id is None:
        raise not_found(instance)
    contact = {"email": email} if email else None

    idempotency_key = request.headers.get("Idempotency-Key")
    existing = _find_idempotent_task(db, idempotency_key)
    if existing is not None:
        same = (
            existing.type == "report"
            and existing.subject_id == subject_id
            and existing.issue_type == issue_type
            and existing.description == description
        )
        if not same:
            raise ProblemError(
                "conflict", "Idempotency key reused with a different request", instance=instance
            )
        return _report_accepted_response(existing)

    task = Task(
        public_id="",
        type="report",
        status="open",
        subject_type=subject_type,
        subject_id=subject_id,
        issue_type=issue_type,
        description=description,
        contact=contact,
        idempotency_key=idempotency_key,
    )
    db.add(task)
    db.flush()
    task.public_id = public_id("task", task.id)
    db.flush()
    return _report_accepted_response(task)


__all__ = ["router"]
