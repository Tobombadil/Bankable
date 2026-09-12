"""Polite HTTP for `egress: plain` connectors (docs/20 §4.3, docs/02 §7, docs/04 O-*).

- Browser-like User-Agent that names the platform and a contact URL (`{{DOMAIN}}` until the owner
  names the product, docs/04 §0 item 6).
- One token bucket per host: `rate_limits` maps host -> max requests per second (default 1 rps).
- Retry with exponential backoff and jitter on 5xx, 429 and connection errors (max 4 attempts);
  `Retry-After` is honoured.
- robots.txt is honoured for every GET a connector marks as robots-relevant (HTML pages and files
  served from a site; API endpoints declare `honour_robots = False`). A disallowed URL raises
  `BlockedError`; a robots.txt that cannot be fetched (403/404/timeout) is treated as permissive,
  which is the standard interpretation.
- No CAPTCHA solving, no challenge bypass: a Cloudflare interstitial is reported as blocked.
"""

from __future__ import annotations

import os
import random
import time
import urllib.robotparser
from typing import Any
from urllib.parse import urlsplit

import requests

#: The platform has no name or domain yet, so the contact URL carries the `{{DOMAIN}}` placeholder
#: of docs/04 §0 item 6. It is rendered DNS-safe before it goes on the wire: braces in a header
#: value are rejected by at least one host we ingest (search.worldbank.org answered 403 to the
#: literal `{{DOMAIN}}` form and 200 to this one, measured 2026-09-12). Set `BANKABLE_DOMAIN` once
#: the owner names the product and the placeholder disappears with no code change.
USER_AGENT_TEMPLATE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 "
    "Bankable/0.2 (+https://{{DOMAIN}}/bot; contact https://{{DOMAIN}}/contact)"
)
DOMAIN_PLACEHOLDER = "domain-placeholder.invalid"


def user_agent(domain: str | None = None) -> str:
    """Render the UA: browser-like, names the platform, carries a contact URL (docs/20 §4.3)."""
    return USER_AGENT_TEMPLATE.replace(
        "{{DOMAIN}}", domain or os.environ.get("BANKABLE_DOMAIN") or DOMAIN_PLACEHOLDER
    )


USER_AGENT = user_agent()
DEFAULT_RPS = 1.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
CHALLENGE_MARKERS = (b"Just a moment", b"cf-chl-", b"__cf_chl", b"Attention Required! | Cloudflare")


class HttpBlocked(Exception):
    """robots.txt disallows the URL, or the host answered with a challenge page."""


class HttpFailed(Exception):
    """Retries exhausted or a non-retryable HTTP error."""


class PoliteSession:
    def __init__(
        self,
        rate_limits: dict[str, float] | None = None,
        default_rps: float = DEFAULT_RPS,
        max_attempts: int = 4,
        timeout: float = 60.0,
        sleep: Any = time.sleep,
        session: requests.Session | None = None,
    ) -> None:
        self.rate_limits = dict(rate_limits or {})
        self.default_rps = default_rps
        self.max_attempts = max_attempts
        self.timeout = timeout
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        self.requests_made = 0

    # ------------------------------------------------------------------ politeness
    def _wait_for_host(self, host: str) -> None:
        rps = self.rate_limits.get(host, self.default_rps)
        interval = 1.0 / rps if rps > 0 else 0.0
        last = self._last.get(host)
        now = time.monotonic()
        if last is not None and now - last < interval:
            self._sleep(interval - (now - last))
        self._last[host] = time.monotonic()

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc}"
        if key in self._robots:
            return self._robots[key]
        rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
        try:
            self._wait_for_host(parts.netloc)
            r = self.session.get(f"{key}/robots.txt", timeout=min(self.timeout, 20))
            self.requests_made += 1
            if r.status_code == 200 and rp is not None:
                rp.parse(r.text.splitlines())
            else:
                rp = None
        except requests.RequestException:
            rp = None
        self._robots[key] = rp
        return rp

    def allowed_by_robots(self, url: str) -> bool:
        rp = self._robots_for(url)
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    # ------------------------------------------------------------------ requests
    def request(
        self, method: str, url: str, *, honour_robots: bool = True, **kwargs: Any
    ) -> requests.Response:
        if honour_robots and method.upper() == "GET" and not self.allowed_by_robots(url):
            raise HttpBlocked(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        kwargs.setdefault("timeout", self.timeout)
        last_error: str = ""
        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_host(host)
            try:
                resp = self.session.request(method, url, **kwargs)
                self.requests_made += 1
            except requests.RequestException as e:
                last_error = repr(e)[:300]
                resp = None
            if resp is not None:
                if resp.status_code < 400 or resp.status_code not in RETRY_STATUSES:
                    if resp.status_code == 200 and any(m in resp.content[:4000] for m in CHALLENGE_MARKERS):
                        raise HttpBlocked(f"challenge page from {host}")
                    return resp
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
            else:
                retry_after = None
            if attempt == self.max_attempts:
                break
            delay = 2.0 ** (attempt - 1) + random.uniform(0, 0.5)  # noqa: S311 - jitter, not security
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
            self._sleep(delay)
        raise HttpFailed(f"{method} {url} failed after {self.max_attempts} attempts: {last_error}")

    def get(self, url: str, *, honour_robots: bool = True, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, honour_robots=honour_robots, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, honour_robots=False, **kwargs)
