"""Polite HTTP for `egress: plain` connectors (docs/20 §4.3, docs/02 §7, docs/04 O-*).

- Browser-like User-Agent that names the platform and a contact URL on the deployment's domain
  (`BANKABLE_DOMAIN`, which Compose sets from `DOMAIN`: inframation.io in staging and production).
- One token bucket per host: `rate_limits` maps host -> max requests per second (default 1 rps).
- Retry with exponential backoff and jitter on 5xx, 429 and connection errors (max 4 attempts);
  `Retry-After` is honoured.
- robots.txt is honoured for every GET a connector marks as robots-relevant (HTML pages and files
  served from a site; API endpoints declare `honour_robots = False`). Rules are evaluated for our
  own product token first (`robots_token(...)`, read from the session's User-Agent: `Inframation`),
  with the `*` group as the fallback — the standard robots.txt precedence, which the stdlib
  parser only applies when it is handed the token rather than the full browser-like UA string
  (audit 2026-09-18 item 4). A disallowed URL raises `HttpBlocked`; a robots.txt that cannot be
  fetched (403/404/timeout) is treated as permissive, which is the standard interpretation.
- No CAPTCHA solving, no challenge bypass: a Cloudflare interstitial is reported as blocked
  (`HttpBlocked`) whatever status it arrives with — 200, 403 (`cf-mitigated: challenge`) or 503 —
  and is never retried.
- `stream=True` leaves a large body unread for the caller to copy in chunks (the 443 MB ECHO
  Exporter, docs/25 §3.9). Its challenge check reads the body only when the server labels it HTML,
  since an interstitial is a small HTML page; `cf-mitigated` is checked either way.
- A certificate that does not verify is `HttpTlsUnverified`, raised at once and never retried: it
  does not heal in seconds, and four handshakes a run, five runs a tick, only repeat it (2026-10-24,
  docs/28 lane F5). The message names the host and OpenSSL's reason. Verification is never turned
  off. A host that leaves its issuing CA out of the handshake (the Permitting Dashboard since its
  2026-10-14 certificate) is reached with `ca_bundle_with(<that CA's PEM>)` as the request's
  `verify`: the bundle requests would use anyway, plus that one certificate, which must itself
  chain to a root already in the bundle. A browser fetches such a CA from the certificate's
  "CA Issuers" URL; OpenSSL does not, so Python answers "unable to get local issuer certificate".
- No URL in an error carries its query string (`safe_url`, `strip_queries`): a key passed as a
  query parameter would otherwise reach the run record, `source.last_error` and the admin pages.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import random
import re
import tempfile
import time
import urllib.robotparser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import certifi
import requests

#: The contact URL is on the domain the deployment serves: `BANKABLE_DOMAIN`, which Compose sets
#: from `DOMAIN` (inframation.io in staging and production; owner, 2026-10-16). `pipeline` may not
#: import `services.api.common.DOMAIN` (infra/importlinter.ini), so a process without the setting
#: (tests, a bare checkout) sends `DOMAIN_PLACEHOLDER`. The template carries that placeholder itself
#: rather than a format marker, so it is DNS-safe even unrendered: braces in a header value are
#: rejected by at least one host we ingest (search.worldbank.org answered 403 to the literal
#: `{{DOMAIN}}` form and 200 to this one, measured 2026-09-12).
DOMAIN_PLACEHOLDER = "domain-placeholder.invalid"
USER_AGENT_TEMPLATE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 "
    f"Inframation/0.2 (+https://{DOMAIN_PLACEHOLDER}/bot; contact https://{DOMAIN_PLACEHOLDER}/contact)"
)


def user_agent(domain: str | None = None) -> str:
    """Render the UA: browser-like, names the platform, carries a contact URL (docs/20 §4.3)."""
    return USER_AGENT_TEMPLATE.replace(
        DOMAIN_PLACEHOLDER, domain or os.environ.get("BANKABLE_DOMAIN") or DOMAIN_PLACEHOLDER
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
    `+https://...` contact URL (`Inframation` in the UA above). Falls back to the first token."""
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


