"""Company-page ownership: the chain above an organisation, the portfolio below it, and the
decision about which of the two a given node is.

The API walks the tree (`services/api/orgtree.py`); this module decides what a page does with the
answer. Three things live here because all three are judgements with numbers behind them, and a
judgement with a number behind it belongs somewhere a test can pin it:

**Which scope a company page reads.** `?scope=self|children|all`, default `all`. Before
2026-09-20 the page always sent `include_subsidiaries=true` (one level) with no way to say
otherwise; the URL is now the control, because every level of the tree has to be a plain link --
this site's browser tests run with scripts off (docs/04 D-30, `web/test_e2e.py`), so drill-down is
URLs and nothing else.

**How an ownership claim is rendered.** Never as a bare name. `X is owned by Y` is a statement
about today, read from a file that states some other day, and the two loaded parent sources
disagree about whether they even publish a date: GLEIF Level 2 gives a relationship period start
(289 of 298 links carry one), the curated file cites when a web page was read and leaves the date
null (the other 9). So the sentence the page prints always carries the source and either the date
or the words that there is none. The failure this guards against is on the record: an asset page
listing a parent in its ownership table while its own narrative describes a different subsidiary
issuing a 2024 RFP.

**Whether a node is a company page or a fund page.** A pipeline operator with 10 assets has a map
that means something. A holding company with hundreds of assets across dozens of portfolio
companies has a dot scatter. The thresholds are in `GroupView` and are not invented numbers: they
are the limits the page already had, now stated instead of silently hit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The URL control for how far down the tree a company page reads.
SCOPE_PARAM = "scope"

#: What the page asks for when the URL says nothing. `all`, not `self`: the owner's request
#: (2026-09-20) is to see a whole group from any node in it, and a holding company holds no
#: `asset_owner` edge of its own, so `self` renders an empty page that is technically correct.
#: The *API* default stays `self` -- a page choosing a wider view for its readers is not the same
#: as changing what an unparameterised API call returns for an integration.
DEFAULT_SCOPE = "all"

SCOPES: tuple[str, ...] = ("self", "children", "all")

SCOPE_LABELS: Mapping[str, str] = {
    "self": "This company only",
    "children": "With direct subsidiaries",
    "all": "Whole group",
}

#: Assets a *group* map may draw, and the number of asset geometries the company page fetches in
#: one render (`web/app.py::ORG_MAP_DETAIL_CAP` is an alias of this, not a second copy): above it
#: the map was already drawing whichever 40
#: assets came first in `asset_owner.id` order, which is not a sample of the group and not a
#: picture of it. Measured on the 2026-09-20 load: the largest group scope holds 18 assets
#: (THE SOUTHERN COMPANY, 13 companies), so no page loses a map today. The eight scopes that do
#: exceed 40 assets are all single companies with no subsidiaries at all, and single-company pages
#: are deliberately left exactly as they were -- the dot scatter the owner described is a fund
#: problem, and this is not a licence to change what a company page does.
GROUP_MAP_MAX_ASSETS = 40

#: Assets a group's flat asset table may list. The page fetches the group's assets with one
#: `limit=100` call, so above this the table is a truncated list of one arbitrary subset of other
#: companies' assets. Above it the portfolio table is the navigation and each company's own page
#: holds its assets. Nothing in the current data reaches it in group scope; the largest single
#: company does (WM Renewable Energy, 102 landfill-gas assets) and is untouched.
GROUP_TABLE_MAX_ASSETS = 100


def resolve_scope(value: str | None) -> str:
    """`?scope=` -> a scope token, unknown values degrading to the default rather than 400ing.

    Same rule as `web/relevance.py::resolve_nearby_filter`: a hand-typed or stale URL must never
    make the register look emptier than it is, and here "emptier" would be a holding company's
    page rendering as the empty row it is.
    """
    token = (value or "").strip().lower()
    return token if token in SCOPES else DEFAULT_SCOPE


@dataclass(frozen=True)
class Claim:
    """One rendered ownership claim: who, from where, as of when."""

    name: str
    href: str | None
    public_id: str | None
    source_id: str | None
    as_of: str | None

    @property
    def dated(self) -> bool:
        return bool(self.as_of)

    @property
    def note(self) -> str:
        """The sentence printed beside the claim. Always names the source; says plainly when there
        is no date rather than leaving the reader to assume today. With neither -- a crumb at the
        top of the chain, whose own parent link is outside it -- there is nothing to say and the
        page prints nothing."""
        if not (self.source_id or self.as_of):
            return ""
        where = f" from {self.source_id}" if self.source_id else ""
        if self.as_of:
            return f"Ownership stated as of {self.as_of}{where}."
        return f"Ownership link recorded{where}; no date stated by the source."


def _summary_name(summary: Mapping[str, Any]) -> str | None:
    name = summary.get("name_canonical") or summary.get("name")
    return str(name) if name else None


def _summary_href(summary: Mapping[str, Any]) -> str | None:
    ident = summary.get("slug") or summary.get("public_id")
    return f"/organizations/{ident}" if ident else None


def claim_from_edge(edge: Any) -> Claim | None:
    """An `OwnershipEdge` from the API (`{organization, source_id, as_of, share_pct}`) as a
    renderable claim. `share_pct` is carried on the row but not in the sentence: it is null on
    every link today and a sentence with a blank in it reads worse than no sentence."""
    if not isinstance(edge, Mapping):
        return None
    summary = edge.get("organization")
    if not isinstance(summary, Mapping):
        return None
    name = _summary_name(summary)
    if not name:
        return None
    return Claim(
        name=name,
        href=_summary_href(summary),
        public_id=summary.get("public_id"),
        source_id=edge.get("source_id"),
        as_of=edge.get("as_of"),
    )


def ancestor_claims(entity: Mapping[str, Any]) -> list[Claim]:
    """The chain above this organisation, root first -- the breadcrumb trail
    (Tallgrass Energy / Trailblazer Pipeline Co). Empty when there is no parent.

    The provenance is shifted one crumb down on purpose. An API `ancestors` entry names a *parent*
    and carries the date of the link that reaches it from below, so entry *i*'s date is a
    statement about the organisation in entry *i+1*, not about the one it names. Rendering it
    where it arrived would put "as of 2024-06-30" under the wrong company, which for an ownership
    claim is worse than showing no date at all. Each crumb therefore carries the claim about *its
    own* parent; the topmost crumb carries none, because the link above it is outside the chain.
    """
    edges = [e for e in (entity.get("ancestors") or []) if isinstance(e, Mapping)]
    out: list[Claim] = []
    for i, edge in enumerate(edges):
        claim = claim_from_edge(edge)
        if claim is None:
            continue
        own = claim_from_edge(edges[i - 1]) if i else None
        out.append(
            Claim(
                name=claim.name,
                href=claim.href,
                public_id=claim.public_id,
                source_id=own.source_id if own else None,
                as_of=own.as_of if own else None,
            )
        )
    return out


def parent_claim(entity: Mapping[str, Any]) -> Claim | None:
    """The direct parent with its provenance. Falls back to the bare `parent` summary when an
    older API build sends no `parent_edge`, in which case the claim is undated and says so --
    which is the correct reading of a response that carries no date."""
    claim = claim_from_edge(entity.get("parent_edge"))
    if claim is not None:
        return claim
    summary = entity.get("parent")
    if isinstance(summary, Mapping) and _summary_name(summary):
        return Claim(
            name=str(_summary_name(summary)),
            href=_summary_href(summary),
            public_id=summary.get("public_id"),
            source_id=None,
            as_of=None,
        )
    return None


@dataclass(frozen=True)
class PortfolioRow:
    name: str
    href: str | None
    assets: int
    by_type: Mapping[str, int]
    is_subject: bool


def portfolio_rows(totals: Mapping[str, Any] | None, *, subject_public_id: str | None) -> list[PortfolioRow]:
    """`totals.by_organization` as rows a table can render, largest holding first (the API's
    order), the page's own subject marked so the reader can see where they are standing."""
    rows: list[PortfolioRow] = []
    for raw in (totals or {}).get("by_organization") or []:
        if not isinstance(raw, Mapping):
            continue
        summary = raw.get("organization")
        if not isinstance(summary, Mapping):
            continue
        name = _summary_name(summary)
        if not name:
            continue
        by_type_raw = raw.get("by_type")
        rows.append(
            PortfolioRow(
                name=name,
                href=_summary_href(summary),
                assets=int(raw.get("assets") or 0),
                by_type=dict(by_type_raw) if isinstance(by_type_raw, Mapping) else {},
                is_subject=bool(subject_public_id) and summary.get("public_id") == subject_public_id,
            )
        )
    return rows


