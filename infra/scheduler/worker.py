"""Procrastinate worker entrypoint: `worker`, `browser-worker` and `social` compose services all
run this, each with a different `--queues` filter (docs/20 §4.1's worker pools). It never runs the
periodic deferrer's ticks itself in the normal case — `infra.scheduler.app`'s `scheduler` service
does that — but see that module's docstring: running it here too would be safe, not just
tolerated, because Procrastinate dedupes ticks at the database level.

    # worker-plain (docs/20 §4.1):
    python -m infra.scheduler.worker \
        --queues fetch,diff,normalise,resolve,alert,post_draft,publish_post,sor_sync,webhook
    # worker-browser:
    python -m infra.scheduler.worker --queues fetch_browser
"""

from __future__ import annotations

import argparse
import logging
import os

from infra.scheduler.app import app

logger = logging.getLogger("infra.scheduler.worker")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--queues",
        default="fetch",
        help="Comma-separated Procrastinate queue names to consume (docs/20 §4.1 worker pools).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("WORKER_CONCURRENCY", "2")),
        help="Max jobs processed concurrently by this process.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(message)s")
    args = parse_args(argv)
    queues = [q.strip() for q in args.queues.split(",") if q.strip()]
    logger.info("worker starting", extra={"queues": queues, "concurrency": args.concurrency})
    app.run_worker(queues=queues, concurrency=args.concurrency, wait=True)


if __name__ == "__main__":
    main()
