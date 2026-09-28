"""Point-of-interconnection text -> named grid node (docs/25 §2; owner decision 2026-09-28,
"substations and transmission lines ... to place grid interconnection points").

Pure functions, no database and no network, same split as `pipeline/context/ethanol_match.py`:
this module parses the free-text point of interconnection a queue row carries and scores it
against a table of named nodes; whoever owns the link (lane G1's `interconnection_point`) reads
the crosswalk parquet this module writes and decides what to store. Nothing here writes to the
database.

Inputs, measured 2026-09-28 on `data/normalized/<source_id>/*.parquet`:

- ERCOT (`raw["Interconnection Location"]`, 1,778 rows, 1,464 distinct strings): bus numbers and
  ERCOT bus mnemonics around a name -- ``"39010 138kV HASTINGS"``, ``"7254 WEIMAR8 138kV"``,
  ``"345kV BUS #5725 PAWNEE SWITCHING STATION"``, ``"Tap 138kV 7100 Burnet - 7529 Bertram"``.
- CAISO (same key, 2,278 rows, 1,684 distinct): ``"Tesla Substation 230 KV bus"``,
  ``"Eldorado 500/230kV Substation"``, ``"Antelope-Magunden 230kV"``.
- NYISO (same key, 1,804 non-null of 1,814 rows, 1,463 distinct): ``"Oswego Substation 345kV"``,
  ``"Rotterdam - Meco 115kV"``, ``"Hurley Ave & Saugerties 69kV"``.
- NESO (`raw["Connection Site"]`, 2,198 rows, 1,237 distinct): GB sites, ``"Drax 132kV
  Substation"``. No US node table can match these; they are parsed and reported, never matched
  against US nodes (the ISO -> country rule below).

**What is not a substation, and is never forced onto one** (`parse_poi`): a string naming two
places joined by a dash, ``to`` or ``-``, or containing ``tap``/``line``/``ckt``/``circuit``, is a
point *on a line between two substations* (``"A - B 115kV"`` is a line tap), kind ``line_tap``;
a string naming several places (``;``, ``,``, ``&``, ``and``, ``via``) is kind ``multiple``. Both
are reported with their reason and left unmatched. Hyphenated single place names
(``Sedro-Woolley``) are therefore mis-read as taps and left unmatched -- the conservative error.

**Matching** (`match_poi`): the node table is blocked on state (the queue row's own state when it
has one, else the ISO's footprint in `ISO_STATES`), then

1. ``exact``: the normalised POI name equals a node's normalised name. Score 1.0.
2. ``fuzzy``: the best `rapidfuzz.fuzz.ratio` over the blocked nodes at or above
   `FUZZY_THRESHOLD` (whole-string ratio, not token-set: token-set scores ``"north alvin"``
   against ``"alvin"`` at 100). Score is the ratio / 100.

Voltage agreement is recorded on every match (``voltage_agrees`` True / False / None where
either side states no voltage) and used differently at the two steps, because a node's voltages
are only those of the lines in the layer that end at it -- a partial list. At the exact step a
same-named node at another voltage is still the match (Whirlwind 230 kV, a node whose layer
lines are all 500 kV) but scores `EXACT_VOLTAGE_CONFLICT_SCORE` and carries ``voltage_agrees =
False`` -- provided the node is placed (two or more lines meet at it); a node named by a single
line at another voltage is ``voltage_conflict``. Among several same-named nodes the agreeing
ones are preferred. At the fuzzy step a
voltage disagreement drops the candidate (``voltage_conflict`` when that empties the set): a
near-name at the wrong voltage is too weak to keep. Several surviving nodes with the
same name more than `SAME_PLACE_KM` apart are ``ambiguous`` and left unmatched -- a common name
("Hastings") is not tie-broken by guesswork.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from rapidfuzz import fuzz

#: Whole-string ratio at or above which a fuzzy candidate is accepted. Measured 2026-09-28 on every
#: fuzzy match at 90 (18): the three between 90 and 92 were "Paris" -> "Parish" twice (different
#: Texas substations, Lamar vs Fort Bend county) and "El Sequndo" -> "El Segundo"; all 15 at or
#: above 92 were right (docs/25 §2).
FUZZY_THRESHOLD = 92.0
#: The best fuzzy candidate must beat the runner-up (a different place) by at least this much.
FUZZY_MARGIN = 3.0
#: Score of an exact name match whose node states none of the POI's voltages (`match_poi`).
EXACT_VOLTAGE_CONFLICT_SCORE = 0.8
#: Same-named nodes closer than this are one place (a substation and its switchyard, or a node
#: named at both ends of two lines); farther apart they are different places -> ``ambiguous``.
SAME_PLACE_KM = 3.0

#: ISO -> the states a point of interconnection can be in when the queue row states none.
#: CAISO's balancing area reaches into Nevada and Arizona (Eldorado, Palo Verde).
ISO_STATES: dict[str, frozenset[str]] = {
    "ERCOT": frozenset({"TX"}),
    "CAISO": frozenset({"CA", "NV", "AZ"}),
    "NYISO": frozenset({"NY"}),
}
#: ISOs whose points of interconnection are outside the US node table altogether.
NON_US_ISOS = frozenset({"NESO"})

SOURCE_ISO: dict[str, str] = {
    "us.iso.ercot.gen_queue": "ERCOT",
    "us.iso.caiso.gen_queue": "CAISO",
    "us.iso.nyiso.gen_queue": "NYISO",
    "gb.neso.tec_register": "NESO",
}
#: The raw-record key holding the point of interconnection, per source.
POI_FIELD: dict[str, str] = {
    "us.iso.ercot.gen_queue": "Interconnection Location",
    "us.iso.caiso.gen_queue": "Interconnection Location",
    "us.iso.nyiso.gen_queue": "Interconnection Location",
    "gb.neso.tec_register": "Connection Site",
}

_KV_RE = re.compile(r"((?:\d+(?:\.\d+)?\s*/\s*)*\d+(?:\.\d+)?)\s*-?\s*kv\b", re.I)
_TAP_RE = re.compile(r"\b(?:tap|taps|tapped|tapping|line|lines|ckt|circuit|loop|segment|corridor)\b", re.I)
_MULTI_RE = re.compile(r"[;,&]|\band\b|\bvia\b|\bor\b", re.I)
#: Two names joined by a dash (any of the dashes the queues use, and NYISO's mis-encoded ``Ð``)
#: or by ``to``; a leading ``To`` ("To 345 kV Birthright Station") is not a join.
_JOIN_RE = re.compile(r"[a-z)]\s*[-–—Ð]\s*[a-z0-9(]|[a-z)]\s+to\s+[a-z0-9(]", re.I)
_PAREN_RE = re.compile(r"\([^)]*\)")
_BUS_NO_RE = re.compile(r"#\s*\d+|\bbus\s*:?\s*\d+|\b\d{3,6}\b", re.I)

#: Words that describe the kind of node or the connection, not the place.
_STOPWORDS = frozenset(
    {
        "substation", "substations", "sub", "subs", "station", "switching", "switchyard", "switch",
        "switchstation", "ses", "ss", "bus", "buses", "kv", "poi", "at", "the", "new", "existing",
        "proposed", "future", "to", "of", "yard", "interconnection", "point", "gen", "tie", "no",
        "collector", "high", "side", "low", "open", "position", "breaker", "bay", "tbd", "a", "b",
        # Transmission-owner abbreviations the queues prefix to a node name ("SCE owned Eldorado",
        # "North Alvin TNMP"); they name the owner, not the place.
        "owned", "sce", "pge", "sdge", "iid", "ladwp", "tnmp", "aep", "oncor", "cps", "lcra", "cnp",
        "nyseg", "nypa", "lipa", "rge", "coned", "cenhud",
    }
)  # fmt: skip
#: Single-letter / short abbreviations expanded on both sides before comparison.
_ABBREV = {"n": "north", "s": "south", "e": "east", "w": "west", "st": "saint", "mt": "mount", "ft": "fort"}
#: Names a node table uses for "no name" (HIFLD-style ``TAP123456``, ``UNKNOWN123``).
_PLACEHOLDER_RE = re.compile(r"^(?:tap|unknown|not available|na|none|deadend|dead end)(?:\b|\d)", re.I)


@dataclass(frozen=True)
class ParsedPoi:
    raw: str
    kind: str  # "substation" | "line_tap" | "multiple" | "empty"
    name_norm: str
    voltages_kv: tuple[float, ...] = ()


def voltages_in(text: str) -> tuple[float, ...]:
    """Every kV figure stated, ``"500/230kV"`` -> (500.0, 230.0); implausible values dropped."""
    out: list[float] = []
    for m in _KV_RE.finditer(text or ""):
        for part in m.group(1).split("/"):
            try:
                v = float(part.strip())
            except ValueError:
                continue
            if 1.0 <= v <= 1200.0 and v not in out:
                out.append(v)
    return tuple(out)


def _strip_mnemonic(token: str) -> str:
    """ERCOT bus mnemonics end in a voltage digit and an optional letter (``WEIMAR8``,
    ``JAVELINA4A``): drop that suffix from alphabetic tokens longer than three letters."""
    m = re.fullmatch(r"([a-z]{4,})\d+[a-z]?", token)
    return m.group(1) if m else token


def normalise_name(text: Any) -> str:
    """Lower-case place tokens with kV figures, bus numbers, parentheticals, node-kind words and
    punctuation removed; used identically on both sides of every comparison."""
    if text is None or (isinstance(text, float) and math.isnan(text)):
        return ""
    s = str(text).lower()
    s = _PAREN_RE.sub(" ", s)
    s = _KV_RE.sub(" ", s)
    s = _BUS_NO_RE.sub(" ", s)
    s = s.replace("&", " and ").replace("_", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = []
    for tok in s.split():
        tok = _strip_mnemonic(tok)
        tok = _ABBREV.get(tok, tok)
        if tok in _STOPWORDS or tok.isdigit():
            continue
        tokens.append(tok)
    return " ".join(tokens)


def parse_poi(text: Any) -> ParsedPoi:
    raw = "" if text is None or (isinstance(text, float) and math.isnan(text)) else str(text).strip()
    if not raw:
        return ParsedPoi(raw=raw, kind="empty", name_norm="")
    voltages = voltages_in(raw)
    body = _PAREN_RE.sub(" ", raw)
    kv_free = _KV_RE.sub(" ", body)
    name = normalise_name(raw)
    if _TAP_RE.search(kv_free):
        kind = "line_tap"
    elif _JOIN_RE.search(re.sub(r"^\s*to\s+", "", kv_free, flags=re.I)):
        kind = "line_tap"
    elif _MULTI_RE.search(kv_free):
        kind = "multiple"
    elif not name:
        kind = "empty"
    else:
        kind = "substation"
    return ParsedPoi(raw=raw, kind=kind, name_norm=name, voltages_kv=voltages)


# --------------------------------------------------------------------------------- node table
@dataclass(frozen=True)
class Node:
    node_id: str
    name: str
    name_norm: str
    state: str | None
    lon: float | None
    lat: float | None
    voltages_kv: tuple[float, ...] = ()


@dataclass
class NodeIndex:
    """Nodes blocked by state; `by_name[state][name_norm]` for the exact step."""

    by_state: dict[str, list[Node]] = field(default_factory=dict)
    by_name: dict[str, dict[str, list[Node]]] = field(default_factory=dict)

    @classmethod
    def from_nodes(cls, nodes: Iterable[Node]) -> NodeIndex:
        idx = cls()
        for n in nodes:
            if not n.name_norm or _PLACEHOLDER_RE.match(n.name_norm) or not n.state:
                continue
            idx.by_state.setdefault(n.state, []).append(n)
            idx.by_name.setdefault(n.state, {}).setdefault(n.name_norm, []).append(n)
        return idx

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> NodeIndex:
        """Columns: ``node_id``, ``name``, ``state`` (two-letter or ``US-XX``), ``lon``, ``lat``,
        optional ``voltages_kv`` (list of floats)."""
        nodes = []
        for row in df.to_dict("records"):
            state = _state2(row.get("state"))
            volts = row.get("voltages_kv")
            vtuple = tuple(float(v) for v in volts) if isinstance(volts, (list, tuple)) else ()
            if not vtuple and hasattr(volts, "tolist"):
                vtuple = tuple(float(v) for v in volts.tolist())
            nodes.append(
                Node(
                    node_id=str(row["node_id"]),
                    name=str(row.get("name") or ""),
                    name_norm=normalise_name(row.get("name")),
                    state=state,
                    lon=_float(row.get("lon")),
                    lat=_float(row.get("lat")),
                    voltages_kv=vtuple,
                )
            )
        return cls.from_nodes(nodes)


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _state2(value: Any) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    s = str(value).strip().upper()
    if s.startswith("US-"):
        s = s[3:]
    return s if len(s) == 2 and s.isalpha() else None


def _km(a: Node, b: Node) -> float:
    if None in (a.lat, a.lon, b.lat, b.lon):
        return math.inf
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))  # type: ignore[arg-type]
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def _one_place(nodes: Sequence[Node]) -> bool:
    return all(_km(nodes[0], n) <= SAME_PLACE_KM for n in nodes[1:])


def _voltage_filter(poi_kv: tuple[float, ...], nodes: Sequence[Node]) -> tuple[list[Node], bool | None]:
    """Nodes whose stated voltages overlap the POI's, and whether the check applied."""
    if not poi_kv:
        return list(nodes), None
    stated = [n for n in nodes if n.voltages_kv]
    if not stated:
        return list(nodes), None
    kept = [n for n in nodes if not n.voltages_kv or set(n.voltages_kv) & set(poi_kv)]
    return kept, True if kept else False


