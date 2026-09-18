"""`power_plant` case of the generalised asset loader (`services/ingest/assets.py`, ADR 0008).

Kept as a thin wrapper -- same public names and signatures (`load_plants`, `load_plants_parquet`,
`PlantsLoadResult`, `main`) as before ADR 0008, because `web/dev_up.py` imports
`load_plants_parquet(session, path)` directly. Every actual loading rule (idempotent upsert on
`(source_id, source_asset_id)`, EIA-860M as the source, geocoding fallbacks, `technologies`
JSON-split handling) now lives in `services/ingest/assets.py`; this module only fixes
`asset_type="power_plant"` and renames the result fields back to their pre-ADR-0008 spelling
(`plants_seen` for `assets_seen`) so any caller still reading `PlantsLoadResult` by field name is
unaffected.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import time
from dataclasses import asdict, dataclass

import pandas as pd
from sqlalchemy.orm import Session

from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.assets import AssetsLoadResult, load_assets

DEFAULT_DB_PATH = pathlib.Path("web/.data/dev.db")


@dataclass
class PlantsLoadResult:
    plants_seen: int = 0
    inserted: int = 0
    updated: int = 0
    placed: int = 0
    unplaced: int = 0

    @classmethod
    def _from_assets_result(cls, r: AssetsLoadResult) -> PlantsLoadResult:
        return cls(
            plants_seen=r.assets_seen,
            inserted=r.inserted,
            updated=r.updated,
            placed=r.placed,
            unplaced=r.unplaced,
        )


def load_plants(session: Session, df: pd.DataFrame, *, manifest_version: str = "") -> PlantsLoadResult:
    """`services.ingest.assets.load_assets(session, df, asset_type="power_plant")`, same
    `source_plant_id`-shaped input columns as before (`services/ingest/assets.py` accepts
    `source_asset_id`; a frame still spelling it `source_plant_id` is renamed here first, so
    nothing that already builds a plants frame the old way needs to change)."""
    if "source_plant_id" in df.columns and "source_asset_id" not in df.columns:
        df = df.rename(columns={"source_plant_id": "source_asset_id"})
    result = load_assets(session, df, "power_plant", manifest_version=manifest_version)
    return PlantsLoadResult._from_assets_result(result)


def load_plants_parquet(session: Session, path: pathlib.Path) -> PlantsLoadResult:
    df = pd.read_parquet(path)
    return load_plants(session, df)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=pathlib.Path, required=True)
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite+pysqlite:///{args.db}"

    t0 = time.monotonic()
    engine = get_engine(db_url)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    session = session_factory()
    try:
        result = load_plants_parquet(session, args.parquet)
    finally:
        session.close()
    elapsed = round(time.monotonic() - t0, 2)
    print(json.dumps({**asdict(result), "elapsed_s": elapsed}))  # noqa: T201 — CLI summary line


if __name__ == "__main__":
    main()
