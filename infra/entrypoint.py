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


#: uvicorn's own default: believe `X-Forwarded-For`/`X-Forwarded-Proto` from loopback only.
DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1"


def forwarded_allow_ips() -> str:
    """The peers whose `X-Forwarded-For` uvicorn believes (`FORWARDED_ALLOW_IPS`, comma-separated).

    Production sets it to Caddy's fixed address on the Compose network (`compose.prod.yml`), so
    `request.client.host` is the visitor Caddy resolved from Cloudflare's `CF-Connecting-IP`, and a
    header from anyone else is ignored (devops audit 2026-09-30 F1). `*` would let any client name
    its own address, so it is refused; the per-address rate limits would be worthless under it."""
    value = os.environ.get("FORWARDED_ALLOW_IPS", "").strip() or DEFAULT_FORWARDED_ALLOW_IPS
    hosts = [h.strip() for h in value.split(",") if h.strip()]
    if "*" in hosts:
        raise SystemExit(
            "FORWARDED_ALLOW_IPS='*' trusts every client's X-Forwarded-For; name the proxy's address"
        )
    return ",".join(hosts)


#: uvicorn worker processes per container when `WEB_CONCURRENCY` is unset. api is 1: each process
#: holds its own geo and asset indexes, and two workers peaked at 1,182 MB against the 1 GB
#: container limit (devops audit 2026-09-30 F4); the api service scales by replicas instead.
DEFAULT_WORKERS = {"api": "1", "web": "1"}


def _serve(service: str, port: int) -> None:
    import uvicorn

    uvicorn.run(
        f"infra.entrypoint:{service}_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 -- container-internal; only Caddy publishes a port (docs/20 §11)
        port=int(os.environ.get("PORT", port)),
        workers=int(os.environ.get("WEB_CONCURRENCY", DEFAULT_WORKERS[service])),
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips(),
        log_config=None,  # keep the JSON root handler; uvicorn's own dictConfig would replace it
    )


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in SERVICES:
        raise SystemExit(f"usage: python -m infra.entrypoint {'|'.join(SERVICES)} [args...]")
    service, rest = args[0], args[1:]
    bootstrap(service)
    if service in ("api", "web"):
        _serve(service, 8000 if service == "api" else 8001)
    elif service == "worker":
        from infra.scheduler.worker import main as run_worker

        run_worker(rest)
    else:
        from infra.scheduler.app import main as run_scheduler

        run_scheduler()


if __name__ == "__main__":
    main()