@dataclass(frozen=True)
class Match:
    method: str  # "exact" | "fuzzy" | "none"
    reason: str | None  # why "none": line_tap | multiple | empty | non_us | no_state | no_candidate | ...
    score: float | None
    node: Node | None
    voltage_agrees: bool | None
    candidates: int


def match_poi(
    poi: ParsedPoi,
    index: NodeIndex,
    *,
    iso: str,
    state: str | None = None,
    threshold: float = FUZZY_THRESHOLD,
) -> Match:
    if iso in NON_US_ISOS:
        return Match("none", "non_us", None, None, None, 0)
    if poi.kind != "substation":
        return Match("none", poi.kind, None, None, None, 0)
    st = _state2(state)
    states = frozenset({st}) if st else ISO_STATES.get(iso, frozenset())
    if not states:
        return Match("none", "no_state", None, None, None, 0)
    blocked = [n for s in sorted(states) for n in index.by_state.get(s, [])]
    if not blocked:
        return Match("none", "no_candidate", None, None, None, 0)

    exact = [n for s in sorted(states) for n in index.by_name.get(s, {}).get(poi.name_norm, [])]
    if exact:
        kept, agrees = _voltage_filter(poi.voltages_kv, exact)
        if not kept:
            # Every same-named node states other voltages. A node's voltages are only those of the
            # lines in the layer that end there -- a partial list (a 500 kV yard whose 230 kV lines
            # are not in the subset) -- so the name match stands, marked and scored lower, but only
            # for a node the layer places (two or more lines meet there). A name carried by one
            # line at another voltage is too weak: measured 2026-09-28, 16 of 34 such matches put
            # the POI in a county its projects are not in (SCE's Antelope vs PG&E's 70 kV Antelope).
            if not any(n.lon is not None for n in exact):
                return Match("none", "voltage_conflict", None, None, False, len(exact))
            exact = [n for n in exact if n.lon is not None]
            if not _one_place(exact):
                return Match("none", "ambiguous", EXACT_VOLTAGE_CONFLICT_SCORE, None, False, len(exact))
            return Match("exact", None, EXACT_VOLTAGE_CONFLICT_SCORE, exact[0], False, len(exact))
        if not _one_place(kept):
            return Match("none", "ambiguous", 1.0, None, agrees, len(kept))
        return Match("exact", None, 1.0, kept[0], agrees, len(kept))

    scored = sorted(
        ((fuzz.ratio(poi.name_norm, n.name_norm), n) for n in blocked),
        key=lambda t: -t[0],
    )
    top = [(s, n) for s, n in scored if s >= threshold]
    if not top:
        best_ratio = scored[0][0] if scored else None
        return Match(
            "none", "below_threshold", round(best_ratio / 100, 4) if best_ratio else None, None, None, 0
        )
    kept, agrees = _voltage_filter(poi.voltages_kv, [n for _, n in top])
    if not kept:
        return Match("none", "voltage_conflict", None, None, False, len(top))
    kept_ids = {id(n) for n in kept}
    ranked = [(s, n) for s, n in top if id(n) in kept_ids]
    best_score, best = ranked[0]
    rivals = [(s, n) for s, n in ranked[1:] if n.name_norm != best.name_norm or _km(best, n) > SAME_PLACE_KM]
    if rivals and best_score - rivals[0][0] < FUZZY_MARGIN:
        return Match("none", "ambiguous", round(best_score / 100, 4), None, agrees, len(ranked))
    same_name = [n for _, n in ranked if n.name_norm == best.name_norm]
    if not _one_place(same_name):
        return Match("none", "ambiguous", round(best_score / 100, 4), None, agrees, len(ranked))
    return Match("fuzzy", None, round(best_score / 100, 4), best, agrees, len(ranked))


