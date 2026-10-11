"""Preflight: the read-only GO / NO-GO the operator runs on the beta host after seeding, before
inviting anyone (docs/64 §3, "After seeding, measure"; owner decision 2026-10-11).

    python -m infra.scheduler.bootstrap preflight [--json] [--only ID,...] [--budget 120]
        [--web-url http://web:8001] [--data-root PATH] [--handcheck-dir PATH]

On the single host it runs in the worker image, which has `DATABASE_URL`, the connector data volume,
the env file's settings and the stack's network (`web` resolves):

    cd /opt/infraque/compose && IMAGE_TAG=$(cat /opt/infraque/current-tag) \\
      INFRAQUE_ENV_FILE=/opt/infraque/secrets/.env docker compose -f docker-compose.yml \\
      -f compose.prod.yml -f compose.single.yml --env-file /opt/infraque/secrets/.env \\
      run --rm --no-deps -T worker python -m infra.scheduler.bootstrap preflight

One line per check, PASS, WARN or FAIL, with the figure behind it and, under a WARN or FAIL, what to
do. GO when nothing FAILs (exit 0), NO-GO otherwise (exit 1). `--json` prints the same report as one
JSON object.

The checks, by number and id, and what makes each FAIL or WARN:

 1 `database`          FAIL unreachable, or not at the migration head.
 2 `freshness`         FAIL a scheduled source never succeeded (in the run records, or its `source` row);
                       WARN stale (`freshness.assess`, run-record vintage rule included), or no `source` row.
 3 `held_runs`         FAIL a data-quality hold waits for release.
 4 `counts`            FAIL no live or no public proposal, or a loaded source with no live record; WARN a
                       figure moved more than 25 % (and 50 rows) since the previous preflight.
 5 `visibility_audit`  FAIL M-11 > 0 (today's nightly run, else run now); WARN served checks inconclusive.
 6 `sites`             FAIL an `oversize` site; WARN no site over a store with live proposals.
 7 `unlisted`          a measurement (docs/64 §3 item 1): records no register lists any more.
 8 `map_count_cost`    WARN `/` costs more than 150 ms over `/?unlisted=include`, or `web` is unreachable.
 9 `email`             FAIL no `RESEND_API_KEY`, or the sender unset or a placeholder.
10 `email_domain`      FAIL env `DOMAIN` differs from `services.api.common.DOMAIN`; WARN `DOMAIN` unset.
11 `monitoring`        FAIL the site is open, or a gate or CSP setting deploy.sh refuses; WARN no
                       `SENTRY_DSN`, or `CSP_MODE=enforce` before the docs/64 §5 checks.
12 `handcheck`         WARN no `<date>-beta` worksheet found.

**Read-only.** Every database read runs in a transaction that is rolled back; on Postgres it is
`READ ONLY` with a `statement_timeout` of the check's time. The visibility audit runs with
`persist=False`. The map timing makes GET requests to the in-stack web service. No email is sent.
The one write is the record of the run, `<data root>/preflight/<YYYYMMDDTHHMMSSZ>.json` (what `--json`
prints), which the next preflight compares its counts against.

**Bounded.** Each check has a timeout and the run a budget (`--budget`, 120 s). A check that times
out, or that the spent budget leaves no time for, is a WARN naming it. Its thread is abandoned, as
`app._run_with_timeout` abandons a hung job, and dies with the process. A check that raises is a
FAIL: the gate could not confirm what it checks.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import functools
import json
import logging
import os
import re
import statistics
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger("infra.scheduler.preflight")

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = ROOT / "services" / "db" / "migrations"

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}

#: How the host runs a one-off container of the deployed image: `remote_compose` in
#: `infra/scripts/seed_single_host.sh`, from `/opt/infraque/compose`.
HOST_COMPOSE = (
    "cd /opt/infraque/compose && IMAGE_TAG=$(cat /opt/infraque/current-tag) "
    "INFRAQUE_ENV_FILE=/opt/infraque/secrets/.env docker compose -f docker-compose.yml "
    "-f compose.prod.yml -f compose.single.yml --env-file /opt/infraque/secrets/.env"
)
PREFLIGHT_COMMAND = (
    f"{HOST_COMPOSE} run --rm --no-deps -T worker python -m infra.scheduler.bootstrap preflight"
)
#: `web` as the other containers reach it: the base Compose file's `web` listens on 8001, and the
#: Caddyfile proxies `web:8001`. Not through Caddy, so no TLS and no login.
IN_STACK_WEB_URL = "http://web:8001"
DEFAULT_BUDGET_S = 120.0

#: docs/64 §3 item 2 and the decisions log (2026-10-11): above this, the map's hidden-records line
#: loses its number.
MAP_COUNT_BUDGET_MS = 150.0
MAP_TIMING_REQUESTS = 15
WEB_REQUEST_TIMEOUT_S = 10.0
MAP_PATHS = ("/", "/?unlisted=include")

#: A count is "far outside" the previous preflight's when it moved by more than this share of the
#: previous figure and by at least `COUNT_DRIFT_MIN` rows. A day of scheduled fetches moves the
#: rehearsal store's totals by well under 1 %; a failed or doubled load moves them by far more.
COUNT_DRIFT_SHARE = 0.25
COUNT_DRIFT_MIN = 50

#: `infra/sops/secrets.example.plain.yaml` ships `Replace Me LLC` and `1 Replace Me St, City, ST
#: 00000`; `services/alerts/mail.py` renders `[SENDER_LEGAL_NAME unset]` in a dry run.
PLACEHOLDER = re.compile(r"replace[\s_-]*me|\[[A-Z_]+ unset\]|\bST 00000\b", re.IGNORECASE)

#: The hand-check worksheet (`data/eval/handcheck/2026-10-10-e2e-reference/README.md`): the seed it
#: names, and the folder name the beta copy takes.
HANDCHECK_SEED = 20261014
HANDCHECK_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}-beta")
HANDCHECK_WORKBOOK = "handcheck.xlsx"
HOST_HANDCHECK_DIR = "/opt/infraque/handcheck"

#: `SITES_ENABLED` values that switch sites off (`services/sites/switch.py`).
_SITES_OFF = frozenset({"0", "false", "off", "no"})


class WebUnreachable(Exception):
    """The web service did not answer at all: refused, unresolvable, or timed out."""


#: `(url, timeout_s) -> (status, seconds)`: one GET, timed from request to the last body byte.
HttpGet = Callable[[str, float], tuple[int, float]]


class HttpTimer:
    """The default `HttpGet`: one keep-alive client, so each timing is the request rather than a
    new connection. `trust_env=False`: an in-stack URL must never go through an outbound proxy."""

    def __init__(self) -> None:
        self._client: Any = None

    def __call__(self, url: str, timeout_s: float) -> tuple[int, float]:
        import httpx

        if self._client is None:
            self._client = httpx.Client(trust_env=False, follow_redirects=False)
        try:
            start = time.perf_counter()
            response = self._client.get(url, timeout=timeout_s)
            elapsed = time.perf_counter() - start
        except httpx.TransportError as exc:
            raise WebUnreachable(_short(exc)) from exc
        return int(response.status_code), elapsed


def default_freshness_rows(now: dt.datetime, data_root: Path) -> list[dict[str, Any]]:
    from infra.scheduler.freshness import store_report

    return store_report(now, data_root)


def default_open_store(data_root: Path) -> Any:
    from pipeline.connectors.store import open_store

    return open_store(data_root)


def default_audit(factory: sessionmaker[Session]) -> Mapping[str, Any]:
    from services.visibility_audit.run import run_audit

    return run_audit(factory, persist=False)


@dataclass
class Context:
    """What the checks read. Everything a test needs to stand in is a field."""

    now: dt.datetime
    data_root: Path
    env: Mapping[str, str]
    session_factory: sessionmaker[Session] | None
    web_url: str = IN_STACK_WEB_URL
    http_get: HttpGet = field(default_factory=HttpTimer)
    handcheck_dirs: Sequence[Path] = ()
    previous: Mapping[str, Any] | None = None
    freshness_rows: Callable[[dt.datetime, Path], list[dict[str, Any]]] = default_freshness_rows
    open_store: Callable[[Path], Any] = default_open_store
    audit: Callable[[sessionmaker[Session]], Mapping[str, Any]] = default_audit
    #: Set by the `database` check; the checks that read the store run only when it is True.
    database_ok: bool = False
    #: The time the running check may take, for its `statement_timeout`.
    timeout_s: float = 30.0

    @property
    def domain(self) -> str:
        return (self.env.get("DOMAIN") or "").strip() or "<DOMAIN>"


@dataclass
class Outcome:
    status: str
    summary: str
    measured: dict[str, Any] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Check:
    n: int
    id: str
    title: str
    run: Callable[[Context], Outcome]
    timeout_s: float
    needs_db: bool = False
    #: Execution order: cheap settings first, the timing before the audit so the audit's load does
    #: not skew it, the audit (the longest) last. Printing is by `n`.
    order: int = 0
    #: How to run this check with more time, printed when it times out.
    alone: str = ""


@dataclass
class Result:
    n: int
    id: str
    title: str
    status: str
    summary: str
    measured: dict[str, Any]
    actions: list[str]
    duration_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "measured": self.measured,
            "actions": self.actions,
            "duration_s": round(self.duration_s, 3),
        }


# ------------------------------------------------------------------------------------- helpers
def _short(exc: BaseException) -> str:
    """One line naming the error, with any credentials in a URL masked."""
    first = (str(exc).strip().splitlines() or [""])[0]
    first = re.sub(r"://[^@\s/]+@", "://***@", first)[:240]
    return f"{type(exc).__name__}: {first}" if first else type(exc).__name__


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    return aware.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def _n(value: int) -> str:
    return f"{value:,}"


@contextmanager
def read_session(ctx: Context) -> Iterator[Session]:
    """A session whose transaction is always rolled back. On Postgres it is also `READ ONLY` and
    bounded by the check's time, so a slow query ends with the check rather than holding the store."""
    if ctx.session_factory is None:
        raise RuntimeError("no database configured")
    session = ctx.session_factory()
    try:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
            limit_ms = str(max(1000, int(ctx.timeout_s * 1000)))
            session.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": limit_ms})
        yield session
    finally:
        session.rollback()
        session.close()


