"""Grid interconnection points: parse a queue row's point of interconnection (POI) and link each
proposal to one `interconnection_point` row (owner decision 2026-09-28; docs/21 §3.24; docs/25 §1).

A project *connects at* a substation bus or a tap on a line; it is not built there, so the POI is
not the proposal's `location` (docs/21 §3.7). Every US ISO queue row carries it only inside the
raw source record (`Interconnection Location`, the gridstatus column name for every ISO), and the
NESO TEC register carries `Connection Site`. This module turns that text into a first-class record
at load time, in two halves:

* `parse_point(text, field=...)` -- a pure function from the source's string to a `ParsedPoint`:
  the display spelling (the source's own, whitespace collapsed), a grouping key, the voltage and
  bus number when the text states them, and a kind (`substation | line_tap | unknown`).
* `link_source_points(session, source)` -- for one loaded source, every active `proposal_source`
  row's raw POI is parsed, grouped on `(source_id, name_key)` and written to
  `interconnection_point`; each proposal gets `interconnection_point_id`.

**The key rule** (measured in docs/25 §1; `KEY_RULE_VERSION` names it):

1. Clean: `\\x96`/en/em dashes become `-`, every whitespace run (newlines included) one space,
   trailing `.,;` dropped, case folded.
2. Voltage: the first `N kV` expression (`345kV`, `345 kV`, `13.2kV`, `275/132kV` -> 275, the
   highest of a slash group) is `voltage_kv` and is removed from the name. It is **part of the
   key**: a point is a bus, and `Gates 230 kV` and `Gates 500 kV` are two buses at one substation.
   No voltage stated is its own value, so `Bellota` never merges with `Bellota 115 kV`.
3. Bus numbers (ERCOT's PSS/E numbering): a leading number of 3-6 digits, `#nnnn`, `bus nnnn`,
   `PSSE nnnn`, `(nnnn)`, `<nnnn>` are removed from the name; a substation's first one is kept as
   `bus_number` and is **part of its key** when stated. Measured on the dev store (docs/25 §1):
   without it, 4 ERCOT names grouped two different buses ("8795 Roma 138kV" / "8796 Roma 138kV",
   Cenizo 80220 / 80223), which is a false merge; with it they stay apart, at the cost of 2
   groups (one ERCOT, one NYISO) where one spelling states the bus and another does not.
4. Kind. `unknown` when the text names several points or describes one in prose (`;`, ` and `,
   ` or `, `&`, `,`, a slash between words, `approx`, `located`, `miles`, `between`, `system`, more
   than 100 characters). `line_tap` when it says tap/line/circuit or joins two names with `-` or
   ` to `. Otherwise `substation`. A NESO `Connection Site` is always a `substation` (the register
   names one site per row; its names carry ` and ` legitimately, "Sussex and Romney Connection
   Node A").
5. Name normalisation: parentheses and angle brackets dropped, `&` read as `and`, every other
   non-alphanumeric run a space, and the generic words `substation, sub, station, stn, switching,
   switch, switchyard, yard, sw, ss, swyd, bus, gsp, existing, proposed, poi` dropped, so
   "Paris Switch Station" and "Paris" group. `new` is kept: "New Cumnock" is a place, not a
   new "Cumnock".
6. Line/tap key: the endpoints after splitting on `-` and ` to `, each normalised as in 5 with
   `tap`/`line`/`circuit` words removed and its own bus number appended when stated, **sorted**
   (A-B is B-A), plus the voltage and the circuit designator when one is stated (`#1`, `ckt 2`,
   `No.1`, `line 4`) -- two circuits of one corridor stay apart, and so do taps between different
   buses of one station (measured: "Tap 345kV 1906 Venus - 68091 Navarro" and "... 1907 Venus
   ..." grouped before the endpoint bus was in the key). A *tap* naming one place ("Tap #1429
   Jacksboro Substation 345kV") is that substation; a *line* or *circuit* naming one place
   ("Warners 69kV line") stays a single-ended `line_tap`, since it is a line out of the place and
   not its bus.
7. `unknown`: the cleaned, case-folded string itself -- identical text groups, nothing else does.

**Never across ISOs, and never across sources**: the unique key is `(source_id, name_key)`. One
register is one operator's naming space; the point's provenance quartet is that register's, so its
licence and publication gate are exactly the proposals' own (docs/21 §8). Two registers naming the
same substation (none today: the only second US register, ERCOT's large-load queue, has no rows)
become two points; lane G2's substation crosswalk (`substation_asset_id`) is the layer that
unifies them.

What is stored is derived-class data (docs/21 §8): a substation name and voltage the source states,
normalised. `location.raw_place` stays gated as before; the POI is not a placement.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import re
import uuid as _uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import InterconnectionPoint, Proposal, ProposalSource, Source, new_uuid
from services.ids import public_id

log = logging.getLogger(__name__)

PointKind = Literal["substation", "line_tap", "unknown"]

#: Bumped whenever the key rule above changes, so a stored key can be told from a re-derived one.
KEY_RULE_VERSION = "2026-09-28.1"

#: Raw-record fields that carry a point of interconnection, in order of preference. The first is
#: gridstatus's column for every ISO queue (ERCOT, CAISO, NYISO today; SPP, ISO-NE, MISO, PJM the
#: day their licences clear), the second the NESO TEC register's.
POI_FIELDS: tuple[str, ...] = ("Interconnection Location", "Connection Site")
#: Fields whose value always names one site (rule 4).
SINGLE_SITE_FIELDS: frozenset[str] = frozenset({"Connection Site"})

_PLACEHOLDERS = frozenset({"", "-", "--", "n/a", "na", "none", "tbd", "tba", "unknown", "nan", "null"})
_DASHES_RE = re.compile("[‒–—―\x96\x97]")
_WS_RE = re.compile(r"\s+")
#: `345kV`, `345 kV`, `13.2 kv`, `275/132kV`, `345/138 kV`, `230-kV`.
_VOLTAGE_RE = re.compile(
    r"(?<![\w.])(\d{1,4}(?:\.\d+)?(?:\s*/\s*\d{1,4}(?:\.\d+)?)*)\s*-?\s*k\s*v\b", re.IGNORECASE
)
#: In order of reliability: a leading number first (ERCOT's "59903 Bearkat 345kV"), so a bare
#: parenthesised voltage later in the string ("68091 (Navarro) (345)") is never taken for the bus.
_BUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*[#(<]?\s*(\d{3,6})\b\s*[)>]?"),
    re.compile(r"\bpsse\s*(?:bus)?\s*#?\s*(\d{2,6})\b", re.IGNORECASE),
    re.compile(r"\bbus\s*(?:no\.?|number)?\s*#?\s*:?\s*(\d{2,6})\b", re.IGNORECASE),
    re.compile(r"\(\s*#?\s*(\d{2,6})\s*\)"),
    re.compile(r"<\s*(\d{3,6})\s*>"),
    re.compile(r"#\s*(\d{3,6})\b"),
)
#: A trailing utility tag ("..., ONCOR", "...; AEP") names the transmission owner, not a second
#: point; a comma straight before a voltage ("Colton - Battle Hill, 115 kV") is punctuation.
_OWNER_TAG_RE = re.compile(r"\s*[,;]\s*[A-Z][A-Z&]{1,7}\s*$")
_COMMA_BEFORE_KV_RE = re.compile(r",\s*(?=\d{1,4}(?:\.\d+)?\s*-?\s*k\s*v)", re.IGNORECASE)
_MULTI_RE = re.compile(
    r";|\band/or\b|\s&\s|\band\b|\bor\b|,|[a-z]\s*/\s*[a-z]|\bapprox|\blocated\b|\bmiles?\b|\bmi\b"
    r"|\bbetween\b|\bsystem\b|\bvicinity\b|\badjacent\b",
    re.IGNORECASE,
)
_LINE_WORD_RE = re.compile(r"\b(?:tap|tapped|taps|line|lines|ckt|circuit|cct)\b", re.IGNORECASE)
_LINE_ONLY_RE = re.compile(r"\b(?:line|lines|ckt|circuit|cct)\b", re.IGNORECASE)
_ENDPOINT_SPLIT_RE = re.compile(r"\s*-+\s*|\s+to\s+", re.IGNORECASE)
_CIRCUIT_RE = re.compile(
    r"\b(?:ckt|circuit|cct|line)\s*#?\s*(\d{1,3}[a-z]?)\b|(?<!\()#\s*(\d{1,2})\b(?!\s*\))|\bno\.?\s*(\d{1,2})\b",
    re.IGNORECASE,
)
_BRACKETS_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_GENERIC_WORDS = frozenset(
    {
        "substation",
        "substations",
        "sub",
        "subs",
        "subsation",
        "substatio",
        "substaion",
        "station",
        "stn",
        "switching",
        "switch",
        "switchyard",
        "yard",
        "sw",
        "ss",
        "swyd",
        "bus",
        "gsp",
        "existing",
        "proposed",
        "poi",
        "kv",
    }
)
_LINE_WORDS = frozenset({"tap", "tapped", "taps", "line", "lines", "ckt", "circuit", "cct", "the"})
_MAX_KNOWN_LENGTH = 100


@dataclass(frozen=True)
class ParsedPoint:
    """One POI string, parsed. `name_display` is the source's spelling (whitespace collapsed,
    cp1252 dashes repaired); `name_key` is what groups spellings (module docstring)."""

    name_display: str
    name_key: str
    voltage_kv: float | None
    bus_number: str | None
    kind: PointKind


#: One coordinate as registers type them into a POI field: an optional `Long:`/`Lat:` label, a
#: signed decimal with at least four places (fewer is a voltage, a mileage or a parcel number --
#: "34.5kV", "1.78mi", "151.00-01"), an optional degree sign. A run of them, comma- or
#: semicolon-separated, is one coordinate expression.
_COORD = r"(?:(?:long(?:itude)?|lon|lng|lat(?:itude)?)\s*[:=]?\s*)?-?\d{1,3}\.\d{4,}\s*°?"
COORDINATE_RUN_RE = re.compile(rf"{_COORD}(?:\s*[,;]?\s*{_COORD})*", re.IGNORECASE)
COORDINATES_WITHHELD = "[coordinates withheld]"


def withhold_coordinates(text: str) -> str:
    """`text` with every coordinate expression replaced by `COORDINATES_WITHHELD` (docs/21 §8:
    under a derived-only licence an exact coordinate is a raw field, and a register that typed
    one into its point-of-interconnection text must not publish it through the point's name;
    L-4 of the 2026-09-30 legal audit, CAISO "Herdlyn - Tracy 70 kV - Long: ... Lat: ...")."""
    return _WS_RE.sub(" ", COORDINATE_RUN_RE.sub(COORDINATES_WITHHELD, text)).strip()


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _DASHES_RE.sub("-", text)).strip().rstrip(".,;:").strip()


def _display(text: str) -> str:
    """The source's spelling for display: only whitespace runs collapse and the cp1252 `\\x96`
    (an en dash mis-decoded as a C1 control) is repaired; every other character is kept."""
    return _WS_RE.sub(" ", text.replace("\x96", "–").replace("\x97", "—")).strip()


def _voltage(text: str) -> tuple[float | None, str]:
    """`(voltage_kv, text with every voltage expression removed)`. The first expression is the
    point's voltage; within a slash group (`275/132kV`) the highest is the bus voltage."""
    match = _VOLTAGE_RE.search(text)
    stripped = _VOLTAGE_RE.sub(" ", text)
    if match is None:
        return None, stripped
    values = [float(v) for v in re.split(r"\s*/\s*", match.group(1)) if v]
    kv = max(values)
    return (kv if 0 < kv <= 1200 else None), stripped