# ------------------------------------------------------------------------------ crosswalk
CROSSWALK_COLUMNS = [
    "source_id",
    "iso",
    "poi_raw",
    "poi_kind",
    "poi_name_norm",
    "poi_voltages_kv",
    "state",
    "queue_rows",
    "method",
    "reason",
    "score",
    "voltage_agrees",
    "candidates",
    "node_id",
    "node_name",
    "node_state",
    "node_voltages_kv",
]


def poi_rows(source_id: str, df: pd.DataFrame) -> pd.DataFrame:
    """One row per distinct (POI string, state) in a normalised queue frame, with the number of
    queue rows that carry it. ``raw`` may be a dict or its JSON string."""
    import json

    key = POI_FIELD[source_id]
    counts: dict[tuple[str, str | None], int] = {}
    for raw, state in zip(df["raw"], df.get("state", pd.Series([None] * len(df))), strict=False):
        rec: Mapping[str, Any] = json.loads(raw) if isinstance(raw, str) else (raw or {})
        text = rec.get(key)
        if text is None or not str(text).strip():
            continue
        k = (str(text).strip(), _state2(state))
        counts[k] = counts.get(k, 0) + 1
    return pd.DataFrame(
        [{"poi_raw": t, "state": s, "queue_rows": n} for (t, s), n in counts.items()],
        columns=["poi_raw", "state", "queue_rows"],
    )


