"""`services/api/auth.py::session_secret` — the committed dev secret is only ever used in dev-like
environments; staging/production without a real `SESSION_SECRET` must fail at startup (audit
2026-09-18 §3.1, "the session secret then falls back to a constant committed in the repo")."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

from services.api import auth

# Obvious low-entropy fake, 40 chars: long enough to pass the length floor, never a real value.
FAKE_SECRET = "test-only-session-secret-0123456789abcdef"


def test_dev_environments_fall_back_to_the_dev_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    for environment in ("", "dev", "development", "local", "test", "ci", "Development"):
        if environment:
            monkeypatch.setenv("ENVIRONMENT", environment)
        else:
            monkeypatch.delenv("ENVIRONMENT", raising=False)
        assert auth.session_secret() == auth._DEV_SESSION_SECRET, environment


def test_dev_environment_prefers_an_explicit_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("SESSION_SECRET", FAKE_SECRET)
    assert auth.session_secret() == FAKE_SECRET


@pytest.mark.parametrize("environment", ["production", "staging", "Production", "preview", "anything-else"])
def test_non_dev_environment_without_secret_raises(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SESSION_SECRET must be set to at least 32 characters"):
        auth.session_secret()


def test_non_dev_environment_with_short_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SESSION_SECRET", "short-fake-secret")
    with pytest.raises(RuntimeError, match="17 chars"):
        auth.session_secret()


def test_non_dev_environment_with_real_length_secret_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("SESSION_SECRET", FAKE_SECRET)
    assert auth.session_secret() == FAKE_SECRET
    assert auth._serializer().dumps({"sid": "x"}) != auth._verification_serializer().dumps({"sid": "x"})


def test_production_import_fails_at_startup_without_secret() -> None:
    """The module-level `session_secret()` call: a production process cannot even import auth
    (and therefore cannot start `services.api.app`) on the dev constant. Run in a fresh
    interpreter — reloading the module in-process would rebind classes 58 other modules hold."""
    env = {k: v for k, v in os.environ.items() if k not in ("SESSION_SECRET", "ENVIRONMENT")}
    env["ENVIRONMENT"] = "production"
    result = subprocess.run(
        [sys.executable, "-c", "import services.api.auth"],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(__file__).resolve().parents[1],
        env=env,
        check=False,
    )
    assert result.returncode != 0
    assert "RuntimeError: SESSION_SECRET must be set to at least 32 characters" in result.stderr
    assert "ENVIRONMENT='production'" in result.stderr
