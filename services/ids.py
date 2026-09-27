"""Public identifiers (docs/21-data-model.md §1: "public_id is what the API and URLs expose;
internal uuid never leaves the store") and slugs (US-201 AC3).

`api/openapi.yaml` pins the shape: `^prop_[0-9A-HJKMNP-TV-Z]{10,26}$` etc. — Crockford base32
(no I, L, O, U) of the internal uuid, zero-padded to at least 10 characters.
"""

from __future__ import annotations

import re
import uuid as _uuid

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
