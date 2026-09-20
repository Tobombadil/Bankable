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
import secrets
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


def _load_ownership(session: Session, data_dir: Path) -> None:
    """ADR 0008 task item 4: the EIA-860 Schedule 4 ownership-share parquet, loaded through the
    ownership ingest lane's own `load_owner_shares_parquet(session, path)` -- same rule as
    `_load_plants_context_layer` above: this script never invents its own load path, both the
    parquet and the loader module are optional (a parallel-built lane that may not exist yet when
    this runs), and either kind of "not ready" is one log line, never a failure of the rest of
    `dev_up`."""
    parquet_path = data_dir / "normalized" / "context" / "us.eia.860.owners.parquet"
    if not parquet_path.exists():
        log.info("asset ownership: %s not found, skipping", parquet_path)
        return
    try:
        from services.ingest.ownership import load_owner_shares_parquet  # type: ignore[import-not-found]
    except ImportError as exc:
        log.info("asset ownership: services.ingest.ownership not available yet (%s), skipping", exc)
        return
    report = load_owner_shares_parquet(session, parquet_path)
    # `as_report()` where it exists: its unmatched-plant list is a sample, so this stays one line.
    summary = report.as_report() if hasattr(report, "as_report") else report
    log.info("asset ownership: loaded from %s (%s)", parquet_path, summary)


#: `data/normalized/context/<file>` -> `asset_type` for the midstream and fuels context layers
#: (owner option (a), 2026-09-19; file names and types as the data lanes landed them, coordinator
#: note 2026-09-19). Two files may feed one type (ethanol from EIA capacity and the Atlas layer;
#: RNG from EPA LMOP and AgSTAR); the `*.proposals.parquet` siblings of the EPA files hold planned
#: rows that are proposals, not assets, and are not listed here. A file that is not there is one
#: log line, never a failure.
_CONTEXT_ASSET_FILES: tuple[tuple[str, str], ...] = (
    ("us.eia.atlas.gas_pipelines.parquet", "gas_pipeline"),
    ("us.eia.atlas.gas_processing_plants.parquet", "gas_processing_plant"),
    ("us.eia.atlas.gas_storage.parquet", "gas_storage"),
    ("us.eia.atlas.lng_terminals.parquet", "lng_terminal"),
    ("us.eia.ethanol_capacity.parquet", "ethanol_plant"),
    ("us.eia.atlas.ethanol_plants.parquet", "ethanol_plant"),
    ("us.epa.lmop.parquet", "rng_project"),
    ("us.epa.agstar.parquet", "rng_project"),
)


def _apply_context_features(session: Session, data_root: Path) -> None:
    """The enrichment lane's `services.ingest.enrich.apply_context_features(session, data_root)`
    (derived asset features such as nearby-asset and ownership context), called exactly once, after
    every asset, edge and owner-share load and before the curated parent links are applied. The
    module is a separate lane that may not exist yet when this runs, and its parquet inputs may be
    absent, so a missing module, a missing file or an unwired feature is one log line, never a
    failure of the rest of `dev_up`."""
    try:
        from services.ingest.enrich import apply_context_features  # type: ignore[import-not-found]
    except ImportError as exc:
        log.info("context features: services.ingest.enrich not available yet (%s), skipping", exc)
        return
    try:
        report = apply_context_features(session, data_root)
    except FileNotFoundError as exc:
        log.info("context features: input not found (%s), skipping", exc)
        return
    log.info("context features: applied (%s)", report)


def _load_context_asset_layers(session: Session, data_dir: Path) -> None:
    """Load the midstream/fuels asset layers through the ingest lanes' own loaders -- same rule as
    `_load_plants_context_layer`: this script never invents a load path. Per file, in order:
    `services.ingest.assets.load_assets_parquet(session, path, asset_type)` (the rows) then
    `services.ingest.midstream.load_operator_edges_parquet(session, path, asset_type)` (the
    `asset_owner` operator edges); once every file is done, `_load_ownership` writes the EIA-860
    Schedule 4 owner shares (they join onto the `power_plant` rows `_load_plants_context_layer`
    loaded first, so this runs after the plants and after every other asset and edge load), then
    `_apply_context_features` runs the enrichment lane once, and last `services.ingest.midstream.
    load_parents(session)` applies the curated parent links (`data/vendored/organizations/
    parents.yaml`) over every organisation the loads above created. Any file or module that is
    not there yet, or an `asset_type` the loader has not wired, is one log line with the reason,
    never a failure of the rest of `dev_up`; every successful step logs its counts."""
    context_dir = data_dir / "normalized" / "context"
    try:
        from services.ingest.assets import UnsupportedAssetTypeError, load_assets_parquet
    except ImportError as exc:
        log.info("context asset layers: services.ingest.assets not available yet (%s), skipping", exc)
        return
    try:
        from services.ingest.midstream import load_operator_edges_parquet, load_parents
    except ImportError as exc:
        log.info("context asset layers: services.ingest.midstream not available yet (%s); rows only", exc)
        load_operator_edges_parquet = None  # type: ignore[assignment]
        load_parents = None  # type: ignore[assignment]

    loaded = 0
    for file_name, asset_type in _CONTEXT_ASSET_FILES:
        parquet_path = context_dir / file_name
        if not parquet_path.exists():
            log.info("context asset layers: %s not found, skipping", parquet_path)
            continue
        try:
            report = load_assets_parquet(session, parquet_path, asset_type)
        except UnsupportedAssetTypeError as exc:
            log.info("context asset layers: %s skipped (%s)", file_name, exc)
            continue
        loaded += 1
        log.info("context asset layers: loaded %s rows from %s (%s)", asset_type, file_name, report)
        if load_operator_edges_parquet is not None:
            edges = load_operator_edges_parquet(session, parquet_path, asset_type)
            log.info("context asset layers: operator edges from %s (%s)", file_name, edges)
    _load_ownership(session, data_dir)
    _apply_context_features(session, data_dir)
    if loaded and load_parents is not None:
        try:
            parents = load_parents(session)
        except FileNotFoundError as exc:
            log.info("context asset layers: curated parents file not found (%s), skipping", exc)
        else:
            log.info("context asset layers: curated parents applied (%s)", parents)
    if not loaded:
        log.info("context asset layers: no midstream/fuels parquet under %s yet, skipping", context_dir)


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
            _load_context_asset_layers(session, args.data_dir)  # includes owner shares + features
            session.commit()  # belt-and-braces: correct even if either loader above also commits
        finally:
            session.close()
        log.info("loaded: %s", report)
    else:
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
