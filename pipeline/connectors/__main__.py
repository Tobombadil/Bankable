"""CLI: `python -m pipeline.connectors list | run <source_id>... [--all] [--allow-restricted]`.

Logs are structured JSON on stdout (docs/04 E-18); nothing is printed otherwise.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys
from typing import Any

from pipeline.connectors.base import GateViolation
from pipeline.connectors.registry import RegistrationError, Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import DATA_DIR, Store

_STD = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "service": "pipeline",
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STD and not k.startswith("_")})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(payload, default=str)


def _setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    return logging.getLogger("pipeline.connectors.cli")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.connectors")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="registry status: implemented / unimplemented / gated / excluded")
    rp = sub.add_parser("run", help="run one or more connectors")
    rp.add_argument("source_ids", nargs="*")
    rp.add_argument("--all", action="store_true", help="every implemented, non-gated source")
    rp.add_argument(
        "--allow-restricted",
        action="store_true",
        help="run a reuse=restricted/unknown source into the quarantine store",
    )
    rp.add_argument("--data-dir", default=str(DATA_DIR))
    args = ap.parse_args(argv)
    log = _setup_logging()
    registry = Registry()

    if args.cmd == "list":
        for row in registry.status():
            log.info("source", extra=row)
        return 0

    ids = list(args.source_ids)
    if args.all:
        ids += [s["id"] for s in registry.status() if s["state"] == "implemented"]
    if not ids:
        ap.error("give source ids or --all")
    store = Store(pathlib.Path(args.data_dir))
    rc = 0
    for sid in ids:
        try:
            res = run(sid, registry=registry, store=store, allow_restricted=args.allow_restricted)
        except GateViolation as e:
            log.error("gate refused", extra={"source_id": sid, "error": str(e)})
            rc = 2
            continue
        except RegistrationError as e:
            log.error("not registered", extra={"source_id": sid, "error": str(e)})
            rc = 2
            continue
        r = res.run
        log.info(
            "result",
            extra={
                "source_id": sid,
                "run_id": r["id"],
                "status": r["status"],
                "rows_seen": r["rows_seen"],
                "rows_fetched": r["rows_fetched"],
                "rows_new": r["rows_new"],
                "rows_changed": r["rows_changed"],
                "rows_gone": r["rows_gone"],
                "dq_status": r["dq_status"],
                "hold_reasons": r.get("hold_reasons"),
                "error": r.get("error"),
                "run_path": str(res.paths.get("run")),
            },
        )
        if r["status"] in ("failed", "blocked"):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
