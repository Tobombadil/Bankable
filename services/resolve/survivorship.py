"""Field-level survivorship: the canonical value of each field of a merged proposal, and the
source that supplied it (docs/22 §23; audit 2026-09-30 data scientist F2, designer D-5).

A merge used to move source links and recompute nothing, so the survivor kept one member's name,
capacity, lifecycle and COD (`darden-ii-solar-vdtde2` showed one EIA generator's 342.7 MW against
a 1,150 MW CAISO request), and every later load of any member wrote that member's row over the
record (`services/ingest/loader.py::_update_existing_entity`), so the record showed whichever
source loaded last. This module is the one rule that decides each field from *all* of a record's
active links, applied:

* on merge and unmerge (`services/resolve/merge.py`);
* after every load, for each touched record with more than one active link
  (`services/ingest/loader.py::load_dataframe`);
* store-wide at every resolve tick (`services/resolve/report.py::apply_all_clusters`), which is
  also how a rule change reaches existing records (a restatement, not news: below);
* at serve time, over the links a caller may read, when a field's supplier is hidden
  (`services/api/visibility.py::GatedRecord`), so the fallback value follows the same rule.

`survive` is pure (members in, picks out). `apply_survivorship` writes the picks onto a stored
row, skipping fields an admin override pins (docs/21 §6.4: a human decision wins), and stamps
`field_provenance[field]` with the supplying source(s) and the rule that chose them.

## The rules (docs/22 §23.1 argues each one)

Each member is one active `proposal_source` row. Its role comes from its source's category in
`data/sources.yaml`: `generation_queue`/`load_queue` -> **request** (an interconnection request),
`registry` -> **inventory** (EIA-860M's generator inventory), anything else -> **other**
(permits, dockets). A request is *live* unless its own lifecycle is withdrawn/cancelled or its row
has left the register (`gone_at`).

* `capacity_mw`: the live requests' MW, summed over distinct requests (one source's rows with the
  same technology, MW and phase number count once: NYISO lists a project under its cluster id and
  its queue position), plus any inventory generator of a technology family no live request covers;
  else the inventory's generators summed (the plant rollup); else every request summed; else the
  largest stated value. The request is what is being proposed at the grid: its MW is the
  point-of-interconnection limit, while EIA nameplate counts a hybrid's solar and storage
  generators separately (docs/22 §22, `coherence_ratio`: "its MW is the point-of-interconnection
  total, not either half").
* `storage_mwh`: the same order over members stating MWh.
* `technology`, `technology_raw`, `kind`: from the members that supplied capacity. One technology
  when they agree; solar + storage -> `solar_storage`, wind + storage -> `wind_storage`; else the
  largest member's. `technology_raw` is that member's verbatim text (distinct texts of one source
  joined with " + " for a combined class), `kind` the largest member's.
* `lifecycle_state` and `status_raw` (one pair, from one member): the most advanced state among
  members still on their register, on the ladder announced < filed < studied < permitted <
  contracted < under_construction < built. A withdrawn or cancelled member does not end a project
  another member shows progressing; the record is withdrawn/cancelled only when every member is.
  `unknown` never wins over a known state. Ties: most recently retrieved, then the larger member,
  then source id, record id.
* `proposed_online_date`: the lifecycle winner's date when it states one, else the next member in
  the same order that does: the status line and the date it is read with come from one source.
* `name_canonical`: the inventory's plant name (the plant with the most MW; the stored name is kept
  when it is one of the tied candidates, so a rule run does not rename a record for nothing); else
  the largest live request's name; else the stored name when a member states it; else the most
  recent member's.
* `jurisdiction`: the most common subdivision-level value among members (the stored value wins a
  tie); `iso`: the requests' operator, else any member's.
* `identifiers`: the union of every member's keys -- `queue_ids` from every request, the primary EIA
  plant (the plant the name came from) as `eia_plant_id`, its generator as `eia_generator_id` when
  the record holds one generator, and `eia_generators` listing them all when it holds several.
  Other keys (`select_basis`, admin-entered keys) are left as stored.
* `first_seen` (store only): the earliest `first_seen` of the record and the rows merged into it
  (the store's clock; a link's `first_seen` is its source's retrieval time and is not compared).
* `location_id` (store only): the most precise location among the record and the rows merged into
  it; the stored one keeps a tie.

A field no member states keeps its stored value. `field_provenance[field]` is
`{source_id, licence_id, retrieved_at, rule}` plus `source_ids` when more than one source supplied
it; `services/api/visibility.py::GatedRecord` serves the stored value only when *every* named
source is readable at the caller's tier.

## Restatement, not news

Survivorship writes fields without writing events, like the loader's own field writes: news about a
member is its source's own diff event (`capacity_changed`, `status_change`), already published by
the runner with that source's before and after. A recomputed canonical value is a reclassification
of what the store already held, the same reading docs/22 §8.1 gives a status-map correction
(lane FX1) and §8.2 a capacity-rule correction (lane W1): `restate_all` reports how many records and
fields moved and emits nothing. `last_changed` moves forward on a record whose served values
changed, so `updated_since` and bulk sync see the correction.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid as _uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.resolve import phase_key, tech_families
from services.db.models import Location, Proposal, ProposalSource

#: Fields `survive` decides. `GatedRecord` re-derives any of them over readable links.
SURVIVING_FIELDS: tuple[str, ...] = (
    "kind",
    "name_canonical",
    "technology",
    "technology_raw",
    "capacity_mw",
    "storage_mwh",
    "jurisdiction",
    "iso",
    "lifecycle_state",
    "status_raw",
    "identifiers",
    "proposed_online_date",
)
#: Store-only columns `apply_survivorship` may also move (not source-attributed fields).
ROW_FIELDS: tuple[str, ...] = ("first_seen", "location_id")

#: `identifiers` keys survivorship owns; every other key is left as stored.
MANAGED_IDENTIFIER_KEYS: frozenset[str] = frozenset(
    {"queue_ids", "eia_plant_id", "eia_generator_id", "eia_generators"}
)

REQUEST_CATEGORIES: frozenset[str] = frozenset({"generation_queue", "load_queue"})
INVENTORY_CATEGORIES: frozenset[str] = frozenset({"registry"})

#: The lifecycle ladder (docs/22 §3: announced -> filed -> studied -> permitted -> contracted ->
#: built, with under_construction between contracted and built).
LIFECYCLE_RANK: dict[str, int] = {
    "announced": 1,
    "filed": 2,
    "studied": 3,
    "permitted": 4,
    "contracted": 5,
    "under_construction": 6,
    "built": 7,
}
TERMINAL_STATES: frozenset[str] = frozenset({"withdrawn", "cancelled"})

#: Technology pairs that describe one hybrid project, and the class the vocabulary has for it.
_HYBRIDS: dict[frozenset[str], str] = {
    frozenset({"solar", "storage"}): "solar_storage",
    frozenset({"solar_storage", "solar"}): "solar_storage",
    frozenset({"solar_storage", "storage"}): "solar_storage",
    frozenset({"solar_storage", "solar", "storage"}): "solar_storage",
    frozenset({"wind", "storage"}): "wind_storage",
    frozenset({"wind_storage", "wind"}): "wind_storage",
    frozenset({"wind_storage", "storage"}): "wind_storage",
    frozenset({"wind_storage", "wind", "storage"}): "wind_storage",
}

#: `Location.precision`, best first.
_PRECISION_RANK: dict[str, int] = {
    "exact": 0,
    "county_centroid": 1,
    "state_centroid": 2,
    "country_centroid": 3,
    "unknown": 4,
}

#: Sources whose `source_record_id` is the register's own key, read when a link carries no
#: `normalised.identifiers` yet (every link stored before 2026-10-07; the loader now writes them).
_EIA_INVENTORY_SOURCES: frozenset[str] = frozenset({"us.eia.860m"})
_QUEUE_ID_IS_RECORD_ID_PREFIX = "us.iso."


# ----------------------------------------------------------------------------------------- members
@dataclass(frozen=True)
class Member:
    """One source's observation of the record: an active `proposal_source` row, reduced to what
    the rules read."""

    source_id: str
    category: str
    record_id: str
    retrieved_at: dt.datetime
    licence_id: str
    values: Mapping[str, Any]
    identifiers: Mapping[str, Any] = field(default_factory=dict)
    gone: bool = False
    first_seen: dt.datetime | None = None
    link_id: _uuid.UUID | None = None

    @property
    def role(self) -> str:
        if self.category in REQUEST_CATEGORIES:
            return "request"
        if self.category in INVENTORY_CATEGORIES:
            return "inventory"
        return "other"

    @property
    def state(self) -> str | None:
        value = self.values.get("lifecycle_state")
        return str(value) if value else None

    @property
    def live(self) -> bool:
        return not self.gone and self.state not in TERMINAL_STATES

    def number(self, name: str) -> float | None:
        value = self.values.get(name)
        if value is None or isinstance(value, bool):
            return None
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
        return out if out == out and out > 0 else None

    @property
    def mw(self) -> float | None:
        return self.number("capacity_mw")

    @property
    def plant_id(self) -> str | None:
        value = self.identifiers.get("eia_plant_id")
        return str(value) if value else None


def link_identifiers(source_id: str, source_record_id: str, normalised: Mapping[str, Any]) -> dict[str, Any]:
    """The identifiers one link contributes. The loader records them on the link's `normalised`
    row (since 2026-10-07); a link stored before that is read from its record id where that id is
    the register's own key (EIA-860M `plant-generator`, a US ISO queue id), else contributes none."""
    stored = normalised.get("identifiers")
    if isinstance(stored, Mapping):
        return {k: v for k, v in stored.items() if k in MANAGED_IDENTIFIER_KEYS}
    base = source_record_id.split("#", 1)[0]
    if source_id in _EIA_INVENTORY_SOURCES and "-" in base:
        plant, generator = base.split("-", 1)
        if plant.isdigit() and generator:
            return {"eia_plant_id": plant, "eia_generator_id": generator}
        return {}
    if source_id.startswith(_QUEUE_ID_IS_RECORD_ID_PREFIX) and base:
        return {"queue_ids": [{"iso": str(normalised.get("iso") or ""), "id": base}]}
    return {}


