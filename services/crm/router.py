"""The inbound Attio webhook and the US-403 lead hand-off route (docs/34-crm-system-of-record.md
§5 "Inbound webhooks"; docs/10 US-403; docs/23 `POST /admin/v1/leads`).

Mounted by the coordinator onto `services.api.app.app` with one `include_router(router)` call
(this module never imports or edits `services/api/app.py`). Both routes depend on `CrmPort`
through `services.sor.wiring.get_crm_port`, exactly like every other port consumer, so tests
override it with `app.dependency_overrides[get_crm_port] = lambda: fake` the same way they
override `get_db`.
"""

from __future__ import annotations

import logging
import re
import uuid as _uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.audit import record_audit_event
from services.api.auth import AuthContext, require_admin
from services.api.common import WEB_HOST, iso, normalise_domain, utcnow
from services.api.deps import get_db
from services.api.errors import ProblemError, not_found, validation_error
from services.api.serialize import build_envelope, build_licence_summary, build_meta
from services.db.models import Account, Match, Opportunity, Proposal
from services.ids import _CROCKFORD, public_id
from services.sor.ports import (
    CompanyRef,
    CompanyUpsert,
    CrmPort,
    DealCreate,
    LeadSignal,
    SorRejected,
    SorUnavailable,
    WebhookRejected,
)
from services.sor.wiring import get_crm_port

logger = logging.getLogger(__name__)

router = APIRouter()

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _find_match_by_public_id(db: Session, value: str) -> Match | None:
    """`mat_<crockford>` ids are synthesised from `match.id` (docs/21 §3.11 has no separate
    `public_id` column on `match`, same situation as `event` — mirrors `services/api/app.py`'s
    `_find_event_by_public_id`)."""
    if not value.startswith("mat_"):
        return None
    digits = value[4:]
    try:
        n = 0
        for ch in digits:
            n = n * 32 + _CROCKFORD.index(ch)
        candidate = _uuid.UUID(int=n)
    except (ValueError, OverflowError):
        return None
    return db.get(Match, candidate)


def _first_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    return _SENTENCE_END.split(text, maxsplit=1)[0].strip()


# ------------------------------------------------------------------------ inbound Attio webhook
@router.post("/webhooks/attio", status_code=200)
async def receive_attio_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    port: Annotated[CrmPort, Depends(get_crm_port)],
) -> dict[str, int]:
    """No session auth — the HMAC signature verified inside `port.parse_webhook` is the
    credential (docs/34 §5). `applied` counts changes that actually caused a platform-side write:
    today only `company.updated` can (refreshing a cached `account.name`); `lead_signal.updated`
    is logged but never increments it until the admin-panel wave adds the task table that
    `handled` would close (services/crm/README.md "deferred")."""
    raw_body = await request.body()
    try:
        changes = port.parse_webhook(body=raw_body, headers=dict(request.headers))
    except WebhookRejected as exc:
        logger.warning("attio_webhook_rejected error=%s", exc)
        raise ProblemError("unauthenticated", "Webhook signature invalid", instance=request.url.path) from exc

    applied = 0
    try:
        for change in changes:
            if change.kind == "company.updated":
                company = port.get_company(CompanyRef(sor_kind=port.sor_kind, sor_ref=change.sor_ref))
                if company is None or company.name is None:
                    continue
                accounts = db.scalars(select(Account).where(Account.sor_ref == change.sor_ref)).all()
                for account in accounts:
                    if account.name != company.name:
                        account.name = company.name
                        applied += 1
            elif change.kind == "lead_signal.updated":
                logger.info("attio_lead_signal_webhook_received sor_ref=%s", change.sor_ref)
    except SorUnavailable as exc:
        logger.warning("attio_webhook_sor_unavailable error=%s", exc)
        raise ProblemError(
            "sor_unavailable",
            "CRM adapter unavailable",
            detail="The CRM could not be reached; try again shortly.",
            instance=request.url.path,
        ) from exc

    return {"received": len(changes), "applied": applied}


