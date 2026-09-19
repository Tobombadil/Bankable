"""Error tracking hook (docs/20 §10; docs/60 §7). Env-gated: a no-op unless `SENTRY_DSN` is set.

Called once per process by `infra/entrypoint.py` before the service module is imported, so every
compose service (api, web, worker, browser-worker, scheduler) reports to the same project with a
`service` tag and no per-service code. The SDK is imported lazily so a checkout without
`sentry-sdk` installed (or a test run) never pays for it. Metrics stay in Postgres (`source_run`
rows) per docs/60 §7; this module is only the exception path.
"""

from __future__ import annotations

import importlib
import logging
import os

logger = logging.getLogger("infra.observability")


def init_error_tracking(service_name: str) -> bool:
    """Initialise the error tracker for this process. Returns True when it is active.

    `SENTRY_DSN` unset or blank: nothing happens (dev, CI, tests). Set but the SDK missing: a
    warning, still no-op — a missing package must never take a worker down. Personal data is
    never sent (`send_default_pii=False`, docs/04 P-rows); tracing is off (cost ceiling, O-8).
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        sentry_sdk = importlib.import_module("sentry_sdk")
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; error tracking disabled")
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("ENVIRONMENT", "dev"),
        release=os.environ.get("SENTRY_RELEASE") or None,
        send_default_pii=False,
        traces_sample_rate=0.0,
    )
    sentry_sdk.set_tag("service", service_name)
    logger.info("error tracking active", extra={"service": service_name})
    return True
