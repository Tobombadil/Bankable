"""The ownership tree: walking `organization.parent_org_id` down to descendants and up to
ancestors, safely, with the provenance of every edge it crosses.

Until 2026-09-20 the walk was one level in each direction: `_subsidiary_ids` in
`services/api/assets.py` selected the organisations whose `parent_org_id` was the subject and
stopped, and the company page's `parent` field named one organisation. That is enough for a
pipeline operator over its own operating companies and not enough for what the owner asked to see
(2026-09-20): "Seeing everything -> all blackrock -> Tallgrass -> trailblazer as well as proposals
from them and those they may be interested in" — a chain walked in both directions, with each
level's assets, sponsored proposals and nearby proposals. (The company is **Blackstone**:
Blackstone Infrastructure Partners took control of Tallgrass in 2019 and bought a further ~30%
from Enagás in 2024. Nothing in this repo names BlackRock.) That edge now exists: a curated
rule in `data/vendored/organizations/parents.yaml`, quoted from Tallgrass's own 8-K and dated
to the 2019-03-11 closing. It is recorded because a source states it, not to make this module
look better against a demo -- nothing here invents an edge.

Three properties this module exists to guarantee, none of which a one-level walk had to worry
about:

* **A cycle cannot hang a request.** Corporate ownership graphs contain them (cross-holdings, and
  simple data error: two loaders each naming the other organisation as the parent). Every walk
  carries a visited set and never re-enters a node, so the worst a cycle costs is the work of
  visiting each reachable organisation once.
* **The caps are visible.** The descent stops at `MAX_DEPTH` levels and `MAX_SCOPE_ORGS`
  organisations, and when either bites, `OrgScope.depth_capped` / `.truncated` say so and the
  caller renders it. A silently truncated ownership scope is the failure this whole feature is
  about: a reader sees a short list and concludes the coverage is thin.
* **Nothing is asserted that the data does not carry.** Every ancestor edge is returned with its
  `parent_source_id`, `parent_as_of` and `parent_share_pct` exactly as stored, including the NULLs
  — `as_of: null` is rendered as "no date recorded", not hidden. An ownership claim with no date
  is a liability (the Lincoln Land Energy Center page lists a parent while its own narrative
  describes a different subsidiary issuing a 2024 RFP), so the date travels with the claim from
  here to the page.

**Cost, measured rather than assumed.** The descent is breadth-first *by level*, not by node: one
`WHERE parent_org_id IN (:this level's ids)` query per level, so the query count scales with
**depth**, not with how many organisations the scope spans. Counted on the 2026-09-20 load (7,412
organisations, 298 with a parent) with a `before_cursor_execute` hook: `scope=self` 0 queries,
`scope=children` 1, `scope=all` on `tallgrass-energy` 2 (one for the nine subsidiaries, one that
finds nothing below them), a synthetic 500-wide fan-out 1, a synthetic 12-deep chain 11. The
consuming queries are unchanged — still one `IN` over the resulting id list, which is why the
`MAX_SCOPE_ORGS` cap exists.

A **recursive CTE** would make it one query instead of *d*, and was measured against this rather
than argued about. Median over 25 runs on SQLite, `WITH RECURSIVE ... UNION` (dedup form, so it
terminates on a cycle): `tallgrass-energy` BFS 2.4 ms vs CTE 6.4 ms; THE SOUTHERN COMPANY (15
organisations, 3 levels) 3.6 vs 6.0; RWE (19) 2.4 vs 6.3; a synthetic 500-wide plus 12-deep tree
2.4 vs 10.3. The CTE is slower at every size in the data, and it also cannot express the depth
cap, cannot report *which* bound stopped it, and needs a dialect-specific path. So: repeated
queries, and revisit if a source lands that makes trees deep **and** the per-query overhead
dominates — on Postgres, where round trips cost more than SQLite's in-process call, the crossover
is closer and the measurement should be redone on the first deploy.

**Scope vocabulary.** `self` (this organisation alone), `children` (it and its direct
subsidiaries — exactly what `include_subsidiaries=true` has always meant) and `all` (the full
descent). The legacy boolean is still accepted and still means what it meant: see
`scope_from_request`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.errors import validation_error
from services.db.models import Organization

#: The scope vocabulary, in widening order. `children` is the pre-2026-09-20 behaviour of
#: `include_subsidiaries=true` and keeps that meaning exactly.
SCOPES: tuple[str, ...] = ("self", "children", "all")

#: Levels below the subject the descent will walk, and the same limit on the climb to ancestors.
#: The deepest chains in the loaded data are 3 levels (ENEL - SPA and NEW JERSEY RESOURCES
#: CORPORATION); of 137 rooted trees, 130 are one level deep, 5 are two and 2 are three, measured
#: 2026-09-20 over 298 parent links. Ten leaves better than
#: three times that headroom while bounding a request at ten queries, which is the point of having
#: a number at all: the cap is a guard against a pathological or mis-loaded graph, not a product
#: limit, so it should sit far above anything a real filing produces and far below anything that
#: could make a page slow.
MAX_DEPTH = 10

#: Organisations one scope may span. A fund with a four-figure portfolio would otherwise put a
#: four-figure `IN` list into every downstream query. At the cap the scope reports `truncated` and
#: the page says so rather than presenting a partial group as the whole. Nothing in the loaded data
#: comes close: the widest tree spans 18 descendants.
MAX_SCOPE_ORGS = 500


@dataclass(frozen=True)
class OrgScope:
    """The organisation ids a scoped query spans, and the truth about how they were gathered."""

    #: What was asked for: one of `SCOPES`.
    scope: str
    #: The subject plus every descendant reached, subject first. Always at least one id.
    ids: list[Any]
    #: Levels actually walked below the subject (0 for `self`).
    depth: int
    #: True when the walk stopped at `MAX_DEPTH` with organisations still below it.
    depth_capped: bool
    #: True when the walk stopped at `MAX_SCOPE_ORGS`.
    truncated: bool
    #: True when a `parent_org_id` chain led back to an organisation already visited.
    cycle_detected: bool

    @property
    def organizations(self) -> int:
        return len(self.ids)

    def as_meta(self) -> dict[str, Any]:
        """The block every scoped response carries, so a caller can tell a complete group from a
        capped one without counting rows."""
        return {
            "scope": self.scope,
            "organizations": self.organizations,
            "depth": self.depth,
            "depth_capped": self.depth_capped,
            "truncated": self.truncated,
            "cycle_detected": self.cycle_detected,
            "max_depth": MAX_DEPTH,
            "max_organizations": MAX_SCOPE_ORGS,
        }


@dataclass(frozen=True)
class OwnershipEdge:
    """One `child -> parent` link, with the provenance of the claim.

    `source_id`, `as_of` and `share_pct` are the stored values, NULLs included. A consumer that
    renders the parent must render what these say — including "no date recorded" — because the
    claim and its date are one fact, not a fact and an optional decoration.
    """

    child: Organization
    parent: Organization
    source_id: str | None
    as_of: dt.date | None
    share_pct: float | None


def _parent_edge(db: Session, child: Organization) -> OwnershipEdge | None:
    if child.parent_org_id is None:
        return None
    parent = db.get(Organization, child.parent_org_id)
    if parent is None:  # pragma: no cover - foreign key guarantees the row
        return None
    return OwnershipEdge(
        child=child,
        parent=parent,
        source_id=child.parent_source_id,
        as_of=child.parent_as_of,
        share_pct=float(child.parent_share_pct) if child.parent_share_pct is not None else None,
    )


def org_ancestors(db: Session, org: Organization) -> list[OwnershipEdge]:
    """The chain above `org`, nearest parent first, at most `MAX_DEPTH` edges.

    A page renders breadcrumbs by reversing this (root first). The visited set is what stops a
    two-node cycle — A's parent is B, B's parent is A — from climbing for ever; it is not
    hypothetical, since `parent_org_id` is written by two loaders that do not see each other's
    rows.
    """
    edges: list[OwnershipEdge] = []
    seen: set[Any] = {org.id}
    current = org
    while len(edges) < MAX_DEPTH:
        edge = _parent_edge(db, current)
        if edge is None or edge.parent.id in seen:
            break
        edges.append(edge)
        seen.add(edge.parent.id)
        current = edge.parent
    return edges


def _children_of(db: Session, ids: list[Any]) -> list[Any]:
    """One level down from every id in `ids`, merged-away organisations excluded (a merged row is
    not a company, it is a redirect). One query per level — see the module docstring on cost."""
    return list(
        db.scalars(
            select(Organization.id).where(
                Organization.parent_org_id.in_(ids), Organization.merged_into_id.is_(None)
            )
        ).all()
    )


def org_scope(db: Session, org: Organization, scope: str = "self") -> OrgScope:
    """The organisation ids a query for "this organisation's ..." spans, under `scope`.

    `self` is the subject alone. `children` is the subject and one level below it. `all` descends
    until there is nothing below, `MAX_DEPTH` levels have been walked or `MAX_SCOPE_ORGS`
    organisations have been gathered, whichever comes first, reporting which.

    The group parent the ownership lane links (Tallgrass Energy over nine operating subsidiaries)
    holds no `asset_owner` edge of its own, which is why the scoped read exists at all; with `all`
    the same is true of anything above it, so a holding company two levels up reads its whole
    group through one call.
    """
    if scope not in SCOPES:  # pragma: no cover - callers validate first
        raise ValueError(f"unknown scope {scope!r}")
    ids: list[Any] = [org.id]
    if scope == "self":
        return OrgScope(
            scope=scope, ids=ids, depth=0, depth_capped=False, truncated=False, cycle_detected=False
        )

    max_levels = 1 if scope == "children" else MAX_DEPTH
    seen: set[Any] = {org.id}
    frontier: list[Any] = [org.id]
    depth = 0
    truncated = False
    cycle_detected = False
    while frontier and depth < max_levels:
        found = _children_of(db, frontier)
        nxt: list[Any] = []
        for child_id in found:
            if child_id in seen:
                # Either a diamond (two parents named the same child, which the single-parent
                # column cannot express but a mis-load can produce) or a true cycle. Both are
                # handled the same way — visit once — and both are reported.
                cycle_detected = True
                continue
            seen.add(child_id)
            nxt.append(child_id)
        if not nxt:
            break
        depth += 1
        room = MAX_SCOPE_ORGS - len(ids)
        if len(nxt) > room:
            ids.extend(nxt[:room])
            truncated = True
            break
        ids.extend(nxt)
        frontier = nxt
    depth_capped = False
    if scope == "all" and not truncated and depth == MAX_DEPTH and frontier:
        # Walked the full allowance and there is still a level below: say so rather than letting
        # the response imply the group ends here.
        depth_capped = bool(_children_of(db, frontier))
    return OrgScope(
        scope=scope,
        ids=ids,
        depth=depth,
        depth_capped=depth_capped,
        truncated=truncated,
        cycle_detected=cycle_detected,
    )


def scope_from_request(request: Request, *, default: str = "self") -> str:
    """Resolve `?scope=` and the legacy `?include_subsidiaries=` into one scope token.

    `include_subsidiaries` is **not** re-pointed at the recursive walk. It was published meaning
    "the organisation and its direct subsidiaries", callers have it in saved URLs, and quietly
    widening a live parameter would change what an existing integration's numbers mean without
    anything in the response saying so. It therefore keeps mapping to `children` (and `false` to
    `self`) and is marked deprecated in `api/openapi.yaml`; `scope=all` is the new behaviour and
    has to be asked for. Sending both is a 400 rather than a precedence rule nobody would
    remember.
    """
    qp = request.query_params
    raw_scope = qp.get("scope")
    raw_legacy = qp.get("include_subsidiaries")
    if raw_scope is not None and raw_legacy is not None:
        raise validation_error(
            "scope",
            "scope and include_subsidiaries cannot both be given; "
            "include_subsidiaries=true is scope=children",
            request.url.path,
        )
    if raw_scope is not None:
        value = raw_scope.strip().lower()
        if value not in SCOPES:
            raise validation_error("scope", f"scope must be one of {', '.join(SCOPES)}", request.url.path)
        return value
    if raw_legacy is not None:
        value = raw_legacy.strip().lower()
        if value in ("true", "1"):
            return "children"
        if value in ("false", "0"):
            return default
        raise validation_error(
            "include_subsidiaries", "include_subsidiaries must be true or false", request.url.path
        )
    return default


def scope_ids(db: Session, org: Organization, scope: str) -> list[Any]:
    """`org_scope(...).ids` for a caller that needs only the ids."""
    return org_scope(db, org, scope).ids
