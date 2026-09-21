"""Published definitions of the status vocabulary, derived from the code that does the mapping.

Why this is derived and not written
-----------------------------------
`LIFECYCLE_STATES` renders as a chip on every list row, map drawer and record page, and until
this module existed there was nowhere a reader could find out what any of those words meant. The
obvious fix — a prose page — rots: the mapping lives in `pipeline/status_map.yaml` and the
per-connector `pipeline/connectors/*/status_map.yaml` files, is revised as versioned data without
a code change, and a page describing it would be wrong the first time someone added a refine rule.

So the page is built in two halves that cannot disagree:

* **What the word means** is prose, from `data/vocabulary/lifecycle_states.yaml`, which carries
  the date it was written.
* **What maps into it** is read out of the status-map files themselves, every render. Every raw
  source value and every refine rule shown on the page is the same text
  `pipeline/normalize.py::harmonise_status` keys off at ingest.

`tests/test_lifecycle_definitions.py` closes the remaining gap: it fails when a state is in the
code vocabulary and not the definitions file, or the reverse, or when a status map produces a
canonical state neither knows about.

The four-state granularity claim
--------------------------------
`filed`, `studied`, `permitted`, `contracted` are four distinct pre-construction states where
Global Energy Monitor's trackers publish one (`pre-construction`). That is a real consequence of
working from interconnection queues, which publish study stage and agreement status as separate
columns, and it is worth nothing if nobody can see what the four words mean. `PRE_CONSTRUCTION`
below is the set the page makes that claim about; it is asserted against `LIFECYCLE_STATES` by
the same test, so the claim cannot outlive the states it names.

Source ids
----------
The status maps are keyed by a connector's `status_key` ("caiso", "eia860m"), not by its
`data/sources.yaml` id. The two are related by class attributes on the connector modules, which
this module reads *textually* rather than by importing thirteen connector modules (and, through
them, gridstatus and the HTTP stack) into the API process on a page render. A text scan can drift
from the classes it scans, so `tests/test_lifecycle_definitions.py` imports the real connector
classes and asserts the scan agrees with them.

Two status-map entries — `spp` and `isone` — have no connector at all. Their sources are withheld
pending licence clearance, so the mapping is written and produces no rows. They are reported with
`rows_published: false` rather than hidden, because a reader comparing our states to an ISO's own
vocabulary should be able to see the mapping exists.
"""

from __future__ import annotations

import functools
import pathlib
import re
from typing import Any

import yaml

from services.db.models import LIFECYCLE_STATES, OPPORTUNITY_STATUSES

_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFINITIONS_PATH = _ROOT / "data" / "vocabulary" / "lifecycle_states.yaml"
STATUS_MAP_PATHS = (_ROOT / "pipeline" / "status_map.yaml",)
CONNECTOR_ROOT = _ROOT / "pipeline" / "connectors"

#: The claim the published page makes about granularity (module docstring). Asserted against
#: `LIFECYCLE_STATES` by the drift test, so it cannot name a state that no longer exists.
PRE_CONSTRUCTION: tuple[str, ...] = ("filed", "studied", "permitted", "contracted")

_SOURCE_ID_RE = re.compile(r'^\s*source_id:\s*ClassVar\[str\]\s*=\s*"([^"]+)"', re.MULTILINE)
_STATUS_KEY_RE = re.compile(r'^\s*status_key:\s*ClassVar\[str\]\s*=\s*"([^"]+)"', re.MULTILINE)
_KIND_RE = re.compile(r'^\s*kind:\s*ClassVar\[Kind\]\s*=\s*"([^"]+)"', re.MULTILINE)


@functools.lru_cache(maxsize=1)
def connectors_by_status_key() -> dict[str, dict[str, str]]:
    """`{"caiso": {"source_id": ..., "kind": "proposal"}, ...}`, read as text from the connector
    modules (module docstring explains why text and not an import).

    A connector that declares no `status_key` does no status harmonisation and contributes
    nothing. Pinned against the real classes by the drift test."""
    out: dict[str, dict[str, str]] = {}
    for path in sorted(CONNECTOR_ROOT.glob("*/connector.py")):
        text = path.read_text(encoding="utf-8")
        source_id = _SOURCE_ID_RE.search(text)
        status_key = _STATUS_KEY_RE.search(text)
        kind = _KIND_RE.search(text)
        if source_id and status_key:
            out[status_key.group(1)] = {
                "source_id": source_id.group(1),
                "kind": kind.group(1) if kind else "proposal",
            }
    return out


def status_key_to_source_id() -> dict[str, str]:
    return {key: c["source_id"] for key, c in connectors_by_status_key().items()}


@functools.lru_cache(maxsize=1)
def status_map_paths() -> tuple[pathlib.Path, ...]:
    """Every status map the pipeline reads: the shared one plus each connector's own."""
    return (*STATUS_MAP_PATHS, *sorted(CONNECTOR_ROOT.glob("*/status_map.yaml")))