# -------------------------------------------------------------------------- US-403 lead hand-off
@router.post("/admin/v1/leads", status_code=201)
def admin_create_lead(
    request: Request,
    body: dict[str, Any],
    db: Annotated[Session, Depends(get_db)],
    port: Annotated[CrmPort, Depends(get_crm_port)],
    ctx: Annotated[AuthContext, Depends(require_admin())],
) -> dict[str, Any]:
    """`api/openapi.yaml` `LeadCreate`/`LeadResponse` (lines ~3770-3815, ~9848-9895). Writes a
    CRM lead through the adapter with proposal id, opportunity id, score, rationale and a link
    back; the app itself stores only `match.crm_lead_ref` plus a `lead_created` audit event
    (US-403 AC1) — never a copy of the lead."""
    match_public_id = body.get("match_id")
    reason = body.get("reason")
    account_id = body.get("account_id")
    notes = body.get("notes")

    if not isinstance(match_public_id, str) or not match_public_id:
        raise validation_error("match_id", "match_id is required", request.url.path)
    if not isinstance(reason, str) or not reason.strip():
        raise validation_error("reason", "reason is required", request.url.path)

    match = _find_match_by_public_id(db, match_public_id)
    if match is None or match.status != "active":
        raise not_found(request.url.path)
    if match.crm_lead_ref:
        raise ProblemError(
            "conflict",
            "Lead already created",
            detail="This match already has a CRM lead reference.",
            instance=request.url.path,
        )

    proposal = db.get(Proposal, match.proposal_id)
    opportunity = db.get(Opportunity, match.opportunity_id)
    if proposal is None or opportunity is None:
        raise not_found(request.url.path)

    account: Account | None = None
    if account_id is not None:
        if not isinstance(account_id, str):
            raise validation_error("account_id", "account_id must be a string", request.url.path)
        account = db.scalar(select(Account).where(Account.public_id == account_id))
        if account is None:
            raise not_found(request.url.path)

    org = proposal.sponsor
    domain = normalise_domain(org.website) if org is not None else None
    platform_org_id = org.public_id if org is not None else None

    subject_name = f"{proposal.name_canonical} ↔ {opportunity.title}"
    subject_url = f"{WEB_HOST}/proposals/{proposal.slug}"
    rationale = _first_sentence(match.rationale_text) or "Matched proposal and opportunity."
    signal_id = public_id("mat", match.id)

    signal = LeadSignal(
        signal_id=signal_id,
        event_type="match.new",
        subject_kind="match",
        subject_name=subject_name,
        subject_url=subject_url,
        observed_at=utcnow(),
        score=round(float(match.score) * 100),
        rationale=rationale,
        company_domain=domain,
        platform_org_id=platform_org_id,
    )
    deal_name_subject = org.name_canonical if org is not None else proposal.name_canonical
    deal_name = f"{deal_name_subject} × {opportunity.title}"

    try:
        # docs/34 §5: only upsert a company when there is a domain to key it on
        # (`CompanyUpsert.domain` is required); without one the signal/deal below still go
        # through and land on Attio's Unmatched list if `platform_org_id` cannot resolve either
        # — that is expected for a sponsor with no public website, not an error (README D-9).
        if domain is not None:
            port.upsert_company(
                CompanyUpsert(
                    domain=domain,
                    platform_org_id=platform_org_id,
                    platform_account_id=account.public_id if account is not None else None,
                )
            )
        port.create_lead_signal(signal)
        deal_ref = port.create_deal(
            DealCreate(
                name=deal_name,
                proposal_public_id=proposal.public_id,
                opportunity_public_id=opportunity.public_id,
                score=float(match.score),
                rationale=rationale,
                link_url=subject_url,
                company_domain=domain,
                platform_org_id=platform_org_id,
                originating_signal_id=signal_id,
            )
        )
    except SorUnavailable as exc:
        logger.warning("crm_lead_sor_unavailable match_public_id=%s error=%s", match_public_id, exc)
        raise ProblemError(
            "sor_unavailable",
            "CRM adapter unavailable",
            detail="The CRM could not be reached; try again shortly.",
            instance=request.url.path,
        ) from exc
    except SorRejected as exc:
        logger.warning("crm_lead_rejected match_public_id=%s error=%s", match_public_id, exc)
        raise ProblemError("conflict", "The CRM rejected this lead", instance=request.url.path) from exc

    match.crm_lead_ref = deal_ref.sor_ref
    db.flush()

    if ctx.user is None:  # unreachable: require_admin() already refused an unauthenticated caller
        raise ProblemError("unauthenticated", "An operator session is required", instance=request.url.path)

    audit_after: dict[str, Any] = {"crm_lead_ref": deal_ref.sor_ref, "sor_kind": deal_ref.sor_kind}
    if notes:
        audit_after["notes"] = notes
    event = record_audit_event(
        db,
        subject_type="match",
        subject_id=match.id,
        event_type="lead_created",
        actor=ctx.user,
        reason=reason,
        after=audit_after,
    )

    data = {
        "match_id": match_public_id,
        "crm_lead_ref": deal_ref.sor_ref,
        "sor_kind": deal_ref.sor_kind,
        "open_in_crm_url": None,
        "created_at": iso(utcnow()),
        "event_id": public_id("evt", event.id),
    }
    return build_envelope(
        data,
        meta=build_meta(lag_days=0, tier=ctx.entitlement),
        licence_summary=build_licence_summary([]),
    )


__all__ = ["router"]
