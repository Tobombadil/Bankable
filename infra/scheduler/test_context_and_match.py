"""The two passes a deployed store had no scheduled path for until 2026-10-09 (docs/64 §7):
proposal-opportunity matches (`match_tick`, chained after every enrich pass) and the context layers
(`tick_context` -> `context_build` -> `context_load`, monthly). No Postgres: the SQLite session
factory, fake builder processes and fake task deferrers, as in `test_loop.py`."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import subprocess
from typing import Any

import pytest

from infra.scheduler import jobs
from services.db.session import get_engine, get_sessionmaker, init_db

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _Factory:
    def __init__(self) -> None:
        engine = get_engine("sqlite+pysqlite:///:memory:")
        init_db(engine)
        self.sessionmaker = get_sessionmaker(engine)

    def __call__(self) -> Any:
        return self.sessionmaker()


class _FakeTask:
    def __init__(self, name: str, deferred: list[str]) -> None:
        self.name, self.deferred = name, deferred

    def defer(self, **kwargs: Any) -> int:
        self.deferred.append(self.name)
        return len(self.deferred)


@pytest.fixture()
def scheduler_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@localhost:5432/dummy")  # never connected
    # The store-wide passes take their advisory lock through the worker's session factory
    # (`jobs.run_store_pass`): SQLite here, where the lock is a no-op.
    monkeypatch.setattr(jobs, "build_session_factory", _Factory)
    import infra.scheduler.app as app_module

    return app_module


def test_enrich_chains_to_match_and_build_chains_to_load(
    monkeypatch: pytest.MonkeyPatch, scheduler_app: Any
) -> None:
    deferred: list[str] = []
    real_enrich, real_build = scheduler_app.enrich_tick, scheduler_app.context_build
    monkeypatch.setattr(scheduler_app, "match_tick", _FakeTask("match_tick", deferred))
    monkeypatch.setattr(scheduler_app, "context_load", _FakeTask("context_load", deferred))
    monkeypatch.setattr(jobs, "enrich_tick_job", lambda: {"ok": True})
    monkeypatch.setattr(jobs, "context_build_job", lambda: {"ok": 13})
    real_enrich.func()  # the task bodies, without a worker
    real_build.func()
    assert deferred == ["match_tick", "context_load"]


def test_the_new_tasks_run_on_queues_the_worker_consumes_under_the_store_wide_lock(
    scheduler_app: Any,
) -> None:
    import yaml

    tasks = scheduler_app.app.tasks
    worker = yaml.safe_load((ROOT / "infra" / "compose" / "docker-compose.yml").read_text())["services"][
        "worker"
    ]
    consumed = set(worker["command"][worker["command"].index("--queues") + 1].split(","))
    for name, queue, lock in (
        ("match_tick", "resolve", "resolve"),
        ("context_load", "resolve", "resolve"),
        ("context_build", "fetch", "context_build"),
    ):
        task = tasks[name]
        assert (task.queue, task.lock, task.queueing_lock) == (queue, lock, name)
        assert task.queue in consumed, (name, task.queue)
    assert tasks["context_load"].retry_strategy is scheduler_app.LOAD_RETRY
    periodic = scheduler_app.app.periodic_registry.periodic_tasks
    assert ("tick_context", "tick:context") in periodic
    assert periodic[("tick_context", "tick:context")].cron == scheduler_app.CONTEXT_CRON == "43 2 4 * *"


def test_match_tick_runs_the_matcher_and_reports_counts_only() -> None:
    """A first run on a store has no watermark, so the matcher runs in full mode; the report is the
    matcher's counts without the new rows' ids."""
    report = jobs.match_tick_job(_session_factory=_Factory())
    assert report["mode"] == "full" and report["added"] == 0 and report["active_total"] == 0
    assert "added_ids" not in report
    # The run moved the watermark: the next one is incremental.
    factory = _Factory()
    jobs.match_tick_job(_session_factory=factory)
    assert jobs.match_tick_job(_session_factory=factory)["mode"] == "incremental"


def test_context_build_runs_every_builder_and_survives_a_failure_and_a_timeout() -> None:
    calls: list[tuple[list[str], Any, int]] = []

    def fake_run(
        cmd: list[str], *, cwd: Any, timeout: int, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, cwd, timeout))
        module = cmd[2]
        if module.endswith("eia_owners"):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="FileNotFoundError: no snapshot")
        if module.endswith("phmsa"):
            raise subprocess.TimeoutExpired(cmd, timeout, stderr=b"still downloading")
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    report = jobs.context_build_job(_run=fake_run, timeout_s=7)
    assert [c[0][1:] for c in calls] == [["-m", *argv] for argv in jobs.CONTEXT_BUILDERS]
    assert {c[1] for c in calls} == {jobs.ROOT} and {c[2] for c in calls} == {7}
    assert report["builders"] == len(jobs.CONTEXT_BUILDERS)
    assert report["ok"] == len(jobs.CONTEXT_BUILDERS) - 2
    assert report["failed"] == ["eia_owners: exit 1", "phmsa: timeout"]
    assert report["not_published"] == []


# ------------------------------------------------------------- the site finder's pulls (lane sf-golive)
PULLS = (
    "pipeline.context.osm_power",
    "pipeline.context.fema_nfhl",
    "pipeline.context.prospect.pull",
    "pipeline.context.txgio_parcels",
)
GRID_URL = "https://tiles.example.test/grid/osm-power"
GRID_TS = "2026-10-21T20:21:02Z"
PRIVATE_BUCKET = "beta-object-storage-production"
PARCELS_LATEST = "data/normalized/context/us.tx.txgio.parcels/latest.json"


class _Resp:
    def __init__(self, status: int, content: bytes = b"") -> None:
        self.status_code, self.content = status, content

    def iter_content(self, chunk_size: int = 1) -> Any:
        yield from (self.content[i : i + chunk_size] for i in range(0, len(self.content), chunk_size))

    def close(self) -> None:
        pass


class _Tiles:
    """The public tiles host the grid build publishes to: a dict of URL -> bytes, else 404."""

    def __init__(self, served: dict[str, bytes]) -> None:
        self.served = served
        self.requested: list[str] = []

    def get(self, url: str, **_: Any) -> _Resp:
        self.requested.append(url)
        return _Resp(200, self.served[url]) if url in self.served else _Resp(404)


class _Bucket:
    """The private bucket, holding `objects` (none: before flood-layer.yml, prospect-layers.yml and
    parcels-layer.yml have run). A missing key is answered the way botocore does (a 404 `response`)
    and the way `S3Backend.get` does; the calls are the ones each pull makes (the flood pull's store,
    the prospect pull's HEAD, the parcels pull's GET and download)."""

    class Missing(Exception):
        def __init__(self, key: str) -> None:
            super().__init__(key)
            self.response = {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}

    class Body:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def read(self) -> bytes:
            return self.data

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = objects or {}
        self.asked: list[str] = []

    def get(self, key: str) -> bytes:
        self.asked.append(key)
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3's names
        assert Bucket == PRIVATE_BUCKET
        self.asked.append(Key)
        if Key not in self.objects:
            raise self.Missing(Key)
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3's names
        assert Bucket == PRIVATE_BUCKET
        self.asked.append(Key)
        if Key not in self.objects:
            raise self.Missing(Key)
        return {"Body": self.Body(self.objects[Key])}

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        assert bucket == PRIVATE_BUCKET
        self.asked.append(key)
        if key not in self.objects:
            raise self.Missing(key)
        pathlib.Path(filename).write_bytes(self.objects[key])


def _published_grid(tmp_path: pathlib.Path) -> dict[str, bytes]:
    """A national grid build with one line and one substation in Texas and one of each in Oklahoma,
    as `infra/scripts/build_grid_layers.sh` publishes it under the tiles host."""
    from pipeline.context import osm_power as op

    def line(osm_id: int, region: str, coords: list[tuple[float, float]]) -> dict[str, Any]:
        record = op.LineRecord("way", osm_id, {"power": "line", "voltage": "138000"}, coords)
        return op.line_row(record, region, GRID_TS)

    def substation(osm_id: int, region: str, point: tuple[float, float]) -> dict[str, Any]:
        record = op.SubstationRecord("node", osm_id, {"power": "substation"}, point, None)
        return op.substation_row(record, region, GRID_TS)

    geo = tmp_path / "published"
    files = {
        op.GEO_LINES_NAME: op.write_geoparquet(
            [
                line(101, "US-TX", [(-97.0, 30.0), (-96.99, 30.0)]),
                line(301, "US-OK", [(-97.5, 35.0), (-97.4, 35.1)]),
            ],
            geo / op.GEO_LINES_NAME,
            kind="lines",
        ),
        op.GEO_SUBSTATIONS_NAME: op.write_geoparquet(
            [substation(201, "US-TX", (-97.0, 30.0)), substation(302, "US-OK", (-97.5, 35.0))],
            geo / op.GEO_SUBSTATIONS_NAME,
            kind="substations",
        ),
    }
    version = "v1/20261021T202102Z-20261022T061500Z"
    manifest = {
        "version": version,
        "regions": [{"region": "US-OK", "timestamp": GRID_TS}, {"region": "US-TX", "timestamp": GRID_TS}],
        "files": files,
    }
    served = {f"{GRID_URL}/latest.json": json.dumps(manifest).encode()}
    for name in files:
        served[f"{GRID_URL}/{version}/{name}"] = (geo / name).read_bytes()
    return served


def _server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, tiles: _Tiles, bucket: _Bucket
) -> pathlib.Path:
    """The worker's environment as the image and the beta's env file set it (INFRAQUE_DATA_DIR,
    GRID_LAYERS_REGIONS=US-TX, PARCELS_COUNTIES=all, the R2 names; no PARCELS_DATA_DIR), with the
    network replaced: the tiles host by `tiles`, the private bucket by `bucket`. Returns the data root."""
    import pipeline.connectors.http as http
    import services.context_sample.sync as sync_mod
    from pipeline.context import fema_nfhl, osm_power, txgio_parcels

    data_root = tmp_path / "data"
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(data_root))
    monkeypatch.delenv("PARCELS_DATA_DIR", raising=False)
    monkeypatch.setenv("GRID_LAYERS_REGIONS", "US-TX")
    monkeypatch.setenv("PARCELS_COUNTIES", "all")
    monkeypatch.setenv("GRID_LAYERS_URL", GRID_URL)
    monkeypatch.setenv("PROSPECT_DATA_DIR", str(data_root / "normalized" / "prospect"))
    monkeypatch.setattr(osm_power, "DATA_DIR", data_root)
    monkeypatch.setattr(osm_power, "NORMALIZED_DIR", data_root / "normalized" / "context")
    monkeypatch.setattr(osm_power, "_licence_text", lambda: "ODbL 1.0 (test)")
    monkeypatch.setattr(http, "PoliteSession", lambda *a, **k: tiles)
    monkeypatch.setattr(fema_nfhl, "DATA_DIR", data_root)
    monkeypatch.setattr(fema_nfhl, "store_from_env", lambda: bucket)
    monkeypatch.setattr(sync_mod, "client_from_env", lambda: (bucket, PRIVATE_BUCKET))
    monkeypatch.setattr(txgio_parcels, "object_store_client", lambda: (bucket, PRIVATE_BUCKET))
    return data_root


def _run_pulls_in_process() -> tuple[Any, list[list[str]]]:
    """`subprocess.run` for `context_build_job`: the four pulls run their real CLI in this process
    (the environment is the one `subprocess.run` would hand the child: it passes no `env`), every
    other builder answers ok."""
    from pipeline.context import fema_nfhl, osm_power, txgio_parcels
    from pipeline.context.prospect import pull as prospect_pull

    mains: dict[str, Any] = {
        "pipeline.context.osm_power": osm_power.main,
        "pipeline.context.fema_nfhl": fema_nfhl.main,
        "pipeline.context.prospect.pull": prospect_pull.main,
        "pipeline.context.txgio_parcels": txgio_parcels.main,
    }
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str], *, cwd: Any, timeout: int, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        assert "env" not in kwargs, "the builder inherits the worker's environment"
        calls.append(cmd[2:])
        main = mains.get(cmd[2])
        if main is None:
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = main(cmd[3:])
        except SystemExit as exc:
            return subprocess.CompletedProcess(
                cmd, 1 if exc.code else 0, stdout=out.getvalue(), stderr=str(exc)
            )
        return subprocess.CompletedProcess(cmd, int(code or 0), stdout=out.getvalue(), stderr="")

    return fake_run, calls


def test_the_pulls_run_last_in_the_build_so_the_load_and_the_site_finder_follow_them() -> None:
    """Order (docs/25 §2.7 limit 1): every builder, then the four pulls, inside `context_build`, which
    chains to `context_load` and that to `site_finder_refresh` (tests above and below). The parcels
    pull (lane parcels-wire, 2026-10-23) comes after the prospect pull."""
    assert tuple(argv[0] for argv in jobs.CONTEXT_BUILDERS[-4:]) == PULLS
    assert [argv[1:] for argv in jobs.CONTEXT_BUILDERS[-4:]] == [("pull",), ("pull",), (), ("pull",)]
    from pipeline.context import fema_nfhl, osm_power, txgio_parcels
    from pipeline.context.prospect import pull as prospect_pull

    assert {
        osm_power.NOT_PUBLISHED,
        fema_nfhl.NOT_PUBLISHED,
        prospect_pull.NOT_PUBLISHED,
        txgio_parcels.NOT_PUBLISHED,
    } == {jobs.NOT_PUBLISHED}


def test_context_build_pulls_the_grid_for_the_configured_regions_and_an_unpublished_layer_fails_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The OpenStreetMap build is published, the flood, prospect and parcels builds are not: the OSM
    pull writes the frames for GRID_LAYERS_REGIONS (Texas only, though the build is national), the
    other three report "not published", and nothing counts as failed."""
    import pandas as pd

    tiles = _Tiles(_published_grid(tmp_path))
    bucket = _Bucket()
    data_root = _server(monkeypatch, tmp_path, tiles, bucket)
    fake_run, calls = _run_pulls_in_process()
    report = jobs.context_build_job(_run=fake_run, timeout_s=7)

    assert [c[0] for c in calls[-4:]] == list(PULLS)
    assert report["failed"] == []
    assert report["not_published"] == ["fema_nfhl pull", "prospect.pull", "txgio_parcels pull"]
    assert report["ok"] == len(jobs.CONTEXT_BUILDERS) - 3
    context = data_root / "normalized" / "context"
    lines = pd.read_parquet(context / "us.osm.power.lines.parquet")
    substations = pd.read_parquet(context / "us.osm.power.substations.parquet")
    assert list(lines["source_asset_id"]) == ["way/101"] and list(substations["source_asset_id"]) == [
        "node/201"
    ]
    held = json.loads((context / "us.osm.power.manifest.json").read_text())
    assert held["regions_loaded"] == ["US-TX"] and [r["region"] for r in held["regions"]] == ["US-TX"]
    assert bucket.asked == [
        "data/normalized/context/us.fema.nfhl/latest.json",
        "prospect/current/texas.json",  # a Texas server never falls back to the national build
        PARCELS_LATEST,  # the private bucket: per-parcel files are never on the tiles host
    ]
    assert not any("parcels" in url for url in tiles.requested)
    assert not (data_root / "normalized" / "prospect").exists()
    assert not (data_root / "normalized" / "context" / "us.tx.txgio.parcels").exists()


