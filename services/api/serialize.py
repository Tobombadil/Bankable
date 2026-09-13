"""Response dict builders matching the schemas in `api/openapi.yaml` `components/schemas`
(docs/04-standards.md API-5: the attribution envelope on every response).

These build plain `dict`/`list` structures (not a parallel Pydantic model tree) because the
authority for shape is the committed OpenAPI file; `tests/test_api_contract.py` validates every
response against it directly with `jsonschema`, which is a stronger guarantee against drift than
hand-duplicating the same schema in Pydantic.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from services.api.common import TERMS_URL, WEB_HOST, iso, utcnow
from services.db.models import (
    Event,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    Source,
)
from services.ingest.lag import LAG_DAYS_BY_KIND, Kind


# --------------------------------------------------------------------------------- envelope
def build_meta(
    kind: Kind | None = None, *, lag_days: int | None = None, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    if lag_days is None:
        lag_days = LAG_DAYS_BY_KIND[kind] if kind else 0
    now = utcnow()
    data_as_of = now - dt.timedelta(days=lag_days)
    meta: dict[str, Any] = {
        "tier": "public",
        "lag_days": lag_days,
        "data_as_of": iso(data_as_of),
        "generated_at": iso(now),
        "request_id": "req_" + now.strftime("%Y%m%d%H%M%S%f")[-16:].lower(),
        "terms_url": TERMS_URL,
    }
    if extra:
        meta.update(extra)
    return meta


def build_page(next_cursor: str | None, prev_cursor: str | None, has_more: bool) -> dict[str, Any]:
    return {"next_cursor": next_cursor, "prev_cursor": prev_cursor, "has_more": has_more}


def build_licence_summary(source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """`source_rows`: one dict per record actually in the payload, each carrying `source_id`,
    `name`, `operator`, `licence_id`, `licence_name`, `licence_url`, `reuse_class`,
    `attribution_text`, `requires_link_back`, `retrieved_at` (docs/23 §10)."""
    by_source: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        sid = row["source_id"]
        entry = by_source.setdefault(
            sid,
            {
                "source_id": sid,
                "name": row["name"],
                "operator": row.get("operator"),
                "licence_id": row["licence_id"],
                "licence_name": row["licence_name"],
                "licence_url": row.get("licence_url"),
                "reuse_class": row["reuse_class"],
                "attribution_text": row.get("attribution_text"),
                "requires_link_back": row["requires_link_back"],
                "record_count": 0,
                "retrieved_at_max": row["retrieved_at"],
            },
        )
        entry["record_count"] += 1
        if row["retrieved_at"] and (
            not entry["retrieved_at_max"] or row["retrieved_at"] > entry["retrieved_at_max"]
        ):
            entry["retrieved_at_max"] = row["retrieved_at"]

    sources = list(by_source.values())
    for s in sources:
        s["retrieved_at_max"] = (
            iso(s["retrieved_at_max"])
            if isinstance(s["retrieved_at_max"], dt.datetime)
            else s["retrieved_at_max"]
        )
    return _finalize_licence_summary(sources)


def _finalize_licence_summary(sources: list[dict[str, Any]]) -> dict[str, Any]:
    names = [s["name"] for s in sources]
    attribution_line = f"Sources: {'; '.join(names)}." if names else "Sources: none in this response."
    redistribution = (
        "Derived fields may be redistributed under each source's licence; attribution is required "
        "wherever a credit line is shown; raw source rows are shown only where the licence allows it."
    )
    return {"sources": sources, "attribution_line": attribution_line, "redistribution": redistribution}


_SourceLicenceAggregateRow = tuple[
    str, str, str | None, str, str, str | None, str, str | None, bool, dt.datetime | None, int
]


def licence_summary_from_source_aggregates(
    rows: list[_SourceLicenceAggregateRow],
) -> dict[str, Any]:
    """Same output shape as `build_licence_summary`, built from a `GROUP BY source_id` SQL
    aggregate instead of one Python dict per record (`services/api/app.py`'s geo endpoints, which
    would otherwise need every visible record's `ProposalSource`/`OpportunitySource` ORM row
    materialised just to compute this — the dominant remaining cost after the visibility-index fix
    over the full ~10,400-row set, services/README.md "Sprint 2 fixes"). Each row: `(source_id,
    name, operator, licence_id, licence_name, licence_url, reuse_class, attribution_text,
    requires_link_back, retrieved_at_max, record_count)`.
    """
    sources = [
        {
            "source_id": source_id,
            "name": name,
            "operator": operator,
            "licence_id": licence_id,
            "licence_name": licence_name,
            "licence_url": licence_url,
            "reuse_class": reuse_class,
            "attribution_text": attribution_text,
            "requires_link_back": requires_link_back,
            "record_count": record_count,
            "retrieved_at_max": iso(retrieved_at_max),
        }
        for (
            source_id,
            name,
            operator,
            licence_id,
            licence_name,
            licence_url,
            reuse_class,
            attribution_text,
            requires_link_back,
            retrieved_at_max,
            record_count,
        ) in rows
    ]
    return _finalize_licence_summary(sources)


def build_envelope(
    data: Any,
    *,
    meta: dict[str, Any],
    licence_summary: dict[str, Any],
    redactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {"data": data, "meta": meta, "licence_summary": licence_summary, "redactions": redactions or []}


def build_list_envelope(
    data: list[Any],
    *,
    meta: dict[str, Any],
    licence_summary: dict[str, Any],
    page: dict[str, Any],
    redactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    env = build_envelope(data, meta=meta, licence_summary=licence_summary, redactions=redactions)
    env["page"] = page
    return env


# --------------------------------------------------------------------------------- provenance
def licence_summary_row(source: Source, licence: Licence, retrieved_at: dt.datetime | None) -> dict[str, Any]:
    return {
        "source_id": source.id,
        "name": source.name,
        "operator": source.operator,
        "licence_id": licence.id,
        "licence_name": licence.name,
        "licence_url": licence.url,
        "reuse_class": licence.reuse_class,
        "attribution_text": source.attribution_text or licence.attribution_text,
        "requires_link_back": licence.requires_link_back,
        "retrieved_at": retrieved_at,
    }


def event_licence_row(event: Event) -> dict[str, Any] | None:
    """`licence_summary_row` for a pipeline event, or `None` for a user-actor event (whose
    `source`/`licence` are null by design, docs/21 §3.10)."""
    if event.source is not None and event.licence is not None:
        return licence_summary_row(event.source, event.licence, event.retrieved_at)
    return None


def provenance_quartet(
    source: Source, licence: Licence, *, source_url: str, retrieved_at: dt.datetime
) -> dict[str, Any]:
    return {
        "source_id": source.id,
        "source_name": source.name,
        "source_url": source_url,
        "retrieved_at": iso(retrieved_at),
        "licence_id": licence.id,
        "reuse_class": licence.reuse_class,
        "attribution_text": source.attribution_text or licence.attribution_text,
    }


def provenance_row(link: ProposalSource | OpportunitySource, source: Source) -> dict[str, Any]:
    row = provenance_quartet(
        source, source.licence, source_url=link.source_url, retrieved_at=link.retrieved_at
    )
    allow_raw = source.licence.allows_raw_publication
    row.update(
        {
            "source_record_id": link.source_record_id
            if allow_raw or source.licence.reuse_class == "open"
            else None,
            "first_seen": iso(link.first_seen),
            "last_seen": iso(link.last_seen),
            "gone_at": iso(link.gone_at),
            "link_method": link.link_method,
            "link_confidence": float(link.link_confidence),
            "active": link.active,
        }
    )
    return row


# --------------------------------------------------------------------------------- entities
def serialize_organization_summary(org: Organization) -> dict[str, Any]:
    return {
        "public_id": org.public_id,
        "slug": org.slug,
        "name_canonical": org.name_canonical,
        "type": org.type,
        "url": f"{WEB_HOST}/organizations/{org.slug}",
    }


def serialize_location(loc: Location) -> dict[str, Any]:
    geom = None
    if loc.geom is not None:
        lon, lat = loc.geom
        geom = {"type": "Point", "coordinates": [lon, lat]}
    return {
        "kind": loc.kind,
        "geom": geom,
        "precision": loc.precision,
        "precision_reason": loc.precision_reason,
        "county_fips": loc.county_fips,
        "county_name": loc.county_name,
        "state_code": loc.state_code,
        "country": loc.country,
        "raw_place": loc.raw_place if loc.licence.allows_raw_publication else None,
        "geocoder": loc.geocoder,
        "provenance": provenance_quartet(
            loc.source, loc.licence, source_url=loc.source_url, retrieved_at=loc.retrieved_at
        ),
    }


def serialize_proposal(proposal: Proposal, *, sources: list[ProposalSource] | None = None) -> dict[str, Any]:
    if sources is None:
        sources = [s for s in proposal.sources if s.active]
    out: dict[str, Any] = {
        "public_id": proposal.public_id,
        "slug": proposal.slug,
        "url": f"{WEB_HOST}/proposals/{proposal.slug}",
        "kind": proposal.kind,
        "name_canonical": proposal.name_canonical,
        "sponsor": serialize_organization_summary(proposal.sponsor) if proposal.sponsor else None,
        "technology": proposal.technology,
        "technology_raw": proposal.technology_raw,
        "capacity_mw": float(proposal.capacity_mw) if proposal.capacity_mw is not None else None,
        "storage_mwh": float(proposal.storage_mwh) if proposal.storage_mwh is not None else None,
        "jurisdiction": proposal.jurisdiction,
        "iso": proposal.iso,
        "location": serialize_location(proposal.location) if proposal.location else None,
        "lifecycle_state": proposal.lifecycle_state,
        "status_raw": proposal.status_raw,
        "identifiers": proposal.identifiers or {},
        "proposed_online_date": iso(proposal.proposed_online_date),
        "first_seen": iso(proposal.first_seen),
        "last_changed": iso(proposal.last_changed),
        "min_reuse_class": proposal.min_reuse_class,
        "source_count": proposal.source_count,
        "created_by": proposal.created_by,
        "resolution_confidence": float(proposal.resolution_confidence)
        if proposal.resolution_confidence is not None
        else None,
        "merged_into": None,
        "provenance": [provenance_row(s, s.source) for s in (sources or [])],
    }
    return out


def serialize_opportunity(
    opportunity: Opportunity, *, sources: list[OpportunitySource] | None = None
) -> dict[str, Any]:
    if sources is None:
        sources = [s for s in opportunity.sources if s.active]
    return {
        "public_id": opportunity.public_id,
        "slug": opportunity.slug,
        "url": f"{WEB_HOST}/opportunities/{opportunity.slug}",
        "kind": opportunity.kind,
        "issuer": serialize_organization_summary(opportunity.issuer) if opportunity.issuer else None,
        "title": opportunity.title,
        "summary": opportunity.summary,
        "jurisdiction": opportunity.jurisdiction,
        "technologies": opportunity.technologies or [],
        "capacity_sought_mw": float(opportunity.capacity_sought_mw)
        if opportunity.capacity_sought_mw is not None
        else None,
        "budget_amount": float(opportunity.budget_amount) if opportunity.budget_amount is not None else None,
        "budget_currency": opportunity.budget_currency,
        "open_at": iso(opportunity.open_at),
        "due_at": iso(opportunity.due_at),
        "status": opportunity.status,
        "status_raw": opportunity.status_raw,
        "location": serialize_location(opportunity.location) if opportunity.location else None,
        "identifiers": opportunity.identifiers or {},
        "first_seen": iso(opportunity.first_seen),
        "last_changed": iso(opportunity.last_changed),
        "min_reuse_class": opportunity.min_reuse_class,
        "source_count": opportunity.source_count,
        "created_by": opportunity.created_by,
        "merged_into": None,
        "provenance": [provenance_row(s, s.source) for s in (sources or [])],
    }


def serialize_organization(
    org: Organization, *, proposal_count: int | None = None, opportunity_count: int | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "public_id": org.public_id,
        "slug": org.slug,
        "url": f"{WEB_HOST}/organizations/{org.slug}",
        "name_canonical": org.name_canonical,
        "type": org.type,
        "country": org.country,
        "jurisdiction": org.jurisdiction,
        "ids": org.ids or {},
        "website": org.website,
        "is_curated_issuer": org.is_curated_issuer,
        "first_seen": iso(org.first_seen),
        "last_changed": iso(org.last_changed),
        "merged_into": None,
        "aliases": [],
        "provenance": [],
    }
    if proposal_count is not None:
        out["proposal_count"] = proposal_count
    if opportunity_count is not None:
        out["opportunity_count"] = opportunity_count
    return out


def serialize_event(
    event: Event, *, subject_public_id: str, subject_name: str, subject_url: str
) -> dict[str, Any]:
    provenance = None
    if event.source is not None and event.licence is not None:
        provenance = provenance_quartet(
            event.source,
            event.licence,
            source_url=event.source_url or event.source.url,
            retrieved_at=event.retrieved_at or event.observed_at,
        )
    out: dict[str, Any] = {
        "id": _event_public_id(event),
        "seq": event.seq,
        "subject_type": event.subject_type,
        "subject_id": subject_public_id,
        "subject": {"public_id": subject_public_id, "name": subject_name, "url": subject_url},
        "event_type": event.event_type,
        "headline": _headline(event, subject_name),
        "observed_at": iso(event.observed_at),
        "recorded_at": iso(event.recorded_at),
        "published_at": iso(event.published_at),
        "public_at": iso(event.public_at),
        "before": event.before,
        "after": event.after,
        "changed_keys": event.changed_keys or [],
        "actor_type": event.actor_type,
        "reason": event.reason,
        "confidence": float(event.confidence) if event.confidence is not None else None,
        "reverses_event_id": None,
        "provenance": provenance,
    }
    return out


def _event_public_id(event: Event) -> str:
    from services.ids import public_id

    return public_id("evt", event.id)


def _headline(event: Event, subject_name: str) -> str:
    after = event.after or {}
    if event.event_type == "status_change" and "lifecycle_state" in after:
        return f"{subject_name}: {after['lifecycle_state']}"
    if event.event_type == "created":
        return f"New record: {subject_name}"
    return f"{subject_name}: {event.event_type}"


def serialize_source(source: Source) -> dict[str, Any]:
    licence = source.licence
    return {
        "source_id": source.id,
        "name": source.name,
        "jurisdiction": source.jurisdiction,
        "category": source.category,
        "operator": source.operator,
        "url": source.url,
        "access": source.access,
        "format": source.format,
        "cadence": source.cadence,
        "tier": source.tier,
        "licence": serialize_licence_embedded(licence),
        "attribution_text": source.attribution_text or licence.attribution_text,
        "publish_state": source.publish_state,
        "lag_days": source.lag_days,
        "lag_overrides": source.lag_overrides or {},
        "implemented": source.implemented,
        "last_success_at": iso(source.last_success_at),
        "provenance": {
            "manifest_version": source.manifest_version or "",
            "manifest_hash": source.manifest_hash or ("0" * 64),
        },
    }


def serialize_licence_embedded(licence: Licence) -> dict[str, Any]:
    return {
        "licence_id": licence.id,
        "name": licence.name,
        "url": licence.url,
        "reuse_class": licence.reuse_class,
        "attribution_required": licence.attribution_required,
        "attribution_text": licence.attribution_text,
        "requires_link_back": licence.requires_link_back,
        "allows_derived_publication": licence.allows_derived_publication,
        "allows_raw_publication": licence.allows_raw_publication,
        "allows_api_redistribution": licence.allows_api_redistribution,
        "allows_bulk_export": licence.allows_bulk_export,
        "allows_commercial_use": licence.allows_commercial_use,
        "share_alike": licence.share_alike,
        "gate_flag": licence.gate_flag,
        "gate_name": licence.gate_name,
        # The free-text licence quote itself (`data/sources.yaml` `license`), public by design —
        # unlike `notes` (data-engineer commentary, sometimes about in-progress legal review, e.g.
        # CAISO's "until counsel resolves..."), which stays admin-only (`AdminLicence` in
        # api/openapi.yaml) and is deliberately not added here.
        "quote_text": licence.quote_text,
    }


def serialize_licence(licence: Licence, *, source_count: int) -> dict[str, Any]:
    out = serialize_licence_embedded(licence)
    out.update(
        {
            "gate_cleared_at": iso(licence.gate_cleared_at),
            "expires_at": iso(licence.expires_at),
            "source_count": source_count,
        }
    )
    return out
