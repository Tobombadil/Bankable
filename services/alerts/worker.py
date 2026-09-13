"""One scheduled alert-and-delivery tick (docs/20-architecture.md §4.1 `worker-plain`, §4.2 job
type `alert`; task brief: "the scheduled alert cycle and delivery worker" that `infra/scheduler/`
calls on an interval — this module owns only the function it calls, never the schedule itself).

A tick is two independent halves, each in its own session/transaction, so a failure in one never
blocks the other (task brief):

1. **Alerts.** `services.alerts.evaluate.run_alert_cycle` already evaluates *every* active
   `SavedSearch` in one call — it holds the loop, the per-search watermark advance, and (for the
   `email` channel) the synchronous send through the given `EmailPort`. This half just opens a
   session, calls it once, counts what it returned, and commits.
2. **Webhook deliveries.** `services.alerts.webhooks.deliver_pending` likewise already finds every
   `pending`/due-`retrying` `WebhookDelivery` and attempts each one through the given `Transport`.
   This half opens a second session, calls it once, counts the outcome, and commits.

See `services/alerts/README.md` for the numbered decisions this module makes (why there is no
separate "send queued alerts" step, how idempotency holds across two ticks, the `HttpxTransport`
shape, and the `--dry-run` CLI contract) and for what `evaluate.py`/`webhooks.py` were found to
already provide versus what this module adds.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from services.alerts.evaluate import run_alert_cycle
from services.alerts.webhooks import Transport, deliver_pending
from services.api.auth import EmailPort, ResendEmailAdapter
from services.db.models import SavedSearch, WebhookEndpoint
from services.db.session import get_engine, get_sessionmaker, init_db

logger = logging.getLogger(__name__)

#: Decision 1 (services/alerts/README.md): one `ResendEmailAdapter()` per process, built lazily on
#: first use rather than at import time, so importing this module never touches `os.environ`
#: before a caller has had a chance to set `RESEND_API_KEY` (relevant for the CLI and for tests
#: that import this module before configuring the environment). It is dry-run whenever
#: `RESEND_API_KEY` is unset (`services/api/auth.py`), which is always true in this sandbox.
_default_email_port: EmailPort | None = None


def _process_default_email_port() -> EmailPort:
    global _default_email_port
    if _default_email_port is None:
        _default_email_port = ResendEmailAdapter()
    return _default_email_port


def _secret_for(endpoint: WebhookEndpoint) -> str:
    """`webhooks.deliver_pending`'s `secret_for`: the endpoint's own signing secret, read fresh
    per delivery (never cached, never logged — `WebhookEndpoint.secret` docstring)."""
    return endpoint.secret


class HttpxTransport:
    """The production `Transport` (`services/alerts/webhooks.py`'s `Transport` Protocol): a real
    `httpx.Client` with a 10 second timeout. `httpx.Client.post(url, *, content=, headers=)`
    returns an `httpx.Response`, which already carries `status_code` — it satisfies
    `TransportResponse` structurally with no adaptation, so this class exists only to own the
    client's lifecycle (construct once per tick with the right timeout, close it after).

    Accepts an already-built `httpx.Client` (`client=`) so a test can hand it one wired to
    `httpx.MockTransport` and exercise this exact class end to end, per the task brief."""

    def __init__(self, *, timeout: float = 10.0, client: httpx.Client | None = None) -> None:
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> httpx.Response:
        return self._client.post(url, content=content, headers=headers)

    def close(self) -> None:
        """No-op when this instance was handed an external client — closing someone else's
        `httpx.Client` is that caller's job, not this transport's."""
        if self._owns_client:
            self._client.close()


@dataclass
class _DryRunResponse:
    """Not frozen: `webhooks.TransportResponse` declares `status_code` as a settable attribute
    (a Protocol data member, not a read-only property), and mypy's structural check requires an
    implementer's attribute to be equally settable — a frozen dataclass's fields are read-only
    and fail that check even though the value itself is never mutated here."""

    status_code: int = 200


class DryRunTransport:
    """`--dry-run`'s transport (task brief: "a transport that records but never sends"): every
    call is recorded in `self.calls` and answered with a synthetic 2xx, with no outbound network
    I/O at all — deliveries still progress to `delivered` in the database, which is the point of
    a *dry run of the worker's wiring* rather than a dry run that leaves rows untouched (decision
    2, services/alerts/README.md)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> _DryRunResponse:
        self.calls.append({"url": url, "headers": dict(headers)})
        logger.info("dry_run_webhook_post url=%s", url)
        return _DryRunResponse()


