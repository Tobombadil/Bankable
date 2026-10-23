"""CLI: `python -m pipeline.connectors.prices list | run <source_id> --mode latest|daily|backfill
[--trigger schedule|manual|backfill|retry] [--run-id <uuid>]`.

Structured JSON on stdout, one `result` line per run, in the record connectors' shape
(`pipeline/connectors/__main__.py`), so the scheduler records it the same way
(`infra/scheduler/jobs.py::fetch_outcome`). Exit 0 for ok/unchanged/partial, 1 for failed or
blocked, 2 for a refusal (the source's terms gate it, or the family does not serve it).
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import uuid

from pipeline.connectors.__main__ import _setup_logging
from pipeline.connectors.objectstore import StoreConfigError
from pipeline.connectors.prices import PRICE_CONNECTORS
from pipeline.connectors.prices.base import MODES, PriceGateRefused
from pipeline.connectors.prices.runner import TRIGGERS, run
from pipeline.connectors.registry import RegistrationError
from pipeline.connectors.store import DATA_DIR, open_store


def _run_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not a UUID: {value!r}") from e


def _use_system_allocator() -> None:
    """Arrow's buffers from the C allocator, not its bundled one: a freed buffer then goes back to
    the operating system. Measured on an ERCOT backfill (review 2026-10-23, docs/29 §9): the whole
    run, its child processes included, peaks at 359 MB in a memory cgroup with this and fills a
    400 MB one without it."""
    import pyarrow as pa  # type: ignore[import-untyped]

    pa.set_memory_pool(pa.system_memory_pool())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.connectors.prices")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the sources this family serves")
    rp = sub.add_parser("run", help="run one price source")
    rp.add_argument("source_id")
    rp.add_argument("--mode", choices=MODES, required=True)
    rp.add_argument("--trigger", choices=TRIGGERS, default="manual")
    rp.add_argument("--run-id", type=_run_id, default=None)
    rp.add_argument("--data-dir", default=str(DATA_DIR))
    args = ap.parse_args(argv)
    log = _setup_logging()

    if args.cmd == "list":
        for source_id, module in PRICE_CONNECTORS.items():
            log.info("source", extra={"id": source_id, "module": module})
        return 0
    try:
        store = open_store(pathlib.Path(args.data_dir))
    except StoreConfigError as e:
        log.error("store misconfigured", extra={"error": str(e)})
        return 1
    _use_system_allocator()
    try:
        result = run(args.source_id, args.mode, store=store, trigger=args.trigger, run_id=args.run_id)
    except (PriceGateRefused, RegistrationError, KeyError) as e:
        log.error("refused", extra={"source_id": args.source_id, "error": str(e)})
        return 2
    except Exception as e:
        logging.getLogger(__name__).exception("price run crashed", extra={"source_id": args.source_id})
        log.error("crashed", extra={"source_id": args.source_id, "error": repr(e)[:500]})
        return 1
    r = result.record
    log.info(
        "result",
        extra={
            "source_id": args.source_id,
            "run_id": r["id"],
            "mode": r["mode"],
            "status": r["status"],
            "trigger": r["trigger"],
            "rows_seen": r.get("rows_seen", 0),
            "rows_new": r.get("rows_new", 0),
            "rows_changed": r.get("rows_changed", 0),
            "requests": len(r.get("requests") or []),
            "bytes": r.get("bytes", 0),
            "dq_status": r.get("dq_status"),
            "error": r.get("error"),
            "run_path": str(result.run_path),
            "run_key": store.key(result.run_path),
            "store": store.backend.name,
        },
    )
    return 1 if r["status"] in ("failed", "blocked") else 0


if __name__ == "__main__":
    sys.exit(main())
