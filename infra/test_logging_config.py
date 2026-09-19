"""Tests for infra/logging_config.py, including the claim infra/entrypoint.py relies on: once
`configure_logging()` has installed the root handler, a later `logging.basicConfig(...)` from a
service module is a no-op. Checks use `expect` instead of bare `assert` (ruff S101, see
infra/scheduler/test_jobs.py)."""

from __future__ import annotations

import io
import json
import logging
import pathlib
import subprocess
import sys
from collections.abc import Iterator

import pytest

from infra.logging_config import JsonFormatter, configure_logging


def expect(condition: bool, message: object) -> None:
    if not condition:
        raise AssertionError(str(message))


@pytest.fixture()
def bare_root() -> Iterator[logging.Logger]:
    """Root logger with no handlers (pytest installs its own capture handlers, which would make
    basicConfig a no-op for the wrong reason); restored afterwards."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    root.handlers = []
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def app_handlers(root: logging.Logger) -> list[logging.Handler]:
    """Root handlers minus the ones pytest's logging plugin attaches around every test."""
    return [h for h in root.handlers if type(h).__name__ != "LogCaptureHandler"]


def emit_and_parse(stream: io.StringIO, logger: logging.Logger, **extra: object) -> dict[str, object]:
    logger.info("source run finished", extra=extra)
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    expect(len(lines) == 1, f"expected exactly one JSON line, got {lines!r}")
    parsed: dict[str, object] = json.loads(lines[-1])
    return parsed


def test_extra_fields_survive_emission(bare_root: logging.Logger) -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    record = emit_and_parse(stream, logging.getLogger("pipeline.test"), source_id="ferc", rows_seen=42)
    expect(record["message"] == "source run finished", record)
    expect(record["source_id"] == "ferc" and record["rows_seen"] == 42, f"extras dropped: {record}")
    expect(record["level"] == "INFO" and record["logger"] == "pipeline.test", record)
    expect(str(record["ts"]).endswith("Z"), "timestamp must be UTC ISO-8601")


def test_basic_config_after_configure_logging_is_a_no_op(bare_root: logging.Logger) -> None:
    stream = io.StringIO()
    handler = configure_logging(level="INFO", stream=stream)
    # Exactly what infra/scheduler/worker.py and services/alerts/worker.py call at startup.
    logging.basicConfig(level="DEBUG", format="%(message)s")
    expect(app_handlers(bare_root) == [handler], f"basicConfig must not add a handler: {bare_root.handlers}")
    expect(isinstance(handler.formatter, JsonFormatter), "basicConfig must not replace the formatter")
    record = emit_and_parse(stream, logging.getLogger("services.alerts.worker"), alerts_sent=3)
    expect(record["alerts_sent"] == 3, "extras must still be emitted after basicConfig")


def test_configure_logging_is_idempotent(bare_root: logging.Logger) -> None:
    stream = io.StringIO()
    first = configure_logging(level="WARNING", stream=stream)
    second = configure_logging(level="INFO", stream=io.StringIO())
    expect(first is second, "a second call must re-use the installed handler")
    expect(len(app_handlers(bare_root)) == 1, f"one handler expected, got {bare_root.handlers}")
    expect(bare_root.level == logging.INFO, "the second call may still change the level")


def test_level_defaults_to_log_level_env(bare_root: logging.Logger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    configure_logging(stream=io.StringIO())
    expect(bare_root.level == logging.WARNING, "LOG_LEVEL must be honoured case-insensitively")


def test_exceptions_and_non_serialisable_extras_are_rendered(bare_root: logging.Logger) -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    logger = logging.getLogger("infra.test")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed", extra={"when": object()})
    record = json.loads(stream.getvalue().splitlines()[-1])
    expect("ValueError: boom" in str(record["exc_info"]), record)
    expect(record["level"] == "ERROR", record)
    expect(isinstance(record["when"], str), "non-JSON extras fall back to str()")


def test_basic_config_claim_holds_in_a_bare_interpreter() -> None:
    """The same claim without pytest's own handlers in the way: a fresh interpreter, the
    entrypoint's order (configure first, service's basicConfig second), extras preserved."""
    script = (
        "import json, logging, sys\n"
        "from infra.logging_config import configure_logging, JsonFormatter\n"
        "handler = configure_logging(level='INFO', stream=sys.stdout)\n"
        "logging.basicConfig(level='DEBUG', format='%(message)s')\n"
        "root = logging.getLogger()\n"
        "logging.getLogger('services.alerts.worker').info('tick', extra={'alerts_sent': 3})\n"
        "is_json = isinstance(root.handlers[0].formatter, JsonFormatter)\n"
        "print(json.dumps({'handlers': len(root.handlers), 'json': is_json}))\n"
    )
    result = subprocess.run(  # noqa: S603 -- fixed argv, our own interpreter, no shell
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(__file__).resolve().parents[1],
        check=True,
    )
    log_line, summary_line = [json.loads(line) for line in result.stdout.strip().splitlines()]
    expect(log_line["alerts_sent"] == 3 and log_line["message"] == "tick", log_line)
    expect(summary_line == {"handlers": 1, "json": True}, summary_line)
