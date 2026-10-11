"""`GET /v1/sites/{public_id}` and the `site` embed on `GET /v1/proposals/{public_id}` (owner
decision 2026-10-10, docs/51 §7 Q6; docs/21 §3.25).

A site is the parent of proposals that share a place by unique identifier (`services/sites`). What
a reader may see of it is decided per request, through the proposal predicate:

* **Members are the visible ones only.** Every member list, count and total is computed over the
  site's members that pass `proposal_visibility_filter` at the caller's tier, and every member row
  is the proposal's served view (`gated_record`): no value from a source the tier may not read. A
  PJM row (not public until a licence exists), an unpublished record or one not yet public is
  absent from every number and list here, as it is from the proposal endpoints.
* **No site of one.** A site with fewer than two visible members does not exist on that tier: the
  detail answers the `404` an unknown id gets, and the proposal embed is `null`.
* **The lead is the viewer's.** The site's name is its lead's served name. When the stored lead is
  hidden from the caller, the lead and the labels are re-run over the visible members with the same
  rules (`services/sites/rules.py`), from the basis stored with each membership, so a hidden
  record's name never heads a page.
* **No bridging through a hidden record** (lane S2). When any member is hidden, the site's
  connectivity is recomputed over the visible members alone (`rules.connected_groups`, from the
  plant ids in each `basis` and the direct links in each `grouping_evidence`), and only one
  connected group is served: the one holding the record asked about (the proposal embed, or
  `?member=` on the detail), else the largest. Lead and labels are re-run over that group, so a
  visible record linked to the others only through a hidden one is not shown with them and the
  relation the hidden record carried is not served. `partial` tells the reader that other records
  of this site are not listed (each keeps its own page); it never says why.
* **Flagged and retired sites are not served.** A site flagged `oversize` waits for review; a
  retired site answers `301` to its successor when the successor is served to the caller, else
  `404`.
* **`shares_interconnection_point` is not membership.** Visible proposals at a member's
  interconnection point that the rules did not group (another or no named developer) are listed
  apart, only when that point's own register is visible (`interconnection_point_visible`).
* **Anchors** are the external identifiers the site rests on, per viewer: the visible members' EIA
  plant ids, their visible interconnection points, and the visible `power_plant` assets of those
  plant ids with their visible owners (the "who owns which assets" link).
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import case, select
from sqlalchemy.orm import Session, selectinload

from services.api.admin_sites import router as admin_router
from services.api.auth import AuthContext, get_auth_context
from services.api.common import WEB_HOST, iso
from services.api.deps import get_db
from services.api.errors import not_found
from services.api.interconnection_points import (
    ACTIVE_LIFECYCLE_STATES,
    BUCKETS,
    BUILT_LIFECYCLE_STATES,
    WITHDRAWN_LIFECYCLE_STATES,
    bucket_of,
    point_name,
    point_url,
)
from services.api.params import check_allowed
from services.api.records import _proposal_licence_rows
from services.api.serialize import (
    build_envelope,
    build_licence_summary,
    build_meta,
    licence_summary_row,
    provenance_row,
    serialize_asset_owner,
    serialize_asset_summary,
    serialize_organization_summary,
)
from services.api.visibility import (
    LinkOk,
    asset_visibility_filter,
    gated_record,
    interconnection_point_visible,
    organization_visible,
    proposal_visibility_filter,
    provenance_visible,
    visible_source_links,
)
from services.api.withheld_names import withheld_names
from services.db.models import Asset, InterconnectionPoint, Proposal, Site, SiteMember
from services.sites import rules
from services.sites.switch import sites_enabled

router = APIRouter()
# The operator read of the site pass (`GET /admin/v1/sites/review`) rides on this router, so the
# app's one `include_router(sites_router)` mounts both.
router.include_router(admin_router)

#: Neighbours listed under `shares_interconnection_point`; the count always covers all of them.
NEIGHBOUR_CAP = 100
#: Members listed on the detail; the totals always cover all of them (the largest site today has
#: 161 members).
MEMBER_CAP = 500
#: Retired-site hops followed to a successor before giving up.
MAX_SUCCESSOR_HOPS = 10


@dataclass(frozen=True)
class ServedMember:
    member: SiteMember
    proposal: Proposal
    label: rules.Label
    rank: int
    group_key: str
    #: The plant-group head this member is a unit of (a visible member), else None.
    parent: Proposal | None


@dataclass(frozen=True)
class ServedSite:
    site: Site
    #: One connected group of the members the caller may see, in display order (the lead first).
    members: list[ServedMember]
    #: The caller may see members of this site that are not in `members` (they are connected to
    #: these only through a member the caller may not see). Served as `partial`, never with a reason.
    partial: bool = False

    @property
    def lead(self) -> ServedMember:
        return self.members[0]


def site_url(site: Site) -> str:
    return f"{WEB_HOST}/sites/{site.public_id}"


def _servable(site: Site) -> bool:
    return site.retired_at is None and site.review_flag is None


def stored_links(member: SiteMember) -> list[str] | None:
    """The member's direct rule b-c partners (`rules.LINKS_KEY`), or None for a row written before
    links were stored (`rules.connected_groups` then joins it by plant ids only)."""
    links = (member.grouping_evidence or {}).get(rules.LINKS_KEY)
    if isinstance(links, dict):
        return [str(k) for k in links]
    if isinstance(links, list):
        return [str(k) for k in links]
    return None


def served_groups(
    db: Session, site: Site, entitlement: str, *, with_sources: bool = True
) -> list[ServedSite]:
    """The site as `entitlement` may see it (module docstring): its connected groups of two or more
    visible members, largest first, each with its own lead and labels; empty when there is none.
    When every member is visible the stored placement is served as built (one group: a stored site
    is connected by construction); otherwise connectivity is recomputed over the visible members
    (`rules.connected_groups`) and `rules.label_site` re-run over each group's stored bases, so no
    lead, group head, label or link rests on a member this caller may not see."""
    if not sites_enabled() or not _servable(site):
        return []
    # The members' ids first, and the join narrowed to them. Without planner statistics SQLite drives
    # this query from the publish-state index over every public proposal rather than from the site's
    # index; the id list halves that (median 45.6 -> 23.2 ms per call over 60 sites of a copy of the
    # e2e store, 2026-10-10). Postgres plans from statistics; there it costs one indexed query.
    member_ids = list(db.scalars(select(SiteMember.proposal_id).where(SiteMember.site_id == site.id)))
    if len(member_ids) < 2:
        return []
    stmt = (
        select(SiteMember, Proposal)
        .join(Proposal, Proposal.id == SiteMember.proposal_id)
        .where(
            SiteMember.site_id == site.id,
            Proposal.id.in_(member_ids),
            *proposal_visibility_filter(entitlement),
        )
    )
    # Every member's links when every row is printed (the detail); the embed prints only the lead's
    # served name, whose links load on first use.
    rows = db.execute(stmt.options(selectinload(Proposal.sources)) if with_sources else stmt).all()
    if len(rows) < 2:
        return []
    pairs = [(cast(SiteMember, r[0]), cast(Proposal, r[1])) for r in rows]
    by_id = {p.id: p for _m, p in pairs}
    if len(pairs) == site.member_count and all(m.parent_proposal_id in (None, *by_id) for m, _p in pairs):
        ordered = sorted(pairs, key=lambda mp: mp[0].lead_rank)
        members = [
            ServedMember(
                m,
                p,
                rules.Label(m.relation, m.relation_rule, m.confidence),
                i,
                m.group_key,
                by_id.get(m.parent_proposal_id) if m.parent_proposal_id else None,
            )
            for i, (m, p) in enumerate(ordered)
        ]
        return [ServedSite(site, members)]
    by_public = {p.public_id: (m, p) for m, p in pairs}
    bases = [rules.Basis.from_json({**(m.basis or {}), "public_id": p.public_id}) for m, p in pairs]
    groups = rules.connected_groups(bases, {p.public_id: stored_links(m) for m, p in pairs})
    out: list[ServedSite] = []
    for group in groups:
        if len(group) < 2:
            continue
        placed = rules.label_site(group)
        out.append(
            ServedSite(
                site,
                [
                    ServedMember(
                        *by_public[x.basis.public_id],
                        x.label,
                        x.rank,
                        x.group_key,
                        by_public[x.parent][1] if x.parent else None,
                    )
                    for x in placed
                ],
                partial=len(group) < len(pairs),
            )
        )
    return out


def served_site(
    db: Session,
    site: Site,
    entitlement: str,
    *,
    with_sources: bool = True,
    member: _uuid.UUID | None = None,
) -> ServedSite | None:
    """The one group of `served_groups` a page shows: the group holding the proposal `member` when
    one is named (None when it is in none), else the largest. None when the caller may see no
    group of two."""
    groups = served_groups(db, site, entitlement, with_sources=with_sources)
    if member is None:
        return groups[0] if groups else None
    return next((g for g in groups if any(m.proposal.id == member for m in g.members)), None)


# ---------------------------------------------------------------------------- proposal embed
def _embed(
    served: ServedSite, proposal_id: _uuid.UUID, entitlement: str, link_ok: LinkOk | None
) -> dict[str, Any] | None:
    mine = next((m for m in served.members if m.proposal.id == proposal_id), None)
    if mine is None:
        return None
    lead = gated_record(served.lead.proposal, entitlement, link_ok)
    return {
        "public_id": served.site.public_id,
        "url": site_url(served.site),
        "name": lead.name_canonical,
        "member_count": len(served.members),
        "is_lead": mine.rank == 0,
        "parent_public_id": mine.parent.public_id if mine.parent is not None else None,
        "relation": mine.label.relation,
        "relation_rule": mine.label.rule,
        "confidence": mine.label.confidence,
        "grouping_rule": mine.member.grouping_rule,
        "lead": {
            "public_id": lead.public_id,
            "slug": lead.slug,
            "url": f"{WEB_HOST}/proposals/{lead.slug}",
            "name_canonical": lead.name_canonical,
        },
    }


def proposal_site_embeds(
    db: Session, proposals: list[Proposal], entitlement: str, *, link_ok: LinkOk | None = None
) -> dict[_uuid.UUID, dict[str, Any] | None]:
    """`site` for each of `proposals` (keyed by proposal id): the site the record belongs to as the
    caller may see it -- its id, name, visible member count and lead, and this record's own place
    in it, all over the connected group that holds the record (`served_groups`) -- or `None` when it
    has none on this tier. One membership query for the batch, then one served read per distinct
    site, so the bulk stream carries the detail shape. `link_ok` (the bulk stream's redistribution
    rule) narrows the lead's served name as it narrows every field."""
    out: dict[_uuid.UUID, dict[str, Any] | None] = {p.id: None for p in proposals}
    if not proposals or not sites_enabled():
        return out
    rows = db.execute(
        select(SiteMember.proposal_id, Site)
        .join(Site, Site.id == SiteMember.site_id)
        .where(SiteMember.proposal_id.in_([p.id for p in proposals]))
    ).all()
    served: dict[_uuid.UUID, list[ServedSite]] = {}
    for proposal_id, site in rows:
        if site.id not in served:
            served[site.id] = served_groups(db, site, entitlement, with_sources=False)
        view = next(
            (g for g in served[site.id] if any(m.proposal.id == proposal_id for m in g.members)), None
        )
        out[proposal_id] = _embed(view, proposal_id, entitlement, link_ok) if view is not None else None
    return out


