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
