"""Tests for infra/observability.py. Bare `assert` is S101 under this repo's ruff config with no
per-file-ignore for infra/ (see infra/scheduler/test_jobs.py), so checks go through `expect`."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import types
from typing import Any

import pytest

from infra import observability
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


def test_error_reports_never_carry_frame_locals_or_request_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame's locals can hold a password, an invitation token or a private note, and a request body
    can be a sign-in form; `send_default_pii=False` alone still sends both (docs/19 §5)."""
    monkeypatch.setenv("SENTRY_DSN", FAKE_DSN)
    fake, calls = fake_sentry_module()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    init_error_tracking("api")
    kwargs = calls["init"][0]
    expect(kwargs.get("include_local_variables") is False, "stack-frame locals must never be sent")
    expect(kwargs.get("max_request_body_size") == "never", "request bodies must never be sent")


def test_the_privacy_options_are_ones_the_pinned_sdk_accepts() -> None:
    """The options above are real constructor parameters of the pinned sentry-sdk, not typos the SDK
    would silently ignore."""
    import inspect

    consts = pytest.importorskip("sentry_sdk.consts")
    params = inspect.signature(consts.ClientConstructor.__init__).parameters
    for name in (
        "include_local_variables",
        "max_request_body_size",
        "send_default_pii",
        "before_send",
        "before_send_transaction",
        "before_breadcrumb",
    ):
        expect(name in params, f"sentry-sdk no longer accepts {name}")


def test_missing_sdk_with_dsn_is_a_warning_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", FAKE_DSN)
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # makes `import sentry_sdk` raise ImportError
    expect(init_error_tracking("scheduler") is False, "missing SDK must degrade to inactive")


# ------------------------------------------------------------- private requests (docs/19 §5.3, lane P2)
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SENTINEL = "ZQX-private-sentinel-7741"
PRIVATE_ID = "prj_0123456789ABCDEFGHJKMNPQRS"

#: Runs the real pinned sentry-sdk in a fresh interpreter, so its integrations' global patches never
#: reach this process: `init_error_tracking` as production calls it, with a transport that keeps the
#: events instead of sending them. The sentinel comes from the environment, so no source line (which
#: an event's frame context carries) holds it.
PROBE = r"""
import json, logging, os, sys
import sentry_sdk
from sentry_sdk.transport import Transport

events = []


TRACES = os.environ.get("PROBE_TRACES") == "1"
KINDS = ("event", "transaction") if TRACES else ("event",)


class Keep(Transport):
    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.headers.get("type") in KINDS:
                events.append(item.payload.json)


real_init = sentry_sdk.init


def init(**kwargs):
    kwargs["transport"] = Keep(kwargs)
    if TRACES:  # tracing is off in production (O-8); this run shows what it would send if it were on
        kwargs["traces_sample_rate"] = 1.0
    return real_init(**kwargs)


sentry_sdk.init = init
from infra.observability import init_error_tracking

assert init_error_tracking("api")
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

SECRET = os.environ["PROBE_SENTINEL"]
PRIVATE_ID = os.environ["PROBE_PRIVATE_ID"]
app = FastAPI()


@app.post("/v1/my/projects/{project_id}/notes")
async def private_route(project_id: str, request: Request):
    note_text = (await request.json())["body"]
    logging.getLogger("services.api.my_projects").warning("note %s", note_text)
    raise RuntimeError(note_text)


@app.get("/v1/proposals")
def public_route():
    logging.getLogger("services.api.records").warning("public breadcrumb")
    raise ValueError("lookup failed for ana@example.org")


client = TestClient(app, raise_server_exceptions=False)
client.post(
    f"/v1/my/projects/{PRIVATE_ID}/notes?q={SECRET}",
    json={"body": SECRET},
    headers={"Cookie": f"session={SECRET}", "X-Trace-Note": SECRET, "User-Agent": SECRET},
)
client.get(
    "/v1/proposals?q=solar&next=/my/invitations/accept%3Ft%3DAbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdEFG",
    headers={"Referer": f"https://inframation.io/my/projects/{PRIVATE_ID}"},
)
logging.getLogger("services.projects.authz").error("failed for %s", SECRET, extra={"title": SECRET})
sentry_sdk.flush()
print(json.dumps(events))
"""


