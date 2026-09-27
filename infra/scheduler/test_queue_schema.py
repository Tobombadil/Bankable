"""`infra/scheduler/queue_schema.py`: the pure planner for every branch (no database), and, when
`POSTGIS_TEST_URL` names a reachable Postgres (the CI service; skipped otherwise), `ensure` run for
real inside a throwaway Postgres schema: install, re-run as a no-op, adopt an unrecorded install,
refuse a mismatched one."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import procrastinate
import pytest
from procrastinate.schema import SchemaManager

from infra.scheduler import queue_schema as qs

INSTALLED = qs.parse_version(procrastinate.__version__)
SCHEMA_SQL = SchemaManager.get_schema()
FILES = qs.migration_files(Path(SchemaManager.get_migrations_path()))


def _state(*, tables: bool, recorded: qs.Version | None = None, routines: set[str] | None = None) -> qs.State:
    return qs.State(tables, recorded, frozenset(routines or ()))


def test_versions_parse_and_refuse_suffixes() -> None:
    assert qs.parse_version("3.9.0") == (3, 9, 0)
    assert qs.format_version((3, 9, 0)) == "3.9.0"
    for bad in ("3.9", "3.9.0rc1", "v3.9.0"):
        with pytest.raises(ValueError, match=r"X\.Y\.Z"):
            qs.parse_version(bad)


def test_the_pinned_package_ships_parseable_ordered_migrations() -> None:
    """Every shipped file matches Procrastinate's documented `XX.YY.ZZ_NN_*` naming, and within a
    version the `pre` files sort before the `post` ones (serial 01 < 50)."""
    assert FILES, "the pinned Procrastinate ships migration files"
    keys = [(m.version, m.serial) for m in FILES]
    assert keys == sorted(keys)
    for version in {m.version for m in FILES}:
        names = [m.name for m in FILES if m.version == version]
        pre = [i for i, n in enumerate(names) if "_pre_" in n]
        post = [i for i, n in enumerate(names) if "_post_" in n]
        assert not pre or not post or max(pre) < min(post), names


def test_an_unexpected_file_name_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "03.00.00_01_ok_name.sql").write_text("")
    (tmp_path / "notes.sql").write_text("")
    with pytest.raises(ValueError, match=r"notes\.sql"):
        qs.migration_files(tmp_path)


def test_pending_is_the_half_open_interval() -> None:
    picked = qs.pending_migrations(FILES, (3, 0, 0), (3, 4, 0))
    assert picked
    assert all((3, 0, 0) < m.version <= (3, 4, 0) for m in picked)
    assert not qs.pending_migrations(FILES, INSTALLED, INSTALLED)


def test_fresh_database_installs_schema_sql() -> None:
    decided = qs.plan(_state(tables=False), installed=INSTALLED, schema_sql=SCHEMA_SQL, files=FILES)
    assert decided.action == "install"
    assert decided.target == INSTALLED
    assert decided.ok


def test_same_recorded_version_is_a_no_op() -> None:
    decided = qs.plan(
        _state(tables=True, recorded=INSTALLED), installed=INSTALLED, schema_sql=SCHEMA_SQL, files=FILES
    )
    assert decided.action == "noop"
    assert decided.migrations == ()


def test_older_recorded_version_upgrades_through_the_shipped_migrations() -> None:
    decided = qs.plan(
        _state(tables=True, recorded=(3, 0, 0)), installed=(3, 4, 0), schema_sql=SCHEMA_SQL, files=FILES
    )
    assert decided.action == "upgrade"
    names = [m.name for m in decided.migrations]
    assert names == [m.name for m in FILES if (3, 0, 0) < m.version <= (3, 4, 0)]
    assert "03.00.00_01_pre_cancel_notification.sql" not in names  # already part of 3.0.0
    assert "03.04.00_50_post_add_retry_failed_job_procedure.sql" in names


def test_newer_recorded_version_is_left_alone_on_rollback() -> None:
    decided = qs.plan(
        _state(tables=True, recorded=(9, 0, 0)), installed=INSTALLED, schema_sql=SCHEMA_SQL, files=FILES
    )
    assert decided.action == "ahead"
    assert decided.ok
    assert "9.0.0" in decided.detail


def test_unrecorded_install_is_adopted_only_when_it_matches() -> None:
    expected = qs.expected_routines(SCHEMA_SQL)
    assert "procrastinate_prune_stalled_workers_v1" in expected  # the function E1's workers missed
    adopted = qs.plan(
        _state(tables=True, routines=expected), installed=INSTALLED, schema_sql=SCHEMA_SQL, files=FILES
    )
    assert adopted.action == "adopt"
    mismatched = qs.plan(
        _state(tables=True, routines=(expected - {"procrastinate_fetch_job_v2"}) | {"procrastinate_old_v0"}),
        installed=INSTALLED,
        schema_sql=SCHEMA_SQL,
        files=FILES,
    )
    assert mismatched.action == "refuse"
    assert not mismatched.ok
    assert "procrastinate_fetch_job_v2" in mismatched.detail
    assert "procrastinate_old_v0" in mismatched.detail


# ------------------------------------------------------------------ real Postgres, optional
POSTGRES_URL = os.environ.get("POSTGIS_TEST_URL")


@pytest.fixture()
def throwaway_schema() -> Iterator[Any]:
    if not POSTGRES_URL:
        pytest.skip("POSTGIS_TEST_URL not set")
    import psycopg

    conninfo = POSTGRES_URL.replace("postgresql+psycopg://", "postgresql://")
    name = f"qs_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(conninfo, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{name}"')

    def connect() -> Any:
        return psycopg.connect(conninfo, options=f"-c search_path={name}")

    try:
        yield connect
    finally:
        with psycopg.connect(conninfo, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{name}" CASCADE')


def _last_report(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out.strip().splitlines()
    report: dict[str, Any] = json.loads(out[-1])
    return report


def test_ensure_installs_then_is_idempotent_then_adopts(
    throwaway_schema: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    connect = throwaway_schema
    assert qs.run("status", connect=connect) == 0
    assert _last_report(capsys)["action"] == "install"
    assert qs.run("ensure", connect=connect) == 0
    assert _last_report(capsys)["action"] == "install"
    assert qs.run("ensure", connect=connect) == 0  # `procrastinate schema --apply` fails here
    assert _last_report(capsys)["action"] == "noop"
    with connect() as conn:  # forget the record: now it looks like a hand-applied schema
        conn.execute(f"DELETE FROM {qs.VERSION_TABLE}")  # noqa: S608 -- constant
    assert qs.run("ensure", connect=connect) == 0
    assert _last_report(capsys)["action"] == "adopt"
    with connect() as conn:
        conn.execute("DROP FUNCTION procrastinate_prune_stalled_workers_v1")
        conn.execute(f"DELETE FROM {qs.VERSION_TABLE}")  # noqa: S608 -- constant
    assert qs.run("ensure", connect=connect) == 1
    assert _last_report(capsys)["action"] == "refuse"
