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
from services.api.lines import (
    DETAIL_ZOOM,
    Parts,
    decimals_for_tolerance,
    km_to_miles,
    parse_line_parts,
    parts_length_km,
    parts_to_geojson,
    round_parts,
    simplify_parts,
    tolerance_for_zoom,
)
from services.api.slippage import proposal_slip
from services.db.models import (
    Account,
    Alert,
    ApiKey,
    Asset,
    AssetOwner,
    AssetSource,
    Event,
    Licence,
    Location,
    Opportunity,
    OpportunitySource,
    Organization,
    Proposal,
    ProposalSource,
    SavedSearch,
    Source,
    User,
    WebhookDelivery,
    WebhookEndpoint,
)
from services.ingest.lag import RECORD_LAG_DAYS, Kind
from services.ingest.vintage import UNDETERMINED as VINTAGE_UNDETERMINED
from services.ingest.vintage import label_for as vintage_label


# --------------------------------------------------------------------------------- envelope
def build_meta(
    kind: Kind | None = None,
    *,
    lag_days: int | None = None,
    tier: str = "public",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`tier` is this sprint's addition (Pro tier and alerts, task item 2): Pro/API callers get
    `meta.tier` and `meta.lag_days = 0` reflecting the entitlement resolved for their request
    (`services/api/auth.py` `AuthContext.entitlement`), not a hardcoded `"public"` — `docs/23` §10
    "every envelope" states tier honestly per caller, and `docs/04` D-3/D-28's delayed-tier
    messaging depends on it being true.

    Since the paywall became a matter of shape rather than time (owner, 2026-09-19) `kind` no
    longer selects a lag: a `proposal` and an `opportunity` are both `RECORD_LAG_DAYS` (zero) on
    every tier, the free one included. The parameter stays because every record route passes it to
    say what it is returning, and because the one shape that *is* still delayed — a change event
    from an ISO queue register — is not a `Kind` and passes its own `lag_days` explicitly
    (`services/api/app.py::_event_lag_days`)."""
    if lag_days is None:
        lag_days = RECORD_LAG_DAYS
    now = utcnow()
    data_as_of = now - dt.timedelta(days=lag_days)
    meta: dict[str, Any] = {
        "tier": tier,
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
    """Restricted-precision rule applied here, once, for every record surface that embeds a
    location (proposal list/detail, opportunities, Pro exports, nearby-proposals): an `exact` row
    under a licence with `allows_raw_publication = false` is served at its region grade with
    `precision_reason = licence` and its stored coordinate is never read
    (`services/api/geo.py::effective_placement`; 2026-09-18 audit, docs/50 §3.1)."""
    from services.api.geo import effective_placement

    placement = effective_placement(loc)
    geom = None
    if placement.geom is not None:
        lon, lat = placement.geom
        geom = {"type": "Point", "coordinates": [lon, lat]}
    return {
        "kind": loc.kind,
        "geom": geom,
        "precision": placement.precision,
        "precision_reason": "licence" if placement.downgraded else loc.precision_reason,
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


def location_redactions(record_public_id: str, loc: Location | None) -> list[dict[str, Any]]:
    """The envelope's `redactions[]` entry for a licence-downgraded location (api/openapi.yaml
    `Redaction`, reason `licence_precision`: "exact geometry replaced by a centroid"). Empty when
    the record has no location or its location is served as stored."""
    from services.api.geo import effective_placement

    if loc is None or not effective_placement(loc).downgraded:
        return []
    return [
        {
            "public_id": record_public_id,
            "field": "location.geom",
            "reason": "licence_precision",
            "source_id": loc.source_id,
            "note": "exact coordinate withheld under the source licence; region centroid returned",
        }
    ]


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
        # Derived at read time against today's date, never stored (services/api/slippage.py);
        # `None` on every record that promised nothing or is not past its own date by the grace
        # period, so absence never has to be read as "on schedule".
        "schedule_slip": proposal_slip(proposal),
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


# --------------------------------------------------------------------------------------- assets
def asset_source_row(asset: Asset) -> dict[str, Any]:
    """The asset's own quartet as a provenance row that satisfies `AssetProvenanceRow` (never just
    `ProvenanceQuartet`): this *is* the primary source, by construction, whether or not an
    `asset_source` link row exists for it yet (`asset_provenance_rows` falls back to this for
    every asset type `asset_source` has not been backfilled for -- all but `ethanol_plant` today,
    docs/24 §5(a))."""
    row = provenance_quartet(
        asset.source, asset.licence, source_url=asset.source_url, retrieved_at=asset.retrieved_at
    )
    row.update({"is_primary": True, "match_method": "deterministic_key", "match_score": None})
    return row


def asset_provenance_row(link: AssetSource) -> dict[str, Any]:
    row = provenance_quartet(
        link.source, link.licence, source_url=link.source_url, retrieved_at=link.retrieved_at
    )
    row.update(
        {
            "is_primary": link.is_primary,
            "match_method": link.match_method,
            "match_score": float(link.match_score) if link.match_score is not None else None,
        }
    )
    return row


def asset_provenance_rows(asset: Asset, *, sources: list[AssetSource] | None = None) -> list[dict[str, Any]]:
    """Every link this asset has, primary first (`AssetSource.sources`'s own ordering) -- or, when
    none exist yet, the asset's own single quartet exactly as `serialize_asset` rendered it before
    `asset_source` existed. Never both: an asset that has been resolved should not also print its
    own columns as a phantom extra source."""
    links = sources if sources is not None else asset.sources
    if links:
        return [asset_provenance_row(link) for link in links]
    return [asset_source_row(asset)]


def serialize_asset_owner(edge: AssetOwner) -> dict[str, Any]:
    return {
        "organization": serialize_organization_summary(edge.organization),
        "role": edge.role,
        "share_pct": float(edge.share_pct) if edge.share_pct is not None else None,
        "as_of": iso(edge.as_of),
        "owner_name_raw": edge.owner_name_raw,
        "provenance": provenance_quartet(
            edge.source, edge.licence, source_url=edge.source_url, retrieved_at=edge.retrieved_at
        ),
    }


def serialize_asset_summary(asset: Asset) -> dict[str, Any]:
    return {
        "public_id": asset.public_id,
        "slug": asset.slug,
        "url": f"{WEB_HOST}/assets/{asset.slug}",
        "asset_type": asset.asset_type,
        "name": asset.name,
        "technology": asset.technology,
        "capacity_mw": float(asset.capacity_mw) if asset.capacity_mw is not None else None,
        "state_code": asset.state_code,
        "country": asset.country,
    }


def serialize_asset(
    asset: Asset,
    *,
    owners: list[AssetOwner] | None = None,
    sources: list[AssetSource] | None = None,
    include_owners: bool = True,
    include_geometry: bool = True,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "public_id": asset.public_id,
        "slug": asset.slug,
        "url": f"{WEB_HOST}/assets/{asset.slug}",
        "asset_type": asset.asset_type,
        "name": asset.name,
        "operator_name": asset.operator_name,
        "status": asset.status,
        "technology": asset.technology,
        "technology_raw": asset.technology_raw,
        "technologies": asset.technologies or {},
        "capacity_mw": float(asset.capacity_mw) if asset.capacity_mw is not None else None,
        "capacity_value": float(asset.capacity_value) if asset.capacity_value is not None else None,
        "capacity_unit": asset.capacity_unit,
        "commissioned_year": asset.commissioned_year,
        "unit_count": asset.unit_count,
        "attributes": asset.attributes or {},
        "state_code": asset.state_code,
        "county_name": asset.county_name,
        "county_fips": asset.county_fips,
        "country": asset.country,
        "first_seen": iso(asset.first_seen),
        "last_changed": iso(asset.last_changed),
        "length_miles": asset_length_miles(asset),
        "provenance": asset_provenance_rows(asset, sources=sources),
    }
    if include_geometry:
        out["geometry"] = asset_geometry(asset)
    if include_owners:
        out["owners"] = [serialize_asset_owner(o) for o in (owners if owners is not None else asset.owners)]
    return out


def asset_line_parts(asset: Asset) -> Parts | None:
    """`asset.geom_line` as parts (`services/api/lines.py`), or `None`. The ORM hands back a list of
    `(lon, lat)` pairs on SQLite; a Postgres `WKBElement` is not parsed here -- the geo index and the
    nearby endpoints read lines through `ST_AsGeoJSON` in SQL instead, and a detail response on
    Postgres goes through the same `parse_line_parts` once the row's line is fetched as GeoJSON
    (`services/api/assets.py::_line_parts_for`)."""
    raw = asset.geom_line
    if raw is None or not isinstance(raw, (str, list, tuple, dict)):
        return None
    return parse_line_parts(raw)


def asset_geometry(asset: Asset, *, parts: Parts | None = None) -> dict[str, Any] | None:
    """The asset's GeoJSON geometry for a detail response: the line (simplified at
    `DETAIL_ZOOM`'s tolerance, ~75 m) for a line asset, else its representative point, else
    `None`. Withheld -- `None`, with a `redactions[]` row from `asset_geometry_redactions` --
    when the asset's licence forbids raw publication (`services/api/visibility.py::
    asset_geometry_visible`, the generic gate; no source in scope today triggers it)."""
    from services.api.visibility import asset_geometry_visible

    if not asset_geometry_visible(asset):
        return None
    line = parts if parts is not None else asset_line_parts(asset)
    if line is not None:
        tolerance = tolerance_for_zoom(DETAIL_ZOOM)
        return parts_to_geojson(
            round_parts(simplify_parts(line, tolerance), decimals_for_tolerance(tolerance))
        )
    if asset.geom is not None and isinstance(asset.geom, (list, tuple)):
        lon, lat = asset.geom
        return {"type": "Point", "coordinates": [float(lon), float(lat)]}
    return None


def asset_length_miles(asset: Asset, *, parts: Parts | None = None) -> float | None:
    """`attributes.length_miles` or `attributes.miles` as the registry states it, else the geodesic
    length of the stored line (the objective derivation ADR 0008 §4 allows), else `None` for a
    point asset."""
    for key in ("length_miles", "miles"):  # the brief's name, then EIA Atlas's as the data lane records it
        stated = (asset.attributes or {}).get(key)
        if isinstance(stated, (int, float)) and not isinstance(stated, bool):
            return round(float(stated), 1)
    line = parts if parts is not None else asset_line_parts(asset)
    if line is None:
        return None
    return round(km_to_miles(parts_length_km(line)), 1)


def asset_geometry_redactions(asset: Asset) -> list[dict[str, Any]]:
    """The envelope's `redactions[]` entry when `asset_geometry` withheld the coordinates."""
    from services.api.visibility import asset_geometry_visible

    if asset_geometry_visible(asset) or (asset.geom is None and asset.geom_line is None):
        return []
    return [
        {
            "public_id": asset.public_id,
            "field": "geometry",
            "reason": "licence",
            "source_id": asset.source_id,
            "note": "coordinates withheld under the source licence; derived fields returned",
        }
    ]


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
        "implemented": source.implemented,
        "last_success_at": iso(source.last_success_at),
        # The release the SOURCE states, which is not the date we fetched it. `last_success_at`
        # above and `retrieved_at` on every link row are ours; this one is theirs, and the two
        # were conflated until migration 0018 (`services/ingest/vintage.py`). `basis` is
        # `undetermined` when no load has resolved it and `not_stated` when the source publishes
        # no release label -- never a fetch date standing in for one.
        "vintage": {
            "value": source.vintage,
            "label": vintage_label(source.vintage),
            "basis": source.vintage_basis or VINTAGE_UNDETERMINED,
            "stated": source.vintage is not None,
        },
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


# ----------------------------------------------------------------------- Pro tier and alerts
def serialize_user(user: User, *, account_public_id: str) -> dict[str, Any]:
    """`api/openapi.yaml` `User` (docs/21 §3.12). `sor_ref` is admin-only per the schema's own
    description — omitted from the shape a user sees of themselves via `/v1/me`, not just from
    the field's meaning; callers needing the admin view build their own dict."""
    return {
        "user_id": user.public_id,
        "account_id": account_public_id,
        "email": user.email,
        "email_verified_at": iso(user.email_verified_at),
        "name": user.name,
        "role": user.role,
        "status": user.status,
        "auth_provider": user.auth_provider,
        "mfa_enforced": user.mfa_enforced,
        "marketing_consent": user.marketing_consent,
        "consent_version": user.consent_version,
        "tos_version": user.tos_version,
        "last_login_at": iso(user.last_login_at),
        "anonymised_at": iso(user.anonymised_at),
        "created_at": iso(user.created_at),
    }


def serialize_account(account: Account, *, organization: Organization | None = None) -> dict[str, Any]:
    return {
        "account_id": account.public_id,
        "name": account.name,
        "kind": account.kind,
        "organization": serialize_organization_summary(organization) if organization else None,
        "entitlement": account.entitlement,
        "entitlement_source": account.entitlement_source,
        "entitlement_checked_at": iso(account.entitlement_checked_at),
        "entitlement_stale": account.entitlement_stale,
        "seats": account.seats,
        "seats_used": account.seats_used,
        "sor_kind": account.sor_kind,
        "status": account.status,
    }


def serialize_saved_search(search: SavedSearch) -> dict[str, Any]:
    rss_url = f"{WEB_HOST}/feeds/saved/{search.rss_token}" if search.rss_token else None
    return {
        "saved_search_id": search.public_id,
        "name": search.name,
        "entity": search.entity,
        "query": search.query,
        "delivery_mode": search.delivery_mode,
        "channels": search.channels,
        "rss_url": rss_url,
        "last_run_at": iso(search.last_run_at),
        "watermark_seq": search.watermark_seq,
        "last_match_count": search.last_match_count,
        "status": search.status,
        "created_at": iso(search.created_at),
        "updated_at": iso(search.updated_at),
    }


def serialize_alert(alert: Alert, *, event_ids: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "alert_id": alert.public_id,
        "saved_search_id": alert.saved_search.public_id,
        "channel": alert.channel,
        "mode": alert.mode,
        "window_start": iso(alert.window_start),
        "window_end": iso(alert.window_end),
        "event_seqs": alert.event_seqs,
        "recipient": alert.recipient,
        "subject": alert.subject,
        "provider_message_id": alert.provider_message_id,
        "status": alert.status,
        "sent_at": iso(alert.sent_at),
        "error": alert.error,
        "created_at": iso(alert.created_at),
    }
    if event_ids is not None:
        out["event_ids"] = event_ids
    return out


def serialize_api_key(key: ApiKey, *, account_public_id: str, created_by_public_id: str) -> dict[str, Any]:
    return {
        "key_id": key.public_id,
        "account_id": account_public_id,
        "created_by_user_id": created_by_public_id,
        "name": key.name,
        "prefix": key.prefix,
        "last4": key.last4,
        "scopes": key.scopes,
        "tier": key.tier,
        "rate_limit_per_hour": key.rate_limit_per_hour,
        "daily_quota": key.daily_quota,
        "expires_at": iso(key.expires_at),
        "last_used_at": iso(key.last_used_at),
        "last_used_ip": key.last_used_ip,
        "revoked_at": iso(key.revoked_at),
        "licence_accepted_version": key.licence_accepted_version,
        "licence_accepted_at": iso(key.licence_accepted_at),
        "created_at": iso(key.created_at),
    }


def serialize_webhook_endpoint(endpoint: WebhookEndpoint) -> dict[str, Any]:
    return {
        "webhook_id": endpoint.public_id,
        "url": endpoint.url,
        "description": endpoint.description,
        "types": endpoint.types,
        "query": endpoint.query,
        "entity": endpoint.entity,
        "status": endpoint.status,
        "consecutive_failures": endpoint.consecutive_failures,
        "last_delivery_at": iso(endpoint.last_delivery_at),
        "last_success_at": iso(endpoint.last_success_at),
        "secret_rotated_at": iso(endpoint.secret_rotated_at),
        "created_at": iso(endpoint.created_at),
    }


def serialize_webhook_delivery(delivery: WebhookDelivery, *, event_public_id: str | None) -> dict[str, Any]:
    return {
        "delivery_id": delivery.public_id,
        "webhook_id": delivery.endpoint.public_id,
        "type": delivery.type,
        "event_id": event_public_id,
        "event_seq": delivery.event_seq,
        "attempt": delivery.attempt,
        "status": delivery.status,
        "response_status": delivery.response_status,
        "response_body_excerpt": delivery.response_body_excerpt,
        "latency_ms": delivery.latency_ms,
        "error_class": delivery.error_class,
        "next_attempt_at": iso(delivery.next_attempt_at),
        "created_at": iso(delivery.created_at),
    }
