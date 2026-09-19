"""Tests for infra/observability.py. Bare `assert` is S101 under this repo's ruff config with no
per-file-ignore for infra/ (see infra/scheduler/test_jobs.py), so checks go through `expect`."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from infra.observability import init_error_tracking

# Obvious fake, never a real project key.
FAKE_DSN = "https://publickey@o0.ingest.example.invalid/0"


def expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(str(message))


def fake_sentry_module() -> tuple[types.ModuleType, dict[str, Any]]:
    """A stand-in `sentry_sdk` recording `init`/`set_tag` calls; nothing leaves the process."""
    module = types.ModuleType("sentry_sdk")
    calls: dict[str, Any] = {"init": [], "tags": []}
    module.init = lambda **kwargs: calls["init"].append(kwargs)  # type: ignore[attr-defined]
    module.set_tag = lambda key, value: calls["tags"].append((key, value))  # type: ignore[attr-defined]
    return module, calls


def test_no_dsn_is_a_no_op_and_never_imports_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    fake, calls = fake_sentry_module()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    expect(init_error_tracking("api") is False, "no DSN must report inactive")
    expect(calls["init"] == [], "no DSN must not call sentry_sdk.init")


def test_blank_dsn_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "   ")
    fake, _calls = fake_sentry_module()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    expect(init_error_tracking("web") is False, "blank DSN must report inactive")


def test_dsn_initialises_sdk_with_env_release_and_service_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", FAKE_DSN)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("SENTRY_RELEASE", "sha-abc1234")
    fake, calls = fake_sentry_module()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    expect(init_error_tracking("worker") is True, "DSN set must report active")
    init_calls = calls["init"]
    expect(len(init_calls) == 1, f"expected one init call, got {init_calls}")
    kwargs = init_calls[0]
    expect(kwargs["dsn"] == FAKE_DSN, "dsn must be passed through")
    expect(kwargs["environment"] == "staging", "environment must come from ENVIRONMENT")
    expect(kwargs["release"] == "sha-abc1234", "release must come from SENTRY_RELEASE")
    expect(kwargs["send_default_pii"] is False, "personal data must never be sent")
    expect(kwargs["traces_sample_rate"] == 0.0, "tracing stays off (cost ceiling)")
    expect(("service", "worker") in calls["tags"], "service tag must be set")


def test_missing_sdk_with_dsn_is_a_warning_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", FAKE_DSN)
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # makes `import sentry_sdk` raise ImportError
    expect(init_error_tracking("scheduler") is False, "missing SDK must degrade to inactive")