def member_from_link(link: ProposalSource) -> Member:
    normalised = dict(link.normalised or {})
    return Member(
        source_id=link.source_id,
        category=str(getattr(link.source, "category", "") or ""),
        record_id=link.source_record_id,
        retrieved_at=_aware(link.retrieved_at),
        licence_id=link.licence_id,
        values=normalised,
        identifiers=link_identifiers(link.source_id, link.source_record_id, normalised),
        gone=link.gone_at is not None,
        first_seen=_aware(link.first_seen) if link.first_seen is not None else None,
        link_id=link.id,
    )


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


# ------------------------------------------------------------------------------------------- picks
@dataclass(frozen=True)
class Pick:
    """The surviving value of one field, the members that supplied it, and the rule's name."""

    value: Any
    sources: tuple[Member, ...]
    rule: str

    @property
    def source_ids(self) -> list[str]:
        return sorted({m.source_id for m in self.sources})

    @property
    def primary(self) -> Member:
        return self.sources[0]


def _recency(m: Member) -> tuple[float, str, str]:
    """Sort key: most recently retrieved first, then source id, then record id (total order)."""
    return (-m.retrieved_at.timestamp(), m.source_id, m.record_id)


def _by_mw(m: Member) -> tuple[float, float, str, str]:
    return (-(m.mw or 0.0), *_recency(m))