def _bus_numbers(text: str) -> tuple[list[str], str]:
    """Every bus-number mention (rule 3), in order found, and the text without them."""
    found: list[str] = []
    for pattern in _BUS_PATTERNS:
        for m in pattern.finditer(text):
            found.append(m.group(1))
        text = pattern.sub(" ", text)
    return found, text


def _normalise_name(text: str, *, extra_drop: frozenset[str] = frozenset()) -> str:
    text = _BRACKETS_RE.sub(" ", text.lower().replace("<", " ").replace(">", " "))
    text = text.replace("&", " and ")
    tokens = [
        t for t in _NON_ALNUM_RE.sub(" ", text).split() if t not in _GENERIC_WORDS and t not in extra_drop
    ]
    return " ".join(tokens)


def _format_kv(kv: float | None) -> str:
    if kv is None:
        return ""
    return f"{kv:g}"


def _circuit(text: str) -> str | None:
    m = _CIRCUIT_RE.search(text)
    if m is None:
        return None
    return next(g for g in m.groups() if g).lower()


def _endpoint(part: str) -> str | None:
    """One end of a line: its normalised name, plus `#<bus>` when the text states the bus (the
    same rule as a substation's key; "1906 Venus" and "1907 Venus" are two buses)."""
    buses, rest = _bus_numbers(_LINE_WORD_RE.sub(" ", part))
    name = _normalise_name(rest, extra_drop=_LINE_WORDS) or _normalise_name(
        rest.replace("(", " ").replace(")", " "), extra_drop=_LINE_WORDS
    )
    if not name:
        return None
    return name + (f"#{buses[0]}" if buses else "")


