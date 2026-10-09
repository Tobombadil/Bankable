"""`make dev` equivalent for the public site (docs/00-PLAN.md Sprint 2 task item 1): load
`data/normalized/*` into a local SQLite store through `services/ingest`, then start
`services.api.app` and `web.app` together as two real subprocesses talking over HTTP -- the shape
a production deployment uses (`docs/20-architecture.md`: the API is its own deployable).

Loads the full ~11,400-row set across all nine sources by default -- `services/README.md`'s
"Sprint 2 fixes" closed the `services/api/visibility.py` query-time gap that used to force this
script onto a sample, so the real volume is what both interactive use and the pytest suite now run
against. `--sample` remains for a fast local edit-reload loop: `services/ingest/loader.py` upserts
row by row (no bulk path) at ~100 rows/second regardless of query speed, so a full load still costs
a couple of minutes wall clock, which a developer iterating on a template or route doesn't always
want to pay.

    python -m web.dev_up                 # loads the full data set, starts both servers, Ctrl-C stops both
    python -m web.dev_up --preview       # bypasses the publish delay (a no-op since 2026-09-19:
                                        # records carry none -- see web/data_loading.py)
    python -m web.dev_up --sample 200    # cap each source to 200 rows/lifecycle-state, for fast iteration
    python -m web.dev_up --skip-load     # reuse whatever is already in --db
    python -m web.dev_up --no-resolve    # skip cross-source resolution (docs/25 §3.8): overlaps show twice

For an in-process run with no second server at all (what `pytest` uses by default, and a fine
way to run `uvicorn web.app:app --reload` locally), leave `API_BASE_URL` unset and skip this
script -- `web/api_client.py` mounts `services.api.app.app` directly against whatever
`DATABASE_URL` points at.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.context_layers import load_context_layers as load_context_files
from services.ingest.interconnection import link_all_points
from services.match.run import run_matches
from web.data_loading import DEFAULT_DATA_ROOT, DEFAULT_SOURCES_YAML, load_dev_database, load_test_database

log = logging.getLogger("web.dev_up")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "web" / ".data" / "dev.db"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--sources-yaml", type=Path, default=DEFAULT_SOURCES_YAML)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Dev-only: bypass the publish delay so today's connector run is visible immediately "
        "(docs/00-PLAN.md task item 5). Labelled in the UI whenever this is on.",
    )
    parser.add_argument("--skip-load", action="store_true", help="Reuse the database at --db as-is.")
    parser.add_argument(
        "--no-resolve",
        dest="resolve",
        action="store_false",
        help="Skip cross-source resolution after the load, so a record two sources both carry shows "
        "once per source (docs/25 §3.8). Resolution runs by default, as the scheduler's resolve_tick "
        "does in production.",
    )
    parser.add_argument(
        "--sample",
        dest="sample_per_state",
        type=int,
        default=None,
        metavar="N",
        help="Load at most N rows per lifecycle_state/status per source instead of the full "
        "~11,400-row set, for a fast local edit-reload loop. Not a performance workaround any "
        "more (services/README.md 'Sprint 2 fixes' closed that gap) -- the full set is the "
        "default; this only trades real volume for a shorter load time when you want that.",
    )
    parser.add_argument(
        "--context-only",
        action="store_true",
        help="Reload only the context layers (the asset layers, the EIA-860M retirements onto them) "
        "and proposal-opportunity matches into the existing store DATABASE_URL names, from "
        "--data-dir, then exit: no schema, no proposal load, no servers (docs/64).",
    )
    parser.add_argument("--api-host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8001)
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8000)
    return parser.parse_args(argv)


def _link_interconnection_points(session: Session) -> None:
    """Grid interconnection points (owner decision 2026-09-28; docs/21 §3.24). The loader already
    links every proposal it loads (`services/ingest/loader.py::load_dataframe`), so on a fresh
    `dev.db` this pass finds nothing to change; it runs anyway, through the ingest lane's own
    `link_all_points`, so a store whose proposals arrived by any other path (the evaluation
    fixture, an older load) shows its points too, and so the log says how many points the site
    has. One line per source."""
    for result in link_all_points(session):
        log.info("interconnection points: %s", result.summary())


def _resolve_clusters(session: Session, engine: Engine, data_dir: Path) -> None:
    """Cross-source resolution (docs/25 §3.8): the scheduler's own `resolve_tick` body,
    `infra.scheduler.jobs.default_resolve`, over this store and the same data root the load read.
    It resolves organisations, then merges proposal clusters (the 138 Virginia DEQ / EPA ICIS-Air
    facilities that cite one `icis_air:` id, and EIA-860M generators that are also ISO queue
    requests). Reused rather than re-implemented, so the dev store matches what production shows.

    The import is from `infra`, which `infra/importlinter.ini` does not list as a root package; the
    `services.resolve` and `pipeline.resolve` imports stay inside `default_resolve`, so the
    `web-reads-through-the-api` contract is not widened. `default_resolve` opens its own session,
    so this one commits first (releasing SQLite's write lock) and expires everything afterwards:
    the session does not expire on commit, and the steps after this one must see `merged_into_id`."""
    from infra.scheduler.jobs import default_resolve

    session.commit()
    started = time.monotonic()
    report = default_resolve(get_sessionmaker(engine), data_root=data_dir)
    session.expire_all()
    log.info("resolution: %s in %.1fs", report, time.monotonic() - started)


def build_store(
    database_url: str,
    *,
    data_dir: Path,
    sources_yaml: Path,
    preview: bool = False,
    sample_per_state: int | None = None,
    resolve: bool = True,
) -> dict[str, Any]:
    """Create the schema at `database_url` and load everything the site serves, in order: the
    connector output (or the committed fixture), the context layers (the plants first, then the
    EIA-860M retirements onto them), cross-source resolution
    (unless `resolve` is false), interconnection points, matches, then `ANALYZE` last. Resolution
    runs before the point and match passes so both see only surviving proposals. Returns the
    load report."""
    engine = get_engine(database_url)
    init_db(engine)
    session: Session = get_sessionmaker(engine)()
    try:
        report = load_dev_database(
            session,
            data_root=data_dir,
            sources_yaml=sources_yaml,
            preview=preview,
            sample_per_state=sample_per_state,
        )
        load_fixture_if_empty(session, report, sample_per_state=sample_per_state)
        # Plants, then retirements onto them, then the other asset layers with owner shares and
        # features (services/ingest/context_layers.py, the scheduler's `context_load` body).
        load_context_files(session, data_dir, sources_yaml=sources_yaml)
        if resolve:
            _resolve_clusters(session, engine, data_dir)
        else:
            log.info("resolution: skipped (--no-resolve)")
        _link_interconnection_points(session)
        _run_matches(session)
        session.commit()  # belt-and-braces: correct even if either loader above also commits
        refresh_planner_statistics(session)
    finally:
        session.close()
    return report


def load_context_layers(database_url: str, *, data_dir: Path, sources_yaml: Path) -> None:
    """Reload the context layers into an existing store without rebuilding it (`--context-only`):
    the plants, the EIA-860M retirements onto them, the other asset layers with owner shares and
    features (services/ingest/context_layers.py), then proposal-opportunity matches and `ANALYZE`.
    Creates no schema. A deployed store gets the same from the scheduler's monthly `context_build`
    -> `context_load` chain and its `match_tick` (docs/64 §7)."""
    engine = get_engine(database_url)
    session: Session = get_sessionmaker(engine)()
    try:
        load_context_files(session, data_dir, sources_yaml=sources_yaml)
        _run_matches(session)
        session.commit()
        refresh_planner_statistics(session)
    finally:
        session.close()


def refresh_planner_statistics(session: Session) -> None:
    """`ANALYZE` after a bulk load, so the query planner has row counts to choose indexes from.
    Without statistics SQLite picked `ix_proposal_publish_public_at` over the interconnection-point
    index for the point visibility predicate: the M-11 audit went from 3.2 s to about 35 s on the
    dev store (lane H2, 2026-09-29; 0.21 s for that query after `ANALYZE`). Postgres's autovacuum
    does this on its own; running it here as well is harmless."""
    session.execute(text("ANALYZE"))
    session.commit()


def _run_matches(session: Session) -> None:
    """docs/10 US-401: compute proposal <-> opportunity matches over everything just loaded, through
    the matcher's own entry point (`services.match.run.run_matches`, full mode -- a fresh store has
    no watermark, and the rule set may have changed since the last `dev.db`). Writes the `match`
    rows and their `match_added` events; the summary line says how many."""
    report = run_matches(session, full=True)
    log.info("matches: %s", report.summary())


def _wait_for(url: str, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1.0)  # noqa: S310 -- localhost only
            return
        except (urllib.error.URLError, ConnectionError) as exc:
            last_error = exc
            time.sleep(0.3)
    raise RuntimeError(f"{url} did not come up in time: {last_error}")


def _columns_missing_from(engine: Engine) -> list[str]:
    """`table.column` for every ORM column the database does not have. Empty when the schema is
    current. Only the columns are compared -- types, indexes and constraints are the migrations'
    business, not a dev runner's."""
    import services.db.models  # noqa: F401 -- registers tables on Base.metadata
    from services.db.base import Base

    inspector = sa_inspect(engine)
    present = set(inspector.get_table_names())
    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in present:
            continue  # a missing table is `create_all`'s job and it does do that one
        have = {c["name"] for c in inspector.get_columns(table.name)}
        missing.extend(f"{table.name}.{c.name}" for c in table.columns if c.name not in have)
    return missing


