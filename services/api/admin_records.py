"""Admin record editing, publish/unpublish, merge/unmerge, resolution review and extraction
review (Sprint 3 item 3, `docs/00-PLAN.md` "Sprint 3 kickoff" item 3; `docs/20-architecture.md`
§8 items 2-4; `docs/10-prd-mvp.md` US-201, US-202, US-905, US-906, US-907, US-1002; `docs/21-data-
model.md` §3.9, §6, §8).

Mounted onto `services.api.app.app` with one `include_router(router)` call by the coordinator
(this module never imports or edits `services/api/app.py`), the same pattern `services/crm/
router.py` and `services/api/pro.py` already use.

Numbered decisions (full detail and rationale in `services/api/admin_records.md`):

1. `services.ids.public_id` is imported as `make_public_id` because several handlers below take a
   path parameter literally named `public_id` (matching `api/openapi.yaml`'s path template) and
   also need to encode a *different* row's id inside the same function body — `services/api/
   app.py`'s handlers never do both in one function, so this module needs the alias where that
   codebase's existing convention does not.
2. `ResolutionDecision` (`services/resolve/models.py`) and `Event`/`Match` have no `public_id`
   column; `rc_`/`evt_` ids are the Crockford encoding of the row's internal uuid, decoded with
   the same walk `services/api/app.py::_find_event_by_public_id` and `services/crm/router.py::
   _find_match_by_public_id` use (read-only files this task cites for exactly that pattern).
3. Merge preview/apply is atomic per HTTP call (no server-side "preview session" table): the
   preview token is a self-contained, HMAC-signed, 15-minute-lived credential over
   `(surviving_public_id, absorbed_public_id, field_choices, both rows' updated_at)`. A stale
   token (either row changed since, or the field choices differ from what was previewed) answers
   `409 conflict`; a missing, malformed or expired one answers `400 validation_error` (the task's
   "apply without a valid preview_token -> 400").
4. `merge_proposal`/`unmerge_proposal` (`services/resolve/merge.py`) already write one fully
   audit-shaped `merged`/`unmerged` event with `before`/`after`, a `reason` and `actor_type`
   (docs/21 §6.3). Calling `record_audit_event` a *second* time for the same action would create a
   second, indistinguishable `merged`/`unmerged` event on the same subject's timeline, breaking
   docs/21 §6.1's "one code path, one predicate" reuse of the event table and confusing US-202
   AC1's newest-first, one-row-per-change timeline. Instead this module sets `actor_user_id`
   directly on the event `merge_proposal`/`unmerge_proposal` already flushed. The resolution-
   candidate "confirm" path (which also calls `merge_proposal`) does the same; its "reject" path
   has no proposal-level state change to attach to, so it calls `record_audit_event` with
   `subject_type="proposal"` (the left member) — `resolution_decision` is not in docs/21 §3.10's
   `subject_type` vocabulary, and `admin_edit` is the closest event the vocab already has for "a
   human recorded a decision with a reason".
5. `docs/21` §7.1's `RecordPublishState` and the visibility predicate exist for `proposal` and
   `opportunity`; `organization` has no `publish_state`/`published_at`/`public_at` columns in
   `services/db/models.py`, and no visibility predicate anywhere reads one for it. Setting
   `record_type=organizations` on `PUT .../publish-state` is refused with `400 validation_error`
   rather than silently accepted-and-ignored (this task's paths cannot add the column or a
   migration) — a documented gap, not a fabricated no-op success.
6. Takedown (`RecordPublishStateRequest.takedown=true`) nulls `published_at`/`public_at` on every
   event of the subject (docs/21 has no separate publish flag on `event`, per this task's brief),
   including the just-written `published`/`unpublished` admin event itself — leaving the takedown
   notice publicly visible while the record it concerns is taken down would itself be a leak.
7. Admin proposal/opportunity detail always includes `sources` (every `proposal_source`/
   `opportunity_source` row, active or not) and `events` (first page, newest first) rather than
   gating them behind `include=` query params, and never hides `raw`/`source_record_id` behind the
   source's licence — `adminGetProposal`'s own description says "with ... gated sources and raw
   present. Admin only", and `docs/20` §8 item 2 lists "view provenance and event history" as a
   base admin capability, not an opt-in.
8. Extraction `accept` applies only scalar canonical fields (`docs/21` §3.9's `field_path`) on the
   subject record — organisation/issuer references and the nested `location` object are out of
   scope for one extraction row (an extraction proposes one field from one document) and are left
   as a follow-up; `field_path` must be one of the fields `AdminProposalUpdate`/
   `AdminOpportunityUpdate`/`AdminOrganizationUpdate` expose, else `409 conflict` (task rule).
9. Manually setting a proposal's `location` (`AdminProposalUpdate.location`) still needs a real
   `source_id`/`licence_id` (docs/21 §3.7 NOT NULL provenance quartet); this module attributes a
   newly created `Location` row to the proposal's most recently retrieved active source, with
   `geocoder="manual"` and `precision_reason="geocoder"` (the fixed enum `api/openapi.yaml` allows
   is `licence | source | geocoder`) marking it as hand-entered rather than that source's own
   geocoding.

Style matches `services/api/pro.py`: dict envelopes built by hand via `services.api.serialize`
(the committed `api/openapi.yaml` is the authority on shape; `tests/test_api_admin_records.py`
validates every response against it directly), no parallel Pydantic schema tree, no model
identifiers of any AI system anywhere in this file or its output.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import hashlib
import hmac
import json
import uuid as _uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.audit import record_audit_event
from services.api.auth import AuthContext, require_admin
from services.api.common import WEB_HOST, utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, validation_error
from services.api.pagination import clamp_limit, paginate
from services.api.params import check_allowed, csv_param
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_list_envelope,
    build_meta,
    build_page,
    provenance_quartet,
    serialize_event,
    serialize_location,
    serialize_opportunity,
    serialize_organization,
    serialize_proposal,
)
from services.db.models import (
    EXTRACTION_STATUSES,
    LIFECYCLE_STATES,
    OPPORTUNITY_STATUSES,
    RECORD_PUBLISH_STATES,
    Event,
    Extraction,
    Licence,
    Location,
    ModelCall,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
)
from services.ids import _CROCKFORD
from services.ids import public_id as make_public_id
from services.resolve.merge import merge_proposal, unmerge_proposal
from services.resolve.models import ResolutionDecision

router = APIRouter()

# ---------------------------------------------------------------------------------------- vocab
# `api/openapi.yaml` enums with no counterpart tuple in `services/db/models.py` (that module has
# no CHECK constraint for `proposal.kind`, `opportunity.kind` or `organization.type`) — kept here,
# local to this module, rather than added to the read-only `services/db/models.py`.
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
ORGANIZATION_TYPES = (
    "developer",
    "ipp",
    "utility",
    "coop",
    "cca",
    "agency",
    "lender",
    "investor",
    "epc",
    "oem",
    "offtaker",
    "other",
)
RECORD_TYPES = ("proposals", "opportunities", "organizations")
_SUBJECT_MODELS: dict[str, type] = {
    "proposal": Proposal,
    "opportunity": Opportunity,
    "organization": Organization,
}

#: Extraction `field_path` values this endpoint will apply (decision 8): scalar canonical fields
#: only, a subset of each `Admin*Update` schema's own properties.
_PROPOSAL_EXTRACTION_FIELDS = frozenset(
    {
        "name_canonical",
        "technology",
        "capacity_mw",
        "storage_mwh",
        "jurisdiction",
        "iso",
        "lifecycle_state",
        "proposed_online_date",
    }
)
_OPPORTUNITY_EXTRACTION_FIELDS = frozenset(
    {
        "title",
        "summary",
        "jurisdiction",
        "technologies",
        "capacity_sought_mw",
        "budget_amount",
        "budget_currency",
        "open_at",
        "due_at",
        "status",
    }
)
_ORGANIZATION_EXTRACTION_FIELDS = frozenset(
    {"name_canonical", "type", "country", "jurisdiction", "website", "is_curated_issuer"}
)
_EDITABLE_FIELDS: dict[str, frozenset[str]] = {
    "proposal": _PROPOSAL_EXTRACTION_FIELDS,
    "opportunity": _OPPORTUNITY_EXTRACTION_FIELDS,
    "organization": _ORGANIZATION_EXTRACTION_FIELDS,
}
_DATE_FIELDS = frozenset({"proposed_online_date", "open_at"})
_DATETIME_FIELDS = frozenset({"due_at"})

_MERGE_COMPARABLE_FIELDS = (
    "name_canonical",
    "kind",
    "technology",
    "capacity_mw",
    "storage_mwh",
    "jurisdiction",
    "iso",
    "lifecycle_state",
)
_PREVIEW_MAX_AGE_SECONDS = 15 * 60


# ------------------------------------------------------------------------------- small utilities
def _jsonable(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _int_param(request: Request, name: str) -> int | None:
    raw = request.query_params.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise validation_error(name, f"{name} must be an integer", request.url.path) from exc


def _decode_crockford(prefix: str, value: str) -> _uuid.UUID | None:
    """Reverses a `<prefix>_<crockford>` id synthesised from a row's internal uuid (decision 2) —
    same walk as `services/api/app.py::_find_event_by_public_id` and `services/crm/router.py::
    _find_match_by_public_id`."""
    if not value.startswith(f"{prefix}_"):
        return None
    digits = value[len(prefix) + 1 :]
    try:
        n = 0
        for ch in digits:
            n = n * 32 + _CROCKFORD.index(ch)
        return _uuid.UUID(int=n)
    except (ValueError, OverflowError):
        return None


def _session_secret() -> str:
    # One source of truth (services/api/auth.py::session_secret): dev-only fallback outside
    # production, RuntimeError when unset or short in production (blockers sprint, 2026-09-19).
    from services.api.auth import session_secret

    return session_secret()


def _empty_licence_summary() -> dict[str, Any]:
    return build_licence_summary([])


def _admin_meta() -> dict[str, Any]:
    return build_meta(lag_days=0, tier="admin")


# ----------------------------------------------------------------------------- provenance/sources
def _admin_provenance_row(link: ProposalSource | OpportunitySource) -> dict[str, Any]:
    """Like `services.api.serialize.provenance_row`, but never withholds `source_record_id` —
    admin sees every field regardless of the source's `allows_raw_publication` (decision 7)."""
    source = link.source
    row = provenance_quartet(
        source, source.licence, source_url=link.source_url, retrieved_at=link.retrieved_at
    )
    row.update(
        {
            "source_record_id": link.source_record_id,
            "first_seen": _jsonable(link.first_seen),
            "last_seen": _jsonable(link.last_seen),
            "gone_at": _jsonable(link.gone_at),
        }
    )
    return row