def build_crosswalk(frames: Mapping[str, pd.DataFrame], index: NodeIndex) -> pd.DataFrame:
    """`frames`: source_id -> normalised queue frame. One output row per distinct (source, POI
    string, state), matched or not, so the match rate is read straight off it."""
    rows: list[dict[str, Any]] = []
    for source_id, df in frames.items():
        iso = SOURCE_ISO[source_id]
        for rec in poi_rows(source_id, df).to_dict("records"):
            poi = parse_poi(rec["poi_raw"])
            m = match_poi(poi, index, iso=iso, state=rec["state"])
            rows.append(
                {
                    "source_id": source_id,
                    "iso": iso,
                    "poi_raw": poi.raw,
                    "poi_kind": poi.kind,
                    "poi_name_norm": poi.name_norm,
                    "poi_voltages_kv": list(poi.voltages_kv),
                    "state": rec["state"],
                    "queue_rows": rec["queue_rows"],
                    "method": m.method,
                    "reason": m.reason,
                    "score": m.score,
                    "voltage_agrees": m.voltage_agrees,
                    "candidates": m.candidates,
                    "node_id": m.node.node_id if m.node else None,
                    "node_name": m.node.name if m.node else None,
                    "node_state": m.node.state if m.node else None,
                    "node_voltages_kv": list(m.node.voltages_kv) if m.node else [],
                }
            )
    out = pd.DataFrame(rows, columns=CROSSWALK_COLUMNS)
    # Nullable boolean, so parquet keeps True / False / null rather than their string spellings.
    out["voltage_agrees"] = out["voltage_agrees"].astype("boolean")
    return out


