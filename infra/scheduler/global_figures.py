"""The global electricity figures' refresh: Ember's data, twice a month (docs/26-global-figures.md §8).

Ember updates its yearly and monthly data "twice a month with an update in the first week of the month
followed by a second update in the third week of the month" (its yearly data page, read 2026-10-23), so the
tick runs on the 8th and the 22nd, the day after each of those weeks ends, at 09:38 UTC. Minute 38 is used by
no other tick (`cadence.CRON_BY_BUCKET`, every `@app.periodic` in app.py and the price ticks;
`test_global_figures.py` checks it). The manifest row carries `runner: global`, so the generic bucket ticks
skip it (`app.py::due_sources`).

One job, two processes, the same CLIs an operator runs by hand:

    python -m pipeline.connectors.global_ember_electricity run --trigger <t>   the fetch, its run record
    python -m services.global_figures.load --latest                             the load, if the run wrote

The fetch's outcome becomes a `source_run` row and the source's health through `jobs.fetch_outcome`, as for
every connector, so a broken key, a refused request or a changed layout alerts as any failing source does
(freshness, `consecutive_failures`, dead letters). **A failed download keeps the last good data**: the load
runs after a run that ended `ok`, `partial` or `unchanged` (the last also retries a load an earlier job
could not finish), or after any run that wrote a dataset: the connector reads the inbox whatever the API
answered, so a person's download is loaded even while the API refuses the key or fails (the run's own
status still says so, and a transient failure is still retried). Nothing from a failed API answer is
written, and the loader replaces a dataset inside one transaction. A failed load raises, so the job fails
visibly; the next tick loads the same run again.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger("infra.scheduler.global_figures")

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ID = "global.ember.electricity"
#: The 8th and the 22nd of each month, 09:38 UTC (module docstring).
GLOBAL_ELECTRICITY_CRON = "38 9 8,22 * *"
PERIODIC_ID = "tick:global_electricity"
#: Three answers of up to about 20 MB at 0.5 requests a second, parsed and checked.
FETCH_TIMEOUT_S = 900
LOAD_TIMEOUT_S = 900
LOADABLE = ("ok", "partial", "unchanged")


class GlobalLoadFailed(RuntimeError):
    """The load process failed; the figures already loaded stay served, and the next tick retries."""


def is_global_source(source: Mapping[str, Any]) -> bool:
    """A manifest row this job serves (`runner: global`); the generic ticks skip it."""
    return str(source.get("runner") or "") == "global"


def fetch_command(trigger: str) -> list[str]:
    return [sys.executable, "-m", "pipeline.connectors.global_ember_electricity", "run", "--trigger", trigger]


def load_command() -> list[str]:
    return [sys.executable, "-m", "services.global_figures.load", "--latest"]


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _load(run: Callable[..., Any]) -> dict[str, Any]:
    from infra.scheduler import jobs

    env = dict(os.environ)
    options = jobs.worker_connect_args(env.get("DATABASE_URL")).get("options")
    if options:
        env["PGOPTIONS"] = f"{env['PGOPTIONS']} {options}" if env.get("PGOPTIONS") else str(options)
    env.setdefault("INFRAQUE_DATA_DIR", str(jobs.connector_data_root()))
    try:
        result = run(
            load_command(),
            cwd=ROOT,
            env=env,
            timeout=LOAD_TIMEOUT_S,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GlobalLoadFailed(f"global electricity load timed out after {LOAD_TIMEOUT_S} s") from exc
    lines = [line for line in (result.stdout or "").splitlines() if line.startswith("{")]
    report: dict[str, Any] = json.loads(lines[-1]) if lines else {}
    if result.returncode != 0:
        detail = report.get("error") or (result.stderr or "")[-500:]
        raise GlobalLoadFailed(f"global electricity load exited {result.returncode}: {detail}")
    return report


def global_electricity_job(
    *,
    trigger: str = "schedule",
    attempt: int = 1,
    final_attempt: bool = False,
    _run: Callable[..., Any] = subprocess.run,
    _outcome: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Body of `global_electricity_refresh`: the fetch as its own process, its outcome recorded, then the
    load when the run ended in a state whose output may be served."""
    from infra.scheduler import jobs

    outcome_fn = _outcome or jobs.fetch_outcome
    started = dt.datetime.now(dt.UTC)
    try:
        result = _run(
            fetch_command(trigger),
            cwd=ROOT,
            timeout=FETCH_TIMEOUT_S,
            capture_output=True,
            text=True,
            check=False,
        )
        returncode: int | None = result.returncode
        stdout, stderr = result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        returncode, stdout, stderr = None, _text(exc.stdout), _text(exc.stderr)
    out = outcome_fn(
        SOURCE_ID,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        trigger=trigger,
        started_at=started,
        timeout_s=FETCH_TIMEOUT_S,
        attempt=attempt,
        final_attempt=final_attempt,
    )
    status = str(out.get("status"))
    logger.info("global electricity fetch -> %s", status, extra={"source_id": SOURCE_ID, "status": status})
    if status in LOADABLE or wrote_datasets(out.get("record")):
        out = {**out, "load": _load(_run)}
    return out


def wrote_datasets(record: Mapping[str, Any] | None) -> bool:
    """Whether a run wrote any dataset (`outputs.datasets`), whatever its status."""
    outputs = (record or {}).get("outputs") or {}
    return bool(outputs.get("datasets")) if isinstance(outputs, Mapping) else False