# ------------------------------------------------------------------------------ 1. the database
def migration_heads() -> set[str]:
    """The revisions the image's migration scripts end at (`alembic heads`, no database)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def alembic_versions(session: Session) -> set[str]:
    """The revisions the database is stamped at; empty when it has no `alembic_version` table."""
    import sqlalchemy as sa

    if not sa.inspect(session.connection()).has_table("alembic_version"):
        return set()
    return {str(v) for v in session.scalars(text("SELECT version_num FROM alembic_version"))}


def check_database(ctx: Context) -> Outcome:
    if ctx.session_factory is None:
        return Outcome(
            FAIL,
            "DATABASE_URL is not set, so nothing in the store can be checked",
            actions=[f"run it in the worker image, which reads the env file: {PREFLIGHT_COMMAND}"],
        )
    try:
        with read_session(ctx) as session:
            session.execute(text("SELECT 1"))
            dialect = session.get_bind().dialect.name
            current = sorted(alembic_versions(session))
    except Exception as exc:
        return Outcome(
            FAIL,
            f"database unreachable ({_short(exc)})",
            actions=["check `docker compose ... ps postgres` and `logs postgres` (docs/64 §5)"],
        )
    ctx.database_ok = True
    heads = sorted(migration_heads())
    measured = {"dialect": dialect, "current": current, "heads": heads}
    if current == heads:
        return Outcome(PASS, f"{dialect}, at the migration head {', '.join(heads)}", measured)
    upgrade = (
        f"{HOST_COMPOSE} run --rm --no-deps -T api alembic -c services/db/migrations/alembic.ini upgrade head"
    )
    if not current:
        return Outcome(
            FAIL,
            f"{dialect} has no alembic_version: the schema was never migrated (head {', '.join(heads)})",
            measured,
            [f"redeploy the tag (deploy.sh dumps, then migrates), or: {upgrade}"],
        )
    return Outcome(
        FAIL,
        f"{dialect} is at {', '.join(current)}, the image's head is {', '.join(heads)}",
        measured,
        [f"redeploy the tag (deploy.sh dumps, then migrates), or: {upgrade}"],
    )


# -------------------------------------------------------------------------------- 2. freshness
_ORDER = {"fresh": 0, "late": 1, "stale": 2, "never": 3}


def _days(hours: Any) -> str:
    return f"{float(hours) / 24:.1f} d" if hours is not None else "?"


def check_freshness(ctx: Context) -> Outcome:
    """`freshness.store_report` over the run records (the run-record vintage rule included), and
    `freshness.assess_source` over the `source` rows the masthead and `freshness_tick` read, given
    the same held vintage."""
    rows = ctx.freshness_rows(ctx.now, ctx.data_root)
    scheduled = [r for r in rows if r.get("status") != "unscheduled"]
    db_view: dict[str, dict[str, Any]] | None = None
    if ctx.database_ok:
        from sqlalchemy import select

        from infra.scheduler.freshness import assess_source
        from services.db.models import Source

        vintage_of = {str(r["source_id"]): r.get("vintage") for r in rows}
        db_view = {}
        with read_session(ctx) as session:
            for source in session.scalars(select(Source)):
                fr = assess_source(source, ctx.now, vintage=vintage_of.get(source.id))
                db_view[source.id] = fr.to_dict()

    tally: Counter[str] = Counter()
    per_source: dict[str, dict[str, Any]] = {}
    #: Problems shared by many sources are one line each, naming the sources.
    never_store: list[str] = []
    never_db: list[str] = []
    no_row: list[str] = []
    paused: list[str] = []
    stale: list[str] = []
    for row in scheduled:
        sid = str(row["source_id"])
        store_status = str(row.get("status"))
        db = db_view.get(sid) if db_view is not None else None
        db_status = str(db["status"]) if db is not None else ("no_row" if db_view is not None else "not_read")
        effective = store_status
        if db_status in _ORDER and _ORDER[db_status] > _ORDER.get(effective, 0):
            effective = db_status
        tally[effective] += 1
        per_source[sid] = {
            "store": store_status,
            "database": db_status,
            "age_hours": row.get("age_hours"),
            "stale_after_hours": row.get("stale_after_hours"),
            "vintage": row.get("vintage"),
            "vintage_stale": row.get("vintage_stale"),
        }
        if store_status == "never":
            never_store.append(sid)
        elif db_status == "never":
            never_db.append(sid)
        if "stale" in (store_status, db_status):
            why = (
                f"the release after {row.get('vintage')} was due {row.get('vintage_overdue_at')}"
                if row.get("vintage_stale")
                else f"last success {_days(row.get('age_hours'))} ago, stale after "
                f"{_days(row.get('stale_after_hours'))}"
            )
            stale.append(f"{sid}: stale ({why})")
        if db_status == "no_row":
            no_row.append(sid)
        elif db_status == "paused":
            paused.append(sid)
    states = ", ".join(f"{tally[s]} {s}" for s in ("fresh", "late", "stale", "never") if tally[s])
    summary = f"{len(scheduled)} scheduled sources: {states or 'none'}"
    measured = {"scheduled": len(scheduled), "by_status": dict(tally), "sources": per_source}
    if not scheduled:
        return Outcome(FAIL, "no implemented, scheduled source in the registry", measured)
    fails = []
    if never_store:
        fails.append(f"no successful run record (ok or unchanged) in the store: {', '.join(never_store)}")
    if never_db:
        fails.append(
            f"no last success on the `source` row, so the site counts them unfetched: {', '.join(never_db)}"
        )
    warns = list(stale)
    if no_row:
        warns.append(f"no `source` row, so no fetch is recorded in the database: {', '.join(no_row)}")
    fetch = (
        "fetch every source now, then rerun once the jobs finish: "
        f"{HOST_COMPOSE} run --rm --no-deps -T worker python -m infra.scheduler.bootstrap fetch"
    )
    notes = [f"paused by an operator, so not expected to be fresh: {', '.join(paused)}"] if paused else []
    if fails:
        return Outcome(FAIL, summary, measured, [*fails, *warns, *notes, fetch])
    if warns:
        actions = [
            *warns,
            *notes,
            f"read each source's last error on https://admin.{ctx.domain}/admin/sources; docs/60 §10.4",
        ]
        if no_row:
            actions.append(fetch)
        return Outcome(WARN, summary, measured, actions)
    return Outcome(PASS, summary, measured, notes)


# ------------------------------------------------------------------------------ 3. held runs
def check_held_runs(ctx: Context) -> Outcome:
    """Runs the data-quality gates held (`status = partial`, `dq_status = fail`) that no later run
    with output superseded and nobody released: their rows were never loaded. The same rule the
    release route applies (`services/api/admin_sources.py` D12). The store is read as well, for a
    held run shipped by the seed that has no `source_run` row (a hand run records none)."""
    import uuid

    from sqlalchemy import exists, select
    from sqlalchemy.orm import aliased

    from pipeline.connectors.registry import Registry
    from services.db.models import Source, SourceRun
    from services.ids import public_id

    held: list[dict[str, Any]] = []
    gated: list[dict[str, Any]] = []
    later = aliased(SourceRun)
    with read_session(ctx) as session:
        superseded = exists(
            select(later.id).where(
                later.source_id == SourceRun.source_id,
                later.started_at > SourceRun.started_at,
                later.status.in_(("ok", "partial")),
            )
        )
        stmt = (
            select(SourceRun)
            .where(
                SourceRun.status == "partial",
                SourceRun.dq_status == "fail",
                SourceRun.released_at.is_(None),
                ~superseded,
            )
            .order_by(SourceRun.source_id, SourceRun.started_at)
        )
        for run in session.scalars(stmt):
            source = session.get(Source, run.source_id)
            row = {
                "source_id": run.source_id,
                "run_id": public_id("run", run.id),
                "started_at": _iso(run.started_at),
                "where": "database",
            }
            (gated if source is not None and source.gated else held).append(row)

        store = ctx.open_store(ctx.data_root)
        for status_row in Registry().status():
            if status_row.get("state") != "implemented":
                continue
            source_id = str(status_row["id"])
            with_output = [r for r in store.runs(source_id) if r.get("status") in ("ok", "partial")]
            if not with_output:
                continue
            last = with_output[-1]
            if last.get("status") != "partial" or not (last.get("outputs") or {}).get("held"):
                continue
            try:
                key = uuid.UUID(str(last.get("id")))
            except ValueError:
                key = None
            if key is not None and session.get(SourceRun, key) is not None:
                continue  # the database half above has already judged it
            held.append(
                {
                    "source_id": source_id,
                    "run_id": str(last.get("id")),
                    "started_at": last.get("started_at"),
                    "where": "store",
                }
            )
    measured = {"held": held, "gated_not_releasable": gated}
    if not held:
        note = f"; {len(gated)} on gated sources, which a release never lifts" if gated else ""
        return Outcome(PASS, f"no held run waits for release{note}", measured)
    actions = []
    for row in held:
        if row["where"] == "database":
            actions.append(
                f"{row['source_id']} run {row['run_id']} (held since {row['started_at']}): read its "
                f"data-quality failures at https://admin.{ctx.domain}/admin/source-runs/{row['run_id']}, "
                "then Release it with a reason (POST /admin/v1/source-runs/{id}/release), or fix the source"
            )
        else:
            actions.append(
                f"{row['source_id']} run {row['run_id']} is held in the store with no source_run row "
                "(a hand run shipped by the seed): run `bootstrap fetch`; a refetch held again "
                "appears in the admin's source runs, to release there"
            )
    return Outcome(
        FAIL, f"{len(held)} held run(s) wait for release, so their rows were never loaded", measured, actions
    )


# ------------------------------------------------------------------------------------ 4. counts
def _drift(previous: Any, current: int) -> bool:
    if not isinstance(previous, int | float) or isinstance(previous, bool):
        return False
    delta = abs(current - previous)
    return delta >= COUNT_DRIFT_MIN and delta > COUNT_DRIFT_SHARE * previous


def previous_counts(previous: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The `counts` check's figures in a previous preflight record, if it has them."""
    if not previous:
        return None
    for check in previous.get("checks") or []:
        if isinstance(check, Mapping) and check.get("id") == "counts":
            measured = check.get("measured")
            if isinstance(measured, Mapping) and isinstance(measured.get("totals"), Mapping):
                return measured
    return None


