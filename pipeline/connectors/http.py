"""Polite HTTP for `egress: plain` connectors (docs/20 §4.3, docs/02 §7, docs/04 O-*).

- Browser-like User-Agent that names the platform and a contact URL (`infraque.com` until the owner
  names the product, docs/04 §0 item 6).
- One token bucket per host: `rate_limits` maps host -> max requests per second (default 1 rps).
- Retry with exponential backoff and jitter on 5xx, 429 and connection errors (max 4 attempts);
  `Retry-After` is honoured.
- robots.txt is honoured for every GET a connector marks as robots-relevant (HTML pages and files
  served from a site; API endpoints declare `honour_robots = False`). Rules are evaluated for our
  own product token first (`robots_token(...)`, read from the session's User-Agent: `Bankable`),
  with the `*` group as the fallback — the standard robots.txt precedence, which the stdlib
  parser only applies when it is handed the token rather than the full browser-like UA string
  (audit 2026-09-18 item 4). A disallowed URL raises `HttpBlocked`; a robots.txt that cannot be
  fetched (403/404/timeout) is treated as permissive, which is the standard interpretation.
- No CAPTCHA solving, no challenge bypass: a Cloudflare interstitial is reported as blocked
  (`HttpBlocked`) whatever status it arrives with — 200, 403 (`cf-mitigated: challenge`) or 503 —
  and is never retried.
"""

from __future__ import annotations

import os
import random
import re
import time
import urllib.robotparser
from typing import Any
from urllib.parse import urlsplit

import requests

#: The platform has no name or domain yet, so the contact URL carries the `infraque.com` placeholder
#: of docs/04 §0 item 6. It is rendered DNS-safe before it goes on the wire: braces in a header
#: value are rejected by at least one host we ingest (search.worldbank.org answered 403 to the
#: literal `infraque.com` form and 200 to this one, measured 2026-09-12). Set `BANKABLE_DOMAIN` once
#: the owner names the product and the placeholder disappears with no code change.
USER_AGENT_TEMPLATE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 "
    "Bankable/0.2 (+https://infraque.com/bot; contact https://infraque.com/contact)"
)
DOMAIN_PLACEHOLDER = "domain-placeholder.invalid"


def user_agent(domain: str | None = None) -> str:
    """Render the UA: browser-like, names the platform, carries a contact URL (docs/20 §4.3)."""
    return USER_AGENT_TEMPLATE.replace(
        "infraque.com", domain or os.environ.get("BANKABLE_DOMAIN") or DOMAIN_PLACEHOLDER
    )


USER_AGENT = user_agent()
DEFAULT_RPS = 1.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
CHALLENGE_MARKERS = (
    b"Just a moment",
    b"cf-chl-",
    b"__cf_chl",
    b"/cdn-cgi/challenge-platform/",
    b"Attention Required! | Cloudflare",
)
#: Statuses a challenge interstitial is known to ship with. Anything else with a marker in the
#: body is still a challenge; the status set only bounds how much of the body is scanned.
CHALLENGE_STATUSES = frozenset({200, 403, 429, 503})
_TOKEN_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)/[0-9][0-9.]*\s*(?:\(\+|;\s*\+)https?://")


def robots_token(ua: str) -> str:
    """The product token robots.txt groups name us by: the `Name/version` immediately before the
    `+https://...` contact URL (`Bankable` in the UA above). Falls back to the first token."""
    m = _TOKEN_RE.search(ua)
    if m:
        return m.group(1)
    return ua.split("/", 1)[0].split()[0] if ua.strip() else "*"


def is_challenge(status_code: int, headers: Any, content: bytes) -> bool:
    """Cloudflare-style managed challenge: `cf-mitigated: challenge` (any status), or one of the
    interstitial markers in the first 4 kB of the body on a status a challenge is served with."""
    try:
        mitigated = str(headers.get("cf-mitigated", "")).lower()
    except AttributeError:
        mitigated = ""
    if mitigated == "challenge":
        return True
    if status_code not in CHALLENGE_STATUSES:
        return False
    head = content[:4000]
    return any(m in head for m in CHALLENGE_MARKERS)


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

    @property
    def robots_agent(self) -> str:
        """Our product token, from whatever User-Agent this session actually sends."""
        return robots_token(str(self.session.headers.get("User-Agent", USER_AGENT)))

    def allowed_by_robots(self, url: str) -> bool:
        rp = self._robots_for(url)
        # `RobotFileParser.can_fetch` looks for a group naming this token (substring match,
        # case-insensitive) and only then falls back to the `*` group — passing the full
        # browser-like UA string defeated that lookup, because the parser keys on the text
        # before the first `/`, i.e. `Mozilla`.
        return True if rp is None else rp.can_fetch(self.robots_agent, url)

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
                if is_challenge(resp.status_code, resp.headers, resp.content):
                    # A challenge is a block signal, not a transient error: no retry, no bypass.
                    raise HttpBlocked(f"challenge page from {host} (HTTP {resp.status_code})")
                if resp.status_code < 400 or resp.status_code not in RETRY_STATUSES:
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
