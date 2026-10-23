"""The price connectors' schedule and job bodies (docs/29 §7.3; lane price-hubs, 2026-10-22).

Price sources (`runner: prices` in `data/sources.yaml`) are not queued by the generic bucket ticks
(`app.py::due_sources` skips them): their runner, their modes and their politeness differ, so each
source gets the ticks its terms allow, here. Pure data and plain functions, importable without
Postgres or Procrastinate (`app.py` registers them, as it does `jobs.py`'s).

* ERCOT: latest every 15 minutes, daily 19:17 UTC. Raw-ok with no delay; one 55 KB page asked
  with its validators (a 304 when unchanged); the next day's day-ahead is posted about 12:30 Central.
* SPP: latest hourly, daily 22:27 UTC. The Acceptable Use Policy's "basic reconciliation": one
  2.5 KB readout an hour, one 24 KB day-ahead read a day after SPP posts it.
* CAISO: daily 21:32 UTC. Derived-only (no readout); day-ahead posted about 13:00 Pacific.
* NYISO: daily 17:39 UTC. Derived-only; day-ahead posted about 11:00 Eastern.
* Elexon: latest hourly, daily 02:53 UTC. The BMRS API terms bar asking more than once a half
  hour for half-hourly data.
* EIA: Mondays 08:12 UTC. One file a month (released about the 24th), asked with
  `If-Modified-Since`.

Every minute used here is used by no other tick (`cadence.CRON_BY_BUCKET`, the `@app.periodic`
jobs in app.py, `queue_maintenance`), so no two ticks fire in the same minute.
A source whose store holds no backfill yet runs `backfill` instead of `daily` the first time, so
a fresh deployment fills itself without an operator step (ERCOT's four annual workbooks, about
30 MB; Elexon's 366 days; CAISO's 24 requests, NYISO's 26). The statistics are recomputed
once a day after the last daily fetch (`PRICE_STATS_CRON`).

**A failed backfill is not started again on its own (review 2026-10-23).** The escalation counts
backfill *attempts* (the marker the runner writes before it fetches, so a run killed before its
run record still counts, `pipeline/connectors/prices/store.py`), not successes: a source gets
`AUTO_BACKFILL_ATTEMPTS` automatic tries (ERCOT one, since each try may spend one of a workbook's
three downloads a year; the others three, one per daily tick), and after that its daily tick
runs `daily` and the failure stays on `source.health` until an operator runs
`python -m pipeline.connectors.prices run <source> --mode backfill`. A Procrastinate retry of a
backfill that crashed therefore runs `daily`, never the backfill again.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy.exc

logger = logging.getLogger("infra.scheduler.prices")

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class PriceTick:
    source_id: str
    mode: str  # latest | daily (a daily tick runs a backfill until one has succeeded)
    cron: str

    @property
    def periodic_id(self) -> str:
        return f"tick:price:{self.source_id}:{self.mode}"


PRICE_TICKS: tuple[PriceTick, ...] = (
    PriceTick("us.iso.ercot.prices", "latest", "5,20,35,50 * * * *"),
    PriceTick("us.iso.ercot.prices", "daily", "17 19 * * *"),
    PriceTick("us.iso.spp.prices", "latest", "9 * * * *"),
    PriceTick("us.iso.spp.prices", "daily", "27 22 * * *"),
    PriceTick("us.iso.caiso.oasis_prices", "daily", "32 21 * * *"),
    PriceTick("us.iso.nyiso.prices", "daily", "39 17 * * *"),
    PriceTick("gb.elexon.bmrs_prices", "latest", "42 * * * *"),
    PriceTick("gb.elexon.bmrs_prices", "daily", "53 2 * * *"),
    PriceTick("us.eia.retail_sales", "daily", "12 8 * * 1"),
)
#: After the last daily fetch (SPP 22:27, CAISO 21:32) and their loads.
PRICE_STATS_CRON = "57 23 * * *"
#: Per mode. A backfill parses ERCOT's annual workbooks (about four minutes each for the two
#: real-time years, measured 2026-10-22) or asks Elexon 366 times at 0.5 a second.
TIMEOUT_S: dict[str, int] = {"latest": 300, "daily": 900, "backfill": 3600}
LOAD_TIMEOUT_S = 1800
STATS_TIMEOUT_S = 900
#: `services.prices.load --run`'s exit code for a retryable database conflict (that module's
#: `EXIT_RETRYABLE`; repeated here so this module imports nothing heavy).
EXIT_RETRYABLE = 75


#: Automatic backfill tries per source before its daily tick stops escalating (module docstring).
#: ERCOT: one, because a backfill downloads capped workbooks ("no more than three times within a
#: 12-month period"); a second try is an operator's decision.
AUTO_BACKFILL_ATTEMPTS: dict[str, int] = {"us.iso.ercot.prices": 1}
DEFAULT_AUTO_BACKFILL_ATTEMPTS = 3


def needs_backfill(runs: Sequence[Mapping[str, Any]]) -> bool:
    """True until the store holds a backfill run that ended `ok` or `unchanged`."""
    return not any(r.get("mode") == "backfill" and r.get("status") in ("ok", "unchanged") for r in runs)


def _attempts_in_store(source_id: str) -> set[str]:
    from infra.scheduler import jobs
    from pipeline.connectors.prices import store as pstore
    from pipeline.connectors.store import open_store

    return pstore.backfill_attempts(open_store(jobs.connector_data_root()), source_id)


def effective_mode(
    source_id: str,
    mode: str,
    *,
    runs_of: Callable[[str], Sequence[Mapping[str, Any]]],
    attempts_of: Callable[[str], set[str]] | None = None,
) -> str:
    """The mode a tick runs: a `daily` tick runs `backfill` for a source never backfilled, while it
    has automatic tries left (`AUTO_BACKFILL_ATTEMPTS`). A `latest` tick never escalates (a
    readout must not wait on a year's history)."""
    if mode != "daily":
        return mode
    try:
        if not needs_backfill(runs_of(source_id)):
            return mode
        tried = len((attempts_of or _attempts_in_store)(source_id))
    except Exception:  # an unreadable store must not stop the daily fetch
        logger.warning("price runs unreadable for %s; running daily", source_id, exc_info=True)
        return mode
    allowed = AUTO_BACKFILL_ATTEMPTS.get(source_id, DEFAULT_AUTO_BACKFILL_ATTEMPTS)
    if tried >= allowed:
        logger.warning(
            "price backfill for %s tried %d time(s) without success; running daily (an operator "
            "runs the next backfill)",
            source_id,
            tried,
            extra={"source_id": source_id, "backfill_attempts": tried},
        )
        return mode
    return "backfill"


def price_command(source_id: str, mode: str, trigger: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pipeline.connectors.prices",
        "run",
        source_id,
        "--mode",
        mode,
        "--trigger",
        trigger,
    ]


def price_fetch_job(
    source_id: str,
    mode: str,
    *,
    trigger: str = "schedule",
    attempt: int = 1,
    final_attempt: bool = False,
    _run: Callable[..., Any] = subprocess.run,
    _runs_of: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
    _attempts_of: Callable[[str], set[str]] | None = None,
    _outcome: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Body of `price_fetch`: the price CLI as its own process (as `run_connector` runs the record
    CLI), its outcome recorded as a `source_run` row and the source's health through the same
    function the record connectors use (`jobs.fetch_outcome`), so a failing price source alerts
    exactly as a failing queue does (freshness, `consecutive_failures`, dead letters)."""
    from infra.scheduler import jobs

    runs_of: Callable[[str], Sequence[Mapping[str, Any]]]
    if _runs_of is None:
        from pipeline.connectors.store import open_store

        runs_of = open_store(jobs.connector_data_root()).runs
    else:
        runs_of = _runs_of
    actual = effective_mode(source_id, mode, runs_of=runs_of, attempts_of=_attempts_of)
    timeout = TIMEOUT_S[actual]
    started = dt.datetime.now(dt.UTC)
    run_trigger = trigger if actual == mode else "backfill"
    cmd = price_command(source_id, actual, run_trigger)
    outcome_fn = _outcome or jobs.fetch_outcome
    try:
        result = _run(cmd, cwd=ROOT, timeout=timeout, capture_output=True, text=True, check=False)
    except subprocess.TimeoutExpired as exc:
        out = outcome_fn(
            source_id,
            returncode=None,
            stdout=_text(exc.stdout),
            stderr=_text(exc.stderr),
            trigger=run_trigger,
            started_at=started,
            timeout_s=timeout,
            attempt=attempt,
            final_attempt=final_attempt,
        )
        return {**out, "mode": actual}
    out = outcome_fn(
        source_id,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        trigger=run_trigger,
        started_at=started,
        attempt=attempt,
        final_attempt=final_attempt,
    )
    logger.info(
        "price fetch %s %s -> %s",
        source_id,
        actual,
        out.get("status"),
        extra={"source_id": source_id, "mode": actual, "status": out.get("status")},
    )
    return {**out, "mode": actual}


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


class PriceLoadConflict(sqlalchemy.exc.OperationalError):
    """The load process ended on a database conflict a fresh transaction can clear (a deadlock, a
    unique violation from a concurrent load): `LOAD_RETRY` retries `OperationalError`."""


class PriceLoadFailed(RuntimeError):
    """The load process failed (not retried; the next run's load covers its months again)."""


def load_command(source_id: str, ts: str) -> list[str]:
    return [sys.executable, "-m", "services.prices.load", source_id, "--run", ts]


def price_load_job(source_id: str, ts: str, *, _run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Body of `price_load`: `python -m services.prices.load <source> --run <ts>` as its own process
    (review 2026-10-23). In the worker process, the memory a load uses stays with the worker
    afterwards, and an out-of-memory kill takes the worker and every queue it serves; as a
    process, it is the operating system's again when the load ends, and a kill takes the load. The
    worker's session limits reach it as `PGOPTIONS` (`jobs.worker_connect_args`)."""
    import os

    from infra.scheduler import jobs

    env = dict(os.environ)
    options = jobs.worker_connect_args(env.get("DATABASE_URL")).get("options")
    if options:
        env["PGOPTIONS"] = f"{env['PGOPTIONS']} {options}" if env.get("PGOPTIONS") else str(options)
    env.setdefault("INFRAQUE_DATA_DIR", str(jobs.connector_data_root()))
    try:
        result = _run(
            load_command(source_id, ts),
            cwd=ROOT,
            env=env,
            timeout=LOAD_TIMEOUT_S,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PriceLoadFailed(f"price load {source_id} {ts} timed out after {LOAD_TIMEOUT_S} s") from exc
    lines = [line for line in (result.stdout or "").splitlines() if line.startswith("{")]
    report: dict[str, Any] = json.loads(lines[-1]) if lines else {}
    if result.returncode == EXIT_RETRYABLE:
        raise PriceLoadConflict(f"price load {source_id} {ts}", None, Exception(str(report.get("error"))))
    if result.returncode != 0:
        detail = report.get("error") or (result.stderr or "")[-500:]
        raise PriceLoadFailed(f"price load {source_id} {ts} exited {result.returncode}: {detail}")
    jobs._log_report("price_load", {k: v for k, v in report.items() if not isinstance(v, dict | list)})
    return report


def price_stats_job(
    *, _session_factory: Any = None, _stats: Callable[..., Any] | None = None
) -> dict[str, Any]:
    """Body of `price_stats`: the trailing statistics for every location (`services.prices.stats`)."""
    from infra.scheduler import jobs
    from services.db.session import session_scope

    stats = _stats or jobs._load_fn("services.prices.stats", "run_stats")
    factory = _session_factory if _session_factory is not None else jobs.build_session_factory()
    with session_scope(factory) as session:
        report: dict[str, Any] = dict(stats(session))
    jobs._log_report("price_stats", report)
    return report


def is_price_source(source: Mapping[str, Any]) -> bool:
    """A manifest row the price runner serves (`runner: prices`); the generic ticks skip it."""
    return str(source.get("runner") or "") == "prices"