def check_counts(ctx: Context) -> Outcome:
    from sqlalchemy import distinct, func, select

    from services.api.visibility import (
        event_visibility_filter,
        opportunity_visibility_filter,
        proposal_visibility_filter,
    )
    from services.db.models import Event, Opportunity, OpportunitySource, Proposal, ProposalSource, Source
    from services.ingest.loader import SPECIALISED_LOADERS

    with read_session(ctx) as session:

        def count(stmt: Any) -> int:
            return int(session.scalar(stmt) or 0)

        totals = {
            "proposals_live": count(select(func.count(Proposal.id)).where(Proposal.merged_into_id.is_(None))),
            "proposals_public": count(
                select(func.count(Proposal.id)).where(*proposal_visibility_filter("public", ctx.now))
            ),
            "opportunities_live": count(
                select(func.count(Opportunity.id)).where(Opportunity.merged_into_id.is_(None))
            ),
            "opportunities_public": count(
                select(func.count(Opportunity.id)).where(*opportunity_visibility_filter("public", ctx.now))
            ),
            "events": count(
                select(func.count(Event.id)).where(Event.subject_type.in_(("proposal", "opportunity")))
            ),
            "events_public": count(
                select(func.count(Event.id)).where(*event_visibility_filter("public", ctx.now))
            ),
        }
        per_source: dict[str, dict[str, Any]] = {}
        for source in session.scalars(select(Source).order_by(Source.id)):
            if source.implemented or source.last_loaded_ts:
                per_source[source.id] = {
                    "proposals": 0,
                    "opportunities": 0,
                    "last_loaded_ts": source.last_loaded_ts,
                }
        for model, link, fk, key in (
            (Proposal, ProposalSource, ProposalSource.proposal_id, "proposals"),
            (Opportunity, OpportunitySource, OpportunitySource.opportunity_id, "opportunities"),
        ):
            stmt = (
                select(link.source_id, func.count(distinct(fk)))
                .join(model, model.id == fk)
                .where(link.active.is_(True), model.merged_into_id.is_(None))
                .group_by(link.source_id)
            )
            for source_id, n in session.execute(stmt):
                entry = per_source.setdefault(
                    str(source_id), {"proposals": 0, "opportunities": 0, "last_loaded_ts": None}
                )
                entry[key] = int(n)

    fails: list[str] = []
    notes: list[str] = []
    if totals["proposals_live"] == 0:
        fails.append("the store holds no live proposal: the seed's loads did not run or did not commit")
    elif totals["proposals_public"] == 0:
        fails.append(
            "no proposal is public, so the site would show none: check PLATFORM_POSTURE and the sources' "
            "publish_state on the admin source pages"
        )
    for sid, entry in per_source.items():
        if not entry["last_loaded_ts"]:
            continue
        if sid in SPECIALISED_LOADERS:
            entry["specialised_loader"] = SPECIALISED_LOADERS[sid]
            continue  # it writes assets or nothing by design, never proposals (services/ingest/loader.py)
        if entry["proposals"] + entry["opportunities"] == 0:
            fails.append(
                f"{sid} loaded run {entry['last_loaded_ts']} but has no live proposal or opportunity"
            )
    drifts: list[str] = []
    prev = previous_counts(ctx.previous)
    if prev is not None:
        prev_totals = prev.get("totals") or {}
        for key, value in totals.items():
            if _drift(prev_totals.get(key), value):
                drifts.append(f"{key}: {_n(int(prev_totals[key]))} -> {_n(value)}")
        prev_sources = prev.get("per_source") or {}
        for sid, entry in per_source.items():
            before = prev_sources.get(sid)
            if not isinstance(before, Mapping):
                continue
            for key in ("proposals", "opportunities"):
                if _drift(before.get(key), int(entry[key])):
                    drifts.append(f"{sid} {key}: {_n(int(before[key]))} -> {_n(int(entry[key]))}")
    else:
        notes.append("no previous preflight record to compare against")
    measured: dict[str, Any] = {
        "totals": totals,
        "per_source": per_source,
        "compared_with": (ctx.previous or {}).get("started_at") if prev is not None else None,
        "drift": drifts,
    }
    loaded = sum(1 for e in per_source.values() if e["last_loaded_ts"])
    summary = (
        f"{_n(totals['proposals_live'])} live proposals ({_n(totals['proposals_public'])} public), "
        f"{_n(totals['opportunities_live'])} opportunities ({_n(totals['opportunities_public'])} public), "
        f"{_n(totals['events'])} events ({_n(totals['events_public'])} public); {loaded} sources loaded"
    )
    if fails:
        return Outcome(
            FAIL,
            summary,
            measured,
            [*fails, "read the worker log for that source's `load_source` job; rerun `bootstrap load`"],
        )
    if drifts:
        return Outcome(
            WARN,
            f"{summary}; {len(drifts)} figure(s) moved more than {COUNT_DRIFT_SHARE:.0%} since "
            f"the preflight of {measured['compared_with']}",
            measured,
            [
                *drifts,
                "a large drop is a load that failed or a resolve that merged too much: read the worker log",
            ],
        )
    return Outcome(PASS, summary, measured, notes)