def test_before_any_build_is_published_every_pull_reports_it_and_the_build_still_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    tiles = _Tiles({})
    data_root = _server(monkeypatch, tmp_path, tiles, _Bucket())
    fake_run, _calls = _run_pulls_in_process()
    report = jobs.context_build_job(_run=fake_run, timeout_s=7)
    assert report["failed"] == []
    assert report["not_published"] == [
        "osm_power pull",
        "fema_nfhl pull",
        "prospect.pull",
        "txgio_parcels pull",
    ]
    assert tiles.requested == [f"{GRID_URL}/latest.json"]
    assert not (data_root / "normalized" / "context").exists()


def test_a_pull_without_a_regions_setting_fails_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The pulls refuse an unset GRID_LAYERS_REGIONS (never read as national); the deploy always
    renders it (infra/ci/beta.py), so this is a misconfigured server, reported as a failed builder.
    The parcels pull reads PARCELS_COUNTIES instead and goes on (here: nothing published)."""
    _server(monkeypatch, tmp_path, _Tiles({}), _Bucket())
    monkeypatch.delenv("GRID_LAYERS_REGIONS")
    fake_run, _calls = _run_pulls_in_process()
    report = jobs.context_build_job(_run=fake_run, timeout_s=7)
    assert report["failed"] == ["osm_power pull: exit 1", "fema_nfhl pull: exit 1", "prospect.pull: exit 1"]
    assert report["not_published"] == ["txgio_parcels pull"]
    assert report["ok"] == len(jobs.CONTEXT_BUILDERS) - 4


def test_a_parcels_pull_without_a_counties_setting_fails_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The parcels pull refuses an unset PARCELS_COUNTIES (a server says what it serves; never read
    as `all`); the deploy always renders it (infra/ci/beta.py, `all` by default), so this is a
    misconfigured server: one failed builder, and the other pulls run on."""
    _server(monkeypatch, tmp_path, _Tiles({}), _Bucket())
    monkeypatch.delenv("PARCELS_COUNTIES")
    fake_run, _calls = _run_pulls_in_process()
    report = jobs.context_build_job(_run=fake_run, timeout_s=7)
    assert report["failed"] == ["txgio_parcels pull: exit 1"]
    assert report["not_published"] == ["osm_power pull", "fema_nfhl pull", "prospect.pull"]