def _run_probe(tmp_path_factory: pytest.TempPathFactory, *, traces: bool) -> list[dict[str, Any]]:
    pytest.importorskip("sentry_sdk")
    script = tmp_path_factory.mktemp("sentry_probe") / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith(("SENTRY_", "PROBE_"))},
        "PYTHONPATH": str(REPO_ROOT),
        "SENTRY_DSN": FAKE_DSN,
        "PROBE_SENTINEL": SENTINEL,
        "PROBE_PRIVATE_ID": PRIVATE_ID,
        **({"PROBE_TRACES": "1"} if traces else {}),
    }
    result = subprocess.run(  # noqa: S603 -- this interpreter, a script this test wrote
        [sys.executable, str(script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    expect(result.returncode == 0, result.stderr[-3000:])
    events: list[dict[str, Any]] = json.loads(result.stdout.strip().splitlines()[-1])
    return events


@pytest.fixture(scope="module")
def captured_events(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    return _run_probe(tmp_path_factory, traces=False)


@pytest.fixture(scope="module")
def traced_events(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    """The same probe with tracing on, keeping transactions as well as events."""
    return _run_probe(tmp_path_factory, traces=True)


def _by_kind(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for event in events:
        values = (event.get("exception") or {}).get("values") or []
        kind = values[-1]["type"] if values else f"log:{event.get('logger')}"
        out[kind] = event
    return out


def _crumbs(event: dict[str, Any]) -> list[dict[str, Any]]:
    crumbs: list[dict[str, Any]] = (event.get("breadcrumbs") or {}).get("values") or []
    return crumbs


def test_error_reports_drop_locals_and_bodies(captured_events: list[dict[str, Any]]) -> None:
    """End to end with the real SDK: a private request's report carries the method and an id-free
    path, and no query string, header, cookie, body, frame local or exception message."""
    text = json.dumps(captured_events)
    expect(SENTINEL not in text, "a private value reached an error report")
    expect(PRIVATE_ID not in text, "a private id reached an error report")
    event = _by_kind(captured_events)["RuntimeError"]
    expect(set(event["request"]) <= {"method", "url"}, f"request kept {sorted(event['request'])}")
    expect(event["request"]["url"].endswith("/v1/my/projects/{id}/notes"), event["request"]["url"])
    expect(event["transaction"] == "/v1/my/projects/{project_id}/notes", event.get("transaction"))
    exception = event["exception"]["values"][-1]
    expect(exception["value"] == observability.WITHHELD, "the exception message must be withheld")
    frames = exception["stacktrace"]["frames"]
    expect(bool(frames) and all("vars" not in frame for frame in frames), "frames kept, locals never")
    bare = {"type", "category", "level", "timestamp"}
    expect(all(set(crumb) <= bare for crumb in _crumbs(event)), _crumbs(event))
    private_crumbs = [c for c in _crumbs(event) if c.get("category") == "services.api.my_projects"]
    expect(not private_crumbs, "a private log line became a breadcrumb")
    expect("extra" not in event and "user" not in event, "extras and user are withheld")


def test_a_private_modules_log_event_is_withheld(captured_events: list[dict[str, Any]]) -> None:
    event = _by_kind(captured_events)["log:services.projects.authz"]
    expect(event["logentry"] == {"message": observability.WITHHELD}, event.get("logentry"))
    expect("extra" not in event, "a private record's extras are withheld")


def test_a_public_report_keeps_its_shape_without_tokens_addresses_or_private_referers(
    captured_events: list[dict[str, Any]],
) -> None:
    event = _by_kind(captured_events)["ValueError"]
    message = event["exception"]["values"][-1]["value"]
    expect(message.startswith("lookup failed for") and "ana@example.org" not in message, message)
    query = event["request"]["query_string"]
    expect("q=solar" in query and "AbCdEfGhIjKlMnOp" not in query, query)
    headers = {name.lower(): value for name, value in event["request"]["headers"].items()}
    expect(headers["referer"] == observability.WITHHELD, headers)
    messages = [crumb.get("message") for crumb in _crumbs(event)]
    expect("public breadcrumb" in messages, messages)


def test_init_installs_the_scrubbing_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", FAKE_DSN)
    fake, calls = fake_sentry_module()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    init_error_tracking("api")
    kwargs = calls["init"][0]
    expect(kwargs["before_send"] is observability.before_send, "before_send must be installed")
    expect(
        kwargs["before_send_transaction"] is observability.before_send,
        "a transaction skips before_send, so the same scrubber is installed for it",
    )
    expect(kwargs["before_breadcrumb"] is observability.before_breadcrumb, "before_breadcrumb too")


def test_with_tracing_on_no_private_value_leaves_in_a_transaction(
    traced_events: list[dict[str, Any]],
) -> None:
    """Tracing is off (`traces_sample_rate=0.0`), but if it is turned on: with the real SDK a
    transaction skips `before_send`. Before 2026-10-23 a private request's transaction carried its
    full URL with the project id, its query string and its headers, and a private error's trace
    context its whole span description and data (lane P2 security review)."""
    text = json.dumps(traced_events)
    expect(SENTINEL not in text, "a private value reached a transaction or event")
    expect(PRIVATE_ID not in text, "a private id reached a transaction or event")
    private = [
        event
        for event in traced_events
        if event.get("type") == "transaction"
        and event.get("transaction") == "/v1/my/projects/{project_id}/notes"
    ]
    expect(len(private) == 1, [event.get("transaction") for event in traced_events])
    (transaction,) = private
    expect(set(transaction["request"]) <= {"method", "url"}, f"request kept {sorted(transaction['request'])}")
    for event in [transaction, _by_kind(traced_events)["RuntimeError"]]:
        trace = event["contexts"]["trace"]
        expect(set(trace) <= {"trace_id", "span_id"} and trace.get("trace_id"), trace)
    kept_span_keys = set(observability._KEPT_SPAN_KEYS)
    expect(
        all(set(span) <= kept_span_keys for span in transaction.get("spans") or []), transaction.get("spans")
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/v1/my/projects/{PRIVATE_ID}/notes/nte_0123456789ABCD", "/v1/my/projects/{id}/notes/{id}"),
        ("/my/projects/prj_0123456789/checklist", "/my/projects/{id}/checklist"),
        ("/api/my/pins", "/api/my/pins"),
        ("/v1/my/projects/a-title-typed-into-the-url", "/v1/my/projects/{id}"),
        ("/v1/my/projects/{project_id}", "/v1/my/projects/{id}"),
    ],
)
def test_private_paths_lose_every_segment_that_is_not_a_route_word(path: str, expected: str) -> None:
    scrubbed = observability.scrub_private_path(path)
    expect(scrubbed == expected, scrubbed)


def _event(url: str, *, module: str = "services.api.records", message: str = SENTINEL) -> dict[str, Any]:
    return {
        "request": {
            "method": "GET",
            "url": url,
            "query_string": f"q={SENTINEL}",
            "headers": {"X-Note": SENTINEL},
            "cookies": {"session": SENTINEL},
            "data": {"body": SENTINEL},
            "env": {"REMOTE_ADDR": "203.0.113.9"},
        },
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": message,
                    "stacktrace": {"frames": [{"module": module, "function": "f", "vars": {"x": SENTINEL}}]},
                }
            ]
        },
        "extra": {"title": SENTINEL},
        "breadcrumbs": {"values": [{"category": "x", "message": SENTINEL, "data": {"v": SENTINEL}}]},
    }


@pytest.mark.parametrize("url", ["https://inframation.io/my/projects/prj_0123456789", "/V1/MY/pins?x=1"])
def test_a_private_path_is_private_whatever_its_form(url: str) -> None:
    scrubbed = observability.before_send(_event(url))
    expect(scrubbed is not None and SENTINEL not in json.dumps(scrubbed), scrubbed)


def test_an_exception_raised_through_private_code_is_private_on_any_path() -> None:
    """Erasure, retention or a private helper failing under a public path: the frame decides."""
    event = _event("https://api.inframation.io/v1/me", module="services.projects.authz")
    scrubbed = observability.before_send(event)
    expect(scrubbed is not None and SENTINEL not in json.dumps(scrubbed), scrubbed)
    expect(event["exception"]["values"][0]["value"] == observability.WITHHELD, event)


def test_a_public_event_keeps_its_message_and_loses_frame_locals() -> None:
    event = _event("https://api.inframation.io/v1/proposals", message="no such row")
    expect(observability.before_send(event) is not None, "a public event is kept")
    value = event["exception"]["values"][0]
    expect(value["value"] == "no such row" and "vars" not in value["stacktrace"]["frames"][0], value)


def test_an_event_that_cannot_be_scrubbed_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(event: dict[str, Any]) -> None:
        raise RuntimeError("scrubber failed")

    monkeypatch.setattr(observability, "_scrub_public", broken)
    event = _event("https://api.inframation.io/v1/proposals")
    expect(observability.before_send(event) is None, "an event that cannot be scrubbed is dropped")


def test_a_private_modules_breadcrumbs_are_never_recorded() -> None:
    dropped = observability.before_breadcrumb({"category": "services.api.my_projects", "message": "x"})
    expect(dropped is None, dropped)
    crumb = observability.before_breadcrumb(
        {"category": "httplib", "message": f"GET /v1/my/projects/{PRIVATE_ID}"}
    )
    expect(crumb is not None and "message" not in crumb, crumb)
    kept = observability.before_breadcrumb({"category": "httplib", "message": "GET /v1/proposals"})
    expect(kept == {"category": "httplib", "message": "GET /v1/proposals"}, kept)


def test_a_private_events_trace_context_and_spans_keep_only_ids_and_timing() -> None:
    url = f"https://api.inframation.io/v1/my/projects/{PRIVATE_ID}/notes?q={SENTINEL}"
    trace = {
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "op": "http.server",
        "description": f"GET {url}",
        "data": {"url": url},
    }
    span = {"span_id": "c" * 16, "op": "db", "description": f"SELECT '{SENTINEL}'", "data": {"q": SENTINEL}}
    event = {
        **_event(url),
        "contexts": {"trace": dict(trace), "runtime": {"name": "CPython"}},
        "spans": [span],
    }
    scrubbed = observability.before_send(event)
    expect(scrubbed is not None and SENTINEL not in json.dumps(scrubbed), scrubbed)
    assert scrubbed is not None
    expect(scrubbed["contexts"]["trace"] == {"trace_id": "a" * 32, "span_id": "b" * 16}, scrubbed["contexts"])
    expect(scrubbed["contexts"]["runtime"] == {"name": "CPython"}, "process contexts stay")
    expect(scrubbed["spans"] == [{"span_id": "c" * 16, "op": "db"}], scrubbed["spans"])
    public = observability.before_send(
        {"request": {"url": "https://api.inframation.io/v1/proposals"}, "contexts": {"trace": dict(trace)}}
    )
    expect(public is not None and public["contexts"]["trace"] == trace, "a public event's trace is untouched")
