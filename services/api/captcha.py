"""Server-side Cloudflare Turnstile verification for the public write endpoints (`/v1/intake/*`;
docs/40-launch-runbook.md §6 gap 1, "captcha is accepted but not verified").

Behaviour, in one place so the routes stay thin:

- `TURNSTILE_SECRET_KEY` set: every `captcha_token` is POSTed to Cloudflare's `siteverify` endpoint
  and the submission is accepted only on an explicit `success: true`. A token Cloudflare rejects, a
  non-2xx status, an unparsable body and a transport error (timeout, DNS, connection reset) are all
  `False` — fail closed. Every rejection is logged at WARNING with Cloudflare's `error-codes` (or the
  exception class), never the token and never the IP.
- `TURNSTILE_SECRET_KEY` unset: `enabled()` is `False`, the routes keep the pre-existing behaviour
  (`captcha_token` is a required, opaque, accepted string) and `warn_if_disabled()` logs exactly one
  WARNING per process on first use so a deployed environment without the key is visible in the logs,
  not silent.
- The visitor's IP is passed to `siteverify` as its optional `remoteip` field — that is the only place
  it goes. Nothing here stores, logs or returns it (CLAUDE.md "store the minimum personal data").
- The HTTP client is injectable (`client_factory`, or a `client=` argument): tests swap in an
  `httpx.Client` over `httpx.MockTransport` and never reach the network. The default client is
  built per call with a short timeout so an unreachable Cloudflare cannot hold an intake request open.

API reference: https://developers.cloudflare.com/turnstile/get-started/server-side-validation/
(POST `secret`, `response`, optional `remoteip` as form fields; JSON reply with `success` and
`error-codes`).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
SECRET_ENV = "TURNSTILE_SECRET_KEY"  # noqa: S105 -- the variable NAME, not a credential
SITE_KEY_ENV = "TURNSTILE_SITE_KEY"  # consumed by the web form once one renders the widget; unused here

#: Upper bound on one siteverify round trip. Cloudflare answers in well under a second; anything
#: slower is treated as a rejection rather than stalling the submitter.
TIMEOUT_SECONDS = 5.0

_disabled_warned = False


def _new_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS)


#: Injection point for tests (`monkeypatch.setattr(captcha, "client_factory", ...)`); production
#: never changes it.
client_factory: Callable[[], httpx.Client] = _new_client


def secret_key() -> str | None:
    """`TURNSTILE_SECRET_KEY` read at the point of use (like `SESSION_SECRET` and `PLATFORM_POSTURE`
    elsewhere in this tree), so tests can set and clear it per case; blank counts as unset."""
    value = os.environ.get(SECRET_ENV, "").strip()
    return value or None


def enabled() -> bool:
    return secret_key() is not None


def warn_if_disabled() -> bool:
    """Logs the one-per-process "verification disabled" warning the first time an unverified
    submission is accepted. Returns whether a warning was emitted by this call (tests)."""
    global _disabled_warned  # once-per-process latch: the point of the function
    if enabled() or _disabled_warned:
        return False
    _disabled_warned = True
    logger.warning(
        "captcha_verification_disabled env=%s intake captcha_token is accepted without verification;"
        " honeypot and the per-IP rate limit are the only abuse controls",
        SECRET_ENV,
    )
    return True


def verify(
    token: str,
    remote_ip: str | None,
    *,
    client: httpx.Client | None = None,
    secret: str | None = None,
) -> bool:
    """`True` only when Cloudflare's siteverify answers `success: true` for `token`.

    `remote_ip` is forwarded as `remoteip` when known (Cloudflare uses it to tighten the check) and
    is otherwise unused: not logged, not stored, not part of the return value. `secret` defaults to
    `TURNSTILE_SECRET_KEY`; with neither, the token cannot be verified and the answer is `False`.
    """
    secret_value = secret if secret is not None else secret_key()
    if not secret_value or not token:
        return False
    form: dict[str, str] = {"secret": secret_value, "response": token}
    if remote_ip and remote_ip != "unknown":
        form["remoteip"] = remote_ip

    try:
        if client is None:
            with client_factory() as owned:
                response = owned.post(SITEVERIFY_URL, data=form)
        else:
            response = client.post(SITEVERIFY_URL, data=form)
    except httpx.HTTPError as exc:
        logger.warning("captcha_siteverify_unreachable error=%s", type(exc).__name__)
        return False

    if response.status_code != 200:
        logger.warning("captcha_siteverify_http_error status=%s", response.status_code)
        return False
    try:
        payload: Any = response.json()
    except ValueError:
        logger.warning("captcha_siteverify_bad_body status=%s", response.status_code)
        return False
    if not isinstance(payload, dict):
        logger.warning("captcha_siteverify_bad_body status=%s", response.status_code)
        return False
    if payload.get("success") is True:
        return True
    codes = payload.get("error-codes")
    logger.warning(
        "captcha_rejected error_codes=%s", ",".join(map(str, codes)) if isinstance(codes, list) else "-"
    )
    return False
