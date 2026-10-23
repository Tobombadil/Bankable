"""Error tracking hook (docs/20 §10; docs/60 §7). Env-gated: a no-op unless `SENTRY_DSN` is set.

Called once per process by `infra/entrypoint.py` before the service module is imported, so every
compose service (api, web, worker, browser-worker, scheduler) reports to the same project with a
`service` tag and no per-service code. The SDK is imported lazily so a checkout without
`sentry-sdk` installed (or a test run) never pays for it. Metrics stay in Postgres (`source_run`
rows) per docs/60 §7; this module is only the exception path.

**Private requests (docs/19 §5.3).** `before_send` sees every event before it leaves the process.
An event is private when its request path is under `/my/`, `/v1/my/` or `/api/my/` (the site's
pages, the API and the site's relay), when its transaction names such a path, when a private module
logged it, or when any frame of its exception is in a private module (`PRIVATE_NAMESPACES`, the
logging filter's own list). A private event keeps the request's method and its path with every id
replaced by `{id}`, and nothing else of the request: no query string, headers, cookies, body or
environment. Its exceptions keep their types and frames and lose their messages; its log message,
extras and user are withheld; its breadcrumbs keep only their category, level and time; its trace
context keeps only `trace_id` and `span_id`, and its spans (a transaction's, which reaches the same
hook as `before_send_transaction`) only their ids, op, status and timing. Every other
event keeps its shape, with invitation tokens, addresses and SQL parameters redacted from its text
(`infra.logging_config.redact_text`), any header that names a private page withheld, and any
breadcrumb that mentions a private path or id emptied. `before_breadcrumb` drops a private module's
breadcrumbs as they are recorded. If scrubbing fails, the event is dropped: an error report is
worth less than what it might carry.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

from infra.logging_config import PRIVATE_NAMESPACES, is_private_namespace, redact_text

logger = logging.getLogger("infra.observability")

#: Path prefixes of private pages (`/my/`), the private API (`/v1/my/`) and the site's same-origin
#: relay of it (`/api/my/`, docs/19 §2.7).
PRIVATE_PATH_PREFIXES: Final = ("/my", "/v1/my", "/api/my")
#: The literal words of private routes (docs/19 §6.1, §6.2). Under a private prefix every other
#: segment is an id or user input and becomes `{id}`.
PRIVATE_PATH_WORDS: Final = frozenset(
    {
        "my",
        "v1",
        "api",
        "projects",
        "new",
        "links",
        "checklist",
        "evidence",
        "accept",
        "dismiss",
        "notes",
        "members",
        "invitations",
        "preview",
        "activity",
        "people",
        "pins",
        "export",
        "prefill",
    }
)
ID_PLACEHOLDER: Final = "{id}"
WITHHELD: Final = "[withheld: private]"
#: The parts of a private event's `contexts` that describe the process, not the request.
_KEPT_CONTEXTS: Final = frozenset({"runtime", "os", "device", "trace", "app"})
#: Of a private event's `contexts.trace`, only the ids that join it to its trace. Its description,
#: op and data can name the request's raw URL once tracing is on (`traces_sample_rate` is 0 today).
_KEPT_TRACE_KEYS: Final = ("trace_id", "span_id")
#: Of a private transaction's spans, their place in the trace and their timing: a span's
#: `description` and `data` can carry a URL, a query or a statement.
_KEPT_SPAN_KEYS: Final = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "op",
    "origin",
    "status",
    "start_timestamp",
    "timestamp",
)
_BREADCRUMB_KEYS: Final = ("type", "category", "level", "timestamp")
#: Private ids (`prj_`, `nte_`, `inv_`; `services.projects.models.PRIVATE_ID_PATTERN`) and a path
#: under a private prefix, anywhere in a string.
_PRIVATE_ID = re.compile(r"\b(?:prj|nte|inv)_[0-9A-HJKMNP-TV-Z]{10,26}\b", re.IGNORECASE)
_PRIVATE_PATH_IN_TEXT = re.compile(
    r"(?<![\w.%-])(?:(?:https?:)?//[^/\s]+)?/(?:v1/|api/)?my(?![\w.%-])", re.IGNORECASE
)
_PRIVATE_FILES: Final = tuple(name.replace(".", "/") for name in PRIVATE_NAMESPACES)
#: A route template's parameter (`{project_id}`), as a transaction names it: code, not input.
_TEMPLATE = re.compile(r"\{[a-z_]{1,32}\}")


def is_private_path(path: str) -> bool:
    lowered = path.lower()
    return any(lowered == prefix or lowered.startswith(prefix + "/") for prefix in PRIVATE_PATH_PREFIXES)


def scrub_private_path(path: str, *, keep_templates: bool = False) -> str:
    """`/v1/my/projects/prj_…/notes/nte_…` → `/v1/my/projects/{id}/notes/{id}`. With
    `keep_templates` (a transaction, which names the route), `{project_id}` stays as it is."""

    def kept(segment: str) -> bool:
        if segment == "" or segment.lower() in PRIVATE_PATH_WORDS:
            return True
        return keep_templates and _TEMPLATE.fullmatch(segment) is not None

    return "/".join(segment if kept(segment) else ID_PLACEHOLDER for segment in path.split("/"))


def _path_of(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("/"):
        return value.split("?", 1)[0].split("#", 1)[0]
    parsed = urlsplit(value)
    return parsed.path if parsed.scheme and parsed.netloc else None


def _scrub_url(value: object) -> str | None:
    """A private request's URL as its scheme, host and id-free path; no query, no fragment."""
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.netloc, scrub_private_path(parsed.path), "", ""))
    return scrub_private_path(value.split("?", 1)[0].split("#", 1)[0])