@dataclass(frozen=True)
class GroupView:
    """What this node renders: a company page, or a fund page.

    `is_group` is the only thing that changes the layout's shape. A single organisation renders
    exactly as it did before 2026-09-20, whatever it holds -- the thresholds below apply to a
    *group* scope, because "hundreds of dots across dozens of portfolio companies" is the case
    that needed an answer and a company's own hundred assets is not.
    """

    scope: str
    is_group: bool
    #: Organisations in the scope holding at least one visible asset.
    holder_count: int
    #: Organisations in the scope, holding assets or not.
    organization_count: int
    assets: int
    show_map: bool
    show_asset_table: bool
    #: Why the map or the table is absent, in words the page prints. Empty when both render.
    notes: tuple[str, ...] = field(default=())
    truncated: bool = False
    depth: int = 0


def group_view(
    *,
    scope: str,
    totals: Mapping[str, Any] | None,
    scope_meta: Mapping[str, Any] | None,
    asset_rows: Sequence[Any] = (),
) -> GroupView:
    """Decide the layout from the API's own counts.

    `totals.organization_count` is the number of organisations in scope that actually hold an
    asset edge; `scope.organizations` is the whole scope including the ones that hold nothing (a
    holding company is usually one of those). Both are stated on the page, because "9 companies,
    7 of which hold assets" is a different fact from either number alone.
    """
    meta: Mapping[str, Any] = scope_meta if isinstance(scope_meta, Mapping) else {}
    counts: Mapping[str, Any] = totals if isinstance(totals, Mapping) else {}
    holder_count = int(counts.get("organization_count") or 0)
    organization_count = int(meta.get("organizations") or 1)
    assets = int(counts.get("assets") or len(asset_rows))
    is_group = holder_count > 1
    notes: list[str] = []
    show_map = True
    show_asset_table = True
    if is_group and assets > GROUP_MAP_MAX_ASSETS:
        show_map = False
        notes.append(
            f"No map: {assets} assets across {holder_count} companies is past the "
            f"{GROUP_MAP_MAX_ASSETS} this page can draw, and a partial map of a group would not "
            "be a picture of it. Open a company below for its own map."
        )
    if is_group and assets > GROUP_TABLE_MAX_ASSETS:
        show_asset_table = False
        notes.append(
            f"The {assets} assets are listed on each company's own page rather than here: one "
            f"page holds {GROUP_TABLE_MAX_ASSETS}, so a single table would show an arbitrary "
            "part of the group."
        )
    if meta.get("truncated"):
        notes.append(
            f"This group is larger than {meta.get('max_organizations')} companies; the counts "
            "below cover the part that was read."
        )
    if meta.get("depth_capped"):
        notes.append(
            f"The ownership chain goes deeper than the {meta.get('max_depth')} levels this page "
            "walks; companies below that are not counted here."
        )
    if meta.get("cycle_detected"):
        notes.append(
            "The ownership records for this group point back on themselves; each company is counted once."
        )
    return GroupView(
        scope=scope,
        is_group=is_group,
        holder_count=holder_count,
        organization_count=organization_count,
        assets=assets,
        show_map=show_map,
        show_asset_table=show_asset_table,
        notes=tuple(notes),
        truncated=bool(meta.get("truncated")),
        depth=int(meta.get("depth") or 0),
    )


def scope_links(path: str, current: str, *, descendant_count: int) -> list[dict[str, Any]]:
    """The three scope choices as plain links (no JavaScript, docs/04 D-30). Omitted entirely for
    an organisation with nothing below it, where all three would return the same page."""
    if descendant_count <= 0:
        return []
    return [
        {
            "scope": token,
            "label": SCOPE_LABELS[token],
            "href": path if token == DEFAULT_SCOPE else f"{path}?{SCOPE_PARAM}={token}",
            "current": token == current,
        }
        for token in SCOPES
    ]