def match_rates(crosswalk: pd.DataFrame) -> pd.DataFrame:
    """Per ISO, over distinct POI strings: total, substation-kind, matched, and the two rates."""
    out = []
    for iso, g in crosswalk.groupby("iso", sort=True):
        total = len(g)
        subs = int((g["poi_kind"] == "substation").sum())
        matched = int((g["method"] != "none").sum())
        out.append(
            {
                "iso": iso,
                "distinct_poi": total,
                "substation_kind": subs,
                "line_tap": int((g["poi_kind"] == "line_tap").sum()),
                "multiple": int((g["poi_kind"] == "multiple").sum()),
                "matched": matched,
                "exact": int((g["method"] == "exact").sum()),
                "fuzzy": int((g["method"] == "fuzzy").sum()),
                "match_rate": round(matched / total, 4) if total else 0.0,
                "match_rate_substation_kind": round(matched / subs, 4) if subs else 0.0,
            }
        )
    return pd.DataFrame(out)


# --------------------------------------------------------------------------------------- CLI
def _latest(source_id: str, root: Any) -> Any:
    files = sorted((root / source_id).glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no normalised parquet under {root / source_id}")
    return files[-1]


def main(argv: list[str] | None = None) -> None:
    """``python -m pipeline.context.poi_match``: the four queues' latest normalised parquets against
    the named line endpoints of the transmission layer -> the crosswalk parquet, plus one JSON line
    of per-ISO rates. Writes no database row (lane G1 owns the interconnection-point link)."""
    import argparse
    import json
    import pathlib

    from pipeline.connectors.base import to_parquet_safe
    from pipeline.context import lbnl_transmission

    root = pathlib.Path(__file__).resolve().parents[2] / "data" / "normalized"
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--lines", type=pathlib.Path, default=lbnl_transmission.default_output())
    parser.add_argument(
        "--out", type=pathlib.Path, default=root / "context" / "poi_substation_crosswalk.parquet"
    )
    args = parser.parse_args(argv)

    nodes = lbnl_transmission.endpoint_nodes(pd.read_parquet(args.lines))
    index = NodeIndex.from_frame(nodes)
    frames = {sid: pd.read_parquet(_latest(sid, root)) for sid in POI_FIELD}
    crosswalk = build_crosswalk(frames, index)
    located = nodes.set_index("node_id")[["lon", "lat", "location_method", "line_ids"]]
    crosswalk = crosswalk.join(located, on="node_id")
    crosswalk = crosswalk.rename(columns={"lon": "node_lon", "lat": "node_lat", "line_ids": "node_line_ids"})
    args.out.parent.mkdir(parents=True, exist_ok=True)
    to_parquet_safe(crosswalk).to_parquet(args.out, index=False)
    rates = match_rates(crosswalk)
    summary = {
        "out": str(args.out),
        "nodes": len(nodes),
        "rows": len(crosswalk),
        "rates": rates.to_dict("records"),
    }
    print(json.dumps(summary))  # noqa: T201 -- CLI summary line, same convention as eia_atlas.py


if __name__ == "__main__":
    main()
