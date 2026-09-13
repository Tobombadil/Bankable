"""Reproducible measurement script for `services/ingest/loader.py`'s bulk-insert pass (Sprint 3).

`docs/00-PLAN.md` records the loader at roughly 100 rows/second (the full-data site test took
451s; `services/README.md`'s "Bulk-insert pass (Sprint 3)" section has the fix and the
before/after numbers this script produced). Run it before and after any future change to the
row-loop hot path in `load_dataframe`/`upsert_licence_and_source` to get a like-for-like number:
it loads `data/eval/normalized.parquet` (14,388 Phase 2 resolution-evaluation records, docs/22)
into a fresh SQLite *file* database (never `:memory:` -- closer to Postgres's real commit costs,
per the task's measurement method) through the unmodified public loader functions, and reports
rows loaded, elapsed seconds, rows/second, and a source-setup / record-load / commit breakdown
(the loader itself exposes no finer-grained phase timing than that; see `services/README.md`'s
"Profile" section for the cProfile breakdown of the record-load phase itself).

Benchmark input adapter (kept here, never in `services/ingest/loader.py`, per the task brief): the
parquet uses short source ids (`ercot`, `caiso`, `nyiso`, `eia860m`, `spp`, `isone`) rather than
the `data/sources.yaml` ids `upsert_licence_and_source`/`load_dataframe` need a real `Source` row
for, and two of those six -- `spp` and `isone` -- are `restricted` under
`docs/13-legal-data-rights.md` and must never enter the store (CLAUDE.md's guardrail: "PJM rows
are not public until a licence exists. MISO/SPP/NYISO/ISO-NE terms must be read and recorded
before their rows are published"). `EVAL_SOURCE_MAP` below excludes them rather than remapping
them -- the same scope `web/build_data.py::EVAL_SHORT_ID_MAP` uses for this exact fixture,
reproduced here (not imported) to keep this measurement script independent of `web/`. Loading the
four open/attribution sources gives 9,563 rows, matching the count `services/README.md`'s "Loader
fixes" section already recorded for this same fixture.

Usage:
    .venv/bin/python -m services.ingest.bench_loader
    .venv/bin/python -m services.ingest.bench_loader --batch-size 200 --runs 3
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from pipeline.connectors.registry import Registry
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import DEFAULT_BATCH_SIZE, load_dataframe, upsert_licence_and_source

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_EVAL_PARQUET = REPO_ROOT / "data" / "eval" / "normalized.parquet"
DEFAULT_SOURCES_YAML = REPO_ROOT / "data" / "sources.yaml"

#: docs/00-PLAN.md's 2026-09-12 legal register + CLAUDE.md's guardrail: `spp` and `isone` are
#: `restricted` and must never be published, so this benchmark excludes them rather than
#: remapping them -- the same scope `web/build_data.py::EVAL_SHORT_ID_MAP` uses for this fixture.
EVAL_SOURCE_MAP: dict[str, str] = {
    "ercot": "us.iso.ercot.gen_queue",
    "caiso": "us.iso.caiso.gen_queue",
    "nyiso": "us.iso.nyiso.gen_queue",
    "eia860m": "us.eia.860m",
}


@dataclass
class BenchRun:
    rows_loaded: int
    setup_s: float
    load_s: float
    commit_s: float

    @property
    def total_s(self) -> float:
        return self.setup_s + self.load_s + self.commit_s

    @property
    def rows_per_s(self) -> float:
        return self.rows_loaded / self.total_s if self.total_s else 0.0


def load_eval_frames(eval_parquet: Path = DEFAULT_EVAL_PARQUET) -> dict[str, pd.DataFrame]:
    """`{registry source_id: frame}` for the four open/attribution sources in the eval fixture
    (module docstring above); `spp`/`isone` rows are dropped, never remapped."""
    df = pd.read_parquet(eval_parquet)
    frames: dict[str, pd.DataFrame] = {}
    for short_id, source_id in EVAL_SOURCE_MAP.items():
        frame = df[df["source_id"] == short_id].copy()
        if not frame.empty:
            frames[source_id] = frame
    return frames


def run_once(
    frames: dict[str, pd.DataFrame],
    *,
    db_path: Path,
    sources_yaml: Path = DEFAULT_SOURCES_YAML,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BenchRun:
    """Load `frames` into a fresh SQLite file database at `db_path` through the real, unmodified
    loader path, timing source/licence setup, the record loads themselves, and the final commit
    separately so a regression in one phase doesn't hide inside the others."""
    registry = Registry(sources_yaml)
    engine = get_engine(f"sqlite+pysqlite:///{db_path}")
    init_db(engine)
    session_factory = get_sessionmaker(engine)

    rows_loaded = 0
    setup_s = 0.0
    load_s = 0.0
    with session_factory() as session:
        for source_id, frame in frames.items():
            entry = registry.get(source_id)

            t0 = time.perf_counter()
            source = upsert_licence_and_source(session, entry, registry.version)
            setup_s += time.perf_counter() - t0

            t0 = time.perf_counter()
            load_dataframe(session, source, "proposal", frame, None, batch_size=batch_size)
            load_s += time.perf_counter() - t0
            rows_loaded += len(frame)

        t0 = time.perf_counter()
        session.commit()
        commit_s = time.perf_counter() - t0

    engine.dispose()
    return BenchRun(rows_loaded=rows_loaded, setup_s=setup_s, load_s=load_s, commit_s=commit_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--eval-parquet", type=Path, default=DEFAULT_EVAL_PARQUET)
    parser.add_argument("--sources-yaml", type=Path, default=DEFAULT_SOURCES_YAML)
    args = parser.parse_args(argv)

    batch_size: int = args.batch_size
    runs: int = args.runs
    eval_parquet: Path = args.eval_parquet
    sources_yaml: Path = args.sources_yaml

    frames = load_eval_frames(eval_parquet)
    total_rows = sum(len(f) for f in frames.values())
    sys.stdout.write(
        f"services/ingest/bench_loader: {total_rows} rows across {len(frames)} sources "
        f"({', '.join(sorted(frames))}), batch_size={batch_size}\n"
    )

    for run_index in range(1, runs + 1):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bench.sqlite3"
            result = run_once(frames, db_path=db_path, sources_yaml=sources_yaml, batch_size=batch_size)
        sys.stdout.write(
            f"run {run_index}: {result.rows_loaded} rows in {result.total_s:.3f}s "
            f"({result.rows_per_s:.1f} rows/s) "
            f"[setup {result.setup_s:.3f}s, load {result.load_s:.3f}s, commit {result.commit_s:.3f}s]\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