#: Rows per lifecycle state taken from the committed fixture when no connector output exists: enough
#: for every page type to render real records, small enough to load in seconds on a CI runner.
FIXTURE_SAMPLE_PER_STATE = 60


def load_fixture_if_empty(session: Session, report: dict[str, Any], *, sample_per_state: int | None) -> bool:
    """A fresh checkout has no `data/normalized/*` (git-ignored; only connector runs create it), so the
    real load reports every source `missing` and the site serves empty pages. That is what CI's
    accessibility job scanned until 2026-09-27, when its new detail-page guard refused to call an
    empty scan a pass. Fall back, as `web/test_e2e.py` already does, to the committed entity-resolution
    fixture (`data/eval/normalized.parquet`, docs/22): real rows and geometry, no network. Returns
    whether the fixture was loaded; says so in the log, since fixture rows are not the live register."""
    loaded = sum(1 for value in (report.get("sources") or {}).values() if value == "loaded")
    if loaded:
        return False
    log.warning(
        "no connector output under data/normalized: loading the committed evaluation fixture instead "
        "(data/eval/normalized.parquet). The site is serving fixture rows, not the live register."
    )
    load_test_database(
        session,
        sample_per_state=sample_per_state or FIXTURE_SAMPLE_PER_STATE,
        include_opportunities=False,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    if args.context_only:
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            log.error("--context-only loads into the store DATABASE_URL names; it is not set")
            return 2
        load_context_layers(database_url, data_dir=args.data_dir, sources_yaml=args.sources_yaml)
        return 0
    args.db.parent.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite+pysqlite:///{args.db}"

    if not args.skip_load:
        # Start from an empty file. `init_db` is `metadata.create_all`, which creates missing
        # *tables* and never adds a column to a table that already exists, so a `dev.db` left
        # over from before a migration keeps its old schema for ever and every page that reads
        # the new column dies with "no such column". That is what happened to a database written
        # before migration 0015 added `organization.parent_org_id`: the site started, then threw
        # on the proposals query. Since a run without `--skip-load` reloads every row anyway,
        # keeping the old file buys nothing but that failure mode.
        if args.db.exists():
            log.info("rebuilding %s from scratch (a full load follows)", args.db)
            args.db.unlink()
        report = build_store(
            database_url,
            data_dir=args.data_dir,
            sources_yaml=args.sources_yaml,
            preview=args.preview,
            sample_per_state=args.sample_per_state,
            resolve=args.resolve,
        )
        log.info("loaded: %s", report)
    else:
        # Reuse is the whole point of --skip-load, so the stale-schema case cannot be fixed by
        # rebuilding here. Say so plainly instead of letting it surface as a SQL error from
        # whichever page happens to read the missing column first.
        engine = get_engine(database_url)
        missing = _columns_missing_from(engine)
        if missing:
            log.error(
                "--skip-load: %s predates the current models and is missing %s. "
                "Re-run without --skip-load to rebuild it.",
                args.db,
                ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else ""),
            )
            return 1
        log.info("--skip-load: reusing %s", args.db)

    env = dict(os.environ)
    env["DATABASE_URL"] = database_url

    api_env = dict(env)
    internal_token = secrets.token_urlsafe(24)  # per-run; the site's calls bypass the anonymous bucket
    api_env["API_INTERNAL_TOKEN"] = internal_token
    api_proc = subprocess.Popen(  # noqa: S603 -- fixed argv (sys.executable + literal strings)
        [
            sys.executable,
            "-m",
            "uvicorn",
            "services.api.app:app",
            "--host",
            args.api_host,
            "--port",
            str(args.api_port),
        ],
        cwd=REPO_ROOT,
        env=api_env,
    )

    web_env = dict(env)
    web_env["API_BASE_URL"] = f"http://{args.api_host}:{args.api_port}"
    web_env["API_INTERNAL_TOKEN"] = internal_token
    if args.preview:
        web_env["WEB_DEV_PREVIEW"] = "1"
    web_proc = subprocess.Popen(  # noqa: S603 -- fixed argv (sys.executable + literal strings)
        [
            sys.executable,
            "-m",
            "uvicorn",
            "web.app:app",
            "--host",
            args.web_host,
            "--port",
            str(args.web_port),
        ],
        cwd=REPO_ROOT,
        env=web_env,
    )

    try:
        _wait_for(f"http://{args.api_host}:{args.api_port}/v1/health")
        _wait_for(f"http://{args.web_host}:{args.web_port}/health")
        log.info("API:  http://%s:%s", args.api_host, args.api_port)
        log.info("Site: http://%s:%s", args.web_host, args.web_port)
        log.info("Ctrl-C to stop both.")
        while True:
            api_status = api_proc.poll()
            web_status = web_proc.poll()
            if api_status is not None or web_status is not None:
                log.info("a server exited (api=%s, web=%s)", api_status, web_status)
                return 1
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0
    finally:
        for proc in (api_proc, web_proc):
            proc.terminate()
        for proc in (api_proc, web_proc):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