# ------------------------------------------------------------------------- 5. visibility audit
def check_visibility_audit(ctx: Context) -> Outcome:
    """M-11: today's nightly result when there is one (`visibility_audit_tick`, 04:52 UTC), else a
    run now that is not persisted."""
    from services.visibility_audit.run import latest_results

    result: Mapping[str, Any] | None = None
    origin = "run now, not persisted"
    with read_session(ctx) as session:
        latest = latest_results(session, limit=1)
        if latest and _aware(latest[0].observed_at).date() == ctx.now.date() and latest[0].after:
            result = dict(latest[0].after)
            origin = f"nightly run of {_iso(latest[0].observed_at)}"
    if result is None:
        if ctx.session_factory is None:  # pragma: no cover - the check needs the database
            raise RuntimeError("no database configured")
        result = ctx.audit(ctx.session_factory)
    m11 = int(result.get("m11") or 0)
    served = result.get("served") or {}
    counts = result.get("counts") or {}
    shown = {k: v.get("shown") for k, v in counts.items() if isinstance(v, Mapping)}
    measured = {
        "origin": origin,
        "run_id": result.get("run_id"),
        "run_at": result.get("run_at"),
        "posture": result.get("posture"),
        "m11": m11,
        "shown": shown,
        "served_checked": served.get("checked"),
        "served_leaks": served.get("leaks"),
        "served_inconclusive": served.get("inconclusive"),
        "withheld_links": result.get("withheld_links"),
        "breaches": [
            {k: b.get(k) for k in ("surface", "public_id", "source_id", "reason")}
            for b in (result.get("breaches") or [])[:10]
            if isinstance(b, Mapping)
        ],
    }
    summary = (
        f"M-11 = {m11} ({origin}, posture {result.get('posture')}); "
        f"{served.get('checked', 0)} served checks, {served.get('leaks', 0)} leaks"
    )
    if m11 > 0:
        return Outcome(
            FAIL,
            summary,
            measured,
            [
                "a gated or restricted row is reachable on a public surface: an S1 incident (docs/04 S-9); "
                "do not invite anyone",
                f"the breaches: https://admin.{ctx.domain}/admin/v1/visibility-audits, or "
                f"{HOST_COMPOSE} run --rm --no-deps -T worker "
                "python -m services.visibility_audit.run --no-persist --json",
            ],
        )
    if int(served.get("inconclusive") or 0) > 0:
        return Outcome(
            WARN,
            f"{summary}; {served.get('inconclusive')} served checks inconclusive (not 200 or 404)",
            measured,
            [
                "rerun it alone and read the served checks: "
                "python -m services.visibility_audit.run --no-persist --json"
            ],
        )
    return Outcome(PASS, summary, measured)


