"""Order-independent keys for a source id that appears more than once in one snapshot
(docs/22 §2 "record_id must be unique", audit 2026-09-18 §3.1 "Change events" item 1).

A `dedupe_strategy = "suffix"` connector (NYISO today) has a handful of queue positions that
occur twice in the workbook. Until 2026-09-18 the repeats were numbered by file position
(`X`, `X#2`, ...), so a row reorder in the source swapped the two identities and the diff
fabricated a withdrawal plus a re-creation for each. The suffix is now derived from the row's
own disambiguating content, so it is the same whatever position the row lands in:

    record_id = f"{source_id}:{source_record_id}#{content_disambiguator(row)}"

for *every* member of a duplicated group (a unique id keeps its bare key). The disambiguator is
a short hash over the stable fields the connector already normalises (`DEDUPE_KEY_COLUMNS`:
name, capacity, county/state, technology, sponsor). Status, dates and anything else that moves
over a record's life are deliberately excluded, so a status change on a duplicated id stays a
status change and never a new identity. Two rows whose disambiguating fields are identical fall
back to a hash of the raw row, and two byte-identical rows to their order among themselves
(interchangeable by definition, so the order is harmless).

Legacy keys on read: snapshots and store rows written before this change carry the positional
`#N` suffix. `align_previous_keys` maps a previous snapshot onto the new keys by the same
disambiguator before the diff runs, so the first run after the change reports only real
changes (no fabricated removal/new pair), and `services/ingest/loader.py` applies the same
content match against stored `#N` rows (see its module docstring). No stored identity is
rewritten; the matcher simply accepts either spelling.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

import pandas as pd

#: Fields that distinguish two rows sharing one source id, in precedence order. Only the columns
#: present on the frame are used, so the same function serves proposals (name_norm, county_norm,
#: ...) and opportunities (title, capacity_sought_mw, ...).
DEDUPE_KEY_COLUMNS: tuple[str, ...] = (
    "name_norm",
    "name_canonical",
    "title",
    "sponsor_norm",
    "technology",
    "capacity_mw",
    "capacity_sought_mw",
    "county_norm",
    "county",
    "state",
    "jurisdiction",
)

#: Content suffix: `#h` + 10 hex characters. Distinct from the legacy positional `#<int>`.
CONTENT_SUFFIX_RE = re.compile(r"^(?P<base>.+)#(?P<suffix>h[0-9a-f]{10}(?:~\d+)?)$")
LEGACY_SUFFIX_RE = re.compile(r"^(?P<base>.+)#(?P<suffix>\d+)$")


def _isna(v: Any) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v)) if not isinstance(v, (list, dict, tuple, set)) else False
    except (TypeError, ValueError):
        return False


def _norm(v: Any) -> str:
    if _isna(v):
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{float(v):.6g}"
    s = str(v).strip().lower()
    try:
        return f"{float(s):.6g}"  # "100.0" and 100 must hash alike (parquet round-trips as text)
    except ValueError:
        return s


def content_disambiguator(row: Mapping[str, Any], columns: tuple[str, ...] = DEDUPE_KEY_COLUMNS) -> str:
    """`h` + 10 hex chars over the stable disambiguating fields present on `row`."""
    parts = [f"{c}={_norm(row.get(c))}" for c in columns if c in row]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()  # noqa: S324 - identity key, not security
    return "h" + digest[:10]


def raw_disambiguator(raw: str) -> str:
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324 - identity key, not security
    return "h" + digest[:10]


def split_key(key: str) -> tuple[str, str | None]:
    """`(base, suffix)` for either spelling; `(key, None)` when the key carries no suffix."""
    m = CONTENT_SUFFIX_RE.match(key) or LEGACY_SUFFIX_RE.match(key)
    if m is None:
        return key, None
    return m.group("base"), m.group("suffix")


def suffix_duplicates(df: pd.DataFrame, raw_payloads: list[str]) -> tuple[pd.Series, int]:
    """Return `record_id` with every duplicated value suffixed by content, plus the count of rows
    suffixed. `df` must carry `record_id` and be positionally aligned with `raw_payloads`."""
    rid = df["record_id"].astype("string")
    dup = rid.duplicated(keep=False).to_numpy()
    if not dup.any():
        return rid, 0
    rows = df.to_dict("records")
    keys = list(rid)
    for i in [int(i) for i in dup.nonzero()[0]]:
        keys[i] = f"{keys[i]}#{content_disambiguator(rows[i])}"
    # identical disambiguating fields: fall back to the raw row, then to order among identical rows
    still = pd.Series(keys).duplicated(keep=False).to_numpy()
    for i in [int(i) for i in still.nonzero()[0]]:
        keys[i] = f"{rid.iloc[i]}#{raw_disambiguator(raw_payloads[i])}"
    seen: dict[str, int] = {}
    for i, k in enumerate(keys):
        n = seen.get(k, 0)
        seen[k] = n + 1
        if n:
            keys[i] = f"{k}~{n + 1}"
    return pd.Series(keys, index=df.index, dtype="string"), int(dup.sum())


def align_previous_keys(previous: pd.DataFrame, current: pd.DataFrame) -> pd.DataFrame:
    """Re-key rows of `previous` whose base id has a different key set in `current`, matching
    by `content_disambiguator` computed on both frames (same columns, same function). Covers the
    one-time legacy `#N` transition and the unique <-> duplicated transitions in either
    direction. Rows that match nothing keep their key (and diff as `removed`, correctly)."""
    if "record_id" not in previous.columns or "record_id" not in current.columns:
        return previous
    prev_keys = previous["record_id"].astype("string")
    cur_keys = set(current["record_id"].astype("string"))
    if set(prev_keys).issubset(cur_keys):
        return previous
    prev_groups: dict[str, list[int]] = {}
    for i, key in enumerate(prev_keys):
        base, _ = split_key(str(key))
        prev_groups.setdefault(base, []).append(i)
    cur_groups: dict[str, list[int]] = {}
    for i, key in enumerate(current["record_id"].astype("string")):
        base, _ = split_key(str(key))
        cur_groups.setdefault(base, []).append(i)

    out = previous.copy()
    prev_rows = previous.to_dict("records")
    cur_rows = current.to_dict("records")
    cur_key_list = list(current["record_id"].astype("string"))
    new_keys = list(prev_keys)
    for base, prev_idx in prev_groups.items():
        cur_idx = cur_groups.get(base)
        if not cur_idx:
            continue
        prev_set = {str(prev_keys.iloc[i]) for i in prev_idx}
        cur_set = {str(cur_key_list[i]) for i in cur_idx}
        if prev_set == cur_set:
            continue
        unclaimed = {i: content_disambiguator(cur_rows[i]) for i in cur_idx}
        for i in prev_idx:
            if str(prev_keys.iloc[i]) in cur_set:
                # exact key survives; take it out of the pool so nothing else claims it
                unclaimed = {j: d for j, d in unclaimed.items() if cur_key_list[j] != prev_keys.iloc[i]}
        for i in prev_idx:
            if str(prev_keys.iloc[i]) in cur_set:
                continue
            want = content_disambiguator(prev_rows[i])
            match = next((j for j, d in unclaimed.items() if d == want), None)
            if match is None:
                continue
            new_keys[i] = cur_key_list[match]
            del unclaimed[match]
    out["record_id"] = pd.Series(new_keys, index=previous.index, dtype="string")
    return out


__all__ = [
    "CONTENT_SUFFIX_RE",
    "DEDUPE_KEY_COLUMNS",
    "LEGACY_SUFFIX_RE",
    "align_previous_keys",
    "content_disambiguator",
    "raw_disambiguator",
    "split_key",
    "suffix_duplicates",
]
