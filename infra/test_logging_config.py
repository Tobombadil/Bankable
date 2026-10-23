"""Tests for infra/logging_config.py, including the claim infra/entrypoint.py relies on: once
`configure_logging()` has installed the root handler, a later `logging.basicConfig(...)` from a
service module is a no-op. Checks use `expect` instead of bare `assert` (ruff S101, see
infra/scheduler/test_jobs.py).

The redaction tests (docs/19 §5.3) feed sentinel values (a note or title, an invitee's address,
an invitation token) through each way the app logs: extras, nested extras, models and mappings,
message arguments, uvicorn's access and error lines, exception text from a failed statement, a
validation error, private code (also run as `__main__`), an ASGI request for a private path, a
FastAPI response that fails its model, sentry-sdk's logging integration, and real uvicorn
processes run the way the entrypoint runs them. Each asserts the sentinel never reaches the
output, and that the rest of the line is still there."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import datetime as dt
import io
import json
import logging
import pathlib
import socket
import subprocess
import sys
import textwrap
import time
import types
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from infra.logging_config import WITHHELD_MESSAGE, JsonFormatter, configure_logging


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


# ------------------------------------------------------------------------------ redaction (docs/19 §5.3)
#: A note's text or a project's title: free text no pattern could recognise.
SENTINEL = "SENTINEL-private-5e1f"
#: An invitee's address.
SENTINEL_EMAIL = "invitee.sentinel@example.test"
#: An invitation token: the shape `secrets.token_urlsafe(32)` gives (43 URL-safe characters).
SENTINEL_TOKEN = "SENTINELtok_a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P"  # noqa: S105 -- a test sentinel, asserted absent from the logs
SENTINELS = (SENTINEL, SENTINEL_EMAIL, SENTINEL_TOKEN)
#: docs/19 §5.3's list, written out: the filter's contract, not a copy of its constant.
SPEC_FIELDS = ("body", "note", "title", "email", "lat", "lon", "token")
PROJECT_ID = "prj_01HABCDEFGHJKMNPQRSTVW"
#: uvicorn's access line, exactly (uvicorn/protocols/http/h11_impl.py and httptools_impl.py).
ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'


def logged(bare_root: logging.Logger) -> io.StringIO:
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    return stream


def parsed(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def expect_no_sentinel(stream: io.StringIO) -> None:
    text = stream.getvalue()
    expect(text.strip() != "", "nothing was logged")
    for sentinel in SENTINELS:
        expect(sentinel not in text, f"{sentinel!r} reached the log: {text}")


def test_the_spec_fields_are_dropped_from_every_record(bare_root: logging.Logger) -> None:
    """docs/19 §5.3: `body`, `note`, `title`, `email`, `lat`, `lon` and `token` never appear, on any
    logger, at the top level or nested, in any case; the other fields are untouched."""
    stream = logged(bare_root)
    payload: dict[str, Any] = {
        "note": SENTINEL,
        "items": [{"email": SENTINEL_EMAIL, "kind": "invite"}],
        "n": 1,
    }
    extra: dict[str, Any] = dict.fromkeys(SPEC_FIELDS, SENTINEL) | {
        "Title": SENTINEL,
        "payload": payload,
        "rows_seen": 42,
    }
    extra["lat"], extra["lon"] = 51.501, -0.142
    for name in ("pipeline.test", "uvicorn.error", "services.alerts.worker"):
        logging.getLogger(name).info("source run finished", extra=extra)
    expect_no_sentinel(stream)
    for record in parsed(stream):
        expect(not set(SPEC_FIELDS) & set(record) and "Title" not in record, record)
        expect("51.501" not in json.dumps(record), "a coordinate reached the log")
        expect(record["rows_seen"] == 42, f"an ordinary field was dropped: {record}")
        expect(record["payload"] == {"items": [{"kind": "invite"}], "n": 1}, record["payload"])
        expect(
            set(record["redacted_fields"]) == {*SPEC_FIELDS, "Title", "payload.note", "payload.items.email"},
            record["redacted_fields"],
        )
    expect(payload["note"] == SENTINEL and "email" in payload["items"][0], "the caller's dict was changed")


def test_private_modules_log_only_named_extras_and_opaque_arguments(bare_root: logging.Logger) -> None:
    """Deny by default in services.projects, services.api.my_projects and web.my_projects: extras
    pass by allowlisted name, message arguments only as ints, booleans or public ids."""
    stream = logged(bare_root)
    private = logging.getLogger("services.projects.invitations")
    private.info(
        "invited %s as %s to %s (%d pending, owner=%s)",
        SENTINEL_EMAIL,
        "editor",
        PROJECT_ID,
        3,
        True,
        extra={
            "route": "create_invitation",
            "outcome": "ok",
            "latency_ms": 4.2,
            "project_id": PROJECT_ID,
            "invitee_email": SENTINEL_EMAIL,
            "note_text": SENTINEL,
            "detail": SENTINEL,
        },
    )
    private.info("pin at %.4f, %.4f for %s", 51.5012, -0.1419, PROJECT_ID)  # a float is never logged
    private.info({"title": SENTINEL})  # a non-string message is written as its type only
    logging.getLogger("web.my_projects").warning("note %s", SENTINEL)
    expect_no_sentinel(stream)
    first, pin, as_object, web = parsed(stream)
    expect(first["message"] == f"invited REDACTED as REDACTED to {PROJECT_ID} (3 pending, owner=True)", first)
    kept = {k: first.get(k) for k in ("route", "outcome", "latency_ms", "project_id")}
    expect(
        kept == {"route": "create_invitation", "outcome": "ok", "latency_ms": 4.2, "project_id": PROJECT_ID},
        first,
    )
    expect(set(first["redacted_fields"]) == {"invitee_email", "note_text", "detail"}, first)
    expect(pin["message"] == f"pin at REDACTED, REDACTED for {PROJECT_ID}", pin)
    expect("51.50" not in pin["message"], pin)
    expect(as_object["message"] == "<dict>" and web["message"] == "note REDACTED", (as_object, web))


INVITATION_PATHS = [
    (f"/my/invitations/accept?t={SENTINEL_TOKEN}", "/my/invitations/accept?t=REDACTED"),
    (f"/my/invitations/accept?x=1&t={SENTINEL_TOKEN}&y=2", "/my/invitations/accept?x=1&t=REDACTED&y=2"),
    (
        f"/login?next=%2Fmy%2Finvitations%2Faccept%3Ft%3D{SENTINEL_TOKEN}",
        "/login?next=%2Fmy%2Finvitations%2Faccept%3Ft%3DREDACTED",
    ),
    (
        f"/login?next=%252Fmy%252Finvitations%252Faccept%253Ft%253D{SENTINEL_TOKEN}",
        "/login?next=%252Fmy%252Finvitations%252Faccept%253Ft%253DREDACTED",
    ),
    (f"/my/invitations/{SENTINEL_TOKEN}", "/my/invitations/REDACTED"),
    (f"/MY/Invitations/accept?T={SENTINEL_TOKEN}", "/MY/Invitations/accept?T=REDACTED"),
    # `%74` is `t`: Starlette decodes the name, so this is the token too.
    (f"/my/invitations/accept?%74={SENTINEL_TOKEN}", "/my/invitations/accept?%74=REDACTED"),
    (
        f"/login?next=%2Fmy%2Finvitations%2Faccept%3F%2574%3D{SENTINEL_TOKEN}",
        "/login?next=%2Fmy%2Finvitations%2Faccept%3F%2574%3DREDACTED",
    ),
    # A link in HTML text (an email body, a rendered page) separates parameters with `&amp;`.
    (f"/my/invitations/accept?x=1&amp;t={SENTINEL_TOKEN}", "/my/invitations/accept?x=1&amp;t=REDACTED"),
]


@pytest.mark.parametrize(("path", "written"), INVITATION_PATHS)
def test_an_invitation_token_is_never_logged(bare_root: logging.Logger, path: str, written: str) -> None:
    """uvicorn's access line carries the path with its query string; the same URL can also arrive
    as an extra or inside an exception. The token goes; the rest of each line stays."""
    stream = logged(bare_root)
    logging.getLogger("uvicorn.access").info(ACCESS_FORMAT, "203.0.113.9:51234", "GET", path, "1.1", 302)
    logging.getLogger("web.app").warning("api unavailable", extra={"url": f"https://example.test{path}"})
    try:
        raise ValueError(f"bad redirect target {path}")
    except ValueError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    expect_no_sentinel(stream)
    access, extra, error = parsed(stream)
    expect(access["message"] == f'203.0.113.9:51234 - "GET {written} HTTP/1.1" 302', access)
    expect(extra["url"] == f"https://example.test{written}", extra)
    expect(f"ValueError: bad redirect target {written}" in error["exc_info"], error)


def test_public_paths_and_other_parameters_keep_their_values(bare_root: logging.Logger) -> None:
    stream = logged(bare_root)
    for path in (
        "/v1/proposals?state=US-TX&at=5&t2=x&st=1&%74x=1",
        "/my/invitations/accept",
        "/feeds/saved/abc123",
    ):
        logging.getLogger("uvicorn.access").info(ACCESS_FORMAT, "203.0.113.9:51234", "GET", path, "1.1", 200)
    messages = [r["message"] for r in parsed(stream)]
    expect("REDACTED" not in " ".join(messages), messages)


TOKEN_FIELDS = [
    pytest.param({"query": f"t={SENTINEL_TOKEN}&x=1"}, {"query": "t=REDACTED&x=1"}, id="bare-query-string"),
    pytest.param(
        {"params": {"t": SENTINEL_TOKEN, "x": "1"}},
        {"params": {"t": "REDACTED", "x": "1"}},
        id="query-params-dict",
    ),
    pytest.param({"t": SENTINEL_TOKEN, "n": 1}, {"t": "REDACTED", "n": 1}, id="t-extra"),
    pytest.param(
        {"html": f'<a href="/my/invitations/accept?x=1&amp;t={SENTINEL_TOKEN}">'},
        {"html": '<a href="/my/invitations/accept?x=1&amp;t=REDACTED">'},
        id="html-text",
    ),
    pytest.param(
        {"url": f"/my/invitations/accept?%74={SENTINEL_TOKEN}"},
        {"url": "/my/invitations/accept?%74=REDACTED"},
        id="percent-encoded-name",
    ),
]


@pytest.mark.parametrize(("extra", "written"), TOKEN_FIELDS)
def test_an_invitation_token_in_a_field_is_never_logged(
    bare_root: logging.Logger, extra: dict[str, Any], written: dict[str, Any]
) -> None:
    """The shapes a request's query reaches a field in: `request.url.query` (no `?`),
    `dict(request.query_params)` (a `t` key), HTML text and a percent-encoded name."""
    stream = logged(bare_root)
    logging.getLogger("web.app").info("request", extra=extra)
    expect_no_sentinel(stream)
    (record,) = parsed(stream)
    expect({key: record[key] for key in written} == written, record)


def test_an_invitation_token_in_a_mapping_s_text_is_never_logged(bare_root: logging.Logger) -> None:
    """A `t` key's value rendered as text: a dict or pairs as a message argument, JSON, an
    exception message. The key stays, so the line still shows a token was there."""
    stream = logged(bare_root)
    logger = logging.getLogger("web.app")
    logger.info("params %s", {"t": SENTINEL_TOKEN, "x": "1"})
    logger.info("pairs %s", [("t", SENTINEL_TOKEN), ("x", "1")])
    logger.info("json %s", json.dumps({"t": SENTINEL_TOKEN}))
    try:
        raise ValueError(f"bad params { ({'t': SENTINEL_TOKEN}) }")
    except ValueError:
        logger.exception("accept failed")
    expect_no_sentinel(stream)
    as_dict, pairs, as_json, failed = parsed(stream)
    expect(as_dict["message"] == "params {'t': 'REDACTED', 'x': '1'}", as_dict)
    expect(pairs["message"] == "pairs [('t', 'REDACTED'), ('x', '1')]", pairs)
    expect(as_json["message"] == 'json {"t": "REDACTED"}', as_json)
    expect("ValueError: bad params {'t': 'REDACTED'}" in failed["exc_info"], failed)


def test_an_email_address_is_never_logged(bare_root: logging.Logger) -> None:
    stream = logged(bare_root)
    logger = logging.getLogger("services.api.auth_routes")
    logger.info("invitation sent to %s", SENTINEL_EMAIL)
    logger.info(
        "lookup", extra={"recipients": [SENTINEL_EMAIL], "query": "q=invitee.sentinel%40example.test"}
    )
    try:
        raise LookupError(f"no verified user for {SENTINEL_EMAIL.upper()}")
    except LookupError:
        logger.exception("accept failed")
    expect_no_sentinel(stream)
    expect("invitee.sentinel%40example.test" not in stream.getvalue(), "a percent-encoded address was logged")
    sent, lookup, failed = parsed(stream)
    expect(sent["message"] == "invitation sent to REDACTED", sent)
    expect(lookup["recipients"] == ["REDACTED"] and lookup["query"] == "q=REDACTED", lookup)
    expect("LookupError: no verified user for REDACTED" in failed["exc_info"], failed)


ADDRESS_FORMS = [
    pytest.param('"invitee sentinel"@example.test', id="quoted-local-part"),
    pytest.param("first/invitee.sentinel@example.test", id="slash-in-local-part"),
    pytest.param("invitee.sentinel!x$y*z^w~v@example.test", id="other-atext"),
    pytest.param("invitee.sentinel%2540example.test", id="double-percent-encoded"),
    pytest.param("invitee.sentinel＠example.test", id="fullwidth-at"),
    pytest.param("invitee.sentinel﹫example.test", id="small-at"),
]


@pytest.mark.parametrize("address", ADDRESS_FORMS)
def test_every_form_of_an_address_is_redacted_whole(bare_root: logging.Logger, address: str) -> None:
    """No part of the address is left behind: not the quoted name, not a `first/` prefix."""
    stream = logged(bare_root)
    logger = logging.getLogger("services.api.auth_routes")
    logger.info("invitation sent to %s", address)
    logger.info("lookup", extra={"to": address, "query": f"email={address}&x=1"})
    text = stream.getvalue()
    expect("invitee" not in text and "sentinel" not in text and "first" not in text, text)
    sent, lookup = parsed(stream)
    expect(sent["message"] == "invitation sent to REDACTED", sent)
    expect(lookup["to"] == "REDACTED" and lookup["query"] == "email=REDACTED&x=1", lookup)


def test_a_failed_statement_logs_its_sql_but_not_its_values(bare_root: logging.Logger) -> None:
    """SQLAlchemy puts a failed statement's bound values in the exception text (`[parameters:
    ...]`), and Postgres quotes the failing row (`DETAIL:`). Both are a note, a title or an email
    as often as not; the statement and the error type stay, for debugging."""
    import sqlalchemy as sa

    stream = logged(bare_root)
    engine = sa.create_engine("sqlite://")
    notes = sa.Table(
        "private_project_note",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("body", sa.Text, unique=True),
    )
    notes.metadata.create_all(engine)
    logger = logging.getLogger("services.api.request_context")
    with engine.begin() as connection:
        connection.execute(notes.insert().values(body=SENTINEL))
    try:
        with engine.begin() as connection:
            connection.execute(notes.insert().values(body=SENTINEL))
    except sa.exc.IntegrityError:
        logger.exception("unhandled_error request_id=%s", "req_abc123def456")
    postgres = (
        '(psycopg.errors.CheckViolation) new row for relation "private_project" violates check constraint\n'  # noqa: S608 -- the text of an error message, never executed
        f"DETAIL:  Failing row contains (1, {PROJECT_ID}, {SENTINEL}, {SENTINEL_EMAIL}).\n"
        "[SQL: INSERT INTO private_project (public_id, title) VALUES (%(public_id)s, %(title)s)]\n"
        f"[parameters: {{'public_id': '{PROJECT_ID}', 'title': '{SENTINEL}'}}]"
    )
    try:
        raise RuntimeError(postgres)
    except RuntimeError:
        logger.exception("unhandled_error request_id=%s", "req_abc123def457")
    expect_no_sentinel(stream)
    sqlite, pg = parsed(stream)
    expect("IntegrityError" in sqlite["exc_info"], sqlite)
    expect("[SQL: INSERT INTO private_project_note (body) VALUES (?)]" in sqlite["exc_info"], sqlite)
    expect("[parameters: REDACTED]" in sqlite["exc_info"], sqlite)
    expect("DETAIL:  REDACTED" in pg["exc_info"] and "[parameters: REDACTED]" in pg["exc_info"], pg)
    expect(sqlite["message"] == "unhandled_error request_id=req_abc123def456", sqlite)


def test_a_validation_error_logs_its_rule_but_not_its_input(bare_root: logging.Logger) -> None:
    import pydantic

    class NoteIn(pydantic.BaseModel):
        body: str = pydantic.Field(max_length=5)

    stream = logged(bare_root)
    try:
        NoteIn(body=SENTINEL)
    except pydantic.ValidationError:
        logging.getLogger("services.api.request_context").exception("unhandled_error")
    expect_no_sentinel(stream)
    (record,) = parsed(stream)
    expect("string_too_long" in record["exc_info"] and "input_value=REDACTED" in record["exc_info"], record)


def test_an_input_that_quotes_the_input_type_is_still_redacted_whole(bare_root: logging.Logger) -> None:
    """The user's own text can hold `, input_type=`; the value runs to the last one on the line."""
    import pydantic

    class NoteIn(pydantic.BaseModel):
        body: str = pydantic.Field(max_length=5)

    stream = logged(bare_root)
    try:
        NoteIn(body=f"aaaa, input_type=str] {SENTINEL}")
    except pydantic.ValidationError:
        logging.getLogger("services.api.request_context").exception("unhandled_error")
    expect_no_sentinel(stream)
    (record,) = parsed(stream)
    expect("[type=string_too_long, input_value=REDACTED, input_type=str]" in record["exc_info"], record)


def _as_private_module(*functions: Any) -> dict[str, Any]:
    """The functions rebuilt with `services.projects.probe` as their module, so their frames look
    exactly like private code's to anything walking a traceback."""
    module: dict[str, Any] = {"__name__": "services.projects.probe"}
    for fn in functions:
        module[fn.__name__] = types.FunctionType(fn.__code__, module, fn.__name__)
    return module


def _fail(text: str) -> None:
    raise ValueError(text)


def _fail_chained(text: str) -> None:
    try:
        _fail(text)
    except ValueError as exc:
        wrapped = RuntimeError(f"could not save {text}")
        wrapped.add_note(f"note: {text}")
        raise wrapped from exc


def test_an_exception_from_private_code_keeps_type_and_frames_not_message(bare_root: logging.Logger) -> None:
    """Logged on a public logger (uvicorn's error line for an exception escaping a private route),
    the exception's frames show where it came from; its messages and notes are withheld."""
    stream = logged(bare_root)
    probe = _as_private_module(_fail, _fail_chained)
    try:
        probe["_fail_chained"](SENTINEL)
    except RuntimeError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    expect_no_sentinel(stream)
    (record,) = parsed(stream)
    text = record["exc_info"]
    expect("ValueError: REDACTED (message withheld)" in text, text)
    expect("RuntimeError: REDACTED (message withheld)" in text, text)
    expect("The above exception was the direct cause of the following exception:" in text, text)
    expect(", in _fail\n" in text and ", in _fail_chained\n" in text, f"frames missing: {text}")
    expect("note:" not in text, text)


def test_an_exception_on_a_private_path_keeps_the_request_id_not_the_message(
    bare_root: logging.Logger,
) -> None:
    """The API's 500 line (services/api/request_context.py) for a /v1/my/ request: the exception
    was raised outside private code, but the record names the private path."""
    stream = logged(bare_root)
    try:
        raise KeyError(SENTINEL)
    except KeyError as exc:
        logging.getLogger("services.api.request_context").error(
            "unhandled_error request_id=%s path=%s error_type=%s",
            "req_abc123def456",
            f"/v1/my/projects/{PROJECT_ID}/notes",
            type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    expect_no_sentinel(stream)
    (record,) = parsed(stream)
    expect(
        record["message"]
        == f"unhandled_error request_id=req_abc123def456 path=/v1/my/projects/{PROJECT_ID}/notes "
        "error_type=KeyError",
        record,
    )
    expect("KeyError: REDACTED (message withheld)" in record["exc_info"], record)


def test_a_private_path_named_by_an_object_a_nested_field_or_the_message_withholds_it(
    bare_root: logging.Logger,
) -> None:
    """The path can arrive as Starlette's URL object (`request.url`), inside a nested extra, or in
    an f-string message; each still marks the exception as a private request's."""
    from starlette.datastructures import URL

    stream = logged(bare_root)
    logger = logging.getLogger("web.app")
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any] | None]] = [
        ("page failed for %s", (URL(f"https://example.test/my/projects/{PROJECT_ID}"),), None),
        ("page failed", (), {"request": {"path": f"/v1/my/projects/{PROJECT_ID}"}}),
        (f"page failed for /api/my/pins?project={PROJECT_ID}", (), None),
    ]
    for msg, args, extra in calls:
        try:
            raise ValueError(f"could not render {SENTINEL}")
        except ValueError:
            logger.exception(msg, *args, extra=extra)
    expect_no_sentinel(stream)
    for record in parsed(stream):
        expect("ValueError: REDACTED (message withheld)" in record["exc_info"], record)


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _send(message: dict[str, Any]) -> None:
    return None


def _http_scope(path: str) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("203.0.113.9", 51234),
    }


def _serve_like_uvicorn(app: Callable[..., Any], path: str) -> None:
    """uvicorn's `run_asgi` (uvicorn/protocols/http/h11_impl.py): call the app, log whatever escapes
    on `uvicorn.error` with no path in the record. This frame holds no `scope` of its own."""

    async def run_asgi() -> None:
        try:
            await app(_http_scope(path), _receive, _send)
        except BaseException as exc:
            logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=exc)

    asyncio.run(run_asgi())


class _PublicMiddleware:
    """A public module's middleware: the exception passes through it on its way out."""

    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)


async def _failing_public_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    raise ValueError(f"could not serialise {SENTINEL}")


@pytest.mark.parametrize(
    "path", ["/v1/my/projects/prj_01HABCDEFGHJKMNPQRSTVW", "/my/projects", "/api/my/pins"]
)
def test_an_exception_while_serving_a_private_path_is_withheld(bare_root: logging.Logger, path: str) -> None:
    """Raised by public code (a serialiser, a middleware) while serving a private path: no private
    frame, no private logger, no path in the record. The ASGI frames' `scope` says it all."""
    stream = logged(bare_root)
    _serve_like_uvicorn(_PublicMiddleware(_failing_public_app), path)
    expect_no_sentinel(stream)
    (record,) = parsed(stream)
    expect("ValueError: REDACTED (message withheld)" in record["exc_info"], record)
    expect(", in _failing_public_app\n" in record["exc_info"], f"frames missing: {record}")


def test_an_exception_while_serving_a_public_path_keeps_its_message(bare_root: logging.Logger) -> None:
    stream = logged(bare_root)
    _serve_like_uvicorn(_PublicMiddleware(_failing_public_app), "/v1/proposals/prop_01HABCDEFGHJKM")
    (record,) = parsed(stream)
    expect(f"ValueError: could not serialise {SENTINEL}" in record["exc_info"], record)


VALIDATION_ERRORS = [
    {
        "type": "string_too_long",
        "loc": ("response", "title"),
        "msg": "String should have at most 200 characters",
        "input": SENTINEL,
        "ctx": {"max_length": 200},
    },
    {"type": "missing", "loc": ("response", "stage"), "msg": "Field required", "input": {"note": SENTINEL}},
]


@pytest.mark.parametrize(
    "endpoint_ctx",
    [
        pytest.param(
            {"function": "get_project", "path": "GET /v1/my/projects/{project_id}"}, id="private-route"
        ),
        pytest.param(
            {
                "file": "/app/services/api/my_projects.py",
                "line": 40,
                "function": "get_project",
                "path": "GET /v1/projects/{project_id}",
            },
            id="private-module",
        ),
    ],
)
def test_a_fastapi_validation_error_for_a_private_endpoint_keeps_its_types_not_its_input(
    bare_root: logging.Logger, endpoint_ctx: dict[str, Any]
) -> None:
    """FastAPI checks a response against its model after the endpoint has returned, so no private
    frame is left; its `endpoint_ctx` still names the route and the module. The withheld line
    keeps what a developer needs: the error types, the route template and the function."""
    from fastapi.exceptions import ResponseValidationError

    stream = logged(bare_root)
    try:
        raise ResponseValidationError(VALIDATION_ERRORS, body={"title": SENTINEL}, endpoint_ctx=endpoint_ctx)  # type: ignore[arg-type]
    except ResponseValidationError as exc:
        logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=exc)
    expect_no_sentinel(stream)
    (record,) = parsed(stream)
    expect(
        "fastapi.exceptions.ResponseValidationError: REDACTED (message withheld; 2 errors: missing, "
        f"string_too_long; endpoint {endpoint_ctx['path']}; in get_project)" in record["exc_info"],
        record,
    )


def test_a_fastapi_validation_error_for_a_public_endpoint_keeps_its_message(
    bare_root: logging.Logger,
) -> None:
    from fastapi.exceptions import ResponseValidationError

    stream = logged(bare_root)
    ctx = {"function": "get_proposal", "path": "GET /v1/proposals/{public_id}"}
    try:
        raise ResponseValidationError(VALIDATION_ERRORS[:1], endpoint_ctx=ctx)  # type: ignore[arg-type]
    except ResponseValidationError as exc:
        logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=exc)
    (record,) = parsed(stream)
    expect(
        f"'input': '{SENTINEL}'" in record["exc_info"], f"a public route's error lost its detail: {record}"
    )


def _at(filename: str, fn: Callable[..., Any], module: str = "__main__") -> Callable[..., Any]:
    """`fn` as if defined in `filename` under the module name `module`: what logging's call-site
    lookup and a traceback see for code in that file."""
    namespace: dict[str, Any] = {"__name__": module, "logging": logging, "SENTINEL": SENTINEL}
    code = fn.__code__.replace(co_filename=filename)
    return types.FunctionType(code, namespace, fn.__name__)


def _load_checklist(title: str) -> None:
    logging.getLogger(__name__).info("loading %s", title, extra={"pack_title": title, "item_count": 3})
    raise ValueError(f"unmatched key in {title}")


def test_a_private_module_run_as_main_is_private_by_its_file(bare_root: logging.Logger) -> None:
    """`python -m services.projects.load_checklist` (docs/19 §2.3): the module and its logger are
    both `__main__`, so only the file says the code is private."""
    stream = logged(bare_root)
    load = _at("/app/services/projects/load_checklist.py", _load_checklist)
    try:
        load(SENTINEL)
    except ValueError:
        logging.getLogger("infra.scheduler.jobs").exception("checklist load failed")
    expect_no_sentinel(stream)
    loading, failed = parsed(stream)
    expect(loading["logger"] == "__main__" and loading["message"] == "loading REDACTED", loading)
    expect(loading["item_count"] == 3 and loading["redacted_fields"] == ["pack_title"], loading)
    expect("ValueError: REDACTED (message withheld)" in failed["exc_info"], failed)
    expect(", in _load_checklist\n" in failed["exc_info"], f"frames missing: {failed}")


def _log_on(logger_name: str, msg: str, *args: Any, **extra: Any) -> None:
    logging.getLogger(logger_name).info(msg, *args, extra=extra)


def test_account_erasure_and_the_retention_entry_point_log_as_private_code(bare_root: logging.Logger) -> None:
    """The two other modules that may import services.projects (docs/19 §5.2 item 1). Erasure is a
    private namespace; the retention entry point's calls are private on the logger it shares with
    the snapshot rule, whose line (services/retention/snapshots.py) is written exactly as before."""
    stream = logged(bare_root)
    logging.getLogger("services.api.account_erasure").info(
        "deleted project %s", SENTINEL, extra={"project_count": 2, "detail": SENTINEL}
    )
    run = _at("/app/services/retention/run.py", _log_on, module="services.retention.run")
    run("services.retention", "cleared invitations to %s", SENTINEL, invitation_count=4)
    snapshots = _at("/app/services/retention/snapshots.py", _log_on, module="services.retention.snapshots")
    snapshot_line = (
        "retention: %s %s/%s: %s of %s raw snapshots %s",
        "local",
        "raw",
        "ferc",
        3,
        10,
        "deleted",
    )
    snapshots("services.retention", *snapshot_line, source_id="ferc", medium="local", tree="raw", count=3)
    expect_no_sentinel(stream)
    erased, cleared, kept = parsed(stream)
    expect(erased["message"] == "deleted project REDACTED" and erased["project_count"] == 2, erased)
    expect(erased["redacted_fields"] == ["detail"], erased)
    expect(
        cleared["message"] == "cleared invitations to REDACTED" and cleared["invitation_count"] == 4, cleared
    )
    expect(kept["message"] == "retention: local raw/ferc: 3 of 10 raw snapshots deleted", kept)
    expect(
        {k: kept[k] for k in ("source_id", "medium", "tree", "count")}
        == {"source_id": "ferc", "medium": "local", "tree": "raw", "count": 3}
        and "redacted_fields" not in kept,
        kept,
    )


def test_a_private_record_s_allowed_names_carry_only_their_own_shape(bare_root: logging.Logger) -> None:
    """Rule 3 allows names, then values in that name's shape: `outcome=str(exc)`, a title under an
    `_id` name or a nested object under `_ids` do not pass."""
    stream = logged(bare_root)
    try:
        raise ValueError(f"could not save {SENTINEL}")
    except ValueError as exc:
        outcome = str(exc)
    allowed = {
        "route": "/v1/my/projects/{project_id}/notes",
        "action": "edit",
        "outcome": "refused",
        "status": "ok",
        "status_code": 201,
        "method": "POST",
        "path": f"/v1/my/projects/{PROJECT_ID}/notes",
        "role": "editor",
        "error_type": "sqlalchemy.exc.IntegrityError",
        "error_code": "not_found",
        "project_id": PROJECT_ID,
        "note_ids": ["nte_01HABCDEFGHJKMNPQRSTVW"],
        "item_count": 3,
        "latency_ms": 4.2,
    }
    refused = {
        "outcome": outcome,
        "route": f"{SENTINEL} notes",
        "title_id": SENTINEL,
        "project_ids": [PROJECT_ID, {"name": SENTINEL}],
        "latency_ms": SENTINEL,
        "error_count": [SENTINEL],
    }
    logger = logging.getLogger("services.projects.notes")
    logger.info("note saved", extra=allowed)
    logger.info("note refused", extra=refused)
    expect_no_sentinel(stream)
    saved, failed = parsed(stream)
    expect({k: saved[k] for k in allowed} == allowed and "redacted_fields" not in saved, saved)
    expect(all(failed[k] == "REDACTED" for k in refused), failed)
    expect(failed["redacted_fields"] == sorted(refused), failed)


def test_a_model_or_mapping_from_a_public_module_loses_its_private_fields(bare_root: logging.Logger) -> None:
    """Rule 1 is not only for extras: a pydantic model, a dataclass or a dict passed as a message
    argument, as the message, or as an extra loses its private fields and keeps the rest."""
    import pydantic

    class NoteIn(pydantic.BaseModel):
        project_id: str
        title: str
        body: str

    @dataclasses.dataclass
    class Pin:
        public_id: str
        lat: float
        lon: float

    note = NoteIn(project_id=PROJECT_ID, title=SENTINEL, body=SENTINEL)
    pin = Pin(PROJECT_ID, 51.501, -0.142)
    stream = logged(bare_root)
    logger = logging.getLogger("services.api.listing")
    logger.info("saved %s", note)
    logger.info("pinned %r", pin)
    logger.info("payload %s", {"title": SENTINEL, "n": 1, "items": [{"note": SENTINEL, "k": 2}]})
    logger.info("%(title)s for %(project_id)s", {"title": SENTINEL, "project_id": PROJECT_ID})
    logger.info({"title": SENTINEL, "n": 1})
    logger.info("as extras", extra={"payload": note, "pin": pin})
    expect_no_sentinel(stream)
    expect("51.501" not in stream.getvalue(), "a coordinate reached the log")
    saved, pinned, payload, mapping, as_message, as_extras = parsed(stream)
    expect(saved["message"] == f"saved NoteIn(project_id='{PROJECT_ID}')", saved)
    expect(saved["redacted_fields"] == ["args[0].body", "args[0].title"], saved)
    expect(pinned["message"] == f"pinned Pin(public_id='{PROJECT_ID}')", pinned)
    # One dict argument is the format's mapping (stdlib logging), so its own keys stay, REDACTED.
    expect(payload["message"] == "payload {'title': 'REDACTED', 'n': 1, 'items': [{'k': 2}]}", payload)
    expect(mapping["message"] == f"REDACTED for {PROJECT_ID}", mapping)
    expect(as_message["message"] == "{'n': 1}", as_message)
    expect(
        as_extras["payload"] == {"project_id": PROJECT_ID} and as_extras["pin"] == {"public_id": PROJECT_ID},
        as_extras,
    )
    expect(note.title == SENTINEL and pin.lat == 51.501, "the caller's objects were changed")


def test_a_model_without_private_fields_is_written_as_before(bare_root: logging.Logger) -> None:
    import pydantic

    class Bbox(pydantic.BaseModel):
        west: float
        south: float

    box = Bbox(west=-1.0, south=50.0)
    record = _record("services.api.geo", "bbox %s %r", (box, box), {"bbox": box}, False)
    before = JsonFormatter().format(copy.copy(record))
    stream = logged(bare_root)
    logging.getLogger().handlers[-1].handle(copy.copy(record))
    expect(stream.getvalue().strip() == before, f"\nbefore: {before}\nafter:  {stream.getvalue().strip()}")


def test_an_extra_with_a_non_string_key_never_raises_into_the_caller(bare_root: logging.Logger) -> None:
    """`extra={1: ...}` puts an int key on the record. The filter drops it, also when the record
    is withheld for another reason; the caller sees no error either way."""
    stream = logged(bare_root)
    logger = logging.getLogger("pipeline.test")
    tick_extra: dict[Any, Any] = {1: SENTINEL, "n": 1}
    tock_extra: dict[Any, Any] = {2: SENTINEL, "odd": _Unprintable()}
    logger.info("tick", extra=tick_extra)
    logger.info("tock %s", SENTINEL, extra=tock_extra)
    expect_no_sentinel(stream)
    tick, tock = parsed(stream)
    expect(tick["message"] == "tick" and tick["n"] == 1 and tick["redacted_fields"] == ["<int key>"], tick)
    expect(tock["message"] == WITHHELD_MESSAGE and set(tock) == {"ts", "level", "logger", "message"}, tock)


def _record(
    name: str, msg: str, args: tuple[Any, ...], extra: dict[str, Any], exc: bool
) -> logging.LogRecord:
    exc_info = None
    if exc:
        try:
            raise TimeoutError("upstream took 30.0 s")
        except TimeoutError:
            exc_info = sys.exc_info()
    return logging.getLogger(name).makeRecord(
        name, logging.INFO, __file__, 1, msg, args, exc_info, extra=extra
    )


PUBLIC_LINES = [
    (
        "infra.scheduler.jobs",
        "%s %s",
        ("alert_tick", "emails_refused=0 emails_sent=3 error_count=0"),
        {"job": "alert_tick", "emails_sent": 3, "emails_refused": 0, "error_count": 0},
        False,
    ),
    (
        "web.csp_reports",
        "csp violation: %s blocked %s on %s (%s)",
        ("script-src", "https://cdn.example.test", "/proposals/{public_id}", "report"),
        {"csp_directive": "script-src", "csp_page": "/proposals/{public_id}", "csp_disposition": "report"},
        False,
    ),
    (
        "uvicorn.access",
        ACCESS_FORMAT,
        ("203.0.113.9:443", "GET", "/v1/proposals?state=US-TX&limit=50", "1.1", 200),
        {},
        False,
    ),
    (
        "infra.scheduler.jobs",
        "source %s is %s",
        ("ferc", "stale"),
        {
            "source_id": "ferc",
            "alerting": [{"source_id": "ercot", "status": "stale"}],
            "by_status": {"ok": 3},
        },
        False,
    ),
    (
        "pipeline.connectors.runner",
        "snapshot written",
        (),
        {
            "path": pathlib.Path("/data/ferc/run.json"),
            "queues": ("default", "browser"),
            "when": dt.date(2026, 10, 22),
        },
        False,
    ),
    ("web.app", "api unavailable", (), {"path": "/proposals/prop_01HABCDEFGHJKM", "api_status": 503}, True),
    ("infra.scheduler.app", "%d%% of buckets due, 100%% checked", (40,), {"due_count": 2}, False),
]


@pytest.mark.parametrize(("name", "msg", "args", "extra", "exc"), PUBLIC_LINES)
def test_public_lines_are_written_exactly_as_before(
    bare_root: logging.Logger, name: str, msg: str, args: tuple[Any, ...], extra: dict[str, Any], exc: bool
) -> None:
    """The filter changes nothing a public line carried before: same message, fields, values and
    exception text, byte for byte (the JSON formatter alone is the reference)."""
    record = _record(name, msg, args, extra, exc)
    before = JsonFormatter().format(copy.copy(record))
    stream = logged(bare_root)
    logging.getLogger().handlers[-1].handle(copy.copy(record))
    expect(stream.getvalue().strip() == before, f"\nbefore: {before}\nafter:  {stream.getvalue().strip()}")


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError(SENTINEL)


def test_a_record_that_cannot_be_redacted_is_withheld_and_the_caller_never_sees_an_error(
    bare_root: logging.Logger,
) -> None:
    stream = logged(bare_root)
    logging.getLogger("pipeline.test").info(
        "tick %s", SENTINEL_EMAIL, extra={"odd": _Unprintable(), "x": SENTINEL}
    )
    expect_no_sentinel(stream)
    (record,) = parsed(stream)
    expect(record["message"] == "log record withheld: redaction failed", record)
    expect(set(record) == {"ts", "level", "logger", "message"}, record)


def test_handlers_after_the_filter_see_the_redacted_record(bare_root: logging.Logger) -> None:
    """The filter rewrites the record in place, so a handler after the JSON one gets the redacted
    version. An exception whose text was withheld leaves no `exc_info` to re-render the original
    from; `exc_text` holds the redacted rendering, which a standard formatter writes."""
    logged(bare_root)
    seen: list[logging.LogRecord] = []

    class Later(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    later = Later()
    later.setFormatter(logging.Formatter("%(message)s"))
    bare_root.addHandler(later)
    try:
        raise ValueError(f"could not save {SENTINEL}")
    except ValueError:
        logging.getLogger("services.api.my_projects").exception(
            "note %s", SENTINEL, extra={"title": SENTINEL}
        )
    (record,) = seen
    expect(record.exc_info is None, "the original exception is still on the record")
    text = later.format(record) + json.dumps({k: str(v) for k, v in record.__dict__.items()})
    expect(SENTINEL not in text, text)
    expect("ValueError: REDACTED (message withheld)" in later.format(record), later.format(record))


def test_sentry_s_logging_integration_gets_no_private_content() -> None:
    """A real sentry-sdk (the pinned one) with its logging integration, which builds an event from
    the record after the handlers have run: from `exc_info` when the record has one. A private
    record's event carries the redacted message and no exception; a public exception with nothing
    to redact keeps its full exception. Run in its own interpreter: the integration patches
    `logging.Logger` for the whole process. (An unhandled exception that sentry-sdk's ASGI
    integration reports itself is `infra/observability.py`'s `before_send`, not this filter.)"""
    pytest.importorskip("sentry_sdk")
    script = textwrap.dedent(
        f"""
        import io, json, logging
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.transport import Transport
        from infra.logging_config import configure_logging

        events = []

        class Captured(Transport):
            def capture_envelope(self, envelope):
                events.extend(item.payload.json for item in envelope.items if item.type == "event")

        configure_logging(level="INFO", stream=io.StringIO())
        sentry_sdk.init(
            dsn="https://public@sentry.invalid/1",
            transport=Captured,
            default_integrations=False,
            integrations=[LoggingIntegration(level=None, event_level=logging.ERROR)],
        )
        try:
            raise ValueError("could not save {SENTINEL}")
        except ValueError as exc:
            logging.getLogger("services.api.request_context").error(
                "unhandled_error request_id=%s path=%s", "req_abc123def456", "/v1/my/projects/x", exc_info=exc
            )
        try:
            raise LookupError("no verified user for {SENTINEL_EMAIL}")
        except LookupError:
            logging.getLogger("services.api.auth_routes").exception("accept failed")
        try:
            raise TimeoutError("upstream took 30.0 s")
        except TimeoutError:
            logging.getLogger("web.app").exception("api unavailable")
        sentry_sdk.flush()
        print(json.dumps(events, default=str))
        """
    )
    result = subprocess.run(  # noqa: S603 -- fixed argv, our own interpreter, no shell
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(__file__).resolve().parents[1],
        check=True,
    )
    for sentinel in SENTINELS:
        expect(sentinel not in result.stdout, f"{sentinel!r} reached a Sentry event: {result.stdout}")
    private, address, public = json.loads(result.stdout)
    expect(
        private["logentry"]["formatted"]
        == "unhandled_error request_id=req_abc123def456 path=/v1/my/projects/x",
        private,
    )
    expect("exception" not in private and "exception" not in address, (private, address))
    values = public["exception"]["values"]
    expect([(v["type"], v["value"]) for v in values] == [("TimeoutError", "upstream took 30.0 s")], values)


def test_the_filter_is_installed_once_and_applies_twice_safely(bare_root: logging.Logger) -> None:
    stream = io.StringIO()
    handler = configure_logging(level="INFO", stream=stream)
    configure_logging(level="INFO", stream=stream)
    expect(len(handler.filters) == 1, f"one filter expected: {handler.filters}")
    record = _record("pipeline.test", "sent to %s", (SENTINEL_EMAIL,), {"title": SENTINEL, "n": 1}, False)
    handler.handle(record)
    handler.handle(record)  # the same record again: the filter runs over its own output
    once, twice = stream.getvalue().splitlines()
    expect(once == twice, f"a second pass changed the line:\n{once}\n{twice}")
    expect_no_sentinel(stream)


PATHOLOGICAL = {
    "a long word run (a base64 blob)": "a" * 20_000,
    "many input_value= with no input_type": "input_value=" * 1_700,
    "many percent-encoded @": "a%40" * 5_000,
    "a long domain with no dot": "a@" + "b" * 20_000,
    "many encoded separators": "%253F" * 4_000,
    "many invitation path prefixes": "/my/invitations/" * 1_250,
}


@pytest.mark.parametrize("text", PATHOLOGICAL.values(), ids=list(PATHOLOGICAL))
def test_redaction_stays_linear_in_the_length_of_a_line(bare_root: logging.Logger, text: str) -> None:
    """Every record passes through the patterns on the request's own thread. A pattern that
    rescans from each position (the email pattern did, before its lookbehind) turns one logged
    blob into seconds of CPU per line; these 20,000-character lines took 7 s that way."""
    stream = logged(bare_root)
    started = time.perf_counter()
    logging.getLogger("uvicorn.access").info(ACCESS_FORMAT, "203.0.113.9:443", "GET", f"/{text}", "1.1", 404)
    elapsed = time.perf_counter() - started
    expect(elapsed < 0.5, f"one {len(text)}-character line took {elapsed:.2f} s to redact")
    expect(len(parsed(stream)) == 1, "the line was not written")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve(script: str, paths: list[str]) -> tuple[list[int], str]:
    """Run `script` (which starts uvicorn on the port it is formatted with), request each path,
    stop it, and return the statuses and everything the process wrote."""
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603 -- fixed argv, our own interpreter, no shell
        [sys.executable, "-c", script.format(port=port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=pathlib.Path(__file__).resolve().parents[1],
    )
    # No keep-alive: after a 500 that the API's own handler has already sent, uvicorn closes the
    # connection, and a pooled one could be reused before the close arrives.
    no_reuse = httpx.Limits(max_keepalive_connections=0)
    try:
        deadline = time.monotonic() + 20
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5.0, limits=no_reuse
        ) as client:
            while True:
                try:
                    client.get("/health")
                    break
                except httpx.TransportError:
                    expect(process.poll() is None, "uvicorn exited")
                    expect(time.monotonic() < deadline, "uvicorn did not answer within 20 s")
                    time.sleep(0.1)
            statuses = [client.get(path).status_code for path in paths]
    finally:
        process.terminate()
        output, _ = process.communicate(timeout=10)
    return statuses, output


def test_uvicorn_run_as_the_entrypoint_runs_it_writes_no_private_content() -> None:
    """End to end: a real uvicorn process, configured in the entrypoint's order (JSON logging
    first, then `uvicorn.run(..., log_config=None)`), serving an invitation link, a sign-in
    redirect carrying one, and two requests that raise with an invitee's address in the message:
    on a private path the message is withheld, on a public one only the address goes."""
    script = (
        "import sys\n"
        "from infra.logging_config import configure_logging\n"
        "configure_logging(level='INFO', stream=sys.stdout)\n"
        "import uvicorn\n"
        f"EMAIL = {SENTINEL_EMAIL!r}\n"
        "async def app(scope, receive, send):\n"
        "    if scope['path'] in ('/my/boom', '/boom'):\n"
        "        raise ValueError('no verified user for ' + EMAIL)\n"
        "    await send({{'type': 'http.response.start', 'status': 200, 'headers': []}})\n"
        "    await send({{'type': 'http.response.body', 'body': b'ok'}})\n"
        "uvicorn.run(app, host='127.0.0.1', port={port}, log_config=None, lifespan='off')\n"
    )
    statuses, output = _serve(script, [p for p, _ in INVITATION_PATHS[:3]] + ["/my/boom", "/boom"])
    expect(statuses == [200, 200, 200, 500, 500], statuses)
    for sentinel in SENTINELS:
        expect(sentinel not in output, f"{sentinel!r} reached uvicorn's log:\n{output}")
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    access = [r["message"] for r in records if r["logger"] == "uvicorn.access"]
    for (_, written), line in zip(INVITATION_PATHS[:3], access[1:4], strict=True):
        expect(f'"GET {written} HTTP/1.1" 200' in line, f"access line changed: {line}")
    expect(any('"GET /my/boom HTTP/1.1" 500' in line for line in access), access)
    errors = [r["exc_info"] for r in records if r["logger"] == "uvicorn.error" and "exc_info" in r]
    expect(len(errors) == 2, errors)
    expect(errors[0].endswith("ValueError: REDACTED (message withheld)"), errors[0])
    expect(errors[1].endswith("ValueError: no verified user for REDACTED"), errors[1])


#: A P2-shaped route module (docs/19 §6.1): its endpoints return what fails their response model,
#: once with a title over the model's limit and once with a required field missing.
PRIVATE_ROUTES = f"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()


class ProjectOut(BaseModel):
    public_id: str
    title: str = Field(max_length=10)
    stage: str


@router.get("/v1/my/projects/{{project_id}}")
def get_project(project_id: str) -> ProjectOut:
    return {{"public_id": project_id, "title": {SENTINEL!r}, "stage": "early"}}  # type: ignore[return-value]


@router.get("/v1/my/projects/{{project_id}}/pin")
def get_pin(project_id: str) -> ProjectOut:
    return {{"public_id": project_id, "lat": 51.501, "note": {SENTINEL!r}}}  # type: ignore[return-value]
"""


def test_a_response_that_fails_its_model_on_a_private_route_is_withheld_end_to_end(
    tmp_path: pathlib.Path,
) -> None:
    """The case no private frame covers: FastAPI validates the response after the endpoint has
    returned, and Starlette re-raises the error to uvicorn after the API's own handler
    (services/api/request_context.py) has logged it. Real FastAPI, the API's middleware and
    handler, the routes in a module named and filed as services.api.my_projects, real uvicorn."""
    module = tmp_path / "services" / "api" / "my_projects.py"
    module.parent.mkdir(parents=True)
    module.write_text(PRIVATE_ROUTES)
    script = (
        "import importlib.util, sys\n"
        "from infra.logging_config import configure_logging\n"
        "configure_logging(level='INFO', stream=sys.stdout)\n"
        "import uvicorn\n"
        "from fastapi import FastAPI\n"
        "from services.api.request_context import RequestContextMiddleware, unhandled_exception_handler\n"
        f"spec = importlib.util.spec_from_file_location('services.api.my_projects', {str(module)!r})\n"
        "routes = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(routes)\n"
        "app = FastAPI()\n"
        "app.include_router(routes.router)\n"
        "app.add_exception_handler(Exception, unhandled_exception_handler)\n"
        "app.add_middleware(RequestContextMiddleware)\n"
        "uvicorn.run(app, host='127.0.0.1', port={port}, log_config=None, lifespan='off')\n"
    )
    statuses, output = _serve(script, [f"/v1/my/projects/{PROJECT_ID}", f"/v1/my/projects/{PROJECT_ID}/pin"])
    expect(statuses == [500, 500], f"{statuses}\n{output}")
    expect(SENTINEL not in output and "51.501" not in output, f"private content reached the log:\n{output}")
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    errors = [(r["logger"], r["exc_info"].splitlines()[-1]) for r in records if "exc_info" in r]
    withheld = "fastapi.exceptions.ResponseValidationError: REDACTED (message withheld; {})"
    first = withheld.format(
        "1 error: string_too_long; endpoint GET /v1/my/projects/{project_id}; in get_project"
    )
    second = withheld.format("2 errors: missing; endpoint GET /v1/my/projects/{project_id}/pin; in get_pin")
    expect(
        errors
        == [
            ("services.api.request_context", first),
            ("uvicorn.error", first),
            ("services.api.request_context", second),
            ("uvicorn.error", second),
        ],
        errors,
    )