def _mentions_private(text: object) -> bool:
    return isinstance(text, str) and bool(_PRIVATE_PATH_IN_TEXT.search(text) or _PRIVATE_ID.search(text))


def _frames(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for group in ("exception", "threads"):
        for value in (event.get(group) or {}).get("values") or []:
            if isinstance(value, dict):
                frames.extend(
                    frame
                    for frame in ((value.get("stacktrace") or {}).get("frames") or [])
                    if isinstance(frame, dict)
                )
    return frames


def _private_frame(frame: Mapping[str, Any]) -> bool:
    module = frame.get("module")
    if isinstance(module, str) and is_private_namespace(module):
        return True
    for key in ("abs_path", "filename"):
        path = str(frame.get(key) or "").replace("\\", "/")
        stem = path[:-3] if path.endswith(".py") else path
        if any(stem.endswith(name) or f"{name}/" in path for name in _PRIVATE_FILES):
            return True
    return False


def is_private_event(event: Mapping[str, Any]) -> bool:
    request = event.get("request") or {}
    path = _path_of(request.get("url")) if isinstance(request, dict) else None
    if path is not None and is_private_path(path):
        return True
    transaction = event.get("transaction")
    if isinstance(transaction, str) and transaction.startswith("/") and is_private_path(transaction):
        return True
    logger_name = event.get("logger")
    if isinstance(logger_name, str) and is_private_namespace(logger_name):
        return True
    return any(_private_frame(frame) for frame in _frames(event))


def _bare_breadcrumbs(event: dict[str, Any]) -> None:
    crumbs = event.get("breadcrumbs")
    values = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
    if not isinstance(values, list):
        return
    bare = [
        {key: crumb[key] for key in _BREADCRUMB_KEYS if key in crumb}
        for crumb in values
        if isinstance(crumb, dict)
    ]
    if isinstance(crumbs, dict):
        crumbs["values"] = bare
    else:
        event["breadcrumbs"] = bare


def _scrub_exceptions(event: dict[str, Any], *, private: bool) -> None:
    for group in ("exception", "threads"):
        for value in (event.get(group) or {}).get("values") or []:
            if not isinstance(value, dict):
                continue
            if "value" in value and value["value"] is not None:
                value["value"] = WITHHELD if private else redact_text(str(value["value"]))
            for frame in (value.get("stacktrace") or {}).get("frames") or []:
                if isinstance(frame, dict):
                    frame.pop("vars", None)


def _scrub_private(event: dict[str, Any]) -> None:
    request = event.get("request")
    if isinstance(request, dict):
        event["request"] = {
            key: value
            for key, value in (("method", request.get("method")), ("url", _scrub_url(request.get("url"))))
            if value is not None
        }
    transaction = event.get("transaction")
    if isinstance(transaction, str):
        event["transaction"] = (
            scrub_private_path(transaction, keep_templates=True)
            if transaction.startswith("/")
            else transaction
        )
    _scrub_exceptions(event, private=True)
    if "logentry" in event:
        event["logentry"] = {"message": WITHHELD}
    if "message" in event:
        event["message"] = WITHHELD
    for key in ("extra", "user", "request_body", "_meta"):
        event.pop(key, None)
    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        kept = {key: value for key, value in contexts.items() if key in _KEPT_CONTEXTS}
        trace = kept.get("trace")
        if trace is not None:
            kept["trace"] = (
                {key: trace[key] for key in _KEPT_TRACE_KEYS if key in trace}
                if isinstance(trace, dict)
                else {}
            )
        event["contexts"] = kept
    spans = event.get("spans")
    if isinstance(spans, list):
        event["spans"] = [
            {key: span[key] for key in _KEPT_SPAN_KEYS if key in span}
            for span in spans
            if isinstance(span, dict)
        ]
    tags = event.get("tags")
    if isinstance(tags, dict):
        event["tags"] = {key: WITHHELD if _mentions_private(value) else value for key, value in tags.items()}
    _bare_breadcrumbs(event)


def _scrub_public(event: dict[str, Any]) -> None:
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("url", "query_string"):
            if isinstance(request.get(key), str):
                request[key] = redact_text(request[key])
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                name: WITHHELD if _mentions_private(value) else value for name, value in headers.items()
            }
    _scrub_exceptions(event, private=False)
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for key in ("message", "formatted"):
            if isinstance(logentry.get(key), str):
                logentry[key] = redact_text(logentry[key])
    if isinstance(event.get("message"), str):
        event["message"] = redact_text(event["message"])
    crumbs = event.get("breadcrumbs")
    values = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
    for crumb in values if isinstance(values, list) else []:
        if isinstance(crumb, dict) and (
            _mentions_private(crumb.get("message")) or _mentions_private(str(crumb.get("data") or ""))
        ):
            crumb.pop("message", None)
            crumb.pop("data", None)


