"""JSON logging for every process (docs/04 E-18; docs/60 §7), with private content redacted
(docs/19 §5.3). Standard library only.

The services log with `logger.info("...", extra={...})`; the default `%(message)s` format drops
those fields at emission. `JsonFormatter` emits one JSON object per record carrying the standard
fields plus every `extra` key. `configure_logging()` installs it on the root logger; it is called
by `infra/entrypoint.py` *before* the service module is imported, so the service's own
`logging.basicConfig(...)` call is a no-op (basicConfig does nothing once the root logger has a
handler — `infra/test_logging_config.py` proves that claim).

**Redaction (docs/19 §5.3).** `configure_logging()` also puts `PrivateFieldFilter` on that handler.
Every record the process logs passes through it before a line is written, including uvicorn's
access and error lines (the entrypoint runs uvicorn with `log_config=None`, so they reach the root
handler). The filter rewrites the record in place, so a later handler, and sentry-sdk's logging
integration (which reads the record after the handlers have run), get the redacted record.

1. **Named fields, every record.** Attributes named `body`, `note`, `title`, `email`, `lat`, `lon`
   or `token` (`PRIVATE_FIELDS`, any case) are dropped: at the top level; inside dict, list, tuple
   and set extras; and from a dict, a pydantic model or a dataclass logged as an extra, as a
   message argument or as the message (the one dict that is a message's argument mapping keeps
   the key, as `REDACTED`, so `%(title)s` still formats). A `t` extra or key (the invitation
   link's parameter) keeps its name and loses a string value. `redacted_fields` lists what was
   dropped or redacted, never the values. No public log line uses these names today; a public
   value that needs one goes under a more specific name.
2. **Content, every string** (the message, string extras, exception and stack text). These become
   `REDACTED`: an invitation token (`INVITATION_TOKEN_PATTERN`, the same pattern as the Caddy
   access log filter: the value of a `t` parameter at the start of a value or after `?`, `&` or
   `;` (`&amp;t=`), with the name or the separators percent-encoded at any depth, and a
   token-length path segment under `/my/invitations/`); a `t` key's value in a dict's or a
   tuple's text; email addresses (also with a quoted local part, percent-encoded at any depth, or
   with a fullwidth at sign); SQLAlchemy's `[parameters: ...]` line, which holds a failed
   statement's bound values (a note, a title, an email); Postgres's `DETAIL:` line, which can
   quote the failing row; pydantic's `input_value=...`.
3. **Private records, deny by default.** A record is private when its logger is in
   `PRIVATE_NAMESPACES` or `PRIVATE_LOG_SITES`, or when the call that logged it is in one of their
   files (which covers `python -m services.projects.load_checklist`, whose logger and module are
   `__main__`). Its extras pass only under the names in `PRIVATE_EXTRA_NAMES` or ending `_id`,
   `_ids`, `_count` or `_ms`, which covers docs/19 §5.3's "the route name, the outcome, the
   latency and opaque ids", and only in that name's shape: a public id under `_id`, a number under
   `_count` and `_ms`, a lower-case code under `outcome`, a path under `path`
   (`_PRIVATE_EXTRA_SHAPES`); any other value becomes `REDACTED`, so a sentence, a nested object
   or `str(exc)` cannot pass. One word in the right shape still can (a one-word title as an
   `error_type`): these names are for code, never content. Its message arguments pass only as
   integers, booleans, None or public ids (`prj_…`, `req_…`); any other argument becomes
   `REDACTED`. Measurements go in named extras (`latency_ms=`), not the message. Values
   interpolated into the message text itself (an f-string) are beyond any filter; rule 2 still
   applies to them.
4. **Exceptions from private code or private requests.** An exception keeps its type and frames but
   loses its message and notes when the record is private (rule 3); when the record's message,
   arguments or extras name a private path (`/my/`, `/v1/my/`, `/api/my/`; also a URL object); or
   when any exception in its chain was raised through a `PRIVATE_NAMESPACES` frame (by module name
   or by file), through an ASGI frame serving a private path (the `scope` every ASGI app,
   middleware and router frame holds), or by FastAPI for a private endpoint (`endpoint_ctx`). The
   last two cover uvicorn's "Exception in ASGI application" line, which names no path: a response
   that fails its model is checked after the endpoint has returned, when no private frame is left.
   A pydantic or FastAPI validation error keeps its error types and endpoint, never its input.
   A record whose exception text was withheld or redacted loses `exc_info`, so nothing after the
   filter can render the original from the exception object; `exc_text` holds the redacted
   rendering, which `JsonFormatter` and the standard formatters write.

**Not covered here.** sentry-sdk's ASGI integration reports an unhandled exception from the
exception object itself, with the request's query string and headers; redacting that event is the
`before_send` hook's job in `infra/observability.py` (docs/19 §5.3), not this filter's. In a public
module's text, content no pattern recognises (a title in an f-string, an address with no dot in
its domain, an object's own `repr`) is written as it is. Private code belongs in a private
module, where rules 3 and 4 do not depend on recognising anything.

If redaction fails, the record is replaced by a fixed line: nothing of the original is written.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import re
import sys
import traceback
from collections.abc import Iterator, Mapping
from types import FrameType
from typing import IO, Any

# Attribute names every LogRecord carries; anything else on the record came from `extra=`.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}

#: What every redacted value becomes. Caddy writes the same word for cookies and the token.
REDACTED = "REDACTED"
#: Record attributes no log line carries, whatever logged them (docs/19 §5.3).
PRIVATE_FIELDS = frozenset({"body", "note", "title", "email", "lat", "lon", "token"})
#: The query parameter that carries an invitation token (docs/19 §6.2, `/my/invitations/accept?t=`).
TOKEN_PARAMETER = "t"  # noqa: S105 -- a parameter name, not a credential
#: Modules that hold or handle private-project content (docs/19 §5.2 item 1, §8), as logger and
#: module names: their records are private (rule 3), and so is an exception raised through their
#: code (rule 4).
PRIVATE_NAMESPACES = (
    "services.projects",
    "services.api.my_projects",
    "services.api.account_erasure",
    "web.my_projects",
)
#: Modules whose own log calls are private (rule 3), whatever logger they log on, but whose
#: exceptions are not private by that alone. The retention job's entry point holds docs/19 §5.4's
#: invitation rule beside the public rules and logs on the shared `services.retention` logger,
#: whose snapshot lines (services/retention/snapshots.py) stay public; its failures keep their
#: messages, which is how an operator reads a failed nightly run.
PRIVATE_LOG_SITES = ("services.retention.run",)

_CODE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_PATH_VALUE = re.compile(r"/[A-Za-z0-9_./{}~%-]{0,255}")
#: The shape each allowed extra's value must have in a private record (rule 3). A number, a
#: boolean or None passes under any allowed name.
_PRIVATE_EXTRA_SHAPES: dict[str, re.Pattern[str]] = {
    "route": re.compile(rf"[a-z_][a-z0-9_.]{{0,127}}|{_PATH_VALUE.pattern}"),
    "action": _CODE,
    "outcome": _CODE,
    "status": _CODE,
    "status_code": re.compile(r"[1-5][0-9]{2}"),
    "method": re.compile(r"[A-Z]{3,10}"),
    "path": _PATH_VALUE,
    "role": _CODE,
    "error_type": re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}"),
    "error_code": _CODE,
}
#: Extras a private record may carry, besides names ending in `_PRIVATE_EXTRA_SUFFIXES`.
PRIVATE_EXTRA_NAMES = frozenset(_PRIVATE_EXTRA_SHAPES)
_PRIVATE_EXTRA_SUFFIXES = ("_id", "_ids", "_count", "_ms")
#: The attribute where the filter lists the field names it dropped or redacted.
REDACTED_FIELDS_ATTR = "redacted_fields"
#: Written instead of a record the filter could not redact.
WITHHELD_MESSAGE = "log record withheld: redaction failed"

#: An invitation token in a URL: the value of a `t` query parameter, at the start of the value
#: (`request.url.query`) or after `?`, `&` or `;` (`&amp;t=` in HTML), with `?`, `&`, `t` and `=`
#: raw or percent-encoded at any depth (`/login?next=%2Fmy%2Finvitations%2Faccept%3Ft%3D...`,
#: `?%74=...`, which Starlette decodes to `t`), and a token-length path segment under
#: /my/invitations/. infra/compose/Caddyfile carries this exact pattern and replacement (Go RE2 and
#: Python agree on it: no backreferences, no lookaround); infra/test_caddyfile.py pins it. The
#: value ends at the next separator, at whitespace, or at a quote or angle bracket (`\x22`, `\x27`:
#: the Caddyfile cannot quote a `"`), so `href="…?t=…">` and JSON text keep their shape.
INVITATION_TOKEN_PATTERN = (
    r"(?i)((?:^|[?&;]|%(?:25)*(?:3f|26))(?:t|%(?:25)*(?:74|54))(?:=|%(?:25)*3d))[^&#%;\s\x22\x27<>]*"  # noqa: S105 -- a regexp, not a credential
    r"|((?:/|%(?:25)*2f)my(?:/|%(?:25)*2f)invitations(?:/|%(?:25)*2f))[a-z0-9_-]{20,}"
)
_INVITATION_TOKEN = re.compile(INVITATION_TOKEN_PATTERN)
#: Characters of an address's local part (RFC 5322 atext, less the ones that delimit a URL's query
#: or fragment, a quoted string or a format field, so `q=alice%40example.com` keeps `q=`).
_LOCAL = r"[\w.+%/!$*^~-]"
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # SQLAlchemy StatementError text: "[parameters: ('prj_…', 'the note text', …)]" on its own line.
    (re.compile(r"\[parameters: [^\n]*"), f"[parameters: {REDACTED}]"),
    # Postgres error detail: "DETAIL:  Key (email)=(…)" or "DETAIL:  Failing row contains (…)".
    (re.compile(r"(?m)^(DETAIL:\s*).*$"), rf"\g<1>{REDACTED}"),
    # pydantic ValidationError text: "[type=string_too_long, input_value='…', input_type=str]",
    # one error per line. Greedy, so a value that itself contains ", input_type=" goes whole;
    # pydantic cuts the value to about 50 characters, and the bound keeps a line of many
    # `input_value=` and no `input_type` from being rescanned without end.
    (re.compile(r"(input_value=)[^\n]{0,200}(?=, input_type=)"), rf"\g<1>{REDACTED}"),
    (_INVITATION_TOKEN, rf"\g<1>\g<2>{REDACTED}"),
    # A `t` key's value in a dict's or a tuple's text: `{'t': '…'}` (`dict(request.query_params)`),
    # `('t', '…')` (`query_params.multi_items()`), `"t": "…"`.
    (re.compile(r"""(?i)((?<!\w)['"]t['"]\s{0,8}[:,]\s{0,8}['"])[^'"\s]*"""), rf"\g<1>{REDACTED}"),
    # An address: quoted (`"john doe"@…`), percent-encoded at any depth (`alice%40…`,
    # `alice%2540…`) or with a fullwidth or small at sign. The lookbehind starts a candidate only
    # where a run of address characters starts: without it, a long run with no `@` (a base64
    # blob) is rescanned from every position, quadratic in its length.
    (
        re.compile(
            rf'(?:"[^"\\\n]{{1,64}}"|(?<!{_LOCAL}){_LOCAL}+)(?:@|%(?:25)*40|＠|﹫)[\w-]+(?:\.[\w-]+)+'
        ),
        REDACTED,
    ),
)
#: Message arguments a private module may log: public ids (`^prop_[0-9A-HJKMNP-TV-Z]{10,26}$`,
#: services/ids.py) and request ids (`^req_[0-9a-z]{6,32}$`, services/api/common.py).
_OPAQUE_ID = re.compile(r"[a-z]{2,6}_[0-9A-HJKMNP-TV-Z]{10,26}|req_[0-9a-z]{6,32}")
#: A private page or API path, bare or as a full URL, where a path or URL starts (not inside a
#: longer path: `/home/my/` is not one).
_PRIVATE_PATH = re.compile(
    r"(?<![\w./%-])(?:(?:https?:)?//[^/\s]+)?/(?:v1/|api/)?my(?![\w.%-])", re.IGNORECASE
)
#: One printf conversion (`%s`, `%(name)d`, `%.1f`); `%%` is not one.
_PRINTF_SPEC = re.compile(r"%(\([^)]*\))?[#0 +-]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?[diouxXeEfFgGcrsa]")
#: What a validation error's summary may name (rule 4): pydantic's error types and FastAPI's
#: route template, which are code, never input.
_ERROR_TYPE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ENDPOINT = re.compile(r"[A-Z]{2,10} /[A-Za-z0-9_./{}:-]{0,255}")
_VALIDATION_MODULES = frozenset({"pydantic", "pydantic_core", "fastapi"})
_MAX_DEPTH = 8


