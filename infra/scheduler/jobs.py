"""Job bodies for the `alert_tick` and `post_draft_tick` periodic jobs (docs/00-PLAN.md Sprint 3
item 4; ADR 0004).

Importable without Postgres and without `services.alerts`/`services.social` existing yet: the two
worker modules are lazily imported inside each `*_job` function, and `build_session_factory` only
touches `services.db.session` (which itself falls back to an in-memory SQLite engine when
`DATABASE_URL` is unset — see its docstring) when actually called, never at import time. This lets
`infra/scheduler/app.py` import this module unconditionally, the same way it already imports
`infra/scheduler/cadence.py`.

Each `*_job` function is the exact body registered on the corresponding Procrastinate task in
`infra/scheduler/app.py`; kept in this module, separate from `app.py`, only so the module stays
importable before a live `DATABASE_URL`/`procrastinate.App` exists (this file imports neither).
"""

from __future__ import annotations

import functools
import importlib
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any

logger = logging.getLogger("infra.scheduler.jobs")


def _load(module_path: str, attr: str) -> Callable[..., Any]:
    """Import `module_path` and return its `attr`, both fully dynamic to mypy (a plain `from X
    import Y` makes mypy resolve `X` at check time, which fails today for `services.social.worker`
    — a concurrent agent is still writing it — and would then flip to an "unused ignore" error
    under `--strict` the moment it lands if suppressed with a `type: ignore` instead). This way the
    module and function are only ever looked up at call time, which is also the actual "lazy
    import" requirement: neither worker module is imported until a tick runs.
    """
    module = importlib.import_module(module_path)
    return getattr(module, attr)  # type: ignore[no-any-return]


@functools.lru_cache(maxsize=1)
def build_session_factory() -> Any:
    """Build (and cache) a SQLAlchemy sessionmaker bound to `DATABASE_URL`.

    `services.db.session.get_engine` already implements the "`DATABASE_URL` from the environment,
    fall back to in-memory SQLite" convention (CLAUDE.md: config from environment only) — this
    function just reuses it rather than re-deriving it, and caches the result so every tick shares
    one engine/connection pool instead of opening a new one per job.
    """
    from services.db.session import get_engine, get_sessionmaker

    engine = get_engine(os.environ.get("DATABASE_URL"))
    return get_sessionmaker(engine)


def _report_to_dict(report: Any) -> dict[str, Any]:
    """Normalise a worker report (a frozen dataclass per the contract, or a test double shaped
    like one) into a plain, JSON-safe dict for Procrastinate's job result column."""
    data = asdict(report) if is_dataclass(report) and not isinstance(report, type) else dict(vars(report))
    return {key: (list(value) if isinstance(value, tuple) else value) for key, value in data.items()}


def _log_report(job_name: str, data: dict[str, Any]) -> None:
    """Log one structured line: every count field, plus `error_count` — never the `errors` tuple's
    text, which can embed a customer's saved-search query or a post's draft copy (CLAUDE.md "store
    the minimum personal data")."""
    errors = data.get("errors") or []
    fields = {key: value for key, value in data.items() if key != "errors"}
    fields["error_count"] = len(errors)
    line = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
    logger.info("%s %s", job_name, line, extra={"job": job_name, **fields})


def alert_tick_job(_run: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Body of the `alert_tick` Procrastinate task: run `services.alerts.worker.run_alert_tick`
    against the shared session factory, log its report, and return the report as a dict (the
    Procrastinate job result).

    `_run` is the test seam: pass a fake to exercise this function without importing
    `services.alerts.worker` (which a concurrent agent may not have written yet) or needing a
    database — `build_session_factory()` still runs, but only ever builds a cheap in-memory SQLite
    sessionmaker unless `DATABASE_URL` is set, and a fake `_run` need not use it at all.
    """
    run = _run if _run is not None else _load("services.alerts.worker", "run_alert_tick")
    report = run(build_session_factory())
    data = _report_to_dict(report)
    _log_report("alert_tick", data)
    return data


def post_draft_tick_job(_run: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Body of the `post_draft_tick` Procrastinate task: the `services.social.worker` analogue of
    `alert_tick_job` above — same injection seam, same report handling."""
    run = _run if _run is not None else _load("services.social.worker", "draft_posts_tick")
    report = run(build_session_factory())
    data = _report_to_dict(report)
    _log_report("post_draft_tick", data)
    return data
