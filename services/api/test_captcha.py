"""`services/api/captcha.py`: Turnstile siteverify over an injected `httpx.MockTransport` — no network,
ever. Route-level behaviour (400 envelope, disabled mode, idempotent replay) lives in
`tests/test_api_admin_posts.py`; this file pins the verifier itself."""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs

import httpx
import pytest

from services.api import captcha

SECRET = "sk-test"  # noqa: S105 -- a fixture value for the fake siteverify, not a credential
LEAKY_SECRET = "sk-very-secret"  # noqa: S105 -- same; asserted absent from the logs


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv(captcha.SECRET_ENV, raising=False)
    monkeypatch.setattr(captcha, "_disabled_warned", False)
    monkeypatch.setattr(captcha, "client_factory", _refusing_factory)


def _refusing_factory() -> httpx.Client:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call to {request.url}")

    return httpx.Client(transport=httpx.MockTransport(_handler))


def _fake_client(reply, *, status: int = 200, seen: list[httpx.Request] | None = None) -> httpx.Client:
    def _handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            return httpx.Response(status, text=reply)
        return httpx.Response(status, json=reply)

    return httpx.Client(transport=httpx.MockTransport(_handler))


def _form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def test_success_posts_secret_token_and_ip_as_form_fields():
    seen: list[httpx.Request] = []
    client = _fake_client({"success": True, "hostname": "infraque.com"}, seen=seen)
    assert captcha.verify("tok-1", "203.0.113.9", client=client, secret=SECRET) is True
    assert len(seen) == 1
    assert str(seen[0].url) == captcha.SITEVERIFY_URL
    assert seen[0].method == "POST"
    assert _form(seen[0]) == {"secret": SECRET, "response": "tok-1", "remoteip": "203.0.113.9"}


def test_secret_defaults_to_the_environment(monkeypatch):
    monkeypatch.setenv(captcha.SECRET_ENV, f"  {SECRET}  ")
    seen: list[httpx.Request] = []
    assert captcha.verify("tok", None, client=_fake_client({"success": True}, seen=seen)) is True
    assert _form(seen[0])["secret"] == SECRET
    assert captcha.enabled() is True


def test_unknown_ip_is_not_forwarded():
    for ip in (None, "", "unknown"):
        seen: list[httpx.Request] = []
        captcha.verify("tok", ip, client=_fake_client({"success": True}, seen=seen), secret=SECRET)
        assert "remoteip" not in _form(seen[0]), ip


def test_rejection_is_false_and_logs_codes_but_never_the_token_or_ip(caplog):
    client = _fake_client({"success": False, "error-codes": ["invalid-input-response"]})
    with caplog.at_level(logging.WARNING, logger="services.api.captcha"):
        assert (
            captcha.verify("secret-token-value", "198.51.100.4", client=client, secret=LEAKY_SECRET) is False
        )
    assert "captcha_rejected" in caplog.text
    assert "invalid-input-response" in caplog.text
    assert "secret-token-value" not in caplog.text
    assert "198.51.100.4" not in caplog.text
    assert LEAKY_SECRET not in caplog.text


@pytest.mark.parametrize(
    ("reply", "status", "marker"),
    [
        ({"success": False}, 200, "captcha_rejected error_codes=-"),
        ({"success": "true"}, 200, "captcha_rejected"),  # only the boolean counts
        ({"success": True}, 500, "captcha_siteverify_http_error status=500"),
        ("<html>bad gateway</html>", 200, "captcha_siteverify_bad_body"),
        (json.dumps([1, 2]), 200, "captcha_siteverify_bad_body"),
        (httpx.ConnectError("boom"), 200, "captcha_siteverify_unreachable error=ConnectError"),
        (httpx.ReadTimeout("slow"), 200, "captcha_siteverify_unreachable error=ReadTimeout"),
    ],
)
def test_every_non_success_path_fails_closed(caplog, reply, status, marker):
    with caplog.at_level(logging.WARNING, logger="services.api.captcha"):
        assert (
            captcha.verify("tok", "203.0.113.1", client=_fake_client(reply, status=status), secret=SECRET)
            is False
        )
    assert marker in caplog.text


def test_missing_secret_or_token_is_false_without_a_request():
    seen: list[httpx.Request] = []
    client = _fake_client({"success": True}, seen=seen)
    assert captcha.verify("tok", None, client=client, secret="") is False
    assert captcha.verify("tok", None, client=client) is False  # env unset by the fixture
    assert captcha.verify("", None, client=client, secret=SECRET) is False
    assert seen == []


def test_default_client_comes_from_the_injectable_factory(monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr(captcha, "client_factory", lambda: _fake_client({"success": True}, seen=seen))
    assert captcha.verify("tok", None, secret=SECRET) is True
    assert len(seen) == 1


def test_disabled_warning_fires_once_per_process(caplog):
    assert captcha.enabled() is False
    with caplog.at_level(logging.WARNING, logger="services.api.captcha"):
        assert captcha.warn_if_disabled() is True
        assert captcha.warn_if_disabled() is False
    assert caplog.text.count("captcha_verification_disabled") == 1


def test_disabled_warning_is_silent_when_the_key_is_set(monkeypatch, caplog):
    monkeypatch.setenv(captcha.SECRET_ENV, SECRET)
    with caplog.at_level(logging.WARNING, logger="services.api.captcha"):
        assert captcha.warn_if_disabled() is False
    assert "captcha_verification_disabled" not in caplog.text
