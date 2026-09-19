"""Politeness tests for the shared HTTP layer (docs/20 §4.3, docs/02 §7).

No network: a stub session records what the layer would have sent.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from pipeline.connectors.http import (
    DOMAIN_PLACEHOLDER,
    USER_AGENT_TEMPLATE,
    HttpBlocked,
    HttpFailed,
    PoliteSession,
    user_agent,
)
from pipeline.connectors.registry import Registry


class StubResponse:
    def __init__(
        self, status: int = 200, content: bytes = b"ok", headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers = headers or {}


class StubSession:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append((method, url))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url: str, **kwargs: Any) -> StubResponse:
        return self.request("GET", url, **kwargs)


def session(responses: list[Any], **kw: Any) -> tuple[PoliteSession, StubSession, list[float]]:
    slept: list[float] = []
    stub = StubSession(responses)
    ps = PoliteSession(session=stub, sleep=slept.append, **kw)
    return ps, stub, slept


# ---------------------------------------------------------------- user agent
def test_user_agent_is_browser_like_names_the_platform_and_carries_a_contact_url():
    ua = user_agent()
    assert ua.startswith("Mozilla/5.0")
    assert "Bankable" in ua
    assert "/bot" in ua and "contact" in ua
    assert "infraque.com" in USER_AGENT_TEMPLATE  # the placeholder lives in the template
    assert "{" not in ua and "}" not in ua  # and never reaches the wire (WAFs reject it)
    assert DOMAIN_PLACEHOLDER in ua


def test_user_agent_takes_the_real_domain_once_the_owner_names_it():
    assert "bankable.example" in user_agent("bankable.example")


# ---------------------------------------------------------------- robots.txt
def test_robots_disallow_blocks_the_fetch():
    robots = StubResponse(200, b"User-agent: *\nDisallow: /private/\n")
    ps, stub, _ = session([robots])
    assert ps.allowed_by_robots("https://example.invalid/public/x.xlsx")
    with pytest.raises(HttpBlocked):
        ps.get("https://example.invalid/private/x.xlsx")
    assert len(stub.calls) == 1  # only robots.txt was fetched


def test_robots_is_fetched_once_per_host():
    ps, stub, _ = session([StubResponse(200, b"User-agent: *\nAllow: /\n"), StubResponse(), StubResponse()])
    ps.get("https://example.invalid/a")
    ps.get("https://example.invalid/b")
    assert [c[1] for c in stub.calls].count("https://example.invalid/robots.txt") == 1


def test_a_missing_robots_txt_is_permissive():
    ps, _, _ = session([StubResponse(404, b""), StubResponse()])
    assert ps.get("https://example.invalid/a").status_code == 200


def test_apis_opt_out_of_robots():
    ps, stub, _ = session([StubResponse()])
    ps.get("https://api.example.invalid/v1/search", honour_robots=False)
    assert stub.calls == [("GET", "https://api.example.invalid/v1/search")]


def test_post_never_consults_robots():
    ps, stub, _ = session([StubResponse()])
    ps.post("https://api.example.invalid/v1/search", json={})
    assert stub.calls == [("POST", "https://api.example.invalid/v1/search")]


# ---------------------------------------------------------------- retries and backoff
def test_5xx_is_retried_with_exponential_backoff():
    # a high rate limit keeps the token-bucket waits out of the recorded sleeps
    ps, stub, slept = session(
        [StubResponse(503), StubResponse(500), StubResponse(200)], rate_limits={"api.example.invalid": 1000}
    )
    r = ps.get("https://api.example.invalid/x", honour_robots=False)
    assert r.status_code == 200
    assert len(stub.calls) == 3
    backoffs = [d for d in slept if d > 0.5]
    assert len(backoffs) == 2 and backoffs[1] > backoffs[0]


def test_retry_after_is_honoured():
    ps, _, slept = session(
        [StubResponse(429, headers={"Retry-After": "7"}), StubResponse(200)],
        rate_limits={"api.example.invalid": 1000},
    )
    ps.get("https://api.example.invalid/x", honour_robots=False)
    assert max(slept) >= 7


def test_connection_errors_are_retried_then_fail():
    ps, stub, _ = session([requests.ConnectionError("boom")] * 4, max_attempts=4)
    with pytest.raises(HttpFailed):
        ps.get("https://api.example.invalid/x", honour_robots=False)
    assert len(stub.calls) == 4


def test_a_404_is_not_retried():
    ps, stub, _ = session([StubResponse(404)])
    assert ps.get("https://api.example.invalid/x", honour_robots=False).status_code == 404
    assert len(stub.calls) == 1


def test_a_challenge_page_is_blocked_not_bypassed():
    ps, _, _ = session([StubResponse(200, b"<html><title>Just a moment...</title>")])
    with pytest.raises(HttpBlocked):
        ps.get("https://api.example.invalid/x", honour_robots=False)


# ---------------------------------------------------------------- rate limits
def test_per_host_rate_limit_sleeps_between_requests():
    ps, _, slept = session([StubResponse(), StubResponse()], rate_limits={"api.example.invalid": 0.5})
    ps.get("https://api.example.invalid/a", honour_robots=False)
    ps.get("https://api.example.invalid/b", honour_robots=False)
    assert slept and max(slept) > 1.0  # 0.5 rps => at least 2 s apart


def test_rate_limits_come_from_the_registry():
    reg = Registry()
    assert reg.get("us.iso.ercot.gen_queue").max_rps == 0.5  # docs/02 §7 ERCOT clause 6
    assert reg.get("us.ferc.elibrary").max_rps == 0.5  # FERC <= 0.5 rps
    assert reg.get("news.gdelt.doc").max_rps == pytest.approx(0.2)  # one per five seconds
    assert reg.get("us.iso.pjm.gen_queue").max_rps == pytest.approx(0.1)  # 6 connections/min
    assert reg.get("us.eia.860m").max_rps == 1.0  # default


# ------------------------------------------- 403 challenges and robots UA (audit 2026-09-18 item 4)
def test_a_403_cloudflare_challenge_is_blocked_not_retried():
    """Cloudflare serves its managed challenge as 403 with `cf-mitigated: challenge`; that is a
    block signal, not a transient error, so it must surface as HttpBlocked on the first attempt."""
    ps, stub, slept = session(
        [StubResponse(403, b"<html><title>Just a moment...</title>", headers={"cf-mitigated": "challenge"})]
        + [StubResponse(200)] * 3,
        rate_limits={"api.example.invalid": 1000},
    )
    with pytest.raises(HttpBlocked, match="challenge"):
        ps.get("https://api.example.invalid/x", honour_robots=False)
    assert len(stub.calls) == 1
    assert not [d for d in slept if d > 0.5]  # no backoff sleep: nothing was retried


def test_a_403_with_a_challenge_body_but_no_header_is_blocked_too():
    ps, stub, _ = session(
        [StubResponse(403, b'<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page">')]
    )
    with pytest.raises(HttpBlocked):
        ps.get("https://api.example.invalid/x", honour_robots=False)
    assert len(stub.calls) == 1


def test_a_503_challenge_page_is_blocked_not_retried():
    ps, stub, _ = session(
        [StubResponse(503, b"<html>Attention Required! | Cloudflare</html>")] + [StubResponse(200)] * 3,
        rate_limits={"api.example.invalid": 1000},
    )
    with pytest.raises(HttpBlocked):
        ps.get("https://api.example.invalid/x", honour_robots=False)
    assert len(stub.calls) == 1


def test_a_plain_403_is_a_failure_not_a_block_and_not_retried():
    ps, stub, _ = session([StubResponse(403, b"Forbidden")])
    r = ps.get("https://api.example.invalid/x", honour_robots=False)
    assert r.status_code == 403 and len(stub.calls) == 1


def test_robots_rules_naming_our_bot_win_over_the_wildcard():
    robots = StubResponse(200, b"User-agent: *\nAllow: /\n\nUser-agent: Bankable\nDisallow: /\n")
    ps, stub, _ = session([robots, StubResponse()])
    with pytest.raises(HttpBlocked):
        ps.get("https://example.invalid/anything")
    assert len(stub.calls) == 1


def test_robots_wildcard_is_the_fallback_when_our_bot_is_not_named():
    robots = StubResponse(200, b"User-agent: Googlebot\nAllow: /\n\nUser-agent: *\nDisallow: /private/\n")
    ps, _, _ = session([robots, StubResponse()])
    assert ps.allowed_by_robots("https://example.invalid/public")
    assert not ps.allowed_by_robots("https://example.invalid/private/x")


def test_robots_allow_for_our_bot_overrides_a_wildcard_disallow():
    robots = StubResponse(200, b"User-agent: *\nDisallow: /\n\nUser-agent: Bankable\nAllow: /\n")
    ps, _, _ = session([robots, StubResponse()])
    assert ps.allowed_by_robots("https://example.invalid/data.csv")


def test_the_robots_token_comes_from_the_sessions_user_agent():
    from pipeline.connectors.http import robots_token

    assert robots_token(user_agent()) == "Bankable"
    assert robots_token("Mozilla/5.0 (compatible; OtherBot/1.0; +https://x.example/bot)") == "OtherBot"