def proposal_site_embed(db: Session, proposal: Proposal, entitlement: str) -> dict[str, Any] | None:
    """`site` on `GET /v1/proposals/{public_id}` (`proposal_site_embeds` for one)."""
    return proposal_site_embeds(db, [proposal], entitlement)[proposal.id]


# ------------------------------------------------------------------------------------ detail
def _proposal_row(p: Proposal, entitlement: str) -> dict[str, Any]:
    view = gated_record(p, entitlement)
    sponsor = view.sponsor  # the served view: None unless `organization_visible` admits it
    return {
        "public_id": view.public_id,
        "slug": view.slug,
        "url": f"{WEB_HOST}/proposals/{view.slug}",
        "name_canonical": view.name_canonical,
        "kind": view.kind,
        "technology": view.technology,
        "capacity_mw": float(view.capacity_mw) if view.capacity_mw is not None else None,
        "lifecycle_state": view.lifecycle_state,
        "lifecycle_bucket": bucket_of(view.lifecycle_state),
        "jurisdiction": view.jurisdiction,
        "proposed_online_date": iso(view.proposed_online_date),
        "sponsor": serialize_organization_summary(sponsor) if sponsor is not None else None,
        "provenance": [provenance_row(s, s.source) for s in visible_source_links(p.sources, entitlement)],
    }


