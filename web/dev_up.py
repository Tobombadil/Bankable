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
    python -m web.dev_up --preview       # also bypasses the publish delay so today's rows show
    python -m web.dev_up --sample 200    # cap each source to 200 rows/lifecycle-state, for fast iteration
    python -m web.dev_up --skip-load     # reuse whatever is already in --db

For an in-process run with no second server at all (what `pytest` uses by default, and a fine
way to run `uvicorn web.app:app --reload` locally), leave `API_BASE_URL` unset and skip this
script -- `web/api_client.py` mounts `services.api.app.app` directly against whatever
`DATABASE_URL` points at.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from sqlalchemy.orm import Session

from services.db.session import get_engine, get_sessionmaker, init_db
from web.data_loading import DEFAULT_DATA_ROOT, DEFAULT_SOURCES_YAML, load_dev_database

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
    parser.add_argument("--api-host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8001)
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8000)
    return parser.parse_args(argv)


def _load_plants_context_layer(session: Session, data_dir: Path) -> None:
    """Task item 7: the existing-plants context layer (docs/00-PLAN.md 2026-09-14/15 owner
    decision), loaded through the plants ingest lane's own `load_plants_parquet(session, path)` --
    this script never invents its own load path for that data, matching `load_dev_database`'s rule
    for every other source. Both the parquet file and the loader function are a separate,
    parallel-built lane (`python -m services.ingest.plants`) that may not exist yet when this
    runs, so both are optional: missing skips silently past the parquet check, and an import
    failure (the module not landed yet) is caught the same way and logged once -- either way this
    prints exactly one line, per the task brief, and never fails the rest of `dev_up`."""
    parquet_path = data_dir / "normalized" / "context" / "us.eia.860m.plants.parquet"
    if not parquet_path.exists():
        log.info("plants context layer: %s not found, skipping", parquet_path)
        return
    try:
        from services.ingest.plants import load_plants_parquet
    except ImportError as exc:
        log.info("plants context layer: services.ingest.plants not available yet (%s), skipping", exc)
        return
    report = load_plants_parquet(session, parquet_path)
    log.info("plants context layer: loaded from %s (%s)", parquet_path, report)


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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite+pysqlite:///{args.db}"

    if not args.skip_load:
        engine = get_engine(database_url)
        init_db(engine)
        session: Session = get_sessionmaker(engine)()
        try:
            report = load_dev_database(
                session,
                data_root=args.data_dir,
                sources_yaml=args.sources_yaml,
                preview=args.preview,
                sample_per_state=args.sample_per_state,
            )
            _load_plants_context_layer(session, args.data_dir)
            session.commit()  # belt-and-braces: correct even if the plants loader also commits
        finally:
            session.close()
        log.info("loaded: %s", report)
    else:
        log.info("--skip-load: reusing %s", args.db)

    env = dict(os.environ)
    env["DATABASE_URL"] = database_url

    api_env = dict(env)
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
