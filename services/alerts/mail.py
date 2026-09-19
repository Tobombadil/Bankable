"""What every alert or digest email must carry before it may leave the process (docs/50-audit-
2026-09-18.md §3.1 "no legal entity, a placeholder postal address, no unsubscribe headers, so no
email can be sent lawfully"; docs/13-legal-outreach-and-social.md §1 CAN-SPAM, §4 CASL; docs/32
§2.2 "Email footer (every send)"):

1. `List-Unsubscribe` with both a `mailto:` and an `https:` target, and
   `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058) — the headers Gmail and Yahoo
   require of bulk senders since February 2024. The `https:` target is the existing
   `/v1/alerts/unsubscribe` route (`services/api/unsubscribe_routes.py`), which accepts the
   RFC 8058 `POST` with the token in the query string as well as the mail-client `GET`.
2. A postal address line naming the legal sender, rendered from `SENDER_LEGAL_NAME` and
   `SENDER_POSTAL_ADDRESS`. The legal entity and address were decided 2026-09-18 (docs/00-PLAN.md
   decisions log) but live only in the host's env file, never in the repo: in development they
   render as visible placeholders; in production (`ENVIRONMENT=production`, alias `APP_ENV`) or
   whenever a port would really send, a missing value makes `sender_identity` raise, log and
   count instead of sending an unlawful message.
3. The delayed-data notice, which the digest body renders from the account's entitlement.

`services.api.auth.EmailPort.send` has no `headers` parameter and `services/api/auth.py` is not
this module's to change, so this module carries its own `AlertMailer` protocol (the same call
plus `headers`) and its own Resend adapter. `deliver` accepts the legacy port too, but only when
that port is a dry run — a port that could really send without the headers is refused.
"""

from __future__ import annotations

import inspect
import logging
import os
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

from services.api.audit import is_production
from services.api.auth import SentEmail
from services.api.common import API_HOST, DOMAIN, WEB_HOST

logger = logging.getLogger(__name__)

FROM_ADDRESS = f"alerts@{DOMAIN}"
REPLY_TO_ADDRESS = f"hello@{DOMAIN}"
UNSUBSCRIBE_MAILBOX = f"unsubscribe@{DOMAIN}"
ONE_CLICK_POST_VALUE = "List-Unsubscribe=One-Click"


def product_name() -> str:
    return os.environ.get("PRODUCT_NAME", "").strip() or "Infraque"


class SenderIdentityMissing(RuntimeError):
    """`SENDER_LEGAL_NAME` or `SENDER_POSTAL_ADDRESS` is unset where a real send could happen."""


class MailPortCannotCarryHeaders(RuntimeError):
    """The given port's `send` has no `headers` parameter and is not a dry run."""


@dataclass(frozen=True)
class SenderIdentity:
    legal_name: str
    postal_address: str
    #: True when either value is a development placeholder rather than an operator-set value.
    placeholder: bool = False

    @property
    def postal_line(self) -> str:
        return f"{self.legal_name}, {self.postal_address}"


_counters: dict[str, int] = {"refused_sends": 0}


def refused_send_count() -> int:
    """How many sends this process refused for a missing sender identity (the "count" in
    "raise, log, count"); exported to the worker's tick report by `services/alerts/worker.py`."""
    return _counters["refused_sends"]


def reset_counters() -> None:
    _counters["refused_sends"] = 0


def sender_identity(*, strict: bool) -> SenderIdentity:
    """The legal sender line. `strict` is "a real message could leave": production, or a port
    that is not a dry run. Non-strict callers get placeholders that are obviously placeholders
    (square brackets naming the unset variable) so a dry-run body still shows where the line
    goes and never looks finished."""
    legal_name = os.environ.get("SENDER_LEGAL_NAME", "").strip()
    postal_address = os.environ.get("SENDER_POSTAL_ADDRESS", "").strip()
    if legal_name and postal_address:
        return SenderIdentity(legal_name=legal_name, postal_address=postal_address)
    missing = [
        name
        for name, value in (("SENDER_LEGAL_NAME", legal_name), ("SENDER_POSTAL_ADDRESS", postal_address))
        if not value
    ]
    if strict:
        _counters["refused_sends"] += 1
        logger.error(
            "alert_send_refused missing=%s refused_total=%d", ",".join(missing), _counters["refused_sends"]
        )
        raise SenderIdentityMissing(
            f"refusing to send: {', '.join(missing)} unset "
            "(owner decision D5: no legal entity or address yet)"
        )
    return SenderIdentity(
        legal_name=legal_name or "[SENDER_LEGAL_NAME unset]",
        postal_address=postal_address or "[SENDER_POSTAL_ADDRESS unset]",
        placeholder=True,
    )


def unsubscribe_targets(token: str) -> tuple[str, str]:
    """`(mailto, https)` for one alert's own token. The `https` target is the API route, since a
    mail provider's one-click `POST` goes straight there; the human-facing confirmation page
    (`WEB_HOST/unsubscribe`) stays the link in the body."""
    encoded = quote(token, safe="")
    mailto = f"mailto:{UNSUBSCRIBE_MAILBOX}?subject=unsubscribe%20{encoded}"
    https = f"{API_HOST}/v1/alerts/unsubscribe?token={encoded}"
    return mailto, https