def before_send(event: dict[str, Any], hint: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """The module docstring's rules. Never raises: an event that cannot be scrubbed is dropped."""
    del hint
    try:
        if is_private_event(event):
            _scrub_private(event)
        else:
            _scrub_public(event)
        return event
    except Exception:  # fail closed: no event leaves unscrubbed
        logger.warning("error report dropped: scrubbing failed")
        return None


def before_breadcrumb(crumb: dict[str, Any], hint: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """A private module's log line never becomes a breadcrumb (it would ride on the next event of
    the same scope); any other breadcrumb that names a private path or id loses its text."""
    del hint
    try:
        category = crumb.get("category")
        if isinstance(category, str) and is_private_namespace(category):
            return None
        if _mentions_private(crumb.get("message")) or _mentions_private(str(crumb.get("data") or "")):
            crumb.pop("message", None)
            crumb.pop("data", None)
        return crumb
    except Exception:  # fail closed
        return None


def init_error_tracking(service_name: str) -> bool:
    """Initialise the error tracker for this process. Returns True when it is active.

    `SENTRY_DSN` unset or blank: nothing happens (dev, CI, tests). Set but the SDK missing: a
    warning, still no-op — a missing package must never take a worker down. Personal data is
    never sent (`send_default_pii=False`, docs/04 P-rows), nor are stack-frame locals or request bodies
    (docs/19 §5), and private requests are scrubbed before they leave (`before_send`); tracing is
    off (cost ceiling, O-8).
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        sentry_sdk = importlib.import_module("sentry_sdk")
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; error tracking disabled")
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("ENVIRONMENT", "dev"),
        release=os.environ.get("SENTRY_RELEASE") or None,
        send_default_pii=False,
        # `send_default_pii=False` still sends stack-frame locals and request bodies by default. A
        # frame can hold a password, an invitation token or a private note; a body can be a sign-in
        # form. Neither leaves the server (docs/19 §5, lane P0).
        include_local_variables=False,
        max_request_body_size="never",
        # A private request's query string, headers, cookies, path ids and exception messages, and
        # its breadcrumbs' text, never leave either (docs/19 §5.3, lane P2).
        before_send=before_send,
        # A transaction (sent only when tracing is on) skips `before_send`, and with the real SDK
        # it carried a private request's full URL, query string and headers. Same scrubber.
        before_send_transaction=before_send,
        before_breadcrumb=before_breadcrumb,
        traces_sample_rate=0.0,
    )
    sentry_sdk.set_tag("service", service_name)
    logger.info("error tracking active", extra={"service": service_name})
    return True