def _published_parcels(tmp_path: pathlib.Path) -> dict[str, bytes]:
    """The private bucket after parcels-layer.yml published the site finder fixture's county (48999,
    beside line A; `services.site_finder.testing.write_parcels`): `latest.json` and the county's files
    under the version that built them, as `infra/scripts/build_parcels_layer.sh` leaves them."""
    from pipeline.context import txgio_parcels as tp
    from services.site_finder.testing import write_parcels

    built = tmp_path / "built"
    current = write_parcels(built)
    entry = current["counties"]["48999"]
    files = {kind: {k: v for k, v in spec.items() if k != "local"} for kind, spec in entry["files"].items()}
    latest = {**current, "counties": {"48999": {**entry, "files": files}}}
    objects = {PARCELS_LATEST: json.dumps(latest).encode()}
    for spec in files.values():
        key = f"{tp.OBJECT_PREFIX}/{current['version']}/{spec['path']}"
        objects[key] = (
            built / "normalized" / "context" / tp.SOURCE_ID / current["version"] / spec["path"]
        ).read_bytes()
    return objects


def test_the_site_finder_run_after_the_build_measures_the_parcels_the_pull_wrote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Lane parcels-wire (2026-10-23): `context_build` pulls the published parcels into the worker's
    data root, and the `site_finder_refresh` that follows the load measures the segments beside the
    county from those files: the job hands `compute.refresh` the connector data root, whose
    `load_parcels` opens `parcels_dir(data_root)`, the directory the pull wrote. Nothing else puts
    parcels there: the world's own copy is removed first."""
    import shutil

    import sqlalchemy as sa

    from services.context_sample.parcels import CURRENT_NAME, parcels_dir
    from services.db.models import SiteCandidate, SiteFinderRun
    from services.site_finder.compute import refresh
    from services.site_finder.testing import PARCELS_VERSION, FakeSampler, seed

    bucket = _Bucket(_published_parcels(tmp_path))
    data_root = _server(monkeypatch, tmp_path, _Tiles({}), bucket)
    engine = get_engine(f"sqlite+pysqlite:///{tmp_path / 'sf.db'}")
    init_db(engine)
    factory = get_sessionmaker(engine)
    with factory() as db:
        world = seed(db, data_root)
    shutil.rmtree(parcels_dir(data_root))

    fake_run, _calls = _run_pulls_in_process()
    report = jobs.context_build_job(_run=fake_run, timeout_s=7)
    assert report["failed"] == [] and "txgio_parcels pull" not in report["not_published"]
    assert (parcels_dir(data_root) / CURRENT_NAME).exists()
    assert parcels_dir(data_root) == parcels_dir(jobs.connector_data_root())

    roots: list[pathlib.Path] = []

    def run(session_factory: Any, root: pathlib.Path) -> dict[str, Any]:
        roots.append(root)
        return dict(refresh(session_factory, root, sampler=FakeSampler()))

    result = jobs.site_finder_refresh_job(
        run,
        _session_factory=factory,
        _counties=lambda root: {"fetched": False},
        _memory_limit=lambda: None,
    )
    assert roots == [data_root] and result["status"] == "ok"
    with factory() as db:
        served = db.scalars(sa.select(SiteFinderRun).where(SiteFinderRun.status == "ok")).one()
        contexts = [
            (c or {}).get("parcels")
            for c in db.scalars(
                sa.select(SiteCandidate.context)
                .where(SiteCandidate.line_asset_id == world.line_a.id)
                .order_by(SiteCandidate.segment_index)
            )
        ]
    assert served.inputs["parcels"]["loaded"] is True
    assert served.inputs["parcels"]["version"] == PARCELS_VERSION
    assert contexts and all(c is not None and c["cov"] == "ok" and c["n"][-1] > 0 for c in contexts)
    engine.dispose()


