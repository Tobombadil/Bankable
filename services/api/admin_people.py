"""Admin surface: users, the task queue (reports, intake review, deletion requests), customers and
subscriptions (`/admin/v1`, Sprint 3 item 3; docs/10 §4.9 US-901/902/903/907/910, §4.10 US-1002,
US-204). `router = APIRouter()` is mounted by the coordinator (`services/api/app.py`); this module
never imports or edits `app.py`, `admin_sources.py`, `admin_records.py`, `admin_posts.py` or
`services/billing/*`/`services/crm/*` (CLAUDE.md "one agent per file area") — it only *imports*
`services.billing.router.subscriptions_for_account`/`_serialize_subscription` and the two ports.

Decisions (fuller versions with reasons in `services/api/admin_people.md`):

1. `Task.pending_record` is typed `AdminProposal | AdminOpportunity | null` in `api/openapi.yaml`,
   not the raw `IntakeProposalRequest`/`IntakeOpportunityRequest` shape — the intake-submission
   endpoints that would populate it are unbuilt this sprint (`x-status: planned`), so field mapping
   here reads defensively (`name_canonical` *or* `project_name`, `sponsor.public_id` *or*
   `sponsor_org_id`, ...) and accepts extra top-level convenience keys (`source_id`, `licence_id`,
   `source_url`, `retrieved_at`) that neither schema forbids (no `additionalProperties: false`).
2. The on-demand intake source (task brief: "a source with id `platform.intake`") is created as
   `intake.platform` instead: `SourceIdValue`'s pattern caps the first dotted segment at 6 lower-
   case letters and `platform` is 8 — the literal id would fail every contract check that reads it
   back. Same on-demand-creation behaviour and open licence (`platform-open`), different, spec-
   conformant literal.
3. `IntakeDecisionResponse.lead` always serialises `null`. `Lead` requires `match_id` (a `mat_...`
   match reference); an intake-approval lead has no match. The CRM writes (`upsert_company`,
   `create_lead_signal`) still happen when `create_lead: true`; only the response's `lead` field
   cannot honestly use a schema built for the US-403 match hand-off.
4. `create_lead` fires only on `decision: approve` (the request's own description: "subject the new
   record") — `link` attaches identifiers to an existing record without creating a lead signal, and
   `reject` obviously does not.
5. `add_curated_issuer` only ever touches an *opportunity* intake's issuer org; a proposal intake
   has no issuer, so the flag is a silent no-op there (documented, not a 400 — the request schema
   defaults it `true` for every intake decision).
6. `Task.subject_id`'s schema (`AnyPublicIdValue`) accepts only `prop_ | opp_ | org_ | mat_ | evt_ |
   doc_` prefixes. A `deletion_request` task's subject is a user (`usr_...`), which the pattern
   cannot express — `_task_subject_public_id` returns `null` for any `subject_type` the pattern
   does not cover rather than emit a string that fails contract validation.
7. A user's own API keys (`created_by_user_id`), not every key on their account, are revoked on
   disable/delete/redaction — deleting one member of a multi-user account must not cut the other
   members' credentials.
8. `POST /admin/v1/customers`'s `primary_contact_email` is, per its own schema description,
   "stored on the resulting user row only" — this endpoint creates that first `User` row (role
   `member`, no password: an invitation/reset flow is out of scope this sprint) when the field is
   given, and never writes the address onto `Account`.
9. Organisation resolution for intake approval defaults `type` to `developer` for a proposal
   sponsor and `other` for an opportunity issuer (the vocab has no safe generic "issuer" bucket);
   `country` comes from the two-letter prefix of `jurisdiction`, else `US` (every source in this
   sprint is US-only).
10. Every CRM/billing adapter call in this module runs inside the request's one DB session
    (`services/api/deps.py` `get_db`): raising past `SorUnavailable`/`SorRejected` rolls back
    whatever this request already wrote (a new proposal, a task update), matching the deletion-task
    rule ("nothing half-applied") without needing bespoke transaction handling per route.
11. `matches_computed` on `POST .../approve-intake` is always `0` — the US-401 rule engine is a
    different sprint's area; nothing here recomputes matches, and the field says so honestly rather
    than fabricating a count.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from services.alerts.suppression import suppress
from services.api.audit import hash_identifier, record_audit_event
from services.api.auth import AuthContext, require_admin, revoke_session
from services.api.common import WEB_HOST, iso, utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, validation_error
from services.api.pagination import DEFAULT_LIMIT, clamp_limit, paginate
from services.api.params import check_allowed, csv_param
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    serialize_account,
    serialize_api_key,
    serialize_opportunity,
    serialize_proposal,
    serialize_user,
)
from services.billing.entitlement import apply_entitlement_change
from services.billing.router import _serialize_subscription, subscriptions_for_account
from services.db.models import (
    ACCOUNT_ENTITLEMENT_SOURCES,
    ACCOUNT_KINDS,
    LIFECYCLE_STATES,
    RECORD_PUBLISH_STATES,
    TASK_STATUSES,
    USER_ROLES,
    Account,
    ApiKey,
    Licence,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    SavedSearch,
    Source,
    Subscription,
    Task,
    User,
    UserSession,
)
from services.ids import _CROCKFORD, public_id, slugify
from services.sor.ports import (
    PLAN_TIERS,
    BillingPort,
    CompanyUpsert,
    CrmPort,
    EntitlementChange,
    LeadSignal,
    SorRejected,
    SorUnavailable,
    SubscriptionCreate,
    entitlement_for,
)
from services.sor.wiring import get_billing_port, get_crm_port

router = APIRouter()

#: Free-mail domains a `CustomerCreate.primary_contact_email` must not resolve to when falling back
#: from an unset organisation website (decision, `services/api/admin_people.md`) — small and
#: deliberately not exhaustive; a false negative just means the CRM write is skipped, not wrong.
_FREE_MAIL_DOMAINS = frozenset(
    {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com", "protonmail.com"}
)

_INTAKE_SOURCE_ID = "intake.platform"  # decision 2
_INTAKE_LICENCE_ID = "platform-open"

_TASK_SUBJECT_PREFIX = {
    "proposal": "prop",
    "opportunity": "opp",
    "organization": "org",
    "match": "mat",
    "document": "doc",
}


# ------------------------------------------------------------------------------------- small helpers
def _require_reason(body: dict[str, Any], request: Request, *, min_length: int = 1) -> str:
    reason = body.get("reason")
    if not isinstance(reason, str) or len(reason.strip()) < min_length:
        raise validation_error("reason", "reason is required", request.url.path)
    return reason


def _decode_public_id(value: str) -> _uuid.UUID | None:
    """Internal uuid behind any `<prefix>_<crockford>` public id, `None` if malformed — the same
    scheme `services/crm/router.py`'s `_find_match_by_public_id` uses, generalised to any prefix
    since `Task.subject_id`/`assignee_user_id`/`account_id` filters all need it."""
    if "_" not in value:
        return None
    digits = value.split("_", 1)[1]
    try:
        n = 0
        for ch in digits:
            n = n * 32 + _CROCKFORD.index(ch)
        return _uuid.UUID(int=n)
    except (ValueError, OverflowError):
        return None


def _parse_iso_datetime(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _parse_iso_date(value: Any) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _dict_field(source: dict[str, Any], key: str) -> dict[str, Any]:
    """`source.get(key)` narrowed to a `dict`, `{}` otherwise — a bare
    `x if isinstance(x, dict) else {}` ternary re-evaluates `source.get(key)` for the condition and
    the branch separately, so mypy cannot narrow the branch's type; binding it here first fixes
    that (same reasoning for `_list_field` below)."""
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def _list_field(source: dict[str, Any], key: str) -> list[Any]:
    value = source.get(key)
    return value if isinstance(value, list) else []


def _country_from_jurisdiction(jurisdiction: str | None) -> str:
    if jurisdiction and len(jurisdiction) >= 2 and jurisdiction[:2].isalpha():
        return jurisdiction[:2].upper()
    return "US"


def _unique_slug(db: Session, column: InstrumentedAttribute[str], base: str) -> str:
    base = base or "record"
    slug = base
    n = 2
    while db.scalar(select(column).where(column == slug)) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _normalise_domain(website: str | None) -> str | None:
    """Same idea as `services/crm/router.py`'s `_normalise_domain` (docs/34 §5 "upsert by primary
    domain"): strip scheme, `www.`, path and port; lowercase; `None` if unusable."""
    if not website:
        return None
    candidate = website.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"//{candidate}"
    host = urlparse(candidate).netloc
    host = host.rsplit("@", 1)[-1]
    host = host.split(":", 1)[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _domain_from_email(email: str) -> str | None:
    if "@" not in email:
        return None
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if not domain or domain in _FREE_MAIL_DOMAINS:
        return None
    return domain


# --------------------------------------------------------------------------------------- lookups
def _find_user(db: Session, user_id: str) -> User | None:
    return db.scalar(select(User).where(User.public_id == user_id))


def _find_task(db: Session, task_id: str) -> Task | None:
    return db.scalar(select(Task).where(Task.public_id == task_id))


def _find_account(db: Session, account_id: str) -> Account | None:
    return db.scalar(select(Account).where(Account.public_id == account_id))


def _subscription_status_map(db: Session, account_ids: set[Any]) -> dict[Any, str]:
    """Most-recently-mirrored subscription's `status` per account (task brief: "subscription status
    ... read from the Subscription mirror"), not a live adapter call."""
    if not account_ids:
        return {}
    rows = db.scalars(select(Subscription).where(Subscription.account_id.in_(account_ids))).all()
    best: dict[Any, Subscription] = {}
    for row in rows:
        current = best.get(row.account_id)
        if current is None or row.mirrored_at > current.mirrored_at:
            best[row.account_id] = row
    return {account_id: row.status for account_id, row in best.items()}


def _revoke_user_api_keys(db: Session, user: User, *, now: dt.datetime) -> None:
    """Decision 7: only the keys this user created, not the whole account's."""
    keys = db.scalars(
        select(ApiKey).where(ApiKey.created_by_user_id == user.id, ApiKey.revoked_at.is_(None))
    ).all()
    for key in keys:
        key.revoked_at = now
    db.flush()


def _revoke_user_sessions(db: Session, user: User) -> None:
    sessions = db.scalars(
        select(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    ).all()
    for session_row in sessions:
        revoke_session(db, session_row)


# --------------------------------------------------------------------------------- serialization
def serialize_user_admin_view(
    user: User, account: Account, subscription_status: str | None
) -> dict[str, Any]:
    out = serialize_user(user, account_public_id=account.public_id)
    out["entitlement"] = account.entitlement
    out["subscription_status"] = subscription_status
    out["account_name"] = account.name
    return out


def _task_subject_public_id(task: Task) -> str | None:
    if task.subject_id is None or task.subject_type is None:
        return None
    prefix = _TASK_SUBJECT_PREFIX.get(task.subject_type)  # decision 6
    if prefix is None:
        return None
    return public_id(prefix, task.subject_id)


def serialize_task(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.public_id,
        "type": task.type,
        "subject_type": task.subject_type,
        "subject_id": _task_subject_public_id(task),
        "status": task.status,
        "assignee_user_id": public_id("usr", task.assignee_user_id) if task.assignee_user_id else None,
        "notes": task.notes,
        "issue_type": task.issue_type,
        "description": task.description,
        "contact": task.contact,
        "pending_record": task.pending_record,
        "resolver_suggestions": task.resolver_suggestions or [],
        "due_at": iso(task.due_at),
        "public_opt_in": task.public_opt_in,
        "audit_event_ids": task.audit_event_ids or [],
        "created_at": iso(task.created_at),
        "updated_at": iso(task.updated_at),
        "completed_at": iso(task.completed_at),
    }


def _serialize_admin_proposal(proposal: Proposal) -> dict[str, Any]:
    out = serialize_proposal(proposal)
    out["published_at"] = iso(proposal.published_at)
    out["public_at"] = iso(proposal.public_at)
    out["publish_state"] = proposal.publish_state
    out["overrides"] = proposal.overrides or {}
    out["last_admin_event_id"] = None
    return out


def _serialize_admin_opportunity(opportunity: Opportunity) -> dict[str, Any]:
    out = serialize_opportunity(opportunity)
    out["published_at"] = iso(opportunity.published_at)
    out["public_at"] = iso(opportunity.public_at)
    out["publish_state"] = opportunity.publish_state
    out["overrides"] = opportunity.overrides or {}
    out["last_admin_event_id"] = None
    return out


def _serialize_customer(db: Session, account: Account, *, detail: bool) -> dict[str, Any]:
    organization = db.get(Organization, account.organization_id) if account.organization_id else None
    account_view = serialize_account(account, organization=organization)
    account_view["sor_ref"] = account.sor_ref
    account_view["billing_ref"] = account.billing_ref
    data: dict[str, Any] = {
        "account": account_view,
        "subscriptions": subscriptions_for_account(db, account),
        "sor": {
            "kind": account.sor_kind or "",
            "ref": account.sor_ref or "",
            "company_name": None,
            "owner": None,
            "lifecycle_stage": None,
            "fetched_at": iso(account.entitlement_checked_at),
            "stale": account.entitlement_stale,
        },
        # docs/20 §8 item 6 calls for an "open in CRM" link; no Attio workspace URL scheme is fixed
        # yet this sprint (`docs/34` records only object/list ids, no vendor UI URL pattern) —
        # `null` rather than guessing a link that would 404 (decision, admin_people.md).
        "open_in_crm_url": None,
    }
    if detail:
        users = db.scalars(select(User).where(User.account_id == account.id)).all()
        sub_status = _subscription_status_map(db, {account.id}).get(account.id)
        data["users"] = [serialize_user_admin_view(u, account, sub_status) for u in users]
        keys = db.scalars(select(ApiKey).where(ApiKey.account_id == account.id)).all()
        creators: dict[Any, str] = {}
        if keys:
            creator_ids = {k.created_by_user_id for k in keys}
            creators = {
                u.id: u.public_id for u in db.scalars(select(User).where(User.id.in_(creator_ids))).all()
            }
        data["keys"] = [
            serialize_api_key(
                k,
                account_public_id=account.public_id,
                created_by_public_id=creators.get(k.created_by_user_id, ""),
            )
            for k in keys
        ]
    return data


# ------------------------------------------------------------------------------------- org lookup
def _resolve_org(
    db: Session,
    request: Request,
    *,
    public_id_value: Any,
    name: Any,
    default_type: str,
    jurisdiction: str | None,
    field_name: str,
) -> Organization | None:
    if isinstance(public_id_value, str) and public_id_value:
        org = db.scalar(select(Organization).where(Organization.public_id == public_id_value))
        if org is None:
            raise validation_error(field_name, f"no organization {public_id_value!r}", request.url.path)
        return org
    if not isinstance(name, str) or not name.strip():
        return None
    normalised = name.strip().lower()
    org = db.scalar(select(Organization).where(Organization.name_normalised == normalised))
    if org is not None:
        return org
    org = Organization(
        public_id="",
        slug=_unique_slug(db, Organization.slug, slugify(name)),
        name_canonical=name.strip(),
        name_normalised=normalised,
        type=default_type,
        country=_country_from_jurisdiction(jurisdiction),
    )
    db.add(org)
    db.flush()
    org.public_id = public_id("org", org.id)
    db.flush()
    return org


# --------------------------------------------------------------------------------- intake source
def _get_or_create_intake_source(db: Session) -> Source:
    source = db.get(Source, _INTAKE_SOURCE_ID)
    if source is not None:
        return source
    licence = db.get(Licence, _INTAKE_LICENCE_ID)
    if licence is None:
        licence = Licence(
            id=_INTAKE_LICENCE_ID,
            name="Platform intake submissions",
            reuse_class="open",
            attribution_required=False,
            allows_derived_publication=True,
            allows_raw_publication=True,
            allows_api_redistribution=True,
            allows_bulk_export=True,
            allows_commercial_use=True,
            gate_flag=False,
            classified_by="platform",
        )
        db.add(licence)
        db.flush()
    source = Source(
        id=_INTAKE_SOURCE_ID,
        name="Platform intake submissions",
        category="registry",
        url=f"{WEB_HOST}/submit",
        access="html",
        cadence="continuous",
        licence_id=licence.id,
        publish_state="public",
        implemented=True,
        manifest_version="intake-1",
    )
    db.add(source)
    db.flush()
    return source


def _resolve_intake_source(db: Session, pending: dict[str, Any]) -> tuple[str, Licence, str, dt.datetime]:
    """`(source_id, licence, source_url, retrieved_at)` — the pending record's own flat
    `source_id`/`licence_id` (decision 1's convenience keys) if both resolve to real rows, else the
    on-demand `intake.platform` source (decision 2). Returns the `Licence` row itself (not just its
    id) so callers never need to re-fetch it, which is always found: either read back off an
    existing row here, or just created by `_get_or_create_intake_source`."""
    sid, lid = pending.get("source_id"), pending.get("licence_id")
    if isinstance(sid, str) and isinstance(lid, str):
        source = db.get(Source, sid)
        licence = db.get(Licence, lid)
        if source is not None and licence is not None:
            source_url_raw = pending.get("source_url")
            url = source_url_raw if isinstance(source_url_raw, str) else source.url
            retrieved_at = _parse_iso_datetime(pending.get("retrieved_at")) or utcnow()
            return sid, licence, url, retrieved_at
    source = _get_or_create_intake_source(db)
    return source.id, source.licence, source.url, utcnow()


# ---------------------------------------------------------------------- record creation from intake
def _create_proposal_from_pending(
    db: Session, request: Request, pending: dict[str, Any], *, publish_state: str
) -> tuple[Proposal, Organization | None]:
    name = pending.get("name_canonical") or pending.get("project_name")
    if not isinstance(name, str) or not name.strip():
        raise validation_error("pending_record", "pending record is missing a project name", request.url.path)
    jurisdiction = pending.get("jurisdiction") if isinstance(pending.get("jurisdiction"), str) else "US"
    lifecycle_state = pending.get("lifecycle_state")
    if lifecycle_state not in LIFECYCLE_STATES:
        lifecycle_state = "unknown"
    identifiers = _dict_field(pending, "identifiers")
    sponsor_ref = _dict_field(pending, "sponsor")
    sponsor_org = _resolve_org(
        db,
        request,
        public_id_value=sponsor_ref.get("public_id") or pending.get("sponsor_org_id"),
        name=sponsor_ref.get("name_canonical") or pending.get("sponsor_name"),
        default_type="developer",  # decision 9
        jurisdiction=jurisdiction,
        field_name="sponsor_org_id",
    )
    source_id, licence, source_url, retrieved_at = _resolve_intake_source(db, pending)

    now = utcnow()
    proposal = Proposal(
        public_id="",
        slug="",
        kind=pending.get("kind") if isinstance(pending.get("kind"), str) else "other",
        name_canonical=name.strip(),
        sponsor_org_id=sponsor_org.id if sponsor_org else None,
        technology=pending.get("technology") if isinstance(pending.get("technology"), str) else None,
        technology_raw=pending.get("technology_raw")
        if isinstance(pending.get("technology_raw"), str)
        else None,
        capacity_mw=pending.get("capacity_mw"),
        storage_mwh=pending.get("storage_mwh"),
        jurisdiction=jurisdiction,
        iso=pending.get("iso") if isinstance(pending.get("iso"), str) else None,
        lifecycle_state=lifecycle_state,
        identifiers=identifiers,
        publish_state=publish_state,
        published_at=now if publish_state in ("api_only", "public") else None,
        public_at=now if publish_state == "public" else None,
        min_reuse_class=licence.reuse_class,
        source_count=1,
        created_by="user",
    )
    db.add(proposal)
    db.flush()
    proposal.public_id = public_id("prop", proposal.id)
    proposal.slug = _unique_slug(db, Proposal.slug, slugify(name))
    db.flush()
    link = ProposalSource(
        proposal_id=proposal.id,
        source_id=source_id,
        source_record_id=proposal.public_id,
        source_url=source_url,
        retrieved_at=retrieved_at,
        licence_id=licence.id,
        raw={},
        normalised={},
        first_seen=now,
        last_seen=now,
        link_method="user",
    )
    db.add(link)
    db.flush()
    return proposal, sponsor_org


def _create_opportunity_from_pending(
    db: Session, request: Request, pending: dict[str, Any], *, publish_state: str
) -> tuple[Opportunity, Organization | None]:
    title = pending.get("title") or pending.get("name_canonical")
    if not isinstance(title, str) or not title.strip():
        raise validation_error("pending_record", "pending record is missing a title", request.url.path)
    jurisdiction = pending.get("jurisdiction") if isinstance(pending.get("jurisdiction"), str) else "US"
    technologies = _list_field(pending, "technologies")
    identifiers = _dict_field(pending, "identifiers")
    issuer_ref = _dict_field(pending, "issuer")
    issuer_org = _resolve_org(
        db,
        request,
        public_id_value=issuer_ref.get("public_id") or pending.get("issuer_org_id"),
        name=issuer_ref.get("name_canonical") or pending.get("issuer_name"),
        default_type="other",  # decision 9
        jurisdiction=jurisdiction,
        field_name="issuer_org_id",
    )
    source_id, licence, source_url, retrieved_at = _resolve_intake_source(db, pending)

    now = utcnow()
    opportunity = Opportunity(
        public_id="",
        slug="",
        kind=pending.get("kind") if isinstance(pending.get("kind"), str) else "other",
        issuer_org_id=issuer_org.id if issuer_org else None,
        title=title.strip(),
        summary=pending.get("summary") if isinstance(pending.get("summary"), str) else None,
        jurisdiction=jurisdiction,
        technologies=[t for t in technologies if isinstance(t, str)],
        capacity_sought_mw=pending.get("capacity_sought_mw"),
        budget_amount=pending.get("budget_amount"),
        budget_currency=pending.get("budget_currency")
        if isinstance(pending.get("budget_currency"), str)
        else None,
        open_at=_parse_iso_date(pending.get("open_at")),
        due_at=_parse_iso_datetime(pending.get("due_at")),
        status="open",
        identifiers=identifiers,
        publish_state=publish_state,
        published_at=now if publish_state in ("api_only", "public") else None,
        public_at=now if publish_state == "public" else None,
        min_reuse_class=licence.reuse_class,
        source_count=1,
        created_by="user",
    )
    db.add(opportunity)
    db.flush()
    opportunity.public_id = public_id("opp", opportunity.id)
    opportunity.slug = _unique_slug(db, Opportunity.slug, slugify(title))
    db.flush()
    link = OpportunitySource(
        opportunity_id=opportunity.id,
        source_id=source_id,
        source_record_id=opportunity.public_id,
        source_url=source_url,
        retrieved_at=retrieved_at,
        licence_id=licence.id,
        raw={},
        normalised={},
        first_seen=now,
        last_seen=now,
        link_method="user",
    )
    db.add(link)
    db.flush()
    return opportunity, issuer_org


# ============================================================================================ users
@router.get("/admin/v1/users")
def admin_list_users(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"limit", "cursor", "q", "role", "status", "account_id"})
    stmt = select(User)

    if q := request.query_params.get("q"):
        like = f"%{q}%"
        stmt = stmt.where(or_(User.email.ilike(like), User.name.ilike(like)))
    if role := request.query_params.get("role"):
        stmt = stmt.where(User.role.in_(csv_param(role)))
    if status_filter := request.query_params.get("status"):
        stmt = stmt.where(User.status.in_(csv_param(status_filter)))
    if account_id := request.query_params.get("account_id"):
        account = _find_account(db, account_id)
        if account is None:
            return build_list_envelope(
                [],
                meta=build_meta(lag_days=0, tier="admin"),
                licence_summary=build_licence_summary([]),
                page=build_page(None, None, False),
            )
        stmt = stmt.where(User.account_id == account.id)

    limit_raw = request.query_params.get("limit")
    limit = clamp_limit(int(limit_raw)) if limit_raw is not None else DEFAULT_LIMIT
    cursor = request.query_params.get("cursor")
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=User.created_at,
        id_column=User.public_id,
        ascending=False,
        cursor=cursor,
        limit=limit,
        instance=request.url.path,
    )
    accounts_by_id = {}
    if rows:
        account_ids = {u.account_id for u in rows}
        accounts_by_id = {
            a.id: a for a in db.scalars(select(Account).where(Account.id.in_(account_ids))).all()
        }
    sub_status = _subscription_status_map(db, set(accounts_by_id))
    data = [
        serialize_user_admin_view(u, accounts_by_id[u.account_id], sub_status.get(u.account_id)) for u in rows
    ]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/admin/v1/users/{user_id}")