def redact_text(text: str) -> str:
    """`text` with rule 2's patterns replaced: invitation tokens, email addresses, SQL parameters,
    Postgres row detail and pydantic input values. Safe to apply twice."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _in_names(name: str, names: tuple[str, ...]) -> bool:
    return any(name == ns or name.startswith(ns + ".") for ns in names)


def is_private_namespace(name: str) -> bool:
    """True for a logger or module name inside `PRIVATE_NAMESPACES`."""
    return _in_names(name, PRIVATE_NAMESPACES)


def _module_files(names: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Each module's file (`/services/api/my_projects.py`) and package directory (`/services/projects/`)."""
    return tuple((f"/{ns.replace('.', '/')}.py", f"/{ns.replace('.', '/')}/") for ns in names)


_PRIVATE_CODE_FILES = _module_files(PRIVATE_NAMESPACES)
_PRIVATE_LOG_FILES = _module_files(PRIVATE_NAMESPACES + PRIVATE_LOG_SITES)


def _in_files(filename: str, files: tuple[tuple[str, str], ...]) -> bool:
    path = "/" + filename.replace("\\", "/")
    return any(path.endswith(module) or package in path for module, package in files)


def _is_private_record(record: logging.LogRecord) -> bool:
    """Rule 3: the record's logger, or the file of the call that logged it, is private."""
    return _in_names(record.name, PRIVATE_NAMESPACES + PRIVATE_LOG_SITES) or _in_files(
        str(record.pathname), _PRIVATE_LOG_FILES
    )


