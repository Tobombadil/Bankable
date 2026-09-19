"""`python -m infra.entrypoint api|web|worker|scheduler [args]` — one entrypoint per compose service.
JSON logging (infra/logging_config.py) and error tracking (infra/observability.py) are wired before
the service module is imported, so each service's own `logging.basicConfig` becomes a no-op and no
service file changes. uvicorn spawns api/web workers as fresh processes: the factories re-bootstrap."""

from __future__ import annotations

import os
import sys
from typing import Any

from infra.logging_config import configure_logging
from infra.observability import init_error_tracking

SERVICES = ("api", "web", "worker", "scheduler")


def bootstrap(service: str) -> None:
    configure_logging()
    init_error_tracking(service)


def api_app() -> Any:  # uvicorn factory: runs inside each spawned worker process
    bootstrap("api")
    from services.api.app import app

    return app


def web_app() -> Any:
    bootstrap("web")
    from web.app import app

    return app


def _serve(service: str, port: int, workers: str) -> None:
    import uvicorn

    uvicorn.run(
        f"infra.entrypoint:{service}_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 -- container-internal; only Caddy publishes a port (docs/20 §11)
        port=int(os.environ.get("PORT", port)),
        workers=int(os.environ.get("WEB_CONCURRENCY", workers)),
        log_config=None,  # keep the JSON root handler; uvicorn's own dictConfig would replace it
    )


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in SERVICES:
        raise SystemExit(f"usage: python -m infra.entrypoint {'|'.join(SERVICES)} [args...]")
    service, rest = args[0], args[1:]
    bootstrap(service)
    if service in ("api", "web"):
        _serve(service, 8000 if service == "api" else 8001, "2" if service == "api" else "1")
    elif service == "worker":
        from infra.scheduler.worker import main as run_worker

        run_worker(rest)
    else:
        from infra.scheduler.app import main as run_scheduler

        run_scheduler()


if __name__ == "__main__":
    main()
