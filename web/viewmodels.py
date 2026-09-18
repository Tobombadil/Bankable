"""Shape API envelope dicts (`services/api/serialize.py`) into what the Jinja templates render,
and the handful of presentation rules that belong to the frontend rather than the API:
lifecycle-family colour grouping (docs/31 §1.2), the default "active states only" map/list filter
(product defect A, `docs/00-PLAN.md` task), and the collapsed provenance panel (product defect C).

Nothing here decides *visibility* -- that is entirely the API's `services/api/visibility.py`
predicate. This module only relabels and regroups fields the API already returned.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from services.api.common import WEB_HOST
from web.api_client import ApiClient, ApiError

Family = Literal["neutral", "progress", "committed", "success", "danger"]

# docs/21 §7.1-§7.2; docs/31 §1.2 five-family grouping. Duplicated from the (now-unused for the
# live path) web/build_data.py table rather than imported from it: build_data.py is retained only
# for the vendored basemap per this task's brief, not as a dependency of the live app.
LIFECYCLE_FAMILY: dict[str, Family] = {
    "announced": "neutral",
    "unknown": "neutral",
    "closed": "neutral",
    "filed": "progress",
    "studied": "progress",
    "permitted": "progress",
    "under_construction": "progress",
    "reinstated": "progress",
    "contracted": "committed",
    "awarded": "committed",
    "built": "success",
    "open": "success",
    "withdrawn": "danger",
    "cancelled": "danger",
    "frozen": "danger",
}

# Product defect A (docs/00-PLAN.md): "default view shows active lifecycle states only
# (announced through under_construction)". `built` and `unknown` are deliberately outside this
# set -- the task's own wording bounds the default to the six states between `announced` and
# `under_construction` inclusive; withdrawn/cancelled sit behind the explicit toggle below.
ACTIVE_PROPOSAL_STATES: tuple[str, ...] = (
    "announced",
    "filed",
    "studied",
    "permitted",
    "contracted",
    "under_construction",
)
WITHDRAWN_PROPOSAL_STATES: tuple[str, ...] = ("withdrawn", "cancelled")
# ADR 0008 sitemap task: every lifecycle state a proposal can carry, `built`/`unknown` included --
# unlike ACTIVE_PROPOSAL_STATES (product defect A's default view), the sitemap must list every
# published proposal's page regardless of which lifecycle-state bucket it is in.
ALL_PROPOSAL_LIFECYCLE_STATES: tuple[str, ...] = (
    ACTIVE_PROPOSAL_STATES
    + WITHDRAWN_PROPOSAL_STATES
    + (
        "built",
        "unknown",
    )
)

ALL_OPPORTUNITY_STATUSES: tuple[str, ...] = (
    "unknown",
    "announced",
    "open",
    "frozen",
    "reinstated",
    "closed",
    "cancelled",
    "awarded",
)

WORLD_BBOX = "-179,-85,179,85"


def resolve_proposal_lifecycle_param(qp: Mapping[str, str]) -> tuple[str, bool, bool]:
    """Return `(lifecycle_state_csv, explicit, include_withdrawn)` for a request.

    If the caller passed `lifecycle_state=` explicitly (e.g. from a saved link, or picking one
    state in `/proposals`), that set wins verbatim -- same "explicit overrides the default"
    pattern the opportunities list already used for `status=all` before this task. Otherwise the
    default is `ACTIVE_PROPOSAL_STATES`, extended with `WITHDRAWN_PROPOSAL_STATES` when
    `include_withdrawn` is truthy.
    """
    explicit_value = qp.get("lifecycle_state")
    if explicit_value:
        return explicit_value, True, _is_truthy(qp.get("include_withdrawn"))
    include_withdrawn = _is_truthy(qp.get("include_withdrawn"))
    states = list(ACTIVE_PROPOSAL_STATES)
    if include_withdrawn:
        states += list(WITHDRAWN_PROPOSAL_STATES)
    return ",".join(states), False, include_withdrawn


def _is_truthy(value: str | None) -> bool:
    return (value or "").lower() in ("1", "true", "yes", "on")


def opportunity_status_param(qp: Mapping[str, str]) -> str:
    """`status=all` means "every status" -- the API itself only knows a literal CSV list (it
    defaults to `open` if the parameter is absent), so the web layer expands `all` to the full
    vocabulary rather than passing the literal word through.
    """
    value = qp.get("status", "open")
    if value == "all":
        return ",".join(ALL_OPPORTUNITY_STATUSES)
    return value


def lifecycle_family(state: str | None) -> Family:
    return LIFECYCLE_FAMILY.get(state or "", "neutral")


def lifecycle_breakdown(api: ApiClient, *, extra_filters: Mapping[str, str] | None = None) -> dict[str, int]:
    """Counts behind the empty-state/notice copy for product defect A: how many currently-visible
    proposals (ignoring the lifecycle_state filter itself, but honouring every other filter in
    play) are active vs. withdrawn/cancelled vs. other (built/unknown) -- "so the choice is
    visible" per the task brief, computed from the live API rather than hard-coded.
    """
    params: dict[str, str] = dict(extra_filters or {})
    params.pop("lifecycle_state", None)
    params.pop("include_withdrawn", None)
    params["bbox"] = WORLD_BBOX
    params["zoom"] = "1"
    try:
        envelope = api.get("/v1/proposals/geo", params=params)
    except ApiError:
        return {"active": 0, "withdrawn": 0, "other": 0, "total": 0}
    counts: dict[str, int] = envelope["data"].get("totals", {}).get("lifecycle_state_counts", {})
    active = sum(counts.get(s, 0) for s in ACTIVE_PROPOSAL_STATES)
    withdrawn = sum(counts.get(s, 0) for s in WITHDRAWN_PROPOSAL_STATES)
    total = sum(counts.values())
    return {"active": active, "withdrawn": withdrawn, "other": total - active - withdrawn, "total": total}


def flatten_proposal(entity: Mapping[str, Any]) -> dict[str, Any]:
    """API `serialize_proposal()` shape -> the flat dict the detail/list templates read."""
    location = entity.get("location") or {}
    primary_source = _primary_provenance(entity.get("provenance") or [])
    identifiers = entity.get("identifiers") or {}
    queue_ids = identifiers.get("queue_ids") or []
    return {
        "public_id": entity["public_id"],
        "slug": entity["slug"],
        "name": entity["name_canonical"],
        "kind": entity.get("kind"),
        "technology": entity.get("technology"),
        "technology_raw": entity.get("technology_raw"),
        "capacity_mw": entity.get("capacity_mw"),
        "storage_mwh": entity.get("storage_mwh"),
        "jurisdiction": entity.get("jurisdiction"),
        "state": (location.get("state_code") or entity.get("jurisdiction") or "").rsplit("-", 1)[-1] or None,
        "county": location.get("county_name"),
        "location_precision": location.get("precision"),
        "restricted_precision": location.get("precision_reason") == "licence",
        "iso": entity.get("iso"),
        "sponsor": (entity.get("sponsor") or {}).get("name_canonical"),
        "lifecycle_state": entity.get("lifecycle_state"),
        "lifecycle_family": lifecycle_family(entity.get("lifecycle_state")),
        "status_raw": entity.get("status_raw"),
        "queue_id": queue_ids[0]["id"] if queue_ids else None,
        "eia_plant_id": identifiers.get("eia_plant_id"),
        "eia_generator_id": identifiers.get("eia_generator_id"),
        "queue_date": None,
        "proposed_online_date": entity.get("proposed_online_date"),
        "source_count": entity.get("source_count"),
        "source_id": primary_source.get("source_id"),
        "source_name": primary_source.get("source_name"),
        "source_url": primary_source.get("source_url"),
        "retrieved_at": primary_source.get("retrieved_at"),
        "reuse_class": primary_source.get("reuse_class"),
        "attribution_text": primary_source.get("attribution_text"),
        "allows_raw": primary_source.get("source_record_id") is not None,
        "provenance": entity.get("provenance") or [],
    }


def flatten_opportunity(entity: Mapping[str, Any]) -> dict[str, Any]:
    primary_source = _primary_provenance(entity.get("provenance") or [])
    return {
        "public_id": entity["public_id"],
        "slug": entity["slug"],
        "title": entity["title"],
        "kind": entity.get("kind"),
        "issuer": (entity.get("issuer") or {}).get("name_canonical"),
        "jurisdiction": entity.get("jurisdiction"),
        "technologies": entity.get("technologies") or [],
        "capacity_sought_mw": entity.get("capacity_sought_mw"),
        "budget_amount": entity.get("budget_amount"),
        "budget_currency": entity.get("budget_currency"),
        "open_at": entity.get("open_at"),
        "due_at": entity.get("due_at"),
        "status": entity.get("status"),
        "lifecycle_family": lifecycle_family(entity.get("status")),
        "status_raw": entity.get("status_raw"),
        "summary": entity.get("summary"),
        "source_id": primary_source.get("source_id"),
        "source_name": primary_source.get("source_name"),
        "source_url": primary_source.get("source_url"),
        "retrieved_at": primary_source.get("retrieved_at"),
        "reuse_class": primary_source.get("reuse_class"),
        "attribution_text": primary_source.get("attribution_text"),
        "allows_raw": primary_source.get("source_record_id") is not None,
        "provenance": entity.get("provenance") or [],
    }


def flatten_asset(entity: Mapping[str, Any]) -> dict[str, Any]:
    """ADR 0008 `asset` shape (`docs/21` §3.22) -> the flat dict `asset_detail.html` reads.
    `owners[]` (docs/23 §3.1 `/v1/assets/{public_id}`: "organisation public id, name, role,
    share_pct, as_of, source") is kept as its own list of small dicts rather than merged into the
    top level, since a page renders it as its own table.
    """
    primary_source = _primary_provenance(entity.get("provenance") or [])
    owners = [
        {
            "public_id": o.get("public_id") or o.get("organization_public_id"),
            "name": o.get("name") or o.get("name_canonical"),
            "role": o.get("role"),
            "share_pct": o.get("share_pct"),
            "as_of": o.get("as_of"),
            "source_name": o.get("source_name") or o.get("source"),
        }
        for o in (entity.get("owners") or [])
    ]
    return {
        "public_id": entity["public_id"],
        "slug": entity["slug"],
        "name": entity.get("name"),
        "asset_type": entity.get("asset_type"),
        "status": entity.get("status"),
        "operator_name": entity.get("operator_name"),
        "technology": entity.get("technology"),
        "technology_raw": entity.get("technology_raw"),
        "technologies": entity.get("technologies") or {},
        "capacity_mw": entity.get("capacity_mw"),
        "capacity_value": entity.get("capacity_value"),
        "capacity_unit": entity.get("capacity_unit"),
        "commissioned_year": entity.get("commissioned_year"),
        "unit_count": entity.get("unit_count"),
        "state": entity.get("state_code"),
        "county": entity.get("county_name"),
        "county_fips": entity.get("county_fips"),
        "country": entity.get("country"),
        "attributes": entity.get("attributes") or {},
        "owners": owners,
        "source_id": primary_source.get("source_id"),
        "source_name": primary_source.get("source_name"),
        "source_url": primary_source.get("source_url"),
        "retrieved_at": primary_source.get("retrieved_at"),
        "reuse_class": primary_source.get("reuse_class"),
        "attribution_text": primary_source.get("attribution_text"),
        "allows_raw": primary_source.get("source_record_id") is not None,
        "provenance": entity.get("provenance") or [],
    }


def flatten_org_asset_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """`GET /v1/organizations/{public_id}/assets` row (docs/23 §3.1: "through `asset_owner`, with
    role and share") -> the flat dict `organization_detail.html`'s assets table reads. Tolerant of
    either an embedded `asset` sub-object or a row whose asset fields are already flattened onto
    it, since the exact embed shape is not pinned down in `docs/23`'s terse table row.
    """
    asset_raw = row.get("asset")
    asset = asset_raw if isinstance(asset_raw, Mapping) else row
    return {
        "public_id": asset.get("public_id"),
        "slug": asset.get("slug"),
        "name": asset.get("name"),
        "asset_type": asset.get("asset_type"),
        "capacity_mw": asset.get("capacity_mw"),
        "role": row.get("role"),
        "share_pct": row.get("share_pct"),
    }


def flatten_organization(entity: Mapping[str, Any]) -> dict[str, Any]:
    """`organization` shape (`docs/21` §3.5, plus ADR 0008's `parent_org_id`) -> the flat dict
    `organization_detail.html` and the `/search` organisations section read."""
    parent_raw = entity.get("parent")
    parent: Mapping[str, Any] = parent_raw if isinstance(parent_raw, Mapping) else {}
    return {
        "public_id": entity["public_id"],
        "slug": entity.get("slug"),
        "name": entity.get("name_canonical") or entity.get("name"),
        "type": entity.get("type"),
        "country": entity.get("country"),
        "jurisdiction": entity.get("jurisdiction"),
        "website": entity.get("website"),
        "is_curated_issuer": entity.get("is_curated_issuer", False),
        "parent_public_id": parent.get("public_id"),
        "parent_name": parent.get("name_canonical") or parent.get("name"),
        "provenance": entity.get("provenance") or [],
    }


def _primary_provenance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return rows[0] if rows else {}


def provenance_panel_rows(api: ApiClient, provenance: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Product defect C: one row per source, collapsed behind the source name -- classification
    (`reuse_class`) and retrieval date visible, the licence's quote text behind an expandable
    `<details>` (docs/31 §5.2 anatomy). Gated sources are already omitted server-side
    (`provenance_row`/the visibility predicate), so every row here is one the public tier may show.

    `Licence.quote_text` (`services/README.md`'s "Sprint 2 fixes" #5, exposed on the embedded
    licence shape returned by `/v1/sources/{id}`) carries `data/sources.yaml`'s free-text `license`
    clause verbatim -- rendered as-is here rather than composed from the licence's boolean
    permission flags, which is what this module did before that field existed (web/README.md
    "Missing from the API" item 3, now resolved).
    """
    out: list[dict[str, Any]] = []
    licence_cache: dict[str, dict[str, Any]] = {}
    for row in provenance:
        source_id = row.get("source_id")
        licence = licence_cache.get(source_id) if source_id else None
        if licence is None and source_id:
            try:
                licence = api.get(f"/v1/sources/{source_id}")["data"]["licence"]
            except ApiError:
                licence = None
            licence_cache[source_id] = licence or {}
        out.append(
            {
                "source_id": source_id,
                "source_name": row.get("source_name"),
                "source_url": row.get("source_url"),
                "retrieved_at": row.get("retrieved_at"),
                "reuse_class": row.get("reuse_class"),
                "attribution_text": row.get("attribution_text"),
                "allows_raw": row.get("source_record_id") is not None,
                "active": row.get("active", True),
                "licence_quote": _licence_quote_text(licence),
            }
        )
    return out


def _licence_quote_text(licence: dict[str, Any] | None) -> str:
    quote = licence.get("quote_text") if licence else None
    return quote if quote else "No licence quote recorded for this source."


def web_relative_url(url: str | None) -> str | None:
    """The API's own `url` fields are absolute, built from `services/api/common.WEB_HOST` --
    which is still the literal `infraque.com` placeholder token (docs/00-PLAN.md: the product name
    is not chosen yet), so they are not navigable links on whatever host this site is actually
    served from. Anywhere the map (`web/static/js/map.js`, via the `/api/proposals/geo` proxy)
    needs to link to a full record, this strips that placeholder host down to a same-origin path.
    """
    if url and url.startswith(WEB_HOST):
        return url[len(WEB_HOST) :] or "/"
    return url


def relativize_geo_feature_urls(feature_collection: dict[str, Any]) -> dict[str, Any]:
    for feature in feature_collection.get("features", []):
        props = feature.get("properties") or {}
        if "url" in props:
            props["url"] = web_relative_url(props["url"])
    return feature_collection


def restricted_precision_note(location: Mapping[str, Any] | None) -> str | None:
    if not location:
        return None
    if location.get("precision_reason") == "licence":
        return "Location shown at county level (source licence)."
    return None