def _is_private_field(name: object) -> bool:
    return str(name).lower() in PRIVATE_FIELDS


def _is_token_key(name: object) -> bool:
    return isinstance(name, str) and name.lower() == TOKEN_PARAMETER


def _private_extra_allowed(name: str) -> bool:
    return name in PRIVATE_EXTRA_NAMES or name.endswith(_PRIVATE_EXTRA_SUFFIXES)


def _private_extra_value_allowed(name: str, value: Any) -> bool:
    """Rule 3's value shapes, for an extra whose name `_private_extra_allowed` let through."""
    if value is None or isinstance(value, bool | int | float):
        return True
    if name.endswith("_ids"):
        return isinstance(value, list | tuple | set | frozenset) and all(
            _private_extra_value_allowed(name[:-1], item) for item in value
        )
    if not isinstance(value, str):
        return False
    if name.endswith("_id"):
        return _OPAQUE_ID.fullmatch(value) is not None
    shape = _PRIVATE_EXTRA_SHAPES.get(name)  # `_count` and `_ms` have none: numbers only
    return shape is not None and shape.fullmatch(value) is not None


def _fields_of(value: Any) -> dict[str, Any] | None:
    """A pydantic model's or a dataclass instance's fields, read as attributes (no serialiser
    runs), or None for anything else."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: getattr(value, f.name, None) for f in dataclasses.fields(value)}
    model_fields = getattr(type(value), "model_fields", None)
    if isinstance(model_fields, Mapping) and callable(getattr(value, "model_dump", None)):
        fields = {name: getattr(value, name, None) for name in model_fields}
        extra = getattr(value, "__pydantic_extra__", None)
        return fields | (dict(extra) if isinstance(extra, Mapping) else {})
    return None


def _scrub(value: Any, *, path: str, dropped: list[str], depth: int = 0) -> Any:
    """A redacted copy of an extra's value; the caller's own object is never changed."""
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if depth >= _MAX_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if _is_private_field(key):
                dropped.append(f"{path}.{key}")
                continue
            if _is_token_key(key) and isinstance(item, str):
                dropped.append(f"{path}.{key}")
                out[key] = REDACTED
                continue
            out[key] = _scrub(item, path=f"{path}.{key}", dropped=dropped, depth=depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        items = [_scrub(i, path=path, dropped=dropped, depth=depth + 1) for i in value]
        # The same kind of container, so the line renders as before and a set's members (tuples
        # stay tuples) stay hashable.
        if isinstance(value, frozenset):
            return frozenset(items)
        if isinstance(value, set):
            return set(items)
        return items if isinstance(value, list) else tuple(items)
    fields = _fields_of(value)
    if fields is not None:
        before = len(dropped)
        scrubbed = _scrub(fields, path=path, dropped=dropped, depth=depth)
        if len(dropped) > before:  # written as its fields, less the private ones
            return scrubbed
    # Anything else is written through str(), the formatter's fallback.
    return redact_text(str(value))


class _Shown(str):
    """A model or dataclass rendered without its private fields: the same text under `%s` and `%r`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return str(self)


