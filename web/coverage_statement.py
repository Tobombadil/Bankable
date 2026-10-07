"""The pricing page's coverage line, derived rather than typed (expert review 2026-10-07, large-load
finding 1 of §3; docs/41 "Coverage line").

The line read "Infraque tracks every major US interconnection queue", while `/methodology` said PJM,
MISO, SPP and ISO-NE rows are ingested nowhere, and `/proposals?iso=PJM` listed 382 EIA-860M units
with nothing saying where they came from. The sentence is now built from the same two inputs the
methodology page uses: `/v1/coverage` (which sources have rows, which registered supply sources the
licence gate withholds and why) and the manifest's own `category` per source (`data/sources.yaml`,
which the web image ships). It cannot claim a queue it does not load, and it names the ones it
does not.
"""

from __future__ import annotations

import functools
import pathlib
from collections.abc import Mapping
from typing import Any

from web.viewmodels import source_label

_MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "data" / "sources.yaml"
QUEUE_CATEGORIES = ("generation_queue", "load_queue")
TENDER_CATEGORIES = ("procurement", "funding")

#: Short names for the US ISO queues the manifest registers (their ids carry the ISO key).
ISO_QUEUE_NAMES = {
    "us.iso.pjm.gen_queue": "PJM",
    "us.iso.miso.gen_queue": "MISO",
    "us.iso.spp.gen_queue": "SPP",
    "us.iso.isone.gen_queue": "ISO-NE",
}

#: Said when the API cannot say which sources have rows: nothing about coverage is claimed.
FALLBACK = "Which registers have rows here, and which are withheld and why, is listed per source."


@functools.lru_cache(maxsize=1)
def manifest_categories() -> dict[str, str]:
    """`{source_id: category}` from `data/sources.yaml`."""
    import yaml

    entries = (yaml.safe_load(_MANIFEST.read_text(encoding="utf-8")) or {}).get("sources") or []
    return {str(e["id"]): str(e.get("category") or "") for e in entries if e.get("id")}


def _names(ids: list[str]) -> list[str]:
    return sorted({ISO_QUEUE_NAMES.get(i) or source_label(i) or i for i in ids})


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def coverage_line(data: Mapping[str, Any] | None) -> str:
    """One paragraph: the queues with rows here, the US ISO queues withheld (and that proposals in
    those areas come only from EIA-860M), and the tender registers with rows."""
    if not data:
        return FALLBACK
    sources = data.get("sources") or {}
    loaded = [str(i) for i in sources.get("loaded_source_ids") or []]
    categories = manifest_categories()
    queues = [i for i in loaded if categories.get(i) in QUEUE_CATEGORIES]
    tenders = [i for i in loaded if categories.get(i) in TENDER_CATEGORIES]
    withheld_iso = [
        str(w["source_id"])
        for w in sources.get("withheld") or []
        if str(w.get("source_id")) in ISO_QUEUE_NAMES
    ]
    parts: list[str] = []
    if queues:
        parts.append(f"Interconnection queues with rows here: {_join(_names(queues))}.")
    if "us.eia.860m" in loaded:
        parts.append("EIA-860M adds planned and operating US generators, unit by unit, in every state.")
    if withheld_iso:
        parts.append(
            f"The {_join(_names(withheld_iso))} queues are not covered: we hold no licence for, or have not "
            "confirmed the terms of, their data, so projects in those areas appear only as EIA-860M units."
        )
    if tenders:
        parts.append(f"Tenders and funding notices: {_join(_names(tenders))}.")
    return " ".join(parts) or FALLBACK


def uncovered_iso_notes(iso_param: str | None, data: Mapping[str, Any] | None) -> list[str]:
    """For a list or map filtered by `iso=`: one sentence per named ISO whose own queue has no rows
    here, saying that what the view shows in that ISO's area comes from EIA-860M (expert review
    2026-10-07: `/proposals?iso=PJM` listed 382 EIA-860M units with nothing saying so). Nothing
    when the API cannot say which sources have rows."""
    if not iso_param or not data:
        return []
    loaded = {str(i) for i in (data.get("sources") or {}).get("loaded_source_ids") or []}
    notes: list[str] = []
    for token in (t.strip() for t in iso_param.split(",")):
        if not token:
            continue
        queue_id = f"us.iso.{token.lower().replace('-', '')}.gen_queue"
        if queue_id in loaded or queue_id not in manifest_categories():
            continue
        name = ISO_QUEUE_NAMES.get(queue_id) or token
        notes.append(
            f"{name}'s own interconnection queue is not covered here. The proposals listed for {name} "
            f"come from EIA-860M, an inventory of planned and operating generators in its area, not "
            f"from the queue."
        )
    return notes