def _dedupe_requests(members: Iterable[Member]) -> list[Member]:
    """One source's requests with the same technology, MW and phase number count once: NYISO lists
    one project under its cluster id and its queue position ("KCE NY 30" as C24-008 and 1448), the
    reading `merge.coherence_ratio` gives too. Phases of equal size are kept apart by their numbers
    (`pipeline.resolve.phase_key`): ERCOT's "Roseland Solar" and "Roseland Solar II" are 254 MW
    each and both count."""
    seen: set[tuple[str, Any, float, frozenset[int | str]]] = set()
    out: list[Member] = []
    for m in sorted(members, key=_recency):
        phase = phase_key(m.values.get("name_canonical"))
        key = (m.source_id, m.values.get("technology"), round(m.mw or 0.0, 1), phase)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def _sum_pick(members: Sequence[Member], name: str, rule: str) -> Pick:
    ordered = sorted(members, key=lambda m: (-(m.number(name) or 0.0), *_recency(m)))
    total = round(sum(m.number(name) or 0.0 for m in ordered), 3)
    return Pick(total, tuple(ordered), rule if len(ordered) == 1 else f"{rule}_sum")


def _families(m: Member) -> frozenset[str] | None:
    return tech_families(m.values.get("technology"))


def _uncovered_inventory(live: Sequence[Member], inventory: Sequence[Member]) -> list[Member]:
    """Inventory generators of a technology family no live request covers: ERCOT files a hybrid's
    halves as two requests and the queue may hold only one of them (Albatross: a 50.4 MW storage
    request beside EIA's 101 MW solar and 50.4 MW storage generators), so the solar half is counted
    from the inventory. A hybrid request (`solar_storage`) covers both families; a request of
    unknown technology covers everything, as does an inventory row of unknown technology."""
    covered: set[str] = set()
    for m in live:
        families = _families(m)
        if families is None:
            return []
        covered |= families
    return [m for m in inventory if (f := _families(m)) is not None and not (f & covered)]