@dataclass(frozen=True)
class AlertTickReport:
    started_at: dt.datetime
    finished_at: dt.datetime
    searches_evaluated: int
    alerts_created: int
    emails_sent: int
    deliveries_attempted: int
    deliveries_delivered: int
    deliveries_failed: int
    errors: tuple[str, ...]


def _run_alert_half(
    session_factory: sessionmaker[Session],
    *,
    email_port: EmailPort,
    now: dt.datetime,
    errors: list[str],
) -> tuple[int, int, int]:
    """Transaction 1: evaluate every active saved search and send its digest email, if any.
    Returns `(searches_evaluated, alerts_created, emails_sent)`; on any exception, rolls back and
    appends to `errors` instead of raising — a caller runs this and the delivery half regardless
    of which one failed."""
    searches_evaluated = 0
    alerts_created = 0
    emails_sent = 0
    with session_factory() as session:
        try:
            searches_evaluated = (
                session.scalar(
                    select(func.count()).select_from(SavedSearch).where(SavedSearch.status == "active")
                )
                or 0
            )
            created = run_alert_cycle(session, email_port=email_port, now=now)
            alerts_created = len(created)
            emails_sent = sum(1 for alert in created if alert.channel == "email" and alert.status == "sent")
            session.commit()
        except Exception as exc:  # every exception is caught so the tick never dies (task brief)
            session.rollback()
            logger.exception("alert_cycle_failed error_type=%s", type(exc).__name__)
            errors.append(f"alert_cycle: {type(exc).__name__}: {exc}")
    return searches_evaluated, alerts_created, emails_sent


def _run_delivery_half(
    session_factory: sessionmaker[Session],
    *,
    transport: Transport,
    now: dt.datetime,
    errors: list[str],
) -> tuple[int, int, int]:
    """Transaction 2: attempt every due webhook delivery. Returns `(deliveries_attempted,
    deliveries_delivered, deliveries_failed)`; on any exception, rolls back and appends to
    `errors` instead of raising. A delivery `webhooks.attempt_delivery` marks `retrying` (backoff
    scheduled) or terminally `failed` (attempts exhausted) both count as `deliveries_failed` here
    — this is a per-tick attempt count, not a delivery's final outcome."""
    deliveries_attempted = 0
    deliveries_delivered = 0
    deliveries_failed = 0
    with session_factory() as session:
        try:
            due = deliver_pending(session, transport=transport, secret_for=_secret_for, now=now)
            deliveries_attempted = len(due)
            deliveries_delivered = sum(1 for delivery in due if delivery.status == "delivered")
            deliveries_failed = deliveries_attempted - deliveries_delivered
            session.commit()
        except Exception as exc:  # every exception is caught so the tick never dies (task brief)
            session.rollback()
            logger.exception("webhook_delivery_failed error_type=%s", type(exc).__name__)
            errors.append(f"webhook_delivery: {type(exc).__name__}: {exc}")
    return deliveries_attempted, deliveries_delivered, deliveries_failed