def _clean(value: Any, *, path: str, dropped: list[str], depth: int = 0) -> Any:
    """A message argument with rule 1's fields taken out of any dict, list, tuple, set, model or
    dataclass in it: the very same object when there were none, so the line renders as before."""
    if value is None or isinstance(value, str | bytes | bool | int | float):
        return value
    is_container = isinstance(value, Mapping | list | tuple | set | frozenset)
    fields = None if is_container else _fields_of(value)
    if (is_container or fields is not None) and depth >= _MAX_DEPTH:
        return _Shown(REDACTED)
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        changed = False
        for key, item in value.items():
            if _is_private_field(key):
                dropped.append(f"{path}.{key}")
                changed = True
                continue
            out[key] = _clean(item, path=f"{path}.{key}", dropped=dropped, depth=depth + 1)
            changed = changed or out[key] is not item
        return out if changed else value
    if isinstance(value, list | tuple | set | frozenset):
        items = [_clean(i, path=path, dropped=dropped, depth=depth + 1) for i in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        if isinstance(value, frozenset):
            return frozenset(items)
        if isinstance(value, set):
            return set(items)
        return items if isinstance(value, list) else tuple(items)
    if fields is not None:
        private = [k for k in fields if _is_private_field(k)]
        kept = {
            k: _clean(v, path=f"{path}.{k}", dropped=dropped, depth=depth + 1)
            for k, v in fields.items()
            if k not in private
        }
        if private or any(kept[k] is not fields[k] for k in kept):
            dropped.extend(f"{path}.{k}" for k in private)
            shown = ", ".join(f"{k}={v!r}" for k, v in kept.items())
            return _Shown(f"{type(value).__name__}({shown})")
    return value


def _safe_arg(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str) and _OPAQUE_ID.fullmatch(value):
        return value
    return REDACTED


def _render_message(record: logging.LogRecord, *, strict: bool, dropped: list[str]) -> str:
    msg = record.msg if isinstance(record.msg, str) else None
    if msg is None:
        msg = (
            f"<{type(record.msg).__name__}>"
            if strict
            else str(_clean(record.msg, path="msg", dropped=dropped))
        )
    args: Any = record.args
    if not args:
        return msg
    if strict:
        if isinstance(args, Mapping):
            args = {key: _safe_arg(value) for key, value in args.items()}
        else:
            args = tuple(_safe_arg(value) for value in args)
    elif isinstance(args, Mapping):  # `%(title)s`: the field's value goes, the format still works
        dropped.extend(f"args.{key}" for key in args if _is_private_field(key))
        args = {
            key: REDACTED if _is_private_field(key) else _clean(value, path=f"args.{key}", dropped=dropped)
            for key, value in args.items()
        }
    else:
        args = tuple(_clean(value, path=f"args[{i}]", dropped=dropped) for i, value in enumerate(args))
    try:
        return str(msg % args)
    except (TypeError, ValueError, KeyError):
        pass
    try:  # a redacted argument under `%d` or `%.1f`: write every conversion as `%s`
        return str(_PRINTF_SPEC.sub(lambda m: f"%{m.group(1) or ''}s", msg) % args)
    except (TypeError, ValueError, KeyError):
        return f"{msg} [arguments withheld: they do not match the format]"


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """`exc` and every exception reachable from it (causes, contexts, group members), once each."""
    seen: dict[int, BaseException] = {}
    stack = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen[id(current)] = current
        stack.extend(e for e in (current.__cause__, current.__context__) if e is not None)
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
    return list(seen.values())


def _private_endpoint(ctx: object) -> bool:
    """FastAPI's `endpoint_ctx` (on its validation errors) names a private route or module."""
    if not isinstance(ctx, Mapping):
        return False
    path, file = ctx.get("path"), ctx.get("file")
    return (isinstance(path, str) and _PRIVATE_PATH.search(path) is not None) or (
        isinstance(file, str) and _in_files(file, _PRIVATE_CODE_FILES)
    )


def _private_frame(frame: FrameType) -> bool:
    """Private code (by module name, or by file for a module run as `__main__`), or an ASGI frame
    serving a private path: every ASGI app, middleware and router frame holds the `scope`."""
    if is_private_namespace(str(frame.f_globals.get("__name__", ""))):
        return True
    if _in_files(frame.f_code.co_filename, _PRIVATE_CODE_FILES):
        return True
    scope = frame.f_locals.get("scope")
    if not isinstance(scope, Mapping) or scope.get("type") not in ("http", "websocket"):
        return False
    path = scope.get("path")
    return isinstance(path, str) and _PRIVATE_PATH.match(path) is not None


def _raised_privately(exc: BaseException) -> bool:
    for current in _exception_chain(exc):
        if _private_endpoint(getattr(current, "endpoint_ctx", None)):
            return True
        tb = current.__traceback__
        while tb is not None:
            if _private_frame(tb.tb_frame):
                return True
            tb = tb.tb_next
    return False


def _type_name(kind: type[BaseException]) -> str:
    module = kind.__module__
    return kind.__qualname__ if module in ("builtins", "__main__") else f"{module}.{kind.__qualname__}"


def _validation_summary(exc: BaseException) -> str:
    """For pydantic's and FastAPI's validation errors: the error types and FastAPI's endpoint (its
    route template and function), which are code. Never the input, the location or the message."""
    if type(exc).__module__.partition(".")[0] not in _VALIDATION_MODULES:
        return ""
    parts: list[str] = []
    errors = getattr(exc, "errors", None)
    items = errors() if callable(errors) else None
    if isinstance(items, list | tuple) and items:
        kinds = sorted(
            {
                e["type"]
                for e in items
                if isinstance(e, Mapping)
                and isinstance(e.get("type"), str)
                and _ERROR_TYPE.fullmatch(e["type"])
            }
        )
        parts.append(f"{len(items)} error{'' if len(items) == 1 else 's'}: {', '.join(kinds) or '?'}")
    ctx = getattr(exc, "endpoint_ctx", None)
    if isinstance(ctx, Mapping):
        path, function = ctx.get("path"), ctx.get("function")
        if isinstance(path, str) and _ENDPOINT.fullmatch(path):
            parts.append(f"endpoint {path}")
        if isinstance(function, str) and function.isidentifier():
            parts.append(f"in {function}")
    return "".join(f"; {part}" for part in parts)


def _format_without_messages(exc: BaseException) -> str:
    """Like `traceback.format_exception`, with each exception's message and notes withheld. The
    frames are code (file, line, source line), never runtime values."""
    lines: list[str] = []
    seen: set[int] = set()

    def walk(current: BaseException) -> None:
        seen.add(id(current))
        if current.__cause__ is not None and id(current.__cause__) not in seen:
            walk(current.__cause__)
            lines.append("\nThe above exception was the direct cause of the following exception:\n\n")
        elif (
            current.__context__ is not None
            and not current.__suppress_context__
            and id(current.__context__) not in seen
        ):
            walk(current.__context__)
            lines.append("\nDuring handling of the above exception, another exception occurred:\n\n")
        if current.__traceback__ is not None:
            lines.append("Traceback (most recent call last):\n")
            lines.extend(traceback.format_tb(current.__traceback__))
        summary = _validation_summary(current)
        lines.append(f"{_type_name(type(current))}: {REDACTED} (message withheld{summary})\n")
        if isinstance(current, BaseExceptionGroup):
            lines.extend(f"  member: {_type_name(type(member))}\n" for member in current.exceptions)

    walk(exc)
    return "".join(lines).rstrip("\n")


def _strings(value: Any, depth: int = 0) -> Iterator[str]:
    """Every string `value` would be written as: itself, its members', or its str() (a URL object)."""
    if isinstance(value, str):
        yield value
    elif value is None or isinstance(value, bool | int | float | bytes) or depth >= _MAX_DEPTH:
        return
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item, depth + 1)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _strings(item, depth + 1)
    else:
        yield str(value)