# ------------------------------------------------------------------------------------- 6. sites
RESOLVE_SUCCEEDED_SQL = text(
    """
    SELECT MAX(e.at) FROM procrastinate_events e
      JOIN procrastinate_jobs j ON j.id = e.job_id
     WHERE j.task_name = 'resolve_tick' AND e.type = 'succeeded'
    """
)


def check_sites(ctx: Context) -> Outcome:
    from sqlalchemy import func, select

    from services.db.models import Proposal, Site, SiteAudit, SiteMember

    with read_session(ctx) as session:
        live = Site.retired_at.is_(None)
        sites = int(session.scalar(select(func.count(Site.id)).where(live)) or 0)
        members = int(session.scalar(select(func.sum(Site.member_count)).where(live)) or 0)
        member_rows = int(session.scalar(select(func.count(SiteMember.id))) or 0)
        oversize = [
            {"site": pid, "members": n}
            for pid, n in session.execute(
                select(Site.public_id, Site.member_count)
                .where(live, Site.review_flag == "oversize")
                .order_by(Site.member_count.desc())
            )
        ]
        built_at = session.scalar(select(func.max(Site.built_at)))
        audit_at = session.scalar(select(func.max(SiteAudit.recorded_at)))
        live_proposals = int(
            session.scalar(select(func.count(Proposal.id)).where(Proposal.merged_into_id.is_(None))) or 0
        )
        resolved_at = None
        if session.get_bind().dialect.name == "postgresql":
            try:
                with session.begin_nested():
                    resolved_at = session.scalar(RESOLVE_SUCCEEDED_SQL)
            except Exception:
                resolved_at = None  # no queue schema here: say nothing rather than guess
    enabled = (ctx.env.get("SITES_ENABLED") or "").strip().lower() not in _SITES_OFF
    measured = {
        "sites": sites,
        "members": members,
        "member_rows": member_rows,
        "oversize": oversize,
        "last_site_change_at": _iso(built_at),
        "last_site_audit_at": _iso(audit_at),
        "last_resolve_succeeded_at": _iso(resolved_at),
        "sites_enabled": enabled,
    }
    changed = max((_aware(t) for t in (built_at, audit_at) if t is not None), default=None)
    if resolved_at is not None:
        last = f"last rebuild {_iso(resolved_at)} (the last resolve_tick that succeeded)"
    elif changed is not None:
        last = f"last rebuild that changed a site {_iso(changed)}"
    else:
        last = "no rebuild has written a site"
    summary = f"{_n(sites)} sites, {_n(members)} members, {len(oversize)} oversize; {last}" + (
        "" if enabled else "; SITES_ENABLED is off, so readers see none"
    )
    if oversize:
        names = ", ".join(f"{o['site']} ({o['members']} members)" for o in oversize[:10])
        return Outcome(
            FAIL,
            summary,
            measured,
            [
                f"flagged oversize, withheld from readers until reviewed: {names}",
                f"inspect: {HOST_COMPOSE} run --rm --no-deps -T worker "
                "python -m services.sites --dry-run --top 10",
                "if the grouping is wrong, set SITES_ENABLED=0 for the beta (services/sites/switch.py)",
            ],
        )
    if sites == 0 and live_proposals > 0:
        return Outcome(
            WARN,
            summary,
            measured,
            [
                "no site over a store with live proposals: the resolve tick's site rebuild has not run or "
                "failed; look for `site rebuild failed` in the worker log, or run "
                f"{HOST_COMPOSE} run --rm --no-deps -T worker python -m services.sites --dry-run",
            ],
        )
    return Outcome(PASS, summary, measured)


# ---------------------------------------------------------------------------------- 7. unlisted
def check_unlisted(ctx: Context) -> Outcome:
    """docs/64 §3 item 1: `GET /v1/proposals?listed=false&include=count&lifecycle_state=<active>`
    as the public tier, counted with the list route's own clauses (`proposal_visibility_filter`,
    `listing.listed_clause`) rather than over HTTP."""
    import sqlalchemy as sa
    from sqlalchemy import func, select

    from services.api.listing import listed_clause
    from services.api.slippage import SLIP_ACTIVE_STATES
    from services.api.visibility import proposal_visibility_filter
    from services.db.models import Proposal

    with read_session(ctx) as session:
        base = [
            *proposal_visibility_filter("public", ctx.now),
            Proposal.lifecycle_state.in_(SLIP_ACTIVE_STATES),
        ]
        active = int(session.scalar(select(func.count(Proposal.id)).where(*base)) or 0)
        unlisted = int(
            session.scalar(select(func.count(Proposal.id)).where(*base, sa.not_(listed_clause("public"))))
            or 0
        )
    share = f"{unlisted / active:.1%}" if active else "n/a"
    measured = {"unlisted_active": unlisted, "active_public": active, "states": list(SLIP_ACTIVE_STATES)}
    return Outcome(
        PASS,
        f"{_n(unlisted)} of {_n(active)} active public proposals are no longer listed by any "
        f"register ({share}); the map and list hide them by default",
        measured,
        ["record this figure in docs/64 §3 item 1"],
    )