def _group(m: ServedMember) -> dict[str, Any]:
    plants = (
        m.group_key[len(rules.PLANT_GROUP_PREFIX) :]
        if m.group_key.startswith(rules.PLANT_GROUP_PREFIX)
        else ""
    )
    return {"key": m.group_key, "eia_plant_ids": plants.split(",") if plants else []}


def _member_row(m: ServedMember, entitlement: str) -> dict[str, Any]:
    return {
        **_proposal_row(m.proposal, entitlement),
        "is_lead": m.rank == 0,
        "group": _group(m),
        "parent_public_id": m.parent.public_id if m.parent is not None else None,
        "relation": m.label.relation,
        "relation_rule": m.label.rule,
        "confidence": m.label.confidence,
        "grouping_rule": m.member.grouping_rule,
    }


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = dict.fromkeys(BUCKETS, 0)
    mw = dict.fromkeys(BUCKETS, 0.0)
    for row in rows:
        counts[row["lifecycle_bucket"]] += 1
        mw[row["lifecycle_bucket"]] += row["capacity_mw"] or 0.0
    out: dict[str, Any] = {"member_count": len(rows), "total_mw": round(sum(mw.values()), 3)}
    for bucket in BUCKETS:
        out[f"{bucket}_count"] = counts[bucket]
        out[f"{bucket}_mw"] = round(mw[bucket], 3)
    return out


