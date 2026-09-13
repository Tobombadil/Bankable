"""`make dev` equivalent for the public site (docs/00-PLAN.md Sprint 2 task item 1): load
`data/normalized/*` into a local SQLite store through `services/ingest`, then start
`services.api.app` and `web.app` together as two real subprocesses talking over HTTP -- the shape
a production deployment uses (`docs/20-architecture.md`: the API is its own deployable).

    python -m web.dev_up                 # loads data, starts both servers, Ctrl-C stops both
    python -m web.dev_up --preview       # also bypasses the publish delay so today's rows show
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
        "--sample-per-state",
        type=int,
        default=None,
        help="Load at most this many rows per lifecycle_state/status per source instead of the "
        "full ~10,400-row set. Workaround for the services/api/visibility.py performance gap "
        "documented in web/README.md 'Missing from the API' (measured 30-75s/page unsampled).",
    )
    parser.add_argument("--api-host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8001)
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8000)
    return parser.parse_args(argv)


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
