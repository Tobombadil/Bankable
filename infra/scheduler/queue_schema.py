"""Install or upgrade Procrastinate's job-queue schema, idempotently (ADR 0004; docs/60 §10.1).

    python -m infra.scheduler.queue_schema ensure   # infra/scripts/deploy.sh step 4, after Alembic
    python -m infra.scheduler.queue_schema status   # read-only: what `ensure` would do, as JSON

Why this exists: nothing applied Procrastinate's schema, so on the first real stack run (2026-09-26,
docs/60 §11 item 9) every worker crash-looped on `function procrastinate_prune_stalled_workers_v1
(double precision) does not exist`. `procrastinate schema --apply` is not idempotent (a second run
fails with `type "procrastinate_job_status" already exists`) and Procrastinate records no version:
"It's your responsibility to keep track of which migrations have been applied yet or not"
(procrastinate.readthedocs.io, howto/production/migrations). This module is that bookkeeping.

Why a guarded deploy step and not an Alembic revision: the queue schema's lifecycle follows the
Procrastinate pin (requirements.txt), not the domain schema. An Alembic revision that ran today's
`schema.sql` would install whichever Procrastinate version the image happened to carry, and the
revision after a pin bump could not know which one a given database got; one that vendored 3.9.0's
SQL would have to be followed by a hand-written revision per upgrade. Here the version the image
carries is compared with the one recorded in the database, and Procrastinate's own shipped files
are applied: the fresh `schema.sql` when nothing is there, the numbered migrations in between on an
upgrade. It also keeps every Postgres-only statement out of the Alembic chain, which the test
suites run on SQLite.

What `ensure` does, inside one transaction under a transaction-scoped advisory lock (so two
concurrent deploys serialise rather than race):

  * no `procrastinate_jobs` table: apply the installed version's `schema.sql`, record the version;
  * recorded version == installed: nothing;
  * recorded version < installed: apply every migration file whose version is in
    (recorded, installed], in file-name order, which puts each version's `pre` files before its
    `post` files; record the new version. This is the upgrade path Procrastinate documents "with
    service interruption" (stop everything that defers or runs jobs, apply pre and post, upgrade
    code, restart), and deploy.sh has already stopped the workers and the scheduler (step 3). The
    api can still defer a manual "run now" job (services/api/admin_sources.py) until step 5
    replaces it; that request fails with a 503 at worst, it cannot corrupt the queue;
  * recorded version > installed (a rollback to an older image): nothing, with a warning. The
    newer schema stays; Procrastinate's `pre` migrations are written to be compatible with the
    previous release, but a `post` one may not be, which is why the warning names both versions;
  * tables present, no record (the one-off `procrastinate schema --apply` docs/40 §3 step 7 told
    the owner to run before this existed, or lane E1's run): if the database's `procrastinate_*`
    functions are exactly the set the installed `schema.sql` creates, adopt it as that version;
    otherwise refuse (exit 1) and name the difference, because guessing a version and applying
    migrations on top of it is how a queue schema gets half-upgraded.

Exit codes: 0 ok (including no-op), 1 refused or failed. Postgres only: the `DATABASE_URL` is the
one every service reads (`infra.scheduler.app._database_url` strips SQLAlchemy's dialect suffix).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import procrastinate
from procrastinate.schema import SchemaManager

#: Our record of what is installed; Procrastinate keeps none. One row per applied change.
VERSION_TABLE = "infraque_queue_schema_version"
#: `pg_advisory_xact_lock` key: any fixed 64-bit value no other code in this database uses.
ADVISORY_LOCK_KEY = 0x1F0E_0A5C_4E0E_0001
#: The table whose presence means "a queue schema is installed" (also what `/v1/health` checks).
SENTINEL_TABLE = "procrastinate_jobs"

_MIGRATION_NAME = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})_(\d{2})_[a-z0-9_]+\.sql$")
_CREATED_ROUTINE = re.compile(
    r"^CREATE (?:OR REPLACE )?(?:FUNCTION|PROCEDURE) (procrastinate_\w+)", re.IGNORECASE | re.MULTILINE
)

Version = tuple[int, int, int]


def parse_version(text: str) -> Version:
    """`"3.9.0"` -> `(3, 9, 0)`. Pre-release/local suffixes are not expected on a pinned release
    and are refused rather than silently truncated."""
    parts = text.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a plain X.Y.Z version: {text!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def format_version(version: Version) -> str:
    return ".".join(str(n) for n in version)


@dataclasses.dataclass(frozen=True)
class Migration:
    version: Version
    serial: int
    path: Path

    @property
    def name(self) -> str:
        return self.path.name


def migration_files(directory: Path) -> list[Migration]:
    """Every `XX.YY.ZZ_NN_<pre|post>_*.sql` file Procrastinate ships, in apply order. A file whose
    name does not match the documented pattern is an error, not something to skip quietly."""
    found = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise ValueError(f"unexpected Procrastinate migration file name: {path.name}")
        major, minor, patch, serial = (int(g) for g in match.groups())
        found.append(Migration((major, minor, patch), serial, path))
    return sorted(found, key=lambda m: (m.version, m.serial, m.name))


def pending_migrations(files: Iterable[Migration], recorded: Version, installed: Version) -> list[Migration]:
    """The files that move a database from `recorded` to `installed`: version in (recorded,
    installed]. A migration named for version V brings the schema to V's `schema.sql`."""
    return [m for m in files if recorded < m.version <= installed]