def test_the_pull_the_job_and_the_api_name_one_parcels_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The pull's default directory, the site finder job's (`load_parcels(connector_data_root())`)
    and the API's (`default_layer(services.api.site_finder._data_root())`) are one, with and without
    PARCELS_DATA_DIR. On the single host the api container mounts the worker's `connector_data`
    volume read-only at the same INFRAQUE_DATA_DIR (infra/test_compose.py)."""
    from services.api.site_finder import _data_root as api_data_root
    from services.context_sample.parcels import parcels_dir

    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(tmp_path / "data"))
    for explicit in (None, str(tmp_path / "parcels")):
        if explicit is None:
            monkeypatch.delenv("PARCELS_DATA_DIR", raising=False)
        else:
            monkeypatch.setenv("PARCELS_DATA_DIR", explicit)
        assert parcels_dir() == parcels_dir(jobs.connector_data_root()) == parcels_dir(api_data_root())
    monkeypatch.delenv("PARCELS_DATA_DIR")
    assert parcels_dir() == tmp_path / "data" / "normalized" / "context" / "us.tx.txgio.parcels"


def test_builder_names_and_summaries() -> None:
    assert jobs.builder_name(("pipeline.context.eia_atlas", "--layer", "all")) == "eia_atlas"
    assert jobs.builder_name(("pipeline.context.osm_power", "pull")) == "osm_power pull"
    assert jobs.builder_name(("pipeline.context.prospect.pull",)) == "prospect.pull"
    assert jobs.builder_name(("pipeline.context.txgio_parcels", "pull")) == "txgio_parcels pull"
    assert jobs.builder_summary('progress\n{"status": "pulled"}\n{"status": "not_published"}\n') == {
        "status": "not_published"
    }
    assert jobs.builder_summary("no json here\n{broken") is None and jobs.builder_summary(None) is None


def test_the_builders_are_the_ones_whose_files_the_load_reads() -> None:
    """Every file `context_load` reads directly has a builder in the monthly run, except GLEIF's
    parent links (an operator run, `jobs.CONTEXT_BUILDERS` comment); and every builder module
    exists, so a rename cannot leave the tick running a module that is gone."""
    import importlib.util

    from services.ingest import context_layers

    modules = {argv[0] for argv in jobs.CONTEXT_BUILDERS}
    for module in modules:
        assert importlib.util.find_spec(module) is not None, module
    builder_for = {
        "us.eia.860m.plants.parquet": "eia_plants",
        "us.eia.atlas.gas_pipelines.parquet": "eia_atlas",
        "us.eia.atlas.gas_processing_plants.parquet": "eia_atlas",
        "us.eia.atlas.gas_storage.parquet": "eia_atlas",
        "us.eia.atlas.lng_terminals.parquet": "eia_atlas",
        "us.epa.lmop.parquet": "lmop",
        "us.epa.agstar.parquet": "agstar",
        "us.lbnl.ferc_hifld_transmission_lines.parquet": "lbnl_transmission",
        "us.eia.atlas.ethanol_plants.parquet": "ethanol_plants",
        "us.eia.ethanol_capacity.parquet": "ethanol_capacity",
        "us.eia.860.owners.parquet": "eia_owners",
        "us.epa.ghgrp.parquet": "ghgrp",
        "global.gleif.lei.parents.parquet": None,
        # docs/25 §2.7 limit 1: written by the OpenStreetMap pull, which reads the published build.
        "us.osm.power.lines.parquet": "osm_power",
        "us.osm.power.substations.parquet": "osm_power",
    }
    assert set(builder_for) == set(context_layers.expected_files())
    for name, builder in builder_for.items():
        if builder is not None:
            assert f"pipeline.context.{builder}" in modules, name


def test_context_load_reads_the_connector_data_root_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    seen: list[pathlib.Path] = []

    def fake_load(session: Any, data_root: pathlib.Path) -> dict[str, Any]:
        seen.append(data_root)
        return {"files_loaded": 0}

    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(tmp_path))
    refreshed: list[Any] = []

    def fake_refresh(factory: Any) -> dict[str, Any]:
        refreshed.append(factory)
        return {"written": 0}

    factory = _Factory()
    report = jobs.context_load_job(fake_load, _session_factory=factory, _refresh=fake_refresh)
    assert report == {"files_loaded": 0, "organization_stats": {"written": 0}}
    assert seen == [tmp_path]
    assert refreshed == [factory]  # after the load's transaction, on the same store


def test_context_load_on_an_empty_data_root_reports_every_file_missing(tmp_path: pathlib.Path) -> None:
    """The real loader over a data root with no context file: nothing fails, every expected file is
    reported missing, and the curated organisation files (which ship with the code) still apply.
    The companies index's figures are refreshed over what it loaded (one row per organisation,
    scope and tier)."""
    from services.ingest import context_layers

    factory = _Factory()
    report = jobs.context_load_job(_session_factory=factory, _data_root=tmp_path)
    assert report["files_loaded"] == 0
    assert report["files_missing"] == context_layers.expected_files()
    with factory() as session:
        from services.db.models import Organization

        listed = session.query(Organization).filter(Organization.merged_into_id.is_(None)).count()
    assert report["organization_stats"]["rows"] == 4 * listed
    assert "error" not in report["organization_stats"]


def test_a_stats_refresh_failure_is_reported_never_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The figures are derived and withheld by the API when it cannot vouch for them, so a defect in
    them must not fail the pass that ran first (`jobs.refresh_organization_stats`)."""
    import services.api.org_stats_refresh as org_stats_job

    def broken(session_factory: Any, *, if_stale: bool = False) -> dict[str, Any]:
        raise RuntimeError("figure defect")

    monkeypatch.setattr(org_stats_job, "run", broken)
    assert jobs.refresh_organization_stats(_Factory()) == {"error": "RuntimeError"}
    assert jobs.org_stats_tick_job(_Factory()) == {"error": "RuntimeError"}