def run_alert_tick(
    session_factory: sessionmaker[Session],
    *,
    email_port: EmailPort | None = None,
    transport: Transport | None = None,
    now: dt.datetime | None = None,
) -> AlertTickReport:
    """One tick: the alert half, then the webhook-delivery half, each in its own transaction
    (module docstring). `email_port` defaults to a process-wide `ResendEmailAdapter()` (dry-run
    without `RESEND_API_KEY`); `transport` defaults to a fresh `HttpxTransport()`, closed at the
    end of this call when this function is the one that built it (an injected transport is the
    caller's to close)."""
    started_at = now or dt.datetime.now(dt.UTC)
    effective_email_port = email_port if email_port is not None else _process_default_email_port()
    owns_transport = transport is None
    effective_transport: Transport = transport if transport is not None else HttpxTransport()
    errors: list[str] = []

    searches_evaluated, alerts_created, emails_sent = _run_alert_half(
        session_factory, email_port=effective_email_port, now=started_at, errors=errors
    )
    try:
        deliveries_attempted, deliveries_delivered, deliveries_failed = _run_delivery_half(
            session_factory, transport=effective_transport, now=started_at, errors=errors
        )
    finally:
        if owns_transport and isinstance(effective_transport, HttpxTransport):
            effective_transport.close()

    finished_at = dt.datetime.now(dt.UTC)
    logger.info(
        "alert_tick_completed searches_evaluated=%d alerts_created=%d emails_sent=%d "
        "deliveries_attempted=%d deliveries_delivered=%d deliveries_failed=%d errors=%d",
        searches_evaluated,
        alerts_created,
        emails_sent,
        deliveries_attempted,
        deliveries_delivered,
        deliveries_failed,
        len(errors),
    )
    return AlertTickReport(
        started_at=started_at,
        finished_at=finished_at,
        searches_evaluated=searches_evaluated,
        alerts_created=alerts_created,
        emails_sent=emails_sent,
        deliveries_attempted=deliveries_attempted,
        deliveries_delivered=deliveries_delivered,
        deliveries_failed=deliveries_failed,
        errors=tuple(errors),
    )


def _format_report(report: AlertTickReport) -> str:
    duration_ms = int((report.finished_at - report.started_at).total_seconds() * 1000)
    return (
        "alert_tick "
        f"duration_ms={duration_ms} "
        f"searches_evaluated={report.searches_evaluated} "
        f"alerts_created={report.alerts_created} "
        f"emails_sent={report.emails_sent} "
        f"deliveries_attempted={report.deliveries_attempted} "
        f"deliveries_delivered={report.deliveries_delivered} "
        f"deliveries_failed={report.deliveries_failed} "
        f"errors={len(report.errors)}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m services.alerts.worker`: run one tick against `DATABASE_URL` and write a
    one-line summary to stdout (not `logging`, so the summary is visible even at a quiet log
    level — the scheduler's own job log is expected to capture stdout, task brief). `--dry-run`
    forces a dry-run `ResendEmailAdapter` (even if `RESEND_API_KEY` happens to be set) and a
    `DryRunTransport` that never makes an outbound webhook call. Exit code is non-zero when the
    tick recorded any error, so a scheduler wrapper can treat that as a failed job."""
    parser = argparse.ArgumentParser(
        prog="python -m services.alerts.worker",
        description="Run one alert-evaluation-and-webhook-delivery tick against DATABASE_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Force a dry-run email port (no live Resend call, even if RESEND_API_KEY is set) "
            "and a transport that records webhook posts without sending them."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    engine = get_engine()
    # Additive/no-op against an already-migrated database (services/db/session.py's `init_db`
    # only creates tables that do not exist yet); kept here so this CLI is usable stand-alone
    # against a fresh sqlite target without a separate migration step (services/alerts/README.md).
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    email_port: EmailPort | None = None
    transport: Transport | None = None
    if args.dry_run:
        email_port = ResendEmailAdapter(api_key="")
        transport = DryRunTransport()

    report = run_alert_tick(session_factory, email_port=email_port, transport=transport)
    sys.stdout.write(_format_report(report) + "\n")
    for error in report.errors:
        logger.error("alert_tick_error %s", error)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AlertTickReport",
    "DryRunTransport",
    "HttpxTransport",
    "main",
    "run_alert_tick",
]