def expected_routines(schema_sql: str) -> set[str]:
    """The `procrastinate_*` functions/procedures a fresh `schema.sql` creates: the fingerprint
    used to adopt an unrecorded install."""
    return set(_CREATED_ROUTINE.findall(schema_sql))


@dataclasses.dataclass(frozen=True)
class State:
    tables_present: bool
    recorded: Version | None
    routines: frozenset[str]


@dataclasses.dataclass(frozen=True)
class Plan:
    action: str  # install | noop | upgrade | adopt | ahead | refuse
    target: Version
    migrations: tuple[Migration, ...] = ()
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.action != "refuse"


def plan(state: State, *, installed: Version, schema_sql: str, files: Sequence[Migration]) -> Plan:
    """Decide what `ensure` does. Pure: the database state and the package contents come in as
    arguments, so every branch is unit-tested without Postgres (infra/scheduler/test_queue_schema.py)."""
    if not state.tables_present:
        return Plan("install", installed, detail="no queue schema present; applying schema.sql")
    if state.recorded is None:
        expected = expected_routines(schema_sql)
        if set(state.routines) == expected:
            return Plan("adopt", installed, detail="unrecorded install matches this version's schema.sql")
        missing = sorted(expected - set(state.routines))
        extra = sorted(set(state.routines) - expected)
        return Plan(
            "refuse",
            installed,
            detail=(
                "queue tables exist but no version is recorded, and their functions do not match "
                f"Procrastinate {format_version(installed)}'s schema.sql (missing: {missing or 'none'}; "
                f"extra: {extra or 'none'}). Find the version that created them, apply the migrations "
                "from `procrastinate schema --migrations-path` by hand, then insert that version into "
                f"{VERSION_TABLE}."
            ),
        )
    if state.recorded == installed:
        return Plan("noop", installed, detail="queue schema already at this version")
    if state.recorded > installed:
        return Plan(
            "ahead",
            state.recorded,
            detail=(
                f"database records Procrastinate {format_version(state.recorded)}, this image carries "
                f"{format_version(installed)} (a rollback?); leaving the newer schema in place"
            ),
        )
    pending = tuple(pending_migrations(files, state.recorded, installed))
    return Plan(
        "upgrade",
        installed,
        migrations=pending,
        detail=f"{len(pending)} migration file(s) from {format_version(state.recorded)}",
    )


# ------------------------------------------------------------------------------ database side
def _connect() -> Any:
    import psycopg

    from infra.scheduler.app import _database_url

    return psycopg.connect(_database_url())


def read_state(cur: Any) -> State:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (SENTINEL_TABLE,))
    tables_present = bool(cur.fetchone()[0])
    cur.execute(
        f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} ("
        " id bigserial PRIMARY KEY,"
        " version text NOT NULL,"
        " action text NOT NULL,"
        " applied_at timestamptz NOT NULL DEFAULT now())"
    )
    cur.execute(f"SELECT version FROM {VERSION_TABLE} ORDER BY id DESC LIMIT 1")  # noqa: S608 -- constant
    row = cur.fetchone()
    cur.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = current_schema() AND p.proname LIKE 'procrastinate\\_%'"
    )
    routines = frozenset(str(r[0]) for r in cur.fetchall())
    return State(tables_present, parse_version(row[0]) if row else None, routines)


def apply(cur: Any, decided: Plan, *, schema_sql: str) -> None:
    # No parameters are passed, so psycopg sends each script as-is (multiple statements allowed,
    # `%` not interpreted) -- the same way Procrastinate's own `schema --apply` runs it.
    if decided.action == "install":
        cur.execute(schema_sql)
    elif decided.action == "upgrade":
        for migration in decided.migrations:
            cur.execute(migration.path.read_text(encoding="utf-8"))
    if decided.action in ("install", "adopt", "upgrade"):
        cur.execute(
            f"INSERT INTO {VERSION_TABLE} (version, action) VALUES (%s, %s)",  # noqa: S608 -- constant
            (format_version(decided.target), decided.action),
        )


def _report(decided: Plan, installed: Version, state: State | None = None) -> dict[str, Any]:
    return {
        "event": "queue_schema",
        "action": decided.action,
        "installed_package": format_version(installed),
        "recorded": format_version(state.recorded) if state and state.recorded else None,
        "target": format_version(decided.target),
        "migrations": [m.name for m in decided.migrations],
        "detail": decided.detail,
    }


def run(command: str, *, connect: Callable[[], Any] = _connect) -> int:
    """`connect` is injectable so the Postgres test can point `ensure` at a throwaway schema."""
    installed = parse_version(procrastinate.__version__)
    schema_sql = SchemaManager.get_schema()
    files = migration_files(Path(SchemaManager.get_migrations_path()))
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        state = read_state(cur)
        decided = plan(state, installed=installed, schema_sql=schema_sql, files=files)
        if command == "ensure" and decided.ok:
            apply(cur, decided, schema_sql=schema_sql)
            after = read_state(cur)
            if not after.tables_present:  # belt and braces: never report success without the tables
                raise RuntimeError(f"{SENTINEL_TABLE} still absent after {decided.action}")
        else:
            conn.rollback()  # `status` and a refusal change nothing, not even the bookkeeping table
    sys.stdout.write(json.dumps(_report(decided, installed, state)) + "\n")
    if not decided.ok:
        sys.stderr.write(decided.detail + "\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m infra.scheduler.queue_schema", description=__doc__)
    parser.add_argument("command", choices=("ensure", "status"))
    args = parser.parse_args(argv)
    return run(args.command)


if __name__ == "__main__":
    sys.exit(main())
