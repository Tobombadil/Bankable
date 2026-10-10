"""The facts the site rules read about each live proposal, and the grouping edges (docs/21 §3.25).

**Rules, unique identifiers first** (each edge records the rule and the identifier that carried it):

a. `eia_plant`: the same EIA plant id. Read from the record's active EIA-860M links: the source
   record id is `<plant>-<generator>` (a Planned generator) or `plant:<plant>` (an operating plant
   linked by the close-out rule, `services/resolve/closeout.py`). Never from `identifiers`, which a
   merge unions.
b. `exact_point_*`: both locations are `exact` and within `EXACT_POINT_TOLERANCE_M`, AND the same
   sponsor organisation (`exact_point_sponsor`) or a shared name stem with sponsors not in conflict
   (`exact_point_stem`). Unrelated developers at one point never group.
c. `poi_*`: the same interconnection point, AND the same sponsor (`poi_sponsor`) or a shared name
   stem that is not just the point's own name, sponsors not in conflict (`poi_stem`).
d. County, state and country centroids group nothing: only `exact` points enter rule b.

**Name stem** (`name_stem`): the name lower-cased, parentheticals dropped, then legal forms,
technology and facility words, phase words (phase, unit, stage, expansion, repower ...), roman
numerals, number words after the first word, tokens of one or two letters ("A", "BR", "GG", state
codes) and every token holding a digit removed. "Darden IV Solar" and "DARDEN" share `darden`;
"Keys Hollow Storage Phase II SLF" and "Keys Hollow Solar Phase II SLF" share `keys hollow`. A
stem shorter than four letters is no stem.

**Sponsor conflict**: both records name a sponsor, the organisations differ, their ultimate parents
(`organization.parent_org_id`) differ, and their name stems neither match nor start with the same
word of four or more letters. "Darden Solar I LLC" and "Darden Solar II LLC", or "Fermi America" and
"Fermi Nuclear", are two organisations without a conflict; NextEra and Invenergy conflict, so a
shared project stem between them groups nothing.

**Exact-point tolerance, 200 m.** Measured on the beta store 2026-10-10 (docs/21 §3.25):
generators of one plant carry identical coordinates; 47 compatible pairs sit between 0 and 200 m
(Richland Parish 3/4 at 153 m); the 67 between 200 m and 1 km mix phases of one project (Mammoth
Plains I/II at 243 m) with sponsor-only chains of rooftop portfolios and data-centre buildings
(Prologis Bensenville, six buildings). 1 km would add 50 members.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pipeline.resolve import TECH_FAMILIES, phase_tokens
from services.sites.rules import Basis, Edge

EIA_SOURCE_ID = "us.eia.860m"
NESO_SOURCE_ID = "gb.neso.tec_register"
EXACT_PRECISION = "exact"
EXACT_POINT_TOLERANCE_M = 200.0
_EARTH_RADIUS_M = 6_371_008.8

_TOKEN = re.compile(r"[a-z0-9]+")
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_LEGAL = frozenset(
    "llc llp lp ltd limited inc incorporated corp corporation co company plc gmbh sa ag bv nv "
    "holdings holding partners trust".split()
)
_FACILITY = frozenset(
    "solar pv photovoltaic wind windfarm windpark offshore onshore energy energies power powerplant "
    "storage battery batteries bess ess hybrid project projects center centre farm farms park "
    "facility facilities generating generation generator station plant site gas natural ccgt ocgt "
    "cc ct peaker peaking nuclear hydro hydroelectric geothermal biomass renewable renewables clean "
    "green slf mw kv substation the and of at an".split()
)
_PHASE_WORDS = frozenset(
    "phase ph unit units stage block tranche expansion extension ext addition repower repowering "
    "uprate augmentation number".split()
)
_ROMAN = frozenset("ii iii iv vi vii viii ix".split())
_NUMBER_WORDS = frozenset("one two three four five six seven eight nine ten".split())
_EXPANSION = re.compile(
    r"\b(expansion|extension|ext|repower|repowering|uprate|addition|augmentation)\b", re.IGNORECASE
)
#: Fewer letters than this is not a stem ("ab", "sun").
MIN_STEM_LETTERS = 4


def name_stem(name: str | None) -> str | None:
    """The identity-bearing words of a project or organisation name (module docstring)."""
    if not name:
        return None
    text = _PARENTHETICAL.sub(" ", str(name).lower()).replace("&", " and ")
    kept: list[str] = []
    for k, token in enumerate(_TOKEN.findall(text)):
        if len(token) <= 2 or any(ch.isdigit() for ch in token):
            continue
        if token in _LEGAL or token in _FACILITY or token in _PHASE_WORDS or token in _ROMAN:
            continue
        if token in _NUMBER_WORDS and k > 0:
            continue
        kept.append(token)
    stem = " ".join(kept)
    return stem if len(stem.replace(" ", "")) >= MIN_STEM_LETTERS else None


def phase_markers(name: str | None) -> tuple[str, ...]:
    """Phase / unit markers the resolver reads in a name (`pipeline.resolve.phase_tokens`):
    "Darden II Solar" -> ("2",), "Solar Phase B" -> ("B",), "Darden" -> ()."""
    return tuple(sorted(str(t) for t in phase_tokens(name)))


def has_expansion_marker(name: str | None) -> bool:
    return bool(name) and _EXPANSION.search(str(name)) is not None


def technology_families(technology: str | None) -> tuple[str, ...] | None:
    if not technology:
        return None
    families = TECH_FAMILIES.get(str(technology))
    return tuple(sorted(families)) if families is not None else None


def eia_plant_id(source_id: str, source_record_id: str) -> str | None:
    """The EIA plant id an active link names: `<plant>-<generator>` or `plant:<plant>`."""
    if source_id != EIA_SOURCE_ID or not source_record_id:
        return None
    text = str(source_record_id)
    plant = text[len("plant:") :] if text.startswith("plant:") else text.split("-", 1)[0]
    plant = plant.strip()
    return plant if plant.isdigit() else None


def _number(value: object) -> float | None:
    try:
        return float(str(value).replace(",", "")) if value not in (None, "") else None
    except ValueError:
        return None


def neso_mw_increase(raw: dict[str, object] | None) -> bool:
    """The TEC register states an increase on an already connected project: both `MW Connected`
    and `MW Increase / Decrease` positive (17 of 2,225 rows on 2026-10-10). Most rows carry their
    whole requested MW in the increase column with nothing connected; that is not an expansion."""
    if not raw:
        return False
    connected = _number(raw.get("MW Connected"))
    increase = _number(raw.get("MW Increase / Decrease"))
    return bool(connected and connected > 0 and increase and increase > 0)


# ----------------------------------------------------------------------------------- candidate
@dataclass(frozen=True)
class Candidate:
    """One live proposal as the grouping rules see it."""

    #: The proposal's internal id (a uuid in the store; any hashable in tests).
    proposal_id: Any
    public_id: str
    name: str
    technology: str | None
    capacity_mw: float | None
    lifecycle_state: str
    #: The sponsor organisation after organisation merges, as text; None when not named.
    sponsor_key: str | None = None
    sponsor_name: str | None = None
    #: The sponsor's ultimate parent organisation (itself when it has none).
    sponsor_family: str | None = None
    poi_key: str | None = None
    poi_name: str | None = None
    precision: str | None = None
    lon: float | None = None
    lat: float | None = None
    plant_ids: frozenset[str] = frozenset()
    #: Every name the record goes by: its own, then its active links' names.
    names: tuple[str, ...] = ()
    filing_date: str | None = None
    mw_increase: bool = False

    @property
    def stems(self) -> frozenset[str]:
        return frozenset(s for s in (name_stem(n) for n in (self.name, *self.names)) if s)

    @property
    def project_vehicle(self) -> bool:
        """The sponsor is a project company named after the project (its name stem is one of the
        record's own stems), not a developer."""
        stem = name_stem(self.sponsor_name)
        return stem is not None and stem in self.stems

    @property
    def exact(self) -> bool:
        return self.precision == EXACT_PRECISION and self.lon is not None and self.lat is not None

    def basis(self) -> Basis:
        return Basis(
            public_id=self.public_id,
            capacity_mw=self.capacity_mw,
            lifecycle_state=self.lifecycle_state,
            filing_date=self.filing_date,
            plant_ids=tuple(sorted(self.plant_ids)),
            stems=tuple(sorted(self.stems)),
            phases=phase_markers(self.name),
            expansion_marker=has_expansion_marker(self.name),
            mw_increase=self.mw_increase,
            families=technology_families(self.technology),
            sponsor_key=self.sponsor_key,
        )


def sponsor_relation(a: Candidate, b: Candidate) -> str:
    """`same` (one organisation), `conflict` (module docstring) or `compatible`. A sponsor named
    after the project itself (`Candidate.project_vehicle`: "Cowboy Solar I" sponsoring "Cowboy
    Solar II") says nothing about who develops it and counts as not named."""
    if a.sponsor_key is not None and a.sponsor_key == b.sponsor_key:
        return "same"
    if a.sponsor_key is None or b.sponsor_key is None or a.project_vehicle or b.project_vehicle:
        return "compatible"
    if a.sponsor_family is not None and a.sponsor_family == b.sponsor_family:
        return "compatible"
    sa, sb = name_stem(a.sponsor_name), name_stem(b.sponsor_name)
    if sa is None or sb is None:
        return "conflict"
    # One house name heads both: "Fermi America" and "Fermi Nuclear" (stem "fermi"), "Darden Solar I
    # LLC" and "Darden Solar II LLC". The first word must be a word, not an initial.
    first_a, first_b = sa.split()[0], sb.split()[0]
    house = first_a == first_b and len(first_a) >= MIN_STEM_LETTERS
    return "compatible" if sa == sb or house else "conflict"


def _poi_tokens(name: str | None) -> frozenset[str]:
    return frozenset(_TOKEN.findall(str(name or "").lower()))


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def _pair_edge(a: Candidate, b: Candidate, ia: int, ib: int, kind: str, shared: Iterable[str]) -> Edge | None:
    relation = sponsor_relation(a, b)
    if relation == "same":
        return Edge(ia, ib, f"{kind}_sponsor", {"sponsor": a.sponsor_key})
    stems = sorted(shared)
    if stems and relation == "compatible":
        return Edge(ia, ib, f"{kind}_stem", {"stem": stems[0]})
    return None


def grouping_edges(
    candidates: Sequence[Candidate], *, tolerance_m: float = EXACT_POINT_TOLERANCE_M
) -> list[Edge]:
    """Rules a-c over `candidates` (module docstring). Deterministic in the input order."""
    edges: list[Edge] = []
    stems = [c.stems for c in candidates]

    # a. the same EIA plant id: a star from the first record naming it.
    by_plant: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(candidates):
        for plant in c.plant_ids:
            by_plant[plant].append(i)
    for plant in sorted(by_plant):
        idx = by_plant[plant]
        edges.extend(Edge(idx[0], j, "eia_plant", {"eia_plant_id": plant}) for j in idx[1:])

    # b. the same exact point (within the tolerance), every pair, through a grid of cells about
    # the tolerance wide so only neighbouring cells are compared.
    cell_deg = tolerance_m / 111_000.0
    points: dict[int, tuple[float, float]] = {
        i: (c.lon, c.lat)
        for i, c in enumerate(candidates)
        if c.exact and c.lon is not None and c.lat is not None
    }
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (lon, lat) in points.items():
        cells[(math.floor(lat / cell_deg), math.floor(lon / cell_deg))].append(i)
    for (cy, cx), here in sorted(cells.items()):
        # A degree of longitude shrinks with latitude, so look as many cells east and west as one
        # tolerance spans there.
        reach = math.ceil(1 / max(math.cos(math.radians(cy * cell_deg)), 0.01))
        nearby: list[int] = []
        for dy in (-1, 0, 1):
            for dx in range(-reach, reach + 1):
                nearby.extend(cells.get((cy + dy, cx + dx), ()))
        for i in here:
            for j in nearby:
                if j <= i or haversine_m(*points[i], *points[j]) > tolerance_m:
                    continue
                edge = _pair_edge(candidates[i], candidates[j], i, j, "exact_point", stems[i] & stems[j])
                if edge is not None:
                    edges.append(edge)

    # c. the same interconnection point, every pair at it.
    by_poi: dict[str, list[int]] = defaultdict(list)
    for i, c in enumerate(candidates):
        if c.poi_key is not None:
            by_poi[c.poi_key].append(i)
    for poi in sorted(by_poi):
        idx = by_poi[poi]
        if len(idx) < 2:
            continue
        own = _poi_tokens(candidates[idx[0]].poi_name)
        for k, i in enumerate(idx):
            for j in idx[k + 1 :]:
                # A stem made only of the point's own words is the substation's name, which
                # unrelated requests there borrow ("Imperial Valley Solar" at Imperial Valley).
                shared = {s for s in stems[i] & stems[j] if not set(s.split()) <= own}
                edge = _pair_edge(candidates[i], candidates[j], i, j, "poi", shared)
                if edge is not None:
                    edges.append(edge)
    return edges