def _challenge_body(resp: Any, stream: bool) -> bytes:
    """What `is_challenge` scans: the whole body, or for a streamed response only an HTML one."""
    if not stream:
        return bytes(resp.content)
    ctype = str(resp.headers.get("Content-Type") or "").lower()
    return bytes(resp.content) if "html" in ctype else b""


class HttpBlocked(Exception):
    """robots.txt disallows the URL, or the host answered with a challenge page."""


class HttpFailed(Exception):
    """Retries exhausted or a non-retryable HTTP error."""


class HttpTlsUnverified(HttpFailed):
    """The host's TLS certificate did not verify against the CA bundle the request used. Its own
    class, so the scheduler's retry rule (`infra/scheduler/jobs.py` TRANSIENT_ERROR_CLASSES, which
    names `HttpFailed` but not this) records it once instead of five times."""


# ------------------------------------------------------------------ redaction of URLs in errors
_URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s'\"<>()\\]+")
#: A path with a query string, as urllib3 writes it in "Max retries exceeded with url: /p?q=1".
_PATH_QUERY_RE = re.compile(r"(?<![\w/:.])(/[^\s?'\"<>()\\]*)\?[^\s'\"<>()\\]*")


def safe_url(url: str) -> str:
    """`url` without credentials, query string or fragment: `?…` marks a query that was there."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.split("?", 1)[0]
    if not parts.scheme or not parts.netloc:
        return url.split("?", 1)[0] + ("?…" if "?" in url else "")
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", "")) + ("?…" if parts.query else "")


def strip_queries(text: str) -> str:
    """Every URL in `text` through `safe_url`, and every bare `/path?query` cut at its `?`."""
    text = _URL_RE.sub(lambda m: safe_url(m.group(0)), text)
    return _PATH_QUERY_RE.sub(lambda m: m.group(1) + "?…", text)


# ------------------------------------------------------------------ TLS
_VERIFY_REASON_RE = re.compile(r"certificate verify failed:\s*([^()'\"\]\\]+)", re.IGNORECASE)


def tls_verify_failure(exc: BaseException) -> str | None:
    """OpenSSL's reason when `exc` is a certificate that did not verify ("unable to get local issuer
    certificate", "certificate has expired", "Hostname mismatch, ..."), else None. Other TLS
    errors (a reset or EOF during the handshake) are not this, and stay retryable."""
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" not in text and "certificate verify failed" not in text.lower():
        return None
    m = _VERIFY_REASON_RE.search(text)
    reason = m.group(1).strip(" .,;:") if m else ""
    return reason or "certificate verify failed"


def base_ca_bundle() -> str:
    """The CA file `requests` verifies against when a request names none: `REQUESTS_CA_BUNDLE`,
    then `CURL_CA_BUNDLE` (a file, not a directory), then certifi's, which requests ships with."""
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        value = (os.environ.get(name) or "").strip()
        if value and os.path.isfile(value):
            return value
    return str(certifi.where())


_PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"
#: The bundles this process wrote: key -> (path, SHA-256 of the bytes written).
_BUNDLES: dict[tuple[str, ...], tuple[str, str]] = {}
#: This process's own directory for them (`tempfile.mkdtemp`: mode 0700, a fresh name), never a
#: shared one, so no other user of the host can put a CA file where a fetch will trust it.
_BUNDLE_DIR: str | None = None


