"""Loader for the built-infrastructure context layer (docs/21 §3.20, `pipeline.context.eia_plants`
output) -> `built_plant`.

Same provenance quartet and licence-gate discipline as `services/ingest/loader.py`, but a
different shape entirely: `built_plant` has no lifecycle, no `proposal_source`/`event` rows, no
entity resolution -- one row per `(source_id, source_plant_id)`, upserted in place on every run.
`upsert_licence_and_source` is reused unchanged so the same `licence`/`source` rows the proposal
loader writes back this source's registry entry identically (docs/00-PLAN.md decision
2026-09-14).

Idempotency choice (task scope leaves this open): a re-run always overwrites every mapped field
on an existing row, even when nothing changed, and reports it as `updated` -- not skipped. This
keeps the loader a single straightforward pass (load existing keys once, then one unconditional
write per row) rather than a field-by-field diff that `built_plant` has no event log to record
the outcome of anyway; the unique constraint on `(source_id, source_plant_id)` still guarantees
no duplicate rows, which is the idempotency property that matters here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import time
from dataclasses import asdict, dataclass

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from services.db.models import BuiltPlant, new_uuid
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import upsert_licence_and_source

DEFAULT_DB_PATH = pathlib.Path("web/.data/dev.db")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class PlantsLoadResult:
    plants_seen: int = 0
    inserted: int = 0
    updated: int = 0
    placed: int = 0
    unplaced: int = 0


def _none_if_missing(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    return value


def _to_str(value: object) -> str | None:
    value = _none_if_missing(value)
    return None if value is None else str(value)


def _to_float(value: object) -> float | None:
    value = _none_if_missing(value)
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def _to_int(value: object) -> int | None:
    f = _to_float(value)
    return None if f is None else int(f)


def _to_datetime(value: object) -> dt.datetime | None:
    value = _none_if_missing(value)
    if value is None:
        return None
    ts = pd.Timestamp(value)  # type: ignore[arg-type]
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    result: dt.datetime = ts.to_pydatetime()
    return result


def _to_technologies(value: object) -> dict[str, float]:
    value = _none_if_missing(value)
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value) if value else {}
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    # Defensive against a value missing for one raw label in one row of a batch written by a
    # parquet engine that infers a shared struct schema across rows with different key sets
    # (each row then gets every key, null-filled) -- `pipeline.context.eia_plants`'s own CLI
    # avoids this by JSON-encoding the column first (`to_parquet_safe`), but a caller building a
    # frame by hand (tests included) may not.
    return {str(k): float(v) for k, v in value.items() if v is not None and not pd.isna(v)}


def load_plants(session: Session, df: pd.DataFrame, *, manifest_version: str = "") -> PlantsLoadResult:
    """Upsert one `pipeline.context.eia_plants.aggregate_plants` frame by `(source_id,
    source_plant_id)`. Bulk-friendly: the registry/licence/source rows and every existing
    `built_plant` key for this source are loaded once, up front -- no per-row `SELECT`."""
    registry = Registry()
    entry = registry.get("us.eia.860m")
    source = upsert_licence_and_source(session, entry, manifest_version or registry.version)

    result = PlantsLoadResult(plants_seen=len(df))
    if not len(df):
        session.commit()
        return result

    existing: dict[str, BuiltPlant] = {
        p.source_plant_id: p
        for p in session.scalars(select(BuiltPlant).where(BuiltPlant.source_id == source.id))
    }

    now = utcnow()
    for row in df.to_dict("records"):
        source_plant_id = str(row.get("source_plant_id"))
        lon = _to_float(row.get("lon"))
        lat = _to_float(row.get("lat"))
        geom = (lon, lat) if lon is not None and lat is not None else None
        retrieved_at = _to_datetime(row.get("retrieved_at")) or now

        fields: dict[str, object] = {
            "name": _to_str(row.get("name")) or f"EIA Plant {source_plant_id}",
            "operator_name": _to_str(row.get("operator_name")),
            "technology": _to_str(row.get("technology")),
            "technology_raw": _to_str(row.get("technology_raw")),
            "technologies": _to_technologies(row.get("technologies")),
            "capacity_mw": _to_float(row.get("capacity_mw")),
            "generator_count": _to_int(row.get("generator_count")) or 0,
            "earliest_operating_year": _to_int(row.get("earliest_operating_year")),
            "geom": geom,
            "state_code": _to_str(row.get("state_code")),
            "county_name": _to_str(row.get("county_name")),
            "country": _to_str(row.get("country")) or "US",
            "source_url": _to_str(row.get("source_url")) or source.url,
            "retrieved_at": retrieved_at,
            "licence_id": source.licence_id,
        }

        plant = existing.get(source_plant_id)
        if plant is None:
            plant = BuiltPlant(
                id=new_uuid(),
                source_id=source.id,
                source_plant_id=source_plant_id,
                **fields,
            )
            session.add(plant)
            existing[source_plant_id] = plant
            result.inserted += 1
        else:
            for key, value in fields.items():
                setattr(plant, key, value)
            result.updated += 1

        if geom is not None:
            result.placed += 1
        else:
            result.unplaced += 1

    session.commit()
    return result


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
