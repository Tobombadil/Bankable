"""Procrastinate's own upkeep: recover jobs a dead worker left `doing`, and prune old jobs
(docs/51 §2.9 item 2, 2026-10-10). Registered in `infra/scheduler/app.py` like every other tick: a
`tick_*` on `SCHEDULER_ONLY_QUEUE` defers one job onto a queue the `worker` service consumes.

**Why a stalled job matters here.** `resolve_tick`, `enrich_tick`, `match_tick` and `context_load`
share `lock="resolve"`, and each source's fetch and load share that source's lock. Procrastinate
holds a lock for as long as one job carrying it is `doing`. A worker killed mid-job (an OOM, a
SIGKILL at the end of Docker's stop grace, the host going down) never moves that job on, so it stays
`doing` forever and every later job with the same lock waits behind it. Seen on the rehearsal store
(docs/64 §4) on 2026-10-10: two `run_connector` jobs left `doing` by the sandbox stopping on
2026-10-09 22:25 UTC, with no worker, holding `source:us-grants-gov-search2` and
`source:gb-find-a-tender`; no later fetch of either source could run. Nothing called Procrastinate's
recovery before this module.

**How a stalled job is told from a slow one.** By the worker's heartbeat, Procrastinate 3.x's own
rule (`JobManager.get_stalled_jobs` without `nb_seconds`, which 3.9 deprecates): a live worker
writes `procrastinate_workers.last_heartbeat` every 10 s from its event loop, also while a
synchronous job runs in a thread, so a one-hour resolve pass on a live worker is never "stalled".
A worker stops beating when its graceful shutdown starts, and Docker kills it `STOP_GRACE_S` later
(`stop_grace_period` in `infra/compose/docker-compose.yml`). `STALLED_WORKER_TIMEOUT_S` is twice
that, so a worker still draining its jobs is never taken for dead. The same figure is every
worker's `stalled_worker_timeout` (`infra/scheduler/worker.py`, `app.main`): a starting worker
prunes the rows of workers silent for longer, and a pruned worker's jobs count as stalled at once
(`worker_id IS NULL`), so the default 30 s would let a starting worker orphan a draining one's job.

**What happens to one.** It is retried (back to `todo`, `attempts + 1`), which also releases its
lock. Every task the scheduler runs is idempotent by design (each one's docstring), so a retry is
the same work again. Two exceptions, both failed instead (`failed`, kept with its events):
- it has already been attempted `STALLED_RETRY_MAX_ATTEMPTS` times, so a job that kills its worker
  (an OOM) cannot crash-loop the pool;
- another job with its `queueing_lock` is already queued, which will do the same work.
"""

from __future__ import annotations

import logging
from typing import Any

import procrastinate
from procrastinate.jobs import Job, Status
from procrastinate.manager import JobManager

logger = logging.getLogger("infra.scheduler.queue_maintenance")

#: Docker's grace between SIGTERM and SIGKILL for `worker`, `browser-worker` and `scheduler`
#: (`stop_grace_period: 300s`, infra/compose/docker-compose.yml; infra/test_compose.py pins the two).
#: Procrastinate's graceful stop waits for running jobs; most finish well inside it (measured on the
#: rehearsal store: context_load 190 s, resolve 43 s, a load 24 s), and a longer one is killed and
#: recovered here.
STOP_GRACE_S = 300
#: A worker silent this long is dead, not draining (module docstring).
STALLED_WORKER_TIMEOUT_S = 2 * STOP_GRACE_S
#: Attempts after which a stalled job is failed rather than retried (module docstring). A retried
#: job's `attempts` counts every earlier run, Procrastinate's own retries included.
STALLED_RETRY_MAX_ATTEMPTS = 3
#: Every 10 minutes, at minutes no other tick or fetch bucket uses (cadence.py `CRON_BY_BUCKET` and
#: the ticks in app.py; infra/scheduler/test_queue_maintenance.py checks). A stalled job is found
#: 10-20 minutes after its worker died: the heartbeat threshold plus at most one interval.
STALLED_CRON = "4,14,24,34,44,54 * * * *"

#: Finished jobs older than this are deleted daily: succeeded (and cancelled or aborted) jobs after
#: 14 days, failed ones after 90, so a failure stays readable for a quarter's review (docs/63 §7.4)
#: while the routine rows do not pile up: about 2,600 jobs a day at the current cadence, 1,824 of
#: them fetches of the 19 sources in the 15-minute bucket (data/sources.yaml, counted 2026-10-10).
#: The run records that matter for operations are `source_run` rows, which this does not touch.
SUCCEEDED_JOBS_KEEP_HOURS = 14 * 24
FAILED_JOBS_KEEP_HOURS = 90 * 24
#: Daily at 02:33 UTC: a minute no other tick uses, clear of the context build (02:43 on the 3rd),
#: the daily fetch bucket (03:07) and the backup timer (03:17).
PRUNE_CRON = "33 2 * * *"


def _describe(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "task_name": job.task_name,
        "queue": job.queue,
        "lock": job.lock,
        "attempts": job.attempts,
        "worker_id": job.worker_id,
    }


async def recover_stalled_jobs(
    manager: JobManager, *, stalled_after_s: float = STALLED_WORKER_TIMEOUT_S
) -> dict[str, Any]:
    """Prune workers silent for `stalled_after_s`, then retry (or fail, module docstring) every
    `doing` job whose worker is gone. Returns the ids acted on, for the job's result column."""
    pruned = list(await manager.prune_stalled_workers(stalled_after_s))
    retried: list[int] = []
    failed: list[int] = []
    for job in await manager.get_stalled_jobs(seconds_since_heartbeat=stalled_after_s):
        detail = _describe(job)
        if job.attempts >= STALLED_RETRY_MAX_ATTEMPTS:
            await manager.finish_job(job, status=Status.FAILED, delete_job=False)
            logger.error("stalled job failed: attempted too often to retry again", extra=detail)
            failed.append(int(job.id or 0))
            continue
        try:
            await manager.retry_job(job)
        except procrastinate.exceptions.UniqueViolation:
            # The same task is already queued under its queueing lock; that one does the work.
            await manager.finish_job(job, status=Status.FAILED, delete_job=False)
            logger.warning("stalled job failed: the same task is already queued", extra=detail)
            failed.append(int(job.id or 0))
            continue
        logger.warning("stalled job retried: its worker stopped beating", extra=detail)
        retried.append(int(job.id or 0))
    report = {"pruned_workers": len(pruned), "retried": retried, "failed": failed}
    logger.info("stalled-job recovery", extra=report | {"stalled_after_s": stalled_after_s})
    return report


async def remove_old_jobs(manager: JobManager) -> dict[str, Any]:
    """Delete finished jobs past their keep period (`SUCCEEDED_JOBS_KEEP_HOURS`,
    `FAILED_JOBS_KEEP_HOURS`). Their events go with them (`ON DELETE CASCADE`), and a periodic
    defer that pointed at one is unlinked by Procrastinate's own trigger. Jobs still `todo` or
    `doing` are never touched."""
    await manager.delete_old_jobs(
        nb_hours=SUCCEEDED_JOBS_KEEP_HOURS, include_cancelled=True, include_aborted=True
    )
    await manager.delete_old_jobs(nb_hours=FAILED_JOBS_KEEP_HOURS, include_failed=True)
    report = {
        "succeeded_kept_hours": SUCCEEDED_JOBS_KEEP_HOURS,
        "failed_kept_hours": FAILED_JOBS_KEEP_HOURS,
    }
    logger.info("old jobs removed", extra=report)
    return report