def _admin_source_row(link: ProposalSource | OpportunitySource) -> dict[str, Any]:
    # `link_event_id` is typed `EventPublicId` (a plain string, not nullable) in
    # `api/openapi.yaml`'s `ProposalSource` schema — omitted entirely when there is no linking
    # event, same as `services.api.serialize.provenance_row` already does for the public tier.
    row = _admin_provenance_row(link)
    row.update(
        {
            "raw": link.raw or {},
            "normalised": link.normalised or {},
            "status_raw": link.status_raw,
            "link_method": link.link_method,
            "link_confidence": float(link.link_confidence),
            "active": link.active,
        }
    )
    if link.link_event_id:
        row["link_event_id"] = make_public_id("evt", link.link_event_id)
    return row


def _admin_events(
    db: Session, subject_type: str, obj: Proposal | Opportunity, *, limit: int = 50
) -> list[Any]:
    rows = db.scalars(
        select(Event)
        .where(Event.subject_type == subject_type, Event.subject_id == obj.id)
        .order_by(Event.seq.desc())
        .limit(limit)
    ).all()
    name = obj.name_canonical if isinstance(obj, Proposal) else obj.title
    path = "proposals" if subject_type == "proposal" else "opportunities"
    subject_url = f"{WEB_HOST}/{path}/{obj.slug}"
    return [
        serialize_event(e, subject_public_id=obj.public_id, subject_name=name, subject_url=subject_url)
        for e in rows
    ]


