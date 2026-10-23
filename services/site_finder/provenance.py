"""Provenance for values the site finder reads from files rather than from rows that carry their own
quartet: the flood shares (FEMA NFHL), the county (Census TIGER/Line), fiber (FCC BDC), the
prospecting layers (their build's manifest).

The shape matches `services/api/serialize.py::provenance_quartet` (`source_id`, `source_name`,
`source_url`, `retrieved_at`, `licence_id`, `reuse_class`, `attribution_text`), so the API can put a
file-backed value and a database row side by side, plus `vintage` and a `note` where the source asks
for one. The licence id, class and credit come from `data/sources.yaml` (the registry), never from
the file, so a terms change in the register reaches every served value on the next run.
"""

from __future__ import annotations

import functools
from typing import Any

from pipeline.connectors.registry import Registry


@functools.cache
def _registry() -> Registry:
    return Registry()


def registry() -> Registry:
    """The register, read once per process (parsing `data/sources.yaml` takes about two seconds)."""
    return _registry()


def registry_provenance(
    source_id: str,
    *,
    source_url: str,
    retrieved_at: Any,
    vintage: Any = None,
    note: str | None = None,
) -> dict[str, Any]:
    reg = _registry()
    entry = reg.get(source_id) if source_id in reg else None
    out: dict[str, Any] = {
        "source_id": source_id,
        "source_name": entry.name if entry else source_id,
        "source_url": source_url,
        "retrieved_at": str(retrieved_at) if retrieved_at is not None else None,
        "licence_id": entry.licence_id if entry else None,
        "reuse_class": entry.reuse if entry else "unknown",
        "attribution_text": (entry.credit_text if entry else None) or (entry.name if entry else source_id),
    }
    if vintage is not None:
        out["vintage"] = str(vintage)
    if note:
        out["note"] = note
    return out


def publishable(source_id: str) -> bool:
    """Whether the register lets a value from `source_id` leave the building at all."""
    from pipeline.connectors.registry import GATED_REUSE

    reg = _registry()
    if source_id not in reg:
        return False
    entry = reg.get(source_id)
    return entry.reuse not in GATED_REUSE and (entry.publication or "none") != "none"
