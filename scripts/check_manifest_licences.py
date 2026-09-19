#!/usr/bin/env python3
"""Fail when `data/sources.yaml` is more permissive than the legal register (docs/13 §6).

The 2026-09-18 audit (docs/50 §3.1, "Licence and privacy") found fourteen sources the register
classifies `unknown` recorded `reuse: attribution` in the manifest, which is what the loader and
the API gate on -- so their rows were publishable raw on the free tier with no terms ever read.
This check makes that drift a failing test (`tests/test_manifest_licences.py` runs it under the
ordinary pytest job) instead of something a reviewer has to notice.

What it parses
  * docs/13-legal-data-rights.md §6, "Per-source publication matrix": every table row directly
    under the `## 6.` heading (a `###` subsection ends the matrix) whose first cell is one or more
    backticked `source_id`s. The Class cell is scanned for the register's own
    vocabulary (`public-domain`, `open`, `open-attribution`, `permissive`, `attribution-restricted`,
    `restricted`, `unknown`); when a cell names several (PJM: "restricted (DM2) /
    attribution-restricted (planning pages)") the strictest wins. The Publication-rule cell is
    scanned the same way for `raw-ok`, `derived-only`, `link-out-only`, `paid-api-only` and the
    "do not ingest/store/publish" phrasings; the strictest wins there too.
  * data/sources.yaml: `reuse` (open | attribution | restricted | unknown) and `publication`
    (raw_ok | derived_only | none), the explicit per-source field that replaces the loader's
    free-text "derived-only" regex (docs/21 §8). `publication` defaults per reuse class when
    absent (`default_publication`), and the check requires it to be written out anyway.

Rules (each violation is one line of output; exit 1 if any)
  R1  every manifest source that carries `reuse` must have a row in the register;
  R2  manifest `reuse` may not rank above what the register class allows
      (public-domain/open/permissive -> open; open-attribution/attribution-restricted ->
      attribution; restricted -> restricted; unknown -> unknown; `restricted` and `unknown` rank
      equal because the loader and the API gate both the same way);
  R3  `publication` must be in vocabulary, and may not rank above the register's rule
      (raw-ok > derived-only > link-out-only/do-not-ingest = none);
  R4  a `restricted`/`unknown` source must be `publication: none`; an `attribution-restricted`
      register class must not be `raw_ok`.

Usage:  python scripts/check_manifest_licences.py [--manifest PATH] [--register PATH] [--quiet]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "sources.yaml"
REGISTER = ROOT / "docs" / "13-legal-data-rights.md"

REUSE_VOCAB = ("open", "attribution", "restricted", "unknown")
PUBLICATION_VOCAB = ("raw_ok", "derived_only", "none")

#: Manifest `reuse` rank: higher is more permissive. `restricted` and `unknown` are the same gate
#: (services/ingest/loader.py `GATED_REUSE`; services/api/visibility.py
#: `PUBLISHABLE_REUSE_CLASSES`), so neither is "more permissive" than the other.
_REUSE_RANK = {"open": 3, "attribution": 2, "restricted": 1, "unknown": 1}
#: Register class -> the most permissive manifest `reuse` it supports.
_REGISTER_CLASS_MAX_REUSE = {
    "public-domain": "open",
    "open": "open",
    "permissive": "open",
    "open-attribution": "attribution",
    "attribution-restricted": "attribution",
    "restricted": "restricted",
    "unknown": "unknown",
}
#: Register class strictness for "strictest wins" over a mixed cell (lower is stricter).
_REGISTER_CLASS_ORDER = {
    "unknown": 0,
    "restricted": 1,
    "attribution-restricted": 2,
    "open-attribution": 3,
    "permissive": 4,
    "public-domain": 5,
    "open": 5,
}
_CLASS_RE = re.compile(
    r"\b(public-domain|open-attribution|attribution-restricted|permissive|restricted|unknown|open)\b"
)

_PUBLICATION_RANK = {"raw_ok": 2, "derived_only": 1, "none": 0}
#: Register publication-rule phrasings -> manifest `publication` value (strictest wins).
_RULE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"do not (ingest|store|publish)", re.I), "none"),
    (re.compile(r"link-out-only", re.I), "none"),
    (re.compile(r"paid-api-only", re.I), "none"),
    (re.compile(r"derived-only", re.I), "derived_only"),
    (re.compile(r"raw-ok", re.I), "raw_ok"),
)
_ROW_ID_RE = re.compile(r"`([^`]+)`")


def default_publication(reuse: str | None) -> str:
    """The `publication` a source gets when the manifest does not say (docs/21 §8's rows:
    `open`/`attribution` are raw-ok unless the source's own terms withhold raw; `restricted`/
    `unknown` publish nothing)."""
    return "raw_ok" if reuse in ("open", "attribution") else "none"


@dataclass(frozen=True)
class RegisterRow:
    source_ids: tuple[str, ...]
    class_text: str
    rule_text: str
    line_no: int

    @property
    def strictest_class(self) -> str | None:
        found: list[str] = _CLASS_RE.findall(self.class_text)
        if not found:
            return None
        return min(found, key=lambda c: _REGISTER_CLASS_ORDER[c])

    @property
    def strictest_rule(self) -> str | None:
        hits = [value for pattern, value in _RULE_PATTERNS if pattern.search(self.rule_text)]
        if not hits:
            return None
        return min(hits, key=lambda v: _PUBLICATION_RANK[v])


def parse_register(path: pathlib.Path = REGISTER) -> dict[str, RegisterRow]:
    """Every §6 matrix row keyed by each `source_id` it names. Rows outside §6 (the classification
    scheme table, the personal-data table) are skipped by section, not by shape."""
    rows: dict[str, RegisterRow] = {}
    in_matrix = False
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith("#"):
            # Only the rows directly under `## 6.`: a `### 6.x` subsection (the 2026-09-18
            # reconciliation table, which repeats ids with their *old* class) ends the matrix.
            in_matrix = line.startswith("## 6.")
            continue
        if not in_matrix or not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        ids = tuple(_ROW_ID_RE.findall(cells[0]))
        if not ids or ids == ("source_id",):
            continue
        row = RegisterRow(source_ids=ids, class_text=cells[1], rule_text=cells[2], line_no=line_no)
        for sid in ids:
            rows[sid] = row
    return rows


def load_manifest(path: pathlib.Path = MANIFEST) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return list(doc["sources"])


def check(manifest_path: pathlib.Path = MANIFEST, register_path: pathlib.Path = REGISTER) -> list[str]:
    """Every violation as one `source_id: ...` line; empty means the manifest is no more permissive
    than the register on any source."""
    register = parse_register(register_path)
    problems: list[str] = []
    for entry in load_manifest(manifest_path):
        sid = str(entry["id"])
        reuse = entry.get("reuse")
        if reuse is None:
            # Outbound social channels carry no `reuse`; the register lists them as "n/a
            # (outbound)". Nothing is ingested from them, so nothing to gate.
            continue
        if reuse not in REUSE_VOCAB:
            problems.append(f"{sid}: reuse={reuse!r} is not in {REUSE_VOCAB}")
            continue
        publication = entry.get("publication")
        if publication is None:
            problems.append(
                f"{sid}: no `publication` field (would default to {default_publication(reuse)!r}); "
                "write it explicitly"
            )
            publication = default_publication(reuse)
        elif publication not in PUBLICATION_VOCAB:
            problems.append(f"{sid}: publication={publication!r} is not in {PUBLICATION_VOCAB}")
            continue
        if reuse in ("restricted", "unknown") and publication != "none":
            problems.append(f"{sid}: reuse={reuse!r} must be publication: none, not {publication!r} (R4)")

        row = register.get(sid)
        if row is None:
            problems.append(f"{sid}: not in the docs/13 §6 register matrix (R1)")
            continue
        register_class = row.strictest_class
        if register_class is None:
            problems.append(
                f"{sid}: register class cell {row.class_text!r} (docs/13 line {row.line_no}) names no "
                "known class"
            )
            continue
        max_reuse = _REGISTER_CLASS_MAX_REUSE[register_class]
        if _REUSE_RANK[reuse] > _REUSE_RANK[max_reuse]:
            problems.append(
                f"{sid}: manifest reuse={reuse!r} but register class is {register_class!r} "
                f"(docs/13 line {row.line_no}: {row.class_text!r}); at most {max_reuse!r} (R2)"
            )
        if register_class == "attribution-restricted" and publication == "raw_ok":
            problems.append(
                f"{sid}: register class 'attribution-restricted' (docs/13 line {row.line_no}) "
                "forbids publication: raw_ok (R4)"
            )
        rule = row.strictest_rule
        if rule is not None and _PUBLICATION_RANK[publication] > _PUBLICATION_RANK[rule]:
            problems.append(
                f"{sid}: manifest publication={publication!r} but register rule is {rule!r} "
                f"(docs/13 line {row.line_no}: {row.rule_text!r}) (R3)"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--manifest", type=pathlib.Path, default=MANIFEST)
    parser.add_argument("--register", type=pathlib.Path, default=REGISTER)
    parser.add_argument("--quiet", action="store_true", help="print only the verdict line")
    args = parser.parse_args(argv)
    problems = check(args.manifest, args.register)
    out = sys.stdout
    if not args.quiet:
        for line in problems:
            out.write(line + "\n")
    entries = [e for e in load_manifest(args.manifest) if e.get("reuse") is not None]
    if problems:
        out.write(
            f"RESULT: FAIL — {len(problems)} violation(s) across {len(entries)} gated-vocabulary sources\n"
        )
        return 1
    out.write(f"RESULT: PASS — {len(entries)} sources no more permissive than docs/13 §6\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