def ca_bundle_with(*pem_paths: str | os.PathLike[str]) -> str:
    """A CA file for a request's `verify`: `base_ca_bundle()` plus the certificates in `pem_paths`
    (each file one or more PEM certificates, comments allowed), written once per process into a
    private directory and checked against the digest of what was written each time it is handed out.
    Passing it as `verify` also stops `requests` from swapping in `REQUESTS_CA_BUNDLE`, which is why
    the base is that bundle when it is set.

    Adding an issuing CA does not widen trust: it verifies only when it chains to a root already in
    the base bundle (the connector's test pins that for each file it ships)."""
    global _BUNDLE_DIR
    base = base_ca_bundle()
    key = (base, *(str(p) for p in pem_paths))
    cached = _BUNDLES.get(key)
    if cached is not None:
        path_s, digest = cached
        try:
            if hashlib.sha256(pathlib.Path(path_s).read_bytes()).hexdigest() == digest:
                return path_s
        except OSError:
            pass  # gone: written again below
    parts = [pathlib.Path(base).read_bytes().rstrip(b"\n") + b"\n"]
    for p in pem_paths:
        extra = pathlib.Path(p).read_bytes()
        if _PEM_BEGIN not in extra:
            raise ValueError(f"{p} holds no PEM certificate")
        parts.append(extra.rstrip(b"\n") + b"\n")
    blob = b"".join(parts)
    digest = hashlib.sha256(blob).hexdigest()
    if _BUNDLE_DIR is None or not os.path.isdir(_BUNDLE_DIR):
        _BUNDLE_DIR = tempfile.mkdtemp(prefix="inframation-ca-")
    path = pathlib.Path(_BUNDLE_DIR) / f"bundle-{digest[:16]}.pem"
    fd, tmp = tempfile.mkstemp(dir=_BUNDLE_DIR, prefix=".bundle-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise
    _BUNDLES[key] = (str(path), digest)
    return str(path)


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

    def crawl_delay(self, url: str) -> float | None:
        """The `Crawl-delay` the host's robots.txt declares for our product token (else for `*`),
        in seconds, or None when it declares none or has no robots.txt. A caller that honours it
        lowers its rate limit for the host to one request per that many seconds."""
        rp = self._robots_for(url)
        if rp is None:
            return None
        delay = rp.crawl_delay(self.robots_agent)
        return float(delay) if delay is not None else None

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
            raise HttpBlocked(f"robots.txt disallows {safe_url(url)}")
        host = urlsplit(url).netloc
        kwargs.setdefault("timeout", self.timeout)
        stream = bool(kwargs.get("stream"))
        last_error: str = ""
        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_host(host)
            try:
                resp = self.session.request(method, url, **kwargs)
                self.requests_made += 1
            except requests.exceptions.SSLError as e:
                reason = tls_verify_failure(e)
                if reason is not None:
                    raise HttpTlsUnverified(
                        f"{method} {safe_url(url)}: the TLS certificate of {urlsplit(url).hostname} "
                        f"did not verify ({reason}); not retried"
                    ) from e
                last_error = strip_queries(repr(e))[:300]
                resp = None
            except requests.RequestException as e:
                last_error = strip_queries(repr(e))[:300]
                resp = None
            if resp is not None:
                if is_challenge(resp.status_code, resp.headers, _challenge_body(resp, stream)):
                    # A challenge is a block signal, not a transient error: no retry, no bypass.
                    raise HttpBlocked(f"challenge page from {host} (HTTP {resp.status_code})")
                if resp.status_code < 400 or resp.status_code not in RETRY_STATUSES:
                    return resp
                last_error = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
                if stream and hasattr(resp, "close"):
                    resp.close()  # release the unread connection before the retry
            else:
                retry_after = None
            if attempt == self.max_attempts:
                break
            delay = 2.0 ** (attempt - 1) + random.uniform(0, 0.5)  # noqa: S311 - jitter, not security
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
            self._sleep(delay)
        raise HttpFailed(f"{method} {safe_url(url)} failed after {self.max_attempts} attempts: {last_error}")

    def get(self, url: str, *, honour_robots: bool = True, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, honour_robots=honour_robots, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, honour_robots=False, **kwargs)