# ---------------------------------------------------------------------------- 8. map count cost
def check_map_count_cost(ctx: Context) -> Outcome:
    """docs/64 §3 item 2: the median of `MAP_TIMING_REQUESTS` requests each to `/` (hides and counts)
    and `/?unlisted=include` (neither), interleaved so drift falls on both, after one unmeasured
    request each. Timed from this container to `web` directly: no TLS, no Caddy, no login."""
    base = ctx.web_url.rstrip("/")
    host_loop = (
        "for p in / '/?unlisted=include'; do for i in $(seq 15); do curl -s -o /dev/null "
        f"-u '<user>:<password>' -w '%{{time_total}}\\n' \"https://{ctx.domain}$p\"; done "
        "| sort -n | sed -n 8p; done"
    )
    try:
        for path in MAP_PATHS:
            ctx.http_get(base + path, WEB_REQUEST_TIMEOUT_S)
        times: dict[str, list[float]] = {path: [] for path in MAP_PATHS}
        statuses: Counter[str] = Counter()
        for i in range(MAP_TIMING_REQUESTS):
            for path in MAP_PATHS if i % 2 == 0 else tuple(reversed(MAP_PATHS)):
                status, seconds = ctx.http_get(base + path, WEB_REQUEST_TIMEOUT_S)
                statuses[f"{path} {status}"] += 1
                if status != 200:
                    return Outcome(
                        WARN,
                        f"skipped: {base}{path} answered {status}",
                        {"web_url": base, "statuses": dict(statuses)},
                        [f"read `docker compose ... logs web`; then measure from the host: {host_loop}"],
                    )
                times[path].append(seconds)
    except WebUnreachable as exc:
        return Outcome(
            WARN,
            f"skipped: the web service is unreachable at {base} from here ({exc})",
            {"web_url": base},
            [
                "run the preflight in the worker container on the beta host, where `web` resolves: "
                + PREFLIGHT_COMMAND,
                f"or measure through Caddy from the host (prints the two medians, in seconds): {host_loop}",
            ],
        )
    hides, plain = (statistics.median(times[p]) * 1000 for p in MAP_PATHS)
    diff = hides - plain
    measured = {
        "web_url": base,
        "requests_each": MAP_TIMING_REQUESTS,
        "median_ms": {MAP_PATHS[0]: round(hides, 1), MAP_PATHS[1]: round(plain, 1)},
        "difference_ms": round(diff, 1),
        "budget_ms": MAP_COUNT_BUDGET_MS,
    }
    summary = (
        f"median of {MAP_TIMING_REQUESTS}: / {hides:.0f} ms, /?unlisted=include {plain:.0f} ms, "
        f"difference {diff:+.0f} ms (budget {MAP_COUNT_BUDGET_MS:.0f} ms)"
    )
    if diff > MAP_COUNT_BUDGET_MS:
        return Outcome(
            WARN,
            summary,
            measured,
            [
                "above 150 ms: replace the hidden-records number on the map with a line without one "
                "(lane U's alternative, docs/30 §5.1; decisions log 2026-10-11), and record the figures "
                "in docs/64 §3"
            ],
        )
    return Outcome(PASS, summary, measured, ["record these figures in docs/64 §3 item 2"])


# ------------------------------------------------------------------------------------- 9. email
def is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER.search(value))


def check_email(ctx: Context) -> Outcome:
    """Whether sign-up verification, password reset and alert emails can leave, from the settings
    alone. Sends nothing."""
    key = (ctx.env.get("RESEND_API_KEY") or "").strip()
    states: dict[str, str] = {"RESEND_API_KEY": "set" if key else "unset"}
    problems: list[str] = []
    if not key:
        problems.append(
            "RESEND_API_KEY is unset: every email is a dry run, sign-up verification and password reset "
            "included, and outside development the API returns no dev_verification_url or dev_reset_url "
            "either, so a tester cannot verify an address"
        )
    for name in ("SENDER_LEGAL_NAME", "SENDER_POSTAL_ADDRESS"):
        value = (ctx.env.get(name) or "").strip()
        if not value:
            states[name] = "unset"
            problems.append(f"{name} is unset: production refuses every alert (SenderIdentityMissing)")
        elif is_placeholder(value):
            states[name] = "placeholder"
            problems.append(
                f"{name} is still the secrets example's placeholder: it would be printed as the legal "
                "sender in every alert's footer (CAN-SPAM, docs/13 §1)"
            )
        else:
            states[name] = "set"
    measured = {"settings": states}
    summary = ", ".join(f"{k} {v}" for k, v in states.items())
    if problems:
        return Outcome(
            FAIL,
            summary,
            measured,
            [*problems, "set them in the secrets file (infra/sops/README.md) and redeploy the same tag"],
        )
    return Outcome(PASS, f"{summary}; nothing was sent", measured)


# ----------------------------------------------------------------------------- 10. email domain
def check_email_domain(ctx: Context) -> Outcome:
    """Every email's links and From address come from `services.api.common.DOMAIN` (`WEB_HOST`,
    `alerts@{DOMAIN}` in `services/alerts/mail.py` and `services/api/auth.py`), never from the
    `DOMAIN` setting Caddy serves. They must agree."""
    from services.alerts.mail import FROM_ADDRESS
    from services.api.common import DOMAIN, WEB_HOST

    code = DOMAIN.lower()
    env_domain = (ctx.env.get("DOMAIN") or "").strip().lower()
    measured = {
        "env_domain": env_domain or None,
        "code_domain": code,
        "web_host": WEB_HOST,
        "alert_from": FROM_ADDRESS,
    }
    problems: list[str] = []
    if env_domain and env_domain != code:
        problems.append(
            f"DOMAIN is {env_domain}, but every email's links and From address use "
            f"services.api.common.DOMAIN = {code}: verification and reset links would point at {WEB_HOST}, "
            "not the beta host, and "
            f"Resend refuses a From domain it has not verified",
        )
    if problems:
        return Outcome(
            FAIL,
            f"DOMAIN {env_domain or 'unset'} vs the code's {code}",
            measured,
            [
                *problems,
                f"the Resend sending domain must be {code} (or change services.api.common.DOMAIN to the beta "
                "domain before inviting anyone)",
            ],
        )
    if not env_domain:
        return Outcome(
            WARN,
            f"DOMAIN is unset here, so it cannot be compared with the code's {code}",
            measured,
            [f"run it in the worker image, which reads the env file: {PREFLIGHT_COMMAND}"],
        )
    return Outcome(
        PASS,
        f"DOMAIN {env_domain} matches services.api.common.DOMAIN; the Resend sending domain must be {code}",
        measured,
    )