def unsubscribe_headers(token: str) -> dict[str, str]:
    mailto, https = unsubscribe_targets(token)
    return {
        "List-Unsubscribe": f"<{mailto}>, <{https}>",
        "List-Unsubscribe-Post": ONE_CLICK_POST_VALUE,
    }


def delayed_data_notice(entitlement: str) -> str:
    """docs/32 §2.2's footer plus the tier statement the audit found missing: a reader must be
    able to tell whether what they are looking at is live or delayed."""
    if entitlement in ("pro", "api", "admin"):
        return (
            "Data notice: this digest is sent at your account's live tier. The public site "
            f"({WEB_HOST}) runs behind the live tier, so items here may not yet appear there."
        )
    return (
        "Data notice: your account is on the delayed public tier. Items in this digest are "
        "published on the public schedule and may lag the live tier; live alerts are a paid feature."
    )


@dataclass(frozen=True)
class OutboundEmail:
    to: str
    subject: str
    body: str
    headers: dict[str, str] = field(default_factory=dict)


class AlertMailer(Protocol):
    """`services.api.auth.EmailPort` plus `headers`. `dry_run` is read by `deliver` and by
    `services/alerts/evaluate.py` to decide whether a missing sender identity is fatal."""

    @property
    def dry_run(self) -> bool: ...

    def send(self, *, to: str, subject: str, body: str, headers: dict[str, str]) -> SentEmail: ...


class ResendAlertMailer:
    """`services.api.auth.ResendEmailAdapter` with headers and a reply-to. Dry-run whenever
    `RESEND_API_KEY` is unset (CLAUDE.md: secrets from environment only) — the send is recorded in
    `self.sent` and a synthetic id returned, so no test needs a live account (docs/04 E-6)."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("RESEND_API_KEY")
        self.sent: list[SentEmail] = []
        self.sent_headers: list[dict[str, str]] = []

    @property
    def dry_run(self) -> bool:
        return not self.api_key

    def send(self, *, to: str, subject: str, body: str, headers: dict[str, str]) -> SentEmail:
        if self.dry_run:
            record = SentEmail(
                to=to,
                subject=subject,
                body=body,
                provider_message_id=f"dryrun_{secrets.token_hex(8)}",
                dry_run=True,
            )
        else:  # pragma: no cover — no live Resend account in this environment (docs/04 E-6)
            import httpx

            payload: dict[str, Any] = {
                "from": f"{product_name()} <{FROM_ADDRESS}>",
                "reply_to": REPLY_TO_ADDRESS,
                "to": [to],
                "subject": subject,
                "text": body,
                "headers": dict(headers),
            }
            resp = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=10.0,
            )
            resp.raise_for_status()
            record = SentEmail(
                to=to,
                subject=subject,
                body=body,
                provider_message_id=str(resp.json().get("id", "")),
                dry_run=False,
            )
        self.sent.append(record)
        self.sent_headers.append(dict(headers))
        return record


def port_is_dry_run(port: object) -> bool:
    """A port with no `dry_run` attribute is assumed to really send — the conservative reading."""
    return bool(getattr(port, "dry_run", False))


def deliver(port: Any, message: OutboundEmail) -> SentEmail:
    """Hands `message` to `port.send`. A port whose `send` takes `headers` gets them; a legacy
    `EmailPort` (no `headers` parameter) is accepted only while it is a dry run, because a
    message that really left without `List-Unsubscribe` would be the audit finding again."""
    if not message.headers.get("List-Unsubscribe") or not message.headers.get("List-Unsubscribe-Post"):
        raise ValueError("alert emails must carry List-Unsubscribe and List-Unsubscribe-Post headers")
    parameters = inspect.signature(port.send).parameters
    if "headers" in parameters:
        sent: SentEmail = port.send(
            to=message.to, subject=message.subject, body=message.body, headers=dict(message.headers)
        )
        return sent
    if port_is_dry_run(port):
        logger.warning("legacy_email_port_dry_run headers_dropped=%s", sorted(message.headers))
        legacy: SentEmail = port.send(to=message.to, subject=message.subject, body=message.body)
        return legacy
    raise MailPortCannotCarryHeaders(
        f"{type(port).__name__}.send has no 'headers' parameter and is not a dry run"
    )


def strict_for(port: Any) -> bool:
    return is_production() or not port_is_dry_run(port)


__all__ = [
    "AlertMailer",
    "MailPortCannotCarryHeaders",
    "OutboundEmail",
    "ResendAlertMailer",
    "SenderIdentity",
    "SenderIdentityMissing",
    "delayed_data_notice",
    "deliver",
    "product_name",
    "refused_send_count",
    "reset_counters",
    "sender_identity",
    "strict_for",
    "unsubscribe_headers",
    "unsubscribe_targets",
]