def _quantity(members: Sequence[Member], name: str) -> Pick | None:
    """`capacity_mw` / `storage_mwh` (module docstring)."""
    stating = [m for m in members if m.number(name) is not None]
    requests = [m for m in stating if m.role == "request"]
    inventory = [m for m in stating if m.role == "inventory"]
    live = _dedupe_requests(m for m in requests if m.live)
    if live:
        uncovered = _uncovered_inventory(live, inventory)
        if uncovered:
            return _sum_pick([*live, *uncovered], name, "interconnection_request_plus_inventory")
        return _sum_pick(live, name, "interconnection_request")
    if inventory:
        return _sum_pick(inventory, name, "plant_inventory")
    if requests:
        return _sum_pick(_dedupe_requests(requests), name, "interconnection_request_inactive")
    if stating:
        best = min(stating, key=lambda m: (-(m.number(name) or 0.0), *_recency(m)))
        return Pick(best.number(name), (best,), "largest_stated")
    return None


def _technology(supplier_set: Sequence[Member]) -> tuple[Pick, Pick | None, Pick | None] | None:
    """(`technology`, `technology_raw`, `kind`) from the members that supplied capacity."""
    stating = [m for m in supplier_set if m.values.get("technology")]
    if not stating:
        return None
    ordered = sorted(stating, key=_by_mw)
    techs = {str(m.values["technology"]) for m in ordered}
    largest = ordered[0]
    if len(techs) == 1:
        tech, rule, suppliers = next(iter(techs)), "capacity_supplier", ordered
    elif frozenset(techs) in _HYBRIDS:
        tech, rule, suppliers = _HYBRIDS[frozenset(techs)], "hybrid_of_capacity_suppliers", ordered
    else:
        tech, rule, suppliers = str(largest.values["technology"]), "largest_capacity_supplier", [largest]
    tech_pick = Pick(tech, tuple(suppliers), rule)

    raw_pick: Pick | None = None
    raws = [m for m in suppliers if m.values.get("technology_raw")]
    if raws:
        if rule == "hybrid_of_capacity_suppliers" and len({m.source_id for m in raws}) == 1:
            texts: list[str] = []
            for m in raws:
                text = str(m.values["technology_raw"])
                if text not in texts:
                    texts.append(text)
            raw_pick = Pick(" + ".join(texts), tuple(raws), rule)
        else:
            raw_pick = Pick(str(raws[0].values["technology_raw"]), (raws[0],), rule)

    kinds = [m for m in ordered if m.values.get("kind")]
    kind_pick = (
        Pick(str(kinds[0].values["kind"]), (kinds[0],), "largest_capacity_supplier") if kinds else None
    )
    return tech_pick, raw_pick, kind_pick


def _lifecycle_order(members: Sequence[Member]) -> list[Member]:
    """Members in lifecycle precedence: present before gone; a known non-terminal state before a
    terminal one before unknown; higher on the ladder first; then recency, then MW."""
    known = [m for m in members if m.state and m.state != "unknown"]
    present = [m for m in known if not m.gone]
    pool = present or known

    def key(m: Member) -> tuple[int, int, float, float, str, str]:
        state = m.state or "unknown"
        group = 0 if state in LIFECYCLE_RANK else 1
        newest, source_id, record_id = _recency(m)
        return (group, -LIFECYCLE_RANK.get(state, 0), newest, -(m.mw or 0.0), source_id, record_id)

    return sorted(pool, key=key)


def _lifecycle(members: Sequence[Member]) -> tuple[Pick, Pick, Pick | None] | None:
    ordered = _lifecycle_order(members)
    if not ordered:
        return None
    winner = ordered[0]
    states = {m.state for m in ordered}
    if winner.state in TERMINAL_STATES:
        rule = "every_member_terminal"
    elif len(states) > 1:
        rule = "most_advanced_of_conflicting"
    else:
        rule = "members_agree"
    state_pick = Pick(winner.state, (winner,), rule)
    raw_pick = Pick(winner.values.get("status_raw"), (winner,), rule)
    dated = [m for m in ordered if m.values.get("proposed_online_date")]
    date_pick = None
    if dated:
        first = dated[0]
        date_rule = "with_lifecycle" if first is winner else "next_most_advanced"
        date_pick = Pick(first.values["proposed_online_date"], (first,), date_rule)
    return state_pick, raw_pick, date_pick


def _plants(members: Sequence[Member]) -> dict[str, list[Member]]:
    plants: dict[str, list[Member]] = {}
    for m in members:
        if m.role == "inventory" and m.plant_id:
            plants.setdefault(m.plant_id, []).append(m)
    return plants


def _primary_plant(members: Sequence[Member], current: Mapping[str, Any]) -> str | None:
    """The plant with the most MW; on a tie the plant the stored name or id already names."""
    plants = _plants(members)
    if not plants:
        return None
    totals = {pid: sum(m.mw or 0.0 for m in ms) for pid, ms in plants.items()}
    top = max(totals.values())
    tied = sorted((pid for pid, total in totals.items() if total == top), key=_plant_sort)
    stored_name = current.get("name_canonical")
    stored_ids = current.get("identifiers") or {}
    for pid in tied:
        if any(m.values.get("name_canonical") == stored_name for m in plants[pid]):
            return pid
    if stored_ids.get("eia_plant_id") in tied:
        return str(stored_ids["eia_plant_id"])
    return tied[0]


