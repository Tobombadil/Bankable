"""The intake-approval flow (`POST /admin/v1/tasks/{task_id}/approve-intake`), split out of
`services/api/admin_people.py` (docs/42-backend-review-2026-09-26.md §4.3, §7 lane L4): turning an
`intake_proposal`/`intake_opportunity` task's pending record into a stored `Proposal`/`Opportunity`
(`approve`), attaching identifiers to an existing one (`link`), or closing the task with no record
(`reject`). `router = APIRouter()` is mounted by the coordinator (`services/api/app.py`) exactly the
way `admin_people`'s is — same prefix-free routes, tags and dependencies, so the mounted path is
unchanged. This module imports `_find_task`, `_require_reason` and `serialize_task` from
`admin_people` (task-queue plumbing shared with routes that stay there) and never imports or edits
`app.py`, `admin_sources.py`, `admin_records.py`, `admin_posts.py` or `services/billing/*`/
`services/crm/*` (CLAUDE.md "one agent per file area").

Decisions carried over from `admin_people.py` (fuller versions with reasons in
`services/api/admin_people.md`), renumbered for this module:

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
6. Organisation resolution for intake approval defaults `type` to `developer` for a proposal
   sponsor and `other` for an opportunity issuer (the vocab has no safe generic "issuer" bucket);
   `country` comes from the two-letter prefix of `jurisdiction`, else `US` (every source in this
   sprint is US-only).
7. Every CRM adapter call in this module runs inside the request's one DB session
   (`services/api/deps.py` `get_db`): raising past `SorUnavailable`/`SorRejected` rolls back
   whatever this request already wrote (a new proposal, a task update), matching the deletion-task
   rule in `admin_people.py` ("nothing half-applied") without needing bespoke transaction handling.
8. `matches_computed` on `POST .../approve-intake` is always `0` — the US-401 rule engine is a
   different sprint's area; nothing here recomputes matches, and the field says so honestly rather
   than fabricating a count.

The three decisions were one 159-line `if/elif` block (`admin_people.py` L1155-1313 on
`daf5e80`); `_IntakeDecisionContext` carries the five inputs every decision needs (`db`, `request`,
`task`, `reason`, `ctx`) so none of the three decision functions takes more than six parameters.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute, Session

from services.api.admin_people import _find_task, _require_reason, serialize_task
from services.api.audit import record_audit_event
from services.api.auth import AuthContext, require_admin
from services.api.common import WEB_HOST, iso, normalise_domain, utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, validation_error
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_meta,
    serialize_opportunity,
    serialize_proposal,
)
from services.db.models import (
    LIFECYCLE_STATES,
    RECORD_PUBLISH_STATES,
    Licence,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    Source,
    Task,
    User,
)
from services.ids import public_id, slugify
from services.sor.ports import CompanyUpsert, CrmPort, LeadSignal, SorRejected, SorUnavailable
from services.sor.wiring import get_crm_port

router = APIRouter()

_INTAKE_SOURCE_ID = "intake.platform"  # decision 2
_INTAKE_LICENCE_ID = "platform-open"


# ------------------------------------------------------------------------------------- small helpers
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


# --------------------------------------------------------------------------------------- serialization
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
        default_type="developer",  # decision 6
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
        default_type="other",  # decision 6
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


# ------------------------------------------------------------------------------------ the decision
@dataclass(frozen=True, slots=True)
class _IntakeDecisionContext:
    """The five inputs every decision function needs — one call site each below, plus the route,
    which is why a shared frozen context is a smaller change than repeating five parameters three
    times over (docs/42-backend-review-2026-09-26.md §7 lane L4). `actor` is `ctx.user` already
    narrowed non-`None` by the route's own check, so the decision functions never need `AuthContext`
    or a second `None` check to satisfy the type checker."""

    db: Session
    request: Request
    task: Task
    reason: str
    actor: User


def _apply_reject_decision(dctx: _IntakeDecisionContext) -> None:
    task = dctx.task
    before = {"status": task.status}
    task.status = "rejected"
    task.completed_at = utcnow()
    event = record_audit_event(
        dctx.db,
        subject_type="task",
        subject_id=task.id,
        event_type="admin_edit",
        actor=dctx.actor,
        reason=dctx.reason,
        before=before,
        after={"status": "rejected", "decision": "reject"},
    )
    task.audit_event_ids = [*(task.audit_event_ids or []), public_id("evt", event.id)]


def _apply_link_decision(
    dctx: _IntakeDecisionContext, pending: dict[str, Any], body: dict[str, Any]
) -> dict[str, Any]:
    db, request, task, reason, actor = dctx.db, dctx.request, dctx.task, dctx.reason, dctx.actor
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
        actor=actor,
        reason=reason,
        before={"identifiers": before_identifiers},
        after={"identifiers": target_identifiers},
    )
    task.audit_event_ids = [*(task.audit_event_ids or []), public_id("evt", event.id)]
    return record_dict


def _create_intake_lead_signal(
    dctx: _IntakeDecisionContext,
    crm: CrmPort,
    record: Proposal | Opportunity,
    org: Organization | None,
    subject_type: str,
    record_dict: dict[str, Any],
) -> None:
    """decision 4: only on approve, and only when the pending record is new."""
    task, request = dctx.task, dctx.request
    domain = normalise_domain(org.website) if org is not None else None
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
                CompanyUpsert(domain=domain, platform_org_id=org.public_id if org is not None else None)
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
        raise ProblemError("conflict", "The CRM rejected this lead", instance=request.url.path) from exc


def _record_approve_audit_events(
    dctx: _IntakeDecisionContext,
    subject_type: str,
    record: Proposal | Opportunity,
    record_dict: dict[str, Any],
) -> None:
    db, task, reason, actor = dctx.db, dctx.task, dctx.reason, dctx.actor
    task_event = record_audit_event(
        db,
        subject_type="task",
        subject_id=task.id,
        event_type="admin_edit",
        actor=actor,
        reason=reason,
        before={"status": "open"},
        after={"status": "done", "decision": "approve"},
    )
    record_event = record_audit_event(
        db,
        subject_type=subject_type,
        subject_id=record.id,
        event_type="created",
        actor=actor,
        reason=reason,
        after={"name": record_dict.get("name_canonical") or record_dict.get("title")},
    )
    task.audit_event_ids = [
        *(task.audit_event_ids or []),
        public_id("evt", task_event.id),
        public_id("evt", record_event.id),
    ]


def _apply_approve_decision(
    dctx: _IntakeDecisionContext, pending: dict[str, Any], body: dict[str, Any], crm: CrmPort
) -> dict[str, Any]:
    db, request, task = dctx.db, dctx.request, dctx.task
    create_lead = bool(body.get("create_lead", True))
    add_curated_issuer = bool(body.get("add_curated_issuer", True))

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
    _record_approve_audit_events(dctx, subject_type, record, record_dict)

    if create_lead:
        _create_intake_lead_signal(dctx, crm, record, org, subject_type, record_dict)

    return record_dict


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
    dctx = _IntakeDecisionContext(db=db, request=request, task=task, reason=reason, actor=ctx.user)
    record_dict: dict[str, Any] | None = None
    if decision == "reject":
        _apply_reject_decision(dctx)
    elif decision == "link":
        record_dict = _apply_link_decision(dctx, pending, body)
    else:
        record_dict = _apply_approve_decision(dctx, pending, body, crm)

    db.flush()
    data = {
        "task": serialize_task(task),
        "decision": decision,
        "record": record_dict,
        "matches_computed": 0,  # decision 8
        "lead": None,  # decision 3
        "curated_issuer_source_id": None,
    }
    return build_envelope(
        data,
        meta=build_meta(lag_days=0, tier="admin"),
        licence_summary=build_licence_summary([]),
    )


__all__ = ["router"]