def _points(db: Session, served: ServedSite, entitlement: str) -> dict[_uuid.UUID, InterconnectionPoint]:
    ids = {m.proposal.interconnection_point_id for m in served.members if m.proposal.interconnection_point_id}
    if not ids:
        return {}
    rows = db.scalars(select(InterconnectionPoint).where(InterconnectionPoint.id.in_(ids)))
    return {pt.id: pt for pt in rows if interconnection_point_visible(pt, entitlement)}


def _neighbours(
    db: Session, served: ServedSite, points: dict[_uuid.UUID, InterconnectionPoint], entitlement: str
) -> tuple[list[dict[str, Any]], int]:
    """Visible proposals at the members' visible points that are not members of this site."""
    if not points:
        return [], 0
    member_ids = [m.proposal.id for m in served.members]
    order_bucket = case(
        (Proposal.lifecycle_state.in_(ACTIVE_LIFECYCLE_STATES), 0),
        (Proposal.lifecycle_state.in_(BUILT_LIFECYCLE_STATES), 1),
        (Proposal.lifecycle_state.in_(WITHDRAWN_LIFECYCLE_STATES), 3),
        else_=2,
    )
    rows = list(
        db.scalars(
            select(Proposal)
            .where(
                Proposal.interconnection_point_id.in_(list(points)),
                Proposal.id.not_in(member_ids),
                *proposal_visibility_filter(entitlement),
            )
            .options(selectinload(Proposal.sources))
            .order_by(order_bucket, Proposal.capacity_mw.desc().nulls_last(), Proposal.public_id)
        )
    )
    out = []
    for p in rows[:NEIGHBOUR_CAP]:
        point = points[cast(_uuid.UUID, p.interconnection_point_id)]
        out.append(
            {
                **_proposal_row(p, entitlement),
                "relation": rules.NEIGHBOUR_RELATION,
                "interconnection_point": {
                    "public_id": point.public_id,
                    "url": point_url(point),
                    "name": point_name(point),
                },
            }
        )
    return out, len(rows)