def _plant_sort(pid: str) -> tuple[int, str]:
    return (int(pid), pid) if pid.isdigit() else (1 << 62, pid)


def _name(members: Sequence[Member], current: Mapping[str, Any], plant: str | None) -> Pick | None:
    stored = current.get("name_canonical")
    if plant is not None:
        ms = sorted(_plants(members)[plant], key=_by_mw)
        named = [m for m in ms if m.values.get("name_canonical")]
        if named:
            keep = next((m for m in named if m.values["name_canonical"] == stored), named[0])
            return Pick(keep.values["name_canonical"], (keep,), "plant_inventory")
    requests = sorted(
        (m for m in members if m.role == "request" and m.values.get("name_canonical")),
        key=lambda m: (0 if m.live else 1, *_by_mw(m)),
    )
    if requests:
        top = requests[0]
        tied = [m for m in requests if m.live == top.live and (m.mw or 0.0) == (top.mw or 0.0)]
        keep = next((m for m in tied if m.values["name_canonical"] == stored), top)
        return Pick(keep.values["name_canonical"], (keep,), "largest_request")
    named = sorted((m for m in members if m.values.get("name_canonical")), key=_recency)
    if not named:
        return None
    keep = next((m for m in named if m.values["name_canonical"] == stored), named[0])
    return Pick(keep.values["name_canonical"], (keep,), "stated_by_member")


def _most_common(
    members: Sequence[Member], name: str, current: Any, rule: str, prefer: Any = None
) -> Pick | None:
    stating = [m for m in members if m.values.get(name)]
    if prefer is not None:
        stating = [m for m in stating if prefer(m)] or stating
    if not stating:
        return None
    counts = Counter(str(m.values[name]) for m in stating)
    top = max(counts.values())
    tied = sorted(v for v, n in counts.items() if n == top)
    value = str(current) if current is not None and str(current) in tied else tied[0]
    suppliers = tuple(sorted((m for m in stating if str(m.values[name]) == value), key=_recency))
    return Pick(value, suppliers, rule)


def _identifiers(members: Sequence[Member], plant: str | None) -> Pick | None:
    managed: dict[str, Any] = {}
    suppliers: list[Member] = []
    queue: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    requests = sorted(
        (m for m in members if m.identifiers.get("queue_ids")), key=lambda m: (0 if m.live else 1, *_by_mw(m))
    )
    for m in requests:
        for entry in m.identifiers.get("queue_ids") or []:
            if not isinstance(entry, Mapping) or not entry.get("id"):
                continue
            key = (str(entry.get("iso") or ""), str(entry["id"]))
            if key not in seen:
                seen.add(key)
                queue.append({"iso": key[0], "id": key[1]})
                suppliers.append(m)
    if queue:
        managed["queue_ids"] = queue
    generators = sorted(
        {
            (str(m.identifiers["eia_plant_id"]), str(m.identifiers.get("eia_generator_id") or ""))
            for m in members
            if m.identifiers.get("eia_plant_id")
        },
        key=lambda pg: (_plant_sort(pg[0]), pg[1]),
    )
    if generators:
        primary = plant or generators[0][0]
        managed["eia_plant_id"] = primary
        if len(generators) == 1 and generators[0][1]:
            managed["eia_generator_id"] = generators[0][1]
        elif len(generators) > 1:
            managed["eia_generators"] = [{"plant_id": p, "generator_id": g} for p, g in generators if g]
        suppliers.extend(m for m in members if m.identifiers.get("eia_plant_id"))
    if not managed:
        return None
    unique: list[Member] = []
    for m in suppliers:
        if m not in unique:
            unique.append(m)
    return Pick(managed, tuple(unique), "union_of_members")