# ------------------------------------------------------------------------------- 11. monitoring
def _bcrypt_from_base64(value: str) -> bool:
    try:
        decoded = base64.b64decode(value, validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return decoded.startswith(("$2a$", "$2b$", "$2y$"))


def check_monitoring(ctx: Context) -> Outcome:
    """Error reporting, the content security policy's mode and the beta's login gate, from the
    settings every container reads (docs/64 §2 steps 6 and 8, §5). The uptime monitors live at
    UptimeRobot and cannot be seen from here (docs/64 §2 steps 7 and 9)."""
    env = ctx.env
    sentry = bool((env.get("SENTRY_DSN") or "").strip())
    csp = (env.get("CSP_MODE") or "").strip().lower() or "report"
    access = (env.get("SITE_ACCESS") or "").strip().lower() or "open"
    user = bool((env.get("SITE_ACCESS_USER") or "").strip())
    hashed = (env.get("SITE_ACCESS_HASH") or "").strip()
    measured = {
        "SENTRY_DSN": "set" if sentry else "unset",
        "CSP_MODE": csp,
        "SITE_ACCESS": access,
        "SITE_ACCESS_USER": "set" if user else "unset",
        "SITE_ACCESS_HASH": "set" if hashed else "unset",
    }
    fails: list[str] = []
    warns: list[str] = []
    if access != "basic":
        fails.append(
            f"SITE_ACCESS is {access}: the site is open to anyone during the private beta; set "
            "SITE_ACCESS=basic with SITE_ACCESS_USER and SITE_ACCESS_HASH (docs/64 §2 step 6) and "
            "redeploy the same tag"
        )
    else:
        if not user or not hashed:
            fails.append(
                "SITE_ACCESS=basic needs SITE_ACCESS_USER and SITE_ACCESS_HASH; deploy.sh refuses it"
            )
        elif not _bcrypt_from_base64(hashed):
            fails.append(
                "SITE_ACCESS_HASH is not a base64-encoded bcrypt hash "
                "(caddy hash-password --plaintext '<password>' | base64 -w0); deploy.sh refuses it"
            )
    if csp not in ("report", "enforce"):
        fails.append(f"CSP_MODE is {csp}: Caddy refuses to start on any value but report or enforce")
    elif csp == "enforce":
        warns.append(
            "CSP_MODE=enforce before the docs/64 §5 checks (Chrome's reports arrive; 7 consecutive beta days "
            "without a report): a violation breaks a page for a tester"
        )
    if not sentry:
        warns.append("SENTRY_DSN is unset: no error is reported to anyone (docs/64 §2 step 8)")
    summary = (
        f"SITE_ACCESS {access}{' with login' if access == 'basic' and user and hashed else ''}, "
        f"CSP_MODE {csp}, Sentry {'on' if sentry else 'off'}"
    )
    actions = [*fails, *warns]
    if fails:
        return Outcome(FAIL, summary, measured, actions)
    if warns:
        return Outcome(WARN, summary, measured, actions)
    return Outcome(
        PASS, summary, measured, ["uptime monitors are not visible from here: docs/64 §2 steps 7 and 9"]
    )


# -------------------------------------------------------------------------------- 12. hand check
def default_handcheck_dirs(data_root: Path) -> list[Path]:
    """Where a `<date>-beta` worksheet may be: under the data root, the README's container mount
    (`/out`), the README's host folder, and the checkout's `data/eval/handcheck/`."""
    return [
        data_root / "eval" / "handcheck",
        Path("/out"),
        Path(HOST_HANDCHECK_DIR),
        ROOT / "data" / "eval" / "handcheck",
    ]


def handcheck_command(ctx: Context) -> str:
    from services.posture import normalise_posture

    posture = normalise_posture(ctx.env.get("PLATFORM_POSTURE"))
    return (
        f"mkdir -p {HOST_HANDCHECK_DIR} && chown 10001 {HOST_HANDCHECK_DIR} && "
        f"{HOST_COMPOSE} run --rm --no-deps -T -v {HOST_HANDCHECK_DIR}:/out worker "
        f"python -m services.sites.handcheck generate --out /out/{ctx.now:%Y-%m-%d}-beta "
        f"--seed {HANDCHECK_SEED} --posture {posture} --base-url https://{ctx.domain}"
    )


def check_handcheck(ctx: Context) -> Outcome:
    looked = [str(d) for d in ctx.handcheck_dirs]
    found: list[dict[str, Any]] = []
    for folder in ctx.handcheck_dirs:
        if not folder.is_dir():
            continue
        for child in sorted(folder.iterdir()):
            if child.is_dir() and HANDCHECK_FOLDER.fullmatch(child.name):
                found.append({"path": str(child), "workbook": (child / HANDCHECK_WORKBOOK).is_file()})
    ready = [f for f in found if f["workbook"]]
    measured = {"looked_in": looked, "found": found}
    if ready:
        return Outcome(PASS, f"worksheet found: {ready[-1]['path']}/{HANDCHECK_WORKBOOK}", measured)
    what = "a <date>-beta folder without its workbook" if found else "no <date>-beta worksheet"
    return Outcome(
        WARN,
        f"{what} in {len(looked)} places (data/eval/handcheck/ under the data root and the README's folders)",
        measured,
        [
            f"generate it from this store (data/eval/handcheck/2026-10-10-e2e-reference/README.md): "
            f"{handcheck_command(ctx)}",
            f"copy it back to data/eval/handcheck/{ctx.now:%Y-%m-%d}-beta/ in the checkout; to let the "
            f"preflight see it here, add `-v {HOST_HANDCHECK_DIR}:/out:ro` before `worker`",
        ],
    )


# ------------------------------------------------------------------------------------ the run
CHECKS: tuple[Check, ...] = (
    Check(1, "database", "Database", check_database, 15, order=1, alone="--only database"),
    Check(2, "freshness", "Source freshness", check_freshness, 30, needs_db=True, order=6),
    Check(3, "held_runs", "Held runs", check_held_runs, 20, needs_db=True, order=7),
    Check(4, "counts", "Record counts", check_counts, 30, needs_db=True, order=8),
    Check(
        5,
        "visibility_audit",
        "Visibility audit (M-11)",
        check_visibility_audit,
        75,
        needs_db=True,
        order=12,
        alone="python -m services.visibility_audit.run --no-persist",
    ),
    Check(6, "sites", "Sites", check_sites, 15, needs_db=True, order=9),
    Check(7, "unlisted", "No longer listed", check_unlisted, 20, needs_db=True, order=10),
    Check(8, "map_count_cost", "Map hidden-count cost", check_map_count_cost, 45, order=11),
    Check(9, "email", "Email can be sent", check_email, 5, order=2),
    Check(10, "email_domain", "Email domain", check_email_domain, 10, order=3),
    Check(11, "monitoring", "Monitoring and gate", check_monitoring, 5, order=4),
    Check(12, "handcheck", "Hand-check worksheet", check_handcheck, 5, order=5),
)


def _run_bounded(fn: Callable[[], Outcome], timeout_s: float) -> Outcome | BaseException | None:
    """`fn()` in a daemon thread: its outcome, the exception it raised, or None when it is still
    running after `timeout_s` (it is then abandoned and dies with the process)."""
    box: list[Outcome | BaseException] = []

    def target() -> None:
        try:
            box.append(fn())
        except BaseException as exc:
            box.append(exc)

    thread = threading.Thread(target=target, name="preflight-check", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive() or not box:
        return None
    return box[0]


def run_checks(
    ctx: Context,
    checks: Sequence[Check] | None = None,
    *,
    budget_s: float = DEFAULT_BUDGET_S,
    only: Sequence[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[Result]:
    """Run the selected checks within `budget_s` and return one result per check, in `n` order. The
    `database` check runs whenever a selected check reads the store, since they depend on it."""
    checks = CHECKS if checks is None else checks
    wanted = set(only or [c.id for c in checks])
    if any(c.needs_db and c.id in wanted for c in checks):
        wanted.add("database")
    selected = sorted((c for c in checks if c.id in wanted), key=lambda c: (c.order, c.n))
    started = clock()
    results: list[Result] = []
    for check in selected:
        remaining = budget_s - (clock() - started)
        t0 = clock()
        if check.needs_db and not ctx.database_ok:
            outcome = Outcome(WARN, "not run: it reads the store, and the database check failed")
        elif remaining < 1.0:
            outcome = Outcome(
                WARN,
                f"not run: the preflight's {budget_s:.0f} s budget was spent before check {check.n} "
                f"({check.id})",
                actions=[f"run it alone: {_alone(check)}"],
            )
        else:
            timeout = min(check.timeout_s, remaining)
            ctx.timeout_s = timeout
            got = _run_bounded(functools.partial(check.run, ctx), timeout)
            if got is None:
                outcome = Outcome(
                    WARN,
                    f"check {check.n} ({check.id}) timed out after {round(timeout, 1):g} s",
                    {"timeout_s": round(timeout, 1)},
                    [f"run it alone, with more time: {_alone(check)}"],
                )
            elif isinstance(got, BaseException):
                logger.error("preflight check %s raised", check.id, exc_info=got)
                outcome = Outcome(
                    FAIL,
                    f"check {check.n} ({check.id}) could not finish: {_short(got)}",
                    actions=[f"rerun it alone and read the traceback: {_alone(check)}"],
                )
            else:
                outcome = got
        results.append(
            Result(
                check.n,
                check.id,
                check.title,
                outcome.status,
                outcome.summary,
                outcome.measured,
                outcome.actions,
                clock() - t0,
            )
        )
    return sorted(results, key=lambda r: r.n)


def _alone(check: Check) -> str:
    if check.alone.startswith("python "):
        return f"{HOST_COMPOSE} run --rm --no-deps -T worker {check.alone}"
    return f"{PREFLIGHT_COMMAND} --only {check.id} --budget 600"


def verdict(results: Sequence[Result]) -> str:
    return "NO-GO" if any(r.status == FAIL for r in results) else "GO"


# ---------------------------------------------------------------------------------- the record
def record_dir(data_root: Path) -> Path:
    return data_root / "preflight"


def load_previous(data_root: Path) -> Mapping[str, Any] | None:
    """The newest earlier preflight record that holds the counts, if any (`--only` runs may not)."""
    folder = record_dir(data_root)
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.json"), reverse=True)[:50]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and previous_counts(data) is not None:
            return data
    return None


def record_path(data_root: Path, now: dt.datetime) -> Path:
    path = record_dir(data_root) / f"{now:%Y%m%dT%H%M%SZ}.json"
    if path.exists():
        path = path.with_name(f"{now:%Y%m%dT%H%M%S%fZ}.json")
    return path


def build_report(
    ctx: Context,
    results: Sequence[Result],
    *,
    started_at: dt.datetime,
    duration_s: float,
    budget_s: float,
    only: Sequence[str] | None,
) -> dict[str, Any]:
    tally = Counter(r.status for r in results)
    decided = verdict(results)
    return {
        "preflight": 1,
        "verdict": decided,
        "exit_code": 0 if decided == "GO" else 1,
        "started_at": _iso(started_at),
        "duration_s": round(duration_s, 1),
        "budget_s": budget_s,
        "only": list(only) if only else None,
        "data_root": str(ctx.data_root),
        "web_url": ctx.web_url,
        "totals": {s: tally.get(s, 0) for s in (PASS, WARN, FAIL)},
        "checks": [r.to_dict() for r in results],
        "record": None,
        "record_error": None,
    }


def write_record(report: dict[str, Any], data_root: Path, now: dt.datetime) -> None:
    """Store the report as `<data root>/preflight/<ts>.json`; a failure is reported, not raised."""
    path = record_path(data_root, now)
    report["record"] = str(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    except OSError as exc:
        report["record"] = None
        report["record_error"] = _short(exc)


def render_text(report: Mapping[str, Any]) -> str:
    checks = report["checks"]
    width = max((len(c["title"]) for c in checks), default=10)
    lines = [f"Preflight {report['started_at']} (data root {report['data_root']})"]
    if report.get("only"):
        lines.append(f"Partial run: {', '.join(report['only'])}")
    for c in checks:
        lines.append(f"{c['n']:>3}  {c['status']:<4}  {c['title']:<{width}}  {c['summary']}")
        lines.extend(f"{'':>{width + 13}}- {action}" for action in c["actions"])
    t = report["totals"]
    lines.append(
        f"{report['verdict']}: {t[FAIL]} FAIL, {t[WARN]} WARN, {t[PASS]} PASS in {report['duration_s']} s"
        + ("" if report["verdict"] == "GO" else "; do not invite testers")
    )
    if report.get("record"):
        lines.append(f"Record: {report['record']}")
    elif report.get("record_error"):
        lines.append(f"Record not written: {report['record_error']}")
    return "\n".join(lines) + "\n"


def execute(
    ctx: Context,
    *,
    checks: Sequence[Check] | None = None,
    budget_s: float = DEFAULT_BUDGET_S,
    only: Sequence[str] | None = None,
    as_json: bool = False,
    record: bool = True,
    out: IO[str] | None = None,
) -> int:
    """Run, print, store; the exit code (0 GO, 1 NO-GO)."""
    stream = out if out is not None else sys.stdout
    started = time.monotonic()
    results = run_checks(ctx, checks, budget_s=budget_s, only=only)
    report = build_report(
        ctx, results, started_at=ctx.now, duration_s=time.monotonic() - started, budget_s=budget_s, only=only
    )
    if record:
        write_record(report, ctx.data_root, ctx.now)
    stream.write(json.dumps(report, indent=1, default=str) + "\n" if as_json else render_text(report))
    return int(report["exit_code"])


def build_context(args: argparse.Namespace, env: Mapping[str, str]) -> Context:
    from infra.scheduler import jobs

    data_root = Path(args.data_root) if args.data_root else jobs.connector_data_root()
    factory: sessionmaker[Session] | None = None
    if (env.get("DATABASE_URL") or "").strip():
        # The worker's engine: its statement and lock limits (docs/60 §6.4) bound anything a
        # check's own `statement_timeout` does not.
        factory = jobs.build_session_factory()
    dirs = [Path(d) for d in args.handcheck_dir] if args.handcheck_dir else default_handcheck_dirs(data_root)
    return Context(
        now=dt.datetime.now(dt.UTC).replace(microsecond=0),
        data_root=data_root,
        env=env,
        session_factory=factory,
        web_url=args.web_url,
        handcheck_dirs=dirs,
        previous=load_previous(data_root),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m infra.scheduler.bootstrap preflight",
        description="Read-only GO / NO-GO before inviting testers (docs/64 §3).",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--only",
        default=None,
        help=f"comma-separated check ids to run ({', '.join(c.id for c in CHECKS)}); default all",
    )
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_S, help="seconds for the whole run")
    parser.add_argument("--web-url", default=os.environ.get("PREFLIGHT_WEB_URL") or IN_STACK_WEB_URL)
    parser.add_argument(
        "--data-root", default=None, help="default INFRAQUE_DATA_DIR (the connector data root)"
    )
    parser.add_argument(
        "--handcheck-dir", action="append", default=[], help="where to look for <date>-beta (repeatable)"
    )
    parser.add_argument(
        "--no-record", action="store_true", help="do not write <data root>/preflight/<ts>.json"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
    unknown = sorted(set(only or []) - {c.id for c in CHECKS})
    if unknown:
        parser.error(f"unknown check id(s): {', '.join(unknown)}")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ctx = build_context(args, os.environ)
    return execute(ctx, budget_s=args.budget, only=only, as_json=args.json, record=not args.no_record)


if __name__ == "__main__":
    sys.exit(main())