def _anchors(
    db: Session, served: ServedSite, points: dict[_uuid.UUID, InterconnectionPoint], entitlement: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The anchors (module docstring) and the licence rows of everything they print."""
    plants: set[str] = set()
    for m in served.members:
        plants.update(str(p) for p in (m.member.basis or {}).get("plant_ids") or ())
    by_plant = (served.site.anchors or {}).get("assets") or {}
    asset_ids = sorted({_uuid.UUID(str(by_plant[p])) for p in plants if p in by_plant}, key=str)
    assets: list[dict[str, Any]] = []
    licence_rows = [licence_summary_row(pt.source, pt.licence, pt.retrieved_at) for pt in points.values()]
    if asset_ids:
        withheld = withheld_names(db)
        for asset in db.scalars(
            select(Asset)
            .where(Asset.id.in_(asset_ids), *asset_visibility_filter(entitlement))
            .order_by(Asset.public_id)
        ):
            owners = [
                o
                for o in asset.owners
                if organization_visible(o.organization, entitlement)
                and provenance_visible(o.source, o.licence, entitlement)
            ]
            assets.append(
                {
                    **serialize_asset_summary(asset),
                    "eia_plant_id": asset.source_asset_id,
                    "owners": [serialize_asset_owner(o, withheld=withheld) for o in owners],
                }
            )
            licence_rows.append(licence_summary_row(asset.source, asset.licence, asset.retrieved_at))
            licence_rows.extend(licence_summary_row(o.source, o.licence, o.retrieved_at) for o in owners)
    anchors = {
        "eia_plant_ids": sorted(plants, key=lambda s: (len(s), s)),
        "interconnection_points": [
            {"public_id": pt.public_id, "url": point_url(pt), "name": point_name(pt)}
            for pt in sorted(points.values(), key=lambda pt: pt.public_id)
        ],
        "assets": assets,
    }
    return anchors, licence_rows


def serialize_site(
    db: Session, served: ServedSite, entitlement: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The detail's `data`, and the licence rows of the anchors it prints (members and neighbours
    are credited by the caller from their links)."""
    members = [_member_row(m, entitlement) for m in served.members]
    points = _points(db, served, entitlement)
    neighbours, neighbour_total = _neighbours(db, served, points, entitlement)
    lead = members[0]
    anchors, licence_rows = _anchors(db, served, points, entitlement)
    sponsors: dict[str, dict[str, Any]] = {}
    for row in members:
        if row["sponsor"] is not None:
            entry = sponsors.setdefault(row["sponsor"]["public_id"], {**row["sponsor"], "member_count": 0})
            entry["member_count"] += 1
    data = {
        "public_id": served.site.public_id,
        "url": site_url(served.site),
        "name": lead["name_canonical"],
        "rule_version": served.site.rule_version,
        "member_count": len(members),
        "partial": served.partial,
        "lead": lead,
        "members": members[:MEMBER_CAP],
        "members_truncated": len(members) > MEMBER_CAP,
        "totals": _totals(members),
        "sponsors": sorted(sponsors.values(), key=lambda o: (-o["member_count"], o["name_canonical"])),
        "anchors": anchors,
        "shares_interconnection_point": neighbours,
        "shares_interconnection_point_count": neighbour_total,
    }
    return data, licence_rows


def _successor(db: Session, site: Site, entitlement: str) -> ServedSite | None:
    seen = {site.id}
    current = site
    for _ in range(MAX_SUCCESSOR_HOPS):
        if current.retired_at is None:
            return served_site(db, current, entitlement)
        nxt = db.get(Site, current.successor_site_id) if current.successor_site_id else None
        if nxt is None or nxt.id in seen:
            return None
        seen.add(nxt.id)
        current = nxt
    return None


def _member_id(db: Session, site: Site, public_id: str | None) -> _uuid.UUID | None:
    """The proposal `?member=` names when it is a member of `site` (visibility is applied by
    `served_site`, which finds it in no group otherwise)."""
    if not public_id:
        return None
    return db.scalar(
        select(SiteMember.proposal_id)
        .join(Proposal, Proposal.id == SiteMember.proposal_id)
        .where(SiteMember.site_id == site.id, Proposal.public_id == public_id)
    )


@router.get("/v1/sites/{public_id}")
def get_site(
    public_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> Any:
    check_allowed(request, {"member"})
    site = db.scalar(select(Site).where(Site.public_id == public_id)) if sites_enabled() else None
    if site is None:
        raise not_found(request.url.path)
    if site.retired_at is not None:
        successor = _successor(db, site, ctx.entitlement)
        if successor is None:
            raise not_found(request.url.path)
        return RedirectResponse(
            str(request.url.replace(path=f"/v1/sites/{successor.site.public_id}")), status_code=301
        )
    named = request.query_params.get("member")
    member = _member_id(db, site, named)
    if named and member is None:
        raise not_found(request.url.path)
    served = served_site(db, site, ctx.entitlement, member=member)
    if served is None:
        raise not_found(request.url.path)
    data, anchor_rows = serialize_site(db, served, ctx.entitlement)
    listed = [m.proposal for m in served.members]
    neighbour_ids = [n["public_id"] for n in data["shares_interconnection_point"]]
    if neighbour_ids:
        listed += list(
            db.scalars(
                select(Proposal)
                .where(Proposal.public_id.in_(neighbour_ids))
                .options(selectinload(Proposal.sources))
            )
        )
    return build_envelope(
        data,
        meta=build_meta(tier=ctx.entitlement),
        licence_summary=build_licence_summary(
            [*_proposal_licence_rows(listed, ctx.entitlement), *anchor_rows]
        ),
    )