def _names_private_path(record: logging.LogRecord) -> bool:
    values: list[Any] = [record.msg]
    if isinstance(record.args, Mapping):
        values.extend(record.args.values())
    elif isinstance(record.args, tuple):
        values.extend(record.args)
    values.extend(v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS)
    return any(_PRIVATE_PATH.search(text) for value in values for text in _strings(value))


def redact_record(record: logging.LogRecord) -> None:
    """Apply the module docstring's rules to `record`, in place. May raise; `PrivateFieldFilter`
    turns any failure into `WITHHELD_MESSAGE`."""
    strict = _is_private_record(record)
    dropped: list[str] = []
    exc_info = record.exc_info
    if exc_info is not None and exc_info[1] is not None:
        exc = exc_info[1]
        if strict or _names_private_path(record) or _raised_privately(exc):
            record.exc_text = redact_text(_format_without_messages(exc))
            record.exc_info = None
        else:
            text = logging.Formatter().formatException(exc_info)
            record.exc_text = redact_text(text)  # re-rendered: an earlier formatter's cache is the original
            if record.exc_text != text:
                record.exc_info = None
    elif record.exc_text:
        record.exc_text = redact_text(record.exc_text)
    if record.stack_info:
        record.stack_info = redact_text(record.stack_info)

    record.msg, record.args = redact_text(_render_message(record, strict=strict, dropped=dropped)), ()
    if "message" in record.__dict__:  # an earlier handler's formatter cached the original text
        record.message = record.msg

    for key in [k for k in record.__dict__ if k not in _STANDARD_ATTRS]:
        if not isinstance(key, str):  # `extra={1: ...}`: no formatter can name it
            del record.__dict__[key]
            dropped.append(f"<{type(key).__name__} key>")
            continue
        if key.startswith("_") or key == REDACTED_FIELDS_ATTR:
            continue
        value = record.__dict__[key]
        if _is_private_field(key) or (strict and not _private_extra_allowed(key)):
            del record.__dict__[key]
            dropped.append(key)
        elif (strict and not _private_extra_value_allowed(key, value)) or (
            _is_token_key(key) and isinstance(value, str)
        ):
            record.__dict__[key] = REDACTED
            dropped.append(key)
        else:
            record.__dict__[key] = _scrub(value, path=key, dropped=dropped)
    if dropped:
        earlier = getattr(record, REDACTED_FIELDS_ATTR, None)
        merged = set(dropped) | (set(earlier) if isinstance(earlier, list) else set())
        setattr(record, REDACTED_FIELDS_ATTR, sorted(merged))