def _mapping_rows() -> list[dict[str, Any]]:
    """One row per raw value or refine rule in every status map, in file order.

    `rule` is `map`, `fallback_map` or the refine rule's own id — the same three things
    `harmonise_status` distinguishes — and `raw` is the text it keys off, verbatim."""
    connectors = connectors_by_status_key()
    rows: list[dict[str, Any]] = []
    for path in status_map_paths():
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # The shared `pipeline/status_map.yaml` holds the interconnection-queue and EIA vocabulary
        # (its own header says so); a connector-local map holds exactly its own connector's key.
        # So a key with no connector module — `spp`, `isone`, both withheld — is a proposal source.
        for status_key, config in (document.get("sources") or {}).items():
            if not isinstance(config, dict):
                continue
            connector = connectors.get(status_key)
            source_id = connector["source_id"] if connector else None
            common = {
                "status_key": status_key,
                "source_id": source_id,
                "kind": connector["kind"] if connector else "proposal",
                # A status map with no connector is a mapping written for a source whose rows are
                # withheld (module docstring). Shown, not hidden.
                "rows_published": source_id is not None,
                "field": config.get("field"),
            }
            for raw, state in (config.get("map") or {}).items():
                rows.append({**common, "state": state, "raw": str(raw), "rule": "map"})
            for raw, state in (config.get("fallback_map") or {}).items():
                rows.append({**common, "state": state, "raw": str(raw), "rule": "fallback_map"})
            for rule in config.get("refine") or []:
                if not isinstance(rule, dict):
                    continue
                conditions = rule.get("when") or {}
                rows.append(
                    {
                        **common,
                        "state": rule.get("then"),
                        "raw": ", ".join(f"{k} = {v}" for k, v in conditions.items()),
                        "rule": str(rule.get("id") or "refine"),
                        "note": (rule.get("note") or "").strip() or None,
                    }
                )
    return rows


def mapped_states() -> set[str]:
    """Every canonical state any status map can produce. The drift test compares this to the
    code vocabularies, so a map that starts emitting a word the database would reject fails the
    suite rather than the load."""
    return {str(row["state"]) for row in _mapping_rows() if row.get("state")}


@functools.lru_cache(maxsize=1)
def _definitions_document() -> dict[str, Any]:
    return yaml.safe_load(DEFINITIONS_PATH.read_text(encoding="utf-8")) or {}


def _states(
    vocabulary: tuple[str, ...],
    definitions: dict[str, Any],
    rows: list[dict[str, Any]],
    kind: str,
) -> list[dict[str, Any]]:
    """`kind` selects which half of the maps answers for this vocabulary. Four words — `unknown`,
    `announced`, `cancelled`, `withdrawn` — exist in both vocabularies with different meanings, so
    listing every mapping under both would show a reader of the proposal vocabulary that a TED
    prior-information notice makes a project `announced`, which it does not."""
    out: list[dict[str, Any]] = []
    for state in vocabulary:
        entry = definitions.get(state) or {}
        mappings = [r for r in rows if r.get("state") == state and r.get("kind") == kind]
        out.append(
            {
                "state": state,
                "definition": (entry.get("definition") or "").strip(),
                "excludes": (entry.get("excludes") or "").strip() or None,
                "uncertainty": (entry.get("uncertainty") or "").strip() or None,
                # Derived, every render, from the files the pipeline itself keys off.
                "maps_from": [
                    {
                        "source_id": m["source_id"],
                        "status_key": m["status_key"],
                        "field": m["field"],
                        "raw": m["raw"],
                        "rule": m["rule"],
                        "note": m.get("note"),
                        "rows_published": m["rows_published"],
                    }
                    for m in mappings
                ],
                # `unknown` is reached by falling through every map rather than by a rule, so it
                # has no `maps_from` and saying "no source maps here" would be wrong.
                "is_fallback": state == "unknown",
            }
        )
    return out


def vocabulary() -> dict[str, Any]:
    """The whole published vocabulary: prose plus the mappings derived from the status maps."""
    document = _definitions_document()
    rows = _mapping_rows()
    return {
        "definitions_version": document.get("version"),
        # The prose half is written, not derived, so the page says when (task: "where it must be
        # prose, say when it was written").
        "definitions_written": str(document.get("written") or ""),
        "lifecycle_states": _states(
            LIFECYCLE_STATES, document.get("lifecycle_states") or {}, rows, "proposal"
        ),
        "opportunity_statuses": _states(
            OPPORTUNITY_STATUSES, document.get("opportunity_statuses") or {}, rows, "opportunity"
        ),
        "pre_construction_states": list(PRE_CONSTRUCTION),
        "status_map_files": [str(p.relative_to(_ROOT)) for p in status_map_paths()],
    }