def admin_get_user(
    user_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    user = _find_user(db, user_id)
    if user is None:
        raise not_found(request.url.path)
    sub_status = _subscription_status_map(db, {user.account_id}).get(user.account_id)
    return build_envelope(
        serialize_user_admin_view(user, user.account, sub_status),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


@router.patch("/admin/v1/users/{user_id}")
def admin_update_user(
    user_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    reason = _require_reason(body, request)
    role = body.get("role")
    status_value = body.get("status")
    if role is None and status_value is None:
        raise validation_error("role", "role or status is required", request.url.path)

    user = _find_user(db, user_id)
    if user is None:
        raise not_found(request.url.path)

    if role is not None:
        if role not in USER_ROLES:
            raise validation_error("role", f"role must be one of {USER_ROLES}", request.url.path)
        if role == "owner" and ctx.user is not None and ctx.user.role != "owner":
            raise ProblemError("forbidden_tier", "Only an owner may grant the owner role")
    if status_value is not None and status_value not in ("active", "disabled"):
        raise validation_error("status", "status must be 'active' or 'disabled'", request.url.path)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    if role is not None and role != user.role:
        before["role"] = user.role
        user.role = role
        after["role"] = role
        if role in ("operator", "owner"):
            user.mfa_enforced = True
    if status_value is not None and status_value != user.status:
        before["status"] = user.status
        user.status = status_value
        after["status"] = status_value
        if status_value == "disabled":
            _revoke_user_sessions(db, user)

    if before and ctx.user is not None:
        # The event id is not surfaced on `User` (unlike `Task.audit_event_ids`); it still lands
        # in the append-only audit trail (US-901 AC2).
        record_audit_event(
            db,
            subject_type="user",
            subject_id=user.id,
            event_type="admin_edit",
            actor=ctx.user,
            reason=reason,
            before=before,
            after=after,
        )
    db.flush()

    sub_status = _subscription_status_map(db, {user.account_id}).get(user.account_id)
    return build_envelope(
        serialize_user_admin_view(user, user.account, sub_status),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


@router.delete("/admin/v1/users/{user_id}", status_code=202)
def admin_delete_user(
    user_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    reason = _require_reason(body, request, min_length=3)  # ReasonRequest minLength: 3
    user = _find_user(db, user_id)
    if user is None:
        raise not_found(request.url.path)

    task = Task(
        public_id="",
        type="deletion_request",
        subject_type="user",
        subject_id=user.id,
        status="open",
        due_at=utcnow() + dt.timedelta(days=30),
        created_by_user_id=ctx.user.id if ctx.user is not None else None,
    )
    db.add(task)
    db.flush()
    task.public_id = public_id("task", task.id)

    before = {"status": user.status}
    user.status = "disabled"
    _revoke_user_sessions(db, user)
    _revoke_user_api_keys(db, user, now=utcnow())
    # The request itself is the signal to stop writing to the address (docs/13 §4 CASL: an
    # unsubscribe is actioned within 10 business days; here, immediately): the user's saved
    # searches are paused so no digest is built during the 30-day window, and the address goes
    # on the suppression store now rather than at completion. Neither the email nor the name is
    # written to the audit event — the log is append-only (`services/api/audit.py`).
    for search in db.scalars(select(SavedSearch).where(SavedSearch.user_id == user.id)).all():
        search.status = "paused"
    suppress(db, user.email, "erasure")

    if ctx.user is None:  # unreachable: require_admin() already refused an unauthenticated caller
        raise ProblemError("unauthenticated", "An operator session is required")
    event = record_audit_event(
        db,
        subject_type="user",
        subject_id=user.id,
        event_type="admin_edit",
        actor=ctx.user,
        reason=reason,
        before=before,
        after={"status": "disabled", "deletion_task_id": task.public_id},
    )
    task.audit_event_ids = [public_id("evt", event.id)]
    db.flush()

    return build_envelope(
        serialize_task(task),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


# ============================================================================================ tasks
@router.get("/admin/v1/tasks")
def admin_list_tasks(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(
        request, {"limit", "cursor", "type", "status", "assignee_user_id", "subject_type", "subject_id"}
    )
    stmt = select(Task)

    if type_filter := request.query_params.get("type"):
        stmt = stmt.where(Task.type.in_(csv_param(type_filter)))
    if status_filter := request.query_params.get("status"):
        stmt = stmt.where(Task.status.in_(csv_param(status_filter)))
    if assignee_id := request.query_params.get("assignee_user_id"):
        assignee = _find_user(db, assignee_id)
        if assignee is None:
            return build_list_envelope(
                [],
                meta=build_meta(lag_days=0, tier="admin"),
                licence_summary=build_licence_summary([]),
                page=build_page(None, None, False),
            )
        stmt = stmt.where(Task.assignee_user_id == assignee.id)
    if subject_type := request.query_params.get("subject_type"):
        stmt = stmt.where(Task.subject_type.in_(csv_param(subject_type)))
    if subject_id := request.query_params.get("subject_id"):
        decoded = _decode_public_id(subject_id)
        if decoded is None:
            return build_list_envelope(
                [],
                meta=build_meta(lag_days=0, tier="admin"),
                licence_summary=build_licence_summary([]),
                page=build_page(None, None, False),
            )
        stmt = stmt.where(Task.subject_id == decoded)

    limit_raw = request.query_params.get("limit")
    limit = clamp_limit(int(limit_raw)) if limit_raw is not None else DEFAULT_LIMIT
    cursor = request.query_params.get("cursor")
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=Task.created_at,
        id_column=Task.public_id,
        ascending=True,  # "Tasks, oldest open first" (api/openapi.yaml adminListTasks)
        cursor=cursor,
        limit=limit,
        instance=request.url.path,
    )
    return build_list_envelope(
        [serialize_task(t) for t in rows],
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@router.get("/admin/v1/tasks/{task_id}")
def admin_get_task(
    task_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    task = _find_task(db, task_id)
    if task is None:
        raise not_found(request.url.path)
    return build_envelope(
        serialize_task(task),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


#: The reserved-TLD tombstone an erased user's `email` becomes (RFC 2606 `.invalid`): a
#: non-deliverable, non-personal value built from the *peppered* hash, so an operator can still
#: answer "was this address one of ours?" by hashing a candidate, without the log or the row
#: holding the address (docs/50-audit-2026-09-18.md §3.1).
_ERASED_EMAIL_DOMAIN = "erased.invalid"


def _tombstone_email(email: str | None) -> str | None:
    digest = hash_identifier(email)
    return f"erased-{digest[:16]}@{_ERASED_EMAIL_DOMAIN}" if digest else None


def _cancel_billing_for_erased_user(db: Session, user: User, billing: BillingPort) -> dict[str, Any]:
    """Cancels the erased user's subscriptions through the billing port — but only for a
    `personal` account, whose one member is the person being erased. An `organization`
    account's subscription belongs to the organisation and outlives any one member (docs/21
    §3.13/§3.14). `services.sor.ports.BillingPort` has no cancel operation yet (ADR 0006's
    port surface: checkout, portal, create, get, invoices, webhook), so the call is made
    through `cancel_subscription(ref=...)` when the adapter provides it and recorded as pending
    otherwise — never silently skipped. The local `subscription` mirror is *not* edited here:
    it is written only from the provider's webhook (`services/billing/entitlement.py`)."""
    account = db.get(Account, user.account_id)
    if account is None or account.kind != "personal":
        return {"billing": "not_applicable"}
    active = [
        s
        for s in db.scalars(select(Subscription).where(Subscription.account_id == account.id)).all()
        if s.status in ("trialing", "active", "past_due", "paused")
    ]
    if not active:
        return {"billing": "no_active_subscription"}
    cancel = getattr(billing, "cancel_subscription", None)
    if cancel is None:
        return {
            "billing": "cancellation_pending",
            "subscription_refs": [s.sor_ref for s in active],
            "detail": "billing port has no cancel_subscription operation",
        }
    cancelled = [str(cancel(ref=s.sor_ref)) for s in active]
    return {"billing": "cancelled", "subscription_refs": cancelled}


def _complete_deletion_task(
    db: Session,
    task: Task,
    request: Request,
    ctx: AuthContext,
    *,
    reason: str,
    crm: CrmPort,
    billing: BillingPort,
) -> None:
    """The erasure itself (US-910; docs/50-audit-2026-09-18.md §3.1). Order matters: the CRM
    deletion goes first because it needs the real address and can fail (the task then stays open
    with nothing changed); then the row is anonymised — email to a tombstone, name and password
    null, sessions and keys revoked, saved searches paused so no digest is ever built for it,
    the address written to the suppression store, the personal account's subscriptions cancelled
    through the billing port — and only then is the audit event written, carrying *hashes* of
    the identifiers (`hash_identifier`), never the values, because the log is append-only. The
    task's `contact` (the one place a deletion request may hold a contact) is nulled with it."""
    if task.subject_type != "user" or task.subject_id is None:
        raise ProblemError("conflict", "Deletion task has no user subject", instance=request.url.path)
    user = db.get(User, task.subject_id)
    if user is None:
        raise not_found(request.url.path)

    original_email = user.email
    try:
        crm.request_personal_data_deletion(email=original_email or "", reason=reason)
    except SorUnavailable as exc:
        raise ProblemError(
            "sor_unavailable",
            "CRM adapter unavailable",
            detail="The CRM could not be reached; the task stays open.",
            instance=request.url.path,
        ) from exc

    before = {
        "email_hash": hash_identifier(original_email),
        "name_hash": hash_identifier(user.name),
        "status": user.status,
    }
    now = utcnow()
    billing_outcome = _cancel_billing_for_erased_user(db, user, billing)
    suppress(db, original_email, "erasure")
    user.email = _tombstone_email(original_email)
    user.name = None
    user.password_hash = None
    user.status = "anonymised"
    user.anonymised_at = now
    user.marketing_consent = False
    _revoke_user_sessions(db, user)
    _revoke_user_api_keys(db, user, now=now)
    for search in db.scalars(select(SavedSearch).where(SavedSearch.user_id == user.id)).all():
        search.status = "paused"
        search.channels = [c for c in search.channels if c != "email"]
    task.contact = None

    if ctx.user is None:  # unreachable: require_admin() already refused an unauthenticated caller
        raise ProblemError("unauthenticated", "An operator session is required")
    event = record_audit_event(
        db,
        subject_type="user",
        subject_id=user.id,
        event_type="personal_data_redacted",
        actor=ctx.user,
        reason=reason,
        before=before,
        after={
            "email": "tombstone",
            "name": None,
            "status": "anonymised",
            "suppressed": True,
            "saved_searches": "paused",
            **billing_outcome,
        },
    )
    task.status = "done"
    task.completed_at = now
    task.audit_event_ids = [*(task.audit_event_ids or []), public_id("evt", event.id)]
    db.flush()


@router.patch("/admin/v1/tasks/{task_id}")
def admin_update_task(
    task_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
    crm: Annotated[CrmPort, Depends(get_crm_port)],
    billing: Annotated[BillingPort, Depends(get_billing_port)],
) -> Any:
    reason = _require_reason(body, request)
    task = _find_task(db, task_id)
    if task is None:
        raise not_found(request.url.path)

    status_value = body.get("status")
    if status_value is not None and status_value not in TASK_STATUSES:
        raise validation_error("status", f"status must be one of {TASK_STATUSES}", request.url.path)

    has_assignee_key = "assignee_user_id" in body
    new_assignee_id = None
    if has_assignee_key and body["assignee_user_id"] is not None:
        assignee = _find_user(db, body["assignee_user_id"])
        if assignee is None:
            raise validation_error(
                "assignee_user_id", "assignee_user_id does not reference an existing user", request.url.path
            )
        new_assignee_id = assignee.id

    notes = body.get("notes")

    if status_value == "done" and task.type == "deletion_request":
        if task.status == "done":
            raise ProblemError(
                "conflict", "This deletion request is already completed", instance=request.url.path
            )
        _complete_deletion_task(db, task, request, ctx, reason=reason, crm=crm, billing=billing)
        if has_assignee_key:
            task.assignee_user_id = new_assignee_id
        if notes is not None:
            task.notes = notes
        db.flush()
        return build_envelope(
            serialize_task(task),
            meta=build_meta(lag_days=0, tier="admin"),
            licence_summary=build_licence_summary([]),
        )

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    if status_value is not None and status_value != task.status:
        before["status"] = task.status
        task.status = status_value
        after["status"] = status_value
        if status_value in ("done", "rejected") and task.completed_at is None:
            task.completed_at = utcnow()
    if has_assignee_key and new_assignee_id != task.assignee_user_id:
        before["assignee_user_id"] = str(task.assignee_user_id) if task.assignee_user_id else None
        task.assignee_user_id = new_assignee_id
        after["assignee_user_id"] = str(new_assignee_id) if new_assignee_id else None
    if notes is not None and notes != task.notes:
        before["notes"] = task.notes
        task.notes = notes
        after["notes"] = notes

    if before and ctx.user is not None:
        event = record_audit_event(
            db,
            subject_type="task",
            subject_id=task.id,
            event_type="admin_edit",
            actor=ctx.user,
            reason=reason,
            before=before,
            after=after,
        )
        task.audit_event_ids = [*(task.audit_event_ids or []), public_id("evt", event.id)]
    db.flush()
    return build_envelope(
        serialize_task(task),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


@router.post("/admin/v1/tasks/{task_id}/approve-intake")
def admin_approve_intake(
    task_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
    crm: Annotated[CrmPort, Depends(get_crm_port)],
) -> Any:
    reason = _require_reason(body, request)
    decision = body.get("decision")
    if decision not in ("approve", "link", "reject"):
        raise validation_error("decision", "decision must be approve, link or reject", request.url.path)

    task = _find_task(db, task_id)
    if task is None:
        raise not_found(request.url.path)
    if task.type not in ("intake_proposal", "intake_opportunity") or task.status not in (
        "open",
        "in_progress",
    ):
        raise ProblemError(
            "conflict",
            "Task is not an open intake submission",
            instance=request.url.path,
        )
    if ctx.user is None:  # unreachable: require_admin() already refused an unauthenticated caller
        raise ProblemError("unauthenticated", "An operator session is required")

    pending = task.pending_record if isinstance(task.pending_record, dict) else {}
    record_dict: dict[str, Any] | None = None
    create_lead = bool(body.get("create_lead", True))
    add_curated_issuer = bool(body.get("add_curated_issuer", True))

    if decision == "reject":
        before = {"status": task.status}
        task.status = "rejected"
        task.completed_at = utcnow()
        event = record_audit_event(
            db,
            subject_type="task",
            subject_id=task.id,
            event_type="admin_edit",
            actor=ctx.user,
            reason=reason,
            before=before,
            after={"status": "rejected", "decision": "reject"},
        )
        task.audit_event_ids = [*(task.audit_event_ids or []), public_id("evt", event.id)]

    elif decision == "link":
        link_to = body.get("link_to_public_id")
        if not isinstance(link_to, str) or not link_to:
            raise validation_error("link_to_public_id", "required for decision=link", request.url.path)
        target_subject_type: str
        if task.type == "intake_proposal":
            if not link_to.startswith("prop_"):
                raise not_found(request.url.path)
            proposal_target = db.scalar(select(Proposal).where(Proposal.public_id == link_to))
            if proposal_target is None:
                raise not_found(request.url.path)
            before_identifiers = dict(proposal_target.identifiers or {})
            proposal_target.identifiers = {**before_identifiers, **_dict_field(pending, "identifiers")}
            target_subject_type = "proposal"
            target_id = proposal_target.id
            target_identifiers = proposal_target.identifiers
            record_dict = _serialize_admin_proposal(proposal_target)
        else:
            if not link_to.startswith("opp_"):
                raise not_found(request.url.path)
            opportunity_target = db.scalar(select(Opportunity).where(Opportunity.public_id == link_to))
            if opportunity_target is None:
                raise not_found(request.url.path)
            before_identifiers = dict(opportunity_target.identifiers or {})
            opportunity_target.identifiers = {**before_identifiers, **_dict_field(pending, "identifiers")}
            target_subject_type = "opportunity"
            target_id = opportunity_target.id
            target_identifiers = opportunity_target.identifiers
            record_dict = _serialize_admin_opportunity(opportunity_target)

        task.subject_type = target_subject_type
        task.subject_id = target_id
        task.status = "done"
        task.completed_at = utcnow()
        event = record_audit_event(
            db,
            subject_type=target_subject_type,
            subject_id=target_id,
            event_type="admin_edit",
            actor=ctx.user,
            reason=reason,
            before={"identifiers": before_identifiers},
            after={"identifiers": target_identifiers},
        )
        task.audit_event_ids = [*(task.audit_event_ids or []), public_id("evt", event.id)]

    else:  # approve
        publish_state = body.get("publish_state") or "pending_review"
        if publish_state not in RECORD_PUBLISH_STATES:
            raise validation_error(
                "publish_state", f"publish_state must be one of {RECORD_PUBLISH_STATES}", request.url.path
            )
        if publish_state == "public" and not task.public_opt_in:
            raise validation_error(
                "publish_state", "publish_state 'public' requires the submitter's opt-in", request.url.path
            )

        org: Organization | None
        record: Proposal | Opportunity
        if task.type == "intake_proposal":
            record, org = _create_proposal_from_pending(db, request, pending, publish_state=publish_state)
            record_dict = _serialize_admin_proposal(record)
            subject_type = "proposal"
        else:
            record, org = _create_opportunity_from_pending(db, request, pending, publish_state=publish_state)
            record_dict = _serialize_admin_opportunity(record)
            subject_type = "opportunity"
            if add_curated_issuer and org is not None:
                # A source-registry row for the issuer is created separately by the operator
                # through `POST /admin/v1/sources` (another agent's area) — this only flips the
                # organisation flag (task brief, US-1003 AC1).
                org.is_curated_issuer = True

        task.subject_type = subject_type
        task.subject_id = record.id
        task.status = "done"
        task.completed_at = utcnow()

        task_event = record_audit_event(
            db,
            subject_type="task",
            subject_id=task.id,
            event_type="admin_edit",
            actor=ctx.user,
            reason=reason,
            before={"status": "open"},
            after={"status": "done", "decision": "approve"},
        )
        record_event = record_audit_event(
            db,
            subject_type=subject_type,
            subject_id=record.id,
            event_type="created",
            actor=ctx.user,
            reason=reason,
            after={"name": record_dict.get("name_canonical") or record_dict.get("title")},
        )
        task.audit_event_ids = [
            *(task.audit_event_ids or []),
            public_id("evt", task_event.id),
            public_id("evt", record_event.id),
        ]

        if create_lead:  # decision 4: only on approve, and only when the pending record is new
            domain = _normalise_domain(org.website) if org is not None else None
            signal_event_type = "proposal.new" if task.type == "intake_proposal" else "opportunity.rfp_opened"
            subject_url = (
                f"{WEB_HOST}/proposals/{record.slug}"
                if task.type == "intake_proposal"
                else f"{WEB_HOST}/opportunities/{record.slug}"
            )
            subject_name = record_dict.get("name_canonical") or record_dict.get("title") or ""
            signal = LeadSignal(
                signal_id=f"intake:{task.public_id}",
                event_type=signal_event_type,
                subject_kind=subject_type,
                subject_name=subject_name,
                subject_url=subject_url,
                observed_at=utcnow(),
                score=50,
                rationale="Approved from an admin-reviewed intake submission.",
                company_domain=domain,
                platform_org_id=org.public_id if org is not None else None,
            )
            try:
                if domain is not None:
                    crm.upsert_company(
                        CompanyUpsert(
                            domain=domain, platform_org_id=org.public_id if org is not None else None
                        )
                    )
                crm.create_lead_signal(signal)
            except SorUnavailable as exc:
                raise ProblemError(
                    "sor_unavailable",
                    "CRM adapter unavailable",
                    detail="The CRM could not be reached; try again shortly.",
                    instance=request.url.path,
                ) from exc
            except SorRejected as exc:
                raise ProblemError(
                    "conflict", "The CRM rejected this lead", instance=request.url.path
                ) from exc

    db.flush()
    data = {
        "task": serialize_task(task),
        "decision": decision,
        "record": record_dict,
        "matches_computed": 0,  # decision 11
        "lead": None,  # decision 3
        "curated_issuer_source_id": None,
    }
    return build_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


# ======================================================================================== customers
@router.get("/admin/v1/customers")
def admin_list_customers(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"limit", "cursor", "q", "entitlement", "status"})
    stmt = select(Account)

    if q := request.query_params.get("q"):
        stmt = stmt.where(Account.name.ilike(f"%{q}%"))
    if entitlement := request.query_params.get("entitlement"):
        stmt = stmt.where(Account.entitlement.in_(csv_param(entitlement)))
    if status_filter := request.query_params.get("status"):
        stmt = stmt.where(Account.status.in_(csv_param(status_filter)))

    limit_raw = request.query_params.get("limit")
    limit = clamp_limit(int(limit_raw)) if limit_raw is not None else DEFAULT_LIMIT
    cursor = request.query_params.get("cursor")
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=Account.created_at,
        id_column=Account.public_id,
        ascending=False,
        cursor=cursor,
        limit=limit,
        instance=request.url.path,
    )
    data = [_serialize_customer(db, a, detail=False) for a in rows]
    return build_list_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
        page=build_page(next_cursor, None, has_more),
    )


@router.post("/admin/v1/customers", status_code=201)
def admin_create_customer(
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
    crm: Annotated[CrmPort, Depends(get_crm_port)],
) -> Any:
    reason = _require_reason(body, request)
    name = body.get("name")
    kind = body.get("kind")
    if not isinstance(name, str) or not name.strip():
        raise validation_error("name", "name is required", request.url.path)
    if kind not in ACCOUNT_KINDS:
        raise validation_error("kind", f"kind must be one of {ACCOUNT_KINDS}", request.url.path)
    entitlement_source = body.get("entitlement_source", "sor")
    if entitlement_source not in ACCOUNT_ENTITLEMENT_SOURCES:
        raise validation_error(
            "entitlement_source", f"must be one of {ACCOUNT_ENTITLEMENT_SOURCES}", request.url.path
        )

    organization: Organization | None = None
    org_id = body.get("organization_id")
    if isinstance(org_id, str) and org_id:
        organization = db.scalar(select(Organization).where(Organization.public_id == org_id))
        if organization is None:
            raise validation_error("organization_id", f"no organization {org_id!r}", request.url.path)

    account = Account(
        public_id="",
        name=name.strip(),
        kind=kind,
        organization_id=organization.id if organization else None,
        entitlement_source=entitlement_source,
    )
    db.add(account)
    db.flush()
    account.public_id = public_id("acc", account.id)
    db.flush()

    primary_contact_email = body.get("primary_contact_email")
    if isinstance(primary_contact_email, str) and primary_contact_email.strip():
        contact_user = User(
            public_id="",
            account_id=account.id,
            email=primary_contact_email.strip().lower(),
            role="member",
            status="active",
        )
        db.add(contact_user)
        db.flush()
        contact_user.public_id = public_id("usr", contact_user.id)
        db.flush()

    domain = _normalise_domain(organization.website) if organization is not None else None
    crm_skipped_reason = None
    if domain is None and isinstance(primary_contact_email, str):
        domain = _domain_from_email(primary_contact_email)
    if domain is None:
        crm_skipped_reason = "no organization website or usable contact email domain"

    if domain is not None:
        try:
            ref = crm.upsert_company(CompanyUpsert(domain=domain, platform_account_id=account.public_id))
        except SorUnavailable as exc:
            raise ProblemError(
                "sor_unavailable",
                "CRM adapter unavailable",
                detail="The CRM could not be reached; no customer was created.",
                instance=request.url.path,
            ) from exc
        except SorRejected as exc:
            raise ProblemError(
                "conflict", "The CRM rejected this customer", instance=request.url.path
            ) from exc
        company = crm.get_company(ref)  # US-902 AC2: mirror refreshed from the read-back
        account.sor_kind = company.ref.sor_kind if company is not None else ref.sor_kind
        account.sor_ref = company.ref.sor_ref if company is not None else ref.sor_ref
        db.flush()

    audit_after: dict[str, Any] = {"name": account.name, "kind": account.kind}
    if crm_skipped_reason:
        audit_after["crm_skipped_reason"] = crm_skipped_reason
    if ctx.user is not None:
        record_audit_event(
            db,
            subject_type="account",
            subject_id=account.id,
            event_type="created",
            actor=ctx.user,
            reason=reason,
            after=audit_after,
        )

    return build_envelope(
        _serialize_customer(db, account, detail=True),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


@router.get("/admin/v1/customers/{account_id}")
def admin_get_customer(
    account_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    account = _find_account(db, account_id)
    if account is None:
        raise not_found(request.url.path)
    return build_envelope(
        _serialize_customer(db, account, detail=True),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


# ===================================================================================== subscriptions
@router.post("/admin/v1/subscriptions", status_code=201)
def admin_create_subscription(
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
    billing: Annotated[BillingPort, Depends(get_billing_port)],
) -> Any:
    reason = _require_reason(body, request)
    account_id = body.get("account_id")
    plan_code = body.get("plan_code")
    seats = body.get("seats")
    trial_days = body.get("trial_days")

    if not isinstance(account_id, str) or not account_id:
        raise validation_error("account_id", "account_id is required", request.url.path)
    if plan_code not in PLAN_TIERS:
        raise validation_error("plan_code", f"plan_code must be one of {PLAN_TIERS}", request.url.path)
    if not isinstance(seats, int) or isinstance(seats, bool) or seats < 1:
        raise validation_error("seats", "seats must be an integer >= 1", request.url.path)
    if trial_days is not None and (not isinstance(trial_days, int) or isinstance(trial_days, bool)):
        raise validation_error("trial_days", "trial_days must be an integer or null", request.url.path)

    account = _find_account(db, account_id)
    if account is None:
        raise not_found(request.url.path)
    if not account.billing_ref:
        raise ProblemError(
            "conflict",
            "Account has no billing customer on file",
            detail="A checkout or a customer id must exist before a subscription can be created.",
            instance=request.url.path,
        )

    try:
        state = billing.create_subscription(
            SubscriptionCreate(
                billing_ref=account.billing_ref,
                plan=plan_code,
                seats=seats,
                trial_days=trial_days,
                account_public_id=account.public_id,
                reason=reason,
            )
        )
    except SorUnavailable as exc:
        raise ProblemError(
            "sor_unavailable",
            "Billing provider unavailable",
            detail="The billing provider could not be reached; try again shortly.",
            instance=request.url.path,
        ) from exc
    except SorRejected as exc:
        raise ProblemError(
            "conflict", "The billing provider rejected the subscription", instance=request.url.path
        ) from exc

    # `EntitlementChange.event_ref` idempotency key for an operator-created subscription (task
    # brief: `admin:<task or uuid>`) — there is no `Task` row for this write, so a fresh uuid4.
    change = EntitlementChange(
        event_ref=f"admin:{_uuid.uuid4()}",
        occurred_at=utcnow(),
        billing_ref=state.billing_ref,
        subscription=state,
        entitlement=entitlement_for(state.plan_tier, state.status),
        account_public_id=account.public_id,
    )
    apply_entitlement_change(db, change)

    row = db.scalar(
        select(Subscription).where(
            Subscription.sor_kind == state.sor_kind, Subscription.sor_ref == state.sor_ref
        )
    )
    if row is None:  # pragma: no cover - apply_entitlement_change always creates it for a matched account
        raise ProblemError("conflict", "Subscription could not be mirrored", instance=request.url.path)

    return build_envelope(
        _serialize_subscription(row, account),
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


__all__ = ["router"]
