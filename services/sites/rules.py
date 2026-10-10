"""The site rules, pure (docs/21 §3.25; owner decision 2026-10-10).

Nothing here reads the store or imports the pipeline: `evidence.py` turns records into `Basis`
values and grouping `Edge`s, `build.py` persists what these functions decide, and the API re-runs
`rank` and `label` over the members a caller may see when the stored lead is hidden from them.

**Membership** is the connected components of the grouping edges (union-find, so transitive within
a site). Each member keeps the strongest rule among its own edges (`GROUPING_RULES` order).

**Lead** ("put them under the latest and largest filing"): among members that are not withdrawn or
cancelled, the largest `capacity_mw`; ties broken by the latest filing date, then `public_id`. When
every member is inactive, the largest overall. "Latest" alone would let a small add-on filed last
month headline an 11 GW campus; excluding withdrawn members is how "latest" wins for re-filings.

**Two levels** (`label_site`; owner, 2026-10-10): site > plant group > unit. Generators of one EIA
plant form a group headed by its first member in lead order; a record with no EIA plant is a group
of its own. A unit is `unit_of` its own group's head. A group head is labelled relative to the lead
group, first match wins (`label`): `co_located`, `phase_of`, `expansion_of` / `expanded_by`,
`superseded_by` / `refiling_of`, `same_site`. Sponsor is never a level. Each label carries the rule
that fired and a confidence word (`high | medium | low`), never a probability: no labelled sample
exists to calibrate one (docs/51 §2.8 item 6).

**Stable ids** (`inherit`): a rebuilt cluster takes the id of the existing site it shares the most
members with (ties: the oldest site); a site no cluster takes is retired, never reused.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

#: Version of the rules below, stored on every site so a rule change is visible in the data.
RULE_VERSION = "2026-10-10.1"

#: Grouping rules, strongest first; a member stores the first of these among its own edges.
GROUPING_RULES: tuple[str, ...] = (
    "eia_plant",  # a. the same EIA plant id (EIA-860M record id `<plant>-<generator>` or `plant:<plant>`)
    "exact_point_sponsor",  # b. the same exact point and the same sponsor organisation
    "exact_point_stem",  # b. the same exact point and a shared name stem, sponsors not in conflict
    "poi_sponsor",  # c. the same interconnection point and the same sponsor organisation
    "poi_stem",  # c. the same interconnection point and a shared name stem, sponsors not in conflict
)

#: Member -> lead relations. Each reads "member <relation> lead".
RELATIONS: tuple[str, ...] = (
    "lead",
    "unit_of",  # another generator of the lead's EIA plant
    "phase_of",  # another numbered phase of the lead's project (shared stem, differing markers)
    "co_located",  # a different technology family at the lead's site
    "refiling_of",  # a later filing for the lead's project, itself withdrawn
    "superseded_by",  # an earlier, withdrawn filing the lead replaces
    "expansion_of",  # an addition to the lead (an operating member, or a stated MW increase)
    "expanded_by",  # the operating member the lead adds to
    "same_site",  # grouped, relation unclear
)
CONFIDENCES: tuple[str, ...] = ("high", "medium", "low")
#: The one non-hierarchical relation: records at the same interconnection point that the rules did
#: not group (another or no named developer). Computed at read time, served as its own list.
NEIGHBOUR_RELATION = "shares_interconnection_point"
#: `site.review_flag` values.
REVIEW_FLAGS: tuple[str, ...] = ("oversize",)

INACTIVE_STATES = frozenset({"withdrawn", "cancelled"})
BUILT_STATE = "built"
#: A site grouping more distinct things than this (`effective_size`: one EIA plant counts once) is
#: flagged for review, logged and withheld from readers rather than built silently. Project Matador
#: (161 records: 157 + 4 generators of two EIA plants at one exact point) counts as 2.
OVERSIZE_MEMBERS = 60

K = TypeVar("K", bound=Hashable)


# --------------------------------------------------------------------------------------- basis
@dataclass(frozen=True)
class Basis:
    """What the lead and label rules read about one member. Stored with the membership
    (`site_member.basis`) so the API can re-run the rules over a viewer's visible members."""

    public_id: str
    capacity_mw: float | None
    lifecycle_state: str
    #: Latest queue/application date among the record's active links (ISO date), if any.
    filing_date: str | None = None
    plant_ids: tuple[str, ...] = ()
    stems: tuple[str, ...] = ()
    #: Phase / unit markers in the record's name ("II" -> "2", "Phase B" -> "B").
    phases: tuple[str, ...] = ()
    #: The name says expansion / extension / repower / uprate / addition.
    expansion_marker: bool = False
    #: The source states an MW increase on an already connected project (NESO TEC register).
    mw_increase: bool = False
    #: Technology families (`pipeline.resolve.TECH_FAMILIES`); `None` when the technology is unknown.
    families: tuple[str, ...] | None = None
    sponsor_key: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "public_id": self.public_id,
            "capacity_mw": self.capacity_mw,
            "lifecycle_state": self.lifecycle_state,
            "filing_date": self.filing_date,
            "plant_ids": list(self.plant_ids),
            "stems": list(self.stems),
            "phases": list(self.phases),
            "expansion_marker": self.expansion_marker,
            "mw_increase": self.mw_increase,
            "families": list(self.families) if self.families is not None else None,
            "sponsor_key": self.sponsor_key,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Basis:
        families = data.get("families")
        capacity = data.get("capacity_mw")
        return cls(
            public_id=str(data["public_id"]),
            capacity_mw=float(capacity) if capacity is not None else None,
            lifecycle_state=str(data.get("lifecycle_state") or "unknown"),
            filing_date=data.get("filing_date"),
            plant_ids=tuple(data.get("plant_ids") or ()),
            stems=tuple(data.get("stems") or ()),
            phases=tuple(data.get("phases") or ()),
            expansion_marker=bool(data.get("expansion_marker")),
            mw_increase=bool(data.get("mw_increase")),
            families=tuple(families) if families is not None else None,
            sponsor_key=data.get("sponsor_key"),
        )

    @property
    def inactive(self) -> bool:
        return self.lifecycle_state in INACTIVE_STATES