def _substation(display: str, rest: str, kv: float | None) -> ParsedPoint | None:
    buses, rest = _bus_numbers(rest)
    # A name only in brackets ("47150 (H_O_C__B138) 138kV") is still the name.
    name = _normalise_name(rest) or _normalise_name(rest.replace("(", " ").replace(")", " "))
    if not name:
        return None
    bus = buses[0] if buses else None
    return ParsedPoint(
        name_display=display,
        name_key=f"sub:{name}|{_format_kv(kv)}" + (f"|b{bus}" if bus else ""),
        voltage_kv=kv,
        bus_number=bus,
        kind="substation",
    )


def parse_point(text: Any, *, field: str = POI_FIELDS[0]) -> ParsedPoint | None:
    """Parse one POI string (module docstring, "The key rule"). `None` for an empty value, a
    placeholder (`TBD`, `N/A`) or a string that names no place once voltage and numbers are
    removed ("46kV line", "Line 123975") -- nothing to group, so no point."""
    if not isinstance(text, str):
        return None
    cleaned = _clean(text)
    if cleaned.lower() in _PLACEHOLDERS:
        return None
    display = _display(text)
    cleaned = _COMMA_BEFORE_KV_RE.sub(" ", _OWNER_TAG_RE.sub("", cleaned))
    lowered = cleaned.lower()
    kv, rest = _voltage(cleaned)

    if field in SINGLE_SITE_FIELDS:
        return _substation(display, rest, kv)

    if len(cleaned) > _MAX_KNOWN_LENGTH or _MULTI_RE.search(lowered):
        key = _NON_ALNUM_RE.sub(" ", lowered).strip()
        if not key:
            return None
        return ParsedPoint(
            name_display=display, name_key=f"raw:{key}", voltage_kv=kv, bus_number=None, kind="unknown"
        )

    is_line = bool(_LINE_WORD_RE.search(rest)) or bool(
        re.search(r"[a-z0-9]\s*-+\s*[a-z0-9]|\sto\s", rest, re.I)
    )
    if not is_line:
        return _substation(display, rest, kv)

    circuit = _circuit(rest)
    endpoints = sorted(
        {name for part in _ENDPOINT_SPLIT_RE.split(_CIRCUIT_RE.sub(" ", rest)) if (name := _endpoint(part))}
    )
    if not endpoints:
        return None
    if len(endpoints) == 1 and not _LINE_ONLY_RE.search(rest):
        # "Tap #1429 Jacksboro Substation 345kV": a tap that names one place is that place. A
        # "line" or "circuit" that names one place ("Warners 69kV line") is a line out of it, not
        # its bus, so it stays a single-ended `line_tap` rather than joining the substation.
        return _substation(display, _LINE_WORD_RE.sub(" ", rest), kv)
    key = "line:" + "~".join(endpoints) + f"|{_format_kv(kv)}" + (f"|c{circuit}" if circuit else "")
    return ParsedPoint(name_display=display, name_key=key, voltage_kv=kv, bus_number=None, kind="line_tap")