def survive(members: Sequence[Member], current: Mapping[str, Any] | None = None) -> dict[str, Pick]:
    """The surviving value of each field `members` state (module docstring). `current` is the
    stored row's values, read only to keep a stored name, plant or jurisdiction on a tie."""
    current = current or {}
    picks: dict[str, Pick] = {}
    if not members:
        return picks

    capacity = _quantity(members, "capacity_mw")
    if capacity is not None:
        picks["capacity_mw"] = capacity
    storage = _quantity(members, "storage_mwh")
    if storage is not None:
        picks["storage_mwh"] = storage

    supplier_set = list(capacity.sources) if capacity is not None else list(members)
    tech = _technology(supplier_set) or _technology(members)
    if tech is not None:
        picks["technology"], raw, kind = tech
        if raw is not None:
            picks["technology_raw"] = raw
        if kind is not None:
            picks["kind"] = kind

    lifecycle = _lifecycle(members)
    if lifecycle is not None:
        picks["lifecycle_state"], picks["status_raw"], date = lifecycle
        if date is not None:
            picks["proposed_online_date"] = date
    else:
        dated = sorted((m for m in members if m.values.get("proposed_online_date")), key=_recency)
        if dated:
            picks["proposed_online_date"] = Pick(
                dated[0].values["proposed_online_date"], (dated[0],), "most_recent"
            )

    plant = _primary_plant(members, current)
    name = _name(members, current, plant)
    if name is not None:
        picks["name_canonical"] = name
    jurisdiction = _most_common(
        members,
        "jurisdiction",
        current.get("jurisdiction"),
        "most_common_subdivision",
        prefer=lambda m: "-" in str(m.values.get("jurisdiction") or ""),
    )
    if jurisdiction is not None:
        picks["jurisdiction"] = jurisdiction
    iso = _most_common(
        [m for m in members if m.role == "request"] or list(members), "iso", current.get("iso"), "operator"
    )
    if iso is not None:
        picks["iso"] = iso
    identifiers = _identifiers(members, plant)
    if identifiers is not None:
        picks["identifiers"] = identifiers
    return picks


# ---------------------------------------------------------------------------------- store writing
def coerce(name: str, value: Any) -> Any:
    """A pick's value in the column's Python type (`normalised` stores dates as ISO text)."""
    if value is None:
        return None
    if name == "proposed_online_date" and isinstance(value, str):
        return dt.date.fromisoformat(value[:10])
    if name in ("capacity_mw", "storage_mwh"):
        return float(value)
    return value


def same_value(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float, decimal.Decimal)) and isinstance(b, (int, float, decimal.Decimal)):
        return abs(float(a) - float(b)) < 1e-6
    if isinstance(a, dt.date) and isinstance(b, str):
        return a.isoformat() == b[:10]
    if isinstance(b, dt.date) and isinstance(a, str):
        return b.isoformat() == a[:10]
    return bool(a == b)


def provenance_source_ids(entry: Any) -> list[str]:
    """Every source a `field_provenance` entry names (`source_ids`, else `source_id`)."""
    if not isinstance(entry, Mapping):
        return []
    many = entry.get("source_ids")
    if isinstance(many, list) and many:
        return [str(s) for s in many]
    one = entry.get("source_id")
    return [str(one)] if one else []


def provenance_entry(pick: Pick) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "source_id": pick.primary.source_id,
        "licence_id": pick.primary.licence_id,
        "retrieved_at": max(m.retrieved_at for m in pick.sources).isoformat(),
        "rule": pick.rule,
    }
    if len(pick.source_ids) > 1:
        entry["source_ids"] = pick.source_ids
    return entry


def _identifiers_value(stored: Mapping[str, Any] | None, managed: Mapping[str, Any]) -> dict[str, Any]:
    kept = {k: v for k, v in (stored or {}).items() if k not in MANAGED_IDENTIFIER_KEYS}
    return {**kept, **managed}


@dataclass
class Change:
    field: str
    before: Any
    after: Any


@dataclass
class SurvivorshipResult:
    proposal_id: _uuid.UUID
    changes: list[Change] = field(default_factory=list)
    provenance_restamped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, _uuid.UUID):
        return str(value)
    return value


def apply_picks(proposal: Proposal, picks: Mapping[str, Pick]) -> SurvivorshipResult:
    """Write `picks` onto `proposal`, skipping overridden fields. A field whose value and
    supplying sources are unchanged keeps its provenance entry exactly (so a no-op is a no-op)."""
    result = SurvivorshipResult(proposal.id)
    pinned = set(proposal.overrides or {})
    provenance = dict(proposal.field_provenance or {})
    for name in SURVIVING_FIELDS:
        pick = picks.get(name)
        if pick is None or name in pinned:
            continue
        stored = getattr(proposal, name)
        new = _identifiers_value(stored, pick.value) if name == "identifiers" else coerce(name, pick.value)
        if name in ("name_canonical", "jurisdiction", "kind", "lifecycle_state") and new is None:
            continue  # NOT NULL columns
        old_entry = provenance.get(name)
        same = same_value(stored, new)
        if same and sorted(provenance_source_ids(old_entry)) == pick.source_ids:
            if isinstance(old_entry, Mapping) and old_entry.get("rule") == pick.rule:
                continue
        if not same:
            setattr(proposal, name, new)
            result.changes.append(Change(name, _json_safe(stored), _json_safe(new)))
        provenance[name] = provenance_entry(pick)
        result.provenance_restamped.append(name)
    if result.provenance_restamped:
        proposal.field_provenance = provenance  # reassigned so the JSON column is marked dirty
    return result