def _ordinal(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return dt.date.fromisoformat(str(iso)[:10]).toordinal()
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------- lead
def lead_order_key(b: Basis) -> tuple[Any, ...]:
    """Ascending sort key whose first element is the lead: active before withdrawn/cancelled,
    then the largest stated capacity (none stated sorts last), then the latest filing date (none
    sorts last), then `public_id` so the order is total."""
    filed = _ordinal(b.filing_date)
    return (
        b.inactive,
        b.capacity_mw is None,
        -(b.capacity_mw or 0.0),
        filed is None,
        -(filed or 0),
        b.public_id,
    )


def rank(members: Iterable[Basis]) -> list[Basis]:
    """`members` in lead order: the first is the lead."""
    return sorted(members, key=lead_order_key)


# -------------------------------------------------------------------------------------- labels
@dataclass(frozen=True)
class Label:
    relation: str
    rule: str
    confidence: str


LEAD_LABEL = Label("lead", "largest_active_then_latest", "high")


def _weaken(confidence: str) -> str:
    return {"high": "medium", "medium": "low"}.get(confidence, "low")


def label(member: Basis, lead: Basis) -> Label:
    """How `member` relates to `lead`; the first rule that holds wins.

    1. `unit_of` (high): they share an EIA plant id -- generators of one plant.
    2. `co_located`: both technologies known and their families disjoint (solar + storage is one
       family set, so a hybrid and its storage are not co-located; gas + nuclear are).
    3. `phase_of`: a shared name stem and differing phase markers (high when both names carry one,
       medium when only one does).
    4. `expansion_of` / `expanded_by`: the source states an MW increase on a connected project
       (high), the name marks an expansion (medium), or one of the two is operating and the other a
       live later filing (medium).
    5. `superseded_by` / `refiling_of`: the member is withdrawn or cancelled and the lead is a
       filing of the same family -- earlier member: superseded by the lead; later member: a
       re-filing that was itself withdrawn (low).
    6. `same_site` (low): grouped, relation unclear.

    Rules 2 and 4-5 are inferred, not stated: their confidence drops one step when the member and
    the lead share neither a sponsor nor a name stem (they are in one site only transitively)."""
    if member.public_id == lead.public_id:
        return LEAD_LABEL
    if set(member.plant_ids) & set(lead.plant_ids):
        return Label("unit_of", "shared_eia_plant", "high")
    same_sponsor = member.sponsor_key is not None and member.sponsor_key == lead.sponsor_key
    shared_stem = bool(set(member.stems) & set(lead.stems))
    kin = same_sponsor or shared_stem

    def inferred(confidence: str) -> str:
        return confidence if kin else _weaken(confidence)

    fm, fl = member.families, lead.families
    if fm is not None and fl is not None and not set(fm) & set(fl):
        return Label("co_located", "different_technology_family", inferred("high"))
    if shared_stem and set(member.phases) != set(lead.phases):
        both = bool(member.phases) and bool(lead.phases)
        return Label("phase_of", "differing_phase_markers", "high" if both else "medium")
    m_built, l_built = member.lifecycle_state == BUILT_STATE, lead.lifecycle_state == BUILT_STATE
    if member.mw_increase and not lead.mw_increase:
        return Label("expansion_of", "source_states_mw_increase", "high")
    if lead.mw_increase and not member.mw_increase and m_built:
        return Label("expanded_by", "source_states_mw_increase", "high")
    if member.expansion_marker and not lead.expansion_marker and not member.inactive:
        return Label("expansion_of", "name_marks_expansion", inferred("medium"))
    if lead.expansion_marker and not member.expansion_marker and m_built:
        return Label("expanded_by", "name_marks_expansion", inferred("medium"))
    if l_built and not m_built and not member.inactive:
        return Label("expansion_of", "later_filing_at_operating_member", inferred("medium"))
    if m_built and not l_built and not lead.inactive:
        return Label("expanded_by", "later_filing_at_operating_member", inferred("medium"))
    if member.inactive:
        mo, lo = _ordinal(member.filing_date), _ordinal(lead.filing_date)
        if mo is not None and lo is not None and mo > lo:
            return Label("refiling_of", "withdrawn_later_filing", "low")
        if mo is not None and lo is not None:
            rule = "withdrawn_earlier_filing" if not lead.inactive else "earlier_filing_both_withdrawn"
            return Label("superseded_by", rule, inferred("medium") if not lead.inactive else "low")
        return Label("superseded_by", "withdrawn_filing_dates_unknown", "low")
    return Label("same_site", "grouped_relation_unclear", "low")


UNIT_LABEL = Label("unit_of", "shared_eia_plant", "high")
#: `site_member.group_key` prefixes: a group of one EIA plant (or of plants one record bridges), or
#: a record with no EIA plant, which is a group of its own.
PLANT_GROUP_PREFIX = "eia:"
SINGLE_GROUP_PREFIX = "proposal:"


@dataclass(frozen=True)
class Placement:
    """One member's place in its site: its rank in display order (0 = the lead), its group, the
    group's head (`parent`, a `public_id`; None for a head) and its label. A head's label is
    relative to the site's lead; a unit's is `unit_of` its head."""

    basis: Basis
    label: Label
    rank: int
    group_key: str
    parent: str | None


def plant_groups(members: Sequence[Basis]) -> dict[str, str]:
    """`public_id -> group_key`: members sharing an EIA plant id are one group (transitively, so a
    merged record holding generators of several plants joins them), keyed by its sorted plant ids;
    a member with no plant id is a group of its own."""
    uf = UnionFind(len(members))
    first: dict[str, int] = {}
    for i, m in enumerate(members):
        for plant in m.plant_ids:
            if plant in first:
                uf.union(first[plant], i)
            else:
                first[plant] = i
    plants: dict[int, set[str]] = defaultdict(set)
    for i, m in enumerate(members):
        plants[uf.find(i)].update(m.plant_ids)
    out: dict[str, str] = {}
    for i, m in enumerate(members):
        group = plants[uf.find(i)]
        out[m.public_id] = (
            PLANT_GROUP_PREFIX + ",".join(sorted(group, key=lambda p: (len(p), p)))
            if group
            else SINGLE_GROUP_PREFIX + m.public_id
        )
    return out


def _group_basis(head: Basis, members: Sequence[Basis]) -> Basis:
    """The group as one record for the between-group labels: the head's own facts, with the plant
    ids, stems and technology families of every member (a gas plant of 157 units is gas)."""
    families: set[str] = set()
    known = False
    for m in members:
        if m.families is not None:
            known = True
            families.update(m.families)
    return Basis(
        public_id=head.public_id,
        capacity_mw=head.capacity_mw,
        lifecycle_state=head.lifecycle_state,
        filing_date=head.filing_date,
        plant_ids=tuple(sorted({p for m in members for p in m.plant_ids})),
        stems=tuple(sorted({s for m in members for s in m.stems})),
        phases=head.phases,
        expansion_marker=head.expansion_marker,
        mw_increase=any(m.mw_increase for m in members),
        families=tuple(sorted(families)) if known else None,
        sponsor_key=head.sponsor_key,
    )


def label_site(members: Iterable[Basis]) -> list[Placement]:
    """Two levels where EIA provides them: site > plant group > unit (owner, 2026-10-10).

    The lead is the first member in lead order (`rank`); its group is the lead group. Groups are
    listed in the lead order of their heads (each group's first member in that order), each head
    followed by its units. A head is labelled against the lead group (`label` over the two
    `_group_basis`), so `phase_of`, `co_located`, `refiling_of`, `superseded_by`, `expansion_of`,
    `expanded_by` and `same_site` are relations between groups; `unit_of` only ever means "another
    generator of this member's own plant group", relative to that group's head, never to a lead
    that is a unit of a different plant."""
    ordered = rank(members)
    if not ordered:
        return []
    groups = plant_groups(ordered)
    by_group: dict[str, list[Basis]] = {}
    for m in ordered:
        by_group.setdefault(groups[m.public_id], []).append(m)
    lead_group = groups[ordered[0].public_id]
    lead_basis = _group_basis(ordered[0], by_group[lead_group])
    out: list[Placement] = []
    for key, group in by_group.items():
        head = group[0]
        head_label = LEAD_LABEL if key == lead_group else label(_group_basis(head, group), lead_basis)
        out.append(Placement(head, head_label, len(out), key, None))
        out.extend(Placement(m, UNIT_LABEL, len(out), key, head.public_id) for m in group[1:])
    return out


# ---------------------------------------------------------------------------------- membership
@dataclass(frozen=True)
class Edge:
    """One piece of grouping evidence between records `a` and `b` (indexes into the candidate
    list) under `rule`, with the identifier that carried it (`evidence`)."""

    a: int
    b: int
    rule: str
    evidence: Mapping[str, Any] = field(default_factory=dict)


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # The lower index is the root: the result does not depend on the edge order.
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass
class Component:
    """One cluster: member indexes (ascending), each member's strongest rule and the rules and
    identifiers over all its edges."""

    members: list[int]
    rule_of: dict[int, str]
    evidence_of: dict[int, dict[str, Any]]

    @property
    def rules(self) -> set[str]:
        return set(self.rule_of.values())


def components(n: int, edges: Iterable[Edge]) -> list[Component]:
    """Connected components of size >= 2, ordered by their smallest member index."""
    uf = UnionFind(n)
    per_member: dict[int, list[Edge]] = defaultdict(list)
    for e in edges:
        if e.a == e.b:
            continue
        uf.union(e.a, e.b)
        per_member[e.a].append(e)
        per_member[e.b].append(e)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in per_member:
        groups[uf.find(i)].append(i)
    out: list[Component] = []
    for root in sorted(groups):
        members = sorted(groups[root])
        if len(members) < 2:
            continue
        rule_of: dict[int, str] = {}
        evidence_of: dict[int, dict[str, Any]] = {}
        for i in members:
            mine = per_member[i]
            rules = sorted({e.rule for e in mine}, key=GROUPING_RULES.index)
            rule_of[i] = rules[0]
            ev: dict[str, Any] = {"rules": rules}
            for e in mine:
                for key, value in e.evidence.items():
                    bucket = ev.setdefault(key, [])
                    if value not in bucket:
                        bucket.append(value)
            for key, value in ev.items():
                if key != "rules" and isinstance(value, list):
                    ev[key] = sorted(value, key=str)
            evidence_of[i] = ev
        out.append(Component(members, rule_of, evidence_of))
    return out


def effective_size(plant_ids: Sequence[Iterable[str]]) -> int:
    """How many distinct things a site groups: members sharing an EIA plant id count once (a plant
    of 157 generators is one plant), every other member counts as itself. Union-find again, over
    the members' plant ids."""
    uf = UnionFind(len(plant_ids))
    first: dict[str, int] = {}
    for i, plants in enumerate(plant_ids):
        for plant in plants:
            if plant in first:
                uf.union(first[plant], i)
            else:
                first[plant] = i
    return len({uf.find(i) for i in range(len(plant_ids))})


def needs_review(plant_ids: Sequence[Iterable[str]]) -> bool:
    """`oversize`: the site groups more than `OVERSIZE_MEMBERS` distinct things (`effective_size`).
    Such a site is built, flagged, logged and reported, and not served until reviewed: a chain of
    shared points and names that long is a pathology to look at, not a campus to publish."""
    return effective_size(plant_ids) > OVERSIZE_MEMBERS


# ------------------------------------------------------------------------------------ stable ids
@dataclass(frozen=True)
class PriorSite(Generic[K]):
    """An existing, unretired site as the rebuild sees it."""

    site_id: Any
    created_at: dt.datetime
    members: frozenset[K]


@dataclass
class Inheritance(Generic[K]):
    #: For each new cluster (same order as given), the prior site whose id it keeps, or None.
    assigned: list[PriorSite[K] | None]
    #: Prior sites no cluster took, each with the index of the cluster holding most of its former
    #: members (its successor), or None when they are all gone.
    retired: list[tuple[PriorSite[K], int | None]]


def inherit(clusters: Sequence[frozenset[K]], prior: Sequence[PriorSite[K]]) -> Inheritance[K]:
    """Give each rebuilt cluster the id of the prior site it shares the most members with.

    Greedy over (shared members desc, prior site's age asc, site id, cluster index): a split keeps
    the id on the part holding most of the old members; a merge keeps the older site's id and
    retires the other with the merged cluster as its successor. Deterministic for a given input."""
    where: dict[K, int] = {}
    for ci, cluster in enumerate(clusters):
        for member in cluster:
            where[member] = ci
    overlaps: list[tuple[int, dt.datetime, str, int, int]] = []
    for pi, site in enumerate(prior):
        counts: dict[int, int] = defaultdict(int)
        for member in site.members:
            hit = where.get(member)
            if hit is not None:
                counts[hit] += 1
        overlaps.extend((-n, site.created_at, str(site.site_id), ci, pi) for ci, n in counts.items())
    overlaps.sort()
    assigned: list[PriorSite[K] | None] = [None] * len(clusters)
    taken: set[int] = set()
    best_cluster: dict[int, int] = {}
    for _neg, _created, _sid, ci, pi in overlaps:
        best_cluster.setdefault(pi, ci)
        if assigned[ci] is None and pi not in taken:
            assigned[ci] = prior[pi]
            taken.add(pi)
    retired = [(site, best_cluster.get(pi)) for pi, site in enumerate(prior) if pi not in taken]
    return Inheritance(assigned, retired)
