"""Data-quality checks on a month's document, against the previous month's (`docs/24-trends.md` §9.4).

The record connectors' gates (`pipeline/connectors/dq.py`) judge rows; these judge a month's
tallies, which is what a history document holds. Each check compares like with like: a block is
compared with the previous month's block only when both have the same `basis`.

| Check | Holds | Warns |
|---|---|---|
| `empty_block` | a block counted no row | |
| `row_drift` | rows moved by more than `HOLD_DRIFT` against the previous month | more than `WARN_DRIFT` |
| `null_spike` | | the share of rows without MW rose by more than `NULL_SPIKE_PP` points |
| `duplicate_keys` | | a key (EIA plant and generator, ERCOT INR) appears twice in one sheet |
| `status_vocabulary` | | a status code outside the source's known vocabulary |
| `code_vocabulary` | | a fuel or technology code outside the publisher's own list (ERCOT's Acronyms sheet) |
| `unmapped_technology` | | a technology label the classifier reads as `unknown` |
| `technology_blank` | | a block's rows with no technology carry `BLANK_MW_WARN` MW or more |
| `period_mismatch` | the month is the name's, the file's own date disputes it (ERCOT) | the reverse (EIA) |
| `duplicate_file` | the same bytes are already another month's served document | |
| `cumulative_shrank` | | a list that only grows (EIA's Retired sheet since March 2017) lost rows |
| `cumulative_restated` | | its settled years (before last year) moved over `RESTATED_SHARE` of its MW |

(`row_drift` and `null_spike` only for a block the previous month counted `MIN_ROWS` rows or more.)

A held month is written with `dq.status: held`; readers skip it until an operator releases it
(`python -m pipeline.connectors.history release <source_id> <YYYY-MM>`), so a broken parse never
reaches a chart, and the last good months stay served: a held reading of a month that already has a
served document is kept beside it (`documents.MonthlyStore.write_pending`), never over it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pipeline.connectors.history.base import BLANK_LABEL

WARN_DRIFT = 0.20
HOLD_DRIFT = 0.50
NULL_SPIKE_PP = 5.0
#: Below this many rows a month-on-month share says nothing (ERCOT's small-generator sheet went from
#: 4 to 10 rows in March 2022): drift and null spikes are judged only from this size up.
MIN_ROWS = 50
#: A cumulative list's settled years moving by more than this share of its MW in one month is a
#: restatement of the list, not a month of retirements. Measured on EIA's Retired sheet, 2017-03 to
#: 2026-09 (2026-10-24): the settled years move by under 0.7 % in every month but four, 2024-11 and
#: 2024-12 (277 generators, 5.2 GW, left out and put back) and 2026-08 and 2026-09 (25 generators,
#: 9.4 GW, mostly old nuclear, added and taken out again), each 2 % or more.
RESTATED_SHARE = 0.01
#: Rows a block lists with no technology land in `unknown`. EIA's sheets carry one to nine such rows
#: in 76 of 135 months, almost all small; a line by technology moves visibly only when they carry
#: real capacity: 100 MW or more in four months (2016-12 Planned 1,072 MW, 2023-05 Operating 250 MW,
#: 2023-12 Planned 100.3 MW, 2026-09 Planned 500 MW), measured 2026-10-24.
BLANK_MW_WARN = 100.0


def _rows(document: Mapping[str, Any], block: str) -> int | None:
    """The rows a block counted (its total), in a summary or a stored document alike."""
    total = ((document.get("blocks") or {}).get(block) or {}).get("total") or {}
    value = total.get("count")
    return int(value) if isinstance(value, int) else None


def _missing(document: Mapping[str, Any], block: str) -> int:
    facts = document.get("dq_facts") or document.get("dq", {}).get("facts") or {}
    value = (facts.get("capacity_missing") or {}).get(block)
    return int(value) if isinstance(value, int) else 0


def _cumulative_checks(
    name: str, rule: Mapping[str, Any], block: Mapping[str, Any], before: Mapping[str, Any], period: Any
) -> list[dict[str, Any]]:
    """`cumulative_shrank` and `cumulative_restated` for a block its connector declares cumulative
    (`dq_facts.cumulative`: the dimension that dates a row, and the first year still open)."""
    out: list[dict[str, Any]] = []
    now_total, then_total = block.get("total") or {}, before.get("total") or {}
    rows, prior = now_total.get("count"), then_total.get("count")
    if isinstance(rows, int) and isinstance(prior, int) and rows < prior:
        out.append(
            {
                "check": "cumulative_shrank",
                "block": name,
                "level": "warn",
                "detail": f"{prior} -> {rows} rows against {period}; a list that only grows lost rows",
            }
        )
    dimension = f"by_{rule.get('dimension')}"
    open_from = str(rule.get("open_from") or "")
    now_by, then_by = block.get(dimension) or {}, before.get(dimension) or {}
    moved_mw = 0.0
    moved_rows = 0
    for key in set(now_by) | set(then_by):
        if key.startswith("_") or not open_from or key >= open_from:
            continue
        a, b = now_by.get(key) or {}, then_by.get(key) or {}
        moved_mw += abs(float(a.get("mw") or 0.0) - float(b.get("mw") or 0.0))
        moved_rows += abs(int(a.get("count") or 0) - int(b.get("count") or 0))
    base = then_total.get("mw")
    if isinstance(base, (int, float)) and base > 0 and moved_mw > RESTATED_SHARE * base:
        out.append(
            {
                "check": "cumulative_restated",
                "block": name,
                "level": "warn",
                "detail": (
                    f"years before {open_from} moved {moved_mw:,.1f} MW over {moved_rows} rows against "
                    f"{period} ({moved_mw / base:.1%} of the list)"
                ),
            }
        )
    return out


def check_month(summary: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    """The `dq` block of a month's document. `summary` is the connector's `summarise` output (the
    runner adds `duplicate_file`); `previous` the stored document of the month before, or None."""
    checks: list[dict[str, Any]] = []
    facts = summary.get("dq_facts") or {}
    blocks = summary.get("blocks") or {}
    previous_blocks = (previous or {}).get("blocks") or {}
    cumulative = facts.get("cumulative") or {}
    for name, block in sorted(blocks.items()):
        rows = _rows(summary, name) or 0
        if rows == 0:
            checks.append(
                {"check": "empty_block", "block": name, "level": "hold", "detail": "no row counted"}
            )
            continue
        before = previous_blocks.get(name)
        if previous is None or not isinstance(before, Mapping) or before.get("basis") != block.get("basis"):
            continue
        if isinstance(cumulative.get(name), Mapping):
            checks.extend(_cumulative_checks(name, cumulative[name], block, before, previous.get("period")))
        prior = _rows(previous, name)
        if prior and prior >= MIN_ROWS:
            drift = abs(rows - prior) / prior
            level = "hold" if drift > HOLD_DRIFT else "warn" if drift > WARN_DRIFT else None
            if level:
                checks.append(
                    {
                        "check": "row_drift",
                        "block": name,
                        "level": level,
                        "detail": f"{prior} -> {rows} rows ({drift:.0%}) against {previous.get('period')}",
                    }
                )
            share_now = 100.0 * _missing(summary, name) / rows
            share_before = 100.0 * _missing(previous, name) / prior
            if share_now - share_before > NULL_SPIKE_PP:
                checks.append(
                    {
                        "check": "null_spike",
                        "block": name,
                        "level": "warn",
                        "detail": f"rows without MW {share_before:.1f}% -> {share_now:.1f}%",
                    }
                )
    for name, count in sorted((facts.get("duplicate_keys") or {}).items()):
        if count:
            checks.append(
                {
                    "check": "duplicate_keys",
                    "block": name,
                    "level": "warn",
                    "detail": f"{count} repeated keys",
                }
            )
    for name, codes in sorted((facts.get("unknown_status") or {}).items()):
        if codes:
            checks.append(
                {
                    "check": "status_vocabulary",
                    "block": name,
                    "level": "warn",
                    "detail": f"unknown codes {codes}",
                }
            )
    if facts.get("unlisted_codes"):
        checks.append(
            {
                "check": "code_vocabulary",
                "block": None,
                "level": "warn",
                "detail": f"codes the publisher's list does not hold, with rows: {facts['unlisted_codes']}",
            }
        )
    labels = list(facts.get("unmapped_technology") or [])
    unread = [label for label in labels if label != BLANK_LABEL]
    if unread:
        checks.append(
            {
                "check": "unmapped_technology",
                "block": None,
                "level": "warn",
                "detail": f"labels read as unknown: {unread[:10]}",
            }
        )
    blank_mw = facts.get("technology_blank_mw")
    if isinstance(blank_mw, Mapping):
        blank_rows: Mapping[str, Any] = facts.get("technology_blank") or {}
        for name, mw in sorted(blank_mw.items()):
            if isinstance(mw, (int, float)) and mw >= BLANK_MW_WARN:
                checks.append(
                    {
                        "check": "technology_blank",
                        "block": name,
                        "level": "warn",
                        "detail": f"{blank_rows.get(name)} rows with no technology, {mw:,.1f} MW",
                    }
                )
    elif BLANK_LABEL in labels:
        # A connector that does not measure its blank rows' MW: every blank row is worth a look.
        checks.append(
            {"check": "technology_blank", "block": None, "level": "warn", "detail": "rows with no technology"}
        )
    mismatch = facts.get("period_hint_mismatch")
    if mismatch:
        from_name = isinstance(mismatch, Mapping) and mismatch.get("period_from") == "name"
        checks.append(
            {
                "check": "period_mismatch",
                "block": None,
                # Filed under its name's month, which its own date disputes: possibly another month's
                # report, so it is never served on its own (ERCOT). Filed under its own date, the name
                # alone is wrong, and the month is right (EIA).
                "level": "hold" if from_name else "warn",
                "detail": str(mismatch),
            }
        )
    if facts.get("duplicate_file"):
        checks.append(
            {
                "check": "duplicate_file",
                "block": None,
                "level": "hold",
                "detail": f"the same file (sha256) is the served document of {facts['duplicate_file']}",
            }
        )
    levels = {c["level"] for c in checks}
    status = "held" if "hold" in levels else "warn" if "warn" in levels else "pass"
    return {
        "status": status,
        "checks": checks,
        "compared_with": (previous or {}).get("period"),
        "facts": dict(facts),
    }


__all__ = [
    "BLANK_MW_WARN",
    "HOLD_DRIFT",
    "MIN_ROWS",
    "NULL_SPIKE_PP",
    "RESTATED_SHARE",
    "WARN_DRIFT",
    "check_month",
]
