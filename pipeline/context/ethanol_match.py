"""Matcher for the two `ethanol_plant` registries -> one real-world plant (docs/24 §5(a) option
(a); the `asset_source` link table of `services/db/migrations/versions/0021_asset_source.py`).

`pipeline/context/ethanol_plants.py` (EIA Energy Atlas, 197 rows, exact coordinates, 2021-01-01
vintage) and `pipeline/context/ethanol_capacity.py` (EIA's annual capacity table, 191 rows,
current, no coordinate) list the same fleet of plants from two registries with no shared key
(docs/24 §2: 100 of 388 rows share a derived slug by coincidence, not by design). Measured
2026-09-22 (docs/24 §2.3): 48.2% of the 388 rows are a cross-source duplicate of a row already
present under the other source.

Same split and matching shape as `pipeline/context/ghgrp.py`: pure functions here, no database;
`services/ingest/assets.py::load_ethanol_plants` reads the two context parquets, calls
`match_ethanol`, and writes one `asset` row plus one `asset_source` row per matched or unmatched
source record.

No deterministic key exists between the two registries (neither publishes the other's id, and the
content-hash slugs collide only by chance -- docs/24 §2.2), so matching here is a single scored
rule, not "crosswalk then geo+name" as `ghgrp.py` runs: **state blocking, then a weighted score of
company name, city and nameplate-capacity closeness**, assigned globally greedy (highest-scoring
candidate pair first, each row on each side consumed at most once -- docs/24 §2.3's lesson: a
per-row greedy pass mis-assigns exactly the ownership-change pairs a global pass gets right,
because it lets a weak true match be pre-empted by a stronger wrong one before the true partner is
ever considered).

Scoring, in order of what it defends against (`data/eval/ethanol_match_labels.csv` is the
labelled sample the threshold is chosen against; see that file and docs/24 §11 for the measured
precision/recall):

- **`name_score`** (`pipeline.context.ghgrp.name_score`, reused rather than re-implemented):
  legal-form-and-generic-word-stripped token Jaccard/containment on `operator_name`. Weight 0.35.
  Alone it both under- and over-fires: it misses every ownership-change pair (the whole reason to
  fuse two registries -- docs/24 §2.1) and it wrongly rewards two *different* plants the same
  multi-site operator runs under an identical name (five "Valero Renewable Fuels LLC" plants in
  Iowa alone).
- **`city_score`**: the place name is the discriminator name_score cannot be, but a naive
  token-overlap or character-similarity metric on short place names is its own trap -- two unrelated
  Iowa towns ("Charles City", "Albert City") share the literal word "city", and difflib's character
  ratio alone puts "Hartley" and "Charles City" at 0.63. `_CITY_GENERIC` drops single-word
  suffixes/facility words that carry no place identity ("city", "town", "plant", "mill", ...) from
  counting as a match on their own; an exact match after light abbreviation normalisation
  (`Ft`->`Fort`, `St`->`Saint`, `Mt`->`Mount`) is 1.0, a shared *non-generic* token is a containment
  ratio, and anything else falls back to a character-similarity ratio **capped at 0.5** so it can
  never carry a match alone. Weight 0.35.
- **`capacity_score`**: nameplate MMgal/yr, binned on the ratio of the smaller to the larger value
  (>=0.90 -> 1.0, >=0.75 -> 0.7, >=0.60 -> 0.4, else 0.0) rather than used as a continuous number,
  because the two registries' nameplate figures are for different report dates and disagree by more
  than the owner does even on confirmed pairs (docs/24 §7.2: 43% of confirmed pairs differ by more
  than 5%). Weight 0.30. Present on every ethanol row in both registries, so it is not treated as
  optional the way a missing city or a missing coordinate is.
- No distance term: only the Atlas side carries a coordinate (docs/24 §2.4), so a distance score
  would be structurally zero on every candidate pair; name and city carry the location signal here.

A candidate pair with `name_score == 0` and `city_score in (0, None)` is never scored -- there is no
positive evidence at all, and scoring it would let capacity alone (present on every row) manufacture
matches between unrelated same-state, same-size plants.
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any

import pandas as pd

from pipeline.context.ghgrp import name_score

DEFAULT_THRESHOLD = 0.33

#: Weights for the three present signals; renormalised over whichever are actually available for
#: a given pair (only `capacity_score` can be absent, and only when a row states no nameplate).
WEIGHTS: dict[str, float] = {"name": 0.35, "city": 0.35, "capacity": 0.30}

#: Words that name a *kind* of place or facility, not the place itself; a match on one of these
#: alone (`Charles City` / `Albert City`, `Jefferson Plant` / `Jefferson`) is not place identity.
_CITY_GENERIC = frozenset(
    {
        "city",
        "town",
        "ville",
        "park",
        "center",
        "junction",
        "springs",
        "valley",
        "plains",
        "corner",
        "corners",
        "plant",
        "mill",
    }
)
_ABBREV = {"ft": "fort", "st": "saint", "mt": "mount"}
_PUNCT_RE = re.compile(r"[.,]")


def city_of(attributes_text: Any) -> str | None:
    """The place name a fuels-lane frame's `attributes_text` JSON carries: Atlas calls it `site`,
    the capacity table calls it `city` (module docstrings of the two parsers)."""
    if attributes_text is None or (isinstance(attributes_text, float) and pd.isna(attributes_text)):
        return None
    if isinstance(attributes_text, str):
        try:
            payload = json.loads(attributes_text) if attributes_text else {}
        except json.JSONDecodeError:
            return None
    elif isinstance(attributes_text, dict):
        payload = attributes_text
    else:
        return None
    value = payload.get("site") or payload.get("city")
    return str(value).strip() if value else None


def _norm_city(value: str | None) -> str:
    if not value:
        return ""
    text = _PUNCT_RE.sub("", value.lower())
    tokens = [_ABBREV.get(t, t) for t in text.split()]
    return " ".join(tokens)


def city_score(a_city: str | None, b_city: str | None) -> float | None:
    """`None` when either side states no place name at all (never on this pair of registries, but
    kept honest for reuse); otherwise 1.0 for an exact match after normalisation, a token
    containment ratio when a non-generic token is shared, and a character-similarity ratio capped
    at 0.5 otherwise (module docstring explains why the cap exists)."""
    na, nb = _norm_city(a_city), _norm_city(b_city)
    if not na or not nb:
        return None
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    # A bare digit ("1", "2", ...) identifies nothing about a place on its own -- excluded here
    # for the same reason `_CITY_GENERIC` is: a shared token must carry place identity to count.
    meaningful = {t for t in (ta & tb) - _CITY_GENERIC if not t.isdigit()}
    if meaningful:
        return round(len(ta & tb) / min(len(ta), len(tb)), 4)
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return round(0.5 * ratio, 4)


def capacity_score(a_value: float | None, b_value: float | None) -> float | None:
    if (
        a_value is None
        or b_value is None
        or (isinstance(a_value, float) and pd.isna(a_value))
        or (isinstance(b_value, float) and pd.isna(b_value))
        or a_value <= 0
        or b_value <= 0
    ):
        return None
    ratio = min(a_value, b_value) / max(a_value, b_value)
    if ratio >= 0.90:
        return 1.0
    if ratio >= 0.75:
        return 0.7
    if ratio >= 0.60:
        return 0.4
    return 0.0


def combined_score(name: float, city: float | None, capacity: float | None) -> float:
    """The weighted average of whichever of the three signals are present (`WEIGHTS`), renormalised
    over the ones actually available -- only `city`/`capacity` can be `None` here."""
    parts = [(name, WEIGHTS["name"]), (city, WEIGHTS["city"]), (capacity, WEIGHTS["capacity"])]
    present = [(v, w) for v, w in parts if v is not None]
    if not present:
        return 0.0
    total_weight = sum(w for _, w in present)
    return round(sum(v * w for v, w in present) / total_weight, 4)


def pair_score(
    a_name: str | None,
    a_city: str | None,
    a_capacity: float | None,
    b_name: str | None,
    b_city: str | None,
    b_capacity: float | None,
) -> tuple[float, float, float | None, float | None]:
    """`(combined, name_score, city_score, capacity_score)` for one candidate pair."""
    ns = name_score(a_name, b_name)
    cts = city_score(a_city, b_city)
    caps = capacity_score(a_capacity, b_capacity)
    if ns == 0.0 and not (cts or 0):
        return 0.0, ns, cts, caps
    return combined_score(ns, cts, caps), ns, cts, caps


MATCH_COLUMNS: list[str] = [
    "atlas_source_asset_id",
    "capacity_source_asset_id",
    "state_code",
    "name_score",
    "city_score",
    "capacity_score",
    "score",
    "accepted",
]


def match_ethanol(
    atlas: pd.DataFrame, capacity: pd.DataFrame, *, threshold: float = DEFAULT_THRESHOLD
) -> pd.DataFrame:
    """Candidate pairs between the two frames (`MATCH_COLUMNS`), one row per pair scored, sorted by
    score descending; `accepted` marks the globally greedy 1:1 assignment at `score >= threshold`.

    Both frames need `source_asset_id`, `operator_name`, `state_code`, `capacity_value` and
    `attributes_text` (`city_of` reads the place name out of it) -- the shape
    `pipeline.context.ethanol_plants.build_assets` / `ethanol_capacity.build_assets` produce.
    An asset present in only one registry (no candidate clears the threshold, or the state has no
    candidate at all) is simply absent from the accepted rows; the caller loads it as its own
    single-source asset (module docstring, "nothing is dropped").
    """
    a = atlas.reset_index(drop=True)
    c = capacity.reset_index(drop=True)
    a_city = a.get("attributes_text", pd.Series([None] * len(a))).map(city_of)
    c_city = c.get("attributes_text", pd.Series([None] * len(c))).map(city_of)

    by_state: dict[str, list[int]] = {}
    for j, st in c["state_code"].items():
        by_state.setdefault(str(st), []).append(int(j))

    candidates: list[dict[str, Any]] = []
    for i, st in a["state_code"].items():
        for j in by_state.get(str(st), []):
            score, ns, cts, caps = pair_score(
                a.at[i, "operator_name"],
                a_city.iat[i],
                a.at[i, "capacity_value"],
                c.at[j, "operator_name"],
                c_city.iat[j],
                c.at[j, "capacity_value"],
            )
            if score <= 0.0:
                continue
            candidates.append(
                {
                    "atlas_source_asset_id": str(a.at[i, "source_asset_id"]),
                    "capacity_source_asset_id": str(c.at[j, "source_asset_id"]),
                    "state_code": str(st),
                    "name_score": ns,
                    "city_score": cts,
                    "capacity_score": caps,
                    "score": score,
                    "_a": i,
                    "_c": j,
                }
            )

    candidates.sort(key=lambda r: (-r["score"], r["atlas_source_asset_id"], r["capacity_source_asset_id"]))
    used_a: set[int] = set()
    used_c: set[int] = set()
    rows: list[dict[str, Any]] = []
    for cand in candidates:
        accepted = cand["score"] >= threshold and cand["_a"] not in used_a and cand["_c"] not in used_c
        if accepted:
            used_a.add(cand["_a"])
            used_c.add(cand["_c"])
        rows.append({**{k: v for k, v in cand.items() if not k.startswith("_")}, "accepted": accepted})
    if not rows:
        return pd.DataFrame(columns=MATCH_COLUMNS)
    return pd.DataFrame(rows, columns=MATCH_COLUMNS)