def _last_admin_event_id(db: Session, subject_type: str, subject_id: _uuid.UUID) -> str | None:
    ev = db.scalar(
        select(Event)
        .where(
            Event.subject_type == subject_type,
            Event.subject_id == subject_id,
            Event.event_type == "admin_edit",
        )
        .order_by(Event.seq.desc())
    )
    return make_public_id("evt", ev.id) if ev is not None else None


def _admin_proposal_dict(db: Session, proposal: Proposal) -> dict[str, Any]:
    sources = list(db.scalars(select(ProposalSource).where(ProposalSource.proposal_id == proposal.id)).all())
    data = serialize_proposal(proposal, sources=[s for s in sources if s.active])
    data["provenance"] = [_admin_provenance_row(s) for s in sources]
    data["sources"] = [_admin_source_row(s) for s in sources]
    data["events"] = _admin_events(db, "proposal", proposal)
    data["field_provenance"] = proposal.field_provenance or {}
    data["publish_state"] = proposal.publish_state
    data["overrides"] = proposal.overrides or {}
    data["last_admin_event_id"] = _last_admin_event_id(db, "proposal", proposal.id)
    return data


def _admin_opportunity_dict(db: Session, opportunity: Opportunity) -> dict[str, Any]:
    sources = list(
        db.scalars(select(OpportunitySource).where(OpportunitySource.opportunity_id == opportunity.id)).all()
    )
    data = serialize_opportunity(opportunity, sources=[s for s in sources if s.active])
    data["provenance"] = [_admin_provenance_row(s) for s in sources]
    data["sources"] = [_admin_source_row(s) for s in sources]
    data["events"] = _admin_events(db, "opportunity", opportunity)
    data["field_provenance"] = opportunity.field_provenance or {}
    data["publish_state"] = opportunity.publish_state
    data["overrides"] = opportunity.overrides or {}
    data["last_admin_event_id"] = _last_admin_event_id(db, "opportunity", opportunity.id)
    return data


