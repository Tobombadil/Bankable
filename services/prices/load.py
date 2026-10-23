"""Load a price source's interval store into the tables (`python -m services.prices load <source>`).

Called by the scheduler after a price run that wrote rows (`infra/scheduler/prices.py`), and by
hand for a rebuild. Steps, in one transaction:

1. The licence gate, twice, as for records: `services.ingest.loader.upsert_licence_and_source`
   refuses a gated source (`GateRefused`) and writes the `licence`/`source` rows the price rows'
   foreign keys need. `source.implemented` is set: the price runner implements these sources,
   though the record registry, which looks for a record connector, lists them as unimplemented.
2. `price_location`: the catalogue (`pipeline/connectors/prices/locations.py`) with its placement.
   A location leaving the catalogue keeps its row and its history (nothing here deletes).
3. `price_daily`: the market-local days the run touched (or every stored day on a rebuild) are
   summarised again from the interval files (`services/prices/daily.py`) and replace their rows.
4. `price_latest`: the newest stored interval per location and market **that has begun**, only
   where the licence allows raw publication (ERCOT, SPP under the noncommercial posture, Elexon);
   a derived-only source (CAISO, NYISO) never has a latest row, so no surface can serve one.
   ERCOT posts tomorrow's day-ahead prices today, and a future hour is not a "latest" price
   (review 2026-10-23).
5. `retail_price` for EIA's table.

**Memory (review 2026-10-23).** This runs inside the worker (`infra/scheduler/app.py::price_load`,
1 GB container). It never reads the `raw` source rows or the `record_id`s, which are most of an
interval file in memory (`LOAD_COLUMNS`), and it summarises one market-local month at a time over a
sliding window of three UTC months (a local day can begin in the UTC month before and end in the
one after). A full ERCOT rebuild read every month with `raw` and peaked at 2.6 GB; measured peaks
since are in docs/29 §9.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from collections.abc import Sequence
from typing import Any

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.orm import Session

from pipeline.connectors.prices import PRICE_CONNECTORS
from pipeline.connectors.prices import store as pstore
from pipeline.connectors.prices.locations import PriceLocation as CatalogueLocation
from pipeline.connectors.prices.locations import for_source, reference_of
from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store, open_store
from services.ingest.loader import upsert_licence_and_source
from services.prices.daily import summarise
from services.prices.models import PriceDaily, PriceLatest, PriceLocation, RetailPrice

log = logging.getLogger("services.prices.load")

RETAIL_SOURCE = "us.eia.retail_sales"
#: The catalogue's own provenance: the date its placements and names were read (docs/29, lane).
CATALOGUE_READ = dt.datetime(2026, 10, 22, 20, 28, 18, tzinfo=dt.UTC)
#: The interval columns a summary or a latest row reads (no `raw`, no `record_id`).
LOAD_COLUMNS = [
    "location_id",
    "market",
    "interval_start",
    "interval_end",
    "interval_minutes",
    "price",
    "currency",
    "source_id",
    "licence_id",
    "source_url",
    "retrieved_at",
]
#: The newest stored UTC months the latest readout is read from.
LATEST_MONTHS = 2


def _location_values(loc: CatalogueLocation, licence_id: str, source_url: str) -> dict[str, Any]:
    ref = reference_of(loc)
    point = loc.placement.grade == "point"
    return {
        "market": loc.market,
        "name": loc.name,
        "label": loc.label,
        "kind": loc.kind,
        "timezone": loc.timezone,
        "currency": loc.currency,
        "markets": ",".join(loc.markets),
        "reference_id": loc.id if loc.reference == loc.name else (ref.id if ref else None),
        "note": loc.note,
        "placement_grade": loc.placement.grade,
        "placement_method": loc.placement.method,
        "placement_basis": loc.placement.basis,
        "region_id": loc.placement.region_id,
        "lat": loc.placement.lat if point else None,
        "lon": loc.placement.lon if point else None,
        "placement_evidence_url": loc.placement.evidence_url,
        "source_id": loc.source_id,
        "licence_id": licence_id,
        "source_url": loc.placement.evidence_url or source_url,
        "retrieved_at": (
            dt.datetime.fromisoformat(loc.placement.retrieved_at.replace("Z", "+00:00"))
            if loc.placement.retrieved_at
            else CATALOGUE_READ
        ),
    }


def upsert_locations(session: Session, source_id: str, licence_id: str, source_url: str) -> int:
    count = 0
    for loc in for_source(source_id):
        values = _location_values(loc, licence_id, source_url)
        row = session.get(PriceLocation, loc.id)
        if row is None:
            session.add(PriceLocation(id=loc.id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        count += 1
    session.flush()
    return count


def _shift(month: str, by: int) -> str:
    year, mon = (int(x) for x in month.split("-"))
    index = year * 12 + (mon - 1) + by
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _month_days(month: str) -> set[dt.date]:
    year, mon = (int(x) for x in month.split("-"))
    first = dt.date(year, mon, 1)
    following = dt.date.fromisoformat(f"{_shift(month, 1)}-01")
    return {first + dt.timedelta(days=i) for i in range((following - first).days)}


class _MonthWindow:
    """The stored UTC months a local month's summaries need (it and its two neighbours), read
    with `LOAD_COLUMNS` only and kept while the next local month still needs them."""

    def __init__(self, store: Store, source_id: str, stored: Sequence[str]) -> None:
        self.store, self.source_id, self.stored = store, source_id, set(stored)
        self.cache: dict[str, pd.DataFrame] = {}
        self.reads = 0

    def around(self, month: str) -> pd.DataFrame:
        wanted = [m for m in (_shift(month, -1), month, _shift(month, 1)) if m in self.stored]
        for stale in [m for m in self.cache if m not in wanted]:
            del self.cache[stale]
        for m in wanted:
            if m not in self.cache:
                frame = pstore.read_month(self.store, self.source_id, m, LOAD_COLUMNS)
                self.cache[m] = frame if frame is not None else pd.DataFrame(columns=LOAD_COLUMNS)
                self.reads += 1
        parts = [self.cache[m] for m in wanted if len(self.cache[m])]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=LOAD_COLUMNS)


def _touched_days(
    store: Store, source_id: str, months: Sequence[str], stored: Sequence[str], tz: str
) -> set[dt.date]:
    """The market-local days a run's UTC months hold an interval of."""
    days: set[dt.date] = set()
    for month in months:
        if month not in stored:
            continue
        starts = pstore.read_month(store, source_id, month, ["interval_start"])
        if starts is not None and len(starts):
            days.update(pd.to_datetime(starts["interval_start"], utc=True).dt.tz_convert(tz).dt.date)
    return days


