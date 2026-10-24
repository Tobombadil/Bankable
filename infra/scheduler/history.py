"""The history connectors' schedule and job body (`docs/24-trends.md` §9.5; lane trends-history, 2026-10-23).

History sources (`runner: history` in `data/sources.yaml`) are not queued by the generic bucket
ticks (`app.py::due_sources` skips them): their runner reads a month per document, not records, so
each source gets its own weekly tick, here. Pure data and plain functions, importable without
Postgres or Procrastinate (`app.py` registers them, as it does `prices.py`'s).

* EIA-860M: Thursdays 06:28 UTC. EIA releases a month in the last week of the next one (September
  2026's on 23 October, October's due 24 November, the index page says); a weekly tick finds it
  within seven days, and costs the index page plus a conditional request (a 304) otherwise.
* ERCOT GIS: Tuesdays 06:49 UTC. ERCOT publishes on the 1st or 2nd of the month at about 21:00 UTC;
  an unchanged week costs the 80 KB document list.

Every run is `--mode auto`: the backfill until one has ended `ok` or `unchanged` with the same
`backfill_key` (`pipeline/connectors/history/runner.py::effective_mode`: EIA's mirror record, so
switching the mirror on in `data/sources.yaml` makes the next tick a backfill), then the month's
update. A backfill is
resumable, so a failed or killed one simply continues at the next tick; an item that failed three
runs running is skipped until an operator passes `--retry-failed`. The outcome is recorded as a
`source_run` row and the source's health through `jobs.fetch_outcome`, the function every
connector's runs go through, so a broken archive alerts as a broken queue does. A run holds the
execution lock of the register source whose stored snapshots it reads (`HistoryTick.reads_from`):
the register's fetch and load wait for it, and it for them.

Minutes 28 and 49 are used by no other tick (`test_history.py` checks).
"""

from __future__ import annotations

import datetime as dt
import logging
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("infra.scheduler.history")

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class HistoryTick:
    source_id: str
    cron: str
    #: The register source whose stored snapshots this history source reads (`shared` route). The
    #: run holds that source's execution lock, so it never reads a snapshot the register's fetch is
    #: writing, and the two never ask the same host at once (ERCOT: 0.5 rps in all, docs/13 §1.1).
    reads_from: str

    @property
    def periodic_id(self) -> str:
        return f"tick:history:{self.source_id}"


HISTORY_TICKS: tuple[HistoryTick, ...] = (
    HistoryTick("us.eia.860m.history", "28 6 * * 4", reads_from="us.eia.860m"),
    HistoryTick("us.iso.ercot.gen_queue.history", "49 6 * * 2", reads_from="us.iso.ercot.gen_queue"),
)
#: A backfill reads EIA's 1.1 GB of yearly zips and parses 135 workbooks (measured 2026-10-23,
#: docs/24-trends.md §9.6); a month's update is one workbook.
TIMEOUT_S = 7200


def is_history_source(source: Mapping[str, Any]) -> bool:
    """A manifest row the history runner serves (`runner: history`); the generic ticks skip it."""
    return str(source.get("runner") or "") == "history"


def history_command(source_id: str, trigger: str, run_id: str | None = None) -> list[str]:
    cmd = [sys.executable, "-m", "pipeline.connectors.history", "run", source_id, "--mode", "auto"]
    cmd += ["--trigger", trigger]
    if run_id:
        cmd += ["--run-id", run_id]
    return cmd


def history_fetch_job(
    source_id: str,
    *,
    trigger: str = "schedule",
    attempt: int = 1,
    final_attempt: bool = False,
    _run: Callable[..., Any] = subprocess.run,
    _outcome: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Body of `history_fetch`: the history CLI as its own process (as `run_connector` runs the
    record CLI), its outcome recorded through `jobs.fetch_outcome`."""
    from infra.scheduler import jobs

    started = dt.datetime.now(dt.UTC)
    cmd = history_command(source_id, trigger)
    outcome_fn = _outcome or jobs.fetch_outcome
    try:
        result = _run(cmd, cwd=ROOT, timeout=TIMEOUT_S, capture_output=True, text=True, check=False)
    except subprocess.TimeoutExpired as exc:
        return outcome_fn(
            source_id,
            returncode=None,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            trigger=trigger,
            started_at=started,
            timeout_s=TIMEOUT_S,
            attempt=attempt,
            final_attempt=final_attempt,
        )
    out = outcome_fn(
        source_id,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        trigger=trigger,
        started_at=started,
        attempt=attempt,
        final_attempt=final_attempt,
    )
    logger.info(
        "history fetch %s -> %s",
        source_id,
        out.get("status"),
        extra={"source_id": source_id, "status": out.get("status")},
    )
    return out


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


__all__ = [
    "HISTORY_TICKS",
    "TIMEOUT_S",
    "HistoryTick",
    "history_command",
    "history_fetch_job",
    "is_history_source",
]
