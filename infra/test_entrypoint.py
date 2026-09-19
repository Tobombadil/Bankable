"""Tests for infra/entrypoint.py with a stub DSN and a fake `sentry_sdk` module (no network, no
real key). Checks use `expect` instead of bare `assert` (ruff S101, see infra/scheduler/test_jobs.py)."""

from __future__ import annotations

import logging
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from infra import entrypoint
from infra.logging_config import JsonFormatter

FAKE_DSN = "https://publickey@o0.ingest.example.invalid/0"


def expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(str(message))


@pytest.fixture()
def fake_sentry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"init": [], "tags": []}
    module = types.ModuleType("sentry_sdk")
    module.init = lambda **kwargs: calls["init"].append(kwargs)  # type: ignore[attr-defined]
    module.set_tag = lambda key, value: calls["tags"].append((key, value))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", module)
    monkeypatch.setenv("SENTRY_DSN", FAKE_DSN)
    # infra.scheduler.app builds its Procrastinate connector at import (no connection is opened);
    # same dummy URL infra/scheduler/test_jobs.py uses.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    return calls


@pytest.fixture()
def json_root() -> Iterator[logging.Logger]:
    """Root logger with no handlers, restored afterwards (pytest's capture handlers would otherwise
    hide whether the entrypoint installed the JSON one)."""
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers = []
    try:
        yield root
    finally:
        root.handlers = saved


def test_usage_error_without_a_known_service() -> None:
    with pytest.raises(SystemExit):
        entrypoint.main([])
    with pytest.raises(SystemExit):
        entrypoint.main(["social"])


def test_api_serves_via_uvicorn_factory_with_logging_and_tracking(
    monkeypatch: pytest.MonkeyPatch, fake_sentry: dict[str, Any], json_root: logging.Logger
) -> None:
    captured: dict[str, Any] = {}
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda target, **kwargs: captured.update(target=target, **kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("WEB_CONCURRENCY", "3")

    entrypoint.main(["api"])

    expect(captured["target"] == "infra.entrypoint:api_app", captured)
    expect(captured["factory"] is True and captured["log_config"] is None, captured)
    expect(captured["port"] == 8000 and captured["workers"] == 3, captured)
    expect(fake_sentry["init"][0]["dsn"] == FAKE_DSN, "tracking must be initialised before serving")
    expect(("service", "api") in fake_sentry["tags"], fake_sentry)
    expect(any(isinstance(h.formatter, JsonFormatter) for h in json_root.handlers), "JSON logging first")


def test_worker_forwards_its_arguments(monkeypatch: pytest.MonkeyPatch, fake_sentry: dict[str, Any]) -> None:
    received: list[list[str]] = []
    monkeypatch.setattr("infra.scheduler.worker.main", lambda argv=None: received.append(list(argv or [])))
    entrypoint.main(["worker", "--queues", "fetch_browser"])
    expect(received == [["--queues", "fetch_browser"]], received)
    expect(("service", "worker") in fake_sentry["tags"], fake_sentry)


def test_scheduler_runs_the_scheduler_main(
    monkeypatch: pytest.MonkeyPatch, fake_sentry: dict[str, Any]
) -> None:
    ran: list[str] = []
    monkeypatch.setattr("infra.scheduler.app.main", lambda: ran.append("scheduler"))
    entrypoint.main(["scheduler"])
    expect(ran == ["scheduler"], ran)
    expect(("service", "scheduler") in fake_sentry["tags"], fake_sentry)


def test_factories_bootstrap_then_return_the_apps(
    monkeypatch: pytest.MonkeyPatch, fake_sentry: dict[str, Any], json_root: logging.Logger
) -> None:
    api = entrypoint.api_app()
    expect(getattr(api, "title", None) is not None, "api factory must return the FastAPI app")
    expect(("service", "api") in fake_sentry["tags"], "api factory must bootstrap in its own process")
    # web.app is not imported here: ci.yml keeps the web suite in its own process (see its header
    # note on cross-suite state), so the factory is proven against a stand-in module instead.
    fake_web_app = object()
    fake_web = types.ModuleType("web.app")
    fake_web.app = fake_web_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "web.app", fake_web)
    expect(entrypoint.web_app() is fake_web_app, "web factory must return web.app:app")
    expect(("service", "web") in fake_sentry["tags"], "web factory must bootstrap too")
    expect(any(isinstance(h.formatter, JsonFormatter) for h in json_root.handlers), "JSON logging installed")