def poi_text(raw: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """`(field, value)` for the first POI field the raw record carries with a string value."""
    if not raw:
        return None
    for name in POI_FIELDS:
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return name, value
    return None


def parse_raw(raw: Mapping[str, Any] | None) -> ParsedPoint | None:
    found = poi_text(raw)
    if found is None:
        return None
    name, value = found
    return parse_point(value, field=name)


# ------------------------------------------------------------------------------------- linking
@dataclass
class LinkResult:
    source_id: str
    links_seen: int = 0
    with_poi: int = 0
    linked: int = 0
    points_created: int = 0
    points_touched: int = 0
    unparsed: int = 0
    by_kind: Counter[str] = field(default_factory=Counter)

    def summary(self) -> str:
        return (
            f"{self.source_id}: {self.linked}/{self.links_seen} proposals linked to "
            f"{self.points_touched} points ({self.points_created} new; kinds {dict(self.by_kind)}); "
            f"{self.with_poi - self.linked} POI strings named no point"
        )


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value


def _operator_token(source: Source, proposals: Iterable[Proposal]) -> str:
    """The market operator the point belongs to: the proposals' own `iso` token (the value
    `?iso=` filters on), else the source's operator name."""
    counts = Counter(p.iso for p in proposals if p.iso)
    if counts:
        return counts.most_common(1)[0][0]
    return source.operator or source.id


def link_source_points(
    session: Session, source: Source, links: Iterable[ProposalSource] | None = None
) -> LinkResult:
    """Parse the POI of every active `proposal_source` row of `source` and link its proposal to
    the `(source_id, name_key)` point, creating the point on first sight. Idempotent: a second run
    over the same rows changes nothing but `updated_at` on points whose facts moved.

    A proposal already linked to a point *from another source* keeps it (the first register to
    name its POI wins); one linked from this source follows the source's current text, so a POI
    the register revises moves the proposal. A point left with no proposals stays stored and is
    invisible (it needs a visible proposal to be shown, `services/api/visibility.py`).

    The point's display name is the spelling most of its rows use (ties: the lexically first);
    `jurisdiction` the most common jurisdiction of its proposals; `retrieved_at` the latest
    retrieval of any row that names it."""
    result = LinkResult(source_id=source.id)
    if links is None:
        links = session.scalars(
            select(ProposalSource).where(
                ProposalSource.source_id == source.id, ProposalSource.active.is_(True)
            )
        ).all()
    links = list(links)
    result.links_seen = len(links)
    if not links:
        return result

    proposal_ids = {link.proposal_id for link in links}
    proposals = {
        p.id: p for p in session.scalars(select(Proposal).where(Proposal.id.in_(proposal_ids))).all()
    }
    existing = {
        p.name_key: p
        for p in session.scalars(
            select(InterconnectionPoint).where(InterconnectionPoint.source_id == source.id)
        ).all()
    }
    point_sources: dict[_uuid.UUID, str] = dict(
        session.execute(
            select(InterconnectionPoint.id, InterconnectionPoint.source_id).where(
                InterconnectionPoint.id.in_(
                    {p.interconnection_point_id for p in proposals.values() if p.interconnection_point_id}
                )
            )
        )
        .tuples()
        .all()
    )

    groups: dict[str, list[tuple[ParsedPoint, ProposalSource, Proposal]]] = {}
    for link in links:
        proposal = proposals.get(link.proposal_id)
        if proposal is None or proposal.merged_into_id is not None:
            continue
        found = poi_text(link.raw)
        if found is None:
            continue
        result.with_poi += 1
        parsed = parse_point(found[1], field=found[0])
        if parsed is None:
            result.unparsed += 1
            continue
        groups.setdefault(parsed.name_key, []).append((parsed, link, proposal))

    for name_key, members in groups.items():
        spellings = Counter(parsed.name_display for parsed, _l, _p in members)
        top = max(spellings.values())
        display = min(s for s, n in spellings.items() if n == top)
        if not source.licence.allows_raw_publication:
            display = withhold_coordinates(display)
        first = members[0][0]
        member_proposals = [p for _parsed, _l, p in members]
        jurisdictions = Counter(p.jurisdiction for p in member_proposals if p.jurisdiction)
        jurisdiction = (
            min(j for j, n in jurisdictions.items() if n == max(jurisdictions.values()))
            if jurisdictions
            else None
        )
        bus = next((parsed.bus_number for parsed, _l, _p in members if parsed.bus_number), None)
        retrieved = max(_utc(link.retrieved_at) for _parsed, link, _p in members)
        facts = {
            "name_display": display,
            "voltage_kv": first.voltage_kv,
            "bus_number": bus,
            "kind": first.kind,
            "operator": _operator_token(source, member_proposals),
            "jurisdiction": jurisdiction,
            "retrieved_at": retrieved,
            "source_url": source.url,
            "licence_id": source.licence_id,
        }
        point = existing.get(name_key)
        if point is None:
            point_id = new_uuid()
            point = InterconnectionPoint(
                id=point_id,
                public_id=public_id("poi", point_id),
                source_id=source.id,
                name_key=name_key,
                key_rule=KEY_RULE_VERSION,
                **facts,
            )
            session.add(point)
            existing[name_key] = point
            result.points_created += 1
        else:
            for attr, value in facts.items():
                current = getattr(point, attr)
                if attr == "retrieved_at" and current is not None:
                    current = _utc(current)
                if attr == "voltage_kv" and current is not None:
                    current = float(current)
                if current != value:
                    setattr(point, attr, value)
        result.points_touched += 1
        result.by_kind[first.kind] += 1
        for proposal in member_proposals:
            current_id = proposal.interconnection_point_id
            if current_id is not None and current_id != point.id:
                owner = point_sources.get(current_id)
                if owner is not None and owner != source.id:
                    continue  # another register named this proposal's POI first
            if current_id != point.id:
                proposal.interconnection_point_id = point.id
            result.linked += 1
    session.flush()
    return result


def link_all_points(session: Session) -> list[LinkResult]:
    """`link_source_points` over every source that has proposal links: the backfill for a store
    loaded before migration 0026, and what `web/dev_up.py` runs after its load."""
    source_ids = session.scalars(select(ProposalSource.source_id).distinct()).all()
    results = []
    for source_id in sorted(source_ids):
        source = session.get(Source, source_id)
        if source is None:
            continue
        results.append(link_source_points(session, source))
    return results


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI over link_all_points
    """`python -m services.ingest.interconnection` -- backfill every source's points into the
    store `DATABASE_URL` names, then commit."""
    from services.db.session import get_engine, get_sessionmaker

    argparse.ArgumentParser(description=main.__doc__).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    session = get_sessionmaker(get_engine())()
    try:
        for result in link_all_points(session):
            log.info("%s", result.summary())
        session.commit()
    finally:
        session.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