def load_intervals(
    session: Session, store: Store, source_id: str, licence_id: str, months: Sequence[str] | None
) -> dict[str, Any]:
    """`price_daily` for the local days of the run's `months` (every stored day when None), one
    local month at a time."""
    stored = pstore.list_months(store, source_id)
    catalogue = for_source(source_id)
    if not stored or not catalogue:
        return {"months_read": 0, "daily_rows": 0}
    timezones = {loc.id: loc.timezone for loc in catalogue}
    references: dict[str, str | None] = {}
    for loc in catalogue:
        ref = reference_of(loc)
        references[loc.id] = ref.id if ref else None
    tz = catalogue[0].timezone
    touched: set[dt.date] | None = None
    if months is None:
        local_months = sorted({_shift(m, d) for m in stored for d in (-1, 0, 1)})
    else:
        touched = _touched_days(store, source_id, months, stored, tz)
        local_months = sorted({f"{d.year:04d}-{d.month:02d}" for d in touched})
    window = _MonthWindow(store, source_id, stored)
    written = 0
    for month in local_months:
        frame = window.around(month)
        if frame.empty:
            continue
        days = _month_days(month)
        if touched is not None:
            days &= touched
        if not days:
            continue
        written += _write_daily(session, licence_id, summarise(frame, timezones, references, days))
    session.flush()
    return {"months_read": window.reads, "daily_rows": written}