def test_the_stats_tick_refreshes_only_a_stale_table() -> None:
    from services.db.models import Organization

    factory = _Factory()
    with factory() as session:
        session.add(
            Organization(
                public_id="org_tick",
                slug="tick-co",
                name_canonical="Tick Co",
                name_normalised="tick co",
                type="developer",
                country="US",
            )
        )
        session.commit()
    assert jobs.org_stats_tick_job(factory)["inserted"] == 4  # never refreshed: stale
    assert jobs.org_stats_tick_job(factory) == {"skipped": "current"}


def test_the_stats_tick_is_registered_under_the_store_wide_lock(scheduler_app: Any) -> None:
    task = scheduler_app.app.tasks["org_stats_tick"]
    assert (task.queue, task.queueing_lock, task.lock) == ("resolve", "org_stats_tick", "resolve")
    periodic = scheduler_app.app.periodic_registry.periodic_tasks
    assert ("tick_org_stats", "tick:org_stats") in periodic
    assert scheduler_app.ORG_STATS_CRON == "1,6,11,16,21,26,31,36,41,46,51,56 * * * *"


def test_the_task_bodies_call_their_jobs_and_an_already_queued_successor_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, scheduler_app: Any
) -> None:
    import procrastinate

    real_build, real_enrich = scheduler_app.context_build, scheduler_app.enrich_tick
    tick = scheduler_app.app.tasks["tick_context"]
    monkeypatch.setattr(jobs, "match_tick_job", lambda: {"mode": "incremental"})
    monkeypatch.setattr(jobs, "context_load_job", lambda: {"files_loaded": 12})
    monkeypatch.setattr(jobs, "enrich_tick_job", lambda: {"ok": True})
    monkeypatch.setattr(jobs, "context_build_job", lambda: {"ok": 13})
    monkeypatch.setattr(scheduler_app, "site_finder_refresh", _FakeTask("site_finder_refresh", []))
    assert scheduler_app.match_tick.func() == {"mode": "incremental"}
    assert scheduler_app.context_load.func() == {"files_loaded": 12}

    deferred: list[str] = []
    monkeypatch.setattr(scheduler_app, "context_build", _FakeTask("context_build", deferred))
    tick.func(timestamp=0)
    assert deferred == ["context_build"]

    class _Queued(_FakeTask):
        def defer(self, **kwargs: Any) -> int:
            raise procrastinate.exceptions.AlreadyEnqueued("queued")

    # The successor still queued from an earlier run: logged and dropped, the job still succeeds.
    for name in ("context_build", "context_load", "match_tick"):
        monkeypatch.setattr(scheduler_app, name, _Queued(name, deferred))
    tick.func(timestamp=0)
    assert real_build.func() == {"ok": 13}
    assert real_enrich.func() == {"ok": True}
    assert deferred == ["context_build"]


