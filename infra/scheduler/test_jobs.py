"""Unit tests for `infra/scheduler/jobs.py` and the `alert_tick`/`post_draft_tick` registrations
in `infra/scheduler/app.py` (docs/00-PLAN.md Sprint 3 item 4).

No Postgres and no `services.alerts`/`services.social` import: those two modules are being written
concurrently by other agents and may not exist yet, so every job test injects a fake `_run` that
matches the documented contract instead — a frozen dataclass report with counts and an
`errors: tuple[str, ...]` field.

Bare `assert` is flagged by ruff's S101 here (this file has no per-file-ignore in `pyproject.toml`,
and the Makefile's `--extend-per-file-ignores` only names `test_cadence.py`; extending either is
out of this task's write scope) — every check below is an explicit `if ...: raise
AssertionError(...)` instead, per the "lint-clean as is" option in the brief.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

import pytest
import yaml
from sqlalchemy import text

from infra.scheduler import jobs

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _ComposeSafeLoader(yaml.SafeLoader):  # type: ignore[misc]  # PyYAML ships no type stubs here
    """`compose.prod.yml` uses Compose's own merge tags (`!reset`, `!override`) on top of plain
    YAML, which plain `yaml.safe_load` cannot parse (no registered constructor). Docker Compose
    itself resolves these when merging with the base file; since only the CLI-level `docker
    compose config` check does that merge (unavailable in this sandbox per the task's own Tooling
    note), this loader just resolves each tagged node to its plain value — enough to confirm the
    file parses as YAML and to inspect its own services/keys in isolation, not to reproduce the
    merge Compose would perform."""


def _compose_tag(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeSafeLoader.add_constructor("!reset", _compose_tag)
_ComposeSafeLoader.add_constructor("!override", _compose_tag)


@dataclass(frozen=True)
class _FakeReport:
    """Shaped like `AlertTickReport`/`DraftTickReport` per the contract: counts plus `errors`."""

    processed: int
    sent: int
    errors: tuple[str, ...] = ()


def test_alert_tick_job_logs_one_line_and_returns_the_report_as_a_dict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = _FakeReport(processed=3, sent=2, errors=("boom: user@example.com",))

    def fake_run(session_factory: Any) -> _FakeReport:
        if session_factory is None:
            raise AssertionError("alert_tick_job must pass a session factory through to _run")
        return report

    caplog.set_level("INFO", logger="infra.scheduler.jobs")
    result = jobs.alert_tick_job(_run=fake_run)

    if result != {"processed": 3, "sent": 2, "errors": ["boom: user@example.com"]}:
        raise AssertionError(result)

    lines = [record.getMessage() for record in caplog.records]
    if len(lines) != 1:
        raise AssertionError(lines)
    line = lines[0]
    for expected in ("alert_tick", "processed=3", "sent=2", "error_count=1"):
        if expected not in line:
            raise AssertionError(f"{expected!r} missing from log line: {line!r}")
    if "boom" in line or "user@example.com" in line:
        raise AssertionError(f"raw error text must never be logged, found it in: {line!r}")


def test_post_draft_tick_job_logs_one_line_and_returns_the_report_as_a_dict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = _FakeReport(processed=5, sent=5, errors=())

    caplog.set_level("INFO", logger="infra.scheduler.jobs")
    result = jobs.post_draft_tick_job(_run=lambda session_factory: report)

    if result != {"processed": 5, "sent": 5, "errors": []}:
        raise AssertionError(result)
    lines = [record.getMessage() for record in caplog.records]
    if len(lines) != 1:
        raise AssertionError(lines)
    if "post_draft_tick" not in lines[0] or "error_count=0" not in lines[0]:
        raise AssertionError(lines[0])


def test_alert_tick_job_propagates_a_failing_run_so_procrastinate_sees_the_failure() -> None:
    def fake_run(session_factory: Any) -> _FakeReport:
        raise RuntimeError("db exploded")

    with pytest.raises(RuntimeError, match="db exploded"):
        jobs.alert_tick_job(_run=fake_run)


def test_post_draft_tick_job_propagates_a_failing_run_so_procrastinate_sees_the_failure() -> None:
    def fake_run(session_factory: Any) -> _FakeReport:
        raise RuntimeError("queue exploded")

    with pytest.raises(RuntimeError, match="queue exploded"):
        jobs.post_draft_tick_job(_run=fake_run)


def test_build_session_factory_is_cached() -> None:
    first = jobs.build_session_factory()
    second = jobs.build_session_factory()
    if first is not second:
        raise AssertionError("build_session_factory should be cached (functools.lru_cache)")


def test_build_session_factory_needs_no_real_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """`services.db.session.get_engine` falls back to an in-memory SQLite engine when
    `DATABASE_URL` is unset (its own docstring) — this is what lets a fake `_run` in the tests
    above skip a real database entirely, since `alert_tick_job`/`post_draft_tick_job` always call
    `build_session_factory()` regardless of whether `_run` is real or fake."""
    jobs.build_session_factory.cache_clear()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        sessionmaker_ = jobs.build_session_factory()
        session = sessionmaker_()
        try:
            value = session.execute(text("SELECT 1")).scalar()
            if value != 1:
                raise AssertionError(value)
        finally:
            session.close()
    finally:
        jobs.build_session_factory.cache_clear()


def test_periodic_tasks_are_registered_with_expected_cron_queue_and_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `procrastinate.PsycopgConnector.__init__` only stores its pool factory/kwargs — it does not
    # build or open a `psycopg_pool` connection pool at construction time (verified by reading the
    # source: `self._async_pool` stays `None`; the pool is created lazily when the app is opened).
    # So importing `infra.scheduler.app` with a dummy `DATABASE_URL` is safe here and needs no
    # App-instance helper/workaround — module-level `app = procrastinate.App(...)` never touches
    # the network.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")
    import infra.scheduler.app as scheduler_app

    registry = scheduler_app.app.periodic_registry.periodic_tasks

    alert = registry.get(("tick_alert", "tick:alert"))
    if alert is None:
        raise AssertionError(f"tick_alert periodic registration missing: {sorted(registry)}")
    if alert.cron != "*/15 * * * *":
        raise AssertionError(alert.cron)
    if alert.task.queue != scheduler_app.SCHEDULER_ONLY_QUEUE:
        raise AssertionError(alert.task.queue)

    post_draft = registry.get(("tick_post_draft", "tick:post_draft"))
    if post_draft is None:
        raise AssertionError(f"tick_post_draft periodic registration missing: {sorted(registry)}")
    if post_draft.cron != "7 * * * *":
        raise AssertionError(post_draft.cron)
    if post_draft.task.queue != scheduler_app.SCHEDULER_ONLY_QUEUE:
        raise AssertionError(post_draft.task.queue)

    alert_tick_task = scheduler_app.app.tasks["alert_tick"]
    if alert_tick_task.queue != "alert":
        raise AssertionError(alert_tick_task.queue)
    if alert_tick_task.queueing_lock != "alert_tick":
        raise AssertionError(alert_tick_task.queueing_lock)
    if alert_tick_task.retry_strategy is not None:
        raise AssertionError(alert_tick_task.retry_strategy)

    post_draft_tick_task = scheduler_app.app.tasks["post_draft_tick"]
    if post_draft_tick_task.queue != "post_draft":
        raise AssertionError(post_draft_tick_task.queue)
    if post_draft_tick_task.queueing_lock != "post_draft_tick":
        raise AssertionError(post_draft_tick_task.queueing_lock)
    if post_draft_tick_task.retry_strategy is not None:
        raise AssertionError(post_draft_tick_task.retry_strategy)


def test_compose_files_parse_and_name_the_expected_queues() -> None:
    base = yaml.safe_load((ROOT / "infra/compose/docker-compose.yml").read_text())
    # _ComposeSafeLoader only subclasses SafeLoader with two extra constructors that build plain
    # lists/dicts/scalars (no `!!python/object` or similar) -- exactly as safe as yaml.safe_load,
    # ruff just cannot see that statically.
    prod_text = (ROOT / "infra/compose/compose.prod.yml").read_text()
    prod = yaml.load(prod_text, Loader=_ComposeSafeLoader)  # noqa: S506

    if "social" in base["services"]:
        raise AssertionError("expected the placeholder `social` service folded into `worker`")
    if "social" in prod.get("services", {}):
        raise AssertionError("compose.prod.yml still overrides a removed `social` service")

    worker_command = " ".join(base["services"]["worker"]["command"])
    for queue in ("alert", "post_draft", "publish_post"):
        if queue not in worker_command:
            raise AssertionError(f"{queue!r} missing from the worker service command: {worker_command!r}")
