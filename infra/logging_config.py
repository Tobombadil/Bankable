"""JSON logging for every process (docs/04 E-18; docs/60 §7). Standard library only.

The services log with `logger.info("...", extra={...})`; the default `%(message)s` format drops
those fields at emission. `JsonFormatter` emits one JSON object per record carrying the standard
fields plus every `extra` key. `configure_logging()` installs it on the root logger; it is called
by `infra/entrypoint.py` *before* the service module is imported, so the service's own
`logging.basicConfig(...)` call is a no-op (basicConfig does nothing once the root logger has a
handler — `infra/test_logging_config.py` proves that claim).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
from typing import IO, Any

# Attribute names every LogRecord carries; anything else on the record came from `extra=`.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = record.stack_info
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str | None = None, stream: IO[str] | None = None) -> logging.Handler:
    """Install the JSON handler on the root logger (idempotent) and return it.

    Level comes from `level`, else `LOG_LEVEL` (infra/compose/.env.example), else INFO. A second
    call re-uses the handler already installed rather than adding another, so a process that
    bootstraps twice (uvicorn's parent and its spawned workers each call this) never double-logs.
    """
    root = logging.getLogger()
    level_name = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    handler = next((h for h in root.handlers if getattr(h, "_infraque_json", False)), None)
    if handler is None:
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler._infraque_json = True  # type: ignore[attr-defined]  # marker for idempotency, see above
        root.addHandler(handler)
    root.setLevel(level_name)
    return handler