def test_a_context_load_chains_to_the_site_finder_unless_the_store_was_busy(
    monkeypatch: pytest.MonkeyPatch, scheduler_app: Any
) -> None:
    """The site finder reads what the load wrote, so it runs after it; a load skipped because another
    store-wide pass held the lock wrote nothing new, and the next load re-chains."""
    import procrastinate

    deferred: list[str] = []
    monkeypatch.setattr(scheduler_app, "site_finder_refresh", _FakeTask("site_finder_refresh", deferred))
    monkeypatch.setattr(jobs, "context_load_job", lambda: {"files_loaded": 12})
    scheduler_app.context_load.func()
    assert deferred == ["site_finder_refresh"]
    monkeypatch.setattr(jobs, "run_store_pass", lambda name, work: {"skipped": jobs.STORE_WIDE_BUSY})
    scheduler_app.context_load.func()
    assert deferred == ["site_finder_refresh"]

    class _Queued(_FakeTask):
        def defer(self, **kwargs: Any) -> int:
            raise procrastinate.exceptions.AlreadyEnqueued("queued")

    monkeypatch.setattr(jobs, "run_store_pass", lambda name, work: work())
    monkeypatch.setattr(scheduler_app, "site_finder_refresh", _Queued("site_finder_refresh", deferred))
    assert scheduler_app.context_load.func() == {"files_loaded": 12}