def proposal_members(session: Session, proposal_id: _uuid.UUID) -> list[Member]:
    links = session.scalars(
        select(ProposalSource).where(
            ProposalSource.proposal_id == proposal_id, ProposalSource.active.is_(True)
        )
    ).all()
    return [member_from_link(link) for link in links]


def current_values(proposal: Proposal) -> dict[str, Any]:
    return {name: getattr(proposal, name) for name in SURVIVING_FIELDS}


@dataclass(frozen=True)
class Absorbed:
    """What `_apply_row_fields` reads of a row merged into a record."""

    id: _uuid.UUID
    first_seen: dt.datetime | None
    location_id: _uuid.UUID | None


AbsorbedIndex = dict[_uuid.UUID, list[Absorbed]]

_ABSORBED_COLUMNS = (Proposal.id, Proposal.merged_into_id, Proposal.first_seen, Proposal.location_id)


def absorbed_index(session: Session) -> AbsorbedIndex:
    """`{survivor id: rows merged directly into it}` for the whole store, in one query: a store pass
    reads it instead of one unindexed `merged_into_id` lookup per record (about 11 s of a 12 s pass
    over 403 records on the measured SQLite copy)."""
    out: AbsorbedIndex = {}
    rows = session.execute(select(*_ABSORBED_COLUMNS).where(Proposal.merged_into_id.is_not(None))).all()
    for row_id, parent, first_seen, location_id in rows:
        out.setdefault(parent, []).append(Absorbed(row_id, first_seen, location_id))
    return out


def _absorbed_rows(
    session: Session, proposal_id: _uuid.UUID, index: AbsorbedIndex | None = None
) -> list[Absorbed]:
    """Every row merged into `proposal_id`, directly or through a chain."""
    out: list[Absorbed] = []
    frontier = [proposal_id]
    seen = {proposal_id}
    while frontier:
        if index is not None:
            rows = [row for parent in frontier for row in index.get(parent, [])]
        else:
            rows = [
                Absorbed(row_id, first_seen, location_id)
                for row_id, _parent, first_seen, location_id in session.execute(
                    select(*_ABSORBED_COLUMNS).where(Proposal.merged_into_id.in_(frontier))
                ).all()
            ]
        rows = [r for r in rows if r.id not in seen]
        frontier = [r.id for r in rows]
        seen.update(frontier)
        out.extend(rows)
    return out


def _apply_row_fields(
    session: Session, proposal: Proposal, result: SurvivorshipResult, index: AbsorbedIndex | None = None
) -> None:
    """`first_seen` (the earliest of the record and the rows merged into it: the store's own clock,
    not a link's retrieval time) and `location_id` (most precise of the record and its absorbed
    rows; the stored one keeps a tie). Overrides of `location` pin it."""
    absorbed = _absorbed_rows(session, proposal.id, index)
    seen = [_aware(r.first_seen) for r in absorbed if r.first_seen is not None]
    if seen and proposal.first_seen is not None and min(seen) < _aware(proposal.first_seen):
        earliest = min(seen)
        result.changes.append(Change("first_seen", _json_safe(proposal.first_seen), earliest.isoformat()))
        proposal.first_seen = earliest
    if "location" in (proposal.overrides or {}):
        return
    candidates = [proposal.location_id, *(r.location_id for r in absorbed)]
    ids = [c for c in candidates if c is not None]
    if not ids:
        return
    precision = dict(
        session.execute(select(Location.id, Location.precision).where(Location.id.in_(set(ids))))
        .tuples()
        .all()
    )

    def rank(loc_id: _uuid.UUID) -> int:
        return _PRECISION_RANK.get(str(precision.get(loc_id)), 9)

    best = min(ids, key=lambda i: (rank(i), 0 if i == proposal.location_id else 1))
    if best != proposal.location_id and (
        proposal.location_id is None or rank(best) < rank(proposal.location_id)
    ):
        result.changes.append(Change("location_id", _json_safe(proposal.location_id), str(best)))
        proposal.location_id = best


def apply_survivorship(
    session: Session,
    proposal: Proposal,
    *,
    touch: bool = True,
    now: dt.datetime | None = None,
    index: AbsorbedIndex | None = None,
) -> SurvivorshipResult:
    """Recompute `proposal`'s surviving fields from its active links and write them (module
    docstring). Moves `last_changed` forward when a served value changed and `touch` is set.
    `index` (`absorbed_index`) saves a store pass one query per record."""
    members = proposal_members(session, proposal.id)
    picks = survive(members, current_values(proposal))
    result = apply_picks(proposal, picks)
    _apply_row_fields(session, proposal, result, index)
    if result.changed and touch:
        proposal.last_changed = now or dt.datetime.now(dt.UTC)
    return result


