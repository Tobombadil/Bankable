"""Queue, once, the jobs a store that starts empty would otherwise wait days for.

The bucket ticks (`infra/scheduler/app.py`) fetch a source only when its bucket next fires: the 1st
of the month for EIA-860M, Monday 04:13 UTC for the weekly queues. A load is queued only by a fetch
that wrote a new snapshot. A new server therefore serves an empty store for up to a month, and a
data root fetched by hand (`python -m pipeline.connectors run --all`) is never loaded at all,
because a hand run records no `source_run` row and queues nothing.

    python -m infra.scheduler.bootstrap fetch             # every implemented, ungated source, now
    python -m infra.scheduler.bootstrap load              # each source's latest stored promoted run
    python -m infra.scheduler.bootstrap context           # the context layers on disk, now
    python -m infra.scheduler.bootstrap context --build   # rebuild them first (`context_build`)

Both queue the jobs the ticks queue (`run_connector`, `load_source`) under the same per-source
`lock` and `queueing_lock`, so they are safe while the scheduler and workers run and safe to
repeat: a source whose job is still queued is reported `already_queued`. `fetch` is the path for a
new server. Each run records its `source_run` row and, when it writes a new snapshot, queues its own
load, which chains into the store-wide resolve and enrich passes. `load` is the path for a data root
that already holds runs. The loader starts from the latest promoted run when the source has never
been loaded, and replays the promoted runs after its last loaded one otherwise
(`services.ingest.loader.runs_to_load`). `context` queues the monthly tick's `context_load` (or,
with `--build`, its `context_build`, which chains into the load), for a data root shipped with its
`normalized/context/` files or a refresh that should not wait for the 3rd of the month.

The selection and the deferral are separate functions so the tests need no database; `main` wires
them to the Procrastinate app, which needs `DATABASE_URL` (`infra/scheduler/app.py`).
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any

logger = logging.getLogger("infra.scheduler.bootstrap")


def fetchable_sources(
    scheduled: Iterable[Mapping[str, Any]], registry_status: Iterable[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """The scheduled sources (`data/sources.yaml` entries with a `cadence`) that a fetch would run:
    registry state `implemented`, the same rule as `pipeline.connectors run --all`. Gated,
    excluded and unimplemented sources are left out, where a tick queues them and the runner
    refuses them before any I/O."""
    implemented = {str(row["id"]) for row in registry_status if row.get("state") == "implemented"}
    return [source for source in scheduled if str(source.get("id")) in implemented]


def latest_promoted_ts(runs: Iterable[Mapping[str, Any]]) -> str | None:
    """The snapshot token of the newest run that promoted a normalised output (`status == "ok"`):
    the basename of `outputs.normalized`, as `services.ingest.loader._run_ts` reads it. `None` when
    no run did, so there is nothing to load."""
    latest: str | None = None
    for record in runs:
        if record.get("status") != "ok":
            continue
        location = (record.get("outputs") or {}).get("normalized")
        if not location:
            continue
        name = pathlib.PurePosixPath(str(location)).name
        if not name.endswith(".parquet"):
            continue
        ts = name[: -len(".parquet")]
        if latest is None or ts > latest:
            latest = ts
    return latest


def queue_fetches(
    sources: Iterable[Mapping[str, Any]], defer_fetch: Callable[[Mapping[str, Any]], bool]
) -> list[dict[str, str]]:
    """Queue one fetch per source. `defer_fetch` returns False when the source's fetch is already
    queued or running."""
    report = []
    for source in sources:
        queued = defer_fetch(source)
        report.append(
            {
                "source_id": str(source["id"]),
                "action": "fetch",
                "result": "queued" if queued else "already_queued",
            }
        )
    return report


def queue_loads(
    source_ids: Iterable[str],
    runs_for: Callable[[str], Iterable[Mapping[str, Any]]],
    load_refusal: Callable[[str], str | None],
    defer_load: Callable[[str, str], bool],
) -> list[dict[str, str]]:
    """Queue one load per source that has a promoted run, at that run's token. A source the generic
    loader does not load (`jobs.load_kind_refusal`: a `document` or an asset-only source, which the
    fetch job never queues a load for either) is reported `no_generic_load`. `defer_load` returns
    False when the source's load is already queued or running."""
    report = []
    for source_id in source_ids:
        row = {"source_id": source_id, "action": "load"}
        ts = latest_promoted_ts(runs_for(source_id))
        if ts is None:
            report.append({**row, "result": "no_promoted_run"})
            continue
        refusal = load_refusal(source_id)
        if refusal:
            report.append({**row, "result": "no_generic_load", "reason": refusal})
            continue
        queued = defer_load(source_id, ts)
        report.append({**row, "result": "queued" if queued else "already_queued", "ts": ts})
    return report


def queue_context(task_name: str, defer: Callable[[], Any]) -> dict[str, str]:
    """Queue `context_load` or `context_build`. Both carry a `queueing_lock`: a second call while
    one is still waiting is reported `already_queued`. One that has started no longer holds it, so
    a second call then queues a run that waits on the job's `lock` and follows it."""
    import procrastinate

    row = {"action": task_name}
    try:
        defer()
    except procrastinate.exceptions.AlreadyEnqueued:
        return {**row, "result": "already_queued"}
    return {**row, "result": "queued"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m infra.scheduler.bootstrap", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("action", choices=("fetch", "load", "context"))
    parser.add_argument("--build", action="store_true", help="context: rebuild the files before loading them")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Deferred: importing the app opens no connection but needs DATABASE_URL.
    import procrastinate

    from infra.scheduler import app as scheduler
    from infra.scheduler import jobs
    from infra.scheduler.cadence import (
        execution_lock_for,
        queue_for_source,
        queueing_lock_for,
        safe_id,
    )
    from pipeline.connectors import store as store_module
    from pipeline.connectors.registry import Registry

    sources = fetchable_sources(scheduler._load_sources(), Registry().status())

    def defer_fetch(source: Mapping[str, Any]) -> bool:
        source_id = str(source["id"])
        try:
            scheduler.run_connector.configure(
                queue=queue_for_source(dict(source)),
                lock=execution_lock_for(source_id),
                queueing_lock=queueing_lock_for(source_id),
            ).defer(source_id=source_id)
        except procrastinate.exceptions.AlreadyEnqueued:
            return False
        return True

    def defer_load(source_id: str, ts: str) -> bool:
        # The lock pair `run_connector` queues its own load under (`app._defer_load`).
        try:
            scheduler.load_source.configure(
                lock=execution_lock_for(source_id), queueing_lock=f"load:{safe_id(source_id)}"
            ).defer(source_id=source_id, ts=ts)
        except procrastinate.exceptions.AlreadyEnqueued:
            return False
        return True

    with scheduler.app.open():
        if args.action == "context":
            task = scheduler.context_build if args.build else scheduler.context_load
            report = [queue_context(task.name, task.defer)]
        elif args.action == "fetch":
            report = queue_fetches(sources, defer_fetch)
        else:
            # The root and backend the fetch wrote through (`INFRAQUE_DATA_DIR`, `SNAPSHOT_STORE`).
            store = store_module.open_store(jobs.connector_data_root())
            ids = [str(s["id"]) for s in sources]
            report = queue_loads(ids, store.runs, jobs.load_kind_refusal, defer_load)
    counts: dict[str, int] = {}
    for row in report:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
        sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.write(json.dumps({"action": args.action, "sources": len(report), **counts}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