def test_the_site_finder_runs_on_a_consumed_queue_under_its_own_lock(scheduler_app: Any) -> None:
    import yaml

    task = scheduler_app.app.tasks["site_finder_refresh"]
    assert (task.queue, task.lock, task.queueing_lock) == ("resolve", "site_finder", "site_finder_refresh")
    worker = yaml.safe_load((ROOT / "infra" / "compose" / "docker-compose.yml").read_text())["services"][
        "worker"
    ]
    assert task.queue in set(worker["command"][worker["command"].index("--queues") + 1].split(","))


def test_the_site_finder_job_runs_a_refresh_over_the_connector_data_root(tmp_path: pathlib.Path) -> None:
    seen: list[Any] = []

    def run(factory: Any, data_root: pathlib.Path) -> dict[str, Any]:
        seen.append((factory, data_root))
        return {"status": "ok", "segments": 5}

    factory = _Factory()
    counties: list[pathlib.Path] = []

    def fetch_counties(root: pathlib.Path) -> dict[str, Any]:
        counties.append(root)
        return {"fetched": False}

    assert jobs.site_finder_refresh_job(
        run,
        _session_factory=factory,
        _data_root=tmp_path,
        _counties=fetch_counties,
        _memory_limit=lambda: None,
    ) == {"status": "ok", "segments": 5, "counties_file": {"fetched": False}}
    assert seen == [(factory, tmp_path)] and counties == [tmp_path]


