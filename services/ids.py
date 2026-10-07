"""Public identifiers (docs/21-data-model.md §1: "public_id is what the API and URLs expose;
internal uuid never leaves the store") and slugs (US-201 AC3).

`api/openapi.yaml` pins the shape: `^prop_[0-9A-HJKMNP-TV-Z]{10,26}$` etc. — Crockford base32
(no I, L, O, U) of the internal uuid, zero-padded to at least 10 characters.
"""

from __future__ import annotations

import re
import uuid as _uuid
from collections.abc import Container

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def crockford_from_uuid(value: _uuid.UUID) -> str:
    n = value.int
    if n == 0:
        digits = "0"
    else:
        chars: list[str] = []
        while n:
            n, rem = divmod(n, 32)
            chars.append(_CROCKFORD[rem])
        digits = "".join(reversed(chars))
    return digits.zfill(10)


def public_id(prefix: str, value: _uuid.UUID) -> str:
    return f"{prefix}_{crockford_from_uuid(value)}"


def slugify(text: str, *, max_length: int = 80) -> str:
    lowered = text.strip().lower()
    slug = _SLUG_STRIP.sub("-", lowered).strip("-")
    return (slug or "record")[:max_length]


#: Characters of the public id's tail a suffixed slug starts with. Six Crockford digits are the low
#: 30 bits of the uuid; for a uuid v7 (`services.db.types.uuid7`) those are all random, so two
#: records with the same title collide with probability 1 in 2**30 per pair -- about 1 in 1,200
#: per full load of the eval fixture's 1,352 "Untitled" proposals. Hence the check below.
SLUG_SUFFIX_MIN = 6


def unique_slug(
    text: str,
    record_public_id: str,
    taken: Container[str],
    *,
    bare_first: bool = False,
    min_suffix: int = SLUG_SUFFIX_MIN,
) -> str:
    """A slug for a new record that is not in `taken` (every slug the table already holds, plus
    any this run has assigned but not yet flushed).

    `slugify(text)` followed by the shortest tail of `record_public_id`'s digits, at least
    `min_suffix` long, that is free -- so a record that does not collide gets exactly the slug it
    always got, and one that does gets one or two more characters of its own id. Unique by
    construction once the tail is the whole id, because `public_id` is unique per table and a
    suffix never contains `-` (two equal slugs must share both base and tail). `bare_first` tries
    `slugify(text)` alone first (organisations, whose slug is the bare name when it is free).

    Deterministic in its inputs and only ever called when a record is created: a reload matches
    the stored row and never reassigns its slug, which is in public URLs (US-201 AC3)."""
    base = slugify(text)
    if bare_first and base not in taken:
        return base
    digits = record_public_id.rpartition("_")[2].lower()
    for length in range(min(min_suffix, len(digits)), len(digits) + 1):
        candidate = f"{base}-{digits[-length:]}"
        if candidate not in taken:
            return candidate
    raise ValueError(
        f"slug {base}-{digits} is already taken: {record_public_id} is not a new public id "
        "(a store consistency bug, not a data error)"
    )


def parse_public_id(prefix: str, value: str) -> _uuid.UUID | None:
    """Inverse of `public_id` for an entity whose public id is *synthesised* from its internal uuid
    rather than stored (`match` -> `mat_...`, `event` -> `evt_...`; docs/21 §3.11 gives `match` no
    `public_id` column). `None` for anything that is not `<prefix>_<crockford digits>` or whose
    digits do not fit a uuid -- callers turn that into the same 404 an unknown id gets, never a 400
    that would confirm the shape of a real id."""
    if not value.startswith(f"{prefix}_"):
        return None
    digits = value[len(prefix) + 1 :]
    if not digits:
        return None
    n = 0
    for ch in digits:
        index = _CROCKFORD.find(ch)
        if index < 0:
            return None
        n = n * 32 + index
    try:
        return _uuid.UUID(int=n)
    except ValueError:
        return None