# -------------------------------------------------------------------------- proposal/opportunity
@router.get("/admin/v1/proposals/{public_id}")
def admin_get_proposal(
    public_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    proposal = db.scalar(select(Proposal).where(Proposal.public_id == public_id))
    if proposal is None:
        raise not_found(request.url.path)
    return build_envelope(
        _admin_proposal_dict(db, proposal), meta=_admin_meta(), licence_summary=_empty_licence_summary()
    )


def _apply_manual_location(
    db: Session, proposal: Proposal, loc_body: dict[str, Any], instance: str
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Sets a hand-entered county/state/country on `proposal.location` (decision 9)."""
    county_fips = loc_body.get("county_fips")
    state_code = loc_body.get("state_code")
    country = loc_body.get("country") or (proposal.location.country if proposal.location else "US")

    location = proposal.location
    before = serialize_location(location) if location is not None else None
    if location is None:
        anchor = db.scalar(
            select(ProposalSource)
            .where(ProposalSource.proposal_id == proposal.id, ProposalSource.active.is_(True))
            .order_by(ProposalSource.retrieved_at.desc())
        )
        if anchor is None:
            raise ProblemError(
                "validation_error",
                "Invalid request",
                detail="This proposal has no active source to attribute a manually set location to.",
                instance=instance,
            )
        location = Location(
            kind="county" if county_fips else ("state" if state_code else "region"),
            precision="county_centroid" if county_fips else ("state_centroid" if state_code else "unknown"),
            precision_reason="geocoder",
            country=country,
            source_id=anchor.source_id,
            source_url=anchor.source_url,
            retrieved_at=anchor.retrieved_at,
            licence_id=anchor.licence_id,
            geocoder="manual",
        )
        db.add(location)
        db.flush()
        proposal.location = location  # sets location_id and keeps the ORM relationship in sync
    location.county_fips = county_fips
    location.state_code = state_code
    location.country = country
    location.geocoder = "manual"
    location.precision_reason = "geocoder"
    db.flush()
    return before, serialize_location(location)


@router.patch("/admin/v1/proposals/{public_id}")
def admin_update_proposal(
    public_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    proposal = db.scalar(select(Proposal).where(Proposal.public_id == public_id))
    if proposal is None:
        raise not_found(request.url.path)
    reason = body.get("reason")
    if not reason:
        raise validation_error("reason", "reason is required", request.url.path)
    edit_keys = [k for k in body if k not in ("reason", "clear_overrides")]
    if not edit_keys:
        raise validation_error(
            "body", "at least one editable field besides reason is required", request.url.path
        )

    if "kind" in body and body["kind"] not in PROPOSAL_KINDS:
        raise validation_error("kind", f"must be one of {PROPOSAL_KINDS}", request.url.path)
    if "lifecycle_state" in body and body["lifecycle_state"] not in LIFECYCLE_STATES:
        raise validation_error("lifecycle_state", f"must be one of {LIFECYCLE_STATES}", request.url.path)

    sponsor: Organization | None = None
    if "sponsor_org_id" in body and body["sponsor_org_id"] is not None:
        sponsor = db.scalar(select(Organization).where(Organization.public_id == body["sponsor_org_id"]))
        if sponsor is None:
            raise validation_error("sponsor_org_id", "no such organization", request.url.path)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    for field in (
        "name_canonical",
        "kind",
        "technology",
        "capacity_mw",
        "storage_mwh",
        "jurisdiction",
        "iso",
        "lifecycle_state",
        "identifiers",
    ):
        if field in body:
            old = _jsonable(getattr(proposal, field))
            new = body[field]
            if old != new:
                before[field] = old
                after[field] = _jsonable(new)
            setattr(proposal, field, new)

    if "proposed_online_date" in body:
        old = _jsonable(proposal.proposed_online_date)
        raw = body["proposed_online_date"]
        new_date = dt.date.fromisoformat(raw) if isinstance(raw, str) else None
        if old != _jsonable(new_date):
            before["proposed_online_date"] = old
            after["proposed_online_date"] = _jsonable(new_date)
        proposal.proposed_online_date = new_date

    if "sponsor_org_id" in body:
        old_sponsor = proposal.sponsor.public_id if proposal.sponsor else None
        new_sponsor = sponsor.public_id if sponsor else None
        if old_sponsor != new_sponsor:
            before["sponsor_org_id"] = old_sponsor
            after["sponsor_org_id"] = new_sponsor
        proposal.sponsor = sponsor  # keeps the ORM relationship in sync, not just the FK column

    if body.get("location") is not None:
        loc_before, loc_after = _apply_manual_location(db, proposal, body["location"], request.url.path)
        if loc_before != loc_after:
            before["location"] = loc_before
            after["location"] = loc_after

    if not before:
        raise validation_error("body", "no recognised field value actually changed", request.url.path)

    proposal.last_changed = utcnow()
    event = record_audit_event(
        db,
        subject_type="proposal",
        subject_id=proposal.id,
        event_type="admin_edit",
        actor=ctx.user,  # type: ignore[arg-type]
        reason=reason,
        before=before,
        after=after,
    )

    overrides = dict(proposal.overrides or {})
    for field in before:
        overrides[field] = {
            "value": after[field],
            "event_id": make_public_id("evt", event.id),
            "set_at": _jsonable(utcnow()),
            "user_id": ctx.user.public_id,  # type: ignore[union-attr]
        }
    for field in body.get("clear_overrides") or []:
        overrides.pop(field, None)
    proposal.overrides = overrides
    db.flush()

    return build_envelope(
        _admin_proposal_dict(db, proposal), meta=_admin_meta(), licence_summary=_empty_licence_summary()
    )


@router.get("/admin/v1/opportunities/{public_id}")
def admin_get_opportunity(
    public_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    opportunity = db.scalar(select(Opportunity).where(Opportunity.public_id == public_id))
    if opportunity is None:
        raise not_found(request.url.path)
    return build_envelope(
        _admin_opportunity_dict(db, opportunity), meta=_admin_meta(), licence_summary=_empty_licence_summary()
    )


@router.patch("/admin/v1/opportunities/{public_id}")
def admin_update_opportunity(
    public_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    opportunity = db.scalar(select(Opportunity).where(Opportunity.public_id == public_id))
    if opportunity is None:
        raise not_found(request.url.path)
    reason = body.get("reason")
    if not reason:
        raise validation_error("reason", "reason is required", request.url.path)
    edit_keys = [k for k in body if k not in ("reason", "clear_overrides")]
    if not edit_keys:
        raise validation_error(
            "body", "at least one editable field besides reason is required", request.url.path
        )

    if "kind" in body and body["kind"] not in OPPORTUNITY_KINDS:
        raise validation_error("kind", f"must be one of {OPPORTUNITY_KINDS}", request.url.path)
    if "status" in body and body["status"] not in OPPORTUNITY_STATUSES:
        raise validation_error("status", f"must be one of {OPPORTUNITY_STATUSES}", request.url.path)

    issuer: Organization | None = None
    if "issuer_org_id" in body and body["issuer_org_id"] is not None:
        issuer = db.scalar(select(Organization).where(Organization.public_id == body["issuer_org_id"]))
        if issuer is None:
            raise validation_error("issuer_org_id", "no such organization", request.url.path)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    for field in (
        "title",
        "kind",
        "summary",
        "jurisdiction",
        "technologies",
        "capacity_sought_mw",
        "budget_amount",
        "budget_currency",
        "status",
        "identifiers",
    ):
        if field in body:
            old = _jsonable(getattr(opportunity, field))
            new = body[field]
            if old != new:
                before[field] = old
                after[field] = _jsonable(new)
            setattr(opportunity, field, new)

    for field, dest_attr in (("open_at", "open_at"), ("due_at", "due_at")):
        if field in body:
            old = _jsonable(getattr(opportunity, dest_attr))
            raw = body[field]
            if raw is None:
                new_value: dt.date | dt.datetime | None = None
            elif field in _DATE_FIELDS:
                new_value = dt.date.fromisoformat(raw)
            else:
                new_value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if old != _jsonable(new_value):
                before[field] = old
                after[field] = _jsonable(new_value)
            setattr(opportunity, dest_attr, new_value)

    if "issuer_org_id" in body:
        old_issuer = opportunity.issuer.public_id if opportunity.issuer else None
        new_issuer = issuer.public_id if issuer else None
        if old_issuer != new_issuer:
            before["issuer_org_id"] = old_issuer
            after["issuer_org_id"] = new_issuer
        opportunity.issuer = issuer  # keeps the ORM relationship in sync, not just the FK column

    if not before:
        raise validation_error("body", "no recognised field value actually changed", request.url.path)

    opportunity.last_changed = utcnow()
    event = record_audit_event(
        db,
        subject_type="opportunity",
        subject_id=opportunity.id,
        event_type="admin_edit",
        actor=ctx.user,  # type: ignore[arg-type]
        reason=reason,
        before=before,
        after=after,
    )

    overrides = dict(opportunity.overrides or {})
    for field in before:
        overrides[field] = {
            "value": after[field],
            "event_id": make_public_id("evt", event.id),
            "set_at": _jsonable(utcnow()),
            "user_id": ctx.user.public_id,  # type: ignore[union-attr]
        }
    for field in body.get("clear_overrides") or []:
        overrides.pop(field, None)
    opportunity.overrides = overrides
    db.flush()

    return build_envelope(
        _admin_opportunity_dict(db, opportunity), meta=_admin_meta(), licence_summary=_empty_licence_summary()
    )


@router.patch("/admin/v1/organizations/{public_id}")
def admin_update_organization(
    public_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    org = db.scalar(select(Organization).where(Organization.public_id == public_id))
    if org is None:
        raise not_found(request.url.path)
    reason = body.get("reason")
    if not reason:
        raise validation_error("reason", "reason is required", request.url.path)
    edit_keys = [k for k in body if k != "reason"]
    if not edit_keys:
        raise validation_error(
            "body", "at least one editable field besides reason is required", request.url.path
        )

    if "type" in body and body["type"] not in ORGANIZATION_TYPES:
        raise validation_error("type", f"must be one of {ORGANIZATION_TYPES}", request.url.path)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field in ("name_canonical", "type", "country", "jurisdiction", "ids", "website", "is_curated_issuer"):
        if field in body:
            old = getattr(org, field)
            new = body[field]
            if old != new:
                before[field] = old
                after[field] = new
            setattr(org, field, new)

    if not before:
        raise validation_error("body", "no recognised field value actually changed", request.url.path)

    org.last_changed = utcnow()
    record_audit_event(
        db,
        subject_type="organization",
        subject_id=org.id,
        event_type="admin_edit",
        actor=ctx.user,  # type: ignore[arg-type]
        reason=reason,
        before=before,
        after=after,
    )
    db.flush()

    return build_envelope(
        serialize_organization(org), meta=_admin_meta(), licence_summary=_empty_licence_summary()
    )


# ------------------------------------------------------------------------------- publish state
@router.put("/admin/v1/records/{record_type}/{public_id}/publish-state")
def admin_set_record_publish_state(
    record_type: str,
    public_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    if record_type not in RECORD_TYPES:
        raise validation_error("record_type", f"must be one of {RECORD_TYPES}", request.url.path)
    if record_type == "organizations":
        # decision 5: no publish_state column exists on `organization` yet.
        raise validation_error(
            "record_type",
            "organizations do not carry an independent publish_state in this schema version",
            request.url.path,
        )

    record: Proposal | Opportunity | None
    if record_type == "proposals":
        subject_type = "proposal"
        record = db.scalar(select(Proposal).where(Proposal.public_id == public_id))
    else:
        subject_type = "opportunity"
        record = db.scalar(select(Opportunity).where(Opportunity.public_id == public_id))
    if record is None:
        raise not_found(request.url.path)

    new_state = body.get("publish_state")
    reason = body.get("reason")
    takedown = bool(body.get("takedown", False))
    if new_state not in RECORD_PUBLISH_STATES:
        raise validation_error("publish_state", f"must be one of {RECORD_PUBLISH_STATES}", request.url.path)
    if not reason:
        raise validation_error("reason", "reason is required", request.url.path)
    if new_state in ("public", "api_only") and record.min_reuse_class in ("restricted", "unknown"):
        raise ProblemError(
            "gate_unmet",
            "Publication gate unmet",
            detail=(
                f"min_reuse_class is {record.min_reuse_class!r} over visible sources; "
                "publish/api_only is refused (docs/21 §8)."
            ),
            instance=request.url.path,
        )

    before = {"publish_state": record.publish_state}
    record.publish_state = new_state
    now = utcnow()
    if new_state == "public":
        record.published_at = record.published_at or now
        record.public_at = record.public_at or now
    record.last_changed = now

    event = record_audit_event(
        db,
        subject_type=subject_type,
        subject_id=record.id,
        event_type="published" if new_state == "public" else "unpublished",
        actor=ctx.user,  # type: ignore[arg-type]
        reason=reason,
        before=before,
        after={"publish_state": new_state, "takedown": takedown},
    )
    db.flush()

    if takedown:
        # decision 6: null every event's public visibility, including the one just written.
        for ev in db.scalars(
            select(Event).where(Event.subject_type == subject_type, Event.subject_id == record.id)
        ).all():
            ev.published_at = None
            ev.public_at = None
        db.flush()

    data = {
        "record_type": record_type,
        "public_id": record.public_id,
        "publish_state": record.publish_state,
        "event_id": make_public_id("evt", event.id),
        "effective_within_seconds": 60,
    }
    return build_envelope(data, meta=_admin_meta(), licence_summary=_empty_licence_summary())


# ------------------------------------------------------------------------------- merge/unmerge
def _proposal_merge_conflicts(
    surviving: Proposal, absorbed: Proposal, field_choices: dict[str, str]
) -> list[dict[str, Any]]:
    conflicts = []
    for field in _MERGE_COMPARABLE_FIELDS:
        sval = _jsonable(getattr(surviving, field))
        aval = _jsonable(getattr(absorbed, field))
        if sval != aval:
            conflicts.append(
                {
                    "field": field,
                    "surviving_value": sval,
                    "absorbed_value": aval,
                    "chosen": field_choices.get(field, "surviving"),
                }
            )
    return conflicts


def _merge_preview_token(surviving: Proposal, absorbed: Proposal, field_choices: dict[str, str]) -> str:
    payload = {
        "s": surviving.public_id,
        "a": absorbed.public_id,
        "fc": dict(sorted(field_choices.items())),
        "su": _jsonable(surviving.updated_at),
        "au": _jsonable(absorbed.updated_at),
        "ts": utcnow().isoformat(),
    }
    body = json.dumps(payload, sort_keys=True).encode()
    sig = hmac.new(_session_secret().encode(), body, hashlib.sha256).hexdigest()
    return f"{base64.urlsafe_b64encode(body).decode().rstrip('=')}.{sig}"


def _verify_merge_preview_token(
    token: str, surviving: Proposal, absorbed: Proposal, field_choices: dict[str, str]
) -> str:
    """Returns `"ok"`, `"invalid"` (bad/expired/mismatched token) or `"stale"` (either row
    changed, or different field choices, since the preview was issued) — decision 3."""
    try:
        body_b64, sig = token.rsplit(".", 1)
        padded = body_b64 + "=" * (-len(body_b64) % 4)
        body = base64.urlsafe_b64decode(padded.encode())
        expected_sig = hmac.new(_session_secret().encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return "invalid"
        payload = json.loads(body)
        if payload["s"] != surviving.public_id or payload["a"] != absorbed.public_id:
            return "invalid"
        ts = dt.datetime.fromisoformat(payload["ts"])
        if (utcnow() - ts).total_seconds() > _PREVIEW_MAX_AGE_SECONDS:
            return "invalid"
    except Exception:
        return "invalid"
    if payload["su"] != _jsonable(surviving.updated_at) or payload["au"] != _jsonable(absorbed.updated_at):
        return "stale"
    if payload["fc"] != dict(sorted(field_choices.items())):
        return "stale"
    return "ok"


@router.post("/admin/v1/proposals/{public_id}/merge")
def admin_merge_proposal(
    public_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    surviving = db.scalar(select(Proposal).where(Proposal.public_id == public_id))
    if surviving is None:
        raise not_found(request.url.path)

    absorb_public_id = body.get("absorb_public_id")
    preview = body.get("preview")
    if not isinstance(absorb_public_id, str) or preview is None:
        raise validation_error(
            "absorb_public_id", "absorb_public_id and preview are required", request.url.path
        )
    absorbed = db.scalar(select(Proposal).where(Proposal.public_id == absorb_public_id))
    if absorbed is None:
        raise not_found(request.url.path)

    if surviving.id == absorbed.id:
        raise ProblemError(
            "conflict",
            "Conflict",
            detail="cannot merge a proposal into itself",
            instance=request.url.path,
        )
    if surviving.merged_into_id is not None or absorbed.merged_into_id is not None:
        raise ProblemError(
            "conflict",
            "Conflict",
            detail="one of these proposals is already merged elsewhere",
            instance=request.url.path,
        )

    field_choices = body.get("field_choices") or {}
    for choice in field_choices.values():
        if choice not in ("surviving", "absorbed"):
            raise validation_error(
                "field_choices", "values must be 'surviving' or 'absorbed'", request.url.path
            )
    conflicts = _proposal_merge_conflicts(surviving, absorbed, field_choices)

    if preview:
        token = _merge_preview_token(surviving, absorbed, field_choices)
        data = {
            "preview": True,
            "preview_token": token,
            "surviving": _admin_proposal_dict(db, surviving),
            "absorbed": _admin_proposal_dict(db, absorbed),
            "conflicts": conflicts,
            "moved": {"proposal_source_ids": [], "match_ids": [], "document_ids": []},
            "event_id": None,
        }
        return build_envelope(data, meta=_admin_meta(), licence_summary=_empty_licence_summary())

    reason = body.get("reason")
    if not reason:
        raise validation_error("reason", "reason is required for a real merge", request.url.path)
    preview_token = body.get("preview_token")
    if not preview_token:
        raise validation_error(
            "preview_token", "preview_token is required for a real merge", request.url.path
        )
    verdict = _verify_merge_preview_token(preview_token, surviving, absorbed, field_choices)
    if verdict == "invalid":
        raise validation_error("preview_token", "preview_token is invalid or expired", request.url.path)
    if verdict == "stale":
        raise ProblemError(
            "conflict",
            "Conflict",
            detail=(
                f"Merge preview is stale — {surviving.public_id} or {absorbed.public_id} changed "
                "since the preview token was issued. Preview again."
            ),
            instance=request.url.path,
        )

    moved_source_ids = [
        str(s.id)
        for s in db.scalars(
            select(ProposalSource).where(
                ProposalSource.proposal_id == absorbed.id, ProposalSource.active.is_(True)
            )
        ).all()
    ]
    for conflict in conflicts:
        if conflict["chosen"] == "absorbed":
            setattr(surviving, conflict["field"], getattr(absorbed, conflict["field"]))

    event = merge_proposal(
        db, canonical=surviving, absorbed=absorbed, score=100.0, rationale=reason, actor_type="user"
    )
    event.actor_user_id = ctx.user.id  # type: ignore[union-attr]
    db.flush()

    data = {
        "preview": False,
        "preview_token": None,
        "surviving": _admin_proposal_dict(db, surviving),
        "absorbed": _admin_proposal_dict(db, absorbed),
        "conflicts": conflicts,
        "moved": {"proposal_source_ids": moved_source_ids, "match_ids": [], "document_ids": []},
        "event_id": make_public_id("evt", event.id),
    }
    return build_envelope(data, meta=_admin_meta(), licence_summary=_empty_licence_summary())


@router.post("/admin/v1/proposals/{public_id}/unmerge")
def admin_unmerge_proposal(
    public_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    surviving = db.scalar(select(Proposal).where(Proposal.public_id == public_id))
    if surviving is None:
        raise not_found(request.url.path)

    reason = body.get("reason")
    merge_event_public_id = body.get("merge_event_id")
    if not reason or not merge_event_public_id:
        raise validation_error("reason", "merge_event_id and reason are required", request.url.path)

    event_uuid = _decode_crockford("evt", merge_event_public_id)
    merge_event = db.get(Event, event_uuid) if event_uuid is not None else None
    if merge_event is None:
        raise not_found(request.url.path)
    if (
        merge_event.event_type != "merged"
        or merge_event.subject_type != "proposal"
        or merge_event.subject_id != surviving.id
    ):
        raise ProblemError(
            "conflict",
            "Conflict",
            detail="This event is not a merge of this proposal.",
            instance=request.url.path,
        )

    try:
        event = unmerge_proposal(db, merge_event.id, reason=reason)
    except ValueError as exc:
        raise ProblemError("conflict", "Conflict", detail=str(exc), instance=request.url.path) from exc
    event.actor_user_id = ctx.user.id  # type: ignore[union-attr]
    db.flush()

    assert merge_event.before is not None  # noqa: S101 -- a `merged` event always carries one (invariant M1)
    absorbed_id = _uuid.UUID(merge_event.before["absorbed"]["id"])
    restored = db.get(Proposal, absorbed_id)
    assert restored is not None  # noqa: S101 -- unmerge_proposal already verified this row exists

    data = {
        "surviving": _admin_proposal_dict(db, surviving),
        "restored": _admin_proposal_dict(db, restored),
        "event_id": make_public_id("evt", event.id),
    }
    return build_envelope(data, meta=_admin_meta(), licence_summary=_empty_licence_summary())


# --------------------------------------------------------------------------- resolution candidates
def _candidate_side(proposal: Proposal | None) -> dict[str, Any]:
    if proposal is None:  # pragma: no cover -- defensive; rows are never deleted (docs/21 §6.1)
        return {"public_id": "prop_0000000000", "name": "(removed)", "summary": {}}
    return {
        "public_id": proposal.public_id,
        "name": proposal.name_canonical,
        "summary": {
            "kind": proposal.kind,
            "jurisdiction": proposal.jurisdiction,
            "lifecycle_state": proposal.lifecycle_state,
            "capacity_mw": _jsonable(proposal.capacity_mw),
        },
    }


def _serialize_candidate(db: Session, decision: ResolutionDecision) -> dict[str, Any]:
    left = db.get(Proposal, decision.left_proposal_id)
    right = db.get(Proposal, decision.right_proposal_id)
    extra = dict(decision.extra or {})
    merge_event_id = extra.pop("merge_event_id", None)
    status = "pending" if decision.status == "proposed" else "decided"
    api_decision = {"confirmed": "same", "rejected": "different"}.get(decision.status)
    return {
        "candidate_id": make_public_id("rc", decision.id),
        "subject_type": "proposal",
        "left": _candidate_side(left),
        "right": _candidate_side(right),
        "score": float(decision.score),
        "features": extra,
        "model_rationale": decision.rationale,
        "model_alias": None,
        "status": status,
        "decision": api_decision,
        "decided_by_user_id": make_public_id("usr", decision.decided_by_user_id)
        if decision.decided_by_user_id
        else None,
        "decided_at": _jsonable(decision.decided_at),
        "merge_event_id": merge_event_id,
        "created_at": _jsonable(decision.created_at),
    }


@router.get("/admin/v1/resolution-candidates")
def admin_list_resolution_candidates(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"limit", "cursor", "status", "subject_type"})
    qp = request.query_params

    if v := qp.get("subject_type"):
        if "proposal" not in csv_param(v):
            page = build_page(None, None, False)
            return build_list_envelope(
                [], meta=_admin_meta(), licence_summary=_empty_licence_summary(), page=page
            )

    status_param = qp.get("status", "pending")
    if status_param not in ("pending", "decided"):
        raise validation_error("status", "must be one of pending, decided", request.url.path)

    stmt = select(ResolutionDecision)
    if status_param == "pending":
        stmt = stmt.where(ResolutionDecision.status == "proposed")
    else:
        stmt = stmt.where(ResolutionDecision.status.in_(("confirmed", "rejected")))

    limit = clamp_limit(_int_param(request, "limit"))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=ResolutionDecision.score,
        id_column=ResolutionDecision.id,
        ascending=False,
        cursor=qp.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [_serialize_candidate(db, r) for r in rows]
    return build_list_envelope(
        data,
        meta=_admin_meta(),
        licence_summary=_empty_licence_summary(),
        page=build_page(next_cursor, None, has_more),
    )


@router.post("/admin/v1/resolution-candidates/{candidate_id}/decide")
def admin_decide_resolution_candidate(
    candidate_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    decision_uuid = _decode_crockford("rc", candidate_id)
    decision = db.get(ResolutionDecision, decision_uuid) if decision_uuid is not None else None
    if decision is None:
        raise not_found(request.url.path)

    api_decision = body.get("decision")
    reason = body.get("reason")
    if api_decision not in ("same", "different", "defer"):
        raise validation_error("decision", "must be one of same, different, defer", request.url.path)
    if not reason:
        raise validation_error("reason", "reason is required", request.url.path)
    if decision.status != "proposed":
        raise ProblemError(
            "conflict",
            "Conflict",
            detail="This candidate has already been decided.",
            instance=request.url.path,
        )

    if api_decision == "defer":
        return build_envelope(
            _serialize_candidate(db, decision), meta=_admin_meta(), licence_summary=_empty_licence_summary()
        )

    left = db.get(Proposal, decision.left_proposal_id)
    right = db.get(Proposal, decision.right_proposal_id)
    assert left is not None and right is not None  # noqa: S101 -- rows are never deleted

    if api_decision == "same":
        surviving_public_id = body.get("surviving_public_id") or left.public_id
        if surviving_public_id == left.public_id:
            surviving, absorbed = left, right
        elif surviving_public_id == right.public_id:
            surviving, absorbed = right, left
        else:
            raise validation_error(
                "surviving_public_id", "must be one of the candidate's two proposals", request.url.path
            )
        if surviving.merged_into_id is not None or absorbed.merged_into_id is not None:
            raise ProblemError(
                "conflict",
                "Conflict",
                detail="one of these proposals is already merged elsewhere",
                instance=request.url.path,
            )
        event = merge_proposal(
            db,
            canonical=surviving,
            absorbed=absorbed,
            score=float(decision.score),
            rationale=reason,
            actor_type="user",
        )
        event.actor_user_id = ctx.user.id  # type: ignore[union-attr]
        decision.status = "confirmed"
        decision.decided_by_user_id = ctx.user.id  # type: ignore[union-attr]
        decision.decided_at = utcnow()
        extra = dict(decision.extra or {})
        extra["merge_event_id"] = make_public_id("evt", event.id)
        decision.extra = extra
        db.flush()
    else:  # "different"
        decision.status = "rejected"
        decision.decided_by_user_id = ctx.user.id  # type: ignore[union-attr]
        decision.decided_at = utcnow()
        db.flush()
        record_audit_event(
            db,
            subject_type="proposal",
            subject_id=decision.left_proposal_id,
            event_type="admin_edit",
            actor=ctx.user,  # type: ignore[arg-type]
            reason=reason,
            before={"resolution_candidate": make_public_id("rc", decision.id), "status": "proposed"},
            after={"resolution_candidate": make_public_id("rc", decision.id), "status": "rejected"},
        )

    return build_envelope(
        _serialize_candidate(db, decision), meta=_admin_meta(), licence_summary=_empty_licence_summary()
    )


# ------------------------------------------------------------------------------------ extractions
def _subject_public_id(db: Session, subject_type: str, subject_id: _uuid.UUID) -> str | None:
    model = _SUBJECT_MODELS.get(subject_type)
    if model is None:  # pragma: no cover -- defensive; subject_type is CHECK-constrained upstream
        return None
    row = db.get(model, subject_id)
    return row.public_id if row is not None else None


def _serialize_extraction(db: Session, extraction: Extraction) -> dict[str, Any]:
    cost = 0.0
    if extraction.model_call_id is not None:
        call = db.get(ModelCall, extraction.model_call_id)
        if call is not None:
            cost = float(call.cost_usd)
    return {
        "extraction_id": extraction.public_id,
        "document_id": make_public_id("doc", extraction.document_id) if extraction.document_id else None,
        "subject_type": extraction.subject_type,
        "subject_id": _subject_public_id(db, extraction.subject_type, extraction.subject_id)
        or str(extraction.subject_id),
        "purpose": extraction.purpose,
        "field_path": extraction.field_path,
        "payload": extraction.payload or {},
        "confidence": float(extraction.confidence),
        "citations": extraction.citations or [],
        "model_alias": extraction.model_alias,
        "prompt_template_id": extraction.prompt_template_id,
        "prompt_version": extraction.prompt_version,
        "status": extraction.status,
        "accepted_by_user_id": make_public_id("usr", extraction.accepted_by_user_id)
        if extraction.accepted_by_user_id
        else None,
        "applied_event_id": make_public_id("evt", extraction.applied_event_id)
        if extraction.applied_event_id
        else None,
        "source_id": extraction.source_id,
        "licence_id": extraction.licence_id,
        "cost_usd": cost,
        "created_at": _jsonable(extraction.created_at),
    }


@router.get("/admin/v1/extractions")
def admin_list_extractions(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    check_allowed(request, {"limit", "cursor", "status", "subject_type", "source_id"})
    qp = request.query_params

    status_param = qp.get("status")
    statuses: list[str] = csv_param(status_param) if status_param else ["proposed"]
    for s in statuses:
        if s not in EXTRACTION_STATUSES:
            raise validation_error("status", f"must be one of {EXTRACTION_STATUSES}", request.url.path)

    stmt = select(Extraction).where(Extraction.status.in_(statuses))
    if v := qp.get("subject_type"):
        stmt = stmt.where(Extraction.subject_type.in_(csv_param(v)))
    if v := qp.get("source_id"):
        stmt = stmt.where(Extraction.source_id.in_(csv_param(v)))

    limit = clamp_limit(_int_param(request, "limit"))
    rows, next_cursor, has_more = paginate(
        db,
        stmt,
        sort_column=Extraction.confidence,
        id_column=Extraction.id,
        ascending=True,
        cursor=qp.get("cursor"),
        limit=limit,
        instance=request.url.path,
    )
    data = [_serialize_extraction(db, e) for e in rows]
    return build_list_envelope(
        data,
        meta=_admin_meta(),
        licence_summary=_empty_licence_summary(),
        page=build_page(next_cursor, None, has_more),
    )


def _coerce_extraction_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field in _DATE_FIELDS and isinstance(value, str):
        return dt.date.fromisoformat(value)
    if field in _DATETIME_FIELDS and isinstance(value, str):
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


@router.post("/admin/v1/extractions/{extraction_id}/accept")
def admin_accept_extraction(
    extraction_id: str,
    request: Request,
    body: dict[str, Any] | None,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    extraction = db.scalar(select(Extraction).where(Extraction.public_id == extraction_id))
    if extraction is None:
        raise not_found(request.url.path)
    reason = (body or {}).get("reason") or "accepted by admin"

    if extraction.status != "proposed":
        raise ProblemError(
            "conflict",
            "Conflict",
            detail="This extraction has already been decided.",
            instance=request.url.path,
        )
    licence = db.get(Licence, extraction.licence_id)
    if licence is None or not licence.allows_derived_publication:
        raise ProblemError(
            "gate_unmet",
            "Publication gate unmet",
            detail="The source licence withholds this value from every tier (docs/21 §8).",
            instance=request.url.path,
        )
    if not extraction.field_path:
        raise ProblemError(
            "conflict",
            "Conflict",
            detail="This extraction has no field_path to apply.",
            instance=request.url.path,
        )
    allowed = _EDITABLE_FIELDS.get(extraction.subject_type, frozenset())
    if extraction.field_path not in allowed:
        raise ProblemError(
            "conflict",
            "Conflict",
            detail=(
                f"{extraction.field_path!r} is not an admin-editable field for {extraction.subject_type!r}."
            ),
            instance=request.url.path,
        )
    payload = extraction.payload or {}
    if extraction.field_path not in payload:
        raise ProblemError(
            "conflict",
            "Conflict",
            detail="The extraction payload does not carry a value for its own field_path.",
            instance=request.url.path,
        )

    model = _SUBJECT_MODELS[extraction.subject_type]
    subject = db.get(model, extraction.subject_id)
    if subject is None:
        raise not_found(request.url.path)

    old_value = _jsonable(getattr(subject, extraction.field_path))
    new_value = _coerce_extraction_value(extraction.field_path, payload[extraction.field_path])
    setattr(subject, extraction.field_path, new_value)

    event = record_audit_event(
        db,
        subject_type=extraction.subject_type,
        subject_id=subject.id,
        event_type="extraction_accepted",
        actor=ctx.user,  # type: ignore[arg-type]
        reason=reason,
        before={extraction.field_path: old_value},
        after={extraction.field_path: _jsonable(new_value)},
    )
    extraction.status = "accepted"
    extraction.accepted_by_user_id = ctx.user.id  # type: ignore[union-attr]
    extraction.applied_event_id = event.id
    db.flush()

    return build_envelope(
        _serialize_extraction(db, extraction), meta=_admin_meta(), licence_summary=_empty_licence_summary()
    )


@router.post("/admin/v1/extractions/{extraction_id}/reject")
def admin_reject_extraction(
    extraction_id: str,
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> Any:
    extraction = db.scalar(select(Extraction).where(Extraction.public_id == extraction_id))
    if extraction is None:
        raise not_found(request.url.path)
    reason = (body or {}).get("reason")
    if not reason:
        raise validation_error("reason", "reason is required", request.url.path)
    if extraction.status != "proposed":
        raise ProblemError(
            "conflict",
            "Conflict",
            detail="This extraction has already been decided.",
            instance=request.url.path,
        )

    record_audit_event(
        db,
        subject_type=extraction.subject_type,
        subject_id=extraction.subject_id,
        event_type="admin_edit",
        actor=ctx.user,  # type: ignore[arg-type]
        reason=reason,
        before={"extraction_status": "proposed", "extraction_id": extraction.public_id},
        after={"extraction_status": "rejected", "extraction_id": extraction.public_id},
    )
    extraction.status = "rejected"
    db.flush()

    return build_envelope(
        _serialize_extraction(db, extraction), meta=_admin_meta(), licence_summary=_empty_licence_summary()
    )


__all__ = ["router"]