def test_the_site_finder_does_not_start_in_a_container_too_small_for_it(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Review 2026-10-23: once the pulls land Texas's OpenStreetMap lines, the monthly chain runs the
    full Texas computation inside the worker, which peaked at 1.2 GB (docs/25 §7), and the beta's
    worker was limited to 1 GB: the kernel would kill the worker with every job in flight, and the
    stalled-job sweep would start it again up to three times. Below the floor the job does nothing
    but say so; at the 3 GB the beta's worker needs, or with no limit at all, it runs."""
    gib = 1024**3
    factory = _Factory()

    def refresh(limit: int | None) -> tuple[dict[str, Any], list[str]]:
        calls: list[str] = []

        def run(factory: Any, data_root: pathlib.Path) -> dict[str, Any]:
            calls.append("run")
            return {"status": "ok"}

        def fetch_counties(root: pathlib.Path) -> dict[str, Any]:
            calls.append("counties")
            return {"fetched": False}

        report = jobs.site_finder_refresh_job(
            run,
            _session_factory=factory,
            _data_root=tmp_path,
            _counties=fetch_counties,
            _memory_limit=lambda: limit,
        )
        return report, calls

    with caplog.at_level("WARNING", logger=jobs.logger.name):
        report, calls = refresh(gib)  # compose.prod.yml's `mem_limit: 1g`
    assert calls == []
    assert report["skipped"] == "memory"
    assert (report["memory_limit_mb"], report["memory_needed_mb"]) == (1024, 2048)
    assert "compose.single.yml" in report["fix"]
    assert "1024 MB, below the 2048 MB" in caplog.text
    for limit in (3 * gib, jobs.SITE_FINDER_MIN_MEMORY_BYTES, None):
        report, calls = refresh(limit)
        assert report["status"] == "ok" and calls == ["counties", "run"], limit


def test_the_container_memory_limit_is_read_from_cgroup_v2_then_v1(tmp_path: pathlib.Path) -> None:
    v2, v1 = tmp_path / "memory.max", tmp_path / "memory.limit_in_bytes"
    assert jobs.container_memory_limit((v2, v1)) is None  # neither file: a development machine
    v2.write_text("1073741824\n")
    assert jobs.container_memory_limit((v2, v1)) == 1024**3  # Compose `mem_limit: 1g`
    v2.write_text("max\n")
    assert jobs.container_memory_limit((v2, v1)) is None
    v2.unlink()
    v1.write_text("9223372036854771712\n")  # cgroup v1's "no limit"
    assert jobs.container_memory_limit((v2, v1)) is None
    v1.write_text("3221225472\n")
    assert jobs.container_memory_limit((v2, v1)) == 3 * 1024**3
    v2.write_text("garbage\n")
    assert jobs.container_memory_limit((v2, v1)) == 3 * 1024**3


def test_the_context_tick_runs_after_the_builds_it_pulls_have_finished(scheduler_app: Any) -> None:
    """Review 2026-10-23: the tick fired on the 3rd at 02:43, before the flood (the 3rd, 04:41) and
    prospect (the 3rd, 06:41) builds, so each scheduled build reached the server a month late. Each
    build's schedule plus the sum of its jobs' time limits (an upper bound) must end before the tick."""
    import yaml

    def minutes(cron: str) -> int:
        minute, hour, day = (int(field) for field in cron.split()[:3])
        return (day * 24 + hour) * 60 + minute

    tick = minutes(scheduler_app.CONTEXT_CRON)
    for name in ("grid-layers.yml", "flood-layer.yml", "prospect-layers.yml"):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
        on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as true
        limit = sum(int(job.get("timeout-minutes", 360)) for job in workflow["jobs"].values())
        for schedule in on["schedule"]:
            assert minutes(schedule["cron"]) + limit < tick, (name, schedule["cron"], limit)


def test_the_site_finder_brings_the_counties_file_up_to_date_and_runs_on_without_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before each run the TIGER counties file is checked (one HEAD request when unchanged) for the
    states the server loads; a failed fetch is reported and the run goes on with what it has."""
    calls: list[tuple[pathlib.Path, Any]] = []

    def build(root: pathlib.Path, *, states: Any = None) -> dict[str, Any]:
        calls.append((root, states))
        return {"fetched": True, "counties": 254, "retrieved_at": "2026-10-23T07:00:00Z", "out_bytes": 1}

    monkeypatch.setenv("GRID_LAYERS_REGIONS", "US-TX, US-OK")
    assert jobs.site_finder_counties(tmp_path, build) == {
        "fetched": True,
        "counties": 254,
        "retrieved_at": "2026-10-23T07:00:00Z",
    }
    monkeypatch.setenv("GRID_LAYERS_REGIONS", "all")
    jobs.site_finder_counties(tmp_path, build)
    assert calls == [(tmp_path, ["TX", "OK"]), (tmp_path, None)]

    def offline(root: pathlib.Path, *, states: Any = None) -> dict[str, Any]:
        raise OSError("census.gov unreachable")

    failed = jobs.site_finder_counties(tmp_path, offline)
    assert failed["fetched"] is False and "unreachable" in failed["error"]
