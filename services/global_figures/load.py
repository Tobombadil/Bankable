"""Load Ember runs into the global electricity tables (`python -m services.global_figures.load --latest`).

Called by the refresh job after a run (`infra/scheduler/global_figures.py`), and by hand for a rebuild.

1. The licence gate, twice, as for records: `services.ingest.loader.upsert_licence_and_source` refuses a
   gated source and writes the `licence`/`source` rows the values' foreign keys need. `source.implemented`
   is set: this runner implements the source, though the record registry, which looks for a record
   connector, lists it as unimplemented (as for the price sources).
2. For each dataset and route a run wrote (`yearly/api`, `yearly/file`, `monthly/api`,
   `monthly_capacity/api`), the previous load of that pair and its values are deleted and the run's are
   inserted, in the caller's transaction: a load that fails part-way rolls back to the previous figures.
   A load already applied (its id is `<run ts>:<dataset>:<origin>`) is skipped, so loading twice changes
   nothing.
3. A run whose values were fetched under other terms than the manifest quotes now (its `licence_id`
   differs) is refused: the terms changed, and a reviewer reads them before anything new is served.

`--latest` loads, for each dataset and route, the newest run that wrote it, if it is not loaded yet:
what the job calls, so a load that failed is retried by the next one.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from collections.abc import Iterator
from typing import Any

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.orm import Session

from pipeline.connectors.global_ember_electricity import SOURCE_ID
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store, open_store
from services.global_figures.models import GlobalElectricityLoad, GlobalElectricityValue
from services.ingest.loader import upsert_licence_and_source

log = logging.getLogger("services.global_figures.load")

#: The normalised columns a load reads (never `raw`, which stays in the run's parquet as evidence).
LOAD_COLUMNS = [
    "dataset",
    "origin",
    "area_id",
    "area_name",
    "area_kind",
    "iso3",
    "period",
    "year",
    "month",
    "source",
    "source_label",
    "is_aggregate",
    "metric",
    "value",
    "unit",
    "source_id",
    "source_url",
    "retrieved_at",
    "licence_id",
]
CHUNK = 5000
#: `--run` / `--latest` exit code for a retryable database conflict (the price loader's convention).
EXIT_RETRYABLE = 75


class LoadRefused(RuntimeError):
    """The run may not be loaded (terms changed since it was fetched, or its output is missing)."""


def _when(value: Any) -> dt.datetime:
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_convert("UTC") if stamp.tzinfo is not None else stamp.tz_localize("UTC")
    when: dt.datetime = stamp.to_pydatetime()
    return when


def _runs(store: Store) -> list[tuple[str, dict[str, Any]]]:
    return list(store._run_entries(SOURCE_ID))


def latest_written(store: Store) -> dict[str, str]:
    """`dataset/origin` -> the ts of the newest run that wrote it."""
    out: dict[str, str] = {}
    for ts, record in _runs(store):
        for label in (record.get("outputs") or {}).get("datasets") or []:
            out[str(label)] = ts
    return out


def _value_rows(frame: pd.DataFrame, load_id: str) -> Iterator[list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for r in frame.itertuples(index=False):
        month = None if pd.isna(r.month) else int(r.month)
        rows.append(
            {
                "load_id": load_id,
                "area_id": r.area_id,
                "period": r.period,
                "source": r.source,
                "metric": r.metric,
                "dataset": r.dataset,
                "area_name": r.area_name,
                "area_kind": r.area_kind,
                "iso3": None if pd.isna(r.iso3) else r.iso3,
                "year": int(r.year),
                "month": month,
                "source_label": r.source_label,
                "is_aggregate": bool(r.is_aggregate),
                "value": float(r.value),
                "unit": r.unit,
                "source_id": r.source_id,
                "licence_id": r.licence_id,
                "source_url": r.source_url,
                "retrieved_at": _when(r.retrieved_at),
            }
        )
        if len(rows) >= CHUNK:
            yield rows
            rows = []
    if rows:
        yield rows


def _load_id(ts: str, label: str) -> str:
    """`<run ts>:<dataset>:<origin>` for a `dataset/origin` label."""
    dataset, _, origin = label.partition("/")
    return f"{ts}:{dataset}:{origin}"


def _coverage(frame: pd.DataFrame) -> dict[str, int]:
    countries = frame[(frame["area_kind"] == "country") & (~frame["is_aggregate"].astype(bool))]
    metric = "generation_twh" if (countries["metric"] == "generation_twh").any() else "capacity_gw"
    picked = countries[countries["metric"] == metric]
    return {str(int(y)): int(n) for y, n in picked.groupby("year")["area_id"].nunique().items()}


def load_run(
    session: Session,
    ts: str,
    *,
    store: Store | None = None,
    registry: Registry | None = None,
    only: set[str] | None = None,
) -> dict[str, Any]:
    """Load the datasets run `ts` wrote (those in `only`, when given). Returns a report; raises
    `LoadRefused`."""
    store = store or open_store()
    registry = registry or Registry()
    entry = registry.get(SOURCE_ID)
    source = upsert_licence_and_source(session, entry, registry.version)
    source.implemented = True
    record = store.read_json(store.run_path(SOURCE_ID, ts))
    labels = [str(x) for x in ((record.get("outputs") or {}).get("datasets") or [])]
    if only is not None:
        labels = [label for label in labels if label in only]
    report: dict[str, Any] = {"source_id": SOURCE_ID, "ts": ts, "loaded": [], "skipped": []}
    pending = [label for label in labels if session.get(GlobalElectricityLoad, _load_id(ts, label)) is None]
    report["skipped"] = [label for label in labels if label not in pending]
    labels = pending
    if not labels:
        return report
    path = store.normalized_path(SOURCE_ID, ts)
    if not store.exists(path):
        raise LoadRefused(f"run {ts} names datasets {labels} but its normalised file is gone")
    frame = store.read_parquet(path)
    frame = frame[[c for c in LOAD_COLUMNS if c in frame.columns]]
    stale = set(frame["licence_id"].astype(str)) - {entry.licence_id}
    if stale:
        raise LoadRefused(
            f"run {ts} was fetched under {sorted(stale)}; the manifest now quotes {entry.licence_id} "
            "(the terms changed: a reviewer reads them before anything new is served)"
        )
    for label in labels:
        dataset, _, origin = label.partition("/")
        load_id = _load_id(ts, label)
        part = frame[(frame["dataset"] == dataset) & (frame["origin"] == origin)]
        if part.empty:
            report["skipped"].append(label)
            continue
        old = session.scalars(
            sa.select(GlobalElectricityLoad.id).where(
                GlobalElectricityLoad.dataset == dataset, GlobalElectricityLoad.origin == origin
            )
        ).all()
        if old:
            session.execute(sa.delete(GlobalElectricityValue).where(GlobalElectricityValue.load_id.in_(old)))
            session.execute(sa.delete(GlobalElectricityLoad).where(GlobalElectricityLoad.id.in_(old)))
            session.flush()
        retrieved = max(_when(v) for v in part["retrieved_at"].unique())
        session.add(
            GlobalElectricityLoad(
                id=load_id,
                dataset=dataset,
                origin=origin,
                run_ts=ts,
                run_id=str(record.get("id") or "") or None,
                metrics=",".join(sorted(part["metric"].unique())),
                rows=len(part),
                areas=int(part["area_id"].nunique()),
                first_period=str(part["period"].min()),
                last_period=str(part["period"].max()),
                countries_by_year=_coverage(part),
                source_id=SOURCE_ID,
                licence_id=entry.licence_id,
                source_url=str(part["source_url"].iloc[0]),
                retrieved_at=retrieved,
            )
        )
        session.flush()
        for chunk in _value_rows(part, load_id):
            session.execute(sa.insert(GlobalElectricityValue), chunk)
        session.flush()
        report["loaded"].append({"dataset": label, "rows": len(part), "replaced": list(old)})
    return report


def load_latest(
    session: Session, *, store: Store | None = None, registry: Registry | None = None
) -> dict[str, Any]:
    """For each dataset and route, the newest run that wrote it, if not loaded yet."""
    store = store or open_store()
    latest = latest_written(store)
    report: dict[str, Any] = {"source_id": SOURCE_ID, "runs": []}
    for ts in sorted(set(latest.values())):
        labels = {label for label, run_ts in latest.items() if run_ts == ts}
        report["runs"].append(load_run(session, ts, store=store, registry=registry, only=labels))
    return report


def main(argv: list[str] | None = None) -> int:
    from services.db.session import get_engine, get_sessionmaker, init_db, session_scope

    ap = argparse.ArgumentParser(prog="python -m services.global_figures.load")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", metavar="TS", help="load the datasets one run wrote")
    group.add_argument(
        "--latest", action="store_true", help="load the newest run of each dataset not yet loaded"
    )
    ap.add_argument("--init-db", action="store_true", help="create missing tables first (dev only)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = get_engine()
    if args.init_db:
        import services.global_figures.models  # noqa: F401 - registers the tables

        init_db(engine)
    factory = get_sessionmaker(engine)
    try:
        with session_scope(factory) as session:
            report = load_run(session, args.run) if args.run else load_latest(session)
    except sa.exc.OperationalError as exc:
        sys.stdout.write(json.dumps({"error": f"OperationalError: {str(exc)[:300]}"}) + "\n")
        return EXIT_RETRYABLE
    except Exception as exc:
        sys.stdout.write(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:300]}"}) + "\n")
        return 1
    sys.stdout.write(json.dumps(report, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