def _write_daily(session: Session, licence_id: str, daily: pd.DataFrame) -> int:
    written = 0
    for rec in daily.to_dict("records"):
        key = (rec["location_id"], rec["market"], rec["day"])
        values = {k: v for k, v in rec.items() if k not in ("location_id", "market", "day")}
        values["retrieved_at"] = _ts(values["retrieved_at"])
        values["licence_id"] = licence_id
        values["basis_mean"] = None if pd.isna(values.get("basis_mean")) else float(values["basis_mean"])
        for name in ("top4_mean", "bottom4_mean"):
            values[name] = None if values.get(name) is None or pd.isna(values[name]) else float(values[name])
        row = session.get(PriceDaily, key)
        if row is None:
            session.add(PriceDaily(location_id=key[0], market=key[1], day=key[2], **values))
        else:
            for k, v in values.items():
                setattr(row, k, v)
        written += 1
    session.flush()
    return written


def _ts(value: Any) -> dt.datetime:
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    out: dt.datetime = stamp.to_pydatetime()
    return out


def load_latest(
    session: Session,
    source_id: str,
    licence_id: str,
    frame: pd.DataFrame,
    *,
    now: dt.datetime | None = None,
) -> int:
    """`price_latest` from the newest interval per location and market in `frame` that began at or
    before `now`: ERCOT's next-day day-ahead hours are stored a day early and are not "latest"."""
    if frame.empty:
        return 0
    cutoff = pd.Timestamp(now or dt.datetime.now(dt.UTC))
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    begun = frame[pd.to_datetime(frame["interval_start"], utc=True) <= cutoff]
    if begun.empty:
        return 0
    newest = begun.sort_values("interval_start").groupby(["location_id", "market"]).tail(1)
    count = 0
    known = {loc.id for loc in for_source(source_id)}
    for rec in newest.to_dict("records"):
        if rec["location_id"] not in known:
            continue
        key = (rec["location_id"], rec["market"])
        values = {
            "interval_start": _ts(rec["interval_start"]),
            "interval_end": _ts(rec["interval_end"]),
            "price": float(rec["price"]),
            "currency": rec["currency"],
            "source_id": source_id,
            "licence_id": licence_id,
            "source_url": rec["source_url"],
            "retrieved_at": _ts(rec["retrieved_at"]),
        }
        row = session.get(PriceLatest, key)
        if row is None:
            session.add(PriceLatest(location_id=key[0], market=key[1], **values))
        else:
            # Always the newest begun interval of the newest stored months (`load_source`), so it
            # also replaces a row an earlier build set to an hour that had not begun.
            for k, v in values.items():
                setattr(row, k, v)
        count += 1
    session.flush()
    return count


def load_retail(session: Session, store: Store, licence_id: str, released: str | None) -> int:
    path = pstore.retail_path(store, RETAIL_SOURCE)
    if not store.exists(path):
        return 0
    df = store.read_parquet(path)
    session.execute(sa.delete(RetailPrice))
    rows = []
    for rec in df.to_dict("records"):
        rows.append(
            {
                "state": rec["state"],
                "sector": rec["sector"],
                "period": rec["period"],
                "price_cents_kwh": _float(rec.get("price_cents_kwh")),
                "revenue_thousand_usd": _float(rec.get("revenue_thousand_usd")),
                "sales_mwh": _float(rec.get("sales_mwh")),
                "customers": _float(rec.get("customers")),
                "data_status": rec.get("data_status") if isinstance(rec.get("data_status"), str) else None,
                "source_id": RETAIL_SOURCE,
                "licence_id": licence_id,
                "source_url": rec["source_url"],
                "retrieved_at": _ts(rec["retrieved_at"]),
                "released": released,
            }
        )
    if rows:
        session.execute(sa.insert(RetailPrice), rows)
    session.flush()
    return len(rows)


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _released(store: Store, runs: list[dict[str, Any]]) -> str | None:
    """EIA's release date: the `Last-Modified` the newest successful download recorded."""
    from pipeline.connectors.prices.eia_retail import release_label

    for record in reversed(runs):
        for entry in record.get("requests") or []:
            headers = {k.lower(): v for k, v in (entry.get("headers") or {}).items()}
            if entry.get("http_status") == 200 and headers.get("last-modified"):
                return release_label(headers["last-modified"])
    return None