def snapshot(proposal: Proposal) -> dict[str, Any]:
    """The survivorship fields and their provenance entries, JSON-safe: what a merge event keeps
    so its unmerge can put the survivor back exactly (docs/21 §6.3 invariant M1)."""
    provenance = proposal.field_provenance or {}
    values = {name: _json_safe(getattr(proposal, name)) for name in (*SURVIVING_FIELDS, *ROW_FIELDS)}
    # Only the keys survivorship owns: `select_basis` has its own carry and drop (merge.py).
    values["identifiers"] = {
        k: v for k, v in (proposal.identifiers or {}).items() if k in MANAGED_IDENTIFIER_KEYS
    }
    return {
        "values": values,
        "provenance": {name: provenance[name] for name in SURVIVING_FIELDS if name in provenance},
    }


def restore_snapshot(proposal: Proposal, data: Mapping[str, Any]) -> None:
    """Inverse of `snapshot`, skipping fields an admin override pins now."""
    pinned = set(proposal.overrides or {})
    values = data.get("values") or {}
    provenance = dict(proposal.field_provenance or {})
    for name, value in values.items():
        if name in pinned or (name == "location_id" and "location" in pinned):
            continue
        if name == "proposed_online_date" and value is not None:
            value = dt.date.fromisoformat(value)
        elif name == "first_seen" and value is not None:
            value = dt.datetime.fromisoformat(value)
        elif name == "location_id" and value is not None:
            value = _uuid.UUID(value)
        elif name == "identifiers":
            value = _identifiers_value(proposal.identifiers, value or {})
        setattr(proposal, name, value)
        if name in SURVIVING_FIELDS:
            entry = (data.get("provenance") or {}).get(name)
            if entry is None:
                provenance.pop(name, None)
            else:
                provenance[name] = entry
    proposal.field_provenance = provenance


# ---------------------------------------------------------------------------------- whole-store run
@dataclass
class RestatementReport:
    """What one `restate_all` (or a load's survivorship step) did. No events are written."""

    records_examined: int = 0
    records_changed: int = 0
    fields_changed: Counter[str] = field(default_factory=Counter)
    provenance_restamped: int = 0
    changes: list[SurvivorshipResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "records_examined": self.records_examined,
            "records_changed": self.records_changed,
            "fields_changed": dict(sorted(self.fields_changed.items())),
            "provenance_restamped": self.provenance_restamped,
        }


def multi_source_ids(session: Session, ids: Iterable[_uuid.UUID] | None = None) -> list[_uuid.UUID]:
    """Live proposals with more than one active link (all of them, or those among `ids`)."""
    base = (
        select(ProposalSource.proposal_id)
        .join(Proposal, Proposal.id == ProposalSource.proposal_id)
        .where(ProposalSource.active.is_(True), Proposal.merged_into_id.is_(None))
        .group_by(ProposalSource.proposal_id)
        .having(func.count() > 1)
    )
    if ids is None:
        return sorted(session.scalars(base).all(), key=str)
    wanted = sorted(set(ids), key=str)
    out: list[_uuid.UUID] = []
    for start in range(0, len(wanted), 500):
        out.extend(
            session.scalars(base.where(ProposalSource.proposal_id.in_(wanted[start : start + 500]))).all()
        )
    return sorted(out, key=str)


def restate(
    session: Session, ids: Iterable[_uuid.UUID] | None = None, *, now: dt.datetime | None = None
) -> RestatementReport:
    """Apply survivorship to every live multi-source proposal (or those among `ids`), writing no
    event (module docstring, "Restatement, not news")."""
    report = RestatementReport()
    now = now or dt.datetime.now(dt.UTC)
    wanted = multi_source_ids(session, ids)
    index = absorbed_index(session) if len(wanted) > 20 else None
    for proposal_id in wanted:
        proposal = session.get(Proposal, proposal_id)
        if proposal is None:
            continue
        report.records_examined += 1
        result = apply_survivorship(session, proposal, now=now, index=index)
        if result.provenance_restamped:
            report.provenance_restamped += 1
        if result.changed:
            report.records_changed += 1
            report.fields_changed.update(c.field for c in result.changes)
            report.changes.append(result)
    session.flush()
    return report


def restate_all(session: Session, *, now: dt.datetime | None = None) -> RestatementReport:
    return restate(session, None, now=now)