def _withhold(record: logging.LogRecord) -> None:
    for key in [
        k
        for k in record.__dict__
        if k not in _STANDARD_ATTRS and not (isinstance(k, str) and k.startswith("_"))
    ]:
        del record.__dict__[key]
    record.msg, record.args = WITHHELD_MESSAGE, ()
    record.exc_info = record.exc_text = record.stack_info = None
    if "message" in record.__dict__:
        record.message = WITHHELD_MESSAGE


class PrivateFieldFilter(logging.Filter):
    """The redaction step (module docstring). Never drops a record and never raises into the code
    that logged: a record it cannot redact is written as `WITHHELD_MESSAGE` and nothing else."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            redact_record(record)
        except Exception:  # fail closed: no part of an unredactable record is written
            _withhold(record)
        return True


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
            if isinstance(key, str) and key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_text:  # the filter's redacted rendering (or a standard formatter's cache)
            payload["exc_info"] = record.exc_text
        elif record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = record.stack_info
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str | None = None, stream: IO[str] | None = None) -> logging.Handler:
    """Install the JSON handler, with `PrivateFieldFilter`, on the root logger (idempotent) and
    return it.

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
    if not any(isinstance(f, PrivateFieldFilter) for f in handler.filters):
        handler.addFilter(PrivateFieldFilter())
    root.setLevel(level_name)
    return handler