def load_source(
    session: Session,
    source_id: str,
    *,
    store: Store | None = None,
    months: Sequence[str] | None = None,
    registry: Registry | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Load `source_id`; `months` limits the interval summaries to a run's months (None: all)."""
    t0 = time.monotonic()
    if source_id not in PRICE_CONNECTORS:
        raise KeyError(f"{source_id} is not a price source")
    registry = registry or Registry()
    store = store or open_store()
    entry = registry.get(source_id)
    source = upsert_licence_and_source(session, entry, registry.version)
    source.implemented = True
    report: dict[str, Any] = {"source_id": source_id}
    if source_id == RETAIL_SOURCE:
        report["retail_rows"] = load_retail(
            session, store, source.licence_id, _released(store, store.runs(source_id))
        )
    else:
        report["locations"] = upsert_locations(session, source_id, source.licence_id, entry.url)
        report.update(load_intervals(session, store, source_id, source.licence_id, months))
        report["latest_rows"] = 0
        if source.licence.allows_raw_publication:
            newest = pstore.list_months(store, source_id)[-LATEST_MONTHS:]
            frame = pstore.read_months(store, source_id, newest, columns=LOAD_COLUMNS)
            report["latest_rows"] = load_latest(session, source_id, source.licence_id, frame, now=now)
    session.flush()
    report["seconds"] = round(time.monotonic() - t0, 2)
    log.info("price load %s", report, extra=report)
    return report


def load_run(session: Session, source_id: str, ts: str, *, store: Store | None = None) -> dict[str, Any]:
    """Load the months one price run wrote (`runs/<source>/<ts>.json` `outputs.months`)."""
    store = store or open_store()
    record = store.read_json(store.run_path(source_id, ts))
    months = list((record.get("outputs") or {}).get("months") or [])
    if source_id != RETAIL_SOURCE and not months:
        return {"source_id": source_id, "skipped": "the run wrote no interval month"}
    return load_source(session, source_id, store=store, months=months or None)


#: `--run`'s exit code when the load failed on a database conflict a retry can clear (a deadlock,
#: a unique violation from a concurrent load): the scheduler retries those (`LOAD_RETRY`).
EXIT_RETRYABLE = 75


def _run_one(source_id: str, ts: str) -> int:
    """`load_run` for the scheduler, as its own process (`infra/scheduler/prices.py::
    price_load_job`, which passes the worker's session limits as `PGOPTIONS`); the report is one
    JSON line on stdout. The store root is `INFRAQUE_DATA_DIR`, as for the price CLI."""
    from services.db.session import get_engine, get_sessionmaker, session_scope

    try:
        with session_scope(get_sessionmaker(get_engine())) as session:
            report = load_run(session, source_id, ts, store=open_store())
    except (sa.exc.OperationalError, sa.exc.IntegrityError) as exc:
        sys.stdout.write(
            json.dumps({"source_id": source_id, "error": f"{type(exc).__name__}: {exc}"[:500]}) + "\n"
        )
        return EXIT_RETRYABLE
    except Exception as exc:
        sys.stdout.write(
            json.dumps({"source_id": source_id, "error": f"{type(exc).__name__}: {exc}"[:500]}) + "\n"
        )
        return 1
    sys.stdout.write(json.dumps(report, default=str) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    from services.db.session import get_engine, get_sessionmaker, init_db, session_scope
    from services.prices.stats import run_stats

    ap = argparse.ArgumentParser(prog="python -m services.prices.load")
    ap.add_argument("source_ids", nargs="*", help="price sources to load (default: all)")
    ap.add_argument("--stats", action="store_true", help="recompute the statistics afterwards")
    ap.add_argument("--init-db", action="store_true", help="create missing tables first (dev only)")
    ap.add_argument("--run", metavar="TS", help="load the months of one run (`runs/<source>/<TS>.json`) only")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.run:
        if len(args.source_ids) != 1:
            ap.error("--run takes exactly one source id")
        return _run_one(args.source_ids[0], args.run)
    engine = get_engine()
    if args.init_db:
        init_db(engine)
    factory = get_sessionmaker(engine)
    reports = []
    for source_id in args.source_ids or list(PRICE_CONNECTORS):
        with session_scope(factory) as session:
            try:
                reports.append(load_source(session, source_id))
            except Exception as exc:
                reports.append({"source_id": source_id, "error": f"{type(exc).__name__}: {exc}"[:300]})
    if args.stats:
        with session_scope(factory) as session:
            reports.append({"stats": run_stats(session)})
    sys.stdout.write(json.dumps(reports, default=str) + "\n")
    return 1 if any(r.get("error") for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
