"""`/v1/privacy/requests` and `/admin/v1/privacy-requests`: the deletion/correction route for
people named in the records the platform indexes (docs/50-audit-2026-09-18.md §3.1: "the privacy
notice promises a filer-deletion route that does not exist"; US-910; CLAUDE.md "honour deletion
requests").

`web/templates/legal/privacy.html` §3 says a deletion request "from someone named in a filing we
index" is handled as a deletion task. Before this module the only route was a `mailto:`; the admin
`DELETE /admin/v1/users/{id}` flow covers *account holders* only. This is the minimum that makes
the notice true:

- `POST /v1/privacy/requests` — public, no session, no API key. Stores `{kind: erasure |
  correction, record_public_id, contact_email, message}` as a `privacy_request` row and answers
  `202` with the request's own public id. It emails nothing (CLAUDE.md: outbound communication to
  real people is drafted by agents and sent by a human), records no IP or user agent, and does not
  confirm whether `record_public_id` exists — a request about a record the caller cannot see is
  still a request, and answering "no such record" would leak existence (docs/21 §8 item 3).
  Per-IP rate-limited through the shared `default_limiter` at the same 20-per-window shape as the
  other public unauthenticated routes (`services/api/unsubscribe_routes.py`, `auth_routes.py`).
- `GET /admin/v1/privacy-requests` and `GET /admin/v1/privacy-requests/{id}` — operator reads,
  `status` filter, cursor-paginated oldest-open-first like the task queue.
- `PATCH /admin/v1/privacy-requests/{id}` — status/notes with a `reason`; closing (`done` or
  `rejected`) clears `contact_email` **and `message`** (free text that usually names the person
  and, in the 2026-09-30 audit's test, gave their street address), and writes an audit event
  carrying only their peppered hashes (`services/api/audit.hash_identifier`; legal audit L-6).
- Every request carries `due_at = created_at + 30 days` (docs/13 §5.4 rule 6) and `overdue` (open
  or in progress past `due_at`); the admin list filters on `overdue=true`.
- `GET /admin/v1/privacy-requests/{id}/checklist` — the records that name the subject, so an
  erasure is done everywhere at once rather than on the one record the person named: the
  organisation, the proposals it sponsors, the opportunities it issues, its ownership edges and
  aliases, and proposals whose name or slug carries its name. Each item says what state it is in
  and what remains (L-6 (b)). Read-only: the actions are the existing admin routes.

The coordinator mounts `router` on `services.api.app.app` (`app.include_router(privacy_router)`)
and merges the OpenAPI paths; `tests/test_api_privacy_requests.py` builds a standalone app so it
passes before either happens.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from services.api.audit import hash_identifier, record_audit_event
from services.api.auth import AuthContext, iter_client_ip_prefix, require_admin
from services.api.common import WEB_HOST, ensure_aware, iso, utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, validation_error
from services.api.pagination import clamp_limit, paginate
from services.api.params import check_allowed, csv_param, int_param
from services.api.ratelimit import default_limiter
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
)
from services.db.models import (
    PRIVACY_REQUEST_KINDS,
    PRIVACY_REQUEST_STATUSES,
    Asset,
    AssetOwner,
    Opportunity,
    Organization,
    OrganizationAlias,
    PrivacyRequest,
    Proposal,
    privacy_request_due_at,
)
from services.ids import public_id

router = APIRouter()

_PRIVACY_REQUEST_LIMIT = 20
_MAX_MESSAGE_CHARS = 4000
#: `<prefix>_<crockford>` (`services/ids.py`): the shape of every public id the site shows.
_PUBLIC_ID_RE = re.compile(r"^[a-z]{2,8}_[0-9A-HJKMNP-TV-Z]{10,32}$")
#: Deliberately loose (one `@`, no spaces, a dot in the domain): the address is only ever used by
#: a human replying, so a stricter grammar buys nothing and rejects real addresses.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _rate_limit(request: Request) -> None:
    key = f"privacy-request:{iter_client_ip_prefix(request) or 'unknown'}"
    result = default_limiter.check(key, limit=_PRIVACY_REQUEST_LIMIT)
    if not result.allowed:
        raise ProblemError(
            "rate_limited",
            "Rate limit exceeded",
            detail=f"More than {result.limit} requests in the current window.",
            headers={"Retry-After": str(result.reset_seconds)},
        )


OPEN_STATUSES = ("open", "in_progress")


def is_overdue(row: PrivacyRequest, now: dt.datetime | None = None) -> bool:
    """Still open or in progress after its 30-day deadline (docs/13 §5.4 rule 6)."""
    if row.status not in OPEN_STATUSES or row.due_at is None:
        return False
    return ensure_aware(row.due_at) < (now or utcnow())


def serialize_privacy_request(row: PrivacyRequest, *, admin: bool) -> dict[str, Any]:
    """The public (`202`) shape carries no personal data back at all — the caller already knows
    their own address; the admin shape adds the contact and message the operator needs. Both carry
    the deadline, so the person can see when an answer is owed."""
    data: dict[str, Any] = {
        "public_id": row.public_id,
        "kind": row.kind,
        "record_public_id": row.record_public_id,
        "status": row.status,
        "created_at": iso(row.created_at),
        "due_at": iso(row.due_at),
        "overdue": is_overdue(row),
        "completed_at": iso(row.completed_at),
    }
    if admin:
        data["contact_email"] = row.contact_email
        data["message"] = row.message
    return data


def _find_request(db: Session, request_id: str) -> PrivacyRequest | None:
    return db.scalar(select(PrivacyRequest).where(PrivacyRequest.public_id == request_id))


# ------------------------------------------------------------------------------------- public
@router.post("/v1/privacy/requests", status_code=202)
def create_privacy_request(
    body: dict[str, Any],
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Any:
    _rate_limit(request)
    instance = request.url.path

    kind = body.get("kind")
    if kind not in PRIVACY_REQUEST_KINDS:
        raise validation_error("kind", f"kind must be one of {PRIVACY_REQUEST_KINDS}", instance)
    record_public_id = body.get("record_public_id")
    if not isinstance(record_public_id, str) or not _PUBLIC_ID_RE.match(record_public_id.strip()):
        raise validation_error("record_public_id", "record_public_id must be a platform public id", instance)
    contact_email = body.get("contact_email")
    if not isinstance(contact_email, str) or not _EMAIL_RE.match(contact_email.strip()):
        raise validation_error("contact_email", "contact_email must be an email address", instance)
    message = body.get("message")
    if message is not None and not isinstance(message, str):
        raise validation_error("message", "message must be a string", instance)
    if isinstance(message, str) and len(message) > _MAX_MESSAGE_CHARS:
        raise validation_error(
            "message", f"message must be at most {_MAX_MESSAGE_CHARS} characters", instance
        )

    received_at = utcnow()
    row = PrivacyRequest(
        public_id="",
        kind=kind,
        record_public_id=record_public_id.strip(),
        contact_email=contact_email.strip(),
        message=(message or "").strip() or None,
        status="open",
        created_at=received_at,
        due_at=privacy_request_due_at(received_at),
    )
    db.add(row)
    db.flush()
    row.public_id = public_id("prq", row.id)
    db.flush()
    return build_envelope(
        serialize_privacy_request(row, admin=False),
        meta=build_meta(lag_days=0, tier="public"),
        licence_summary=build_licence_summary([]),
    )


# -------------------------------------------------------------------------------------- admin
@router.get("/admin/v1/privacy-requests")
def admin_list_privacy_requests(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"limit", "cursor", "status", "kind", "overdue"})
    stmt = select(PrivacyRequest)
    if status_filter := request.query_params.get("status"):
        stmt = stmt.where(PrivacyRequest.status.in_(csv_param(status_filter)))
    if kind_filter := request.query_params.get("kind"):
        stmt = stmt.where(PrivacyRequest.kind.in_(csv_param(kind_filter)))
    overdue = request.query_params.get("overdue")
    if overdue is not None:
        if overdue not in ("true", "false"):
            raise validation_error("overdue", "overdue must be true or false", request.url.path)
        late = PrivacyRequest.status.in_(OPEN_STATUSES) & (PrivacyRequest.due_at < utcnow())
        stmt = stmt.where(late if overdue == "true" else ~late)
    limit = clamp_limit(int_param(request, "limit"))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=PrivacyRequest.created_at,
        id_column=PrivacyRequest.public_id,
        ascending=True,
        cursor=request.query_params.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    return build_list_envelope(
        [serialize_privacy_request(r, admin=True) for r in rows],
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/admin/v1/privacy-requests/{request_id}")
def admin_get_privacy_request(
    request_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    row = _find_request(db, request_id)
    if row is None:
        raise not_found(request.url.path)
    return build_envelope(
        serialize_privacy_request(row, admin=True),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


@router.patch("/admin/v1/privacy-requests/{request_id}")
def admin_update_privacy_request(
    request_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    instance = request.url.path
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise validation_error("reason", "reason is required", instance)
    row = _find_request(db, request_id)
    if row is None:
        raise not_found(instance)
    status_value = body.get("status")
    if status_value not in PRIVACY_REQUEST_STATUSES:
        raise validation_error("status", f"status must be one of {PRIVACY_REQUEST_STATUSES}", instance)
    if ctx.user is None:  # unreachable: require_admin() already refused an unauthenticated caller
        raise ProblemError("unauthenticated", "An operator session is required")

    before = {
        "status": row.status,
        "contact_email_hash": hash_identifier(row.contact_email),
        "message_hash": hash_identifier(row.message),
    }
    row.status = status_value
    closing = status_value in ("done", "rejected")
    if closing:
        row.completed_at = utcnow()
        # Both personal fields, cleared the moment they are no longer needed: the address the
        # answer went to, and the free text, which usually names the person (legal audit L-6 (a)).
        row.contact_email = None
        row.message = None
    kept = "cleared" if closing else "retained"
    record_audit_event(
        db,
        subject_type="privacy_request",
        subject_id=row.id,
        event_type="admin_edit",
        actor=ctx.user,
        reason=reason,
        before=before,
        after={"status": status_value, "contact_email": kept, "message": kept},
    )
    db.flush()
    return build_envelope(
        serialize_privacy_request(row, admin=True),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


# ------------------------------------------------------------------------------------ checklist
#: How many proposals a name search returns before the checklist says "more": a person's name in a
#: record name is rare, and a short name that matches hundreds is a search to refine, not a list.
_NAME_MATCH_LIMIT = 50


def _item(
    kind: str,
    public_id: str,
    label: str,
    *,
    url: str | None,
    state: str,
    done: bool,
    action: str,
    personal_data: bool | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": kind,
        "public_id": public_id,
        "label": label,
        "url": url,
        "state": state,
        "done": done,
        "action": action,
    }
    if personal_data is not None:
        item["personal_data"] = personal_data
    return item


def _proposal_item(p: Proposal, why: str) -> dict[str, Any]:
    pinned = "name_canonical" in (p.overrides or {})
    hidden = p.publish_state != "public"
    return _item(
        "proposal",
        p.public_id,
        p.name_canonical,
        url=f"{WEB_HOST}/proposals/{p.slug}",
        state=f"{p.publish_state}{'; name overridden' if pinned else ''}",
        done=hidden or pinned,
        action=(
            f"{why}: redact the name with PATCH /admin/v1/proposals/{p.public_id} (name_canonical) "
            "or unpublish it; the slug still carries the original name (not re-slugged)"
        ),
    )


def _organization_items(db: Session, org: Organization) -> list[dict[str, Any]]:
    items = [
        _item(
            "organization",
            org.public_id,
            org.name_canonical,
            url=f"{WEB_HOST}/organizations/{org.slug}",
            state=org.publish_state,
            done=org.publish_state != "public",
            action=(
                "unpublish with takedown: PUT /admin/v1/records/organizations/"
                f"{org.public_id}/publish-state (drops sponsor, issuer and owner embeds everywhere)"
            ),
            personal_data=bool(org.personal_data),
        )
    ]
    for alias in db.scalars(select(OrganizationAlias).where(OrganizationAlias.organization_id == org.id)):
        items.append(
            _item(
                "alias",
                org.public_id,
                alias.alias,
                url=None,
                state=f"alias from {alias.source_id}",
                done=org.publish_state != "public",
                action=(
                    "hidden with the organisation; a curated alias rule naming the person "
                    "must be removed by hand"
                ),
            )
        )
    seen: set[str] = set()
    for p in db.scalars(
        select(Proposal).where(Proposal.sponsor_org_id == org.id).order_by(Proposal.public_id)
    ):
        seen.add(p.public_id)
        items.append(_proposal_item(p, "sponsored by the subject"))
    for o in db.scalars(
        select(Opportunity).where(Opportunity.issuer_org_id == org.id).order_by(Opportunity.public_id)
    ):
        items.append(
            _item(
                "opportunity",
                o.public_id,
                o.title,
                url=f"{WEB_HOST}/opportunities/{o.slug}",
                state=o.publish_state,
                done=o.publish_state != "public" or org.publish_state != "public",
                action="issuer embed drops with the organisation takedown; check the title and text",
            )
        )
    for edge in db.scalars(select(AssetOwner).where(AssetOwner.organization_id == org.id)):
        asset = db.get(Asset, edge.asset_id)
        if asset is None:
            continue
        items.append(
            _item(
                "asset_owner",
                asset.public_id,
                f"{asset.name} ({edge.role}"
                + (f", raw name {edge.owner_name_raw})" if edge.owner_name_raw else ")"),
                url=f"{WEB_HOST}/assets/{asset.slug}",
                state=f"{edge.role} edge from {edge.source_id}",
                done=org.publish_state != "public",
                action="edge and raw owner spelling are withheld once the organisation is taken down",
            )
        )
    needles = {org.name_canonical.strip(), org.slug}
    conditions = [Proposal.name_canonical.ilike(f"%{n}%") for n in needles if len(n) >= 4]
    conditions += [Proposal.slug.ilike(f"%{org.slug}%")] if len(org.slug) >= 4 else []
    if conditions:
        matches = db.scalars(
            select(Proposal).where(or_(*conditions)).order_by(Proposal.public_id).limit(_NAME_MATCH_LIMIT + 1)
        ).all()
        for p in matches[:_NAME_MATCH_LIMIT]:
            if p.public_id not in seen:
                seen.add(p.public_id)
                items.append(_proposal_item(p, "names the subject"))
        if len(matches) > _NAME_MATCH_LIMIT:
            items.append(
                _item(
                    "search",
                    org.public_id,
                    f"more than {_NAME_MATCH_LIMIT} proposal names match",
                    url=None,
                    state="truncated",
                    done=False,
                    action="refine by hand in the admin records search",
                )
            )
    return items


def build_checklist(db: Session, record_public_id: str) -> list[dict[str, Any]]:
    """Every record that names the subject of a request, starting from the id the person gave.

    A proposal or opportunity id is widened to its sponsor or issuer; an asset id to its owners;
    an organisation id is used as is. An id that resolves to nothing yields one item saying so,
    because the person may have pasted a URL fragment or a record that has since merged."""
    items: list[dict[str, Any]] = []
    orgs: list[Organization] = []
    rid = record_public_id
    if rid.startswith("org_"):
        org = db.scalar(select(Organization).where(Organization.public_id == rid))
        if org is not None:
            orgs.append(org)
    elif rid.startswith("prop_"):
        prop = db.scalar(select(Proposal).where(Proposal.public_id == rid))
        if prop is not None:
            items.append(_proposal_item(prop, "the record named in the request"))
            if prop.sponsor_org_id is not None and (sponsor := db.get(Organization, prop.sponsor_org_id)):
                orgs.append(sponsor)
    elif rid.startswith("opp_"):
        opp = db.scalar(select(Opportunity).where(Opportunity.public_id == rid))
        if (
            opp is not None
            and opp.issuer_org_id is not None
            and (issuer := db.get(Organization, opp.issuer_org_id))
        ):
            orgs.append(issuer)
    elif rid.startswith("asset_"):
        asset = db.scalar(select(Asset).where(Asset.public_id == rid))
        if asset is not None:
            for edge in db.scalars(select(AssetOwner).where(AssetOwner.asset_id == asset.id)):
                if (owner := db.get(Organization, edge.organization_id)) is not None:
                    orgs.append(owner)
    for org in orgs:
        while org.merged_into_id is not None and (survivor := db.get(Organization, org.merged_into_id)):
            org = survivor
        for item in _organization_items(db, org):
            if not any(
                i["kind"] == item["kind"]
                and i["public_id"] == item["public_id"]
                and i["label"] == item["label"]
                for i in items
            ):
                items.append(item)
    if not items:
        items.append(
            _item(
                "unresolved",
                rid,
                "no record with this id",
                url=None,
                state="not found",
                done=False,
                action="search the records by the name in the request; the id may be mistyped or merged",
            )
        )
    return items


@router.get("/admin/v1/privacy-requests/{request_id}/checklist")
def admin_privacy_request_checklist(
    request_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    row = _find_request(db, request_id)
    if row is None:
        raise not_found(request.url.path)
    items = build_checklist(db, row.record_public_id)
    data = {
        "request": serialize_privacy_request(row, admin=True),
        "items": items,
        "remaining": sum(1 for i in items if not i["done"]),
        "notes": [
            "Sitemap: a takedown done from the admin site drops the page from the cached sitemap at once; "
            "one made directly against the API reaches it when the hourly cache next rebuilds.",
            "Backups: an erased value persists in database backups until they age out "
            "(35 days local, 14 days of R2 dailies, 8 weeks of Sunday dumps; docs/60 §8).",
        ],
    }
    return build_envelope(
        data, meta=build_meta(lag_days=0, tier="admin"), licence_summary=build_licence_summary([])
    )


__all__ = ["build_checklist", "router", "serialize_privacy_request"]
